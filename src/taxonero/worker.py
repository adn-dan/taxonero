"""Work loop: Immich asset scan, analysis, calibrated auto-confirmation, synchronisation.
Auto-confirmation rules: autoconfirm.py; detections already in the database awaiting review: curation.py."""

import hashlib
import io
import json
import logging
import queue
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image

from . import backbone, hardware, ranges, series, store, wiki
from .config import EDITABLE, Settings, apply_overrides
from .immich.client import is_missing, is_transient, probe
from .pipeline import _contained, _iou, Analysis, Analyzer, Box, Classifier, Detector, image_quality, open_image, OpenVocabDetector
from .sources import SOURCES, available, check_path, make_source, source_path
from .sync import WRITE_GAP, Syncer
from .taxonomy import HIGHER, NAMES_VERSION, TAXON_VERSION, Taxonomy, tidy_names, to_json
from .autoconfirm import audit_pick, auto_threshold, blocked_groups, should_auto_confirm
from .curation import EXCLUDED, cand_json, candidate_filter, drop_unwanted, prune_pending

log = logging.getLogger(__name__)

ANALYZER_ID = "mdv6-apa-rtdetr-e+owlv2-base+bioclip-2"
# Candidates whose common names are fetched during analysis (shown in the review card).
CANDIDATE_NAMES = 5
# "Whole image" fallback (no animal detected): kept only above this threshold.
FALLBACK_MIN_CONF = 0.6


class Unavailable(RuntimeError):
    """Network service temporarily unavailable during analysis (Immich, GBIF)."""


class Busy(RuntimeError):
    """A cycle is still running despite the stop request: the setting asked for from the dashboard is not applied."""


class Interrupted(BaseException):
    """Cycle stopped at a safe point to apply a dashboard setting (Worker._idle). BaseException, like
    KeyboardInterrupt: the "except Exception" blocks that protect a photo or a request do not stop it."""


# Photo kept in the analysis queue (SQL condition on asset; parameters: Settings.skip_args, i.e.
# analyze_videos, skip_raw_twins, skip_people).
SKIPPED_NOT = "(? OR kind IS NOT 'VIDEO') AND (NOT ? OR twin_of IS NULL) AND (NOT ? OR coalesce(people, 0) = 0)"
# Unidentified box kept for review only if the detector is confident
# (score scales specific to each detector).
UNKNOWN_BOX_MIN_SCORE = {"md": 0.7, "owl": 0.5}
# Beyond this, a photo whose analysis fails is no longer retried (otherwise it blocks the queue).
MAX_FAILURES = 3
# Full check of the Immich state (missing photos, tags removed by hand): once a day.
DAILY = 86400
# Setting changed during a cycle: time given to the cycle to reach a stopping point (one photo, one
# network request) before giving up (Busy).
STOP_WAIT = 60


def in_window(window: str, now: datetime | None = None) -> bool:
    """Time window "HH:MM-HH:MM" (may span midnight). Empty = always."""
    if not window:
        return True
    now = now or datetime.now()
    start, end = (datetime.strptime(x.strip(), "%H:%M").time() for x in window.split("-"))
    t = now.time()
    return start <= t < end if start <= end else (t >= start or t < end)


def fetch_model(url: str, dest: Path, sha256: str = "", user_agent: str = "taxonero") -> None:
    """Download a model file (temporary file renamed at the end; checksum verified if given)."""
    log.info("downloading model %s…", dest.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp, h = dest.with_suffix(".part"), hashlib.sha256()
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60, read=300),
                          headers={"User-Agent": user_agent}) as r, open(tmp, "wb") as f:
            r.raise_for_status()
            for chunk in r.iter_bytes(2 ** 20):
                f.write(chunk)
                h.update(chunk)
        if sha256 and h.hexdigest() != sha256.lower():
            raise ValueError(f"unexpected checksum for {dest.name}: {h.hexdigest()}")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


# Largest file of each Hugging Face model (repository, file, repository type): present in the cache, the
# model is downloaded. BioCLIP 2: model weights and species text embeddings (TreeOfLife-200M dataset).
HF_FILES = [("imageomics/bioclip-2", "open_clip_model.safetensors", None),
            ("imageomics/TreeOfLife-200M", "embeddings/txt_emb_species.npy", "dataset")]


def models_on_disk(s: Settings) -> bool:
    """Models already downloaded. Otherwise first use starts with a ~5 GB download, to be announced."""
    from huggingface_hub import try_to_load_from_cache

    from .megadetector import VERSION
    if not (s.detector_path.exists() or (s.data_dir / "models" / f"{VERSION}.weights.pt").exists()):
        return False
    files = HF_FILES + ([(s.open_detector_model, "model.safetensors", None)] if s.open_detector else [])
    return all(isinstance(try_to_load_from_cache(r, f, repo_type=t), str) for r, f, t in files)


def fetch_models(s: Settings) -> None:
    """Download the models, loaded on the CPU then released right away (desktop installation: everything is
    ready at first use). Same paths as the analysis, hence exactly the files it will ask for."""
    import gc

    from .megadetector import weights
    if models_on_disk(s):   # application update: nothing to download
        return
    if not s.detector_path.exists():
        print("MegaDetector", flush=True)
        weights(s.data_dir / "models", s.user_agent)
    if s.open_detector:
        print("OWLv2", flush=True)
        OpenVocabDetector(s.open_detector_model, device="cpu")
        gc.collect()
    print("BioCLIP 2", flush=True)
    Classifier(device="cpu")


def requeue(db: sqlite3.Connection, scope: str) -> int:
    """Put photos back in the analysis queue. failed: given up after failures; outdated: analysed
    by another model version; all: all of them. Reviewed detections are kept."""
    where, args = {"failed": ("fail_count >= ?", (MAX_FAILURES,)),
                   "outdated": ("analyzed_at IS NOT NULL AND analyzer IS NOT ?", (ANALYZER_ID,)),
                   "all": ("1", ())}[scope]
    # AI results still waiting for the geographic check: made again too (other model version, or failed). Before
    # the update, which resets fail_count.
    db.execute(f"DELETE FROM inferred WHERE asset_id IN (SELECT asset_id FROM asset WHERE {where})" if scope == "failed"
               else "DELETE FROM inferred", args if scope == "failed" else ())
    n = db.execute(f"UPDATE asset SET analyzed_at = NULL, fail_count = 0, last_error = NULL WHERE {where}", args).rowcount
    db.commit()
    return n


# -- service -------------------------------------------------------------------

class _TagWriter:
    """Tags written to the source while the analysis goes on ("tags" thread, its own database connection): each
    photo confirmed by the geographic check gets its tags a few seconds later, instead of at the end of the cycle.
    Only the photos it is given (no pass over the whole library); the sync at the end of the cycle stays as the
    safety net (photos postponed because Immich just changed them, RAW twins, failures)."""

    def __init__(self, w: "Worker"):
        self.w, self.q, self.done, self.queued = w, queue.Queue(), 0, 0
        self.thread = threading.Thread(target=self._run, daemon=True, name="tags")
        self.thread.start()

    def add(self, asset_ids: list[str]) -> None:
        """Photos just saved: those with a confirmed detection go to the writer."""
        ids = [a for a in asset_ids if self.w.db.execute(
            "SELECT 1 FROM detection WHERE asset_id = ? AND status = 'confirmed' LIMIT 1", (a,)).fetchone()]
        for a in ids:
            self.q.put(a)
        self.queued += len(ids)
        self._show()

    def _show(self) -> None:
        if (phases := self.w.progress.get("phases")) is not None:
            phases["tags"] = [self.done, self.queued, phases.get("tags", [0, 0, time.time()])[2]]

    def _run(self) -> None:
        w, s = self.w, self.w.s
        db = store.connect(s.db_path)
        try:
            syncer = Syncer(db, w.client, s.tag_root, s.names_lang, s.write_description,
                            write_gap=getattr(w.client, "write_gap", WRITE_GAP), checkpoint=w._checkpoint)
            end = False
            while not end:
                ids = [self.q.get()]
                while True:   # everything waiting, in one pass
                    try:
                        ids.append(self.q.get_nowait())
                    except queue.Empty:
                        break
                end = None in ids
                ids = [a for a in ids if a]
                if ids:
                    syncer.sync_all(asset_ids=ids, full=False)
                    for aid in syncer.missing:
                        w.mark_missing(aid, db)
                    syncer.missing.clear()
                    self.done += len(ids)
                    self._show()
        except Interrupted:   # setting from the dashboard: stops, the next cycle resumes
            pass
        except Exception:
            log.exception("tags written during the analysis: stopped, the end of the cycle writes them")
        finally:
            db.close()

    def finish(self) -> None:
        self.q.put(None)
        while self.thread.is_alive():
            self.thread.join(1)


