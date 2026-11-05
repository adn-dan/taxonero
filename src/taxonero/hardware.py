"""Choix du matériel de calcul : GPU s'il y en a un d'utilisable, sinon processeur.

Deux moteurs tournent les modèles :
- PyTorch (OWLv2, BioCLIP 2) : 'cuda' couvre NVIDIA (CUDA) et AMD (ROCm, qui se présente aussi comme 'cuda'),
  'xpu' les GPU Intel (Arc, Data Center, iGPU récents), 'mps' Apple ;
- ONNX Runtime (MegaDetector) : un « fournisseur » par famille de GPU, présent seulement si le paquet
  onnxruntime installé le contient (onnxruntime-gpu pour NVIDIA, onnxruntime-rocm / -migraphx pour AMD,
  onnxruntime-openvino pour Intel).
"""

import logging
import subprocess
import sys
from functools import cache
from importlib import metadata

log = logging.getLogger(__name__)

DEVICES = ("auto", "cpu", "cuda", "xpu", "mps")
# Fournisseurs ONNX Runtime par famille, par ordre de préférence (TensorRT écarté : il compile au premier
# lancement et demande ses propres bibliothèques).
ONNX_PROVIDERS = {
    "nvidia": ["CUDAExecutionProvider"],
    "amd": ["MIGraphXExecutionProvider", "ROCMExecutionProvider"],
    "intel": ["OpenVINOExecutionProvider"],
    "apple": ["CoreMLExecutionProvider"],
    "cpu": [],
}


def _torch():
    import torch
    return torch


def _vendor(device: str) -> str:
    if device == "cuda":
        return "amd" if getattr(_torch().version, "hip", None) else "nvidia"
    return {"xpu": "intel", "mps": "apple"}.get(device, "cpu")


def _available(device: str) -> bool:
    torch = _torch()
    if device == "cuda":
        return torch.cuda.is_available()
    if device == "xpu":
        return hasattr(torch, "xpu") and torch.xpu.is_available()
    if device == "mps":
        return hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
    return device == "cpu"


# Calcul d'essai sur le GPU, dans un processus à part : certains GPU se déclarent disponibles puis font planter le
# processus au premier calcul (Intel Iris Xe avec PyTorch XPU : erreur de segmentation, constaté). Un plantage ici
# ne coûte que le processus d'essai ; l'analyse repasse alors sur le processeur.
PROBE = "import sys, torch; x = torch.ones(64, 64, device=sys.argv[1]); print(float((x @ x).sum()))"
PROBE_TIMEOUT = 300   # premier lancement : chargement des bibliothèques du GPU, parfois compilation des noyaux


