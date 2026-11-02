"""``PLX_SEARCH=hybrid``: the free-text paths of ``plx.find`` (``OntologyTools.
_text_matches``) and ``plx.documents`` (``EvidenceTools._query_documents``) ask
``search()`` first, ranked by meaning, OR-matched and typo tolerant, instead of only the
store's AND-matching word index.

The tools' own gates stay in force on every hit shown: a hit becomes a candidate only
when the projection store shows its object to this reader at the asked time
(``_visible_versions``: audience, markings, time and the item ACL), and its citation is
the store's version handle, never the search row's. Search runs only for "now" (an
``as_known_at`` in the past reads the word index alone: the search tables keep no
history). Any failure of the search path (no database, no schema, a timeout) falls back
to the word index, stated in the answer (``search_fallback`` gap). A page cursor is bound
to the ranking it pages (``Ranked.mode``): a change mid-pagination is an error.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import sys
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence

# Hits read per call: the tools page and cap after their own gates. The tool holds its
# store lock meanwhile, so the reranked top stays small.
FIND_K = 30
# Hits ranked before the item gate (``ranked`` gates them on use, best first, until
# FIND_K pass): the ranking runs beside the tool's own word-index search
# (``prefetch``), and the per-item gate runs in the tool call, as every gate does.
POOL_K = 2 * FIND_K
# The longest a tool call waits for the ranking it started (``prefetch``); past it the
# word index answers alone and says so (``search_fallback``).
RANK_WAIT_S = 20.0
_POOL = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="plx-search")
# An as_known_at this close to now is "now" (a cursor pins the first page's time).
NOW_TOLERANCE = dt.timedelta(minutes=15)
# A ranking is reused for the same reader and query this long (a next page, a repeated
# search): its pages stay one order and the models do not run again. Its ids are gated
# again on every use, so a grant revoked meanwhile still hides them.
REUSE_SECONDS = 600.0
REUSE_MAX = 64


def enabled() -> bool:
    return (os.environ.get("PLX_SEARCH") or "").strip().lower() == "hybrid"


def warm_if_enabled() -> None:
    """At a session's start: when hybrid search is on, load its models in the background
    so the first searches are embedded and reranked, not ranked while the reranker loads.
    Never raises (a model that cannot load is stated per query, as before)."""
    if not enabled():
        return
    try:
        from operating.search.models import warm
        warm(wait=False)
    except Exception as exc:  # noqa: BLE001 - search still runs, stating what is off
        print(f"plx search warm-up failed: {type(exc).__name__}", file=sys.stderr)


def is_now(at: dt.datetime, now: dt.datetime | None = None) -> bool:
    """Whether a read at ``at`` asks about now (the tool's clock ``now``)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return at >= now - NOW_TOLERANCE


@dataclass
class Ranked:
    """One hybrid ranking of a query: the hits naming a projection object, best first
    (one per object); ``mode`` what a page cursor binds (``index``: the word index alone;
    ``hybrid:<digest of the ordered object ids>``); ``problem`` why search did not run
    when it was asked to (shown to the reader as a ``search_fallback`` gap)."""
    hits: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "index"
    problem: str | None = None


_CALL: ContextVar[dict[str, Any] | None] = ContextVar("plx_search_call", default=None)


@contextmanager
def call_state() -> Iterator[dict[str, Any]]:
    """One tool call's search state: the rankings fetched before the store lock is
    taken (``prefetch``) and the mode and problem of the ranking the call used."""
    state: dict[str, Any] = {"mode": "index", "problem": None, "prefetched": {}}
    token = _CALL.set(state)
    try:
        yield state
    finally:
        _CALL.reset(token)


def current() -> dict[str, Any] | None:
    return _CALL.get()


# key -> (made at, the policy it was made under (kept so its id is never reused while
# the entry lives), the ranking)
_RECENT: "OrderedDict[tuple, tuple[float, Any, Ranked]]" = OrderedDict()
_RECENT_LOCK = threading.Lock()


def _rank(policy: Any, actor: Any, query: str, *,
          kinds: Sequence[str] | None = None, k: int = POOL_K) -> Ranked:
    """The ranking of ``query`` for this reader before the item gate (``_gated``
    applies it on use): the one made in the last
    ``REUSE_SECONDS`` for the same policy, reader, query, kinds, k, database and
    weights, else a new one (kept only when search ran)."""
    try:
        key: tuple | None = (id(policy), actor, query, tuple(kinds or ()), k,
                             os.environ.get("PLX_PG_DSN"), os.environ.get("PLX_SEARCH_WEIGHTS"),
                             os.environ.get("PLX_RERANK"))
        hash(key)
    except TypeError:
        key = None   # an unhashable reader is never reused
    now = time.monotonic()
    if key is not None:
        with _RECENT_LOCK:
            found = _RECENT.get(key)
            if found is not None and now - found[0] <= REUSE_SECONDS:
                _RECENT.move_to_end(key)
                return found[2]
    result = _ranked_now(policy, actor, query, kinds=kinds, k=k)
    if key is not None and result.problem is None:
        with _RECENT_LOCK:
            _RECENT[key] = (now, policy, result)
            _RECENT.move_to_end(key)
            while len(_RECENT) > REUSE_MAX:
                _RECENT.popitem(last=False)
    return result


def _deferred(_actor: Any, _handle: str) -> bool:
    """The item gate inside ``search``: every per-item decision is deferred to
    ``_gated``, made in the tool call (the read policy's units and markings still filter
    every row in SQL before anything is ranked)."""
    return True


def _ranked_now(policy: Any, actor: Any, query: str, *,
                kinds: Sequence[str] | None = None, k: int = POOL_K) -> Ranked:
    import dataclasses
    from operating.search.config import load_weights
    from operating.search.core import search
    try:
        weights = load_weights()
        weights = dataclasses.replace(weights, rerank_top=min(weights.rerank_top, FIND_K))
        result = search(query, actor, k=k, kinds=kinds, policy=policy, item_gate=_deferred,
                        weights=weights, hidden=False, log=False)
    except Exception as exc:  # noqa: BLE001 - the word index answers, and says so
        return Ranked(problem="search_unavailable:" + type(exc).__name__)
    if "no_read_policy" in result.notes:
        return Ranked(problem="search_no_read_policy")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in result.hits:
        object_id = hit.get("object_id")
        if isinstance(object_id, str) and object_id and object_id not in seen:
            seen.add(object_id)
            out.append(hit)
    return Ranked(out, "hybrid")


def _gated(result: Ranked, policy: Any, actor: Any, item_gate: Any) -> Ranked:
    """``result`` as this reader may be shown it now: each hit through the item gate
    (``SearchReader.item_allowed``: the per-item ACL of a mail, file or Teams item),
    best first, until ``FIND_K`` pass; ``mode`` binds a cursor to that order."""
    from operating.search.reader import SearchReader
    if result.problem is not None:
        return result
    reader = SearchReader.from_policy(policy, actor, item_gate=item_gate)
    out: list[dict[str, Any]] = []
    for hit in result.hits:
        if len(out) >= FIND_K:
            break
        if reader.item_allowed(hit.get("handle")):
            out.append(hit)
    digest = hashlib.sha256(json.dumps([h["object_id"] for h in out]).encode()
                            ).hexdigest()[:16]
    return Ranked(out, "hybrid:" + digest)


def prefetch(policy: Any, actor: Any, item_gate: Any, query: str) -> None:
    """Start the ranking now, beside the caller's own search (it runs on a search
    thread: the database and the models, never the caller's store or gate), for the
    call's later ``ranked``; nothing when search is off or no call state is open."""
    state = _CALL.get()
    if state is not None and enabled() and query not in state["prefetched"]:
        state["prefetched"][query] = _POOL.submit(_rank, policy, actor, query)


def ranked(policy: Any, actor: Any, item_gate: Any, query: str) -> Ranked:
    """The call's ranking of ``query`` (prefetched, else run now), gated for this
    reader (``_gated``) and recorded as the call's mode. Search off: an empty
    ``index`` ranking."""
    if not enabled():
        return Ranked()
    state = _CALL.get()
    found = state["prefetched"].get(query) if state is not None else None
    if found is None:
        result = _rank(policy, actor, query)
    else:
        try:
            result = found.result(timeout=RANK_WAIT_S)
        except cf.TimeoutError:
            result = Ranked(problem="search_timeout")
        except Exception as exc:  # noqa: BLE001 - the word index answers, and says so
            result = Ranked(problem="search_unavailable:" + type(exc).__name__)
    result = _gated(result, policy, actor, item_gate)
    if state is not None:
        state["mode"], state["problem"] = result.mode, result.problem
    return result


def fallback_gap(subject: str) -> dict[str, Any] | None:
    """The gap a call states when hybrid search was asked for and could not run."""
    state = _CALL.get()
    if state is None or not state.get("problem"):
        return None
    return {"state": "unknown", "reason": "search_fallback", "subject": subject[:700],
            "detail": ("hybrid search could not run (" + state["problem"][:80] + "); only "
                       "the word index answered: a document must hold every word of the "
                       "query, and the order is not by relevance")}


def check_cursor_mode(cursor_mode: str | None, mode: str) -> None:
    """A page cursor continues only the ranking it was written for: a mode or order
    change mid-pagination is an explicit error, never a short or "complete" page."""
    from operating.ontology_tools import ToolError
    if (cursor_mode or "index") != mode:
        raise ToolError("invalid_cursor",
                        "the ranking this cursor pages has changed (hybrid search ran or "
                        "not, or its order moved); later pages cannot be listed",
                        "repeat the query without cursor")


def bindings(conn: Any, object_ids: Sequence[str]) -> Mapping[str, str]:
    """object_id -> binding id from the projection store (the caller passes readable
    objects only)."""
    if not object_ids:
        return {}
    return {row[0]: row[1] for row in conn.execute(
        "SELECT object_id, binding_id FROM objects WHERE object_id IN "
        "(SELECT value FROM json_each(?))", (json.dumps(list(object_ids)),))}


# Source families graph expansion follows (the audience-key prefixes of the projection).
GRAPH_FAMILIES = ("api", "bc", "mail", "sharepoint", "sharepoint-list", "teams", "mc",
                  "original", "brain")
GRAPH_WEIGHT = 0.8


def neighbours_for(evidence_tools: Any, actor: Any) -> Any:
    """``search(neighbours=)`` over a projection store: the objects linked to the
    question's resolved objects within two hops (a PO's emails, their attachments, its
    twin in another system), as this reader may see them at now: the same visible-link
    read and item gate ``plx.documents`` follows (``EvidenceTools._linked_documents``)."""
    def neighbours(resolved: Sequence[str], _candidates: Sequence[tuple[str, float]]
                   ) -> list[tuple[str, float]]:
        def body(gate: str) -> list[str]:
            scope = evidence_tools.traverse._scope(actor, {}, gate, None)
            visible = evidence_tools.traverse._visible_ids(scope, list(resolved))
            if not visible:
                return []
            linked, _cut = evidence_tools._linked_documents(scope, sorted(visible),
                                                            GRAPH_FAMILIES)
            return sorted(linked)
        try:
            ids = evidence_tools._with_gate(actor, body)
        except Exception as exc:  # noqa: BLE001 - no expansion, never a guess
            print(f"plx search graph expansion failed: {type(exc).__name__}",
                  file=sys.stderr)
            return []
        return [(object_id, GRAPH_WEIGHT) for object_id in ids]
    return neighbours
