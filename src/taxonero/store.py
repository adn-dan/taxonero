"""Stockage local (SQLite). C'est la source de vérité : Immich n'en reçoit qu'une projection
(tags, bloc de description) reconstructible à tout moment, et chaque écriture faite dans
Immich est tracée ici pour pouvoir être annulée (uninstall).
"""

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS asset (
    asset_id     TEXT PRIMARY KEY,
    checksum     TEXT,
    file_name    TEXT,
    taken_at     TEXT,
    lat          REAL,
    lon          REAL,
    make         TEXT,
    model        TEXT,
    lens         TEXT,
    city         TEXT,          -- lieu (géocodage inverse fait par Immich)
    state        TEXT,
    country      TEXT,
    width        INTEGER,
    height       INTEGER,
    analyzed_at  REAL,          -- NULL = à analyser
    analyzer     TEXT,          -- détecteur + classifieur utilisés
    fail_count   INTEGER DEFAULT 0,  -- analyses en échec d'affilée (abandon au-delà de MAX_FAILURES)
    last_error   TEXT,
    missing_at   REAL,          -- disparue d'Immich (supprimée ou à la corbeille) : masquée, plus synchronisée
    kind         TEXT,          -- IMAGE ou VIDEO (vidéo : seule l'image clé d'Immich est analysée)
    people       INTEGER,       -- personnes reconnues par Immich sur la photo (NULL : pas encore demandé)
    twin_of      TEXT,          -- RAW doublé d'un JPG du même cliché : id du JPG (analysé à sa place)
    geo_user     INTEGER,       -- 1 : lat/lon indiqués à la main (photo sans GPS) ; un GPS de la source les remplace
    series_id    TEXT           -- série (rafale) : id de sa première photo ; NULL pour une photo seule (series.py)
);

CREATE TABLE IF NOT EXISTS detection (
    id           INTEGER PRIMARY KEY,
    asset_id     TEXT NOT NULL REFERENCES asset(asset_id) ON DELETE CASCADE,
    -- Rectangle normalisé (0..1) dans l'image.
    x REAL, y REAL, w REAL, h REAL,
    det_score    REAL,
    box_label    TEXT,                          -- animal | plant | fungus | image
    detector     TEXT,                          -- md | owl | image | manual
    source       TEXT NOT NULL,                 -- 'auto' | 'manual'
    status       TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'confirmed' | 'rejected' | 'deleted'
    confirmed_by TEXT,                          -- 'auto' (seuil) | 'user'
    taxon_key    INTEGER,                       -- GBIF
    taxon_rank   TEXT,                          -- rang retenu (species, genus, family...)
    score        REAL,
    margin       REAL,
    plausibility TEXT,                          -- green | amber | red | unknown
    candidates   TEXT,                          -- JSON : top-k [{name, rank, score, gbif_key, plausibility}]
    -- Candidats écartés par les paramètres (disparus, absents de la région), gardés pour pouvoir les
    -- rendre si le réglage change. Même format, avec 'p0' : probabilité avant filtrage.
    candidates_out TEXT,
    audit        INTEGER DEFAULT 0,             -- 1 : aurait été auto-validée, gardée pour contrôle
    -- Proposition d'origine du modèle (conservée même si l'utilisateur corrige) : sert à calibrer.
    model_key    INTEGER,
    model_rank   TEXT,
    model_conf   REAL,
    series_n     INTEGER,                       -- photos de la série qui proposent la même espèce (series.py)
    smoothed     INTEGER,                       -- 1 : proposition remplacée par l'espèce dominante de la série
    presmooth    TEXT,                          -- JSON : proposition d'avant le lissage, rétablie si la règle ne le justifie plus
    frame_support REAL,                         -- sujet seul : probabilité du taxon retenu sur le recadrage seul
    created_at   REAL NOT NULL,
    reviewed_at  REAL
);
-- (asset_id, status) : sans lui, SQLite prend l'index sur status pour les EXISTS par photo et parcourt toutes
-- les détections en attente pour chacune (plusieurs minutes pour la page Photos à 70 000 photos).
DROP INDEX IF EXISTS detection_asset;
CREATE INDEX IF NOT EXISTS detection_asset_status ON detection(asset_id, status);
CREATE INDEX IF NOT EXISTS detection_status ON detection(status);
-- Relectures faites à la main (fiabilité par groupe, calibration) : quelques centaines de lignes parmi des dizaines
-- de milliers, lues sans parcourir toute la table.
CREATE INDEX IF NOT EXISTS detection_user ON detection(model_key) WHERE confirmed_by = 'user';

