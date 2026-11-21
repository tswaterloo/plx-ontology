"""Calendar windows and page cursors of the traverse tools: whether an event falls
outside a span, overlaps in a window, and the opaque cursor of a paged read (split
out of ``ontology_tools_traverse``, module size cap)."""
from __future__ import annotations

import base64
import datetime as dt
import json
import time
from typing import Any, Mapping, Sequence
from ontology.store import instant
from operating.ontology_tools_identity import ToolError
from operating.ontology_tools_policy import _format_time, _parse_time


def _day_past(moment: dt.datetime | None, days: int) -> str | None:
    """The date ``days`` from ``moment`` (YYYY-MM-DD), None without one or past the
    calendar's ends (no prefilter then)."""
    if moment is None:
        return None
    try:
        return (moment + dt.timedelta(days=days)).date().isoformat()
    except OverflowError:
        return None


def _outside(row: Mapping[str, Any], span: tuple[dt.datetime, dt.datetime],
             since: tuple[str | None, dt.datetime | None],
             until: tuple[str | None, dt.datetime | None]) -> bool:
    """Whether an event falls outside the ``since``/``until`` bounds (each its text and
    instant). A business date is the calendar day as recorded, never a UTC day: when the
    event or the bound is a bare date, the dates as written compare (a timestamp's own
    date in its recorded offset); two timestamps, or a capture window, compare as
    instants."""
    if row["time_basis"] == "capture_observed":
        return (since[1] is not None and span[1] < since[1]
                or until[1] is not None and span[0] > until[1])
    day, dated = row["time"][:10], len(row["time"]) == 10
    for (text, moment), after in ((since, False), (until, True)):
        if text is None or moment is None:
            continue
        if dated or len(text) == 10:
            if (day > text[:10]) if after else (day < text[:10]):
                return True
        elif (span[0] > moment) if after else (span[1] < moment):
            return True
    return False


def _window_conflicts(events: Sequence[tuple]) -> list[dict]:
    """Capture windows overlapping an occurred time on a shared object: their order
    is unknown, so it is reported, never asserted."""
    windows = [(e, span) for key, e, span in events if e["time_basis"] == "capture_observed"]
    occurred = [(e, span) for key, e, span in events if key[0] == 0]
    out = []
    for win, wspan in windows:
        objects = {o["object_id"] for o in win["objects"]}
        for other, ospan in occurred:
            if not objects & {o["object_id"] for o in other["objects"]}:
                continue
            if ospan[0] <= wspan[1] and wspan[0] <= ospan[1]:
                out.append({"kind": "time_basis_conflict",
                            "subject": sorted(objects & {o["object_id"]
                                                         for o in other["objects"]})[0][:700],
                            "events": [win["event_id"], other["event_id"]],
                            "note": (f"{win['event_type']} was observed in a capture window "
                                     f"that overlaps {other['event_type']} "
                                     f"({other['time_basis']}); their order is unknown")[:600]})
                if len(out) >= 50:
                    return out
    return out


def _write_cursor(offset: int, at: dt.datetime, fingerprint: str, handle: str | None,
                  explicit: bool) -> str:
    raw = json.dumps({"o": offset, "t": _format_time(at), "f": fingerprint, "s": handle,
                      "x": explicit}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _read_cursor(cursor: str, fingerprint: str
                 ) -> tuple[int, tuple[dt.datetime, bool], str | None]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        body = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        offset, at = int(body["o"]), _parse_time(body["t"])
        handle = body.get("s")
        if (body["f"] != fingerprint or offset < 0 or at is None
                or not isinstance(body.get("x"), bool)
                or handle is not None and not isinstance(handle, str)):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ToolError("invalid_cursor", "the cursor does not belong to this query",
                        "repeat the call without cursor") from None
    return offset, (at, body["x"]), handle


# --------------------------------------------------------------------------- host wiring
