"""Real-system adapters for goal-2 dispatch: the exact request and its read-back (EXE-02).

An adapter turns an authorized action intent into the one request the owning system's
existing capability takes, as the action catalog declares it (``real_capability`` of a
type with ``executor.dispatch: gated_adapter``: method, path, path keys from the effect's
record key, the mapping of written fields to the owner's field names), sends it once
through a transport, and verifies the outcome by a GET of the same record.

An adapter never decides whether an action may run. ``operating.action_path`` does, and
dispatches to an adapter only when every gate holds (``GATES``, reported by
``unmet_gates`` and named by ``production_target_refusal`` while one is unmet):

* ``catalog_real_capability``: the type declares a real capability for this owner and
  the effect's relation is one it binds;
* ``adapter_registered``: the dispatcher registry holds this capability's adapter;
* ``mc_approval_read_back``: the MC gate the human-approved call requested was decided
  by an MC admin/owner who is not a subject of the intent (``record_decision``), and
  that decision was read back from MC again at dispatch (``dispatch(..., mc=...)``);
* ``allow_switch``: the capability is listed in ``ALLOW_ENV``. The switch is off by
  default; the development loop never sets it (a person enables one capability on the
  credentialed host, devloop/GOAL2-ACTIONS.md section 8). In test mode
  (``twin.mode.test_mode(allow=...)``, never on a credentialed host) the capabilities it
  names read as switched on for the write twin's own transport only: at dispatch the
  action path names the adapter's transport, and any other (an injected or a later
  reviewed live transport in the same process) still needs ``ALLOW_ENV``.

No transport that reaches a network ships here. A transport declares the evidence label
of what answers it (``evidence``) and only ``TRANSPORT_LABELS`` are accepted, today
``injected-transport``, plus ``twin-transport`` in test mode only and only from the exact
type ``twin.transport.TwinTransport`` (the in-process write twin; ``transport_admitted``,
re-checked at every dispatch and read-back): pointing an adapter at a real system is a
reviewed code change (a non-production instance, the ``devloop/live-checks`` spec run,
then a new label), never configuration.

Before the write, ``dispatch`` reads the owner's record (the catalog's GET) and sends
nothing unless, on that read, every structured precondition of the intent on a written
field holds, every written field one of the type's no-overwrite guards names
(``capability_guards.NO_OVERWRITE_CHECKS``, e.g. ``target_has_no_po``) holds nothing,
and the record's ``owner_state`` admits the write (``action_catalog.owner_state_refusal``,
the function the write twin answers its 409 by: the Portal takes a header edit only
until the order's first shipment or cancel, and its GET reports the lock a SHIPPED or
DELIVERED shipment or a shipped line puts on the order, editability.ts@5cb95e1:42-67).
Registration checked those guards on the cited row; this checks the owner's own record
as it is now, so a value set at the owner after approval is never replaced, whatever the
proposal's own preconditions name.

Outcomes of ``dispatch`` (the action path maps them): a receipt (2xx); ``NotSent`` when
it is certain nothing was written by this attempt (the pre-write read failed, a
precondition or no-overwrite guard does not hold at the owner
(``owner_precondition_failed:<field or guard id>``), the order has shipped
(``order_locked_shipped``) or the owner's record is otherwise not in the state the write
needs (``owner_state_refused:<field>=<value>``), the key was already sent
through this adapter); ``OwnerRejected`` when the owner answered a definite refusal (4xx
other than 408/425/429, as a code only, ``http_<status>:<owner code>`` while the owner's
code is in its own vocabulary, else ``http_<status>:<hint>`` by status (``owner_code``:
never the owner's message); 412 is ``etag_mismatch``); anything else (5xx, a timeout, a
lost answer) propagates: the outcome is unknown and is reconciled by read-back only, never
re-sent.

Evidence label: ``injected-transport`` (live-unverified;
``devloop/live-checks/portal.sales_order_adapter.json``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import re
import sys
from typing import Any, Mapping, Protocol
from urllib.parse import quote

from twin import mode as twin_mode
from twin.transport import TwinTransport

from . import capability_guards
from .action_catalog import GATED_ADAPTER, ActionCatalog, normalized, owner_state_refusal
from .case_verification import field_status
from .cases import effect_values
from .goal2_exec_owner import OwnerRejected, owner_error_code

SCHEMA = "plx-owner-request/v1"
ALLOW_ENV = "PLX_WORKBENCH_REAL_DISPATCH"
EVIDENCE_LABEL = "injected-transport"
TRANSPORT_LABELS = frozenset({EVIDENCE_LABEL})
GATES = ("catalog_real_capability", "adapter_registered", "mc_approval_read_back",
         "allow_switch")
_NOT_DEFINITE = frozenset({408, 425, 429})
# What a definite refusal with no code of the owner's vocabulary tells the employee, by
# HTTP status (``owner_code``); another status reads ``http_<status>`` alone.
STATUS_HINTS = {400: "validation", 403: "forbidden", 404: "not_found", 409: "conflict",
                422: "validation"}
# A key fills one path segment: one made only of dots is a dot-segment that survives
# quote() and the server normalises away (RFC 3986 5.2.4), moving the request onto a
# route the catalog never matched, so it is refused.
_KEY_VALUE = re.compile(r"^(?!\.+$)[A-Za-z0-9._~:@-]{1,200}$")


class AdapterError(ValueError):
    """The request cannot be built (nothing is sent)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NotSent(Exception):
    """Certain: this attempt wrote nothing to the owner."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AnswerUnknown(Exception):
    """The write was sent and its answer does not say whether it applied."""


@dataclass(frozen=True)
class OwnerRequest:
    """One request to an owning system; the transport resolves ``system`` to its host
    and adds the credential of the Workbench identity (never part of the request)."""

    system: str
    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "system": self.system, "method": self.method,
                "path": self.path, "headers": dict(self.headers),
                "body": dict(self.body) if self.body is not None else None}


class Transport(Protocol):
    evidence: str

    def send(self, request: OwnerRequest) -> Mapping[str, Any]:
        """``{"status": int, "body": Any}``; raises when no answer arrived."""


def allow_switch_on(capability: str, environ: Mapping[str, str] | None = None, *,
                    transport: Any = None, gate_request: bool = False) -> bool:
    """Whether ``ALLOW_ENV`` lists exactly ``capability`` (comma separated), or the active
    test mode names it (``twin.mode.allowed``) and ``transport``, the one that would carry
    the write, is the write twin's (exact type ``TwinTransport``). A view or ``status``
    names no transport, so it shows a test-mode allow as off. ``gate_request``: the MC
    gate request, which sends nothing and has no transport yet; the dispatch gate always
    names the dispatcher's transport and decides again."""
    raw = (os.environ if environ is None else environ).get(ALLOW_ENV, "")
    if capability in {part.strip() for part in raw.split(",")}:
        return True
    return twin_mode.allowed(capability) and (gate_request
                                              or type(transport) is TwinTransport)


