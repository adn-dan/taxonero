"""Séries (rafales) : photos du même appareil prises à moins de SERIES_GAP secondes l'une de l'autre, de proche en
proche, et à moins de SERIES_KM si les deux sont géolocalisées. Une série dure au plus SERIES_MAX_SPAN secondes de sa
première à sa dernière photo : sans cette borne, une balade photographiée toutes les minutes formait une seule
« série » de plusieurs heures et kilomètres, où le lissage remplaçait un rouge-gorge sûr à 92 % par le merle du début.

Lissage par série, sur le modèle du post-traitement de MegaDetector (classification_postprocessing,
smooth_classification_results_sequence_level : « une séquence cerf/cerf/cerf/élan/cerf/cerf/cerf/cerf, c'est juste
un cerf »), appliqué seulement aux détections encore à relire :
- ne comptent que les propositions au rang espèce à au moins MIN_CONF ;
- une espèce isolée (une seule photo) devient l'espèce dominante si celle-ci est proposée sur au moins
  MIN_TO_FLIP photos et figure parmi ses candidats avec une probabilité d'au moins MIN_FLIP_RATIO fois celle de
  l'espèce proposée : un modèle sûr de lui (92 %) n'est pas contredit par un candidat marginal (3 %) ;
- une proposition déjà issue du lissage ne vote pas pour l'espèce dominante (sinon elle se renforcerait d'elle-même) ;
- une proposition vague au rang genre, famille ou ordre devient l'espèce dominante si celle-ci est sur au moins
  MIN_TO_REFINE photos et appartient à ce taxon. MegaDetector va plus loin (une proposition « animal » prend
  l'espèce dominante) : ses séquences de pièges photo n'ont en général qu'une espèce, une photothèque en mélange
  souvent (une détection « Animalia » sur une poule deviendrait « chat » si les chats dominent la série).
Une série demande un appareil connu (marque ou modèle) : sans lui (captures, images reçues, scans), des photos
importées ensemble paraissent prises au même moment.
Les photos d'une rafale se ressemblent : une erreur du modèle s'y répète souvent. Le lissage corrige donc les
incohérences, il ne rend pas une proposition plus sûre : la validation automatique n'en tient pas compte. La
proposition d'origine du modèle reste enregistrée (model_key), et la détection garde la trace du lissage
(smoothed ; series_n : photos de la série qui proposent la même espèce).
"""

import json
import logging
import math
import sqlite3
from datetime import datetime

SERIES_GAP = 120
SERIES_KM = 1.0
SERIES_MAX_SPAN = 600
MIN_CONF = 0.5
MIN_TO_FLIP = 4
MIN_FLIP_RATIO = 0.25
MIN_TO_REFINE = 2
log = logging.getLogger(__name__)

RANKS = ("kingdom", "phylum", "class", "order", "family", "genus")


