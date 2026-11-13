"""Review routes: images, Photos page (filters, sorting, groups), detections (confirm, reject, box
drawn or reframed), candidate names, series, bulk actions, place entered by hand."""

import io
import json
import logging
import threading
import time
from datetime import datetime, timezone

import httpx
from fastapi import Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from .. import series, store, traits
from ..autoconfirm import auto_threshold
from ..curation import DETECTION_GROUP, EXCLUDED, cand_json, relocate
from ..immich.client import is_missing
from ..pipeline import SMALL_CROP_PX, Box, crop, crop_side, decide, load_original, open_image, rerank
from ..taxonomy import Taxonomy
from ..worker import MAX_FAILURES, Busy
from .context import BoxIn, TaxonIn, fail

log = logging.getLogger(__name__)


def register(app, c) -> None:
    s, w, db, tx, auth, lock = c.s, c.w, c.db, c.tx, c.auth, c.lock
    taxon, det_json, ensure_taxon, asset_json = c.taxon, c.det_json, c.ensure_taxon, c.asset_json
    set_status, drop, original = c.set_status, c.drop, c.original

    @app.get("/api/thumb/{asset_id}", dependencies=[Depends(auth)])
    def thumb(asset_id: str, size: str = "preview"):
        if not db.execute("SELECT 1 FROM asset WHERE asset_id = ?", (asset_id,)).fetchone():
            raise HTTPException(404)
        try:
            data = w.client_for(asset_id).thumbnail(asset_id, "thumbnail" if size == "thumbnail" else "preview")
        except Exception as e:
            if is_missing(e):
                fail(404, "photo_deleted", "photo deleted from Immich")
            raise
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/full/{asset_id}", dependencies=[Depends(auth)])
    def full_image(asset_id: str):
        """Full-resolution photo for the viewer zoom (the preview is only 1440 px on the short
        side): the original, re-encoded as JPEG for formats the browser does not display. It needs the
        optional asset.download permission; without it, or for an unreadable format, the viewer keeps the preview."""
        if not db.execute("SELECT 1 FROM asset WHERE asset_id = ?", (asset_id,)).fetchone():
            raise HTTPException(404)
        try:
            data = original(asset_id)
        except Exception as e:
            if is_missing(e):
                fail(404, "photo_deleted", "photo deleted from Immich")
            raise
        if data is None:
            fail(404, "no_original", "original unavailable (asset.download permission)")
        try:
            image = open_image(data, 8192)
        except Exception:
            fail(415, "bad_image", "unreadable original format")
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=90)
        return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # -- review --------------------------------------------------------------

    HIGHER_RANKS = ("kingdom", "phylum", "class", "order", "family", "genus")

    class PhotoScope(BaseModel):
        taxon: int | None = None
        rank: str | None = None
        name: str | None = None
        country: str | None = None
        city: str | None = None
        camera: str | None = None
        day: str | None = None      # AAAA-MM-JJ
        month: int | None = None    # 1 to 12, all years
        year: int | None = None
        source: str | None = None   # "main" or the id of an added source (several sources)

    # Photos page filters: SQL condition on the asset (alias a).
    PHOTO_FILTERS = {
        "pending": "EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'pending')",
        "audit": "EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'pending' AND d.audit = 1)",
        "auto": "EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'confirmed' AND d.confirmed_by = 'auto')",
        "user": "EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'confirmed' AND d.confirmed_by = 'user')",
        "rejected": "EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'rejected')",
        "none": "a.analyzed_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id "
                "AND d.status NOT IN ('rejected', 'deleted'))",
        "failed": f"a.analyzed_at IS NULL AND a.fail_count >= {MAX_FAILURES}",
        "all": "1",   # all photos, including those not analysed yet (badge in the grid)
        # At least one confirmed species: the photos that the statistics count.
        "identified": "EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'confirmed' AND d.taxon_key IS NOT NULL)",
    }
    # Sorting: by date, or by confidence of the best detection awaiting review (bulk review of the most certain,
    # or of the most doubtful first).
    PHOTO_SORTS = {
        "date": "a.taken_at DESC",
        "confident": "(SELECT max(d.score) FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'pending') DESC, a.taken_at DESC",
        "uncertain": "(SELECT min(d.score) FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'pending') ASC, a.taken_at DESC",
    }
    # Group: classes (Aves, Insecta…) or kingdoms (Plantae, Fungi) of the top candidate of a detection,
    # comma separated (reptiles are spread over several classes). Without a class (bony
    # fishes in GBIF), the phylum.
    KINDS = ("animal", "plant", "fungus", "microbe")   # values of curation.DETECTION_GROUP
    GROUP_EXPR = ("CASE WHEN json_extract(d.candidates, '$[0].lineage.kingdom') IN ('Plantae', 'Fungi') "
                  "THEN json_extract(d.candidates, '$[0].lineage.kingdom') "
                  "ELSE COALESCE(json_extract(d.candidates, '$[0].lineage.class'), json_extract(d.candidates, '$[0].lineage.phylum')) END")

    def scope_where(sc: "PhotoScope") -> tuple[str, tuple]:
        """Scope of the photo grid: species, rank, place, camera, date (all optional, can be combined)."""
        parts: list[str] = []
        args: list = []
        confirmed = "EXISTS (SELECT 1 FROM detection d {} WHERE d.asset_id = a.asset_id AND d.status = 'confirmed' AND {})"
        if sc.taxon is not None:
            parts.append(confirmed.format("", "d.taxon_key = ?"))
            args.append(sc.taxon)
        if sc.rank or sc.name:
            if sc.rank not in HIGHER_RANKS or not sc.name:
                fail(400, "unknown_rank", "unknown rank")
            parts.append(confirmed.format("JOIN taxon t ON t.gbif_key = d.taxon_key", f"json_extract(t.data, '$.lineage.{sc.rank}') = ?"))
            args.append(sc.name)
        if sc.source:
            parts.append("a.source = ?")
            args.append("" if sc.source == "main" else sc.source)
        for value, sql in ((sc.country, "a.country = ?"), (sc.city, "a.city = ?"),
                           (sc.camera, "trim(coalesce(a.make, '') || ' ' || coalesce(a.model, '')) = ?"),
                           (sc.day, "substr(a.taken_at, 1, 10) = ?"), (sc.month, "CAST(strftime('%m', a.taken_at) AS INTEGER) = ?"),
                           (sc.year, "CAST(substr(a.taken_at, 1, 4) AS INTEGER) = ?")):
            if value is not None:
                parts.append(sql)
                args.append(value)
        return " AND ".join(parts), tuple(args)

    def photo_where(filter: str, group: str, scope: "PhotoScope | None" = None) -> tuple[str, tuple]:
        where = PHOTO_FILTERS.get(filter)
        if where is None:
            fail(400, "unknown_filter", "unknown filter")
        where, args = f"a.missing_at IS NULL AND ({where})", ()
        extra, extra_args = scope_where(scope) if scope else ("", ())
        if extra:
            where, args = f"{where} AND {extra}", extra_args
        match, match_args = chosen(group)
        if not match:
            return where, args
        return (f"{where} AND EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND {kept(filter)} "
                f"AND {match})", (*args, *match_args))

    def chosen(group: str) -> tuple[str, tuple]:
        """Detections (alias d) of the kinds or groups chosen in the group parameter, any of them: "kind:animal,Plantae"
        is every animal and every plant. Empty when nothing is chosen."""
        kinds, groups = split_group(group)
        parts = ([f"{DETECTION_GROUP} IN ({', '.join('?' * len(kinds))})"] if kinds else []) \
            + ([f"{GROUP_EXPR} IN ({', '.join('?' * len(groups))})"] if groups else [])
        return (f"({' OR '.join(parts)})" if parts else ""), (*kinds, *groups)

    def kept(filter: str) -> str:
        """Detections that tell what a photo shows: not the rejected ones (wrong box), except in the Rejected filter."""
        return "d.status != 'deleted'" if filter == "rejected" else "d.status NOT IN ('rejected', 'deleted')"

    def split_group(group: str) -> tuple[list[str], list[str]]:
        """The group parameter of the Photos page: "kind:animal,kind:plant" (what was detected: animal, plant, fungus
        or microbe) and raw group values (class, or kingdom for plants and fungi), several of each, any of them. Kept in
        a single parameter so that every list, count and bulk action of the grid gets both."""
        items = [g for g in group.split(",") if g]
        kinds = [g[5:] for g in items if g.startswith("kind:") and g[5:] in KINDS]
        return kinds, [g for g in items if not g.startswith("kind:")]

    @app.get("/api/photos", dependencies=[Depends(auth)])
    def photos(filter: str = "pending", limit: int = Query(300, ge=1, le=1000), offset: int = Query(0, ge=0),
               sort: str = "date", group: str = "", scope: PhotoScope = Depends()):
        where, args = photo_where(filter, group, scope)
        order = PHOTO_SORTS.get(sort, PHOTO_SORTS["date"])
        rows = db.execute(f"SELECT a.* FROM asset a WHERE {where} ORDER BY {order}, a.asset_id LIMIT ? OFFSET ?",
                          (*args, limit, offset)).fetchall()
        threshold = auto_threshold(db, s)
        return [asset_json(r["asset_id"], dict(r), threshold) for r in rows]

    @app.get("/api/photos/counts", dependencies=[Depends(auth)])
    def photo_counts(group: str = "", scope: PhotoScope = Depends()):
        out = {}
        for k in PHOTO_FILTERS:
            where, args = photo_where(k, group, scope)
            out[k] = db.execute(f"SELECT count(*) FROM asset a WHERE {where}", args).fetchone()[0]
        return out

    @app.get("/api/photos/months", dependencies=[Depends(auth)])
    def photo_months(filter: str = "pending", group: str = "", scope: PhotoScope = Depends()):
        """Photos per month of the filter, from the most recent to the oldest (same order as the list by date):
        navigation timeline of the Photos page. m: "YYYY-MM", null for photos without a date (at the end)."""
        where, args = photo_where(filter, group, scope)
        return [{"m": r[0], "n": r[1]} for r in db.execute(
            f"SELECT substr(a.taken_at, 1, 7) AS m, count(*) FROM asset a WHERE {where} GROUP BY m "
            "ORDER BY m IS NULL, m DESC", args)]

    @app.get("/api/photos/groups", dependencies=[Depends(auth)])
    def photo_groups(filter: str = "pending", scope: PhotoScope = Depends()):
        """Groups present in a filter, with their kind (animal, plant…) and their number of photos: the Groups menu of
        the Photos page. Counted without the groups chosen, so that the menu shows what adding one would bring."""
        where, args = photo_where(filter, "", scope)
        rows = db.execute(
            f"SELECT k, g, count(DISTINCT asset_id) AS n FROM (SELECT d.asset_id, {DETECTION_GROUP} AS k, {GROUP_EXPR} AS g "
            f"  FROM detection d JOIN asset a ON a.asset_id = d.asset_id WHERE {kept(filter)} AND {where}) "
            "WHERE g IS NOT NULL GROUP BY k, g ORDER BY n DESC", args).fetchall()
        return [{"kind": r["k"], "group": r["g"], "n": r["n"]} for r in rows]

    @app.get("/api/photos/kinds", dependencies=[Depends(auth)])
    def photo_kinds(filter: str = "pending", scope: PhotoScope = Depends()):
        """Photos of a filter with at least one animal, plant, fungus or microbe detected (not rejected): the kind
        chips of the Photos page. A photo with an animal and a plant counts in both."""
        where, args = photo_where(filter, "", scope)
        rows = db.execute(
            f"SELECT g, count(DISTINCT asset_id) AS n FROM (SELECT d.asset_id, {DETECTION_GROUP} AS g "
            f"  FROM detection d JOIN asset a ON a.asset_id = d.asset_id WHERE {kept(filter)} AND {where}) GROUP BY g", args).fetchall()
        return {r["g"]: r["n"] for r in rows}

    @app.get("/api/assets/{asset_id}", dependencies=[Depends(auth)])
    def get_asset(asset_id: str):
        return asset_json(asset_id)

    @app.post("/api/detections/{det_id}/confirm", dependencies=[Depends(auth)])
    def confirm(det_id: int, body: TaxonIn | None = None):
        if body and (body.gbif_key or body.name):
            key = ensure_taxon(body.gbif_key, body.name)
            if key is None:
                fail(400, "taxon_not_found", "taxon not found in GBIF")
            rank = (taxon(key) or {}).get("rank", "species")
            return set_status(det_id, "confirmed", key, rank)
        row = db.execute("SELECT taxon_key FROM detection WHERE id = ?", (det_id,)).fetchone()
        if not row or not row["taxon_key"]:
            fail(400, "no_taxon", "no taxon proposed: pick a candidate")
        return set_status(det_id, "confirmed")

    class StateIn(BaseModel):
        status: str
        confirmed_by: str | None = None
        taxon_key: int | None = None
        taxon_rank: str | None = None
        reviewed_at: float | None = None
        # Optional (undoing a reframe): box and results of the original identification.
        x: float | None = None
        y: float | None = None
        w: float | None = None
        h: float | None = None
        score: float | None = None
        margin: float | None = None
        plausibility: str | None = None
        candidates: list | None = None
        candidates_out: list | None = None

    @app.post("/api/detections/{det_id}/restore", dependencies=[Depends(auth)])
    def restore(det_id: int, st: StateIn):
        """Undo: restore exactly the previous state (including "auto-confirmed"),
        so as not to count a phantom review in the calibration."""
        if st.status not in ("pending", "confirmed", "rejected", "deleted") or st.confirmed_by not in (None, "auto", "user"):
            fail(400, "bad_state", "invalid state")
        if not db.execute("SELECT 1 FROM detection WHERE id = ?", (det_id,)).fetchone():
            raise HTTPException(404)
        db.execute("UPDATE detection SET status = ?, confirmed_by = ?, taxon_key = ?, taxon_rank = ?, reviewed_at = ? WHERE id = ?",
                   (st.status, st.confirmed_by, st.taxon_key, st.taxon_rank, st.reviewed_at, det_id))
        if st.x is not None and st.candidates is not None:
            db.execute("UPDATE detection SET x = ?, y = ?, w = ?, h = ?, score = ?, margin = ?, plausibility = ?, candidates = ?, "
                       "candidates_out = ? WHERE id = ?",
                       (st.x, st.y, st.w, st.h, st.score, st.margin, st.plausibility, json.dumps(st.candidates, ensure_ascii=False),
                        json.dumps(st.candidates_out or [], ensure_ascii=False), det_id))
        db.commit()
        return det_json(db.execute("SELECT * FROM detection WHERE id = ?", (det_id,)).fetchone())

    class TraitIn(BaseModel):
        value: str | None = None   # one of the values of the trait | '' cannot tell | None: back to what the model says

    @app.post("/api/detections/{det_id}/trait/{trait}", dependencies=[Depends(auth)])
    def set_trait(det_id: int, trait: str, body: TraitIn):
        """Sex or life stage of the individual set by hand (kept whatever the model says), or handed back to the model."""
        if trait not in traits.TRAITS:
            raise HTTPException(404)
        if body.value not in (None, "", *traits.LABELS[trait]):
            fail(400, "bad_value", f"{trait}: {', '.join(traits.LABELS[trait])} or empty")
        if not db.execute("SELECT 1 FROM detection WHERE id = ?", (det_id,)).fetchone():
            raise HTTPException(404)
        if body.value is None:   # what the model said comes back at once (nothing, if it never looked at it)
            db.execute(f"UPDATE detection SET {trait} = CASE WHEN {trait}_score >= ? THEN {trait}_auto WHEN {trait}_auto IS NOT NULL THEN '' END, "
                       f"{trait}_by = CASE WHEN {trait}_auto IS NULL THEN NULL ELSE 'auto' END WHERE id = ?", (traits.MIN_MARGIN, det_id))
        else:
            db.execute(f"UPDATE detection SET {trait} = ?, {trait}_by = 'user' WHERE id = ?", (body.value, det_id))
        db.commit()
        return det_json(db.execute("SELECT * FROM detection WHERE id = ?", (det_id,)).fetchone())

    @app.post("/api/detections/{det_id}/reject", dependencies=[Depends(auth)])
    def reject(det_id: int):
        return set_status(det_id, "rejected")

    @app.post("/api/detections/{det_id}/reset", dependencies=[Depends(auth)])
    def reset(det_id: int):
        return set_status(det_id, "pending")

    def identify(asset_id: str, b: "BoxIn"):
        """Identify the subject of a box (tight crop, geography of the photo)."""
        a = db.execute("SELECT lat, lon FROM asset WHERE asset_id = ?", (asset_id,)).fetchone()
        if not a:
            raise HTTPException(404)
        x, y = min(max(0.0, b.x), 0.98), min(max(0.0, b.y), 0.98)
        box = Box(x, y, max(0.02, min(1 - x, b.w)), max(0.02, min(1 - y, b.h)), 1.0, "manual", None, "manual")
        try:
            image = open_image(w.client_for(asset_id).thumbnail(asset_id, "preview"))
        except Exception as e:
            if is_missing(e):
                fail(404, "photo_deleted", "photo deleted from Immich")
            raise
        if crop_side(image, box, 0.05) < SMALL_CROP_PX:   # small subject: crop from the original
            image = load_original(lambda: original(asset_id), image) or image
        with lock:
            cands = w.analyzer.classifier.classify([crop(image, box, margin=0.05)])[0]
        excluded: list = []
        ranked = rerank(cands, tx, a["lat"], a["lon"], drop=drop, excluded=excluded, tolerant=True)
        d = decide(ranked, tx, tolerant=True)
        if d.gbif_key:
            ensure_taxon(d.gbif_key, d.name)
        cj = [cand_json(c) for c in ranked[:8] if c.name not in EXCLUDED]
        out = [cand_json(c) for c in excluded[:8] if c.name not in EXCLUDED]
        return box, d, json.dumps(cj, ensure_ascii=False), json.dumps(out, ensure_ascii=False)

    @app.post("/api/assets/{asset_id}/boxes", dependencies=[Depends(auth)])
    def add_box(asset_id: str, b: BoxIn):
        """Box drawn by hand: identification of the crop, detection awaiting review."""
        box, d, cj, out = identify(asset_id, b)
        db.execute(
            "INSERT INTO detection (asset_id, x, y, w, h, det_score, box_label, detector, source, status, taxon_key, "
            "taxon_rank, score, margin, plausibility, candidates, candidates_out, model_key, model_rank, model_conf, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1.0, 'manual', 'manual', 'manual', 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (asset_id, box.x, box.y, box.w, box.h, d.gbif_key, d.rank, d.confidence, d.margin, d.plausibility,
             cj, out, d.gbif_key, d.rank, d.confidence, time.time()))
        db.commit()
        return asset_json(asset_id)

    @app.post("/api/assets/{asset_id}/reanalyze", dependencies=[Depends(auth)])
    def reanalyze(asset_id: str):
        """Analyse the photo again right away. As in bulk: reviewed or drawn detections are kept."""
        try:
            with lock:
                w.reanalyze(asset_id)
        except KeyError:
            raise HTTPException(404)
        except Busy as e:
            fail(409, "cycle_running", str(e))
        except (httpx.HTTPError, OSError) as e:
            if is_missing(e):
                fail(404, "photo_deleted", "photo deleted from Immich")
            raise
        return asset_json(asset_id)

    @app.put("/api/detections/{det_id}/box", dependencies=[Depends(auth)])
    def reframe(det_id: int, b: BoxIn):
        """Reframe a detection (automatic or manual): new box, new identification.
        A species already confirmed by the user is kept; only the candidates are recomputed."""
        row = db.execute("SELECT asset_id, status, confirmed_by FROM detection WHERE id = ?", (det_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        box, d, cj, out = identify(row["asset_id"], b)
        keep = row["status"] == "confirmed" and row["confirmed_by"] == "user"
        # frame_support cleared: the new box is identified on the crop alone. Sex and stage told by the models on the
        # old frame are forgotten (told again at the next cycle); what was set by hand stays.
        forget = "".join(f", {t} = CASE WHEN {t}_by = 'user' THEN {t} END, {t}_by = CASE WHEN {t}_by = 'user' THEN 'user' END, "
                         f"{t}_auto = NULL, {t}_score = NULL, {t}_key = NULL" for t in traits.TRAITS)
        db.execute("UPDATE detection SET x = ?, y = ?, w = ?, h = ?, candidates = ?, candidates_out = ?, margin = ?, plausibility = ?, "
                   "frame_support = NULL, smoothed = NULL, presmooth = NULL" + forget
                   + ("" if keep else ", taxon_key = ?, taxon_rank = ?, score = ?, status = 'pending', confirmed_by = NULL")
                   + " WHERE id = ?",
                   (box.x, box.y, box.w, box.h, cj, out, d.margin, d.plausibility)
                   + (() if keep else (d.gbif_key, d.rank, d.confidence)) + (det_id,))
        db.commit()
        return asset_json(row["asset_id"])

    @app.delete("/api/detections/{det_id}", dependencies=[Depends(auth)])
    def delete_detection(det_id: int, purge: bool = False):
        """Delete a detection (automatic or manual): it is not a subject. It stays in the database,
        marked "deleted", so that a re-analysis does not create it again and undo stays possible.
        purge: undoing a box just drawn, erased for good."""
        row = db.execute("SELECT source FROM detection WHERE id = ?", (det_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        if purge and row["source"] == "manual":
            db.execute("DELETE FROM detection WHERE id = ?", (det_id,))
            db.commit()
            return {"ok": True}
        db.execute("UPDATE detection SET status = 'deleted', confirmed_by = 'user', reviewed_at = ? WHERE id = ?", (time.time(), det_id))
        db.commit()
        return {"ok": True}

    names_pending: set[int] = set()

    def _enrich_names(items: list[tuple[int, str]]) -> None:
        for key, name in items:
            try:
                with lock:
                    ensure_taxon(key, name)
            except Exception as e:
                log.info("name of taxon %s not fetched: %s", key, e)
            finally:
                names_pending.discard(key)

    class NameRef(BaseModel):
        key: int
        name: str = ""

    @app.post("/api/names", dependencies=[Depends(auth)])
    def names(refs: list[NameRef]):
        """Common names of the candidates. Missing ones are fetched in the background
        (GBIF + Wikidata): ask again a few seconds later."""
        out, missing = {}, []
        for r in refs:
            t = store.load_taxon(db, r.key)
            if t:
                out[r.key] = {"vernacular": t.get("vernacular", {}), "scientific_name": t.get("scientific_name")}
            elif r.key not in names_pending:
                missing.append((r.key, r.name))
        if missing:
            names_pending.update(k for k, _ in missing)
            threading.Thread(target=_enrich_names, args=(missing,), daemon=True).start()
        return {"names": out, "pending": len({k for k, _ in missing} | {r.key for r in refs if r.key in names_pending and r.key not in out})}

    # -- series and bulk review --------------------------------------------

    def series_of(asset_id: str) -> list[str]:
        """Photos of the same series (burst) as asset_id, itself included, in chronological order (rules of
        series.py, computed here on demand: up to date even before the next cycle)."""
        a = db.execute("SELECT asset_id, taken_at, lat, lon, make, model FROM asset WHERE asset_id = ?", (asset_id,)).fetchone()
        t0 = series.seconds(a["taken_at"]) if a and (a["make"] or a["model"]) else None
        if t0 is None:
            return [asset_id] if a else []
        # Wide window then step-by-step chaining. One day of margin: dates without a time zone (file sources)
        # are in local time, compared here as is with a UTC bound.
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        rows = [dict(r) for r in db.execute(
            "SELECT asset_id, taken_at, lat, lon FROM asset WHERE missing_at IS NULL AND make IS ? AND model IS ? "
            "AND taken_at BETWEEN ? AND ?", (a["make"], a["model"], iso(t0 - 86400), iso(t0 + 86400) + "~"))]
        for r in rows:
            r["t"] = series.seconds(r["taken_at"])
        rows = sorted((r for r in rows if r["t"] is not None), key=lambda r: r["t"])
        # Same split as series.assign (bounded duration), over the ±1 day window.
        chain = next((c for c in series.chains(rows) if any(r["asset_id"] == asset_id for r in c)), None)
        if chain is None:   # hidden photo (gone from the source): alone
            return [asset_id]
        return [r["asset_id"] for r in chain]

    def series_matches(asset_id: str, key: int) -> list[dict]:
        """In the series, the detections awaiting review where this taxon is proposed (among the top 3 candidates):
        one per photo, the one where it ranks best. The starting photo is excluded."""
        out = []
        for aid in series_of(asset_id):
            if aid == asset_id:
                continue
            best = None
            for r in db.execute("SELECT * FROM detection WHERE asset_id = ? AND status = 'pending'", (aid,)):
                cands = json.loads(r["candidates"] or "[]")[:3]
                p = next((c["p"] for c in cands if c.get("gbif_key") == key), None)
                if p is None and r["taxon_key"] == key:
                    p = r["score"] or 0.0
                if p is not None and (best is None or p > best[1]):
                    best = (r, p)
            if best:
                out.append({"asset_id": aid, "detection": det_json(best[0]), "p": best[1]})
        return out

    @app.get("/api/assets/{asset_id}/series", dependencies=[Depends(auth)])
    def get_series(asset_id: str, key: int | None = None):
        """Series of the photo and, if key is given, the detections of the series that can be confirmed with this taxon."""
        ids = series_of(asset_id)
        if not ids:
            raise HTTPException(404)
        return {"assets": ids, "matches": series_matches(asset_id, key) if key else []}

    class SeriesIn(BaseModel):
        gbif_key: int

    @app.post("/api/assets/{asset_id}/series/confirm", dependencies=[Depends(auth)])
    def series_confirm(asset_id: str, body: SeriesIn):
        """Confirm the same taxon on the other photos of the series where it is proposed. Return the previous state
        of each changed detection, for undo."""
        # Saved key, not necessarily the requested one: a synonym is resolved to the accepted name.
        key = ensure_taxon(body.gbif_key, None)
        if not key:
            fail(400, "taxon_not_found", "taxon not found in GBIF")
        rank = (taxon(key) or {}).get("rank", "species")
        changed = []
        for m in series_matches(asset_id, key):
            changed.append(m["detection"])
            set_status(m["detection"]["id"], "confirmed", key, rank)
        return {"changed": changed}

    def selected(body) -> list[str]:
        """Photos targeted by a bulk action: the given list, or the whole filter (every) except the excluded ones."""
        if not body.every:
            return body.asset_ids
        where, args = photo_where(body.filter, body.group, body.scope)
        skip = set(body.exclude)
        return [r[0] for r in db.execute(f"SELECT a.asset_id FROM asset a WHERE {where}", args) if r[0] not in skip]

    class BulkIn(BaseModel):
        asset_ids: list[str] = []
        filter: str = "pending"   # filter shown: decides which detections are rejected
        # every: all photos of the filter ("Select all"), except those in exclude, instead of asset_ids.
        every: bool = False
        group: str = ""
        scope: PhotoScope = PhotoScope()
        exclude: list[str] = []
        action: str   # confirm | reject | reset | delete | taxon | reanalyze
        gbif_key: int | None = None   # action taxon

    SNAPSHOT = ("id", "asset_id", "status", "confirmed_by", "taxon_key", "taxon_rank", "reviewed_at")

    @app.post("/api/photos/bulk", dependencies=[Depends(auth)])
    def bulk(body: BulkIn):
        """Bulk review. Return the previous state of the changed detections (changed) and the created detections
        (created), for undo.
        - confirm: the detections awaiting review, with the proposed taxon (those without a proposal stay to review);
        - reject: per photo, what remains to review; if there is nothing to review, what is confirmed. The rule does not
          depend on the filter shown, and a confirmation done by hand is not dropped while there is something left to review;
        - delete: all detections of the photo ("this is not a subject");
        - reset: confirmed or rejected detections put back to review;
        - taxon: this taxon confirmed on the detection awaiting review of each photo; a photo without a detection gets a
          "whole image" detection; a photo with several subjects to review is left (skipped);
        - reanalyze: photos put back in the analysis queue (next cycle; reviewed detections kept).
        With kinds or groups chosen (group "kind:plant,Aves"), only the detections of these: the bird of a photo
        selected under Plants stays as it is."""
        if body.action not in ("confirm", "reject", "reset", "delete", "taxon", "reanalyze"):
            fail(400, "unknown_action", "unknown action")
        ids = selected(body)
        if body.action == "reanalyze":
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                db.execute(f"UPDATE asset SET analyzed_at = NULL, fail_count = 0, last_error = NULL "
                           f"WHERE asset_id IN ({','.join('?' * len(chunk))})", chunk)
            db.commit()
            return {"changed": [], "created": [], "skipped": 0, "requeued": len(ids)}
        rank = key = None
        if body.action == "taxon":
            # Saved key, not necessarily the requested one: a synonym is resolved to the accepted name.
            key = ensure_taxon(body.gbif_key, None) if body.gbif_key else None
            if not key:
                fail(400, "taxon_not_found", "taxon not found in GBIF")
            rank = (taxon(key) or {}).get("rank", "species")
        targets = {"confirm": "status = 'pending'", "taxon": "status = 'pending'",
                   "reset": "status IN ('confirmed', 'rejected')",
                   "delete": "status != 'deleted'",
                   "reject": "status IN ('pending', 'confirmed')"}[body.action]
        match, match_args = chosen(body.group)
        if match:
            targets = f"{targets}) AND {match} AND (1"
        changed, created, skipped, now = [], [], 0, time.time()
        for aid in ids:
            rows = db.execute(f"SELECT {', '.join(SNAPSHOT)} FROM detection d WHERE asset_id = ? AND ({targets})",
                              (aid, *match_args)).fetchall()
            if body.action == "reject" and any(r["status"] == "pending" for r in rows):
                rows = [r for r in rows if r["status"] == "pending"]
            if body.action == "taxon":
                if len(rows) > 1:
                    skipped += 1
                    continue
                if not rows:
                    if db.execute("SELECT 1 FROM detection WHERE asset_id = ? AND status IN ('pending', 'confirmed')", (aid,)).fetchone():
                        skipped += 1   # already identified: no second subject is added
                        continue
                    cur = db.execute(
                        "INSERT INTO detection (asset_id, x, y, w, h, det_score, box_label, detector, source, status, confirmed_by, "
                        "taxon_key, taxon_rank, created_at, reviewed_at) VALUES (?, 0, 0, 1, 1, 1.0, 'image', 'manual', 'manual', "
                        "'confirmed', 'user', ?, ?, ?, ?)", (aid, key, rank, now, now))
                    created.append(cur.lastrowid)
                    continue
                changed.append(dict(rows[0]))
                db.execute("UPDATE detection SET status = 'confirmed', confirmed_by = 'user', reviewed_at = ?, taxon_key = ?, "
                           "taxon_rank = ? WHERE id = ?", (now, key, rank, rows[0]["id"]))
                continue
            for r in rows:
                if body.action == "confirm" and not r["taxon_key"]:
                    skipped += 1
                    continue
                changed.append(dict(r))
                status = {"confirm": "confirmed", "reject": "rejected", "reset": "pending", "delete": "deleted"}[body.action]
                db.execute("UPDATE detection SET status = ?, confirmed_by = ?, reviewed_at = ? WHERE id = ?",
                           (status, None if status == "pending" else "user", now, r["id"]))
        db.commit()
        return {"changed": changed, "created": created, "skipped": skipped}

    class LocationIn(BaseModel):
        asset_ids: list[str] = []
        filter: str = "all"
        every: bool = False
        group: str = ""
        scope: PhotoScope = PhotoScope()
        exclude: list[str] = []
        lat: float | None = None   # None: clear the place entered by hand
        lon: float | None = None

    @app.post("/api/assets/location", dependencies=[Depends(auth)])
    def set_location(body: LocationIn):
        """Place entered by hand for photos without GPS (a GPS from the source is never replaced). Detections
        awaiting review are reclassified with this place; photos not analysed yet will use it. Beyond a few
        photos, the reclassification goes on in the background (GBIF requests for the new position)."""
        if (body.lat is None) != (body.lon is None):
            fail(400, "bad_location", "latitude and longitude go together")
        clear = body.lat is None
        if not clear and not (-90 <= body.lat <= 90 and -180 <= body.lon <= 180):
            fail(400, "bad_location", "latitude or longitude out of range")
        ids, changed, previous = selected(body), [], []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            cond = "geo_user = 1" if clear else "(lat IS NULL OR geo_user = 1)"
            for r in db.execute(f"SELECT asset_id, lat, lon, geo_user FROM asset WHERE asset_id IN ({marks}) AND {cond}", chunk):
                changed.append(r["asset_id"])
                previous.append(dict(r))
        for i in range(0, len(changed), 500):
            chunk = changed[i:i + 500]
            marks = ",".join("?" * len(chunk))
            db.execute(f"UPDATE asset SET lat = ?, lon = ?, geo_user = ? WHERE asset_id IN ({marks})",
                       (None if clear else round(body.lat, 5), None if clear else round(body.lon, 5), None if clear else 1, *chunk))
        db.commit()
        n, background = reclassify(changed)
        return {"changed": len(changed), "skipped": len(ids) - len(changed), "reclassified": n, "background": background,
                "previous": previous}

    def reclassify(changed: list[str]) -> tuple[int | None, bool]:
        """Detections awaiting review reclassified after a change of place; in the background beyond 20 photos."""
        if len(changed) > 20:
            def run():
                own = store.connect(s.db_path)
                try:
                    relocate(own, Taxonomy(own, langs=s.langs, user_agent=s.user_agent, backbone=tx.backbone_path), s, changed)
                except Exception:
                    log.exception("reclassification after a change of place failed")
                finally:
                    own.close()
            threading.Thread(target=run, daemon=True, name="place").start()
            return None, True
        try:
            return relocate(db, tx, s, changed), False
        except Exception:   # place already saved; network down: no transaction left open
            db.rollback()
            log.exception("reclassification after a change of place failed")
            return 0, False

    class PreviousLocation(BaseModel):
        asset_id: str
        lat: float | None = None
        lon: float | None = None
        geo_user: int | None = None

    @app.post("/api/assets/location-undo", dependencies=[Depends(auth)])
    def undo_location(items: list[PreviousLocation]):
        """Undo a place entered by hand: the previous values, then the reclassification."""
        db.executemany("UPDATE asset SET lat = ?, lon = ?, geo_user = ? WHERE asset_id = ?",
                       [(p.lat, p.lon, p.geo_user, p.asset_id) for p in items])
        db.commit()
        reclassify([p.asset_id for p in items])
        return {"restored": len(items)}

    @app.get("/api/search", dependencies=[Depends(auth)])
    def search(q: str = Query("", max_length=100)):
        """Places, cameras and files whose name contains q (taxa are searched in the browser)."""
        if len(q.strip()) < 2:
            return {"places": [], "cameras": [], "files": []}
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        camera = "trim(coalesce(make, '') || ' ' || coalesce(model, ''))"
        places = db.execute("SELECT city, country, count(*) AS n, min(asset_id) AS cover FROM asset WHERE missing_at IS NULL "
                            "AND (city LIKE ? ESCAPE '\\' OR country LIKE ? ESCAPE '\\') GROUP BY city, country ORDER BY n DESC LIMIT 4",
                            (like, like)).fetchall()
        cameras = db.execute(f"SELECT {camera} AS camera, count(*) AS n, min(asset_id) AS cover FROM asset WHERE missing_at IS NULL "
                             f"AND {camera} LIKE ? ESCAPE '\\' GROUP BY 1 ORDER BY n DESC LIMIT 3", (like,)).fetchall()
        files = db.execute("SELECT asset_id, file_name FROM asset WHERE missing_at IS NULL AND file_name LIKE ? ESCAPE '\\' "
                           "ORDER BY taken_at DESC LIMIT 4", (like,)).fetchall()
        return {"places": [dict(r) for r in places], "cameras": [dict(r) for r in cameras], "files": [dict(r) for r in files]}

    class RestoreIn(BaseModel):
        id: int
        status: str
        confirmed_by: str | None = None
        taxon_key: int | None = None
        taxon_rank: str | None = None
        reviewed_at: float | None = None

    @app.post("/api/detections/restore-many", dependencies=[Depends(auth)])
    def restore_many(items: list[RestoreIn]):
        """Undo a bulk review: the previous state of each detection, in one transaction."""
        for st in items:
            if st.status not in ("pending", "confirmed", "rejected", "deleted") or st.confirmed_by not in (None, "auto", "user"):
                fail(400, "bad_state", "invalid state")
        db.executemany("UPDATE detection SET status = ?, confirmed_by = ?, taxon_key = ?, taxon_rank = ?, reviewed_at = ? WHERE id = ?",
                       [(st.status, st.confirmed_by, st.taxon_key, st.taxon_rank, st.reviewed_at, st.id) for st in items])
        db.commit()
        return {"restored": len(items)}

    class UndoIn(BaseModel):
        changed: list[RestoreIn] = []
        created: list[int] = []

    @app.post("/api/photos/bulk-undo", dependencies=[Depends(auth)])
    def bulk_undo(body: UndoIn):
        """Undo a bulk action: previous states restored, created detections ("whole image") erased."""
        restore_many(body.changed)
        for i in range(0, len(body.created), 500):
            chunk = body.created[i:i + 500]
            db.execute(f"DELETE FROM detection WHERE source = 'manual' AND id IN ({','.join('?' * len(chunk))})", chunk)
        db.commit()
        return {"restored": len(body.changed), "deleted": len(body.created)}
