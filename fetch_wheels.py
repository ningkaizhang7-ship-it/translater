#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直接从国内 PyPI 镜像抓取指定版本的轮子, 下载到本地 wheels 目录, 供 pip --no-index 离线安装。"""
import os, re, time, urllib.request
from html import unescape
from urllib.parse import urljoin

MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wheels")
os.makedirs(OUT, exist_ok=True)

# (package, version, need_cp310_win_binary)
TARGETS = [
    ("transformers", "4.46.3", False),
    ("tokenizers", "0.20.3", True),
    ("huggingface_hub", "0.25.2", False),
    ("accelerate", "0.34.2", False),
    ("requests", "2.32.3", False),
    ("urllib3", "2.2.3", False),
    ("charset_normalizer", "3.3.2", False),
    ("nvidia-cublas-cu12", "", "win"),   # 纯 CUDA 二进制库, 平台轮子
    ("nvidia-cudnn-cu12", "", "win"),    # 纯 CUDA 二进制库, 平台轮子
    # --- 音频旁路(WASAPI loopback) 与 按应用采集 ---
    ("soundcard", "0.4.6", "py"),
    ("pycaw", "20251023", "py"),
    ("comtypes", "1.4.16", "py"),
    ("process-audio-capture", "1.0.0", "py"),
]

def get(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
            time.sleep(2)
    raise last

def pick_wheel(html, idx_url, pkg, ver, tag):
    anchors = re.findall(r'href="([^"]+)"', html)
    best = None
    for href in anchors:
        raw = unescape(href).split("#")[0]
        fn = raw.split("/")[-1]
        if not fn.endswith(".whl"):
            continue
        if ver and f"{pkg}-{ver}" not in fn:
            continue
        if tag == "bin":
            if "win_amd64" not in fn or "cp310" not in fn:
                continue
            return urljoin(idx_url, raw), fn
        if tag == "win":
            if "win_amd64" not in fn:
                continue
            return urljoin(idx_url, raw), fn
        if tag == "py":
            if "py3-none-any" not in fn:
                continue
            return urljoin(idx_url, raw), fn
        return urljoin(idx_url, raw), fn
    return best

PIP_UA = "pip/26.2.1"

def download(url, dest):
    # TUNA/阿里云 对 /packages 直连会 403, 需用 pip 风格 UA; 403 时换镜像重试
    bases = [
        url,
        url.replace("https://pypi.tuna.tsinghua.edu.cn/packages/", "https://mirrors.aliyun.com/pypi/packages/"),
        url.replace("https://pypi.tuna.tsinghua.edu.cn/packages/", "https://mirror.sjtu.edu.cn/pypi/packages/"),
    ]
    for b in bases:
        try:
            req = urllib.request.Request(b, headers={"User-Agent": PIP_UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                data = r.read()
            with open(dest, "wb") as f:
                f.write(data)
            return len(data)
        except Exception as e:
            print(f"     [{e}] fallback...", flush=True)
    raise RuntimeError(f"all mirrors failed for {url}")

total = 0
for pkg, ver, tag in TARGETS:
    idx = f"{MIRROR}/{pkg}/"
    html = get(idx)
    found = pick_wheel(html, idx, pkg, ver, tag)
    if not found:
        # 打印该版本的候选, 便于排错
        vers = sorted(set(re.findall(rf"{pkg}-([\d.]+)-", html)))[-8:]
        print(f"[skip] {pkg}=={ver} not found; available e.g. {vers}"); continue
    url, fn = found
    dest = os.path.join(OUT, fn)
    if os.path.exists(dest):
        print(f"[have] {fn}"); continue
    t0 = time.time()
    n = download(url, dest)
    total += n
    print(f"[ok]   {fn}  ({n/1e6:.1f} MB, {time.time()-t0:.1f}s)")

print(f"\nDone. Downloaded {total/1e6:.1f} MB into {OUT}")
