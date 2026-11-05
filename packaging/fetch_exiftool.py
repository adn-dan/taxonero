"""Récupère ExifTool pour l'exécutable de bureau, dans packaging/build/exiftool :
- Windows : version autonome officielle (exiftool.exe + exiftool_files, Perl compris) ;
- macOS et Linux : le script Perl et sa bibliothèque (Perl est présent sur ces systèmes).

    python packaging/fetch_exiftool.py [version]      (défaut : la version courante, exiftool.org/ver.txt)
"""

import hashlib
import io
import re
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

def current() -> str:
    with urllib.request.urlopen(urllib.request.Request("https://exiftool.org/ver.txt", headers={"User-Agent": "taxonero"}), timeout=60) as r:
        return r.read().decode().strip()


VERSION = sys.argv[1] if len(sys.argv) > 1 else current()
OUT = Path(__file__).parent / "build" / "exiftool"
# exiftool.org ne garde que la dernière version ; SourceForge les garde toutes.
MIRRORS = ["https://exiftool.org/{f}", "https://sourceforge.net/projects/exiftool/files/{f}/download"]


def expected_sha256(name: str) -> str:
    """Empreinte publiée par exiftool.org (checksums.txt, version courante seulement)."""
    with urllib.request.urlopen(urllib.request.Request("https://exiftool.org/checksums.txt", headers={"User-Agent": "taxonero"}), timeout=60) as r:
        m = re.search(rf"SHA2-256\({re.escape(name)}\)= *([0-9a-f]{{64}})", r.read().decode())
    if not m:
        raise SystemExit(f"{name} absent de exiftool.org/checksums.txt : seule la version courante est vérifiable")
    return m[1]


def fetch(name: str) -> bytes:
    want = expected_sha256(name)
    for m in MIRRORS:
        try:
            with urllib.request.urlopen(urllib.request.Request(m.format(f=name), headers={"User-Agent": "taxonero"}), timeout=120) as r:
                data = r.read()
            if hashlib.sha256(data).hexdigest() != want:
                raise ValueError("empreinte SHA-256 inattendue")
            return data
        except Exception as e:
            print(f"{m.format(f=name)} : {e}")
    raise SystemExit(f"ExifTool {VERSION} introuvable")


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    if sys.platform == "win32":
        with zipfile.ZipFile(io.BytesIO(fetch(f"exiftool-{VERSION}_64.zip"))) as z:
            z.extractall(OUT.parent / "exiftool-zip")
        src = next((OUT.parent / "exiftool-zip").iterdir())
        for p in src.iterdir():
            shutil.move(str(p), OUT / ("exiftool.exe" if p.name.startswith("exiftool(") else p.name))
        shutil.rmtree(OUT.parent / "exiftool-zip")
    else:
        with tarfile.open(fileobj=io.BytesIO(fetch(f"Image-ExifTool-{VERSION}.tar.gz"))) as t:
            t.extractall(OUT.parent / "exiftool-src", filter="data")
        src = OUT.parent / "exiftool-src" / f"Image-ExifTool-{VERSION}"
        shutil.copy(src / "exiftool", OUT / "exiftool")
        shutil.copytree(src / "lib", OUT / "lib")
        shutil.rmtree(OUT.parent / "exiftool-src")
    print(f"ExifTool {VERSION} : {OUT}")


if __name__ == "__main__":
    main()
