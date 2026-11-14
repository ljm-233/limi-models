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
    def probe():
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                if pat in open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "ignore"):
                    return True
            except Exception:
                pass
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
def last_log():
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
    step = total = rate = None
    losses = []
    for line in text.splitlines():
        m = re.search(r"(\d+)/(\d+)\s*\[[\d:]+<[\d:]+,\s*([\d.]+)(it/s|s/it)(?:,\s*avr_loss=([\d.]+))?", line)
        if m:
            step, total = int(m.group(1)), int(m.group(2))
            v = float(m.group(3))
            rate = v if m.group(4) == "it/s" else 1.0 / max(v, 1e-6)
            if m.group(5):
                losses.append(float(m.group(5)))
    try:
        base = int(read(BASE_FILE).strip())
        step = step + base if step is not None else None
        total = total + base if total is not None else None
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
    if stale:
        best = 0
        for f in glob.glob(f"{TRAIN}/out_*/*-step*.safetensors"):
            m = re.search(r"-step(\d+)", os.path.basename(f))
            if m:
                best = max(best, int(m.group(1)))
        if best and (step is None or best > step):
            step, src = best, "ckpt"
    return step, total, rate, losses, log_step, src


def stage():
    if alive("train_network"):
        return "训练"
    if alive("gen_detail") or alive("gen_face"):
        return "出图"
    if alive("eval_faces"):
        return "评分"
    return "完成" if last_log() else "待机"


EPOCH_FILE = f"{TRAIN}/.epoch_cur"
LASTSTEP_FILE = f"{TRAIN}/.last_step"


def absolute_mode(step, total):
    """日志里的 total 等于脚本的 max_train_steps，说明它报的是全局步数"""
    ms = max_steps()
    return bool(ms and total and abs(total - ms) <= 1)


