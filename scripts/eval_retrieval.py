"""Retrieval metrics on the graded use-case cards: recall@10 and MRR of each card's known
source documents, for its question and its messy variants, given a search function.

    python3 -m scripts.eval_retrieval --out FILE [--search tools|hybrid]
        [--tool plx.documents|plx.find] [--host-config FILE] [--k 10]
        [--phrasings original,bare,m1,m2,m3] [--items ID,...] [--limit N]

**Targets** (``targets``): the documents a card is about, each one document however many
handles name it:

- each byte-pinned document the compiled item names: its ``scoring.source_documents``
  (scorer /21: its set's and its map's own documents) and the ``original://sha256/<hex>``
  pins of its ``oracle.derived_from``, and each registry row id (``co-``/``nr-``/``rv-``)
  it cites; a document is matched by ``original://sha256/<hex>``, by
  ``original://<row id>`` of every registry row holding those bytes, by the
  ``sharepoint://<drive>/<item>`` the row was fetched from (any version), and by the
  row's file name when no registry row of other bytes carries that name (a package member
  served from SharePoint carries no bytes pin; ``README.md`` names nothing);
- the use-case catalog's documents of the card's use case (``examples/use_case_catalog.json``:
  its map as an original row or a SharePoint item);
- the record the card is about (``root`` of an anchored card: ``PO5447``,
  ``Related Release Order Map.md``): a hit whose title, snippet or mentions name it.

A card with no target is ``no_targets`` and stays out of the means (counted).

**Metrics** per query over the top ``k`` documents (one slot per document: its other
handles and pages never take a second, ``dedupe``): ``recall@k`` = targets found /
``min(targets, k)``, each hit counting for one target at most and recall capped at 1.0
(a card with 34 workbooks can still reach 1), ``mrr`` = 1 / rank of the
first hit on any target (0 when none in the top k), ``hit@k`` = any target found. Means
per set and phrasing, and overall per phrasing; ``served`` measures what the reader is
given: the tool's routed use-case documents first, then its ranked hits.

**Phrasings**: ``original`` (the compiled question exactly as the agent is asked it, case
prefix included), ``bare`` (the card's own question and its record, no case prefix: what
a person would type cleanly), ``m1``-``m3`` (``scripts.eval_messy``).

**Search** (pluggable, ``SearchFn``: ``(question, k) -> {hits: [{handle, title, snippet,
kind, score?, signals?}], extra?}``):

- ``tools`` (default): today's search, the read-only tool ``plx.documents`` (or
  ``--tool plx.find``) called with ``{"query": question, "limit": k}`` through the host's
  harness reader (``scripts.eval_real_agent.LiveHost``) as the reader of
  ``PLX_EVAL_READER`` on ``PLX_WORKBENCH_STATE_DIR`` with ``--host-config`` (e.g. the
  owner host of ``scripts.eval_owner_host``). Every hit has passed the host's own gate for
  that reader. ``plx.documents``' ``use_case`` block (the use case's own documents the
  tool pins beside the hits) is reported apart as ``routed`` and never ranked.
- ``hybrid``: the hybrid search run inside the same host as the same reader
  (``HostSearch``: ``evals.harness_reads`` ``plx.search_ranked``), under the read policy
  and item gate the host decides for that reader's tool calls, as ``plx.find`` /
  ``plx.documents`` rank with ``PLX_SEARCH=hybrid``; its signals and timings recorded per
  query, its models loaded before the first query is measured. Backends are a fixed
  registry (``BACKENDS``), never loaded from a runtime string. The run carries the host
  run's evidence label.

A host config that resolves readers in the Entra directory gets the directory's Graph
credentials passed to its host process (as ``scripts.eval_real_agent`` passes them); a
host that does not start or serve the reader stops the run (``host_unavailable``):
never a table of empty rankings.

The output (``--out``) holds the tables, every query's top ``k`` with its signals and
target flags, and the evidence label; stdout gets the tables. No key is read or printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "plx-eval-retrieval/v1"
K = 10
PHRASINGS = ("original", "bare", "m1", "m2", "m3")
CATALOG_PATH = ROOT / "examples" / "use_case_catalog.json"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ROW_ID = re.compile(r"^(?:co|nr|rv)-[0-9a-f]{6,}$")
NAME_PREFIX = "name:"  # a target handle that is a unique registry file name, folded
MIN_NAME = 8
SearchFn = Callable[[str, int], Mapping[str, Any]]


class RetrievalError(ValueError):
    """A search backend or input that cannot be used (fail closed: no table on a guess)."""


# --------------------------------------------------------------------------- targets

def normalize_handle(handle: Any) -> str:
    """One document's handle without its page, fragment or SharePoint version, decoded:
    ``original://co-1#page=3`` -> ``original://co-1``; ``sharepoint://b%21x/Y@"v",2``
    -> ``sharepoint://b!x/Y``."""
    text = unquote(str(handle or "")).split("#", 1)[0].split("&page=", 1)[0]
    if text.startswith("sharepoint://"):
        text = text.split("@", 1)[0]
    return text


