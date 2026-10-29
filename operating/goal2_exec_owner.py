"""The owning-system adapter contract for goal-2 dispatch, and an isolated owner.

``operating.action_path`` decides
*whether* an action may run (MC gate decided by a human admin/owner, current
preconditions, the production hard guard); the owning system decides *what
happened*. This module fixes the boundary between them, so that a real,
PLX-authorized capability can later sit behind the same two calls:

``dispatch(payload, idempotency_key) -> receipt``
    Apply ``payload`` at most once per key. ``payload`` is
    ``plx-owner-dispatch/v1``: ``capability``, ``relation``, ``key``, ``set``
    ({field: value}) and ``expect`` (the compare-and-set conditions, from the
    intent's machine-checked preconditions on the target record). Outcomes:

    * returns a receipt string: the owner applied it (or had already applied
      this key with this payload: same receipt);
    * raises ``OwnerRejected(code)``: the owner is *certain* it did not and will
      never apply this key (validation, conditional-write conflict, key fenced);
    * raises anything else (timeout, reset, 5xx): the outcome is unknown. The
      caller records ``dispatched_unknown`` and never re-sends.

``read_back(ref) -> observation``
    ``ref`` is ``{"idempotency_key", "relation", "key"}`` (derivable without a
    receipt, so an unknown outcome can be read). Returns ``plx-owner-readback/v1``:
    ``operation`` (``applied`` | ``not_applied`` | ``pending`` | ``unknown``),
    ``fenced`` (only with ``not_applied``: the owner guarantees the key can never
    apply later; reading an unseen key fences it), ``receipt``, ``reason`` (the
    owner's own word on the operation: why it waits or was not applied, or ``None``)
    and ``record`` (the owning record as the owner holds it now, or ``None``). The
    action path
    verifies the effect on ``record`` itself (``field_status``); the operation
    status only attributes it and, when ``not_applied`` *and* ``fenced``, lets an
    unknown outcome end as ``failed``. Without fencing, ``not_applied`` proves
    nothing (the request may still land) and the outcome stays unknown.

``IsolatedOwnerSystem`` is an in-process owning system with its own SQLite state
(records and an idempotency/operation log), holding only ``fixture.*``
relations, whose writable fields declare their roles (``FIELD_ROLES``). It is never Petra production and makes no network call. It is also
the case store's citation resolver for its ``fixture://`` handles, so a change
in it is a change in cited evidence, as with a real source. Fault injection
(``faults``) reproduces the outcomes a real owner can produce: timeout after
apply, accepted-but-pending then timeout, unavailable before accepting, a
conditional-write conflict, and a crash hook after the apply commits.

A relation may be *typed* (``define``): declared fields with a type each
(``FIELD_TYPES``) and an owner-managed version field (the record's etag, bumped
on every write by anyone). A typed relation refuses, certainly and before
writing: an unknown field (``field_unknown:<f>``), a value of the wrong type
(``field_type_mismatch:<f>``) and any attempt to set the version field
(``field_read_only:<f>``). A compare-and-set condition on the version field that
no longer holds is ``etag_mismatch`` (HTTP 412 in a real owner): the intent's
``field_equals`` precondition on the version it cited becomes that ``If-Match``.
Further faults: ``concurrent_write`` (someone else edits the record between the
Workbench's checks and the owner's apply; only the etag catches an edit of
another field), ``partial_apply`` (a non-transactional owner commits the first
field of a multi-field write, then fails: the answer is lost and its own
operation log says ``partial``, read back as ``unknown``) and
``overwritten_after_apply`` (the write applies, then another writer changes the
same fields: the read-back sees a different value).

What a real capability needs before it can be plugged in is
``REAL_CAPABILITY_REQUIREMENTS`` (GOAL2-ACTIONS.md section 8 item 2; Vince).

Evidence label: ``isolated-fixture``.
"""

from __future__ import annotations

import builtins
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterator, Mapping, Protocol

from ontology.core import Actor

from .action_catalog import FIELD_TYPES, OWNER_DECLARED_ROLE, role_declared, value_fits_type
from .case_verification import field_status
from .cases import CitationError, _canon, effect_values
from .goal2_preconditions import CHECKS

DISPATCH_SCHEMA = "plx-owner-dispatch/v1"
READBACK_SCHEMA = "plx-owner-readback/v1"
EVIDENCE_LABEL = "isolated-fixture"
OPERATIONS = ("applied", "not_applied", "pending", "unknown")
FAULTS = ("timeout_after_apply", "timeout_pending", "unavailable", "conflict",
          "concurrent_write", "partial_apply", "overwritten_after_apply")
