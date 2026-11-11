"""ExifTool (Phil Harvey, the reference for photo metadata) kept open (-stay_open): a single process
for all reads and writes, instead of starting Perl (~0.2 s) per photo.

A process kept open never stops by itself, not even when the program that started it is gone (it keeps waiting
for its next command): every one still open is closed when Python exits."""

import atexit
import json
import os
import queue
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
    for base in _bundled():
        if (base / "exiftool.exe").exists():
            return [str(base / "exiftool.exe")]
        if (base / "exiftool").is_file() and shutil.which("perl"):
            return [shutil.which("perl"), str(base / "exiftool")]
    found = shutil.which("exiftool")
    return [found] if found else None


def _bundled() -> list[Path]:
    """Folders that may hold the ExifTool of the desktop application: the one given by the launcher, the frozen
    executable, then the copy the installer keeps in the data folder (runtime/exiftool), found even when the application
    is started by hand (python -m taxonero desktop) without the launcher."""
    from ..config import user_data_dir
    found = [Path(os.environ["TAXONERO_EXIFTOOL"])] if os.environ.get("TAXONERO_EXIFTOOL") else []
    if getattr(sys, "frozen", False):
        found.append(Path(getattr(sys, "_MEIPASS", "")) / "exiftool")
    data = Path(os.environ["DATA_DIR"]) if os.environ.get("DATA_DIR") else user_data_dir()   # same rule as the launcher
    return found + [data / "runtime" / "exiftool"]


def available() -> bool:
    return command() is not None


_OPEN: set[subprocess.Popen] = set()   # processes started and not closed yet (even if their ExifTool object is gone)


def _stop(p: subprocess.Popen) -> None:
    """Ask the process to stop (after the command in progress, if any), kill it if it does not."""
    _OPEN.discard(p)
    if p.poll() is not None:
        return
    try:
        p.stdin.write("-stay_open\nFalse\n")
        p.stdin.flush()
        p.wait(5)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        p.kill()
        p.wait(5)


@atexit.register
def close_all() -> None:
    """Exit of Python: the processes still open are closed (a source never closed, the desktop window closed)."""
    for p in list(_OPEN):
        _stop(p)


class ExifTool:
    def __init__(self, cmd: list[str] | None = None):
        self.cmd = cmd or command() or ["exiftool"]
        self._proc: subprocess.Popen | None = None
        self._errors: queue.Queue = queue.Queue()   # error lines of the process (None: its error output is closed)
        self._lock = threading.Lock()

    def _start(self) -> subprocess.Popen:
        if self._proc is None or self._proc.poll() is not None:
            _OPEN.discard(self._proc)   # stopped on its own (crash): started again
            self._proc = subprocess.Popen([*self.cmd, "-stay_open", "True", "-@", "-"], stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", bufsize=1,
                                          **NO_WINDOW)
            _OPEN.add(self._proc)
            # Error output read as it comes, in its own thread: read only after the standard output, a batch with many
            # errors (unreadable files) filled its pipe (a few KB) and ExifTool waited there for ever, as did the scan.
            self._errors = queue.Queue()
            threading.Thread(target=self._drain, args=(self._proc, self._errors), daemon=True, name="exiftool-errors").start()
        return self._proc

    @staticmethod
    def _drain(p: subprocess.Popen, errors: queue.Queue) -> None:
        try:
            for line in p.stderr:
                errors.put(line)
        except (OSError, ValueError):   # process stopped, pipe closed
            pass
        finally:
            errors.put(None)

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
            err, errors = [], self._errors
            while (line := errors.get()) is not None and line.rstrip("\n") != "{done}":
                err.append(line)
            if line is None:   # process gone: the next command starts another one, with its own queue
                errors.put(None)
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
            p, self._proc = self._proc, None
            if p is not None:
                _stop(p)


def escape(value: str) -> str:
    """Value for -ec: backslashes and line breaks escaped."""
    return value.replace("\\", "\\\\").replace("\n", "\\n")
