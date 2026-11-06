"""Series (bursts): photos from the same camera taken less than SERIES_GAP seconds apart, step by
step, and less than SERIES_KM apart if both are geotagged. A series lasts at most SERIES_MAX_SPAN seconds from its
first to its last photo: without this bound, a walk photographed every minute formed a single
"series" of several hours and kilometres, where smoothing replaced a robin at 92 % by the blackbird from the start.

Smoothing per series, modelled on the MegaDetector post-processing (classification_postprocessing,
smooth_classification_results_sequence_level: "a deer/deer/deer/elk/deer/deer/deer/deer sequence is just
a deer"), applied only to detections still awaiting review:
- only proposals at species rank with at least MIN_CONF count;
- an isolated species (a single photo) becomes the dominant species if that one is proposed on at least
  MIN_TO_FLIP photos and is among its candidates with a probability of at least MIN_FLIP_RATIO times that of
  the proposed species: a confident model (92 %) is not contradicted by a marginal candidate (3 %);
- a proposal already coming from smoothing does not vote for the dominant species (otherwise it would reinforce itself);
- a vague proposal at genus, family or order rank becomes the dominant species if that one is on at least
  MIN_TO_REFINE photos and belongs to this taxon. MegaDetector goes further (an "animal" proposal takes
  the dominant species): its camera-trap sequences usually hold a single species, a photo library often mixes
  them (an "Animalia" detection on a hen would become "cat" if cats dominate the series).
A series needs a known camera (make or model): without it (screenshots, received images, scans), photos
imported together look as if taken at the same moment.
Photos of a burst look alike: a model error often repeats across them. Smoothing therefore fixes
inconsistencies, it does not make a proposal more certain: auto-confirmation ignores it. The
original model proposal stays saved (model_key), and the detection keeps a record of the smoothing
(smoothed; series_n: photos of the series proposing the same species).
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
    """Two consecutive photos (same camera, sorted by date) of the same series."""
    near = p["lat"] is None or q["lat"] is None or km(p, q) <= SERIES_KM
    return abs(q["t"] - p["t"]) <= SERIES_GAP and near


def chains(rows: list[dict]) -> list[list[dict]]:
    """Split into series the photos of one camera sorted by date (key "t", in seconds): step by step
    (linked), and never more than SERIES_MAX_SPAN seconds between the first and the last photo of a series."""
    out: list[list[dict]] = []
    for r in rows:
        if out and linked(out[-1][-1], r) and r["t"] - out[-1][0]["t"] <= SERIES_MAX_SPAN:
            out[-1].append(r)
        else:
            out.append([r])
    return out


def assign(db: sqlite3.Connection) -> int:
    """Compute the series of the whole photo library (asset.series_id: id of the first photo of the series; NULL for
    a single photo). Return the number of photos whose series changed."""
    rows = [dict(r) for r in db.execute(
        "SELECT asset_id, taken_at, lat, lon, make, model, series_id FROM asset WHERE missing_at IS NULL AND taken_at IS NOT NULL")]
    for r in rows:   # unknown camera or unreadable date: never in a series
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
    db.executemany("UPDATE asset SET series_id = NULL WHERE asset_id = ?", stale)   # no longer in a series (unknown camera…)
    db.commit()
    return len(changes) + len(stale)


def _before(d: dict) -> dict:
    """Proposal from before the smoothing of a smoothed detection: the saved one (presmooth), otherwise, for a smoothing
    from before this column, the original model proposal (model_key) with the plausibility of that candidate."""
    if d["presmooth"]:
        return json.loads(d["presmooth"])
    cands = json.loads(d["candidates"] or "[]")
    plaus = next((c.get("plausibility") for c in cands if c.get("gbif_key") == d["model_key"]), None)
    return {"taxon_key": d["model_key"], "taxon_rank": d["model_rank"], "score": d["model_conf"],
            "plausibility": plaus or d["plausibility"], "frame_support": None}


def smooth(db: sqlite3.Connection, load_taxon) -> int:
    """Smoothing of the detections awaiting review, series by series (see the module docstring). load_taxon(key) -> dict | None
    gives the lineage of a taxon. Return the number of detections changed.

    A detection already smoothed is judged again on its proposal from before the smoothing: if the rule no longer
    justifies it (series split again, safeguards added since), that proposal is restored. Its photo may then no longer
    be in a series at all."""
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
        if d["lissee"]:   # judged on its proposal from before the smoothing
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
                    continue   # already smoothed this way (a smoothing from before presmooth is rewritten once, to save it)
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
        log.info("%d detection(s) smoothed by a series restored to their original proposal", len(reverts))
    return len(flips) + len(reverts)
