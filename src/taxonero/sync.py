"""Projection des détections confirmées vers Immich (tags hiérarchiques + bloc de description).

Règles :
- idempotent : relancer sync() ne change rien si rien n'a changé ;
- on ne retire que ce qu'on a soi-même posé (table `written`), jamais un tag de l'utilisateur ;
- dans la description, seul le bloc entre BLOCK_START et BLOCK_END nous appartient.
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
    tags_deleted: int = 0   # tags de l'outil devenus inutiles, supprimés d'Immich
    failed: int = 0   # photos dont l'écriture a été refusée (retentées au prochain cycle)


def display_name(taxon: dict, lang: str = "fr") -> str:
    sci = taxon["scientific_name"]
    common = taxon.get("vernacular", {}).get(lang)
    suffix = "" if taxon["rank"] == "species" else f" ({taxon['rank']})"
    return f"{common} ({sci}){suffix}" if common else f"{sci}{suffix}"


def tag_value(taxon: dict, root: str, lang: str = "fr") -> str:
    """Ex. Nature/Aves/Passeriformes/Paridae/Mésange charbonnière (Parus major)."""
    lineage = taxon.get("lineage", {})
    parts = [root]
    # Ne garde que les rangs strictement au-dessus du rang du taxon.
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
    """Remplace notre bloc dans la description sans toucher au texte de l'utilisateur."""
    text = current or ""
    # Bloc au milieu du texte : on rend la séparation de paragraphes qu'il occupait ; en bord : rien.
    user_text = _BLOCK_RE.sub(lambda m: "" if m.start() == 0 or m.end() == len(text) else "\n\n", text).strip()
    if not block:
        return user_text
    return f"{user_text}\n\n{block}" if user_text else block


def desired_taxa(db: sqlite3.Connection, asset_id: str) -> list[dict]:
    """Taxons confirmés de la photo ; pour un RAW relié à son JPG (asset.twin_of), aussi ceux du JPG."""
    rows = db.execute(
        "SELECT taxon_key FROM detection WHERE status = 'confirmed' AND taxon_key IS NOT NULL AND asset_id IN "
        "(SELECT ? UNION SELECT twin_of FROM asset WHERE asset_id = ? AND twin_of IS NOT NULL) "
        "GROUP BY taxon_key ORDER BY max(score) DESC", (asset_id, asset_id)).fetchall()
    return [t for r in rows if (t := store.load_taxon(db, r["taxon_key"]))]


# Immich (v3.3.5) perd parfois une modification quand deux écritures sur un même asset
# arrivent collées (course entre les jobs SidecarWrite et le déverrouillage des propriétés).
# Mesuré : aucune perte dès 0,5 s d'écart ; on garde une marge.
WRITE_GAP = 2.0
# Ne touche pas un asset modifié par quelqu'un d'autre que l'outil dans ce délai.
SETTLE_SECONDS = 600
# Retrait (uninstall) : tours de relecture et de réparation, et échecs de suite avant d'abandonner.
UNINSTALL_ROUNDS = 3
MAX_CONSECUTIVE_FAILURES = 20


def _epoch(iso: str | None) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else 0.0


