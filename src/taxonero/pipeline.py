"""Analyse d'une image : détection (MegaDetector v6 pour la faune, OWLv2 à vocabulaire ouvert
pour les plantes, champignons et petits animaux), classification (BioCLIP 2), repondération
géographique (GBIF), décision hiérarchique et nettoyage des détections superposées.
"""

import io
import time
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import httpx
from PIL import Image, ImageOps

from .taxonomy import RANKS, Taxonomy

log = logging.getLogger(__name__)



@dataclass
class Box:
    x: float  # normalisé 0..1, coin haut gauche
    y: float
    w: float
    h: float
    score: float
    label: str                 # animal | plant | fungus | image (pas de boîte : image entière)
    kingdom: str | None = None  # indice de règne donné par le détecteur (Animalia, Plantae, Fungi)
    source: str = "md"          # md (MegaDetector) | owl (OWLv2) | image | manual

    @property
    def area(self) -> float:
        return self.w * self.h


@dataclass
class Candidate:
    name: str                      # nom scientifique (binôme) donné par le modèle
    score: float                   # probabilité modèle
    lineage: dict[str, str]
    gbif_key: int | None = None
    accepted_name: str | None = None
    plausibility: str = "unknown"
    geo_weight: float = 1.0        # a priori p(espèce | lieu), non normalisé
    posterior: float = 0.0         # après repondération géographique (et filtrage des candidats écartés)
    prior: float = 0.0             # même probabilité, avant filtrage (sert à rétablir un candidat écarté)


@dataclass
class Decision:
    gbif_key: int | None
    name: str | None
    rank: str                      # species, genus, family... ou 'none'
    confidence: float              # probabilité cumulée au rang retenu
    margin: float
    plausibility: str
    candidates: list[Candidate] = field(default_factory=list)
    excluded: list[Candidate] = field(default_factory=list)   # écartés par les paramètres


def average(a: list[Candidate], b: list[Candidate]) -> list[Candidate]:
    """Moyenne des scores de deux listes de candidats (absent d'une liste = score 0)."""
    out: dict[str, Candidate] = {}
    for cands in (a, b):
        for c in cands:
            if c.name not in out:
                out[c.name] = Candidate(name=c.name, score=0.0, lineage=dict(c.lineage))
            out[c.name].score += c.score / 2
    return sorted(out.values(), key=lambda c: -c.score)


def image_quality(image: Image.Image) -> float:
    """Qualité visuelle 0..1 : moitié couleurs (indice de Hasler et Süsstrunk, nul en noir et blanc
    comme les pièges photo infrarouges), moitié netteté (variance du laplacien)."""
    rgb = np.asarray(image.convert("RGB").resize((256, 256)), dtype=np.float32)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    rg, yb = r - g, 0.5 * (r + g) - b
    colorful = float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))
    gray = rgb.mean(axis=2)
    lap = gray[1:-1, 1:-1] * 4 - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
    return 0.5 * min(1.0, colorful / 60.0) + 0.5 * min(1.0, float(lap.var()) / 400.0)


