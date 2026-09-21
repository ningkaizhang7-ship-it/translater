#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_ru2zh v2 —— 实时 俄语 -> 简体中文 直播翻译（旁路监听版）

特点：
  1) 像 oCam / OBS 一样「旁路监听」：直接采集声卡的**回环(WDM-KS loopback / Stereo Mix)**
     或虚拟声卡输出，**扬声器照常外放，完全不影响你自己听**。
  2) 可选择监听目标：主输出回环 / 第二输出回环 / 全部输入设备（配合 Windows「按应用指定输出」
     即可只监听某个应用；选主输出回环 = 全局监听）。
  3) UI：设备选择、开始/停止、实时字幕、**翻译文本导出**、**AI 自动总结（API）**。

管线：回环采集 -> 重采样16k -> silero VAD 分段 -> faster-whisper 俄语识别(GPU)
      -> NLLB 本地翻译(->简体中文) -> 字幕窗口 + OBS 叠加层

用法：
  python live_translator.py             # 图形界面
  python live_translator.py --no-gui    # 只跑叠加层(给 OBS 用)
  python live_translator.py --list-devices

AI 总结：需在同目录 ai_config.json 填 api_base / api_key / model（OpenAI 兼容接口，默认 DeepSeek）。
"""

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

# 控制台可能是 GBK，避免打印俄语/中文/emoji 崩溃
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("HF_HOME", os.path.join(BASE_DIR, "hf_cache"))
os.environ.setdefault("TRANSFORMERS_CACHE", os.path.join(BASE_DIR, "hf_cache", "hub"))
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("XDG_CACHE_HOME", os.path.join(BASE_DIR, "hf_cache"))

# 让 ctranslate2 找到 NVIDIA cuBLAS/cuDNN 运行时库
_dll_handles = []
def _add_nvidia_cuda_to_path():
    for sp in list(sys.path) + [BASE_DIR]:
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

# --------------------------------------------------------------------------- #
# DPI 感知（必须在创建 Tk 之前设置！否则在 125%/150% 缩放下 Windows 会位图拉伸 => 字体发糊）
# --------------------------------------------------------------------------- #
def _enable_dpi_awareness():
    import ctypes
    try:                                    # PER_MONITOR_AWARE_V2 (Win10 1703+)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except Exception:
        pass
    try:                                    # PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return True
    except Exception:
        pass
    try:                                    # SYSTEM_DPI_AWARE
        ctypes.windll.user32.SetProcessDPIAware()
        return True
    except Exception:
        return False

_DPI_READY = _enable_dpi_awareness() if os.name == "nt" else False

def win_screen_size():
    """真实屏幕像素（DPI 感知下 Tk 有时仍报旧值，故用 Win32）。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
    except Exception:
        return None, None

def win_dpi_scale():
    try:
        import ctypes
        return int(ctypes.windll.user32.GetDpiForSystem()) / 96.0
    except Exception:
        return 1.0

# 必须在创建 Tk 之前取好：Tk 会改变线程 DPI 上下文，之后再取会得到错误值(96)
_UI_SCALE = win_dpi_scale() if os.name == "nt" else 1.0
_UI_SCREEN = win_screen_size() if os.name == "nt" else (None, None)

import sounddevice as sd

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def env(name, default):
    return os.environ.get(name, default)

SAMPLE_RATE    = 16000        # 识别管线统一 16k
VAD_CHUNK      = 512          # silero VAD 每次 512 抽样

# —— ASR 侧：避免"过滤掉"教学示范发音 ——
MIN_UTTERANCE_SEC = float(env("MIN_UTTERANCE_SEC", "0.2"))  # 很短也保留（原 0.6 会丢掉示范发音）
VAD_THRESH        = float(env("VAD_THRESH", "0.5"))
WHISPER_BEAM      = int(env("WHISPER_BEAM", "5"))            # 提高识别准确度
WHISPER_CONDITION = env("WHISPER_CONDITION", "0") == "1"     # False=不跳过重复/示范内容

# —— 端点判定：确认"语句完整"后才翻译 ——
# 说话中静音多久才算这句说完了（原 ~160ms 太短，会把句子拦腰切断）
END_SILENCE_MS    = int(env("END_SILENCE_MS", "500"))
VAD_NEED_SILENCE  = max(1, round(END_SILENCE_MS / (VAD_CHUNK * 1000.0 / SAMPLE_RATE)))  # 帧数
# —— 流式原文：说话过程中每隔多久刷新一次"预览识别" ——
PARTIAL_INTERVAL_MS = int(env("PARTIAL_INTERVAL_MS", "700"))
PARTIAL_MAX_SEC     = float(env("PARTIAL_MAX_SEC", "20"))   # 预览只取最近这么长，避免越算越慢
PARTIAL_DISPLAY_CHARS = int(env("PARTIAL_DISPLAY_CHARS", "150"))  # "识别中"那行只显示最近的这些字符
# —— 完整性判定与兜底 ——
MIN_FINAL_CHARS   = int(env("MIN_FINAL_CHARS", "4"))        # 太短不算"完整句"
MIN_UNIT_CHARS    = int(env("MIN_UNIT_CHARS", "4"))         # 单句最短字数（切句用）
MAX_CHARS         = int(env("MAX_CHARS", "300"))            # 累计超长也翻（安全阀）
FLUSH_IDLE        = float(env("FLUSH_IDLE", "6.0"))         # 停顿这么久没新内容就翻
HARD_FLUSH_SEC    = float(env("HARD_FLUSH_SEC", "25.0"))    # 强制兜底：长句/不流利可再调大

# —— 翻译后端：nllb(本地) | llm(走 ai_config.json 的 API，泛用性更好) ——
# 默认 llm：泛用性/教学场景明显更好；无 Key 或请求失败会自动回退本地 NLLB
TRANSLATE_BACKEND = env("TRANSLATE_BACKEND", "llm")

# --------------------------------------------------------------------------- #
# 识别语言表：显示名 -> (Whisper 代码, NLLB/FLORES-200 代码)
# --------------------------------------------------------------------------- #
LANGUAGES = [
    ("俄语",     "ru", "rus_Cyrl"),
    ("英语",     "en", "eng_Latn"),
    ("日语",     "ja", "jpn_Jpan"),
    ("韩语",     "ko", "kor_Hang"),
    ("德语",     "de", "deu_Latn"),
    ("法语",     "fr", "fra_Latn"),
    ("西班牙语", "es", "spa_Latn"),
    ("意大利语", "it", "ita_Latn"),
    ("葡萄牙语", "pt", "por_Latn"),
    ("乌克兰语", "uk", "ukr_Cyrl"),
    ("波兰语",   "pl", "pol_Latn"),
    ("荷兰语",   "nl", "nld_Latn"),
    ("土耳其语", "tr", "tur_Latn"),
    ("阿拉伯语", "ar", "arb_Arab"),
    ("印地语",   "hi", "hin_Deva"),
    ("越南语",   "vi", "vie_Latn"),
    ("泰语",     "th", "tha_Thai"),
    ("印尼语",   "id", "ind_Latn"),
    ("自动检测", "auto", None),
]
_LANG_BY_WHISPER = {w: (n, w, nllb) for (n, w, nllb) in LANGUAGES}

def nllb_code_for(whisper_code):
    it = _LANG_BY_WHISPER.get(whisper_code)
    return it[2] if it else None

