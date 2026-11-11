@echo off
title osu! coach
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
echo and tick "Add python.exe to PATH" during setup. Then run this again.
pause
exit /b 1

:run
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 3)" >nul 2>nul
if errorlevel 3 (
  echo Your Python is too old. The coach needs Python 3.11 or newer from https://www.python.org/downloads/
  pause
  exit /b 1
)
echo Analysing your recent osu! replays...
echo.
%PY% osu_coach.py --last 40
if errorlevel 1 (
  echo.
  echo Something went wrong - see the message above.
  pause
  exit /b
)
start "" notepad "osu_coach_report.txt"
echo.
echo Done. Your report opened in Notepad.
pause
