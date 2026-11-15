#!/usr/bin/env bash
# 训练进度：不带参数只打印一次；加 loop 常驻刷新，源码改动后自动重启（退出码 42）
PY=/home/link/ComfyUI/.venv/bin/python
SRC=/home/link/.dsh/miyu/scripts/train-status.py
if [ "${1:-}" != "loop" ]; then
  exec "$PY" "$SRC"
fi
while :; do
  "$PY" "$SRC" loop
  [ $? -eq 42 ] || break
  sleep 0.2
done
