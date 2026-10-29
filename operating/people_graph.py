"""People and communications graph: who works on a record, and what we last said to a party.

The engines of the ``plx.investigate`` plans ``ontology/plans/people_on_record.json`` (family
F18: "who is working on PO X / supplier Y", "who should I ask about item Z") and
``ontology/plans/party_last_said.json`` (F19: "what did we last say to supplier Y, and who said
it"). The plans reach the conversations and records around the root with the generic
primitives (mail and Teams messages, Mission Control tasks, approval gates and task events
that name the root, its twins and, for an item, the orders that carry it) and read them; these
engines turn the cited participants of what the reader was shown into people. They read no
source by themselves beyond re-reading, for the investigating actor, the citations the plan
produced (``resolver``), and they decide nobody's access: whatever a primitive withheld from
the reader (a mailbox, a marking, a Teams conversation) never reaches them, so a person's link
to a conversation the reader may not read is never formed, not even counted.

**Person.** One employee, resolved conservatively:

* A shared key joins observations into one person, and only a shared key: an Entra object id
  (a Teams author's ``from.user.id``, a captured mailbox's id) or a mail address, joined
  through the captured directory (the mail capture's mailbox rows: the mailbox's object id,
  UPN and every SMTP address, ``PeopleDirectory``). Such a person's identity is ``verified``.
* A name alone (an MC field holding a name, a Teams display name against another person's)
  is compared with ``ontology.identity_scoring``'s name levels: ``exact`` or ``close`` makes a
  ``lead`` that two entries may be one person, never a merge. A name that matches no cleared
  person is withheld (below).
* Portal ``app_user`` rows carry no name or address on the served API, and BC/FileMaker user
  fields are codes or free-text names: they share no key with the directory, so they are never
  joined to a person (a lead at most).
* An external address (a domain outside the tenant's) is a counterpart, never a person of
  ours; F19 reads it to match the party.

**People exclusion, fail closed.** Every observed identity passes the people guard
(``integrations.people_exclusion``) before it may name a person: an object id is decided by the
guard; an address through the object ids the captured directory records beside it (and the
capture's own exclusion: ``MailCaptureStore.withheld_mailboxes``, the guard and the
department deny list), else by a guard bound to the tenant directory; an address in a tenant
domain nothing can decide, a name the guard cannot decide, and everything while the guard is
unavailable are withheld. A record naming a withheld identity in any role (a message an
excluded person sent or is addressed or copied on, an approval they decided) is withheld
whole, as the capture drops such a message: it forms no person, no lead and no count, and
every plan entry of that record leaves the answer (``withhold``: the executor drops those
entries), so an excluded person never appears in the answer. A name alone that matches no
cleared person withholds its own entry. Only undecided
identities (never excluded ones) add the gap ``participants_not_cleared``, which names no
count and makes the answer partial (the withheld record may be the latest message or another
role).

**Teams conversations.** A chat the plan reached (the chat of a message naming the record,
``TeamsChatMessage.in_chat``) names its participants (``TeamsChat.participants``, identity
``teams_members``): each is resolved by its Entra object id, the shared key, its display name
read beside it. The chat is gated per conversation unit like its messages, so a reader who
cannot read it never reaches its participants; a chat naming a participant the exclusion
withholds or cannot clear is withheld whole (decision item 4). Channels are not read for
participants: a channel's membership is its team's, often everyone.

**Answer (F18, engine ``people_on_record``).** Up to ``MAX_PEOPLE`` people, ranked by what
they did with the records reached (owner and assignee 4, approver 3, sender, author and actor
2, requester, reporter, recipient and chat participant 1, once per record and role), then by their latest dated
activity, then by label: ``<name>_<n>`` (value: the person's label as a source text envelope
citing where it was read: a Teams display name, else the address or UPN; ``inputs``: the
cited entries each role rests on; ``basis``: the records; ``steps``: how the identity was
resolved, each role on each record by entry name, leads to other people), then ``<name>``
(how many). Steps name entries, roles, records and resolution kinds, never source text.

**Answer (F19, engine ``party_last_said``).** The party's mail addresses are the senders of the
mail a person linked to the party (``party_links``, reviewed ``Email.sent_by_*`` links) and its
website domain (``party_websites``: of the root and the twins identity-tier same-record links
join to it, ``twin_links``, never a twin matched by text or a lead); a shared mail provider's
domain is never the party's. A
message "we said to the party" is a reached mail whose sender is a cleared person of ours and
whose recipients (``row.to``/``row.cc`` re-read from its citation for the actor) hold a party
address or a recipient at a party domain. ``<name>`` is its time (latest first), with
``<name>_by`` (who said it, a person entry as above) and ``<name>_earlier_<n>`` up to
``MAX_EARLIER`` earlier ones; what was said is the plan's cited subject and body entries
(``inputs``). Nothing matched is the gap ``nothing_said_found`` (``unknown``: a withheld
mailbox is the primitives' gap, never proof nothing was said); no party address known is
``party_address_unknown``.

Evidence label of fixture runs: ``isolated-fixture``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology.identity_scoring import _name_level, mail_address, normalize_name
from ontology.mentions import mail_domain, registrable_domain, shared_mail_domain, website_domain

PEOPLE_ENGINE = "people_on_record"
SAID_ENGINE = "party_last_said"
ROLE_WEIGHTS = {"owner": 4, "assignee": 4, "approver": 3, "sender": 2, "author": 2,
                "actor": 2, "requester": 1, "reporter": 1, "recipient": 1, "participant": 1}
IDENTITIES = ("address", "mixed", "teams_party", "teams_members")
LEAD_LEVELS = ("exact", "close")
MAX_PEOPLE = 20
MAX_BASIS = 25   # records one person entry names; the steps count them all
MAX_EARLIER = 4
MAX_STEP_CHARS = 400
AGENT_PREFIX = "agent:"
_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_REREAD = re.compile(r"^row(?:\.[A-Za-z_][A-Za-z0-9_]*){1,3}$")
_LINK_TYPE = re.compile(r"^[A-Z][A-Za-z0-9]{1,63}\.[a-z][a-z0-9_]{0,63}$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CHECK_FAILED = "failed"

# Resolution states of one observed identity.
PERSON, EXTERNAL, WITHHELD, UNDECIDED, AGENT = (
    "person", "external", "withheld", "undecided", "agent")

Resolver = Callable[[str, Any], Any]


# --------------------------------------------------------------------------- directory

@dataclass(frozen=True)
class Resolution:
    """What one identity is: ``state`` (``PERSON``, ``EXTERNAL``, ``WITHHELD``,
    ``UNDECIDED``), the person ``key`` (``mailbox:<oid>``, ``oid:<oid>``,
    ``address:<address>``), how it was ``verified`` and the person's directory addresses."""

    state: str
    key: str | None = None
    verified: str | None = None
    addresses: frozenset[str] = frozenset()
    upn: str | None = None