CREATE TABLE IF NOT EXISTS taxon (
    gbif_key     INTEGER PRIMARY KEY,
    data         TEXT NOT NULL                  -- JSON (taxonomy.Taxon)
);

-- Trace de chaque objet écrit dans Immich (pour rebuild / uninstall).
CREATE TABLE IF NOT EXISTS written (
    kind         TEXT NOT NULL,                 -- 'tag' | 'tag_asset' | 'description'
    immich_id    TEXT NOT NULL,                 -- id du tag, ou de l'asset pour description
    asset_id     TEXT NOT NULL DEFAULT '',
    value        TEXT,
    written_at   REAL NOT NULL,
    PRIMARY KEY (kind, immich_id, asset_id)
);

-- Qualité d'image (couleurs, netteté) pour choisir la photo de couverture d'une espèce.
CREATE TABLE IF NOT EXISTS image_quality (
    asset_id     TEXT PRIMARY KEY,
    quality      REAL NOT NULL                  -- 0..1
);

-- Dernier état écrit avec succès dans Immich, par photo (empreinte des tags et du bloc de description) :
-- la synchronisation ne relit que les photos dont l'état voulu a changé, plus un contrôle complet par jour.
CREATE TABLE IF NOT EXISTS asset_sync (
    asset_id     TEXT PRIMARY KEY,
    wanted       TEXT NOT NULL,
    synced_at    REAL NOT NULL
);