class Detector:
    """MegaDetector v6 (ONNX). Classes du modèle : 0 animal, 1 personne, 2 véhicule ; seule la 0 est gardée."""

    def __init__(self, onnx_path: str | Path, providers: list[str] | None = None, min_score: float = 0.5,
                 min_area: float = 0.005):
        import onnxruntime as ort
        names = [p[0] if isinstance(p, tuple) else p for p in providers or []]
        if "CUDAExecutionProvider" in names and hasattr(ort, "preload_dlls"):
            # Bibliothèques CUDA et cuDNN installées par PyTorch (paquets nvidia-*) : sans ce préchargement,
            # ONNX Runtime ne les trouve pas et repasse en silence sur le processeur.
            try:
                ort.preload_dlls()
            except Exception as e:
                log.warning("préchargement CUDA pour ONNX Runtime impossible : %s", e)
        self.session = ort.InferenceSession(str(onnx_path), providers=providers or ort.get_available_providers())
        self.min_score = min_score
        self.min_area = min_area  # fraction de l'image : en dessous, presque toujours du bruit

    def detect(self, image: Image.Image) -> list[Box]:
        w, h = image.size
        x = np.asarray(image.convert("RGB").resize((640, 640), Image.BILINEAR), dtype=np.float32) / 255.0
        x = x.transpose(2, 0, 1)[None]
        labels, boxes, scores = self.session.run(None, {"images": x, "orig_target_sizes": np.array([[w, h]], dtype=np.int64)})
        out = []
        for lab, (x1, y1, x2, y2), s in zip(labels[0], boxes[0], scores[0]):
            if s < self.min_score or int(lab) != 0:
                continue
            # float() : les numpy.float32 seraient stockés en BLOB par SQLite.
            x1, y1, x2, y2 = max(0.0, float(x1)), max(0.0, float(y1)), min(float(w), float(x2)), min(float(h), float(y2))
            if x2 - x1 < 8 or y2 - y1 < 8 or (x2 - x1) * (y2 - y1) < self.min_area * w * h:
                continue
            # Pas d'indice de règne : MegaDetector pose parfois une boîte « animal » sur une plante,
            # et l'indice pénaliserait alors la bonne réponse (mesuré sur le jeu de dev).
            out.append(Box(x1 / w, y1 / h, (x2 - x1) / w, (y2 - y1) / h, float(s), "animal", None, "md"))
        return _nms(sorted(out, key=lambda b: -b.score))


