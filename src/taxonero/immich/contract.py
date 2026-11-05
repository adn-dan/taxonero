"""Contrat avec l'API Immich.

Seuls les endpoints listés ici sont appelés par l'outil. Au démarrage, on compare
ce contrat à la spec OpenAPI exposée par le serveur : si un endpoint, une méthode,
un champ ou une permission a disparu, l'outil passe en lecture seule.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Endpoint:
    # Méthodes acceptées, par ordre de préférence (ex. "patch|put" : Immich v3
    # a déprécié ses PUT de mise à jour au profit d'un remplaçant à venir).
    method: str
    path: str
    permission: str | None
    writes: bool = False
    # Champs du corps de requête (schéma JSON) dont on dépend.
    body_fields: tuple[str, ...] = field(default=())
    # Facultatif : son absence (ou la permission non cochée) dégrade une fonction sans bloquer l'outil.
    optional: bool = False


CONTRACT: tuple[Endpoint, ...] = (
    Endpoint("get", "/server/about", "server.about"),
    Endpoint("get", "/server/version", None),
    # Permissions de la clé elle-même (sans permission requise) : dire laquelle manque, ou laquelle est en trop.
    Endpoint("get", "/api-keys/me", None, optional=True),
    Endpoint("post", "/search/metadata", "asset.read", body_fields=("page", "size", "type", "updatedAfter", "withExif", "tagIds")),
    Endpoint("get", "/assets/{id}", "asset.read"),
    Endpoint("get", "/assets/{id}/thumbnail", "asset.view"),
    # Original, pour identifier les petits sujets en pleine résolution (sinon : aperçu ~1440 px).
    Endpoint("get", "/assets/{id}/original", "asset.download", optional=True),
    Endpoint("patch|put", "/assets/{id}", "asset.update", writes=True, body_fields=("description",)),
    Endpoint("get", "/tags", "tag.read"),
    Endpoint("put", "/tags", "tag.create", writes=True, body_fields=("tags",)),
    Endpoint("put", "/tags/assets", "tag.asset", writes=True, body_fields=("tagIds", "assetIds")),
    Endpoint("delete", "/tags/{id}/assets", "tag.asset", writes=True, body_fields=("ids",)),
    Endpoint("delete", "/tags/{id}", "tag.delete", writes=True),
)

# Permissions à cocher sur la clé API dédiée (et rien d'autre), puis celles qui sont facultatives.
REQUIRED_PERMISSIONS: tuple[str, ...] = tuple(sorted({e.permission for e in CONTRACT if e.permission and not e.optional}))
OPTIONAL_PERMISSIONS: tuple[str, ...] = tuple(sorted({e.permission for e in CONTRACT if e.permission and e.optional}))

# Versions sur lesquelles le cycle complet a été testé (dev/compat-test.sh).
TESTED_VERSIONS: tuple[str, ...] = ("3.3.5",)


def _resolve(spec: dict, schema: dict) -> dict:
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"][name]
    return schema


def resolve_method(spec: dict, ep: Endpoint) -> str | None:
    """Première méthode du contrat disponible et non dépréciée, sinon première disponible."""
    ops = spec.get("paths", {}).get(ep.path, {})
    available = [m for m in ep.method.split("|") if m in ops]
    for m in available:
        if not ops[m].get("deprecated"):
            return m
    return available[0] if available else None


def check_spec(spec: dict) -> tuple[list[str], list[str]]:
    """Compare le contrat à la spec du serveur. Retourne (erreurs bloquantes, avertissements)."""
    errors: list[str] = []
    warnings: list[str] = []
    for ep in CONTRACT:
        method = resolve_method(spec, ep)
        if method is None:
            (warnings if ep.optional else errors).append(f"{ep.method.upper()} {ep.path} : endpoint absent")
            continue
        op = spec["paths"][ep.path][method]
        label = f"{method.upper()} {ep.path}"
        state = op.get("x-immich-state")
        if op.get("deprecated") or state == "Deprecated":
            warnings.append(f"{label} : déprécié (toujours fonctionnel)")
        elif state and state != "Stable":
            warnings.append(f"{label} : état {state}")
        if ep.permission and op.get("x-immich-permission") not in (None, ep.permission):
            (warnings if ep.optional else errors).append(f"{label} : permission {op.get('x-immich-permission')} au lieu de {ep.permission}")
        if ep.body_fields:
            content = op.get("requestBody", {}).get("content", {}).get("application/json", {})
            props = _resolve(spec, content.get("schema", {})).get("properties", {})
            errors += [f"{label} : champ '{f}' absent du corps" for f in ep.body_fields if f not in props]
    return errors, warnings
