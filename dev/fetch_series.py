"""Ajoute au jeu de test des séries de plusieurs photos d'une même espèce (observations distinctes,
lieux différents), pour tester les vues par espèce, la carte et les filtres.

    python dev/fetch_series.py "Erithacus rubecula:6" "Vulpes vulpes:5" ...

Les photos s'ajoutent à dev/photos et à truth.csv (mêmes règles que fetch_dataset.py).
"""

import csv
import subprocess
import sys
import time
from pathlib import Path

import httpx

OUT = Path(__file__).parent / "photos"
FRANCE = 6753
FIELDS = ["file", "group", "taxon_id", "name", "common", "lat", "lon", "license", "attribution", "source"]


def fetch(http: httpx.Client, name: str, n: int, known: set[str]) -> list[dict]:
    r = http.get("https://api.inaturalist.org/v1/observations", params={
        "taxon_name": name, "quality_grade": "research", "place_id": FRANCE, "photo_license": "cc0,cc-by",
        "per_page": n * 4, "order_by": "votes", "geoprivacy": "open"}).raise_for_status().json()
    rows, places = [], set()
    for obs in r["results"]:
        if len(rows) >= n:
            break
        if not obs.get("location") or not obs.get("observed_on") or f"inat_{obs['id']}.jpg" in known:
            continue
        lat, lon = (float(x) for x in obs["location"].split(","))
        cell = (round(lat), round(lon))
        if cell in places:  # une photo par zone d'environ 100 km : des lieux variés
            continue
        photo = next((p for p in obs["photos"] if p.get("license_code") in ("cc0", "cc-by")), None)
        if not photo:
            continue
        content = http.get(photo["url"].replace("/square.", "/large.")).raise_for_status().content
        if not content.startswith(b"\xff\xd8"):
            continue
        path = OUT / f"inat_{obs['id']}.jpg"
        path.write_bytes(content)
        date = obs["observed_on"].replace("-", ":") + " 12:00:00"
        subprocess.run(["exiftool", "-q", "-overwrite_original", f"-DateTimeOriginal={date}",
                        f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                        f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}", str(path)], check=True)
        t = obs["taxon"]
        rows.append({"file": path.name, "group": t.get("iconic_taxon_name", ""), "taxon_id": t["id"], "name": t["name"],
                     "common": t.get("preferred_common_name", ""), "lat": lat, "lon": lon, "license": photo["license_code"],
                     "attribution": photo.get("attribution", ""), "source": f"https://www.inaturalist.org/observations/{obs['id']}"})
        places.add(cell)
        time.sleep(0.5)
    return rows


def main(specs: list[str]) -> None:
    truth = OUT / "truth.csv"
    existing = list(csv.DictReader(open(truth)))
    known = {r["file"] for r in existing}
    added = []
    with httpx.Client(timeout=60, headers={"User-Agent": "taxonero-dev/0.1"}) as http:
        for spec in specs:
            name, _, n = spec.partition(":")
            rows = fetch(http, name, int(n or 5), known)
            print(f"{name}: {len(rows)} photos")
            added += rows
            time.sleep(1)
    with open(truth, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(existing + added)
    print(f"{len(added)} photos ajoutées ({len(existing) + len(added)} au total)")


if __name__ == "__main__":
    main(sys.argv[1:])
