"""Analysis of an image: detection (MegaDetector v6 for wildlife, open-vocabulary OWLv2
for plants, fungi and small animals), classification (BioCLIP 2), geographic
reweighting (GBIF), hierarchical decision and cleanup of overlapping detections.
"""

import io
import json
import time
import zlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import httpx
from PIL import Image, ImageOps

from .taxonomy import RANKS, Taxonomy

log = logging.getLogger(__name__)



@dataclass
class Box:
    x: float  # normalised 0..1, top left corner
    y: float
    w: float
    h: float
    score: float
    label: str                 # animal | plant | fungus | image (no box: whole image)
    kingdom: str | None = None  # kingdom hint given by the detector (Animalia, Plantae, Fungi)
    source: str = "md"          # md (MegaDetector) | owl (OWLv2) | image | manual

    @property
    def area(self) -> float:
        return self.w * self.h


@dataclass
class Candidate:
    name: str                      # scientific name (binomial) given by the model
    score: float                   # model probability
    lineage: dict[str, str]
    gbif_key: int | None = None
    accepted_name: str | None = None
    plausibility: str = "unknown"
    geo_weight: float = 1.0        # prior p(species | place), not normalised
    posterior: float = 0.0         # after geographic reweighting (and filtering of excluded candidates)
    prior: float = 0.0             # same probability, before filtering (used to restore an excluded candidate)


@dataclass
class Decision:
    gbif_key: int | None
    name: str | None
    rank: str                      # species, genus, family... or 'none'
    confidence: float              # cumulative probability at the kept rank
    margin: float
    plausibility: str
    candidates: list[Candidate] = field(default_factory=list)
    excluded: list[Candidate] = field(default_factory=list)   # excluded by the settings


def average(a: list[Candidate], b: list[Candidate]) -> list[Candidate]:
    """Mean of the scores of two candidate lists (missing from a list = score 0)."""
    out: dict[str, Candidate] = {}
    for cands in (a, b):
        for c in cands:
            if c.name not in out:
                out[c.name] = Candidate(name=c.name, score=0.0, lineage=dict(c.lineage))
            out[c.name].score += c.score / 2
    return sorted(out.values(), key=lambda c: -c.score)


def image_quality(image: Image.Image) -> float:
    """Visual quality 0..1: half colourfulness (Hasler and Süsstrunk index, zero in black and white
    like infrared camera traps), half sharpness (variance of the Laplacian)."""
    rgb = np.asarray(image.convert("RGB").resize((256, 256)), dtype=np.float32)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    rg, yb = r - g, 0.5 * (r + g) - b
    colorful = float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))
    gray = rgb.mean(axis=2)
    lap = gray[1:-1, 1:-1] * 4 - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
    return 0.5 * min(1.0, colorful / 60.0) + 0.5 * min(1.0, float(lap.var()) / 400.0)


class Detector:
    """MegaDetector v6 (ONNX). Model classes: 0 animal, 1 person, 2 vehicle; only 0 is kept."""

    def __init__(self, onnx_path: str | Path, providers: list[str] | None = None, min_score: float = 0.5,
                 min_area: float = 0.005):
        import onnxruntime as ort
        names = [p[0] if isinstance(p, tuple) else p for p in providers or []]
        if "CUDAExecutionProvider" in names and hasattr(ort, "preload_dlls"):
            # CUDA and cuDNN libraries installed by PyTorch (nvidia-* packages): without this preload,
            # ONNX Runtime does not find them and silently falls back to the CPU.
            try:
                ort.preload_dlls()
            except Exception as e:
                log.warning("CUDA preload for ONNX Runtime failed: %s", e)
        self.session = ort.InferenceSession(str(onnx_path), providers=providers or ort.get_available_providers())
        self.min_score = min_score
        self.min_area = min_area  # fraction of the image: below it, almost always noise

    def detect(self, image: Image.Image) -> list[Box]:
        w, h = image.size
        x = np.asarray(image.convert("RGB").resize((640, 640), Image.BILINEAR), dtype=np.float32) / 255.0
        x = x.transpose(2, 0, 1)[None]
        labels, boxes, scores = self.session.run(None, {"images": x, "orig_target_sizes": np.array([[w, h]], dtype=np.int64)})
        out = []
        for lab, (x1, y1, x2, y2), s in zip(labels[0], boxes[0], scores[0]):
            if s < self.min_score or int(lab) != 0:
                continue
            # float(): numpy.float32 values would be stored as BLOBs by SQLite.
            x1, y1, x2, y2 = max(0.0, float(x1)), max(0.0, float(y1)), min(float(w), float(x2)), min(float(h), float(y2))
            if x2 - x1 < 8 or y2 - y1 < 8 or (x2 - x1) * (y2 - y1) < self.min_area * w * h:
                continue
            # No kingdom hint: MegaDetector sometimes puts an "animal" box on a plant,
            # and the hint would then penalise the right answer (measured on the dev set).
            out.append(Box(x1 / w, y1 / h, (x2 - x1) / w, (y2 - y1) / h, float(s), "animal", None, "md"))
        return _nms(sorted(out, key=lambda b: -b.score))


