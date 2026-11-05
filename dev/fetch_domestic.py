"""Photos de test d'animaux domestiques (chiens, chats, chevaux, poules) depuis iNaturalist, toujours les
mêmes observations pour que les mesures restent comparables. Photos CC0/CC-BY, non versionnées : ce script
les retélécharge. Sert à vérifier que les animaux domestiques sont bien reconnus comme tels
(DOMESTIC_ANIMALS=skip), par exemple en comparant les modes d'OWLv2.
Sortie : dev/photos_domestic/*.jpg + dev/photos_domestic/truth.csv (licence et auteur de chaque photo)
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
                print(f"{obs['id']} : plus de photo sous licence libre, ignorée")
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
    print(f"{len(rows)} photos dans {OUT}")


if __name__ == "__main__":
    main()
