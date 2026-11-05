import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from taxonero.immich.contract import check_spec
from taxonero.pipeline import Box, Candidate, _nms, decide, rerank
from taxonero.sync import BLOCK_END, BLOCK_START, merge_description, tag_value
from taxonero.autoconfirm import wilson_lower
from taxonero.worker import in_window

PARUS = {"scientific_name": "Parus major", "rank": "species",
         "lineage": {"class": "Aves", "order": "Passeriformes", "family": "Paridae", "genus": "Parus"},
         "vernacular": {"fr": "Mésange charbonnière"}}


def test_merge_description_keeps_user_text():
    block = f"{BLOCK_START}\nx\n{BLOCK_END}"
    assert merge_description("", block) == block
    assert merge_description("Mon texte", block) == f"Mon texte\n\n{block}"
    once = merge_description("Mon texte", block)
    assert merge_description(once, block) == once                     # idempotent
    assert merge_description(once, None) == "Mon texte"                # retrait exact
    assert merge_description(f"avant\n\n{block}\n\naprès", None) == "avant\n\naprès"


def test_tag_value_species_and_genus():
    assert tag_value(PARUS, "Nature") == "Nature/Aves/Passeriformes/Paridae/Parus/Mésange charbonnière (Parus major)"
    genus = {"scientific_name": "Parus", "rank": "genus", "lineage": PARUS["lineage"], "vernacular": {}}
    assert tag_value(genus, "Nature") == "Nature/Aves/Passeriformes/Paridae/Parus (genus)"


def _c(name, score, plaus="green", genus=None, key=None):
    g = genus or name.split()[0]
    return Candidate(name=name, score=score, lineage={"genus": g, "family": "F", "kingdom": "Animalia"},
                     gbif_key=key or hash(name) % 10**6, accepted_name=name, plausibility=plaus)


def test_rerank_geo_demotes_implausible_species():
    cands = [_c("Canis latrans", 0.6, "amber"), _c("Canis lupus", 0.3, "green")]
    cands[0].geo_weight, cands[1].geo_weight = 0.02 * (0 + 0.05 * 1 + 0.5) ** 0.1, (1613 + 0.05 * 61776 + 0.5) ** 0.1
    ranked = rerank(cands, None, None, None)  # tx=None : poids déjà fixés, pas de réseau
    assert ranked[0].name == "Canis lupus" and ranked[0].posterior > 0.9


def test_decide_falls_back_to_genus():
    cands = [_c("Parus major", 0.4), _c("Parus minor", 0.35), _c("Cyanistes caeruleus", 0.25, genus="Cyanistes")]
    for c in cands:
        c.posterior = c.score
    d = decide(cands, None)
    assert (d.rank, d.name) == ("genus", "Parus")
    cands[0].posterior = 0.8
    assert decide(cands, None).rank == "species"


def test_nms():
    a, b, c = Box(0, 0, .5, .5, .9, "animal"), Box(.02, .02, .5, .5, .8, "animal"), Box(.6, .6, .3, .3, .7, "animal")
    assert _nms([a, b, c]) == [a, c]
    whole, part = Box(0, 0, 1, 1, .6, "animal"), Box(.2, .2, .4, .4, .5, "animal")   # contenu dans l'autre
    assert _nms([whole, part]) == [whole]
    swan1, swan2 = Box(.1, .1, .2, .2, .9, "animal"), Box(.5, .5, .2, .2, .4, "animal")  # deux individus
    assert _nms([swan1, swan2]) == [swan1, swan2]


def test_in_window():
    assert in_window("", datetime(2026, 1, 1, 12))
    assert in_window("01:00-07:00", datetime(2026, 1, 1, 3))
    assert not in_window("01:00-07:00", datetime(2026, 1, 1, 12))
    assert in_window("22:00-06:00", datetime(2026, 1, 1, 23)) and in_window("22:00-06:00", datetime(2026, 1, 1, 2))


def test_wilson_is_conservative():
    assert wilson_lower(20, 20) < 1.0
    assert wilson_lower(98, 100) < 0.98
    assert wilson_lower(0, 0) == 0.0


def test_contract_detects_missing_and_deprecated():
    spec = {"paths": {}, "components": {"schemas": {}}}
    errors, _ = check_spec(spec)
    assert any("/server/version" in e for e in errors)


# -- détections superposées, fusion des détecteurs, auto-validation, nettoyage HTML ---------

from taxonero.pipeline import Analysis, Decision, dedupe, merge_boxes
from taxonero.wiki import sanitize
from taxonero.autoconfirm import should_auto_confirm


def _an(box, key, conf, genus="G", runner_up=0.0, plaus="green", rank="species"):
    cands = [_c(f"{genus} sp{key}", conf, plaus, genus=genus, key=key), _c("Other x", runner_up, key=999)]
    cands[0].posterior, cands[1].posterior = conf, runner_up
    return Analysis(box, Decision(key, f"{genus} sp{key}", rank, conf, conf - runner_up, plaus, cands))


def test_dedupe_same_subject_overlapping():
    big, inner = Box(.1, .1, .6, .6, .9, "animal"), Box(.2, .2, .3, .3, .5, "animal")
    assert len(dedupe([_an(big, 1, .9), _an(inner, 1, .8)])) == 1             # même espèce, imbriquées
    assert len(dedupe([_an(big, 1, .9), _an(inner, 2, .8, genus="H")])) == 2  # papillon sur une fleur
    far = Box(.8, .8, .15, .15, .9, "animal")
    assert len(dedupe([_an(big, 1, .9), _an(far, 1, .9)])) == 2              # deux individus distincts


def test_merge_boxes_prefers_megadetector_and_full_frame():
    md = [Box(.1, .1, .3, .3, .9, "animal", "Animalia", "md")]
    ov = [Box(.12, .12, .28, .28, .6, "animal", "Animalia", "owl"),   # doublon de MD
          Box(.6, .6, .3, .3, .5, "plant", "Plantae", "owl"),
          Box(0, 0, .95, .95, .4, "plant", "Plantae", "owl")]        # couvre tout : scène
    out = merge_boxes(md, ov)
    assert [b.source for b in out] == ["md", "owl"]                  # le plein cadre recouvre MD : écarté
    assert merge_boxes([], [Box(0, 0, .95, .95, .4, "plant", "Plantae", "owl")])[0].area == 1.0


def test_auto_confirm_rule():
    b = Box(.1, .1, .3, .3, .9, "animal", "Animalia", "md")
    assert should_auto_confirm(_an(b, 1, .9), .85, .05)
    assert not should_auto_confirm(_an(b, 1, .9, runner_up=.08), .85, .05)       # autre candidat crédible
    assert not should_auto_confirm(_an(b, 1, .9, plaus="amber"), .85, .05)       # pas observé à proximité
    assert not should_auto_confirm(_an(b, 1, .8), .85, .05)                      # sous le seuil
    assert not should_auto_confirm(_an(Box(0, 0, 1, 1, 0, "image", None, "image"), 1, .99), .85, .05)
    assert not should_auto_confirm(_an(b, 1, .9, rank="genus"), .85, .05)
    assert not should_auto_confirm(_an(b, 1, .99), None, .05)                    # mode off


def test_sanitize():
    html = ('<p onclick="x()">a<script>alert(1)</script><img src=x onerror=y><a href="javascript:z">l</a></p>'
            '<h2>Références</h2><p>refs</p><h3>Sous</h3><p>refs2</p><h2>Habitat</h2><p>ok</p>')
    assert sanitize(html) == "<p>al</p><h2>Habitat</h2><p>ok</p>"


def test_merge_drops_unsure_md_box_on_flower():
    md = [Box(.2, .2, .4, .4, .55, "animal", None, "md")]
    flower = [Box(.18, .18, .45, .45, .6, "plant", "Plantae", "owl")]
    assert [b.label for b in merge_boxes(md, flower)] == ["plant"]
    md[0].score = .95  # animal sûr (ex. papillon sur la fleur) : gardé
    assert [b.label for b in merge_boxes(md, flower)][0] == "animal"


def test_dedupe_merges_fragments_but_not_neighbours():
    head = Box(.69, .39, .12, .21, .88, "animal", None, "md")
    tail = Box(.70, .62, .11, .26, .43, "animal", "Animalia", "owl")       # même serpent, peu sûr
    out = dedupe([_an(head, 1, .99, genus="Natrix"), _an(tail, 2, .64, genus="Natrix")])
    assert len(out) == 1 and out[0].box.y + out[0].box.h >= .87              # boîte englobante
    toad1, toad2 = Box(.65, .28, .33, .25, .91, "animal"), Box(.05, .28, .32, .27, .95, "animal")
    assert len(dedupe([_an(toad1, 1, .85, genus="Bufo"), _an(toad2, 1, .85, genus="Bufo")])) == 2


def test_image_quality_prefers_sharp_colour_over_grey():
    import numpy as np
    from PIL import Image
    from taxonero.pipeline import image_quality
    rng = np.random.default_rng(0)
    colour = Image.fromarray((rng.random((128, 128, 3)) * 255).astype("uint8"))
    grey = colour.convert("L").convert("RGB")                    # même netteté, sans couleur (infrarouge)
    flat = Image.new("RGB", (128, 128), (120, 120, 120))          # ni couleur ni détail
    assert image_quality(colour) > image_quality(grey) > image_quality(flat)


def test_range_legacy_entries_are_dropped():
    import sqlite3
    from taxonero import ranges
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE http_cache (url TEXT PRIMARY KEY, body TEXT, fetched REAL)")
    db.execute("INSERT INTO http_cache VALUES ('range:2:123', '{}', 1e12)")
    db.execute("INSERT INTO http_cache VALUES ('https://api.gbif.org/x', '{}', 1e12)")
    assert ranges.migrate_legacy(db) == 1                                   # retéléchargée au nouveau format
    assert db.execute("SELECT url FROM http_cache").fetchall() == [("https://api.gbif.org/x",)]


def test_range_cells_snap_to_exact_gbif_grid():
    """Centres mesurés (sommets arrondis par GBIF : ±0,45 unité) -> colonne et demi-ligne exactes, et origine
    verticale de la tuile, même quand elle tombe près d'une demi-ligne."""
    import random
    from taxonero import ranges
    sx, hy = ranges.grid(400)
    rng = random.Random(1)
    for y0 in (0.0, 3.1, hy - 0.2, 2 * hy - 0.1):
        truth = [(i, h) for i in range(0, 300, 7) for h in range(i % 2, 600, 2 * 3)]
        noisy = [(i * sx + rng.uniform(-.45, .45), y0 + h * hy + rng.uniform(-.45, .45)) for i, h in truth]
        got_y0, idx = ranges.snap(noisy, 400)
        shift = round((got_y0 - y0) / hy)                                    # origine équivalente à un pas près
        assert abs(got_y0 - shift * hy - y0) < 0.1
        assert idx == [(i, h - shift) for i, h in truth]


def test_domestic_detection():
    from taxonero.pipeline import is_domestic
    b = Box(.2, .2, .5, .5, .9, "animal", None, "md")
    dog_hint = Box(.22, .2, .5, .52, .6, "domestic", "Canidae", "owl")
    wolf = _an(b, 1, .9, genus="Canis")
    wolf.decision.candidates[0].lineage["family"] = "Canidae"
    assert is_domestic(wolf, [dog_hint])                    # Canis + « a dog » sur le sujet : chien
    assert not is_domestic(wolf, [])                        # Canis sans indice : loup sauvage
    far = Box(.8, .8, .15, .15, .6, "domestic", "Canidae", "owl")
    assert not is_domestic(wolf, [far])                     # indice ailleurs dans l'image
    cat = _an(b, 2, .9, genus="Felis"); cat.decision.name = "Felis catus"
    assert is_domestic(cat, [])                             # espèce uniquement domestique


def test_no_auto_confirm_for_species_confused_with_domestic():
    b = Box(.1, .1, .3, .3, .9, "animal", None, "md")
    a = _an(b, 1, .97); a.decision.name = "Canis lupus"
    assert not should_auto_confirm(a, .85, .05)


def test_settings_overrides_are_validated(monkeypatch):
    import pytest
    from taxonero.config import Settings, apply_overrides, validate_overrides
    s = Settings(immich_url="http://x", immich_api_key="k")
    assert validate_overrides(s, {"auto_confirm_threshold": "0.9", "hide_absent": True}) == {"auto_confirm_threshold": 0.9, "hide_absent": True}
    for bad in ({"auto_confirm_threshold": 0.2}, {"auto_confirm": "always"}, {"dry_run": False},
                {"analysis_window": "1h-7h"}, {"exclude_extinct": "oui"}):
        with pytest.raises(ValueError):
            validate_overrides(s, bad)
    # Une valeur enregistrée devenue invalide est ignorée, les autres s'appliquent.
    assert apply_overrides(s, '{"auto_confirm": "always", "interval_minutes": 30}') == {"interval_minutes": 30}
    assert s.interval_minutes == 30 and s.auto_confirm == "rule"


def test_prune_pending_drops_extinct_and_redecides(tmp_path):
    import json
    from taxonero import store
    from taxonero.config import Settings
    from taxonero.curation import prune_pending

    class FakeTx:
        def fossil_only(self, key):
            return key == 2
        def match(self, *a, **k):
            return None

    db = store.connect(tmp_path / "t.sqlite")
    db.execute("INSERT INTO asset (asset_id) VALUES ('a')")
    lin = {"kingdom": "Animalia", "genus": "Equus"}
    cands = [{"name": "Equus leidyi", "gbif_key": 2, "p": 0.5, "model_score": 0.5, "plausibility": "unknown", "lineage": lin},
             {"name": "Equus caballus", "gbif_key": 1, "p": 0.4, "model_score": 0.4, "plausibility": "unknown", "lineage": lin},
             {"name": "Equus asinus", "gbif_key": 3, "p": 0.1, "model_score": 0.1, "plausibility": "unknown", "lineage": lin}]
    for status in ("pending", "confirmed"):
        db.execute("INSERT INTO detection (asset_id, x, y, w, h, source, status, taxon_key, taxon_rank, score, candidates, created_at) "
                   "VALUES ('a', 0, 0, 1, 1, 'auto', ?, 2, 'species', 0.5, ?, 0)", (status, json.dumps(cands)))
    s = Settings(immich_url="http://x", immich_api_key="k")
    assert prune_pending(db, FakeTx(), s) == 1
    pend, conf = db.execute("SELECT taxon_key, score, candidates FROM detection ORDER BY status DESC").fetchall()
    assert pend["taxon_key"] == 1 and abs(pend["score"] - 0.8) < 1e-6           # 0.4 / (0.4 + 0.1)
    assert [c["name"] for c in json.loads(pend["candidates"])] == ["Equus caballus", "Equus asinus"]
    assert conf["taxon_key"] == 2                                                 # relue : intacte
    assert prune_pending(db, FakeTx(), s) == 0                                    # idempotent


def test_prune_is_reversible(tmp_path):
    """Un candidat écarté par un réglage revient quand on désactive le réglage."""
    import json
    from taxonero import store
    from taxonero.config import Settings
    from taxonero.curation import prune_pending

    class FakeTx:
        def fossil_only(self, key):
            return False
        def match(self, *a, **k):
            return None

    db = store.connect(tmp_path / "t.sqlite")
    db.execute("INSERT INTO asset (asset_id) VALUES ('a')")
    lin = {"kingdom": "Animalia", "genus": "Lacerta"}
    cands = [{"name": "Lacerta schreiberi", "gbif_key": 7, "p": 0.6, "model_score": 0.6, "plausibility": "red", "lineage": lin},
             {"name": "Lacerta bilineata", "gbif_key": 8, "p": 0.4, "model_score": 0.4, "plausibility": "green", "lineage": lin}]
    db.execute("INSERT INTO detection (asset_id, x, y, w, h, source, status, taxon_key, taxon_rank, score, candidates, created_at) "
               "VALUES ('a', 0, 0, 1, 1, 'auto', 'pending', 7, 'species', 0.6, ?, 0)", (json.dumps(cands),))
    s = Settings(immich_url="http://x", immich_api_key="k", hide_absent=True)
    assert prune_pending(db, FakeTx(), s) == 1
    r = db.execute("SELECT taxon_key, candidates, candidates_out FROM detection").fetchone()
    assert r["taxon_key"] == 8 and [c["gbif_key"] for c in json.loads(r["candidates_out"])] == [7]
    s.hide_absent = False
    assert prune_pending(db, FakeTx(), s) == 1
    r = db.execute("SELECT taxon_key, score, candidates, candidates_out FROM detection").fetchone()
    assert r["taxon_key"] == 7 and abs(r["score"] - 0.6) < 1e-6 and json.loads(r["candidates_out"]) == []


