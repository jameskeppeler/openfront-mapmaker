"""
Map Processor - DEM Processing without QGIS
============================================
Uses GDAL/Rasterio to replicate the QGIS map generation workflow.
"""

import os
import json
import math
import threading
import time
import tempfile
import uuid
import requests
import zipfile
import numpy as np
from PIL import Image
from io import BytesIO
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
from rasterio.merge import merge
from rasterio.mask import mask
from pyproj import Transformer
from shapely.geometry import box, shape, Point, LineString
from shapely.ops import transform, linemerge, unary_union


# Game file constants
MIN_ISLAND_SIZE = 60
MIN_LAKE_SIZE = 200
TYPE_LAND = 0
TYPE_WATER = 1
# Deepest ocean depth (metres) mapped to the darkest water shade in depth.bin.
# ~6000 m covers all but the deepest trenches; deeper water clamps to max.
DEPTH_MAX_M = 6000.0
# Land is elevation ABOVE this, not above exactly 0. Near-coastal water in
# USGS 3DEP sits at 0 +/- a few centimetres (tidal datum vs NAVD88, project
# seams), and different distributions of the same data tip those cells to
# either side of zero — a strict >0 test renders that noise as blocky land
# teeth and ghost bands along every shore. Sub-half-metre cells are tidal
# flats / water surface, not playable land.
SEA_LEVEL_EPS_M = 0.5

# Use detailed OpenStreetMap water for selections up to this size (deg^2);
# larger areas use Natural Earth (Overpass would be too heavy/slow). Raised
# from 1.5 so province-sized maps still capture real lakes (e.g. Lake Mainit).
OSM_MAX_AREA_DEG2 = 4.0
# Above this size (deg^2), drop minor waterways (streams/ditches/drains) from the
# OSM query so large-area requests stay fast; lakes and major rivers are kept.
OSM_MINOR_WATERWAY_MAX_DEG2 = 1.25
# Below this size (deg^2), prefer the finer COP30 DEM over COP90.
SMALL_AREA_COP30_DEG2 = 0.25
# Auto-tiling: when a single OpenTopography request is rejected as too large,
# the bbox is split into quadrants and retried, then mosaicked. This caps the
# recursion depth (4^depth tiles max) to bound the number of API calls.
MAX_TILE_DEPTH = 3
# Aim for up to this many named nation spawns (with real coordinates).
NATION_TARGET = 30
# If fewer than this many in-area names are found, pull extra place names from a
# wider surrounding area to use as a fallback name pool (additionalNations).
NATION_MIN = 8
# OSM place types, most-prominent first (used to rank/limit place spawns).
OSM_PLACE_TYPES = ["city", "town", "village", "hamlet", "suburb", "borough"]

# Target terrain mix (fractions of land), applied via quantile mapping so maps
# get a consistent, gameplay-friendly balance while preserving relief order.
# Mountains take the remainder (1 - plains - highlands).
TERRAIN_PLAINS_FRAC = 0.25
TERRAIN_HIGHLAND_FRAC = 0.65
# Overpass API endpoints (tried in order).
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


# OpenFront Color Palette (Discrete)
DEM_COLOR_RAMP = [
    (0.0, (0, 0, 106), "Water"),           # #00006a
    (30.0, (190, 220, 140), "Plains"),      # #bedc8c
    (60.0, (190, 218, 142), "Plains"),      # #beda8e
    (90.0, (190, 216, 144), "Plains"),      # #bed890
    (120.0, (190, 214, 146), "Plains"),     # #bed692
    (150.0, (190, 212, 148), "Plains"),     # #bed494
    (180.0, (190, 210, 150), "Plains"),     # #bed296
    (210.0, (190, 208, 152), "Plains"),     # #bed098
    (240.0, (190, 206, 154), "Plains"),     # #bece9a
    (270.0, (190, 204, 156), "Plains"),     # #becc9c
    (300.0, (190, 202, 158), "Plains"),     # #beca9e
    (420.0, (220, 203, 160), "Highlands"),  # #dccba0
    (540.0, (222, 205, 162), "Highlands"),  # #decda2
    (660.0, (224, 207, 164), "Highlands"),  # #e0cfa4
    (780.0, (226, 209, 166), "Highlands"),  # #e2d1a6
    (900.0, (228, 211, 168), "Highlands"),  # #e4d3a8
    (1020.0, (230, 213, 170), "Highlands"), # #e6d5aa
    (1140.0, (232, 215, 172), "Highlands"), # #e8d7ac
    (1260.0, (234, 217, 174), "Highlands"), # #ead9ae
    (1380.0, (236, 219, 176), "Highlands"), # #ecdbb0
    (1500.0, (238, 221, 178), "Highlands"), # #eeddb2
    (1818.0, (240, 240, 180), "Mountains"), # #f0f0b4
    (2136.0, (240, 240, 182), "Mountains"), # #f0f0b6
    (2455.0, (241, 241, 184), "Mountains"), # #f1f1b8
    (2773.0, (242, 242, 186), "Mountains"), # #f2f2ba
    (3091.0, (242, 242, 188), "Mountains"), # #f2f2bc
    (3409.0, (242, 242, 190), "Mountains"), # #f2f2be
    (3727.0, (243, 243, 192), "Mountains"), # #f3f3c0
    (4045.0, (244, 244, 194), "Mountains"), # #f4f4c2
    (4364.0, (244, 244, 196), "Mountains"), # #f4f4c4
    (4682.0, (244, 244, 198), "Mountains"), # #f4f4c6
    (5000.0, (245, 245, 200), "Mountains"), # #f5f5c8
]

# Natural Earth data URLs
NE_RIVERS_URL = "https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_rivers_lake_centerlines.zip"
NE_LAKES_URL = "https://naturalearth.s3.amazonaws.com/10m_physical/ne_10m_lakes.zip"
NE_ADMIN1_URL = "https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_admin_1_states_provinces.zip"
NE_ADMIN0_URL = "https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_admin_0_countries.zip"

# DEM sources -> which OpenTopography endpoint + query parameter carries them.
# Global sources use /API/globaldem?demtype=...; USGS 3DEP LiDAR uses the
# separate /API/usgsdem?datasetName=... endpoint, which only covers the US
# (see https://apps.nationalmap.gov/lidar-explorer/). GEBCO is global
# topography+bathymetry (negative values are ocean depth), used for water-depth
# coloring rather than as a primary land DEM.
GLOBALDEM_URL = "https://portal.opentopography.org/API/globaldem"
USGSDEM_URL = "https://portal.opentopography.org/API/usgsdem"
DEM_SOURCES = {
    # ui_key:      (endpoint,      param_name,    param_value)
    "COP30":        (GLOBALDEM_URL, "demtype",     "COP30"),
    "COP90":        (GLOBALDEM_URL, "demtype",     "COP90"),
    "SRTMGL1":      (GLOBALDEM_URL, "demtype",     "SRTMGL1"),
    "SRTM15+":      (GLOBALDEM_URL, "demtype",     "SRTMGL1"),  # legacy alias
    "USGS1m":       (USGSDEM_URL,   "datasetName", "USGS1m"),   # LiDAR, US only
    "USGS10m":      (USGSDEM_URL,   "datasetName", "USGS10m"),  # LiDAR, US only
    "USGS30m":      (USGSDEM_URL,   "datasetName", "USGS30m"),  # US only
    "GEBCOIceTopo": (GLOBALDEM_URL, "demtype",     "GEBCOIceTopo"),  # bathymetry
}
# USGS 3DEP sources have limited (US) coverage; when a request lands outside
# coverage (or can't be downloaded at that resolution for the area) we walk
# down the resolution ladder and finally fall back to this global source.
DEM_FALLBACK_SOURCE = "COP30"
LIDAR_SOURCES = {"USGS1m", "USGS10m", "USGS30m"}
# Native ground resolution (metres) of the laddered USGS sources, finest first.
LIDAR_LADDER = ["USGS1m", "USGS10m", "USGS30m"]
LIDAR_RES_M = {"USGS1m": 1.0, "USGS10m": 10.0, "USGS30m": 30.0}


class NoCoverageError(Exception):
    """Raised when a DEM source has no data for the requested area (as opposed
    to the area being too large, which triggers tiling instead)."""


