"""The autopilot's 30-day replay: for one role, the real events of each past day run
through the autopilot (``autopilot.run_cycle``, the live code path), and each item it would
have prepared is compared with what the person actually did next. Scored per workflow
(``operating.workflows``) of the role; the live engine switches a workflow on for the role's
people only when it passed (``autopilot.gate_from``: 90% or better on 15 or more judged).

* **Role** (``members``): ``owner`` (the approver roster), ``purchasing`` (the roles file,
  else ``autopilot_roster.derive`` over the 90 days before the window), any role of the
  roles file, or ``person:<address or id>`` for one person's whole day. Every member
  passes the people exclusion (unavailable: the run is ``blocked``).
* **Prepared**: each day of the window is one cycle (and its drain) with ``now`` at the
  day's end, preparing only for the role's members, into the role's own queue. Resumable:
  a finished day is recorded and skipped on the next run; the queue's events and items are
  insert-or-ignore, so a day cut short re-runs without a duplicate.
* **Actions** (``PROVIDERS``, declared in ``autopilot_replay.json``): mail the person sent,
  approvals they decided, task events they acted on, and every store event naming them as
  its actor (per-person system activity in BC, the Portal, Sage and FileMaker plugs in
  there, or as a ``plugins`` provider).
* **Match**: an item matches when its owner, after the event and within ``horizon_hours``,
  acted on the same conversation or touched one of the records the item is about (a mail
  to the same contact about something else is no match). An item whose horizon passes the end of the data is
  ``unjudged`` (never counted either way). Precision = matched / judged, per workflow;
  coverage beside it = the person's actions of that workflow an item anticipated / all.

Read-only on the store and the capture; writes only ``--out``. Memory: before each day the
run waits for ``--min-available-gb`` (``MemAvailable``), then gives up resumably (exit 75);
the process's data segment is capped at ``--mem-max-gb``. The result holds ids, counts and
workflow names, never message text.

Run::

    python3 -m operating.autopilot_replay --store STORE --out DIR --role purchasing \\
        --days 30 --until 2026-11-05T07:00:00Z [--capture-state STATE]
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import autopilot as ap
from . import workflows as wf

CONFIG_FILE = Path(__file__).with_name("autopilot_replay.json")
SCHEMA = "plx-autopilot-replay/v1"
CHUNK = 2000
EXIT_NO_MEMORY = 75
MENTIONS = re.compile(r"^Email\.(?:mentions_|sent_by_)")


class ReplayInvalid(ValueError):
    pass


# ----------------------------------------------------------------------------- config

def load_config(path: Path | str | None = None) -> dict[str, Any]:
    raw = json.loads(Path(path or CONFIG_FILE).read_text(encoding="utf-8"))
    if raw.get("schema") != SCHEMA:
        raise ReplayInvalid("schema")
    unknown = [p for p in raw.get("providers") or () if p not in PROVIDERS]
    if unknown:
        raise ReplayInvalid(f"provider {unknown[0]}")
    plugins = []
    for spec in raw.get("plugins") or ():
        module, _, func = str(spec).partition(":")
        if not module.startswith("operating.") or not func.isidentifier():
            raise ReplayInvalid(f"plugin {spec}")
        fn = getattr(importlib.import_module(module), func, None)
        if not callable(fn):
            raise ReplayInvalid(f"plugin {spec}")
        plugins.append(fn)
    hours = int(raw.get("horizon_hours") or 0)
    if not 1 <= hours <= 24 * 14:
        raise ReplayInvalid("horizon_hours")
    return {"horizon": timedelta(hours=hours),
            "providers": [PROVIDERS[p] for p in raw.get("providers") or ()] + plugins}


# ----------------------------------------------------------------------------- members

def members(st: Any, people: Any, role: str, roles: Mapping[str, list[str]], *,
            until: str) -> list[str]:
    """The role's people (module doc), each cleared by the people exclusion."""
    directory, _addresses, _domains = ap._people_maps(people)
    if role.startswith("person:"):
        who = role[len("person:"):].lower()
        found = {directory.get(who, who if re.fullmatch(r"[0-9a-f-]{36}", who) else "")}
    elif role == "purchasing" and not roles.get("purchasing"):
        from . import autopilot_roster as roster
        since = ap._iso(ap._instant(until) - timedelta(days=90))
        found = set(roster.derive(st, people, "purchasing", since, until)["members"])
    else:
        found = ap._role_members(role, roles, directory)
    return sorted(o for o in found if o and people.identity_clear(o))


