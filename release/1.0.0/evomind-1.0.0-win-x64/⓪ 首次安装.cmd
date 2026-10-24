@echo off
chcp 65001 >nul
setlocal
title EvoMind - Setup
cd /d "%~dp0"

set PY=
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if "%PY%"=="" (
    where py >nul 2>nul
    if not errorlevel 1 set PY=py -3
)
if "%PY%"=="" (
    where python >nul 2>nul
    if not errorlevel 1 set PY=python
)

if "%PY%"=="" (
    echo.
    echo   [ERROR] Python not found.
    echo.
    echo   Please install Python 3.10 or newer first:
    echo   https://www.python.org/downloads/
    echo.
    echo   IMPORTANT: tick "Add python.exe to PATH" during installation.
    echo.
    pause
    exit /b 1
)

%PY% "scripts\install.py"
set RC=%errorlevel%

echo.
if not "%RC%"=="0" (
    echo   Setup did not finish. Check the messages above.
    echo.
)
pause
exit /b %RC%