def seconds(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() if iso else None
    except ValueError:
        return None


def km(a, b) -> float:
    dlat, dlon = math.radians(b["lat"] - a["lat"]), math.radians(b["lon"] - a["lon"])
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a["lat"])) * math.cos(math.radians(b["lat"])) * math.sin(dlon / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def linked(p: dict, q: dict) -> bool:
    """Deux photos consécutives (même appareil, triées par date) de la même série."""
    near = p["lat"] is None or q["lat"] is None or km(p, q) <= SERIES_KM
    return abs(q["t"] - p["t"]) <= SERIES_GAP and near


def chains(rows: list[dict]) -> list[list[dict]]:
    """Découpe en séries des photos d'un même appareil triées par date (clé « t », en secondes) : de proche en proche
    (linked), et jamais plus de SERIES_MAX_SPAN secondes entre la première et la dernière photo d'une série."""
    out: list[list[dict]] = []
    for r in rows:
        if out and linked(out[-1][-1], r) and r["t"] - out[-1][0]["t"] <= SERIES_MAX_SPAN:
            out[-1].append(r)
        else:
            out.append([r])
    return out


def assign(db: sqlite3.Connection) -> int:
    """Calcule les séries de toute la photothèque (asset.series_id : id de la première photo de la série ; NULL pour
    une photo seule). Renvoie le nombre de photos dont la série a changé."""
    rows = [dict(r) for r in db.execute(
        "SELECT asset_id, taken_at, lat, lon, make, model, series_id FROM asset WHERE missing_at IS NULL AND taken_at IS NOT NULL")]
    for r in rows:   # appareil inconnu ou date illisible : jamais en série
        r["t"] = seconds(r["taken_at"]) if r["make"] or r["model"] else None
    stale = [(r["asset_id"],) for r in rows if r["t"] is None and r["series_id"]]
    rows = sorted((r for r in rows if r["t"] is not None), key=lambda r: (r["make"] or "", r["model"] or "", r["t"]))
    cameras: dict[tuple, list[dict]] = {}
    for r in rows:
        cameras.setdefault((r["make"], r["model"]), []).append(r)
    wanted: dict[str, str | None] = {}
    for same_camera in cameras.values():
        for chain in chains(same_camera):
            for r in chain:
                wanted[r["asset_id"]] = chain[0]["asset_id"] if len(chain) > 1 else None
    changes = [(sid, r["asset_id"]) for r in rows if (sid := wanted.get(r["asset_id"])) != r["series_id"]]
    db.executemany("UPDATE asset SET series_id = ? WHERE asset_id = ?", changes)
    db.executemany("UPDATE asset SET series_id = NULL WHERE asset_id = ?", stale)   # plus en série (appareil inconnu…)
    db.commit()
    return len(changes) + len(stale)


def _before(d: dict) -> dict:
    """Proposition d'avant le lissage d'une détection lissée : celle enregistrée (presmooth), sinon, pour un lissage
    d'avant cette colonne, la proposition d'origine du modèle (model_key) avec la plausibilité de ce candidat."""
    if d["presmooth"]:
        return json.loads(d["presmooth"])
    cands = json.loads(d["candidates"] or "[]")
    plaus = next((c.get("plausibility") for c in cands if c.get("gbif_key") == d["model_key"]), None)
    return {"taxon_key": d["model_key"], "taxon_rank": d["model_rank"], "score": d["model_conf"],
            "plausibility": plaus or d["plausibility"], "frame_support": None}


def smooth(db: sqlite3.Connection, load_taxon) -> int:
    """Lissage des détections à relire, série par série (voir le docstring du module). load_taxon(key) -> dict | None
    donne la lignée d'un taxon. Renvoie le nombre de détections modifiées.

    Une détection déjà lissée est jugée de nouveau sur sa proposition d'avant le lissage : si la règle ne le justifie
    plus (série redécoupée, garde-fous ajoutés depuis), cette proposition est rétablie. Sa photo peut alors ne plus
    être dans une série du tout."""
    keep = ("taxon_key", "taxon_rank", "score", "plausibility", "frame_support")
    groups: dict[str, list[dict]] = {}
    for r in db.execute(
            "SELECT d.id, d.asset_id, d.status, d.taxon_key, d.taxon_rank, d.score, d.plausibility, d.frame_support, "
            "d.candidates, d.series_n, d.smoothed, d.presmooth, d.model_key, d.model_rank, d.model_conf, a.series_id "
            "FROM detection d JOIN asset a ON a.asset_id = d.asset_id "
            "WHERE a.missing_at IS NULL AND d.status IN ('pending', 'confirmed') "
            "AND (a.series_id IS NOT NULL OR (d.smoothed = 1 AND d.status = 'pending'))"):
        d = dict(r)
        d["lissee"] = d["status"] == "pending" and d["smoothed"] == 1
        if d["lissee"]:   # jugée sur sa proposition d'avant le lissage
            d["now"] = {k: d[k] for k in keep}
            d.update(_before(d))
        groups.setdefault(d["series_id"] or f"seule-{d['id']}", []).append(d)
    flips, reverts, supports = [], [], []
    for group in groups.values():
        photos: dict[int, set[str]] = {}
        for d in group:
            if d["taxon_rank"] == "species" and d["taxon_key"] and (d["score"] or 0) >= MIN_CONF:
                photos.setdefault(d["taxon_key"], set()).add(d["asset_id"])
        counts = {k: len(v) for k, v in photos.items()}
        dominant = max(counts, key=counts.get) if counts else None
        n_dom = counts.get(dominant, 0)
        lineage = (load_taxon(dominant) or {}).get("lineage", {}) if dominant else {}
        for d in group:
            n = counts.get(d["taxon_key"], 0) if d["taxon_rank"] == "species" else 0
            target = None
            if d["status"] == "pending" and dominant and d["taxon_key"] != dominant:
                cands = json.loads(d["candidates"] or "[]")
                c = next((c for c in cands if c.get("gbif_key") == dominant), None)
                species = d["taxon_rank"] == "species" and (d["score"] or 0) >= MIN_CONF
                credible = c is not None and c.get("p", 0) >= MIN_FLIP_RATIO * (d["score"] or 0)
                parent = (load_taxon(d["taxon_key"]) or {}).get("scientific_name") if d["taxon_key"] else None
                vague = d["taxon_rank"] in ("genus", "family", "order")
                if ((species and n_dom >= MIN_TO_FLIP and counts.get(d["taxon_key"], 0) <= 1 and credible)
                        or (vague and n_dom >= MIN_TO_REFINE and parent and lineage.get(d["taxon_rank"]) == parent)):
                    c = c or {}
                    target = (c.get("p", d["score"]), c.get("plausibility"))
            if target:
                if d["lissee"] and d["now"]["taxon_key"] == dominant and d["series_n"] == n_dom and d["presmooth"]:
                    continue   # déjà lissée ainsi (un lissage d'avant presmooth est réécrit une fois, pour l'enregistrer)
                before = json.dumps({k: d[k] for k in keep})
                flips.append((dominant, target[0], target[1], n_dom, before, d["id"]))
            elif d["lissee"]:
                reverts.append((d["taxon_key"], d["taxon_rank"], d["score"], d["plausibility"], d["frame_support"], n or None, d["id"]))
            elif (d["series_n"] or 0) != n:
                supports.append((n or None, d["id"]))
    db.executemany("UPDATE detection SET taxon_key = ?, taxon_rank = 'species', score = ?, plausibility = COALESCE(?, plausibility), "
                   "series_n = ?, smoothed = 1, presmooth = ?, frame_support = NULL WHERE id = ?", flips)
    db.executemany("UPDATE detection SET taxon_key = ?, taxon_rank = ?, score = ?, plausibility = ?, frame_support = ?, "
                   "series_n = ?, smoothed = NULL, presmooth = NULL WHERE id = ?", reverts)
    db.executemany("UPDATE detection SET series_n = ? WHERE id = ?", supports)
    db.commit()
    if reverts:
        log.info("%d détection(s) lissée(s) par une série rendue(s) à leur proposition d'origine", len(reverts))
    return len(flips) + len(reverts)
