gpuno="0"
n_gpu=(${gpuno//,/ })
n_gpu=${#n_gpu[@]}

echo "Using ${n_gpu} GPU with DDP Training."

backbone='bge'
dataset=$1
stage='test'
temperature=0.2
max_turn_view_range=100

CUDA_VISIBLE_DEVICES=${gpuno} 
torchrun run.py \
  --stage ${stage} \
  --backbone ${backbone} \
  --temperature ${temperature} \
  --max_turn_view_range ${max_turn_view_range} \
  --test_batch_size 10 \
  --dev_batch_size 10 \
  --use_turn_embedding False \
  --use_role_embedding False \
  --use_sep_token False \
  --dataset ${dataset} \
  --data_dir "./datasets/${dataset}"

# > ./logs/dial2vec_${backbone}_${dataset}_${stage}_-1Epochs_${n_gpu}GPU.log 2>&1 \