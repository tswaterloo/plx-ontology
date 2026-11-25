"""Watermark-keyed answer cache (Ask spec 19 S8: same inputs, same answer).

An Ask answer is kept in memory alongside the exact watermarks of every view and store
source it read (``biz.view_state``, ``biz.state``, ``assist.matters_heartbeat``, or whole-
store ``data_as_of``). When the same reader asks the same single-turn question under the
same mode, context, memory, calendar day, prompt/code digest and permission door mark, and
every source the kept answer read still sits at the exact same watermark, the kept answer
returns immediately without starting a model session.

Fail closed everywhere:
- Any permission, access-class, column-marking, hidden-document or people-exclusion change
  moves ``door_mark`` (``door_value`` + live ``biz.door_state.revoked`` / exclusion check),
  so a cached answer never leaks across readers or past a revocation.
- Any unreadable door mark, unreadable watermark, mid-run watermark change, tool error,
  refusal, partial failure, untraced figure, gap, clarification, write/side-effecting tool,
  attachment, follow-up history, memory instruction, canary/view-as session, or explicit
  ``refresh`` request bypasses the cache and runs live.
- Moving an unread view's watermark does not invalidate an answer that read only other
  views; moving any read view's watermark (or whole-store ``data_as_of`` when read) evicts
  the entry and re-runs live.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from operating.time_window import BUSINESS_TZ
from . import ask_legs, ask_refs
from .ask_refs import _parsed
from .door_value import door_value

LOG = logging.getLogger("operator_api.ask")

ANSWER_CACHE_ENV = "PLX_ASK_ANSWER_CACHE"
ANSWER_CACHE_VERSION = 1
ANSWER_CACHE_MAX_ENTRIES = 256
ANSWER_CACHE_MAX_AGE_SECONDS = 24 * 3600.0
CACHE_PG_CONNECT_TIMEOUT = 2
CACHE_PG_STATEMENT_TIMEOUT_MS = 1500

VIEW_PREFIX = "view:"
STORE_SOURCE = "store:data_as_of"
MATTERS_SOURCE = "assist:matters"
OPEN_WORK_STATE_SOURCE = "assist:notes_and_optin"
READINGS_SOURCE = "assist:readings"

COMMS_VIEWS = frozenset({
    "emails",
    "attachments",
    "teams_messages",
    "meetings",
    "meeting_transcripts",
    "sharepoint_files",
})
COMMS_TOOLS = frozenset({"plx.search", "plx.open", "plx.read_document"})

_UNSET = object()


class _HookNoSnapshot(dict):
    """Sentinel returned by ``snapshot_all_watermarks`` when a test hook provides
    ``_watermarks`` without ``_watermarks_all`` (never used for Postgres/production)."""


_HOOK_NO_SNAPSHOT = _HookNoSnapshot()

_HIDDEN_DIGESTS: dict[tuple[int, int, int], tuple[frozenset[str], frozenset[str], frozenset[str], str]] = {}
_HIDDEN_DIGEST_LOCK = threading.Lock()

READ_ONLY_TOOLS = frozenset({
    "plx.sql",
    "plx.search",
    "plx.open",
    "plx.read_document",
    "plx.my_open_work",
    "plx.insight",
    "plx.legs",
})

SEARCH_SOURCE_VIEWS: dict[str, tuple[str, ...]] = {
    "graph_mail": ("emails", "attachments"),
    "teams": ("teams_messages",),
    "sharepoint": ("sharepoint_files",),
    "records": ("records",),
    "original": (),
}

_TZ = ZoneInfo(BUSINESS_TZ)


def enabled(environ: Mapping[str, str] = os.environ) -> bool:
    """Whether the watermark-keyed answer cache is on (default on; ``0`` disables)."""
    return environ.get(ANSWER_CACHE_ENV, "1").strip() != "0"


def normalize_question(question: str) -> str:
    """Canonical form of a question for exact-repeat matching (whitespace folded, case-folded)."""
    return " ".join(str(question or "").split()).casefold()


def _format_as_of(as_of: Optional[str]) -> Optional[str]:
    if not isinstance(as_of, str) or not as_of.strip():
        return None
    raw = as_of.strip()
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(_TZ)
    hour = local.hour % 12 or 12
    ampm = "a.m." if local.hour < 12 else "p.m."
    return f"{local.day} {local:%b %Y}, {hour}:{local:%M} {ampm} ET"


def freshness_label(as_of: Optional[str]) -> str:
    """Plain-language provenance label shown in ``Freshness.label`` for a kept answer."""
    when = _format_as_of(as_of)
    if when:
        return f"Data as of {when} (unchanged since last check)"
    return "Data unchanged since last check"


def status_message(as_of: Optional[str]) -> str:
    """Plain-language status note emitted on a cache hit."""
    when = _format_as_of(as_of)
    if when:
        return f"Data unchanged as of {when}"
    return "Data unchanged since last check"


def eligible(question: str, *, history: Sequence[Any] = (), files: Sequence[Any] = (),
             session: Any = None, refresh: bool = False, clarified: Any = None,
             stated_kept: Any = None, environ: Mapping[str, str] = os.environ
             ) -> tuple[bool, str]:
    """Whether this request may read from and write to the answer cache (fail closed)."""
    if not enabled(environ):
        return False, "disabled"
    if refresh:
        return False, "refresh"
    if not normalize_question(question):
        return False, "empty"
    if history:
        return False, "follow_up"
    if files:
        return False, "attachments"
    if clarified is not None:
        return False, "clarified"
    if stated_kept is not None:
        return False, "stated_instruction"
    if getattr(session, "canary", False):
        return False, "canary"
    if getattr(session, "view_as", False):
        return False, "view_as"
    try:
        from . import memory as mem
        if mem.asked_to_keep(question) is not None or mem._INSTRUCTION.search(str(question or "")):
            return False, "memory_instruction"
    except Exception as exc:  # noqa: BLE001 - fail closed if memory rule cannot be checked
        LOG.warning("ask_answer_cache: memory instruction check failed: %s", type(exc).__name__)
        return False, "memory_check_failed"
    return True, ""


def _pg_connection(backend: Any = None) -> Any:
    """Short-timeout read-only Postgres connection context manager."""
    pg_fn = getattr(backend, "pg", None)
    if callable(pg_fn):
        pool = pg_fn()
        if pool is not None and hasattr(pool, "connection"):
            return pool.connection()
    from operating.pg import pool as pg_pool
    return pg_pool.connect(
        connect_timeout=CACHE_PG_CONNECT_TIMEOUT,
        options=f"-c statement_timeout={CACHE_PG_STATEMENT_TIMEOUT_MS}",
    )


def _db_door_state(backend: Any) -> Optional[tuple[int, Any]]:
    """Live ``(revoked, exclusion_tag)`` from ``biz.door_state`` verified against the
    current people-exclusion stamp. Returns None if behind or unreadable (fail closed)."""
    hook = getattr(backend, "_door_db_stamp", None)
    if callable(hook):
        try:
            got = hook()
        except Exception as exc:  # noqa: BLE001 - fail closed on hook error
            LOG.warning("ask_answer_cache: door_db_stamp hook failed: %s", type(exc).__name__)
            return None
        if isinstance(got, tuple) and len(got) == 2 and isinstance(got[0], int):
            return got
        return None
    try:
        from operating.pg import business_views as bv, door
        with _pg_connection(backend) as pg:
            if not door.present(pg, bv.SCHEMA):
                return None
            excl = door.exclusion_stamp(pg)
            row = pg.execute(
                f"SELECT revoked, excluded_at FROM {bv.SCHEMA}.{door.STATE}"
            ).fetchone()
            if not row or row[1] != excl:
                return None
            revoked = row[0]
            if not isinstance(revoked, int) or isinstance(revoked, bool) or revoked < 0:
                return None
            tag = json.loads(excl)[0]
            return revoked, tag
    except Exception as exc:  # noqa: BLE001 - unreadable door state: fail closed
        LOG.warning("ask_answer_cache: db door state unreadable: %s", type(exc).__name__)
        return None


def _hidden_docs_digest(held: Iterable[str]) -> Optional[str]:
    """Digest of document markings withheld from ``held`` plus PLX-issued supplier docs.
    Returns None if the search store is enabled and unreadable (fail closed)."""
    from operating.search import document_marks
    held_set = frozenset(h for h in (held or ()) if isinstance(h, str))
    marked = document_marks.withheld_set(held_set)
    if marked is None:
        return None
    ids, handles = marked
    own = document_marks._own_issued()
    key = (id(ids), id(handles), id(own))
    with _HIDDEN_DIGEST_LOCK:
        hit = _HIDDEN_DIGESTS.get(key)
        if hit is not None and hit[0] is ids and hit[1] is handles and hit[2] is own:
            return hit[3]
    digest = hashlib.sha256(
        json.dumps([sorted(ids), sorted(handles), sorted(own)], separators=(",", ":")).encode()
    ).hexdigest()[:32]
    with _HIDDEN_DIGEST_LOCK:
        if len(_HIDDEN_DIGESTS) >= 64:
            _HIDDEN_DIGESTS.clear()
        _HIDDEN_DIGESTS[key] = (ids, handles, own, digest)
    return digest


def _compute_comms_digest(runtime: Any, actor: Any, held: Iterable[str]) -> Optional[str]:
    """Live comms-access digest covering ``teams_windows``, ``transcript_reader``, and
    hidden documents (even when ``door.reader_scope`` reports ``docs=False``). Returns None
    if any part cannot be read (fail closed)."""
    if runtime is None or actor is None:
        return None
    try:
        from operating.pg import business_comms as bc
        teams = bc.teams_windows(runtime, actor)
        transcripts = bc.transcript_reader(runtime, actor)
        hidden_digest = _hidden_docs_digest(held)
    except Exception as exc:  # noqa: BLE001 - fail closed when comms scope unreadable
        LOG.warning("ask_answer_cache: comms scope unreadable: %s", type(exc).__name__)
        return None
    if not isinstance(teams, Mapping) or not isinstance(transcripts, Mapping) or hidden_digest is None:
        return None
    body = {
        "teams": sorted((str(k), str(v)) for k, v in teams.items()),
        "transcripts": {
            "full": bool(transcripts.get("full")),
            "ids": sorted(str(i) for i in (transcripts.get("ids") or ())),
        },
        "hidden_docs": hidden_digest,
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]


def resolve_comms_digest(asker: Any, session: Any, *, backend: Any = None,
                         reader: Any = None) -> Optional[str]:
    """Compute a live digest of comms-specific access rules (``teams_windows``,
    ``transcript_reader``, hidden document ids) when exposed by the runtime/hooks.
    Returns None if comms scope cannot be verified."""
    hook = getattr(asker, "_comms_scope", None) or getattr(backend, "_comms_scope", None)
    if callable(hook):
        try:
            got = hook(session)
        except Exception as exc:  # noqa: BLE001 - fail closed on hook error
            LOG.warning("ask_answer_cache: comms_scope hook failed: %s", type(exc).__name__)
            return None
        if got is None:
            return None
        return hashlib.sha256(_canonical_mark(got).encode("utf-8")).hexdigest()[:32]

    if backend is None:
        return None

    direct_runtime = getattr(backend, "runtime", None) or getattr(
        getattr(backend, "item_gate", None), "runtime", None
    )
    if direct_runtime is not None:
        actor = getattr(backend, "actor", None)
        if actor is None and callable(getattr(backend, "actor_for", None)) and reader is not None:
            try:
                actor = backend.actor_for(reader)
            except Exception as exc:  # noqa: BLE001 - fail closed
                LOG.warning("ask_answer_cache: actor_for failed: %s", type(exc).__name__)
                return None
        if actor is None:
            actor = getattr(session, "actor", None)
        held: Any = getattr(backend, "held", None)
        if held is None and callable(getattr(backend, "held_for", None)) and reader is not None:
            try:
                held = backend.held_for(reader)
            except Exception as exc:  # noqa: BLE001 - fail closed
                LOG.warning("ask_answer_cache: held_for failed: %s", type(exc).__name__)
                return None
        if held is None:
            held = getattr(session, "held", None) or ()
        return _compute_comms_digest(direct_runtime, actor, held)

    worker = getattr(backend, "access_worker", None) or getattr(backend, "reads", None)
    if callable(getattr(worker, "run", None)) and reader is not None:
        def job(view: Any) -> Optional[str]:
            access = getattr(view, "access", None)
            runtime = getattr(access, "runtime", None)
            if runtime is None:
                return None
            req_cm = getattr(view, "_request", None)
            if callable(req_cm):
                with req_cm():
                    gated = (
                        access._gated()
                        if callable(getattr(access, "_gated", None))
                        else getattr(access, "actor", None)
                    )
                    grant = getattr(access, "grant", None)
                    held = grant()[1] if callable(grant) else ()
            else:
                gated = (
                    access._gated()
                    if callable(getattr(access, "_gated", None))
                    else getattr(access, "actor", None)
                )
                grant = getattr(access, "grant", None)
                held = grant()[1] if callable(grant) else ()
            return _compute_comms_digest(runtime, gated, held)

        try:
            got = worker.run(reader, job, timeout=5)
        except Exception as exc:  # noqa: BLE001 - fail closed when worker cannot check comms
            LOG.warning("ask_answer_cache: worker comms scope failed: %s", type(exc).__name__)
            return None
        if isinstance(got, str) and len(got) == 32:
            return got
        return None
    return None


def resolve_door_state(asker: Any, session: Any, *, backend: Any = None,
                       reader: Any = None) -> tuple[Optional[str], bool]:
    """Resolve ``(door_mark, comms_ok)``.

    ``door_mark`` covers reader identity, decided door ``access_mark``, people-exclusion
    rule stamp, live ``door_state`` revocation/exclusion state, and (when available) the
    live comms-scope digest (``teams_windows``, ``transcript_reader``, hidden docs).
    ``comms_ok`` is True only when the live comms-scope digest was verified.
    """
    comms_digest = resolve_comms_digest(asker, session, backend=backend, reader=reader)
    comms_ok = comms_digest is not None

    hook = getattr(asker, "_door_mark", None)
    if callable(hook):
        try:
            val = hook(session)
        except Exception as exc:  # noqa: BLE001 - fail closed on hook failure
            LOG.warning("ask_answer_cache: door_mark hook failed: %s", type(exc).__name__)
            return None, False
        if not isinstance(val, str) or not val.strip():
            return None, False
        if comms_digest is not None:
            folded = hashlib.sha256(
                json.dumps([val, comms_digest], separators=(",", ":")).encode()
            ).hexdigest()[:32]
            return folded, True
        return val, False
    if backend is None or reader is None:
        return None, False
    if getattr(session, "view_as", False) or getattr(session, "canary", False):
        return None, False
    doors = getattr(backend, "doors", None)
    if not hasattr(doors, "access_mark"):
        return None, False
    try:
        access = doors.access_mark(reader)
        dv = door_value(reader, access)
    except Exception as exc:  # noqa: BLE001 - fail closed on door lookup failure
        LOG.warning("ask_answer_cache: door_value unreadable: %s", type(exc).__name__)
        return None, False
    if not dv:
        return None, False
    db_state = _db_door_state(backend)
    if db_state is None:
        return None, False
    digest = hashlib.sha256(
        json.dumps([dv, db_state[0], db_state[1], comms_digest],
                   separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return digest[:32], comms_ok


def resolve_door_mark(asker: Any, session: Any, *, backend: Any = None,
                      reader: Any = None) -> Optional[str]:
    """One digest covering reader identity, decided door ``access_mark``, people-exclusion
    rule stamp, live ``door_state`` revocation/exclusion state, and live comms scope when
    available. None on any missing or unreadable piece (fail closed)."""
    return resolve_door_state(asker, session, backend=backend, reader=reader)[0]


def code_prompt_digest(asker: Any, *, deep: bool = False) -> Optional[str]:
    """Digest of the Ask prompt, model settings, deep method, and host policy config."""
    hook = getattr(asker, "_code_digest", None)
    if callable(hook):
        try:
            val = hook()
        except Exception as exc:  # noqa: BLE001 - fail closed
            LOG.warning("ask_answer_cache: code_digest hook failed: %s", type(exc).__name__)
            return None
        return val if isinstance(val, str) and val.strip() else None
    try:
        from . import simple_ask
        model, level = simple_ask.settings()
        prompt_hash = hashlib.sha256(simple_ask.system_prompt().encode("utf-8")).hexdigest()
        host_cfg = getattr(asker, "host_config", None)
        cfg_hash: Optional[str] = None
        if host_cfg is not None:
            cfg_hash = hashlib.sha256(Path(host_cfg).read_bytes()).hexdigest()
        body = {
            "v": ANSWER_CACHE_VERSION,
            "model": model,
            "effort": level,
            "deep": bool(deep),
            "deep_method": simple_ask.DEEP_METHOD if deep else None,
            "deep_effort": simple_ask.DEEP_EFFORT if deep else None,
            "prompt": prompt_hash,
            "config": cfg_hash,
        }
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return digest[:32]
    except Exception as exc:  # noqa: BLE001 - fail closed when prompt/config unreadable
        LOG.warning("ask_answer_cache: code_prompt_digest failed: %s", type(exc).__name__)
        return None


def cache_identity_state(asker: Any, session: Any, question: str, *, deep: bool = False,
                         record: Optional[str] = None, context: Sequence[str] = (),
                         memory_text: str = "", today: Optional[str] = None,
                         backend: Any = None, reader: Any = None
                         ) -> tuple[Optional[str], Optional[str], Optional[str], bool]:
    """Compute ``(cache_key, reader_key, door_mark, comms_ok)``; returns
    ``(None, None, None, False)`` if identity, door, or prompt/code input cannot be
    resolved (fail closed)."""
    from . import simple_ask
    try:
        reader_key = simple_ask._reader_key(asker, session)
    except Exception as exc:  # noqa: BLE001 - non-employee or unreadable ticket: fail closed
        LOG.warning("ask_answer_cache: reader key unavailable: %s", type(exc).__name__)
        return None, None, None, False
    door_mark, comms_ok = resolve_door_state(asker, session, backend=backend, reader=reader)
    if door_mark is None:
        return None, None, None, False
    code_digest = code_prompt_digest(asker, deep=deep)
    if code_digest is None:
        return None, None, None, False
    model, level = simple_ask.settings()
    mem_digest = hashlib.sha256(str(memory_text or "").encode("utf-8")).hexdigest()[:32]
    day = today or datetime.now(_TZ).date().isoformat()
    payload = {
        "v": ANSWER_CACHE_VERSION,
        "q": normalize_question(question),
        "reader": reader_key,
        "door": door_mark,
        "deep": bool(deep),
        "model": model,
        "effort": level,
        "record": str(record) if record else None,
        "context": [str(c) for c in context],
        "memory": mem_digest,
        "today": day,
        "code": code_digest,
    }
    key_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return key_digest, reader_key, door_mark, comms_ok


def cache_identity(asker: Any, session: Any, question: str, *, deep: bool = False,
                   record: Optional[str] = None, context: Sequence[str] = (),
                   memory_text: str = "", today: Optional[str] = None,
                   backend: Any = None, reader: Any = None
                   ) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Compute ``(cache_key, reader_key, door_mark)``; returns ``(None, None, None)`` if any
    identity, door, or prompt/code input cannot be resolved (fail closed)."""
    key_digest, reader_key, door_mark, _comms_ok = cache_identity_state(
        asker, session, question, deep=deep, record=record, context=context,
        memory_text=memory_text, today=today, backend=backend, reader=reader,
    )
    return key_digest, reader_key, door_mark


