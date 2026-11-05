"""Lanceur de l'édition de bureau (exécutable léger, bibliothèque standard seulement).

Convention des applications de bureau qui reposent sur PyTorch (ComfyUI Desktop, Invoke, Stability Matrix) : un
seul petit exécutable par système, qui installe au premier lancement le PyTorch adapté au GPU de la machine, avec
uv (Astral), dans le dossier de l'utilisateur. Un exécutable par GPU serait énorme (PyTorch CUDA : 2 à 3 Go) et
dépasserait la limite de 2 Go par fichier des versions GitHub.

L'exécutable contient : uv, la roue de taxonero, constraints.txt et ExifTool. Au premier lancement (ou après une
mise à jour) : Python 3.12 (géré par uv), PyTorch pour le GPU détecté, puis taxonero ; ensuite il lance
« taxonero desktop » dans cet environnement. L'installation s'affiche dans une petite fenêtre (Tk) ; sans
affichage graphique, dans la console.

    TAXONERO_GPU=cpu|cu130|cu128|rocm|xpu|mps|amd-win   impose une variante (sinon détection automatique)
    taxonero --reinstall                                   refait l'installation (autre GPU, réparation)
"""

import hashlib
import json
import locale
import os
import platform
import re
import subprocess
import sys
import threading
from pathlib import Path

