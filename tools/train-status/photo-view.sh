#!/usr/bin/env bash
# 建一份与清单同序的符号链接目录，先起 imv，再用 imv-msg 跳到指定那张
# 用法：photo-view.sh <图片路径>
IMG="${1:?用法: photo-view.sh <图片路径>}"
LIST=/tmp/ts_imgs.txt
FARM=/tmp/ts_all
# 清单可能被 /tmp 清掉：缺了就按和面板同样的规则现生成
if [ ! -f "$LIST" ]; then
  /home/link/ComfyUI/.venv/bin/python -c \
    "import csv,os;rows=[r['file'] for r in csv.DictReader(open('/home/link/.dsh/miyu/train/report_v3.csv')) if os.path.exists(r['file'])];
k=lambda f:(next((x for x in f.split('/') if x.isdigit() and len(x)>=8), ''), os.path.basename(f));print('\n'.join(sorted(rows,key=k)))" > "$LIST"
fi
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