# The faults an owner with a ``door`` emulates: the others happen inside the owner's
# own apply, which the door replaces. Any other fault, and an unknown one, is refused
# (``OwnerRejected``, the key fenced) before anything is sent.
DOOR_FAULTS = ("unavailable", "concurrent_write")
# Operation-log reason of a pending operation, its marker: ``process_pending`` finishes
# only an operation the owner itself accepted (``OWNER_PENDING``) and refuses any other,
# a door's and an unmarked one alike (a door operation logged before door operations
# were marked has none). A door operation, once the door's answer was lost, says how
# (``door_answer_lost:<exception type>``), so an operator sees why it waits; one whose
# door certainly sent nothing (``NothingSent``) is settled not applied, fenced, and says
# so (``door_nothing_sent:<code>:`` + ``DOOR_NOTHING_SENT_HINT``). A reason never keeps an
# exception's message (a transport's names the URL, so the host): a lost answer keeps
# only the exception type, and a door's code (``NothingSent``, ``OwnerRejected``) is kept
# only while every part is in a code vocabulary (``door_code``), else replaced by
# ``DOOR_CODE_REDACTED``.
OWNER_PENDING = "owner_accepted_pending"
DOOR_PENDING = "door_awaiting_answer"
DOOR_ANSWER_LOST = "door_answer_lost:"
DOOR_NOTHING_SENT = "door_nothing_sent:"
DOOR_NOTHING_SENT_HINT = "not sent, safe to retry with a new intent"
DOOR_CODE_REDACTED = "door_code_redacted"
# The vocabularies one ``:``-separated part of a code is kept by (``door_code``); a host,
# a person's or a customer's name, a PO number or another type name is in none of them.
# The Workbench's own codes a door gives (the BC twin's OData door, ``twin/bc/odata.py``),
# and a status as ``http_<n>``, ``odata_<n>`` or ``odata_answer_<n>``.
DOOR_CODES = frozenset({"conditional_write_conflict", "etag_mismatch",
                        "odata_if_match_needs_the_cited_version", "odata_read_failed",
                        "odata_target_not_a_company_record", "target_absent"})
_STATUS_CODE = re.compile(r"(?:http|odata|odata_answer)_[1-5][0-9]{2}")
# The Portal's own error codes, generated from its source at the repo pin
# (``scripts/portal_error_codes.py``; its ``--check`` fails when they differ).
PORTAL_ERROR_CODES = frozenset(json.loads(
    Path(__file__).with_name("portal_error_codes.json").read_text(encoding="utf-8"))["codes"])
# BC API v2.0 error codes (those ``integrations.live_source_guard`` classifies and those the
# BC twin answers with), and an AL or .NET exception type as ``Application_<Type>Exception``,
# its namespace kept only under ``Microsoft.Dynamics``.
BC_ERROR_CODES = frozenset({
    "Authentication_InvalidCredentials", "BadRequest", "BadRequest_InvalidToken",
    "BadRequest_InvalidValue", "BadRequest_MethodNotAllowed", "BadRequest_NotFound",
    "BadRequest_PreconditionRequired", "BadRequest_PropertyNotWritable",
    "Internal_ServerError", "Request_EntityChanged"})
_BC_APPLICATION = re.compile(
    r"Application_(?:Microsoft\.Dynamics(?:\.[A-Z][A-Za-z0-9]*)*\.)?[A-Z][A-Za-z0-9]*Exception")
# The type names of Python's built-in exceptions (``ConnectionError``, ``TimeoutError``).
BUILTIN_EXCEPTIONS = frozenset(name for name, value in vars(builtins).items()
                               if isinstance(value, type) and issubclass(value, BaseException))
# The modules a lost answer's exception type is kept from (``answer_lost_type``): Python's
# built-ins and its transport modules (``RemoteDisconnected``, ``IncompleteRead``,
# ``SSLError``, ``URLError``; ``socket.timeout`` is ``TimeoutError``), judged by the type's
# own module, so a type of the same name elsewhere is not kept (and this module imports no
# network module, ``scripts/check_http_egress.py``).
TRANSPORT_EXCEPTION_MODULES = frozenset({"builtins", "http.client", "socket", "ssl",
                                         "urllib.error"})
# Field types a typed isolated relation declares (``IsolatedOwnerSystem.define``) are
# the catalog's ``FIELD_TYPES`` (one vocabulary: the catalog checks a value against a
# cited record's declared ``field_types`` at register, the owner again at apply).
# Read-back reasons that contradict the dispatched effect (``read_back_for``): the
# owning record holds another value, or only part of the write. The action path
# flags them (F7 ``effect_contradicted``) and never re-sends.
CONTRADICTING_READBACK_REASONS = ("value_differs", "partial_effect")
# The role of each writable field the isolated owner holds (trace roles,
# ``injection_guard.cited_role``), served on its citations as ``field_roles`` for the
# fields a record has: the catalog refuses an owner write to, or a cited value from, a
# field without one. Its order record mirrors a Portal sales order (po_reference and
# its BC externalDocumentNumber copy); its inbound document states a customer PO.
FIELD_ROLES: Mapping[str, str] = {
    "po_reference": "identifier:customer_po",
    "external_document_number": "identifier:bc_external_document_no",
    "customer_po": "identifier:customer_po",
    "line_count": "integer",
    "lines_stated": "text",
}

