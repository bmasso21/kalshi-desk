@echo off
setlocal
cd /d "%~dp0"
echo.
echo  === Kalshi Desk setup ===
echo.
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo  Python was not found.
  echo  Install Python 3.11 or newer from https://www.python.org/downloads/
  echo  and CHECK "Add python.exe to PATH" on the first installer screen.
  echo  Then run setup.bat again.
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo  Python 3.10+ is required. Please install a newer Python.
  pause
  exit /b 1
)
if not exist .venv (
  echo  Creating virtual environment...
  %PY% -m venv .venv || (echo  venv creation failed & pause & exit /b 1)
)
echo  Installing packages...
.venv\Scripts\python -m pip install --upgrade pip -q
.venv\Scripts\python -m pip install -r requirements.txt -q || (echo  pip install failed & pause & exit /b 1)
if not exist keys mkdir keys
echo.
echo  Setup complete. Double-click start.bat to launch Kalshi Desk.
echo  Put your Kalshi private key files in the "keys" folder.
echo.
pause