# ----------------------------------------------------------------------------- actions

def _window(st: Any, kinds: Sequence[str], since: str, until: str) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in st.conn.execute(
        "SELECT eo.object_id, MIN(e.time) FROM events e JOIN event_objects eo ON eo.event_id "
        "= e.event_id WHERE e.event_type IN (SELECT value FROM json_each(?)) AND e.time >= ? "
        "AND e.time < ? GROUP BY eo.object_id", (json.dumps(list(kinds)), since, until))]


def mail_sent(st: Any, people: Any, who: set[str], since: str, until: str
              ) -> list[dict[str, Any]]:
    """Mail a member sent (the copy in their own mailbox)."""
    directory, addresses_of, domains = ap._people_maps(people)
    boxes = {f"mail:{m}" for m in who}
    ids = [o for o, _t in _window(st, ("Email.received",), since, until)
           if o.startswith("Email:")]
    out = []
    for start in range(0, len(ids), CHUNK):
        typed = ap._types(st, ids[start:start + CHUNK])
        mine = sorted(o for o, (_t, aud) in typed.items() if aud in boxes)
        rows = ap._read(st, mine, ("sender", "to_addresses", "cc_addresses", "subject",
                                   "body_preview", "conversation_id", "sent_at",
                                   "received_at", "mailbox"))
        links: dict[str, set[str]] = defaultdict(set)
        for row in (st.touching(mine, None) if mine else ()):
            if MENTIONS.match(row[2]) and row[3] in rows:
                links[row[3]].add(row[4])
        for oid, r in rows.items():
            box = ap._v(r, "mailbox").lower()
            sender = (ap._addresses(ap._v(r, "sender")) or [""])[0]
            if box not in who or sender not in addresses_of.get(box, set()):
                continue
            to = ap._addresses(ap._v(r, "to_addresses")) + ap._addresses(ap._v(r, "cc_addresses"))
            outside = [a for a in to if not ap._internal(a, domains)]
            if outside:
                sides = {s for a in outside for s, _o in ap._parties(st, a)}
                side = next(iter(sides)) if len(sides) == 1 else "internal_out"
            else:
                side = "internal"
            out.append({"person": box, "at": ap._v(r, "sent_at") or ap._v(r, "received_at"),
                        "kind": "mail_sent",
                        "workflow": wf.classify(ap._v(r, "subject"), ap._v(r, "body_preview"),
                                                side),
                        "conversation": ap._v(r, "conversation_id") or None,
                        "to": sorted(set(to)), "records": sorted(links.get(oid, ()))})
    return out


def approval_decided(st: Any, people: Any, who: set[str], since: str, until: str
                     ) -> list[dict[str, Any]]:
    directory, _a, _d = ap._people_maps(people)
    ids = [o for o, _t in _window(st, ("MCApproval.decided",), since, until)]
    rows = ap._read(st, ids, ("decided_by", "decided_at"))
    out = []
    for oid, r in rows.items():
        by = ap._v(r, "decided_by").lower()
        person = directory.get(by, by)
        if person in who:
            out.append({"person": person, "at": ap._v(r, "decided_at"), "kind": "approval",
                        "workflow": "internal_approval", "conversation": None, "to": [],
                        "records": [oid]})
    return out


def task_event(st: Any, people: Any, who: set[str], since: str, until: str
               ) -> list[dict[str, Any]]:
    directory, _a, _d = ap._people_maps(people)
    ids = [o for o, _t in _window(st, ("MCTaskEvent.occurred",), since, until)]
    rows = ap._read(st, ids, ("actor", "at"))
    out = []
    for oid, r in rows.items():
        actor = ap._v(r, "actor").lower()
        person = directory.get(actor, actor)
        if person not in who:
            continue
        tasks = [o for _t, o in ap._linked(st, oid) if o.startswith("MCTask:")]
        out.append({"person": person, "at": ap._v(r, "at"), "kind": "task",
                    "workflow": None, "conversation": None, "to": [], "records": tasks})
    return out


