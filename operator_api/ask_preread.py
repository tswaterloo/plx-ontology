"""Reads that run alongside the model's first turn (Ask spec 19 S2, A1): for every
identifier or name the question names (from the one shared definition
``operating.session_prefetch.sql_reads``), the host runs the cheap deterministic
``plx.sql`` reads (``{"record": ...}`` for codes and ``{"names": ...}`` for names) as
the reader, placing their ``sql://`` results into the host's result store.

Fast reads that finish within ``PROMPT_WAIT_SECONDS`` enter the turn-1 prompt directly so
a status question can answer in one turn. Slower reads keep running in the background
alongside the model's first turn (no serial 20+5 s stall), and the prompt explicitly tells
the model which reads are still running, skipped or failed."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import threading
from typing import Any, Callable, Mapping, Optional, Sequence

from .ask_refs import _parsed

LOG = logging.getLogger("operator_api.ask")

HEADING = (
    "Already read for this question as this reader (each result is in the result store "
    "under its sql:// id: cite its figures with {{...}} or page it directly with "
    "plx.sql {\"page\": \"sql://...\", \"offset\": N} without repeating these reads; "
    "nothing here was decided for you):"
)

# Brief overlap window so a fast record card (< 50 ms) enters turn 1 directly while slow
# reads continue in the background alongside the model's first turn (never a serial stall).
PROMPT_WAIT_SECONDS = 0.5
PROMPT_RESULT_CHARS = 40_000


def reads_for(question: str, *, context_record: Optional[str] = None
              ) -> list[tuple[str, dict[str, Any]]]:
    """The deterministic ``plx.sql`` pre-reads for ``question`` (delegates to the one
    shared definition ``operating.session_prefetch.sql_reads``)."""
    from operating import session_prefetch
    return session_prefetch.sql_reads(question, context_record=context_record)


def valid_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pre-read ``tool_call`` events that returned a non-error result stored under a
    result id (``sql://...``)."""
    kept: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "tool_call" or event.get("is_error"):
            continue
        parsed = _parsed(event.get("result"))
        if not isinstance(parsed, Mapping) or parsed.get("error"):
            continue
        rid = parsed.get("result")
        if isinstance(rid, str) and rid.startswith("sql://"):
            kept.append(dict(event))
    return kept


def error_reason(event: Mapping[str, Any]) -> Optional[str]:
    """Why a completed pre-read event did not produce a usable result (None when valid)."""
    parsed = _parsed(event.get("result"))
    if event.get("is_error"):
        if isinstance(parsed, Mapping):
            err = parsed.get("error")
            if isinstance(err, Mapping):
                return f"{err.get('code') or 'error'}: {err.get('message') or 'failed'}"
            return str(err or parsed.get("message") or "tool error")
        return "tool error"
    if isinstance(parsed, Mapping) and parsed.get("error"):
        err = parsed["error"]
        if isinstance(err, Mapping):
            return f"{err.get('code') or 'error'}: {err.get('message') or 'failed'}"
        return str(err)
    rid = parsed.get("result") if isinstance(parsed, Mapping) else None
    if not (isinstance(rid, str) and rid.startswith("sql://")):
        return "no result id returned"
    return None


@dataclass
class PrereadBatch:
    """Snapshot of pre-reads at the moment the model's first turn starts."""
    events: list[dict[str, Any]] = field(default_factory=list)
    failed: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)
    skipped: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)
    job: Any = None


def _compact_result(parsed: Mapping[str, Any], max_chars: int) -> str:
    """Compact JSON for one pre-read result; if the full rows exceed ``max_chars``, keep
    its result id, scope, profile, columns, chain and card summary and point to paging so
    the result id is always usable."""
    text = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    summary = {k: parsed[k] for k in ("result", "scope", "columns", "row_count", "profile",
                                      "views", "as_of", "chain", "card", "note", "truncated")
               if k in parsed}
    summary["rows_in_store"] = True
    summary["page"] = {"page": parsed.get("result"), "offset": 0}
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


