"""How much of each person's day the autopilot does: every action a person took in a past
window, in every system that captured it, against the items the autopilot's replay
(``operating.autopilot_replay``) prepared for that person over the same days.

* **Actions** (``ACTIONS``): the replay's providers (mail and Teams messages sent, MC
  approvals decided and task events) plus the system activity the store holds a person
  for: Planner tasks created or completed (``created_by`` / ``completed_by``, a person's
  id), calendar events organized (the organizer's address, the organizer's own calendar
  copy), SharePoint list items created or edited (the editor's address), Sage entries
  keyed (``enteredby``: a Sage user code, resolved only when it names exactly one person,
  ``user_code_person``) and FileMaker QC results (``tested_by`` / ``checked_by``: a first
  name, the same rule). Every action has a ``kind`` (its system and verb) and a
  ``workflow`` (``operating.workflows``) and is weighed in minutes (``MINUTES``; a meeting
  by its length).
* **Unattributed**: record activity whose system holds no who-did-what field (BC ledger
  entries, Portal and Sage orders, shipments and invoices, FileMaker production records,
  SharePoint files), or whose who-value names nobody or several people: counted per system
  and kind, never given to a person.
* **Coverage**: an action is covered when an item the replay prepared for its person
  anticipated it (``autopilot_replay.score``: the same conversation or record, after the
  trigger, within the horizon). Meetings attended are listed and never counted as
  preparable work. Mail that took its sender no time (``not_human``: automated, a template
  sent again and again, a scheduled send) is counted per reason and left out; MC events an
  agent runtime raised never reach here (``autopilot_replay.task_event``).

Read-only on the store and the replay's queues; the result holds counts, kinds and
workflow names, never names, addresses or text. People pass the exclusion first
(``autopilot_replay.members``: an excluded or undecidable person is never counted).

Run::

    python3 -m operating.day_coverage --store STORE --replay DIR --role ROLE \\
        --since 2026-10-20T07:00:00Z --until 2026-10-23T07:00:00Z --out FILE \\
        [--source postgres]
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

from . import autopilot as ap
from . import autopilot_replay as rp

SCHEMA = "plx-day-coverage/v1"
CHUNK = 5000

# minutes one action takes the person (the replay's, plus each system's entry work)
MINUTES = {**rp.MINUTES, "planner_created": 3, "planner_completed": 5,
           "meeting_organized": 10, "sharepoint_edit": 3, "sage_gl_journal": 10,
           "sage_ar_invoice": 6, "sage_ar_receipt": 6, "sage_ap_invoice": 6,
           "sage_bank_entry": 5, "sage_oe_shipment": 5, "sage_ap_payment": 4,
           "sage_ic_adjustment": 5, "sage_po_receipt": 5,
           "qc_test_entered": 10, "qc_release_checked": 5}

# Sage tables a person keys entries into: type -> (kind, date property, workflow)
SAGE_ENTRIES = {"RelSageGlJeHeader": ("sage_gl_journal", "dateentry", "finance_journal"),
                "RelSageArIbh": ("sage_ar_invoice", "audtdate", "ar_invoice_send"),
                "RelSageArTcr": ("sage_ar_receipt", "audtdate", "cash_remittance"),
                "RelSageApIbh": ("sage_ap_invoice", "audtdate", "ap_invoice_entry")}
GL_HEADER = "RelSageGlJeHeader"
# a GL header's (source ledger, source type) -> (kind, workflow); the rest (an AR or AP
# invoice or receipt) is the subledger entry its own table counts
GL_SOURCES = {("GL", "JE"): ("sage_gl_journal", "finance_journal"),
              ("BK", "EN"): ("sage_bank_entry", "finance_journal"),
              ("OE", "SH"): ("sage_oe_shipment", "shipping_freight"),
              ("AP", "PY"): ("sage_ap_payment", "ap_payment"),
              ("IC", "AS"): ("sage_ic_adjustment", "inventory_adjustment"),
              ("IC", "TF"): ("sage_ic_adjustment", "inventory_adjustment"),
              ("PO", "RC"): ("sage_po_receipt", "supplier_eta_receiving")}
# FileMaker QC: the batch's event -> (kind, who property)
QC_EVENTS = {"Batch.tested": ("qc_test_entered", "tested_by"),
             "Batch.released": ("qc_release_checked", "checked_by")}
# store events of systems with no who-did-what field: counted, never attributed
UNATTRIBUTED_EVENTS = {
    "GeneralLedgerEntry.posted": "bc", "ItemLedgerEntry.posted": "bc",
    "SalesOrder.ordered": "sage", "Shipment.sage_shipped": "sage",
    "SalesInvoice.invoiced": "sage", "PurchaseOrder.sage_ordered": "sage",
    "Receipt.sage_received": "sage", "SalesOrder.created": "portal",
    "SalesOrder.required": "portal", "PurchaseOrder.created": "filemaker",
    "AssemblyOrder.created": "filemaker", "AssemblyOrder.scheduled": "filemaker",
    "AssemblyOrder.allocated": "filemaker", "AssemblyOrder.committed": "filemaker",
    "AssemblyOrder.manufactured": "filemaker", "AssemblyOrder.realized": "filemaker",
    "AssemblyOrder.cancelled": "filemaker", "Batch.scheduled": "filemaker",
    "Batch.manufactured": "filemaker", "Batch.realized": "filemaker",
    "BatchSchedule.starts": "filemaker", "SalesOrder.fm_required": "filemaker"}
SAGE_DAY = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
NOT_WORK = frozenset({"meeting_attended"})


class _Code:
    """Sage user codes and FileMaker first names -> one person, from the directory's
    address local parts (``first.last``, ``firstl``, ``first``; a first-name-only address
    also ``first`` + one letter, a code's last initial): exactly one person, else nobody
    (two people sharing the code, or none, is never guessed)."""

    def __init__(self, directory: Mapping[str, str], domains: Iterable[str]) -> None:
        home = {str(d).lower() for d in domains}
        keys: dict[str, set[str]] = defaultdict(set)
        initial: dict[str, set[str]] = defaultdict(set)
        for address, oid in directory.items():
            if "@" not in address or (home and address.rsplit("@", 1)[1] not in home):
                continue
            local = address.split("@", 1)[0].lower()
            parts = [p for p in re.split(r"[._-]+", local) if p]
            if not parts or not parts[0].isalpha():
                continue
            keys[local.replace(".", "")].add(oid)
            keys[parts[0]].add(oid)
            if len(parts) == 1:
                initial[parts[0]].add(oid)
            if len(parts) > 1 and parts[1][:1].isalpha():
                keys[parts[0] + parts[1][0]].add(oid)
        self.keys = {k: next(iter(v)) for k, v in keys.items() if len(v) == 1}
        self.shared = {k for k, v in keys.items() if len(v) > 1}
        self.initial = {k: next(iter(v)) for k, v in initial.items()
                        if len(keys[k]) == 1 and len(v) == 1}

    def __call__(self, code: Any) -> str | None:
        key = re.sub(r"[^a-z]", "", str(code or "").strip().lower())
        if not key or key in self.shared:
            return None
        if key in self.keys:
            return self.keys[key]
        # ``first`` + a last initial: the first name names exactly one person
        return self.initial.get(key[:-1]) if len(key) >= 4 else None


def user_code_person(code: Any, directory: Mapping[str, str],
                     domains: Iterable[str]) -> str | None:
    return _Code(directory, domains)(code)


def _ids(st: Any, otype: str) -> list[str]:
    from ontology import store as ostore
    return [r[0] for r in st.conn.execute(
        "SELECT o.object_id FROM objects o WHERE o.object_type = ? AND NOT "
        + ostore.removed_at_sql("o.object_id"), (otype,))]


def _scan(st: Any, otype: str, props: Sequence[str]) -> Iterable[tuple[str, dict[str, Any]]]:
    ids = _ids(st, otype)
    for start in range(0, len(ids), CHUNK):
        yield from ap._read(st, ids[start:start + CHUNK], props).items()


def _address(raw: str) -> str:
    try:
        got = json.loads(raw)
    except ValueError:
        return raw.strip().lower() if "@" in raw else ""
    if isinstance(got, Mapping):
        user = got.get("user") if isinstance(got.get("user"), Mapping) else got
        return str(user.get("email") or user.get("address") or "").lower()
    return ""


def _in(at: datetime | None, lo: datetime, hi: datetime) -> bool:
    return at is not None and lo <= at < hi


def _sage_day(raw: str) -> datetime | None:
    m = SAGE_DAY.match(raw.strip())
    if not m:
        return None
    try:
        # a Sage date is a business day: noon UTC keeps it on its calendar day
        return datetime(int(m[1]), int(m[2]), int(m[3]), 12, tzinfo=timezone.utc)
    except ValueError:
        return None


def _sources(st: Any, people: Any, since: str, until: str) -> dict[str, Any]:
    """Each system's activity in [since, until) for everyone: source -> a function giving
    ``(person or None, at, kind, workflow, records, minutes or None)`` rows."""
    directory, addresses_of, domains = ap._people_maps(people)
    code = _Code(directory, domains)
    lo, hi = ap._instant(since), ap._instant(until)

    def planner() -> Iterable[tuple]:
        for oid, r in _scan(st, "PlannerTask", ("created_at", "created_by", "completed_at",
                                                "completed_by")):
            for verb in ("created", "completed"):
                at = ap._instant(ap._v(r, f"{verb}_at"))
                if _in(at, lo, hi):
                    by = ap._v(r, f"{verb}_by").lower()
                    yield (by if by in addresses_of else None, at, f"planner_{verb}",
                           "task_followthrough", [oid], None)

    def calendar() -> Iterable[tuple]:
        for oid, r in _scan(st, "CalendarEvent", ("start", "end", "is_organizer",
                                                  "is_cancelled", "mailbox")):
            start, end = ap._instant(ap._v(r, "start")), ap._instant(ap._v(r, "end"))
            if not _in(start, lo, hi) or ap._v(r, "is_cancelled").lower() == "true":
                continue
            box = ap._v(r, "mailbox").lower()
            person = directory.get(box, box if box in addresses_of else None)
            length = int((end - start).total_seconds() // 60) if end and end > start else 30
            if ap._v(r, "is_organizer").lower() == "true":
                yield (person, start, "meeting_organized", "meeting", [oid], None)
            yield (person, start, "meeting_attended", "meeting", [oid], min(length, 480))

    def sharepoint() -> Iterable[tuple]:
        for oid, r in _scan(st, "SharePointListItem", ("created_date_time", "created_by",
                                                       "last_modified_date_time",
                                                       "last_modified_by")):
            seen = set()
            for t, b in (("created_date_time", "created_by"),
                         ("last_modified_date_time", "last_modified_by")):
                at = ap._instant(ap._v(r, t))
                if not _in(at, lo, hi) or at in seen:
                    continue
                seen.add(at)
                address = _address(ap._v(r, b))
                yield (directory.get(address) if address else None, at, "sharepoint_edit",
                       "shared_list_upkeep", [oid], None)

    def sage() -> Iterable[tuple]:
        for otype, (kind, prop, flow) in SAGE_ENTRIES.items():
            for oid, r in _scan(st, otype, (prop, "enteredby", "srceledger", "srcetype")):
                at = _sage_day(ap._v(r, prop))
                if not _in(at, lo, hi):
                    continue
                if otype == GL_HEADER:
                    # a GL entry mirrors its subledger's posting: only what no other
                    # table counts (a journal, a bank entry, a shipment, a receipt...)
                    got = GL_SOURCES.get((ap._v(r, "srceledger").strip().upper(),
                                          ap._v(r, "srcetype").strip().upper()))
                    if got is None:
                        continue
                    kind, flow = got
                yield (code(ap._v(r, "enteredby")), at, kind, flow, [oid], None)

    def qc() -> Iterable[tuple]:
        for etype, (kind, prop) in QC_EVENTS.items():
            rows = st.conn.execute(
                "SELECT e.time, eo.object_id FROM events e JOIN event_objects eo ON "
                "eo.event_id = e.event_id WHERE e.event_type = ? AND e.time >= ? AND "
                "e.time < ?", (etype, since, until)).fetchall()
            got = ap._read(st, [o for _t, o in rows], (prop,))
            for t, oid in rows:
                yield (code(ap._v(got.get(oid), prop)), ap._instant(t), kind, "qc_release",
                       [oid], None)

    def unattributed() -> Iterable[tuple]:
        for etype, n in st.conn.execute(
                "SELECT event_type, count(*) FROM events WHERE event_type IN (SELECT value "
                "FROM json_each(?)) AND time >= ? AND time < ? GROUP BY event_type",
                (json.dumps(sorted(UNATTRIBUTED_EVENTS)), since, until)):
            yield ("-", None, etype, UNATTRIBUTED_EVENTS[etype], [], n)
        n = sum(1 for _o, r in _scan(st, "SharePointFile", ("last_modified",))
                if _in(ap._instant(ap._v(r, "last_modified")), lo, hi))
        yield ("-", None, "SharePointFile.modified", "sharepoint", [], n)

    return {"planner": planner, "calendar": calendar, "sharepoint": sharepoint,
            "sage": sage, "filemaker": qc, "unattributed": unattributed}


def _rows(people: Any, rows: Iterable[tuple]) -> list[list[Any]]:
    """A source's rows as kept: a person the exclusion does not clear is dropped whole
    (never counted, attributed or kept)."""
    return [[p, ap._iso(at) if at else None, k, f, r, m] for p, at, k, f, r, m in rows
            if p in (None, "-") or people.identity_clear(p)]


def system_actions(st: Any, people: Any, who: set[str], since: str, until: str, *,
                   cache: Path | None = None) -> tuple[list[dict[str, Any]], Counter]:
    """The system activity the store attributes to members of ``who`` in [since, until),
    and what it could not attribute (``(system, kind, reason)`` -> count). ``cache``: each
    source's rows for a window are kept there (ids, kinds and times only) and read back for
    any window inside it, so a run cut short resumes and every role reads each source once
    (the unattributed counts only for the same window)."""
    out: list[dict[str, Any]] = []
    lost: Counter = Counter()
    span = (since, until)
    for system, source in _sources(st, people, since, until).items():
        path = cache / f"{system}.json" if cache else None
        kept = json.loads(path.read_text()) if path is not None and path.exists() else None
        if kept and kept["since"] <= since and until <= kept["until"] and (
                system != "unattributed" or (kept["since"], kept["until"]) == span):
            rows = [r for r in kept["rows"] if r[1] is None or since <= r[1] < until]
        else:
            rows = _rows(people, source())
            if path is not None:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"since": since, "until": until, "rows": rows}))
                tmp.replace(path)
        for person, at, kind, flow, recs, minutes in rows:
            if person == "-":
                lost[(flow, kind, "no_who_field")] += minutes
            elif person is None:
                lost[(system, kind, "who_not_one_person")] += 1
            elif person in who:
                a = {"person": person, "at": at, "kind": kind, "workflow": flow,
                     "conversation": None, "to": [], "records": recs, "system": system}
                if minutes is not None:
                    a["minutes"] = minutes
                out.append(a)
    return out, lost


def _minutes(a: Mapping[str, Any]) -> int:
    return int(a.get("minutes") or MINUTES.get(a.get("kind") or "", 5))


def _type_of(a: Mapping[str, Any]) -> str:
    return f"{a.get('kind')}:{a.get('workflow') or 'unclassified'}"


def coverage(actions: Sequence[Mapping[str, Any]], anticipated: set[int], *,
             since: datetime, until: datetime) -> dict[str, Any]:
    """Per action type (kind:workflow): actions, covered, hours a week, covered hours a
    week, people and active person-days; work (everything but meetings attended) totals."""
    weeks = Decimal(max((until - since).total_seconds(), 1)) / Decimal(7 * 86400)
    rows: dict[str, Counter] = defaultdict(Counter)
    days: dict[str, set] = defaultdict(set)
    people: dict[str, set] = defaultdict(set)
    for a in actions:
        at = ap._instant(a.get("at"))
        if not _in(at, since, until):
            continue
        t = _type_of(a)
        hit = id(a) in anticipated
        rows[t]["actions"] += 1
        rows[t]["covered"] += hit
        rows[t]["minutes"] += _minutes(a)
        rows[t]["covered_minutes"] += _minutes(a) if hit else 0
        days[t].add((a["person"], at.date()))
        people[t].add(a["person"])

    def hours(m: int) -> str:
        return str((Decimal(m) / 60 / weeks).quantize(Decimal("0.1")))

    def share(a: int, b: int) -> str | None:
        return str((Decimal(a) / b).quantize(Decimal("0.001"))) if b else None

    types = {}
    total: Counter = Counter()
    for t, c in rows.items():
        types[t] = {"actions": c["actions"], "covered": c["covered"],
                    "hours_week": hours(c["minutes"]),
                    "covered_hours_week": hours(c["covered_minutes"]),
                    "people": len(people[t]), "person_days": len(days[t]),
                    "work": t.split(":", 1)[0] not in NOT_WORK}
        if types[t]["work"]:
            total.update(c)
    ranked = sorted(types, key=lambda t: (-Decimal(types[t]["hours_week"]) +
                                          Decimal(types[t]["covered_hours_week"]), t))
    return {"weeks": str(weeks.quantize(Decimal("0.01"))), "types": {t: types[t] for t in ranked},
            "work": {"actions": total["actions"], "covered": total["covered"],
                     "count_share": share(total["covered"], total["actions"]),
                     "hours_week": hours(total["minutes"]),
                     "covered_hours_week": hours(total["covered_minutes"]),
                     "hours_share": share(total["covered_minutes"], total["minutes"])}}


# a body sent this often unchanged is a template a system sends (or one click sends); a
# subject sent at the same minute on this many days is a scheduled send
TEMPLATE_MIN = 5
SCHEDULED_MIN_DAYS = 3
SHAPE_LOOKBACK = timedelta(days=14)     # the sent mail a template or schedule is told from


def not_human(actions: Sequence[Mapping[str, Any]],
              history: Sequence[Mapping[str, Any]] | None = None) -> dict[int, str]:
    """The sent mail that took its sender no time (``id(action)`` -> reason): automated
    (``autopilot_replay.sent_shape``), one body sent unchanged ``TEMPLATE_MIN`` times or
    more (``system_generated``), one subject sent at the same minute of the day on
    ``SCHEDULED_MIN_DAYS`` days or more (``scheduled_send``). Per person, over the actions
    given, counted over ``history`` when given (the person's sent mail over a span holding
    the actions': a daily notice is a template in a three-day window too); agent activity
    never reaches here (``autopilot_replay.task_event``)."""
    mail = [a for a in actions if a.get("kind") == "mail_sent"]
    seen = [a for a in (mail if history is None else history) if a.get("kind") == "mail_sent"]
    bodies: Counter = Counter((a["person"], a["body_key"]) for a in seen if a.get("body_key"))
    minutes: dict[tuple, set] = defaultdict(set)
    for a in seen:
        at = ap._instant(a.get("at"))
        if a.get("subject_key") and at is not None:
            minutes[(a["person"], a["subject_key"], at.strftime("%H:%M"))].add(at.date())
    out: dict[int, str] = {}
    for a in mail:
        at = ap._instant(a.get("at"))
        if a.get("automated") or (a.get("body_key") and
                                  bodies[(a["person"], a["body_key"])] >= TEMPLATE_MIN):
            out[id(a)] = "system_generated"
        elif at is not None and a.get("subject_key") and len(minutes[(
                a["person"], a["subject_key"], at.strftime("%H:%M"))]) >= SCHEDULED_MIN_DAYS:
            out[id(a)] = "scheduled_send"
    return out


def _provided(provider: Any, st: Any, people: Any, who: set[str], since: str, until: str,
              cache: Path | None) -> list[dict[str, Any]]:
    """A replay provider's actions of ``who`` in [since, until): from the cache when it
    holds a run of that provider over a covering window for a covering set of people."""
    path = cache / f"replay-{getattr(provider, '__name__', '')}.json" if cache else None
    kept = json.loads(path.read_text()) if path is not None and path.exists() else None
    if kept and kept["since"] <= since and until <= kept["until"] and who <= set(kept["who"]):
        lo, hi = ap._instant(since), ap._instant(until)
        return [a for a in kept["rows"]
                if a["person"] in who and _in(ap._instant(a.get("at")), lo, hi)]
    rows = provider(st, people, who, since, until)
    if path is not None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"since": since, "until": until, "who": sorted(who),
                                   "rows": rows}))
        tmp.replace(path)
    return rows


def measure(st: Any, people: Any, *, role: str, replay_dir: Path, since: datetime,
            until: datetime, roles: Mapping[str, list[str]],
            config: Mapping[str, Any], cache: Path | None = None) -> dict[str, Any]:
    """One role's (or person's) day coverage over [since, until), from its replay's queue."""
    if people is None or getattr(people, "unavailable", None):
        return {"schema": SCHEMA, "role": role, "status": "blocked",
                "reason": "people_exclusion_unavailable"}
    slug = re.sub(r"[^a-z0-9_]+", "_", role.lower())[:60]
    root = replay_dir / slug
    saved = json.loads((root / ap.ROLES_FILE).read_text()) if (root / ap.ROLES_FILE).exists() \
        else {"roles": {}}
    who = set(saved["roles"].get(role) or ()) if not role.startswith("person:") else set()
    who = set(rp.members(st, people, role, {**roles, **({role: sorted(who)} if who else {})},
                         until=ap._iso(since)))
    if not who:
        return {"schema": SCHEMA, "role": role, "status": "no_members"}
    queue = ap.Queue(root / ap.QUEUE_FILE)
    try:
        items = queue.items(sorted(who), ("open", "done", "settled", "dismissed"))
        events = {}
        ev_ids = sorted({i["event_id"] for i in items})
        for start in range(0, len(ev_ids), 500):
            for eid, body in queue.conn.execute(
                    "SELECT event_id, body FROM events WHERE event_id IN (SELECT value FROM "
                    "json_each(?))", (json.dumps(ev_ids[start:start + 500]),)):
                events[eid] = json.loads(body)
    finally:
        queue.close()
    closes = rp.answer_end(until, config["horizon"])
    last = rp.data_end(st, ap._iso(closes))
    a_until = ap._iso(min(closes, last))
    actions: list[dict[str, Any]] = []
    for provider in config["providers"]:
        actions += [{**a, "system": "mail" if a["kind"] == "mail_sent" else
                     "teams" if a["kind"] == "teams_sent" else "mission_control"}
                    for a in _provided(provider, st, people, who, ap._iso(since), a_until,
                                       cache)]
    sys_actions, lost = system_actions(st, people, who, ap._iso(since), ap._iso(until),
                                      cache=cache)
    actions += sys_actions
    history = None
    if rp.mail_sent in config["providers"]:
        history = _provided(rp.mail_sent, st, people, who, ap._iso(since - SHAPE_LOOKBACK),
                            a_until, cache)
    if any(a.get("kind") == "mail_sent" and "automated" not in a for a in actions + (
            history or [])):
        # a provider cache kept before the mail's shape was read: never counted as people's
        return {"schema": SCHEMA, "role": role, "status": "blocked",
                "reason": "mail_shape_missing_in_cache"}
    dropped = not_human(actions, history)
    excluded = Counter(dropped[id(a)] for a in actions
                       if id(a) in dropped and _in(ap._instant(a.get("at")), since, until))
    actions = [a for a in actions if id(a) not in dropped]
    anticipated: set[int] = set()
    rp.score(items, events, actions, horizon=config["horizon"], data_end=last, since=since,
             until=until, anticipated=anticipated)
    by_system: Counter = Counter()
    for a in actions:
        if _in(ap._instant(a.get("at")), since, until):
            by_system[a["system"]] += 1
    return {"schema": SCHEMA, "role": role, "status": "complete", "members": len(who),
            "window": [ap._iso(since), ap._iso(until)], "items": len(items),
            "attributed_by_system": dict(sorted(by_system.items())),
            "excluded_not_human": dict(sorted(excluded.items())),
            "unattributed": [{"system": s, "kind": k, "reason": r, "count": n}
                             for (s, k, r), n in sorted(lost.items())],
            **coverage(actions, anticipated, since=since, until=until)}


def main(argv: Sequence[str] | None = None) -> int:
    from . import proactive_engine as pe
    p = argparse.ArgumentParser(prog="python3 -m operating.day_coverage")
    p.add_argument("--store", required=True)
    p.add_argument("--replay", required=True, help="the replay's --out directory")
    p.add_argument("--role", required=True)
    p.add_argument("--since", required=True)
    p.add_argument("--until", required=True)
    p.add_argument("--roles-file", default=None)
    p.add_argument("--capture-state", default=None)
    p.add_argument("--out", required=True)
    p.add_argument("--cache", default=None, help="a directory keeping each source's rows")
    p.add_argument("--source", choices=ap.STORE_SOURCES, default=None,
                   help="as the replay's --source: the SQLite generation at --store, or the "
                        "Postgres ontology (a fresh window is counted on the data it was "
                        "replayed on); refused when Postgres does not hold that generation")
    a = p.parse_args(argv)
    from ontology import store as ostore
    store = Path(a.store)
    try:
        st = ap._store(a.store, a.source)
    except ostore.StoreError as error:
        print(json.dumps({"schema": SCHEMA, "role": a.role, "status": "blocked",
                          "reason": error.code}))
        return 2
    people = pe._people(store)
    if a.capture_state and (Path(a.capture_state) / "mc-capture" / "capture.sqlite").exists():
        from . import autopilot_work as aw
        st.autopilot_mc = aw.MCQuestions(a.capture_state)
    try:
        roles = ap._roles({ap.ROLES_ENV: a.roles_file} if a.roles_file else {},
                          Path(a.replay))
        result = measure(st, people, role=a.role, replay_dir=Path(a.replay),
                         since=ap._instant(a.since), until=ap._instant(a.until), roles=roles,
                         config=rp.load_config(),
                         cache=Path(a.cache) if a.cache else None)
    finally:
        st.close()
    Path(a.out).write_text(json.dumps(result, indent=1, sort_keys=True))
    print(json.dumps({k: result.get(k) for k in ("role", "status", "members", "work")}))
    return 0 if result.get("status") == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
