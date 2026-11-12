@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Local virtual environment not found.
    pause
    exit /b 1
)

rem Debugging launcher: keeps the console visible so print/traceback output
rem can be read directly. Normal use should be run.bat (no console).
".venv\Scripts\python.exe" main.py
if errorlevel 1 (
    echo.
    echo The application exited with an error. See the message above
    echo and logs\error.log for the full traceback.
    pause
)