class OpenVocabDetector:
    """OWLv2 (Apache 2.0) queried with free text: plants, fungi, lichens, and the small
    animals (insects, spiders) that MegaDetector, trained on camera traps, detects poorly."""

    QUERIES = {  # query -> (label, kingdom)
        "a flower": ("plant", "Plantae"), "a plant": ("plant", "Plantae"), "a leaf": ("plant", "Plantae"),
        "a tree": ("plant", "Plantae"), "a moss": ("plant", "Plantae"), "a fern": ("plant", "Plantae"),
        "a mushroom": ("fungus", "Fungi"), "a lichen": ("fungus", "Fungi"),
        "an insect": ("animal", "Animalia"), "a butterfly": ("animal", "Animalia"), "a spider": ("animal", "Animalia"),
        "a snail": ("animal", "Animalia"), "a bird": ("animal", "Animalia"), "an animal": ("animal", "Animalia"),
        # Domestic versus wild: OWLv2 keeps, for each box, the best-scoring query. Without the
        # wild competitors, "a dog" fired on wolves and foxes (measured on the dev set).
        # "domestic" boxes are only hints; "wild" boxes remain subjects (animal).
        "a dog": ("domestic", "Canidae"), "a cat": ("domestic", "Felidae"), "a horse": ("domestic", "Equidae"),
        "a cow": ("domestic", "Bovidae"), "a sheep": ("domestic", "Bovidae"), "a goat": ("domestic", "Bovidae"),
        "a chicken": ("domestic", "Phasianidae"),
        "a wolf": ("animal", "Animalia"), "a fox": ("animal", "Animalia"), "a wild cat": ("animal", "Animalia"),
        "a deer": ("animal", "Animalia"), "a wild boar": ("animal", "Animalia"), "a pheasant": ("animal", "Animalia"),
    }

    def __init__(self, model_id: str = "google/owlv2-base-patch16-ensemble", device: str = "cpu",
                 min_score: float = 0.3, min_area: float = 0.005):
        import torch
        from transformers import Owlv2ForObjectDetection, Owlv2Processor
        self._torch = torch
        self.processor = Owlv2Processor.from_pretrained(model_id)
        self.model = Owlv2ForObjectDetection.from_pretrained(model_id).to(device).eval()
        self.device, self.min_score, self.min_area = device, min_score, min_area
        self.queries = list(self.QUERIES)
        # The queries never change: their text embeddings are computed once here, instead of
        # going through the text tower for every photo (as model(**inputs) does). Same boxes.
        tok = self.processor(text=[self.queries], return_tensors="pt").to(device)
        with torch.no_grad():
            q = self.model.owlv2.get_text_features(input_ids=tok["input_ids"], attention_mask=tok["attention_mask"]).pooler_output
        self._query = (q / torch.linalg.norm(q, ord=2, dim=-1, keepdim=True))[None]
        self._query_mask = (tok["input_ids"][:, 0] > 0)[None]

    # OWLv2 pads the image to a square then scales it to 960 px: done by its processor on a
    # 1440 px preview, this costs ~230 ms per photo on 4 cores, while the GPU waits. Reduced to 960 px first
    # with Pillow: ~26 ms, same boxes on 19 dev photos out of 20 (one box at 0.31, right at the threshold, lost).
    INPUT_SIDE = 960

    # Photos per pass on the GPU: OWLv2 at 960 px is heavy in memory, half of the analysis batch (set by the
    # worker from the GPU memory, see hardware.batch_size).
    batch = 4

    def detect(self, image: Image.Image) -> list[Box]:
        return self.detect_many([image])[0]

    def detect_many(self, images: list[Image.Image]) -> list[list[Box]]:
        """Several photos per pass on the GPU (same boxes as one by one); one by one if its memory runs out."""
        out = []
        n = max(1, self.batch)
        for i in range(0, len(images), n):
            out += on_gpu_batch(self._detect_batch, self.detect_one, images[i:i + n])
        return out

    def detect_one(self, image: Image.Image) -> list[Box]:
        return self._detect_batch([image])[0]

    def _detect_batch(self, images: list[Image.Image]) -> list[list[Box]]:
        from transformers.models.owlv2.modeling_owlv2 import Owlv2ObjectDetectionOutput
        small = []
        for image in images:
            k = self.INPUT_SIDE / max(image.size)
            if k < 1:
                image = image.resize((round(image.width * k), round(image.height * k)), Image.Resampling.BILINEAR, reducing_gap=2.0)
            small.append(image)
        pixels = self.processor(images=small, return_tensors="pt")["pixel_values"].to(self.device)
        with self._torch.no_grad():
            feature_map = self.model.image_embedder(pixel_values=pixels)[0]
            b, ph, pw, dim = feature_map.shape
            feats = feature_map.reshape(b, ph * pw, dim)
            query, mask = self._query.expand(b, -1, -1), self._query_mask.expand(b, -1)
            logits = self.model.class_predictor(feats, query, mask)[0]
            outputs = Owlv2ObjectDetectionOutput(logits=logits, pred_boxes=self.model.box_predictor(feats, feature_map))
        # OWLv2 works on the image padded to a square
        results = self.processor.post_process_grounded_object_detection(
            outputs, threshold=self.min_score, target_sizes=[(max(im.size), max(im.size)) for im in small])
        return [self._boxes(im.size, res) for im, res in zip(small, results)]

    def _boxes(self, size, res) -> list[Box]:
        w, h = size
        out = []
        for s, lab, (x1, y1, x2, y2) in zip(res["scores"], res["labels"], res["boxes"]):
            x1, y1 = max(0.0, float(x1)), max(0.0, float(y1))
            x2, y2 = min(float(w), float(x2)), min(float(h), float(y2))
            if x2 - x1 < 8 or y2 - y1 < 8 or (x2 - x1) * (y2 - y1) < self.min_area * w * h:
                continue
            label, kingdom = self.QUERIES[self.queries[int(lab)]]
            out.append(Box(x1 / w, y1 / h, (x2 - x1) / w, (y2 - y1) / h, float(s), label, kingdom, "owl"))
        return _nms(sorted(out, key=lambda b: -b.score))


