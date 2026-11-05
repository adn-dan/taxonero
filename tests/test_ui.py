"""Tests du dashboard dans un vrai navigateur (Playwright + Firefox), sur l'Immich de dev et une copie de la
base de dev, identification simulée. Ils attrapent ce que les tests d'API ne voient pas : une erreur JavaScript,
un fichier de script manquant, une page qui ne s'affiche plus, un débordement sur téléphone.

    pip install -e '.[dev]' && playwright install firefox

Ignorés si Playwright, Firefox ou l'Immich de dev manquent."""

import socket
import threading
import time
from pathlib import Path

import pytest

from conftest import DEV_READY

sync_api = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.skipif(not DEV_READY, reason="Immich de dev non initialisé")
PHOTO = next(iter(sorted((Path(__file__).parent.parent / "dev" / "photos").glob("inat_*.jpg"))), None)


@pytest.fixture
def base_url(dev_app):
    """Dashboard servi par uvicorn sur un port libre, le temps du test."""
    import uvicorn
    from taxonero.web import create_app
    s, w = dev_app
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(s, w), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/"
    server.should_exit = True
    thread.join(5)


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.firefox.launch()
        except Exception as e:   # navigateur non installé
            pytest.skip(f"Firefox pour Playwright absent : {e}")
        yield b
        b.close()


@pytest.fixture(params=[(1440, 900), (390, 844)], ids=["ordinateur", "telephone"])
def page(request, browser, base_url):
    w, h = request.param
    # bypass_csp : wait_for_function passe par eval(), que la CSP du dashboard refuse (test_csp_allows_every_page la vérifie).
    ctx = browser.new_context(viewport={"width": w, "height": h}, locale="fr-FR", bypass_csp=True)
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.goto(base_url)
    pg.wait_for_selector("#syncline, #server", state="attached")
    yield pg
    ctx.close()


def _no_overflow(pg):
    """Pas de défilement horizontal de la page (téléphone surtout)."""
    assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "la page déborde en largeur"


@pytest.mark.parametrize("view", ["photos", "species", "stats", "settings", "identify"])
def test_pages_render_without_errors(page, view):
    page.goto(page.url.split("#")[0] + f"#{view}")
    page.wait_for_selector(f"#v-{view}.on")
    page.wait_for_timeout(1500)   # chargements asynchrones
    assert not page.errors, page.errors
    assert page.inner_text(f"#v-{view}").strip()
    _no_overflow(page)


def test_species_grid_and_tree(page):
    page.goto(page.url.split("#")[0] + "#species")
    page.wait_for_selector(".sptile")
    page.click("[data-mode=tree]")
    page.wait_for_selector(".tnode")
    page.click(".tleaf >> nth=0")
    page.wait_for_selector("#sppage .sphero")                  # toujours la page de l'espèce
    assert "#species/" in page.url
    page.goto(page.url.split("#")[0] + "#species")
    page.click("[data-mode=grid]")            # remet la vue par défaut pour les tests suivants
    assert not page.errors, page.errors


def test_family_page_from_species_page(page):
    page.goto(page.url.split("#")[0] + "#species")
    page.wait_for_selector(".sptile")
    page.click(".sptile >> nth=0")
    page.click('#sppage .crumbs a[href^="#family/"]')          # la famille, dans le fil de classification
    page.wait_for_selector("#fapage .sptile")
    page.wait_for_selector("#famap.leaflet-container")
    assert "#family/" in page.url and page.inner_text("#fapage h1")
    assert not page.errors, page.errors
    _no_overflow(page)


@pytest.mark.skipif(PHOTO is None, reason="pas de photo de test (dev/fetch_dataset.py)")
def test_identify_shows_result_and_map(page):
    page.goto(page.url.split("#")[0] + "#identify")
    page.set_input_files("#idfile", str(PHOTO))
    page.wait_for_selector(".idcard")
    page.wait_for_selector("#idmore .rmap.leaflet-container", timeout=20000)
    assert "compact" in page.get_attribute("#idzone", "class")
    assert not page.errors, page.errors
    _no_overflow(page)


def test_identify_location_button(page):
    """Image sans GPS : bouton sous la photo, identification relancée avec le lieu, qui reste modifiable."""
    import io
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (300, 200), "green").save(buf, "JPEG")
    page.goto(page.url.split("#")[0] + "#identify")
    page.set_input_files("#idfile", {"name": "sans-gps.jpg", "mimeType": "image/jpeg", "buffer": buf.getvalue()})
    page.wait_for_selector("#id-loc")
    urls = []
    page.on("request", lambda r: urls.append(r.url) if "/api/identify" in r.url else None)
    page.click("#id-loc")
    page.wait_for_selector("#locpick.on #lpmap.leaflet-container")
    bb = page.locator("#lpmap").bounding_box()
    page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
    page.click("#locpick [data-a=save]")
    page.wait_for_function("ID.place && document.querySelector('.idphoto p')?.textContent.includes('📍')")
    assert "lat=" in urls[0] and page.is_visible("#id-loc")
    assert not page.errors, page.errors


def test_settings_immich_connection(page):
    page.goto(page.url.split("#")[0] + "#settings")
    page.wait_for_selector("#immcard")
    assert page.is_disabled("#imm-key")                          # clé venue de .env : non modifiable ici
    page.click("#imm-test")
    page.wait_for_selector("#imm-state .immst.ok")
    assert page.locator("#imm-perms code.perm.off").count() == 0
    assert not page.errors, page.errors
    _no_overflow(page)


def test_settings_tabs(page):
    page.goto(page.url.split("#")[0] + "#settings")
    page.wait_for_selector("#settab-source[aria-selected=true]")
    assert page.is_visible("#immcard") and not page.is_visible("#set-detection")
    page.click("#settab-analysis")
    assert page.is_visible("#set-detection") and not page.is_visible("#immcard")
    page.focus("#settab-analysis")
    page.keyboard.press("ArrowRight")                            # onglet suivant au clavier
    assert page.get_attribute("#settab-validation", "aria-selected") == "true"
    assert page.is_visible("#set-auto_confirm")
    page.reload()
    page.wait_for_selector("#settab-validation[aria-selected=true]")   # onglet retenu
    page.click("#settab-source")
    assert not page.errors, page.errors
    _no_overflow(page)


