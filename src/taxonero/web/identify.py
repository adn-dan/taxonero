"""Routes of the Identify page (uploaded image, nothing is kept) and of the analysis (synchronisation, retry of
failed photos)."""

import json
import threading
import time

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import store
from ..curation import EXCLUDED, cand_json
from ..pipeline import MAX_SIDE, exif_latlon, open_image
from ..worker import models_on_disk, requeue
from .context import fail

# Public identification page (Settings > General, /identify): open to anyone, hence bounded. Lighter images than in the
# dashboard, a number of images per address over a sliding window, and few requests at once (each one holds its image
# in memory while it waits for the models, which take one image at a time).
PUBLIC_MAX_BYTES = 25 * 2 ** 20
PUBLIC_WINDOW = 600
PUBLIC_PER_ADDRESS = 20
PUBLIC_AT_ONCE = 3


def register(app, c) -> None:
    w, db, auth, lock, taxon, ensure_taxon, analyzer = c.w, c.db, c.auth, c.lock, c.taxon, c.ensure_taxon, c.analyzer

    IDENTIFY_MAX_BYTES = 60 * 2 ** 20

    def identify_image(data: bytes, place: tuple[float, float] | None = None, public: bool = False) -> dict:
        try:
            image = open_image(data, MAX_SIDE)   # a 50 Mpx photo is never decoded in full
        except Exception:
            fail(400, "bad_image", "unreadable image (formats: JPEG, PNG, WebP, TIFF)")
        lat, lon = exif_latlon(data)
        if lat is None and place:   # a GPS from the photo is never replaced
            lat, lon = place
        with lock:
            results = analyzer().analyze(image, lat, lon)
        out = []
        for a in results:
            d = a.decision
            if d.name in EXCLUDED:
                continue
            cands = [cand_json(c) for c in d.candidates[:5] if c.name not in EXCLUDED]
            if d.gbif_key:
                ensure_taxon(d.gbif_key, d.name)
            out.append({"x": a.box.x, "y": a.box.y, "w": a.box.w, "h": a.box.h, "box_label": a.box.label,
                        "taxon_key": d.gbif_key, "taxon_rank": d.rank, "name": d.name, "score": d.confidence, "plausibility": d.plausibility,
                        "domestic": a.domestic, "candidates": cands, "taxon": taxon(d.gbif_key)})
        if public:
            # Nothing about the collection. Names of the candidates already known here (the public page cannot ask
            # for them one by one as the dashboard does).
            for o in out:
                for cand in o["candidates"]:
                    known = store.load_taxon(db, cand["gbif_key"]) if cand.get("gbif_key") else None
                    cand["vernacular"] = (known or {}).get("vernacular", {})
            return {"lat": lat, "lon": lon, "detections": out}
        # Photos already in the collection for the kept taxon and the candidates: link to the species page.
        keys = {k for o in out for k in [o["taxon_key"], *(c.get("gbif_key") for c in o["candidates"])] if k}
        mine = {r["taxon_key"]: r["n"] for r in db.execute(
            "SELECT d.taxon_key, count(DISTINCT d.asset_id) AS n FROM detection d JOIN asset a ON a.asset_id = d.asset_id "
            f"WHERE d.status = 'confirmed' AND a.missing_at IS NULL AND d.taxon_key IN ({','.join('?' * len(keys))}) "
            "GROUP BY d.taxon_key", list(keys))} if keys else {}
        return {"lat": lat, "lon": lon, "detections": out, "mine": mine}

    async def read_image(request: Request, max_bytes: int) -> bytes:
        """Image of the request body, refused beyond max_bytes."""
        too_big = lambda: fail(413, "image_too_big", "image too large", max=max_bytes // 2 ** 20)
        size = request.headers.get("content-length", "")
        if size.isdigit() and int(size) > max_bytes:
            too_big()
        # Read in chunks, capped: an upload without Content-Length (or a lying one) is not kept in memory in full.
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > max_bytes:
                too_big()
        if not buf:
            fail(400, "empty_image", "no image")
        return bytes(buf)

    @app.post("/api/identify", dependencies=[Depends(auth)])
    async def identify_upload(request: Request, lat: float | None = None, lon: float | None = None):
        """Identify the image sent in the request body. It stays in memory for the duration of the analysis:
        nothing is written (no database, no file, no Immich). GPS coordinates from the EXIF used if present, otherwise
        lat and lon if given."""
        if (lat is None) != (lon is None) or (lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180)):
            fail(400, "bad_location", "latitude or longitude out of range")
        data = await read_image(request, IDENTIFY_MAX_BYTES)
        return await run_in_threadpool(identify_image, data, (lat, lon) if lat is not None else None)

    # -- public identification page (no password; only while the setting is on) ---------------------------------

    public_hits: dict[str, list[float]] = {}   # address -> times of its recent images
    public_lock = threading.Lock()
    public_slots = threading.BoundedSemaphore(PUBLIC_AT_ONCE)

    def public_open() -> None:
        if not c.public_on():
            raise HTTPException(404)

    @app.get("/api/public/info", dependencies=[Depends(public_open)])
    def public_info():
        """What the public page needs before sending an image: the size accepted, and whether the models are in memory
        (otherwise the first answer takes longer, and the page says so)."""
        return {"max_mb": PUBLIC_MAX_BYTES // 2 ** 20, "models_loaded": w.models_loaded, "models_on_disk": models_on_disk(w.s)}

    @app.post("/api/public/identify", dependencies=[Depends(public_open)])
    async def public_identify(request: Request):
        """Same identification as the dashboard page, for anyone: the image stays in memory for the duration of the
        analysis and nothing is written. The answer says what is recognised (names, candidates, public links), never
        anything about the photos of the collection."""
        ip, now = request.client.host if request.client else "", time.monotonic()
        with public_lock:
            for k in [k for k, v in public_hits.items() if now - v[-1] >= PUBLIC_WINDOW]:   # addresses gone quiet
                del public_hits[k]
            recent = [x for x in public_hits.get(ip, []) if now - x < PUBLIC_WINDOW]
            if len(recent) >= PUBLIC_PER_ADDRESS:
                wait = int(PUBLIC_WINDOW - (now - recent[0])) + 1
                raise HTTPException(429, {"code": "too_many_requests", "message": "too many images: try again in a few minutes"},
                                    headers={"Retry-After": str(wait)})
            public_hits[ip] = recent + [now]
        if not public_slots.acquire(blocking=False):
            raise HTTPException(429, {"code": "public_busy", "message": "busy: try again in a moment"}, headers={"Retry-After": "10"})
        try:
            data = await read_image(request, PUBLIC_MAX_BYTES)
            return await run_in_threadpool(identify_image, data, None, True)
        finally:
            public_slots.release()

    @app.post("/api/requeue-failed", dependencies=[Depends(auth)])
    def requeue_failed():
        """Restart the analysis of the photos given up after several failures."""
        return {"requeued": requeue(db, "failed")}

    @app.post("/api/sync", dependencies=[Depends(auth)])
    def sync_start():
        """"Sync" button: new photos from Immich, analysis, tags, local data."""
        return {"started": w.start_manual(), "progress": w.progress}

    class PauseIn(BaseModel):
        paused: bool

    @app.post("/api/analysis/pause", dependencies=[Depends(auth)])
    def analysis_pause(body: PauseIn):
        """Analysis pause/resume button (Worker.pause_analysis), kept from one launch to the next."""
        store.set_state(db, "analysis_paused", "1" if body.paused else "0")
        w.pause_analysis(body.paused)
        return {"paused": w.analysis_paused}

    @app.get("/api/sync/status", dependencies=[Depends(auth)])
    def sync_status():
        return {**w.progress, "analysis_paused": w.analysis_paused, "last_run": json.loads(store.get_state(db, "last_run") or "null"),
                "models_loaded": w.models_loaded, "models_on_disk": models_on_disk(w.s)}
