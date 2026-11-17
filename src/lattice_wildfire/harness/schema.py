"""Request-body validation against the vendored Lattice REST OpenAPI component schemas."""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPEC_URI = "urn:lattice-rest-openapi"


def _spec_path() -> Path:
    override = os.environ.get("LW_LATTICE_OPENAPI")
    if override:
        return Path(override).expanduser()
    # src/lattice_wildfire/harness/schema.py -> repo root / vendor (editable install, as uv sync does)
    return Path(__file__).resolve().parents[3] / "vendor" / "lattice-rest-openapi.json"


def _normalize(node: Any) -> Any:
    """OpenAPI -> JSON Schema: fold OpenAPI 3.0 ``nullable: true`` into a type union, drop OpenAPI-only keys."""
    if isinstance(node, dict):
        out = {
            k: _normalize(v) for k, v in node.items() if k not in ("discriminator", "example", "deprecated")
        }
        if out.pop("nullable", False):
            t = out.get("type")
            if isinstance(t, str):
                out["type"] = [t, "null"]
            elif isinstance(t, list) and "null" not in t:
                out["type"] = [*t, "null"]
            elif "$ref" in out:
                ref = out.pop("$ref")
                out["anyOf"] = [{"$ref": ref}, {"type": "null"}]
        return out
    if isinstance(node, list):
        return [_normalize(v) for v in node]
    return node


@cache
def _spec() -> dict[str, Any]:
    with _spec_path().open(encoding="utf-8") as fh:
        return _normalize(json.load(fh))


@cache
def _registry() -> Registry:
    return Registry().with_resource(
        SPEC_URI, Resource.from_contents(_spec(), default_specification=DRAFT202012)
    )


@cache
def validator(component: str) -> Draft202012Validator:
    if component not in _spec()["components"]["schemas"]:
        raise KeyError(f"no component schema {component!r} in the OpenAPI spec")
    return Draft202012Validator({"$ref": f"{SPEC_URI}#/components/schemas/{component}"}, registry=_registry())


def strip_nulls(value: Any) -> Any:
    """Protobuf JSON treats ``null`` as "unset"; drop nulls the same way before validating or storing."""
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value if v is not None]
    return value


def schema_error(component: str, body: Any) -> str | None:
    """``None`` when ``body`` matches the component schema, else a one-line message."""
    err = best_match(validator(component).iter_errors(body))
    if err is None:
        return None
    where = ".".join(str(p) for p in err.absolute_path) or "<body>"
    message = err.message if len(err.message) <= 300 else err.message[:300] + "..."
    return f"{component} schema violation at {where}: {message}"
