"""``plx.search``'s records leg: the structured records of every system (``records``, the
generic door table ``generic_records`` builds) found by their label words, so a question
naming a customer, an item, an order or a task number finds it like a mail or a file.

One fixed statement over the reader's guarded ``records`` view (DOOR v2: the reader's held
access classes, ``ai_ok`` in the AI's schema, no excluded person's record), run through
``plx.sql``'s own door (``SqlTool.records_search``): the permission predicate is the view's,
inside the statement. Every word must be in the label (``ILIKE``, the trigram index on
``records.label``); the label that is exactly the query first, then the newest record
date. ``since``/``until`` bound the record date.

A hit: citation = the record's handle (``plx.open`` reads it through ``plx.read``), kind
``record``, title = its label, date = its record date, snippet = its type, system and id;
every text field an untrusted-text envelope citing the record."""
from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

SOURCE = "records"
KIND = "record"
MAX_TERMS = 8
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_CURSOR = re.compile(r"^r1:([0-9]{1,4})$")
MAX_OFFSET = 1000
COLS = ("handle", "type", "system", "record_id", "label", "record_date")


class SearchInputError(ValueError):
    pass


def terms(query: Any) -> list[str]:
    """The query's words to match in the label (module doc), or SearchInputError."""
    got = _WORD.findall(query if isinstance(query, str) else "")
    if not got:
        raise SearchInputError("the query has no word to search for")
    return got[:MAX_TERMS]


def _like(word: str) -> str:
    return "%" + re.sub(r"([\\%_])", r"\\\1", word) + "%"


def read_cursor(text: Any) -> int:
    m = _CURSOR.match(text) if isinstance(text, str) else None
    if not m or int(m.group(1)) > MAX_OFFSET:
        raise SearchInputError("not a cursor of this search")
    return int(m.group(1))


def day(value: Any) -> str | None:
    """A ``since``/``until`` as the record date's own YYYY-MM-DD text."""
    if value is None:
        return None
    if not isinstance(value, str) or not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise SearchInputError("since/until must be a date (YYYY-MM-DD)")
    return value[:10]


def search_sql(n_words: int, *, since: bool, until: bool) -> str:
    """The statement (``records`` resolves to the reader's guarded view through the
    session's search path); parameters ``w0..``, ``q``, ``since``, ``until``, ``k``,
    ``off``."""
    if n_words < 1:
        raise SearchInputError("the query has no word to search for")
    where = [f"label ILIKE %(w{i})s ESCAPE '\\'" for i in range(n_words)]
    if since:
        where.append("record_date >= %(since)s")
    if until:
        where.append("record_date <= %(until)s")
    return (f"SELECT {', '.join(COLS)} FROM records WHERE " + " AND ".join(where)
            + " ORDER BY lower(label) = lower(%(q)s) DESC, record_date DESC NULLS LAST,"
            " handle LIMIT %(k)s OFFSET %(off)s")


def params(words: Sequence[str], query: str, *, since: str | None, until: str | None,
           k: int, off: int) -> dict[str, Any]:
    out: dict[str, Any] = {f"w{i}": _like(w) for i, w in enumerate(words)}
    out.update(q=query.strip(), since=since, until=until, k=k, off=off)
    return out


def _text(value: Any, citation: str, cut: int = 500) -> dict[str, Any]:
    text = str(value)
    out: dict[str, Any] = {"untrusted_text": text[:cut], "origin": {"citation": citation}}
    if len(text) > cut:
        out["truncated"] = True
    return out


def hits(rows: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    """The statement's rows as ``plx.search`` hits (module doc)."""
    out = []
    for handle, rtype, system, record_id, label, record_date in rows:
        date = record_date if isinstance(record_date, str) and re.match(
            r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", record_date) else None
        out.append({"citation": handle, "kind": KIND,
                    "title": _text(label, handle) if label else None,
                    "date": date,
                    "snippet": _text(f"{rtype} in {system}, id {record_id}", handle)})
    return out


def searched(args: Mapping[str, Any], words: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"sources": [SOURCE], "queries": [" ".join(words)[:200]]}
    for key in ("since", "until"):
        if args.get(key):
            out[key] = args[key]
    return out


__all__ = ["KIND", "SOURCE", "SearchInputError", "day", "hits", "params", "read_cursor",
           "search_sql", "searched", "terms"]
