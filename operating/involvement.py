"""What a reader is involved in: the records of their briefing that are theirs.

A briefing item is the reader's own (``yours``) when the reader's identity is tied to its
record by the ontology, read AS THAT READER through the generic primitives
(``plx.object``, ``plx.around``) so nothing the reader may not open ever makes an item
theirs, and nothing here widens what they see (the items are their already-trimmed inbox):

* ``named``: a property of the record names one of the reader's identities exactly (an
  address or the Entra object id; case-insensitive, a whole value or one element of a
  JSON list): a Mission Control task's assignee or accountable owner, a buyer, owner or
  contact property once a source serves one. Never a name or a partial match
  (conservative identity: two people can share a name);
* ``your_mail``: a message in the reader's own mailbox (the ``Email`` type's mailbox
  owner property is their Entra object id) mentions the record (the schema's
  ``Email.mentions_<type>`` link, at the primitives' default tier floor: a lone lead is
  not involvement);
* ``you_correspond``: the record's counterpart (a link from its type to a type messages
  are linked to as their sender: a purchase order's supplier, a sales order's customer)
  has written to the reader's own mailbox.

Every path is read from the schema (``link_types``, ``object_types``), never a list of
kinds. The reader's identity (``Identity``) comes from their employee record: the
addresses and object id the Portal and Entra rows confirm (``source_identities``), never
from text. A reader with no identity (a fixture or API reader) is involved in nothing.

Reads are bounded (``MAX_OBJECTS`` records, ``BUDGET_SECONDS``); what was not checked is
counted (``not_checked``) and the result says it is not complete, never silently "not
yours". Lots carry no area or location in the ontology today, so a lot is the reader's
only by the rules above; a lot is otherwise routed to them by their department.

Evidence label of fixture runs: ``isolated-fixture``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

MAX_OBJECTS = 120        # records checked per briefing (in its ranked order)
BUDGET_SECONDS = 4.0     # time for every check of one briefing
MAIL_FROM = "Email"      # the message type (``ontology.schema.MENTION_LINK_RULE['from']``)
MAILBOX_PROPERTY = "mailbox"   # its mailbox owner's object id (schema: documents.json)
MENTION_PREFIX = "mentions_"
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

Call = Callable[[str, Mapping[str, Any], Any], Mapping[str, Any]]


@dataclass(frozen=True)
class Identity:
    """Who the reader is in the sources: confirmed addresses and Entra object ids."""
    addresses: frozenset[str] = frozenset()
    object_ids: frozenset[str] = frozenset()
    basis: str = "employee"

    @classmethod
    def of(cls, entitlement: Any) -> "Identity":
        """From an ``EmployeeEntitlement``: its confirmed source identities (the Portal
        row's address, the Entra mail and UPN, the object id); groups are no identity."""
        if entitlement is None or getattr(entitlement, "actor", None) is None:
            return cls(basis="not_an_employee")
        values = {str(v).strip().lower() for v in
                  getattr(entitlement, "source_identities", ()) or () if isinstance(v, str)}
        principal = getattr(entitlement, "principal", None)
        oid = str(getattr(principal, "object_id", "") or "").strip().lower()
        if oid:
            values.add(oid)
        return cls(frozenset(v for v in values if _ADDRESS.match(v)),
                   frozenset(v for v in values if _GUID.match(v)))

    @property
    def known(self) -> bool:
        return bool(self.addresses or self.object_ids)

    def names(self, value: Any) -> bool:
        """Whether a property value names this reader exactly (a whole value, or one
        element of a JSON list of them)."""
        mine = self.addresses | self.object_ids
        if isinstance(value, Mapping) and "untrusted_text" in value:
            value = value["untrusted_text"]
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                try:
                    value = json.loads(text)
                except ValueError:
                    return text.lower() in mine
            else:
                return text.lower() in mine
        if isinstance(value, (list, tuple)):
            return any(isinstance(v, str) and v.strip().lower() in mine for v in value)
        return False


@dataclass
class Involvement:
    """Per record id, why it is the reader's (each reason: ``{"reason", "via"}``)."""
    by_object: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    checked: int = 0
    not_checked: int = 0
    basis: str = "employee"

    @property
    def complete(self) -> bool:
        return not self.not_checked

    def out(self) -> dict[str, Any]:
        return {"basis": self.basis, "checked": self.checked,
                "not_checked": self.not_checked, "complete": self.complete}


def _type_of(object_id: str) -> str:
    return object_id.split(":", 1)[0]


class _Paths:
    """The schema's paths from a record type to the reader's mail."""

    def __init__(self, schema: Any) -> None:
        links = dict(getattr(schema, "link_types", None) or {})
        props = {p.get("name") for p in ((getattr(schema, "object_types", None) or {})
                                         .get(MAIL_FROM, {}).get("properties") or ())}
        self.mailbox = MAILBOX_PROPERTY in props
        self.mentions: dict[str, str] = {}      # target type -> mention link id
        self.sent_by: dict[str, str] = {}       # sender type -> message link id
        for lid, link in sorted(links.items()):
            if link.get("from") != MAIL_FROM or not isinstance(link.get("to"), str):
                continue
            name = str(link.get("name") or lid.split(".", 1)[-1])
            (self.mentions if name.startswith(MENTION_PREFIX) else self.sent_by
             ).setdefault(link["to"], lid)
        self.counterparts: dict[str, list[str]] = {}   # record type -> links to a sender
        for lid, link in sorted(links.items()):
            if link.get("from") != MAIL_FROM and link.get("to") in self.sent_by \
                    and link.get("from") != link.get("to"):
                self.counterparts.setdefault(str(link.get("from")), []).append(lid)


def derive(call: Call, actor: Any, schema: Any, identity: Identity,
           object_ids: Sequence[str], *, max_objects: int = MAX_OBJECTS,
           budget_seconds: float = BUDGET_SECONDS,
           clock: Callable[[], float] = time.monotonic) -> Involvement:
    """Which of ``object_ids`` (the briefing's records, in its ranked order) are the
    reader's, read as ``actor`` through ``call``. Unknown identity: nothing is theirs."""
    seen: list[str] = []
    for oid in object_ids:
        if isinstance(oid, str) and ":" in oid and oid not in seen:
            seen.append(oid)
    if not identity.known:
        return Involvement(basis=identity.basis if identity.basis != "employee"
                           else "no_identity_on_record")
    paths = _Paths(schema)
    found = Involvement()
    start = clock()
    mailboxes = sorted(identity.object_ids) if paths.mailbox else []
    corresponds: dict[str, str | None] = {}   # counterpart id -> a message id, or None

    def over() -> bool:
        return clock() - start >= budget_seconds

    def mail_around(root: str, link: str) -> str | None:
        """A message in the reader's own mailbox one reverse hop from ``root``."""
        for box in mailboxes:
            out = call("plx.around", {"from": root, "limit": 1, "path": [
                {"link_type": link, "direction": "reverse"}],
                "filters": [{"property": MAILBOX_PROPERTY, "op": "eq", "value": box}]},
                actor)
            if not isinstance(out, Mapping) or "error" in out:
                continue
            for item in out.get("objects") or ():
                oid = item.get("object_id") if isinstance(item, Mapping) else None
                if isinstance(oid, str) and oid != root:
                    return oid
        return None

    for n, oid in enumerate(seen):
        if n >= max_objects or over():
            found.not_checked = len(seen) - n
            break
        reasons: list[dict[str, str]] = []
        record = call("plx.object", {"object_id": oid}, actor)
        if isinstance(record, Mapping) and "error" not in record:
            for value in record.get("values") or ():
                if isinstance(value, Mapping) and identity.names(value.get("value")):
                    reasons.append({"reason": "named", "via": str(value.get("property"))})
        kind = _type_of(oid)
        if mailboxes and kind in paths.mentions:
            message = mail_around(oid, paths.mentions[kind])
            if message:
                reasons.append({"reason": "your_mail", "via": message})
        if mailboxes:
            for link in paths.counterparts.get(kind, ()):
                out = call("plx.around", {"from": oid, "limit": 5, "path": [
                    {"link_type": link, "direction": "forward"}]}, actor)
                if not isinstance(out, Mapping) or "error" in out:
                    continue
                for item in out.get("objects") or ():
                    other = item.get("object_id") if isinstance(item, Mapping) else None
                    sender = paths.sent_by.get(_type_of(other)) if isinstance(other, str) \
                        else None
                    if sender is None:
                        continue
                    if other not in corresponds:
                        corresponds[other] = mail_around(other, sender)
                    if corresponds[other]:
                        reasons.append({"reason": "you_correspond", "via": other})
        found.checked += 1
        if reasons:
            found.by_object[oid] = reasons
    return found


def related_ids(gap: Mapping[str, Any]) -> list[str]:
    """A briefing item's record and the records its event joins (a late PO's customer
    orders, an order's late POs): the item is the reader's when any of them is."""
    out = [gap.get("object_id")]
    event = gap.get("event") if isinstance(gap.get("event"), Mapping) else {}
    for key in ("pos", "orders"):
        value = event.get(key)
        if isinstance(value, (list, tuple)):
            out.extend(value)
    return [v for v in out if isinstance(v, str)]


def gather(ids: Iterable[str]) -> list[str]:
    out: list[str] = []
    for oid in ids:
        if oid not in out:
            out.append(oid)
    return out