def transport_admitted(transport: Any) -> bool:
    """A reviewed transport: an accepted label with a ``send``, or the write twin's own
    transport (exact type) while test mode is active."""
    label = getattr(transport, "evidence", None)
    if label == twin_mode.TWIN_LABEL:
        return type(transport) is TwinTransport and twin_mode.active()
    return label in TRANSPORT_LABELS and callable(getattr(transport, "send", None))


def unmet_gates(intent: Any, *, adapter_registered: bool | None = None,
                mc_read_back: bool | None = None,
                environ: Mapping[str, str] | None = None,
                transport: Any = None, gate_request: bool = False) -> list[str]:
    """The gates a real-system intent has not met, in ``GATES`` order (``[]``: all met).

    ``adapter_registered`` / ``mc_read_back``: ``None`` where the caller cannot know yet
    (before dispatch), so those gates are not reported (``transport``, ``gate_request``:
    see ``allow_switch_on``)."""
    spec = ActionCatalog.builtin().real_capability(intent.capability)
    relation = (intent.expected_effect or {}).get("relation")
    unmet = []
    if (spec is None or spec["dispatch"] != GATED_ADAPTER
            or spec["target_system"] != intent.owner_system
            or relation not in spec["target_relations"]):
        unmet.append("catalog_real_capability")
    if adapter_registered is False:
        unmet.append("adapter_registered")
    if mc_read_back is False:
        unmet.append("mc_approval_read_back")
    if not allow_switch_on(intent.capability, environ, transport=transport,
                           gate_request=gate_request):
        unmet.append("allow_switch")
    return unmet


