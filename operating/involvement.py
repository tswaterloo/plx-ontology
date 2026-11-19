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
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from .call_limits import facts_cut

MAX_OBJECTS = 120        # records checked per briefing (in its ranked order)
BUDGET_SECONDS = 4.0     # time for every check of one briefing
# An answer that says the store could not be read (a fact read cut at the budget, a
# store gone, a call cut at its limit): the record is not checked, never "not yours".
UNREAD_CODES = frozenset({"store_unavailable", "tool_timed_out", "rebuild_required"})
MAIL_FROM = "Email"      # the message type (``ontology.schema.MENTION_LINK_RULE['from']``)
MAILBOX_PROPERTY = "mailbox"   # its mailbox owner's object id (schema: documents.json)
MENTION_PREFIX = "mentions_"
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

Call = Callable[[str, Mapping[str, Any], Any], Mapping[str, Any]]
# The reader's batched value read (``ObjectWorkspace.object_words``): of the ids, those the
# reader may read now as ``plx.object`` decides a record, each one's readable values of the
# properties named ({"values": {property: [value, ...]}, "withheld": {property, ...}}); a
# record not visible is absent; any failure raises.
Words = Callable[[Sequence[str], Sequence[str]], Mapping[str, Mapping[str, Any]]]
# A set handle of the ids for the reader (``ObjectWorkspace.members``): ``plx.around``
# walks it re-checking every member's visibility, as for any set.
Members = Callable[[Sequence[str]], str]
WALK_PAGE = 50           # objects per page of a batched walk (plx.around's limit)
WALK_ROOTS = 1000        # records per walked set (a set holds at most 5,000: never cut)
# The value types a person's address or object id can be held in (``Identity.names``
# matches a whole string or one string of a JSON list); a typed identifier
# (``identifier_kind``) names a record, never a person.
NAMING_VALUE_TYPES = frozenset({"text", "json"})


def naming_properties(schema: Any, object_type: str) -> frozenset[str] | None:
    """The properties of ``object_type`` that can name a person (``NAMING_VALUE_TYPES``,
    no ``identifier_kind``), from the schema; None when the schema does not declare the
    type (its record is then read whole, never assumed to name no one)."""
    spec = (getattr(schema, "object_types", None) or {}).get(object_type)
    if not isinstance(spec, Mapping):
        return None
    return frozenset(
        str(p["name"]) for p in spec.get("properties") or ()
        if isinstance(p, Mapping) and isinstance(p.get("name"), str)
        and p.get("value_type") in NAMING_VALUE_TYPES and not p.get("identifier_kind"))


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


