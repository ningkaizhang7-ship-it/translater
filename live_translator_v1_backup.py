#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_ru2zh  —— 实时 俄语 -> 简体中文 直播翻译管线

管线：Chrome/系统音频 (VB-CABLE / Voicemeeter) -> silero VAD 分段
      -> faster-whisper (俄语转写) -> NLLB-200 (ru -> zh-Hans) -> 实时字幕

用法：
    python live_translator.py                 # 默认：本地字幕窗口 + 本地 HTTP 叠加层(8000)
    python live_translator.py --list-devices  # 列出音频输入设备，用于确认采集设备名
    python live_translator.py --no-gui        # 只用 HTTP 叠加层(给 OBS 用)，不开窗口

环境变量（均可覆盖默认值）：
    INPUT_DEVICE  采集设备名(子串匹配)或索引, 默认自动找 "CABLE Output" / "VoiceMeeter"
    WHISPER_SIZE  faster-whisper 模型: tiny|base|small|medium|large-v3, 默认 medium
    WHISPER_DEVICE  cuda|cpu, 默认 cuda
    WHISPER_COMPUTE float16|int8_float16, 默认 float16
    NLLB_MODEL    NLLB 模型, 默认 facebook/nllb-200-distilled-600M
    NLLB_DEVICE   cuda|cpu, 默认 cuda
    SRC_LANG      源语言码, 默认 ru
    TGT_LANG      目标语言码(NLLB), 默认 zho_Hans (简体中文)
    APP_PORT      本地叠加层端口, 默认 8000
"""

import argparse
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

# 控制台可能是 GBK 等非 UTF-8 编码; 让 print 永不因俄语/中文/emoji 崩溃
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

# 把模型下载/缓存放进本项目目录，避免写到系统缓存区、也更易管理
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("HF_HOME", os.path.join(BASE_DIR, "hf_cache"))
os.environ.setdefault("TRANSFORMERS_CACHE", os.path.join(BASE_DIR, "hf_cache", "hub"))
os.environ.setdefault("XDG_CACHE_HOME", os.path.join(BASE_DIR, "hf_cache"))

# 让 ctranslate2 找到 NVIDIA 的 cuBLAS/cuDNN 运行时库(通过 pip nvidia-* 装入的 DLL)
_dll_handles = []

def _add_nvidia_cuda_to_path():
    import sys
    for sp in list(sys.path) + [os.path.dirname(__file__)]:
        nv = os.path.join(sp, "nvidia")
        if not os.path.isdir(nv):
            continue
        for sub in ("cublas", "cudnn", "cutlass", "cusparse", "cub"):
            b = os.path.join(nv, sub, "bin")
            if os.path.isdir(b):
                os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")
                try:
                    _dll_handles.append(os.add_dll_directory(b))
                except Exception:
                    pass

_add_nvidia_cuda_to_path()


# 模型走国内镜像，绕开 HuggingFace 连通问题
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
SAMPLE_RATE = 16000
VAD_CHUNK = 512          # silero VAD 每次处理的样本数 (16kHz)
MIN_UTTERANCE_SEC = 0.6  # 过短的语音片段丢弃

def env(name, default):
    return os.environ.get(name, default)

def torch_has_cuda():
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False

INPUT_DEVICE   = env("INPUT_DEVICE", "")            # 空 = 自动检测
WHISPER_SIZE   = env("WHISPER_SIZE", "medium")
WHISPER_DEVICE = env("WHISPER_DEVICE", "cuda")      # cuda / cpu；无 GPU 会自动退回 cpu
WHISPER_COMPUTE= env("WHISPER_COMPUTE", "float16")
NLLB_MODEL     = env("NLLB_MODEL", "facebook/nllb-200-distilled-600M")
NLLB_DEVICE    = env("NLLB_DEVICE", "")             # 空 = 自动：有 GPU 用 cuda，否则 cpu
SRC_LANG       = env("SRC_LANG", "ru")
TGT_LANG       = env("TGT_LANG", "zho_Hans")
APP_PORT       = int(env("APP_PORT", "8000"))

# --- 「攒几句再翻译」批处理参数: 提高翻译连贯性 ---
MAX_SENTENCES  = int(env("MAX_SENTENCES", "3"))   # 攒满几句就一起翻译
MAX_CHARS      = int(env("MAX_CHARS", "160"))      # 或累计够多字符就翻译
FLUSH_IDLE     = float(env("FLUSH_IDLE", "4.0"))   # 或停顿这么久没新句子就翻译

OVERLAY_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>
  body{margin:0;background:rgba(0,0,0,.55);color:#fff;font-family:'Microsoft YaHei',sans-serif;overflow:hidden;padding:14px}
  #log{display:flex;flex-direction:column;gap:8px}
  .line{padding:8px 10px;border-radius:8px;background:rgba(20,20,30,.72);transition:opacity .4s}
  .ru{font-size:15px;opacity:.72;margin-bottom:3px}
  .zh{font-size:22px;font-weight:700;line-height:1.35}
</style></head><body><div id="log"></div>
<script>
async function tick(){
  try{
    const r=await fetch('/data'); const j=await r.json();
    const log=document.getElementById('log'); log.innerHTML='';
    j.lines.slice(-6).forEach(l=>{
      const d=document.createElement('div'); d.className='line';
      const ru=document.createElement('div'); ru.className='ru'; ru.textContent=l.ru;
      const zh=document.createElement('div'); zh.className='zh'; zh.textContent=l.zh;
      d.appendChild(ru); d.appendChild(zh); log.appendChild(d);
    });
  }catch(e){}
  setTimeout(tick,500);
}
tick();
</script></body></html>"""


