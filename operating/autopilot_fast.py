"""The autopilot's fast feed: a message the capture just stored is prepared within seconds of
its capture, not after the next delta cycle and the follower (owner, 2026-11-13: a reply
ready in the person's My work about 10 minutes after the message arrived, ideally under 2;
measured before: Teams p50 85 min, p95 134 min from the message to its event).

* **Signal.** A capture store that stores a new person-to-person message version touches
  ``<store>.new`` (``integrations.capture_store.signal_new``: the Teams store per page, the
  mail store per message); ``plx-autopilot-fast.path`` starts ``python3 -m operating.autopilot
  fast`` on it, and its timer every minute in case a signal came while a cycle ran.
* **Take** (``take``): the message versions each store holds past this feed's mark (the
  queue's meta ``fast_marks``: the Teams store's capture id under its generation, the mail
  store's version id), at most ``MAX_RECORDS`` a cycle, oldest first. A first cycle, or a
  store made anew, starts at its newest version and reads nothing before it: the regular
  feed (``follow``) holds the past. Only a unit the people exclusion clears is read: a
  Teams chat whose every member it clears, a channel, a mailbox it decides and clears; an
  unavailable exclusion reads nothing (``blocked``), the marks stay.
* **Approvals** (AUDIT3-10): the Mission Control store (``integrations.mc_sources``, one
  version per approval gate in ``mc:<scope>/approvals``) is read past its mark as the Teams
  store is (capture id under its generation). Only a gate whose version is still requested
  (``pending``, no decision) and was requested inside the horizon is taken, and only when
  the people exclusion clears every person that gate and its task's current row name
  (``mc_clear``: requester, decider, assignees, owner, reporter, any address in either
  row): an approval naming an excluded or undecided person is never projected; an
  exclusion that cannot decide stops the read with the marks kept. Its event is the regular feed's
  ``approval.requested`` (same object id and version digest, so the same event id);
  ``EVENT_PRIORITY`` puts it first. The MC capture signals ``<store>.new`` when it stores
  a requested gate (``mc_sources.signals_new``). With each taken gate its closure is
  projected too (``CLOSURE``: its task's current version and every other gate of that
  task), and the approval is handled with the scratch store's records over the served
  store's (``autopilot.OverlayCtx``): the served store does not hold the new gate yet, nor
  often the decision of the gate before it, so the item states what ``follow``'s does once
  the delta holds them (an earlier approval, the task's title and stage, a newer gate). A
  gate whose other gates name a person the exclusion does not clear (``gate_closure``) is
  withheld on both feeds: its event is never made here (``left_to_follow``, the count's
  name) and ``follow`` withholds it too (``autopilot.approvals_cleared``).
* **Project** (``project``): those records through the one projector (``ontology.delta``
  ``store_subset`` / ``mail_subset``, ``ontology.project.build_store``) into a scratch store
  (``<state>/autopilot/fast-work``, removed after the cycle): the object ids, values and
  handles the delta layer will give them, so the event a later follow cycle types for the
  same message has the same id and is never handled twice. A mail's attachments are left
  out (an event is typed from the message; a handler reads attachment text from the
  capture, ``autopilot_work.CaptureTexts``).
* **Handle**: ``autopilot.run_cycle`` types the events in the scratch store
  (``discover_store``; a sender's side from the served store's parties, the records a message
  names by its identifiers) and handles them over the served store exactly as ``follow``
  does: the people exclusion, the replay gate and Draft, the horizon, the hourly rate limit,
  the ordered event queue, prepare only. No due-date events and no settling (``follow``
  does both), and a shorter cycle budget (``CYCLE_SECONDS``). A batch whose projection or
  handling fails is ``failed`` (the unit fails, its alarm records it) and tried again by the
  next starts; after ``TRIES`` it is left to the regular feed and the marks move past it.

* **Latency** (``LATENCY``, ``latency``): every message a cycle handled leaves one sample in
  the queue's meta (times only, never an id: its source, when it was sent, when the capture
  stored it, when the cycle that handled it ended, i.e. when a reply it prepared was in the
  person's My work), the newest ``LATENCY_KEEP``. ``latency`` gives p50/p95 over a window;
  ``operating.ops_alarms`` pages ``autopilot_reply_latency`` when the p95 from sent to ready
  passes ``READY_MINUTES`` (owner 11-13: 10 minutes) and ``autopilot_fast_waiting`` when
  captured messages wait past the feed's mark that long.

* **Continuous** (``serve``, ``python3 -m operating.autopilot fast --serve``, unit
  ``plx-autopilot-fast-serve.service``; AUDIT3-10): one long-running consumer instead of a
  process per signal. It loads the served store, the people exclusion, the registry, the
  replay gate and Draft once (again after ``RELOAD_SECONDS`` or when a new generation is
  served), sees a capture's signal within ``POLL_SECONDS`` and runs the same cycles over the
  same ordered event queue (``operating.jobq.events``: per conversation in order, an approval
  or a customer's message first), and runs one at least every ``BACKSTOP_SECONDS`` though no
  signal came. It holds ``SERVE_LOCK`` beside the queue for as long as it runs; a start of
  the path or timer unit that finds it held does nothing (``served``), so those units are the
  backstop while the consumer is down, and ``follow`` remains the backstop of both. Each cycle
  a signal woke records ``wake_seconds`` (the signal's time to the cycle's start).
  Its environment is its unit's ``proactive.env``, read once by systemd: before each cycle it
  checks that file (``env_moved``: mtime, then a hash of the content) and ends when it
  changed, or when it changed after the process started, so ``Restart=always`` starts it
  over the new people exclusion or kill switch (``PLX_AUTOPILOT_OFF``) before another cycle
  runs. An unreadable file ends it too. A consumer that fails records it (``SERVE_STATE``);
  the next start waits ``BACKOFF_SECONDS`` doubling to ``BACKOFF_MAX_SECONDS`` before it
  takes the lock (the path and timer starts cover the wait), and ``RESTART_ALARM`` failures
  in a row page as ``autopilot_fast_restarts``.

Nothing here sends, writes to a business system or approves, as for ``follow``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

MARKS = "fast_marks"          # queue meta: where each store was read to
FAILED = "fast_failed"        # queue meta: the marks of a batch that failed, and its tries
TRIES = 3                     # a failing batch is tried this often, then left to follow
MAX_RECORDS = 200             # message versions one cycle projects
CYCLE_SECONDS = 60            # one cycle's handling budget (follow: limits.cycle_seconds)
ROUNDS, ROUNDS_SECONDS = 20, 240   # cycles one start runs while versions wait
WORK_DIR = "fast-work"
TEAMS_KINDS = ("teams_chat", "teams_channel")
MC_KIND = "mission_control"   # the MC capture store's kind (``integrations.mc_sources.KIND``)
MC_TYPES = ("MCApproval", "MCTaskEvent")   # the record types an MC version is typed as here
CLOSURE = "mc_closure"        # ``take``'s key of a taken gate's task and its other gates
STORE_SOURCES = ("teams", "mission_control")   # capture stores read by capture id
MESSAGE_SOURCES = ("teams", "mail")            # the latency alarm's sources (messages)
MESSAGE_ROWS = ("chat_message", "channel_message")
SCAN = 2000                   # Teams versions scanned per cycle (memberships, conversations)
LATENCY = "fast_latency"      # queue meta: [ended, source, sent, captured] of each handled message
LATENCY_KEEP = 500
RAN = "fast_ran"              # queue meta: when a start of this feed last ended
READY_MINUTES = 10            # owner 11-13: a reply in My work within 10 min of the message
SERVE_LOCK = "fast.lock"      # beside the queue: held by the one consumer reading the stores
POLL_SECONDS = 1.0            # serve: a capture's signal is seen within this long
BACKSTOP_SECONDS = 60.0       # serve: a cycle at least this often though no signal came
RELOAD_SECONDS = 600.0        # serve: store, exclusion, registry and gate read afresh
RELEASE_ENV = "PLX_RELEASE_CURRENT"   # serve: the release link its unit runs from
ENV_FILE_ENV = "PLX_PROACTIVE_ENV"    # serve: its unit's EnvironmentFile (proactive.env)
SERVE_STATE = "fast-serve.json"       # beside the queue: the consumer's failures in a row
BACKOFF_SECONDS, BACKOFF_MAX_SECONDS = 15.0, 900.0   # serve: wait 15 s, 30 s, ... 15 min
RESTART_ALARM = 3             # serve: this many failures in a row page (ops_alarms)


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def stores(state_dir: Path) -> dict[str, Path]:
    from .host import state_capture_paths
    paths = state_capture_paths(state_dir)
    return {"teams": paths["teams"], "mail": paths["graph_mail"],
            "mission_control": paths["mission_control"]}


def _newest(source: str, path: Path) -> tuple[str, int] | None:
    """(the store's generation, its newest version) of one capture store; None: no store."""
    if not path.is_file():
        return None
    with contextlib.closing(_ro(path)) as conn:
        if source in STORE_SOURCES:
            generation = conn.execute("SELECT value FROM meta WHERE key = 'generation'"
                                      ).fetchone()
            newest = conn.execute("SELECT MAX(capture_id) FROM versions").fetchone()[0]
            return (generation[0] if generation else "", int(newest or 0))
        newest = conn.execute("SELECT MAX(version_id) FROM messages").fetchone()[0]
        return ("", int(newest or 0))


def waiting(state_dir: Path, marks: Mapping[str, Any]) -> dict[str, tuple[str, int, int]]:
    """source -> (generation, its mark, its newest version) of each store with versions
    past this feed's mark; a store without a mark (or made anew) starts at its newest."""
    out = {}
    for source, path in stores(state_dir).items():
        got = _newest(source, path)
        if got is None:
            continue
        generation, newest = got
        mark = marks.get(source)
        if not (isinstance(mark, list) and len(mark) == 2 and mark[0] == generation
                and isinstance(mark[1], int) and mark[1] <= newest):
            mark = [generation, newest]      # first read, or a store made anew
        out[source] = (generation, mark[1], newest)
    return out


def _first_version(conn: sqlite3.Connection, earlier: str, args: tuple) -> bool:
    """Whether a taken version is its message's first (``earlier`` finds an older version of
    the same message by the store's own index). Only a first version is a latency sample: a
    later one (an edit, a reaction, a moved or re-read mail) was sent long before it was
    stored again, and timing it as a new message read an hour-old Teams edit as an hour's
    reply time (11-17: all 4 Teams samples over 6 min were later versions)."""
    return conn.execute(earlier, args).fetchone() is None


def take(state_dir: Path, marks: Mapping[str, Any], people: Any, *,
         horizon: datetime, times: dict | None = None
         ) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list], dict[str, int]]:
    """(source -> the record keys to project, the marks once they are, counts) of the
    message versions past the marks (module doc: oldest first, at most ``MAX_RECORDS``,
    cleared units only). ``times``: filled with (source, key) -> (sent, captured) of each
    taken message whose version is its first (``_first_version``; a mail's sent is the
    capture's ``received_at``, and a mail received before the horizon, a history read, is
    no sample); an approval's are (requested, captured)."""
    from .autopilot_records import _instant
    keys: dict[str, list[tuple[str, str]]] = {"teams": [], "mail": []}
    new_marks: dict[str, list] = {}
    counts = {"versions": 0, "not_cleared": 0, "before_horizon": 0}
    closure: list[tuple[str, str]] = []
    paths = stores(state_dir)
    for source, (generation, mark, newest) in waiting(state_dir, marks).items():
        last = mark
        keys.setdefault(source, [])
        with contextlib.closing(_ro(paths[source])) as conn:
            if source == "mission_control":
                counts.setdefault("left_to_follow", 0)
                last = _take_approvals(conn, mark, newest, people, horizon, keys[source],
                                       counts, times, closure)
            elif source == "teams":
                rows = conn.execute(
                    "SELECT capture_id, kind, path, unit, payload, recorded_at FROM versions WHERE "
                    "capture_id > ? AND capture_id <= ? AND kind IN (?, ?) ORDER BY capture_id "
                    "LIMIT ?", (mark, newest, *TEAMS_KINDS, SCAN)).fetchall()
                for capture_id, kind, path, unit, payload, recorded in rows:
                    if len(keys["teams"]) >= MAX_RECORDS:
                        break
                    last = capture_id
                    try:
                        row = json.loads(payload).get("row") or {}
                    except (ValueError, AttributeError):
                        continue
                    if row.get("kind") not in MESSAGE_ROWS:
                        continue
                    counts["versions"] += 1
                    if not people.unit_clear(unit):
                        counts["not_cleared"] += 1   # never projected nor read further
                        continue
                    created = _instant(row.get("created_at"))
                    if created is None or created < horizon:
                        counts["before_horizon"] += 1
                        continue
                    keys["teams"].append((kind, path))
                    if times is not None and _first_version(
                            conn, "SELECT 1 FROM versions WHERE kind = ? AND path = ? AND "
                            "capture_id < ? LIMIT 1", (kind, path, capture_id)):
                        times.setdefault(("teams", (kind, path)),
                                         (row.get("created_at"), recorded))
                if len(rows) < SCAN and len(keys["teams"]) < MAX_RECORDS:
                    last = newest
            else:
                rows = conn.execute(
                    "SELECT version_id, mailbox_id, message_id, recorded_at, "
                    "coalesce(json_extract(record, '$.received_at'), "
                    "json_extract(record, '$.receivedDateTime')) FROM messages WHERE "
                    "version_id > ? AND version_id <= ? ORDER BY version_id LIMIT ?",
                    (mark, newest, MAX_RECORDS)).fetchall()
                for version_id, mailbox, message, recorded, sent in rows:
                    last = version_id
                    counts["versions"] += 1
                    if not people.unit_clear(f"mail:{mailbox}"):
                        counts["not_cleared"] += 1
                        continue
                    keys["mail"].append((mailbox, message))
                    received = _instant(sent)
                    if times is not None and (received is None or received >= horizon) \
                            and _first_version(
                                conn, "SELECT 1 FROM messages WHERE mailbox_id = ? AND "
                                "message_id = ? AND version_id < ? LIMIT 1",
                                (mailbox, message, version_id)):
                        times.setdefault(("mail", (mailbox, message)), (sent, recorded))
                if len(rows) < MAX_RECORDS:
                    last = newest
        keys[source] = list(dict.fromkeys(keys[source]))
        new_marks[source] = [generation, last]
    if closure:
        taken = set(keys.get("mission_control") or ())
        keys[CLOSURE] = [k for k in dict.fromkeys(closure) if k not in taken]
    return keys, new_marks, counts


