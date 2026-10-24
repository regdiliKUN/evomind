@echo off
chcp 65001 >nul
setlocal
title EvoMind - Build Package
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   [ERROR] Run the setup script first:  ⓪ 首次安装.cmd
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "scripts\pack.py"
echo.
pause
