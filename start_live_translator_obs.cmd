@echo off
REM ============================================================
REM  live_ru2zh : realtime RU -> Simplified Chinese live translator
REM  FULL UI (control panel + subtitle window) AND OBS overlay,
REM  and it auto-starts monitoring the default speaker.
REM  In OBS add a "Browser Source" with the URL shown in the panel.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"
title Live RU -> ZH Translator (UI + OBS overlay)

REM Loopback-monitor the DEFAULT speaker (WASAPI loopback); playback is unaffected.
set "HF_ENDPOINT=https://hf-mirror.com"

echo Starting live translator (UI + OBS overlay, auto-start) ...
".venv\Scripts\python.exe" live_translator.py --autostart
echo.
echo Program exited.
pause
