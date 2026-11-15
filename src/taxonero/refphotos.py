"""Reference photos of a taxon for comparison: those iNaturalist highlights on the taxon page (chosen
by its community, usually a dozen: male, female, juvenile, details). Only photos under a
Creative Commons licence are kept, with their author and licence; they stay hosted by iNaturalist (the
browser loads them from there), nothing is copied. Responses cached for 30 days (http_cache)."""

from itertools import zip_longest

from .taxonomy import Taxonomy

API = "https://api.inaturalist.org/v1"
MAX_PHOTOS = 12


def inat_taxa(tx: Taxonomy, names: list[str], synonyms=None) -> list[dict]:
    """iNaturalist taxa matching a scientific name (tried in order: accepted name, name given by the model), the
    largest first. iNaturalist and GBIF do not always classify alike, hence in order of preference:
    - the taxon of that exact name;
    - the narrower taxa iNaturalist files under that name: Milvus aegyptius is a species for GBIF, two subspecies of
      Milvus migrans there (matched_term "Milvus aegyptius parasitus", "… aegyptius"); the species Milvus migrans,
      whose old name it also is, would show black kites;
    - the taxon of which it is a synonym (matched_term): Felis manul is Otocolobus manul there;
    - a subspecies of the same genus and epithet: Ardea modesta is Ardea alba modesta there, which its search does
      not give as a synonym.
    synonyms(): other names, asked as a last resort."""
    def tries():
        yield from names
        if synonyms:
            yield from synonyms()
    for name in dict.fromkeys(n for n in tries() if n):
        results, low = tx.get(f"{API}/taxa", {"q": name, "per_page": 10}).get("results", []), name.lower()
        exact = [r for r in results if (r.get("name") or "").lower() == low]
        parts = sorted((r for r in results if (r.get("matched_term") or "").lower().startswith(low + " ")),
                       key=lambda r: -(r.get("observations_count") or 0))
        synonym = [r for r in results if (r.get("matched_term") or "").lower() == low]
        genus, epithet = low.split()[0], low.split()[-1]
        trinomial = [r for r in results if len(n := (r.get("name") or "").lower().split()) == 3 and n[0] == genus and n[2] == epithet]
        found = exact[:1] or parts or synonym[:1] or trinomial[:1]
        if found:
            return found
    return []


def inat_taxon(tx: Taxonomy, names: list[str], synonyms=None) -> dict | None:
    """The main iNaturalist taxon for these names (inat_taxa): the most observed when the name covers several."""
    return next(iter(inat_taxa(tx, names, synonyms)), None)


def photos(tx: Taxonomy, names: list[str], synonyms=None) -> dict:
    """{'taxon': {id, name, url} | None, 'photos': [{thumb, large, attribution, license, url}]}."""
    taxa = inat_taxa(tx, names, synonyms)
    if not taxa:
        return {"taxon": None, "photos": []}
    t, lists = taxa[0], []
    for one in taxa:   # several subspecies (inat_taxa): their photos taken in turn, the most observed first
        full = (tx.get(f"{API}/taxa/{one['id']}").get("results") or [{}])[0]
        lists.append(full.get("taxon_photos") or [{"photo": one.get("default_photo") or {}}])
    out = []
    for tp in (tp for row in zip_longest(*lists) for tp in row if tp):
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
