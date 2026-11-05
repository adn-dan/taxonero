"""Captures d'écran du README (docs/screenshots) et leurs crédits (docs/screenshots/CREDITS.md).

Travaille sur une copie de la base de dev, dont sont retirées les photos sans auteur ni licence connus (absentes de
dev/photos/truth.csv). Rien n'est écrit dans Immich. Demande l'Immich de dev lancé, et Playwright avec Firefox.

    python dev/screenshots.py
"""

import csv
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUT = ROOT / "docs" / "screenshots"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LICENSES = {"cc-by": ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
            "cc0": ("CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/")}


def prepare(tmp: Path, truth: dict) -> None:
    """Copie de la base, réduite aux photos créditées."""
    src, dst = sqlite3.connect(ROOT / "data" / "dev" / "taxonero.sqlite"), sqlite3.connect(tmp / "taxonero.sqlite")
    src.backup(dst)
    src.close()
    gone = [r[0] for r in dst.execute("SELECT asset_id, file_name FROM asset") if r[1] not in truth]
    for table in ("detection", "asset_sync", "image_quality", "written", "cover_choice", "asset"):
        cols = [c[1] for c in dst.execute(f"PRAGMA table_info({table})")]
        if "asset_id" in cols:
            dst.executemany(f"DELETE FROM {table} WHERE asset_id = ?", [(a,) for a in gone])
    dst.commit()
    dst.close()


def serve(tmp: Path):
    import socket

    import uvicorn

    from taxonero.config import Settings
    from taxonero.web import create_app
    from taxonero.worker import Worker
    import os
    for line in (ROOT / "dev" / ".env.dev").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k] = v
    os.environ.update(DATA_DIR=str(tmp), DASHBOARD_NO_AUTH="true")
    s = Settings()
    w = Worker(s)
    w.check()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(s, w), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}/", server


VISIBLE = """() => { const ids = [];
  document.querySelectorAll('img, [style*="background"]').forEach(el => { const r = el.getBoundingClientRect();
    if (r.width < 8 || r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth || !el.checkVisibility()) return;
    ids.push(el.src || el.getAttribute('style') || ''); });
  return ids; }"""
FIND = """P.assets.findIndex(a => a.lat != null && a.kind !== 'VIDEO' && a.detections?.some(d => d.status === 'pending'
  && d.candidates?.filter(c => c.gbif_key).length > 2 && d.w * d.h > .15))"""


def capture(base: str, browser, lang: str, used: dict, refs: dict) -> None:
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale={"fr": "fr-FR", "en": "en-US"}[lang], bypass_csp=True)
    page = ctx.new_page()
    (OUT / lang).mkdir(parents=True, exist_ok=True)

    def shot(name: str, extra: tuple = ()) -> None:
        page.wait_for_timeout(1500)
        page.screenshot(path=OUT / lang / f"{name}.jpg", type="jpeg", quality=82)
        # Visionneuse et comparaison couvrent la grille : seule leur photo se voit.
        ids = set(extra) or {m for s in page.evaluate(VISIBLE) for m in UUID.findall(s)}
        used.setdefault(name, set()).update(ids)

    page.goto(base + "#photos")
    page.wait_for_selector("#pfilters [data-f=all]")
    page.click("#pfilters [data-f=all]")
    page.wait_for_selector("#pfilters [data-f=all].on")
    page.wait_for_selector("#pgrid .tile img")
    page.wait_for_load_state("networkidle")
    shot("photos")
    i = page.evaluate(FIND)
    assert i >= 0, "aucune photo géolocalisée à relire avec trois candidats"
    page.evaluate(f"openViewer(P.assets, {i})")
    page.wait_for_selector("#viewer.on .cand")
    page.evaluate("V.sel = cur().detections.findIndex(d => d.status === 'pending' && d.candidates?.filter(c => c.gbif_key).length > 2); drawBoxes(); drawInfo()")
    page.wait_for_load_state("networkidle")
    aid = page.evaluate("cur().asset_id")
    shot("viewer", (aid,))
    page.click(".cand >> nth=0")
    page.wait_for_selector("#compare.on .cmpimg.on", timeout=30000)
    page.wait_for_function("document.querySelector('#cmp-big img')?.complete")
    page.wait_for_load_state("networkidle")
    shot("compare", (aid,))
    if not refs:
        refs.update(page.evaluate("""() => { const out = {};
          document.querySelectorAll('#compare .cmpimg').forEach(b => { const r = b.getBoundingClientRect();
            if (r.top < innerHeight) out[b.querySelector('img').src] = b.title; });
          return out; }"""))
    page.keyboard.press("Escape"); page.keyboard.press("Escape")
    page.wait_for_selector("#viewer:not(.on)", state="attached")
    page.goto(base + "#species")
    page.wait_for_selector("#v-species .frow")
    page.wait_for_load_state("networkidle")
    shot("species")
    page.click("#v-species [data-mode=tree]")
    page.wait_for_selector("#v-species .tnode")
    page.wait_for_load_state("networkidle")
    shot("tree")
    page.click("#v-species [data-mode=grid]")   # vue gardée par le navigateur : la remettre comme avant
    key =page.evaluate("SP.all.slice().sort((a, b) => b.n_photos - a.n_photos)[0]?.taxon_key ?? SP.all[0].taxon_key")
    page.goto(base + f"#species/{key}")
    page.wait_for_selector(".leaflet-container", timeout=20000)
    page.wait_for_load_state("networkidle")
    page.add_style_tag(content=".wbody img, .wbody figure { display: none !important; }")   # images de l'article : licences propres
    shot("species-page")
    page.goto(base + "#stats")
    page.wait_for_selector("#statsmap.leaflet-container", timeout=20000)
    page.wait_for_load_state("networkidle")
    shot("stats")
    ctx.close()


