"""plx.sql's rows as the model is shown them and the host's store of every result
(split from ``ontology_tools_sql``, re-exported there): the cell form the contract types,
the shown rows within the client's window, and the result store that paging and the
answer's references read (Ask plan A1)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
from collections import OrderedDict
from decimal import Decimal
from typing import Any, Mapping, Optional, Sequence

from operating import result_profile
from operating.pg import business_views as bv

MAX_BYTES = 40_000
SHOWN_ROWS = 200   # rows one result shows (the contract's rows maxItems); the rest page
# one cell's text shown in a result's page (the contract's untrusted_text length): a longer
# cell is shown by its first CELL_PAGE characters, says ``chars`` (its whole length), and the
# rest is read in pieces with {"page": id, "offset": row, "column": n, "from": char}. Nothing
# of a cell is dropped: the store holds it whole.
CELL_PAGE = 20_000
RESULTS_KEPT = 512
RESULTS_BYTES = 48_000_000   # rows held in MEMORY across a host's results (the rest of a big result is on disk)


class _Results:
    """The results answered lately: id -> the rows the host holds (every fetched row), what
    paging and the answer's references read. Bounded by count and by the rows' size
    (RESULTS_BYTES, the host process's memory): the oldest go first, and a page of one no
    longer held is refused with the reason (``SqlTool._page``)."""

    def __init__(self, most: int = RESULTS_KEPT, size: int = 0) -> None:
        self.most, self._lock, self._got = most, threading.Lock(), OrderedDict()
        self.size = size or RESULTS_BYTES
        self._sizes: dict[str, int] = {}

    def put(self, rid: str, entry: dict[str, Any]) -> None:
        rows = entry.get("rows")
        # a result's rows past ``HELD_ROWS`` are on disk (``result_rows``): only the part in
        # memory counts against the host's memory
        grew = rows.memory_bytes() if hasattr(rows, "memory_bytes") \
            else len(json.dumps(rows or [], default=str))
        dropped = []
        with self._lock:
            self._got[rid] = entry
            self._got.move_to_end(rid)
            self._sizes[rid] = grew
            while len(self._got) > 1 and (len(self._got) > self.most
                                          or sum(self._sizes.values()) > self.size):
                old, gone = self._got.popitem(last=False)
                self._sizes.pop(old, None)
                dropped.append(gone)
        for gone in dropped:   # a dropped result's file goes with it
            close = getattr(gone.get("rows"), "close", None)
            if close is not None:
                close()

    def get(self, rid: str) -> dict[str, Any] | None:
        with self._lock:
            return self._got.get(rid)


RESULTS = _Results()


def _actor_key(actor: Any) -> str:
    return json.dumps([getattr(actor, "tenant", None),
                       sorted(getattr(actor, "principals", ()) or ())], default=str)


# A cell the contract types bare (``cells`` items, ontology_tool_contracts.json): a
# number, an ISO date or time, or one whitespace-free code (currency, status, document
# number, handle). Anything else (names, descriptions, notes) leaves as an envelope.
CELL_PATTERN = (  # guard-ok: the contract's cell grammar (what stays bare at the firewall), not a meaning
    r"-?[0-9]{1,30}(?:\.[0-9]{1,30})?"
    r"|[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}"
    r"(?:\.[0-9]{1,9})?)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?)?"
    r"|[A-Za-z0-9][A-Za-z0-9_.:/@+=!,#%?&~-]{0,255}")
_CODE = re.compile(f"(?:{CELL_PATTERN})")


def _cell(value: Any) -> tuple[Any, bool]:
    """A result cell as the tools state it (number as Decimal text, date ISO, source text
    in an ``untrusted_text`` envelope; a code-like word bare) and whether it was cut."""
    if value is None or isinstance(value, bool):
        return value, False
    if isinstance(value, (int, Decimal)):
        return format(Decimal(value), "f"), False
    if isinstance(value, float):
        return format(Decimal(repr(value)), "f"), False
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat(), False
    if isinstance(value, (list, tuple)):
        got = [_cell(v) for v in value]
        if not any(c for _, c in got) and cell_chars(value) <= CELL_PAGE:
            return [g for g, _ in got], False
        # an array too long to show whole is shown as its JSON text, read on in pieces
        return _piece(cell_text(value), 0)
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if _CODE.fullmatch(text):
        return text, False
    return _piece(text, 0)


def cell_text(value: Any) -> str:
    """The text of a cell that is read in pieces (an array: its JSON; else the text)."""
    if isinstance(value, (list, tuple)):
        return json.dumps([cell_text(v) if isinstance(v, (list, tuple)) else
                           (v if isinstance(v, str) else _plain(v)) for v in value],
                          ensure_ascii=False, default=str)
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _plain(value: Any) -> Any:
    if isinstance(value, (Decimal, int)) and not isinstance(value, bool):
        return format(Decimal(value), "f")
    if isinstance(value, float):
        return format(Decimal(repr(value)), "f")
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def cell_chars(value: Any) -> int:
    return len(cell_text(value))


def _piece(text: str, start: int) -> tuple[Any, bool]:
    """The envelope of ``text`` from character ``start`` for CELL_PAGE characters; ``chars``
    (the whole length) and ``truncated`` when more follows or came before."""
    part = text[start:start + CELL_PAGE]
    env: dict[str, Any] = {"untrusted_text": part}
    cut = start > 0 or start + len(part) < len(text)
    if cut:
        env["truncated"] = True
        env["chars"] = len(text)
        if start:
            env["from"] = start
    return env, cut


def _shown(result: str, cols: Sequence[str], rows: Sequence[Sequence[Any]], start: int,
           profile: Optional[Mapping[str, Any]] = None
           ) -> tuple[list[dict[str, Any]], bool, int]:
    """The rows from ``start`` as the model is shown them (each cited by its row number in
    the whole result), until MAX_BYTES less what the profile takes (the client shows one
    result whole only up to ``CLIENT_SHOWN_CHARS``); whether the budget cut them, and how
    many cells were cut."""
    budget = max(MAX_BYTES // 5, MAX_BYTES - len(json.dumps(profile or {}, default=str)))
    shown: list[dict[str, Any]] = []
    size, cut_cells, truncated = 0, 0, False
    # the catalog's list column (``bv.SYSTEMS_COL``: "bc,portal", stored as one text so
    # a filter is a LIKE) is shown as the list it is: 'bc,portal' read as one value was
    # said "only in BC" (HARD20 h09, 11-23)
    lists = {i for i, c in enumerate(cols) if c == bv.SYSTEMS_COL}
    for n, raw in enumerate(rows[start:], start):
        # cells line up with ``columns``: under one contract-typed key (``cells``)
        # numbers, dates and codes stay bare at the output boundary; keyed by the
        # query's own column names every cell would leave as an envelope.
        cells: list[Any] = []
        for i, value in enumerate(raw):
            if i in lists and isinstance(value, str):
                value = [part.strip() for part in value.split(",") if part.strip()]
            cell, cut = _cell(value)
            cut_cells += cut
            cells.append(cell)
        entry = {"cite": f"{result}#row={n}", "cells": cells}
        grew = len(json.dumps(entry, default=str))
        if not shown and grew > budget:
            # a first row wider than the whole budget (several long texts) is shown
            # with its texts cut shorter, never dropped
            entry = _narrowed(entry, budget)
            grew, truncated = len(json.dumps(entry, default=str)), True
        size += grew
        if size > budget or len(shown) >= SHOWN_ROWS:
            truncated = True
            break
        shown.append(entry)
    return shown, truncated, cut_cells


def cut_row(entry: dict[str, Any], keep: int) -> dict[str, Any]:
    """``entry`` (one row's cite and cells) with every text cell, in arrays too, cut to
    ``keep`` characters (said ``truncated``; ``chars`` is its whole length, and the rest is
    read with {"page": id, "offset": row, "column": n, "from": char})."""
    def cut(value: Any) -> Any:
        if isinstance(value, list):
            return [cut(v) for v in value]
        if isinstance(value, dict) and isinstance(value.get("untrusted_text"), str) \
                and len(value["untrusted_text"]) > keep:
            return {**value, "untrusted_text": value["untrusted_text"][:keep],
                    "truncated": True, "chars": value.get("chars", len(value["untrusted_text"]))}
        return value
    return {**entry, "cells": [cut(c) for c in entry["cells"]]}


def _narrowed(entry: dict[str, Any], most: int) -> dict[str, Any]:
    """``entry`` with every text cell cut to one length (``cut_row``) so the row's JSON
    takes at most ``most`` bytes."""
    keep = CELL_PAGE
    while keep > 0:
        got = cut_row(entry, keep)
        if len(json.dumps(got, default=str)) <= most:
            return got
        keep //= 2
    return cut_row(entry, 0)


def _why(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _profiled(pg: Any, sql: str, cols: list[str], rows: list[tuple], stopped: bool,
              cats: list[str], found: list[Any], max_rows: int) -> dict[str, Any]:
    """The whole result's profile: from the rows when they are all held, else from one
    aggregate statement (in a savepoint: its failure leaves the read intact and is said
    in the profile, with the counts of the rows held)."""
    if not stopped and len(rows) <= max_rows:
        return result_profile.from_rows(cols, rows, cats, found)
    agg, group = result_profile.aggregate_sql(sql, cols, cats, found)
    try:
        with pg.transaction():
            got = pg.execute(agg).fetchall()
        return result_profile.from_aggregate(cols, cats, found, got, group)
    except Exception as exc:  # noqa: BLE001 - said in the profile, never a silent part
        logging.getLogger("operating.ontology_tools_sql").warning(
            "plx.sql profile of a cut result failed: %s", _why(exc))
        held = result_profile.from_rows(cols, rows[:max_rows], cats, found)
        return {**held, "rows": None, "exact": False, "rows_held": len(rows[:max_rows]),
                "note": ("the exact count of this result could not be taken ("
                         f"{_why(exc)}); the counts below cover only the "
                         f"first {len(rows[:max_rows])} rows: count with a query")}
