"""The Team view's snapshot: what each person did, day by day, in every captured system.

One file (``SNAPSHOT_FILE``, owner-only, in the Workbench's data directory) built off the
request path (``main``; the ``plx-team-activity`` timer, nightly) so the Team page reads it in
well under a second. Per person (an Entra object id, the key; never shown):

* **name**: the person's Teams display name, else the name calendars give their address,
  else their address's local part (``name_from`` says which); never the id;
* **department** (the directory's, when captured) and **roles** (the autopilot's derived
  roles file: purchasing, customer service, receivables, ...);
* **days**: per day (US Eastern) of the last ``WINDOW_DAYS``, counts per channel: mail sent
  and received, Teams messages, and every system the per-system snapshot attributes
  (``operating.system_activity``: meetings, Mission Control approvals, questions and task
  steps, Planner, SharePoint, BC, the Portal, Sage);
* **hours**: messages sent (mail and Teams) per hour of the day, US Eastern, last 28 days;
* **reply**: the median minutes from a message to them (in their own mailbox, from a person,
  addressed to them) to their next message in the same conversation, last 7 and 28 days;
* **with**: the colleagues they exchange most with (mail between them, messages in the same
  Teams chat), counts only.

People first, fail closed (``integrations.people_exclusion``): with the guard unavailable
nothing is built (``status`` ``blocked``). A mail that names an excluded colleague, or is sent from an
address at one of our own domains the directory cannot decide, is read for nobody; one that
merely has such an undecidable recipient counts for its readable sender and recipients and
names that address in no one's counts (``withheld.mail_names``, never served); an
excluded person's mailbox is not read; a Teams chat with an excluded or undecidable member
counts for nobody; the excluded appear nowhere, in no ``with`` list. The API re-checks the
exclusion when it serves the file and applies who may see whom (``operator_api.team_view``).

No content leaves here: counts, days, hours, names and object ids. Read-only: every store
is opened ``mode=ro``. Fixture runs are ``isolated-fixture``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import statistics
import sys
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from . import system_activity as sa
from ontology import parent_watch

SCHEMA = "plx-team-activity/v1"
SNAPSHOT_FILE = "team-activity.json"
WINDOW_DAYS = sa.DAYS_KEPT
PERIODS = (7, 28)
TRACKER_WEEKS = 4                   # the Team view's weekly tracker: Monday weeks, current last
MAX_AGE = timedelta(days=3)
TZ = ZoneInfo("America/New_York")
REPLY_MAX = timedelta(days=14)      # a later message after this long is not a reply
WITH_MAX = 12
CHANNELS = ("mail_sent", "mail_received", "teams", "meetings", "mc_approvals", "mc_questions",
            "mc_tasks", "planner", "sharepoint", "bc", "portal", "sage")
ROLE_LABELS = {"purchasing": "Purchasing", "customer_service": "Customer service",
               "receivables": "Receivables", "payables": "Payables", "quality": "Quality",
               "finance": "Finance", "production_planning": "Production planning",
               "rnd": "R&D"}
_OID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAIL_SLICES = 64


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def _instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        at = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _addr(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        value = value.get("address")
    if not isinstance(value, str) or "@" not in value:
        return None
    return value.strip().lower()


# --------------------------------------------------------------------------- readers

def _first_version(conn: sqlite3.Connection, table: str, key: str, since: str) -> int:
    """The first row id recorded at or after ``since`` (row ids grow with recording time;
    a message sent in the window is recorded after its window starts), by bisection."""
    top = conn.execute(f"SELECT max({key}) FROM {table}").fetchone()[0] or 0
    lo, hi = 0, top + 1
    while lo < hi:
        mid = (lo + hi) // 2
        row = conn.execute(f"SELECT recorded_at FROM {table} WHERE {key} >= ? ORDER BY {key} "
                           "LIMIT 1", (mid,)).fetchone()
        if row is None or str(row[0]) >= since:
            hi = mid
        else:
            lo = mid + 1
    return lo


def _mail_slice(args: tuple[str, int, int, str]) -> list[tuple]:
    path, lo, hi, since = args
    conn = _ro(Path(path))
    out = []
    try:
        from integrations.capture_mail import undecided_free_sql
        for mailbox, record in conn.execute(
                "SELECT mailbox_id, record FROM messages WHERE version_id >= ? AND "
                "version_id < ? AND superseded_at IS NULL AND removed_reason IS NULL AND "
                f"{undecided_free_sql(conn)}", (lo, hi)):
            rec = json.loads(record)
            at = _instant(rec.get("sent_at") or rec.get("received_at"))
            if at is None or at.isoformat() < since:
                continue
            out.append((str(mailbox).lower(), str(rec.get("internet_message_id") or
                                                  rec.get("id") or ""),
                        str(rec.get("conversation_id") or ""), at.isoformat(),
                        _addr(rec.get("from")),
                        tuple(a for a in map(_addr, rec.get("to") or ()) if a),
                        tuple(a for a in map(_addr, rec.get("cc") or ()) if a)))
    finally:
        conn.close()
    return out


def mail_rows(state: Path, since: datetime, workers: Optional[int] = None) -> list[tuple]:
    """(mailbox, message id, conversation, sent at, from, to, cc) of every current captured
    message sent since ``since``, read in parallel slices of the recent rows."""
    path = state / "mail-capture" / "capture.db"
    if not path.is_file():
        return []
    conn = _ro(path)
    try:
        lo = _first_version(conn, "messages", "version_id",
                            (since - timedelta(days=2)).isoformat())
        hi = (conn.execute("SELECT max(version_id) FROM messages").fetchone()[0] or 0) + 1
    finally:
        conn.close()
    if hi <= lo:
        return []
    step = max((hi - lo) // MAIL_SLICES + 1, 1)
    jobs = [(str(path), a, min(a + step, hi), since.isoformat()) for a in range(lo, hi, step)]
    out: list[tuple] = []
    with ProcessPoolExecutor(max_workers=workers or os.cpu_count(),
                             **parent_watch.pool_init()) as pool:
        for part in pool.map(_mail_slice, jobs):
            out.extend(part)
    return out


def teams_rows(state: Path, since: datetime) -> list[tuple[str, str, str, Optional[str]]]:
    """(sender object id, sent at, chat or channel, display name) of every current captured
    Teams message by a user since ``since``."""
    path = state / "teams-capture" / "capture.db"
    if not path.is_file():
        return []
    conn = _ro(path)
    out = []
    try:
        lo = _first_version(conn, "versions", "capture_id",
                            (since - timedelta(days=2)).isoformat())
        for payload, in conn.execute(
                "SELECT payload FROM versions WHERE capture_id >= ? AND superseded_at IS NULL "
                "AND kind IN ('teams_chat', 'teams_channel')", (lo,)):
            row = json.loads(payload).get("row") or {}
            who = row.get("from") if isinstance(row.get("from"), Mapping) else {}
            oid = str(who.get("id") or "").lower()
            at = _instant(row.get("created_at"))
            if not _OID.match(oid) or at is None or at < since or row.get("deleted_at") \
                    or row.get("message_type") not in (None, "message"):
                continue
            place = str(row.get("chat_id") or row.get("channel_id") or "")
            name = who.get("display_name")
            out.append((oid, at.isoformat(), place,
                        name.strip() if isinstance(name, str) and name.strip() else None))
    finally:
        conn.close()
    return out


def calendar_names(state: Path) -> dict[str, Counter]:
    """Per address, the names calendar events give it (organiser and attendees)."""
    path = state / "meetings-capture" / "capture.db"
    names: dict[str, Counter] = defaultdict(Counter)
    if not path.is_file():
        return names
    conn = _ro(path)
    try:
        for payload, in conn.execute("SELECT payload FROM versions WHERE superseded_at IS NULL "
                                     "AND kind = 'calendar_event'"):
            row = json.loads(payload).get("row") or {}
            for who in (row.get("organizer"), *(row.get("attendees") or ())):
                if isinstance(who, Mapping):
                    a, n = _addr(who), who.get("name")
                    if a and isinstance(n, str) and n.strip() and "@" not in n:
                        names[a][n.strip()] += 1
    finally:
        conn.close()
    return names


def departments(state: Path) -> dict[str, str]:
    """Per object id, the person's department: the org chart capture's (the directory the
    business view ``biz.staff`` reads, so Team, Insights and Ask say one department), else
    the mail capture's mailbox directory."""
    from integrations.capture_org import current_rows
    out: dict[str, str] = {}
    path = state / "mail-capture" / "capture.db"
    if path.is_file():
        conn = _ro(path)
        try:
            out = {str(o).lower(): str(d) for o, d in conn.execute(
                "SELECT mailbox_id, department FROM mailbox_directory") if d and str(d).strip()}
        except sqlite3.Error:
            out = {}
        finally:
            conn.close()
    for oid, row in current_rows(state / "org-capture" / "capture.db").items():
        said = str(row.get("department") or "").strip()
        if said:
            out[str(oid).lower()] = said
    return out