def _down_gbif(status=500):
    """Client HTTP de Taxonomy qui répond toujours status, et compte les appels."""
    import httpx

    class Http:
        calls = 0
        def get(self, url, params=None):
            Http.calls += 1
            return httpx.Response(status, request=httpx.Request("GET", str(url)))
    return Http()


def test_fail_fast_stops_at_first_error_and_remembers_outage(monkeypatch):
    import httpx
    from taxonero import store
    from taxonero.taxonomy import Taxonomy
    monkeypatch.setattr("time.sleep", lambda s: None)
    tx = Taxonomy(store.connect(":memory:"), fail_fast=True)
    tx.min_interval, tx.http = 0, _down_gbif()
    for _ in range(3):
        with pytest.raises(httpx.HTTPError):
            tx.occurrences_near(1, 45.0, 5.0, 5.0)
    assert tx.http.calls == 1                              # une seule requête : la panne est retenue
    tx.http = _down_gbif(404)
    with pytest.raises(httpx.HTTPError):
        tx.get("https://api.gbif.org/v1/species/1")        # les autres services restent interrogés
    assert tx.http.calls == 1
    slow = Taxonomy(store.connect(":memory:"))             # analyse automatique : relances inchangées
    slow.min_interval, slow.http = 0, _down_gbif()
    with pytest.raises(httpx.HTTPError):
        slow.occurrences_near(1, 45.0, 5.0, 5.0)
    assert slow.http.calls == 6


def test_rerank_tolerant_keeps_candidates_when_gbif_is_down():
    import httpx
    boom = httpx.HTTPError("panne")

    class Tx:
        def prefetch_candidates(self, *a, **k): pass
        def match(self, name, kingdom=None, enrich=True, rank=None):
            return type("T", (), {"gbif_key": 1, "scientific_name": name, "rank": "species", "lineage": {}})()
        def geo(self, *a): raise boom

    def drop(c): raise boom
    cands = [Candidate("A a", .9, {"genus": "A"})]
    with pytest.raises(httpx.HTTPError):
        rerank(cands, Tx(), 45.0, 5.0)                     # analyse automatique : l'erreur remonte, photo réessayée
    out = rerank([Candidate("A a", .9, {"genus": "A"})], Tx(), 45.0, 5.0, drop=drop, tolerant=True)
    assert out[0].plausibility == "unverified" and out[0].posterior == 1.0
    assert decide(out, Tx(), tolerant=True).plausibility == "unverified"


def test_rerank_keeps_excluded_candidates_aside():
    excluded = []
    cands = [Candidate("A a", 0.6, {"genus": "A"}), Candidate("B b", 0.4, {"genus": "B"})]
    out = rerank(cands, None, None, None, drop=lambda c: c.name == "A a", excluded=excluded)
    assert [c.name for c in out] == ["B b"] and out[0].posterior == 1.0
    assert [c.name for c in excluded] == ["A a"] and abs(excluded[0].prior - 0.6) < 1e-9


def test_analysis_window_rejects_impossible_hours():
    import pytest
    from taxonero.config import Settings, validate_overrides
    s = Settings(immich_url="http://x", immich_api_key="k")
    for bad in ("25:00-07:00", "01:60-07:00"):
        with pytest.raises(ValueError):
            validate_overrides(s, {"analysis_window": bad})
        with pytest.raises(ValueError):
            Settings(immich_url="http://x", immich_api_key="k", analysis_window=bad)
    assert validate_overrides(s, {"analysis_window": "23:00-06:30"})


class FakeImmich:
    """Immich en mémoire, pour Syncer : photos, tags, et une photo supprimée."""
    dry_run = False

    def __init__(self, ids, gone=()):
        import itertools
        self.assets = {a: {"id": a, "tags": [], "exifInfo": {"description": ""}, "updatedAt": "2000-01-01T00:00:00Z"} for a in ids}
        self.gone, self.tags, self.gets = set(gone), {}, 0
        self._ids = (f"t{i}" for i in itertools.count())

    def get_asset(self, aid, missing_ok=False):
        self.gets += 1
        if aid in self.gone:
            if missing_ok:
                return None
            raise RuntimeError("400")
        return self.assets[aid]

    def list_tags(self):
        return [{"value": v, "id": i} for v, i in self.tags.items()]

    def upsert_tags(self, values):
        for v in values:
            for k in range(v.count("/") + 1):
                self.tags.setdefault(v.rsplit("/", k)[0], next(self._ids))

    def _value(self, tid):
        return next(v for v, i in self.tags.items() if i == tid)

    def bulk_tag(self, tag_ids, asset_ids):
        for a in asset_ids:
            self.assets[a]["tags"] += [{"value": self._value(t), "id": t} for t in tag_ids]

    def untag_assets(self, tid, asset_ids):
        for a in asset_ids:
            self.assets[a]["tags"] = [t for t in self.assets[a]["tags"] if t["id"] != tid]

    def set_description(self, aid, text):
        self.assets[aid]["exifInfo"]["description"] = text

    def count_assets_with_tag(self, tid):
        return sum(any(t["id"] == tid for t in a["tags"]) for a in self.assets.values())

    def delete_tag(self, tid):
        self.tags = {v: i for v, i in self.tags.items() if i != tid}


def _sync_db(tmp_path, ids):
    import json
    from taxonero import store
    db = store.connect(tmp_path / "s.sqlite")
    for key, name in ((1, "Parus major"), (2, "Cyanistes caeruleus")):
        store.save_taxon(db, key, json.dumps({"scientific_name": name, "rank": "species", "vernacular": {},
                                              "lineage": {"class": "Aves", "family": "Paridae", "genus": name.split()[0]}}))
    for aid in ids:
        db.execute("INSERT INTO asset (asset_id) VALUES (?)", (aid,))
        db.execute("INSERT INTO detection (asset_id, source, status, confirmed_by, taxon_key, taxon_rank, score, created_at) "
                   "VALUES (?, 'auto', 'confirmed', 'auto', 1, 'species', 0.9, 0)", (aid,))
    db.commit()
    return db


def test_sync_survives_deleted_photo_and_is_incremental(tmp_path):
    from taxonero.sync import Syncer
    db = _sync_db(tmp_path, ["a", "b", "gone"])
    im = FakeImmich(["a", "b"], gone=["gone"])
    sy = Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0)
    st = sy.sync_all(full=False)
    assert st.assets == 2 and sy.missing == ["gone"]
    assert all(t["value"] == "Nature/Aves/Paridae/Parus/Parus major" for a in ("a", "b") for t in im.assets[a]["tags"])
    # Rien n'a changé : le passage suivant ne relit aucune photo (hors la disparue, que le worker masque).
    db.execute("UPDATE asset SET missing_at = 1 WHERE asset_id = 'gone'")
    im.gets = 0
    assert Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all(full=False).assets == 0 and im.gets == 0
    # Une correction : seule la photo concernée est relue.
    db.execute("UPDATE detection SET taxon_key = 2 WHERE asset_id = 'a'")
    assert Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all(full=False).assets == 1 and im.gets == 2   # lecture + relecture avant la description


def test_sync_deletes_tags_that_became_useless(tmp_path):
    from taxonero.sync import Syncer
    db = _sync_db(tmp_path, ["a", "b"])
    im = FakeImmich(["a", "b"])
    Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all()
    im.upsert_tags(["Perso/Vacances"])   # tag de l'utilisateur : intouchable
    db.execute("UPDATE detection SET taxon_key = 2")   # mauvaise espèce corrigée sur toutes les photos
    st = Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all(full=False)
    assert st.tags_deleted >= 2   # l'espèce et son genre, devenus vides
    assert not any("Parus" in v for v in im.tags) and "Perso/Vacances" in im.tags
    assert any("Cyanistes" in v for v in im.tags) and "Nature/Aves/Paridae" in im.tags


def test_analysis_queue_gives_up_and_hides_deleted_photos(tmp_path):
    import httpx
    from taxonero.config import Settings
    from taxonero.worker import MAX_FAILURES, Worker

    class BrokenAnalyzer:
        def analyze(self, *a, **k):
            raise ValueError("image illisible")

    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off")
    w = Worker(s, analyzer=BrokenAnalyzer())

    def thumbnail(aid, size):
        if aid == "gone":
            req = httpx.Request("GET", "http://x")
            raise httpx.HTTPStatusError("400", request=req, response=httpx.Response(
                400, json={"message": "Not found or no asset.read access"}, request=req))
        return b""
    w.client.thumbnail = thumbnail
    for aid in ("broken", "gone"):
        w.db.execute("INSERT INTO asset (asset_id) VALUES (?)", (aid,))
    w.db.commit()
    for _ in range(MAX_FAILURES + 2):
        w.analyze()
    r = {x["asset_id"]: x for x in w.db.execute("SELECT * FROM asset")}
    assert r["broken"]["fail_count"] == MAX_FAILURES and "illisible" in r["broken"]["last_error"]
    assert r["gone"]["missing_at"] is not None and w.pending_analysis() == 0
    assert w.requeue("failed") == 1 and w.pending_analysis() == 1


def test_device_falls_back_to_cpu_instead_of_crashing():
    import pytest
    from taxonero import hardware
    from taxonero.config import Settings
    hardware.resolve.cache_clear()
    import torch
    if torch.cuda.is_available():
        pytest.skip("GPU présent")
    assert hardware.resolve("auto")[0] == "cpu" and hardware.resolve("cuda")[0] == "cpu"
    assert hardware.onnx_providers("cpu") == ["CPUExecutionProvider"]
    assert Settings(immich_url="http://x", immich_api_key="k").device == "auto"
    with pytest.raises(ValueError):
        Settings(immich_url="http://x", immich_api_key="k", device="gpu")


def test_gpu_that_crashes_on_first_use_falls_back_to_cpu(monkeypatch):
    """Un GPU qui se dit disponible mais plante au premier calcul (Iris Xe avec PyTorch XPU) : processeur, et le
    processus du programme survit (le calcul d'essai tourne à part)."""
    from taxonero import hardware
    assert hardware._works("cpu")                       # le calcul d'essai lui-même est juste
    monkeypatch.setattr(hardware, "PROBE", "import os; os.abort()")   # plantage brutal du processus d'essai
    monkeypatch.setattr(hardware, "_available", lambda d: d == "xpu")
    hardware.resolve.cache_clear()
    try:
        assert hardware.resolve("auto") == ("cpu", "gpu_broken")
        assert hardware.resolve("xpu") == ("cpu", "gpu_broken")
    finally:
        hardware.resolve.cache_clear()


def test_full_precision_on_every_device():
    """Mêmes résultats sur GPU et processeur : pas de TF32, ni côté PyTorch ni côté ONNX Runtime."""
    import torch
    from taxonero import hardware
    torch.backends.cudnn.allow_tf32 = True
    hardware.full_precision()
    assert torch.backends.cudnn.allow_tf32 is False and torch.backends.cuda.matmul.allow_tf32 is False
    assert torch.get_float32_matmul_precision() == "highest"
    assert hardware.ONNX_OPTIONS["CUDAExecutionProvider"] == {"use_tf32": "0"}


def test_device_choice_reloads_models(tmp_path, monkeypatch):
    from taxonero import hardware, worker
    from taxonero.config import Settings, validate_overrides

    class Fake:
        def __init__(self, *a, device="cpu", **k):
            self.device = device
    monkeypatch.setattr(worker, "Classifier", Fake)
    monkeypatch.setattr(worker, "OpenVocabDetector", Fake)
    from taxonero import megadetector   # pas d'ONNX : MegaDetector dans PyTorch, sans téléchargement ici
    monkeypatch.setattr(megadetector, "weights", lambda *a, **k: tmp_path / "w.pt")
    monkeypatch.setattr(megadetector, "TorchDetector", lambda *a, **k: type("D", (), {"providers": ["PyTorch"]})())
    monkeypatch.setattr(hardware, "resolve", lambda wanted: ("cuda", "gpu_found") if wanted == "auto" else ("cpu", "cpu_chosen"))
    monkeypatch.setattr(hardware, "full_precision", lambda: None)
    monkeypatch.setattr(hardware, "release", lambda d: None)
    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, detector_path=tmp_path / "absent.onnx")
    w = worker.Worker(s)
    assert s.device == "auto" and w.analyzer.classifier.device == "cuda"   # GPU par défaut
    first = w.analyzer
    assert w.analyzer is first                                             # pas de rechargement inutile
    s.device = validate_overrides(s, {"device": "cpu"})["device"]
    assert w.analyzer is not first and w.analyzer.classifier.device == "cpu"


def test_ui_catalogs_match_french():
    """Chaque catalogue de langue : mêmes clés que le français, mêmes variables {x}, mêmes balises HTML,
    et les formes plurielles de la langue (CLDR)."""
    import json
    import re
    from pathlib import Path
    root = Path(__file__).parent.parent / "src" / "taxonero" / "static" / "i18n"
    fr = json.loads((root / "fr.json").read_text())
    plural = re.compile(r"_(zero|one|two|few|many|other)$")
    base = lambda cat: {plural.sub("", k) for k in cat}
    forms = {"en": {"one", "other"}, "de": {"one", "other"}, "nl": {"one", "other"}, "sv": {"one", "other"},
             "es": {"one", "many", "other"}, "it": {"one", "many", "other"}, "pt": {"one", "many", "other"},
             "ca": {"one", "many", "other"}, "pl": {"one", "few", "many", "other"}, "ja": {"other"}, "zh": {"other"}}
    fr_plural = {plural.sub("", k) for k in fr if plural.search(k)}
    sig = lambda m: (sorted(set(re.findall(r"\{\w+\}", m))), re.findall(r"</?\w+", m))
    for f in sorted(root.glob("*.json")):
        if f.stem == "fr":
            continue
        cat = json.loads(f.read_text())
        assert base(cat) == base(fr), (f.stem, sorted(base(fr) ^ base(cat))[:10])
        for k in fr_plural:
            have = {plural.search(x).group(1) for x in cat if plural.sub("", x) == k and plural.search(x)}
            assert forms.get(f.stem, {"other"}) <= have, (f.stem, k, have)
        for k, v in cat.items():
            ref = fr.get(k) or fr[plural.sub("", k) + "_other"]
            assert sig(v) == sig(ref), (f.stem, k, v)


def test_tidy_name_fixes_all_caps():
    from taxonero.taxonomy import tidy_name
    assert tidy_name("DOGS") == "Dogs"                     # GBIF, famille Canidae
    assert tidy_name("ÉCUREUIL ROUX") == "Écureuil roux"
    assert tidy_name("Old World flycatchers") == "Old World flycatchers"
    assert tidy_name("UK") == "UK"                         # sigle court : laissé tel quel
    assert tidy_name(" renard roux ") == "Renard roux"


def test_open_mode_auto_depends_on_hardware():
    from taxonero.hardware import open_mode
    assert open_mode("auto", "cpu") == "fallback"
    assert open_mode("auto", "cuda") == "always"
    assert open_mode("always", "cpu") == "always"


