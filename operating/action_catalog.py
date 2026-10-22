"""The typed action catalog: data that feeds the goal-2 action path.

``operating/action_catalog.json`` (``plx-action-catalog/v1``) declares every action
type the Workbench knows. This module validates that file and runs its generic
checks; nothing here names a kind of record or a transaction.

Two kinds:

* ``workbench_internal``: identity link reviews, corrections, follow-ups, drafts.
  ``ActionCatalog.execute`` runs one now: typed, traced parameters, data-declared
  preconditions checked here, then exactly one write to the Workbench's own case
  store (``operating.cases.CaseStore``: an allow-listed operation), then a
  read-back of that store as the acting reader. No approval: nothing outside the
  Workbench is written. The case store keeps its own checks (readable case,
  citations resolve for the actor, closed case refused); its refusal is certain
  (validated before its transaction appends anything).
* ``owner_system_proposal``: a write the owning system must make. It is never
  executed here. ``operating.action_path.ActionPath.register`` calls
  ``registration_refusal`` (the catalog hook): an unknown type, an internal type,
  untyped or untraced parameters, or an expected effect that does not set the
  traced value refuse before an intent exists. The rest is the action path:
  MC approval gate, dispatch refused for every real owner until a typed write
  surface exists (``goal2-real-capability``), read-back of the owner record.

Parameter traces (parameters trace to cited values or the
user's objective): ``cited_value`` (equals the trimmed text of a field of a cited,
unchanged record), ``cited_field`` (a field present in that record), ``citation``
(a citation handle; for proposals one of the proposal's own, resolving unchanged),
``objective`` (written as a whole token in a human-written case question, never a
subject identifier or a root: ``injection_guard.objective_supplies``),
``argument_provenance`` (the owner value parameter only: it must equal the expected
effect's value, which ``ActionPath.register``'s injection hook traces to a typed cell
of a cited structured record or the human-written question), ``case_record`` (an id of a
record already on the case), ``declared_set`` (a declared value list, or a set the
store holds and checks; or, ``checked_by: gap_guard``, schema names the gap guard
resolves), ``effect`` (restates the proposal's expected effect) and,
for internal actions only, ``actor_statement`` (the acting person's own words: the
store records them under their label; never a target value and never a fact).

Absence (``gap_guard``, internal only): every internal action that writes a case gap
declares it, so ``absent_in_source`` passes the one absence prover
(``capability_guards.check_gap``) the ``plx.case_record_gap`` route runs, over the
cited rows as the acting reader reads them now (``execute``'s ``read`` and ``schema``).
Without a reader an absence is not shown and is refused; other states write as before.

Preconditions are one entry shape (``{"id", "check", ...}``). An owner-system type
lists its capability guards among them (the generic checks of
``operating/capability_guards.py``, ``GUARD_CHECKS``); they run where proposals are
recorded (``plx.case_propose``) and registered (``ActionPath.register``, the one
registration enforcement point) and in the playbook evaluator. A ``person_confirms``
precondition (``statement``, optional ``evidence_link`` label) is recorded as a prose
precondition on every proposal of the type: a condition a person confirms. This file is their one source (decision action-catalog-format,
option b). Each owner-system type binds ``target_relations`` (exact relation names,
or ``fixture.*`` for the isolated owner) and ``hosts`` (``case_host``; ``test``: a
host built with test capabilities over a fixture session). The test-host types the
isolated owner runs are ``test_host_isolated_owner_ids`` (the action path's
``FIXTURE_CAPABILITIES``). ``registration.refused`` declares a type that may be
recorded and guarded but never becomes an intent. The expected effect names one value
per field (``values``); ``effect.value_parameter`` sets every field to that
parameter, ``effect.field_parameters`` maps each field to its own parameter.

Field roles (trace roles, ``injection_guard.cited_role``): every owner-system value
parameter declares the ``role`` of what it writes, or the type does not load
(``catalog_parameter_role_undeclared``). A role is a value type plus what the type
leaves open (``identifier:<kind declared in identifiers.json>``,
``quantity:<quantity role>``, ``money:amount|unit_price``) or ``owner_declared``: the
role the owning record declares for the written field (schema bindings of ``api://``
and ``bc://`` rows, ``field_roles`` of a ``fixture://`` owner), which the action
path's injection hook enforces on the written value. At registration an owner write
fails closed on roles: every written field of the cited target must declare a role
(``effect_field_role_undeclared``) that fits a fixed parameter role
(``effect_field_role_mismatch``), and a ``cited_value`` must be read from a cell that
declares a role (``parameter_role_undeclared_source``) fitting the role it writes
(``parameter_role_mismatch``: a sales-order or Petra PO number offered as the customer
PO). Only a ``fixture://`` row of a ``fixture.*`` relation, cited for a fixture or
test owner, that declares no field roles at all is exempt (test fixtures seeded
without them; the isolated owner declares its fields): a real owner's type never
binds or reads one. A cell's role is the one ``injection_guard.cited_role`` gives
(the one role rule; for a written field, of the value written): a structured column
the schema reads both as raw text and as an identifier takes the kinds whose
identifier ``carriers`` declare it (identifiers.json, with the upstream citation of
what the column holds), and the identifier grammar decides among them per value; an
ambiguous one fits no role (an externalDocumentNumber holding a sales-order number is
never a customer PO). The written roles also feed ``injection_guard.effect_trace``
(``roles``): an equal cell of another role than the parameter's never supplies it.

Mission Control writes (``executor.dispatch: mc_write_door``, owner ``mc``): the one real
owner written today is MC itself. The type's ``mc_call`` names the exact MC write tool
(``MC_WRITE_TOOLS``, a code constant) and, per argument, where its value comes from
(``param:<name>``, ``host:<setting>``, ``intent:idempotency_key`` / ``intent:reference`` /
``intent:cited_sources``: each record the proposal cites, with its citation, and its text
only where every member of the target bucket's audience may read it;
joined by a blank line when several) and its ``role``; an argument without a declared role,
or an argument the tool does not take, does not load. Its parameters trace to a case gap
(``case_gap``: equal to a field of a gap recorded on the case, current for the reader) or a
declared set; it takes no expected effect on a cited row, and its proposal cites every
citation of a case record a parameter names (``record_citations_not_on_proposal``), so the
sources the call quotes are the ones that record rests on. ``mc_call`` builds the exact call
a person approves; ``operating.action_path`` sends it.

No real capability writes a Portal route that raises or posts a Business Central financial
document (``BC_FINANCIAL_ROUTES``: invoices, credit memos, payments, AP bills; BC posts
them, the Portal raises only drafts): such a type does not load, whatever its approval.

Evidence label: ``workbench-store`` for internal executions (the Workbench's own
state), whatever the cited sources are; owner-system proposals keep the action
path's ``isolated-fixture`` label (no real owner dispatches).
"""

from __future__ import annotations

import copy
from datetime import date
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from ontology import identifiers
from ontology.types import Actor, ValidationError

from . import injection_guard
from .capability_guards import (DOCUMENT_SCHEMES, Read, check_gap,
                                read_field)  # one field-path reading
from .cases import WITHHELD, CaseNotFound, CaseStore, CitationError, actor_label, effect_values
from .quantity_types import QUANTITY_TYPES

CATALOG_PATH = Path(__file__).with_name("action_catalog.json")
SCHEMA = "plx-action-catalog/v1"
EXECUTION_SCHEMA = "plx-action-execution/v1"
EVIDENCE_LABEL = "workbench-store"
KINDS = ("workbench_internal", "owner_system_proposal")
TYPES = frozenset({"string", "text", "text_list", "identifier", "record_id", "citation",
                   "citation_list", "field_path", "enum", "scalar", "evidence_map",
                   "schema_names"})
TRACES = frozenset({"cited_value", "cited_field", "citation", "objective", "case_record",
                    "declared_set", "effect", "actor_statement", "argument_provenance",
                    "cited_evidence", "case_gap"})
# The capability guards' generic checks (operating/capability_guards.py CHECKS; kept
# here as data names so the loader needs no import of the checker).
GUARD_CHECKS = frozenset({"effect_fields_within", "effect_targets_cited_row",
                          "target_fields_empty", "effect_equals_payload_value",
                          "effect_values_match_payload", "payload_fields_in_cited_evidence",
                          "payload_value_matches_shape", "payload_value_in_cited_field",
                          "payload_value_not_in_cited_fields", "target_fields_hold_no_shape",
                          "payload_value_not_in_target_fields"})
# A condition a person must confirm before the owner's write is approved (owner-system
# proposals only): recorded as a prose precondition on every proposal of the type, so
# the approver always sees it (operating.capability_guards.person_preconditions).
PERSON_CHECK = "person_confirms"
EVIDENCE_LINKS = frozenset({"asserted_by_proposer_not_verified"})
# The absence guard of an internal gap write: its parameter roles.
GAP_CHECK = "gap_guard"
GAP_ROLES = {"state": "enum", "property": "schema_names", "citations": "citation_list"}
CHECKS = frozenset({"case_open", "record_exists", "parameters_differ",
                    "proposal_preconditions", PERSON_CHECK, GAP_CHECK}) | GUARD_CHECKS
HOSTS = frozenset({"case_host", "test"})
# The role an owner-system value parameter writes (see the module docstring).
OWNER_DECLARED_ROLE = "owner_declared"
MONEY_MEASURES = frozenset({"amount", "unit_price"})
ROLE_VALUE_TYPES = frozenset({"text", "integer", "decimal", "boolean", "date", "timestamp",
                              "enum"})
# The Workbench store writes an internal action may name: one write surface per
# store, allow-listed in code (the catalog cannot reach any other method).
STORE_OPERATIONS: Mapping[str, frozenset[str]] = {
    "cases": frozenset({"finding", "gap", "link", "supersede"})}
