"""Test d'intégration contre l'Immich de dev (dev/docker-compose.yml + dev/bootstrap.sh).

Vérifie que sync est idempotent et que uninstall restaure exactement l'état utilisateur.
Ignoré si dev/.env.dev est absent.

Immich 3.3.5 peut perdre une écriture si la suivante arrive pendant la tâche d'écriture du sidecar XMP de la
précédente (voir README, « Limites connues »). L'écart fixe WRITE_GAP suffit d'habitude, pas sur une machine
chargée : le test attend donc en plus que les files de tâches d'Immich soient vides avant chaque écriture et
chaque vérification (endpoint /queues, réservé aux admins et encore « Alpha » : utilisé ici seulement).
"""

import json
import time
from pathlib import Path

import httpx
import pytest

from taxonero import store
from taxonero.immich.client import ImmichClient
from taxonero.sync import BLOCK_START, WRITE_GAP, Syncer

ENV = Path(__file__).parent.parent / "dev" / ".env.dev"
pytestmark = pytest.mark.skipif(not ENV.exists(), reason="Immich de dev non initialisé")

PARUS = {"gbif_key": 9705453, "scientific_name": "Parus major", "rank": "species",
         "lineage": {"kingdom": "Animalia", "class": "Aves", "order": "Passeriformes", "family": "Paridae",
                     "genus": "Parus", "species": "Parus major"},
         "vernacular": {"fr": "Mésange charbonnière"}, "iucn": "LEAST_CONCERN",
         "wikipedia": {"fr": "https://fr.wikipedia.org/wiki/M%C3%A9sange_charbonni%C3%A8re"}}


QUEUES = ("sidecar", "metadataExtraction")


def _idle(c: httpx.Client, timeout: float = 60) -> None:
    """Attend que les tâches sidecar et métadonnées d'Immich soient terminées (deux lectures vides de suite)."""
    end, calm = time.monotonic() + timeout, 0
    while time.monotonic() < end:
        busy = sum(q["statistics"][k] for q in c.get("/queues").json() if q["name"] in QUEUES
                   for k in ("active", "waiting", "delayed"))
        calm = calm + 1 if not busy else 0
        if calm >= 2:
            return
        time.sleep(0.25)
    raise TimeoutError("files de tâches Immich toujours occupées")


class PatientSyncer(Syncer):
    """Syncer qui attend, avant chaque écriture, que la précédente soit entièrement traitée par Immich."""

    def __init__(self, *args, admin: httpx.Client, **kwargs):
        super().__init__(*args, **kwargs)
        self._admin = admin

    def _write(self, asset_id: str, fn, *args) -> None:
        _idle(self._admin)
        super()._write(asset_id, fn, *args)


def _env() -> dict:
    return dict(line.split("=", 1) for line in ENV.read_text().splitlines() if "=" in line and not line.startswith("#"))


@pytest.fixture
def admin():
    """Session admin de dev, pour simuler les actions manuelles de l'utilisateur."""
    env = _env()
    url = env["IMMICH_URL"] + "/api"
    tok = httpx.post(f"{url}/auth/login", json={"email": "dev@example.invalid", "password": "dev-password"}).json()["accessToken"]
    with httpx.Client(base_url=url, headers={"Authorization": f"Bearer {tok}"}) as c:
        _reset(c)
        yield c
        _reset(c)


def _reset(c: httpx.Client) -> None:
    """Supprime les tags racines de test (et leurs enfants) ; vide la description du premier asset."""
    for t in c.get("/tags").json():
        if t["value"] in ("Nature", "Perso"):
            c.delete(f"/tags/{t['id']}")
    aid = c.post("/search/metadata", json={"size": 1}).json()["assets"]["items"][0]["id"]
    c.put(f"/assets/{aid}", json={"description": ""})
    time.sleep(WRITE_GAP)
    _idle(c)


