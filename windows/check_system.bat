@echo off
setlocal
title Voice Dictation - System Hardware Check
cd /d "%~dp0.."

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0system_check.ps1"
echo.
pause
