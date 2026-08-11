@echo off
REM ===================================================================
REM  Drobo Dashboard REBORN -- check for a newer version.
REM
REM  On demand only, never automatic. An update check that nags while
REM  you are on a train with no signal is worse than no update check.
REM
REM  This calls the SAME code the app itself uses
REM  (agent\drobo_agent\updates.py) against the SAME settings block in
REM  agent\config.json. Until 2026-07-28 there was a second checker here
REM  -- its own PowerShell script, its own config file, its own idea of
REM  what a feed looks like. Two mechanisms that both had to be switched
REM  on separately is how an updater quietly stops working: you enable
REM  one and assume you are covered. There is now one.
REM
REM  Update checks ship OFF. To turn them on, set "enabled": true in the
REM  "updates" block of agent\config.json and point "manifest_url" at
REM  either a GitHub Releases API URL or your own JSON manifest.
REM ===================================================================

setlocal
set "AGENT=%~dp0agent"
if not exist "%AGENT%\drobo_agent\updates.py" set "AGENT=%~dp0..\agent"

pushd "%AGENT%"
py -m drobo_agent.updates
if errorlevel 9009 (
    echo.
    echo   Could not run Python, so the update check could not be performed.
    echo   Drobo Dashboard REBORN itself is unaffected.
)
popd

echo.
pause
