"""The one envelope every served number, list and feed travels in (contract C1, master plan
section 4.3), and the two legal ways to shorten or to swallow: ``cut`` and ``fail``.

    Value(value, status, total, as_of, why, source_job)

``status`` is one of ``VALUE_STATUSES``: ``ok`` (the whole thing, current), ``partial`` (some of it
could not be read; ``why`` says which), ``cut`` (a list shortened; ``total`` is exact and
``value["next"]`` reaches the rest), ``stale`` (older than its source; ``as_of`` says how old),
``unknown`` (no reading exists: never shown as 0), ``not_run`` (the job that makes it has not
run), ``off`` (switched off on purpose), ``failed`` (reading it failed; ``why`` names what).
A list's ``value`` is ``{"items": [...], "total": n, "next": cursor-or-None}``.

The app renders it with ``app/src/lib/value.ts`` (the same statuses), ``components/ui/
status-line.tsx`` (anything not ``ok``) and ``components/ui/cut-list.tsx`` (a ``cut``).
A status outside ``VALUE_STATUSES`` is refused on both sides (fail closed): an envelope nobody can
render is never shown as fine.

Cutting text for display stays ``operating.limits.cut``; ``cut`` here is the served form of a
list cut (``operating.limits.cut_list``) with the way to the rest.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from operating.limits import cut_list

VALUE_STATUSES = ("ok", "partial", "cut", "stale", "unknown", "not_run", "off", "failed")
_FIELDS = ("value", "status", "total", "as_of", "why", "source_job")


@dataclass(frozen=True)
class Value:
    value: Any = None
    status: str = "ok"
    total: Optional[int] = None
    as_of: Optional[str] = None
    why: Optional[str] = None
    source_job: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in VALUE_STATUSES:
            raise ValueError(f"unknown status {self.status!r}")
        if self.status != "ok" and not (self.why or self.as_of):
            # anything not ok says why (or, stale, since when): never a bare status
            raise ValueError(f"status {self.status!r} without why or as_of")
        if self.total is not None and (not isinstance(self.total, int) or self.total < 0):
            raise ValueError("total must be a count")

    def to_json(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in _FIELDS}


def parse(raw: Any) -> Value:
    """An envelope read back (from the store, a payload): every field checked, an unknown
    status or a missing one refused (ValueError), never read as ``ok``."""
    if not isinstance(raw, Mapping) or "status" not in raw:
        raise ValueError("not a value envelope")
    extra = set(raw) - set(_FIELDS)
    if extra:
        raise ValueError(f"unknown envelope fields {sorted(extra)}")
    return Value(**{k: raw.get(k) for k in _FIELDS})


def listed(items: Sequence[Any], *, total: Optional[int] = None, next: Any = None,
           why: Optional[str] = None, as_of: Optional[str] = None,
           source_job: Optional[str] = None) -> Value:
    """A list as the envelope: ``ok`` when it is whole, ``cut`` when ``total`` says more exist
    (then ``why`` is required and ``next`` reaches the rest)."""
    shown = list(items)
    n = len(shown) if total is None else total
    if n < len(shown):
        raise ValueError(f"total {n} below the {len(shown)} items shown")
    if n > len(shown):
        if not why:
            raise ValueError("a cut list says why it is cut")
        if next is None:
            raise ValueError("a cut list says how to reach the rest (next)")
        return Value({"items": shown, "total": n, "next": next}, "cut", n, as_of, why,
                     source_job)
    return Value({"items": shown, "total": n, "next": None}, "ok", n, as_of, None, source_job)


def cut(rows: Sequence[Any], n: int, reason: str, *, next: Any = None,
        as_of: Optional[str] = None, source_job: Optional[str] = None) -> Value:
    """The first ``n`` of ``rows`` (every row known: ``total`` exact) with the reason and the
    way to the rest (``next``; default the offset of the first row left out)."""
    kept = cut_list(rows, n, say=reason)
    if not kept.cut:
        return listed(kept.items, as_of=as_of, source_job=source_job)
    return listed(kept.items, total=kept.total, next=n if next is None else next,
                  why=kept.note, as_of=as_of, source_job=source_job)


def fail(reason: str, exc: Optional[BaseException] = None, *, source_job: Optional[str] = None,
         logger: Optional[logging.Logger] = None, as_of: Optional[str] = None) -> Value:
    """A reading that failed: logged (with the exception's type and first line), returned as a
    ``failed`` envelope the caller must pass on. ``reason`` is the person-facing sentence
    (what could not be read); the exception's text never reaches it."""
    if not reason:
        raise ValueError("a failure says what failed")
    detail = ""
    if exc is not None:
        first = (str(exc).strip().splitlines() or [""])[0]
        detail = f": {type(exc).__name__}: {first}" if first else f": {type(exc).__name__}"
    (logger or logging.getLogger("operating.value")).warning(
        "failed%s%s%s", f" [{source_job}]" if source_job else "", f" {reason}", detail)
    return Value(None, "failed", None, as_of, reason, source_job)


__all__ = ["VALUE_STATUSES", "Value", "cut", "fail", "listed", "parse"]
