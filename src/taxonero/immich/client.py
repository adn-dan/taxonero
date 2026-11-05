"""Client Immich minimal, limité au contrat (contract.py).

Garanties :
- aucune requête hors contrat ne peut partir (ContractViolation) ;
- aucune écriture tant que check() n'a pas validé le serveur, ni en mode dry-run ;
- en dry-run, les écritures sont journalisées au lieu d'être envoyées.
"""

import logging
import re
from collections.abc import Iterator

import httpx

from .contract import CONTRACT, OPTIONAL_PERMISSIONS, REQUIRED_PERMISSIONS, TESTED_VERSIONS, Endpoint, check_spec, resolve_method

log = logging.getLogger(__name__)


class ContractViolation(RuntimeError):
    pass


class ReadOnlyError(RuntimeError):
    pass


def is_missing(e: Exception) -> bool:
    """Asset supprimé d'Immich (ou hors de portée de la clé) : Immich répond 400 « Not found or no
    asset.read access », parfois 404. Source « dossier local » : fichier disparu."""
    if isinstance(e, FileNotFoundError):
        return True
    if not isinstance(e, httpx.HTTPStatusError):
        return False
    r = e.response
    return r.status_code == 404 or (r.status_code == 400 and "not found" in r.text.lower())


def is_transient(e: BaseException) -> bool:
    """Panne passagère plutôt que photo illisible : réseau coupé, délai dépassé, 429 ou erreur 5xx."""
    if isinstance(e, httpx.TransportError):
        return True
    return isinstance(e, httpx.HTTPStatusError) and (e.response.status_code == 429 or e.response.status_code >= 500)


