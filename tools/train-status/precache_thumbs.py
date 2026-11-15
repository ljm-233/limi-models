#!/home/link/ComfyUI/.venv/bin/python
"""预生成“面板最终尺寸”的缩略图，让翻页时不必现算"""
import importlib.util, os, sys, time
spec=importlib.util.spec_from_file_location("ts","/home/link/.dsh/miyu/scripts/train-status.py")
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
fs=m.image_list(); done=0; t0=time.time()
for i,f in enumerate(fs):
    tp=m.thumb(f, 0, 0, (1744,1441))
    if os.path.exists(tp): continue
    m.kitty_img(f, 0, 0, 1, (1744,1441), 999)
    done+=1
    if i%100==0: print(f"{i}/{len(fs)} 新增{done} {int(time.time()-t0)}s", flush=True)
print("完成 新增", done, "用时", int(time.time()-t0), "s")
