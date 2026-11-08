"""Who knows what (``plx.aggregate`` mode ``who_knows``): per person of ours, the customers,
suppliers and items they correspond about most, and how many open threads wait on their
reply. Counts only: no message body, subject or address of a message is read or returned.

Read from the projection store the tools read:

* a person is a staff ``Person`` (``ontology/people_directory.py``) the reader may read (its
  audience granted, its values' markings held). The people exclusion
  (``PLX_EXCLUDED_PEOPLE_OIDS``, ``integrations.people_exclusion``) decides each one by its
  Entra object id, else by its addresses; a guard that is unavailable or cannot decide a
  person leaves nobody, or that person, out (fail closed, never named or counted);
* a message counts for a person when a current link ties it to them (``PERSON_LINKS``:
  sender, To/Cc, a Teams author) and the reader may read the message and the link;
* a customer, supplier or item counts for that message when a current link names it
  (``ENTITY_LINKS``: a message sent by or mentioning it) and the reader may read the record
  and its label;
* waiting: the open ``needs_my_reply`` alerts of the proactive inbox
  (``operating.proactive_commitments``) whose owner is that person and whose message the
  reader may read.

Each person lists at most ``TOP`` of each kind, the most messages first; at most
``MAX_PEOPLE`` people, the most messages first. Fixture runs are ``isolated-fixture``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping, MutableMapping

from ontology.json_contract import validate

TOOL_NAME = "plx.aggregate"   # served as its mode ``who_knows``, never a listed tool
MODE = "who_knows"
FORMAT = "plx-who-knows/v1"
TOP = 5
MAX_PEOPLE = 50
PERSON = "Person"
STAFF_KINDS = frozenset({"staff", "employee", "internal", "ours"})
PERSON_LINKS = ("Email.sent_by_person", "Email.addressed_to_person",
                "TeamsChatMessage.sent_by_person", "TeamsChannelMessage.sent_by_person")
ENTITY_LINKS = {
    "customers": ("Email.sent_by_customer", "Email.mentions_customer",
                  "TeamsChatMessage.mentions_customer", "TeamsChannelMessage.mentions_customer"),
    "suppliers": ("Email.sent_by_supplier", "Email.mentions_supplier",
                  "TeamsChatMessage.mentions_supplier", "TeamsChannelMessage.mentions_supplier"),
    "items": ("Email.mentions_product", "TeamsChatMessage.mentions_product",
              "TeamsChannelMessage.mentions_product"),
}
LABEL_PROPS = ("name", "code", "erp_code")
WAITING_KIND = "needs_my_reply"
_TEXT = re.compile(r"^[^\x00-\x1f]{1,120}$")


def _contracts() -> Mapping[str, Any]:
    from .ontology_tools import load_contracts
    return load_contracts()


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message[:600]}}


def _marks(raw: Any) -> frozenset[str] | None:
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or not all(isinstance(m, str) for m in parsed):
        return None
    return frozenset(parsed)


def _chunks(ids: list[str], size: int = 500):
    for start in range(0, len(ids), size):
        yield json.dumps(ids[start:start + size])


def answer(conn: sqlite3.Connection, *, may_read: Callable[[str], bool],
           held: frozenset[str], guard: Any, waiting: Mapping[str, list[str]] | None = None,
           person: str | None = None) -> dict[str, Any]:
    """The who-knows-what answer for one reader (module doc). ``waiting``: person object
    id (Entra) -> the message ids of its open ``needs_my_reply`` alerts (None: unknown)."""
    from ontology import store as ostore

    unavailable = getattr(guard, "unavailable", "excluded_people_oids_absent") \
        if guard is not None else "excluded_people_oids_absent"
    base = {"mode": MODE, "format": FORMAT, "people": [], "complete": False}
    if unavailable:
        return {**base, "withheld": "people_exclusion_unavailable"}
    audience: dict[str, str] = {}

    def readable(oid: str) -> bool:
        key = audience.get(oid)
        return key is not None and may_read(key)

    def held_all(raw: Any) -> bool:
        marks = _marks(raw)
        return marks is not None and marks <= held

    alive = "NOT " + ostore.removed_at_sql("o.object_id")
    people: dict[str, dict[str, Any]] = {}
    undecided = 0
    for oid, key in conn.execute(
            f"SELECT o.object_id, o.audience_key FROM objects o WHERE o.object_type = ? "
            f"AND {alive}", (PERSON,)):
        audience[oid] = key
        # every person is decided by the exclusion (a message naming an excluded one is
        # withheld for every reader); only those this reader may read are shown
        people[oid] = {"values": defaultdict(list), "handles": {}, "visible": may_read(key),
                       "ok": may_read(key) and (person is None or oid == person)}
    for chunk in _chunks(sorted(people)):
        for oid, prop, value, marks, handle in conn.execute(
                "SELECT object_id, property, value, markings, l1_handle FROM object_values "
                "WHERE superseded_at IS NULL AND object_id IN (SELECT value FROM json_each(?))",
                (chunk,)):
            if not held_all(marks):
                people[oid]["ok"] = False   # a value the reader may not hold: not shown
                people[oid]["visible"] = False
            people[oid]["values"][prop].append(value)
            people[oid]["handles"].setdefault(prop, handle)
    cleared: dict[str, dict[str, Any]] = {}
    withheld: set[str] = set()   # excluded or undecided: their messages count for no one
    seeable: set[str] = set()    # cleared and readable by this reader (any kind)
    for oid, entry in sorted(people.items()):
        values = entry["values"]
        kind = str((values.get("kind") or [""])[0] or "").strip().casefold()
        ids = [v for v in values.get("object_id", ()) if v]
        addresses = [a for raw in values.get("addresses", ()) + values.get("upn", [])
                     for a in _addresses(raw)]
        try:
            if ids:
                excluded = any(guard.is_excluded(v) for v in ids)
            elif addresses and all(guard.decides(a) for a in addresses):
                excluded = any(guard.is_excluded(a) for a in addresses)
            else:
                undecided += 1
                withheld.add(oid)
                continue
        except Exception:   # noqa: BLE001 - a guard that cannot decide: left out
            undecided += 1
            withheld.add(oid)
            continue
        if excluded:
            withheld.add(oid)
            continue   # never named, never counted
        if entry["visible"]:
            seeable.add(oid)
        if not entry["ok"] or kind not in STAFF_KINDS:
            continue   # not this reader's to see, not the one asked about, or not ours
        # the name as source text, cited to the value it was read from (never bare)
        named = [*((v, "upn") for v in values.get("upn", ())),
                 *((a, "addresses") for raw in values.get("addresses", ())
                   for a in _addresses(raw)),
                 *((a, "upn") for raw in values.get("upn", ()) for a in _addresses(raw))]
        label = next(((str(v), prop) for v, prop in named if v and _TEXT.match(str(v))
                      and entry["handles"].get(prop)), None)
        if label is None:
            continue
        cleared[oid] = {"label": _envelope(label[0], entry["handles"][label[1]]),
                        "oids": ids}

    # messages per person: the link and the message both readable
    messages: dict[str, set[str]] = defaultdict(set)
    linked: list[tuple[str, str, Any]] = []
    for chunk in _chunks(sorted(set(cleared) | withheld)):
        linked.extend(conn.execute(
            "SELECT from_object, to_object, markings FROM link_rows WHERE to_object IN "
            "(SELECT value FROM json_each(?)) AND link_type IN (SELECT value FROM "
            "json_each(?)) AND superseded_at IS NULL AND tier <> 'contradicted'",
            (chunk, json.dumps(PERSON_LINKS))))
    _audiences(conn, audience, {m for m, _p, _k in linked})
    # a message naming an excluded or undecided person in any role is withheld whole
    dropped = {m for m, who, _k in linked if who in withheld}
    for message, who, marks in linked:
        if who in cleared and message not in dropped and readable(message) \
                and held_all(marks):
            messages[who].add(message)
    every = sorted({m for found in messages.values() for m in found})
    about: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    entity_types = {t: kind for kind, types in ENTITY_LINKS.items() for t in types}
    rows: list[tuple[str, str, str, Any]] = []
    for chunk in _chunks(every):
        rows.extend(conn.execute(
            "SELECT from_object, to_object, link_type, markings FROM link_rows WHERE "
            "from_object IN (SELECT value FROM json_each(?)) AND link_type IN (SELECT value "
            "FROM json_each(?)) AND superseded_at IS NULL AND tier <> 'contradicted'",
            (chunk, json.dumps(sorted(entity_types)))))
    _audiences(conn, audience, {e for _m, e, _t, _k in rows})
    labels = _labels(conn, sorted({e for _m, e, _t, _k in rows if readable(e)}), held_all)
    for message, entity, link_type, marks in rows:
        if readable(entity) and held_all(marks) and entity in labels:
            about[message][entity_types[link_type]].add(entity)
    hidden_waiting: set[str] = set()
    if waiting is not None:
        pending = sorted({m for found in waiting.values() for m in found})
        _audiences(conn, audience, set(pending))
        # a waiting message naming a person this reader may not see (excluded, undecided,
        # outside its grant) is not counted
        for chunk in _chunks(pending):
            for message, who in conn.execute(
                    "SELECT from_object, to_object FROM link_rows WHERE from_object IN "
                    "(SELECT value FROM json_each(?)) AND link_type IN (SELECT value FROM "
                    "json_each(?)) AND superseded_at IS NULL AND tier <> 'contradicted'",
                    (chunk, json.dumps(PERSON_LINKS))):
                if who not in seeable:
                    hidden_waiting.add(message)
    out = []
    for oid, info in cleared.items():
        found = messages.get(oid, set())
        tallies = {kind: Counter() for kind in ENTITY_LINKS}
        for message in found:
            for kind, entities in about.get(message, {}).items():
                tallies[kind].update(entities)
        waits = None
        if waiting is not None:
            waits = len({m for i in info["oids"] for m in waiting.get(i.casefold(), ())
                         if readable(m) and m not in hidden_waiting})
        out.append({"person": oid, "label": info["label"], "messages": len(found),
                    **{kind: [{"object_id": e, "label": labels[e], "messages": n}
                              for e, n in sorted(t.items(), key=lambda x: (
                                  -x[1], _text(labels[x[0]]), x[0]))[:TOP]]
                       for kind, t in tallies.items()},
                    "waiting_on_them": waits})
    out.sort(key=lambda p: (-p["messages"], _text(p["label"]), p["person"]))
    complete = undecided == 0 and waiting is not None and len(out) <= MAX_PEOPLE
    return {**base, "people": out[:MAX_PEOPLE], "complete": complete,
            **({"more_people": len(out) - MAX_PEOPLE} if len(out) > MAX_PEOPLE else {})}


def _addresses(raw: Any) -> list[str]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else raw
    except ValueError:
        parsed = raw
    values = parsed if isinstance(parsed, list) else [parsed]
    return [str(v).strip().lower() for v in values if isinstance(v, str) and "@" in v]


def _audiences(conn: sqlite3.Connection, known: MutableMapping[str, str],
               ids: set[str]) -> None:
    wanted = sorted(i for i in ids if i not in known)
    for chunk in _chunks(wanted):
        for oid, key in conn.execute("SELECT object_id, audience_key FROM objects WHERE "
                                     "object_id IN (SELECT value FROM json_each(?))", (chunk,)):
            known[oid] = key


def _envelope(text: str, handle: str) -> dict[str, Any]:
    """Source text as the contract's untrusted_text envelope, cited to its value."""
    return {"untrusted_text": text[:120], "origin": {"citation": handle}}


