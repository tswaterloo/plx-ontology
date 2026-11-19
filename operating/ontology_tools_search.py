"""``plx.search``: the simple Ask's one text search (mail, Teams, SharePoint files and
captured originals the reader may read), beside ``plx.sql`` for the business tables.

Mail and Teams: one ranked statement over the communications tables through the reader's
guarded views (``operating.pg.comms_search``, called as the host's internal
``plx.sql_comms_search`` for the session's reader: the same door ``plx.sql`` reads, the
permission predicate inside the statement, never an item gate inside a row-limited walk).
SharePoint files and captured originals (not yet served as a table) and any host that
does not serve the mail tables: ``plx.documents`` through the host's own path, its hits cut
to what a search result needs (citation, kind, title, sender, date, snippet, direction).
``plx.open`` reads a mail or Teams item through the same views by its handle
(``plx.sql_comms_open``); any other citation through ``plx.read``.

Mail and Teams:
the guarded views hold back every item whose full text names it (``ai_ok``, decided at
build; a body not yet indexed is held back too), and a shown field naming it is left out
here as well. Files and originals: a hit any text of which names Freightcom
(``image_ocr_gpu.FREIGHTCOM``) is left out and only counted (``egress_withheld``): every field ``plx.documents`` returned for it (mentions and meta
too, not only the shown ones) and, when the host gives a reader, its full text read by
``plx.read`` as the same reader (up to ``CHECK_MAX_CHARS``). A hit whose text could not
be read in time is left out too (``unchecked``): fail closed.
An unknown argument, or a result that is neither a hit list nor an error, fails closed."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

TOOL_NAME = "plx.search"
OPEN_NAME = "plx.open"
TOOL_NAMES = (TOOL_NAME, OPEN_NAME)
DOCUMENTS = "plx.documents"
READ = "plx.read"
COMMS_SEARCH = "plx.sql_comms_search"   # ontology_tools_sql.COMMS_SEARCH
COMMS_OPEN = "plx.sql_comms_open"
COMMS_SOURCES = ("graph_mail", "teams")
RECORDS_SEARCH = "plx.sql_records_search"   # ontology_tools_sql.RECORDS_SEARCH
RECORDS = "records"     # the structured records of every system (``records_search``)
# the one records leg error that is the search's own (the query itself is wrong); any
# other (not served, busy, a failed or refused statement, timeout) leaves the other legs'
# hits standing and is said as a gap
RECORDS_OWN_ERRORS = frozenset({"invalid_input"})
# the tables path gives way to the old one: not served here, or the business tables are
# not built / mid-swap / behind the people exclusion (the old path keeps its own gates)
FALLBACK = frozenset({"not_served", "not_ready"})
# plx.open also reads the old way when the store refused the comms statement itself (a
# missing grant: never an out-of-scope row, which the comms path answers as no comms row)
OPEN_FALLBACK = FALLBACK | {"not_readable"}
ALL_SOURCES = ("graph_mail", "sharepoint", "original", "teams", RECORDS)
OPEN_MAX_CHARS = 20000
CHECK_MAX_CHARS = 200_000  # a hit's full text checked for Freightcom up to this many chars
CHECK_RESERVE_S = 2.0      # time kept back from the call's limit when checking full texts
DEFAULT_LIMIT = 20

_TOOLS = json.loads((Path(__file__).with_name("ontology_tool_contracts.json"))
                    .read_text(encoding="utf-8"))["tools"]
_CONTRACT = _TOOLS[TOOL_NAME]
# One plx.read window at most (its contract's own bound): the full-text check pages by
# text_offset up to CHECK_MAX_CHARS. Asked for more in one read, plx.read refuses the call
# (invalid_input), and every hit was left out unchecked (live 11-18: no document at all).
CHECK_PAGE_CHARS: int = _TOOLS[READ]["input"]["properties"]["max_chars"]["maximum"]
DESCRIPTION: str = _CONTRACT["description"]
INPUT_SCHEMA: dict[str, Any] = _CONTRACT["input"]
_OK = _CONTRACT["output"]["oneOf"][0]
_KEPT = frozenset(_OK["properties"]) - {"documents", "egress_withheld"}
_HIT = frozenset(_OK["properties"]["documents"]["items"]["properties"])
TOOL: dict[str, Any] = {"name": TOOL_NAME, "description": DESCRIPTION,
                        "inputSchema": INPUT_SCHEMA, "annotations": {"readOnlyHint": True}}
_OPEN = _TOOLS[OPEN_NAME]
OPEN_TOOL: dict[str, Any] = {"name": OPEN_NAME, "description": _OPEN["description"],
                             "inputSchema": _OPEN["input"],
                             "annotations": {"readOnlyHint": True}}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _names_freightcom(value: Any) -> bool:
    from integrations.image_ocr_gpu import FREIGHTCOM
    if isinstance(value, str):
        return FREIGHTCOM.search(value) is not None
    if isinstance(value, Mapping):
        return any(_names_freightcom(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_names_freightcom(v) for v in value)
    return False


def arguments_for_documents(arguments: Any) -> dict[str, Any]:
    """``plx.search``'s arguments as the ``plx.documents`` call they are, or ValueError."""
    if not isinstance(arguments, Mapping):
        raise ValueError("arguments must be an object")
    unknown = sorted(set(arguments) - set(INPUT_SCHEMA["properties"]))
    if unknown:
        raise ValueError("unknown arguments: " + ", ".join(unknown)[:200])
    if not (arguments.get("query") or arguments.get("participant")):
        raise ValueError("send query (words) or participant (an address or @domain)")
    out = dict(arguments)
    out.setdefault("limit", DEFAULT_LIMIT)
    return out