PYTHON = "3.12"
PYTORCH = "https://download.pytorch.org/whl/"
AMD_WIN = "https://repo.radeon.com/rocm/windows/rocm-rel-7.2/"
# Variante -> (index de paquets, paquets PyTorch). Mêmes versions que l'image Docker.
TORCH = {
    "cu130": (PYTORCH + "cu130", ["torch==2.14.1", "torchvision==0.29.1"]),     # NVIDIA, pilote >= 580
    "cu128": (PYTORCH + "cu128", ["torch==2.11.0", "torchvision==0.26.0"]),     # NVIDIA, pilote >= 525
    "rocm": (PYTORCH + "rocm7.2", ["torch==2.14.1", "torchvision==0.29.1"]),    # AMD, Linux
    "xpu": (PYTORCH + "xpu", ["torch==2.14.1", "torchvision==0.29.1"]),         # Intel Arc, Core Ultra
    "cpu": (PYTORCH + "cpu", ["torch==2.14.1", "torchvision==0.29.1"]),
    "mps": (None, ["torch==2.14.1", "torchvision==0.29.1"]),                    # Apple Silicon (PyPI, Metal)
    # AMD sous Windows : version d'aperçu publiée par AMD (Python 3.12, pilote 26.1.1, Radeon RX 7000/9000).
    "amd-win": (None, [AMD_WIN + f for f in (
        "rocm_sdk_core-7.2.0.dev0-py3-none-win_amd64.whl", "rocm_sdk_devel-7.2.0.dev0-py3-none-win_amd64.whl",
        "rocm_sdk_libraries_custom-7.2.0.dev0-py3-none-win_amd64.whl", "rocm-7.2.0.dev0.tar.gz",
        "torch-2.9.1%2Brocmsdk20260116-cp312-cp312-win_amd64.whl",
        "torchvision-0.24.1%2Brocmsdk20260116-cp312-cp312-win_amd64.whl")]),
}
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def bundle() -> Path:
    """Fichiers livrés avec l'exécutable (ou packaging/build en développement)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent / "build"))


def data_dir() -> Path:
    """Même règle que config.user_data_dir (ce lanceur n'importe pas taxonero)."""
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
    """Noms des cartes graphiques (Windows : WMI ; Linux : fabricant PCI des cartes DRM)."""
    if sys.platform == "win32":
        out = _run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"])
        return [x.strip() for x in out.splitlines() if x.strip()]
    names = []
    for card in Path("/sys/class/drm").glob("card[0-9]*/device"):
        if not re.fullmatch(r"card\d+", card.parent.name):   # card1-eDP-1… : connecteurs d'écran, pas des cartes
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
    """Raison du choix, dans la langue de la fenêtre d'installation (voir FR, plus bas)."""
    return fr if FR else en


def detect() -> tuple[str, str]:
    """(variante, raison). Ordre : choix imposé, Apple Silicon, NVIDIA, AMD, Intel, processeur."""
    forced = os.environ.get("TAXONERO_GPU", "").strip()
    if forced:
        if forced not in TORCH:
            raise SystemExit(f"TAXONERO_GPU={forced} inconnue : {' | '.join(TORCH)}")
        return forced, _why("imposée par TAXONERO_GPU", "forced by TAXONERO_GPU")
    if sys.platform == "darwin":
        return ("mps", "Apple Silicon") if platform.machine() == "arm64" else ("cpu", _why("Mac Intel : processeur", "Intel Mac: CPU"))
    arm = platform.machine().lower() in ("arm64", "aarch64")
    driver = nvidia_driver()
    if driver and driver >= 580:   # CUDA 13 : x86 et Linux ARM (NVIDIA Grace, DGX Spark)
        return "cu130", _why(f"NVIDIA, pilote {driver}", f"NVIDIA, driver {driver}")
    if driver and not arm:
        return "cu128", _why(f"NVIDIA, pilote {driver}", f"NVIDIA, driver {driver}")
    names = " ".join(gpu_names())
    if not arm and ("AMD" in names or "Radeon" in names):
        if sys.platform == "win32":
            return "amd-win", _why("AMD Radeon (version d'aperçu d'AMD)", "AMD Radeon (AMD preview build)")
        if Path("/dev/kfd").exists():   # pilote amdgpu avec calcul (ROCm)
            return "rocm", "AMD Radeon (ROCm)"
    if not arm and ("Arc" in names or "(xe)" in names):   # Intel Arc, Core Ultra (pilote xe sous Linux)
        return "xpu", "Intel Arc"
    return "cpu", _why("aucun GPU pris en charge détecté", "no supported GPU found")


FR = (locale.getlocale()[0] or os.environ.get("LANG", "")).lower().startswith("fr")
TEXT = {   # fenêtre d'installation : français ou anglais selon le système
    "title": ("Installation de Taxonero", "Installing Taxonero"),
    "intro": ("Première installation ou mise à jour : {why}. Quelques minutes, une seule fois.",
              "First installation or update: {why}. A few minutes, only once."),
    "python": ("Python {v}", "Python {v}"),
    "torch": ("PyTorch ({variant}), plusieurs centaines de Mo", "PyTorch ({variant}), several hundred MB"),
    "app": ("Taxonero et ses bibliothèques", "Taxonero and its libraries"),
    "fallback": ("Échec de l'installation pour {variant} : repli sur le processeur",
                 "Installation for {variant} failed: falling back to the CPU"),
    "failed": ("L'installation a échoué. Détails ci-dessous ; relancer avec --reinstall pour réessayer.",
               "Installation failed. Details below; run again with --reinstall to retry."),
    "close": ("Fermer", "Close"),
}
tr = lambda k, **kw: TEXT[k][0 if FR else 1].format(**kw)


def install(home: Path, variant: str, wheel: Path, ui) -> None:
    """Installe l'environnement ; ui.step(i, n, texte) et ui.line(texte) suivent la progression."""
    uv = str(bundle() / "uv" / ("uv.exe" if sys.platform == "win32" else "uv"))
    env = {**os.environ, "UV_PYTHON_INSTALL_DIR": str(home / "python"), "UV_CACHE_DIR": str(home / "uv-cache"),
           "NO_COLOR": "1"}
    venv = home / "venv"

    def run(*a):
        p = subprocess.Popen([uv, *a], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", **NO_WINDOW)
        for line in p.stdout:
            ui.line(line.rstrip())
        if p.wait():
            raise subprocess.CalledProcessError(p.returncode, a[0])

    ui.step(1, 3, tr("python", v=PYTHON))
    run("venv", "--clear", "--python", PYTHON, str(venv))
    py = str(venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))
    index, packages = TORCH[variant]
    ui.step(2, 3, tr("torch", variant=variant))
    run("pip", "install", "--python", py, *(["--index-url", index] if index else []), *packages)
    ui.step(3, 3, tr("app"))
    run("pip", "install", "--python", py, "-c", str(bundle() / "constraints.txt"), f"{wheel}[desktop,cpu]")


class ConsoleUI:
    def step(self, i, n, text):
        print(f"[{i}/{n}] {text}…", flush=True)

    def line(self, text):
        print(f"    {text}", flush=True)

    def run(self, work, intro):
        print(intro, flush=True)
        work()


class WindowUI:
    """Petite fenêtre Tk : étape en cours, barre de progression, dernières lignes d'uv. L'installation tourne dans
    un fil ; Tk reste dans le fil principal, les messages passent par after()."""

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
        self.bar = ttk.Progressbar(frame, maximum=3, mode="determinate")
        self.bar.pack(fill="x")
        self.log = tk.Text(frame, height=10, wrap="none", state="disabled", relief="flat", font=("TkFixedFont", 9))
        self.log.pack(fill="both", expand=True, pady=(12, 0))
        self.close = ttk.Button(frame, text=tr("close"), command=self.root.destroy)
        self.error = None
        self.root.protocol("WM_DELETE_WINDOW", self._quit)

    def _quit(self):
        os._exit(1)   # fermée pendant l'installation : uv s'arrête avec le processus

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
            self.log.delete("1.0", "end-200l")   # 200 dernières lignes
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
    except Exception:   # pas de Tk (Linux sans python3-tk) ou pas d'affichage : console
        return ConsoleUI()


def main() -> None:
    home = data_dir() / "runtime"
    home.mkdir(parents=True, exist_ok=True)
    wheel = next(bundle().glob("wheel/*.whl"))
    marker = home / "installed.json"
    state = json.loads(marker.read_text()) if marker.exists() else {}
    variant, why = detect()
    stamp = f"{wheel.name}:{hashlib.sha256(wheel.read_bytes()).hexdigest()[:16]}"   # même version, autre contenu : réinstallé
    if "--reinstall" in sys.argv or state.get("wheel") != stamp or (os.environ.get("TAXONERO_GPU") and state.get("variant") != variant):
        ui = make_ui()

        def work():
            nonlocal variant
            try:
                install(home, variant, wheel, ui)
            except subprocess.CalledProcessError:
                if variant == "cpu":
                    raise
                ui.line(tr("fallback", variant=variant))
                variant = "cpu"
                install(home, variant, wheel, ui)
            marker.write_text(json.dumps({"wheel": stamp, "variant": variant}))
        ui.run(work, tr("intro", why=why))
    py = home / "venv" / ("Scripts/pythonw.exe" if sys.platform == "win32" else "bin/python")
    env = {**os.environ}
    exiftool = bundle() / "exiftool"
    if exiftool.exists():
        env["TAXONERO_EXIFTOOL"] = str(exiftool)
    args = [str(py), "-m", "taxonero", "desktop"]
    if sys.platform == "win32":
        subprocess.Popen(args, env=env, **NO_WINDOW)   # Windows : pas d'exec, l'application continue seule
    else:
        os.execve(args[0], args, env)


if __name__ == "__main__":
    main()
