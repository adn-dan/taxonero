"""Every icon file of Taxonero, from the two SVG sources of packaging/icons:
- taxonero.svg: the logo (from 32 px up);
- taxonero-small.svg: the tree alone, for 16 and 24 px (browser tab, smallest Windows icons).

    pip install cairosvg && python dev/make_icons.py

Written files (all committed, so that building needs no SVG renderer):
    packaging/icons/taxonero.png    512 px: Linux menus, AppImage, README, TrueNAS icon URL
    packaging/icons/taxonero.ico    Windows: executable, installer, Start menu (16 to 256 px)
    packaging/icons/taxonero.icns   macOS: Taxonero.app, .dmg (16 to 1024 px)
    src/taxonero/static/logo.svg            dashboard top bar
    src/taxonero/static/favicon.svg         browser tab
    src/taxonero/static/apple-touch-icon.png  180 px: phone home screen, window icon on Linux and macOS
    src/taxonero/static/app.ico             window and task bar icon on Windows (a copy of taxonero.ico)
"""

import io
import shutil
from pathlib import Path

import cairosvg
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "packaging" / "icons"
STATIC = ROOT / "src" / "taxonero" / "static"
LOGO, SMALL = ICONS / "taxonero.svg", ICONS / "taxonero-small.svg"


def render(svg: Path, size: int) -> Image.Image:
    return Image.open(io.BytesIO(cairosvg.svg2png(url=str(svg), output_width=size, output_height=size))).convert("RGBA")


def main() -> None:
    render(LOGO, 512).save(ICONS / "taxonero.png", optimize=True)
    # .ico: one image per size, the small drawing below 32 px (Pillow keeps the frames given in append_images).
    sizes = [16, 24, 32, 48, 64, 128, 256]
    frames = [render(SMALL if n < 32 else LOGO, n) for n in sizes]
    frames[-1].save(ICONS / "taxonero.ico", format="ICO", sizes=[(n, n) for n in sizes], append_images=frames[:-1])
    render(LOGO, 1024).save(ICONS / "taxonero.icns", format="ICNS")
    shutil.copyfile(LOGO, STATIC / "logo.svg")
    shutil.copyfile(SMALL, STATIC / "favicon.svg")
    render(LOGO, 180).save(STATIC / "apple-touch-icon.png", optimize=True)
    shutil.copyfile(ICONS / "taxonero.ico", STATIC / "app.ico")
    for p in (ICONS / "taxonero.png", ICONS / "taxonero.ico", ICONS / "taxonero.icns", STATIC / "logo.svg",
              STATIC / "favicon.svg", STATIC / "apple-touch-icon.png", STATIC / "app.ico"):
        print(f"{p.relative_to(ROOT)}: {p.stat().st_size} bytes")


if __name__ == "__main__":
    main()