def test_warning_link_opens_source_tab_with_the_warning(page, dev_app):
    _, w = dev_app
    w.client.warnings = ["alerte de test"]
    page.goto(page.url.split("#")[0] + "#settings")
    page.wait_for_selector("#settab-source")
    page.reload()
    page.wait_for_selector("#settab-source")
    page.click("#settab-models")
    if not page.is_visible("#server [data-settab]"):             # téléphone : volet replié
        page.evaluate("document.querySelector('#server [data-settab]').click()")
    else:
        page.click("#server [data-settab]")
    assert page.get_attribute("#settab-source", "aria-selected") == "true"   # déjà sur Paramètres
    page.wait_for_selector("#immcard")
    page.goto(page.url.split("#")[0] + "#stats")
    page.wait_for_selector("#v-stats.on")
    page.evaluate("localStorage.setItem('settings-tab', 'models')")
    page.evaluate("document.querySelector('#server [data-settab]').click()")  # depuis une autre page
    page.wait_for_selector("#settab-source[aria-selected=true]")
    page.wait_for_selector("#immcard:has-text('alerte de test')")   # le texte de l'avertissement est là
    assert not page.errors, page.errors


def test_undo_requires_the_typed_word(page, dev_app):
    """Aucune écriture ici : le mode test refuse, puis un faux tag fait refuser par le contrôle du serveur."""
    import sqlite3
    s, w = dev_app
    db = sqlite3.connect(s.db_path)
    db.execute("DELETE FROM written")
    db.execute("INSERT INTO written VALUES ('tag', '00000000-0000-4000-8000-000000000000', '', 'Nature', 1)")
    db.commit()
    page.goto(page.url.split("#")[0] + "#settings")
    page.wait_for_selector("#settab-source[aria-selected=true]")
    page.wait_for_selector("#undocard")
    assert "DRY_RUN" in page.inner_text("#undocard") and page.locator("#undo-go").count() == 0
    s.dry_run = False                                           # le client, lui, reste en dry-run : rien ne peut partir
    page.reload()
    page.wait_for_selector("#undo-go")
    assert page.is_disabled("#undo-go")
    page.fill("#undo-word", "non")
    assert page.is_disabled("#undo-go")
    page.fill("#undo-word", "RETIRER")
    assert page.is_enabled("#undo-go")
    page.click("#undo-go")
    page.wait_for_selector("#toast:has-text('autre Immich')")
    assert db.execute("SELECT count(*) FROM written").fetchone()[0] == 1
    assert store_state(db) != "1"
    assert not page.errors, page.errors
    _no_overflow(page)


def store_state(db):
    r = db.execute("SELECT value FROM state WHERE key = 'writes_paused'").fetchone()
    return r and r[0]


def test_settings_logs_tab(page):
    import logging
    logging.getLogger("taxonero.test").warning("<b>ligne</b> avertissement")
    logging.getLogger("taxonero.test").error("ligne erreur")
    page.goto(page.url.split("#")[0] + "#settings")
    page.click("#settab-logs")
    page.wait_for_selector(".logline.lv-error")
    assert page.locator(".logline b").count() == 0                # texte échappé
    assert "<b>ligne</b> avertissement" in page.inner_text("#logbox")
    logging.getLogger("taxonero.test").error("ligne suivante")
    page.wait_for_function("document.querySelector('#logbox').innerText.includes('ligne suivante')")   # relevé
    page.click("#log-level [data-o=err]")
    assert "avertissement" not in page.inner_text("#logbox") and "ligne erreur" in page.inner_text("#logbox")
    page.click("#log-level [data-o=all]")
    page.once("dialog", lambda d: d.accept())
    page.click("#log-clear")                                         # « Effacer », après confirmation
    page.wait_for_selector("#logbox .sub")
    assert page.locator(".logline").count() == 0
    logging.getLogger("taxonero.test").error("après effacement")
    page.wait_for_function("document.querySelector('#logbox').innerText.includes('après effacement')")
    assert "ligne erreur" not in page.inner_text("#logbox")          # les lignes effacées ne reviennent pas
    assert not page.errors, page.errors
    _no_overflow(page)


def test_tile_opens_with_keyboard(page):
    """Entrée sur une tuile ouvre la visionneuse, sans valider aussitôt une détection (même touche)."""
    page.goto(page.url.split("#")[0] + "#photos")
    page.wait_for_selector("#pgrid .tile")
    before = page.evaluate("fetch('/api/review-stats').then(r => r.json())")
    page.focus("#pgrid .tile >> nth=0")
    page.keyboard.press("Enter")
    page.wait_for_selector("#viewer.on")
    page.wait_for_timeout(500)
    assert page.evaluate("fetch('/api/review-stats').then(r => r.json())") == before
    page.keyboard.press("Escape")
    assert not page.errors, page.errors


def test_timeline_and_month_selection(page):
    page.goto(page.url.split("#")[0] + "#photos?filter=all")
    page.wait_for_selector("#pgrid .tile")
    months = page.evaluate("P.months")
    if len(months) < 2:
        pytest.skip("photos de dev sur un seul mois")
    last = months[-1]["m"]
    # Frise de défilement (toutes largeurs) : survol, le mois s'affiche ; clic au milieu du dernier mois, on y va.
    box = page.locator("#ptimeline").bounding_box()
    r = page.evaluate("P.tl[P.tl.length - 1]")
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] * (r["y0"] + r["h"] / 2))
    assert page.locator("#ptimeline .tlhover span").is_visible()
    assert page.evaluate("monthLabel(P.tl[P.tl.length - 1].m)") in page.inner_text("#ptimeline .tlhover")
    page.mouse.down()
    page.mouse.up()
    page.wait_for_function(f"P.assets.some(a => (a.taken_at ? a.taken_at.slice(0, 7) : null) === {last!r})".replace("None", "null"))
    page.click("#pgrid .month >> nth=0 >> .msel")          # tout le mois
    n = page.evaluate("document.querySelector('#pgrid .month').nextElementSibling.querySelectorAll('.tile').length")
    assert n and page.evaluate("selCount()") >= n
    page.click("#selbar [data-b=none]")
    assert not page.errors, page.errors
    _no_overflow(page)
    if page.viewport_size["width"] < 1100:                 # menu « Aller à » sur petit écran
        page.select_option("#pgoto", months[0]["m"] or "")
        page.wait_for_function(f"P.assets.some(a => (a.taken_at ? a.taken_at.slice(0, 7) : null) === {months[0]['m']!r})")