class Syncer:
    def __init__(self, db: sqlite3.Connection, client: ImmichClient, tag_root: str = "Nature", lang: str = "fr",
                 write_description: bool = True, write_gap: float = WRITE_GAP, settle_seconds: float = SETTLE_SECONDS,
                 verify_rounds: int = 2, checkpoint=None):
        self.db, self.client = db, client
        # Appelé entre deux photos (rien d'écrit à moitié) : arrêt du cycle demandé par le dashboard (worker._checkpoint).
        # Une photo pas encore synchronisée l'est au cycle suivant (asset_sync non mis à jour).
        self.checkpoint = checkpoint or (lambda: None)
        self.tag_root, self.lang, self.write_description = tag_root, lang, write_description
        self.write_gap, self.settle_seconds, self.verify_rounds = write_gap, settle_seconds, verify_rounds
        self._tag_ids: dict[str, str] | None = None
        self._last_write: dict[str, float] = {}
        self.missing: list[str] = []   # photos introuvables dans Immich (supprimées) pendant ce passage
        self.remaining = 0             # uninstall : photos pas encore nettoyées

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
        # PUT /tags crée aussi les parents manquants sans les renvoyer : on relit la liste et on trace
        # comme nôtres le tag et ceux de ses parents qui viennent d'apparaître (pas un tag que
        # l'utilisateur aurait créé au même moment ailleurs).
        lineage = {value.rsplit("/", i)[0] for i in range(value.count("/") + 1)}
        for t in self.client.list_tags():
            if t["value"] not in tags and t["value"] in lineage:
                tags[t["value"]] = t["id"]
                self._record("tag", t["id"], "", t["value"])
        return tags.get(value)

    def _record(self, kind: str, immich_id: str, asset_id: str, value: str) -> None:
        """Trace un tag (ou un tag posé sur une photo). Un tag supprimé à la main dans Immich puis recréé par l'outil
        change d'id : l'ancienne trace de la même valeur est remplacée, sinon le retrait viserait un id disparu."""
        self.db.execute("DELETE FROM written WHERE kind = ? AND asset_id = ? AND value = ? AND immich_id != ?",
                        (kind, asset_id, value, immich_id))
        store.record_written(self.db, kind, immich_id, asset_id, value)

    def _ours(self, existing: dict[str, str]) -> dict[str, str]:
        """{valeur: id} des tags créés par l'outil, d'après Immich (existing : {valeur: id}). Les traces d'un tag qui
        n'existe plus dans Immich (supprimé à la main) sont effacées ; elles ne correspondent plus à rien."""
        ours: dict[str, str] = {}
        for r in self.db.execute("SELECT immich_id, value FROM written WHERE kind = 'tag'").fetchall():
            if existing.get(r["value"]) == r["immich_id"]:
                ours[r["value"]] = r["immich_id"]
            elif not self.client.dry_run:
                self.db.execute("DELETE FROM written WHERE kind = 'tag' AND immich_id = ?", (r["immich_id"],))
        return ours

    def _write(self, asset_id: str, fn, *args) -> None:
        """Exécute une écriture en respectant l'écart minimal entre deux écritures sur le même asset."""
        if not self.client.dry_run and (last := self._last_write.get(asset_id)):
            time.sleep(max(0.0, self.write_gap - (time.monotonic() - last)))
        fn(*args)
        self._last_write[asset_id] = time.monotonic()

    def _recently_edited(self, asset: dict) -> bool:
        """Vrai si l'asset a été modifié récemment par autre chose que l'outil."""
        updated = _epoch(asset.get("updatedAt"))
        ours = self.db.execute("SELECT max(written_at) AS t FROM written WHERE asset_id = ?", (asset["id"],)).fetchone()["t"]
        if ours and updated <= ours + 30:
            return False
        return time.time() - updated < self.settle_seconds

    # -- plan ----------------------------------------------------------------

    def _wanted(self, taxa: list[dict]) -> str:
        """Empreinte de ce que l'outil veut voir dans Immich pour une photo (tags + bloc de description)."""
        tags = sorted(tag_value(t, self.tag_root, self.lang) for t in taxa)
        block = description_block(taxa, self.lang) if taxa and self.write_description else None
        return json.dumps([tags, block, self.write_description], ensure_ascii=False)

    def _plan(self, asset: dict) -> tuple[list[str], dict[str, str], list[dict]]:
        """(valeurs de tags à ajouter, {valeur: id} à retirer, taxons voulus), d'après l'état réel."""
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
        # Désigne comme nôtres les tags voulus déjà présents (posés lors d'un run précédent).
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
        """Photos à synchroniser : celles qui ont (ou ont eu) quelque chose de l'outil, hors photos disparues.
        full=False : seulement celles dont l'état voulu a changé depuis la dernière écriture réussie."""
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
            log.info("asset %s introuvable dans Immich (supprimé), ignoré", aid)
            self.missing.append(aid)
            return None
        if self._recently_edited(a):
            log.info("asset %s modifié récemment, reporté", aid)
            stats.skipped += 1
            return None
        return a

    def _write_failed(self, aid: str, e: Exception, stats: SyncStats, failed: set[str]) -> None:
        """Écriture refusée sur une photo (fichier en lecture seule, 400 d'Immich) : les autres continuent, et celle-ci,
        jamais notée synchronisée, est retentée au prochain cycle. Panne générale (réseau, 5xx, clé refusée) : remonte."""
        if is_transient(e) or (isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 401):
            raise e
        if is_missing(e):
            log.info("asset %s introuvable dans Immich (supprimé), ignoré", aid)
            self.missing.append(aid)
        elif aid not in failed:
            log.warning("écriture refusée sur la photo %s : %s", aid, e)
            stats.failed += 1
        failed.add(aid)
        if not self.client.dry_run:   # notée synchronisée à un tour précédent : à reprendre au prochain cycle
            self.db.execute("DELETE FROM asset_sync WHERE asset_id = ?", (aid,))
            self.db.commit()

    def _mark_synced(self, aid: str) -> None:
        """Photo écrite : notée tout de suite, pour qu'un passage interrompu (réglage du dashboard, redémarrage) ne
        reprenne pas depuis le début toutes les photos d'une longue première synchronisation."""
        if not self.client.dry_run:
            self.db.execute("INSERT OR REPLACE INTO asset_sync VALUES (?, ?, ?)",
                            (aid, self._wanted(desired_taxa(self.db, aid)), time.time()))
            self.db.commit()

    def sync_all(self, asset_ids: list[str] | None = None, full: bool = True) -> SyncStats:
        """Tags de tous les assets d'abord, descriptions ensuite (espace les écritures sur un même asset),
        puis vérification sur l'état réel et réparation si Immich a perdu une écriture.
        full=False : seulement les photos dont l'état voulu a changé (le cycle courant) ; full=True relit
        tout (une fois par jour : repose un tag retiré à la main, rattrape une écriture perdue)."""
        stats = SyncStats()
        pending = asset_ids if asset_ids is not None else self._candidates(full)
        failed: set[str] = set()
        for round_ in range(1 + self.verify_rounds):
            assets = []
            for aid in pending:
                try:
                    if a := self._fetch(aid, stats):
                        assets.append(a)
                except (httpx.HTTPError, RuntimeError, OSError) as e:
                    self._write_failed(aid, e, stats, failed)
            before = (stats.tags_added, stats.tags_removed, stats.descriptions)
            taxa = {}
            for a in assets:
                self.checkpoint()
                try:
                    taxa[a["id"]] = self._apply_tags(a, stats)
                except (httpx.HTTPError, RuntimeError, OSError) as e:
                    self._write_failed(a["id"], e, stats, failed)
            for a in assets:
                if a["id"] in failed:
                    continue
                self.checkpoint()
                try:
                    if self.write_description:
                        # Relit l'asset : la description courante doit être celle d'après l'écriture des tags.
                        fresh = self.client.get_asset(a["id"]) if a["id"] in self._last_write else a
                        self._apply_description(fresh, taxa[a["id"]], stats)
                    self._mark_synced(a["id"])
                except (httpx.HTTPError, RuntimeError, OSError) as e:
                    self._write_failed(a["id"], e, stats, failed)
            if round_ == 0:
                stats.assets = len(assets)
            elif (stats.tags_added, stats.tags_removed, stats.descriptions) != before:
                stats.repaired += 1
                log.warning("écart détecté après écriture (passe %d), réparé", round_)
            touched = [a["id"] for a in assets if a["id"] in self._last_write and a["id"] not in failed]
            if not touched or self.client.dry_run:
                break
            pending = touched
            time.sleep(self.write_gap * 2)
        if not self.client.dry_run and (stats.tags_removed or full):
            stats.tags_deleted = self.cleanup_tags()
        return stats

    def cleanup_tags(self) -> int:
        """Supprime les tags créés par l'outil qui ne servent plus (espèce corrigée sur sa seule photo,
        nom commun changé) : aucune photo de l'outil ne les porte, aucune photo d'Immich non plus, et
        aucun sous-tag ne reste dessous. Un tag que l'utilisateur a réutilisé est donc conservé."""
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
        for value in sorted(unused, key=len, reverse=True):   # les enfants avant les parents
            if any(v.startswith(value + "/") for v in existing) or self.client.count_assets_with_tag(ours[value]):
                continue
            try:
                self.client.delete_tag(ours[value])
            except Exception:
                log.warning("tag %s non supprimé", value, exc_info=True)
                continue
            existing.pop(value, None)
            self.db.execute("DELETE FROM written WHERE kind = 'tag' AND immich_id = ?", (ours[value],))
            n += 1
        self.db.commit()
        if n:
            log.info("%d tag(s) devenu(s) inutile(s) supprimé(s)", n)
        return n

    def same_server(self) -> bool:
        """Faux si aucun tag tracé n'existe sur ce serveur : autre Immich ou autre compte. Immich y répondrait « not
        found » partout, ce qui passerait pour des photos supprimées et effacerait les traces sans rien retirer."""
        ids = {r["immich_id"] for r in self.db.execute("SELECT immich_id FROM written WHERE kind IN ('tag', 'tag_asset')")}
        self._tag_ids = None
        return not ids or bool(ids & set(self._tags().values()))

    def _clean_asset(self, aid: str, tag_ids: set[str], description: bool, stats: SyncStats) -> bool:
        """Retire nos tags et notre bloc de la photo telle qu'elle est maintenant. Vrai si quelque chose a été écrit :
        la photo est alors relue au tour suivant, car Immich peut perdre une écriture. Faux : plus rien à retirer."""
        a = self.client.get_asset(aid, missing_ok=True)
        if a is None:   # photo supprimée : plus rien à nettoyer
            return False
        wrote = False
        present = {t["id"] for t in a.get("tags") or []}
        for tid in sorted(tag_ids & present):
            self._write(aid, self.client.untag_assets, tid, [aid])
            stats.tags_removed += 1
            wrote = True
        current = (a.get("exifInfo") or {}).get("description") or ""
        if description and _BLOCK_RE.search(current):   # sans notre bloc, on ne réécrit pas le texte de l'utilisateur
            self._write(aid, self.client.set_description, aid, merge_description(current, None))
            stats.descriptions += 1
            wrote = True
        return wrote

    def uninstall(self) -> SyncStats:
        """Retire d'Immich ce que l'outil y a posé, et seulement cela. Les écritures sont d'abord suspendues : sans
        cela, le cycle suivant reposerait tout. La trace d'une photo n'est effacée qu'une fois relue propre ; ce qui
        reste (self.remaining) est repris en relançant uninstall."""
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
                    log.warning("photo %s non nettoyée : %s", aid, e)
                    stats.failed += 1
                    failures += 1
                    if failures >= MAX_CONSECUTIVE_FAILURES:   # Immich en panne : pas la peine d'insister sur chaque photo
                        raise RuntimeError(f"{failures} échecs de suite, retrait arrêté : {e}") from e
                    continue
                failures = 0
                if wrote:
                    again.append(aid)
                elif not dry:
                    self.db.execute("DELETE FROM written WHERE kind IN ('tag_asset', 'description') AND asset_id = ?", (aid,))
                    self.db.execute("DELETE FROM asset_sync WHERE asset_id = ?", (aid,))   # à réécrire si on reprend
                    self.db.commit()
            pending = again
            if dry or not pending:
                break
            time.sleep(self.write_gap * 2)
        if not dry:
            self.remaining = self.db.execute("SELECT count(DISTINCT asset_id) FROM written WHERE kind IN ('tag_asset', 'description')").fetchone()[0]
            if self.remaining:
                log.warning("%d photo(s) pas encore nettoyée(s) : tags conservés, relancer le retrait", self.remaining)
                return stats
        done: list[tuple[str, str, str]] = []   # tags retirés : traces effacées à la fin
        # Ne supprime un tag créé par l'outil que s'il ne sert plus à rien d'autre : aucun asset encore tagué
        # (l'utilisateur a pu le réutiliser) et aucun sous-tag encore présent, à nous ou non (Immich supprime
        # les sous-tags avec leur parent).
        self._tag_ids = None   # relue : l'utilisateur a pu en créer entre-temps
        if self.client.dry_run:
            ours = {r["value"]: r["immich_id"] for r in self.db.execute("SELECT immich_id, value FROM written WHERE kind = 'tag'")}
            all_values = set(ours)
        else:   # traces des tags supprimés à la main dans Immich effacées par _ours
            existing = self._tags()
            ours, all_values = self._ours(existing), set(existing)
        for value in sorted(ours, key=len, reverse=True):   # les enfants avant les parents
            if any(v.startswith(value + "/") for v in all_values) or (
                    not self.client.dry_run and self.client.count_assets_with_tag(ours[value])):
                log.info("tag conservé (utilisé ailleurs) : %s", value)
                continue
            try:
                self.client.delete_tag(ours[value])
            except Exception as e:
                log.warning("tag %s non supprimé : %s", value, e)
                continue
            all_values.discard(value)
            stats.tags_deleted += 1
            done.append(("tag", ours[value], ""))
        if not self.client.dry_run:
            self.db.executemany("DELETE FROM written WHERE kind = ? AND immich_id = ? AND asset_id = ?", done)
            self.db.execute("DELETE FROM asset_sync")
            self.db.commit()
        return stats
