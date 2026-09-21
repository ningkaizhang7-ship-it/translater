# live_ru2zh —— 实时 多语种 → 简体中文 直播翻译（旁路监听版）

把你正在看的**直播声音**（俄语 / 英语 / 日语 …）实时转写为原文并翻译成**简体中文**，显示在置顶字幕小窗里（也可作为 OBS 叠加层）。

**最大特点：像 oCam / OBS 那样「旁路监听」——你自己的外放完全不受影响，声音照常从耳机/音箱播出。**

---

## 功能一览

| 功能 | 说明 |
|---|---|
| ✅ 旁路监听（不影响外放） | 基于 **WASAPI loopback**，直接"旁听"某个输出设备，无需把系统默认设备改成虚拟声卡 |
| ✅ 可选监听目标 | 控制面板下拉选择要旁听的输出设备（耳机/扬声器/NVIDIA/VB-CABLE…），默认 = **当前默认扬声器** |
| ✅ 只监听某个应用 | Windows「设置 → 声音 → 音量合成器」把该应用输出改到某设备，再在面板选该设备的旁路即可 |
| ✅ 双窗口界面 | ① **控制面板窗**（设置与功能集成）；② **字幕小窗**（Live Captions 风格：置顶、可拖动、背景全透明、文字描边） |
| ✅ 多语言识别 | 识别语言可选 **19 种 + 自动检测**，翻译仍输出简体中文 |
| ✅ 原文实时、译文按句 | 说话中**原文边说边出**（金色 `▍`）；**一句说完才翻译**并定格 |
| ✅ 前后连贯（术语一致） | LLM 翻译带**上下文窗口**（最近 3 句），同一术语不再一会儿 A 一会儿 B |
| ✅ 防"臆想" | 抑制 Whisper 幻觉（"请点赞订阅"之类） |
| ✅ 翻译文本导出 | 一键导出 `.txt`（时间戳+双语）与 `.srt` 字幕到 `transcripts/` |
| ✅ AI 自动总结 | 手动 / 按间隔自动调用 LLM 生成中文摘要 |
| ✅ OBS 叠加层 | 内置本地网页 `http://127.0.0.1:8000/`，可作 OBS「浏览器源」 |

---

## 界面说明

打开后有两个窗口：

**① 控制面板（`RU→中文 实时翻译 · 控制面板`）** — 三个选项卡：

- **控制**
  1. **监听目标**：输出设备下拉 + `刷新`；**识别语言**下拉；`▶ 开始监听` / `■ 停止`
  2. **字幕小窗**：`显示字幕小窗`、`置顶`、`无边框`、**`背景全透明（只留文字，可鼠标穿透）`**、
     `自动高度`、字号 / 文字不透明度 / 显示行数、**位置预设**（顶部居中/底部居中/屏幕居中/自定义拖动）、宽 / 高
  3. **功能**：`导出翻译文本`、`AI 总结`、`自动总结（每 N 分钟）`、**翻译后端**（本地 NLLB / API 大模型）、**OBS 地址（可复制）**
  - 下方有一行金色的"**识别中**"实时原文
- **字幕记录**：完整双语记录（可滚动），导出即取此处内容
- **AI 总结**：显示最近一次摘要结果

**② 字幕小窗（标题 `字幕`）** — Live Captions 风格的置顶字幕条：

- **背景全透明、只留文字**（色键透明 + 文字保持不透明，透明区域自动鼠标穿透）
- **文字带黑色描边**（Canvas 绘制），压在任何画面上都清晰
- **无边框 + 可拖动**；位置预设一键切换；**自动高度**或手动宽高
- 底部一行**金色 `▍`** = 正在识别的原文（只显示最近 150 字符）；一句说完后转为**定格译文**（原文浅蓝小字 + 中文白色大字）
- 设置（位置/大小/字号/透明度等）写入 `ui_state.json`，下次自动恢复；窗口会**自动夹紧到屏幕内**

