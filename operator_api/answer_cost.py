"""What the model run behind each Ask answer cost, carried from where the answer is made
(``simple_ask.answer_payload``) to where it is logged (the ask route's ``_review``).

The answer a person receives goes through ``presentation.polished``, which rebuilds it, so
the cost cannot ride on the answer object: it waits here by ``answer_id`` until the route
takes it (once). The figure is what the run itself reports (``total_cost_usd`` of its result
line, summed over the runs an answer took: the first read, a chart redo, a figure repair, a
sentence patch, a check repair). It is the run's own estimate, not the invoice; whether a kept
session's run reports only that turn is not yet checked against ``usage-*.jsonl``
(KNOWN-GAPS GI2), so Data health says "as the run reports it".

A waiting entry no route ever takes (an answer that never reached the stream) is dropped
oldest first once ``WAITING`` are held, and that is logged, never silent.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

log = logging.getLogger("operator_api.answer_cost")
WAITING = 5000
_held: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_lock = threading.Lock()
KEYS = ("cost_usd_estimate", "input_tokens", "output_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens", "duration_api_ms")


def _num(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def joined(first: Mapping[str, Any], again: Mapping[str, Any]) -> dict[str, Any]:
    """``first``'s usage figures plus ``again``'s (a repair run is part of the same answer's
    cost); a figure either run did not report stays the other's, never 0."""
    out: dict[str, Any] = {}
    for key in KEYS:
        a, b = _num(first.get(key)), _num(again.get(key))
        if a is not None or b is not None:
            total = (a or Decimal(0)) + (b or Decimal(0))
            out[key] = float(total) if key == "cost_usd_estimate" else int(total)
    return out


def figures(usage: Mapping[str, Any]) -> dict[str, Any]:
    """The cost figures of a run's usage that it reported (none: an empty dict)."""
    out: dict[str, Any] = {}
    for key in KEYS:
        n = _num(usage.get(key))
        if n is not None:
            out[key] = float(n) if key == "cost_usd_estimate" else int(n)
    return out


def note(answer_id: str, usage: Mapping[str, Any]) -> None:
    got = figures(usage)
    if not got:
        return
    with _lock:
        _held[answer_id] = got
        while len(_held) > WAITING:
            gone, _ = _held.popitem(last=False)
            log.warning("answer cost of %s dropped: %d waiting entries never taken", gone, WAITING)


def take(answer_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        return _held.pop(answer_id, None)
