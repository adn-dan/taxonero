"""Facts about a taxon, beyond its names: shown on the species page and in the viewer.

- season: GBIF field observations per month in the region of the photo (the same ±5° square as the geographic check);
- local presence: observations nearby (±1°, about 100 km) and in the region (±5°, about 500 km);
- status in the country of the photo (native, introduced, invasive), from the GBIF distributions of the species (lists
  of introduced and invasive species per country such as GRIIS; a native species is often simply not listed);
- figures from Wikidata when it has them (mass, length, wingspan, lifespan, gestation, clutch…): good coverage for
  birds and mammals, thin elsewhere;
- a recording (Wikimedia Commons, through Wikidata), and for birds the species page of xeno-canto.

Everything comes from public services and is cached locally in a compact form (http_cache, under "facts:…" keys: the
raw responses weigh up to several hundred KB per species). Each part is optional: one that fails or has no data is
simply left out.
"""

import hashlib
import json
import logging
import time
from urllib.parse import quote

from .taxonomy import GBIF, WIKIDATA, Taxonomy

log = logging.getLogger(__name__)

TTL = 180 * 86400
FIELD = ["HUMAN_OBSERVATION", "MACHINE_OBSERVATION", "OBSERVATION"]   # no zoo, herbarium, museum (as taxonomy._near_req)
NEAR_DEG, REGION_DEG = 1.0, 5.0

# Wikidata quantities shown: property -> (id of the fact, kind of unit, keep the largest value). Mass and length have
# several values (egg, newborn, male, female): the largest is the adult.
QUANTITIES = {"P2067": ("mass", "mass", True), "P2043": ("length", "length", True), "P2048": ("height", "length", True),
              "P2050": ("wingspan", "length", True), "P2250": ("lifespan", "time", False),
              "P4214": ("max_lifespan", "time", True), "P3063": ("gestation", "time", False),
              "P7770": ("incubation", "time", False), "P7725": ("litter", "count", False)}
ORDER = [v[0] for v in QUANTITIES.values()]
# Wikidata unit -> (kind, factor to grams or metres, or Intl unit name for a duration).
UNITS = {"Q41803": ("mass", 1.0), "Q11570": ("mass", 1000.0),
         "Q11573": ("length", 1.0), "Q174728": ("length", 0.01), "Q174789": ("length", 0.001),
         "Q573": ("time", "day"), "Q23387": ("time", "week"), "Q5151": ("time", "month"), "Q577": ("time", "year"),
         "1": ("count", None)}
AUDIO = "P51"
# A quantity can say what it is the value of ("object has role"): the mass of a bird is often given at birth only
# (2.4 g for the robin), next to or instead of the adult's. Kept: no role, or one of these (adult weight, mass).
ROLE, ADULT_ROLES = "P3831", {"Q78101716", "Q11423"}
# Establishment means of a GBIF distribution -> status shown (the strongest one found for the country wins).
MEANS = {"INVASIVE": "invasive", "INTRODUCED": "introduced", "NATURALISED": "introduced", "NATURALIZED": "introduced",
         "INTRODUCEDASSISTEDCOLONISATION": "introduced", "NATIVE": "native", "NATIVEREINTRODUCED": "native"}
STRENGTH = {"native": 1, "introduced": 2, "invasive": 3}
NOT_THERE = {"ABSENT", "DOUBTFUL", "EXCLUDED", "EXTINCT"}   # occurrence status of a distribution that says "not here"


def _cached(tx: Taxonomy, key: str, compute, ttl: float = TTL):
    """compute() kept in the local cache under key (compact JSON), for ttl seconds."""
    row = tx.db.execute("SELECT body, fetched FROM http_cache WHERE url = ?", (key,)).fetchone()
    if row and time.time() - row[1] < ttl:
        return json.loads(row[0])
    value = compute()
    tx.db.execute("INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?)", (key, json.dumps(value, separators=(",", ":")), time.time()))
    tx.db.commit()
    return value


def cell(lat: float, lon: float) -> tuple[float, float]:
    """Centre of the 1° cell of a point: photos of the same area share the cached answers (as taxonomy._near_req)."""
    return float(round(lat)), float(round(lon))


def months(tx: Taxonomy, gbif_key: int, lat: float, lon: float) -> list[int] | None:
    """Field observations of the taxon per month (January first) in the region; None without any."""
    lat, lon = cell(lat, lon)
    q = {"taxonKey": gbif_key, "limit": 0, "occurrenceStatus": "PRESENT", "basisOfRecord": FIELD,
         **tx.square(lat, lon, REGION_DEG), "facet": "month", "facetLimit": 12}
    facets = tx.get(f"{GBIF}/occurrence/search", q, TTL).get("facets") or []
    counts = {c["name"]: c["count"] for f in facets if f.get("field") == "MONTH" for c in f.get("counts", [])}
    out = [int(counts.get(str(m), 0)) for m in range(1, 13)]
    return out if any(out) else None


