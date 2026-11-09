"""Who does what in each captured system: actor resolution and per-person activity.

Every captured system records some of its users: Business Central rows (``systemCreatedBy``
/ ``systemModifiedBy`` user security ids, when the API carries them), the Portal mirror
(``app_*`` columns named ``<verb>_by`` / ``<verb>_by_id``), Sage headers (``enteredby``),
SharePoint files (the last modifier; an author when captured), Mission Control (task event
actors, approval requesters and deciders), Planner (creator, assignees, completer) and
calendars (organiser, attendees). This module maps those values to people (Entra object
ids) through the directory only, folds the actions into weekly counts per person, system
and workflow, and counts the hand-offs between systems on the same business record.

Identity is conservative. A value resolves only when it IS an object id the directory
lists, an address the directory maps to one object id, or a system user id the owner's
alias file (``ALIASES_FILE``, outside git, keyed by system) maps to a listed object id.
Anything else stays unresolved; a unique match of the value to one address's local part is
recorded only as a candidate count for the owner to confirm, never attributed. A person
the people exclusion does not clear (fail closed: an unavailable guard clears nobody) is
counted only as ``withheld`` in the system's coverage and appears nowhere else, in no
chain and in no hand-off. Actors the system marks as agents count as ``non_person`` and
addresses outside every directory domain as ``external``; neither is in a coverage share.

No row content leaves here: counts, ISO weeks, system and workflow names and object ids.
Read-only: every store is opened ``mode=ro``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional

SCHEMA = "plx-system-activity/v1"
ALIASES_SCHEMA = "plx-system-actors/v1"
SNAPSHOT_FILE = "system-activity.json"
ALIASES_FILE = "system-actors.json"
SNAPSHOT_MAX_AGE = timedelta(days=8)
WINDOW_WEEKS = 26
HANDOFF_MAX_DAYS = 120

SYSTEMS = ("bc", "portal", "sage", "sharepoint", "mission_control", "planner", "calendar")
LABELS = {"bc": "Business Central", "portal": "the Portal", "sage": "Sage",
          "sharepoint": "SharePoint", "mission_control": "Mission Control",
          "planner": "Planner", "calendar": "calendars"}

# A record kind's workflow: first pattern that matches ``<system>:<kind>`` wins.
WORKFLOWS: tuple[tuple[str, str], ...] = (
    (r"approval", "approvals"),
    (r"purchase|:po_|:ap_|vendor|supplier|receipt|requisition", "procure_to_pay"),
    (r"sales|:oe_|:ar_|customer|quote|shipment|ship_release", "order_to_cash"),
    (r":gl_|generalledger|account|journal|payment|currency", "finance"),
    (r"manufactur|assembly|batch|production|equipment|itemledger", "production"),
    (r"product|item|unitsofmeasure|fg_item|manufacturer|location", "item_master"),
    (r"^planner:|^mission_control:task", "tasks"),
    (r"^calendar:", "meetings"),
    (r"^sharepoint:", "documents"),
)
# The business number a record carries (joined across systems within one workflow).
REF_FIELDS = ("po_number", "ponumber", "orderNumber", "order_number", "ordnumber", "number")

_OID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VERB_COL = re.compile(r"^(?:(\w+?)_by(?:_id)?|entered_by|enteredby)$")


def workflow_of(system: str, kind: str) -> str:
    key = f"{system}:{kind}".lower()
    return next((wf for pattern, wf in WORKFLOWS if re.search(pattern, key)), "other")


def week_of(day: date) -> str:
    return (day - timedelta(days=day.weekday())).isoformat()


def day_of(value: Any) -> Optional[date]:
    """The day of an ISO timestamp or a Sage ``YYYYMMDD`` date; None when it is neither."""
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class Action:
    """One thing a user did in a system: ``actor`` as the system recorded it (None when the
    record names nobody), ``ref`` the business number it is about (or None)."""
    system: str
    kind: str
    verb: str
    day: date
    actor: Optional[str] = None
    ref: Optional[str] = None
    non_person: bool = False


# --------------------------------------------------------------------------- directory

class Directory:
    """People resolution: ``addresses`` maps lower-case addresses and UPNs to object ids
    (the directory the mail capture recorded); ``aliases`` maps, per system, a system user
    id to an object id (the owner's alias file; an entry whose id the directory does not
    list is dropped)."""

    def __init__(self, addresses: Mapping[str, str],
                 aliases: Optional[Mapping[str, Mapping[str, str]]] = None) -> None:
        self.addresses = {str(a).strip().lower(): str(o).lower()
                          for a, o in addresses.items() if _OID.match(str(o).lower())}
        self.oids = frozenset(self.addresses.values())
        self.aliases: dict[str, dict[str, str]] = {}
        for system, table in (aliases or {}).items():
            if system in SYSTEMS and isinstance(table, Mapping):
                self.aliases[system] = {_norm(k): str(v).lower() for k, v in table.items()
                                        if str(v).lower() in self.oids and _norm(k)}
        local: dict[str, set[str]] = defaultdict(set)
        for address, oid in self.addresses.items():
            local[_norm(address.split("@")[0])].add(oid)
        self._local = {k: next(iter(v)) for k, v in local.items() if len(v) == 1 and k}
        self.domains = frozenset(a.rsplit("@", 1)[1] for a in self.addresses if "@" in a)

    def resolve(self, system: str, value: Any) -> tuple[Optional[str], str]:
        """(object id or None, basis): ``object_id``, ``address`` or ``alias`` when
        resolved; ``external`` for an address in a domain the directory holds none in (an
        outside party, not a colleague); ``empty`` or ``unresolved`` otherwise."""
        if not isinstance(value, str) or not value.strip():
            return None, "empty"
        lowered = value.strip().lower()
        if _OID.match(lowered):
            if lowered in self.oids:
                return lowered, "object_id"
        elif "@" in lowered:
            found = self.addresses.get(lowered)
            if found:
                return found, "address"
            if self.domains and lowered.rsplit("@", 1)[1] not in self.domains:
                return None, "external"
        alias = self.aliases.get(system, {}).get(_norm(lowered))
        if alias:
            return alias, "alias"
        return None, "unresolved"

    def candidate(self, value: Any) -> Optional[str]:
        """The one directory person whose address local part equals ``value`` (a suggestion
        for the alias file, never an attribution)."""
        if not isinstance(value, str) or "@" in value or _OID.match(value.strip().lower()):
            return None
        return self._local.get(_norm(value))


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def load_aliases(path: Optional[Path]) -> dict[str, dict[str, str]]:
    """The owner's alias file (``{"schema": ALIASES_SCHEMA, "aliases": {system: {id: oid}}}``);
    empty when absent or malformed (nothing is then resolved by alias)."""
    if path is None or not Path(path).is_file():
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, Mapping) or data.get("schema") != ALIASES_SCHEMA \
            or not isinstance(data.get("aliases"), Mapping):
        return {}
    return {k: dict(v) for k, v in data["aliases"].items() if isinstance(v, Mapping)}


def directory_from_state(state_dir: Path, aliases: Optional[Path] = None) -> Directory:
    """The directory the mail capture recorded (its mailboxes and its last tenant listing,
    read-only, within the people exclusion's age bound)."""
    from integrations.people_exclusion import mail_capture_directory, mail_capture_listing
    addresses: dict[str, str] = {}
    for oid, upn, identities in mail_capture_directory(state_dir) or ():
        for value in (upn, *identities):
            if isinstance(value, str) and "@" in value:
                addresses[value.lower()] = oid
    for address, oid, kind in mail_capture_listing(state_dir) or ():
        if kind == "user" and oid:
            addresses.setdefault(address.lower(), oid)
    return Directory(addresses, load_aliases(aliases))


# --------------------------------------------------------------------------- folding

Key = tuple[str, str, str, str, Optional[str], bool]   # system, kind, verb, week, actor, agent
Link = tuple[str, str, str, str, Optional[str]]        # workflow, ref, day, system, actor


def fold(actions: Iterable[Action], since: date, until: date
         ) -> tuple[Counter, list[Link]]:
    """Counts per (system, kind, verb, week, actor, non_person) of the actions dated in
    [since, until], and the dated links of those that carry a business number."""
    counts: Counter = Counter()
    links: list[Link] = []
    for a in actions:
        if not since <= a.day <= until:
            continue
        counts[(a.system, a.kind, a.verb, week_of(a.day), a.actor, a.non_person)] += 1
        if a.ref:
            links.append((workflow_of(a.system, a.kind), a.ref, a.day.isoformat(),
                          a.system, a.actor))
    return counts, links


def activity(counts: Mapping[Key, int], links: Iterable[Link], directory: Directory,
             excluded: Callable[[str], bool], *, since: date, until: date) -> dict[str, Any]:
    """The snapshot: coverage per system, each cleared person's weekly counts per system,
    their workflows and hand-offs, and the system-to-system hand-offs on shared records."""
    cover: dict[str, Counter] = {s: Counter() for s in SYSTEMS}
    people: dict[str, dict[str, Any]] = {}
    resolved: dict[tuple[str, Optional[str]], tuple[Optional[str], str]] = {}

    def person(system: str, actor: Optional[str]) -> tuple[Optional[str], str]:
        key = (system, actor)
        if key not in resolved:
            oid, basis = directory.resolve(system, actor)
            if oid is not None and excluded(oid):
                oid, basis = None, "withheld"
            elif oid is None and basis == "unresolved" and directory.candidate(actor):
                basis = "candidate"
            resolved[key] = (oid, basis)
        return resolved[key]

    def row(oid: str) -> dict[str, Any]:
        return people.setdefault(oid, {"systems": {}, "weeks": {}, "workflows": Counter(),
                                       "handoffs_out": Counter(), "handoffs_in": Counter()})

    for (system, kind, verb, week, actor, agent), n in counts.items():
        c = cover.setdefault(system, Counter())
        c["actions"] += n
        if agent:
            c["non_person"] += n
            continue
        oid, basis = person(system, actor)
        if basis == "external":
            c["external"] += n
            continue
        if oid is None:
            c["withheld" if basis == "withheld" else
              "candidate" if basis == "candidate" else "unresolved"] += n
            continue
        c["attributed"] += n
        c[f"by_{basis}"] += n
        r = row(oid)
        r["systems"].setdefault(system, Counter())[verb] += n
        r["weeks"].setdefault(week, Counter())[system] += n
        r["workflows"][workflow_of(system, kind)] += n

    chains: dict[tuple[str, str], list[tuple[str, str, Optional[str]]]] = defaultdict(list)
    for wf, ref, day, system, actor in links:
        oid, basis = (None, "agent") if actor is None else person(system, actor)
        if basis == "withheld":
            continue   # an excluded person's step is in no chain
        chains[(wf, ref)].append((day, system, oid))
    edges: Counter = Counter()
    limit = timedelta(days=HANDOFF_MAX_DAYS)
    for (wf, _ref), steps in chains.items():
        steps.sort(key=lambda s: (s[0], s[1], s[2] or ""))
        for (d1, s1, p1), (d2, s2, p2) in zip(steps, steps[1:]):
            if s1 == s2 or date.fromisoformat(d2) - date.fromisoformat(d1) > limit:
                continue
            edges[(s1, s2, wf)] += 1
            if p1 and p2 and p1 != p2:
                row(p1)["handoffs_out"][f"{s1}>{s2}"] += 1
                row(p2)["handoffs_in"][f"{s1}>{s2}"] += 1

    coverage = []
    for system in sorted(cover, key=lambda s: SYSTEMS.index(s) if s in SYSTEMS else 99):
        c = cover[system]
        people_actions = c["actions"] - c["non_person"] - c["external"]
        coverage.append({
            "system": system, "actions": c["actions"], "attributed": c["attributed"],
            "non_person": c["non_person"], "external": c["external"],
            "withheld": c["withheld"],
            "unresolved": c["unresolved"], "candidate": c["candidate"],
            "by_object_id": c["by_object_id"], "by_address": c["by_address"],
            "by_alias": c["by_alias"],
            "share": round(c["attributed"] / people_actions, 4) if people_actions else 0.0})
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": {"from": since.isoformat(), "to": until.isoformat()},
        "coverage": coverage,
        "people": {oid: {"systems": {s: dict(v) for s, v in r["systems"].items()},
                         "weeks": {w: dict(v) for w, v in sorted(r["weeks"].items())},
                         "workflows": dict(r["workflows"]),
                         "handoffs_out": dict(r["handoffs_out"]),
                         "handoffs_in": dict(r["handoffs_in"])}
                   for oid, r in sorted(people.items())},
        "handoffs": [{"from": a, "to": b, "workflow": wf, "n": n}
                     for (a, b, wf), n in sorted(edges.items())]}