def _take_approvals(conn: sqlite3.Connection, mark: int, newest: int, people: Any,
                    horizon: datetime, keys: list[tuple[str, str]], counts: dict[str, int],
                    times: dict | None, closure: list[tuple[str, str]] | None = None) -> int:
    """``take`` of the MC store: the requested gates' version keys past ``mark`` into
    ``keys``, their tasks' and the tasks' other gates' into ``closure`` (module doc); the
    capture id read to."""
    from .autopilot_records import _instant
    last = mark
    rows = conn.execute(
        "SELECT capture_id, path, unit, payload, recorded_at FROM versions WHERE "
        "capture_id > ? AND capture_id <= ? AND kind = ? ORDER BY capture_id LIMIT ?",
        (mark, newest, MC_KIND, SCAN)).fetchall()
    for capture_id, path, unit, payload, recorded in rows:
        if len(keys) >= MAX_RECORDS:
            break
        last = capture_id
        parts = str(path).split("/")
        if len(parts) != 3 or parts[1] not in ("approvals", "task_events"):
            continue
        try:
            row = json.loads(payload).get("row") or {}
        except (ValueError, AttributeError):
            continue
        if parts[1] == "task_events":
            if isinstance(row, dict) and row.get("kind") == "question.asked":
                _take_question(conn, parts, path, unit, row, recorded, people, horizon, keys,
                               counts, times, closure)
            continue
        if not isinstance(row, dict) or not requested(row):
            continue
        counts["versions"] += 1
        task_id = row.get("task_id")
        task = _current_row(conn, f"{parts[0]}/tasks/{task_id}") \
            if isinstance(task_id, str) and task_id else None
        if not mc_clear(people, unit, row, task):
            counts["not_cleared"] += 1        # never projected nor read further
            continue
        at = _instant(row.get("requestedAt"))
        if at is None or at < horizon:
            counts["before_horizon"] += 1
            continue
        near = gate_closure(conn, parts[0], task_id, path, people, task)
        if near is None:
            counts["left_to_follow"] += 1     # follow withholds it too (approvals_cleared)
            continue
        if closure is not None:
            closure.extend(near)
        keys.append((MC_KIND, path))
        if times is not None:
            times.setdefault(("mission_control", (MC_KIND, path)),
                             (row.get("requestedAt"), recorded))
    if len(rows) < SCAN and len(keys) < MAX_RECORDS:
        last = newest
    return last