class OpenVocabDetector:
    """OWLv2 (Apache 2.0) interrogé en texte libre : plantes, champignons, lichens, et les petits
    animaux (insectes, araignées) que MegaDetector, entraîné sur des pièges photo, détecte mal."""

    QUERIES = {  # requête -> (libellé, règne)
        "a flower": ("plant", "Plantae"), "a plant": ("plant", "Plantae"), "a leaf": ("plant", "Plantae"),
        "a tree": ("plant", "Plantae"), "a moss": ("plant", "Plantae"), "a fern": ("plant", "Plantae"),
        "a mushroom": ("fungus", "Fungi"), "a lichen": ("fungus", "Fungi"),
        "an insect": ("animal", "Animalia"), "a butterfly": ("animal", "Animalia"), "a spider": ("animal", "Animalia"),
        "a snail": ("animal", "Animalia"), "a bird": ("animal", "Animalia"), "an animal": ("animal", "Animalia"),
        # Domestique contre sauvage : OWLv2 garde, pour chaque boîte, la requête au meilleur score. Sans les
        # concurrents sauvages, « a dog » se déclenchait sur les loups et les renards (mesuré sur le jeu de dev).
        # Les boîtes « domestic » ne sont que des indices ; les boîtes « wild » restent des sujets (animal).
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
        # Les requêtes ne changent jamais : leurs empreintes de texte sont calculées une fois ici, au lieu de
        # repasser à chaque photo dans la tour de texte (comme le fait model(**inputs)). Mêmes boîtes.
        tok = self.processor(text=[self.queries], return_tensors="pt").to(device)
        with torch.no_grad():
            q = self.model.owlv2.get_text_features(input_ids=tok["input_ids"], attention_mask=tok["attention_mask"]).pooler_output
        self._query = (q / torch.linalg.norm(q, ord=2, dim=-1, keepdim=True))[None]
        self._query_mask = (tok["input_ids"][:, 0] > 0)[None]

    # OWLv2 complète l'image en carré puis la ramène à 960 px : faite par sa préparation (processeur) sur un aperçu de
    # 1440 px, l'opération coûte ~230 ms par photo sur 4 cœurs, pendant que le GPU attend. Réduite d'abord à 960 px
    # avec Pillow : ~26 ms, mêmes boîtes sur 19 photos de dev sur 20 (une boîte à 0,31, au ras du seuil, perdue).
    INPUT_SIDE = 960

    def detect(self, image: Image.Image) -> list[Box]:
        k = self.INPUT_SIDE / max(image.size)
        if k < 1:
            image = image.resize((round(image.width * k), round(image.height * k)), Image.Resampling.BILINEAR, reducing_gap=2.0)
        w, h = image.size
        from transformers.models.owlv2.modeling_owlv2 import Owlv2ObjectDetectionOutput
        pixels = self.processor(images=image, return_tensors="pt")["pixel_values"].to(self.device)
        with self._torch.no_grad():
            feature_map = self.model.image_embedder(pixel_values=pixels)[0]
            b, ph, pw, dim = feature_map.shape
            feats = feature_map.reshape(b, ph * pw, dim)
            logits = self.model.class_predictor(feats, self._query, self._query_mask)[0]
            outputs = Owlv2ObjectDetectionOutput(logits=logits, pred_boxes=self.model.box_predictor(feats, feature_map))
        side = max(w, h)  # OWLv2 travaille sur l'image complétée en carré
        res = self.processor.post_process_grounded_object_detection(outputs, threshold=self.min_score,
                                                                     target_sizes=[(side, side)])[0]
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


def merge_boxes(primary: list[Box], secondary: list[Box], max_boxes: int = 5) -> list[Box]:
    """Fusionne deux détecteurs : MegaDetector prioritaire, OWLv2 ajoute ce qui ne recouvre rien.
    Une boîte qui couvre presque toute l'image devient « image entière » (le sujet, c'est la scène)."""
    def overlaps(a: Box, b: Box) -> bool:
        return _iou(a, b) >= 0.3 or _contained(a, b) >= 0.6 or _contained(b, a) >= 0.6

    # MegaDetector peu sûr de lui sur ce qu'OWLv2 voit comme une plante ou un champignon : faux
    # positif typique (fleur prise pour un animal). Les vrais animaux sortent à >= 0,9.
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
    """Part de la boîte a contenue dans b."""
    ix = max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    return ix * iy / (a.w * a.h + 1e-9)


def _nms(boxes: list[Box], thr: float = 0.5, contain_thr: float = 0.7) -> list[Box]:
    """Suppression des doublons : recouvrement (IoU) ou boîte contenue dans une boîte mieux notée
    (MegaDetector détecte parfois l'animal entier et des parties de lui-même)."""
    kept: list[Box] = []
    for b in boxes:
        if all(_iou(b, k) < thr and _contained(b, k) < contain_thr and _contained(k, b) < contain_thr for k in kept):
            kept.append(b)
    return kept


# Recadrage plus petit que ça dans l'aperçu (1440 px sur le petit côté par défaut) : on le refait dans l'original s'il est disponible.
# BioCLIP travaille en 224 px ; avec la marge, en dessous de ~400 px l'aperçu perd déjà des détails.
SMALL_CROP_PX = 400


def crop_side(image: Image.Image, box: Box, margin: float = 0.15) -> float:
    w, h = image.size
    return max(box.w * w, box.h * h) * (1 + 2 * margin)


# Côté maximal d'une image décodée en entier (original, image déposée sur la page Identifier) : bien au-delà de
# ce qu'exigent les petits sujets (SMALL_CROP_PX), et ~50 Mo en mémoire au lieu de ~150 Mo pour 50 Mpx.
MAX_SIDE = 4096


def open_image(data: bytes | Image.Image, max_side: int | None = None) -> Image.Image:
    image = data if isinstance(data, Image.Image) else Image.open(io.BytesIO(data))
    if max_side and max(image.size) > max_side:
        k = max_side / max(image.size)
        image.draft("RGB", (int(image.width * k), int(image.height * k)))   # JPEG : décodage directement réduit
    image = ImageOps.exif_transpose(image).convert("RGB")
    if max_side and max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return image


def exif_latlon(data: bytes) -> tuple[float | None, float | None]:
    """Coordonnées GPS inscrites dans l'EXIF d'une image (None, None si absentes ou illisibles)."""
    try:
        gps = Image.open(io.BytesIO(data)).getexif().get_ifd(0x8825)
        def deg(v, ref):
            d, m, sec = (float(x) for x in v)
            r = d + m / 60 + sec / 3600
            return -r if ref in ("S", "W") else r
        lat, lon = deg(gps[2], gps[1]), deg(gps[4], gps[3])
        # Récepteur sans position : certains appareils écrivent 0/0 (NaN une fois divisé) ou des valeurs hors limites.
        return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else (None, None)
    except Exception:
        return None, None


def same_frame(a: Image.Image, b: Image.Image, tol: float = 0.01) -> bool:
    """Même cadrage (rapport largeur/hauteur) : sinon l'original a été recadré ou tourné depuis l'aperçu."""
    return abs(a.width / a.height - b.width / b.height) <= tol * a.width / a.height


def crop(image: Image.Image, box: Box, margin: float = 0.15) -> Image.Image:
    """Recadre avec une marge (le contexte aide la classification), en carré si possible."""
    w, h = image.size
    cx, cy = (box.x + box.w / 2) * w, (box.y + box.h / 2) * h
    side = max(box.w * w, box.h * h) * (1 + 2 * margin)
    x1, y1 = max(0, cx - side / 2), max(0, cy - side / 2)
    x2, y2 = min(w, cx + side / 2), min(h, cy + side / 2)
    return image.crop((int(x1), int(y1), int(x2), int(y2)))


def _tol_classifier(device: str):
    """BioCLIP 2 allégé, résultats inchangés :
    - les empreintes de texte des ~870 000 espèces (2,7 Go en float32) sont lues en mmap : sur processeur,
      ces pages sont du cache disque que le noyau peut reprendre sous pression mémoire, au lieu de mémoire
      privée qui finit en arrêt du processus (OOM) ;
    - la tour de texte du modèle (~0,5 Go), inutile une fois ces empreintes calculées, est libérée."""
    import warnings
    import torch
    from bioclip import TreeOfLifeClassifier

    class Light(TreeOfLifeClassifier):
        def get_txt_emb(self):
            path = self.get_cached_datafile("embeddings/txt_emb_species.npy")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)   # tableau en lecture seule : jamais modifié
                return torch.from_numpy(np.load(path, mmap_mode="r"))

    clf = Light(device=device)
    m = getattr(clf.model, "_orig_mod", clf.model)   # pybioclip enveloppe le modèle dans torch.compile
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
        # Images par passage : un GPU en traite beaucoup à la fois, le processeur n'y gagne rien.
        self.batch_size = 8 if device == "cpu" else 32

    def classify(self, images: list[Image.Image]) -> list[list[Candidate]]:
        preds = self.model.predict(images, self._rank, k=self.k, batch_size=self.batch_size)
        out: list[list[Candidate]] = [[] for _ in images]
        for p in preds:
            idx = int(p["file_name"])  # pybioclip indexe les images PIL par position
            lineage = {r: p[r] for r in RANKS[:-1] if p.get(r)}
            name = f"{p['genus']} {p['species_epithet']}"
            lineage["species"] = name
            out[idx].append(Candidate(name=name, score=float(p["score"]), lineage=lineage))
        for c in out:
            c.sort(key=lambda x: -x.score)
        return out


