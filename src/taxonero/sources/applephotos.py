""""Apple Photos" source (macOS): the photos of a Photos library (.photoslibrary).

Reading: the library database, read-only, with osxphotos (MIT). Images: the previews Photos keeps in the library
(enough for the analysis, and present even when the originals stay in iCloud); the original when there is none.

Writing: done by Photos itself, through AppleScript (photoscript, MIT), never in its database:
- keywords: Photos has no hierarchy, so the tag is written whole as one keyword ("Nature/Aves/Paridae/Great tit"):
  removing it can never take away a keyword of the user;
- description: the same delimited block as with Immich.
AppleScript acts on the library open in Photos: writing is enabled only when that is the chosen one. macOS asks once
to allow Taxonero to control Photos (System Settings > Privacy & Security > Automation).

Videos are not supported, like the other file sources.
"""

import io
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from .files import FileSource

SUFFIX = ".photoslibrary"
MACOS = sys.platform == "darwin"


def default_library() -> Path | None:
    """System library at its usual location, if it exists."""
    p = Path.home() / "Pictures" / f"Photos Library{SUFFIX}"
    return p if p.is_dir() else None


def _local(d: datetime | None) -> str | None:
    """Capture time as shown by Photos (time of the place, without time zone), like the other sources."""
    return d.strftime("%Y-%m-%dT%H:%M:%S") if d else None