def test_open_detector_fallback_runs_only_without_animal():
    from PIL import Image
    from taxonero.pipeline import Analyzer

    class Fixed:
        def __init__(self, boxes): self.boxes, self.calls = boxes, 0
        def detect(self, image): self.calls += 1; return list(self.boxes)

    class NoClassifier:
        def classify(self, images): return [[] for _ in images]

    image = Image.new("RGB", (64, 48))
    for md_boxes, expected in (([Box(.1, .1, .3, .3, .9, "animal", None, "md")], 0), ([], 1)):
        ov = Fixed([])
        Analyzer(Fixed(md_boxes), NoClassifier(), None, open_detector=ov, open_mode="fallback").analyze(image)
        assert ov.calls == expected
    ov = Fixed([])
    Analyzer(Fixed([Box(.1, .1, .3, .3, .9, "animal", None, "md")]), NoClassifier(), None, open_detector=ov).analyze(image)
    assert ov.calls == 1                                   # 'always' par défaut


def test_group_reliability_blocks_weak_groups_until_reviewed():
    import json as _json
    from taxonero import store
    from taxonero.config import Settings
    from taxonero.autoconfirm import GROUP_MIN_REVIEWS, auto_blockers, blocked_groups, group_of
    assert group_of({"kingdom": "Animalia", "phylum": "Chordata", "order": "Perciformes"}) == "pisces"
    assert group_of({"kingdom": "Fungi", "class": "Agaricomycetes"}) == "fungi"
    assert group_of({"class": "Arachnida"}) == "arachnida"
    s = Settings(immich_url="", immich_api_key="")
    db = store.connect(":memory:")
    assert blocked_groups(db, s) == {"arachnida", "pisces"}       # par défaut : mesurés faibles sur le jeu de dev
    spider = {"scientific_name": "Araneus diadematus", "lineage": {"class": "Arachnida", "kingdom": "Animalia"}}
    store.save_taxon(db, 1, _json.dumps(spider))
    db.execute("INSERT INTO asset (asset_id) VALUES ('a')")
    for i in range(GROUP_MIN_REVIEWS):                            # relectures toutes justes : le groupe est débloqué
        db.execute("INSERT INTO detection (asset_id, x, y, w, h, source, status, confirmed_by, taxon_key, taxon_rank, "
                   "model_key, model_rank, model_conf, created_at) VALUES ('a', 0, 0, 1, 1, 'auto', 'confirmed', 'user', "
                   "1, 'species', 1, 'species', 0.95, 0)")
    assert blocked_groups(db, s) == {"pisces"}
    why = auto_blockers("species", "green", 0.99, "x", 0.0, "animal", 0.85, 0.05, "pisces", {"pisces"})
    assert why == [{"code": "group_unreliable", "group": "pisces"}]


def test_batched_analysis_isolates_a_broken_photo(tmp_path, monkeypatch):
    """Sur GPU, les photos passent par lots : une photo illisible fait échouer le lot, qui est alors repris
    photo par photo ; seules les illisibles sont comptées en échec."""
    from taxonero import hardware
    from taxonero.config import Settings
    from taxonero.worker import Worker

    class Batching:
        calls = 0

        def analyze(self, data, *a, **k):
            if data == b"broken":
                raise ValueError("image illisible")
            return []

        def analyze_many(self, items, fallback=None, groups=None):
            Batching.calls += 1
            return [self.analyze(*it) for it in items]

    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off")
    w = Worker(s, analyzer=Batching())
    monkeypatch.setattr(hardware, "resolve", lambda wanted: ("cuda", "gpu_found"))   # chemin GPU : lots de 8
    w._device = "cuda"
    w.client.thumbnail = lambda aid, size: b"broken" if aid == "p3" else b"ok"
    for i in range(10):
        w.db.execute("INSERT INTO asset (asset_id, file_name) VALUES (?, ?)", (f"p{i}", f"p{i}.jpg"))
    w.db.commit()
    assert w.analyze() == 10 and Batching.calls == 2               # 8 + 2 (GPU_BATCH par défaut)
    r = {x["asset_id"]: x for x in w.db.execute("SELECT * FROM asset")}
    assert r["p3"]["fail_count"] == 1 and r["p3"]["analyzed_at"] is None
    assert all(r[f"p{i}"]["analyzed_at"] for i in range(10) if i != 3)
    assert w.progress["done"] == 10
    s.gpu_batch = 4                                                  # réglable dans les Paramètres
    w.db.execute("UPDATE asset SET analyzed_at = NULL, fail_count = 0")
    w.db.commit()
    Batching.calls = 0
    assert w.analyze() == 10 and Batching.calls == 3               # lots de 4 + 4 + 2


def test_higher_ranks_take_gbif_plural_names():
    """Rang supérieur en cache : les noms GBIF (pluriel : « Oiseaux ») remplacent les libellés Wikidata
    (titres d'articles au singulier : « Oiseau ») ; les langues sans nom GBIF gardent le leur."""
    from taxonero.taxonomy import HIGHER, NAMES_VERSION, Taxonomy
    tx = Taxonomy.__new__(Taxonomy)
    tx._vernacular = lambda key: {"fr": "Oiseaux", "en": "Birds"}
    d = tx.plural_names({"gbif_key": 212, "rank": "class", "vernacular": {"fr": "Oiseau", "br": "Evned"}})
    assert d["vernacular"] == {"fr": "Oiseaux", "en": "Birds", "br": "Evned"} and d["nv"] == NAMES_VERSION
    assert HIGHER("class") and HIGHER("genus") and not HIGHER("species")