def lang_name_for(whisper_code):
    it = _LANG_BY_WHISPER.get(whisper_code)
    return it[0] if it else (whisper_code or "未知语言")

INPUT_DEVICE   = env("INPUT_DEVICE", "")     # 为空=自动挑一个回环设备; 也可填索引/名字子串
WHISPER_SIZE   = env("WHISPER_SIZE", "medium")
WHISPER_DEVICE = env("WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE= env("WHISPER_COMPUTE", "float16")
NLLB_MODEL     = env("NLLB_MODEL", "facebook/nllb-200-distilled-600M")
NLLB_DEVICE    = env("NLLB_DEVICE", "")
SRC_LANG       = env("SRC_LANG", "ru")
TGT_LANG       = env("TGT_LANG", "zho_Hans")
APP_PORT       = int(env("APP_PORT", "8000"))

# 运行时可变：当前识别语言（whisper 代码；"auto" = 自动检测）
lang_state = {"whisper": SRC_LANG if SRC_LANG in _LANG_BY_WHISPER else "ru",
              "detected": None}

# 「攒几句再翻译」批处理（MAX_CHARS / FLUSH_IDLE 已在上面定义）
MAX_SENTENCES  = int(env("MAX_SENTENCES", "3"))

AI_CONFIG_PATH = os.path.join(BASE_DIR, "ai_config.json")
TRANSCRIPT_DIR = os.path.join(BASE_DIR, "transcripts")
UI_STATE_PATH  = os.path.join(BASE_DIR, "ui_state.json")

# 字幕小窗专用的"透明色"：窗口背景用它填充后设为透明 => 只有文字可见
SUB_TCOLOR = "#010203"

def load_ui_state():
    try:
        if os.path.exists(UI_STATE_PATH):
            with open(UI_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_ui_state(state):
    try:
        with open(UI_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# --------------------------------------------------------------------------- #
# 共享状态
# --------------------------------------------------------------------------- #
speech_queue  = queue.Queue()        # 说完的完整片段 -> 最终转写+翻译
partial_audio_queue = queue.Queue()  # VAD -> worker：说话中的音频快照
partial_text_queue  = queue.Queue()  # worker -> 界面：预览出来的"原文"文本
result_queue  = queue.Queue()        # (ru, zh, ts) -> 界面（最终结果）
partial_state = {"text": ""}         # 当前"识别中"的原文（始终是 str）
capture_state = {"active": False, "device": ""}   # 是否正在监听
force_split = threading.Event()                   # worker 请求 VAD 立刻断开当前句（半句强翻后）
transcript = []                  # [{"t":datetime,"ru":..,"zh":..}]
recent_ctx = []                  # [(ru, zh), ...] 最近几对，供 LLM 翻译保持术语一致
lines_lock = threading.Lock()
status_cb = None                 # 由界面注册的状态回调

def partial_text():
    """安全地取"识别中"的原文（永远返回 str）。"""
    t = partial_state.get("text", "")
    return t if isinstance(t, str) else ""

def push_partial(text):
    """更新"识别中"的原文（不翻译）。"""
    if not isinstance(text, str):
        return
    partial_state["text"] = text
    try:
        partial_text_queue.put_nowait(text)
    except Exception:
        pass

def clear_partial():
    partial_state["text"] = ""
    try:
        partial_text_queue.put_nowait("")
    except Exception:
        pass

def build_context_text(n=3):
    """取最近 n 对(原文,译文)作为 LLM 翻译的上下文，用于术语/人称保持一致。"""
    with lines_lock:
        items = list(recent_ctx[-n:])
    if not items:
        return ""
    return "\n".join(f"- {ru}  →  {zh}" for ru, zh in items)

# —— Whisper 幻觉短语（在句尾/静音处凭空冒出，典型如"请点赞订阅"）——
HALLUCINATION_PATTERNS = (
    "please like and subscribe", "like and subscribe", "please subscribe",
    "subscribe to my channel", "thanks for watching", "thank you for watching",
    "thank you for watching!", "subtitles by", "subtitle by", "subtitles created by",
    "transcription by", "transcribed by", "amara.org", "www.mooji.org",
    "请点赞", "点赞订阅", "订阅频道", "感谢观看", "謝謝觀看", "字幕由", "字幕志愿者",
)

def strip_hallucinations(text):
    """去掉句子里的幻觉短语；若整句只剩幻觉则返回空串。"""
    if not text:
        return ""
    t = text
    for p in HALLUCINATION_PATTERNS:
        t = re.sub(re.escape(p) + r"[\s!！.。,，]*", "", t, flags=re.I)
    t = re.sub(r"\s{2,}", " ", t).strip(" \t,，、-—")
    return t

_SENT_END = (".", "?", "!", "…", "。", "？", "！")

def is_complete_sentence(text):
    """粗判"这句话说完整了"：够长 且 以句末标点结尾。"""
    t = (text or "").strip()
    return len(t) >= MIN_FINAL_CHARS and t.endswith(_SENT_END)

def split_for_translation(text, max_len=500):
    """把过长文本按句子切块（避免一次喂太长导致译文重复/退化）。"""
    t = (text or "").strip()
    if len(t) <= max_len:
        return [t] if t else []
    out, cur = [], ""
    for p in re.split(r"(?<=[.!?…。！？])\s+", t):
        if not p:
            continue
        if len(cur) + len(p) + 1 <= max_len:
            cur = (cur + " " + p).strip()
        else:
            if cur:
                out.append(cur)
            while len(p) > max_len:          # 单句过长则硬切
                out.append(p[:max_len])
                p = p[max_len:]
            cur = p
    if cur:
        out.append(cur)
    return out

def push_result(ru, zh):
    ts = datetime.now()
    with lines_lock:
        transcript.append({"t": ts, "ru": ru, "zh": zh})
        if len(transcript) > 5000:
            del transcript[:-5000]
        recent_ctx.append((ru, zh))
        if len(recent_ctx) > 3:
            del recent_ctx[:-3]
    result_queue.put((ru, zh, ts))

def set_status(msg):
    if status_cb:
        try:
            status_cb(msg)
        except Exception:
            pass
    print(f"[status] {msg}", flush=True)

# --------------------------------------------------------------------------- #
# 设备枚举（回环 / 输入）
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 设备枚举：WASAPI loopback（来自 soundcard）——对任意输出设备可靠旁路
# --------------------------------------------------------------------------- #
def list_loopback_devices():
    """返回 [(name, label), ...]：所有可旁路监听的输出设备。"""
    try:
        import soundcard as sc
        out = []
        for m in sc.all_microphones(include_loopback=True):
            if not getattr(m, "isloopback", False):
                continue
            out.append((m.name, f"🔊 {m.name}"))
        try:                                   # 默认扬声器排最前
            dname = sc.default_speaker().name
            out.sort(key=lambda it: 0 if it[0] == dname else 1)
        except Exception:
            pass
        return out
    except Exception as e:
        print(f"[device] soundcard 枚举失败: {e}", file=sys.stderr)
        return []

def auto_pick_loopback():
    """默认监听「默认扬声器」的旁路（即你正在用哪副耳机/音箱，就听哪个）。"""
    try:
        import soundcard as sc
        return sc.default_speaker().name
    except Exception:
        return None

# --------------------------------------------------------------------------- #
# 采集 + VAD
# --------------------------------------------------------------------------- #
class CaptureSession:
    """一次「开始监听」：WASAPI loopback 旁路采集 + VAD 分段。可停止。"""
    def __init__(self, device_name):
        self.device_name = device_name
        self.stop_event = threading.Event()
        self.raw_q = queue.Queue(maxsize=200)
        self.threads = []

    def start(self):
        self.threads = [
            threading.Thread(target=self._reader, daemon=True),
            threading.Thread(target=self._vad, daemon=True),
        ]
        for t in self.threads:
            t.start()

    def stop(self):
        self.stop_event.set()

    # --- 旁路采集：WASAPI loopback, 直接 16k 单声道, 不影响外放 ---
    def _reader(self):
        # soundcard 走 COM/WASAPI, 子线程必须先初始化 COM
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)   # COINIT_APARTMENTTHREADED
        except Exception:
            pass
        try:
            import soundcard as sc
            mic = sc.get_microphone(self.device_name, include_loopback=True)
            with mic.recorder(samplerate=SAMPLE_RATE, channels=1) as rec:
                capture_state["active"] = True
                capture_state["device"] = self.device_name
                set_status(f"监听中（旁路）：{self.device_name}")
                while not self.stop_event.is_set():
                    data = rec.record(numframes=int(SAMPLE_RATE * 0.1))
                    if data is None:
                        continue
                    x = np.asarray(data, dtype=np.float32).reshape(-1)
                    if x.size:
                        try:
                            self.raw_q.put_nowait(x)
                        except queue.Full:
                            pass
        except Exception as e:
            set_status(f"打开监听设备失败：{e}")
            print(f"[audio error] {e}", file=sys.stderr)

    # --- VAD 线程：16k float32 -> silero 分段 ---
    def _vad(self):
        try:
            from silero_vad import load_silero_vad
            import torch
            model = load_silero_vad()
        except Exception as e:
            set_status(f"VAD 加载失败：{e}")
            return
        THRESH, NEED_SILENCE = VAD_THRESH, VAD_NEED_SILENCE
        in_speech, low_run, cur = False, 0, []
        buf = np.zeros(0, dtype=np.float32)
        partial_every = max(1, int(PARTIAL_INTERVAL_MS / (VAD_CHUNK * 1000.0 / SAMPLE_RATE)))
        since_partial = 0

        while not self.stop_event.is_set():
            try:
                x = self.raw_q.get(timeout=0.3)
            except queue.Empty:
                continue
            buf = np.concatenate([buf, x]) if buf.size else x

            while buf.size >= VAD_CHUNK:
                chunk = buf[:VAD_CHUNK]
                buf = buf[VAD_CHUNK:]

                # worker 要求立刻断句（半句强翻之后，避免"识别中"重复已翻内容）
                if force_split.is_set():
                    force_split.clear()
                    if in_speech and cur:
                        audio = np.concatenate(cur)
                        cur = []
                        low_run = 0
                        if len(audio) >= MIN_UTTERANCE_SEC * SAMPLE_RATE:
                            speech_queue.put(audio)

                prob = model(torch.tensor(chunk), SAMPLE_RATE).item()
                if not in_speech and prob > THRESH:
                    in_speech, low_run, cur, since_partial = True, 0, [chunk], 0
                elif in_speech:
                    cur.append(chunk)
                    if prob < THRESH:
                        low_run += 1
                        if low_run >= NEED_SILENCE:
                            in_speech = False
                            audio = np.concatenate(cur)
                            cur = []
                            if len(audio) >= MIN_UTTERANCE_SEC * SAMPLE_RATE:
                                speech_queue.put(audio)     # 完整片段 -> 最终转写+翻译
                    else:
                        low_run = 0
                        # 说话中：周期性推送"预览快照"，让原文边说边出
                        since_partial += 1
                        if since_partial >= partial_every:
                            since_partial = 0
                            snap = np.concatenate(cur)
                            max_len = int(PARTIAL_MAX_SEC * SAMPLE_RATE)
                            if snap.size > max_len:
                                snap = snap[-max_len:]
                            if snap.size >= int(0.5 * SAMPLE_RATE):
                                try:
                                    partial_audio_queue.put_nowait(snap)
                                except Exception:
                                    pass

# --------------------------------------------------------------------------- #
# 转写 + 翻译（whisper GPU + NLLB CPU），攒几句再翻
# --------------------------------------------------------------------------- #
class ASRTranslateWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.whisper = None
        self.translator = None
        self.pending = []
        self.pending_chars = 0
        self.pending_since = None
        self.last_final = None
        self.ready = threading.Event()

    def _torch_has_cuda(self):
        try:
            import torch
            return torch.cuda.is_available()
        except Exception:
            return False

    def run(self):
        from faster_whisper import WhisperModel
        from transformers import pipeline

        dev = WHISPER_DEVICE
        try:
            set_status(f"加载识别模型 faster-whisper '{WHISPER_SIZE}' on {dev} ...")
            self.whisper = WhisperModel(WHISPER_SIZE, device=dev, compute_type=WHISPER_COMPUTE,
                                        download_root=os.path.join(BASE_DIR, "models_whisper"))
            probe = np.zeros(SAMPLE_RATE // 2, dtype=np.float32)
            _probe_lang = lang_state.get("whisper") or "ru"
            if _probe_lang == "auto":
                _probe_lang = "ru"
            list(self.whisper.transcribe(probe, language=_probe_lang, beam_size=1, vad_filter=False))
        except Exception as e:
            set_status(f"GPU 识别不可用({e})，回退 CPU ...")
            self.whisper = WhisperModel(WHISPER_SIZE, device="cpu", compute_type="int8",
                                        download_root=os.path.join(BASE_DIR, "models_whisper"))

        # 当前 faster-whisper 版本支持哪些转写参数（用于安全传入防幻觉阈值）
        import inspect as _inspect
        try:
            _ASR_SUPPORTED = set(_inspect.signature(self.whisper.transcribe).parameters)
        except Exception:
            _ASR_SUPPORTED = set()

        nllb_dev = NLLB_DEVICE or ("cuda" if self._torch_has_cuda() else "cpu")
        set_status(f"加载翻译模型 NLLB on {nllb_dev} ...")
        # NLLB 需要 FLORES-200 代码（如 rus_Cyrl），不是 whisper 的 "ru"
        init_src = nllb_code_for(lang_state["whisper"]) or "rus_Cyrl"
        self.translator = pipeline("translation", model=NLLB_MODEL,
                                   device=0 if nllb_dev == "cuda" else -1,
                                   src_lang=init_src, tgt_lang=TGT_LANG)
        self.ready.set()
        if capture_state.get("active"):
            set_status(f"模型就绪 ✓ 监听中：{capture_state.get('device','')}")
        else:
            set_status("模型就绪 ✓ 点「开始监听」即可")

        def _degenerate(s):
            """判断是否为退化输出(大量重复字符/标点)——NLLB 在短句或噪声上偶发。"""
            s = (s or "").strip()
            if len(s) < 4:
                return False
            from collections import Counter
            top = Counter(s).most_common(1)[0][1]
            if top / len(s) > 0.45:
                return True
            punct = sum(1 for ch in s if ch in ",.、。，！？!?…-—;；:：\"'")
            return punct / len(s) > 0.5

        def _cur_src_code():
            """当前源语言(whisper 代码)：固定选择，或自动检测到的语种。"""
            w = lang_state.get("whisper")
            if w == "auto":
                return lang_state.get("detected") or None
            return w

        def translate(txt):
            if not txt.strip():
                return ""
            src_code = _cur_src_code()
            # —— 后端选择：LLM（泛用性更好）优先，失败自动回退本地 NLLB ——
            if backend_state["kind"] == "llm":
                cfg = dict(load_ai_config())
                p = cfg.get("translate_prompt", DEFAULT_TR_PROMPT)
                p = (p.replace("{lang}", lang_name_for(src_code))
                      .replace("{context}", build_context_text(3) or "（无）"))
                if "{text}" not in p:
                    p = p + "\n\n{text}"
                cfg["translate_prompt"] = p
                zh = llm_translate(txt, cfg)
                if zh:
                    return zh
            # —— 本地 NLLB：按当前源语言动态设置 tokenizer.src_lang ——
            code = nllb_code_for(src_code)
            if code:
                try:
                    self.translator.tokenizer.src_lang = code
                except Exception:
                    pass
            try:
                out = self.translator(txt, max_length=512,
                                      no_repeat_ngram_size=3, repetition_penalty=1.15)
                zh = out[0]["translation_text"]
            except Exception as e:
                print(f"[translate error] {e}", file=sys.stderr)
                return ""
            if _degenerate(zh):          # 退化 -> 换参数重试一次
                try:
                    out2 = self.translator(txt, max_length=512, num_beams=4,
                                           repetition_penalty=1.4, no_repeat_ngram_size=3)
                    zh2 = out2[0]["translation_text"]
                    if not _degenerate(zh2):
                        zh = zh2
                except Exception:
                    pass
            return zh

        ANTI_HALLUC = dict(no_speech_threshold=0.6, log_prob_threshold=-1.0,
                           compression_ratio_threshold=2.4,
                           hallucination_silence_threshold=2.0, temperature=0.0)

        def _asr_units(audio, beam):
            """转写并切成"句子"单元（利用 Whisper 的 segment 边界 + 句末标点）。
            返回 [句子1, 句子2, ...]；这避免把整段拼成一大坨，也让每句能立即翻译。"""
            wl = lang_state.get("whisper")
            kw = dict(language=(None if wl == "auto" else wl), beam_size=beam,
                      vad_filter=False, condition_on_previous_text=WHISPER_CONDITION)
            kw.update(ANTI_HALLUC)
            kw = {k: v for k, v in kw.items() if (not _ASR_SUPPORTED) or (k in _ASR_SUPPORTED)}
            segments, info = self.whisper.transcribe(audio, **kw)
            if wl == "auto" and getattr(info, "language", None):
                if lang_state.get("detected") != info.language:
                    lang_state["detected"] = info.language
                    set_status(f"检测到语言：{lang_name_for(info.language)}（{info.language}）")
            units, buf = [], ""
            for s in segments:
                t = strip_hallucinations((s.text or "").strip())
                if not t:
                    continue
                buf = (buf + " " + t).strip() if buf else t
                if buf.endswith(_SENT_END) and len(buf) >= MIN_UNIT_CHARS:
                    units.append(buf)
                    buf = ""
            if buf:
                units.append(buf)
            # 小写/数字开头的片段通常是上一句的延续（whisper 偶会把长句从中间断开），并回上句
            merged = []
            for u in units:
                if merged and u and (u[0].islower() or u[0].isdigit()):
                    merged[-1] = (merged[-1] + " " + u).strip()
                else:
                    merged.append(u)
            return merged

        def flush_pending(reason="", mid_utterance=False):
            if not self.pending:
                return
            ru = " ".join(self.pending)
            self.pending, self.pending_chars, self.pending_since = [], 0, None
            # 过长则分块翻译再拼接（防止译文重复/退化）
            if len(ru) > 400:
                zh = " ".join(x for x in (translate(p) for p in split_for_translation(ru, 400)) if x)
            else:
                zh = translate(ru)
            print(f"\nRU  {ru}\nZH  {zh}    [{reason}]\n", flush=True)
            clear_partial()
            if mid_utterance:
                force_split.set()      # 半句强翻：让 VAD 立刻断开，"识别中"重新开始
            push_result(ru, zh)

        while True:
            worked = False

            # ① 优先："说完的完整片段" -> 最终转写（防幻觉）
            try:
                audio = speech_queue.get_nowait()
            except queue.Empty:
                audio = None
            if audio is not None:
                worked = True
                try:
                    units = _asr_units(audio, WHISPER_BEAM)
                except Exception as e:
                    print(f"[asr error] {e}", file=sys.stderr)
                    units = []
                for text in units:
                    if not self.pending:
                        self.pending_since = time.time()
                    self.pending.append(text)
                    self.pending_chars += len(text)
                    joined = " ".join(self.pending)
                    if is_complete_sentence(joined):                 # 句末标点 => 立即翻
                        flush_pending("完整句")
                    elif self.pending_chars >= MAX_CHARS:            # 超长安全阀
                        flush_pending("超长", mid_utterance=True)
                        break
                self.last_final = time.time()

            # ② 其次：实时预览（只取最新一张快照，不翻译）
            if not worked:
                snap = None
                while True:
                    try:
                        snap = partial_audio_queue.get_nowait()
                    except queue.Empty:
                        break
                if isinstance(snap, np.ndarray):
                    worked = True
                    try:
                        units = _asr_units(snap, 1)
                        if units:
                            push_partial(" ".join(units))
                    except Exception as e:
                        print(f"[partial error] {e}", file=sys.stderr)

            # ③ 兜底（每轮都检查，不能只在有新片段时检查）：
            #    讲师连续讲话时 VAD 很久不切句，否则译文会一直不更新
            if self.pending and self.pending_since and \
               (time.time() - self.pending_since) >= HARD_FLUSH_SEC:
                flush_pending("兜底超时", mid_utterance=True)
            elif self.pending and self.last_final and \
                 (time.time() - self.last_final) >= FLUSH_IDLE:
                flush_pending("停顿")

            if not worked:
                time.sleep(0.08)

# --------------------------------------------------------------------------- #
# AI 自动总结（API）
# --------------------------------------------------------------------------- #
def load_ai_config():
    cfg = {"api_base": "https://api.deepseek.com/v1", "api_key": "", "model": "deepseek-chat",
           "system_prompt": "你是专业的直播内容摘要助手，输出简体中文。",
           "prompt": "以下是俄语直播的实时中文翻译记录（按时间顺序）。请用简体中文总结：\n"
                     "1) 主要话题/发生了什么；2) 关键要点（分条）；3) 若有结论或结果请写明。\n"
                     "要求简洁、分点、不要逐句复述。\n\n记录：\n{text}"}
    try:
        if os.path.exists(AI_CONFIG_PATH):
            with open(AI_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception as e:
        print(f"[ai config] {e}", file=sys.stderr)
    cfg["api_key"] = env("SUMMARY_API_KEY", cfg.get("api_key", ""))
    cfg["api_base"] = env("SUMMARY_API_BASE", cfg.get("api_base", ""))
    cfg["model"] = env("SUMMARY_MODEL", cfg.get("model", ""))
    return cfg

def ai_summarize(text, cfg, timeout=60):
    """调用 OpenAI 兼容接口做总结。返回 (ok, 结果或错误)。"""
    import requests
    if not cfg.get("api_key"):
        return False, "未配置 API Key（请编辑 ai_config.json 的 api_key）"
    url = cfg["api_base"].rstrip("/") + "/chat/completions"
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": cfg.get("system_prompt", "")},
            {"role": "user", "content": cfg.get("prompt", "{text}").replace("{text}", text)},
        ],
        "temperature": 0.3,
        "stream": False,
    }
    try:
        r = requests.post(url, headers={"Authorization": f"Bearer {cfg['api_key']}",
                                        "Content-Type": "application/json"},
                          json=body, timeout=timeout)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:300]}"
        data = r.json()
        return True, data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return False, f"请求失败：{e}"

