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

env TRAIN_STATUS_RESIZE=0 TRAIN_STATUS_KEYS=noq kitty --instance-group trainstatus --class trainstatus --title "马吉训练" \
    -o remember_window_size=no \
    -o initial_window_width=${C}c -o initial_window_height=${L}c \
    -o window_padding_width=0 \
    -o background_opacity=${TRAIN_STATUS_OPACITY:-0.15} \
    /home/link/.dsh/miyu/scripts/train-status.sh loop &

# 等窗口出现，把它按到「当前工作区」，否则浮动窗会挂在 workspace=None 上，
# 合成时后面没有窗口，透明就变成透壁纸
for _ in $(seq 1 40); do
    sleep 0.25
    ID=$(niri msg --json windows 2>/dev/null | python3 -c '
import json,sys
for w in json.load(sys.stdin):
    if w.get("app_id") == "trainstatus":
        print(w["id"]); break
' 2>/dev/null)
    [ -n "$ID" ] && break
done
[ -n "$ID" ] && niri msg action move-window-to-workspace --window-id "$ID" 1 >/dev/null 2>&1
wait
