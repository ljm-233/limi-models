#!/usr/bin/env python3
"""马吉训练监视器（kitty 浮窗）

六页：环境 / 流程 / 质量 / 数据 / 日志 / 图片
  e 下一页  q 上一页（浮窗里）  z 缩小  Z 放大  p 暂停  c 占用视图
  图片页：滚轮翻图，左键或 i 开 imv 看原图
尺寸四档按屏幕逻辑宽度算，换屏幕/缩放自动跟着变。
"""
import glob
import json
import os
import re
import select
import struct
import subprocess
import sys
import termios
import tty
import time
import unicodedata
import urllib.request
import fcntl
import csv
import hashlib
import shutil
import collections

HOME = "/home/link"
TRAIN = f"{HOME}/.dsh/miyu/train"
COMFY = f"{HOME}/ComfyUI"
SIZE_FILE = f"{TRAIN}/.size_idx"
BASE_FILE = f"{TRAIN}/.status_base"
THUMB_DIR = f"{HOME}/.dsh/miyu/cache/thumbs"
IMV_PID = "/tmp/ts_imv.pid"
IMG_POS = f"{TRAIN}/.img_pos"

R = "\033[0m"
DIM = "\033[38;5;240m"
TXT = "\033[38;5;252m"
WH = "\033[38;2;235;240;255m"
CYA = "\033[38;2;148;226;213m"
YEL = "\033[38;2;249;226;175m"
GRN = "\033[38;2;166;227;161m"
VIO = "\033[38;2;203;166;247m"
HOT = "\033[38;2;243;139;168m"

VIEWS = ["环境", "流程", "质量", "数据", "日志", "图片"]
_CUR_VIEW = [0]
_NARROW = [False]
H_ROWS = [0]                # 当前窗口行数，供分页用        # 窗口窄：标签缩写、行距收紧
_C = {}


# ── 小工具 ────────────────────────────────────────────
def wlen(s):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in re.sub(r"\033\[[0-9;]*m", "", s))


def fit(text, W, align="left"):
    pad = max(0, W - wlen(text))
    if align == "right":
        return " " * pad + text
    if align == "center":
        return " " * (pad // 2) + text
    return text


def clip(text, W):
    out, w, i = [], 0, 0
    while i < len(text):
        m = re.match(r"\033\[[0-9;]*m", text[i:])
        if m:
            out.append(m.group(0))
            i += len(m.group(0))
            continue
        ch = text[i]
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw <= W:
            out.append(ch)
            w += cw
        i += 1
    return "".join(out) + R


def cache(key, secs, fn):
    now = time.time()
    hit = _C.get(key)
    if hit and now - hit[0] < secs:
        return hit[1]
    val = fn()
    if len(_C) > 80:
        _C.clear()
    _C[key] = (now, val)
    return val


def alive(pat, ttl=2.0):
    """真的在看"这个脚本在跑"：只比对 argv 里的脚本文件名，不在整条命令行里做子串匹配。
    否则我自己跑的 shell 命令只要提到 train_network，就会误报成"训练中"。"""
    def probe():
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                args = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "ignore").split("\0")
            except Exception:
                continue
            args = [a for a in args if a]
            if not args:
                continue
            exe = os.path.basename(args[0])
            if not (exe.startswith("python") or exe == "accelerate"):
                continue                      # 只认 python 进程，shell/编辑器不算
            for a in args[1:]:
                base = os.path.basename(a)
                if base == pat or base == pat + ".py" or base.startswith(pat + "."):
                    return True
        return False
    return cache("alive:" + pat, ttl, probe)


def read(path, default=""):
    try:
        return open(path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return default


# ── 硬件 ───────────────────────────────────────────────
def _nvml():
    if "nvml" not in _C:
        try:
            import pynvml
            pynvml.nvmlInit()
            _C["nvml"] = (0, pynvml.nvmlDeviceGetHandleByIndex(0), pynvml)
        except Exception:
            _C["nvml"] = (0, None, None)
    return _C["nvml"][1], _C["nvml"][2]


def gpu():
    h, pynvml = _nvml()
    if h is None:
        return None
    try:
        u = pynvml.nvmlDeviceGetUtilizationRates(h).gpu
        m = pynvml.nvmlDeviceGetMemoryInfo(h)
        t = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        pw = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
        try:
            lim = pynvml.nvmlDeviceGetEnforcedPowerLimit(h) / 1000.0
        except Exception:
            lim = 0
        return u, m.used / 2**30, m.total / 2**30, t, pw, lim
    except Exception:
        return None


def mem():
    d = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
    return ((d["MemTotal"] - d["MemAvailable"]) / 1048576, d["MemTotal"] / 1048576,
            (d.get("SwapTotal", 0) - d.get("SwapFree", 0)) / 1048576)


def cpu_pct():
    def snap():
        v = [int(x) for x in open("/proc/stat").readline().split()[1:]]
        return sum(v), v[3] + v[4]
    a = snap()
    time.sleep(0.05)
    b = snap()
    dt = b[0] - a[0]
    return 0 if dt <= 0 else int(100 * (1 - (b[1] - a[1]) / dt))


def cpu_temp():
    """优先取 coretemp 的 Package id 0；没有就取核心最高温；再不行用 acpitz"""
    pkg = best = acpi = None
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read(f"{d}/name").strip()
        for f in glob.glob(f"{d}/temp*_input"):
            v = int(read(f, "0") or 0) / 1000.0
            if v <= 0:
                continue
            lab = read(f.replace("_input", "_label")).strip()
            if name == "coretemp":
                if "Package" in lab:
                    pkg = v if pkg is None else max(pkg, v)
                else:
                    best = v if best is None else max(best, v)
            elif name == "acpitz":
                acpi = v if acpi is None else max(acpi, v)
    return pkg or best or acpi


def load1():
    try:
        return float(open("/proc/loadavg").read().split()[0])
    except Exception:
        return 0.0


def comfy_online():
    try:
        urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=0.3)
        return True
    except Exception:
        return False


def top_procs(n=3):
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            comm = open(f"/proc/{pid}/stat").read().split("(")[1].split(")")[0]
            rss = int(open(f"/proc/{pid}/statm").read().split()[1]) * 4096 / 2**30
            out.append((rss, comm))
        except Exception:
            pass
    out.sort(reverse=True)
    return out[:n]


# ── 日志 ───────────────────────────────────────────────
TRACK_FILE = "/tmp/ts_track.json"


def tracked_log():
    """启动训练/出图时写进来的日志路径——面板只认它，不猜最新文件"""
    try:
        d = json.load(open(TRACK_FILE))
        p = d.get("log")
        if p and os.path.exists(p) and time.time() - os.path.getmtime(p) < 6 * 3600:
            return p
    except Exception:
        pass
    return None


def last_log():
    t = tracked_log()
    if t:
        return t
    now, best = time.time(), None
    for f in glob.glob("/tmp/*.log") + glob.glob("/tmp/*.out"):
        try:
            if now - os.path.getmtime(f) > 3600:
                continue
            with open(f, "rb") as fh:
                fh.seek(max(0, os.path.getsize(f) - 32768))
                tail = fh.read().decode("utf-8", "ignore")
            if any(k in tail for k in ("avr_loss", "steps:", "it/s", "s/it")):
                if best is None or os.path.getmtime(f) > os.path.getmtime(best):
                    best = f
        except Exception:
            pass
    return best


