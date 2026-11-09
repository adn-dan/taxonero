"""Minimal Immich client, limited to the contract (contract.py).

Guarantees:
- no request outside the contract can be sent (ContractViolation);
- no write until check() has validated the server, nor in dry-run mode;
- in dry-run, writes are logged instead of being sent.
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
    """Asset deleted from Immich (or out of reach of the key): Immich answers 400 "Not found or no
    asset.read access", sometimes 404. "Local folder" source: file gone."""
    if isinstance(e, FileNotFoundError):
        return True
    if not isinstance(e, httpx.HTTPStatusError):
        return False
    r = e.response
    return r.status_code == 404 or (r.status_code == 400 and "not found" in r.text.lower())


def is_transient(e: BaseException) -> bool:
    """Transient outage rather than an unreadable photo: network down, timeout, 429 or 5xx error."""
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
        # Permissions of the key (None: unknown), and the gap with the contract.
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

    # -- safeguard -----------------------------------------------------------

    def check(self) -> bool:
        """Check the server version and spec. Enable writing only if everything is compatible."""
        v = self._http.get("/server/version").raise_for_status().json()
        self.version = f"{v['major']}.{v['minor']}.{v['patch']}"
        spec = self._http.get("/spec.json").raise_for_status().json()
        self.errors, self.warnings = check_spec(spec)
        self._methods = {ep.path: m for ep in CONTRACT if (m := resolve_method(spec, ep))}
        if self.version not in TESTED_VERSIONS:
            self.warnings.append(f"version {self.version} untested (tested: {', '.join(TESTED_VERSIONS)})")
        # Also check that the key has access (read-only, no effect).
        self._request("get", "/server/about")
        self._check_permissions()
        self.writable = not self.errors
        return self.writable

    def _check_permissions(self) -> None:
        """Compare the key permissions with the contract: a missing one blocks writing, an extra one
        (or "all") is reported, as the key must stay limited to what is strictly needed."""
        try:
            self.permissions = self._request("get", "/api-keys/me").json().get("permissions", [])
        except httpx.HTTPStatusError:
            self.permissions = None   # older Immich: unknown, 403 errors will tell
            return
        perms = set(self.permissions)
        self.missing = [] if "all" in perms else sorted(set(REQUIRED_PERMISSIONS) - perms)
        self.extra = sorted(perms - set(REQUIRED_PERMISSIONS) - set(OPTIONAL_PERMISSIONS))
        if self.missing:
            self.errors.append(f"permissions missing on the API key: {', '.join(self.missing)}")
        if self.extra:
            self.warnings.append(f"API key more powerful than needed: {', '.join(self.extra)}")

    def status(self) -> dict:
        return {"version": self.version, "writable": self.writable, "errors": self.errors, "warnings": self.warnings,
                "permissions": self.permissions, "missing": self.missing, "extra": self.extra}


    # -- requests ------------------------------------------------------------

    def _endpoint(self, method: str, path: str) -> Endpoint:
        for ep, pat in _ROUTES:
            if pat.match(path) and method in ep.method.split("|"):
                return ep
        raise ContractViolation(f"{method.upper()} {path} outside the contract")

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        ep = self._endpoint(method, path)
        if ep.writes:
            if not self.writable:
                raise ReadOnlyError(f"write refused ({method.upper()} {path}): server not validated")
            if self.dry_run:
                log.info("[dry-run] %s %s %s", method.upper(), path, kw.get("json", ""))
                return httpx.Response(204, request=httpx.Request(method, path))
        return self._http.request(method, path, **kw).raise_for_status()

    def _update_method(self, template: str) -> str:
        return self._methods.get(template, "put")

    # -- reading -------------------------------------------------------------

    def iter_assets(self, updated_after: str | None = None, size: int = 250, with_exif: bool = True,
                    kind: str = "IMAGE", with_people: bool = False) -> Iterator[dict]:
        """Photos (kind IMAGE) or videos (VIDEO), outside the trash, page by page. with_people: people
        recognised by Immich in 'people' (without it, the list is empty)."""
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
        """missing_ok: None if the asset no longer exists in Immich, instead of an exception."""
        try:
            return self._request("get", f"/assets/{asset_id}").json()
        except httpx.HTTPStatusError as e:
            if missing_ok and is_missing(e):
                return None
            raise

    def thumbnail(self, asset_id: str, size: str = "preview") -> bytes:
        return self._request("get", f"/assets/{asset_id}/thumbnail", params={"size": size}).content

    def original(self, asset_id: str) -> bytes:
        """Original file (optional asset.download permission)."""
        return self._request("get", f"/assets/{asset_id}/original").content

    def list_tags(self) -> list[dict]:
        return self._request("get", "/tags").json()

    # -- writing ------------------------------------------------------------

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
    """Try a connection without writing anything. (ready client or None, state); state['code']: ok, readonly
    (connected, but missing permissions or incompatible server), unreachable, bad_key, not_immich.
    timeout: short for a simple test; that of a client kept afterwards (Worker.connect) is the normal timeout."""
    if not url or not api_key:
        return None, {"code": "missing", "errors": ["URL or API key missing"]}
    client = ImmichClient(url, api_key, dry_run=dry_run, timeout=timeout)
    try:
        ok = client.check()
    except httpx.HTTPStatusError as e:
        client.close()
        code = "bad_key" if e.response.status_code in (401, 403) else "not_immich"
        return None, {"code": code, "errors": [f"HTTP {e.response.status_code} on {e.request.url.path}"]}
    except (httpx.HTTPError, ValueError, KeyError) as e:   # network, invalid URL, response that is not Immich
        client.close()
        code = "unreachable" if isinstance(e, httpx.TransportError) or isinstance(e, httpx.InvalidURL) else "not_immich"
        return None, {"code": code, "errors": [f"{type(e).__name__}: {e}"]}
    return client, {"code": "ok" if ok else "readonly", **client.status()}