def record_actor(st: Any, people: Any, who: set[str], since: str, until: str
                 ) -> list[dict[str, Any]]:
    """Every store event naming a member as its actor (``event_objects.qualifier``)."""
    directory, _a, _d = ap._people_maps(people)
    rule = wf.load()["record_workflow"]
    out = []
    for eid, at, actor in st.conn.execute(
            "SELECT e.event_id, e.time, a.object_id FROM events e JOIN event_objects a ON "
            "a.event_id = e.event_id AND a.qualifier = 'actor' WHERE e.time >= ? AND "
            "e.time < ?", (since, until)):
        key = str(actor).rsplit(":", 1)[-1].lower()
        person = directory.get(key, key)
        if person not in who:
            continue
        recs = [r[0] for r in st.conn.execute(
            "SELECT object_id FROM event_objects WHERE event_id = ? AND qualifier <> 'actor'",
            (eid,))]
        flow = next((rule[r.split(":", 1)[0]] for r in recs if r.split(":", 1)[0] in rule),
                    None)
        out.append({"person": person, "at": at, "kind": "record", "workflow": flow,
                    "conversation": None, "to": [], "records": recs})
    return out


PROVIDERS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "mail_sent": mail_sent, "approval_decided": approval_decided, "task_event": task_event,
    "record_actor": record_actor}


# ----------------------------------------------------------------------------- scoring

def item_keys(item: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, set[str]]:
    work = (item.get("body") or {}).get("prepared") or {}
    records = set(event.get("records") or ())
    if event.get("object_type") not in ("Email", "TeamsChatMessage") and event.get("object_id"):
        records.add(event["object_id"])
    for key in ("purchase_order", "order", "object"):
        if isinstance(work.get(key), Mapping) and work[key].get("object_id"):
            records.add(work[key]["object_id"])
    to = {str(t.get("address")).lower() for t in (work.get("draft") or {}).get("to") or ()
          if isinstance(t, Mapping) and t.get("address")}
    if event.get("sender"):
        to.add(str(event["sender"]).lower())
    conv = {str(event["conversation_id"])} if event.get("conversation_id") else set()
    return {"records": records, "to": to, "conversation": conv}


def score(items: Sequence[Mapping[str, Any]], events: Mapping[str, Mapping[str, Any]],
          actions: Sequence[Mapping[str, Any]], *, horizon: timedelta, data_end: datetime,
          since: datetime, until: datetime) -> dict[str, Any]:
    by_person: dict[str, list[tuple[datetime, Mapping[str, Any]]]] = defaultdict(list)
    for a in actions:
        at = ap._instant(a.get("at"))
        if at is not None:
            by_person[a["person"]].append((at, a))
    for v in by_person.values():
        v.sort(key=lambda x: x[0])
    flows: dict[str, Counter] = defaultdict(Counter)
    anticipated: set[int] = set()
    for item in items:
        event = events.get(item["event_id"]) or {}
        flow = wf.of_item(item["autopilot"], event)
        c = flows[flow]
        c["prepared"] += 1
        at = ap._instant(event.get("at"))
        if event.get("event_type") == "record.due":   # due on the day the cycle found it
            made = ap._instant(item.get("created_at"))
            at = made - timedelta(days=1) if made else None
        if at is None or at + horizon > data_end:
            c["unjudged"] += 1
            continue
        c["judged"] += 1
        keys = item_keys(item, event)
        hit = None
        for when, a in by_person.get(item["owner"], ()):
            if when <= at or when > at + horizon:
                continue
            # the same conversation or a record the item is about: a mail to the same
            # contact about something else is no match
            if (a.get("conversation") and a["conversation"] in keys["conversation"]) or \
                    keys["records"] & set(a.get("records") or ()):
                hit = a
                break
        if hit is not None:
            c["matched"] += 1
            anticipated.add(id(hit))
    for person_actions in by_person.values():
        for when, a in person_actions:
            if since <= when < until:
                flow = a.get("workflow") or "unclassified"
                flows[flow]["actions"] += 1
                flows[flow]["anticipated"] += id(a) in anticipated
    out = {}
    for flow, c in sorted(flows.items()):
        judged = c["judged"]
        out[flow] = {k: c[k] for k in ("prepared", "judged", "matched", "unjudged", "actions",
                                       "anticipated")}
        out[flow]["precision"] = str((Decimal(c["matched"]) / judged).quantize(
            Decimal("0.001"))) if judged else None
        out[flow]["coverage"] = str((Decimal(c["anticipated"]) / c["actions"]).quantize(
            Decimal("0.001"))) if c["actions"] else None
        out[flow]["passes_gate"] = bool(judged >= ap.GATE_MIN_JUDGED and
                                        Decimal(c["matched"]) / judged >= ap.GATE_PRECISION)
    return out