# --------------------------------------------------------------------------- #
# 共享状态
# --------------------------------------------------------------------------- #
speech_queue = queue.Queue()   # (音频片段 numpy) 待转写+翻译
result_queue = queue.Queue()   # (ru_text, zh_text) 已翻译，供显示
result_lines = []              # 供 HTTP 叠加层用的最近 N 条
lines_lock = threading.Lock()

def push_result(ru, zh):
    result_queue.put((ru, zh))
    with lines_lock:
        result_lines.append({"ru": ru, "zh": zh})
        if len(result_lines) > 60:
            del result_lines[:-60]


# --------------------------------------------------------------------------- #
# 1) 音频采集线程：读取录音设备 -> 转 16kHz 单声道 float32 -> 入队
# --------------------------------------------------------------------------- #
import sounddevice as sd

def pick_input_device():
    """根据 INPUT_DEVICE 配置 / 自动检测，返回 sounddevice 设备索引。"""
    devs = sd.query_devices()
    if INPUT_DEVICE != "":
        # 支持子串匹配或索引
        if INPUT_DEVICE.isdigit():
            return int(INPUT_DEVICE)
        for i, d in enumerate(devs):
            if INPUT_DEVICE in d["name"]:
                return i
        print(f"[!] 未找到匹配设备 '{INPUT_DEVICE}'，使用默认输入。", file=sys.stderr)
        return sd.default.device[0]
    # 自动检测 VB-CABLE / VoiceMeeter
    for key in ("CABLE Output", "VoiceMeeter Input", "CABLE-A Output"):
        for i, d in enumerate(devs):
            if key in d["name"]:
                print(f"[+] 自动选用采集设备[{i}] {d['name']}")
                return i
    print("[!] 未检测到虚拟音频设备，将使用默认输入设备(可能采到麦克风)。请先安装并配置 VB-CABLE。",
          file=sys.stderr)
    return sd.default.device[0]


def audio_reader(device_index, out_q):
    # 用较大的块读取(减少 callback 次数), 由 VAD 线程内部再切分成 512 抽样
    blocksize = max(VAD_CHUNK * 4, 2048)

    def callback(indata, frames, time_info, status):
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        out_q.put(indata.copy()[:, 0])  # 单声道

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32",
        device=device_index, blocksize=blocksize, callback=callback,
    )
    with stream:
        while True:
            time.sleep(1.0)  # 回调线程持续运行；主循环仅保活


