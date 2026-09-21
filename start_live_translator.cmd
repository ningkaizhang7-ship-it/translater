@echo off
REM ============================================================
REM  live_ru2zh : realtime RU -> Simplified Chinese live translator
REM  (topmost subtitle window). Double-click to start.
REM  A console window opens, then a topmost black subtitle window.
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"
title Live RU -> ZH Translator (topmost window)

REM Loopback-monitor the DEFAULT speaker (WASAPI loopback): your own playback
REM keeps working normally. Pick another device inside the UI if you want.
set "HF_ENDPOINT=https://hf-mirror.com"

echo Starting live translator (subtitle window) ... press Ctrl+C to stop.
".venv\Scripts\python.exe" live_translator.py
echo.
echo Program exited.
pause
