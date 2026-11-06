"""Dashboard API against the dev Immich, on a copy of the dev database; simulated identification
(no model loaded). Skipped if the dev Immich or its database are not available."""

import json
import sqlite3

import pytest

from conftest import DEV_READY

pytestmark = pytest.mark.skipif(not DEV_READY, reason="dev Immich not initialised")


@pytest.fixture
def client(dev_app):
    from fastapi.testclient import TestClient
    from taxonero.web import create_app
    s, w = dev_app
    db = sqlite3.connect(s.db_path)
    db.row_factory = sqlite3.Row
    return TestClient(create_app(s, w)), db


FIELDS = ("status", "confirmed_by", "taxon_key", "taxon_rank", "reviewed_at", "x", "y", "w", "h", "score", "margin", "plausibility")


def test_all_filter_includes_unanalysed(client):
    c, db = client
    db.execute("UPDATE asset SET analyzed_at = NULL WHERE rowid = (SELECT min(rowid) FROM asset)")
    db.commit()
    assert c.get("/api/photos/counts").json()["all"] == db.execute("SELECT count(*) FROM asset").fetchone()[0]


def test_delete_reframe_and_exact_undo(client):
    c, db = client
    det = dict(db.execute("SELECT * FROM detection WHERE status = 'confirmed' AND confirmed_by = 'auto' LIMIT 1").fetchone())
    prev = {k: det[k] for k in FIELDS} | {"candidates": json.loads(det["candidates"])}
    c.delete(f"/api/detections/{det['id']}")
    assert all(d["id"] != det["id"] for d in c.get(f"/api/assets/{det['asset_id']}").json()["detections"])
    c.post(f"/api/detections/{det['id']}/restore", json=prev)
    r = c.put(f"/api/detections/{det['id']}/box", json={"x": .1, "y": .1, "w": .4, "h": .5}).json()
    moved = next(d for d in r["detections"] if d["id"] == det["id"])
    assert moved["status"] == "pending" and abs(moved["w"] - .4) < 1e-6
    c.post(f"/api/detections/{det['id']}/restore", json=prev)
    now = dict(db.execute("SELECT * FROM detection WHERE id = ?", (det["id"],)).fetchone())
    assert all(now[k] == det[k] for k in FIELDS) and json.loads(now["candidates"]) == prev["candidates"]


def test_reframe_keeps_user_choice(client):
    c, db = client
    det = dict(db.execute("SELECT * FROM detection WHERE status = 'confirmed' LIMIT 1").fetchone())
    c.post(f"/api/detections/{det['id']}/confirm", json={"gbif_key": det["taxon_key"]})
    r = c.put(f"/api/detections/{det['id']}/box", json={"x": .2, "y": .2, "w": .3, "h": .3}).json()
    d = next(x for x in r["detections"] if x["id"] == det["id"])
    assert (d["status"], d["confirmed_by"], d["taxon_key"]) == ("confirmed", "user", det["taxon_key"])