def compact(result: Any) -> dict[str, Any]:
    """``plx.documents``' result as ``plx.search`` answers it (module doc)."""
    if not isinstance(result, Mapping):
        return _error("search_failed", "the search gave no result")
    if "error" in result:
        return {"error": result["error"]}
    documents = result.get("documents")
    if not isinstance(documents, list):
        return _error("search_failed", "the search gave no hit list")
    hits, left_out = [], 0
    for document in documents:
        if not isinstance(document, Mapping):
            continue
        hit = {k: v for k, v in document.items() if k in _HIT}
        if _names_freightcom(document):
            left_out += 1
            continue
        hits.append(hit)
    out = {k: v for k, v in result.items() if k in _KEPT}
    out["documents"] = hits
    if left_out:
        out["egress_withheld"] = left_out
    return out


def run(call_documents: Callable[[dict[str, Any]], Any], arguments: Any,
        call_read: Callable[[dict[str, Any]], Any] | None = None,
        call_comms: Callable[[dict[str, Any]], Any] | None = None,
        call_records: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
    """One ``plx.search``: mail and Teams through ``call_comms`` (the host's internal
    ``plx.sql_comms_search`` for the session's reader: Freightcom is decided inside the
    guarded views on the full text, ``ai_ok``), files and originals through
    ``call_documents`` (its ``plx.documents``; with ``call_read`` each hit's full text is
    checked for Freightcom before the hit is shown). A participant searches mail only; a
    cursor continues the list it came from. Without ``call_comms``, or where the host does
    not serve the mail tables (``not_served``), everything goes to ``plx.documents``.
    Records (``call_records``, the host's internal ``plx.sql_records_search``: the
    structured records of every system through ``plx.sql``'s door, found by their label
    words): searched with query words, never for a participant; where they are not served
    or the search would read too much, the other hits stand and a gap says so."""
    try:
        wanted = arguments_for_documents(arguments)
    except ValueError as exc:
        return _error("invalid_input", str(exc))
    sources = list(wanted.get("sources") or ALL_SOURCES)
    cursor = wanted.get("cursor")
    records = (call_records is not None and RECORDS in sources and bool(wanted.get("query"))
               and not wanted.get("participant"))
    sources = [x for x in sources if x != RECORDS]
    comms = [x for x in sources if x in COMMS_SOURCES] if call_comms is not None else []
    docs = [] if wanted.get("participant") and comms else [
        x for x in sources if x not in comms]
    if isinstance(cursor, str):
        comms, docs, records = ((comms, [], False) if cursor.startswith("c1:") else
                                ([], [], records) if cursor.startswith("r1:") else
                                ([], docs, False))
    if not (comms or docs or records):
        if wanted.get("sources") and not sources:   # only records asked, none searchable
            return _error("invalid_input", "records are searched by query words only")
        docs = sources
    got = []
    if comms:
        one = call_comms({**{k: v for k, v in wanted.items() if k != "sources"},
                          "sources": comms})
        if isinstance(one, Mapping) and (one.get("error") or {}).get("code") in FALLBACK:
            comms, docs = [], [x for x in sources]
        else:
            one = compact(one)
            if "error" in one:
                return one
            got.append(one)
    if docs:
        # every non-record source and none named: the documents search's own default
        whole = not wanted.get("sources") and set(docs) == set(ALL_SOURCES) - {RECORDS}
        one = _documents(call_documents, {k: v for k, v in wanted.items() if k != "sources"}
                         if whole else {**wanted, "sources": docs}, call_read)
        if "error" in one:
            return one
        got.append(one)
    if records:
        one = _records(call_records, wanted)
        if "error" in one:
            return one
        got.append(one)
    return _merged(got)


def _records(call_records: Callable[[dict[str, Any]], Any], wanted: dict[str, Any]
             ) -> dict[str, Any]:
    """The records leg (``run``): its hits, or, where it could not run, none and a gap
    saying why; only ``RECORDS_OWN_ERRORS`` are the search's. An answer that is no mapping
    or a raised error is a gap too: the records leg never takes the other legs' hits."""
    try:
        one = call_records({k: v for k, v in wanted.items()
                            if k in ("query", "since", "until", "limit", "cursor")})
    except Exception as exc:  # noqa: BLE001 - one leg failing: a gap, never the search
        one = {"error": {"code": "query_failed", "message": type(exc).__name__}}
    if not isinstance(one, Mapping):
        one = {"error": {"code": "query_failed", "message": "no answer"}}
    error = one.get("error")
    code = ((error.get("code") if isinstance(error, Mapping) else "query_failed")
            if error is not None else None)
    if code is not None and code not in RECORDS_OWN_ERRORS:
        return {"documents": [], "searched": {"sources": [RECORDS]},
                "gaps": [{"state": "inaccessible" if code != "not_served" else
                          "not_ingested", "reason": code, "subject": "records",
                          "detail": "the structured records were not searched: "
                          + str((error if isinstance(error, Mapping) else {}).get(
                              "message") or code)[:300]}],
                "completeness": {"complete": False, "truncated": False, "returned": 0,
                                 "reason": code}}
    return compact(one)


def _documents(call_documents: Callable[[dict[str, Any]], Any], wanted: dict[str, Any],
               call_read: Callable[[dict[str, Any]], Any] | None) -> dict[str, Any]:
    out = compact(call_documents(wanted))
    if call_read is None or "error" in out:
        return out
    return _full_text_checked(out, call_read)


def _full_text_checked(out: dict[str, Any], call_read: Callable[[dict[str, Any]], Any]
                       ) -> dict[str, Any]:
    kept, named, unchecked = [], 0, 0
    for hit in out["documents"]:
        citation = hit.get("citation") if isinstance(hit, Mapping) else None
        verdict = _text_names_freightcom(call_read, citation) \
            if isinstance(citation, str) else None
        if verdict is None:
            unchecked += 1
        elif verdict:
            named += 1
        else:
            kept.append(hit)
    out = {**out, "documents": kept}
    if named:
        out["egress_withheld"] = int(out.get("egress_withheld") or 0) + named
    if unchecked:
        out["unchecked"] = unchecked
    return out


def _text_names_freightcom(call_read: Callable[[dict[str, Any]], Any], citation: str
                           ) -> bool | None:
    """Whether ``citation``'s text, read by ``call_read`` (plx.read as the reader) in windows
    of ``CHECK_PAGE_CHARS`` from its start up to ``CHECK_MAX_CHARS``, names Freightcom: any
    field of any window's answer, or the windows' text joined (a name split across two
    windows is found). None: not known (a read refused, failed or answered no window; the
    call's time nearly spent): the hit is never shown."""
    from operating import call_limits
    pieces: list[str] = []
    offset = 0
    while True:
        left = call_limits.seconds_left()
        if left is not None and left < CHECK_RESERVE_S:
            return None
        wanted: dict[str, Any] = {"citation": citation, "max_chars": CHECK_PAGE_CHARS}
        if offset:
            wanted["text_offset"] = offset
        try:
            got = call_read(wanted)
        except Exception:  # noqa: BLE001 - an unread text is never shown
            return None
        if not isinstance(got, Mapping) or "error" in got:
            return None
        if _names_freightcom(got):
            return True
        window = got.get("text_window")
        content = got.get("content")
        text = content.get("untrusted_text") if isinstance(content, Mapping) else None
        if isinstance(text, str):
            pieces.append(text)
        following = window.get("next_offset") if isinstance(window, Mapping) else None
        if offset and not isinstance(window, Mapping):
            return None   # a window asked for and none answered: not known
        if (not isinstance(following, int) or isinstance(following, bool)
                or following <= offset or following >= CHECK_MAX_CHARS):
            break
        offset = following
    return len(pieces) > 1 and _names_freightcom("".join(pieces))


def _merged(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Several lists as one, newest first; truncated when any is (the first truncated
    list's cursor continues it)."""
    if len(results) == 1:
        return results[0]
    documents = sorted((d for r in results for d in r["documents"]),
                       key=lambda d: d.get("date") or "", reverse=True)
    sources: list[str] = []
    for r in results:
        sources += [x for x in (r.get("searched") or {}).get("sources") or ()
                    if x not in sources]
    searched = dict(results[0].get("searched") or {})
    searched["sources"] = sources or ["graph_mail"]
    cut = [r["completeness"] for r in results
           if (r.get("completeness") or {}).get("truncated")]
    out: dict[str, Any] = {
        "documents": documents, "searched": searched,
        "gaps": [g for r in results for g in r.get("gaps") or ()][:20],
        "completeness": {"complete": not cut and all(
            (r.get("completeness") or {}).get("complete", True) for r in results),
            "truncated": bool(cut), "returned": len(documents),
            "next_cursor": cut[0].get("next_cursor") if cut else None}}
    if not out["completeness"]["complete"]:
        out["completeness"]["reason"] = (cut[0].get("reason") if cut else None) or next(
            ((r.get("completeness") or {}).get("reason") for r in results
             if (r.get("completeness") or {}).get("reason")), "page_cap")
    for key in ("egress_withheld", "unchecked"):
        left = sum(r.get(key, 0) for r in results)
        if left:
            out[key] = left
    return out


def open_one(call_read: Callable[[dict[str, Any]], Any], arguments: Any,
             call_comms: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
    """One ``plx.open``: a mail or Teams item through ``call_comms`` (the host's internal
    ``plx.sql_comms_open``: the same guarded views as the search); any other citation, or
    a host not serving the mail tables, through ``call_read`` (the host's own ``plx.read``
    for the session's reader: its gates decide). A result naming Freightcom anywhere is
    never shown (``egress_withheld``). Unknown arguments fail closed."""
    if not isinstance(arguments, Mapping):
        return _error("invalid_input", "arguments must be an object")
    unknown = sorted(set(arguments) - set(_OPEN["input"]["properties"]))
    if unknown:
        return _error("invalid_input", "unknown arguments: " + ", ".join(unknown)[:200])
    citation = arguments.get("citation")
    if not isinstance(citation, str) or not citation.strip():
        return _error("invalid_input", "send citation: a plx.search hit's citation")
    wanted: dict[str, Any] = {"citation": citation, "max_chars": OPEN_MAX_CHARS}
    offset = arguments.get("text_offset")
    if offset is not None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return _error("invalid_input", "text_offset must be a whole number >= 0")
        wanted["text_offset"] = offset
    got = call_comms(dict(wanted)) if call_comms is not None else None
    if not isinstance(got, Mapping) or got.get("not_comms") or (
            got.get("error") or {}).get("code") in OPEN_FALLBACK:
        got = call_read(wanted)
    if not isinstance(got, Mapping):
        return _error("open_failed", "the read gave no result")
    if _names_freightcom(got):
        return {"error": {"code": "egress_withheld",
                          "message": "this item is never shown to the assistant"}}
    return dict(got)


__all__ = ["OPEN_NAME", "OPEN_TOOL", "open_one", "TOOL", "TOOL_NAME", "TOOL_NAMES", "arguments_for_documents", "compact", "run"]