def test_compare_with_reference_photos(page, monkeypatch):
    """Comparaison en images dans la visionneuse : rangée de photos de référence, clic : à côté du sujet ;
    Échap ferme la comparaison sans fermer la visionneuse."""
    pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="
    monkeypatch.setattr("taxonero.refphotos.photos", lambda tx, names, synonyms=None: {
        "taxon": {"id": 1, "name": names[0], "url": "https://www.inaturalist.org/taxa/1"},
        "photos": [{"thumb": pixel, "large": pixel, "attribution": "(c) test", "license": "CC-BY", "url": None}] * 3})
    page.goto(page.url.split("#")[0] + "#photos?filter=pending")
    page.wait_for_selector("#pgrid .tile")
    page.click("#pgrid .tile >> nth=0")
    page.wait_for_selector("#viewer.on .cand")
    page.click(".cand >> nth=0")
    page.wait_for_selector("#compare.on .cmpimg")
    page.wait_for_selector("#cmp-big img")                 # première photo du premier candidat, à côté du sujet
    page.click("#compare .cmprow >> nth=0 >> .cmpimg >> nth=2")
    assert "(c) test" in page.inner_text("#cmp-big")
    # Zoom : molette et double clic sur chaque photo, indépendamment ; retour par double clic.
    box = page.locator("#cmp-big .cmpzoom").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.wheel(0, -300)
    page.wait_for_selector("#cmp-big .cmpzoom.zoomed")
    assert not page.locator("#cmp-mine.zoomed").count()
    page.mouse.dblclick(x, y)
    page.wait_for_selector("#cmp-big .cmpzoom:not(.zoomed)")
    box = page.locator("#cmp-mine").bounding_box()
    page.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_selector("#cmp-mine.zoomed")
    page.keyboard.press("Escape")
    assert not page.is_visible("#compare") and page.is_visible("#viewer")
    page.keyboard.press("Escape")
    assert not page.errors, page.errors


def test_search_suggestions(page):
    page.goto(page.url.split("#")[0] + "#photos")
    first = page.evaluate("fetch('/api/species').then(r => r.json()).then(l => l[0].taxon)")
    word = (first["vernacular"].get("fr") or first["scientific_name"])[:4]
    page.fill("#gsearch", word)
    page.wait_for_selector("#sugbox .sug")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_function("location.hash.startsWith('#species/') || /^#(kingdom|phylum|class|order|family|genus)\\//.test(location.hash)")
    assert page.input_value("#gsearch") == "" and page.is_hidden("#sugbox")
    assert not page.errors, page.errors


