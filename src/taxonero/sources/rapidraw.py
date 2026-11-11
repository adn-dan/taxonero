""""RapidRAW" source: a photo folder opened in RapidRAW.

RapidRAW stores its settings and keywords in photo.nef.rrdata (its own JSON, never written here). With
XMP sync enabled (Settings > "XMP sync", enableXmpSync key of settings.json; missing = disabled), it
reads the photo.xmp (or photo.XMP) sidecar while browsing the folder and adds to its keywords those of Xmp.dc.subject
(sync_metadata_from_xmp). It only adds: a keyword removed from the XMP stays in RapidRAW.
"""

import json
import os
import sys
from pathlib import Path

from .files import first_existing, std_sidecar
from .local import LocalFolder

APP_ID = "io.github.CyberTimon.RapidRAW"   # Tauri identifier: name of its settings folder


def settings_file() -> Path | None:
    """RapidRAW settings.json (application data folder according to Tauri; Flatpak)."""
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

    def __init__(self, root: Path, data_dir: Path, dry_run: bool = True, settings: Path | None = None, sid: str = ""):
        super().__init__(root, data_dir, dry_run, sid)
        self.settings = settings or settings_file()

    def _sidecars(self, p: Path) -> list[Path]:
        return [std_sidecar(p)]

    def _check_source(self) -> bool:
        ok = super()._check_source()
        if self.settings is None:   # never launched here, or installed elsewhere: its default setting is active
            self.warnings.append("RapidRAW settings not found: check that XMP sync is enabled")
            return ok
        try:
            sync = json.loads(self.settings.read_text(encoding="utf-8")).get("enableXmpSync")
        except (OSError, ValueError, AttributeError):
            sync = None
        if sync is not True:
            self.warnings.append("RapidRAW: enable XMP sync in its settings so that it picks up the "
                                 "keywords written here")
        return ok