# What PLX must provide before a real capability replaces IsolatedOwnerSystem
# behind ``dispatch``/``read_back`` (and before ``production_target_refusal`` and
# ``DispatcherRegistry`` may admit it, which is a reviewed code change, not
# configuration). Each item names who supplies it.
REAL_CAPABILITY_REQUIREMENTS: tuple[Mapping[str, str], ...] = (
    {"id": "authorized_capability", "by": "Vince",
     "need": "a named existing PLX capability approved for agent dispatch (first "
             "candidate: Portal sales-order customer PO, updateSalesOrderHeader "
             "po_reference) and which approval governs it (the MC gate, the Portal "
             "AgentRun approval, or both)"},
    {"id": "service_identity", "by": "Vince",
     "need": "a service identity scoped to that one capability on the owning system, "
             "distinct from the MC key and from any human, with its secret in the "
             "PLX secret store"},
    {"id": "idempotency_key", "by": "Portal owner",
     "need": "the write route accepts our idempotency key (wb-<40 hex>) and returns "
             "the same receipt for the same key and payload, and refuses the same key "
             "with another payload"},
    {"id": "conditional_write", "by": "Portal owner",
     "need": "a compare-and-set write (e.g. only while po_reference is empty, or an "
             "updatedAt match) so a change between approval and apply is refused, "
             "never overwritten"},
    {"id": "operation_lookup_and_fence", "by": "Portal owner",
     "need": "an exact read of an operation by idempotency key (applied / pending / "
             "not applied); reading an unseen key must fence it so a late arrival is "
             "refused. Without it an unknown outcome can only be verified or "
             "escalated, never concluded failed"},
    {"id": "exact_record_read", "by": "Portal owner",
     "need": "an exact-record GET of the target (the reporting API is paged, advisory "
             "and has no exact-record filter, GOV-18) for the independent read-back"},
    {"id": "isolated_test_tenant", "by": "Vince",
     "need": "a non-production instance of the owning system to prove the adapter "
             "live (a test company inside BC Production is not a sandbox), plus a "
             "devloop/live-checks spec for it"},
)

_DDL = """
CREATE TABLE IF NOT EXISTS owner_record (
  relation TEXT NOT NULL, record_key TEXT NOT NULL, row_json TEXT NOT NULL,
  PRIMARY KEY (relation, record_key));
CREATE TABLE IF NOT EXISTS owner_operation (
  idempotency_key TEXT PRIMARY KEY,
  payload_sha256 TEXT, payload_json TEXT,
  relation TEXT, record_key TEXT,
  status TEXT NOT NULL CHECK(status IN ('applied','pending','rejected','fenced','partial')),
  receipt TEXT, reason TEXT, seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS owner_relation (
  relation TEXT PRIMARY KEY, fields_json TEXT NOT NULL, version_field TEXT NOT NULL);
"""


class OwnerRejected(Exception):
    """The owning system refuses, certain it wrote nothing for the request: from
    ``dispatch``, it did not and never will apply this key; from ``process_pending``,
    it will not finish that operation outside its one write path."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NothingSent(Exception):
    """A ``door``'s certain answer that it sent nothing that could write (it failed
    before its write request, e.g. on its read of the record). The owner settles the
    key not applied and fenced, and ``dispatch`` raises ``OwnerRejected``."""


class PayloadUnmappable(ValueError):
    """``dispatch_payload`` cannot express an intent as ``plx-owner-dispatch/v1``
    (a precondition on the target that cannot become a compare-and-set). Raised
    before any owner call; the caller sends nothing."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OwnerSystem(Protocol):
    """The adapter contract (module docstring). ``system`` names the owner."""

    system: str

    def dispatch(self, payload: Mapping[str, Any], idempotency_key: str) -> str: ...
    def read_back(self, ref: Mapping[str, Any]) -> Mapping[str, Any]: ...


def owner_error_code(code: Any) -> bool:
    """Is ``code`` an owning system's own error code: the Portal's (``PORTAL_ERROR_CODES``)
    or BC's (``BC_ERROR_CODES``, ``Application_<Type>Exception``)?"""
    return isinstance(code, str) and (code in PORTAL_ERROR_CODES or code in BC_ERROR_CODES
                                      or _BC_APPLICATION.fullmatch(code) is not None)


def door_code(text: Any) -> str:
    """A door's or an owner's code, or an exception type, as a reason: kept only while it
    is at most 120 characters and every ``:``-separated part is in a vocabulary (the
    Workbench's ``DOOR_CODES`` or a status, ``owner_error_code``, ``BUILTIN_EXCEPTIONS``;
    no URL, host, name or value is), else ``DOOR_CODE_REDACTED``."""
    return (text if isinstance(text, str) and len(text) <= 120
            and all(part in DOOR_CODES or _STATUS_CODE.fullmatch(part) or owner_error_code(part)
                    or part in BUILTIN_EXCEPTIONS for part in text.split(":"))
            else DOOR_CODE_REDACTED)


def answer_lost_type(exc: BaseException) -> str:
    """A lost answer's exception type as a reason: its name while the type is Python's own
    (``TRANSPORT_EXCEPTION_MODULES``), else ``DOOR_CODE_REDACTED``."""
    kind = type(exc)
    return kind.__name__ if kind.__module__ in TRANSPORT_EXCEPTION_MODULES else DOOR_CODE_REDACTED


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def handle(relation: str, key: Mapping[str, Any]) -> str:
    """Citation handle of an owner record (the ``fixture://`` scheme the case store
    already resolves for the sandbox)."""
    return f"fixture://{relation}/{_canon(dict(key))}"


def parse_handle(citation: Any) -> tuple[str, dict[str, Any]] | None:
    """``(relation, key)`` of a ``fixture://`` handle; ``None`` for another scheme.

    Raises ``CitationError`` for a ``fixture://`` handle whose key is not a JSON
    object. The key is parsed, so any JSON spelling of it (spacing, escapes, key
    order) names the same record, as the owner's resolver reads it.
    """
    if not isinstance(citation, str) or not citation.startswith("fixture://"):
        return None
    relation, _, key = citation[len("fixture://"):].partition("/")
    try:
        parsed = json.loads(key)
    except ValueError:
        raise CitationError("fixture_handle_malformed") from None
    if not relation or not isinstance(parsed, dict):
        raise CitationError("fixture_handle_malformed")
    return relation, parsed


