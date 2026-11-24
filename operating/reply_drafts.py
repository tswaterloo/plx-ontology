"""Real drafts for the open-replies list ("needs my reply"), made in the background.

The list (``inbox_triage.triage``, ``/api/replies``) carried one fixed holding sentence on
every line: no line ever got a model draft, since the shared drafter
(``autopilot_drafter``: the person's memory and learned style, the counterpart's open
orders and invoices, the records the message names, record lookups as the person when
every reader is ours) ran only on autopilot events. Here that same drafter runs over each
person's open reply lines, the way ``autopilot.dry_drafts`` writes a draft for a grade
(same events, owners, screens, handler, material, style and writer; nothing is written to
any queue, nothing is sent), and the result is kept by message id (``Kept``) beside the
other state stores:

* ``python3 -m operator_api.autopilot_context assist-drafts --state-dir <state>`` (a
  timer: ``deploy/systemd/plx-reply-drafts.*``) picks the open ``reply_owed`` and
  ``needs_my_reply`` lines no draft is kept for (``wanted``), heaviest first, at most
  ``PER_PERSON`` per person and ``PER_RUN`` a run, and drafts them ``PARALLEL`` at once,
  each worker on its own store. A line is marked ``drafting`` before its call, so the page
  says so; ``ready`` (a reply, or a ``your_call`` question with its options' replies) or
  ``none`` (why: a screen, nothing worth sending) after. A ``drafting`` mark older than
  ``STALE_MINUTES`` (a run that died) is drafted again; a ``none`` is tried again after
  ``RETRY_HOURS``, except a judgment on the message itself (``JUDGED``: the Assistant or
  the reply sort found nothing to send), which stays like a ready draft: the line is one
  message, so a new message is a new line. Measured on the live store, a second session on
  10 lines judged nothing to do found the same on 9.
* Before the session, a small model sorts out a message plainly owed no reply
  (``autopilot_agent.REPLY_SCREEN``, ``SCREEN_ENV``, default ``SCREEN``; ``off``: none),
  after the same people, pay/HR and Freightcom screens; a failed sort lets the session
  decide.
* ``/api/replies`` never waits on any of it: ``attach`` puts the kept draft on its line
  (the reply card's body then is the model's) or the state (``drafting``, ``queued``),
  and the holding sentence stays until a draft is ready.

Drafts only: nothing here sends, confirms or writes to a business system. A change the
Assistant proposes in a business system becomes a job item on the person's autopilot queue
(``keep_jobs``), prepared and confirmed there by that person only.
Evidence: isolated-fixture (``operating/tests/test_reply_drafts.py``); live-unverified.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from operating import declared_schema as _schemas
from operating.declared_schema import reply_drafts as _REPLY_DRAFTS

DIR = "reply-drafts"
FILE = "reply-drafts.sqlite"
PARALLEL_ENV = "PLX_REPLY_DRAFTS_PARALLEL"
PARALLEL = 3          # drafts at once (each a model process and a reader host)
PER_PERSON_ENV = "PLX_REPLY_DRAFTS_PER_PERSON"
PER_PERSON = 6        # lines one person gets drafted a run (the rest wait for the next)
PER_RUN_ENV = "PLX_REPLY_DRAFTS_PER_RUN"
PER_RUN = 24
STALE_MINUTES = 30
RETRY_HOURS = 12
# a line left without a draft only because its person had no session yet, or the host
# refused or did not start it (not a judgment on the line): tried again soon, so a person
# given a session gets drafts in the next runs
SETUP_GAPS = ("agent_no_session", "draft_failed:HostListRefused")
SETUP_RETRY_MINUTES = 30
# a line's own message judged: nothing for the person to send (the Assistant's session, the
# reply sort) or only changes kept on their queue; never drafted again on a timer
JUDGED = ("nothing_to_do", "no_reply_wanted", "changes_prepared")
SCREEN_ENV = "PLX_REPLY_DRAFTS_SCREEN_MODEL"
SCREEN = "haiku"
FORCE = "reply_prep"  # the drafting autopilot for a line no live rule matched
KINDS = ("reply_owed", "needs_my_reply")   # ``inbox_triage.TRIAGE_KINDS``
STATES = ("drafting", "ready", "none")

SCHEMA = _REPLY_DRAFTS._DDL


def _now() -> datetime:
    return datetime.now(timezone.utc)


def path_of(state_dir: Path | str) -> Path:
    return Path(state_dir) / DIR / FILE


# "never for this sender" (``operator_api.autopilot_area.NEVER_FILE``, under the state dir)
NEVER_FILE = Path("autopilot-write") / "autopilot-writes" / "never-senders.jsonl"


def never_senders(state_dir: Optional[Path | str], owner: Any) -> set[str]:
    """The senders this person said never to prepare anything for them from: their own
    rows (any of their ids), the last row for a sender wins."""
    if state_dir is None or not (Path(state_dir) / NEVER_FILE).exists():
        return set()
    from .card_actions import Journal
    me = bare(owner)
    last: dict[str, bool] = {}
    for row in Journal(Path(state_dir) / NEVER_FILE).rows():
        ids = {bare(i) for i in (row.get("ids") or [row.get("by")])}
        if me in ids and isinstance(row.get("sender"), str):
            last[row["sender"]] = bool(row.get("never"))
    return {s for s, on in last.items() if on}


def bare(owner: Any) -> str:
    """A person's id without the tenant prefix, lower-cased (gaps and readers differ)."""
    return str(owner or "").rsplit(":", 1)[-1].strip().lower()


