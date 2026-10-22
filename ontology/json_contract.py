"""Stdlib JSON Schema validator for the C0 contracts (no third-party dependency).

Supports the subset of JSON Schema 2020-12 the contracts use: ``type``, ``enum``,
``const``, ``pattern``, ``minLength``/``maxLength``, ``minimum``/``maximum``,
``properties``/``required``/``additionalProperties``/``patternProperties``/
``propertyNames``/``minProperties``/``maxProperties``, ``items``/``minItems``/
``maxItems``/``uniqueItems``/``contains``, ``allOf``/``anyOf``/``oneOf``/``not``,
``if``/``then``/``else`` and local ``$ref`` (``#/...`` JSON pointers into the root
document). Any other keyword is refused when the schema is checked, so a
contract cannot silently rely on a keyword this validator ignores. Annotation
keywords (``description``, ``title``, ``$comment``, ``$schema``, ``$id``,
``$defs``, ``examples``, ``default``, ``format``) are accepted and not enforced.

Usage: ``errors = validate(instance, schema, root=document)``; an empty list
means valid. Errors are ``"<json path>: <message>"`` strings, at most ``limit``.
This module imports nothing from the rest of the repository.
"""

from __future__ import annotations

import re
from typing import Any

ANNOTATIONS = frozenset({
    "description", "title", "$comment", "$schema", "$id", "$defs", "examples", "default",
    "format",
})
ASSERTIONS = frozenset({
    "type", "enum", "const", "pattern", "minLength", "maxLength", "minimum", "maximum",
    "properties", "required", "additionalProperties", "patternProperties", "propertyNames",
    "minProperties", "maxProperties", "items", "minItems", "maxItems", "uniqueItems",
    "contains", "allOf", "anyOf", "oneOf", "not", "if", "then", "else", "$ref",
})
_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class SchemaError(ValueError):
    """The schema itself uses an unsupported keyword or a broken reference."""


def _pointer(root: Any, ref: str) -> Any:
    if not ref.startswith("#"):
        raise SchemaError(f"only local $ref is supported: {ref}")
    node = root
    for raw in [p for p in ref[1:].split("/") if p]:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            raise SchemaError(f"unresolvable $ref {ref}")
    return node


def check_schema(schema: Any, root: Any | None = None, _path: str = "#") -> None:
    """Raise SchemaError when ``schema`` uses an unsupported keyword or bad $ref."""
    root = schema if root is None else root
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise SchemaError(f"{_path}: schema must be an object or boolean")
    unknown = set(schema) - ANNOTATIONS - ASSERTIONS
    if unknown:
        raise SchemaError(f"{_path}: unsupported keyword(s) {sorted(unknown)}")
    if "$ref" in schema:
        _pointer(root, schema["$ref"])
    if "pattern" in schema:
        re.compile(schema["pattern"])
    for key in ("properties", "patternProperties", "$defs"):
        for name, sub in (schema.get(key) or {}).items():
            if key == "patternProperties":
                re.compile(name)
            check_schema(sub, root, f"{_path}/{key}/{name}")
    for key in ("additionalProperties", "propertyNames", "items", "contains", "not", "if", "then",
                "else"):
        if key in schema:
            check_schema(schema[key], root, f"{_path}/{key}")
    for key in ("allOf", "anyOf", "oneOf"):
        for index, sub in enumerate(schema.get(key) or []):
            check_schema(sub, root, f"{_path}/{key}/{index}")


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def _errors(value: Any, schema: Any, root: Any, path: str, out: list[str], limit: int) -> None:
    if len(out) >= limit:
        return
    if schema is True:
        return
    if schema is False:
        out.append(f"{path}: no value is allowed here")
        return
    if "$ref" in schema:
        _errors(value, _pointer(root, schema["$ref"]), root, path, out, limit)
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        if not any(_TYPES[name](value) for name in names):
            out.append(f"{path}: expected type {'/'.join(names)}")
            return
    if "const" in schema and not _equal(value, schema["const"]):
        out.append(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and not any(_equal(value, item) for item in schema["enum"]):
        out.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            out.append(f"{path}: does not match {schema['pattern']}")
    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: above maximum {schema['maximum']}")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                out.append(f"{path}: missing required property {name!r}")
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            out.append(f"{path}: fewer than {schema['minProperties']} properties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            out.append(f"{path}: more than {schema['maxProperties']} properties")
        properties = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        for name, item in value.items():
            if "propertyNames" in schema:
                _errors(name, schema["propertyNames"], root, f"{path}.{name}<name>", out, limit)
            matched = False
            if name in properties:
                matched = True
                _errors(item, properties[name], root, f"{path}.{name}", out, limit)
            for pattern, sub in patterns.items():
                if re.search(pattern, name):
                    matched = True
                    _errors(item, sub, root, f"{path}.{name}", out, limit)
            if not matched and "additionalProperties" in schema:
                extra = schema["additionalProperties"]
                if extra is False:
                    out.append(f"{path}: unexpected property {name!r}")
                else:
                    _errors(item, extra, root, f"{path}.{name}", out, limit)
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: more than {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            seen: list[Any] = []
            for item in value:
                if any(_equal(item, other) for other in seen):
                    out.append(f"{path}: items are not unique")
                    break
                seen.append(item)
        if "items" in schema:
            for index, item in enumerate(value):
                _errors(item, schema["items"], root, f"{path}[{index}]", out, limit)
        if "contains" in schema and not any(
                not validate(item, schema["contains"], root=root, limit=1) for item in value):
            out.append(f"{path}: no item matches contains")
    for sub in schema.get("allOf", []):
        _errors(value, sub, root, path, out, limit)
    for key in ("anyOf", "oneOf"):
        if key not in schema:
            continue
        branches = [_branch(value, sub, root, path) for sub in schema[key]]
        matches = sum(1 for errors in branches if not errors)
        if matches == 0:
            # Report the closest branch's errors so a near-miss names its real fault.
            closest = min(branches, key=len)
            out.append(f"{path}: matches none of {key}; closest branch: " + "; ".join(closest[:5]))
        elif key == "oneOf" and matches > 1:
            out.append(f"{path}: matches {matches} of oneOf (expected exactly 1)")
    if "not" in schema and not validate(value, schema["not"], root=root, limit=1):
        out.append(f"{path}: matches a forbidden schema")
    if "if" in schema:
        branch = "then" if not validate(value, schema["if"], root=root, limit=1) else "else"
        if branch in schema:
            _errors(value, schema[branch], root, path, out, limit)


def _branch(value: Any, schema: Any, root: Any, path: str) -> list[str]:
    out: list[str] = []
    _errors(value, schema, root, path, out, 20)
    return out


def validate(value: Any, schema: Any, *, root: Any | None = None, limit: int = 50) -> list[str]:
    """Return validation errors of ``value`` against ``schema`` (empty = valid)."""
    out: list[str] = []
    _errors(value, schema, schema if root is None else root, "$", out, limit)
    return out[:limit]
