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
    ranked = rerank(cands, None, None, None)  # tx=None: weights already set, no network
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
    whole, part = Box(0, 0, 1, 1, .6, "animal"), Box(.2, .2, .4, .4, .5, "animal")   # contained in the other one
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


# -- overlapping detections, detector merging, auto-confirmation, HTML cleanup ---------

from taxonero.pipeline import Analysis, Decision, dedupe, merge_boxes
from taxonero.wiki import sanitize
from taxonero.autoconfirm import should_auto_confirm


def _an(box, key, conf, genus="G", runner_up=0.0, plaus="green", rank="species"):
    cands = [_c(f"{genus} sp{key}", conf, plaus, genus=genus, key=key), _c("Other x", runner_up, key=999)]
    cands[0].posterior, cands[1].posterior = conf, runner_up
    return Analysis(box, Decision(key, f"{genus} sp{key}", rank, conf, conf - runner_up, plaus, cands))


def test_dedupe_same_subject_overlapping():
    big, inner = Box(.1, .1, .6, .6, .9, "animal"), Box(.2, .2, .3, .3, .5, "animal")
    assert len(dedupe([_an(big, 1, .9), _an(inner, 1, .8)])) == 1             # same species, nested
    assert len(dedupe([_an(big, 1, .9), _an(inner, 2, .8, genus="H")])) == 2  # butterfly on a flower
    far = Box(.8, .8, .15, .15, .9, "animal")
    assert len(dedupe([_an(big, 1, .9), _an(far, 1, .9)])) == 2              # deux individus distincts


def test_merge_boxes_prefers_megadetector_and_full_frame():
    md = [Box(.1, .1, .3, .3, .9, "animal", "Animalia", "md")]
    ov = [Box(.12, .12, .28, .28, .6, "animal", "Animalia", "owl"),   # doublon de MD
          Box(.6, .6, .3, .3, .5, "plant", "Plantae", "owl"),
          Box(0, 0, .95, .95, .4, "plant", "Plantae", "owl")]        # covers everything: scene
    out = merge_boxes(md, ov)
    assert [b.source for b in out] == ["md", "owl"]                  # the full frame overlaps MD: dropped
    assert merge_boxes([], [Box(0, 0, .95, .95, .4, "plant", "Plantae", "owl")])[0].area == 1.0


def test_auto_confirm_rule():
    b = Box(.1, .1, .3, .3, .9, "animal", "Animalia", "md")
    assert should_auto_confirm(_an(b, 1, .9), .85, .05)
    assert not should_auto_confirm(_an(b, 1, .9, runner_up=.08), .85, .05)       # other credible candidate
    assert not should_auto_confirm(_an(b, 1, .9, plaus="amber"), .85, .05)       # not observed nearby
    assert not should_auto_confirm(_an(b, 1, .8), .85, .05)                      # below the threshold
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
    md[0].score = .95  # confident animal (e.g. butterfly on the flower): kept
    assert [b.label for b in merge_boxes(md, flower)][0] == "animal"


def test_dedupe_merges_fragments_but_not_neighbours():
    head = Box(.69, .39, .12, .21, .88, "animal", None, "md")
    tail = Box(.70, .62, .11, .26, .43, "animal", "Animalia", "owl")       # same snake, unsure
    out = dedupe([_an(head, 1, .99, genus="Natrix"), _an(tail, 2, .64, genus="Natrix")])
    assert len(out) == 1 and out[0].box.y + out[0].box.h >= .87              # enclosing box
    toad1, toad2 = Box(.65, .28, .33, .25, .91, "animal"), Box(.05, .28, .32, .27, .95, "animal")
    assert len(dedupe([_an(toad1, 1, .85, genus="Bufo"), _an(toad2, 1, .85, genus="Bufo")])) == 2


def test_image_quality_prefers_sharp_colour_over_grey():
    import numpy as np
    from PIL import Image
    from taxonero.pipeline import image_quality
    rng = np.random.default_rng(0)
    colour = Image.fromarray((rng.random((128, 128, 3)) * 255).astype("uint8"))
    grey = colour.convert("L").convert("RGB")                    # same sharpness, without colour (infrared)
    flat = Image.new("RGB", (128, 128), (120, 120, 120))          # neither colour nor detail
    assert image_quality(colour) > image_quality(grey) > image_quality(flat)


def test_range_legacy_entries_are_dropped():
    import sqlite3
    from taxonero import ranges
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE http_cache (url TEXT PRIMARY KEY, body TEXT, fetched REAL)")
    db.execute("INSERT INTO http_cache VALUES ('range:2:123', '{}', 1e12)")
    db.execute("INSERT INTO http_cache VALUES ('https://api.gbif.org/x', '{}', 1e12)")
    assert ranges.migrate_legacy(db) == 1                                   # downloaded again in the new format
    assert db.execute("SELECT url FROM http_cache").fetchall() == [("https://api.gbif.org/x",)]


def test_range_cells_snap_to_exact_gbif_grid():
    """Measured centres (vertices rounded by GBIF: ±0.45 unit) -> exact column and half-row, and vertical
    origin of the tile, even when it falls near a half-row."""
    import random
    from taxonero import ranges
    sx, hy = ranges.grid(400)
    rng = random.Random(1)
    for y0 in (0.0, 3.1, hy - 0.2, 2 * hy - 0.1):
        truth = [(i, h) for i in range(0, 300, 7) for h in range(i % 2, 600, 2 * 3)]
        noisy = [(i * sx + rng.uniform(-.45, .45), y0 + h * hy + rng.uniform(-.45, .45)) for i, h in truth]
        got_y0, idx = ranges.snap(noisy, 400)
        shift = round((got_y0 - y0) / hy)                                    # origin equivalent up to one step
        assert abs(got_y0 - shift * hy - y0) < 0.1
        assert idx == [(i, h - shift) for i, h in truth]


def test_domestic_detection():
    from taxonero.pipeline import is_domestic
    b = Box(.2, .2, .5, .5, .9, "animal", None, "md")
    dog_hint = Box(.22, .2, .5, .52, .6, "domestic", "Canidae", "owl")
    wolf = _an(b, 1, .9, genus="Canis")
    wolf.decision.candidates[0].lineage["family"] = "Canidae"
    assert is_domestic(wolf, [dog_hint])                    # Canis + "a dog" on the subject: dog
    assert not is_domestic(wolf, [])                        # Canis without a hint: wild wolf
    far = Box(.8, .8, .15, .15, .6, "domestic", "Canidae", "owl")
    assert not is_domestic(wolf, [far])                     # hint elsewhere in the image
    cat = _an(b, 2, .9, genus="Felis"); cat.decision.name = "Felis catus"
    assert is_domestic(cat, [])                             # species that is only domestic


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
    # A saved value that became invalid is ignored, the others apply.
    assert apply_overrides(s, '{"auto_confirm": "always", "interval_minutes": 30}') == {"interval_minutes": 30}
    assert s.interval_minutes == 30 and s.auto_confirm == "rule"


def test_prune_pending_drops_extinct_and_redecides(tmp_path):
    import json
    from taxonero import store
    from taxonero.config import Settings
    from taxonero.curation import prune_pending

    class FakeTx:
        def fossil_only(self, key, lat=None, lon=None):
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
    """A candidate excluded by a setting comes back when the setting is disabled."""
    import json
    from taxonero import store
    from taxonero.config import Settings
    from taxonero.curation import prune_pending

    class FakeTx:
        def fossil_only(self, key, lat=None, lon=None):
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
    """Taxonomy HTTP client that always answers status, and counts the calls."""
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
    assert tx.http.calls == 1                              # a single request: the outage is remembered
    tx.http = _down_gbif(404)
    with pytest.raises(httpx.HTTPError):
        tx.get("https://api.gbif.org/v1/species/1")        # the other services are still queried
    assert tx.http.calls == 1
    slow = Taxonomy(store.connect(":memory:"))             # automatic analysis: retries unchanged
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
        rerank(cands, Tx(), 45.0, 5.0)                     # automatic analysis: the error goes up, photo retried
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
    """In-memory Immich, for Syncer: photos, tags, and a deleted photo."""
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
    # Nothing changed: the next pass reads no photo (except the missing one, which the worker hides).
    db.execute("UPDATE asset SET missing_at = 1 WHERE asset_id = 'gone'")
    im.gets = 0
    assert Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all(full=False).assets == 0 and im.gets == 0
    # A correction: only the photo concerned is read again.
    db.execute("UPDATE detection SET taxon_key = 2 WHERE asset_id = 'a'")
    assert Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all(full=False).assets == 1 and im.gets == 2   # read + read again before the description


def test_sync_deletes_tags_that_became_useless(tmp_path):
    from taxonero.sync import Syncer
    db = _sync_db(tmp_path, ["a", "b"])
    im = FakeImmich(["a", "b"])
    Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all()
    im.upsert_tags(["Perso/Vacances"])   # tag de l'utilisateur : intouchable
    db.execute("UPDATE detection SET taxon_key = 2")   # wrong species corrected on all photos
    st = Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all(full=False)
    assert st.tags_deleted >= 2   # the species and its genus, now empty
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
        pytest.skip("GPU present")
    assert hardware.resolve("auto")[0] == "cpu" and hardware.resolve("cuda")[0] == "cpu"
    assert hardware.onnx_providers("cpu") == ["CPUExecutionProvider"]
    assert Settings(immich_url="http://x", immich_api_key="k").device == "auto"
    with pytest.raises(ValueError):
        Settings(immich_url="http://x", immich_api_key="k", device="gpu")


def test_gpu_that_crashes_on_first_use_falls_back_to_cpu(monkeypatch):
    """A GPU that says it is available but crashes at the first computation (Iris Xe with PyTorch XPU): CPU, and the
    program's process survives (the test computation runs separately)."""
    from taxonero import hardware
    assert hardware._works("cpu")                       # the test computation itself is right
    monkeypatch.setattr(hardware, "PROBE", "import os; os.abort()")   # abrupt crash of the test process
    monkeypatch.setattr(hardware, "_available", lambda d: d == "xpu")
    hardware.resolve.cache_clear()
    try:
        assert hardware.resolve("auto") == ("cpu", "gpu_broken")
        assert hardware.resolve("xpu") == ("cpu", "gpu_broken")
    finally:
        hardware.resolve.cache_clear()


def test_full_precision_on_every_device():
    """Same results on GPU and CPU: no TF32, neither on the PyTorch side nor on the ONNX Runtime side."""
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
    from taxonero import megadetector   # no ONNX: MegaDetector in PyTorch, without download here
    monkeypatch.setattr(megadetector, "weights", lambda *a, **k: tmp_path / "w.pt")
    monkeypatch.setattr(megadetector, "TorchDetector", lambda *a, **k: type("D", (), {"providers": ["PyTorch"]})())
    monkeypatch.setattr(hardware, "resolve", lambda wanted: ("cuda", "gpu_found") if wanted == "auto" else ("cpu", "cpu_chosen"))
    monkeypatch.setattr(hardware, "full_precision", lambda: None)
    monkeypatch.setattr(hardware, "release", lambda d: None)
    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, detector_path=tmp_path / "absent.onnx")
    w = worker.Worker(s)
    assert s.device == "auto" and w.analyzer.classifier.device == "cuda"   # GPU by default
    first = w.analyzer
    assert w.analyzer is first                                             # no useless reload
    s.device = validate_overrides(s, {"device": "cpu"})["device"]
    assert w.analyzer is not first and w.analyzer.classifier.device == "cpu"


