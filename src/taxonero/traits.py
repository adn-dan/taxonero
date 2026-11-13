"""Sex and life stage of an individual, for the species in which they show.

Asking BioCLIP 2 in words ("a male…", "a juvenile…") is no better than chance, and so is a model shared by all species
(measured, dev/benchmark_traits.py). What works is one small model per species and trait: a linear separation of the
BioCLIP image embeddings, learnt on a few dozen photos of the species annotated by the iNaturalist community. Its
cross-validated accuracy also says whether the difference is visible at all: mallard, blackbird, stag beetle reach
95 % and more for the sex, robin or blue tit stay near chance and nothing is then proposed for them; caterpillars,
ladybird larvae or young gulls are told from the adults, fawns are not.

The example photos are read once per species, embedded and dropped: only the model (a few KB) is kept, in the
trait_model table. Learnt on whole photos, applied to the frame of the detected animal (measured: same accuracy).
"""

import io
import json
import logging
import sqlite3
import time
from typing import Callable

import httpx
import numpy as np
from PIL import Image

from . import refphotos
from .taxonomy import Taxonomy

log = logging.getLogger(__name__)

API = refphotos.API
SEX, STAGE = "sex", "stage"
# trait -> (iNaturalist annotation, {its value: label}); the trait is also the prefix of its columns in detection.
# A value with too few annotated photos for a species is left out of its model (no larva of a bird).
TRAITS = {SEX: (9, {10: "female", 11: "male"}),
          STAGE: (1, {2: "adult", 8: "juvenile", 6: "larva", 5: "nymph"})}
LABELS = {trait: tuple(values.values()) for trait, (_, values) in TRAITS.items()}
PER_VALUE = 30          # example photos per value: the accuracy no longer rises beyond (measured from 6 to 40)
MIN_PER_VALUE = 15      # fewer annotated photos of a value: not in the model; fewer than two values left: no model
MIN_ACCURACY = 0.9      # cross-validated accuracy below which the values are taken as looking alike
MIN_MARGIN = 0.5        # score below which a photo is left without an answer (profile, far away, in between…)
RIDGE = 0.1
CALIBRATION = 6.5       # slope of the logistic turning the score into a share of right answers (measured, 0.5 -> 96 %)
LICENSES = "cc0,cc-by,cc-by-nc"
RETRY_AFTER = 180 * 86400   # a species without enough annotated photos is asked again after this long
MAX_FAILED_IN_A_ROW = 3     # example photos that cannot be downloaded one after the other: iNaturalist is down, given up


def _normalised(X: np.ndarray) -> np.ndarray:
    X = np.asarray(X, dtype=np.float64)
    return X / np.maximum(np.linalg.norm(X, axis=-1, keepdims=True), 1e-12)


def fit(X: np.ndarray, y: np.ndarray, classes: int = 2) -> np.ndarray:
    """Ridge separation of groups of embeddings (y: 0 … classes - 1), each against the others, in its dual form (few
    photos, many dimensions). One row [w…, c] per group: the score of an embedding x for it is x·w + c, near 1 when
    it belongs to it and -1 otherwise. Two groups: a single row, the one of the group 1 (the other is its opposite)."""
    X = _normalised(X)
    mu, T = X.mean(0), np.where(np.asarray(y)[:, None] == np.arange(classes), 1.0, -1.0)
    A = X - mu
    W = A.T @ np.linalg.solve(A @ A.T + RIDGE * np.eye(len(A)), T - T.mean(0))
    M = np.vstack([W, T.mean(0) - mu @ W]).T.astype(np.float32)
    return M[1] if classes == 2 else M


def scores(model: np.ndarray, X: np.ndarray) -> np.ndarray:
    """Score of each embedding for each group (one column per group)."""
    M = np.atleast_2d(model).astype(np.float64)
    S = _normalised(X) @ M[:, :-1].T + M[:, -1]
    return np.hstack([-S, S]) if len(M) == 1 else S


def accuracy(X: np.ndarray, y: np.ndarray, classes: int = 2, folds: int = 10) -> float:
    """Share of right answers on photos left out of the learning (cross-validation)."""
    y = np.asarray(y)
    order, hits = np.random.default_rng(0).permutation(len(y)), 0
    for f in range(folds):
        test = order[f::folds]
        train = np.setdiff1d(order, test)
        hits += int((scores(fit(X[train], y[train], classes), X[test]).argmax(1) == y[test]).sum())
    return hits / len(y)


def guess(model: np.ndarray, labels: tuple, embedding: np.ndarray) -> tuple[str, float]:
    """(the label the photo leans to, its score): an answer only from MIN_MARGIN on, a leaning below."""
    s = scores(model, embedding[None])[0]
    k = int(s.argmax())
    return labels[k], float(s[k])


def tell(model: np.ndarray, labels: tuple, embedding: np.ndarray) -> tuple[str, float]:
    """(label | '' when the photo does not tell, score of the best label)."""
    label, s = guess(model, labels, embedding)
    return (label if s >= MIN_MARGIN else ""), s


def confidence(margin: float) -> float:
    """Chance that what was told with this score is right: measured on left-out photos of the species whose model
    is usable (96 % at the margin of 0.5, 99 % at 0.75; about 70 % below 0.25, where no answer is given)."""
    return float(1 / (1 + np.exp(-CALIBRATION * margin)))


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS trait_model (taxon_key INTEGER NOT NULL, trait TEXT NOT NULL, n INTEGER NOT NULL, "
               "accuracy REAL, model BLOB, fetched REAL NOT NULL, labels TEXT, PRIMARY KEY (taxon_key, trait))")
    if "labels" not in {r[1] for r in db.execute("PRAGMA table_info(trait_model)")}:
        db.execute("ALTER TABLE trait_model ADD COLUMN labels TEXT")


