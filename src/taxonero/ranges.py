"""Range maps: counts of GBIF observations on a fixed hexagonal grid, stored locally.

GBIF image tiles recompute hexagons and colours at every zoom level. So the vector tiles of a fixed level
are read only once, each cell is snapped to the exact GBIF grid
(vector tile vertices are rounded: without snapping, the cells do not fit together when zooming),
and a compact integer format is stored. The dashboard recomputes the positions (normalised Web Mercator,
0..1 from left to right and from top to bottom) and draws on a canvas, per tile.
"""

import json
import math
import time

import mapbox_vector_tile

from . import store
from .taxonomy import Taxonomy

ZOOM = 2               # 16 tiles for the whole world
TILE = 4096            # extent of the GBIF vector tiles (integer units: rounded vertices)
# Hexagons per tile: ~25 km in Europe for a species or a genus; ~50 km for higher ranks, whose
# ranges cover the world (the Animalia kingdom already exceeds 200,000 cells at 50 km).
HEX_FINE, HEX_COARSE = 400, 200
FINE_RANKS = ("species", "subspecies", "variety", "form", "genus")
FIELD = ("HUMAN_OBSERVATION", "OBSERVATION", "MACHINE_OBSERVATION")  # no zoo, herbarium, museum
# High zoom: GBIF tile of the displayed zoom, requested on the fly (local cache TILE_TTL); ~11 px per cell on screen.
HEX_ZOOM = 30
TILE_TTL = 30 * 86400
TTL = 30 * 86400
FORMAT = 4


def _ensure_table(db) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS taxon_range (gbif_key INTEGER PRIMARY KEY, data TEXT NOT NULL, fetched REAL NOT NULL)")


def cached(db, gbif_key: int, max_age: float = TTL) -> dict | None:
    """Range map already local (and recent enough), otherwise None. No network access."""
    _ensure_table(db)
    row = db.execute("SELECT data, fetched FROM taxon_range WHERE gbif_key = ?", (gbif_key,)).fetchone()
    if row and time.time() - row[1] < max_age:
        data = json.loads(row[0])
        if data.get("v") == FORMAT:
            return data
    return None


def grid(hex_per_tile: int) -> tuple[float, float]:
    """Exact geometry of the GBIF grid, measured on its tiles (remaining gap < 0.5 unit, the rounding of the
    vertices): regular flat-top hexagons, columns at a step of TILE / (hexPerTile + 1) starting from 0
    in each tile, rows at a step of √3 R, odd columns shifted by half a row. Only the vertical origin
    changes from one tile to the next: the grids of two neighbouring tiles do not join, each
    cell is therefore drawn clipped at the edge of its tile (as on GBIF maps).
    Return (column step, row half-step), in tile units."""
    sx = TILE / (hex_per_tile + 1)
    return sx, math.sqrt(3) * sx / 1.5 / 2


def snap(centers: list[tuple[float, float]], hex_per_tile: int) -> tuple[float, list[tuple[int, int]]]:
    """Measured centres (tile units) -> vertical origin of the tile and exact (column, half-row)."""
    sx, hy = grid(hex_per_tile)
    cols = [round(x / sx) for x, _ in centers]
    # Vertical origin: mean phase of the centres modulo one row step (2 half-steps), circular mean
    # so as not to split the cloud in two when the origin falls near a half-row.
    period = 2 * hy
    angles = [2 * math.pi * ((y - (i % 2) * hy) % period) / period for (_, y), i in zip(centers, cols)]
    phase = math.atan2(sum(map(math.sin, angles)), sum(map(math.cos, angles))) % (2 * math.pi)
    y0 = phase / (2 * math.pi) * period

    def half(y, i, y0):
        """Nearest half-row, with the same parity as the column."""
        return 2 * round(((y - y0) / hy - i % 2) / 2) + i % 2
    return y0, [(i, half(y, i, y0)) for (_, y), i in zip(centers, cols)]