MD_SURE = 0.8


def on_gpu_batch(batch: Callable, one: Callable, images: list) -> list:
    """batch(images), or one(image) for each if the GPU runs out of memory for the whole batch (detections in a
    batch are computed per image: same results)."""
    if len(images) <= 1:
        return [one(im) for im in images]
    try:
        return batch(images)
    except RuntimeError as e:   # torch.OutOfMemoryError is a RuntimeError
        if "out of memory" not in str(e).lower():
            raise
        from . import hardware
        hardware.OOM_EVENTS += 1   # smaller automatic batches from now on (hardware.auto_batch)
        log.warning("GPU memory full for %d photos at once: one by one, then smaller batches", len(images))
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        return [one(im) for im in images]


def merge_boxes(primary: list[Box], secondary: list[Box], max_boxes: int = 5) -> list[Box]:
    """Merge two detectors: MegaDetector first, OWLv2 adds what overlaps nothing.
    A box covering almost the whole image becomes "whole image" (the subject is the scene)."""
    def overlaps(a: Box, b: Box) -> bool:
        return _iou(a, b) >= 0.3 or _contained(a, b) >= 0.6 or _contained(b, a) >= 0.6

    # MegaDetector unsure about what OWLv2 sees as a plant or a fungus: typical false
    # positive (flower taken for an animal). Real animals come out at >= 0.9.
    flora = [b for b in secondary if b.label in ("plant", "fungus") and b.area <= 0.8]
    kept = [b for b in primary if b.score >= MD_SURE or not any(overlaps(b, f) for f in flora)]
    for b in secondary:
        if all(_iou(b, k) < 0.3 and _contained(b, k) < 0.6 and _contained(k, b) < 0.6 for k in kept):
            kept.append(b)
    for i, b in enumerate(kept):
        if b.area > 0.8:
            kept[i] = Box(0.0, 0.0, 1.0, 1.0, b.score, b.label, b.kingdom, b.source)
    return kept[:max_boxes]


def _iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    inter = ix * iy
    return inter / (a.w * a.h + b.w * b.h - inter + 1e-9)


def _contained(a: Box, b: Box) -> float:
    """Fraction of box a contained in b."""
    ix = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    return ix * iy / (a.w * a.h + 1e-9)


def _nms(boxes: list[Box], thr: float = 0.5, contain_thr: float = 0.7) -> list[Box]:
    """Duplicate removal: overlap (IoU) or box contained in a better-scored box
    (MegaDetector sometimes detects the whole animal and parts of itself)."""
    kept: list[Box] = []
    for b in boxes:
        if all(_iou(b, k) < thr and _contained(b, k) < contain_thr and _contained(k, b) < contain_thr for k in kept):
            kept.append(b)
    return kept


# Crop smaller than this in the preview (1440 px on the short side by default): redone from the original if available.
# BioCLIP works at 224 px; with the margin, below ~400 px the preview already loses detail.
SMALL_CROP_PX = 400


def crop_side(image: Image.Image, box: Box, margin: float = 0.15) -> float:
    w, h = image.size
    return max(box.w * w, box.h * h) * (1 + 2 * margin)


# Maximum side of an image decoded in full (original, image dropped on the Identify page): well beyond
# what small subjects need (SMALL_CROP_PX), and ~50 MB in memory instead of ~150 MB for 50 Mpx.
MAX_SIDE = 4096


def open_image(data: bytes | Image.Image, max_side: int | None = None) -> Image.Image:
    image = data if isinstance(data, Image.Image) else Image.open(io.BytesIO(data))
    if max_side and max(image.size) > max_side:
        k = max_side / max(image.size)
        image.draft("RGB", (int(image.width * k), int(image.height * k)))   # JPEG: decoded directly at reduced size
    image = ImageOps.exif_transpose(image).convert("RGB")
    if max_side and max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return image


