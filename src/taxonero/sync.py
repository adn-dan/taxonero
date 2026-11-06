"""Projection of the confirmed detections to Immich (hierarchical tags + description block).

Rules:
- idempotent: running sync() again changes nothing if nothing changed;
- only what the tool itself set is removed (`written` table), never a tag of the user;
- in the description, only the block between BLOCK_START and BLOCK_END belongs to the tool.
"""

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from . import store
from .immich.client import ImmichClient, is_missing, is_transient

log = logging.getLogger(__name__)

BLOCK_START = "⟦nature⟧"
BLOCK_END = "⟦/nature⟧"
_BLOCK_RE = re.compile(r"\n*" + re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END) + r"\n*", re.S)

GROUP_ICONS = {"Aves": "🐦", "Mammalia": "🦊", "Insecta": "🦋", "Arachnida": "🕷️", "Reptilia": "🦎",
               "Amphibia": "🐸", "Actinopterygii": "🐟", "Plantae": "🌿", "Fungi": "🍄"}
TAG_RANKS = ("class", "order", "family", "genus")


@dataclass
class SyncStats:
    assets: int = 0
    tags_added: int = 0
    tags_removed: int = 0
    descriptions: int = 0
    skipped: int = 0
    repaired: int = 0
    tags_deleted: int = 0   # tool tags no longer needed, deleted from Immich
    failed: int = 0   # photos whose write was refused (retried at the next cycle)


def display_name(taxon: dict, lang: str = "fr") -> str:
    sci = taxon["scientific_name"]
    common = taxon.get("vernacular", {}).get(lang)
    suffix = "" if taxon["rank"] == "species" else f" ({taxon['rank']})"
    return f"{common} ({sci}){suffix}" if common else f"{sci}{suffix}"


def tag_value(taxon: dict, root: str, lang: str = "fr") -> str:
    """E.g. Nature/Aves/Passeriformes/Paridae/Great tit (Parus major)."""
    lineage = taxon.get("lineage", {})
    parts = [root]
    # Keep only the ranks strictly above the rank of the taxon.
    for r in TAG_RANKS:
        if r == taxon["rank"]:
            break
        if lineage.get(r):
            parts.append(lineage[r])
    parts.append(display_name(taxon, lang).replace("/", "-"))
    return "/".join(parts)


def description_block(taxa: list[dict], lang: str = "fr") -> str:
    lines = []
    for t in taxa:
        icon = next((i for k, i in GROUP_ICONS.items() if k in t.get("lineage", {}).values()), "•")
        url = t.get("wikipedia", {}).get(lang) or next(iter(t.get("wikipedia", {}).values()), "")
        lines.append(f"{icon} {display_name(t, lang)}" + (f" · {url}" if url else ""))
    return f"{BLOCK_START}\n" + "\n".join(lines) + f"\n{BLOCK_END}"


def merge_description(current: str | None, block: str | None) -> str:
    """Replace our block in the description without touching the user's text."""
    text = current or ""
    # Block in the middle of the text: restore the paragraph break it took up; at an edge: nothing.
    user_text = _BLOCK_RE.sub(lambda m: "" if m.start() == 0 or m.end() == len(text) else "\n\n", text).strip()
    if not block:
        return user_text
    return f"{user_text}\n\n{block}" if user_text else block


def desired_taxa(db: sqlite3.Connection, asset_id: str) -> list[dict]:
    """Confirmed taxa of the photo; for a RAW linked to its JPG (asset.twin_of), those of the JPG too."""
    rows = db.execute(
        "SELECT taxon_key FROM detection WHERE status = 'confirmed' AND taxon_key IS NOT NULL AND asset_id IN "
        "(SELECT ? UNION SELECT twin_of FROM asset WHERE asset_id = ? AND twin_of IS NOT NULL) "
        "GROUP BY taxon_key ORDER BY max(score) DESC", (asset_id, asset_id)).fetchall()
    return [t for r in rows if (t := store.load_taxon(db, r["taxon_key"]))]


