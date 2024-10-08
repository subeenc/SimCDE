#!/bin/bash

gpuno="0, 1"
IFS=',' read -ra ADDR <<< "$gpuno"
n_gpu=${#ADDR[@]}

echo "Using ${n_gpu} GPU with DDP Training."

backbone='plato'
dataset=$1
stage=$2
num_train_epochs=$3
test_interval=$4

CUDA_VISIBLE_DEVICES=${gpuno} \
torchrun run.py \
  --stage ${stage} \
  --backbone ${backbone} \
  --dataset ${dataset} \
  --temperature 0.2 \
  --max_turn_view_range 10 \
  --test_interval ${test_interval:-"100"} \
  --use_response False \
  --sep_token "[unused1]" \
  --num_train_epochs ${num_train_epochs} \
  --init_checkpoint "PLATO.pt" \
  --config_file "plato/config.json" \
  --data_dir "./datasets/${dataset}" \
  --model_dir "./dial2vec/"

  # > ./logs/dial2vec_${backbone}_${dataset}_${stage}_${num_train_epochs}Epochs_${n_gpu}GPU.log 2>&1
