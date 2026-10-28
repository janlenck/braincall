@echo off
chcp 1252 >nul
cd /d "%~dp0"
"%LOCALAPPDATA%\claude-telefon\venv\Scripts\python.exe" app\call.py "Sag kurz hallo und frag wer dran ist und danach was er will." %*
pause