# Immich (v3.3.5) sometimes loses a change when two writes to the same asset
# arrive back to back (race between the SidecarWrite jobs and the unlocking of properties).
# Measured: no loss from a 0.5 s gap; a margin is kept.
WRITE_GAP = 2.0
# Do not touch an asset changed by anyone other than the tool within this delay.
SETTLE_SECONDS = 600
# Removal (uninstall): rounds of re-reading and repair, and consecutive failures before giving up.
UNINSTALL_ROUNDS = 3
MAX_CONSECUTIVE_FAILURES = 20


def _epoch(iso: str | None) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else 0.0


class Syncer:
    def __init__(self, db: sqlite3.Connection, client: ImmichClient, tag_root: str = "Nature", lang: str = "fr",
                 write_description: bool = True, write_gap: float = WRITE_GAP, settle_seconds: float = SETTLE_SECONDS,
                 verify_rounds: int = 2, checkpoint=None, progress=None):
        self.db, self.client = db, client
        # Called between two photos (nothing half written): cycle stop requested by the dashboard (worker._checkpoint).
        # A photo not synchronised yet is at the next cycle (asset_sync not updated).
        self.checkpoint = checkpoint or (lambda: None)
        # progress(done, total, phases): first pass of sync_all, in units (each photo is read, then gets its tags,
        # then its description: three units).
        self.progress = progress or (lambda done, total, phases: None)
        self.tag_root, self.lang, self.write_description = tag_root, lang, write_description
        self.write_gap, self.settle_seconds, self.verify_rounds = write_gap, settle_seconds, verify_rounds
        self._tag_ids: dict[str, str] | None = None
        self._last_write: dict[str, float] = {}
        self.missing: list[str] = []   # photos not found in Immich (deleted) during this pass
        self.remaining = 0             # uninstall: photos not cleaned up yet

    # -- utilitaires ---------------------------------------------------------

    def _tags(self) -> dict[str, str]:
        if self._tag_ids is None:
            self._tag_ids = {t["value"]: t["id"] for t in self.client.list_tags()}
        return self._tag_ids

    def _ensure_tag(self, value: str) -> str | None:
        tags = self._tags()
        if value in tags:
            return tags[value]
        self.client.upsert_tags([value])
        if self.client.dry_run:
            return None
        # PUT /tags also creates the missing parents without returning them: read the list again and record
        # as ours the tag and those of its parents that just appeared (not a tag the
        # user might have created elsewhere at the same moment).
        lineage = {value.rsplit("/", i)[0] for i in range(value.count("/") + 1)}
        for t in self.client.list_tags():
            if t["value"] not in tags and t["value"] in lineage:
                tags[t["value"]] = t["id"]
                self._record("tag", t["id"], "", t["value"])
        return tags.get(value)

    def _record(self, kind: str, immich_id: str, asset_id: str, value: str) -> None:
        """Record a tag (or a tag set on a photo). A tag deleted by hand in Immich then created again by the tool
        changes id: the old record of the same value is replaced, otherwise the removal would target a vanished id."""
        self.db.execute("DELETE FROM written WHERE kind = ? AND asset_id = ? AND value = ? AND immich_id != ?",
                        (kind, asset_id, value, immich_id))
        store.record_written(self.db, kind, immich_id, asset_id, value)
        # Committed right away: no write transaction left open across the calls to Immich that follow, which
        # would block the other threads writing to the database (analysis alongside, see worker._TagWriter).
        self.db.commit()

    def _ours(self, existing: dict[str, str]) -> dict[str, str]:
        """{value: id} of the tags created by the tool, according to Immich (existing: {value: id}). Records of a tag that
        no longer exists in Immich (deleted by hand) are erased; they no longer match anything."""
        ours: dict[str, str] = {}
        for r in self.db.execute("SELECT immich_id, value FROM written WHERE kind = 'tag'").fetchall():
            if existing.get(r["value"]) == r["immich_id"]:
                ours[r["value"]] = r["immich_id"]
            elif not self.client.dry_run:
                self.db.execute("DELETE FROM written WHERE kind = 'tag' AND immich_id = ?", (r["immich_id"],))
        self.db.commit()   # before the calls to Immich that follow (see _record)
        return ours

    def _write(self, asset_id: str, fn, *args) -> None:
        """Run a write while respecting the minimum gap between two writes to the same asset."""
        if not self.client.dry_run and (last := self._last_write.get(asset_id)):
            time.sleep(max(0.0, self.write_gap - (time.monotonic() - last)))
        fn(*args)
        self._last_write[asset_id] = time.monotonic()

    def _recently_edited(self, asset: dict) -> bool:
        """True if the asset was recently changed by something other than the tool."""
        updated = _epoch(asset.get("updatedAt"))
        ours = self.db.execute("SELECT max(written_at) AS t FROM written WHERE asset_id = ?", (asset["id"],)).fetchone()["t"]
        if ours and updated <= ours + 30:
            return False
        return time.time() - updated < self.settle_seconds

    # -- plan ----------------------------------------------------------------

    def _wanted(self, taxa: list[dict]) -> str:
        """Fingerprint of what the tool wants to see in Immich for a photo (tags + description block)."""
        tags = sorted(tag_value(t, self.tag_root, self.lang) for t in taxa)
        block = description_block(taxa, self.lang) if taxa and self.write_description else None
        return json.dumps([tags, block, self.write_description], ensure_ascii=False)

    def _plan(self, asset: dict) -> tuple[list[str], dict[str, str], list[dict]]:
        """(tag values to add, {value: id} to remove, wanted taxa), from the actual state."""
        aid = asset["id"]
        taxa = desired_taxa(self.db, aid)
        want = {tag_value(t, self.tag_root, self.lang) for t in taxa}
        actual = {t["value"]: t["id"] for t in asset.get("tags", [])}
        ours = {r["value"]: r["immich_id"] for r in self.db.execute(
            "SELECT immich_id, value FROM written WHERE kind = 'tag_asset' AND asset_id = ?", (aid,))}
        add = sorted(want - actual.keys())
        remove = {v: actual[v] for v in ours if v not in want and v in actual}
        return add, remove, taxa

    # -- synchronisation -----------------------------------------------------

    def _apply_tags(self, asset: dict, stats: SyncStats) -> list[dict]:
        aid = asset["id"]
        add, remove, taxa = self._plan(asset)
        want = {tag_value(t, self.tag_root, self.lang) for t in taxa}
        # Mark as ours the wanted tags already present (set during a previous run).
        if not self.client.dry_run:
            for v, tid in ((t["value"], t["id"]) for t in asset.get("tags", [])):
                if v in want:
                    self._record("tag_asset", tid, aid, v)
        if add:
            ids = [self._ensure_tag(v) for v in add]
            self._write(aid, self.client.bulk_tag, [i or "dry-run" for i in ids], [aid])
            if not self.client.dry_run:
                for v, i in zip(add, ids):
                    self._record("tag_asset", i, aid, v)
            stats.tags_added += len(add)
        for v, tid in remove.items():
            self._write(aid, self.client.untag_assets, tid, [aid])
            if not self.client.dry_run:
                self.db.execute("DELETE FROM written WHERE kind = 'tag_asset' AND immich_id = ? AND asset_id = ?", (tid, aid))
                self.db.commit()   # before the next call to Immich (see _record)
            stats.tags_removed += 1
        self.db.commit()
        return taxa

    def _apply_description(self, asset: dict, taxa: list[dict], stats: SyncStats) -> None:
        aid = asset["id"]
        current = (asset.get("exifInfo") or {}).get("description") or ""
        new = merge_description(current, description_block(taxa, self.lang) if taxa else None)
        if new.strip() != current.strip():
            self._write(aid, self.client.set_description, aid, new)
            if not self.client.dry_run:
                store.record_written(self.db, "description", aid, aid, json.dumps(bool(taxa)))
                self.db.commit()
            stats.descriptions += 1

    def _candidates(self, full: bool = True) -> list[str]:
        """Photos to synchronise: those that have (or had) something from the tool, except missing photos.
        full=False: only those whose wanted state changed since the last successful write."""
        ids = [r["asset_id"] for r in self.db.execute(
            "SELECT c.asset_id FROM (SELECT asset_id FROM detection WHERE status = 'confirmed' "
            "  UNION SELECT r.asset_id FROM asset r JOIN detection d ON d.asset_id = r.twin_of WHERE d.status = 'confirmed' "
            "  UNION SELECT asset_id FROM written WHERE kind IN ('tag_asset', 'description')) c "
            "LEFT JOIN asset a ON a.asset_id = c.asset_id WHERE a.missing_at IS NULL")]
        if full:
            return ids
        done = {r["asset_id"]: r["wanted"] for r in self.db.execute("SELECT asset_id, wanted FROM asset_sync")}
        return [a for a in ids if done.get(a) != self._wanted(desired_taxa(self.db, a))]

    def _fetch(self, aid: str, stats: SyncStats) -> dict | None:
        self.checkpoint()
        a = self.client.get_asset(aid, missing_ok=True)
        if a is None:
            log.info("asset %s not found in Immich (deleted), skipped", aid)
            self.missing.append(aid)
            return None
        if self._recently_edited(a):
            log.info("asset %s recently changed, postponed", aid)
            stats.skipped += 1
            return None
        return a

    def _write_failed(self, aid: str, e: Exception, stats: SyncStats, failed: set[str]) -> None:
        """Write refused on a photo (read-only file, 400 from Immich): the others go on, and this one,
        never marked as synchronised, is retried at the next cycle. General outage (network, 5xx, key refused): raised."""
        if is_transient(e) or (isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 401):
            raise e
        if is_missing(e):
            log.info("asset %s not found in Immich (deleted), skipped", aid)
            self.missing.append(aid)
        elif aid not in failed:
            log.warning("write refused on photo %s: %s", aid, e)
            stats.failed += 1
        failed.add(aid)
        if not self.client.dry_run:   # marked as synchronised in a previous round: to be redone at the next cycle
            self.db.execute("DELETE FROM asset_sync WHERE asset_id = ?", (aid,))
            self.db.commit()

    def _mark_synced(self, aid: str) -> None:
        """Photo written: recorded right away, so that an interrupted pass (dashboard setting, restart) does not
        start again from the beginning with all the photos of a long first synchronisation."""
        if not self.client.dry_run:
            self.db.execute("INSERT OR REPLACE INTO asset_sync VALUES (?, ?, ?)",
                            (aid, self._wanted(desired_taxa(self.db, aid)), time.time()))
            self.db.commit()

    def sync_all(self, asset_ids: list[str] | None = None, full: bool = True) -> SyncStats:
        """Tags of all assets first, descriptions next (spaces out the writes to the same asset),
        then a check against the actual state and repair if Immich lost a write.
        full=False: only the photos whose wanted state changed (the current cycle); full=True reads
        everything again (once a day: sets back a tag removed by hand, catches up a lost write)."""
        stats = SyncStats()
        pending = asset_ids if asset_ids is not None else self._candidates(full)
        failed: set[str] = set()
        units, total = 0, 3 * len(pending)

        def step():
            nonlocal units
            if round_ == 0:
                units += 1
                self.progress(units, total, 3)

        self.progress(0, total, 3)
        for round_ in range(1 + self.verify_rounds):
            assets = []
            for aid in pending:
                step()
                try:
                    if a := self._fetch(aid, stats):
                        assets.append(a)
                except (httpx.HTTPError, RuntimeError, OSError) as e:
                    self._write_failed(aid, e, stats, failed)
            before = (stats.tags_added, stats.tags_removed, stats.descriptions)
            taxa = {}
            if round_ == 0:   # deleted or unreadable photos: their units are counted as done
                units += 2 * (len(pending) - len(assets))
            for a in assets:
                self.checkpoint()
                step()
                try:
                    taxa[a["id"]] = self._apply_tags(a, stats)
                except (httpx.HTTPError, RuntimeError, OSError) as e:
                    self._write_failed(a["id"], e, stats, failed)
            for a in assets:
                step()
                if a["id"] in failed:
                    continue
                self.checkpoint()
                try:
                    if self.write_description:
                        # Read the asset again: the current description must be the one after the tag writes.
                        fresh = self.client.get_asset(a["id"]) if a["id"] in self._last_write else a
                        self._apply_description(fresh, taxa[a["id"]], stats)
                    self._mark_synced(a["id"])
                except (httpx.HTTPError, RuntimeError, OSError) as e:
                    self._write_failed(a["id"], e, stats, failed)
            if round_ == 0:
                stats.assets = len(assets)
            elif (stats.tags_added, stats.tags_removed, stats.descriptions) != before:
                stats.repaired += 1
                log.warning("mismatch found after writing (pass %d), repaired", round_)
            touched = [a["id"] for a in assets if a["id"] in self._last_write and a["id"] not in failed]
            if not touched or self.client.dry_run:
                break
            pending = touched
            time.sleep(self.write_gap * 2)
        if not self.client.dry_run and (stats.tags_removed or full):
            stats.tags_deleted = self.cleanup_tags()
        return stats

    def cleanup_tags(self) -> int:
        """Delete the tags created by the tool that are no longer used (species corrected on its only photo,
        common name changed): no photo of the tool carries them, no Immich photo either, and
        no sub-tag remains below. A tag the user reused is therefore kept."""
        self._tag_ids = None
        existing = self._tags()
        ours = self._ours(existing)
        used: set[str] = set()
        for r in self.db.execute("SELECT DISTINCT value FROM written WHERE kind = 'tag_asset'"):
            v = r["value"] or ""
            used.update(v.rsplit("/", i)[0] for i in range(v.count("/") + 1))
        unused = [v for v in ours if v not in used]
        if not unused:
            self.db.commit()
            return 0
        n = 0
        for value in sorted(unused, key=len, reverse=True):   # children before parents
            if any(v.startswith(value + "/") for v in existing) or self.client.count_assets_with_tag(ours[value]):
                continue
            try:
                self.client.delete_tag(ours[value])
            except Exception:
                log.warning("tag %s not deleted", value, exc_info=True)
                continue
            existing.pop(value, None)
            self.db.execute("DELETE FROM written WHERE kind = 'tag' AND immich_id = ?", (ours[value],))
            self.db.commit()   # before the next call to Immich (see _record)
            n += 1
        self.db.commit()
        if n:
            log.info("%d unused tag(s) deleted", n)
        return n

    def same_server(self) -> bool:
        """False if no recorded tag exists on this server: another Immich or another account. Immich would answer "not
        found" everywhere, which would look like deleted photos and erase the records without removing anything."""
        ids = {r["immich_id"] for r in self.db.execute("SELECT immich_id FROM written WHERE kind IN ('tag', 'tag_asset')")}
        self._tag_ids = None
        return not ids or bool(ids & set(self._tags().values()))

    def _clean_asset(self, aid: str, tag_ids: set[str], description: bool, stats: SyncStats) -> bool:
        """Remove our tags and our block from the photo as it is now. True if something was written:
        the photo is then read again at the next round, as Immich can lose a write. False: nothing left to remove."""
        a = self.client.get_asset(aid, missing_ok=True)
        if a is None:   # photo deleted: nothing left to clean up
            return False
        wrote = False
        present = {t["id"] for t in a.get("tags") or []}
        for tid in sorted(tag_ids & present):
            self._write(aid, self.client.untag_assets, tid, [aid])
            stats.tags_removed += 1
            wrote = True
        current = (a.get("exifInfo") or {}).get("description") or ""
        if description and _BLOCK_RE.search(current):   # without our block, the user's text is not rewritten
            self._write(aid, self.client.set_description, aid, merge_description(current, None))
            stats.descriptions += 1
            wrote = True
        return wrote

    def uninstall(self) -> SyncStats:
        """Remove from Immich what the tool set there, and only that. Writes are suspended first: otherwise
        the next cycle would set everything back. The record of a photo is erased only once it reads clean; what
        remains (self.remaining) is picked up by running uninstall again."""
        stats = SyncStats()
        dry = self.client.dry_run
        if not dry:
            store.set_state(self.db, store.WRITES_PAUSED, "1")
            self.db.commit()
        tags: dict[str, set[str]] = {}
        for r in self.db.execute("SELECT immich_id, asset_id FROM written WHERE kind = 'tag_asset'").fetchall():
            tags.setdefault(r["asset_id"], set()).add(r["immich_id"])
        described = {r["asset_id"] for r in self.db.execute("SELECT asset_id FROM written WHERE kind = 'description'").fetchall()}
        pending = sorted(set(tags) | described)
        failures = 0
        for _ in range(UNINSTALL_ROUNDS):
            again = []
            for aid in pending:
                self.checkpoint()
                try:
                    wrote = self._clean_asset(aid, tags.get(aid, set()), aid in described, stats)
                except (httpx.HTTPError, RuntimeError, OSError) as e:
                    log.warning("photo %s not cleaned up: %s", aid, e)
                    stats.failed += 1
                    failures += 1
                    if failures >= MAX_CONSECUTIVE_FAILURES:   # Immich down: no point insisting on every photo
                        raise RuntimeError(f"{failures} failures in a row, removal stopped: {e}") from e
                    continue
                failures = 0
                if wrote:
                    again.append(aid)
                elif not dry:
                    self.db.execute("DELETE FROM written WHERE kind IN ('tag_asset', 'description') AND asset_id = ?", (aid,))
                    self.db.execute("DELETE FROM asset_sync WHERE asset_id = ?", (aid,))   # to be written again if resumed
                    self.db.commit()
            pending = again
            if dry or not pending:
                break
            time.sleep(self.write_gap * 2)
        if not dry:
            self.remaining = self.db.execute("SELECT count(DISTINCT asset_id) FROM written WHERE kind IN ('tag_asset', 'description')").fetchone()[0]
            if self.remaining:
                log.warning("%d photo(s) not cleaned up yet: tags kept, run the removal again", self.remaining)
                return stats
        done: list[tuple[str, str, str]] = []   # removed tags: records erased at the end
        # Delete a tag created by the tool only if it serves nothing else: no asset still tagged
        # (the user may have reused it) and no sub-tag still present, ours or not (Immich deletes
        # sub-tags with their parent).
        self._tag_ids = None   # read again: the user may have created some in the meantime
        if self.client.dry_run:
            ours = {r["value"]: r["immich_id"] for r in self.db.execute("SELECT immich_id, value FROM written WHERE kind = 'tag'")}
            all_values = set(ours)
        else:   # records of tags deleted by hand in Immich erased by _ours
            existing = self._tags()
            ours, all_values = self._ours(existing), set(existing)
        for value in sorted(ours, key=len, reverse=True):   # children before parents
            if any(v.startswith(value + "/") for v in all_values) or (
                    not self.client.dry_run and self.client.count_assets_with_tag(ours[value])):
                log.info("tag kept (used elsewhere): %s", value)
                continue
            try:
                self.client.delete_tag(ours[value])
            except Exception as e:
                log.warning("tag %s not deleted: %s", value, e)
                continue
            all_values.discard(value)
            stats.tags_deleted += 1
            done.append(("tag", ours[value], ""))
        if not self.client.dry_run:
            self.db.executemany("DELETE FROM written WHERE kind = ? AND immich_id = ? AND asset_id = ?", done)
            self.db.execute("DELETE FROM asset_sync")
            self.db.commit()
        return stats
