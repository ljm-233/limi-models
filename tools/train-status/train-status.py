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
BOLD = "\033[1m"
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

ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def fit_line(s, W, ellipsis=True):
    """按显示宽度裁剪一行，保留颜色转义不截断。窗口窄的时候不至于窜出去。"""
    if W <= 0:
        return ""
    if wlen(s) <= W:
        return s
    out, cur, i = [], 0, 0
    limit = W - (1 if ellipsis else 0)
    while i < len(s):
        mm = ANSI_RE.match(s, i)
        if mm:
            out.append(mm.group(0))
            i = mm.end()
            continue
        ch = s[i]
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if cur + w > limit:
            break
        out.append(ch)
        cur += w
        i += 1
    out.append(R)
    if ellipsis:
        out.append("…")
    return "".join(out)


def fit_page(lines, W, H):
    """整页适配：每行裁到窗口宽，行数裁到窗口高。五页共用同一套规则。"""
    rows = max(1, (H or 0) - 3)
    fixed = [fit_line(x, W) for x in lines]
    if len(fixed) <= rows:
        return fixed
    # 放不下时：先保前几行，再保最后一行（末行常是结论/评分）
    head = fixed[:max(1, rows - 1)]
    return head + [fixed[-1]]


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
                if base.endswith(".py") and pat in base:     # train_network / sdxl_train_network 都认
                    return True
        return False
    return cache("alive:" + pat, ttl, probe)


def read(path, default=""):
    """按 mtime+大小缓存的小文件读取。流程页/质量页每帧要读好几个小文件。"""
    try:
        st = os.stat(path)
        key = f"read:{path}:{st.st_mtime}:{st.st_size}"
        hit = _C.get(key)
        if hit and time.time() - hit[0] < 5:
            return hit[1]
        txt = open(path, encoding="utf-8", errors="ignore").read()
        _C[key] = (time.time(), txt)
        return txt
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
        urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=0.15)
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


SKIP_LOGS = ("comfy_eval", "popup", "dsh-purge")


def _usable(p):
    name = os.path.basename(p)
    return not any(k in name for k in SKIP_LOGS)


def auto_log():
    """谁在跑就跟谁：切图/训练/出图各认自己的日志"""
    if alive("build_base_png"):
        return "/tmp/rebuild_png.log"
    if alive("train_network"):
        best, bt = None, 0
        # 训练日志现在写在 $TRAIN/logs_*.log 里，不能只扫 /tmp，否则会抓到旧探针的 20 步日志
        cands = glob.glob("/tmp/*.log") + glob.glob(f"{TRAIN}/*.log")
        for f in cands:
            if not _usable(f):
                continue
            try:
                if time.time() - os.path.getmtime(f) > 600:
                    continue
                tail = open(f, "rb").read()[-65536:].decode("utf-8", "ignore")
            except Exception:
                continue
            if "steps:" in tail and os.path.getmtime(f) > bt:
                best, bt = f, os.path.getmtime(f)
        if best:
            return best
    if alive("eval_auto") or alive("gen_detail") or alive("gen_face"):
        for f in ("/tmp/eval_v3.log", "/tmp/eval_v4.log"):
            if os.path.exists(f):
                return f
        best, bt = None, 0
        for f in glob.glob("/tmp/*.log"):
            if not _usable(f):
                continue
            try:
                if os.path.getmtime(f) > bt:
                    best, bt = f, os.path.getmtime(f)
            except Exception:
                pass
        if best:
            return best
    return None


def last_log():
    a = auto_log()
    if a:
        return a
    t = tracked_log()
    if t:
        return t
    now, best = time.time(), None
    for f in glob.glob("/tmp/*.log") + glob.glob("/tmp/*.out"):
        if not _usable(f):
            continue
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
        # 只认训练自己的进度条（以 steps: 开头）；缓存 latent 的那些 "[数字]" 行不算
        if "steps:" not in line and "steps " not in line:
            continue
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
        # total 很小说明这是探针/旧日志，加 base 会把 20 步变成 7200 步，直接不用
        if total < 500:
            base = 0
        if 0 < base < total * 20:          # 明显是脏值就不加，别把 200/200 变成 7200/200
            step += base
            total += base
    except Exception:
        pass
    return step, total, rate, losses


def cache_progress():
    """缓存 latent 阶段：日志里只有 "659/2959 [..it/s]" 这种行，没有 steps: 前缀"""
    p = last_log()
    txt = cache("cacheprog:" + str(p), 0.5, lambda: log_text(p))
    last = None
    for line in txt.splitlines():
        if "steps:" in line:
            continue
        m = re.search(r"(\d+)/(\d+)\s*\[[\d:]+<[\d:]+,\s*([\d.]+)(it/s|s/it)", line)
        if m:
            last = m
    if not last:
        return None, None
    return int(last.group(1)), int(last.group(2))


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






def dir_size(path):
    """目录总字节 + 最新改动时刻，缓存 60 秒（这个数字一小时也不会变）。"""
    def scan():
        size, newest = 0, 0.0
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        st = e.stat()
                    except Exception:
                        continue
                    size += st.st_size
                    newest = max(newest, st.st_mtime)
        except Exception:
            pass
        return {"size": size, "newest": newest}
    return cache("dirsize:" + path, 60, scan)


def dir_counts(path):
    """一次 scandir 拿到目录里各扩展名的数量和总字节，缓存 2 秒。

    原来 dataset_count / crop_progress / _cache_sub / stage_progress 各扫一遍目录，
    每帧四千多个条目扫三四次；现在一次算完共用。
    """
    def scan():
        # 只数名字，不 stat：stat 一千次要 9ms，而数量每帧都要看
        c = {"png": 0, "txt": 0, "npz": 0, "jpg": 0, "txt_npz": 0}
        try:
            with os.scandir(path) as it:
                for e in it:
                    ext = e.name.rsplit(".", 1)[-1].lower() if "." in e.name else ""
                    if ext in c:
                        c[ext] += 1
                    if ext == "txt" and e.name.endswith("_sdxl.txt"):
                        c["txt_npz"] += 1
        except Exception:
            pass
        return c
    return cache("dircounts:" + path, 2.0, scan)