# --------------------------------------------------------------------------- #
# 2) VAD 分段线程：silero VAD 把连续音频切成「一句话」
# --------------------------------------------------------------------------- #
class VADSegmenter(threading.Thread):
    def __init__(self, in_q):
        super().__init__(daemon=True)
        self.in_q = in_q
        self.in_speech = False
        self.cur = []
        self.prob_hist = []   # 最近概率，用于平滑去抖

    def run(self):
        from silero_vad import load_silero_vad
        import torch
        model = load_silero_vad()
        print("[+] VAD 已加载")

        THRESH = 0.5
        NEED_SILENCE = 6      # 连续多少帧低于阈值才算结束(去抖)
        low_run = 0

        while True:
            block = self.in_q.get()
            if block is None:
                break
            # 把较大的读入块切分成 VAD_CHUNK(512) 抽样, 逐一喂给 silero
            for off in range(0, len(block), VAD_CHUNK):
                chunk = block[off:off + VAD_CHUNK]
                if len(chunk) < VAD_CHUNK:
                    break
                p = torch.tensor(chunk.astype(np.float32))
                prob = model(p, SAMPLE_RATE).item()

                if not self.in_speech and prob > THRESH:
                    self.in_speech = True
                    low_run = 0
                    self.cur = [chunk]
                elif self.in_speech:
                    self.cur.append(chunk)
                    if prob < THRESH:
                        low_run += 1
                        if low_run >= NEED_SILENCE:
                            self._emit()
                    else:
                        low_run = 0

    def _emit(self):
        self.in_speech = False
        audio = np.concatenate(self.cur)
        self.cur = []
        if len(audio) < MIN_UTTERANCE_SEC * SAMPLE_RATE:
            return
        speech_queue.put(audio)


# --------------------------------------------------------------------------- #
# 3) 转写+翻译流水线：faster-whisper(ru) -> NLLB(zh-Hans)
# --------------------------------------------------------------------------- #
class ASRTranslateWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.whisper = None
        self.translator = None
        self.pending = []          # 待一起翻译的句子
        self.pending_chars = 0

    def run(self):
        from faster_whisper import WhisperModel
        from transformers import pipeline

        # --- 语音识别设备：优先 GPU(ctranslate2 需 CUDA 运行时库)，自检失败则回退 CPU ---
        whisper_device = WHISPER_DEVICE
        try:
            print(f"[loading] faster-whisper '{WHISPER_SIZE}' on {whisper_device} ...")
            self.whisper = WhisperModel(
                WHISPER_SIZE, device=whisper_device, compute_type=WHISPER_COMPUTE,
                download_root=os.path.join(BASE_DIR, "models_whisper"),
            )
            # 自检: 用一小段静音触发一次编码, 确认 CUDA 运行时库(cuBLAS等)可用
            probe = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
            list(self.whisper.transcribe(probe, language=SRC_LANG, beam_size=1, vad_filter=False))
        except Exception as e:
            print(f"[!] GPU 识别不可用({e})，回退到 CPU ...", file=sys.stderr)
            whisper_device = "cpu"
            self.whisper = WhisperModel(
                WHISPER_SIZE, device="cpu", compute_type="int8",
                download_root=os.path.join(BASE_DIR, "models_whisper"),
            )

        # --- 翻译设备：自动检测，有 GPU 用 cuda，否则 cpu ---
        nllb_device = NLLB_DEVICE or ("cuda" if torch_has_cuda() else "cpu")
        print(f"[loading] NLLB '{NLLB_MODEL}' on {nllb_device} ...")
        self.translator = pipeline(
            "translation", model=NLLB_MODEL, device=0 if nllb_device == "cuda" else -1,
            src_lang=SRC_LANG, tgt_lang=TGT_LANG,
        )
        print("[+] 转写+翻译模型就绪。开始监听直播声音……")

        def translate(txt):
            if not txt.strip():
                return ""
            try:
                out = self.translator(txt, max_length=512)
                return out[0]["translation_text"]
            except Exception as e:
                print(f"[translate error] {e}", file=sys.stderr)
                return ""

        def flush_pending():
            """把攒下的句子一起翻译, 提升连贯性。"""
            if not self.pending:
                return
            ru = " ".join(self.pending)
            zh = translate(ru)
            print(f"\nRU  {ru}\nZH  {zh}\n", flush=True)
            push_result(ru, zh)
            self.pending = []
            self.pending_chars = 0

        while True:
            try:
                audio = speech_queue.get(timeout=FLUSH_IDLE)
            except queue.Empty:
                # 一段时间没新句子 => 把手头的句子翻译掉, 避免一直憋着
                flush_pending()
                continue
            if audio is None:
                break
            try:
                segments, _ = self.whisper.transcribe(
                    audio, language=SRC_LANG, beam_size=1, vad_filter=False,
                )
                text = "".join(s.text for s in segments).strip()
            except Exception as e:
                print(f"[asr error] {e}", file=sys.stderr)
                text = ""
            if not text:
                continue
            # 攒句子, 达到阈值再一起翻译
            self.pending.append(text)
            self.pending_chars += len(text)
            if len(self.pending) >= MAX_SENTENCES or self.pending_chars >= MAX_CHARS:
                flush_pending()