def test_photo_selection_and_zoom(page):
    """Sélection (tout le filtre, glisser) et zoom de la visionneuse, sans recharger la grille au retour."""
    page.goto(page.url.split("#")[0] + "#photos")
    page.click("#pfilters [data-f=all]")
    page.wait_for_selector("#pfilters [data-f=all].on")         # filtre rechargé (pas la grille d'avant)
    page.wait_for_selector("#pgrid .tile")
    total = page.evaluate("P.counts.all")
    page.locator("#pgrid .tile").nth(0).hover()
    page.locator("#pgrid .tile").nth(0).locator(".tick").click()               # première coche : sélection commencée
    page.click("#selbar [data-b=all]:visible" if page.viewport_size["width"] > 1000 else "#selbar summary")
    if page.viewport_size["width"] <= 1000:
        page.click("#selbar .menu [data-b=all]")
    assert page.evaluate("selCount()") == total
    page.locator("#pgrid .tile").nth(1).click()                  # décochée : exclue de « tout »
    assert page.evaluate("selCount()") == total - 1 and page.evaluate("P.excl.size") == 1
    page.click("#selbar [data-b=none]")
    assert page.evaluate("selCount()") == 0 and page.is_hidden("#selbar")
    # visionneuse sur une photo (pas une vidéo) : molette, Z, Échap
    page.locator("#pgrid .tile:not(:has(.vid))").first.click()
    page.wait_for_selector("#viewer.on")
    page.evaluate("document.querySelectorAll('#pgrid .tile')[0].dataset.mark = 1")
    bb = page.locator("#frame").bounding_box()
    page.mouse.move(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
    for _ in range(4):
        page.mouse.wheel(0, -100)
    page.wait_for_function("pz.getScale() > 1.3 && pz.getScale() < 1.6 && $('#frame').classList.contains('zoomed')")   # ×1,1 par cran
    page.wait_for_timeout(500)                                   # crans de molette encore en route
    page.keyboard.press("z")                                     # zoomé : Z revient à la photo entière
    page.wait_for_function("pz.getScale() < 1.001")
    page.keyboard.press("Escape")
    page.wait_for_selector("#viewer:not(.on)", state="attached")
    page.wait_for_timeout(300)
    assert page.evaluate("document.querySelectorAll('#pgrid .tile')[0].dataset.mark") == "1"   # grille gardée
    assert not page.errors, page.errors


def test_viewer_click_zooms_and_never_draws(page):
    """Souris : un clic zoome, le suivant revient ; un glisser ne trace rien sans le mode dessin ; zoomé sur le sujet
    sélectionné, un glisser déplace la photo sans toucher au rectangle. Toutes les actions sont visibles d'un coup."""
    if page.viewport_size["width"] < 800:
        pytest.skip("souris : ordinateur seulement")
    page.goto(page.url.split("#")[0] + "#photos")
    page.click("#pfilters [data-f=all]")
    page.wait_for_selector("#pfilters [data-f=all].on")
    page.wait_for_selector("#pgrid .tile")
    i = page.evaluate("P.assets.findIndex(a => a.kind !== 'VIDEO' && a.detections.length === 1 && a.detections[0].w * a.detections[0].h > .05)")
    if i < 0:
        pytest.skip("aucune photo à un sujet dans la base de dev")
    page.evaluate(f"openViewer(P.assets, {i})")
    page.wait_for_function("$('#photo').complete && $('#photo').naturalWidth && document.querySelectorAll('.a9s-annotation').length")
    for sel in ("#v-draw", "#v-rescan", "#v-del"):
        assert page.is_visible(sel)
    assert not page.locator("#v-more").count()
    sent = []
    page.on("request", lambda r: sent.append(r.url) if r.method in ("POST", "PUT") and "/box" in r.url else None)
    d = page.evaluate("curDet()")
    pt = lambda: page.evaluate(f"(() => {{ const b = $('#photo').getBoundingClientRect(); return [b.left + {d['x'] + d['w'] / 2} * b.width, b.top + {d['y'] + d['h'] / 2} * b.height] }})()")
    x, y = pt()
    page.mouse.move(x, y); page.mouse.down(); page.mouse.move(x + 60, y + 60, steps=6); page.mouse.up()   # sans zoom
    page.wait_for_timeout(400)
    assert not sent and page.evaluate("pz.getScale()") < 1.001
    x, y = pt()
    page.mouse.click(x, y)
    page.wait_for_function("pz.getScale() > 2.9")
    page.wait_for_function("anno.getSelected()[0]?.id === String(curDet().id)")
    before = page.evaluate("$('#photo').getBoundingClientRect().left")
    box = page.evaluate("JSON.stringify(curDet())")
    page.wait_for_timeout(400)
    page.mouse.move(x, y); page.mouse.down(); page.mouse.move(x + 50, y, steps=6); page.mouse.up()         # zoomé : déplacement
    page.wait_for_timeout(600)
    assert not sent and page.evaluate("JSON.stringify(curDet())") == box
    assert abs(page.evaluate("$('#photo').getBoundingClientRect().left") - before) > 20
    page.mouse.click(x + 50, y)
    page.wait_for_function("pz.getScale() < 1.001")
    page.wait_for_timeout(400)
    x, y = pt()
    page.mouse.dblclick(x, y)                              # double clic : reste zoomé
    page.wait_for_function("pz.getScale() > 2.9")
    assert not page.errors, page.errors


def test_viewer_boxes_draw_and_edit(page):
    """Rectangles (Annotorious) : tracé d'une boîte à la souris, redimensionnement du rectangle sélectionné sous
    zoom (Panzoom), coordonnées envoyées au serveur justes dans les deux cas."""
    if page.viewport_size["width"] < 800:
        pytest.skip("souris : ordinateur seulement")
    page.goto(page.url.split("#")[0] + "#photos")
    page.click("#pfilters [data-f=all]")
    page.wait_for_selector("#pfilters [data-f=all].on")
    page.wait_for_selector("#pgrid .tile")
    i = page.evaluate("P.assets.findIndex(a => a.kind !== 'VIDEO' && a.detections.length)")
    page.evaluate(f"openViewer(P.assets, {i})")
    page.wait_for_function("$('#photo').complete && $('#photo').naturalWidth && document.querySelectorAll('.a9s-annotation').length")
    sent = []
    page.on("request", lambda r: sent.append((r.method, r.url.split("/api/")[1], r.post_data_json)) if r.method in ("POST", "PUT") and ("/boxes" in r.url or "/box" in r.url) else None)
    r = page.evaluate("(() => { const b = $('#photo').getBoundingClientRect(); return [b.left, b.top, b.width, b.height] })()")
    # tracé sans zoom, dans un coin libre de rectangles, sur 20 % x 20 % de la photo
    dets = page.evaluate("cur().detections.map(d => [d.x, d.y, d.w, d.h])")
    n0 = len(dets)
    free = lambda x, y: all(not (dx - .01 < x < dx + dw + .01 and dy - .01 < y < dy + dh + .01) for dx, dy, dw, dh in dets)
    x0, y0 = next((x, y) for x, y in ((.05, .05), (.75, .05), (.05, .75), (.75, .75)) if all(free(x + i * .2, y + j * .2) for i in (0, .5, 1) for j in (0, .5, 1)))
    page.click("#v-draw")                                  # une seule action, sans menu
    page.mouse.move(r[0] + r[2] * x0, r[1] + r[3] * y0); page.mouse.down()
    page.mouse.move(r[0] + r[2] * (x0 + .1), r[1] + r[3] * (y0 + .1), steps=5); page.mouse.move(r[0] + r[2] * (x0 + .2), r[1] + r[3] * (y0 + .2), steps=5); page.mouse.up()
    page.wait_for_function(f"cur().detections.length > {n0} && anno.getSelected()[0]?.id === String(curDet().id)")
    box = [s for s in sent if s[1].endswith("/boxes")][0][2]
    assert abs(box["x"] - x0) < .01 and abs(box["y"] - y0) < .01 and abs(box["w"] - .2) < .01 and abs(box["h"] - .2) < .01, box
    # zoom ×2 sur le coin bas-droit du rectangle sélectionné (il reste en place), puis ce coin tiré de 40 px
    d = page.evaluate("curDet()")
    page.evaluate(f"(() => {{ const b = $('#photo').getBoundingClientRect(); zoomAt(b.left + {d['x'] + d['w']} * b.width, b.top + {d['y'] + d['h']} * b.height, 2) }})()")
    page.wait_for_function("pz.getScale() > 1.9")
    page.wait_for_timeout(200)
    corner = page.evaluate(f"(() => {{ const b = $('#photo').getBoundingClientRect(); return [b.left + {d['x'] + d['w']} * b.width, b.top + {d['y'] + d['h']} * b.height, b.width] }})()")
    page.mouse.move(corner[0] - 1, corner[1] - 1); page.mouse.down(); page.mouse.move(corner[0] + 40, corner[1] + 40, steps=8); page.mouse.up()
    page.wait_for_timeout(800)
    put = [s for s in sent if s[0] == "PUT"]
    assert put, sent
    b = put[0][2]
    assert abs(b["x"] - d["x"]) < .01 and abs(b["y"] - d["y"]) < .01
    assert abs(b["w"] - min(1 - d["x"], d["w"] + 41 / corner[2])) < .015, (b, d, corner)
    assert not page.errors, page.errors


def test_compare_keyboard_click_zoom_and_plausibility(page, monkeypatch):
    """Comparaison : 1-5 candidat, flèches photo, Entrée choisit ; clic : zoom ; région affichée si le lieu est connu."""
    pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="
    monkeypatch.setattr("taxonero.refphotos.photos", lambda tx, names, synonyms=None: {
        "taxon": None, "photos": [{"thumb": pixel, "large": pixel, "attribution": f"(c) {k}", "license": "CC-BY", "url": None} for k in range(3)]})
    page.goto(page.url.split("#")[0] + "#photos")
    page.click("#pfilters [data-f=all]")
    page.wait_for_selector("#pfilters [data-f=all].on")
    page.wait_for_selector("#pgrid .tile")
    i = page.evaluate("P.assets.findIndex(a => a.lat != null && a.kind !== 'VIDEO' && a.detections?.some(d => d.status === 'pending' && d.candidates?.filter(c => c.gbif_key).length > 1))")
    if i < 0:
        pytest.skip("aucune photo géolocalisée à relire avec plusieurs candidats")
    page.evaluate(f"openViewer(P.assets, {i})")
    page.wait_for_selector("#viewer.on .cand")
    page.evaluate("V.sel = cur().detections.findIndex(d => d.status === 'pending' && d.candidates?.filter(c => c.gbif_key).length > 1); drawBoxes(); drawInfo()")
    page.wait_for_selector("#viewer.on .cand")
    page.click(".cand >> nth=0")
    page.wait_for_selector("#compare.on .cmpimg.on")
    assert page.locator("#compare .cmprow >> nth=0 >> .plaus").count() == 1
    assert "(c) 0" in page.inner_text("#cmp-big")
    page.keyboard.press("ArrowRight")
    page.wait_for_function("document.querySelector('#cmp-big').innerText.includes('(c) 1')")
    page.keyboard.press("ArrowLeft"); page.keyboard.press("ArrowLeft")
    page.wait_for_function("document.querySelector('#cmp-big').innerText.includes('(c) 2')")
    page.keyboard.press("2")
    page.wait_for_selector("#compare .cmprow.cur >> nth=0")
    key2 = page.evaluate("CMP.cands[1].gbif_key")
    assert page.evaluate("CMP.current") == key2
    page.wait_for_function("document.querySelector('#cmp-big').innerText.includes('(c) 0')")
    if page.viewport_size["width"] >= 800:
        bb = page.locator("#cmp-mine").bounding_box()
        page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        page.wait_for_selector("#cmp-mine.zoomed")
        page.wait_for_timeout(400)
        page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        page.wait_for_selector("#cmp-mine:not(.zoomed)")
    page.keyboard.press("Enter")                           # choisit le candidat 2, et ferme
    page.wait_for_selector("#compare:not(.on)", state="attached")
    assert not page.errors, page.errors


def test_other_groups_fold_under_one_chip(page):
    """Classes créées à la volée : une seule pastille « Autres », qui les déplie."""
    page.goto(page.url.split("#")[0] + "#photos")
    page.wait_for_selector("#pgrid .tile")
    page.evaluate("P.groups = [{group: 'Aves', n: 5}, {group: 'Bacilli', n: 2}, {group: 'Clostridia', n: 3}]; P.group = ''; renderTools()")
    assert "5" in page.inner_text("#ptools [data-gmore]")
    bacilli = page.evaluate("GROUPS.find(g => g.name === 'Bacilli').id")
    assert not page.is_visible(f"#ptools [data-g={bacilli}]")
    page.click("#ptools [data-gmore]")
    assert page.is_visible(f"#ptools [data-g={bacilli}]")
    page.click("#ptools [data-gmore]")
    assert not page.is_visible(f"#ptools [data-g={bacilli}]")
    assert not page.errors, page.errors


def test_compare_browses_the_series(page, monkeypatch, dev_app):
    """Comparaison : les autres photos de la série se parcourent, et le choix reste pour la détection de départ."""
    import sqlite3
    pixel = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="
    monkeypatch.setattr("taxonero.refphotos.photos", lambda tx, names, synonyms=None: {
        "taxon": None, "photos": [{"thumb": pixel, "large": pixel, "attribution": "(c)", "license": "CC-BY", "url": None}]})
    db = sqlite3.connect(dev_app[0].db_path)
    rows = db.execute("SELECT d.id, d.asset_id FROM detection d JOIN asset a USING (asset_id) WHERE d.status = 'pending' "
                      "AND a.kind IS NOT 'VIDEO' AND a.missing_at IS NULL GROUP BY d.asset_id LIMIT 2").fetchall()
    if len(rows) < 2:
        pytest.skip("il faut deux photos à relire")
    db.execute("UPDATE detection SET x = 0, y = 0, w = 1, h = 1 WHERE id = ?", (rows[1][0],))   # autre cadrage
    for k, (_, aid) in enumerate(rows):
        db.execute("UPDATE asset SET make = 'T', model = 'T', lat = NULL, lon = NULL, taken_at = ? WHERE asset_id = ?",
                   (f"2031-01-01T10:00:{k:02d}.000Z", aid))
    db.commit()
    page.goto(page.url.split("#")[0] + "#photos?filter=all")
    page.wait_for_selector("#pgrid .tile")
    i = page.evaluate(f"P.assets.findIndex(a => a.asset_id === {rows[0][1]!r})")
    assert i >= 0
    page.evaluate(f"openViewer(P.assets, {i})")
    page.wait_for_selector("#viewer.on .cand")
    page.click(".cand >> nth=0")
    page.wait_for_selector("#cmp-sn")
    assert page.inner_text("#cmp-sn") == "1/2"
    src = page.get_attribute("#cmp-mine img", "src")
    page.keyboard.press("ArrowDown")
    page.wait_for_function("s => document.querySelector('#cmp-mine img').src !== s", arg=src)
    assert page.inner_text("#cmp-sn") == "2/2"
    other = page.get_attribute("#cmp-mine img", "src")
    box = page.locator("#cmp-mine").bounding_box()              # le zoom marche sur la nouvelle photo
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, -300)
    page.wait_for_selector("#cmp-mine.zoomed")
    page.click("#cmp-snext")                                     # retour à la première : photo de départ
    page.wait_for_function("s => document.querySelector('#cmp-mine img').src !== s", arg=other)
    assert page.inner_text("#cmp-sn") == "1/2" and page.get_attribute("#cmp-mine img", "src") == src
    page.keyboard.press("ArrowDown")
    page.wait_for_function("document.querySelector('#cmp-sn').textContent === '2/2'")
    page.click("#compare [data-pick] >> nth=0")
    db2 = sqlite3.connect(dev_app[0].db_path)
    for _ in range(50):
        if db2.execute("SELECT status FROM detection WHERE id = ?", (rows[0][0],)).fetchone()[0] == "confirmed":
            break
        page.wait_for_timeout(100)
    assert db2.execute("SELECT status FROM detection WHERE id = ?", (rows[0][0],)).fetchone()[0] == "confirmed"
    assert db2.execute("SELECT status FROM detection WHERE id = ?", (rows[1][0],)).fetchone()[0] == "pending"
    assert not page.errors, page.errors


