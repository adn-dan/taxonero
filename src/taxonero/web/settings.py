"""Routes de la page Paramètres : réglages, source des photos, connexion à Immich, modèles."""

import json

import httpx
from fastapi import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import store
from ..config import EDITABLE, validate_overrides
from ..curation import drop_unwanted
from ..immich.client import probe
from ..immich.contract import OPTIONAL_PERMISSIONS, REQUIRED_PERMISSIONS
from ..sources import available, default_path, kind, source_path
from ..sync import Syncer
from ..worker import Busy
from .context import fail


UNDO_CONFIRM = "remove-from-immich"   # envoyé par le dashboard une fois le mot de confirmation saisi


def register(app, c) -> None:
    s, w, db, auth = c.s, c.w, c.db, c.auth

    # -- retrait de ce que l'outil a écrit dans Immich ------------------------------

    class UndoIn(BaseModel):
        confirm: str = ""

    @app.get("/api/immich/uninstall", dependencies=[Depends(auth)])
    def undo_state():
        return w.undo_summary(db)

    @app.post("/api/immich/uninstall", dependencies=[Depends(auth)])
    def undo_start(body: UndoIn):
        """Refuse tout ce qui rendrait le retrait inutile ou dangereux, avant la moindre écriture."""
        if body.confirm != UNDO_CONFIRM:
            fail(400, "undo_confirm", "confirmation manquante")
        state = w.undo_summary(db)
        if state["state"] == "running":
            fail(409, "undo_running", "retrait déjà en cours")
        if s.source != "immich":
            fail(409, "local_source", f"source : {s.source} (pas Immich)")
        if s.dry_run:
            fail(409, "undo_dry_run", "DRY_RUN=true : rien ne peut être écrit dans Immich")
        if not w.client.writable:
            fail(409, "undo_readonly", "serveur Immich non validé (lecture seule)")
        if not (state["photos"] or state["tags"]):
            fail(409, "undo_empty", "rien à retirer")
        try:
            same = Syncer(db, w.client).same_server()
        except httpx.HTTPError as e:
            fail(502, "undo_unreachable", f"Immich injoignable : {e}")
        if not same:
            fail(409, "undo_other_server", "aucun des tags écrits n'existe sur ce serveur : autre Immich ou autre compte ?")
        w.start_uninstall(state["photos"])
        return w.undo_summary(db)

    @app.post("/api/immich/resume-writes", dependencies=[Depends(auth)])
    def undo_resume():
        if w.undo.get("state") == "running":
            fail(409, "undo_running", "retrait en cours")
        try:
            w.resume_writes()
        except Busy as e:
            fail(409, "cycle_running", str(e))
        return w.undo_summary(db)

    @app.get("/api/settings", dependencies=[Depends(auth)])
    def get_settings():
        saved = json.loads(store.get_state(db, "settings") or "{}")
        return {"values": {k: getattr(s, k) for k in EDITABLE},
                "defaults": w.env_defaults,
                "saved": sorted(saved),
                "env": {"dry_run": s.dry_run, "names_lang": s.names_lang, "device": s.device,
                        "tag_root": s.tag_root, "open_detector": s.open_detector}}

    # -- connexion à Immich (Paramètres > Immich) ----------------------------------
    # La clé n'est jamais renvoyée au navigateur : seulement ses 4 derniers caractères.

    def immich_info() -> dict:
        if s.source != "immich":   # dossier ou logiciel photo : pas de connexion à régler, seulement son état
            return {"source": s.source, "kind": kind(s.source), "path": str(source_path(s) or ""), "dry_run": s.dry_run,
                    "status": {"code": "ok" if w.client.writable else ("readonly" if w.client.version else "missing"),
                               **w.client.status()}}
        key = s.immich_api_key
        return {"url": s.immich_url, "key_hint": key[-4:] if len(key) >= 12 else ("…" if key else ""),
                "from_env": w.env_immich, "configured": w.configured,
                "required": list(REQUIRED_PERMISSIONS), "optional": list(OPTIONAL_PERMISSIONS),
                "status": {"code": "ok" if w.client.writable else ("readonly" if w.client.version else "missing"),
                           **w.client.status()} if w.configured else {"code": "missing"}}

    # -- source des photos (Paramètres) ------------------------------------------------

    @app.get("/api/sources", dependencies=[Depends(auth)])
    def get_sources():
        """Sources proposées, avec le chemin à préremplir pour chacune : le chemin en cours, sinon le dossier des
        photos ou la bibliothèque du logiciel à son emplacement habituel."""
        def suggested(o: dict):
            if o["id"] == s.source:
                return source_path(s)
            if o["kind"] == "folder":
                return s.photos_dir
            return default_path(o["id"]) if o["kind"] == "catalog" and o["available"] else None

        options = available(s.edition)
        for o in options:
            o["path"] = str(suggested(o) or "")
        return {"current": s.source, "locked": w.env_source, "edition": s.edition, "dry_run": s.dry_run,
                "dry_run_editable": s.edition == "desktop" and "dry_run" not in s.model_fields_set, "options": options}

    class SourceIn(BaseModel):
        source: str | None = None
        path: str | None = None   # dossier des photos ou bibliothèque du logiciel, selon la source
        dry_run: bool | None = None

    @app.put("/api/source", dependencies=[Depends(auth)])
    def put_source(body: SourceIn):
        try:
            if body.dry_run is not None:
                w.set_dry_run(body.dry_run)
            if body.source:
                w.set_source(body.source, body.path)
        except Busy as e:
            fail(409, "cycle_running", str(e))
        except ValueError as e:
            fail(422, "invalid_source", str(e))
        except (httpx.HTTPError, RuntimeError, OSError) as e:
            fail(502, "source_unreachable", str(e))
        return get_sources()

    @app.get("/api/immich", dependencies=[Depends(auth)])
    def get_immich():
        return immich_info()

    class ImmichIn(BaseModel):
        url: str = ""
        key: str = ""   # vide : garder la clé enregistrée

    @app.post("/api/immich/test", dependencies=[Depends(auth)])
    def test_immich(body: ImmichIn):
        """Essaie une connexion sans rien enregistrer ni écrire dans Immich."""
        try:
            url, key = w.credentials(body.url, body.key)
        except ValueError as e:
            return {"code": "key_required", "errors": [str(e)]}
        client, state = probe(url, key)
        if client:
            client.close()
        return state

    @app.put("/api/immich", dependencies=[Depends(auth)])
    def put_immich(body: ImmichIn):
        """Enregistre la connexion si Immich répond et accepte la clé ; sinon rien ne change."""
        if s.source != "immich":
            fail(409, "local_source", f"source : {s.source} (pas Immich)")
        try:
            state = w.connect(body.url, body.key)
        except Busy as e:   # même forme qu'un échec de connexion : affiché par la fiche Immich
            return JSONResponse({"code": "cycle_running", "errors": [], "saved": False, "message": str(e)}, status_code=409)
        if state["code"] not in ("ok", "readonly"):
            return JSONResponse({**state, "saved": False}, status_code=422)
        return {**immich_info(), "saved": True, "test": state}

    @app.get("/api/models", dependencies=[Depends(auth)])
    def models():
        """Modèles d'IA, matériel de calcul réellement utilisé, vitesse mesurée."""
        info = w.models_info()
        info["outdated"] = db.execute("SELECT count(*) FROM asset WHERE analyzed_at IS NOT NULL AND analyzer IS NOT ? "
                                      "AND missing_at IS NULL", (info["analyzer_id"],)).fetchone()[0]
        return info

    @app.put("/api/settings", dependencies=[Depends(auth)])
    def put_settings(values: dict):
        """Enregistre des réglages (null = revenir à la valeur par défaut / de l'environnement)."""
        reset = [k for k, v in values.items() if v is None]
        try:
            changes = validate_overrides(s, {k: v for k, v in values.items() if v is not None})
            if any(k not in EDITABLE for k in reset):
                raise ValueError("réglage inconnu")
        except ValueError as e:
            fail(422, "invalid_value", str(e))
        before = (s.exclude_extinct, s.hide_absent)
        saved = json.loads(store.get_state(db, "settings") or "{}")
        saved.update(changes)
        for k in reset:
            saved.pop(k, None)
            changes[k] = w.env_defaults[k]
        store.set_state(db, "settings", json.dumps(saved))
        for k, v in changes.items():
            setattr(s, k, v)
        if (s.exclude_extinct, s.hide_absent) != before:
            w.start_prune()   # sinon appliqué au cycle suivant (« pruned_with » diffère)
        if {"detection", "detect_groups"} & changes.keys():
            drop_unwanted(db, s)   # détections automatiques à relire devenues hors réglage, retirées tout de suite
            db.commit()
        return get_settings()