_WITHHELD = Resolution(WITHHELD)
_UNDECIDED = Resolution(UNDECIDED)


def _lower(value: Any) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


class PeopleDirectory:
    """The captured directory under the people exclusion (module doc).

    ``guard``: the host's ``PeopleExclusion``. ``mailboxes``: the captured mailbox rows
    (``mailbox_id``, ``upn``, ``identities``: the JSON list or a list). ``withheld``: the
    mailbox ids the capture's own exclusion withholds now (None: every one, the capture could
    not decide). ``internal_domains``: the tenant's mail domains beside those of the captured
    mailboxes. ``address_guard``: a zero-argument callable returning a guard bound to the
    tenant directory (or None), asked only for a tenant address no mailbox holds."""

    def __init__(self, guard: Any, mailboxes: Iterable[Mapping[str, Any]] = (), *,
                 withheld: Iterable[str] | None = (), internal_domains: Iterable[str] = (),
                 address_guard: Callable[[], Any] | None = None) -> None:
        self.guard = guard
        self._address_guard = address_guard
        self._bound: Any = None
        self._asked = False
        self._withheld_all = withheld is None
        withheld_ids = {_lower(m) for m in withheld or ()} - {None}
        self._boxes: dict[str, dict[str, Any]] = {}
        self._by_identity: dict[str, str] = {}
        domains = {_lower(d) for d in internal_domains} - {None}
        for row in mailboxes:
            box = _lower(row.get("mailbox_id"))
            if box is None:
                continue
            identities = row.get("identities")
            if isinstance(identities, str):
                try:
                    identities = json.loads(identities)
                except ValueError:
                    identities = []
            values = {v for v in (_lower(i) for i in [box, row.get("upn"),
                                                      *(identities or [])]) if v}
            addresses = frozenset(v for v in values if v.count("@") == 1)
            domains |= {a.rsplit("@", 1)[1] for a in addresses}
            self._boxes[box] = {"withheld": box in withheld_ids, "values": values,
                                "addresses": addresses, "upn": _lower(row.get("upn")),
                                "oids": frozenset(v for v in values if _GUID.match(v))}
            for value in values:
                # An identity two mailboxes record is no one's key: both are withheld.
                self._by_identity[value] = box if value not in self._by_identity else ""
        self.domains = frozenset(domains)

    @classmethod
    def from_runtime(cls, runtime: Any) -> "PeopleDirectory":
        """The directory of a case runtime: its people guard, its mail capture's mailboxes
        (read-only; none when the host holds no capture) and its mail scope's internal
        domains, with the runtime's bound address guard (``CaseRuntime.address_exclusion``)."""
        from integrations.capture_mail import MailCaptureStore, store_exists
        guard = getattr(runtime, "people_exclusion", None)
        rows: list[dict[str, Any]] = []
        withheld: list[str] | None = []
        directory = getattr(runtime, "mail_capture_dir", None)
        if directory is not None and store_exists(directory):
            store = MailCaptureStore(directory, people=guard)
            try:
                withheld = store.withheld_mailboxes(guard)
                rows = [dict(r) for r in store.db.execute(
                    "SELECT mailbox_id, upn, identities FROM mailboxes ORDER BY mailbox_id")]
            finally:
                store.db.close()   # a reader: no WAL checkpoint (``MailCaptureStore.close``)
        scope = getattr(runtime, "mail_scope", None)
        bound = getattr(runtime, "address_exclusion", None)
        return cls(guard, rows, withheld=withheld,
                   internal_domains=getattr(scope, "internal_domains", ()) or (),
                   address_guard=bound if callable(bound) else None)

    @property
    def unavailable(self) -> str | None:
        """Why the guard cannot decide anything (``PeopleExclusion.unavailable``), or None."""
        from integrations.people_exclusion import PeopleExclusion
        if not isinstance(self.guard, PeopleExclusion):
            return "excluded_people_oids_absent"
        return self.guard.unavailable

    def _excluded(self, guard: Any, values: Iterable[str]) -> bool | None:
        from integrations.people_exclusion import ExclusionUnavailable
        try:
            return bool(guard.excluded(values))
        except ExclusionUnavailable:
            return None

    def _bound_guard(self) -> Any:
        if self.guard is not None and getattr(self.guard, "bound", False):
            return self.guard
        if not self._asked:
            self._asked = True
            try:
                found = self._address_guard() if self._address_guard is not None else None
            except Exception:  # noqa: BLE001 - no bound guard: addresses stay undecided
                found = None
            self._bound = found if getattr(found, "bound", False) and not getattr(
                found, "unavailable", None) else None
        return self._bound

    def _mailbox(self, box: str) -> Resolution:
        entry = self._boxes[box]
        if self._withheld_all or entry["withheld"]:
            return _WITHHELD
        excluded = self._excluded(self.guard, entry["oids"])
        if excluded is None or excluded:
            return _WITHHELD if excluded else _UNDECIDED
        bound = self._bound_guard()
        if bound is not None and self._excluded(bound, entry["addresses"]) is not False:
            return _WITHHELD
        return Resolution(PERSON, f"mailbox:{box}", "directory_mailbox", entry["addresses"],
                          entry["upn"])

    def address(self, value: Any) -> Resolution:
        """One mail address (module doc). An address two mailboxes record is withheld."""
        if self.unavailable:
            return _WITHHELD
        address = mail_address(value) if isinstance(value, str) else None
        if address is None:
            return _UNDECIDED
        box = self._by_identity.get(address)
        if box == "":
            return _UNDECIDED
        if box:
            return self._mailbox(box)
        domain = address.rsplit("@", 1)[1]
        if domain not in self.domains:
            return Resolution(EXTERNAL)
        bound = self._bound_guard()
        if bound is None:
            return _UNDECIDED
        excluded = self._excluded(bound, [address])
        if excluded is None:
            return _UNDECIDED
        return _WITHHELD if excluded else Resolution(
            PERSON, f"address:{address}", "tenant_address", frozenset({address}))

    def object_id(self, value: Any) -> Resolution:
        """One Entra object id: decided by the guard, then its captured mailbox if any."""
        if self.unavailable:
            return _WITHHELD
        oid = _lower(value)
        if oid is None or not _GUID.match(oid):
            return _UNDECIDED
        excluded = self._excluded(self.guard, [oid])
        if excluded is None or excluded:
            return _WITHHELD if excluded else _UNDECIDED
        box = self._by_identity.get(oid)
        if box == "":
            return _UNDECIDED
        if box:
            return self._mailbox(box)
        return Resolution(PERSON, f"oid:{oid}", "object_id")


