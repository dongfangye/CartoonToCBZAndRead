@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title ComicPack - image folder to CBZ

set "PY=..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=..\..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" app.py %*
if errorlevel 1 (
    echo.
    echo   [ERROR] Startup failed. Make sure Python 3 is installed.
)
echo.
pause
