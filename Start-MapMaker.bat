@echo off
title OpenFront Map Maker
REM Run from the webapp folder, relative to this script's location.
pushd "%~dp0webapp"

REM --- First run: create the Python virtual environment and install deps ---
if not exist ".venv" (
  echo First run: setting up the Python environment. This can take a few minutes...
  python -m venv .venv
  call ".venv\Scripts\python.exe" -m pip install --upgrade pip
  call ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

REM --- Warn if the OpenTopography API key file is missing ---
if not exist ".env" (
  echo.
  echo WARNING: webapp\.env not found - map generation will fail without an API key.
  echo Create it with your free OpenTopography key, e.g.:
  echo     echo OPENTOPO_API_KEY=your_key_here ^> .env
  echo Get a key at https://portal.opentopography.org/myopentopo
  echo.
)

echo Starting the Map Maker server...
echo (A separate window will open - keep it open while making maps, close it to stop.)
start "OpenFront Map Maker - close this window to stop" cmd /k ".venv\Scripts\python.exe app.py"

echo Waiting for the Map Maker to be ready...
powershell -NoProfile -Command "for ($i=0; $i -lt 30; $i++) { try { Invoke-WebRequest -Uri 'http://localhost:5000/api/health' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { Start-Sleep -Seconds 1 } } ; exit 1"

start "" "http://localhost:5000"
popd
exit /b
