#!/usr/bin/env bash
set -e
export HF_ENDPOINT=https://hf-mirror.com
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
PY=/home/link/ComfyUI/.venv/bin/python
BASE=/home/link/ComfyUI/models/checkpoints/sd15-v1-5-pruned-emaonly.safetensors
DATASET=/home/link/.dsh/miyu/train/dataset_m2e
OUT=/home/link/.dsh/miyu/train/out_m2e
mkdir -p "$OUT"
cd /home/link/sd-scripts
$PY train_network.py \
  --pretrained_model_name_or_path "$BASE" \
  --train_data_dir "$DATASET" \
  --output_dir "$OUT" --output_name machi_sd15_legacy_v2 \
  --network_module networks.lora --network_dim 64 --network_alpha 32 \
  --resolution 896 --enable_bucket --min_bucket_reso 768 --max_bucket_reso 1216 \
  --train_batch_size 1 --max_train_steps 3600 \
  --learning_rate 1e-4 --text_encoder_lr 5e-5 --unet_lr 1e-4 --lr_scheduler cosine \
  --optimizer_type AdamW8bit --mixed_precision fp16 --full_fp16 --save_precision fp16 \
  --gradient_checkpointing --sdpa \
  --cache_latents --network_train_unet_only \
  --save_model_as safetensors --save_every_n_steps 1000 \
  --seed 20261113 --max_data_loader_n_workers 2 --persistent_data_loader_workers \
  --caption_extension .txt --keep_tokens 1 --min_snr_gamma 5 --noise_offset 0.02
