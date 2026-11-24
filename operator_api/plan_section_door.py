"""A record page's plan sections read from the precomputed rows (``operating.pg.plan_sections``)
instead of running the question plan for the reader (seconds, cold).

One read through the page door (``plan_section``): the row is there only when the reader holds
the order's class and the class of every business record the plan cited, and nothing it depends on has moved since the
plan ran (decided in Postgres by
the guarded view). The cited mail and attachments are decided here by the reader's own live
decision (``access.decide``, the one ``plx.investigate`` marks a citation ``openable`` with):
any one the reader may not open, a row that is missing, one that cannot be read, a switch that
is not exactly ``1``, a record of another kind: the section is read live, never a reduced one.

A row that is OLD (built on an earlier business day, or something it depends on has moved since)
is still served, with its age (``stale_scope``: the section says ``stale_since``), and its record
is picked up again by the resident runner on its own (``plan_resident.discover`` reads the same
condition: moved rows first); a request never runs the plan because a row is old. A fallback is
named: every read counts as ``served``, ``served_stale`` or ``fallback:<reason>`` (``rates``) and
a fallback is logged with its reason.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

SWITCH = "PLX_PLAN_SECTIONS"
FAMILY_SECTION = {"late_po": "late", "people_on_record": "people", "landed_cost": "landed",
                  "po_trail": "trail"}   # the plan families stored (``plan_sections.FAMILY``)
KINDS = ("PurchaseOrder",)
LOG = logging.getLogger("operator_api.plan_section_door")
BUSINESS_ZONE = ZoneInfo("America/New_York")   # the business day a "late by N days" is said for


def _day(value: Any) -> Optional[date]:
    """The business day (ET) of an ISO time, None when it is not one."""
    try:
        got = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (got if got.tzinfo else got.replace(tzinfo=timezone.utc)).astimezone(BUSINESS_ZONE).date()


def fresh(row: Mapping[str, Any], today: Optional[date] = None) -> bool:
    """Whether a stored answer was made on today's business day (its text counts days: "Late by
    N days"). An older one is served with its age, not refused (``precomputed``)."""
    ran, now = _day(row.get("built_at")), today or datetime.now(BUSINESS_ZONE).date()
    return ran is not None and ran == now


def on() -> bool:
    """``PLX_PLAN_SECTIONS=1`` exactly (anything else, a space or ``true`` included, is off)."""
    return os.environ.get(SWITCH, "") == "1"


_RATES: dict[str, int] = {}
_RATES_LOCK = threading.Lock()
_STALE: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar("plan_stale", default=None)
_NOT_RUN: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar("plan_not_run", default=None)
_MISS: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("plan_miss", default=None)
# a miss for a reason about THIS reader (a row exists; the reader may not open what it cites): the
# reader's own plan is the only right answer, so it runs live. Every other miss (no row, an unreadable
# or old-format row) is answered ``not_run`` with a request to the runner: no plan runs in the request
READER_OWN = ("mail_not_openable",)


RATES_LOG_EVERY = 3600.0
_RATES_LAST = [time.monotonic()]


def count(outcome: str) -> None:
    """One read's outcome counted; every hour the totals since the process started are logged
    (``plan sections read: {...}``), which is how the served-versus-fallback rate is measured."""
    with _RATES_LOCK:
        _RATES[outcome] = _RATES.get(outcome, 0) + 1
        due = time.monotonic() - _RATES_LAST[0] >= RATES_LOG_EVERY
        if due:
            _RATES_LAST[0] = time.monotonic()
            totals = dict(_RATES)
    if due:
        LOG.info("plan sections read: %s", json.dumps(totals, sort_keys=True))


def rates() -> dict[str, int]:
    """How the reads since this process started ended: ``served`` (a current row), ``served_stale``
    (an old row, with its age) and ``fallback:<reason>`` (the page read the plan live)."""
    with _RATES_LOCK:
        return dict(_RATES)


@contextlib.contextmanager
def stale_scope():
    """The built-at times of the old rows served inside the block (the section's ``stale_since``
    is the oldest)."""
    seen: list = []
    token = _STALE.set(seen)
    try:
        yield seen
    finally:
        _STALE.reset(token)


@contextlib.contextmanager
def not_run_scope():
    """The families answered ``not_run`` inside the block, each as
    ``{"family", "why", "requested"}`` (the section says so, typed)."""
    seen: list = []
    token = _NOT_RUN.set(seen)
    try:
        yield seen
    finally:
        _NOT_RUN.reset(token)


def last_miss() -> Optional[str]:
    """Why the last ``precomputed`` of this request gave no row (None when it gave one)."""
    return _MISS.get()


def answer_not_run(oid: str, family: str, why: str) -> None:
    """Record that ``family`` of ``oid`` is answered ``not_run`` (no plan runs) and ask the runner
    for it; a request that cannot be made is said (``requested`` false), never hidden."""
    from operating import plan_requests
    sent = plan_requests.request(oid)
    count("not_run")
    seen = _NOT_RUN.get()
    if seen is not None:
        seen.append({"family": family, "why": why, "requested": sent, "status": "not_run"})