def country(tx: Taxonomy, lat: float, lon: float) -> dict | None:
    """{"code": ISO code (two letters), "name": English name} of the country of a point, from the GBIF reverse geocoder;
    points 0.1° apart share the answer. None at sea."""
    lat, lon = round(lat, 1), round(lon, 1)

    def fetch():
        places = tx._fetch(f"{GBIF}/geocode/reverse?lat={lat}&lng={lon}")
        inside = [p for p in places if p.get("isoCountryCode2Digit") and not p.get("distance")]
        best = next((p for p in inside if p.get("type") == "GADM0"), None)
        return {"code": best["isoCountryCode2Digit"], "name": best.get("title") or ""} if best else None
    return _cached(tx, f"facts:place:{lat},{lon}", fetch, 365 * 86400)


def statuses(tx: Taxonomy, gbif_key: int) -> dict[str, dict]:
    """Establishment of the species per country code, from its GBIF distributions:
    {code: {"": [statuses of the records about the whole country], locality in lower case: [statuses]}}.
    A record carries the code of the country a place belongs to, not only of the country itself: the daisy is "FR,
    introduced" for the Kerguelen Islands, the red fox for Corsica. The locality is kept to tell them apart (status)."""
    def fetch():
        out: dict[str, dict] = {}
        for r in tx._fetch(f"{GBIF}/species/{gbif_key}/distributions?limit=1000").get("results", []):
            status = MEANS.get((r.get("establishmentMeans") or "").upper().replace("_", ""))
            code = r.get("country")
            if not status or not code or (r.get("status") or "").upper() in NOT_THERE:
                continue
            whole = not r.get("locality") or (r.get("locationId") or "").replace("_", "").upper() == f"ISO3166-2:{code}"
            found = out.setdefault(code, {}).setdefault("" if whole else r["locality"].strip().lower(), [])
            if status not in found:
                found.append(status)
        return out
    return _cached(tx, f"facts:status:{gbif_key}", fetch)


def status(tx: Taxonomy, gbif_key: int, place: dict) -> str | None:
    """native, introduced or invasive in the country of the photo: only from records about the whole country (no
    locality, or the country by name), and only when they agree. Sources that disagree (native for one, introduced for
    another), or that speak of a part of the country only: nothing is said."""
    known = statuses(tx, gbif_key).get(place["code"]) or {}
    found = set(known.get("", [])) | set(known.get((place.get("name") or "").strip().lower(), []) if place.get("name") else [])
    if not found or ("native" in found and len(found) > 1):
        return None
    return max(found, key=STRENGTH.get)


