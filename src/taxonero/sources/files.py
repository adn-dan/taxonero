"""Base des sources « fichiers » : photos sur le disque, résultats dans un fichier XMP à côté de chacune (« sidecar »).

Lecture : fichiers et métadonnées (date, GPS, appareil) par ExifTool. Écriture, sans jamais toucher aux photos :
- mots-clés hiérarchiques (XMP-lr:HierarchicalSubject, « Nature|Aves|Paridae|Mésange charbonnière ») et le
  dernier niveau en mot-clé simple (XMP-dc:Subject) pour les logiciels sans hiérarchie ;
- description (XMP-dc:Description), avec le même bloc délimité qu'avec Immich.

Chaque source précise d'où viennent les fichiers (un dossier, la bibliothèque d'un logiciel) et le nom des sidecars
que son logiciel lit : _files, _resolve, _sidecars, et _check_source pour l'état. Même interface que ImmichClient
(voir sources/__init__.py) : les « tags » sont identifiés par leur valeur (« Nature/Aves/… ») et les assets par un
identifiant stable tiré de la clé du fichier (chemin relatif au dossier, ou absolu pour une bibliothèque). Les vidéos
ne sont pas prises en charge.
"""

import io
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps

from ..store import RAW_EXT
from .exiftool import INSTALL_HINT, NO_WINDOW, ExifTool, available, command, escape

log = logging.getLogger(__name__)

IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "tif", "tiff", "heic", "heif"} | RAW_EXT
READ_TAGS = ["DateTimeOriginal", "CreateDate", "GPSLatitude", "GPSLongitude", "Make", "Model", "LensModel",
             "ImageWidth", "ImageHeight", "Orientation"]
SIZES = {"thumbnail": 250, "preview": 1440}   # petit côté, comme les vignettes et aperçus d'Immich
NAMESPACE = uuid.UUID("6f1c2b8e-6a7d-4a43-9d8e-4f3e5c2a1b90")

try:   # HEIC (iPhone) : greffon facultatif
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _taken(meta: dict) -> str | None:
    """Date de prise de vue, heure de l'appareil (sans fuseau), au format ISO."""
    for k in ("DateTimeOriginal", "CreateDate"):
        v = str(meta.get(k) or "")
        if len(v) >= 19 and v[:4].isdigit() and not v.startswith("0000"):
            return f"{v[:4]}-{v[5:7]}-{v[8:10]}T{v[11:19]}"
    return None


WINDOWS = sys.platform == "win32"


def same_file(path: str) -> str:
    """Chemin comparable au SourceFile d'ExifTool, qui sous Windows remplace les \\ par des /."""
    return path.replace("\\", "/") if WINDOWS else path


def is_image(name: str) -> bool:
    return name.rpartition(".")[2].lower() in IMAGE_EXT and not name.startswith(".")


def is_raw(p: Path) -> bool:
    return p.suffix.lower().lstrip(".") in RAW_EXT


def ext_sidecar(p: Path) -> Path:
    """photo.nef.xmp (darktable, digiKam)."""
    return p.with_name(p.name + ".xmp")


def std_sidecar(p: Path) -> Path:
    """photo.xmp (Lightroom Classic, Capture One, Bridge, RapidRAW) ; photo.XMP s'il existe déjà sous ce nom."""
    upper = p.with_suffix(".XMP")
    return upper if upper.exists() and not p.with_suffix(".xmp").exists() else p.with_suffix(".xmp")


def first_existing(paths: Iterable[Path]) -> Path | None:
    return next((p for p in paths if p.is_file()), None)


def open_readonly(path: Path) -> sqlite3.Connection:
    """Base d'un autre logiciel, en lecture seule (jamais modifiée)."""
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