def prompt_section(events: Sequence[Mapping[str, Any]], *,
                   failed: Sequence[tuple[str, Mapping[str, Any], str]] = (),
                   skipped: Sequence[tuple[str, Mapping[str, Any], str]] = (),
                   max_chars: int = PROMPT_RESULT_CHARS) -> str:
    """The prompt block appended to the turn's task listing pre-read results held in the
    result store and any pre-reads that failed, were skipped, or are still running in the
    background; empty string when nothing ran or was noted."""
    sections: list[str] = []
    shown: list[str] = []
    for event in valid_events(events):
        parsed = _parsed(event.get("result"))
        if not isinstance(parsed, Mapping):
            continue
        called = event.get("arguments") or {}
        body = _compact_result(parsed, max_chars)
        shown.append(f"{event.get('name')} {json.dumps(called, ensure_ascii=False)} returned:\n{body}")
    if shown:
        sections.append(HEADING + "\n\n" + "\n\n".join(shown))
    notes: list[str] = []
    for name, args, why in failed:
        notes.append(f"- {name} {json.dumps(dict(args), ensure_ascii=False)}: failed ({why}); "
                     "do not assume it does not exist — query or search directly if needed.")
    for name, args, why in skipped:
        notes.append(f"- {name} {json.dumps(dict(args), ensure_ascii=False)}: {why}.")
    if notes:
        sections.append("Pre-read notes (not yet in the prompt):\n" + "\n".join(notes))
    return "\n\n".join(sections)


def _split_raw(reads: Sequence[tuple[str, Mapping[str, Any]]],
               raw: Sequence[Mapping[str, Any]]) -> PrereadBatch:
    events: list[dict[str, Any]] = []
    failed: list[tuple[str, dict[str, Any], str]] = []
    seen_keys: set[str] = set()
    for event in raw or ():
        name = str(event.get("name") or "")
        args = dict(event.get("arguments") or {})
        seen_keys.add(json.dumps([name, args], sort_keys=True))
        why = error_reason(event)
        if why is None:
            events.append(dict(event))
        else:
            failed.append((name, args, why))
    skipped: list[tuple[str, dict[str, Any], str]] = []
    for name, args in reads:
        key = json.dumps([name, dict(args)], sort_keys=True)
        if key not in seen_keys:
            skipped.append((name, dict(args), "skipped before turn started"))
    return PrereadBatch(events=events, failed=failed, skipped=skipped)


def start_on_session(session: Any, reads: Sequence[tuple[str, Mapping[str, Any]]], *,
                     cancel: Optional[threading.Event] = None,
                     wait_seconds: float = PROMPT_WAIT_SECONDS,
                     emit: Optional[Callable[[str, Mapping[str, Any]], None]] = None
                     ) -> PrereadBatch:
    """Start ``reads`` alongside the turn on ``session``. Uses ``session.start_preread``
    (background execution joined by ``serve_gate`` if still running when the model calls
    it) and waits at most ``wait_seconds`` (waking immediately on ``cancel``) so fast
    cards enter turn 1 while slower reads run concurrently with the model's first turn."""
    if not reads:
        return PrereadBatch()
    if cancel is not None and cancel.is_set():
        return PrereadBatch(skipped=[(n, dict(a), "skipped (cancelled)") for n, a in reads])
    starter = getattr(session, "start_preread", None)
    if callable(starter):
        try:
            job = starter(reads, cancel=cancel)
            if job is None:
                return PrereadBatch()
            job.wait(wait_seconds, cancel=cancel)
            raw_events, raw_failed, raw_skipped = job.snapshot()
            batch = PrereadBatch(events=valid_events(raw_events), failed=list(raw_failed),
                                 skipped=list(raw_skipped), job=job)
        except Exception:  # noqa: BLE001 - pre-read failure never breaks the Ask turn
            LOG.warning("ask_preread: session start_preread failed", exc_info=True)
            return PrereadBatch(failed=[(n, dict(a), "preread_error") for n, a in reads])
    else:
        preread = getattr(session, "preread", None)
        if not callable(preread):
            return PrereadBatch()
        try:
            raw = preread(reads)
        except Exception:  # noqa: BLE001 - pre-read failure never breaks the Ask turn
            LOG.warning("ask_preread: session preread failed", exc_info=True)
            return PrereadBatch(failed=[(n, dict(a), "preread_error") for n, a in reads])
        batch = _split_raw(reads, raw or ())
    if emit is not None:
        for event in batch.events:
            emit("call", {"tool": event.get("name"), "input": event.get("arguments")})
            emit("result", {"tool": event.get("name"), "error": False,
                            "result": event.get("result")})
    return batch


def run_on_session(session: Any, reads: Sequence[tuple[str, Mapping[str, Any]]],
                   emit: Optional[Callable[[str, Mapping[str, Any]], None]] = None
                   ) -> list[dict[str, Any]]:
    """Compatibility wrapper returning valid pre-read events from ``start_on_session``."""
    return start_on_session(session, reads, emit=emit).events
