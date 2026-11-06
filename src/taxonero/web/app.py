"""Dashboard : relecture au clavier, rectangles cliquables vers Wikipédia, espèces, arbre du vivant,
carte, statistiques et export CSV. Les images sont relayées depuis Immich avec la clé de l'outil.

create_app pose la sécurité (session de bureau, anti-CSRF), la page et l'état ; les routes sont réparties par
domaine (settings, review, species, identify), sur un contexte commun (context.make_context).
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
# img-src https: : images de Wikipédia et fond de carte. style-src 'unsafe-inline' : attributs style de l'interface.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data: blob: https:; object-src 'none'; base-uri 'none'; "
                               "form-action 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def create_app(s: Settings, worker: Worker | None = None, token: str | None = None, picker=None) -> FastAPI:
    """token : jeton de session de l'édition de bureau (aléatoire, un par lancement), à défaut d'identifiant et de
    mot de passe. L'application l'ouvre dans l'adresse /?t=… ; le serveur le garde alors dans un cookie.
    picker : boîtes de dialogue natives de la fenêtre de bureau (pick_folder, pick_file), sinon aucune."""
    logbuffer.install(s)
    app = FastAPI(title="Taxonero", docs_url=None, redoc_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=2048)  # aires de répartition : ~1 Mo de GeoJSON
    desktop = s.edition == "desktop"
    token = token or (secrets.token_urlsafe(32) if desktop else None)
    app.state.token = token

    @app.middleware("http")
    async def desktop_session(request: Request, call_next):
        """Édition de bureau : pas de compte, mais pas d'accès libre pour autant. Seul ce qui présente le jeton du
        lancement (cookie posé par /?t=…) est servi : une page web ouverte dans le navigateur ou un autre compte de
        la machine ne peut pas piloter l'outil. L'en-tête Host doit être local (contre le « DNS rebinding »)."""
        if not desktop:
            return await call_next(request)
        if request.headers.get("host", "").rsplit(":", 1)[0] not in LOCAL_HOSTS:
            return JSONResponse({"detail": "hôte non local refusé"}, status_code=403)
        given = request.query_params.get("t")
        if request.url.path == "/" and given is not None and secrets.compare_digest(given.encode(), token.encode()):
            r = RedirectResponse("/", status_code=303)
            r.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict")
            return r
        if request.url.path != "/healthz" and not secrets.compare_digest(request.cookies.get(SESSION_COOKIE, "").encode(), token.encode()):
            return HTMLResponse("<p>Session expirée : ouvre taxonero depuis l'application.</p>", status_code=401)
        return await call_next(request)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        """Anti-CSRF : avec l'authentification HTTP Basic, le navigateur renvoie le mot de passe tout seul,
        même pour un formulaire posté depuis un autre site. Toute requête qui modifie quelque chose doit
        donc venir du dashboard lui-même (en-têtes Sec-Fetch-Site / Origin posés par le navigateur)."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # Sec-Fetch-Site (tous les navigateurs récents) d'abord : il reste juste derrière un proxy qui
            # réécrit l'en-tête Host. Origin seulement pour les navigateurs qui ne l'envoient pas.
            site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            if (site not in ("same-origin", "none") if site else origin and origin.split("://", 1)[-1] != host):
                return JSONResponse({"detail": {"code": "cross_origin", "message": "requête d'une autre origine refusée"}},
                                    status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """Déclaré en dernier, donc le plus externe : aussi sur les refus des contrôles ci-dessus."""
        r = await call_next(request)
        r.headers.update(SECURITY_HEADERS)
        return r

    w = worker or Worker(s)
    c = make_context(s, w, desktop)
    db, auth, public_immich = c.db, c.auth, c.public_immich

    # -- page ----------------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        """Santé du conteneur : le serveur répond et la boucle de travail avance. Délai toléré : un intervalle,
        un cycle d'analyse complet et 30 min de marge (premier téléchargement des modèles)."""
        idle = time.time() - w.last_activity
        limit = (s.interval_minutes + s.analysis_max_minutes) * 60 + 1800
        if w.loop_started and idle > limit:
            return JSONResponse({"ok": False, "stalled_s": int(idle)}, status_code=503)
        return {"ok": True}

    @app.get("/", dependencies=[Depends(auth)])
    def index():
        # Toujours revalider : sinon le navigateur peut garder une ancienne version de l'interface.
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/static/{path:path}", dependencies=[Depends(auth)])
    def static(path: str):
        """Polices et Leaflet servis en local : le dashboard ne dépend ni de Google Fonts ni d'un CDN."""
        f = (STATIC / path).resolve()
        if not f.is_relative_to(STATIC.resolve()) or not f.is_file() or f.name == "index.html":
            raise HTTPException(404)
        # Code et textes de l'interface (js/, app.css, i18n/) : revalidés à chaque chargement, comme index.html,
        # sinon une mise à jour mélange ancienne et nouvelle version. Polices et Leaflet (vendor/) : une semaine.
        vendor = f.is_relative_to((STATIC / "vendor").resolve())
        cache = "private, max-age=604800" if vendor else "no-cache"
        return FileResponse(f, headers={"Cache-Control": cache})

    @app.get("/api/logs", dependencies=[Depends(auth)])
    def logs(after: int = 0):
        """Lignes du journal gardées en mémoire, après la ligne numéro `after`."""
        return {"entries": logbuffer.BUFFER.read(after), "capacity": logbuffer.CAPACITY + logbuffer.KEEP}

    @app.delete("/api/logs", dependencies=[Depends(auth)])
    def logs_clear():
        """Bouton « Effacer » du Journal : lignes en mémoire et avertissements gardés dans data/."""
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
            # Photos à analyser (hors photos écartées : personnes, RAW doublé d'un JPG, vidéos si désactivées).
            "assets": db.execute(f"SELECT count(*) FROM asset WHERE missing_at IS NULL AND {SKIPPED_NOT}", keep).fetchone()[0],
            "analyzed": db.execute(f"SELECT count(*) FROM asset WHERE analyzed_at IS NOT NULL AND missing_at IS NULL AND {SKIPPED_NOT}",
                                   keep).fetchone()[0],
            "skipped": db.execute(f"SELECT count(*) FROM asset WHERE missing_at IS NULL AND kind IS NOT 'VIDEO' AND NOT ({SKIPPED_NOT})",
                                  keep).fetchone()[0],
            "failed": db.execute("SELECT count(*) FROM asset WHERE fail_count >= ? AND missing_at IS NULL",
                                 (MAX_FAILURES,)).fetchone()[0],
            "max_failures": MAX_FAILURES,
            # Vidéos désactivées : plus listées, mais pas disparues pour autant (voir Worker.reconcile).
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
            "manual": s.detection == "manual",   # détection manuelle : rien n'est « en file »
            "can_pick": picker is not None,      # bureau : bouton « Parcourir… »
            "models_on_disk": models_on_disk(s),  # sinon ~5 Go à télécharger au premier usage, annoncés
            "langs": list(s.langs),
            # Langues dont l'interface est traduite (catalogues static/i18n) ; les autres : noms d'espèces seulement.
            "ui_langs": sorted(f.stem for f in (STATIC / "i18n").glob("*.json")),
        }

    class PickIn(BaseModel):
        kind: str = "folder"
        suffix: str = ""
        start: str = ""

    @app.post("/api/desktop/pick", dependencies=[Depends(auth)])
    def desktop_pick(body: PickIn):
        """Bureau : choix d'un dossier, ou de la bibliothèque d'un logiciel photo, dans une boîte de dialogue native.
        Par le serveur et non par window.pywebview.api, que pywebview crée avec new Function, refusé par la CSP."""
        if picker is None:
            fail(404, "no_picker", "boîte de dialogue disponible seulement dans l'application de bureau")
        return {"path": picker.pick_folder() if body.kind == "folder" else picker.pick_file(body.suffix, body.start)}

    for module in (settings, review, species, identify):
        module.register(app, c)
    return app
