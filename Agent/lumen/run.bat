@echo off
chcp 65001 >nul
cd /d "%~dp0"

python main.py %*
if errorlevel 1 (
    echo.
    echo [Launch failed] See the message above.
    echo Try: python -m pip install -r requirements.txt
    pause
)