def model(db: sqlite3.Connection, taxon_key: int, trait: str) -> tuple[np.ndarray, tuple] | None:
    """(model, its labels) of a species, when it is reliable enough to be used."""
    row = db.execute("SELECT accuracy, model, labels FROM trait_model WHERE taxon_key = ? AND trait = ?", (taxon_key, trait)).fetchone()
    if not row or row[1] is None or (row[0] or 0) < MIN_ACCURACY:
        return None
    labels = tuple(json.loads(row[2])) if row[2] else LABELS[trait]
    return np.frombuffer(row[1], dtype=np.float32).reshape(1 if len(labels) == 2 else len(labels), -1), labels


def examples(tx: Taxonomy, inat_id: int, trait: str, embed: Callable[[list[Image.Image]], np.ndarray],
             tick: Callable[[], None] | None = None) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """(embeddings, index of the label of each, labels) of the photos of a species annotated on iNaturalist for this
    trait: research grade observations, the best voted first, first photo of each, Creative Commons licences only.
    Only the values with enough photos are kept."""
    term, values = TRAITS[trait]
    X, y, labels, failed = [], [], [], 0
    for value, label in values.items():
        found = tx._fetch(str(httpx.URL(f"{API}/observations", params={
            "quality_grade": "research", "taxon_id": inat_id, "term_id": term, "term_value_id": value,
            "photo_license": LICENSES, "per_page": PER_VALUE * 2, "order_by": "votes"})))
        results = found.get("results", [])
        if len(results) < MIN_PER_VALUE:   # not worth downloading
            continue
        images = []
        for obs in results:
            if len(images) >= PER_VALUE:
                break
            url = ((obs.get("photos") or [{}])[0].get("url") or "").replace("/square.", "/medium.")
            if not url:
                continue
            if tick:
                tick()
            try:
                images.append(Image.open(io.BytesIO(tx.fetch_bytes(url))).convert("RGB"))
                failed = 0
            except (httpx.HTTPStatusError, OSError):   # photo removed, unreadable file: the next one
                continue
            except httpx.TransportError:   # connection cut on this photo: the next one, unless nothing comes any more
                failed += 1
                if failed >= MAX_FAILED_IN_A_ROW:
                    raise
        if len(images) >= MIN_PER_VALUE:
            X.append(np.asarray(embed(images)))
            y += [len(labels)] * len(images)
            labels.append(label)
    return (np.concatenate(X) if X else np.zeros((0, 1))), np.array(y, dtype=int), labels


def learn(tx: Taxonomy, taxon_key: int, names: list[str], trait: str, embed, tick=None) -> dict:
    """Learn the model of a species and store it, usable or not (a species in which nothing shows is not asked again;
    one without enough annotated photos is, after RETRY_AFTER). Network failures go up to the caller: nothing is
    stored, the species is tried again at the next cycle (a photo that fails alone is only skipped)."""
    ensure_schema(tx.db)
    taxon = refphotos.inat_taxon(tx, names)
    X, y, labels = examples(tx, taxon["id"], trait, embed, tick) if taxon else (np.zeros((0, 1)), np.array([], dtype=int), [])
    out: dict = {"n": len(y), "accuracy": None, "model": None, "labels": labels}
    if len(labels) >= 2 and (trait != SEX or len(labels) == len(LABELS[SEX])):
        out["accuracy"] = accuracy(X, y, len(labels))
        out["model"] = fit(X, y, len(labels)).tobytes()
    tx.db.execute("INSERT OR REPLACE INTO trait_model (taxon_key, trait, n, accuracy, model, fetched, labels) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (taxon_key, trait, out["n"], out["accuracy"], out["model"], time.time(), json.dumps(labels)))
    tx.db.commit()
    return out


def to_learn(db: sqlite3.Connection, trait: str, limit: int) -> list[int]:
    """Animal species with confirmed photos and no model yet, the most photographed first."""
    ensure_schema(db)
    return [r[0] for r in db.execute(
        "SELECT d.taxon_key FROM detection d JOIN taxon t ON t.gbif_key = d.taxon_key "
        "LEFT JOIN trait_model m ON m.taxon_key = d.taxon_key AND m.trait = ? "
        "WHERE d.status = 'confirmed' AND d.taxon_rank = 'species' AND json_extract(t.data, '$.lineage.kingdom') = 'Animalia' "
        "AND (m.taxon_key IS NULL OR (m.model IS NULL AND m.fetched < ?)) "
        "GROUP BY d.taxon_key ORDER BY count(*) DESC LIMIT ?", (trait, time.time() - RETRY_AFTER, limit))]


def to_tell(db: sqlite3.Connection, trait: str, limit: int) -> list[sqlite3.Row]:
    """Confirmed detections of a species with a reliable model, not looked at yet for this trait (or looked at for
    another species, before a correction). What the user set by hand is never touched."""
    assert trait in TRAITS   # column prefix
    ensure_schema(db)
    return db.execute(
        "SELECT d.id, d.asset_id, d.taxon_key, d.x, d.y, d.w, d.h, d.box_label FROM detection d "
        "JOIN trait_model m ON m.taxon_key = d.taxon_key AND m.trait = ? AND m.model IS NOT NULL AND m.accuracy >= ? "
        "JOIN asset a ON a.asset_id = d.asset_id AND a.missing_at IS NULL "
        f"WHERE d.status = 'confirmed' AND d.taxon_rank = 'species' AND coalesce(d.{trait}_by, '') != 'user' "
        f"AND (d.{trait}_by IS NULL OR d.{trait}_key IS NOT d.taxon_key) ORDER BY d.asset_id LIMIT ?", (trait, MIN_ACCURACY, limit)).fetchall()