def log_text(path):
    if not path:
        return ""
    try:
        st = os.stat(path)
        key = f"log:{path}:{st.st_mtime}:{st.st_size}"
        if key in _C:
            return _C[key][1]
        with open(path, "rb") as f:
            f.seek(max(0, st.st_size - 65536))
            txt = f.read().decode("utf-8", "ignore").replace("\r", "\n")
        _C[key] = (time.time(), txt)
        return txt
    except Exception:
        return ""


def parse(text):
    """只看最后一条进度行：步数、总数、速度、loss 都取它，避免混用不同轮的行"""
    last = None
    losses = []
    for line in text.splitlines():
        m = re.search(r"(\d+)/(\d+)\s*\[[\d:]+<[\d:]+,\s*([\d.]+)(it/s|s/it)(?:,\s*avr_loss=([\d.]+))?", line)
        if m:
            last = m
            if m.group(5):
                losses.append(float(m.group(5)))
    if not last:
        return None, None, None, losses
    step, total = int(last.group(1)), int(last.group(2))
    v = float(last.group(3))
    rate = v if last.group(4) == "it/s" else 1.0 / max(v, 1e-6)
    try:
        base = int(read(BASE_FILE).strip())
        if 0 < base < total * 20:          # 明显是脏值就不加，别把 200/200 变成 7200/200
            step += base
            total += base
    except Exception:
        pass
    return step, total, rate, losses


def progress():
    """日志被 Python 缓冲时步数会滞后，这时用最新存档的步数兜底
    注意：兜底步数只用来显示，不参与翻轮判定，否则轮次会算多"""
    p = last_log()
    step, total, rate, losses = cache("parse:" + str(p), 0.5, lambda: parse(log_text(p)))
    log_step = step
    src = "log"
    try:
        stale = p and time.time() - os.path.getmtime(p) > 60
    except Exception:
        stale = False
    if stale and alive("train_network"):    # 只有训练进程还在时，才用存档步数兜底
        best = 0
        for f in glob.glob(f"{TRAIN}/out_*/*-step*.safetensors"):
            m = re.search(r"-step(\d+)", os.path.basename(f))
            if m:
                best = max(best, int(m.group(1)))
        if best and (step is None or best > step):
            step, src = best, "ckpt"
    return step, total, rate, losses, log_step, src


LORA_DIR = "/home/link/ComfyUI/models/loras"


def pretty_model(fname):
    """machi_sd15_id_v1.safetensors -> 马吉 · sd15 · id · v1"""
    if not fname:
        return ""
    stem = os.path.basename(fname).rsplit(".", 1)[0]
    parts = stem.split("_")
    if len(parts) >= 4:
        head, model, cls, ver = parts[0], parts[1], parts[2], "_".join(parts[3:])
        pretty = {"machi": "马吉", "aowo": "嗷呜虎虎"}.get(head, head)
        return f"{pretty} {model}·{cls}·{ver}"
    return stem


def in_use_lora():
    """当前项目在本机最新装上的那个权重（按文件时间）"""
    try:
        proj = read(f"{TRAIN}/.project_name", "machi").strip().split()[0] or "machi"
    except Exception:
        proj = "machi"
    try:
        fs = [f for f in os.listdir(LORA_DIR) if f.endswith(".safetensors") and f.startswith(proj)]
        if not fs:
            return None
        f = max(fs, key=lambda x: os.path.getmtime(os.path.join(LORA_DIR, x)))
        return f, os.path.getmtime(os.path.join(LORA_DIR, f))
    except Exception:
        return None


def latest_weight():
    """最近一次训出来的权重（用来在空闲时显示"上次做到哪"）"""
    try:
        outs = glob.glob(f"{TRAIN}/out_*/*.safetensors")
        if not outs:
            return None
        f = max(outs, key=os.path.getmtime)
        return os.path.basename(f), os.path.getmtime(f), os.path.getsize(f) / 1048576
    except Exception:
        return None


def idle_now():
    """没有训练/出图/评分进程，且日志 10 分钟没更新 → 真空闲（不是在跑但卡住）"""
    if alive("train_network") or alive("gen_detail") or alive("gen_face") or alive("eval_faces"):
        return False
    age = log_age()
    return age is None or age > 600


def stage():
    if alive("train_network"):
        return "训练"
    if alive("gen_detail") or alive("gen_face"):
        return "出图"
    if alive("eval_faces"):
        return "评分"
    if idle_now():
        return "空闲"
    return "完成" if last_log() else "待机"


EPOCH_FILE = f"{TRAIN}/.epoch_cur"
LASTSTEP_FILE = f"{TRAIN}/.last_step"


def absolute_mode(step, total):
    """日志里的 total 等于脚本的 max_train_steps，说明它报的是全局步数"""
    ms = max_steps()
    return bool(ms and total and abs(total - ms) <= 1)


