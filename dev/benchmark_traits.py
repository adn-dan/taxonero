"""What can BioCLIP 2 tell of an individual beyond its species: sex, life stage, flowers or fruits, alive or dead?

Measured on iNaturalist photos annotated by its community (dev/fetch_annotated.py), per species, with four ways of
asking:
- text:    zero-shot with plain words ("a male animal" / "a female animal"…);
- species: zero-shot with the species in the prompt ("a male Anas platyrhynchos"…);
- probe:   a linear separation learnt on the image embeddings of the OTHER species (leave one species out): what a
           small classifier shipped with the application could do on a species it never saw;
- own:     nearest class centre learnt on other photos of the SAME species (leave one photo out);
- ridge:   a regularised linear classifier (one value against the others) learnt on other photos of the SAME species,
           10 folds: what a small model per species would do, given labelled examples of it. Far above "own" once
           there are ~100 photos per species: the embedding does hold the information, a class centre misses it.
Chance: 1 / number of values of the species. Image embeddings are cached in <root>/embeddings.npz.

Whole photos by default. With --crop, the application's own way: the frame of the detected animal (MegaDetector,
OWLv2 where it sees nothing), on the photos showing a single animal (with several, nothing says which one the
annotation is about); the same photos are also measured whole, to tell what the crop brings.

    python dev/benchmark_traits.py
    python dev/benchmark_traits.py --crop --root dev/photos_annotated/large
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
ROOT = Path(__file__).parent / "photos_annotated"
CROP = "#crop"   # suffix of the embedding of the cropped subject, next to the one of the whole photo
WORDS = {   # task -> label -> (plain words, with the species)
    "sex": {"male": ("a male animal", "a male {}"), "female": ("a female animal", "a female {}")},
    "stage": {"adult": ("an adult animal", "an adult {}"), "juvenile": ("a juvenile animal, a young", "a juvenile {}"),
              "larva": ("a larva, a caterpillar, a tadpole", "a larva of {}"), "nymph": ("a nymph, an immature insect", "a nymph of {}")},
    "flowers": {"flowers": ("a plant in flower, with flowers", "{} in flower"), "fruits": ("a plant with fruits or seeds", "{} with fruits"),
                "none": ("a plant without flowers or fruits, leaves only", "{} without flowers or fruits")},
    "alive": {"alive": ("a living animal", "a living {}"), "dead": ("a dead animal, roadkill, a carcass", "a dead {}")},
}


def animal_boxes(rows: list[dict], root: Path) -> dict[str, list]:
    """file -> boxes [x, y, w, h] of the animals seen in it, as the application finds them; cached in boxes.json."""
    from PIL import Image
    from taxonero.pipeline import Detector, OpenVocabDetector
    cache = root / "boxes.json"
    known = json.loads(cache.read_text()) if cache.exists() else {}
    todo = sorted({r["file"] for r in rows} - set(known))
    if todo:
        md, owl = Detector(Path(__file__).parent.parent / "models" / "MDV6-apa-rtdetr-e.onnx"), None
        for i, f in enumerate(todo):
            im = Image.open(root / f).convert("RGB")
            boxes = md.detect(im)
            if not boxes:   # insects and other small animals
                owl = owl or OpenVocabDetector()
                boxes = [b for b in owl.detect(im) if b.label == "animal"]
            known[f] = [[b.x, b.y, b.w, b.h] for b in boxes]
            if i % 50 == 49 or i + 1 == len(todo):
                cache.write_text(json.dumps(known))
                print(f"  boxes {i + 1}/{len(todo)}", flush=True)
    return known


def embeddings(rows: list[dict], root: Path, boxes: dict[str, list] | None = None):
    """(image embeddings, text encoder) of BioCLIP 2; the image ones are computed once and cached. With boxes, the
    crop of the only animal of each photo is embedded too (key: file + CROP)."""
    import torch
    from bioclip import CustomLabelsClassifier
    from PIL import Image
    from taxonero.pipeline import Box, crop
    clf = CustomLabelsClassifier(["animal"], device="cpu")
    cache = root / "embeddings.npz"
    known = dict(np.load(cache)) if cache.exists() else {}
    keys = [r["file"] for r in rows] + ([r["file"] + CROP for r in rows] if boxes else [])
    todo = [k for k in dict.fromkeys(keys) if k not in known]

    def load(key: str):
        im = Image.open(root / key.removesuffix(CROP)).convert("RGB")
        return crop(im, Box(*boxes[key.removesuffix(CROP)][0], 1.0, "animal")) if key.endswith(CROP) else im

    for i in range(0, len(todo), 16):
        batch = todo[i:i + 16]
        feats = clf.create_image_features([load(k) for k in batch]).cpu().numpy()
        known.update(zip(batch, feats))
        print(f"  images {min(i + 16, len(todo))}/{len(todo)}", flush=True)
        if (i // 16) % 5 == 4 or i + 16 >= len(todo):   # kept as it goes: an interrupted run resumes
            np.savez(cache, **known)

    def text(prompts: list[str]) -> np.ndarray:
        with torch.no_grad():
            return clf._get_txt_embeddings(prompts).T.cpu().numpy()   # one row per prompt, normalised, prompt templates averaged
    return known, text


def ridge(X: np.ndarray, y: np.ndarray, classes: int, lam: float = 0.1, folds: int = 10) -> float:
    """Cross-validated accuracy of a ridge classifier on normalised embeddings (dual form: few photos, many dimensions)."""
    X = X / np.linalg.norm(X, axis=1, keepdims=True)
    order, hits = np.random.default_rng(0).permutation(len(y)), 0
    for f in range(folds):
        test = order[f::folds]
        train = np.setdiff1d(order, test)
        mu = X[train].mean(0)
        A, T = X[train] - mu, np.where(y[train][:, None] == np.arange(classes), 1.0, -1.0)
        W = A.T @ np.linalg.solve(A @ A.T + lam * np.eye(len(A)), T - T.mean(0))
        hits += int((((X[test] - mu) @ W + T.mean(0)).argmax(1) == y[test]).sum())
    return hits / len(y)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--crop", action="store_true", help="measure on the frame of the detected animal")
    args = ap.parse_args()
    rows = list(csv.DictReader(open(args.root / "truth.csv")))
    # A photo annotated twice for the same species and task (rare) is ambiguous: left out.
    seen = defaultdict(set)
    for r in rows:
        seen[(r["task"], r["species"], r["file"])].add(r["label"])
    rows = [r for r in rows if len(seen[(r["task"], r["species"], r["file"])]) == 1]
    if not args.crop:
        emb, text = embeddings(rows, args.root)
        return report(rows, emb, text, "whole photos")
    boxes = animal_boxes(rows, args.root)
    count = defaultdict(lambda: [0, 0, 0])   # species -> photos without an animal, with one, with several
    for f, sp in {r["file"]: r["species"] for r in rows}.items():
        count[sp][min(len(boxes[f]), 2)] += 1
    print(f"{'species':28} {'none':>5} {'one':>5} {'several':>8}   animals found in the photo")
    for sp, (none, one, several) in sorted(count.items()):
        print(f"{sp:28} {none:>5} {one:>5} {several:>8}")
    rows = [r for r in rows if len(boxes[r["file"]]) == 1]
    emb, text = embeddings(rows, args.root, boxes)
    report(rows, emb, text, "single animal, whole photo")
    report(rows, {r["file"]: emb[r["file"] + CROP] for r in rows}, text, "single animal, crop")


def report(rows: list[dict], emb: dict, text, title: str) -> None:
    for task, words in WORDS.items():
        mine = [r for r in rows if r["task"] == task]
        if not mine:
            continue
        species = sorted({r["species"] for r in mine})
        print(f"\n== {task} ({title}): {len(mine)} photos, {len(species)} species ==")
        print(f"{'species':28} {'n':>4} {'chance':>7} {'text':>6} {'species':>8} {'probe':>6} {'own':>6} {'ridge':>6}   values")
        total = defaultdict(lambda: [0, 0])
        for sp in species:
            test = [r for r in mine if r["species"] == sp]
            labels = sorted({r["label"] for r in test})
            if len(labels) < 2:
                continue
            X = np.stack([emb[r["file"]] for r in test])
            y = np.array([labels.index(r["label"]) for r in test])
            score = {}
            for name, k in (("text", 0), ("species", 1)):
                T = text([words[lab][k].format(sp) for lab in labels])
                score[name] = float(((X @ T.T).argmax(1) == y).mean())
            # Probe: class centres of the other species (each centred on its own mean, so that only the difference
            # between values is learnt, not the species), applied to this one centred the same way.
            others = [r for r in mine if r["species"] != sp and r["label"] in labels]
            centres = []
            for lab in labels:
                diffs = []
                for other in {r["species"] for r in others}:
                    of = [r for r in others if r["species"] == other]
                    if {r["label"] for r in of} >= set(labels):
                        mean = np.stack([emb[r["file"]] for r in of]).mean(0)
                        diffs.append(np.stack([emb[r["file"]] for r in of if r["label"] == lab]).mean(0) - mean)
                centres.append(np.mean(diffs, 0) if diffs else None)
            score["probe"] = float((((X - X.mean(0)) @ np.stack(centres).T).argmax(1) == y).mean()) if all(c is not None for c in centres) else float("nan")
            # Own: leave one photo out, nearest class centre among the other photos of the species.
            hits = 0
            for i in range(len(test)):
                keep = np.arange(len(test)) != i
                cs = np.stack([X[keep & (y == c)].mean(0) if (keep & (y == c)).any() else np.zeros(X.shape[1]) for c in range(len(labels))])
                hits += int((X[i] @ cs.T).argmax() == y[i])
            score["own"] = hits / len(test)
            score["ridge"] = ridge(X.astype(np.float64), y, len(labels))
            counts = ", ".join(f"{lab} {int((y == i).sum())}" for i, lab in enumerate(labels))
            print(f"{sp:28} {len(test):>4} {1 / len(labels):>7.0%} {score['text']:>6.0%} {score['species']:>8.0%} {score['probe']:>6.0%} {score['own']:>6.0%} {score['ridge']:>6.0%}   {counts}")
            for k, v in score.items():
                if v == v:
                    total[k][0] += v * len(test)
                    total[k][1] += len(test)
        print(f"{'ALL':28} {'':>4} {'':>7} " + " ".join(f"{(total[k][0] / total[k][1] if total[k][1] else float('nan')):>{w}.0%}" for k, w in (("text", 6), ("species", 8), ("probe", 6), ("own", 6), ("ridge", 6))))


if __name__ == "__main__":
    main()
