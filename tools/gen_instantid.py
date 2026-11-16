#!/usr/bin/env python
"""InstantID：用参考脸给 SDXL 注入身份。用法:
gen_instantid.py PREFIX REF SEED [W] [H] [WID] [PROMPT] [LORA] [LORAW]"""
import json, os, shutil, sys, time, urllib.request, uuid

PREF = sys.argv[1]; REF = sys.argv[2]; SEED = int(sys.argv[3])
W = int(sys.argv[4]) if len(sys.argv) > 4 else 832
H = int(sys.argv[5]) if len(sys.argv) > 5 else 1216
WID = float(sys.argv[6]) if len(sys.argv) > 6 else 0.8
PROMPT = sys.argv[7] if len(sys.argv) > 7 else "photo of a woman, natural skin texture, sharp focus, 85mm"
LORA = sys.argv[8] if len(sys.argv) > 8 else ""
LOW = float(sys.argv[9]) if len(sys.argv) > 9 else 0.7
CK = "RealVisXL_V5.0_fp16.safetensors"
NEG = "cartoon, anime, 3d, plastic skin, blurry, watermark, text, doll face"
INP = "/home/link/ComfyUI/input"; HOST = "http://127.0.0.1:8188"

shutil.copy(REF, os.path.join(INP, "instantid_ref.png"))
wf = {
 "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CK}},
 "2": {"class_type": "InstantIDModelLoader", "inputs": {"instantid_file": "ip-adapter.bin"}},
 "3": {"class_type": "InstantIDFaceAnalysis", "inputs": {"provider": "CPU"}},
 "4": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": "instantid_diffusion_pytorch_model.safetensors"}},
 "5": {"class_type": "LoadImage", "inputs": {"image": "instantid_ref.png"}},
 "6": {"class_type": "ApplyInstantID", "inputs": {"instantid": ["2",0], "insightface": ["3",0],
        "control_net": ["4",0], "image": ["5",0], "model": ["1",0], "positive": ["8",0], "negative": ["9",0],
        "weight": WID, "start_at": 0.0, "end_at": 1.0}},
 "7": {"class_type": "EmptyLatentImage", "inputs": {"width": W, "height": H, "batch_size": 1}},
 "8": {"class_type": "CLIPTextEncode", "inputs": {"text": PROMPT, "clip": ["1",1]}},
 "9": {"class_type": "CLIPTextEncode", "inputs": {"text": NEG, "clip": ["1",1]}},
 "10": {"class_type": "KSampler", "inputs": {"model": ["6",0], "positive": ["6",1], "negative": ["6",2],
        "latent_image": ["7",0], "seed": SEED, "steps": 30, "cfg": 5.0,
        "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0}},
 "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10",0], "vae": ["1",2]}},
 "12": {"class_type": "SaveImage", "inputs": {"images": ["11",0], "filename_prefix": PREF}},
}
if LORA:
    wf["20"] = {"class_type": "LoraLoader", "inputs": {"model": ["1",0], "clip": ["1",1],
        "lora_name": LORA, "strength_model": LOW, "strength_clip": LOW}}
    wf["6"]["inputs"]["model"] = ["20",0]

body = json.dumps({"prompt": wf, "client_id": str(uuid.uuid4())}).encode()
pid = json.load(urllib.request.urlopen(urllib.request.Request(f"{HOST}/prompt", body, {"Content-Type":"application/json"}), timeout=30))["prompt_id"]
for _ in range(600):
    time.sleep(1)
    h = json.load(urllib.request.urlopen(f"{HOST}/history/{pid}", timeout=20))
    if pid in h:
        for k, v in h[pid]["outputs"].items():
            if v.get("images"): print(v["images"][0]["filename"]); sys.exit(0)
        print("无输出:", json.dumps(h[pid].get("status",{}))[:300], file=sys.stderr); sys.exit(1)
print("超时", file=sys.stderr); sys.exit(1)