def test_cross_site_writes_are_refused(client):
    c, db = client
    det = db.execute("SELECT id FROM detection WHERE status = 'pending' LIMIT 1").fetchone()
    assert c.post(f"/api/detections/{det['id']}/reject", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post(f"/api/detections/{det['id']}/reject", headers={"Origin": "http://evil.example"}).status_code == 403
    assert db.execute("SELECT status FROM detection WHERE id = ?", (det["id"],)).fetchone()[0] == "pending"
    assert c.post(f"/api/detections/{det['id']}/reject", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_pending_detections_explain_why_not_auto(client):
    c, db = client
    photos = c.get("/api/photos?filter=pending&limit=20").json()
    pend = [d for p in photos for d in p["detections"] if d["status"] == "pending"]
    assert pend and all(d["why_pending"] or d["source"] == "manual" for d in pend)


def test_bulk_review_and_undo(client):
    c, db = client
    aid = db.execute("SELECT asset_id FROM detection WHERE status = 'pending' AND taxon_key IS NOT NULL LIMIT 1").fetchone()[0]
    before = {r["id"]: dict(r) for r in db.execute("SELECT * FROM detection WHERE asset_id = ? AND status = 'pending'", (aid,))}
    r = c.post("/api/photos/bulk", json={"asset_ids": [aid], "action": "confirm"}).json()
    assert {d["id"] for d in r["changed"]} <= set(before) and r["changed"]
    for d in r["changed"]:
        assert db.execute("SELECT status FROM detection WHERE id = ?", (d["id"],)).fetchone()[0] == "confirmed"
    assert c.post("/api/detections/restore-many", json=r["changed"]).json()["restored"] == len(r["changed"])
    for d in r["changed"]:
        assert dict(db.execute("SELECT * FROM detection WHERE id = ?", (d["id"],)).fetchone()) == before[d["id"]]


def test_bulk_reject_whole_filter(client):
    """"Select all": all photos of the filter, even not loaded, except the excluded ones. Rejection takes what
    remains to review; on a photo where nothing remains to review, what is confirmed."""
    c, db = client
    auto = [r[0] for r in db.execute("SELECT DISTINCT asset_id FROM detection WHERE status = 'confirmed' AND confirmed_by = 'auto'")]
    assert len(auto) >= 2
    keep = auto[0]
    has_pending = {r[0] for r in db.execute("SELECT DISTINCT asset_id FROM detection WHERE status = 'pending'")}
    snap = {r["id"]: dict(r) for r in db.execute("SELECT * FROM detection")}
    r = c.post("/api/photos/bulk", json={"filter": "auto", "every": True, "exclude": [keep], "action": "reject"}).json()
    hit = {d["asset_id"] for d in r["changed"]}
    assert hit == set(auto) - {keep}
    assert c.get("/api/photos/counts").json()["auto"] == 1 + len(set(auto) - {keep} & has_pending)
    assert db.execute("SELECT count(*) FROM detection WHERE asset_id = ? AND status = 'rejected' AND reviewed_at IS NULL", (keep,)).fetchone()[0] == 0
    c.post("/api/detections/restore-many", json=r["changed"])
    assert {r["id"]: dict(r) for r in db.execute("SELECT * FROM detection")} == snap
    # "To review": the confirmations done by hand on the same photos stay.
    user = db.execute("SELECT count(*) FROM detection WHERE status = 'confirmed' AND confirmed_by = 'user'").fetchone()[0]
    r = c.post("/api/photos/bulk", json={"filter": "pending", "every": True, "action": "reject"}).json()
    assert r["changed"] and all(d["status"] == "pending" for d in r["changed"])
    assert db.execute("SELECT count(*) FROM detection WHERE status = 'confirmed' AND confirmed_by = 'user'").fetchone()[0] == user
    assert c.post("/api/photos/bulk", json={"filter": "nope", "every": True, "action": "reject"}).status_code == 400


def test_photo_months_follow_list_order(client):
    """Timeline: number of photos per month, in the order of the list by date (the running total gives the position of a month)."""
    c, _ = client
    months = c.get("/api/photos/months?filter=all").json()
    assert sum(x["n"] for x in months) == c.get("/api/photos/counts").json()["all"]
    listed = [a["taken_at"][:7] if a["taken_at"] else None for a in c.get("/api/photos?filter=all&limit=1000").json()]
    off = 0
    for x in months:
        assert listed[off:off + x["n"]] == [x["m"]] * x["n"]
        off += x["n"]


def test_bulk_taxon_reset_reanalyze_and_undo(client):
    """Species in bulk: confirmed on the detection awaiting review; photo without a detection: "whole image" detection;
    photo with several subjects to review: left. Put back to review, analyse again, undo."""
    c, db = client
    key = db.execute("SELECT taxon_key FROM detection WHERE taxon_key IS NOT NULL LIMIT 1").fetchone()[0]
    one = db.execute("SELECT asset_id FROM detection WHERE status = 'pending' GROUP BY asset_id HAVING count(*) = 1 LIMIT 1").fetchone()[0]
    many = db.execute("SELECT asset_id FROM detection WHERE status = 'pending' GROUP BY asset_id HAVING count(*) > 1 LIMIT 1").fetchone()
    empty = db.execute("SELECT asset_id FROM asset WHERE asset_id NOT IN (SELECT asset_id FROM detection) LIMIT 1").fetchone()[0]
    ids = [one, empty] + ([many[0]] if many else [])
    snap = {r["id"]: dict(r) for r in db.execute("SELECT * FROM detection")}
    r = c.post("/api/photos/bulk", json={"asset_ids": ids, "action": "taxon", "gbif_key": key}).json()
    assert len(r["changed"]) == 1 and len(r["created"]) == 1 and r["skipped"] == (1 if many else 0)
    assert tuple(db.execute("SELECT status, confirmed_by, taxon_key FROM detection WHERE id = ?", (r["changed"][0]["id"],)).fetchone()) == ("confirmed", "user", key)
    assert tuple(db.execute("SELECT asset_id, w, h, status FROM detection WHERE id = ?", (r["created"][0],)).fetchone()) == (empty, 1, 1, "confirmed")
    c.post("/api/photos/bulk-undo", json={"changed": r["changed"], "created": r["created"]})
    assert {r["id"]: dict(r) for r in db.execute("SELECT * FROM detection")} == snap
    done = db.execute("SELECT asset_id FROM detection WHERE status = 'confirmed' LIMIT 1").fetchone()[0]
    r = c.post("/api/photos/bulk", json={"asset_ids": [done], "action": "reset"}).json()
    assert r["changed"] and all(db.execute("SELECT status FROM detection WHERE id = ?", (d["id"],)).fetchone()[0] == "pending" for d in r["changed"])
    c.post("/api/photos/bulk-undo", json={"changed": r["changed"]})
    assert {r["id"]: dict(r) for r in db.execute("SELECT * FROM detection")} == snap
    assert c.post("/api/photos/bulk", json={"asset_ids": [one], "action": "reanalyze"}).json()["requeued"] == 1
    assert db.execute("SELECT analyzed_at FROM asset WHERE asset_id = ?", (one,)).fetchone()[0] is None
    assert db.execute("SELECT count(*) FROM detection WHERE asset_id = ? AND status = 'pending'", (one,)).fetchone()[0] == 1


def test_reference_photos_keep_only_open_licences(client, monkeypatch):
    """Reference photos (iNaturalist): taxon with the same name, photos under a free licence only."""
    from taxonero import refphotos
    calls = []

    def fake_get(self, url, params=None, ttl=0):
        calls.append(url)
        if url.endswith("/taxa"):
            return {"results": [{"id": 1, "name": "Autre chose"}, {"id": 13094, "name": "Erithacus rubecula"}]}
        return {"results": [{"taxon_photos": [
            {"photo": {"id": 5, "medium_url": "https://x/photos/5/medium.jpg", "attribution": "(c) A", "license_code": "cc-by"}},
            {"photo": {"id": 6, "medium_url": "https://x/photos/6/medium.jpg", "attribution": "(c) B", "license_code": None}}]}]}
    monkeypatch.setattr("taxonero.taxonomy.Taxonomy.get", fake_get)
    c, db = client
    key = db.execute("SELECT taxon_key FROM detection WHERE taxon_key IS NOT NULL LIMIT 1").fetchone()[0]
    r = c.get(f"/api/refphotos/{key}?name=Erithacus%20rubecula").json()
    assert r["taxon"]["id"] == 13094 and [p["license"] for p in r["photos"]] == ["CC-BY"]
    assert r["photos"][0]["large"] == "https://x/photos/5/large.jpg" and calls[-1].startswith(refphotos.API)


def test_reference_photos_follow_inaturalist_synonym():
    """GBIF name that iNaturalist classifies differently: taxon found through matched_term, the exact name keeping priority."""
    from taxonero import refphotos

    class Tx:
        def __init__(self, results):
            self.results = results

        def get(self, url, params=None):
            return {"results": self.results}
    syn, exact = {"id": 1, "name": "Ardea alba modesta", "matched_term": "Ardea modesta"}, {"id": 2, "name": "Ardea modesta"}
    assert refphotos.inat_taxon(Tx([syn]), ["Ardea modesta"])["id"] == 1
    assert refphotos.inat_taxon(Tx([syn, exact]), ["Ardea modesta"])["id"] == 2
    assert refphotos.inat_taxon(Tx([{"id": 3, "name": "Autre", "matched_term": "Autre"}]), ["Ardea modesta"]) is None


def test_series_groups_close_shots(client):
    c, db = client
    a, b = [r[0] for r in db.execute("SELECT asset_id FROM asset LIMIT 2")]
    db.execute("UPDATE asset SET taken_at = '2020-06-01T10:00:00.000Z', make = 'X', model = 'Y', lat = NULL WHERE asset_id = ?", (a,))
    db.execute("UPDATE asset SET taken_at = '2020-06-01T10:01:30.000Z', make = 'X', model = 'Y', lat = NULL WHERE asset_id = ?", (b,))
    db.commit()
    assert set(c.get(f"/api/assets/{a}/series").json()["assets"]) == {a, b}
    db.execute("UPDATE asset SET taken_at = '2020-06-01T10:05:00.000Z' WHERE asset_id = ?", (b,))
    db.commit()
    assert c.get(f"/api/assets/{a}/series").json()["assets"] == [a]


def test_models_info(client):
    c, _ = client
    m = c.get("/api/models").json()
    assert m["hardware"]["device"] in ("cpu", "cuda", "xpu", "mps") and m["analyzer_id"]
    assert "torch" in m["hardware"]["versions"] and m["detector_providers"] is None


def test_manual_actions_survive_gbif_outage(client, monkeypatch):
    """GBIF observation service down: reframing answers fast, without error, candidates "unverified"."""
    import time
    import httpx
    from taxonero.taxonomy import Taxonomy
    real = Taxonomy._fetch
    def fetch(self, url, retries=6):
        if "/occurrence/" in url:
            raise httpx.HTTPError("simulated outage")
        return real(self, url, retries)
    monkeypatch.setattr(Taxonomy, "_fetch", fetch)
    c = client[0] if isinstance(client, tuple) else client
    a = next(x for x in c.get("/api/photos?filter=all&limit=200").json() if x["lat"] is not None and x["kind"] != "VIDEO" and x["detections"])
    t0 = time.monotonic()
    r = c.put(f"/api/detections/{a['detections'][0]['id']}/box", json={"x": .2, "y": .2, "w": .5, "h": .5})
    assert r.status_code == 200, r.text
    det = next(d for d in r.json()["detections"] if d["id"] == a["detections"][0]["id"])
    assert all(x["plausibility"] == "unverified" for x in det["candidates"]) and time.monotonic() - t0 < 20


def test_identify_upload_stores_nothing(client):
    import io
    from PIL import Image
    c, db = client
    buf = io.BytesIO(); Image.new("RGB", (64, 48), "green").save(buf, "JPEG")
    count = lambda: [db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("asset", "detection", "written")]
    before = count()
    r = c.post("/api/identify", content=buf.getvalue(), headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200 and r.json()["detections"][0]["candidates"][0]["name"] == "Vulpes vulpes"
    assert r.json()["lat"] is None and count() == before
    assert c.post("/api/identify?lat=95&lon=2", content=buf.getvalue()).status_code == 400
    assert c.post("/api/identify?lat=45", content=buf.getvalue()).status_code == 400
    placed = c.post("/api/identify?lat=45.5&lon=2.5", content=buf.getvalue(), headers={"Content-Type": "application/octet-stream"})
    assert placed.status_code == 200 and (placed.json()["lat"], placed.json()["lon"]) == (45.5, 2.5)
    bad = c.post("/api/identify", content=b"not an image", headers={"Content-Type": "application/octet-stream"})
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "bad_image"


def test_backup_daily_rotation_without_caches(dev_app):
    from taxonero import store
    s, w = dev_app
    s.backup_keep = 2
    folder = s.data_dir / "backups"
    folder.mkdir()
    for day in ("2026-01-01", "2026-01-02"):   # two old backups: the oldest must go
        (folder / f"taxonero-{day}.sqlite").write_bytes(b"")
    store.set_state(w.db, "last_backup", "")                     # the copied dev database may already have been backed up
    dest = w.backup()
    assert dest and dest.exists() and w.backup() is None          # only one per day
    assert sorted(p.name for p in folder.glob("taxonero-*.sqlite")) == ["taxonero-2026-01-02.sqlite", dest.name]
    copy = sqlite3.connect(dest)
    assert copy.execute("SELECT count(*) FROM detection").fetchone()[0] == w.db.execute("SELECT count(*) FROM detection").fetchone()[0]
    assert copy.execute("SELECT count(*) FROM http_cache").fetchone()[0] == 0
    assert copy.execute("SELECT count(*) FROM state WHERE key = 'immich'").fetchone()[0] == 0


def test_healthz_reports_stalled_loop(client, dev_app):
    c, _ = client
    _, w = dev_app
    assert c.get("/healthz").status_code == 200
    w.loop_started, w.last_activity = True, 0.0
    assert c.get("/healthz").status_code == 503


def test_immich_key_never_sent_to_browser(client):
    c, _ = client
    from conftest import ENV
    key = dict(l.split("=", 1) for l in ENV.read_text().splitlines() if "=" in l and not l.startswith("#"))["IMMICH_API_KEY"]
    r = c.get("/api/immich")
    assert key not in r.text and r.json()["key_hint"] == key[-4:]
    assert r.json()["from_env"] == {"url": True, "key": True} and r.json()["status"]["code"] == "ok"


def test_immich_connection_from_dashboard(dev_app, monkeypatch):
    """Without IMMICH_URL or IMMICH_API_KEY: startup possible, connection entered in the dashboard, tested before
    being saved, then picked up again at restart."""
    from fastapi.testclient import TestClient
    from conftest import ENV
    from taxonero.config import Settings
    from taxonero.web import create_app
    from taxonero.worker import Worker
    env = dict(l.split("=", 1) for l in ENV.read_text().splitlines() if "=" in l and not l.startswith("#"))
    monkeypatch.delenv("IMMICH_URL")
    monkeypatch.delenv("IMMICH_API_KEY")
    s, _ = dev_app
    s2 = Settings()
    w = Worker(s2)
    store_state = lambda: w.db.execute("SELECT value FROM state WHERE key = 'immich'").fetchone()
    assert not w.configured and w.run_once() == {"configured": False} and not w.check()
    c = TestClient(create_app(s2, w))
    assert c.get("/api/status").json()["immich_configured"] is False
    assert c.post("/api/immich/test", json={"url": env["IMMICH_URL"], "key": "mauvaise"}).json()["code"] == "bad_key"
    r = c.put("/api/immich", json={"url": env["IMMICH_URL"], "key": "mauvaise"})
    assert r.status_code == 422 and not r.json()["saved"] and not w.configured and store_state() is None
    r = c.put("/api/immich", json={"url": env["IMMICH_URL"] + "/", "key": env["IMMICH_API_KEY"]}).json()
    assert r["saved"] and r["configured"] and r["status"]["code"] == "ok" and r["url"] == env["IMMICH_URL"]
    assert c.get("/api/status").json()["immich_configured"] is True
    again = Worker(Settings())                                   # restart: connection picked up from the database
    assert again.configured and again.check()


def test_main_photo_can_be_chosen_and_reset(client):
    c, db = client
    db.execute("DELETE FROM cover_choice")                              # the copied dev database may have some
    db.commit()
    key, n = db.execute("SELECT taxon_key, count(DISTINCT asset_id) AS n FROM detection WHERE status = 'confirmed' "
                        "AND taxon_key IS NOT NULL GROUP BY taxon_key ORDER BY n DESC LIMIT 1").fetchone()
    assert n > 1
    auto = c.get(f"/api/species/{key}").json()["cover"]
    other = next(r[0] for r in db.execute("SELECT DISTINCT asset_id FROM detection WHERE taxon_key = ? AND status = 'confirmed'", (key,))
                 if r[0] != auto["asset_id"])
    r = c.put(f"/api/species/{key}/cover", json={"asset_id": other}).json()["cover"]
    assert r["asset_id"] == other and r["chosen"]
    assert next(s for s in c.get("/api/species").json() if s["taxon_key"] == key)["cover"] == other
    elsewhere = db.execute("SELECT asset_id FROM detection WHERE taxon_key != ? AND asset_id NOT IN "
                           "(SELECT asset_id FROM detection WHERE taxon_key = ?) LIMIT 1", (key, key)).fetchone()[0]
    assert c.put(f"/api/species/{key}/cover", json={"asset_id": elsewhere}).status_code == 422
    assert c.put(f"/api/species/{key}/cover", json={"asset_id": None}).json()["cover"]["asset_id"] == auto["asset_id"]


def test_higher_rank_pages(client):
    c, db = client
    birds = c.get("/api/higher/class/Aves").json()
    assert birds["rank"] == "class" and birds["species"]
    assert all(s["taxon"]["lineage"]["class"] == "Aves" for s in birds["species"])
    order = birds["species"][0]["taxon"]["lineage"]["order"]
    sub = c.get(f"/api/higher/order/{order}").json()["species"]
    assert sub and {s["taxon_key"] for s in sub} <= {s["taxon_key"] for s in birds["species"]}
    assert c.get("/api/higher/species/Aves").status_code == 404          # not a higher rank
    assert c.get("/api/higher/family/Inexistantidae").status_code == 404


def test_models_idle_minutes_is_editable(client):
    client, _ = client
    r = client.put("/api/settings", json={"models_idle_minutes": 0})
    assert r.status_code == 200, r.text
    assert client.get("/api/settings").json()["values"]["models_idle_minutes"] == 0
    assert client.put("/api/settings", json={"models_idle_minutes": 2000}).status_code != 200


def test_photo_scope_filters_every_listing(client):
    c, db = client
    key = db.execute("SELECT taxon_key FROM detection WHERE status = 'confirmed' AND taxon_key IS NOT NULL LIMIT 1").fetchone()[0]
    want = {r[0] for r in db.execute("SELECT DISTINCT asset_id FROM detection WHERE status = 'confirmed' AND taxon_key = ?", (key,))}
    got = {a["asset_id"] for a in c.get(f"/api/photos?filter=all&limit=500&taxon={key}").json()}
    assert got == want
    assert c.get(f"/api/photos/counts?taxon={key}").json()["all"] == len(want)
    assert sum(m["n"] for m in c.get(f"/api/photos/months?filter=all&taxon={key}").json()) == len(want)
    fam = c.get("/api/photos?filter=all&limit=500&rank=family&name=Canidae").json()
    assert fam and c.get("/api/photos?filter=all&rank=nope&name=x").status_code == 400
    day = db.execute("SELECT substr(taken_at, 1, 10) FROM asset WHERE taken_at IS NOT NULL LIMIT 1").fetchone()[0]
    assert all(a["taken_at"].startswith(day) for a in c.get(f"/api/photos?filter=all&limit=500&day={day}").json())
    n = c.get("/api/photos/counts").json()["identified"]
    assert n == db.execute("SELECT count(DISTINCT d.asset_id) FROM detection d JOIN asset a ON a.asset_id = d.asset_id "
                           "WHERE d.status = 'confirmed' AND d.taxon_key IS NOT NULL AND a.missing_at IS NULL").fetchone()[0]
    r = c.post("/api/photos/bulk", json={"filter": "all", "every": True, "scope": {"taxon": key}, "action": "reset"}).json()
    assert {d["asset_id"] for d in r["changed"]} <= want
    c.post("/api/photos/bulk-undo", json={"changed": r["changed"]})


def test_bulk_delete_and_undo(client):
    c, db = client
    aid = db.execute("SELECT asset_id FROM detection WHERE status = 'pending' LIMIT 1").fetchone()[0]
    snap = {r["id"]: dict(r) for r in db.execute("SELECT * FROM detection WHERE asset_id = ?", (aid,))}
    r = c.post("/api/photos/bulk", json={"asset_ids": [aid], "action": "delete"}).json()
    assert r["changed"] and not db.execute("SELECT 1 FROM detection WHERE asset_id = ? AND status != 'deleted'", (aid,)).fetchone()
    c.post("/api/photos/bulk-undo", json={"changed": r["changed"]})
    assert {r["id"]: dict(r) for r in db.execute("SELECT * FROM detection WHERE asset_id = ?", (aid,))} == snap


def test_reject_keeps_manual_validation_while_something_is_pending(client):
    c, db = client
    aid = db.execute("SELECT asset_id FROM detection WHERE status = 'pending' LIMIT 1").fetchone()[0]
    for flt in ("all", "user", "pending"):   # same rule whatever the filter shown
        r = c.post("/api/photos/bulk", json={"asset_ids": [aid], "filter": flt, "action": "reject"}).json()
        assert all(d["status"] == "pending" for d in r["changed"]), flt
        c.post("/api/photos/bulk-undo", json={"changed": r["changed"]})


def test_location_undo_restores_previous_values(client, monkeypatch):
    c, db = client
    aid = db.execute("SELECT asset_id FROM asset WHERE lat IS NULL LIMIT 1").fetchone()[0]
    r = c.post("/api/assets/location", json={"asset_ids": [aid], "lat": 45.0, "lon": 5.0}).json()
    assert r["changed"] == 1 and r["previous"][0]["lat"] is None
    assert db.execute("SELECT lat FROM asset WHERE asset_id = ?", (aid,)).fetchone()[0] == 45.0
    c.post("/api/assets/location-undo", json=r["previous"])
    assert tuple(db.execute("SELECT lat, geo_user FROM asset WHERE asset_id = ?", (aid,)).fetchone()) == (None, None)


def test_search_places_cameras_files(client):
    c, db = client
    name = db.execute("SELECT file_name FROM asset WHERE file_name IS NOT NULL LIMIT 1").fetchone()[0]
    assert any(f["file_name"] == name for f in c.get("/api/search", params={"q": name[:8]}).json()["files"])
    assert c.get("/api/search?q=%25%25").json()["files"] == []              # % is not a wildcard
    assert c.get("/api/search?q=a").json()["files"] == []


def test_export_selection_only(client):
    c, db = client
    aid = db.execute("SELECT asset_id FROM detection WHERE status = 'confirmed' LIMIT 1").fetchone()[0]
    text = c.post("/api/export.csv", json={"asset_ids": [aid]}).text
    assert aid in text and {l.split(",")[0] for l in text.splitlines()[1:]} == {aid}
    assert len(c.get("/api/export.csv").text.splitlines()) >= len(text.splitlines())


def test_logs_keep_recent_lines_and_mask_secrets(client):
    import logging
    from taxonero import logbuffer
    c, _ = client
    log, s = logging.getLogger("taxonero.test"), logbuffer.BUFFER.settings
    start = c.get("/api/logs").json()["entries"]
    after = start[-1]["id"] if start else 0
    s.dashboard_password = "motdepasse-secret"
    try:
        log.warning("failure with the key %s and %s", s.immich_api_key, s.dashboard_password)
        try:
            raise ValueError("boum")
        except ValueError:
            log.exception("stage failed")
        log.debug("trop bavard")
    finally:
        s.dashboard_password = ""
    got = c.get("/api/logs", params={"after": after}).json()["entries"]
    got = [e for e in got if e["name"] == "taxonero.test"]
    assert [e["level"] for e in got] == ["WARNING", "ERROR"]     # no DEBUG
    assert s.immich_api_key not in got[0]["msg"] and "motdepasse-secret" not in got[0]["msg"] and "***" in got[0]["msg"]
    assert "ValueError: boum" in got[1]["msg"]                   # trace d'erreur comprise
    assert c.get("/api/logs", params={"after": got[-1]["id"]}).json()["entries"] == []


def test_videos_not_analysed_are_not_counted_as_missing(client, dev_app):
    c, db = client
    s, _ = dev_app
    db.execute("UPDATE asset SET missing_at = 1, kind = 'VIDEO' WHERE rowid = (SELECT min(rowid) FROM asset)")
    db.commit()
    s.analyze_videos = True
    assert c.get("/api/status").json()["missing"] == 1
    s.analyze_videos = False
    assert c.get("/api/status").json()["missing"] == 0