# --------------------------------------------------------------------------- owner view

def owner_columns(snapshot: Optional[Mapping[str, Any]], excluded: Callable[[str], bool], *,
                  today: date, days: int) -> tuple[dict[str, dict[str, int]], list, str]:
    """(per cleared person: actions per system in the last ``days``; coverage rows; the
    note naming exactly the systems covered). A missing, foreign or stale snapshot gives
    nothing and the mail-only note. The exclusion is re-checked here (fail closed)."""
    base = ("Floor and lab work, phone and paper are not seen, so these figures undercount "
            "that work and are not a measure of effort.")
    if not isinstance(snapshot, Mapping) or snapshot.get("schema") != SCHEMA:
        return {}, [], "Covers mail, Teams and tasks only. " + base
    try:
        made = datetime.fromisoformat(str(snapshot.get("generated_at")))
        if made.tzinfo is None or datetime.now(timezone.utc) - made > SNAPSHOT_MAX_AGE:
            return {}, [], "Covers mail, Teams and tasks only. " + base
    except ValueError:
        return {}, [], "Covers mail, Teams and tasks only. " + base
    since = week_of(today - timedelta(days=days))
    per: dict[str, dict[str, int]] = {}
    for oid, row in (snapshot.get("people") or {}).items():
        if not isinstance(oid, str) or not _OID.match(oid) or excluded(oid):
            continue
        n: Counter = Counter()
        for week, systems in (row.get("weeks") or {}).items():
            if str(week) >= since and isinstance(systems, Mapping):
                n.update({s: int(v) for s, v in systems.items() if s in SYSTEMS})
        if n:
            per[oid] = dict(n)
    coverage = [{"system": c["system"], "share": float(c.get("share") or 0.0),
                 "actions": int(c.get("actions") or 0)}
                for c in snapshot.get("coverage") or () if c.get("system") in SYSTEMS]
    covered = [f"{LABELS[c['system']]} ({round(c['share'] * 100)}% tied to a person)"
               for c in coverage if c["share"] > 0]
    unmatched = [LABELS[c["system"]] for c in coverage if c["share"] <= 0 and c["actions"]]
    seen = {c["system"] for c in coverage if c["actions"]}
    missing = [LABELS[s] for s in SYSTEMS if s not in seen]
    note = "Covers mail, Teams and tasks"
    note += (", and records created or changed in " + ", ".join(covered) if covered else
             " only")
    note += ". " + (f"Not tied to people: {', '.join(unmatched)} (their records name no user "
                    "we can match). " if unmatched else "")
    note += f"Not captured: {', '.join(missing)}. " if missing else ""
    return per, coverage, note + base


