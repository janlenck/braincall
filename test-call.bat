@echo off
rem Triggers a test call via the running agent. Optional argument: number to call (default: default_number)
cd /d "%~dp0"
"%LOCALAPPDATA%\claude-telefon\venv\Scripts\python.exe" app\call.py "Briefly say hello, ask who is speaking and then what they need." %*
pause