def epochs_of(step, per):
    return (step // per + 1) if (step and per) else 0


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
    c, t = cpu_pct(), cpu_temp()
    s = (f"{CYA}CPU{R} {heat(c, 60, 85)}{c:>3}%{R} "
         f"{DIM}负载{R}{BLU}{load1():.1f}{R}")
    if t:
        s += f" {heat(t, 70, 85)}{t:.0f}℃{R}"
    return s


def line_gpu():
    g = gpu()
    if not g:
        return f"{DIM}GPU{R} {DIM}无数据{R}"
    u, used, tot, t, pw, lim = g
    s = (f"{VIO}GPU{R} {heat(u, 80, 97)}{u:>3}%{R} "
         f"{BLU}{used:.1f}{DIM}/{tot:.1f}G{R}")
    s += f" {heat(t, 72, 82)}{t}℃{R}"
    return s + (f" {ORG}{pw:.0f}{DIM}/{lim:.0f}W{R}" if lim else f" {ORG}{pw:.0f}W{R}")


def page_env(state):
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
    p = last_log()
    out.append(f"{VIO}日志{R} {BLU}{os.path.basename(p) if p else '—'}{R}")
    return out


def _stage_times():
    try:
        return json.load(open(f"{TRAIN}/stage_times.json"))
    except Exception:
        return {}


def page_flow(step, total, n_img, st, rate):
    times = _stage_times()
    src = cache("src", 60, lambda: max(0, sum(1 for _ in open(f"{TRAIN}/report_v3.csv")) - 1)
                if os.path.exists(f"{TRAIN}/report_v3.csv") else 0)
    cnt = lambda pat, ttl=60: cache("n:" + pat, ttl, lambda: len(glob.glob(pat)))
    cut = cnt(f"{TRAIN}/dataset_v3_base/10_machi/*.jpg")
    tag = cnt(f"{TRAIN}/captions_wd14/*.txt")
    ls = log_step if log_step is not None else step
    if absolute_mode(step, total):
        ep = epochs_of(step, n_img)
    else:
        ep = epoch_cur(ls, n_img) if (ls and n_img) else 0
    train_dur = _dur(step / rate) if (step and rate and st == "训练") else times.get("训练", "x")
    rows = [
        ("扫描", f"{src} 张原图" if src else "", times.get("扫描", "x")),
        ("切图", f"{cut} 张裁片" if cut else "", times.get("切图", "x")),
        ("打标", f"{tag} 条描述" if tag else "", times.get("打标", "x")),
        ("训练", f"{ep}/{rnd}轮" if rnd else "", train_dur),
        ("出图", "4 张样图", times.get("出图", "x")),
        ("评分", "相似度+纹理", times.get("评分", "x")),
    ]
    now = [r[0] for r in rows].index(st) if st in [r[0] for r in rows] else len(rows)
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
        dcol = GRN if dur not in ("x", "") else DIM
        pad = " " * max(1, 34 - wlen(body))
        out.append(body + pad + f"{dcol}{dur}{R}")
    return out


def _dur(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}h{sec % 3600 // 60:02d}m"
    return f"{sec // 60}m" if sec >= 60 else f"{sec}s"


_SPARK = "▁▂▃▄▅▆▇█"
class _SparkColor:
    """按相对高度取色：低绿、中青、偏高黄、最高红"""
    def __getitem__(self, f):
        return GRN if f < 0.35 else (CYA if f < 0.6 else (YEL if f < 0.85 else HOT))


_SPARK_COLOR = _SparkColor()


def page_quality(W):
    out = []
    vals = loss_series(min(max(8, W - 12), 32))
    if vals:
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1e-9
        spark = "".join(f"{_SPARK_COLOR[(v - lo) / span]}{_SPARK[min(7, int((v - lo) / span * 7))]}{R}"
                        for v in vals)
        out.append(f"{CYA}loss{R} {spark}")
        drop = vals[-1] - vals[0]
        trend = f"{GRN}↓{R}" if drop < 0 else (f"{HOT}↑{R}" if drop > 0 else f"{DIM}→{R}")
        out.append(f"{DIM}当前{R} {WH}{vals[-1]:.4f}{R} {trend} "
                   f"{DIM}最低{R}{GRN}{lo:.4f}{R} {DIM}最高{R}{BLU}{hi:.4f}{R}")
    else:
        out.append(f"{CYA}loss{R} {DIM}还没有数据{R}")
    for cand in (f"{TRAIN}/train_v5face.sh", f"{TRAIN}/train_v3wd14.sh"):
        sh = read(cand)
        lr = re.search(r"--learning_rate (\S+)", sh)
        if lr:
            sch = re.search(r"--lr_scheduler (\S+)", sh)
            out.append(f"{VIO}学习率{R} {WH}{lr.group(1)}{R} {DIM}{sch.group(1) if sch else ''}{R}")
            break
    for cand in (f"{TRAIN}/train_v5face.sh", f"{TRAIN}/train_v3wd14.sh"):
        sh = read(cand)
        if not sh:
            continue
        res = re.search(r"--resolution (\d+)", sh)
        dim = re.search(r"--network_dim (\d+)", sh)
        bs = re.search(r"--train_batch_size (\d+)", sh)
        if res:
            out.append(f"{CYA}本版{R} {WH}{os.path.basename(cand)}{R} {DIM}{res.group(1)}px{R} "
                       f"{DIM}dim{R}{BLU}{dim.group(1) if dim else '-'}{R} "
                       f"{DIM}batch{R}{BLU}{bs.group(1) if bs else '-'}{R}")
            break

    name, mb = latest_ckpt()
    if name:
        out.append(f"{ORG}存档{R} {BLU}{name}{R} {DIM}{mb:.0f}MB{R}")
    rep = last_report()
    if rep:
        sc = GRN if rep[1] >= 0.66 else (YEL if rep[1] >= 0.60 else HOT)
        tc = GRN if rep[2] >= 8 else (YEL if rep[2] >= 6.5 else HOT)
        out.append(f"{YEL}评分{R} {DIM}相似{R}{sc}{rep[1]:.3f}{R} "
                   f"{DIM}纹理{R}{tc}{rep[2]:.2f}{R} {DIM}{rep[0]}张{R}")
    else:
        out.append(f"{YEL}评分{R} {DIM}还没跑{R}")
    return out


def page_data():
    fs = glob.glob(f"{TRAIN}/dataset_v3_base/10_machi/*.jpg")
    kinds = collections.Counter(os.path.basename(f)[0] for f in fs)
    size = sum(os.path.getsize(f) for f in fs) / 2**30 if fs else 0
    tag = len(glob.glob(f"{TRAIN}/captions_wd14/*.txt"))
    return [
        f"{CYA}素材{R} {WH}13 套{R} {DIM}·{R} {BLU}53G{R}",
        f"{CYA}裁片{R} {WH}{len(fs)}{R} {DIM}张{R}",
        f"{GRN}脸{R}{WH}{kinds.get('f', 0)}{R}  {BLU}上身{R}{WH}{kinds.get('b', 0)}{R}  "
        f"{VIO}半身{R}{WH}{kinds.get('u', 0)}{R}  {ORG}整图{R}{WH}{kinds.get('n', 0)}{R}",
        f"{YEL}标注{R} {WH}{tag}{R} {DIM}条 WD14{R}",
        f"{CYA}体积{R} {BLU}{size:.1f}G{R}",
        f"{DIM}基线{R} {BLU}v3id{R} {DIM}相似{R}{YEL}0.676{R} {DIM}纹理{R}{YEL}7.33{R}",
    ]


def page_log(W, step, total, losses):
    path = last_log() or ""
    key = ("INFO", "WARNING", "ERROR", "Error", "Traceback", "saving", "checkpoint", "OOM", "Killed", "nan")
    lines = []
    for raw in log_text(path).split("\n"):
        t = re.sub(r"\s{2,}", " ", raw).strip()
        if len(t) < 6 or t.startswith("steps:") or not any(k in t for k in key):
            continue
        t = re.sub(r"^(INFO|WARNING|ERROR)\s*\[RANK \d+\]\s*", r"\1 ", t)
        t = re.sub(r"\s+[a-z_]+\.py:\d+$", "", t)
        t = re.sub(r"^saving checkpoint: .*/([^/]+)$", r"saving checkpoint: \1", t)
        if not lines or lines[-1] != t:
            lines.append(t)
    out = [f"{DIM}日志{R} {DIM}{os.path.basename(path) or '—'}{R}"]
    if step and total:
        ls = f" {DIM}loss{R} {WH}{losses[-1]:.4f}{R}" if losses else ""
        out.append(f"{DIM}进度{R} {WH}{step}/{total}{R}{ls}")
    if not lines:
        for raw in reversed(log_text(path).split("\n")):
            t = re.sub(r"\s{2,}", " ", raw).strip()
            if len(t) > 8 and not t.startswith("steps:"):
                lines.append(t)
                break
    room = max(1, 6 - (1 if (step and total) else 0))
    for t in lines[-room:]:
        if "ERROR" in t or "Traceback" in t or "Killed" in t:
            col = HOT
        elif "WARNING" in t:
            col = YEL
        elif "saving" in t or "checkpoint" in t:
            col = GRN
        else:
            col = TXT
        out.append(f"{col}{t[:max(8, W - 1)]}{R}")
    return out


# ── 图片页 ─────────────────────────────────────────────
def image_list():
    """必须和 photo-view.sh / imv 用同一份清单，否则索引错位"""
    def _load():
        try:
            with open("/tmp/ts_imgs.txt", encoding="utf-8") as f:
                lst = [l.strip() for l in f if l.strip()]
            if lst:
                return lst
        except Exception:
            pass
        return sorted(
            r["file"] for r in csv.DictReader(open(f"{TRAIN}/report_v3.csv"))
            if os.path.exists(r["file"])) if os.path.exists(f"{TRAIN}/report_v3.csv") \
            else sorted(glob.glob(f"{TRAIN}/dataset_v3_base/10_machi/*.jpg"))
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


def thumb(path, cols, rows):
    try:
        st = os.stat(path)
        key = f"{path}|{st.st_mtime_ns}|{cols}x{rows}"
    except Exception:
        key = f"{path}|{cols}x{rows}"
    return os.path.join(THUMB_DIR, hashlib.sha1(key.encode()).hexdigest() + ".png")


def kitty_img(path, cols, rows, row=1):
    if not path:
        return ""
    try:
        from PIL import Image
        import io, base64
        tp = thumb(path, cols, rows)
        png = open(tp, "rb").read() if os.path.exists(tp) else None
        if png is None:
            os.makedirs(THUMB_DIR, exist_ok=True)
            im = Image.open(path)
            im.draft("RGB", (cols * 44, rows * 88))
            im = im.convert("RGB")
            im.thumbnail((cols * 22, rows * 44), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            png = buf.getvalue()
            open(tp + ".tmp", "wb").write(png)
            os.replace(tp + ".tmp", tp)
        data = base64.standard_b64encode(png).decode("ascii")
        parts = [data[i:i + 3800] for i in range(0, len(data), 3800)]
        out = [f"\033[{row};1H"]
        for i, ch in enumerate(parts):
            head = "a=T,f=100," + (f"c={cols},r={rows}," if i == 0 else "")
            out.append(f"\033_G{head}C=1,q=2,m={0 if i == len(parts) - 1 else 1};{ch}\033\\")
        return "".join(out)
    except Exception:
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
def header(st, step, total, n_img, log_step=None):
    name = read(f"{TRAIN}/.project_name", "马吉 v3").strip() or "马吉 v3"
    ls = log_step if log_step is not None else step
    if absolute_mode(step, total):
        ep = epochs_of(step, n_img)
    else:
        ep = epoch_cur(ls, n_img) if (ls and n_img) else 0
    color = {"训练": CYA, "出图": YEL, "评分": VIO, "完成": GRN}.get(st, DIM)
    tail = f" {color}{st}{R}" + (f" {WH}第{ep}轮{R}" if ep else "")
    tail += f" {DIM}· {VIEWS[_CUR_VIEW[0]]}{R}"
    return f"{GRN}●{R} {WH}{name}{R} {DIM}{time.strftime('%H:%M:%S')}{R}{tail}"


def bottom(step, total, rate, W):
    if not (step and total and rate):
        return [DIM + "━" * max(1, W // 2) + "─" * max(1, W - W // 2) + R, f"{DIM}等待数据{R}"]
    pct = step * 100 // total
    filled = max(1, min(W, int(W * step / total)))
    bar = f"{CYA}{'━' * filled}{R}{DIM}{'─' * max(0, W - filled)}{R}"
    left = max(0, int((total - step) / rate))
    h, m, sec = left // 3600, left % 3600 // 60, left % 60
    remain = f"{h}h{m:02d}m{sec:02d}s" if h else f"{m}m{sec:02d}s"
    eta = time.strftime("%H:%M:%S", time.localtime(time.time() + left))
    for txt in (f"{WH}{step}/{total}{R} {WH}{pct}%{R} {DIM}剩{R} {WH}{remain}{R} {DIM}至{R} {WH}{eta}{R}",
                f"{WH}{step}/{total}{R} {WH}{pct}%{R} {DIM}剩{R}{WH}{remain}{R} {DIM}至{R}{WH}{eta}{R}",
                f"{WH}{step}/{total}{R} {WH}{pct}%{R} {DIM}剩{R}{WH}{remain}{R}",
                f"{WH}{step}/{total}{R} {WH}{pct}%{R}"):
        if wlen(txt) <= W:
            break
    return [bar, txt]


def compact(W, state, H):
    step, total, rate, losses, log_step, src = progress()
    st = cache("stage", 2.0, stage)
    n_img = imgs_per_epoch()
    view = state["view"]
    _CUR_VIEW[0] = view
    rows = [(1, header(st, step, total, n_img, log_step))]
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
        body = page_env(state)
    _CUR_VIEW[0] = view
    r = 3
    stop = max(3, H - 3)
    for line in body:
        if r > stop:
            break
        rows.append((r, line))
        r += 2
    per, ms = n_img, max_steps()
    if absolute_mode(step, total):
        b = bottom(min(step or 0, total or 0), total, rate, W)
    elif per and ms:
        if src == "ckpt":
            done = min(ms, step or 0)          # 存档步数本身就是全局步数
        else:
            ep_now = max(1, epoch_cur(log_step or step, per))
            done = min(ms, (ep_now - 1) * per + (step or 0))
        b = bottom(done, ms, rate, W)
    else:
        b = bottom(step, total, rate, W)
    rows += [(max(3, H - 2), b[0]), (max(3, H - 1), b[1]), (max(3, H), footer(state, W))]
    return rows, step, total


DEL_IMG = "\033_Ga=d,d=A\033\\"


def footer(state, W):
    """最后一行：页点 + 当前页快捷键提示"""
    view = state["view"]
    dots = "".join(f"{WH}●{R}" if i == view else f"{DIM}○{R}" for i in range(6))
    hints = {
        5: f"{BLU}w/s{R} 翻图 {BLU}滚轮{R} 翻图 {BLU}左键{R} 原图",
        4: f"{BLU}e/q{R} 翻页 {BLU}z/Z{R} 尺寸 {BLU}p{R} 暂停",
        0: f"{BLU}e/q{R} 翻页 {BLU}c{R} 占用 {BLU}z/Z{R} 尺寸",
    }.get(view, f"{BLU}e/q{R} 翻页 {BLU}z/Z{R} 尺寸 {BLU}p{R} 暂停")
    left = dots
    if wlen(left) + wlen(hints) + 2 > W:
        hints = f"{BLU}e/q{R} {BLU}z/Z{R}"
    pad = max(1, W - wlen(left) - wlen(hints))
    return left + " " * pad + hints


def draw(rows, W):
    out = [DEL_IMG, "\033[H\033[J"]
    for row, txt in rows:
        out.append(f"\033[{row};1H" + clip(fit(txt, W), W) + "\033[K")
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
    mt = os.path.getmtime(me) if os.path.exists(me) else 0
    W, H = tsize()
    state = {"view": 0, "paused": False, "proc": False, "img_idx": load_pos()}
    keys = os.environ.get("TRAIN_STATUS_KEYS", "all")     # all | noq
    mouse = os.environ.get("TRAIN_STATUS_MOUSE", "1") != "0"
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sys.stdout.write("\033[?1049h\033[?25l" + ("\033[?1000h\033[?1006h" if mouse else ""))
    nxt = time.time()
    try:
        tty.setcbreak(fd)
        while True:
            try:
                if os.path.exists(me) and os.path.getmtime(me) != mt:
                    os.execv(sys.executable, [sys.executable, me])
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
                        seq = cache(f"full:{key}", 600, lambda: kitty_img(path, W, H))
                        sys.stdout.write(DEL_IMG + "\033[H\033[J" + seq + "\033[?25l")
                        sys.stdout.flush()
                        state["drawn"] = key
                else:
                    rows, _, _ = compact(W, state, H)
                    draw(rows, W)
                    state["drawn"] = None
                nxt = time.time() + (1.0 if W < 62 else 3.0)

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
