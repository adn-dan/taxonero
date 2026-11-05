"""Boucle de travail : scan des assets Immich, analyse, auto-validation calibrée, synchronisation.
Règles de validation automatique : autoconfirm.py ; détections à relire déjà en base : curation.py."""

import hashlib
import io
import json
import logging
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image

from . import backbone, hardware, ranges, series, store, wiki
from .config import EDITABLE, Settings, apply_overrides
from .immich.client import is_missing, is_transient, probe
from .pipeline import _contained, _iou, Analysis, Analyzer, Box, Classifier, Detector, image_quality, open_image, OpenVocabDetector
from .sources import SOURCES, available, check_path, make_source, source_path
from .sync import WRITE_GAP, Syncer
from .taxonomy import HIGHER, NAMES_VERSION, TAXON_VERSION, Taxonomy, tidy_names, to_json
from .autoconfirm import audit_pick, auto_threshold, blocked_groups, should_auto_confirm
from .curation import EXCLUDED, cand_json, candidate_filter, drop_unwanted, prune_pending

log = logging.getLogger(__name__)

ANALYZER_ID = "mdv6-apa-rtdetr-e+owlv2-base+bioclip-2"
# Candidats dont les noms communs sont récupérés dès l'analyse (affichés dans la fiche de relecture).
CANDIDATE_NAMES = 5
# Repli « image entière » (aucun animal détecté) : conservé seulement au-dessus de ce seuil.
FALLBACK_MIN_CONF = 0.6


class Unavailable(RuntimeError):
    """Service réseau momentanément indisponible pendant l'analyse (Immich, GBIF)."""


class Busy(RuntimeError):
    """Un cycle tourne encore malgré la demande d'arrêt : le réglage demandé depuis le dashboard n'est pas appliqué."""


class Interrupted(BaseException):
    """Cycle arrêté à un point sûr pour appliquer un réglage du dashboard (Worker._idle). BaseException, comme
    KeyboardInterrupt : les « except Exception » qui protègent une photo ou une requête ne l'arrêtent pas."""


# Photo gardée dans la file d'analyse (condition SQL sur asset ; paramètres : Settings.skip_args, soit
# analyze_videos, skip_raw_twins, skip_people).
SKIPPED_NOT = "(? OR kind IS NOT 'VIDEO') AND (NOT ? OR twin_of IS NULL) AND (NOT ? OR coalesce(people, 0) = 0)"
# Boîte non identifiée gardée pour relecture seulement si le détecteur est sûr de lui
# (échelles de score propres à chaque détecteur).
UNKNOWN_BOX_MIN_SCORE = {"md": 0.7, "owl": 0.5}
# Au-delà, une photo dont l'analyse échoue n'est plus retentée (sinon elle bloque la file).
MAX_FAILURES = 3
# Contrôle complet de l'état d'Immich (photos disparues, tags retirés à la main) : une fois par jour.
DAILY = 86400
# Changement de réglage pendant un cycle : délai laissé au cycle pour atteindre un point d'arrêt (une photo, une
# requête réseau) avant de renoncer (Busy).
STOP_WAIT = 60


def in_window(window: str, now: datetime | None = None) -> bool:
    """Fenêtre horaire "HH:MM-HH:MM" (peut passer minuit). Vide = toujours."""
    if not window:
        return True
    now = now or datetime.now()
    start, end = (datetime.strptime(x.strip(), "%H:%M").time() for x in window.split("-"))
    t = now.time()
    return start <= t < end if start <= end else (t >= start or t < end)


def fetch_model(url: str, dest: Path, sha256: str = "", user_agent: str = "taxonero") -> None:
    """Télécharge un fichier de modèle (fichier temporaire renommé à la fin ; empreinte vérifiée si donnée)."""
    log.info("téléchargement du modèle %s…", dest.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp, h = dest.with_suffix(".part"), hashlib.sha256()
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60, read=300),
                          headers={"User-Agent": user_agent}) as r, open(tmp, "wb") as f:
            r.raise_for_status()
            for chunk in r.iter_bytes(2 ** 20):
                f.write(chunk)
                h.update(chunk)
        if sha256 and h.hexdigest() != sha256.lower():
            raise ValueError(f"empreinte inattendue pour {dest.name} : {h.hexdigest()}")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def requeue(db: sqlite3.Connection, scope: str) -> int:
    """Remet des photos dans la file d'analyse. failed : abandonnées après échecs ; outdated : analysées
    par une autre version des modèles ; all : toutes. Les détections relues sont conservées."""
    where, args = {"failed": ("fail_count >= ?", (MAX_FAILURES,)),
                   "outdated": ("analyzed_at IS NOT NULL AND analyzer IS NOT ?", (ANALYZER_ID,)),
                   "all": ("1", ())}[scope]
    n = db.execute(f"UPDATE asset SET analyzed_at = NULL, fail_count = 0, last_error = NULL WHERE {where}", args).rowcount
    db.commit()
    return n


# -- service -------------------------------------------------------------------

