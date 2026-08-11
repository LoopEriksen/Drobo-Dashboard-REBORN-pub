@echo off
REM ===================================================================
REM  Drobo Dashboard REBORN
REM
REM  Double-click this. Nothing else in the repo needs finding.
REM
REM  It starts the agent (the piece that talks to your Drobo) and then
REM  opens the dashboard. If the native Windows app has been built it
REM  opens that too; otherwise the browser dashboard shows the same
REM  information and works on your phone as well.
REM
REM  Leave this window open while you're using it. Closing it stops the
REM  agent, and the dashboard will stop updating.
REM ===================================================================

title Drobo Dashboard REBORN
cd /d "%~dp0"

echo.
echo   Drobo Dashboard REBORN
echo   ----------------------
echo.

REM --- Python is the only hard requirement -------------------------
where py >nul 2>&1
if errorlevel 1 (
    echo   Python was not found.
    echo.
    echo   Install it from https://www.python.org/downloads/ and tick
    echo   "Add Python to PATH" during setup, then run this again.
    echo.
    pause
    exit /b 1
)

REM --- The native app is optional. If none has been built we just use the
REM     browser dashboard, which shows the same information.
REM
REM     Which build to run is decided by tools\find-app-exe.ps1, shared with
REM     the simulator launcher. It picks the NEWEST executable wherever it
REM     landed, rather than the first entry in a hand-kept list.
REM
REM     That list used to live here, and it had already drifted from the other
REM     launcher's copy: this one never looked in ...\net8.0-windows\win-x64\,
REM     so a build made with -r win-x64 was invisible to it while being the
REM     preferred path for the other. Two lists, two different answers, no way
REM     to tell which one you got. One shared rule instead.
set "NATIVE="
for /f "delims=" %%F in ('powershell -NoProfile -ExecutionPolicy Bypass -File "tools\find-app-exe.ps1" -Quiet 2^>nul') do (
    if not defined NATIVE set "NATIVE=%%F"
)

if defined NATIVE (
    echo   Starting the Windows app...
    start "" "%NATIVE%"
) else (
    echo   Windows app not built yet.
    echo   ^(To build it: dotnet build windows\DroboDashboardReborn -c Release^)
    echo.
    echo   Until then the browser dashboard shows the same information -
    echo   the address is printed below.
)

echo   Starting the agent...
echo.
echo   Close this window when you're done.
echo.

REM Deliberately NOT --open.
REM
REM The browser used to be launched automatically here, which was wrong for a
REM reason worth writing down: the web dashboard always shows whichever Drobo
REM the AGENT is monitoring, and it opened before you had chosen anything. On
REM a network with more than one Drobo that means a window appears showing a
REM device you did not pick -- and it looks authoritative.
REM
REM The Windows app's Tools tab has an "Open web dashboard" button, which is
REM the right place for it: by then you have chosen a Drobo, so opening the
REM browser is something you asked for rather than something that happened.
REM
REM The address is still printed below, for the phone and for the case where
REM the Windows app isn't built.
py "agent\run_agent.py"

REM If the agent stops on its own, keep the window up so the reason is
REM readable rather than vanishing.
echo.
echo   The agent has stopped.
pause
