"""Photo source connectors.

A source provides the photos and receives the results. Common interface (that of ImmichClient, which the others
reproduce):
- state: check() -> bool, status() -> dict, version, writable, errors, warnings, dry_run, close(), link(id) (link
  to the photo in the source, or None), write_gap (minimum gap between two writes to the same photo);
- reading: iter_assets(updated_after, size, with_exif, kind, with_people) and get_asset(id, missing_ok), in the format
  of Immich assets (id, checksum, originalFileName, type, people, updatedAt, tags, exifInfo);
- images: thumbnail(id, "thumbnail" | "preview") and original(id), as bytes;
- writing: list_tags, upsert_tags, bulk_tag, untag_assets, count_assets_with_tag, delete_tag, set_description.

"File" sources (files.py): a folder (kind "folder", PHOTOS_DIR) or the library of an application
(kind "catalog", CATALOG_PATH), results in XMP files next to the photos (Apple Photos: in the library, through Photos).

Several sources: the main one (settings above, id '') and the ones added in the dashboard (extra_id, make_extra),
each with its photos (asset.source) and its writes (written.source).
"""

import hashlib

import importlib
import sys
from pathlib import Path

from ..config import Settings

# Known connectors: class, kind of path to give, editions where they make sense and systems where they run
# (None: all). Those of an application installed on the computer (settings read from it) are desktop only.
SOURCES = {
    "immich": {"cls": "..immich.client:ImmichClient", "kind": "server", "editions": ("server", "desktop"), "platforms": None},
    "local": {"cls": ".local:LocalFolder", "kind": "folder", "editions": ("server", "desktop"), "platforms": None},
    "darktable": {"cls": ".darktable:Darktable", "kind": "catalog", "editions": ("desktop",), "platforms": None,
                  "default": ".darktable:default_library", "suffix": ".db"},
    "lightroom": {"cls": ".lightroom:Lightroom", "kind": "catalog", "editions": ("desktop",), "platforms": ("win32", "darwin"),
                  "default": ".lightroom:default_catalog", "suffix": ".lrcat"},
    "rawtherapee": {"cls": ".rawtherapee:RawTherapee", "kind": "folder", "editions": ("desktop",), "platforms": None},
    "rapidraw": {"cls": ".rapidraw:RapidRaw", "kind": "folder", "editions": ("desktop",), "platforms": None},
    # A .photoslibrary is a folder that macOS shows as a file: chosen like a library file.
    "applephotos": {"cls": ".applephotos:ApplePhotos", "kind": "catalog", "editions": ("desktop",), "platforms": ("darwin",),
                    "default": ".applephotos:default_library", "suffix": ".photoslibrary", "bundle": True},
}


def _load(ref: str):
    module, _, attr = ref.partition(":")
    return getattr(importlib.import_module(module, __name__), attr)


def kind(source: str) -> str:
    return SOURCES[source]["kind"]


def default_path(source: str) -> Path | None:
    """Library of the application at its usual location, if it is installed here."""
    ref = SOURCES[source].get("default")
    return _load(ref)() if ref else None


def check_path(source: str, path: Path) -> str | None:
    """Reason why this path does not suit the source, or None."""
    spec = SOURCES[source]
    if not path.is_absolute():
        return f"chemin complet attendu : {path}"
    if spec["kind"] == "folder" and not path.is_dir():
        return f"folder not found: {path}"
    if spec["kind"] == "catalog" and not ((path.is_dir() if spec.get("bundle") else path.is_file())
                                          and path.suffix.lower() == spec["suffix"]):
        return f"{spec['suffix']} file not found: {path}"
    return None


def available(edition: str) -> list[dict]:
    """Connectors offered in the dashboard, with the reason if they cannot be used here."""
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
    """Folder or library of the chosen source (None for Immich)."""
    return {"folder": s.photos_dir, "catalog": s.catalog_path}.get(kind(s.source))


def make_source(s: Settings):
    cls = _load(SOURCES[s.source]["cls"])
    if s.source == "immich":
        return cls(s.immich_url, s.immich_api_key, dry_run=s.dry_run)
    return cls(source_path(s) or Path(), s.data_dir, dry_run=s.dry_run)


def extra_id(spec: dict) -> str:
    """Id of an added source, from what it is (type and folder, or server and key): the same source removed then
    added again gets its photos and reviews back."""
    what = spec.get("url", "").rstrip("/") + "|" + spec.get("key", "") if spec["type"] == "immich" else spec.get("path", "")
    return "s" + hashlib.sha256(f"{spec['type']}|{what}".encode()).hexdigest()[:8]


def make_extra(spec: dict, s: Settings):
    """Client of an added source: {"id", "type", "path"} or {"id", "type": "immich", "url", "key"}."""
    cls = _load(SOURCES[spec["type"]]["cls"])
    if spec["type"] == "immich":
        return cls(spec["url"], spec["key"], dry_run=s.dry_run)
    return cls(Path(spec["path"]), s.data_dir, dry_run=s.dry_run, sid=spec["id"])
