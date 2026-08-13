@echo off
REM Double-click launcher for GateKeeper. No terminal knowledge required:
REM it finds Python, installs anything missing the first time, then starts
REM the overlay. The console window stays open only if something goes wrong.
setlocal
cd /d "%~dp0"
title GateKeeper

REM --- find a usable Python -------------------------------------------------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY goto :nopython

REM --- first run: install dependencies --------------------------------------
%PY% -c "import numpy, yaml, cv2, mss, PIL" >nul 2>&1
if errorlevel 1 (
    echo Setting up GateKeeper for the first time. This takes a minute...
    echo.
    %PY% -m pip install --quiet --upgrade pip
    %PY% -m pip install --quiet -r requirements.txt
    if errorlevel 1 goto :installfailed
    echo Setup complete.
    echo.
)

REM --- launch ---------------------------------------------------------------
start "" %PY% -m poa.overlay
exit /b 0

:nopython
echo.
echo   Python was not found on this computer.
echo.
echo   Install it from https://www.python.org/downloads/
echo   IMPORTANT: tick "Add Python to PATH" on the first installer screen.
echo.
pause
exit /b 1

:installfailed
echo.
echo   Something went wrong installing GateKeeper's requirements.
echo   Check your internet connection and try again.
echo.
pause
exit /b 1