def test_raw_paired_with_jpg_is_skipped_and_gets_its_tags(tmp_path):
    from taxonero import store
    from taxonero.config import Settings
    from taxonero.sync import Syncer
    from taxonero.worker import Worker
    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off")
    w = Worker(s)
    t = "2024-05-01T10:00:00"
    for aid, name, taken in (("jpg", "IMG_1.JPG", t + ".000Z"), ("raw", "IMG_1.CR3", t + ".500Z"),
                             ("alone", "IMG_2.NEF", t + ".000Z"), ("other", "IMG_1.ARW", "2024-06-01T10:00:00.000Z")):
        w.db.execute("INSERT INTO asset (asset_id, file_name, taken_at, people) VALUES (?, ?, ?, 0)", (aid, name, taken))
    # RAW analysé avant la règle : sa détection automatique en attente est retirée, il sort de la file.
    w.db.execute("INSERT INTO detection (asset_id, source, status, created_at) VALUES ('raw', 'auto', 'pending', 0)")
    w.db.execute("UPDATE asset SET analyzed_at = 1 WHERE asset_id = 'raw'")
    w.db.commit()
    assert w.apply_skips() == 1
    twins = {r["asset_id"]: r["twin_of"] for r in w.db.execute("SELECT asset_id, twin_of FROM asset")}
    assert twins == {"jpg": None, "raw": "jpg", "alone": None, "other": None}   # RAW seul ou d'un autre cliché : analysé
    queued = {r["asset_id"] for r in w.db.execute(f"SELECT asset_id FROM asset WHERE {w.queue_sql()[0]}", w.queue_sql()[1])}
    assert queued == {"jpg", "alone", "other"} and w.pending_analysis() == 3
    s.skip_raw_twins = False                    # option désactivée : le RAW est analysé lui aussi
    assert w.pending_analysis() == 4
    s.skip_raw_twins = True
    # Le JPG validé : le RAW reçoit les mêmes tags dans Immich.
    import json
    store.save_taxon(w.db, 1, json.dumps({"scientific_name": "Parus major", "rank": "species", "vernacular": {},
                                          "lineage": {"class": "Aves", "family": "Paridae", "genus": "Parus"}}))
    w.db.execute("INSERT INTO detection (asset_id, source, status, taxon_key, taxon_rank, score, created_at) "
                 "VALUES ('jpg', 'auto', 'confirmed', 1, 'species', 0.9, 0)")
    w.db.commit()
    im = FakeImmich(["jpg", "raw", "alone", "other"])
    Syncer(w.db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all()
    tags = {a: [t["value"] for t in im.assets[a]["tags"]] for a in im.assets}
    assert tags["raw"] == tags["jpg"] == ["Nature/Aves/Paridae/Parus/Parus major"] and not tags["alone"]


def test_photos_with_people_are_skipped_unless_disabled(tmp_path):
    from taxonero.config import Settings
    from taxonero.worker import Worker

    class Client:
        """Immich : 'family' a une personne (reconnue après l'import), 'bird' aucune."""
        def iter_assets(self, updated_after=None, size=250, with_exif=True, kind="IMAGE", with_people=False):
            for aid, n in (("family", 1), ("bird", 0)):
                yield {"id": aid, "type": "IMAGE", "people": [{"id": "p"}] * n if with_people else []}

    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off", analyze_videos=False)
    w = Worker(s)
    w.client = Client()
    for aid in ("family", "bird"):
        w.db.execute("INSERT INTO asset (asset_id, kind) VALUES (?, 'IMAGE')", (aid,))   # base d'avant : people inconnu
    w.db.commit()
    assert w.reconcile() is not None   # personnes inconnues : relu tout de suite, sans attendre le lendemain
    assert w.reconcile() is None       # puis une fois par jour seulement
    assert dict(w.db.execute("SELECT asset_id, people FROM asset").fetchall()) == {"family": 1, "bird": 0}
    w.apply_skips()
    assert w.pending_analysis() == 1
    s.skip_people = False
    assert w.pending_analysis() == 2


def test_geo_skips_near_query_when_region_is_empty(tmp_path):
    from taxonero import store
    from taxonero.taxonomy import Taxonomy
    tx = Taxonomy(store.connect(tmp_path / "t.sqlite"))
    calls = []
    tx.occurrences_near = lambda key, lat, lon, r: calls.append(r) or (0 if key == 1 else 50)
    assert tx.geo(1, 45.0, 5.0) == ("red", 0.02 * 0.5 ** 0.1) and calls == [5.0]
    calls.clear()
    assert tx.geo(2, 45.0, 5.0)[0] == "green" and calls == [5.0, 1.0]


def test_whole_image_only_when_asked():
    from PIL import Image
    from taxonero.pipeline import Analyzer

    class Nothing:
        def detect(self, image): return []

    class Clf:
        def __init__(self): self.seen = 0
        def classify(self, images): self.seen += len(images); return [[] for _ in images]

    clf = Clf()
    an = Analyzer(Nothing(), clf, None)
    an.analyze(Image.new("RGB", (64, 48)), fallback=False)
    assert clf.seen == 0                      # analyse automatique : rien de détecté, rien de classé
    an.analyze(Image.new("RGB", (64, 48)))
    assert clf.seen == 1                      # page Identifier : l'image entière


def test_pending_whole_image_detections_are_dropped(tmp_path):
    from taxonero.config import Settings
    from taxonero.worker import Worker
    w = Worker(Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off"))
    w.db.execute("INSERT INTO asset (asset_id, people) VALUES ('a', 0)")
    for det, status in (("image", "pending"), ("image", "confirmed"), ("md", "pending")):
        w.db.execute("INSERT INTO detection (asset_id, detector, source, status, created_at) VALUES ('a', ?, 'auto', ?, 0)",
                     (det, status))
    w.db.commit()
    assert w.apply_skips() == 1
    assert sorted(tuple(r) for r in w.db.execute("SELECT detector, status FROM detection")) == [("image", "confirmed"), ("md", "pending")]


def test_gbif_throttling_does_not_count_as_photo_failure(tmp_path):
    import httpx
    from taxonero.config import Settings
    from taxonero.worker import Unavailable, Worker

    class Throttled:
        def analyze(self, *a, **k):
            req = httpx.Request("GET", "https://api.gbif.org/v1/occurrence/search")
            raise httpx.HTTPStatusError("429", request=req, response=httpx.Response(429, request=req))

    w = Worker(Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off"), analyzer=Throttled())
    w.client.thumbnail = lambda aid, size: b""
    w.db.execute("INSERT INTO asset (asset_id, people) VALUES ('a', 0)")
    w.db.commit()
    import pytest
    with pytest.raises(Unavailable):
        w.analyze()
    assert w.db.execute("SELECT fail_count FROM asset").fetchone()[0] == 0 and w.pending_analysis() == 1


def test_rerank_prefetch_covers_sequential_path():
    """rerank prépare en parallèle exactement ce que le chemin normal demande ensuite (aucune requête de plus),
    et ignore les candidats sous MIN_CANDIDATE_SCORE (au moins MIN_CANDIDATES gardés)."""
    import json as _json
    import threading
    import httpx
    from taxonero import store
    from taxonero.pipeline import Candidate, rerank
    from taxonero.taxonomy import Taxonomy

    keys = {"Vulpes vulpes": 1, "Martes foina": 2, "Meles meles": 3, "Lutra lutra": 4, "Felis catus": 5}
    seen, lock = [], threading.Lock()

    def handler(req):
        with lock:
            seen.append(str(req.url))
        p = req.url.params
        if req.url.path.endswith("/match"):
            k = keys.get(p["name"])
            body = {"matchType": "EXACT", "usageKey": k} if k else {"matchType": "NONE"}
        elif "/species/" in req.url.path:
            k = int(req.url.path.rsplit("/", 1)[1])
            name = next(n for n, v in keys.items() if v == k)
            body = {"canonicalName": name, "rank": "SPECIES", "kingdom": "Animalia", "species": name}
        else:
            body = {"count": 0 if "FOSSIL" in str(req.url) else 50}
        return httpx.Response(200, content=_json.dumps(body).encode())

    tx = Taxonomy(store.connect(":memory:"))
    tx.http, tx.min_interval = httpx.Client(transport=httpx.MockTransport(handler)), 0.0
    drop = lambda c: False
    drop.fossils = True
    scores = [0.6, 0.2, 0.15, 0.03, 0.005]
    cands = [Candidate(n, s, {"kingdom": "Animalia"}) for n, s in zip(keys, scores)]
    after = []
    tx.prefetch_candidates = (lambda f: lambda *a, **k: (f(*a, **k), after.append(len(seen))))(tx.prefetch_candidates)
    out = rerank(cands, tx, 45.2, 5.7, drop=drop)
    assert {c.name for c in out} == set(list(keys)[:4])          # Felis catus (0,5 %) ignoré
    assert len(seen) == after[0], seen[after[0]:]                # rien de plus après le prefetch
    assert len(seen) == len(set(seen))                           # aucune requête en double


def test_local_backbone(tmp_path):
    """Taxonomie GBIF locale construite depuis une mini-archive au format de GBIF : mêmes réponses que l'API
    (synonyme -> nom accepté, règne, fiche), recherche par nom commun, et Taxonomy ne demande plus au réseau que
    ce qui reste à l'API (noms communs, UICN)."""
    import zipfile
    import httpx
    from taxonero import store
    from taxonero.backbone import Backbone, build
    from taxonero.taxonomy import Taxonomy

    head = "\t".join(["taxonID", "datasetID", "parentNameUsageID", "acceptedNameUsageID", "originalNameUsageID", "scientificName",
                      "scientificNameAuthorship", "canonicalName", "genericName", "specificEpithet", "infraspecificEpithet",
                      "taxonRank", "nameAccordingTo", "namePublishedIn", "taxonomicStatus", "nomenclaturalStatus", "taxonRemarks",
                      "kingdom", "phylum", "class", "order", "family", "genus"])
    def taxon(key, name, rank, status, accepted="", auth="", kingdom="Animalia", genus="Vulpes", family="Canidae"):
        g, _, e = name.partition(" ")
        return "\t".join([str(key), "", "", str(accepted), "", f"{name} {auth}".strip(), auth, name, g, e, "", rank, "", "",
                          status, "", "", kingdom, "Chordata", "Mammalia", "Carnivora", family, genus])
    rows = [head,
            taxon(5219243, "Vulpes vulpes", "species", "accepted", auth="(Linnaeus, 1758)"),
            taxon(9999001, "Canis vulpes", "species", "synonym", accepted=5219243),
            taxon(5219234, "Vulpes", "genus", "accepted"),
            taxon(7000001, "Morus", "genus", "accepted", kingdom="Plantae", genus="Morus", family="Moraceae"),
            taxon(7000002, "Morus", "genus", "accepted", kingdom="Animalia", genus="Morus", family="Sulidae")]
    vern = ["taxonID\tvernacularName\tlanguage\tcountry\tcountryCode\tsex\tlifeStage\tsource",
            "5219243\tRenard roux\tfr\t\t\t\t\tx", "5219243\tRed Fox\ten\t\t\t\t\tx", "5219243\t \tfr\t\t\t\t\tx"]
    archive = tmp_path / "backbone.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name, lines in (("Taxon.tsv", rows), ("VernacularName.tsv", vern)):
            z.writestr(name, "\n".join(lines) + "\n")
    path = build(archive, tmp_path / "gbif-backbone.sqlite", "2023-08-28")
    bb = Backbone(path)

    assert bb.match("vulpes VULPES")["usageKey"] == 5219243                       # casse ignorée
    m = bb.match("Canis vulpes", kingdom="Animalia")
    assert m["usageKey"] == 9999001 and m["acceptedUsageKey"] == 5219243 and m["status"] == "SYNONYM"
    assert bb.match("Morus") is None                                              # homonymes : l'API tranche
    assert bb.match("Morus", kingdom="Plantae")["usageKey"] == 7000001
    assert bb.match("Vulpes", rank="species") is None and bb.match("Inconnu inconnu") is None
    sp = bb.species(5219243)
    assert sp["canonicalName"] == "Vulpes vulpes" and sp["authorship"] == "(Linnaeus, 1758)" and sp["species"] == "Vulpes vulpes"
    assert sp["family"] == "Canidae" and sp["rank"] == "SPECIES"
    assert [h["gbif_key"] for h in bb.search("renard")] == [5219243] and bb.search("renard")[0]["common"] == "Renard roux"
    assert bb.search("Vulp")[0]["gbif_key"] == 5219243                           # espèce avant genre

    asked = []
    def api(req):
        asked.append(req.url.path)
        body = {"results": [{"vernacularName": "Renard roux", "language": "fra"}]} if "vernacular" in req.url.path else {"category": "LEAST_CONCERN"}
        return httpx.Response(200, json=body)
    tx = Taxonomy(store.connect(":memory:"), backbone=path)
    tx.http = httpx.Client(transport=httpx.MockTransport(api))
    tx._wikidata = lambda t: None
    t = tx.match("Canis vulpes", kingdom="Animalia")
    assert t.gbif_key == 5219243 and t.scientific_name == "Vulpes vulpes" and t.lineage["genus"] == "Vulpes"
    assert t.vernacular["fr"] == "Renard roux" and t.iucn == "LEAST_CONCERN"
    assert sorted(asked) == ["/v1/species/5219243/iucnRedListCategory", "/v1/species/5219243/vernacularNames"]   # ni match ni fiche
    assert tx.match("Vulpes vulpes", enrich=False).gbif_key == 5219243 and len(asked) == 2


def test_detect_groups_filter_boxes_and_results():
    """Groupes cherchés : MegaDetector sauté sans les animaux, boîtes OWLv2 des groupes décochés ignorées,
    sujet identifié dans un groupe décoché écarté avant la vérification GBIF (taxonomy None ici)."""
    from PIL import Image
    from taxonero.pipeline import Analyzer, Candidate

    class Fixed:
        def __init__(self, boxes): self.boxes, self.calls = boxes, 0
        def detect(self, image): self.calls += 1; return list(self.boxes)

    class Clf:   # chaque recadrage : un renard, sauf les boîtes plantes (une fleur)
        def classify(self, images):
            return [[Candidate("Vulpes vulpes", .9, {"kingdom": "Animalia"})] if im.size[0] < 40 else
                    [Candidate("Bellis perennis", .9, {"kingdom": "Plantae"})] for im in images]

    image = Image.new("RGB", (200, 100))
    md = Fixed([Box(.0, .0, .15, .3, .9, "animal", None, "md")])
    ov = Fixed([Box(.5, .5, .4, .4, .8, "plant", "Plantae", "owl"), Box(.3, .0, .1, .2, .7, "animal", "Animalia", "owl")])
    an = Analyzer(md, Clf(), None, open_detector=ov)
    labels = lambda res: sorted(r.decision.candidates[0].name for r in res)
    assert labels(an.analyze(image)) == ["Bellis perennis", "Vulpes vulpes", "Vulpes vulpes"]
    assert labels(an.analyze(image, groups=frozenset({"animal"}))) == ["Vulpes vulpes", "Vulpes vulpes"]
    md.calls = 0
    assert labels(an.analyze(image, groups=frozenset({"plant"}))) == ["Bellis perennis"] and md.calls == 0
    # boîte MegaDetector reconnue comme plante, plantes non cherchées : écartée
    md2 = Fixed([Box(.5, .5, .4, .4, .95, "animal", None, "md")])
    assert Analyzer(md2, Clf(), None).analyze(image, groups=frozenset({"animal"})) == []


def test_microbe_group_same_in_python_and_sql_and_off_by_default(tmp_path):
    """Bactéries et protistes microscopiques : groupe à part, non cherché par défaut ; algues brunes et myxomycètes
    restent avec les plantes. La règle SQL (détections en base) suit la règle Python (analyse)."""
    import json
    from taxonero.config import Settings
    from taxonero.curation import DETECTION_GROUP, drop_unwanted
    from taxonero.pipeline import group_of_lineage
    from taxonero.worker import Worker
    cases = [({"kingdom": "Bacteria", "phylum": "Spirochaetota"}, "microbe"), ({"kingdom": "Archaea"}, "microbe"),
             ({"kingdom": "Viruses"}, "microbe"), ({"kingdom": "Chromista", "phylum": "Myzozoa"}, "microbe"),
             ({"kingdom": "Chromista", "phylum": "Foraminifera"}, "microbe"), ({"kingdom": "Protozoa"}, "microbe"),
             ({"kingdom": "Chromista", "phylum": "Ochrophyta"}, "plant"), ({"kingdom": "Protozoa", "phylum": "Mycetozoa"}, "plant"),
             ({"kingdom": "Plantae"}, "plant"), ({"kingdom": "Archaeplastida"}, "plant"),
             ({"kingdom": "Fungi"}, "fungus"), ({"kingdom": "Metazoa"}, "animal")]
    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off")
    assert "microbe" not in s.groups
    w = Worker(s)
    w.db.execute("INSERT INTO asset (asset_id, people) VALUES ('a', 0)")
    for lineage, group in cases:
        assert group_of_lineage(lineage) == group
        w.db.execute("INSERT INTO detection (asset_id, source, status, candidates, box_label, created_at) "
                     "VALUES ('a', 'auto', 'pending', ?, 'plant', 0)", (json.dumps([{"lineage": lineage}]),))
    sql = [r[0] for r in w.db.execute(f"SELECT {DETECTION_GROUP} FROM detection ORDER BY id")]
    assert sql == [g for _, g in cases]
    assert drop_unwanted(w.db, s) == sum(g == "microbe" for _, g in cases)
    assert w.db.execute("SELECT count(*) FROM detection").fetchone()[0] == sum(g != "microbe" for _, g in cases)


def test_microbes_not_kept_by_default_analysis():
    from PIL import Image
    from taxonero.config import Settings
    from taxonero.pipeline import Analyzer, Candidate

    class Clf:
        def classify(self, images):
            return [[Candidate("Bacillus subtilis", .9, {"kingdom": "Bacteria", "phylum": "Firmicutes"})] for _ in images]

    image = Image.new("RGB", (200, 100))
    an = Analyzer(None, Clf(), None)
    default = Settings(immich_url="http://x", immich_api_key="k").groups
    assert an.analyze(image, fallback=True, groups=default) == []
    assert len(an.analyze(image, fallback=True, groups=default | {"microbe"})) == 1


def test_detect_groups_setting_and_manual_mode(tmp_path):
    import pytest as _pytest
    from taxonero.config import Settings, validate_overrides
    from taxonero.curation import drop_unwanted
    from taxonero.worker import Worker
    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off")
    assert validate_overrides(s, {"detect_groups": "fungus, animal"}) == {"detect_groups": "animal,fungus"}
    for bad in ("", "animal,bird"):
        with _pytest.raises(ValueError):
            validate_overrides(s, {"detect_groups": bad})
    w = Worker(s)
    w.db.execute("INSERT INTO asset (asset_id, people) VALUES ('a', 0)")
    rows = [("auto", "pending", '[{"lineage": {"kingdom": "Plantae"}}]', "plant"),
            ("auto", "pending", '[{"lineage": {"kingdom": "Animalia"}}]', "animal"),
            ("auto", "pending", None, "fungus"),
            ("auto", "confirmed", '[{"lineage": {"kingdom": "Plantae"}}]', "plant"),
            ("manual", "pending", '[{"lineage": {"kingdom": "Plantae"}}]', "plant")]
    w.db.executemany("INSERT INTO detection (asset_id, source, status, candidates, box_label, created_at) VALUES ('a', ?, ?, ?, ?, 0)", rows)
    left = lambda: sorted((r[0], r[1], r[2]) for r in w.db.execute("SELECT source, status, box_label FROM detection"))
    s.detect_groups = "animal"
    assert drop_unwanted(w.db, s) == 2                                  # plante et champignon à relire
    assert left() == [("auto", "confirmed", "plant"), ("auto", "pending", "animal"), ("manual", "pending", "plant")]
    s.detection = "manual"
    assert drop_unwanted(w.db, s) == 1 and w.pending_analysis() == 0    # plus d'analyse automatique
    assert left() == [("auto", "confirmed", "plant"), ("manual", "pending", "plant")]


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="ExifTool absent")
def test_local_folder_source_end_to_end(tmp_path):
    """Source « dossier local » : photos lues (date, GPS), vignettes, puis tags et description écrits dans un
    fichier XMP à côté de la photo, et retirés quand la détection est rejetée. La photo n'est jamais modifiée."""
    import json
    import shutil
    from pathlib import Path
    from taxonero import store
    from taxonero.config import Settings
    from taxonero.pipeline import Analysis, Box, Candidate, Decision
    from taxonero.worker import Worker

    photos = Path(__file__).parent.parent / "dev" / "photos"
    src = photos / "inat_141438356.jpg"                    # GPS à l'ouest de Greenwich : longitude négative
    root = tmp_path / "photos"
    (root / "2024" / "sortie").mkdir(parents=True)
    shutil.copy(src, root / "2024" / "sortie" / "busard.jpg")
    (root / "@eaDir").mkdir()
    shutil.copy(src, root / "@eaDir" / "ignore.jpg")       # dossier technique Synology : ignoré
    before = (root / "2024" / "sortie" / "busard.jpg").read_bytes()
    import os, time as _time
    old = _time.time() - 3600   # photo modifiée il y a une heure (sinon laissée tranquille 10 min, comme avec Immich)
    os.utime(root / "2024" / "sortie" / "busard.jpg", (old, old))

    class Fake:
        def analyze(self, data, lat=None, lon=None, original=None, fallback=None, groups=None):
            c = Candidate("Circus hudsonius", .9, {"kingdom": "Animalia"}, 1, "Circus hudsonius", posterior=.9, prior=.9)
            return [Analysis(Box(.1, .1, .5, .5, .9, "animal"), Decision(1, "Circus hudsonius", "species", .9, .8, "green", [c]))]

    s = Settings(source="local", photos_dir=root, data_dir=tmp_path / "data", dry_run=False, auto_confirm="off", gbif_local=False)
    s.data_dir.mkdir()
    w = Worker(s, analyzer=Fake())
    assert w.configured and w.check()
    assert w.scan() == 1
    a = dict(w.db.execute("SELECT * FROM asset").fetchone())
    assert a["file_name"] == "busard.jpg" and abs(a["lon"] + 5.0918) < 1e-3 and a["taken_at"].startswith("20")
    aid = a["asset_id"]
    assert w.scan() == 0                                   # rien de changé : rien de relu
    thumb = w.client.thumbnail(aid, "thumbnail")
    from PIL import Image
    import io as _io
    assert min(Image.open(_io.BytesIO(thumb)).size) == 250

    store.save_taxon(w.db, 1, json.dumps({"scientific_name": "Circus hudsonius", "rank": "species", "vernacular": {"fr": "Busard d'Amérique"},
                                          "lineage": {"class": "Aves", "family": "Accipitridae", "genus": "Circus"}, "v": 99}))
    w.db.commit()
    assert w.analyze() == 1
    w.db.execute("UPDATE detection SET status = 'confirmed', confirmed_by = 'user'")
    w.db.commit()
    w.sync()
    side = root / "2024" / "sortie" / "busard.jpg.xmp"
    got = w.client.get_asset(aid)
    assert side.exists() and [t["value"] for t in got["tags"]] == ["Nature/Aves/Accipitridae/Circus/Busard d'Amérique (Circus hudsonius)"]
    assert "Busard d'Amérique" in got["exifInfo"]["description"]
    assert (root / "2024" / "sortie" / "busard.jpg").read_bytes() == before   # photo intacte
    w.db.execute("UPDATE detection SET status = 'rejected'")
    w.db.commit()
    w.sync()
    got = w.client.get_asset(aid)
    assert got["tags"] == [] and "Busard" not in got["exifInfo"]["description"]
    shutil.copy(src, root / "autre.jpg")
    (root / "2024" / "sortie" / "busard.jpg").unlink()
    w.reconcile(force=True)
    assert w.db.execute("SELECT missing_at FROM asset WHERE asset_id = ?", (aid,)).fetchone()[0] is not None   # supprimé : masqué
    w.client.close()


def _darktable_library(path, images):
    """library.db réduite aux tables lues (film_rolls, images) : [(dossier, fichier, version)]."""
    import sqlite3
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE film_rolls (id INTEGER PRIMARY KEY, access_timestamp INTEGER, folder VARCHAR(1024) NOT NULL);"
                     "CREATE TABLE images (id INTEGER PRIMARY KEY AUTOINCREMENT, film_id INTEGER, filename VARCHAR, version INTEGER);")
    for folder, name, version in images:
        film = (db.execute("SELECT id FROM film_rolls WHERE folder = ?", (str(folder),)).fetchone()
                or [db.execute("INSERT INTO film_rolls (folder) VALUES (?)", (str(folder),)).lastrowid])[0]
        db.execute("INSERT INTO images (film_id, filename, version) VALUES (?, ?, ?)", (film, name, version))
    db.commit()
    db.close()


def _lightroom_catalog(path, images):
    """Catalogue .lrcat réduit aux tables lues : [(racine, sous-dossier, nom, extension)], une ligne par image
    (une copie virtuelle répète le même fichier)."""
    import sqlite3
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE AgLibraryRootFolder (id_local INTEGER PRIMARY KEY, absolutePath UNIQUE NOT NULL DEFAULT '');"
                     "CREATE TABLE AgLibraryFolder (id_local INTEGER PRIMARY KEY, pathFromRoot NOT NULL DEFAULT '', rootFolder INTEGER NOT NULL DEFAULT 0);"
                     "CREATE TABLE AgLibraryFile (id_local INTEGER PRIMARY KEY, baseName NOT NULL DEFAULT '', extension NOT NULL DEFAULT '', folder INTEGER NOT NULL DEFAULT 0);"
                     "CREATE TABLE Adobe_images (id_local INTEGER PRIMARY KEY, rootFile INTEGER NOT NULL DEFAULT 0, masterImage INTEGER);")
    for root, sub, base, ext in images:
        r = db.execute("INSERT OR IGNORE INTO AgLibraryRootFolder (absolutePath) VALUES (?)", (root,)) and \
            db.execute("SELECT id_local FROM AgLibraryRootFolder WHERE absolutePath = ?", (root,)).fetchone()[0]
        f = (db.execute("SELECT id_local FROM AgLibraryFolder WHERE pathFromRoot = ? AND rootFolder = ?", (sub, r)).fetchone()
             or [db.execute("INSERT INTO AgLibraryFolder (pathFromRoot, rootFolder) VALUES (?, ?)", (sub, r)).lastrowid])[0]
        i = (db.execute("SELECT id_local FROM AgLibraryFile WHERE baseName = ? AND extension = ? AND folder = ?", (base, ext, f)).fetchone()
             or [db.execute("INSERT INTO AgLibraryFile (baseName, extension, folder) VALUES (?, ?, ?)", (base, ext, f)).lastrowid])[0]
        db.execute("INSERT INTO Adobe_images (rootFile) VALUES (?)", (i,))
    db.commit()
    db.close()


