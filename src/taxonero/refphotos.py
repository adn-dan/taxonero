"""Photos de référence d'un taxon pour comparer : celles qu'iNaturalist met en avant sur la fiche du taxon (choisies
par sa communauté, en général une douzaine : mâle, femelle, juvénile, détails). Seules les photos sous licence
Creative Commons sont gardées, avec leur auteur et leur licence ; elles restent hébergées par iNaturalist (le
navigateur les charge de là-bas), rien n'est copié. Réponses mises en cache 30 jours (http_cache)."""

from .taxonomy import Taxonomy

API = "https://api.inaturalist.org/v1"
MAX_PHOTOS = 12


def inat_taxon(tx: Taxonomy, names: list[str], synonyms=None) -> dict | None:
    """Taxon iNaturalist du même nom scientifique (essayés dans l'ordre : nom accepté, nom donné par le modèle), ou
    dont ce nom est un synonyme (matched_term) : iNaturalist et GBIF ne classent pas toujours pareil (Ardea modesta
    y est Ardea alba modesta, Felis manul y est Otocolobus manul). synonyms() : autres noms, demandés en dernier recours."""
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
        if not p.get("license_code") or not p.get("medium_url"):   # « tous droits réservés » : pas affichée
            continue
        out.append({"thumb": p["medium_url"], "large": p["medium_url"].replace("/medium.", "/large."),
                    "full": p["medium_url"].replace("/medium.", "/original."),   # chargée seulement au zoom
                    "attribution": p.get("attribution", ""), "license": p["license_code"].upper(),
                    "url": f"https://www.inaturalist.org/photos/{p['id']}" if p.get("id") else None})
        if len(out) >= MAX_PHOTOS:
            break
    return {"taxon": {"id": t["id"], "name": t.get("name"), "url": f"https://www.inaturalist.org/taxa/{t['id']}"},
            "photos": out}