def directory_of(runtime: Any) -> Callable[[], PeopleDirectory | None] | None:
    """The people directory provider a host gives ``plx.investigate`` (read anew per call,
    so a changed guard or capture applies at once); None without a runtime."""
    if runtime is None:
        return None

    def directory() -> PeopleDirectory | None:
        try:
            return PeopleDirectory.from_runtime(runtime)
        except Exception:  # noqa: BLE001 - no directory: the engines withhold every person
            return None
    return directory


# --------------------------------------------------------------------------- plan checks

def _names(value: Any) -> list[str] | None:
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return value
    return None


def _item_problems(where: str, names: Any, items: Mapping[str, Mapping[str, Any]],
                   types: set[str]) -> list[str]:
    listed = _names(names)
    if listed is None:
        return [f"{where} must list evidence names"]
    out = []
    for name in listed:
        item = items.get(name)
        if item is None or item["kind"] != "property":
            out.append(f"{where}: {name} is not a property evidence item listed before it")
        else:
            types.add(item["object_type"])
    return out


def _people_problems(label: str, specs: Any, items: Mapping[str, Mapping[str, Any]],
                     types: set[str]) -> list[str]:
    if not isinstance(specs, list) or not 1 <= len(specs) <= 6:
        return [f"{label}: people must list 1-6 roles"]
    out = []
    for spec in specs:
        if not isinstance(spec, Mapping) or spec.get("role") not in ROLE_WEIGHTS:
            out.append(f"{label}: every people entry needs a role of {sorted(ROLE_WEIGHTS)}")
            continue
        role = spec["role"]
        out += [f"{label}.{role}: unknown key {k}"
                for k in set(spec) - {"role", "items", "reread", "identity"}]
        if spec.get("identity") not in IDENTITIES:
            out.append(f"{label}.{role}: identity must be one of {list(IDENTITIES)}")
        if ("items" in spec) == ("reread" in spec):
            out.append(f"{label}.{role}: give items or reread, not both")
        elif "items" in spec:
            out += _item_problems(f"{label}.{role} items", spec["items"], items, types)
        elif not (_names(spec["reread"]) and all(_REREAD.match(p) for p in spec["reread"])):
            out.append(f"{label}.{role}: reread must list row field paths")
    return out


def people_check(args: Mapping[str, Any], doc: Mapping[str, Any], index: int,
                 schema: Any) -> list[str]:
    """Problems with ``people_on_record`` ``engine_args``: ``records``, 1-8 kinds of
    ``{label, time, people: [{role, items | reread, identity}]}``, each naming property items
    listed before the compute item, all of one object type per kind."""
    if not isinstance(args, Mapping):
        return ["engine_args must be an object"]
    out = [f"unknown engine argument {k}" for k in set(args) - {"records"}]
    records = args.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= 8:
        return out + ["records must list 1-8 record kinds"]
    items = {e["name"]: e for e in doc["evidence"][:index]}
    labels: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping) or not _LABEL.match(str(record.get("label"))):
            out.append("every record needs a label (lower_snake)")
            continue
        label = record["label"]
        if label in labels:
            out.append(f"{label}: duplicate label")
        labels.add(label)
        out += [f"{label}: unknown key {k}" for k in set(record) - {"label", "time", "people"}]
        types: set[str] = set()
        out += _item_problems(f"{label} time", record.get("time"), items, types)
        out += _people_problems(label, record.get("people"), items, types)
        if len(types) > 1:
            out.append(f"{label}: items name several object types {sorted(types)}")
    return out