def test_compare_shows_location_and_sets_it(page, monkeypatch):
    """Comparaison : lieu de la photo affiché ; sans GPS, bouton pour l'indiquer, puis comparaison rouverte à jour."""
    monkeypatch.setattr("taxonero.refphotos.photos", lambda tx, names, synonyms=None: {"taxon": None, "photos": []})
    page.goto(page.url.split("#")[0] + "#photos")
    page.click("#pfilters [data-f=all]")
    page.wait_for_selector("#pfilters [data-f=all].on")
    page.wait_for_selector("#pgrid .tile")
    find = "P.assets.findIndex(a => {} && a.kind !== 'VIDEO' && a.detections?.some(d => d.candidates?.some(c => c.gbif_key)))"
    gps = page.evaluate(find.format("a.lat != null && !a.geo_user"))
    if gps >= 0:
        page.evaluate(f"openViewer(P.assets, {gps})")
        page.wait_for_selector("#viewer.on .cand")
        page.click(".cand >> nth=0")
        page.wait_for_selector("#compare.on")
        assert "📍" in page.inner_text("#compare .cmploc") and not page.locator("#cmp-loc").count()
        page.keyboard.press("Escape")
        page.keyboard.press("Escape")
        page.wait_for_selector("#viewer:not(.on)", state="attached")
    none = page.evaluate(find.format("a.lat == null"))
    if none < 0:
        pytest.skip("aucune photo sans GPS avec des candidats dans la base de dev")
    page.evaluate(f"openViewer(P.assets, {none})")
    page.wait_for_selector("#viewer.on .cand")
    page.click(".cand >> nth=0")
    page.wait_for_selector("#compare.on #cmp-loc")
    page.click("#cmp-loc")
    page.wait_for_selector("#locpick.on #lpmap.leaflet-container")
    page.keyboard.press("Escape")                          # annuler la carte ne ferme pas la comparaison
    page.wait_for_selector("#locpick:not(.on)", state="attached")
    assert page.is_visible("#compare")
    page.click("#cmp-loc")
    page.wait_for_selector("#locpick.on #lpmap.leaflet-container")
    bb = page.locator("#lpmap").bounding_box()
    page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
    page.click("#locpick [data-a=save]")
    page.wait_for_function("cur().geo_user === 1 && document.querySelector('#compare .cmploc')?.textContent.includes(',')")
    assert page.is_visible("#compare") and page.is_visible("#cmp-mine img")


