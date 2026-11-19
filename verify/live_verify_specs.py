"""Live-check specs: scrubbing, JSON paths, spec validation, session paths and the artifact
kinds a spec may capture (``verify.live_verify``, which re-exports them)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence
import xml.etree.ElementTree as ET

from verify.live_verify_policy import (
    ALLOWED_MODULES,
    ARTIFACT_PATH,
    CADENCES,
    CAUSES,
    CSDL_ATTRIBUTE,
    CSDL_EDMX,
    CSDL_EXPRESSIONS,
    CSDL_STRUCTURE,
    CSDL_TEXT_EXPRESSIONS,
    DAILY_REPORT_ARG,
    DEFAULT_CADENCE,
    DEFAULT_TIMEOUT,
    ENUM_STRING,
    ENV_NAME,
    EXPECT_KEYS,
    ID_SHAPE,
    MAX_ARG_CHARS,
    MAX_ARTIFACTS,
    MAX_ARTIFACT_TEXT,
    MAX_DETAIL_CHARS,
    MAX_DETAIL_CHARS_CEILING,
    MAX_ITEMS,
    MAX_STRING_CHARS,
    MAX_TIMEOUT_BY_CADENCE,
    MODULE_SHAPE,
    PATH_SEGMENT,
    PROFILE_ID_PREFIX,
    PROFILE_MODULE,
    REGISTRY_NAME_KINDS,
    RELATION_NAME,
    SECRET_ENV_NAME,
    SESSION_ARG,
    SESSION_AUDIENCE_KEYS,
    SESSION_STATE,
    SPEC_KEYS,
    TIERS,
    TIER_BY_PREFIX,
    _EDMX_NS,
    _EDM_NS,
    _SCRUBBERS,
)


class SpecError(ValueError):
    """A spec that the runner refuses to run."""


# ------------------------------------------------------------------ scrubbing

def secret_values(environ: Mapping[str, str]) -> list[str]:
    """Values of credential-named variables, longest first (never printed)."""
    values = {value.strip() for name, value in environ.items()
              if SECRET_ENV_NAME.search(name) and isinstance(value, str)
              and len(value.strip()) >= 6}
    return sorted(values, key=len, reverse=True)


def scrub(text: str, secrets: Sequence[str] = ()) -> str:
    for value in secrets:
        text = text.replace(value, "<redacted>")
    for pattern, replacement in _SCRUBBERS:
        text = pattern.sub(replacement, text)
    if len(text) > MAX_STRING_CHARS:
        text = text[:MAX_STRING_CHARS] + "...<truncated>"
    return text


def scrub_tree(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, Mapping):
        return {scrub(str(k), secrets): scrub_tree(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_tree(v, secrets) for v in value]
    return value


# ------------------------------------------------------------------ JSON paths

def parse_path(path: str) -> list[tuple[str, int]]:
    if not isinstance(path, str) or not path:
        raise SpecError("path must be a non-empty string")
    parts = []
    for segment in path.split("."):
        match = PATH_SEGMENT.fullmatch(segment)
        if match is None:
            raise SpecError(f"invalid path segment {segment!r} in {path!r}")
        parts.append((match.group(1), match.group(2).count("[*]")))
    return parts


def resolve(document: Any, path: str) -> list[Any]:
    """Every value at ``path`` (``[*]`` fans out over lists); [] when absent."""
    current = [document]
    for key, stars in parse_path(path):
        nxt = []
        for node in current:
            if isinstance(node, Mapping) and key in node:
                nxt.append(node[key])
        for _ in range(stars):
            nxt = [item for node in nxt if isinstance(node, list) for item in node]
        current = nxt
    return current


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    return left == right


def summarize(value: Any, *, enums: bool, keys: bool = False, depth: int = 0) -> Any:
    """Counts-only view: numbers, booleans and null pass; strings only as enums.

    Mapping keys are shown only when ``keys`` or ``enums`` allows it and every key is
    enum-shaped; otherwise the mapping is reduced to its key count."""
    keys = keys or enums
    if value is None or isinstance(value, bool) or _is_number(value):
        return value
    if isinstance(value, str):
        if enums and ENUM_STRING.fullmatch(value):
            return value
        return f"<str len={len(value)}>"
    if isinstance(value, list):
        if (len(value) <= MAX_ITEMS and depth < 3 and all(
                x is None or isinstance(x, bool) or _is_number(x)
                or (enums and isinstance(x, str)) for x in value)):
            return [summarize(x, enums=enums, keys=keys, depth=depth + 1) for x in value]
        return {"count": len(value)}
    if isinstance(value, Mapping):
        if (depth >= 3 or len(value) > MAX_ITEMS or not keys
                or not all(isinstance(k, str) and ENUM_STRING.fullmatch(k) for k in value)):
            return {"keys": len(value)}
        return {k: summarize(v, enums=enums, keys=keys, depth=depth + 1)
                for k, v in value.items()}
    return f"<{type(value).__name__}>"


# ------------------------------------------------------------------ specs

def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise SpecError(f"{name} must be a list of non-empty strings")
    return value


def _validate_session_audience(spec: Mapping[str, Any]) -> None:
    """Every ``{session_state}`` path declares its capture audience (module docstring,
    "Session state"), from what the spec itself declares: its ``source``, its credential
    names and the files its command passes; anything else refuses the spec."""
    paths, declared = session_paths(spec), spec.get("session_audience", {})
    if not isinstance(declared, Mapping) or set(declared) != set(paths):
        raise SpecError("session_audience must declare exactly the {session_state} paths "
                        f"of the command: {', '.join(paths) or 'none'}")
    sources = set(str(spec.get("source", "")).split("+"))
    env = {name for key in ("requires_env", "requires_env_any", "optional_env")
           for name in spec.get(key, [])}
    for path, audience in declared.items():
        if not isinstance(audience, Mapping) or set(audience) != SESSION_AUDIENCE_KEYS:
            raise SpecError(f"session_audience {path!r} must be {{sources, env, reader}}")
        if not set(_string_list(audience["sources"], "session_audience sources")) <= sources:
            raise SpecError(f"session_audience {path!r} sources must be in the spec's source")
        if not set(_string_list(audience["env"], "session_audience env")) <= env:
            raise SpecError(f"session_audience {path!r} env must be credentials the spec "
                            "declares")
        reader = audience["reader"]
        if reader != "service" and not set(_string_list(
                reader, "session_audience reader")) <= set(spec["command"][3:]):
            raise SpecError(f"session_audience {path!r} reader must be 'service' or files "
                            "the command passes")
        if not audience["sources"] or not audience["env"] or not reader:
            raise SpecError(f"session_audience {path!r} must name sources, env and reader")


def validate_spec(spec: Any, *, allowed_modules: Iterable[str] = ALLOWED_MODULES,
                  stem: str | None = None) -> list[str]:
    """Raise SpecError if the spec may not run; return warnings (unknown keys)."""
    if not isinstance(spec, Mapping):
        raise SpecError("spec must be a JSON object")
    spec_id = spec.get("id")
    if not isinstance(spec_id, str) or ID_SHAPE.fullmatch(spec_id) is None:
        raise SpecError("id must match " + ID_SHAPE.pattern)
    if stem is not None and stem != spec_id:
        raise SpecError(f"file name {stem!r}.json does not match id {spec_id!r}")
    if not isinstance(spec.get("description"), str) or not spec["description"].strip():
        raise SpecError("description must be a non-empty string")
    command = spec.get("command")
    if (not isinstance(command, list) or len(command) < 3
            or not all(isinstance(arg, str) for arg in command)):
        raise SpecError("command must be an argv list: python3 -m <module> [args...]")
    if command[0] != "python3" or command[1] != "-m":
        raise SpecError("command must start with python3 -m (no shell, no other program)")
    module = command[2]
    if MODULE_SHAPE.fullmatch(module) is None:
        raise SpecError(f"module {module!r} is not an integrations.*, operating.*, "
                        "scripts.* or devloop.* module")
    if module.startswith("devloop.") and module != PROFILE_MODULE:
        raise SpecError(f"module {module!r}: the only devloop module is {PROFILE_MODULE}")
    if "profile" in spec and spec["profile"] is not True:
        raise SpecError("profile must be true when present")
    if (spec.get("profile") is True) != (module == PROFILE_MODULE):
        raise SpecError(f"profile specs (and only they) run {PROFILE_MODULE}")
    if (spec.get("profile") is True) != spec_id.startswith(PROFILE_ID_PREFIX):
        raise SpecError(f"profile spec ids (and only they) start with {PROFILE_ID_PREFIX!r}")
    if module not in set(allowed_modules) and not spec.get("disabled"):
        raise SpecError(f"module {module!r} is not in the live-verify allowlist")
    for arg in command[3:]:
        if not arg or len(arg) > MAX_ARG_CHARS or any(ord(c) < 32 for c in arg):
            raise SpecError("command arguments must be 1..300 printable characters")
        if "{daily_report}" in arg and arg != DAILY_REPORT_ARG:
            raise SpecError(f"{{daily_report}} is allowed only as {DAILY_REPORT_ARG!r}")
        if SESSION_STATE in arg and SESSION_ARG.fullmatch(arg) is None:
            raise SpecError(f"{{session_state}} is allowed only as {{session_state}}/<path> "
                            f"ending an argument, not {arg!r}")
        if re.search(r"\{(?!tmp\}|daily_report\}|session_state\})[^}]*\}", arg):
            raise SpecError(f"unknown placeholder in {arg!r}; only {{tmp}}, {{session_state}} "
                            "and {daily_report} are supported")
        if arg in ("-c", "--shell"):
            raise SpecError(f"argument {arg!r} refused")
    cadence = spec.get("cadence", DEFAULT_CADENCE)
    if cadence not in CADENCES:
        raise SpecError("cadence must be one of: " + ", ".join(CADENCES))
    ceiling = MAX_TIMEOUT_BY_CADENCE[cadence]
    timeout = spec.get("timeout_seconds", DEFAULT_TIMEOUT)
    if not _is_number(timeout) or not 0 < timeout <= ceiling:
        raise SpecError(f"timeout_seconds must be in (0, {ceiling}] for a {cadence} spec")
    if "max_detail_chars" in spec:
        cap = spec["max_detail_chars"]
        if isinstance(cap, bool) or not isinstance(cap, int) or \
                not MAX_DETAIL_CHARS < cap <= MAX_DETAIL_CHARS_CEILING:
            raise SpecError(f"max_detail_chars must be an integer in ({MAX_DETAIL_CHARS}, "
                            f"{MAX_DETAIL_CHARS_CEILING}]")
    for upstream in _string_list(spec.get("requires", []), "requires"):
        if ID_SHAPE.fullmatch(upstream) is None or upstream == spec_id:
            raise SpecError(f"requires entry {upstream!r} must be another spec id")
    for relation in _string_list(spec.get("requires_relations", []), "requires_relations"):
        if RELATION_NAME.fullmatch(relation) is None:
            raise SpecError(f"requires_relations entry {relation!r} must be schema.relation")
    if spec.get("requires_relations") and not spec.get("requires"):
        raise SpecError("requires_relations needs the upstream spec in requires")
    for key in ("requires_env", "requires_env_any", "optional_env"):
        for name in _string_list(spec.get(key, []), key):
            if ENV_NAME.fullmatch(name) is None:
                raise SpecError(f"{key} name {name!r} invalid")
    if not set(_string_list(spec.get("blocked_without_env", []), "blocked_without_env")) <= set(
            spec.get("optional_env", [])):
        raise SpecError("blocked_without_env names must also be in optional_env")
    for key in ("record", "record_keys", "record_enums"):
        for path in _string_list(spec.get(key, []), key):
            parse_path(path)
    _validate_session_audience(spec)
    reads = spec.get("session_reads", [])
    if not set(_string_list(reads, "session_reads")) <= set(session_paths(spec)):
        raise SpecError("session_reads must name {session_state} paths of the command")
    if "session_required" in spec and (spec["session_required"] is not True or not reads):
        raise SpecError("session_required must be true beside session_reads")
    _validate_artifacts(spec)
    if "carry_previous_measured" in spec and (
            spec["carry_previous_measured"] is not True
            or DAILY_REPORT_ARG not in spec["command"]):
        raise SpecError(f"carry_previous_measured must be true beside {DAILY_REPORT_ARG!r}")
    if "blocked_when" in spec:
        if not isinstance(spec["blocked_when"], str):
            raise SpecError("blocked_when must be one path")
        parse_path(spec["blocked_when"])
    if "blocked_cause" in spec and (spec["blocked_cause"] not in CAUSES
                                    or "blocked_when" not in spec):
        raise SpecError(f"blocked_cause must be one of {', '.join(CAUSES)} beside blocked_when")
    causes = spec.get("blocked_causes")
    if causes is not None and (
            "blocked_when" not in spec or not isinstance(causes, dict) or not causes
            or any(not isinstance(k, str) or not k or not ENUM_STRING.fullmatch(k)
                   or v not in CAUSES
                   for k, v in causes.items())):
        raise SpecError("blocked_causes maps blocked strings to causes beside blocked_when")
    if "disabled" in spec and (not isinstance(spec["disabled"], str)
                               or not spec["disabled"].strip()):
        raise SpecError("disabled must be a non-empty reason")
    for legacy in _string_list(spec.get("legacy_ids", []), "legacy_ids"):
        if len(legacy) > 64:
            raise SpecError("legacy_ids entries must be <= 64 characters")
    _validate_expect(spec.get("expect"), "expect")
    if "expect_blocked" in spec:
        _validate_expect(spec["expect_blocked"], "expect_blocked")
    if "complete_when" in spec:
        _validate_equals(spec["complete_when"], "complete_when")
        if not spec["complete_when"]:
            raise SpecError("complete_when must name at least one path")
    if "registry_names" in spec:
        kinds = spec["registry_names"]
        if not isinstance(kinds, Mapping) or not kinds or set(kinds) - set(REGISTRY_NAME_KINDS):
            raise SpecError("registry_names must map some of " + ", ".join(REGISTRY_NAME_KINDS)
                            + " to recorded paths")
        recorded = set(spec.get("record") or ())
        for kind, paths in kinds.items():
            for path in _string_list(paths, f"registry_names.{kind}"):
                if path not in recorded:
                    raise SpecError(f"registry_names.{kind} path {path!r} is not in record")
    if "tier" in spec and spec["tier"] not in TIERS:
        raise SpecError("tier must be one of: " + ", ".join(TIERS))
    if "answer_source" in spec and spec["answer_source"] is not True:
        raise SpecError("answer_source must be true when present")
    if "metric" in spec and spec["metric"] is not True:
        raise SpecError("metric must be true when present")
    return [f"unknown spec key {key!r} ignored" for key in sorted(set(spec) - SPEC_KEYS)]


def _validate_artifacts(spec: Mapping[str, Any]) -> None:
    if "artifacts" not in spec:
        return
    declared = spec["artifacts"]
    if not isinstance(declared, list) or not 0 < len(declared) <= MAX_ARTIFACTS:
        raise SpecError(f"artifacts must list 1..{MAX_ARTIFACTS} {{path, kind}} objects")
    paths = set()
    for item in declared:
        if not isinstance(item, Mapping) or set(item) != {"path", "kind"}:
            raise SpecError("each artifact must be exactly {path, kind}")
        kind, path = item["kind"], item["path"]
        if not isinstance(kind, str) or kind not in ARTIFACT_KINDS:
            raise SpecError("artifact kind must be one of: " + ", ".join(sorted(ARTIFACT_KINDS)))
        found = ARTIFACT_PATH.fullmatch(path) if isinstance(path, str) else None
        if found is None or found.group(2) != ARTIFACT_KINDS[kind][0]:
            raise SpecError(f"artifact path must be {{tmp}}/<name>.{ARTIFACT_KINDS[kind][0]}")
        if path in paths:
            raise SpecError(f"artifact path {path!r} declared twice")
        paths.add(path)
        if not any(arg == path or arg.endswith("=" + path) for arg in spec["command"][3:]):
            raise SpecError(f"artifact path {path!r} is not a command argument")


def _validate_equals(mapping: Any, name: str) -> None:
    if not isinstance(mapping, Mapping):
        raise SpecError(f"{name} must be an object")
    for path, value in mapping.items():
        parse_path(path)
        if not (value is None or isinstance(value, (bool, str)) or _is_number(value)):
            raise SpecError(f"{name} values must be scalars")


def _validate_expect(expect: Any, name: str) -> None:
    if not isinstance(expect, Mapping):
        raise SpecError(f"{name} must be an object")
    unknown = set(expect) - EXPECT_KEYS
    if unknown:  # an unenforced expectation would be a silent pass
        raise SpecError(f"unknown {name} keys: " + ", ".join(sorted(unknown)))
    if not isinstance(expect.get("exit_code"), int) or isinstance(expect["exit_code"], bool):
        raise SpecError(f"{name}.exit_code must be an integer")
    for path in _string_list(expect.get("json_fields"), f"{name}.json_fields"):
        parse_path(path)
    for key in ("json_empty", "json_absent"):
        for path in _string_list(expect.get(key, []), f"{name}.{key}"):
            parse_path(path)
    _validate_equals(expect.get("json_equals", {}), f"{name}.json_equals")
    for key in ("json_min", "json_max"):
        mapping = expect.get(key, {})
        if not isinstance(mapping, Mapping):
            raise SpecError(f"{name}.{key} must be an object")
        for path, value in mapping.items():
            parse_path(path)
            if not _is_number(value):
                raise SpecError(f"{name}.{key} values must be numbers")
    mapping = expect.get("json_same", {})
    if not isinstance(mapping, Mapping):
        raise SpecError(f"{name}.json_same must be an object")
    for path, other in mapping.items():
        parse_path(path)
        if not isinstance(other, str) or "[*]" in path or "[*]" in other:
            raise SpecError(f"{name}.json_same maps one path to one other path")
        parse_path(other)


def spec_tier(spec: Mapping[str, Any] | None, check_id: str = "") -> str:
    """The tier a spec declares, else the one its id prefix maps to (``TIER_BY_PREFIX``)."""
    declared = spec.get("tier") if isinstance(spec, Mapping) else None
    if declared in TIERS:
        return declared
    spec_id = str((spec or {}).get("id") or check_id)
    return next((tier for prefix, tier in TIER_BY_PREFIX if spec_id.startswith(prefix)),
                "access")


def _requires_cycle_members(graph: Mapping[str, Sequence[str]]) -> set[str]:
    """Ids that sit on a ``requires`` cycle (a spec that reaches itself), iteratively."""
    members = set()
    for start in graph:
        seen: set[str] = set()
        stack = list(graph[start])
        while stack:
            node = stack.pop()
            if node == start:
                members.add(start)
                break
            if node not in seen:
                seen.add(node)
                stack.extend(graph.get(node, ()))
    return members


def session_paths(spec: Mapping[str, Any] | None) -> list[str]:
    """The ``{session_state}`` paths a (valid) spec's command names, in order."""
    command = spec.get("command") if isinstance(spec, Mapping) else None
    found = (SESSION_ARG.fullmatch(arg) for arg in command[3:]
             if isinstance(arg, str)) if isinstance(command, list) else ()
    return list(dict.fromkeys(match.group(1) for match in found if match))