def test_ui_catalogs_match_french():
    """Each language catalogue: same keys as French, same {x} variables, same HTML tags,
    and the plural forms of the language (CLDR)."""
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
    assert tidy_name("UK") == "UK"                         # short acronym: left as is
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
    assert ov.calls == 1                                   # 'always' by default


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
    assert blocked_groups(db, s) == {"arachnida", "pisces"}       # by default: measured weak on the dev set
    spider = {"scientific_name": "Araneus diadematus", "lineage": {"class": "Arachnida", "kingdom": "Animalia"}}
    store.save_taxon(db, 1, _json.dumps(spider))
    db.execute("INSERT INTO asset (asset_id) VALUES ('a')")
    for i in range(GROUP_MIN_REVIEWS):                            # all reviews right: the group is unblocked
        db.execute("INSERT INTO detection (asset_id, x, y, w, h, source, status, confirmed_by, taxon_key, taxon_rank, "
                   "model_key, model_rank, model_conf, created_at) VALUES ('a', 0, 0, 1, 1, 'auto', 'confirmed', 'user', "
                   "1, 'species', 1, 'species', 0.95, 0)")
    assert blocked_groups(db, s) == {"pisces"}
    why = auto_blockers("species", "green", 0.99, "x", 0.0, "animal", 0.85, 0.05, "pisces", {"pisces"})
    assert why == [{"code": "group_unreliable", "group": "pisces"}]


def test_batched_analysis_isolates_a_broken_photo(tmp_path, monkeypatch):
    """On GPU, photos go in batches: an unreadable photo makes the batch fail, which is then retried
    photo by photo; only the unreadable ones are counted as failed."""
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
    assert w.analyze() == 10 and Batching.calls == 2               # 8 + 2 (GPU_BATCH by default)
    r = {x["asset_id"]: x for x in w.db.execute("SELECT * FROM asset")}
    assert r["p3"]["fail_count"] == 1 and r["p3"]["analyzed_at"] is None
    assert all(r[f"p{i}"]["analyzed_at"] for i in range(10) if i != 3)
    assert w.progress["done"] == 10
    s.gpu_batch = 4                                                  # adjustable in Settings
    w.db.execute("UPDATE asset SET analyzed_at = NULL, fail_count = 0")
    w.db.commit()
    Batching.calls = 0
    assert w.analyze() == 10 and Batching.calls == 3               # lots de 4 + 4 + 2


def test_higher_ranks_take_gbif_plural_names():
    """Cached higher rank: GBIF names (plural: "Oiseaux") replace Wikidata labels
    (article titles in the singular: "Oiseau"); languages without a GBIF name keep theirs."""
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
    # RAW analysed before the rule: its pending automatic detection is removed, it leaves the queue.
    w.db.execute("INSERT INTO detection (asset_id, source, status, created_at) VALUES ('raw', 'auto', 'pending', 0)")
    w.db.execute("UPDATE asset SET analyzed_at = 1 WHERE asset_id = 'raw'")
    w.db.commit()
    assert w.apply_skips() == 1
    twins = {r["asset_id"]: r["twin_of"] for r in w.db.execute("SELECT asset_id, twin_of FROM asset")}
    assert twins == {"jpg": None, "raw": "jpg", "alone": None, "other": None}   # RAW alone or from another shot: analysed
    queued = {r["asset_id"] for r in w.db.execute(f"SELECT asset_id FROM asset WHERE {w.queue_sql()[0]}", w.queue_sql()[1])}
    assert queued == {"jpg", "alone", "other"} and w.pending_analysis() == 3
    s.skip_raw_twins = False                    # option disabled: the RAW is analysed too
    assert w.pending_analysis() == 4
    s.skip_raw_twins = True
    # The JPG confirmed: the RAW receives the same tags in Immich.
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
        """Immich: 'family' has one person (recognised after import), 'bird' none."""
        def iter_assets(self, updated_after=None, size=250, with_exif=True, kind="IMAGE", with_people=False):
            for aid, n in (("family", 1), ("bird", 0)):
                yield {"id": aid, "type": "IMAGE", "people": [{"id": "p"}] * n if with_people else []}

    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off", analyze_videos=False)
    w = Worker(s)
    w.client = Client()
    for aid in ("family", "bird"):
        w.db.execute("INSERT INTO asset (asset_id, kind) VALUES (?, 'IMAGE')", (aid,))   # base d'avant : people inconnu
    w.db.commit()
    assert w.reconcile() is not None   # people unknown: read again right away, without waiting for the next day
    assert w.reconcile() is None       # then only once a day
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
    assert clf.seen == 0                      # automatic analysis: nothing detected, nothing classified
    an.analyze(Image.new("RGB", (64, 48)))
    assert clf.seen == 1                      # Identify page: the whole image


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
    """rerank prepares in parallel exactly what the normal path asks next (no extra request),
    and ignores the candidates below MIN_CANDIDATE_SCORE (at least MIN_CANDIDATES kept)."""
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
    drop = lambda c, lat=None, lon=None: False   # like curation.candidate_filter
    drop.fossils = True
    scores = [0.6, 0.2, 0.15, 0.03, 0.005]
    cands = [Candidate(n, s, {"kingdom": "Animalia"}) for n, s in zip(keys, scores)]
    after = []
    tx.prefetch_candidates = (lambda f: lambda *a, **k: (f(*a, **k), after.append(len(seen))))(tx.prefetch_candidates)
    out = rerank(cands, tx, 45.2, 5.7, drop=drop)
    assert {c.name for c in out} == set(list(keys)[:4])          # Felis catus (0.5 %) ignored
    assert len(seen) == after[0], seen[after[0]:]                # nothing more after the prefetch
    assert len(seen) == len(set(seen))                           # no duplicate request


def test_local_backbone(tmp_path):
    """Local GBIF taxonomy built from a mini archive in the GBIF format: same responses as the API
    (synonym -> accepted name, kingdom, record), search by common name, and Taxonomy only asks the network for
    what stays with the API (common names, IUCN)."""
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

    assert bb.match("vulpes VULPES")["usageKey"] == 5219243                       # case ignored
    m = bb.match("Canis vulpes", kingdom="Animalia")
    assert m["usageKey"] == 9999001 and m["acceptedUsageKey"] == 5219243 and m["status"] == "SYNONYM"
    assert bb.match("Morus") is None                                              # homonymes : l'API tranche
    assert bb.match("Morus", kingdom="Plantae")["usageKey"] == 7000001
    assert bb.match("Vulpes", rank="species") is None and bb.match("Inconnu inconnu") is None
    sp = bb.species(5219243)
    assert sp["canonicalName"] == "Vulpes vulpes" and sp["authorship"] == "(Linnaeus, 1758)" and sp["species"] == "Vulpes vulpes"
    assert sp["family"] == "Canidae" and sp["rank"] == "SPECIES"
    assert [h["gbif_key"] for h in bb.search("renard")] == [5219243] and bb.search("renard")[0]["common"] == "Renard roux"
    assert bb.search("Vulp")[0]["gbif_key"] == 5219243                           # species before genus

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
    """Searched groups: MegaDetector skipped without animals, OWLv2 boxes of unticked groups ignored,
    subject identified in an unticked group dropped before the GBIF check (taxonomy None here)."""
    from PIL import Image
    from taxonero.pipeline import Analyzer, Candidate

    class Fixed:
        def __init__(self, boxes): self.boxes, self.calls = boxes, 0
        def detect(self, image): self.calls += 1; return list(self.boxes)

    class Clf:   # each crop: a fox, except the plant boxes (a flower)
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
    # MegaDetector box recognised as a plant, plants not searched: dropped
    md2 = Fixed([Box(.5, .5, .4, .4, .95, "animal", None, "md")])
    assert Analyzer(md2, Clf(), None).analyze(image, groups=frozenset({"animal"})) == []


def test_microbe_group_same_in_python_and_sql_and_off_by_default(tmp_path):
    """Bacteria and microscopic protists: a separate group, not searched by default; brown algae and slime moulds
    stay with plants. The SQL rule (detections in the database) follows the Python rule (analysis)."""
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
    assert drop_unwanted(w.db, s) == 2                                  # plant and fungus awaiting review
    assert left() == [("auto", "confirmed", "plant"), ("auto", "pending", "animal"), ("manual", "pending", "plant")]
    s.detection = "manual"
    assert drop_unwanted(w.db, s) == 1 and w.pending_analysis() == 0    # plus d'analyse automatique
    assert left() == [("auto", "confirmed", "plant"), ("manual", "pending", "plant")]


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="ExifTool absent")
def test_local_folder_source_end_to_end(tmp_path):
    """"Local folder" source: photos read (date, GPS), thumbnails, then tags and description written in an
    XMP file next to the photo, and removed when the detection is rejected. The photo is never modified."""
    import json
    import shutil
    from pathlib import Path
    from taxonero import store
    from taxonero.config import Settings
    from taxonero.pipeline import Analysis, Box, Candidate, Decision
    from taxonero.worker import Worker

    photos = Path(__file__).parent.parent / "dev" / "photos"
    src = photos / "inat_141438356.jpg"                    # GPS west of Greenwich: negative longitude
    root = tmp_path / "photos"
    (root / "2024" / "sortie").mkdir(parents=True)
    shutil.copy(src, root / "2024" / "sortie" / "busard.jpg")
    (root / "@eaDir").mkdir()
    shutil.copy(src, root / "@eaDir" / "ignore.jpg")       # Synology technical folder: ignored
    before = (root / "2024" / "sortie" / "busard.jpg").read_bytes()
    import os, time as _time
    old = _time.time() - 3600   # photo changed an hour ago (otherwise left alone for 10 min, as with Immich)
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
    assert w.scan() == 0                                   # nothing changed: nothing read again
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
    assert (root / "2024" / "sortie" / "busard.jpg").read_bytes() == before   # photo untouched
    w.db.execute("UPDATE detection SET status = 'rejected'")
    w.db.commit()
    w.sync()
    got = w.client.get_asset(aid)
    assert got["tags"] == [] and "Busard" not in got["exifInfo"]["description"]
    shutil.copy(src, root / "autre.jpg")
    (root / "2024" / "sortie" / "busard.jpg").unlink()
    w.reconcile(force=True)
    assert w.db.execute("SELECT missing_at FROM asset WHERE asset_id = ?", (aid,)).fetchone()[0] is not None   # deleted: hidden
    w.client.close()


def _darktable_library(path, images):
    """library.db reduced to the tables read (film_rolls, images): [(folder, file, version)]."""
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
    """.lrcat catalog reduced to the tables read: [(root, subfolder, name, extension)], one row per image
    (a virtual copy repeats the same file)."""
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
    """Set then remove a tag on the photo file_name of the source; return the sidecars written."""
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
    assert all(p.read_bytes() == b for p, b in before.items())   # photos untouched
    return [p.name for p in written]


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="ExifTool absent")
def test_darktable_source(tmp_path):
    """darktable: photos of library.db (database never modified), sidecars photo.jpg.xmp and photo_01.jpg.xmp of a
    duplicate, darktablerc settings checked."""
    import shutil
    from pathlib import Path
    from taxonero.sources.darktable import Darktable
    src_photo = Path(__file__).parent.parent / "dev" / "photos" / "inat_141438356.jpg"
    photos = tmp_path / "photos"
    photos.mkdir()
    shutil.copy(src_photo, photos / "busard.jpg")
    shutil.copy(src_photo, photos / "autre.jpg")   # in the folder, not in the library: ignored
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
    assert [a["originalFileName"] for a in src.iter_assets()] == ["busard.jpg"]   # missing file: ignored
    assert _tag_roundtrip(src, "busard.jpg") == ["busard.jpg.xmp", "busard_01.jpg.xmp"]
    assert (conf / "library.db").read_bytes() == library
    src.close()
    assert not Darktable(tmp_path / "absent.db", tmp_path / "data").check()


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="ExifTool absent")
def test_lightroom_source(tmp_path):
    """Lightroom Classic: photos of the catalog (virtual copy counted once), photo.xmp next to a RAW,
    photo.jpg.xmp for a JPEG (reported: Lightroom does not read it), catalog locked by an open Lightroom."""
    import shutil
    import sqlite3
    from pathlib import Path
    from taxonero.sources.lightroom import CatalogLocked, Lightroom
    src_photo = Path(__file__).parent.parent / "dev" / "photos" / "inat_141438356.jpg"
    photos = tmp_path / "Photos"
    (photos / "2024").mkdir(parents=True)
    shutil.copy(src_photo, photos / "2024" / "busard.nef")   # JPEG named .nef: ExifTool reads its content
    shutil.copy(src_photo, photos / "2024" / "busard.jpg")
    catalog = tmp_path / "Lightroom Catalog.lrcat"
    root = photos.as_posix() + "/"
    _lightroom_catalog(catalog, [(root, "2024/", "busard", "nef"), (root, "2024/", "busard", "nef"), (root, "2024/", "busard", "jpg")])
    (tmp_path / "data").mkdir()
    src = Lightroom(catalog, tmp_path / "data", dry_run=False)
    assert src.check() and src.warnings == ["1 JPEG, TIFF, HEIC or DNG photo(s): Lightroom reads their keywords in the "
                                            "file itself, never modified here; only RAW files receive them in Lightroom"]
    assert sorted(a["originalFileName"] for a in src.iter_assets()) == ["busard.jpg", "busard.nef"]
    assert _tag_roundtrip(src, "busard.nef") == ["busard.xmp"]
    assert _tag_roundtrip(src, "busard.jpg") == ["busard.jpg.xmp"]
    lock = sqlite3.connect(catalog)   # Lightroom open: catalog locked for exclusive writing
    lock.execute("PRAGMA locking_mode = EXCLUSIVE")
    lock.execute("BEGIN EXCLUSIVE")
    with pytest.raises(CatalogLocked):
        list(src.iter_assets())
    assert src.check() and "close Lightroom" in src.warnings[0]
    lock.close()
    src.close()


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="ExifTool absent")
def test_rawtherapee_and_rapidraw_sources(tmp_path):
    """RawTherapee: sidecar name and XMP sync read in its options file. RapidRAW: photo.xmp (or
    existing photo.XMP), sync read in settings.json."""
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
    assert rr.check() and any("XMP sync" in w for w in rr.warnings)
    settings.write_text(json.dumps({"enableXmpSync": True}))
    assert rr.check() and rr.warnings == []
    assert _tag_roundtrip(rr, "busard.jpg") == ["busard.xmp"]
    (photos / "busard.xmp").rename(photos / "busard.XMP")
    assert _tag_roundtrip(rr, "busard.jpg") == ["busard.XMP"]
    rr.close()