def _take_question(conn: sqlite3.Connection, parts: list[str], path: str, unit: str,
                   row: Mapping[str, Any], recorded: Any, people: Any, horizon: datetime,
                   keys: list[tuple[str, str]], counts: dict[str, int], times: dict | None,
                   closure: list[tuple[str, str]] | None) -> None:
    """``take`` of one ``question.asked`` task event (the ``mc_question`` autopilot's): taken
    when the question is still open on its task's current row, was asked inside the horizon
    and ``question_clear`` clears it; its task goes into the closure (the event is typed
    with its task, as ``follow`` types it). Before, MC questions reached the autopilot only
    through ``follow`` (median 31 h after they were asked, 37 of 41 already answered)."""
    from .autopilot_records import _instant
    counts["versions"] += 1
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    task_id = row.get("task_id") or row.get("taskId")
    task = _current_row(conn, f"{parts[0]}/tasks/{task_id}") \
        if isinstance(task_id, str) and task_id else None
    if not task or not question_open(task, payload.get("questionId")):
        counts["answered"] = counts.get("answered", 0) + 1
        return
    if not question_clear(people, unit, row, task):
        counts["not_cleared"] += 1        # never projected nor read further
        return
    at = _instant(row.get("ts"))
    if at is None or at < horizon:
        counts["before_horizon"] += 1
        return
    if closure is not None:
        closure.append((MC_KIND, f"{parts[0]}/tasks/{task_id}"))
    keys.append((MC_KIND, path))
    if times is not None:
        times.setdefault(("mission_control", (MC_KIND, path)), (row.get("ts"), recorded))


def question_open(task: Mapping[str, Any], question_id: Any) -> bool:
    """The task's current row holds the question unanswered and not withdrawn."""
    for q in task.get("questions") or ():
        if isinstance(q, Mapping) and question_id and q.get("id") == question_id:
            return not q.get("answer") and not q.get("withdrawal")
    return False