class Kept:
    """The kept drafts, one row per (person, message record). Writers: the job only;
    the API opens it read-only (``rows``)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _open(self) -> sqlite3.Connection:
        from .file_modes import keep_shared
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        _schemas.apply(conn, _REPLY_DRAFTS)
        # the API's user reads it (``/api/replies``): in the shared state the store and its
        # directory keep the group (``deploy/release/api_user_setup.sh`` R_DIRS)
        keep_shared(self.path.parent, 0o700)
        keep_shared(self.path, 0o600)
        return conn

    def mark(self, owner: str, object_id: str, state: str, **fields: Any) -> None:
        if state not in STATES:
            raise ValueError(state)
        row = {"kind": str(fields.get("kind") or ""), "body": str(fields.get("body") or ""),
               "ask": str(fields.get("ask") or ""),
               "options": json.dumps(list(fields.get("options") or ()), ensure_ascii=False),
               "why": str(fields.get("why") or ""), "seconds": fields.get("seconds")}
        with self._lock:
            conn = self._open()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO drafts (owner, object_id, state, kind, body, ask,"
                    " options, why, at, seconds) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (bare(owner), object_id, state, row["kind"], row["body"], row["ask"],
                     row["options"], row["why"], _now().isoformat(), row["seconds"]))
                conn.commit()
            finally:
                conn.close()

    def rows(self, owners: Iterable[str], ids: Iterable[str] | None = None
             ) -> dict[str, dict[str, Any]]:
        """{object id: kept row} for ``owners`` (read-only; nothing kept: {})."""
        owners = sorted({bare(o) for o in owners if o})
        if not owners or not self.path.exists():
            return {}
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return {}
        try:
            got = conn.execute(
                "SELECT owner, object_id, state, kind, body, ask, options, why, at FROM drafts"
                f" WHERE owner IN ({','.join('?' * len(owners))})", owners).fetchall()
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        want = set(ids) if ids is not None else None
        out: dict[str, dict[str, Any]] = {}
        for owner, oid, state, kind, body, ask, options, why, at in got:
            if want is not None and oid not in want:
                continue
            try:
                opts = json.loads(options)
            except ValueError:
                opts = []
            out[oid] = {"owner": owner, "state": state, "kind": kind, "body": body,
                        "ask": ask, "options": opts if isinstance(opts, list) else [],
                        "why": why, "at": at}
        return out


def due(row: Optional[Mapping[str, Any]], now: datetime) -> bool:
    """Whether a line with this kept row is drafted (again): never drafted, a ``drafting``
    mark a dead run left, a ``none`` old enough to try again. A ``ready`` draft stays."""
    if not row:
        return True
    try:
        at = datetime.fromisoformat(str(row.get("at")))
    except ValueError:
        return True
    if row.get("state") == "drafting":
        return now - at > timedelta(minutes=STALE_MINUTES)
    if row.get("state") == "none":
        if str(row.get("why") or "") in SETUP_GAPS:
            return now - at > timedelta(minutes=SETUP_RETRY_MINUTES)
        if str(row.get("why") or "") in JUDGED:
            return False
        return now - at > timedelta(hours=RETRY_HOURS)
    return False


def open_lines(inbox: Path | str) -> list[tuple[str, str, int]]:
    """(owner, message record id, priority) of every open reply line, heaviest first (the
    run's gap inbox, read only)."""
    path = Path(inbox)
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        got = conn.execute(
            "SELECT object_id, follow_up, priority FROM gaps WHERE state = 'open' AND kind IN"
            f" ({','.join('?' * len(KINDS))}) ORDER BY priority DESC, first_seen", KINDS
        ).fetchall()
    finally:
        conn.close()
    out, seen = [], set()
    for oid, follow_up, priority in got:
        try:
            fu = json.loads(follow_up)
        except ValueError:
            continue
        owner = bare(fu.get("owner") if isinstance(fu, Mapping) else "")
        if owner and (owner, oid) not in seen:
            seen.add((owner, oid))
            out.append((owner, oid, int(priority or 0)))
    return out


def wanted(lines: Sequence[tuple[str, str, int]], kept: Kept, *,
           people: Optional[Iterable[str]] = None, per_person: int = PER_PERSON,
           per_run: int = PER_RUN, now: Optional[datetime] = None) -> list[tuple[str, str]]:
    """The (owner, record id) pairs this run drafts: lines with no draft kept (``due``),
    only ``people`` when given, at most ``per_person`` per person and ``per_run`` in all,
    in the list's order (heaviest first)."""
    now = now or _now()
    only = {bare(p) for p in people} if people is not None else None
    owners = sorted({o for o, _i, _p in lines if only is None or o in only})
    kept_rows: dict[str, dict[str, dict[str, Any]]] = {}
    for owner in owners:
        kept_rows[owner] = kept.rows([owner])
    counts: dict[str, int] = {}
    out: list[tuple[str, str]] = []
    for owner, oid, _p in lines:
        if owner not in kept_rows or len(out) >= per_run:
            continue
        if counts.get(owner, 0) >= per_person or not due(kept_rows[owner].get(oid), now):
            continue
        counts[owner] = counts.get(owner, 0) + 1
        out.append((owner, oid))
    return out


def result_of(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What ``dry_drafts`` gave for one line, as the kept fields: the first row with a
    reply or a question, else ``none`` with the first reason."""
    for row in rows:
        if row.get("kind") == "your_call" and row.get("ask"):
            return {"state": "ready", "kind": "your_call", "body": "",
                    "ask": str(row["ask"]), "options": list(row.get("options") or ())}
        if row.get("draft"):
            # a reply's ``ask`` column holds its note to the person (what a blank needs),
            # its ``why`` the outbound check's warning (a reply with an outside reader)
            return {"state": "ready", "kind": "reply", "body": str(row["draft"]),
                    "ask": str(row.get("note") or ""), "why": str(row.get("warning") or "")}
    why = next((str(r.get("why")) for r in rows if r.get("why")), "no_draft")
    return {"state": "none", "why": why}


def run(pairs: Sequence[tuple[str, str]], kept: Kept,
        draft: Callable[[str, str], Sequence[Mapping[str, Any]]], *,
        parallel: int = PARALLEL) -> dict[str, int]:
    """Drafts ``pairs`` with ``draft(owner, record id)`` (``dry_drafts`` for one line),
    ``parallel`` at once; every pair is marked ``drafting`` first, its outcome kept after.
    A call that raises is kept as ``none`` (``draft_failed``), tried again later."""
    import time
    for owner, oid in pairs:
        kept.mark(owner, oid, "drafting")
    counts = {"ready": 0, "none": 0}

    def one(pair: tuple[str, str]) -> None:
        owner, oid = pair
        start = time.monotonic()
        try:
            got = result_of(draft(owner, oid))
        except Exception as exc:  # noqa: BLE001 - one line's failure never stops the rest
            got = {"state": "none", "why": f"draft_failed:{type(exc).__name__}"}
        state = got.pop("state")
        kept.mark(owner, oid, state, seconds=round(time.monotonic() - start, 1), **got)
        counts[state] += 1

    with ThreadPoolExecutor(max(1, parallel), thread_name_prefix="reply-draft") as pool:
        list(pool.map(one, pairs))
    return counts


def attach(lines: Sequence[dict[str, Any]], kept: Mapping[str, Mapping[str, Any]]) -> None:
    """Each line gets its kept draft (in place): ``draft_state`` ``ready`` with the model's
    body on the line's draft (a ``your_call`` question: ``your_call`` with its options, the
    holding body kept until the person picks), ``drafting`` while a run writes it, else
    ``queued`` (the holding sentence until a run gets to it)."""
    for line in lines:
        row = kept.get(str(line.get("object_id") or ""))
        state = str((row or {}).get("state") or "")
        if state == "ready" and row:
            line["draft_state"] = "ready"
            line["drafted_by"] = "model"
            if row.get("kind") == "your_call":
                line["your_call"] = {"ask": row.get("ask") or "",
                                     "options": [o for o in row.get("options") or ()
                                                 if isinstance(o, Mapping)]}
            elif row.get("body"):
                draft = dict(line.get("draft") or {})
                draft["body"] = str(row["body"])
                draft.pop("copy_check", None)
                line["draft"] = draft
                if row.get("ask"):
                    line["draft_note"] = str(row["ask"])
                if row.get("why"):
                    line["draft_warning"] = str(row["why"])
        elif state == "drafting":
            line["draft_state"] = "drafting"
        else:
            line["draft_state"] = "queued"


def kept_for(state_dir: Any, owners: Iterable[str], lines: Sequence[Mapping[str, Any]]
             ) -> dict[str, dict[str, Any]]:
    """The kept rows for ``lines`` (the API side; no state dir or no store: {})."""
    if not state_dir:
        return {}
    ids = [str(x.get("object_id")) for x in lines if x.get("object_id")]
    return Kept(path_of(state_dir)).rows(owners, ids) if ids else {}


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        return default


ASSISTANT = "assistant"
HUNG_SECONDS = 1800.0     # a material read that has not returned in this long is a hung process
# What a message's prepare step needs of its autopilot (the general assistant: the job handler's
# material, the model deciding): code, not a registry trigger, so the drafts path outlives the
# triggers (ASSIST specs 5; open-assist3-l1-draft-executor, option 2)
ASSISTANT_PREPARE = {"id": ASSISTANT, "handler": "job", "draft": "agent",
                     "label": "Prepared for you", "due_minutes": 480, "done_when": "owner_replied"}


def screen_model(environ: Mapping[str, str]) -> Optional[str]:
    """The reply sort's model (``SCREEN_ENV``, else ``SCREEN``); None when it is off."""
    name = str(environ.get(SCREEN_ENV) or SCREEN).strip()
    return None if name.lower() in ("off", "none", "0", "") else name


def keep_jobs(queue: Any, registry: Mapping[str, Any], spec: Mapping[str, Any],
              event: Mapping[str, Any], owner: str, summary: str,
              proposals: Sequence[Mapping[str, Any]], prepared: Mapping[str, Any],
              now: datetime, todos: Sequence[Mapping[str, Any]] = ()) -> Optional[str]:
    """The Assistant's proposed changes for one line as a job item on the person's own
    autopilot queue (the same item, id and shape ``autopilot.run_cycle`` gives its
    ``also_jobs``): each change is prepared through the prepared-writes desk and runs only
    on that person's confirm, under every guard of the write path. ``todos`` (a system with
    no write path, an invitation to answer: things to do by hand) ride on the same item.
    The item id or None: no queue, the kill switch, or nothing kept (no hourly cap: owner 11-23)."""
    from . import autopilot as apm
    from .autopilot_jobs import kept
    from .autopilot_records import _digest, _iso
    jobs = kept(proposals)
    todos = [dict(t) for t in todos if isinstance(t, Mapping) and str(t.get("what") or "").strip()]
    if queue is None or not (jobs or todos) or apm.killed(queue, os.environ):
        return None
    item_id = "ap_" + _digest(str(spec["id"]) + ":job", event["event_id"], owner)[:24]
    label = str(spec.get("label") or spec["id"])
    due = now + timedelta(minutes=int(spec.get("due_minutes") or 480))
    with queue.conn:
        if queue.item(item_id) is not None:
            return item_id
        queue.add_item({
            "item_id": item_id, "event_id": event["event_id"], "autopilot": spec["id"],
            "owner": owner, "title": f"{label}: " + summary[:apm.SUMMARY_CHARS],
            "body": {"happened": apm._happened(event),
                     "prepared": {"kind": "job", "summary": summary, "jobs": jobs,
                                  "drafted_by": "model", **({"todos": todos} if todos else {})},
                     "done_when": spec.get("done_when") or "person_decided", "label": label},
            "audiences": set(prepared.get("audiences") or ()),
            "markings": sorted(set(spec.get("markings") or ())
                               | set(prepared.get("markings") or ())),
            "citations": list(prepared.get("citations") or ()),
            "due_at": _iso(due), "created_at": _iso(now), "prepare_ms": prepared.get("ms")})
    return item_id


# what the drafts path knows of every line it drafts: it is on the person's replies to write
# (dev 11-22: 3 of 10 messages the person answered got nothing, the session reading them as
# only copied)
OWED = ("This message is on {person}'s list of replies to write: prepare the reply they would "
        "send (or the question to them when its answer is only theirs); nothing only when the "
        "thread shows they already answered it.")


def line_draft(st: Any, registry: Mapping[str, Any], people: Any, maps: Sequence[Any],
               assistant: Any, owner: str, oid: str, *,
               now: Optional[datetime] = None, state_dir: Optional[Path] = None,
               held: Optional[dict[str, Any]] = None, queue: Any = None,
               history: Any = None, extra: str = "", as_of: str = "",
               prep: Any = None) -> list[dict[str, Any]]:
    """One open line's draft by the Assistant (``operating.autopilot_agent``), the same
    session Autopilot runs: the line's message event, the screens a draft keeps (participants
    cleared, nothing about pay or HR), the prepare step for its material, the session reading
    as the person, then ``agent_output`` in review (a reply the checks flag is kept with its
    warning, never swapped for a card). Rows as ``result_of`` reads them:
    ``[{"draft", "note", "warning"}]``, a question card ``[{"kind": "your_call", ...}]`` or
    ``[{"why"}]``. Changes the Assistant proposed in a business system (BC, Planner, Portal,
    MC, calendar...) and its things to do by hand become a job item on ``queue`` for the
    person (``keep_jobs``; its id as the row's ``jobs``), confirmed there one by one;
    nothing is written here.

    The session reads beside the message what the live cycle puts on it (``autopilot.
    _engage`` over ``history``, default ``queue``: how the person answers this sender, their
    own posts in the thread, known before the message), that the line is on their replies to
    write (``OWED``), and their memory (``autopilot.person_memory``)."""
    from . import autopilot as apm, autopilot_engine as eng, autopilot_agent_io as io
    from .autopilot_handlers import Ctx
    import time
    directory, addresses_of, domains = maps
    events, skipped = apm.discover(st, [oid], registry, people)
    if not events:
        # the feed's own reason when it names one (``approval_not_cleared``...), never a bare "no event"
        return [{"why": next(iter(sorted(k for k, n in skipped.items() if n)), "no_event")}]
    event = events[0]
    if event.get("event_type") == "message.received" and not event.get("cleared"):
        return [{"why": "participants_not_cleared"}]
    if eng.pay_or_hr(event):
        return [{"why": "pay_or_hr"}]
    if str(event.get("sender") or "").strip().lower() in never_senders(state_dir, owner):
        return [{"why": "never_sender"}]
    spec = ASSISTANT_PREPARE
    if assistant is None:
        return [{"why": "draft_model_off"}]
    # the line is on their replies to write: a reply is due (``autopilot_agent_io.reply_due``)
    event = {**event, "owed": True}
    habit = history if history is not None else queue
    if habit is not None:
        try:
            apm._engage(habit, event, None)
        except (sqlite3.Error, AttributeError, TypeError, ValueError):
            pass        # no history read: the message alone, as before
    day = (now or _now()).date()
    seconds = HUNG_SECONDS      # no deadline on reading the material: only a hung read ends it
    prepared = eng._prepare(st, spec, event, day, domains, seconds)
    if prepared.get("skip") or prepared.get("error"):
        return [{"why": str(prepared.get("skip") or prepared.get("error"))}]
    material = (prepared.get("work") or {}).get("material") or {}
    text = str(material.get("text") or event.get("text") or "")
    mine = sorted(addresses_of.get(owner) or ())
    sent_to = {str(a).lower() for a in [*(event.get("to") or ()), *(event.get("cc") or ())]}
    person = next((a for a in mine if a in sent_to), mine[0] if mine else owner)
    described, _inside = io.agent_input(event, text, material, addresses_of)
    described += "\n" + OWED.format(person=person)
    # the person's memory and past corrections (``autopilot.person_memory``)
    described += apm.person_memory(state_dir, held if held is not None else {}, owner, mine,
                                   event, prepared.get("work") or {})
    # ``extra``: the prepared session's matter, nearest precedents and ground rules
    # (``assist_prepared``), already screened part by part there; the thread screen reads the
    # thread above only (a past case's own words must not refuse this matter)
    got, why = assistant.run(event, owner, described, str(person), people, reads=True, as_of=as_of,
                             **({"extra": extra} if extra else {}),
                             **({"prepared": prep} if prep is not None else {}))
    if got is None:
        got = io.invitation_only(event, material, why)
    if got is None:
        return [{"why": why or "no_draft"}]
    got, _held, why = io.agent_output(
        got, event, owner, text, material, addresses_of,
        lambda: Ctx(st, day, time.monotonic() + seconds, domains), str(person), review=True)
    jobs = None
    if got.proposals or got.todos:
        try:
            jobs = keep_jobs(queue, registry, spec, event, owner, str(got.summary or ""),
                             got.proposals, prepared, now or _now(), todos=got.todos)
        except sqlite3.Error:   # a busy queue costs the changes, never the line's draft
            jobs = None
    extra = {"jobs": jobs} if jobs else {}
    if got.reply:
        row = {"draft": got.reply, "note": got.note or "", **extra}
        if got.warning:
            row["warning"] = got.warning
        return [row]
    if got.ask and got.ask.get("question"):
        return [{"kind": "your_call", "ask": got.ask["question"],
                 "options": list(got.ask.get("options") or ()), **extra}]
    if jobs:
        return [{"why": "changes_prepared", **extra}]
    return [{"why": why or "no_draft"}]


def cli(a: Any, registry: Mapping[str, Any]) -> int:
    """``assist-drafts``: draft this run's ``wanted`` lines (``--owner``: only those people)
    with the autopilot's dry drafter, each worker on its own store; prints the counts."""
    from . import autopilot as apm
    from . import proactive_engine as pe
    if not a.state_dir:
        print(json.dumps({"error": "assist-drafts needs --state-dir"}))
        return 2
    state = Path(a.state_dir)
    kept = Kept(path_of(state))
    inbox = state / "projection" / "ontology.sqlite.gap-inbox.sqlite"
    pairs = wanted(open_lines(inbox), kept, people=a.owner or None,
                   per_person=_int_env(PER_PERSON_ENV, PER_PERSON),
                   per_run=_int_env(PER_RUN_ENV, PER_RUN))
    local = threading.local()
    stores: list[Any] = []
    people = pe._people(Path(a.store))

    from . import autopilot_agent as ag
    maps = apm._people_maps(people)
    assistant = ag.from_environ(os.environ)
    if assistant is not None:
        assistant.reply_screen = screen_model(os.environ)
    held: dict[str, Any] = {}
    apm.prime_memory(state, held)

    def draft(owner: str, oid: str) -> Sequence[Mapping[str, Any]]:
        if getattr(local, "st", None) is None:
            local.st = apm._with_captures(apm._store(a.store), a)
            stores.append(local.st)
            # the person's queue, where the Assistant's proposed changes wait for a confirm
            try:
                local.queue = apm.Queue(state / "autopilot" / apm.QUEUE_FILE)
                stores.append(local.queue)
            except (OSError, sqlite3.Error):
                local.queue = None
        return line_draft(local.st, registry, people, maps, assistant, owner, oid,
                          state_dir=state, held=held, queue=local.queue)

    try:
        counts = run(pairs, kept, draft, parallel=_int_env(PARALLEL_ENV, PARALLEL))
    finally:
        for st in stores:
            st.close()
    print(json.dumps({"schema": "plx-reply-drafts/v1", "wanted": len(pairs), **counts},
                     sort_keys=True))
    return 0