def epochs_of(step, per):
    return (step // per + 1) if (step and per) else 0


def full_log_text(path):
    try:
        return open(path, "rb").read().decode("utf-8", "ignore").replace("\r", "\n")
    except Exception:
        return ""


def grad_accum():
    m = re.search(r"--gradient_accumulation_steps (\d+)", read(script_for_run()))
    return max(1, int(m.group(1))) if m else 1


def total_epochs():
    per, ms = imgs_per_epoch(), max_steps()
    if not (per and ms):
        return 0
    return -(-(ms * grad_accum()) // per)


def _epoch_scan():
    """从日志里数轮次：step 大幅回退就是新一轮。返回 [(本轮最大 step, 本轮最后一条的秒数)]"""
    eps, cur_max, cur_sec, prev = [], 0, 0, None
    for line in full_log_text(last_log() or "").split("\n"):
        m = re.search(r"(\d+)/(\d+)\s*\[\s*(\d+):(\d+)<", line)
        if not m:
            continue
        step, total = int(m.group(1)), int(m.group(2))
        sec = int(m.group(3)) * 60 + int(m.group(4))
        if prev is not None and step + 50 < prev and prev >= 0.9 * max(total, 1):
            eps.append((cur_max, cur_sec))   # 跑满一轮再回退 = 新一轮；中途重启不算
            cur_max, cur_sec = 0, 0
        prev = step
        cur_max = max(cur_max, step)
        cur_sec = max(cur_sec, sec)
    eps.append((cur_max, cur_sec))
    return eps


def epoch_display(step, total, n_img):
    if not step:
        return 0
    tot = total_epochs()
    seen = len(_epoch_scan())
    return min(seen, tot) if tot else seen


def epoch_durations():
    per = imgs_per_epoch() or 0
    out = []
    for mx, sec in _epoch_scan():
        if sec > 0 and (not per or mx >= 0.9 * per):
            out.append(sec)
    return out


def batches_done(step=None):
    """已完成批次：有一轮容量就按总容量封顶，否则退回日志里看到的量"""
    per = imgs_per_epoch() or 0
    cap = (max_steps() or 0) * grad_accum()
    seen = sum(mx for mx, _ in _epoch_scan())
    if step:
        seen = max(seen, step)
    return min(seen, cap) if cap else seen


def max_steps():
    """从最新的训练脚本里读 --max_train_steps；读不到就返回 0"""
    fs = sorted(glob.glob(f"{TRAIN}/train_*.sh"), key=os.path.getmtime, reverse=True)
    for f in fs:
        m = re.search(r"--max_train_steps\s+(\d+)", read(f))
        if m:
            return int(m.group(1))
    return 0


def epoch_cur(step, per):
    """sd-scripts 的 step 每轮重置：步数回退就说明进了下一轮，记到文件里"""
    if not (step and per):
        return 1
    try:
        last = int(read(LASTSTEP_FILE, "0").strip() or 0)
        cur = int(read(EPOCH_FILE, "1").strip() or 1)
    except Exception:
        last, cur = 0, 1
    if last and step < last - max(50, per // 10):
        cur += 1
        open(EPOCH_FILE, "w").write(str(cur))
    if step != last:
        open(LASTSTEP_FILE, "w").write(str(step))
    return cur


def imgs_per_epoch():
    return int(read(f"{TRAIN}/.status_imgs", "0").strip() or 0) or 0


def loss_series(n):
    vals = [float(v) for v in re.findall(r"avr_loss=([0-9.]+)", log_text(last_log()))]
    if len(vals) > n:
        step = len(vals) / n
        return [vals[int(i * step)] for i in range(n)]
    return vals


def latest_ckpt():
    fs = glob.glob(f"{TRAIN}/out_*/*.safetensors")
    if not fs:
        return "", 0
    f = max(fs, key=os.path.getmtime)
    return os.path.basename(f)[:-12], os.path.getsize(f) / 1048576


def last_report():
    fs = glob.glob(f"{TRAIN}/report_*.md")
    if not fs:
        return None
    t = read(max(fs, key=os.path.getmtime))
    sims = [float(x) for x in re.findall(r"相似 ([0-9.]+)", t)]
    tex = [float(x) for x in re.findall(r"纹理 ([0-9.]+)", t)]
    return (len(sims), sum(sims) / len(sims), sum(tex) / len(tex) if tex else 0) if sims else None


# ── 各页 ───────────────────────────────────────────────
ORG = "\033[38;2;250;179;135m"      # 暖橙：偏高
BLU = "\033[38;2;137;180;250m"      # 蓝：次要数值


def heat(v, warn, danger):
    """按阈值给颜色：正常绿、偏高黄、过高红"""
    return HOT if v >= danger else (YEL if v >= warn else GRN)


def line_cpu():
    c, t = cache("cpu", 2, lambda: (cpu_pct(), cpu_temp()))
    s = (f"{CYA}CPU{R} {heat(c, 60, 85)}{c:>3}%{R} "
         f"{DIM}负载{R}{BLU}{load1():.1f}{R}")
    if t:
        s += f" {heat(t, 70, 85)}{t:.0f}℃{R}"
    return s


def line_gpu():
    g = cache("gpu", 2, gpu)
    if not g:
        return f"{DIM}GPU{R} {DIM}无数据{R}"
    u, used, tot, t, pw, lim = g
    s = (f"{VIO}GPU{R} {heat(u, 80, 97)}{u:>3}%{R} "
         f"{BLU}{used:.1f}{DIM}/{tot:.1f}G{R}")
    s += f" {heat(t, 72, 82)}{t}℃{R}"
    return s + (f" {ORG}{pw:.0f}{DIM}/{lim:.0f}W{R}" if lim else f" {ORG}{pw:.0f}W{R}")


def log_age():
    """日志最后一次写入距今多少秒——判断训练是不是卡住了"""
    p = last_log()
    if not p:
        return None
    try:
        return max(0, int(time.time() - os.path.getmtime(p)))
    except Exception:
        return None


def disk(path="/home/link"):
    try:
        st = os.statvfs(path)
        free = st.f_bavail * st.f_frsize / 2**30
        tot = st.f_blocks * st.f_frsize / 2**30
        return free, tot
    except Exception:
        return None, None


def page_env(state, st="待机"):
    used, total, swap = mem()
    pct = used / total * 100
    out = [line_cpu(), line_gpu(),
           f"{YEL}内存{R} {heat(pct, 70, 88)}{used:.1f}{DIM}/{total:.1f}G{R} "
           f"{DIM}交换{R}{heat(swap, 2, 6)}{swap:.1f}G{R}"]
    if state.get("proc"):
        out += [f"{BLU}{c[:12]}{R} {ORG}{r:.1f}G{R}" for r, c in top_procs()]
    else:
        out.append(f"{GRN}出图{R} {GRN}在线{R}" if cache("comfy", 3, comfy_online)
                   else f"{DIM}出图 · 离线{R}")

    age = log_age()
    p = last_log()
    name = os.path.basename(p) if p else "—"
    if age is None:
        out.append(f"{VIO}日志{R} {BLU}{name}{R}")
    else:
        col = GRN if age < 20 else (YEL if age < 90 else HOT)
        out.append(f"{VIO}日志{R} {BLU}{name}{R} {col}{age}s前{R}")

    lw = latest_weight()
    if lw and st == "完成":
        name, mt, mb = lw
        out.append(f"{YEL}上次{R} {BLU}{name[:max(10, 30)]}{R} {DIM}{mb:.0f}MB · {_dur(time.time() - mt)}前{R}")

    free, tot = disk()
    if free is not None:
        out.append(f"{CYA}磁盘{R} {heat(100 * free / tot, 60, 85)}{free:.0f}{DIM}/{tot:.0f}G{R}")
    return out


def _norm_dur(v):
    """把历史里 "8m"/"1h02m" 这类没有秒的写法转成带秒"""
    if not isinstance(v, str):
        return v
    m = re.fullmatch(r"(?:(\d+)h)?(\d+)m", v.strip())
    if m:
        h = int(m.group(1) or 0)
        return _dur(h * 3600 + int(m.group(2)) * 60)
    return v


def _stage_times():
    try:
        d = json.load(open(f"{TRAIN}/stage_times.json"))
        return {k: _norm_dur(v) for k, v in d.items()}
    except Exception:
        return {}


def current_run():
    """从正在跑的进程里取输出名和脚本，用来判断"当前这一版"是谁"""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            args = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "ignore").split("\0")
        except Exception:
            continue
        if not any(a.endswith("train_network.py") for a in args):
            continue
        name = ""
        for i, a in enumerate(args):
            if a == "--output_name" and i + 1 < len(args):
                name = args[i + 1]
        return name or ""
    return ""


def _shot_count():
    """当前在役权重这一版，出了几张样图"""
    lm = in_use_lora()
    if not lm:
        return 0
    stem = os.path.basename(lm[0]).rsplit(".", 1)[0]
    try:
        return len([f for f in os.listdir("/home/link/ComfyUI/output")
                    if f.startswith(f"eval_{stem}_") and f.endswith(".png")])
    except Exception:
        return 0


def _sample_count():
    try:
        return len([f for f in os.listdir("/home/link/ComfyUI/output")
                    if f.startswith("eval_") and f.endswith(".png")]) or 4
    except Exception:
        return 4


def _train_sub(ep, tot):
    durs = epoch_durations()
    if not ep:
        return ""
    s = f"第{ep}{('/' + str(tot)) if tot else ''}轮"
    if durs:
        s += f" · 上轮{_dur(durs[-1])}"
        if len(durs) > 1:
            s += f" · 均{_dur(sum(durs) // len(durs))}"
    return s


def page_flow(step, total, n_img, st, rate):
    times = _stage_times()
    src = cache("src", 60, lambda: max(0, sum(1 for _ in open(f"{TRAIN}/report_v3.csv")) - 1)
                if os.path.exists(f"{TRAIN}/report_v3.csv") else 0)
    cnt = lambda pat, ttl=60: cache("n:" + pat, ttl, lambda: len(glob.glob(pat)))
    cut = cnt(f"{TRAIN}/dataset_v3_base/10_machi/*.jpg")
    tag = cnt(f"{TRAIN}/captions_wd14/*.txt")
    ep = epoch_display(step, total, n_img)
    tot = total_epochs()

    lw = latest_weight()
    lw_name = os.path.basename(lw[0]).rsplit(".", 1)[0] if lw else ""
    if st == "训练" and step and rate:
        train_dur = _dur(step / rate)
    elif st in ("空闲", "待机", "出图", "评分", "完成"):
        train_dur = f"已结束 · {lw_name}" if lw else "—"
    else:
        train_dur = times.get("训练", "—")

    rows = [
        ("扫描", f"{src} 张原图" if src else "", times.get("扫描", "—")),
        ("切图", f"{cut} 张裁片" if cut else "", times.get("切图", "—")),
        ("打标", f"{tag} 条描述" if tag else "", times.get("打标", "—")),
        ("训练", "" if st in ("空闲", "待机") else _train_sub(ep, tot),
         train_dur if st != "完成" else (f"已结束 · {lw_name}" if lw_name else "已结束")),
        ("出图", cache("nsample2", 5, lambda: f"本次 {_shot_count()} 张"), times.get("出图", "—")),
        ("评分", "相似度+纹理", times.get("评分", "—")),
    ]
    names = [r[0] for r in rows]
    if st in names:
        now = names.index(st)
    elif st in ("空闲", "待机"):
        now = -1                      # 没有正在跑的步骤：谁都不打勾
    else:
        now = len(rows)
    out = []
    for i, (name, sub, dur) in enumerate(rows):
        if i < now:
            mark, ncol = GRN + "✓" + R, CYA
        elif i == now:
            mark, ncol = YEL + "▶" + R, YEL
        else:
            mark, ncol = DIM + "·" + R, DIM
        body = f"{mark} {ncol}{name}{R}"
        if sub:
            body += f" {BLU}{sub}{R}"
        dcol = GRN if dur not in ("—", "", None) else DIM
        width = 26 if _NARROW[0] else 34
        pad = " " * max(1, width - wlen(body))
        out.append(body + pad + f"{dcol}{dur}{R}")
    return out


def _dur(sec):
    """时长统一到秒：45s / 8m12s / 1h02m03s"""
    sec = max(0, int(sec))
    if sec >= 3600:
        return f"{sec // 3600}h{sec % 3600 // 60:02d}m{sec % 60:02d}s"
    if sec >= 60:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec}s"


_SPARK = "▁▂▃▄▅▆▇█"
class _SparkColor:
    """按相对高度取色：低绿、中青、偏高黄、最高红"""
    def __getitem__(self, f):
        return GRN if f < 0.35 else (CYA if f < 0.6 else (YEL if f < 0.85 else HOT))


_SPARK_COLOR = _SparkColor()


def script_for_run():
    """挑当前这版对应的训练脚本；找不到就退回最新的"""
    name = current_run()
    fs = sorted(glob.glob(f"{TRAIN}/train_*.sh"), key=os.path.getmtime, reverse=True)
    if name:
        for f in fs:
            if f"output_name {name}" in read(f):
                return f
    return fs[0] if fs else ""


def page_quality(W):
    out = []
    vals = loss_series(min(max(8, W - 12), 32))
    if vals:
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1e-9
        spark = "".join(f"{_SPARK_COLOR[(v - lo) / span]}{_SPARK[min(7, int((v - lo) / span * 7))]}{R}"
                        for v in vals)
        out.append(f"{CYA}loss{R} {spark}")
        k = max(1, len(vals) // 10)
        head, tail = sum(vals[:k]) / k, sum(vals[-k:]) / k
        trend = f"{GRN}↓{R}" if tail < head - 1e-4 else (f"{HOT}↑{R}" if tail > head + 1e-4 else f"{DIM}→{R}")
        out.append(f"{DIM}当前{R} {WH}{vals[-1]:.4f}{R} {trend} "
                   f"{DIM}最低{R}{GRN}{lo:.4f}{R} {DIM}最高{R}{BLU}{hi:.4f}{R}")
    else:
        out.append(f"{CYA}loss{R} {DIM}本轮日志没输出 avr_loss{R}")

    sh = read(script_for_run())
    if sh:
        lr = re.search(r"--learning_rate (\S+)", sh)
        res = re.search(r"--resolution (\d+)", sh)
        dim = re.search(r"--network_dim (\d+)", sh)
        bs = re.search(r"--train_batch_size (\d+)", sh)
        ms = re.search(r"--max_train_steps (\d+)", sh)
        if lr:
            sch = re.search(r"--lr_scheduler (\S+)", sh)
            out.append(f"{VIO}学习率{R} {WH}{lr.group(1)}{R} {DIM}{sch.group(1) if sch else ''}{R}")
        if res:
            full = (f"{CYA}本版{R} {BLU}{os.path.basename(script_for_run())}{R} "
                    f"{DIM}{res.group(1)}px dim{R}{WH}{dim.group(1) if dim else '-'}{R} "
                    f"{DIM}b{R}{WH}{bs.group(1) if bs else '-'}{R} "
                    f"{DIM}目标{R}{WH}{ms.group(1) if ms else '-'}{R}")
            if wlen(full) > W - 1:
                full = (f"{CYA}本版{R} {DIM}{res.group(1)}px dim{R}{WH}{dim.group(1) if dim else '-'}{R} "
                        f"{DIM}b{R}{WH}{bs.group(1) if bs else '-'}{R} "
                        f"{DIM}目标{R}{WH}{ms.group(1) if ms else '-'}{R}")
            out.append(full)

    name = current_run()
    cks = [f for f in glob.glob(f"{TRAIN}/out_*/*-step*.safetensors")
           if not name or os.path.basename(f).startswith(name)]
    if cks:
        f = max(cks, key=os.path.getmtime)
        step_n = re.search(r"-step(\d+)", os.path.basename(f))
        n = int(step_n.group(1)) if step_n else 0
        out.append(f"{ORG}存档{R}{BLU}{n}{R}{DIM}步{R} {DIM}{os.path.getsize(f) / 1048576:.0f}MB{R}")
    else:
        out.append(f"{ORG}存档{R} {DIM}还没有{R}")

    durs = epoch_durations()
    tot_ep = total_epochs()
    if durs:
        detail = " ".join(f"{i + 1}:{_dur(d)}" for i, d in enumerate(durs))
        out.append(f"{CYA}各轮{R} {BLU}{detail}{R}"
                   + (f" {DIM}共{R}{WH}{tot_ep}{R}{DIM}轮{R}" if tot_ep else ""))

    rep = last_report()
    if rep:
        sc = GRN if rep[1] >= 0.66 else (YEL if rep[1] >= 0.60 else HOT)
        tc = GRN if rep[2] >= 8 else (YEL if rep[2] >= 6.5 else HOT)
        out.append(f"{YEL}评分{R} {DIM}相似{R}{sc}{rep[1]:.3f}{R} "
                   f"{DIM}纹理{R}{tc}{rep[2]:.2f}{R} {DIM}{rep[0]}张{R}")
    else:
        out.append(f"{YEL}评分{R} {DIM}还没跑{R}")
    return out


def _src_stats():
    """素材来源：工坊条目的数量（只数目录，不遍历文件；体积由后台脚本记在文件里）"""
    base = "/mnt/winbot-software/Program Files (x86)/Steam/steamapps/workshop/content/431960"
    try:
        n = len([d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))])
    except Exception:
        n = 0
    gb = 0.0
    try:
        gb = float(open(f"{TRAIN}/.src_size_gb").read().strip())
    except Exception:
        pass
    return n, gb