class Worker:
    def __init__(self, s: Settings, analyzer: Analyzer | None = None):
        self.s = s
        self.db = store.connect(s.db_path)
        self.undo: dict = {"state": "idle"}   # retrait d'Immich (start_uninstall)
        # Environment values (or defaults), then settings made from the dashboard on top.
        self.env_defaults = {k: getattr(s, k) for k in EDITABLE}
        apply_overrides(s, store.get_state(self.db, "settings"))
        # Immich connection: environment first, otherwise the one saved from the dashboard.
        self.env_immich = {"url": bool(s.immich_url), "key": bool(s.immich_api_key)}
        saved = json.loads(store.get_state(self.db, "immich") or "{}")
        s.immich_url = s.immich_url or saved.get("url", "")
        s.immich_api_key = s.immich_api_key or saved.get("key", "")
        # Desktop: result writing set in the dashboard (DRY_RUN from the environment otherwise).
        if s.edition == "desktop" and "dry_run" not in s.model_fields_set and store.get_state(self.db, "dry_run"):
            s.dry_run = store.get_state(self.db, "dry_run") == "1"
        # Photo source: environment first (locked), otherwise the one chosen in the dashboard.
        self.env_source = bool({"source", "photos_dir", "catalog_path"} & s.model_fields_set)
        saved_source = json.loads(store.get_state(self.db, "source") or "{}")
        if not self.env_source and saved_source.get("source") in SOURCES:
            s.source = saved_source["source"]
            s.photos_dir = Path(saved_source.get("photos_dir") or s.photos_dir)
            s.catalog_path = Path(saved_source["catalog_path"]) if saved_source.get("catalog_path") else s.catalog_path
        self.client = make_source(s)
        self.tx = Taxonomy(self.db, langs=s.langs, user_agent=s.user_agent, backbone=s.backbone_path if s.gbif_local else None)
        self._analyzer = analyzer
        self._device: str | None = None   # device of the loaded models (None: provided from outside)
        # Health (/healthz): last loop activity; a frozen loop makes the container "unhealthy".
        self.loop_started, self.last_activity = False, time.time()
        # Model loading (several GB): only once, even if the dashboard and the cycle ask for it together.
        self._analyzer_lock = threading.Lock()
        self._models_used = 0.0   # last request for the models (time.monotonic), to unload them when idle
        self._stages_kept: dict[str, list] = {}   # time per stage of unloaded models (Settings page)
        # Original for small subjects: None until tried, True if obtained, False without the asset.download permission.
        self._original_ok: bool | None = None
        self.backbone_state = {"state": "waiting" if s.gbif_local else "off"}   # local GBIF taxonomy (Settings)
        self._speed: list[float] = []   # time per photo of the latest photos (s), whole analysis or AI part (two phases)
        self._speed_geo: list[float] = []   # same for the geographic check (second phase)
        self._last_done_geo = 0.0
        self._decider: Analyzer | None = None   # decision without the models (second phase), see decider
        self._timings: dict[str, list] = {}   # worker stages (download, saving) -> [seconds, photos]
        self._last_done = 0.0
        self._blocked_set: set[str] = set()
        self._blocked_at = -1e9
        # One cycle at a time (automatic loop or "Sync" button), same SQLite connection.
        self.cycle_lock = threading.Lock()
        # Stop requested by a dashboard setting (_idle), checked at the cycle's safe points (_checkpoint); wakes
        # the loop to resume right after.
        self._stop, self._wake = threading.Event(), threading.Event()
        self._cycle_manual = False    # is the running cycle manual ("Sync" button)?
        self._resume_manual = False   # manual cycle stopped by a setting: the loop restarts it as manual
        self._resume_pending = False  # cycle stopped by a setting, to be restarted by the loop
        self._loading: threading.Thread | None = None   # model loading, outside the cycle
        # Analysis paused from the dashboard (it takes the whole machine): kept from one launch to the next.
        self.analysis_paused = store.get_state(self.db, "analysis_paused") == "1"
        self._load_error: BaseException | None = None
        self.progress: dict = {"running": False, "step": None, "done": 0, "total": 0, "current": None, "step_started": None,
                               "started": None, "finished": None, "error": None}
        n = tidy_names(self.db)
        if n:
            log.info("%d common name(s) set back to normal case (e.g. \"DOGS\")", n)
        n = ranges.migrate_legacy(self.db)
        if n:
            log.info("%d range map(s) in an old format deleted, downloaded again later", n)

    def backup(self, force: bool = False) -> Path | None:
        """Copy of today's database in DATA_DIR/backups (one per day, the BACKUP_KEEP most recent kept).
        Consistent copy even during analysis (VACUUM INTO), stripped of the network caches that can be downloaded
        again: GBIF/Wikidata responses and range maps. Without the Immich API key either (to be entered again in the
        dashboard after a restore). Restore: stop the service, copy the chosen file over
        DATA_DIR/taxonero.sqlite, restart."""
        keep = self.s.backup_keep
        today = datetime.now().strftime("%Y-%m-%d")
        if keep <= 0 or (not force and store.get_state(self.db, "last_backup") == today):
            return None
        folder = self.s.data_dir / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        dest, tmp = folder / f"taxonero-{today}.sqlite", folder / f".taxonero-{today}.tmp"
        tmp.unlink(missing_ok=True)
        self.db.commit()
        self.db.execute("VACUUM INTO ?", (str(tmp),))
        copy = sqlite3.connect(tmp)
        for table in ("http_cache", "taxon_range"):
            if copy.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone():
                copy.execute(f"DELETE FROM {table}")
        copy.execute("DELETE FROM state WHERE key = 'immich'")   # the API key does not travel with backups
        copy.execute("DELETE FROM inferred")   # AI results waiting for the check: made again after a restore
        copy.commit()
        copy.execute("VACUUM")
        copy.close()
        tmp.replace(dest)
        for old in sorted(folder.glob("taxonero-*.sqlite"))[:-keep]:
            old.unlink()
        store.set_state(self.db, "last_backup", today)
        log.info("sauvegarde : %s (%d Ko)", dest.name, dest.stat().st_size // 1024)
        return dest

    def _checkpoint(self) -> None:
        """Safe stopping point of the cycle (nothing half written): Interrupted if a setting is waiting (_idle). Reaching a
        stopping point proves the cycle is progressing: /healthz counts it as activity (long synchronisation,
        daily check), without which the container would turn "unhealthy" in the middle of the work."""
        self.last_activity = time.time()
        if self._stop.is_set():
            raise Interrupted()

    def _step(self, step: str, total: int = 0) -> None:
        """Current stage, as a code (check, scan, reconcile, models, analyze, prune, sync, prefetch): the dashboard
        translates it. current: item in progress (file name during analysis). Stopping point (_checkpoint)."""
        self._checkpoint()
        self.progress.update(step=step, done=0, total=total, scale=1, current=None, step_started=time.time())
        if step not in ("analyze", "geo"):
            self.progress["phases"] = None
        self.last_activity = time.time()

    def _tick_phase(self, phase: str) -> None:
        """Next photo of one of the two analysis phases, which may run at the same time: its own counter, and the
        step counter (top bar, remaining time) if it is the step shown. Stopping point (_checkpoint)."""
        self._checkpoint()
        if (phases := self.progress.get("phases")) and phase in phases:
            phases[phase][0] += 1
        if self.progress["step"] == phase:
            self.progress["done"] += 1
        self.last_activity = time.time()

    def _tick(self) -> None:
        """Next item of a stage; stopping point (_checkpoint): call outside any open transaction."""
        self._checkpoint()
        self.progress["done"] += 1
        if (phases := self.progress.get("phases")) and self.progress["step"] in phases:
            phases[self.progress["step"]][0] += 1
        self.last_activity = time.time()

    @property
    def analyzer(self) -> Analyzer:
        with self._analyzer_lock:
            device, why = hardware.resolve(self.s.device)
            ov_mode = hardware.open_mode(self.s.open_detector_mode, device)
            if self._analyzer is not None and self._device not in (None, device):
                # GPU / CPU choice changed in Settings: models reloaded on the new device
                # (the running analysis, if any, finishes on the old one).
                log.info("compute: switching from %s to %s, models reloaded", self._device, device)
                old, self._analyzer = self._device, None
                hardware.release(old)
            if self._analyzer is None:
                log.info("calcul : %s (%s), OWLv2 %s", hardware.WHY[why], device, ov_mode)
                hardware.full_precision()
                self._device = device
                det = None
                if self.s.detector_path.exists():   # ONNX (older Docker images, or provided): kept as is
                    det = Detector(self.s.detector_path, providers=hardware.onnx_session_providers(device),
                                   min_score=self.s.detector_min_score)
                    det.providers = det.session.get_providers()
                else:   # PyTorch, official weights downloaded at first use (megadetector.py)
                    from .megadetector import TorchDetector, weights
                    det = TorchDetector(weights(self.s.data_dir / "models", self.s.user_agent), device,
                                        min_score=self.s.detector_min_score)
                log.info("detector: %s", ", ".join(det.providers))
                ov = None
                if self.s.open_detector:
                    ov = OpenVocabDetector(self.s.open_detector_model, device=device,
                                           min_score=self.s.open_detector_min_score)
                analyzer = Analyzer(det, Classifier(device=device), self.tx, open_detector=ov)
                analyzer.drop = candidate_filter(self.s, self.tx)
                self._analyzer = analyzer
            self._analyzer.open_mode = ov_mode   # no reload
            self._models_used = time.monotonic()
            return self._analyzer

    def unload_idle(self) -> bool:
        """Models unused for models_idle_minutes: removed from GPU memory, reloaded on the next request.
        A running analysis keeps its reference to the models: it finishes normally. Nothing on the CPU, where they
        take no GPU memory, nor for an analyser provided from outside (_device None)."""
        minutes = self.s.models_idle_minutes
        with self._analyzer_lock:
            if (self._analyzer is None or self._device in (None, "cpu") or minutes <= 0
                    or time.monotonic() - self._models_used < minutes * 60):
                return False
            device, self._stages_kept = self._device, {**self._stages_kept, **getattr(self._analyzer, "timings", {})}
            self._analyzer = None
        hardware.release(device)
        log.info("models unloaded from %s after %d min without analysis", device, minutes)
        return True

    def _watch_idle_models(self) -> None:
        while True:
            time.sleep(30)
            try:
                self.unload_idle()
            except Exception:
                log.exception("model unloading failed")

    @property
    def configured(self) -> bool:
        return self.s.source != "immich" or bool(self.s.immich_url and self.s.immich_api_key)

    def credentials(self, url: str, api_key: str) -> tuple[str, str]:
        """(url, key) of a connection entered in the dashboard; environment values take precedence. Empty key:
        the saved key, but only for the server already set (or when the url is first set): sent to
        another url, it would go to that other server. ValueError in that case."""
        url = self.s.immich_url if self.env_immich["url"] else url.strip().rstrip("/")
        given = "" if self.env_immich["key"] else api_key.strip()
        if not given and self.s.immich_url and url != self.s.immich_url:
            raise ValueError("new address: enter the API key as well")
        return url, given or self.s.immich_api_key

    def connect(self, url: str, api_key: str) -> dict:
        """New connection from the dashboard: tested, then saved and used only if Immich answers
        and accepts the key (otherwise nothing changes). Values coming from the environment are not replaced.
        Empty key: the saved key (see credentials)."""
        try:
            url, api_key = self.credentials(url, api_key)
        except ValueError as e:
            return {"code": "key_required", "errors": [str(e)]}
        # Normal timeout (30 s): this client is then used for analysis (heavy originals), not only for the test.
        client, state = probe(url, api_key, dry_run=self.s.dry_run, timeout=30.0)
        if client is None:
            return state
        try:
            with self._idle():
                saved = {k: v for k, v in (("url", url), ("key", api_key)) if not self.env_immich[k]}
                store.set_state(self.db, "immich", json.dumps(saved))
                self.s.immich_url, self.s.immich_api_key = url, api_key
                old, self.client = self.client, client
                self._original_ok = None
                self.check()
        except Busy:
            client.close()
            raise
        log.info("Immich %s connection saved (%s)", client.version, state["code"])
        threading.Timer(30, old.close).start()
        return state

    def set_dry_run(self, value: bool) -> None:
        """Desktop edition: result writing enabled from the dashboard (no environment to set)."""
        if self.s.edition != "desktop":
            raise ValueError("DRY_RUN is set in the environment (server edition)")
        with self._idle():
            before = self.s.dry_run
            self.s.dry_run = self.client.dry_run = bool(value)
            try:
                self.check()
            except Exception:   # source unreachable: setting unchanged, especially when enabling writes
                self.s.dry_run = self.client.dry_run = before
                raise
            store.set_state(self.db, "dry_run", "1" if value else "0")

    def set_source(self, source: str, path: str | None = None) -> dict:
        """Change source from the dashboard (unless the environment sets it): photo folder or
        application library depending on the source. Photos of the previous source stay in the database with their
        reviews: hidden at the next check, they come back if the user switches back."""
        if self.env_source:
            raise ValueError("source set by the environment (SOURCE, PHOTOS_DIR, CATALOG_PATH)")
        if not any(o["id"] == source and o["available"] for o in available(self.s.edition)):
            raise ValueError(f"source not available here: {source}")
        with self._idle():
            where = SOURCES[source]["kind"]
            before = (self.s.source, self.s.photos_dir, self.s.catalog_path)
            if where != "server":
                chosen = Path(path or "").expanduser()
                if reason := check_path(source, chosen):
                    raise ValueError(reason)
                if where == "folder":
                    self.s.photos_dir = chosen
                else:
                    self.s.catalog_path = chosen
            self.s.source = source
            old = self.client
            try:
                self.client = make_source(self.s)
                self.check()
            except Exception:   # source unreachable: stay on the previous one, nothing is saved
                if self.client is not old:
                    self.client.close()
                self.client = old
                self.s.source, self.s.photos_dir, self.s.catalog_path = before
                raise
            store.set_state(self.db, "source", json.dumps({"source": source, "photos_dir": str(self.s.photos_dir),
                                                           "catalog_path": str(self.s.catalog_path or "")}))
            threading.Timer(30, old.close).start()
            for key in ("last_scan", "last_scan_video", "last_reconcile"):
                store.set_state(self.db, key, "")   # new source: list everything again at the next cycle
        log.info("photo source: %s %s", source, source_path(self.s) or self.s.immich_url)
        return {"source": source, "writable": self.client.writable, **self.client.status()}

    @contextmanager
    def _idle(self):
        """Change made from the dashboard (source, connection, writing): never during a cycle, which uses
        the same SQLite connection and the same source (otherwise a running scan overwrites its "list everything" marker, or
        goes on with a closed client). A running cycle is stopped at the next safe point (_checkpoint), the setting
        applied, then the cycle restarted right away: the user has nothing to redo. Busy if the cycle could not
        stop within STOP_WAIT seconds."""
        running = not self.cycle_lock.acquire(blocking=False)
        if running:
            manual = self._cycle_manual
            self._stop.set()
            got = self.cycle_lock.acquire(timeout=STOP_WAIT)
            self._stop.clear()
            if not got:
                raise Busy("a synchronisation cycle is running")
        try:
            yield
        finally:
            self.cycle_lock.release()
            if running:   # restarted by the loop (loop), in the same mode
                self._resume_manual = self._resume_manual or manual
                self._resume_pending = True
                self._wake.set()

    def check(self) -> bool:
        if not self.configured:
            store.set_state(self.db, "writable", "0")
            return False
        self._original_ok = None   # asset.download permission possibly added since a 403
        ok = self.client.check()
        for w in self.client.warnings:
            log.warning("compatibility: %s", w)
        for e in self.client.errors:
            log.error("incompatibility: %s", e)
        store.set_state(self.db, "immich_version", self.client.version or "")
        store.set_state(self.db, "writable", "1" if ok else "0")
        return ok

    def mark_missing(self, asset_id: str, db: sqlite3.Connection | None = None) -> None:
        db = db or self.db
        log.info("photo %s missing from Immich (deleted or in the trash): hidden", asset_id)
        db.execute("UPDATE asset SET missing_at = ? WHERE asset_id = ?", (time.time(), asset_id))
        db.commit()

    def refresh_taxa(self, limit: int = 50) -> int:
        """Enrich again the taxa saved in an older format (e.g. before multilingual names)."""
        n = 0
        for r in self.db.execute("SELECT gbif_key, data FROM taxon").fetchall():
            data = json.loads(r["data"])
            if data.get("v", 0) >= TAXON_VERSION or n >= limit:
                continue
            self.db.commit()
            self._checkpoint()
            t = self.tx.match(data["scientific_name"], kingdom=data.get("lineage", {}).get("kingdom"))
            if t and t.gbif_key == r["gbif_key"]:
                store.save_taxon(self.db, t.gbif_key, to_json(t))
                n += 1
        self.db.commit()
        if n:
            log.info("%d taxon(s) enriched again to format %d", n, TAXON_VERSION)
        # Plural names of higher ranks (GBIF) for taxa cached before this rule.
        m = 0
        for r in self.db.execute("SELECT gbif_key, data FROM taxon").fetchall():
            data = json.loads(r["data"])
            if m >= limit or data.get("nv", 0) >= NAMES_VERSION or not HIGHER(data.get("rank", "species")):
                continue
            self.db.commit()
            self._checkpoint()
            try:
                data = self.tx.plural_names({**data, "gbif_key": r["gbif_key"]})
            except Exception:
                log.debug("GBIF names unavailable for %s", data.get("scientific_name"), exc_info=True)
                continue
            store.save_taxon(self.db, r["gbif_key"], json.dumps(data, ensure_ascii=False))
            m += 1
        self.db.commit()
        if m:
            log.info("%d higher rank(s) renamed to plural (GBIF names)", m)
        # Species without an article: search by name and by synonyms (Wikidata often has one item per name).
        a = 0
        for r in self.db.execute("SELECT gbif_key, data FROM taxon").fetchall():
            data = json.loads(r["data"])
            if a >= limit or not self.tx.articles_stale(data):
                continue
            self.db.commit()
            self._checkpoint()
            try:
                data = self.tx.refresh_articles({**data, "gbif_key": r["gbif_key"]})
            except Exception:
                log.debug("articles unavailable for %s", data.get("scientific_name"), exc_info=True)
                continue
            store.save_taxon(self.db, r["gbif_key"], json.dumps(data, ensure_ascii=False))
            a += 1
        self.db.commit()
        if a:
            log.info("articles searched again for %d species", a)
        return n + m + a

    def refresh_places(self, limit: int = 200) -> int:
        """Catch-up: places (city, region, country) of photos saved before they were supported."""
        rows = self.db.execute("SELECT asset_id FROM asset WHERE country IS NULL AND lat IS NOT NULL AND missing_at IS NULL "
                               "AND (city IS NULL OR city != '') LIMIT ?", (limit,)).fetchall()
        for r in rows:
            self.db.commit()
            self._checkpoint()
            a = self.client.get_asset(r["asset_id"], missing_ok=True)
            if a is None:
                self.mark_missing(r["asset_id"])
                continue
            exif = a.get("exifInfo") or {}
            # Empty string if Immich has no place: avoids asking again at every cycle.
            self.db.execute("UPDATE asset SET city = ?, state = ?, country = ? WHERE asset_id = ?",
                            (exif.get("city") or "", exif.get("state"), exif.get("country"), r["asset_id"]))
        self.db.commit()
        return len(rows)

    def kinds(self) -> tuple[str, ...]:
        return ("IMAGE", "VIDEO") if self.s.analyze_videos else ("IMAGE",)

    def scan(self) -> int:
        """Fetch the assets new or changed since the last scan. One marker per type: enabling
        videos later fetches all of them at the next cycle."""
        n = 0
        # Progress: files walked out of the total for a file source (known in advance), otherwise a counter of the
        # photos received (Immich gives no total for "changed since").
        walked = hasattr(self.client, "count_files")
        if walked:
            self.progress["total"] = self.client.count_files()
            self.client.on_file = self._tick
        try:
            for kind in self.kinds():
                key = "last_scan" if kind == "IMAGE" else "last_scan_video"
                since = store.get_state(self.db, key)
                started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                for a in self.client.iter_assets(updated_after=since or None, kind=kind, with_people=True):
                    # Stopping point; marker not advanced: the next scan picks these photos up again.
                    self._checkpoint() if walked else self._tick()
                    if store.upsert_asset(self.db, a, with_people=True):
                        n += 1
                self.db.commit()
                store.set_state(self.db, key, started)
        finally:
            if walked:
                self.client.on_file = None
        return n

    def reconcile(self, force: bool = False) -> dict | None:
        """Once a day: full list of Immich photos (without EXIF, hence light) to spot
        those deleted or moved to the trash. They are hidden, not erased: a restored
        photo comes back with its reviews. Also reads the people of each photo again: Immich recognises
        faces after import, without changing the modification date that scan() follows."""
        last = float(store.get_state(self.db, "last_reconcile") or 0)
        # Database from before people were counted: one pass right away, without waiting for the next day.
        unknown = self.db.execute("SELECT 1 FROM asset WHERE people IS NULL AND missing_at IS NULL "
                                  "AND (? OR kind IS NOT 'VIDEO') LIMIT 1", (self.s.analyze_videos,)).fetchone()
        if not force and not unknown and time.time() - last < DAILY:
            return None
        self._step("reconcile")
        # Videos disabled: they are not listed, hence hidden like missing photos.
        listed = {}
        for kind in self.kinds():
            for a in self.client.iter_assets(with_exif=False, size=1000, kind=kind, with_people=True):
                self._tick()   # counter of the photos listed (no total known in advance)
                listed[a["id"]] = len(a.get("people") or [])
        present = listed.keys()
        local = {r["asset_id"]: r["missing_at"] for r in self.db.execute("SELECT asset_id, missing_at FROM asset")}
        if not present and local:
            log.warning("Immich returns no photo: deletion check skipped")
            return None
        gone = [a for a, m in local.items() if a not in present and m is None]
        back = [a for a, m in local.items() if a in present and m is not None]
        now = time.time()
        self.db.executemany("UPDATE asset SET missing_at = ? WHERE asset_id = ?", [(now, a) for a in gone])
        self.db.executemany("UPDATE asset SET missing_at = NULL WHERE asset_id = ?", [(a,) for a in back])
        self.db.executemany("UPDATE asset SET people = ? WHERE asset_id = ? AND people IS NOT ?",
                            [(n, a, n) for a, n in listed.items()])
        self.db.commit()
        store.set_state(self.db, "last_reconcile", str(now))
        if gone or back:
            log.info("photos missing from Immich: %d, back: %d", len(gone), len(back))
        return {"missing": len(gone), "back": len(back)}

    def original(self, asset_id: str, kind: str | None = None) -> bytes | None:
        """Original for small subjects; None without the asset.download permission (noted once), and
        never for a video (heavy file, and Immich's key frame is already at full preview size). Deleted
        photo (is_missing): the exception propagates, so that the caller says so instead of blaming the permission."""
        if self._original_ok is False:
            return None
        if kind is None:
            row = self.db.execute("SELECT kind FROM asset WHERE asset_id = ?", (asset_id,)).fetchone()
            kind = row["kind"] if row else None
        if kind == "VIDEO":
            return None
        try:
            data = self.client.original(asset_id)
            self._original_ok = True
            return data
        except httpx.HTTPStatusError as e:
            if is_missing(e):
                raise
            if e.response.status_code in (401, 403):
                self._original_ok = False
                log.info("no asset.download permission: small subjects identified on the preview")
            return None
        except (OSError, ValueError) as e:   # local folder: RAW without an embedded preview, unreadable file
            if is_missing(e):   # file gone
                raise
            return None

    def _blocked(self) -> set[str]:
        """Groups excluded from auto-confirmation: recomputed at most once a minute (the computation goes through
        the reviews; for every photo, it cost ~90 ms on a 70,000-photo database and grew with it)."""
        now = time.monotonic()
        if now - self._blocked_at > 60:
            self._blocked_set, self._blocked_at = blocked_groups(self.db, self.s), now
        return self._blocked_set

    def _prepare(self, asset_id: str, results: list[Analysis], threshold: float | None) -> list[tuple]:
        """Rows to insert for an analysis. All network access (taxon records, candidate names) happens
        here, before writing: the write that follows is then a short, atomic transaction."""
        # Detections already reviewed (confirmed, rejected, deleted): a re-analysis does not create them twice.
        reviewed = [Box(r["x"], r["y"], r["w"], r["h"], 1.0, "animal") for r in self.db.execute(
            "SELECT x, y, w, h FROM detection WHERE asset_id = ? AND status != 'pending'", (asset_id,))]
        blocked = self._blocked() if threshold is not None else set()
        # Records and common names to fetch (decisions, top candidates): requested in parallel first.
        stale = lambda k: k and ((t := store.load_taxon(self.db, k)) is None or t.get("v", 0) < TAXON_VERSION)
        self.tx.prefetch_enrich([a.decision.gbif_key for a in results if stale(a.decision.gbif_key)]
                                + [c.gbif_key for a in results for c in a.decision.candidates[:CANDIDATE_NAMES]
                                   if c.gbif_key and not store.load_taxon(self.db, c.gbif_key)])
        rows = []
        for a in results:
            if any(_iou(a.box, b) >= 0.3 or _contained(a.box, b) >= 0.6 for b in reviewed):
                continue
            d = a.decision
            if a.box.label == "image" and (d.rank == "none" or d.confidence < FALLBACK_MIN_CONF):
                continue
            if d.name in EXCLUDED:
                continue
            if a.domestic and self.s.domestic_animals == "skip":
                continue
            # Unidentified box: kept for review only if the detector is confident.
            if d.rank == "none" and a.box.score < UNKNOWN_BOX_MIN_SCORE.get(a.box.source, 1.0):
                continue
            known = store.load_taxon(self.db, d.gbif_key) if d.gbif_key else None
            if d.gbif_key and (not known or known.get("v", 0) < TAXON_VERSION):
                t = self.tx.match(d.name, kingdom=(d.candidates[0].lineage.get("kingdom") if d.candidates else None))
                if t:
                    store.save_taxon(self.db, t.gbif_key, to_json(t))
                    self.db.commit()
            auto = should_auto_confirm(a, threshold, self.s.auto_confirm_max_runner_up, blocked)
            audit = auto and audit_pick(asset_id, a.box, self.s.auto_confirm_audit)
            auto = auto and not audit
            cands = [cand_json(c) for c in d.candidates[:8] if c.name not in EXCLUDED]
            out = [cand_json(c) for c in d.excluded[:8] if c.name not in EXCLUDED]
            b = a.box
            rows.append((asset_id, b.x, b.y, b.w, b.h, b.score, b.label, b.source,
                         "confirmed" if auto else "pending", "auto" if auto else None,
                         d.gbif_key, d.rank, d.confidence, d.margin, d.plausibility,
                         json.dumps(cands, ensure_ascii=False), json.dumps(out, ensure_ascii=False), int(audit),
                         d.gbif_key, d.rank, d.confidence, a.frame_support, time.time()))
            if not auto:
                self._enrich_candidates(d.candidates[:CANDIDATE_NAMES])
        return rows

    def _insert(self, rows: list[tuple]) -> int:
        self.db.executemany(
            "INSERT INTO detection (asset_id, x, y, w, h, det_score, box_label, detector, source, status, "
            "confirmed_by, taxon_key, taxon_rank, score, margin, plausibility, candidates, candidates_out, audit, "
            "model_key, model_rank, model_conf, frame_support, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'auto', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def _enrich_candidates(self, cands) -> None:
        """Common names of the top candidates, for a readable review (cached, non-blocking)."""
        for c in cands:
            if c.gbif_key and not store.load_taxon(self.db, c.gbif_key):
                try:
                    t = self.tx.match(c.accepted_name or c.name, kingdom=c.lineage.get("kingdom"))
                    if t and t.gbif_key == c.gbif_key:
                        store.save_taxon(self.db, t.gbif_key, to_json(t))
                        self.db.commit()
                except Exception:
                    log.debug("names unavailable for %s", c.name, exc_info=True)

    def _analyze_asset(self, asset_id: str, lat: float | None, lon: float | None) -> list[Analysis]:
        data = self.client.thumbnail(asset_id, self.s.thumbnail_size)
        analyzer = self.analyzer
        out = analyzer.analyze(data, lat, lon, original=lambda: self.original(asset_id), fallback=self.s.full_image_fallback,
                               groups=self.s.groups)
        return out

    def models_info(self) -> dict:
        """Models, hardware actually used and measured speed (Settings page)."""
        hw = hardware.describe(self.s.device)
        det = getattr(self._analyzer, "detector", None)
        median = lambda v: {"median_s": round((v[(len(v) - 1) // 2] + v[len(v) // 2]) / 2, 1), "n": len(v)} if v else None
        return {
            "hardware": hw,
            "loaded": self._analyzer is not None,
            "detector_file": True,   # ONNX provided, otherwise PyTorch (weights downloaded at first use)
            "detector_onnx": self.s.detector_path.exists(),
            "detector_providers": det.providers if det else None,
            "open_detector": self.s.open_detector, "open_detector_model": self.s.open_detector_model,
            "open_mode": hardware.open_mode(self.s.open_detector_mode, hw["device"]),
            "original": self._original_ok,
            "analyzer_id": ANALYZER_ID,
            "taxonomy": {**self.backbone_state, "local": self.tx.local is not None},
            # Where the time goes: ms per photo for each stage (stages overlap, see analyze()).
            "stages": {k: round(sec / n * 1000) for k, (sec, n) in {**self._stages_kept, **getattr(self._analyzer, "timings", {}), **self._timings}.items() if n},
            # Photos per GPU pass actually used (automatic from its memory, unless set), None on the CPU.
            "batch": hardware.batch_size(self._device or hw["device"], self.s.gpu_batch) if hw["device"] != "cpu" else None,
            "speed": median(sorted(self._speed)),           # whole analysis, or its AI part (two phases)
            "speed_geo": median(sorted(self._speed_geo)),   # geographic check, second phase
        }

    def queue_sql(self) -> tuple[str, tuple]:
        """Photos to analyse (SQL condition on asset, parameters): not missing, not given up, not skipped
        (videos if disabled, RAW paired with a JPG, photos with people if SKIP_PEOPLE)."""
        return ("analyzed_at IS NULL AND missing_at IS NULL AND fail_count < ? AND " + SKIPPED_NOT,
                (MAX_FAILURES, *self.s.skip_args))

    def pending_analysis(self) -> int:
        if self.s.detection == "manual":   # boxes drawn by hand only: no automatic analysis
            return 0
        where, args = self.queue_sql()
        return self.db.execute(f"SELECT count(*) FROM asset WHERE {where}", args).fetchone()[0]

    def pending_ai(self) -> int:
        """Photos of the queue still waiting for the AI part (first phase)."""
        return self.pending_analysis() - self.pending_geo()

    def pending_geo(self) -> int:
        """Photos of the queue whose AI result waits for the geographic check (second phase)."""
        if self.s.detection == "manual":
            return 0
        where, args = self.queue_sql()
        return self.db.execute(f"SELECT count(*) FROM inferred i JOIN asset ON asset.asset_id = i.asset_id WHERE {where}",
                               args).fetchone()[0]

    @property
    def decider(self) -> Analyzer:
        """Analyser without models, for the second phase: rerank and decision only (GBIF, settings filters).
        Its time goes with the worker's stages (geo)."""
        # Same taxonomy and filters as the analyser of the first phase (one provided from outside keeps its own).
        outside = isinstance(self._analyzer, Analyzer) and self._device is None
        if self._decider is None:
            self._decider = Analyzer(None, None, self._analyzer.taxonomy if outside else self.tx)
            self._decider.timings = self._timings
        self._decider.drop = self._analyzer.drop if outside else candidate_filter(self.s, self.tx)
        return self._decider

    def analyze(self, limit: int | None = None, infer_only: bool = False, db: sqlite3.Connection | None = None) -> int:
        """A batch of photos to analyse, most recent first. A failed photo is retried at the following
        cycles, then given up after MAX_FAILURES failures (visible in the dashboard, can be restarted).

        Pipelined, so that the GPU waits as little as possible: previews of the next photos are downloaded
        and decoded while the models work, and the models process a group ("gpu" thread) while
        the previous group goes through the geographic check (GBIF) and the database write.
        infer_only: first phase of the two-phase analysis (run_once), the models only: results stored for decide().
        db: connection of the calling thread; given, the AI part runs alongside the check (see _both_phases)."""
        phase, db = db is not None, db or self.db
        where, args = self.queue_sql()
        if infer_only:
            where += " AND NOT EXISTS (SELECT 1 FROM inferred i WHERE i.asset_id = asset.asset_id)"
        rows = db.execute(f"SELECT asset_id, file_name, lat, lon, kind FROM asset WHERE {where} "
                               "ORDER BY fail_count, taken_at DESC LIMIT ?", (*args, limit or self.s.batch_size)).fetchall()
        threshold = None if infer_only else auto_threshold(self.db, self.s)
        if not phase and self.progress["step"] != "analyze":   # run_once counts the whole queue, not only this batch
            self._step("analyze", len(rows))
        if not rows:
            return 0
        analyzer = self.analyzer   # model loading before the timing and the batch choice
        # GPU: several photos per BioCLIP pass. CPU: one at a time (no gain from grouping).
        chunk = hardware.batch_size(self._device, self.s.gpu_batch) if hasattr(analyzer, "analyze_many") else 1
        if getattr(analyzer, "open_detector", None) is not None:   # OWLv2: half as many at once (memory)
            analyzer.open_detector.batch = max(1, chunk // 2)
        piped = hasattr(analyzer, "infer_many")
        finish = (lambda *a: self._keep(*a, db=db)) if infer_only and piped else self._finish
        load = lambda r: open_image(self.client.thumbnail(r["asset_id"], self.s.thumbnail_size)) if piped else \
            self.client.thumbnail(r["asset_id"], self.s.thumbnail_size)
        # Previews downloaded ahead: enough at once to keep a large GPU batch fed, at least 4.
        with ThreadPoolExecutor(max(4, min(16, chunk)), thread_name_prefix="apercu") as pool, \
                ThreadPoolExecutor(1, thread_name_prefix="gpu") as gpu:
            fetch = lambda r: pool.submit(load, r)
            pending = [fetch(r) for r in rows[:chunk * 2]]
            previous = None
            try:
                for i in range(0, len(rows), chunk):
                    if self.analysis_paused:   # pause: the current group finishes, the next ones wait
                        break
                    group = rows[i:i + chunk]
                    pending += [fetch(r) for r in rows[i + chunk * 2:i + chunk * 3]]
                    items, t0 = [], time.perf_counter()
                    for r, fut in zip(group, pending[:len(group)]):
                        self._tick_phase("analyze") if phase else self._tick()
                        self.progress["current"] = r["file_name"]
                        try:
                            items.append((r, fut.result()))
                        except Exception as e:
                            self._failed(r["asset_id"], e, db)
                    del pending[:len(group)]
                    self._timed("download", t0, len(group))
                    if not piped:
                        self._analyze_group(analyzer, items, threshold)
                        continue
                    job = gpu.submit(analyzer.infer_many, self._args(items), self.s.full_image_fallback, self.s.groups) if items else None
                    if previous:
                        finish(analyzer, *previous, threshold)
                    previous = (items, job)
            finally:
                for fut in pending:   # stop (Interrupted): previews not started yet are dropped
                    fut.cancel()
                if previous:
                    finish(analyzer, *previous, threshold)
        return len(rows)

    def _args(self, items: list[tuple]) -> list[tuple]:
        # kind passed as is: _original does not read the database from the model thread.
        return [(data, r["lat"], r["lon"], (lambda aid=r["asset_id"], kind=r["kind"]: self.original(aid, kind))) for r, data in items]

    def _timed(self, stage: str, t0: float, photos: int) -> None:
        acc = self._timings.setdefault(stage, [0.0, 0])
        acc[0] += time.perf_counter() - t0
        acc[1] += photos

    def _finish(self, analyzer, items: list[tuple], job, threshold: float | None) -> None:
        """End of the analysis of a group whose models ran in the "gpu" thread: decision (GBIF) and
        database write. If the group failed, each photo is retried alone."""
        if not items:
            return
        try:
            batch = analyzer.decide_many(job.result(), self.s.groups)
        except Unavailable:
            raise
        except Exception:
            for item in items:
                self._analyze_group(analyzer, [item], threshold)
            return
        self._save(items, batch, threshold)

    def _keep(self, analyzer, items: list[tuple], job, threshold: float | None = None,
              db: sqlite3.Connection | None = None) -> None:
        """End of the AI part of a group (first phase): results stored for the geographic check (decide). If
        the group failed, each photo is run again alone, so that an unreadable photo does not hold the others."""
        if not items:
            return
        try:
            out = job.result()
        except Exception:
            out = None
        db = db or self.db
        t0 = time.perf_counter()
        for k, (r, data) in enumerate(items):
            aid = r["asset_id"]
            try:
                res = out[k] if out is not None else \
                    analyzer.infer_many(self._args([(r, data)]), self.s.full_image_fallback, self.s.groups)[0]
                db.execute("INSERT OR REPLACE INTO inferred VALUES (?, ?, ?)", (aid, analyzer.pack(res), time.time()))
                db.commit()
            except Exception as e:
                self._failed(aid, e, db)
        self._timed("save", t0, len(items))
        self._last_done = self._measure(self._speed, self._last_done, len(items))

    def decide(self, limit: int | None = None, saved: list | None = None) -> int:
        """Second phase of the analysis: geographic check (GBIF), decision and saving of the photos whose AI result
        is stored, without the models (the GPU is free). A photo whose check fails is counted as failed (given up
        after MAX_FAILURES); a GBIF outage (Unavailable) stops the phase, the results wait for the next cycle."""
        where, args = self.queue_sql()
        # AI results of photos that left the queue (deleted, skipped, given up): no longer needed.
        self.db.execute(f"DELETE FROM inferred WHERE asset_id NOT IN (SELECT asset_id FROM asset WHERE {where})", args)
        self.db.commit()
        rows = self.db.execute("SELECT a.asset_id, a.file_name, a.kind, i.data FROM inferred i JOIN asset a ON a.asset_id = i.asset_id "
                               "ORDER BY a.taken_at DESC LIMIT ?", (limit or self.s.batch_size,)).fetchall()
        if self.progress["step"] not in ("geo", "analyze"):   # run_once counts the whole phase, not only this batch
            self._step("geo", len(rows))
        threshold = auto_threshold(self.db, self.s)
        decider = self.decider
        for r in rows:
            self._tick_phase("geo")
            if self.progress["step"] == "geo":   # alongside the AI part, the file shown is the AI one
                self.progress["current"] = r["file_name"]
            try:
                batch = decider.decide_many([decider.unpack(r["data"])], self.s.groups)
            except Exception as e:
                self._failed(r["asset_id"], e)
                continue
            self._save([(r, None)], batch, threshold, geo=True)
            if saved is not None:
                saved.append(r["asset_id"])
        return len(rows)

    def _measure(self, speed: list[float], last: float, n: int) -> float:
        """Actual throughput (time per photo between two groups, pipelined stages included); returns the new mark."""
        now = time.monotonic()
        if last and now - last < 600:
            speed += [(now - last) / n] * n
            del speed[:-50]
        return now

    def _analyze_group(self, analyzer, items: list[tuple], threshold: float | None) -> None:
        """Analyse and save a batch of already downloaded photos. If the batch fails, each photo is retried
        alone: an unreadable photo does not make the others fail."""
        if not items:
            return
        args = self._args(items)
        try:
            fb = self.s.full_image_fallback
            g = self.s.groups
            batch = analyzer.analyze_many(args, fb, g) if len(items) > 1 else [analyzer.analyze(*args[0], fallback=fb, groups=g)]
        except Exception:
            if len(items) == 1:
                self._failed(items[0][0]["asset_id"], sys.exc_info()[1])
                return
            for item in items:
                self._analyze_group(analyzer, [item], threshold)
            return
        self._save(items, batch, threshold)

    def _save(self, items: list[tuple], batch: list, threshold: float | None, geo: bool = False) -> None:
        t0 = time.perf_counter()
        for (r, _), results in zip(items, batch):
            aid = r["asset_id"]
            try:
                new = self._prepare(aid, results, threshold)
                # Re-analysis: replace the automatic detections still pending, never the reviewed ones.
                self.db.execute("DELETE FROM detection WHERE asset_id = ? AND source = 'auto' AND status = 'pending'", (aid,))
                self._insert(new)
                self.db.execute("UPDATE asset SET analyzed_at = ?, analyzer = ?, fail_count = 0, last_error = NULL "
                                "WHERE asset_id = ?", (time.time(), ANALYZER_ID, aid))
                self.db.execute("DELETE FROM inferred WHERE asset_id = ?", (aid,))
                self.db.commit()
            except Exception as e:
                self._failed(aid, e)
        self._timed("save", t0, len(items))
        if geo:   # second phase: its own speed (the AI part is measured by _keep)
            self._last_done_geo = self._measure(self._speed_geo, self._last_done_geo, len(items))
        else:
            self._last_done = self._measure(self._speed, self._last_done, len(items))

    def _failed(self, aid: str, e: BaseException, db: sqlite3.Connection | None = None) -> None:
        """Failed photo: counted (given up after MAX_FAILURES), or hidden if it disappeared from Immich.
        db: connection of the calling thread (AI part in its own thread, see _both_phases)."""
        db = db or self.db
        db.rollback()
        if is_missing(e):
            self.mark_missing(aid, db)
            return
        if is_transient(e):
            # Network, Immich or GBIF temporarily unavailable (429: too many requests): the photo is not
            # to blame, it is not counted as failed. The analysis stops and resumes at the next cycle.
            raise Unavailable(f"{type(e).__name__}: {e}"[:300]) from e
        log.error("analysis failed for %s", aid, exc_info=e)
        db.execute("UPDATE asset SET fail_count = fail_count + 1, last_error = ? WHERE asset_id = ?",
                   (f"{type(e).__name__}: {e}"[:500], aid))
        db.commit()

    def apply_skips(self) -> int:
        """Link RAW files to their JPG, then remove the automatic detections still pending from skipped
        photos (RAW pair, people if SKIP_PEOPLE), analysed before the rule applied.
        Reviewed detections stay. These photos go back to "to analyse": if the rule is lifted
        (SKIP_PEOPLE disabled, JPG deleted), they are analysed."""
        store.link_raw_twins(self.db)
        skipped = f"SELECT asset_id FROM asset WHERE NOT ({SKIPPED_NOT})"
        args = self.s.skip_args
        n = self.db.execute(f"DELETE FROM detection WHERE source = 'auto' AND status = 'pending' AND asset_id IN ({skipped}) "
                            "AND asset_id IN (SELECT asset_id FROM asset WHERE kind IS NOT 'VIDEO')", args).rowcount
        if not self.s.full_image_fallback:   # whole image classified with nothing detected: removed if the setting excludes it
            n += self.db.execute("DELETE FROM detection WHERE source = 'auto' AND status = 'pending' AND detector = 'image'").rowcount
        n += drop_unwanted(self.db, self.s)
        self.db.execute(f"UPDATE asset SET analyzed_at = NULL WHERE analyzed_at IS NOT NULL AND kind IS NOT 'VIDEO' "
                        f"AND asset_id IN ({skipped}) AND NOT EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = asset.asset_id)", args)
        self.db.commit()
        if n:
            log.info("%d pending detection(s) removed: skipped photos (people, RAW paired with a JPG), whole "
                     "image without a detected subject, group not searched or manual detection", n)
        return n

    def prune(self) -> int:
        """Candidate filters applied to pending detections, if the settings changed."""
        key = json.dumps([self.s.exclude_extinct, self.s.hide_absent])
        if store.get_state(self.db, "pruned_with") == key:
            return 0
        self._step("prune")
        n = prune_pending(self.db, self.tx, self.s, checkpoint=self._checkpoint)
        store.set_state(self.db, "pruned_with", key)
        return n

    def start_prune(self) -> bool:
        """prune() in the background (after a settings change); False if a cycle is already running:
        it will be applied at the end of that cycle or the next one."""
        if not self.cycle_lock.acquire(blocking=False):
            return False
        def run():
            try:
                self._cycle_manual = False   # stopped by a setting: the next cycle runs prune() again
                self.progress.update(running=True, started=time.time(), finished=None, error=None, manual=True)
                log.info("settings applied to %d pending detection(s)", self.prune())
            except Interrupted:
                log.info("applying settings interrupted: resumed at the next cycle")
            except Exception:
                self.db.rollback()   # no transaction left open (see run_once)
                log.exception("applying settings failed")
            finally:
                self.progress.update(running=False, step=None, finished=time.time())
                self.cycle_lock.release()
        threading.Thread(target=run, daemon=True).start()
        return True

    def reanalyze(self, asset_id: str) -> int:
        """Immediate re-analysis of a photo. Like putting it back in the queue: reviewed or drawn detections stay,
        only the proposals awaiting review are replaced. Nothing is erased until the analysis has succeeded."""
        if not self.cycle_lock.acquire(timeout=5):
            raise Busy("a synchronisation cycle is running")
        self._cycle_manual = False
        try:
            r = self.db.execute("SELECT lat, lon FROM asset WHERE asset_id = ?", (asset_id,)).fetchone()
            if not r:
                raise KeyError(asset_id)
            results = self._analyze_asset(asset_id, r["lat"], r["lon"])
            rows = self._prepare(asset_id, results, auto_threshold(self.db, self.s))
            try:
                self.db.execute("DELETE FROM detection WHERE asset_id = ? AND status = 'pending' AND source != 'manual'", (asset_id,))
                self.db.execute("DELETE FROM inferred WHERE asset_id = ?", (asset_id,))
                n = self._insert(rows)
                self.db.execute("UPDATE asset SET analyzed_at = ?, analyzer = ?, fail_count = 0, last_error = NULL "
                                "WHERE asset_id = ?", (time.time(), ANALYZER_ID, asset_id))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            return n
        finally:
            self.cycle_lock.release()

    def requeue(self, scope: str) -> int:
        return requeue(self.db, scope)

    def undo_summary(self, db) -> dict:
        """What the removal would touch (traces in the database, without calling Immich) and how far it got."""
        q = lambda sql: db.execute(sql).fetchone()[0]
        return {"photos": q("SELECT count(DISTINCT asset_id) FROM written WHERE kind IN ('tag_asset', 'description')"),
                "tags": q("SELECT count(*) FROM written WHERE kind = 'tag'"),
                "descriptions": q("SELECT count(*) FROM written WHERE kind = 'description'"),
                "paused": self.writes_paused(db), "dry_run": self.s.dry_run, "source": self.s.source, **self.undo}

    def writes_paused(self, db=None) -> bool:
        return store.get_state(db or self.db, store.WRITES_PAUSED) == "1"

    def resume_writes(self) -> None:
        with self._idle():
            store.set_state(self.db, store.WRITES_PAUSED, "0")
            self.db.commit()
        log.info("writes resumed: confirmed photos will be written again at the next cycles")

    def start_uninstall(self, total: int) -> None:
        """Removal in the background: the running cycle is stopped first (_idle), one write at a time."""
        self.undo = {"state": "running", "done": 0, "total": total, "stats": None, "error": None, "remaining": 0}

        def tick():
            self._checkpoint()
            self.undo["done"] = min(self.undo["done"] + 1, total)
            self.last_activity = time.time()

        def run():
            log.warning("removal requested: %d photo(s) to clean up in Immich", total)
            try:
                with self._idle():
                    syncer = Syncer(self.db, self.client, self.s.tag_root, self.s.names_lang, self.s.write_description,
                                    write_gap=getattr(self.client, "write_gap", WRITE_GAP), checkpoint=tick)
                    stats = syncer.uninstall()
                self.undo.update(state="done", stats=asdict(stats), remaining=syncer.remaining)
                log.warning("removal finished: %s, remaining photos: %d", stats, syncer.remaining)
            except Interrupted:
                self.undo.update(state="interrupted")
                log.warning("removal interrupted: running it again resumes where it stopped")
            except Exception as e:
                self.undo.update(state="failed", error=str(e)[:300])
                log.exception("removal failed: running it again resumes where it stopped")

        threading.Thread(target=run, daemon=True, name="removal").start()

    def sync(self):
        if self.writes_paused():
            log.info("writes suspended after a removal: nothing is written")
            return None
        if not self.client.writable:
            log.warning("Immich server not validated: synchronisation suspended (read-only)")
            return None
        last_full = float(store.get_state(self.db, "last_full_sync") or 0)
        full = time.time() - last_full >= DAILY
        syncer = Syncer(self.db, self.client, self.s.tag_root, self.s.names_lang, self.s.write_description,
                        write_gap=getattr(self.client, "write_gap", WRITE_GAP), checkpoint=self._checkpoint,
                        progress=self._sync_progress)
        try:
            stats = syncer.sync_all(full=full)
        finally:   # even after a stop (Interrupted)
            for aid in syncer.missing:
                self.mark_missing(aid)
        if full and not self.client.dry_run:
            store.set_state(self.db, "last_full_sync", str(time.time()))
        return stats

    def _sync_progress(self, done: int, total: int, phases: int) -> None:
        """Progress of the tag writing (Syncer): done and total in work units, phases units per photo (read,
        tags, description); the dashboard shows photos and computes the remaining time on the units."""
        self.progress.update(done=done, total=total, scale=phases)

    def ensure_quality(self, asset_id: str) -> float:
        """Image quality (colours, sharpness), computed once on the thumbnail then kept in the database."""
        row = self.db.execute("SELECT quality FROM image_quality WHERE asset_id = ?", (asset_id,)).fetchone()
        if row:
            return row["quality"]
        q = image_quality(Image.open(io.BytesIO(self.client.thumbnail(asset_id, "thumbnail"))))
        self.db.execute("INSERT OR REPLACE INTO image_quality VALUES (?, ?)", (asset_id, q))
        self.db.commit()
        return q

    def prefetch(self, candidates: int = 3, limit: int = 400) -> int:
        """Store locally everything the dashboard shows: taxon record (names, links), range map
        and Wikipedia article, for confirmed taxa and the top candidates of detections awaiting review,
        and quality of confirmed photos (choice of cover photos).
        This way the viewer, species pages and maps no longer wait for the network."""
        wanted: dict[int, str] = {}
        for r in self.db.execute("SELECT DISTINCT taxon_key FROM detection WHERE status = 'confirmed' AND taxon_key IS NOT NULL"):
            t = store.load_taxon(self.db, r["taxon_key"])
            wanted[r["taxon_key"]] = t["scientific_name"] if t else ""
        for r in self.db.execute("SELECT candidates FROM detection WHERE status = 'pending'"):
            for c in json.loads(r["candidates"] or "[]")[:candidates]:
                if c.get("gbif_key"):
                    wanted.setdefault(c["gbif_key"], c.get("name", ""))
        todo = [(k, n) for k, n in wanted.items()
                if ranges.cached(self.db, k) is None or not (store.load_taxon(self.db, k) or {}).get("v", 0) >= TAXON_VERSION][:limit]
        no_quality = [r["asset_id"] for r in self.db.execute(
            "SELECT DISTINCT d.asset_id FROM detection d JOIN asset a ON a.asset_id = d.asset_id "
            "LEFT JOIN image_quality q ON q.asset_id = d.asset_id "
            "WHERE d.status = 'confirmed' AND q.asset_id IS NULL AND a.missing_at IS NULL LIMIT ?", (limit,))]
        self._step("prefetch", len(todo) + len(no_quality))
        for key, name in todo:
            self._tick()
            try:
                t = store.load_taxon(self.db, key)
                if not t or t.get("v", 0) < TAXON_VERSION:
                    m = self.tx.match(name) if name else None
                    if m and m.gbif_key == key:
                        store.save_taxon(self.db, key, to_json(m))
                        self.db.commit()
                        t = store.load_taxon(self.db, key)
                if ranges.cached(self.db, key) is None:
                    ranges.fetch(self.tx, key)
                url = (t or {}).get("wikipedia", {}).get(self.s.names_lang)
                if url:
                    wiki.article(self.tx, url)   # mis en cache (7 jours)
            except httpx.TransportError as e:   # network down: one line, and the rest waits for the next cycle
                log.warning("local data incomplete for %s, resumed at the next cycle: %s", key, str(e) or type(e).__name__)
                break
            except httpx.HTTPError as e:
                log.warning("local data incomplete for %s: %s", key, e)
            except Exception:
                log.warning("local data incomplete for %s", key, exc_info=True)
        for aid in no_quality:
            self._tick()
            try:
                self.ensure_quality(aid)
            except Exception:
                log.debug("quality unavailable for %s", aid, exc_info=True)
        return len(todo)

    def run_once(self, manual: bool = False) -> dict:
        """A full cycle. Manual ("Sync" button): also analyses outside the time window.
        The analysis runs batch after batch until the queue is empty, within ANALYSIS_MAX_MINUTES; if any
        remain, report['more'] says so and the loop starts another cycle without waiting."""
        if not self.configured:
            return {"configured": False}
        if not self.cycle_lock.acquire(blocking=False):
            return {"busy": True}
        self._cycle_manual = manual
        self.progress.update(running=True, started=time.time(), finished=None, error=None, manual=manual, phases=None)
        report: dict = {"at": time.time(), "manual": manual, "dry_run": self.s.dry_run}
        try:
            self._step("check")
            report["writable"] = self.check()
            self._step("scan")
            report["scanned"] = self.scan()
            report["reconciled"] = self.reconcile()
            report["skipped"] = self.apply_skips()
            self._soft(report, "taxa_refreshed", self.refresh_taxa)
            self._soft(report, "places_refreshed", self.refresh_places)
            report["analyzed"] = report["inferred"] = 0
            allowed = lambda: manual or in_window(self.s.analysis_window)
            if self._analyzer is None or isinstance(self._analyzer, Analyzer):
                # Real analyser: the AI part and the geographic check at the same time (see _both_phases).
                self._both_phases(report, allowed)
            else:
                # Analyser provided from outside (tests): in one go, batch after batch.
                if allowed() and not self.analysis_paused and self.pending_analysis():
                    self._step("analyze", self.pending_analysis())
                deadline = time.monotonic() + self.s.analysis_max_minutes * 60
                while allowed() and not self.analysis_paused and self.pending_analysis():
                    if time.monotonic() >= deadline:
                        report["more"] = True
                        break
                    try:
                        n = self.analyze()
                    except Unavailable as e:
                        log.warning("analysis suspended until the next cycle: %s", e)
                        report["paused"] = str(e)
                        break
                    report["analyzed"] += n
                    if not n:
                        break
            if self.analysis_paused:
                # Pause in the middle of a group: the group held the models, GPU memory is only released here.
                self.unload_models()
            self._soft(report, "pruned", self.prune)
            # Series (bursts) and smoothing of the proposals awaiting review within a series (series.py).
            report["series"] = series.assign(self.db)
            report["smoothed"] = series.smooth(self.db, lambda k: store.load_taxon(self.db, k))
            self._step("sync")
            self._soft(report, "sync", lambda: asdict(stats) if (stats := self.sync()) else None)
            self._soft(report, "prefetched", self.prefetch)
            try:
                self.backup()
            except Exception:   # disk full, permissions: reported, without blocking the cycle
                log.exception("database backup failed")
            report["failed"] = self.db.execute("SELECT count(*) FROM asset WHERE fail_count >= ? AND missing_at IS NULL",
                                               (MAX_FAILURES,)).fetchone()[0]
            store.set_state(self.db, "last_run", json.dumps(report))
            if report.get("errors"):
                self.progress["error"] = " ; ".join(f"{k} : {v}" for k, v in report["errors"].items())
            return report
        except Interrupted:
            # Stop requested by a dashboard setting (_idle): nothing half written is kept, the cycle restarts
            # right after the change.
            self.db.rollback()
            report["interrupted"] = True
            log.info("cycle stopped to apply a dashboard setting")
            return report
        except Exception as e:
            # Cycle writes not committed yet (scan cut short by an Immich outage…): rolled back, otherwise the
            # transaction would stay open and block the dashboard writes until the next cycle.
            self.db.rollback()
            self.progress["error"] = str(e)
            raise
        finally:
            self.progress.update(running=False, step=None, finished=time.time(), report=report)
            self.cycle_lock.release()

    def _both_phases(self, report: dict, allowed) -> None:
        """Analysis in two phases running at the same time, each with its bar. The AI part ("ai" thread, its own
        database connection) goes through the queue at full GPU speed and stores its results; this thread checks
        them against GBIF as they arrive and saves the photos. When the AI part has nothing left, the models leave
        the GPU and the check finishes on its own. Both stop at ANALYSIS_MAX_MINUTES (the loop starts the next
        cycle right away, models still loaded if the queue is not empty)."""
        ai_n = self.pending_ai() if allowed() and not self.analysis_paused else 0
        geo_n = self.pending_geo() if allowed() else 0
        if not ai_n and not geo_n:
            return
        if ai_n and self._analyzer is None:
            # First launch: models downloaded (~5 GB) then loaded, possibly several minutes.
            self._step("models")
            self._wait_models()
        deadline = time.monotonic() + self.s.analysis_max_minutes * 60
        self._step("analyze" if ai_n else "geo", ai_n or geo_n)
        now = time.time()   # [done, total, start] per phase: two bars, each with its remaining time
        self.progress["phases"] = {"analyze": [0, ai_n, now], "geo": [0, ai_n + geo_n, now]}
        self.progress["active"] = ["analyze", "geo"] if ai_n else ["geo"]
        ai: dict = {"n": 0, "error": None}

        def run_ai():
            db = store.connect(self.s.db_path)
            try:
                while not self.analysis_paused and allowed() and time.monotonic() < deadline:
                    n = self.analyze(infer_only=True, db=db)
                    ai["n"] += n
                    if not n:
                        break
            except Interrupted:   # setting from the dashboard: this thread stops, run_once handles it
                pass
            except BaseException as e:
                ai["error"] = e
            finally:
                db.close()

        thread = threading.Thread(target=run_ai, daemon=True, name="ai") if ai_n else None
        if thread:
            thread.start()
        # Tags of the photos confirmed by the check, written as they come (not during a removal, nor read-only).
        tags = _TagWriter(self) if not self.writes_paused() and self.client.writable else None
        try:
            while True:
                if thread and not thread.is_alive():   # AI part over: GPU given back, the check goes on alone
                    thread = None
                    self.progress["active"] = ["geo"]
                    if not self.pending_ai() or self.analysis_paused:
                        self.unload_models()
                    self._step("geo", self.pending_geo())
                if time.monotonic() >= deadline:
                    report["more"] = True
                    break
                if not allowed():   # end of the analysis window: the AI thread stops too
                    break
                saved: list[str] = []
                try:
                    n = self.decide(saved=saved)
                except Unavailable as e:   # GBIF down: the AI part goes on, its results wait for the next cycle
                    log.warning("geographic check suspended until the next cycle: %s", e)
                    report["paused"] = str(e)
                    break
                report["analyzed"] += n
                if tags and saved:
                    tags.add(saved)
                if not n:
                    if thread is None:
                        break
                    self._checkpoint()
                    thread.join(0.5)   # waiting for AI results
            while thread and thread.is_alive():   # check stopped first: the AI part ends on its own
                self._checkpoint()
                thread.join(1)
        finally:
            if thread:   # stop requested (Interrupted): the thread stops at its next photo
                thread.join()
            if tags:   # the tags still waiting are written before the end of the analysis
                tags.finish()
                report["tags_written"] = tags.done
            report["inferred"] = ai["n"]
            self.progress["active"] = None
        if not self.pending_ai() or self.analysis_paused:
            self.unload_models()
        if isinstance(ai["error"], Unavailable):
            log.warning("analysis suspended until the next cycle: %s", ai["error"])
            report["paused"] = str(ai["error"])
        elif ai["error"] is not None:
            raise ai["error"]

    @property
    def models_loaded(self) -> bool:
        return self._analyzer is not None

    def _soft(self, report: dict, key: str, step) -> None:
        """Cycle stage whose failure (GBIF, Immich) does not deprive the cycle of the following ones: reported in the report,
        retried at the next cycle. Only its own writes are rolled back, the work already done is committed before."""
        self.db.commit()
        try:
            report[key] = step()
        except Exception as e:
            self.db.rollback()
            log.exception("cycle stage %s failed", key)
            report.setdefault("errors", {})[key] = str(e)

    def _wait_models(self) -> None:
        """Model loading (download included) in a separate thread, which the cycle waits for while staying
        interruptible: a dashboard setting does not wait for the end of a multi-GB download. A stop
        does not interrupt the loading, which is picked up as is at the next cycle."""
        if self._loading is None or not self._loading.is_alive():
            self._load_error = None

            def load():
                try:
                    self.analyzer
                    if self.analysis_paused:   # paused during loading: nothing keeps the models in memory
                        self.unload_models()
                except BaseException as e:
                    self._load_error = e

            self._loading = threading.Thread(target=load, daemon=True, name="models")
            self._loading.start()
        while self._loading.is_alive() and not self.analysis_paused:   # pause: the rest of the cycle does not wait
            self._checkpoint()
            self._loading.join(1)
        if self._load_error is not None:
            raise self._load_error

    def pause_analysis(self, paused: bool) -> None:
        """Analysis pause/resume button. Pause: the current group of photos finishes, then the models leave
        memory (GPU as well as CPU). Resume: the analysis restarts right away, even outside the time window,
        as with "Sync". The rest of the cycle (new photos, tags) goes on during the pause."""
        self.analysis_paused = paused
        if paused:
            self.unload_models()
        elif not self.start_manual():   # cycle running: the loop starts a manual one as soon as it ends
            self._resume_manual = self._resume_pending = True
            self._wake.set()

    def unload_models(self) -> None:
        with self._analyzer_lock:
            if self._analyzer is None or self._device is None:   # analyser provided from outside (tests)
                return
            device, self._stages_kept = self._device, {**self._stages_kept, **getattr(self._analyzer, "timings", {})}
            self._analyzer = None
        hardware.release(device)
        log.info("analysis paused: models removed from memory")

    def start_manual(self) -> bool:
        """Start a full cycle in the background; False if a cycle is already running."""
        if self.cycle_lock.locked():
            return False
        def run():
            try:
                log.info("synchronisation manuelle : %s", self.run_once(manual=True))
            except Exception:
                log.exception("manual synchronisation failed")
        threading.Thread(target=run, daemon=True, name="sync-manuelle").start()
        return True

    def install_backbone(self) -> None:
        """Local GBIF taxonomy: downloaded and built if it is missing or no longer matches the API
        version (in the background; meanwhile, the API is used as before)."""
        if not self.s.gbif_local:
            return
        try:
            backbone.install(self.s.data_dir, self.s.user_agent, lambda state, version: self.backbone_state.update(state=state, version=version))
        except Exception as e:
            self.backbone_state.update(state="error", error=str(e)[:200])
            log.exception("local GBIF taxonomy unavailable, the GBIF API is still used")

    def loop(self) -> None:
        self.loop_started = True
        threading.Thread(target=self.install_backbone, daemon=True, name="taxonomie-gbif").start()
        threading.Thread(target=self._watch_idle_models, daemon=True, name="modeles-inactifs").start()
        while True:
            self.last_activity = time.time()
            more = False
            self._wake.clear()   # before the cycle: a wake-up requested during it starts the next one right away
            try:
                manual, self._resume_manual = self._resume_manual, False
                resume, self._resume_pending = self._resume_pending, False
                report = self.run_once(manual=manual)
                # Resuming after a setting, but another setting still holds the cycle: retry soon,
                # keeping manual mode.
                retry = bool(report.get("busy")) and resume
                if retry:
                    self._resume_manual, self._resume_pending = self._resume_manual or manual, True
                # No Immich connection yet: check again soon, to start as soon as it is entered.
                more = bool(report.get("more")) or report.get("configured") is False or retry
                if report.get("configured") is not False and not report.get("busy"):
                    log.info("cycle : %s", report)
            except Exception:
                log.exception("cycle failed")
            # Wait interrupted by _idle: cycle stopped for a setting, restarted right away.
            self._wake.wait(5 if more else self.s.interval_minutes * 60)
