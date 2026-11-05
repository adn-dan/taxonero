"""Source « Lightroom Classic » : les photos d'un catalogue .lrcat (base SQLite, lue sans jamais l'écrire).

Chemin d'une photo : AgLibraryRootFolder.absolutePath + AgLibraryFolder.pathFromRoot + AgLibraryFile.baseName
.extension (Adobe_images.rootFile ; les copies virtuelles partagent le même fichier, compté une fois). Lightroom
verrouille le catalogue tant qu'il est ouvert : il n'est relu qu'une fois Lightroom fermé.

Écriture : photo.xmp à côté des RAW, le sidecar que Lightroom lit et écrit lui-même. Pour un JPEG, TIFF, PSD, HEIC
ou DNG, Lightroom ne lit les métadonnées que dans le fichier lui-même, qui n'est jamais modifié ici : le sidecar
écrit (photo.jpg.xmp) sert aux autres logiciels, pas à Lightroom. Dans Lightroom, Metadata > « Read Metadata from
Files » reprend les mots-clés ; avec « Automatically write changes into XMP » (Catalog Settings), les sidecars
contiennent aussi ses réglages et rien n'est perdu à la relecture.
"""

import sqlite3
from pathlib import Path

from ..store import RAW_EXT
from .files import FileSource, ext_sidecar, is_image, open_readonly, std_sidecar

# Fichiers dont Lightroom lit les métadonnées dans un sidecar .xmp (RAW propriétaires ; le DNG les porte en lui).
SIDECAR_EXT = RAW_EXT - {"dng"}
FILES = ("SELECT DISTINCT r.absolutePath || f.pathFromRoot || i.baseName || '.' || i.extension AS path "
         "FROM Adobe_images a JOIN AgLibraryFile i ON i.id_local = a.rootFile "
         "JOIN AgLibraryFolder f ON f.id_local = i.folder JOIN AgLibraryRootFolder r ON r.id_local = f.rootFolder")


def default_catalog() -> Path | None:
    """Catalogue à l'emplacement par défaut (Images/Lightroom), le plus récemment modifié."""
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
                raise CatalogLocked("catalogue Lightroom verrouillé : ferme Lightroom Classic pour qu'il soit relu") from e
            raise

    def _files(self):
        for r in self._query(FILES + " ORDER BY path"):
            if is_image(r["path"].rpartition("/")[2]):
                yield r["path"], Path(r["path"])

    def _resolve(self, key: str) -> Path:
        return Path(key)

    def _sidecars(self, p: Path) -> list[Path]:
        # photo.xmp d'un RAW : celui de Lightroom ; le JPEG d'une paire RAW+JPEG a le sien (photo.jpg.xmp).
        return [std_sidecar(p) if p.suffix.lower().lstrip(".") in SIDECAR_EXT else ext_sidecar(p)]

    def _check_source(self) -> bool:
        if not self.root.is_file():
            self.errors.append(f"catalogue Lightroom introuvable : {self.root}")
            return False
        if self.root.with_name(self.root.name + ".lock").exists():
            self.warnings.append("Lightroom Classic est ouvert : le catalogue sera relu à sa fermeture")
            return True
        try:
            names = [r["path"].rpartition("/")[2] for r in self._query(FILES)]
        except CatalogLocked as e:
            self.warnings.append(str(e))
            return True
        except sqlite3.Error as e:
            self.errors.append(f"catalogue Lightroom illisible ({self.root.name}) : {e}")
            return False
        inside = sum(1 for n in names if is_image(n) and n.rpartition(".")[2].lower() not in SIDECAR_EXT)
        if inside:
            self.warnings.append(f"{inside} photo(s) JPEG, TIFF, HEIC ou DNG : Lightroom lit leurs mots-clés dans le "
                                 "fichier lui-même, jamais modifié ici ; seuls les RAW les reçoivent dans Lightroom")
        return True
