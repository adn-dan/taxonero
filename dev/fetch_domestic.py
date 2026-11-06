"""Test photos of domestic animals (dogs, cats, horses, hens) from iNaturalist, always the
same observations so that measurements stay comparable. CC0/CC-BY photos, not versioned: this script
downloads them again. Used to check that domestic animals are recognised as such
(DOMESTIC_ANIMALS=skip), for instance by comparing the OWLv2 modes.
Output: dev/photos_domestic/*.jpg + dev/photos_domestic/truth.csv (licence and author of each photo)
"""

import csv
import time
from pathlib import Path

import httpx

OUT = Path(__file__).parent / "photos_domestic"
OBSERVATIONS = (120643706, 136695837, 149918166, 150489057, 160376155, 187229121, 204091517, 234313028,
                234865890, 258826091, 344515017, 38985113)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    rows = []
    with httpx.Client(timeout=60, headers={"User-Agent": "taxonero-dev/0.1"}) as http:
        ids = ",".join(map(str, OBSERVATIONS))
        for obs in http.get(f"https://api.inaturalist.org/v1/observations/{ids}").raise_for_status().json()["results"]:
            photo = next((p for p in obs["photos"] if p.get("license_code") in ("cc0", "cc-by")), None)
            if not photo or not obs.get("location"):
                print(f"{obs['id']}: no photo under a free licence any more, skipped")
                continue
            path = OUT / f"dom_{obs['id']}.jpg"
            if not path.exists():
                content = http.get(photo["url"].replace("/square.", "/large.")).raise_for_status().content
                if not content.startswith(b"\xff\xd8"):
                    continue
                path.write_bytes(content)
                time.sleep(0.5)
            lat, lon = (float(x) for x in obs["location"].split(","))
            rows.append({"file": path.name, "name": obs["taxon"]["name"], "lat": lat, "lon": lon,
                         "license": photo["license_code"], "attribution": photo.get("attribution", ""),
                         "source": f"https://www.inaturalist.org/observations/{obs['id']}"})
    with open(OUT / "truth.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} photos in {OUT}")


if __name__ == "__main__":
    main()
