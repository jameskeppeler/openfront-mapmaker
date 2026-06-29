@echo off
title OpenFront Map Maker
REM Run from the webapp folder, relative to this script's location.
pushd "%~dp0webapp"

REM --- Require uv ---
where uv >nul 2>&1
if errorlevel 1 (
  echo ERROR: 'uv' is not installed.
  echo Install it, then run this again:  winget install astral-sh.uv
  echo   or see https://docs.astral.sh/uv/getting-started/installation/
  pause
  popd
  exit /b 1
)

REM --- Sync the environment from the lockfile (uv fetches Python 3.11 if needed) ---
echo Preparing the Python environment (uv)...
uv sync
if errorlevel 1 (
  echo Failed to set up the environment.
  pause
  popd
  exit /b 1
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
start "OpenFront Map Maker - close this window to stop" cmd /k "uv run python app.py"

echo Waiting for the Map Maker to be ready...
powershell -NoProfile -Command "for ($i=0; $i -lt 30; $i++) { try { Invoke-WebRequest -Uri 'http://localhost:5050/api/health' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { Start-Sleep -Seconds 1 } } ; exit 1"

start "" "http://localhost:5050"
popd
exit /b
