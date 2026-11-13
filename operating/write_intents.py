"""Approve-to-write: a prepared entry for a business system, previewed against the record as
read now, approved by a person who holds a write grant, and executed (today: recorded as
"would write", never sent).

The same pattern as approve-to-send (``operating.card_actions``): the exact payload is shown
before anything happens, a named person confirms it, every step is a hash-chained journal
row, and a repeated click never does the thing twice.

* **Intent** (``prepare``): who prepared it (a person or an autopilot), the target system,
  the operation (``OPERATIONS``: a BC purchase order, a sales order, a purchase order line
  change, a vendor or customer note), the company and record, the exact payload, the
  precondition (the record's ``@odata.etag`` as read, or for a new order the party's), the
  reason and at least one citation. Its id is the digest of all of that, so preparing the
  same entry twice gives the same intent.
* **Preview** (``preview``): each field's current value (read now, read-only, through the
  injected ``current`` reader) beside the proposed one, line by line for a new order;
  refusals (``problems``: a payload $metadata does not describe, a precondition that no
  longer holds, an unreadable record) and warnings (a blocked vendor or customer, a price
  outside the price sheet, a quantity above open demand, and every check that had no
  source); the exact BC API v2.0 request; the compensation note; ``preview_hash``.
* **Approve** (``approve``): the second step after the preview. Only a signed-in, named
  person who holds the write grant ``<system>:<entity set>`` (``WriteGrants``; none by
  default), who did not prepare the intent, and whom the people-exclusion guard decides
  is not excluded (an unavailable guard refuses everyone: the two-person exclusion) may
  approve, for the preview hash they saw, with every warning acknowledged.
* **Execute** (``execute``): re-reads the record, refuses when its etag moved since the
  approval, builds the request and validates it against $metadata again, journals
  ``pending`` and then the outcome. Live writing needs every one of: the owner's flag
  (``LIVE_FLAG``, or ``SANDBOX_FLAG`` for the configured sandbox environment), the record
  that Vince granted the write scopes (``SCOPES_FLAG``), and a write transport. The API
  hands the desk ``integrations.write_transports.for_system("bc")``: None until the owner
  opened BC to approved writes (``PLX_WRITES_LIVE_SYSTEMS``), so until then the outcome is
  ``would_write`` with what is missing. An ``unknown`` outcome blocks a repeat
  until a person checked BC.

Nothing in this module sends HTTP; ``current`` and ``transport`` are injected.

Evidence label of tests: ``isolated-fixture``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from operating.card_actions import Journal, JournalBroken, in_flight

SCHEMA = "plx-write-intent/v1"
LIVE_FLAG = "PLX_WRITES_LIVE"                    # owner's switch for production writes
SANDBOX_FLAG = "PLX_WRITES_SANDBOX_LIVE"         # owner's switch for sandbox writes
SANDBOX_ENV = "PLX_BC_SANDBOX_ENVIRONMENT"       # the BC sandbox environment's name
SCOPES_FLAG = "PLX_WRITES_SCOPES_GRANTED"        # "bc" once Vince granted BC write scopes
GRANTS_FILE_ENV = "PLX_WRITE_GRANTS_FILE"        # JSON outside git: who may approve what
PRODUCTION = "Production"
API_ROOT = "/v2.0/{tenant}/{environment}/api/v2.0"

_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEY = re.compile(r"^[A-Za-z0-9_-]{16,80}$")
_ENVIRONMENT = re.compile(r"^[A-Za-z0-9_-]{1,50}$")
_INTENT_ID = re.compile(r"^wi_[0-9a-f]{24}$")
MAX_LINES = 200
MAX_TEXT = 250
MAX_REASON = 1000
MAX_CITATIONS = 20

# What each operation may write: the entity set, the HTTP shape, the fields a person may
# set (every one also has to be in $metadata), the lines it may carry (deep insert) and
# the party whose block state is checked. Fields BC computes (totals, status, received
# quantities, ids, numbers) are never writable here even though $metadata marks none.
OPERATIONS: dict[str, dict[str, Any]] = {
    "purchase_order_create": {
        "system": "bc", "entity_set": "purchaseOrders", "method": "POST",
        "label": "New purchase order",
        "fields": ("vendorNumber", "vendorId", "orderDate", "postingDate", "currencyCode",
                   "requestedReceiptDate", "shipToName", "shipToContact", "purchaser",
                   "paymentTermsId", "shipmentMethodId"),
        "required": ("vendorNumber",),
        "lines": ("purchaseOrderLines", ("lineType", "itemId", "lineObjectNumber",
                                         "description", "quantity", "directUnitCost",
                                         "unitOfMeasureCode", "locationId",
                                         "expectedReceiptDate", "discountPercent")),
        "line_price": "directUnitCost",
        "party": ("vendors", "vendorId"),
        "undo": ("Delete the new purchase order (DELETE purchaseOrders({id}) with its etag) "
                 "while it is open and nothing is received; once anything is received, "
                 "a purchase return order reverses it."),
    },
    "sales_order_create": {
        "system": "bc", "entity_set": "salesOrders", "method": "POST",
        "label": "New sales order",
        "fields": ("customerNumber", "customerId", "externalDocumentNumber", "orderDate",
                   "postingDate", "currencyCode", "requestedDeliveryDate", "shipToName",
                   "shipToContact", "salesperson", "partialShipping", "paymentTermsId",
                   "shipmentMethodId"),
        "required": ("customerNumber",),
        "lines": ("salesOrderLines", ("lineType", "itemId", "lineObjectNumber",
                                      "description", "quantity", "unitPrice",
                                      "unitOfMeasureCode", "locationId", "shipmentDate",
                                      "discountPercent")),
        "line_price": "unitPrice",
        "party": ("customers", "customerId"),
        "undo": ("Delete the new sales order (DELETE salesOrders({id}) with its etag) while "
                 "nothing is shipped or invoiced; after shipping, a sales return order or "
                 "credit memo reverses it."),
    },
    "purchase_order_line_update": {
        "system": "bc", "entity_set": "purchaseOrderLines", "method": "PATCH",
        "label": "Purchase order line change",
        "fields": ("quantity", "directUnitCost", "expectedReceiptDate", "description",
                   "discountPercent", "locationId"),
        "required": (),
        "lines": None, "line_price": "directUnitCost", "party": None,
        "undo": ("PATCH the line back to the values it had before ({previous}) with the "
                 "etag the write returned."),
    },
    "vendor_note": {
        "system": "bc", "entity_set": "vendors", "method": None, "label": "Vendor note",
        "fields": ("note",), "required": ("note",), "lines": None, "line_price": None,
        "party": ("vendors", None), "undo": "Remove the note line it added.",
        "unwritable": ("BC API v2.0 has no note or comment entity for vendors ($metadata "
                       "describes none), so a vendor note cannot be written through it: "
                       "it needs a custom API page over BC's comment lines."),
    },
    "customer_note": {
        "system": "bc", "entity_set": "customers", "method": None, "label": "Customer note",
        "fields": ("note",), "required": ("note",), "lines": None, "line_price": None,
        "party": ("customers", None), "undo": "Remove the note line it added.",
        "unwritable": ("BC API v2.0 has no note or comment entity for customers ($metadata "
                       "describes none), so a customer note cannot be written through it: "
                       "it needs a custom API page over BC's comment lines."),
    },
}
SYSTEMS = ("bc", "portal")   # the Portal has no write entity yet: every Portal intent refuses
UNDECIDED = frozenset({"pending", "unknown"})
# An approval runs for this long; older, the entry is approved again (ACT-005).
APPROVAL_MAX_AGE = timedelta(days=7)
# The widest decimal a write carries: digits and the power of ten (ACT-007). BC decimals
# are far narrower; the bound keeps a few characters like 1e10000 from expanding.
MAX_DECIMAL_DIGITS, MAX_DECIMAL_EXPONENT = 28, 15
BLANK_BLOCK = frozenset({"", " ", "_x0020_"})


class WriteRefused(ValueError):
    """Nothing was done; ``problems`` say why, ``status`` the HTTP status to answer."""

    def __init__(self, problems: Sequence[str], status: int = 422) -> None:
        super().__init__("; ".join(problems))
        self.problems, self.status = list(problems), status


# --------------------------------------------------------------------------- $metadata

class Metadata:
    """The entity sets and enum types of one BC API v2.0 ``$metadata`` (normalised by
    ``ontology.generator.bc.parse_csdl`` or read from its pin)."""

    def __init__(self, name: str, entity_sets: Mapping[str, Any],
                 enum_types: Mapping[str, Any]) -> None:
        self.name, self.entity_sets, self.enum_types = name, entity_sets, enum_types

    @classmethod
    def from_pin(cls, path: Path | None = None) -> "Metadata":
        """The live capture's normalised $metadata (``bc_metadata.json``)."""
        from ontology.generator import bc
        pin = json.loads(Path(path or bc.PIN_PATH).read_text("utf-8"))
        return cls(f"pin:{pin['document']['sha256'][:12]}", pin["entity_sets"],
                   pin["enum_types"])

    @classmethod
    def from_csdl(cls, raw: bytes, name: str) -> "Metadata":
        from ontology.generator import bc
        parsed = bc.parse_csdl(raw)
        return cls(name, parsed["entity_sets"], parsed["enum_types"])

    def entity(self, entity_set: str) -> Mapping[str, Any] | None:
        found = self.entity_sets.get(entity_set)
        return found if isinstance(found, Mapping) else None

    def properties(self, entity_set: str) -> dict[str, Mapping[str, Any]]:
        found = self.entity(entity_set) or {}
        return {p["name"]: p for p in found.get("properties") or ()}

    def navigation(self, entity_set: str, name: str) -> Mapping[str, Any] | None:
        found = self.entity(entity_set) or {}
        return next((n for n in found.get("navigation") or () if n.get("name") == name), None)

    def line_set(self, header_set: str, nav: str) -> str | None:
        """The entity set holding a header's contained lines (``nav``), when $metadata
        describes it as a contained collection of that set's type."""
        link = self.navigation(header_set, nav)
        if not link or not link.get("collection") or not link.get("contained"):
            return None
        found = self.entity(nav)
        return nav if found and found.get("entity_type") == link.get("target") else None


