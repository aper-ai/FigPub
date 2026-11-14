@echo off
setlocal
cd /d "%~dp0"
title Figure Workbench
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 launch.py %*
) else (
  python launch.py %*
)
if errorlevel 1 (
  echo.
  echo Startup failed. Read the error above. Python 3.12 is recommended.
  pause
)
