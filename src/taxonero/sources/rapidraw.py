"""Source « RapidRAW » : un dossier de photos ouvert dans RapidRAW.

RapidRAW range ses réglages et ses mots-clés dans photo.nef.rrdata (JSON à lui, jamais écrit ici). Avec la
synchronisation XMP active (Réglages > « XMP sync », clé enableXmpSync de settings.json ; absente = inactive), il
lit le sidecar photo.xmp (ou photo.XMP) en parcourant le dossier et ajoute à ses mots-clés ceux de Xmp.dc.subject
(sync_metadata_from_xmp). Il ne fait qu'ajouter : un mot-clé retiré du XMP reste dans RapidRAW.
"""

import json
import os
import sys
from pathlib import Path

from .files import first_existing, std_sidecar
from .local import LocalFolder

APP_ID = "io.github.CyberTimon.RapidRAW"   # identifiant Tauri : nom du dossier de ses réglages


def settings_file() -> Path | None:
    """settings.json de RapidRAW (dossier de données de l'application selon Tauri ; Flatpak)."""
    home = Path.home()
    if sys.platform == "win32":
        dirs = [Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming") / APP_ID]
    elif sys.platform == "darwin":
        dirs = [home / "Library" / "Application Support" / APP_ID]
    else:
        dirs = [Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / APP_ID,
                home / ".var" / "app" / APP_ID / "data" / APP_ID]
    return first_existing(d / "settings.json" for d in dirs)


class RapidRaw(LocalFolder):
    name = "rapidraw"
    label = "RapidRAW"

    def __init__(self, root: Path, data_dir: Path, dry_run: bool = True, settings: Path | None = None):
        super().__init__(root, data_dir, dry_run)
        self.settings = settings or settings_file()

    def _sidecars(self, p: Path) -> list[Path]:
        return [std_sidecar(p)]

    def _check_source(self) -> bool:
        ok = super()._check_source()
        if self.settings is None:   # jamais lancé ici, ou installé ailleurs : son réglage par défaut est actif
            self.warnings.append("réglages de RapidRAW introuvables : vérifie que la synchronisation XMP est active")
            return ok
        try:
            sync = json.loads(self.settings.read_text(encoding="utf-8")).get("enableXmpSync")
        except (OSError, ValueError, AttributeError):
            sync = None
        if sync is not True:
            self.warnings.append("RapidRAW : active la synchronisation XMP dans ses réglages pour qu'il reprenne les "
                                 "mots-clés écrits ici")
        return ok
