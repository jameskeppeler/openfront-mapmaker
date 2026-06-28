"""Install a generated map straight into the local OpenFront game.

Copies the game-ready files into ``<game>/resources/maps/<id>/`` and registers
the map in ``Maps.gen.ts`` + ``en.json`` so it appears in the in-game **Custom**
tab. No manual download/move/register needed.

The game repo is located via the ``GAME_REPO_PATH`` env var, or auto-detected
next to this tool (e.g. ``C:\\Games\\openfront.io``).
"""

import json
import os
import re
import shutil

GAME_FILES = ["map.bin", "map4x.bin", "map16x.bin", "manifest.json", "thumbnail.webp"]


def find_game_repo():
    """Return the absolute path to the OpenFront game repo, or None."""
    candidates = []
    env = os.environ.get("GAME_REPO_PATH")
    if env:
        candidates.append(env)
    here = os.path.dirname(os.path.abspath(__file__))  # .../webapp
    repo_root = os.path.dirname(here)  # .../OpenFrontMapGenerator
    parent = os.path.dirname(repo_root)  # e.g. C:\Games
    candidates += [
        os.path.join(parent, "openfront.io"),
        os.path.join(parent, "OpenFrontIO"),
        os.path.join(parent, "openfront-local"),
    ]
    for c in candidates:
        if c and os.path.isdir(os.path.join(c, "src", "core", "game")):
            return os.path.abspath(c)
    return None


def _sanitize(display_name):
    """Return (folder_id, enum_key) derived from a display name.

    folder_id is lowercase-alphanumeric (matches the game's folder lookup,
    which lowercases the enum key); enum_key is PascalCase-alphanumeric.
    """
    base = re.sub(r"[^a-z0-9]", "", display_name.lower())
    words = re.findall(r"[A-Za-z0-9]+", display_name)
    key = "".join(w[:1].upper() + w[1:] for w in words) if words else base
    return base, key


def _repair_manifest_dims(dest):
    """Ensure manifest.map dimensions match map.bin's byte count.

    Older Map Maker output wrote the full-image dimensions for map.bin instead
    of the half-scale (L1) dimensions. map.bin is L1 = 2x the map4x dims.
    """
    mpath = os.path.join(dest, "manifest.json")
    with open(mpath, "r", encoding="utf-8") as f:
        man = json.load(f)
    bin_bytes = os.path.getsize(os.path.join(dest, "map.bin"))
    if man["map"]["width"] * man["map"]["height"] == bin_bytes:
        return  # already correct
    w = man["map4x"]["width"] * 2
    h = man["map4x"]["height"] * 2
    if w * h != bin_bytes:
        raise RuntimeError(
            f"map.bin size {bin_bytes} does not match any known dimensions"
        )
    man["map"]["width"] = w
    man["map"]["height"] = h
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(man, f, indent=2, sort_keys=True)


def install_map(output_dir, display_name, game_repo=None):
    """Copy + register a generated map into the game. Returns an info dict."""
    game = game_repo or find_game_repo()
    if not game:
        raise RuntimeError(
            "Could not locate the OpenFront game repo. "
            "Set GAME_REPO_PATH in webapp/.env."
        )
    base, key = _sanitize(display_name)
    if not base:
        raise RuntimeError(f"Invalid map name: {display_name!r}")

    # 1) Copy the game-ready files.
    dest = os.path.join(game, "resources", "maps", base)
    os.makedirs(dest, exist_ok=True)
    for fn in GAME_FILES:
        src = os.path.join(output_dir, fn)
        if not os.path.exists(src):
            raise RuntimeError(f"Generated file missing: {fn}")
        shutil.copy2(src, os.path.join(dest, fn))

    # 2) Self-heal the manifest dimensions if needed.
    _repair_manifest_dims(dest)

    # 3) Register in Maps.gen.ts and en.json.
    _patch_maps_ts(
        os.path.join(game, "src", "core", "game", "Maps.gen.ts"), key, display_name, base
    )
    _patch_en_json(
        os.path.join(game, "resources", "lang", "en.json"), base, display_name
    )

    return {
        "id": key,
        "folder": base,
        "display_name": display_name,
        "category": "custom",
        "game_repo": game,
    }


def _patch_maps_ts(path, key, value, base):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    changed = False

    if re.search(rf'^\s*{re.escape(key)}\s*=\s*"', src, re.M) is None:
        src = src.replace(
            "export enum GameMapType {\n",
            f'export enum GameMapType {{\n  {key} = "{value}", // custom map\n',
            1,
        )
        changed = True

    if re.search(rf'id:\s*"{re.escape(key)}"', src) is None:
        entry = (
            "  {\n"
            f'    id: "{key}",\n'
            f"    type: GameMapType.{key},\n"
            f'    translationKey: "map.{base}",\n'
            '    categories: ["custom"],\n'
            "    multiplayerFrequency: 0,\n"
            "  },\n"
        )
        src = src.replace(
            "export const maps: readonly MapInfo[] = [\n",
            f"export const maps: readonly MapInfo[] = [\n{entry}",
            1,
        )
        changed = True

    if changed:
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
    return changed


def _patch_en_json(path, base, display_name):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    # Already present in the "map" section?
    if re.search(rf'\n    "{re.escape(base)}":\s*"', src):
        return False
    needle = '  "map": {\n'
    idx = src.find(needle)
    if idx == -1:
        raise RuntimeError('Could not find the "map" section in en.json')
    insert_at = idx + len(needle)
    line = f'    "{base}": {json.dumps(display_name)},\n'
    src = src[:insert_at] + line + src[insert_at:]
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    return True


if __name__ == "__main__":
    # CLI: python install_to_game.py <output_dir> "<Display Name>"
    import sys

    if len(sys.argv) < 3:
        print('Usage: python install_to_game.py <output_dir> "<Display Name>"')
        sys.exit(1)
    info = install_map(sys.argv[1], sys.argv[2])
    print("Installed:", json.dumps(info, indent=2))