def test_sync_idempotent_and_uninstall_restores(admin, tmp_path):
    env = _env()
    client = ImmichClient(env["IMMICH_URL"], env["IMMICH_API_KEY"], dry_run=False)
    assert client.check(), client.errors
    asset = next(client.iter_assets())
    aid = asset["id"]

    # État « utilisateur » : description perso + tag perso.
    admin.put(f"/assets/{aid}", json={"description": "Ma photo du jardin"}).raise_for_status()
    _idle(admin)
    user_tag = admin.put("/tags", json={"tags": ["Perso/Jardin"]}).raise_for_status().json()
    user_tag_id = next(t["id"] for t in user_tag if t["value"] == "Perso/Jardin")
    admin.put(f"/tags/{user_tag_id}/assets", json={"ids": [aid]}).raise_for_status()
    # En prod, SETTLE_SECONDS empêche d'écrire juste après une modification de l'utilisateur
    # (course Immich) ; ici on la désactive et on respecte seulement l'écart minimal.
    time.sleep(WRITE_GAP)
    _idle(admin)

    db = store.connect(tmp_path / "t.sqlite")
    store.upsert_asset(db, asset)
    store.save_taxon(db, PARUS["gbif_key"], json.dumps(PARUS))
    db.execute("INSERT INTO detection (asset_id, x, y, w, h, source, status, confirmed_by, taxon_key, taxon_rank, score, created_at) "
               "VALUES (?, 0.1, 0.1, 0.5, 0.5, 'auto', 'confirmed', 'user', ?, 'species', 0.9, ?)",
               (aid, PARUS["gbif_key"], time.time()))

    s1 = PatientSyncer(db, client, settle_seconds=0, admin=admin).sync_all()
    assert (s1.tags_added, s1.descriptions) == (1, 1)
    _idle(admin)
    info = admin.get(f"/assets/{aid}").json()
    desc = info["exifInfo"]["description"]
    assert desc.startswith("Ma photo du jardin") and BLOCK_START in desc and "Parus major" in desc
    tag_values = {t["value"] for t in info.get("tags", [])}
    assert "Perso/Jardin" in tag_values
    assert "Nature/Aves/Passeriformes/Paridae/Parus/Mésange charbonnière (Parus major)" in tag_values

    s2 = PatientSyncer(db, client, settle_seconds=0, admin=admin).sync_all()
    assert (s2.tags_added, s2.tags_removed, s2.descriptions) == (0, 0, 0), "sync non idempotent"

    PatientSyncer(db, client, settle_seconds=0, admin=admin).uninstall()
    _idle(admin)
    info = admin.get(f"/assets/{aid}").json()
    assert info["exifInfo"]["description"] == "Ma photo du jardin"
    assert {t["value"] for t in info.get("tags", [])} == {"Perso/Jardin"}
    assert not [t for t in admin.get("/tags").json() if t["value"].startswith("Nature")]