def _text(label: Mapping[str, Any]) -> str:
    return label["untrusted_text"]


def _labels(conn: sqlite3.Connection, ids: list[str], held_all: Callable[[Any], bool]
            ) -> dict[str, dict[str, Any]]:
    """A record's name (else code) the reader may hold, as source text in its envelope,
    cited to the value it was read from."""
    found: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for chunk in _chunks(ids):
        for oid, prop, value, marks, handle in conn.execute(
                "SELECT object_id, property, value, markings, l1_handle FROM object_values "
                "WHERE superseded_at IS NULL AND property IN (SELECT value FROM json_each(?)) AND "
                "object_id IN (SELECT value FROM json_each(?))",
                (json.dumps(LABEL_PROPS), chunk)):
            text = " ".join(str(value or "").split())
            if held_all(marks) and text and _TEXT.match(text) and handle:
                found[oid].setdefault(prop, _envelope(text, handle))
    return {oid: next(v[p] for p in LABEL_PROPS if p in v) for oid, v in found.items()}


def waiting_of(inbox: Path) -> dict[str, list[str]] | None:
    """Owner object id -> message ids of the open ``needs_my_reply`` alerts (None: no inbox
    or one this code cannot read)."""
    if not inbox.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{inbox}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT object_id, follow_up FROM gaps WHERE kind = ? AND "
                                "state = 'open'", (WAITING_KIND,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    out: dict[str, list[str]] = defaultdict(list)
    for message, follow in rows:
        try:
            owner = json.loads(follow or "{}").get("owner")
        except ValueError:
            continue
        if isinstance(owner, str) and owner:
            out[owner.casefold()].append(message)
    return dict(out)


class WhoKnowsTool:
    """``plx.aggregate`` mode ``who_knows`` over a host's ``OntologyTools``."""

    def __init__(self, base: Any, guard: Callable[[], Any] | None = None,
                 inbox: Path | None = None) -> None:
        self.base = base
        self.guard = guard or (lambda: _env_guard())
        row = base.store.conn.execute("PRAGMA database_list").fetchone()
        if inbox is None and row and row[2]:
            from .proactive_engine import inbox_path
            inbox = inbox_path(Path(row[2]))
        self.inbox = inbox

    def call(self, arguments: Any, actor: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict) or arguments.get("mode") != MODE:
            return self._checked(_error("invalid_input", "mode must be who_knows"))
        person = arguments.get("person")
        if person is not None and (not isinstance(person, str) or not person.startswith(
                PERSON + ":") or len(person) > 700):
            return self._checked(_error("invalid_input", "person must be a Person record id"))
        policy = self.base.policy
        result = answer(self.base.store.conn,
                        may_read=lambda key: policy.may_read(actor, key),
                        held=frozenset(policy.held_markings(actor)), guard=self.guard(),
                        waiting=waiting_of(self.inbox) if self.inbox else None,
                        person=person)
        return self._checked(result)

    def _checked(self, result: dict[str, Any]) -> dict[str, Any]:
        contracts = _contracts()
        schema = contracts["$defs"]["who_knows_answer" if "error" not in result
                                    else "error_result"]
        problems = validate(result, schema, root=contracts)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + str(problems[0]))[:600])
        return result


def _env_guard() -> Any:
    from integrations.people_exclusion import PeopleExclusion

    return PeopleExclusion.from_env(os.environ)


def register(table: MutableMapping[str, dict[str, Any]], base: Any) -> WhoKnowsTool | None:
    """Serve ``plx.aggregate`` mode ``who_knows`` in a host tool table: a call naming that
    mode goes here, every other call to the handler registered before (``plx.aggregate``
    and its other modes)."""
    entry = table.get(TOOL_NAME)
    if entry is None:
        return None
    tool = WhoKnowsTool(base)
    before = entry["handler"]

    def handler(arguments: Any, actor: Any) -> dict[str, Any]:
        if isinstance(arguments, dict) and arguments.get("mode") == MODE:
            return tool.call(arguments, actor)
        return before(arguments, actor)
    table[TOOL_NAME] = dict(entry, handler=handler)
    return tool


__all__ = ["FORMAT", "MODE", "TOOL_NAME", "WhoKnowsTool", "answer", "register",
           "waiting_of"]
