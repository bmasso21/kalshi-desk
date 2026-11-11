@echo off
cd /d "%~dp0"
if not exist .venv (
  echo  Run setup.bat first.
  pause
  exit /b 1
)
title Kalshi Desk (close this window to stop)
.venv\Scripts\python run.py
pause
