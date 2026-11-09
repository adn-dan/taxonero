"""Base of the "file" sources: photos on disk, results in an XMP file next to each one ("sidecar").

Reading: files and metadata (date, GPS, camera) through ExifTool. Writing, without ever touching the photos:
- hierarchical keywords (XMP-lr:HierarchicalSubject, "Nature|Aves|Paridae|Great tit") and the
  last level as a plain keyword (XMP-dc:Subject) for applications without hierarchy;
- description (XMP-dc:Description), with the same delimited block as with Immich.

Each source specifies where the files come from (a folder, the library of an application) and the name of the sidecars
its application reads: _files, _resolve, _sidecars, and _check_source for the state. Same interface as ImmichClient
(see sources/__init__.py): "tags" are identified by their value ("Nature/Aves/…") and assets by a
stable identifier derived from the file key (path relative to the folder, or absolute for a library). Videos
are not supported.
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
SIZES = {"thumbnail": 250, "preview": 1440}   # short side, like Immich thumbnails and previews
NAMESPACE = uuid.UUID("6f1c2b8e-6a7d-4a43-9d8e-4f3e5c2a1b90")

try:   # HEIC (iPhone) : greffon facultatif
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _taken(meta: dict) -> str | None:
    """Capture date, camera time (without time zone), in ISO format."""
    for k in ("DateTimeOriginal", "CreateDate"):
        v = str(meta.get(k) or "")
        if len(v) >= 19 and v[:4].isdigit() and not v.startswith("0000"):
            return f"{v[:4]}-{v[5:7]}-{v[8:10]}T{v[11:19]}"
    return None


WINDOWS = sys.platform == "win32"


def same_file(path: str) -> str:
    """Path comparable with ExifTool's SourceFile, which on Windows replaces \\ with /."""
    return path.replace("\\", "/") if WINDOWS else path


def is_image(name: str) -> bool:
    return name.rpartition(".")[2].lower() in IMAGE_EXT and not name.startswith(".")


def is_raw(p: Path) -> bool:
    return p.suffix.lower().lstrip(".") in RAW_EXT


def ext_sidecar(p: Path) -> Path:
    """photo.nef.xmp (darktable, digiKam)."""
    return p.with_name(p.name + ".xmp")


def std_sidecar(p: Path) -> Path:
    """photo.xmp (Lightroom Classic, Capture One, Bridge, RapidRAW); photo.XMP if it already exists under that name."""
    upper = p.with_suffix(".XMP")
    return upper if upper.exists() and not p.with_suffix(".xmp").exists() else p.with_suffix(".xmp")


def first_existing(paths: Iterable[Path]) -> Path | None:
    return next((p for p in paths if p.is_file()), None)


def open_readonly(path: Path) -> sqlite3.Connection:
    """Database of another application, read-only (never modified)."""
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


