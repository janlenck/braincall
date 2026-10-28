@echo off
setlocal
cd /d "%~dp0"
if not exist logs mkdir logs
set "VENV=%LOCALAPPDATA%\claude-telefon\venv"

if not exist "config.json" (
  echo config.json fehlt - kopiere config.example.json nach config.json und fuelle sie aus.
  pause
  exit /b 1
)

if not exist "%VENV%\Scripts\python.exe" (
  echo Lege virtuelles Environment an: %VENV% ^(dauert ca. 1 Minute^)
  py -3.12 -m venv "%VENV%" || goto :fail
  "%VENV%\Scripts\python.exe" -m pip install -q --upgrade pip
)

title Claude Telefon
"%VENV%\Scripts\python.exe" -u app\main.py
set RC=%errorlevel%
if not "%RC%"=="0" (
  echo.
  echo Beendet mit Fehlercode %RC% - siehe Ausgabe oben bzw. logs\claude-telefon.log
  pause
)
exit /b %RC%

:fail
echo Anlegen des venv fehlgeschlagen.
pause
exit /b 1