# ----------------------------------------------------------------------------- memory

def available_gb() -> float:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def wait_for_memory(floor_gb: float, wait_s: float, *, probe: Callable[[], float] = available_gb,
                    sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = time.monotonic() + wait_s
    while probe() < floor_gb:
        if time.monotonic() >= deadline:
            return False
        sleep(min(30.0, max(1.0, wait_s / 20)))
    return True


def cap_memory(max_gb: float | None) -> None:
    if not max_gb:
        return
    import resource
    limit = int(max_gb * 1024 ** 3)
    resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))


# ----------------------------------------------------------------------------- run

class Progress:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, "
                          "finished_at TEXT, summary TEXT)")

    def done(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT day FROM days")}

    def finish(self, day: str, summary: Mapping[str, Any]) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO days VALUES (?, ?, ?)",
                              (day, ap._iso(datetime.now(timezone.utc)),
                               json.dumps(summary, sort_keys=True, default=str)))

    def summaries(self) -> list[dict[str, Any]]:
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT summary FROM days ORDER BY day")]

    def close(self) -> None:
        self.conn.close()


def _role_ids(st: Any, ids: Sequence[str], who: set[str]) -> list[str]:
    """The day's records less the mail of mailboxes outside the role (never read)."""
    typed = ap._types(st, ids)
    boxes = {f"mail:{m}" for m in who}
    return [o for o in ids if typed.get(o, ("", ""))[0] != "Email"
            or typed[o][1] in boxes]


def data_end(st: Any, until: str) -> datetime:
    row = st.conn.execute("SELECT MAX(time) FROM events WHERE event_type = 'Email.received' "
                          "AND time <= ?", (until,)).fetchone()
    return ap._instant(row[0]) if row and row[0] else ap._instant(until)


