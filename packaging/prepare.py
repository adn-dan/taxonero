"""Prepare packaging/build for the desktop executable (light launcher, see bootstrap.py):
- wheel/: the taxonero wheel; constraints.txt;
- uv/: uv (Astral), pinned version, for this system and processor;
- exiftool/: ExifTool (fetch_exiftool.py).

    python packaging/prepare.py && pyinstaller packaging/taxonero.spec --noconfirm
"""

import hashlib
import io
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

UV_VERSION = "0.13.0"
# .sha256 files of the version, published by Astral: to update with UV_VERSION.
UV_SHA256 = {
    "x86_64-pc-windows-msvc": "088962f9e7b7bd9ea740c04c650b2a21c8928c345bd99ac24350dc924dba656c",
    "aarch64-pc-windows-msvc": "cb54028b59aa87f11cf6dec293ce000420043a49a8aea522e822a671321f8932",
    "x86_64-apple-darwin": "5f44dcbde809b632f47c36fadb241cb4d6f9af71d0c8f172b5d2026d3dde742c",
    "aarch64-apple-darwin": "a9c1b29002cf3c83f07fa9cd8a887a3be0107d90e23189721221e7257db8e3d6",
    "x86_64-unknown-linux-gnu": "1468ebd5a5541121837c5a2817b9972ba6090fa6caa3d142620850a47fb75154",
    "aarch64-unknown-linux-gnu": "3ccfb6af6e242433eb552f7d9676abd5412c6595c497e990d9c8cb7b5bd4d2c3",
}
HERE = Path(__file__).parent
ROOT = HERE.parent
BUILD = HERE / "build"


def uv_target() -> str:
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}[platform.machine().lower()]
    return {"win32": f"{arch}-pc-windows-msvc", "darwin": f"{arch}-apple-darwin"}.get(sys.platform, f"{arch}-unknown-linux-gnu")


def fetch_uv() -> None:
    target = uv_target()
    ext = "zip" if sys.platform == "win32" else "tar.gz"
    url = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{target}.{ext}"
    data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "taxonero"}), timeout=120).read()
    if hashlib.sha256(data).hexdigest() != UV_SHA256[target]:
        raise SystemExit(f"uv {UV_VERSION} ({target}): unexpected SHA-256 checksum")
    out = BUILD / "uv"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    name = "uv.exe" if sys.platform == "win32" else "uv"
    if ext == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            (out / name).write_bytes(z.read(next(n for n in z.namelist() if n.endswith("/" + name) or n == name)))
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            member = next(m for m in t.getmembers() if m.name.endswith("/" + name))
            (out / name).write_bytes(t.extractfile(member).read())
        (out / name).chmod(0o755)
    print(f"uv {UV_VERSION} ({target})")


def main() -> None:
    BUILD.mkdir(exist_ok=True)
    shutil.rmtree(BUILD / "wheel", ignore_errors=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "-w", str(BUILD / "wheel")], check=True)
    shutil.copy(ROOT / "constraints.txt", BUILD / "constraints.txt")
    fetch_uv()
    subprocess.run([sys.executable, str(HERE / "fetch_exiftool.py")], check=True)


if __name__ == "__main__":
    main()
