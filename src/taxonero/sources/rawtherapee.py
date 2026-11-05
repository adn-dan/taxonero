"""Source « RawTherapee » : un dossier de photos ouvert dans RawTherapee.

RawTherapee n'a pas de catalogue : ses réglages vont dans photo.nef.pp3, qu'il ne faut pas créer à sa place (un
.pp3 présent remplace le profil par défaut de l'image). Depuis la version 5.12, il lit aussi un sidecar XMP et en
reprend les mots-clés et la description à l'export (Exiv2::copyXmpToIptc : Xmp.dc.subject -> IPTC Keywords), si
Préférences > Métadonnées > « XMP sidecar synchronization » n'est pas sur « none » (défaut). Nom du sidecar selon
le même onglet : photo.xmp (std, défaut) ou photo.nef.xmp (ext). Les deux réglages sont lus dans son fichier
options ([Metadata] XMPSynchronization, XMPSidecarStyle).
"""

import os
import sys
from pathlib import Path

from .files import ext_sidecar, first_existing, std_sidecar
from .local import LocalFolder


def options_file() -> Path | None:
    """Fichier options de RawTherapee (paquet, Flatpak, Windows, macOS)."""
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
    """Section [Metadata] du fichier options (format GKeyFile : [groupe], puis clé=valeur)."""
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
            self.warnings.append("réglages de RawTherapee introuvables : sidecars nommés photo.xmp (son réglage par défaut)")
        if meta.get("XMPSynchronization", "none") == "none":
            self.warnings.append("RawTherapee : règle Préférences > Métadonnées > « XMP sidecar synchronization » sur "
                                 "« read » pour qu'il reprenne les mots-clés écrits ici")
        return ok
