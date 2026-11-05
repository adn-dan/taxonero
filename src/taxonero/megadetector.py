"""MegaDetector v6 (variante RT-DETR « apa », Apache 2.0) dans PyTorch.

Les poids officiels sont téléchargés là où l'amont les publie (Zenodo, comme le fait PytorchWildlife), empreinte
vérifiée, au premier usage. Le fichier publié (1,2 Go) contient aussi l'état d'entraînement : seuls les poids
utiles sont gardés (~300 Mo), l'original est supprimé. Le modèle est construit avec le code RT-DETRv2 repris dans
taxonero/rtdetr (Apache 2.0), sans PytorchWildlife ni ultralytics (AGPL).

Dans PyTorch, MegaDetector tourne sur le même GPU que OWLv2 et BioCLIP, quel qu'il soit (NVIDIA, AMD, Intel,
Apple), sans variante d'ONNX Runtime par GPU. Un fichier ONNX (DETECTOR_PATH, images Docker d'avant) reste
utilisé s'il existe.
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
    """Poids utiles de MegaDetector (téléchargés et extraits au premier appel)."""
    import torch

    from .worker import fetch_model
    compact = models_dir / f"{VERSION}.weights.pt"
    if compact.exists():
        return compact
    full = models_dir / f"{VERSION}.pth"
    # Le point de contrôle complet se lit avec weights_only=False (pickle : peut exécuter du code) : son empreinte est
    # vérifiée avant chaque lecture, pas seulement au téléchargement (fichier déposé à la main, ou abîmé).
    if full.exists() and _sha256(full) != SHA256:
        log.warning("MegaDetector : %s ne correspond pas à l'empreinte attendue, téléchargé de nouveau", full.name)
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
    """Modèle de déploiement (réseau + post-traitement), comme celui exporté en ONNX : (images, tailles d'origine)
    -> (labels, boîtes xyxy en pixels d'origine, scores)."""
    import torch
    import torch.nn as nn

    from .rtdetr.src.core import YAMLConfig
    # pretrained=False : le backbone n'a pas à télécharger ses poids ImageNet, remplacés par ceux de MegaDetector.
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
    """Même interface et mêmes résultats que pipeline.Detector (ONNX)."""

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