def exif_latlon(data: bytes) -> tuple[float | None, float | None]:
    """GPS coordinates written in the EXIF of an image (None, None if absent or unreadable)."""
    try:
        gps = Image.open(io.BytesIO(data)).getexif().get_ifd(0x8825)
        def deg(v, ref):
            d, m, sec = (float(x) for x in v)
            r = d + m / 60 + sec / 3600
            return -r if ref in ("S", "W") else r
        lat, lon = deg(gps[2], gps[1]), deg(gps[4], gps[3])
        # Receiver without a fix: some cameras write 0/0 (NaN once divided) or out-of-range values.
        return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else (None, None)
    except Exception:
        return None, None


def same_frame(a: Image.Image, b: Image.Image, tol: float = 0.01) -> bool:
    """Same framing (width/height ratio): otherwise the original was cropped or rotated since the preview."""
    return abs(a.width / a.height - b.width / b.height) <= tol * a.width / a.height


def crop(image: Image.Image, box: Box, margin: float = 0.15) -> Image.Image:
    """Crop with a margin (context helps classification), square if possible."""
    w, h = image.size
    cx, cy = (box.x + box.w / 2) * w, (box.y + box.h / 2) * h
    side = max(box.w * w, box.h * h) * (1 + 2 * margin)
    x1, y1 = max(0, cx - side / 2), max(0, cy - side / 2)
    x2, y2 = min(w, cx + side / 2), min(h, cy + side / 2)
    return image.crop((int(x1), int(y1), int(x2), int(y2)))


def _tol_classifier(device: str):
    """Lightweight BioCLIP 2, same results:
    - the text embeddings of the ~870,000 species (2.7 GB in float32) are read with mmap: on the CPU,
      these pages are disk cache that the kernel can reclaim under memory pressure, instead of private
      memory that ends in the process being killed (OOM);
    - the model's text tower (~0.5 GB), useless once these embeddings are computed, is released."""
    import warnings
    import torch
    from bioclip import TreeOfLifeClassifier

    class Light(TreeOfLifeClassifier):
        def get_txt_emb(self):
            path = self.get_cached_datafile("embeddings/txt_emb_species.npy")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)   # read-only array: never modified
                return torch.from_numpy(np.load(path, mmap_mode="r"))

    clf = Light(device=device)
    m = getattr(clf.model, "_orig_mod", clf.model)   # pybioclip wraps the model in torch.compile
    for name in ("transformer", "token_embedding", "ln_final"):
        setattr(m, name, None)
    for name in ("positional_embedding", "text_projection"):
        setattr(m, name, None)
    import gc
    gc.collect()
    return clf


class Classifier:
    def __init__(self, device: str = "cpu", k: int = 20):
        from bioclip import Rank
        self._rank = Rank.SPECIES
        self.model = _tol_classifier(device)
        self.k = k
        # Images per pass: a GPU processes many at once, the CPU gains nothing.
        self.batch_size = 8 if device == "cpu" else 32

    def classify(self, images: list[Image.Image]) -> list[list[Candidate]]:
        # callback: pybioclip then shows no progress bar (one per batch, in the log of the container)
        preds = self.model.predict(images, self._rank, k=self.k, batch_size=self.batch_size, callback=lambda done, total: None)
        out: list[list[Candidate]] = [[] for _ in images]
        for p in preds:
            idx = int(p["file_name"])  # pybioclip indexes PIL images by position
            lineage = {r: p[r] for r in RANKS[:-1] if p.get(r)}
            name = f"{p['genus']} {p['species_epithet']}"
            lineage["species"] = name
            out[idx].append(Candidate(name=name, score=float(p["score"]), lineage=lineage))
        for c in out:
            c.sort(key=lambda x: -x.score)
        return out


# Weight of a candidate whose kingdom does not match the detector hint (soft hint:
# OWLv2 sometimes confuses alga and lichen, gall and spider).
KINGDOM_MISMATCH = 0.4


MIN_CANDIDATE_SCORE = 0.01
MIN_CANDIDATES = 3


