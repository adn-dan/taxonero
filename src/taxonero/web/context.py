"""Context shared by the dashboard routes: settings, worker, database (one connection per thread), taxonomy,
authentication, and the common conversions (taxon, detection, photo)."""

import copy
import json
import logging
import secrets
import threading
import time
from types import SimpleNamespace

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from .. import store
from ..autoconfirm import auto_blockers, auto_threshold, blocked_groups, group_of
from ..config import Settings
from ..curation import candidate_filter
from ..taxonomy import TAXON_VERSION, Taxonomy, to_json
from ..worker import Worker

log = logging.getLogger(__name__)

# Wrong passwords tolerated per IP address over AUTH_WINDOW seconds (see auth).
AUTH_MAX_FAILURES = 10
AUTH_WINDOW = 60


def fail(status: int, code: str, message: str, **params):
    """Error shown in the dashboard: a code (translated in the browser), its values, and an English text
    as a fallback."""
    raise HTTPException(status, {"code": code, "message": message, **params})


class BoxIn(BaseModel):
    x: float
    y: float
    w: float
    h: float


class TaxonIn(BaseModel):
    gbif_key: int | None = None
    name: str | None = None


class _ThreadLocalDB:
    """Behaves like a sqlite3.Connection, but opens one per thread."""

    def __init__(self, path):
        self._path, self._local = path, threading.local()

    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = store.connect(self._path)
        return c

    def __getattr__(self, name):
        return getattr(self._conn(), name)


