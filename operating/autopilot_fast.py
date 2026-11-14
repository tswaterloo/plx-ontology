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

Nothing here sends, writes to a business system or approves, as for ``follow``.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import time
from datetime import datetime
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
MESSAGE_ROWS = ("chat_message", "channel_message")
SCAN = 2000                   # Teams versions scanned per cycle (memberships, conversations)


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def stores(state_dir: Path) -> dict[str, Path]:
    from .host import state_capture_paths
    paths = state_capture_paths(state_dir)
    return {"teams": paths["teams"], "mail": paths["graph_mail"]}


def _newest(source: str, path: Path) -> tuple[str, int] | None:
    """(the store's generation, its newest version) of one capture store; None: no store."""
    if not path.is_file():
        return None
    with contextlib.closing(_ro(path)) as conn:
        if source == "teams":
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


def take(state_dir: Path, marks: Mapping[str, Any], people: Any, *,
         horizon: datetime) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list],
                                     dict[str, int]]:
    """(source -> the record keys to project, the marks once they are, counts) of the
    message versions past the marks (module doc: oldest first, at most ``MAX_RECORDS``,
    cleared units only)."""
    from .autopilot import _instant
    keys: dict[str, list[tuple[str, str]]] = {"teams": [], "mail": []}
    new_marks: dict[str, list] = {}
    counts = {"versions": 0, "not_cleared": 0, "before_horizon": 0}
    paths = stores(state_dir)
    for source, (generation, mark, newest) in waiting(state_dir, marks).items():
        last = mark
        with contextlib.closing(_ro(paths[source])) as conn:
            if source == "teams":
                rows = conn.execute(
                    "SELECT capture_id, kind, path, unit, payload FROM versions WHERE "
                    "capture_id > ? AND capture_id <= ? AND kind IN (?, ?) ORDER BY capture_id "
                    "LIMIT ?", (mark, newest, *TEAMS_KINDS, SCAN)).fetchall()
                for capture_id, kind, path, unit, payload in rows:
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
                if len(rows) < SCAN and len(keys["teams"]) < MAX_RECORDS:
                    last = newest
            else:
                rows = conn.execute(
                    "SELECT version_id, mailbox_id, message_id FROM messages WHERE "
                    "version_id > ? AND version_id <= ? ORDER BY version_id LIMIT ?",
                    (mark, newest, MAX_RECORDS)).fetchall()
                for version_id, mailbox, message in rows:
                    last = version_id
                    counts["versions"] += 1
                    if not people.unit_clear(f"mail:{mailbox}"):
                        counts["not_cleared"] += 1
                        continue
                    keys["mail"].append((mailbox, message))
                if len(rows) < MAX_RECORDS:
                    last = newest
        keys[source] = list(dict.fromkeys(keys[source]))
        new_marks[source] = [generation, last]
    return keys, new_marks, counts


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


def message_ids(store: Path) -> list[str]:
    from .autopilot import TEAMS_TYPES
    with contextlib.closing(_ro(store)) as conn:
        return [r[0] for r in conn.execute(
            "SELECT object_id FROM objects WHERE object_type IN (?, ?, ?)",
            ("Email", *TEAMS_TYPES))]


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
    keys, new_marks, counts = take(state_dir, marks, people, horizon=horizon)
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
        ids = message_ids(store)
        fast = _Store(store, threads=1, file_only=True)
        try:
            limits = {**registry["limits"], "cycle_seconds": CYCLE_SECONDS}
            out = ap.run_cycle(st, queue, ids, people=people,
                               registry={**registry, "limits": limits}, gate=gate, now=now,
                               read_work_log=True, event_queue=True, horizon=horizon,
                               discover_store=fast, due=False, settle_items=False,
                               environ=environ)
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
        with queue.conn:
            queue.set_meta(MARKS, {**marks, **new_marks})
            queue.set_meta(FAILED, {})
    summary["seconds"] = round(time.monotonic() - started, 2)
    return summary


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
    last = dict(cycles[-1])
    last["cycles"] = [{k: c.get(k) for k in ("status", "taken", "events_new", "take_seconds",
                                              "projected_seconds", "seconds")} for c in cycles]
    last["events_new"] = sum(int(c.get("events_new") or 0) for c in cycles)
    return last
