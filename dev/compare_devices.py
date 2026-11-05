"""Vérifie que le GPU donne les mêmes résultats que le processeur, sur les photos de dev/photos.

À lancer sur la machine qui a le GPU, par exemple dans le conteneur avec quelques photos copiées dans data/ :

    docker cp dev/compare_devices.py taxonero:/tmp/
    docker exec taxonero python /tmp/compare_devices.py --photos /data/test-photos \
        --detector /models/MDV6-apa-rtdetr-e.onnx --db /data/compare-cache.sqlite

Pour chaque photo : mêmes sujets, même taxon retenu, même rang ; écart maximal des probabilités.
Les deux passes partagent le cache GBIF : seule l'inférence diffère.
"""

import argparse
import time
from pathlib import Path

from taxonero import hardware, store
from taxonero.pipeline import Analyzer, Classifier, Detector, OpenVocabDetector
from taxonero.taxonomy import Taxonomy


def build(device: str, tx: Taxonomy, detector: Path) -> Analyzer:
    hardware.full_precision()
    det = Detector(detector, providers=hardware.onnx_session_providers(device)) if detector.exists() else None
    return Analyzer(det, Classifier(device=device), tx, open_detector=OpenVocabDetector(device=device))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--photos", default=str(Path(__file__).parent / "photos"))
    ap.add_argument("--detector", default="models/MDV6-apa-rtdetr-e.onnx")
    ap.add_argument("--db", default="data/bench-cache.sqlite")
    args = ap.parse_args()

    gpu, why = hardware.resolve("auto")
    if gpu == "cpu":
        raise SystemExit(f"pas de GPU : {why}")
    tx = Taxonomy(store.connect(args.db))
    files = sorted(Path(args.photos).glob("*.jpg"))[:args.limit]
    results = {}
    for device in ("cpu", gpu):
        analyzer, t = build(device, tx, Path(args.detector)), time.time()
        results[device] = [analyzer.analyze(f.read_bytes()) for f in files]
        print(f"{device} : {(time.time() - t) / len(files):.2f} s par photo")
        del analyzer
        hardware.release(device)

    same, worst = 0, 0.0
    for f, a, b in zip(files, results["cpu"], results[gpu]):
        da = [(r.decision.gbif_key, r.decision.rank) for r in a]
        db = [(r.decision.gbif_key, r.decision.rank) for r in b]
        gap = max((abs(x.decision.confidence - y.decision.confidence) for x, y in zip(a, b)), default=0.0)
        worst = max(worst, gap)
        if da == db:
            same += 1
        else:
            print(f"  différent : {f.name} cpu={da} gpu={db}")
    print(f"\n{same}/{len(files)} photos identiques (sujets, taxon, rang) ; écart maximal de confiance {worst:.2e}")


if __name__ == "__main__":
    main()
