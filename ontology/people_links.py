"""Messages, meetings, Teams posts, Planner tasks and Mission Control work linked to the
``Person`` records they name (``ontology.people_directory``): a ``post_project`` step
(``operating/build_steps.py`` ``with_people``; after ``reviews``, so a pair a person
rejected is never linked).

Every link is an **exact** match of a stored key, ``corroborated``:

* an address (lower case, trimmed) that is exactly one Person's address: an ``Email``'s
  ``sender`` (``Email.sent_by_person``) and its ``to_addresses``/``cc_addresses``
  (``Email.addressed_to_person``); the ``address`` of a ``CalendarEvent``'s or
  ``Meeting``'s ``organizer`` and ``attendees`` (``*.with_person``); an ``MCApproval``'s
  ``requested_by``/``decided_by`` (``MCApproval.with_person``);
* an Entra object id (lower case) that is exactly one staff Person's: the ``id`` of a
  Teams message's ``from`` when its ``kind`` is ``user`` (``*.sent_by_person``) and a
  ``PlannerTask``'s ``created_by``, ``completed_by`` and ``assignees``
  (``PlannerTask.with_person``), and each participant ``id`` of a Teams chat or channel
  (``TeamsChat.with_person``, ``TeamsChannel.with_person``);
* a Mission Control task's ``assignee``/``accountable_owner``, a bare name (no ``@``),
  that is exactly the local part of one staff Person's address at our own domains and
  of no other staff Person's (``MCTask.with_person``).

* an ``Email`` ``sender`` at one of our own domains (``ours``: our domains and the staff
  directory's, never a shared provider's or a help-desk host) that no Person holds, whose
  local part is exactly one staff Person's at our domains (counted ``alias_linked``): one
  mailbox's alias at another of the tenant's domains (staff hold ``x@`` at two of ours
  with one local part; the directory lists one address of most). Only with a usable
  people exclusion (else counted ``alias_exclusion_unavailable``), only for an address
  it decides (else ``alias_undecided``: an unlisted alias may be an excluded person's) and
  never for one it decides is an excluded person's (``alias_excluded``).

A key two Persons hold links nothing (counted ``ambiguous``). There is no Person for an
excluded or withheld person, so nothing ever links to one: an unknown key links nothing
(counted ``no_person``). Each link cites the value it read and the Person's value as
features (never the values), its evidence handles are those versions' and the Person's,
and its markings their union (a Person is ``pii``). A person's rejection (a
``contradicted`` link of the pair) vetoes it. Rows a previous build wrote (link id
prefix ``PREFIX``) are removed first. Stdlib only."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import json
import re
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ontology import mentions
from ontology import step_reads as SR
from ontology.sender_parties import _digest, _q, _values, _versions, own_domains

PREFIX = "person:"
_PREFIX_END = PREFIX[:-1] + chr(ord(PREFIX[-1]) + 1)
PERSON = "Person"
BATCH = 5000
_OID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ADDRESS = re.compile(r"[^@\s<>\"]{1,64}@[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+")
_LOCAL = re.compile(r"[a-z0-9][a-z0-9._'-]{0,63}")

# source type -> property -> (link type, how the value names a person)
FAMILIES: dict[str, dict[str, tuple[str, str]]] = {
    "Email": {"sender": ("Email.sent_by_person", "own_address"),
              "to_addresses": ("Email.addressed_to_person", "addresses"),
              "cc_addresses": ("Email.addressed_to_person", "addresses")},
    "CalendarEvent": {"organizer": ("CalendarEvent.with_person", "attendees"),
                      "attendees": ("CalendarEvent.with_person", "attendees")},
    "Meeting": {"organizer": ("Meeting.with_person", "attendees"),
                "attendees": ("Meeting.with_person", "attendees")},
    "TeamsChatMessage": {"from": ("TeamsChatMessage.sent_by_person", "teams_user")},
    "TeamsChannelMessage": {"from": ("TeamsChannelMessage.sent_by_person", "teams_user")},
    "TeamsChat": {"participants": ("TeamsChat.with_person", "participants")},
    "TeamsChannel": {"participants": ("TeamsChannel.with_person", "participants")},
    "PlannerTask": {"created_by": ("PlannerTask.with_person", "oid"),
                    "completed_by": ("PlannerTask.with_person", "oid"),
                    "assignees": ("PlannerTask.with_person", "oids")},
    "MCTask": {"assignee": ("MCTask.with_person", "local_or_address"),
               "accountable_owner": ("MCTask.with_person", "local_or_address")},
    "MCApproval": {"requested_by": ("MCApproval.with_person", "address"),
                   "decided_by": ("MCApproval.with_person", "address")},
    # a Business Central contact whose stored email is exactly one Person's address
    "Contact": {"email": ("Contact.same_person_as", "address")},
}
# One person's other addresses (``people_directory.plus_base`` / ``alias_key``): a Person
# whose address is another's plus-addressed form (``a+tag@d`` and ``a@d``) is that
# person, corroborated (RFC 5233: the tag delivers to the same mailbox); the same plus
# base under another host of one registrable domain (``a@mail.acme.com``, ``a@acme.com``)
# is only possibly one person, a lead. Never merged: each Person keeps its own audience
# (the mailboxes its own address wrote to), and a reader sees the link only when it may
# see both Persons. An alias group larger than ALIAS_GROUP_MAX links nothing (a shared
# local part like ``info`` across a company's hosts names no one person).
ALIAS_LINK = "Person.same_person_as"
ALIAS_GROUP_MAX = 4
LINK_TYPES = tuple(sorted({lt for props in FAMILIES.values() for lt, _how in props.values()}))
PERSON_PROPS = ("kind", "object_id", "addresses")


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _address(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    return lowered if _ADDRESS.fullmatch(lowered) else None


def _oid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    return lowered if _OID.fullmatch(lowered) else None


def keys(how: str, value: Any) -> list[tuple[str, str]]:
    """The (key kind, key) a stored value names a person by (module docstring)."""
    if how in ("address", "own_address"):
        found = _address(value)
        return [("address", found)] if found else []
    if how == "addresses":
        listed = _json(value)
        return [("address", a) for a in map(_address, listed if isinstance(listed, list)
                                             else []) if a]
    if how == "attendees":
        listed = _json(value)
        listed = [listed] if isinstance(listed, dict) else listed
        return [("address", a) for a in (_address(item.get("address"))
                                         for item in (listed if isinstance(listed, list) else [])
                                         if isinstance(item, dict)) if a]
    if how == "teams_user":
        held = _json(value)
        if isinstance(held, dict) and held.get("kind") == "user":
            found = _oid(held.get("id"))
            return [("oid", found)] if found else []
        return []
    if how == "participants":
        listed = _json(value)
        out: list[tuple[str, str]] = []
        for item in listed if isinstance(listed, list) else []:
            if isinstance(item, dict):
                found = _oid(item.get("id"))
                out.extend([("oid", found)] if found else [])
        return out
    if how == "oid":
        found = _oid(value)
        return [("oid", found)] if found else []
    if how == "oids":
        listed = _json(value)
        return [("oid", o) for o in map(_oid, listed if isinstance(listed, list) else []) if o]
    if how == "local_or_address" and isinstance(value, str) and "@" in value:
        found = _address(value)
        return [("address", found)] if found else []
    if how in ("local", "local_or_address"):
        if not isinstance(value, str):
            return []
        lowered = value.strip().lower()
        return [("local", lowered)] if _LOCAL.fullmatch(lowered) else []
    return []


@dataclass
class Holder:
    """One Person's value a key was read from."""
    person: str
    handle: str
    cite: Mapping[str, Any]
    markings: str
    recorded_at: str