def question_clear(people: Any, unit: str, event: Mapping[str, Any],
                   task: Mapping[str, Any]) -> bool:
    """May the fast feed read this question? As ``mc_clear`` for a gate: its unit clears,
    and every person the event and its task name does (the asker, the addressee, the
    task's people, every address anywhere in either row). Fail closed."""
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    if not people.unit_clear(unit) or not str(payload.get("addressee") or "").strip():
        return False
    return _named_clear(people, ((event, ("actor",)), (payload, ("addressee",)),
                                 (task, TASK_PEOPLE)))


def gate_closure(conn: sqlite3.Connection, scope: str, task_id: Any, path: str, people: Any,
                 task: Mapping[str, Any] | None) -> list[tuple[str, str]] | None:
    """The version keys of a gate's task (when held) and of every other gate of that task
    the store holds now; None when one of those gates names a person ``mc_clear`` does not
    clear (or its row is unreadable). The sibling-gate rule of both feeds, fail closed: a
    gate with None is never projected here, and ``follow`` withholds it on the gate's
    capture rows (``autopilot.approvals_cleared``)."""
    if not isinstance(task_id, str) or not task_id:
        return []
    out = [(MC_KIND, f"{scope}/tasks/{task_id}")] if task is not None else []
    rows = conn.execute(
        "SELECT path, unit, payload FROM versions WHERE kind = ? AND superseded_at IS NULL "
        "AND path LIKE ? ESCAPE '\\' AND path != ? ORDER BY path",
        (MC_KIND, _like(f"{scope}/approvals/") + "%", path)).fetchall()
    for other, unit, payload in rows:
        try:
            row = json.loads(payload).get("row")
        except (ValueError, AttributeError):
            row = None
        if not isinstance(row, dict):
            if f'"{task_id}"' in str(payload):
                return None                   # it may be this task's: undecided
            continue
        if row.get("task_id") != task_id:
            continue
        if not mc_clear(people, unit, row, task):
            return None
        out.append((MC_KIND, other))
    return out


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def requested(row: Mapping[str, Any]) -> bool:
    """A gate row still requested: status ``pending`` (the ``approval_request`` autopilot's
    only status) and no decision recorded."""
    return (str(row.get("status") or "").strip().lower() == "pending"
            and not row.get("decidedAt") and not row.get("decidedBy"))


def _current_row(conn: sqlite3.Connection, path: str) -> dict[str, Any] | None:
    """The current version's row of one MC record; {} when it is held but unreadable (its
    addresses cannot be checked: ``mc_clear`` then withholds), None when not held."""
    found = conn.execute("SELECT payload FROM versions WHERE kind = ? AND path = ? AND "
                         "superseded_at IS NULL", (MC_KIND, path)).fetchone()
    if found is None:
        return None
    try:
        row = json.loads(found[0]).get("row")
    except (ValueError, AttributeError):
        return {}
    return row if isinstance(row, dict) else {}


def capture_rows(conn: sqlite3.Connection, scope: Any, gate_id: Any
                 ) -> tuple[str, dict[str, Any], dict[str, Any] | None] | None:
    """(unit, the gate's current row, its task's current row or None) of one gate as the
    MC capture holds it now, the rows ``mc_clear`` reads (co-assignees, activity, comments,
    the proposal: fields the projection does not hold); None when the capture does not
    hold the gate. An unreadable gate row is {} (``mc_clear`` withholds it)."""
    if not isinstance(scope, str) or not isinstance(gate_id, str) or not scope or \
            not gate_id or "/" in scope or "/" in gate_id:
        return "", {}, None
    found = conn.execute("SELECT unit, payload FROM versions WHERE kind = ? AND path = ? AND "
                         "superseded_at IS NULL", (MC_KIND, f"{scope}/approvals/{gate_id}")
                         ).fetchone()
    if found is None:
        return None
    try:
        row = json.loads(found[1]).get("row")
    except (ValueError, AttributeError):
        row = None
    if not isinstance(row, dict):
        return str(found[0] or ""), {}, None
    task_id = row.get("task_id")
    task = _current_row(conn, f"{scope}/tasks/{task_id}") \
        if isinstance(task_id, str) and task_id else None
    return str(found[0] or ""), row, task


_ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


GATE_PEOPLE = ("requestedBy", "decidedBy")
TASK_PEOPLE = ("assignee", "coassignees", "accountableOwner", "reporter")


def mc_clear(people: Any, unit: str, gate: Mapping[str, Any],
             task: Mapping[str, Any] | None) -> bool:
    """May the fast feed read this approval? Its unit clears, and every person it names
    does (``person_clear``): the gate's requester (required) and decider, its task's
    assignees, accountable owner and reporter, and every address anywhere in the gate's
    row or its task's current row. A task held but unreadable withholds it. Fail closed: a
    check that raises stops the read (``take``'s caller keeps the marks). The regular feed
    applies the same rule to its projected approvals (``autopilot.approvals_cleared``)."""
    if task is not None and not task:
        return False
    if not isinstance(gate.get("requestedBy"), str) or not gate["requestedBy"].strip():
        return False                      # who asked is not known: nothing decided
    if not people.unit_clear(unit):
        return False
    return _named_clear(people, ((gate, GATE_PEOPLE), (task or {}, TASK_PEOPLE)))


def _named_clear(people: Any, rows: Any) -> bool:
    """Every person the rows name (their ``fields``, and every address anywhere in each
    row) is cleared (``person_clear``); a person field of another shape: undecided."""
    named: set[str] = set()
    for row, fields in rows:
        for field in fields:
            value = row.get(field)
            for one in value if isinstance(value, list) else [value]:
                if one is None or one == "":
                    continue
                if not isinstance(one, str):
                    return False          # a person field of another shape: undecided
                named.add(one.strip().lower())
        named.update(a.lower() for a in _ADDRESS.findall(json.dumps(row, default=str)))
    named.discard("")
    return all(person_clear(people, who) for who in sorted(named))


