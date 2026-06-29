# OpenFront Map Maker — Local Setup

This is a local copy of the OpenFront Map Maker, configured to run **offline
with no login**. Draw a region on a map and turn it into OpenFront terrain.

## Run it

- **Windows:** double-click **`Start-MapMaker.bat`**
- **macOS / Linux:** double-click **`Start-MapMaker.command`**
  (first time: `chmod +x Start-MapMaker.command`)

The launcher uses [uv](https://docs.astral.sh/uv/). On first run it downloads the
right Python, installs dependencies from `uv.lock`, and opens
<http://localhost:5050>. (If you don't have uv: `brew install uv`, or
`curl -LsSf https://astral.sh/uv/install.sh | sh`.)

## One-time: API key

Create `webapp/.env` with a free OpenTopography key
(<https://portal.opentopography.org/myopentopo>):

```
OPENTOPO_API_KEY=your_key_here
```

This file is **git-ignored** — never commit your key.

## Local changes made to the upstream tool

- `webapp/static/index.html`
  - Supabase config blanked → runs in **dev mode** (no sign-in wall).
  - Removed the client-side API-key gate (the key is supplied server-side via `.env`).
  - Default basemap switched from satellite to **topographic** (plus an
    OpenTopoMap contour option).
- `webapp/map_processor.py` — re-enabled **nation/province spawn detection**
  (Natural Earth admin-0 countries, falling back to admin-1 provinces/states),
  which the upstream had disabled "for testing".

## Getting a generated map into the game

After generating, run the map through `scripts/map_generator.py` and register
it in the game repo. The full, step-by-step pipeline is documented in the game
repo's **`SETUP.md`** ("How a region becomes a playable map").
