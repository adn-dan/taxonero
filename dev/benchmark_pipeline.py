"""Evaluate the full pipeline on dev/photos and compare variants.

Variants: whole image or detector crop, with or without geographic reweighting.
The ground truth is normalised through GBIF (synonyms, spelling) before comparison.

    python dev/benchmark_pipeline.py [--device cpu|cuda] [--detector models/MDV6-apa-rtdetr-e.onnx]

Output: dev/photos/bench_pipeline.csv + summary table, and coverage/precision curve
of the auto-confirmation depending on the threshold.
"""

import argparse
import csv
import pickle
import time
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageOps

from taxonero import store
from taxonero.pipeline import Box, Classifier, Detector, crop, decide, rerank
from taxonero.taxonomy import Taxonomy

PHOTOS = Path(__file__).parent / "photos"


def judge(dec, truth) -> str:
    """'exact' (right species), 'right_rank' (right higher rank), 'wrong', 'abstain'."""
    if dec.rank == "none":
        return "abstain"
    if dec.rank == "species":
        return "exact" if dec.gbif_key == truth.gbif_key else "wrong"
    return "right_rank" if (dec.name or "").lower() == truth.lineage.get(dec.rank, "").lower() else "wrong"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--detector", default="models/MDV6-apa-rtdetr-e.onnx")
    ap.add_argument("--db", default="data/bench-cache.sqlite")
    args = ap.parse_args()

    db = store.connect(args.db)
    tx = Taxonomy(db)
    truth_rows = list(csv.DictReader(open(PHOTOS / "truth.csv")))
    truths = {r["file"]: tx.match(r["name"], enrich=False) for r in truth_rows}

    cache = PHOTOS / "bench_classif.pkl"
    if cache.exists():
        full_c, crop_c, boxes_info = pickle.loads(cache.read_bytes())
        print("classifications read back from the cache")
    else:
        full_c, crop_c, boxes_info = classify_all(args, truth_rows)
        cache.write_bytes(pickle.dumps((full_c, crop_c, boxes_info)))
    evaluate(tx, truth_rows, truths, full_c, crop_c, boxes_info)


def classify_all(args, truth_rows):
    det = Detector(args.detector)
    clf = Classifier(device=args.device, k=20)

    images, crops, boxes_info = [], [], []
    t0 = time.time()
    for r in truth_rows:
        im = ImageOps.exif_transpose(Image.open(PHOTOS / r["file"])).convert("RGB")
        boxes = det.detect(im)
        images.append(im)
        b = boxes[0] if boxes else Box(0, 0, 1, 1, 0, "image")
        crops.append(im if b.label == "image" else crop(im, b))
        boxes_info.append((len(boxes), b.score))
    t_det = time.time() - t0
    t0 = time.time()
    full_c = clf.classify(images)
    crop_c = clf.classify(crops)
    t_clf = time.time() - t0
    print(f"detection {t_det / len(images):.2f} s/img, classification {t_clf / (2 * len(images)):.2f} s/img ({args.device})")
    return full_c, crop_c, boxes_info


def evaluate(tx, truth_rows, truths, full_c, crop_c, boxes_info):

    variants = {"whole": full_c, "cropped": crop_c}
    results = defaultdict(list)
    rows = []
    for i, r in enumerate(truth_rows):
        truth = truths[r["file"]]
        lat, lon = float(r["lat"]), float(r["lon"])
        row = {"file": r["file"], "group": r["group"], "truth": r["name"], "boxes": boxes_info[i][0]}
        for vname, cands in variants.items():
            for geo in (False, True):
                ranked = rerank([type(c)(**vars(c)) for c in cands[i]], tx, lat if geo else None, lon if geo else None)
                dec = decide(ranked, tx)
                key = f"{vname}{'+geo' if geo else ''}"
                verdict = judge(dec, truth)
                top1_ok = bool(ranked) and ranked[0].gbif_key == truth.gbif_key
                results[key].append((verdict, top1_ok, ranked[0].posterior if ranked else 0.0, r["group"]))
                row[f"{key}:dec"] = f"{dec.rank}:{dec.name}"
                row[f"{key}:verdict"] = verdict
                row[f"{key}:p"] = round(ranked[0].posterior, 3) if ranked else 0
        rows.append(row)

    with open(PHOTOS / "bench_pipeline.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    n = len(rows)
    print(f"\n{'variant':16s} {'top1 sp.':>9s} {'exact':>7s} {'higher':>8s} {'wrong':>6s} {'abstain':>8s}")
    for key, res in results.items():
        c = defaultdict(int)
        for v, *_ in res:
            c[v] += 1
        top1 = sum(t for _, t, _, _ in res) / n
        print(f"{key:16s} {top1:9.1%} {c['exact'] / n:7.1%} {c['right_rank'] / n:8.1%} {c['wrong'] / n:6.1%} {c['abstain'] / n:8.1%}")

    best = max(results, key=lambda k: sum(t for _, t, _, _ in results[k]))
    print(f"\nAuto-confirmation at species rank ({best}): threshold -> coverage / precision")
    for thr in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
        sel = [t for _, t, p, _ in results[best] if p >= thr]
        if sel:
            print(f"  >= {thr:.2f}  {len(sel) / n:6.1%}  {sum(sel) / len(sel):6.1%}  (n={len(sel)})")
    print("\nPer group (species top-1,", best, ")")
    groups = defaultdict(list)
    for _, t, _, g in results[best]:
        groups[g].append(t)
    for g, ts in groups.items():
        print(f"  {g:15s} n={len(ts):2d} {sum(ts) / len(ts):6.1%}")


if __name__ == "__main__":
    main()