@dataclass
class People:
    """key kind -> key -> the Persons holding it (each with the value it came from)."""
    by: dict[str, dict[str, dict[str, Holder]]] = field(
        default_factory=lambda: {"address": defaultdict(dict), "oid": defaultdict(dict),
                                 "local": defaultdict(dict)})
    ours: frozenset[str] = frozenset()   # our own mail domains (``read_people``)
    guard: Any = None                    # the people exclusion; None: unavailable

    def alias(self, address: str) -> tuple[str, Holder | None]:
        """The one staff Person whose local part an address at our own domains that no
        Person holds is (module docstring), as (outcome, holder)."""
        local, _, domain = address.rpartition("@")
        if not any(mentions.domain_under(domain, d) for d in self.ours) or \
                not self.by["local"].get(local):
            return "no_person", None
        if self.guard is None:
            return "alias_exclusion_unavailable", None
        if not self.guard.decides(address):   # whose it is cannot be told: never linked
            return "alias_undecided", None
        if self.guard.is_excluded(address):
            return "alias_excluded", None
        outcome, holder = self.judge("local", local)
        return "alias_" + outcome, holder

    def hold(self, kind: str, key: str, holder: Holder) -> None:
        self.by[kind][key].setdefault(holder.person, holder)

    def judge(self, kind: str, key: str) -> tuple[str, Holder | None]:
        held = self.by[kind].get(key)
        if not held:
            return "no_person", None
        if len(held) > 1:
            return "ambiguous", None
        return "linked", next(iter(held.values()))

    def persons(self) -> set[str]:
        return {p for table in self.by.values() for held in table.values() for p in held}


