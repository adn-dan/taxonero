"""Routes of the Settings page: settings, photo source, Immich connection, models."""

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


UNDO_CONFIRM = "remove-from-immich"   # sent by the dashboard once the confirmation word is typed


def register(app, c) -> None:
    s, w, db, auth = c.s, c.w, c.db, c.auth

    # -- removal of what the tool wrote to Immich ------------------------------

    class UndoIn(BaseModel):
        confirm: str = ""

    @app.get("/api/immich/uninstall", dependencies=[Depends(auth)])
    def undo_state():
        return w.undo_summary(db)

    @app.post("/api/immich/uninstall", dependencies=[Depends(auth)])
    def undo_start(body: UndoIn):
        """Refuse anything that would make the removal useless or dangerous, before any write."""
        if body.confirm != UNDO_CONFIRM:
            fail(400, "undo_confirm", "confirmation missing")
        state = w.undo_summary(db)
        if state["state"] == "running":
            fail(409, "undo_running", "removal already running")
        accounts = w.immich_sources()
        if not accounts:
            fail(409, "local_source", f"source: {s.source} (not Immich)", source=s.source)
        if s.dry_run:
            fail(409, "undo_dry_run", "DRY_RUN=true: nothing can be written to Immich")
        if not all(c.writable for _, c in accounts):
            fail(409, "undo_readonly", "Immich server not validated (read-only)")
        if not (state["photos"] or state["tags"]):
            fail(409, "undo_empty", "nothing to remove")
        for sid, c in accounts:
            try:
                same = Syncer(db, c, source=sid).same_server()
            except httpx.HTTPError as e:
                fail(502, "undo_unreachable", f"Immich unreachable: {e}", error=str(e))
            if not same:
                fail(409, "undo_other_server", "none of the written tags exists on this server: another Immich or another account?")
        w.start_uninstall(state["photos"])
        return w.undo_summary(db)

    @app.post("/api/immich/resume-writes", dependencies=[Depends(auth)])
    def undo_resume():
        if w.undo.get("state") == "running":
            fail(409, "undo_running", "removal running")
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

    # -- Immich connection (Settings > Immich) ----------------------------------
    # The key is never sent back to the browser: only its last 4 characters.

    def immich_info() -> dict:
        if s.source != "immich":   # folder or photo application: no connection to set, only its state
            return {"source": s.source, "kind": kind(s.source), "path": str(source_path(s) or ""), "dry_run": s.dry_run,
                    "status": {"code": "ok" if w.client.writable else ("readonly" if w.client.version else "missing"),
                               **w.client.status()}}
        key = s.immich_api_key
        return {"url": s.immich_url, "key_hint": key[-4:] if len(key) >= 12 else ("…" if key else ""),
                "from_env": w.env_immich, "configured": w.main_configured,
                "required": list(REQUIRED_PERMISSIONS), "optional": list(OPTIONAL_PERMISSIONS),
                "status": {"code": "ok" if w.client.writable else ("readonly" if w.client.version else "missing"),
                           **w.client.status()} if w.main_configured else {"code": "missing"}}

    # -- photo source (Settings) ------------------------------------------------

    @app.get("/api/sources", dependencies=[Depends(auth)])
    def get_sources():
        """Sources offered, with the path to prefill for each: the current path, otherwise the usual location of the
        photo folder or of the application library."""
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
                "dry_run_editable": s.edition == "desktop" and "dry_run" not in s.model_fields_set, "options": options,
                "extra": w.sources_status(db)}

    class ExtraIn(BaseModel):
        type: str
        path: str = ""    # folder or application library
        url: str = ""     # Immich account
        key: str = ""
        label: str = ""

    @app.post("/api/sources/extra", dependencies=[Depends(auth)])
    def add_extra(body: ExtraIn):
        """Another source next to the main one: a second Immich account, another folder, a photo application."""
        try:
            w.add_source(body.model_dump())
        except Busy as e:
            fail(409, "cycle_running", str(e))
        except ValueError as e:
            fail(422, "invalid_source", str(e))
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                fail(422, "invalid_source", "Immich refused the API key")
            fail(502, "source_unreachable", str(e))
        except (httpx.HTTPError, RuntimeError, OSError) as e:
            fail(502, "source_unreachable", str(e))
        return get_sources()

    class KeyIn(BaseModel):
        key: str

    @app.put("/api/sources/extra/{sid}", dependencies=[Depends(auth)])
    def extra_key(sid: str, body: KeyIn):
        """New API key for an added Immich account: same source, its reviews kept."""
        try:
            w.set_source_key(sid, body.key)
        except KeyError:
            fail(404, "unknown_source", "unknown source")
        except Busy as e:
            fail(409, "cycle_running", str(e))
        except ValueError as e:
            fail(422, "invalid_source", str(e))
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                fail(422, "invalid_source", "Immich refused the API key")
            fail(502, "source_unreachable", str(e))
        except (httpx.HTTPError, RuntimeError, OSError) as e:
            fail(502, "source_unreachable", str(e))
        return get_sources()

    @app.delete("/api/sources/extra/{sid}", dependencies=[Depends(auth)])
    def remove_extra(sid: str):
        try:
            w.remove_source(sid)
        except KeyError:
            fail(404, "unknown_source", "unknown source")
        except Busy as e:
            fail(409, "cycle_running", str(e))
        return get_sources()

    class SourceIn(BaseModel):
        source: str | None = None
        path: str | None = None   # photo folder or application library, depending on the source
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
        key: str = ""   # empty: keep the saved key

    @app.post("/api/immich/test", dependencies=[Depends(auth)])
    def test_immich(body: ImmichIn):
        """Try a connection without saving or writing anything to Immich."""
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
        """Save the connection if Immich answers and accepts the key; otherwise nothing changes."""
        if s.source != "immich":
            fail(409, "local_source", f"source: {s.source} (not Immich)", source=s.source)
        try:
            state = w.connect(body.url, body.key)
        except Busy as e:   # same shape as a connection failure: shown by the Immich card
            return JSONResponse({"code": "cycle_running", "errors": [], "saved": False, "message": str(e)}, status_code=409)
        if state["code"] not in ("ok", "readonly"):
            return JSONResponse({**state, "saved": False}, status_code=422)
        return {**immich_info(), "saved": True, "test": state}

    @app.get("/api/models", dependencies=[Depends(auth)])
    def models():
        """AI models, compute hardware actually used, measured speed."""
        info = w.models_info()
        info["outdated"] = db.execute("SELECT count(*) FROM asset WHERE analyzed_at IS NOT NULL AND analyzer IS NOT ? "
                                      "AND missing_at IS NULL", (info["analyzer_id"],)).fetchone()[0]
        return info

    @app.put("/api/settings", dependencies=[Depends(auth)])
    def put_settings(values: dict):
        """Save settings (null = back to the default / environment value)."""
        reset = [k for k, v in values.items() if v is None]
        try:
            changes = validate_overrides(s, {k: v for k, v in values.items() if v is not None})
            if any(k not in EDITABLE for k in reset):
                raise ValueError("unknown setting")
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
            w.start_prune()   # otherwise applied at the next cycle ("pruned_with" differs)
        if {"detection", "detect_groups"} & changes.keys():
            drop_unwanted(db, s)   # automatic detections awaiting review now outside the settings, removed right away
            db.commit()
        return get_settings()