def _fold(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize(
        "NFKD", str(text or "")).casefold())


def registry_handles(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, set[str]],
                                                                  dict[str, str]]:
    """(sha -> every handle of those bytes, row id -> its sha) from the case-originals
    registry's rows."""
    by_sha: dict[str, set[str]] = {}
    sha_of: dict[str, str] = {}
    # A file name names one document only when no other registry row of other bytes
    # carries it ("README.md" names many): such a name matches a hit's title (a package
    # member served from SharePoint carries no bytes pin, only its name).
    shas_of_name: dict[str, set[str]] = {}
    for row in rows:
        sha = (row.get("expected") or {}).get("sha256") if isinstance(row, Mapping) \
            and isinstance(row.get("expected"), Mapping) else None
        name = _fold(row.get("name")) if isinstance(row, Mapping) else ""
        if isinstance(sha, str) and _HEX64.match(sha) and len(name) >= MIN_NAME:
            shas_of_name.setdefault(name, set()).add(sha)
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        sha = (row.get("expected") or {}).get("sha256") \
            if isinstance(row.get("expected"), Mapping) else None
        if not isinstance(sha, str) or not _HEX64.match(sha):
            continue
        sha_of[row["id"]] = sha
        handles = by_sha.setdefault(sha, {f"original://sha256/{sha}"})
        handles.add(f"original://{row['id']}")
        source = row.get("source") if isinstance(row.get("source"), Mapping) else {}
        if source.get("drive_id") and source.get("item_id"):
            handles.add(normalize_handle(f"sharepoint://{source['drive_id']}/{source['item_id']}"))
        name = _fold(row.get("name"))
        if len(shas_of_name.get(name, ())) == 1:
            handles.add(f"{NAME_PREFIX}{name}")
    return by_sha, sha_of


