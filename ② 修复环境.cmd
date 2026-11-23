@echo off
chcp 65001 >nul
setlocal
title EvoMind - Repair Environment
cd /d "%~dp0"

call "scripts\setup.cmd" --repair
set RC=%errorlevel%
echo.
if not "%RC%"=="0" echo   Repair did not finish. Check the messages above.
echo.
pause
exit /b %RC%
