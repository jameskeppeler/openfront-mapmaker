#!/bin/bash
# OpenFront Map Maker launcher (macOS / Linux).
# Double-click in Finder to run (you may need to `chmod +x Start-MapMaker.command` once).
# Keep the Terminal window open while making maps; close it to stop.

cd "$(dirname "$0")/webapp" || exit 1

# Pick a Python 3 interpreter.
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python

# First run: create the virtual environment and install dependencies.
if [ ! -d .venv ]; then
  echo "First run: setting up the Python environment. This can take a few minutes..."
  "$PY" -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -r requirements.txt
fi

# Warn if the OpenTopography API key file is missing.
if [ ! -f .env ]; then
  echo ""
  echo "WARNING: webapp/.env not found - map generation will fail without an API key."
  echo "Create it with your free OpenTopography key, e.g.:"
  echo "    echo 'OPENTOPO_API_KEY=your_key_here' > .env"
  echo "Get a key at https://portal.opentopography.org/myopentopo"
  echo ""
fi

# Open the browser once the server is ready (in the background).
(
  for i in $(seq 1 30); do
    if curl -sf http://localhost:5000/api/health >/dev/null 2>&1; then
      open http://localhost:5000 2>/dev/null || xdg-open http://localhost:5000
      break
    fi
    sleep 1
  done
) &

echo "Starting OpenFront Map Maker. Keep this window open; close it to stop."
./.venv/bin/python app.py