def rerank(cands: list[Candidate], tx: Taxonomy | None, lat: float | None, lon: float | None,
           kingdom_hint: str | None = None, drop: "Callable[[Candidate], bool] | None" = None,
           excluded: list[Candidate] | None = None, tolerant: bool = False) -> list[Candidate]:
    """Normalise names via GBIF (synonyms) and reweight by geographic plausibility
    and, if there is one, by the detector's kingdom hint. drop: candidates excluded (settings),
    added to excluded if given. tolerant: GBIF down, candidates are still proposed (plausibility "unverified",
    neutral weight) instead of failing the action; reserved for manual actions, the automatic analysis retries."""
    # Candidates below 1 %: never shown, and on the benchmark ignoring them changes neither the rate of correct
    # species nor the error rate (54 photos: 8.6 candidates checked instead of 20, two different decisions,
    # one more correct). Each cost up to six GBIF requests.
    cands = [c for i, c in enumerate(sorted(cands, key=lambda c: -c.score)) if i < MIN_CANDIDATES or c.score >= MIN_CANDIDATE_SCORE]
    if tx is not None:
        tx.prefetch_candidates([(c.name, c.lineage.get("kingdom")) for c in cands], lat, lon,
                               fossils=bool(getattr(drop, "fossils", False)))
    merged: dict[object, Candidate] = {}
    for c in cands:
        if tx is not None:
            try:
                t = tx.match(c.name, kingdom=c.lineage.get("kingdom"), enrich=False)
            except httpx.HTTPError:
                if not tolerant:
                    raise
                t, c.plausibility = None, "unverified"
            if t and t.rank in ("species", "subspecies", "variety", "form"):
                c.gbif_key, c.accepted_name = t.gbif_key, t.scientific_name
                c.lineage = {**c.lineage, **t.lineage}
                try:
                    c.plausibility, c.geo_weight = tx.geo(t.gbif_key, lat, lon)
                except httpx.HTTPError:
                    if not tolerant:
                        raise
                    c.plausibility, c.geo_weight = "unverified", 1.0
        key = c.gbif_key or c.name
        if key in merged:  # two model names -> same accepted species: add them up
            merged[key].score += c.score
        else:
            merged[key] = c
    everything = list(merged.values())
    for c in everything:
        c.posterior = c.score * c.geo_weight
        if kingdom_hint and c.lineage.get("kingdom") and c.lineage["kingdom"] != kingdom_hint:
            c.posterior *= KINGDOM_MISMATCH
    z = sum(c.posterior for c in everything) or 1.0
    for c in everything:
        c.prior = c.posterior = c.posterior / z
    def dropped(c: Candidate) -> bool:
        try:   # the filter of the settings (curation.candidate_filter) also takes the place of the photo
            return bool(drop and (drop(c, lat, lon) if hasattr(drop, "fossils") else drop(c)))
        except httpx.HTTPError:
            if not tolerant:
                raise
            return False
    out = [c for c in everything if not dropped(c)]
    if excluded is not None:
        kept = {id(c) for c in out}
        excluded.extend(sorted((c for c in everything if id(c) not in kept), key=lambda c: -c.prior))
    return renormalize(out)


def renormalize(cands: list[Candidate]) -> list[Candidate]:
    """Probabilities (prior) renormalised over the kept candidates, sorted."""
    z = sum(c.prior for c in cands) or 1.0
    for c in cands:
        c.posterior = c.prior / z
    return sorted(cands, key=lambda c: -c.posterior)


def decide(cands: list[Candidate], tx: Taxonomy | None, species_threshold: float = 0.5,
           higher_threshold: float = 0.7, tolerant: bool = False) -> Decision:
    """Keep the most precise rank whose cumulative probability exceeds the threshold."""
    if not cands:
        return Decision(None, None, "none", 0.0, 0.0, "unknown")
    top = cands[0]
    margin = top.posterior - (cands[1].posterior if len(cands) > 1 else 0.0)
    if top.posterior >= species_threshold:
        return Decision(top.gbif_key, top.accepted_name or top.name, "species", top.posterior, margin,
                        top.plausibility, cands)
    for rank in ("genus", "family", "order", "class"):
        groups: dict[str, float] = {}
        for c in cands:
            if c.lineage.get(rank):
                groups[c.lineage[rank]] = groups.get(c.lineage[rank], 0.0) + c.posterior
        if not groups:
            continue
        name, p = max(groups.items(), key=lambda kv: kv[1])
        if p >= higher_threshold:
            # Expected rank: without it, the API answers an ambiguous genus or order with the whole kingdom (key 1, Animalia).
            try:
                t = tx.match(name, kingdom=top.lineage.get("kingdom"), enrich=False, rank=rank) if tx else None
            except httpx.HTTPError:
                if not tolerant:
                    raise
                t = None
            others = sorted(groups.values(), reverse=True)
            return Decision(t.gbif_key if t else None, name, rank, p, p - (others[1] if len(others) > 1 else 0.0),
                            top.plausibility, cands)
    return Decision(None, None, "none", top.posterior, margin, top.plausibility, cands)


@dataclass
class Analysis:
    box: Box
    decision: Decision
    domestic: bool = False
    # Single subject (crop and whole photo averaged): probability of the kept taxon on the crop alone. Very
    # low, the species comes from the whole photo: the box may not contain the subject.
    frame_support: float | None = None


def frame_support(crop: list[Candidate], d: Decision) -> float | None:
    """Probability, on the crop alone (BioCLIP), of the kept taxon (species, or genus, family… at the decided rank)."""
    if d.rank == "none" or not d.candidates:
        return None
    if d.rank == "species":
        return sum(c.score for c in crop if c.name == d.candidates[0].name)
    return sum(c.score for c in crop if c.lineage.get(d.rank) == d.name)


def _grow(b: Box, f: float) -> Box:
    return Box(b.x - b.w * f, b.y - b.h * f, b.w * (1 + 2 * f), b.h * (1 + 2 * f), b.score, b.label, b.kingdom, b.source)


