# live_ru2zh one-click installer (Windows)
# Usage: run  .\setup.ps1  in the project directory
# Creates .venv and installs all dependencies from PyPI.
# Model weights download on first run into ./models_whisper and ./hf_cache (via hf-mirror.com).
# AUTO-DETECTS GPU: if a CUDA torch is installed later, translation switches to GPU automatically.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Keep pip cache inside the project (not the OS cache)
$env:PIP_CACHE_DIR = Join-Path $PSScriptRoot ".pip-cache"
# Use a China PyPI mirror (fast in mainland China). Change/remove if you have a faster route.
$env:PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
# Model weights use the China mirror for HuggingFace
$env:HF_ENDPOINT = "https://hf-mirror.com"

Write-Host "[1/4] Creating venv (.venv) ..." -ForegroundColor Cyan
if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$pip    = Join-Path $PSScriptRoot ".venv\Scripts\pip.exe"

Write-Host "[2/4] Upgrading pip ..." -ForegroundColor Cyan
& $python -m pip install --upgrade pip

Write-Host "[3/4] Installing dependencies (faster-whisper / torch CPU / transformers / NLLB ...) ..." -ForegroundColor Cyan
& $pip install -r requirements.txt

Write-Host ""
Write-Host "===== Install done. Verifying torch / GPU =====" -ForegroundColor Green
& $python -c "import torch; print('torch', torch.__version__); print('cuda available =', torch.cuda.is_available())"
Write-Host "Note: cuda=True -> translation uses GPU; cuda=False -> CPU (still works, a bit slower)."
Write-Host ""
Write-Host "Next:" -ForegroundColor Green
Write-Host "  1. Install and configure the VB-CABLE virtual audio device (see README.md)" -ForegroundColor Yellow
Write-Host "  2. Open the live stream, then run:  .\.venv\Scripts\python.exe live_translator.py" -ForegroundColor Yellow
