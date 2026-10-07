@echo off
rem ============================================================
rem  Ruijie ePortal Tool - launcher
rem  Usage:
rem    run.bat                 normal run (toast notification, no console)
rem    run.bat --check         self check, shows diagnostic output
rem    run.bat --console       run with console output
rem    run.bat --gen-config    create a config.yml template
rem    run.bat --connect       connect now, no confirmation dialog
rem    run.bat --disconnect    disconnect now, no confirmation dialog
rem    run.bat --gui           open the settings window (same as SheZhi.bat)
rem    run.bat --retry         keep retrying, exit after 5 minutes anyway
rem    run.bat --autostart on  enable autostart / off / status
rem    run.bat --fetch-cookie  get cookie+queryString from the server
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "VENV=%CD%\.venv\Scripts"
set "PYW=%VENV%\pythonw.exe"
set "PYC=%VENV%\python.exe"

rem fall back to the system python when the venv is missing
if not exist "%PYW%" set "PYW=pythonw"
if not exist "%PYC%" set "PYC=python"

rem --gui is a window app: just launch it, do not wait
echo %* | findstr /i /c:"--gui" >nul && (
    start "" "%PYW%" "%CD%\src\__main__.py" --gui
    goto :eof
)

rem these need visible output, use python.exe
set "INTERACTIVE="
echo %* | findstr /i /c:"--console" /c:"--check" /c:"--gen-config" /c:"--autostart" /c:"--fetch-cookie" >nul && set "INTERACTIVE=1"

if defined INTERACTIVE (
    "%PYC%" "%CD%\src\__main__.py" %*
    echo.
    echo [exit code %ERRORLEVEL%]
    pause
) else (
    start "" "%PYW%" "%CD%\src\__main__.py" %*
)