# --------------------------------------------------------------------------- #
# 4) 本地 HTTP 叠加层(给 OBS 浏览器源用)：http://127.0.0.1:8000/
# --------------------------------------------------------------------------- #
class OverlayHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = OVERLAY_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/data":
            with lines_lock:
                payload = json.dumps({"lines": result_lines[-20:]})
            body = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
        else:
            self.send_response(404); body = b""
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


OVERLAY_PORT = APP_PORT

def start_http_server():
    global OVERLAY_PORT
    # 端口被占用时不再崩溃, 而是换一个可用端口(叠加层失败不影响字幕窗口)
    for p in range(APP_PORT, APP_PORT + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), OverlayHandler)
            OVERLAY_PORT = p
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            print(f"[+] OBS 叠加层: 在 OBS 里添加「浏览器源」, 地址填 http://127.0.0.1:{OVERLAY_PORT}/")
            return
        except OSError:
            continue
    print("[!] 端口被占用, 叠加层未启动(不影响字幕窗口)", file=sys.stderr)


# --------------------------------------------------------------------------- #
# 5) GUI 置顶字幕窗口
# --------------------------------------------------------------------------- #
def run_gui():
    import tkinter as tk
    from tkinter import font as tkfont

    root = tk.Tk()
    root.title("RU → 简体中文 实时翻译")
    # 强制窗口置顶、带到前台、放在屏幕中上部, 确保一定能看到
    root.attributes("-topmost", True)
    try:
        w, h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"760x300+{(w - 760) // 2}+{max(40, (h // 2) - 360)}")
    except Exception:
        root.geometry("760x300+200+120")
    root.configure(bg="black")
    root.deiconify()
    root.lift()
    root.focus_force()
    root.update_idletasks()

    text = tk.Text(root, bg="black", fg="white", wrap="word",
                   font=tkfont.Font(size=18, weight="bold"), insertbackground="white")
    text.pack(fill="both", expand=True, padx=10, pady=10)
    text.tag_configure("ru", foreground="#bdbdbd", font=(None, 12))
    text.tag_configure("zh", foreground="#ffffff", font=(None, 20, "bold"))

    # 预滚动占位
    text.insert("end", "正在加载模型…… 打开直播并确认声音已路由到 CABLE Input 后, 这里会滚动显示俄语原文与中文翻译。\n",
                ("zh",))
    text.see("end")

    def poll():
        while True:
            try:
                ru, zh = result_queue.get_nowait()
            except queue.Empty:
                break
            text.insert("end", f"🇷🇺 {ru}\n", ("ru",))
            text.insert("end", f"🇨🇳 {zh}\n", ("zh",))
            text.see("end")
        root.after(200, poll)

    root.after(200, poll)
    root.mainloop()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-devices", action="store_true", help="列出音频输入设备")
    ap.add_argument("--no-gui", action="store_true", help="不开置顶窗口，只用 HTTP 叠加层")
    args = ap.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        print("\n可用输入设备索引如上。可将其中一个名字/索引设为 INPUT_DEVICE 环境变量。")
        return

    dev_index = pick_input_device()
    print(f"[+] 使用输入设备索引: {dev_index}")

    audio_q = queue.Queue()
    VADSegmenter(audio_q).start()
    threading.Thread(target=audio_reader, args=(dev_index, audio_q), daemon=True).start()
    ASRTranslateWorker().start()
    start_http_server()

    if args.no_gui:
        print("[+] 运行中(无窗口)。按 Ctrl+C 停止。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n已停止。")
    else:
        run_gui()


if __name__ == "__main__":
    main()
