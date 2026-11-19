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


# A word the label's trigram index can find (``records.label`` gin_trgm_ops): 3 letters
# or more; a shorter one is only checked on the rows the others found.
INDEXED_LETTERS = 3
# The labels read for candidates (``candidates_sql``): at least this many, and PER_ROW per
# row a page needs, so a reader shown a quarter of the matches still fills it from them.
CANDIDATES = 1000
PER_ROW = 4


def indexed(words: Sequence[str]) -> list[int]:
    """The positions of ``words`` the label index finds (``INDEXED_LETTERS``)."""
    return [i for i, w in enumerate(words) if len(w) >= INDEXED_LETTERS]


def order_sql() -> str:
    """The one order of every hit: the label that is exactly the query first, then the
    newest record date, then the handle (the candidates' and the hits' own)."""
    return ("lower(label) = lower(%(q)s) DESC, record_date DESC NULLS LAST, handle")


def candidates(off: int, k: int) -> int:
    return max(CANDIDATES, PER_ROW * (off + k))


def candidates_sql(positions: Sequence[int], *, biz: str, since: bool, until: bool) -> str:
    """The handles of the records whose label holds the words at ``positions`` (each found
    by the label index), in the hits' order, at most ``%(cap)s``: read from the store's own
    table, never shown (``SqlTool.records_search`` reads the hits through the reader's
    guarded view by these handles). Why: the guarded view is a security barrier and ILIKE
    is not leakproof, so a label match through it was never pushed to the index: every
    search planned a scan of all records and was refused as too broad (live: "Azelis",
    "Univar" found nothing)."""
    if not positions:
        raise SearchInputError("no word the label index finds")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", biz):
        raise SearchInputError("not a schema name")
    where = [f"label ILIKE %(w{i})s ESCAPE '\\'" for i in positions]
    if since:
        where.append("record_date >= %(since)s")
    if until:
        where.append("record_date <= %(until)s")
    return (f"SELECT handle FROM {biz}.records WHERE " + " AND ".join(where)
            + " AND handle IS NOT NULL ORDER BY " + order_sql() + " LIMIT %(cap)s")


def search_sql(n_words: int, *, since: bool, until: bool, among: bool = False) -> str:
    """The statement (``records`` resolves to the reader's guarded view through the
    session's search path); parameters ``w0..``, ``q``, ``since``, ``until``, ``k``,
    ``off`` and, ``among``: ``hs``, the candidates' handles (``candidates_sql``; equality
    on the handle is leakproof, so the view reads them by its handle index)."""
    if n_words < 1:
        raise SearchInputError("the query has no word to search for")
    where = [f"label ILIKE %(w{i})s ESCAPE '\\'" for i in range(n_words)]
    if among:
        where.insert(0, "handle = ANY(%(hs)s::text[])")
    if since:
        where.append("record_date >= %(since)s")
    if until:
        where.append("record_date <= %(until)s")
    return (f"SELECT {', '.join(COLS)} FROM records WHERE " + " AND ".join(where)
            + " ORDER BY " + order_sql() + " LIMIT %(k)s OFFSET %(off)s")


# A record whose label is spelled close to the whole query (``close_candidates_sql``):
# pg_trgm similarity at least this ("Federated Coop Alberni" finds "Federated
# Co-operatives Ltd. Alberni", 0.54); at most CLOSE_CAP candidates read.
CLOSE_SIMILARITY = 0.4
CLOSE_CAP = 200
CLOSE_MIN_LETTERS = 4


def close_candidates_sql(*, biz: str, trgm: str, since: bool, until: bool) -> str:
    """The handles of the records whose label is spelled close to the whole query ``q``
    (pg_trgm ``%``, its threshold set by the caller's transaction to ``CLOSE_SIMILARITY``;
    the label's trigram index), closest first, at most ``%(cap)s``: read from the store's
    own table like ``candidates_sql``, never shown (the hits are read through the reader's
    guarded view by these handles, ``among_sql``). ``trgm``: the schema pg_trgm is
    installed in."""
    if not (re.fullmatch(r"[a-z_][a-z0-9_]*", biz) and re.fullmatch(r"[a-z_][a-z0-9_]*", trgm)):
        raise SearchInputError("not a schema name")
    where = [f"label OPERATOR({trgm}.%%) %(q)s", "handle IS NOT NULL"]
    if since:
        where.append("record_date >= %(since)s")
    if until:
        where.append("record_date <= %(until)s")
    return (f"SELECT handle FROM {biz}.records WHERE " + " AND ".join(where)
            + f" ORDER BY {trgm}.similarity(label, %(q)s) DESC, handle LIMIT %(cap)s")


def among_sql(*, since: bool, until: bool) -> str:
    """The reader's guarded ``records`` rows among the handles ``hs``, in their order
    (``close_candidates_sql``), at most ``%(k)s``."""
    where = ["handle = ANY(%(hs)s::text[])"]
    if since:
        where.append("record_date >= %(since)s")
    if until:
        where.append("record_date <= %(until)s")
    return (f"SELECT {', '.join(COLS)} FROM records WHERE " + " AND ".join(where)
            + " ORDER BY array_position(%(hs)s::text[], handle), handle LIMIT %(k)s")


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


__all__ = ["CLOSE_CAP", "CLOSE_SIMILARITY", "KIND", "SOURCE", "SearchInputError", "among_sql",
           "candidates", "candidates_sql", "close_candidates_sql", "day", "hits", "indexed", "params", "read_cursor", "search_sql", "searched", "terms"]