def _cites_target(citation: Any, relation: str, key: Mapping[str, Any]) -> bool:
    """Whether ``citation`` names the effect's record: relation and canonical key
    equal, the rule ``CaseStore._effect_target`` uses to accept the target."""
    parsed = parse_handle(citation)
    return parsed is not None and parsed[0] == relation and _canon(parsed[1]) == _canon(key)


def dispatch_payload(intent: Any) -> dict[str, Any]:
    """``plx-owner-dispatch/v1`` for an ``ActionIntent``: the effect to set and, as
    compare-and-set conditions, the intent's machine-checked preconditions that
    cite the target record itself (a real owner re-checks them atomically).

    A precondition cites the target when its handle names the same relation and
    canonical key, however the handle is spelled. A precondition that may cite
    the target but cannot become a condition (a malformed ``fixture://`` handle,
    a check kind the owner cannot re-check) raises ``PayloadUnmappable``, and the
    dispatcher refuses before anything is sent: an approved conditional write
    never runs unconditionally.
    """
    effect = intent.expected_effect
    relation, key = effect["relation"], dict(effect["key"])
    expect = []
    for check in intent.checked_preconditions:
        try:
            on_target = _cites_target(check.get("citation"), relation, key)
        except CitationError:
            raise PayloadUnmappable("precondition_citation_not_mappable") from None
        if not on_target:
            continue
        if check.get("check") not in CHECKS or not isinstance(check.get("field"), str):
            raise PayloadUnmappable("precondition_not_compare_and_set")
        expect.append({name: check[name] for name in ("check", "field", "value")
                       if name in check})
    return {"schema": DISPATCH_SCHEMA, "capability": intent.capability,
            "relation": relation, "key": key,
            "set": effect_values(effect),
            "expect": expect}


def readback_ref(intent: Any) -> dict[str, Any]:
    effect = intent.expected_effect
    return {"idempotency_key": intent.idempotency_key, "relation": effect["relation"],
            "key": dict(effect["key"])}


def read_back_for(owner: Any) -> Callable[[Any], dict[str, Any]]:
    """The ``ActionPath.read_back`` callable over an owner's ``read_back(ref)``.

    The effect is judged on the returned record (independently of the owner's
    operation status). The owner payload sets *every* listed field
    (``dispatch_payload``), so the effect is observed only when every listed field
    holds the expected value. A contradiction is named only when the owner can no
    longer be applying the write: its operation is ``applied``, or ``unknown`` with
    a partial write in its own log (``partial_write``). Then some fields equal and
    some not is ``partial_effect`` and another non-empty value is ``value_differs``
    (both ``CONTRADICTING_READBACK_REASONS``: flagged, never re-sent). A reported
    partial write is decided first. Otherwise (``pending``, not applied, an owner
    that cannot say, or, only for an owner that declares replica lag
    (``declares_replica_lag``), a field that still holds the pre-dispatch value the
    intent cited in a ``field_equals`` precondition) it is plain
    ``effect_not_observed``: read back again later. For a synchronous owner an
    applied write that shows the cited prior value is a revert, and one whose field
    is now empty was cleared: ``value_differs``. (An owner declaring replica lag
    reads an empty field as plain ``effect_not_observed``: its read may not show
    the write yet.) The operation status is passed on for attribution and for the fenced
    ``not_applied`` conclusion (``failed``).

    Escalation is a hand-off to a human, never a verification: an escalated intent
    reads back like a dispatched one, and only a *fresh* read-back that returns
    ``effect_observed`` moves it to ``verified`` (``ActionPath.read_back``). A
    not-observed or unknown read-back leaves it ``escalated``; the escalation, a
    flag on the case and a human's note never verify it.
    """
    lags = getattr(owner, "declares_replica_lag", False) is True

    def read(intent: Any) -> dict[str, Any]:
        observed = type(owner).read_back(owner, readback_ref(intent))
        effect = intent.expected_effect
        operation = {"status": observed.get("operation"),
                     "fenced": observed.get("fenced") is True,
                     "receipt": observed.get("receipt"),
                     "reason": observed.get("reason")}
        if operation["status"] not in OPERATIONS:
            operation["status"] = "unknown"
        record = observed.get("record")
        if not isinstance(record, Mapping):
            return {"outcome": "unknown", "reason": "row_not_returned", "fields": {},
                    "operation": operation}
        # One expected value per field (effect_values); every field must hold its own.
        fields = {path: field_status(record, path, value)
                  for path, value in effect_values(effect).items()}
        statuses = [item["status"] for item in fields.values()]
        partial = (operation["status"] == "unknown"
                   and observed.get("partial_write") is True)
        settled = operation["status"] == "applied" or partial
        if "not_inspectable" in statuses:
            outcome, reason = "unknown", "field_not_inspectable"
        elif statuses and all(status == "equals_expected" for status in statuses):
            outcome, reason = "effect_observed", None
        elif not settled:
            outcome, reason = "effect_not_observed", None   # read back again later
        elif partial:
            # The owner itself reports a partial write: never hidden behind a prior value.
            outcome, reason = ("effect_not_observed",
                               "partial_effect" if "equals_expected" in statuses
                               else "value_differs" if "differs" in statuses else None)
        elif lags and _still_before(intent, record, fields):
            # Only an owner whose reads may lag behind its writes: the read may not show
            # the applied write yet. A synchronous owner showing the prior is a revert.
            outcome, reason = "effect_not_observed", None
        elif "equals_expected" in statuses:
            outcome, reason = "effect_not_observed", "partial_effect"
        elif "differs" in statuses:
            outcome, reason = "effect_not_observed", "value_differs"
        elif not lags and "empty" in statuses:
            # Synchronous owner, applied write, field now empty: the value was cleared
            # after the write (it cannot be replica lag): the owner's record differs.
            outcome, reason = "effect_not_observed", "value_differs"
        else:
            outcome, reason = "effect_not_observed", None
        return {"outcome": outcome, "reason": reason, "fields": fields, "operation": operation}

    return read