def session_key(spec: Mapping[str, Any], path: str) -> str:
    """The session store of one ``{session_state}`` path of a (valid) spec: the path and its
    declared capture audience (``session_audience``), as a 16-hex key."""
    audience = spec["session_audience"][path]
    reader = audience["reader"]
    document = {"path": path, "sources": sorted(audience["sources"]),
                "env": sorted(audience["env"]),
                "reader": reader if reader == "service" else sorted(reader)}
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()[:16]


def session_producers(spec: Mapping[str, Any] | None,
                      specs: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    """Per ``{session_state}`` path of the spec, the specs it ``requires`` that name the same
    path under the same capture audience (its session producers); a path no required spec
    names so is the spec's own."""
    required = [up for up in (spec or {}).get("requires", []) if up in specs]
    return {path: [up for up in required if path in session_paths(specs[up])
                   and session_key(specs[up], path) == session_key(spec, path)]
            for path in session_paths(spec)}


def blocking_requires(spec: Mapping[str, Any] | None,
                      specs: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The ``requires`` entries that block the spec when they do not pass: all but its
    session producers (a consumer of a failed producer captures cold instead)."""
    producers = {up for ups in session_producers(spec, specs).values() for up in ups}
    return [up for up in (spec or {}).get("requires", []) if up not in producers]


def reads_producers(spec: Mapping[str, Any] | None,
                    specs: Mapping[str, Mapping[str, Any]]) -> bool:
    """The spec cannot run cold (``session_required``) and only reads (``session_reads``)
    every path a session producer of it names: it never runs cold for want of budget."""
    consumed = [path for path, ups in session_producers(spec, specs).items() if ups]
    return ((spec or {}).get("session_required") is True and bool(consumed)
            and set(consumed) <= set(spec.get("session_reads", ())))


def filled_session(check: Mapping[str, Any] | None) -> bool:
    """A producer's run filled its session paths: it ran to its own exit (not timed out,
    refused, skipped or blocked before running) and printed a JSON report that names no
    ``error``. A missed expectation does not void what it captured."""
    detail = (check or {}).get("detail") or {}
    fields = detail.get("fields")
    return ((check or {}).get("result") in ("pass", "fail", "blocked")
            and (check or {}).get("exit_code") is not None and not detail.get("timed_out")
            and isinstance(fields, Mapping) and not fields.get("error"))


def csdl_refusal(raw: bytes) -> str | None:
    """None when ``raw`` is a schema-only OData CSDL document (``bc_metadata_csdl``), else
    the fixed refusal code. Every element is CSDL (an entity instance is not), attributes
    are plain names, text sits only in annotation constants; no DTD, comment or PI."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "not_utf8"
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return "doctype"
    body = re.sub(r"\A\ufeff?\s*<\?xml[^>]*\?>", "", text)
    if "<!--" in body or "<?" in body:  # outside the root ElementTree drops them unseen
        return "comment_or_pi"
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return "not_xml"
    if root.tag != f"{{{_EDMX_NS}}}Edmx":
        return "not_csdl"
    if root.find(f"{{{_EDMX_NS}}}DataServices/{{{_EDM_NS}}}Schema") is None:
        return "no_schema"
    stack = [(root, False)]
    while stack:
        element, in_annotation = stack.pop()
        ns, _, local = element.tag[1:].partition("}") if element.tag[:1] == "{" else ("", "", "")
        expression = ns == _EDM_NS and local in CSDL_EXPRESSIONS
        if not ((ns == _EDMX_NS and local in CSDL_EDMX)
                or (ns == _EDM_NS and local in CSDL_STRUCTURE)
                or (expression and in_annotation)):
            return "foreign_element"
        if not all(CSDL_ATTRIBUTE.fullmatch(name) for name in element.attrib):
            return "foreign_attribute"
        value = (element.text or "").strip()
        if (value and not (expression and local in CSDL_TEXT_EXPRESSIONS)) \
                or (element.tail or "").strip():
            return "text_content"
        if len(value) > MAX_ARTIFACT_TEXT:
            return "text_length"
        stack.extend((child, in_annotation or local == "Annotation") for child in element)
    return None


# kind -> (file extension, validator: None when the bytes are that kind, else a code)
ARTIFACT_KINDS = {"bc_metadata_csdl": ("xml", csdl_refusal)}