# Poids d'un candidat dont le règne ne correspond pas à l'indice du détecteur (indice souple :
# OWLv2 confond parfois algue et lichen, galle et araignée).
KINGDOM_MISMATCH = 0.4


MIN_CANDIDATE_SCORE = 0.01
MIN_CANDIDATES = 3


def rerank(cands: list[Candidate], tx: Taxonomy | None, lat: float | None, lon: float | None,
           kingdom_hint: str | None = None, drop: "Callable[[Candidate], bool] | None" = None,
           excluded: list[Candidate] | None = None, tolerant: bool = False) -> list[Candidate]:
    """Normalise les noms via GBIF (synonymes) et repondère par la plausibilité géographique
    et, s'il y en a un, par l'indice de règne du détecteur. drop : candidats écartés (paramètres),
    ajoutés à excluded si fourni. tolerant : GBIF en panne, les candidats restent proposés (plausibilité « unverified »,
    poids neutre) au lieu de faire échouer l'action ; réservé aux actions manuelles, l'analyse automatique réessaie."""
    # Candidats sous 1 % : jamais affichés, et sur le banc d'essai les ignorer ne change ni le taux d'espèces
    # justes ni celui des erreurs (54 photos : 8,6 candidats vérifiés au lieu de 20, deux décisions différentes,
    # une de plus juste). Chacun coûtait jusqu'à six requêtes GBIF.
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
        if key in merged:  # deux noms du modèle -> même espèce acceptée : on cumule
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
        try:
            return bool(drop and drop(c))
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
    """Probabilités (prior) renormalisées sur les candidats gardés, triées."""
    z = sum(c.prior for c in cands) or 1.0
    for c in cands:
        c.posterior = c.prior / z
    return sorted(cands, key=lambda c: -c.posterior)


