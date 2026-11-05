"""Routes de la relecture : images, page Photos (filtres, tri, groupes), détections (valider, rejeter, cadre
dessiné ou recadré), noms des candidats, séries, actions en lot, lieu indiqué à la main."""

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

from .. import series, store
from ..autoconfirm import auto_threshold
from ..curation import EXCLUDED, cand_json, relocate
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
            data = w.client.thumbnail(asset_id, "thumbnail" if size == "thumbnail" else "preview")
        except Exception as e:
            if is_missing(e):
                fail(404, "photo_deleted", "photo supprimée d'Immich")
            raise
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/full/{asset_id}", dependencies=[Depends(auth)])
    def full_image(asset_id: str):
        """Photo en pleine définition pour le zoom de la visionneuse (l'aperçu ne fait que 1440 px sur le petit
        côté) : l'original, réencodé en JPEG pour les formats que le navigateur n'affiche pas. Il faut la
        permission facultative asset.download ; sans elle, ou pour un format illisible, la visionneuse garde l'aperçu."""
        if not db.execute("SELECT 1 FROM asset WHERE asset_id = ?", (asset_id,)).fetchone():
            raise HTTPException(404)
        try:
            data = original(asset_id)
        except Exception as e:
            if is_missing(e):
                fail(404, "photo_deleted", "photo supprimée d'Immich")
            raise
        if data is None:
            fail(404, "no_original", "original indisponible (permission asset.download)")
        try:
            image = open_image(data, 8192)
        except Exception:
            fail(415, "bad_image", "format de l'original non lisible")
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=90)
        return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # -- relecture -----------------------------------------------------------

    HIGHER_RANKS = ("kingdom", "phylum", "class", "order", "family", "genus")

    class PhotoScope(BaseModel):
        taxon: int | None = None
        rank: str | None = None
        name: str | None = None
        country: str | None = None
        city: str | None = None
        camera: str | None = None
        day: str | None = None      # AAAA-MM-JJ
        month: int | None = None    # 1 à 12, toutes années
        year: int | None = None

    # Filtres de la page Photos : condition SQL sur l'asset (alias a).
    PHOTO_FILTERS = {
        "pending": "EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'pending')",
        "audit": "EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'pending' AND d.audit = 1)",
        "auto": "EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'confirmed' AND d.confirmed_by = 'auto')",
        "user": "EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'confirmed' AND d.confirmed_by = 'user')",
        "rejected": "EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'rejected')",
        "none": "a.analyzed_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id "
                "AND d.status NOT IN ('rejected', 'deleted'))",
        "failed": f"a.analyzed_at IS NULL AND a.fail_count >= {MAX_FAILURES}",
        "all": "1",   # toutes les photos, y compris celles pas encore analysées (badge dans la grille)
        # Au moins une espèce validée : les photos que comptent les statistiques.
        "identified": "EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'confirmed' AND d.taxon_key IS NOT NULL)",
    }
    # Tri : par date, ou par confiance de la meilleure détection à relire (relecture en lot des plus sûres,
    # ou des plus douteuses d'abord).
    PHOTO_SORTS = {
        "date": "a.taken_at DESC",
        "confident": "(SELECT max(d.score) FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'pending') DESC, a.taken_at DESC",
        "uncertain": "(SELECT min(d.score) FROM detection d WHERE d.asset_id = a.asset_id AND d.status = 'pending') ASC, a.taken_at DESC",
    }
    # Groupe : classes (Aves, Insecta…) ou règnes (Plantae, Fungi) du premier candidat d'une détection,
    # séparés par des virgules (les reptiles sont répartis sur plusieurs classes). Sans classe (poissons
    # osseux dans GBIF), l'embranchement.
    GROUP_EXPR = ("CASE WHEN json_extract(d.candidates, '$[0].lineage.kingdom') IN ('Plantae', 'Fungi') "
                  "THEN json_extract(d.candidates, '$[0].lineage.kingdom') "
                  "ELSE COALESCE(json_extract(d.candidates, '$[0].lineage.class'), json_extract(d.candidates, '$[0].lineage.phylum')) END")

    def scope_where(sc: "PhotoScope") -> tuple[str, tuple]:
        """Portée de la grille de photos : espèce, rang, lieu, appareil, date (tous facultatifs, cumulables)."""
        parts: list[str] = []
        args: list = []
        confirmed = "EXISTS (SELECT 1 FROM detection d {} WHERE d.asset_id = a.asset_id AND d.status = 'confirmed' AND {})"
        if sc.taxon is not None:
            parts.append(confirmed.format("", "d.taxon_key = ?"))
            args.append(sc.taxon)
        if sc.rank or sc.name:
            if sc.rank not in HIGHER_RANKS or not sc.name:
                fail(400, "unknown_rank", "rang inconnu")
            parts.append(confirmed.format("JOIN taxon t ON t.gbif_key = d.taxon_key", f"json_extract(t.data, '$.lineage.{sc.rank}') = ?"))
            args.append(sc.name)
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
            fail(400, "unknown_filter", "filtre inconnu")
        where, args = f"a.missing_at IS NULL AND ({where})", ()
        extra, extra_args = scope_where(scope) if scope else ("", ())
        if extra:
            where, args = f"{where} AND {extra}", extra_args
        groups = [g for g in group.split(",") if g]
        if not groups:
            return where, args
        return (f"{where} AND EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = a.asset_id AND d.status != 'deleted' "
                f"AND {GROUP_EXPR} IN ({', '.join('?' * len(groups))}))", (*args, *groups))

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
        """Photos par mois du filtre, des plus récentes aux plus anciennes (même ordre que la liste par date) :
        frise de navigation de la page Photos. m : « AAAA-MM », null pour les photos sans date (à la fin)."""
        where, args = photo_where(filter, group, scope)
        return [{"m": r[0], "n": r[1]} for r in db.execute(
            f"SELECT substr(a.taken_at, 1, 7) AS m, count(*) FROM asset a WHERE {where} GROUP BY m "
            "ORDER BY m IS NULL, m DESC", args)]

    @app.get("/api/photos/groups", dependencies=[Depends(auth)])
    def photo_groups(filter: str = "pending", scope: PhotoScope = Depends()):
        """Groupes présents dans un filtre, avec leur nombre de photos (puces de la page Photos)."""
        where, args = photo_where(filter, "", scope)
        rows = db.execute(
            f"SELECT g, count(DISTINCT asset_id) AS n FROM (SELECT d.asset_id, {GROUP_EXPR} AS g "
            f"  FROM detection d JOIN asset a ON a.asset_id = d.asset_id WHERE d.status != 'deleted' AND {where}) "
            "WHERE g IS NOT NULL GROUP BY g ORDER BY n DESC", args).fetchall()
        return [{"group": r["g"], "n": r["n"]} for r in rows]

    @app.get("/api/assets/{asset_id}", dependencies=[Depends(auth)])
    def get_asset(asset_id: str):
        return asset_json(asset_id)

    @app.post("/api/detections/{det_id}/confirm", dependencies=[Depends(auth)])
    def confirm(det_id: int, body: TaxonIn | None = None):
        if body and (body.gbif_key or body.name):
            key = ensure_taxon(body.gbif_key, body.name)
            if key is None:
                fail(400, "taxon_not_found", "taxon introuvable dans GBIF")
            rank = (taxon(key) or {}).get("rank", "species")
            return set_status(det_id, "confirmed", key, rank)
        row = db.execute("SELECT taxon_key FROM detection WHERE id = ?", (det_id,)).fetchone()
        if not row or not row["taxon_key"]:
            fail(400, "no_taxon", "aucun taxon proposé : choisis un candidat")
        return set_status(det_id, "confirmed")

    class StateIn(BaseModel):
        status: str
        confirmed_by: str | None = None
        taxon_key: int | None = None
        taxon_rank: str | None = None
        reviewed_at: float | None = None
        # Facultatif (annulation d'un recadrage) : cadre et résultats de l'identification d'origine.
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
        """Annulation : remet exactement l'état précédent (y compris « validée automatiquement »),
        pour ne pas compter une relecture fantôme dans la calibration."""
        if st.status not in ("pending", "confirmed", "rejected", "deleted") or st.confirmed_by not in (None, "auto", "user"):
            fail(400, "bad_state", "état invalide")
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

    @app.post("/api/detections/{det_id}/reject", dependencies=[Depends(auth)])
    def reject(det_id: int):
        return set_status(det_id, "rejected")

    @app.post("/api/detections/{det_id}/reset", dependencies=[Depends(auth)])
    def reset(det_id: int):
        return set_status(det_id, "pending")

    def identify(asset_id: str, b: "BoxIn"):
        """Identifie le sujet d'un cadre (recadrage serré, géographie de la photo)."""
        a = db.execute("SELECT lat, lon FROM asset WHERE asset_id = ?", (asset_id,)).fetchone()
        if not a:
            raise HTTPException(404)
        x, y = min(max(0.0, b.x), 0.98), min(max(0.0, b.y), 0.98)
        box = Box(x, y, max(0.02, min(1 - x, b.w)), max(0.02, min(1 - y, b.h)), 1.0, "manual", None, "manual")
        try:
            image = open_image(w.client.thumbnail(asset_id, "preview"))
        except Exception as e:
            if is_missing(e):
                fail(404, "photo_deleted", "photo supprimée d'Immich")
            raise
        if crop_side(image, box, 0.05) < SMALL_CROP_PX:   # petit sujet : recadrage dans l'original
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
        """Boîte dessinée à la main : identification du recadrage, détection à relire."""
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
        """Ré-analyse la photo tout de suite. Comme en lot : les détections relues ou dessinées sont gardées."""
        try:
            with lock:
                w.reanalyze(asset_id)
        except KeyError:
            raise HTTPException(404)
        except Busy as e:
            fail(409, "cycle_running", str(e))
        except (httpx.HTTPError, OSError) as e:
            if is_missing(e):
                fail(404, "photo_deleted", "photo supprimée d'Immich")
            raise
        return asset_json(asset_id)

    @app.put("/api/detections/{det_id}/box", dependencies=[Depends(auth)])
    def reframe(det_id: int, b: BoxIn):
        """Recadrage d'une détection (automatique ou manuelle) : nouveau cadre, ré-identification.
        Une espèce déjà validée par l'utilisateur est conservée ; seuls les candidats sont recalculés."""
        row = db.execute("SELECT asset_id, status, confirmed_by FROM detection WHERE id = ?", (det_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        box, d, cj, out = identify(row["asset_id"], b)
        keep = row["status"] == "confirmed" and row["confirmed_by"] == "user"
        # frame_support effacé : le nouveau cadre est identifié sur le recadrage seul.
        db.execute("UPDATE detection SET x = ?, y = ?, w = ?, h = ?, candidates = ?, candidates_out = ?, margin = ?, plausibility = ?, "
                   "frame_support = NULL, smoothed = NULL, presmooth = NULL"
                   + ("" if keep else ", taxon_key = ?, taxon_rank = ?, score = ?, status = 'pending', confirmed_by = NULL")
                   + " WHERE id = ?",
                   (box.x, box.y, box.w, box.h, cj, out, d.margin, d.plausibility)
                   + (() if keep else (d.gbif_key, d.rank, d.confidence)) + (det_id,))
        db.commit()
        return asset_json(row["asset_id"])

    @app.delete("/api/detections/{det_id}", dependencies=[Depends(auth)])
    def delete_detection(det_id: int, purge: bool = False):
        """Supprime une détection (automatique ou manuelle) : ce n'est pas un sujet. Elle reste en base,
        marquée « supprimée », pour qu'une ré-analyse ne la recrée pas et que l'annulation reste possible.
        purge : annulation d'une boîte qu'on vient de dessiner, effacée pour de bon."""
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
                log.info("nom du taxon %s non récupéré : %s", key, e)
            finally:
                names_pending.discard(key)

    class NameRef(BaseModel):
        key: int
        name: str = ""

    @app.post("/api/names", dependencies=[Depends(auth)])
    def names(refs: list[NameRef]):
        """Noms communs des candidats. Ceux qui manquent sont récupérés en arrière-plan
        (GBIF + Wikidata) : redemander quelques secondes plus tard."""
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

    # -- séries et relecture en lot --------------------------------------------

    def series_of(asset_id: str) -> list[str]:
        """Photos de la même série (rafale) que asset_id, elle comprise, dans l'ordre chronologique (règles de
        series.py, calculées ici à la demande : à jour même avant le prochain cycle)."""
        a = db.execute("SELECT asset_id, taken_at, lat, lon, make, model FROM asset WHERE asset_id = ?", (asset_id,)).fetchone()
        t0 = series.seconds(a["taken_at"]) if a and (a["make"] or a["model"]) else None
        if t0 is None:
            return [asset_id] if a else []
        # Fenêtre large puis chaînage de proche en proche. Un jour de marge : les dates sans fuseau (sources fichiers)
        # sont en heure locale, ici comparées telles quelles à une borne en UTC.
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        rows = [dict(r) for r in db.execute(
            "SELECT asset_id, taken_at, lat, lon FROM asset WHERE missing_at IS NULL AND make IS ? AND model IS ? "
            "AND taken_at BETWEEN ? AND ?", (a["make"], a["model"], iso(t0 - 86400), iso(t0 + 86400) + "~"))]
        for r in rows:
            r["t"] = series.seconds(r["taken_at"])
        rows = sorted((r for r in rows if r["t"] is not None), key=lambda r: r["t"])
        # Même découpage que series.assign (durée bornée), sur la fenêtre de ±1 jour.
        chain = next((c for c in series.chains(rows) if any(r["asset_id"] == asset_id for r in c)), None)
        if chain is None:   # photo masquée (disparue de la source) : seule
            return [asset_id]
        return [r["asset_id"] for r in chain]

    def series_matches(asset_id: str, key: int) -> list[dict]:
        """Dans la série, les détections à relire où ce taxon est proposé (parmi les 3 premiers candidats) :
        une par photo, celle où il est le mieux placé. La photo de départ est exclue."""
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
        """Série de la photo et, si key est donné, les détections de la série qu'on peut valider avec ce taxon."""
        ids = series_of(asset_id)
        if not ids:
            raise HTTPException(404)
        return {"assets": ids, "matches": series_matches(asset_id, key) if key else []}

    class SeriesIn(BaseModel):
        gbif_key: int

    @app.post("/api/assets/{asset_id}/series/confirm", dependencies=[Depends(auth)])
    def series_confirm(asset_id: str, body: SeriesIn):
        """Valide le même taxon sur les autres photos de la série où il est proposé. Renvoie l'état d'avant
        de chaque détection modifiée, pour l'annulation."""
        # Clé enregistrée, pas forcément celle demandée : un synonyme est résolu vers le nom accepté.
        key = ensure_taxon(body.gbif_key, None)
        if not key:
            fail(400, "taxon_not_found", "taxon introuvable dans GBIF")
        rank = (taxon(key) or {}).get("rank", "species")
        changed = []
        for m in series_matches(asset_id, key):
            changed.append(m["detection"])
            set_status(m["detection"]["id"], "confirmed", key, rank)
        return {"changed": changed}

    def selected(body) -> list[str]:
        """Photos visées par une action en lot : la liste donnée, ou tout le filtre (every) sauf les exclues."""
        if not body.every:
            return body.asset_ids
        where, args = photo_where(body.filter, body.group, body.scope)
        skip = set(body.exclude)
        return [r[0] for r in db.execute(f"SELECT a.asset_id FROM asset a WHERE {where}", args) if r[0] not in skip]

    class BulkIn(BaseModel):
        asset_ids: list[str] = []
        filter: str = "pending"   # filtre affiché : décide des détections rejetées
        # every : toutes les photos du filtre (« Tout sélectionner »), sauf celles d'exclude, au lieu d'asset_ids.
        every: bool = False
        group: str = ""
        scope: PhotoScope = PhotoScope()
        exclude: list[str] = []
        action: str   # confirm | reject | reset | delete | taxon | reanalyze
        gbif_key: int | None = None   # action taxon

    SNAPSHOT = ("id", "asset_id", "status", "confirmed_by", "taxon_key", "taxon_rank", "reviewed_at")

    @app.post("/api/photos/bulk", dependencies=[Depends(auth)])
    def bulk(body: BulkIn):
        """Relecture en lot. Renvoie l'état d'avant des détections modifiées (changed) et les détections créées
        (created), pour l'annulation.
        - confirm : les détections à relire, avec le taxon proposé (celles sans proposition restent à relire) ;
        - reject : par photo, ce qui reste à relire ; s'il n'y a rien à relire, ce qui est validé. La règle ne dépend
          pas du filtre affiché, et une validation faite à la main ne saute pas tant qu'il reste à relire ;
        - delete : toutes les détections de la photo (« ce n'est pas un sujet ») ;
        - reset : détections validées ou rejetées remises à relire ;
        - taxon : ce taxon validé sur la détection à relire de chaque photo ; une photo sans détection reçoit une
          détection « image entière » ; une photo à plusieurs sujets à relire est laissée (skipped) ;
        - reanalyze : photos remises dans la file d'analyse (prochain cycle ; détections relues gardées)."""
        if body.action not in ("confirm", "reject", "reset", "delete", "taxon", "reanalyze"):
            fail(400, "unknown_action", "action inconnue")
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
            # Clé enregistrée, pas forcément celle demandée : un synonyme est résolu vers le nom accepté.
            key = ensure_taxon(body.gbif_key, None) if body.gbif_key else None
            if not key:
                fail(400, "taxon_not_found", "taxon introuvable dans GBIF")
            rank = (taxon(key) or {}).get("rank", "species")
        targets = {"confirm": "status = 'pending'", "taxon": "status = 'pending'",
                   "reset": "status IN ('confirmed', 'rejected')",
                   "delete": "status != 'deleted'",
                   "reject": "status IN ('pending', 'confirmed')"}[body.action]
        changed, created, skipped, now = [], [], 0, time.time()
        for aid in ids:
            rows = db.execute(f"SELECT {', '.join(SNAPSHOT)} FROM detection WHERE asset_id = ? AND ({targets})", (aid,)).fetchall()
            if body.action == "reject" and any(r["status"] == "pending" for r in rows):
                rows = [r for r in rows if r["status"] == "pending"]
            if body.action == "taxon":
                if len(rows) > 1:
                    skipped += 1
                    continue
                if not rows:
                    if db.execute("SELECT 1 FROM detection WHERE asset_id = ? AND status IN ('pending', 'confirmed')", (aid,)).fetchone():
                        skipped += 1   # déjà identifiée : on ne lui ajoute pas un second sujet
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
        lat: float | None = None   # None : effacer le lieu indiqué à la main
        lon: float | None = None

    @app.post("/api/assets/location", dependencies=[Depends(auth)])
    def set_location(body: LocationIn):
        """Lieu indiqué à la main pour des photos sans GPS (un GPS de la source n'est jamais remplacé). Les détections
        à relire sont reclassées avec ce lieu ; les photos pas encore analysées l'utiliseront. Au-delà de quelques
        photos, le reclassement continue en tâche de fond (requêtes GBIF pour la nouvelle position)."""
        if (body.lat is None) != (body.lon is None):
            fail(400, "bad_location", "latitude et longitude vont ensemble")
        clear = body.lat is None
        if not clear and not (-90 <= body.lat <= 90 and -180 <= body.lon <= 180):
            fail(400, "bad_location", "latitude ou longitude hors limites")
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
        """Détections à relire reclassées après un changement de lieu ; en tâche de fond au-delà de 20 photos."""
        if len(changed) > 20:
            def run():
                own = store.connect(s.db_path)
                try:
                    relocate(own, Taxonomy(own, langs=s.langs, user_agent=s.user_agent, backbone=tx.backbone_path), s, changed)
                except Exception:
                    log.exception("reclassement après changement de lieu en échec")
                finally:
                    own.close()
            threading.Thread(target=run, daemon=True, name="lieu").start()
            return None, True
        try:
            return relocate(db, tx, s, changed), False
        except Exception:   # lieu déjà enregistré ; réseau en panne : pas de transaction laissée ouverte
            db.rollback()
            log.exception("reclassement après changement de lieu en échec")
            return 0, False

    class PreviousLocation(BaseModel):
        asset_id: str
        lat: float | None = None
        lon: float | None = None
        geo_user: int | None = None

    @app.post("/api/assets/location-undo", dependencies=[Depends(auth)])
    def undo_location(items: list[PreviousLocation]):
        """Annulation d'un lieu indiqué à la main : les valeurs d'avant, puis le reclassement."""
        db.executemany("UPDATE asset SET lat = ?, lon = ?, geo_user = ? WHERE asset_id = ?",
                       [(p.lat, p.lon, p.geo_user, p.asset_id) for p in items])
        db.commit()
        reclassify([p.asset_id for p in items])
        return {"restored": len(items)}

    @app.get("/api/search", dependencies=[Depends(auth)])
    def search(q: str = Query("", max_length=100)):
        """Lieux, appareils et fichiers dont le nom contient q (les taxons sont cherchés dans le navigateur)."""
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
        """Annulation d'une relecture en lot : l'état d'avant de chaque détection, en une transaction."""
        for st in items:
            if st.status not in ("pending", "confirmed", "rejected", "deleted") or st.confirmed_by not in (None, "auto", "user"):
                fail(400, "bad_state", "état invalide")
        db.executemany("UPDATE detection SET status = ?, confirmed_by = ?, taxon_key = ?, taxon_rank = ?, reviewed_at = ? WHERE id = ?",
                       [(st.status, st.confirmed_by, st.taxon_key, st.taxon_rank, st.reviewed_at, st.id) for st in items])
        db.commit()
        return {"restored": len(items)}

    class UndoIn(BaseModel):
        changed: list[RestoreIn] = []
        created: list[int] = []

    @app.post("/api/photos/bulk-undo", dependencies=[Depends(auth)])
    def bulk_undo(body: UndoIn):
        """Annulation d'une action en lot : états d'avant remis, détections créées (« image entière ») effacées."""
        restore_many(body.changed)
        for i in range(0, len(body.created), 500):
            chunk = body.created[i:i + 500]
            db.execute(f"DELETE FROM detection WHERE source = 'manual' AND id IN ({','.join('?' * len(chunk))})", chunk)
        db.commit()
        return {"restored": len(body.changed), "deleted": len(body.created)}
