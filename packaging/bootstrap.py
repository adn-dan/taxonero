"""Launcher of the desktop edition (light executable, standard library only).

Convention of desktop applications built on PyTorch (ComfyUI Desktop, Invoke, Stability Matrix): a
single small executable per system, which installs at first launch the PyTorch matching the machine's GPU, with
uv (Astral), in the user folder. One executable per GPU would be huge (CUDA PyTorch: 2 to 3 GB) and
would exceed the 2 GB per-file limit of GitHub releases.

The executable contains: uv, the taxonero wheel, constraints.txt and ExifTool. At first launch (or after an
update): Python 3.12 (managed by uv), PyTorch for the detected GPU, taxonero, then the AI models (~5 GB,
only once); then it runs "taxonero desktop" in this environment. The installation is shown in a small window (Tk); without
a graphical display, in the console.

    TAXONERO_GPU=cpu|cu130|cu128|rocm|xpu|mps|amd-win   forces a variant (otherwise automatic detection)
    taxonero --reinstall                                   installs again (other GPU, repair)
"""

import hashlib
import json
import locale
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PYTHON = "3.12"
STEPS = 4   # Python, PyTorch, Taxonero, AI models
PYTORCH = "https://download.pytorch.org/whl/"
AMD_WIN = "https://repo.radeon.com/rocm/windows/rocm-rel-7.2/"
# Variant -> (package index, PyTorch packages). Same versions as the Docker image.
TORCH = {
    "cu130": (PYTORCH + "cu130", ["torch==2.14.1", "torchvision==0.29.1"]),     # NVIDIA, driver >= 580
    "cu128": (PYTORCH + "cu128", ["torch==2.11.0", "torchvision==0.26.0"]),     # NVIDIA, driver >= 525
    "rocm": (PYTORCH + "rocm7.2", ["torch==2.14.1", "torchvision==0.29.1"]),    # AMD, Linux
    "xpu": (PYTORCH + "xpu", ["torch==2.14.1", "torchvision==0.29.1"]),         # Intel Arc, Core Ultra
    "cpu": (PYTORCH + "cpu", ["torch==2.14.1", "torchvision==0.29.1"]),
    "mps": (None, ["torch==2.14.1", "torchvision==0.29.1"]),                    # Apple Silicon (PyPI, Metal)
    # AMD on Windows: preview build published by AMD (Python 3.12, driver 26.1.1, Radeon RX 7000/9000).
    "amd-win": (None, [AMD_WIN + f for f in (
        "rocm_sdk_core-7.2.0.dev0-py3-none-win_amd64.whl", "rocm_sdk_devel-7.2.0.dev0-py3-none-win_amd64.whl",
        "rocm_sdk_libraries_custom-7.2.0.dev0-py3-none-win_amd64.whl", "rocm-7.2.0.dev0.tar.gz",
        "torch-2.9.1%2Brocmsdk20260116-cp312-cp312-win_amd64.whl",
        "torchvision-0.24.1%2Brocmsdk20260116-cp312-cp312-win_amd64.whl")]),
}
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def bundle() -> Path:
    """Files shipped with the executable (or packaging/build in development)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent / "build"))


def data_dir() -> Path:
    """Same rule as config.user_data_dir (this launcher does not import taxonero)."""
    if os.environ.get("DATA_DIR"):
        return Path(os.environ["DATA_DIR"])
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "taxonero"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "taxonero"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "taxonero"


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20, **NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def gpu_names() -> list[str]:
    """Names of the graphics cards (Windows: WMI; Linux: PCI vendor of the DRM cards)."""
    if sys.platform == "win32":
        out = _run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"])
        return [x.strip() for x in out.splitlines() if x.strip()]
    names = []
    for card in Path("/sys/class/drm").glob("card[0-9]*/device"):
        if not re.fullmatch(r"card\d+", card.parent.name):   # card1-eDP-1…: display connectors, not cards
            continue
        vendor = (card / "vendor").read_text().strip() if (card / "vendor").exists() else ""
        driver = (card / "driver").resolve().name if (card / "driver").exists() else ""
        names.append({"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}.get(vendor, vendor) + f" ({driver})")
    return names


def nvidia_driver() -> float | None:
    out = _run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
    m = re.match(r"(\d+)\.(\d+)", out.strip())
    return float(f"{m[1]}.{m[2]}") if m else None


def _why(fr: str, en: str) -> str:
    """Reason for the choice, in the language of the installation window (see FR, below)."""
    return fr if FR else en


def detect() -> tuple[str, str]:
    """(variant, reason). Order: forced choice, Apple Silicon, NVIDIA, AMD, Intel, CPU."""
    forced = os.environ.get("TAXONERO_GPU", "").strip()
    if forced:
        if forced not in TORCH:
            raise SystemExit(f"TAXONERO_GPU={forced} unknown: {' | '.join(TORCH)}")
        return forced, _why("imposée par TAXONERO_GPU", "forced by TAXONERO_GPU")
    if sys.platform == "darwin":
        return ("mps", "Apple Silicon") if platform.machine() == "arm64" else ("cpu", _why("Mac Intel : processeur", "Intel Mac: CPU"))
    arm = platform.machine().lower() in ("arm64", "aarch64")
    driver = nvidia_driver()
    if driver and driver >= 580:   # CUDA 13: x86 and Linux ARM (NVIDIA Grace, DGX Spark)
        return "cu130", _why(f"NVIDIA, pilote {driver}", f"NVIDIA, driver {driver}")
    if driver and not arm:
        return "cu128", _why(f"NVIDIA, pilote {driver}", f"NVIDIA, driver {driver}")
    names = " ".join(gpu_names())
    if not arm and ("AMD" in names or "Radeon" in names):
        if sys.platform == "win32":
            return "amd-win", _why("AMD Radeon (version d'aperçu d'AMD)", "AMD Radeon (AMD preview build)")
        if Path("/dev/kfd").exists():   # amdgpu driver with compute (ROCm)
            return "rocm", "AMD Radeon (ROCm)"
    if not arm and ("Arc" in names or "(xe)" in names):   # Intel Arc, Core Ultra (xe driver on Linux)
        return "xpu", "Intel Arc"
    return "cpu", _why("aucun GPU pris en charge détecté", "no supported GPU found")


FR = (locale.getlocale()[0] or os.environ.get("LANG", "")).lower().startswith("fr")
TEXT = {   # installation window: French or English depending on the system
    "title": ("Installation de Taxonero", "Installing Taxonero"),
    "intro": ("Première installation ou mise à jour : {why}. Quelques minutes, une seule fois.",
              "First installation or update: {why}. A few minutes, only once."),
    "python": ("Python {v}", "Python {v}"),
    "torch": ("PyTorch ({variant}), plusieurs centaines de Mo", "PyTorch ({variant}), several hundred MB"),
    "app": ("Taxonero et ses bibliothèques", "Taxonero and its libraries"),
    "models": ("Modèles d'IA, environ 5 Go (une seule fois)", "AI models, about 5 GB (only once)"),
    "models_later": ("Modèles non téléchargés : ils le seront au premier usage.",
                     "Models not downloaded: they will be at first use."),
    "retry": ("Échec ({what}), nouvel essai dans {s} s…", "Failed ({what}), new attempt in {s} s…"),
    "log": ("Journal complet : {path}", "Full log: {path}"),
    "fallback": ("Échec de l'installation pour {variant} : repli sur le processeur",
                 "Installation for {variant} failed: falling back to the CPU"),
    "failed": ("L'installation a échoué. Détails ci-dessous ; relancer avec --reinstall pour réessayer.",
               "Installation failed. Details below; run again with --reinstall to retry."),
    "close": ("Fermer", "Close"),
}
tr = lambda k, **kw: TEXT[k][0 if FR else 1].format(**kw)


def install(home: Path, variant: str, wheel: Path, ui) -> None:
    """Install the environment; ui.step(i, n, text) and ui.line(text) follow the progress."""
    uv = str(bundle() / "uv" / ("uv.exe" if sys.platform == "win32" else "uv"))
    env = {**os.environ, "UV_PYTHON_INSTALL_DIR": str(home / "python"), "UV_CACHE_DIR": str(home / "uv-cache"),
           "NO_COLOR": "1"}
    venv = home / "venv"

    def run(*a):
        """uv, tried again after a pause: on Windows, a step often fails once and passes the next time (file still
        locked by the antivirus scan or by a Taxonero still closing, network hiccup)."""
        for attempt, pause in enumerate((5, 15, None)):
            try:
                return stream([uv, *a], env, ui)
            except subprocess.CalledProcessError:
                if pause is None:
                    raise
                ui.line(tr("retry", what=f"uv {a[0]}", s=pause))
                time.sleep(pause)

    ui.step(1, STEPS, tr("python", v=PYTHON))
    run("venv", "--clear", "--python", PYTHON, str(venv))
    py = str(venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))
    index, packages = TORCH[variant]
    ui.step(2, STEPS, tr("torch", variant=variant))
    try:
        run("pip", "install", "--python", py, *(["--index-url", index] if index else []), *packages)
    except subprocess.CalledProcessError as e:
        raise TorchFailed() from e
    ui.step(3, STEPS, tr("app"))
    run("pip", "install", "--python", py, "-c", str(bundle() / "constraints.txt"), f"{wheel}[desktop,cpu]")


class TorchFailed(Exception):
    """The PyTorch build of the detected GPU could not be installed: the CPU one is tried instead."""


def fetch_models(home: Path, ui) -> None:
    """Models downloaded during the installation rather than at first use (nothing if they are already there). A failure
    (offline) does not prevent launching the application: they will be downloaded at the first analysis."""
    ui.step(STEPS, STEPS, tr("models"))
    py = str(home / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))
    try:
        stream([py, "-m", "taxonero", "desktop", "--fetch-models"],
               {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}, ui)
    except (OSError, subprocess.CalledProcessError):
        ui.line(tr("models_later"))


LOG: Path | None = None   # install.log of the current installation (main)


def log_line(text: str) -> None:
    """Also kept in install.log: the window closes, the log stays (to understand a failure afterwards)."""
    if LOG:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            pass


def stream(cmd: list[str], env: dict, ui) -> None:
    """Run cmd and show its output line by line (window and install.log); CalledProcessError if it fails."""
    log_line("$ " + " ".join(cmd))
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", **NO_WINDOW)
    for line in p.stdout:
        if line.strip():
            ui.line(line.rstrip())
            log_line(line.rstrip())
    if p.wait():
        log_line(f"exit code {p.returncode}")
        raise subprocess.CalledProcessError(p.returncode, cmd[0])


def keep_exiftool(home: Path) -> Path | None:
    """ExifTool copied out of the launcher: on Windows, the launcher (a single file) extracts itself into a temporary
    folder that PyInstaller deletes as soon as it closes, before the application even uses it."""
    src, dest = bundle() / "exiftool", home / "exiftool"
    if not src.exists():
        return None
    tmp, old = home / "exiftool.new", home / "exiftool.old"
    # Copied aside, then swapped by renaming: renaming a folder whose files are open fails without touching
    # anything (Windows: application left open), whereas a deletion would stop half-way.
    for d in (tmp, old):
        shutil.rmtree(d, ignore_errors=True)
    try:
        shutil.copytree(src, tmp)
        if dest.exists():
            dest.rename(old)
        tmp.rename(dest)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        return dest if dest.exists() else None
    shutil.rmtree(old, ignore_errors=True)
    return dest


class ConsoleUI:
    def step(self, i, n, text):
        print(f"[{i}/{n}] {text}…", flush=True)

    def line(self, text):
        print(f"    {text}", flush=True)

    def run(self, work, intro):
        print(intro, flush=True)
        try:
            work()
        except BaseException as e:
            log_line(f"failed: {type(e).__name__}: {e}")
            if LOG:
                print(tr("log", path=LOG), flush=True)
            raise


class WindowUI:
    """Small Tk window: current step, progress bar, last lines from uv. The installation runs in
    a thread; Tk stays in the main thread, messages go through after()."""

    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.root = tk, tk.Tk()
        self.root.title(tr("title"))
        self.root.geometry("620x360")
        self.root.minsize(420, 260)
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)
        self.intro = ttk.Label(frame, wraplength=580, justify="left")
        self.intro.pack(anchor="w")
        self.label = ttk.Label(frame, font=("TkDefaultFont", 11, "bold"))
        self.label.pack(anchor="w", pady=(12, 4))
        self.bar = ttk.Progressbar(frame, maximum=STEPS, mode="determinate")
        self.bar.pack(fill="x")
        self.log = tk.Text(frame, height=10, wrap="none", state="disabled", relief="flat", font=("TkFixedFont", 9))
        self.log.pack(fill="both", expand=True, pady=(12, 0))
        self.close = ttk.Button(frame, text=tr("close"), command=self.root.destroy)
        self.error = None
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

    def _quit(self):
        os._exit(1)   # closed during the installation: uv stops with the process

    def _later(self, fn):
        self.root.after(0, fn)

    def step(self, i, n, text):
        def f():
            self.label.config(text=f"{i}/{n} · {text}…")
            self.bar.config(value=i - 1)
        self._later(f)

    def line(self, text):
        def f():
            self.log.config(state="normal")
            self.log.insert("end", text + "\n")
            self.log.delete("1.0", "end-200l")   # last 200 lines
            self.log.see("end")
            self.log.config(state="disabled")
        self._later(f)

    def run(self, work, intro):
        self.intro.config(text=intro)

        def target():
            try:
                work()
                self._later(self.root.destroy)
            except BaseException as e:
                self.error = e
                log_line(f"failed: {type(e).__name__}: {e}")
                if LOG:
                    self.line(tr("log", path=LOG))

                def failed():
                    self.label.config(text=tr("failed"))
                    self.close.pack(anchor="e", pady=(8, 0))
                    self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
                self._later(failed)

        threading.Thread(target=target, daemon=True).start()
        self.root.mainloop()
        if self.error:
            raise SystemExit(1)


def make_ui():
    try:
        return WindowUI()
    except Exception:   # no Tk (Linux without python3-tk) or no display: console
        return ConsoleUI()


def main() -> None:
    home = data_dir() / "runtime"
    home.mkdir(parents=True, exist_ok=True)
    wheel = next(bundle().glob("wheel/*.whl"))
    marker = home / "installed.json"
    state = json.loads(marker.read_text()) if marker.exists() else {}
    variant, why = detect()
    stamp = f"{wheel.name}:{hashlib.sha256(wheel.read_bytes()).hexdigest()[:16]}"   # same version, other content: installed again
    if "--reinstall" in sys.argv or state.get("wheel") != stamp or (os.environ.get("TAXONERO_GPU") and state.get("variant") != variant):
        global LOG
        LOG = home / "install.log"
        LOG.unlink(missing_ok=True)   # the last installation only
        log_line(f"{wheel.name}, {platform.platform()}, {variant} ({why})")
        ui = make_ui()

        def work():
            nonlocal variant
            try:
                install(home, variant, wheel, ui)
            except TorchFailed:   # only PyTorch: another failure would fail the same way on the CPU
                if variant == "cpu":
                    raise
                ui.line(tr("fallback", variant=variant))
                log_line(tr("fallback", variant=variant))
                variant = "cpu"
                install(home, variant, wheel, ui)
            keep_exiftool(home)
            marker.write_text(json.dumps({"wheel": stamp, "variant": variant}))
            fetch_models(home, ui)
        ui.run(work, tr("intro", why=why))
    py = home / "venv" / ("Scripts/pythonw.exe" if sys.platform == "win32" else "bin/python")
    env = {**os.environ}
    exiftool = home / "exiftool"
    if not exiftool.exists():   # installed by an earlier version
        keep_exiftool(home)
    if exiftool.exists():
        env["TAXONERO_EXIFTOOL"] = str(exiftool)
    args = [str(py), "-m", "taxonero", "desktop"]
    if sys.platform == "win32":
        subprocess.Popen(args, env=env, **NO_WINDOW)   # Windows: no exec, the application goes on alone
    else:
        os.execve(args[0], args, env)


if __name__ == "__main__":
    main()
