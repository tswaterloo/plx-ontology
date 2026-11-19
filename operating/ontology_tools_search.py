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
# the tables path gives way to the old one: not served here, or the business tables are
# not built / mid-swap / behind the people exclusion (the old path keeps its own gates)
FALLBACK = frozenset({"not_served", "not_ready"})
# plx.open also reads the old way when the store refused the comms statement itself (a
# missing grant: never an out-of-scope row, which the comms path answers as no comms row)
OPEN_FALLBACK = FALLBACK | {"not_readable"}
ALL_SOURCES = ("graph_mail", "sharepoint", "original", "teams")
OPEN_MAX_CHARS = 20000
CHECK_MAX_CHARS = 200_000
CHECK_RESERVE_S = 2.0      # time kept back from the call's limit when checking full texts
DEFAULT_LIMIT = 20

_CONTRACT = json.loads((Path(__file__).with_name("ontology_tool_contracts.json"))
                       .read_text(encoding="utf-8"))["tools"][TOOL_NAME]
DESCRIPTION: str = _CONTRACT["description"]
INPUT_SCHEMA: dict[str, Any] = _CONTRACT["input"]
_OK = _CONTRACT["output"]["oneOf"][0]
_KEPT = frozenset(_OK["properties"]) - {"documents", "egress_withheld"}
_HIT = frozenset(_OK["properties"]["documents"]["items"]["properties"])
TOOL: dict[str, Any] = {"name": TOOL_NAME, "description": DESCRIPTION,
                        "inputSchema": INPUT_SCHEMA, "annotations": {"readOnlyHint": True}}
_OPEN = json.loads((Path(__file__).with_name("ontology_tool_contracts.json"))
                   .read_text(encoding="utf-8"))["tools"][OPEN_NAME]
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
        call_comms: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
    """One ``plx.search``: mail and Teams through ``call_comms`` (the host's internal
    ``plx.sql_comms_search`` for the session's reader: Freightcom is decided inside the
    guarded views on the full text, ``ai_ok``), files and originals through
    ``call_documents`` (its ``plx.documents``; with ``call_read`` each hit's full text is
    checked for Freightcom before the hit is shown). A participant searches mail only; a
    cursor continues the list it came from. Without ``call_comms``, or where the host does
    not serve the mail tables (``not_served``), everything goes to ``plx.documents``."""
    try:
        wanted = arguments_for_documents(arguments)
    except ValueError as exc:
        return _error("invalid_input", str(exc))
    if call_comms is None:
        return _documents(call_documents, wanted, call_read)
    sources = list(wanted.get("sources") or ALL_SOURCES)
    cursor = wanted.get("cursor")
    comms = [x for x in sources if x in COMMS_SOURCES]
    docs = [] if wanted.get("participant") else [x for x in sources if x not in COMMS_SOURCES]
    if isinstance(cursor, str):
        comms, docs = (comms, []) if cursor.startswith("c1:") else ([], docs)
    got = []
    if comms:
        one = call_comms({**{k: v for k, v in wanted.items() if k != "sources"},
                          "sources": comms})
        if isinstance(one, Mapping) and (one.get("error") or {}).get("code") in FALLBACK:
            return _documents(call_documents, wanted, call_read)
        one = compact(one)
        if "error" in one:
            return one
        got.append(one)
    if docs:
        one = _documents(call_documents, {**wanted, "sources": docs}, call_read)
        if "error" in one:
            return one
        got.append(one)
    return _merged(got)


def _documents(call_documents: Callable[[dict[str, Any]], Any], wanted: dict[str, Any],
               call_read: Callable[[dict[str, Any]], Any] | None) -> dict[str, Any]:
    out = compact(call_documents(wanted))
    if call_read is None or "error" in out:
        return out
    return _full_text_checked(out, call_read)


def _full_text_checked(out: dict[str, Any], call_read: Callable[[dict[str, Any]], Any]
                       ) -> dict[str, Any]:
    from operating import call_limits
    kept, named, unchecked = [], 0, 0
    for hit in out["documents"]:
        left = call_limits.seconds_left()
        citation = hit.get("citation") if isinstance(hit, Mapping) else None
        if not isinstance(citation, str) or (left is not None and left < CHECK_RESERVE_S):
            unchecked += 1
            continue
        try:
            got = call_read({"citation": citation, "max_chars": CHECK_MAX_CHARS})
        except Exception:  # noqa: BLE001 - an unread text is never shown
            got = None
        if not isinstance(got, Mapping) or "error" in got:
            unchecked += 1
        elif _names_freightcom(got):
            named += 1
        else:
            kept.append(hit)
    out = {**out, "documents": kept}
    if named:
        out["egress_withheld"] = int(out.get("egress_withheld") or 0) + named
    if unchecked:
        out["unchecked"] = unchecked
    return out


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