def said_check(args: Mapping[str, Any], doc: Mapping[str, Any], index: int,
               schema: Any) -> list[str]:
    """Problems with ``party_last_said`` ``engine_args``: ``messages`` ``{time, sender, said,
    recipients}`` (property items of one object type; ``recipients`` row field paths),
    optional ``party_websites`` (property items) and ``party_links`` (declared link types)."""
    if not isinstance(args, Mapping):
        return ["engine_args must be an object"]
    out = [f"unknown engine argument {k}"
           for k in set(args) - {"messages", "party_websites", "party_links", "twin_links"}]
    items = {e["name"]: e for e in doc["evidence"][:index]}
    messages = args.get("messages")
    if not isinstance(messages, Mapping):
        return out + ["messages must be an object"]
    out += [f"messages: unknown key {k}"
            for k in set(messages) - {"time", "sender", "said", "recipients"}]
    types: set[str] = set()
    for role in ("time", "sender", "said"):
        out += _item_problems(f"messages {role}", messages.get(role), items, types)
    if len(types) > 1:
        out.append(f"messages: items name several object types {sorted(types)}")
    recipients = messages.get("recipients")
    if not (_names(recipients) and all(_REREAD.match(p) for p in recipients)):
        out.append("messages recipients must list row field paths")
    if "party_websites" in args:
        out += _item_problems("party_websites", args["party_websites"], items, set())
    declared = getattr(schema, "link_types", None) or {}
    for key in ("party_links", "twin_links"):
        links = args.get(key, [])
        if not isinstance(links, list) or not all(
                isinstance(x, str) and _LINK_TYPE.match(x) for x in links):
            out.append(f"{key} must list link types")
        elif declared:
            out += [f"{key}: {x} is not a declared link type" for x in links
                    if x not in declared]
    return out


# --------------------------------------------------------------------------- run helpers

def _text(value: Any) -> Any:
    if isinstance(value, Mapping) and "untrusted_text" in value:
        return value.get("untrusted_text")
    return value


def _instant(text: Any) -> dt.datetime | None:
    text = _text(text)
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    try:
        moment = dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _reread(resolver: Resolver | None, actor: Any, handle: Any, path: str) -> Any:
    """The cited field re-read for the actor, None when unreadable or not carried."""
    from operating.cases import _field
    if resolver is None or not isinstance(handle, str):
        return None
    try:
        view = resolver(handle, actor)
        return _field(view, path) if isinstance(view, Mapping) else None
    except Exception:  # noqa: BLE001 - unreadable for this reader: fails closed
        return None


def _envelope(text: str, handle: str, field_path: str) -> dict[str, Any]:
    return {"untrusted_text": text[:300], "origin": {"citation": handle,
                                                     "field_path": field_path}}


def _joined(prefix: str, names: Sequence[str]) -> str:
    """``prefix`` and as many whole names as fit one step, then how many more."""
    out, room = [], MAX_STEP_CHARS - len(prefix) - 12
    for index, name in enumerate(names):
        if len(", ".join([*out, name])) > room:
            return prefix + ", ".join(out) + f" +{len(names) - index} more"
        out.append(name)
    return prefix + (", ".join(out) or "none")


