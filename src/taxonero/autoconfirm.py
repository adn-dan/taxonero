"""Auto-confirmation: threshold (rule, calibration on the reviews), reliability per group of life, reasons
why a detection stays to review, draw of the check detections."""

import hashlib
import json
import math
import sqlite3

from .config import Settings
from .pipeline import AMBIGUOUS_WITH_DOMESTIC, Analysis, Box


def wilson_lower(ok: int, n: int, z: float = 1.64) -> float:
    if n == 0:
        return 0.0
    p = ok / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n)


def calibrate(db: sqlite3.Connection, target: float, min_reviews: int) -> dict:
    """Minimum threshold such that the precision of the model proposals, measured on your reviews,
    reaches the target (lower Wilson bound at 95 % one-sided, so as not to get carried away on few cases)."""
    rows = db.execute(
        "SELECT model_conf, model_key, taxon_key, status FROM detection "
        "WHERE confirmed_by = 'user' AND status != 'deleted' AND model_key IS NOT NULL AND model_rank = 'species'").fetchall()
    n = len(rows)
    audits = db.execute("SELECT count(*) FROM detection WHERE audit = 1 AND confirmed_by = 'user'").fetchone()[0]
    out = {"reviews": n, "min_reviews": min_reviews, "threshold": None, "precision": None, "coverage": None,
           "audits": audits}
    if n < min_reviews:
        return out
    for thr in [x / 100 for x in range(50, 100)]:
        sel = [r for r in rows if r["model_conf"] >= thr]
        if len(sel) < 20:
            break
        ok = sum(1 for r in sel if r["status"] == "confirmed" and r["taxon_key"] == r["model_key"])
        if wilson_lower(ok, len(sel)) >= target:
            out.update(threshold=thr, precision=ok / len(sel), coverage=len(sel) / n)
            break
    return out


def auto_threshold(db: sqlite3.Connection, s: Settings) -> float | None:
    if s.auto_confirm == "off":
        return None
    if s.auto_confirm == "fixed":
        return s.auto_confirm_threshold
    calibrated = calibrate(db, s.auto_confirm_target_precision, s.auto_confirm_min_reviews)["threshold"]
    if s.auto_confirm == "calibrated":
        return calibrated
    # 'rule': measured on the dev set, 0.85 + green badge + no credible runner-up gives
    # 100 % precision excluding doubtful labels. Calibration can only make it stricter.
    return max(s.auto_confirm_threshold, calibrated or 0.0)


# Reliability per group: the confidence rule is not enough for groups where the model is often wrong
# with confidence. Without reviews, the groups measured as weak on the dev set (spiders 0/3, fish
# 1/3) are never auto-confirmed; from GROUP_MIN_REVIEWS reviews of a group (proposals above
# the threshold), they decide: precision at least equal to AUTO_CONFIRM_TARGET_PRECISION.
WEAK_GROUPS = ("arachnida", "pisces")
GROUP_MIN_REVIEWS = 10
_FISH = {"Actinopterygii", "Chondrichthyes", "Elasmobranchii", "Holocephali", "Sarcopterygii", "Dipneusti", "Actinistia",
         "Myxini", "Petromyzonti", "Cephalaspidomorphi"}
_REPTILES = {"Reptilia", "Squamata", "Testudines", "Crocodylia"}


def group_of(lineage: dict | None) -> str | None:
    """Group of life of a taxon, same identifiers as the dashboard (group.<id>) for the main ones."""
    L = lineage or {}
    cls, kingdom = L.get("class"), L.get("kingdom")
    if kingdom in ("Plantae", "Fungi"):
        return kingdom.lower()
    if cls in _FISH or (L.get("phylum") == "Chordata" and cls in (None, "Chordata")):
        return "pisces"
    if cls in _REPTILES:
        return "reptilia"
    return cls.lower() if cls else None


