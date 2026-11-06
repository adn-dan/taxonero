"""Dashboard: keyboard review, boxes clickable to Wikipedia, species, tree of life,
map, statistics and CSV export. Images are relayed from Immich with the tool's key.

create_app sets up security (desktop session, anti-CSRF), the page and the state; the routes are split by
domain (settings, review, species, identify), over a shared context (context.make_context).
"""

import json
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .. import logbuffer, store
from ..autoconfirm import auto_threshold, calibrate, group_reliability
from ..config import Settings
from ..sources import source_path
from ..worker import MAX_FAILURES, SKIPPED_NOT, Worker, models_on_disk
from . import identify, review, settings, species
from .context import fail, make_context

STATIC = Path(__file__).parent.parent / "static"
SESSION_COOKIE = "taxonero_session"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
# img-src https: Wikipedia images and map background. style-src 'unsafe-inline': style attributes of the interface.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data: blob: https:; object-src 'none'; base-uri 'none'; "
                               "form-action 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def create_app(s: Settings, worker: Worker | None = None, token: str | None = None, picker=None) -> FastAPI:
    """token: session token of the desktop edition (random, one per launch), instead of a login and
    password. The application opens it in the address /?t=…; the server then keeps it in a cookie.
    picker: native dialogs of the desktop window (pick_folder, pick_file), otherwise none."""
    logbuffer.install(s)
    app = FastAPI(title="Taxonero", docs_url=None, redoc_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=2048)  # range maps: ~1 MB of GeoJSON
    desktop = s.edition == "desktop"
    token = token or (secrets.token_urlsafe(32) if desktop else None)
    app.state.token = token

    @app.middleware("http")
    async def desktop_session(request: Request, call_next):
        """Desktop edition: no account, but no free access either. Only what presents the token of the
        launch (cookie set by /?t=…) is served: a web page opened in the browser or another account of
        the machine cannot drive the tool. The Host header must be local (against "DNS rebinding")."""
        if not desktop:
            return await call_next(request)
        if request.headers.get("host", "").rsplit(":", 1)[0] not in LOCAL_HOSTS:
            return JSONResponse({"detail": "non-local host refused"}, status_code=403)
        given = request.query_params.get("t")
        if request.url.path == "/" and given is not None and secrets.compare_digest(given.encode(), token.encode()):
            r = RedirectResponse("/", status_code=303)
            r.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict")
            return r
        if request.url.path != "/healthz" and not secrets.compare_digest(request.cookies.get(SESSION_COOKIE, "").encode(), token.encode()):
            return HTMLResponse("<p>Session expired: open Taxonero from the application.</p>", status_code=401)
        return await call_next(request)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        """Anti-CSRF: with HTTP Basic authentication, the browser sends the password back on its own,
        even for a form posted from another site. Any request that changes something must
        therefore come from the dashboard itself (Sec-Fetch-Site / Origin headers set by the browser)."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # Sec-Fetch-Site (all recent browsers) first: it stays right behind a proxy that
            # rewrites the Host header. Origin only for browsers that do not send it.
            site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            if (site not in ("same-origin", "none") if site else origin and origin.split("://", 1)[-1] != host):
                return JSONResponse({"detail": {"code": "cross_origin", "message": "request from another origin refused"}},
                                    status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """Declared last, hence the outermost: also on the refusals of the checks above."""
        r = await call_next(request)
        r.headers.update(SECURITY_HEADERS)
        return r

    w = worker or Worker(s)
    c = make_context(s, w, desktop)
    db, auth, public_immich = c.db, c.auth, c.public_immich

    # -- page ----------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        """Container health: the server answers and the work loop progresses. Tolerated delay: one interval,
        a full analysis cycle and 30 min of margin (first model download)."""
        idle = time.time() - w.last_activity
        limit = (s.interval_minutes + s.analysis_max_minutes) * 60 + 1800
        if w.loop_started and idle > limit:
            return JSONResponse({"ok": False, "stalled_s": int(idle)}, status_code=503)
        return {"ok": True}

    @app.get("/", dependencies=[Depends(auth)])
    def index():
        # Always revalidate: otherwise the browser may keep an old version of the interface.
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/static/{path:path}", dependencies=[Depends(auth)])
    def static(path: str):
        """Fonts and Leaflet served locally: the dashboard depends neither on Google Fonts nor on a CDN."""
        f = (STATIC / path).resolve()
        if not f.is_relative_to(STATIC.resolve()) or not f.is_file() or f.name == "index.html":
            raise HTTPException(404)
        # Interface code and texts (js/, app.css, i18n/): revalidated at every load, like index.html,
        # otherwise an update mixes old and new versions. Fonts and Leaflet (vendor/): one week.
        vendor = f.is_relative_to((STATIC / "vendor").resolve())
        cache = "private, max-age=604800" if vendor else "no-cache"
        return FileResponse(f, headers={"Cache-Control": cache})

    @app.get("/api/logs", dependencies=[Depends(auth)])
    def logs(after: int = 0):
        """Log lines kept in memory, after line number `after`."""
        return {"entries": logbuffer.BUFFER.read(after), "capacity": logbuffer.CAPACITY + logbuffer.KEEP}

    @app.delete("/api/logs", dependencies=[Depends(auth)])
    def logs_clear():
        """"Clear" button of the Log: lines in memory and warnings kept in data/."""
        logbuffer.BUFFER.clear()
        return {"last_id": logbuffer.BUFFER.last_id}

    @app.get("/api/status", dependencies=[Depends(auth)])
    def status():
        counts = {r["status"]: r["n"] for r in db.execute("SELECT status, count(*) AS n FROM detection GROUP BY status")}
        keep = s.skip_args
        return {
            "immich_version": store.get_state(db, "immich_version"),
            "writable": store.get_state(db, "writable") == "1",
            "dry_run": s.dry_run,
            "writes_paused": store.get_state(db, store.WRITES_PAUSED) == "1",
            "warnings": w.client.warnings, "errors": w.client.errors,
            # Photos to analyse (except skipped photos: people, RAW paired with a JPG, videos if disabled).
            "assets": db.execute(f"SELECT count(*) FROM asset WHERE missing_at IS NULL AND {SKIPPED_NOT}", keep).fetchone()[0],
            "analyzed": db.execute(f"SELECT count(*) FROM asset WHERE analyzed_at IS NOT NULL AND missing_at IS NULL AND {SKIPPED_NOT}",
                                   keep).fetchone()[0],
            "skipped": db.execute(f"SELECT count(*) FROM asset WHERE missing_at IS NULL AND kind IS NOT 'VIDEO' AND NOT ({SKIPPED_NOT})",
                                  keep).fetchone()[0],
            "failed": db.execute("SELECT count(*) FROM asset WHERE fail_count >= ? AND missing_at IS NULL",
                                 (MAX_FAILURES,)).fetchone()[0],
            "max_failures": MAX_FAILURES,
            # Videos disabled: no longer listed, but not gone either (see Worker.reconcile).
            "missing": db.execute("SELECT count(*) FROM asset WHERE missing_at IS NOT NULL AND (? OR kind IS NOT 'VIDEO')",
                                  (s.analyze_videos,)).fetchone()[0],
            "detections": counts,
            "auto_confirm": s.auto_confirm,
            "threshold": auto_threshold(db, s),
            "calibration": calibrate(db, s.auto_confirm_target_precision, s.auto_confirm_min_reviews),
            "groups": group_reliability(db, s),
            "last_run": json.loads(store.get_state(db, "last_run") or "null"),
            "immich_url": public_immich(),
            "immich_configured": w.configured,
            "source": s.source, "source_path": str(path) if (path := source_path(s)) else None,
            "manual": s.detection == "manual",   # manual detection: nothing is "queued"
            "can_pick": picker is not None,      # desktop: "Browse…" button
            "models_on_disk": models_on_disk(s),  # otherwise ~5 GB to download at first use, announced
            "langs": list(s.langs),
            # Languages whose interface is translated (static/i18n catalogues); the others: species names only.
            "ui_langs": sorted(f.stem for f in (STATIC / "i18n").glob("*.json")),
        }

    class PickIn(BaseModel):
        kind: str = "folder"
        suffix: str = ""
        start: str = ""

    @app.post("/api/desktop/pick", dependencies=[Depends(auth)])
    def desktop_pick(body: PickIn):
        """Desktop: choice of a folder, or of the library of a photo application, in a native dialog.
        Through the server and not window.pywebview.api, which pywebview creates with new Function, refused by the CSP."""
        if picker is None:
            fail(404, "no_picker", "dialog available only in the desktop application")
        return {"path": picker.pick_folder() if body.kind == "folder" else picker.pick_file(body.suffix, body.start)}

    for module in (settings, review, species, identify):
        module.register(app, c)
    return app
