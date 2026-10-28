@echo off
setlocal
cd /d "%~dp0"
title Claude Telefon - Installation
set "VENV=%LOCALAPPDATA%\claude-telefon\venv"

echo Python environment: %VENV%
if not exist "%VENV%\Scripts\python.exe" (
  echo Creating virtual environment ...
  py -3.12 -m venv "%VENV%" || goto :fail
)

echo.
echo Updating pip ...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip --disable-pip-version-check || goto :fail

echo.
echo Installing / updating packages from requirements.txt ...
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt --disable-pip-version-check || goto :fail

echo.
echo Checking imports ...
"%VENV%\Scripts\python.exe" -c "import pyVoIP, anthropic, requests, numpy, truststore, zoneinfo; zoneinfo.ZoneInfo('Europe/Berlin'); print('All good.')" || goto :fail

echo.
echo Done. Now run start.bat.
pause
exit /b 0

:fail
echo.
echo Installation FAILED - see the output above.
pause
exit /b 1
