"""Versioned case playbooks (``plx-case-playbook/v1``): how an agent investigates one kind of
business question on the shared case context.

A playbook is *data*, not an orchestrator and not an agent registry. The investigating
agent is Claude driving the case-centric MCP tools; the playbook says which of those tools
it may use, what evidence the case must cite, which named computations it must record, which
gaps must be visible when evidence is absent, and which declarative ``success_checks`` a
harness (component F, later) can evaluate against a ``plx.case_get`` view. Nothing here runs
a tool, reads a source or writes anything.

Safety rules enforced at load time (typed errors, never silently repaired):

* unknown keys and duplicate JSON keys are refused at every level;
* ``allowed_tools`` must be a subset of ``CASE_RUNTIME_TOOLS``; a tool whose name looks like
  a business dispatch/approval/send is refused by name even if it were ever added;
* every allowed tool whose ``readOnlyHint`` is false must be listed in ``writes_case_state``
  (and nothing else may be) so the case-state write surface is explicit and reviewable;
* ``authority`` must be exactly ``{business_writes: 0, approvals: "portal"}``;
* success checks, gaps and computations may only refer to declared evidence roles,
  computations and gaps.

* every evidence role carries an ``identity`` rule binding a cited row to the case's business
  subject: ``"any"`` (explicit, no binding), ``{"kind": "case_root", "fields": [...]}`` (a cited
  row is the subject only when one of those row fields, or its handle, equals a case root) or
  ``{"kind": "join", "to_role", "field", "to_field", ["fixed"]}`` (the row's ``field`` equals
  ``to_field`` of a bound row of ``to_role``; ``fixed`` pins other columns). A join chain must end
  at a ``case_root`` role. A playbook definition is deep-frozen and its hash re-verified on use.

``definition_sha256`` is the SHA-256 of the canonical JSON (sorted keys, compact separators,
UTF-8) of the definition as loaded; list order is part of the content.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

SCHEMA = "plx-case-playbook/v1"
PLAYBOOK_DIR = Path(__file__).with_name("playbooks")
# Adopted versions (``operating.semantic_proposals``) live in this directory of a host's
# Workbench state directory (``PLX_WORKBENCH_STATE_DIR`` / ``--state-dir``), never in
# ``PLAYBOOK_DIR``, which is code and only changes with a reviewed commit.
OVERLAY_DIR = "playbooks"

GAP_KINDS = ("absent_in_source", "inaccessible", "stale", "not_ingested", "unknown")
# Gap kinds that claim absence: such an expected gap of a current version names what is
# absent (``property``, schema names as for plx.case_record_gap) and is matched on it,
# never on wording.
ABSENCE_KINDS = frozenset({"absent_in_source"})
PROPERTY_RE = re.compile(r"^[A-Z][A-Za-z0-9]*(\.[a-z][a-z0-9_]*)?$")
# Tool names containing one of these segments would act on a business system; none exist in
# the case tool family and none may ever be allowed by a playbook.
FORBIDDEN_TOOL_SEGMENTS = frozenset({
    "dispatch", "execute", "approve", "approval", "send", "publish", "apply", "submit",
    "write", "post", "deploy", "commit", "run"})
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
RELATION_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
CONDITION_RES = (
    re.compile(r"^always$"),
    re.compile(r"^role_missing:([a-z][a-z0-9_]*)$"),
    re.compile(r"^field_empty:([a-z][a-z0-9_]*):([A-Za-z_][A-Za-z0-9_.]*(,[A-Za-z_][A-Za-z0-9_.]*)*)$"),
    re.compile(r"^chain_inconsistent$"),
    re.compile(r"^dates_unreadable$"),
    re.compile(r"^after_required_date$"),
    re.compile(r"^po_field_empty$"),
    re.compile(r"^po_field_not_po_like$"),
    re.compile(r"^source_not_ingested$"))

# Every expected gap carries ``match``: a recorded gap satisfies it only when its kind equals the
# gap's ``kind``, it cites a resolved row of each ``match.cites`` role's relation and either the
# expected gap's ``property`` equals the recorded gap's (structured; required for absence kinds,
# whose wording is never matched) or, without one, its text contains every ``match.terms`` entry
# (case-insensitive). A recorded gap is never matched by kind alone.
# ``gap_recorded`` is evaluated only when the named gap's ``when`` condition holds for the case.
# check type -> (required params, case_get sections the harness evaluates it over)
CHECK_TYPES: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {
    "every_fact_cites": (frozenset(), ("findings",)),
    "no_uncited_number_in_statements": (frozenset(), ("findings", "gaps", "proposals")),
    "calculation_equals_cited_arithmetic": (frozenset({"calculation"}), ("calculations",)),
    "evidence_cited": (frozenset({"role"}), ("findings", "calculations", "gaps")),
    "gap_recorded": (frozenset({"gap"}), ("gaps",)),
    "proposals_recorded_only": (frozenset(), ("proposals", "verifications")),
    "proposals_cite_support": (frozenset(), ("findings", "proposals")),
    # The role's one cited row has field == equals (numerically) and every source row the
    # role's identity selects agrees with it on the comma-separated ``agree`` fields.
    "basis_row_agrees": (frozenset({"role", "field", "equals", "agree"}),
                         ("findings", "calculations", "gaps")),
    # Rows of the role are summed per ``group`` value, each group alone in its own unit,
    # never across groups (component damage: never one mixed "pack" total).
    "component_sums": (frozenset({"role", "field", "group"}), ("calculations",))}

TOP_KEYS = frozenset({
    "schema", "id", "version", "question_pattern", "allowed_tools", "writes_case_state",
    "required_evidence", "required_computations", "expected_gaps", "authority",
    "success_checks"})
# Paths ``validate`` accepts that no evaluation reads: the version's own identity, and wording
# for the investigating agent and a reviewer. ``name[]`` steps into every item of a list. What a
# version does is its definition without them (``behavioural_body``). Kept in step with what
# ``playbook_eval`` reads by ``test_playbooks.DescriptiveFieldsTests`` (a traced evaluation).
VERSION_FIELDS = ("schema", "version")
DESCRIPTIVE_FIELDS = ("question_pattern", "required_evidence[].key_hint", "expected_gaps[].about")


class PlaybookError(ValueError):
    """Base of every playbook refusal; ``code`` is stable, ``path`` locates the fault."""

    def __init__(self, code: str, path: str = "", message: str = "") -> None:
        self.code, self.path, self.message = code, path, message
        super().__init__(f"{code}" + (f" at {path}" if path else "")
                         + (f": {message}" if message else ""))


class PlaybookSchemaError(PlaybookError):
    """Shape, type, unknown-key or reference error."""


class PlaybookToolError(PlaybookError):
    """Tool allow-list or write-declaration error."""


class PlaybookAuthorityError(PlaybookError):
    """``authority`` is not exactly the permitted value."""


class PlaybookIntegrityError(PlaybookError):
    """A loaded definition no longer matches the hash it was loaded with."""


class PlaybookRegistryError(PlaybookError):
    """Directory load or duplicate id+version error."""


def _case_tools() -> tuple[frozenset[str], frozenset[str]]:
    """(CASE_RUNTIME_TOOLS, those with readOnlyHint false), imported lazily from the server."""
    from . import mcp_server
    tools = frozenset(mcp_server.CASE_RUNTIME_TOOLS)
    writes = frozenset(
        tool["name"] for tool in mcp_server.TOOLS if tool["name"] in tools and
        not (tool.get("annotations") or {}).get("readOnlyHint", False))
    return tools, writes


def _retired_tools() -> frozenset[str]:
    """Host tools that retired into successors (``ontology_tools.HOST_RETIREMENTS`` and
    ``mcp_server.CASE_TOOL_RETIREMENTS``): a published version naming one still loads, as
    a retired version."""
    from .mcp_server import CASE_TOOL_RETIREMENTS
    from .ontology_tools import HOST_RETIREMENTS
    return frozenset(HOST_RETIREMENTS) | frozenset(CASE_TOOL_RETIREMENTS)


def retired_tools(definition: Mapping[str, Any]) -> list[str]:
    """The retired host tools a definition allows (empty for a current version)."""
    return sorted(set(definition.get("allowed_tools", ())) & _retired_tools())


def _object(value: Any, path: str, allowed: frozenset[str],
            required: frozenset[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlaybookSchemaError("expected_object", path)
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PlaybookSchemaError("unknown_key", path, ", ".join(unknown))
    missing = sorted((allowed if required is None else required) - set(value))
    if missing:
        raise PlaybookSchemaError("missing_key", path, ", ".join(missing))
    return value


def _text(value: Any, path: str, *, upper: int = 2000, pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > upper:
        raise PlaybookSchemaError("invalid_text", path)
    if pattern is not None and not pattern.match(value):
        raise PlaybookSchemaError("invalid_format", path, value[:80])
    return value


def _list(value: Any, path: str, *, allow_empty: bool = True) -> list[Any]:
    if not isinstance(value, list) or (not allow_empty and not value) or len(value) > 200:
        raise PlaybookSchemaError("expected_list", path)
    return value


def _unique(names: list[str], path: str) -> None:
    if len(set(names)) != len(names):
        raise PlaybookSchemaError("duplicate_entry", path)


def _check_tool_names(names: list[Any], path: str) -> None:
    for index, name in enumerate(names):
        here = f"{path}[{index}]"
        if not isinstance(name, str) or not name:
            raise PlaybookSchemaError("invalid_text", here)
        segments = set(re.split(r"[._\-]", name.lower()))
        if segments & FORBIDDEN_TOOL_SEGMENTS:
            raise PlaybookToolError("business_dispatch_tool_forbidden", here, name)


def _validate_expression(expression: str, inputs: Mapping[str, Any], path: str) -> None:
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        raise PlaybookSchemaError("invalid_expression", path) from None
    used: set[str] = set()
    summed: set[str] = set()
    bare: set[str] = set()

    def walk(node: ast.AST, in_sum: bool) -> None:
        if isinstance(node, ast.Expression):
            walk(node.body, in_sum)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            walk(node.left, in_sum)
            walk(node.right, in_sum)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            walk(node.operand, in_sum)
        elif isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return
        elif isinstance(node, ast.Name):
            used.add(node.id)
            (summed if in_sum else bare).add(node.id)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "sum" and len(node.args) == 1 and not node.keywords
              and isinstance(node.args[0], ast.Name)):
            walk(node.args[0], True)
        else:
            raise PlaybookSchemaError("invalid_expression", path, type(node).__name__)

    walk(tree, False)
    unknown = sorted(used - set(inputs))
    if unknown:
        raise PlaybookSchemaError("expression_unknown_input", path, ", ".join(unknown))
    repeat = {name for name, spec in inputs.items() if spec["repeat"]}
    if bare & repeat or (summed - repeat):
        raise PlaybookSchemaError("expression_repeat_misuse", path,
                                  "repeat inputs are used only as sum(name)")
    if set(inputs) - used:
        raise PlaybookSchemaError("expression_unused_input", path,
                                  ", ".join(sorted(set(inputs) - used)))


def _validate_identity(identity: Any, path: str) -> None:
    if identity == "any":
        return  # an explicit, reviewable decision not to bind this role to the case subject
    if not isinstance(identity, dict) or identity.get("kind") not in ("case_root", "join"):
        raise PlaybookSchemaError("invalid_identity", path,
                                  'use "any", a case_root rule or a join rule')
    if identity["kind"] == "case_root":
        _object(identity, path, frozenset({"kind", "fields"}))
        fields = _list(identity["fields"], path + ".fields", allow_empty=False)
        for index, field in enumerate(fields):
            _text(field, f"{path}.fields[{index}]", upper=200, pattern=FIELD_RE)
        _unique(fields, path + ".fields")
        return
    _object(identity, path, frozenset({"kind", "to_role", "field", "to_field", "fixed"}),
            frozenset({"kind", "to_role", "field", "to_field"}))
    _text(identity["field"], path + ".field", upper=200, pattern=FIELD_RE)
    _text(identity["to_field"], path + ".to_field", upper=200, pattern=FIELD_RE)
    fixed = identity.setdefault("fixed", {})
    if not isinstance(fixed, dict) or len(fixed) > 8:
        raise PlaybookSchemaError("expected_object", path + ".fixed")
    for key, value in fixed.items():
        _text(key, f"{path}.fixed.{key}", upper=200, pattern=FIELD_RE)
        _text(value, f"{path}.fixed.{key}", upper=200)


def _check_identity_chain(role: str, identities: Mapping[str, Any], roles: list[str],
                          path: str) -> None:
    seen, current = {role}, identities[role]
    while current != "any" and current["kind"] == "join":
        target = current["to_role"]
        if target not in roles:
            raise PlaybookSchemaError("unknown_role", path + ".to_role", str(target)[:80])
        if target in seen:
            raise PlaybookSchemaError("identity_cycle", path, target)
        seen.add(target)
        current = identities[target]
    if current == "any" and identities[role] != "any":
        raise PlaybookSchemaError("identity_chain_unanchored", path,
                                  "a join chain must end at a case_root role")


def validate(definition: Any) -> dict[str, Any]:
    """Strictly validate a parsed definition; return a normalised deep copy."""
    top = _object(copy.deepcopy(definition), "$", TOP_KEYS)
    if top["schema"] != SCHEMA:
        raise PlaybookSchemaError("unsupported_schema", "$.schema", str(top["schema"])[:80])
    _text(top["id"], "$.id", pattern=ID_RE)
    if type(top["version"]) is not int or top["version"] < 1:
        raise PlaybookSchemaError("invalid_version", "$.version", "positive integer")
    _text(top["question_pattern"], "$.question_pattern", upper=1000)

    known, write_tools = _case_tools()
    retired = _retired_tools()
    allowed = _list(top["allowed_tools"], "$.allowed_tools", allow_empty=False)
    _check_tool_names(allowed, "$.allowed_tools")
    declared = _list(top["writes_case_state"], "$.writes_case_state")
    _check_tool_names(declared, "$.writes_case_state")
    _unique(allowed, "$.allowed_tools")
    _unique(declared, "$.writes_case_state")
    for index, name in enumerate(allowed):
        if name not in known and name not in retired:
            raise PlaybookToolError("tool_not_in_case_runtime", f"$.allowed_tools[{index}]", name)
    for index, name in enumerate(declared):
        if name not in allowed:
            raise PlaybookToolError("write_tool_not_allowed", f"$.writes_case_state[{index}]", name)
        if name not in write_tools and name not in retired:
            raise PlaybookToolError("write_tool_is_read_only", f"$.writes_case_state[{index}]", name)
    undeclared = sorted(name for name in allowed if name in write_tools and name not in declared)
    if undeclared:
        raise PlaybookToolError("write_tool_not_declared", "$.writes_case_state",
                                ", ".join(undeclared))

    authority = _object(top["authority"], "$.authority", frozenset({"business_writes", "approvals"}))
    if type(authority["business_writes"]) is not int or authority["business_writes"] != 0:
        raise PlaybookAuthorityError("business_writes_must_be_zero", "$.authority.business_writes")
    if authority["approvals"] != "portal":
        raise PlaybookAuthorityError("approvals_must_be_portal", "$.authority.approvals")

    roles: list[str] = []
    identities: dict[str, Any] = {}
    for index, item in enumerate(_list(top["required_evidence"], "$.required_evidence",
                                       allow_empty=False)):
        here = f"$.required_evidence[{index}]"
        item = _object(item, here,
                       frozenset({"role", "relation", "key_hint", "required", "identity"}),
                       frozenset({"role", "relation", "key_hint", "identity"}))
        _text(item["role"], here + ".role", pattern=NAME_RE)
        if item["relation"] != "original":
            _text(item["relation"], here + ".relation", pattern=RELATION_RE)
        _text(item["key_hint"], here + ".key_hint", upper=500)
        if "required" in item and type(item["required"]) is not bool:
            raise PlaybookSchemaError("invalid_boolean", here + ".required")
        roles.append(item["role"])
        identities[item["role"]] = item["identity"]
    _unique(roles, "$.required_evidence")
    for index, item in enumerate(top["required_evidence"]):
        _validate_identity(item["identity"], f"$.required_evidence[{index}].identity")
    for index, item in enumerate(top["required_evidence"]):
        _check_identity_chain(item["role"], identities, roles,
                              f"$.required_evidence[{index}].identity")

    names: list[str] = []
    for index, item in enumerate(_list(top["required_computations"], "$.required_computations")):
        here = f"$.required_computations[{index}]"
        item = _object(item, here, frozenset({"name", "inputs", "expression", "unit"}))
        _text(item["name"], here + ".name", pattern=NAME_RE)
        _text(item["unit"], here + ".unit", upper=32)
        _text(item["expression"], here + ".expression", upper=500)
        inputs = item["inputs"]
        if not isinstance(inputs, dict) or not inputs:
            raise PlaybookSchemaError("expected_object", here + ".inputs")
        for name, spec in inputs.items():
            ihere = f"{here}.inputs.{name}"
            _text(name, ihere, pattern=NAME_RE)
            spec = _object(spec, ihere, frozenset({"role", "field", "unit", "repeat"}),
                           frozenset({"role", "field", "unit"}))
            spec.setdefault("repeat", False)
            if spec["role"] not in roles:
                raise PlaybookSchemaError("unknown_role", ihere + ".role", str(spec["role"])[:80])
            _text(spec["field"], ihere + ".field", upper=200, pattern=FIELD_RE)
            _text(spec["unit"], ihere + ".unit", upper=32)
            if type(spec["repeat"]) is not bool:
                raise PlaybookSchemaError("invalid_boolean", ihere + ".repeat")
        _validate_expression(item["expression"], inputs, here + ".expression")
        names.append(item["name"])
    _unique(names, "$.required_computations")

    gap_ids: list[str] = []
    for index, item in enumerate(_list(top["expected_gaps"], "$.expected_gaps")):
        here = f"$.expected_gaps[{index}]"
        item = _object(item, here, frozenset({"id", "kind", "about", "when", "match",
                                              "property"}),
                       frozenset({"id", "kind", "about", "when", "match"}))
        _text(item["id"], here + ".id", pattern=NAME_RE)
        structured = "property" in item
        if structured:
            about = item["property"] if isinstance(item["property"], list) else [item["property"]]
            if not 1 <= len(about) <= 5:
                raise PlaybookSchemaError("invalid_property", here + ".property")
            for pindex, name in enumerate(about):
                _text(name, f"{here}.property[{pindex}]", upper=200, pattern=PROPERTY_RE)
        elif item.get("kind") in ABSENCE_KINDS and not set(allowed) & retired:
            # An absence is a structured claim (what is absent, from which cited row),
            # attributed and checked by structure, never by wording. A published version
            # that loads as retired keeps its term match.
            raise PlaybookSchemaError("absence_gap_needs_property", here + ".property")
        match = _object(item["match"], here + ".match", frozenset({"terms", "cites"}),
                        frozenset() if structured else frozenset({"terms"}))
        if "terms" in match:
            terms = _list(match["terms"], here + ".match.terms", allow_empty=False)
            for tindex, term in enumerate(terms):
                _text(term, f"{here}.match.terms[{tindex}]", upper=120)
            _unique([term.lower() for term in terms], here + ".match.terms")
        cites = _list(match.setdefault("cites", []), here + ".match.cites")
        for cindex, role in enumerate(cites):
            if role not in roles:
                raise PlaybookSchemaError("unknown_role", f"{here}.match.cites[{cindex}]",
                                          str(role)[:80])
        if item["kind"] not in GAP_KINDS:
            raise PlaybookSchemaError("unknown_gap_kind", here + ".kind", str(item["kind"])[:80])
        _text(item["about"], here + ".about", upper=500)
        when = _text(item["when"], here + ".when", upper=300)
        for pattern in CONDITION_RES:
            found = pattern.match(when)
            if found:
                if pattern.pattern.startswith(("^role_missing", "^field_empty")) \
                        and found.group(1) not in roles:
                    raise PlaybookSchemaError("unknown_role", here + ".when", found.group(1))
                break
        else:
            raise PlaybookSchemaError("unknown_condition", here + ".when", when[:80])
        gap_ids.append(item["id"])
    _unique(gap_ids, "$.expected_gaps")

    check_ids: list[str] = []
    checked: set[str] = set()
    for index, item in enumerate(_list(top["success_checks"], "$.success_checks",
                                       allow_empty=False)):
        here = f"$.success_checks[{index}]"
        item = _object(item, here, frozenset({"id", "type", "params"}),
                       frozenset({"id", "type"}))
        _text(item["id"], here + ".id", pattern=NAME_RE)
        if item["type"] not in CHECK_TYPES:
            raise PlaybookSchemaError("unknown_check_type", here + ".type", str(item["type"])[:80])
        params = item.setdefault("params", {})
        required = CHECK_TYPES[item["type"]][0]
        params = _object(params, here + ".params", required)
        for key, value in params.items():
            _text(value, f"{here}.params.{key}", upper=64)
        if "calculation" in params and params["calculation"] not in names:
            raise PlaybookSchemaError("unknown_calculation", here + ".params.calculation",
                                      params["calculation"])
        if "role" in params and params["role"] not in roles:
            raise PlaybookSchemaError("unknown_role", here + ".params.role", params["role"])
        if "gap" in params and params["gap"] not in gap_ids:
            raise PlaybookSchemaError("unknown_gap", here + ".params.gap", params["gap"])
        check_ids.append(item["id"])
        if item["type"] == "calculation_equals_cited_arithmetic":
            checked.add(params["calculation"])
    _unique(check_ids, "$.success_checks")
    unchecked = [name for name in names if name not in checked]
    if unchecked:
        raise PlaybookSchemaError("computation_not_checked", "$.required_computations",
                                  ", ".join(unchecked)[:80])
    return json.loads(json.dumps(top))


def canonical_json(definition: Any) -> bytes:
    return json.dumps(definition, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def definition_sha256(definition: Any) -> str:
    return hashlib.sha256(canonical_json(definition)).hexdigest()


def _drop(value: Any, steps: list[str]) -> None:
    key, many = steps[0].removesuffix("[]"), steps[0].endswith("[]")
    if not isinstance(value, dict) or key not in value:
        return
    if len(steps) == 1:
        del value[key]
        return
    for item in (value[key] if many else [value[key]]):
        _drop(item, steps[1:])


def behavioural_body(definition: Mapping[str, Any],
                     dropped: Sequence[str] | None = None) -> dict[str, Any]:
    """A validated definition without its ``VERSION_FIELDS`` and ``DESCRIPTIVE_FIELDS``: two
    versions with the same body (and so the same ``id``) investigate and score a case alike.
    ``dropped`` names another, pinned set of paths to drop instead."""
    body = copy.deepcopy(dict(definition))
    for path in (VERSION_FIELDS + DESCRIPTIVE_FIELDS if dropped is None else dropped):
        _drop(body, path.split("."))
    return body


def _refuse(self: Any, *args: Any, **kwargs: Any) -> Any:
    raise TypeError("playbook definitions are immutable; build a new Playbook from a copy")


class _FrozenDict(dict):
    """A dict that refuses in-place change; ``copy.deepcopy`` yields a plain mutable dict."""
    __setitem__ = __delitem__ = update = pop = popitem = clear = setdefault = __ior__ = _refuse

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, Any]:
        return {key: copy.deepcopy(value, memo) for key, value in self.items()}

    def __copy__(self) -> dict[str, Any]:
        return dict(self)


class _FrozenList(list):
    __setitem__ = __delitem__ = append = extend = insert = pop = remove = clear = _refuse
    sort = reverse = __iadd__ = __imul__ = _refuse

    def __deepcopy__(self, memo: dict[int, Any]) -> list[Any]:
        return [copy.deepcopy(value, memo) for value in self]

    def __copy__(self) -> list[Any]:
        return list(self)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    return value


class Playbook:
    """A validated, deep-frozen definition with its content hash.

    In-place mutation raises ``TypeError``; a change that bypasses that (``dict.__setitem__``)
    makes the next ``definition_sha256``/``verify`` call raise ``PlaybookIntegrityError``
    because the hash is recomputed from the live definition on every use.
    """

    __slots__ = ("_definition", "_sha256")

    def __init__(self, definition: Any) -> None:
        self._definition = _freeze(validate(definition))
        self._sha256: str = definition_sha256(self._definition)

    def verify(self) -> str:
        """The hash, after proving the definition still equals the one loaded."""
        current = definition_sha256(self._definition)
        if current != self._sha256:
            raise PlaybookIntegrityError(
                "definition_changed_after_load", f"{self._definition.get('id')}",
                f"loaded {self._sha256[:12]}, now {current[:12]}")
        return self._sha256

    @property
    def definition(self) -> dict[str, Any]:
        self.verify()
        return self._definition

    @property
    def definition_sha256(self) -> str:
        return self.verify()

    id = property(lambda self: self.definition["id"])
    version = property(lambda self: self.definition["version"])

    @property
    def retired_tools(self) -> list[str]:
        """Retired host tools this version allows: such a version is kept (its hash binds
        stored reports and adoption ledgers) but never evaluated; a newer version names
        the successors."""
        return retired_tools(self.definition)

    @property
    def retired(self) -> bool:
        return bool(self.retired_tools)

    def __repr__(self) -> str:
        return f"Playbook({self.id} v{self.version} {self.definition_sha256[:12]})"


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlaybookSchemaError("duplicate_json_key", "$", key[:80])
        result[key] = value
    return result


def _no_constant(name: str) -> Any:
    raise PlaybookSchemaError("invalid_json_constant", "$", name)


def parse_playbook(text: str | bytes) -> Playbook:
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except PlaybookError:
        raise
    except (ValueError, UnicodeDecodeError):
        raise PlaybookSchemaError("invalid_json", "$") from None
    return Playbook(value)


def load_playbook(path: str | Path) -> Playbook:
    try:
        data = Path(path).read_bytes()
    except OSError:
        raise PlaybookRegistryError("unreadable_file", str(path)) from None
    return parse_playbook(data)


class PlaybookRegistry:
    """Playbooks by (id, version). Two different definitions of one id+version are refused."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, int], Playbook] = {}
        self._sources: dict[tuple[str, int], list[str]] = {}

    def add(self, playbook: Playbook, source: str = "") -> Playbook:
        key = (playbook.id, playbook.version)
        existing = self._items.get(key)
        if existing is not None and existing.definition_sha256 != playbook.definition_sha256:
            raise PlaybookRegistryError(
                "conflicting_definition", f"{playbook.id} v{playbook.version}",
                f"{existing.definition_sha256[:12]} (from {self._sources[key][0]}) differs from "
                f"{playbook.definition_sha256[:12]} (from {source or 'caller'}); bump the version")
        if existing is None:
            self._items[key] = playbook
            self._sources[key] = []
        self._sources[key].append(source)
        return self._items[key]

    def load_directory(self, directory: str | Path = PLAYBOOK_DIR) -> "PlaybookRegistry":
        folder = Path(directory)
        if not folder.is_dir():
            raise PlaybookRegistryError("not_a_directory", str(folder))
        for path in sorted(folder.glob("*.json")):
            try:
                self.add(load_playbook(path), path.name)
            except PlaybookRegistryError:
                raise
            except PlaybookError as error:
                raise type(error)(error.code, f"{path.name}:{error.path}", error.message) from None
        return self

    def load_layers(self, shipped: str | Path = PLAYBOOK_DIR,
                    overlay: str | Path | None = None) -> "PlaybookRegistry":
        """The playbooks in force, the one loader of adopted versions: the shipped directory,
        then the overlay of adopted versions (``overlay_dir``) when it exists (none yet: no
        adoption). An overlay file is one more version of its id, so the latest version wins
        whichever layer holds it; an overlay never redefines a shipped version
        (``conflicting_definition``)."""
        self.load_directory(shipped)
        if overlay is not None and Path(overlay).exists():
            self.load_directory(overlay)
        return self

    def get(self, playbook_id: str, version: int | None = None) -> Playbook:
        """One version; without ``version`` the latest one that is not retired (else the
        latest)."""
        versions = sorted(v for (i, v) in self._items if i == playbook_id)
        if not versions:
            raise PlaybookRegistryError("unknown_playbook", playbook_id[:64])
        current = [v for v in versions if not self._items[(playbook_id, v)].retired]
        chosen = (current or versions)[-1] if version is None else version
        if (playbook_id, chosen) not in self._items:
            raise PlaybookRegistryError("unknown_version", f"{playbook_id} v{chosen}")
        return self._items[(playbook_id, chosen)]

    def listing(self) -> list[dict[str, Any]]:
        return [{"id": i, "version": v, "definition_sha256": p.definition_sha256,
                 "sources": list(self._sources[(i, v)]), "retired": p.retired}
                for (i, v), p in sorted(self._items.items())]

    def __len__(self) -> int:
        return len(self._items)


