import os
import re
from importlib import metadata
from pathlib import Path


def _version() -> str:
    """Version of the program: pyproject.toml of a source checkout (development, always current), otherwise the
    metadata of the installed package (Docker image, desktop application)."""
    try:
        text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        if m := re.search(r'^name = "taxonero"\s*\nversion = "([^"]+)"', text, re.M):
            return m[1]
    except OSError:
        pass
    try:
        return metadata.version("taxonero")
    except metadata.PackageNotFoundError:
        return "?"


__version__ = _version()

# Mac (Apple GPU, MPS): an operation Metal cannot do runs on the CPU instead of stopping the analysis.
# To be set before the first import of torch.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
