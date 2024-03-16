#! /bin/bash

BASE_PATH=${1-"/home/MiniLLM"}

# type
TYPE="toy"
# hp
LR=0.01
BATCH_SIZE=-1
# runtime
SAVE_PATH="${BASE_PATH}/results/${TYPE}"
# seed
SEED=10


OPTS=""
# type
OPTS+=" --type ${TYPE}"
# model
OPTS+=" --model-type linear_dnn"
OPTS+=" --base-path ${BASE_PATH}"
OPTS+=" --input-dim 128"
OPTS+=" --dnn-hidden-dim 128"
# data
OPTS+=" --train-num 1024"
OPTS+=" --dev-num 256"
OPTS+=" --test-num 256"
OPTS+=" --train-mu 0.0"
OPTS+=" --train-sigma 2.0"
OPTS+=" --train-noise 1"
OPTS+=" --dev-mu 2.0"
OPTS+=" --dev-sigma 0.1"
OPTS+=" --dev-noise 0.0"
# hp
OPTS+=" --lr ${LR}"
OPTS+=" --batch-size ${BATCH_SIZE}"
OPTS+=" --epochs 1000"
OPTS+=" --log-interval 100"
OPTS+=" --lam 0.0"
# runtime
OPTS+=" --save ${SAVE_PATH}"
# seed
OPTS+=" --seed ${SEED}"


export NCCL_DEBUG=""
# export WANDB_DISABLED=True
export TF_CPP_MIN_LOG_LEVEL=3
export PYTHONPATH=${BASE_PATH}
export OMP_NUM_THREADS=16
CMD="python3 ${BASE_PATH}/toy/linear_dnn/main.py ${OPTS} $@"

echo ${CMD}
echo "PYTHONPATH=${PYTHONPATH}"
mkdir -p ${SAVE_PATH}
${CMD}
