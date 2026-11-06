""""RawTherapee" source: a photo folder opened in RawTherapee.

RawTherapee has no catalog: its settings go in photo.nef.pp3, which must not be created in its place (a
.pp3 present replaces the default profile of the image). Since version 5.12, it also reads an XMP sidecar and
picks up its keywords and description on export (Exiv2::copyXmpToIptc: Xmp.dc.subject -> IPTC Keywords), if
Preferences > Metadata > "XMP sidecar synchronization" is not set to "none" (default). Sidecar name according to
the same tab: photo.xmp (std, default) or photo.nef.xmp (ext). Both settings are read in its options
file ([Metadata] XMPSynchronization, XMPSidecarStyle).
"""

import os
import sys
from pathlib import Path

from .files import ext_sidecar, first_existing, std_sidecar
from .local import LocalFolder


def options_file() -> Path | None:
    """RawTherapee options file (package, Flatpak, Windows, macOS)."""
    home = Path.home()
    if sys.platform == "win32":
        dirs = [Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "RawTherapee"]
    elif sys.platform == "darwin":
        dirs = [home / "Library" / "Application Support" / "RawTherapee" / "config"]
    else:
        dirs = [Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "RawTherapee",
                home / ".var" / "app" / "com.rawtherapee.RawTherapee" / "config" / "RawTherapee"]
    return first_existing(d / "options" for d in dirs)


def metadata_options(path: Path | None) -> dict[str, str]:
    """[Metadata] section of the options file (GKeyFile format: [group], then key=value)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path else []
    except OSError:
        return {}
    out, group = {}, None
    for line in lines:
        if line.startswith("["):
            group = line.strip()[1:-1]
        elif group == "Metadata" and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class RawTherapee(LocalFolder):
    name = "rawtherapee"
    label = "RawTherapee"

    def __init__(self, root: Path, data_dir: Path, dry_run: bool = True, options: Path | None = None):
        super().__init__(root, data_dir, dry_run)
        self.options = options or options_file()

    def _sidecars(self, p: Path) -> list[Path]:
        if metadata_options(self.options).get("XMPSidecarStyle") == "ext":
            return [ext_sidecar(p)]
        return [std_sidecar(p)]

    def _check_source(self) -> bool:
        ok = super()._check_source()
        meta = metadata_options(self.options)
        if not meta:
            self.warnings.append("RawTherapee settings not found: sidecars named photo.xmp (its default setting)")
        if meta.get("XMPSynchronization", "none") == "none":
            self.warnings.append("RawTherapee: set Preferences > Metadata > \"XMP sidecar synchronization\" to "
                                 "\"read\" so that it picks up the keywords written here")
        return ok
