@echo off
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs
set "VENV=%LOCALAPPDATA%\braincall\venv"

if not exist "config.json" (
  echo config.json is missing - copy config.example.json to config.json and fill it in,
  echo or put a pointer to your real config into it: { "path": "C:\...\config.json" }
  pause
  exit /b 1
)

if not exist "%VENV%\Scripts\python.exe" (
  echo Creating virtual environment: %VENV% ^(takes about a minute^)
  py -3.12 -m venv "%VENV%" || goto :fail
  "%VENV%\Scripts\python.exe" -m pip install -q --upgrade pip
)

title BrainCall
"%VENV%\Scripts\python.exe" -u app\main.py
set RC=%errorlevel%
if not "%RC%"=="0" (
  echo.
  echo Exited with error code %RC% - see the output above or logs\braincall.log
  pause
)
exit /b %RC%

:fail
echo Creating the virtual environment failed.
pause
exit /b 1
