@echo off
cd /d "%~dp0"
set "PATH=%~dp0.venv\Scripts;%SystemRoot%\System32;%SystemRoot%"
".venv\Scripts\python.exe" -m pip install ".[bridge,dev]"
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --windowed --onedir --name PacketTracerAgent --collect-all ptagent --distpath bin --workpath build main.py
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --console --onedir --name PacketTracerBridge --collect-all packet_tracer_mcp --distpath bin --workpath build bridge_main.py
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" tools\finalize_build.py
pause