def _still_before(intent: Any, record: Mapping[str, Any],
                  fields: Mapping[str, Mapping[str, Any]]) -> bool:
    """Whether a field not yet at the expected value still holds the pre-dispatch
    value the intent cited for the target (a ``field_equals`` precondition)."""
    effect = intent.expected_effect
    for check in intent.checked_preconditions:
        try:
            on_target = _cites_target(check.get("citation"), effect["relation"], effect["key"])
        except CitationError:
            continue
        name = check.get("field")
        if (on_target and check.get("check") == "field_equals" and name in fields
                and fields[name]["status"] != "equals_expected"
                and field_status(record, name, check.get("value", ""))["status"]
                == "equals_expected"):
            return True
    return False


class IsolatedOwnerSystem:
    """An owning system we own: in-process, its own SQLite state, ``fixture.*`` only.

    ``readers``: principals allowed to read its records as citations (``None``:
    any actor). ``faults``: outcomes to inject on the next dispatches, in order
    (``FAULTS``). ``after_apply``: called after an apply commits and before the
    answer is returned (a crash test kills the process there). ``door``: the owner's own
    API in front of its records (the BC twin's OData, ``twin.bc.odata.ODataDoor``); when
    set, an apply is that one call with the payload (it checks the conditions and writes,
    or raises ``OwnerRejected``, or ``NothingSent`` when it failed before its write
    request; any other exception is an answer never heard), logged ``pending`` before the
    call and settled by its answer. The door is then the one write path:
    ``process_pending`` refuses a door operation, and a fault the door cannot emulate
    (``DOOR_FAULTS``) is refused.
    """

    system = "fixture"
    # Reads are synchronous (same SQLite state as the writes): a read never lags.
    declares_replica_lag = False

    def __init__(self, path: str | Path, *, readers: frozenset[str] | None = None,
                 field_roles: Mapping[str, str] = FIELD_ROLES) -> None:
        if not all(isinstance(name, str) and name and role_declared(role)
                   and role != OWNER_DECLARED_ROLE for name, role in field_roles.items()):
            raise ValueError("field_roles_invalid")
        self.path = str(path)
        self.readers = readers
        self.field_roles = dict(field_roles)
        self.faults: list[str] = []
        self.after_apply: Callable[[], None] | None = None
        self.door: Callable[[dict[str, Any]], None] | None = None
        self.dispatch_calls = 0
        self.read_back_calls = 0
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.executescript(_DDL)
        finally:
            db.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    # ---- the owner's own data (schema, seeding and other writers) ----------

    def define(self, relation: str, fields: Mapping[str, str], *,
               version_field: str = "version") -> None:
        """Declare a typed relation: ``{field: type}`` (``FIELD_TYPES``) and the
        owner-managed version field (the etag; integer, starts at 1, bumped on
        every write). Records of an undeclared relation stay untyped."""
        self._check_relation(relation)
        if (not isinstance(fields, Mapping) or not fields
                or any(not isinstance(name, str) or not name or kind not in FIELD_TYPES
                       for name, kind in fields.items())):
            raise ValueError("relation_fields_invalid")
        if not isinstance(version_field, str) or not version_field or version_field in fields:
            raise ValueError("version_field_invalid")
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO owner_relation VALUES(?,?,?)",
                       (relation, _canon(dict(fields)), version_field))

    def seed(self, relation: str, key: Mapping[str, Any], row: Mapping[str, Any]) -> str:
        self._check_relation(relation)
        row = dict(row)
        with self._connect() as db:
            schema = self._schema(db, relation)
            if schema is not None:
                fields, version_field = schema
                problem = _typed_problem(fields, version_field,
                                         {k: v for k, v in row.items() if k != version_field})
                if problem:
                    raise ValueError(problem)
                row[version_field] = 1
            db.execute("INSERT OR REPLACE INTO owner_record VALUES(?,?,?)",
                       (relation, _canon(dict(key)), _canon(row)))
        return handle(relation, key)

    def update_by_other_writer(self, relation: str, key: Mapping[str, Any],
                               changes: Mapping[str, Any]) -> None:
        """A change made in the owning system by someone else (not through dispatch)."""
        with self._connect() as db:
            self._write_as_other(db, relation, key, changes)

    def _write_as_other(self, db: sqlite3.Connection, relation: str, key: Mapping[str, Any],
                        changes: Mapping[str, Any]) -> None:
        row = self._row(db, relation, key)
        if row is None:
            raise KeyError("record_absent")
        row.update(changes)
        self._bump(db, relation, row)
        db.execute("UPDATE owner_record SET row_json=? WHERE relation=? AND record_key=?",
                   (_canon(row), relation, _canon(dict(key))))

    def delete_by_other_writer(self, relation: str, key: Mapping[str, Any]) -> None:
        """A record removed in the owning system by someone else (not through dispatch)."""
        with self._connect() as db:
            if self._row(db, relation, key) is None:
                raise KeyError("record_absent")
            db.execute("DELETE FROM owner_record WHERE relation=? AND record_key=?",
                       (relation, _canon(dict(key))))

    def record(self, relation: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        with self._connect() as db:
            return self._row(db, relation, key)

    def operations(self) -> list[dict[str, Any]]:
        """The owner's operation log (idempotency key, status), in arrival order."""
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT idempotency_key, status, receipt, reason FROM owner_operation "
                "ORDER BY seq")]

    def process_pending(self, idempotency_key: str, *, apply: bool) -> str:
        """The owner finishes an accepted-but-pending operation (its own background work).

        Refused (``OwnerRejected``, nothing written) for an owner with a door and for any
        operation the owner did not itself accept (``OWNER_PENDING``): the door is the one
        write path (decision 14), and a door whose answer was lost may already have
        written, so neither applying nor dropping it here would be true. An unmarked
        operation may be a door's logged before door operations were marked, so it is
        refused too (fail closed, no migration)."""
        with self._connect() as db:
            op = db.execute("SELECT * FROM owner_operation WHERE idempotency_key=?",
                            (idempotency_key,)).fetchone()
            if op is None or op["status"] != "pending":
                raise KeyError("no_pending_operation")
            if self.door is not None or _door_operation(op["reason"]):
                raise OwnerRejected("door_operation_not_processed")
            if op["reason"] != OWNER_PENDING:
                raise OwnerRejected("pending_operation_unmarked")
            if not apply:
                db.execute("UPDATE owner_operation SET status='rejected', reason=? WHERE "
                           "idempotency_key=?", ("owner_dropped_pending", idempotency_key))
                return "rejected"
            _, rejected = self._apply(db, idempotency_key, json.loads(op["payload_json"]),
                                      op["payload_sha256"], update=True)
            return "rejected" if rejected else "applied"

    # ---- citation resolver ----------------------------------------------

    def resolve(self, citation: str, actor: Actor) -> dict[str, Any]:
        if self.readers is not None and not (set(actor.principals) & set(self.readers)):
            raise CitationError("not visible")
        if not isinstance(citation, str) or not citation.startswith("fixture://"):
            raise CitationError("not_a_fixture_handle")
        try:
            relation, parsed = parse_handle(citation)  # type: ignore[misc]
        except CitationError:
            raise CitationError("fixture_handle_malformed") from None
        with self._connect() as db:
            row = self._row(db, relation, parsed)
            schema = self._schema(db, relation)
        if row is None:
            raise CitationError("fixture_row_absent")
        cited = {"relation": relation, "key": parsed, "row": row, "evidence": EVIDENCE_LABEL}
        cited["field_roles"] = {name: role for name, role in self.field_roles.items()
                                if name in row}
        if schema is not None:   # a typed relation declares its fields in what is cited
            cited["field_types"] = {**schema[0], schema[1]: "integer"}
            cited["read_only_fields"] = [schema[1]]
        return cited

    # ---- the adapter contract -------------------------------------------

    def dispatch(self, payload: Mapping[str, Any], idempotency_key: str) -> str:
        self.dispatch_calls += 1
        fault = self.faults.pop(0) if self.faults else None
        if fault == "unavailable":
            raise ConnectionError("owner unavailable before accepting the request")
        if not isinstance(idempotency_key, str) or not idempotency_key.startswith("wb-"):
            raise OwnerRejected("idempotency_key_invalid")
        if not isinstance(payload, Mapping) or payload.get("schema") != DISPATCH_SCHEMA:
            raise OwnerRejected("payload_invalid")
        self._check_relation(str(payload.get("relation")))
        digest = _sha(dict(payload))
        with self._connect() as db:
            op = db.execute("SELECT * FROM owner_operation WHERE idempotency_key=?",
                            (idempotency_key,)).fetchone()
            if op is not None:
                if op["status"] in ("rejected", "fenced"):
                    raise OwnerRejected("idempotency_key_fenced")
                if op["payload_sha256"] != digest:
                    raise OwnerRejected("idempotency_key_reused_with_other_payload")
                if op["status"] == "applied":
                    return op["receipt"]
                raise RuntimeError("operation_" + op["status"])  # outcome still unknown
            refused = ("fault_unknown" if fault is not None and fault not in FAULTS
                       else "fault_not_emulated_through_the_door"
                       if self.door is not None and fault not in (None, *DOOR_FAULTS)
                       else None)
            if fault == "concurrent_write":
                # Someone else saves the record after the Workbench's checks and before
                # this apply (an edit of nothing we set or check): only the etag sees it.
                self._write_as_other(db, payload["relation"], payload["key"], {})
            # Through the door, logged before the call: an answer never heard leaves it
            # pending (the outcome unknown) and a replay of the key is never sent again.
            if refused:   # certain, nothing sent: logged rejected, so the key is fenced
                db.execute("INSERT INTO owner_operation VALUES(?,?,?,?,?,?,?,?,?)",
                           (idempotency_key, digest, _canon(dict(payload)),
                            payload["relation"], _canon(dict(payload["key"])), "rejected",
                            None, refused, self._next_seq(db)))
                pending, receipt, rejected = False, None, refused
            elif self.door is not None or fault == "timeout_pending":
                db.execute("INSERT INTO owner_operation VALUES(?,?,?,?,?,?,?,?,?)",
                           (idempotency_key, digest, _canon(dict(payload)),
                            payload["relation"], _canon(dict(payload["key"])), "pending",
                            None, DOOR_PENDING if self.door is not None else OWNER_PENDING,
                            self._next_seq(db)))
                pending, receipt, rejected = True, None, None
            else:
                pending = False
                if fault == "conflict":  # another writer got there between check and apply
                    row = self._row(db, payload["relation"], payload["key"])
                    if row is not None:
                        for field in payload["set"]:
                            row[field] = "changed-by-another-writer"
                        db.execute("UPDATE owner_record SET row_json=? WHERE relation=? AND "
                                   "record_key=?", (_canon(row), payload["relation"],
                                                    _canon(dict(payload["key"]))))
                receipt, rejected = self._apply(db, idempotency_key, dict(payload), digest,
                                                partial=fault == "partial_apply")
                if fault == "overwritten_after_apply" and not rejected:
                    self._write_as_other(db, payload["relation"], payload["key"],
                                         {name: _other_value(value)
                                          for name, value in payload["set"].items()})
        if rejected:  # committed first: the key is fenced before the caller hears it
            raise OwnerRejected(rejected)
        if self.door is not None:
            return self._through_door(payload, idempotency_key, digest)
        if pending:
            raise TimeoutError("owner accepted the request; the answer timed out")
        if fault == "partial_apply":
            raise RuntimeError("owner failed after writing part of the request")
        if self.after_apply is not None:
            self.after_apply()
        if fault == "timeout_after_apply":
            raise TimeoutError("owner applied the request; the answer timed out")
        return receipt

    def _through_door(self, payload: Mapping[str, Any], idempotency_key: str,
                      digest: str) -> str:
        try:
            self.door(json.loads(_canon(dict(payload))))   # a copy: the door owns nothing
        except OwnerRejected as exc:   # certain: settled, the key fenced, then raised
            code = door_code(exc.code)
        except NothingSent as exc:   # certain, nothing sent: likewise
            code = f"{DOOR_NOTHING_SENT}{door_code(str(exc))}:{DOOR_NOTHING_SENT_HINT}"
        except Exception as exc:   # never heard: stays pending, saying why it waits
            self._settle(idempotency_key, "pending", None,
                         f"{DOOR_ANSWER_LOST}{answer_lost_type(exc)}")
            raise
        else:
            receipt = "owner-receipt:" + _sha([idempotency_key, digest])[:16]
            self._settle(idempotency_key, "applied", receipt, None)
            if self.after_apply is not None:
                self.after_apply()
            return receipt
        self._settle(idempotency_key, "rejected", None, code)
        # Raised outside the handler: no cause and no context chains the door's exception,
        # so a logged traceback shows the code, never its text (a URL names the host).
        raise OwnerRejected(code)

    def _settle(self, idempotency_key: str, status: str, receipt: str | None,
                reason: str | None) -> None:
        with self._connect() as db:
            db.execute("UPDATE owner_operation SET status=?, receipt=?, reason=? WHERE "
                       "idempotency_key=? AND status='pending'",
                       (status, receipt, reason, idempotency_key))

    def read_back(self, ref: Mapping[str, Any]) -> dict[str, Any]:
        """Operation by idempotency key (fencing an unseen key) and the record now."""
        self.read_back_calls += 1
        key = ref.get("idempotency_key")
        if not isinstance(key, str) or not key.startswith("wb-"):
            return {"schema": READBACK_SCHEMA, "operation": "unknown", "fenced": False,
                    "receipt": None, "reason": None, "record": None,
                    "evidence": EVIDENCE_LABEL}
        with self._connect() as db:
            op = db.execute("SELECT * FROM owner_operation WHERE idempotency_key=?",
                            (key,)).fetchone()
            if op is None:
                # Never seen: fence it, so a request still in flight can never apply.
                reason = "fenced_by_read_back"
                db.execute("INSERT INTO owner_operation VALUES(?,?,?,?,?,?,?,?,?)",
                           (key, None, None, ref.get("relation"),
                            _canon(dict(ref.get("key") or {})), "fenced", None,
                            reason, self._next_seq(db)))
                operation, fenced, receipt = "not_applied", True, None
            else:
                # ``partial``: the owner itself cannot say the operation applied.
                operation = {"applied": "applied", "pending": "pending",
                             "partial": "unknown"}.get(op["status"], "not_applied")
                fenced = op["status"] in ("rejected", "fenced")
                receipt, reason = op["receipt"], op["reason"]
            partial_write = op is not None and op["status"] == "partial"
            relation = ref.get("relation")
            record = (self._row(db, relation, ref.get("key") or {})
                      if isinstance(relation, str) else None)
        return {"schema": READBACK_SCHEMA, "operation": operation, "fenced": fenced,
                "receipt": receipt, "reason": reason, "record": record,
                "partial_write": partial_write, "evidence": EVIDENCE_LABEL}

    # ---- internals --------------------------------------------------------

    @staticmethod
    def _check_relation(relation: str) -> None:
        if not relation.startswith("fixture."):
            raise ValueError("the isolated owner holds only fixture.* relations")

    @staticmethod
    def _row(db: sqlite3.Connection, relation: str,
             key: Mapping[str, Any]) -> dict[str, Any] | None:
        found = db.execute("SELECT row_json FROM owner_record WHERE relation=? AND "
                           "record_key=?", (relation, _canon(dict(key)))).fetchone()
        return json.loads(found["row_json"]) if found else None

    @staticmethod
    def _schema(db: sqlite3.Connection, relation: str) -> tuple[dict[str, str], str] | None:
        found = db.execute("SELECT fields_json, version_field FROM owner_relation WHERE "
                           "relation=?", (relation,)).fetchone()
        return (json.loads(found["fields_json"]), found["version_field"]) if found else None

    def _bump(self, db: sqlite3.Connection, relation: str, row: dict[str, Any]) -> None:
        """Every write by anyone moves a typed record's version (its etag)."""
        schema = self._schema(db, relation)
        if schema is not None:
            row[schema[1]] = int(row.get(schema[1]) or 0) + 1

    @staticmethod
    def _next_seq(db: sqlite3.Connection) -> int:
        return int(db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM owner_operation")
                   .fetchone()[0])

    def _apply(self, db: sqlite3.Connection, idempotency_key: str, payload: Mapping[str, Any],
               digest: str, update: bool = False,
               partial: bool = False) -> tuple[str | None, str | None]:
        """Compare-and-set (the etag first), type-check, then write; or reject
        (recorded, so the key is fenced). Returns ``(receipt, None)`` or
        ``(None, rejection code)``. ``partial``: commit only the first field and
        log the operation ``partial`` (a non-transactional owner that failed)."""
        row = self._row(db, payload["relation"], payload["key"])
        schema = self._schema(db, payload["relation"])
        reason = None
        if row is None:
            reason = "target_absent"
        else:
            for condition in payload.get("expect") or ():
                status = field_status(row, condition["field"], condition.get("value", ""))
                wanted = "empty" if condition["check"] == "field_empty" else "equals_expected"
                if status["status"] != wanted:
                    reason = ("etag_mismatch" if schema is not None
                              and condition["field"] == schema[1]
                              else "conditional_write_conflict")
                    break
            if reason is None and schema is not None:
                reason = _typed_problem(schema[0], schema[1], payload["set"])
        receipt = None if reason else "owner-receipt:" + _sha([idempotency_key, digest])[:16]
        status = "rejected" if reason else "applied"
        written = dict(payload["set"])
        if partial and not reason:
            first = next(iter(written))
            written, status, receipt = {first: written[first]}, "partial", None
            reason = "failed_after_partial_write"
        if update:
            db.execute("UPDATE owner_operation SET status=?, receipt=?, reason=? WHERE "
                       "idempotency_key=?", (status, receipt, reason, idempotency_key))
        else:
            db.execute("INSERT INTO owner_operation VALUES(?,?,?,?,?,?,?,?,?)",
                       (idempotency_key, digest, _canon(dict(payload)), payload["relation"],
                        _canon(dict(payload["key"])), status, receipt, reason,
                        self._next_seq(db)))
        if row is not None and status in ("applied", "partial"):
            row.update(written)
            self._bump(db, payload["relation"], row)
            db.execute("UPDATE owner_record SET row_json=? WHERE relation=? AND record_key=?",
                       (_canon(row), payload["relation"], _canon(dict(payload["key"]))))
        return receipt, (reason if status == "rejected" else None)