def catalog_documents(catalog: Mapping[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Use case id -> its catalog documents, each ``{original}`` or ``{sharepoint}``
    handle."""
    out: dict[str, list[dict[str, str]]] = {}
    for case in catalog.get("use_cases") or []:
        if not isinstance(case, Mapping) or not isinstance(case.get("id"), str):
            continue
        docs = []
        for doc in case.get("documents") or []:
            if not isinstance(doc, Mapping):
                continue
            if isinstance(doc.get("original"), str):
                docs.append({"original": doc["original"]})
            sp = doc.get("sharepoint")
            if isinstance(sp, Mapping) and sp.get("drive_id") and sp.get("item_id"):
                docs.append({"sharepoint": normalize_handle(
                    f"sharepoint://{sp['drive_id']}/{sp['item_id']}")})
        out[case["id"]] = docs
    return out


def targets(row: Mapping[str, Any], by_sha: Mapping[str, set[str]],
            sha_of: Mapping[str, str],
            catalog: Mapping[str, Sequence[Mapping[str, str]]]) -> list[dict[str, Any]]:
    """The card's target documents (module docstring, **Targets**): each ``{key,
    handles}`` (normalized handles) or, for its record, ``{key, text}``."""
    item = row["item"]
    groups: dict[str, set[str]] = {}

    def add_sha(sha: str) -> None:
        groups.setdefault(f"sha256:{sha}", set()).update(
            by_sha.get(sha, {f"original://sha256/{sha}"}))

    def add_row(row_id: str) -> None:
        if row_id in sha_of:
            add_sha(sha_of[row_id])
        else:
            groups.setdefault(f"original:{row_id}", set()).add(f"original://{row_id}")

    for sha in (item.get("scoring") or {}).get("source_documents") or []:
        if isinstance(sha, str) and _HEX64.match(sha):
            add_sha(sha)
    for ref in (item.get("oracle") or {}).get("derived_from") or []:
        ref = str(ref)
        if ref.startswith("original://sha256/") and _HEX64.match(ref[18:]):
            add_sha(ref[18:])
        elif _ROW_ID.match(ref):
            add_row(ref)
    for doc in catalog.get(str(row["case"]), ()):
        if "original" in doc:
            add_row(doc["original"])
        elif "sharepoint" in doc:
            key = doc["sharepoint"]
            if not any(key in handles for handles in groups.values()):
                groups.setdefault(f"sharepoint:{key}", set()).add(key)
    out: list[dict[str, Any]] = [{"key": key, "handles": sorted(handles)}
                                 for key, handles in sorted(groups.items())]
    root = item.get("root") or {}
    if root.get("identifier_kind") != "case_card_batch" and root.get("identifier"):
        out.append({"key": f"record:{root['identifier']}", "text": _fold(root["identifier"])})
    return out


def _hit_text(hit: Mapping[str, Any]) -> str:
    parts = [hit.get("title"), hit.get("snippet")]
    parts += [json.dumps(m, sort_keys=True) for m in hit.get("mentions") or []]
    return _fold(" ".join(str(p) for p in parts if p))


def matches(hit: Mapping[str, Any], target: Mapping[str, Any]) -> bool:
    if "text" in target:
        return bool(target["text"]) and target["text"] in _hit_text(hit)
    handles = set(target["handles"])
    return normalize_handle(hit.get("handle")) in handles \
        or f"{NAME_PREFIX}{_fold(hit.get('title'))}" in handles


def document_key(hit: Mapping[str, Any], wanted: Sequence[Mapping[str, Any]]) -> str:
    """The one document a hit is: the first target document it matches (any of that
    document's handles, any page), else its own handle without page or version."""
    for target in wanted:
        if "handles" in target and matches(hit, target):
            return target["key"]
    return normalize_handle(hit.get("handle"))


def dedupe(hits: Sequence[Mapping[str, Any]], wanted: Sequence[Mapping[str, Any]]
           ) -> list[Mapping[str, Any]]:
    """One slot per document, at its best rank: a document's other handles and pages
    (``original://co-1#page=2`` after ``sharepoint://...``) never take a second slot."""
    seen: set[str] = set()
    out = []
    for hit in hits:
        key = document_key(hit, wanted)
        if key not in seen:
            seen.add(key)
            out.append(hit)
    return out


def measure(hits: Sequence[Mapping[str, Any]], wanted: Sequence[Mapping[str, Any]],
            k: int = K) -> dict[str, Any]:
    """recall@k, mrr and hit@k of one ranked list (module docstring, **Metrics**), over
    its top k documents (``dedupe``). A hit counts for one target at most (a hit on the
    record's document that also names the record is one find), and recall is at most
    1.0. ``relevant``: per hit of the top k, the target keys it matched."""
    top = dedupe(hits, wanted)[:k]
    relevant = [[t["key"] for t in wanted if matches(hit, t)] for hit in top]
    found: list[str] = []
    for keys in relevant:
        fresh = [key for key in keys if key not in found]
        if fresh:
            found.append(fresh[0])
    first = next((rank for rank, keys in enumerate(relevant, 1) if keys), None)
    return {"recall": min(1.0, len(found) / min(len(wanted), k)) if wanted else None,
            "mrr": (1.0 / first if first else 0.0) if wanted else None,
            "hit": (first is not None) if wanted else None,
            "found": sorted(found), "relevant": relevant}


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    scored = [r for r in rows if r.get("recall") is not None]
    n = len(scored)
    return {"queries": len(rows), "scored": n, "no_targets": len(rows) - n,
            "errors": sum(bool(r.get("error")) for r in rows),
            "recall@k": round(sum(r["recall"] for r in scored) / n, 4) if n else None,
            "mrr": round(sum(r["mrr"] for r in scored) / n, 4) if n else None,
            "hit@k": round(sum(bool(r["hit"]) for r in scored) / n, 4) if n else None}


# --------------------------------------------------------------------------- questions

def phrasings(row: Mapping[str, Any], card: Mapping[str, Any] | None,
              variants: Mapping[str, Any] | None) -> dict[str, str]:
    """The questions one card is searched with (module docstring, **Phrasings**)."""
    from scripts import eval_messy as messy
    out = {"original": row["item"]["question"]["text"]}
    if card is not None:
        out["bare"] = messy.asked_text(card, row["item"])
    for kind in messy.KINDS:
        text = ((variants or {}).get("variants") or {}).get(kind)
        if isinstance(text, str):
            out[kind] = text
    return out


# --------------------------------------------------------------------------- search

def _text(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("untrusted_text")
    return value if isinstance(value, str) else None


def document_hits(result: Mapping[str, Any]) -> dict[str, Any]:
    """A ``plx.documents`` result as ranked hits, its ``use_case`` documents apart."""
    hits = [{"handle": doc.get("citation"), "title": _text(doc.get("title")),
             "snippet": _text(doc.get("snippet")), "kind": doc.get("kind"),
             "mentions": doc.get("mentions") or [], "signals": None}
            for doc in result.get("documents") or [] if isinstance(doc, Mapping)]
    routed = [{"handle": doc.get("citation"), "title": _text(doc.get("title")),
               "kind": doc.get("kind")}
              for doc in ((result.get("use_case") or {}).get("documents") or [])
              if isinstance(doc, Mapping)]
    reasons = sorted({g.get("reason") for g in result.get("gaps") or []
                      if isinstance(g, Mapping) and isinstance(g.get("reason"), str)})
    return {"hits": hits, "routed": routed, "gap_reasons": reasons,
            "use_case": (result.get("use_case") or {}).get("id")}


def find_hits(result: Mapping[str, Any]) -> dict[str, Any]:
    """A ``plx.find`` result as ranked hits: each candidate's origin citation."""
    hits = []
    for cand in result.get("candidates") or []:
        if not isinstance(cand, Mapping):
            continue
        display = cand.get("display") if isinstance(cand.get("display"), Mapping) else {}
        origin = (display.get("origin") or {}) if isinstance(display, Mapping) else {}
        hits.append({"handle": origin.get("citation") or cand.get("object_id"),
                     "title": _text(display), "snippet": None, "kind": cand.get("object_type"),
                     "mentions": [], "signals": {"score": cand.get("score")}
                     if cand.get("score") is not None else None})
    return {"hits": hits, "routed": [], "gap_reasons": sorted({
        g.get("reason") for g in result.get("gaps") or [] if isinstance(g, Mapping)
        and isinstance(g.get("reason"), str)})}


class ToolsSearch:
    """Today's search through the host's read-only tools (module docstring, **Search**)."""

    def __init__(self, host: Any, tool: str = "plx.documents") -> None:
        if tool not in ("plx.documents", "plx.find"):
            raise RetrievalError(f"tool_not_a_search:{tool}")
        self.host, self.tool = host, tool

    def __call__(self, question: str, k: int) -> dict[str, Any]:
        result = self.host.call(self.tool, {"query": question, "limit": k})
        if not isinstance(result, Mapping) or result.get("ok") is False:
            return {"hits": [], "error": "tool_refused"}
        return (document_hits if self.tool == "plx.documents" else find_hits)(result)


def live_host(environ: Mapping[str, str], config: Path) -> Any:
    """The harness reader of ``PLX_EVAL_READER`` on the state and ``config`` (no secret
    is read from the environment: a fresh session secret is minted for this run)."""
    from scripts import eval_case_cards as cards
    from scripts import eval_real_agent as real
    claims, problem = real.reader_identity(environ, real.READER_ENV)
    state = Path(environ.get(real.STATE_ENV) or "")
    if claims is None or not state.is_dir() or not Path(config).is_file():
        raise RetrievalError(f"host_unavailable:{problem or 'state_or_config_missing'}")
    host_config = json.loads(Path(config).read_text("utf-8"))
    # A host config resolving readers in the Entra directory needs the directory's Graph
    # credentials passed to the host process (``eval_real_agent.live_part`` does the same);
    # without them the host refuses to start and every read would fail.
    extra = ({name: environ[name] for name in real.GRAPH_ENV if environ.get(name)}
             if host_config.get("entra_directory") else {})
    host = real.LiveHost(state, Path(config), claims, secrets.token_hex(32),
                         tenant=str(host_config.get("tenant") or "plx"), extra_env=extra,
                         agent=cards.CARD_AGENT)
    try:
        host.tools()   # the host starts and serves this reader, or the run stops here
    except Exception as error:  # noqa: BLE001 - no table on a host that cannot answer
        host.close()
        raise RetrievalError(f"host_unavailable:{str(error)[:120]}") from None
    return host


class HostSearch:
    """Hybrid search (``operating.search``) run inside the tools host as its reader
    (``evals.harness_reads`` ``plx.search_ranked``): the read policy the host decides for
    a tool call scopes every retriever in SQL, and the tools' item gate decides every hit
    shown, exactly as ``plx.find`` / ``plx.documents`` rank with ``PLX_SEARCH=hybrid``.
    A refused or failed search is an error (counted), never an empty ranking. Asked for
    ``OVERFETCH`` x k hits (capped at 100) so a document's other chunks never leave the
    reader's top k short."""

    OVERFETCH = 3

    def __init__(self, host: Any) -> None:
        self.host = host

    def __call__(self, question: str, k: int) -> dict[str, Any]:
        result = self.host.read("plx.search_ranked",
                                {"question": question, "k": min(k * self.OVERFETCH, 100)})
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            error = result.get("error") if isinstance(result, Mapping) else None
            raise RetrievalError(f"search_refused:{str(error or 'unreadable')[:60]}")
        hits = [dict(h) for h in result.get("hits") or [] if isinstance(h, Mapping)]
        return {"hits": hits, "extra": result.get("extra")}


# The search backends a run may name (``--search``), a fixed registry: no backend is
# loaded from a runtime string. Each is built over the tools host of the run's reader,
# so no backend is ever measured ungated.
BACKENDS: dict[str, Callable[[Any, str], SearchFn]] = {
    "tools": lambda host, tool: ToolsSearch(host, tool),
    "hybrid": lambda host, _tool: HostSearch(host)}


# --------------------------------------------------------------------------- agent searches

def agent_searches(paths: Sequence[Path], rows: Sequence[Mapping[str, Any]], *,
                   by_sha: Mapping[str, set[str]], sha_of: Mapping[str, str],
                   catalog: Mapping[str, Sequence[Mapping[str, str]]], k: int = K
                   ) -> dict[str, Any]:
    """The agent's own searches in recorded card runs (``--transcripts``): per item, the
    latest transcript (the last path given wins), its first ``plx.documents`` /
    ``plx.find`` call measured as a query (``first``), and whether any search of the
    session found a target in its top ``k`` (``any_hit``). The queries are the agent's
    rewrites, not the card's question: this measures today's search as the agent drives it."""
    from scripts import eval_messy as messy
    by_id = {r["item"]["id"]: r for r in rows}
    latest: dict[tuple[str, int], Path] = {}
    for path in messy.transcripts(paths):
        try:
            doc = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("item_id") in by_id:
            latest[(doc["item_id"], int(doc.get("phrasing_index") or 0))] = path
    out = []
    for (item_id, phrasing), path in sorted(latest.items()):
        doc = json.loads(path.read_text("utf-8"))
        row = by_id[item_id]
        wanted = targets(row, by_sha, sha_of, catalog)
        searches = []
        for event in doc.get("events") or []:
            if not isinstance(event, Mapping) or event.get("name") not in messy.SEARCH_TOOLS \
                    or not isinstance(event.get("result"), Mapping):
                continue
            got = (document_hits if event["name"] == "plx.documents" else find_hits)(
                event["result"])
            searches.append((event, got, measure(got["hits"], wanted, k)))
        served = [measure([*(g.get("routed") or []), *g["hits"]], wanted, k)
                  for _, g, _ in searches]
        first = searches[0] if searches else None
        out.append({"case": row["case"], "card_id": row["card_id"], "item_id": item_id,
                    "phrasing_index": phrasing, "searches": len(searches),
                    "first_query": (first[0].get("arguments") or {}).get("query")
                    if first else None,
                    "recall": first[2]["recall"] if first else (0.0 if wanted else None),
                    "mrr": first[2]["mrr"] if first else (0.0 if wanted else None),
                    "hit": first[2]["hit"] if first else (False if wanted else None),
                    "any_hit": any(m["hit"] for _, _, m in searches) if wanted else None,
                    "any_routed": any(matches(h, t) for _, g, _ in searches
                                      for h in g.get("routed") or [] for t in wanted)
                    if wanted else None,
                    "served": {name: (served[0][name] if served else
                                      (0.0 if name != "hit" else False) if wanted else None)
                               for name in ("recall", "mrr", "hit")}})
    scored = [q for q in out if q["any_hit"] is not None]
    return {"basis": "the first search call of the latest recorded session per item",
            "first": summarize(out),
            "first_served": summarize([{**q, **q["served"]} for q in out]),
            "any_search_hit": round(sum(bool(q["any_hit"]) for q in scored) / len(scored), 4)
            if scored else None,
            "any_routed": round(sum(bool(q["any_routed"]) for q in scored) / len(scored), 4)
            if scored else None,
            "by_set": {case: summarize([q for q in out if q["case"] == case])
                       for case in sorted({q["case"] for q in out})},
            "items": out}


# --------------------------------------------------------------------------- run

def evaluate(rows: Sequence[Mapping[str, Any]], search: SearchFn, *,
             sets: Mapping[str, Any], variants: Mapping[tuple[str, str], Any],
             by_sha: Mapping[str, set[str]], sha_of: Mapping[str, str],
             catalog: Mapping[str, Sequence[Mapping[str, str]]],
             kinds: Sequence[str] = PHRASINGS, k: int = K,
             clock: Callable[[], float] = time.monotonic,
             progress: Callable[[int], None] | None = None) -> dict[str, Any]:
    """Every card x phrasing searched once and measured; tables per set and overall."""
    from scripts import eval_messy as messy
    queries: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row["case"]), str(row["card_id"]))
        wanted = targets(row, by_sha, sha_of, catalog)
        texts = phrasings(row, messy.card_of(sets, *key), variants.get(key))
        for kind in kinds:
            if kind not in texts:
                continue
            started = clock()
            try:
                result = dict(search(texts[kind], k))
            except Exception as error:  # noqa: BLE001 - a failed query is counted, never dropped
                result = {"hits": [], "error": type(error).__name__}
            elapsed = round((clock() - started) * 1000)
            if progress is not None:
                progress(len(queries) + 1)
            hits = dedupe(list(result.get("hits") or []), wanted)[:k]
            got = measure(hits, wanted, k)
            routed = result.get("routed") or []
            # what the reader is served: the routed documents first, then the ranked
            # hits, one slot per document (``measure`` dedupes)
            with_routing = measure([*routed, *hits], wanted, k)
            queries.append({
                "case": key[0], "card_id": key[1], "item_id": row["item"]["id"],
                "phrasing": kind, "question": texts[kind], "ms": elapsed,
                "targets": [t["key"] for t in wanted],
                **{name: got[name] for name in ("recall", "mrr", "hit", "found")},
                "routed_hit": any(matches(h, t) for h in routed for t in wanted)
                if wanted else None,
                "served": {name: with_routing[name] for name in ("recall", "mrr", "hit")},
                **({"error": result["error"]} if result.get("error") else {}),
                "gap_reasons": result.get("gap_reasons") or [],
                **({"hidden": result["hidden"]} if isinstance(result.get("hidden"), int)
                   else {}),
                **({"extra": result["extra"]} if isinstance(result.get("extra"), Mapping)
                   else {}),
                "top": [{"rank": i, "handle": h.get("handle"), "title": h.get("title"),
                         "kind": h.get("kind"), "signals": h.get("signals"),
                         "targets": got["relevant"][i - 1]}
                        for i, h in enumerate(hits, 1)]})
    cases = sorted({q["case"] for q in queries})
    table = {case: {kind: summarize([q for q in queries if q["case"] == case
                                     and q["phrasing"] == kind]) for kind in kinds}
             for case in cases}
    overall = {kind: summarize([q for q in queries if q["phrasing"] == kind]) for kind in kinds}
    served = {kind: summarize([{**q, **q["served"]} for q in queries if q["phrasing"] == kind])
              for kind in kinds}
    for kind in kinds:
        mine = [q for q in queries if q["phrasing"] == kind and q["routed_hit"] is not None]
        overall[kind]["routed_hit"] = (round(sum(bool(q["routed_hit"]) for q in mine)
                                            / len(mine), 4) if mine else None)
        hidden = [q["hidden"] for q in queries if q["phrasing"] == kind and "hidden" in q]
        if hidden:
            overall[kind]["hidden_per_query"] = round(sum(hidden) / len(hidden), 2)
        ms = sorted(q["ms"] for q in queries if q["phrasing"] == kind)
        if ms:
            from scripts.eval_messy import percentile
            overall[kind]["latency_ms"] = {"p50": percentile(ms, 50), "p90": percentile(ms, 90),
                                           "p95": percentile(ms, 95)}
    return {"k": k, "phrasings": list(kinds), "overall": overall, "by_set": table,
            "served": served, "queries": queries}


def table_text(doc: Mapping[str, Any]) -> str:
    kinds = doc["phrasings"]
    lines = [f"{'set':<22}" + "".join(f"{kind:>18}" for kind in kinds),
             f"{'':<22}" + "".join(f"{'R@' + str(doc['k']) + ' / MRR':>18}" for _ in kinds)]

    def cell(entry: Mapping[str, Any]) -> str:
        if entry.get("recall@k") is None:
            return f"{'-':>18}"
        return f"{entry['recall@k']:>10.3f} / {entry['mrr']:.3f}"

    for case, row in doc["by_set"].items():
        lines.append(f"{case:<22}" + "".join(cell(row[kind]) for kind in kinds))
    lines.append(f"{'OVERALL':<22}" + "".join(cell(doc["overall"][kind]) for kind in kinds))
    lines.append(f"{'+ routed (served)':<22}" + "".join(cell(doc["served"][kind])
                                                    for kind in kinds))
    lines.append(f"{'hit@k':<22}" + "".join(
        f"{doc['overall'][kind]['hit@k'] if doc['overall'][kind]['hit@k'] is not None else '-':>18}"
        for kind in kinds))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, search: SearchFn | None = None,
         environ: Mapping[str, str] | None = None) -> int:
    """``search`` (tests): an injected search function (``evidence_label``
    ``injected-transport``)."""
    from scripts import eval_case_cards as cards
    from scripts import eval_messy as messy

    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_retrieval",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--search", default="tools", choices=sorted(BACKENDS),
                        help="tools (the host's read-only search tool) or hybrid "
                             "(Lane D's operating.search, gated as the same reader)")
    parser.add_argument("--tool", default="plx.documents", choices=("plx.documents", "plx.find"))
    parser.add_argument("--host-config", type=Path)
    parser.add_argument("--k", type=int, default=K)
    parser.add_argument("--phrasings", default=",".join(PHRASINGS))
    parser.add_argument("--items", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--variants", type=Path, default=messy.VARIANTS_PATH)
    parser.add_argument("--transcripts", type=Path, nargs="+",
                        help="measure the agent's own searches in these recorded card runs "
                             "instead of searching (no host; later paths win)")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    kinds = [k.strip() for k in args.phrasings.split(",") if k.strip()]
    host = None
    try:
        if set(kinds) - set(PHRASINGS) or not kinds or args.k < 1:
            raise RetrievalError("phrasings_or_k_invalid")
        sets = cards.card_sets()
        compiled, _ = cards.compile_cards(sets, now=cards._now())
        picked = {i.strip() for i in args.items.split(",") if i.strip()}
        if picked - {r["item"]["id"] for r in compiled}:
            raise RetrievalError("items_not_compiled")
        rows = [r for r in compiled if not picked or r["item"]["id"] in picked]
        rows = rows[:args.limit] if args.limit > 0 else rows
        variants = messy.by_card(messy.load(args.variants))
        registry = json.loads(cards.REGISTRY_PATH.read_text("utf-8")).get("rows") or []
        by_sha, sha_of = registry_handles(registry)
        catalog = catalog_documents(json.loads(CATALOG_PATH.read_text("utf-8")))
        if args.transcripts:
            doc = {"format": FORMAT, "evidence_label": "recorded-transcripts",
                   "search": "agent", "k": args.k,
                   **agent_searches(args.transcripts, rows, by_sha=by_sha, sha_of=sha_of,
                                    catalog=catalog, k=args.k)}
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", "utf-8")
            print(json.dumps({key: doc[key] for key in ("first", "first_served", "any_search_hit",
                                                         "any_routed")}, indent=1))
            return 0
        if search is not None:
            label, backend = "injected-transport", "injected"
        else:
            # Every backend reads as the tools host's reader, and the run carries that
            # host's evidence label: a backend is never measured ungated.
            make = BACKENDS[args.search]
            host = live_host(environ, args.host_config or Path(
                environ.get("PLX_WORKBENCH_HOST_CONFIG") or ""))
            label = "live-captures"
            search = make(host, args.tool)
            backend = f"tools:{args.tool}" if args.search == "tools" else args.search
        doc = evaluate(rows, search, sets=sets, variants=variants, by_sha=by_sha,
                       sha_of=sha_of, catalog=catalog, kinds=kinds, k=args.k,
                       progress=lambda n: n % 50 or print(f"queries: {n}", file=sys.stderr,
                                                          flush=True))
    except (RetrievalError, messy.MessyError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error)[:200]}), file=sys.stderr)
        return 1
    finally:
        if host is not None:
            host.close()
    doc = {"format": FORMAT, "evidence_label": label, "search": backend,
           "cards": len(rows), **doc}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", "utf-8")
    print(table_text(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