def load_snapshot(path: Optional[Path]) -> Optional[dict[str, Any]]:
    if path is None:
        return None
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- extractors

def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def _ref(row: Mapping[str, Any]) -> Optional[str]:
    for field in REF_FIELDS:
        value = row.get(field)
        if value not in (None, ""):
            text = re.sub(r"[^A-Z0-9]", "", str(value).upper())
            if len(text) >= 3:
                return text
    return None


def bc_actions(state: Path) -> Iterator[Action]:
    """Each captured BC record version: created / changed with the user the row names
    (``systemCreatedBy`` / ``systemModifiedBy``), else nobody."""
    path = state / "bc-capture" / "capture.sqlite"
    if not path.is_file():
        return
    conn = _ro(path)
    seen: set[tuple[str, str]] = set()
    try:
        for path_, payload in conn.execute("SELECT path, payload FROM versions"):
            data = json.loads(payload)
            row = data.get("row") or {}
            kind = (data.get("meta") or {}).get("entity_set") or "record"
            ref = _ref(row)
            made = day_of(row.get("systemCreatedAt"))
            if made and (path_, "") not in seen:
                seen.add((path_, ""))
                yield Action("bc", kind, "created", made, row.get("systemCreatedBy"), ref)
            stamp = str(row.get("systemModifiedAt") or row.get("lastModifiedDateTime") or "")
            changed = day_of(stamp)
            if changed and changed != made and (path_, stamp) not in seen:
                seen.add((path_, stamp))
                yield Action("bc", kind, "changed", changed, row.get("systemModifiedBy"), ref)
    finally:
        conn.close()