def _door_operation(reason: Any) -> bool:
    """Whether a pending operation's logged reason marks it as a door operation."""
    return reason == DOOR_PENDING or (isinstance(reason, str)
                                      and reason.startswith(DOOR_ANSWER_LOST))


def _typed_problem(fields: Mapping[str, str], version_field: str,
                   values: Mapping[str, Any]) -> str | None:
    """Why a typed relation refuses ``values`` (None: every value fits)."""
    for name, value in values.items():
        if name == version_field:
            return "field_read_only:" + name
        kind = fields.get(name)
        if kind is None:
            return "field_unknown:" + name
        if value is None:
            continue
        if not value_fits_type(kind, value):
            return "field_type_mismatch:" + name
    return None


def _other_value(value: Any) -> Any:
    """A different value of the same type (another writer's edit)."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    return f"{value}-edited"


__all__ = ["CONTRADICTING_READBACK_REASONS", "DISPATCH_SCHEMA", "DOOR_FAULTS", "EVIDENCE_LABEL",
           "FAULTS",
           "FIELD_TYPES", "IsolatedOwnerSystem", "NothingSent", "OPERATIONS",
           "OwnerRejected", "OwnerSystem", "PayloadUnmappable", "READBACK_SCHEMA", "REAL_CAPABILITY_REQUIREMENTS",
           "answer_lost_type", "dispatch_payload", "door_code", "handle", "owner_error_code",
           "parse_handle", "read_back_for", "readback_ref"]
