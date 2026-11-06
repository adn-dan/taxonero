"""Fetch ExifTool for the desktop executable, into packaging/build/exiftool:
- Windows: official standalone version (exiftool.exe + exiftool_files, Perl included);
- macOS and Linux: the Perl script and its library (Perl is present on these systems).

    python packaging/fetch_exiftool.py [version]      (default: the current version, exiftool.org/ver.txt)
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
# exiftool.org only keeps the latest version; SourceForge keeps all of them.
MIRRORS = ["https://exiftool.org/{f}", "https://sourceforge.net/projects/exiftool/files/{f}/download"]


def expected_sha256(name: str) -> str:
    """Checksum published by exiftool.org (checksums.txt, current version only)."""
    with urllib.request.urlopen(urllib.request.Request("https://exiftool.org/checksums.txt", headers={"User-Agent": "taxonero"}), timeout=60) as r:
        m = re.search(rf"SHA2-256\({re.escape(name)}\)= *([0-9a-f]{{64}})", r.read().decode())
    if not m:
        raise SystemExit(f"{name} missing from exiftool.org/checksums.txt: only the current version can be verified")
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
    raise SystemExit(f"ExifTool {VERSION} not found")


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
