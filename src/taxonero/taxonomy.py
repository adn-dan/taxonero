"""Enrichissement taxonomique : GBIF (lignée, noms vernaculaires, UICN, occurrences)
et Wikidata (lien Wikipédia). Toutes les réponses sont mises en cache dans SQLite.
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from .backbone import Backbone

log = logging.getLogger(__name__)
GBIF = "https://api.gbif.org/v1"
WIKIDATA = "https://www.wikidata.org/w/api.php"
RANKS = ("kingdom", "phylum", "class", "order", "family", "genus", "species")


TAXON_VERSION = 2
# Règle des noms des rangs supérieurs (GBIF d'abord, au pluriel) : les taxons en cache plus anciens sont
# corrigés en tâche de fond (Worker.refresh_taxa), une requête par taxon, sans tout ré-enrichir.
NAMES_VERSION = 1
# Recherche des articles par nom et par synonymes (Taxonomy._wikidata) : les taxons en cache sans article sont repris
# en tâche de fond (Worker.refresh_taxa) ou à l'ouverture du panneau Wikipédia.
WIKI_VERSION = 1
SYNONYMS_TRIED = 10   # synonymes GBIF essayés pour trouver un article (une requête Wikidata chacun, en cache)
HIGHER = lambda rank: rank not in ("species", "subspecies", "variety", "form")


@dataclass
class Taxon:
    gbif_key: int
    scientific_name: str
    rank: str
    lineage: dict[str, str] = field(default_factory=dict)
    vernacular: dict[str, str] = field(default_factory=dict)
    iucn: str | None = None
    wikidata: str | None = None
    wikipedia: dict[str, str] = field(default_factory=dict)
    wikispecies: str | None = None
    authority: str | None = None       # ex. "(Linnaeus, 1758)"
    v: int = 0                         # version du format (ré-enrichissement si < TAXON_VERSION)
    nv: int = NAMES_VERSION            # version de la règle des noms (voir NAMES_VERSION)
    wv: int = WIKI_VERSION             # version de la recherche des articles (voir WIKI_VERSION)


# Langues des noms communs par défaut (codes Wikipédia / ISO 639-1).
DEFAULT_LANGS = ("fr", "en", "de", "es", "it", "nl", "pt", "ca", "br", "eu", "oc", "pl", "sv", "ja", "zh")
ISO3 = {"fr": "fra", "en": "eng", "de": "deu", "es": "spa", "it": "ita", "nl": "nld", "pt": "por", "ca": "cat",
        "br": "bre", "eu": "eus", "oc": "oci", "pl": "pol", "sv": "swe", "ja": "jpn", "zh": "zho", "ru": "rus",
        "da": "dan", "no": "nor", "fi": "fin", "cs": "ces"}


OUTAGE_S = 60   # s sans interroger un service constaté en panne (Taxonomy.fail_fast)


class Taxonomy:
    def __init__(self, db: sqlite3.Connection, langs: tuple[str, ...] = DEFAULT_LANGS, user_agent: str = "taxonero/0.1 (https://github.com/adn-dan/taxonero)",
                 backbone: Path | None = None, fail_fast: bool = False):
        self.db = db
        # Actions manuelles (dashboard) : pas de longues relances quand un service est en panne, quelqu'un attend.
        # Une panne constatée évite d'interroger ce service pendant OUTAGE_S.
        self.fail_fast = fail_fast
        self._down: dict[str, float] = {}
        # Copie locale de la taxonomie GBIF (backbone.py) : noms, fiches, noms communs et UICN sans réseau. Ouverte
        # dès que le fichier apparaît (construit en tâche de fond), rouverte s'il est remplacé.
        self.backbone_path = backbone
        self._bb: Backbone | None = None
        self._bb_stamp = None
        self._bb_checked = 0.0
        self.langs = langs
        self.http = httpx.Client(timeout=10 if fail_fast else 30, headers={"User-Agent": user_agent})
        self.min_interval = 0.2  # s entre deux départs de requête réseau (les réponses en cache ne comptent pas)
        self._last_request = 0.0
        self._rate = threading.Lock()
        # Requêtes simultanées de prefetch : GBIF met souvent près d'une seconde à répondre, le débit reste
        # plafonné par min_interval.
        self.workers = 4
        db.execute("CREATE TABLE IF NOT EXISTS http_cache (url TEXT PRIMARY KEY, body TEXT, fetched REAL)")

    @property
    def local(self) -> Backbone | None:
        if self.backbone_path and time.monotonic() - self._bb_checked > 60:
            self._bb_checked = time.monotonic()
            try:
                st = os.stat(self.backbone_path)
                stamp = (st.st_ino, st.st_mtime)
            except OSError:
                stamp = None
            if stamp != self._bb_stamp:
                self._bb_stamp, self._bb = stamp, Backbone(self.backbone_path) if stamp else None
                if self._bb:
                    log.info("taxonomie GBIF locale utilisée : %s", self.backbone_path)
        return self._bb

    _SPECIES = re.compile(rf"{re.escape(GBIF)}/species/(\d+)")

    def _local(self, url: str, params: dict | None) -> dict | None:
        """Réponse de la taxonomie locale, au format de l'API ; None si elle ne sait pas (l'API prend le relais)."""
        bb = self.local
        if bb is None:
            return None
        if url == f"{GBIF}/species/match":
            return bb.match(params["name"], params.get("kingdom"), params.get("rank"))
        m = self._SPECIES.fullmatch(url)   # fiche seulement : noms communs et UICN restent à l'API (backbone.py)
        return bb.species(int(m[1])) if m else None

    def get(self, url: str, params: dict | None = None, ttl: float = 30 * 86400) -> dict:
        if (local := self._local(url, params)) is not None:
            return local
        key = str(httpx.URL(url, params=params))
        row = self.db.execute("SELECT body, fetched FROM http_cache WHERE url = ?", (key,)).fetchone()
        if row and time.time() - row[1] < ttl:
            return json.loads(row[0])
        body = self._fetch(key)
        self.db.execute("INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?)", (key, json.dumps(body), time.time()))
        self.db.commit()
        return body

    def _throttle(self) -> None:
        """Attend son tour : au plus une requête qui part toutes les min_interval s, tous threads confondus."""
        with self._rate:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _fetch(self, url: str, retries: int = 6) -> dict:
        """GET avec débit plafonné et relance progressive (GBIF répond 429 au-delà de quelques req/s)."""
        service = "occurrence" if "/occurrence/" in url else httpx.URL(url).host
        if self.fail_fast and time.monotonic() < self._down.get(service, 0):
            raise httpx.HTTPError(f"{service} : en panne, pas de nouvel essai pour l'instant")
        for attempt in range(retries):
            self._throttle()
            try:
                r = self.http.get(url)
            except httpx.TransportError:
                if self.fail_fast:
                    self._down[service] = time.monotonic() + OUTAGE_S
                raise
            if self.fail_fast and r.status_code in (500, 502, 503, 504):
                self._down[service] = time.monotonic() + OUTAGE_S
                log.warning("%s répond %d : pas de nouvel essai", service, r.status_code)
                break
            if r.status_code in (429, 500, 502, 503, 504):
                delay = float(r.headers.get("Retry-After") or 2 ** attempt)
                # Relance normale (débit plafonné de GBIF) : un échec définitif remonte à l'appelant, qui le signale.
                log.info("%s répond %d, nouvel essai dans %.0f s", httpx.URL(url).host, r.status_code, min(delay, 60))
                time.sleep(min(delay, 60))
                continue
            r.raise_for_status()
            # Certains endpoints GBIF (ex. iucnRedListCategory) répondent un corps vide sans donnée.
            body = r.json() if r.content.strip() else {}
            # Wikidata signale une surcharge par un code 200 et un corps {"error": …} : à ne pas mettre en cache,
            # le taxon resterait sans article pendant 30 jours.
            if isinstance(body, dict) and "error" in body and httpx.URL(url).host == httpx.URL(WIKIDATA).host:
                error = body["error"].get("code") if isinstance(body["error"], dict) else body["error"]
                log.warning("Wikidata répond %s, nouvel essai dans %d s", error, 2 ** attempt)
                time.sleep(2 ** attempt)
                continue
            return body
        if r.is_success:
            raise httpx.HTTPError(f"Wikidata : {error}")
        return r.raise_for_status().json()

    def fetch_bytes(self, url: str, params: list | dict | None = None, retries: int = 6) -> bytes:
        """GET binaire (tuiles vectorielles) avec le même plafond de débit et les mêmes relances."""
        for attempt in range(retries):
            self._throttle()
            r = self.http.get(url, params=params)
            if self.fail_fast and r.status_code in (500, 502, 503, 504):
                break
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(float(r.headers.get("Retry-After") or 2 ** attempt), 60))
                continue
            if r.status_code == 204:
                return b""
            return r.raise_for_status().content
        return r.raise_for_status().content

    # -- GBIF ----------------------------------------------------------------

    # -- requêtes en parallèle -------------------------------------------------

    @staticmethod
    def _match_req(name: str, kingdom: str | None = None, rank: str | None = None) -> tuple:
        params = {"name": name, "strict": "false"}
        if kingdom:
            params["kingdom"] = kingdom
        if rank:
            params["rank"] = rank.upper()
        return f"{GBIF}/species/match", params, 30 * 86400

    @staticmethod
    def _near_req(gbif_key: int, lat: float, lon: float, radius_deg: float) -> tuple:
        # Centre aligné sur une grille de 1° pour que les photos d'un même secteur partagent le cache.
        lat, lon = float(round(lat)), float(round(lon))
        # Observations de terrain uniquement : exclut zoos, herbiers et collections de musée.
        q = {"taxonKey": gbif_key, "limit": 0, "occurrenceStatus": "PRESENT",
             "basisOfRecord": ["HUMAN_OBSERVATION", "MACHINE_OBSERVATION", "OBSERVATION"],
             "decimalLatitude": f"{lat - radius_deg:.2f},{lat + radius_deg:.2f}",
             "decimalLongitude": f"{lon - radius_deg:.2f},{lon + radius_deg:.2f}"}
        return f"{GBIF}/occurrence/search", q, 180 * 86400

    @staticmethod
    def _fossil_req(gbif_key: int, field: bool) -> tuple:
        basis = ["HUMAN_OBSERVATION", "MACHINE_OBSERVATION", "OBSERVATION"] if field else "FOSSIL_SPECIMEN"
        return f"{GBIF}/occurrence/search", {"taxonKey": gbif_key, "limit": 0, "basisOfRecord": basis}, 365 * 86400

    def _cached(self, req: tuple) -> dict | None:
        url, params, ttl = req
        if (local := self._local(url, params)) is not None:
            return local
        row = self.db.execute("SELECT body, fetched FROM http_cache WHERE url = ?", (str(httpx.URL(url, params=params)),)).fetchone()
        return json.loads(row[0]) if row and time.time() - row[1] < ttl else None

    def _prefetch(self, reqs: list[tuple]) -> None:
        """Met en cache, à plusieurs en même temps, les réponses manquantes. Seul le réseau passe dans les
        threads ; la base n'est touchée que d'ici. Une requête en échec est simplement laissée : le chemin
        normal (_get) la refera et signalera l'erreur."""
        missing = list(dict.fromkeys(str(httpx.URL(u, params=p)) for u, p, ttl in reqs if self._cached((u, p, ttl)) is None))
        if len(missing) < 2:
            return
        def get(key):
            try:
                return self._fetch(key)
            except Exception:
                return None
        with ThreadPoolExecutor(min(self.workers, len(missing))) as pool:
            bodies = list(pool.map(get, missing))
        now = time.time()
        self.db.executemany("INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?)",
                            [(k, json.dumps(b), now) for k, b in zip(missing, bodies) if b is not None])
        self.db.commit()

    def prefetch_candidates(self, cands: list[tuple[str, str | None]], lat: float | None, lon: float | None,
                            fossils: bool = False) -> None:
        """Prépare en parallèle tout ce que rerank va demander pour ces candidats (nom, kingdom) : correspondance,
        fiche, présence autour du point, test des fossiles. Mêmes requêtes que le chemin séquentiel, qui
        les trouve ensuite en cache."""
        self._prefetch([self._match_req(n, k) for n, k in cands])
        keys = []
        for n, k in cands:
            m = self._cached(self._match_req(n, k)) or {}
            if m.get("matchType") not in (None, "NONE") and "usageKey" in m:
                keys.append(m.get("acceptedUsageKey") or m["usageKey"])
        keys = list(dict.fromkeys(keys))
        geo = lat is not None and lon is not None
        self._prefetch([(f"{GBIF}/species/{k}", None, 30 * 86400) for k in keys]
                       + ([self._near_req(k, lat, lon, 5.0) for k in keys] if geo else [])
                       + ([self._fossil_req(k, False) for k in keys] if fossils else []))
        self._prefetch(([self._near_req(k, lat, lon, 1.0) for k in keys
                         if geo and (self._cached(self._near_req(k, lat, lon, 5.0)) or {}).get("count")] )
                       + [self._fossil_req(k, True) for k in keys
                          if fossils and (self._cached(self._fossil_req(k, False)) or {}).get("count")])

    def _wd_search_req(self, key: int) -> tuple:
        return WIKIDATA, {"action": "query", "list": "search", "srsearch": f"haswbstatement:P846={key}",
                          "format": "json", "srlimit": 1}, 30 * 86400

    @staticmethod
    def _wd_name_req(name: str) -> tuple:
        return WIKIDATA, {"action": "query", "list": "search", "srsearch": f'haswbstatement:"P225={name}"',
                          "format": "json", "srlimit": 3}, 30 * 86400

    def _wd_entity_req(self, qid: str) -> tuple:
        return WIKIDATA, {"action": "wbgetentities", "ids": qid, "props": "labels|sitelinks/urls",
                          "languages": "|".join(self.langs), "format": "json"}, 30 * 86400

    def prefetch_enrich(self, keys: list[int]) -> None:
        """Prépare en parallèle l'enrichissement (match(enrich=True)) de ces taxons : noms communs, UICN, Wikidata."""
        keys = list(dict.fromkeys(k for k in keys if k))
        self._prefetch([r for k in keys for r in ((f"{GBIF}/species/{k}/vernacularNames", {"limit": 1000}, 30 * 86400),
                                                  (f"{GBIF}/species/{k}/iucnRedListCategory", None, 30 * 86400),
                                                  self._wd_search_req(k))])
        hits = [((self._cached(self._wd_search_req(k)) or {}).get("query", {}).get("search") or [{}])[0].get("title") for k in keys]
        self._prefetch([self._wd_entity_req(q) for q in hits if q])

    def match(self, name: str, kingdom: str | None = None, enrich: bool = True, rank: str | None = None) -> Taxon | None:
        """Correspondance GBIF (synonymes résolus vers le nom accepté). enrich=False : clé et lignée seulement.
        rank : rang attendu (homonymes : « Amphibia » est ambigu sans lui) ; une réponse d'un autre rang est
        rejetée (GBIF se rabat sinon sur un rang supérieur, le règne par exemple)."""
        url, params, ttl = self._match_req(name, kingdom, rank)
        m = self.get(url, params, ttl)
        if m.get("matchType") in (None, "NONE") or "usageKey" not in m:
            return None
        if rank and (m.get("rank") or "").lower() != rank:
            return None
        key = m.get("acceptedUsageKey") or m["usageKey"]
        sp = self.get(f"{GBIF}/species/{key}")
        t = Taxon(gbif_key=key, scientific_name=sp.get("canonicalName") or sp["scientificName"],
                  rank=sp.get("rank", "").lower(),
                  lineage={r: sp[r] for r in RANKS if sp.get(r)})
        if not enrich:
            return t
        t.authority = (sp.get("authorship") or "").strip() or None
        t.vernacular = self._vernacular(key)
        t.iucn = self.get(f"{GBIF}/species/{key}/iucnRedListCategory").get("category")
        self._wikidata(t)
        t.v = TAXON_VERSION
        return t

    def plural_names(self, data: dict) -> dict:
        """Taxon en cache d'un rang supérieur : noms GBIF (pluriel) à la place des libellés Wikidata (singulier)."""
        data["vernacular"] = {**(data.get("vernacular") or {}), **self._vernacular(data["gbif_key"])}
        data["nv"] = NAMES_VERSION
        return data

    def _vernacular(self, key: int) -> dict[str, str]:
        res = self.get(f"{GBIF}/species/{key}/vernacularNames", {"limit": 1000}).get("results", [])
        out: dict[str, str] = {}
        for lang in self.langs:
            names = [r for r in res if r.get("language") in (ISO3.get(lang), lang)]
            if not names:
                continue
            # Préfère un nom marqué « preferred », sinon le plus fréquent parmi les sources.
            pref = [r["vernacularName"] for r in names if r.get("preferred")]
            counts: dict[str, int] = {}
            for r in names:
                n = r["vernacularName"].strip().lower()
                counts[n] = counts.get(n, 0) + 1
            best = (pref[0] if pref else max(names, key=lambda r: counts[r["vernacularName"].strip().lower()])["vernacularName"]).strip()
            out[lang] = tidy_name(best)
        return out

    # -- Wikidata / Wikipédia / Wikispecies ----------------------------------

    def synonym_names(self, key: int, name: str = "", same_epithet: bool = False) -> list[str]:
        """Synonymes GBIF du taxon, au plus SYNONYMS_TRIED : d'abord ceux qui gardent l'épithète de name (Ardea modesta :
        Ardea alba modesta, pas Casmerodius egretta, plus large), puis les espèces. same_epithet : ceux-là seulement."""
        epithet = name.rsplit(" ", 1)[-1].lower()
        same = lambda r: (r.get("canonicalName") or "").rsplit(" ", 1)[-1].lower() == epithet
        res = self.get(f"{GBIF}/species/{key}/synonyms", {"limit": 50}).get("results", [])
        res = sorted((r for r in res if same(r) or not same_epithet), key=lambda r: (not same(r), r.get("rank") != "SPECIES"))
        return list(dict.fromkeys(r["canonicalName"] for r in res if r.get("canonicalName")))[:SYNONYMS_TRIED]

    @staticmethod
    def articles_stale(data: dict) -> bool:
        """Espèce en cache sans article, enregistrée avant la recherche par nom et synonymes."""
        return data.get("wv", 0) < WIKI_VERSION and not data.get("wikipedia") and not HIGHER(data.get("rank", "species"))

    def refresh_articles(self, data: dict) -> dict:
        """Refait la recherche Wikidata d'un taxon en cache (une ou quelques requêtes, sans le reste de la fiche)."""
        t = Taxon(data["gbif_key"], data["scientific_name"], data.get("rank", ""), vernacular=dict(data.get("vernacular") or {}))
        self._wikidata(t)
        return {**data, "wikidata": t.wikidata or data.get("wikidata"), "wikipedia": t.wikipedia, "wikispecies": t.wikispecies,
                "vernacular": t.vernacular, "wv": WIKI_VERSION}

    def _wd_entity(self, qid: str) -> dict:
        return self.get(*self._wd_entity_req(qid)).get("entities", {}).get(qid, {})

    def _wd_links(self, e: dict) -> dict[str, str]:
        links = e.get("sitelinks", {})
        return {lang: links[f"{lang}wiki"]["url"] for lang in self.langs if f"{lang}wiki" in links}

    def _wd_with_articles(self, t: Taxon) -> tuple[str, dict, str] | None:
        """Fiche Wikidata portant les articles quand celle de la clé GBIF n'en a pas. Wikidata garde souvent une fiche
        par nom : Ardea modesta (clé GBIF de l'espèce, aucun article) et Ardea alba modesta (son synonyme, les
        articles) ; ou une fiche sans la bonne clé GBIF. Cherche par nom scientifique exact, puis par synonymes GBIF,
        espèces d'abord. Un nom porté par plusieurs fiches avec articles (homonymes) est ignoré."""
        for name in dict.fromkeys([t.scientific_name, *self.synonym_names(t.gbif_key, t.scientific_name)]):
            hits = self.get(*self._wd_name_req(name)).get("query", {}).get("search", [])
            found = [(h["title"], e) for h in hits if self._wd_links(e := self._wd_entity(h["title"]))]
            if len(found) == 1:
                return (*found[0], name)
        return None

    def _wikidata(self, t: Taxon) -> None:
        """Entité Wikidata portant l'identifiant GBIF (P846) : libellés multilingues (prioritaires sur
        GBIF quand ce ne sont pas juste le nom latin), articles Wikipédia et page Wikispecies."""
        hits = self.get(*self._wd_search_req(t.gbif_key)).get("query", {}).get("search", [])
        qid = hits[0]["title"] if hits else None
        e = self._wd_entity(qid) if qid else {}
        other = None
        # Espèces et rangs inférieurs seulement : au-dessus, les homonymes entre règnes sont fréquents.
        if not self._wd_links(e) and not HIGHER(t.rank):
            other = self._wd_with_articles(t)
        source = other[1] if other else e
        t.wikidata = other[0] if other else qid
        t.wikipedia = self._wd_links(source)
        t.wikispecies = source.get("sitelinks", {}).get("specieswiki", {}).get("url")
        # Libellés Wikidata = titres d'articles, au singulier (« Oiseau », « Canidé »). Pour une espèce, ils priment ;
        # pour un rang supérieur, le nom GBIF garde la priorité : l'usage naturaliste nomme les groupes au pluriel
        # (« Oiseaux », « Canidés »), comme les groupes du dashboard. Wikidata ne fait alors que compléter.
        higher = HIGHER(t.rank)
        for lang, lab in e.get("labels", {}).items():
            value = lab["value"].strip()
            if value and value.lower() != t.scientific_name.lower() and not (higher and t.vernacular.get(lang)):
                t.vernacular[lang] = tidy_name(value)
        if other:   # la fiche aux articles complète les noms, sans reprendre son nom latin comme nom commun
            latin = {t.scientific_name.lower(), other[2].lower()}
            for lang, lab in other[1].get("labels", {}).items():
                value = lab["value"].strip()
                if value and value.lower() not in latin and not t.vernacular.get(lang):
                    t.vernacular[lang] = tidy_name(value)

    # -- Plausibilité géographique -------------------------------------------

    def occurrences_near(self, gbif_key: int, lat: float, lon: float, radius_deg: float = 1.0) -> int:
        """Nombre d'occurrences GBIF du taxon dans un carré de ±radius_deg autour du point.
        Le centre est aligné sur une grille de 1° pour que les photos d'un même secteur partagent le cache."""
        return int(self.get(*self._near_req(gbif_key, lat, lon, radius_deg)).get("count", 0))

    def fossil_only(self, gbif_key: int) -> bool:
        """Espèce connue surtout par des fossiles : plus de fossiles que 5 fois ses observations de terrain
        dans le monde (Equus leidyi, Canis gregarius...). Le drapeau « extinct » des profils GBIF n'est
        pas fiable (il marque par exemple la fouine) : on s'appuie sur les occurrences."""
        fossils = int(self.get(*self._fossil_req(gbif_key, False)).get("count", 0))
        if not fossils:
            return False
        return int(self.get(*self._fossil_req(gbif_key, True)).get("count", 0)) * 5 < fossils

    def geo(self, gbif_key: int, lat: float | None, lon: float | None) -> tuple[str, float]:
        """(badge, poids a priori) pour la présence du taxon autour du point.

        Paliers (présent à proximité / dans la région / quasi absent) multipliés par une légère
        pondération par la densité d'observations, qui sert surtout à départager. Mesuré sur le jeu
        de dev (dev/benchmark_pipeline.py) : une densité plus fortement pondérée favorise trop les
        espèces communes et dégrade le top-1.
        Sans GPS : ('unknown', 1.0) pour tous les candidats.
        """
        if lat is None or lon is None:
            return "unknown", 1.0
        region = self.occurrences_near(gbif_key, lat, lon, 5.0)
        # Le carré de ±1° est dans celui de ±5° (même centre) : rien dans la région, rien à proximité.
        near = self.occurrences_near(gbif_key, lat, lon, 1.0) if region else 0
        badge = "green" if near >= 3 else "amber" if region >= 10 else "red"
        tier = {"green": 1.0, "amber": 0.25, "red": 0.02}[badge]
        return badge, tier * (near + 0.05 * region + 0.5) ** 0.1


def tidy_name(value: str) -> str:
    """Nom commun propre : majuscule initiale ; un nom tout en capitales (GBIF donne « DOGS » pour Canidae)
    repasse en casse de phrase. Les sigles courts (≤ 3 lettres) sont laissés tels quels."""
    value = value.strip()
    letters = [c for c in value if c.isalpha()]
    if len(letters) > 3 and all(c.isupper() for c in letters):
        value = value.lower()
    return value[:1].upper() + value[1:]


def tidy_names(db: sqlite3.Connection) -> int:
    """Corrige les noms déjà en cache (une fois au démarrage) ; renvoie le nombre de taxons modifiés."""
    n = 0
    for key, data in db.execute("SELECT gbif_key, data FROM taxon").fetchall():
        d = json.loads(data)
        verna = d.get("vernacular") or {}
        fixed = {lang: tidy_name(v) for lang, v in verna.items() if v}
        if fixed != verna:
            d["vernacular"] = fixed
            db.execute("UPDATE taxon SET data = ? WHERE gbif_key = ?", (json.dumps(d, ensure_ascii=False), key))
            n += 1
    db.commit()
    return n


def to_json(t: Taxon) -> str:
    return json.dumps(asdict(t), ensure_ascii=False)