class FileSource:
    name = ""
    label = ""          # nom affiché comme « version » de la source
    write_gap = 0.0     # pas de course d'écriture comme avec Immich

    def __init__(self, root: Path, data_dir: Path, dry_run: bool = True):
        self.root = Path(root)   # dossier des photos, ou fichier de la bibliothèque du logiciel
        self.dry_run = dry_run
        self.version: str | None = None
        self.writable = False
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.thumbs = Path(data_dir) / "local-thumbs"
        self.db = sqlite3.connect(Path(data_dir) / f"{self.name}-source.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS file (asset_id TEXT PRIMARY KEY, path TEXT UNIQUE, size INTEGER, mtime INTEGER, meta TEXT);
            CREATE TABLE IF NOT EXISTS tag (value TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS tag_asset (value TEXT, asset_id TEXT, PRIMARY KEY (value, asset_id));
        """)
        self._lock = threading.Lock()
        self.et = ExifTool()

    # -- à préciser par chaque source --------------------------------------------------------------------------

    def _files(self) -> Iterable[tuple[str, Path]]:
        """(clé, chemin) de chaque photo."""
        raise NotImplementedError

    def _resolve(self, key: str) -> Path:
        raise NotImplementedError

    def _sidecars(self, p: Path) -> list[Path]:
        """Fichiers XMP de la photo, à écrire (créés s'ils manquent) ; le premier existant est relu."""
        raise NotImplementedError

    def _check_source(self) -> bool:
        """Dossier ou bibliothèque utilisable (sinon self.errors) ; réglages du logiciel dans self.warnings.
        Renvoie True si les fichiers XMP peuvent être écrits."""
        raise NotImplementedError

    # -- état ------------------------------------------------------------------------------------------------

    def check(self) -> bool:
        self.errors, self.warnings = [], []
        if not available():
            self.errors.append(f"ExifTool introuvable : {INSTALL_HINT}")
        can_write = self._check_source()
        self.version = self.label
        self.writable = not self.errors and can_write
        return self.writable

    def status(self) -> dict:
        return {"version": self.version, "errors": self.errors, "warnings": self.warnings, "path": str(self.root)}

    def close(self) -> None:
        self.et.close()
        self.db.close()

    def link(self, asset_id: str) -> str | None:
        return None

    # -- fichiers ---------------------------------------------------------------------------------------------

    def _walk(self):
        for key, p in self._files():
            try:
                st = p.stat()
            except OSError:
                continue
            yield key, st.st_size, st.st_mtime_ns

    @staticmethod
    def asset_id(key: str) -> str:
        return str(uuid.uuid5(NAMESPACE, key))

    def _path(self, asset_id: str) -> Path | None:
        with self._lock:
            r = self.db.execute("SELECT path FROM file WHERE asset_id = ?", (asset_id,)).fetchone()
        return self._resolve(r["path"]) if r else None

    def _asset(self, key: str, size: int, mtime: int, meta: dict) -> dict:
        w, h = meta.get("ImageWidth"), meta.get("ImageHeight")
        if meta.get("Orientation") in (5, 6, 7, 8):
            w, h = h, w
        return {"id": self.asset_id(key), "checksum": f"{size}:{mtime}", "originalFileName": key.rpartition("/")[2],
                "type": "IMAGE", "people": [], "updatedAt": _iso(mtime / 1e9),
                "exifInfo": {"dateTimeOriginal": _taken(meta), "latitude": meta.get("GPSLatitude"),
                             "longitude": meta.get("GPSLongitude"), "make": meta.get("Make"), "model": meta.get("Model"),
                             "lensModel": meta.get("LensModel"), "exifImageWidth": w, "exifImageHeight": h}}

    def iter_assets(self, updated_after: str | None = None, size: int = 250, with_exif: bool = True,
                    kind: str = "IMAGE", with_people: bool = False):
        """Photos de la source. updated_after : seulement celles dont le fichier a changé depuis le dernier passage
        (tout, sinon). with_exif=False : liste rapide sans lecture des métadonnées (contrôle des suppressions)."""
        if kind != "IMAGE":
            return
        with self._lock:
            # Métadonnées vides : jamais lues (ExifTool renvoie toujours au moins SourceFile), par exemple sous Windows
            # avant la 0.1.2 : relues au prochain passage.
            known = {r["path"]: (r["size"], r["mtime"])
                     for r in self.db.execute("SELECT path, size, mtime FROM file WHERE meta IS NOT NULL AND meta != '{}'")}
        batch = []
        for key, sz, mt in self._walk():
            if not with_exif:
                yield {"id": self.asset_id(key), "people": []}
                continue
            if updated_after is not None and known.get(key) == (sz, mt):
                continue
            batch.append((key, sz, mt))
            if len(batch) >= 200:
                yield from self._read(batch)
                batch = []
        yield from self._read(batch)

    def _read(self, batch: list[tuple]):
        paths = {key: str(self._resolve(key)) for key, _, _ in batch}
        found = {same_file(m.get("SourceFile") or ""): m for m in self.et.read(list(paths.values()), READ_TAGS)}
        metas = {p: found[same_file(p)] for p in paths.values() if same_file(p) in found}
        with self._lock:
            for key, sz, mt in batch:
                self.db.execute("INSERT OR REPLACE INTO file VALUES (?, ?, ?, ?, ?)",
                                (self.asset_id(key), key, sz, mt, json.dumps(metas.get(paths[key], {}))))
            self.db.commit()
        for key, sz, mt in batch:
            yield self._asset(key, sz, mt, metas.get(paths[key], {}))

    def get_asset(self, asset_id: str, missing_ok: bool = False) -> dict | None:
        """Photo avec ses mots-clés et sa description actuels (fichier XMP)."""
        p = self._path(asset_id)
        if p is None or not p.exists():
            if missing_ok:
                return None
            raise FileNotFoundError(asset_id)
        st = p.stat()
        with self._lock:
            r = self.db.execute("SELECT path, meta FROM file WHERE asset_id = ?", (asset_id,)).fetchone()
        a = self._asset(r["path"], st.st_size, st.st_mtime_ns, json.loads(r["meta"] or "{}"))
        side = first_existing(self._sidecars(p))
        tags, desc, updated = [], "", st.st_mtime
        if side:
            x = (self.et.read([str(side)], ["XMP-lr:HierarchicalSubject", "XMP-dc:Description"]) or [{}])[0]
            hs = x.get("HierarchicalSubject") or []
            tags = [{"id": v, "value": v} for v in ([hs] if isinstance(hs, str) else hs) for v in [v.replace("|", "/")]]
            desc = str(x.get("Description") or "")
            updated = max(updated, side.stat().st_mtime)
        a["tags"], a["exifInfo"]["description"], a["updatedAt"] = tags, desc, _iso(updated)
        return a

    # -- images ----------------------------------------------------------------------------------------------

    def _image(self, p: Path) -> Image.Image:
        if is_raw(p):   # RAW : le plus grand JPEG intégré par l'appareil
            for tag in ("JpgFromRaw", "PreviewImage", "OtherImage"):
                out = self._binary(p, tag)
                if out:
                    return Image.open(io.BytesIO(out))
            raise ValueError(f"RAW sans aperçu intégré : {p.name}")
        return Image.open(p)

    def _binary(self, p: Path, tag: str) -> bytes | None:
        r = subprocess.run([*(command() or ["exiftool"]), "-b", f"-{tag}", str(p)], capture_output=True, timeout=60, **NO_WINDOW)
        return r.stdout or None

    def thumbnail(self, asset_id: str, size: str = "preview") -> bytes:
        """Vignette (250 px) ou aperçu (1440 px sur le petit côté), en JPEG, gardés en cache tant que le fichier ne
        change pas."""
        p = self._path(asset_id)
        if p is None or not p.exists():
            raise FileNotFoundError(asset_id)
        side = SIZES.get(size, SIZES["preview"])
        cache = self.thumbs / f"{asset_id}-{side}.jpg"
        if cache.exists() and cache.stat().st_mtime >= p.stat().st_mtime:
            return cache.read_bytes()
        im = ImageOps.exif_transpose(self._image(p)).convert("RGB")
        if min(im.size) > side:
            k = side / min(im.size)
            im = im.resize((round(im.width * k), round(im.height * k)), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        self.thumbs.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f"{cache.name}.{uuid.uuid4().hex}.tmp")   # unique : plusieurs requêtes pour la même vignette
        tmp.write_bytes(buf.getvalue())
        tmp.replace(cache)
        return buf.getvalue()

    def original(self, asset_id: str) -> bytes:
        p = self._path(asset_id)
        if p is None or not p.exists():
            raise FileNotFoundError(asset_id)
        if is_raw(p):
            buf = io.BytesIO()
            self._image(p).save(buf, "JPEG", quality=95)
            return buf.getvalue()
        return p.read_bytes()

    # -- écriture (fichiers XMP) ------------------------------------------------------------------------------

    def _write(self, asset_id: str, assignments: list[str]) -> None:
        if self.dry_run:
            return
        p = self._path(asset_id)
        if p is None:
            raise FileNotFoundError(asset_id)
        for side in self._sidecars(p):
            self.et.write(str(side), assignments)

    @staticmethod
    def _xmp(value: str) -> tuple[str, str]:
        """Tag « Nature/Aves/… » -> (hiérarchie XMP « Nature|Aves|… », dernier niveau)."""
        parts = [x.replace("|", "-") for x in value.split("/")]
        return "|".join(parts), parts[-1]

    def list_tags(self) -> list[dict]:
        with self._lock:
            return [{"id": r["value"], "value": r["value"]} for r in self.db.execute("SELECT value FROM tag")]

    def upsert_tags(self, values: list[str]) -> list[dict]:
        if not self.dry_run:
            with self._lock:
                # Comme Immich, crée aussi les niveaux parents.
                self.db.executemany("INSERT OR IGNORE INTO tag VALUES (?)",
                                    [(v.rsplit("/", i)[0],) for v in values for i in range(v.count("/") + 1)])
                self.db.commit()
        return [{"id": v, "value": v} for v in values]

    def bulk_tag(self, tag_ids: list[str], asset_ids: list[str]) -> None:
        for aid in asset_ids:
            args = []
            for v in tag_ids:
                h, leaf = self._xmp(v)
                args += [f"-XMP-lr:HierarchicalSubject-={escape(h)}", f"-XMP-lr:HierarchicalSubject+={escape(h)}",
                         f"-XMP-dc:Subject-={escape(leaf)}", f"-XMP-dc:Subject+={escape(leaf)}"]   # -= puis += : pas de doublon
            self._write(aid, args)
            if not self.dry_run:
                with self._lock:
                    self.db.executemany("INSERT OR IGNORE INTO tag_asset VALUES (?, ?)", [(v, aid) for v in tag_ids])
                    self.db.commit()

    def untag_assets(self, tag_id: str, asset_ids: list[str]) -> None:
        h, leaf = self._xmp(tag_id)
        for aid in asset_ids:
            self._write(aid, [f"-XMP-lr:HierarchicalSubject-={escape(h)}", f"-XMP-dc:Subject-={escape(leaf)}"])
            if not self.dry_run:
                with self._lock:
                    self.db.execute("DELETE FROM tag_asset WHERE value = ? AND asset_id = ?", (tag_id, aid))
                    self.db.commit()

    def count_assets_with_tag(self, tag_id: str) -> int:
        with self._lock:
            return self.db.execute("SELECT count(*) FROM tag_asset WHERE value = ?", (tag_id,)).fetchone()[0]

    def delete_tag(self, tag_id: str) -> None:
        if not self.dry_run:
            with self._lock:
                self.db.execute("DELETE FROM tag WHERE value = ?", (tag_id,))
                self.db.commit()

    def set_description(self, asset_id: str, description: str) -> None:
        self._write(asset_id, [f"-XMP-dc:Description={escape(description)}"])


def check_folder(src: FileSource) -> bool:
    """Dossier des photos : présent, et accessible en écriture pour les fichiers XMP."""
    if not src.root.is_dir():
        src.errors.append(f"dossier des photos introuvable : {src.root}")
        return False
    if not os.access(src.root, os.W_OK):
        src.warnings.append(f"dossier en lecture seule : {src.root} (pas d'écriture des fichiers XMP)")
        return False
    return True
