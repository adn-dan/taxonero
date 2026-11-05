"""Mesure l'effet d'un a priori saisonnier (observations GBIF du mois dans la région) sur le jeu de test,
à partir des classifications en cache (dev/photos/bench_classif.pkl) : aucune inférence, seulement GBIF.

    python dev/benchmark_season.py

Les dates viennent de dev/photos/dates.json (date d'observation iNaturalist).

Mesuré le 2026-10-23 sur les 54 photos : top-1 et espèce exacte inchangés (74,1 % / 68,5 %), une
auto-validation juste de plus mais une erreur de plus selon le plancher. Pas de gain : non intégré au
pipeline. À refaire sur un jeu riche en migrateurs, insectes et champignons de saison.
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
    """Observations de terrain du taxon par mois (janvier..décembre) dans la région de la photo
    (même grille que occurrences_near, donc même cache pour les photos d'un même secteur)."""
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
    """Poids saisonnier dans 0..1 : part des observations régionales faites ce mois-ci (lissée sur les mois
    voisins) rapportée à une répartition uniforme, plafonnée à 1. On ne fait que pénaliser une espèce
    hors saison (migrateur en hiver, champignon en juillet), jamais favoriser une espèce de saison.
    Sans assez d'observations dans la région pour juger : 1."""
    total = sum(profile)
    if total < 30:
        return 1.0
    m = month - 1
    near = (profile[(m - 1) % 12] + 2 * profile[m] + profile[(m + 1) % 12]) / 4
    ratio = near / (total / 12)
    return max(floor, min(1.0, ratio)) ** power


def load_dates(rows) -> dict:
    """Dates d'observation iNaturalist, récupérées une fois puis gardées dans dev/photos/dates.json."""
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
    variants = {"sans saison": None, "plancher 0,3": (0.3, 1.0), "plancher 0,1": (0.1, 1.0),
                "plancher 0,1, doux": (0.1, 0.5), "plancher 0,03": (0.03, 1.0)}
    print(f"{'variante':22s} {'top1':>6s} {'exact':>6s} {'faux':>5s}  auto (n / justes)")
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
