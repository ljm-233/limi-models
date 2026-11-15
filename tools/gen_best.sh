#!/usr/bin/env bash
# 定稿出图：LoRA(v5) + FaceID，一套参数一行命令
# 用法: gen_best.sh <前缀> <提示词> <seed...>
set -u
PREFIX="${1:?前缀}"; PROMPT="${2:?提示词}"; shift 2
REF=/home/link/.dsh/miyu/train/refs_crop/r1.png
MODE="${GEN_MODE:-sd15}"      # sd15 = 当前最优；sdxl = 老路线
NEG="cartoon, anime, 3d, plastic skin, blurry, watermark, text, extra limbs"
for sd in "$@"; do
  if [ "$MODE" = "sdxl" ]; then
    LORA_NAME=machi_sdxl_id_v5.safetensors LORA_W=0.85 \
    /home/link/ComfyUI/.venv/bin/python /home/link/.dsh/miyu/scripts/gen_faceid.py \
      "$PROMPT" "$NEG" "$sd" "$PREFIX" "$REF" 1.0 768 1024 | tail -1
  else
    LORA_NAME=machi_sd15_id_v1.safetensors LORA_W=0.85 \
    /home/link/ComfyUI/.venv/bin/python /home/link/.dsh/miyu/scripts/gen_faceid.py \
      "$PROMPT" "$NEG" "$sd" "$PREFIX" "$REF" 1.0 512 768 majicmixRealistic_v7.safetensors | tail -1
  fi
done
