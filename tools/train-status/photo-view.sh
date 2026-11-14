#!/usr/bin/env bash
# 建一份与清单同序的符号链接目录，先起 imv，再用 imv-msg 跳到指定那张
# 用法：photo-view.sh <图片路径>
IMG="${1:?用法: photo-view.sh <图片路径>}"
LIST=/tmp/ts_imgs.txt
FARM=/tmp/ts_all
[ -f "$LIST" ] || { echo "缺清单 $LIST" >&2; exit 1; }
mapfile -t FILES < "$LIST"
IDX=0
for i in "${!FILES[@]}"; do [ "${FILES[$i]}" = "$IMG" ] && { IDX=$i; break; }; done
mkdir -p "$FARM"
rm -f "$FARM"/*.jpg 2>/dev/null
for i in "${!FILES[@]}"; do
  ln -s "${FILES[$i]}" "$(printf '%s/%05d.jpg' "$FARM" "$i")"
done
imv -r "$FARM" &
PID=$!
echo "$PID" > /tmp/ts_imv.pid
for _ in $(seq 1 40); do
  sleep 0.1
  imv-msg "$PID" goto "$((IDX + 1))" 2>/dev/null && break
done
wait "$PID"
