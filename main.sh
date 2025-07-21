GPUS=$1
shift
torchrun --nproc_per_node=$GPUS main.py "$@"