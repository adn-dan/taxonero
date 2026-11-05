"""Configuration par variables d'environnement (voir .env.example)."""

import json
import re
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Vides : à renseigner dans le dashboard (Paramètres > Immich), qui les enregistre dans la base. Définies
    # dans l'environnement, elles priment et ne sont pas modifiables depuis le dashboard.
    immich_url: str = ""
    immich_api_key: str = ""
    # Source des photos : immich (défaut) ou local (un dossier, monté dans le conteneur sur PHOTOS_DIR ; résultats
    # écrits dans des fichiers XMP à côté des photos, voir sources/files.py). Sur le bureau, aussi les logiciels
    # photo : darktable et lightroom (leur bibliothèque, CATALOG_PATH), rawtherapee et rapidraw (un dossier).
    source: str = "immich"
    photos_dir: Path = Path("/photos")
    catalog_path: Path | None = None

    # Sécurité : rien n'est écrit dans Immich tant que DRY_RUN n'est pas explicitement à false.
    dry_run: bool = True

    data_dir: Path = Path("data")
    # auto (défaut) : GPU s'il y en a un d'utilisable (NVIDIA ou AMD via 'cuda', Intel 'xpu', Apple 'mps'),
    # sinon processeur. Un GPU demandé mais absent : repli sur le processeur (voir hardware.py).
    device: str = "auto"
    # MegaDetector en ONNX, s'il existe (images Docker d'avant) ; sinon version PyTorch, poids officiels téléchargés
    # au premier usage dans DATA_DIR/models (megadetector.py).
    detector_path: Path = Path("models/MDV6-apa-rtdetr-e.onnx")
    detector_min_score: float = 0.5
    # Détecteur à vocabulaire ouvert (plantes, champignons, insectes) : OWLv2, Apache 2.0.
    open_detector: bool = True
    open_detector_model: str = "google/owlv2-base-patch16-ensemble"
    open_detector_min_score: float = 0.3
    # auto (défaut) : always sur GPU, fallback sur processeur ; always : sur chaque photo ; fallback : seulement
    # quand MegaDetector ne trouve aucun animal. OPEN_DETECTOR=false désactive OWLv2 dans tous les cas.
    open_detector_mode: str = "auto"
    # GPU : photos analysées ensemble (recadrages identifiés en un seul passage). Plus grand : plus rapide, plus
    # de mémoire du GPU. Sans effet sur processeur (une photo à la fois, grouper n'y fait rien gagner).
    gpu_batch: int = 8
    # GPU : modèles retirés de sa mémoire après ce temps sans analyse, rechargés à la suivante. 0 : toujours gardés.
    models_idle_minutes: int = 5
    # Animaux domestiques (chiens, chats, chevaux, bétail, poules) : 'skip' = ni détection ni tag Nature.
    domestic_animals: str = "skip"
    thumbnail_size: str = "preview"   # preview (1440 px sur le petit côté) | fullsize (seulement si Immich génère les aperçus pleine taille, sinon redirection vers l'original refusée)
    # Candidats écartés : espèces disparues (connues surtout par des fossiles) et, pour les photos
    # géolocalisées, espèces jamais observées dans la région (badge rouge).
    exclude_extinct: bool = True
    hide_absent: bool = False
    # Vidéos : l'image clé générée par Immich est analysée comme une photo (pièges photo, vidéos animalières).
    analyze_videos: bool = True
    # Photos où Immich a reconnu au moins une personne : pas analysées (portraits, photos de famille).
    skip_people: bool = True
    # RAW accompagné d'un JPG (ou HEIC) du même cliché : pas analysé, il reçoit les tags du JPG.
    skip_raw_twins: bool = True
    # Rien de détecté sur la photo : classer l'image entière quand même. Rattrape quelques sujets qu'aucun
    # détecteur ne cadre (algues, lichens), mais produit surtout des faux positifs sur les photos sans animal
    # (paysages, intérieurs) et beaucoup de requêtes GBIF. La page Identifier le fait toujours.
    full_image_fallback: bool = False
    # auto : les détecteurs cherchent les sujets sur chaque photo ; manual : aucune détection automatique, les
    # rectangles sont tracés à la main dans la visionneuse (chacun identifié à la volée).
    detection: str = "auto"
    # Groupes recherchés par la détection automatique (animal, plant, fungus ; lichens et algues comptent avec les
    # champignons et les plantes) : les autres ne sont ni cherchés ni gardés.
    detect_groups: str = "animal,plant,fungus"
    # Taxonomie GBIF en local (~1 Go dans DATA_DIR, téléchargée et construite une fois au démarrage) : noms, synonymes,
    # noms communs et UICN sans requête réseau. Seules la présence autour d'un point et les cartes restent en ligne.
    gbif_local: bool = True

    tag_root: str = "Nature"
    names_lang: str = "fr"  # langue principale des noms (tags, description) ; pas LANG : variable système
    # Langues des noms communs affichés dans le dashboard (codes Wikipédia).
    names_langs: str = "fr,en,de,es,it,nl,pt,ca,br,eu,oc,pl,sv,ja,zh"

    @property
    def langs(self) -> tuple[str, ...]:
        rest = [x.strip() for x in self.names_langs.split(",") if x.strip() and x.strip() != self.names_lang]
        return (self.names_lang, *rest)
    write_description: bool = True
    user_agent: str = "taxonero/0.1 (https://github.com/adn-dan/taxonero)"

    # Auto-validation : 'rule' (défaut), 'calibrated', 'fixed' ou 'off'.
    #   rule       : confiance >= AUTO_CONFIRM_THRESHOLD, espèce observée à proximité (badge vert) et
    #                aucun autre candidat crédible (second <= AUTO_CONFIRM_MAX_RUNNER_UP). Dès que
    #                tes relectures suffisent, le seuil calibré remplace le seuil fixe s'il est plus haut.
    #   calibrated : seulement après calibration sur tes relectures.
    auto_confirm: str = "rule"
    auto_confirm_threshold: float = 0.85
    auto_confirm_max_runner_up: float = 0.05
    auto_confirm_target_precision: float = 0.98
    auto_confirm_min_reviews: int = 100
    # Part des détections qui auraient été auto-validées gardées à relire, tirées au hasard : sans elles,
    # la calibration ne voit jamais la zone de forte confiance (elle n'est faite que de tes relectures).
    auto_confirm_audit: float = 0.05

    # Boucle du service : intervalle et fenêtre horaire d'analyse (ex. "01:00-07:00", vide = toujours).
    interval_minutes: int = 15
    analysis_window: str = ""
    batch_size: int = 200
    # Sauvegarde quotidienne de la base (relectures, détections, taxons ; sans les caches réseau) dans
    # DATA_DIR/backups : nombre de jours gardés, 0 pour désactiver.
    backup_keep: int = 7
    # Durée maximale d'analyse par cycle (le reste attend le cycle suivant, lancé aussitôt) : les tags
    # des photos déjà validées sont écrits entre-temps.
    analysis_max_minutes: int = 30

    # Édition : server (Docker, NAS : mot de passe obligatoire) ou desktop (taxonero desktop : ordinateur
    # personnel, accès local seulement, sans identifiant ni mot de passe ; voir desktop.py). Posée par la commande.
    edition: str = "server"
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8765
    dashboard_password: str = Field(default="", description="Mot de passe HTTP Basic (utilisateur 'nature').")
    dashboard_no_auth: bool = False
    # URL publique d'Immich pour les liens « ouvrir dans Immich » (par défaut immich_url).
    immich_public_url: str = ""

    @field_validator("device")
    @classmethod
    def _check_device(cls, v: str) -> str:
        from .hardware import DEVICES
        v = v.strip().lower()
        if v not in DEVICES:
            raise ValueError(f"DEVICE : {' | '.join(DEVICES)}")
        return v

    @field_validator("source")
    @classmethod
    def _check_source(cls, v: str) -> str:
        from .sources import SOURCES
        v = v.strip().lower()
        if v not in SOURCES:
            raise ValueError(f"SOURCE : {' | '.join(SOURCES)}")
        return v

    @field_validator("analysis_window")
    @classmethod
    def _check_window(cls, v: str) -> str:
        v = v.strip()
        if not window_ok(v):
            raise ValueError("ANALYSIS_WINDOW : vide ou HH:MM-HH:MM, début et fin différents (ex. 01:00-07:00)")
        return v

    @field_validator("dashboard_password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        # HTTP Basic : FastAPI décode l'identifiant en ASCII ; un mot de passe accentué ne pourrait jamais être saisi.
        if not v.isascii():
            raise ValueError("DASHBOARD_PASSWORD : caractères ASCII seulement (pas d'accent)")
        # Valeur d'exemple des anciens fichiers de déploiement : publique, donc jamais acceptée.
        if v.strip().lower() in ("change-moi", "changeme", "change-me"):
            raise ValueError("DASHBOARD_PASSWORD : remplacer la valeur d'exemple par un vrai mot de passe")
        return v

    @property
    def db_path(self) -> Path:
        return self.data_dir / "taxonero.sqlite"

    @property
    def skip_args(self) -> tuple:
        """Paramètres de worker.SKIPPED_NOT."""
        return (self.analyze_videos, self.skip_raw_twins, self.skip_people)

    @property
    def groups(self) -> frozenset[str]:
        return frozenset(g for g in self.detect_groups.split(",") if g)

    @property
    def backbone_path(self) -> Path:
        return self.data_dir / "gbif-backbone.sqlite"


def user_data_dir(app: str = "taxonero") -> Path:
    """Dossier des données de l'édition de bureau, selon les conventions du système."""
    import os
    import sys
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / app
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / app


# Réglages modifiables depuis le dashboard (page Paramètres). Ils sont enregistrés dans la base et
# priment sur l'environnement. Les réglages de sécurité (DRY_RUN, clé API, mot de passe) restent
# dans l'environnement uniquement, comme NAMES_LANG (en changer renomme tous les tags dans Immich).
# device : depuis le dashboard, GPU (auto : GPU s'il y en a un, sinon processeur) ou processeur. Les valeurs
# fines (cuda, xpu, mps) restent possibles dans l'environnement.
CHOICES = {"detection": ("auto", "manual"), "domestic_animals": ("skip", "keep"), "auto_confirm": ("rule", "calibrated", "fixed", "off"),
           "device": ("auto", "cpu"),
           "open_detector_mode": ("auto", "always", "fallback")}
BOUNDS = {"auto_confirm_threshold": (0.5, 0.99), "auto_confirm_max_runner_up": (0.0, 0.3), "interval_minutes": (1, 1440),
          "auto_confirm_audit": (0.0, 0.5), "gpu_batch": (1, 64), "models_idle_minutes": (0, 1440)}
GROUPS = ("animal", "plant", "fungus", "microbe")
EDITABLE = ("detection", "detect_groups", "device", "open_detector_mode", "gpu_batch", "models_idle_minutes", "exclude_extinct", "hide_absent", "analyze_videos", "skip_people", "skip_raw_twins", "full_image_fallback", "domestic_animals", "auto_confirm", "auto_confirm_threshold",
            "auto_confirm_max_runner_up", "auto_confirm_audit", "write_description", "analysis_window", "interval_minutes")
WINDOW = re.compile(r"^$|^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


def window_ok(v: str) -> bool:
    """Plage horaire valide : vide (toujours) ou HH:MM-HH:MM. Début = fin refusé : la plage serait vide et l'analyse
    automatique ne démarrerait jamais (worker.in_window)."""
    return bool(WINDOW.match(v)) and (not v or v[:5] != v[6:])


def validate_overrides(s: Settings, values: dict) -> dict:
    """Valeurs converties et vérifiées ; ValueError avec un message lisible sinon."""
    out = {}
    for k, v in values.items():
        if k not in EDITABLE:
            raise ValueError(f"réglage inconnu : {k}")
        kind = type(getattr(s, k))
        if kind is bool:
            if not isinstance(v, bool):
                raise ValueError(f"{k} : oui ou non attendu")
        elif kind in (int, float):
            try:
                v = kind(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} : nombre attendu") from None
            lo, hi = BOUNDS[k]
            if not lo <= v <= hi:
                raise ValueError(f"{k} : entre {lo} et {hi}")
        else:
            v = str(v).strip()
        if k in CHOICES and v not in CHOICES[k]:
            raise ValueError(f"{k} : {' | '.join(CHOICES[k])}")
        if k == "detect_groups":
            chosen = {g.strip() for g in v.split(",") if g.strip()}
            if not chosen or chosen - set(GROUPS):
                raise ValueError(f"detect_groups : au moins un parmi {', '.join(GROUPS)}")
            v = ",".join(g for g in GROUPS if g in chosen)
        if k == "analysis_window" and not window_ok(v):
            raise ValueError("analysis_window : vide ou HH:MM-HH:MM, début et fin différents")
        out[k] = v
    return out


def apply_overrides(s: Settings, raw: str | None) -> dict:
    """Applique les réglages enregistrés (JSON) sur s, en place. Les valeurs devenues invalides sont ignorées."""
    applied = {}
    for k, v in (json.loads(raw) if raw else {}).items():
        try:
            applied.update(validate_overrides(s, {k: v}))
        except ValueError:
            continue
    for k, v in applied.items():
        setattr(s, k, v)
    return applied