def load_roles(paths: Iterable[Optional[Path]]) -> dict[str, list[str]]:
    """Per object id, its roles from the roles files (``{"roles": {role: [oid]}}``), the
    first file naming a role winning; an unreadable file gives nothing."""
    out: dict[str, list[str]] = defaultdict(list)
    seen: set[str] = set()
    for path in paths:
        if path is None:
            continue
        try:
            roles = json.loads(Path(path).read_text()).get("roles")
        except (OSError, ValueError, AttributeError):
            continue
        for role, members in (roles if isinstance(roles, Mapping) else {}).items():
            if role in seen or role not in ROLE_LABELS or not isinstance(members, list):
                continue
            seen.add(role)
            for oid in members:
                if isinstance(oid, str) and _OID.match(oid.lower()):
                    out[oid.lower()].append(role)
    return dict(out)


# --------------------------------------------------------------------------- folding

def _median(values: Sequence[float]) -> Optional[int]:
    return round(statistics.median(values)) if values else None


def week_starts(until: date, weeks: int = TRACKER_WEEKS) -> list[date]:
    """The Mondays of the last ``weeks`` weeks (US Eastern days), the week holding ``until``
    last: the same weeks the work log's time saved counts in."""
    first = until - timedelta(days=until.weekday() + 7 * (weeks - 1))
    return [first + timedelta(days=7 * i) for i in range(weeks)]