def fetch(tx: Taxonomy, gbif_key: int) -> dict:
    """Download the range map from GBIF (16 tiles), snap it to the exact grid and save it in the compact
    format: per tile its vertical origin, per cell [tile, column, half-row, observations]."""
    rank = (store.load_taxon(tx.db, gbif_key) or {}).get("rank")
    hpt = HEX_FINE if rank in FINE_RANKS else HEX_COARSE
    n = 2 ** ZOOM
    tiles: dict[int, float] = {}
    cells: list[list[int]] = []
    for x in range(n):
        for y in range(n):
            params = [("srs", "EPSG:3857"), ("taxonKey", gbif_key), ("bin", "hex"), ("hexPerTile", hpt)]
            params += [("basisOfRecord", b) for b in FIELD]
            data = tx.fetch_bytes(f"https://api.gbif.org/v2/map/occurrence/density/{ZOOM}/{x}/{y}.mvt", params)
            if not data:
                continue
            layer = mapbox_vector_tile.decode(data, default_options={"y_coord_down": True}).get("occurrence")
            if not layer:
                continue
            found = [(f["geometry"]["coordinates"][0][:-1], int(f["properties"].get("total", 0)))
                     for f in layer["features"] if f["geometry"]["type"] == "Polygon"]
            found = [(ring, total) for ring, total in found if len(ring) == 6 and total > 0]
            if not found:
                continue
            centers = [(sum(p[0] for p in ring) / 6, sum(p[1] for p in ring) / 6) for ring, _ in found]
            t = x * n + y
            y0, idx = snap(centers, hpt)
            tiles[t] = round(y0, 4)
            cells += [[t, i, h, total] for (i, h), (_, total) in zip(idx, found)]
    out = {"v": FORMAT, "z": ZOOM, "tile": TILE, "hex": hpt, "tiles": tiles, "cells": cells}
    _ensure_table(tx.db)
    tx.db.execute("INSERT OR REPLACE INTO taxon_range VALUES (?, ?, ?)", (gbif_key, json.dumps(out, separators=(",", ":")), time.time()))
    tx.db.commit()
    return out


def zoom_tile(tx: Taxonomy, gbif_key: int, z: int, x: int, y: int) -> dict:
    """Cells of a GBIF tile at the displayed zoom (constant size on screen), snapped to the exact grid:
    {hex, y0, cells: [[column, half-row, observations]]}. Kept TILE_TTL locally."""
    db = tx.db
    db.execute("CREATE TABLE IF NOT EXISTS range_tile (gbif_key INTEGER, z INTEGER, x INTEGER, y INTEGER, data TEXT NOT NULL, "
               "fetched REAL NOT NULL, PRIMARY KEY (gbif_key, z, x, y))")
    row = db.execute("SELECT data, fetched FROM range_tile WHERE gbif_key = ? AND z = ? AND x = ? AND y = ?",
                     (gbif_key, z, x, y)).fetchone()
    if row and time.time() - row[1] < TILE_TTL:
        return json.loads(row[0])
    params = [("srs", "EPSG:3857"), ("taxonKey", gbif_key), ("bin", "hex"), ("hexPerTile", HEX_ZOOM)]
    params += [("basisOfRecord", b) for b in FIELD]
    # Not the rate cap of the GBIF APIs: the map service is made to serve tiles on the fly.
    r = tx.http.get(f"https://api.gbif.org/v2/map/occurrence/density/{z}/{x}/{y}.mvt", params=params)
    out = {"hex": HEX_ZOOM, "y0": 0.0, "cells": []}
    if r.status_code != 204:
        layer = mapbox_vector_tile.decode(r.raise_for_status().content, default_options={"y_coord_down": True}).get("occurrence")
        found = [(f["geometry"]["coordinates"][0][:-1], int(f["properties"].get("total", 0)))
                 for f in (layer or {}).get("features", []) if f["geometry"]["type"] == "Polygon"]
        found = [(ring, total) for ring, total in found if len(ring) == 6 and total > 0]
        if found:
            ext = (layer or {}).get("extent", TILE)
            centers = [(sum(p[0] for p in ring) / 6 * TILE / ext, sum(p[1] for p in ring) / 6 * TILE / ext) for ring, _ in found]
            y0, idx = snap(centers, HEX_ZOOM)
            out = {"hex": HEX_ZOOM, "y0": round(y0, 4), "cells": [[i, h, total] for (i, h), (_, total) in zip(idx, found)]}
    db.execute("INSERT OR REPLACE INTO range_tile VALUES (?, ?, ?, ?, ?, ?)",
               (gbif_key, z, x, y, json.dumps(out, separators=(",", ":")), time.time()))
    db.execute("DELETE FROM range_tile WHERE fetched < ?", (time.time() - TILE_TTL,))
    db.commit()
    return out


def get(tx: Taxonomy, gbif_key: int) -> dict:
    """Local range map if available, otherwise downloaded now."""
    return cached(tx.db, gbif_key) or fetch(tx, gbif_key)


def migrate_legacy(db) -> int:
    """Delete the range maps of old formats kept in the HTTP cache (GeoJSON): they are downloaded again
    in the new format by the synchronisation. Return the number of entries deleted."""
    _ensure_table(db)
    n = db.execute("DELETE FROM http_cache WHERE url LIKE 'range:%'").rowcount
    db.commit()
    return n