def test_dashboard_undo_keeps_user_data_and_stops_writes(admin, dev_app):
    """Retrait depuis le dashboard, contre un vrai Immich : tout ce qui a été posé part, rien de l'utilisateur, et le
    cycle suivant ne repose rien tant qu'on ne reprend pas."""
    from fastapi.testclient import TestClient
    from taxonero.web import create_app
    s, w = dev_app
    s.dry_run = w.client.dry_run = False
    aid = next(w.client.iter_assets())["id"]
    admin.put(f"/assets/{aid}", json={"description": "Ma photo du jardin"}).raise_for_status()
    _idle(admin)
    user_tag = admin.put("/tags", json={"tags": ["Perso/Jardin"]}).raise_for_status().json()
    user_id = next(t["id"] for t in user_tag if t["value"] == "Perso/Jardin")
    admin.put(f"/tags/{user_id}/assets", json={"ids": [aid]}).raise_for_status()
    time.sleep(WRITE_GAP)
    _idle(admin)
    w.db.execute("DELETE FROM detection")
    store.save_taxon(w.db, PARUS["gbif_key"], json.dumps(PARUS))
    w.db.execute("INSERT INTO detection (asset_id, x, y, w, h, source, status, confirmed_by, taxon_key, taxon_rank, score, created_at) "
                 "VALUES (?, 0.1, 0.1, 0.5, 0.5, 'auto', 'confirmed', 'user', ?, 'species', 0.9, ?)", (aid, PARUS["gbif_key"], time.time()))
    w.db.execute("DELETE FROM written")
    w.db.commit()
    PatientSyncer(w.db, w.client, settle_seconds=0, admin=admin).sync_all()
    _idle(admin)
    assert BLOCK_START in admin.get(f"/assets/{aid}").json()["exifInfo"]["description"]
    c = TestClient(create_app(s, w))

    assert c.post("/api/immich/uninstall", json={}).status_code == 400            # sans confirmation : rien
    s.dry_run = True
    assert c.post("/api/immich/uninstall", json={"confirm": "remove-from-immich"}).status_code == 409   # DRY_RUN : rien
    s.dry_run = False
    saved = w.db.execute("SELECT * FROM written").fetchall()
    w.db.execute("UPDATE written SET immich_id = 'autre-' || immich_id WHERE kind IN ('tag', 'tag_asset')")
    w.db.commit()
    r = c.post("/api/immich/uninstall", json={"confirm": "remove-from-immich"})    # autre serveur : refus, traces intactes
    assert r.status_code == 409 and r.json()["detail"]["code"] == "undo_other_server"
    w.db.execute("DELETE FROM written")
    w.db.executemany("INSERT INTO written VALUES (?, ?, ?, ?, ?)", [tuple(x) for x in saved])
    w.db.commit()

    assert c.post("/api/immich/uninstall", json={"confirm": "remove-from-immich"}).status_code == 200
    for _ in range(300):
        state = c.get("/api/immich/uninstall").json()
        if state["state"] != "running":
            break
        time.sleep(0.2)
    assert state["state"] == "done" and state["remaining"] == 0 and state["paused"], state
    assert (state["photos"], state["descriptions"]) == (0, 0)
    _idle(admin)
    info = admin.get(f"/assets/{aid}").json()
    assert info["exifInfo"]["description"] == "Ma photo du jardin"
    assert {t["value"] for t in info.get("tags", [])} == {"Perso/Jardin"}
    values = {t["value"] for t in admin.get("/tags").json()}
    assert "Perso/Jardin" in values and not any(v == "Nature" or v.startswith("Nature/") for v in values)

    assert w.sync() is None                                                        # suspendu : le cycle ne repose rien
    _idle(admin)
    assert {t["value"] for t in admin.get(f"/assets/{aid}").json().get("tags", [])} == {"Perso/Jardin"}
    assert c.get("/api/status").json()["writes_paused"] is True
    assert c.post("/api/immich/resume-writes").json()["paused"] is False


def test_tags_deleted_by_hand_then_resync_then_uninstall(admin, tmp_path):
    """Tags de l'outil supprimés à la main dans Immich, puis recréés par la synchro (nouveaux ids) : le retrait doit
    quand même tout enlever, sans viser les anciens ids (400 d'Immich, tags laissés en place)."""
    env = _env()
    client = ImmichClient(env["IMMICH_URL"], env["IMMICH_API_KEY"], dry_run=False)
    assert client.check(), client.errors
    aid = next(client.iter_assets())["id"]
    db = store.connect(tmp_path / "t.sqlite")
    store.upsert_asset(db, client.get_asset(aid))
    store.save_taxon(db, PARUS["gbif_key"], json.dumps(PARUS))
    db.execute("INSERT INTO detection (asset_id, x, y, w, h, source, status, confirmed_by, taxon_key, taxon_rank, score, created_at) "
               "VALUES (?, 0.1, 0.1, 0.5, 0.5, 'auto', 'confirmed', 'user', ?, 'species', 0.9, ?)",
               (aid, PARUS["gbif_key"], time.time()))
    nature = lambda: [t for t in admin.get("/tags").json() if t["value"].split("/")[0] == "Nature"]

    PatientSyncer(db, client, settle_seconds=0, admin=admin).sync_all()
    _idle(admin)
    admin.delete(f"/tags/{next(t['id'] for t in nature() if t['value'] == 'Nature')}").raise_for_status()
    admin.put(f"/assets/{aid}", json={"description": ""}).raise_for_status()
    time.sleep(WRITE_GAP)
    _idle(admin)
    assert not nature()

    PatientSyncer(db, client, settle_seconds=0, admin=admin).sync_all()
    _idle(admin)
    assert len(nature()) == 6
    traced = {r["immich_id"] for r in db.execute("SELECT immich_id FROM written WHERE kind IN ('tag', 'tag_asset')")}
    assert traced == {t["id"] for t in nature()}, "anciens ids encore tracés"

    u = PatientSyncer(db, client, settle_seconds=0, admin=admin)
    u.uninstall()
    _idle(admin)
    assert not nature() and u.remaining == 0
    assert admin.get(f"/assets/{aid}").json()["exifInfo"]["description"] in ("", None)
    assert not db.execute("SELECT * FROM written").fetchall()
