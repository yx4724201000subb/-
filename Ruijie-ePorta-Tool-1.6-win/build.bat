@echo off
rem ============================================================
rem  Build a standalone exe with PyInstaller.
rem  Output: dist\Ruijie-ePorta-Tool.exe
rem  The exe needs a config.yml placed next to it when copied elsewhere.
rem
rem  NOTE: pip needs working HTTPS. If the campus portal has not
rem  authenticated this machine yet, all HTTPS gets intercepted and pip
rem  fails with SSLCertVerificationError (untrusted root certificate).
rem  In that case run run.bat --connect first, then build again.
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/3] installing pyinstaller ...
"%PY%" -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo.
    echo    normal install failed, retrying with the Windows cert store ...
    "%PY%" -m pip install --upgrade --use-feature=truststore pyinstaller
)
if errorlevel 1 (
    echo.
    echo    still failing, last try without certificate verification ...
    "%PY%" -m pip install --upgrade ^
        --trusted-host pypi.org ^
        --trusted-host pypi.python.org ^
        --trusted-host files.pythonhosted.org ^
        pyinstaller
)
if errorlevel 1 goto :neterr

echo [2/3] building ...
rem Do NOT add -F / -w here: those are makespec options and PyInstaller
rem refuses them when a .spec file is given:
rem   "makespec options not valid when a .spec file is given"
rem Onefile / no-console are already set inside Windows-Ruijie.spec (EXE(...)).
"%PY%" -m PyInstaller --clean Windows-Ruijie.spec || goto :fail

echo [3/3] done.
echo.
echo Build finished. The exe is in the dist\ folder.
echo Copy config.yml next to the exe if you move it elsewhere.
goto :eof

:neterr
echo.
echo ============================================================
echo  Cannot install pyinstaller -- most likely the network is still
echo  intercepted by the campus portal.
echo  If you saw SSLCertVerificationError / untrusted root above:
echo    1) run run.bat --connect first (authenticate the network)
echo    2) make sure https://pypi.org opens in a browser
echo    3) then run this script again
echo  You do not have to build: run.bat / SheZhi.bat work as-is.
echo ============================================================
exit /b 1

:fail
echo.
echo Build failed, see the errors above.
exit /b 1
