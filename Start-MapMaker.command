#!/bin/bash
# OpenFront Map Maker launcher (macOS / Linux).
# Double-click in Finder to run (you may need to `chmod +x Start-MapMaker.command` once).
# Keep the Terminal window open while making maps; close it to stop.

cd "$(dirname "$0")/webapp" || exit 1

# Make sure uv is on PATH (common install locations if the shell is minimal).
command -v uv >/dev/null 2>&1 || export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' is not installed. Install it, then run this again:"
  echo "    brew install uv"
  echo "  or: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

# Sync the environment from the lockfile. uv downloads the right Python (3.11)
# automatically if it is missing, and only reinstalls when something changed.
echo "Preparing the Python environment (uv)..."
uv sync || exit 1

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
    if curl -sf http://localhost:5050/api/health >/dev/null 2>&1; then
      open http://localhost:5050 2>/dev/null || xdg-open http://localhost:5050
      break
    fi
    sleep 1
  done
) &

echo "Starting OpenFront Map Maker. Keep this window open; close it to stop."
uv run python app.py