def _amount(kind: str, value: dict) -> tuple[float, object] | None:
    """(amount in a common unit, unit detail) of a Wikidata quantity of the expected kind; None for another unit."""
    unit = UNITS.get(str(value.get("unit", "")).rsplit("/", 1)[-1])
    if not unit or unit[0] != kind:
        return None
    try:
        amount = float(value["amount"])
    except (KeyError, TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return (amount * unit[1], None) if kind in ("mass", "length") else (amount, unit[1])


def _shown(kind: str, amount: float, detail) -> dict:
    """Value and unit to display (Intl unit names): kg or g, m, cm or mm, the duration in its own unit."""
    if kind == "mass":
        value, unit = (amount / 1000, "kilogram") if amount >= 1000 else (amount, "gram")
    elif kind == "length":
        value, unit = (amount, "meter") if amount >= 1 else (amount * 100, "centimeter") if amount >= 0.01 else (amount * 1000, "millimeter")
    else:
        value, unit = amount, detail
    return {"value": round(value, 1 if value >= 10 else 2), "unit": unit}


def parse_claims(claims: dict) -> dict:
    """{"traits": [{id, value, unit}], "audio": file name | None} from the claims of a Wikidata item."""
    traits: dict[str, dict] = {}
    for prop, (name, kind, largest) in QUANTITIES.items():
        found = []
        for c in claims.get(prop) or []:
            value = (c.get("mainsnak", {}).get("datavalue") or {}).get("value")
            roles = {(q.get("datavalue") or {}).get("value", {}).get("id") for q in (c.get("qualifiers") or {}).get(ROLE, [])
                     if isinstance((q.get("datavalue") or {}).get("value"), dict)}
            if roles and not roles & ADULT_ROLES:   # birth weight, egg…: not the animal one sees
                continue
            parsed = _amount(kind, value) if c.get("rank") != "deprecated" and isinstance(value, dict) else None
            if parsed:
                found.append((c.get("rank") == "preferred", *parsed))
        if not found:
            continue
        # The value marked as preferred, otherwise the largest (mass, length) or the first one given.
        preferred = [f for f in found if f[0]] or found
        _, amount, detail = max(preferred, key=lambda f: f[1]) if largest else preferred[0]
        traits[name] = {"id": name, **_shown(kind, amount, detail)}
    audio = next(((c.get("mainsnak", {}).get("datavalue") or {}).get("value") for c in claims.get(AUDIO) or []
                  if c.get("rank") != "deprecated"), None)
    return {"traits": [traits[n] for n in ORDER if n in traits], "audio": audio if isinstance(audio, str) else None}


def wikidata(tx: Taxonomy, qid: str) -> dict:
    """Figures and recording of a Wikidata item (see parse_claims)."""
    def fetch():
        url = f"{WIKIDATA}?action=wbgetentities&ids={quote(qid)}&props=claims&format=json"
        return parse_claims((tx._fetch(url).get("entities", {}).get(qid) or {}).get("claims") or {})
    return _cached(tx, f"facts:wd:{qid}", fetch)   # "facts:wikidata:…": answers kept before the roles were read


def commons_audio(name: str) -> dict:
    """Addresses of a Wikimedia Commons recording: the file, its MP3 version (browsers that do not play Ogg) and its
    page (author, licence)."""
    file = name.replace(" ", "_")
    h, q = hashlib.md5(file.encode()).hexdigest(), quote(file)
    base = "https://upload.wikimedia.org/wikipedia/commons"
    return {"src": f"{base}/{h[0]}/{h[:2]}/{q}", "mp3": f"{base}/transcoded/{h[0]}/{h[:2]}/{q}/{q}.mp3",
            "page": f"https://commons.wikimedia.org/wiki/File:{q}"}


def regions(tx: Taxonomy, db, gbif_key: int, limit: int = 60) -> list[dict]:
    """Regions where confirmed photos of the taxon were taken, most photos first (species page: one season, presence
    and status per region, chosen in a menu): [{"country": ISO code or None, "lat", "lon", "n": photos}]. One region
    per country, placed on the 1° cell of that country with most of the photos; a place whose country is not known
    (at sea, geocoder down) is a region of its own. At most limit places are looked up."""
    found: dict[str, dict] = {}
    for r in db.execute(
            "SELECT a.lat, a.lon, round(a.lat) AS clat, round(a.lon) AS clon, count(DISTINCT a.asset_id) AS n "
            "FROM detection d JOIN asset a ON a.asset_id = d.asset_id WHERE d.taxon_key = ? AND d.status = 'confirmed' "
            "AND a.lat IS NOT NULL AND a.lon IS NOT NULL AND a.missing_at IS NULL "
            "GROUP BY round(a.lat, 1), round(a.lon, 1) ORDER BY n DESC LIMIT ?", (gbif_key, limit)):
        try:
            place = country(tx, r["lat"], r["lon"])
        except Exception as e:   # geocoder down: the place still counts, without its country
            log.debug("facts of %s, country at %s,%s unavailable: %s", gbif_key, r["lat"], r["lon"], e)
            place = None
        code = place["code"] if place else None
        region = found.setdefault(code or f"{r['clat']},{r['clon']}", {"country": code, "n": 0, "cells": {}})
        region["n"] += r["n"]
        region["cells"][(r["clat"], r["clon"])] = region["cells"].get((r["clat"], r["clon"]), 0) + r["n"]
    out = []
    for region in sorted(found.values(), key=lambda x: -x["n"]):
        lat, lon = max(region["cells"], key=region["cells"].get)
        out.append({"country": region["country"], "lat": lat, "lon": lon, "n": region["n"]})
    return out


def targets(db) -> list[tuple[int, float | None, float | None]]:
    """(taxon, lat, lon) of everything the dashboard will ask for the confirmed photos: each taxon at each place of its
    photos (one photo per 1° cell and 0.1° square, which is what the answers depend on), at the centre of each of these
    cells (regions of its species page), and without a place when none of its photos has one."""
    out: dict[tuple, tuple] = {}
    keys = [r[0] for r in db.execute("SELECT DISTINCT taxon_key FROM detection WHERE status = 'confirmed' AND taxon_key IS NOT NULL")]
    for r in db.execute(
            "SELECT d.taxon_key AS k, a.lat, a.lon FROM detection d JOIN asset a ON a.asset_id = d.asset_id "
            "WHERE d.status = 'confirmed' AND d.taxon_key IS NOT NULL AND a.lat IS NOT NULL AND a.lon IS NOT NULL "
            "AND a.missing_at IS NULL GROUP BY d.taxon_key, round(a.lat), round(a.lon), round(a.lat, 1), round(a.lon, 1)"):
        out[_done_key(r["k"], r["lat"], r["lon"])] = (r["k"], r["lat"], r["lon"])
    # Species page: the centre of each 1° cell of its photos (the regions of its menu are among them).
    for r in db.execute(
            "SELECT d.taxon_key AS k, round(a.lat) AS lat, round(a.lon) AS lon FROM detection d JOIN asset a ON a.asset_id = d.asset_id "
            "WHERE d.status = 'confirmed' AND d.taxon_key IS NOT NULL AND a.lat IS NOT NULL AND a.lon IS NOT NULL "
            "AND a.missing_at IS NULL GROUP BY 1, 2, 3"):
        out.setdefault(_done_key(r["k"], r["lat"], r["lon"]), (r["k"], r["lat"], r["lon"]))
    located = {x[0] for x in out.values()}
    for k in keys:
        if k not in located:
            out.setdefault(_done_key(k, None, None), (k, None, None))
    return list(out.values())


def _done_key(gbif_key: int, lat: float | None, lon: float | None) -> str:
    """What the facts of a taxon depend on: the 1° cell of the place (season, presence) and its 0.1° square (country)."""
    where = f"{cell(lat, lon)}:{round(lat, 1)},{round(lon, 1)}" if lat is not None and lon is not None else ""
    return f"facts:done:{gbif_key}:{where}"


def fetched(tx: Taxonomy, gbif_key: int, lat: float | None, lon: float | None) -> bool:
    """Already fetched in full for this place, recently enough (prefetch): nothing to ask again."""
    row = tx.db.execute("SELECT fetched FROM http_cache WHERE url = ?", (_done_key(gbif_key, lat, lon),)).fetchone()
    return bool(row and time.time() - row[0] < TTL)


def prefetch(tx: Taxonomy, gbif_key: int, taxon: dict | None, lat: float | None, lon: float | None) -> bool:
    """Fetch the facts of a taxon for a place so that the dashboard no longer waits for them (worker, at each cycle).
    True when everything was obtained: it is then noted as done; a part that failed is asked again next time."""
    failed: list[str] = []
    facts(tx, gbif_key, taxon, lat, lon, failed)
    if not failed:
        tx.db.execute("INSERT OR REPLACE INTO http_cache VALUES (?, '1', ?)", (_done_key(gbif_key, lat, lon), time.time()))
        tx.db.commit()
    return not failed


def facts(tx: Taxonomy, gbif_key: int, taxon: dict | None, lat: float | None = None, lon: float | None = None,
          failed: list[str] | None = None) -> dict:
    """Everything known about the taxon (its cached record: rank, lineage, Wikidata item) for a place, or without one.
    Each part is None (or empty) when there is no data or when its service does not answer (its name is then added
    to failed, if given)."""
    out: dict = {"months": None, "near": None, "region": None, "presence": None, "country": None, "status": None,
                 "traits": [], "audio": None, "xeno_canto": None}
    taxon = taxon or {}

    def part(name: str, fn):
        try:
            return fn()
        except Exception as e:   # service down, unexpected answer: this part is left out
            log.debug("facts of %s, %s unavailable: %s", gbif_key, name, e)
            if failed is not None:
                failed.append(name)
            return None

    if lat is not None and lon is not None:
        out["months"] = part("months", lambda: months(tx, gbif_key, lat, lon))
        region = part("region", lambda: tx.occurrences_near(gbif_key, lat, lon, REGION_DEG))
        # The ±1° square is inside the ±5° one (same centre): nothing in the region, nothing nearby.
        near = part("near", lambda: tx.occurrences_near(gbif_key, lat, lon, NEAR_DEG)) if region else 0 if region == 0 else None
        out["near"], out["region"] = near, region
        if near is not None and region is not None:   # same tiers as the badge of the geographic check (Taxonomy.geo)
            out["presence"] = "green" if near >= 3 else "amber" if region >= 10 else "red"
        place = part("country", lambda: country(tx, lat, lon))
        out["country"] = place["code"] if place else None
        if place and taxon.get("rank") in ("species", "subspecies", "variety", "form"):
            out["status"] = part("status", lambda: status(tx, gbif_key, place))
    if taxon.get("wikidata"):
        wd = part("wikidata", lambda: wikidata(tx, taxon["wikidata"])) or {}
        out["traits"] = wd.get("traits") or []
        out["audio"] = commons_audio(wd["audio"]) if wd.get("audio") else None
    name = taxon.get("scientific_name") or ""
    if (taxon.get("lineage") or {}).get("class") == "Aves" and taxon.get("rank") == "species" and len(name.split()) == 2:
        out["xeno_canto"] = f"https://xeno-canto.org/species/{quote(name.replace(' ', '-'))}"
    return out
