@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  COOLITS Korean Mart - First-time setup
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found.
    echo.
    echo Install Python 3.13 from:
    echo   https://www.python.org/downloads/windows/
    echo During installation, check "Add python.exe to PATH".
    echo Then run this setup again.
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\pythonw.exe" (
    echo [1/2] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo [1/2] Virtual environment already exists - skipping.
)

echo [2/2] Installing packages (needs internet, one time only)...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Package install failed. Check your internet connection.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Setup complete. Double-click run.bat to start.
echo ============================================
echo.
pause
exit /b 0
