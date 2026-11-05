"""Aires de répartition : comptages d'observations GBIF sur une grille hexagonale fixe, stockés en local.

Les tuiles image de GBIF recalculent hexagones et couleurs à chaque niveau de zoom. On lit donc une
seule fois les tuiles vectorielles d'un niveau fixe, on recale chaque case sur la grille exacte de GBIF
(les sommets des tuiles vectorielles sont arrondis : sans recalage, les cases ne s'emboîtent pas au zoom),
et on stocke un format compact d'entiers. Le dashboard recalcule les positions (Web Mercator normalisé,
0..1 de gauche à droite et de haut en bas) et dessine sur un canvas, par tuile.
"""

import json
import math
import time

import mapbox_vector_tile

from . import store
from .taxonomy import Taxonomy

ZOOM = 2               # 16 tuiles pour le monde entier
TILE = 4096            # étendue des tuiles vectorielles GBIF (unités entières : sommets arrondis)
# Hexagones par tuile : ~25 km en Europe pour une espèce ou un genre ; ~50 km pour les rangs supérieurs, dont les
# aires couvrent le monde (le règne Animalia dépasse déjà 200 000 cases à 50 km).
HEX_FINE, HEX_COARSE = 400, 200
FINE_RANKS = ("species", "subspecies", "variety", "form", "genus")
FIELD = ("HUMAN_OBSERVATION", "OBSERVATION", "MACHINE_OBSERVATION")  # pas de zoo, herbier, musée
# Zoom fort : tuile GBIF du zoom affiché, demandée à la volée (cache local TILE_TTL) ; ~11 px par case à l'écran.
HEX_ZOOM = 30
TILE_TTL = 30 * 86400
TTL = 30 * 86400
FORMAT = 4


def _ensure_table(db) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS taxon_range (gbif_key INTEGER PRIMARY KEY, data TEXT NOT NULL, fetched REAL NOT NULL)")


def cached(db, gbif_key: int, max_age: float = TTL) -> dict | None:
    """Aire déjà en local (et assez récente), sinon None. Aucun accès réseau."""
    _ensure_table(db)
    row = db.execute("SELECT data, fetched FROM taxon_range WHERE gbif_key = ?", (gbif_key,)).fetchone()
    if row and time.time() - row[1] < max_age:
        data = json.loads(row[0])
        if data.get("v") == FORMAT:
            return data
    return None


def grid(hex_per_tile: int) -> tuple[float, float]:
    """Géométrie exacte de la grille GBIF, mesurée sur ses tuiles (écart restant < 0,5 unité, l'arrondi des
    sommets) : hexagones réguliers à sommet plat, colonnes au pas de TILE / (hexPerTile + 1) à partir de 0
    dans chaque tuile, lignes au pas de √3 R, colonnes impaires décalées d'une demi-ligne. Seule l'origine
    verticale change d'une tuile à l'autre : les grilles de deux tuiles voisines ne se raccordent pas, chaque
    case est donc dessinée découpée au bord de sa tuile (comme sur les cartes de GBIF).
    Renvoie (pas des colonnes, demi-pas des lignes), en unités de tuile."""
    sx = TILE / (hex_per_tile + 1)
    return sx, math.sqrt(3) * sx / 1.5 / 2


def snap(centers: list[tuple[float, float]], hex_per_tile: int) -> tuple[float, list[tuple[int, int]]]:
    """Centres mesurés (unités de tuile) -> origine verticale de la tuile et (colonne, demi-ligne) exactes."""
    sx, hy = grid(hex_per_tile)
    cols = [round(x / sx) for x, _ in centers]
    # Origine verticale : phase moyenne des centres modulo un pas de ligne (2 demi-pas), moyenne circulaire
    # pour ne pas couper le nuage en deux quand l'origine tombe près d'une demi-ligne.
    period = 2 * hy
    angles = [2 * math.pi * ((y - (i % 2) * hy) % period) / period for (_, y), i in zip(centers, cols)]
    phase = math.atan2(sum(map(math.sin, angles)), sum(map(math.cos, angles))) % (2 * math.pi)
    y0 = phase / (2 * math.pi) * period

    def half(y, i, y0):
        """Demi-ligne la plus proche, de même parité que la colonne."""
        return 2 * round(((y - y0) / hy - i % 2) / 2) + i % 2
    return y0, [(i, half(y, i, y0)) for (_, y), i in zip(centers, cols)]


def fetch(tx: Taxonomy, gbif_key: int) -> dict:
    """Télécharge l'aire depuis GBIF (16 tuiles), la recale sur la grille exacte et l'enregistre au format
    compact : par tuile son origine verticale, par case [tuile, colonne, demi-ligne, observations]."""
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
    """Cases d'une tuile GBIF au zoom affiché (taille constante à l'écran), recalées sur la grille exacte :
    {hex, y0, cells: [[colonne, demi-ligne, observations]]}. Gardée TILE_TTL en local."""
    db = tx.db
    db.execute("CREATE TABLE IF NOT EXISTS range_tile (gbif_key INTEGER, z INTEGER, x INTEGER, y INTEGER, data TEXT NOT NULL, "
               "fetched REAL NOT NULL, PRIMARY KEY (gbif_key, z, x, y))")
    row = db.execute("SELECT data, fetched FROM range_tile WHERE gbif_key = ? AND z = ? AND x = ? AND y = ?",
                     (gbif_key, z, x, y)).fetchone()
    if row and time.time() - row[1] < TILE_TTL:
        return json.loads(row[0])
    params = [("srs", "EPSG:3857"), ("taxonKey", gbif_key), ("bin", "hex"), ("hexPerTile", HEX_ZOOM)]
    params += [("basisOfRecord", b) for b in FIELD]
    # Pas le plafond de débit des API GBIF : le service de cartes est fait pour servir des tuiles à la volée.
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
    """Aire locale si disponible, sinon téléchargée maintenant."""
    return cached(tx.db, gbif_key) or fetch(tx, gbif_key)


def migrate_legacy(db) -> int:
    """Supprime les aires des anciens formats gardées dans le cache HTTP (GeoJSON) : elles sont retéléchargées
    au nouveau format par la synchronisation. Renvoie le nombre d'entrées supprimées."""
    _ensure_table(db)
    n = db.execute("DELETE FROM http_cache WHERE url LIKE 'range:%'").rowcount
    db.commit()
    return n
