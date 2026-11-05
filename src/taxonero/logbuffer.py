"""Journal de la page Paramètres (GET /api/logs).

Les lignes d'information restent en mémoire et s'effacent les unes les autres. Les avertissements et les erreurs ont
leur propre réserve, que les autres lignes ne chassent pas, recopiée dans data/ pour survivre aux redémarrages. Le
journal complet reste sur la sortie standard (docker logs). Les secrets connus (clé API, mot de passe) sont masqués
dès l'enregistrement de la ligne.
"""

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path

from .config import Settings

CAPACITY = 2000    # lignes d'information
KEEP = 2000        # avertissements et erreurs
MAX_CHARS = 8000   # une ligne : trace d'erreur comprise
MASK = "***"
MIN_SECRET = 4     # plus court, le masquage abîmerait le texte sans rien protéger
FILENAME = "journal-erreurs.jsonl"


class LogBuffer(logging.Handler):
    def __init__(self, capacity: int = CAPACITY, keep: int = KEEP) -> None:
        super().__init__(logging.INFO)
        self.setFormatter(logging.Formatter("%(message)s"))
        self.settings: Settings | None = None
        self.path: Path | None = None
        self.entries: deque[dict] = deque(maxlen=capacity)
        self.kept: deque[dict] = deque(maxlen=keep)
        self.appended = 0   # lignes ajoutées au fichier depuis qu'il a été réduit
        self.entries_lock = threading.Lock()
        # Numéros croissants d'un lancement à l'autre : la page ouverte reprend sans rien manquer après un redémarrage.
        self.last_id = int(time.time() * 1000)

    def bind(self, s: Settings) -> None:
        """Réglages (secrets à masquer) et dossier de données ; relit ce qui y a été conservé."""
        self.settings = s
        path = Path(s.data_dir) / FILENAME
        if path == self.path:
            return
        with self.entries_lock:
            self.path, self.appended = path, 0
            self.kept.clear()
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return
            for line in lines:   # la réserve ne garde que les dernières
                try:
                    self.kept.append(json.loads(line))
                except ValueError:   # dernière ligne coupée par un arrêt brutal
                    continue
            if self.kept:
                self.last_id = max(self.last_id, self.kept[-1]["id"])

    def _mask(self, text: str) -> str:
        s = self.settings
        for secret in (s.immich_api_key, s.dashboard_password) if s else ():
            if len(secret) >= MIN_SECRET:
                text = text.replace(secret, MASK)
        return text

    def _save(self, entry: dict) -> None:
        """Ajout au fichier ; réduit à ce qui est gardé quand il a doublé. Fichier lisible par son seul propriétaire."""
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.appended += 1
        if self.appended >= self.kept.maxlen:
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in self.kept)
            tmp.replace(self.path)
            self.appended = 0
            return
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self._mask(self.format(record))
            if len(text) > MAX_CHARS:
                text = text[:MAX_CHARS] + "…"
            with self.entries_lock:
                self.last_id += 1
                entry = {"id": self.last_id, "t": record.created, "level": record.levelname,
                         "name": record.name, "msg": text}
                if record.levelno >= logging.WARNING:
                    self.kept.append(entry)
                    try:
                        self._save(entry)
                    except OSError:   # disque plein, droits : la ligne reste en mémoire et sur la sortie standard
                        pass
                else:
                    self.entries.append(entry)
        except Exception:
            self.handleError(record)

    def clear(self) -> None:
        """Bouton « Effacer » : tout le journal, fichier compris. La numérotation continue (la page ouverte
        reprend après la dernière ligne effacée)."""
        with self.entries_lock:
            self.entries.clear()
            self.kept.clear()
            self.appended = 0
            if self.path:
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    pass

    def read(self, after: int = 0) -> list[dict]:
        with self.entries_lock:
            return sorted((e for e in (*self.entries, *self.kept) if e["id"] > after), key=lambda e: e["id"])


BUFFER = LogBuffer()


def install(s: Settings) -> None:
    """À appeler avant le démarrage du worker, pour garder aussi les premières lignes. Rappelable sans risque."""
    BUFFER.bind(s)
    root = logging.getLogger()
    if BUFFER not in root.handlers:
        root.addHandler(BUFFER)
