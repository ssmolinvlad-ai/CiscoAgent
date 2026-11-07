@echo off
cd /d "%~dp0"
if not defined CISCO_AGENT_PROFILE if exist "%~dp0course-user.local.json" set "CISCO_AGENT_PROFILE=%~dp0course-user.local.json"
if exist "bin\Verified\PacketTracerAgent\PacketTracerAgent.exe" (
  start "" "bin\Verified\PacketTracerAgent\PacketTracerAgent.exe"
  exit /b
)
if exist "bin\Direct\PacketTracerAgent\PacketTracerAgent.exe" (
  start "" "bin\Direct\PacketTracerAgent\PacketTracerAgent.exe"
  exit /b
)
if exist "bin\Current\PacketTracerAgent.exe" (
  start "" "bin\Current\PacketTracerAgent.exe"
  exit /b
)
if exist "bin\PacketTracerAgent\PacketTracerAgent.exe" (
  start "" "bin\PacketTracerAgent\PacketTracerAgent.exe"
  exit /b
)
if not exist ".venv\Scripts\pythonw.exe" (
  echo Run Prepare.cmd first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "main.py"
