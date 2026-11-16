#!/usr/bin/env bash
# 每 20 秒记一行：时间、GPU 显存、GPU 温度、GPU 利用率、训练速度
# 质量页用它画「吞吐 / 显存 / 温度」的迷你趋势，不然只有瞬时值
set -u
OUT=/home/link/.dsh/miyu/train/metrics.csv
[ -f "$OUT" ] || echo "ts,vram_mb,temp,util,rate" > "$OUT"
while :; do
  read -r vram temp util <<<"$(nvidia-smi --query-gpu=memory.used,temperature.gpu,utilization.gpu --format=csv,noheader,nounits | tr ',' ' ')"
  rate=""
  log=/home/link/.dsh/miyu/train/logs_v7_run.log
  if [ -f "$log" ]; then
    # 日志里是 "5.50s/it" 或 "0.18it/s"，统一换算成 it/s
    raw=$(tail -c 6000 "$log" | tr '\r' '\n' | grep -oE "[0-9.]+(it/s|s/it)" | tail -1)
    if [[ "$raw" == *"s/it" ]]; then rate=$(awk -v v="${raw%s/it*}" 'BEGIN{if (v>0) printf "%.3f", 1/v}')
    else rate="${raw%it/s}"; fi
  fi
  echo "$(date +%s),${vram:-0},${temp:-0},${util:-0},${rate:-}" >> "$OUT"
  tail -n 400 "$OUT" > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"
  sleep 20
done