def default_metadata() -> list[Metadata]:
    """The live capture's pin (the API v2.0 document BC served). Tests also hold every
    request against the committed resource-reference fixture."""
    return [Metadata.from_pin()]


def _typed(value: Any, prop: Mapping[str, Any], meta: Metadata) -> str | None:
    """Why ``value`` does not fit the $metadata property (None: it fits)."""
    kind = str(prop.get("type") or "")
    if value is None:
        return None if prop.get("nullable", True) else "may not be empty"
    if kind == "Edm.String":
        return None if isinstance(value, str) and len(value) <= MAX_TEXT else "not text"
    if kind == "Edm.Guid":
        return None if isinstance(value, str) and _GUID.fullmatch(value) else "not a GUID"
    if kind == "Edm.Boolean":
        return None if isinstance(value, bool) else "not true/false"
    if kind == "Edm.Int32":
        return None if isinstance(value, int) and not isinstance(value, bool) \
            and -2**31 <= value < 2**31 else "not a whole number"
    if kind == "Edm.Decimal":
        return None if _decimal(value) is not None else "not a decimal number"
    if kind == "Edm.Date":
        try:
            ok = isinstance(value, str) and _DAY.fullmatch(value) and date.fromisoformat(value)
        except ValueError:
            ok = False
        return None if ok else "not a date (YYYY-MM-DD)"
    if kind.startswith("enum:"):
        members = meta.enum_types.get(kind[5:]) or ()
        return None if value in members else f"not one of {', '.join(members)}"
    return f"type {kind} is not writable here"


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None     # a float is refused: money and quantities travel as exact text
    text = str(value)
    if len(text) > 64:
        return None
    try:
        found = Decimal(text)
    except InvalidOperation:
        return None
    if not found.is_finite() or len(found.as_tuple().digits) > MAX_DECIMAL_DIGITS \
            or (found != 0 and abs(found.adjusted()) > MAX_DECIMAL_EXPONENT) \
            or found.as_tuple().exponent < -MAX_DECIMAL_EXPONENT:
        return None
    return found