def _tag_roundtrip(src, file_name):
    """Pose puis retire un tag sur la photo file_name de la source ; renvoie les sidecars écrits."""
    before = {p: p.read_bytes() for _, p in src._files() if p.exists()}
    assets = {a["originalFileName"]: a for a in src.iter_assets()}
    aid = assets[file_name]["id"]
    tag = "Nature/Aves/Accipitridae/Busard Saint-Martin (Circus cyaneus)"
    src.bulk_tag([tag], [aid])
    got = src.get_asset(aid)
    assert [t["value"] for t in got["tags"]] == [tag]
    written = sorted(p for p in src._sidecars(src._path(aid)) if p.exists())
    keywords = src.et.read([str(p) for p in written], ["XMP-dc:Subject"])
    assert all(k["Subject"] in ("Busard Saint-Martin (Circus cyaneus)", ["Busard Saint-Martin (Circus cyaneus)"]) for k in keywords)
    src.untag_assets(tag, [aid])
    assert src.get_asset(aid)["tags"] == []
    assert all(p.read_bytes() == b for p, b in before.items())   # photos intactes
    return [p.name for p in written]


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="ExifTool absent")
def test_darktable_source(tmp_path):
    """darktable : photos de library.db (base jamais modifiée), sidecars photo.jpg.xmp et photo_01.jpg.xmp d'un
    duplicata, réglages de darktablerc vérifiés."""
    import shutil
    from pathlib import Path
    from taxonero.sources.darktable import Darktable
    src_photo = Path(__file__).parent.parent / "dev" / "photos" / "inat_141438356.jpg"
    photos = tmp_path / "photos"
    photos.mkdir()
    shutil.copy(src_photo, photos / "busard.jpg")
    shutil.copy(src_photo, photos / "autre.jpg")   # dans le dossier, pas dans la bibliothèque : ignorée
    conf = tmp_path / "darktable"
    conf.mkdir()
    _darktable_library(conf / "library.db", [(photos, "busard.jpg", 0), (photos, "busard.jpg", 1), (tmp_path / "parti", "x.jpg", 0)])
    library = (conf / "library.db").read_bytes()
    (conf / "darktablerc").write_text("write_sidecar_files=on import\nrun_crawler_on_start=false\n")
    (tmp_path / "data").mkdir()
    src = Darktable(conf / "library.db", tmp_path / "data", dry_run=False)
    assert src.check() and any("look for updated XMP" in w for w in src.warnings)
    (conf / "darktablerc").write_text("write_sidecar_files=on import\nrun_crawler_on_start=true\n")
    assert src.check() and src.warnings == []
    assert [a["originalFileName"] for a in src.iter_assets()] == ["busard.jpg"]   # fichier absent : ignoré
    assert _tag_roundtrip(src, "busard.jpg") == ["busard.jpg.xmp", "busard_01.jpg.xmp"]
    assert (conf / "library.db").read_bytes() == library
    src.close()
    assert not Darktable(tmp_path / "absent.db", tmp_path / "data").check()


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="ExifTool absent")
def test_lightroom_source(tmp_path):
    """Lightroom Classic : photos du catalogue (copie virtuelle comptée une fois), photo.xmp à côté d'un RAW,
    photo.jpg.xmp pour un JPEG (signalé : Lightroom ne le lit pas), catalogue verrouillé par Lightroom ouvert."""
    import shutil
    import sqlite3
    from pathlib import Path
    from taxonero.sources.lightroom import CatalogLocked, Lightroom
    src_photo = Path(__file__).parent.parent / "dev" / "photos" / "inat_141438356.jpg"
    photos = tmp_path / "Photos"
    (photos / "2024").mkdir(parents=True)
    shutil.copy(src_photo, photos / "2024" / "busard.nef")   # JPEG nommé .nef : ExifTool lit son contenu
    shutil.copy(src_photo, photos / "2024" / "busard.jpg")
    catalog = tmp_path / "Lightroom Catalog.lrcat"
    root = photos.as_posix() + "/"
    _lightroom_catalog(catalog, [(root, "2024/", "busard", "nef"), (root, "2024/", "busard", "nef"), (root, "2024/", "busard", "jpg")])
    (tmp_path / "data").mkdir()
    src = Lightroom(catalog, tmp_path / "data", dry_run=False)
    assert src.check() and src.warnings == ["1 photo(s) JPEG, TIFF, HEIC ou DNG : Lightroom lit leurs mots-clés dans le "
                                            "fichier lui-même, jamais modifié ici ; seuls les RAW les reçoivent dans Lightroom"]
    assert sorted(a["originalFileName"] for a in src.iter_assets()) == ["busard.jpg", "busard.nef"]
    assert _tag_roundtrip(src, "busard.nef") == ["busard.xmp"]
    assert _tag_roundtrip(src, "busard.jpg") == ["busard.jpg.xmp"]
    lock = sqlite3.connect(catalog)   # Lightroom ouvert : catalogue verrouillé en écriture exclusive
    lock.execute("PRAGMA locking_mode = EXCLUSIVE")
    lock.execute("BEGIN EXCLUSIVE")
    with pytest.raises(CatalogLocked):
        list(src.iter_assets())
    assert src.check() and "ferme Lightroom" in src.warnings[0]
    lock.close()
    src.close()


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="ExifTool absent")
def test_rawtherapee_and_rapidraw_sources(tmp_path):
    """RawTherapee : nom du sidecar et synchronisation XMP lus dans son fichier options. RapidRAW : photo.xmp (ou
    photo.XMP existant), synchronisation lue dans settings.json."""
    import json
    import shutil
    from pathlib import Path
    from taxonero.sources.rapidraw import RapidRaw
    from taxonero.sources.rawtherapee import RawTherapee
    src_photo = Path(__file__).parent.parent / "dev" / "photos" / "inat_141438356.jpg"
    photos = tmp_path / "photos"
    photos.mkdir()
    shutil.copy(src_photo, photos / "busard.jpg")
    (tmp_path / "data").mkdir()
    options = tmp_path / "options"
    options.write_text("[General]\nVersion=5.13\n\n[Metadata]\nXMPSidecarStyle=ext\nXMPSynchronization=none\n")
    rt = RawTherapee(photos, tmp_path / "data", dry_run=False, options=options)
    assert rt.check() and any("XMP sidecar synchronization" in w for w in rt.warnings)
    assert _tag_roundtrip(rt, "busard.jpg") == ["busard.jpg.xmp"]
    options.write_text("[Metadata]\nXMPSidecarStyle=std\nXMPSynchronization=read\n")
    assert rt.check() and rt.warnings == []
    assert _tag_roundtrip(rt, "busard.jpg") == ["busard.xmp"]
    rt.close()

    for side in photos.glob("*.xmp"):
        side.unlink()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"enableXmpSync": False}))
    rr = RapidRaw(photos, tmp_path / "data", dry_run=False, settings=settings)
    assert rr.check() and any("synchronisation XMP" in w for w in rr.warnings)
    settings.write_text(json.dumps({"enableXmpSync": True}))
    assert rr.check() and rr.warnings == []
    assert _tag_roundtrip(rr, "busard.jpg") == ["busard.xmp"]
    (photos / "busard.xmp").rename(photos / "busard.XMP")
    assert _tag_roundtrip(rr, "busard.jpg") == ["busard.XMP"]
    rr.close()


def test_sources_by_edition():
    """Logiciels photo : sur le bureau seulement ; Lightroom Classic sur Windows et macOS (sur Linux : ses
    chemins de catalogue ne pourraient pas être ouverts)."""
    import sys
    from taxonero.sources import available
    srv = {o["id"]: o for o in available("server")}
    assert [k for k, o in srv.items() if o["available"]] == ["immich", "local"]
    desk = {o["id"]: o for o in available("desktop")}
    assert desk["darktable"]["available"] and desk["rawtherapee"]["available"] and desk["rapidraw"]["available"]
    assert desk["lightroom"]["available"] == (sys.platform in ("win32", "darwin"))


def test_manual_location_reranks_pending_and_survives_rescan(tmp_path):
    """Lieu indiqué à la main : gardé tant que la source n'a pas de GPS (un vrai GPS le remplace), et les détections
    à relire sont reclassées avec ce lieu à partir des scores du modèle enregistrés."""
    import json
    from taxonero import store
    from taxonero.config import Settings
    from taxonero.curation import relocate
    from taxonero.worker import Worker

    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off", gbif_local=False)
    w = Worker(s)
    store.upsert_asset(w.db, {"id": "a", "checksum": "c", "exifInfo": {}})
    w.db.execute("UPDATE asset SET lat = 45.2, lon = 5.7, geo_user = 1 WHERE asset_id = 'a'")
    store.upsert_asset(w.db, {"id": "a", "checksum": "c", "exifInfo": {"city": "x"}})        # rescan sans GPS : gardé
    assert tuple(w.db.execute("SELECT lat, lon, geo_user FROM asset").fetchone()) == (45.2, 5.7, 1)
    store.upsert_asset(w.db, {"id": "a", "checksum": "c", "exifInfo": {"latitude": 48.0, "longitude": 2.0}})
    assert tuple(w.db.execute("SELECT lat, lon, geo_user FROM asset").fetchone()) == (48.0, 2.0, None)   # vrai GPS

    # Reclassement : le modèle hésite (renard roux 0,5, renard véloce 0,5) ; seul le roux est présent près du point.
    seen = []
    class Tx:
        def match(self, name, kingdom=None, enrich=True, rank=None):
            from taxonero.taxonomy import Taxon
            return Taxon(1 if "vulpes" in name else 2, name, "species", {"kingdom": "Animalia", "genus": "Vulpes"})
        def geo(self, key, lat, lon):
            seen.append((lat, lon))
            return ("green", 1.0) if key == 1 else ("red", 0.02)
        def prefetch_candidates(self, *a, **k): pass
        def fossil_only(self, key): return False
    cands = [{"name": n, "gbif_key": k, "p": .5, "p0": .5, "model_score": .5, "plausibility": "unknown",
              "lineage": {"kingdom": "Animalia"}} for n, k in (("Vulpes velox", 2), ("Vulpes vulpes", 1))]
    w.db.execute("INSERT INTO detection (asset_id, source, status, candidates, candidates_out, created_at) "
                 "VALUES ('a', 'auto', 'pending', ?, '[]', 0)", (json.dumps(cands),))
    w.db.execute("INSERT INTO detection (asset_id, source, status, taxon_key, candidates, created_at) "
                 "VALUES ('a', 'auto', 'confirmed', 2, ?, 0)", (json.dumps(cands),))
    store.save_taxon(w.db, 1, json.dumps({"scientific_name": "Vulpes vulpes", "rank": "species", "lineage": {}, "v": 99}))
    w.db.commit()
    assert relocate(w.db, Tx(), s, ["a"]) == 1                                  # la validée ne bouge pas
    d = w.db.execute("SELECT taxon_key, plausibility FROM detection WHERE status = 'pending'").fetchone()
    assert tuple(d) == (1, "green") and seen and seen[0] == (48.0, 2.0)
    assert w.db.execute("SELECT taxon_key FROM detection WHERE status = 'confirmed'").fetchone()[0] == 2


def test_pipelined_analysis_overlaps_models_and_saving(tmp_path):
    """Chemin en chaîne (infer_many dans le fil « gpu », decide_many et enregistrement dans le fil principal) :
    toutes les photos analysées, une photo illisible écartée sans bloquer les autres, modèles jamais appelés
    depuis le fil principal, temps par étape relevé."""
    import io as _io
    import threading
    from PIL import Image
    from taxonero.config import Settings
    from taxonero.pipeline import Analyzer, Candidate
    from taxonero.worker import Worker

    threads = set()
    class Det:
        providers = ["CPUExecutionProvider"]
        def detect(self, image):
            threads.add(threading.current_thread().name)
            return [Box(.1, .1, .5, .5, .95, "animal", None, "md")]
    class Clf:
        def classify(self, images):
            threads.add(threading.current_thread().name)
            return [[Candidate("Vulpes vulpes", .9, {"kingdom": "Animalia"})] for _ in images]
    def jpeg():
        b = _io.BytesIO(); Image.new("RGB", (300, 200), (90, 120, 60)).save(b, "JPEG"); return b.getvalue()
    class Client:
        def thumbnail(self, aid, size="preview"):
            if aid == "p3":
                raise ValueError("image illisible")
            return jpeg()

    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off", gbif_local=False,
                 open_detector=False)
    w = Worker(s, analyzer=Analyzer(Det(), Clf(), None))
    w.client = Client()
    for i in range(6):
        w.db.execute("INSERT INTO asset (asset_id, file_name, taken_at, kind, people) VALUES (?, ?, ?, 'IMAGE', 0)",
                     (f"p{i}", f"p{i}.jpg", f"2024-01-0{i + 1}"))
    w.db.commit()
    assert w.analyze() == 6
    done = {r[0] for r in w.db.execute("SELECT asset_id FROM asset WHERE analyzed_at IS NOT NULL")}
    assert done == {"p0", "p1", "p2", "p4", "p5"}
    assert w.db.execute("SELECT fail_count FROM asset WHERE asset_id = 'p3'").fetchone()[0] == 1
    assert w.db.execute("SELECT count(*) FROM detection").fetchone()[0] == 5
    assert threads and all(t.startswith("gpu") for t in threads)          # modèles : fil « gpu » seulement
    stages = w.models_info()["stages"]
    assert {"download", "decode", "megadetector", "bioclip", "geo", "save"} <= stages.keys()


def test_desktop_edition_session_and_source(tmp_path):
    """Édition de bureau : pas de mot de passe, mais un jeton par lancement (cookie posé par /?t=…), hôte local
    seulement ; source et écriture réglables depuis le dashboard, chemins vérifiés. Édition serveur inchangée."""
    from fastapi.testclient import TestClient
    from taxonero.config import Settings
    from taxonero.desktop import prepare
    from taxonero.web import create_app
    from taxonero.worker import Worker

    s = prepare(Settings(data_dir=tmp_path))
    assert s.edition == "desktop" and s.dashboard_host == "127.0.0.1" and s.detector_path.parent == tmp_path / "models"
    app = create_app(s, Worker(s), token="jeton-du-lancement")
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    assert c.get("/api/status").status_code == 401
    assert c.get("/?t=faux", follow_redirects=False).status_code == 401
    r = c.get("/?t=jeton-du-lancement", follow_redirects=False)
    assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    assert c.get("/api/status").status_code == 200                                   # cookie gardé par le client
    assert c.get("/api/status", headers={"host": "evil.example"}).status_code == 403  # DNS rebinding
    photos = tmp_path / "photos"
    photos.mkdir()
    r = c.put("/api/source", json={"source": "local", "path": str(photos), "dry_run": False})
    assert r.status_code == 200 and r.json()["current"] == "local" and r.json()["dry_run"] is False
    assert c.put("/api/source", json={"source": "local", "path": "pas/absolu"}).status_code == 422
    assert c.put("/api/source", json={"source": "picasa"}).status_code == 422
    assert c.put("/api/source", json={"source": "darktable", "path": str(photos)}).status_code == 422   # pas un library.db
    library = tmp_path / "library.db"
    _darktable_library(library, [])
    r = c.put("/api/source", json={"source": "darktable", "path": str(library)})
    assert r.status_code == 200 and r.json()["current"] == "darktable"
    assert next(o for o in r.json()["options"] if o["id"] == "darktable")["path"] == str(library)
    assert c.get("/api/status").json()["source_path"] == str(library)
    assert c.get("/api/immich").json()["kind"] == "catalog"

    srv = Settings(data_dir=tmp_path / "srv", dashboard_password="secret")
    srv.data_dir.mkdir()
    cs = TestClient(create_app(srv, Worker(srv)))
    assert cs.get("/api/status").status_code == 401                                   # mot de passe toujours exigé
    assert cs.put("/api/source", json={"dry_run": False}, auth=("nature", "secret")).status_code == 422   # DRY_RUN : environnement
    assert cs.put("/api/source", json={"source": "darktable", "path": str(library)}, auth=("nature", "secret")).status_code == 422   # bureau seulement


