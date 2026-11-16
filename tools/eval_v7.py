#!/usr/bin/env python
"""对 out_sdxl_v7 的每个检查点跑一次「纯 LoRA + machi」出图并打分，写 train/eval_v7.json。

为什么要它：loss 对身份 LoRA 没参考价值；只有「这一档权重能不能自己画出她」才是真信号。
结果按步数存，质量页画成「步数 → 中心分」的曲线。
"""
import glob, json, os, subprocess, sys, time

T = "/home/link/.dsh/miyu/train"
OUT = f"{T}/out_sdxl_v7"
RES = f"{T}/eval_v7.json"
PY = "/home/link/ComfyUI/.venv/bin/python"
PROMPT = "machi, face close-up, portrait, natural photo, detailed skin, sharp focus"


def wait_comfy(timeout=180):
    import urllib.request
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=3)
            return True
        except Exception:
            time.sleep(5)
    return False


def main():
    res = {}
    if os.path.exists(RES):
        try:
            res = json.load(open(RES))
        except Exception:
            res = {}
    for ck in sorted(glob.glob(f"{OUT}/*step*.safetensors")):
        step = int(ck.split("-step")[1].split(".")[0])
        if str(step) in res:
            continue
        name = os.path.basename(ck)
        pref = f"v7eval_{step}"
        subprocess.run([PY, "/home/link/.dsh/miyu/scripts/gen_lora.py", pref, PROMPT, "7", name, "832", "1.0"],
                       check=False)
        hits = sorted(glob.glob(f"/home/link/ComfyUI/output/{pref}_*.png"))
        if not hits:
            print(f"step {step}: 出图失败", flush=True)
            continue
        out = subprocess.run([PY, "/home/link/.dsh/miyu/scripts/score_params.py",
                              os.path.dirname(hits[0])],
                             capture_output=True, text=True).stdout
        val = None
        for line in out.splitlines():
            if pref in line and "中心" in line:
                try:
                    val = float(line.split("中心")[1].split()[0])
                except Exception:
                    pass
        if val is not None:
            res[str(step)] = round(val, 4)
            json.dump(res, open(RES, "w"), ensure_ascii=False, indent=1)
            print(f"step {step}: 中心 {val:.3f}", flush=True)


if __name__ == "__main__":
    if not wait_comfy():
        print("ComfyUI 没起来，先跳过", flush=True)
        sys.exit(0)
    main()