def test_manual_location(page):
    """Photo sans GPS : lieu indiqué sur la carte dans la visionneuse, puis le même lieu en lot sur une sélection."""
    page.goto(page.url.split("#")[0] + "#photos")
    page.click("#pfilters [data-f=all]")
    page.wait_for_selector("#pfilters [data-f=all].on")
    page.wait_for_selector("#pgrid .tile")
    i = page.evaluate("P.assets.findIndex(a => a.lat == null && a.kind !== 'VIDEO')")
    if i < 0:
        pytest.skip("aucune photo sans GPS dans la base de dev")
    page.evaluate(f"openViewer(P.assets, {i})")
    page.wait_for_selector("#b-loc")
    page.click("#b-loc")
    page.wait_for_selector("#locpick.on #lpmap.leaflet-container")
    bb = page.locator("#lpmap").bounding_box()
    page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
    page.click("#locpick [data-a=save]")
    page.wait_for_function("cur().geo_user === 1 && cur().lat != null")
    assert "📍" in page.locator("#vi-body .locline").inner_text()
    page.keyboard.press("Escape")
    page.wait_for_selector("#viewer:not(.on)", state="attached")
    # en lot : deux photos sans GPS, « même lieu que la dernière fois »
    page.click("#pfilters [data-f=all]")
    page.wait_for_selector("#pgrid .tile")
    nogps = page.evaluate("P.assets.map((a, k) => a.lat == null && a.kind !== 'VIDEO' ? k : -1).filter(k => k >= 0).slice(0, 2)")
    for n, k in enumerate(nogps):
        tile = page.locator(f"#pgrid .tile[data-i='{k}']")
        tile.hover()
        tile.locator(".tick").click() if not n else tile.click()
    page.click("#selbar .selmore summary")                 # menu « Plus »
    page.click("#selbar [data-b=loc]")
    page.click("#locpick [data-a=last]")
    page.wait_for_function("document.querySelector('#toast').textContent.includes('📍') || document.querySelector('#toast').textContent.length > 0")
    n = page.evaluate("fetch('/api/photos?filter=all&limit=500').then(r => r.json()).then(l => l.filter(a => a.geo_user === 1).length)")
    assert n >= len(nogps)
    assert not page.errors, page.errors