def overlay_dir(state_dir: str | Path) -> Path:
    """The overlay of adopted playbook versions in a Workbench state directory."""
    return Path(state_dir) / OVERLAY_DIR


def _keyed(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {item[key]: item for item in items}


def _section(old: list[dict[str, Any]], new: list[dict[str, Any]], key: str) -> dict[str, Any]:
    a, b = _keyed(old, key), _keyed(new, key)
    return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
            "changed": sorted(name for name in set(a) & set(b) if a[name] != b[name])}


def _weakening(a: Mapping[str, Any], b: Mapping[str, Any], checks: Mapping[str, Any],
               evidence: Mapping[str, Any], computations: Mapping[str, Any],
               gaps: Mapping[str, Any]) -> list[str]:
    """Why the new version asks less of a case than the old one (empty when it does not)."""
    reasons = [f"check_removed:{i}" for i in checks["removed"]]
    reasons += [f"check_changed:{i}" for i in checks["changed"]]
    reasons += [f"evidence_removed:{i}" for i in evidence["removed"]]
    old_ev, new_ev = _keyed(a["required_evidence"], "role"), _keyed(b["required_evidence"], "role")
    for role in sorted(set(old_ev) & set(new_ev)):
        if old_ev[role].get("required", True) and not new_ev[role].get("required", True):
            reasons.append(f"evidence_required_to_optional:{role}")
        if old_ev[role]["relation"] != new_ev[role]["relation"]:
            reasons.append(f"evidence_relation_changed:{role}")
        old_id, new_id = old_ev[role]["identity"], new_ev[role]["identity"]
        if old_id != new_id:
            # any -> rule tightens; a rule -> any loosens; one rule -> another is unproven.
            if new_id == "any":
                reasons.append(f"evidence_identity_loosened:{role}")
            elif old_id != "any":
                reasons.append(f"evidence_identity_changed:{role}")
    reasons += [f"computation_removed:{i}" for i in computations["removed"]]
    reasons += [f"computation_changed:{i}" for i in computations["changed"]]
    reasons += [f"gap_removed:{i}" for i in gaps["removed"]]
    old_gaps, new_gaps = _keyed(a["expected_gaps"], "id"), _keyed(b["expected_gaps"], "id")
    for ident in sorted(set(old_gaps) & set(new_gaps)):
        for key in ("kind", "when", "match", "property"):
            if old_gaps[ident].get(key) != new_gaps[ident].get(key):
                reasons.append(f"gap_{key}_changed:{ident}")
    return reasons