> 字体清晰：程序已声明 **DPI 感知（PER_MONITOR_AWARE_V2）**，在 125%/150% 缩放下按原生像素渲染，不会发糊。

---

## 快速开始

1. 打开直播（**正常外放即可**，不用改任何系统默认设备）。
2. 双击 **`start_live_translator.cmd`**：出现【控制面板 + 字幕小窗】，等状态栏显示「模型就绪 ✓」。
   - 想**启动即自动监听**：双击 `start_live_translator_obs.cmd`（界面相同，自动开始）。
3. 选好**监听目标**（默认已选你的默认扬声器）和**识别语言**，点 **▶ 开始监听**。
4. 想在 OBS 里叠加：用控制面板里显示并复制的地址，在 OBS 添加「浏览器源」。

> 首次运行加载模型约 10 秒，之后启动很快。某设备没声音就换一条带 🔊 的。

---

## 翻译后端与连贯性

控制面板「翻译后端」可切换（会被记住）：

| 后端 | 说明 |
|---|---|
| **API 大模型（默认）** | 走 `ai_config.json` 的 OpenAI 兼容接口（默认 DeepSeek）。**泛用性最好**（教学/口语/术语多），单句约 1–3 秒；需联网、耗 API 额度。 |
| **本地 NLLB（离线）** | 完全本地、免费；单句约 0.9–1.5 秒（CPU），但 600M 蒸馏模型泛用性/术语一致性弱于大模型。 |

> API 失败（无 Key/断网）会**自动回退本地 NLLB**，不中断。

**前后连贯性**：逐句翻译会"术语漂移"（如 `agent` 一会儿译"代理"、一会儿译"智能体"）。LLM 后端已加**上下文窗口**——把**最近 3 句（原文→译文）**作为 `{context}` 喂给模型，与上文保持术语/人称一致。

**提示词**在 `ai_config.json` 的 `translate_system` / `translate_prompt` 里可自定义（含 `{lang}` / `{context}` / `{text}` 三个占位符）。

---

## 识别语言（多语言 / 自动检测）

控制面板「识别语言」可选，默认**俄语**，会被记住。内置 19 项：

> 俄语、英语、日语、韩语、德语、法语、西班牙语、意大利语、葡萄牙语、乌克兰语、波兰语、荷兰语、土耳其语、阿拉伯语、印地语、越南语、泰语、印尼语、**自动检测**

- 选**自动检测**：Whisper 自行判断语种（状态栏显示结果）。
- 目标语言固定**简体中文**（环境变量 `TGT_LANG` 可改 NLLB 目标码）。
- 加语言：在 `live_translator.py` 的 `LANGUAGES` 表加一行 `("显示名","whisper码","NLLB/FLORES码")`。
- 环境变量 `SRC_LANG` 设初始识别语言（如 `SRC_LANG=en`）。

---

## 显示逻辑：原文实时出，译文按"句"出

```
说话中 ─► 每 700ms 用当前缓冲做一次"预览识别" ─► 字幕小窗底部实时滚动【原文】（金色 ▍）
说完   ─► 静音 ≥ 500ms 做一次切分 ─► Whisper 转写后【按句子单元切开】
       ─► 每句（句末标点）立即翻译并定格【原文 + 译文】
```

> 转写结果按 **Whisper 句边界 + 句末标点**切成"一句一条"，而不是把整段拼成一大坨——这样不再"成段识别"，且每句立即翻译、速度快很多。小写开头的碎片（如 `of AI development.`）自动并入上一句。

| 参数（环境变量） | 默认 | 作用 |
|---|---|---|
| `END_SILENCE_MS` | 500 | 说话中静音多久做一次切分 |
| `PARTIAL_INTERVAL_MS` | 700 | 预览原文的刷新间隔 |
| `PARTIAL_MAX_SEC` | 20 | 预览只取最近这么长，避免越算越慢 |
| `PARTIAL_DISPLAY_CHARS` | 150 | "识别中"那行只显示最近多少字符 |
| `MIN_UNIT_CHARS` | 4 | 单句最短字数（切句用） |
| `MIN_FINAL_CHARS` | 4 | 太短不算"完整句" |
| `MAX_CHARS` | 300 | 累计超长也翻（安全阀） |
| `FLUSH_IDLE` | 6.0 | 停顿这么久没新内容就翻 |
| `HARD_FLUSH_SEC` | 25.0 | **强制兜底**：长句/讲师不流利时可调大（如 40） |