-- Photo principale choisie à la main pour un taxon (sinon : choix automatique, web.compute_covers).
CREATE TABLE IF NOT EXISTS cover_choice (
    taxon_key    INTEGER PRIMARY KEY,
    asset_id     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key          TEXT PRIMARY KEY,
    value        TEXT
);
"""


def _private(path: str | Path) -> None:
    """La base contient la clé API : lisible par son seul propriétaire. SQLite donne ses droits au -wal et au -shm
    qu'il crée ; ceux d'une base créée avant cette règle sont corrigés ici. Pas le propriétaire : on laisse tel quel."""
    if str(path) == ":memory:":
        return
    Path(path).touch(mode=0o600, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(f"{path}{suffix}").chmod(0o600)
        except OSError:
            pass


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _private(path)
    # timeout : le worker et le dashboard écrivent dans la même base ; on attend un verrou plutôt que d'échouer.
    db = sqlite3.connect(path, check_same_thread=False, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    # WAL + NORMAL (réglage recommandé par SQLite) : plus d'écriture forcée sur le disque à chaque commit, sans
    # risque de corruption ; une coupure de courant peut seulement perdre les dernières transactions. Sur un pool
    # ZFS à disques durs, chaque écriture forcée coûte ~5 à 20 ms, et l'analyse en fait plusieurs par photo.
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def _migrate(db: sqlite3.Connection) -> None:
    """Ajoute les colonnes apparues depuis la création de la base (jamais de suppression)."""
    for table in ("asset", "detection"):
        have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        create = db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()["sql"]
        for line in SCHEMA.split(f"CREATE TABLE IF NOT EXISTS {table} (", 1)[1].split(");", 1)[0].splitlines():
            line = line.split("--", 1)[0].strip().rstrip(",")
            if not line or line.split()[0].upper() in ("PRIMARY", "UNIQUE", "FOREIGN"):
                continue
            col = line.split()[0]
            if col not in have and col not in create:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {line.replace('NOT NULL', '')}")
    # Index sur des colonnes ajoutées par une migration : créés après elle (une base d'avant ne les a pas encore
    # quand le schéma s'applique).
    db.execute("CREATE INDEX IF NOT EXISTS asset_series ON asset(series_id)")
    db.commit()


def get_state(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


WRITES_PAUSED = "writes_paused"   # état : "1" après un retrait, rien n'est écrit tant qu'on ne reprend pas


def set_state(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))
    db.commit()


def upsert_asset(db: sqlite3.Connection, a: dict, with_people: bool = False) -> bool:
    """Enregistre un asset Immich. Retourne True s'il est nouveau ou si son fichier a changé.
    with_people : la réponse a été demandée avec withPeople (sinon Immich renvoie quand même une liste vide)."""
    exif = a.get("exifInfo") or {}
    row = db.execute("SELECT checksum FROM asset WHERE asset_id = ?", (a["id"],)).fetchone()
    changed = row is None or row["checksum"] != a.get("checksum")
    db.execute(
        """INSERT INTO asset (asset_id, checksum, file_name, taken_at, lat, lon, make, model, lens, width, height,
                              city, state, country, kind, people)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(asset_id) DO UPDATE SET
             checksum=excluded.checksum, file_name=excluded.file_name, taken_at=excluded.taken_at,
             lat=CASE WHEN excluded.lat IS NULL AND asset.geo_user = 1 THEN asset.lat ELSE excluded.lat END,
             lon=CASE WHEN excluded.lat IS NULL AND asset.geo_user = 1 THEN asset.lon ELSE excluded.lon END,
             geo_user=CASE WHEN excluded.lat IS NULL THEN asset.geo_user ELSE NULL END,
             make=excluded.make, model=excluded.model, lens=excluded.lens,
             width=excluded.width, height=excluded.height,
             city=excluded.city, state=excluded.state, country=excluded.country, kind=excluded.kind,
             people=COALESCE(excluded.people, asset.people),
             analyzed_at=CASE WHEN asset.checksum IS excluded.checksum THEN asset.analyzed_at ELSE NULL END,
             fail_count=CASE WHEN asset.checksum IS excluded.checksum THEN asset.fail_count ELSE 0 END,
             missing_at=NULL""",
        (a["id"], a.get("checksum"), a.get("originalFileName"), exif.get("dateTimeOriginal"),
         exif.get("latitude"), exif.get("longitude"), exif.get("make"), exif.get("model"), exif.get("lensModel"),
         exif.get("exifImageWidth"), exif.get("exifImageHeight"), exif.get("city"), exif.get("state"), exif.get("country"),
         a.get("type") or "IMAGE", len(a.get("people") or []) if with_people else None),
    )
    return changed


# Formats RAW (extensions). Un RAW accompagné d'un JPG (ou HEIC…) du même cliché n'est pas analysé :
# même image, travail en double. Il reçoit les tags de son JPG.
RAW_EXT = {"3fr", "arw", "cr2", "cr3", "crw", "dcr", "dng", "erf", "fff", "iiq", "k25", "kdc", "mef", "mos",
           "mrw", "nef", "nrw", "orf", "ori", "pef", "raf", "raw", "rw2", "rwl", "sr2", "srf", "srw", "x3f"}


def _split(name: str | None) -> tuple[str, str]:
    stem, _, ext = (name or "").rpartition(".")
    return stem.lower(), ext.lower()


def link_raw_twins(db: sqlite3.Connection) -> int:
    """Relie chaque RAW à l'image du même cliché : même nom de fichier sans l'extension et même date de prise
    de vue (à la seconde). Un RAW seul reste analysé normalement. Renvoie le nombre de RAW reliés."""
    others: dict[tuple[str, str], str] = {}
    raws: list[tuple[str, tuple[str, str], str | None]] = []
    for r in db.execute("SELECT asset_id, file_name, taken_at, twin_of FROM asset "
                        "WHERE missing_at IS NULL AND kind IS NOT 'VIDEO' AND taken_at IS NOT NULL"):
        stem, ext = _split(r["file_name"])
        key = (stem, r["taken_at"][:19])
        if ext in RAW_EXT:
            raws.append((r["asset_id"], key, r["twin_of"]))
        elif stem:
            others.setdefault(key, r["asset_id"])
    changes = [(others.get(key), aid) for aid, key, twin in raws if others.get(key) != twin]
    db.executemany("UPDATE asset SET twin_of = ? WHERE asset_id = ?", changes)
    db.execute("UPDATE asset SET twin_of = NULL WHERE twin_of IS NOT NULL AND twin_of NOT IN "
               "(SELECT asset_id FROM asset WHERE missing_at IS NULL)")
    db.commit()
    return sum(1 for _, key, _ in raws if key in others)


def save_taxon(db: sqlite3.Connection, gbif_key: int, data: str) -> None:
    db.execute("INSERT OR REPLACE INTO taxon VALUES (?, ?)", (gbif_key, data))


def load_taxon(db: sqlite3.Connection, gbif_key: int) -> dict | None:
    row = db.execute("SELECT data FROM taxon WHERE gbif_key = ?", (gbif_key,)).fetchone()
    return json.loads(row["data"]) if row else None


def record_written(db: sqlite3.Connection, kind: str, immich_id: str, asset_id: str = "", value: str | None = None) -> None:
    db.execute("INSERT OR REPLACE INTO written VALUES (?, ?, ?, ?, ?)", (kind, immich_id, asset_id, value, time.time()))