def _iso(d: datetime | None) -> str:
    d = d or datetime.fromtimestamp(0, timezone.utc)
    if d.tzinfo is None:
        d = d.astimezone()
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _photos_running() -> bool:
    """The Photos application is open (pgrep, without starting it)."""
    try:
        return subprocess.run(["pgrep", "-x", "Photos"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class ApplePhotos(FileSource):
    name = "applephotos"
    label = "Apple Photos"

    def __init__(self, root: Path, data_dir: Path, dry_run: bool = True, sid: str = ""):
        super().__init__(root, data_dir, dry_run, sid)
        self._photos: dict = {}   # uuid -> PhotoInfo, from the last reading of the library
        self._stamp = None        # state of the library database at that reading
        self._loading = threading.Lock()
        self._can_script = False  # the library is the one open in Photos (AppleScript reaches its photos)

    # -- library -------------------------------------------------------------------------------------------

    def _db_stamp(self) -> tuple:
        """Size and date of the library database (and its journal): it changes whenever Photos saves something."""
        out = []
        for name in ("Photos.sqlite", "Photos.sqlite-wal"):
            try:
                st = (self.root / "database" / name).stat()
                out.append((st.st_size, st.st_mtime_ns))
            except OSError:
                out.append(None)
        return tuple(out)

    def _load(self) -> dict:
        """Photos of the library. Reading it takes a while on a large library (osxphotos reads the whole database):
        done again only when Photos has changed it since, and by one thread at a time."""
        with self._loading:
            stamp = self._db_stamp()
            if self._photos and stamp == self._stamp and stamp != (None, None):
                return self._photos
            import osxphotos
            db = osxphotos.PhotosDB(dbfile=str(self.root))
            self._photos = {p.uuid: p for p in db.photos(images=True, movies=False)}
            self._stamp = stamp
            return self._photos

    def _uuid(self, asset_id: str) -> str | None:
        with self._lock:
            r = self.db.execute("SELECT path FROM file WHERE asset_id = ?", (asset_id,)).fetchone()
        return r["path"] if r else None

    def _info(self, asset_id: str):
        uuid = self._uuid(asset_id)
        if uuid is None:
            return None
        if uuid not in self._photos:
            self._load()
        return self._photos.get(uuid)

    @staticmethod
    def _script():
        import photoscript
        return photoscript

    def _check_source(self) -> bool:
        if not MACOS:
            self.errors.append("Apple Photos: macOS only")
            return False
        if not self.root.is_dir():
            self.errors.append(f"Photos library not found: {self.root}")
            return False
        try:
            photos = self._load()
        except ImportError:
            self.errors.append("osxphotos missing: reinstall the application (--reinstall)")
            return False
        except Exception as e:   # no access (Privacy & Security), damaged or too recent library
            self.errors.append(f"Photos library unreadable ({type(e).__name__}: {e}): allow Taxonero in "
                               "System Settings > Privacy & Security > Full Disk Access")
            return False
        self._can_script = False
        first = next(iter(photos), None)
        if first is None:
            return False
        # Testing AppleScript would start Photos: Taxonero never opens it by itself, it writes while Photos is open.
        if not _photos_running():
            self.warnings.append("Photos is not open: the keywords are written while it is (read only meanwhile)")
            return False
        try:
            self._script().Photo(first)
            self._can_script = True
        except ValueError:
            self.warnings.append("this library is not the one open in Photos: open it in Photos to write the "
                                 "keywords (read only meanwhile)")
        except Exception as e:   # Automation permission refused, Photos not starting
            self.warnings.append(f"Photos cannot be controlled ({type(e).__name__}: {e}): allow Taxonero in "
                                 "System Settings > Privacy & Security > Automation (read only meanwhile)")
        return self._can_script

    def check(self) -> bool:
        # No ExifTool needed: the previews are JPEG files, and the metadata comes from the library.
        self.errors, self.warnings = [], []
        can_write = self._check_source()
        self.version = self.label
        self.writable = not self.errors and can_write
        return self.writable

    def count_files(self) -> int:
        return len(self._photos or self._load())

    # -- reading -------------------------------------------------------------------------------------------

    def _asset(self, p, aid: str) -> dict:
        exif = p.exif_info
        lat, lon = p.location
        addr = p.place.address if p.place else None
        w, h = p.width, p.height
        return {"id": aid, "checksum": p.fingerprint or str(p.original_filesize), "originalFileName": p.original_filename,
                "type": "IMAGE", "people": list(p.persons), "updatedAt": _iso(p.date_modified or p.date),
                "exifInfo": {"dateTimeOriginal": _local(p.date), "latitude": lat, "longitude": lon,
                             "make": exif.camera_make if exif else None, "model": exif.camera_model if exif else None,
                             "lensModel": exif.lens_model if exif else None, "exifImageWidth": w, "exifImageHeight": h,
                             # Place found by Photos: no need to ask for it again later (worker.refresh_places).
                             "city": (addr.city if addr else None) or "", "state": addr.state_province if addr else None,
                             "country": addr.country if addr else None,
                             "description": p.description or ""},
                "tags": [{"id": k, "value": k} for k in p.keywords]}

    def iter_assets(self, updated_after: str | None = None, size: int = 250, with_exif: bool = True,
                    kind: str = "IMAGE", with_people: bool = False):
        if kind != "IMAGE":
            return
        photos = self._load()
        since = datetime.fromisoformat(updated_after.replace("Z", "+00:00")).timestamp() if updated_after else None
        # Asset id -> Photos uuid, saved before any photo is handed over: a scan stopped half way leaves no photo that
        # could not be found again (size and mtime unused here).
        with self._lock:
            self.db.executemany("INSERT OR REPLACE INTO file VALUES (?, ?, 0, 0, '{}')",
                                [(self.asset_id(uuid), uuid) for uuid in photos])
            self.db.commit()
        for uuid, p in photos.items():
            if self.on_file:
                self.on_file()
            aid = self.asset_id(uuid)
            if not with_exif:
                yield {"id": aid, "people": list(p.persons)}
                continue
            modified = p.date_modified or p.date
            if since is not None and modified and modified.timestamp() < since:
                continue
            yield self._asset(p, aid)

    def get_asset(self, asset_id: str, missing_ok: bool = False) -> dict | None:
        """Photo with its keywords and description as they are now: asked from Photos when it can be controlled
        (after a write, the library database is not up to date yet), otherwise from the last reading."""
        p = self._info(asset_id)
        if p is None:
            if missing_ok:
                return None
            raise FileNotFoundError(asset_id)
        a = self._asset(p, asset_id)
        if self._can_script:
            try:
                live = self._script().Photo(p.uuid)
            except ValueError:   # deleted in Photos since the last reading
                if missing_ok:
                    return None
                raise FileNotFoundError(asset_id) from None
            a["tags"] = [{"id": k, "value": k} for k in live.keywords]
            a["exifInfo"]["description"] = live.description or ""
        return a

    # -- images ----------------------------------------------------------------------------------------------

    def _path(self, asset_id: str) -> Path | None:
        """Image to show and analyse: the largest preview made by Photos (edits included), otherwise the original."""
        p = self._info(asset_id)
        if p is None:
            return None
        for d in p.path_derivatives:
            if Path(d).is_file():
                return Path(d)
        return Path(p.path) if p.path else None

    def original(self, asset_id: str) -> bytes:
        """Original for small subjects; the preview when it is only in iCloud."""
        p = self._info(asset_id)
        if p is not None and p.path and Path(p.path).is_file():
            path = Path(p.path)
            # HEIC (iPhone) or RAW (its embedded preview, ExifTool): converted to JPEG.
            return path.read_bytes() if path.suffix.lower() in (".jpg", ".jpeg") else self._jpeg(path)
        found = self._path(asset_id)
        if found is None:
            raise FileNotFoundError(asset_id)
        return found.read_bytes()

    def _jpeg(self, path: Path) -> bytes:
        buf = io.BytesIO()
        self._image(path).convert("RGB").save(buf, "JPEG", quality=95)
        return buf.getvalue()

    # -- writing (through Photos) -------------------------------------------------------------------------------

    def _photo(self, asset_id: str):
        uuid = self._uuid(asset_id)
        if uuid is None:
            raise FileNotFoundError(asset_id)
        try:
            return self._script().Photo(uuid)
        except ValueError:
            raise FileNotFoundError(asset_id) from None

    def _set_keywords(self, asset_id: str, change) -> None:
        if self.dry_run:
            return
        ph = self._photo(asset_id)
        before = list(ph.keywords)
        after = change(before)
        if after != before:
            ph.keywords = after

    def bulk_tag(self, tag_ids: list[str], asset_ids: list[str]) -> None:
        for aid in asset_ids:
            self._set_keywords(aid, lambda kw: kw + [v for v in tag_ids if v not in kw])
            if not self.dry_run:
                with self._lock:
                    self.db.executemany("INSERT OR IGNORE INTO tag_asset VALUES (?, ?)", [(v, aid) for v in tag_ids])
                    self.db.commit()

    def untag_assets(self, tag_id: str, asset_ids: list[str]) -> None:
        for aid in asset_ids:
            self._set_keywords(aid, lambda kw: [k for k in kw if k != tag_id])
            if not self.dry_run:
                with self._lock:
                    self.db.execute("DELETE FROM tag_asset WHERE value = ? AND asset_id = ?", (tag_id, aid))
                    self.db.commit()

    def set_description(self, asset_id: str, description: str) -> None:
        if self.dry_run:
            return
        self._photo(asset_id).description = description
