@echo off
rem  IT Stock Manager - double-click launcher for Windows 11 - no admin needed
setlocal

rem  Keep the window open no matter what: re-run once inside "cmd /k" so the
rem  window can never vanish before showing what happened.
if not defined KEEP_OPEN (
  set KEEP_OPEN=1
  cmd /k "%~f0"
  exit /b
)

cd /d "%~dp0"

rem  Find a real Python. The plain "python" command can be hijacked by the
rem  Microsoft Store alias stub, which prints nothing when run - so check the
rem  OUTPUT of "--version", not just whether the command exists. Prefer the
rem  "py" launcher, which the python.org installer adds and the Store alias
rem  cannot hijack.
set "PY="
set "PYVER="
for /f "delims=" %%v in ('py -3 --version 2^>nul') do set "PYVER=%%v"
if defined PYVER set "PY=py -3"

if not defined PY (
  set "PYVER="
  for /f "delims=" %%v in ('python --version 2^>nul') do set "PYVER=%%v"
  if defined PYVER set "PY=python"
)

if not defined PY goto no_python

if not exist .venv (
  echo Creating a private Python environment - first run only...
  %PY% -m venv .venv
  if errorlevel 1 goto venv_fail
)

call .venv\Scripts\activate.bat
if errorlevel 1 goto activate_fail

echo Installing dependencies - first run only...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto pip_fail

echo.
echo  Starting IT Stock Manager...
echo  Your browser will open at http://localhost:8077
echo  Keep this window open while using the app. Close it to stop the server.
echo.
start "" http://localhost:8077
python app.py
pause
exit /b 0

:no_python
echo.
echo  Python was not found on this PC.
echo.
echo  If Python IS already installed, the Microsoft Store alias is hijacking
echo  the python command. Fix it in one of these ways:
echo    1. Open Settings - Apps - Advanced app settings - App execution
echo       aliases and turn off the Python entries, or
echo    2. Re-run the Python installer from python.org, choose Modify,
echo       and tick "Add python.exe to PATH".
echo.
echo  Otherwise install Python from https://www.python.org/downloads/ -
echo  during setup choose "Install for me only" and tick
echo  "Add python.exe to PATH" - no admin needed.
echo.
pause
exit /b 1

:venv_fail
echo.
echo  Failed to create the virtual environment.
echo.
pause
exit /b 1

:activate_fail
echo.
echo  Failed to activate the virtual environment.
echo  Delete the .venv folder and double-click this file again.
echo.
pause
exit /b 1

:pip_fail
echo.
echo  Failed to install dependencies. Check your internet connection.
echo.
pause
exit /b 1