def read_people(conn: Any, own: Sequence[str]) -> People:
    """Every projected Person's keys: its addresses (and object id, a staff person's), and
    a staff person's local parts at our own domains (``own``) or the staff domains."""
    rows: dict[str, dict[str, Any]] = defaultdict(dict)
    for row, prop in _values(conn, PERSON_PROPS, (PERSON + ":",)):
        rows[row["object_id"]][prop] = row
    staff_domains = {d for held in rows.values() if held.get("kind") is not None
                     and held["kind"]["value"] == "staff" and held.get("addresses") is not None
                     for a in (_json(held["addresses"]["value"]) or [])
                     if isinstance(a, str) and (d := mentions.mail_domain(a))}
    ours = set(own) | set(mentions.own_mail_domains(staff_domains))
    people = People(ours=frozenset(mentions.own_mail_domains(ours)))
    for person, held in sorted(rows.items()):
        kind = held.get("kind")
        staff = kind is not None and kind["value"] == "staff"
        for prop, row in held.items():
            holder = Holder(person, row["l1_handle"],
                            {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
                             "digest": row["digest"], "lineage": row["lineage"]},
                            mentions._marks(row["vmarks"], row["kmarks"]), row["recorded_at"])
            if prop == "object_id" and staff and (oid := _oid(row["value"])):
                people.hold("oid", oid, holder)
            elif prop == "addresses":
                listed = _json(row["value"])
                for a in map(_address, listed if isinstance(listed, list) else []):
                    if not a:
                        continue
                    people.hold("address", a, holder)
                    local, domain = a.rsplit("@", 1)
                    if staff and mentions.sent_from_own_domain(domain, sorted(ours)):
                        people.hold("local", local, holder)
    return people


def judge_values(conn: Any, people: People, families: Mapping[str, Mapping[str, Any]],
                 counts: Counter) -> Iterator[tuple[Any, str, Holder, str]]:
    """(value row, link type, Person holder, property) of every stored value naming exactly
    one Person, over the declared ``families``."""
    props = sorted({p for f in families.values() for p in f})
    if not props:
        return
    for row, prop in _values(conn, props, tuple(t + ":" for t in families)):
        source = row["object_id"].split(":", 1)[0]
        declared = families.get(source, {}).get(prop)
        if declared is None:
            continue
        link_type, how = declared
        for kind, key in dict.fromkeys(keys(how, row["value"])):
            outcome, holder = people.judge(kind, key)
            if outcome == "no_person" and how == "own_address":
                outcome, holder = people.alias(key)
            counts[f"{link_type}:{outcome}"] += 1
            if holder is not None:
                yield row, link_type, holder, prop


def link_row(value: Any, link_type: str, holder: Holder, prop: str, version: Any
             ) -> dict[str, Any]:
    cite = {"l1_handle": value["l1_handle"], "field_path": value["field_path"],
            "digest": value["digest"], "lineage": value["lineage"]}
    reason = [{"feature": f"{prop}_key", "agrees": True, "citations": [cite]},
              {"feature": "person_key", "agrees": True, "citations": [dict(holder.cite)]}]
    return {
        "link_id": PREFIX + _digest(value["object_id"], link_type, holder.person),
        "link_type": link_type, "from_object": value["object_id"],
        "to_object": holder.person, "tier": "corroborated",
        "binding_kind": mentions.BINDING_KIND, "link_contract": None,
        "reason": json.dumps(reason, sort_keys=True, separators=(",", ":")),
        "why_not": "[]",
        "evidence_handles": json.dumps(list(dict.fromkeys(
            [value["l1_handle"], holder.handle, version["l1_handle"]])), separators=(",", ":")),
        "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
        "markings": mentions._marks(value["vmarks"], value["kmarks"], holder.markings,
                                    version["markings"]),
        "recorded_at": max(value["recorded_at"] or "", holder.recorded_at or "",
                           version["recorded_at"] or ""),
        "superseded_at": None}