def reply_weeks(replies: Sequence[tuple[date, float]], until: date
                ) -> dict[str, dict[str, Optional[int]]]:
    """Per tracker week (its Monday): the median minutes to reply and how many replies."""
    out: dict[str, dict[str, Optional[int]]] = {}
    for start in week_starts(until):
        end = min(start + timedelta(days=6), until)
        minutes = [m for d, m in replies if start <= d <= end]
        out[start.isoformat()] = {"median": _median(minutes), "n": len(minutes)}
    return out


def fold(*, mail: Iterable[tuple], teams: Iterable[tuple], cal_names: Mapping[str, Counter],
         directory: sa.Directory, groups: Iterable[str], excluded: Callable[[str], bool],
         system: Optional[Mapping[str, Any]], roles: Mapping[str, list[str]],
         depts: Mapping[str, str], until: date, automated: Callable[[str], bool],
         bulk: frozenset = frozenset()
         ) -> dict[str, Any]:
    """The snapshot (module docstring) from the rows the readers gave."""
    start = until - timedelta(days=WINDOW_DAYS - 1)
    recent = until - timedelta(days=PERIODS[-1] - 1)
    groups = frozenset(groups)
    own = directory.domains
    days: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    hours: dict[str, list[int]] = defaultdict(lambda: [0] * 24)
    with_: dict[str, Counter] = defaultdict(Counter)
    withheld: Counter = Counter()

    def local(at: datetime) -> datetime:
        return at.astimezone(TZ)

    def staff(address: str) -> tuple[Optional[str], str]:
        """(colleague's object id or None, "ok" | "excluded" | "undecidable"): an excluded
        colleague, or an address at our own domains that is no listed person or group (the
        directory cannot decide it)."""
        oid = directory.addresses.get(address)
        if oid is not None:
            return oid, "excluded" if excluded(oid) else "ok"
        domain = address.rsplit("@", 1)[-1]
        if own and domain in own and address not in groups:
            return None, "undecidable"
        return None, "ok"

    # mail: one message counted once, whichever mailboxes hold it
    sent_seen: set[str] = set()
    named_seen: set[str] = set()
    got_seen: set[tuple[str, str]] = set()
    threads: dict[tuple[str, str], list[tuple[datetime, str]]] = defaultdict(list)
    for mailbox, mid, conv, at_text, sender, to, cc in mail:
        if not _OID.match(mailbox) or excluded(mailbox):
            withheld["mail_mailbox"] += 1
            continue
        # One definition of "mail sent": every message a readable colleague sent counts for
        # them. A message that names an excluded colleague (as sender or recipient), or whose
        # SENDER is an address the directory cannot decide, is read for nobody (nothing
        # about it, not even a count, can show who is excluded). A recipient that is only
        # undecidable is left out of the people it names (no received count for them, in no
        # ``with`` list) and the message counted in ``withheld["mail_names"]``.
        people: dict[str, str] = {}
        left_out = False
        verdicts = {a: staff(a) for a in {sender, *to, *cc} - {None}}
        if any(v[1] == "excluded" or (v[1] == "undecidable" and a == sender)
               for a, v in verdicts.items()):
            withheld["mail"] += 1
            continue
        for a, (oid, status) in verdicts.items():
            if status == "ok" and oid:
                people[a] = oid
            elif status == "undecidable":
                left_out = True
        at = local(datetime.fromisoformat(at_text))
        day = at.date()
        if not start <= day <= until:
            continue
        if left_out and mid not in named_seen:
            named_seen.add(mid)
            withheld["mail_names"] += 1
        who = people.get(sender) if sender else None
        targets = {people[a] for a in (*to, *cc) if a in people} - {who}
        if who and mid in bulk:
            # one definition of "mail sent" (measure mail_sent): a message the business tables
            # call bulk (no copy of it is not) is not a person's sent mail
            withheld["mail_bulk"] += mid not in sent_seen
            sent_seen.add(mid)
        elif who and mid not in sent_seen:
            sent_seen.add(mid)
            days[who][day.isoformat()]["mail_sent"] += 1
            if day >= recent:
                hours[who][at.hour] += 1
            for other in targets:
                with_[who][other] += 1
                with_[other][who] += 1
        if sender and not automated(sender):
            for other in targets:
                if (other, mid) not in got_seen:
                    got_seen.add((other, mid))
                    days[other][day.isoformat()]["mail_received"] += 1
        if conv:
            to_owner = mailbox in {people.get(a) for a in to}
            if who == mailbox:
                threads[(mailbox, conv)].append((at, "out"))
            elif sender and not automated(sender) and to_owner:
                threads[(mailbox, conv)].append((at, "in"))

    replies: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for (mailbox, _conv), steps in threads.items():
        steps.sort()
        waiting: Optional[datetime] = None
        for at, way in steps:
            if way == "in":
                waiting = waiting or at
            elif waiting is not None:
                if at - waiting <= REPLY_MAX:
                    replies[mailbox].append((at.date(), (at - waiting).total_seconds() / 60))
                waiting = None

    # Teams: messages per sender; chat partners by the chats they write in
    names: dict[str, Counter] = defaultdict(Counter)
    places: dict[str, Counter] = defaultdict(Counter)
    rows = list(teams)
    blocked: set[str] = set()
    for oid, _at, place, name in rows:
        if excluded(oid):
            blocked.add(place)
    for oid, at_text, place, name in rows:
        if place in blocked:
            withheld["teams"] += 1
            continue
        if oid not in directory.oids:
            continue                          # a guest or a departed account: no colleague
        at = local(datetime.fromisoformat(at_text))
        if not start <= at.date() <= until:
            continue
        days[oid][at.date().isoformat()]["teams"] += 1
        if at.date() >= recent:
            hours[oid][at.hour] += 1
        if name:
            names[oid][name] += 1
        if place:
            places[place][oid] += 1
    for place, members in places.items():
        for a, n in members.items():
            for b in members:
                if a != b:
                    with_[a][b] += n

    # the other systems: the per-system snapshot's days, re-checked here
    fresh = isinstance(system, Mapping) and system.get("schema") == sa.SCHEMA
    if fresh:
        made = _instant(str(system.get("generated_at")))
        fresh = made is not None and datetime.now(timezone.utc) - made <= sa.SNAPSHOT_MAX_AGE
    if fresh:
        for oid, row in (system.get("people") or {}).items():
            if not isinstance(oid, str) or not _OID.match(oid) or excluded(oid):
                continue
            for day, counts in ((row or {}).get("days") or {}).items():
                if isinstance(counts, Mapping) and start.isoformat() <= day <= until.isoformat():
                    for channel, n in counts.items():
                        if channel in CHANNELS and isinstance(n, int):
                            days[oid][day][channel] += n

    by_address: dict[str, set[str]] = defaultdict(set)
    for address, oid in directory.addresses.items():
        by_address[oid].add(address)
    people: dict[str, Any] = {}
    for oid in sorted(set(days) | {o for o in roles if o in directory.oids}):
        if not _OID.match(oid) or excluded(oid):
            continue
        name, source = None, "address"
        if names.get(oid):
            name, source = names[oid].most_common(1)[0][0], "teams"
        else:
            heard: Counter = Counter()
            for address in by_address.get(oid, ()):
                heard.update(cal_names.get(address) or {})
            if heard:
                name, source = heard.most_common(1)[0][0], "calendar"
        if name is None:
            addresses = sorted(by_address.get(oid, ()))
            if not addresses:
                continue                      # nothing to call them by: never an id
            name = addresses[0].split("@")[0]
        mine = replies.get(oid, [])
        people[oid] = {
            "name": name, "name_from": source, "department": depts.get(oid),
            "roles": list(roles.get(oid, ())),
            "addresses": sorted(by_address.get(oid, ()))[:10],
            # a distribution list the directory types as a group is a mailbox, never a person
            "group_address": any(a in groups for a in by_address.get(oid, ())),
            "days": {d: dict(c) for d, c in sorted(days.get(oid, {}).items())},
            "hours": hours.get(oid, [0] * 24),
            "reply": {str(p): _median([m for d, m in mine
                                       if d > until - timedelta(days=p)]) for p in PERIODS},
            "reply_n": {str(p): sum(1 for d, _m in mine if d > until - timedelta(days=p))
                        for p in PERIODS},
            "reply_weeks": reply_weeks(mine, until),
            "with": {o: n for o, n in with_.get(oid, Counter()).most_common()
                     if o != oid and not excluded(o)}}
    for row in people.values():   # partners kept only when they are listed themselves
        row["with"] = dict(Counter({o: n for o, n in row["with"].items() if o in people}
                                   ).most_common(WITH_MAX))
    coverage = [{"system": c.get("system"), "share": float(c.get("share") or 0.0),
                 "actions": int(c.get("actions") or 0)}
                for c in ((system or {}).get("coverage") or ()) if fresh
                and c.get("system") in sa.SYSTEMS]
    return {"schema": SCHEMA, "status": "built",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window": {"from": start.isoformat(), "to": until.isoformat()},
            "systems_as_of": (system or {}).get("generated_at") if fresh else None,
            "coverage": coverage, "withheld": dict(withheld), "people": people,
            # the directory's own domains: the Team view joins rows only inside these
            "domains": sorted(own)}