def decide(cands: list[Candidate], tx: Taxonomy | None, species_threshold: float = 0.5,
           higher_threshold: float = 0.7, tolerant: bool = False) -> Decision:
    """Retient le rang le plus précis dont la probabilité cumulée dépasse le seuil."""
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
            # Rang attendu : sans lui, l'API répond pour un genre ou un ordre ambigu par le règne entier (clé 1, Animalia).
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
    # Sujet seul (recadrage et photo entière moyennés) : probabilité du taxon retenu sur le recadrage seul. Très
    # basse, l'espèce vient de la photo entière : le cadre ne contient peut-être pas le sujet.
    frame_support: float | None = None


def frame_support(crop: list[Candidate], d: Decision) -> float | None:
    """Probabilité, sur le recadrage seul (BioCLIP), du taxon retenu (espèce, ou genre, famille… au rang décidé)."""
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


# Espèces (ou formes) uniquement domestiques : écartées d'office.
DOMESTIC_NAMES = {"Felis catus", "Canis familiaris", "Canis lupus familiaris", "Equus caballus", "Equus asinus",
                  "Bos taurus", "Ovis aries", "Capra hircus", "Sus domesticus", "Gallus gallus domesticus"}


# Espèces sauvages qu'on confond avec un animal domestique (chien/loup, chat/chat forestier, porc/sanglier,
# pigeon de ville, canard domestique) : jamais auto-validées, toujours relues.
AMBIGUOUS_WITH_DOMESTIC = {"Canis lupus", "Felis silvestris", "Sus scrofa", "Columba livia", "Anas platyrhynchos",
                           "Gallus gallus", "Equus ferus", "Bos primigenius", "Ovis gmelini", "Capra aegagrus"}


def is_domestic(a: "Analysis", hints: list[Box]) -> bool:
    """Animal domestique : espèce uniquement domestique, ou famille ambiguë quand OWLv2 voit sur le même sujet
    « a dog », « a cat »… plutôt que leur équivalent sauvage. La famille vient de l'indice (Canidae…)
    et doit se retrouver parmi les premiers candidats de BioCLIP."""
    d = a.decision
    top = d.candidates[0] if d.candidates else None
    names = {d.name, top.accepted_name if top else None, top.name if top else None} - {None}
    if names & DOMESTIC_NAMES:
        return True
    families = {c.lineage.get("family") for c in d.candidates[:3]} - {None}
    overlap = lambda h: _iou(a.box, h) >= 0.3 or _contained(a.box, h) >= 0.6 or _contained(h, a.box) >= 0.6
    return any(h.kingdom in families and overlap(h) for h in hints)