def _genus(a: "Analysis") -> str | None:
    d = a.decision
    return d.candidates[0].lineage.get("genus") if d.candidates else None


# Species (or forms) that are only domestic: excluded outright.
DOMESTIC_NAMES = {"Felis catus", "Canis familiaris", "Canis lupus familiaris", "Equus caballus", "Equus asinus",
                  "Bos taurus", "Ovis aries", "Capra hircus", "Sus domesticus", "Gallus gallus domesticus"}


# Wild species confused with a domestic animal (dog/wolf, cat/wildcat, pig/wild boar,
# feral pigeon, domestic duck): never auto-confirmed, always reviewed.
AMBIGUOUS_WITH_DOMESTIC = {"Canis lupus", "Felis silvestris", "Sus scrofa", "Columba livia", "Anas platyrhynchos",
                           "Gallus gallus", "Equus ferus", "Bos primigenius", "Ovis gmelini", "Capra aegagrus"}


def is_domestic(a: "Analysis", hints: list[Box]) -> bool:
    """Domestic animal: species that is only domestic, or ambiguous family when OWLv2 sees on the same subject
    "a dog", "a cat"… rather than their wild counterpart. The family comes from the hint (Canidae…)
    and must be among BioCLIP's top candidates."""
    d = a.decision
    top = d.candidates[0] if d.candidates else None
    names = {d.name, top.accepted_name if top else None, top.name if top else None} - {None}
    if names & DOMESTIC_NAMES:
        return True
    families = {c.lineage.get("family") for c in d.candidates[:3]} - {None}
    overlap = lambda h: _iou(a.box, h) >= 0.3 or _contained(a.box, h) >= 0.6 or _contained(h, a.box) >= 0.6
    return any(h.kingdom in families and overlap(h) for h in hints)


def same_subject(a: "Analysis", b: "Analysis") -> bool:
    """Two detections of the same individual: they overlap and point to the same taxon (or genus),
    or they are neighbouring fragments of the same genus, one of which the detector is unsure about
    (snake cut in two, ground beetle cut into pieces). A butterfly on a flower stays two subjects;
    so do two well-detected animals side by side."""
    da, db = a.decision, b.decision
    same_taxon = bool(da.gbif_key and da.gbif_key == db.gbif_key)
    same_genus = bool(_genus(a) and _genus(a) == _genus(b))
    overlap = _iou(a.box, b.box) >= 0.3 or _contained(a.box, b.box) >= 0.6 or _contained(b.box, a.box) >= 0.6
    if overlap and (same_taxon or same_genus or (da.rank == "none" and db.rank == "none")):
        return True
    adjacent = _iou(_grow(a.box, 0.15), _grow(b.box, 0.15)) > 0
    unsure = min(a.box.score, b.box.score) < FRAGMENT_MAX_SCORE
    return adjacent and same_genus and unsure


FRAGMENT_MAX_SCORE = 0.6


def _union(a: Box, b: Box) -> Box:
    x1, y1 = min(a.x, b.x), min(a.y, b.y)
    x2, y2 = max(a.x + a.w, b.x + b.w), max(a.y + a.h, b.y + b.h)
    return Box(x1, y1, x2 - x1, y2 - y1, max(a.score, b.score), a.label, a.kingdom, a.source)


def dedupe(results: list["Analysis"]) -> list["Analysis"]:
    """One detection per subject: keep the most certain one, and its box encloses the merged fragments."""
    kept: list[Analysis] = []
    for r in sorted(results, key=lambda r: -r.decision.confidence):
        for k in kept:
            if same_subject(r, k):
                k.box = _union(k.box, r.box) if not (_contained(r.box, k.box) >= 0.6) else k.box
                break
        else:
            kept.append(r)
    return kept


ALL_GROUPS = frozenset({"animal", "plant", "fungus", "microbe"})
# Kingdom given by BioCLIP (TreeOfLife) -> group; the other kingdoms (plants, algae…) count with plants.
RAW_GROUP = {"Animalia": "animal", "Metazoa": "animal", "Fungi": "fungus"}
# Invisible in photos: model answers on a texture, not real subjects. In these two mixed kingdoms, only
# brown algae and slime moulds get photographed. Same rule in SQL: curation.DETECTION_GROUP.
MICROBE_KINGDOMS = ("Bacteria", "Archaea", "Viruses")
VISIBLE_PHYLUM = {"Chromista": "Ochrophyta", "Protozoa": "Mycetozoa"}


def group_of_lineage(lineage: dict) -> str:
    """Group (animal, plant, fungus, microbe) from the kingdom, and the phylum for mixed kingdoms."""
    kingdom = lineage.get("kingdom")
    if kingdom in MICROBE_KINGDOMS or (kingdom in VISIBLE_PHYLUM and lineage.get("phylum") != VISIBLE_PHYLUM[kingdom]):
        return "microbe"
    return RAW_GROUP.get(kingdom, "plant")