def test_desktop_bootstrap_gpu_detection(monkeypatch):
    """Lanceur de bureau : variante de PyTorch choisie selon le GPU (NVIDIA selon le pilote, AMD, Intel Arc,
    Apple Silicon), choix imposé par TAXONERO_GPU, processeur sinon."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("bootstrap", Path(__file__).parent.parent / "packaging" / "bootstrap.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)

    def case(platform_, machine, driver=None, names=(), kfd=False, forced=""):
        monkeypatch.setattr(b.sys, "platform", platform_)
        monkeypatch.setattr(b.platform, "machine", lambda: machine)
        monkeypatch.setattr(b, "nvidia_driver", lambda: driver)
        monkeypatch.setattr(b, "gpu_names", lambda: list(names))
        monkeypatch.setattr(b.Path, "exists", lambda self: kfd if str(self) == "/dev/kfd" else False)
        monkeypatch.setenv("TAXONERO_GPU", forced)
        return b.detect()[0]

    assert case("linux", "x86_64", driver=580.95) == "cu130"
    assert case("win32", "AMD64", driver=566.3) == "cu128"
    assert case("linux", "x86_64", names=["AMD (amdgpu)"], kfd=True) == "rocm"
    assert case("linux", "x86_64", names=["AMD (amdgpu)"], kfd=False) == "cpu"     # pas de calcul ROCm possible
    assert case("win32", "AMD64", names=["AMD Radeon RX 7800 XT"]) == "amd-win"
    assert case("win32", "AMD64", names=["Intel(R) Arc(TM) B580 Graphics"]) == "xpu"
    assert case("linux", "x86_64", names=["Intel (xe)"]) == "xpu"
    assert case("linux", "x86_64", names=["Intel (i915)"]) == "cpu"                # ancien iGPU : pas de XPU
    assert case("darwin", "arm64") == "mps" and case("darwin", "x86_64") == "cpu"
    assert case("linux", "aarch64", driver=580.0) == "cu130"                       # NVIDIA Grace, DGX Spark
    assert case("linux", "aarch64", driver=550.0) == "cpu"                         # pas de CUDA 12 pour ARM
    assert case("win32", "AMD64", driver=580.0, forced="cpu") == "cpu"
    assert set(b.TORCH) == {"cu130", "cu128", "rocm", "xpu", "cpu", "mps", "amd-win"}


def test_series_assign_and_smoothing(tmp_path):
    """Séries : appareil connu, moins de 2 min de proche en proche ; lissage à la MegaDetector sur les détections à
    relire seulement : espèce isolée -> dominante (>= 4 photos, parmi ses candidats), genre -> espèce dominante de
    ce genre (>= 2), jamais un règne ; validées intactes."""
    import json
    from taxonero import series, store
    db = store.connect(tmp_path / "s.sqlite")
    t = lambda m, s: f"2024-05-01T10:{m:02d}:{s:02d}"
    shots = [("a1", t(0, 0), "X"), ("a2", t(0, 50), "X"), ("a3", t(1, 40), "X"), ("a4", t(2, 30), "X"), ("a5", t(3, 10), "X"),
             ("a6", t(3, 50), "X"), ("b1", t(20, 0), "X"), ("c1", t(0, 10), None), ("c2", t(0, 20), None)]
    for aid, taken, make in shots:
        db.execute("INSERT INTO asset (asset_id, taken_at, make, model) VALUES (?, ?, ?, ?)", (aid, taken, make, make))
    db.commit()
    series.assign(db)
    sid = dict(db.execute("SELECT asset_id, series_id FROM asset").fetchall())
    assert {sid[f"a{i}"] for i in range(1, 7)} == {"a1"} and sid["b1"] is None   # 20 min plus tard : seule
    assert sid["c1"] is None and sid["c2"] is None                                  # appareil inconnu : jamais en série

    TAX = {1: ("Parus major", "species", {"genus": "Parus"}), 2: ("Cyanistes caeruleus", "species", {}),
           3: ("Parus", "genus", {}), 4: ("Animalia", "kingdom", {})}
    for k, (n, rank, lin) in TAX.items():
        store.save_taxon(db, k, json.dumps({"scientific_name": n, "rank": rank, "lineage": {**lin, rank: n}}))
    cands = lambda *keys: json.dumps([{"name": TAX[k][0], "gbif_key": k, "p": .3} for k in keys])
    det = lambda aid, key, rank, status="pending", score=.8, c=(1, 2): db.execute(
        "INSERT INTO detection (asset_id, source, status, taxon_key, taxon_rank, score, candidates, model_key, created_at) "
        "VALUES (?, 'auto', ?, ?, ?, ?, ?, ?, 0)", (aid, status, key, rank, score, cands(*c), key))
    for aid in ("a1", "a2", "a3"):
        det(aid, 1, "species")
    det("a4", 1, "species", status="confirmed")
    det("a5", 2, "species")                  # isolée, la dominante (Parus major, 4 photos) parmi ses candidats
    det("a6", 3, "genus", c=(3,))            # genre Parus : précisé en Parus major
    det("a6", 4, "kingdom", c=(4, 1))        # règne : jamais précisé (une poule ne devient pas un chat)
    db.commit()
    assert series.smooth(db, lambda k: store.load_taxon(db, k)) == 2
    got = {(r["asset_id"], r["model_key"]): (r["taxon_key"], r["series_n"]) for r in db.execute("SELECT * FROM detection")}
    assert got[("a5", 2)] == (1, 4) and got[("a6", 3)] == (1, 4) and got[("a6", 4)][0] == 4
    assert {r[0] for r in db.execute("SELECT model_key FROM detection WHERE smoothed = 1")} == {2, 3}   # trace du lissage
    assert got[("a4", 1)] == (1, 4)          # validée : inchangée, son nombre de photos noté
    # 3 photos seulement : pas assez pour remplacer une espèce isolée
    db.execute("UPDATE detection SET taxon_key = model_key, taxon_rank = CASE model_key WHEN 3 THEN 'genus' ELSE taxon_rank END")
    db.execute("UPDATE detection SET status = 'rejected' WHERE asset_id = 'a4'")
    db.commit()
    series.smooth(db, lambda k: store.load_taxon(db, k))
    assert db.execute("SELECT taxon_key FROM detection WHERE asset_id = 'a5'").fetchone()[0] == 2


def test_frame_support_flags_box_beside_subject():
    """Sujet seul : l'espèce retenue presque absente du recadrage (vue sur la photo entière seulement) bloque la
    validation automatique ; le signal suit le taxon (effacé si la décision change d'espèce)."""
    import sqlite3
    from taxonero.autoconfirm import auto_blockers
    from taxonero.curation import UPDATE_DECISION
    from taxonero.pipeline import Decision, frame_support
    lin = lambda g, sp: {"genus": g, "species": sp}
    crop = [Candidate("Podarcis muralis", 0.9, lin("Podarcis", "Podarcis muralis")),
            Candidate("Hierophis viridiflavus", 0.02, lin("Hierophis", "Hierophis viridiflavus"))]
    snake = Candidate("Hierophis viridiflavus", 0.5, lin("Hierophis", "Hierophis viridiflavus"))
    d = Decision(1, "Hierophis viridiflavus", "species", 0.9, 0.5, "green", [snake])
    assert frame_support(crop, d) == pytest.approx(0.02)
    assert frame_support(crop, Decision(2, "Podarcis", "genus", 0.9, 0.5, "green", [crop[0]])) == pytest.approx(0.9)
    assert frame_support(crop, Decision(None, None, "none", 0, 0, "unknown")) is None
    args = ("species", "green", 0.99, "x", 0.0, "animal", 0.85, 0.05)
    assert auto_blockers(*args, frame_support=0.02) == [{"code": "frame"}]
    assert auto_blockers(*args, frame_support=0.4) == [] and auto_blockers(*args) == []
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE detection (id, taxon_key, taxon_rank, score, margin, plausibility, candidates, "
               "candidates_out, frame_support, smoothed, presmooth)")
    db.execute("INSERT INTO detection VALUES (1, 10, 'species', 0.5, 0, 'green', '[]', '[]', 0.02, NULL, NULL)")
    db.execute(UPDATE_DECISION, (10, "species", 0.6, 0, "green", "[]", "[]", 1))
    assert db.execute("SELECT frame_support FROM detection").fetchone()[0] == 0.02      # même taxon : gardé
    db.execute(UPDATE_DECISION, (11, "species", 0.6, 0, "green", "[]", "[]", 1))
    assert db.execute("SELECT frame_support, taxon_key FROM detection").fetchone() == (None, 11)


# -- corrections de l'audit -----------------------------------------------------

def _local_worker(tmp_path, **kw):
    from PIL import Image
    from taxonero.config import Settings
    from taxonero.worker import Worker
    ph = tmp_path / "ph"
    ph.mkdir()
    for i in range(2):
        Image.new("RGB", (64, 48), (40 * i, 120, 80)).save(ph / f"p{i}.jpg")
    s = Settings(data_dir=tmp_path, gbif_local=False, dashboard_no_auth=True, source="local", photos_dir=ph, **kw)

    class NoAnalyzer:
        def analyze(self, *a, **k):
            return []
    w = Worker(s, analyzer=NoAnalyzer())
    w.check()
    return s, w


def _web(s, w, **kw):
    from fastapi.testclient import TestClient
    from taxonero.web import create_app
    return TestClient(create_app(s, w, **kw), raise_server_exceptions=False)


def test_backbone_one_connection_per_thread(tmp_path):
    import sqlite3
    import threading
    from taxonero.backbone import Backbone
    path = tmp_path / "bb.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE t (x)")
    db.commit()
    db.close()
    bb, seen = Backbone(path), []
    t = threading.Thread(target=lambda: seen.append(bb.db))
    t.start()
    t.join()
    assert seen[0] is not bb.db and bb.db is bb.db
    with pytest.raises(sqlite3.OperationalError):
        Backbone(tmp_path / "absent.sqlite")


@pytest.mark.parametrize("gps,expected", [
    (((0, 0), (0, 0), (0, 0)), (None, None)),      # 0/0 : NaN
    (((200, 1), (0, 1), (0, 1)), (None, None)),    # hors limites
    (((45, 1), (30, 1), (0, 1)), (45.5, 45.5)),
])
def test_exif_latlon_rejects_invalid_gps(gps, expected):
    import io
    from PIL import Image
    from PIL.TiffImagePlugin import IFDRational
    from taxonero.pipeline import exif_latlon
    exif = Image.Exif()
    ifd = exif.get_ifd(0x8825)
    v = tuple(IFDRational(*x) for x in gps)
    ifd.update({1: "N", 2: v, 3: "E", 4: v})
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "JPEG", exif=exif)
    assert exif_latlon(buf.getvalue()) == expected


def test_sync_one_refused_photo_does_not_stop_the_others(tmp_path):
    import httpx
    from taxonero.sync import Syncer

    def refusal(code, text="refused"):
        req = httpx.Request("PUT", "http://x")
        return httpx.HTTPStatusError(text, request=req, response=httpx.Response(code, text=text, request=req))

    class Refuses(FakeImmich):
        error = refusal(400)

        def get_asset(self, aid, missing_ok=False):
            if aid == "bad":
                raise self.error
            return super().get_asset(aid, missing_ok=missing_ok)

    db = _sync_db(tmp_path, ["a", "bad", "b"])
    im = Refuses(["a", "b"])
    stats = Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all(full=False)
    assert stats.failed == 1 and stats.assets == 2
    assert {r[0] for r in db.execute("SELECT asset_id FROM asset_sync")} == {"a", "b"}   # « bad » sera retentée
    for code in (401, 503):   # clé refusée, serveur en panne : inutile d'insister sur chaque photo
        im.error = refusal(code)
        db.execute("DELETE FROM asset_sync")
        with pytest.raises(httpx.HTTPStatusError):
            Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all(full=False)


def test_cycle_continues_after_a_failed_step(tmp_path):
    s, w = _local_worker(tmp_path)
    calls = []
    def down():
        raise OSError("GBIF en panne")
    w.prune = down
    w.sync = lambda: calls.append("sync")
    w.backup = lambda: calls.append("backup")
    report = w.run_once()
    assert calls == ["sync", "backup"] and "GBIF en panne" in report["errors"]["pruned"]
    assert "GBIF en panne" in w.progress["error"]
    assert not w.db.in_transaction
    from taxonero import store
    assert store.get_state(w.db, "last_run")


def test_settings_unchanged_when_source_unreachable(tmp_path, monkeypatch):
    import httpx
    from taxonero import store, worker as worker_mod
    s, w = _local_worker(tmp_path, immich_url="http://127.0.0.1:9", immich_api_key="k")
    client = w.client
    w.env_source = False

    class Down:
        dry_run, closed = True, False
        def check(self):
            raise httpx.ConnectError("injoignable")
        def close(self):
            self.closed = True
    down = Down()
    monkeypatch.setattr(worker_mod, "make_source", lambda _: down)
    r = _web(s, w).put("/api/source", json={"source": "immich"})
    assert r.status_code == 502 and r.json()["detail"]["code"] == "source_unreachable"
    assert s.source == "local" and w.client is client and down.closed and not store.get_state(w.db, "source")
    # Écriture : réglage de bureau, resté désactivé si la source ne répond pas.
    s.edition, s.dry_run, client.dry_run = "desktop", True, True
    def boom():
        raise httpx.ConnectError("injoignable")
    w.check = boom
    with pytest.raises(httpx.ConnectError):
        w.set_dry_run(False)
    assert s.dry_run is True and client.dry_run is True