# -- corrections de l'audit -----------------------------------------------------

def _photo_viewer(page):
    """Visionneuse ouverte sur une photo à relire."""
    page.goto(page.url.split("#")[0] + "#photos")
    page.click("#pfilters [data-f=all]")
    page.wait_for_selector("#pfilters [data-f=all].on")
    page.wait_for_selector("#pgrid .tile")
    i = page.evaluate("P.assets.findIndex(a => a.kind !== 'VIDEO' && a.detections.some(d => d.status === 'pending'))")
    if i < 0:
        pytest.skip("aucune détection à relire dans la base de dev")
    page.evaluate(f"openViewer(P.assets, {i})")
    page.wait_for_selector("#viewer.on #b-ok")
    return i


def test_csp_allows_every_page(browser, base_url):
    """Sans bypass_csp : aucune page ne déclenche de violation de la politique de sécurité."""
    ctx = browser.new_context(locale="fr-FR")
    pg = ctx.new_page()
    pg.add_init_script("window.__csp = []; document.addEventListener('securitypolicyviolation', e => window.__csp.push([e.violatedDirective, e.blockedURI]))")
    for view in ("photos", "species", "stats", "settings", "identify"):
        pg.goto(f"{base_url}#{view}")
        pg.wait_for_timeout(800)
        assert pg.evaluate("window.__csp") == [], view
    ctx.close()


def test_enter_on_focused_button_only_activates_it(page):
    _photo_viewer(page)
    det = page.evaluate("curDet().id")
    before = page.evaluate("V.info")
    page.focus("#v-info")
    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    assert page.evaluate("V.info") != before
    assert page.evaluate(f"cur().detections.find(d => d.id === {det}).status") == "pending"


def test_mouse_click_keeps_enter_for_review(page):
    """Après un clic à la souris sur un bouton de la barre, Entrée valide toujours la détection."""
    _photo_viewer(page)
    page.click("#v-info")
    assert page.evaluate("document.activeElement.id") != "v-info"


def test_tab_stays_in_viewer_and_reaches_controls(page):
    _photo_viewer(page)
    assert page.get_attribute("#viewer", "role") == "dialog"
    seen = set()
    for _ in range(60):
        page.keyboard.press("Tab")
        assert page.evaluate("!!document.activeElement.closest('#viewer')")
        seen.add(page.evaluate("document.activeElement.id || document.activeElement.tagName"))
    assert "v-close" in seen and "v-info" in seen and "b-ok" in seen


def test_undo_survives_closing_the_viewer(page):
    """Un seul historique d'annulation pour la session : U annule encore après la fermeture de la visionneuse."""
    _photo_viewer(page)
    aid, det = page.evaluate("[cur().asset_id, curDet().id]")
    page.click("#b-no")
    page.wait_for_function(f"cur().detections.find(d => d.id === {det}).status === 'rejected' || P.assets.some(a => a.detections.find(d => d.id === {det})?.status === 'rejected')")
    page.keyboard.press("Escape")
    page.wait_for_selector("#viewer:not(.on)", state="attached")
    page.wait_for_selector("#b-undo:not([hidden])")
    page.click("#b-undo")
    page.wait_for_function(f"fetch('/api/assets/{aid}').then(r => r.json()).then(a => a.detections.find(d => d.id === {det}).status === 'pending')")
    assert not page.errors, page.errors


def test_taxon_picker_available_without_candidates(page):
    i = _photo_viewer(page)
    page.evaluate(f"(() => {{ P.assets[{i}].detections.forEach(d => d.candidates = []); drawInfo(); }})()")
    page.click("#b-taxon")
    assert page.is_visible("#taxpick #tp-q")
    page.click("#taxpick .actions .btn")


# -- navigation ------------------------------------------------------------------

def _back_label(page):
    return page.locator("#b-back-l").inner_text() if page.viewport_size["width"] > 850 else None


def test_back_button_follows_the_route(page):
    """Le bouton Retour revient d'où l'on vient (pas à une page fixe) et nomme sa destination."""
    root = page.url.split("#")[0]
    page.goto(root + "#stats")
    page.wait_for_selector('#stats .hbar[data-href^="species/"]')
    assert page.is_visible("#b-back")
    page.click('#stats .hbar[data-href^="species/"]')
    page.wait_for_selector("#sppage .sphero")
    assert _back_label(page) in (None, "Statistiques")
    page.click("#b-back")
    page.wait_for_function("location.hash === '#stats'")
    page.goto(root + "#family/Canidae")
    page.wait_for_selector("#fapage .sptile")
    page.click("#fapage .sptile")
    page.wait_for_selector("#sppage .sphero")
    page.click("#b-back")
    page.wait_for_function("location.hash === '#family/Canidae'")
    assert not page.errors, page.errors


def test_back_without_history_goes_to_parent(browser, base_url):
    ctx = browser.new_context(locale="fr-FR", bypass_csp=True)
    pg = ctx.new_page()
    pg.goto(base_url + "#family/Canidae")
    pg.wait_for_selector("#fapage .sptile")
    pg.click("#b-back")
    pg.wait_for_function("location.hash.startsWith('#order/')")
    ctx.close()


def test_viewer_is_in_the_address(page):
    root = page.url.split("#")[0]
    page.goto(root + "#stats")
    page.wait_for_selector("#stats .kpi")
    page.goto(root + "#photos?filter=all")
    page.wait_for_selector("#pgrid .tile")
    page.click("#pgrid .tile >> nth=3")
    page.wait_for_selector("#viewer.on")
    page.wait_for_function("location.hash.includes('photo=')")
    photo = page.evaluate("cur().asset_id")
    page.keyboard.press("ArrowRight")
    page.wait_for_function(f"!location.hash.includes('{photo}')")
    after = page.evaluate("cur().asset_id")
    page.reload()                                        # F5 : même photo rouverte
    page.wait_for_selector("#viewer.on")
    assert page.evaluate("cur().asset_id") == after
    page.go_back()                                       # le précédent ferme la visionneuse, sans quitter la page
    page.wait_for_selector("#viewer:not(.on)", state="attached")
    assert "#photos" in page.url
    assert not page.errors, page.errors