def group_of_result(r: "Analysis") -> str:
    """Group of a detection: that of the top candidate, otherwise the box label."""
    top = r.decision.candidates[0] if r.decision.candidates else None
    kingdom = top.lineage.get("kingdom") if top else None
    if kingdom:
        return group_of_lineage(top.lineage)
    return r.box.label if r.box.label in ALL_GROUPS else "animal"


def fix_false_animals(results: list["Analysis"]) -> list["Analysis"]:
    """Unsure MegaDetector box whose content is identified as a plant or a fungus:
    false positive (flower, fungus taken for an animal). Dropped if there is already a flora box
    in the image, otherwise relabelled."""
    flora = {"Plantae": "plant", "Fungi": "fungus"}
    has_flora = any(r.box.label in ("plant", "fungus") for r in results)
    out = []
    for r in results:
        top = r.decision.candidates[0] if r.decision.candidates else None
        kingdom = top.lineage.get("kingdom") if top else None
        if r.box.source == "md" and r.box.score < MD_SURE and kingdom in flora:
            if has_flora:
                continue
            r.box.label, r.box.kingdom = flora[kingdom], kingdom
        out.append(r)
    return out


def load_original(original: "Callable[[], bytes | None]", preview: Image.Image) -> Image.Image | None:
    """Decoded original, if readable (no RAW, no HEIC without the plugin) and framed like the preview."""
    try:
        data = original()
        full = open_image(data, MAX_SIDE) if data else None
    except Exception as e:
        log.debug("original unavailable: %s", e)
        return None
    if full is None or full.width <= preview.width or not same_frame(full, preview):
        return None
    return full