def test_sources_by_edition():
    """Photo applications: on the desktop only; Lightroom Classic on Windows and macOS (on Linux: its
    catalog paths could not be opened)."""
    import sys
    from taxonero.sources import available
    srv = {o["id"]: o for o in available("server")}
    assert [k for k, o in srv.items() if o["available"]] == ["immich", "local"]
    desk = {o["id"]: o for o in available("desktop")}
    assert desk["darktable"]["available"] and desk["rawtherapee"]["available"] and desk["rapidraw"]["available"]
    assert desk["lightroom"]["available"] == (sys.platform in ("win32", "darwin"))


def test_manual_location_reranks_pending_and_survives_rescan(tmp_path):
    """Place entered by hand: kept as long as the source has no GPS (a real GPS replaces it), and the detections
    awaiting review are reclassified with this place from the saved model scores."""
    import json
    from taxonero import store
    from taxonero.config import Settings
    from taxonero.curation import relocate
    from taxonero.worker import Worker

    s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, auto_confirm="off", gbif_local=False)
    w = Worker(s)
    store.upsert_asset(w.db, {"id": "a", "checksum": "c", "exifInfo": {}})
    w.db.execute("UPDATE asset SET lat = 45.2, lon = 5.7, geo_user = 1 WHERE asset_id = 'a'")
    store.upsert_asset(w.db, {"id": "a", "checksum": "c", "exifInfo": {"city": "x"}})        # rescan without GPS: kept
    assert tuple(w.db.execute("SELECT lat, lon, geo_user FROM asset").fetchone()) == (45.2, 5.7, 1)
    store.upsert_asset(w.db, {"id": "a", "checksum": "c", "exifInfo": {"latitude": 48.0, "longitude": 2.0}})
    assert tuple(w.db.execute("SELECT lat, lon, geo_user FROM asset").fetchone()) == (48.0, 2.0, None)   # vrai GPS

    # Reclassification: the model hesitates (red fox 0.5, swift fox 0.5); only the red one is present near the point.
    seen = []
    class Tx:
        def match(self, name, kingdom=None, enrich=True, rank=None):
            from taxonero.taxonomy import Taxon
            return Taxon(1 if "vulpes" in name else 2, name, "species", {"kingdom": "Animalia", "genus": "Vulpes"})
        def geo(self, key, lat, lon):
            seen.append((lat, lon))
            return ("green", 1.0) if key == 1 else ("red", 0.02)
        def prefetch_candidates(self, *a, **k): pass
        def fossil_only(self, key, lat=None, lon=None): return False
    cands = [{"name": n, "gbif_key": k, "p": .5, "p0": .5, "model_score": .5, "plausibility": "unknown",
              "lineage": {"kingdom": "Animalia"}} for n, k in (("Vulpes velox", 2), ("Vulpes vulpes", 1))]
    w.db.execute("INSERT INTO detection (asset_id, source, status, candidates, candidates_out, created_at) "
                 "VALUES ('a', 'auto', 'pending', ?, '[]', 0)", (json.dumps(cands),))
    w.db.execute("INSERT INTO detection (asset_id, source, status, taxon_key, candidates, created_at) "
                 "VALUES ('a', 'auto', 'confirmed', 2, ?, 0)", (json.dumps(cands),))
    store.save_taxon(w.db, 1, json.dumps({"scientific_name": "Vulpes vulpes", "rank": "species", "lineage": {}, "v": 99}))
    w.db.commit()
    assert relocate(w.db, Tx(), s, ["a"]) == 1                                  # the confirmed one does not move
    d = w.db.execute("SELECT taxon_key, plausibility FROM detection WHERE status = 'pending'").fetchone()
    assert tuple(d) == (1, "green") and seen and seen[0] == (48.0, 2.0)
    assert w.db.execute("SELECT taxon_key FROM detection WHERE status = 'confirmed'").fetchone()[0] == 2


def test_pipelined_analysis_overlaps_models_and_saving(tmp_path):
    """Pipelined path (infer_many in the "gpu" thread, decide_many and saving in the main thread):
    all photos analysed, an unreadable photo dropped without blocking the others, models never called
    from the main thread, time per stage recorded."""
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
    assert threads and all(t.startswith("gpu") for t in threads)          # models: "gpu" thread only
    stages = w.models_info()["stages"]
    assert {"download", "decode", "megadetector", "bioclip", "geo", "save"} <= stages.keys()


def test_desktop_edition_session_and_source(tmp_path):
    """Desktop edition: no password, but one token per launch (cookie set by /?t=…), local host
    only; source and writing adjustable from the dashboard, paths checked. Server edition unchanged."""
    from fastapi.testclient import TestClient
    from taxonero.config import Settings
    from taxonero.desktop import prepare
    from taxonero.web import create_app
    from taxonero.worker import Worker

    s = prepare(Settings(data_dir=tmp_path))
    assert s.edition == "desktop" and s.dashboard_host == "127.0.0.1" and s.detector_path.parent == tmp_path / "models"
    app = create_app(s, Worker(s), token="launch-token")
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    assert c.get("/api/status").status_code == 401
    assert c.get("/?t=faux", follow_redirects=False).status_code == 401
    r = c.get("/?t=launch-token", follow_redirects=False)
    assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    assert c.get("/api/status").status_code == 200                                   # cookie kept by the client
    assert c.get("/api/status", headers={"host": "evil.example"}).status_code == 403  # DNS rebinding
    photos = tmp_path / "photos"
    photos.mkdir()
    r = c.put("/api/source", json={"source": "local", "path": str(photos), "dry_run": False})
    assert r.status_code == 200 and r.json()["current"] == "local" and r.json()["dry_run"] is False
    assert c.put("/api/source", json={"source": "local", "path": "not/absolute"}).status_code == 422
    assert c.put("/api/source", json={"source": "picasa"}).status_code == 422
    assert c.put("/api/source", json={"source": "darktable", "path": str(photos)}).status_code == 422   # not a library.db
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
    assert cs.get("/api/status").status_code == 401                                   # password still required
    assert cs.put("/api/source", json={"dry_run": False}, auth=("nature", "secret")).status_code == 422   # DRY_RUN : environnement
    assert cs.put("/api/source", json={"source": "darktable", "path": str(library)}, auth=("nature", "secret")).status_code == 422   # desktop only


def test_desktop_bootstrap_gpu_detection(monkeypatch):
    """Desktop launcher: PyTorch variant chosen according to the GPU (NVIDIA depending on the driver, AMD, Intel Arc,
    Apple Silicon), choice forced by TAXONERO_GPU, CPU otherwise."""
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
    assert case("linux", "x86_64", names=["AMD (amdgpu)"], kfd=False) == "cpu"     # no ROCm compute possible
    assert case("win32", "AMD64", names=["AMD Radeon RX 7800 XT"]) == "amd-win"
    assert case("win32", "AMD64", names=["Intel(R) Arc(TM) B580 Graphics"]) == "xpu"
    assert case("linux", "x86_64", names=["Intel (xe)"]) == "xpu"
    assert case("linux", "x86_64", names=["Intel (i915)"]) == "cpu"                # old iGPU: no XPU
    assert case("darwin", "arm64") == "mps" and case("darwin", "x86_64") == "cpu"
    assert case("linux", "aarch64", driver=580.0) == "cu130"                       # NVIDIA Grace, DGX Spark
    assert case("linux", "aarch64", driver=550.0) == "cpu"                         # no CUDA 12 for ARM
    assert case("win32", "AMD64", driver=580.0, forced="cpu") == "cpu"
    assert set(b.TORCH) == {"cu130", "cu128", "rocm", "xpu", "cpu", "mps", "amd-win"}


def test_series_assign_and_smoothing(tmp_path):
    """Series: known camera, less than 2 min step by step; MegaDetector-style smoothing on the detections awaiting
    review only: isolated species -> dominant one (>= 4 photos, among its candidates), genus -> dominant species of
    that genus (>= 2), never a kingdom; confirmed ones untouched."""
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
    assert {sid[f"a{i}"] for i in range(1, 7)} == {"a1"} and sid["b1"] is None   # 20 min later: alone
    assert sid["c1"] is None and sid["c2"] is None                                  # unknown camera: never in a series

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
    det("a5", 2, "species")                  # isolated, the dominant one (Parus major, 4 photos) among its candidates
    det("a6", 3, "genus", c=(3,))            # genus Parus: refined to Parus major
    det("a6", 4, "kingdom", c=(4, 1))        # kingdom: never refined (a hen does not become a cat)
    db.commit()
    assert series.smooth(db, lambda k: store.load_taxon(db, k)) == 2
    got = {(r["asset_id"], r["model_key"]): (r["taxon_key"], r["series_n"]) for r in db.execute("SELECT * FROM detection")}
    assert got[("a5", 2)] == (1, 4) and got[("a6", 3)] == (1, 4) and got[("a6", 4)][0] == 4
    assert {r[0] for r in db.execute("SELECT model_key FROM detection WHERE smoothed = 1")} == {2, 3}   # record of the smoothing
    assert got[("a4", 1)] == (1, 4)          # confirmed: unchanged, its number of photos noted
    # Only 3 photos: not enough to replace an isolated species
    db.execute("UPDATE detection SET taxon_key = model_key, taxon_rank = CASE model_key WHEN 3 THEN 'genus' ELSE taxon_rank END")
    db.execute("UPDATE detection SET status = 'rejected' WHERE asset_id = 'a4'")
    db.commit()
    series.smooth(db, lambda k: store.load_taxon(db, k))
    assert db.execute("SELECT taxon_key FROM detection WHERE asset_id = 'a5'").fetchone()[0] == 2


