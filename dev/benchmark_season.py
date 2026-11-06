"""Measure the effect of a seasonal prior (GBIF observations of the month in the region) on the test set,
from the cached classifications (dev/photos/bench_classif.pkl): no inference, only GBIF.

    python dev/benchmark_season.py

Dates come from dev/photos/dates.json (iNaturalist observation date).

Measured on 2026-10-23 on the 54 photos: top-1 and exact species unchanged (74.1 % / 68.5 %), one more
correct auto-confirmation but one more error depending on the floor. No gain: not integrated in the
pipeline. To redo on a set rich in migrants, insects and seasonal fungi.
"""

import csv
import json
import pickle
from pathlib import Path

from taxonero import store
from taxonero.pipeline import Candidate, average, decide, rerank
from taxonero.taxonomy import GBIF, Taxonomy

PHOTOS = Path(__file__).parent / "photos"


def season_profile(tx: Taxonomy, gbif_key: int, lat: float, lon: float, radius_deg: float = 5.0) -> list[int]:
    """Field observations of the taxon per month (January..December) in the region of the photo
    (same grid as occurrences_near, hence the same cache for photos of the same area)."""
    lat, lon = float(round(lat)), float(round(lon))
    q = {"taxonKey": gbif_key, "limit": 0, "occurrenceStatus": "PRESENT", "facet": "month", "facetLimit": 12,
         "basisOfRecord": ["HUMAN_OBSERVATION", "MACHINE_OBSERVATION", "OBSERVATION"],
         "decimalLatitude": f"{lat - radius_deg:.2f},{lat + radius_deg:.2f}",
         "decimalLongitude": f"{lon - radius_deg:.2f},{lon + radius_deg:.2f}"}
    counts = [0] * 12
    for f in tx.get(f"{GBIF}/occurrence/search", q, ttl=180 * 86400).get("facets", []):
        for c in f.get("counts", []):
            m = int(c["name"])
            if 1 <= m <= 12:
                counts[m - 1] = int(c["count"])
    return counts


def season_weight(profile: list[int], month: int, floor: float = 0.1, power: float = 1.0) -> float:
    """Seasonal weight in 0..1: share of the regional observations made this month (smoothed over the neighbouring
    months) relative to a uniform distribution, capped at 1. It only penalises a species
    out of season (migrant in winter, fungus in July), never favours a species in season.
    Without enough observations in the region to judge: 1."""
    total = sum(profile)
    if total < 30:
        return 1.0
    m = month - 1
    near = (profile[(m - 1) % 12] + 2 * profile[m] + profile[(m + 1) % 12]) / 4
    ratio = near / (total / 12)
    return max(floor, min(1.0, ratio)) ** power


def load_dates(rows) -> dict:
    """iNaturalist observation dates, fetched once then kept in dev/photos/dates.json."""
    path = PHOTOS / "dates.json"
    if not path.exists():
        import urllib.request
        ids = [r["file"].split("_")[1].split(".")[0] for r in rows]
        dates = {}
        for i in range(0, len(ids), 30):
            req = urllib.request.Request("https://api.inaturalist.org/v1/observations?per_page=30&id=" + ",".join(ids[i:i + 30]),
                                         headers={"User-Agent": "taxonero-bench"})
            for o in json.load(urllib.request.urlopen(req, timeout=30))["results"]:
                dates[f"inat_{o['id']}.jpg"] = o.get("observed_on")
        path.write_text(json.dumps(dates))
    return json.loads(path.read_text())


def main() -> None:
    db = store.connect("data/bench-cache.sqlite")
    tx = Taxonomy(db)
    full_c, crop_c, _ = pickle.loads((PHOTOS / "bench_classif.pkl").read_bytes())
    rows = list(csv.DictReader(open(PHOTOS / "truth.csv")))[:len(full_c)]
    dates = load_dates(rows)
    truths = [tx.match(r["name"], enrich=False) for r in rows]
    variants = {"no season": None, "floor 0.3": (0.3, 1.0), "floor 0.1": (0.1, 1.0),
                "floor 0.1, soft": (0.1, 0.5), "floor 0.03": (0.03, 1.0)}
    print(f"{'variant':22s} {'top1':>6s} {'exact':>6s} {'wrong':>5s}  auto (n / right)")
    for name, params in variants.items():
        top1 = exact = wrong = auto = auto_ok = 0
        for i, r in enumerate(rows):
            lat, lon = float(r["lat"]), float(r["lon"])
            month = int(dates[r["file"]][5:7]) if dates.get(r["file"]) else None
            cands = average([Candidate(**vars(c)) for c in crop_c[i]], [Candidate(**vars(c)) for c in full_c[i]])
            ranked = rerank(cands, tx, lat, lon)
            if params and month:
                for c in ranked:
                    if c.gbif_key and c.plausibility != "unknown":
                        c.prior *= season_weight(season_profile(tx, c.gbif_key, lat, lon), month, *params)
                z = sum(c.prior for c in ranked) or 1
                for c in ranked:
                    c.prior /= z
                    c.posterior = c.prior
                ranked.sort(key=lambda c: -c.posterior)
            d = decide(ranked, tx)
            t = truths[i]
            top1 += bool(ranked) and ranked[0].gbif_key == t.gbif_key
            if d.rank == "species":
                exact += d.gbif_key == t.gbif_key
                wrong += d.gbif_key != t.gbif_key
            ru = ranked[1].posterior if len(ranked) > 1 else 0
            if d.rank == "species" and d.confidence >= 0.85 and d.plausibility == "green" and ru <= 0.05:
                auto += 1
                auto_ok += d.gbif_key == t.gbif_key
        n = len(rows)
        print(f"{name:22s} {top1 / n:6.1%} {exact / n:6.1%} {wrong:5d}  {auto} / {auto_ok}")


if __name__ == "__main__":
    main()