class Worker:
    def __init__(self, s: Settings, analyzer: Analyzer | None = None):
        self.s = s
        self.db = store.connect(s.db_path)
        self.undo: dict = {"state": "idle"}   # retrait d'Immich (start_uninstall)
        # Valeurs de l'environnement (ou par défaut), puis réglages faits depuis le dashboard par-dessus.
        self.env_defaults = {k: getattr(s, k) for k in EDITABLE}
        apply_overrides(s, store.get_state(self.db, "settings"))
        # Connexion à Immich : environnement d'abord, sinon celle enregistrée depuis le dashboard.
        self.env_immich = {"url": bool(s.immich_url), "key": bool(s.immich_api_key)}
        saved = json.loads(store.get_state(self.db, "immich") or "{}")
        s.immich_url = s.immich_url or saved.get("url", "")
        s.immich_api_key = s.immich_api_key or saved.get("key", "")
        # Bureau : écriture des résultats réglée dans le dashboard (DRY_RUN de l'environnement sinon).
        if s.edition == "desktop" and "dry_run" not in s.model_fields_set and store.get_state(self.db, "dry_run"):
            s.dry_run = store.get_state(self.db, "dry_run") == "1"
        # Source des photos : environnement d'abord (verrouillée), sinon celle choisie dans le dashboard.
        self.env_source = bool({"source", "photos_dir", "catalog_path"} & s.model_fields_set)
        saved_source = json.loads(store.get_state(self.db, "source") or "{}")
        if not self.env_source and saved_source.get("source") in SOURCES:
            s.source = saved_source["source"]
            s.photos_dir = Path(saved_source.get("photos_dir") or s.photos_dir)
            s.catalog_path = Path(saved_source["catalog_path"]) if saved_source.get("catalog_path") else s.catalog_path
        self.client = make_source(s)
        self.tx = Taxonomy(self.db, langs=s.langs, user_agent=s.user_agent, backbone=s.backbone_path if s.gbif_local else None)
        self._analyzer = analyzer
        self._device: str | None = None   # périphérique des modèles chargés (None : fournis de l'extérieur)
        # Santé (/healthz) : dernière activité de la boucle ; une boucle figée rend le conteneur « unhealthy ».
        self.loop_started, self.last_activity = False, time.time()
        # Chargement des modèles (plusieurs Go) : une seule fois, même si le dashboard et le cycle le demandent ensemble.
        self._analyzer_lock = threading.Lock()
        self._models_used = 0.0   # dernière demande des modèles (time.monotonic), pour les décharger après inactivité
        self._stages_kept: dict[str, list] = {}   # temps par étape des modèles déchargés (page Paramètres)
        # Original des petits sujets : None tant que non essayé, True si obtenu, False sans la permission asset.download.
        self._original_ok: bool | None = None
        self.backbone_state = {"state": "waiting" if s.gbif_local else "off"}   # taxonomie GBIF locale (Paramètres)
        self._speed: list[float] = []   # durée d'analyse des dernières photos (s) : modèles et requêtes GBIF, sans le téléchargement de l'image
        self._timings: dict[str, list] = {}   # étapes du worker (téléchargement, enregistrement) -> [secondes, photos]
        self._last_done = 0.0
        self._blocked_set: set[str] = set()
        self._blocked_at = -1e9
        # Un seul cycle à la fois (boucle automatique ou bouton « Synchroniser »), même connexion SQLite.
        self.cycle_lock = threading.Lock()
        # Arrêt demandé par un réglage du dashboard (_idle), vérifié aux points sûrs du cycle (_checkpoint) ; réveil
        # de la boucle pour reprendre aussitôt après.
        self._stop, self._wake = threading.Event(), threading.Event()
        self._cycle_manual = False    # le cycle en cours est-il manuel (bouton « Synchroniser ») ?
        self._resume_manual = False   # cycle manuel arrêté par un réglage : la boucle le relance en manuel
        self._resume_pending = False  # cycle arrêté par un réglage, à relancer par la boucle
        self._loading: threading.Thread | None = None   # chargement des modèles, hors du cycle
        # Analyse mise en pause depuis le dashboard (elle prend toute la machine) : gardé d'un lancement à l'autre.
        self.analysis_paused = store.get_state(self.db, "analysis_paused") == "1"
        self._load_error: BaseException | None = None
        self.progress: dict = {"running": False, "step": None, "done": 0, "total": 0, "current": None, "step_started": None,
                               "started": None, "finished": None, "error": None}
        n = tidy_names(self.db)
        if n:
            log.info("%d nom(s) commun(s) remis en casse normale (ex. « DOGS »)", n)
        n = ranges.migrate_legacy(self.db)
        if n:
            log.info("%d aire(s) de répartition d'un ancien format supprimée(s), retéléchargée(s) ensuite", n)

    def backup(self, force: bool = False) -> Path | None:
        """Copie de la base du jour dans DATA_DIR/backups (une par jour, les BACKUP_KEEP plus récentes gardées).
        Copie cohérente même pendant l'analyse (VACUUM INTO), allégée des caches réseau qui se retéléchargent :
        réponses GBIF/Wikidata et aires de répartition. Sans la clé API Immich non plus (à ressaisir dans le
        dashboard après une restauration). Restauration : arrêter le service, copier le fichier voulu sur
        DATA_DIR/taxonero.sqlite, relancer."""
        keep = self.s.backup_keep
        today = datetime.now().strftime("%Y-%m-%d")
        if keep <= 0 or (not force and store.get_state(self.db, "last_backup") == today):
            return None
        folder = self.s.data_dir / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        dest, tmp = folder / f"taxonero-{today}.sqlite", folder / f".taxonero-{today}.tmp"
        tmp.unlink(missing_ok=True)
        self.db.commit()
        self.db.execute("VACUUM INTO ?", (str(tmp),))
        copy = sqlite3.connect(tmp)
        for table in ("http_cache", "taxon_range"):
            if copy.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone():
                copy.execute(f"DELETE FROM {table}")
        copy.execute("DELETE FROM state WHERE key = 'immich'")   # la clé API ne voyage pas avec les sauvegardes
        copy.commit()
        copy.execute("VACUUM")
        copy.close()
        tmp.replace(dest)
        for old in sorted(folder.glob("taxonero-*.sqlite"))[:-keep]:
            old.unlink()
        store.set_state(self.db, "last_backup", today)
        log.info("sauvegarde : %s (%d Ko)", dest.name, dest.stat().st_size // 1024)
        return dest

    def _checkpoint(self) -> None:
        """Point d'arrêt sûr du cycle (rien d'écrit à moitié) : Interrupted si un réglage attend (_idle). Atteindre un
        point d'arrêt prouve que le cycle avance : /healthz le compte comme une activité (longue synchronisation,
        contrôle quotidien), sans quoi le conteneur passerait « unhealthy » en plein travail."""
        self.last_activity = time.time()
        if self._stop.is_set():
            raise Interrupted()

    def _step(self, step: str, total: int = 0) -> None:
        """Étape en cours, en code (check, scan, reconcile, models, analyze, prune, sync, prefetch) : le dashboard
        la traduit. current : élément en cours (nom du fichier pendant l'analyse). Point d'arrêt (_checkpoint)."""
        self._checkpoint()
        self.progress.update(step=step, done=0, total=total, current=None, step_started=time.time())
        self.last_activity = time.time()

    def _tick(self) -> None:
        """Élément suivant d'une étape ; point d'arrêt (_checkpoint) : à appeler hors transaction en cours."""
        self._checkpoint()
        self.progress["done"] += 1
        self.last_activity = time.time()

    @property
    def analyzer(self) -> Analyzer:
        with self._analyzer_lock:
            device, why = hardware.resolve(self.s.device)
            ov_mode = hardware.open_mode(self.s.open_detector_mode, device)
            if self._analyzer is not None and self._device not in (None, device):
                # Choix GPU / processeur changé dans les Paramètres : modèles rechargés sur le nouveau périphérique
                # (l'analyse en cours, s'il y en a une, finit sur l'ancien).
                log.info("calcul : passage de %s à %s, modèles rechargés", self._device, device)
                old, self._analyzer = self._device, None
                hardware.release(old)
            if self._analyzer is None:
                log.info("calcul : %s (%s), OWLv2 %s", hardware.WHY[why], device, ov_mode)
                hardware.full_precision()
                self._device = device
                det = None
                if self.s.detector_path.exists():   # ONNX (images Docker d'avant, ou fourni) : gardé tel quel
                    det = Detector(self.s.detector_path, providers=hardware.onnx_session_providers(device),
                                   min_score=self.s.detector_min_score)
                    det.providers = det.session.get_providers()
                else:   # PyTorch, poids officiels téléchargés au premier usage (megadetector.py)
                    from .megadetector import TorchDetector, weights
                    det = TorchDetector(weights(self.s.data_dir / "models", self.s.user_agent), device,
                                        min_score=self.s.detector_min_score)
                log.info("détecteur : %s", ", ".join(det.providers))
                ov = None
                if self.s.open_detector:
                    ov = OpenVocabDetector(self.s.open_detector_model, device=device,
                                           min_score=self.s.open_detector_min_score)
                analyzer = Analyzer(det, Classifier(device=device), self.tx, open_detector=ov)
                analyzer.drop = candidate_filter(self.s, self.tx)
                self._analyzer = analyzer
            self._analyzer.open_mode = ov_mode   # sans rechargement
            self._models_used = time.monotonic()
            return self._analyzer

    def unload_idle(self) -> bool:
        """Modèles inutilisés depuis models_idle_minutes : retirés de la mémoire du GPU, rechargés à la prochaine demande.
        Une analyse en cours garde sa référence aux modèles : elle finit normalement. Rien sur processeur, où ils ne
        prennent pas de mémoire du GPU, ni pour un analyseur fourni de l'extérieur (_device None)."""
        minutes = self.s.models_idle_minutes
        with self._analyzer_lock:
            if (self._analyzer is None or self._device in (None, "cpu") or minutes <= 0
                    or time.monotonic() - self._models_used < minutes * 60):
                return False
            device, self._stages_kept = self._device, {**self._stages_kept, **getattr(self._analyzer, "timings", {})}
            self._analyzer = None
        hardware.release(device)
        log.info("modèles déchargés de %s après %d min sans analyse", device, minutes)
        return True

    def _watch_idle_models(self) -> None:
        while True:
            time.sleep(30)
            try:
                self.unload_idle()
            except Exception:
                log.exception("déchargement des modèles en échec")

    @property
    def configured(self) -> bool:
        return self.s.source != "immich" or bool(self.s.immich_url and self.s.immich_api_key)

    def credentials(self, url: str, api_key: str) -> tuple[str, str]:
        """(adresse, clé) d'une connexion saisie dans le dashboard ; les valeurs de l'environnement priment. Clé vide :
        la clé enregistrée, mais seulement pour le serveur déjà réglé (ou au premier réglage de l'adresse) : envoyée à
        une autre adresse, elle partirait chez ce serveur-là. ValueError dans ce cas."""
        url = self.s.immich_url if self.env_immich["url"] else url.strip().rstrip("/")
        given = "" if self.env_immich["key"] else api_key.strip()
        if not given and self.s.immich_url and url != self.s.immich_url:
            raise ValueError("nouvelle adresse : saisir aussi la clé API")
        return url, given or self.s.immich_api_key

    def connect(self, url: str, api_key: str) -> dict:
        """Nouvelle connexion depuis le dashboard : testée, puis enregistrée et utilisée seulement si Immich répond
        et accepte la clé (sinon rien ne change). Les valeurs venues de l'environnement ne sont pas remplacées.
        Clé vide : la clé enregistrée (voir credentials)."""
        try:
            url, api_key = self.credentials(url, api_key)
        except ValueError as e:
            return {"code": "key_required", "errors": [str(e)]}
        # Délai normal (30 s) : ce client sert ensuite à l'analyse (originaux lourds), pas seulement au test.
        client, state = probe(url, api_key, dry_run=self.s.dry_run, timeout=30.0)
        if client is None:
            return state
        try:
            with self._idle():
                saved = {k: v for k, v in (("url", url), ("key", api_key)) if not self.env_immich[k]}
                store.set_state(self.db, "immich", json.dumps(saved))
                self.s.immich_url, self.s.immich_api_key = url, api_key
                old, self.client = self.client, client
                self._original_ok = None
                self.check()
        except Busy:
            client.close()
            raise
        log.info("connexion à Immich %s enregistrée (%s)", client.version, state["code"])
        threading.Timer(30, old.close).start()
        return state

    def set_dry_run(self, value: bool) -> None:
        """Édition de bureau : écriture des résultats activée depuis le dashboard (pas d'environnement à régler)."""
        if self.s.edition != "desktop":
            raise ValueError("DRY_RUN se règle dans l'environnement (édition serveur)")
        with self._idle():
            before = self.s.dry_run
            self.s.dry_run = self.client.dry_run = bool(value)
            try:
                self.check()
            except Exception:   # source injoignable : réglage inchangé, surtout quand il s'agit d'activer l'écriture
                self.s.dry_run = self.client.dry_run = before
                raise
            store.set_state(self.db, "dry_run", "1" if value else "0")

    def set_source(self, source: str, path: str | None = None) -> dict:
        """Change de source depuis le dashboard (sauf si l'environnement la fixe) : dossier des photos ou
        bibliothèque du logiciel selon la source. Les photos de l'ancienne source restent en base avec leurs
        relectures : masquées au prochain contrôle, elles reviennent si on y retourne."""
        if self.env_source:
            raise ValueError("source fixée par l'environnement (SOURCE, PHOTOS_DIR, CATALOG_PATH)")
        if not any(o["id"] == source and o["available"] for o in available(self.s.edition)):
            raise ValueError(f"source non disponible ici : {source}")
        with self._idle():
            where = SOURCES[source]["kind"]
            before = (self.s.source, self.s.photos_dir, self.s.catalog_path)
            if where != "server":
                chosen = Path(path or "").expanduser()
                if reason := check_path(source, chosen):
                    raise ValueError(reason)
                if where == "folder":
                    self.s.photos_dir = chosen
                else:
                    self.s.catalog_path = chosen
            self.s.source = source
            old = self.client
            try:
                self.client = make_source(self.s)
                self.check()
            except Exception:   # source injoignable : on reste sur l'ancienne, rien n'est enregistré
                if self.client is not old:
                    self.client.close()
                self.client = old
                self.s.source, self.s.photos_dir, self.s.catalog_path = before
                raise
            store.set_state(self.db, "source", json.dumps({"source": source, "photos_dir": str(self.s.photos_dir),
                                                           "catalog_path": str(self.s.catalog_path or "")}))
            threading.Timer(30, old.close).start()
            for key in ("last_scan", "last_scan_video", "last_reconcile"):
                store.set_state(self.db, key, "")   # nouvelle source : tout relister au prochain cycle
        log.info("source des photos : %s %s", source, source_path(self.s) or self.s.immich_url)
        return {"source": source, "writable": self.client.writable, **self.client.status()}

    @contextmanager
    def _idle(self):
        """Changement fait depuis le dashboard (source, connexion, écriture) : jamais pendant un cycle, qui utilise
        la même connexion SQLite et la même source (sinon un scan en cours écrase son repère « tout relister », ou
        continue sur un client fermé). Un cycle en cours est arrêté au prochain point sûr (_checkpoint), le réglage
        appliqué, puis le cycle relancé aussitôt : l'utilisateur n'a rien à refaire. Busy si le cycle n'a pas pu
        s'arrêter dans STOP_WAIT secondes."""
        running = not self.cycle_lock.acquire(blocking=False)
        if running:
            manual = self._cycle_manual
            self._stop.set()
            got = self.cycle_lock.acquire(timeout=STOP_WAIT)
            self._stop.clear()
            if not got:
                raise Busy("un cycle de synchronisation est en cours")
        try:
            yield
        finally:
            self.cycle_lock.release()
            if running:   # reprise par la boucle (loop), dans le même mode
                self._resume_manual = self._resume_manual or manual
                self._resume_pending = True
                self._wake.set()

    def check(self) -> bool:
        if not self.configured:
            store.set_state(self.db, "writable", "0")
            return False
        self._original_ok = None   # permission asset.download peut-être ajoutée depuis un 403
        ok = self.client.check()
        for w in self.client.warnings:
            log.warning("compatibilité : %s", w)
        for e in self.client.errors:
            log.error("incompatibilité : %s", e)
        store.set_state(self.db, "immich_version", self.client.version or "")
        store.set_state(self.db, "writable", "1" if ok else "0")
        return ok

    def mark_missing(self, asset_id: str) -> None:
        log.info("photo %s absente d'Immich (supprimée ou à la corbeille) : masquée", asset_id)
        self.db.execute("UPDATE asset SET missing_at = ? WHERE asset_id = ?", (time.time(), asset_id))
        self.db.commit()

    def refresh_taxa(self, limit: int = 50) -> int:
        """Ré-enrichit les taxons enregistrés dans un format plus ancien (ex. avant les noms multilingues)."""
        n = 0
        for r in self.db.execute("SELECT gbif_key, data FROM taxon").fetchall():
            data = json.loads(r["data"])
            if data.get("v", 0) >= TAXON_VERSION or n >= limit:
                continue
            self.db.commit()
            self._checkpoint()
            t = self.tx.match(data["scientific_name"], kingdom=data.get("lineage", {}).get("kingdom"))
            if t and t.gbif_key == r["gbif_key"]:
                store.save_taxon(self.db, t.gbif_key, to_json(t))
                n += 1
        self.db.commit()
        if n:
            log.info("%d taxon(s) ré-enrichi(s) au format %d", n, TAXON_VERSION)
        # Noms des rangs supérieurs au pluriel (GBIF) pour les taxons en cache d'avant cette règle.
        m = 0
        for r in self.db.execute("SELECT gbif_key, data FROM taxon").fetchall():
            data = json.loads(r["data"])
            if m >= limit or data.get("nv", 0) >= NAMES_VERSION or not HIGHER(data.get("rank", "species")):
                continue
            self.db.commit()
            self._checkpoint()
            try:
                data = self.tx.plural_names({**data, "gbif_key": r["gbif_key"]})
            except Exception:
                log.debug("noms GBIF indisponibles pour %s", data.get("scientific_name"), exc_info=True)
                continue
            store.save_taxon(self.db, r["gbif_key"], json.dumps(data, ensure_ascii=False))
            m += 1
        self.db.commit()
        if m:
            log.info("%d rang(s) supérieur(s) renommé(s) au pluriel (noms GBIF)", m)
        # Espèces sans article : recherche par nom et par synonymes (Wikidata a souvent une fiche par nom).
        a = 0
        for r in self.db.execute("SELECT gbif_key, data FROM taxon").fetchall():
            data = json.loads(r["data"])
            if a >= limit or not self.tx.articles_stale(data):
                continue
            self.db.commit()
            self._checkpoint()
            try:
                data = self.tx.refresh_articles({**data, "gbif_key": r["gbif_key"]})
            except Exception:
                log.debug("articles indisponibles pour %s", data.get("scientific_name"), exc_info=True)
                continue
            store.save_taxon(self.db, r["gbif_key"], json.dumps(data, ensure_ascii=False))
            a += 1
        self.db.commit()
        if a:
            log.info("articles recherchés de nouveau pour %d espèce(s)", a)
        return n + m + a

    def refresh_places(self, limit: int = 200) -> int:
        """Rattrapage : lieux (ville, région, pays) des photos enregistrées avant leur prise en charge."""
        rows = self.db.execute("SELECT asset_id FROM asset WHERE country IS NULL AND lat IS NOT NULL AND missing_at IS NULL "
                               "AND (city IS NULL OR city != '') LIMIT ?", (limit,)).fetchall()
        for r in rows:
            self.db.commit()
            self._checkpoint()
            a = self.client.get_asset(r["asset_id"], missing_ok=True)
            if a is None:
                self.mark_missing(r["asset_id"])
                continue
            exif = a.get("exifInfo") or {}
            # Chaîne vide si Immich n'a pas de lieu : évite de redemander à chaque cycle.
            self.db.execute("UPDATE asset SET city = ?, state = ?, country = ? WHERE asset_id = ?",
                            (exif.get("city") or "", exif.get("state"), exif.get("country"), r["asset_id"]))
        self.db.commit()
        return len(rows)

    def kinds(self) -> tuple[str, ...]:
        return ("IMAGE", "VIDEO") if self.s.analyze_videos else ("IMAGE",)

    def scan(self) -> int:
        """Récupère les assets nouveaux ou modifiés depuis le dernier scan. Un repère par type : activer les
        vidéos plus tard les récupère toutes au cycle suivant."""
        n = 0
        for kind in self.kinds():
            key = "last_scan" if kind == "IMAGE" else "last_scan_video"
            since = store.get_state(self.db, key)
            started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            for a in self.client.iter_assets(updated_after=since or None, kind=kind, with_people=True):
                self._checkpoint()   # repère non avancé : le prochain scan reprend ces photos
                if store.upsert_asset(self.db, a, with_people=True):
                    n += 1
            self.db.commit()
            store.set_state(self.db, key, started)
        return n

    def reconcile(self, force: bool = False) -> dict | None:
        """Une fois par jour : liste complète des photos d'Immich (sans EXIF, donc légère) pour repérer
        celles qui ont été supprimées ou mises à la corbeille. Elles sont masquées, pas effacées : une photo
        restaurée réapparaît avec ses relectures. Relit aussi les personnes de chaque photo : Immich reconnaît
        les visages après l'import, sans changer la date de modification que suit scan()."""
        last = float(store.get_state(self.db, "last_reconcile") or 0)
        # Base d'avant le comptage des personnes : un passage tout de suite, sans attendre le lendemain.
        unknown = self.db.execute("SELECT 1 FROM asset WHERE people IS NULL AND missing_at IS NULL "
                                  "AND (? OR kind IS NOT 'VIDEO') LIMIT 1", (self.s.analyze_videos,)).fetchone()
        if not force and not unknown and time.time() - last < DAILY:
            return None
        self._step("reconcile")
        # Vidéos désactivées : elles ne sont pas listées, donc masquées comme des photos disparues.
        listed = {}
        for kind in self.kinds():
            for a in self.client.iter_assets(with_exif=False, size=1000, kind=kind, with_people=True):
                self._checkpoint()
                listed[a["id"]] = len(a.get("people") or [])
        present = listed.keys()
        local = {r["asset_id"]: r["missing_at"] for r in self.db.execute("SELECT asset_id, missing_at FROM asset")}
        if not present and local:
            log.warning("Immich ne renvoie aucune photo : contrôle des suppressions ignoré")
            return None
        gone = [a for a, m in local.items() if a not in present and m is None]
        back = [a for a, m in local.items() if a in present and m is not None]
        now = time.time()
        self.db.executemany("UPDATE asset SET missing_at = ? WHERE asset_id = ?", [(now, a) for a in gone])
        self.db.executemany("UPDATE asset SET missing_at = NULL WHERE asset_id = ?", [(a,) for a in back])
        self.db.executemany("UPDATE asset SET people = ? WHERE asset_id = ? AND people IS NOT ?",
                            [(n, a, n) for a, n in listed.items()])
        self.db.commit()
        store.set_state(self.db, "last_reconcile", str(now))
        if gone or back:
            log.info("photos disparues d'Immich : %d, revenues : %d", len(gone), len(back))
        return {"missing": len(gone), "back": len(back)}

    def original(self, asset_id: str, kind: str | None = None) -> bytes | None:
        """Original pour les petits sujets ; None sans la permission asset.download (on le note une fois), et
        jamais pour une vidéo (fichier lourd, et l'image clé d'Immich est déjà à pleine taille d'aperçu). Photo
        supprimée (is_missing) : l'exception remonte, pour que l'appelant le dise au lieu d'invoquer la permission."""
        if self._original_ok is False:
            return None
        if kind is None:
            row = self.db.execute("SELECT kind FROM asset WHERE asset_id = ?", (asset_id,)).fetchone()
            kind = row["kind"] if row else None
        if kind == "VIDEO":
            return None
        try:
            data = self.client.original(asset_id)
            self._original_ok = True
            return data
        except httpx.HTTPStatusError as e:
            if is_missing(e):
                raise
            if e.response.status_code in (401, 403):
                self._original_ok = False
                log.info("pas de permission asset.download : petits sujets identifiés sur l'aperçu")
            return None
        except (OSError, ValueError) as e:   # dossier local : RAW sans aperçu intégré, fichier illisible
            if is_missing(e):   # fichier disparu
                raise
            return None

    def _blocked(self) -> set[str]:
        """Groupes exclus de la validation automatique : recalculés au plus une fois par minute (le calcul parcourt
        les relectures ; à chaque photo, il coûtait ~90 ms sur une base de 70 000 photos et grossissait avec elle)."""
        now = time.monotonic()
        if now - self._blocked_at > 60:
            self._blocked_set, self._blocked_at = blocked_groups(self.db, self.s), now
        return self._blocked_set

    def _prepare(self, asset_id: str, results: list[Analysis], threshold: float | None) -> list[tuple]:
        """Lignes à insérer pour une analyse. Tout l'accès réseau (fiches des taxons, noms des candidats) se
        fait ici, avant d'écrire : l'écriture qui suit est alors une transaction courte et atomique."""
        # Détections déjà relues (validées, rejetées, supprimées) : une ré-analyse ne les recrée pas en double.
        reviewed = [Box(r["x"], r["y"], r["w"], r["h"], 1.0, "animal") for r in self.db.execute(
            "SELECT x, y, w, h FROM detection WHERE asset_id = ? AND status != 'pending'", (asset_id,))]
        blocked = self._blocked() if threshold is not None else set()
        # Fiches et noms communs à récupérer (décisions, premiers candidats) : demandés en parallèle d'abord.
        stale = lambda k: k and ((t := store.load_taxon(self.db, k)) is None or t.get("v", 0) < TAXON_VERSION)
        self.tx.prefetch_enrich([a.decision.gbif_key for a in results if stale(a.decision.gbif_key)]
                                + [c.gbif_key for a in results for c in a.decision.candidates[:CANDIDATE_NAMES]
                                   if c.gbif_key and not store.load_taxon(self.db, c.gbif_key)])
        rows = []
        for a in results:
            if any(_iou(a.box, b) >= 0.3 or _contained(a.box, b) >= 0.6 for b in reviewed):
                continue
            d = a.decision
            if a.box.label == "image" and (d.rank == "none" or d.confidence < FALLBACK_MIN_CONF):
                continue
            if d.name in EXCLUDED:
                continue
            if a.domestic and self.s.domestic_animals == "skip":
                continue
            # Boîte non identifiée : gardée pour relecture seulement si le détecteur est sûr de lui.
            if d.rank == "none" and a.box.score < UNKNOWN_BOX_MIN_SCORE.get(a.box.source, 1.0):
                continue
            known = store.load_taxon(self.db, d.gbif_key) if d.gbif_key else None
            if d.gbif_key and (not known or known.get("v", 0) < TAXON_VERSION):
                t = self.tx.match(d.name, kingdom=(d.candidates[0].lineage.get("kingdom") if d.candidates else None))
                if t:
                    store.save_taxon(self.db, t.gbif_key, to_json(t))
                    self.db.commit()
            auto = should_auto_confirm(a, threshold, self.s.auto_confirm_max_runner_up, blocked)
            audit = auto and audit_pick(asset_id, a.box, self.s.auto_confirm_audit)
            auto = auto and not audit
            cands = [cand_json(c) for c in d.candidates[:8] if c.name not in EXCLUDED]
            out = [cand_json(c) for c in d.excluded[:8] if c.name not in EXCLUDED]
            b = a.box
            rows.append((asset_id, b.x, b.y, b.w, b.h, b.score, b.label, b.source,
                         "confirmed" if auto else "pending", "auto" if auto else None,
                         d.gbif_key, d.rank, d.confidence, d.margin, d.plausibility,
                         json.dumps(cands, ensure_ascii=False), json.dumps(out, ensure_ascii=False), int(audit),
                         d.gbif_key, d.rank, d.confidence, a.frame_support, time.time()))
            if not auto:
                self._enrich_candidates(d.candidates[:CANDIDATE_NAMES])
        return rows

    def _insert(self, rows: list[tuple]) -> int:
        self.db.executemany(
            "INSERT INTO detection (asset_id, x, y, w, h, det_score, box_label, detector, source, status, "
            "confirmed_by, taxon_key, taxon_rank, score, margin, plausibility, candidates, candidates_out, audit, "
            "model_key, model_rank, model_conf, frame_support, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'auto', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def _enrich_candidates(self, cands) -> None:
        """Noms communs des premiers candidats, pour une relecture lisible (mis en cache, sans bloquer)."""
        for c in cands:
            if c.gbif_key and not store.load_taxon(self.db, c.gbif_key):
                try:
                    t = self.tx.match(c.accepted_name or c.name, kingdom=c.lineage.get("kingdom"))
                    if t and t.gbif_key == c.gbif_key:
                        store.save_taxon(self.db, t.gbif_key, to_json(t))
                        self.db.commit()
                except Exception:
                    log.debug("noms indisponibles pour %s", c.name, exc_info=True)

    def _analyze_asset(self, asset_id: str, lat: float | None, lon: float | None) -> list[Analysis]:
        data = self.client.thumbnail(asset_id, self.s.thumbnail_size)
        analyzer = self.analyzer   # chargement des modèles hors chronométrage
        t = time.monotonic()
        out = analyzer.analyze(data, lat, lon, original=lambda: self.original(asset_id), fallback=self.s.full_image_fallback,
                               groups=self.s.groups)
        self._speed.append(time.monotonic() - t)
        del self._speed[:-50]
        return out

    def models_info(self) -> dict:
        """Modèles, matériel réellement utilisé et vitesse mesurée (page Paramètres)."""
        hw = hardware.describe(self.s.device)
        det = getattr(self._analyzer, "detector", None)
        speed = sorted(self._speed)
        return {
            "hardware": hw,
            "loaded": self._analyzer is not None,
            "detector_file": True,   # ONNX fourni, sinon PyTorch (poids téléchargés au premier usage)
            "detector_onnx": self.s.detector_path.exists(),
            "detector_providers": det.providers if det else None,
            "open_detector": self.s.open_detector, "open_detector_model": self.s.open_detector_model,
            "open_mode": hardware.open_mode(self.s.open_detector_mode, hw["device"]),
            "original": self._original_ok,
            "analyzer_id": ANALYZER_ID,
            "taxonomy": {**self.backbone_state, "local": self.tx.local is not None},
            # Où passe le temps : ms par photo pour chaque étape (les étapes se chevauchent, voir analyze()).
            "stages": {k: round(sec / n * 1000) for k, (sec, n) in {**self._stages_kept, **getattr(self._analyzer, "timings", {}), **self._timings}.items() if n},
            "speed": {"median_s": round((speed[(len(speed) - 1) // 2] + speed[len(speed) // 2]) / 2, 1), "n": len(speed)} if speed else None,
        }

    def queue_sql(self) -> tuple[str, tuple]:
        """Photos à analyser (condition SQL sur asset, paramètres) : ni disparues, ni abandonnées, ni écartées
        (vidéos si désactivées, RAW doublé d'un JPG, photos avec des personnes si SKIP_PEOPLE)."""
        return ("analyzed_at IS NULL AND missing_at IS NULL AND fail_count < ? AND " + SKIPPED_NOT,
                (MAX_FAILURES, *self.s.skip_args))

    def pending_analysis(self) -> int:
        if self.s.detection == "manual":   # rectangles tracés à la main seulement : pas d'analyse automatique
            return 0
        where, args = self.queue_sql()
        return self.db.execute(f"SELECT count(*) FROM asset WHERE {where}", args).fetchone()[0]

    def analyze(self, limit: int | None = None) -> int:
        """Un lot de photos à analyser, les plus récentes d'abord. Une photo en échec est retentée aux cycles
        suivants, puis abandonnée après MAX_FAILURES échecs (visible dans le dashboard, relançable).

        En chaîne, pour que le GPU attende le moins possible : les aperçus des photos suivantes se téléchargent
        et se décodent pendant que les modèles travaillent, et les modèles traitent un groupe (fil « gpu ») pendant
        que le groupe précédent passe la vérification géographique (GBIF) et l'enregistrement en base."""
        where, args = self.queue_sql()
        rows = self.db.execute(f"SELECT asset_id, file_name, lat, lon, kind FROM asset WHERE {where} "
                               "ORDER BY fail_count, taken_at DESC LIMIT ?", (*args, limit or self.s.batch_size)).fetchall()
        threshold = auto_threshold(self.db, self.s)
        if self.progress["step"] != "analyze":   # run_once compte toute la file, pas seulement ce lot
            self._step("analyze", len(rows))
        if not rows:
            return 0
        analyzer = self.analyzer   # chargement des modèles avant le chronométrage et le choix du lot
        # GPU : plusieurs photos par passage de BioCLIP. Processeur : une à la fois (aucun gain à grouper).
        chunk = self.s.gpu_batch if self._device not in (None, "cpu") and hasattr(analyzer, "analyze_many") else 1
        piped = hasattr(analyzer, "infer_many")
        load = lambda r: open_image(self.client.thumbnail(r["asset_id"], self.s.thumbnail_size)) if piped else \
            self.client.thumbnail(r["asset_id"], self.s.thumbnail_size)
        with ThreadPoolExecutor(4, thread_name_prefix="apercu") as pool, ThreadPoolExecutor(1, thread_name_prefix="gpu") as gpu:
            fetch = lambda r: pool.submit(load, r)
            pending = [fetch(r) for r in rows[:chunk * 2]]
            previous = None
            try:
                for i in range(0, len(rows), chunk):
                    if self.analysis_paused:   # pause : le groupe en cours finit, les suivants attendent
                        break
                    group = rows[i:i + chunk]
                    pending += [fetch(r) for r in rows[i + chunk * 2:i + chunk * 3]]
                    items, t0 = [], time.perf_counter()
                    for r, fut in zip(group, pending[:len(group)]):
                        self._tick()
                        self.progress["current"] = r["file_name"]
                        try:
                            items.append((r, fut.result()))
                        except Exception as e:
                            self._failed(r["asset_id"], e)
                    del pending[:len(group)]
                    self._timed("download", t0, len(group))
                    if not piped:
                        self._analyze_group(analyzer, items, threshold)
                        continue
                    job = gpu.submit(analyzer.infer_many, self._args(items), self.s.full_image_fallback, self.s.groups) if items else None
                    if previous:
                        self._finish(analyzer, *previous, threshold)
                    previous = (items, job)
            finally:
                for fut in pending:   # arrêt (Interrupted) : aperçus pas encore commencés abandonnés
                    fut.cancel()
                if previous:
                    self._finish(analyzer, *previous, threshold)
        return len(rows)

    def _args(self, items: list[tuple]) -> list[tuple]:
        # kind passé tel quel : _original ne lit pas la base depuis le fil des modèles.
        return [(data, r["lat"], r["lon"], (lambda aid=r["asset_id"], kind=r["kind"]: self.original(aid, kind))) for r, data in items]

    def _timed(self, stage: str, t0: float, photos: int) -> None:
        acc = self._timings.setdefault(stage, [0.0, 0])
        acc[0] += time.perf_counter() - t0
        acc[1] += photos

    def _finish(self, analyzer, items: list[tuple], job, threshold: float | None) -> None:
        """Fin d'analyse d'un groupe dont les modèles ont tourné dans le fil « gpu » : décision (GBIF) et
        enregistrement. Si le groupe a échoué, chaque photo est reprise seule."""
        if not items:
            return
        try:
            batch = analyzer.decide_many(job.result(), self.s.groups)
        except Unavailable:
            raise
        except Exception:
            for item in items:
                self._analyze_group(analyzer, [item], threshold)
            return
        self._save(items, batch, threshold)

    def _analyze_group(self, analyzer, items: list[tuple], threshold: float | None) -> None:
        """Analyse et enregistre un lot de photos déjà téléchargées. Si le lot échoue, chaque photo est reprise
        seule : une photo illisible ne fait pas échouer les autres."""
        if not items:
            return
        args = self._args(items)
        try:
            fb = self.s.full_image_fallback
            g = self.s.groups
            batch = analyzer.analyze_many(args, fb, g) if len(items) > 1 else [analyzer.analyze(*args[0], fallback=fb, groups=g)]
        except Exception:
            if len(items) == 1:
                self._failed(items[0][0]["asset_id"], sys.exc_info()[1])
                return
            for item in items:
                self._analyze_group(analyzer, [item], threshold)
            return
        self._save(items, batch, threshold)

    def _save(self, items: list[tuple], batch: list, threshold: float | None) -> None:
        t0 = time.perf_counter()
        for (r, _), results in zip(items, batch):
            aid = r["asset_id"]
            try:
                new = self._prepare(aid, results, threshold)
                # Ré-analyse : on remplace les détections automatiques encore en attente, jamais les relues.
                self.db.execute("DELETE FROM detection WHERE asset_id = ? AND source = 'auto' AND status = 'pending'", (aid,))
                self._insert(new)
                self.db.execute("UPDATE asset SET analyzed_at = ?, analyzer = ?, fail_count = 0, last_error = NULL "
                                "WHERE asset_id = ?", (time.time(), ANALYZER_ID, aid))
                self.db.commit()
            except Exception as e:
                self._failed(aid, e)
        self._timed("save", t0, len(items))
        # Débit réel (photos terminées par seconde), étapes en chaîne comprises.
        now = time.monotonic()
        if self._last_done and now - self._last_done < 600:
            self._speed += [(now - self._last_done) / len(items)] * len(items)
            del self._speed[:-50]
        self._last_done = now

    def _failed(self, aid: str, e: BaseException) -> None:
        """Photo en échec : comptée (abandonnée après MAX_FAILURES), ou masquée si elle a disparu d'Immich."""
        self.db.rollback()
        if is_missing(e):
            self.mark_missing(aid)
            return
        if is_transient(e):
            # Réseau, Immich ou GBIF momentanément indisponible (429 : trop de requêtes) : la photo n'y est pour
            # rien, elle n'est pas comptée en échec. L'analyse s'arrête et reprend au cycle suivant.
            raise Unavailable(f"{type(e).__name__}: {e}"[:300]) from e
        log.error("analyse impossible pour %s", aid, exc_info=e)
        self.db.execute("UPDATE asset SET fail_count = fail_count + 1, last_error = ? WHERE asset_id = ?",
                        (f"{type(e).__name__}: {e}"[:500], aid))
        self.db.commit()

    def apply_skips(self) -> int:
        """Relie les RAW à leur JPG, puis retire les détections automatiques encore en attente des photos
        écartées (RAW doublé, personnes si SKIP_PEOPLE), analysées avant que la règle ne s'applique.
        Les détections relues restent. Ces photos repassent « à analyser » : si la règle est levée
        (SKIP_PEOPLE désactivé, JPG supprimé), elles sont analysées."""
        store.link_raw_twins(self.db)
        skipped = f"SELECT asset_id FROM asset WHERE NOT ({SKIPPED_NOT})"
        args = self.s.skip_args
        n = self.db.execute(f"DELETE FROM detection WHERE source = 'auto' AND status = 'pending' AND asset_id IN ({skipped}) "
                            "AND asset_id IN (SELECT asset_id FROM asset WHERE kind IS NOT 'VIDEO')", args).rowcount
        if not self.s.full_image_fallback:   # image entière classée sans rien de détecté : retirée si le réglage l'exclut
            n += self.db.execute("DELETE FROM detection WHERE source = 'auto' AND status = 'pending' AND detector = 'image'").rowcount
        n += drop_unwanted(self.db, self.s)
        self.db.execute(f"UPDATE asset SET analyzed_at = NULL WHERE analyzed_at IS NOT NULL AND kind IS NOT 'VIDEO' "
                        f"AND asset_id IN ({skipped}) AND NOT EXISTS (SELECT 1 FROM detection d WHERE d.asset_id = asset.asset_id)", args)
        self.db.commit()
        if n:
            log.info("%d détection(s) en attente retirée(s) : photos écartées (personnes, RAW doublé d'un JPG), image "
                     "entière sans sujet détecté, groupe non cherché ou détection manuelle", n)
        return n

    def prune(self) -> int:
        """Filtres de candidats appliqués aux détections en attente, si les paramètres ont changé."""
        key = json.dumps([self.s.exclude_extinct, self.s.hide_absent])
        if store.get_state(self.db, "pruned_with") == key:
            return 0
        self._step("prune")
        n = prune_pending(self.db, self.tx, self.s, checkpoint=self._checkpoint)
        store.set_state(self.db, "pruned_with", key)
        return n

    def start_prune(self) -> bool:
        """prune() en arrière-plan (après un changement de paramètres) ; False si un cycle tourne déjà :
        il sera appliqué à la fin de ce cycle-là ou au suivant."""
        if not self.cycle_lock.acquire(blocking=False):
            return False
        def run():
            try:
                self._cycle_manual = False   # arrêté par un réglage : le prochain cycle refait prune()
                self.progress.update(running=True, started=time.time(), finished=None, error=None, manual=True)
                log.info("paramètres appliqués à %d détection(s) en attente", self.prune())
            except Interrupted:
                log.info("application des paramètres interrompue : reprise au prochain cycle")
            except Exception:
                self.db.rollback()   # pas de transaction laissée ouverte (voir run_once)
                log.exception("application des paramètres en échec")
            finally:
                self.progress.update(running=False, step=None, finished=time.time())
                self.cycle_lock.release()
        threading.Thread(target=run, daemon=True).start()
        return True

    def reanalyze(self, asset_id: str) -> int:
        """Ré-analyse immédiate d'une photo. Comme une remise en file : les détections relues ou dessinées restent,
        seules les propositions à relire sont remplacées. Rien n'est effacé tant que l'analyse n'a pas abouti."""
        if not self.cycle_lock.acquire(timeout=5):
            raise Busy("un cycle de synchronisation est en cours")
        self._cycle_manual = False
        try:
            r = self.db.execute("SELECT lat, lon FROM asset WHERE asset_id = ?", (asset_id,)).fetchone()
            if not r:
                raise KeyError(asset_id)
            results = self._analyze_asset(asset_id, r["lat"], r["lon"])
            rows = self._prepare(asset_id, results, auto_threshold(self.db, self.s))
            try:
                self.db.execute("DELETE FROM detection WHERE asset_id = ? AND status = 'pending' AND source != 'manual'", (asset_id,))
                n = self._insert(rows)
                self.db.execute("UPDATE asset SET analyzed_at = ?, analyzer = ?, fail_count = 0, last_error = NULL "
                                "WHERE asset_id = ?", (time.time(), ANALYZER_ID, asset_id))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            return n
        finally:
            self.cycle_lock.release()

    def requeue(self, scope: str) -> int:
        return requeue(self.db, scope)

    def undo_summary(self, db) -> dict:
        """Ce que le retrait toucherait (traces en base, sans appeler Immich) et où il en est."""
        q = lambda sql: db.execute(sql).fetchone()[0]
        return {"photos": q("SELECT count(DISTINCT asset_id) FROM written WHERE kind IN ('tag_asset', 'description')"),
                "tags": q("SELECT count(*) FROM written WHERE kind = 'tag'"),
                "descriptions": q("SELECT count(*) FROM written WHERE kind = 'description'"),
                "paused": self.writes_paused(db), "dry_run": self.s.dry_run, "source": self.s.source, **self.undo}

    def writes_paused(self, db=None) -> bool:
        return store.get_state(db or self.db, store.WRITES_PAUSED) == "1"

    def resume_writes(self) -> None:
        with self._idle():
            store.set_state(self.db, store.WRITES_PAUSED, "0")
            self.db.commit()
        log.info("écritures reprises : les photos validées seront réécrites aux prochains cycles")

    def start_uninstall(self, total: int) -> None:
        """Retrait en arrière-plan : le cycle en cours est arrêté d'abord (_idle), une seule écriture à la fois."""
        self.undo = {"state": "running", "done": 0, "total": total, "stats": None, "error": None, "remaining": 0}

        def tick():
            self._checkpoint()
            self.undo["done"] = min(self.undo["done"] + 1, total)
            self.last_activity = time.time()

        def run():
            log.warning("retrait demandé : %d photo(s) à nettoyer dans Immich", total)
            try:
                with self._idle():
                    syncer = Syncer(self.db, self.client, self.s.tag_root, self.s.names_lang, self.s.write_description,
                                    write_gap=getattr(self.client, "write_gap", WRITE_GAP), checkpoint=tick)
                    stats = syncer.uninstall()
                self.undo.update(state="done", stats=asdict(stats), remaining=syncer.remaining)
                log.warning("retrait terminé : %s, photos restantes : %d", stats, syncer.remaining)
            except Interrupted:
                self.undo.update(state="interrupted")
                log.warning("retrait interrompu : le relancer reprend où il en était")
            except Exception as e:
                self.undo.update(state="failed", error=str(e)[:300])
                log.exception("retrait en échec : le relancer reprend où il en était")

        threading.Thread(target=run, daemon=True, name="retrait").start()

    def sync(self):
        if self.writes_paused():
            log.info("écritures suspendues après un retrait : rien n'est écrit")
            return None
        if not self.client.writable:
            log.warning("serveur Immich non validé : synchronisation suspendue (lecture seule)")
            return None
        last_full = float(store.get_state(self.db, "last_full_sync") or 0)
        full = time.time() - last_full >= DAILY
        syncer = Syncer(self.db, self.client, self.s.tag_root, self.s.names_lang, self.s.write_description,
                        write_gap=getattr(self.client, "write_gap", WRITE_GAP), checkpoint=self._checkpoint)
        try:
            stats = syncer.sync_all(full=full)
        finally:   # même après un arrêt (Interrupted)
            for aid in syncer.missing:
                self.mark_missing(aid)
        if full and not self.client.dry_run:
            store.set_state(self.db, "last_full_sync", str(time.time()))
        return stats

    def ensure_quality(self, asset_id: str) -> float:
        """Qualité de l'image (couleurs, netteté), calculée une fois sur la vignette puis gardée en base."""
        row = self.db.execute("SELECT quality FROM image_quality WHERE asset_id = ?", (asset_id,)).fetchone()
        if row:
            return row["quality"]
        q = image_quality(Image.open(io.BytesIO(self.client.thumbnail(asset_id, "thumbnail"))))
        self.db.execute("INSERT OR REPLACE INTO image_quality VALUES (?, ?)", (asset_id, q))
        self.db.commit()
        return q

    def prefetch(self, candidates: int = 3, limit: int = 400) -> int:
        """Met en local tout ce que le dashboard affiche : fiche du taxon (noms, liens), aire de répartition
        et article Wikipédia, pour les taxons validés et les premiers candidats des détections à relire,
        et qualité des photos validées (choix des photos de couverture).
        Ainsi la visionneuse, les pages espèce et les cartes n'attendent plus le réseau."""
        wanted: dict[int, str] = {}
        for r in self.db.execute("SELECT DISTINCT taxon_key FROM detection WHERE status = 'confirmed' AND taxon_key IS NOT NULL"):
            t = store.load_taxon(self.db, r["taxon_key"])
            wanted[r["taxon_key"]] = t["scientific_name"] if t else ""
        for r in self.db.execute("SELECT candidates FROM detection WHERE status = 'pending'"):
            for c in json.loads(r["candidates"] or "[]")[:candidates]:
                if c.get("gbif_key"):
                    wanted.setdefault(c["gbif_key"], c.get("name", ""))
        todo = [(k, n) for k, n in wanted.items()
                if ranges.cached(self.db, k) is None or not (store.load_taxon(self.db, k) or {}).get("v", 0) >= TAXON_VERSION][:limit]
        no_quality = [r["asset_id"] for r in self.db.execute(
            "SELECT DISTINCT d.asset_id FROM detection d JOIN asset a ON a.asset_id = d.asset_id "
            "LEFT JOIN image_quality q ON q.asset_id = d.asset_id "
            "WHERE d.status = 'confirmed' AND q.asset_id IS NULL AND a.missing_at IS NULL LIMIT ?", (limit,))]
        self._step("prefetch", len(todo) + len(no_quality))
        for key, name in todo:
            self._tick()
            try:
                t = store.load_taxon(self.db, key)
                if not t or t.get("v", 0) < TAXON_VERSION:
                    m = self.tx.match(name) if name else None
                    if m and m.gbif_key == key:
                        store.save_taxon(self.db, key, to_json(m))
                        self.db.commit()
                        t = store.load_taxon(self.db, key)
                if ranges.cached(self.db, key) is None:
                    ranges.fetch(self.tx, key)
                url = (t or {}).get("wikipedia", {}).get(self.s.names_lang)
                if url:
                    wiki.article(self.tx, url)   # mis en cache (7 jours)
            except httpx.TransportError as e:   # réseau coupé : une ligne, et le reste attend le prochain cycle
                log.warning("données locales incomplètes pour %s, reprise au prochain cycle : %s", key, str(e) or type(e).__name__)
                break
            except httpx.HTTPError as e:
                log.warning("données locales incomplètes pour %s : %s", key, e)
            except Exception:
                log.warning("données locales incomplètes pour %s", key, exc_info=True)
        for aid in no_quality:
            self._tick()
            try:
                self.ensure_quality(aid)
            except Exception:
                log.debug("qualité indisponible pour %s", aid, exc_info=True)
        return len(todo)

    def run_once(self, manual: bool = False) -> dict:
        """Un cycle complet. Manuel (bouton « Synchroniser ») : analyse aussi hors de la fenêtre horaire.
        L'analyse enchaîne les lots jusqu'à vider la file, dans la limite de ANALYSIS_MAX_MINUTES ; s'il en
        reste, report['more'] le signale et la boucle relance un cycle sans attendre."""
        if not self.configured:
            return {"configured": False}
        if not self.cycle_lock.acquire(blocking=False):
            return {"busy": True}
        self._cycle_manual = manual
        self.progress.update(running=True, started=time.time(), finished=None, error=None, manual=manual)
        report: dict = {"at": time.time(), "manual": manual, "dry_run": self.s.dry_run}
        try:
            self._step("check")
            report["writable"] = self.check()
            self._step("scan")
            report["scanned"] = self.scan()
            report["reconciled"] = self.reconcile()
            report["skipped"] = self.apply_skips()
            self._soft(report, "taxa_refreshed", self.refresh_taxa)
            self._soft(report, "places_refreshed", self.refresh_places)
            report["analyzed"] = 0
            deadline = time.monotonic() + self.s.analysis_max_minutes * 60
            analyzing = lambda: (manual or in_window(self.s.analysis_window)) and not self.analysis_paused and self.pending_analysis()
            if analyzing():
                if self._analyzer is None:
                    # Premier lancement : modèles téléchargés (~5 Go) puis chargés, plusieurs minutes possibles.
                    self._step("models")
                    self._wait_models()
                self._step("analyze", self.pending_analysis())
            while analyzing():
                if time.monotonic() >= deadline:
                    report["more"] = True
                    break
                try:
                    report["analyzed"] += self.analyze()
                except Unavailable as e:
                    log.warning("analyse suspendue jusqu'au prochain cycle : %s", e)
                    report["paused"] = str(e)
                    break
            if self.analysis_paused:
                # Pause en plein groupe : le groupe gardait les modèles, la mémoire du GPU n'est rendue qu'ici.
                self.unload_models()
            self._soft(report, "pruned", self.prune)
            # Séries (rafales) et lissage des propositions à relire d'une même série (series.py).
            report["series"] = series.assign(self.db)
            report["smoothed"] = series.smooth(self.db, lambda k: store.load_taxon(self.db, k))
            self._step("sync")
            self._soft(report, "sync", lambda: asdict(stats) if (stats := self.sync()) else None)
            self._soft(report, "prefetched", self.prefetch)
            try:
                self.backup()
            except Exception:   # disque plein, droits : signalé, sans bloquer le cycle
                log.exception("sauvegarde de la base impossible")
            report["failed"] = self.db.execute("SELECT count(*) FROM asset WHERE fail_count >= ? AND missing_at IS NULL",
                                               (MAX_FAILURES,)).fetchone()[0]
            store.set_state(self.db, "last_run", json.dumps(report))
            if report.get("errors"):
                self.progress["error"] = " ; ".join(f"{k} : {v}" for k, v in report["errors"].items())
            return report
        except Interrupted:
            # Arrêt demandé par un réglage du dashboard (_idle) : rien d'écrit à moitié n'est gardé, le cycle repart
            # juste après le changement.
            self.db.rollback()
            report["interrupted"] = True
            log.info("cycle arrêté pour appliquer un réglage du dashboard")
            return report
        except Exception as e:
            # Écritures du cycle pas encore validées (scan coupé par une panne d'Immich…) : annulées, sinon la
            # transaction resterait ouverte et bloquerait les écritures du dashboard jusqu'au cycle suivant.
            self.db.rollback()
            self.progress["error"] = str(e)
            raise
        finally:
            self.progress.update(running=False, step=None, finished=time.time(), report=report)
            self.cycle_lock.release()

    @property
    def models_loaded(self) -> bool:
        return self._analyzer is not None

    def _soft(self, report: dict, key: str, step) -> None:
        """Étape du cycle dont la panne (GBIF, Immich) ne prive pas le cycle des suivantes : signalée dans le rapport,
        retentée au prochain cycle. Seules ses écritures à elle sont annulées, le travail déjà fait est validé avant."""
        self.db.commit()
        try:
            report[key] = step()
        except Exception as e:
            self.db.rollback()
            log.exception("étape %s du cycle en échec", key)
            report.setdefault("errors", {})[key] = str(e)

    def _wait_models(self) -> None:
        """Chargement des modèles (téléchargement compris) dans un fil à part, que le cycle attend en restant
        interruptible : un réglage du dashboard n'attend pas la fin d'un téléchargement de plusieurs Go. Un arrêt
        n'interrompt pas le chargement, repris tel quel au cycle suivant."""
        if self._loading is None or not self._loading.is_alive():
            self._load_error = None

            def load():
                try:
                    self.analyzer
                    if self.analysis_paused:   # pause pendant le chargement : rien ne garde les modèles en mémoire
                        self.unload_models()
                except BaseException as e:
                    self._load_error = e

            self._loading = threading.Thread(target=load, daemon=True, name="modeles")
            self._loading.start()
        while self._loading.is_alive() and not self.analysis_paused:   # pause : le reste du cycle n'attend pas
            self._checkpoint()
            self._loading.join(1)
        if self._load_error is not None:
            raise self._load_error

    def pause_analysis(self, paused: bool) -> None:
        """Bouton pause/reprise de l'analyse. Pause : le groupe de photos en cours finit, puis les modèles quittent
        la mémoire (GPU comme processeur). Reprise : l'analyse repart aussitôt, même hors de la fenêtre horaire,
        comme avec « Synchroniser ». Le reste du cycle (nouvelles photos, tags) continue pendant la pause."""
        self.analysis_paused = paused
        if paused:
            self.unload_models()
        elif not self.start_manual():   # cycle en cours : la boucle en relance un, manuel, dès qu'il finit
            self._resume_manual = self._resume_pending = True
            self._wake.set()

    def unload_models(self) -> None:
        with self._analyzer_lock:
            if self._analyzer is None or self._device is None:   # analyseur fourni de l'extérieur (tests)
                return
            device, self._stages_kept = self._device, {**self._stages_kept, **getattr(self._analyzer, "timings", {})}
            self._analyzer = None
        hardware.release(device)
        log.info("analyse en pause : modèles retirés de la mémoire")

    def start_manual(self) -> bool:
        """Lance un cycle complet en arrière-plan ; False si un cycle tourne déjà."""
        if self.cycle_lock.locked():
            return False
        def run():
            try:
                log.info("synchronisation manuelle : %s", self.run_once(manual=True))
            except Exception:
                log.exception("synchronisation manuelle en échec")
        threading.Thread(target=run, daemon=True, name="sync-manuelle").start()
        return True

    def install_backbone(self) -> None:
        """Taxonomie GBIF locale : téléchargée et construite si elle manque ou ne correspond plus à la version de
        l'API (en tâche de fond ; en attendant, l'API sert comme avant)."""
        if not self.s.gbif_local:
            return
        try:
            backbone.install(self.s.data_dir, self.s.user_agent, lambda state, version: self.backbone_state.update(state=state, version=version))
        except Exception as e:
            self.backbone_state.update(state="error", error=str(e)[:200])
            log.exception("taxonomie GBIF locale indisponible, l'API GBIF reste utilisée")

    def loop(self) -> None:
        self.loop_started = True
        threading.Thread(target=self.install_backbone, daemon=True, name="taxonomie-gbif").start()
        threading.Thread(target=self._watch_idle_models, daemon=True, name="modeles-inactifs").start()
        while True:
            self.last_activity = time.time()
            more = False
            self._wake.clear()   # avant le cycle : un réveil demandé pendant celui-ci relance aussitôt le suivant
            try:
                manual, self._resume_manual = self._resume_manual, False
                resume, self._resume_pending = self._resume_pending, False
                report = self.run_once(manual=manual)
                # Reprise après un réglage, mais un autre réglage occupe encore le cycle : on réessaie vite, en
                # gardant le mode manuel.
                retry = bool(report.get("busy")) and resume
                if retry:
                    self._resume_manual, self._resume_pending = self._resume_manual or manual, True
                # Pas encore de connexion à Immich : on revérifie vite, pour démarrer dès qu'elle est saisie.
                more = bool(report.get("more")) or report.get("configured") is False or retry
                if report.get("configured") is not False and not report.get("busy"):
                    log.info("cycle : %s", report)
            except Exception:
                log.exception("cycle en échec")
            # Attente interrompue par _idle : cycle arrêté pour un réglage, relancé aussitôt.
            self._wake.wait(5 if more else self.s.interval_minutes * 60)