def test_frame_support_flags_box_beside_subject():
    """Single subject: the kept species almost absent from the crop (seen on the whole photo only) blocks
    auto-confirmation; the signal follows the taxon (cleared if the decision changes species)."""
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
    assert db.execute("SELECT frame_support FROM detection").fetchone()[0] == 0.02      # same taxon: kept
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
    (((200, 1), (0, 1), (0, 1)), (None, None)),    # out of range
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
    assert {r[0] for r in db.execute("SELECT asset_id FROM asset_sync")} == {"a", "b"}   # "bad" will be retried
    for code in (401, 503):   # key refused, server down: no point insisting on every photo
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
    # Writing: desktop setting, left disabled if the source does not answer.
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
    path.chmod(0o644)     # database created before this rule
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
        store.save_taxon(db, 1, "{}")   # written without committing, then network outage
        raise OSError("GBIF en panne")
    monkeypatch.setattr(review, "relocate", half_written)
    with _web(s, w) as c:
        r = c.post("/api/assets/location", json={"asset_ids": [aid], "lat": 3, "lon": 4})
    assert r.status_code == 200 and r.json()["reclassified"] == 0
    w.db.execute("INSERT OR REPLACE INTO state VALUES ('x', '1')")   # another writer comes by
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
    assert not w.unload_idle() and w.models_loaded                  # used less than 5 min ago
    w._models_used -= 299
    assert not w.unload_idle()
    w._models_used -= 2
    s.models_idle_minutes = 0
    assert not w.unload_idle()                                      # 0: always kept
    s.models_idle_minutes = 5
    w._device = "cpu"
    assert not w.unload_idle()                                      # no GPU memory to give back
    w._device = "cuda"
    assert w.unload_idle() and not w.models_loaded and released == ["cuda"] and ref() is None
    assert w.models_info()["stages"]["detection"] == 1000           # measured times always shown


def test_wikipedia_found_through_name_or_synonym():
    """Wikidata item of the GBIF key without an article: article searched on the synonym (Ardea modesta / Ardea alba
    modesta), without taking the Latin name of the synonym as a common name. An ambiguous name (two items) is ignored."""
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
    """Wikidata overload (200 and {"error": …}): retry, and never cached like an empty response."""
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
    for k in range(10):                                          # information lines do not push out the error
        line(buf, logging.INFO, f"info {k}")
    assert [e["msg"] for e in buf.read()] == ["panne ***", "info 7", "info 8", "info 9"]
    assert "cle-secrete" not in (tmp_path / FILENAME).read_text()
    assert (tmp_path / FILENAME).stat().st_mode & 0o077 == 0

    for k in range(10):                                          # the last 4 warnings, file shrunk along the way
        line(buf, logging.WARNING, f"alerte {k}")
        line(buf, logging.INFO, f"info {k}")
    assert [e["msg"] for e in buf.read() if e["level"] != "INFO"] == [f"alerte {k}" for k in range(6, 10)]
    assert len((tmp_path / FILENAME).read_text().splitlines()) <= 8
    last = buf.kept[-1]["id"]
    with (tmp_path / FILENAME).open("a") as f:
        f.write('{"id": 1, "cut')                                # abrupt stop in the middle of a write

    again = LogBuffer(capacity=3, keep=4)                        # restart: kept, numbering goes on
    again.bind(s)
    assert [e["msg"] for e in again.read()] == [f"alerte {k}" for k in range(6, 10)]
    line(again, logging.ERROR, "nouvelle")
    assert again.read()[-1]["id"] > last

    cleared = again.read()[-1]["id"]                            # "Clear" button: everything, file included
    again.clear()
    assert again.read() == [] and not (tmp_path / FILENAME).exists()
    line(again, logging.WARNING, "after")
    assert [e["msg"] for e in again.read(cleared)] == ["after"]


class _UndoClient:
    """Simulated Immich for the removal: photos in memory, writes lost or refused on demand."""
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
    db.executemany("INSERT INTO written (kind, immich_id, asset_id, value, written_at) VALUES (?, ?, ?, ?, 1)", rows)
    db.commit()
    return db


def test_uninstall_rereads_lost_writes_and_leaves_user_text_alone(tmp_path):
    from taxonero import store
    from taxonero.sync import BLOCK_END, BLOCK_START, Syncer
    block = f"{BLOCK_START}\nx\n{BLOCK_END}"
    c = _UndoClient({"a": {"tags": ["t1", "perso"], "desc": f"Mon texte\n\n{block}"},
                     "b": {"tags": [], "desc": "  text without block  "}})   # block already removed by hand
    c.lose = 1                                                             # Immich loses the first write
    db = _undo_db(tmp_path, [("tag", "t1", "", "Nature"), ("tag_asset", "t1", "a", "Nature"),
                             ("description", "a", "a", None), ("description", "b", "b", None)])
    s = Syncer(db, c, write_gap=0)
    s.uninstall()
    assert c.assets["a"] == {"tags": ["perso"], "desc": "Mon texte"}
    assert c.assets["b"]["desc"] == "  text without block  "               # not rewritten
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
    with pytest.raises(RuntimeError, match="failures in a row"):
        Syncer(db, c, write_gap=0).uninstall()
    assert not c.writes                                                    # the tag is not deleted
    assert db.execute("SELECT count(*) FROM written").fetchone()[0] == n + 1
    assert store.get_state(db, store.WRITES_PAUSED) == "1"                 # the cycle sets nothing back in the meantime
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
    """A long first synchronisation cut short (dashboard setting, restart) does not start from scratch:
    each photo is recorded as soon as it is written."""
    from taxonero.sync import Syncer

    class Stop(BaseException):
        pass

    db = _sync_db(tmp_path, ["a", "b", "c"])
    im = FakeImmich(["a", "b", "c"])
    calls = []

    def checkpoint():   # 3 reads, 3 tags, then stop before the description of the 2nd photo
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
    """Photo marked as synchronised in the first round then refused on re-reading: picked up at the next cycle."""
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
    """A long synchronisation goes through stopping points without a "tick": /healthz must not think it frozen."""
    s, w = _local_worker(tmp_path)
    w.loop_started, w.last_activity = True, 0.0
    w._checkpoint()
    assert time.time() - w.last_activity < 5
    assert _web(s, w).get("/healthz").status_code == 200


def test_megadetector_checkpoint_hash_checked_before_loading(tmp_path, monkeypatch):
    """The full checkpoint is read with pickle: a file present but with another checksum is never loaded."""
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
    """A walk photographed every minute for 3 h is not a single series: 10 min at most per series.
    And even in a real burst, a confident species (92 %) is not replaced by a dominant one it gives at 3 %."""
    import json
    from taxonero import series, store
    db = store.connect(tmp_path / "s.sqlite")
    for i in range(180):   # one photo per minute, ~50 m apart
        db.execute("INSERT INTO asset (asset_id, taken_at, lat, lon, make, model) VALUES (?, ?, ?, ?, 'OM', 'OM-1')",
                   (f"p{i:03d}", f"2026-05-01T{8 + i // 60:02d}:{i % 60:02d}:00", 45 + i * 0.00045, 5.0))
    db.commit()
    series.assign(db)
    spans = db.execute("SELECT series_id, min(taken_at), max(taken_at), count(*) FROM asset GROUP BY series_id").fetchall()
    assert len(spans) == 17 and max(r[3] for r in spans) == 11   # 10 min at most: 11 photos (from 0 to 600 s)

    MERLE, ROUGEGORGE = 1, 2
    det = lambda aid, key, score, cands: db.execute(
        "INSERT INTO detection (asset_id, source, status, taxon_key, taxon_rank, score, candidates, model_key, created_at) "
        "VALUES (?, 'auto', 'pending', ?, 'species', ?, ?, ?, 0)", (aid, key, score, json.dumps(cands), key))
    for aid in ("p000", "p001", "p002", "p003"):
        det(aid, MERLE, 0.9, [{"gbif_key": MERLE, "p": 0.9}])
    det("p170", ROUGEGORGE, 0.92, [{"gbif_key": ROUGEGORGE, "p": 0.92}, {"gbif_key": MERLE, "p": 0.03}])   # 2 h 50 later
    det("p005", ROUGEGORGE, 0.92, [{"gbif_key": ROUGEGORGE, "p": 0.92}, {"gbif_key": MERLE, "p": 0.03}])   # same series
    db.commit()
    assert series.smooth(db, lambda k: {"lineage": {}}) == 0
    assert {r[0] for r in db.execute("SELECT taxon_key FROM detection WHERE asset_id IN ('p005', 'p170')")} == {ROUGEGORGE}
    # Credible candidate (the model hesitated): smoothing still applies.
    db.execute("UPDATE detection SET score = 0.55, candidates = ? WHERE asset_id = 'p005'",
               (json.dumps([{"gbif_key": ROUGEGORGE, "p": 0.55}, {"gbif_key": MERLE, "p": 0.35}]),))
    db.commit()
    assert series.smooth(db, lambda k: {"lineage": {}}) == 1
    assert db.execute("SELECT taxon_key, smoothed FROM detection WHERE asset_id = 'p005'").fetchone()[:] == (MERLE, 1)


def test_wrong_smoothing_from_before_is_undone(tmp_path):
    """Smoothing done by the old rule (3 h walk taken for a burst, without presmooth): the model
    proposal is restored, even if the photo is no longer in any series; a smoothing still justified stays."""
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
    # Wrongly smoothed in the past: 2 h 50 later (p170), and in another burst with a marginal blackbird (p030).
    for aid in ("p170", "p030"):
        det(aid, MERLE, 0.03, [{"gbif_key": ROUGEGORGE, "p": 0.92, "plausibility": "amber"}, {"gbif_key": MERLE, "p": 0.03}],
            model=ROUGEGORGE, model_conf=0.92, smoothed=1, series_n=4, plaus="green")
    # Rightly smoothed (the model hesitated): kept.
    det("p005", MERLE, 0.35, [{"gbif_key": ROUGEGORGE, "p": 0.55}, {"gbif_key": MERLE, "p": 0.35}],
        model=ROUGEGORGE, model_conf=0.55, smoothed=1, series_n=4)
    db.commit()
    series.assign(db)
    assert db.execute("SELECT series_id FROM asset WHERE asset_id = 'p170'").fetchone()[0] != "p000"
    for aid in ("p031", "p032", "p033", "p034"):   # rafale de p030 : quatre merles
        det(aid, MERLE, 0.9, [{"gbif_key": MERLE, "p": 0.9}])
    db.commit()
    assert series.smooth(db, lambda k: {"lineage": {}}) == 3   # 2 restored + p005 recorded with its previous proposal
    got = {r["asset_id"]: dict(r) for r in db.execute("SELECT * FROM detection WHERE asset_id IN ('p005', 'p030', 'p170')")}
    for aid in ("p030", "p170"):
        assert (got[aid]["taxon_key"], got[aid]["score"], got[aid]["plausibility"], got[aid]["smoothed"]) == (ROUGEGORGE, 0.92, "amber", None)
    assert (got["p005"]["taxon_key"], got["p005"]["smoothed"]) == (MERLE, 1)
    assert json.loads(got["p005"]["presmooth"])["taxon_key"] == ROUGEGORGE
    assert series.smooth(db, lambda k: {"lineage": {}}) == 0   # stable from one cycle to the next


def test_analysis_pause_stops_after_current_group_and_survives_restart(tmp_path, monkeypatch):
    """Pause button: the current group finishes, the next ones wait; run_once no longer analyses; the state is kept
    from one launch to the next; resuming starts a cycle."""
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
            w.analysis_paused = True                                  # pause requested during the first group
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
    report = w.run_once(manual=True)                                 # paused: nothing analysed, even by hand
    assert report["analyzed"] == 0 and Pausing.calls == 1

    d = prepare(Settings(data_dir=tmp_path / "bureau"))
    dw = Worker(d)
    started = []
    monkeypatch.setattr(dw, "start_manual", lambda: started.append(1) or True)
    c = TestClient(create_app(d, dw, token="t"), base_url="http://127.0.0.1:8765")
    c.get("/?t=t", follow_redirects=False)
    assert c.post("/api/analysis/pause", json={"paused": True}).json() == {"paused": True}
    assert c.get("/api/sync/status").json()["analysis_paused"] is True
    assert Worker(d).analysis_paused                                 # restart: still paused
    assert c.post("/api/analysis/pause", json={"paused": False}).json() == {"paused": False} and started == [1]
    assert not Worker(d).analysis_paused