def group_reliability(db: sqlite3.Connection, s: Settings) -> dict[str, dict]:
    """Per group: reviews of the model proposals above the threshold, correct ones, and auto-confirmation
    allowed or not."""
    rows = db.execute(
        "SELECT d.model_key, d.taxon_key, d.status, t.data FROM detection d LEFT JOIN taxon t ON t.gbif_key = d.model_key "
        "WHERE d.confirmed_by = 'user' AND d.status != 'deleted' AND d.model_key IS NOT NULL AND d.model_rank = 'species' "
        "AND d.model_conf >= ?", (s.auto_confirm_threshold,)).fetchall()
    stats: dict[str, list[int]] = {g: [0, 0] for g in WEAK_GROUPS}
    for r in rows:
        g = group_of(json.loads(r["data"]).get("lineage") if r["data"] else None)
        if g:
            n_ok = stats.setdefault(g, [0, 0])
            n_ok[0] += 1
            n_ok[1] += r["status"] == "confirmed" and r["taxon_key"] == r["model_key"]
    out = {}
    for g, (n, ok) in stats.items():
        auto = ok / n >= s.auto_confirm_target_precision if n >= GROUP_MIN_REVIEWS else g not in WEAK_GROUPS
        out[g] = {"reviews": n, "ok": ok, "auto": auto, "measured": n >= GROUP_MIN_REVIEWS}
    return out


def blocked_groups(db: sqlite3.Connection, s: Settings) -> set[str]:
    return {g for g, v in group_reliability(db, s).items() if not v["auto"]}


# Single subject whose kept species has less than 5 % on the crop: it comes from the whole photo, the box may
# be next to the subject. Measured on 49 single-subject dev photos: 3 cases (one right species with a misplaced box,
# two errors), never above the confirmation threshold: no correct auto-confirmation lost.
FRAME_MIN_SUPPORT = 0.05


def auto_blockers(rank: str | None, plausibility: str | None, confidence: float | None, name: str | None,
                  runner_up: float, box_label: str | None, threshold: float | None, max_runner_up: float,
                  group: str | None = None, blocked: set[str] = frozenset(),
                  frame_support: float | None = None) -> list[dict]:
    """Reasons why a detection is not auto-confirmed (empty: it is), as codes
    with their values: the dashboard translates them."""
    if threshold is None:
        return [{"code": "auto_off"}]
    out = []
    if rank != "species":
        out.append({"code": "not_species"})
    if plausibility != "green":
        out.append({"code": "not_near" if plausibility in ("amber", "red") else "unverified" if plausibility == "unverified" else "no_gps"})
    if (confidence or 0.0) < threshold:
        out.append({"code": "low_confidence", "conf": confidence or 0.0, "thr": threshold})
    if runner_up > max_runner_up:
        out.append({"code": "runner_up", "p": runner_up})
    if name in AMBIGUOUS_WITH_DOMESTIC:   # a dog taken for a wolf must not be tagged without review
        out.append({"code": "domestic"})
    if box_label == "image":              # never the "whole image" fallback
        out.append({"code": "whole_image"})
    if group in blocked:                  # group where the model is often confidently wrong
        out.append({"code": "group_unreliable", "group": group})
    if frame_support is not None and frame_support < FRAME_MIN_SUPPORT:   # species seen on the whole photo only
        out.append({"code": "frame"})
    return out


def should_auto_confirm(a: Analysis, threshold: float | None, max_runner_up: float = 0.05,
                        blocked: set[str] = frozenset()) -> bool:
    d = a.decision
    runner_up = d.candidates[1].posterior if len(d.candidates) > 1 else 0.0
    group = group_of(d.candidates[0].lineage) if d.candidates else None
    return not auto_blockers(d.rank, d.plausibility, d.confidence, d.name, runner_up, a.box.label, threshold,
                             max_runner_up, group, blocked, a.frame_support)


def audit_pick(asset_id: str, box: Box, rate: float) -> bool:
    """Reproducible draw (same photo, same box: same result) of a check detection."""
    h = hashlib.sha256(f"{asset_id}:{box.x:.3f}:{box.y:.3f}".encode()).digest()
    return int.from_bytes(h[:4], "big") / 2 ** 32 < rate
