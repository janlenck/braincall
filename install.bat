@echo off
setlocal
cd /d "%~dp0"
title Claude Telefon - Installation
set "VENV=%LOCALAPPDATA%\claude-telefon\venv"

echo Python-Umgebung: %VENV%
if not exist "%VENV%\Scripts\python.exe" (
  echo Lege virtuelles Environment an ...
  py -3.12 -m venv "%VENV%" || goto :fail
)

echo.
echo Aktualisiere pip ...
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip --disable-pip-version-check || goto :fail

echo.
echo Installiere / aktualisiere Pakete aus requirements.txt ...
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt --disable-pip-version-check || goto :fail

echo.
echo Pruefe Importe ...
"%VENV%\Scripts\python.exe" -c "import pyVoIP, anthropic, requests, numpy, truststore, zoneinfo; zoneinfo.ZoneInfo('Europe/Berlin'); print('Alles da.')" || goto :fail

echo.
echo Fertig. Jetzt start.bat starten.
pause
exit /b 0

:fail
echo.
echo FEHLER bei der Installation - siehe Ausgabe oben.
pause
exit /b 1