def bulk_message_ids(dsn: str, since: datetime) -> frozenset:
    """The messages sent since ``since`` of which no copy is not bulk (``biz.emails.is_bulk``):
    the ones the measure ``mail_sent`` leaves out. Raises when the tables cannot be read: a
    count that cannot follow the measure is not built."""
    import psycopg
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        return frozenset(r[0] for r in conn.execute(
            "SELECT message_id FROM biz.emails WHERE sent_at >= %s AND message_id IS NOT NULL "
            "GROUP BY message_id HAVING bool_and(is_bulk)", (since,)))


def build(state: Path, *, until: date, system: Optional[Mapping[str, Any]],
          roles: Mapping[str, list[str]], people: Any, workers: Optional[int] = None,
          bulk: Optional[frozenset] = None) -> dict[str, Any]:
    """Read the captures under ``state`` (read-only) and fold them; ``people`` is the
    exclusion guard (None or unavailable: ``blocked``, nothing read)."""
    from integrations.capture_mail import automated_sender
    from integrations.people_exclusion import mail_capture_listing
    from .inbox_triage import excluded_by
    if people is None or getattr(people, "unavailable", None) or bulk is None:
        return {"schema": SCHEMA, "status": "blocked",
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "people": {}}
    since = datetime.combine(until - timedelta(days=WINDOW_DAYS), datetime.min.time(), TZ)
    directory = sa.directory_from_state(state)
    groups = [a.lower() for a, _o, kind in (mail_capture_listing(state) or ()) if kind == "group"]
    return fold(mail=mail_rows(state, since, workers), teams=teams_rows(state, since),
                cal_names=calendar_names(state), directory=directory, groups=groups,
                excluded=excluded_by(people), system=system, roles=roles,
                depts=departments(state), until=until,
                automated=lambda a: bool(automated_sender(a)), bulk=bulk)


