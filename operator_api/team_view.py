"""The Team view (``/api/team``, ``/api/team/{key}``): each person's activity across systems,
their open work and who they work with, served from the precomputed snapshot
(``operating.team_activity``) and the run's inbox, so a page reads in well under a second.

Who sees whom (2026-11-11), decided per request from the signed-in
session only (nothing in a request can claim it):

* the owner-open reader (``ReadPolicy.unrestricted``) and the readers ``PLX_TEAM_VIEW_READERS``
  names (object ids or addresses) see everyone;
* every other reader sees only their own row;
* the people ``PLX_TEAM_HIDDEN_PEOPLE`` names (the owner) are in no row, no drill-down and
  no partner list, for every reader, themselves included. Fail closed: with it unset, the
  owner-open reader's own ids are hidden and every other reader sees only their own row;
* the people exclusion fails closed: a person it does not clear, or every person while it
  is unavailable, is shown to nobody.

A row's ``key`` is an opaque handle (a digest of the object id), so no id reaches a page or
a URL. Open items come from the reader's own trimmed alert list, so a marked item they cannot
open is counted (``withheld``), never shown.

The productivity tracker (AUDIT2-12; the measure decided under Internal
only, the owner hidden). Each row's ``weeks``: the last four Monday weeks (US Eastern, the
current one cut at the snapshot's day) of **done** (everything they sent or did: mail sent,
Teams messages, meetings and each system's recorded actions; mail received is what **came
in**, not work), **closed** (their alerts resolved that week), the **median reply** and the
replies it rests on, and **time saved** (the work log's estimate, joined per request by the
API: everyone's for the owner only, as ``/api/time-saved``; a named Team reader sees only
their own, other rows null). **Waiting** is the row's open replies, tasks and alerts now. No
single blended score: a score would rank people by what the captured systems happen to see
(floor, lab and phone work are not seen), so the tracker shows the parts side by side.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
import hashlib
import json
import os
import re
from pathlib import Path
import threading
from typing import Any, Callable, Iterable, Mapping, Optional

from operating.file_modes import keep_shared
from operating import briefs as br
from . import models as m
from .auth import Reader
from .errors import UserFacing

READERS_ENV = "PLX_TEAM_VIEW_READERS"
HIDDEN_ENV = "PLX_TEAM_HIDDEN_PEOPLE"
FILE_ENV = "PLX_TEAM_ACTIVITY_FILE"
CLOSED_DAYS = 28
# what counts as work done in the tracker: every channel but mail they received
DONE_CHANNELS = frozenset(("mail_sent", "teams", "meetings", "mc_approvals", "mc_questions",
                           "mc_tasks", "planner", "sharepoint", "bc", "portal", "sage"))
NOTE = "Mail, Teams, meetings and the systems that record who did what. Floor, lab and phone work is not seen."

_CACHE: dict[str, tuple[float, Any]] = {}
_LOCK = threading.Lock()


def key_of(oid: str) -> str:
    return hashlib.sha256(f"plx-team:{oid.lower()}".encode()).hexdigest()[:16]


def _listed(name: str) -> frozenset[str]:
    return frozenset(v.strip().lower() for v in (os.environ.get(name) or "").split(",")
                     if v.strip())


OWNER_IDS_FILE = "team-owner-ids.json"
_OID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _learned_owners(path: Optional[Path]) -> frozenset[str]:
    try:
        data = json.loads(Path(path).read_text()) if path else []
    except (OSError, ValueError, TypeError):
        return frozenset()
    return frozenset(v for v in data if isinstance(v, str) and _OID_RE.fullmatch(v)) \
        if isinstance(data, list) else frozenset()


def _learn_owner(path: Optional[Path], oids: Iterable[str]) -> None:
    """Keep the object ids of an owner-open session (the one owner test) so every later
    reader's views hide the owner without any setting."""
    new = {o.lower() for o in oids if _OID_RE.fullmatch(o.lower())}
    if path is None or not new:
        return
    with _LOCK:
        have = _learned_owners(path)
        if new <= have:
            return
        try:
            tmp = Path(str(path) + ".tmp")
            tmp.write_text(json.dumps(sorted(have | new)))
            keep_shared(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            pass


def hidden_people(env_values: Iterable[str], learned: Iterable[str],
                  people: Optional[Mapping[str, Any]] = None) -> frozenset[str]:
    """Object ids shown to nobody: ``PLX_TEAM_HIDDEN_PEOPLE`` (object ids or e-mail
    addresses; an address is resolved through the snapshot's addresses, one that resolves
    to nobody hides nobody) and the owner ids kept from owner sessions."""
    out = {str(v).lower() for v in learned}
    by_address = {a: o for o, row in (people or {}).items()
                  for a in (row.get("addresses") or ())}
    for value in env_values:
        v = value.strip().lower()
        if _OID_RE.fullmatch(v):
            out.add(v)
        elif v in by_address:
            out.add(by_address[v])
    return frozenset(out)


def snapshot(path: Optional[Path]) -> Optional[dict[str, Any]]:
    """The team snapshot (``team_activity.load``), parsed once per file change."""
    from operating import team_activity as ta
    if path is None:
        return None
    try:
        stamp = Path(path).stat().st_mtime
    except OSError:
        return None
    with _LOCK:
        held = _CACHE.get(str(path))
        if held and held[0] == stamp:
            return held[1]
    data = ta.load(Path(path))
    with _LOCK:
        _CACHE[str(path)] = (stamp, data)
    return data


def _sum(days: Mapping[str, Mapping[str, int]], start: date, until: date) -> dict[str, int]:
    out: Counter = Counter()
    for day, counts in days.items():
        if start.isoformat() <= day <= until.isoformat():
            out.update({k: int(v) for k, v in counts.items()})
    return {k: v for k, v in out.items() if v}


CLOSED_ON = "closed_on:"     # open_counts key prefix: alerts closed on that day


def tracker_weeks(row: Mapping[str, Any], *, until: date,
                  open_: Mapping[str, int]) -> list[dict[str, Any]]:
    """The productivity tracker (AUDIT2-12): per Monday week, the current one last and cut
    at ``until``: work done (everything they sent or did in a system), messages received,
    alerts closed, the median reply time and the replies it rests on. Time saved is joined
    in by the API from the work log (``attach_saved``); waiting is the row's open counts."""
    from operating import team_activity as ta
    days = row.get("days") or {}
    replies = row.get("reply_weeks") or {}
    out = []
    for start in ta.week_starts(until):
        end = min(start + timedelta(days=6), until)
        counts = _sum(days, start, end)
        closed = sum(int(v) for k, v in open_.items() if k.startswith(CLOSED_ON)
                     and start.isoformat() <= k[len(CLOSED_ON):] <= end.isoformat())
        reply = replies.get(start.isoformat()) or {}
        out.append({"week": start.isoformat(), "to": end.isoformat(),
                    "partial": end < start + timedelta(days=6),
                    "done": sum(n for c, n in counts.items() if c in DONE_CHANNELS),
                    "received": counts.get("mail_received", 0), "closed": closed,
                    "reply_minutes": reply.get("median"), "replies": int(reply.get("n") or 0),
                    "saved_minutes": None})
    return out


def attach_saved(people: Iterable[Any], found: Optional[Mapping[str, Any]],
                 reader_key: Optional[str] = None) -> None:
    """Fills each person's (``TeamPerson`` models already shown to the reader) weekly ``saved_minutes`` from ``work_log.time_saved``
    (keyed by the reader's principals, which name their object ids). Only rows already in
    the view get a figure (the owner, hidden, never does); a week the log has no row for
    stays None (not counted), a counted week with nothing used is 0. ``reader_key``: the
    log was read for that reader alone, so only their own row gets a figure."""
    from operating.proactive_commitments import owner_ids
    if found is None:
        return
    by_key: dict[str, dict[str, int]] = {}
    for person in found.get("people") or ():
        weeks = {str(w.get("start")): int(w.get("minutes") or 0) for w in person.get("weeks") or ()}
        for oid in owner_ids(str(person.get("reader") or "")):
            slot = by_key.setdefault(key_of(oid), {})
            for start, minutes in weeks.items():
                slot[start] = slot.get(start, 0) + minutes
    counted = {str(s) for s in found.get("weeks") or ()}
    only = None if reader_key is None else {key_of(o) for o in owner_ids(reader_key)}
    for person in people:
        if only is not None and person.key not in only:
            continue
        mine = by_key.get(person.key, {})
        for week in person.weeks:
            if week.week in counted:
                week.saved_minutes = mine.get(week.week, 0)


def person_row(oid: str, row: Mapping[str, Any], *, until: date, team: Optional[str],
               open_: Mapping[str, int], is_self: bool) -> dict[str, Any]:
    from operating import team_activity as ta
    days = row.get("days") or {}
    trend = [sum(int(v) for v in (days.get((until - timedelta(days=i)).isoformat()) or {}
                                  ).values()) for i in range(27, -1, -1)]
    roles = [ta.ROLE_LABELS[r] for r in row.get("roles") or () if r in ta.ROLE_LABELS]
    reply, n = row.get("reply") or {}, row.get("reply_n") or {}
    return {"key": key_of(oid), "name": str(row.get("name")), "self": is_self,
            "team": team or row.get("department"),   # a derived role is a role, never a team
            "roles": roles,
            "d7": _sum(days, until - timedelta(days=6), until),
            "d28": _sum(days, until - timedelta(days=27), until),
            "awaiting_reply": open_.get("awaiting_reply", 0), "tasks": open_.get("tasks", 0),
            "alerts": open_.get("alerts", 0), "alerts_closed": open_.get("alerts_closed", 0),
            "reply_minutes_7": reply.get("7"), "reply_minutes_28": reply.get("28"),
            "replies_28": int(n.get("28") or 0), "trend": trend,
            "hours": [int(h) for h in (row.get("hours") or [0] * 24)][:24],
            "weeks": tracker_weeks(row, until=until, open_=open_)}


def open_counts(rows: Iterable[Mapping[str, Any]], closed: Iterable[Mapping[str, Any]],
                owner_kinds: Iterable[str]) -> dict[str, Counter]:
    from operating import inbox_triage as it
    per: dict[str, Counter] = {}
    for row in rows:
        if row.get("owner"):
            per.setdefault(row["owner"], Counter())[it.bucket_of(row.get("kind"),
                                                                 owner_kinds)] += 1
    for row in closed:
        if row.get("owner"):
            mine = per.setdefault(row["owner"], Counter())
            mine["alerts_closed"] += 1
            if row.get("resolved_on"):
                mine[CLOSED_ON + str(row["resolved_on"])] += 1
    return per


_NAME_RANK = {"teams": 0, "calendar": 1, "address": 2}


def merge_identities(people: Mapping[str, Mapping[str, Any]], shown: Iterable[str],
                     counts: Mapping[str, Counter], domains: Iterable[str] = ()
                     ) -> tuple[dict[str, dict[str, Any]], dict[str, Counter], dict[str, frozenset[str]]]:
    """One row per person: the snapshot keeps one row per directory object, and one person
    can hold several (a work address and a second brand's address, a tenant sign-in).

    Two rows are one person only when every join below holds; otherwise they stay apart:
    * they share the local part of an address at one of the directory's own ``domains``
      (none known: nothing is joined);
    * at least one has a display name a colleague gave it in Teams (a person's evidence), and
      the other has either no name (an address only) or the same Teams name;
    * neither is a directory group (``group_address``).
    Two rows with no Teams name are never joined: a role mailbox (info@ at two brands) and
    two people who share a first name look the same there. A calendar's name never anchors a
    join (it can name a mailbox's alias). Counts add; the reply times of the row with the
    most replies stand (a median cannot be added); partners are re-pointed to the merged
    rows. Returns (people with the merged rows, their open counts, representative -> members)."""
    own = frozenset(str(d).lower() for d in domains)
    shown = [o for o in shown if o in people]
    parent = {o: o for o in shown}

    def find(o: str) -> str:
        while parent[o] != o:
            parent[o] = parent[parent[o]]
            o = parent[o]
        return o

    def named(o: str) -> Optional[str]:
        row = people[o]
        return str(row.get("name")).strip().lower() if row.get("name_from") == "teams" else None
    by_local: dict[str, list[str]] = {}
    for oid in shown:
        for a in people[oid].get("addresses") or ():
            local, _, dom = str(a).lower().partition("@")
            if local and dom in own and not people[oid].get("group_address"):
                by_local.setdefault(local, []).append(oid)
    for oids in by_local.values():
        oids = list(dict.fromkeys(oids))
        anchors = [o for o in oids if people[o].get("name_from") == "teams"]
        plain = [o for o in oids if people[o].get("name_from") == "address"]
        # one Teams name in the bucket, and at most one name-less row: two name-less rows
        # sharing a local part may be two people or one role mailbox, so none is joined
        if len({named(o) for o in anchors}) != 1:
            continue   # no Teams name, or two that differ
        if len(plain) > 1:
            plain = []
        if len(anchors) + len(plain) < 2:
            continue
        for other in anchors[1:] + plain:
            a, b = find(anchors[0]), find(other)
            if a != b:
                parent[b] = a
    groups: dict[str, list[str]] = {}
    for oid in shown:
        groups.setdefault(find(oid), []).append(oid)
    for members in groups.values():
        members.sort(key=lambda o: (_NAME_RANK.get(str(people[o].get("name_from")), 3), o))
    alias = {m: members[0] for members in groups.values() for m in members}
    merged = {o: people[o] for o in people if o not in alias}
    new_counts: dict[str, Counter] = {o: c for o, c in counts.items() if o not in alias}
    members_of: dict[str, frozenset[str]] = {}
    for members in groups.values():
        rep = members[0]
        members_of[rep] = frozenset(members)
        rows = [people[o] for o in members]
        days: dict[str, Counter] = {}
        hours = [0] * 24
        for row in rows:
            for d, c in (row.get("days") or {}).items():
                days.setdefault(d, Counter()).update({k: int(v) for k, v in c.items()})
            for i, h in enumerate((row.get("hours") or [])[:24]):
                hours[i] += int(h)
        best = max(rows, key=lambda r: int((r.get("reply_n") or {}).get("28") or 0))
        partners: Counter = Counter()
        for row in rows:
            for o, n in (row.get("with") or {}).items():
                target = alias.get(o, o)
                if target != rep:
                    partners[target] += int(n)
        row0 = dict(rows[0])
        row0.update({
            "roles": sorted({r for row in rows for r in row.get("roles") or ()}),
            "department": next((r.get("department") for r in rows if r.get("department")), None),
            "addresses": sorted({a for row in rows for a in row.get("addresses") or ()})[:10],
            "days": {d: dict(c) for d, c in sorted(days.items())}, "hours": hours,
            "reply": best.get("reply"), "reply_n": best.get("reply_n"),
            "reply_weeks": best.get("reply_weeks"), "with": dict(partners.most_common())})
        merged[rep] = row0
        total: Counter = Counter()
        for o in members:
            total.update(counts.get(o, Counter()))
        if total:
            new_counts[rep] = total
    for row in merged.values():   # a partner row merged into another is that row
        if row.get("with"):
            tally: Counter = Counter()
            for o, n in row["with"].items():
                tally[alias.get(o, o)] += int(n)
            row["with"] = dict(tally.most_common())
    return merged, new_counts, members_of


class TeamViewMixin:
    """``/api/team`` and ``/api/team/{key}`` on the real backend."""

    def _team_file(self) -> Optional[Path]:
        from operating import team_activity as ta
        named = os.environ.get(FILE_ENV)
        if named:
            return Path(named)
        # the API's data directory, else the state directory the nightly unit writes to
        # (plx-team-activity --out <state>/team-activity.json): a host that never set
        # PLX_TEAM_ACTIVITY_FILE showed "not counted yet" beside a fresh snapshot
        dirs = [Path(d) for d in (getattr(self, "data_dir", None), getattr(self, "state_dir", None)) if d]
        for d in dirs:
            if (d / ta.SNAPSHOT_FILE).is_file():
                return d / ta.SNAPSHOT_FILE
        return dirs[0] / ta.SNAPSHOT_FILE if dirs else None

    def my_roles(self, reader: Reader) -> list[str]:
        """The reader's own work roles in the team snapshot (``team_activity.ROLE_LABELS``
        keys, e.g. ``purchasing``), for the app's per-role defaults (pinned pages). Only
        the reader's own row; unknown roles are dropped; no snapshot or any failure: []."""
        from operating import team_activity as ta
        try:
            people = (snapshot(self._team_file()) or {}).get("people") or {}
            mine = self._my_oids(reader)   # type: ignore[attr-defined]
            roles = {str(r) for oid in mine for r in ((people.get(oid) or {}).get("roles") or ())}
        except Exception:  # noqa: BLE001 - a default, never a page failure
            return []
        return sorted(r for r in roles if r in ta.ROLE_LABELS)

    def _team_scope(self, reader: Reader) -> tuple[bool, frozenset[str], Callable[[str], bool], bool]:
        """(sees everyone, the reader's own ids, whether a person is shown to nobody)."""
        from operating import inbox_triage as it
        me = self._my_oids(reader)   # type: ignore[attr-defined]
        owner = self._is_owner(reader)   # type: ignore[attr-defined]
        held = {str(v).lower() for v in getattr(reader.session, "identities", ()) or ()} | me
        data_dir = getattr(self, "data_dir", None)
        owners_file = Path(data_dir) / OWNER_IDS_FILE if data_dir else None
        if owner:
            _learn_owner(owners_file, me)
        snap = snapshot(self._team_file())
        hidden = hidden_people(_listed(HIDDEN_ENV), _learned_owners(owners_file) | (
            me if owner else frozenset()), (snap or {}).get("people"))
        # no owner known (never signed in, nothing set): a plain reader's view must not
        # name anyone it cannot be sure is not the owner
        everyone = bool(hidden) and (owner or bool(held & _listed(READERS_ENV)))
        excluded = it.excluded_by(br.env_people())

        def gone(oid: str) -> bool:
            return oid in hidden or excluded(oid)
        return everyone, me, gone, bool(hidden)

    def _team_parts(self, reader: Reader):
        from operating import inbox_triage as it
        from operating import proactive_engine as pe
        from operating.proactive_commitments import OWNER_KINDS
        everyone, me, gone, known = self._team_scope(reader)
        snap = snapshot(self._team_file())
        store = getattr(self, "store_path", None)
        inbox = pe.inbox_path(store) if store else None
        since = (date.today() - timedelta(days=CLOSED_DAYS)).isoformat()
        counts = open_counts(it.open_rows(inbox) if inbox else (),
                             it.closed_rows(inbox, since) if inbox else (), OWNER_KINDS)
        grants = self._workload_grants()   # type: ignore[attr-defined]
        teams = (grants.load() if grants else {}).get("teams") or {}
        people = (snap or {}).get("people") or {}
        shown = {oid for oid in people if not gone(oid) and (everyone or oid in me)}
        people, counts, members = merge_identities(people, shown, counts, (snap or {}).get("domains") or ())
        shown = {oid for oid in shown if oid in people}
        me = frozenset(me | {rep for rep, ms in members.items() if ms & me})
        return everyone, me, gone, snap, counts, teams, people, shown, known, members

    def team_view(self, reader: Reader) -> m.TeamView:
        everyone, me, _gone, snap, counts, teams, people, shown, _known, _members = self._team_parts(reader)
        if snap is None or snap.get("status") != "built":
            return m.TeamView(scope="everyone" if everyone else "self",
                              state="blocked" if snap else "not_built", note=NOTE)
        until = date.fromisoformat(snap["window"]["to"])
        rows = [m.TeamPerson(**person_row(oid, people[oid], until=until, team=teams.get(oid),
                                          open_=counts.get(oid, {}), is_self=oid in me))
                for oid in sorted(shown, key=lambda o: str(people[o].get("name")).lower())]
        return m.TeamView(scope="everyone" if everyone else "self", state="ready",
                          as_of=snap.get("generated_at"), window_to=snap["window"]["to"],
                          people=rows, note=NOTE,
                          coverage=[m.SystemCoverage(**c) for c in snap.get("coverage") or ()])

    def team_person(self, reader: Reader, key: str) -> m.TeamPersonDetail:
        """One person's drill-down; 404 for a key the reader may not open (never 403, so a
        key tells nothing about who exists)."""
        from operating import inbox_triage as it
        from operating import proactive_engine as pe
        from operating.proactive_commitments import OWNER_KINDS
        everyone, me, gone, snap, counts, teams, people, shown, known, members = self._team_parts(reader)
        oid = next((o for o in shown if key_of(o) == key), None)
        if oid is None or snap is None:
            raise UserFacing("not_found", "No such person.", 404)
        row = people[oid]
        until = date.fromisoformat(snap["window"]["to"])
        person = person_row(oid, row, until=until, team=teams.get(oid),
                            open_=counts.get(oid, {}), is_self=oid in me)
        days = row.get("days") or {}
        timeline = [m.TeamDay(day=d, counts={k: int(v) for k, v in
                                             (days.get(d) or {}).items() if v})
                    for d in ((until - timedelta(days=i)).isoformat() for i in range(27, -1, -1))]
        partners = [m.TeamPartner(key=key_of(o) if o in shown else None,
                                  name=str(people[o].get("name")), n=int(n))
                    for o, n in (row.get("with") or {}).items()
                    if known and o in people and not gone(o)]   # owner unknown: name no one
        people_guard = br.env_people()
        excluded = it.excluded_by(people_guard)

        def address_excluded(address: str) -> bool:
            try:
                return bool(people_guard.is_excluded(address))
            except Exception:  # noqa: BLE001 - an undecidable counterpart is never shown
                return True

        # the list is read from what the count counts: only the kinds this person has open
        # (the read's limit follows the inbox's order, late orders first, then priority, so
        # a whole-inbox read never reached a person's low-priority replies), kinds the alert
        # registry keeps off Home included (counted, so listed), still trimmed to the reader
        store = getattr(self, "store_path", None)
        mine = members.get(oid) or frozenset({oid})   # every row this one person is
        kinds = sorted({r["kind"] for r in (it.open_rows(pe.inbox_path(store)) if store
                                            else ())
                        if r.get("owner") in mine and isinstance(r.get("kind"), str)})

        def job(view: Any) -> Any:
            found = view.gap_inbox(limit=br_max(), all_owners=everyone, kinds=kinds,
                                   include_off=True)
            if not isinstance(found, Mapping) or found.get("status") != "read":
                return None
            return found.get("gaps") or ()
        gaps = None if not known else () if not kinds else \
            self._brief_run(reader, job)   # type: ignore[attr-defined]
        items: list[dict[str, Any]] = []
        for bucket in it.BUCKETS:
            for member in sorted(mine):
                for x in it.owner_items(gaps or (), person=member, bucket=bucket,
                                        owner_kinds=OWNER_KINDS, excluded=lambda o: gone(o)
                                        or excluded(o), address_excluded=address_excluded,
                                        today=date.today()):
                    if x["waiting_on"] in mine:
                        x["waiting_on"] = person["name"]
                    items.append({**x, "bucket": bucket})
        counted = sum(counts.get(oid, {}).get(b, 0) for b in it.BUCKETS)
        items.sort(key=lambda r: (-(r["age_days"] if r["age_days"] is not None else -1),
                                  r["id"]))
        return m.TeamPersonDetail(
            person=m.TeamPerson(**person), timeline=timeline, partners=partners,
            items=[m.TeamItem(**x) for x in items[:it.DRILL_MAX]],
            withheld=max(counted - len(items), 0) if gaps is not None else counted,
            truncated=len(items) > it.DRILL_MAX)


def br_max() -> int:
    from .briefs import bf_max
    return bf_max()
