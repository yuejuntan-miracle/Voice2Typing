@echo off
setlocal enabledelayedexpansion
title Voice Dictation - First-time Setup
cd /d "%~dp0.."

set "APP_DIR=%CD%"
set "PS_CHECK=%~dp0system_check.ps1"

echo ============================================================
echo  Voice Dictation - First-Time Setup and System Check
echo ============================================================

rem 1. Run Pre-flight System & Hardware Check
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_CHECK%"
if errorlevel 1 (
    echo [!] Pre-flight check encountered a warning, continuing setup...
)

rem 2. Find Python 3.10+ Executable
set "PYTHON_EXE="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_CHECK%" -Action find-python`) do (
    set "PYTHON_EXE=%%i"
)

if not defined PYTHON_EXE (
    echo.
    echo ============================================================
    echo  Python 3.10+ was not found on your system.
    echo ============================================================
    echo.
    set /p "INSTALL_PY=Would you like setup to install Python 3.12 automatically? (Y/N) [Y]: "
    if "!INSTALL_PY!"=="" set "INSTALL_PY=Y"
    if /i "!INSTALL_PY!"=="Y" (
        for /f "usebackq delims=" %%i in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_CHECK%" -Action install-python`) do (
            set "PYTHON_EXE=%%i"
        )
    )
)

if not defined PYTHON_EXE (
    echo.
    echo [ERROR] Python 3.12 is required but could not be located.
    echo Please install Python 3.12 manually from: https://www.python.org/downloads/
    echo Be sure to tick "Add python.exe to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo Using Python: "%PYTHON_EXE%"
echo.

rem 3. Virtual Environment (.venv)
if exist ".venv\Scripts\python.exe" (
    echo Virtual environment .venv already exists, reusing it.
) else (
    echo Creating virtual environment in "%APP_DIR%\.venv" ...
    "%PYTHON_EXE%" -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created.
)

rem 4. Dependencies Installation
echo.
echo ============================================================
echo  Installing Dependencies (this can take a minute or two)...
echo ============================================================
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo.
echo Installing project requirements...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. Please check the error messages above.
    pause
    exit /b 1
)

rem 5. Environment Doctor Check
echo.
echo ============================================================
echo  Checking Environment Health...
echo ============================================================
".venv\Scripts\python.exe" dictate.py --doctor

rem 6. Taskbar / Traffic Light Tray Icon Tip
echo.
echo ============================================================
echo  Taskbar Traffic Light Icon Notice:
echo ============================================================
echo  On Windows, new tray icons are hidden by default inside
echo  the overflow menu behind the '^' chevron near the clock.
echo.
echo  If you do not see the green/red traffic light on your taskbar:
echo    1. Click the '^' chevron on the right side of your taskbar.
echo    2. Click and drag the traffic light icon onto the main taskbar.
echo.
echo  The app also automatically promotes the icon on future starts.
echo ============================================================

echo.
echo ============================================================
echo  Setup Complete!
echo  Next: put your Gemini API key in config.json
echo        (or set GEMINI_API_KEY), then run windows\run_dictate.bat
echo ============================================================
echo.
pause
