"""Source « darktable » : les photos de la bibliothèque de darktable (library.db, lue sans jamais l'écrire).

darktable range chaque photo dans un sidecar photo.nef.xmp, et chaque duplicata dans photo_01.nef.xmp, photo_02…
(version 0 sans suffixe, voir dt_image_path_append_version_no_db) : les résultats sont écrits dans tous ceux de la
photo. darktable lit les mots-clés dans Xmp.lr.hierarchicalSubject (à défaut Xmp.dc.subject), séparateur « | ».

darktable garde sa propre base et réécrit ses sidecars à partir d'elle : pour qu'il reprenne les mots-clés écrits
ici, l'option « look for updated XMP files on startup » (Préférences > stockage) doit être active ; il propose
alors au démarrage de relire les XMP modifiés. Elle est vérifiée dans darktablerc (run_crawler_on_start), comme
l'écriture des sidecars (write_sidecar_files : jamais = mots-clés des XMP ignorés à l'import).
"""

import os
import sqlite3
import sys
from pathlib import Path

from .files import FileSource, ext_sidecar, first_existing, is_image, open_readonly


def default_library() -> Path | None:
    """library.db de l'installation courante (paquet, Flatpak, Windows)."""
    home = Path.home()
    if sys.platform == "win32":
        dirs = [Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "darktable"]
    else:
        dirs = [Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "darktable",
                home / ".var" / "app" / "org.darktable.Darktable" / "config" / "darktable"]
    return first_existing(d / "library.db" for d in dirs)


def version_name(p: Path, version: int) -> Path:
    """Fichier d'un duplicata : photo.nef, photo_01.nef…"""
    return p if version == 0 else p.with_name(f"{p.stem}_{version:02d}{p.suffix}")


def read_rc(path: Path) -> dict[str, str]:
    """darktablerc : une ligne clé=valeur par réglage."""
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
            self.errors.append(f"bibliothèque darktable introuvable : {self.root}")
            return False
        try:
            self._query("SELECT 1 FROM images JOIN film_rolls ON film_rolls.id = images.film_id LIMIT 1")
        except sqlite3.Error as e:
            self.errors.append(f"bibliothèque darktable illisible ({self.root.name}) : {e}")
            return False
        rc = read_rc(self.root.parent / "darktablerc")
        if rc.get("write_sidecar_files") == "never":
            self.warnings.append("darktable n'utilise pas les fichiers XMP (Préférences > stockage, « write sidecar file "
                                 "for each image » sur « never ») : les mots-clés écrits ici n'y apparaîtront pas")
        if rc.get("run_crawler_on_start") != "true":
            self.warnings.append("darktable : active « look for updated XMP files on startup » (Préférences > stockage) "
                                 "pour qu'il relise les mots-clés écrits ici")
        return True
