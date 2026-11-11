""""Lightroom Classic" source: the photos of a .lrcat catalog (SQLite database, read without ever writing it).

Path of a photo: AgLibraryRootFolder.absolutePath + AgLibraryFolder.pathFromRoot + AgLibraryFile.baseName
.extension (Adobe_images.rootFile; virtual copies share the same file, counted once). Lightroom
locks the catalog while it is open: it is only read once Lightroom is closed.

Writing: photo.xmp next to RAW files, the sidecar Lightroom reads and writes itself. For a JPEG, TIFF, PSD, HEIC
or DNG, Lightroom only reads metadata in the file itself, which is never modified here: the sidecar
written (photo.jpg.xmp) is for the other applications, not for Lightroom. In Lightroom, Metadata > "Read Metadata from
Files" picks up the keywords; with "Automatically write changes into XMP" (Catalog Settings), sidecars
also contain its settings and nothing is lost when reading them back.
"""

import sqlite3
from pathlib import Path

from ..store import RAW_EXT
from .files import FileSource, ext_sidecar, is_image, open_readonly, std_sidecar

# Files whose metadata Lightroom reads in an .xmp sidecar (proprietary RAW files; DNG carries it inside).
SIDECAR_EXT = RAW_EXT - {"dng"}
FILES = ("SELECT DISTINCT r.absolutePath || f.pathFromRoot || i.baseName || '.' || i.extension AS path "
         "FROM Adobe_images a JOIN AgLibraryFile i ON i.id_local = a.rootFile "
         "JOIN AgLibraryFolder f ON f.id_local = i.folder JOIN AgLibraryRootFolder r ON r.id_local = f.rootFolder")


def default_catalog() -> Path | None:
    """Catalog at the default location (Pictures/Lightroom), the most recently modified."""
    found = sorted((Path.home() / "Pictures" / "Lightroom").glob("*.lrcat"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


class CatalogLocked(RuntimeError):
    pass


class Lightroom(FileSource):
    name = "lightroom"
    label = "Lightroom Classic"

    def _query(self, sql: str) -> list[sqlite3.Row]:
        try:
            db = open_readonly(self.root)
            try:
                return db.execute(sql).fetchall()
            finally:
                db.close()
        except sqlite3.OperationalError as e:
            if "locked" in str(e):
                raise CatalogLocked("Lightroom catalog locked: close Lightroom Classic so that it can be read") from e
            raise

    def _files(self):
        for r in self._query(FILES + " ORDER BY path"):
            if is_image(r["path"].rpartition("/")[2]):
                yield r["path"], Path(r["path"])

    def _resolve(self, key: str) -> Path:
        return Path(key)

    def _sidecars(self, p: Path) -> list[Path]:
        # photo.xmp of a RAW: Lightroom's; the JPEG of a RAW+JPEG pair has its own (photo.jpg.xmp).
        return [std_sidecar(p) if p.suffix.lower().lstrip(".") in SIDECAR_EXT else ext_sidecar(p)]

    def _check_source(self) -> bool:
        if not self.root.is_file():
            self.errors.append(f"Lightroom catalog not found: {self.root}")
            return False
        if self.root.with_name(self.root.name + ".lock").exists():
            self.warnings.append("Lightroom Classic is open: the catalog will be read when it closes")
            return True
        try:
            names = [r["path"].rpartition("/")[2] for r in self._query(FILES)]
        except CatalogLocked as e:
            self.warnings.append(str(e))
            return True
        except sqlite3.Error as e:
            self.errors.append(f"Lightroom catalog unreadable ({self.root.name}): {e}")
            return False
        inside = sum(1 for n in names if is_image(n) and n.rpartition(".")[2].lower() not in SIDECAR_EXT)
        if inside:
            self.warnings.append(f"{inside} JPEG, TIFF, HEIC or DNG photo(s): Lightroom reads their keywords in the "
                                 "file itself, never modified here; only RAW files receive them in Lightroom")
        return True
