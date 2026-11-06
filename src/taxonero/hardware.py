"""Choice of the compute hardware: GPU if a usable one exists, otherwise CPU.

Two engines run the models:
- PyTorch (OWLv2, BioCLIP 2): 'cuda' covers NVIDIA (CUDA) and AMD (ROCm, which also presents itself as 'cuda'),
  'xpu' Intel GPUs (Arc, Data Center, recent iGPUs), 'mps' Apple;
- ONNX Runtime (MegaDetector): one "provider" per GPU family, present only if the installed onnxruntime
  package contains it (onnxruntime-gpu for NVIDIA, onnxruntime-rocm / -migraphx for AMD,
  onnxruntime-openvino for Intel).
"""

import logging
import subprocess
import sys
from functools import cache
from importlib import metadata

log = logging.getLogger(__name__)

DEVICES = ("auto", "cpu", "cuda", "xpu", "mps")
# ONNX Runtime providers per family, in order of preference (TensorRT left out: it compiles at the first
# launch and needs its own libraries).
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


# Test computation on the GPU, in a separate process: some GPUs report themselves available then crash the
# process at the first computation (Intel Iris Xe with PyTorch XPU: segmentation fault, observed). A crash here
# only costs the test process; the analysis then falls back to the CPU.
PROBE = "import sys, torch; x = torch.ones(64, 64, device=sys.argv[1]); print(float((x @ x).sum()))"
PROBE_TIMEOUT = 300   # first launch: loading of the GPU libraries, sometimes kernel compilation


def _works(device: str) -> bool:
    """True if a small computation on this GPU gives the right result (64 × 64 × 64)."""
    try:
        r = subprocess.run([sys.executable, "-c", PROBE, device], capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("GPU %s: test computation impossible (%s), analysis on the CPU", device, e)
        return False
    if r.returncode == 0 and r.stdout.strip().endswith("262144.0"):
        return True
    log.warning("GPU %s detected but unusable (test computation: code %s), analysis on the CPU. %s",
                device, r.returncode, r.stderr.strip()[-500:])
    return False


# Reason for the choice (code translated by the dashboard; text for the log).
WHY = {"cpu_chosen": "CPU chosen", "gpu_found": "GPU detected", "gpu_requested": "GPU requested",
       "no_gpu": "no GPU usable by PyTorch: CPU", "fallback": "requested GPU unavailable: falling back to the CPU",
       "gpu_broken": "GPU detected but unusable (test computation failed): CPU"}


@cache
def resolve(wanted: str) -> tuple[str, str]:
    """(PyTorch device kept, reason code, see WHY). 'auto': the first usable GPU, otherwise the
    CPU. A GPU requested but absent: falls back to the CPU (reported) rather than stopping the analysis."""
    wanted = (wanted or "auto").lower()
    if wanted == "cpu":
        return "cpu", "cpu_chosen"
    candidates = ("cuda", "xpu", "mps") if wanted == "auto" else (wanted,)
    broken = False
    for d in candidates:
        try:
            if not _available(d):
                continue
        except Exception as e:   # missing driver or library
            log.debug("device %s unavailable: %s", d, e)
            continue
        if _works(d):
            return d, "gpu_found" if wanted == "auto" else "gpu_requested"
        broken = True
    if broken:
        return "cpu", "gpu_broken"
    if wanted == "auto":
        return "cpu", "no_gpu"
    log.warning("DEVICE=%s requested but unavailable: analysis on the CPU", wanted)
    return "cpu", "fallback"


# ONNX provider options: full float32 computation (ONNX Runtime's CUDA enables TF32 by default).
ONNX_OPTIONS = {"CUDAExecutionProvider": {"use_tf32": "0"}}


def onnx_providers(device: str) -> list[str]:
    """ONNX Runtime providers to use for this device, limited to those of the installed package."""
    import onnxruntime as ort
    have = ort.get_available_providers()
    return [p for p in ONNX_PROVIDERS[_vendor(device)] if p in have] + ["CPUExecutionProvider"]


def onnx_session_providers(device: str) -> list:
    """Same list, with the precision options: to pass to InferenceSession."""
    return [(p, ONNX_OPTIONS[p]) if p in ONNX_OPTIONS else p for p in onnx_providers(device)]


def full_precision() -> None:
    """Same results on GPU as on CPU: full float32 everywhere. On recent NVIDIA GPUs
    (Ampere and later), cuDNN computes convolutions in TF32 by default (10-bit mantissa instead of
    23); it is disabled, as for matrix products. Cost: a bit of speed, no precision."""
    torch = _torch()
    torch.set_float32_matmul_precision("highest")
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False


def release(device: str) -> None:
    """Give the GPU memory back once the models are dropped (device change, inactivity)."""
    import gc
    gc.collect()
    torch = _torch()
    if device == "cuda" and torch.cuda.is_available():
        torch.cuda.empty_cache()
    elif device == "xpu" and hasattr(torch, "xpu") and torch.xpu.is_available():
        torch.xpu.empty_cache()
    elif device == "mps" and torch.backends.mps.is_available():
        torch.mps.empty_cache()


# OWLv2 in auto mode: on every photo with a GPU; on the CPU, only when MegaDetector finds
# no animal (3 times faster for 1 photo fewer out of 73 in the benchmark: README, Compute hardware).
AUTO_OPEN_MODE = {"gpu": "always", "cpu": "fallback"}


def open_mode(wanted: str, device: str) -> str:
    """OWLv2 mode actually applied: always | fallback."""
    return AUTO_OPEN_MODE["cpu" if device == "cpu" else "gpu"] if wanted == "auto" else wanted


@cache
def describe(wanted: str) -> dict:
    """What is actually used: shown in the Settings page."""
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
        log.debug("GPU description impossible: %s", e)
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
