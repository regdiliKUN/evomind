@echo off
chcp 65001 >nul
setlocal
title EvoMind - Repair Environment
cd /d "%~dp0"

echo.
echo   EvoMind environment repair
echo   --------------------------
echo.

set PY=
if exist "C:\Users\%USERNAME%\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe" set PY=C:\Users\%USERNAME%\.workbuddy-ai\binaries\python\versions\3.13.12\python.exe
if "%PY%"=="" (
    where py >nul 2>nul
    if not errorlevel 1 set PY=py -3
)
if "%PY%"=="" (
    where python >nul 2>nul
    if not errorlevel 1 set PY=python
)
if "%PY%"=="" (
    echo   [ERROR] Python 3.11+ not found.
    echo   Install from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo   Python: %PY%
echo   Creating virtual environment...
if not exist ".venv" %PY% -m venv .venv

echo   Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install fastapi "uvicorn[standard]" httpx beautifulsoup4 lxml trafilatura numpy huggingface_hub

echo.
echo   Done. Now run the start script again.
echo.
pause