def person_clear(people: Any, value: str) -> bool:
    """One named person: an address or object id as ``identity_clear`` decides it; a bare
    handle (MC's ``vince``) is the directory's addresses with that local part, each of
    which must clear; a handle no address has is undecided (withheld). A value written
    ``@vince`` (or ``vince@host`` with no dot in the domain) is a handle, never an outside
    address."""
    if value.startswith("@") or ("@" in value and "." not in value.rsplit("@", 1)[1]):
        value = value.lstrip("@").split("@", 1)[0]
        if not value:
            return False                  # nobody named: undecided
    elif "@" in value:
        return bool(people.identity_clear(value))
    directory = getattr(people, "directory", None) or {}
    matches = [a for a in directory if str(a).lower().split("@", 1)[0] == value]
    if matches:
        return all(people.identity_clear(a) for a in matches)
    return bool(people.identity_clear(value))   # an object id decides; anything else no


def project(state_dir: Path, work: Path, keys: Mapping[str, list[tuple[str, str]]],
            environ: Mapping[str, str]) -> Path:
    """The scratch store of these records, projected by the one projector."""
    from ontology import delta
    from ontology import project as projector
    from ontology.schema import load_schema
    store = work / "fast.sqlite"
    with contextlib.ExitStack() as stack:
        sources: dict[str, Any] = {}
        if keys.get("teams"):
            sub = work / "teams.db"
            delta.store_subset(stores(state_dir)["teams"], sub, sorted(keys["teams"]))
            sources["teams"] = stack.enter_context(projector.capture_source("teams", sub))
        if keys.get("mission_control"):
            sub = work / "mc.db"
            delta.store_subset(stores(state_dir)["mission_control"], sub,
                               sorted({*keys["mission_control"], *(keys.get(CLOSURE) or ())}))
            sources["mission_control"] = stack.enter_context(
                projector.capture_source("mission_control", sub))
        if keys.get("mail"):
            delta.mail_subset(state_dir, work, sorted(keys["mail"]))
            with contextlib.closing(sqlite3.connect(work / "mail-capture" / "capture.db")
                                    ) as conn, conn:
                conn.execute("DELETE FROM attachment_text_versions")
                conn.execute("DELETE FROM attachments")
            sources["graph_mail"] = stack.enter_context(projector.MailCaptures(
                work, internal_domains=delta._internal_domains(environ)))
        projector.build_store(store, load_schema(), sources)
    return store


def message_ids(store: Path, keys: Mapping[str, list[tuple[str, str]]] | None = None
                ) -> list[str]:
    """The objects of the scratch store ``run_cycle`` types: the messages and the MC
    approvals; with ``keys`` (``take``'s), only the gates taken, never the closure's
    other gates (read, never typed here)."""
    from .autopilot_events import TEAMS_TYPES
    types = ("Email", *TEAMS_TYPES, *MC_TYPES)
    with contextlib.closing(_ro(store)) as conn:
        rows = conn.execute(
            "SELECT object_id, object_type FROM objects WHERE object_type IN (SELECT value "
            "FROM json_each(?))", (json.dumps(types),)).fetchall()
        if keys is None:
            return [r[0] for r in rows]
        gates = [r[0] for r in rows if r[1] == "MCApproval"]
        # MC's gate ids (``apg_...``) are its own, one per gate in every scope
        taken = {str(path).rsplit("/", 1)[-1] for _k, path in
                 keys.get("mission_control") or ()}
        keep = {oid for oid, gid in conn.execute(
            "SELECT object_id, value FROM object_values WHERE superseded_at IS NULL AND "
            "property = 'gate_id' AND object_id IN (SELECT value FROM json_each(?))",
            (json.dumps(gates),)) if str(gid) in taken} if gates else set()
        # a question's task event by its scope and event id (its native key)
        asked = {tuple(str(path).split("/")[::2]) for _k, path in
                 keys.get("mission_control") or () if "/task_events/" in str(path)}
        events = [r[0] for r in rows if r[1] == "MCTaskEvent"]
        if events and asked:
            for oid, native in conn.execute(
                    "SELECT object_id, native_key FROM objects WHERE object_id IN (SELECT "
                    "value FROM json_each(?))", (json.dumps(events),)):
                try:
                    raw = json.loads(native)
                except (TypeError, ValueError):
                    continue
                if isinstance(raw, dict) and (str(raw.get("scope")),
                                              str(raw.get("event_id"))) in asked:
                    keep.add(oid)
        return [r[0] for r in rows if r[1] not in MC_TYPES or r[0] in keep]