def _digest(entry: dict[str, Any]) -> dict[str, Any]:
    entry["digest"] = hashlib.sha256(json.dumps(
        {k: entry.get(k) for k in ("name", "value", "inputs", "steps", "basis")},
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return entry


@dataclass
class _Seen:
    """One observed identity on one record."""
    role: str
    record: str
    label: str
    cites: tuple[str, ...]          # the answer entries it rests on
    handle: str
    field_path: str
    when: dt.datetime | None


@dataclass
class _Person:
    key: str
    verified: str
    addresses: set[str] = field(default_factory=set)
    upn: str | None = None
    seen: list[_Seen] = field(default_factory=list)
    names: list[tuple[str, str, str]] = field(default_factory=list)  # (name, handle, field)
    name_leads: list[tuple[_Seen, str]] = field(default_factory=list)  # (name seen, level)
    peer_leads: dict[str, str] = field(default_factory=dict)          # other key -> level

    def score(self) -> int:
        return sum(ROLE_WEIGHTS[role] for role, _ in {(s.role, s.record) for s in self.seen})

    def latest(self) -> dt.datetime | None:
        times = [s.when for s in self.seen if s.when is not None]
        return max(times) if times else None

    def label(self) -> tuple[str, str, str]:
        """(label text, handle, field) of where the label was read: a display name read
        beside the identity, else the identity read (the UPN or address when it was one)."""
        if self.names:
            return self.names[0]
        first = self.seen[0]
        address = self.upn or (min(self.addresses) if self.addresses else None)
        return (address or "person (object id only)"), first.handle, first.field_path


class _Run:
    def __init__(self, evidence: Mapping[str, Sequence[Mapping[str, Any]]],
                 resolver: Resolver | None, actor: Any, directory: Any) -> None:
        self.evidence, self.resolver, self.actor = evidence, resolver, actor
        self.directory = directory
        self.gaps: list[dict[str, Any]] = []
        self.reasons: list[str] = []
        self.withhold: set[str] = set()
        self.people: dict[str, _Person] = {}
        self.names: list[tuple[_Seen, str, str | None]] = []   # name-only (seen, text, entry)
        self.undecided = False
        self.unverified = 0

    def gap(self, code: str, state: str, subject: str, detail: str, *,
            incomplete: bool = True) -> None:
        if any(g["reason"] == code and g["subject"] == subject[:700] for g in self.gaps):
            return
        self.gaps.append({"state": state, "reason": code, "subject": subject[:700],
                          "detail": f"{code}: {detail}"[:600]})
        if incomplete and code not in self.reasons:
            self.reasons.append(code)

    def entries(self, names: Sequence[str], oid: str) -> list[Mapping[str, Any]]:
        return [e for n in names for e in self.evidence.get(n, ())
                if e.get("object_id") == oid]

    def first(self, names: Sequence[str], oid: str) -> Mapping[str, Any] | None:
        for entry in self.entries(names, oid):
            if _text(entry.get("value")) not in (None, ""):
                return entry
        return None

    def resolve(self, identity: str, raw: Any) -> list[tuple[Resolution, Any, str | None]]:
        """(resolution, the identity value, a display name) of each identity a raw value
        names; a name-only value is (None-state resolution, name, None)."""
        if identity == "teams_members":
            return self._members(raw)
        if identity == "teams_party":
            party = raw
            if isinstance(party, str):
                try:
                    party = json.loads(party)
                except ValueError:
                    party = None
            if not isinstance(party, Mapping):
                return [(_UNDECIDED, None, None)]
            if party.get("kind") != "user":
                return [(Resolution(AGENT), None, None)]   # an application or device
            name = party.get("display_name")
            return [(self.directory.object_id(party.get("id")), party.get("id"),
                     name if isinstance(name, str) and name.strip() else None)]
        values = raw if isinstance(raw, list) else [raw]
        out = []
        for value in values:
            value = _text(value)
            if not isinstance(value, str) or not value.strip():
                continue
            if identity == "mixed" and value.strip().lower().startswith(AGENT_PREFIX):
                out.append((Resolution(AGENT), value, None))
            elif "@" in value or identity == "address":
                out.append((self.directory.address(value), value, None))
            else:
                out.append((Resolution("name"), value.strip(), None))
        return out

    def _members(self, raw: Any) -> list[tuple[Resolution, Any, str | None]]:
        """A Teams conversation's participants (``TeamsChat.participants``: Entra object id,
        display name, work address), each resolved by its object id, the shared key; a
        list that is not one of participants is undecided (fails closed)."""
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        if not isinstance(raw, list):
            return [(_UNDECIDED, None, None)]
        out = []
        for member in raw:
            if not isinstance(member, Mapping):
                return [(_UNDECIDED, None, None)]
            name = member.get("display_name")
            out.append((self.directory.object_id(member.get("id")), member.get("id"),
                        name if isinstance(name, str) and name.strip() else None))
        return out

    def observe(self, seen: _Seen, entry_name: str | None, identity: str, raw: Any
                ) -> list[tuple[Resolution, Any, str | None]] | None:
        """The resolutions of one observed value, or None when it names a person the
        exclusion withholds or cannot clear: then its whole record is withheld (as the
        capture drops a message an excluded person sent or is addressed or copied on, and
        the case runtime one whose sender it cannot clear)."""
        found = self.resolve(identity, raw)
        for resolution, _value, _name in found:
            if resolution.state in (WITHHELD, UNDECIDED):
                self.undecided = self.undecided or resolution.state == UNDECIDED
                return None
        return found

    def add(self, seen: _Seen, entry_name: str | None, identity: str,
            found: Sequence[tuple[Resolution, Any, str | None]]) -> None:
        """The persons of one cleared observation (``observe``)."""
        for resolution, value, name in found:
            state = resolution.state
            if state == "name":
                self.names.append((seen, value, entry_name))
                continue
            if state != PERSON:
                continue
            person = self.people.get(resolution.key)
            if person is None:
                person = self.people[resolution.key] = _Person(resolution.key,
                                                               resolution.verified)
            person.addresses |= set(resolution.addresses)
            if isinstance(value, str) and "@" in value \
                    and identity not in ("teams_party", "teams_members"):
                found = mail_address(value)
                if found:
                    person.addresses.add(found)
            person.upn = person.upn or resolution.upn
            person.seen.append(seen)
            if name:
                person.names.append((name.strip(), seen.handle, seen.field_path))

    def settle_names(self) -> None:
        """Name-only observations: a lead to the cleared people whose names match at a lead
        level, else withheld (the guard decides no name). Two cleared people whose names
        match stay two people with a lead between them."""
        known = [(p, normalize_name(n)) for p in self.people.values() for n, _, _ in p.names]
        for seen, text, entry_name in self.names:
            wanted = normalize_name(text)
            matched: dict[str, str] = {}
            for person, name in known:
                level = _name_level(wanted, name) if wanted and name else "differ"
                if level in LEAD_LEVELS and person.key not in matched:
                    matched[person.key] = level
            if not matched:
                if entry_name:
                    self.withhold.add(entry_name)
                self.undecided = True
                continue
            for key, level in matched.items():
                self.people[key].name_leads.append((seen, level))
        people = sorted(self.people.values(), key=lambda p: p.key)
        for i, one in enumerate(people):
            for other in people[i + 1:]:
                levels = {_name_level(normalize_name(a), normalize_name(b))
                          for a, _, _ in one.names for b, _, _ in other.names}
                level = next((lv for lv in LEAD_LEVELS if lv in levels), None)
                if level:
                    one.peer_leads[other.key] = level
                    other.peer_leads[one.key] = level


def _unavailable(run: _Run, all_identity_entries: Iterable[str], name: str,
                 code: str) -> dict[str, Any]:
    run.withhold |= set(all_identity_entries)
    run.gap(code, "inaccessible", name,
            "the people exclusion cannot decide who is excluded, so no person is named and "
            "every entry naming a participant is withheld (fail closed)")
    return {"entries": [{"name": name, "kind": "computed", "value": None, "inputs": [],
                         "steps": ["no person named: the people exclusion is unavailable"],
                         "check": "not_applicable"}],
            "gaps": run.gaps, "reasons": run.reasons, "withhold": sorted(run.withhold)}


def _directory(people: Any) -> Any:
    try:
        return people() if callable(people) else people
    except Exception:  # noqa: BLE001 - no directory: fail closed
        return None


def _identity_entries(evidence: Mapping[str, Sequence[Mapping[str, Any]]],
                      names: Iterable[str]) -> list[str]:
    return [e["name"] for n in names for e in evidence.get(n, ()) if e.get("name")]


def _check_failed(entry: Mapping[str, Any]) -> bool:
    return entry.get("check") == _CHECK_FAILED


# --------------------------------------------------------------------------- F18

def _record_entries(run: _Run, oid: str) -> set[str]:
    """Every property entry of one record, whatever item produced it."""
    return {e["name"] for done in run.evidence.values() for e in done
            if e.get("object_id") == oid and e.get("kind") == "property"}


def _collect(run: _Run, records: Sequence[Mapping[str, Any]]) -> None:
    """Observe every participant of every record; a record naming a person the exclusion
    withholds or cannot clear (an address or object id) is withheld whole, before any of its
    participants counts for anyone."""
    for record in records:
        label = record["label"]
        item_names = [n for spec in record["people"] for n in spec.get("items", ())]
        objects = sorted({e["object_id"] for n in [*record["time"], *item_names]
                          for e in run.evidence.get(n, ()) if e.get("object_id")})
        for oid in objects:
            time = run.first(record["time"], oid)
            when = _instant(time.get("value")) if time else None
            time_cites = (time["name"],) if time else ()
            anchor = time or run.first(item_names, oid)
            cleared: list[tuple[_Seen, str | None, str, Any]] = []
            withheld = False
            for spec in record["people"]:
                if "reread" in spec:
                    citation = (anchor or {}).get("citation") or {}
                    for path in spec["reread"]:
                        raw = _reread(run.resolver, run.actor, citation.get("l1_handle"), path)
                        if raw is None:
                            continue
                        # Read from the record's own citation, not from an answer entry: it
                        # cites none (an entry several people cited would repeat on every
                        # answer page, and a large answer would then fit no page).
                        seen = _Seen(spec["role"], oid, label, (), citation["l1_handle"],
                                     path, when)
                        found = run.observe(seen, None, spec["identity"], raw)
                        withheld = withheld or found is None
                        cleared.append((seen, None, spec["identity"], found))
                    continue
                for entry in run.entries(spec["items"], oid):
                    if _text(entry.get("value")) in (None, ""):
                        continue
                    citation = entry.get("citation") or {}
                    raw = _reread(run.resolver, run.actor, citation.get("l1_handle"),
                                  str(citation.get("field_path")))
                    stated = _text(entry.get("value"))
                    if raw is None or _check_failed(entry) or (
                            isinstance(raw, str) and isinstance(stated, str)
                            and raw.strip() != stated.strip()):
                        # Not re-read as stated for this reader: it names nobody here.
                        run.withhold.add(entry["name"])
                        run.unverified += 1
                        continue
                    seen = _Seen(spec["role"], oid, label, (*time_cites, entry["name"]),
                                 citation["l1_handle"], citation["field_path"], when)
                    found = run.observe(seen, entry["name"], spec["identity"], raw)
                    withheld = withheld or found is None
                    cleared.append((seen, entry["name"], spec["identity"], found))
            if withheld:
                run.withhold |= _record_entries(run, oid)
                continue
            for seen, entry_name, identity, found in cleared:
                run.add(seen, entry_name, identity, found or ())


def _resolution_step(person: _Person) -> str:
    return {"directory_mailbox": "identity verified: one captured directory mailbox holds "
                                 "every address and object id joined here (shared keys)",
            "object_id": "identity verified: one Entra object id (no captured mailbox)",
            "tenant_address": "identity verified: one tenant address (no captured mailbox), "
                              "cleared by the directory-bound people guard"}[person.verified]


def _person_entries(run: _Run, people: Sequence[_Person], name: str,
                    numbers: Mapping[str, int]) -> list[dict[str, Any]]:
    entries = []
    for index, person in enumerate(people, start=1):
        text, handle, path = person.label()
        roles: dict[str, set[str]] = {}
        groups: dict[tuple[str, str], list[_Seen]] = {}
        for seen in person.seen:
            roles.setdefault(seen.role, set()).add(seen.record)
            groups.setdefault((seen.role, seen.label), []).append(seen)
        steps = [_resolution_step(person),
                 "roles: " + ", ".join(f"{r} on {len(v)} record(s)"
                                       for r, v in sorted(roles.items(),
                                                          key=lambda kv: -ROLE_WEIGHTS[kv[0]]))]
        inputs: list[str] = []
        basis: list[str] = []
        for (role, label), seen_all in sorted(groups.items(),
                                              key=lambda kv: (-ROLE_WEIGHTS[kv[0][0]], kv[0])):
            ordered = sorted(seen_all, key=lambda s: (s.when is None, -(s.when.timestamp()
                                                      if s.when else 0), s.record))
            cites = [c for seen in ordered for c in seen.cites]
            fields = sorted({seen.field_path for seen in ordered})
            prefix = (f"{role} on {len({s.record for s in ordered})} {label} record(s) "
                      f"({', '.join(fields)})")
            steps.append(_joined(prefix + ": entries ", list(dict.fromkeys(cites))) if cites
                         else prefix + ": re-read from each record's citation for this reader")
            for seen in ordered:
                inputs += [c for c in seen.cites if c not in inputs]
                if seen.record not in basis:
                    basis.append(seen.record)
        if len(basis) > MAX_BASIS or len(inputs) > 50:
            steps.append(f"listed: {min(len(basis), MAX_BASIS)} of {len(basis)} records and "
                         f"{min(len(inputs), 50)} of {len(inputs)} entries (by role, then "
                         "latest first)")
        latest = person.latest()
        if latest is not None:
            steps.append(f"latest dated activity {latest.isoformat()}")
        for other, level in sorted(person.peer_leads.items()):
            peer = f"{name}_{numbers[other]}" if other in numbers else "a person not listed"
            steps.append(f"lead: may be the same person as {peer} (names {level}; no shared "
                         "key, so never merged)")
        for seen, level in person.name_leads:
            cited = ", ".join(seen.cites) or "its citation"
            steps.append(f"lead: the name on {seen.label} {seen.record} ({seen.role}, {cited}) "
                         f"matches this person's (names {level}; no shared key, so not "
                         "counted as theirs)")
            inputs += [c for c in seen.cites if c not in inputs]
        entries.append(_digest({
            "name": f"{name}_{index}"[:64], "kind": "computed",
            "value": _envelope(text, handle, path), "inputs": inputs[:50],
            "steps": [s[:MAX_STEP_CHARS] for s in steps][:40], "basis": basis[:MAX_BASIS],
            "check": "not_applicable"}))
    return entries


def people_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
               edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Mapping[str, Any]],
               name: str, as_of: str, resolver: Resolver | None, actor: Any, complete: bool,
               covers: Callable[[str], bool], people: Any = None) -> dict[str, Any]:
    """The people of the records the plan reached, ranked (module doc)."""
    directory = _directory(people)
    run = _Run(evidence, resolver, actor, directory)
    identity_items = [n for r in args["records"] for s in r["people"] for n in s.get("items", ())]
    if directory is None:
        return _unavailable(run, _identity_entries(evidence, identity_items), name,
                            "people_directory_unavailable")
    if directory.unavailable:
        return _unavailable(run, _identity_entries(evidence, identity_items), name,
                            "people_exclusion_unavailable")
    _collect(run, args["records"])
    run.settle_names()
    if run.undecided:
        run.gap("participants_not_cleared", "inaccessible", name,
                "a participant the people exclusion could not decide (a tenant address no "
                "captured mailbox holds, or a name alone) is not named here, and the entry "
                "that carried it is withheld; it may hold a later or another role, so the "
                "answer is partial")
    if run.unverified:
        run.gap("participant_unverified", "unknown", name,
                "a participant value was not re-read as stated for this reader, so it names "
                "nobody here")
    ranked = sorted(run.people.values(),
                    key=lambda p: (-p.score(), -(p.latest().timestamp() if p.latest() else 0),
                                   p.label()[0].lower(), p.key))
    if len(ranked) > MAX_PEOPLE:
        run.gap("people_truncated", "unknown", name,
                f"{len(ranked)} people took part; the {MAX_PEOPLE} ranked first are listed")
        ranked = ranked[:MAX_PEOPLE]
    numbers = {p.key: i for i, p in enumerate(ranked, start=1)}
    entries = _person_entries(run, ranked, name, numbers)
    if entries:
        entries.append({"name": name, "kind": "computed", "value": len(entries),
                        "inputs": [e["name"] for e in entries][:50],
                        "steps": [f"{len(entries)} person(s) took part in what this reader "
                                  "was shown, ranked by role and latest activity"
                                  + ("" if complete else "; the run was cut, so others may "
                                     "exist")], "check": "not_applicable"})
    else:
        entries.append({"name": name, "kind": "computed", "value": None, "inputs": [],
                        "steps": ["no person of ours found in what this reader may read"],
                        "check": "not_applicable"})
        run.gaps.append({
            "state": "unknown", "reason": "no_person_found", "subject": name,
            "detail": "no_person_found: no participant of the records this reader may read "
                      "resolved to a person of ours; a withheld source is reported in its own "
                      "gap and this is not proof that nobody worked on it",
            "searched": {"sources": [r["label"] for r in args["records"]][:8]}})
        if "no_person_found" not in run.reasons:
            run.reasons.append("no_person_found")
    return {"entries": entries, "gaps": run.gaps, "reasons": run.reasons,
            "withhold": sorted(run.withhold)}