def test_thumbnail_concurrent_requests(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    s, w = _local_worker(tmp_path)
    w.scan()
    aid = w.db.execute("SELECT asset_id FROM asset").fetchone()[0]
    with ThreadPoolExecutor(8) as ex:
        futures = [ex.submit(w.client.thumbnail, aid) for _ in range(40)]
        assert all(f.result() for f in futures)
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.skipif(sys.platform == "win32", reason="droits POSIX")
def test_database_is_private(tmp_path):
    from taxonero import store
    path = tmp_path / "x.sqlite"
    db = store.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS t (x)")
    db.commit()
    for suffix in ("", "-wal", "-shm"):
        assert Path(f"{path}{suffix}").stat().st_mode & 0o777 == 0o600
    db.close()
    path.chmod(0o644)     # base créée avant cette règle
    store.connect(path).close()
    assert path.stat().st_mode & 0o777 == 0o600


def test_security_headers_and_non_ascii_token(tmp_path):
    s, w = _local_worker(tmp_path)
    r = _web(s, w).get("/healthz")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    s.edition = "desktop"
    c = _web(s, w, token="jeton")
    c.base_url = "http://127.0.0.1"
    assert c.get("/?t=é", follow_redirects=False).status_code == 401
    assert c.get("/api/status", headers={"cookie": "taxonero_session=é".encode("latin-1")}).status_code == 401


def test_location_needs_both_coordinates(tmp_path):
    s, w = _local_worker(tmp_path)
    w.scan()
    aid = w.db.execute("SELECT asset_id FROM asset").fetchone()[0]
    w.db.execute("UPDATE asset SET lat = 1, lon = 2, geo_user = 1 WHERE asset_id = ?", (aid,))
    w.db.commit()
    r = _web(s, w).post("/api/assets/location", json={"asset_ids": [aid], "lat": 3})
    assert r.status_code == 400
    assert tuple(w.db.execute("SELECT lat, lon FROM asset WHERE asset_id = ?", (aid,)).fetchone()) == (1, 2)


def test_failed_relocation_leaves_no_open_transaction(tmp_path, monkeypatch):
    import taxonero.web.review as review
    from taxonero import store
    s, w = _local_worker(tmp_path)
    w.scan()
    aid = w.db.execute("SELECT asset_id FROM asset").fetchone()[0]
    w.db.execute("UPDATE asset SET lat = NULL, lon = NULL WHERE asset_id = ?", (aid,))
    w.db.commit()
    def half_written(db, *a):
        store.save_taxon(db, 1, "{}")   # écrit sans valider, puis panne réseau
        raise OSError("GBIF en panne")
    monkeypatch.setattr(review, "relocate", half_written)
    with _web(s, w) as c:
        r = c.post("/api/assets/location", json={"asset_ids": [aid], "lat": 3, "lon": 4})
    assert r.status_code == 200 and r.json()["reclassified"] == 0
    w.db.execute("INSERT OR REPLACE INTO state VALUES ('x', '1')")   # un autre écrivain passe
    w.db.commit()


def test_series_of_local_dates(tmp_path, monkeypatch):
    import time
    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    try:
        s, w = _local_worker(tmp_path)
        w.scan()
        a, b = [r[0] for r in w.db.execute("SELECT asset_id FROM asset ORDER BY file_name")]
        for aid, at in ((a, "2026-07-01T12:00:00"), (b, "2026-07-01T12:00:02")):
            w.db.execute("UPDATE asset SET make = 'X', model = 'Y', taken_at = ? WHERE asset_id = ?", (at, aid))
        w.db.commit()
        r = _web(s, w).get(f"/api/assets/{a}/series")
        assert r.status_code == 200 and a in r.text and b in r.text
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_idle_models_leave_gpu_memory(tmp_path, monkeypatch):
    import time
    import weakref
    from taxonero import hardware
    s, w = _local_worker(tmp_path, models_idle_minutes=5)
    released = []
    monkeypatch.setattr(hardware, "release", released.append)

    class Models:
        timings = {"detection": [1.0, 1]}
    models = Models()
    ref = weakref.ref(models)
    w._analyzer, w._device, w._models_used = models, "cuda", time.monotonic()
    del models
    assert not w.unload_idle() and w.models_loaded                  # utilisés il y a moins de 5 min
    w._models_used -= 299
    assert not w.unload_idle()
    w._models_used -= 2
    s.models_idle_minutes = 0
    assert not w.unload_idle()                                      # 0 : toujours gardés
    s.models_idle_minutes = 5
    w._device = "cpu"
    assert not w.unload_idle()                                      # pas de mémoire de GPU à rendre
    w._device = "cuda"
    assert w.unload_idle() and not w.models_loaded and released == ["cuda"] and ref() is None
    assert w.models_info()["stages"]["detection"] == 1000           # temps mesurés toujours affichés


def test_wikipedia_found_through_name_or_synonym():
    """Fiche Wikidata de la clé GBIF sans article : article cherché sur le synonyme (Ardea modesta / Ardea alba
    modesta), sans reprendre le nom latin du synonyme comme nom commun. Un nom ambigu (deux fiches) est ignoré."""
    import httpx
    from taxonero import store
    from taxonero.taxonomy import Taxon, Taxonomy
    ents = {"Q1": {"labels": {"en": {"value": "Ardea modesta"}}},
            "Q2": {"labels": {"en": {"value": "Ardea alba modesta"}, "fr": {"value": "Aigrette d'Australasie"}},
                   "sitelinks": {"enwiki": {"url": "https://en.wikipedia.org/wiki/Eastern_great_egret"}}},
            "Q3": {"sitelinks": {"frwiki": {"url": "https://fr.wikipedia.org/wiki/A"}}},
            "Q4": {"sitelinks": {"frwiki": {"url": "https://fr.wikipedia.org/wiki/B"}}}}
    search = {"haswbstatement:P846=1": ["Q1"], 'haswbstatement:"P225=Ardea alba modesta"': ["Q2"],
              'haswbstatement:"P225=Homonyme"': ["Q3", "Q4"]}

    def api(req):
        if req.url.path.endswith("/synonyms"):
            return httpx.Response(200, json={"results": [{"canonicalName": "Ardea alba modesta", "rank": "SUBSPECIES"}] if "/1/" in req.url.path else []})
        if req.url.params.get("action") == "wbgetentities":
            return httpx.Response(200, json={"entities": {req.url.params["ids"]: ents[req.url.params["ids"]]}})
        return httpx.Response(200, json={"query": {"search": [{"title": q} for q in search.get(req.url.params["srsearch"], [])]}})
    tx = Taxonomy(store.connect(":memory:"), langs=("fr", "en"))
    tx.http = httpx.Client(transport=httpx.MockTransport(api))
    tx.min_interval = 0
    t = Taxon(1, "Ardea modesta", "species")
    tx._wikidata(t)
    assert t.wikidata == "Q2" and t.wikipedia == {"en": "https://en.wikipedia.org/wiki/Eastern_great_egret"}
    assert t.vernacular == {"fr": "Aigrette d'Australasie"}
    amb = Taxon(2, "Homonyme", "species")
    tx._wikidata(amb)
    assert amb.wikipedia == {} and amb.wikidata is None


def test_wikidata_overload_is_not_cached(monkeypatch):
    """Surcharge de Wikidata (200 et {"error": …}) : relance, et jamais mise en cache comme une réponse vide."""
    import httpx
    from taxonero import store
    from taxonero.taxonomy import WIKIDATA, Taxonomy
    monkeypatch.setattr("taxonero.taxonomy.time.sleep", lambda s: None)
    replies = [{"error": {"code": "too-busy"}}, {"query": {"search": [{"title": "Q1"}]}}]
    tx = Taxonomy(store.connect(":memory:"))
    tx.http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=replies.pop(0))))
    tx.min_interval = 0
    assert tx.get(WIKIDATA, {"action": "query"})["query"]["search"][0]["title"] == "Q1" and not replies
    tx.http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"error": "x"})))
    with pytest.raises(httpx.HTTPError):
        tx.get(WIKIDATA, {"action": "other"})
    assert tx.db.execute("SELECT count(*) FROM http_cache WHERE body LIKE '%error%'").fetchone()[0] == 0


def test_articles_stale_only_for_species_without_article():
    from taxonero.taxonomy import Taxonomy
    assert Taxonomy.articles_stale({"rank": "species", "wikipedia": {}})
    assert not Taxonomy.articles_stale({"rank": "species", "wikipedia": {"fr": "u"}})
    assert not Taxonomy.articles_stale({"rank": "genus", "wikipedia": {}})
    assert not Taxonomy.articles_stale({"rank": "species", "wikipedia": {}, "wv": 1})


def test_log_buffer_keeps_warnings_apart_and_across_restarts(tmp_path):
    import logging
    from types import SimpleNamespace
    from taxonero.logbuffer import FILENAME, LogBuffer
    s = SimpleNamespace(data_dir=tmp_path, immich_api_key="cle-secrete", dashboard_password="")

    def line(buf, level, msg):
        buf.emit(logging.LogRecord("t", level, __file__, 1, msg, None, None))

    buf = LogBuffer(capacity=3, keep=4)
    buf.bind(s)
    line(buf, logging.ERROR, "panne cle-secrete")
    for k in range(10):                                          # les lignes d'information ne chassent pas l'erreur
        line(buf, logging.INFO, f"info {k}")
    assert [e["msg"] for e in buf.read()] == ["panne ***", "info 7", "info 8", "info 9"]
    assert "cle-secrete" not in (tmp_path / FILENAME).read_text()
    assert (tmp_path / FILENAME).stat().st_mode & 0o077 == 0

    for k in range(10):                                          # les 4 derniers avertissements, fichier réduit en route
        line(buf, logging.WARNING, f"alerte {k}")
        line(buf, logging.INFO, f"info {k}")
    assert [e["msg"] for e in buf.read() if e["level"] != "INFO"] == [f"alerte {k}" for k in range(6, 10)]
    assert len((tmp_path / FILENAME).read_text().splitlines()) <= 8
    last = buf.kept[-1]["id"]
    with (tmp_path / FILENAME).open("a") as f:
        f.write('{"id": 1, "coup')                               # arrêt brutal au milieu d'une écriture

    again = LogBuffer(capacity=3, keep=4)                        # redémarrage : conservées, numérotation qui continue
    again.bind(s)
    assert [e["msg"] for e in again.read()] == [f"alerte {k}" for k in range(6, 10)]
    line(again, logging.ERROR, "nouvelle")
    assert again.read()[-1]["id"] > last

    cleared = again.read()[-1]["id"]                            # bouton « Effacer » : tout, fichier compris
    again.clear()
    assert again.read() == [] and not (tmp_path / FILENAME).exists()
    line(again, logging.WARNING, "après")
    assert [e["msg"] for e in again.read(cleared)] == ["après"]


class _UndoClient:
    """Immich simulé pour le retrait : photos en mémoire, écritures perdues ou refusées à la demande."""
    dry_run = False

    def __init__(self, assets):
        self.assets, self.lose, self.down, self.writes = assets, 0, False, []

    def get_asset(self, aid, missing_ok=False):
        if self.down:
            import httpx
            raise httpx.ConnectError("panne")
        a = self.assets.get(aid)
        return a and {"id": aid, "tags": [{"id": t} for t in a["tags"]], "exifInfo": {"description": a["desc"]}}

    def untag_assets(self, tid, ids):
        self.writes.append(("untag", ids[0], tid))
        if self.lose:
            self.lose -= 1
            return
        self.assets[ids[0]]["tags"].remove(tid)

    def set_description(self, aid, text):
        self.writes.append(("desc", aid, text))
        self.assets[aid]["desc"] = text

    def list_tags(self):
        return [{"id": "t1", "value": "Nature"}]

    def delete_tag(self, tid):
        self.writes.append(("delete", tid))

    def count_assets_with_tag(self, tid):
        return sum(tid in a["tags"] for a in self.assets.values())


def _undo_db(tmp_path, rows):
    from taxonero import store
    db = store.connect(tmp_path / "u.sqlite")
    db.executemany("INSERT INTO written VALUES (?, ?, ?, ?, 1)", rows)
    db.commit()
    return db


def test_uninstall_rereads_lost_writes_and_leaves_user_text_alone(tmp_path):
    from taxonero import store
    from taxonero.sync import BLOCK_END, BLOCK_START, Syncer
    block = f"{BLOCK_START}\nx\n{BLOCK_END}"
    c = _UndoClient({"a": {"tags": ["t1", "perso"], "desc": f"Mon texte\n\n{block}"},
                     "b": {"tags": [], "desc": "  texte sans bloc  "}})   # bloc déjà retiré à la main
    c.lose = 1                                                             # Immich perd la première écriture
    db = _undo_db(tmp_path, [("tag", "t1", "", "Nature"), ("tag_asset", "t1", "a", "Nature"),
                             ("description", "a", "a", None), ("description", "b", "b", None)])
    s = Syncer(db, c, write_gap=0)
    s.uninstall()
    assert c.assets["a"] == {"tags": ["perso"], "desc": "Mon texte"}
    assert c.assets["b"]["desc"] == "  texte sans bloc  "                  # pas récrit
    assert not any(w[0] == "desc" and w[1] == "b" for w in c.writes)
    assert s.remaining == 0 and db.execute("SELECT count(*) FROM written").fetchone()[0] == 0
    assert ("delete", "t1") in c.writes
    assert store.get_state(db, store.WRITES_PAUSED) == "1"


def test_uninstall_stops_on_outage_and_keeps_traces(tmp_path):
    from taxonero import store
    from taxonero.sync import MAX_CONSECUTIVE_FAILURES, Syncer
    n = MAX_CONSECUTIVE_FAILURES + 5
    c = _UndoClient({f"p{k}": {"tags": ["t1"], "desc": ""} for k in range(n)})
    c.down = True
    db = _undo_db(tmp_path, [("tag", "t1", "", "Nature")] + [("tag_asset", "t1", f"p{k}", "Nature") for k in range(n)])
    with pytest.raises(RuntimeError, match="échecs de suite"):
        Syncer(db, c, write_gap=0).uninstall()
    assert not c.writes                                                    # le tag n'est pas supprimé
    assert db.execute("SELECT count(*) FROM written").fetchone()[0] == n + 1
    assert store.get_state(db, store.WRITES_PAUSED) == "1"                 # le cycle ne repose rien entre-temps
    c.down = False
    s = Syncer(db, c, write_gap=0)                                         # relancer reprend
    s.uninstall()
    assert s.remaining == 0 and ("delete", "t1") in c.writes


def test_uninstall_keeps_tag_while_a_photo_resists(tmp_path):
    from taxonero.sync import Syncer
    c = _UndoClient({"a": {"tags": ["t1"], "desc": ""}})
    c.lose = 99
    db = _undo_db(tmp_path, [("tag", "t1", "", "Nature"), ("tag_asset", "t1", "a", "Nature")])
    s = Syncer(db, c, write_gap=0)
    s.uninstall()
    assert s.remaining == 1 and not any(w[0] == "delete" for w in c.writes)
    assert db.execute("SELECT count(*) FROM written").fetchone()[0] == 2


def test_interrupted_sync_keeps_the_photos_already_written(tmp_path):
    """Une première synchronisation longue coupée en route (réglage du dashboard, redémarrage) ne repart pas de zéro :
    chaque photo est notée dès qu'elle est écrite."""
    from taxonero.sync import Syncer

    class Stop(BaseException):
        pass

    db = _sync_db(tmp_path, ["a", "b", "c"])
    im = FakeImmich(["a", "b", "c"])
    calls = []

    def checkpoint():   # 3 lectures, 3 tags, puis arrêt avant la description de la 2e photo
        calls.append(1)
        if len(calls) == 8:
            raise Stop()

    with pytest.raises(Stop):
        Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0, checkpoint=checkpoint).sync_all(full=False)
    assert [r[0] for r in db.execute("SELECT asset_id FROM asset_sync")] == ["a"]
    im.gets = 0
    st = Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all(full=False)
    assert st.assets == 2 and {r[0] for r in db.execute("SELECT asset_id FROM asset_sync")} == {"a", "b", "c"}


def test_sync_refused_in_a_later_round_is_retried(tmp_path):
    """Photo notée synchronisée au premier tour puis refusée à la relecture : reprise au cycle suivant."""
    import httpx
    from taxonero.sync import Syncer

    class RefusesSecondRead(FakeImmich):
        def get_asset(self, aid, missing_ok=False):
            if aid == "a" and self.gets >= 2:
                req = httpx.Request("GET", "http://x")
                raise httpx.HTTPStatusError("refused", request=req, response=httpx.Response(400, text="refused", request=req))
            return super().get_asset(aid, missing_ok=missing_ok)

    db = _sync_db(tmp_path, ["a"])
    st = Syncer(db, RefusesSecondRead(["a"]), write_gap=0, settle_seconds=0, verify_rounds=1).sync_all(full=False)
    assert st.failed == 1 and not db.execute("SELECT 1 FROM asset_sync").fetchone()


def test_checkpoints_count_as_activity_for_healthz(tmp_path):
    """Une longue synchronisation passe par des points d'arrêt sans « tick » : /healthz ne doit pas la croire figée."""
    s, w = _local_worker(tmp_path)
    w.loop_started, w.last_activity = True, 0.0
    w._checkpoint()
    assert time.time() - w.last_activity < 5
    assert _web(s, w).get("/healthz").status_code == 200


def test_megadetector_checkpoint_hash_checked_before_loading(tmp_path, monkeypatch):
    """Le point de contrôle complet est lu par pickle : un fichier présent mais d'une autre empreinte n'est jamais chargé."""
    import hashlib
    import types
    from taxonero import megadetector, worker

    loaded = []
    fake = types.SimpleNamespace(load=lambda p, **k: loaded.append(p.read_bytes()) or {"model": {}},
                                 save=lambda obj, p: p.write_bytes(b"compact"))
    monkeypatch.setitem(sys.modules, "torch", fake)
    monkeypatch.setattr(megadetector, "SHA256", hashlib.sha256(b"official").hexdigest())
    fetched = []
    monkeypatch.setattr(worker, "fetch_model", lambda url, dest, sha, ua: fetched.append(1) or dest.write_bytes(b"official"))
    models = tmp_path / "models"
    models.mkdir()
    (models / f"{megadetector.VERSION}.pth").write_bytes(b"planted")
    out = megadetector.weights(models)
    assert loaded == [b"official"] and fetched == [1] and out.read_bytes() == b"compact"


