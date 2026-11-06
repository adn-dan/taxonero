"""End-to-end benchmark: each photo goes through Analyzer.analyze (detectors, merging,
classification, geography, deduplication), as in production.

    python dev/benchmark_e2e.py [--device cpu|cuda] [--no-owl] [--owl-mode always|fallback]

Measures: right species (most certain detection), number of boxes, and precision of the
auto-confirmation rule (autoconfirm.should_auto_confirm).
"""

import argparse
import csv
import time
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image

from taxonero import store
from taxonero.pipeline import Analyzer, Classifier, Detector, OpenVocabDetector
from taxonero.taxonomy import Taxonomy
from taxonero.autoconfirm import should_auto_confirm

PHOTOS = Path(__file__).parent / "photos"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--no-owl", action="store_true")
    ap.add_argument("--owl-mode", default="always", choices=("always", "fallback"))
    ap.add_argument("--out", default="", help="name of the output CSV (in dev/photos)")
    ap.add_argument("--threshold", type=float, default=0.85)
    ap.add_argument("--groups", default="", help="ex. Plantae,Fungi,Insecta")
    args = ap.parse_args()

    tx = Taxonomy(store.connect("data/bench-cache.sqlite"))
    rows = [r for r in csv.DictReader(open(PHOTOS / "truth.csv"))
            if not args.groups or r["group"] in args.groups.split(",")]
    truth = {r["file"]: tx.match(r["name"], enrich=False) for r in rows}
    an = Analyzer(Detector("models/MDV6-apa-rtdetr-e.onnx"), Classifier(device=args.device), tx,
                  open_detector=None if args.no_owl else OpenVocabDetector(device=args.device),
                  open_mode=args.owl_mode)

    out, per_group, nboxes, auto = [], defaultdict(list), Counter(), []
    t0 = time.time()
    for r in rows:
        res = an.analyze(Image.open(PHOTOS / r["file"]), float(r["lat"]), float(r["lon"]))
        nboxes[len(res)] += 1
        t = truth[r["file"]]
        best = max(res, key=lambda a: a.decision.confidence) if res else None
        ok = bool(best and best.decision.rank == "species" and best.decision.gbif_key == t.gbif_key)
        per_group[r["group"]].append(ok)
        for a in res:
            if should_auto_confirm(a, args.threshold):
                auto.append((a.decision.gbif_key == t.gbif_key, r["name"], a.decision.name))
        out.append({"file": r["file"], "group": r["group"], "truth": r["name"], "boxes": len(res),
                    "domestic": sum(a.domestic for a in res),
                    "labels": "+".join(f"{a.box.source}:{a.box.label}" for a in res),
                    "best": f"{best.decision.rank}:{best.decision.name}" if best else "", "ok": ok})
    dt = time.time() - t0
    with open(PHOTOS / (args.out or ("bench_e2e.csv" if not args.groups else "bench_e2e_partial.csv")), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)

    n = len(rows)
    print(f"{n} photos, {dt / n:.1f} s/photo ({args.device}, "
          f"OWLv2 {'non' if args.no_owl else args.owl_mode})")
    print(f"right species: {sum(o['ok'] for o in out) / n:.1%}")
    print("boxes per photo:", dict(sorted(nboxes.items())))
    for g, oks in per_group.items():
        print(f"  {g:15s} n={len(oks):2d} {sum(oks) / len(oks):6.1%}")
    if auto:
        print(f"auto-confirmation (threshold {args.threshold}): {len(auto)} detections, precision {sum(a[0] for a in auto) / len(auto):.1%}")
        print("  erreurs :", [f"{a[1]} -> {a[2]}" for a in auto if not a[0]])


if __name__ == "__main__":
    main()
