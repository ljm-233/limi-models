#!/usr/bin/env bash
# 训练结束后自动跑评估：等训练进程消失 → 拉起 ComfyUI → 每档权重出图打分
set -u
while pgrep -f "sdxl_train[_]network" >/dev/null; do sleep 60; done
echo "$(date '+%H:%M') 训练结束，开始评估" >> /home/link/.dsh/miyu/train/eval_v7.log
if ! curl -s -m 3 http://127.0.0.1:8188/system_stats >/dev/null; then
  (cd /home/link/ComfyUI && setsid nohup /home/link/ComfyUI/.venv/bin/python main.py --listen 127.0.0.1 --port 8188 >> /tmp/comfy_v11.log 2>&1 &)
  for i in $(seq 1 40); do curl -s -m 3 http://127.0.0.1:8188/system_stats >/dev/null && break; sleep 6; done
fi
/home/link/ComfyUI/.venv/bin/python /home/link/.dsh/miyu/scripts/eval_v7.py >> /home/link/.dsh/miyu/train/eval_v7.log 2>&1
echo "$(date '+%H:%M') 评估完成" >> /home/link/.dsh/miyu/train/eval_v7.log