def test_pause_and_resume_in_the_running_loop(tmp_path, monkeypatch):
    """Real loop: the pause stops the analysis in the middle of the queue (the rest waits), resuming starts right away a
    cycle that finishes the queue, even outside the time window."""
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
    assert done() == 0                                   # outside the time window: the loop does not analyse
    assert w.start_manual()
    for _ in range(500):
        if done() >= 5:
            break
        _time.sleep(0.02)
    w.pause_analysis(True)
    _time.sleep(0.5)
    paused_at = done()
    _time.sleep(0.5)
    assert 0 < paused_at < 60 and done() == paused_at    # nothing gets analysed any more
    w.pause_analysis(False)                              # cycle perhaps still running: restarted when it ends
    for _ in range(300):
        if done() == 60:
            break
        _time.sleep(0.02)
    assert done() == 60


def test_prefetch_stops_on_network_outage_with_one_line(tmp_path, monkeypatch, caplog):
    """Network down while storing locally: one warning line, without a trace, and the rest at the next cycle
    (before: a full trace per taxon)."""
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
    # Only the lines of this prefetch: a dev server left by the browser tests may log in the meantime.
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "local data" in r.getMessage()]
    assert len(calls) == 1 and len(warnings) == 1 and not warnings[0].exc_info
    assert "resumed at the next cycle" in warnings[0].getMessage()


def test_desktop_folder_picker_goes_through_the_server(tmp_path):
    """Desktop: "Browse…" goes through /api/desktop/pick (window.pywebview.api, created with new Function, is refused
    by the CSP); session token required; elsewhere, no button and 404."""
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
    assert c.post("/api/desktop/pick", json={"kind": "folder"}).status_code == 401   # without the token
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
    """Pause during model loading (download of several GB): the cycle does not wait for the end of the
    loading, and the models do not stay in memory once loaded (before: kept until resuming)."""
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
    assert not w.progress["running"]          # the cycle finished without waiting for the loading
    _time.sleep(2.0)
    assert not w.models_loaded                # loaded then removed right away



def test_local_folder_metadata_found_with_windows_paths(tmp_path, monkeypatch):
    """On Windows, ExifTool returns SourceFile with / instead of \\: date, camera and GPS must still
    be attached to the photo (otherwise no sorting by date, no series, no geographic check)."""
    from taxonero.sources import files
    from taxonero.sources.local import LocalFolder

    root = tmp_path / "C:\\Photos"          # simulated "Windows" path: \\ in the folder name
    (root / "sortie").mkdir(parents=True)
    (root / "sortie" / "a.jpg").write_bytes(b"x")
    monkeypatch.setattr(files, "WINDOWS", True)

    class FakeExifTool:
        def read(self, paths, tags):
            return [{"SourceFile": p.replace("\\", "/"), "DateTimeOriginal": "2024:05:01 08:30:00", "Make": "OM",
                     "Model": "OM-1", "GPSLatitude": 45.5, "GPSLongitude": 6.1} for p in paths]

        def close(self):
            pass

    data = tmp_path / "data"
    data.mkdir()
    src = LocalFolder(root, data)
    src.et = FakeExifTool()
    src.db.execute("INSERT INTO file VALUES (?, 'sortie/a.jpg', ?, ?, '{}')",   # read before the fix: empty
                   (src.asset_id("sortie/a.jpg"), *(lambda st: (st.st_size, st.st_mtime_ns))((root / "sortie" / "a.jpg").stat())))
    (a,) = list(src.iter_assets(updated_after="2000-01-01"))   # unchanged file, but read again
    assert a["exifInfo"]["dateTimeOriginal"] == "2024-05-01T08:30:00"
    assert (a["exifInfo"]["make"], a["exifInfo"]["latitude"]) == ("OM", 45.5)
    src.close()


def test_progress_of_tag_writing_and_folder_scan(tmp_path):
    """Dashboard progress: tag writing goes from 0 to 3 units per photo (read, tags, description), deleted photos
    included; a folder scan counts every file walked, unchanged ones too, against a total known in advance."""
    from taxonero.sources.local import LocalFolder
    from taxonero.sync import Syncer
    db = _sync_db(tmp_path, ["a", "b", "gone"])
    seen = []
    Syncer(db, FakeImmich(["a", "b"], gone=["gone"]), write_gap=0, settle_seconds=0, verify_rounds=1,
           progress=lambda done, total, phases: seen.append((done, total, phases))).sync_all(full=False)
    assert seen[0] == (0, 9, 3) and seen[-1] == (9, 9, 3)
    assert [d for d, _, _ in seen] == sorted(d for d, _, _ in seen)   # never goes back (second round not counted)

    root = tmp_path / "photos"
    (root / "sub").mkdir(parents=True)
    for name in ("a.jpg", "sub/b.jpg", "sub/notes.txt"):
        (root / name).write_bytes(b"x")
    data = tmp_path / "data"
    data.mkdir()
    src = LocalFolder(root, data)
    ticks = []
    src.on_file = lambda: ticks.append(1)
    assert src.count_files() == 2
    list(src.iter_assets(with_exif=False))
    assert len(ticks) == 2
    src.close()


def test_presence_from_one_faceted_search_per_area():
    """Presence around a point: one faceted GBIF search per area answers every candidate (absent = 0), for every
    photo of the area; a rank without a facet (subspecies) still uses the per-taxon search."""
    import httpx
    from taxonero import store
    from taxonero.taxonomy import Taxonomy

    class Http:
        calls: list = []
        def get(self, url, params=None):
            u = httpx.URL(str(url))
            Http.calls.append(u)
            if u.path.startswith("/v1/species/"):
                key = int(u.path.rsplit("/", 1)[1])
                body = {"key": key, "rank": "SUBSPECIES" if key == 3 else "SPECIES"}
            elif "facet" in u.params:
                body = {"count": 99, "facets": [{"field": "SPECIES_KEY", "counts": [{"name": "1", "count": 50}]},
                                                {"field": "GENUS_KEY", "counts": [{"name": "10", "count": 80}]}]}
            else:
                body = {"count": 7}
            return httpx.Response(200, json=body, request=httpx.Request("GET", str(u)))

    tx = Taxonomy(store.connect(":memory:"))
    tx.min_interval, tx.http = 0, Http()
    for k in (1, 2, 3):
        tx.get(f"https://api.gbif.org/v1/species/{k}")
    Http.calls.clear()
    assert tx.occurrences_near(1, 45.2, 5.1, 5.0) == 50
    assert tx.occurrences_near(2, 45.0, 4.8, 5.0) == 0             # absent from the area: no request of its own
    assert len(Http.calls) == 1 and "facet" in Http.calls[0].params
    assert tx.occurrences_near(3, 45.0, 5.0, 5.0) == 7             # subspecies: per-taxon search
    assert "taxonKey" in Http.calls[-1].params and len(Http.calls) == 2
    tx._areas.clear()
    assert tx.occurrences_near(1, 45.0, 5.0, 5.0) == 50 and len(Http.calls) == 2   # cached in the database


def test_two_phase_analysis_frees_the_gpu_before_the_geographic_check(tmp_path, monkeypatch):
    """run_once: the AI part ("ai" thread, its own connection) and the geographic check run at the same time; when
    the AI part is over the models are released and the check finishes alone. Same detections as the analysis in
    one go, two progress counters, nothing left waiting. An AI result kept across a restart is checked without
    running the models again."""
    import io as _io
    import threading
    import time
    from PIL import Image
    from taxonero.config import Settings
    from taxonero.pipeline import Analyzer, Candidate
    from taxonero.worker import Worker

    class Det:
        providers = ["CPUExecutionProvider"]
        def detect(self, image):
            return [Box(.1, .1, .5, .5, .95, "animal", None, "md")]
    class Clf:
        calls, slow = 0, 0.0
        def classify(self, images):
            Clf.calls += 1
            time.sleep(Clf.slow)
            return [[Candidate("Vulpes vulpes", .7, {"kingdom": "Animalia", "genus": "Vulpes"}),
                     Candidate("Vulpes velox", .2, {"kingdom": "Animalia", "genus": "Vulpes"})] for _ in images]
    def jpeg():
        b = _io.BytesIO(); Image.new("RGB", (300, 200), (90, 120, 60)).save(b, "JPEG"); return b.getvalue()

    def worker(name, n=5):
        s = Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path / name, auto_confirm="off",
                     gbif_local=False, open_detector=False)
        s.data_dir.mkdir()
        w = Worker(s, analyzer=Analyzer(Det(), Clf(), None))
        w.client.thumbnail = lambda aid, size="preview": jpeg()
        w.client.original = lambda aid: None   # no original: the subject is identified on the preview
        for i in range(n):
            w.db.execute("INSERT INTO asset (asset_id, file_name, taken_at, kind, people) VALUES (?, ?, ?, 'IMAGE', 0)",
                         (f"p{i}", f"p{i}.jpg", f"2024-01-{i + 1:02d}"))
        w.db.commit()
        return w
    dets = lambda w: sorted(tuple(r) for r in w.db.execute("SELECT asset_id, taxon_rank, round(score, 6), status FROM detection"))

    one = worker("one", 12)
    assert one.analyze() == 12

    two = worker("two", 12)
    for step in ("check", "scan", "reconcile", "apply_skips", "refresh_taxa", "refresh_places", "prune", "sync", "prefetch", "backup"):
        monkeypatch.setattr(two, step, lambda *a, **k: 0)
    monkeypatch.setattr(two, "check", lambda: True)
    unloads, overlap, threads = [], [], set()
    monkeypatch.setattr(two, "unload_models", lambda: unloads.append(two.progress.get("active")))
    real_decide, real_analyze = two.decide, two.analyze
    monkeypatch.setattr(two, "analyze", lambda *a, **k: threads.add(threading.current_thread().name) or real_analyze(*a, **k))
    seen = []
    def decide(*a, **k):
        n = real_decide(*a, **k)
        if two.progress.get("phases"):
            seen.append([p[:2] for p in two.progress["phases"].values()])
        if n and two.progress.get("active") == ["analyze", "geo"]:
            overlap.append(n)   # photos checked while the AI part was still running
        return n
    monkeypatch.setattr(two, "decide", decide)
    two.s.batch_size, two.s.gpu_batch, Clf.slow = 2, 1, 0.2   # small batches: both phases have work at once
    report = two.run_once(manual=True)
    assert (report["inferred"], report["analyzed"]) == (12, 12)
    assert threads == {"ai"} and overlap                       # AI in its thread, the check alongside
    assert unloads and unloads[0] == ["geo"]                   # models released once the AI part is over
    assert dets(two) == dets(one) and dets(two)
    assert two.db.execute("SELECT count(*) FROM inferred").fetchone()[0] == 0 and two.pending_analysis() == 0
    assert seen[-1] == [[12, 12], [12, 12]]                     # both counters complete

    # Dashboard setting during the cycle (Interrupted): the AI thread stops too, nothing half done, the next
    # cycle resumes where it was.
    for i in range(20, 30):
        two.db.execute("INSERT INTO asset (asset_id, file_name, taken_at, kind, people) VALUES (?, ?, ?, 'IMAGE', 0)",
                       (f"p{i}", f"p{i}.jpg", f"2024-02-{i - 19:02d}"))
    two.db.commit()
    stop_after = [1]
    def stopping(*a, **k):
        n = real_decide(*a, **k)
        if n:
            stop_after[0] -= 1
            if stop_after[0] == 0:
                two._stop.set()
        return n
    monkeypatch.setattr(two, "decide", stopping)
    report = two.run_once(manual=True)
    assert report.get("interrupted") and not [t for t in threading.enumerate() if t.name == "ai"]
    two._stop.clear()
    monkeypatch.setattr(two, "decide", real_decide)
    report = two.run_once(manual=True)
    assert two.pending_analysis() == 0 and two.db.execute("SELECT count(*) FROM inferred").fetchone()[0] == 0
    assert len({r[0] for r in two.db.execute("SELECT asset_id FROM detection")}) == 22

    # AI result stored, then a restart: only the geographic check is left.
    Clf.slow = 0
    two.db.execute("UPDATE asset SET analyzed_at = NULL WHERE asset_id = 'p0'")
    two.db.execute("INSERT INTO inferred VALUES ('p0', ?, 0)", (Analyzer.pack(two._analyzer.infer_many([(jpeg(), None, None, None)])[0]),))
    two.db.commit()
    calls = Clf.calls
    assert Worker(two.s, analyzer=two._analyzer).pending_geo() == 1
    assert two.decide() == 1 and Clf.calls == calls and two.pending_analysis() == 0


