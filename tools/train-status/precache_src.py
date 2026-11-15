#!/home/link/ComfyUI/.venv/bin/python
"""为图片页预生成 1280×800 源缓存：每张图只解码一次，翻页时只做缩放"""
import importlib.util, os, sys, time
from PIL import Image, ImageFile
Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = False
spec = importlib.util.spec_from_file_location("ts", "/home/link/.dsh/miyu/scripts/train-status.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
files = m.image_list()
os.makedirs(m.THUMB_DIR, exist_ok=True)
bad, done, t0 = [], 0, time.time()
for i, f in enumerate(files):
    src = m.src_thumb(f)
    if os.path.exists(src):
        continue
    try:
        im = Image.open(f); im.draft("RGB", (2560, 1600))
        im = im.convert("RGB"); im.thumbnail((m.THUMB_W, m.THUMB_H), Image.LANCZOS)
        im.save(src + ".tmp", format="PNG"); os.replace(src + ".tmp", src)
        done += 1
    except Exception as e:
        bad.append(f)
    if i % 100 == 0:
        print(f"{i}/{len(files)} 新增 {done} 坏图 {len(bad)} 用时 {int(time.time()-t0)}s", flush=True)
print(f"完成：新增 {done}，坏图 {len(bad)}，总用时 {int(time.time()-t0)}s")
open("/home/link/.dsh/miyu/train/bad_images.txt", "w").write("\n".join(bad))
