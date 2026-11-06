@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.11+ is required.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install ".[bridge]"
if errorlevel 1 goto failed
echo Ready. Launch the app using the launcher.
pause
exit /b 0
:failed
echo Setup failed. Check the message above.
pause
exit /b 1