def credits(used: dict, refs: dict, truth: dict, names: dict) -> None:
    lines = ["# Credits for the screenshots", "",
             "The code license of this project does not cover the photographs visible in these screenshots. They stay",
             "under the licenses below. They come from [iNaturalist](https://www.inaturalist.org) and were resized and",
             "cropped by the interface; GPS and date metadata were added to the test copies. No endorsement by the authors is implied.", ""]
    for shot, ids in used.items():
        rows = sorted({names[a] for a in ids if a in names})
        if not rows:
            continue
        lines += [f"## {shot}", "", "| Photo | Species | Author and license | Source |", "|---|---|---|---|"]
        for f in rows:
            t = truth[f]
            name, url = LICENSES[t["license"]]
            lines.append(f"| {f} | *{t['name']}* | {t['attribution']} · [{name}]({url}) | [observation]({t['source']}) |")
        lines.append("")
    if refs:
        lines += ["## compare: reference photos", "",
                  "Shown as served by iNaturalist, with the attribution given by iNaturalist:", ""]
        lines += [f"- {a}" for a in sorted(set(refs.values()))]
        lines.append("")
    lines += ["## Other content", "",
              "- Species names, taxonomy and range maps: [GBIF](https://www.gbif.org) (CC0 / CC BY depending on the dataset).",
              "- Article excerpts: [Wikipedia](https://www.wikipedia.org) (CC BY-SA 4.0). Their images are left out of the screenshots.",
              "- Base maps: as credited on each map.", ""]
    (OUT / "CREDITS.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    from playwright.sync_api import sync_playwright
    with open(ROOT / "dev" / "photos" / "truth.csv", newline="") as f:
        truth = {r["file"]: r for r in csv.DictReader(f) if r.get("attribution") and r.get("license") in LICENSES}
    tmp = Path(tempfile.mkdtemp())
    try:
        prepare(tmp, truth)
        base, server = serve(tmp)
        used, refs = {}, {}
        with sync_playwright() as p:
            browser = p.firefox.launch()
            for lang in ("en", "fr"):
                capture(base, browser, lang, used, refs)
            browser.close()
        server.should_exit = True
        db = sqlite3.connect(tmp / "taxonero.sqlite")
        names = dict(db.execute("SELECT asset_id, file_name FROM asset"))
        db.close()
        unknown = {a for ids in used.values() for a in ids if a not in names}
        if unknown:
            raise SystemExit(f"images sans crédit visibles : {unknown}")
        credits(used, refs, truth, names)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
