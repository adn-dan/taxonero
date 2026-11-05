"""Routes de la page Identifier (image envoyée, rien n'est gardé) et de l'analyse (synchronisation, reprise des
photos en échec)."""

import json

from fastapi import Depends, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import store
from ..curation import EXCLUDED, cand_json
from ..pipeline import MAX_SIDE, exif_latlon, open_image
from ..worker import requeue
from .context import fail


def register(app, c) -> None:
    w, db, auth, lock, taxon, ensure_taxon, analyzer = c.w, c.db, c.auth, c.lock, c.taxon, c.ensure_taxon, c.analyzer

    IDENTIFY_MAX_BYTES = 60 * 2 ** 20

    def identify_image(data: bytes, place: tuple[float, float] | None = None) -> dict:
        try:
            image = open_image(data, MAX_SIDE)   # une photo de 50 Mpx n'est jamais décodée en entier
        except Exception:
            fail(400, "bad_image", "image illisible (formats : JPEG, PNG, WebP, TIFF)")
        lat, lon = exif_latlon(data)
        if lat is None and place:   # un GPS de la photo n'est jamais remplacé
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
                        "taxon_key": d.gbif_key, "taxon_rank": d.rank, "score": d.confidence, "plausibility": d.plausibility,
                        "domestic": a.domestic, "candidates": cands, "taxon": taxon(d.gbif_key)})
        # Photos déjà dans la collection pour le taxon retenu et les candidats : lien vers la fiche espèce.
        keys = {k for o in out for k in [o["taxon_key"], *(c.get("gbif_key") for c in o["candidates"])] if k}
        mine = {r["taxon_key"]: r["n"] for r in db.execute(
            "SELECT d.taxon_key, count(DISTINCT d.asset_id) AS n FROM detection d JOIN asset a ON a.asset_id = d.asset_id "
            f"WHERE d.status = 'confirmed' AND a.missing_at IS NULL AND d.taxon_key IN ({','.join('?' * len(keys))}) "
            "GROUP BY d.taxon_key", list(keys))} if keys else {}
        return {"lat": lat, "lon": lon, "detections": out, "mine": mine}

    @app.post("/api/identify", dependencies=[Depends(auth)])
    async def identify_upload(request: Request, lat: float | None = None, lon: float | None = None):
        """Identifie l'image envoyée dans le corps de la requête. Elle reste en mémoire le temps de l'analyse :
        rien n'est écrit (ni base, ni fichier, ni Immich). Coordonnées GPS de l'EXIF utilisées si présentes, sinon
        lat et lon s'ils sont indiqués."""
        if (lat is None) != (lon is None) or (lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180)):
            fail(400, "bad_location", "latitude ou longitude hors limites")
        too_big = lambda: fail(413, "image_too_big", "image trop lourde", max=IDENTIFY_MAX_BYTES // 2 ** 20)
        size = request.headers.get("content-length", "")
        if size.isdigit() and int(size) > IDENTIFY_MAX_BYTES:
            too_big()
        # Lu par morceaux, plafonné : un envoi sans Content-Length (ou mensonger) n'est pas gardé en mémoire en entier.
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > IDENTIFY_MAX_BYTES:
                too_big()
        data = bytes(buf)
        if not data:
            fail(400, "empty_image", "aucune image")
        return await run_in_threadpool(identify_image, data, (lat, lon) if lat is not None else None)

    @app.post("/api/requeue-failed", dependencies=[Depends(auth)])
    def requeue_failed():
        """Relance l'analyse des photos abandonnées après plusieurs échecs."""
        return {"requeued": requeue(db, "failed")}

    @app.post("/api/sync", dependencies=[Depends(auth)])
    def sync_start():
        """Bouton « Synchroniser » : nouvelles photos d'Immich, analyse, tags, données locales."""
        return {"started": w.start_manual(), "progress": w.progress}

    class PauseIn(BaseModel):
        paused: bool

    @app.post("/api/analysis/pause", dependencies=[Depends(auth)])
    def analysis_pause(body: PauseIn):
        """Bouton pause/reprise de l'analyse (Worker.pause_analysis), gardé d'un lancement à l'autre."""
        store.set_state(db, "analysis_paused", "1" if body.paused else "0")
        w.pause_analysis(body.paused)
        return {"paused": w.analysis_paused}

    @app.get("/api/sync/status", dependencies=[Depends(auth)])
    def sync_status():
        return {**w.progress, "analysis_paused": w.analysis_paused, "last_run": json.loads(store.get_state(db, "last_run") or "null"),
                "models_loaded": w.models_loaded}
