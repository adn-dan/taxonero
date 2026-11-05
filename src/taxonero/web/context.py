"""Contexte partagé par les routes du dashboard : réglages, worker, base (une connexion par thread), taxonomie,
authentification, et les conversions communes (taxon, détection, photo)."""

import copy
import json
import logging
import secrets
import threading
import time
from types import SimpleNamespace

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from .. import store
from ..autoconfirm import auto_blockers, auto_threshold, blocked_groups, group_of
from ..config import Settings
from ..curation import candidate_filter
from ..taxonomy import TAXON_VERSION, Taxonomy, to_json
from ..worker import Worker

log = logging.getLogger(__name__)

# Mots de passe faux tolérés par adresse IP sur AUTH_WINDOW secondes (voir auth).
AUTH_MAX_FAILURES = 10
AUTH_WINDOW = 60


def fail(status: int, code: str, message: str, **params):
    """Erreur affichée dans le dashboard : un code (traduit côté navigateur), ses valeurs, et le texte français
    en repli."""
    raise HTTPException(status, {"code": code, "message": message, **params})


class BoxIn(BaseModel):
    x: float
    y: float
    w: float
    h: float


class TaxonIn(BaseModel):
    gbif_key: int | None = None
    name: str | None = None


class _ThreadLocalDB:
    """Se comporte comme une sqlite3.Connection, mais en ouvre une par thread."""

    def __init__(self, path):
        self._path, self._local = path, threading.local()

    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = store.connect(self._path)
        return c

    def __getattr__(self, name):
        return getattr(self._conn(), name)