# Case-store operations that record who wrote (finding) or check it (supersede).
AUTHORED_OPERATIONS = frozenset({"finding", "supersede"})
CASE_COLLECTIONS = frozenset({"findings", "gaps", "links"})
MC_WRITE_DOOR = "mc_write_door"
# A real owning system's existing capability, dispatched only through its reviewed
# adapter (operating/action_adapters.py) once every gate of the action path holds.
GATED_ADAPTER = "gated_adapter"
OWNER_DISPATCH = frozenset({"refused", "isolated_owner_only", MC_WRITE_DOOR, GATED_ADAPTER})
# ``real_capability`` (dispatch ``gated_adapter`` or ``mc_write_door`` only): the adapter
# per target system (a code constant; each names one adapter class), the risk tiers,
# how the owner takes our idempotency key and how the write is made conditional.
REAL_ADAPTERS: Mapping[str, str] = {"portal_http": "portal", MC_WRITE_DOOR: "mc"}
RISK_TIERS = ("low", "medium", "high")
IDEMPOTENCY_SUPPORT = ("native", "none")
CONDITIONAL_WRITES = ("if_match", "read_before_write", "none")
HTTP_WRITE_METHODS = frozenset({"PATCH", "PUT", "POST"})
# Portal routes whose write raises or posts a Business Central financial document
# (plx-customer-portal@4243499). BC alone posts sales invoices, credit memos and payments: the
# Portal raises only unposted drafts (3PL billing invoice, three-pl-invoice.ts TASK-2318;
# return credit memo, credit-memo.ts TASK-803 behind BC_CREDIT_MEMO_DRAFT_ENABLED), refuses
# every payment write (payment-boundary.ts, TASK-1616: payment initiation is BC-only),
# posts misc AP bills and vendor credit memos (ap-misc.ts, TASK-805) and queues vendor
# invoices (enqueue-purchase-invoice.ts). No action type writes one, approved or not: the
# Workbench never raises, drafts or posts a financial document. Matched per path segment
# (any case, sub-routes included); ``*`` is any one segment, and so is a placeholder segment.
BC_FINANCIAL_ROUTES = (
    ("api", "mrp", "3pl", "invoice"),
    ("api", "mrp", "sales-returns"),
    ("api", "mrp", "ap-misc"),
    ("api", "mrp", "ap-payments"),
    ("api", "mrp", "ap-settle"),
    ("api", "mrp", "purchase-orders", "*", "vendor-invoice"))
_PATH = re.compile(r"^/[A-Za-z0-9._~/{}-]{1,200}$")
_PLACEHOLDER = re.compile(r"\{([a-z_]{1,40})\}")
_API_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
ISOLATED_OWNER = "fixture"
# Mission Control as the owning system: the MC write tools a catalog type may name (code,
# never configuration) and what an argument's value may come from.
MC_OWNER = "mc"
MC_WRITE_TOOLS = frozenset({"mc_create_task"})
MC_HOST_SETTINGS = frozenset({"mc_follow_up_bucket", "mc_operator"})
MC_INTENT_VALUES = frozenset({"idempotency_key", "reference", "cited_sources"})
GAP_FIELDS = frozenset({"missing", "why"})
# Owners the Workbench runs itself (never a business system).
FIXTURE_OWNERS = frozenset({ISOLATED_OWNER, "test"})
# The one spelling of an action/capability name (same rule as
# operating/capability_guards.py ``canonical``): never repaired.
_CANONICAL = re.compile(r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63}){0,5}$")
# ``column`` or ``column.jsonKey``: the paths operating.case_verification reads.
_FIELD_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}(?:\.[A-Za-z_][A-Za-z0-9_]{0,63})?$")
# A ``field_path`` parameter: also a nested ``column.key.key`` (up to three keys), as
# ``capability_guards.read_field`` reads it (a cited document's ``fields.<name>.value``).
_PARAMETER_PATH = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]{0,63}(?:\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,3}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/#-]{0,127}$")
_RECORD_ID = re.compile(r"^[A-Z][0-9]{1,6}$")
_MAX_TEXT = 4000
_MAX_STRING = 200
_MAX_HANDLE = 2000
_MAX_SCHEMA_NAMES = 5
# Every key the loader reads; any other key does not load (fails closed).
_DOCUMENT_KEYS = frozenset({"schema", "version", "note", "shapes", "actions"})
_SHAPE_KEYS = frozenset({"pattern", "rule", "upstream"})
_ENTRY_KEYS = {
    "workbench_internal": frozenset({"kind", "summary", "parameters", "preconditions",
                                     "approval", "executor", "read_back", "upstream"}),
    "owner_system_proposal": frozenset({"kind", "summary", "owner_system", "target_relations",
                                        "hosts", "registration", "parameters", "effect",
                                        "preconditions", "approval", "executor", "read_back",
                                        "evidence_note", "mc_call", "real_capability",
                                        "upstream"})}
_SECTION_KEYS = {
    ("workbench_internal", "approval"): frozenset({"required", "why"}),
    ("workbench_internal", "executor"): frozenset({"kind", "store", "operation", "arguments",
                                                   "recorded_when"}),
    ("workbench_internal", "read_back"): frozenset({"kind", "collection", "record", "expect"}),
    ("owner_system_proposal", "approval"): frozenset({"required", "tool",
                                                      "per_call_human_approval", "decider",
                                                      "approver"}),
    ("owner_system_proposal", "executor"): frozenset({"kind", "dispatch", "refusal",
                                                      "later_ask", "owner_interface"}),
    ("owner_system_proposal", "read_back"): frozenset({"kind", "relation", "fields", "values",
                                                       "tool"})}
_PARAMETER_KEYS = frozenset({"type", "role", "required", "trace", "max_length", "max_items",
                             "values"})
_TRACE_KEYS = frozenset({"kind", "checked_by", "citation", "collection", "effect_key", "field",
                         "record", "set_ref", "state"})
_PRECONDITION_KEYS: Mapping[str, frozenset[str]] = {
    "case_open": frozenset(), "effect_targets_cited_row": frozenset(),
    "record_exists": frozenset({"parameter", "collection", "state"}),
    "parameters_differ": frozenset({"parameters"}), "gap_guard": frozenset({"parameters"}),
    "proposal_preconditions": frozenset({"checked_by"}),
    "person_confirms": frozenset({"statement", "evidence_link"}),
    "effect_fields_within": frozenset({"fields"}), "target_fields_empty": frozenset({"fields"}),
    "effect_equals_payload_value": frozenset({"value"}),
    "effect_values_match_payload": frozenset({"numeric"}),
    "payload_fields_in_cited_evidence": frozenset({"evidence", "numeric", "sources"}),
    "payload_value_matches_shape": frozenset({"value", "shape"}),
    "payload_value_in_cited_field": frozenset({"value", "citation", "field"}),
    "payload_value_not_in_cited_fields": frozenset({"value", "citation", "fields"}),
    "target_fields_hold_no_shape": frozenset({"fields", "shape", "ignore_equal_to"}),
    "payload_value_not_in_target_fields": frozenset({"value", "fields"})}
if set(_PRECONDITION_KEYS) != CHECKS:
    raise RuntimeError("catalog_precondition_keys_differ_from_checks")
_REAL_KEYS = frozenset({"target_system", "adapter", "write", "read_back", "entitlement",
                        "idempotency", "owner_state", "owner_warnings", "risk_tier",
                        "downstream", "upstream"})
# ``owner_warnings`` (HTTP adapters only): a reuse warning the owner gives on the write
# (another record holds the written value under the same ``same`` fields), which
# ``ActionPath.register`` flags before approval; ``normalize`` names how the owner compares
# (``operating.action_adapters.normalized``).
_OWNER_WARNING_KEYS = frozenset({"id", "field", "same", "skip", "normalize", "rule",
                                 "upstream"})
OWNER_WARNING_NORMALIZERS = ("trim_collapse_upper",)
# ``owner_state`` (HTTP adapters only): the owner's own predicate for taking the write,
# a state field that is neither blank nor one of the ``locked`` values, and optionally
# (``edit_lock``) the owner's lock as its GET reports it; the adapter reads both on the
# pre-write GET and the twin answers its 409 by the same function (``owner_state_refusal``).
_OWNER_STATE_KEYS = frozenset({"api_field", "locked", "rule", "upstream"})
_EDIT_LOCK_KEYS = frozenset({"api_field", "flag", "reason", "refusal"})
_STATE_VALUE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_REFUSAL_CODE = re.compile(r"^[a-z][a-z0-9_]{2,60}$")
# ECMAScript WhiteSpace and LineTerminator (ECMA-262 sections 12.2-12.3): what
# ``String.prototype.trim`` strips and a regex ``\s`` matches. Python's ``str.strip()``
# and ``\s`` differ on both sides: they also take U+001C-U+001F and U+0085, and keep U+FEFF.
JS_WHITESPACE = ("\t\n\v\f\r \u00a0\u1680" + "".join(map(chr, range(0x2000, 0x200B)))
                 + "\u2028\u2029\u202f\u205f\u3000\ufeff")
_JS_SPACE_RUN = re.compile("[" + re.escape(JS_WHITESPACE) + "]+")
_ENTITLEMENT_KEYS = frozenset({"owner_check", "workbench_identity"})
_IDEMPOTENCY_KEYS = frozenset({"key", "owner_support", "header", "conditional", "rule"})
_HTTP_KEYS = frozenset({"method", "path", "path_keys", "fields"})
_TOOL_KEYS = frozenset({"tool"})


class CatalogError(ValidationError):
    """The catalog file is malformed (raised at load, never at use)."""