def _material_count():
    """素材母本里的原片总数（跟符号链接走）"""
    base = os.path.expanduser("~/Desktop/马吉素材")
    n = 0
    for d in os.listdir(base):
        dp = os.path.join(base, d)
        if os.path.isdir(dp):
            n += len(glob.glob(os.path.join(dp, "*.jpg"))) + len(glob.glob(os.path.join(dp, "*.png")))
    return n


def current_set():
    """当前这一版数据集目录：挑 mtime 最新的 dataset_v*face/10_machi，退回 v3_png。

    2026-11-16：以前这里是写死的路径，换一版就要手改一次，忘了就显示过期数字。
    """
    cands = []
    for d in glob.glob(f"{TRAIN}/dataset_v*face/10_machi") + glob.glob(f"{TRAIN}/dataset_v*_png/10_machi"):
        if os.path.isdir(d):
            cands.append((os.path.getmtime(d), d))
    if cands:
        return max(cands)[1]
    return f"{TRAIN}/dataset_v3_png/10_machi"


def dataset_count():
    """当前这一版训练数据有多少张（优先数据集目录，其次清单）"""
    try:
        return dir_counts(current_set())["png"]
    except Exception:
        return 0


def crop_progress():
    """无损切图进度：已出 PNG / 预计总数（当前清单源图数 × 4 类）"""
    try:
        n = dir_counts(current_set())["png"]
    except Exception:
        n = 0
    try:
        src = max(1, _material_count())
    except Exception:
        src = 1522
    return n, src