def recent_text(minutes=10, max_chars=12000):
    with lines_lock:
        items = list(transcript)
    cutoff = time.time() - minutes * 60
    sel = [it for it in items if it["t"].timestamp() >= cutoff]
    if not sel:
        sel = items[-200:]
    s = "\n".join(f"[{it['t'].strftime('%H:%M:%S')}] {it['zh']}" for it in sel)
    return s[-max_chars:]

# --------------------------------------------------------------------------- #
# LLM 翻译（泛用性更好，尤其语言教学 / 口语 / 术语多的内容）
# --------------------------------------------------------------------------- #
DEFAULT_TR_SYS = "你是专业、严谨的多语种→简体中文翻译引擎。"
DEFAULT_TR_PROMPT = (
    "把下面的{lang}翻译成简体中文。要求：\n"
    "1) 忠实、完整地翻译，不要省略、不要概括，也不要把内容“润色掉”；\n"
    "2) 若是语言教学场景：字母名称、音节、示范发音、语法术语要如实保留（可加极简短说明）；\n"
    "3) 人名/专有名词/游戏术语用中文常见译法；\n"
    "4) 术语与人称要与【上下文】里的译法保持一致（例如同一概念不要一会儿译成A一会儿译成B）；\n"
    "5) 只输出译文本身，不要解释、不要加引号。\n\n"
    "【上下文】\n{context}\n\n"
    "原文：\n{text}"
)