class FileSource:
    name = ""
    label = ""          # name shown as the "version" of the source
    write_gap = 0.0     # no write race as with Immich

    def __init__(self, root: Path, data_dir: Path, dry_run: bool = True, sid: str = ""):
        self.root = Path(root)   # photo folder, or library file of the application
        self.dry_run = dry_run
        # Added source (sid: its id, '' for the main one): its own file list, and photo ids of its own, otherwise two
        # folders that both hold a "IMG_0001.jpg" would give it the same id.
        self.sid = sid
        self.version: str | None = None
        self.writable = False
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.thumbs = Path(data_dir) / "local-thumbs"
        self.db = sqlite3.connect(Path(data_dir) / f"{self.name}-source{'-' + sid if sid else ''}.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS file (asset_id TEXT PRIMARY KEY, path TEXT UNIQUE, size INTEGER, mtime INTEGER, meta TEXT);
            CREATE TABLE IF NOT EXISTS tag (value TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS tag_asset (value TEXT, asset_id TEXT, PRIMARY KEY (value, asset_id));
        """)
        self._lock = threading.Lock()
        self.et = ExifTool()
        self.on_file = None   # called for each file walked (scan progress)
        self._odd_names: set[str] = set()

    # -- to be specified by each source --------------------------------------------------------------------------

    def _files(self) -> Iterable[tuple[str, Path]]:
        """(key, path) of each photo."""
        raise NotImplementedError

    def _resolve(self, key: str) -> Path:
        raise NotImplementedError

    def _sidecars(self, p: Path) -> list[Path]:
        """XMP files of the photo, to write (created if missing); the first existing one is read back."""
        raise NotImplementedError

    def _check_source(self) -> bool:
        """Folder or library usable (otherwise self.errors); application settings in self.warnings.
        Return True if the XMP files can be written."""
        raise NotImplementedError

    # -- state ------------------------------------------------------------------------------------------------

    def check(self) -> bool:
        self.errors, self.warnings = [], []
        if not available():
            self.errors.append(f"ExifTool not found: {INSTALL_HINT}")
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

    # -- files ------------------------------------------------------------------------------------------------

    def _usable(self):
        """Photos of the source, without those whose name holds a line break: ExifTool takes its arguments one per
        line, so such a name cannot be passed to it (and would stop the whole batch). Reported once each."""
        for key, p in self._files():
            if "\n" in key or "\r" in key or "\n" in str(p) or "\r" in str(p):
                if key not in self._odd_names:
                    self._odd_names.add(key)
                    log.warning("photo skipped, line break in its name: %r", str(p))
                continue
            yield key, p

    def count_files(self) -> int:
        """Number of photos of the source (scan progress), without reading their metadata."""
        return sum(1 for _ in self._usable())

    def _walk(self):
        for key, p in self._usable():
            if self.on_file:   # scan progress (Worker.scan), also for unchanged files
                self.on_file()
            try:
                st = p.stat()
            except OSError:
                continue
            yield key, st.st_size, st.st_mtime_ns

    def asset_id(self, key: str) -> str:
        return str(uuid.uuid5(NAMESPACE, f"{self.sid}:{key}" if self.sid else key))

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
        """Photos of the source. updated_after: only those whose file changed since the last pass
        (everything otherwise). with_exif=False: fast list without reading the metadata (deletion check)."""
        if kind != "IMAGE":
            return
        with self._lock:
            # Empty metadata: never read (ExifTool always returns at least SourceFile), for instance on Windows
            # before 0.1.2: read again at the next pass.
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
        """Photo with its current keywords and description (XMP file)."""
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
        if is_raw(p):   # RAW: the largest JPEG embedded by the camera
            for tag in ("JpgFromRaw", "PreviewImage", "OtherImage"):
                out = self._binary(p, tag)
                if out:
                    return Image.open(io.BytesIO(out))
            raise ValueError(f"RAW without an embedded preview: {p.name}")
        return Image.open(p)

    def _binary(self, p: Path, tag: str) -> bytes | None:
        r = subprocess.run([*(command() or ["exiftool"]), "-b", f"-{tag}", str(p)], capture_output=True, timeout=60, **NO_WINDOW)
        return r.stdout or None

    def thumbnail(self, asset_id: str, size: str = "preview") -> bytes:
        """Thumbnail (250 px) or preview (1440 px on the short side), as JPEG, cached as long as the file does not
        change."""
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
        tmp = cache.with_name(f"{cache.name}.{uuid.uuid4().hex}.tmp")   # unique: several requests for the same thumbnail
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

    # -- writing (XMP files) ------------------------------------------------------------------------------

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
        """Tag "Nature/Aves/…" -> (XMP hierarchy "Nature|Aves|…", last level)."""
        parts = [x.replace("|", "-") for x in value.split("/")]
        return "|".join(parts), parts[-1]

    def list_tags(self) -> list[dict]:
        with self._lock:
            return [{"id": r["value"], "value": r["value"]} for r in self.db.execute("SELECT value FROM tag")]

    def upsert_tags(self, values: list[str]) -> list[dict]:
        if not self.dry_run:
            with self._lock:
                # Like Immich, also creates the parent levels.
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
                         f"-XMP-dc:Subject-={escape(leaf)}", f"-XMP-dc:Subject+={escape(leaf)}"]   # -= then +=: no duplicate
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
    """Photo folder: present, and writable for the XMP files."""
    if not src.root.is_dir():
        src.errors.append(f"photo folder not found: {src.root}")
        return False
    if not os.access(src.root, os.W_OK):
        src.warnings.append(f"read-only folder: {src.root} (XMP files not written)")
        return False
    return True