def stale_of(previous: Any) -> Optional[dict[str, Any]]:
    """The previous built snapshot, unchanged but for a stale mark (its own time stays, plus a
    note that it could not be refreshed), when the bulk flags could not be read; None when
    there is no built snapshot of this schema to keep."""
    if not isinstance(previous, dict) or previous.get("schema") != SCHEMA \
            or previous.get("status") != "built":
        return None
    return {**previous, "stale": True,
            "stale_note": "couldn't refresh: the business tables' bulk flags could not be read; "
                          "these are the counts as of the time shown"}


def load(path: Optional[Path]) -> Optional[dict[str, Any]]:
    """The snapshot at ``path`` when it is this schema's and fresh (``MAX_AGE``), else None."""
    data = sa.load_snapshot(path)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    made = _instant(str(data.get("generated_at")))
    if made is None or datetime.now(timezone.utc) - made > MAX_AGE:
        return None
    return data


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    from integrations.people_exclusion import PeopleExclusion, environ_with_exclusion
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="the snapshot (JSON) to write")
    parser.add_argument("--system-activity", type=Path,
                        help=f"the per-system snapshot ({sa.SNAPSHOT_FILE})")
    parser.add_argument("--roles", type=Path, action="append", default=[],
                        help="a roles file (repeatable; default: PLX_AUTOPILOT_ROLES_FILE and "
                             "the work graph's roles-derived.json)")
    args = parser.parse_args(argv)
    try:
        people = PeopleExclusion.from_env(environ_with_exclusion())
    except Exception:  # noqa: BLE001 - no guard: nothing is built
        people = None
    named = os.environ.get("PLX_AUTOPILOT_ROLES_FILE")
    roles = load_roles(args.roles or [Path(named) if named else None,
                                      args.state_dir / "work-graph" / "roles-derived.json"])
    until = datetime.now(TZ).date()
    try:
        from operating.pg import config
        bulk = bulk_message_ids(config.dsn(), datetime.combine(
            until - timedelta(days=WINDOW_DAYS), datetime.min.time(), TZ))
    except Exception:  # noqa: BLE001 - no flags, no count that follows the measure: blocked
        bulk = None
    snap = build(args.state_dir, until=until,
                 system=sa.load_snapshot(args.system_activity), roles=roles, people=people,
                 bulk=bulk)
    if snap["status"] == "blocked" and bulk is None and people is not None \
            and not getattr(people, "unavailable", None):
        snap = stale_of(sa.load_snapshot(args.out)) or snap
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, sort_keys=True))
    # owner and group (plx-state, the state directory's): the API user reads it; others none
    os.chmod(tmp, 0o640)
    tmp.replace(args.out)
    print(json.dumps({"status": snap["status"], "people": len(snap["people"]),
                      "withheld": snap.get("withheld", {})}, sort_keys=True))
    return 0 if snap["status"] == "built" and not snap.get("stale") else 1


if __name__ == "__main__":
    sys.exit(main())