def mirror_relations(state: Path) -> list[tuple[str, list[str]]]:
    path = state / "case-runtime" / "source-mirror.sqlite"
    if not path.is_file():
        return []
    conn = _ro(path)
    try:
        out = []
        for relation, columns in conn.execute("SELECT relation, columns_json FROM "
                                              "relation_state"):
            if relation.startswith("public.app_") or relation.startswith("sage."):
                out.append((relation, [c if isinstance(c, str) else str(c.get("name"))
                                       for c in json.loads(columns)]))
        return out
    finally:
        conn.close()


def mirror_plan(relation: str, columns: list[str]) -> list[tuple[str, str, Optional[str]]]:
    """(verb, time column, actor column or None) per action a mirror row records. Portal:
    ``created`` at ``created_at``; each ``<verb>_by`` at ``<verb>_at`` / ``date_<verb>``;
    ``changed`` only where the row names its modifier (``updated_at`` alone is moved by
    the sync). Sage: headers entered (``enteredby``) at their entry or audit date; other
    ``*_header`` tables at their audit date, naming nobody."""
    cols = set(columns)
    plan: list[tuple[str, str, Optional[str]]] = []
    if relation.startswith("sage."):
        when = next((c for c in ("dateentry", "audtdate") if c in cols), None)
        if when and "enteredby" in cols:
            plan.append(("entered", when, "enteredby"))
        elif when and relation.endswith("_header"):
            plan.append(("entered", when, None))
        return plan
    actors = {}
    for c in columns:
        m = _VERB_COL.match(c)
        if m:
            verb = "entered" if c in ("entered_by", "enteredby") else m.group(1)
            actors.setdefault(verb, c)
    if "created_at" in cols:
        plan.append(("created", "created_at", actors.pop("created", None)))
    if "updated" in actors and "updated_at" in cols:
        plan.append(("changed", "updated_at", actors.pop("updated")))
    for verb, actor in sorted(actors.items()):
        when = next((c for c in (f"{verb}_at", f"date_{verb}", f"{verb}_date",
                                 "date_entered", "updated_at") if c in cols), None)
        if when:
            plan.append((verb, when, actor))
    return plan


