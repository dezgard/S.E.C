@echo off
setlocal
title Star Empire Companion
cd /d "%~dp0"
python launcher.py
if errorlevel 1 (
  echo.
  echo Star Empire Companion could not start. Check that Python and Pillow are available.
  pause
)
