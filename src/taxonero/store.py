"""Local storage (SQLite). This is the source of truth: Immich only receives a projection of it
(tags, description block) that can be rebuilt at any time, and every write made in
Immich is recorded here so that it can be undone (uninstall).
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
    city         TEXT,          -- place (reverse geocoding done by Immich)
    state        TEXT,
    country      TEXT,
    width        INTEGER,
    height       INTEGER,
    analyzed_at  REAL,          -- NULL = to analyse
    analyzer     TEXT,          -- detector + classifier used
    fail_count   INTEGER DEFAULT 0,  -- consecutive failed analyses (given up beyond MAX_FAILURES)
    last_error   TEXT,
    missing_at   REAL,          -- gone from Immich (deleted or in the trash): hidden, no longer synchronised
    kind         TEXT,          -- IMAGE or VIDEO (video: only Immich's key frame is analysed)
    people       INTEGER,       -- people recognised by Immich in the photo (NULL: not asked yet)
    twin_of      TEXT,          -- RAW paired with a JPG of the same shot: id of the JPG (analysed instead)
    geo_user     INTEGER,       -- 1: lat/lon entered by hand (photo without GPS), a GPS from the source replaces them
    series_id    TEXT,          -- series (burst): id of its first photo; NULL for a single photo (series.py)
    source       TEXT NOT NULL DEFAULT ''   -- photo source: '' the main one, otherwise the id of an added source
);

CREATE TABLE IF NOT EXISTS detection (
    id           INTEGER PRIMARY KEY,
    asset_id     TEXT NOT NULL REFERENCES asset(asset_id) ON DELETE CASCADE,
    -- Normalised rectangle (0..1) in the image.
    x REAL, y REAL, w REAL, h REAL,
    det_score    REAL,
    box_label    TEXT,                          -- animal | plant | fungus | image
    detector     TEXT,                          -- md | owl | image | manual
    source       TEXT NOT NULL,                 -- 'auto' | 'manual'
    status       TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'confirmed' | 'rejected' | 'deleted'
    confirmed_by TEXT,                          -- 'auto' (threshold) | 'user'
    taxon_key    INTEGER,                       -- GBIF
    taxon_rank   TEXT,                          -- rank kept (species, genus, family...)
    score        REAL,
    margin       REAL,
    plausibility TEXT,                          -- green | amber | red | unknown
    candidates   TEXT,                          -- JSON: top-k [{name, rank, score, gbif_key, plausibility}]
    -- Candidates excluded by the settings (extinct, absent from the region), kept so that they can be
    -- restored if the setting changes. Same format, with 'p0': probability before filtering.
    candidates_out TEXT,
    audit        INTEGER DEFAULT 0,             -- 1: would have been auto-confirmed, kept for checking
    -- Original model proposal (kept even if the user corrects it): used for calibration.
    model_key    INTEGER,
    model_rank   TEXT,
    model_conf   REAL,
    series_n     INTEGER,                       -- photos of the series proposing the same species (series.py)
    smoothed     INTEGER,                       -- 1: proposal replaced by the dominant species of the series
    presmooth    TEXT,                          -- JSON: proposal from before smoothing, restored if the rule no longer justifies it
    frame_support REAL,                         -- single subject: probability of the kept taxon on the crop alone
    created_at   REAL NOT NULL,
    reviewed_at  REAL
);
-- (asset_id, status): without it, SQLite picks the index on status for the per-photo EXISTS and goes through all
-- pending detections for each one (several minutes for the Photos page at 70,000 photos).
DROP INDEX IF EXISTS detection_asset;
CREATE INDEX IF NOT EXISTS detection_asset_status ON detection(asset_id, status);
CREATE INDEX IF NOT EXISTS detection_status ON detection(status);
-- Reviews done by hand (reliability per group, calibration): a few hundred rows among tens
-- of thousands, read without going through the whole table.
CREATE INDEX IF NOT EXISTS detection_user ON detection(model_key) WHERE confirmed_by = 'user';

CREATE TABLE IF NOT EXISTS taxon (
    gbif_key     INTEGER PRIMARY KEY,
    data         TEXT NOT NULL                  -- JSON (taxonomy.Taxon)
);

-- Record of every object written to Immich (for rebuild / uninstall).
CREATE TABLE IF NOT EXISTS written (
    kind         TEXT NOT NULL,                 -- 'tag' | 'tag_asset' | 'description'; '…@<id>' for an added source
    immich_id    TEXT NOT NULL,                 -- tag id, or asset id for a description
    asset_id     TEXT NOT NULL DEFAULT '',
    value        TEXT,
    written_at   REAL NOT NULL,
    PRIMARY KEY (kind, immich_id, asset_id)
);

-- Image quality (colours, sharpness) to choose the cover photo of a species.
CREATE TABLE IF NOT EXISTS image_quality (
    asset_id     TEXT PRIMARY KEY,
    quality      REAL NOT NULL                  -- 0..1
);

-- Last state successfully written to Immich, per photo (fingerprint of the tags and of the description block):
-- synchronisation only reads again the photos whose wanted state changed, plus a full check once a day.
CREATE TABLE IF NOT EXISTS asset_sync (
    asset_id     TEXT PRIMARY KEY,
    wanted       TEXT NOT NULL,
    synced_at    REAL NOT NULL
);

-- AI result of a photo waiting for the geographic check (Worker.infer, then Worker.decide): the GPU part of the
-- analysis is done for the whole queue first, then the GPU is released while GBIF is queried. Deleted once the
-- photo is saved (Worker._save). data: Analyzer.pack.
CREATE TABLE IF NOT EXISTS inferred (
    asset_id     TEXT PRIMARY KEY,
    data         BLOB NOT NULL,
    created      REAL NOT NULL
);

-- Main photo chosen by hand for a taxon (otherwise: automatic choice, web.compute_covers).
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
    """The database holds the API key: readable by its owner only. SQLite gives its permissions to the -wal and -shm
    files it creates; those of a database created before this rule are fixed here. Not the owner: left as is."""
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
    # timeout: the worker and the dashboard write to the same database; wait for a lock rather than fail.
    db = sqlite3.connect(path, check_same_thread=False, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    # WAL + NORMAL (setting recommended by SQLite): no more forced disk write at every commit, without
    # risk of corruption; a power cut can only lose the last transactions. On a ZFS pool
    # of hard drives, each forced write costs ~5 to 20 ms, and the analysis does several per photo.
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    _migrate(db)
    return db


def _migrate(db: sqlite3.Connection) -> None:
    """Add the columns that appeared since the database was created (never any removal)."""
    for table in ("asset", "detection"):
        # The real columns only: the CREATE text of an older database may name a column in a comment.
        have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        for line in SCHEMA.split(f"CREATE TABLE IF NOT EXISTS {table} (", 1)[1].split(");", 1)[0].splitlines():
            line = line.split("--", 1)[0].strip().rstrip(",")
            if not line or line.split()[0].upper() in ("PRIMARY", "UNIQUE", "FOREIGN"):
                continue
            col = line.split()[0]
            if col not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {line.replace('NOT NULL', '')}")
    # Indexes on columns added by a migration: created after it (an older database does not have them yet
    # when the schema is applied).
    db.execute("CREATE INDEX IF NOT EXISTS asset_series ON asset(series_id)")
    db.execute("CREATE INDEX IF NOT EXISTS asset_source ON asset(source)")
    db.commit()


def get_state(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


WRITES_PAUSED = "writes_paused"   # state: "1" after a removal, nothing is written until writes are resumed


def set_state(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))
    db.commit()


def upsert_asset(db: sqlite3.Connection, a: dict, with_people: bool = False, source: str = "") -> bool:
    """Save an Immich asset. Return True if it is new or if its file changed.
    with_people: the response was requested with withPeople (otherwise Immich still returns an empty list).
    source: the source it comes from ('' the main one). An id already known from another source is left to it
    (False): the same Immich account added twice, for instance."""
    exif = a.get("exifInfo") or {}
    row = db.execute("SELECT checksum, source FROM asset WHERE asset_id = ?", (a["id"],)).fetchone()
    if row is not None and row["source"] != source:
        return False
    changed = row is None or row["checksum"] != a.get("checksum")
    db.execute(
        """INSERT INTO asset (asset_id, checksum, file_name, taken_at, lat, lon, make, model, lens, width, height,
                              city, state, country, kind, people, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
         a.get("type") or "IMAGE", len(a.get("people") or []) if with_people else None, source),
    )
    return changed