def mirror_relation(args: tuple[str, str, list, str, str]) -> tuple[Counter, list[Link]]:
    """Fold one mirror relation (a worker: reads only the columns its plan names)."""
    state, relation, plan, since, until = args
    system = "sage" if relation.startswith("sage.") else "portal"
    kind = relation.split(".", 1)[1].removeprefix("app_")
    wanted = sorted({c for _v, t, a in plan for c in (t, a) if c} | set(REF_FIELDS))
    select = ", ".join(f"json_extract(row_json, '$.{c}')" for c in wanted)
    conn = _ro(Path(state) / "case-runtime" / "source-mirror.sqlite")

    def actions() -> Iterator[Action]:
        for values in conn.execute(f"SELECT {select} FROM row_current WHERE relation = ?",
                                   (relation,)):
            row = dict(zip(wanted, values))
            ref = _ref(row)
            for verb, when, actor in plan:
                day = day_of(row.get(when))
                if day:
                    who = row.get(actor) if actor else None
                    yield Action(system, kind, verb, day,
                                 who.strip() if isinstance(who, str) else
                                 (str(who) if who is not None else None), ref)
    try:
        return fold(actions(), date.fromisoformat(since), date.fromisoformat(until))
    finally:
        conn.close()


def sharepoint_actions(state: Path) -> Iterator[Action]:
    """Each captured file version: created (by its author when the capture has one, or by
    the modifier of a version unchanged since creation) and changed by its last modifier;
    one change per (file, modified time)."""
    path = state / "sharepoint-capture" / "capture.db"
    if not path.is_file():
        return
    conn = _ro(path)
    seen: set[tuple[str, str, str]] = set()
    try:
        for drive, item, record in conn.execute("SELECT drive_id, item_id, record FROM items"):
            rec = json.loads(record)
            made = day_of(rec.get("created_at"))
            stamp = str(rec.get("last_modified") or "")
            if made and (drive, item, "c") not in seen:
                seen.add((drive, item, "c"))
                # a version never changed since it was made: its modifier is its author
                author = rec.get("created_by") or (rec.get("last_modified_by")
                                                   if stamp == rec.get("created_at") else None)
                yield Action("sharepoint", "file", "created", made, author)
            changed = day_of(stamp)
            if changed and (drive, item, stamp) not in seen and stamp != rec.get("created_at"):
                seen.add((drive, item, stamp))
                yield Action("sharepoint", "file", "changed", changed,
                             rec.get("last_modified_by"))
    finally:
        conn.close()


