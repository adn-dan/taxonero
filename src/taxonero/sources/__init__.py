"""Connecteurs de sources de photos.

Une source fournit les photos et reçoit les résultats. Interface commune (celle d'ImmichClient, que les autres
reproduisent) :
- état : check() -> bool, status() -> dict, version, writable, errors, warnings, dry_run, close(), link(id) (lien
  vers la photo dans la source, ou None), write_gap (écart minimal entre deux écritures sur une même photo) ;
- lecture : iter_assets(updated_after, size, with_exif, kind, with_people) et get_asset(id, missing_ok), au format
  des assets d'Immich (id, checksum, originalFileName, type, people, updatedAt, tags, exifInfo) ;
- images : thumbnail(id, "thumbnail" | "preview") et original(id), en octets ;
- écriture : list_tags, upsert_tags, bulk_tag, untag_assets, count_assets_with_tag, delete_tag, set_description.

Sources « fichiers » (files.py) : un dossier (kind « folder », PHOTOS_DIR) ou la bibliothèque d'un logiciel
(kind « catalog », CATALOG_PATH), résultats dans des fichiers XMP à côté des photos.
"""

import importlib
import sys
from pathlib import Path

from ..config import Settings

# Connecteurs connus : classe, nature du chemin à indiquer, éditions où ils ont un sens et systèmes où ils tournent
# (None : tous). Ceux d'un logiciel installé sur l'ordinateur (réglages lus chez lui) sont réservés au bureau.
SOURCES = {
    "immich": {"cls": "..immich.client:ImmichClient", "kind": "server", "editions": ("server", "desktop"), "platforms": None},
    "local": {"cls": ".local:LocalFolder", "kind": "folder", "editions": ("server", "desktop"), "platforms": None},
    "darktable": {"cls": ".darktable:Darktable", "kind": "catalog", "editions": ("desktop",), "platforms": None,
                  "default": ".darktable:default_library", "suffix": ".db"},
    "lightroom": {"cls": ".lightroom:Lightroom", "kind": "catalog", "editions": ("desktop",), "platforms": ("win32", "darwin"),
                  "default": ".lightroom:default_catalog", "suffix": ".lrcat"},
    "rawtherapee": {"cls": ".rawtherapee:RawTherapee", "kind": "folder", "editions": ("desktop",), "platforms": None},
    "rapidraw": {"cls": ".rapidraw:RapidRaw", "kind": "folder", "editions": ("desktop",), "platforms": None},
}


def _load(ref: str):
    module, _, attr = ref.partition(":")
    return getattr(importlib.import_module(module, __name__), attr)


def kind(source: str) -> str:
    return SOURCES[source]["kind"]


def default_path(source: str) -> Path | None:
    """Bibliothèque du logiciel à son emplacement habituel, s'il est installé ici."""
    ref = SOURCES[source].get("default")
    return _load(ref)() if ref else None


def check_path(source: str, path: Path) -> str | None:
    """Raison pour laquelle ce chemin ne convient pas à la source, ou None."""
    spec = SOURCES[source]
    if not path.is_absolute():
        return f"chemin complet attendu : {path}"
    if spec["kind"] == "folder" and not path.is_dir():
        return f"dossier introuvable : {path}"
    if spec["kind"] == "catalog" and not (path.is_file() and path.suffix.lower() == spec["suffix"]):
        return f"fichier {spec['suffix']} introuvable : {path}"
    return None


def available(edition: str) -> list[dict]:
    """Connecteurs proposés dans le dashboard, avec la raison s'ils ne sont pas utilisables ici."""
    out = []
    for name, spec in SOURCES.items():
        reason = None
        if edition not in spec["editions"]:
            reason = "edition"
        elif spec["platforms"] and not any(sys.platform.startswith(p) for p in spec["platforms"]):
            reason = "platform"
        out.append({"id": name, "kind": spec["kind"], "suffix": spec.get("suffix"), "available": reason is None, "reason": reason})
    return out


def source_path(s: Settings) -> Path | None:
    """Dossier ou bibliothèque de la source choisie (None pour Immich)."""
    return {"folder": s.photos_dir, "catalog": s.catalog_path}.get(kind(s.source))


def make_source(s: Settings):
    cls = _load(SOURCES[s.source]["cls"])
    if s.source == "immich":
        return cls(s.immich_url, s.immich_api_key, dry_run=s.dry_run)
    return cls(source_path(s) or Path(), s.data_dir, dry_run=s.dry_run)