def _read_number(value: Any) -> Decimal | None:
    """A number as BC returned it (JSON numbers arrive as int or float): exact through its
    shortest text, for comparison and checks only, never for a value written."""
    if isinstance(value, float):
        return _decimal(repr(value))
    return _decimal(value)


def validate_request(request: Mapping[str, Any], metadata: Iterable[Metadata]) -> list[str]:
    """Every reason the request does not hold against each $metadata (empty: it holds):
    the entity set, each body field (name and type), and each deep-inserted line."""
    problems: list[str] = []
    entity_set, body = request.get("entity_set"), request.get("body") or {}
    for meta in metadata:
        props = meta.properties(entity_set)
        if not props:
            problems.append(f"{meta.name}: no entity set {entity_set}")
            continue
        for name, value in body.items():
            if isinstance(value, list):
                line_set = meta.line_set(entity_set, name)
                if line_set is None:
                    problems.append(f"{meta.name}: {entity_set} contains no lines {name}")
                    continue
                line_props = meta.properties(line_set)
                for n, line in enumerate(value, 1):
                    for key, item in line.items():
                        prop = line_props.get(key)
                        why = f"is not in {line_set}" if prop is None else _typed(item, prop,
                                                                                    meta)
                        if why:
                            problems.append(f"{meta.name}: line {n} {key} {why}")
                continue
            prop = props.get(name)
            why = f"is not in {entity_set}" if prop is None else _typed(value, prop, meta)
            if why:
                problems.append(f"{meta.name}: {name} {why}")
    return problems


# --------------------------------------------------------------------------- grants

class WriteGrants:
    """Who may approve writes: person (lower-case email or Entra object id) -> grants
    ``<system>:<entity set>``. Read from ``PLX_WRITE_GRANTS_FILE`` (JSON
    ``{"schema": "plx-write-grants/v1", "grants": {person: [grant, ...]}}``), never from
    git; no file, an unreadable one or an invalid entry: nobody holds a grant."""

    def __init__(self, grants: Mapping[str, Iterable[str]] | None = None) -> None:
        self.grants = {str(k).strip().lower(): frozenset(v) for k, v in (grants or {}).items()}

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "WriteGrants":
        path = environ.get(GRANTS_FILE_ENV)
        if not path:
            return cls()
        try:
            raw = json.loads(Path(path).read_text("utf-8"))
        except (OSError, ValueError):
            return cls()
        grants = raw.get("grants") if isinstance(raw, Mapping) \
            and raw.get("schema") == "plx-write-grants/v1" else None
        if not isinstance(grants, Mapping) or not all(
                isinstance(v, list) and all(isinstance(g, str) and ":" in g for g in v)
                for v in grants.values()):
            return cls()
        return cls(grants)

    def holds(self, identities: Iterable[str | None], system: str, entity_set: str) -> bool:
        want = f"{system}:{entity_set}"
        return any(want in self.grants.get(str(i).strip().lower(), ()) for i in identities if i)


# --------------------------------------------------------------------------- people

@dataclass(frozen=True)
class Person:
    """Who prepares or approves: a signed-in person (name, email, Entra object id) or an
    autopilot (``autopilot`` set, no email)."""

    name: str
    email: str | None = None
    oid: str | None = None
    autopilot: str | None = None

    def ids(self) -> list[str]:
        return [i for i in (self.email, self.oid) if i]

    def record(self) -> dict[str, Any]:
        return {"name": self.name[:120], "email": (self.email or "").lower() or None,
                "oid": self.oid, "autopilot": self.autopilot}


def _prepared_by(prepared_by: Mapping[str, Any], who: Person) -> bool:
    """Whether ``who`` is the person (or autopilot) that prepared an intent, for seeing it
    without a grant (ACT-006): by the Entra object id when the intent recorded one (a
    session without one is not its preparer: a reused or re-pointed mailbox never takes
    over another person's entries); by the exact mailbox only for an intent recorded before
    object ids were kept; an autopilot by its name and kind."""
    if prepared_by.get("autopilot") or who.autopilot:
        return bool(who.autopilot) and who.email is None \
            and prepared_by.get("autopilot") == who.autopilot \
            and prepared_by.get("name") == who.name
    oid = prepared_by.get("oid")
    if isinstance(oid, str) and oid.strip():
        return isinstance(who.oid, str) and who.oid.strip().lower() == oid.strip().lower()
    email = prepared_by.get("email")
    return bool(email) and email == (who.email or "").lower()


def _person_ids(email: Any, oid: Any) -> set[str]:
    """One person's comparable identities: the mailbox (trimmed, lower case, a ``+tag``
    sub-address dropped: the rule of ``plx_hub_guard.normalize_email``, kept here because
    this module imports nothing that reaches a source) and the Entra object id."""
    ids = set()
    if isinstance(email, str) and "@" in email.strip():
        local, _, domain = email.strip().lower().rpartition("@")
        ids.add("mail:" + local.split("+", 1)[0] + "@" + domain)
    if isinstance(oid, str) and oid.strip():
        ids.add("oid:" + oid.strip().lower())
    return ids


def _same_person(prepared_by: Mapping[str, Any], who: Person) -> bool:
    """The two-person rule (SEC-002): the approver is the preparer when they share any
    identity, the Entra object id or the mailbox. A renamed mailbox or an alias keeps the
    object id; a session without an email still has it. An autopilot prepared nothing a
    person could share."""
    if prepared_by.get("autopilot"):
        return False
    return bool(_person_ids(prepared_by.get("email"), prepared_by.get("oid"))
                & _person_ids(who.email, who.oid))