class Analyzer:
    def __init__(self, detector: Detector | None, classifier: Classifier, taxonomy: Taxonomy | None,
                 max_boxes: int = 5, full_image_fallback: bool = True, open_detector: OpenVocabDetector | None = None,
                 open_mode: str = "always"):
        self.detector, self.classifier, self.taxonomy = detector, classifier, taxonomy
        self.open_detector = open_detector
        # 'always': OWLv2 on every photo; 'fallback': only if MegaDetector finds no animal
        # (OWLv2 costs ~4 times MegaDetector on the CPU).
        self.open_mode = open_mode
        self.max_boxes, self.full_image_fallback = max_boxes, full_image_fallback
        self.drop: Callable[[Candidate], bool] | None = None   # excluded candidates (settings)
        self.tolerant = False   # GBIF down: decide anyway (Identify page), see rerank
        self.timings: dict[str, list] = {}   # stage -> [seconds, photos]

    def analyze(self, data: bytes | Image.Image, lat: float | None = None, lon: float | None = None,
                original: "Callable[[], bytes | None] | None" = None, fallback: bool | None = None,
                groups: frozenset[str] = ALL_GROUPS) -> list[Analysis]:
        """original: returns the original file (or None); called only if a subject is small.
        fallback: whole image classified if nothing is detected (None: analyser setting).
        groups: groups searched (animal, plant, fungus, microbe); the others are neither detected nor kept."""
        return self.analyze_many([(data, lat, lon, original)], fallback, groups)[0]

    def analyze_many(self, items: list[tuple], fallback: bool | None = None,
                     groups: frozenset[str] = ALL_GROUPS) -> list[list[Analysis]]:
        """Several photos (data, lat, lon, original): detection photo by photo, then all the crops
        identified in a single BioCLIP pass (much faster on GPU), then decision photo by photo.
        Same results as photo by photo: BioCLIP processes each image independently."""
        return self.decide_many(self.infer_many(items, fallback, groups), groups)

    def infer_many(self, items: list[tuple], fallback: bool | None = None,
                   groups: frozenset[str] = ALL_GROUPS) -> list[tuple]:
        """"Models" part of analyze_many (detectors, BioCLIP): neither database nor GBIF, it can run in a
        separate thread while the previous batch goes through the geographic check (decide_many)."""
        fallback = self.full_image_fallback if fallback is None else fallback
        prepared = self._detect_many(items, fallback, groups)
        batch: list[Image.Image] = []
        for p in prepared:
            batch += p["crops"] + ([p["image"]] if p["context"] else [])
        t0 = time.perf_counter()
        classified = iter(self.classifier.classify(batch) if batch else [])
        self._timed("bioclip", t0, len(prepared))
        return [(p, [next(classified) for _ in p["crops"]], next(classified) if p["context"] else None) for p in prepared]

    @staticmethod
    def pack(inferred: tuple) -> bytes:
        """Result of infer_many for one photo, for the database: what decide_many needs (boxes, hints, place,
        BioCLIP candidates), without the images. Compressed JSON, a few KB."""
        p, classified, whole = inferred
        cands = lambda cs: [asdict(c) for c in cs]
        data = {"boxes": [asdict(b) for b in p["boxes"]], "hints": [asdict(b) for b in p["hints"]],
                "lat": p["lat"], "lon": p["lon"], "context": p["context"],
                "classified": [cands(cs) for cs in classified], "whole": cands(whole) if whole is not None else None}
        return zlib.compress(json.dumps(data, separators=(",", ":"), default=float).encode())

    @staticmethod
    def unpack(blob: bytes) -> tuple:
        """Inverse of pack: the (p, classified, whole) tuple that decide_many takes."""
        d = json.loads(zlib.decompress(blob))
        cands = lambda cs: [Candidate(**c) for c in cs]
        p = {"boxes": [Box(**b) for b in d["boxes"]], "hints": [Box(**b) for b in d["hints"]],
             "lat": d["lat"], "lon": d["lon"], "context": d["context"]}
        return p, [cands(cs) for cs in d["classified"]], cands(d["whole"]) if d["whole"] is not None else None

    def decide_many(self, inferred: list[tuple], groups: frozenset[str] = ALL_GROUPS) -> list[list[Analysis]]:
        t0 = time.perf_counter()
        out = [self._decide(p, classified, whole, groups) for p, classified, whole in inferred]
        self._timed("geo", t0, len(inferred))
        return out

    def _timed(self, stage: str, t0: float, photos: int = 1) -> None:
        """Time spent per stage (Settings > AI models: where the time goes)."""
        acc = self.timings.setdefault(stage, [0.0, 0])
        acc[0] += time.perf_counter() - t0
        acc[1] += photos

    def _detect_many(self, items: list[tuple], fallback: bool, groups: frozenset[str] = ALL_GROUPS) -> list[dict]:
        """Detection of several photos: each detector in batches on the GPU (detect_many), same boxes as photo by
        photo; then boxes and crops photo by photo."""
        t0 = time.perf_counter()
        images = [open_image(item[0]) for item in items]
        self._timed("decode", t0, len(items))
        many = lambda det: getattr(det, "detect_many", None) or (lambda ims: [det.detect(im) for im in ims])
        # Without animals, MegaDetector (animals, people, vehicles) is useless; OWLv2 keeps its queries
        # for small animals (insects, spiders) if animals are searched.
        t0 = time.perf_counter()
        md = many(self.detector)(images) if self.detector and "animal" in groups else [[] for _ in images]
        self._timed("megadetector", t0, len(items))
        ov: list[list[Box]] = [[] for _ in images]
        need = [i for i, m in enumerate(md) if self.open_detector and (self.open_mode == "always" or not m)]
        t0 = time.perf_counter()
        for i, boxes in zip(need, many(self.open_detector)([images[i] for i in need]) if need else []):
            ov[i] = boxes
        self._timed("owlv2", t0, len(items))
        return [self._detect(images[i], md[i], ov[i], *items[i][1:], fallback, groups) for i in range(len(items))]

    def _detect(self, image, md, ov, lat, lon, original, fallback: bool, groups: frozenset[str] = ALL_GROUPS) -> dict:
        hints = [b for b in ov if b.label == "domestic"]          # "a dog", "a cat"…: hints only
        boxes = merge_boxes(md, [b for b in ov if b.label in groups], self.max_boxes)
        if not boxes and fallback:
            # Nothing detected (blurred subject, very large, or scene): whole image.
            boxes = [Box(0.0, 0.0, 1.0, 1.0, 0.0, "image", None, "image")]
        t0 = time.perf_counter()
        crops = [image if b.area >= 0.999 else crop(image, b) for b in boxes]
        if original and any(b.area < 0.999 and crop_side(image, b) < SMALL_CROP_PX for b in boxes):
            full = load_original(original, image)
            if full is not None:
                crops = [c if b.area >= 0.999 or crop_side(image, b) >= SMALL_CROP_PX else crop(full, b)
                         for b, c in zip(boxes, crops)]
        self._timed("crops", t0)
        # Single subject: average of crop + whole image (context helps, see benchmark).
        context = len(boxes) == 1 and boxes[0].area < 0.999
        return {"image": image, "boxes": boxes, "crops": crops, "hints": hints, "lat": lat, "lon": lon, "context": context}

    def _decide(self, p: dict, classified: list[list[Candidate]], whole: list[Candidate] | None,
                groups: frozenset[str] = ALL_GROUPS) -> list[Analysis]:
        crop_only = classified[0] if whole is not None else None
        if whole is not None:
            classified = [average(classified[0], whole)]
        results = []
        for b, cands in zip(p["boxes"], classified):
            # Group not searched according to BioCLIP: dropped before the GBIF requests of the geographic check.
            top = max(cands, key=lambda c: c.score, default=None)
            if groups != ALL_GROUPS and top and top.lineage.get("kingdom") and group_of_lineage(top.lineage) not in groups:
                continue
            excluded: list[Candidate] = []
            ranked = rerank(cands, self.taxonomy, p["lat"], p["lon"], b.kingdom, self.drop, excluded, tolerant=self.tolerant)
            d = decide(ranked, self.taxonomy, tolerant=self.tolerant)
            d.excluded = excluded
            results.append(Analysis(b, d, frame_support=frame_support(crop_only, d) if crop_only else None))
        results = [r for r in dedupe(fix_false_animals(results)) if group_of_result(r) in groups]
        for r in results:
            r.domestic = is_domestic(r, p["hints"])
        return results
