"""MegaDetector v6 (RT-DETR "apa" variant, Apache 2.0) in PyTorch.

The official weights are downloaded where upstream publishes them (Zenodo, as PytorchWildlife does), checksum
verified, at first use. The published file (1.2 GB) also contains the training state: only the useful weights
are kept (~300 MB), the original is deleted. The model is built with the RT-DETRv2 code taken into
taxonero/rtdetr (Apache 2.0), without PytorchWildlife or ultralytics (AGPL).

In PyTorch, MegaDetector runs on the same GPU as OWLv2 and BioCLIP, whatever it is (NVIDIA, AMD, Intel,
Apple), without an ONNX Runtime variant per GPU. An ONNX file (DETECTOR_PATH, older Docker images) is still
used if it exists.
"""

import hashlib
import logging
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

VERSION = "MDV6-apa-rtdetr-e"
URL = "https://zenodo.org/records/15398270/files/MDV6-apa-rtdetr-e.pth?download=1"
SHA256 = "8017129225455be75998195453282f1984aa1c46ebb77b05dd7db97f1bf79117"
CONFIG = Path(__file__).parent / "rtdetr" / "configs" / "rtdetrv2" / "rtdetrv2_r101vd_6x_megadetector.yml"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(2 ** 20), b""):
            h.update(chunk)
    return h.hexdigest()


def weights(models_dir: Path, user_agent: str = "taxonero") -> Path:
    """Useful MegaDetector weights (downloaded and extracted at the first call)."""
    import torch

    from .worker import fetch_model
    compact = models_dir / f"{VERSION}.weights.pt"
    if compact.exists():
        return compact
    full = models_dir / f"{VERSION}.pth"
    # The full checkpoint is read with weights_only=False (pickle: can run code): its checksum is
    # verified before every read, not only at download (file dropped by hand, or damaged).
    if full.exists() and _sha256(full) != SHA256:
        log.warning("MegaDetector: %s does not match the expected checksum, downloaded again", full.name)
        full.unlink()
    if not full.exists():
        fetch_model(URL, full, SHA256, user_agent)
    ckpt = torch.load(full, map_location="cpu", weights_only=False)
    state = ckpt["ema"]["module"] if "ema" in ckpt else ckpt["model"]
    tmp = compact.with_suffix(".part")
    torch.save(state, tmp)
    tmp.replace(compact)
    full.unlink(missing_ok=True)
    log.info("MegaDetector : poids extraits (%s)", compact.name)
    return compact


def build(state_path: Path):
    """Deployment model (network + post-processing), like the one exported to ONNX: (images, original sizes)
    -> (labels, xyxy boxes in original pixels, scores)."""
    import torch
    import torch.nn as nn

    from .rtdetr.src.core import YAMLConfig
    # pretrained=False: the backbone does not need to download its ImageNet weights, replaced by those of MegaDetector.
    cfg = YAMLConfig(str(CONFIG), PResNet={"pretrained": False})
    cfg.model.load_state_dict(torch.load(state_path, map_location="cpu", weights_only=True))

    class Deployed(nn.Module):
        def __init__(self):
            super().__init__()
            self.model = cfg.model.deploy()
            self.postprocessor = cfg.postprocessor.deploy()

        def forward(self, images, orig_target_sizes):
            return self.postprocessor(self.model(images), orig_target_sizes)

    return Deployed().eval()


class TorchDetector:
    """Same interface and same results as pipeline.Detector (ONNX)."""

    def __init__(self, state_path: Path, device: str = "cpu", min_score: float = 0.5, min_area: float = 0.005):
        import torch
        self._torch = torch
        self.model = build(state_path).to(device)
        self.device, self.min_score, self.min_area = device, min_score, min_area
        self.providers = [f"PyTorch ({device})"]

    def detect(self, image: Image.Image):
        from .pipeline import Box, _nms
        torch = self._torch
        w, h = image.size
        x = np.asarray(image.convert("RGB").resize((640, 640), Image.BILINEAR), dtype=np.float32) / 255.0
        x = torch.from_numpy(x.transpose(2, 0, 1)[None].copy()).to(self.device)
        sizes = torch.tensor([[w, h]], dtype=torch.int64, device=self.device)
        with torch.no_grad():
            labels, boxes, scores = (t.cpu().numpy() for t in self.model(x, sizes))
        out = []
        for lab, (x1, y1, x2, y2), s in zip(labels[0], boxes[0], scores[0]):
            if s < self.min_score or int(lab) != 0:
                continue
            x1, y1, x2, y2 = max(0.0, float(x1)), max(0.0, float(y1)), min(float(w), float(x2)), min(float(h), float(y2))
            if x2 - x1 < 8 or y2 - y1 < 8 or (x2 - x1) * (y2 - y1) < self.min_area * w * h:
                continue
            out.append(Box(x1 / w, y1 / h, (x2 - x1) / w, (y2 - y1) / h, float(s), "animal", None, "md"))
        return _nms(sorted(out, key=lambda b: -b.score))
