@echo off
chcp 65001 >nul
setlocal
title EvoMind - Setup
cd /d "%~dp0"

call "scripts\setup.cmd"
set RC=%errorlevel%

echo.
if not "%RC%"=="0" (
    echo   Setup did not finish. Check the messages above.
    echo.
)
pause
exit /b %RC%