def stage():
    if alive("train_network"):
        # 还没打出 steps 进度 = 正在建 latent/文本缓存
        try:
            st_, tot_, rate_, _ = parse(log_text(last_log())) if last_log() else (None, None, None, [])
        except Exception:
            st_ = None
        return "训练" if st_ else "缓存"
    if alive("gen_detail") or alive("gen_face"):
        return "出图"
    if alive("build_base_png") or alive("cut_v7face") or alive("cut_v7"):
        return "切图"
    if alive("eval_auto") or alive("eval_faces"):
        return "评分"
    if idle_now():
        return "空闲"
    # 当前这一版还没产出任何权重 → 是「待训」，不是「完成」
    name = pending_output()
    if name:
        if not glob.glob(f"{TRAIN}/out_*/*{name}*.safetensors"):
            return "待训"
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
    def calc():
        per, ms = imgs_per_epoch(), max_steps()
        if not (per and ms):
            return 0
        return -(-(ms * grad_accum()) // per)
    return cache("totalepochs", 5, calc)


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
    """每轮吃多少张：优先数当前数据集，别用旧的 .status_imgs（那是 v3 的 2959）"""
    try:
        n = len(glob.glob(os.path.join(current_set(), "*.png")))
        if n:
            return n
    except Exception:
        pass
    return int(read(f"{TRAIN}/.status_imgs", "0").strip() or 0) or 0


def _log_sig(path):
    """日志签名：大小按 4KB 取整。训练每步都在写，按字节缓存会全失效。"""
    try:
        st = os.stat(path)
        return f"{path}:{st.st_mtime}:{st.st_size // 4096}"
    except Exception:
        return str(path)


def loss_series_cached(path, n):
    return cache("lossser:" + _log_sig(path), 0.8, lambda: loss_series(n))


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
        out.append(f"{GRN}出图{R} {GRN}在线{R}" if cache("comfy", 15, comfy_online)
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
    # 工具信息：都是现场查，不写死（2026-11-16 他要"还能不能自动更新"）
    def _probe():
        py = sys.version.split()[0]
        torch_v = "—"
        try:
            # 别 import torch：那是 1 秒起的开销，读分发元数据就够
            from importlib.metadata import version as _v
            torch_v = _v("torch").split("+")[0]
        except Exception:
            pass
        sds = "—"
        try:
            import subprocess as _sp
            sds = _sp.run(["git", "-C", "/home/link/sd-scripts", "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True, timeout=3).stdout.strip() or "—"
        except Exception:
            pass
        have = lambda p_: "✓" if os.path.exists(p_) else "·"
        return py, torch_v, sds, (
            have("/home/link/ComfyUI/models/instantid/ip-adapter.bin")
            + have("/home/link/ComfyUI/models/insightface/inswapper_128.onnx")
            + have("/home/link/ComfyUI/models/pulid/ip-adapter_pulid_sdxl_fp16.safetensors")
        )
    py_v, ty_v, sds_v, inj = cache("probe", 30, _probe)
    out.append(f"{DIM}py{R} {BLU}{py_v}{R} {DIM}torch{R} {BLU}{ty_v}{R} {DIM}sd-scripts{R} {BLU}{sds_v}{R} {DIM}身份注入 I/P/F{R} {GRN}{inj}{R}")

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


STAGE_FILE = "/tmp/ts_stages.json"



def proc_starts():
    """一次遍历 /proc，返回 {脚本名片段: 最早启动时刻}。缓存 1 秒。

    原来每个阶段各扫一遍 /proc（单次 2.7ms，流程页要扫五六次），现在只扫一次。
    """
    def scan():
        out = {}
        try:
            uptime = float(open("/proc/uptime").read().split()[0])
            hz = os.sysconf("SC_CLK_TCK")
        except Exception:
            return out
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                args = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "ignore").split("\0")
            except Exception:
                continue
            base = None
            for a in args:
                if a and a.endswith(".py"):
                    base = os.path.basename(a)
                    break
            if not base:
                continue
            try:
                fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
                ts = time.time() - (uptime - float(fields[19]) / hz)
            except Exception:
                continue
            cur = out.get(base)
            out[base] = ts if cur is None else min(cur, ts)
        return out
    return cache("procstarts", 1.0, scan)


def proc_start_ts(*names):
    """从 proc_starts() 的表里取最早的那个，不再自己扫 /proc。"""
    if not names:
        return None
    table = proc_starts()
    best = None
    for base, ts in table.items():
        if any(n in base for n in names):
            best = ts if best is None else min(best, ts)
    return best


def tick_stages():
    """记录每个阶段的起止时刻；结果缓存 1 秒（流程页一帧要问五次，别每次都读盘）"""
    return cache("tickstages", 1.0, _tick_stages_impl)


def _tick_stages_impl():
    """真正干活的部分"""
    st = stage()
    try:
        d = json.load(open(STAGE_FILE))
    except Exception:
        d = {}
    now = time.time()
    # 只在「换了一版」时清零：同一版里每一步的耗时要留着（他要看的正是这个使用时间）
    try:
        run = pending_output() or current_run() or ""
    except Exception:
        run = ""
    if run and d.get("_run") != run:
        d = {"_run": run}
    cur = d.get("_cur")
    if cur != st:
        # 从空闲进入某个阶段 = 新的一轮，历史耗时一律清零，否则界面会显示几天前的 22 小时
        if cur in ("空闲", "待机", "完成", None) and st not in ("空闲", "待机", "完成"):
            d = {}
        if cur and cur in d and not d[cur].get("end"):
            d[cur]["end"] = now
        d.setdefault(st, {"start": now, "end": None})
        if not d[st].get("start"):
            d[st]["start"] = now
        d[st]["end"] = None
        d["_cur"] = st
        try:
            json.dump(d, open(STAGE_FILE, "w"))
        except Exception:
            pass
    return d


def log_durations():
    """从日志回填这一段的时间：训练 = 步数/速率；缓存 = 总跨度 - 训练"""
    p = last_log()
    if not p:
        return {}
    txt = log_text(p)
    stamps = []
    for line in txt.splitlines():
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
        if not m:
            continue
        try:
            stamps.append(time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")))
        except Exception:
            pass
    step, total, rate, _ = parse(txt)
    out = {}
    if step and rate:
        out["训练"] = step / rate
    if stamps:
        span = max(stamps) - min(stamps)
        if out.get("训练") and 0 < span - out["训练"] < 6 * 3600:
            out["缓存"] = span - out["训练"]
    return out


def shot_span():
    """出图耗时：按这一版样图的生成时间跨度算"""
    lm = latest_weight()
    if not lm:
        return None
    stem = os.path.basename(lm[0]).rsplit(".", 1)[0]
    try:
        ts = [os.path.getmtime(f) for f in glob.glob(f"/home/link/ComfyUI/output/eval_{stem}_*.png")]
    except Exception:
        return None
    if len(ts) < 2:
        return None
    span = max(ts) - min(ts)
    return span if 0 < span < 2 * 3600 else None


def stage_time(name):
    """某一步花了多久：跑着的是到现在为止，跑完的是最终值。

    2026-11-16：以前用 tick_stages 的累计起点，跨天累加，切图能显示 22 小时。
    现在跑着的步骤一律以「这一轮进程真实的启动时刻」为准。
    """
    running = {
        "切图": ("cut_v7face", "cut_v7", "build_base_png"),
        "训练": ("train_network", "sdxl_train_network"),
        "出图": ("gen_face", "gen_detail"),
        "评分": ("eval_auto", "eval_faces"),
    }.get(name)
    if running:
        t0 = proc_start_ts(*running)
        if t0:
            return _dur(int(time.time() - t0))
        # 进程已经结束：用本轮记下的 start/end（tick_stages 里存的），别显示成空
    d = tick_stages()
    v = d.get(name) or {}
    st, en = v.get("start"), v.get("end")
    if not st and name == "切图":
        try:
            pngs = glob.glob(os.path.join(current_set(), "*.png"))
            if pngs:
                t0 = min(os.path.getmtime(x) for x in pngs)
                t1 = max(os.path.getmtime(x) for x in pngs)
                if t1 > t0:
                    return _dur(int(t1 - t0))
        except Exception:
            pass
    if not st:
        if name == "出图":
            sp = shot_span()
            if sp:
                return _dur(sp)
        back = log_durations().get(name)
        return _dur(back) if back else ""
    return _dur((en or time.time()) - st)


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
        if name.lower().startswith("cache"):      # 只为建缓存的短跑，不算一版
            return ""
        return name or ""
    return ""


def _cache_sub():
    """缓存进度：优先数盘上的 latent 文件，日志只在缓存正在写时补一脚。"""
    try:
        _set = current_set()
        _c = dir_counts(_set)
        png, npz = _c["png"], _c["npz"]
        te = 0
        if png:
            if te:
                return f"latent {npz}/{png} · 文本 {te}"
            return f"latent {npz}/{png}" if npz else ""
    except Exception:
        pass
    if stage() != "训练":
        return ""
    cd, ct = cache_progress()
    return f"{cd}/{ct} 张" if cd and ct else ""


def _cache_dur():
    st_ = stage()
    cd, ct = cache_progress()
    if st_ == "训练" and cd and ct and not progress()[0]:
        return "进行中"
    if cd and ct and progress()[0]:
        return "已完成"
    return "—"


def _shot_count():
    """这一版出的样图：先认 eval_<权重>_，没有就算今天新出的图（别显示 0 骗人）"""
    out = "/home/link/ComfyUI/output"
    try:
        lm = latest_weight() or in_use_lora()
        if lm:
            stem = os.path.basename(lm[0]).rsplit(".", 1)[0]
            n = len([f for f in os.listdir(out) if f.startswith(f"eval_{stem}_") and f.endswith(".png")])
            if n:
                return n
        day0 = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        return len([f for f in os.listdir(out)
                    if f.endswith(".png") and os.path.getmtime(os.path.join(out, f)) >= day0])
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



def render_cached(tag, key, fn, ttl=1.0):
    """整页渲染缓存：同一组输入 1 秒内不重算。

    内容没变就没必要每秒重排一次字符串；时间戳、进度这些都会进 key，
    所以看到的仍是实时的，只是重复的同一帧不重复劳动。
    """
    return cache("render:" + tag + ":" + str(hash(key)), ttl, fn)


def page_flow(step, total, n_img, st, rate):
    times = _stage_times()
    def _src_count():
        """清单：素材母本里到底有多少张原片（以集中目录为准，比 report 行数可信）"""
        base = os.path.expanduser("~/Desktop/马吉素材")
        try:
            n = 0
            for d in os.listdir(base):
                dp = os.path.join(base, d)
                if os.path.isdir(dp):
                    n += len(glob.glob(os.path.join(dp, "*.jpg"))) + len(glob.glob(os.path.join(dp, "*.png")))
            if n:
                return n
        except Exception:
            pass
        rep = f"{TRAIN}/report_v5.csv" if os.path.exists(f"{TRAIN}/report_v5.csv") else f"{TRAIN}/report_v3.csv"
        try:
            return max(0, sum(1 for _ in open(rep)) - 1)
        except Exception:
            return 0
    src = cache("src", 5, _src_count)
    cnt = lambda pat, ttl=60: cache("n:" + pat, ttl, lambda: len(glob.glob(pat)))
    _set = current_set()
    _c = dir_counts(_set)
    cut = _c["png"]
    tag = max(0, _c["txt"] - _c["txt_npz"])
    ep = epoch_display(step, total, n_img)
    tot = total_epochs()

    lw = latest_weight()
    lw_name = os.path.basename(lw[0]).rsplit(".", 1)[0] if lw else ""
    train_dur = "—"
    if st == "训练":
        train_dur = stage_time("训练") or times.get("训练", "—")
    elif st in ("出图", "评分", "完成"):
        train_dur = stage_time("训练") or times.get("训练", "—")
    if st == "训练" and not (step and rate):
        train_dur = "等缓存"
    elif st == "训练" and step and rate:
        train_dur = _dur(step / rate)
    train_sub = "" if st in ("空闲", "待机", "完成") else _train_sub(ep, tot)
    if st in ("出图", "评分", "完成") and lw_name:
        train_sub = f"已结束 · {lw_name}"

    # 数据集/清单这一版是多少张，供第一步展示
    dset = dataset_count()
    rows = [
        ("更新工具信息", f"清单 {src} 张 · 本版 {dset} 张", ""),
        ("扫描", f"{src} 张原图" if src else "", stage_time("扫描") or times.get("扫描", "—")),
        ("切图", (f"{cut}/{src} 张" if st == "切图" and cut and src else (f"{cut} 张裁片" if cut else "")),
         stage_time("切图") or times.get("切图", "—")),
        ("打标", (f"{tag} 条（切图时写入）" if st in ("切图", "待训") and tag else (f"{tag} 条描述" if tag else "")),
         stage_time("打标") or times.get("打标", "—")),
        ("缓存", _cache_sub(), stage_time("缓存") or "—"),
        ("训练", train_sub, train_dur),
        ("出图", ("待训练完成" if st in ("训练", "切图", "待训") else f"本次 {_shot_count()} 张"),
         stage_time("出图") or times.get("出图", "—")),
        ("评分", ("待出图" if st in ("训练", "出图") else "相似度+纹理"),
         stage_time("评分") or times.get("评分", "—")),
    ]
    names = [r[0] for r in rows]
    csub = cache("cacherow", 1.0, lambda: _cache_sub())
    cache_running = bool(csub) and not progress()[0] and st == "训练"
    if cache_running:
        now = names.index("缓存")      # 缓存还没跑完，光标停在缓存这一行
    elif st in names:
        now = names.index(st)
    elif st in ("空闲", "待机"):
        now = -1                      # 没有正在跑的步骤：谁都不打勾
    elif st == "完成":
        now = names.index("训练") + 1  # 训练确实走完了，出图/评分都还没跑
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



def metrics_tail(n=60):
    """最近 n 条采样：[(ts, vram, temp, util, rate)]"""
    out = []
    try:
        with open(f"{TRAIN}/metrics.csv") as fh:
            for line in fh.read().splitlines()[1:][-n:]:
                p = line.split(",")
                if len(p) >= 4:
                    out.append((int(p[0]), float(p[1]), float(p[2]), float(p[3]),
                                float(p[4]) if len(p) > 4 and p[4] else 0.0))
    except Exception:
        pass
    return out


def id_scores():
    """每档权重的身份分：{步数: 中心分}，由 eval_v7.py 写进 train/eval_v7.json"""
    try:
        return json.load(open(f"{TRAIN}/eval_v7.json"))
    except Exception:
        return {}


def page_quality(W):
    out = []
    vals = loss_series_cached(last_log() or "", min(max(8, W - 12), 32))
    if vals:
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1e-9
        spark = "".join(f"{_SPARK_COLOR[(v - lo) / span]}{_SPARK[min(7, int((v - lo) / span * 7))]}{R}"
                        for v in vals)
        lo_txt = f"{lo:.3f}"
        out.append(f"{CYA}loss{R} {spark} {DIM}{hi:.3f}→{lo_txt}{R}")
        k = max(1, len(vals) // 10)
        head, tail = sum(vals[:k]) / k, sum(vals[-k:]) / k
        trend = f"{GRN}↓{R}" if tail < head - 1e-4 else (f"{HOT}↑{R}" if tail > head + 1e-4 else f"{DIM}→{R}")
        trend += f"{DIM}(末10步 vs 首10步){R}"
        out.append(f"{DIM}当前{R} {WH}{vals[-1]:.4f}{R} {trend} "
                   f"{DIM}最低{R}{GRN}{lo:.4f}{R} {DIM}最高{R}{BLU}{hi:.4f}{R}")
    else:
        st_now = stage()
        if st_now == "切图":
            n, tot = crop_progress()
            out.append(f"{CYA}loss{R} {DIM}还没开训 · 正在切图 {n}/{tot}{R}")
        elif st_now == "训练":
            out.append(f"{CYA}loss{R} {DIM}日志里还没有 avr_loss（前几步还没打）{R}")
        else:
            out.append(f"{CYA}loss{R} {DIM}未开始训练{R}")

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

    name = current_run() or pending_output()
    cks = [f for f in glob.glob(f"{TRAIN}/out_*/*-step*.safetensors")
           if not name or os.path.basename(f).startswith(name)]
    cks = [f for f in cks if name and os.path.basename(f).startswith(name)] if name else []
    if cks:
        f = max(cks, key=os.path.getmtime)
        step_n = re.search(r"-step(\d+)", os.path.basename(f))
        n = int(step_n.group(1)) if step_n else 0
        out.append(f"{ORG}存档{R}{BLU}{n}{R}{DIM}步{R} {DIM}{os.path.getsize(f) / 1048576:.0f}MB{R}")
    else:
        st_, tot_, rate_, *_ = progress()
        nxt = ""
        if st_ and rate_:
            left = 500 - (st_ % 500)
            nxt = f" {DIM}下次{R}{WH}{left}{R}{DIM}步 · 约{R}{BLU}{_dur(int(left / rate_))}{R}{DIM}后{R}"
        out.append(f"{ORG}存档{R} {DIM}还没有{R}{nxt}")

    durs = epoch_durations()
    tot_ep = total_epochs()
    if durs:
        detail = " ".join(f"{i + 1}:{_dur(d)}" for i, d in enumerate(durs))
        st_, tot_, rate_, *_ = progress()
        if rate_ and tot_:
            per = int(imgs_per_epoch() / max(rate_ * grad_accum(), 1e-9))
            out.append(f"{DIM}每轮约{R}{BLU}{_dur(per)}{R}{DIM} · 全部结束约{R}{BLU}{time.strftime('%H:%M', time.localtime(time.time() + (tot_ - (st_ or 0)) / rate_))}{R}")
        out.append(f"{CYA}各轮{R} {BLU}{detail}{R}"
                   + (f" {DIM}共{R}{WH}{tot_ep}{R}{DIM}轮{R}" if tot_ep else ""))

    # 身份分随步数（每档权重自动评估后才有）
    ids = id_scores()
    if ids:
        pts = sorted((int(k), float(v)) for k, v in ids.items())
        bars = "".join(f"{GRN if v >= 0.5 else (YEL if v >= 0.35 else HOT)}{_SPARK[min(7, int(v * 7))]}{R}" for _, v in pts)
        out.append(f"{VIO}身份分{R} {bars} {DIM}{pts[0][0]}→{pts[-1][0]}步 · 末{R}{WH}{pts[-1][1]:.3f}{R}")
    else:
        out.append(f"{VIO}身份分{R} {DIM}存档后自动评估（纯 LoRA + machi）{R}")

    # 吞吐 / 显存 / 温度
    ms = metrics_tail(40)
    if ms:
        # 三条迷你趋势要按窗口宽度分长度，不然窄窗口会被截掉后两项
        labels = ("温度", "显存", "吞吐")
        vals = (f"{ms[-1][2]:.0f}℃", f"{ms[-1][1]/1024:.1f}G", f"{ms[-1][4]:.2f}/s")
        # 宽度按显示宽度算（中文两格），每段留 3 格给标签/数值/空格
        overhead = sum(wlen(a) + wlen(b) + 3 for a, b in zip(labels, vals))
        per = max(3, min(16, (W - overhead) // 3))

        def spark_of(idx, lo, hi, col, n):
            seg = [m[idx] for m in ms if m[idx]][-n:]
            if not seg:
                return ""
            return "".join(f"{col}{_SPARK[min(7, max(0, int((v - lo) / max(hi - lo, 1e-9) * 7)))]}{R}"
                           for v in seg)
        ok = per >= 4
        parts = []
        if ok:
            parts.append(f"{DIM}温度{R} {spark_of(2, 50, 90, ORG, per)} {WH}{vals[0]}{R}")
            if per >= 6:
                parts.append(f"{DIM}显存{R} {spark_of(1, 2000, 8000, BLU, per)} {WH}{vals[1]}{R}")
                parts.append(f"{DIM}吞吐{R} {spark_of(4, 0, 0.4, CYA, per)} {WH}{vals[2]}{R}")
        if not parts:
            parts = [f"{DIM}温度{R}{WH}{vals[0]}{R} {DIM}显存{R}{WH}{vals[1]}{R} {DIM}吞吐{R}{WH}{vals[2]}{R}"]
        out.append(" ".join(parts))

    v6tag = "·" 
    rep = last_report()
    if rep:
        sc = GRN if rep[1] >= 0.66 else (YEL if rep[1] >= 0.60 else HOT)
        tc = GRN if rep[2] >= 8 else (YEL if rep[2] >= 6.5 else HOT)
        out.append(f"{YEL}评分{R} {DIM}上次{v6tag}{R} {DIM}相似{R}{sc}{rep[1]:.3f}{R} "
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
    """数据页：当前这一版的数据实况 + 和上一版的对比 + 训练吃的是哪份快照。"""
    _set = current_set()
    _dc = dir_counts(_set)
    n_png = _dc["png"]
    tagged = max(0, _dc["txt"] - _dc["txt_npz"])   # 减掉给 npz 复制的副本
    npz = _dc["npz"]
    _ds = dir_size(_set)
    newest = cache("pngmtime:" + _set, 60, lambda: max(
        (os.path.getmtime(os.path.join(_set, n)) for n in os.listdir(_set) if n.endswith(".png")), default=0))
    age = _dur(time.time() - newest) if newest else "—"
    size_mb = _ds["size"] / 2**30
    src = _material_count()
    cut_off = max(0, src - n_png)
    run = cache("penddata", 30, pending_output) or "—"

    # 训练吃的是哪份快照：数据集冻结时刻 vs 训练进程开跑时刻
    t_train = proc_start_ts("train_network", "sdxl_train_network")
    frozen = time.strftime("%H:%M", time.localtime(newest)) if newest else "—"
    if t_train and newest:
        snap = "训练用的是这份快照" if newest <= t_train + 60 else "训练后又改过数据（训练吃的是旧的）"
    else:
        snap = "还没开训"

    # 上一版（v6 半身）对比：有就并列，没有就略过
    v6 = glob.glob(f"{TRAIN}/dataset_v6face/10_machi/*.png")
    cmp_line = ""
    if v6:
        cmp_line = (f"{DIM}v6{R} {BLU}{len(v6)}{R}{DIM}张 半身 896 · {R}"
                    f"{GRN}v7{R} {WH}{n_png}{R}{DIM}张 紧裁 1024{R}")

    out = [
        f"{CYA}本版{R} {WH}{os.path.basename(_set)}{R} {DIM}·{R} {BLU}{run}{R}",
        f"{GRN}切图{R} {WH}{n_png}{R}{DIM}/{R}{BLU}{src}{R} {DIM}张 · {size_mb:.1f}G · 1024px · 脸占 62%（切图规则）{R}",
        f"{YEL}剔除{R} {WH}{cut_off}{R}{DIM} 张（无脸/眼睛被挡/近似重复）{R}",
        f"{YEL}标注{R} {WH}{tagged}{R}{DIM}/{n_png} 张（{int(100*tagged/max(1,n_png))}%，切图时统一写入）{R}",
        f"{VIO}缓存{R} {WH}{npz}{R}{DIM}/{n_png} 份 latent{R}" if npz else f"{VIO}缓存{R} {DIM}未建{R}",
        cmp_line,
        f"{CYA}快照{R} {BLU}{frozen}{R}{DIM} 冻结 · {age}前改动 · {snap}{R}",
        f"{DIM}在役{R} {BLU}{(pretty_model(in_use_lora()[0]) if in_use_lora() else '—')[:26]}{R}",
    ]
    return [x for x in out if x]



def log_text_wide(path, head_kb=256, tail_kb=256):
    """头 256K + 尾 256K：启动参数在头、存档与异常在尾，中间全是进度条。"""
    try:
        st = os.stat(path)
        with open(path, "rb") as fh:
            head = fh.read(head_kb * 1024)
            if st.st_size > (head_kb + tail_kb) * 1024:
                fh.seek(st.st_size - tail_kb * 1024)
                tail = fh.read()
            else:
                tail = b""
        return (head + "\n...[mid omitted]...\n".encode() + tail).decode("utf-8", "ignore").replace("\r", "\n")
    except Exception:
        return ""


def page_log(W, step, total, losses):
    """日志页：出处 + 进度/告警 + 关键事件 + 最近几行原文。

    2026-11-16 重写。旧版找不到关键词就统一写「这一版日志里没有关键行」，
    明明在正常训练也像在报错；也没有存档/启动这类事件，五小时里只剩三个数字。
    """
    path = last_log() or ""
    raw = log_text_wide(path)
    errs, warns = [], []
    events = []          # 关键事件：启动、存档、完成、异常
    tail_lines = []      # 最近几行原文

    for chunk in raw.split("\n"):
        t = re.sub(r"\s{2,}", " ", chunk).strip()
        if len(t) < 6 or t.startswith("steps:") or re.search(r"\d+/\d+ \[\d+:", t):
            continue
        if re.search(r"it/s\]|s/it\]", t) or not re.search(r"[A-Za-z]{3,}", t):
            continue
        if "ERROR" in t or "Traceback" in t or "Killed" in t or "out of memory" in t:
            errs.append(t)
        elif "WARNING" in t:
            warns.append(t)
        if any(k in t for k in ("model saved", "saving checkpoint", "steps: 100%",
                                "accelerate", "network_dim", "compiling", "caching latents",
                                "cache", "use fp8")):
            events.append(t)
        tail_lines.append(t)

    age = log_age()
    name = os.path.basename(path) or "—"
    # 出处：这轮训练自己的日志才叫本版
    cur = pending_output() or current_run() or ""
    is_current = bool(cur) and (os.path.exists(f"{TRAIN}/logs_v7_run.log") and path == f"{TRAIN}/logs_v7_run.log")
    tag = f"{DIM}本版{R}" if is_current else f"{DIM}历史{R}"
    head = f"{VIO}日志{R} {BLU}{name}{R} {tag}"
    if age is not None:
        col = GRN if age < 20 else (YEL if age < 90 else HOT)
        head += f" {col}{age}s前{R}"
    head += f" {DIM}关键事件{R}{WH}{len(events)}{R} {DIM}原文{R}{WH}{len(tail_lines)}{R}{DIM}行{R}"

    e_c = f"{HOT}错 {len(errs)}{R}" if errs else f"{GRN}错 0{R}"
    w_c = f"{YEL}警 {len(warns)}{R}" if warns else f"{DIM}警 0{R}"
    mid = f"{DIM}进度{R}{WH}{step or 0}{DIM}/{total or 0}{R} {e_c} {w_c}"
    if losses:
        mid += f" {DIM}loss{R}{WH}{losses[-1]:.4f}{R}"
    if idle_now():
        mid = f"{DIM}空闲 · 这条是历史日志{R}" if not is_current else f"{DIM}空闲{R}"

    out = [head, mid]

    # 1) 告警/错误先跳出来（只贴第一条，省地方）
    if errs:
        out.append(f"{HOT}✕ {errs[0][:max(10, W - 4)]}{R}")
    elif warns:
        out.append(f"{YEL}! {warns[0][:max(10, W - 4)]}{R}")
    else:
        out.append(f"{GRN}无告警{R} {DIM}（没有 error / traceback / OOM）{R}")

    # 2) 关键事件：最近 4 条
    for t in events[-4:]:
        t = re.sub(r"\s+[a-z_]+\.py:\d+$", "", t)
        out.append(f"{BLU}· {t[:max(10, W - 4)]}{R}")

    # 3) 最近原文：填满剩下的行
    rooms = max(2, (H_ROWS[0] or 14) - len(out) - 2) if H_ROWS[0] else 4
    for t in tail_lines[-rooms:]:
        out.append(f"{DIM}{t[:max(10, W - 1)]}{R}")
    if len(tail_lines) == 0:
        out.append(f"{DIM}日志还是空的（刚启动或还没写盘）{R}")
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
        rep = f"{TRAIN}/report_v5.csv" if os.path.exists(f"{TRAIN}/report_v5.csv") else f"{TRAIN}/report_v3.csv"
        if os.path.exists(rep):
            return _sorted(_drop([r["file"] for r in csv.DictReader(open(rep))
                                  if os.path.exists(r["file"])]))
        return _sorted(_drop(glob.glob(f"{TRAIN}/dataset_v3_base/10_machi/*.jpg")))
    lst = cache("imgs", 60, _load)
    try:
        if lst and not os.path.exists("/tmp/ts_imgs.txt"):
            with open("/tmp/ts_imgs.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(lst) + "\n")
    except Exception:
        pass
    return lst


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


def bg_seq(state, W, H):
    """文字页的底图：最近看的那张图调暗铺满窗口，桌面不参与"""
    try:
        if not image_list():
            return ""
        g = cache("geom", 2, panel_geom)
        sc = cache("scale", 30, output_scale)
        px = (int(g[2] * sc), int(g[3] * sc)) if g else None
        path = preview_image(state["img_idx"])
        key = f"{path}|{px}"
        state["bg_key"] = key
        return cache("bg:" + key, 600, lambda: _dimmed_img(path, px))
    except Exception:
        return ""


def _dimmed_img(path, px):
    from PIL import Image
    import io, base64
    try:
        tp = thumb(path, 0, 0, (1744, 1441))
        im = Image.open(tp).convert("RGB")
        if px:
            im = im.resize((int(px[0]), int(px[1])), Image.LANCZOS)
        im = Image.eval(im, lambda v: int(v * 0.32))
        buf = io.BytesIO(); im.save(buf, "PNG", compress_level=1)
        data = base64.b64encode(buf.getvalue()).decode()
        out = []
        chunks = [data[i:i + 3800] for i in range(0, len(data), 3800)]
        for i, c in enumerate(chunks):
            more = 0 if i == len(chunks) - 1 else 1
            head = (f"\033_Ga=T,f=100,i=7,q=2,m={more}" if i == 0 else f"\033_Gm={more}")
            out.append(f"{head};{c}\033\\")
        return "".join(out) + "\033[H"
    except Exception:
        return ""


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
    # 标题优先显示「这一版在做的」：正在训的 > 目标已定但还没开跑 > 在役权重
    run = cache("currun", 5, current_run)
    pend = cache("pend", 30, pending_output)
    run_name = (run[1] if isinstance(run, (list, tuple)) and len(run) > 1 else (run or ""))
    target = ""
    if run_name:
        target = run_name
    elif pend and st in ("切图", "训练", "待训"):
        target = pend
    full = pretty_model((target or (lora[0] if lora else "")) + ".safetensors") if (target or lora) else name
    short = full.split(" ")[0] if full else name
    parts_full = full or name
    ep = 0 if st in ("空闲", "待机", "完成") else epoch_display(step, total, n_img)
    tot = total_epochs()
    color = {"训练": CYA, "出图": YEL, "评分": VIO, "完成": GRN, "空闲": DIM}.get(st, DIM)
    ep_txt = f"{WH}第{ep}{('/' + str(tot)) if tot else ''}轮{R}" if ep else ""
    view_txt = f" {DIM}· {VIEWS[_CUR_VIEW[0]]}{R}"
    _r0 = proc_start_ts("cut_v7face", "cut_v7", "build_png", "build_base_png", "train_network",
                        "sdxl_train_network", "gen_face", "gen_detail", "eval_auto", "eval_faces")
    run_txt = f" {DIM}本轮{R}{BLU}{_dur(int(time.time()-_r0))}{R}" if _r0 else ""

    # 由长到短逐级尝试：全名 + 秒 + 页名 → 全名 → 只留项目名 → 去掉页名
    candidates = [
        f"{GRN}●{R}{WH}{parts_full}{R} {DIM}{time.strftime('%H:%M:%S')}{R} {color}{st}{R}"
        f"{(' ' + ep_txt) if ep_txt else ''}{run_txt}{view_txt}",
        f"{GRN}●{R}{WH}{parts_full}{R} {DIM}{time.strftime('%H:%M:%S')}{R} {color}{st}{R}"
        f"{(' ' + ep_txt) if ep_txt else ''}{run_txt}",
        f"{GRN}●{R}{WH}{short}{R} {DIM}{time.strftime('%H:%M')}{R} {color}{st}{R}"
        f"{(' ' + ep_txt) if ep_txt else ''}",
        f"{GRN}●{R}{WH}{short}{R} {color}{st}{R}",
    ]
    for line in candidates:
        if wlen(line) <= W:
            return line
    return candidates[-1]



def _bar_line(W, done, total, label, used=0, color=None):
    """统一的两行底栏：进度条 + 数量/总数 百分比 已用 剩余 至 速度。任何阶段都用它。"""
    color = color or CYA
    total = max(1, int(total or 0))
    done = max(0, int(done or 0))
    pct = min(100, done * 100 // total)
    filled = max(1, min(W, int(W * done / total))) if done else 0
    rate = (done / used) if used > 0 and done else 0
    remain = int((total - done) / rate) if rate else 0
    eta = time.strftime("%H:%M:%S", time.localtime(time.time() + remain)) if remain else "--:--:--"
    bar = f"{BOLD}{color}" + "━" * filled + f"{R}{DIM}" + "─" * max(1, W - filled) + R
    txt = (f"{color}{label}{R} {WH}{done}{DIM}/{total}{R} {color}{pct}%{R} "
           f"{DIM}已用{R}{BLU}{_dur(used)}{R} {DIM}剩{R}{YEL}{_dur(remain)}{R} "
           f"{DIM}至{R}{BLU}{eta}{R}")
    if rate:
        txt += f" {DIM}{rate:.2f}/s{R}"
    return [bar, txt]


def stage_progress(st):
    """每个阶段的 (done, total, label, 本阶段已用秒)。没有可数的就返回 None。"""
    _set = current_set()
    png = len(glob.glob(os.path.join(_set, "*.png")))
    txt = len(glob.glob(os.path.join(_set, "*.txt")))
    npz = len(glob.glob(os.path.join(_set, "*.npz")))
    src = _material_count()
    used = 0
    for names in ({"切图": ("cut_v7face", "cut_v7", "build_base_png"),
                   "训练": ("train_network", "sdxl_train_network"),
                   "出图": ("gen_face", "gen_detail"),
                   "评分": ("eval_auto", "eval_faces")}.get(st) or (),):
        t0 = proc_start_ts(*names) if names else None
        used = int(time.time() - t0) if t0 else 0
    if st == "扫描":
        return (src if src else 0, src or 0, "扫描", used)
    if st == "切图":
        t0 = proc_start_ts("cut_v7face", "cut_v7", "build_base_png")
        if not t0:
            pngs = glob.glob(os.path.join(_set, "*.png"))
            if pngs:
                t0 = min(os.path.getmtime(x) for x in pngs)
        used = int(time.time() - t0) if t0 else 0
        return (png, src or png, "切图", used)
    if st == "打标":
        return (txt, png or txt, "打标", used)
    if st == "缓存":
        t0 = proc_start_ts("sdxl_train_network", "train_network")
        if st == "缓存":
            pass
        return (npz, png or npz, "缓存", used)
    return None


def bottom(step, total, rate, W, cum=0):
    _sp = stage_progress(stage()) if stage() in ("扫描", "切图", "打标", "缓存") else None
    if _sp:
        done, tot, label, used = _sp
        return _bar_line(W, done, tot, label, used, CYA)
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
        body = render_cached('flow', (W, st, step, total, n_img, round(rate or 0, 2)), lambda: page_flow(step, total, n_img, st, rate))
    elif view == 2:
        body = render_cached('quality', (W, st, step, total), lambda: page_quality(W))
    elif view == 3:
        body = render_cached('data', (st, step), lambda: page_data())
    elif view == 4:
        body = page_log(W, step, total, losses)
    elif view == 0:
        body = page_env(state, st)
    _CUR_VIEW[0] = view
    # 五页共用一套适配：先按窗口宽裁每一行，再按窗口高裁行数
    body = fit_page(body, W, H)
    r = 2
    stop = max(2, H - 2)
    gap = 1 if (H < 20 or _NARROW[0]) else 2
    for line in body:
        if r > stop:
            break
        rows.append((r, line))
        r += gap
    # 底部给整体进度：累计批次 vs 总批次（= 优化步 × 梯度累积）；拿不到就退回日志自己的数字
    _sp = stage_progress(st) if st in ("扫描", "切图", "打标", "缓存") else None
    if _sp:
        done, tot, label, used = _sp
        return _bar_line(W, done, tot, label, used, CYA)
    if st == "切图":
        n, tot = crop_progress()
        pct = n * 100 // max(1, tot)
        filled = max(1, min(W, int(W * n / max(1, tot))))
        t0 = proc_start_ts("cut_v7face", "cut_v7", "build_base_png")
        used = int(time.time() - t0) if t0 else 0
        rate = (n / used) if used > 0 and n else 0
        remain = int((tot - n) / rate) if rate else 0
        eta = time.strftime("%H:%M:%S", time.localtime(time.time() + remain)) if remain else "--:--:--"
        b = [f"{BOLD}{CYA}" + "━" * filled + f"{R}{DIM}" + "─" * max(1, W - filled) + R,
             f"{CYA}切图{R} {WH}{n}/{tot}{R} {CYA}{pct}%{R} {DIM}已用{R}{BLU}{_dur(used)}{R} "
             f"{DIM}剩{R}{YEL}{_dur(remain)}{R} {DIM}至{R}{BLU}{eta}{R}"]
    elif st == "出图":
        n = _shot_count(); tot_s = 4
        pct = min(100, n * 100 // tot_s)
        filled = max(1, min(W, int(W * min(n, tot_s) / tot_s)))
        b = [f"{BOLD}{YEL}" + "━" * filled + f"{R}{DIM}" + "─" * max(1, W - filled) + R,
             f"{YEL}出图{R} {WH}{n}/{tot_s}{R} {DIM}{pct}%{R}"]
    elif st == "评分":
        n = int(time.time() * 2) % max(2, W - 4)
        b = [f"{DIM}" + "─" * n + f"{VIO}▌" + "─" * max(0, W - n - 2) + R,
             f"{VIO}评分中{R} {DIM}算相似度与纹理{R}"]
    elif st in ("空闲", "待机", "完成"):
        lw = latest_weight()
        nm = os.path.basename(lw[0]).rsplit(".", 1)[0] if lw else ""
        head_word = "完成" if st == "完成" else "空闲"
        cands = [f"{DIM}{head_word} · 无任务在跑{R} {DIM}· 上次产出{R} {BLU}{nm}{R}",
                 f"{DIM}{head_word}{R} {DIM}· 上次{R} {BLU}{nm}{R}",
                 f"{DIM}{head_word}{R}"]
        tip = next((c for c in cands if wlen(c) <= W - 1), cands[-1])
        b = [GRN + "━" * W + R, tip] if st == "完成" else [DIM + "─" * W + R, tip]
    elif st == "训练" and not step:
        cd, ct = cache_progress()
        if cd and ct:
            pct = cd * 100 // ct
            filled = max(1, min(W, int(W * cd / ct)))
            b = [f"{BOLD}{CYA}" + "━" * filled + f"{R}{DIM}" + "─" * max(1, W - filled) + R,
                 f"{CYA}缓存 latent{R} {WH}{cd}/{ct}{R} {DIM}{pct}%{R}"]
        else:
            b = bottom(None, None, None, W)
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
def pending_output():
    """还没开跑时，从训练脚本里找这一版的目标名（切图阶段也能显示 v4）"""
    import glob as _g
    for f in sorted(_g.glob(f"{TRAIN}/train_*.sh"), key=os.path.getmtime, reverse=True):
        try:
            txt = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        if not re.search(r"--train_data_dir\s+\S*dataset_v\w*", txt):
            continue                          # 没有数据集参数的脚本不算这一版
        m = re.search(r"--output_name\s+(\S+)", txt)
        if m:
            return m.group(1).strip("\\ ")
    return ""


def self_place():
    """把自己的窗口按到当前工作区：niri 有时会把浮动窗丢在 workspace=None 上"""
    try:
        out = subprocess.run(["niri", "msg", "--json", "windows"], capture_output=True, timeout=3).stdout
        for w in json.loads(out):
            if w.get("app_id") != "trainstatus":
                continue
            if w.get("workspace_id") is None or not w.get("is_floating"):
                subprocess.run(["niri", "msg", "action", "move-window-to-workspace",
                                "--window-id", str(w["id"]), "1"], timeout=3,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


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
    self_place()
    sys.stdout.write("\033_Ga=d,d=i,i=7\033\\")   # 清掉上一版留下的底图，别让它盖着窗口
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
                    bg = bg_seq(state, W, H) if os.environ.get("TRAIN_STATUS_BGIMG", "0") != "0" else ""
                    if bg and state.get("bg_key") != state.get("bg_key_done"):
                        sys.stdout.write(bg)
                        state["bg_key_done"] = state.get("bg_key")
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