def diff(old: Playbook, new: Playbook) -> dict[str, Any]:
    """What changed between two versions of one playbook, for review before adoption.

    ``widens_authority`` is true when the new definition allows more tools or more
    case-state writes; ``authority_changed`` cannot be true for a valid definition (it is
    fixed) but is reported so a future schema change cannot hide.
    """
    if old.id != new.id:
        raise PlaybookRegistryError("different_playbooks", f"{old.id} vs {new.id}")
    a, b = old.definition, new.definition
    tools_added = sorted(set(b["allowed_tools"]) - set(a["allowed_tools"]))
    tools_removed = sorted(set(a["allowed_tools"]) - set(b["allowed_tools"]))
    writes_added = sorted(set(b["writes_case_state"]) - set(a["writes_case_state"]))
    writes_removed = sorted(set(a["writes_case_state"]) - set(b["writes_case_state"]))
    evidence = _section(a["required_evidence"], b["required_evidence"], "role")
    computations = _section(a["required_computations"], b["required_computations"], "name")
    gaps = _section(a["expected_gaps"], b["expected_gaps"], "id")
    checks = _section(a["success_checks"], b["success_checks"], "id")
    reasons = _weakening(a, b, checks, evidence, computations, gaps)
    result = {
        "id": old.id,
        "from": {"version": old.version, "definition_sha256": old.definition_sha256},
        "to": {"version": new.version, "definition_sha256": new.definition_sha256},
        "identical": old.definition_sha256 == new.definition_sha256,
        "question_pattern_changed": a["question_pattern"] != b["question_pattern"],
        "tools": {"added": tools_added, "removed": tools_removed},
        "writes_case_state": {"added": writes_added, "removed": writes_removed},
        "required_evidence": evidence, "required_computations": computations,
        "expected_gaps": gaps, "success_checks": checks,
        "authority_changed": a["authority"] != b["authority"],
        "widens_authority": bool(tools_added or writes_added),
        "weakening_reasons": reasons,
        "weakens_checks": bool(reasons)}
    return result


__all__ = ["CHECK_TYPES", "GAP_KINDS", "OVERLAY_DIR", "Playbook", "PlaybookAuthorityError",
           "PlaybookError", "PlaybookIntegrityError", "PlaybookRegistry", "PlaybookRegistryError",
           "PlaybookSchemaError", "PlaybookToolError", "SCHEMA", "behavioural_body",
           "canonical_json", "definition_sha256", "diff", "load_playbook", "overlay_dir",
           "parse_playbook", "validate"]