class Known:
    """Each record's reasons as derived for one reader on one data stamp (``derive``'s
    ``known``), kept per key (the caller's: the reader's principals, policy, people
    exclusion, identity and the store generation and delta layers) for ``KNOWN_SECONDS``.
    Only a record read whole is kept (an unread one is checked again); another key never
    sees it. The briefing is read by several consumers (Home, its "+N more" groups,
    suggestions) on one request each: the records they share are checked once."""

    def __init__(self, most: int = 64, seconds: float = 15 * 60,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.most, self.seconds, self.clock = most, seconds, clock
        self.lock = threading.Lock()
        self.kept: dict[tuple, tuple[float, dict[str, list[dict[str, str]]]]] = {}

    def of(self, key: tuple | None) -> dict[str, list[dict[str, str]]] | None:
        """The reasons kept under ``key`` (a dict ``derive`` reads and fills), None
        without a key (nothing kept)."""
        try:
            hash(key)
        except TypeError:   # an unkeyable reader keeps nothing: every record is read
            return None
        if key is None:
            return None
        with self.lock:
            got = self.kept.get(key)
            if got is None or self.clock() - got[0] > self.seconds:
                got = (self.clock(), {})
                self.kept[key] = got
                while len(self.kept) > self.most:
                    self.kept.pop(next(iter(self.kept)))
            return got[1]

    def clear(self) -> None:
        with self.lock:
            self.kept.clear()


KNOWN = Known()


def derive(call: Call, actor: Any, schema: Any, identity: Identity,
           object_ids: Sequence[str], *, max_objects: int = MAX_OBJECTS,
           budget_seconds: float = BUDGET_SECONDS,
           clock: Callable[[], float] = time.monotonic,
           known: dict[str, list[dict[str, str]]] | None = None,
           words: Words | None = None,
           members: Members | None = None) -> Involvement:
    """Which of ``object_ids`` (the briefing's records, in its ranked order) are the
    reader's, read as ``actor`` through ``call``. Unknown identity: nothing is theirs.
    ``known``: each record's reasons already read for this same reader and data stamp
    (``Known.of``), used instead of reading it again and filled with each record read
    whole here. ``words``: the reader's batched value read; with it the ``named`` check of
    every record to be read is one read of the properties that can name a person
    (``naming_properties``) instead of a whole ``plx.object`` per record (each with its
    identity edges, linked records and every citation gated); a record of a type the
    schema does not declare, or every record when that read fails, is read whole.
    ``members``: a set of the records for the reader; with it each mail path (a mention
    in the reader's mailbox, a counterpart, a counterpart's message to the reader) is
    walked once from the set of every record to be read, paged, instead of once per
    record; a record a walk did not settle (cut, an edge cap, an unread answer, the
    budget) is walked alone as before."""
    seen: list[str] = []
    for oid in object_ids:
        if isinstance(oid, str) and ":" in oid and oid not in seen:
            seen.append(oid)
    if not identity.known:
        return Involvement(basis=identity.basis if identity.basis != "employee"
                           else "no_identity_on_record")
    paths = _Paths(schema)
    found = Involvement()
    known_before = frozenset(known or ())
    start = clock()
    mailboxes = sorted(identity.object_ids) if paths.mailbox else []
    corresponds: dict[str, str | None] = {}   # counterpart id -> a message id, or None

    unread: list[bool] = [False]   # a check of the record under way could not be read
    spent: dict[str, list[float]] = {}   # per tool: [calls, seconds] (stage timing log)

    def over() -> bool:
        return clock() - start >= budget_seconds

    def asked(name: str, arguments: Mapping[str, Any]) -> Any:
        """``call``, a failure noted: a raise, or a store that could not be read (a fact
        read cut at the budget, ``call_limits.facts_cut``) leaves the record not
        checked, never "not yours"."""
        began = time.monotonic()
        try:
            out = call(name, arguments, actor)
        except Exception:  # noqa: BLE001 - counted as not checked
            unread[0] = True
            return None
        finally:
            tally = spent.setdefault(name, [0, 0.0])
            tally[0] += 1
            tally[1] += time.monotonic() - began
        error = out.get("error") if isinstance(out, Mapping) else None
        if not isinstance(out, Mapping) or (
                isinstance(error, Mapping) and error.get("code") in UNREAD_CODES):
            unread[0] = True
        return out

    def mail_around(root: str, link: str) -> str | None:
        """A message in the reader's own mailbox one reverse hop from ``root``."""
        for box in mailboxes:
            out = asked("plx.around", {"from": root, "limit": 1, "path": [
                {"link_type": link, "direction": "reverse"}],
                "filters": [{"property": MAILBOX_PROPERTY, "op": "eq", "value": box}]})
            if not isinstance(out, Mapping) or "error" in out:
                continue
            for item in out.get("objects") or ():
                oid = item.get("object_id") if isinstance(item, Mapping) else None
                if isinstance(oid, str) and oid != root:
                    return oid
        return None

    named: dict[str, list[dict[str, str]]] = {}   # the batched read's, per record id

    def named_in_batch() -> None:
        """``named`` of every record to be read whose type the schema declares, in one
        read through ``words`` (a record not visible names no one, as ``plx.object``'s
        not-found answer). A property the reader holds a value of it may not read is
        left out whole (``plx.object`` withholds it). A failed read fills nothing: each
        record is then read whole."""
        todo: dict[str, frozenset[str]] = {}
        for oid in seen[:max_objects]:
            if known is not None and oid in known:
                continue
            props = naming_properties(schema, _type_of(oid))
            if props is not None:
                todo[oid] = props
        if not todo:
            return
        wanted = sorted(set().union(*todo.values()))
        began = time.monotonic()
        try:
            got = words(list(todo), wanted) if wanted else {}
            if not isinstance(got, Mapping):
                return
        except Exception:  # noqa: BLE001 - each record is read whole instead
            return
        finally:
            tally = spent.setdefault("values", [0, 0.0])
            tally[0] += 1
            tally[1] += time.monotonic() - began
        for oid, props in todo.items():
            record = got.get(oid)
            reasons: list[dict[str, str]] = []
            if isinstance(record, Mapping):
                values = record.get("values")
                values = values if isinstance(values, Mapping) else {}
                withheld = set(record.get("withheld") or ())
                for prop in sorted(props - withheld):
                    held = values.get(prop)
                    if isinstance(held, (list, tuple)) and any(
                            identity.names(v) for v in held):
                        reasons.append({"reason": "named", "via": prop})
            named[oid] = reasons

    mentioned: dict[str, str | None] = {}         # record -> a message of the reader's
    linked: dict[tuple[str, str], list[str]] = {}  # (record, link) -> its counterparts

    def walk(roots: Sequence[str], link: str, direction: str, box: str | None
             ) -> dict[str, list[str]]:
        """``walk_set`` over ``roots`` in sets of at most ``WALK_ROOTS``."""
        out: dict[str, list[str]] = {}
        for at in range(0, len(roots), WALK_ROOTS):
            out.update(walk_set(roots[at:at + WALK_ROOTS], link, direction, box))
        return out

    def walk_set(roots: Sequence[str], link: str, direction: str, box: str | None
                 ) -> dict[str, list[str]]:
        """One hop over ``link`` from the set of ``roots`` (a ``box``: only messages in
        that mailbox), paged: each root settled -> the objects it reaches (every
        reaching edge of each listed object is shown); a root with nothing settled only
        when the walk listed everything (no next page, no edge cap). An unread answer, a
        failure or the budget settles only the roots something was found for."""
        reached: dict[str, list[str]] = {r: [] for r in roots}
        if not roots or members is None:
            return {}
        try:
            handle = members(list(roots))
        except Exception:  # noqa: BLE001 - each record walked alone
            return {}
        if not isinstance(handle, str):
            return {}
        arguments: dict[str, Any] = {"from": handle, "limit": WALK_PAGE, "path": [
            {"link_type": link, "direction": direction}]}
        if box is not None:
            arguments["filters"] = [{"property": MAILBOX_PROPERTY, "op": "eq", "value": box}]
        whole = False
        while not over():
            before = unread[0]
            out = asked("plx.around", arguments)
            failed = unread[0]
            unread[0] = before
            if failed or not isinstance(out, Mapping) or "error" in out:
                break
            for edge in out.get("edges") or ():
                if not isinstance(edge, Mapping) or edge.get("link_type") != link:
                    continue
                root, other = (edge.get("to"), edge.get("from")) if direction == "reverse" \
                    else (edge.get("from"), edge.get("to"))
                if root in reached and isinstance(other, str) and other != root \
                        and other not in reached[root]:
                    reached[root].append(other)
            capped = any(isinstance(g, Mapping) and g.get("code") == "edge_cap"
                         for g in out.get("gaps") or ())
            following = (out.get("completeness") or {}).get("next_cursor")
            if capped:
                break
            if not following:
                whole = True
                break
            arguments = dict(arguments, cursor=following)
        return reached if whole else {r: o for r, o in reached.items() if o}

    def paths_in_batch() -> None:
        """Each mail path walked once for every record to be read (``walk``); what a
        walk settles is used by ``check``, the rest is walked per record."""
        todo = [oid for oid in seen[:max_objects]
                if not (known is not None and oid in known)]
        by_type: dict[str, list[str]] = {}
        for oid in todo:
            by_type.setdefault(_type_of(oid), []).append(oid)
        for kind, roots in sorted(by_type.items()):
            link = paths.mentions.get(kind)
            if link is not None:
                left = list(roots)
                for box in mailboxes:
                    got = walk(left, link, "reverse", box)
                    for root, others in got.items():
                        if others:
                            mentioned[root] = others[0]
                    left = [r for r in left if r not in mentioned]
                    if any(r not in got for r in left):
                        break   # not settled in this mailbox: read alone
                else:
                    for root in left:
                        mentioned[root] = None
            for link in paths.counterparts.get(kind, ()):
                for root, others in walk(roots, link, "forward", None).items():
                    linked[(root, link)] = others
        senders: dict[str, list[str]] = {}
        for others in linked.values():
            for other in others:
                sender = paths.sent_by.get(_type_of(other))
                if sender is not None and other not in corresponds:
                    senders.setdefault(sender, [])
                    if other not in senders[sender]:
                        senders[sender].append(other)
        for sender, others in sorted(senders.items()):
            left = list(others)
            for box in mailboxes:
                got = walk(left, sender, "reverse", box)
                for other, messages in got.items():
                    if messages:
                        corresponds[other] = messages[0]
                left = [o for o in left if o not in corresponds]
                if any(o not in got for o in left):
                    break
            else:
                for other in left:
                    corresponds[other] = None

    def check(oid: str) -> list[dict[str, str]]:
        if oid in named:
            reasons = [dict(r) for r in named[oid]]
        else:
            reasons = []
            record = asked("plx.object", {"object_id": oid})
            if isinstance(record, Mapping) and "error" not in record:
                for value in record.get("values") or ():
                    if isinstance(value, Mapping) and identity.names(value.get("value")):
                        reasons.append({"reason": "named",
                                        "via": str(value.get("property"))})
        kind = _type_of(oid)
        if mailboxes and kind in paths.mentions:
            message = mentioned[oid] if oid in mentioned else \
                mail_around(oid, paths.mentions[kind])
            if message:
                reasons.append({"reason": "your_mail", "via": message})
        if mailboxes:
            for link in paths.counterparts.get(kind, ()):
                if (oid, link) in linked:
                    reach: Iterable[Any] = linked[(oid, link)]
                else:
                    out = asked("plx.around", {"from": oid, "limit": 5, "path": [
                        {"link_type": link, "direction": "forward"}]})
                    if not isinstance(out, Mapping) or "error" in out:
                        continue
                    reach = [item.get("object_id") for item in out.get("objects") or ()
                             if isinstance(item, Mapping)]
                for other in reach:
                    sender = paths.sent_by.get(_type_of(other)) if isinstance(other, str) \
                        else None
                    if sender is None:
                        continue
                    if other not in corresponds:
                        before = unread[0]
                        unread[0] = False
                        message = mail_around(other, sender)
                        if not unread[0]:   # an unread answer is asked again, not kept
                            corresponds[other] = message
                        unread[0] = unread[0] or before
                    if corresponds.get(other):
                        reasons.append({"reason": "you_correspond", "via": other})
        return reasons

    with facts_cut(time.monotonic() + max(0.0, budget_seconds - (clock() - start))):
        if words is not None:
            named_in_batch()
        if members is not None and mailboxes:
            paths_in_batch()
        for n, oid in enumerate(seen):
            if n >= max_objects or over():
                found.not_checked += len(seen) - n
                break
            unread[0] = False
            if known is not None and oid in known:
                reasons = [dict(r) for r in known[oid]]
            else:
                reasons = check(oid)
                if known is not None and not unread[0]:
                    known[oid] = [dict(r) for r in reasons]
            if reasons:
                found.by_object[oid] = reasons   # a reason read is the reader's
            if unread[0] and not reasons:
                found.not_checked += 1
            else:
                found.checked += 1
    if os.environ.get("PLX_STAGE_TIMING") == "1":
        logging.getLogger("operator_api.stages").info(
            "involvement derive %d records (%d reused, %d checked) in %.2f s; %s", len(seen),
            sum(1 for o in seen[:max_objects] if known is not None and o in known_before),
            found.checked, clock() - start,
            ", ".join(f"{name} {n}x {s:.2f} s" for name, (n, s) in sorted(spent.items())))
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
