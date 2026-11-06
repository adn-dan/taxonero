"""Desktop edition: taxonero desktop.

The same program as the server edition, run on the user's computer:
- data in the system user folder (config.user_data_dir), models included;
- dashboard on 127.0.0.1 only, without login or password: one random token per launch, passed
  in the address opened by the application then kept in a cookie (see web.desktop_session);
- everything in an application window (pywebview: WebView2 on Windows, WebKit on macOS, Qt on Linux;
  pip install '.[desktop]'), never in the browser; without it, an error message;
- photo source (Immich or folder) and result writing set in the dashboard.

Also opens the way to connectors that only make sense on the computer (sources.SOURCES, editions=("desktop",)).
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
    raise RuntimeError("no free local port")


class _Api:
    """Native dialogs of the window, offered to the page by the server (/api/desktop/pick)."""

    def __init__(self):
        self.window = None

    def pick_folder(self) -> str | None:
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        chosen = self.window.create_file_dialog(kind) if self.window else None
        return chosen[0] if chosen else None

    def pick_file(self, suffix: str, start: str = "") -> str | None:
        """Library of a photo application (darktable library.db, .lrcat catalog)."""
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
        folder = str(Path(start).parent) if start else ""
        chosen = self.window.create_file_dialog(kind, directory=folder, file_types=(f"{suffix} (*{suffix})",)) if self.window else None
        return chosen[0] if chosen else None


def prepare(s: Settings) -> Settings:
    """Desktop-specific settings, before any model loading (HF_HOME must be set before importing the
    Hugging Face libraries)."""
    s.edition = "desktop"
    s.dashboard_host = "127.0.0.1"
    if "data_dir" not in s.model_fields_set:
        s.data_dir = user_data_dir()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    if s.data_dir == user_data_dir():   # a folder chosen by the user keeps its permissions
        s.data_dir.chmod(0o700)
    os.environ.setdefault("HF_HOME", str(s.data_dir / "hf"))
    if "detector_path" not in s.model_fields_set:   # no ONNX shipped: MegaDetector in PyTorch (megadetector.py)
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
                                           log_config=None))   # web server errors in the common log
    threading.Thread(target=server.run, daemon=True, name="dashboard").start()
    while not server.started:
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}/?t={token}"
    log.info("taxonero (desktop): data in %s", s.data_dir)
    if not _window(url, Path(s.data_dir) / "fenetre", api):
        server.should_exit = True
        raise SystemExit(1)
    server.should_exit = True


def _window(url: str, storage: Path, api: "_Api") -> bool:
    """Native window; False if it cannot open on this system. Page storage (language, filters,
    tabs) kept in storage: by default, pywebview opens a private session, forgotten on close."""
    try:
        import webview
        # No js_api: pywebview creates it with new Function, refused by the CSP. Dialogs go through
        # the server (/api/desktop/pick).
        api.window = webview.create_window("Taxonero", url, width=1440, height=920, min_size=(900, 600))
        # Linux: Qt (installed with the application) rather than GTK, whose Python bindings cannot be installed
        # with pip without build tools.
        if sys.platform.startswith("linux"):
            _fix_qt_permissions(storage)
        webview.start(gui="qt" if sys.platform.startswith("linux") else None, private_mode=False, storage_path=str(storage))   # returns on close
        return True
    except Exception as e:   # ImportError, or WebViewException (no rendering engine)
        log.error("the application window cannot be opened", exc_info=e)
        _error(f"The Taxonero window cannot open on this system ({type(e).__name__}: {e}).\n\n"
               "Run again with --reinstall to repair the installation.")
        return False


def _fix_qt_permissions(storage: Path) -> None:
    """pywebview 6.2 (Qt) answers the page's permission requests (clipboard, notifications…) with an
    integer where PyQt6 expects the enumeration: the exception in this Qt callback stops the application. Same decision
    (microphone and camera accepted, the rest refused), with the right values."""
    try:
        from webview.platforms import qt
    except Exception:   # Qt missing: webview.start will report it
        return
    page = qt.QWebPage
    media = {getattr(page.Feature, f, None) for f in ("MediaAudioCapture", "MediaVideoCapture", "MediaAudioVideoCapture")}

    def on_permission(self, url, feature):
        policy = page.PermissionPolicy.PermissionGrantedByUser if feature in media else page.PermissionPolicy.PermissionDeniedByUser
        self.setFeaturePermission(url, feature, policy)

    qt.BrowserView.WebPage.onFeaturePermissionRequested = on_permission
    # The module reads the storage folder when imported, before webview.start sets it: to be given again here.
    qt._profile_storage_path = str(storage)


def _error(message: str) -> None:
    """Error message in a dialog (Tk, standard library), otherwise on standard error."""
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("Taxonero", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr, flush=True)