def same_subject(a: "Analysis", b: "Analysis") -> bool:
    """Deux détections du même individu : elles se recouvrent et désignent le même taxon (ou genre),
    ou bien ce sont des fragments voisins du même genre dont l'un est peu sûr côté détecteur
    (serpent coupé en deux, carabe découpé en morceaux). Un papillon sur une fleur reste deux sujets ;
    deux animaux bien détectés côte à côte aussi."""
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
    """Une détection par sujet : on garde la plus sûre, et sa boîte englobe les fragments fusionnés."""
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
# Règne donné par BioCLIP (TreeOfLife) -> groupe ; les autres règnes (plantes, algues…) comptent avec les plantes.
RAW_GROUP = {"Animalia": "animal", "Metazoa": "animal", "Fungi": "fungus"}
# Invisibles en photo : réponses du modèle sur une texture, pas de vrais sujets. Dans ces deux règnes mêlés, seuls
# les algues brunes et les myxomycètes se photographient. Même règle en SQL : curation.DETECTION_GROUP.
MICROBE_KINGDOMS = ("Bacteria", "Archaea", "Viruses")
VISIBLE_PHYLUM = {"Chromista": "Ochrophyta", "Protozoa": "Mycetozoa"}


def group_of_lineage(lineage: dict) -> str:
    """Groupe (animal, plant, fungus, microbe) d'après le règne, et l'embranchement pour les règnes mêlés."""
    kingdom = lineage.get("kingdom")
    if kingdom in MICROBE_KINGDOMS or (kingdom in VISIBLE_PHYLUM and lineage.get("phylum") != VISIBLE_PHYLUM[kingdom]):
        return "microbe"
    return RAW_GROUP.get(kingdom, "plant")


def group_of_result(r: "Analysis") -> str:
    """Groupe d'une détection : celui du premier candidat, sinon libellé de la boîte."""
    top = r.decision.candidates[0] if r.decision.candidates else None
    kingdom = top.lineage.get("kingdom") if top else None
    if kingdom:
        return group_of_lineage(top.lineage)
    return r.box.label if r.box.label in ALL_GROUPS else "animal"


