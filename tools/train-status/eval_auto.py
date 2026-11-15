#!/home/link/ComfyUI/.venv/bin/python
"""训练完自动评估：拷权重 → 起 ComfyUI → 出四张 → 算身份/纹理 → 追加进 machi/v3.md

用法: eval_auto.py <权重文件> <新名字(不含.safetensors)> [提示词]
"""
import json, os, subprocess, sys, time, urllib.request
import glob, numpy as np, cv2

LORA_DIR = "/home/link/ComfyUI/models/loras"
TRAIN = "/home/link/.dsh/miyu/train"
REF_GLOB = "/home/link/.dsh/miyu/train/refs_crop/*.png"
DOC = "/home/link/.dsh/miyu/limi-models/machi/v3.md"
C = "http://127.0.0.1:8188"
PROMPT = sys.argv[3] if len(sys.argv) > 3 else \
    "ohw2 woman, white hair, blue eyes, cosplay, upper body, soft light, looking at viewer, 1girl, solo"
NEG = "lowres, bad anatomy, worst quality, watermark"
SEEDS = (11, 22, 33, 44)


def comfy_up():
    try:
        urllib.request.urlopen(C + "/system_stats", timeout=2); return True
    except Exception:
        return False


def start_comfy():
    if comfy_up():
        return True
    subprocess.Popen(["/home/link/ComfyUI/.venv/bin/python", "main.py", "--listen", "127.0.0.1", "--port", "8188"],
                     cwd="/home/link/ComfyUI", stdout=open("/tmp/comfy_eval.log", "wb"),
                     stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(40):
        time.sleep(5)
        if comfy_up():
            return True
    return False


def gen(lora, seed, prefix):
    subprocess.run(["/home/link/ComfyUI/.venv/bin/python", "/home/link/.dsh/miyu/scripts/gen_detail.py",
                    PROMPT, NEG, str(seed), prefix, lora, "0.8", "768", "1024",
                    "majicmixRealistic_v7.safetensors"], check=False)


def score(paths):
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_size=(640, 640))
    c = np.load(f"{TRAIN}/face_centroid.npy").reshape(-1)
    refs = []
    for f in glob.glob(REF_GLOB):
        img = cv2.imread(f)
        fs = app.get(img)
        if fs:
            refs.append(max(fs, key=lambda x: (x.bbox[2] - x.bbox[0])).normed_embedding)
    sims, simc, tex = [], [], []
    for p in paths:
        img = cv2.imread(p)
        fs = app.get(img)
        if fs:
            e = max(fs, key=lambda x: (x.bbox[2] - x.bbox[0])).normed_embedding
            sims.append(max(float(np.dot(e, r)) for r in refs))
            simc.append(float(np.dot(e, c)))
        g = cv2.resize(cv2.imread(p, 0), (512, 512))
        tex.append(float(np.abs(cv2.Laplacian(g, cv2.CV_64F)).mean()))
    return (float(np.mean(sims)) if sims else 0.0,
            float(np.mean(simc)) if simc else 0.0,
            float(np.mean(tex)) if tex else 0.0)


def main():
    src, name = sys.argv[1], sys.argv[2]
    dst = f"{LORA_DIR}/{name}.safetensors"
    if not os.path.exists(dst):
        subprocess.run(["cp", src, dst], check=True)
    if not start_comfy():
        print("ComfyUI 起不来"); return 1
    made = []
    for s in SEEDS:
        pre = f"eval_{name}_{s}"
        gen(f"{name}.safetensors", s, pre)
        hit = sorted(glob.glob(f"/home/link/ComfyUI/output/{pre}_*.png"))
        if hit:
            made.append(hit[-1])
    if not made:
        print("没出图"); return 1
    a, b, t = score(made)
    line = (f"\n| `{name}` | 自动评估 {time.strftime('%m-%d %H:%M')} | {a:.3f} | {b:.3f} | {t:.2f} |\n")
    with open(DOC, "a", encoding="utf-8") as f:
        f.write(line)
    print(f"{name}: 对真人 {a:.3f} 对均值脸 {b:.3f} 纹理 {t:.2f}（{len(made)} 张）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