def make_context(s: Settings, w: Worker, desktop: bool) -> SimpleNamespace:
    """Objets et fonctions communs aux modules de routes (attributs du contexte renvoyé)."""
    # Une connexion SQLite par thread : FastAPI sert les requêtes en parallèle dans un pool de threads,
    # et une connexion partagée entre threads simultanés lève « bad parameter or other API misuse ».
    db = _ThreadLocalDB(s.db_path)
    # Taxonomie propre au web (sa base est thread-locale), distincte de celle du worker.
    tx = Taxonomy(db, langs=s.langs, user_agent=s.user_agent, backbone=s.backbone_path if s.gbif_local else None, fail_fast=True)
    drop = candidate_filter(s, tx)
    lock = threading.Lock()
    range_lock = threading.Lock()  # une aire à la fois : évite de lancer deux fois les 16 requêtes
    security = HTTPBasic(auto_error=False)
    # URL des liens « ouvrir dans Immich » : peut changer quand la connexion est réglée depuis le dashboard.
    public_immich = lambda: (s.immich_public_url or s.immich_url).rstrip("/")
    # Lien vers la photo dans sa source (Immich) ; aucun pour un dossier ou un logiciel photo.
    source_link = lambda aid: f"{public_immich()}/photos/{aid}" if s.source == "immich" else None

    # Contre la force brute : mots de passe faux par adresse IP, sur une fenêtre glissante. Au-delà de la limite,
    # tout essai de cette adresse est refusé sans être vérifié (même juste) jusqu'à la fin de la fenêtre.
    failures: dict[str, list[float]] = {}
    failures_lock = threading.Lock()

    def auth(request: Request, creds: HTTPBasicCredentials | None = Depends(security)) -> None:
        if s.dashboard_no_auth or desktop:   # bureau : jeton de session vérifié par desktop_session
            return
        if not s.dashboard_password:
            raise HTTPException(503, "DASHBOARD_PASSWORD non défini (ou DASHBOARD_NO_AUTH=true)")
        ip, now = request.client.host if request.client else "", time.monotonic()
        with failures_lock:
            recent = [t for t in failures.get(ip, []) if now - t < AUTH_WINDOW]
            if recent:
                failures[ip] = recent
            else:
                failures.pop(ip, None)
        if len(recent) >= AUTH_MAX_FAILURES:
            raise HTTPException(429, "trop d'essais de mot de passe : réessayer dans une minute",
                                headers={"Retry-After": str(int(AUTH_WINDOW - (now - recent[0])) + 1)})
        # En octets : compare_digest refuse les chaînes non ASCII (TypeError, donc erreur 500).
        if not creds or not (secrets.compare_digest(creds.username.encode(), b"nature")
                             and secrets.compare_digest(creds.password.encode(), s.dashboard_password.encode())):
            if creds:   # sans identifiants : première requête du navigateur, avant la fenêtre de connexion
                with failures_lock:
                    failures.setdefault(ip, []).append(now)
            raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="taxonero"'})

    def taxon(key: int | None) -> dict | None:
        t = store.load_taxon(db, key) if key else None
        if t and t.get("v", 0) < TAXON_VERSION:
            # Ancien format (sans noms multilingues ni Wikispecies) : ré-enrichi à la volée.
            fresh = tx.match(t["scientific_name"], kingdom=t.get("lineage", {}).get("kingdom"))
            if fresh and fresh.gbif_key == key:
                store.save_taxon(db, key, to_json(fresh))
                db.commit()
                t = store.load_taxon(db, key)
        return t

    blocked_cache = {"at": 0.0, "groups": set()}

    def blocked_now() -> set[str]:
        """Groupes exclus de la validation automatique, recalculés au plus toutes les 10 s (listes de photos)."""
        if time.monotonic() - blocked_cache["at"] > 10:
            blocked_cache.update(at=time.monotonic(), groups=blocked_groups(db, s))
        return blocked_cache["groups"]

    def det_json(r, threshold: float | None = None) -> dict:
        d = dict(r)
        d["candidates"] = json.loads(d["candidates"] or "[]")
        d["candidates_out"] = json.loads(d.get("candidates_out") or "[]")
        d["taxon"] = taxon(d["taxon_key"])
        # Proposition corrigée par la série (lissage, series.py) : ce que le modèle proposait au départ.
        if d.get("smoothed") and d["status"] == "pending" and d.get("model_key") and d["model_key"] != d["taxon_key"]:
            d["model_taxon"] = taxon(d["model_key"])
        if d["status"] == "pending":
            # Pourquoi elle n'a pas été validée automatiquement (affiché dans la fiche de relecture).
            runner_up = d["candidates"][1]["p"] if len(d["candidates"]) > 1 else 0.0
            name = (d["taxon"] or {}).get("scientific_name")
            group = group_of((d["taxon"] or {}).get("lineage"))
            d["why_pending"] = ([{"code": "audit"}] if d.get("audit") else
                                [] if d["source"] == "manual" else
                                auto_blockers(d["taxon_rank"], d["plausibility"], d["score"], name, runner_up,
                                              d["box_label"], threshold, s.auto_confirm_max_runner_up, group,
                                              blocked_now() if threshold is not None else set(), d.get("frame_support"))
                                or [{"code": "reset_by_hand" if d["reviewed_at"] else "before_rule"}])
        return d

    def ensure_taxon(key: int | None, name: str | None, kingdom: str | None = None) -> int | None:
        if key and taxon(key):
            return key
        try:
            t = tx.match(name, kingdom=kingdom) if name else None
            if t is None and key:
                sp = tx.get(f"https://api.gbif.org/v1/species/{key}")
                t = tx.match(sp.get("canonicalName") or sp["scientificName"])
        except httpx.HTTPError as e:   # fiche récupérée plus tard : l'action en cours ne doit pas échouer pour ça
            log.warning("fiche du taxon %s non récupérée : %s", key or name, e)
            return None
        if t is None:
            return None
        store.save_taxon(db, t.gbif_key, to_json(t))
        db.commit()
        return t.gbif_key

    # Le worker a sa propre connexion SQLite, utilisée par le cycle dans un autre fil : le web n'y touche pas (un commit
    # fait ici validerait une transaction du cycle à moitié faite). Ses méthodes sont appelées avec la base du web.
    def analyzer():
        """Analyseur du worker (modèles partagés) dont la décision (GBIF, filtre des candidats) passe par la base du web."""
        a = copy.copy(w.analyzer)
        a.taxonomy, a.drop, a.tolerant = tx, drop, True
        return a

    def original(asset_id: str) -> bytes | None:
        """Original de la photo (Worker.original), le type de l'asset lu dans la base du web."""
        row = db.execute("SELECT kind FROM asset WHERE asset_id = ?", (asset_id,)).fetchone()
        return w.original(asset_id, (row and row["kind"]) or "IMAGE")

    def asset_json(asset_id: str, a: dict | None = None, threshold: float | None = None) -> dict:
        a = a or dict(db.execute("SELECT * FROM asset WHERE asset_id = ?", (asset_id,)).fetchone() or {})
        if not a:
            raise HTTPException(404)
        if threshold is None:
            threshold = auto_threshold(db, s)
        a["detections"] = [det_json(r, threshold) for r in db.execute(
            "SELECT * FROM detection WHERE asset_id = ? AND status != 'deleted' ORDER BY status = 'rejected', w * h DESC", (asset_id,))]
        a["immich_link"] = source_link(asset_id)
        return a

    def set_status(det_id: int, status: str, key: int | None = None, rank: str | None = None):
        row = db.execute("SELECT * FROM detection WHERE id = ?", (det_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        db.execute("UPDATE detection SET status = ?, confirmed_by = ?, reviewed_at = ?, "
                   "taxon_key = COALESCE(?, taxon_key), taxon_rank = COALESCE(?, taxon_rank) WHERE id = ?",
                   (status, None if status == "pending" else "user", time.time(), key, rank, det_id))
        db.commit()
        return det_json(db.execute("SELECT * FROM detection WHERE id = ?", (det_id,)).fetchone())

    return SimpleNamespace(s=s, w=w, db=db, tx=tx, drop=drop, lock=lock, range_lock=range_lock, auth=auth,
                           public_immich=public_immich, source_link=source_link, taxon=taxon, blocked_now=blocked_now,
                           det_json=det_json, ensure_taxon=ensure_taxon, asset_json=asset_json, set_status=set_status,
                           analyzer=analyzer, original=original)
