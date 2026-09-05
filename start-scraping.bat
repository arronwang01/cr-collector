@echo off
REM Windows: double-click this. Sets up what it needs the first time, then collects.
REM Everything it installs lives in this folder. Nothing is added to your system.
setlocal enabledelayedexpansion
cd /d "%~dp0"
echo Clash Royale replay collector
echo.

REM The "py" launcher is the reliable one. Plain "python" on Windows is often a
REM Microsoft Store stub that opens the Store instead of running anything.
set PY=
py -3 --version >nul 2>&1 && set PY=py -3
if "%PY%"=="" (
  python --version >nul 2>&1 && set PY=python
)
if "%PY%"=="" (
  echo Python 3 is needed. Install it from https://www.python.org/downloads/
  echo IMPORTANT: tick "Add Python to PATH" in the installer, then run this again.
  pause
  exit /b 1
)

if not exist .venv (
  echo First run: setting up. This downloads a browser and takes a few minutes.
  echo It only happens once.
  %PY% -m venv .venv
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\python -m pip install -q playwright beautifulsoup4 lxml
  .venv\Scripts\python -m playwright install chromium
)

for /f "usebackq tokens=1,* delims== " %%a in ("coordinator\settings.txt") do (
  if "%%a"=="server" set SERVER=%%b
  if "%%a"=="token"  set TOKEN=%%b
)
if "%TOKEN%"=="CHANGE-ME" goto askcode
if "%TOKEN%"=="ASK-THE-ADMIN" goto askcode
if "%TOKEN%"=="" goto askcode
goto havecode

:askcode
echo You need the access code from whoever invited you.
echo It looks like:  cr-1234abcd...
echo.
set /p TOKEN=Paste the access code and press Enter: 
if "%TOKEN%"=="" (
  echo No code entered. Ask the admin for it, then run this again.
  pause
  exit /b 1
)
> "coordinator\settings.txt" echo server = %SERVER%
>>"coordinator\settings.txt" echo token = %TOKEN%
echo Saved. You will not be asked again.
echo.

:havecode

echo.
echo A browser window will open. Log in to RoyaleAPI there.
echo It may sit on a loading page for a minute or two - that is normal.
echo.

:loop
.venv\Scripts\python coordinator\client.py --server "%SERVER%" --token "%TOKEN%"
if %errorlevel%==0 goto done
echo.
echo Stopped unexpectedly. Restarting in 15s - your progress is saved.
echo Close this window if you want to stop for good.
timeout /t 15 /nobreak >nul
goto loop

:done
echo All done.
pause
