@echo off
@chcp 65001 > nul
setlocal EnableDelayedExpansion
title DIGIDAW Setup
cd /d "%~dp0"
rem  DIGIDAW setup for Windows 10/11. Safe to run again at any time (updates everything).
rem  Steps: Python 3.12 environment -> modules -> Gemini API key (.env) -> speech models ->
rem         optional local AI (Ollama + qwen3) -> Desktop shortcut -> setup check.

echo.
echo [DIGIDAW] 1/6  Memeriksa Python 3.12...
set "PY="
py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY (
    python -c "import sys; assert sys.version_info[:2] == (3, 12)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [!] Python 3.12 belum terpasang. Memasang lewat winget...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
)
if not defined PY (
    echo [ERROR] Python 3.12 tidak ditemukan. Install dari https://www.python.org/downloads/release/python-3120/
    echo         ^(centang "Add python.exe to PATH"^), lalu jalankan setup ini lagi.
    pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [DIGIDAW] Membuat virtual environment...
    %PY% -m venv .venv || (echo [ERROR] Gagal membuat virtual environment. & pause & exit /b 1)
)

echo.
echo [DIGIDAW] 2/6  Menginstall / memperbarui modul (pertama kali bisa beberapa menit)...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Sebagian modul gagal diinstall. Periksa koneksi internet lalu jalankan lagi.
    pause & exit /b 1
)

echo.
echo [DIGIDAW] 3/6  Gemini API key
if not exist ".env" copy ".env.example" ".env" >nul
findstr /r /c:"^GEMINI_API_KEY=..*" ".env" >nul
if errorlevel 1 (
    echo          Gratis: buka https://aistudio.google.com/apikey lalu "Create API key".
    echo          Boleh dikosongkan sekarang dan diisi nanti di Control Panel - AI.
    set "KEY="
    set /p "KEY=         Tempel API key (Enter = lewati): "
    if defined KEY (
        > ".env" echo GEMINI_API_KEY=!KEY: =!
        echo          Tersimpan di .env ^(tidak ikut ter-upload ke GitHub^).
    )
) else (
    echo          Sudah ada.
)

echo.
echo [DIGIDAW] 4/6  Mengunduh model pengenal suara lokal (Whisper, +-220 MB, sekali saja)...
".venv\Scripts\python.exe" -c "import local_stt, sys; sys.exit(0 if local_stt.ensure_models() else 1)"
if errorlevel 1 echo [!] Unduhan gagal; Digidaw akan mencoba lagi saat pertama jalan.

echo.
echo [DIGIDAW] 5/6  Otak lokal (Ollama + qwen3:4b-instruct, +-2.5 GB) - opsional tapi disarankan:
echo          pertanyaan ringan ^& perintah jadi cepat, gratis, dan tetap jalan tanpa internet.
where ollama >nul 2>&1
if errorlevel 1 (
    set "ANS=y"
    set /p "ANS=         Pasang Ollama sekarang? [Y/n] "
    if /i "!ANS:~0,1!"=="y" (
        winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
        set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Ollama"
    )
)
where ollama >nul 2>&1
if not errorlevel 1 (
    ollama list | findstr /b /c:"qwen3:4b-instruct" >nul
    if errorlevel 1 (
        set "ANS=y"
        set /p "ANS=         Unduh model qwen3:4b-instruct sekarang? [Y/n] "
        if /i "!ANS:~0,1!"=="y" ollama pull qwen3:4b-instruct
    ) else (
        echo          qwen3:4b-instruct sudah ada.
    )
)

echo.
echo [DIGIDAW] 6/6  Membuat shortcut Digidaw di Desktop...
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP=%%D"
".venv\Scripts\python.exe" -c "import settings, pathlib; settings.make_shortcut(pathlib.Path(r'%DESKTOP%') / 'Digidaw.lnk')"
if errorlevel 1 (echo [!] Shortcut gagal dibuat; jalankan start_digidaw.vbs secara langsung.) else (echo          Shortcut "Digidaw" ada di Desktop.)

echo.
echo [DIGIDAW] Memeriksa hasil setup...
".venv\Scripts\python.exe" check_setup.py

echo.
echo [DIGIDAW] Selesai! Klik dua kali shortcut "Digidaw" di Desktop (atau start_digidaw.vbs),
echo           lalu ucapkan: "Digidaw, jam berapa sekarang?"
pause
