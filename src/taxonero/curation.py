"""Detections awaiting review already in the database: format of the saved candidates, and update when a setting or the place
changes (excluded candidates, unticked groups, new position), without a new model pass."""

import json
import sqlite3

from . import store
from .config import Settings
from .pipeline import ALL_GROUPS, MICROBE_KINGDOMS, VISIBLE_PHYLUM, Candidate, decide, renormalize, rerank
from .taxonomy import Taxonomy, to_json

EXCLUDED = {"Homo sapiens"}
# Group of a detection (animal, plant, fungus, microbe), like pipeline.group_of_result: kingdom of the top candidate,
# otherwise the box label.
_K = "json_extract(candidates, '$[0].lineage.kingdom')"
_P = "COALESCE(json_extract(candidates, '$[0].lineage.phylum'), '')"
_MICROBE = (f"{_K} IN ({', '.join(repr(k) for k in MICROBE_KINGDOMS)}) OR "
            + " OR ".join(f"({_K} = '{k}' AND {_P} != '{p}')" for k, p in VISIBLE_PHYLUM.items()))
DETECTION_GROUP = (f"CASE WHEN {_MICROBE} THEN 'microbe' WHEN {_K} IN ('Animalia', 'Metazoa') THEN 'animal' WHEN {_K} = 'Fungi' THEN 'fungus' "
                   f"WHEN {_K} IS NOT NULL THEN 'plant' WHEN box_label IN ('plant', 'fungus') THEN box_label ELSE 'animal' END")


def cand_json(c: Candidate) -> dict:
    """Candidate as saved in the database (p: probability shown, p0: before the settings filter)."""
    return {"name": c.accepted_name or c.name, "gbif_key": c.gbif_key, "p": round(c.posterior, 4),
            "p0": round(c.prior, 4), "model_score": round(c.score, 4), "plausibility": c.plausibility,
            "lineage": c.lineage}


def cand_from_json(c: dict) -> Candidate:
    return Candidate(c["name"], c.get("model_score", 0.0), c.get("lineage") or {}, c.get("gbif_key"), c["name"],
                     c.get("plausibility") or "unknown", posterior=c.get("p", 0.0), prior=c.get("p0", c.get("p", 0.0)))


# New decision for a detection awaiting review (no longer the one from a series smoothing); frame_support (computed for the old taxon) is kept only if the
# taxon does not change (in an UPDATE, SQLite reads the old values on the right of the "=").
UPDATE_DECISION = ("UPDATE detection SET frame_support = CASE WHEN taxon_key IS ?1 THEN frame_support END, taxon_key = ?1, "
                   "taxon_rank = ?2, score = ?3, margin = ?4, plausibility = ?5, candidates = ?6, candidates_out = ?7, "
                   "smoothed = NULL, presmooth = NULL WHERE id = ?8")


def drop_unwanted(db: sqlite3.Connection, s: Settings) -> int:
    """Automatic detections still awaiting review that the settings now exclude: all of them in manual detection,
    those of the unticked groups otherwise. Reviewed detections (confirmed, rejected) stay. No commit."""
    if s.detection == "manual":
        return db.execute("DELETE FROM detection WHERE source = 'auto' AND status = 'pending'").rowcount
    if s.groups == ALL_GROUPS:
        return 0
    return db.execute(f"DELETE FROM detection WHERE source = 'auto' AND status = 'pending' AND ({DETECTION_GROUP}) "
                      f"NOT IN ({','.join('?' * len(s.groups))})", tuple(s.groups)).rowcount


def candidate_filter(s: Settings, tx: Taxonomy):
    """Predicate "candidate to exclude" according to the settings (extinct species, absent from the region)."""
    def drop(c: Candidate, lat: float | None = None, lon: float | None = None) -> bool:
        """lat, lon: place of the photo (rerank), whose field observations usually settle the fossil test."""
        if s.hide_absent and c.plausibility == "red":
            return True
        return bool(s.exclude_extinct and c.gbif_key and tx.fossil_only(c.gbif_key, lat, lon))
    drop.fossils = s.exclude_extinct   # rerank prepares these requests in parallel
    return drop


