@echo off
REM ===================================================================
REM  Drobo Dashboard REBORN -- start with a SIMULATED Drobo
REM
REM  DOUBLE-CLICK THIS to try the app without touching real hardware.
REM
REM  It starts a simulated Drobo, opens the app against it, and shuts
REM  the simulator down again when you exit the app -- including when
REM  you exit from the system tray. The simulator belongs to this app
REM  instance and does not outlive it.
REM
REM  For your REAL Drobo, use START DROBO DASHBOARD.bat instead.
REM ===================================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start-with-mock.ps1"