def _pattern(path: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$")


_ROUTES = [(ep, _pattern(ep.path)) for ep in CONTRACT]


class ImmichClient:
    def __init__(self, url: str, api_key: str, dry_run: bool = True, timeout: float = 30.0):
        self.dry_run = dry_run
        self.writable = False
        self.version: str | None = None
        self.warnings: list[str] = []
        self.errors: list[str] = []
        # Permissions de la clé (None : inconnues), et l'écart avec le contrat.
        self.permissions: list[str] | None = None
        self.missing: list[str] = []
        self.extra: list[str] = []
        self._methods: dict[str, str] = {}
        self._http = httpx.Client(
            base_url=url.rstrip("/") + "/api",
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    # -- garde-fou -----------------------------------------------------------

    def check(self) -> bool:
        """Vérifie version et spec du serveur. Active l'écriture seulement si tout est compatible."""
        v = self._http.get("/server/version").raise_for_status().json()
        self.version = f"{v['major']}.{v['minor']}.{v['patch']}"
        spec = self._http.get("/spec.json").raise_for_status().json()
        self.errors, self.warnings = check_spec(spec)
        self._methods = {ep.path: m for ep in CONTRACT if (m := resolve_method(spec, ep))}
        if self.version not in TESTED_VERSIONS:
            self.warnings.append(f"version {self.version} non testée (testées : {', '.join(TESTED_VERSIONS)})")
        # Vérifie aussi que la clé a bien accès (lecture seule, sans effet).
        self._request("get", "/server/about")
        self._check_permissions()
        self.writable = not self.errors
        return self.writable

    def _check_permissions(self) -> None:
        """Compare les permissions de la clé au contrat : une manquante bloque l'écriture, une de trop
        (ou « all ») est signalée, la clé devant rester limitée au strict nécessaire."""
        try:
            self.permissions = self._request("get", "/api-keys/me").json().get("permissions", [])
        except httpx.HTTPStatusError:
            self.permissions = None   # Immich plus ancien : on ne sait pas, les erreurs 403 le diront
            return
        perms = set(self.permissions)
        self.missing = [] if "all" in perms else sorted(set(REQUIRED_PERMISSIONS) - perms)
        self.extra = sorted(perms - set(REQUIRED_PERMISSIONS) - set(OPTIONAL_PERMISSIONS))
        if self.missing:
            self.errors.append(f"permissions manquantes sur la clé API : {', '.join(self.missing)}")
        if self.extra:
            self.warnings.append(f"clé API plus puissante que nécessaire : {', '.join(self.extra)}")

    def status(self) -> dict:
        return {"version": self.version, "writable": self.writable, "errors": self.errors, "warnings": self.warnings,
                "permissions": self.permissions, "missing": self.missing, "extra": self.extra}


    # -- requêtes ------------------------------------------------------------

    def _endpoint(self, method: str, path: str) -> Endpoint:
        for ep, pat in _ROUTES:
            if pat.match(path) and method in ep.method.split("|"):
                return ep
        raise ContractViolation(f"{method.upper()} {path} hors contrat")

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        ep = self._endpoint(method, path)
        if ep.writes:
            if not self.writable:
                raise ReadOnlyError(f"écriture refusée ({method.upper()} {path}) : serveur non validé")
            if self.dry_run:
                log.info("[dry-run] %s %s %s", method.upper(), path, kw.get("json", ""))
                return httpx.Response(204, request=httpx.Request(method, path))
        return self._http.request(method, path, **kw).raise_for_status()

    def _update_method(self, template: str) -> str:
        return self._methods.get(template, "put")

    # -- lecture -------------------------------------------------------------

    def iter_assets(self, updated_after: str | None = None, size: int = 250, with_exif: bool = True,
                    kind: str = "IMAGE", with_people: bool = False) -> Iterator[dict]:
        """Photos (kind IMAGE) ou vidéos (VIDEO), hors corbeille, page par page. with_people : personnes
        reconnues par Immich dans 'people' (sans lui, la liste est vide)."""
        page: int | None = 1
        while page:
            body = {"page": page, "size": size, "type": kind, "withExif": with_exif, "withPeople": with_people}
            if updated_after:
                body["updatedAfter"] = updated_after
            data = self._request("post", "/search/metadata", json=body).json()["assets"]
            yield from data["items"]
            page = int(data["nextPage"]) if data.get("nextPage") else None

    def count_assets_with_tag(self, tag_id: str) -> int:
        data = self._request("post", "/search/metadata", json={"tagIds": [tag_id], "size": 1, "page": 1}).json()
        return int(data["assets"]["total"])

    def get_asset(self, asset_id: str, missing_ok: bool = False) -> dict | None:
        """missing_ok : None si l'asset n'existe plus dans Immich, au lieu d'une exception."""
        try:
            return self._request("get", f"/assets/{asset_id}").json()
        except httpx.HTTPStatusError as e:
            if missing_ok and is_missing(e):
                return None
            raise

    def thumbnail(self, asset_id: str, size: str = "preview") -> bytes:
        return self._request("get", f"/assets/{asset_id}/thumbnail", params={"size": size}).content

    def original(self, asset_id: str) -> bytes:
        """Fichier original (permission facultative asset.download)."""
        return self._request("get", f"/assets/{asset_id}/original").content

    def list_tags(self) -> list[dict]:
        return self._request("get", "/tags").json()

    # -- écriture ------------------------------------------------------------

    def set_description(self, asset_id: str, description: str) -> None:
        method = self._update_method("/assets/{id}")
        self._request(method, f"/assets/{asset_id}", json={"description": description})

    def upsert_tags(self, values: list[str]) -> list[dict]:
        r = self._request("put", "/tags", json={"tags": values})
        return [] if self.dry_run else r.json()

    def bulk_tag(self, tag_ids: list[str], asset_ids: list[str]) -> None:
        self._request("put", "/tags/assets", json={"tagIds": tag_ids, "assetIds": asset_ids})

    def untag_assets(self, tag_id: str, asset_ids: list[str]) -> None:
        self._request("delete", f"/tags/{tag_id}/assets", json={"ids": asset_ids})

    def delete_tag(self, tag_id: str) -> None:
        self._request("delete", f"/tags/{tag_id}")


def probe(url: str, api_key: str, dry_run: bool = True, timeout: float = 10.0) -> tuple["ImmichClient | None", dict]:
    """Essaie une connexion sans rien écrire. (client prêt ou None, état) ; état['code'] : ok, readonly
    (connecté, mais permissions manquantes ou serveur incompatible), unreachable, bad_key, not_immich.
    timeout : court pour un simple test ; celui d'un client gardé ensuite (Worker.connect) est le délai normal."""
    if not url or not api_key:
        return None, {"code": "missing", "errors": ["URL ou clé API manquante"]}
    client = ImmichClient(url, api_key, dry_run=dry_run, timeout=timeout)
    try:
        ok = client.check()
    except httpx.HTTPStatusError as e:
        client.close()
        code = "bad_key" if e.response.status_code in (401, 403) else "not_immich"
        return None, {"code": code, "errors": [f"HTTP {e.response.status_code} sur {e.request.url.path}"]}
    except (httpx.HTTPError, ValueError, KeyError) as e:   # réseau, URL invalide, réponse qui n'est pas Immich
        client.close()
        code = "unreachable" if isinstance(e, httpx.TransportError) or isinstance(e, httpx.InvalidURL) else "not_immich"
        return None, {"code": code, "errors": [f"{type(e).__name__}: {e}"]}
    return client, {"code": "ok" if ok else "readonly", **client.status()}
