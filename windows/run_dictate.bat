@echo off
setlocal
title Voice Dictation
cd /d "%~dp0.."

set "APP_DIR=%CD%"
set "PYTHON=%APP_DIR%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo [ERROR] Python virtual environment not found.
    echo.
    echo Run windows\setup.bat once to create it, then start this file again.
    echo Expected: "%PYTHON%"
    echo.
    pause
    exit /b 1
)

echo Starting Voice Dictation from "%APP_DIR%"...
echo Logs: "%APP_DIR%\dictate.log"
echo.
"%PYTHON%" -u "%APP_DIR%\dictate.py" %*
echo.
echo Voice Dictation exited with code %ERRORLEVEL%.
pause