# --------------------------------------------------------------------------- F19

IDENTITY_TIERS = ("verified", "reviewed")


def _firm_twins(edges: Sequence[Mapping[str, Any]], roots: Sequence[str],
                twin_links: Iterable[str]) -> set[str]:
    """The roots and the records a chain of identity-tier same-record links (``twin_links``,
    tier verified or reviewed) joins to them: a twin matched only by text or a lead is not
    the party, so its website is not the party's domain."""
    links = set(twin_links)
    strong: dict[str, set[str]] = {}
    for edge in edges:
        if edge.get("link_type") in links and edge.get("tier") in IDENTITY_TIERS:
            strong.setdefault(edge["from"], set()).add(edge["to"])
            strong.setdefault(edge["to"], set()).add(edge["from"])
    seen, frontier = set(roots), list(roots)
    while frontier:
        for other in strong.get(frontier.pop(), ()):
            if other not in seen:
                seen.add(other)
                frontier.append(other)
    return seen


def _party(run: _Run, args: Mapping[str, Any], edges: Sequence[Mapping[str, Any]],
           sender_items: Sequence[str], roots: Sequence[str] = ()
           ) -> tuple[set[str], set[str]]:
    """(party addresses, party registrable domains): the senders of the mail a person
    linked to the party, and its website domain; a shared mail domain is never the party's."""
    addresses: set[str] = set()
    domains: set[str] = set()
    links = set(args.get("party_links", ()))
    linked = {e["from"] for e in edges if e.get("link_type") in links}
    for oid in sorted(linked):
        for entry in run.entries(sender_items, oid):
            citation = entry.get("citation") or {}
            raw = _reread(run.resolver, run.actor, citation.get("l1_handle"),
                          str(citation.get("field_path")))
            address = mail_address(_text(raw)) if raw is not None else None
            if address and run.directory.address(address).state == EXTERNAL:
                addresses.add(address)
                domain = mail_domain(address)
                if domain and not shared_mail_domain(domain):
                    domains.add(registrable_domain(domain))
    firm = _firm_twins(edges, roots, args.get("twin_links", ()))
    for name in args.get("party_websites", ()):
        for entry in run.evidence.get(name, ()):
            if entry.get("object_id") not in firm:
                continue   # a loosely matched twin's website is not the party's
            citation = entry.get("citation") or {}
            raw = _reread(run.resolver, run.actor, citation.get("l1_handle"),
                          str(citation.get("field_path")))
            if _check_failed(entry) or not isinstance(raw, str) \
                    or raw.strip() != str(_text(entry.get("value")) or "").strip():
                continue   # not re-read as stated for this reader: no party domain
            domain = website_domain(raw)
            if domain and not shared_mail_domain(domain):
                domains.add(registrable_domain(domain))
    return addresses, domains


