@echo off
@chcp 65001 > nul
set PYTHONIOENCODING=utf-8
title DIGIDAW Protocol (Text Mode) - Digidaw Systems
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py --text
if errorlevel 1 (
    echo.
    echo [DIGIDAW Error] Program terhenti dengan kode error.
    pause
)