def test_fossil_test_from_the_world_list_and_the_area():
    """Extinct species: fossils from one world list (no request per species); a living species with a few fossils
    is settled by its field observations around the photo; only a species with fossils and almost no observation
    nearby costs a worldwide search. Same verdicts as the per-taxon rule."""
    import httpx
    from taxonero import store
    from taxonero.taxonomy import Taxonomy

    class Http:
        calls: list = []
        def get(self, url, params=None):
            u = httpx.URL(str(url))
            Http.calls.append(u)
            if u.path.startswith("/v1/species/"):
                body = {"key": int(u.path.rsplit("/", 1)[1]), "rank": "SPECIES"}
            elif u.params.get("basisOfRecord") == "FOSSIL_SPECIMEN" and "facet" in u.params:   # world list
                body = {"facets": [{"field": "SPECIES_KEY", "counts": [{"name": "2", "count": 917}, {"name": "3", "count": 28}]}]}
            elif "facet" in u.params:   # area: the fox (2) is common there, the goose (3) absent
                body = {"facets": [{"field": "SPECIES_KEY", "counts": [{"name": "1", "count": 40}, {"name": "2", "count": 5000}]}]}
            else:   # worldwide field observations of the goose
                body = {"count": 0}
            return httpx.Response(200, json=body, request=httpx.Request("GET", str(u)))

    tx = Taxonomy(store.connect(":memory:"))
    tx.min_interval, tx.http = 0, Http()
    for k in (1, 2, 3):
        tx.get(f"https://api.gbif.org/v1/species/{k}")
    Http.calls.clear()
    assert not tx.fossil_only(1, 45.0, 5.0)          # no fossil
    assert not tx.fossil_only(2, 45.0, 5.0)          # 917 fossils, 5,000 observations nearby: living
    assert len(Http.calls) == 2                      # the world list and the area, once
    assert tx.fossil_only(3, 45.0, 5.0)              # 28 fossils, none observed: extinct
    assert len(Http.calls) == 3 and "taxonKey" in Http.calls[-1].params
    assert not tx.fossil_only(1) and len(Http.calls) == 3   # without a place: still no request for no fossil


def test_grouped_searches_refused_by_gbif_fall_back_to_per_taxon():
    """A permanent refusal of the area search or of the fossil list (4xx, e.g. after a change of the GBIF API) does
    not make the photos fail: noted once, then the per-taxon searches answer as before."""
    import httpx
    from taxonero import store
    from taxonero.taxonomy import Taxonomy

    class Http:
        calls: list = []
        def get(self, url, params=None):
            u = httpx.URL(str(url))
            Http.calls.append(u)
            if u.path.startswith("/v1/species/"):
                return httpx.Response(200, json={"key": 1, "rank": "SPECIES"}, request=httpx.Request("GET", str(u)))
            if "facet" in u.params:
                return httpx.Response(400, text="Facets paging is only supported up to 1200000 elements", request=httpx.Request("GET", str(u)))
            return httpx.Response(200, json={"count": 0 if u.params.get("basisOfRecord") == "FOSSIL_SPECIMEN" else 12},
                                  request=httpx.Request("GET", str(u)))

    tx = Taxonomy(store.connect(":memory:"))
    tx.min_interval, tx.http = 0, Http()
    tx.get("https://api.gbif.org/v1/species/1")
    assert tx.occurrences_near(1, 45.0, 5.0, 5.0) == 12 and not tx.fossil_only(1, 45.0, 5.0)
    n = len(Http.calls)
    assert tx.occurrences_near(1, 46.0, 5.0, 5.0) == 12          # refusal remembered: no new grouped search
    assert len(Http.calls) == n + 1 and "facet" not in Http.calls[-1].params


def test_requeue_failed_drops_their_waiting_ai_results(tmp_path):
    from taxonero.config import Settings
    from taxonero.worker import MAX_FAILURES, Worker, requeue
    w = Worker(Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, gbif_local=False))
    w.db.execute("INSERT INTO asset (asset_id, file_name, fail_count) VALUES ('a', 'a.jpg', ?), ('b', 'b.jpg', 0)", (MAX_FAILURES,))
    w.db.execute("INSERT INTO inferred VALUES ('a', x'00', 0), ('b', x'00', 0)")
    assert requeue(w.db, "failed") == 1
    assert [r[0] for r in w.db.execute("SELECT asset_id FROM inferred")] == ["b"]