def reused_by(warning: Mapping[str, Any], target_key: Mapping[str, Any],
              target_row: Mapping[str, Any], value: Any,
              peers: Any) -> int:
    """How many other records hold ``value`` as the owner compares it, under the same
    ``same`` fields as the target row (none of them empty), skipping records whose ``skip``
    field holds one of its values: the owner's own reuse warning (a catalog
    ``owner_warnings`` entry; ``peers``: ``{"key", "row"}`` of the owner's records)."""
    want = normalized(warning["normalize"], value)
    scope = {name: target_row.get(name) for name in warning["same"]}
    if want is None or any(item in (None, "") for item in scope.values()):
        return 0
    skip = warning.get("skip") or {}
    return sum(1 for peer in peers
               if _digest(dict(peer["key"])) != _digest(dict(target_key))
               and normalized(warning["normalize"], peer["row"].get(warning["field"])) == want
               and all(peer["row"].get(name) == held for name, held in scope.items())
               and peer["row"].get(skip.get("field")) not in skip.get("values", ()))


def owner_code(status: int, body: Any) -> str:
    """A definite refusal as a code: ``http_<status>:<code>`` with the owner's own error
    code (an ``error`` object's ``code`` (BC), else the top-level ``code`` (the Portal))
    while it is in the owner's vocabulary (``owner_error_code``: the Portal's generated
    from its source), else ``http_<status>:<hint>`` (``STATUS_HINTS``), else
    ``http_<status>``. The owner's message is never kept: its words can name a host, a
    person or a row value."""
    if isinstance(body, Mapping):
        error = body.get("error")
        for code in (error.get("code") if isinstance(error, Mapping) else None,
                     body.get("code")):
            if owner_error_code(code):
                return f"http_{status}:{code}"
    hint = STATUS_HINTS.get(status)
    return f"http_{status}:{hint}" if hint else f"http_{status}"


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class OwnerAdapter:
    """One cataloged capability over one transport. Sealed: attributes are set once."""

    SYSTEM = ""
    ADAPTER = ""
    __slots__ = ("capability", "spec", "transport", "_sent")

    def __init__(self, capability: str, transport: Any) -> None:
        if type(self) not in ADAPTER_TYPES:
            raise AdapterError("adapter_type_not_reviewed")
        spec = ActionCatalog.builtin().real_capability(capability)
        if (spec is None or spec["dispatch"] != GATED_ADAPTER
                or spec["adapter"] != self.ADAPTER or spec["target_system"] != self.SYSTEM):
            raise AdapterError("adapter_capability_mismatch")
        # The label is the transport's own claim: any object can declare it. It stops a
        # transport plugged in by mistake, not a hostile one; that is a code change, and
        # review is the control there (devloop/GOAL2-ACTIONS.md section 9).
        if not transport_admitted(transport):
            raise AdapterError("transport_not_reviewed")
        self.capability = capability
        self.spec = spec
        self.transport = transport
        self._sent: set[str] = set()

    def __setattr__(self, name: str, value: Any) -> None:
        if hasattr(self, name):
            raise AttributeError(f"{type(self).__name__} is sealed")
        object.__setattr__(self, name, value)

    # ---- request construction -------------------------------------------

    def _path(self, http: Mapping[str, Any], intent: Any) -> str:
        key = (intent.expected_effect or {}).get("key") or {}
        path = http["path"]
        for placeholder, key_field in http["path_keys"].items():
            value = key.get(key_field) if isinstance(key, Mapping) else None
            if isinstance(value, bool) or not isinstance(value, (str, int)) \
                    or not _KEY_VALUE.fullmatch(str(value)):
                raise AdapterError(f"adapter_key_unmapped:{key_field}")
            path = path.replace("{" + placeholder + "}", quote(str(value), safe=""))
        return path

    def _body(self, intent: Any) -> dict[str, Any]:
        mapping = self.spec["write"]["fields"]
        values = effect_values(intent.expected_effect or {})
        unmapped = sorted(name for name in values if name not in mapping)
        if not values or unmapped:
            raise AdapterError("adapter_field_unmapped:" + ",".join(unmapped))
        return {mapping[name]: value for name, value in values.items()}

    def _conditions(self, intent: Any) -> list[Mapping[str, Any]]:
        """The intent's structured preconditions on a written field, checked on the
        owner's record just before the write (``_pre_write_refusal``, whatever the
        catalog's ``conditional``: an ``if_match`` adds the owner's compare-and-set on
        that same read). The intent does not say which cited record a check was on, so
        one on another record's field of the same name is checked on the target too:
        that can only refuse, never admit, a write."""
        written = set(effect_values(intent.expected_effect or {}))
        return [check for check in intent.checked_preconditions or ()
                if check.get("field") in written]

    def write_request(self, intent: Any, before: Mapping[str, Any] | None = None
                      ) -> OwnerRequest:
        """The exact write: method, path, owner field names, and the idempotency key
        header when the owner keeps one. ``before``: the pre-write read's body."""
        if intent.capability != self.capability or intent.owner_system != self.SYSTEM:
            raise AdapterError("adapter_capability_mismatch")
        write, idem = self.spec["write"], self.spec["idempotency"]
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if idem["owner_support"] == "native":
            headers[idem["header"]] = intent.idempotency_key
        headers.update(self._condition_headers(before))
        return OwnerRequest(self.SYSTEM, write["method"], self._path(write, intent),
                            headers, self._body(intent))

    def read_request(self, intent: Any) -> OwnerRequest:
        read = self.spec["read_back"]
        return OwnerRequest(self.SYSTEM, "GET", self._path(read, intent),
                            {"Accept": "application/json"})

    def _condition_headers(self, before: Mapping[str, Any] | None) -> dict[str, str]:
        return {}

    def refusal(self, intent: Any) -> str | None:
        """Why no request can be built for ``intent`` (checked before anything is sent)."""
        try:
            self.write_request(intent)
            self.read_request(intent)
        except AdapterError as exc:
            return exc.code
        return None

    # ---- the one send ----------------------------------------------------

    def _record(self, intent: Any, body: Any) -> dict[str, Any] | None:
        """The owner's record under the effect's field names (``None``: not a record)."""
        if not isinstance(body, Mapping):
            return None
        mapping = self.spec["read_back"]["fields"]
        return {name: body[api] for name, api in mapping.items() if api in body}

    def _pre_write_refusal(self, intent: Any, record: Mapping[str, Any],
                           body: Mapping[str, Any]) -> str | None:
        """Why the owner's record as just read (``record``: under the effect's field
        names; ``body``: as the owner sent it) does not admit this write, or ``None``.
        Every check can only refuse: an unreadable field or state refuses."""
        for check in self._conditions(intent):
            wanted = "empty" if check["check"] == "field_empty" else "equals_expected"
            if field_status(record, check["field"], check.get("value", ""))["status"] != wanted:
                return "owner_precondition_failed:" + check["field"]
        failures = capability_guards.no_overwrite_failures(
            self.capability, record, list(effect_values(intent.expected_effect or {})))
        if failures:
            return "owner_precondition_failed:" + failures[0]["id"]
        state = self.spec.get("owner_state")
        return None if state is None else owner_state_refusal(state, body)

    def dispatch(self, intent: Any) -> str:
        if not transport_admitted(self.transport):   # e.g. a twin adapter after test mode
            raise NotSent("transport_not_admitted")
        if intent.idempotency_key in self._sent:
            raise NotSent("idempotency_key_already_sent")
        try:
            answer = self.transport.send(self.read_request(intent))
        except AdapterError as exc:
            raise NotSent(exc.code) from None
        except Exception as exc:  # noqa: BLE001 - a read: nothing was written
            raise NotSent("pre_write_read_failed:" + type(exc).__name__) from None
        status = answer.get("status") if isinstance(answer, Mapping) else None
        record = self._record(intent, answer.get("body")) if status == 200 else None
        if record is None:
            raise NotSent(f"pre_write_read_failed:http_{status}")
        refusal = self._pre_write_refusal(intent, record, answer["body"])
        if refusal is not None:
            raise NotSent(refusal)
        try:
            request = self.write_request(intent, answer.get("body"))
        except AdapterError as exc:
            raise NotSent(exc.code) from None
        self._sent.add(intent.idempotency_key)   # committed before the send: never twice
        answer = self.transport.send(request)    # raises: unknown, reconciled by read-back
        status = answer.get("status") if isinstance(answer, Mapping) else None
        if isinstance(status, int) and 200 <= status < 300:
            return f"{self.SYSTEM}:http_{status}:{_digest(answer.get('body'))[:16]}"
        if status == 412:
            raise OwnerRejected("etag_mismatch")
        if isinstance(status, int) and 400 <= status < 500 and status not in _NOT_DEFINITE:
            raise OwnerRejected(owner_code(status, answer.get("body")))
        raise AnswerUnknown(f"http_{status}")

    def read_back(self, intent: Any) -> dict[str, Any]:
        """``ActionPath.read_back`` callable: GET the record and judge every written
        field. The owner reports no operation by key, so nothing here concludes
        ``failed``: a record holding another value is flagged for a person."""
        if not transport_admitted(self.transport):
            return {"outcome": "unknown", "reason": "transport_not_admitted", "fields": {},
                    "evidence": self.transport.evidence}
        try:
            answer = self.transport.send(self.read_request(intent))
        except Exception as exc:  # noqa: BLE001 - a read: report, never guess
            return {"outcome": "unknown", "reason": "read_back_unavailable:"
                    + type(exc).__name__, "fields": {}, "evidence": self.transport.evidence}
        status = answer.get("status") if isinstance(answer, Mapping) else None
        record = self._record(intent, answer.get("body")) if status == 200 else None
        if record is None:
            return {"outcome": "unknown", "reason": f"row_not_returned:http_{status}",
                    "fields": {}, "evidence": self.transport.evidence}
        fields = {name: field_status(record, name, value)
                  for name, value in effect_values(intent.expected_effect).items()}
        statuses = {item["status"] for item in fields.values()}
        if statuses == {"equals_expected"}:
            outcome, reason = "effect_observed", None
        elif "not_inspectable" in statuses or "field_absent" in statuses:
            outcome, reason = "unknown", "field_not_inspectable"
        elif "differs" in statuses:
            outcome, reason = "effect_not_observed", (
                "partial_effect" if "equals_expected" in statuses else "value_differs")
        else:
            outcome, reason = "effect_not_observed", "field_empty"
        return {"outcome": outcome, "reason": reason, "fields": fields,
                "evidence": self.transport.evidence}


