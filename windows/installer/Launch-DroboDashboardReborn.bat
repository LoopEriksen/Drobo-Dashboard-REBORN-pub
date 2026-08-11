@echo off
REM ---------------------------------------------------------------
REM  Drobo Dashboard REBORN -- installed launcher.
REM
REM  Starts the Python agent in the background, then opens the native
REM  Windows app. This is the installed-app equivalent of the repo's
REM  "START DROBO DASHBOARD.bat" -- same idea, adjusted for the
REM  install-folder layout (agent lives in .\agent alongside this file).
REM ---------------------------------------------------------------

setlocal
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python was not found.
    echo   Drobo Dashboard REBORN needs Python to run its agent, the piece
    echo   that actually talks to your Drobo.
    echo.
    echo   Install it from https://www.python.org/downloads/ and tick
    echo   "Add Python to PATH" during setup, then run this again.
    echo.
    pause
    exit /b 1
)

REM Start the agent minimized instead of in this window, so this launcher
REM can hand off to the app and exit -- closing the app shouldn't require
REM also babysitting a console window. The agent keeps its own window,
REM titled below, so it can still be found and closed from the taskbar.
start "Drobo Dashboard REBORN - Agent" /min py "agent\run_agent.py"

REM Give the agent a moment to bind its port before the app's first poll.
timeout /t 2 /nobreak >nul

start "" "%~dp0DroboDashboardReborn.exe"
