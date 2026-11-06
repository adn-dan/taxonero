"""Build a set of test photos with ground truth from iNaturalist.

French "research grade" observations, CC0/CC-BY photos. GPS and date are
injected into the EXIF (exiftool) to reproduce real camera photos.
Output: dev/photos/*.jpg + dev/photos/truth.csv
"""

import csv
import subprocess
import sys
import time
from pathlib import Path

import httpx

OUT = Path(__file__).parent / "photos"
GROUPS = {"Aves": 12, "Mammalia": 8, "Insecta": 8, "Reptilia": 4, "Amphibia": 4, "Arachnida": 3,
          "Actinopterygii": 3, "Plantae": 6, "Fungi": 6}
FRANCE = 6753


def main(per_group_factor: float = 1.0) -> None:
    OUT.mkdir(exist_ok=True)
    rows = []
    with httpx.Client(timeout=60, headers={"User-Agent": "taxonero-dev/0.1"}) as http:
        for group, n in GROUPS.items():
            n = max(1, int(n * per_group_factor))
            r = http.get("https://api.inaturalist.org/v1/observations", params={
                "quality_grade": "research", "place_id": FRANCE, "iconic_taxa": group,
                "photo_license": "cc0,cc-by", "rank": "species", "per_page": n * 3,
                "order_by": "votes", "geoprivacy": "open",
            }).raise_for_status().json()
            taken = 0
            seen: set[int] = set()
            for obs in data["results"] if (data := r) else []:
                taxon = obs["taxon"]
                if taken >= n or taxon["id"] in seen or not obs.get("location") or not obs.get("observed_on"):
                    continue
                photo = next((p for p in obs["photos"] if p.get("license_code") in ("cc0", "cc-by")), None)
                if not photo:
                    continue
                url = photo["url"].replace("/square.", "/large.")
                path = OUT / f"inat_{obs['id']}.jpg"
                content = http.get(url).raise_for_status().content
                if not content.startswith(b"\xff\xd8"):
                    continue
                path.write_bytes(content)
                lat, lon = (float(x) for x in obs["location"].split(","))
                date = obs["observed_on"].replace("-", ":") + " 12:00:00"
                subprocess.run(["exiftool", "-q", "-overwrite_original",
                                f"-DateTimeOriginal={date}",
                                f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                                f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
                                str(path)], check=True)
                rows.append({"file": path.name, "group": group, "taxon_id": taxon["id"],
                             "name": taxon["name"], "common": taxon.get("preferred_common_name", ""),
                             "lat": lat, "lon": lon, "license": photo["license_code"],
                             "attribution": photo.get("attribution", ""),
                             "source": f"https://www.inaturalist.org/observations/{obs['id']}"})
                seen.add(taxon["id"])
                taken += 1
                time.sleep(0.5)
            print(f"{group}: {taken}")
            time.sleep(1)
    with open(OUT / "truth.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} photos in {OUT}")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 1.0)