def fix_false_animals(results: list["Analysis"]) -> list["Analysis"]:
    """Boîte MegaDetector peu sûre dont le contenu est identifié comme plante ou champignon :
    faux positif (fleur, champignon pris pour un animal). Écartée s'il existe déjà une boîte de
    flore dans l'image, sinon reclassée."""
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
    """Original décodé, s'il est lisible (pas de RAW ni de HEIC sans greffon) et cadré comme l'aperçu."""
    try:
        data = original()
        full = open_image(data, MAX_SIDE) if data else None
    except Exception as e:
        log.debug("original indisponible : %s", e)
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
        # 'always' : OWLv2 sur chaque photo ; 'fallback' : seulement si MegaDetector ne trouve aucun animal
        # (OWLv2 coûte ~4 fois MegaDetector sur processeur).
        self.open_mode = open_mode
        self.max_boxes, self.full_image_fallback = max_boxes, full_image_fallback
        self.drop: Callable[[Candidate], bool] | None = None   # candidats écartés (paramètres)
        self.tolerant = False   # GBIF en panne : décision quand même (page Identifier), voir rerank
        self.timings: dict[str, list] = {}   # étape -> [secondes, photos]

    def analyze(self, data: bytes | Image.Image, lat: float | None = None, lon: float | None = None,
                original: "Callable[[], bytes | None] | None" = None, fallback: bool | None = None,
                groups: frozenset[str] = ALL_GROUPS) -> list[Analysis]:
        """original : renvoie le fichier original (ou None) ; appelé seulement si un sujet est petit.
        fallback : image entière classée si rien n'est détecté (None : réglage de l'analyseur).
        groups : groupes cherchés (animal, plant, fungus, microbe) ; les autres ne sont ni détectés ni gardés."""
        return self.analyze_many([(data, lat, lon, original)], fallback, groups)[0]

    def analyze_many(self, items: list[tuple], fallback: bool | None = None,
                     groups: frozenset[str] = ALL_GROUPS) -> list[list[Analysis]]:
        """Plusieurs photos (data, lat, lon, original) : détection photo par photo, puis tous les recadrages
        identifiés en un seul passage de BioCLIP (bien plus rapide sur GPU), puis décision photo par photo.
        Mêmes résultats que photo par photo : BioCLIP traite chaque image indépendamment."""
        return self.decide_many(self.infer_many(items, fallback, groups), groups)

    def infer_many(self, items: list[tuple], fallback: bool | None = None,
                   groups: frozenset[str] = ALL_GROUPS) -> list[tuple]:
        """Partie « modèles » de analyze_many (détecteurs, BioCLIP) : ni base ni GBIF, elle peut tourner dans un
        fil à part pendant que le précédent lot passe la vérification géographique (decide_many)."""
        fallback = self.full_image_fallback if fallback is None else fallback
        prepared = [self._detect(*item, fallback, groups) for item in items]
        batch: list[Image.Image] = []
        for p in prepared:
            batch += p["crops"] + ([p["image"]] if p["context"] else [])
        t0 = time.perf_counter()
        classified = iter(self.classifier.classify(batch) if batch else [])
        self._timed("bioclip", t0, len(prepared))
        return [(p, [next(classified) for _ in p["crops"]], next(classified) if p["context"] else None) for p in prepared]

    def decide_many(self, inferred: list[tuple], groups: frozenset[str] = ALL_GROUPS) -> list[list[Analysis]]:
        t0 = time.perf_counter()
        out = [self._decide(p, classified, whole, groups) for p, classified, whole in inferred]
        self._timed("geo", t0, len(inferred))
        return out

    def _timed(self, stage: str, t0: float, photos: int = 1) -> None:
        """Temps passé par étape (Paramètres > Modèles d'IA : où passe le temps)."""
        acc = self.timings.setdefault(stage, [0.0, 0])
        acc[0] += time.perf_counter() - t0
        acc[1] += photos

    def _detect(self, data, lat, lon, original, fallback: bool, groups: frozenset[str] = ALL_GROUPS) -> dict:
        t0 = time.perf_counter()
        image = open_image(data)
        self._timed("decode", t0)
        # Sans les animaux, MegaDetector (animaux, personnes, véhicules) ne sert à rien ; OWLv2 garde ses requêtes
        # de petits animaux (insectes, araignées) si les animaux sont cherchés.
        t0 = time.perf_counter()
        md = self.detector.detect(image) if self.detector and "animal" in groups else []
        self._timed("megadetector", t0)
        run_ov = self.open_detector and (self.open_mode == "always" or not md)
        t0 = time.perf_counter()
        ov = self.open_detector.detect(image) if run_ov else []
        self._timed("owlv2", t0)
        hints = [b for b in ov if b.label == "domestic"]          # « a dog », « a cat »… : indices seulement
        boxes = merge_boxes(md, [b for b in ov if b.label in groups], self.max_boxes)
        if not boxes and fallback:
            # Rien de détecté (sujet flou, très gros, ou scène) : image entière.
            boxes = [Box(0.0, 0.0, 1.0, 1.0, 0.0, "image", None, "image")]
        t0 = time.perf_counter()
        crops = [image if b.area >= 0.999 else crop(image, b) for b in boxes]
        if original and any(b.area < 0.999 and crop_side(image, b) < SMALL_CROP_PX for b in boxes):
            full = load_original(original, image)
            if full is not None:
                crops = [c if b.area >= 0.999 or crop_side(image, b) >= SMALL_CROP_PX else crop(full, b)
                         for b, c in zip(boxes, crops)]
        self._timed("crops", t0)
        # Un seul sujet : moyenne recadrage + image entière (le contexte aide, cf. benchmark).
        context = len(boxes) == 1 and boxes[0].area < 0.999
        return {"image": image, "boxes": boxes, "crops": crops, "hints": hints, "lat": lat, "lon": lon, "context": context}

    def _decide(self, p: dict, classified: list[list[Candidate]], whole: list[Candidate] | None,
                groups: frozenset[str] = ALL_GROUPS) -> list[Analysis]:
        crop_only = classified[0] if whole is not None else None
        if whole is not None:
            classified = [average(classified[0], whole)]
        results = []
        for b, cands in zip(p["boxes"], classified):
            # Groupe non cherché d'après BioCLIP : écarté avant les requêtes GBIF de la vérification géographique.
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