> 调参示例：`.cmd` 里加 `set HARD_FLUSH_SEC=40`（更完整优先）或 `set END_SILENCE_MS=300`（更快出译文）。

---

## 防"臆想"（Whisper 幻觉）

Whisper 在**句尾/静音处**易凭空补出 "Please like and subscribe!"、"Thanks for watching!" 之类。三重处理：

1. **阈值抑制**：`no_speech_threshold=0.6`、`log_prob_threshold=-1.0`、`compression_ratio_threshold=2.4`、`hallucination_silence_threshold=2.0`、`temperature=0`（按当前 faster-whisper 支持的参数自动过滤）。
2. **短语黑名单**：命中 `like and subscribe / thanks for watching / subtitles by / 请点赞 / 感谢观看 …` 会**从句中剔除**（保留同句真实内容）；整句只剩幻觉则**整句丢弃**。
3. **按句切分**：句子单元化后，幻觉更难整段混入。

---

## 命令行用法

```powershell
.\.venv\Scripts\python.exe live_translator.py                # 图形界面（推荐）
.\.venv\Scripts\python.exe live_translator.py --autostart    # 图形界面 + 启动即监听
.\.venv\Scripts\python.exe live_translator.py --no-gui       # 纯无窗口（只提供 OBS 叠加层）
.\.venv\Scripts\python.exe live_translator.py --list-devices # 列出可旁路监听的输出设备
```

常用环境变量：`INPUT_DEVICE`（指定监听设备名）、`WHISPER_SIZE`（tiny/base/small/medium/large-v3）、`WHISPER_DEVICE`、`SRC_LANG`、`TGT_LANG`、`TRANSLATE_BACKEND`，以及上表所有调优项。

---

## 环境说明
- 依赖见 `requirements.txt`；`.venv` 已装好并**端到端验证通过**。
- **语音识别（faster-whisper）** 走 **GPU**（ctranslate2 + `nvidia-cublas-cu12`/`nvidia-cudnn-cu12`，脚本自动加 DLL 搜索路径）。
- **本地翻译（NLLB）** 走 **CPU**；`transformers` 锁定 **4.46.3**（5.x 移除了 seq2seq 翻译管道）。
- 模型权重缓存在 `models_whisper/` 与 `hf_cache/`（走 hf-mirror.com 镜像），无需重下。
- 网络差可用 `fetch_wheels.py` 抓轮子 + `pip install --no-index --no-deps --find-links wheels` 离线装。

---

## 常见问题

- **某设备没声音**：换默认扬声器那条（带 🔊 的都是可旁听设备）。
- **只翻译某个应用**：Windows「音量合成器」把该应用输出改到某设备 → 面板选该设备旁路。
- **术语前后不一致**：确认翻译后端是「API 大模型」（本地 NLLB 无跨句一致性）；或关闭提示词里的 `{context}` 段。
- **译文偏慢**：把 `HARD_FLUSH_SEC` 调小（如 15）或 `END_SILENCE_MS` 调小（如 300）。
- **识别不准**：可把 `WHISPER_SIZE` 升到 `large-v3`（更准，但约 3GB 下载）。
- **窗口没弹出**：看命令行红字报错（`.cmd` 有 `pause` 会停住）；端口占用会自动换端口，不影响窗口。
- **还原旧版**：`live_translator_v1_backup.py` 是基于 VB-CABLE 的旧版本。

---

## 参考

- [SoundCard（WASAPI loopback）](https://github.com/bastibe/SoundCard)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [NLLB-200](https://huggingface.co/facebook/nllb-200-distilled-600M)
- [silero-vad](https://github.com/snakers4/silero-vad)