def test_installer_retries_a_step_and_falls_back_to_cpu_only_for_pytorch(tmp_path, monkeypatch):
    """Desktop launcher: a uv step that fails once (Windows: file locked by the antivirus or by a closing
    Taxonero) is tried again; only a PyTorch failure leads to the CPU build; everything goes to install.log."""
    import importlib.util
    import subprocess
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("bootstrap", Path(__file__).parent.parent / "packaging" / "bootstrap.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    monkeypatch.setattr(b.time, "sleep", lambda s: None)
    monkeypatch.setattr(b, "LOG", tmp_path / "install.log")
    calls, failures = [], {}

    def stream(cmd, env, ui):
        step = "venv" if cmd[1] == "venv" else "torch" if any(c.startswith("torch") for c in cmd) else "app"
        calls.append(step)
        b.log_line(" ".join(cmd))
        if failures.get(step, 0) > 0:
            failures[step] -= 1
            raise subprocess.CalledProcessError(2, cmd[0])
    monkeypatch.setattr(b, "stream", stream)
    ui = type("UI", (), {"step": lambda *a: None, "line": lambda *a: None})()
    install = lambda variant: b.install(tmp_path, variant, tmp_path / "x.whl", ui)

    failures["venv"] = 1
    install("cu130")
    assert calls == ["venv", "venv", "torch", "app"]
    calls.clear(); failures["torch"] = 3
    with pytest.raises(b.TorchFailed):
        install("cu130")
    calls.clear(); failures["app"] = 3
    with pytest.raises(subprocess.CalledProcessError):
        install("cu130")
    assert calls == ["venv", "torch", "app", "app", "app"]
    assert "venv" in (tmp_path / "install.log").read_text()


def test_gpu_batch_adapts_to_memory_and_shrinks_after_out_of_memory(monkeypatch):
    """Photos per GPU pass from its memory (8 for 12 GB, 16 for 24 GB, 1 on the CPU); a batch that runs out of
    memory is done one by one, with the same results, and the automatic size is halved for the rest of the run."""
    from taxonero import hardware
    from taxonero.pipeline import on_gpu_batch
    monkeypatch.setattr(hardware, "OOM_EVENTS", 0)
    assert [hardware.auto_batch("cuda", g) for g in (12, 24, 6, 80)] == [8, 16, 4, 32]
    assert hardware.auto_batch("cpu", 12) == 1 and hardware.auto_batch("mps", None) == 8

    def batch(images):
        if len(images) > 2:
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        return [i * 10 for i in images]
    assert on_gpu_batch(batch, lambda i: i * 10, [1, 2, 3, 4]) == [10, 20, 30, 40]
    assert hardware.OOM_EVENTS == 1 and hardware.auto_batch("cuda", 12) == 4
    with pytest.raises(RuntimeError, match="other"):
        on_gpu_batch(lambda images: (_ for _ in ()).throw(RuntimeError("other")), lambda i: i, [1, 2])


def test_tags_are_written_while_the_analysis_goes_on(tmp_path):
    """Photos confirmed by the geographic check get their tags from the "tags" thread (its own connection) as they
    come; a photo without a confirmed detection is not sent; the next sync has nothing left to write for them."""
    from taxonero.config import Settings
    from taxonero.sync import Syncer
    from taxonero.worker import Worker, _TagWriter

    _sync_db(tmp_path, ["a", "b"]).close()
    (tmp_path / "s.sqlite").rename(tmp_path / "taxonero.sqlite")   # the worker's database
    w = Worker(Settings(immich_url="http://x", immich_api_key="k", data_dir=tmp_path, gbif_local=False))
    w.db.execute("INSERT INTO asset (asset_id) VALUES ('c')")   # analysed, nothing confirmed
    w.db.commit()
    im = FakeImmich(["a", "b", "c"])
    im.write_gap, im.writable, w.client = 0, True, im
    w.progress["phases"] = {"analyze": [0, 0, 0], "geo": [0, 0, 0]}
    tags = _TagWriter(w)
    tags.add(["a", "c"])
    tags.add(["b"])
    tags.finish()
    assert tags.done == 2 and w.progress["phases"]["tags"][:2] == [2, 2]
    assert all(t["value"].endswith("Parus major") for a in ("a", "b") for t in im.assets[a]["tags"])
    assert not im.assets["c"]["tags"]
    im.gets = 0
    assert Syncer(w.db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all(full=False).assets == 0


def test_sync_keeps_no_write_transaction_open_while_calling_immich(tmp_path):
    """The tags are written alongside the analysis: a slow Immich must not keep the database locked for the other
    threads (each write to the database is committed before the next call to Immich)."""
    import threading
    import time
    from taxonero import store
    from taxonero.sync import Syncer
    db = _sync_db(tmp_path, ["a"])

    class Slow(FakeImmich):
        def bulk_tag(self, tag_ids, asset_ids):
            time.sleep(1.5)
            return super().bulk_tag(tag_ids, asset_ids)
    im = Slow(["a"])
    t = threading.Thread(target=lambda: Syncer(db, im, write_gap=0, settle_seconds=0, verify_rounds=0).sync_all(asset_ids=["a"], full=False))
    t.start()
    other = store.connect(tmp_path / "s.sqlite")
    other.execute("PRAGMA busy_timeout = 200")   # a write that waits for Immich fails here
    while t.is_alive():
        other.execute("INSERT OR REPLACE INTO inferred VALUES ('x', x'00', 0)")
        other.commit()
        time.sleep(0.05)
    assert [x["value"] for x in im.assets["a"]["tags"]] == ["Nature/Aves/Paridae/Parus/Parus major"]


def test_area_search_refused_for_load_falls_back_then_retries(monkeypatch):
    """GBIF busy (429) on the heavy area search: the per-taxon searches answer (same counts, the analysis goes on),
    the area is not asked again for GROUPED_COOLDOWN, then it is."""
    import httpx
    from taxonero import store, taxonomy
    from taxonero.taxonomy import Taxonomy
    monkeypatch.setattr("time.sleep", lambda s: None)
    busy = [True]

    class Http:
        calls: list = []
        def get(self, url, params=None):
            u = httpx.URL(str(url))
            Http.calls.append(u)
            req = httpx.Request("GET", str(u))
            if u.path.startswith("/v1/species/"):
                return httpx.Response(200, json={"key": 1, "rank": "SPECIES"}, request=req)
            if "facet" in u.params:
                if busy[0]:
                    return httpx.Response(429, request=req)
                return httpx.Response(200, json={"facets": [{"field": "SPECIES_KEY", "counts": [{"name": "1", "count": 30}]}]}, request=req)
            return httpx.Response(200, json={"count": 30}, request=req)

    tx = Taxonomy(store.connect(":memory:"))
    tx.min_interval, tx.http = 0, Http()
    tx.get("https://api.gbif.org/v1/species/1")
    assert tx.occurrences_near(1, 45.0, 5.0, 5.0) == 30                       # per-taxon answer
    n = len(Http.calls)
    assert tx.occurrences_near(1, 46.0, 5.0, 5.0) == 30 and len(Http.calls) == n + 1   # no area attempt meanwhile
    busy[0] = False
    tx._busy["area"] = 0                                                       # cooldown over
    assert tx.occurrences_near(1, 47.0, 5.0, 5.0) == 30 and "facet" in Http.calls[-1].params


def test_window_icon_is_an_ico_on_windows():
    """WinForms only reads .ico files: the PNG closed the Windows application at start (ArgumentException in a .NET
    thread, no message). Every platform gets an existing file of the format it reads."""
    from PIL import Image
    from taxonero.desktop import window_icon
    for platform, fmt in (("win32", "ICO"), ("linux", "PNG"), ("darwin", "PNG")):
        path = Path(window_icon(platform))
        assert path.is_file() and Image.open(path).format == fmt
    with Image.open(window_icon("win32")) as ico:
        assert {(16, 16), (32, 32), (256, 256)} <= set(ico.info["sizes"])


def test_exiftool_found_in_the_installer_copy_without_the_launcher(tmp_path, monkeypatch):
    """Started by hand (python -m taxonero desktop), without TAXONERO_EXIFTOOL from the launcher: the copy the
    installer keeps in <data>/runtime/exiftool is used before the PATH."""
    from taxonero.sources import exiftool
    monkeypatch.delenv("TAXONERO_EXIFTOOL", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(exiftool.shutil, "which", lambda name: None)
    assert exiftool.command() is None
    exe = tmp_path / "runtime" / "exiftool" / "exiftool.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert exiftool.command() == [str(exe)]
    other = tmp_path / "launcher"
    other.mkdir()
    (other / "exiftool.exe").write_bytes(b"")
    monkeypatch.setenv("TAXONERO_EXIFTOOL", str(other))                      # the launcher's folder comes first
    assert exiftool.command() == [str(other / "exiftool.exe")]


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="ExifTool absent")
def test_two_folders_as_two_sources(tmp_path):
    """Several sources: a folder added next to the main one. Same file name in both (no id clash), each photo read
    and written in its own source, records kept per source, deletions checked per source; removing the source hides
    its photos, adding it again brings them back with their reviews, and a restart reopens it."""
    import json
    import shutil
    from pathlib import Path
    from taxonero import store
    from taxonero.config import Settings
    from taxonero.pipeline import Analysis, Box, Candidate, Decision
    from taxonero.worker import Worker

    src = Path(__file__).parent.parent / "dev" / "photos" / "inat_141438356.jpg"
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    for d in (a_dir, b_dir):
        d.mkdir()
        shutil.copy(src, d / "img.jpg")
        old = __import__("time").time() - 3600   # changed an hour ago (otherwise left alone for 10 min)
        __import__("os").utime(d / "img.jpg", (old, old))

    class Fake:
        def analyze(self, data, lat=None, lon=None, original=None, fallback=None, groups=None):
            c = Candidate("Circus hudsonius", .9, {"kingdom": "Animalia"}, 1, "Circus hudsonius", posterior=.9, prior=.9)
            return [Analysis(Box(.1, .1, .5, .5, .9, "animal"), Decision(1, "Circus hudsonius", "species", .9, .8, "green", [c]))]

    s = Settings(source="local", photos_dir=a_dir, data_dir=tmp_path / "data", dry_run=False, auto_confirm="off", gbif_local=False)
    s.data_dir.mkdir()
    w = Worker(s, analyzer=Fake())
    for bad, why in (({"type": "local", "path": str(a_dir)}, "main"), ({"type": "local", "path": str(tmp_path / "x")}, "not found"),
                     ({"type": "nope", "path": str(b_dir)}, "not available"), ({"type": "immich", "url": "http://x"}, "required")):
        with pytest.raises(ValueError, match=why):
            w.add_source(bad)
    added = w.add_source({"type": "local", "path": str(b_dir), "label": "Phone"})
    sid = added["id"]
    assert added["label"] == "Phone" and added["writable"] and sid.startswith("s")
    with pytest.raises(ValueError, match="already"):
        w.add_source({"type": "local", "path": str(b_dir)})
    assert w.check() and w.scan() == 2
    rows = {r["source"]: r["asset_id"] for r in w.db.execute("SELECT asset_id, source FROM asset")}
    assert set(rows) == {"", sid} and rows[""] != rows[sid]                 # same "img.jpg", two photos
    assert w.client_for(rows[sid]) is w.extras[sid] and w.client_for(rows[""]) is w.client
    assert w.client_for(rows[sid]).thumbnail(rows[sid], "thumbnail")

    store.save_taxon(w.db, 1, json.dumps({"scientific_name": "Circus hudsonius", "rank": "species", "vernacular": {"fr": "Busard"},
                                          "lineage": {"class": "Aves", "family": "Accipitridae", "genus": "Circus"}, "v": 99}))
    w.db.commit()
    assert w.analyze() == 2
    w.db.execute("UPDATE detection SET status = 'confirmed', confirmed_by = 'user'")
    w.db.commit()
    assert w.sync().assets == 2
    assert (a_dir / "img.jpg.xmp").exists() and (b_dir / "img.jpg.xmp").exists()
    written = {r["kind"] for r in w.db.execute("SELECT kind FROM written")}
    assert {"tag", f"tag@{sid}", "tag_asset", f"tag_asset@{sid}"} <= written   # same tag value, kept apart
    assert w.extras[sid].get_asset(rows[sid])["tags"]

    shutil.copy(src, b_dir / "other.jpg")   # an empty folder is never taken as "everything deleted"
    (b_dir / "img.jpg").unlink()                                             # deleted from the added folder only
    w.reconcile(force=True)
    hidden = lambda aid: w.db.execute("SELECT missing_at FROM asset WHERE asset_id = ?", (aid,)).fetchone()[0] is not None
    assert hidden(rows[sid]) and not hidden(rows[""])
    shutil.copy(src, b_dir / "img.jpg")
    w.scan()
    assert not hidden(rows[sid])

    w.remove_source(sid)
    assert hidden(rows[sid]) and not hidden(rows[""]) and sid not in w.extras
    with pytest.raises(FileNotFoundError):
        w.client_for(rows[sid])
    assert w.add_source({"type": "local", "path": str(b_dir)})["id"] == sid      # same source: same id
    w.scan()
    assert not hidden(rows[sid])
    assert w.db.execute("SELECT status FROM detection WHERE asset_id = ?", (rows[sid],)).fetchone()[0] == "confirmed"

    w.client.close()
    for c in w.extras.values():
        c.close()
    w2 = Worker(s, analyzer=Fake())                                          # restart: the added source is reopened
    assert list(w2.extras) == [sid] and w2.client_for(rows[sid]) is w2.extras[sid]
    w2.client.close()
    w2.extras[sid].close()


def test_records_stay_readable_by_the_previous_version(tmp_path):
    """Going back to 0.2.x after this version must keep working: the table of what was written keeps its columns
    (0.2.x inserts 5 values), the main source's records are unchanged, and those of an added source have their own kinds,
    which 0.2.x ignores (it only reads 'tag', 'tag_asset' and 'description')."""
    from taxonero import store
    from taxonero.sync import Syncer
    db = store.connect(tmp_path / "db.sqlite")
    assert [r["name"] for r in db.execute("PRAGMA table_info(written)")] == ["kind", "immich_id", "asset_id", "value", "written_at"]
    db.execute("INSERT OR REPLACE INTO written VALUES (?, ?, ?, ?, ?)", ("tag", "t1", "", "Nature", 1.0))   # as 0.2.x does
    Syncer(db, None, source="s1")._record("tag", "t1", "", "Nature")       # same tag value, in an added source
    assert sorted(r[0] for r in db.execute("SELECT kind FROM written")) == ["tag", "tag@s1"]
    assert db.execute("SELECT count(*) FROM written WHERE kind = 'tag'").fetchone()[0] == 1


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="ExifTool absent")
def test_sources_api_and_photo_filter(tmp_path):
    """Settings > Other sources: added, listed (never with the API key), refused with a reason, removed; the Photos
    page filters by source and the viewer says where a photo comes from."""
    from PIL import Image
    s, w = _local_worker(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    Image.new("RGB", (64, 48), (200, 10, 10)).save(other / "x.jpg")
    c = _web(s, w)
    assert c.get("/api/sources").json()["extra"] == [] and c.get("/api/status").json()["sources"] == []
    r = c.post("/api/sources/extra", json={"type": "local", "path": str(tmp_path / "nowhere")})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_source"
    r = c.post("/api/sources/extra", json={"type": "local", "path": str(other), "label": "Garden"})
    assert r.status_code == 200
    extra = r.json()["extra"]
    assert [(x["label"], x["type"], x["writable"]) for x in extra] == [("Garden", "local", True)]
    sid = extra[0]["id"]
    w.scan()
    assert c.get("/api/sources").json()["extra"][0]["photos"] == 1
    status = c.get("/api/status").json()
    assert [x["id"] for x in status["sources"]] == ["main", sid]
    ids = lambda **q: {a["asset_id"] for a in c.get("/api/photos", params={"filter": "all", **q}).json()}
    assert len(ids()) == 3 and len(ids(source="main")) == 2 and len(ids(source=sid)) == 1
    (aid,) = ids(source=sid)
    assert c.get(f"/api/assets/{aid}").json()["source_label"] == "Garden"
    assert c.get(f"/api/thumb/{aid}", params={"size": "thumbnail"}).status_code == 200
    assert c.delete("/api/sources/extra/nope").status_code == 404
    assert c.delete(f"/api/sources/extra/{sid}").json()["extra"] == []
    assert len(ids()) == 2 and c.get(f"/api/thumb/{aid}").status_code == 404   # hidden with its source
    # An Immich account is never sent back with its key.
    w.extra_specs.append({"id": "s0", "type": "immich", "url": "http://imm", "key": "secret-key", "label": ""})
    assert "secret-key" not in c.get("/api/sources").text
    w.client.close()


def _fake_apple_photos(monkeypatch, lib, photos, open_in_photos=True, running=True, loads=None):
    """osxphotos and photoscript replaced by fakes with the same interface (they only run on macOS): photos is
    {uuid: dict of PhotoInfo attributes}; keywords and description are those Photos holds (written by photoscript)."""
    import sys
    import types
    from datetime import datetime, timezone
    from taxonero.sources import applephotos

    class Info:
        def __init__(self, uuid, d):
            self.uuid, self._d = uuid, d

        def __getattr__(self, k):
            return self._d[k]

        @property
        def keywords(self):
            return list(self._d["keywords"])

        @property
        def description(self):
            return self._d["description"]

    class PhotosDB:
        def __init__(self, dbfile=None):
            assert dbfile == str(lib)
            if loads is not None:
                loads.append(1)

        def photos(self, images=True, movies=True):
            return [Info(u, d) for u, d in photos.items()]

    class Photo:
        def __init__(self, uuid):
            assert running, "AppleScript would start Photos"
            if not open_in_photos or uuid not in photos:
                raise ValueError(f"Invalid photo id: {uuid}")
            self._d = photos[uuid]

        keywords = property(lambda self: list(self._d["keywords"]), lambda self, v: self._d.update(keywords=list(v)))
        description = property(lambda self: self._d["description"], lambda self, v: self._d.update(description=v))

    monkeypatch.setitem(sys.modules, "osxphotos", types.SimpleNamespace(PhotosDB=PhotosDB))
    monkeypatch.setitem(sys.modules, "photoscript", types.SimpleNamespace(Photo=Photo))
    monkeypatch.setattr(applephotos, "MACOS", True)
    monkeypatch.setattr(applephotos, "_photos_running", lambda: running)
    return datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc)


def test_apple_photos_source(tmp_path, monkeypatch):
    """Apple Photos (macOS): photos read from the library (place, date, people), images from the previews Photos keeps,
    keywords and description written through Photos and removed when rejected, a keyword of the user left alone;
    a library that is not the one open in Photos is read only."""
    import json
    import types
    from PIL import Image
    from taxonero import store
    from taxonero.config import Settings
    from taxonero.pipeline import Analysis, Box, Candidate, Decision
    from taxonero.worker import Worker

    lib = tmp_path / "Photos Library.photoslibrary"
    (lib / "resources" / "derivatives").mkdir(parents=True)
    (lib / "database").mkdir()
    (lib / "database" / "Photos.sqlite").write_bytes(b"v1")
    prev = lib / "resources" / "derivatives" / "A_1_105_c.jpeg"
    Image.new("RGB", (1600, 1200), (30, 140, 60)).save(prev)
    place = types.SimpleNamespace(address=types.SimpleNamespace(city="Arles", state_province="PACA", country="France"))
    exif = types.SimpleNamespace(camera_make="Apple", camera_model="iPhone 15", lens_model=None)
    photos = {}
    when = _fake_apple_photos(monkeypatch, lib, photos)
    photos.update({
        "A": dict(exif_info=exif, location=(43.67, 4.63), place=place, width=4032, height=3024, fingerprint="fa",
                  original_filesize=1, original_filename="IMG_1.HEIC", persons=[], date=when, date_modified=when,
                  description="Holiday", keywords=["Vacances"], path=None, path_derivatives=[str(prev)]),
        "B": dict(exif_info=None, location=(None, None), place=None, width=10, height=10, fingerprint=None,
                  original_filesize=2, original_filename="IMG_2.HEIC", persons=["_UNKNOWN_"], date=when, date_modified=None,
                  description="", keywords=[], path=None, path_derivatives=[str(prev)])})

    class Fake:
        def analyze(self, data, lat=None, lon=None, original=None, fallback=None, groups=None):
            c = Candidate("Circus hudsonius", .9, {"kingdom": "Animalia"}, 1, "Circus hudsonius", posterior=.9, prior=.9)
            return [Analysis(Box(.1, .1, .5, .5, .9, "animal"), Decision(1, "Circus hudsonius", "species", .9, .8, "green", [c]))]

    s = Settings(source="applephotos", catalog_path=lib, data_dir=tmp_path / "data", dry_run=False, auto_confirm="off",
                 gbif_local=False, skip_people=True, analysis_window="")
    s.data_dir.mkdir()
    w = Worker(s, analyzer=Fake())
    assert w.check() and w.client.writable and not w.client.warnings
    assert w.scan() == 2
    rows = {r["file_name"]: dict(r) for r in w.db.execute("SELECT * FROM asset")}
    a = rows["IMG_1.HEIC"]
    assert (a["city"], a["country"], a["taken_at"], a["make"], a["people"]) == ("Arles", "France", "2026-05-01T09:30:00", "Apple", 0)
    assert rows["IMG_2.HEIC"]["people"] == 1                                 # a face: skipped like with Immich
    assert min(Image.open(__import__("io").BytesIO(w.client.thumbnail(a["asset_id"], "thumbnail"))).size) == 250
    store.save_taxon(w.db, 1, json.dumps({"scientific_name": "Circus hudsonius", "rank": "species", "vernacular": {"fr": "Busard"},
                                          "lineage": {"class": "Aves", "family": "Accipitridae", "genus": "Circus"}, "v": 99}))
    w.db.commit()
    w.analyze()
    w.db.execute("UPDATE detection SET status = 'confirmed', confirmed_by = 'user'")
    w.db.commit()
    w.sync()
    kw = photos["A"]["keywords"]
    assert kw[0] == "Vacances" and kw[1].startswith("Nature/Aves/") and len(kw) == 2
    assert photos["A"]["description"].startswith("Holiday") and "Busard" in photos["A"]["description"]
    w.db.execute("UPDATE detection SET status = 'rejected'")
    w.db.commit()
    w.sync()
    assert photos["A"]["keywords"] == ["Vacances"] and photos["A"]["description"].strip() == "Holiday"

    _fake_apple_photos(monkeypatch, lib, photos, open_in_photos=False)    # another library open in Photos
    assert not w.check() and not w.client.errors and "not the one open in Photos" in w.client.warnings[0]
    _fake_apple_photos(monkeypatch, lib, photos, running=False)            # Photos closed: never started by Taxonero
    assert not w.check() and "not open" in w.client.warnings[0]
    assert w.client.get_asset(a["asset_id"])["tags"] == [{"id": "Vacances", "value": "Vacances"}]   # still readable

    loads = []                                                               # the library is read again only when changed
    _fake_apple_photos(monkeypatch, lib, photos, loads=loads)
    w.client._photos = {}
    w.check(), w.scan(), w.reconcile(force=True)
    assert len(loads) == 1
    (lib / "database" / "Photos.sqlite").write_bytes(b"v22")
    w.scan()
    assert len(loads) == 2
    w.client.close()


def test_apple_photos_only_on_macos(tmp_path):
    from taxonero.sources import available
    from taxonero.sources.applephotos import ApplePhotos
    import sys
    assert {o["id"]: o["available"] for o in available("desktop")}["applephotos"] == (sys.platform == "darwin")
    lib = tmp_path / "x.photoslibrary"
    lib.mkdir()
    src = ApplePhotos(lib, tmp_path)
    if sys.platform != "darwin":
        assert not src.check() and "macOS only" in src.errors[0]
    src.close()


def test_an_unreachable_source_does_not_stop_the_others(tmp_path):
    """An added Immich account that does not answer: the main source is still read, the failing one is reported and
    its photos are not taken for deleted; if every source fails, the error is raised as before."""
    import httpx
    s, w = _local_worker(tmp_path)

    class Down:
        writable, dry_run, errors, warnings = False, True, [], []

        def iter_assets(self, **k):
            raise httpx.ConnectError("unreachable")
            yield

    w.db.execute("INSERT INTO asset (asset_id, source) VALUES ('far', 'sX')")
    w.db.commit()
    w.extras, w.extra_specs = {"sX": Down()}, [{"id": "sX", "type": "immich", "url": "http://far", "key": "k", "label": ""}]
    assert w.scan() == 2 and "unreachable" in w.scan_errors["sX"]
    w.reconcile(force=True)
    assert w.db.execute("SELECT missing_at FROM asset WHERE asset_id = 'far'").fetchone()[0] is None   # not "deleted"
    w.sync()                                                                 # read-only source: skipped, no error
    w.s.source, w.s.immich_url = "immich", ""                                # only the failing source left
    with pytest.raises(httpx.ConnectError):
        w.scan()
    w.client.close()


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="ExifTool absent")
def test_only_added_sources_and_no_double_source(tmp_path):
    """No main source set (Immich left empty) but a folder added: the cycle runs on it and the dashboard names it;
    a folder or an Immich account cannot be both the main source and an added one."""
    from PIL import Image
    from taxonero.config import Settings
    from taxonero.worker import Worker
    folder = tmp_path / "f"
    folder.mkdir()
    Image.new("RGB", (64, 48), (1, 2, 3)).save(folder / "a.jpg")
    s = Settings(source="immich", immich_url="", immich_api_key="", data_dir=tmp_path, gbif_local=False,
                 dashboard_no_auth=True)

    class NoAnalyzer:
        def analyze(self, *a, **k):
            return []
    w = Worker(s, analyzer=NoAnalyzer())
    assert not w.configured
    sid = w.add_source({"type": "local", "path": str(folder), "label": "Folder"})["id"]
    assert w.configured and not w.main_configured
    report = w.run_once(manual=True)
    assert report["scanned"] == 1 and not report.get("errors")
    st = _web(s, w).get("/api/status").json()
    assert st["writable"] and [x["id"] for x in st["sources"]] == [sid]
    w.env_source = False                                                     # as if chosen in the dashboard
    with pytest.raises(ValueError, match="already an added source"):
        w.set_source("local", str(folder))
    w.extra_specs = [*w.extra_specs, {"id": "s9", "type": "immich", "url": "http://imm", "key": "k", "label": ""}]
    assert w.connect("http://imm", "k")["code"] == "duplicate"
    w.extras[sid].close()


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="ExifTool absent")
def test_a_file_name_with_a_line_break_does_not_stop_the_scan(tmp_path):
    """ExifTool reads its arguments one per line: such a name cannot be given to it. It used to stop the reading of
    its whole batch (200 photos), at every cycle; it is now skipped alone."""
    from PIL import Image
    from taxonero.sources.local import LocalFolder
    ph = tmp_path / "ph"
    ph.mkdir()
    Image.new("RGB", (32, 32)).save(ph / "ok.jpg")
    Image.new("RGB", (32, 32)).save(ph / "bad\nname.jpg", "JPEG")
    src = LocalFolder(ph, tmp_path)
    assert [a["originalFileName"] for a in src.iter_assets()] == ["ok.jpg"] and src.count_files() == 1
    src.close()