def prune_pending(db: sqlite3.Connection, tx: Taxonomy, s: Settings, checkpoint=None) -> int:
    """Apply the candidate filters to the pending detections already in the database: excluded candidates set
    aside (restored if the setting changes), probabilities renormalised, decision recomputed.
    Reviewed detections do not move. checkpoint: called before each detection, during the computation (nothing
    is written yet), to stop the cycle (worker._checkpoint)."""
    drop, n = candidate_filter(s, tx), 0
    rows = db.execute("SELECT id, candidates, candidates_out FROM detection "
                      "WHERE status = 'pending' AND candidates IS NOT NULL").fetchall()
    updates = []
    for r in rows:
        if checkpoint:
            checkpoint()
        shown, out = json.loads(r["candidates"]), json.loads(r["candidates_out"] or "[]")
        everything = [cand_from_json(c) for c in shown + out]
        keep = [not drop(c) for c in everything]
        if keep == [True] * len(shown) + [False] * len(out):
            continue
        kept = renormalize([c for c, k in zip(everything, keep) if k])
        gone = sorted((c for c, k in zip(everything, keep) if not k), key=lambda c: -c.prior)
        d = decide(kept, tx)
        updates.append((d.gbif_key, d.rank, d.confidence, d.margin, d.plausibility,
                        json.dumps([cand_json(c) for c in kept], ensure_ascii=False),
                        json.dumps([cand_json(c) for c in gone], ensure_ascii=False), r["id"]))
    # Decisions are computed first (network access), then written in one block.
    for u in updates:
        db.execute(UPDATE_DECISION, u)
        n += 1
    db.commit()
    return n


BOX_KINGDOM = {"plant": "Plantae", "fungus": "Fungi"}


def relocate(db: sqlite3.Connection, tx: Taxonomy, s: Settings, asset_ids: list[str]) -> int:
    """Place entered or changed by hand: the detections of these photos still awaiting review are reclassified with the
    new position (geographic plausibility), from the saved model scores: no new
    model pass. Reviewed detections do not move. Return the number of detections reclassified."""
    drop, updates = candidate_filter(s, tx), []
    for aid in asset_ids:
        a = db.execute("SELECT lat, lon FROM asset WHERE asset_id = ?", (aid,)).fetchone()
        if not a:
            continue
        for r in db.execute("SELECT id, candidates, candidates_out, box_label, detector FROM detection "
                            "WHERE asset_id = ? AND status = 'pending' AND candidates IS NOT NULL", (aid,)).fetchall():
            raw = [Candidate(c["name"], c.get("model_score") or c.get("p0") or 0.0, dict(c.get("lineage") or {}))
                   for c in json.loads(r["candidates"]) + json.loads(r["candidates_out"] or "[]")]
            if not raw:
                continue
            excluded: list[Candidate] = []
            hint = BOX_KINGDOM.get(r["box_label"]) if r["detector"] == "owl" else None
            ranked = rerank(raw, tx, a["lat"], a["lon"], hint, drop, excluded)
            d = decide(ranked, tx)
            if d.gbif_key and not store.load_taxon(db, d.gbif_key):   # new taxon kept: its record, for display
                t = tx.match(d.name, kingdom=ranked[0].lineage.get("kingdom") if ranked else None)
                if t and t.gbif_key == d.gbif_key:
                    store.save_taxon(db, t.gbif_key, to_json(t))
            updates.append((d.gbif_key, d.rank, d.confidence, d.margin, d.plausibility,
                            json.dumps([cand_json(c) for c in ranked[:8] if c.name not in EXCLUDED], ensure_ascii=False),
                            json.dumps([cand_json(c) for c in excluded[:8] if c.name not in EXCLUDED], ensure_ascii=False), r["id"]))
    # Decisions computed first (network access), then written in one block.
    db.executemany(UPDATE_DECISION, updates)
    db.commit()
    return len(updates)
