#!/usr/bin/env bash
# 训练监视器浮窗：四档尺寸按屏幕逻辑宽度算，换屏幕/缩放自动跟着变
IDXF=/home/link/.dsh/miyu/train/.size_idx
IDX=$(cat "$IDXF" 2>/dev/null || echo 1)

# 屏幕逻辑宽度（niri 输出里 logical.width）；拿不到就退回 1706
SW=$(niri msg --json outputs 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin)
for v in d.values():
    lg=v.get("logical")
    if lg: print(lg["width"]); break
' 2>/dev/null)
[ -z "$SW" ] && SW=1706

# 四档：屏幕宽的 23.6% / 38% / 53.6% / 69.3%；单元格按 10.88px 宽、24.6px 高估
case "$IDX" in
  0) FRAC=0.236 ;; 1) FRAC=0.38 ;; 2) FRAC=0.536 ;; 3) FRAC=0.693 ;; *) FRAC=0.38 ;;
esac
PX=$(python3 -c "print(int($SW*$FRAC))")
C=$(python3 -c "print(max(24, round($PX/10.88)))")
L=$(python3 -c "print(max(10, round($PX/1.194/24.6)))")

exec env TRAIN_STATUS_RESIZE=0 TRAIN_STATUS_KEYS=noq kitty --instance-group trainstatus --class trainstatus --title "马吉训练" \
    -o remember_window_size=no \
    -o initial_window_width=${C}c -o initial_window_height=${L}c \
    -o window_padding_width=2 \
    /home/link/.dsh/miyu/scripts/train-status.sh loop
