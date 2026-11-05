"""Édition de bureau : taxonero desktop.

Le même programme que l'édition serveur, lancé sur l'ordinateur de l'utilisateur :
- données dans le dossier utilisateur du système (config.user_data_dir), modèles compris ;
- dashboard sur 127.0.0.1 seulement, sans identifiant ni mot de passe : un jeton aléatoire par lancement, passé
  dans l'adresse ouverte par l'application puis gardé dans un cookie (voir web.desktop_session) ;
- tout dans une fenêtre d'application (pywebview : WebView2 sous Windows, WebKit sous macOS, Qt sous Linux ;
  pip install '.[desktop]'), jamais dans le navigateur ; sans elle, un message d'erreur ;
- source des photos (Immich ou dossier) et écriture des résultats réglées dans le dashboard.

Ouvre aussi la voie aux connecteurs qui n'ont de sens que sur l'ordinateur (sources.SOURCES, editions=("desktop",)).
"""

import logging
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

from . import logbuffer
from .config import Settings, user_data_dir

log = logging.getLogger(__name__)


def _free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("aucun port local libre")


class _Api:
    """Boîtes de dialogue natives de la fenêtre, offertes à la page par le serveur (/api/desktop/pick)."""

    def __init__(self):
        self.window = None

    def pick_folder(self) -> str | None:
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        chosen = self.window.create_file_dialog(kind) if self.window else None
        return chosen[0] if chosen else None

    def pick_file(self, suffix: str, start: str = "") -> str | None:
        """Bibliothèque d'un logiciel photo (library.db de darktable, catalogue .lrcat)."""
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
        folder = str(Path(start).parent) if start else ""
        chosen = self.window.create_file_dialog(kind, directory=folder, file_types=(f"{suffix} (*{suffix})",)) if self.window else None
        return chosen[0] if chosen else None


def prepare(s: Settings) -> Settings:
    """Réglages propres au bureau, avant tout chargement de modèle (HF_HOME doit être posé avant l'import des
    bibliothèques de Hugging Face)."""
    s.edition = "desktop"
    s.dashboard_host = "127.0.0.1"
    if "data_dir" not in s.model_fields_set:
        s.data_dir = user_data_dir()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    if s.data_dir == user_data_dir():   # un dossier choisi par l'utilisateur garde ses droits
        s.data_dir.chmod(0o700)
    os.environ.setdefault("HF_HOME", str(s.data_dir / "hf"))
    if "detector_path" not in s.model_fields_set:   # pas d'ONNX livré : MegaDetector dans PyTorch (megadetector.py)
        s.detector_path = s.data_dir / "models" / s.detector_path.name
    return s


def run(s: Settings) -> None:
    import uvicorn

    from .web import create_app
    from .worker import Worker

    prepare(s)
    logbuffer.install(s)
    w = Worker(s)
    threading.Thread(target=w.loop, daemon=True, name="worker").start()
    token = secrets.token_urlsafe(32)
    port = _free_port(s.dashboard_port)
    api = _Api()
    server = uvicorn.Server(uvicorn.Config(create_app(s, w, token, picker=api), host="127.0.0.1", port=port, log_level="warning",
                                           log_config=None))   # erreurs du serveur web dans le journal commun
    threading.Thread(target=server.run, daemon=True, name="dashboard").start()
    while not server.started:
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}/?t={token}"
    log.info("taxonero (bureau) : données dans %s", s.data_dir)
    if not _window(url, Path(s.data_dir) / "fenetre", api):
        server.should_exit = True
        raise SystemExit(1)
    server.should_exit = True


def _window(url: str, storage: Path, api: "_Api") -> bool:
    """Fenêtre native ; False si elle ne peut pas s'ouvrir sur ce système. Stockage de la page (langue, filtres,
    onglets) gardé dans storage : par défaut, pywebview ouvre une session privée, oubliée à la fermeture."""
    try:
        import webview
        # Pas de js_api : pywebview le crée avec new Function, refusé par la CSP. Les boîtes de dialogue passent par
        # le serveur (/api/desktop/pick).
        api.window = webview.create_window("Taxonero", url, width=1440, height=920, min_size=(900, 600))
        # Linux : Qt (installé avec l'application) plutôt que GTK, dont les liaisons Python ne s'installent pas
        # par pip sans outils de compilation.
        if sys.platform.startswith("linux"):
            _fix_qt_permissions(storage)
        webview.start(gui="qt" if sys.platform.startswith("linux") else None, private_mode=False, storage_path=str(storage))   # rend la main à la fermeture
        return True
    except Exception as e:   # ImportError, ou WebViewException (aucun moteur d'affichage)
        log.error("fenêtre de l'application impossible à ouvrir", exc_info=e)
        _error(f"La fenêtre de Taxonero ne peut pas s'ouvrir sur ce système ({type(e).__name__} : {e}).\n\n"
               "Relancer avec --reinstall pour réparer l'installation.")
        return False


def _fix_qt_permissions(storage: Path) -> None:
    """pywebview 6.2 (Qt) répond aux demandes de permission de la page (presse-papiers, notifications…) par un
    entier là où PyQt6 attend l'énumération : l'exception dans ce rappel Qt arrête l'application. Même décision
    (micro et caméra acceptés, le reste refusé), avec les bonnes valeurs."""
    try:
        from webview.platforms import qt
    except Exception:   # Qt absent : webview.start le signalera
        return
    page = qt.QWebPage
    media = {getattr(page.Feature, f, None) for f in ("MediaAudioCapture", "MediaVideoCapture", "MediaAudioVideoCapture")}

    def on_permission(self, url, feature):
        policy = page.PermissionPolicy.PermissionGrantedByUser if feature in media else page.PermissionPolicy.PermissionDeniedByUser
        self.setFeaturePermission(url, feature, policy)

    qt.BrowserView.WebPage.onFeaturePermissionRequested = on_permission
    # Le module lit le dossier de stockage à son import, avant que webview.start le règle : à redonner ici.
    qt._profile_storage_path = str(storage)


def _error(message: str) -> None:
    """Message d'erreur dans une boîte de dialogue (Tk, bibliothèque standard), sinon sur la sortie d'erreur."""
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("Taxonero", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr, flush=True)
