#!/usr/bin/env bash
# 训练进度：不带参数只打印一次；加 loop 自动刷新
exec /home/link/ComfyUI/.venv/bin/python /home/link/.dsh/miyu/scripts/train-status.py "$@"
