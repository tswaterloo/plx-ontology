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

import datetime as dt
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence

# Hits read per call: the tools page and cap after their own gates. The tool holds its
# store lock meanwhile, so the reranked top stays small.
FIND_K = 30
# An as_known_at this close to now is "now" (a cursor pins the first page's time).
NOW_TOLERANCE = dt.timedelta(minutes=15)


def enabled() -> bool:
    return (os.environ.get("PLX_SEARCH") or "").strip().lower() == "hybrid"


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


def _rank(policy: Any, actor: Any, item_gate: Any, query: str, *,
          kinds: Sequence[str] | None = None, k: int = FIND_K) -> Ranked:
    import dataclasses
    from operating.search.config import load_weights
    from operating.search.core import search
    try:
        weights = load_weights()
        weights = dataclasses.replace(weights, rerank_top=min(weights.rerank_top, k))
        result = search(query, actor, k=k, kinds=kinds, policy=policy, item_gate=item_gate,
                        weights=weights, hidden=False)
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
    digest = hashlib.sha256(json.dumps([h["object_id"] for h in out]).encode()
                            ).hexdigest()[:16]
    return Ranked(out, "hybrid:" + digest)


def prefetch(policy: Any, actor: Any, item_gate: Any, query: str) -> None:
    """Run the ranking now (before the caller takes its store lock) for the call's
    later ``ranked``; nothing when search is off or no call state is open."""
    state = _CALL.get()
    if state is not None and enabled() and query not in state["prefetched"]:
        state["prefetched"][query] = _rank(policy, actor, item_gate, query)


def ranked(policy: Any, actor: Any, item_gate: Any, query: str) -> Ranked:
    """The call's ranking of ``query`` (prefetched, else run now), recorded as the
    call's mode. Search off: an empty ``index`` ranking."""
    if not enabled():
        return Ranked()
    state = _CALL.get()
    found = state["prefetched"].get(query) if state is not None else None
    result = found if found is not None else _rank(policy, actor, item_gate, query)
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
