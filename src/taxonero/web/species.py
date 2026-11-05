"""Routes des pages Espèces, espèce, rangs supérieurs et Statistiques : fiches, Wikipédia, aires de répartition,
recherche de taxons, photos principales, statistiques et export CSV."""

import csv
import io
import json
import logging
import threading

from fastapi import Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from .. import ranges, refphotos, store, wiki
from ..autoconfirm import auto_threshold
from ..taxonomy import ISO3, to_json
from .context import fail

log = logging.getLogger(__name__)

GBIF_BACKBONE = "d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"


def register(app, c) -> None:
    s, db, tx, auth, lock = c.s, c.db, c.tx, c.auth, c.lock
    taxon, ensure_taxon, asset_json = c.taxon, c.ensure_taxon, c.asset_json
    range_lock, source_link = c.range_lock, c.source_link

    @app.get("/api/taxon/{key}", dependencies=[Depends(auth)])
    def taxon_card(key: int, name: str = ""):
        """Fiche d'un taxon (noms, liens), récupérée si besoin : fiche de comparaison d'un candidat."""
        with lock:
            if not ensure_taxon(key, name or None):
                raise HTTPException(404)
        return taxon(key)

    @app.get("/api/wiki/{key}", dependencies=[Depends(auth)])
    def wiki_panel(key: int, src: str = "wikipedia", lang: str = ""):
        """Article Wikipédia (dans la langue demandée, sinon la première disponible) ou Wikispecies."""
        t = taxon(key)
        if not t:
            raise HTTPException(404)
        if tx.articles_stale(t):   # fiche d'avant la recherche par synonymes, pas encore reprise en tâche de fond
            try:
                t = tx.refresh_articles({**t, "gbif_key": key})
                with lock:
                    store.save_taxon(db, key, json.dumps(t, ensure_ascii=False))
                    db.commit()
            except Exception:
                log.debug("articles indisponibles pour %s", key, exc_info=True)
        if src == "wikispecies":
            url = t.get("wikispecies")
        else:
            links = t.get("wikipedia", {})
            url = links.get(lang) or links.get(s.names_lang) or next(iter(links.values()), None)
        if not url:
            fail(404, "no_page", "pas de page pour ce taxon")
        a = wiki.article(tx, url)
        a["languages"] = sorted(t.get("wikipedia", {}))
        # Langue réellement servie (repli si l'article n'existe pas dans la langue demandée) : le menu l'affiche.
        a["lang"] = next((k for k, v in t.get("wikipedia", {}).items() if v == url), None)
        return a

    @app.get("/api/refphotos/{key}", dependencies=[Depends(auth)])
    def ref_photos(key: int, name: str = ""):
        """Photos de référence du taxon (iNaturalist, licences Creative Commons) : comparaison à la relecture."""
        t = taxon(key)
        try:
            # iNaturalist et GBIF ne classent pas toujours pareil : en dernier recours, synonymes GBIF de même épithète
            # (les autres peuvent désigner un taxon plus large ou voisin : mieux vaut pas de photo qu'un autre animal).
            accepted = (t or {}).get("scientific_name") or name
            return refphotos.photos(tx, [accepted, name], lambda: tx.synonym_names(key, accepted, same_epithet=True))
        except Exception:
            fail(502, "refphotos_unavailable", "iNaturalist injoignable")

    @app.get("/api/range/{key}", dependencies=[Depends(auth)])
    def range_map(key: int):
        """Aire de répartition au format compact (forme de l'hexagone + [x, y, n] par case, Mercator 0..1).
        Normalement déjà en local (pré-chargée par la synchronisation) ; sinon téléchargée une fois."""
        data = ranges.cached(db, key)
        if data is None:
            with range_lock:
                data = ranges.get(tx, key)
        return Response(json.dumps(data, separators=(",", ":")), media_type="application/json",
                        headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/range/{key}/{z}/{x}/{y}", dependencies=[Depends(auth)])
    def range_tile(key: int, z: int, x: int, y: int):
        """Zoom fort : cases de la tuile GBIF z/x/y, à taille constante à l'écran (voir ranges.zoom_tile)."""
        if not (0 <= z <= 18 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise HTTPException(404)
        try:
            data = ranges.zoom_tile(tx, key, z, x, y)
        except Exception:
            fail(502, "range_unavailable", "GBIF injoignable")
        return Response(json.dumps(data, separators=(",", ":")), media_type="application/json",
                        headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/taxa/search", dependencies=[Depends(auth)])
    def taxa_search(q: str):
        if tx.local:
            return tx.local.search(q, s.names_lang)
        out, seen = [], set()
        for r in tx.get("https://api.gbif.org/v1/species/search",
                           {"q": q, "qField": "VERNACULAR", "datasetKey": GBIF_BACKBONE, "limit": 8}).get("results", []):
            key = r.get("nubKey") or r.get("key")
            if key and key not in seen:
                seen.add(key)
                verna = next((v["vernacularName"] for v in r.get("vernacularNames", []) if v.get("language") == ISO3.get(s.names_lang)), "")
                out.append({"gbif_key": key, "name": r.get("canonicalName"), "rank": r.get("rank", "").lower(), "common": verna})
        for r in tx.get("https://api.gbif.org/v1/species/suggest", {"q": q, "limit": 8}):
            if r.get("key") not in seen:
                seen.add(r["key"])
                out.append({"gbif_key": r["key"], "name": r.get("canonicalName"), "rank": r.get("rank", "").lower(), "common": ""})
        order = {"species": 0, "genus": 1, "family": 2, "subspecies": 3}
        out.sort(key=lambda r: order.get(r["rank"], 4))  # tri stable : garde l'ordre de pertinence GBIF
        return out[:12]

    # -- espèces, arbre, carte, stats ----------------------------------------

    def quality(asset_id: str) -> float:
        """Calculée par la synchronisation ; à défaut (photo validée depuis), valeur neutre : la page ne
        télécharge pas de vignettes pour choisir ses couvertures."""
        row = db.execute("SELECT quality FROM image_quality WHERE asset_id = ?", (asset_id,)).fetchone()
        return row["quality"] if row else 0.5

    covers_cache: dict = {}

    def best_covers() -> dict[int, dict]:
        """Photo principale de chaque taxon : celle choisie à la main si elle est toujours validée pour ce taxon,
        sinon le choix automatique (gardé tant qu'aucune détection ni qualité d'image n'a changé)."""
        sig = tuple(db.execute("SELECT count(*), max(id), total(reviewed_at), total(x + y + w + h), "
                               "(SELECT count(*) FROM image_quality), (SELECT total(missing_at) FROM asset) "
                               "FROM detection WHERE status = 'confirmed'").fetchone())
        if covers_cache.get("sig") != sig:
            covers_cache.update(sig=sig, value=compute_covers())
        out = dict(covers_cache["value"])
        for r in db.execute(
                "SELECT c.taxon_key, d.asset_id, d.x, d.y, d.w, d.h FROM cover_choice c "
                "JOIN detection d ON d.asset_id = c.asset_id AND d.taxon_key = c.taxon_key AND d.status = 'confirmed' "
                "JOIN asset a ON a.asset_id = d.asset_id AND a.missing_at IS NULL GROUP BY c.taxon_key"):
            out[r["taxon_key"]] = {"asset_id": r["asset_id"], "box": [r["x"], r["y"], r["w"], r["h"]], "chosen": True}
        return out

    def compute_covers() -> dict[int, dict]:
        """Pour chaque taxon, la photo où le sujet est le mieux visible : taille du rectangle × confiance
        × qualité de l'image (couleurs, netteté). Le repli « image entière » (aucun détecteur n'a localisé
        le sujet) compte comme un rectangle moyen ; un vrai gros plan détecté plein cadre garde tout son poids.
        La qualité n'est calculée (puis mise en cache) que pour les candidates proches de la meilleure."""
        cands: dict[int, list] = {}
        for r in db.execute(
                "SELECT taxon_key, asset_id, x, y, w, h, confirmed_by, "
                "(CASE WHEN detector = 'image' OR (detector IS NULL AND w * h > 0.98) THEN 0.3 ELSE w * h END) "
                "* COALESCE(score, 0.5) AS base FROM detection WHERE status = 'confirmed' AND taxon_key IS NOT NULL "
                "AND asset_id NOT IN (SELECT asset_id FROM asset WHERE missing_at IS NOT NULL)"):
            cands.setdefault(r["taxon_key"], []).append(dict(r))
        out: dict[int, dict] = {}
        for key, lst in cands.items():
            top = max(c["base"] for c in lst)
            close = [c for c in lst if c["base"] >= 0.6 * top] or lst
            if len(close) > 1:
                for c in close:
                    c["base"] *= 0.4 + 0.6 * quality(c["asset_id"])
            best = max(close, key=lambda c: (c["base"], c["confirmed_by"] == "user"))
            out[key] = {"asset_id": best["asset_id"], "box": [best["x"], best["y"], best["w"], best["h"]]}
        return out

    @app.get("/api/species", dependencies=[Depends(auth)])
    def species():
        # Mois (1-12, en UTC comme la page Statistiques), pays et appareils des observations : la page Espèces
        # filtre avec, quand on clique sur un graphique des statistiques.
        rows = db.execute(
            "SELECT d.taxon_key, count(DISTINCT d.asset_id) AS n, max(a.taken_at) AS last, min(a.taken_at) AS first, "
            "json_group_array(DISTINCT CAST(strftime('%m', a.taken_at) AS INTEGER)) AS months, "
            "json_group_array(DISTINCT a.country) AS countries, "
            "json_group_array(DISTINCT trim(coalesce(a.make, '') || ' ' || coalesce(a.model, ''))) AS cameras "
            "FROM detection d JOIN asset a ON a.asset_id = d.asset_id "
            "WHERE d.status = 'confirmed' AND d.taxon_key IS NOT NULL AND a.missing_at IS NULL "
            "GROUP BY d.taxon_key ORDER BY n DESC").fetchall()
        covers = best_covers()
        clean = lambda v: [x for x in json.loads(v) if x is not None]
        return [{**dict(r), "months": clean(r["months"]), "countries": clean(r["countries"]), "cameras": clean(r["cameras"]),
                 "cover": covers.get(r["taxon_key"], {}).get("asset_id"), "cover_box": covers.get(r["taxon_key"], {}).get("box"),
                 "taxon": taxon(r["taxon_key"])} for r in rows]

    @app.get("/api/species/{key}", dependencies=[Depends(auth)])
    def species_assets(key: int):
        ids = [r["asset_id"] for r in db.execute(
            "SELECT DISTINCT d.asset_id FROM detection d JOIN asset a ON a.asset_id = d.asset_id "
            "WHERE d.taxon_key = ? AND d.status = 'confirmed' AND a.missing_at IS NULL ORDER BY a.taken_at DESC", (key,))]
        threshold = auto_threshold(db, s)
        return {"taxon": taxon(key), "assets": [asset_json(i, None, threshold) for i in ids], "cover": best_covers().get(key)}

    HIGHER_RANKS = ("kingdom", "phylum", "class", "order", "family", "genus")

    @app.get("/api/higher/{rank}/{name}", dependencies=[Depends(auth)])
    def higher_page(rank: str, name: str):
        """Page d'un rang supérieur (règne… genre) : sa fiche (récupérée au besoin), ses espèces validées et les
        lieux de leurs photos."""
        if rank not in HIGHER_RANKS:
            raise HTTPException(404)
        members = [x for x in species() if ((x["taxon"] or {}).get("lineage") or {}).get(rank) == name]
        if not members:
            raise HTTPException(404)
        known = next((r["gbif_key"] for r in db.execute("SELECT gbif_key, data FROM taxon")
                      if json.loads(r["data"]).get("scientific_name") == name), None)
        kingdom = (members[0]["taxon"].get("lineage") or {}).get("kingdom")
        with lock:
            key = known or ensure_taxon(None, name, kingdom)
        keys = [x["taxon_key"] for x in members]
        points = [dict(r) for r in db.execute(
            "SELECT DISTINCT a.asset_id, a.lat, a.lon, a.city, a.country FROM detection d JOIN asset a ON a.asset_id = d.asset_id "
            f"WHERE d.status = 'confirmed' AND a.missing_at IS NULL AND a.lat IS NOT NULL AND d.taxon_key IN ({','.join('?' * len(keys))})",
            keys)]
        return {"rank": rank, "name": name, "key": key, "taxon": taxon(key), "species": members, "points": points}

    class CoverIn(BaseModel):
        asset_id: str | None = None   # None : revenir au choix automatique

    @app.put("/api/species/{key}/cover", dependencies=[Depends(auth)])
    def set_cover(key: int, body: CoverIn):
        """Photo principale du taxon (vignettes, arbre, fiche), choisie parmi ses photos validées."""
        if body.asset_id is None:
            db.execute("DELETE FROM cover_choice WHERE taxon_key = ?", (key,))
        else:
            if not db.execute("SELECT 1 FROM detection WHERE taxon_key = ? AND asset_id = ? AND status = 'confirmed'",
                              (key, body.asset_id)).fetchone():
                fail(422, "not_this_species", "cette photo n'est pas validée pour ce taxon")
            db.execute("INSERT OR REPLACE INTO cover_choice VALUES (?, ?)", (key, body.asset_id))
        db.commit()
        return {"cover": best_covers().get(key)}

    higher_pending: set[str] = set()

    def _enrich_higher(names: list[str]) -> None:
        # Rang et règne de chaque nom, d'après la classification des espèces : GBIF a des homonymes.
        where = {}
        for r in db.execute("SELECT data FROM taxon"):
            lin = json.loads(r["data"]).get("lineage") or {}
            for rk in ("kingdom", "phylum", "class", "order", "family", "genus"):
                if lin.get(rk) and lin[rk] not in where:
                    where[lin[rk]] = (rk, lin.get("kingdom") if rk != "kingdom" else None)
        for name in names:
            try:
                rk, kingdom = where.get(name, (None, None))
                t = tx.match(name, kingdom=kingdom, rank=rk)
                if t:
                    with lock:
                        store.save_taxon(db, t.gbif_key, to_json(t))
                        db.commit()
            except Exception as e:
                log.info("fiche de %s non récupérée : %s", name, e)
            finally:
                higher_pending.discard(name)

    class NamesIn(BaseModel):
        names: list[str]

    @app.post("/api/higher-taxa", dependencies=[Depends(auth)])
    def higher_taxa(body: NamesIn):
        """Noms communs et liens des ordres, familles, genres de l'arbre. Ceux qui manquent sont
        récupérés en arrière-plan (GBIF + Wikidata) et apparaîtront au chargement suivant."""
        known = {}
        for r in db.execute("SELECT gbif_key, data FROM taxon"):
            d = json.loads(r["data"])
            known[d["scientific_name"]] = {"gbif_key": r["gbif_key"], "rank": d.get("rank"),
                                           "vernacular": d.get("vernacular", {}), "has_wiki": bool(d.get("wikipedia"))}
        missing = [n for n in dict.fromkeys(body.names) if n and n not in known and n not in higher_pending]
        if missing:
            higher_pending.update(missing)
            threading.Thread(target=_enrich_higher, args=(missing,), daemon=True).start()
        return {n: known[n] for n in body.names if n in known}

    @app.get("/api/stats", dependencies=[Depends(auth)])
    def stats():
        sp = species()
        by_class: dict[str, int] = {}
        iucn: dict[str, int] = {}
        for r in sp:
            t = r["taxon"] or {}
            c = t.get("lineage", {}).get("class") or t.get("lineage", {}).get("kingdom") or "?"
            by_class[c] = by_class.get(c, 0) + 1
            if t.get("iucn"):
                iucn[t["iucn"]] = iucn.get(t["iucn"], 0) + 1
        gear = [dict(r) for r in db.execute(
            "SELECT a.make, a.model, a.lens, count(DISTINCT a.asset_id) AS n FROM asset a JOIN detection d "
            "ON d.asset_id = a.asset_id WHERE d.status = 'confirmed' AND a.missing_at IS NULL GROUP BY 1, 2, 3 ORDER BY n DESC")]
        points = [dict(r) for r in db.execute(
            "SELECT a.asset_id, a.lat, a.lon, a.taken_at, d.taxon_key FROM asset a JOIN detection d "
            "ON d.asset_id = a.asset_id WHERE d.status = 'confirmed' AND a.lat IS NOT NULL AND a.missing_at IS NULL")]
        return {"species": len(sp), "by_class": by_class, "iucn": iucn, "gear": gear, "points": points}

    @app.get("/api/observations", dependencies=[Depends(auth)])
    def observations():
        """Observations validées (une par photo et par taxon) avec date, lieu et matériel :
        la page Statistiques calcule ses graphiques à partir de cette liste."""
        rows = db.execute(
            "SELECT DISTINCT d.taxon_key, a.asset_id, a.taken_at, a.lat, a.lon, a.city, a.state, a.country, "
            "a.make, a.model, a.lens FROM detection d JOIN asset a ON a.asset_id = d.asset_id "
            "WHERE d.status = 'confirmed' AND d.taxon_key IS NOT NULL AND a.missing_at IS NULL ORDER BY a.taken_at").fetchall()
        keys = {r["taxon_key"] for r in rows}
        return {"observations": [dict(r) for r in rows], "taxa": {k: taxon(k) for k in keys}}

    @app.get("/api/review-stats", dependencies=[Depends(auth)])
    def review_stats():
        """Bilan de la relecture et de l'auto-validation."""
        q = lambda sql: db.execute(sql).fetchone()[0]
        return {
            "auto": q("SELECT count(*) FROM detection WHERE status = 'confirmed' AND confirmed_by = 'auto'"),
            "user": q("SELECT count(*) FROM detection WHERE status = 'confirmed' AND confirmed_by = 'user'"),
            "pending": q("SELECT count(*) FROM detection WHERE status = 'pending'"),
            "rejected": q("SELECT count(*) FROM detection WHERE status = 'rejected'"),
            # Relectures où tu as gardé la proposition du modèle / choisi une autre espèce.
            "kept": q("SELECT count(*) FROM detection WHERE status = 'confirmed' AND confirmed_by = 'user' AND taxon_key = model_key"),
            "corrected": q("SELECT count(*) FROM detection WHERE status = 'confirmed' AND confirmed_by = 'user' AND taxon_key IS NOT model_key"),
            "deleted": q("SELECT count(*) FROM detection WHERE status = 'deleted'"),
            "manual_boxes": q("SELECT count(*) FROM detection WHERE source = 'manual'"),
            # Contrôles : détections qui auraient été auto-validées, relues par toi, et combien étaient justes.
            "audits": q("SELECT count(*) FROM detection WHERE audit = 1 AND confirmed_by = 'user' AND status != 'deleted'"),
            "audits_ok": q("SELECT count(*) FROM detection WHERE audit = 1 AND status = 'confirmed' AND confirmed_by = 'user' "
                           "AND taxon_key = model_key"),
            "by_rank": {r[0] or "none": r[1] for r in db.execute(
                "SELECT taxon_rank, count(*) FROM detection WHERE status = 'confirmed' GROUP BY 1")},
        }

    CSV_COLUMNS = {
        "fr": ["asset_id", "fichier", "date", "lat", "lon", "nom_scientifique", "nom_commun", "rang", "classe", "ordre",
               "famille", "uicn", "confiance", "validé_par", "wikipedia", "lien_immich"],
        "en": ["asset_id", "file", "date", "lat", "lon", "scientific_name", "common_name", "rank", "class", "order",
               "family", "iucn", "confidence", "confirmed_by", "wikipedia", "immich_link"],
    }

    def cell(v):
        """Texte qu'un tableur prendrait pour une formule (=, +, -, @ en tête : noms venus de GBIF/Wikidata, noms de
        fichiers) : précédé d'une apostrophe. Les nombres restent des nombres."""
        return f"'{v}" if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v

    def csv_export(lang: str = "", only: list[str] | None = None):
        """Observations validées. lang : langue du nom commun et des articles (colonnes en français pour fr,
        en anglais sinon). only : seulement ces photos."""
        keep = set(only) if only is not None else None
        lang = lang if lang in s.langs else s.names_lang
        buf = io.StringIO()
        out = csv.writer(buf)
        out.writerow(CSV_COLUMNS["fr" if lang == "fr" else "en"])
        for r in db.execute("SELECT d.*, a.file_name, a.taken_at, a.lat, a.lon FROM detection d JOIN asset a "
                            "ON a.asset_id = d.asset_id WHERE d.status = 'confirmed' AND a.missing_at IS NULL ORDER BY a.taken_at"):
            if keep is not None and r["asset_id"] not in keep:
                continue
            t = taxon(r["taxon_key"]) or {}
            lin = t.get("lineage", {})
            out.writerow([cell(v) for v in [r["asset_id"], r["file_name"], r["taken_at"], r["lat"], r["lon"], t.get("scientific_name"),
                          t.get("vernacular", {}).get(lang), r["taxon_rank"], lin.get("class"), lin.get("order"),
                          lin.get("family"), t.get("iucn"), round(r["score"] or 0, 3), r["confirmed_by"],
                          t.get("wikipedia", {}).get(lang, ""), source_link(r["asset_id"]) or ""]])
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=taxonero.csv"})

    class ExportIn(BaseModel):
        asset_ids: list[str]

    @app.get("/api/export.csv", dependencies=[Depends(auth)])
    def export(lang: str = ""):
        return csv_export(lang)

    @app.post("/api/export.csv", dependencies=[Depends(auth)])
    def export_selection(body: ExportIn, lang: str = ""):
        """Même fichier, limité aux photos sélectionnées."""
        return csv_export(lang, body.asset_ids)