def _fallback(reason: str, kind: str, oid: str, family: str, exc: Optional[BaseException] = None,
              detail: str = "") -> None:
    _MISS.set(reason)
    count("fallback:" + reason)
    LOG.warning("plan section: no stored row for this reader (%s): %s %s %s%s", reason, family, kind, oid[:80],  # guard-ok: a log line shortens the record id it names
                (f" {type(exc).__name__}: {str(exc)[:120]}" if exc is not None else "")  # guard-ok: a log line shortens the error text
                + (f" {detail}" if detail else ""))


def precomputed(view: Any, kind: str, oid: str, family: str, today: Optional[date] = None
                ) -> Optional[Mapping[str, Any]]:
    """The plan run of ``family`` over ``oid`` as the section's presentation reads it
    (``{"verdict": ...}``), from the stored row; None only when the live plan must run: no row
    for this reader (none made, its class or a cited record's not held, a cited record moved on),
    a cited mail or attachment the reader may not open, a row that is not what it says. Each
    outcome is counted and a fallback says why. An old row is served (``stale_scope``)."""
    _MISS.set(None)
    section = FAMILY_SECTION.get(family)
    if not on() or section is None or kind not in KINDS or not oid or len(oid) > 200:
        return None
    from operating import page_reads
    try:
        out = view._call(page_reads.TOOL_NAME, {"reads": [
            {"name": "plan_section", "params": {"record_id": oid, "section": section}}]})
        reads = (out or {}).get("reads", {})
        got = reads.get("plan_section")
        if not isinstance(got, Mapping) or "rows" not in got:
            # the door said no rather than gave rows (an error or a refusal): say what it said
            _fallback("read_error", kind, oid, family,
                      detail=json.dumps((got if got is not None else out), default=str)[:200])  # guard-ok: a log line shortens what the door said
            return None
        cols, rows = got["columns"], got["rows"]
        if len(rows) != 1:
            _fallback("no_row", kind, oid, family)
            return None
        row = dict(zip(cols, rows[0]))
        payload = json.loads(row["payload_text"])
        if row["section"] != section or payload.get("family") != family:
            _fallback("wrong_row", kind, oid, family)
            return None
        if isinstance(payload.get("failed"), str):
            # the runner's run of this plan FAILED (the exact reason is stored): said as a failure,
            # never shown as an answer, never run in a request; the runner runs it again
            seen = _NOT_RUN.get()
            when = payload.get("next_try_at")
            why = ("the plan failed when it ran (attempt %s) and is run again%s: " % (
                payload.get("attempts") or 1, f" from {when}" if isinstance(when, str) else "")
                   ) + payload["failed"]
            if seen is not None:
                seen.append({"family": family, "why": why, "requested": False, "stored": True,
                             "status": "failed"})
            count("failed")
            return {"no_answer": why, "status": "failed"}
        if isinstance(payload.get("no_answer"), str):
            # the runner ran this plan and it gave no complete answer (a typed reason, stored): said
            # as such, never run again in a request
            seen = _NOT_RUN.get()
            if seen is not None:
                seen.append({"family": family, "why": payload["no_answer"], "requested": False,
                             "stored": True, "status": "not_run"})
            count("no_answer")
            return {"no_answer": payload["no_answer"]}
        if family == "people_on_record" and not isinstance(payload.get("read"), list):
            # a row made before the row depended on everything its run read: who it names was
            # found through evidence it does not list, so it is no row for any reader but the owner
            _fallback("no_read_set", kind, oid, family)
            return None
        comm = [h for h in row.get("comm_handles") or () if isinstance(h, str)]
        if comm:
            decided = view.access.decide(comm)
            if any(decided.get(h) is not None for h in comm):
                _fallback("mail_not_openable", kind, oid, family)
                return None
        built = row.get("built_at")
        day_old = not fresh(row, today)
        moved = row.get("current") is not True
        if day_old or moved:
            seen = _STALE.get()
            if seen is not None and isinstance(built, str):
                seen.append(built)
            count("served_stale")
        else:
            count("served")
        note = {"built_at": built, "stale": "day" if day_old else ("moved" if moved else None)}
        if family == "people_on_record":
            # the plan's own entries, read by the page's own ``people_of``
            return {"investigation": {"answer": list(payload["entries"])}, "precomputed": note}
        if family == "po_trail":
            return {"trail": dict(payload["raw"]), "precomputed": note}
        if family == "landed_cost":
            # the plan's own computed entries and what it says is missing, read by the page's own
            # ``landed_cost_of``
            return {"investigation": {"answer": list(payload["entries"]),
                                      "answerability": {"state": payload.get("answerability")}},
                    "verdict": {"follow_ups": [{"missing": x} for x in payload.get("missing") or ()]},
                    "precomputed": note}
        facts = [{"text": f["text"], "citation": f["citation"], "openable": True}
                 for f in payload.get("facts") or ()]
        return {"verdict": {"state": payload["state"], "text": payload.get("text") or "",
                            "facts": facts, "follow_ups": []},
                "precomputed": note}
    except Exception as exc:  # noqa: BLE001 - speed only: the plan is read live, and says so
        _fallback("read_failed", kind, oid, family, exc)
        return None