def declared_families(schema_links: Mapping[str, Mapping[str, Any]]
                      ) -> dict[str, dict[str, tuple[str, str]]]:
    """FAMILIES less the link types the schema does not declare with a mention binding."""
    out: dict[str, dict[str, tuple[str, str]]] = {}
    for source, props in FAMILIES.items():
        for prop, (link_type, how) in props.items():
            if any(b.get("kind") == mentions.BINDING_KIND
                   for b in (schema_links.get(link_type) or {}).get("bindings", ())):
                out.setdefault(source, {})[prop] = (link_type, how)
    return out


def alias_pairs(people: People, counts: Counter, part: tuple[int, int] | None = None
                ) -> Iterator[tuple[Holder, Holder, str, str]]:
    """(from holder, to holder, tier, feature) of every Person alias link (``ALIAS_LINK``):
    a plus-addressed Person to the Person of its plus base (else to the least Person of
    the base, none holding it), ``corroborated``, ``plus_address``; two Persons of one
    alias key under different plus bases, ``lead``, ``sub_domain`` (the lesser id first).
    One link per pair, the firmer kept.

    ``part`` (k, n), a parted run's (``pg_step_inplace.PARTED_STEPS``; every part reads
    the Persons whole): only the pairs from a Person of part k (``object_part``, the part
    staging holds a link to by its from object) and only their counts, a crowded group's
    in the part of its least Person. The parts' union is the unparted output, and
    their counts sum to its counts (TRAIN2-B1). None: every pair, as before."""
    from ontology.people_directory import alias_key, plus_base
    if part is None:
        def mine(person: str) -> bool:
            return True
    else:
        from ontology.pg_step_inplace import object_part
        k, n = part

        def mine(person: str) -> bool:
            return object_part(person, n) == k
    bases: dict[str, dict[str, Holder]] = defaultdict(dict)
    aliases: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    exact: dict[str, str] = {}   # address -> its one Person
    for found, held in people.by["address"].items():
        base, alias = plus_base(found), alias_key(found)
        for person, holder in held.items():
            if base:
                bases[base].setdefault(person, holder)
            if base and alias:
                aliases[alias][base].add(person)
        if len(held) == 1:
            exact[found] = next(iter(held))
    firm: set[frozenset[str]] = set()
    for base, held in sorted(bases.items()):
        if len(held) < 2:
            continue
        target = exact.get(base) if exact.get(base) in held else min(held)
        for person in sorted(held):
            if person != target:
                firm.add(frozenset((person, target)))
                if not mine(person):
                    continue
                counts[f"{ALIAS_LINK}:plus_address"] += 1
                yield held[person], held[target], "corroborated", "plus_address"
    for _alias, by_base in sorted(aliases.items()):
        if len(by_base) < 2:
            continue
        persons = sorted({p for group in by_base.values() for p in group})
        if len(persons) > ALIAS_GROUP_MAX:
            if mine(persons[0]):
                counts[f"{ALIAS_LINK}:alias_group_too_large"] += 1
            continue
        of = {p: b for b, group in by_base.items() for p in group}
        for i, a in enumerate(persons):
            for b in persons[i + 1:]:
                if of[a] == of[b] or frozenset((a, b)) in firm or not mine(a):
                    continue
                counts[f"{ALIAS_LINK}:sub_domain"] += 1
                yield bases[of[a]][a], bases[of[b]][b], "lead", "sub_domain"


