"""Source « dossier local » : les photos d'un dossier (et de ses sous-dossiers), sans Immich.

Résultats dans un fichier XMP à côté de chaque photo (voir files.py), lu par digiKam, darktable, Lightroom Classic
(fichiers RAW), Adobe Bridge et Immich (bibliothèques externes). Nom du sidecar : celui qui existe déjà (photo.jpg.xmp
ou photo.xmp) ; sinon photo.xmp pour un RAW (convention Lightroom, Capture One, Bridge), photo.jpg.xmp pour le reste
(digiKam, darktable).
"""

import os
from pathlib import Path

from .files import FileSource, check_folder, ext_sidecar, first_existing, is_image, is_raw, std_sidecar

# Dossiers techniques (Synology, corbeilles, Windows) ; les dossiers cachés (.xxx) sont toujours ignorés.
SKIP_DIRS = {"@eaDir", "#recycle", "@Recycle", "$RECYCLE.BIN", "System Volume Information", "lost+found"}
# Bibliothèques d'autres logiciels : leurs copies et aperçus internes ne sont pas des photos à part entière
# (photothèque Apple Photos, aperçus de Lightroom Classic).
SKIP_SUFFIXES = (".photoslibrary", ".lrdata", ".lrlibrary")


class LocalFolder(FileSource):
    name = "local"
    label = "dossier local"

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