class ActionRefused(ValidationError):
    """An action was refused; nothing was written."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical(name: Any) -> str | None:
    return name if isinstance(name, str) and _CANONICAL.fullmatch(name) else None


def js_trim(text: str) -> str:
    """``String.prototype.trim``: the one trim wherever the Workbench compares text as a
    JavaScript owner does (a customer PO, a status); ``JS_WHITESPACE`` says how it differs
    from ``str.strip()``."""
    return text.strip(JS_WHITESPACE)


def normalized(kind: str, value: Any) -> str | None:
    """A value as an owner warning compares it (``OWNER_WARNING_NORMALIZERS``); ``None``
    for an empty or non-text value. ``trim_collapse_upper`` is the Portal's
    ``normalizePoReference`` (customer-po.ts:17-20@728a995): ``trim()``, every ``\\s+``
    run to one space, upper case."""
    if kind != "trim_collapse_upper" or not isinstance(value, str):
        return None
    return _JS_SPACE_RUN.sub(" ", js_trim(value)).upper() or None


def owner_state_admits(state: Mapping[str, Any], value: Any) -> bool:
    """Whether the owner takes a write on a record whose ``owner_state`` field holds
    ``value``: a string that is not blank (``js_trim`` leaves nothing) and is not one of the
    ``locked`` values, compared exactly as the owner compares (case-sensitive, untrimmed).
    The Portal's ``canEditSalesOrderUntilShipped`` (status.ts@31a2623:28-33) also
    normalizes first, but only Open and READY move and neither lands in its locked set,
    so the raw comparison gives the same answer. A non-string value refuses."""
    return isinstance(value, str) and bool(js_trim(value)) and value not in state["locked"]


def _shown(value: Any) -> str:
    """A state value as a refusal shows it: an enum-like text, else what it is."""
    return ("absent" if value is None
            else "blank" if isinstance(value, str) and not js_trim(value)
            else value if isinstance(value, str) and _STATE_VALUE.fullmatch(value)
            else "unreadable")


def owner_state_refusal(state: Mapping[str, Any], record: Any) -> str | None:
    """Why the owner refuses a write on ``record`` (the body of its GET) by its state, or
    ``None``. The state field must admit it (``owner_state_admits``, else
    ``owner_state_refused:<field>=<shown>``). With an ``edit_lock``, the owner's own lock
    as its GET reports it decides next: the lock's ``reason`` is the typed ``refusal``
    (``order_locked_shipped``); no lock (null) with the flag ``true`` admits; anything else
    (the lock field absent, another reason, a lock the flag contradicts) refuses as
    ``owner_state_refused:<lock field>=<shown>``. Only ever refuses: unreadable refuses."""
    if not isinstance(record, Mapping):
        return f"owner_state_refused:{state['api_field']}=unreadable"
    value = record.get(state["api_field"])
    if not owner_state_admits(state, value):
        return f"owner_state_refused:{state['api_field']}={_shown(value)}"
    lock = state.get("edit_lock")
    if lock is None:
        return None
    held = record.get(lock["api_field"])
    if isinstance(held, Mapping) and held.get("reason") == lock["reason"]:
        return lock["refusal"]
    if held is None and lock["api_field"] in record and record.get(lock["flag"]) is True:
        return None
    shown = (_shown(held.get("reason")) if isinstance(held, Mapping)
             else "absent" if lock["api_field"] not in record else "unreadable")
    return f"owner_state_refused:{lock['api_field']}={shown}"


# ------------------------------------------------------------------ loading

def _require(condition: bool, code: str) -> None:
    if not condition:
        raise CatalogError(code)


def _known_keys(value: Any, allowed: frozenset[str], where: str) -> None:
    """A mapping with any key the loader does not read does not load."""
    if isinstance(value, Mapping):
        unknown = sorted(str(key) for key in value if key not in allowed)
        _require(not unknown, f"catalog_unknown_field:{where}:{','.join(unknown)}")


def _ref(value: Any, params: Mapping[str, Any], where: str, *, receipt: bool) -> None:
    """``param:<name>`` | ``literal:<text>`` | ``actor_label`` | ``receipt:<key>``."""
    _require(isinstance(value, str), f"catalog_reference_invalid:{where}")
    prefix, _, rest = value.partition(":")
    if value == "actor_label":
        return
    if prefix == "param":
        _require(rest in params, f"catalog_reference_unknown_parameter:{where}:{rest}")
    elif prefix == "receipt":
        _require(receipt and bool(rest), f"catalog_reference_invalid:{where}")
    else:
        _require(prefix == "literal" and bool(rest), f"catalog_reference_invalid:{where}")


def role_declared(role: Any) -> bool:
    """Whether ``role`` is a declarable field role: ``owner_declared``, an
    ``identifier:<kind>`` of a kind identifiers.json declares, a quantity role, a money
    measure or a bare value type."""
    if role == OWNER_DECLARED_ROLE:
        return True
    if not isinstance(role, str) or injection_guard.ROLE.fullmatch(role) is None:
        return False
    value_type, _, qualifier = role.partition(":")
    if value_type == "identifier":
        return qualifier in identifiers.kinds()
    if value_type == "quantity":
        return qualifier in QUANTITY_TYPES
    if value_type == "money":
        return qualifier in MONEY_MEASURES
    return not qualifier and value_type in ROLE_VALUE_TYPES


def _validate_parameters(action_id: str, kind: str, params: Any) -> None:
    _require(isinstance(params, Mapping) and bool(params),
             f"catalog_parameters_missing:{action_id}")
    for name, spec in params.items():
        where = f"{action_id}.{name}"
        _require(isinstance(name, str) and bool(_FIELD_PATH.fullmatch(name)) and "." not in name,
                 f"catalog_parameter_name_invalid:{where}")
        _require(isinstance(spec, Mapping) and spec.get("type") in TYPES,
                 f"catalog_parameter_type_invalid:{where}")
        _known_keys(spec, _PARAMETER_KEYS, where)
        # A role belongs to a value an owning system writes (checked with the effect).
        _require(kind == "owner_system_proposal" or "role" not in spec,
                 f"catalog_parameter_role_not_written:{where}")
        _require(isinstance(spec.get("required"), bool), f"catalog_parameter_required_flag:{where}")
        _require("max_length" not in spec or (
            spec["type"] in ("string", "text") and type(spec["max_length"]) is int
            and 1 <= spec["max_length"] <= _MAX_TEXT), f"catalog_parameter_max_length:{where}")
        if spec["type"] == "enum":
            values = spec.get("values")
            _require(isinstance(values, list) and bool(values)
                     and all(isinstance(v, str) for v in values),
                     f"catalog_enum_values_invalid:{where}")
        trace = spec.get("trace")
        _require(isinstance(trace, Mapping) and trace.get("kind") in TRACES,
                 f"catalog_parameter_untraced:{where}")
        _known_keys(trace, _TRACE_KEYS, f"{where}.trace")
        how = trace["kind"]
        if how == "actor_statement":
            # The acting person's words are never what an owning system is asked to write.
            _require(kind == "workbench_internal",
                     f"catalog_actor_statement_on_owner_proposal:{where}")
            _require(spec["type"] in ("text", "text_list"),
                     f"catalog_actor_statement_type:{where}")
        if how in ("cited_value", "cited_field"):
            # Cited content is read through the proposal's pinned evidence.
            _require(kind == "owner_system_proposal", f"catalog_cited_trace_needs_proposal:{where}")
            citation = trace.get("citation")
            _require(citation in params and params[citation].get("type") == "citation",
                     f"catalog_cited_trace_citation:{where}")
            if how == "cited_value":
                field = trace.get("field")
                _require(field in params and params[field].get("type") == "field_path",
                         f"catalog_cited_trace_field:{where}")
        if how == "case_record":
            # A proposal reads only the case's gaps (the case context register passes).
            _require(trace.get("collection") in (CASE_COLLECTIONS if kind == "workbench_internal"
                                                 else ("gaps",))
                     and spec["type"] == "record_id", f"catalog_case_record_trace:{where}")
        if how == "case_gap":
            record = params.get(trace.get("record")) if isinstance(trace.get("record"), str) \
                else None
            _require(kind == "owner_system_proposal" and trace.get("field") in GAP_FIELDS
                     and spec["type"] in ("string", "text") and isinstance(record, Mapping)
                     and record.get("type") == "record_id"
                     and (record.get("trace") or {}).get("kind") == "case_record"
                     and (record.get("trace") or {}).get("collection") == "gaps",
                     f"catalog_case_gap_trace:{where}")
        if how == "declared_set":
            _require(spec["type"] == "enum" or (isinstance(trace.get("set_ref"), str)
                                                and trace.get("checked_by") in ("store",
                                                                                GAP_CHECK)),
                     f"catalog_declared_set_trace:{where}")
        if how == "effect":
            _require(kind == "owner_system_proposal"
                     and trace.get("effect_key") in ("fields", "relation", "key", "equals", "values"),
                     f"catalog_effect_trace:{where}")
        if how in ("argument_provenance", "cited_evidence"):
            _require(kind == "owner_system_proposal",
                     f"catalog_argument_provenance_needs_proposal:{where}")
        if how == "cited_evidence":
            _require(spec["type"] == "evidence_map", f"catalog_cited_evidence_type:{where}")
        if how == "citation":
            _require(spec["type"] in ("citation", "citation_list"),
                     f"catalog_citation_trace_type:{where}")


def _validate_preconditions(action_id: str, params: Mapping[str, Any], items: Any,
                            shapes: Mapping[str, Any]) -> None:
    _require(isinstance(items, list) and bool(items), f"catalog_preconditions_missing:{action_id}")
    seen: set[str] = set()
    for item in items:
        _require(isinstance(item, Mapping) and isinstance(item.get("id"), str)
                 and item.get("check") in CHECKS, f"catalog_precondition_invalid:{action_id}")
        _require(item["id"] not in seen, f"catalog_precondition_duplicate:{action_id}:{item['id']}")
        seen.add(item["id"])
        where = f"{action_id}.{item['id']}"
        check = item["check"]
        _known_keys(item, _PRECONDITION_KEYS[check] | {"id", "check"}, where)
        if check == "record_exists":
            _require(item.get("parameter") in params
                     and item.get("collection") in CASE_COLLECTIONS,
                     f"catalog_precondition_invalid:{where}")
        elif check == "parameters_differ":
            names = item.get("parameters")
            _require(isinstance(names, list) and len(names) == 2
                     and all(name in params for name in names),
                     f"catalog_precondition_invalid:{where}")
        elif check == GAP_CHECK:
            roles = item.get("parameters")
            _require(isinstance(roles, Mapping) and set(roles) == set(GAP_ROLES)
                     and all(params.get(roles[role], {}).get("type") == kind
                             for role, kind in GAP_ROLES.items()),
                     f"catalog_precondition_invalid:{where}")
        elif check in GUARD_CHECKS:
            _require("shape" not in item or item["shape"] in shapes,
                     f"catalog_guard_shape_unknown:{where}")
        elif check == PERSON_CHECK:
            statement = item.get("statement")
            _require(isinstance(statement, str) and 10 <= len(statement.strip()) <= 300
                     and statement == statement.strip()
                     and item.get("evidence_link", next(iter(EVIDENCE_LINKS)))
                     in EVIDENCE_LINKS, f"catalog_precondition_invalid:{where}")


def _validate_action(action_id: str, entry: Any, shapes: Mapping[str, Any]) -> None:
    _require(canonical(action_id) is not None, f"catalog_action_id_not_canonical:{action_id!r}")
    _require(isinstance(entry, Mapping) and entry.get("kind") in KINDS,
             f"catalog_action_kind_invalid:{action_id}")
    kind = entry["kind"]
    _known_keys(entry, _ENTRY_KEYS[kind], action_id)
    params = entry.get("parameters")
    _validate_parameters(action_id, kind, params)
    _validate_preconditions(action_id, params, entry.get("preconditions"), shapes)
    checks = {item["check"] for item in entry["preconditions"]}
    _require("case_open" in checks, f"catalog_precondition_case_open_missing:{action_id}")
    approval = entry.get("approval")
    executor = entry.get("executor")
    read_back = entry.get("read_back")
    _require(isinstance(approval, Mapping) and isinstance(executor, Mapping)
             and isinstance(read_back, Mapping), f"catalog_action_incomplete:{action_id}")
    for section, value in (("approval", approval), ("executor", executor),
                           ("read_back", read_back)):
        _known_keys(value, _SECTION_KEYS[(kind, section)], f"{action_id}.{section}")
    if kind == "workbench_internal":
        _require(approval.get("required") == "none", f"catalog_internal_approval:{action_id}")
        _require(not checks & ({"proposal_preconditions", PERSON_CHECK} | GUARD_CHECKS),
                 f"catalog_internal_proposal_check:{action_id}")
        store, operation = executor.get("store"), executor.get("operation")
        _require(executor.get("kind") == "workbench_store"
                 and operation in STORE_OPERATIONS.get(store, ()),
                 f"catalog_internal_executor_not_allowed:{action_id}")
        # A case gap is written only past the absence guard (one prover, both routes).
        _require((store, operation) != ("cases", "gap") or GAP_CHECK in checks,
                 f"catalog_gap_write_without_gap_guard:{action_id}")
        guarded = {item["parameters"]["property"] for item in entry["preconditions"]
                   if item["check"] == GAP_CHECK}
        _require(all(spec["trace"].get("checked_by") != GAP_CHECK or name in guarded
                     for name, spec in params.items()),
                 f"catalog_gap_guard_trace_unchecked:{action_id}")
        arguments = executor.get("arguments")
        _require(isinstance(arguments, Mapping) and bool(arguments),
                 f"catalog_executor_arguments:{action_id}")
        for name, value in arguments.items():
            _ref(value, params, f"{action_id}.executor.{name}", receipt=False)
        used = {value[6:] for value in arguments.values() if value.startswith("param:")}
        _require(used == set(params), f"catalog_executor_parameters_unused:{action_id}")
        recorded_when = executor.get("recorded_when")
        _require(recorded_when is None or (isinstance(recorded_when, Mapping)
                                           and isinstance(recorded_when.get("receipt"), str)),
                 f"catalog_executor_recorded_when:{action_id}")
        _require(read_back.get("kind") == "case_view"
                 and read_back.get("collection") in CASE_COLLECTIONS
                 and isinstance(read_back.get("record"), str)
                 and read_back["record"].startswith("receipt:"),
                 f"catalog_internal_read_back:{action_id}")
        expect = read_back.get("expect")
        _require(isinstance(expect, list) and bool(expect), f"catalog_read_back_expect:{action_id}")
        for index, item in enumerate(expect):
            _require(isinstance(item, Mapping) and isinstance(item.get("path"), str),
                     f"catalog_read_back_expect:{action_id}:{index}")
            _known_keys(item, frozenset({"path", "equals"}), f"{action_id}.read_back.{index}")
            _ref(item.get("equals"), params, f"{action_id}.read_back.{index}", receipt=True)
    else:
        _require(GAP_CHECK not in checks, f"catalog_gap_guard_on_owner_proposal:{action_id}")
        owner = entry.get("owner_system")
        _require(isinstance(owner, str) and bool(owner), f"catalog_owner_system_missing:{action_id}")
        _require(owner == ISOLATED_OWNER or action_id.partition(".")[0] == owner,
                 f"catalog_owner_system_prefix:{action_id}")
        registration = entry.get("registration")
        _require(registration is None or (
            isinstance(registration, Mapping) and isinstance(registration.get("refused"), str)
            and re.fullmatch(r"[a-z][a-z0-9_]{2,80}", registration["refused"]) is not None),
            f"catalog_registration_invalid:{action_id}")
        _known_keys(registration, frozenset({"refused", "why"}), f"{action_id}.registration")
        dispatch = executor.get("dispatch")
        _require(executor.get("kind") == "owner_dispatch" and dispatch in OWNER_DISPATCH,
                 f"catalog_owner_executor:{action_id}")
        # Only MC itself is written through the MC write door, and MC only through it.
        _require((dispatch == MC_WRITE_DOOR) == (owner == MC_OWNER),
                 f"catalog_mc_write_door_binding:{action_id}")
        if dispatch == MC_WRITE_DOOR:
            _validate_mc_call(action_id, entry, params, checks)
        else:
            _require(approval.get("required") == "mc_gate"
                     and approval.get("tool") == "mc_request_approval"
                     and approval.get("per_call_human_approval") is True,
                     f"catalog_owner_approval:{action_id}")
            _require("proposal_preconditions" in checks,
                     f"catalog_owner_structured_preconditions:{action_id}")
            _require(read_back.get("kind") == "owner_record",
                     f"catalog_owner_read_back:{action_id}")
        # Only the isolated owner the Workbench runs itself is ever dispatched to.
        _require((dispatch == "isolated_owner_only") == (owner == ISOLATED_OWNER),
                 f"catalog_owner_dispatch_not_allowed:{action_id}")
        # A real capability (adapter, entitlement, idempotency, read-back, risk tier) is
        # declared exactly by the types that can reach a real owner.
        _require(("real_capability" in entry) == (dispatch in (MC_WRITE_DOOR, GATED_ADAPTER)),
                 f"catalog_real_capability_binding:{action_id}")
        if "real_capability" in entry:
            _validate_real_capability(action_id, entry, owner, dispatch)
        # Every proposal type binds the relations it may write and the hosts that
        # offer it. Only the isolated owner binds by prefix (fixture.*), and it and the
        # test owner are offered on test hosts only.
        relations, hosts = entry.get("target_relations"), entry.get("hosts")
        _require(isinstance(relations, list) and bool(relations)
                 and all(isinstance(item, str) and item for item in relations),
                 f"catalog_target_relations_required:{action_id}")
        _require(isinstance(hosts, list) and bool(hosts) and set(hosts) <= HOSTS,
                 f"catalog_hosts_invalid:{action_id}")
        if owner == ISOLATED_OWNER:
            _require(relations == ["fixture.*"] and hosts == ["test"],
                     f"catalog_isolated_owner_binding:{action_id}")
        elif owner == "test":
            _require(hosts == ["test"], f"catalog_test_owner_hosts:{action_id}")
        else:
            _require(all("*" not in item for item in relations),
                     f"catalog_real_owner_binds_exact_relations:{action_id}")
        if dispatch == "refused":
            _require(isinstance(executor.get("later_ask"), str) and executor["later_ask"],
                     f"catalog_owner_later_ask:{action_id}")
        effect = entry.get("effect")
        value_parameters = _value_parameters(effect)
        _require(isinstance(effect, Mapping) and bool(value_parameters)
                 and set(effect) in ({"value_parameter"}, {"field_parameters"})
                 and all(name in params for name in value_parameters),
                 f"catalog_owner_effect:{action_id}")
        # Every value written to an owning system has a declared role (trace roles);
        # a parameter that is not written has none.
        for name, spec in params.items():
            where = f"{action_id}.{name}"
            if name in value_parameters:
                _require("role" in spec, f"catalog_parameter_role_undeclared:{where}")
                _require(role_declared(spec["role"]), f"catalog_parameter_role_invalid:{where}")
            else:
                _require("role" not in spec, f"catalog_parameter_role_not_written:{where}")
        # argument_provenance is checked by the action-path injection hook on the
        # dispatched values (expected_effect.values), so it fits only value parameters.
        _require(all(spec["trace"]["kind"] != "argument_provenance"
                     or name in value_parameters for name, spec in params.items()),
                 f"catalog_argument_provenance_not_the_value:{action_id}")
        # A value written must trace to evidence or the objective, never to the
        # proposal's own expected effect values (a self-trace proves nothing).
        _require(not any(params[name]["trace"]["kind"] == "effect"
                         and params[name]["trace"].get("effect_key") in ("equals", "values")
                         for name in value_parameters),
                 f"catalog_owner_value_self_traced:{action_id}")


def _validate_mc_call(action_id: str, entry: Mapping[str, Any], params: Mapping[str, Any],
                      checks: set[str]) -> None:
    """An MC-owned type: its exact call (``mc_call``), approval and read-back."""
    call, approval = entry.get("mc_call"), entry["approval"]
    _require(isinstance(call, Mapping) and call.get("tool") in MC_WRITE_TOOLS
             and isinstance(call.get("arguments"), Mapping) and bool(call["arguments"]),
             f"catalog_mc_call_invalid:{action_id}")
    _known_keys(call, frozenset({"tool", "arguments"}), f"{action_id}.mc_call")
    _require(approval.get("required") == "per_call_human_approval"
             and approval.get("tool") == call["tool"]
             and approval.get("per_call_human_approval") is True,
             f"catalog_mc_call_approval:{action_id}")
    _require(entry["read_back"].get("kind") == "mc_task"
             and entry["read_back"].get("tool") == "mc_get_task",
             f"catalog_mc_call_read_back:{action_id}")
    _require(not checks & ({"proposal_preconditions", PERSON_CHECK} | GUARD_CHECKS),
             f"catalog_mc_call_row_checks:{action_id}")
    from integrations.plx_hub_guard import MC_APPROVABLE_WRITE_TOOLS
    allowed = MC_APPROVABLE_WRITE_TOOLS[call["tool"]]
    used: set[str] = set()
    for name, spec in call["arguments"].items():
        where = f"{action_id}.mc_call.{name}"
        _require(name in allowed, f"catalog_mc_call_argument_unknown:{where}")
        sources = spec.get("from") if isinstance(spec, Mapping) else None
        _known_keys(spec, frozenset({"from", "read_back", "role"}), where)
        _require(isinstance(sources, list) and bool(sources)
                 and isinstance(spec.get("read_back"), bool),
                 f"catalog_mc_call_argument_invalid:{where}")
        # Every argument MC is sent declares the role of what it carries.
        _require(role_declared(spec.get("role")), f"catalog_mc_call_role_undeclared:{where}")
        for source in sources:
            prefix, _, rest = source.partition(":") if isinstance(source, str) else ("", "", "")
            _require((prefix == "param" and rest in params)
                     or (prefix == "host" and rest in MC_HOST_SETTINGS)
                     or (prefix == "intent" and rest in MC_INTENT_VALUES),
                     f"catalog_mc_call_source_invalid:{where}")
            if prefix == "param":
                # A parameter's declared role is the role of the argument it fills.
                _require(params[rest].get("role") == spec["role"],
                         f"catalog_mc_call_role_mismatch:{where}")
                used.add(rest)
    _require(used == set(_value_parameters(entry.get("effect"))),
             f"catalog_mc_call_parameters:{action_id}")


def _bc_financial_route(path: str) -> bool:
    """Whether ``path`` is, or may be, one of ``BC_FINANCIAL_ROUTES`` or under one. A segment
    holding a placeholder is filled from a record at dispatch, so it matches any segment."""
    segments = [s.casefold() for s in path.split("/") if s]
    return any(len(segments) >= len(route)
               and all(r == "*" or "{" in s or r == s for r, s in zip(route, segments))
               for route in BC_FINANCIAL_ROUTES)


def _validate_real_capability(action_id: str, entry: Mapping[str, Any], owner: str,
                              dispatch: str) -> None:
    """``real_capability``: the existing owner capability a type reaches, what it takes
    (entitlement, idempotency key, condition), how its outcome is read back by a GET
    (or ``mc_get_task``), the owner state the write needs (``owner_state``) and its risk
    tier. Every key is known; nothing is optional except ``header`` (a native idempotency
    key's), ``owner_state``, ``owner_warnings`` and ``downstream``."""
    where = f"{action_id}.real_capability"
    spec = entry["real_capability"]
    _require(isinstance(spec, Mapping), f"catalog_real_capability_invalid:{where}")
    _known_keys(spec, _REAL_KEYS, where)
    adapter = spec.get("adapter")
    _require(REAL_ADAPTERS.get(adapter) == owner == spec.get("target_system")
             and (adapter == MC_WRITE_DOOR) == (dispatch == MC_WRITE_DOOR),
             f"catalog_real_capability_adapter:{where}")
    _require(spec.get("risk_tier") in RISK_TIERS, f"catalog_real_capability_risk_tier:{where}")
    for name in ("upstream",) + (("downstream",) if "downstream" in spec else ()):
        _require(isinstance(spec.get(name), str) and bool(spec[name].strip()),
                 f"catalog_real_capability_{name}:{where}")
    entitlement, idempotency = spec.get("entitlement"), spec.get("idempotency")
    _require(isinstance(entitlement, Mapping) and set(entitlement) == _ENTITLEMENT_KEYS
             and all(isinstance(v, str) and v.strip() for v in entitlement.values()),
             f"catalog_real_capability_entitlement:{where}")
    _require(isinstance(idempotency, Mapping), f"catalog_real_capability_idempotency:{where}")
    _known_keys(idempotency, _IDEMPOTENCY_KEYS, f"{where}.idempotency")
    support = idempotency.get("owner_support")
    _require(idempotency.get("key") == "intent" and support in IDEMPOTENCY_SUPPORT
             and idempotency.get("conditional") in CONDITIONAL_WRITES
             and isinstance(idempotency.get("rule"), str) and bool(idempotency["rule"].strip())
             and (support == "native") == isinstance(idempotency.get("header"), str),
             f"catalog_real_capability_idempotency:{where}")
    # An owner that keeps no key never sees a retry: its write must at least be checked
    # against the record just before it is sent.
    _require(support == "native" or idempotency["conditional"] != "none",
             f"catalog_real_capability_unconditional_without_key:{where}")
    if "owner_state" in spec:
        state = spec["owner_state"]
        _known_keys(state, _OWNER_STATE_KEYS | {"edit_lock"}, f"{where}.owner_state")
        locked = state.get("locked") if isinstance(state, Mapping) else None
        lock = state.get("edit_lock") if isinstance(state, Mapping) else None
        if lock is not None:
            _known_keys(lock, _EDIT_LOCK_KEYS, f"{where}.owner_state.edit_lock")
        _require(adapter != MC_WRITE_DOOR and isinstance(state, Mapping)
                 and set(state) - {"edit_lock"} == _OWNER_STATE_KEYS
                 and (lock is None and "edit_lock" not in state
                      or isinstance(lock, Mapping) and set(lock) == _EDIT_LOCK_KEYS
                      and all(isinstance(lock[k], str) and _API_FIELD.fullmatch(lock[k])
                              for k in ("api_field", "flag"))
                      and len({lock["api_field"], lock["flag"], state.get("api_field")}) == 3
                      and isinstance(lock["reason"], str)
                      and _STATE_VALUE.fullmatch(lock["reason"]) is not None
                      and isinstance(lock["refusal"], str)
                      and _REFUSAL_CODE.fullmatch(lock["refusal"]) is not None)
                 and isinstance(state["api_field"], str)
                 and _API_FIELD.fullmatch(state["api_field"]) is not None
                 and isinstance(locked, list) and bool(locked)
                 and len(set(locked)) == len(locked)
                 and all(isinstance(v, str) and _STATE_VALUE.fullmatch(v) for v in locked)
                 and all(isinstance(state[k], str) and state[k].strip()
                         for k in ("rule", "upstream")),
                 f"catalog_real_capability_owner_state:{where}")
    write, read_back = spec.get("write"), spec.get("read_back")
    warnings = spec.get("owner_warnings", [])
    _require(isinstance(warnings, list) and (not warnings or adapter != MC_WRITE_DOOR),
             f"catalog_real_capability_owner_warnings:{where}")
    for warning in warnings:
        _known_keys(warning, _OWNER_WARNING_KEYS, f"{where}.owner_warnings")
        skip = warning.get("skip") if isinstance(warning, Mapping) else None
        _require(isinstance(warning, Mapping) and set(warning) == _OWNER_WARNING_KEYS
                 and isinstance(warning["id"], str) and _CANONICAL.fullmatch(warning["id"])
                 is not None
                 and warning["field"] in ((write or {}).get("fields") or {})
                 and isinstance(warning["same"], list) and bool(warning["same"])
                 and all(isinstance(v, str) and _FIELD_PATH.fullmatch(v)
                         for v in warning["same"])
                 and isinstance(skip, Mapping) and set(skip) == {"field", "values"}
                 and isinstance(skip["field"], str) and isinstance(skip["values"], list)
                 and warning["normalize"] in OWNER_WARNING_NORMALIZERS
                 and all(isinstance(warning[k], str) and warning[k].strip()
                         for k in ("rule", "upstream")),
                 f"catalog_real_capability_owner_warnings:{where}")
    if adapter == MC_WRITE_DOOR:
        _known_keys(write, _TOOL_KEYS, f"{where}.write")
        _known_keys(read_back, _TOOL_KEYS, f"{where}.read_back")
        _require(isinstance(write, Mapping) and write.get("tool") == entry["mc_call"]["tool"]
                 and isinstance(read_back, Mapping)
                 and read_back.get("tool") == entry["read_back"].get("tool"),
                 f"catalog_real_capability_mc_tools:{where}")
        return
    writable = next((item["fields"] for item in entry["preconditions"]
                     if item["check"] == "effect_fields_within"), None)
    for name, http, methods in (("write", write, HTTP_WRITE_METHODS),
                                ("read_back", read_back, frozenset({"GET"}))):
        at = f"{where}.{name}"
        _require(isinstance(http, Mapping), f"catalog_real_capability_http:{at}")
        _known_keys(http, _HTTP_KEYS, at)
        path, keys, fields = http.get("path"), http.get("path_keys"), http.get("fields")
        _require(http.get("method") in methods and isinstance(path, str)
                 and _PATH.fullmatch(path) is not None and isinstance(keys, Mapping)
                 and set(_PLACEHOLDER.findall(path)) == set(keys)
                 and path.count("{") == len(keys)
                 and all(isinstance(v, str) and _FIELD_PATH.fullmatch(v) for v in keys.values())
                 and isinstance(fields, Mapping) and bool(fields)
                 and all(isinstance(v, str) and _API_FIELD.fullmatch(v) for v in fields.values())
                 and not {".", ".."} & set(path.split("/")),
                 f"catalog_real_capability_http:{at}")
        _require(name != "write" or not _bc_financial_route(path),
                 f"catalog_real_capability_bc_financial_route:{at}")
        # The adapter writes only fields the type may write, and reads back every one.
        _require(isinstance(writable, list) and set(fields) <= set(writable),
                 f"catalog_real_capability_fields_not_writable:{at}")
    _require(write["fields"] == read_back["fields"],
             f"catalog_real_capability_read_back_fields:{where}")


class ActionCatalog:
    """Validated, immutable ``action id -> action type`` lookup and its checks."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        _require(isinstance(document, Mapping) and document.get("schema") == SCHEMA,
                 "catalog_schema_mismatch")
        actions = document.get("actions")
        _require(isinstance(actions, Mapping) and bool(actions), "catalog_actions_missing")
        shapes = document.get("shapes") or {}
        _require(isinstance(shapes, Mapping) and all(
            isinstance(spec, Mapping) and isinstance(spec.get("pattern"), str)
            for spec in shapes.values()), "catalog_shapes_invalid")
        _known_keys(document, _DOCUMENT_KEYS, "document")
        for name, spec in shapes.items():
            _known_keys(spec, _SHAPE_KEYS, f"shapes.{name}")
        for action_id, entry in actions.items():
            _validate_action(action_id, entry, shapes)
        self._actions = copy.deepcopy(dict(actions))
        self.shapes = copy.deepcopy(dict(shapes))
        self.version = document.get("version")

    @classmethod
    def from_file(cls, path: str | Path) -> "ActionCatalog":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def builtin(cls) -> "ActionCatalog":
        return _builtin()

    def ids(self) -> list[str]:
        return sorted(self._actions)

    def proposal_ids(self) -> list[str]:
        """Every owner-system proposal type (what a case proposal may name)."""
        return sorted(name for name, entry in self._actions.items()
                      if entry["kind"] == "owner_system_proposal")

    def test_host_isolated_owner_ids(self) -> frozenset[str]:
        """The test-host proposal types the isolated owner runs: the action path's
        ``FIXTURE_CAPABILITIES`` (one declaration: ``hosts`` plus the owner)."""
        return frozenset(name for name, entry in self._actions.items()
                         if entry["kind"] == "owner_system_proposal"
                         and entry.get("owner_system") == ISOLATED_OWNER
                         and entry.get("hosts") == ["test"])

    def get(self, action_id: Any) -> dict[str, Any] | None:
        name = canonical(action_id)
        entry = self._actions.get(name) if name else None
        return copy.deepcopy(entry) if entry is not None else None

    def _entry(self, action_id: Any) -> Mapping[str, Any]:
        name = canonical(action_id)
        entry = self._actions.get(name) if name else None
        if entry is None:
            raise ActionRefused("action_type_not_in_catalog")
        return entry

    # ---- owner-system proposals: the action-path registration hook ------

    def registration_refusal(self, proposal: Mapping[str, Any],
                             evidence: Sequence[Mapping[str, Any]],
                             objective: Mapping[str, Any]) -> str | None:
        """Why ``ActionPath.register`` must refuse this case proposal, or None.

        ``proposal``: the case view of the proposal (capability, payload,
        expected_effect, citations); ``evidence``: ``CaseStore.proposal_evidence``
        for it; ``objective``: the case ``question``, ``roots`` and
        ``objective_author_kind`` (``injection_guard.objective_supplies``), its
        ``status`` and its ``gaps`` as the reader sees them (``case_record`` and
        ``case_gap`` traces). A byte-pinned document citation (``DOCUMENT_SCHEMES``)
        is read only as the ``documentFields`` row ``ActionPath.register`` put in its
        evidence (``injection_guard.document_fields_row``); its capture's own view
        states no field.
        """
        try:
            entry = self._entry(proposal.get("capability"))
        except ActionRefused as exc:
            return exc.code
        if entry["kind"] != "owner_system_proposal":
            return "catalog_action_is_workbench_internal"
        if entry.get("registration"):
            return "action_type_registration_refused:" + entry["registration"]["refused"]
        payload = proposal.get("payload")
        if not isinstance(payload, Mapping):
            return "parameters_not_an_object"
        if entry["executor"]["dispatch"] == MC_WRITE_DOOR:
            return self._mc_registration_refusal(entry, proposal, payload, objective)
        effect = proposal.get("expected_effect") or {}
        cited = {item["handle"]: item for item in evidence}
        try:
            values = _typed(entry["parameters"], payload)
            for name, value in values.items():
                _trace_proposal(name, value, entry["parameters"], values, effect, cited,
                                objective)
        except ActionRefused as exc:
            return exc.code
        # One value per field: each effect field's value is its traced parameter's.
        written = effect_values(effect) if isinstance(effect, Mapping) else {}
        field_parameters = entry["effect"].get("field_parameters")
        for field, value in written.items():
            name = (entry["effect"]["value_parameter"] if field_parameters is None
                    else field_parameters.get(field))
            if name is None:
                return "effect_field_without_parameter:" + str(field)
            if values.get(name) is not None and not _same(value, values[name]):
                return "effect_does_not_set_the_traced_value"
        refusal = (_declared_type_refusal(effect, cited)
                   or _role_refusal(entry, values, effect, cited))
        if refusal is not None:
            return refusal
        roles = {field: (None if role == OWNER_DECLARED_ROLE else role)
                 for field, role in _parameter_roles(entry, effect).items()}
        traced = injection_guard.effect_trace(
            effect, evidence, injection_guard.human_objective(objective),
            (objective or {}).get("roots") or (), roles=roles)
        # Only the role refusal: provenance itself is the action path's injection hook.
        return traced["reason"] if traced["reason"] == injection_guard.REFUSAL_ROLE else None

    @staticmethod
    def _mc_registration_refusal(entry: Mapping[str, Any], proposal: Mapping[str, Any],
                                 payload: Mapping[str, Any],
                                 objective: Mapping[str, Any]) -> str | None:
        """An MC-owned type: typed parameters traced to the case's gaps or a declared
        set, and its preconditions on the case as the reader sees it. It names no row of
        a cited record, so it takes no expected effect."""
        if proposal.get("expected_effect"):
            return "mc_call_takes_no_expected_effect"
        view = {"status": (objective or {}).get("status"),
                "gaps": (objective or {}).get("gaps")}
        try:
            values = _typed(entry["parameters"], payload)
            for name, value in values.items():
                _trace_proposal(name, value, entry["parameters"], values, {}, {},
                                objective or {})
            for item in entry["preconditions"]:
                _precondition(item, values, view)
        except ActionRefused as exc:
            return exc.code
        # The call quotes the proposal's cited sources: a named case record's are among them.
        cited = {item.get("handle") for item in proposal.get("citations") or ()} - {None}
        for name, spec in entry["parameters"].items():
            if spec["trace"]["kind"] == "case_record" and values.get(name) is not None:
                record = _records(view, spec["trace"]["collection"]).get(values[name]) or {}
                if any(item.get("handle") not in cited for item in record.get("citations") or ()):
                    return "record_citations_not_on_proposal:" + name
        return None

    def mc_call(self, action_id: Any, parameters: Mapping[str, Any], *,
                host: Mapping[str, Any], intent: Mapping[str, Any]) -> dict[str, Any]:
        """The exact MC call of an MC-owned type: ``{"tool", "arguments", "read_back"}``
        (``read_back``: the argument names the created record must hold). ``host``: the
        configured ``MC_HOST_SETTINGS``; ``intent``: its ``MC_INTENT_VALUES``. A missing
        value refuses (``mc_call_value_missing:<argument>``); nothing is defaulted."""
        entry = self._entry(action_id)
        if entry["executor"].get("dispatch") != MC_WRITE_DOOR:
            raise ActionRefused("not_an_mc_call")
        values = _typed(entry["parameters"], parameters)
        found = {"param": values, "host": host, "intent": intent}
        arguments: dict[str, Any] = {}
        for name, spec in entry["mc_call"]["arguments"].items():
            parts = []
            for source in spec["from"]:
                prefix, _, rest = source.partition(":")
                value = found[prefix].get(rest)
                if value is None or (isinstance(value, str) and not value.strip()):
                    raise ActionRefused("mc_call_value_missing:" + name)
                parts.append(value)
            arguments[name] = parts[0] if len(parts) == 1 else "\n\n".join(map(str, parts))
        return {"tool": entry["mc_call"]["tool"], "arguments": arguments,
                "read_back": [name for name, spec in entry["mc_call"]["arguments"].items()
                              if spec["read_back"]]}

    def real_capability(self, action_id: Any) -> dict[str, Any] | None:
        """A type's ``real_capability`` (adapter, entitlement, idempotency, read-back,
        risk tier) and its ``dispatch``, or ``None`` for a type that reaches no real owner."""
        entry = self.get(action_id)
        if entry is None or "real_capability" not in entry:
            return None
        return {**entry["real_capability"], "dispatch": entry["executor"]["dispatch"],
                "target_relations": entry["target_relations"]}

    def mc_call_ids(self) -> frozenset[str]:
        """The types sent through the MC write door (``executor.dispatch: mc_write_door``)."""
        return frozenset(name for name, entry in self._actions.items()
                         if entry["executor"].get("dispatch") == MC_WRITE_DOOR)

    # ---- workbench-internal execution -----------------------------------

    def execute(self, actor: Actor, action_id: Any, case_id: str,
                parameters: Mapping[str, Any], *, stores: Mapping[str, Any],
                read: Read | None = None, schema: Any = None,
                author_kind: str | None = None) -> dict[str, Any]:
        """Run one workbench-internal action now and read it back.

        Refusals raise ``ActionRefused`` before anything is written. Returns the
        execution record: the store receipt, the read-back outcome
        (``verified`` / ``not_observed`` / ``withheld``) and the checks run.
        ``read`` (``capability_guards.current_reader`` for ``actor``) and ``schema``
        (the served ontology schema) feed the ``gap_guard`` precondition; without
        them no cited row is readable and no property is declared, so an absence is
        refused. ``author_kind``: who acts (the session's, ``human`` or ``agent``); the
        case store's authored operations (``AUTHORED_OPERATIONS``) record and check it.
        """
        entry = self._entry(action_id)
        if entry["kind"] != "workbench_internal":
            # Owner-system writes go propose -> MC approval -> owner -> read-back.
            raise ActionRefused("owner_system_action_needs_mc_gate")
        executor = entry["executor"]
        store = stores.get(executor["store"]) if isinstance(stores, Mapping) else None
        if executor["store"] == "cases" and not isinstance(store, CaseStore):
            raise ActionRefused("store_not_configured:cases")
        if not isinstance(parameters, Mapping):
            raise ActionRefused("parameters_not_an_object")
        try:
            view = store.get(actor, case_id)
        except CaseNotFound:
            raise ActionRefused("case_not_found") from None
        values = _typed(entry["parameters"], parameters)
        for name, value in values.items():
            _trace_internal(name, value, entry["parameters"][name], view)
        checked = [_precondition(item, values, view, read, schema)
                   for item in entry["preconditions"]]
        arguments = {name: _resolve(ref, values, {}, actor)
                     for name, ref in executor["arguments"].items()}
        if executor["operation"] in AUTHORED_OPERATIONS:
            arguments["author_kind"] = author_kind
        operation = getattr(store, executor["operation"])
        try:
            receipt = operation(actor, case_id, **arguments)
        except (ValidationError, CitationError, CaseNotFound) as exc:
            # The case store validates before its transaction appends: nothing written.
            raise ActionRefused("store_refused:" + str(getattr(exc, "code", None) or exc)) from None
        record = {"schema": EXECUTION_SCHEMA, "action_id": action_id, "kind": entry["kind"],
                  "case_id": case_id, "approval": "none", "executed": True,
                  "preconditions": checked, "evidence_label": EVIDENCE_LABEL}
        when = executor.get("recorded_when")
        if when is not None and receipt.get(when["receipt"]) != when.get("equals"):
            # The store decided not to record (e.g. a link contract found no match).
            return {**record, "recorded": False, "reason": receipt.get("reason"),
                    "outcome": receipt.get("outcome"),
                    "read_back": {"outcome": "nothing_recorded"}}
        receipt_ids = {key: value for key, value in receipt.items()
                       if key.endswith("_id") or key in ("seq", "outcome", "duplicate", "merged")}
        read_back = self._read_back(entry["read_back"], store, actor, case_id, values, receipt)
        return {**record, "recorded": True, "receipt": receipt_ids, "read_back": read_back}

    @staticmethod
    def _read_back(spec: Mapping[str, Any], store: CaseStore, actor: Actor, case_id: str,
                   values: Mapping[str, Any], receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Re-read the case as ``actor`` and compare the declared fields."""
        record_id = receipt.get(spec["record"][len("receipt:"):])
        view = store.get(actor, case_id)
        collection = view.get(spec["collection"]) or []
        items = ([item for bucket in collection.values() for item in bucket]
                 if isinstance(collection, Mapping) else list(collection))
        found = next((item for item in items if item.get("id") == record_id), None)
        if found is None:
            return {"outcome": "not_observed", "record_id": record_id, "reason": "record_absent"}
        if found.get("content") == WITHHELD:
            return {"outcome": "withheld", "record_id": record_id,
                    "reason": "citation_unavailable_to_reader"}
        checks = []
        for item in spec["expect"]:
            exists, actual = read_field(found, item["path"])
            wanted = _resolve(item["equals"], values, receipt, actor)
            checks.append({"path": item["path"],
                           "holds": exists is True and _same(actual, wanted)})
        return {"outcome": "verified" if all(c["holds"] for c in checks) else "not_observed",
                "record_id": record_id, "checks": checks}


def _value_parameters(effect: Any) -> list[str]:
    if not isinstance(effect, Mapping):
        return []
    if isinstance(effect.get("value_parameter"), str):
        return [effect["value_parameter"]]
    mapping = effect.get("field_parameters")
    if isinstance(mapping, Mapping) and mapping and all(
            isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        return list(mapping.values())
    return []


@lru_cache(maxsize=1)
def _builtin() -> ActionCatalog:
    return ActionCatalog.from_file(CATALOG_PATH)


# ------------------------------------------------------------------ checks

# Field types a cited record may declare for itself (``field_types`` in its citation
# content; e.g. operating/goal2_exec_owner.py typed relations). Generic: the effect
# value of an owner proposal must fit the type its cited target declares.
FIELD_TYPES = ("string", "integer", "boolean", "date")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _calendar_date(value: Any) -> bool:
    """An ISO ``YYYY-MM-DD`` that names a day of the calendar (never 2026-13-45)."""
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def value_fits_type(kind: str, value: Any) -> bool:
    """Whether ``value`` (None always fits: nullable) is of the declared ``kind``."""
    if value is None:
        return True
    return {"string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "date": _calendar_date(value)}.get(kind, False)


def _parameter_roles(entry: Mapping[str, Any], effect: Mapping[str, Any]) -> dict[str, str]:
    """effect field -> the declared role of the parameter written there."""
    field_parameters = entry["effect"].get("field_parameters")
    return {field: entry["parameters"][entry["effect"]["value_parameter"]
                                       if field_parameters is None
                                       else field_parameters[field]]["role"]
            for field in effect_values(effect)}


def _cited(item: Mapping[str, Any] | None) -> tuple[Any, Any]:
    """(handle, content) of a cited record, or nothing (None, None)."""
    return (None, None) if item is None else (item.get("handle"), item.get("content"))


def _cell_role(item: Mapping[str, Any] | None, column: Any, *value: Any) -> str | None:
    """``injection_guard.cited_role`` of ``row.<column>`` of a cited record (``value``:
    the value written there, else the cell's own)."""
    if item is None:
        return None
    return injection_guard.cited_role(item.get("handle"), item.get("content"),
                                      f"row.{column}", *value)


def _role_refusal(entry: Mapping[str, Any], values: Mapping[str, Any],
                  effect: Mapping[str, Any], cited: Mapping[str, Mapping[str, Any]]
                  ) -> str | None:
    """Fail closed on roles (see the module docstring): every written field of the
    cited target declares a role, for the value written, that fits the parameter's,
    and every ``cited_value`` is read from a cell that declares a role fitting each
    field it is written to (``owner_declared``: the target field's role). The one
    exception is a test-host owner's ``fixture.*`` row that declares no roles at all
    (``injection_guard.declares_roles``)."""
    params = entry["parameters"]
    field_parameters = entry["effect"].get("field_parameters")
    target = cited.get(effect.get("target_citation"))
    strict = entry["owner_system"] not in FIXTURE_OWNERS
    writes: dict[str, list[str]] = {}
    for field, value in effect_values(effect).items():
        name = (entry["effect"]["value_parameter"] if field_parameters is None
                else field_parameters[field])
        declared = params[name]["role"]
        found = _cell_role(target, field, value)
        if found is None and not (strict or injection_guard.declares_roles(*_cited(target))):
            continue
        if found is None:
            return "effect_field_role_undeclared:" + str(field)
        if declared != OWNER_DECLARED_ROLE and not injection_guard.same_role(declared, found):
            return "effect_field_role_mismatch:" + str(field)
        writes.setdefault(name, []).append(found if declared == OWNER_DECLARED_ROLE
                                           else declared)
    for name, spec in params.items():
        trace = spec["trace"]
        if trace["kind"] != "cited_value" or values.get(name) is None:
            continue
        item = cited[values[trace["citation"]]]
        found = _cell_role(item, values[trace["field"]])
        if found is None and not (strict or injection_guard.declares_roles(*_cited(item))):
            continue
        if found is None:
            return "parameter_role_undeclared_source:" + name
        if not all(injection_guard.same_role(role, found) for role in writes.get(name, ())):
            return f"parameter_role_mismatch:{name}:{found}"
    return None


def _declared_type_refusal(effect: Mapping[str, Any],
                           cited: Mapping[str, Mapping[str, Any]]) -> str | None:
    """The effect's cited target declares field types: every effect field must be a
    declared, writable field and the effect value must fit its type."""
    target = cited.get(effect.get("target_citation"))
    content = (target or {}).get("content")
    declared = content.get("field_types") if isinstance(content, Mapping) else None
    if not isinstance(declared, Mapping):
        return None
    read_only = set(content.get("read_only_fields") or ())
    for name, value in effect_values(effect).items():
        if name in read_only:
            return "effect_field_read_only:" + str(name)
        if name not in declared:
            return "effect_field_not_declared:" + str(name)
        if not value_fits_type(declared[name], value):
            return "effect_value_type_mismatch:" + str(name)
    return None


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return list(left) == list(right)
    return left == right


def _resolve(ref: str, values: Mapping[str, Any], receipt: Mapping[str, Any],
             actor: Actor) -> Any:
    if ref == "actor_label":
        return actor_label(actor)
    prefix, _, rest = ref.partition(":")
    if prefix == "param":
        return values.get(rest)
    if prefix == "receipt":
        return receipt.get(rest)
    return rest   # literal


def _text_ok(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _schema_name_ok(value: Any) -> bool:
    """One ``<Type>.<property>`` / ``<Type>.<link>`` / ``<Type>`` name, as
    ``plx.case_record_gap`` takes it (declared or not is the gap guard's check)."""
    return isinstance(value, str) and 3 <= len(value) <= _MAX_STRING


def _typed(specs: Mapping[str, Mapping[str, Any]], given: Mapping[str, Any]) -> dict[str, Any]:
    """Only declared parameters, each of its declared type; absent optional ones are None."""
    unknown = sorted(set(map(str, given)) - set(specs))
    if unknown:
        raise ActionRefused("parameter_unknown:" + unknown[0])
    values: dict[str, Any] = {}
    for name, spec in specs.items():
        value = given.get(name)
        if value is None:
            if spec["required"]:
                raise ActionRefused("parameter_missing:" + name)
            values[name] = None
            continue
        kind = spec["type"]
        limit = spec.get("max_items", 20)
        length = spec.get("max_length")
        ok = {
            "string": lambda v: _text_ok(v, length or _MAX_STRING),
            "text": lambda v: _text_ok(v, length or _MAX_TEXT),
            "identifier": lambda v: isinstance(v, str) and bool(_IDENTIFIER.fullmatch(v)),
            "record_id": lambda v: isinstance(v, str) and bool(_RECORD_ID.fullmatch(v)),
            "citation": lambda v: _text_ok(v, _MAX_HANDLE) and v == v.strip(),
            "field_path": lambda v: isinstance(v, str) and bool(_PARAMETER_PATH.fullmatch(v)),
            "enum": lambda v: v in spec["values"],
            "scalar": lambda v: (isinstance(v, int) and not isinstance(v, bool))
            or _text_ok(v, _MAX_STRING),
            "text_list": lambda v: isinstance(v, list) and 0 < len(v) <= limit
            and all(_text_ok(x, _MAX_STRING) for x in v),
            "citation_list": lambda v: isinstance(v, list) and len(v) <= limit
            and all(_text_ok(x, _MAX_HANDLE) and x == x.strip() for x in v),
            "schema_names": lambda v: _schema_name_ok(v) or (
                isinstance(v, list) and 0 < len(v) <= _MAX_SCHEMA_NAMES
                and all(_schema_name_ok(x) for x in v)),
            "evidence_map": lambda v: isinstance(v, Mapping) and 0 < len(v) <= 5
            and all(isinstance(k, str) and bool(_FIELD_PATH.fullmatch(k))
                    and isinstance(x, Mapping) and set(x) == {"citation", "field"}
                    and _text_ok(x["citation"], _MAX_HANDLE)
                    and isinstance(x["field"], str)
                    and bool(_PARAMETER_PATH.fullmatch(x["field"]))
                    for k, x in v.items()),
        }[kind]
        if not ok(value):
            raise ActionRefused("parameter_type:" + name)
        values[name] = value
    return values


def _objective_refusal(value: Any, case: Mapping[str, Any],
                       effect: Mapping[str, Any] | None = None) -> str | None:
    """None when the case's human-written question supplies ``value``
    (``injection_guard.objective_supplies``: the one objective rule), else why not."""
    if injection_guard.objective_supplies(value, case, effect):
        return None
    if (injection_guard.objective_author_kind(case) != "human"
            and injection_guard.objective_literal(value, case.get("question"))):
        return "objective_not_human_authored"
    return "not_in_objective"


def _trace_proposal(name: str, value: Any, specs: Mapping[str, Mapping[str, Any]],
                    values: Mapping[str, Any], effect: Mapping[str, Any],
                    cited: Mapping[str, Mapping[str, Any]],
                    objective: Mapping[str, Any]) -> None:
    if value is None:
        return
    trace = specs[name]["trace"]
    how = trace["kind"]

    def refuse(reason: str) -> None:
        raise ActionRefused(f"parameter_untraced:{name}:{reason}")

    def row_of(param: str) -> Mapping[str, Any]:
        handle = values.get(param)
        item = cited.get(handle)
        if item is None:
            refuse("citation_not_on_proposal")
        if item.get("content") is None or item.get("current_digest") != item.get("cited_digest"):
            refuse("citation_changed")
        if isinstance(handle, str) and handle.startswith(DOCUMENT_SCHEMES):
            # A byte-pinned document: the fields its pinned bytes state for this reader
            # now; without that row (other bytes, no read) the citation is not as cited.
            row = injection_guard.document_fields_row(handle, item["content"])
            if row is None:
                refuse("citation_changed")
        else:
            row = (item["content"].get("row") if isinstance(item["content"], Mapping)
                   else None)
        if not isinstance(row, Mapping):
            refuse("cited_record_not_inspectable")
        return row

    if how == "citation":
        for handle in (value if isinstance(value, list) else [value]):
            if handle not in cited:
                refuse("citation_not_on_proposal")
            item = cited[handle]
            if item.get("content") is None or item.get("current_digest") != item.get(
                    "cited_digest"):
                refuse("citation_changed")
    elif how == "cited_field":
        exists, field_value = read_field(row_of(trace["citation"]), value)
        if exists is not True or field_value is None or str(field_value).strip() == "":
            refuse("field_absent_in_cited_record")
    elif how == "cited_value":
        field = values.get(trace["field"])
        exists, field_value = read_field(row_of(trace["citation"]), field)
        if exists is not True or not isinstance(field_value, (str, int)) or isinstance(
                field_value, bool) or str(field_value).strip() != str(value).strip():
            refuse("value_not_in_cited_field")
    elif how == "objective":
        reason = _objective_refusal(value, objective, effect)
        if reason is not None:
            refuse(reason)
    elif how == "effect":
        stated = effect.get(trace["effect_key"])
        if not (value in stated if isinstance(stated, list) else _same(value, stated)):
            refuse("not_the_expected_effect")
    elif how == "declared_set":
        if specs[name]["type"] == "enum" and value not in specs[name]["values"]:
            refuse("not_in_declared_set")
    elif how == "case_record":
        if value not in _records({"gaps": objective.get("gaps")}, trace["collection"]):
            refuse("record_not_on_case")
    elif how == "case_gap":
        gap = _records({"gaps": objective.get("gaps")}, "gaps").get(values.get(trace["record"]))
        if gap is None:
            refuse("record_not_on_case")
        if gap.get("content") == WITHHELD or gap.get("citations_status") not in (
                "resolved", "uncited"):
            refuse("gap_not_current_for_reader")
        if gap.get(trace["field"]) != value:
            refuse("value_not_the_case_gap_" + trace["field"])
    elif how == "argument_provenance":
        # A value parameter; registration_refusal checks it equals the expected
        # effect's value, which ActionPath.register's injection hook then traces.
        pass
    elif how == "cited_evidence":
        # Each entry's citation must be one of the proposal's, unchanged; the capability
        # guard (payload_fields_in_cited_evidence) checks the value it holds.
        for source in value.values():
            item = cited.get(source["citation"])
            if item is None:
                refuse("citation_not_on_proposal")
            if item.get("content") is None or item.get("current_digest") != item.get(
                    "cited_digest"):
                refuse("citation_changed")
    else:   # loader keeps actor_statement off proposals
        refuse("trace_not_allowed_on_proposal")


def _records(view: Mapping[str, Any], collection: str) -> dict[str, Mapping[str, Any]]:
    found = view.get(collection) or []
    items = ([item for bucket in found.values() for item in bucket]
             if isinstance(found, Mapping) else list(found))
    return {item["id"]: item for item in items if isinstance(item, Mapping) and "id" in item}


def _trace_internal(name: str, value: Any, spec: Mapping[str, Any],
                    view: Mapping[str, Any]) -> None:
    if value is None:
        return
    trace = spec["trace"]
    how = trace["kind"]
    if how == "objective":
        reason = _objective_refusal(value, view)
        if reason is not None:
            raise ActionRefused(f"parameter_untraced:{name}:{reason}")
    if how == "case_record" and value not in _records(view, trace["collection"]):
        raise ActionRefused(f"parameter_untraced:{name}:record_not_on_case")
    # citation: the case store resolves every handle for the actor before it writes;
    # declared_set: the enum type check above, the store's own set (checked_by: store)
    # or the schema names the gap guard resolves (checked_by: gap_guard);
    # actor_statement: recorded under the actor's label as their words.


def _no_read(handle: str) -> None:
    return None


def _precondition(item: Mapping[str, Any], values: Mapping[str, Any],
                  view: Mapping[str, Any], read: Read | None = None,
                  schema: Any = None) -> dict[str, Any]:
    check = item["check"]
    if check == GAP_CHECK:
        # The absence prover plx.case_record_gap runs (capability_guards.check_gap).
        roles = item["parameters"]
        verdict = check_gap(values.get(roles["state"]), values.get(roles["property"]),
                            [str(c) for c in values.get(roles["citations"]) or () if c],
                            read or _no_read, schema)
        if not verdict["passed"]:
            raise ActionRefused(("gap_guard_failed:" + ",".join(
                failure["id"] for failure in verdict["failures"]) + ": "
                + verdict["failures"][0]["reason"])[:600])
        return {"id": item["id"], "check": check, "holds": True,
                **({"checked": verdict["checked"]} if verdict["guarded"] else {})}
    if check == "case_open":
        if view.get("status") != "open":
            raise ActionRefused("precondition_not_met:" + item["id"])
    elif check == "record_exists":
        value = values.get(item["parameter"])
        record = _records(view, item["collection"]).get(value)
        if record is None:
            raise ActionRefused("precondition_not_met:" + item["id"])
        if item.get("state") == "not_superseded" and (
                record.get("superseded") not in (None, False)):
            raise ActionRefused("precondition_not_met:" + item["id"])
    elif check == "parameters_differ":
        left, right = (values.get(name) for name in item["parameters"])
        if left is not None and right is not None and left == right:
            raise ActionRefused("precondition_not_met:" + item["id"])
    return {"id": item["id"], "check": check, "holds": True}


__all__ = ["ActionCatalog", "ActionRefused", "CATALOG_PATH", "CatalogError", "EVIDENCE_LABEL",
           "EVIDENCE_LINKS", "EXECUTION_SCHEMA", "FIELD_TYPES", "GAP_CHECK", "GUARD_CHECKS",
           "HOSTS", "KINDS", "MC_OWNER", "MC_WRITE_DOOR", "MC_WRITE_TOOLS", "OWNER_DECLARED_ROLE",
           "JS_WHITESPACE", "PERSON_CHECK", "SCHEMA", "STORE_OPERATIONS", "canonical", "js_trim",
           "normalized", "owner_state_admits", "owner_state_refusal", "role_declared",
           "value_fits_type"]