class GeoGrid:
    """Pixel <-> WGS84 mapping for an optionally *rotated* selection box.

    The selection is the axis-aligned (south, west, north, east) box rotated
    ``rotation_deg`` counter-clockwise about its center. Pixel (0, 0) is the
    rotated box's top-left corner; x runs along the box's width and y down its
    height. With rotation 0 this reduces exactly to the old linear mapping, so
    unrotated maps are bit-identical to before.

    Rotation is performed in a local metric frame at the box center (with
    cos-latitude correction), so a box drawn at any angle keeps its real
    ground dimensions.
    """

    def __init__(self, south, west, north, east, width_px, height_px,
                 rotation_deg=0.0):
        self.south, self.west, self.north, self.east = south, west, north, east
        self.width_px, self.height_px = int(width_px), int(height_px)
        self.rotation_deg = float(rotation_deg or 0.0)
        self.cy = (south + north) / 2.0
        self.cx = (west + east) / 2.0
        # Local metres-per-degree at the box centre.
        self.m_lat = 110540.0
        self.m_lon = 111320.0 * math.cos(math.radians(self.cy))
        self.w_m = (east - west) * self.m_lon
        self.h_m = (north - south) * self.m_lat
        t = math.radians(self.rotation_deg)
        self._cos, self._sin = math.cos(t), math.sin(t)

    @property
    def is_rotated(self):
        return abs(self.rotation_deg) > 1e-9

    def pixel_to_lonlat(self, x, y):
        u = (x / self.width_px - 0.5) * self.w_m    # metres along box width
        v = (y / self.height_px - 0.5) * self.h_m   # metres down box height
        east_m = u * self._cos + v * self._sin
        north_m = u * self._sin - v * self._cos
        return (self.cx + east_m / self.m_lon, self.cy + north_m / self.m_lat)

    def lonlat_to_pixel(self, lon, lat):
        east_m = (lon - self.cx) * self.m_lon
        north_m = (lat - self.cy) * self.m_lat
        # The rotation matrix [[c, s], [s, -c]] is an involution (its own
        # inverse), so the same coefficients map world back to box frame.
        u = east_m * self._cos + north_m * self._sin
        v = east_m * self._sin - north_m * self._cos
        px = int((u / self.w_m + 0.5) * self.width_px)
        py = int((v / self.h_m + 0.5) * self.height_px)
        return (px, py)

    def aabb(self):
        """(south, west, north, east) of the box that covers the rotated
        selection — what geographic data queries (DEM, Overpass, shapefile
        clips) must fetch."""
        if not self.is_rotated:
            return (self.south, self.west, self.north, self.east)
        corners = [
            self.pixel_to_lonlat(x, y)
            for (x, y) in ((0, 0), (self.width_px, 0), (0, self.height_px),
                           (self.width_px, self.height_px))
        ]
        lons = [c[0] for c in corners]
        lats = [c[1] for c in corners]
        return (min(lats), min(lons), max(lats), max(lons))

    def affine(self):
        """rasterio Affine mapping pixel (col, row) -> (lon, lat), including
        the rotation — used as dst_transform so the DEM is resampled directly
        onto the rotated grid."""
        from rasterio.transform import Affine
        a = (self.w_m / self.width_px) * self._cos / self.m_lon
        b = (self.h_m / self.height_px) * self._sin / self.m_lon
        d = (self.w_m / self.width_px) * self._sin / self.m_lat
        e = -(self.h_m / self.height_px) * self._cos / self.m_lat
        c = self.cx - a * self.width_px / 2.0 - b * self.height_px / 2.0
        f = self.cy - d * self.width_px / 2.0 - e * self.height_px / 2.0
        return Affine(a, b, c, d, e, f)