def _works(device: str) -> bool:
    """Vrai si un petit calcul sur ce GPU aboutit au bon résultat (64 × 64 × 64)."""
    try:
        r = subprocess.run([sys.executable, "-c", PROBE, device], capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("GPU %s : calcul d'essai impossible (%s), analyse sur le processeur", device, e)
        return False
    if r.returncode == 0 and r.stdout.strip().endswith("262144.0"):
        return True
    log.warning("GPU %s détecté mais inutilisable (calcul d'essai : code %s), analyse sur le processeur. %s",
                device, r.returncode, r.stderr.strip()[-500:])
    return False


# Raison du choix (code traduit par le dashboard ; texte pour le journal).
WHY = {"cpu_chosen": "processeur choisi", "gpu_found": "GPU détecté", "gpu_requested": "GPU demandé",
       "no_gpu": "aucun GPU utilisable par PyTorch : processeur", "fallback": "GPU demandé indisponible : repli sur le processeur",
       "gpu_broken": "GPU détecté mais inutilisable (calcul d'essai en échec) : processeur"}


@cache
def resolve(wanted: str) -> tuple[str, str]:
    """(périphérique PyTorch retenu, code de la raison, voir WHY). 'auto' : le premier GPU utilisable, sinon le
    processeur. Un GPU demandé mais absent : repli sur le processeur (signalé) plutôt qu'un arrêt de l'analyse."""
    wanted = (wanted or "auto").lower()
    if wanted == "cpu":
        return "cpu", "cpu_chosen"
    candidates = ("cuda", "xpu", "mps") if wanted == "auto" else (wanted,)
    broken = False
    for d in candidates:
        try:
            if not _available(d):
                continue
        except Exception as e:   # pilote ou bibliothèque absents
            log.debug("périphérique %s indisponible : %s", d, e)
            continue
        if _works(d):
            return d, "gpu_found" if wanted == "auto" else "gpu_requested"
        broken = True
    if broken:
        return "cpu", "gpu_broken"
    if wanted == "auto":
        return "cpu", "no_gpu"
    log.warning("DEVICE=%s demandé mais indisponible : analyse sur le processeur", wanted)
    return "cpu", "fallback"


# Options des fournisseurs ONNX : calcul en float32 complet (le CUDA d'ONNX Runtime active TF32 par défaut).
ONNX_OPTIONS = {"CUDAExecutionProvider": {"use_tf32": "0"}}


def onnx_providers(device: str) -> list[str]:
    """Fournisseurs ONNX Runtime à utiliser pour ce périphérique, limités à ceux du paquet installé."""
    import onnxruntime as ort
    have = ort.get_available_providers()
    return [p for p in ONNX_PROVIDERS[_vendor(device)] if p in have] + ["CPUExecutionProvider"]


def onnx_session_providers(device: str) -> list:
    """Même liste, avec les options de précision : à passer à InferenceSession."""
    return [(p, ONNX_OPTIONS[p]) if p in ONNX_OPTIONS else p for p in onnx_providers(device)]


def full_precision() -> None:
    """Mêmes résultats sur GPU que sur processeur : float32 complet partout. Sur les GPU NVIDIA récents
    (Ampere et suivants), cuDNN calcule les convolutions en TF32 par défaut (mantisse de 10 bits au lieu de
    23) ; on le désactive, comme pour les produits matriciels. Coût : un peu de vitesse, pas de précision."""
    torch = _torch()
    torch.set_float32_matmul_precision("highest")
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False


def release(device: str) -> None:
    """Rend la mémoire du GPU une fois les modèles abandonnés (changement de périphérique, inactivité)."""
    import gc
    gc.collect()
    torch = _torch()
    if device == "cuda" and torch.cuda.is_available():
        torch.cuda.empty_cache()
    elif device == "xpu" and hasattr(torch, "xpu") and torch.xpu.is_available():
        torch.xpu.empty_cache()
    elif device == "mps" and torch.backends.mps.is_available():
        torch.mps.empty_cache()


# OWLv2 en mode auto : sur chaque photo avec un GPU ; sur processeur, seulement quand MegaDetector ne trouve
# aucun animal (3 fois plus rapide pour 1 photo de moins sur 73 au benchmark : README, Matériel de calcul).
AUTO_OPEN_MODE = {"gpu": "always", "cpu": "fallback"}


def open_mode(wanted: str, device: str) -> str:
    """Mode d'OWLv2 réellement appliqué : always | fallback."""
    return AUTO_OPEN_MODE["cpu" if device == "cpu" else "gpu"] if wanted == "auto" else wanted


@cache
def describe(wanted: str) -> dict:
    """Ce qui est réellement utilisé : affiché dans la page Paramètres."""
    device, why = resolve(wanted)
    info = {"wanted": wanted, "device": device, "why": why, "vendor": _vendor(device), "gpu": None, "memory_gb": None}
    torch = _torch()
    try:
        if device == "cuda":
            p = torch.cuda.get_device_properties(0)
            info.update(gpu=p.name, memory_gb=round(p.total_memory / 2 ** 30, 1),
                        runtime=f"ROCm {torch.version.hip}" if torch.version.hip else f"CUDA {torch.version.cuda}")
        elif device == "xpu":
            p = torch.xpu.get_device_properties(0)
            info.update(gpu=p.name, memory_gb=round(p.total_memory / 2 ** 30, 1), runtime="Intel XPU")
        elif device == "mps":
            info.update(gpu="Apple", runtime="Metal (MPS)")
    except Exception as e:
        log.debug("description du GPU impossible : %s", e)
    import onnxruntime as ort
    info["onnx_available"] = ort.get_available_providers()
    info["onnx_wanted"] = onnx_providers(device)
    info["versions"] = {name: _version(name) for name in ("torch", "onnxruntime", "onnxruntime-gpu", "pybioclip", "transformers")}
    info["versions"] = {k: v for k, v in info["versions"].items() if v}
    return info


def _version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None
