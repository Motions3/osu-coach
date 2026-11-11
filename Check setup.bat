@echo off
title osu! coach - setup check
cd /d "%~dp0"

rem Python: "python" on PATH first, else the py launcher.
set "PY="
python -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "PY=python"
  goto :run
)
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "PY=py -3"
  goto :run
)
echo Python wasn't found.
echo Install Python 3.11 or newer from https://www.python.org/downloads/
echo (or from the Microsoft Store), then run this again.
pause
exit /b 1

:run
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 3)" >nul 2>nul
if errorlevel 3 (
  echo Your Python is too old. The coach needs Python 3.11 or newer from https://www.python.org/downloads/
  pause
  exit /b 1
)
%PY% setup_check.py %*
echo.
pause
