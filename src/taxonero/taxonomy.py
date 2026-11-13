"""Taxonomic enrichment: GBIF (lineage, vernacular names, IUCN, occurrences)
and Wikidata (Wikipedia link). All responses are cached in SQLite.
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
# Rule for the names of higher ranks (GBIF first, plural): older cached taxa are
# fixed in the background (Worker.refresh_taxa), one request per taxon, without enriching everything again.
NAMES_VERSION = 1
# Article search by name and by synonyms (Taxonomy._wikidata): cached taxa without an article are picked up
# in the background (Worker.refresh_taxa) or when the Wikipedia panel is opened.
WIKI_VERSION = 1
SYNONYMS_TRIED = 10   # GBIF synonyms tried to find an article (one Wikidata request each, cached)
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
    v: int = 0                         # format version (enriched again if < TAXON_VERSION)
    nv: int = NAMES_VERSION            # version of the naming rule (see NAMES_VERSION)
    wv: int = WIKI_VERSION             # version of the article search (see WIKI_VERSION)


# Default languages of common names (Wikipedia / ISO 639-1 codes).
DEFAULT_LANGS = ("fr", "en", "de", "es", "it", "nl", "pt", "ca", "br", "eu", "oc", "pl", "sv", "ja", "zh")
ISO3 = {"fr": "fra", "en": "eng", "de": "deu", "es": "spa", "it": "ita", "nl": "nld", "pt": "por", "ca": "cat",
        "br": "bre", "eu": "eus", "oc": "oci", "pl": "pol", "sv": "swe", "ja": "jpn", "zh": "zho", "ru": "rus",
        "da": "dan", "no": "nor", "fi": "fin", "cs": "ces"}


OUTAGE_S = 60   # s without querying a service found to be down (Taxonomy.fail_fast)
# Presence around a point, all taxa at once: one GBIF occurrence search per area with facets (number of
# observations per species, genus… key), instead of one request per candidate and per area. Same counts as a
# taxonKey search (checked on Parus major: 144,549 both ways). Rank of the taxon -> facet field of the response.
AREA_FACETS = {"SPECIES": "SPECIES_KEY", "GENUS": "GENUS_KEY", "FAMILY": "FAMILY_KEY", "ORDER": "ORDER_KEY",
               "CLASS": "CLASS_KEY", "PHYLUM": "PHYLUM_KEY", "KINGDOM": "KINGDOM_KEY"}
AREA_LIMIT = 500_000   # values per facet; a facet that reaches it is incomplete and is not used (per-taxon search)
AREA_TTL = 180 * 86400
# Grouped search refused for load (429, GBIF limits heavy searches when its servers are busy): per-taxon searches
# (same counts, small requests that still pass) for this long, then the grouped search is tried again.
GROUPED_COOLDOWN = 600
AREA_INTERVAL = 1.0   # s between two area searches: heavy for GBIF, which refuses them first (429) when they come in bursts
# Species with fossils worldwide (fossil test): one faceted search, ~160,000 species and ~5 MB, kept a year. A species
# absent from it has no fossil; for one present, its field observations around the photo usually settle the test.
FOSSILS_URL = (f"{GBIF}/occurrence/search?limit=0&basisOfRecord=FOSSIL_SPECIMEN&facet=speciesKey"
               "&facetLimit=1200000&facetMincount=1")   # 1,200,000: the most GBIF allows for one facet
FOSSILS_TTL = 365 * 86400
AREAS_IN_MEMORY = 8   # areas kept decoded (5 to 10 MB of memory each); photos come by date, often from the same area


class Taxonomy:
    def __init__(self, db: sqlite3.Connection, langs: tuple[str, ...] = DEFAULT_LANGS, user_agent: str = "taxonero/0.1 (https://github.com/adn-dan/taxonero)",
                 backbone: Path | None = None, fail_fast: bool = False):
        self.db = db
        # Manual actions (dashboard): no long retries when a service is down, someone is waiting.
        # An outage found avoids querying that service for OUTAGE_S.
        self.fail_fast = fail_fast
        self._down: dict[str, float] = {}
        # Local copy of the GBIF taxonomy (backbone.py): names, records, common names and IUCN without network. Opened
        # as soon as the file appears (built in the background), reopened if it is replaced.
        self.backbone_path = backbone
        self._bb: Backbone | None = None
        self._bb_stamp = None
        self._bb_checked = 0.0
        self.langs = langs
        self.http = httpx.Client(timeout=10 if fail_fast else 30, headers={"User-Agent": user_agent})
        self.min_interval = 0.2  # s between two network request starts (cached responses do not count)
        self._last_request = 0.0
        self._rate = threading.Lock()
        # Simultaneous prefetch requests: GBIF often takes nearly a second to answer, the throughput stays
        # capped by min_interval.
        self.workers = 4
        self._areas: dict[str, dict] = {}   # decoded areas (see _area), most recent last
        self._areas_lock = threading.Lock()   # the dashboard shares one Taxonomy between its threads
        self._area_rate = threading.Lock()    # area searches one at a time, AREA_INTERVAL apart
        self._last_area = 0.0
        self._refused: set[str] = set()   # area or fossil list refused by GBIF (4xx): per-taxon searches instead
        self._busy: dict[str, float] = {}   # area or fossil list refused for load (429): per-taxon until then
        self._fossils: dict[str, int] | None = None   # species with fossils (see _fossil_list)
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
                    log.info("local GBIF taxonomy in use: %s", self.backbone_path)
        return self._bb

    _SPECIES = re.compile(rf"{re.escape(GBIF)}/species/(\d+)")

    def _local(self, url: str, params: dict | None) -> dict | None:
        """Response from the local taxonomy, in the API format; None if it does not know (the API takes over)."""
        bb = self.local
        if bb is None:
            return None
        if url == f"{GBIF}/species/match":
            return bb.match(params["name"], params.get("kingdom"), params.get("rank"))
        m = self._SPECIES.fullmatch(url)   # record only: common names and IUCN stay with the API (backbone.py)
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
        """Wait for its turn: at most one request leaving every min_interval s, across all threads."""
        with self._rate:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _fetch(self, url: str, retries: int = 6) -> dict:
        """GET with a capped rate and progressive retries (GBIF answers 429 beyond a few req/s)."""
        service = "occurrence" if "/occurrence/" in url else httpx.URL(url).host
        if self.fail_fast and time.monotonic() < self._down.get(service, 0):
            raise httpx.HTTPError(f"{service}: down, no new attempt for now")
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
                log.warning("%s answers %d: no new attempt", service, r.status_code)
                break
            if r.status_code in (429, 500, 502, 503, 504):
                delay = float(r.headers.get("Retry-After") or 2 ** attempt)
                # Normal retry (GBIF rate cap): a final failure goes up to the caller, which reports it.
                log.info("%s answers %d, new attempt in %.0f s", httpx.URL(url).host, r.status_code, min(delay, 60))
                time.sleep(min(delay, 60))
                continue
            r.raise_for_status()
            # Some GBIF endpoints (e.g. iucnRedListCategory) answer an empty body without data.
            body = r.json() if r.content.strip() else {}
            # Wikidata reports an overload with a 200 code and an {"error": …} body: not to be cached,
            # the taxon would stay without an article for 30 days.
            if isinstance(body, dict) and "error" in body and httpx.URL(url).host == httpx.URL(WIKIDATA).host:
                error = body["error"].get("code") if isinstance(body["error"], dict) else body["error"]
                log.warning("Wikidata answers %s, new attempt in %d s", error, 2 ** attempt)
                time.sleep(2 ** attempt)
                continue
            return body
        if r.is_success:
            raise httpx.HTTPError(f"Wikidata: {error}")
        return r.raise_for_status().json()

    def fetch_bytes(self, url: str, params: list | dict | None = None, retries: int = 6) -> bytes:
        """Binary GET (vector tiles) with the same rate cap and the same retries."""
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

    # -- parallel requests -------------------------------------------------

    @staticmethod
    def _match_req(name: str, kingdom: str | None = None, rank: str | None = None) -> tuple:
        params = {"name": name, "strict": "false"}
        if kingdom:
            params["kingdom"] = kingdom
        if rank:
            params["rank"] = rank.upper()
        return f"{GBIF}/species/match", params, 30 * 86400

    @staticmethod
    def square(lat: float, lon: float, radius_deg: float) -> dict:
        """GBIF search parameters of the ±radius_deg square around a point. GBIF refuses a latitude beyond the poles
        or a longitude beyond ±180° (400): the square is cut at the poles, and one that crosses the antimeridian
        (Fiji, Chatham Islands, Aleutians) is asked as its two halves (repeated parameter: either one)."""
        south, north = max(lat - radius_deg, -90.0), min(lat + radius_deg, 90.0)
        west, east = lon - radius_deg, lon + radius_deg
        if west < -180:
            lons = [f"{west + 360:.2f},180.00", f"-180.00,{east:.2f}"]
        elif east > 180:
            lons = [f"{west:.2f},180.00", f"-180.00,{east - 360:.2f}"]
        else:
            lons = f"{west:.2f},{east:.2f}"
        return {"decimalLatitude": f"{south:.2f},{north:.2f}", "decimalLongitude": lons}

    @staticmethod
    def _near_req(gbif_key: int, lat: float, lon: float, radius_deg: float) -> tuple:
        # Centre aligned on a 1° grid so that photos of the same area share the cache.
        lat, lon = float(round(lat)), float(round(lon))
        # Field observations only: excludes zoos, herbaria and museum collections.
        q = {"taxonKey": gbif_key, "limit": 0, "occurrenceStatus": "PRESENT",
             "basisOfRecord": ["HUMAN_OBSERVATION", "MACHINE_OBSERVATION", "OBSERVATION"],
             **Taxonomy.square(lat, lon, radius_deg)}
        return f"{GBIF}/occurrence/search", q, 180 * 86400

    @staticmethod
    def _area_req(lat: float, lon: float, radius_deg: float) -> str:
        """Faceted search of an area: same square, same filters as _near_req, every taxon counted."""
        lat, lon = float(round(lat)), float(round(lon))
        q = {"limit": 0, "occurrenceStatus": "PRESENT",
             "basisOfRecord": ["HUMAN_OBSERVATION", "MACHINE_OBSERVATION", "OBSERVATION"],
             **Taxonomy.square(lat, lon, radius_deg),
             "facet": [f.lower().replace("_key", "Key") for f in AREA_FACETS.values()],
             "facetLimit": AREA_LIMIT, "facetMincount": 1}
        return str(httpx.URL(f"{GBIF}/occurrence/search", params=q))

    def _area(self, lat: float, lon: float, radius_deg: float, fetch: bool = True) -> dict | None:
        """{facet field: {key: observations}} for the area (cached AREA_TTL, compact form), None if unknown and
        fetch is False. A facet that reached AREA_LIMIT is left out: its counts would be incomplete."""
        key = self._area_req(lat, lon, radius_deg)
        with self._areas_lock:
            if key in self._areas:
                self._areas[key] = self._areas.pop(key)   # most recent last
                return self._areas[key]
        row = self.db.execute("SELECT body, fetched FROM http_cache WHERE url = ?", (key,)).fetchone()
        if row and time.time() - row[1] < AREA_TTL:
            return self._keep_area(key, json.loads(row[0]))
        if not fetch or "area" in self._refused:
            return None
        body = self._refusal_safe("area", key)
        return None if not body or "area" in self._refused else self._store_area(key, body)

    def _refusal_safe(self, what: str, url: str) -> dict:
        """GET of a grouped search (area, fossil list), one at a time and AREA_INTERVAL apart (heavy searches). A
        permanent refusal (4xx other than 429, for instance after a change of the GBIF API) must not make every photo
        fail: noted, and the per-taxon searches take over."""
        if time.monotonic() < self._busy.get(what, 0):
            return {}
        try:
            with self._area_rate:
                wait = AREA_INTERVAL - (time.monotonic() - self._last_area)
                if wait > 0:
                    time.sleep(wait)
                try:
                    return self._fetch(url, retries=2)
                finally:
                    self._last_area = time.monotonic()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:   # load: per-taxon searches for a while, then the grouped one again
                log.info("GBIF busy for the %s search: per-taxon searches for %d min", what, GROUPED_COOLDOWN // 60)
                self._busy[what] = time.monotonic() + GROUPED_COOLDOWN
                return {}
            if 400 <= e.response.status_code < 500:
                log.warning("GBIF refuses the %s search (%d): per-taxon searches instead", what, e.response.status_code)
                self._refused.add(what)
                return {}
            raise

    def _area_cached(self, key: str) -> bool:
        row = self.db.execute("SELECT fetched FROM http_cache WHERE url = ?", (key,)).fetchone()
        return key in self._areas or bool(row and time.time() - row[0] < AREA_TTL)

    def _store_area(self, key: str, body: dict) -> dict:
        """GBIF response -> compact form, cached in the database (call from the thread that owns it)."""
        area = {f["field"]: {c["name"]: c["count"] for c in f.get("counts", [])}
                for f in body.get("facets") or [] if len(f.get("counts", [])) < AREA_LIMIT}
        self.db.execute("INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?)", (key, json.dumps(area, separators=(",", ":")), time.time()))
        self.db.commit()
        return self._keep_area(key, area)

    def _keep_area(self, key: str, area: dict) -> dict:
        with self._areas_lock:
            self._areas[key] = area
            while len(self._areas) > AREAS_IN_MEMORY:
                del self._areas[next(iter(self._areas))]
        return area

    def _area_count(self, gbif_key: int, lat: float, lon: float, radius_deg: float, fetch: bool = True) -> int | None:
        """Observations of the taxon in the area from the faceted search; None if it cannot answer (rank without a
        facet, such as a subspecies, or incomplete facet): the per-taxon search is then used."""
        # Rank from the record, without network: local taxonomy, or cached by prefetch_candidates.
        rank = (self._cached((f"{GBIF}/species/{gbif_key}", None, 30 * 86400)) or {}).get("rank")
        field = AREA_FACETS.get(rank or "")
        area = self._area(lat, lon, radius_deg, fetch) if field else None
        if area is None or field not in area:
            return None
        return area[field].get(str(gbif_key), 0)

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
        """Cache the missing responses, several at the same time. Only the network goes through the
        threads; the database is only touched from here. A failed request is simply left: the normal
        path (_get) will make it again and report the error."""
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
        """Prepare in parallel everything rerank will ask for these candidates (name, kingdom): match,
        record, presence around the point, fossil test. Same requests as the sequential path, which
        then finds them in the cache."""
        self._prefetch([self._match_req(n, k) for n, k in cands])
        keys = []
        for n, k in cands:
            m = self._cached(self._match_req(n, k)) or {}
            if m.get("matchType") not in (None, "NONE") and "usageKey" in m:
                keys.append(m.get("acceptedUsageKey") or m["usageKey"])
        keys = list(dict.fromkeys(keys))
        geo = lat is not None and lon is not None
        self._prefetch([(f"{GBIF}/species/{k}", None, 30 * 86400) for k in keys])
        if fossils:   # world list of the species with fossils (once a year), per-taxon search for the other ranks
            try:
                self._fossil_list()
            except httpx.HTTPError:
                pass   # left to the normal path, which makes it again and reports the error
            self._prefetch([self._fossil_req(k, False) for k in keys if self._fossil_count(k, fetch=False) is None])
        # Presence: the two areas of the point (one request each, shared by every candidate and every photo of the
        # area), then the per-taxon search only for the taxa the areas cannot count.
        old = []
        if geo:
            if any(AREA_FACETS.get((self._cached((f"{GBIF}/species/{k}", None, 30 * 86400)) or {}).get("rank") or "") for k in keys):
                missing = [a for a in (self._area_req(lat, lon, r) for r in (5.0, 1.0)) if not self._area_cached(a)]

                for key in missing if "area" not in self._refused else []:   # one at a time (_refusal_safe)
                    try:
                        body = self._refusal_safe("area", key)
                    except Exception:   # left to the normal path, which makes it again and reports the error
                        continue
                    if body and "area" not in self._refused:
                        self._store_area(key, body)
            old = [k for k in keys if self._area_count(k, lat, lon, 5.0, fetch=False) is None]
            self._prefetch([self._near_req(k, lat, lon, 5.0) for k in old])
        def field_needed(k):   # fossils, and not enough field observations around the point to settle the test
            n = self._fossil_count(k, fetch=False)
            n = (self._cached(self._fossil_req(k, False)) or {}).get("count", 0) if n is None else n
            region = self._area_count(k, lat, lon, 5.0, fetch=False) if geo else None
            return n and (region is None or region * 5 < n)
        self._prefetch([self._near_req(k, lat, lon, 1.0) for k in old
                        if (self._cached(self._near_req(k, lat, lon, 5.0)) or {}).get("count")]
                       + ([self._fossil_req(k, True) for k in keys if field_needed(k)] if fossils else []))

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
        """Prepare in parallel the enrichment (match(enrich=True)) of these taxa: common names, IUCN, Wikidata."""
        keys = list(dict.fromkeys(k for k in keys if k))
        self._prefetch([r for k in keys for r in ((f"{GBIF}/species/{k}/vernacularNames", {"limit": 1000}, 30 * 86400),
                                                  (f"{GBIF}/species/{k}/iucnRedListCategory", None, 30 * 86400),
                                                  self._wd_search_req(k))])
        hits = [((self._cached(self._wd_search_req(k)) or {}).get("query", {}).get("search") or [{}])[0].get("title") for k in keys]
        self._prefetch([self._wd_entity_req(q) for q in hits if q])

    def match(self, name: str, kingdom: str | None = None, enrich: bool = True, rank: str | None = None) -> Taxon | None:
        """GBIF match (synonyms resolved to the accepted name). enrich=False: key and lineage only.
        rank: expected rank (homonyms: "Amphibia" is ambiguous without it); a response of another rank is
        rejected (otherwise GBIF falls back to a higher rank, the kingdom for instance)."""
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
        """Cached taxon of a higher rank: GBIF names (plural) instead of Wikidata labels (singular)."""
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
            # Prefer a name marked "preferred", otherwise the most frequent among the sources.
            pref = [r["vernacularName"] for r in names if r.get("preferred")]
            counts: dict[str, int] = {}
            for r in names:
                n = r["vernacularName"].strip().lower()
                counts[n] = counts.get(n, 0) + 1
            best = (pref[0] if pref else max(names, key=lambda r: counts[r["vernacularName"].strip().lower()])["vernacularName"]).strip()
            out[lang] = tidy_name(best)
        return out

    # -- Wikidata / Wikipedia / Wikispecies ----------------------------------

    def synonym_names(self, key: int, name: str = "", same_epithet: bool = False) -> list[str]:
        """GBIF synonyms of the taxon, at most SYNONYMS_TRIED: first those keeping the epithet of name (Ardea modesta:
        Ardea alba modesta, not Casmerodius egretta, broader), then the species. same_epithet: only those."""
        epithet = name.rsplit(" ", 1)[-1].lower()
        same = lambda r: (r.get("canonicalName") or "").rsplit(" ", 1)[-1].lower() == epithet
        res = self.get(f"{GBIF}/species/{key}/synonyms", {"limit": 50}).get("results", [])
        res = sorted((r for r in res if same(r) or not same_epithet), key=lambda r: (not same(r), r.get("rank") != "SPECIES"))
        return list(dict.fromkeys(r["canonicalName"] for r in res if r.get("canonicalName")))[:SYNONYMS_TRIED]

    @staticmethod
    def articles_stale(data: dict) -> bool:
        """Cached species without an article, saved before the search by name and synonyms."""
        return data.get("wv", 0) < WIKI_VERSION and not data.get("wikipedia") and not HIGHER(data.get("rank", "species"))

    def refresh_articles(self, data: dict) -> dict:
        """Run the Wikidata search of a cached taxon again (one or a few requests, without the rest of the record)."""
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
        """Wikidata item carrying the articles when the one of the GBIF key has none. Wikidata often keeps one item
        per name: Ardea modesta (GBIF key of the species, no article) and Ardea alba modesta (its synonym, the
        articles); or an item without the right GBIF key. Search by exact scientific name, then by GBIF synonyms,
        species first. A name carried by several items with articles (homonyms) is ignored."""
        for name in dict.fromkeys([t.scientific_name, *self.synonym_names(t.gbif_key, t.scientific_name)]):
            hits = self.get(*self._wd_name_req(name)).get("query", {}).get("search", [])
            found = [(h["title"], e) for h in hits if self._wd_links(e := self._wd_entity(h["title"]))]
            if len(found) == 1:
                return (*found[0], name)
        return None

    def _wikidata(self, t: Taxon) -> None:
        """Wikidata entity carrying the GBIF identifier (P846): multilingual labels (taking priority over
        GBIF when they are not just the Latin name), Wikipedia articles and Wikispecies page."""
        hits = self.get(*self._wd_search_req(t.gbif_key)).get("query", {}).get("search", [])
        qid = hits[0]["title"] if hits else None
        e = self._wd_entity(qid) if qid else {}
        other = None
        # Species and lower ranks only: above, homonyms across kingdoms are common.
        if not self._wd_links(e) and not HIGHER(t.rank):
            other = self._wd_with_articles(t)
        source = other[1] if other else e
        t.wikidata = other[0] if other else qid
        t.wikipedia = self._wd_links(source)
        t.wikispecies = source.get("sitelinks", {}).get("specieswiki", {}).get("url")
        # Wikidata labels = article titles, singular ("Oiseau", "Canidé"). For a species, they take priority;
        # for a higher rank, the GBIF name keeps priority: naturalist usage names groups in the plural
        # ("Oiseaux", "Canidés"), like the dashboard groups. Wikidata then only fills in.
        higher = HIGHER(t.rank)
        for lang, lab in e.get("labels", {}).items():
            value = lab["value"].strip()
            if value and value.lower() != t.scientific_name.lower() and not (higher and t.vernacular.get(lang)):
                t.vernacular[lang] = tidy_name(value)
        if other:   # the item with the articles fills in the names, without taking its Latin name as a common name
            latin = {t.scientific_name.lower(), other[2].lower()}
            for lang, lab in other[1].get("labels", {}).items():
                value = lab["value"].strip()
                if value and value.lower() not in latin and not t.vernacular.get(lang):
                    t.vernacular[lang] = tidy_name(value)

    # -- Geographic plausibility -------------------------------------------

    def occurrences_near(self, gbif_key: int, lat: float, lon: float, radius_deg: float = 1.0) -> int:
        """Number of GBIF occurrences of the taxon in a ±radius_deg square around the point.
        The centre is aligned on a 1° grid so that photos of the same area share the cache. From the faceted
        search of the area when it can answer, otherwise a search for this taxon alone."""
        n = self._area_count(gbif_key, lat, lon, radius_deg)
        if n is not None:
            return n
        return int(self.get(*self._near_req(gbif_key, lat, lon, radius_deg)).get("count", 0))

    def _fossil_list(self, fetch: bool = True) -> dict[str, int] | None:
        """{species key: fossil records} for every species with fossils (cached FOSSILS_TTL); None if unknown and
        fetch is False, or if GBIF returned an incomplete list (the per-taxon search is then used)."""
        if self._fossils is None:
            row = self.db.execute("SELECT body, fetched FROM http_cache WHERE url = ?", (FOSSILS_URL,)).fetchone()
            if row and time.time() - row[1] < FOSSILS_TTL:
                self._fossils = json.loads(row[0])
            elif fetch and "fossils" not in self._refused:
                body = self._refusal_safe("fossils", FOSSILS_URL)
                if body and "fossils" not in self._refused:
                    self._store_fossils(body)
        return self._fossils if self._fossils is not None and "_incomplete" not in self._fossils else None

    def _store_fossils(self, body: dict) -> None:
        counts = ((body.get("facets") or [{}])[0]).get("counts", [])
        if not counts:   # unexpected empty list: not kept (it would clear every species), per-taxon searches
            self._fossils = {"_incomplete": 1}
            return
        self._fossils = {c["name"]: c["count"] for c in counts} if len(counts) < 1_200_000 else {"_incomplete": 1}
        self.db.execute("INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?)",
                        (FOSSILS_URL, json.dumps(self._fossils, separators=(",", ":")), time.time()))
        self.db.commit()

    def _fossil_count(self, gbif_key: int, fetch: bool = True) -> int | None:
        """Fossil records of a species from the world list (no request of its own); None for another rank."""
        if (self._cached((f"{GBIF}/species/{gbif_key}", None, 30 * 86400)) or {}).get("rank") != "SPECIES":
            return None
        fossils = self._fossil_list(fetch)
        return None if fossils is None else fossils.get(str(gbif_key), 0)

    def fossil_only(self, gbif_key: int, lat: float | None = None, lon: float | None = None) -> bool:
        """Species known mostly from fossils: more fossils than 5 times its field observations
        worldwide (Equus leidyi, Canis gregarius...). The "extinct" flag of GBIF profiles is
        not reliable (it marks the stone marten, for instance): occurrences are used instead.
        Fossils from the world list; field observations from the area of the photo when they are enough to settle
        it (worldwide ones are at least as many), otherwise a worldwide search for this taxon."""
        fossils = self._fossil_count(gbif_key)
        if fossils is None:
            fossils = int(self.get(*self._fossil_req(gbif_key, False)).get("count", 0))
        if not fossils:
            return False
        if lat is not None and lon is not None:
            region = self._area_count(gbif_key, lat, lon, 5.0)
            if region is not None and region * 5 >= fossils:
                return False
        return int(self.get(*self._fossil_req(gbif_key, True)).get("count", 0)) * 5 < fossils

    def geo(self, gbif_key: int, lat: float | None, lon: float | None) -> tuple[str, float]:
        """(badge, prior weight) for the presence of the taxon around the point.

        Tiers (present nearby / in the region / almost absent) multiplied by a slight
        weighting by observation density, which mostly serves as a tie-breaker. Measured on the dev
        set (dev/benchmark_pipeline.py): a more strongly weighted density favours common
        species too much and degrades the top-1.
        Without GPS: ('unknown', 1.0) for all candidates.
        """
        if lat is None or lon is None:
            return "unknown", 1.0
        region = self.occurrences_near(gbif_key, lat, lon, 5.0)
        # The ±1° square is inside the ±5° one (same centre): nothing in the region, nothing nearby.
        near = self.occurrences_near(gbif_key, lat, lon, 1.0) if region else 0
        badge = "green" if near >= 3 else "amber" if region >= 10 else "red"
        tier = {"green": 1.0, "amber": 0.25, "red": 0.02}[badge]
        return badge, tier * (near + 0.05 * region + 0.5) ** 0.1


def tidy_name(value: str) -> str:
    """Clean common name: initial capital; a name in all capitals (GBIF gives "DOGS" for Canidae)
    goes back to sentence case. Short acronyms (≤ 3 letters) are left as is."""
    value = value.strip()
    letters = [c for c in value if c.isalpha()]
    if len(letters) > 3 and all(c.isupper() for c in letters):
        value = value.lower()
    return value[:1].upper() + value[1:]


def tidy_names(db: sqlite3.Connection) -> int:
    """Fix the names already cached (once at startup); return the number of taxa changed."""
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
