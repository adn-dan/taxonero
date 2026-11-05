"""Copie locale de la taxonomie GBIF (GBIF Backbone Taxonomy) : correspondance des noms (synonymes compris) et
fiches (rang, lignée), sans requête réseau ni limite de débit ; ce sont les requêtes faites pour chaque candidat
de chaque photo. Mêmes identifiants que l'API (même version), donc mêmes clés pour les requêtes qui restent en
ligne (présence autour d'un point, fossiles, cartes).

Restent aussi à l'API, une seule fois par taxon : les noms communs (l'archive en a nettement moins : pas de nom
français local pour la moitié des taxons du jeu de dev) et la catégorie UICN (l'API suit la Liste rouge
actuelle). Les noms communs locaux servent à la recherche de la visionneuse.

L'archive (~0,9 Go, https://hosted-datasets.gbif.org/datasets/backbone/current/) est téléchargée une fois,
convertie en base SQLite (data/gbif-backbone.sqlite), puis supprimée. Tant que la base n'existe pas, ou si
l'API passe à une autre version de la taxonomie, Taxonomy interroge l'API comme avant.
"""

import io
import logging
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

URL = "https://hosted-datasets.gbif.org/datasets/backbone/current/backbone.zip"
DATASET = "https://api.gbif.org/v1/dataset/d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"
SCHEMA = """
CREATE TABLE taxon (
    key        INTEGER PRIMARY KEY,
    accepted   INTEGER,   -- nom accepté (synonymes)
    rank       TEXT,      -- SPECIES, GENUS…
    status     TEXT,      -- ACCEPTED, SYNONYM, DOUBTFUL…
    name       TEXT,      -- nom canonique, sans auteur
    authorship TEXT,
    kingdom TEXT, phylum TEXT, class TEXT, "order" TEXT, family TEXT, genus TEXT, species TEXT
);
CREATE TABLE vernacular (key INTEGER, name TEXT, lang TEXT, source TEXT);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""
INDEXES = """
CREATE INDEX taxon_name ON taxon(name COLLATE NOCASE);
CREATE INDEX vernacular_key ON vernacular(key);
CREATE INDEX vernacular_name ON vernacular(name COLLATE NOCASE);
"""
BELOW_SPECIES = {"SUBSPECIES", "VARIETY", "FORM", "INFRASPECIFIC_NAME", "INFRASUBSPECIFIC_NAME"}
# Règnes au sens de BioCLIP (TreeOfLife) -> règnes de GBIF.
KINGDOMS = {"Archaeplastida": "Plantae", "Metazoa": "Animalia"}


def _rows(z: zipfile.ZipFile, name: str):
    with z.open(name) as f:
        lines = io.TextIOWrapper(f, encoding="utf-8", newline="\n")
        next(lines)   # en-tête
        for line in lines:
            yield line.rstrip("\n").split("\t")


def build(archive: Path, out: Path, version: str = "") -> Path:
    """Convertit l'archive Darwin Core en base SQLite (fichier temporaire renommé à la fin : une base à moitié
    construite n'est jamais utilisée)."""
    tmp = out.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;" + SCHEMA)
    t0 = time.monotonic()
    with zipfile.ZipFile(archive) as z:
        batch = []
        for r in _rows(z, "Taxon.tsv"):
            # 0 taxonID … 3 acceptedNameUsageID, 6 authorship, 7 canonicalName, 8 genericName, 9 specificEpithet,
            # 11 taxonRank, 14 taxonomicStatus, 17-22 kingdom…genus
            rank, name = r[11].upper(), r[7] or r[5]
            species = name if rank == "SPECIES" else f"{r[8]} {r[9]}" if rank in BELOW_SPECIES and r[9] else None
            batch.append((int(r[0]), int(r[3]) if r[3] else None, rank, r[14].upper().replace(" ", "_"), name, r[6] or None,
                          *(v or None for v in r[17:23]), species))
            if len(batch) >= 100_000:
                db.executemany("INSERT INTO taxon VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
                batch.clear()
        db.executemany("INSERT INTO taxon VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        log.info("taxonomie GBIF : %d taxons (%.0f s)", db.execute("SELECT count(*) FROM taxon").fetchone()[0], time.monotonic() - t0)
        db.executemany("INSERT INTO vernacular VALUES (?,?,?,?)",
                       ((int(r[0]), r[1].strip(), r[2], r[7]) for r in _rows(z, "VernacularName.tsv") if r[1].strip()))
    db.executescript(INDEXES)
    db.execute("INSERT INTO meta VALUES ('version', ?)", (version,))
    db.execute("INSERT INTO meta VALUES ('built', ?)", (str(time.time()),))
    db.commit()
    db.execute("VACUUM")
    db.close()
    tmp.replace(out)
    log.info("taxonomie GBIF locale prête : %s (%.0f Mo, %.0f s)", out, out.stat().st_size / 2 ** 20, time.monotonic() - t0)
    return out


def api_version(http: httpx.Client) -> str:
    """Version de la taxonomie utilisée par l'API GBIF (date de publication du jeu de données)."""
    return (http.get(DATASET, timeout=30).raise_for_status().json().get("pubDate") or "")[:10]


def install(data_dir: Path, user_agent: str, progress=lambda state, version: None) -> Path:
    """Télécharge et construit la base si elle manque ou ne correspond plus à la version de l'API. Long (téléchargement
    de ~0,9 Go puis une dizaine de minutes de conversion) : à lancer en tâche de fond. progress(état, version) :
    downloading, building, ready."""
    out = data_dir / "gbif-backbone.sqlite"
    with httpx.Client(headers={"User-Agent": user_agent}, follow_redirects=True) as http:
        version = api_version(http)
        if out.exists() and local_version(out) == version:
            progress("ready", version)
            return out
        # Autre version que celle de l'API : ses clés ne correspondraient plus, elle n'est plus utilisée.
        out.unlink(missing_ok=True)
        archive = data_dir / "gbif-backbone.zip"
        progress("downloading", version)
        log.info("téléchargement de la taxonomie GBIF (version %s, ~0,9 Go)…", version)
        with http.stream("GET", URL, timeout=httpx.Timeout(60, read=300)) as r, open(archive, "wb") as f:
            r.raise_for_status()
            for chunk in r.iter_bytes(2 ** 20):
                f.write(chunk)
    try:
        progress("building", version)
        build(archive, out, version)
    finally:
        archive.unlink(missing_ok=True)
    progress("ready", version)
    return out


def local_version(path: Path) -> str | None:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            return db.execute("SELECT v FROM meta WHERE k = 'version'").fetchone()[0]
    except Exception:
        return None


class Backbone:
    """Lecture seule. Réponses au même format que l'API GBIF, pour que Taxonomy n'ait rien d'autre à changer."""

    def __init__(self, path: Path):
        self._path, self._local = path, threading.local()
        self.db   # fichier absent ou illisible : l'erreur sort ici, pas à la première recherche

    @property
    def db(self) -> sqlite3.Connection:
        """Une connexion par fil : partagée entre fils simultanés, une même connexion mélange les réponses."""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
        return c

    def match(self, name: str, kingdom: str | None = None, rank: str | None = None) -> dict | None:
        """Correspondance exacte du nom canonique (casse ignorée), comme species/match. None si rien de sûr : l'appelant
        se rabat alors sur l'API (correspondance approchée, fautes d'orthographe)."""
        rows = self.db.execute("SELECT * FROM taxon WHERE name = ? COLLATE NOCASE", (name.strip(),)).fetchall()
        if rank:
            rows = [r for r in rows if r["rank"] == rank.upper()]
        kingdom = KINGDOMS.get(kingdom, kingdom)
        # Règne indiqué par le modèle : il départage les homonymes ; s'il n'en reste aucun, il est ignoré (comme l'API).
        rows = [r for r in rows if r["kingdom"] == kingdom] or rows if kingdom else rows
        rows = [r for r in rows if r["status"] != "DOUBTFUL"]   # l'API ne retient pas un nom seulement douteux
        if not rows:
            return None
        best = min(rows, key=lambda r: (r["status"] != "ACCEPTED", r["key"]))
        if len({r["kingdom"] for r in rows if r["status"] == "ACCEPTED"}) > 1:
            return None   # homonymes acceptés dans plusieurs règnes : l'API sait mieux trancher
        out = {"usageKey": best["key"], "matchType": "EXACT", "rank": best["rank"], "status": best["status"],
               "scientificName": best["name"], "canonicalName": best["name"], "kingdom": best["kingdom"]}
        if best["accepted"]:
            out["acceptedUsageKey"] = best["accepted"]
        return out

    def species(self, key: int) -> dict | None:
        r = self.db.execute("SELECT * FROM taxon WHERE key = ?", (key,)).fetchone()
        if not r:
            return None
        out = {"key": r["key"], "canonicalName": r["name"], "authorship": r["authorship"] or "",
               "scientificName": f"{r['name']} {r['authorship']}" if r["authorship"] else r["name"],
               "rank": r["rank"], "taxonomicStatus": r["status"]}
        out.update({k: r[k] for k in ("kingdom", "phylum", "class", "order", "family", "genus", "species") if r[k]})
        return out


    def search(self, q: str, lang: str = "fr", limit: int = 12) -> list[dict]:
        """Recherche par début de nom scientifique ou de nom commun (champ de recherche de la visionneuse) : taxons
        acceptés, espèces d'abord, nom commun dans la langue des noms s'il y en a un."""
        q = q.strip().replace("\\", "").replace("%", "").replace("_", "")
        if len(q) < 2:
            return []
        ranks = ("SPECIES", "GENUS", "FAMILY", "SUBSPECIES", "ORDER", "CLASS")
        hits: dict[int, dict] = {}
        for r in self.db.execute(
                f"SELECT key, name, rank FROM taxon WHERE name LIKE ? AND status = 'ACCEPTED' AND rank IN ({','.join('?' * len(ranks))}) LIMIT 60",
                (q + "%", *ranks)):
            hits.setdefault(r["key"], {"gbif_key": r["key"], "name": r["name"], "rank": r["rank"].lower(), "common": ""})
        for r in self.db.execute(
                "SELECT coalesce(t.accepted, t.key) AS key, v.name AS common, v.lang FROM vernacular v JOIN taxon t ON t.key = v.key "
                "WHERE v.name LIKE ? LIMIT 200", (q + "%",)):
            t = self.db.execute("SELECT key, name, rank FROM taxon WHERE key = ?", (r["key"],)).fetchone()
            if not t or t["rank"] not in ranks:
                continue
            h = hits.setdefault(t["key"], {"gbif_key": t["key"], "name": t["name"], "rank": t["rank"].lower(), "common": ""})
            if not h["common"] or r["lang"] == lang:
                h["common"] = r["common"]
        order = {"species": 0, "genus": 1, "family": 2, "subspecies": 3}
        out = sorted(hits.values(), key=lambda h: (order.get(h["rank"], 4), not h["common"], len(h["name"])))[:limit]
        for h in out:   # trouvé par le nom scientifique : son nom commun dans la langue des noms, s'il y en a un
            if not h["common"]:
                r = self.db.execute("SELECT name FROM vernacular WHERE key = ? AND lang = ? LIMIT 1", (h["gbif_key"], lang)).fetchone()
                h["common"] = r["name"] if r else ""
        return out