class PortalAdapter(OwnerAdapter):
    """The Portal's own API routes (Portal session auth; JSON in and out). The routes
    take no idempotency key and no compare-and-set, so the catalog declares
    ``read_before_write``: the record is re-read just before the write."""

    SYSTEM = "portal"
    ADAPTER = "portal_http"
    __slots__ = ()


ADAPTER_TYPES = (PortalAdapter,)


def status(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Every cataloged gated-adapter capability and whether its allow switch is on.
    Reads no business system."""
    catalog = ActionCatalog.builtin()
    rows = []
    for name in catalog.proposal_ids():
        spec = catalog.real_capability(name)
        if spec is None or spec["dispatch"] != GATED_ADAPTER:
            continue
        rows.append({"capability": name, "target_system": spec["target_system"],
                     "adapter": spec["adapter"], "risk_tier": spec["risk_tier"],
                     "allow_switch": allow_switch_on(name, environ)})
    return {"capabilities": rows, "allow_switch_any": any(r["allow_switch"] for r in rows),
            "transport_labels": sorted(TRANSPORT_LABELS)}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["status"]:
        print("usage: python3 -m operating.action_adapters status", file=sys.stderr)
        return 2
    print(json.dumps(status(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ADAPTER_TYPES", "ALLOW_ENV", "AdapterError", "AnswerUnknown", "EVIDENCE_LABEL",
           "GATES", "NotSent", "OwnerAdapter", "OwnerRequest", "PortalAdapter",
           "TRANSPORT_LABELS", "Transport", "allow_switch_on", "normalized", "owner_code",
           "reused_by", "STATUS_HINTS", "status", "transport_admitted", "unmet_gates"]