def cycle(state_dir: Path, st: Any, queue: Any, people: Any, registry: Mapping[str, Any], *,
          gate: Callable[..., bool], now: datetime, environ: Mapping[str, str]
          ) -> dict[str, Any]:
    """One fast cycle (module doc); the marks move only when it handled what it took."""
    from . import autopilot as ap
    from .proactive_engine import _Store
    started = time.monotonic()
    summary: dict[str, Any] = {"schema": ap.SCHEMA, "feed": "fast"}
    if ap.killed(queue, environ):
        return {**summary, "status": "off"}
    if people is None or getattr(people, "unavailable", None):
        return {**summary, "status": "blocked", "reason": "people_exclusion_unavailable"}
    marks = queue.meta(MARKS) or {}
    horizon = now - ap.FOLLOW_HORIZON
    times: dict = {}
    keys, new_marks, counts = take(state_dir, marks, people, horizon=horizon, times=times)
    summary["taken"] = {**counts, **{s: len(k) for s, k in keys.items()}}
    summary["take_seconds"] = round(time.monotonic() - started, 2)
    if not any(keys.values()):
        with queue.conn:
            queue.set_meta(MARKS, {**marks, **new_marks})
        return {**summary, "status": "complete", "events_new": 0,
                "seconds": round(time.monotonic() - started, 2)}
    work = queue.path.parent / WORK_DIR
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(mode=0o700, parents=True)
    try:
        store = project(state_dir, work, keys, environ)
        summary["projected_seconds"] = round(time.monotonic() - started, 2)
        ids = message_ids(store, keys)
        fast = _Store(store, threads=1, file_only=True)
        # a question's event is filled from the MC capture (``_question_event``)
        fast.autopilot_mc = getattr(st, "autopilot_mc", None)
        try:
            limits = {**registry["limits"], "cycle_seconds": CYCLE_SECONDS}
            from .chat_ask_model import from_environ as chat_ask_judge
            out = ap.run_cycle(st, queue, ids, people=people,
                               registry={**registry, "limits": limits}, gate=gate, now=now,
                               read_work_log=True, event_queue=True, horizon=horizon,
                               discover_store=fast, due=False, settle_items=False,
                               environ=environ, ask_judge=chat_ask_judge(environ))
        finally:
            fast.close()
    except Exception as error:  # noqa: BLE001 - a batch that fails is retried, then left
        # to the regular feed (``follow`` types the same records once the delta holds them):
        # one record that always fails never stalls this feed. Code only, never a message.
        failed = queue.meta(FAILED) or {}
        tries = int(failed.get("tries") or 0) + 1 if failed.get("marks") == marks else 1
        given_up = tries >= TRIES
        with queue.conn:
            queue.set_meta(FAILED, {"marks": marks, "tries": tries})
            if given_up:
                queue.set_meta(MARKS, {**marks, **new_marks})
        out = {"status": "failed", "reason": f"{type(error).__name__}", "tries": tries,
               "left_to_follow": given_up}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    summary.update({k: v for k, v in out.items() if k != "schema"})
    if out.get("status") in ("complete", "partial"):
        ended = now + timedelta(seconds=time.monotonic() - started)
        samples = [[_iso(ended), source, sent, captured]
                   for (source, _key), (sent, captured) in times.items()]
        with queue.conn:
            queue.set_meta(MARKS, {**marks, **new_marks})
            queue.set_meta(FAILED, {})
            if samples:
                kept = (queue.meta(LATENCY) or []) + samples
                queue.set_meta(LATENCY, kept[-LATENCY_KEEP:])
        summary["latency"] = latency(samples, ended, window=None)
    summary["seconds"] = round(time.monotonic() - started, 2)
    return summary


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def _when(text: Any) -> datetime | None:
    from .autopilot_records import _instant
    return _instant(text) if isinstance(text, str) and text else None


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, int(-(-q * len(ordered) // 1)) - 1))], 1)


def latency(samples: Any, now: datetime, *, window: timedelta | None = timedelta(hours=2)
            ) -> dict[str, Any]:
    """p50/p95 seconds from sent to ready and from captured to ready of the samples whose
    cycle ended inside ``window`` (None: all), by source and together (``all``: the
    messages only, the reply alarm's; an approval's ``sent`` is when it was requested, its
    own ``mission_control`` entry). Counts and seconds only. An unreadable sample is
    counted (``unreadable``), never guessed."""
    out: dict[str, Any] = {"messages": 0, "approvals": 0, "unreadable": 0}
    sent_ready: dict[str, list[float]] = {"all": []}
    captured_ready: dict[str, list[float]] = {"all": []}
    for sample in samples if isinstance(samples, list) else ():
        try:
            ended, source, sent, captured = sample
        except (TypeError, ValueError):
            out["unreadable"] += 1
            continue
        end, cap = _when(ended), _when(captured)
        if end is None or cap is None or source not in (*MESSAGE_SOURCES, "mission_control"):
            out["unreadable"] += 1
            continue
        if window is not None and not now - window <= end <= now + timedelta(minutes=1):
            continue
        keys = ("all", source) if source in MESSAGE_SOURCES else (source,)
        out["messages" if source in MESSAGE_SOURCES else "approvals"] += 1
        for key in keys:
            captured_ready.setdefault(key, []).append(max(0.0, (end - cap).total_seconds()))
        sent_at = _when(sent)
        if sent_at is not None:
            for key in keys:
                sent_ready.setdefault(key, []).append(max(0.0, (end - sent_at).total_seconds()))
    for key in ("all", *MESSAGE_SOURCES, "mission_control"):
        if captured_ready.get(key) or sent_ready.get(key):
            out[key] = {"messages": len(captured_ready.get(key) or ()),
                        "timed": len(sent_ready.get(key) or ()),
                        "captured_to_ready_p50_s": _pct(captured_ready.get(key) or [], 0.5),
                        "captured_to_ready_p95_s": _pct(captured_ready.get(key) or [], 0.95),
                        "sent_to_ready_p50_s": _pct(sent_ready.get(key) or [], 0.5),
                        "sent_to_ready_p95_s": _pct(sent_ready.get(key) or [], 0.95)}
    return out


def run(state_dir: Path, st: Any, queue: Any, people: Any, registry: Mapping[str, Any], *,
        gate: Callable[..., bool], environ: Mapping[str, str],
        clock: Callable[[], datetime]) -> dict[str, Any]:
    """Cycles while message versions wait past the marks (at most ``ROUNDS`` cycles and
    ``ROUNDS_SECONDS``): a capture that kept storing while one ran is read at once."""
    started = time.monotonic()
    cycles = []
    for _ in range(ROUNDS):
        out = cycle(state_dir, st, queue, people, registry, gate=gate, now=clock(),
                    environ=environ)
        cycles.append(out)
        left = waiting(state_dir, queue.meta(MARKS) or {})
        if out.get("status") not in ("complete", "partial") or \
                time.monotonic() - started > ROUNDS_SECONDS or \
                all(mark >= newest for _g, mark, newest in left.values()):
            break
    with queue.conn:
        queue.set_meta(RAN, _iso(clock()))
    last = dict(cycles[-1])
    last["cycles"] = [{k: c.get(k) for k in ("status", "taken", "events_new", "take_seconds",
                                              "projected_seconds", "seconds")} for c in cycles]
    last["events_new"] = sum(int(c.get("events_new") or 0) for c in cycles)
    return last


def alarm_checks(state_dir: Path, now: datetime) -> list[dict[str, Any]]:
    """The continuous consumer's restart loop (``restart_check``), the feed's own two
    alarms (``feed_checks``) and the settle pass's failed checks (``settle_checks``)."""
    return restart_check(state_dir) + feed_checks(state_dir, now) + \
        settle_checks(state_dir, now)


def settle_checks(state_dir: Path, now: datetime) -> list[dict[str, Any]]:
    """``autopilot_settle_errors``: the last settle pass (``autopilot_engine.settle``, its
    note in the queue's meta) could not check some open items, which then stay open past
    the owner's reply or decision. Any failure alarms; a note older than a day or none:
    nothing is checked (the cycle's own unit alarms cover a cycle that stopped)."""
    from .autopilot_engine import SETTLE_ERRORS
    from .autopilot_queue import QUEUE_FILE
    from .ops_health import _check
    path = state_dir / "autopilot" / QUEUE_FILE
    if not path.is_file():
        return []
    try:
        with contextlib.closing(_ro(path)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?",
                               (SETTLE_ERRORS,)).fetchone()
        note = json.loads(row[0]) if row else None
        if note is not None and not isinstance(note, dict):
            raise ValueError("note")
    except (sqlite3.Error, ValueError):
        return [_check("autopilot_settle_errors", None, 0, None, "checks",
                       "the autopilot queue's settle note is unreadable")]
    at = _when(note.get("at")) if note else None
    if at is None or now - at > timedelta(days=1):
        return []
    try:
        errors, checked = int(note.get("errors") or 0), int(note.get("checked") or 0)
    except (TypeError, ValueError):
        return [_check("autopilot_settle_errors", None, 0, None, "checks",
                       "the autopilot queue's settle note is unreadable")]
    rules = ", ".join(f"{k} {v}" for k, v in sorted((note.get("by_rule") or {}).items()))
    return [_check("autopilot_settle_errors", errors, 0, errors > 0, "checks",
                   f"{errors} of {checked} open items could not be checked for done "
                   f"({rules}); they stay open: {str(note.get('last_error') or '')[:160]}"
                   if errors else f"all {checked} open items checked")]


def last_ran(queue: Path) -> str | None:
    """When a start of the fast feed last ended (the queue's ``RAN`` meta, an ISO instant),
    read-only; None when the feed never ran on this queue or the queue cannot be read."""
    if not Path(queue).is_file():
        return None
    try:
        with contextlib.closing(sqlite3.connect(f"file:{queue}?mode=ro", uri=True,
                                                timeout=2)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (RAN,)).fetchone()
        ran = _when(json.loads(row[0])) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return None
    return ran.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z") if ran else None


def feed_checks(state_dir: Path, now: datetime) -> list[dict[str, Any]]:
    """The fast feed's two alarms (module doc) as ``ops_health`` checks, once the feed has
    run in the last day (switched off or never enabled: nothing is checked; its units'
    own alarms cover a unit that failed). Reads the queue's meta and each capture store's
    oldest version past the mark (one indexed row), read-only; times and counts only."""
    from .autopilot_queue import QUEUE_FILE
    from .ops_health import _check
    path = state_dir / "autopilot" / QUEUE_FILE
    if not path.is_file():
        return []
    try:
        with contextlib.closing(_ro(path)) as conn:
            meta = {k: json.loads(v) for k, v in conn.execute(
                "SELECT key, value FROM meta WHERE key IN (?, ?, ?)", (RAN, MARKS, LATENCY))}
    except (sqlite3.Error, ValueError):
        return [_check("autopilot_reply_latency", None, READY_MINUTES, None, "minutes",
                       "the autopilot queue is unreadable")]
    ran = _when(meta.get(RAN))
    if ran is None or now - ran > timedelta(days=1):
        return []
    found = latency(meta.get(LATENCY), now)
    p95 = ((found.get("all") or {}).get("sent_to_ready_p95_s"))
    minutes = None if p95 is None else round(p95 / 60, 1)
    checks = [_check("autopilot_reply_latency", minutes if minutes is not None else 0,
                     READY_MINUTES, minutes is not None and minutes > READY_MINUTES, "minutes",
                     f"p95 {minutes} min from a message to its prepared reply over the "
                     f"{found['all']['timed']} new messages with a sent time of "
                     f"{found['messages']} handled in the last 2 h" if minutes is not None
                     else "no message handled in the last 2 h")]
    oldest = None
    try:
        for source, (_generation, mark, newest) in waiting(
                state_dir, meta.get(MARKS) or {}).items():
            if mark >= newest:
                continue
            table, column = (("versions", "capture_id") if source in STORE_SOURCES
                             else ("messages", "version_id"))
            with contextlib.closing(_ro(stores(state_dir)[source])) as conn:
                row = conn.execute(f"SELECT recorded_at FROM {table} WHERE {column} > ? "
                                   f"ORDER BY {column} LIMIT 1", (mark,)).fetchone()
            at = _when(row[0]) if row else None
            if at is not None and (oldest is None or at < oldest):
                oldest = at
    except (sqlite3.Error, OSError):
        checks.append(_check("autopilot_fast_waiting", None, READY_MINUTES, None, "minutes",
                             "a capture store is unreadable"))
        return checks
    waited = 0.0 if oldest is None else round(max(0.0, (now - oldest).total_seconds()) / 60, 1)
    checks.append(_check("autopilot_fast_waiting", waited, READY_MINUTES,
                         waited > READY_MINUTES, "minutes",
                         f"captured messages have waited {waited} min for the fast feed"
                         if waited else "nothing waits"))
    return checks


# ---------------------------------------------------------------- continuous (AUDIT3-10)

def signal_times(state_dir: Path) -> dict[str, float]:
    """Each store's last signal (``<store>.new``'s modification time); a store that never
    signalled is absent."""
    from integrations.capture_store import NEW_SIGNAL
    out = {}
    for source, path in stores(state_dir).items():
        try:
            out[source] = Path(f"{path}{NEW_SIGNAL}").stat().st_mtime
        except OSError:
            continue
    return out


@contextlib.contextmanager
def consumer_lock(queue_dir: Path, *, wait: bool):
    """``SERVE_LOCK`` beside the queue: True while held. ``wait`` False gives False at once
    when another start holds it (a path or timer start while the consumer runs)."""
    import fcntl
    queue_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(queue_dir / SERVE_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            held = True
        except BlockingIOError:
            held = False
        yield held
    finally:
        os.close(fd)


def idle(state_dir: Path, queue: Any, environ: Mapping[str, str]) -> str | None:
    """``off`` (the kill switch) or ``idle`` (nothing stored past the marks: no store is
    opened), else None. A first start, or a store first read (the MC store added), sets
    its mark at the store's newest version: a store's past is the regular feed's, and a
    version stored after it is read though no other store moves."""
    from . import autopilot as ap
    if ap.killed(queue, environ):
        return "off"
    left = waiting(state_dir, queue.meta(MARKS) or {})
    if any(m < n for _g, m, n in left.values()):
        return None
    held = queue.meta(MARKS) or {}
    unmarked = {s: [g, n] for s, (g, _m, n) in left.items() if s not in held}
    if unmarked:
        with queue.conn:
            queue.set_meta(MARKS, {**held, **unmarked})
    return "idle"


def release_moved(environ: Mapping[str, str]) -> Callable[[], bool]:
    """True once the release link (``RELEASE_ENV``) points elsewhere than when the consumer
    started: it then ends, and its unit (``Restart=always``) starts it on the new release."""
    link = environ.get(RELEASE_ENV)
    if not link:
        return lambda: False
    started = os.path.realpath(link)
    return lambda: os.path.realpath(link) != started


def env_file(environ: Mapping[str, str]) -> Path:
    """The consumer unit's ``EnvironmentFile`` (``ENV_FILE_ENV``, else the user's own)."""
    named = environ.get(ENV_FILE_ENV)
    return Path(named) if named else Path.home() / ".config" / "plx" / "proactive.env"


def process_started() -> float:
    """This process's start (epoch seconds, from /proc); now where /proc is not there."""
    try:
        ticks = int(Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19])
        boot = next(int(ln.split()[1]) for ln in Path("/proc/stat").read_text().splitlines()
                    if ln.startswith("btime "))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return time.time()


def env_moved(path: Path, *, started: float | None = None,
              now: Callable[[], float] = time.time) -> Callable[[], bool]:
    """True once ``path`` (the unit's env file) differs from what this process was started
    with: its content hash changed since the first check, or it was written after the
    process started (an edit while the consumer waited for the lock), or it cannot be read
    (fail closed). A touch that leaves the content alone is not a change. The file is only
    hashed when its mtime or size moved."""
    import hashlib
    started = process_started() if started is None else started

    def look() -> tuple[tuple[int, int] | None, str | None]:
        try:
            st = path.stat()
        except FileNotFoundError:
            return None, None
        return (st.st_mtime_ns, st.st_size), hashlib.sha256(path.read_bytes()).hexdigest()

    try:
        seen, digest = look()
    except OSError:
        return lambda: True
    late = seen is not None and started < seen[0] / 1e9 <= now()
    held = [seen]

    def moved() -> bool:
        if late:
            return True
        try:
            st = path.stat()
            key: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            key = None
        except OSError:
            return True
        if key == held[0]:
            return False
        try:
            key, now_digest = look()
        except OSError:
            return True
        held[0] = key
        return now_digest != digest

    return moved


def _serve_state(queue_dir: Path) -> dict[str, Any]:
    try:
        found = json.loads((queue_dir / SERVE_STATE).read_text())
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_serve_state(queue_dir: Path, value: Mapping[str, Any]) -> None:
    tmp = queue_dir / f".{SERVE_STATE}.tmp"
    tmp.write_text(json.dumps(value, sort_keys=True))
    os.replace(tmp, queue_dir / SERVE_STATE)


def serve_failed(queue_dir: Path, error: BaseException, now: datetime) -> None:
    """Record one more failure in a row of the consumer (its kind only, never its text)."""
    held = _serve_state(queue_dir)
    failures = int(held.get("failures") or 0) + 1
    _write_serve_state(queue_dir, {"failures": failures, "at": now.isoformat(),
                                   "since": held.get("since") or now.isoformat(),
                                   "error": type(error).__name__})


def serve_ok(queue_dir: Path) -> None:
    """A cycle ran: the failures in a row end."""
    if int(_serve_state(queue_dir).get("failures") or 0):
        _write_serve_state(queue_dir, {"failures": 0})


def backoff_seconds(queue_dir: Path, now: datetime) -> float:
    """How long a start waits after ``n`` failures in a row (``BACKOFF_SECONDS`` doubling to
    ``BACKOFF_MAX_SECONDS``), less the time since the last one."""
    held = _serve_state(queue_dir)
    failures = int(held.get("failures") or 0)
    at = _when(held.get("at"))
    if failures <= 0 or at is None:
        return 0.0
    wait = min(BACKOFF_SECONDS * 2 ** (failures - 1), BACKOFF_MAX_SECONDS)
    return max(0.0, min(wait, wait - (now - at).total_seconds()))


def restart_check(state_dir: Path) -> list[dict[str, Any]]:
    """``autopilot_fast_restarts``: the consumer failing ``RESTART_ALARM`` times in a row (a
    load error its unit restarts forever); nothing before it first ran."""
    from .autopilot_queue import QUEUE_FILE
    from .ops_health import _check
    queue_dir = (state_dir / "autopilot" / QUEUE_FILE).parent
    if not (queue_dir / SERVE_STATE).is_file():
        return []
    held = _serve_state(queue_dir)
    failures = int(held.get("failures") or 0)
    return [_check("autopilot_fast_restarts", failures, RESTART_ALARM,
                   failures >= RESTART_ALARM, "failures",
                   f"the continuous consumer failed {failures} times in a row since "
                   f"{held.get('since')} (last: {held.get('error')} at {held.get('at')})"
                   if failures else "the continuous consumer runs")]


def serve(state_dir: Path, queue: Any, load: Callable[[], Any], *,
          environ: Mapping[str, str], clock: Callable[[], datetime],
          out: Callable[[dict[str, Any]], None], stop: Callable[[], bool],
          sleep: Callable[[float], None] = time.sleep,
          monotonic: Callable[[], float] = time.monotonic,
          poll_s: float = POLL_SECONDS, backstop_s: float = BACKSTOP_SECONDS,
          reload_s: float = RELOAD_SECONDS) -> int:
    """The continuous consumer (module doc): cycles until ``stop``; how many ran. ``load``
    gives what a start loads (``run(clock)``, ``stale()``, ``close()``); an error it raises
    ends the consumer (its unit restarts it; the path and timer starts cover the gap)."""
    seen = signal_times(state_dir)
    ctx, loaded, last, ran = None, 0.0, None, 0
    try:
        while not stop():
            signals = signal_times(state_dir)
            fresh = [t for s, t in signals.items() if t > seen.get(s, float("-inf"))]
            if not fresh and last is not None and monotonic() - last < backstop_s:
                sleep(poll_s)
                continue
            seen, last = signals, monotonic()
            if idle(state_dir, queue, environ):
                continue
            if ctx is None or monotonic() - loaded >= reload_s or ctx.stale():
                if ctx is not None:
                    ctx.close()
                    ctx = None
                ctx, loaded = load(), monotonic()
            started = clock()
            summary = dict(ctx.run(clock))
            if fresh:
                summary["wake_seconds"] = round(max(0.0, started.timestamp() - max(fresh)), 3)
            summary["serve"] = True
            out(summary)
            ran += 1
            last = monotonic()
    finally:
        if ctx is not None:
            ctx.close()
    return ran