def make_context(s: Settings, w: Worker, desktop: bool) -> SimpleNamespace:
    """Objects and functions shared by the route modules (attributes of the returned context)."""
    # One SQLite connection per thread: FastAPI serves requests in parallel in a thread pool,
    # and a connection shared between simultaneous threads raises "bad parameter or other API misuse".
    db = _ThreadLocalDB(s.db_path)
    # Taxonomy specific to the web (its database is thread-local), separate from the worker's.
    tx = Taxonomy(db, langs=s.langs, user_agent=s.user_agent, backbone=s.backbone_path if s.gbif_local else None, fail_fast=True)
    drop = candidate_filter(s, tx)
    lock = threading.Lock()
    range_lock = threading.Lock()  # one range map at a time: avoids running the 16 requests twice
    security = HTTPBasic(auto_error=False)
    # URL of the "open in Immich" links: may change when the connection is set from the dashboard.
    public_immich = lambda: (s.immich_public_url or s.immich_url).rstrip("/")
    # Link to the photo in its source (Immich); none for a folder or a photo application.
    source_link = lambda aid: f"{public_immich()}/photos/{aid}" if s.source == "immich" else None

    # Against brute force: wrong passwords per IP address, over a sliding window. Beyond the limit,
    # every attempt from this address is refused without being checked (even a right one) until the window ends.
    failures: dict[str, list[float]] = {}
    failures_lock = threading.Lock()

    def auth(request: Request, creds: HTTPBasicCredentials | None = Depends(security)) -> None:
        if s.dashboard_no_auth or desktop:   # desktop: session token checked by desktop_session
            return
        if not s.dashboard_password:
            raise HTTPException(503, "DASHBOARD_PASSWORD not set (or DASHBOARD_NO_AUTH=true)")
        ip, now = request.client.host if request.client else "", time.monotonic()
        with failures_lock:
            recent = [t for t in failures.get(ip, []) if now - t < AUTH_WINDOW]
            if recent:
                failures[ip] = recent
            else:
                failures.pop(ip, None)
        if len(recent) >= AUTH_MAX_FAILURES:
            raise HTTPException(429, "too many password attempts: try again in a minute",
                                headers={"Retry-After": str(int(AUTH_WINDOW - (now - recent[0])) + 1)})
        # As bytes: compare_digest refuses non-ASCII strings (TypeError, hence a 500 error).
        if not creds or not (secrets.compare_digest(creds.username.encode(), b"nature")
                             and secrets.compare_digest(creds.password.encode(), s.dashboard_password.encode())):
            if creds:   # without credentials: first browser request, before the login window
                with failures_lock:
                    failures.setdefault(ip, []).append(now)
            raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="taxonero"'})

    def taxon(key: int | None) -> dict | None:
        t = store.load_taxon(db, key) if key else None
        if t and t.get("v", 0) < TAXON_VERSION:
            # Old format (without multilingual names or Wikispecies): enriched again on the fly.
            fresh = tx.match(t["scientific_name"], kingdom=t.get("lineage", {}).get("kingdom"))
            if fresh and fresh.gbif_key == key:
                store.save_taxon(db, key, to_json(fresh))
                db.commit()
                t = store.load_taxon(db, key)
        return t

    blocked_cache = {"at": 0.0, "groups": set()}

    def blocked_now() -> set[str]:
        """Groups excluded from auto-confirmation, recomputed at most every 10 s (photo lists)."""
        if time.monotonic() - blocked_cache["at"] > 10:
            blocked_cache.update(at=time.monotonic(), groups=blocked_groups(db, s))
        return blocked_cache["groups"]

    def det_json(r, threshold: float | None = None) -> dict:
        d = dict(r)
        d["candidates"] = json.loads(d["candidates"] or "[]")
        d["candidates_out"] = json.loads(d.get("candidates_out") or "[]")
        d["taxon"] = taxon(d["taxon_key"])
        # Proposal corrected by the series (smoothing, series.py): what the model proposed at first.
        if d.get("smoothed") and d["status"] == "pending" and d.get("model_key") and d["model_key"] != d["taxon_key"]:
            d["model_taxon"] = taxon(d["model_key"])
        if d["status"] == "pending":
            # Why it was not auto-confirmed (shown in the review card).
            runner_up = d["candidates"][1]["p"] if len(d["candidates"]) > 1 else 0.0
            name = (d["taxon"] or {}).get("scientific_name")
            group = group_of((d["taxon"] or {}).get("lineage"))
            d["why_pending"] = ([{"code": "audit"}] if d.get("audit") else
                                [] if d["source"] == "manual" else
                                auto_blockers(d["taxon_rank"], d["plausibility"], d["score"], name, runner_up,
                                              d["box_label"], threshold, s.auto_confirm_max_runner_up, group,
                                              blocked_now() if threshold is not None else set(), d.get("frame_support"))
                                or [{"code": "reset_by_hand" if d["reviewed_at"] else "before_rule"}])
        return d

    def ensure_taxon(key: int | None, name: str | None, kingdom: str | None = None) -> int | None:
        if key and taxon(key):
            return key
        try:
            t = tx.match(name, kingdom=kingdom) if name else None
            if t is None and key:
                sp = tx.get(f"https://api.gbif.org/v1/species/{key}")
                t = tx.match(sp.get("canonicalName") or sp["scientificName"])
        except httpx.HTTPError as e:   # record fetched later: the current action must not fail because of it
            log.warning("record of taxon %s not fetched: %s", key or name, e)
            return None
        if t is None:
            return None
        store.save_taxon(db, t.gbif_key, to_json(t))
        db.commit()
        return t.gbif_key

    # The worker has its own SQLite connection, used by the cycle in another thread: the web does not touch it (a commit
    # made here would commit a half-done transaction of the cycle). Its methods are called with the web database.
    def analyzer():
        """Worker analyser (shared models) whose decision (GBIF, candidate filter) goes through the web database."""
        a = copy.copy(w.analyzer)
        a.taxonomy, a.drop, a.tolerant = tx, drop, True
        return a

    def original(asset_id: str) -> bytes | None:
        """Original of the photo (Worker.original), the asset type read from the web database."""
        row = db.execute("SELECT kind FROM asset WHERE asset_id = ?", (asset_id,)).fetchone()
        return w.original(asset_id, (row and row["kind"]) or "IMAGE")

    def asset_json(asset_id: str, a: dict | None = None, threshold: float | None = None) -> dict:
        a = a or dict(db.execute("SELECT * FROM asset WHERE asset_id = ?", (asset_id,)).fetchone() or {})
        if not a:
            raise HTTPException(404)
        if threshold is None:
            threshold = auto_threshold(db, s)
        a["detections"] = [det_json(r, threshold) for r in db.execute(
            "SELECT * FROM detection WHERE asset_id = ? AND status != 'deleted' ORDER BY status = 'rejected', w * h DESC", (asset_id,))]
        a["immich_link"] = source_link(asset_id)
        return a

    def set_status(det_id: int, status: str, key: int | None = None, rank: str | None = None):
        row = db.execute("SELECT * FROM detection WHERE id = ?", (det_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        db.execute("UPDATE detection SET status = ?, confirmed_by = ?, reviewed_at = ?, "
                   "taxon_key = COALESCE(?, taxon_key), taxon_rank = COALESCE(?, taxon_rank) WHERE id = ?",
                   (status, None if status == "pending" else "user", time.time(), key, rank, det_id))
        db.commit()
        return det_json(db.execute("SELECT * FROM detection WHERE id = ?", (det_id,)).fetchone())

    return SimpleNamespace(s=s, w=w, db=db, tx=tx, drop=drop, lock=lock, range_lock=range_lock, auth=auth,
                           public_immich=public_immich, source_link=source_link, taxon=taxon, blocked_now=blocked_now,
                           det_json=det_json, ensure_taxon=ensure_taxon, asset_json=asset_json, set_status=set_status,
                           analyzer=analyzer, original=original)