def _tool_name(raw: Any) -> str:
    text = str(raw or "")
    if text.startswith("mcp__"):
        return text.split("__", 2)[-1].replace("_", ".", 1)
    return text


def _has_file_citation(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("file:")
    if isinstance(value, Mapping):
        return any(_has_file_citation(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_file_citation(v) for v in value)
    return False


def _sql_views_from_statement(statement: str) -> list[str]:
    from . import simple_ask
    try:
        from operating import ontology_tools_sql as sql_mod
        known = set(sql_mod._dictionary())
    except Exception as exc:  # noqa: BLE001 - fallback to whole-store watermark
        LOG.warning("ask_answer_cache: sql dictionary unreadable: %s", type(exc).__name__)
        return []
    return sorted({n.lower() for n in simple_ask._READS.findall(statement) if n.lower() in known})


def extract_sources(ran: Any) -> tuple[Optional[frozenset[str]], str]:
    """Return the exact set of watermarked sources read by a clean ``SimpleRun``, or
    ``(None, reason)`` if the answer must not be cached (fail closed)."""
    if not str(getattr(ran, "text", "") or "").strip():
        return None, "empty_text"
    if getattr(ran, "stopped", False):
        return None, "stopped"
    if getattr(ran, "failure", None):
        return None, "failure"
    if getattr(ran, "unverified", None):
        return None, "unverified"
    if getattr(ran, "clarify", None) is not None:
        return None, "clarify"
    if getattr(ran, "gaps", None):
        return None, "gaps"
    check = getattr(ran, "check", None)
    if isinstance(check, Mapping) and check.get("status") not in (None, "checked", "repaired"):
        return None, "check_not_clean"

    events = list(getattr(ran, "events", None) or ())
    from . import assistant_tools, simple_ask
    if simple_ask.prepared_ids(events) or assistant_tools.documents(events) \
            or assistant_tools.handed_off(events):
        return None, "side_effect"

    sources: set[str] = set()
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "tool_call":
            continue
        name = _tool_name(event.get("name"))
        if name not in READ_ONLY_TOOLS:
            return None, f"non_read_tool:{name}"
        if event.get("is_error"):
            return None, "tool_error"
        result = _parsed(event.get("result"))
        if not isinstance(result, Mapping) or "error" in result:
            return None, "tool_error"
        if name == "plx.legs":
            continue
        args = event.get("arguments") or event.get("input") or {}
        if not isinstance(args, Mapping):
            args = {}

        if name == "plx.sql":
            if result.get("status") in ("failed", "error", "not_ready"):
                return None, "sql_not_ok"
            card = result.get("card")
            if isinstance(card, Mapping) or "record" in args:
                if isinstance(card, Mapping) and card.get("messages_unavailable"):
                    return None, "card_messages_unavailable"
                if isinstance(card, Mapping) and card.get("readings_unavailable"):
                    return None, "card_readings_unavailable"
                sources.add(f"{VIEW_PREFIX}emails")
                sources.add(f"{VIEW_PREFIX}teams_messages")
                if isinstance(card, Mapping) and (
                    card.get("readings_source") or card.get("thread_state") is not None
                ):
                    sources.add(READINGS_SOURCE)
            views = result.get("views")
            if isinstance(views, list) and views:
                for view in views:
                    if isinstance(view, str) and view.strip():
                        sources.add(f"{VIEW_PREFIX}{view.strip().lower()}")
            elif "page" in args:
                # Paging an earlier sql:// result already recorded in the initial call.
                continue
            elif isinstance(args.get("describe"), str) and args["describe"].strip():
                sources.add(f"{VIEW_PREFIX}{args['describe'].strip().lower()}")
            elif isinstance(args.get("sql"), str):
                inferred = _sql_views_from_statement(args["sql"])
                if inferred:
                    for view in inferred:
                        sources.add(f"{VIEW_PREFIX}{view}")
                else:
                    sources.add(STORE_SOURCE)
            else:
                sources.add(STORE_SOURCE)
        elif name == "plx.insight":
            if result.get("not_measurable") or result.get("status") in ("failed", "error"):
                return None, "insight_not_ok"
            views = result.get("views")
            if isinstance(views, list) and views:
                for view in views:
                    if isinstance(view, str) and view.strip():
                        sources.add(f"{VIEW_PREFIX}{view.strip().lower()}")
            else:
                sources.add(STORE_SOURCE)
        elif name == "plx.search":
            gaps = result.get("gaps") or ()
            for gap in gaps:
                if isinstance(gap, Mapping) and gap.get("reason") != "snippets_shortened":
                    return None, "search_partial"
            sources.add(STORE_SOURCE)
            searched = (result.get("coverage") or {}).get("sources") or (
                result.get("searched") or {}).get("sources") or ()
            for src in searched:
                for view in SEARCH_SOURCE_VIEWS.get(str(src), ()):
                    sources.add(f"{VIEW_PREFIX}{view}")
        elif name in ("plx.open", "plx.read_document"):
            if _has_file_citation(args) or _has_file_citation(result):
                return None, "attached_file_open"
            sources.add(STORE_SOURCE)
            opened = ask_legs._opened_sources(result) | ask_legs._opened_sources(args)
            for src in opened:
                for view in SEARCH_SOURCE_VIEWS.get(src, ()):
                    sources.add(f"{VIEW_PREFIX}{view}")
        elif name == "plx.my_open_work":
            if result.get("status") != "ok" or result.get("notes_status"):
                return None, "open_work_not_ok"
            sources.add(MATTERS_SOURCE)
            sources.add(OPEN_WORK_STATE_SOURCE)

    if not sources:
        return None, "no_sources"
    return frozenset(sources), ""


def needs_comms_scope(sources: Iterable[str], ran: Any = None) -> bool:
    """Whether an answer touched comms views, whole-store queries that may read comms, or
    ``plx.search``/``plx.open``/``plx.read_document`` and therefore requires a live comms
    access digest."""
    for src in sources:
        if src == STORE_SOURCE:
            return True
        if src.startswith(VIEW_PREFIX) and src[len(VIEW_PREFIX):] in COMMS_VIEWS:
            return True
    for event in getattr(ran, "events", None) or ():
        if isinstance(event, Mapping) and event.get("type") == "tool_call":
            if _tool_name(event.get("name")) in COMMS_TOOLS:
                return True
    return False


def _canonical_mark(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _iso_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _read_readings_pg_watermark(pg: Any) -> Optional[dict[str, Any]]:
    """Read ``assist:readings`` watermark from ``assist.card_readings`` (preferred API
    view), falling back to ``assist.readings`` or ``assist.reading_runs`` when absent.
    Returns None if missing or unreadable (fail closed)."""
    for key, rel, query in (
        ("card_readings", "assist.card_readings",
         "SELECT max(read_at), count(*) FROM assist.card_readings"),
        ("readings", "assist.readings",
         "SELECT max(read_at), count(*) FROM assist.readings WHERE status = 'ok'"),
        ("reading_runs", "assist.reading_runs",
         "SELECT max(run_at), count(*) FROM assist.reading_runs"),
    ):
        if pg.execute("SELECT to_regclass(%s)", (rel,)).fetchone()[0] is not None:
            try:
                row = pg.execute(query).fetchone()
                if row is not None:
                    return {key: [_iso_or_none(row[0]), int(row[1] or 0)]}
            except Exception as exc:  # noqa: BLE001 - unreadable readings source fails closed
                LOG.warning("ask_answer_cache: %s unreadable: %s", rel, type(exc).__name__)
                return None
    return None


def _read_pg_watermarks(pg: Any, views: Sequence[str], *, need_matters: bool,
                        need_store: bool, need_readings: bool = False
                        ) -> Optional[dict[str, str]]:
    """Read exact watermarks for requested views/sources in one Postgres connection."""
    from operating.pg import business_views as bv
    schema = bv.SCHEMA
    out: dict[str, str] = {}

    has_vs = pg.execute("SELECT to_regclass(%s)", (f"{schema}.view_state",)).fetchone()[0] \
        is not None
    has_state = pg.execute("SELECT to_regclass(%s)", (f"{schema}.state",)).fetchone()[0] \
        is not None

    if views:
        vs_rows: dict[str, tuple[Any, ...]] = {}
        st_rows: dict[str, tuple[Any, ...]] = {}
        if has_vs:
            for r in pg.execute(
                f"SELECT view, watermark, newest_source_at, last_ok_at, version "
                f"FROM {schema}.view_state WHERE view = ANY(%s)",
                (list(views),),
            ).fetchall():
                vs_rows[str(r[0])] = (
                    r[1], _iso_or_none(r[2]), _iso_or_none(r[3]), r[4],
                )
        if has_state:
            for r in pg.execute(
                f"SELECT view, built_at, digest FROM {schema}.state WHERE view = ANY(%s)",
                (list(views),),
            ).fetchall():
                st_rows[str(r[0])] = (_iso_or_none(r[1]), r[2])
        for view in views:
            vs = vs_rows.get(view)
            st = st_rows.get(view)
            has_vs_mark = vs is not None and any(x is not None for x in vs)
            has_st_mark = st is not None and st[0] is not None
            if not has_vs_mark and not has_st_mark:
                return None
            out[f"{VIEW_PREFIX}{view}"] = _canonical_mark({"vs": vs, "st": st})

    if need_matters:
        if pg.execute("SELECT to_regclass('assist.matters_heartbeat')").fetchone()[0] is None:
            return None
        row = pg.execute("SELECT last_cycle_at FROM assist.matters_heartbeat").fetchone()
        if not row or row[0] is None:
            return None
        out[MATTERS_SOURCE] = _canonical_mark(_iso_or_none(row[0]))

    if need_readings:
        readings_mark = _read_readings_pg_watermark(pg)
        if readings_mark is None:
            return None
        out[READINGS_SOURCE] = _canonical_mark(readings_mark)

    if need_store:
        parts: dict[str, Any] = {}
        if has_vs:
            r = pg.execute(
                f"SELECT max(watermark), max(last_ok_at) FROM {schema}.view_state"
            ).fetchone()
            if r and (r[0] is not None or r[1] is not None):
                parts["vs"] = [r[0], _iso_or_none(r[1])]
        if has_state:
            r = pg.execute(f"SELECT max(built_at) FROM {schema}.state").fetchone()
            if r and r[0] is not None:
                parts["st"] = _iso_or_none(r[0])
        if pg.execute("SELECT to_regclass(%s)", (f"{schema}.comm_text_state",)).fetchone()[0] \
                is not None:
            r = pg.execute(
                f"SELECT max(built_at), max(built_field_id) FROM {schema}.comm_text_state"
            ).fetchone()
            if r and (r[0] is not None or r[1] is not None):
                parts["comm_text"] = [_iso_or_none(r[0]), r[1]]
        if pg.execute("SELECT to_regclass('ontology.load_state')").fetchone()[0] is not None:
            rows = pg.execute(
                "SELECT name, value FROM ontology.load_state "
                "WHERE name IN ('change_seq', 'loaded_at', 'delta_at') ORDER BY name"
            ).fetchall()
            if rows:
                parts["load"] = [[str(n), str(v)] for n, v in rows]
        if not parts:
            return None
        out[STORE_SOURCE] = _canonical_mark(parts)

    return out


def _read_notes_and_optin(backend: Any, reader: Any) -> Optional[str]:
    """Live digest of the reader's personal notes (``memory.items``) and Assist opt-in
    state (``_apa_rows``, ``autopilot_engine.load_draft``, ``_apa_pools``, ``_apa_me``).
    Returns None when any piece is unreadable or unconfigured (fail closed)."""
    if backend is None or reader is None:
        return None
    store = getattr(backend, "memory", None)
    if store is None:
        return None
    try:
        reading = getattr(store, "_reading", None)
        if callable(reading) and reading(reader) is None:
            return None
        decide = getattr(backend, "_memory_decide", None)
        rows = store.items(
            reader, limit=None,
            decide=decide(reader, wait=True) if callable(decide) else None,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed when notes unreadable
        LOG.warning("ask_answer_cache: notes unreadable for open work: %s", type(exc).__name__)
        return None
    if rows is None:
        return None
    notes_digest = [
        {
            "id": r.get("id"),
            "kind": r.get("kind"),
            "at": r.get("at"),
            "text": r.get("text"),
            "records": list(r.get("records") or ()),
        }
        for r in sorted(rows, key=lambda r: (str(r.get("id") or ""), str(r.get("at") or "")))
    ]

    apa_state = getattr(backend, "_apa_state", None)
    apa_rows = getattr(backend, "_apa_rows", None)
    apa_me = getattr(backend, "_apa_me", None)
    apa_pools = getattr(backend, "_apa_pools", None)
    apa_owner = getattr(backend, "_apa_is_owner", None)
    if not all(callable(fn) for fn in (apa_state, apa_rows, apa_me, apa_pools, apa_owner)):
        return None
    try:
        from operating import assist_matters as am, autopilot_engine as ae
        from operating.card_actions import JournalBroken
        try:
            optin_rows = apa_rows(apa_state())
        except (OSError, ValueError, JournalBroken) as exc:
            LOG.warning("ask_answer_cache: opt-in journal unreadable: %s", type(exc).__name__)
            return None
        owner = bool(apa_owner(reader))
        ids = set(apa_me(reader)) - {"unknown"}
        pools = sorted(set(apa_pools(reader)))
        draft = ae.load_draft()
        shown = am.shown_to(ids, rows=optin_rows, draft=draft)
    except Exception as exc:  # noqa: BLE001 - fail closed when opt-in state unreadable
        LOG.warning("ask_answer_cache: opt-in state unreadable: %s", type(exc).__name__)
        return None
    if shown is None:
        return None
    payload = {
        "notes": notes_digest,
        "owner": owner,
        "ids": sorted(ids),
        "pools": pools,
        "shown": bool(shown),
        "draft": draft,
    }
    return hashlib.sha256(_canonical_mark(payload).encode("utf-8")).hexdigest()[:32]


def read_watermarks(sources: Iterable[str], *, asker: Any = None,
                    backend: Any = None, reader: Any = None) -> Optional[dict[str, str]]:
    """Read current watermarks for every source in ``sources``. Returns None if ANY source
    has an unreadable or missing watermark (fail closed)."""
    wanted = sorted({str(s) for s in sources if str(s)})
    if not wanted:
        return None
    hook = getattr(asker, "_watermarks", None) or getattr(backend, "_watermarks", None)
    if callable(hook):
        try:
            got = hook(wanted)
        except Exception as exc:  # noqa: BLE001 - fail closed on hook error
            LOG.warning("ask_answer_cache: watermarks hook failed: %s", type(exc).__name__)
            return None
        if not isinstance(got, Mapping):
            return None
        out: dict[str, str] = {}
        for src in wanted:
            if src not in got or got[src] is None:
                return None
            out[src] = _canonical_mark(got[src])
        return out

    views = [s[len(VIEW_PREFIX):] for s in wanted if s.startswith(VIEW_PREFIX)]
    need_matters = MATTERS_SOURCE in wanted
    need_readings = READINGS_SOURCE in wanted
    need_store = STORE_SOURCE in wanted
    need_open_work_state = OPEN_WORK_STATE_SOURCE in wanted
    unknown = [s for s in wanted if not s.startswith(VIEW_PREFIX)
               and s not in (MATTERS_SOURCE, READINGS_SOURCE, STORE_SOURCE, OPEN_WORK_STATE_SOURCE)]
    if unknown:
        return None

    open_work_mark: Optional[str] = None
    if need_open_work_state:
        open_work_mark = _read_notes_and_optin(backend, reader)
        if open_work_mark is None:
            return None

    if views or need_matters or need_readings or need_store:
        try:
            with _pg_connection(backend) as pg:
                pg_marks = _read_pg_watermarks(
                    pg, views, need_matters=need_matters,
                    need_readings=need_readings, need_store=need_store,
                )
        except Exception as exc:  # noqa: BLE001 - DB unreachable: fail closed
            LOG.warning("ask_answer_cache: reading watermarks failed: %s", type(exc).__name__)
            return None
        if pg_marks is None:
            return None
    else:
        pg_marks = {}

    if need_open_work_state and open_work_mark is not None:
        pg_marks[OPEN_WORK_STATE_SOURCE] = open_work_mark
    return pg_marks


def snapshot_all_watermarks(*, asker: Any = None, backend: Any = None,
                            reader: Any = None) -> Optional[dict[str, str]]:
    """Pre-run snapshot of all known watermarks so mid-run view refreshes prevent storing a
    stale answer stamped with a newer watermark.

    Returns ``_HOOK_NO_SNAPSHOT`` only when a test hook provides ``_watermarks`` without
    ``_watermarks_all``; returns ``None`` on any real Postgres/hook error or incomplete
    snapshot so ``put()`` fails closed instead of caching across an unverified window."""
    hook_all = getattr(asker, "_watermarks_all", None) or getattr(backend, "_watermarks_all", None)
    if callable(hook_all):
        try:
            got = hook_all()
        except Exception as exc:  # noqa: BLE001 - fail closed
            LOG.warning("ask_answer_cache: watermarks_all hook failed: %s", type(exc).__name__)
            return None
        if not isinstance(got, Mapping):
            return None
        return {str(k): _canonical_mark(v) for k, v in got.items() if v is not None}
    if callable(getattr(asker, "_watermarks", None)) \
            or callable(getattr(backend, "_watermarks", None)):
        return _HOOK_NO_SNAPSHOT
    try:
        from operating.pg import business_views as bv
        schema = bv.SCHEMA
        with _pg_connection(backend) as pg:
            views: set[str] = set()
            if pg.execute("SELECT to_regclass(%s)", (f"{schema}.view_state",)).fetchone()[0] \
                    is not None:
                views.update(str(r[0]) for r in pg.execute(
                    f"SELECT view FROM {schema}.view_state").fetchall())
            if pg.execute("SELECT to_regclass(%s)", (f"{schema}.state",)).fetchone()[0] \
                    is not None:
                views.update(str(r[0]) for r in pg.execute(
                    f"SELECT view FROM {schema}.state").fetchall())
            has_matters = pg.execute(
                "SELECT to_regclass('assist.matters_heartbeat')"
            ).fetchone()[0] is not None
            has_readings = any(
                pg.execute("SELECT to_regclass(%s)", (rel,)).fetchone()[0] is not None
                for rel in ("assist.card_readings", "assist.readings", "assist.reading_runs")
            )
            snap = _read_pg_watermarks(
                pg, sorted(views), need_matters=False, need_store=True,
            )
            if snap is None:
                return None
            if has_matters:
                m = _read_pg_watermarks(pg, (), need_matters=True, need_store=False)
                if m:
                    snap.update(m)
            if has_readings:
                r_mark = _read_pg_watermarks(
                    pg, (), need_matters=False, need_store=False, need_readings=True,
                )
                if r_mark:
                    snap.update(r_mark)
            if backend is not None and reader is not None:
                ow_mark = _read_notes_and_optin(backend, reader)
                if ow_mark is not None:
                    snap[OPEN_WORK_STATE_SOURCE] = ow_mark
            return snap
    except Exception as exc:  # noqa: BLE001 - DB unreachable: fail closed (never cache)
        LOG.warning("ask_answer_cache: pre-run watermark snapshot failed: %s", type(exc).__name__)
        return None


@dataclass
class CachedEntry:
    key: str
    reader_key: str
    door_mark: str
    watermarks: dict[str, str]
    as_of: Optional[str]
    stored_at: float
    run_data: dict[str, Any]

    @property
    def text(self) -> str:
        return str(self.run_data.get("text") or "")


class AnswerCache:
    """Thread-safe, bounded LRU cache of verified Ask answers keyed on inputs, reader door,
    and per-view watermarks."""

    def __init__(self, max_entries: int = ANSWER_CACHE_MAX_ENTRIES,
                 max_age_seconds: float = ANSWER_CACHE_MAX_AGE_SECONDS) -> None:
        self.max_entries = max(1, int(max_entries))
        self.max_age_seconds = float(max_age_seconds)
        self._entries: "OrderedDict[str, CachedEntry]" = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def invalidate_reader(self, reader_key: str) -> int:
        """Drop every kept answer belonging to ``reader_key``."""
        with self._lock:
            gone = [k for k, v in self._entries.items() if v.reader_key == reader_key]
            for k in gone:
                del self._entries[k]
            return len(gone)

    def get(self, key: str, *, reader_key: str, door_mark: str,
            asker: Any = None, backend: Any = None, reader: Any = None,
            now: Optional[float] = None) -> tuple[Optional[CachedEntry], str]:
        """Look up ``key`` and verify reader, door mark, age, and live watermarks for every
        source read. Always checks live watermarks before serving (fail closed)."""
        ts = time.monotonic() if now is None else now
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return None, "miss"
        if entry.reader_key != reader_key or entry.door_mark != door_mark:
            self._evict_if_same(key, entry)
            return None, "door_changed"
        if ts - entry.stored_at > self.max_age_seconds:
            self._evict_if_same(key, entry)
            return None, "expired"

        current = read_watermarks(
            entry.watermarks.keys(), asker=asker, backend=backend, reader=reader,
        )
        if current is None:
            return None, "watermarks_unreadable"
        if current != entry.watermarks:
            self._evict_if_same(key, entry)
            return None, "watermark_moved"

        with self._lock:
            if self._entries.get(key) is entry:
                self._entries.move_to_end(key)
        return entry, "hit"

    def put(self, key: str, *, reader_key: str, door_mark: str, ran: Any,
            asker: Any = None, backend: Any = None, reader: Any = None,
            before_marks: Any = _UNSET, comms_ok: Optional[bool] = None,
            now: Optional[float] = None) -> tuple[bool, str]:
        """Store a clean ``SimpleRun`` with the exact watermarks of every source it read."""
        if before_marks is None:
            return False, "pre_run_snapshot_failed"
        sources, why = extract_sources(ran)
        if sources is None:
            return False, why
        if needs_comms_scope(sources, ran):
            if comms_ok is None:
                comms_ok = resolve_comms_digest(
                    asker, getattr(reader, "session", None),
                    backend=backend, reader=reader,
                ) is not None
            if not comms_ok:
                return False, "comms_scope_unavailable"
        marks = read_watermarks(sources, asker=asker, backend=backend, reader=reader)
        if marks is None:
            return False, "watermarks_unreadable"
        if before_marks is not _UNSET and before_marks is not _HOOK_NO_SNAPSHOT:
            for src, after_val in marks.items():
                if src not in before_marks or before_marks[src] != after_val:
                    return False, "moved_during_run"

        ts = time.monotonic() if now is None else now
        run_data = {
            "text": str(ran.text),
            "agent_text": str(ran.agent_text),
            "unverified": list(ran.unverified),
            "events": copy.deepcopy(list(ran.events)),
            "as_of": ran.as_of,
            "charts": copy.deepcopy(list(ran.charts)),
            "clarify": copy.deepcopy(ran.clarify),
            "work_items": copy.deepcopy(list(ran.work_items)),
            "gaps": copy.deepcopy(list(ran.gaps)),
            "check": copy.deepcopy(ran.check),
            "working": list(ran.working),
            "coverage": copy.deepcopy(list(ran.coverage)),
        }
        entry = CachedEntry(
            key=key,
            reader_key=reader_key,
            door_mark=door_mark,
            watermarks=dict(marks),
            as_of=ran.as_of,
            stored_at=ts,
            run_data=run_data,
        )
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return True, "stored"

    def _evict_if_same(self, key: str, entry: CachedEntry) -> None:
        with self._lock:
            if self._entries.get(key) is entry:
                del self._entries[key]


CACHE = AnswerCache()


def to_simple_run(entry: CachedEntry, *, seconds: float) -> Any:
    """Materialize a fresh ``SimpleRun`` copy from a verified ``CachedEntry``."""
    from .simple_ask import SimpleRun
    d = entry.run_data
    return SimpleRun(
        text=str(d["text"]),
        agent_text=str(d["agent_text"]),
        unverified=list(d["unverified"]),
        events=copy.deepcopy(d["events"]),
        seconds=seconds,
        stopped=False,
        failure=None,
        usage={
            "cached": True,
            "cost_usd_estimate": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "duration_api_ms": 0,
            "cache_sources": sorted(entry.watermarks),
        },
        as_of=d["as_of"],
        charts=copy.deepcopy(d["charts"]),
        clarify=copy.deepcopy(d["clarify"]),
        work_items=copy.deepcopy(d["work_items"]),
        gaps=copy.deepcopy(d["gaps"]),
        check=copy.deepcopy(d["check"]),
        working=list(d["working"]),
        coverage=copy.deepcopy(d["coverage"]),
        cached=True,
    )


__all__ = [
    "ANSWER_CACHE_ENV",
    "ANSWER_CACHE_MAX_AGE_SECONDS",
    "ANSWER_CACHE_MAX_ENTRIES",
    "ANSWER_CACHE_VERSION",
    "CACHE",
    "COMMS_TOOLS",
    "COMMS_VIEWS",
    "MATTERS_SOURCE",
    "OPEN_WORK_STATE_SOURCE",
    "READINGS_SOURCE",
    "READ_ONLY_TOOLS",
    "STORE_SOURCE",
    "VIEW_PREFIX",
    "AnswerCache",
    "CachedEntry",
    "cache_identity",
    "cache_identity_state",
    "code_prompt_digest",
    "eligible",
    "enabled",
    "extract_sources",
    "freshness_label",
    "needs_comms_scope",
    "normalize_question",
    "read_watermarks",
    "resolve_comms_digest",
    "resolve_door_mark",
    "resolve_door_state",
    "snapshot_all_watermarks",
    "status_message",
    "to_simple_run",
]
