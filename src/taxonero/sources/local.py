""""Local folder" source: the photos of a folder (and of its subfolders), without Immich.

Results in an XMP file next to each photo (see files.py), read by digiKam, darktable, Lightroom Classic
(RAW files), Adobe Bridge and Immich (external libraries). Sidecar name: the one that already exists (photo.jpg.xmp
or photo.xmp); otherwise photo.xmp for a RAW (Lightroom, Capture One, Bridge convention), photo.jpg.xmp for the rest
(digiKam, darktable).
"""

import os
from pathlib import Path

from .files import FileSource, check_folder, ext_sidecar, first_existing, is_image, is_raw, std_sidecar

# Technical folders (Synology, recycle bins, Windows); hidden folders (.xxx) are always ignored.
SKIP_DIRS = {"@eaDir", "#recycle", "@Recycle", "$RECYCLE.BIN", "System Volume Information", "lost+found"}
# Libraries of other applications: their internal copies and previews are not photos in their own right
# (Apple Photos library, Lightroom Classic previews).
SKIP_SUFFIXES = (".photoslibrary", ".lrdata", ".lrlibrary")


class LocalFolder(FileSource):
    name = "local"
    label = "local folder"

    def _files(self):
        for d, dirs, files in os.walk(self.root):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS and not x.lower().endswith(SKIP_SUFFIXES))
            for f in sorted(files):
                if is_image(f):
                    p = Path(d) / f
                    yield p.relative_to(self.root).as_posix(), p

    def _resolve(self, key: str) -> Path:
        return self.root / key

    def _sidecars(self, p: Path) -> list[Path]:
        found = first_existing((ext_sidecar(p), p.with_suffix(".xmp")))
        return [found or (std_sidecar(p) if is_raw(p) else ext_sidecar(p))]

    def _check_source(self) -> bool:
        return check_folder(self)