def said_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
             edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Mapping[str, Any]],
             name: str, as_of: str, resolver: Resolver | None, actor: Any, complete: bool,
             covers: Callable[[str], bool], people: Any = None,
             roots: Sequence[str] = ()) -> dict[str, Any]:
    """The latest messages a person of ours sent the party (module doc)."""
    messages = args["messages"]
    directory = _directory(people)
    run = _Run(evidence, resolver, actor, directory)
    if directory is None or directory.unavailable:
        out = _unavailable(run, _identity_entries(evidence, messages["sender"]), name,
                           "people_directory_unavailable" if directory is None
                           else "people_exclusion_unavailable")
        out["entries"][0]["steps"] = ["nothing said is named: the people exclusion cannot "
                                      "clear the senders"]
        return out
    party_addresses, party_domains = _party(run, args, edges, messages["sender"], roots)
    if not party_addresses and not party_domains:
        run.gap("party_address_unknown", "unknown", name,
                "no mail address or website domain of the party is shown to this reader (no "
                "reviewed sender link, no website), so no message can be shown to be to it")
    found: list[tuple[dt.datetime, str, _Person, list[str], Mapping[str, Any]]] = []
    objects = sorted({e["object_id"] for n in [*messages["time"], *messages["sender"]]
                      for e in evidence.get(n, ()) if e.get("object_id")})
    for oid in objects:
        sender = run.first(messages["sender"], oid)
        time = run.first(messages["time"], oid)
        if sender is None:
            continue
        citation = sender.get("citation") or {}
        raw = _reread(resolver, actor, citation.get("l1_handle"), str(citation.get("field_path")))
        stated = _text(sender.get("value"))
        if raw is None or _check_failed(sender) or (isinstance(raw, str) and isinstance(
                stated, str) and raw.strip() != stated.strip()):
            run.withhold.add(sender["name"])
            run.unverified += 1
            continue
        resolution = directory.address(_text(raw))
        recipients: list[str] = []
        for path in messages["recipients"]:
            got = _reread(resolver, actor, citation.get("l1_handle"), path)
            recipients += [a for a in (mail_address(_text(v)) for v in (
                got if isinstance(got, list) else [got] if got is not None else [])) if a]
        states = [resolution.state, *(directory.address(a).state for a in recipients)]
        if WITHHELD in states or UNDECIDED in states:
            # A message an excluded or uncleared person sent, or is addressed or copied on,
            # is withheld whole (the capture's own rule).
            run.withhold |= _record_entries(run, oid)
            run.undecided = run.undecided or UNDECIDED in states
            continue
        if resolution.state != PERSON:
            continue   # the party's own mail, or another outside sender: not ours
        when = _instant(time.get("value")) if time else None
        matched = [a for a in recipients if a in party_addresses or (
            (mail_domain(a) or "") and not shared_mail_domain(mail_domain(a) or "")
            and registrable_domain(mail_domain(a) or "") in party_domains)]
        if not matched or when is None:
            continue
        how = ("a recipient is an address the party sent us mail from"
               if any(a in party_addresses for a in matched)
               else "a recipient is at the party's own mail domain")
        person = _Person(resolution.key or "", resolution.verified or "object_id",
                         set(resolution.addresses), resolution.upn)
        person.seen.append(_Seen("sender", oid, "mail", (sender["name"],),
                                 citation["l1_handle"], citation["field_path"], when))
        said = [e["name"] for e in run.entries(messages["said"], oid)]
        found.append((when, oid, person, [time["name"], *said, sender["name"]],
                      {"how": how, "sender": sender}))
    if run.undecided:
        run.gap("participants_not_cleared", "inaccessible", name,
                "a message whose sender or recipient the people exclusion could not decide is "
                "withheld whole; it may be a later message to the party, so the answer is "
                "partial")
    if run.unverified:
        run.gap("participant_unverified", "unknown", name,
                "a sender value was not re-read as stated for this reader, so it names nobody")
    found.sort(key=lambda f: (-f[0].timestamp(), f[1]))
    entries: list[dict[str, Any]] = []
    if not found:
        run.gaps.append({
            "state": "unknown", "reason": "nothing_said_found", "subject": name,
            "detail": "nothing_said_found: no message from a person of ours to the party is "
                      "among what this reader may read; a withheld mailbox is reported in its "
                      "own gap and this is not proof that nothing was said",
            "searched": {"sources": ["mail"]}})
        run.reasons.append("nothing_said_found")
        entries.append({"name": name, "kind": "computed", "value": None, "inputs": [],
                        "steps": ["no message from us to the party found"],
                        "check": "not_applicable"})
        return {"entries": entries, "gaps": run.gaps, "reasons": run.reasons,
                "withhold": sorted(run.withhold)}
    for index, (when, oid, person, inputs, note) in enumerate(found[:1 + MAX_EARLIER]):
        entry_name = name if index == 0 else f"{name}_earlier_{index}"
        steps = [f"message from a person of ours to the party: mail {oid}",
                 f"time: entry {inputs[0]}", f"to the party: {note['how']} "
                 "(row.to/row.cc re-read from its citation for this reader)",
                 "what was said: entries " + ", ".join(inputs[1:-1] or ["none readable"]),
                 f"said by: entry {entry_name}_by"]
        entries.append(_digest({"name": entry_name[:64], "kind": "computed",
                                "value": when.isoformat().replace("+00:00", "Z"),
                                "inputs": inputs[:50], "steps": steps, "basis": [oid],
                                "check": "not_applicable"}))
        text, handle, path = person.label()
        entries.append(_digest({
            "name": f"{entry_name}_by"[:64], "kind": "computed",
            "value": _envelope(text, handle, path), "inputs": [inputs[-1]],
            "steps": [_resolution_step(person), f"sender of mail {oid} (entry {inputs[-1]})"],
            "basis": [oid], "check": "not_applicable"}))
    if len(found) > 1 + MAX_EARLIER:
        run.gap("earlier_messages_not_listed", "unknown", name,
                f"{len(found) - 1 - MAX_EARLIER} earlier message(s) to the party are not "
                "listed", incomplete=False)
    return {"entries": entries, "gaps": run.gaps, "reasons": run.reasons,
            "withhold": sorted(run.withhold)}


PLAN_ENGINES = {PEOPLE_ENGINE: {"check": people_check, "run": people_run},
                SAID_ENGINE: {"check": said_check, "run": said_run}}

__all__ = ["PEOPLE_ENGINE", "PLAN_ENGINES", "PeopleDirectory", "Resolution", "SAID_ENGINE",
           "directory_of", "people_check", "people_run", "said_check", "said_run"]