def _unique_size(files):
    """硬链接会重复计算，按 inode 去重"""
    seen, total = set(), 0
    for f in files:
        try:
            st = os.stat(f)
        except Exception:
            continue
        if st.st_ino in seen:
            continue
        seen.add(st.st_ino)
        total += st.st_size
    return total / 2**30


def page_data():
    src, src_gb = cache("srcs", 120, lambda: _src_stats())
    fs = glob.glob(f"{TRAIN}/dataset_v3_base/10_machi/*.jpg")
    kinds = collections.Counter(os.path.basename(f)[0] for f in fs)
    size = _unique_size(fs)
    tagged = sum(1 for f in fs if os.path.exists(f[:-4] + ".txt"))
    cover = (100 * tagged // len(fs)) if fs else 0
    newest = max((os.path.getmtime(f) for f in fs), default=0)
    age = _dur(time.time() - newest) if newest else "—"
    return [
        f"{CYA}素材{R} {WH}{src} 套{R} {DIM}·{R} {BLU}{src_gb:.0f}G{R}",
        f"{CYA}裁片{R} {WH}{len(fs)}{R} {DIM}张 · {size:.1f}G（去重）{R}",
        f"{GRN}脸{R}{WH}{kinds.get('f', 0)}{R}  {BLU}上身{R}{WH}{kinds.get('b', 0)}{R}  "
        f"{VIO}半身{R}{WH}{kinds.get('u', 0)}{R}  {ORG}整图{R}{WH}{kinds.get('n', 0)}{R}",
        f"{YEL}标注{R} {WH}{tagged}{R}{DIM}/{len(fs)} 张（{cover}%）{R}",
        f"{CYA}最近改动{R} {BLU}{time.strftime('%m-%d %H:%M', time.localtime(newest)) if newest else '—'}{R}"
        f" {DIM}({age}前){R}",
        f"{DIM}在役{R} {BLU}{(pretty_model(in_use_lora()[0]) if in_use_lora() else '—')[:26]}{R}"
        f" {DIM}基线{R}{YEL}0.676{R}{DIM}/{YEL}7.33{R}",
    ]


def page_log(W, step, total, losses):
    path = last_log() or ""
    raw = log_text(path)
    errs = warn = 0
    want = ("INFO", "WARNING", "ERROR", "Error", "Traceback", "saving", "checkpoint", "OOM", "Killed")
    lines = []
    for chunk in raw.split("\n"):
        t = re.sub(r"\s{2,}", " ", chunk).strip()
        if len(t) < 6 or t.startswith("steps:") or re.search(r"\d+/\d+ \[\d+:", t):
            continue
        if re.search(r"it/s\]|s/it\]", t) or not re.search(r"[A-Za-z]{3,}", t):
            continue                      # 进度条被 \r 拆出来的碎片，别当成日志行
        if "ERROR" in t or "Traceback" in t or "Killed" in t:
            errs += 1
        elif "WARNING" in t:
            warn += 1
        if not any(k in t for k in want):
            continue
        t = re.sub(r"\s+[a-z_]+\.py:\d+$", "", t)
        t = re.sub(r"^saving checkpoint: .*/([^/]+)$", r"saving: \1", t)
        if lines and lines[-1] == t:
            continue
        lines.append(t)

    age = log_age()
    head = f"{VIO}日志{R} {BLU}{os.path.basename(path) or '—'}{R}"
    if age is not None:
        col = GRN if age < 20 else (YEL if age < 90 else HOT)
        head += f" {col}{age}s前{R}"

    bad = f"{HOT}错{errs}{R}" if errs else f"{DIM}错 0{R}"
    mid = f"{DIM}进度{R}{WH}{step or 0}{DIM}/{total or 0}{R} {bad} {YEL if warn else DIM}警 {warn}{R}"
    if losses:
        mid += f" {DIM}loss{R}{WH}{losses[-1]:.4f}{R}"
    if idle_now():
        mid = f"{DIM}空闲 · 这条是历史日志{R}"
    out = [head, mid]

    if not lines:                                   # 没有关键行就退一步：给最近一条非进度行
        for chunk in reversed(raw.split("\n")):
            t = re.sub(r"\s{2,}", " ", chunk).strip()
            if len(t) > 8 and not re.search(r"\d+/\d+ \[", t):
                lines.append(t)
                break
    rooms = max(2, (H_ROWS[0] or 12) - 6) if H_ROWS[0] else 4
    for t in lines[-rooms:]:
        col = HOT if ("ERROR" in t or "Killed" in t) else (
            YEL if "WARNING" in t else (GRN if t.startswith("saving") else TXT))
        out.append(f"{col}{t[:max(8, W - 1)]}{R}")
    if len(out) == 2 and not lines:
        out.append(f"{DIM}这一版日志里没有关键行{R}")
    return out


BAD_FILE = f"{TRAIN}/bad_images.txt"


def _bad_set():
    try:
        return {l.strip() for l in open(BAD_FILE, encoding="utf-8") if l.strip()}
    except Exception:
        return set()


def image_list():
    """必须和 photo-view.sh / imv 用同一份清单，否则索引错位；
    坏图（解码失败，见 bad_images.txt）剔除掉，别让翻页停在白屏上"""
    bad = cache("badset", 30, _bad_set)

    def _drop(lst):
        return [f for f in lst if f not in bad] or lst

    def _sorted(lst):
        """按「工坊套 + 文件名」排，同一套的放一起，顺序稳定不随机"""
        def key(f):
            parts = f.split("/")
            setid = next((x for x in parts if x.isdigit() and len(x) >= 8), "")
            return (setid, os.path.basename(f))
        return sorted(lst, key=key)

    def _load():
        try:
            with open("/tmp/ts_imgs.txt", encoding="utf-8") as f:
                lst = [l.strip() for l in f if l.strip()]
            if lst:
                return _sorted(_drop(lst))
        except Exception:
            pass
        if os.path.exists(f"{TRAIN}/report_v3.csv"):
            return _sorted(_drop([r["file"] for r in csv.DictReader(open(f"{TRAIN}/report_v3.csv"))
                                  if os.path.exists(r["file"])]))
        return _sorted(_drop(glob.glob(f"{TRAIN}/dataset_v3_base/10_machi/*.jpg")))
    return cache("imgs", 60, _load)


def save_pos(i):
    try:
        open(IMG_POS, "w").write(str(i))
    except Exception:
        pass


def load_pos():
    try:
        return int(read(IMG_POS).strip())
    except Exception:
        return 0


def preview_image(idx=0):
    imgs = image_list()
    return imgs[idx % len(imgs)] if imgs else ""


def imv_pid():
    return read(IMV_PID).strip()


def viewer_alive():
    pid = imv_pid()
    if not pid:
        return False
    try:
        return open(f"/proc/{pid}/stat").read().split()[2] != "Z"
    except Exception:
        return False


def imv_current():
    """从 imv 窗口标题取『第几张/共几张』和路径；标题形如
    imv - [3/1414] [8742x11656] [12%] /path/a.jpg [scale to fit]"""
    try:
        dump = subprocess.check_output(["niri", "msg", "windows"], text=True, timeout=2)
    except Exception:
        return None
    want = imv_pid()
    title = ""
    pid = None
    for line in dump.splitlines():
        t = line.strip()
        if t.startswith("PID:"):
            try:
                pid = t.split(":", 1)[1].strip()
            except Exception:
                pid = None
        elif t.startswith("Title:") and "imv - [" in t:
            if not want or pid == want or not title:
                title = t.split("Title:", 1)[1].strip().strip('"')
    m = re.match(r"imv - \[(\d+)/(\d+)\]", title)
    if not m:
        return None
    idx = int(m.group(1)) - 1
    pm = re.match(r"imv - \[\d+/\d+\] \[[^\]]*\] \[[^\]]*\] (.+?) \[", title)
    return idx, (pm.group(1).strip() if pm else "")


def sync_viewer(idx):
    """让开着的 imv 跳到同一张"""
    if not viewer_alive():
        return
    try:
        subprocess.run(["imv-msg", imv_pid(), "goto", str(idx + 1)],
                       timeout=1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def open_viewer(idx=0):
    if viewer_alive():
        return
    path = preview_image(idx)
    if not path:
        return
    subprocess.run(["niri", "msg", "action", "spawn", "--",
                    f"{HOME}/.dsh/miyu/scripts/photo-view.sh", path],
                   timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


THUMB_W, THUMB_H = 1280, 800          # 原图缓存尺寸（按比例，供不拉伸时用）
CELL_W, CELL_H = 16, 36               # 一个字符格大约多少像素（kitty 默认识别）


def src_thumb(path):
    """原图 → 1280×800 的源缓存（只做一次，之后所有尺寸都从它派生）"""
    try:
        st = os.stat(path)
        key = f"src|{path}|{st.st_mtime_ns}"
    except Exception:
        key = f"src|{path}"
    return os.path.join(THUMB_DIR, hashlib.sha1(key.encode()).hexdigest() + ".png")


def thumb(path, cols=0, rows=0, px=None):
    """缓存键含格数：拉伸版按窗口尺寸单独存，换尺寸才重做"""
    try:
        st = os.stat(path)
        key = f"thumb|{path}|{st.st_mtime_ns}|{cols}x{rows}|{px}"
    except Exception:
        key = f"thumb|{path}|{cols}x{rows}|{px}"
    return os.path.join(THUMB_DIR, hashlib.sha1(key.encode()).hexdigest() + ".png")


def _legacy_thumb(path, cols, rows):
    try:
        st = os.stat(path)
        key = f"{path}|{st.st_mtime_ns}|{cols}x{rows}"
    except Exception:
        key = f"{path}|{cols}x{rows}"
    return os.path.join(THUMB_DIR, hashlib.sha1(key.encode()).hexdigest() + ".png")


def kitty_img(path, cols, rows, row=1, px=None, imgid=1):
    """把图拉伸到窗口像素尺寸（px）铺满，或退回格数估算"""
    if not path:
        return ""
    try:
        from PIL import Image, ImageFile
        import io, base64
        Image.MAX_IMAGE_PIXELS = None
        ImageFile.LOAD_TRUNCATED_IMAGES = True   # 截断的 JPEG 也照样解，imv 能看它就该能看          # 原图有上亿像素，别被"解压炸弹"拦下
        try:
            scale = float(os.environ.get("TRAIN_STATUS_IMG_SCALE", "1.0"))
        except Exception:
            scale = 1.0
        scale = min(1.0, max(0.25, scale))
        BASE_W, BASE_H = 1744, 1441          # 面板 1163x961 逻辑 × 1.5 缩放
        if px:
            tw = max(16, int(px[0] * scale)); th = max(16, int(px[1] * scale))
        else:
            tw, th = max(16, cols * CELL_W), max(16, rows * CELL_H)

        tp = thumb(path, cols, rows, (BASE_W, BASE_H))
        png = open(tp, "rb").read() if os.path.exists(tp) else None
        if png is None:
            src = src_thumb(path)
            if not os.path.exists(src):        # 源缓存：一天只需要为每张图做一次
                os.makedirs(THUMB_DIR, exist_ok=True)
                im = Image.open(path)
                im.draft("RGB", (2560, 1600))
                im = im.convert("RGB")
                im.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                open(src + ".tmp", "wb").write(buf.getvalue())
                os.replace(src + ".tmp", src)
            im = Image.open(src).convert("RGB").resize((BASE_W, BASE_H), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            png = buf.getvalue()
            open(tp + ".tmp", "wb").write(png)
            os.replace(tp + ".tmp", tp)

        data = base64.standard_b64encode(png).decode("ascii")
        parts = [data[i:i + 3800] for i in range(0, len(data), 3800)]
        n = len(parts)
        out = [f"\033[{row};1H"]
        for i, ch in enumerate(parts):
            more = 0 if i == n - 1 else 1
            if i == 0:      # 只有第一块带完整参数，后续块只带 m 和负载
                # i=1：同一 id 反复传，kitty 原地替换而不是先删后画，切换不闪
                head = f"a=T,f=100,i={imgid},c={cols},r={rows},C=1,q=2,m={more}"
            else:
                head = f"m={more}"
            out.append(f"\033_G{head};{ch}\033\\")
        return "".join(out)
    except Exception as e:
        try:
            open("/tmp/ts_img.err", "a").write(f"{type(e).__name__}: {e}\n")
        except Exception:
            pass
        return ""


# ── 窗口几何 ───────────────────────────────────────────
def niri_json(args):
    try:
        return json.loads(subprocess.check_output(["niri", "msg", "--json"] + args, text=True, timeout=2))
    except Exception:
        return None


def panel_window():
    for w in (niri_json(["windows"]) or []):
        if w.get("app_id") == "trainstatus":
            return w
    return None


def panel_geom():
    w = panel_window()
    if not w:
        return None
    l = w.get("layout") or {}
    pos, size = l.get("tile_pos_in_workspace_view"), l.get("window_size")
    if not pos:
        return None
    return int(pos[0]), int(pos[1]), int(size[0]), int(size[1])


def output_scale():
    for v in (niri_json(["outputs"]) or {}).values():
        lg = v.get("logical") or {}
        try:
            return float(lg.get("scale") or 1.0)
        except Exception:
            pass
    return 1.0


def screen_size():
    for v in (niri_json(["outputs"]) or {}).values():
        lg = v.get("logical")
        if lg:
            return int(lg["width"]), int(lg["height"])
    return 1706, 1066


def sizes():
    sw, sh = screen_size()
    if _C.get("sizes_k") == (sw, sh):
        return _C["sizes_v"]
    out = []
    for frac in (0.236, 0.38, 0.536, 0.693):
        w = max(320, int(sw * frac))
        h = int(w / 1.194)
        if h > sh * 0.93:
            h, w = int(sh * 0.93), int(sh * 0.93 * 1.194)
        out.append((w, h))
    _C.update(sizes_k=(sw, sh), sizes_v=out)
    return out


def size_idx():
    try:
        return max(0, min(len(sizes()) - 1, int(read(SIZE_FILE).strip())))
    except Exception:
        return 1


def cycle_size(step):
    sz = sizes()
    i = (size_idx() + step) % len(sz)
    w, h = sz[i]
    win = panel_window()
    if not win:
        return i
    wid, g = str(win["id"]), panel_geom()
    for act, val in (("set-window-width", w), ("set-window-height", h)):
        subprocess.run(["niri", "msg", "action", act, "--id", wid, str(val)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if g:
        dx, dy = -(w - g[2]) // 2, -(h - g[3]) // 2
        if dx or dy:
            subprocess.run(["niri", "msg", "action", "move-floating-window", "--id", wid,
                            "-x", f"{dx:+d}", "-y", f"{dy:+d}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        open(SIZE_FILE, "w").write(str(i))
    except Exception:
        pass
    return i


# ── 排版 ───────────────────────────────────────────────
def header(st, step, total, n_img, log_step=None, W=80):
    lora = in_use_lora()
    name = read(f"{TRAIN}/.project_name", "马吉 v3").strip() or "马吉 v3"
    short = name
    if lora:
        full = pretty_model(lora[0])                       # 马吉 sdxl·id·v2
        short = full.split(" ")[0]                         # 马吉
        parts_full = full
    else:
        parts_full = name
    ep = 0 if st in ("空闲", "待机") else epoch_display(step, total, n_img)
    tot = total_epochs()
    color = {"训练": CYA, "出图": YEL, "评分": VIO, "完成": GRN, "空闲": DIM}.get(st, DIM)
    ep_txt = f"{WH}第{ep}{('/' + str(tot)) if tot else ''}轮{R}" if ep else ""
    view_txt = f" {DIM}· {VIEWS[_CUR_VIEW[0]]}{R}"

    # 由长到短逐级尝试：全名 + 秒 + 页名 → 全名 → 只留项目名 → 去掉页名
    candidates = [
        f"{GRN}●{R}{WH}{parts_full}{R} {DIM}{time.strftime('%H:%M:%S')}{R} {color}{st}{R}"
        f"{(' ' + ep_txt) if ep_txt else ''}{view_txt}",
        f"{GRN}●{R}{WH}{parts_full}{R} {DIM}{time.strftime('%H:%M:%S')}{R} {color}{st}{R}"
        f"{(' ' + ep_txt) if ep_txt else ''}",
        f"{GRN}●{R}{WH}{short}{R} {DIM}{time.strftime('%H:%M')}{R} {color}{st}{R}"
        f"{(' ' + ep_txt) if ep_txt else ''}",
        f"{GRN}●{R}{WH}{short}{R} {color}{st}{R}",
    ]
    for line in candidates:
        if wlen(line) <= W:
            return line
    return candidates[-1]


def bottom(step, total, rate, W, cum=0):
    if not (step and total and rate):
        return [DIM + "━" * max(1, W // 2) + "─" * max(1, W - W // 2) + R, f"{DIM}等待数据{R}"]
    pct = step * 100 // total
    filled = max(1, min(W, int(W * step / total)))
    # 进度条本身就是渐变色：起步青、中段蓝、后段紫，走完变绿
    bar_col = GRN if pct >= 100 else (VIO if pct >= 75 else (BLU if pct >= 40 else CYA))
    bar = f"{bar_col}{'━' * filled}{R}{DIM}{'─' * max(0, W - filled)}{R}"
    left = max(0, int((total - step) / rate))
    h, m, sec = left // 3600, left % 3600 // 60, left % 60
    remain = f"{h}h{m:02d}m{sec:02d}s" if h else f"{m}m{sec:02d}s"
    eta = time.strftime("%H:%M:%S", time.localtime(time.time() + left))
    pc = GRN if pct >= 100 else (VIO if pct >= 75 else (BLU if pct >= 40 else CYA))
    left_col = HOT if left > 1800 else (YEL if left > 600 else GRN)
    speed = f" {DIM}{rate:.2f}it/s{R}" if rate else ""
    cumtxt = f" {DIM}累计{R}{BLU}{cum}{R}" if cum else ""
    for txt in (f"{WH}{step}{DIM}/{total}{R} {pc}{pct}%{R} {DIM}剩{R}{left_col}{remain}{R} "
                f"{DIM}至{R}{BLU}{eta}{R}{speed}{cumtxt}",
                f"{WH}{step}{DIM}/{total}{R} {pc}{pct}%{R} {DIM}剩{R}{left_col}{remain}{R} {DIM}至{R}{BLU}{eta}{R}",
                f"{WH}{step}{DIM}/{total}{R} {pc}{pct}%{R} {DIM}剩{R}{left_col}{remain}{R}",
                f"{WH}{step}{DIM}/{total}{R} {pc}{pct}%{R}"):
        if wlen(txt) <= W:
            break
    return [bar, txt]


def compact(W, state, H):
    _NARROW[0] = (W < 44 or H < 16)
    H_ROWS[0] = H
    step, total, rate, losses, log_step, src = progress()
    st = cache("stage", 2.0, stage)
    n_img = imgs_per_epoch()
    view = state["view"]
    _CUR_VIEW[0] = view
    rows = [(1, header(st, step, total, n_img, log_step, W))]
    body = []
    if view == 1:
        body = page_flow(step, total, n_img, st, rate)
    elif view == 2:
        body = page_quality(W)
    elif view == 3:
        body = page_data()
    elif view == 4:
        body = page_log(W, step, total, losses)
    elif view == 0:
        body = page_env(state, st)
    _CUR_VIEW[0] = view
    r = 2
    stop = max(2, H - 2)
    gap = 1 if (H < 20 or _NARROW[0]) else 2
    for line in body:
        if r > stop:
            break
        rows.append((r, line))
        r += gap
    # 底部给整体进度：累计批次 vs 总批次（= 优化步 × 梯度累积）；拿不到就退回日志自己的数字
    if st in ("空闲", "待机"):
        lw = latest_weight()
        nm = os.path.basename(lw[0]).rsplit(".", 1)[0] if lw else ""
        cands = [f"{DIM}空闲 · 无任务在跑{R} {DIM}· 上次产出{R} {BLU}{nm}{R}",
                 f"{DIM}空闲{R} {DIM}· 上次{R} {BLU}{nm}{R}",
                 f"{DIM}空闲{R}"]
        tip = next((c for c in cands if wlen(c) <= W - 1), cands[-1])
        b = [DIM + "─" * W + R, tip]
    elif step and total:
        b = bottom(min(step, total), total, rate, W, batches_done(step))
    else:
        b = bottom(step, total, rate, W)
    rows += [(max(3, H - 2), b[0]), (max(3, H - 1), b[1]), (max(3, H), footer(state, W))]
    return rows, step, total


DEL_IMG = "\033_Ga=d,d=A\033\\"


def footer(state, W):
    """最后一行：页点 + 当前页快捷键提示"""
    view = state["view"]
    if W < 42:
        return "".join(f"{WH}●{R}" if i == view else f"{DIM}○{R}" for i in range(6))
    dots = "".join(f"{WH}●{R}" if i == view else f"{DIM}○{R}" for i in range(6))
    hints = {
        5: f"{BLU}w/s{R} 翻图 {BLU}滚轮{R} 翻图 {BLU}左键{R} 原图",
        4: f"{BLU}e/q{R} 翻页 {BLU}z/Z{R} 尺寸 {BLU}p{R} 暂停",
        0: f"{BLU}e/q{R} 翻页 {BLU}c{R} 占用 {BLU}z/Z{R} 尺寸",
    }.get(view, f"{BLU}e/q{R} 翻页 {BLU}z/Z{R} 尺寸 {BLU}p{R} 暂停")
    left = dots
    if wlen(left) + wlen(hints) + 2 > W:      # 放不下就换短版，不直接砍掉
        short = {5: f"{BLU}w/s{R}翻图 {BLU}左键{R}原图",
                 4: f"{BLU}e/q{R}页 {BLU}p{R}暂停",
                 0: f"{BLU}e/q{R}页 {BLU}c{R}占用"}.get(view, f"{BLU}e/q{R}页 {BLU}z/Z{R}尺寸")
        hints = short
    if wlen(left) + wlen(hints) + 2 > W:
        hints = f"{BLU}e/q{R}"
    pad = max(1, W - wlen(left) - wlen(hints))
    return left + " " * pad + hints


def draw(rows, W):
    out = [DEL_IMG, "\033[H\033[J"]
    for row, txt in rows:
        out.append(f"\033[{row};1H " + clip(fit(txt, W - 1), W - 1) + "\033[K")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def tsize():
    try:
        h, w = struct.unpack("hh", fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 4)[:4])
        return max(10, w), max(6, h)
    except Exception:
        return int(os.environ.get("COLUMNS", 80)), int(os.environ.get("LINES", 24))


def micro(W):
    step, total, rate, losses, _, _ = progress()
    if step and total and rate:
        txt = f"{step * 100 // total}% {int((total - step) / rate) // 60}m"
        if losses:
            txt += f" {losses[-1]:.3f}"
    else:
        txt = "wait"
    sys.stdout.write("\033[H\033[J" + txt[:W])
    sys.stdout.flush()


# ── 主循环 ─────────────────────────────────────────────
def main():
    me = os.path.abspath(__file__)
    MT_FILE = "/tmp/ts_src_mtime"
    mt = os.path.getmtime(me) if os.path.exists(me) else 0
    try:
        open(MT_FILE, "w").write(str(mt))
    except Exception:
        pass
    W, H = tsize()
    try:
        start_view = int(os.environ.get("TRAIN_STATUS_VIEW", "0") or 0)
    except Exception:
        start_view = 0
    state = {"view": max(0, min(5, start_view)), "paused": False, "proc": False,
             "img_idx": load_pos()}
    keys = os.environ.get("TRAIN_STATUS_KEYS", "all")     # all | noq
    mouse = os.environ.get("TRAIN_STATUS_MOUSE", "1") != "0"
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sys.stdout.write("\033[?1049h\033[?25l" + ("\033[?1000h\033[?1006h" if mouse else ""))
    last_reload = 0.0
    nxt = time.time()
    try:
        tty.setcbreak(fd)
        while True:
            try:
                if (os.path.exists(me) and os.path.getmtime(me) != mt
                        and time.time() - last_reload > 1.5):
                    last_reload = time.time()
                    raise SystemExit(42)          # 交给外层壳重新拉起，避免 execv 卡住终端状态
            except Exception:
                pass

            if time.time() >= nxt:
                W, H = tsize()
                if W < 24:
                    micro(W)
                elif state["view"] == 5:
                    cur = cache("imvcur", 0.8, imv_current)
                    if cur:
                        if state.get("img_idx") != cur[0]:
                            state["img_idx"] = cur[0]
                            save_pos(cur[0])
                        path = cur[1]
                    else:
                        path = preview_image(state["img_idx"])
                    key = f"{path}|{W}x{H}"
                    if state.get("drawn") != key:        # 同一张图不重画，否则整屏每轮闪一下
                        g = cache("geom", 2, panel_geom)
                        sc = cache("scale", 30, output_scale)
                        px = (int(g[2] * sc), int(g[3] * sc)) if g else None
                        fresh = state.get("drawn") is None      # 从文字页切进来才需要清屏
                        old_id = state.get("img_id", 1)
                        new_id = 2 if old_id == 1 else 1
                        seq = cache(f"full:{key}:{px}:{new_id}", 600,
                                    lambda: kitty_img(path, W, H, 1, px, new_id))
                        if seq:
                            prefix = (DEL_IMG + "\033[H\033[J") if fresh else ""
                            # 先画新 id，再删旧 id：任何一帧都有图，不会闪白
                            tail = "" if fresh else f"\033_Ga=d,d=i,i={old_id}\033\\"
                            sys.stdout.write(prefix + seq + tail + "\033[?25l")
                            state["img_id"] = new_id
                        else:                      # 图坏了别白屏，给一行说明
                            name = os.path.basename(path)[:max(8, W - 6)]
                            msg = [f"{HOT}这张读不出来{R}",
                                   f"{BLU}{name}{R}",
                                   f"{DIM}第 {state['img_idx'] + 1}/{len(image_list())} 张{R}",
                                   f"{DIM}w/s 或滚轮换下一张{R}"]
                            sys.stdout.write(DEL_IMG + "\033[H\033[J")
                            for i, t in enumerate(msg):
                                sys.stdout.write(f"\033[{i * 2 + 2};2H" + clip(t, W - 2))
                            sys.stdout.write("\033[?25l")
                        sys.stdout.flush()
                        # 预热下一张，翻页时不用等解码
                        try:
                            import threading
                            nxt_i = (state["img_idx"] + 1) % max(1, len(image_list()))
                            nf = image_list()[nxt_i]
                            for iid in (1, 2):
                                threading.Thread(target=kitty_img,
                                                 args=(nf, W, H, 1, px, iid),
                                                 daemon=True).start()
                        except Exception:
                            pass
                        state["drawn"] = key
                else:
                    rows, _, _ = compact(W, state, H)
                    draw(rows, W)
                    state["drawn"] = None
                try:
                    iv = float(os.environ.get("TRAIN_STATUS_INTERVAL", "") or 1.0)
                except Exception:
                    iv = 1.0
                nxt = time.time() + max(0.2, iv)     # 默认每秒刷一次

            wait = max(0.05, nxt - time.time())
            if keys == "0" or not select.select([fd], [], [], wait)[0]:
                continue
            burst = os.read(fd, 64).decode("utf-8", "ignore")
            dirty = False
            for mb, mx, my, act in re.findall(r"\033\[<(\d+);(\d+);(\d+)([Mm])", burst):
                if act != "M":
                    continue
                code = int(mb)
                if code >= 64 and state["view"] == 5:
                    step = -1 if code % 2 == 1 else 1
                    state["img_idx"] = (state["img_idx"] + step) % max(1, len(image_list()))
                    save_pos(state["img_idx"])
                    sync_viewer(state["img_idx"])
                    dirty = True
                elif code & 3 == 0 and state["view"] == 5:
                    open_viewer(state["img_idx"])
                    dirty = True
            # kitty 键盘协议会把按键发成 CSI codepoint u，先还原成字符，否则图片页收不到 e/q
            plain = re.sub(r"\033\[<\d+;\d+;\d+[Mm]", "", burst)
            plain = re.sub(r"\033\[(\d+)(?:;[\d:]*)?u",
                           lambda m: chr(int(m.group(1))) if int(m.group(1)) < 0x110000 else "", plain)
            for k in plain:
                if k in "qQ":
                    if keys == "noq" or len(burst) > 1:
                        state["view"] = (state["view"] - 1) % 6
                        dirty = True
                        continue
                    raise SystemExit
                elif k in "eE":
                    state["view"] = (state["view"] + 1) % 6
                    dirty = True
                elif k in "zZ":
                    cycle_size(1 if k == "Z" else -1)
                    dirty = True
                elif k in "pP":
                    state["paused"] = not state["paused"]
                    dirty = True
                elif k in "cC":
                    state["proc"] = not state["proc"]
                    dirty = True
                elif k in "iI" and state["view"] == 5:
                    open_viewer(state["img_idx"])
                    dirty = True
                elif k in "wWsS" and state["view"] == 5:
                    step = -1 if k in "wW" else 1
                    state["img_idx"] = (state["img_idx"] + step) % max(1, len(image_list()))
                    save_pos(state["img_idx"])
                    sync_viewer(state["img_idx"])
                    dirty = True
            if dirty:
                nxt = 0
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(("\033[?1000l\033[?1006l" if mouse else "") + "\033[?25h\033[?1049l")
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    main()
