@echo off
rem Starts tosu (if you installed it), osu! and your coach pet Mochi. Nothing is injected into the game:
rem the pet is its own window, so osu! updates never break it.
rem tosu is optional (lets the coach see every try, even fails and quits, plus live PP in song select).
rem Install it yourself from its official releases page https://github.com/tosuapp/tosu/releases into
rem "%LOCALAPPDATA%\tosu\" - NOT inside OneDrive: tosu refuses to start from a OneDrive folder.
rem (Coach > Settings > "Move tosu out of OneDrive" copies an old copy there for you.)
rem This script never downloads anything.
cd /d "%~dp0"

rem ---- Python: "pythonw"/"python" on PATH first, else the py launcher ("pyw"/"py -3").
set "PY="
set "PYW="
python -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "PY=python"
  set "PYW=pythonw"
  goto :pycheck
)
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "PY=py -3"
  set "PYW=pyw -3"
  goto :pycheck
)
echo Python wasn't found.
echo Install Python 3.11 or newer from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup. Then run this again.
pause
exit /b 1

:pycheck
%PY% -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 11) else 3)" >nul 2>nul
if errorlevel 3 (
  echo Your Python is too old. Mochi needs Python 3.11 or newer from https://www.python.org/downloads/
  pause
  exit /b 1
)
if errorlevel 1 (
  echo Your Python is missing tkinter, the window toolkit Mochi uses.
  echo Re-run the python.org installer, choose Modify, and tick "tcl/tk and IDLE".
  pause
  exit /b 1
)
rem pythonw has no console window; fall back to the console one if it isn't there.
if "%PYW%"=="pythonw" (
  where pythonw >nul 2>nul
  if errorlevel 1 set "PYW=python"
)
if "%PYW%"=="pyw -3" (
  where pyw >nul 2>nul
  if errorlevel 1 set "PYW=py -3"
)

rem ---- tosu: pet_settings.json "tosu_path" (if not in OneDrive), else %LOCALAPPDATA%\tosu\tosu.exe,
rem else nothing (a copy inside OneDrive can't start - the coach's Settings tab offers to move it)
set "TOSU="
for /f "usebackq delims=" %%T in (`%PY% "%~dp0pet_util.py" --tosu-path 2^>nul`) do set "TOSU=%%T"
if not defined TOSU goto :notosu
if not exist "%TOSU%" goto :notosu
tasklist /FI "IMAGENAME eq tosu.exe" /NH 2>nul | "%SystemRoot%\System32\find.exe" /i "tosu.exe" >nul
if not errorlevel 1 goto :osu
for %%D in ("%TOSU%") do start "" /min /d "%%~dpD" "%%~fD"
timeout /t 2 /nobreak >nul
goto :osu

:notosu
tasklist /FI "IMAGENAME eq tosu.exe" /NH 2>nul | "%SystemRoot%\System32\find.exe" /i "tosu.exe" >nul
if not errorlevel 1 goto :osu
echo tosu wasn't found, so there's no live PP in song select this time.
echo To get it: download the Windows zip from https://github.com/tosuapp/tosu/releases
echo and extract it to "%LOCALAPPDATA%\tosu" so you have "%LOCALAPPDATA%\tosu\tosu.exe".
echo Don't put it inside OneDrive - tosu won't start from there.
echo Starting without it...
timeout /t 5 >nul

:osu
rem ---- osu!: lazer first, then stable.
set "OSU=%LOCALAPPDATA%\osulazer\osu!.exe"
if exist "%OSU%" goto :startosu
set "OSU=%LOCALAPPDATA%\osu!\osu!.exe"
if exist "%OSU%" goto :startosu
echo Couldn't find osu!lazer at "%LOCALAPPDATA%\osulazer\osu!.exe"
echo or osu!stable at "%LOCALAPPDATA%\osu!\osu!.exe".
echo Start osu! yourself - the pet will appear when it's running.
timeout /t 5 >nul
goto :pet

:startosu
start "" "%OSU%"

:pet
start "" %PYW% "%~dp0pet.pyw" --exit-with-osu
exit /b
