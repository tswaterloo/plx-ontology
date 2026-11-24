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

Freightcom data is read like any other (the owner dropped that rule 11-23).
An unknown argument, or a result that is neither a hit list nor an error, fails closed."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Sequence

from operating import read_ceiling

TOOL_NAME = "plx.search"
OPEN_NAME = "plx.open"
TOOL_NAMES = (TOOL_NAME, OPEN_NAME)
DOCUMENTS = "plx.documents"
READ = "plx.read"
COMMS_SEARCH = "plx.sql_comms_search"   # ontology_tools_sql.COMMS_SEARCH
COMMS_OPEN = "plx.sql_comms_open"
# the sources the comms leg may search; what it did search is its ``searched.sources``
# (SharePoint files only for a full reader: anyone else's go to plx.documents)
COMMS_SOURCES = ("graph_mail", "teams", "sharepoint")
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
# one item's window: within what the client shows of one result
# (``mcp_server_evidence.CLIENT_SHOWN_CHARS``, 48,000) even when its text is escaped as
# JSON at the worst rate measured (a mail of quotes and line breaks: 1.67 characters a
# character); a longer text is read on from its next offset
OPEN_MAX_CHARS = 26000
# plx.open with ``citations``: any number in one call (owner 11-23: no cap); each one's
# window, the call's time for opening: none. What one response holds is the client's window
# (``MANY_BYTES``): items past it come back ``not_opened`` BY THE CITATION SENT, without being
# read, and are opened again in the next call, so every citation is reached.
MANY_MAX = 40   # the batch the tests and a plain call use; not a limit (see above)
MANY_MIN_WINDOW = 1000   # a window never shrinks below this for a long list: the list is cut by items instead
MANY_CHARS = 20000
# what one several-citation call's text and envelope may come to: each window is cut so
# the windows together hold at most ``MANY_TEXT`` characters (2,500 each up to 6 items,
# 1,250 at 12), and items past ``MANY_BYTES`` of encoded result are sent back not opened
# (open them again) instead of the whole call failing for its size (the host's response
# holds 48,000 bytes with its freshness note; 11-22: 8 citations at 2,500 each failed whole).
# Measured as the host sends it: the result's JSON as a JSON string (a mail's line breaks
# and quotes are escaped twice: 11-22 a 36,000-byte result still failed the 48,000 cap)
# (11-23: within what the client shows of one result, CLIENT_SHOWN_CHARS)
MANY_TEXT = 34000
MANY_BYTES = 42000
_NEAR_WORD = re.compile(r"[^\W_]+", re.UNICODE)
DEFAULT_LIMIT = 20
MAX_HITS = 50   # the contract's documents maxItems (and limit maximum)

_TOOLS = json.loads((Path(__file__).with_name("ontology_tool_contracts.json"))
                    .read_text(encoding="utf-8"))["tools"]
_CONTRACT = _TOOLS[TOOL_NAME]
# One plx.read window at most (its contract's own bound): a longer read pages by
# text_offset. Asked for more in one read, plx.read refuses the call
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


# the times a read item carries (its own fields): any of them at or after a replay's
# ceiling, or none at all, and the item is not shown under it
_TIMES = ("received_date_time", "sent_date_time", "created_date_time",
          "last_modified_date_time", "received_at", "sent_at", "created_at", "modified_at",
          "last_modified", "created", "date")


def _known_before(got: Mapping[str, Any], moment: Any) -> bool:
    fields = got.get("fields") if isinstance(got.get("fields"), Mapping) else {}
    times = [fields[k] for k in _TIMES if isinstance(fields.get(k), str) and fields[k]]
    return bool(times) and not any(read_ceiling.after(t, moment) for t in times)


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


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
    hits = []
    for document in documents:
        if not isinstance(document, Mapping):
            continue
        hit = {k: v for k, v in document.items() if k in _HIT}
        hits.append(hit)
    out = {k: v for k, v in result.items() if k in _KEPT}
    out["documents"] = hits
    return out