# RAW formats (extensions). A RAW accompanied by a JPG (or HEIC…) of the same shot is not analysed:
# same image, duplicate work. It receives the tags of its JPG.
RAW_EXT = {"3fr", "arw", "cr2", "cr3", "crw", "dcr", "dng", "erf", "fff", "iiq", "k25", "kdc", "mef", "mos",
           "mrw", "nef", "nrw", "orf", "ori", "pef", "raf", "raw", "rw2", "rwl", "sr2", "srf", "srw", "x3f"}


def _split(name: str | None) -> tuple[str, str]:
    stem, _, ext = (name or "").rpartition(".")
    return stem.lower(), ext.lower()


def link_raw_twins(db: sqlite3.Connection) -> int:
    """Link each RAW to the image of the same shot: same file name without the extension and same capture
    time (to the second). A RAW on its own is still analysed normally. Return the number of RAW files linked."""
    others: dict[tuple[str, str, str], str] = {}
    raws: list[tuple[str, tuple[str, str, str], str | None]] = []
    for r in db.execute("SELECT asset_id, file_name, taken_at, twin_of, source FROM asset "
                        "WHERE missing_at IS NULL AND kind IS NOT 'VIDEO' AND taken_at IS NOT NULL"):
        stem, ext = _split(r["file_name"])
        key = (stem, r["taken_at"][:19], r["source"])   # within a source: the sources are kept apart
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