def replay(st: Any, people: Any, *, role: str, out: Path, since: datetime, until: datetime,
           roles: Mapping[str, list[str]], config: Mapping[str, Any],
           registry: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None,
           floor_gb: float = 0.0, wait_s: float = 0.0,
           probe: Callable[[], float] = available_gb, end: datetime | None = None
           ) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    registry = registry or ap.load_registry()
    if people is None or getattr(people, "unavailable", None):
        return {"schema": SCHEMA, "role": role, "status": "blocked",
                "reason": "people_exclusion_unavailable"}
    who = set(members(st, people, role, roles, until=ap._iso(until)))
    if not who:
        return {"schema": SCHEMA, "role": role, "status": "no_members"}
    slug = re.sub(r"[^a-z0-9_]+", "_", role.lower())[:60]
    root = out / slug
    root.mkdir(parents=True, exist_ok=True)
    (root / ap.ROLES_FILE).write_text(json.dumps(
        {"roles": {**roles, **({role: sorted(who)} if not role.startswith("person:") else {})}}))
    queue = ap.Queue(root / ap.QUEUE_FILE)
    progress = Progress(root / "progress.sqlite")
    env = {k: v for k, v in environ.items() if k != ap.ROLES_ENV}
    status = "complete"
    try:
        day = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
        done = progress.done()
        while day < until:
            nxt = min(day + timedelta(days=1), until)
            key = day.date().isoformat()
            if key not in done:
                if not wait_for_memory(floor_gb, wait_s, probe=probe):
                    status = "waiting_for_memory"
                    break
                ids = ap.window_ids(st, registry, ap._iso(max(day, since)), ap._iso(nxt))
                ids = _role_ids(st, ids, who)
                summary = ap.run_cycle(st, queue, ids, people=people, registry=registry,
                                       environ=env, now=nxt, as_of=day.date(),
                                       settle_items=False, only_owners=who)
                if summary.get("status") in ("off", "blocked"):
                    status = summary["status"]
                    break
                summary["drained"] = ap._drain(st, queue, people, registry, nxt,
                                               only_owners=who)
                progress.finish(key, summary)
            day = nxt
        last = end or data_end(st, ap._iso(until + config["horizon"]))
        items = queue.items(sorted(who), ("open", "done", "settled", "dismissed"))
        ev_ids = sorted({i["event_id"] for i in items})
        events = {}
        for start in range(0, len(ev_ids), 500):
            for eid, body in queue.conn.execute(
                    "SELECT event_id, body FROM events WHERE event_id IN (SELECT value FROM "
                    "json_each(?))", (json.dumps(ev_ids[start:start + 500]),)):
                events[eid] = json.loads(body)
        actions: list[dict[str, Any]] = []
        a_until = ap._iso(min(until + config["horizon"], last))
        for provider in config["providers"]:
            actions += provider(st, people, who, ap._iso(since), a_until)
        flows = score(items, events, actions, horizon=config["horizon"], data_end=last,
                      since=since, until=until)
        skipped: Counter = Counter()
        prepared: Counter = Counter()
        for s in progress.summaries():
            skipped.update(s.get("skipped") or {})
            prepared.update(s.get("prepared") or {})
            for k, v in (s.get("drained") or {}).items():
                (skipped if k.startswith("skipped:") else prepared)[
                    k.split(":", 1)[1] if k.startswith("skipped:") else k] += v
        days_done = sorted(progress.done())
    finally:
        queue.close()
        progress.close()
    result = {"schema": SCHEMA, "role": role, "status": status, "members": len(who),
              "window": [ap._iso(since), ap._iso(until)], "data_end": ap._iso(last),
              "horizon_hours": int(config["horizon"].total_seconds() // 3600),
              "days_done": len(days_done), "workflows": flows,
              "prepared_by_autopilot": dict(sorted(prepared.items())),
              "skipped": dict(sorted(skipped.items())), "actions": len(actions),
              "gate": {"precision": str(ap.GATE_PRECISION), "min_judged": ap.GATE_MIN_JUDGED,
                       "passed": sorted(f for f, v in flows.items() if v["passes_gate"])}}
    (root / "result.json").write_text(json.dumps(result, indent=1, sort_keys=True))
    write_gate(out / ap.GATE_FILE, role, sorted(who), flows)
    return result


def write_gate(path: Path, role: str, who: Sequence[str],
               flows: Mapping[str, Mapping[str, Any]]) -> None:
    """The role's counts into the gate file (other roles kept); ``gate_from`` recomputes the
    verdict from them. Copy it beside the live queue to switch passed workflows on."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema") != ap.GATE_SCHEMA:
            raw = {}
    except (OSError, ValueError):
        raw = {}
    roles = dict(raw.get("roles") or {})
    roles[role] = {"members": list(who),
                   "workflows": {f: {"judged": v["judged"], "matched": v["matched"]}
                                 for f, v in flows.items() if v["judged"]}}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"schema": ap.GATE_SCHEMA, "roles": roles}, indent=1,
                              sort_keys=True))
    os.replace(tmp, path)


def main(argv: Sequence[str] | None = None) -> int:
    from . import proactive_engine as pe
    p = argparse.ArgumentParser(prog="python3 -m operating.autopilot_replay")
    p.add_argument("--store", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--role", required=True,
                   help="owner, purchasing, a roles-file role, or person:<address or id>")
    p.add_argument("--days", type=float, default=30.0)
    p.add_argument("--until", default=None, help="window end (ISO), default the data's end")
    p.add_argument("--roles-file", default=None)
    p.add_argument("--capture-state", default=None)
    p.add_argument("--min-available-gb", type=float, default=9.0)
    p.add_argument("--wait-minutes", type=float, default=60.0)
    p.add_argument("--mem-max-gb", type=float, default=8.0)
    a = p.parse_args(argv)
    cap_memory(a.mem_max_gb)
    config = load_config()
    store = Path(a.store)
    people = pe._people(store)
    st = ap._store(a.store)
    if a.capture_state and (Path(a.capture_state) / "mail-capture" / "capture.db").exists():
        from . import autopilot_work as aw
        st.autopilot_capture = aw.CaptureTexts(a.capture_state)
    try:
        until = ap._instant(a.until) if a.until else data_end(
            st, ap._iso(datetime.now(timezone.utc)))
        since = until - timedelta(days=a.days)
        roles = ap._roles({ap.ROLES_ENV: a.roles_file} if a.roles_file else {},
                          Path(a.out))
        result = replay(st, people, role=a.role, out=Path(a.out), since=since, until=until,
                        roles=roles, config=config, floor_gb=a.min_available_gb,
                        wait_s=a.wait_minutes * 60)
    finally:
        st.close()
    print(json.dumps(result, indent=1, sort_keys=True))
    if result.get("status") == "waiting_for_memory":
        return EXIT_NO_MEMORY
    return 0 if result.get("status") == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