def test_filter_and_scroll_restored_on_return(browser, base_url):
    ctx = browser.new_context(viewport={"width": 1440, "height": 500}, locale="fr-FR", bypass_csp=True)
    pg = ctx.new_page()
    pg.goto(base_url + "#photos?filter=all")
    pg.wait_for_selector("#pgrid .tile")
    pg.click("#pfilters [data-f=auto]")
    pg.wait_for_function("location.hash.includes('filter=auto')")
    pg.wait_for_selector("#pgrid .tile")
    pg.evaluate("window.scrollTo(0, 400)")
    pg.wait_for_timeout(200)
    y = pg.evaluate("scrollY")
    pg.click(".nav a[data-v=species]")
    pg.wait_for_selector(".sptile")
    pg.click("#b-back")
    pg.wait_for_function(f"location.hash.includes('filter=auto') && P.filter === 'auto' && Math.abs(scrollY - {y}) < 5")
    ctx.close()


def test_species_page_has_common_grid_and_refreshes(page):
    """Fiche d'une espèce : même grille (sélection) ; une photo rejetée en sort et les chiffres suivent."""
    page.goto(page.url.split("#")[0] + "#species/5219243")
    page.wait_for_selector("#sppage #pgrid .tile")
    n = page.locator("#sppage #pgrid .tile").count()
    total = page.evaluate("P.counts.all")
    page.locator("#sppage #pgrid .tile").nth(0).click(modifiers=["Control"])
    assert page.evaluate("selCount()") >= 1 and page.is_visible("#selbar")
    page.click("#selbar [data-b=none]")
    page.locator("#sppage #pgrid .tile").nth(0).click()
    page.wait_for_selector("#viewer.on #b-no")
    page.click("#b-no")
    page.wait_for_timeout(500)
    page.click("#v-close")
    page.wait_for_function(f"P.counts.all === {total - 1}")
    assert page.evaluate("P.scope.taxon") == 5219243 and n >= 1
    assert not page.errors, page.errors


@pytest.mark.parametrize("hash_", ["family/Nexistepas", "species/999999999", "species/abc"])
def test_bad_addresses_show_a_message(page, hash_):
    page.goto(page.url.split("#")[0] + "#" + hash_)
    page.wait_for_function("!/Chargement/.test(document.querySelector('.view.on').innerText)")
    text = page.inner_text(".view.on")
    assert "Aucun" in text or "Aucune" in text, text
    assert not page.is_disabled("#b-back")


def test_bad_iucn_filter_is_ignored(page):
    page.goto(page.url.split("#")[0] + "#species?iucn=bogus")
    page.wait_for_selector(".sptile")
    assert not page.errors, page.errors


def test_backspace_does_not_delete(page):
    _photo_viewer(page)
    before = page.evaluate("cur().detections.length")
    page.keyboard.press("Backspace")
    page.wait_for_timeout(300)
    assert page.evaluate("cur().detections.length") == before


def test_end_of_queue_says_so(page):
    page.goto(page.url.split("#")[0] + "#photos?filter=pending")
    page.wait_for_selector("#pgrid .tile")
    page.click("#pgrid .tile >> nth=-1")
    page.wait_for_selector("#viewer.on #b-ok")
    page.evaluate("V.i = V.list.length - 1; P.more = false; V.sel = firstPending(cur()); cur().detections.forEach(d => d.status = 'confirmed'); queueEnd()")
    assert page.is_visible("#vdone")


def test_stats_kpi_matches_the_list_it_opens(page):
    page.goto(page.url.split("#")[0] + "#stats")
    page.wait_for_selector(".kpis .kpi")
    kpi = int(page.inner_text(".kpis .kpi:nth-child(2) b"))
    page.click(".kpis .kpi:nth-child(2)")
    page.wait_for_function("P.filter === 'identified' && P.counts.identified !== undefined")
    assert page.evaluate("P.counts.identified") == kpi


def test_map_tiles_send_a_referer(page):
    """Sans en-tête Referer, OpenStreetMap répond par une tuile « Access blocked »."""
    seen = []
    page.route("**/*.tile.openstreetmap.org/**", lambda r: (seen.append(r.request.headers.get("referer")), r.abort()))
    page.goto(page.url.split("#")[0] + "#species/5219243")
    page.wait_for_selector("#spmap.leaflet-container")
    page.wait_for_timeout(800)
    assert seen and all(seen), seen


def test_failed_cycle_is_not_announced_as_success(page):
    page.route("**/api/sync/status", lambda r: r.fulfill(status=200, content_type="application/json",
        body='{"running": false, "error": "Immich injoignable", "last_run": {"at": 1, "scanned": 7, "analyzed": 3}}'))
    page.evaluate("pollSync(true)")
    page.wait_for_selector("#toast.on")
    text = page.inner_text("#toast")
    assert "Immich injoignable" in text and "7" not in text


@pytest.mark.parametrize("route,view", [("**/api/photos?*", "photos"), ("**/api/species", "species"),
                                        ("**/api/observations", "stats"), ("**/api/settings", "settings")])
def test_server_error_shows_message_and_retry(page, route, view):
    page.goto(page.url.split("#")[0] + "#identify")
    page.route(route, lambda r: r.fulfill(status=500, body="boom"))
    page.goto(page.url.split("#")[0] + f"#{view}")
    page.wait_for_selector(f"#v-{view} > .loaderr button")
    assert "boom" in page.inner_text(f"#v-{view} > .loaderr")
    page.unroute(route)
    page.click(f"#v-{view} > .loaderr button")
    page.wait_for_function(f"!document.querySelector('#v-{view} > .loaderr')")