def alias_row(first: Holder, second: Holder, tier: str, feature: str,
              versions: Mapping[str, Any]) -> dict[str, Any] | None:
    """The ``ALIAS_LINK`` row from ``first``'s Person to ``second``'s (None when either has
    no current version); evidence both Persons' values and versions, markings their union."""
    one, two = versions.get(first.person), versions.get(second.person)
    if one is None or two is None:
        return None
    reason = [{"feature": feature, "agrees": True, "citations": [dict(first.cite)]},
              {"feature": "person_key", "agrees": True, "citations": [dict(second.cite)]}]
    return {
        "link_id": PREFIX + _digest(first.person, ALIAS_LINK, second.person),
        "link_type": ALIAS_LINK, "from_object": first.person, "to_object": second.person,
        "tier": tier, "binding_kind": mentions.BINDING_KIND, "link_contract": None,
        "reason": json.dumps(reason, sort_keys=True, separators=(",", ":")),
        "why_not": "[]" if tier == "corroborated" else json.dumps(
            [{"feature": "sub_domain_only", "agrees": None,
              "detail": "the same mailbox name under another host of the domain: possibly "
                        "one person, never merged"}], sort_keys=True, separators=(",", ":")),
        "evidence_handles": json.dumps(list(dict.fromkeys(
            [first.handle, second.handle, one["l1_handle"], two["l1_handle"]])),
            separators=(",", ":")),
        "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
        "markings": mentions._marks(first.markings, second.markings, one["markings"],
                                    two["markings"]),
        "recorded_at": max(first.recorded_at or "", second.recorded_at or "",
                           one["recorded_at"] or "", two["recorded_at"] or ""),
        "superseded_at": None}


def people_links_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``post_project`` step (module docstring)."""
    conn = builder.connection
    conn.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?",
                 (PREFIX, _PREFIX_END))
    links = getattr(projector, "links", {}) or {}
    families = declared_families(links)
    counts: Counter[str] = Counter()
    if not families:
        return {"counts": {}, "link_types": {}}
    aliased = any(b.get("kind") == mentions.BINDING_KIND
                  for b in (links.get(ALIAS_LINK) or {}).get("bindings", ()))
    people = read_people(conn, own_domains(projector))
    people.guard = _excluder()
    versions = _versions(conn, people.persons())
    written: Counter[str] = Counter()
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        if not pending:
            return
        pairs = SR.rejected_pairs(conn, (r["from_object"] for r in pending))
        rejected = {(source, to, kind) for source, found in pairs.items() for to, kind in found}
        # a pair two values name (To and Cc, organizer and attendee) is one link: kept once
        # in the batch and never again once written (the link id names the pair)
        ids = sorted({r["link_id"] for r in pending})
        held = {row[0] for i in range(0, len(ids), 500) for row in _q(
            conn, f"SELECT link_id FROM link_rows WHERE link_id IN "
                  f"({','.join('?' * len(ids[i:i + 500]))})", ids[i:i + 500])}
        once = {}
        for r in pending:
            if r["link_id"] not in held:
                once.setdefault(r["link_id"], r)
        candidates = list(once.values())
        kept = [r for r in candidates
                if (r["from_object"], r["to_object"], r["link_type"]) not in rejected]
        counts["rejected"] += len(candidates) - len(kept)
        if kept:
            builder.add("links", kept)
        for r in kept:
            written[r["link_type"]] += 1
        pending.clear()

    for value, link_type, holder, prop in judge_values(conn, people, families, counts):
        version = versions.get(holder.person)
        if version is None:
            continue
        pending.append(link_row(value, link_type, holder, prop, version))
        if len(pending) >= BATCH:
            flush()
    # a parted run (``conn.part``: k, n, the whole prefixes): its part's Person pairs only
    part = getattr(conn, "part", None)
    pairs = alias_pairs(people, counts, None if part is None else (part[0], part[1])) \
        if aliased else ()
    for first, second, tier, feature in pairs:
        row = alias_row(first, second, tier, feature, versions)
        if row is not None:
            pending.append(row)
        if len(pending) >= BATCH:
            flush()
    flush()
    return {"counts": dict(counts), "link_types": dict(written)}


def _excluder() -> Any:
    """The people exclusion from the environment, or None when it is unavailable."""
    import os
    from integrations.people_exclusion import PeopleExclusion, environ_with_exclusion
    guard = PeopleExclusion.from_env(environ_with_exclusion(os.environ))
    return None if guard.unavailable else guard


__all__ = ["ALIAS_LINK", "FAMILIES", "LINK_TYPES", "PREFIX", "People", "alias_pairs",
           "alias_row", "declared_families", "judge_values",
           "keys", "link_row", "people_links_step", "read_people"]
