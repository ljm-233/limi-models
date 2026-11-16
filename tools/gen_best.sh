#!/usr/bin/env bash
# 定稿出图：默认走 InstantID（中心 0.742，目前最高），可切 pulid / faceid
# 用法: gen_best.sh <前缀> <提示词> <seed...>
set -u
PREFIX="${1:?前缀}"; PROMPT="${2:?提示词}"; shift 2
REF="${REF:-/home/link/.dsh/miyu/train/refs_machi/r1.png}"   # 马吉本人参考脸
MODE="${GEN_MODE:-iid}"        # iid = InstantID(默认) / pulid / faceid / sd15
LORA=machi_sdxl_id_v6.safetensors
LW=${LORA_W:-0.7}
WID=${INSTANTID_W:-1.0}
PY=/home/link/ComfyUI/.venv/bin/python
D=/home/link/.dsh/miyu/scripts

case "$MODE" in
  iid)
    for sd in "$@"; do
      timeout 900 $PY "$D/gen_instantid.py" "$PREFIX" "$REF" "$sd" 832 1216 "$WID" "$PROMPT" "$LORA" "$LW" | tail -1
    done ;;
  pulid)
    HF_HUB_OFFLINE=1 TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 timeout 900 $PY "$D/gen_pulid.py" "$PREFIX" "$REF" | tail -1 ;;
  faceid)
    for sd in "$@"; do
      LORA_NAME=$LORA LORA_W=$LW timeout 600 $PY "$D/gen_faceid.py" "$PROMPT" \
        "cartoon, anime, 3d, plastic skin, blurry, watermark, text" "$sd" "$PREFIX" "$REF" 0.8 832 1216 | tail -1
    done ;;
  sd15)
    for sd in "$@"; do
      LORA_NAME=machi_sd15_id_v1.safetensors LORA_W=0.85 timeout 600 $PY "$D/gen_faceid.py" "$PROMPT" \
        "cartoon, anime, 3d, plastic skin, blurry, watermark, text" "$sd" "$PREFIX" "$REF" 1.0 512 768 majicmixRealistic_v7.safetensors | tail -1
    done ;;
esac
