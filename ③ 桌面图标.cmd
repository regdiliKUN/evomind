@echo off
chcp 65001 >nul
setlocal
title EvoMind - Desktop Icon
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   [ERROR] EvoMind is not set up yet.
    echo   Please run the setup script first.
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "scripts\desktop_icon.py"
echo.
pause
