"""ExifTool (Phil Harvey, the reference for photo metadata) kept open (-stay_open): a single process
for all reads and writes, instead of starting Perl (~0.2 s) per photo."""

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

# Windows: no console window at every ExifTool launch (desktop application).
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
# Installation, if ExifTool is missing (message shown in the dashboard).
INSTALL_HINT = {"win32": "https://exiftool.org (exiftool.exe in the PATH)", "darwin": "brew install exiftool, or the package from https://exiftool.org"}.get(
    sys.platform, "package libimage-exiftool-perl (Debian, Ubuntu) or perl-Image-ExifTool (Fedora)")


class ExifToolError(RuntimeError):
    pass


def command() -> list[str] | None:
    """ExifTool command: the one shipped with the desktop application ("exiftool" folder of the launcher, or next to the program:
    standalone exiftool.exe on Windows, Perl script elsewhere), otherwise the system one."""
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
        """One command (one argument per item, as on the command line); return (output, errors). Values
        are passed with -ec: line breaks written \\n, backslash \\\\."""
        if any("\n" in a for a in args):
            raise ValueError("argument on several lines: escape it (\\n) with -ec")
        with self._lock:
            p = self._start()
            # End markers of our own, on both outputs (-echo3, -echo4): ExifTool's "{ready}" disappears
            # with -q, and one from a previous command sometimes remains, ignored.
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
        """Metadata of several files (numeric values, -n), one entry per readable file."""
        if not paths:
            return []
        out, _ = self.run("-j", "-n", "-q", "-q", "-ec", "-charset", "filename=utf8", *(f"-{t}" for t in tags), *paths)
        return json.loads(out) if out.strip() else []

    def write(self, path: str, assignments: list[str]) -> None:
        """Write values (e.g. -XMP-dc:Subject+=Tit); create the file if it is a missing .xmp."""
        out, err = self.run("-ec", "-overwrite_original", "-charset", "filename=utf8", *assignments, path)
        if "1 image files updated" not in out and "1 image files created" not in out and "unchanged" not in out:
            raise ExifToolError((err or out).strip() or f"write refused: {path}")

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
    """Value for -ec: backslashes and line breaks escaped."""
    return value.replace("\\", "\\\\").replace("\n", "\\n")