def mission_control_actions(state: Path) -> Iterator[Action]:
    path = state / "mc-capture" / "capture.sqlite"
    if not path.is_file():
        return
    conn = _ro(path)
    try:
        for path_, payload in conn.execute("SELECT path, payload FROM versions WHERE "
                                           "superseded_at IS NULL"):
            row = json.loads(payload).get("row") or {}
            part = path_.split("/")
            kind = part[1] if len(part) > 1 else "record"
            if kind == "task_events":
                if str(row.get("kind") or "").startswith(("approval.requested",
                                                          "approval.decided")):
                    continue   # the approval record below counts it, with its actor
                day = day_of(row.get("ts"))
                actor = row.get("actor")
                extra = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
                marked = extra.get("actorKind")
                agent = (marked != "human" if marked else
                         isinstance(actor, str) and "@" not in actor)
                if day:
                    yield Action("mission_control", "task_event", str(row.get("kind") or
                                 "event"), day, actor, row.get("task_id"), agent)
            elif kind == "approvals":
                for verb, who, when in (("requested", "requestedBy", "requestedAt"),
                                        ("decided", "decidedBy", "decidedAt")):
                    day = day_of(row.get(when))
                    if day:
                        yield Action("mission_control", "approval", verb, day, row.get(who),
                                     row.get("task_id"))
    finally:
        conn.close()


