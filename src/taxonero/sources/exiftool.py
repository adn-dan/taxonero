"""ExifTool (Phil Harvey, la référence pour les métadonnées photo) gardé ouvert (-stay_open) : un seul processus
pour toutes les lectures et écritures, au lieu d'un démarrage de Perl (~0,2 s) par photo."""

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

# Windows : pas de fenêtre de console à chaque lancement d'ExifTool (application de bureau).
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
# Installation, si ExifTool manque (message affiché dans le dashboard).
INSTALL_HINT = {"win32": "https://exiftool.org (exiftool.exe dans le PATH)", "darwin": "brew install exiftool, ou le paquet de https://exiftool.org"}.get(
    sys.platform, "paquet libimage-exiftool-perl (Debian, Ubuntu) ou perl-Image-ExifTool (Fedora)")


class ExifToolError(RuntimeError):
    pass


def command() -> list[str] | None:
    """Commande ExifTool : celle livrée avec l'application de bureau (dossier « exiftool » du lanceur, ou à côté du programme :
    exiftool.exe autonome sous Windows, script Perl ailleurs), sinon celle du système."""
    base = (Path(os.environ["TAXONERO_EXIFTOOL"]) if os.environ.get("TAXONERO_EXIFTOOL")   # lanceur de bureau
            else Path(getattr(sys, "_MEIPASS", "")) / "exiftool" if getattr(sys, "frozen", False) else None)
    if base and (base / "exiftool.exe").exists():
        return [str(base / "exiftool.exe")]
    if base and (base / "exiftool").exists() and shutil.which("perl"):
        return [shutil.which("perl"), str(base / "exiftool")]
    found = shutil.which("exiftool")
    return [found] if found else None


def available() -> bool:
    return command() is not None


class ExifTool:
    def __init__(self, cmd: list[str] | None = None):
        self.cmd = cmd or command() or ["exiftool"]
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def _start(self) -> subprocess.Popen:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen([*self.cmd, "-stay_open", "True", "-@", "-"], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", bufsize=1,
                                          **NO_WINDOW)
        return self._proc

    def run(self, *args: str) -> tuple[str, str]:
        """Une commande (un argument par élément, comme en ligne de commande) ; renvoie (sortie, erreurs). Les valeurs
        sont passées par -ec : retours à la ligne écrits \\n, antislash \\\\."""
        if any("\n" in a for a in args):
            raise ValueError("argument sur plusieurs lignes : l'échapper (\\n) avec -ec")
        with self._lock:
            p = self._start()
            # Marqueurs de fin à nous, sur les deux sorties (-echo3, -echo4) : le « {ready} » d'ExifTool disparaît
            # avec -q, et il en reste parfois un d'une commande précédente, ignoré.
            p.stdin.write("\n".join([*args, "-echo3", "{done}", "-echo4", "{done}", "-execute"]) + "\n")
            p.stdin.flush()
            out = []
            for line in p.stdout:
                if line.rstrip("\n") == "{done}":
                    break
                if line.rstrip("\n") != "{ready}":
                    out.append(line)
            err = []
            for line in p.stderr:
                if line.rstrip("\n") == "{done}":
                    break
                err.append(line)
        return "".join(out), "".join(err)

    def read(self, paths: list[str], tags: list[str]) -> list[dict]:
        """Métadonnées de plusieurs fichiers (valeurs numériques, -n), une entrée par fichier lisible."""
        if not paths:
            return []
        out, _ = self.run("-j", "-n", "-q", "-q", "-ec", "-charset", "filename=utf8", *(f"-{t}" for t in tags), *paths)
        return json.loads(out) if out.strip() else []

    def write(self, path: str, assignments: list[str]) -> None:
        """Écrit des valeurs (ex. -XMP-dc:Subject+=Mésange) ; crée le fichier s'il s'agit d'un .xmp absent."""
        out, err = self.run("-ec", "-overwrite_original", "-charset", "filename=utf8", *assignments, path)
        if "1 image files updated" not in out and "1 image files created" not in out and "unchanged" not in out:
            raise ExifToolError((err or out).strip() or f"écriture refusée : {path}")

    def close(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.stdin.write("-stay_open\nFalse\n")
                self._proc.stdin.flush()
                try:
                    self._proc.wait(5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None


def escape(value: str) -> str:
    """Valeur pour -ec : antislash et retours à la ligne échappés."""
    return value.replace("\\", "\\\\").replace("\n", "\\n")
