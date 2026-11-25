"""Every row of a statement for a document's table (``plx.make_document`` ``tables``), read
again as the person through their own door (``insights.read_sql``: ``plx.sql`` as them, its
views' row and cell scoping, its Freightcom and people rules), never rows the model wrote.

``plx.sql`` shows a page of rows at a time (200 and its byte budget). A statement whose rows fit is taken as it answered. A longer one is counted
(``SELECT count(*)`` over it) and read in pages of one total order: the statement's own
top-level ``ORDER BY`` when each key is a column it returns (a name or a position), then
the whole row's text, so pages never overlap or skip (identical rows are interchangeable);
a key that is no returned column (an expression) orders by the row's text alone, said in
``ordered``. A cell the person may not read is blank as plx.sql answers it (NULL) and the
table says ``partial``. The rows read must be exactly the count (else the rows changed while read:
refused, never a short table). No row count or time bounds it (owner 11-23); nothing partial is
ever made into a file."""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Mapping, Optional, Sequence



class TableRefused(ValueError):
    pass


def _cut(rows: Sequence[Mapping[str, Any]]) -> int:
    """How many cells of a plx.sql page it cut (``MAX_CELL``: an envelope saying truncated)."""
    return sum(1 for r in rows if isinstance(r, Mapping) for c in r.get("cells") or ()
               if isinstance(c, Mapping) and c.get("truncated") is True)


def _cell(value: Any) -> Any:
    if isinstance(value, Mapping):
        value = value.get("untrusted_text", value.get("value", ""))
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return ", ".join(str(_cell(v)) for v in value)
    return str(value)


def _rows(got: Mapping[str, Any]) -> list[list[Any]]:
    return [[_cell(c) for c in (r.get("cells") or [])] for r in got.get("rows") or ()
            if isinstance(r, Mapping)]


def _top_level(sql: str) -> list[tuple[int, str]]:
    """(position, word) of each bare word of ``sql`` outside parentheses, strings and
    quoted names, lower-cased."""
    out, depth, i, n = [], 0, 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"":
            end = i + 1
            while end < n:
                if sql[end] == ch:
                    if end + 1 < n and sql[end + 1] == ch:
                        end += 2
                        continue
                    break
                end += 1
            if depth == 0 and ch == '"':
                out.append((i, sql[i:end + 1]))
            i = end + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and (ch.isalnum() or ch in "_."):
            m = re.match(r"[\w.]+", sql[i:])
            out.append((i, m.group(0).lower()))
            i += len(m.group(0))
            continue
        elif depth == 0 and ch == ",":
            out.append((i, ","))
        i += 1
    return out


def order_keys(sql: str, columns: Sequence[str]) -> Optional[list[str]]:
    """The outer ORDER BY keys (positions with their direction) that keep ``sql``'s own
    top-level order; [] when it has none; None when a key is not a returned column."""
    words = _top_level(sql)
    starts = [k for k in range(len(words) - 1)
              if words[k][1] == "order" and words[k + 1][1] == "by"]
    if not starts:
        return []
    k = starts[-1] + 2
    items: list[list[str]] = [[]]
    while k < len(words) and words[k][1] not in ("limit", "offset", "fetch", "for"):
        if words[k][1] == ",":
            items.append([])
        else:
            items[-1].append(words[k][1])
        k += 1
    # an expression with operators or calls leaves no bare words we can map: the item's
    # words must be exactly <key> [asc|desc] [nulls first|last]
    text_items = []
    names = [c.lower() for c in columns]
    for item in items:
        if not item:
            return None
        key, rest = item[0], item[1:]
        if rest and rest[0] in ("asc", "desc"):
            direction, rest = rest[0], rest[1:]
        else:
            direction = "asc"
        nulls = ""
        if rest[:2] in (["nulls", "first"], ["nulls", "last"]):
            nulls, rest = " " + " ".join(rest[:2]), rest[2:]
        if rest:
            return None
        if key.isdigit():
            pos = int(key)
        else:
            bare = key.strip('"') if key.startswith('"') else key.rsplit(".", 1)[-1]
            if names.count(bare.lower()) != 1:
                return None
            pos = names.index(bare.lower()) + 1
        if not 1 <= pos <= len(columns):
            return None
        text_items.append(f"{pos} {direction}{nulls}")
    return text_items


def _span(sql: str) -> str:
    return re.sub(r";\s*$", "", sql.strip())


def read_all(read: Callable[[str], Mapping[str, Any]], sql: str
             ) -> dict[str, Any]:
    """{"columns", "rows", "ordered", "pages"} of every row of ``sql``, ``read`` being one
    ``plx.sql`` read as the person (``insights.read_sql`` bound to them)."""
    sql = _span(sql)
    first = read(sql)
    columns = [str(c) for c in first.get("columns") or ()]
    partial = bool(first.get("partial"))
    if not first.get("truncated"):
        return {"columns": columns, "rows": _rows(first), "ordered": "as written",
                "pages": 1, "partial": partial, "cells_cut": _cut(first.get("rows") or ()),
                "as_of": first.get("as_of")}
    counted = read(f"SELECT count(*) AS plx_rows FROM (\n{sql}\n) AS plx_t")
    try:
        total = int(_rows(counted)[0][0])
    except (IndexError, TypeError, ValueError):
        raise TableRefused("Its rows could not be counted.") from None
    keys = order_keys(sql, columns)
    ordered = "as written" if keys else ("by the rows' text (its order is an expression)"
                                         if keys is None else "by the rows' text")
    order = ", ".join((keys or []) + ["plx_t::text"])
    rows: list[list[Any]] = []
    pages, cut = 0, 0
    while len(rows) < total:
        got = read(f"SELECT * FROM (\n{sql}\n) AS plx_t ORDER BY {order} "
                   f"LIMIT 200 OFFSET {len(rows)}")
        page = _rows(got)
        partial = partial or bool(got.get("partial"))
        cut += _cut(got.get("rows") or ())
        pages += 1
        if not page:
            if got.get("truncated"):
                raise TableRefused(f"Row {len(rows) + 1:,} is larger than one read holds: "
                                   "pick fewer or shorter columns.")
            break
        rows += page
    if len(rows) != total:
        raise TableRefused("Its rows changed while they were read: make the file again.")
    return {"columns": columns, "rows": rows, "ordered": ordered, "pages": pages,
            "partial": partial, "cells_cut": cut, "as_of": first.get("as_of")}
