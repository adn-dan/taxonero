""""darktable" source: the photos of the darktable library (library.db, read without ever writing it).

darktable stores each photo in a photo.nef.xmp sidecar, and each duplicate in photo_01.nef.xmp, photo_02…
(version 0 without suffix, see dt_image_path_append_version_no_db): results are written to all those of the
photo. darktable reads keywords in Xmp.lr.hierarchicalSubject (failing that Xmp.dc.subject), separator "|".

darktable keeps its own database and rewrites its sidecars from it: for it to pick up the keywords written
here, the "look for updated XMP files on startup" option (Preferences > storage) must be enabled; it then
offers at startup to read the changed XMP files again. It is checked in darktablerc (run_crawler_on_start), like
the writing of sidecars (write_sidecar_files: never = keywords of the XMP files ignored on import).
"""

import os
import sqlite3
import sys
from pathlib import Path

from .files import FileSource, ext_sidecar, first_existing, is_image, open_readonly


def default_library() -> Path | None:
    """library.db of the current installation (package, Flatpak, Windows)."""
    home = Path.home()
    if sys.platform == "win32":
        dirs = [Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "darktable"]
    else:
        dirs = [Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "darktable",
                home / ".var" / "app" / "org.darktable.Darktable" / "config" / "darktable"]
    return first_existing(d / "library.db" for d in dirs)


def version_name(p: Path, version: int) -> Path:
    """File of a duplicate: photo.nef, photo_01.nef…"""
    return p if version == 0 else p.with_name(f"{p.stem}_{version:02d}{p.suffix}")


def read_rc(path: Path) -> dict[str, str]:
    """darktablerc: one key=value line per setting."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in lines if "=" in line)


class Darktable(FileSource):
    name = "darktable"
    label = "darktable"

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        db = open_readonly(self.root)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def _files(self):
        for r in self._query("SELECT DISTINCT f.folder, i.filename FROM images i JOIN film_rolls f ON f.id = i.film_id "
                             "ORDER BY f.folder, i.filename"):
            if is_image(r["filename"]):
                p = Path(r["folder"]) / r["filename"]
                yield p.as_posix(), p

    def _resolve(self, key: str) -> Path:
        return Path(key)

    def _sidecars(self, p: Path) -> list[Path]:
        versions = [r["version"] for r in self._query(
            "SELECT DISTINCT i.version FROM images i JOIN film_rolls f ON f.id = i.film_id "
            "WHERE f.folder = ? AND i.filename = ? ORDER BY i.version", (str(p.parent), p.name))]
        return [ext_sidecar(version_name(p, v)) for v in versions or [0]]

    def _check_source(self) -> bool:
        if not self.root.is_file():
            self.errors.append(f"darktable library not found: {self.root}")
            return False
        try:
            self._query("SELECT 1 FROM images JOIN film_rolls ON film_rolls.id = images.film_id LIMIT 1")
        except sqlite3.Error as e:
            self.errors.append(f"darktable library unreadable ({self.root.name}): {e}")
            return False
        rc = read_rc(self.root.parent / "darktablerc")
        if rc.get("write_sidecar_files") == "never":
            self.warnings.append("darktable does not use XMP files (Preferences > storage, \"write sidecar file "
                                 "for each image\" set to \"never\"): the keywords written here will not appear in it")
        if rc.get("run_crawler_on_start") != "true":
            self.warnings.append("darktable: enable \"look for updated XMP files on startup\" (Preferences > storage) "
                                 "so that it reads the keywords written here")
        return True
