"""Test photos with annotations from iNaturalist (sex, life stage, flowers and fruits, alive or dead), to measure what
BioCLIP 2 can tell of an individual beyond its species (dev/benchmark_traits.py).

Research-grade observations whose annotation was set by the iNaturalist community, CC0 / CC-BY / CC-BY-NC photos
(test set only, never redistributed). Output: <out>/<task>/*.jpg + <out>/truth.csv

    python dev/fetch_annotated.py [photos per species and value, default 12]
    # larger photos of one task, kept apart, to measure on the crop of the subject (benchmark_traits.py --crop):
    python dev/fetch_annotated.py 50 --tasks sex --size large --out dev/photos_annotated/large
"""

import argparse
import csv
import time
from pathlib import Path

import httpx

OUT = Path(__file__).parent / "photos_annotated"
API = "https://api.inaturalist.org/v1"
# task -> (iNaturalist term, {value id: label}, species)
TASKS = {
    "sex": (9, {10: "female", 11: "male"}, [
        # Clear dimorphism…
        "Anas platyrhynchos", "Turdus merula", "Passer domesticus", "Fringilla coelebs", "Pyrrhula pyrrhula",
        "Phoenicurus ochruros", "Capreolus capreolus", "Cervus elaphus", "Calopteryx splendens", "Gonepteryx rhamni",
        "Lucanus cervus", "Anthocharis cardamines",
        # …and none to speak of (controls: nothing to see, the answer should be "cannot tell").
        "Erithacus rubecula", "Cyanistes caeruleus"]),
    "stage": (1, {2: "adult", 8: "juvenile", 6: "larva", 5: "nymph"}, [
        "Larus michahellis", "Erithacus rubecula", "Turdus merula", "Cygnus olor", "Fulica atra", "Sus scrofa",
        "Capreolus capreolus", "Papilio machaon", "Aglais io", "Coccinella septempunctata", "Harmonia axyridis",
        "Palomena prasina", "Nezara viridula", "Bufo bufo", "Salamandra salamandra"]),
    "flowers": (12, {13: "flowers", 14: "fruits", 21: "none"}, [
        "Rubus fruticosus", "Crataegus monogyna", "Sambucus nigra", "Rosa canina", "Prunus spinosa", "Hedera helix"]),
    "alive": (17, {18: "alive", 19: "dead"}, ["Erinaceus europaeus", "Vulpes vulpes", "Meles meles", "Turdus merula"]),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("per_value", nargs="?", type=int, default=12)
    ap.add_argument("--tasks", default=",".join(TASKS), help="comma separated, among: " + ", ".join(TASKS))
    ap.add_argument("--size", default="medium", choices=["medium", "large"], help="500 px or 1024 px on the long side")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    per_value, out = args.per_value, args.out
    rows = []
    with httpx.Client(timeout=60, headers={"User-Agent": "taxonero-dev/0.1"}) as http:
        def get(path, **params):
            for attempt in range(5):
                try:
                    r = http.get(f"{API}/{path}", params=params)
                except httpx.TransportError:   # connection cut by the server: asked again
                    time.sleep(10 * (attempt + 1))
                    continue
                if r.status_code == 429:
                    time.sleep(10 * (attempt + 1))
                    continue
                return r.raise_for_status().json()
            raise RuntimeError("iNaturalist: too many requests")

        for task in args.tasks.split(","):
            term, values, species = TASKS[task]
            (out / task).mkdir(parents=True, exist_ok=True)
            for name in species:
                taxon = next((t for t in get("taxa", q=name, rank="species", per_page=5)["results"] if t["name"] == name), None)
                if not taxon:
                    print(f"{name}: not found")
                    continue
                for value, label in values.items():
                    data = get("observations", quality_grade="research", taxon_id=taxon["id"], term_id=term, term_value_id=value,
                               photo_license="cc0,cc-by,cc-by-nc", per_page=per_value * 2, order_by="votes")
                    taken = 0
                    for obs in data["results"]:
                        if taken >= per_value or not obs.get("photos"):
                            continue
                        path = out / task / f"inat_{obs['id']}.jpg"
                        if not path.exists():
                            try:
                                content = http.get(obs["photos"][0]["url"].replace("/square.", f"/{args.size}.")).raise_for_status().content
                            except httpx.HTTPError:   # photo removed, connection cut: the next one
                                continue
                            if not content.startswith(b"\xff\xd8"):
                                continue
                            path.write_bytes(content)
                            time.sleep(0.3)
                        rows.append({"task": task, "file": f"{task}/{path.name}", "species": name, "label": label,
                                     "source": f"https://www.inaturalist.org/observations/{obs['id']}"})
                        taken += 1
                    if taken:
                        print(f"{task} {name} {label}: {taken}", flush=True)
                    time.sleep(1)
    with open(out / "truth.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} photos in {out}")


if __name__ == "__main__":
    main()
