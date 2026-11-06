"""Reference photos of a taxon for comparison: those iNaturalist highlights on the taxon page (chosen
by its community, usually a dozen: male, female, juvenile, details). Only photos under a
Creative Commons licence are kept, with their author and licence; they stay hosted by iNaturalist (the
browser loads them from there), nothing is copied. Responses cached for 30 days (http_cache)."""

from .taxonomy import Taxonomy

API = "https://api.inaturalist.org/v1"
MAX_PHOTOS = 12


def inat_taxon(tx: Taxonomy, names: list[str], synonyms=None) -> dict | None:
    """iNaturalist taxon with the same scientific name (tried in order: accepted name, name given by the model), or
    of which this name is a synonym (matched_term): iNaturalist and GBIF do not always classify alike (Ardea modesta
    is Ardea alba modesta there, Felis manul is Otocolobus manul). synonyms(): other names, asked as a last resort."""
    def tries():
        yield from names
        if synonyms:
            yield from synonyms()
    for name in dict.fromkeys(n for n in tries() if n):
        results = tx.get(f"{API}/taxa", {"q": name, "per_page": 10}).get("results", [])
        for field in ("name", "matched_term"):
            for r in results:
                if (r.get(field) or "").lower() == name.lower():
                    return r
    return None


def photos(tx: Taxonomy, names: list[str], synonyms=None) -> dict:
    """{'taxon': {id, name, url} | None, 'photos': [{thumb, large, attribution, license, url}]}."""
    t = inat_taxon(tx, names, synonyms)
    if not t:
        return {"taxon": None, "photos": []}
    full = (tx.get(f"{API}/taxa/{t['id']}").get("results") or [{}])[0]
    out = []
    for tp in full.get("taxon_photos") or [{"photo": t.get("default_photo") or {}}]:
        p = tp.get("photo") or {}
        if not p.get("license_code") or not p.get("medium_url"):   # "all rights reserved": not shown
            continue
        out.append({"thumb": p["medium_url"], "large": p["medium_url"].replace("/medium.", "/large."),
                    "full": p["medium_url"].replace("/medium.", "/original."),   # loaded only when zooming
                    "attribution": p.get("attribution", ""), "license": p["license_code"].upper(),
                    "url": f"https://www.inaturalist.org/photos/{p['id']}" if p.get("id") else None})
        if len(out) >= MAX_PHOTOS:
            break
    return {"taxon": {"id": t["id"], "name": t.get("name"), "url": f"https://www.inaturalist.org/taxa/{t['id']}"},
            "photos": out}
