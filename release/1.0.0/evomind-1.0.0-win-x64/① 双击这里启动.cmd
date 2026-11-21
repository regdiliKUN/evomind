@echo off
chcp 65001 >nul
setlocal
title EvoMind - Self-Evolving Learning Engine
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto NOENV

echo.
echo   Starting EvoMind...
echo.

".venv\Scripts\python.exe" run.py
set RC=%errorlevel%

if not "%RC%"=="0" (
    echo.
    echo   [ERROR] EvoMind exited with code %RC%
    echo   If the browser did not open, run  ② 修复环境.cmd  once.
    echo.
    pause
)
exit /b %RC%

:NOENV
echo.
echo   [ERROR] Python environment not found in .venv
echo.
echo   Please run  ② 修复环境.cmd  to rebuild the environment.
echo.
pause
exit /b 1