def test_a_long_walk_is_not_one_burst(tmp_path):
    """Une balade photographiée toutes les minutes pendant 3 h n'est pas une seule série : 10 min au plus par série.
    Et même dans une vraie rafale, une espèce sûre (92 %) n'est pas remplacée par une dominante qu'elle donne à 3 %."""
    import json
    from taxonero import series, store
    db = store.connect(tmp_path / "s.sqlite")
    for i in range(180):   # une photo par minute, ~50 m entre deux
        db.execute("INSERT INTO asset (asset_id, taken_at, lat, lon, make, model) VALUES (?, ?, ?, ?, 'OM', 'OM-1')",
                   (f"p{i:03d}", f"2026-05-01T{8 + i // 60:02d}:{i % 60:02d}:00", 45 + i * 0.00045, 5.0))
    db.commit()
    series.assign(db)
    spans = db.execute("SELECT series_id, min(taken_at), max(taken_at), count(*) FROM asset GROUP BY series_id").fetchall()
    assert len(spans) == 17 and max(r[3] for r in spans) == 11   # 10 min au plus : 11 photos (de 0 à 600 s)

    MERLE, ROUGEGORGE = 1, 2
    det = lambda aid, key, score, cands: db.execute(
        "INSERT INTO detection (asset_id, source, status, taxon_key, taxon_rank, score, candidates, model_key, created_at) "
        "VALUES (?, 'auto', 'pending', ?, 'species', ?, ?, ?, 0)", (aid, key, score, json.dumps(cands), key))
    for aid in ("p000", "p001", "p002", "p003"):
        det(aid, MERLE, 0.9, [{"gbif_key": MERLE, "p": 0.9}])
    det("p170", ROUGEGORGE, 0.92, [{"gbif_key": ROUGEGORGE, "p": 0.92}, {"gbif_key": MERLE, "p": 0.03}])   # 2 h 50 plus tard
    det("p005", ROUGEGORGE, 0.92, [{"gbif_key": ROUGEGORGE, "p": 0.92}, {"gbif_key": MERLE, "p": 0.03}])   # même série
    db.commit()
    assert series.smooth(db, lambda k: {"lineage": {}}) == 0
    assert {r[0] for r in db.execute("SELECT taxon_key FROM detection WHERE asset_id IN ('p005', 'p170')")} == {ROUGEGORGE}
    # Candidat crédible (le modèle hésitait) : le lissage s'applique toujours.
    db.execute("UPDATE detection SET score = 0.55, candidates = ? WHERE asset_id = 'p005'",
               (json.dumps([{"gbif_key": ROUGEGORGE, "p": 0.55}, {"gbif_key": MERLE, "p": 0.35}]),))
    db.commit()
    assert series.smooth(db, lambda k: {"lineage": {}}) == 1
    assert db.execute("SELECT taxon_key, smoothed FROM detection WHERE asset_id = 'p005'").fetchone()[:] == (MERLE, 1)


def test_wrong_smoothing_from_before_is_undone(tmp_path):
    """Lissage fait par l'ancienne règle (balade de 3 h prise pour une rafale, sans presmooth) : la proposition du
    modèle est rétablie, même si la photo n'est plus dans aucune série ; un lissage encore justifié reste."""
    import json
    from taxonero import series, store
    db = store.connect(tmp_path / "s.sqlite")
    for i in range(180):
        db.execute("INSERT INTO asset (asset_id, taken_at, lat, lon, make, model) VALUES (?, ?, ?, ?, 'OM', 'OM-1')",
                   (f"p{i:03d}", f"2026-05-01T{8 + i // 60:02d}:{i % 60:02d}:00", 45 + i * 0.00045, 5.0))
    MERLE, ROUGEGORGE = 1, 2
    det = lambda aid, key, score, cands, **kw: db.execute(
        "INSERT INTO detection (asset_id, source, status, taxon_key, taxon_rank, score, plausibility, candidates, model_key, "
        "model_rank, model_conf, smoothed, series_n, created_at) VALUES (?, 'auto', 'pending', ?, 'species', ?, ?, ?, ?, "
        "'species', ?, ?, ?, 0)", (aid, key, score, kw.get("plaus", "green"), json.dumps(cands), kw.get("model", key),
                                   kw.get("model_conf", score), kw.get("smoothed"), kw.get("series_n")))
    for aid in ("p000", "p001", "p002", "p003"):
        det(aid, MERLE, 0.9, [{"gbif_key": MERLE, "p": 0.9, "plausibility": "green"}])
    # Lissées à tort autrefois : 2 h 50 plus tard (p170), et dans une autre rafale avec un merle marginal (p030).
    for aid in ("p170", "p030"):
        det(aid, MERLE, 0.03, [{"gbif_key": ROUGEGORGE, "p": 0.92, "plausibility": "amber"}, {"gbif_key": MERLE, "p": 0.03}],
            model=ROUGEGORGE, model_conf=0.92, smoothed=1, series_n=4, plaus="green")
    # Lissée à juste titre (le modèle hésitait) : gardée.
    det("p005", MERLE, 0.35, [{"gbif_key": ROUGEGORGE, "p": 0.55}, {"gbif_key": MERLE, "p": 0.35}],
        model=ROUGEGORGE, model_conf=0.55, smoothed=1, series_n=4)
    db.commit()
    series.assign(db)
    assert db.execute("SELECT series_id FROM asset WHERE asset_id = 'p170'").fetchone()[0] != "p000"
    for aid in ("p031", "p032", "p033", "p034"):   # rafale de p030 : quatre merles
        det(aid, MERLE, 0.9, [{"gbif_key": MERLE, "p": 0.9}])
    db.commit()
    assert series.smooth(db, lambda k: {"lineage": {}}) == 3   # 2 rétablies + p005 notée avec sa proposition d'avant
    got = {r["asset_id"]: dict(r) for r in db.execute("SELECT * FROM detection WHERE asset_id IN ('p005', 'p030', 'p170')")}
    for aid in ("p030", "p170"):
        assert (got[aid]["taxon_key"], got[aid]["score"], got[aid]["plausibility"], got[aid]["smoothed"]) == (ROUGEGORGE, 0.92, "amber", None)
    assert (got["p005"]["taxon_key"], got["p005"]["smoothed"]) == (MERLE, 1)
    assert json.loads(got["p005"]["presmooth"])["taxon_key"] == ROUGEGORGE
    assert series.smooth(db, lambda k: {"lineage": {}}) == 0   # stable d'un cycle à l'autre


def test_analysis_pause_stops_after_current_group_and_survives_restart(tmp_path, monkeypatch):
    """Bouton pause : le groupe en cours finit, les suivants attendent ; run_once n'analyse plus ; l'état est gardé
    d'un lancement à l'autre ; la reprise relance un cycle."""
    from fastapi.testclient import TestClient
    from taxonero import hardware
    from taxonero.config import Settings
    from taxonero.desktop import prepare
    from taxonero.web import create_app
    from taxonero.worker import Worker

    class Pausing:
        calls = 0

        def analyze(self, data, *a, **k):
            return []

        def analyze_many(self, items, fallback=None, groups=None):
            Pausing.calls += 1
            w.analysis_paused = True                                  # pause demandée pendant le premier groupe
            return [[] for _ in items]

    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off", gpu_batch=4)
    w = Worker(s, analyzer=Pausing())
    monkeypatch.setattr(hardware, "resolve", lambda wanted: ("cuda", "gpu_found"))
    w._device = "cuda"
    w.client.thumbnail = lambda aid, size: b"ok"
    for i in range(10):
        w.db.execute("INSERT INTO asset (asset_id, file_name) VALUES (?, ?)", (f"p{i}", f"p{i}.jpg"))
    w.db.commit()
    w.analyze()
    assert Pausing.calls == 1
    assert w.db.execute("SELECT count(*) FROM asset WHERE analyzed_at IS NOT NULL").fetchone()[0] == 4
    for step in ("check", "scan", "reconcile", "apply_skips", "refresh_taxa", "refresh_places"):
        monkeypatch.setattr(w, step, lambda: 0)
    monkeypatch.setattr(w, "check", lambda: True)
    report = w.run_once(manual=True)                                 # en pause : rien d'analysé, même à la main
    assert report["analyzed"] == 0 and Pausing.calls == 1

    d = prepare(Settings(data_dir=tmp_path / "bureau"))
    dw = Worker(d)
    started = []
    monkeypatch.setattr(dw, "start_manual", lambda: started.append(1) or True)
    c = TestClient(create_app(d, dw, token="t"), base_url="http://127.0.0.1:8765")
    c.get("/?t=t", follow_redirects=False)
    assert c.post("/api/analysis/pause", json={"paused": True}).json() == {"paused": True}
    assert c.get("/api/sync/status").json()["analysis_paused"] is True
    assert Worker(d).analysis_paused                                 # redémarrage : toujours en pause
    assert c.post("/api/analysis/pause", json={"paused": False}).json() == {"paused": False} and started == [1]
    assert not Worker(d).analysis_paused


def test_pause_and_resume_in_the_running_loop(tmp_path, monkeypatch):
    """Boucle réelle : la pause arrête l'analyse en cours de file (le reste attend), la reprise relance aussitôt un
    cycle qui finit la file, même hors de la fenêtre horaire."""
    import threading
    import time as _time
    from taxonero.config import Settings
    from taxonero.worker import Worker

    class Slow:
        def analyze(self, data, *a, **k):
            _time.sleep(0.02)
            return []

    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off", batch_size=5,
                 analysis_window="03:00-03:01", gbif_local=False)
    w = Worker(s, analyzer=Slow())
    w.client.thumbnail = lambda aid, size: b"ok"
    for step in ("scan", "reconcile", "apply_skips", "refresh_taxa", "refresh_places", "prune", "sync", "prefetch"):
        monkeypatch.setattr(w, step, lambda *a, **k: 0)
    monkeypatch.setattr(w, "check", lambda: True)
    monkeypatch.setattr(w, "_watch_idle_models", lambda: None)
    for i in range(60):
        w.db.execute("INSERT INTO asset (asset_id, file_name, taken_at) VALUES (?, ?, '2024-01-01')", (f"p{i}", f"p{i}.jpg"))
    w.db.commit()
    done = lambda: w.db.execute("SELECT count(*) FROM asset WHERE analyzed_at IS NOT NULL").fetchone()[0]
    threading.Thread(target=w.loop, daemon=True).start()
    _time.sleep(0.3)
    assert done() == 0                                   # hors de la fenêtre horaire : la boucle n'analyse pas
    assert w.start_manual()
    for _ in range(500):
        if done() >= 5:
            break
        _time.sleep(0.02)
    w.pause_analysis(True)
    _time.sleep(0.5)
    paused_at = done()
    _time.sleep(0.5)
    assert 0 < paused_at < 60 and done() == paused_at    # plus rien ne s'analyse
    w.pause_analysis(False)                              # cycle peut-être encore en cours : relancé à sa fin
    for _ in range(300):
        if done() == 60:
            break
        _time.sleep(0.02)
    assert done() == 60


def test_prefetch_stops_on_network_outage_with_one_line(tmp_path, monkeypatch, caplog):
    """Réseau coupé pendant la mise en local : une ligne d'avertissement, sans trace, et le reste au cycle suivant
    (avant : une trace complète par taxon)."""
    import logging
    import httpx
    from taxonero import ranges
    from taxonero.config import Settings
    from taxonero.worker import Worker

    w = Worker(Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, gbif_local=False))
    w.db.execute("INSERT INTO asset (asset_id, file_name) VALUES ('a', 'a.jpg')")
    for k in (1, 2, 3):
        w.db.execute("INSERT INTO detection (asset_id, status, taxon_key, taxon_rank, source, created_at) VALUES ('a', 'confirmed', ?, 'species', 'bioclip', '2026-01-01')", (k,))
    w.db.commit()
    calls = []
    monkeypatch.setattr(w.tx, "match", lambda name: None)
    monkeypatch.setattr(ranges, "cached", lambda db, key: None)
    monkeypatch.setattr(ranges, "fetch", lambda tx, key: calls.append(key) or (_ for _ in ()).throw(httpx.ConnectError("Name or service not known")))
    with caplog.at_level(logging.WARNING):
        w.prefetch()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(calls) == 1 and len(warnings) == 1 and not warnings[0].exc_info
    assert "reprise au prochain cycle" in warnings[0].getMessage()


def test_desktop_folder_picker_goes_through_the_server(tmp_path):
    """Bureau : « Parcourir… » passe par /api/desktop/pick (window.pywebview.api, créé avec new Function, est refusé
    par la CSP) ; jeton de session exigé ; ailleurs, pas de bouton et 404."""
    from fastapi.testclient import TestClient
    from taxonero.config import Settings
    from taxonero.desktop import prepare
    from taxonero.web import create_app
    from taxonero.worker import Worker

    class Picker:
        def pick_folder(self):
            return "/home/moi/Photos"

        def pick_file(self, suffix, start=""):
            return f"{start or '/x'}/library{suffix}"

    d = prepare(Settings(data_dir=tmp_path / "bureau"))
    c = TestClient(create_app(d, Worker(d), token="t", picker=Picker()), base_url="http://127.0.0.1:8765")
    assert c.post("/api/desktop/pick", json={"kind": "folder"}).status_code == 401   # sans le jeton
    c.get("/?t=t", follow_redirects=False)
    assert c.get("/api/status").json()["can_pick"] is True
    assert c.post("/api/desktop/pick", json={"kind": "folder"}).json() == {"path": "/home/moi/Photos"}
    assert c.post("/api/desktop/pick", json={"kind": "file", "suffix": ".db", "start": "/a"}).json() == {"path": "/a/library.db"}
    s = prepare(Settings(data_dir=tmp_path / "sans"))
    c2 = TestClient(create_app(s, Worker(s), token="u"), base_url="http://127.0.0.1:8765")
    c2.get("/?t=u", follow_redirects=False)
    assert c2.get("/api/status").json()["can_pick"] is False
    assert c2.post("/api/desktop/pick", json={"kind": "folder"}).status_code == 404


def test_pause_while_models_load_frees_them(tmp_path, monkeypatch):
    """Pause pendant le chargement des modèles (téléchargement de plusieurs Go) : le cycle n'attend pas la fin du
    chargement, et les modèles ne restent pas en mémoire une fois chargés (avant : gardés jusqu'à la reprise)."""
    import time as _time
    from taxonero import worker as wm
    from taxonero.config import Settings
    from taxonero.worker import Worker

    class Slow:
        timings = {}

        def analyze(self, *a, **k):
            return []

    def slow_load(self):
        _time.sleep(3.0)
        with self._analyzer_lock:
            self._analyzer, self._device = Slow(), "cpu"
            return self._analyzer

    w = Worker(Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off", gbif_local=False))
    for step in ("scan", "reconcile", "apply_skips", "refresh_taxa", "refresh_places", "prune", "sync", "prefetch", "backup"):
        monkeypatch.setattr(w, step, lambda *a, **k: 0)
    monkeypatch.setattr(w, "check", lambda: True)
    monkeypatch.setattr(wm.Worker, "analyzer", property(slow_load))
    monkeypatch.setattr(wm.hardware, "release", lambda d: None)
    w.db.execute("INSERT INTO asset (asset_id, file_name, taken_at) VALUES ('p', 'p.jpg', '2024-01-01')")
    w.db.commit()
    assert w.start_manual()
    _time.sleep(0.3)
    assert w.progress["step"] == "models"
    w.pause_analysis(True)
    _time.sleep(1.5)
    assert not w.progress["running"]          # le cycle a fini sans attendre le chargement
    _time.sleep(2.0)
    assert not w.models_loaded                # chargés puis retirés aussitôt

