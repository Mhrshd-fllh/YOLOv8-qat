import torch
from utils.util import make_anchors


class SiLU(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.sigmoid = torch.nn.Sigmoid()

    def forward(self, x):
        if x.is_quantized:
            return torch.ops.quantized.mul(x, self.sigmoid(x), scale=x.q_scale(), zero_point=x.q_zero_point())
        else:
            return torch.nn.functional.relu(x) * self.sigmoid(x)


class Conv(torch.nn.Module):
    def __init__(self, in_ch, out_ch, k=1, s=1):
        super().__init__()
        self.conv = torch.nn.Conv2d(in_ch, out_ch, k, s, (k - 1) // 2, bias=False)
        self.norm = torch.nn.BatchNorm2d(out_ch, eps=0.001, momentum=0.03)
        self.relu = SiLU()

    def forward(self, x):
        return self.relu(self.norm(self.conv(x)))


class Residual(torch.nn.Module):
    def __init__(self, ch, add=True):
        super().__init__()
        self.add_m = add
        self.conv1 = Conv(ch, ch, 3)
        self.conv2 = Conv(ch, ch, 3)

    def forward(self, x):
        y = self.conv1(x)
        y = self.conv2(y)
        if self.add_m:
            if x.is_quantized and y.is_quantized:
                return torch.ops.quantized.add(x, y, scale=x.q_scale(), zero_point=x.q_zero_point())
            else:
                return x + y
        else:
            return y


class CSP(torch.nn.Module):
    def __init__(self, in_ch, out_ch, n=1, add=True):
        super().__init__()
        self.conv1 = Conv(in_ch, out_ch)
        self.conv2 = Conv((2 + n) * out_ch // 2, out_ch)
        self.res_m = torch.nn.ModuleList(Residual(out_ch // 2, add) for _ in range(n))

    def forward(self, x):
        y_list = list(self.conv1(x).chunk(2, 1))
        for m in self.res_m:
            y_list.append(m(y_list[-1]))

        # Handle quantized tensors safely
        if any(t.is_quantized for t in y_list):
            dequantized = [t.dequantize() if t.is_quantized else t for t in y_list]
            y_cat = torch.cat(dequantized, dim=1)
            scale = x.q_scale() if x.is_quantized else 1.0
            zero_point = x.q_zero_point() if x.is_quantized else 0
            y_out = torch.quantize_per_tensor(y_cat, scale=scale, zero_point=zero_point, dtype=torch.quint8)
        else:
            y_out = torch.cat(y_list, dim=1)

        return self.conv2(y_out)


class SPP(torch.nn.Module):
    def __init__(self, in_ch, out_ch, k=5):
        super().__init__()
        self.conv1 = Conv(in_ch, in_ch // 2)
        self.conv2 = Conv(in_ch * 2, out_ch)
        self.res_m = torch.nn.MaxPool2d(k, 1, k // 2)

    def forward(self, x):
        x = self.conv1(x)
        y1 = self.res_m(x)
        y2 = self.res_m(y1)
        return self.conv2(torch.cat([x, y1, y2, self.res_m(y2)], 1))


class DarkNet(torch.nn.Module):
    def __init__(self, width, depth):
        super().__init__()
        self.p1 = torch.nn.Sequential(
            Conv(width[0], width[1], 3, 2)
        )
        self.p2 = torch.nn.Sequential(
            Conv(width[1], width[2], 3, 2),
            CSP(width[2], width[2], depth[0])
        )
        self.p3 = torch.nn.Sequential(
            Conv(width[2], width[3], 3, 2),
            CSP(width[3], width[3], depth[1])
        )
        self.p4 = torch.nn.Sequential(
            Conv(width[3], width[4], 3, 2),
            CSP(width[4], width[4], depth[2])
        )
        self.p5 = torch.nn.Sequential(
            Conv(width[4], width[5], 3, 2),
            CSP(width[5], width[5], depth[0]),
            SPP(width[5], width[5])
        )

    def forward(self, x):
        p1 = self.p1(x)
        p2 = self.p2(p1)
        p3 = self.p3(p2)
        p4 = self.p4(p3)
        p5 = self.p5(p4)
        return p3, p4, p5


class DarkFPN(torch.nn.Module):
    def __init__(self, width, depth):
        super().__init__()
        self.up = torch.nn.Upsample(scale_factor=2, mode='nearest')  # اضافه شدن mode
        self.h1 = CSP(width[4] + width[5], width[4], depth[0], False)
        self.h2 = CSP(width[3] + width[4], width[3], depth[0], False)
        self.h3 = Conv(width[3], width[3], 3, 2)
        self.h4 = CSP(width[3] + width[4], width[4], depth[0], False)
        self.h5 = Conv(width[4], width[4], 3, 2)
        self.h6 = CSP(width[4] + width[5], width[5], depth[0], False)

    def forward(self, p3, p4, p5):
        p4 = self.h1(torch.cat([self.up(p5), p4], 1))
        p3 = self.h2(torch.cat([self.up(p4), p3], 1))
        p4 = self.h4(torch.cat([self.h3(p3), p4], 1))
        p5 = self.h6(torch.cat([self.h5(p4), p5], 1))
        return p3, p4, p5


class Head(torch.nn.Module):
    def __init__(self, nc=80, ch=()):
        super().__init__()
        self.nc = nc  # number of classes
        self.no = nc + 4  # number of outputs per anchor
        self.stride = torch.zeros(len(ch))  # strides computed during build

        box = max(64, ch[0] // 4)
        cls = max(80, ch[0], self.nc)

        self.box = torch.nn.ModuleList(torch.nn.Sequential(
            Conv(x, box, 3),
            Conv(box, box, 3),
            torch.nn.Conv2d(box, 4, 1)
        ) for x in ch)

        self.cls = torch.nn.ModuleList(torch.nn.Sequential(
            Conv(x, cls, 3),
            Conv(cls, cls, 3),
            torch.nn.Conv2d(cls, self.nc, 1)
        ) for x in ch)

    def forward(self, p3, p4, p5):
        box_out0 = self.box[0](p3)
        cls_out0 = self.cls[0](p3)
        out0 = torch.cat((box_out0, cls_out0), dim=1)

        box_out1 = self.box[1](p4)
        cls_out1 = self.cls[1](p4)
        out1 = torch.cat((box_out1, cls_out1), dim=1)

        box_out2 = self.box[2](p5)
        cls_out2 = self.cls[2](p5)
        out2 = torch.cat((box_out2, cls_out2), dim=1)

        return [out0, out1, out2]

class YOLO(torch.nn.Module):
    def __init__(self, width, depth, num_classes):
        super().__init__()
        self.net = DarkNet(width, depth)
        self.fpn = DarkFPN(width, depth)
        img_dummy = torch.zeros(1, width[0], 256, 256)
        self.head = Head(num_classes, (width[3], width[4], width[5]))
        self.head.stride = torch.tensor([256 / x.shape[-2] for x in self.forward(img_dummy)])
        self.stride = self.head.stride

    def forward(self, x):
        p3, p4, p5 = self.net(x)
        p3, p4, p5 = self.fpn(p3, p4, p5)
        return self.head(p3, p4, p5)


class QAT(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model
        self.quant = torch.quantization.QuantStub()
        self.de_quant = torch.quantization.DeQuantStub()
        self.nc = self.model.head.nc
        self.no = self.model.head.no
        self.stride = self.model.stride

    def forward(self, x):
        x = self.quant(x)
        x = self.model(x)
        return [self.de_quant(f) for f in x]

# Factory functions
def yolo_v8_n(num_classes: int = 80):
    depth = [1, 2, 2]
    width = [3, 16, 32, 64, 128, 256]
    return YOLO(width, depth, num_classes)


def yolo_v8_t(num_classes: int = 80):
    depth = [1, 2, 2]
    width = [3, 24, 48, 96, 192, 384]
    return YOLO(width, depth, num_classes)


def yolo_v8_s(num_classes: int = 80):
    depth = [1, 2, 2]
    width = [3, 32, 64, 128, 256, 512]
    return YOLO(width, depth, num_classes)


def yolo_v8_m(num_classes: int = 80):
    depth = [2, 4, 4]
    width = [3, 48, 96, 192, 384, 576]
    return YOLO(width, depth, num_classes)


def yolo_v8_l(num_classes: int = 80):
    depth = [3, 6, 6]
    width = [3, 64, 128, 256, 512, 512]
    return YOLO(width, depth, num_classes)


def yolo_v8_x(num_classes: int = 80):
    depth = [3, 6, 6]
    width = [3, 80, 160, 320, 640, 640]
    return YOLO(width, depth, num_classes)
