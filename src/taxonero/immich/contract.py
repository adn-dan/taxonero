"""Contract with the Immich API.

Only the endpoints listed here are called by the tool. At startup, this
contract is compared with the OpenAPI spec exposed by the server: if an endpoint, a method,
a field or a permission has disappeared, the tool goes read-only.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Endpoint:
    # Accepted methods, in order of preference (e.g. "patch|put": Immich v3
    # deprecated its update PUTs in favour of an upcoming replacement).
    method: str
    path: str
    permission: str | None
    writes: bool = False
    # Request body fields (JSON schema) the tool depends on.
    body_fields: tuple[str, ...] = field(default=())
    # Optional: its absence (or the permission not ticked) degrades a feature without blocking the tool.
    optional: bool = False


CONTRACT: tuple[Endpoint, ...] = (
    Endpoint("get", "/server/about", "server.about"),
    Endpoint("get", "/server/version", None),
    # Permissions of the key itself (no permission required): say which one is missing, or which one is extra.
    Endpoint("get", "/api-keys/me", None, optional=True),
    Endpoint("post", "/search/metadata", "asset.read", body_fields=("page", "size", "type", "updatedAfter", "withExif", "tagIds")),
    Endpoint("get", "/assets/{id}", "asset.read"),
    Endpoint("get", "/assets/{id}/thumbnail", "asset.view"),
    # Original, to identify small subjects in full resolution (otherwise: ~1440 px preview).
    Endpoint("get", "/assets/{id}/original", "asset.download", optional=True),
    Endpoint("patch|put", "/assets/{id}", "asset.update", writes=True, body_fields=("description",)),
    Endpoint("get", "/tags", "tag.read"),
    Endpoint("put", "/tags", "tag.create", writes=True, body_fields=("tags",)),
    Endpoint("put", "/tags/assets", "tag.asset", writes=True, body_fields=("tagIds", "assetIds")),
    Endpoint("delete", "/tags/{id}/assets", "tag.asset", writes=True, body_fields=("ids",)),
    Endpoint("delete", "/tags/{id}", "tag.delete", writes=True),
)

# Permissions to tick on the dedicated API key (and nothing else), then the optional ones.
REQUIRED_PERMISSIONS: tuple[str, ...] = tuple(sorted({e.permission for e in CONTRACT if e.permission and not e.optional}))
OPTIONAL_PERMISSIONS: tuple[str, ...] = tuple(sorted({e.permission for e in CONTRACT if e.permission and e.optional}))

# Versions on which the full cycle was tested (dev/compat-test.sh).
TESTED_VERSIONS: tuple[str, ...] = ("3.3.5",)


def _resolve(spec: dict, schema: dict) -> dict:
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"][name]
    return schema


def resolve_method(spec: dict, ep: Endpoint) -> str | None:
    """First contract method available and not deprecated, otherwise the first available."""
    ops = spec.get("paths", {}).get(ep.path, {})
    available = [m for m in ep.method.split("|") if m in ops]
    for m in available:
        if not ops[m].get("deprecated"):
            return m
    return available[0] if available else None


def check_spec(spec: dict) -> tuple[list[str], list[str]]:
    """Compare the contract with the server spec. Return (blocking errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    for ep in CONTRACT:
        method = resolve_method(spec, ep)
        if method is None:
            (warnings if ep.optional else errors).append(f"{ep.method.upper()} {ep.path}: endpoint missing")
            continue
        op = spec["paths"][ep.path][method]
        label = f"{method.upper()} {ep.path}"
        state = op.get("x-immich-state")
        if op.get("deprecated") or state == "Deprecated":
            warnings.append(f"{label}: deprecated (still working)")
        elif state and state != "Stable":
            warnings.append(f"{label}: state {state}")
        if ep.permission and op.get("x-immich-permission") not in (None, ep.permission):
            (warnings if ep.optional else errors).append(f"{label}: permission {op.get('x-immich-permission')} instead of {ep.permission}")
        if ep.body_fields:
            content = op.get("requestBody", {}).get("content", {}).get("application/json", {})
            props = _resolve(spec, content.get("schema", {})).get("properties", {})
            errors += [f"{label}: field '{f}' missing from the body" for f in ep.body_fields if f not in props]
    return errors, warnings
