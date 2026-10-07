@echo off
rem ============================================================
rem  Ruijie ePortal Tool - settings window
rem  Double click this file to open the GUI and edit config.yml.
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "VENV=%CD%\.venv\Scripts"
set "PYW=%VENV%\pythonw.exe"
set "PYC=%VENV%\python.exe"

if exist "%PYW%" (
    rem pythonw: no black console window
    start "" "%PYW%" "%CD%\src\__main__.py" --gui
    goto :eof
)

rem venv missing: run with python so that errors stay visible
if exist "%PYC%" (
    "%PYC%" "%CD%\src\__main__.py" --gui
) else (
    echo [ERROR] .venv not found and python not available.
    echo Please run build/setup first, see README.md.
    pause
)