def meetings_actions(state: Path) -> Iterator[Action]:
    """Planner: created, assigned, completed. Calendars (one event across the captured
    mailboxes counted once): organised at creation, attended at start (not declined)."""
    path = state / "meetings-capture" / "capture.db"
    if not path.is_file():
        return
    conn = _ro(path)
    seen: set[tuple[str, str]] = set()
    try:
        for kind, payload in conn.execute("SELECT kind, payload FROM versions WHERE "
                                          "superseded_at IS NULL"):
            row = json.loads(payload).get("row") or {}
            if kind == "planner_task":
                made = day_of(row.get("created_at"))
                if made:
                    yield Action("planner", "task", "created", made, row.get("created_by"))
                    for who in row.get("assignees") or ():
                        yield Action("planner", "task", "assigned", made, who)
                done = day_of(row.get("completed_at"))
                if done:
                    yield Action("planner", "task", "completed", done, row.get("completed_by"))
            elif kind == "calendar_event":
                key = (str(row.get("ical_uid") or row.get("id")), str(row.get("start")))
                if key in seen or row.get("is_cancelled"):
                    continue
                seen.add(key)
                organiser = row.get("organizer") or {}
                made, start = day_of(row.get("created_at")), day_of(row.get("start"))
                if made:
                    yield Action("calendar", "meeting", "organised", made,
                                 organiser.get("address"))
                for who in row.get("attendees") or ():
                    if start and isinstance(who, Mapping) and who.get("response") != "declined":
                        yield Action("calendar", "meeting", "attended", start,
                                     who.get("address"))
    finally:
        conn.close()


def _fold_source(args: tuple[str, str, str, str]) -> tuple[Counter, list[Link]]:
    name, state, since, until = args
    source = {"bc": bc_actions, "sharepoint": sharepoint_actions,
              "mission_control": mission_control_actions, "meetings": meetings_actions}[name]
    return fold(source(Path(state)), date.fromisoformat(since), date.fromisoformat(until))


def build(state: Path, *, since: date, until: date, workers: Optional[int] = None
          ) -> tuple[Counter, list[Link]]:
    """Fold every captured system in parallel (one process per source or mirror relation,
    largest first), read-only."""
    jobs: list[tuple[Callable, Any]] = [
        (_fold_source, (name, str(state), since.isoformat(), until.isoformat()))
        for name in ("bc", "sharepoint", "mission_control", "meetings")]
    for relation, columns in mirror_relations(state):
        plan = mirror_plan(relation, columns)
        if plan:
            jobs.append((mirror_relation, (str(state), relation, plan, since.isoformat(),
                                           until.isoformat())))
    counts: Counter = Counter()
    links: list[Link] = []
    with ProcessPoolExecutor(max_workers=workers or os.cpu_count()) as pool:
        for c, l in pool.map(_call, jobs):
            counts.update(c)
            links.extend(l)
    return counts, links


def _call(job: tuple[Callable, Any]) -> Any:
    fn, args = job
    return fn(args)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    from integrations.people_exclusion import PeopleExclusion, environ_with_exclusion
    from operating.inbox_triage import excluded_by
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="the snapshot (JSON) to write")
    parser.add_argument("--aliases", type=Path, help=f"the owner's {ALIASES_FILE}")
    parser.add_argument("--weeks", type=int, default=WINDOW_WEEKS)
    args = parser.parse_args(argv)
    until = date.today()
    since = until - timedelta(weeks=args.weeks)
    try:
        people = PeopleExclusion.from_env(environ_with_exclusion())
    except Exception:  # noqa: BLE001 - no guard: nobody is cleared
        people = None
    directory = directory_from_state(args.state_dir, args.aliases)
    counts, links = build(args.state_dir, since=since, until=until)
    snap = activity(counts, links, directory, excluded_by(people), since=since, until=until)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(args.out)
    for c in snap["coverage"]:
        print(json.dumps(c, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
