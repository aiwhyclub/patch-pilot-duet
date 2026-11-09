"""Load an OpenAPI spec and flatten its GET /forecast 200 response schema.

frd-trd.md section 4.1 and 13.2:

    load_spec(path) -> dict                                  S01, S02
    response_schema(spec, path="/forecast") -> dict          S03
    flatten(schema, components) -> FieldMap

FieldMap keys are JSON paths: "$", "$.current_weather",
"$.current_weather.temperature", ... `$ref` values of the form
"#/components/..." are resolved, at any depth.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from patchpilot import errors
from patchpilot.models import FieldEntry, FieldMap

# id(spec dict) -> file path, so response_schema() can name the file in S03.
_SOURCES: dict[int, str] = {}

_REF_PREFIX = "#/components/"
_MAX_REF_DEPTH = 32


def load_spec(path: Path) -> dict:
    """Read a JSON OpenAPI file. S01 if missing, S02 if not valid JSON."""
    path = Path(path)
    if not path.is_file():
        raise errors.fail("S01", path=path)
    try:
        with path.open(encoding="utf-8") as f:
            spec = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise errors.fail("S02", path=path) from None
    if not isinstance(spec, dict):
        raise errors.fail("S02", path=path)
    _SOURCES[id(spec)] = str(path)
    return spec


def response_schema(spec: dict, path: str = "/forecast") -> dict:
    """Return the application/json schema of GET <path> -> 200. S03 if absent."""
    try:
        schema = spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    except (KeyError, TypeError):
        schema = None
    if not isinstance(schema, dict):
        raise errors.fail("S03", path=_SOURCES.get(id(spec), "<spec>"))
    return schema


def _resolve(node: Any, components: dict, depth: int = 0) -> Any:
    """Follow "$ref": "#/components/<section>/<name>" until a concrete schema."""
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        if not ref.startswith(_REF_PREFIX) or depth > _MAX_REF_DEPTH:
            return {}
        target: Any = components
        for part in ref[len(_REF_PREFIX):].split("/"):
            if not isinstance(target, dict) or part not in target:
                return {}
            target = target[part]
        node = target
        depth += 1
    return node


def _type_of(schema: dict) -> str:
    t = schema.get("type")
    if isinstance(t, list):  # OpenAPI 3.1 ["number", "null"]
        t = next((x for x in t if x != "null"), None)
    if isinstance(t, str):
        return t
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return "unknown"


def flatten(schema: dict, components: dict) -> FieldMap:
    """Flatten an object schema into {json_path: FieldEntry}."""
    components = components or {}
    out: FieldMap = {}

    def walk(node: Any, path: str, parent: Optional[str], depth: int) -> None:
        node = _resolve(node, components)
        if not isinstance(node, dict):
            node = {}
        typ = _type_of(node)
        props = node.get("properties") if typ == "object" else None
        props = props if isinstance(props, dict) else {}
        out[path] = FieldEntry(path=path, type=typ, parent=parent, children=sorted(props))
        if depth > _MAX_REF_DEPTH:
            return
        for key in sorted(props):
            walk(props[key], f"{path}.{key}", path, depth + 1)

    walk(schema, "$", None, 0)
    return out


# [integration] frd-trd.md §19 helpers (hkt-spec names).
def components(spec: dict) -> dict:
    return spec.get("components", {}) or {}


def load_field_map(path: Path) -> tuple[FieldMap, str]:
    spec = load_spec(path)
    version = str((spec.get("info") or {}).get("version", ""))
    return flatten(response_schema(spec), components(spec)), version