class MapProcessor:
    """Processes DEM data and generates styled terrain maps."""
    
    def __init__(self, api_key: str, output_dir: str):
        self.api_key = api_key
        self.output_dir = output_dir
        self.cache_dir = os.path.join(tempfile.gettempdir(), 'openfront_cache')
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)
    
    def generate(self, name: str, south: float, west: float, north: float, east: float,
                 width_px: int, height_px: int, dem_source: str = 'COP90',
                 plains_frac: float = None, highland_frac: float = None,
                 rotation_deg: float = 0.0) -> dict:
        """
        Generate a styled terrain map.

        Args:
            name: Map name
            south, west, north, east: Bounding box in WGS84
            dem_source: DEM source ('COP30', 'COP90', 'SRTM15+')
            rotation_deg: rotate the selection box CCW about its center; the
                output image's x axis runs along the rotated box's width.

        Returns:
            dict with file paths and metadata
        """
        print(f"Generating map: {name}")
        print(f"Bounds: S={south}, W={west}, N={north}, E={east}")
        if rotation_deg:
            print(f"Rotation: {rotation_deg} deg")
        print(f"DEM Source: {dem_source}")
        print(f"Output size: {width_px} x {height_px} px (total: {width_px * height_px:,} px)")

        # All pixel<->geo mapping goes through the grid so rotated selections
        # stay consistent across the DEM, water overlays, spawns, and depth.
        grid = GeoGrid(south, west, north, east, width_px, height_px, rotation_deg)
        # Geographic data queries must cover the whole rotated box.
        (q_south, q_west, q_north, q_east) = grid.aabb()

        area_deg2 = abs((north - south) * (east - west))

        # For small/zoomed-in areas, COP90 has too few samples; prefer COP30.
        if dem_source == "COP90" and area_deg2 <= SMALL_AREA_COP30_DEG2:
            print("Small area: switching DEM to COP30 for finer terrain detail")
            dem_source = "COP30"

        # Step 1: Download DEM
        print("Downloading DEM...")
        dem_source_requested = dem_source
        # Ground size of one output pixel — used to pick the coarsest USGS
        # ladder rung that still saturates the output resolution.
        effective_mpp = math.sqrt(
            (grid.w_m * grid.h_m) / max(1, width_px * height_px)
        )
        dem_path, dem_source_used = self._download_dem(
            q_south, q_west, q_north, q_east, dem_source,
            effective_mpp=effective_mpp,
        )

        # Step 2: Load and process DEM. Nodata is left as NaN here because its
        # meaning depends on the product: land-only DEMs (USGS 3DEP) use nodata
        # for the ocean and large water bodies as well as project-seam voids.
        print("Processing DEM...")
        dem_array, dem_transform, dem_crs = self._load_dem(
            dem_path, width_px, height_px, grid=grid, fill_nodata=False
        )

        # Blend a global, fully-covered DEM into any nodata cells so seams get
        # REAL terrain and the ocean gets REAL sea level (Copernicus is 0 over
        # ocean -> water). This is what makes land-only LiDAR products usable:
        # no guessing which voids are water and which are gaps.
        nan_mask = ~np.isfinite(dem_array)
        if nan_mask.any():
            n_nan = int(nan_mask.sum())
            blend_source = 'COP30' if dem_source_used != 'COP30' else 'COP90'
            print(f"DEM has {n_nan} nodata cell(s) "
                  f"({100.0 * n_nan / nan_mask.size:.1f}%); blending "
                  f"{blend_source} into the gaps...")
            try:
                blend_path, _ = self._download_dem(
                    q_south, q_west, q_north, q_east, blend_source
                )
                blend_dem, _, _ = self._load_dem(
                    blend_path, width_px, height_px, grid=grid, fill_nodata=True
                )
                dem_array = self._blend_nodata_regions(
                    dem_array, nan_mask, blend_dem
                )
                if os.path.exists(blend_path):
                    os.remove(blend_path)
            except Exception as e:
                # Last resort: nodata becomes sea level (water) — never fake
                # terrain invented from neighbouring cells.
                print(f"Blend DEM unavailable ({e}); "
                      f"treating nodata as water")
                dem_array = np.where(nan_mask, -1.0, dem_array)

        # Step 3: Apply color palette
        print("Applying color palette...")
        styled_image = self._apply_palette(
            dem_array, dynamic_scale=True,
            plains_frac=plains_frac, highland_frac=highland_frac,
        )

        # Step 4: Overlay water features. Small/zoomed-in areas use detailed
        # OpenStreetMap data (real rivers/lakes); large areas use Natural Earth
        # (Overpass would be too heavy). Fall back to NE if OSM yields nothing.
        print("Adding water features...")
        used_osm = False
        if area_deg2 <= OSM_MAX_AREA_DEG2:
            try:
                drawn = self._add_osm_water(
                    styled_image, south, west, north, east, width_px, height_px,
                    grid=grid,
                )
                used_osm = drawn > 0
            except Exception as e:
                print(f"OSM water failed, falling back to Natural Earth: {e}")
        if not used_osm:
            styled_image = self._add_water_features(
                styled_image, south, west, north, east, width_px, height_px,
                grid=grid,
            )

        # Re-apply the terrain mix over the FINAL land (after rivers), so the
        # plains/highlands/mountains proportions reflect the playable land.
        styled_image = self._recolor_land(
            styled_image, dem_array, plains_frac, highland_frac
        )

        # Step 5: Nation spawn points. Admin regions (Natural Earth countries /
        # provinces) first; for small/zoomed-in areas a box may contain only 1-2
        # admin regions, so supplement with OSM place names (towns, villages) at
        # their real coordinates, and if still too few, pull a name pool from a
        # wider surrounding area (used as additionalNations).
        print("Detecting nations for spawns...")
        extra_names = []
        try:
            points = self._get_province_points(
                south, west, north, east, width_px, height_px, grid=grid
            )
        except Exception as e:
            print(f"Warning: admin nation detection failed: {e}")
            points = []
        if area_deg2 <= OSM_MAX_AREA_DEG2 and len(points) < NATION_TARGET:
            try:
                points += self._get_osm_place_points(
                    south, west, north, east, width_px, height_px,
                    NATION_TARGET - len(points), points, grid=grid,
                )
            except Exception as e:
                print(f"Warning: OSM place spawns failed: {e}")
            if len(points) < NATION_MIN:
                try:
                    extra_names = self._get_nearby_place_names(
                        q_south, q_west, q_north, q_east, points,
                        NATION_MIN - len(points) + 10,
                    )
                except Exception as e:
                    print(f"Warning: nearby name pool failed: {e}")
        print(f"Found {len(points)} named spawns (+{len(extra_names)} pool names)")

        # De-cluster spawns so nations aren't packed into one corner.
        points = self._space_out_spawns(points, width_px, height_px)

        # Step 6: Save outputs
        print("Saving outputs...")
        base_name = name.lower().replace(' ', '')
        
        # Save PNG as image.png (game format)
        png_path = os.path.join(self.output_dir, "image.png")
        styled_image.save(png_path, 'PNG')
        
        # Save JSON with metadata
        json_path = os.path.join(self.output_dir, f"{base_name}.json")
        metadata = {
            "image": {
                "path": "image.png",
                "width_px": width_px,
                "height_px": height_px
            },
            "bounds": {
                "south": south,
                "west": west,
                "north": north,
                "east": east
            },
            "origin": "top-left",
            "rotation_deg": rotation_deg,
            "dem_source_requested": dem_source_requested,
            "dem_source_used": dem_source_used,
            "points": points
        }
        with open(json_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        # Step 6b: Bathymetry for cosmetic water-depth coloring (depth.bin).
        # GEBCO is global topo+bathymetry (~450m); negative values are ocean
        # depth. This is purely a render-time color channel in the game and does
        # NOT affect map.bin / naval pathfinding. Failure is non-fatal (water
        # simply renders without a depth gradient).
        depth_array = None
        try:
            print("Fetching bathymetry (GEBCO) for water depth...")
            bathy_path, _ = self._download_dem(
                q_south, q_west, q_north, q_east, "GEBCOIceTopo"
            )
            bathy_dem, _, _ = self._load_dem(
                bathy_path, width_px, height_px, grid=grid
            )
            depth_array = np.maximum(0.0, -bathy_dem.astype(np.float32))
            if os.path.exists(bathy_path):
                os.remove(bathy_path)
        except Exception as e:
            print(f"Bathymetry unavailable, water depth will be flat: {e}")

        # Step 7: Generate game files (bin files, manifest, thumbnail)
        print("Generating game files...")
        game_files = self._generate_game_files(
            styled_image, base_name, points, extra_names, depth_array
        )

        # Clean up temp DEM file
        if os.path.exists(dem_path):
            os.remove(dem_path)
        
        print(f"Map generated successfully!")
        
        all_files = ["image.png", f"{base_name}.json"] + game_files

        return {
            'files': all_files,
            'metadata': metadata,
            'dem_source_requested': dem_source_requested,
            'dem_source_used': dem_source_used,
        }
    
    def _download_dem(self, south: float, west: float, north: float, east: float,
                      dem_source: str, effective_mpp: float = None):
        """Download DEM from OpenTopography, auto-tiling if the area is too large.

        A single request is tried first. If OpenTopography rejects it as too
        large for the chosen resolution, the bbox is split into quadrants and
        each is fetched recursively, then the tiles are mosaicked into one
        GeoTIFF. This removes the practical area limit at fine detail levels.

        USGS 3DEP sources walk a resolution ladder (1m -> 10m -> 30m) instead
        of failing or jumping straight to a global DEM:
        - If `effective_mpp` (the output's ground metres-per-pixel) already
          exceeds a coarser rung's native resolution, the finer rungs are
          skipped — downloading 1m data for a 15 m/px map buys nothing and
          costs 100x the transfer.
        - If a rung has no coverage, is access-denied, or its download is too
          large even after tiling, the next rung is tried.
        - Only when the whole ladder fails does it fall back to Copernicus.

        Returns (dem_path, source_actually_used) so the caller can tell the user
        which source produced the map (e.g. whether a request fell back).
        """
        if dem_source not in DEM_SOURCES:
            dem_source = 'COP90'

        if dem_source in LIDAR_SOURCES:
            area_deg2 = abs((north - south) * (east - west))
            if area_deg2 > OSM_MAX_AREA_DEG2:
                print(f"Warning: {dem_source} over a large area "
                      f"({area_deg2:.1f} deg^2) will be slow and heavy; "
                      f"it downloads in tiles.")

            ladder = LIDAR_LADDER[LIDAR_LADDER.index(dem_source):] \
                if dem_source in LIDAR_LADDER else [dem_source]

            # Skip rungs finer than the output can show: the coarsest rung
            # whose native resolution still saturates the output is the right
            # starting point (identical output, far smaller download).
            while (len(ladder) > 1 and effective_mpp
                   and LIDAR_RES_M[ladder[1]] <= effective_mpp):
                print(f"Output is ~{effective_mpp:.0f} m/px, which "
                      f"{ladder[1]} ({LIDAR_RES_M[ladder[1]]:.0f}m) already "
                      f"saturates — skipping {ladder[0]}.")
                ladder.pop(0)

            for rung in ladder:
                try:
                    path = self._download_dem_source(south, west, north, east, rung)
                    print(f"Using {rung} for this area.")
                    return path, rung
                except NoCoverageError as e:
                    print(f"{rung} unavailable ({e}); trying next resolution.")
                except Exception as e:
                    if "too large" in str(e).lower():
                        print(f"{rung} download too large for this area ({e}); "
                              f"trying next resolution.")
                    else:
                        raise
            print(f"All USGS sources unavailable; "
                  f"falling back to {DEM_FALLBACK_SOURCE}.")
            path = self._download_dem_source(
                south, west, north, east, DEM_FALLBACK_SOURCE
            )
            return path, DEM_FALLBACK_SOURCE

        path = self._download_dem_source(south, west, north, east, dem_source)
        return path, dem_source

    def _download_dem_source(self, south: float, west: float, north: float,
                             east: float, dem_source: str) -> str:
        """Download+mosaic all tiles for one specific DEM source."""
        tiles = self._download_dem_tiles(south, west, north, east, dem_source, depth=0)
        if not tiles:
            raise Exception("Failed to download DEM: no tiles returned")
        if len(tiles) == 1:
            return tiles[0]

        print(f"Mosaicking {len(tiles)} DEM tile(s)...")
        merged_path = os.path.join(
            self.cache_dir,
            f"dem_{dem_source}_{uuid.uuid4().hex}_merged.tif",
        )
        self._merge_tiles(tiles, merged_path)
        # The individual tiles fed the mosaic and are never reused.
        for t in tiles:
            try:
                os.remove(t)
            except OSError:
                pass
        return merged_path

    def _download_dem_tiles(self, south: float, west: float, north: float,
                            east: float, dem_source: str, depth: int) -> list:
        """Return paths to one or more DEM GeoTIFF tiles covering the bbox.

        Tries a single request; on an "area too large" rejection, subdivides
        into quadrants (up to MAX_TILE_DEPTH) and fetches each. A NoCoverageError
        propagates so the caller can fall back to a different source.

        Tile files get unique (uuid) names: the server handles requests on
        multiple threads, and bbox-derived names were never actually reused as
        a cache (every call re-downloads) — shared names only meant two
        concurrent generations of the same area could overwrite each other's
        tile mid-read, or delete a file the other thread still needed.
        """
        content = self._request_dem(south, west, north, east, dem_source)
        if content is not None:
            tile_path = os.path.join(
                self.cache_dir,
                f"dem_{dem_source}_{uuid.uuid4().hex}.tif",
            )
            with open(tile_path, 'wb') as f:
                f.write(content)
            return [tile_path]

        # Request was rejected as too large. Subdivide if we still can.
        if depth >= MAX_TILE_DEPTH:
            raise Exception(
                "Area is too large for this detail level, even after tiling. "
                "Try a smaller area or a coarser DEM (e.g. Copernicus 90m)."
            )

        mid_lat = (south + north) / 2.0
        mid_lon = (west + east) / 2.0
        quadrants = [
            (south, west, mid_lat, mid_lon),
            (south, mid_lon, mid_lat, east),
            (mid_lat, west, north, mid_lon),
            (mid_lat, mid_lon, north, east),
        ]
        print(f"DEM area too large at depth {depth}; splitting into 4 tiles...")
        tiles = []
        for (s, w, n, e) in quadrants:
            tiles.extend(self._download_dem_tiles(s, w, n, e, dem_source, depth + 1))
            time.sleep(1)  # be polite to the OpenTopography API between tiles
        return tiles

    def _request_dem(self, south: float, west: float, north: float, east: float,
                     dem_source: str):
        """Fetch one DEM tile. Returns bytes on success, None if the area was
        rejected as too large (caller should subdivide), raises NoCoverageError
        if the source has no data for the area, raises Exception on other errors.
        """
        url, param_name, param_value = DEM_SOURCES[dem_source]
        params = {
            param_name: param_value,
            'south': south,
            'north': north,
            'west': west,
            'east': east,
            'outputFormat': 'GTiff',
        }
        if self.api_key:
            params['API_Key'] = self.api_key

        print(f"Requesting DEM from OpenTopography ({dem_source}) "
              f"[{south:.3f},{west:.3f},{north:.3f},{east:.3f}]...")
        response = requests.get(url, params=params, timeout=300)

        if response.status_code == 200:
            return response.content

        # OpenTopography returns 400 with an "area exceeds" style message when
        # the request is too large. Signal the caller to tile instead of failing.
        text = (response.text or "")[:300]
        low = text.lower()
        too_large = response.status_code == 400 and any(
            kw in low for kw in ("exceed", "too large", "maximum", "area")
        )
        if too_large:
            print(f"OpenTopography rejected area as too large: {text}")
            return None

        # USGS 3DEP LiDAR is fallback-eligible in two cases: no coverage for
        # the area (it only covers parts of the US), or access denied —
        # OpenTopography restricts the 1m dataset to academic accounts, and
        # 10m/30m still need a valid (free) API key. Either way the map can
        # still be made from a global source, so signal the caller to fall
        # back rather than failing the whole generation.
        if dem_source in LIDAR_SOURCES:
            if response.status_code in (401, 403):
                raise NoCoverageError(
                    f"access denied (HTTP {response.status_code}) — USGS 1m "
                    f"requires an academic OpenTopography account; 10m/30m "
                    f"need a valid free API key"
                )
            no_coverage = (
                response.status_code in (204, 404)
                or any(kw in low for kw in ("no data", "not available", "no dem",
                                            "outside", "coverage", "not found"))
                or not response.content
            )
            if no_coverage:
                raise NoCoverageError(f"{response.status_code}: {text[:120]}")

        raise Exception(f"Failed to download DEM: {response.status_code} - {text}")

    def _merge_tiles(self, tile_paths: list, out_path: str) -> None:
        """Mosaic multiple DEM GeoTIFF tiles into a single file."""
        srcs = [rasterio.open(p) for p in tile_paths]
        try:
            mosaic, out_transform = merge(srcs)
            meta = srcs[0].meta.copy()
            meta.update({
                'height': mosaic.shape[1],
                'width': mosaic.shape[2],
                'transform': out_transform,
            })
            with rasterio.open(out_path, 'w', **meta) as dst:
                dst.write(mosaic)
        finally:
            for s in srcs:
                s.close()
    
    def _load_dem(self, dem_path: str, target_width: int, target_height: int,
                  grid: "GeoGrid" = None, fill_nodata: bool = True) -> tuple:
        """Load DEM and resample to target size.

        When a (rotated) GeoGrid is supplied, the DEM is resampled directly
        onto the rotated pixel grid via its affine transform, so the output
        image's axes run along the rotated selection box.

        Handles two things a naive resample gets wrong:
        - NoData: sentinel values (-999999 / +3.4e38) must never leak into the
          elevation stretch (they render as bogus max-height "mountains").
          They are always masked; what replaces them depends on `fill_nodata`:
          * fill_nodata=True — fill from the nearest valid cell. Correct for
            global, fully-covered sources (GEBCO/Copernicus) whose nodata is
            incidental.
          * fill_nodata=False — leave NaN and let the CALLER decide. Required
            for land-only products (USGS 3DEP), where nodata *means* "not
            land" (ocean, large water bodies) as well as project-seam voids;
            the caller blends a global DEM into the NaN cells so voids get
            real terrain and the ocean gets real sea level.
        - Downsampling quality: high-res sources (1m LiDAR) are typically
          downsampled 10-100x to the output size. Bilinear only samples a 2x2
          neighbourhood, skipping most of the source data (aliasing); use
          average resampling when downsampling so all the detail contributes.
        """
        with rasterio.open(dem_path) as src:
            if grid is not None:
                # Resample straight onto the (possibly rotated) selection grid.
                transform_new = grid.affine()
            else:
                # Calculate new transform for target size
                transform_new, width_new, height_new = calculate_default_transform(
                    src.crs, src.crs,
                    src.width, src.height,
                    *src.bounds,
                    dst_width=target_width,
                    dst_height=target_height
                )

            # Average when downsampling significantly (fine source -> coarse
            # output), bilinear when at similar scale or upsampling.
            scale = src.width / max(1, target_width)
            resampling = Resampling.average if scale > 2 else Resampling.bilinear

            data = np.full((target_height, target_width), np.nan, dtype=np.float32)
            reproject(
                source=rasterio.band(src, 1),
                destination=data,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform_new,
                dst_crs=src.crs,
                src_nodata=src.nodata,
                dst_nodata=np.nan,
                resampling=resampling
            )

            # Mask anything non-finite or physically impossible (nodata
            # sentinels that slipped through, e.g. +/-3.4e38 or -999999).
            invalid = ~np.isfinite(data) | (np.abs(data) > 12000)
            n_invalid = int(invalid.sum())
            if n_invalid == data.size:
                raise Exception("DEM contains no valid elevation data")
            if n_invalid:
                data[invalid] = np.nan
                if fill_nodata:
                    try:
                        from scipy.ndimage import distance_transform_edt
                        idx = distance_transform_edt(
                            invalid, return_distances=False, return_indices=True
                        )
                        data = data[tuple(idx)]
                        print(f"DEM: filled {n_invalid} nodata cell(s) "
                              f"({100.0 * n_invalid / data.size:.1f}%) from "
                              f"nearest valid")
                    except ImportError:
                        data = np.where(invalid, 0.0, data)
                else:
                    print(f"DEM: {n_invalid} nodata cell(s) "
                          f"({100.0 * n_invalid / data.size:.1f}%) left for "
                          f"caller to blend")

            return data, transform_new, src.crs
    
    # Land palette colours (the >0 ramp entries), ordered plains -> peak. Tier
    # index ranges: plains 0..9, highlands 10..19, mountains 20..(K-1).
    @property
    def _land_colors(self):
        return np.array(
            [c for (v, c, _l) in DEM_COLOR_RAMP if v > 0], dtype=np.uint8
        )

    def _terrain_indices(self, vals, K, plains_frac, highland_frac):
        """Map land elevations -> palette indices.

        Natural mode (fracs None): linear min->max stretch (reflects the
        region's real elevation histogram). Custom mode: quantile mapping to the
        requested plains/highlands/mountains fractions, preserving relief order.
        """
        if plains_frac is not None and highland_frac is not None:
            P = max(0.0, min(1.0, plains_frac))
            H = max(0.0, min(1.0 - P, highland_frac))
            M = max(1e-6, 1.0 - P - H)
            n = vals.size
            ranks = np.empty(n, dtype=np.int64)
            ranks[np.argsort(vals, kind="stable")] = np.arange(n)
            r = ranks / max(1, n - 1)
            idx = np.empty(n, dtype=np.int32)
            pl = r < P
            hl = (r >= P) & (r < P + H)
            mt = r >= P + H
            if P > 0:
                idx[pl] = np.round((r[pl] / P) * 9).astype(np.int32)
            if H > 0:
                idx[hl] = (10 + np.round(((r[hl] - P) / H) * 9)).astype(np.int32)
            else:
                idx[hl] = 10
            idx[mt] = (
                20 + np.round(((r[mt] - P - H) / M) * (K - 1 - 20))
            ).astype(np.int32)
            print(
                f"Terrain mix (custom): {round(P*100)}% plains / "
                f"{round(H*100)}% highlands / {round(M*100)}% mountains"
            )
        else:
            # Percentile clip keeps a few outlier peaks/pits from squashing the
            # whole ramp. 1/99 (vs the old 2/98) preserves more distinction among
            # high peaks so mountains don't all flatten to the same max level.
            lo = float(np.percentile(vals, 1))
            hi = float(np.percentile(vals, 99))
            # Minimum-relief floor: a nearly-flat region has a tiny lo..hi span,
            # which the stretch would blow up into fake mountains. Hold the span
            # at >= MIN_RELIEF_M so genuinely flat land stays plains.
            MIN_RELIEF_M = 50.0
            if hi - lo < MIN_RELIEF_M:
                hi = lo + MIN_RELIEF_M
            norm = np.clip((vals - lo) / (hi - lo), 0.0, 1.0)
            # A pure relative stretch fabricates terrain classes: 120m coastal
            # hills would reach the top of the ramp and render as snow-white
            # mountain tiles in game. Cap the highest reachable palette index
            # by what the terrain actually is — the max of:
            #  - an absolute ceiling: where the region's highest ground falls
            #    on the palette's own elevation ramp (DEM_COLOR_RAMP: plains
            #    to 300m, highlands to 1500m, mountains to 5000m), so a high
            #    plateau (e.g. 4500m with 400m of local relief) still gets
            #    mountain colors; and
            #  - a relief ceiling: regions with big local relief read as
            #    mountainous regardless of absolute height (full alpine range
            #    at ~2000m of relief).
            # Within that ceiling the relative stretch keeps local contrast.
            ramp_elevs = [v for (v, _c, _l) in DEM_COLOR_RAMP if v > 0]
            abs_ceiling = float(np.interp(hi, ramp_elevs, np.arange(K)))
            relief = hi - lo
            relief_ceiling = 9.0 + (K - 1 - 9.0) * min(1.0, relief / 2000.0)
            ceiling = min(K - 1.0, max(abs_ceiling, relief_ceiling))
            idx = np.round(norm * ceiling).astype(np.int32)
            print(
                f"Terrain mix (natural): elevation {lo:.0f}m..{hi:.0f}m "
                f"stretched, palette ceiling {ceiling:.0f}/{K - 1} "
                f"(absolute {abs_ceiling:.0f}, relief {relief_ceiling:.0f})"
            )
        return np.clip(idx, 0, K - 1)

    def _apply_palette(self, dem_array: np.ndarray, dynamic_scale: bool = True,
                       plains_frac: float = None,
                       highland_frac: float = None) -> Image.Image:
        """Initial colouring: DEM water (<=0) + land by terrain mix.

        The terrain mix is re-applied later over the FINAL land (after rivers are
        drawn) via _recolor_land, so percentages reflect the playable land.
        """
        height, width = dem_array.shape
        rgba = np.zeros((height, width, 4), dtype=np.uint8)
        rgba[:, :, 3] = 255

        land = dem_array > SEA_LEVEL_EPS_M
        wc = DEM_COLOR_RAMP[0][1]
        rgba[~land, 0], rgba[~land, 1], rgba[~land, 2] = wc[0], wc[1], wc[2]

        if np.any(land):
            land_colors = self._land_colors
            vals = dem_array[land].astype(np.float32)
            idx = self._terrain_indices(vals, len(land_colors), plains_frac, highland_frac)
            cols = land_colors[idx]
            rgba[land, 0] = cols[:, 0]
            rgba[land, 1] = cols[:, 1]
            rgba[land, 2] = cols[:, 2]

        return Image.fromarray(rgba, "RGBA")

    def _recolor_land(self, image: Image.Image, dem_array: np.ndarray,
                      plains_frac: float, highland_frac: float) -> Image.Image:
        """Re-apply the terrain mix over the FINAL land (non-water) pixels.

        Rivers/lakes occupy the lowest ground, so applying the mix only after
        water is placed makes custom percentages (e.g. 25/65/10) reflect the
        actual playable land instead of being eaten into by rivers.
        """
        arr = np.array(image.convert("RGBA"))
        b = arr[:, :, 2].astype(int)
        a = arr[:, :, 3]
        water = (a < 20) | (b == 106)
        land = (~water) & (dem_array > SEA_LEVEL_EPS_M)
        if not np.any(land):
            return image
        land_colors = self._land_colors
        vals = dem_array[land].astype(np.float32)
        idx = self._terrain_indices(vals, len(land_colors), plains_frac, highland_frac)
        cols = land_colors[idx]
        arr[land, 0] = cols[:, 0]
        arr[land, 1] = cols[:, 1]
        arr[land, 2] = cols[:, 2]
        return Image.fromarray(arr, "RGBA")
    
    def _overpass_query(self, query: str):
        """POST a query to Overpass with on-disk caching + endpoint fallback.

        Returns the parsed JSON dict, or None on failure.
        """
        import hashlib

        cache_dir = os.path.join(self.cache_dir, "osm")
        os.makedirs(cache_dir, exist_ok=True)
        key = hashlib.md5(query.encode("utf-8")).hexdigest()
        cache_file = os.path.join(cache_dir, key + ".json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        headers = {"User-Agent": "OpenFrontMapGenerator/1.0 (local map tool)"}
        for url in OVERPASS_ENDPOINTS:
            try:
                print(f"Querying Overpass: {url}")
                resp = requests.post(
                    url, data={"data": query}, headers=headers, timeout=90
                )
                if resp.status_code == 200:
                    data = resp.json()
                    try:
                        # Atomic write: the server handles requests on multiple
                        # threads; a direct json.dump could leave a concurrent
                        # reader a half-written file. os.replace is atomic.
                        tmp = cache_file + f".{uuid.uuid4().hex}.tmp"
                        with open(tmp, "w", encoding="utf-8") as f:
                            json.dump(data, f)
                        os.replace(tmp, cache_file)
                    except Exception:
                        pass
                    return data
                print(f"Overpass {url} returned HTTP {resp.status_code}")
            except Exception as e:
                print(f"Overpass {url} failed: {e}")
        return None

    def _add_osm_water(self, image: Image.Image, south: float, west: float,
                       north: float, east: float, width: int, height: int,
                       grid: "GeoGrid" = None) -> int:
        """Overlay real rivers/lakes from OpenStreetMap (Overpass) onto the image.

        Returns the number of water features drawn (0 if none / on failure), so
        the caller can fall back to Natural Earth.
        """
        from PIL import ImageDraw

        if grid is None:
            grid = GeoGrid(south, west, north, east, width, height)

        water_color = (0, 0, 106)
        # Query the AABB of the (possibly rotated) selection; features outside
        # the rotated box map to off-image pixels and are clipped by PIL.
        (q_s, q_w, q_n, q_e) = grid.aabb()
        bbox = f"{q_s},{q_w},{q_n},{q_e}"
        # For larger areas, keep only major waterways so Overpass stays fast;
        # lakes (natural=water) are always included so real lakes are captured.
        area_deg2 = abs((north - south) * (east - west))
        if area_deg2 > OSM_MINOR_WATERWAY_MAX_DEG2:
            waterway_filter = "^(river|canal)$"
        else:
            waterway_filter = "^(river|stream|canal|drain|ditch)$"
        query = (
            "[out:json][timeout:60];"
            "("
            f'way["waterway"~"{waterway_filter}"]({bbox});'
            f'way["natural"="water"]({bbox});'
            f'relation["natural"="water"]({bbox});'
            f'way["water"]({bbox});'
            ");"
            "out geom;"
        )

        data = self._overpass_query(query)
        if not data:
            return 0
        elements = data.get("elements", [])
        if not elements:
            return 0

        # All water is drawn onto a 1-bit mask first, then composited onto the
        # image in one pass. This lets multipolygon relations subtract their
        # inner rings (islands inside bays/lakes stay land) — impossible when
        # painting water directly over the image.
        mask = Image.new("L", (image.width, image.height), 0)
        draw = ImageDraw.Draw(mask)

        to_px = grid.lonlat_to_pixel

        river_w = max(2, width // 400)
        small_w = max(1, width // 900)

        def draw_line(geom, w):
            pts = [to_px(p["lon"], p["lat"]) for p in geom]
            if len(pts) < 2:
                return False
            draw.line(pts, fill=255, width=w, joint="curve")
            # Round caps at each vertex so sharp bends stay connected.
            r = w // 2
            if r:
                for (x, y) in pts:
                    draw.ellipse([x - r, y - r, x + r, y + r], fill=255)
            return True

        def draw_poly(geom):
            pts = [to_px(p["lon"], p["lat"]) for p in geom]
            if len(pts) < 3:
                return False
            draw.polygon(pts, fill=255)
            return True

        def member_polygons(el, role):
            """Stitch a relation's member ways of one role into CLOSED rings.

            Members arrive as arbitrary unordered segments (a bay outer
            boundary can be hundreds of ways). polygonize() yields only valid
            closed rings; open leftovers are dropped entirely — filling them
            produces the triangular spike / perimeter-ring artifacts.
            """
            from shapely.ops import polygonize
            lines = []
            for m in el.get("members", []):
                if m.get("role") == role and m.get("geometry"):
                    coords = [(p["lon"], p["lat"]) for p in m["geometry"]]
                    if len(coords) >= 2:
                        lines.append(LineString(coords))
            if not lines:
                return []
            try:
                merged = linemerge(unary_union(lines))
                return [list(p.exterior.coords) for p in polygonize(merged)]
            except Exception:
                return []

        def draw_relation(el):
            """Fill a multipolygon water relation: outer rings become water,
            inner rings (islands) are punched back out of the mask."""
            outers = member_polygons(el, "outer")
            n = 0
            for ring in outers:
                pts = [to_px(x, y) for (x, y) in ring]
                if len(pts) >= 3:
                    draw.polygon(pts, fill=255)
                    n += 1
            if n:
                for ring in member_polygons(el, "inner"):
                    pts = [to_px(x, y) for (x, y) in ring]
                    if len(pts) >= 3:
                        draw.polygon(pts, fill=0)
            return n

        drawn = 0
        # Relations first, ways after, so a small closed water way (pond on an
        # island) isn't erased by its surrounding relation's inner ring.
        for el in elements:
            if el.get("type") == "relation":
                drawn += draw_relation(el)
        for el in elements:
            if el.get("type") != "way":
                continue
            tags = el.get("tags", {}) or {}
            geom = el.get("geometry")
            if not geom:
                continue
            if tags.get("waterway"):
                w = river_w if tags.get("waterway") == "river" else small_w
                if draw_line(geom, w):
                    drawn += 1
            elif draw_poly(geom):
                drawn += 1

        # Composite the water mask onto the image in one pass.
        image.paste(water_color + (255,), mask=mask)

        print(f"OSM water: drew {drawn} feature(s) from {len(elements)} element(s)")
        return drawn

    def _add_water_features(self, image: Image.Image, south: float, west: float,
                            north: float, east: float, width: int, height: int,
                            grid: "GeoGrid" = None) -> Image.Image:
        """Add rivers and lakes to the image."""

        if grid is None:
            grid = GeoGrid(south, west, north, east, width, height)

        # Water color from palette
        water_color = (0, 0, 106, 255)  # Ocean blue

        # Clip shapefiles to the AABB of the (possibly rotated) selection;
        # geometry outside the rotated box lands off-image and PIL clips it.
        (q_s, q_w, q_n, q_e) = grid.aabb()

        # Try to get Natural Earth data
        try:
            # Download and cache rivers/lakes shapefiles
            rivers_path = self._get_ne_shapefile(NE_RIVERS_URL, 'rivers')
            lakes_path = self._get_ne_shapefile(NE_LAKES_URL, 'lakes')

            if rivers_path or lakes_path:
                from PIL import ImageDraw
                draw = ImageDraw.Draw(image)

                world_to_pixel = grid.lonlat_to_pixel

                # Draw lakes
                if lakes_path:
                    self._draw_polygons(draw, lakes_path, q_s, q_w, q_n, q_e,
                                       world_to_pixel, water_color[:3])

                # Draw rivers
                if rivers_path:
                    river_width = max(1, int(width / 1000))
                    self._draw_lines(draw, rivers_path, q_s, q_w, q_n, q_e,
                                    world_to_pixel, water_color[:3], river_width)

        except Exception as e:
            print(f"Warning: Could not add water features: {e}")

        return image
    
    # Serializes first-time Natural Earth downloads: the threaded server could
    # otherwise have two requests extract the same zip into the same directory
    # concurrently (partial shapefiles for one of them). Class-level so all
    # MapProcessor instances (one per request) share it.
    _ne_download_lock = threading.Lock()

    def _get_ne_shapefile(self, url: str, name: str) -> str:
        """Download and cache a Natural Earth shapefile."""

        cache_dir = os.path.join(self.cache_dir, 'natural_earth', name)

        with MapProcessor._ne_download_lock:
            # Check if already cached
            if os.path.exists(cache_dir):
                for f in os.listdir(cache_dir):
                    if f.endswith('.shp'):
                        return os.path.join(cache_dir, f)

            # Download
            try:
                os.makedirs(cache_dir, exist_ok=True)
                print(f"Downloading {name} from Natural Earth...")
                response = requests.get(url, timeout=120)

                if response.status_code == 200:
                    # Extract ZIP
                    zip_path = os.path.join(cache_dir, f'{name}.zip')
                    with open(zip_path, 'wb') as f:
                        f.write(response.content)

                    with zipfile.ZipFile(zip_path, 'r') as zf:
                        zf.extractall(cache_dir)

                    os.remove(zip_path)

                    # Find .shp file
                    for f in os.listdir(cache_dir):
                        if f.endswith('.shp'):
                            return os.path.join(cache_dir, f)

            except Exception as e:
                print(f"Warning: Could not download {name}: {e}")

        return None
    
    def _draw_polygons(self, draw, shp_path: str, south: float, west: float,
                       north: float, east: float, world_to_pixel, color):
        """Draw polygon features from a shapefile."""
        try:
            import fiona
            
            bounds_box = box(west, south, east, north)
            
            with fiona.open(shp_path, 'r') as src:
                for feature in src:
                    try:
                        geom = shape(feature['geometry'])
                        if not geom.intersects(bounds_box):
                            continue
                        
                        clipped = geom.intersection(bounds_box)
                        if clipped.is_empty:
                            continue
                        
                        # Draw polygon(s)
                        self._draw_geometry(draw, clipped, world_to_pixel, color)
                    except Exception:
                        continue
        except ImportError:
            print("Warning: fiona not available for shapefile reading")
        except Exception as e:
            print(f"Warning: Error reading shapefile: {e}")
    
    def _draw_lines(self, draw, shp_path: str, south: float, west: float,
                    north: float, east: float, world_to_pixel, color, width: int):
        """Draw line features from a shapefile."""
        try:
            import fiona
            
            bounds_box = box(west, south, east, north)
            
            with fiona.open(shp_path, 'r') as src:
                for feature in src:
                    try:
                        # Filter by scalerank if available
                        props = feature.get('properties', {})
                        scalerank = props.get('scalerank', 0)
                        if scalerank is not None and scalerank > 3:
                            continue
                        
                        geom = shape(feature['geometry'])
                        if not geom.intersects(bounds_box):
                            continue
                        
                        clipped = geom.intersection(bounds_box)
                        if clipped.is_empty:
                            continue
                        
                        # Draw line(s)
                        self._draw_line_geometry(draw, clipped, world_to_pixel, color, width)
                    except Exception:
                        continue
        except ImportError:
            print("Warning: fiona not available for shapefile reading")
        except Exception as e:
            print(f"Warning: Error reading shapefile: {e}")
    
    def _draw_geometry(self, draw, geom, world_to_pixel, color):
        """Draw a shapely geometry as a filled polygon."""
        if geom.geom_type == 'Polygon':
            coords = [world_to_pixel(x, y) for x, y in geom.exterior.coords]
            if len(coords) >= 3:
                draw.polygon(coords, fill=color)
        elif geom.geom_type == 'MultiPolygon':
            for poly in geom.geoms:
                self._draw_geometry(draw, poly, world_to_pixel, color)
    
    def _draw_line_geometry(self, draw, geom, world_to_pixel, color, width):
        """Draw a shapely geometry as a line."""
        if geom.geom_type == 'LineString':
            coords = [world_to_pixel(x, y) for x, y in geom.coords]
            if len(coords) >= 2:
                draw.line(coords, fill=color, width=width)
        elif geom.geom_type == 'MultiLineString':
            for line in geom.geoms:
                self._draw_line_geometry(draw, line, world_to_pixel, color, width)
    
    def _dominant_flag(self, points):
        """Most common (non-placeholder) flag among points, else 'xx'."""
        from collections import Counter

        flags = [
            p.get("flag")
            for p in points
            if p.get("flag") and p.get("flag") != "xx"
        ]
        if not flags:
            return "xx"
        return Counter(flags).most_common(1)[0][0]

    def _osm_places(self, south, west, north, east):
        """Query OSM place nodes (cities/towns/villages...) in a bbox.

        Returns a list of {name, place, lat, lon, pop}, most-prominent first.
        """
        bbox = f"{south},{west},{north},{east}"
        types = "|".join(OSM_PLACE_TYPES)
        query = (
            "[out:json][timeout:60];"
            f'node["place"~"^({types})$"]({bbox});'
            "out body;"
        )
        data = self._overpass_query(query)
        if not data:
            return []
        rank = {t: i for i, t in enumerate(OSM_PLACE_TYPES)}
        out = []
        for el in data.get("elements", []):
            tags = el.get("tags", {}) or {}
            name = tags.get("name")
            if not name or "lat" not in el or "lon" not in el:
                continue
            try:
                pop = int(tags.get("population", "0") or "0")
            except ValueError:
                pop = 0
            out.append({
                "name": name,
                "place": tags.get("place", "town"),
                "lat": el["lat"],
                "lon": el["lon"],
                "pop": pop,
            })
        out.sort(key=lambda p: (rank.get(p["place"], 99), -p["pop"]))
        return out

    def _get_osm_place_points(self, south, west, north, east, width, height,
                              need, existing, grid: "GeoGrid" = None):
        """In-bbox OSM place names as nation spawns (with real coordinates).

        Returns up to `need` points {name, flag, pixel_x, pixel_y}, skipping
        names already present in `existing`.
        """
        if grid is None:
            grid = GeoGrid(south, west, north, east, width, height)
        (q_s, q_w, q_n, q_e) = grid.aabb()
        flag = self._dominant_flag(existing)
        used = {p.get("name", "").lower() for p in existing}
        points = []
        for pl in self._osm_places(q_s, q_w, q_n, q_e):
            if len(points) >= need:
                break
            nm = pl["name"]
            if nm.lower() in used:
                continue
            px, py = grid.lonlat_to_pixel(pl["lon"], pl["lat"])
            if px < 0 or px >= width or py < 0 or py >= height:
                continue
            used.add(nm.lower())
            points.append(
                {"name": nm, "flag": flag, "pixel_x": px, "pixel_y": py}
            )
        print(f"OSM places: added {len(points)} in-area named spawn(s)")
        return points

    def _get_nearby_place_names(self, south, west, north, east, existing, need):
        """Place names from a wider surrounding area (name pool, no coordinates).

        Tops up tiny regions that contain few labeled places. Returns a list of
        {name, flag}.
        """
        flag = self._dominant_flag(existing)
        used = {p.get("name", "").lower() for p in existing}
        dh0, dw0 = (north - south), (east - west)
        names = []
        # Progressively wider rings, so even remote wilderness eventually picks
        # up regional town names.
        for mult in (1, 3, 8):
            if len(names) >= need:
                break
            dh, dw = dh0 * mult, dw0 * mult
            for pl in self._osm_places(
                south - dh, west - dw, north + dh, east + dw
            ):
                if len(names) >= need:
                    break
                nm = pl["name"]
                if nm.lower() in used:
                    continue
                used.add(nm.lower())
                names.append({"name": nm, "flag": flag})
        print(f"OSM nearby: added {len(names)} fallback name(s)")
        return names

    def _get_province_points(self, south: float, west: float, north: float, east: float,
                             width: int, height: int, max_provinces: int = 20,
                             grid: "GeoGrid" = None) -> list:
        """Get province/country center points."""

        if grid is None:
            grid = GeoGrid(south, west, north, east, width, height)
        (south, west, north, east) = grid.aabb()

        points = []

        world_to_pixel = grid.lonlat_to_pixel

        try:
            # Try countries first (for world/continent maps)
            admin0_path = self._get_ne_shapefile(NE_ADMIN0_URL, 'admin0')
            if admin0_path:
                country_points = self._extract_points_from_shapefile(
                    admin0_path, south, west, north, east, world_to_pixel,
                    name_fields=['NAME', 'NAME_EN', 'ADMIN'],
                    flag_fields=['ISO_A2', 'ISO_A2_EH'],
                    is_country=True, width=width, height=height
                )
                
                if len(country_points) >= 3:
                    # Use countries
                    points = sorted(country_points, key=lambda x: x.get('area', 0), reverse=True)
                    points = points[:max_provinces]
        except Exception as e:
            print(f"Warning: Could not load countries: {e}")
        
        # Fall back to provinces/states
        if len(points) < 3:
            try:
                admin1_path = self._get_ne_shapefile(NE_ADMIN1_URL, 'admin1')
                if admin1_path:
                    province_points = self._extract_points_from_shapefile(
                        admin1_path, south, west, north, east, world_to_pixel,
                        name_fields=['name', 'name_en', 'name_1'],
                        flag_fields=['iso_3166_2', 'adm1_code'],
                        is_country=False, width=width, height=height
                    )
                    points = sorted(province_points, key=lambda x: x.get('area', 0), reverse=True)
                    points = points[:max_provinces]
            except Exception as e:
                print(f"Warning: Could not load provinces: {e}")
        
        # Clean up points for output
        cleaned_points = []
        for p in points:
            cleaned_points.append({
                'name': p['name'],
                'flag': p.get('flag', 'xx'),
                'pixel_x': p['pixel_x'],
                'pixel_y': p['pixel_y']
            })
        
        return cleaned_points
    
    def _extract_points_from_shapefile(self, shp_path: str, south: float, west: float,
                                        north: float, east: float, world_to_pixel,
                                        name_fields: list, flag_fields: list,
                                        is_country: bool, width: int = None,
                                        height: int = None) -> list:
        """Extract center points from a shapefile."""
        
        points = []
        
        try:
            import fiona
            
            bounds_box = box(west, south, east, north)
            
            with fiona.open(shp_path, 'r') as src:
                for feature in src:
                    try:
                        geom = shape(feature['geometry'])
                        if not geom.intersects(bounds_box):
                            continue
                        
                        clipped = geom.intersection(bounds_box)
                        if clipped.is_empty:
                            continue
                        
                        # Get center point
                        if hasattr(clipped, 'representative_point'):
                            center = clipped.representative_point()
                        else:
                            center = clipped.centroid
                        
                        if center.is_empty:
                            continue
                        
                        # Get name
                        props = feature.get('properties', {})
                        name = None
                        for field in name_fields:
                            val = props.get(field)
                            if val:
                                name = str(val)
                                break
                        
                        if not name:
                            continue
                        
                        # Get flag code
                        flag = 'xx'
                        for field in flag_fields:
                            val = props.get(field)
                            if val:
                                flag = str(val).lower().split('-')[0]
                                break
                        
                        # Calculate pixel coords
                        px, py = world_to_pixel(center.x, center.y)

                        # Skip if outside image bounds. (For rotated grids the
                        # old corner-derived bounds trick is wrong; use the
                        # actual image dimensions.)
                        max_x = width if width else world_to_pixel(east, south)[0]
                        max_y = height if height else world_to_pixel(west, south)[1]
                        if px < 0 or px >= max_x:
                            continue
                        if py < 0 or py >= max_y:
                            continue
                        
                        # Estimate area
                        area = clipped.area
                        
                        points.append({
                            'name': name,
                            'flag': flag,
                            'pixel_x': px,
                            'pixel_y': py,
                            'area': area
                        })
                    
                    except Exception:
                        continue
        
        except ImportError:
            print("Warning: fiona not available")
        except Exception as e:
            print(f"Warning: Error extracting points: {e}")
        
        return points

    # =========================================================================
    # Game File Generation (map.bin, map4x.bin, map16x.bin, manifest.json, thumbnail.webp)
    # =========================================================================
    
    def _generate_game_files(self, styled_image: Image.Image, base_name: str, points: list, extra_names: list = None, depth_array=None) -> list:
        """
        Generate OpenFront game files from the styled image.
        
        Returns:
            List of generated file names
        """
        # Convert image to numpy array
        img = styled_image.convert('RGBA')
        width, height = img.size
        
        # Normalize to a multiple of 8 so every downscale level (map=full/2,
        # map4x=full/4, map16x=full/8) has even dimensions. The game's minimap
        # lookup does miniMap.ref(floor(x/2), floor(y/2)); if a level's width is
        # odd, floor((W-1)/2) == half-width and indexes one column past the
        # minimap -> "Invalid coordinates" crash. Multiples of 4 (what OpenFront
        # uses) keep the full-res map.bin safe; we use 8 because our map.bin is
        # the half-scale level, so the full must be /8 to keep map4x even too.
        width -= width % 8
        height -= height % 8
        img = img.crop((0, 0, width, height))
        
        pixels = np.array(img)
        
        # Extract channels
        r = pixels[:, :, 0]
        g = pixels[:, :, 1]
        b = pixels[:, :, 2]
        a = pixels[:, :, 3]
        
        # Determine Type: Alpha < 20 or Blue == 106 -> Water
        terrain_type = np.where((a < 20) | (b == 106), TYPE_WATER, TYPE_LAND).astype(np.uint8)
        
        # Determine Magnitude: (Blue - 140) / 2, clamped 0-30
        mag_raw = np.clip(b.astype(float), 140, 200) - 140
        terrain_mag = mag_raw / 2.0
        
        terrain_shore = np.zeros((height, width), dtype=bool)
        terrain_ocean = np.zeros((height, width), dtype=bool)

        # Scale the "small feature" cleanup thresholds to the map's resolution so
        # they mean roughly the same real-world size regardless of map size. The
        # original absolute constants are kept as floors (so small maps behave as
        # before); only larger maps raise the bar. Fractions are calibrated so the
        # floors dominate up to ~10 megapixels.
        total_px = width * height
        min_island = max(MIN_ISLAND_SIZE, int(total_px * 6e-6))
        min_lake = max(MIN_LAKE_SIZE, int(total_px * 2e-5))

        # Remove small islands
        terrain_type, terrain_mag = self._remove_small_areas(
            terrain_type, terrain_mag, TYPE_LAND, min_island, TYPE_WATER
        )

        # Process water (identify oceans, remove small lakes, calc distances)
        terrain_type, terrain_mag, terrain_shore, terrain_ocean = self._process_water(
            terrain_type, terrain_mag, terrain_shore, terrain_ocean, min_lake
        )
        
        # Build LOD minimaps from the full-res terrain (L0):
        #   L1 (1/2) -> map4x.bin,  L2 (1/4) -> map16x.bin
        l1_type, l1_mag, l1_shore, l1_ocean = self._downscale_terrain(
            terrain_type, terrain_mag, terrain_shore, terrain_ocean
        )
        l2_type, l2_mag, l2_shore, l2_ocean = self._downscale_terrain(
            l1_type, l1_mag, l1_shore, l1_ocean
        )

        # Pack data: L0 = full-res (map.bin), L1/L2 = LOD minimaps.
        l0_data, l0_land = self._pack_terrain(terrain_type, terrain_mag, terrain_shore, terrain_ocean)
        l1_data, l1_land = self._pack_terrain(l1_type, l1_mag, l1_shore, l1_ocean)
        l2_data, l2_land = self._pack_terrain(l2_type, l2_mag, l2_shore, l2_ocean)
        
        # Save game files. map.bin holds the FULL-resolution (L0) terrain, with
        # map4x/map16x as the 1/2 and 1/4 LOD minimaps - matching the official Go
        # map-generator. (Previously this wrote L1/L2/L3, shipping a half-scale
        # map and discarding the full-res L0 it had already computed.)
        generated_files = []

        # Write binary files
        with open(os.path.join(self.output_dir, "map.bin"), "wb") as f:
            f.write(l0_data)
        generated_files.append("map.bin")

        # depth.bin: cosmetic per-tile ocean depth, aligned 1:1 with map.bin (L0).
        # 1 byte/tile, 0 = land/shore/shallow ... 255 = deepest. A perceptual
        # (sqrt) scale gives visible near-shore gradients. This is a render-only
        # sidecar the game colors water with; it never touches map.bin, so naval
        # pathfinding is unaffected. Only written when bathymetry was available.
        if depth_array is not None:
            l0_h, l0_w = terrain_type.shape
            depth_l0 = np.asarray(depth_array, dtype=np.float32)[:l0_h, :l0_w]
            if depth_l0.shape == terrain_type.shape:
                depth_norm = np.sqrt(np.clip(depth_l0 / DEPTH_MAX_M, 0.0, 1.0))
                depth_byte = np.round(depth_norm * 255).astype(np.uint8)
                depth_byte[terrain_type != TYPE_WATER] = 0  # depth only for water
                with open(os.path.join(self.output_dir, "depth.bin"), "wb") as f:
                    f.write(depth_byte.tobytes())
                generated_files.append("depth.bin")
                print(f"Wrote depth.bin (max depth "
                      f"{float(depth_l0.max()):.0f}m)")
            else:
                print(f"Skipping depth.bin: shape {depth_l0.shape} != "
                      f"terrain {terrain_type.shape}")

        with open(os.path.join(self.output_dir, "map4x.bin"), "wb") as f:
            f.write(l1_data)
        generated_files.append("map4x.bin")

        with open(os.path.join(self.output_dir, "map16x.bin"), "wb") as f:
            f.write(l2_data)
        generated_files.append("map16x.bin")

        # Generate and save thumbnail (from the 4x LOD = L1, like the Go pipeline)
        thumbnail = self._create_thumbnail(l1_type, l1_mag, l1_shore, 0.5)
        thumbnail.save(os.path.join(self.output_dir, "thumbnail.webp"), "WEBP")
        generated_files.append("thumbnail.webp")
        
        # Create manifest. Dimensions must match the .bin LODs:
        # map = L0 (full), map4x = L1 (1/2), map16x = L2 (1/4).
        l0_h, l0_w = terrain_type.shape
        l1_h, l1_w = l1_type.shape
        l2_h, l2_w = l2_type.shape

        manifest = {
            "name": base_name,
            "map": {
                "width": l0_w,
                "height": l0_h,
                "num_land_tiles": l0_land
            },
            "map4x": {
                "width": l1_w,
                "height": l1_h,
                "num_land_tiles": l1_land
            },
            "map16x": {
                "width": l2_w,
                "height": l2_h,
                "num_land_tiles": l2_land
            },
            "nations": []
        }
        
        # Add nations from points. pixel_x/pixel_y are in full-image (L0) space,
        # which is exactly what map.bin now uses - no scaling needed.
        for p in points:
            nation = {
                "name": p.get("name", "Unknown"),
                "flag": p.get("flag", "unknown"),
                "coordinates": [
                    int(p.get("pixel_x", 0)),
                    int(p.get("pixel_y", 0))
                ]
            }
            manifest["nations"].append(nation)

        # Fallback name pool (no coordinates) for when a game needs more nations
        # than the map defines - the game places these at random.
        if extra_names:
            manifest["additionalNations"] = [
                {"name": n.get("name", "Unknown"), "flag": n.get("flag", "xx")}
                for n in extra_names
            ]

        with open(os.path.join(self.output_dir, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        generated_files.append("manifest.json")
        
        return generated_files
    
    def _blend_nodata_regions(self, dem_array, nan_mask, blend_dem):
        """Fill the primary DEM's nodata cells from a blend DEM, deciding
        water vs missing-land PER CONNECTED NODATA REGION rather than per cell.

        Copernicus GLO-30 (the usual blend) is a radar SURFACE model: over
        near-shore water it carries both small noise (waves, water-mask
        misses) and full 10-25m canopy/building heights where its coarse 30m
        coastline disagrees with the primary DEM's — per-cell blending paints
        those as blocky land teeth and ghost bands along every shore, and no
        elevation floor can gate out canopy. Regions tell the truth: a nodata
        region that is mostly water in the blend (a bay) IS water — including
        its noisy fringe cells — while a region that is mostly land (a genuine
        coverage void over land) is filled from the blend as real terrain.
        """
        BLEND_NOISE_FLOOR_M = 3.0
        try:
            from scipy.ndimage import label as nd_label
        except ImportError:
            # scipy unavailable: per-cell gate (coarser, but bounded).
            blend_vals = np.where(
                blend_dem > BLEND_NOISE_FLOOR_M,
                blend_dem,
                np.minimum(blend_dem, 0.0),
            )
            return np.where(nan_mask, blend_vals, dem_array)

        regions, n_regions = nd_label(nan_mask)
        blend_land = blend_dem > BLEND_NOISE_FLOOR_M
        # Per-region land fraction via bincount (fast for many regions).
        counts = np.bincount(regions.ravel())
        land_counts = np.bincount(
            regions.ravel(), weights=blend_land.ravel().astype(np.float64)
        )
        frac = np.zeros_like(land_counts)
        nz = counts > 0
        frac[nz] = land_counts[nz] / counts[nz]
        fill_region = frac > 0.5   # region is mostly real land
        fill_mask = nan_mask & fill_region[regions]
        water_mask = nan_mask & ~fill_region[regions]
        out = np.where(fill_mask, blend_dem, dem_array)
        out = np.where(water_mask, np.minimum(blend_dem, 0.0), out)
        print(f"Blend: {n_regions} nodata region(s); "
              f"filled {int(fill_mask.sum())} cell(s) as land, "
              f"{int(water_mask.sum())} as water")
        return out

    def _space_out_spawns(self, points, width, height, min_frac=0.045):
        """De-cluster nation spawns so they aren't packed together.

        Greedily keeps points by prominence (largest area first), dropping any
        that fall within min_frac * map-diagonal of an already-kept point. This
        spreads nations out for more balanced starts. Never drops below
        NATION_MIN: if spacing would leave too few, the largest rejected points
        are added back.
        """
        if len(points) <= NATION_MIN:
            return points
        diag = math.hypot(width, height)
        min_dist_sq = (min_frac * diag) ** 2
        ordered = sorted(points, key=lambda p: p.get("area", 0) or 0, reverse=True)
        kept, rejected = [], []
        for p in ordered:
            px, py = p.get("pixel_x", 0), p.get("pixel_y", 0)
            far_enough = all(
                (px - q.get("pixel_x", 0)) ** 2 + (py - q.get("pixel_y", 0)) ** 2
                >= min_dist_sq
                for q in kept
            )
            (kept if far_enough else rejected).append(p)
        # Backfill to the NATION_MIN floor if spacing was too aggressive for the
        # available geography. Add rejects farthest from the kept set first
        # (farthest-point sampling) so even the fallback stays as spread as
        # possible rather than re-clustering.
        while len(kept) < NATION_MIN and rejected:
            best = max(
                rejected,
                key=lambda p: min(
                    (p.get("pixel_x", 0) - q.get("pixel_x", 0)) ** 2
                    + (p.get("pixel_y", 0) - q.get("pixel_y", 0)) ** 2
                    for q in kept
                ),
            )
            rejected.remove(best)
            kept.append(best)
        if len(kept) < len(points):
            print(
                f"Spawn spacing: kept {len(kept)}/{len(points)} spawns "
                f"(min gap {math.sqrt(min_dist_sq):.0f}px)"
            )
        return kept

    def _remove_small_areas(self, t_type, t_mag, target_type, min_size, replace_with):
        """Remove small islands or lakes."""
        try:
            from scipy.ndimage import label
            mask = (t_type == target_type)
            labeled, n_components = label(mask)
            
            sizes = np.bincount(labeled.ravel())
            small_labels = np.where((sizes < min_size) & (sizes > 0))[0]
            
            remove_mask = np.isin(labeled, small_labels)
            t_type = t_type.copy()
            t_mag = t_mag.copy()
            t_type[remove_mask] = replace_with
            t_mag[remove_mask] = 0
            
            print(f"Removed {len(small_labels)} areas smaller than {min_size}")
            
        except ImportError:
            print("scipy not found, skipping small area removal")
        
        return t_type, t_mag
    
    def _process_water(self, t_type, t_mag, t_shore, t_ocean, min_lake_size=MIN_LAKE_SIZE):
        """Process water bodies - identify oceans, remove small lakes, calc distances."""
        try:
            from scipy.ndimage import label, distance_transform_cdt, binary_dilation
            
            water_mask = (t_type == TYPE_WATER)
            labeled, n_components = label(water_mask)
            
            if n_components == 0:
                print("No water bodies found.")
                return t_type, t_mag, t_shore, t_ocean
            
            sizes = np.bincount(labeled.ravel())
            water_labels = np.arange(1, len(sizes))
            water_labels = water_labels[np.argsort(sizes[water_labels])[::-1]]
            
            # Largest is Ocean
            t_type = t_type.copy()
            t_mag = t_mag.copy()
            t_shore = t_shore.copy()
            t_ocean = t_ocean.copy()
            
            largest_label = water_labels[0]
            t_ocean[labeled == largest_label] = True
            print(f"Identified ocean with {sizes[largest_label]} tiles")
            
            # Remove small lakes
            small_labels = water_labels[sizes[water_labels] < min_lake_size]
            remove_mask = np.isin(labeled, small_labels)
            t_type[remove_mask] = TYPE_LAND
            t_mag[remove_mask] = 0
            print(f"Removed {len(small_labels)} lakes smaller than {min_lake_size}")
            
            water_mask = (t_type == TYPE_WATER)
            
            # Shoreline detection
            struct_4 = np.array([[0,1,0],[1,1,1],[0,1,0]], dtype=bool)
            land_mask = (t_type == TYPE_LAND)
            
            dilated_land = binary_dilation(land_mask, structure=struct_4)
            shore_water = dilated_land & water_mask
            
            dilated_water = binary_dilation(water_mask, structure=struct_4)
            shore_land = dilated_water & land_mask
            
            t_shore = shore_water | shore_land
            
            # Water magnitude (distance to land)
            dist = distance_transform_cdt(water_mask, metric='taxicab')
            water_mag = np.maximum(dist - 1, 0).astype(float)
            t_mag = np.where(water_mask, water_mag, t_mag)
            
        except ImportError:
            print("scipy not found, skipping water processing")
        
        return t_type, t_mag, t_shore, t_ocean
    
    def _downscale_terrain(self, t_type, t_mag, t_shore, t_ocean):
        """Downscale terrain by factor of 2."""
        h, w = t_type.shape
        if h % 2 != 0:
            h -= 1
        if w % 2 != 0:
            w -= 1
        
        t_type = t_type[:h, :w]
        t_mag = t_mag[:h, :w]
        t_shore = t_shore[:h, :w]
        t_ocean = t_ocean[:h, :w]
        
        # Get 2x2 blocks
        s00_type = t_type[0::2, 0::2]
        s01_type = t_type[1::2, 0::2]
        s10_type = t_type[0::2, 1::2]
        s11_type = t_type[1::2, 1::2]
        
        w00 = (s00_type == TYPE_WATER)
        w01 = (s01_type == TYPE_WATER)
        w10 = (s10_type == TYPE_WATER)
        
        # Start with s11
        mini_type = s11_type.copy()
        mini_mag = t_mag[1::2, 1::2].copy()
        mini_shore = t_shore[1::2, 1::2].copy()
        mini_ocean = t_ocean[1::2, 1::2].copy()
        
        # Priority: P00 > P01 > P10 > P11 (for Water)
        # P10
        mini_type[w10] = t_type[0::2, 1::2][w10]
        mini_mag[w10] = t_mag[0::2, 1::2][w10]
        mini_shore[w10] = t_shore[0::2, 1::2][w10]
        mini_ocean[w10] = t_ocean[0::2, 1::2][w10]
        
        # P01
        mini_type[w01] = t_type[1::2, 0::2][w01]
        mini_mag[w01] = t_mag[1::2, 0::2][w01]
        mini_shore[w01] = t_shore[1::2, 0::2][w01]
        mini_ocean[w01] = t_ocean[1::2, 0::2][w01]
        
        # P00
        mini_type[w00] = t_type[0::2, 0::2][w00]
        mini_mag[w00] = t_mag[0::2, 0::2][w00]
        mini_shore[w00] = t_shore[0::2, 0::2][w00]
        mini_ocean[w00] = t_ocean[0::2, 0::2][w00]
        
        return mini_type, mini_mag, mini_shore, mini_ocean
    
    def _pack_terrain(self, t_type, t_mag, t_shore, t_ocean):
        """Pack terrain into bytes."""
        # Bit 7: Land (1) / Water (0)
        # Bit 6: Shoreline
        # Bit 5: Ocean
        # Bits 0-4: Magnitude
        
        mag_byte = np.where(
            t_type == TYPE_LAND,
            np.minimum(np.ceil(t_mag), 31),
            np.minimum(np.ceil(t_mag / 2), 31)
        ).astype(np.uint8)
        
        packed = np.zeros_like(t_type, dtype=np.uint8)
        packed |= (t_type == TYPE_LAND).astype(np.uint8) << 7
        packed |= t_shore.astype(np.uint8) << 6
        packed |= t_ocean.astype(np.uint8) << 5
        packed |= mag_byte & 0x1F
        
        num_land = int(np.sum(t_type == TYPE_LAND))
        
        return packed.tobytes(), num_land
    
    def _create_thumbnail(self, t_type, t_mag, t_shore, quality):
        """Create thumbnail image from terrain data."""
        src_h, src_w = t_type.shape
        target_w = int(max(1, math.floor(src_w * quality)))
        target_h = int(max(1, math.floor(src_h * quality)))
        
        img = Image.new('RGBA', (target_w, target_h))
        pixels = img.load()
        
        for x in range(target_w):
            for y in range(target_h):
                src_x = int(min(math.floor(x / quality), src_w - 1))
                src_y = int(min(math.floor(y / quality), src_h - 1))
                
                tile_type = t_type[src_y, src_x]
                tile_mag = t_mag[src_y, src_x]
                tile_shore = t_shore[src_y, src_x]
                
                color = self._get_thumbnail_color(tile_type, tile_mag, tile_shore)
                pixels[x, y] = color
        
        return img
    
    def _get_thumbnail_color(self, t_type, t_mag, t_shore):
        """Get color for thumbnail pixel."""
        if t_type == TYPE_WATER:
            if t_shore:
                return (100, 143, 255, 0)
            
            water_adj = 11 - min(t_mag / 2, 10) - 10
            r = int(max(70 + water_adj, 0))
            g = int(max(132 + water_adj, 0))
            b = int(max(180 + water_adj, 0))
            return (r, g, b, 0)
        
        # Land
        if t_shore:
            return (204, 203, 158, 255)
        
        if t_mag < 10:
            adj = 220 - 2 * t_mag
            return (190, int(adj), 138, 255)
        elif t_mag < 20:
            adj = 2 * t_mag
            return (int(200 + adj), int(183 + adj), int(138 + adj), 255)
        else:
            adj = int(230 + t_mag / 2)
            return (adj, adj, adj, 255)


if __name__ == '__main__':
    # Test the processor
    processor = MapProcessor(
        api_key='',  # Add your key for testing
        output_dir='./test_output'
    )
    
    result = processor.generate(
        name='Cyprus Test',
        south=34.5,
        west=32.0,
        north=35.7,
        east=34.6,
        width_px=1024,
        dem_source='COP90'
    )
    
    print(result)