def _excluded(guard: Any, person: Person) -> str | None:
    """Why the people-exclusion guard does not clear ``person`` (None: cleared). An
    unavailable guard clears nobody; an identity it cannot decide is not cleared."""
    if guard is None or getattr(guard, "unavailable", "unavailable"):
        return "The people-exclusion guard is unavailable: no write is approved."
    try:
        decided = [i for i in person.ids() if guard.decides(i)]
        if not decided:
            return "The people-exclusion guard cannot decide this person: no write is approved."
        if any(guard.is_excluded(i) for i in decided):
            return "This person may not take part in an approval."
    except Exception:   # a guard that cannot answer clears nobody
        return "The people-exclusion guard could not decide: no write is approved."
    return None


# --------------------------------------------------------------------------- requests

def _json(value: Any) -> str:
    """JSON with decimals as exact numbers (never through a float)."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return "{" + ",".join(json.dumps(str(k)) + ":" + _json(v) for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def build_request(intent: Mapping[str, Any], metadata: Sequence[Metadata]) -> dict[str, Any]:
    """The exact BC API v2.0 request the intent stands for: method, path (tenant left as
    a placeholder), headers (``If-Match`` the etag as read for a change) and body, with
    decimals as exact numbers, and its $metadata check (``problems``)."""
    op = OPERATIONS[intent["operation"]]
    if op["method"] is None:
        return {"method": None, "path": None, "headers": {}, "body": None,
                "body_json": None, "entity_set": op["entity_set"],
                "problems": [op["unwritable"]]}
    root = API_ROOT.replace("{environment}", intent["environment"])
    company = f"/companies({intent['company_id']})/{op['entity_set']}"
    lines_nav = (op["lines"] or (None, ()))[0]
    payload = dict(intent["payload"])
    lines = payload.pop("lines", None)
    meta = metadata[0] if metadata else None

    def typed(entity_set: str, values: Mapping[str, Any]) -> dict[str, Any]:
        props = meta.properties(entity_set) if meta else {}
        return {k: (_decimal(v) if (props.get(k) or {}).get("type") == "Edm.Decimal"
                    and _decimal(v) is not None else v) for k, v in values.items()}

    body = typed(op["entity_set"], payload)
    if lines_nav and lines:
        body[lines_nav] = [typed(lines_nav, line) for line in lines]
    headers = {"Content-Type": "application/json"}
    if op["method"] == "PATCH":
        path = f"{root}{company}({intent['record_id']})"
        headers["If-Match"] = intent["precondition"]["etag"]
    else:
        path = f"{root}{company}"
    request = {"method": op["method"], "path": path, "headers": headers,
               "entity_set": op["entity_set"], "body": body}
    request["problems"] = validate_request(request, metadata)
    request["body_json"] = _json(body)       # the exact wire text: decimals as numbers
    request["body"] = _plain(body)           # journal-safe: decimals as exact text
    return request


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


# --------------------------------------------------------------------------- the desk

def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, limit: int) -> str:
    return str(value).strip()[:limit] if isinstance(value, str) else ""


def _check_payload(operation: str, payload: Mapping[str, Any]) -> list[str]:
    """The intent's own shape: only fields the operation writes, the required ones, and
    lines (for a new order: at least one, each with a quantity above zero)."""
    op = OPERATIONS[operation]
    problems = [f"{k} cannot be set by {operation}" for k in payload
                if k not in op["fields"] and not (k == "lines" and op["lines"])]
    problems += [f"{k} is required" for k in op["required"] if payload.get(k) in (None, "")]
    if op["method"] == "PATCH" and not payload:
        problems.append("The change sets no field")
    if op["lines"]:
        lines = payload.get("lines")
        if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
            problems.append(f"A new order needs 1..{MAX_LINES} lines")
            return problems
        allowed = op["lines"][1]
        for n, line in enumerate(lines, 1):
            if not isinstance(line, Mapping):
                problems.append(f"line {n} is not a line")
                continue
            problems += [f"line {n}: {k} cannot be set" for k in line if k not in allowed]
            qty = _decimal(line.get("quantity"))
            if qty is None or qty <= 0:
                problems.append(f"line {n}: quantity must be above zero")
    if "quantity" in payload:
        qty = _decimal(payload["quantity"])
        if qty is None or qty < 0:
            problems.append("quantity must be zero or more")
    return problems


@dataclass
class Facts:
    """What the warnings are checked against, each optional (absent: the check is named
    as not made). ``price_sheet(item, party) -> (low, high)``; ``open_demand(item) ->
    Decimal``."""

    price_sheet: Callable[[str, str | None], tuple[Decimal, Decimal] | None] | None = None
    open_demand: Callable[[str], Decimal | None] | None = None


@dataclass
class WriteContext:
    """One request's world: the environment (flags, grants), the people-exclusion guard,
    ``current(environment, company_id, entity_set, record_id) -> {"etag", "fields"} |
    None`` (a read-only read of the record now), the warning facts, the $metadata."""

    environ: Mapping[str, str]
    exclusion: Any
    current: Callable[[str, str, str, str], Mapping[str, Any] | None] | None
    facts: Facts = field(default_factory=Facts)
    metadata: Sequence[Metadata] = field(default_factory=list)
    grants: WriteGrants | None = None

    def granted(self) -> WriteGrants:
        return self.grants if self.grants is not None else WriteGrants.from_env(self.environ)


def live_gate(environ: Mapping[str, str], environment: str, transport: Any) -> list[str]:
    """What a live write to ``environment`` still lacks (empty: it may be sent). The
    sandbox is only the environment ``SANDBOX_ENV`` names, never Production."""
    missing = []
    sandbox = (environ.get(SANDBOX_ENV) or "").strip()
    is_sandbox = bool(sandbox) and sandbox.lower() == environment.lower() \
        and environment.lower() != PRODUCTION.lower()
    if is_sandbox:
        if environ.get(SANDBOX_FLAG) != "1":
            missing.append(f"Sandbox writing is off ({SANDBOX_FLAG}=1 not set)")
    elif environ.get(LIVE_FLAG) != "1":
        missing.append(f"Writing is off ({LIVE_FLAG}=1 not set)")
    if "bc" not in (environ.get(SCOPES_FLAG) or "").replace(",", " ").split():
        missing.append(f"BC write scopes are not granted ({SCOPES_FLAG})")
    if transport is None:
        missing.append("No BC write client exists in this build: the request is recorded, "
                       "not sent")
    return missing


APPROVED_ENV = "PLX_APPROVED_WRITES"   # ``autopilot_write.APPROVED_ENV``: kinds opened
DESK_KIND = "bc_write"


def desk_gate(environ: Mapping[str, str], environment: str, transport: Any) -> list[str]:
    """``live_gate`` and the owner's per-kind switch every approved write needs: the desk
    writes BC only when ``bc_write`` is in ``PLX_APPROVED_WRITES``."""
    missing = live_gate(environ, environment, transport)
    if DESK_KIND not in {k.strip() for k in str(environ.get(APPROVED_ENV) or "").split(",")}:
        missing.insert(0, f"BC writes are not opened to approved writes ({APPROVED_ENV} "
                          f"lacks {DESK_KIND})")
    return missing


def _status_text(row: Mapping[str, Any]) -> str:
    status = row["status"]
    return {"prepared": "Prepared, waiting for approval",
            "approved": f"Approved by {row['actor']['name']}",
            "would_write": "Would write (dry run): " + "; ".join(row.get("missing") or ()),
            "written": "Written", "failed": "The write was refused: nothing is retried",
            "pending": "Outcome unknown: check BC before running it again",
            "unknown": "Outcome unknown: check BC before running it again"}.get(status, status)


class WriteDesk:
    """Prepares, previews, approves and executes write intents (module doc), one
    hash-chained journal (``state_dir/writes/journal.jsonl``) for all of it."""

    def __init__(self, journal: Journal, *, transport: Any = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.journal, self.transport = journal, transport
        self._dispatching: set[str] = set()   # intent ids this process is sending now
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def for_state_dir(cls, state_dir: Path | None, **options: Any) -> "WriteDesk":
        return cls(Journal(Path(state_dir) / "writes" / "journal.jsonl" if state_dir else None),
                   **options)

    def _now(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")

    def _rows(self) -> list[dict[str, Any]]:
        try:
            return self.journal.rows()
        except JournalBroken:
            raise WriteRefused(["The writes journal failed its integrity check: nothing is "
                                "done until it is repaired."], 503) from None

    # ---- prepare

    def prepare(self, spec: Mapping[str, Any], by: Person) -> dict[str, Any]:
        """Record an intent (module doc); the same entry again returns the same intent."""
        problems: list[str] = []
        if not by.name or not (by.ids() or by.autopilot):
            raise WriteRefused(["Only a named person or autopilot prepares a write."], 403)
        system, operation = spec.get("system"), spec.get("operation")
        if system not in SYSTEMS:
            raise WriteRefused([f"Unknown system {system!r}."])
        if system != "bc":
            raise WriteRefused([f"No {system} write entity is defined yet."])
        if operation not in OPERATIONS or OPERATIONS[operation]["system"] != system:
            raise WriteRefused([f"Unknown operation {operation!r}."])
        op = OPERATIONS[operation]
        environment = spec.get("environment") or PRODUCTION
        company, record = spec.get("company_id"), spec.get("record_id")
        payload, pre = spec.get("payload"), spec.get("precondition")
        reason = _text(spec.get("reason"), MAX_REASON)
        cites = spec.get("citations")
        if not isinstance(environment, str) or not _ENVIRONMENT.fullmatch(environment):
            problems.append("environment is not a BC environment name")
        if not isinstance(company, str) or not _GUID.fullmatch(company):
            problems.append("company_id must be a GUID")
        needs_record = op["method"] == "PATCH" or op["method"] is None
        if needs_record and (not isinstance(record, str) or not _GUID.fullmatch(record)):
            problems.append("record_id must be the GUID of the record it changes")
        if not needs_record and record is not None:
            problems.append("a new record has no record_id")
        if not isinstance(payload, Mapping):
            problems.append("payload must be an object")
        else:
            problems += _check_payload(operation, payload)
        if not isinstance(pre, Mapping) or not isinstance(pre.get("etag"), str) \
                or not pre["etag"]:
            problems.append("precondition.etag (the record's etag as read) is required")
        if op["party"] and op["method"] == "POST" and isinstance(pre, Mapping) \
                and not (isinstance(pre.get("party_id"), str) and _GUID.fullmatch(pre["party_id"])):
            problems.append("precondition.party_id (the vendor or customer read) is required")
        if not reason:
            problems.append("A reason is required")
        if not isinstance(cites, list) or not cites or len(cites) > MAX_CITATIONS \
                or not all(isinstance(c, str) and c.strip() for c in cites):
            problems.append(f"1..{MAX_CITATIONS} citations are required")
        if problems:
            raise WriteRefused(problems)
        intent = {"schema": SCHEMA, "system": system, "operation": operation,
                  "entity_set": op["entity_set"], "environment": environment,
                  "company_id": company.lower(), "record_id": record.lower() if record else None,
                  "payload": json.loads(json.dumps(_plain(payload))),
                  "precondition": {k: pre[k] for k in ("etag", "party_id") if pre.get(k)},
                  "reason": reason, "citations": [c.strip()[:500] for c in cites],
                  "prepared_by": by.record()}
        intent["intent_id"] = "wi_" + _digest(intent)[:24]
        with self.journal.locked() as held:
            same = [r for r in held.rows if r["intent_id"] == intent["intent_id"]]
            if same:
                return {**self._view(held.rows, intent["intent_id"]), "replayed": True}
            held.append({"intent_id": intent["intent_id"], "status": "prepared",
                         "at": self._now(), "actor": by.record(), "intent": intent})
            return {**self._view(held.rows, intent["intent_id"]), "replayed": False}

    # ---- read

    def intent(self, intent_id: str) -> dict[str, Any] | None:
        if not isinstance(intent_id, str) or not _INTENT_ID.fullmatch(intent_id):
            return None
        rows = self._rows()
        first = next((r for r in rows if r["intent_id"] == intent_id), None)
        return dict(first["intent"]) if first else None

    def _view(self, rows: Sequence[Mapping[str, Any]], intent_id: str) -> dict[str, Any]:
        mine = [r for r in rows if r["intent_id"] == intent_id]
        last = mine[-1]
        approved = next((r for r in reversed(mine) if r["status"] == "approved"), None)
        return {"intent": mine[0]["intent"], "status": last["status"],
                "text": _status_text(last), "at": last["at"],
                "approved_by": approved["actor"] if approved else None,
                "request": last.get("request"), "missing": list(last.get("missing") or ()),
                "history": [{"status": r["status"], "at": r["at"], "actor": r["actor"]["name"]}
                            for r in mine]}

    def visible_to(self, intent: Mapping[str, Any], who: Person, ctx: WriteContext) -> bool:
        """The preparer and readers holding the grant for the intent's entity see it."""
        return _prepared_by(intent["prepared_by"], who) or ctx.granted().holds(
            who.ids(), intent["system"], intent["entity_set"])

    def listing(self, who: Person, ctx: WriteContext, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._rows()
        seen: list[str] = []
        for row in rows:
            if row["intent_id"] not in seen:
                seen.append(row["intent_id"])
        out = [self._view(rows, i) for i in reversed(seen)]
        return [v for v in out if self.visible_to(v["intent"], who, ctx)][:limit]

    def status(self, intent_id: str) -> dict[str, Any]:
        return self._view(self._rows(), intent_id)

    # ---- preview

    def preview(self, intent_id: str, ctx: WriteContext) -> dict[str, Any]:
        """Field by field against the record as read now (module doc). Nothing is done."""
        intent = self.intent(intent_id)
        if intent is None:
            raise WriteRefused(["No such write intent."], 404)
        op = OPERATIONS[intent["operation"]]
        problems: list[str] = []
        warnings: list[dict[str, str]] = []
        current_etag, current_fields = None, {}
        env, company = intent["environment"], intent["company_id"]
        if op["method"] == "PATCH" or op["method"] is None:
            read = self._read(ctx, env, company, intent["entity_set"], intent["record_id"])
            if read is None:
                problems.append("The record could not be read now: nothing is approved "
                                "without its current values.")
            else:
                current_etag, current_fields = read["etag"], dict(read["fields"])
                if current_etag != intent["precondition"]["etag"]:
                    problems.append("The record changed since the entry was prepared "
                                    "(its etag moved): prepare it again from the current "
                                    "record.")
        party_set = (op["party"] or (None, None))[0]
        party_id = intent["precondition"].get("party_id") if op["method"] == "POST" \
            else (intent["record_id"] if op["method"] is None else None)
        party = None
        if party_set and party_id:
            party = current_fields if op["method"] is None and current_fields else \
                (self._read(ctx, env, company, party_set, party_id) or {}).get("fields")
            if party is None:
                problems.append("The vendor or customer could not be read now.")
            elif str(party.get("blocked") or "").strip() not in BLANK_BLOCK:
                warnings.append({"code": "party_blocked",
                                 "text": f"{party.get('displayName') or 'The party'} is "
                                         f"blocked in BC ({party.get('blocked')})."})
            elif op["method"] == "POST" and party is not None:
                number_field = op["required"][0]
                if party.get("number") and intent["payload"].get(number_field) \
                        and party["number"] != intent["payload"][number_field]:
                    problems.append(f"{number_field} does not match the party read "
                                    f"({party.get('number')}).")
        changes = self._changes(intent, current_fields)
        warnings += self._warnings(intent, current_fields, party, ctx)
        request = build_request(intent, ctx.metadata)
        problems += request.pop("problems")
        undo = op["undo"].replace("{previous}", ", ".join(
            f"{c['field']}={c['current']}" for c in changes if c.get("changed")) or "none")
        found = {"intent_id": intent_id, "intent": intent, "label": op["label"],
                 "current_etag": current_etag, "changes": changes,
                 "lines": self._lines(intent, ctx), "warnings": warnings,
                 "problems": problems, "request": request, "undo": undo,
                 "live_missing": desk_gate(ctx.environ, env, self.transport)}
        found["preview_hash"] = _digest({"intent": intent, "etag": current_etag,
                                         "request": request, "warnings": warnings})
        return found

    @staticmethod
    def _read(ctx: WriteContext, env: str, company: str, entity_set: str,
              record_id: str) -> Mapping[str, Any] | None:
        if ctx.current is None:
            return None
        try:
            found = ctx.current(env, company, entity_set, record_id)
        except Exception:   # an unreadable record is a refusal, never a pass
            return None
        if not isinstance(found, Mapping) or not isinstance(found.get("etag"), str) \
                or not isinstance(found.get("fields"), Mapping):
            return None
        return found

    @staticmethod
    def _changes(intent: Mapping[str, Any], current: Mapping[str, Any]) -> list[dict[str, Any]]:
        out = []
        for name, proposed in intent["payload"].items():
            if name == "lines":
                continue
            now = current.get(name)
            same = now == proposed or (_read_number(now) is not None and
                                       _decimal(proposed) is not None and
                                       _read_number(now) == _decimal(proposed))
            out.append({"field": name, "current": now, "proposed": proposed,
                        "changed": not same})
        return out

    @staticmethod
    def _lines(intent: Mapping[str, Any], ctx: WriteContext) -> list[dict[str, Any]]:
        op = OPERATIONS[intent["operation"]]
        out = []
        for n, line in enumerate(intent["payload"].get("lines") or (), 1):
            qty, price = _decimal(line.get("quantity")), _decimal(line.get(op["line_price"]))
            amount = format(qty * price, "f") if qty is not None and price is not None else None
            out.append({"n": n, "fields": dict(line), "amount": amount})
        return out

    @staticmethod
    def _warnings(intent: Mapping[str, Any], current: Mapping[str, Any],
                  party: Mapping[str, Any] | None, ctx: WriteContext) -> list[dict[str, str]]:
        op = OPERATIONS[intent["operation"]]
        out: list[dict[str, str]] = []
        payload = intent["payload"]
        price_field = op["line_price"]
        if price_field is None:
            return out
        if op["lines"]:
            lines = [(n, line) for n, line in enumerate(payload.get("lines") or (), 1)]
        else:
            merged = {**current, **payload}
            lines = [(1, merged)]
        party_no = (party or {}).get("number")
        for n, line in lines:
            item = line.get("lineObjectNumber") or line.get("itemId") or ""
            price, qty = _decimal(line.get(price_field)), _decimal(line.get("quantity"))
            if price is not None and (price_field in payload or op["lines"]):
                band = ctx.facts.price_sheet(str(item), party_no) if ctx.facts.price_sheet \
                    else None
                if ctx.facts.price_sheet is None:
                    out.append({"code": "price_not_checked",
                                "text": f"Line {n}: no price sheet source; price not checked."})
                elif band is None:
                    out.append({"code": "price_not_listed",
                                "text": f"Line {n}: {item} is not on the price sheet."})
                elif not band[0] <= price <= band[1]:
                    out.append({"code": "price_outside_sheet",
                                "text": f"Line {n}: {price} is outside the price sheet "
                                        f"({band[0]} to {band[1]})."})
            if qty is not None and intent["operation"] != "sales_order_create" \
                    and ("quantity" in payload or op["lines"]):
                demand = ctx.facts.open_demand(str(item)) if ctx.facts.open_demand else None
                if ctx.facts.open_demand is None:
                    out.append({"code": "demand_not_checked",
                                "text": f"Line {n}: no open-demand source; quantity not checked."})
                elif demand is not None and qty > demand:
                    out.append({"code": "quantity_over_demand",
                                "text": f"Line {n}: {qty} is more than the open demand "
                                        f"of {demand}."})
            received = _read_number(current.get("receivedQuantity"))
            if not op["lines"] and qty is not None and received is not None and qty < received:
                out.append({"code": "below_received",
                            "text": f"Quantity {qty} is below the {received} already received."})
        return out

    def _lapsed(self, approval: Mapping[str, Any], intent: Mapping[str, Any],
                ctx: WriteContext, *, check_age: bool = True) -> tuple[str, int] | None:
        """Why a journalled approval no longer stands (ACT-005), or None: the approver
        still holds the grant, neither approver nor preparer is excluded now, and it is
        younger than ``APPROVAL_MAX_AGE``. A lapsed entry is approved again."""
        actor = approval.get("actor") or {}
        approver = Person(str(actor.get("name") or ""), actor.get("email"), actor.get("oid"))
        if not approver.ids() or not ctx.granted().holds(
                approver.ids(), intent["system"], intent["entity_set"]):
            return ("The person who approved this entry no longer holds a write grant for "
                    "it: approve it again.", 409)
        prep = intent["prepared_by"]
        excluded = _excluded(ctx.exclusion, approver) or (
            _excluded(ctx.exclusion, Person(prep["name"], prep.get("email"), prep.get("oid")))
            if not prep.get("autopilot") else None)
        if excluded:
            return (excluded, 403)
        if not check_age:
            return None
        try:
            approved_at = datetime.fromisoformat(str(approval.get("at")))
        except ValueError:
            approved_at = None
        if approved_at is None or approved_at.tzinfo is None \
                or self.clock() - approved_at > APPROVAL_MAX_AGE:
            return ("The approval is older than "
                    f"{APPROVAL_MAX_AGE.days} days: approve it again.", 409)
        return None

    # ---- approve

    def approve(self, intent_id: str, who: Person, ctx: WriteContext, *, confirmed: bool,
                shown_hash: str | None, acknowledge_warnings: bool = False) -> dict[str, Any]:
        """The second step after the preview (module doc). Raises ``WriteRefused`` when
        any rule does not hold; the approval is journalled."""
        if confirmed is not True:
            raise WriteRefused(["Nothing is approved without your confirm."])
        if who.autopilot or not who.name or not who.ids():
            raise WriteRefused(["Only a signed-in, named person approves a write."], 403)
        intent = self.intent(intent_id)
        if intent is None:
            raise WriteRefused(["No such write intent."], 404)
        if not ctx.granted().holds(who.ids(), intent["system"], intent["entity_set"]):
            raise WriteRefused([f"You hold no write grant for {intent['system']} "
                                f"{intent['entity_set']}."], 403)
        prep = intent["prepared_by"]
        if _same_person(prep, who):
            raise WriteRefused(["The person who prepared a write cannot approve it."], 403)
        excluded = _excluded(ctx.exclusion, who) or (
            _excluded(ctx.exclusion, Person(prep["name"], prep.get("email"), prep.get("oid")))
            if not prep.get("autopilot") else None)
        if excluded:
            raise WriteRefused([excluded], 403)
        found = self.preview(intent_id, ctx)
        if found["problems"]:
            raise WriteRefused(found["problems"], 409)
        if shown_hash != found["preview_hash"]:
            raise WriteRefused(["The entry or the record changed since your preview: "
                                "review it again before you approve."], 409)
        if found["warnings"] and acknowledge_warnings is not True:
            raise WriteRefused(["Acknowledge the warnings: " + " ".join(
                w["text"] for w in found["warnings"])], 409)
        with self.journal.locked() as held:
            status = self._view(held.rows, intent_id)["status"]
            if status != "prepared":
                standing = next((r for r in reversed(held.rows) if r["intent_id"] == intent_id
                                 and r["status"] == "approved"), None)
                # a standing approval is replayed; a lapsed one (ACT-005) is replaced by
                # this person's, who passed every rule above
                if status == "approved" and standing is not None \
                        and not self._lapsed(standing, intent, ctx):
                    return {**self._view(held.rows, intent_id), "replayed": True}
                if status != "approved":
                    raise WriteRefused([f"This entry is already {status}."], 409)
            held.append({"intent_id": intent_id, "status": "approved", "at": self._now(),
                         "actor": who.record(), "preview_hash": found["preview_hash"],
                         "etag": found["current_etag"],
                         "warnings": [w["code"] for w in found["warnings"]]})
            return {**self._view(held.rows, intent_id), "replayed": False}

    # ---- execute

    def execute(self, intent_id: str, who: Person, ctx: WriteContext, *,
                idempotency_key: str, checked_bc: bool = False,
                live: bool | None = None) -> dict[str, Any]:
        """Run an approved intent once (module doc). Today the outcome is ``would_write``.
        ``live``: what the person's page said the run would be (False: a dry run); a run
        that would now write live is refused unless the page said so, so a person who
        pressed "Write (dry run)" never writes BC. None: the caller states nothing."""
        if who.autopilot or not who.name or not who.ids():
            raise WriteRefused(["Only a signed-in, named person runs a write."], 403)
        if not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key):
            raise WriteRefused(["The request carries no valid idempotency key."])
        intent = self.intent(intent_id)
        if intent is None:
            raise WriteRefused(["No such write intent."], 404)
        if not ctx.granted().holds(who.ids(), intent["system"], intent["entity_set"]):
            raise WriteRefused([f"You hold no write grant for {intent['system']} "
                                f"{intent['entity_set']}."], 403)
        excluded = _excluded(ctx.exclusion, who)
        if excluded:
            raise WriteRefused([excluded], 403)
        rows = self._rows()
        mine = [r for r in rows if r["intent_id"] == intent_id]
        approval = next((r for r in reversed(mine) if r["status"] == "approved"), None)
        same = [r for r in mine if r.get("idempotency_key") == idempotency_key]
        if same and same[-1]["status"] not in UNDECIDED:
            return {**self._view(rows, intent_id), "replayed": True}
        if approval is None:
            raise WriteRefused(["This entry is not approved."], 409)
        done = [r for r in mine if r["status"] in ("would_write", "written", "failed")]
        if done:
            return {**self._view(rows, intent_id), "replayed": True}
        # ACT-005: the approval stands only while its people still may (``_lapsed``); a
        # retry of a run with an undecided outcome continues that approved run, so only
        # its people are judged again, not its age.
        lapsed = self._lapsed(approval, intent, ctx, check_age=not any(
            r["status"] in UNDECIDED for r in mine))
        if lapsed:
            raise WriteRefused([lapsed[0]], lapsed[1])
        if self._running(mine):
            raise WriteRefused(["Another run of this entry is in progress: wait for its "
                                "outcome."], 409)
        if mine[-1]["status"] in UNDECIDED and checked_bc is not True:
            raise WriteRefused(["An earlier run of this entry has an unknown outcome: check "
                                "BC, then confirm it is not there to run it again."], 409)
        found = self.preview(intent_id, ctx)
        if found["problems"]:
            raise WriteRefused(found["problems"], 409)
        if found["current_etag"] != approval["etag"] \
                or found["preview_hash"] != approval["preview_hash"]:
            raise WriteRefused(["The record changed since the approval: approve it again."],
                               409)
        request = found["request"]
        missing = desk_gate(ctx.environ, intent["environment"], self.transport)
        if not missing and live is not None and live is not True:
            raise WriteRefused(["This entry would now be written live in BC, but the page "
                                "showed a dry run: preview it again before writing."], 409)
        with self.journal.locked() as held:
            now_rows = [r for r in held.rows if r["intent_id"] == intent_id]
            if any(r["status"] in ("would_write", "written", "failed") for r in now_rows):
                return {**self._view(held.rows, intent_id), "replayed": True}
            if (now_rows[-1]["seq"] != mine[-1]["seq"]
                    and now_rows[-1]["status"] in UNDECIDED) or self._running(now_rows):
                raise WriteRefused(["Another run of this entry is in progress."], 409)
            base = {"intent_id": intent_id, "actor": who.record(),
                    "idempotency_key": idempotency_key, "request": request}
            if missing:
                held.append({**base, "status": "would_write", "at": self._now(),
                             "missing": missing})
                return {**self._view(held.rows, intent_id), "replayed": False}
            held.append({**base, "status": "pending", "at": self._now()})
            self._dispatching.add(intent_id)
        try:
            outcome = self.transport.send(request)   # reached only with nothing missing
            status = outcome.get("status") if isinstance(outcome, Mapping) else None
            with self.journal.locked() as held:
                held.append({**base, "status": status if status in ("written", "failed")
                             else "unknown", "at": self._now(),
                             "http_status": (outcome or {}).get("http_status")})
                return {**self._view(held.rows, intent_id), "replayed": False}
        finally:
            self._dispatching.discard(intent_id)

    def _running(self, rows: Sequence[Mapping[str, Any]]) -> bool:
        """Whether a run of this entry is still going out (ACT-001): this process is
        sending it, or its latest row is a ``pending`` within
        ``card_actions.IN_FLIGHT_SECONDS``. A check of BC never runs it again beside it."""
        return bool(rows) and (rows[-1]["intent_id"] in self._dispatching
                               or in_flight(rows[-1], self.clock()))
