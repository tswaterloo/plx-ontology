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

from operating import briefs as br
from . import models as m
from .auth import Reader
from .errors import UserFacing

READERS_ENV = "PLX_TEAM_VIEW_READERS"
HIDDEN_ENV = "PLX_TEAM_HIDDEN_PEOPLE"
FILE_ENV = "PLX_TEAM_ACTIVITY_FILE"
CLOSED_DAYS = 28
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
            os.chmod(tmp, 0o600)
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


def person_row(oid: str, row: Mapping[str, Any], *, until: date, team: Optional[str],
               open_: Mapping[str, int], is_self: bool) -> dict[str, Any]:
    from operating import team_activity as ta
    days = row.get("days") or {}
    trend = [sum(int(v) for v in (days.get((until - timedelta(days=i)).isoformat()) or {}
                                  ).values()) for i in range(27, -1, -1)]
    roles = [ta.ROLE_LABELS[r] for r in row.get("roles") or () if r in ta.ROLE_LABELS]
    reply, n = row.get("reply") or {}, row.get("reply_n") or {}
    return {"key": key_of(oid), "name": str(row.get("name")), "self": is_self,
            "team": team or row.get("department") or (roles[0] if roles else None),
            "roles": roles,
            "d7": _sum(days, until - timedelta(days=6), until),
            "d28": _sum(days, until - timedelta(days=27), until),
            "awaiting_reply": open_.get("awaiting_reply", 0), "tasks": open_.get("tasks", 0),
            "alerts": open_.get("alerts", 0), "alerts_closed": open_.get("alerts_closed", 0),
            "reply_minutes_7": reply.get("7"), "reply_minutes_28": reply.get("28"),
            "replies_28": int(n.get("28") or 0), "trend": trend,
            "hours": [int(h) for h in (row.get("hours") or [0] * 24)][:24]}


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
            per.setdefault(row["owner"], Counter())["alerts_closed"] += 1
    return per


class TeamViewMixin:
    """``/api/team`` and ``/api/team/{key}`` on the real backend."""

    def _team_file(self) -> Optional[Path]:
        from operating import team_activity as ta
        named = os.environ.get(FILE_ENV)
        data_dir = getattr(self, "data_dir", None)
        return Path(named) if named else Path(data_dir) / ta.SNAPSHOT_FILE if data_dir else None

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
        return everyone, me, gone, snap, counts, teams, people, shown, known

    def team_view(self, reader: Reader) -> m.TeamView:
        everyone, me, _gone, snap, counts, teams, people, shown, _known = self._team_parts(reader)
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
        from operating.proactive_commitments import OWNER_KINDS
        everyone, me, gone, snap, counts, teams, people, shown, known = self._team_parts(reader)
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

        def job(view: Any) -> Any:
            found = view.gap_inbox(limit=br_max(), all_owners=everyone)
            if not isinstance(found, Mapping) or found.get("status") != "read":
                return None
            return found.get("gaps") or ()
        gaps = self._brief_run(reader, job) if known else None   # type: ignore[attr-defined]
        items: list[dict[str, Any]] = []
        for bucket in it.BUCKETS:
            for x in it.owner_items(gaps or (), person=oid, bucket=bucket,
                                    owner_kinds=OWNER_KINDS, excluded=lambda o: gone(o)
                                    or excluded(o), address_excluded=address_excluded,
                                    today=date.today()):
                if x["waiting_on"] == oid:
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
