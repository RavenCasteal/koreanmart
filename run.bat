@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] Local virtual environment not found.
    echo.
    echo Run these two commands once, in this project folder:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

rem pythonw.exe starts the GUI WITHOUT a console window (Stage 8).
rem Startup and runtime errors are still recorded in logs\error.log by main.py.
rem To see a console while debugging, use run_debug.bat instead.
start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0