class _Leg:
    """One leg of a search on its own thread (the caller's contextvars carried over: the
    reader's ceiling and the call's phases), so mail/Teams text and records read together.
    ``result()`` waits for the leg to finish, however long it takes, and re-raises its error:
    no leg is cut."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        import contextvars
        import threading
        self._done = threading.Event()
        self._value: Any = None
        self._error: BaseException | None = None
        ctx = contextvars.copy_context()

        def work() -> None:
            try:
                self._value = ctx.run(fn)
            except BaseException as exc:  # noqa: BLE001 - re-raised by result()
                self._error = exc
            finally:
                self._done.set()
        threading.Thread(target=work, name="plx-search-leg", daemon=True).start()

    def result(self) -> Any:
        self._done.wait()
        if self._error is not None:
            raise self._error
        return self._value


def run(call_documents: Callable[[dict[str, Any]], Any], arguments: Any,
        call_read: Callable[[dict[str, Any]], Any] | None = None,
        call_comms: Callable[[dict[str, Any]], Any] | None = None,
        call_records: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
    """One ``plx.search``: mail and Teams through ``call_comms`` (the host's internal
    ``plx.sql_comms_search`` for the session's reader: the guarded views), files and originals through
    ``call_documents`` (its ``plx.documents``). A participant searches mail only; a
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
    # each leg's time on the call's record (``call_phases``: the result's _meta only)
    call_documents, call_read, call_comms, call_records = (
        _timed("documents", call_documents), _timed("read", call_read),
        _timed("comms", call_comms), _timed("records", call_records))
    try:
        moment = read_ceiling.ceiling()
    except read_ceiling.CeilingInvalid as exc:
        return _error("not_ready", str(exc))
    if moment is not None:
        # a replay of a past moment (``read_ceiling``): every leg searches before it
        wanted["until"] = read_ceiling.clamp_until(wanted.get("until"), moment)
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
    # mail/Teams text and records read together; each is waited for to its end
    comms_leg = _Leg(lambda: call_comms({**{k: v for k, v in wanted.items() if k != "sources"},
                                         "sources": comms})) if comms else None
    records_leg = _Leg(lambda: _records(call_records, wanted)) if records else None
    if comms:
        try:
            one = comms_leg.result()
        except Exception as exc:  # noqa: BLE001 - one leg failing: a gap when others ran
            if not (docs or records):
                raise
            one = {"error": {"code": "query_failed", "message": type(exc).__name__}}
    if comms:
        if isinstance(one, Mapping) and (one.get("error") or {}).get("code") in FALLBACK:
            comms, docs = [], [x for x in sources]
        else:
            one = compact(one)
            # files the comms leg did not search (a reader who is not a full reader, or the
            # statement failed) are searched the old way, as before files were a table
            spill = ("sharepoint" in comms and not wanted.get("participant")
                     and not isinstance(cursor, str))
            if "error" in one:
                code = one["error"].get("code")
                if code in COMMS_OWN_ERRORS or not (docs or records or spill):
                    return one
                # mail and Teams could not be read this time (timeout, busy, a failed
                # statement): the other legs' hits stand and a gap says so
                rest = [x for x in comms if x != "sharepoint"]
                one = _gap(rest, code, one["error"].get("message")) if rest else None
            served = ((one or {}).get("searched") or {}).get("sources") or []
            if spill and "sharepoint" not in served:
                docs = [x for x in sources if x == "sharepoint" or x in docs]
            if one is not None:
                got.append(one)
    if docs:
        # every non-record source and none named: the documents search's own default
        whole = not wanted.get("sources") and set(docs) == set(ALL_SOURCES) - {RECORDS}
        asked = ({k: v for k, v in wanted.items() if k != "sources"} if whole
                 else {**wanted, "sources": docs})
        one = _documents(call_documents, asked, call_read)
        if "error" in one:
            return one
        got.append(one)
    if records:
        one = records_leg.result()
        if "error" in one:
            return one
        got.append(one)
    return _merged(got)


def _timed(name: str, call: Any) -> Any:
    if call is None:
        return None
    from operating.call_phases import phase

    def timed(arguments: dict[str, Any]) -> Any:
        with phase(name):
            return call(arguments)
    return timed


# the comms leg errors that are the search's own (the query itself is wrong): the whole
# search answers them; any other (busy, timeout, a failed statement) is a gap when another
# leg is searched
COMMS_OWN_ERRORS = frozenset({"invalid_input"})


def _gap(sources: Sequence[str], code: Any, message: Any) -> dict[str, Any]:
    return {"documents": [], "searched": {"sources": list(sources)},
            "gaps": [{"state": "inaccessible", "reason": str(code or "query_failed"),
                      "subject": "mail and Teams",
                      "detail": "mail and Teams were not searched: "
                      + str(message or code or "query_failed")[:300]}],
            "completeness": {"complete": False, "truncated": False, "returned": 0,
                             "reason": str(code or "query_failed")}}


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
    return compact(call_documents(wanted))


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
    widened = [w for r in results for w in [(r.get("searched") or {}).get("widened")] if w]
    if widened:
        searched["widened"] = "; ".join(dict.fromkeys(widened))[:300]
    depth = [w for r in results for w in [(r.get("searched") or {}).get("depth")] if w]
    searched.pop("depth", None)
    if depth:
        searched["depth"] = "; ".join(dict.fromkeys(depth))[:600]
    cut = [r["completeness"] for r in results
           if (r.get("completeness") or {}).get("truncated")]
    # how many matched in all: each leg's own count, or its hits when it read every match
    each = [(r.get("completeness") or {}).get("total") if isinstance(
        (r.get("completeness") or {}).get("total"), int) else
        len(r["documents"]) if not (r.get("completeness") or {}).get("truncated") else None
        for r in results]
    out: dict[str, Any] = {
        "documents": documents, "searched": searched,
        "gaps": [g for r in results for g in r.get("gaps") or ()][:20],
        "completeness": {"complete": not cut and all(
            (r.get("completeness") or {}).get("complete", True) for r in results),
            "truncated": bool(cut), "returned": len(documents),
            "next_cursor": cut[0].get("next_cursor") if cut else None}}
    # every cut list's own cursor, with the sources it continues: a search whose legs are
    # each cut is read to the end leg by leg (the single ``next_cursor`` is the first's)
    more = [{"sources": list((r.get("searched") or {}).get("sources") or ()),
             "cursor": (r.get("completeness") or {}).get("next_cursor")}
            for r in results if (r.get("completeness") or {}).get("truncated")
            and (r.get("completeness") or {}).get("next_cursor")]
    if len(more) > 1:
        out["completeness"]["continue"] = more
    if cut and None not in each:
        out["completeness"]["total"] = sum(each)
    if not out["completeness"]["complete"]:
        out["completeness"]["reason"] = (cut[0].get("reason") if cut else None) or next(
            ((r.get("completeness") or {}).get("reason") for r in results
             if (r.get("completeness") or {}).get("reason")), "page_cap")
    for key in ("egress_withheld", "unchecked"):
        left = sum(r.get(key, 0) for r in results)
        if left:
            out[key] = left
    return out


def with_coverage(result: Any, arguments: Any) -> Any:
    """``result`` (``fitted``'s) with its ``coverage`` (Ask plan A1a): how many items the
    search found (``matched``), how many are shown, the sources and dates it covered and
    how deep it read (``searched.depth``). ``matched`` is exact when the search read every
    match (its hits, those that did not fit one response, those kept from the model); when
    a next page continues the list it is not counted (null, ``matched_basis`` says so and
    the cursor reaches the rest)."""
    if not isinstance(result, dict) or "error" in result:
        return result
    searched = result.get("searched") or {}
    done = result.get("completeness") or {}
    shown = len(result.get("documents") or ())
    asked = arguments if isinstance(arguments, Mapping) else {}
    cover: dict[str, Any] = {
        "shown": shown, "sources": list(searched.get("sources") or ()),
        "since": searched.get("since") or asked.get("since"),
        "until": searched.get("until") or asked.get("until"),
        "depth": searched.get("depth")}
    total = done.get("total") if isinstance(done.get("total"), int) else None
    if total is None and (done.get("next_cursor") or done.get("truncated")):
        cover.update(matched=None, matched_basis="not counted: more items match than this "
                     "page lists; its cursor continues them")
    else:
        cover.update(matched=(shown if total is None else total)
                     + int(result.get("egress_withheld") or 0)
                     + int(result.get("unchecked") or 0), matched_basis="every match read")
    return {**result, "coverage": cover}


SNIPPETS_CUT = ("snippets were shortened to fit one response; plx.open on a hit's citation "
                "(or its ref) gives the whole text")


def with_cut_note(result: dict[str, Any]) -> dict[str, Any]:
    """``result`` saying, once, at its top level (a gap, the contract's place for it) that
    its snippets were shortened and ``plx.open`` gives the whole text; where the gap list
    already holds its 20, the same words join ``searched.depth``. Said once, never twice."""
    if not any(isinstance(d, dict) and any(
            isinstance(d.get(f), dict) and d[f].get("truncated") for f in ("snippet", "title"))
            for d in result.get("documents") or ()):
        return result
    gaps = list(result.get("gaps") or [])
    if any(g.get("reason") == "snippets_shortened" for g in gaps) \
            or SNIPPETS_CUT in str((result.get("searched") or {}).get("depth") or ""):
        return result
    if len(gaps) < 20:
        return {**result, "gaps": gaps + [{
            "state": "unknown", "reason": "snippets_shortened", "subject": "snippets",
            "detail": SNIPPETS_CUT}]}
    searched = dict(result.get("searched") or {})
    searched["depth"] = ((searched.get("depth") + "; ") if searched.get("depth") else "") \
        + SNIPPETS_CUT
    return {**result, "searched": searched}


def fitted(result: Any, fits: Callable[[dict[str, Any]], bool]) -> Any:
    """``result`` (``run``'s) as it is: a result that does not fit one response is read
    again as a smaller page by ``paged`` (its legs' own cursors continue the rest), never
    cut shorter (the 400 / 200 character snippet stages are gone: a snippet is shown whole
    until one hit alone does not fit, ``_last_resort``)."""
    return result


def over(result: Any, fits: Callable[[dict[str, Any]], bool]) -> bool:
    """Whether a search answer is too large for one response: more hits than the
    contract's ``MAX_HITS``, or not ``fits``."""
    if not isinstance(result, dict) or "error" in result:
        return False
    return len(result.get("documents") or ()) > MAX_HITS or not fits(result)


def paged(search: Callable[[dict[str, Any]], Any], arguments: Any,
          fits: Callable[[dict[str, Any]], bool]) -> Any:
    """``search(arguments)`` (``run`` then ``fitted``) as one page that fits one response.
    A result too large is never refused or cut without a way on: the search is read again
    with a smaller ``limit`` (each leg's own page and cursor, ``completeness.next_cursor`` or
    ``continue``), the limit scaled to the hits that would fit, then halved until the page
    fits or one hit a leg is left. Whole pages only: what the next page holds is the cursor's
    own rows, so no hit is lost between pages."""
    asked = dict(arguments) if isinstance(arguments, Mapping) else arguments
    result = search(asked)
    if not isinstance(asked, dict) or not over(result, fits):
        return result
    limit = asked.get("limit") if isinstance(asked.get("limit"), int) else DEFAULT_LIMIT
    while limit > 1 and over(result, fits):
        shown = max(1, len(result.get("documents") or ()))
        scaled = int(limit * min(MAX_HITS, shown) / shown * 0.9) if shown > MAX_HITS else limit // 2
        limit = max(1, min(limit - 1, scaled))
        result = search({**asked, "limit": limit})
    return _last_resort(result, fits)


# what a hit's snippet and title are cut to, longest first, when a page of one hit (or one
# per leg) still does not fit: the rest of its text is read with plx.open (text_offset), so
# the hit is shown in parts, never refused
LAST_RESORT = (100, 50, 20, 0)


def _last_resort(result: Any, fits: Callable[[dict[str, Any]], bool]) -> Any:
    if not over(result, fits) or not isinstance(result, dict):
        return result
    documents = list(result.get("documents") or [])
    for most in LAST_RESORT:
        cut = [_short_snippet(_short_title(d, most), most) for d in documents]
        trial = with_cut_note({**result, "documents": cut})
        if fits(trial):
            return trial
    return result


def _short_title(hit: Any, most: int) -> Any:
    """``hit`` with its title's text cut to ``most`` characters (said ``truncated``)."""
    title = hit.get("title") if isinstance(hit, dict) else None
    if not (isinstance(title, dict) and isinstance(title.get("untrusted_text"), str)
            and len(title["untrusted_text"]) > most):
        return hit
    return {**hit, "title": {**title, "untrusted_text": title["untrusted_text"][:most],
                             "truncated": True}}


def _short_snippet(hit: Any, most: int) -> Any:
    """``hit`` with its snippet's text cut to ``most`` characters (said ``truncated``)."""
    snip = hit.get("snippet") if isinstance(hit, dict) else None
    if not (isinstance(snip, dict) and isinstance(snip.get("untrusted_text"), str)
            and len(snip["untrusted_text"]) > most):
        return hit
    return {**hit, "snippet": {**snip, "untrusted_text": snip["untrusted_text"][:most],
                               "truncated": True}}


def _near_words(value: Any) -> list[str] | None:
    """``near``'s words (as plx.search splits a query), at most ``comms_search``'s
    MAX_TERMS; None: not a usable value."""
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        return None
    words = list(dict.fromkeys(w.casefold() for w in _NEAR_WORD.findall(value)))
    return words[:16] or None


def open_one(call_read: Callable[[dict[str, Any]], Any], arguments: Any,
             call_comms: Callable[[dict[str, Any]], Any] | None = None, *,
             resolve: Callable[[str], Any] = lambda citation: citation,
             clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """One ``plx.open``: a mail or Teams item through ``call_comms`` (the host's internal
    ``plx.sql_comms_open``: the same guarded views as the search); any other citation, or
    a host not serving the mail tables, through ``call_read`` (the host's own ``plx.read``
    for the session's reader: its gates decide). Unknown arguments fail closed.

    ``citations`` (several, ``MANY_MAX``): each is opened exactly as one ``citation`` is
    (``resolve`` turns a session ref into its handle; None: not shown this session, that
    item is not found), in a ``MANY_CHARS`` window, the results in the order sent under
    ``opened``; one item's refusal or error is that item's, never the others'. Past
    ``near`` words: a mail or
    Teams window starts just before the first of them its text holds (11-21: Ask listed
    35 of a supplier's mails and concluded without opening one; one call now reads the
    part of each that holds the date or amount asked about)."""
    if not isinstance(arguments, Mapping):
        return _error("invalid_input", "arguments must be an object")
    unknown = sorted(set(arguments) - set(_OPEN["input"]["properties"]))
    if unknown:
        return _error("invalid_input", "unknown arguments: " + ", ".join(unknown)[:200])
    near = None
    if "near" in arguments:
        near = _near_words(arguments["near"])
        if near is None:
            return _error("invalid_input", "near: the words to start each window at")
    if "citations" in arguments:
        many = arguments["citations"]
        if "citation" in arguments or "text_offset" in arguments:
            return _error("invalid_input", "send citation (one, text_offset to read on) or "
                                           "citations (several), not both")
        if not isinstance(many, list) or not many or not all(
                isinstance(c, str) and c.strip() for c in many) or len(set(many)) != len(many):
            return _error("invalid_input", "citations: different citations, at least one")
        out: list[dict[str, Any]] = []
        window = min(MANY_CHARS, max(MANY_MIN_WINDOW, MANY_TEXT // len(many)))
        used, rest = 0, []
        for at, asked in enumerate(many):
            # one response holds ``MANY_BYTES``: the citations that do not fit yet are not read
            # now, they come back whole in ``next_citations`` to send again (every citation is
            # read, a page of them per call; the first of a page always is)
            if out and used + int(window * 1.7) + 2000 > MANY_BYTES:
                rest = list(many[at:])
                break
            full = resolve(asked)
            if not isinstance(full, str):
                out.append({"citation": asked, "error": {
                    "code": "not_found", "message": "no result of this session showed it"}})
                continue
            got = _open_single(call_read, call_comms, full, None, near, window)
            # each says which it is: an error by the citation as sent, a result by its own
            # (else the handle opened)
            out.append({"citation": asked, **got} if "error" in got
                       else got if "citation" in got else {"citation": full, **got})
            used += _encoded(out[-1])
        result: dict[str, Any] = {"opened": out, "window_chars": window}
        if rest:
            result["next_citations"] = rest
            result["note"] = (f"{len(out)} of {len(many)} opened; the other {len(rest)} are in "
                              "next_citations: send them as citations to open them")
        return result
    citation = arguments.get("citation")
    if not isinstance(citation, str) or not citation.strip():
        return _error("invalid_input", "send citation: a plx.search hit's citation")
    offset = arguments.get("text_offset")
    if offset is not None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return _error("invalid_input", "text_offset must be a whole number >= 0")
    full = resolve(citation)
    if not isinstance(full, str):
        return {"error": {"code": "not_found", "message": (
            "no plx.search hit or plx.sql row of this session showed that ref: "
            "send the ref or citation a result showed")}}
    return _open_single(call_read, call_comms, full, offset, near, OPEN_MAX_CHARS)


def _encoded(value: Any) -> int:
    """Bytes ``value`` takes in the host's response: its JSON text, sent as a JSON string."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def _open_single(call_read: Callable[[dict[str, Any]], Any],
                 call_comms: Callable[[dict[str, Any]], Any] | None, citation: str,
                 offset: int | None, near: list[str] | None, most: int) -> dict[str, Any]:
    """One item's open (``_opened_one``) with the host's ``coverage`` line (plan A1a): the
    item's whole text length and the characters shown, ``{"chars": total, "shown":
    [start, end]}``, taken from the result's own window fields (a mail's ``text_offset`` /
    ``text_chars`` / ``next_offset``, a read's ``text_window``). A result with no text
    window (a record, an error) gets none: nothing is said that the result does not hold."""
    got = _opened_one(call_read, call_comms, citation, offset, near, most)
    if not isinstance(got, dict) or "error" in got or "coverage" in got:
        return got
    covered = open_coverage(got)
    return {**got, "coverage": covered} if covered else got


def open_coverage(got: Mapping[str, Any]) -> dict[str, Any] | None:
    """``{"chars": total, "shown": [start, end]}`` of one opened text, or None."""
    def whole(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and v >= 0
    window = got.get("text_window")
    if isinstance(window, Mapping) and whole(window.get("total_chars")) \
            and whole(window.get("offset")):
        total, start = window["total_chars"], window["offset"]
        end = window.get("next_offset")
        return {"chars": total, "shown": [start, end if whole(end) else total]}
    if whole(got.get("text_chars")) and whole(got.get("text_offset")):
        total, start, end = got["text_chars"], got["text_offset"], got.get("next_offset")
        return {"chars": total, "shown": [start, end if whole(end) else total]}
    return None


def _opened_one(call_read: Callable[[dict[str, Any]], Any],
                 call_comms: Callable[[dict[str, Any]], Any] | None, citation: str,
                 offset: int | None, near: list[str] | None, most: int) -> dict[str, Any]:
    """One item's open (``open_one``), its gates whole."""
    # one plx.read holds at most CHECK_PAGE_CHARS (its contract's bound): a longer window
    # is read in pages once the whole text has passed the gates (``_widened``)
    wanted: dict[str, Any] = {"citation": citation, "max_chars": most}
    if offset is not None:
        wanted["text_offset"] = offset
    try:
        moment = read_ceiling.ceiling()
    except read_ceiling.CeilingInvalid as exc:
        return _error("not_ready", str(exc))
    got = call_comms({**wanted, **({"near": near} if near else {})}) \
        if call_comms is not None else None
    via_comms = isinstance(got, Mapping) and not got.get("not_comms") and (
        got.get("error") or {}).get("code") not in OPEN_FALLBACK
    if not via_comms:
        got = call_read({**wanted, "max_chars": min(most, CHECK_PAGE_CHARS)})
        if moment is not None and isinstance(got, Mapping) and "error" not in got \
                and not _known_before(got, moment):
            # under a replay's ceiling the host's own read shows only what carries a
            # time before it (``read_ceiling``): no time, or a later one, is not found
            return _error("not_found", "nothing to open before the replayed moment")
    if not isinstance(got, Mapping):
        return _error("open_failed", "the read gave no result")
    if via_comms:
        # a mail or Teams row of the guarded views: a long mail's window (its ``text``
        # cut, ``next_offset``) is shown as is
        return dict(got)
    if offset or _content_cut(got):
        # the item's text is longer than the window shown: read on
        return _widened(call_read, citation, dict(got), most)
    return dict(got)


def _widened(call_read: Callable[[dict[str, Any]], Any], citation: str,
             got: dict[str, Any], most: int) -> dict[str, Any]:
    """``got`` (one plx.read window of a text its gates passed whole) with the windows that
    follow it read on, up to ``most`` characters in all: one window as the model sees it,
    its ``text_window.next_offset`` where the text still goes on (a read that fails or
    shows no more ends it there, said as before)."""
    content, window = got.get("content"), got.get("text_window")
    if not (isinstance(content, Mapping) and isinstance(content.get("untrusted_text"), str)
            and isinstance(window, Mapping)):
        return got
    text, following = content["untrusted_text"], window.get("next_offset")
    while isinstance(following, int) and not isinstance(following, bool) \
            and len(text) < most:
        more = call_read({"citation": citation, "text_offset": following,
                          "max_chars": min(most - len(text), CHECK_PAGE_CHARS)})
        page = more.get("content") if isinstance(more, Mapping) else None
        if not (isinstance(page, Mapping) and isinstance(page.get("untrusted_text"), str)
                and page["untrusted_text"]) or "error" in more:
            break
        text += page["untrusted_text"]
        step = more.get("text_window") if isinstance(more.get("text_window"), Mapping) else {}
        following = step.get("next_offset")
    shown = {k: v for k, v in content.items() if k != "truncated"}
    shown["untrusted_text"] = text
    if isinstance(following, int) and not isinstance(following, bool):
        shown["truncated"] = True
    edge = {k: v for k, v in window.items() if k not in ("next_offset", "sha256")}
    if isinstance(following, int) and not isinstance(following, bool):
        edge["next_offset"] = following
    return {**got, "content": shown, "text_window": edge}


def _content_cut(got: Mapping[str, Any]) -> bool:
    """Whether ``got``'s text goes on past the window shown."""
    window = got.get("text_window")
    following = window.get("next_offset") if isinstance(window, Mapping) else None
    content = got.get("content")
    return (isinstance(following, int) and not isinstance(following, bool)) or (
        isinstance(content, Mapping) and bool(content.get("truncated")))


__all__ = ["OPEN_NAME", "OPEN_TOOL", "open_one", "TOOL", "TOOL_NAME", "TOOL_NAMES", "arguments_for_documents", "compact", "run"]