backend_state = {"kind": TRANSLATE_BACKEND}      # "nllb" | "llm"

def llm_translate(text, cfg, timeout=30):
    """用 OpenAI 兼容 API 翻译；失败返回 None（由调用方回退本地 NLLB）。"""
    import requests
    if not cfg.get("api_key"):
        return None
    try:
        url = cfg["api_base"].rstrip("/") + "/chat/completions"
        body = {"model": cfg["model"],
                "messages": [
                    {"role": "system", "content": cfg.get("translate_system", DEFAULT_TR_SYS)},
                    {"role": "user",
                     "content": cfg.get("translate_prompt", DEFAULT_TR_PROMPT).replace("{text}", text)},
                ],
                "temperature": 0.2, "stream": False}
        r = requests.post(url, headers={"Authorization": f"Bearer {cfg['api_key']}",
                                        "Content-Type": "application/json"},
                          json=body, timeout=timeout)
        if r.status_code != 200:
            print(f"[llm translate] HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return None
        out = (r.json()["choices"][0]["message"]["content"] or "").strip()
        return out or None
    except Exception as e:
        print(f"[llm translate] {e}", file=sys.stderr)
        return None

# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
def _srt_ts(sec):
    h = int(sec // 3600); m = int(sec % 3600 // 60); s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

def export_transcript(path=None):
    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    with lines_lock:
        items = list(transcript)
    if not items:
        return None
    if not path:
        path = os.path.join(TRANSCRIPT_DIR, f"transcript_{datetime.now():%Y%m%d_%H%M%S}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("俄语直播实时翻译记录\n")
        f.write(f"导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write(f"共 {len(items)} 段\n")
        f.write("=" * 60 + "\n\n")
        for it in items:
            f.write(f"[{it['t']:%H:%M:%S}]\nRU  {it['ru']}\nZH  {it['zh']}\n\n")
    # 同时导出一份 SRT 字幕
    srt = os.path.splitext(path)[0] + ".srt"
    t0 = items[0]["t"]
    with open(srt, "w", encoding="utf-8") as f:
        for n, it in enumerate(items, 1):
            s = (it["t"] - t0).total_seconds()
            f.write(f"{n}\n{_srt_ts(s)} --> {_srt_ts(s + 3.0)}\n{it['zh']}\n\n")
    return path

# --------------------------------------------------------------------------- #
# OBS 叠加层
# --------------------------------------------------------------------------- #
OVERLAY_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>body{margin:0;background:rgba(0,0,0,.55);color:#fff;font-family:'Microsoft YaHei',sans-serif;padding:14px}
#log{display:flex;flex-direction:column;gap:8px}.line{padding:8px 10px;border-radius:8px;background:rgba(20,20,30,.72)}
.ru{font-size:15px;opacity:.72;margin-bottom:3px}.zh{font-size:22px;font-weight:700;line-height:1.35}
.live{font-size:15px;color:#ffd479;opacity:.95}</style>
</head><body><div id="log"></div><div id="live" class="live"></div><script>
async function tick(){try{const r=await fetch('/data');const j=await r.json();const log=document.getElementById('log');
log.innerHTML='';j.lines.slice(-6).forEach(l=>{const d=document.createElement('div');d.className='line';
const ru=document.createElement('div');ru.className='ru';ru.textContent=l.ru;
const zh=document.createElement('div');zh.className='zh';zh.textContent=l.zh;d.append(ru);d.append(zh);log.append(d);});
document.getElementById('live').textContent = j.partial ? ('▍'+j.partial) : '';}catch(e){}
setTimeout(tick,500);}tick();</script></body></html>"""

class OverlayHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = OVERLAY_HTML.encode("utf-8"); ctype = "text/html; charset=utf-8"; self.send_response(200)
        elif self.path == "/data":
            with lines_lock:
                payload = json.dumps({"lines": [{"ru": it["ru"], "zh": it["zh"]} for it in transcript[-20:]],
                                      "partial": partial_text()}, ensure_ascii=False)
            body = payload.encode("utf-8"); ctype = "application/json; charset=utf-8"; self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
        else:
            self.send_response(404); body = b""; ctype = "text/plain"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass
    def log_message(self, *a):
        pass

OVERLAY_PORT = APP_PORT
def start_http_server():
    global OVERLAY_PORT
    for p in range(APP_PORT, APP_PORT + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), OverlayHandler)
            OVERLAY_PORT = p
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            print(f"[+] OBS 叠加层: http://127.0.0.1:{OVERLAY_PORT}/", flush=True)
            return
        except OSError:
            continue
    print("[!] 端口被占用, 叠加层未启动(不影响窗口)", file=sys.stderr)

# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #
def run_gui(worker, autostart=False):
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("RU→中文 实时翻译 · 控制面板")
    root.geometry("660x780+30+20")
    root.minsize(560, 620)

    st = load_ui_state()
    if not st.get("dpi_aware"):          # 旧版(不感知DPI)存的值会错位/偏小，丢弃位置与尺寸字号
        for k in ("x", "y", "font", "w", "h"):
            st.pop(k, None)
    sc = _UI_SCALE      # 已按真实 DPI（125% -> 1.25）计算
    backend_state["kind"] = st.get("backend", TRANSLATE_BACKEND)
    lang_state["whisper"] = st.get("lang", lang_state.get("whisper", "ru"))
    session_holder = {"s": None}
    auto_var = tk.BooleanVar(value=False)
    auto_state = {"last": 0.0}

    # 字幕小窗选项（Live Captions 风格；从 ui_state.json 恢复）
    opt_font = tk.IntVar(value=int(st.get("font", int(round(22 * sc)))))
    opt_alpha = tk.DoubleVar(value=float(st.get("alpha", 1.0)))
    opt_lines = tk.IntVar(value=int(st.get("lines", 2)))
    opt_top = tk.BooleanVar(value=bool(st.get("topmost", True)))
    opt_borderless = tk.BooleanVar(value=bool(st.get("borderless", True)))
    opt_show = tk.BooleanVar(value=bool(st.get("show", True)))
    opt_bgtrans = tk.BooleanVar(value=bool(st.get("bg_transparent", True)))
    opt_autoheight = tk.BooleanVar(value=bool(st.get("autoheight", True)))
    opt_pos = tk.StringVar(value=str(st.get("pos", "bottom")))
    opt_w = tk.IntVar(value=int(st.get("w", int(1000 * sc))))
    opt_h = tk.IntVar(value=int(st.get("h", int(170 * sc))))
    sub = {"win": None, "canvas": None, "data": []}    # data: [(ru, zh), ...]

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))

    # ======================= Tab 1: 控制 =======================
    t1 = ttk.Frame(nb, padding=10)
    nb.add(t1, text="控制")

    lf_dev = ttk.LabelFrame(t1, text="1) 监听目标（旁路监听，你自己的外放不受影响）", padding=8)
    lf_dev.pack(fill="x", pady=(0, 8))
    dev_combo = ttk.Combobox(lf_dev, state="readonly")
    dev_combo.pack(side="left", fill="x", expand=True)
    dev_items = []

    def refresh_devices():
        nonlocal dev_items
        dev_items = list_loopback_devices()          # [(name, label), ...]
        dev_combo["values"] = [lbl for _, lbl in dev_items]
        if dev_items:
            pick = auto_pick_loopback()
            idx = next((n for n, it in enumerate(dev_items) if it[0] == pick), 0)
            dev_combo.current(idx)

    ttk.Button(lf_dev, text="刷新", width=6, command=refresh_devices).pack(side="left", padx=(6, 0))

    lang_row = ttk.Frame(t1)
    lang_row.pack(fill="x", pady=(0, 8))
    ttk.Label(lang_row, text="识别语言：").pack(side="left")
    _lang_names = [n for (n, w, c) in LANGUAGES]
    lang_combo = ttk.Combobox(lang_row, values=_lang_names, state="readonly", width=12)
    lang_combo.pack(side="left", padx=4)
    _cur = lang_state.get("whisper", "ru")
    lang_combo.current(next((i for i, it in enumerate(LANGUAGES) if it[1] == _cur), 0))

    def on_lang_change(*_a):
        n, w, _c = LANGUAGES[lang_combo.current()]
        lang_state["whisper"] = w
        lang_state["detected"] = None
        set_status(f"识别语言已切换：{n}" + ("（自动检测）" if w == "auto" else ""))
    lang_combo.bind("<<ComboboxSelected>>", on_lang_change)
    ttk.Label(lang_row, text="（选「自动检测」时由 Whisper 自行判断语种）",
              foreground="#666").pack(side="left")

    row_btn = ttk.Frame(t1)
    row_btn.pack(fill="x", pady=(0, 8))
    btn_start = ttk.Button(row_btn, text="▶ 开始监听")
    btn_start.pack(side="left")
    btn_stop = ttk.Button(row_btn, text="■ 停止", state="disabled")
    btn_stop.pack(side="left", padx=6)
    ttk.Label(row_btn, text="（提示：只监听某个应用 → Windows 音量合成器里把该应用输出改到某设备，再选该设备）",
              foreground="#666").pack(side="left", padx=6)

    # 实时"识别中"原文（说多少显示多少；完整后才出译文）
    live_var = tk.StringVar(value="")
    ttk.Label(t1, textvariable=live_var, foreground="#b8860b", wraplength=620,
              justify="left", anchor="w").pack(fill="x", pady=(0, 8))

    # ---- 字幕小窗 ----
    lf_sub = ttk.LabelFrame(t1, text="2) 字幕小窗（Live Captions 风格 · 置顶显示原文+翻译）", padding=8)
    lf_sub.pack(fill="x", pady=(0, 8))

    r0 = ttk.Frame(lf_sub); r0.grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Checkbutton(r0, text="显示字幕小窗", variable=opt_show).pack(side="left")
    ttk.Checkbutton(r0, text="置顶", variable=opt_top).pack(side="left", padx=8)
    ttk.Checkbutton(r0, text="无边框", variable=opt_borderless).pack(side="left", padx=8)

    r1 = ttk.Frame(lf_sub); r1.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
    ttk.Checkbutton(r1, text="背景全透明（只留文字，可鼠标穿透）", variable=opt_bgtrans).pack(side="left")
    ttk.Checkbutton(r1, text="自动高度", variable=opt_autoheight).pack(side="left", padx=10)

    ttk.Label(lf_sub, text="字号").grid(row=2, column=0, sticky="w", pady=(8, 0))
    tk.Scale(lf_sub, from_=12, to=56, orient="horizontal", variable=opt_font,
             length=200, showvalue=True).grid(row=2, column=1, sticky="w", columnspan=2)

    ttk.Label(lf_sub, text="文字不透明度").grid(row=3, column=0, sticky="w")
    tk.Scale(lf_sub, from_=0.2, to=1.0, resolution=0.05, orient="horizontal",
             variable=opt_alpha, length=200, showvalue=True).grid(row=3, column=1, sticky="w", columnspan=2)

    ttk.Label(lf_sub, text="显示行数").grid(row=4, column=0, sticky="w")
    tk.Spinbox(lf_sub, from_=1, to=8, width=5, textvariable=opt_lines).grid(row=4, column=1, sticky="w")

    ttk.Label(lf_sub, text="位置").grid(row=5, column=0, sticky="w", pady=(6, 0))
    rp = ttk.Frame(lf_sub); rp.grid(row=5, column=1, columnspan=2, sticky="w", pady=(6, 0))
    for _txt, _val in (("顶部居中", "top"), ("底部居中", "bottom"),
                       ("屏幕居中", "center"), ("自定义(拖动)", "custom")):
        ttk.Radiobutton(rp, text=_txt, value=_val, variable=opt_pos).pack(side="left", padx=2)

    ttk.Label(lf_sub, text="宽 / 高(px)").grid(row=6, column=0, sticky="w", pady=(6, 0))
    rw = ttk.Frame(lf_sub); rw.grid(row=6, column=1, columnspan=2, sticky="w", pady=(6, 0))
    tk.Spinbox(rw, from_=320, to=3840, increment=20, width=7, textvariable=opt_w).pack(side="left")
    tk.Spinbox(rw, from_=60, to=1200, increment=10, width=7, textvariable=opt_h).pack(side="left", padx=6)
    ttk.Label(rw, text="（勾选“自动高度”时高度自动）", foreground="#666").pack(side="left")

    # ---- 功能 ----
    lf_fn = ttk.LabelFrame(t1, text="3) 功能", padding=8)
    lf_fn.pack(fill="x", pady=(0, 8))

    def do_export():
        p = export_transcript()
        if p:
            set_status(f"已导出：{p}")
            messagebox.showinfo("导出成功", f"已保存：\n{p}\n(同名 .srt 字幕也已生成)")
        else:
            messagebox.showwarning("提示", "还没有任何翻译内容可导出")

    def do_summary():
        text = recent_text(minutes=15)
        if not text.strip():
            messagebox.showwarning("提示", "还没有内容可总结"); return
        set_status("AI 正在总结 …")
        cfg = load_ai_config()
        def work():
            ok, res = ai_summarize(text, cfg)
            def apply():
                sum_box.config(state="normal")
                sum_box.delete("1.0", "end")
                sum_box.insert("end", res if ok else f"[总结失败] {res}")
                sum_box.config(state="disabled")
                nb.select(2)
                set_status("总结完成 ✓" if ok else "总结失败")
            root.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    row_fn = ttk.Frame(lf_fn); row_fn.pack(fill="x")
    ttk.Button(row_fn, text="导出翻译文本", command=do_export).pack(side="left")
    ttk.Button(row_fn, text="AI 总结", command=do_summary).pack(side="left", padx=6)
    ttk.Checkbutton(row_fn, text="自动总结（每", variable=auto_var).pack(side="left", padx=(10, 0))
    auto_min = tk.IntVar(value=3)
    tk.Spinbox(row_fn, from_=1, to=30, width=4, textvariable=auto_min).pack(side="left")
    ttk.Label(row_fn, text="分钟）").pack(side="left")

    tr_row = ttk.Frame(lf_fn); tr_row.pack(fill="x", pady=(8, 0))
    ttk.Label(tr_row, text="翻译后端：").pack(side="left")
    tr_var = tk.StringVar(value=backend_state["kind"])
    ttk.Radiobutton(tr_row, text="本地 NLLB（离线）", value="nllb",
                    variable=tr_var).pack(side="left", padx=4)
    ttk.Radiobutton(tr_row, text="API 大模型（泛用性更好，需 ai_config.json 的 Key）",
                    value="llm", variable=tr_var).pack(side="left", padx=4)
    tr_var.trace_add("write", lambda *a: backend_state.__setitem__("kind", tr_var.get()))

    obs_row = ttk.Frame(lf_fn); obs_row.pack(fill="x", pady=(8, 0))
    ttk.Label(obs_row, text="OBS 浏览器源地址：").pack(side="left")
    obs_var = tk.StringVar(value=f"http://127.0.0.1:{OVERLAY_PORT}/")
    ttk.Entry(obs_row, textvariable=obs_var, width=28, state="readonly").pack(side="left")
    def copy_obs():
        root.clipboard_clear(); root.clipboard_append(obs_var.get())
        set_status("已复制 OBS 地址到剪贴板")
    ttk.Button(obs_row, text="复制", width=6, command=copy_obs).pack(side="left", padx=6)

    # ======================= Tab 2: 字幕记录 =======================
    t2 = ttk.Frame(nb, padding=6)
    nb.add(t2, text="字幕记录")
    txt = tk.Text(t2, wrap="word", bg="#111", fg="#eee", font=("Microsoft YaHei", 12))
    txt.pack(side="left", fill="both", expand=True)
    sb = ttk.Scrollbar(t2, command=txt.yview); sb.pack(side="right", fill="y")
    txt.config(yscrollcommand=sb.set)
    txt.tag_configure("ru", foreground="#9fd0ff", font=("Microsoft YaHei", 11))
    txt.tag_configure("zh", foreground="#ffffff", font=("Microsoft YaHei", 15, "bold"))

    # ======================= Tab 3: AI 总结 =======================
    t3 = ttk.Frame(nb, padding=6)
    nb.add(t3, text="AI 总结")
    sum_box = tk.Text(t3, wrap="word", bg="#1b1b25", fg="#eaeaea", font=("Microsoft YaHei", 11))
    sum_box.pack(fill="both", expand=True)
    sum_box.insert("end", "点「AI 总结」生成；勾选「自动总结」则定期刷新。\n（需先在 ai_config.json 填 api_key）")
    sum_box.config(state="disabled")

    # ======================= 字幕小窗（Live Captions 风格） =======================
    def _sub_size():
        import tkinter.font as tkfont
        fs = int(opt_font.get())
        h_zh = tkfont.Font(family="Microsoft YaHei", size=fs, weight="bold").metrics("linespace")
        h_ru = tkfont.Font(family="Microsoft YaHei", size=max(10, fs - 7)).metrics("linespace")
        # 固定为"识别中"预留 2 行高度（避免长原文溢出窗口、也避免抖动）
        h = int(opt_lines.get()) * (h_zh + h_ru + 6) + 2 * (h_ru + 6) + 18
        if not opt_autoheight.get():
            h = int(opt_h.get())
        w = int(opt_w.get())
        return max(320, w), max(60, h)

    def _sub_xy(w, h):
        sw, sh = _UI_SCREEN
        if not sw:
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        p = opt_pos.get()
        if p == "top":
            x, y = (sw - w) // 2, int(sh * 0.05)
        elif p == "center":
            x, y = (sw - w) // 2, (sh - h) // 2
        elif p == "bottom":
            x, y = (sw - w) // 2, max(0, sh - h - int(sh * 0.14))
        else:
            x, y = int(st.get("x", (sw - w) // 2)), int(st.get("y", int(sh * 0.75)))
        # 夹紧到屏幕内：改字号/行数后高度会变，避免窗口跑出屏幕（尤其底部）
        x = max(0, min(x, max(0, sw - w)))
        y = max(0, min(y, max(0, sh - h)))
        return x, y

    def _draw_outlined(c, x, y, text, font, fill, width):
        """画带黑色描边的文字（这样即使背景全透明，压在任何画面上也能看清）。返回本行高度。"""
        off = max(1, int(font[1] // 11))
        for dx, dy in ((-off, 0), (off, 0), (0, -off), (0, off),
                       (-off, -off), (-off, off), (off, -off), (off, off)):
            c.create_text(x + dx, y + dy, text=text, font=font, fill="#000000",
                          anchor="nw", width=width)
        item = c.create_text(x, y, text=text, font=font, fill=fill, anchor="nw", width=width)
        bb = c.bbox(item)
        return (bb[3] - bb[1] + 4) if bb else int(font[1] * 1.5)

    def render_sub():
        c = sub.get("canvas")
        if not c:
            return
        c.delete("all")
        fs = int(opt_font.get())
        n = int(opt_lines.get())
        inner_w = max(120, _sub_size()[0] - 28)
        f_ru = ("Microsoft YaHei", max(10, fs - 7))
        f_zh = ("Microsoft YaHei", fs, "bold")
        y = 6
        for ru, zh in sub["data"][-n:]:
            y += _draw_outlined(c, 14, y, ru, f_ru, "#bfe3ff", inner_w)
            y += _draw_outlined(c, 14, y, zh, f_zh, "#ffffff", inner_w)
        # 底部：正在识别的原文（只显示最近一段，避免撑爆窗口）
        ptxt = partial_text().strip()
        if ptxt:
            if len(ptxt) > PARTIAL_DISPLAY_CHARS:
                ptxt = "…" + ptxt[-PARTIAL_DISPLAY_CHARS:]
            _draw_outlined(c, 14, y, "▍" + ptxt, f_ru, "#ffd479", inner_w)

    def apply_sub_options():
        win, c = sub.get("win"), sub.get("canvas")
        if not win:
            return
        try:
            win.attributes("-topmost", bool(opt_top.get()))
            win.overrideredirect(bool(opt_borderless.get()))
        except Exception:
            pass
        # 背景透明：让"背景"完全消失、文字保持不透明；透明区域同时会鼠标穿透
        try:
            if opt_bgtrans.get():
                win.configure(bg=SUB_TCOLOR); c.configure(bg=SUB_TCOLOR)
                win.attributes("-transparentcolor", SUB_TCOLOR)
            else:
                win.configure(bg="#000000"); c.configure(bg="#000000")
                win.attributes("-transparentcolor", "")
        except Exception:
            pass
        try:
            win.attributes("-alpha", float(opt_alpha.get()))
        except Exception:
            pass
        w, h = _sub_size(); x, y = _sub_xy(w, h)
        win.geometry(f"{w}x{h}+{x}+{y}")
        render_sub()

    def build_sub_window():
        win = tk.Toplevel(root)
        win.title("字幕")
        c = tk.Canvas(win, bd=0, highlightthickness=0, takefocus=0)
        c.pack(fill="both", expand=True)
        sub["win"], sub["canvas"] = win, c

        drag = {}
        def press(e): drag["x"], drag["y"] = e.x, e.y
        def move(e):
            x = win.winfo_x() + e.x - drag["x"]; y = win.winfo_y() + e.y - drag["y"]
            win.geometry(f"+{x}+{y}")
            st["x"], st["y"] = x, y
            if opt_pos.get() != "custom":
                opt_pos.set("custom")
        for w in (win, c):
            w.bind("<Button-1>", press); w.bind("<B1-Motion>", move)

        def on_close():
            opt_show.set(False); win.withdraw(); save_state()
        win.protocol("WM_DELETE_WINDOW", on_close)
        apply_sub_options()

    def toggle_sub():
        win = sub["win"]
        if not win:
            return
        if opt_show.get():
            win.deiconify(); win.lift()
        else:
            win.withdraw()

    def save_state():
        save_ui_state({
            "font": int(opt_font.get()), "alpha": float(opt_alpha.get()),
            "lines": int(opt_lines.get()), "topmost": bool(opt_top.get()),
            "borderless": bool(opt_borderless.get()), "show": bool(opt_show.get()),
            "bg_transparent": bool(opt_bgtrans.get()), "autoheight": bool(opt_autoheight.get()),
            "pos": opt_pos.get(), "w": int(opt_w.get()), "h": int(opt_h.get()),
            "x": st.get("x"), "y": st.get("y"),
            "dpi_aware": True, "backend": backend_state["kind"],
            "lang": lang_state.get("whisper", "ru"),
        })

    def on_quit():
        save_state()
        try:
            root.destroy()
        except Exception:
            pass
    root.protocol("WM_DELETE_WINDOW", on_quit)

    for _v in (opt_font, opt_lines, opt_alpha, opt_top, opt_borderless,
               opt_bgtrans, opt_autoheight, opt_pos, opt_w, opt_h):
        _v.trace_add("write", lambda *a: apply_sub_options())
    opt_show.trace_add("write", lambda *a: toggle_sub())

    # ======================= 监听控制 =======================
    def start_capture():
        if session_holder["s"]:
            return
        if not dev_items:
            messagebox.showwarning("提示", "没有可用设备，请点「刷新」")
            return
        dev_name = dev_items[dev_combo.current()][0]
        s = CaptureSession(dev_name)
        s.start()
        session_holder["s"] = s
        btn_start.config(state="disabled"); btn_stop.config(state="normal")
        if opt_show.get() and not sub["win"]:
            build_sub_window()

    def stop_capture():
        s = session_holder["s"]
        if s:
            s.stop(); session_holder["s"] = None
        btn_start.config(state="normal"); btn_stop.config(state="disabled")
        set_status("已停止监听")

    btn_start.config(command=start_capture)
    btn_stop.config(command=stop_capture)

    # ======================= 状态栏 =======================
    status_var = tk.StringVar(value="启动中 …")
    ttk.Label(root, textvariable=status_var, relief="sunken", anchor="w").pack(fill="x", side="bottom")

    global status_cb
    def _cb(msg):
        root.after(0, lambda: status_var.set(msg))
    status_cb = _cb

    def poll_results():
        # 任何异常都不允许中断这个轮询循环（否则界面会"罢工"）
        try:
            got_final = False
            while True:
                try:
                    ru, zh, ts = result_queue.get_nowait()
                except queue.Empty:
                    break
                sub["data"].append((ru, zh))
                txt.insert("end", f"[{ts:%H:%M:%S}] RU  {ru}\n", ("ru",))
                txt.insert("end", f"          ZH  {zh}\n", ("zh",))
                txt.see("end")
                got_final = True
            # 实时预览原文（worker -> 界面，只收字符串）
            got_partial = False
            while True:
                try:
                    p = partial_text_queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(p, str):
                    partial_state["text"] = p
                    got_partial = True
            ptxt = partial_text()
            if len(ptxt) > PARTIAL_DISPLAY_CHARS:      # 只显示最近一段，避免面板被撑高
                ptxt = "…" + ptxt[-PARTIAL_DISPLAY_CHARS:]
            live_var.set(("识别中： " + ptxt) if ptxt else "")
            if got_final or got_partial:
                render_sub()
            if auto_var.get() and time.time() - auto_state["last"] > auto_min.get() * 60:
                auto_state["last"] = time.time()
                with lines_lock:
                    has = bool(transcript)
                if has:
                    do_summary()
        except Exception as e:
            print(f"[ui poll error] {e}", file=sys.stderr)
        finally:
            root.after(300, poll_results)

    refresh_devices()
    if opt_show.get():
        build_sub_window()
    print(f"[ui] pid={os.getpid()} sc={sc} screen={_UI_SCREEN} w={opt_w.get()} h={opt_h.get()} "
          f"font={opt_font.get()} lines={opt_lines.get()} pos={opt_pos.get()} "
          f"size={_sub_size()} xy={_sub_xy(*_sub_size())}", flush=True)
    root.after(300, poll_results)
    if autostart:
        root.after(600, start_capture)
    root.mainloop()

# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-devices", action="store_true", help="列出可旁路监听的输出设备")
    ap.add_argument("--no-gui", action="store_true", help="纯无窗口模式(只提供 OBS 叠加层)")
    ap.add_argument("--autostart", action="store_true", help="启动后自动开始监听默认扬声器")
    args = ap.parse_args()

    if args.list_devices:
        print("== 可旁路监听的输出设备（WASAPI loopback，不影响外放） ==")
        for name, lbl in list_loopback_devices():
            print("  ", lbl)
        return

    os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
    print(f"[dpi] aware={_DPI_READY} scale={_UI_SCALE:.2f} screen={_UI_SCREEN}", flush=True)
    worker = ASRTranslateWorker()
    worker.start()
    start_http_server()

    if args.no_gui:
        dev = INPUT_DEVICE or auto_pick_loopback()
        if dev is None:
            print("[!] 未找到可旁路监听的设备", file=sys.stderr)
        else:
            CaptureSession(dev).start()
            print(f"[+] 已开始监听：{dev}", flush=True)
            print(f"[+] OBS 源 http://127.0.0.1:{OVERLAY_PORT}/", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n已停止。")
    else:
        run_gui(worker, autostart=args.autostart)

if __name__ == "__main__":
    main()
