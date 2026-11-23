@echo off
setlocal
cd /d "%~dp0.." || exit /b 1

rem The redirector may still exist after its base Python has moved.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 10) or sys.prefix == sys.base_prefix)" >nul 2>&1
    if not errorlevel 1 (
        ".venv\Scripts\python.exe" "scripts\install.py" %*
        exit /b
    )
)

rem Prefer Python 3.13 on Windows, also accept other 3.10+ installations.
set "EVOMIND_SETUP_UV_PYTHON="
for /f "delims=" %%P in ('uv python find --no-project --no-python-downloads --managed-python 3.13 2^>nul') do set "EVOMIND_SETUP_UV_PYTHON=%%P"
if defined EVOMIND_SETUP_UV_PYTHON (
    "%EVOMIND_SETUP_UV_PYTHON%" "scripts\install.py" %*
    exit /b
)

for %%V in (3.13 3) do (
    py -%%V -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
    if not errorlevel 1 (
        py -%%V "scripts\install.py" %*
        exit /b
    )
)

python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 (
    python "scripts\install.py" %*
    exit /b
)

echo   [ERROR] Install Python 3.10 or newer (3.13 recommended):
echo   https://www.python.org/downloads/
exit /b 1