def test_backups_keep_no_api_key_of_added_accounts(tmp_path, monkeypatch):
    """Backups leave out every Immich API key, those of the added accounts too; after a restore the account asks for
    its key again, and a new key keeps the same source (its photos and reviews)."""
    import json
    import sqlite3
    from taxonero import store
    s, w = _local_worker(tmp_path, backup_keep=2)

    class Client:
        writable, dry_run, version, errors, warnings = True, True, "3.3.5", [], []

        def __init__(self, key):
            self.key = key

        def check(self):
            self.errors = [] if self.key == "good" else ["HTTP 401"]
            return not self.errors

        def status(self):
            return {"version": self.version, "errors": self.errors, "warnings": []}

        def close(self):
            pass
    monkeypatch.setattr("taxonero.worker.make_extra", lambda spec, s: Client(spec["key"]))
    spec = {"id": "s1", "type": "immich", "url": "http://far", "key": "secret-key-123", "label": ""}
    w.extra_specs, w.extras = [spec], {"s1": Client("secret-key-123")}
    store.set_state(w.db, "sources", json.dumps([spec]))
    dest = w.backup(force=True)
    saved = sqlite3.connect(dest).execute("SELECT value FROM state WHERE key = 'sources'").fetchone()[0]
    assert "secret-key-123" not in dest.read_bytes().decode("latin-1") and json.loads(saved)[0]["key"] == ""
    assert json.loads(store.get_state(w.db, "sources"))[0]["key"] == "secret-key-123"   # the live database keeps it
    w.extra_specs = [{**spec, "key": ""}]                                    # as after a restore
    assert w.sources_status(w.db)[0]["key_missing"]
    with pytest.raises(ValueError, match="401"):
        w.set_source_key("s1", "wrong")
    assert w.set_source_key("s1", "good")["id"] == "s1"
    assert w.extra_specs[0]["key"] == "good" and not w.sources_status(w.db)[0]["key_missing"]
    with pytest.raises(KeyError):
        w.set_source_key("nope", "good")
    w.client.close()


def test_migration_adds_a_column_named_in_an_old_comment(tmp_path):
    """A database of 0.2.x: its asset table has no 'source' column, but a comment of its CREATE text says "source"
    (0.3.0 skipped the column and stopped at start)."""
    import sqlite3

    from taxonero import store
    body = store.SCHEMA.split("CREATE TABLE IF NOT EXISTS asset (", 1)[1].split(");", 1)[0]
    lines = [ln for ln in body.splitlines() if not ln.strip().startswith(("source ", "series_id "))]
    old = "CREATE TABLE asset (\n    -- a GPS of the source replaces them" + "\n".join(lines) + "\n    dropped INTEGER\n);"
    path = tmp_path / "taxonero.sqlite"
    raw = sqlite3.connect(path)
    raw.executescript(old)
    raw.execute("INSERT INTO asset (asset_id) VALUES ('a')")
    raw.commit()
    raw.close()
    db = store.connect(path)
    cols = {r["name"] for r in db.execute("PRAGMA table_info(asset)")}
    assert {"source", "series_id"} <= cols
    assert db.execute("SELECT source FROM asset WHERE asset_id = 'a'").fetchone()["source"] == ""
    assert {"asset_source", "asset_series"} <= {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def test_exiftool_processes_closed_at_exit(tmp_path):
    """ExifTool kept open never stops by itself: a source never closed, or the desktop window closed, must not leave
    it running after Python exits (hundreds of orphans had piled up, ~20 MB each)."""
    import subprocess
    import textwrap

    from taxonero.sources import exiftool
    if not exiftool.available():
        pytest.skip("ExifTool missing")
    pid_file = tmp_path / "pid"
    code = textwrap.dedent(f"""
        import gc
        from taxonero.sources.exiftool import ExifTool
        a, b = ExifTool(), ExifTool()
        a.run("-ver"); b.run("-ver")
        open({str(pid_file)!r}, "w").write(f"{{a._proc.pid}} {{b._proc.pid}}")
        del b; gc.collect()   # object gone, process never closed
    """)
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)
    for pid in map(int, pid_file.read_text().split()):
        try:
            os_kill = __import__("os").kill
            os_kill(pid, 0)
        except ProcessLookupError:
            continue
        raise AssertionError(f"ExifTool {pid} still running")
