@echo off
rem NOTE: keep this file ASCII-only. Non-ASCII bytes break cmd parsing
rem (UTF-8 .bat is read as GBK on Chinese Windows and splits into garbage).
chcp 65001 >nul
cd /d "%~dp0"

rem ---- 1. find a Python that actually runs ----
rem WindowsApps\python.exe is the Microsoft Store stub: it exits silently.
rem So a real execution test is required, not just "where python".
set "PY="
py -c "import sys" >nul 2>nul && set "PY=py"
if not defined PY (
    python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [ERROR] No working Python found.
    echo         Install Python 3.10+ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

rem ---- 2. check dependencies ----
%PY% -c "import textual, openai" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] This Python is missing textual / openai.
    echo.
    %PY% -c "import sys; print('  interpreter: ' + sys.executable)"
    echo.
    echo Run this, then start again:
    echo     %PY% -m pip install -r requirements.txt -i https://pypi.org/simple
    pause
    exit /b 1
)

rem ---- 3. launch ----
%PY% main.py %*
if errorlevel 1 (
    echo.
    echo [FAILED] See the traceback above.
    pause
)
