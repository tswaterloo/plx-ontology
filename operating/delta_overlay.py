"""The row-delta overlay a case host reads over its served projection generation.

A delta builder (``ontology/delta.py``) projects only the capture rows that changed since
the served generation (plus their link partners, as context) into a small store of the
same DDL, and publishes it by atomically replacing one pointer file:

``<served store dir>/delta/CURRENT.json`` (or ``$PLX_DELTA_DIR/CURRENT.json``)::

    {"format": "plx-delta/v1", "base_digest": "sha256:...",
     "store": "/abs/path/delta-XXXX.sqlite", "owned": <int>, "built_at": "...Z",
     "layers": [{"store": "/abs/path/delta-XXXX.sqlite", "owned": <int>}, ...]}

``layers`` lists the layer stores newest first (``store`` names the newest); a pointer
without it has the one layer ``store``. A layer file is never rewritten (a new file each
cycle). Its ``delta_owned`` table lists the objects whose rows there REPLACE the served
store's (and an older layer's); every other object in it is context only and is never
served from it. An object is read from the newest layer owning it.

``CaseHostTools`` (``operating/ontology_host.py``) opens the overlay (``open_overlay``)
when the pointer file changes, only when ``base_digest`` and every layer's own
``content_digest`` and ``schema_digest`` equal the served store's (one that does not voids
the whole pointer), and routes:

* ``plx.object`` of an owned object id to the tools of the newest layer owning it;
* ``plx.find`` to the served store, then ``merge_find``: the served answer's candidates of
  objects any layer owns are dropped, and each layer's answer adds the candidates of the
  objects it serves (owned there and by no newer layer), newest layer first.

The overlay's ``OntologyTools`` read under the very policy object of the call, with the
host's item gate, so every overlay answer has passed the same audience, marking and item
checks as a served one. Any failure to open or read the overlay serves the base answer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Any, Mapping

FORMAT = "plx-delta/v1"
DIR_ENV = "PLX_DELTA_DIR"
POINTER = "CURRENT.json"
MAX_POINTER_BYTES = 64 * 1024
MAX_CANDIDATES = 50   # plx.find output contract: candidates maxItems
MAX_GAPS = 20         # plx.find output contract: gaps maxItems
_MERGEABLE = frozenset({"found", "ambiguous", "not_found"})
_UNMERGED_GAP = {"state": "stale", "reason": "delta_overlay_unmerged",
                 "subject": "plx.find",
                 "detail": "records changed since the served projection generation could "
                           "not be merged into this answer; it reads that generation only"}
# A pointer published over the Postgres base (``ontology.delta``, ``partial``) lists only
# the newest layers; the changes of the layers it dropped are in Postgres alone. A reader
# of the SQLite generation and this overlay (Postgres unreachable or not serving) answers
# with this gap: never silently older.
PARTIAL = "delta_overlay_partial"
_PARTIAL_GAP = {"state": "stale", "reason": PARTIAL,
                "detail": "the changes since the served projection generation are held in "
                          "Postgres, which this answer could not read; it reads only the "
                          "newest delta layers, so a record may be older than it is"}


def pointer_path(store_path: str | Path) -> Path:
    """The overlay pointer file of the store served at ``store_path``."""
    configured = os.environ.get(DIR_ENV)
    folder = Path(configured) if configured else Path(store_path).parent / "delta"
    return folder / POINTER


def pointer_stamp(path: Path) -> tuple | None:
    """The pointer file's state now (device, inode, mtime, size), or None when missing.
    The pointer is replaced by a rename each cycle, so any new one changes it."""
    try:
        found = os.stat(path)
    except OSError:
        return None
    return (found.st_dev, found.st_ino, found.st_mtime_ns, found.st_size)


def read_pointer(path: Path) -> dict[str, Any] | None:
    """The pointer's fields when it is a well-formed ``plx-delta/v1`` pointer naming an
    absolute store path; else None (an unknown or broken pointer is never followed)."""
    try:
        if path.stat().st_size > MAX_POINTER_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        return None
    digest, store = value.get("base_digest"), value.get("store")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return None
    if not isinstance(store, str) or not os.path.isabs(store):
        return None
    layers = value.get("layers")
    if layers is None:
        layers = [{"store": store}]
    if not isinstance(layers, list) or not layers or not all(
            isinstance(layer, dict) and isinstance(layer.get("store"), str)
            and os.path.isabs(layer["store"]) for layer in layers) \
            or layers[0]["store"] != store:
        return None   # a layer list that does not name the newest store first
    return dict(value, layers=[layer["store"] for layer in layers])


class Layer:
    """One opened delta store: its reader, its tools, the ids it owns and the ids it
    serves (those it owns that no newer layer owns)."""

    def __init__(self, reader: Any, tools: Any, owned: frozenset[str]) -> None:
        self.reader, self.tools, self.owned = reader, tools, owned
        self.serves = owned
        self.evidence: Any = None   # its plx.identity/plx.documents (the host builds them)
        self._traverse: Any = None

    @property
    def traverse(self) -> Any:
        """Its plx.around/plx.timeline (``TraverseTools`` over its tools), with the layer's
        store facts (``TraverseTools.prime``: every audience, gated handle and event type
        the layer holds, read for every reader alike) loaded from beside the layer file
        when kept there (the delta cycle keeps them as it publishes the layer, a warm
        host when it first opens it), else read off the request path by worker
        processes and kept for every later process: a layer of millions of rows took a
        new host's first plx.around five minutes on one thread."""
        if self._traverse is None:
            from .ontology_tools_evidence import STORE_DOCUMENT_FAMILIES
            from .ontology_tools_traverse import TraverseTools
            traverse = TraverseTools(self.tools)
            traverse.prime_lazily(cell_families=STORE_DOCUMENT_FAMILIES)
            self._traverse = traverse
        return self._traverse

    def readable(self, actor: Any, object_ids: Any) -> set[str]:
        """Those of ``object_ids`` this layer serves that ``actor`` may read now under
        its tools' policy (audience, markings, a current version) and item gate."""
        ids = sorted({oid for oid in object_ids if oid in self.serves})
        if not ids:
            return set()
        tools = self.tools
        with tools.store.lock:
            scope = tools._scope(actor, tools._as_known_at({}))
            versions = tools.store.visible_versions(scope, ids)
        refused = {row["object_id"] for row in versions
                   if not tools._item_allowed(actor, row["l1_handle"])}
        return {row["object_id"] for row in versions} - refused

    def close(self) -> None:
        try:
            self.reader.conn.close()
        except Exception:  # noqa: BLE001 - closing a broken connection is no failure
            pass


class Overlay:
    """The pointer's layers, newest first: an object is read from the newest layer
    owning it (``owner``); ``owned``: every layer's owned ids."""

    def __init__(self, layers: list[Layer], stamp: tuple, *, partial: bool = False) -> None:
        self.layers, self.stamp = layers, stamp
        self.partial = partial   # the pointer's ``partial``: answers say so (``PARTIAL``)
        serves, self._owner, self.owned = _ownership(tuple(layer.owned for layer in layers))
        for layer, served in zip(layers, serves):
            layer.serves = served

    def owner(self, object_id: Any) -> Layer | None:
        index = self._owner.get(object_id) if isinstance(object_id, str) else None
        return None if index is None else self.layers[index]

    def audience_keys(self) -> list[str]:
        return sorted({key for layer in self.layers for key in layer.reader.audience_keys()})

    def close(self) -> None:
        for layer in self.layers:
            layer.close()


# Which layer serves each owned object, kept per list of layers' owned sets (the very
# set objects ``_LAYER_FACTS`` keeps per layer file): every host of this process derives
# the same from the same layers, and with hundreds of thousands of owned ids that cost a
# new session a quarter of a second before its first find.
_OWNERSHIP: list[tuple[tuple[frozenset[str], ...], Any]] = []
_OWNERSHIP_LOCK = threading.Lock()
_OWNERSHIP_KEPT = 4


def _ownership(owned: tuple[frozenset[str], ...]
               ) -> tuple[tuple[frozenset[str], ...], dict[str, int], frozenset[str]]:
    """Per layer (newest first) the ids it serves (owned there and by no newer layer),
    each served id's layer index, and every owned id."""
    with _OWNERSHIP_LOCK:
        for key, value in _OWNERSHIP:
            if len(key) == len(owned) and all(a is b for a, b in zip(key, owned)):
                return value
    newer: set[str] = set()
    serves: list[frozenset[str]] = []
    index: dict[str, int] = {}
    for position, ids in enumerate(owned):
        served = ids - newer
        newer |= ids
        serves.append(served)
        index.update(dict.fromkeys(served, position))
    value = (tuple(serves), index, frozenset(newer))
    with _OWNERSHIP_LOCK:
        _OWNERSHIP.insert(0, (owned, value))
        del _OWNERSHIP[_OWNERSHIP_KEPT:]
    return value


def open_overlay(pointer: Path, stamp: tuple, base: Any) -> Overlay | None:
    """The overlay ``pointer`` names over the served tools ``base``, or None when there is
    none to apply (no pointer, an unknown format, another generation's delta, a layer of
    another schema or store format: the whole pointer is ignored). Raises only on an
    unreadable layer store."""
    fields = read_pointer(pointer)
    if fields is None:
        return None
    with base.store.lock:
        served = base.store.meta()
    if not served.get("content_digest") or fields["base_digest"] != served["content_digest"]:
        return None   # a stale overlay of an older generation (or an unidentified base)
    layers: list[Layer] = []
    try:
        for store in fields["layers"]:
            layer = _open_layer(store, served, base)
            if layer is None:
                for opened in layers:
                    opened.close()
                return None
            layers.append(layer)
    except Exception:
        for opened in layers:
            opened.close()
        raise
    return Overlay(layers, stamp, partial=fields.get("partial") is True)


def with_partial_gap(result: dict[str, Any], subject: str) -> dict[str, Any]:
    """``result`` saying the overlay it read is partial (``PARTIAL``), not complete; an
    error, or one with no room for the gap, as it is."""
    marked = with_gap(result, dict(_PARTIAL_GAP, subject=subject)) \
        if isinstance(result, dict) else None
    return result if marked is None else marked


# What every host of this process reads of one layer file (its owned ids and audience
# keys), kept per file identity: a layer file is never rewritten, and every host opens
# every layer again whenever the pointer changes (each delta cycle).
_LAYER_FACTS: dict[tuple, tuple[frozenset[str], tuple[str, ...]]] = {}
_LAYER_FACTS_LOCK = threading.Lock()
_LAYER_FACTS_KEPT = 256


def _layer_identity(store: str) -> tuple | None:
    """The layer file's identity (device, inode, size, change time, and its WAL's size);
    None when it cannot be read (its facts are then read, never kept)."""
    try:
        found = os.stat(store)
        try:
            wal = os.stat(store + "-wal").st_size
        except FileNotFoundError:
            wal = 0
    except OSError:
        return None
    return (store, found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns, wal)


def _open_layer(store: str, served: Mapping[str, str], base: Any) -> Layer | None:
    from .ontology_tools import OntologyTools, StoreReader
    reader = StoreReader.open(store)
    try:
        meta = reader.meta()
        if (meta.get("content_digest") != served["content_digest"]
                or meta.get("schema_digest") != served.get("schema_digest")
                or reader.format_problem() is not None):
            reader.conn.close()
            return None
        identity = _layer_identity(store)
        with _LAYER_FACTS_LOCK:
            kept = _LAYER_FACTS.get(identity) if identity is not None else None
        if kept is None:
            owned = frozenset(row[0] for row in reader.conn.execute(
                "SELECT object_id FROM delta_owned"))
        else:
            owned = kept[0]
            reader.remember_audience_keys(kept[1])
        tools = OntologyTools(
            reader, base.policy, item_gate=base.item_gate, schema=base.schema,
            grammar=base.grammar, contracts=base.contracts, clock=base.clock,
            sources=None,   # never a live read of its own: the served answer makes it
            aliases=base.aliases, reviews=base.reviews, unconfigured=base.unconfigured,
            freshness_bounds=base.freshness_bounds, freshness_clock=base.freshness_clock)
        # One set registry: a handle a layer's plx.around makes is a root for the host.
        tools.sets = base.sets
        keys = reader.audience_keys()   # read now, not on the first reader's call
        if kept is None and identity is not None and identity == _layer_identity(store):
            with _LAYER_FACTS_LOCK:
                if len(_LAYER_FACTS) >= _LAYER_FACTS_KEPT:
                    _LAYER_FACTS.clear()
                _LAYER_FACTS[identity] = (owned, tuple(keys))
    except Exception:
        reader.conn.close()
        raise
    return Layer(reader, tools, owned)


def _object_id(candidate: Any) -> str | None:
    if isinstance(candidate, Mapping):
        ref = candidate.get("object")
        if isinstance(ref, Mapping) and isinstance(ref.get("object_id"), str):
            return ref["object_id"]
    return None


def _status(count: int) -> str:
    return "not_found" if not count else "found" if count == 1 else "ambiguous"


def mergeable(base: Any) -> bool:
    """Whether ``merge_find`` can change ``base``, a served ``plx.find`` answer: one with
    candidates and a status a layer's candidates merge into; else (an error, an entity
    resolution, a home, portfolio or use-case card) the served answer stands and no
    layer needs asking."""
    return isinstance(base, Mapping) and "error" not in base \
        and base.get("status") in _MERGEABLE \
        and not any(key in base for key in ("home", "portfolio", "use_case")) \
        and isinstance(base.get("candidates"), list)


def merge_find(base: dict[str, Any], answers: list[tuple[Any, frozenset[str]]],
               owned: frozenset[str], arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """``base`` (the served store's ``plx.find`` answer) with the overlay's owned objects in
    place of the served ones: None when nothing changes (serve ``base``). ``answers``: per
    layer, newest first, the same arguments' answer from its tools and the ids it serves
    (``Layer.serves``); any error or unusable answer: None, the served answer stands.
    ``owned``: every layer's owned ids. The caller validates the result against the
    output contract and falls back to ``with_unmerged_gap(base)``."""
    if not mergeable(base):
        return None
    candidates = base["candidates"]
    if not answers or not all(
            isinstance(answer, dict) and "error" not in answer
            and answer.get("status") in _MERGEABLE
            and isinstance(answer.get("candidates"), list) for answer, _ in answers):
        return None   # a layer that could not answer: the served answer stands
    first_page = arguments.get("cursor") is None
    kept = [c for c in candidates if _object_id(c) not in owned]
    added: list[dict[str, Any]] = []
    source: Any = None   # the newest layer answer that added a candidate
    if first_page:
        seen = {_object_id(c) for c in kept}
        for answer, serves in answers:
            for candidate in answer["candidates"]:
                oid = _object_id(candidate)
                if oid in serves and oid not in seen and not candidate.get("repeated"):
                    seen.add(oid)
                    added.append(dict(candidate))
                    source = source if source is not None else answer
    dropped = len(candidates) - len(kept)
    if not dropped and not added:
        return None
    result = {key: value for key, value in base.items() if key != "set"}
    completeness = dict(base.get("completeness") or {})
    # A layer that cut its own answer (a cap, a budget, a next page it holds) leaves the
    # merged answer incomplete too: its matches past the cut are not in it.
    layer_cut = [answer.get("completeness") or {} for answer, _ in answers
                 if (answer.get("completeness") or {}).get("complete") is not True
                 or (answer.get("completeness") or {}).get("truncated")
                 or (answer.get("completeness") or {}).get("next_cursor") is not None]
    exact = (completeness.get("complete") is True and not completeness.get("truncated")
             and completeness.get("next_cursor") is None and first_page and not layer_cut)
    if base.get("status") == "not_found" and added:
        # The served answer's gaps say why nothing matched there; the overlay's say how
        # its own (owned) matches were read.
        result["gaps"] = list(source.get("gaps") or [])
    merged = kept + added
    cut = False
    limit = arguments.get("limit")
    room = MAX_CANDIDATES if not isinstance(limit, int) else max(min(limit, MAX_CANDIDATES),
                                                                  len(kept))
    if len(merged) > room:
        merged, cut = merged[:room], True
    if first_page:
        merged = [dict(c, rank=index) for index, c in enumerate(merged, start=1)]
    result["candidates"] = merged
    completeness["returned"] = len(merged)
    if exact and not cut:
        completeness["total"] = len(merged)
        result["status"] = _status(len(merged))
    else:
        completeness["total"] = None
        result["status"] = _status(len(merged)) if base.get("status") == "not_found" \
            or not merged else ("ambiguous" if len(merged) > 1
                                or base.get("status") == "ambiguous" else "found")
    if result["status"] == "not_found" and not result.get("gaps"):
        # Every served match was an owned object the delta no longer matches: the
        # overlay's own not-found gaps say what was searched, else a plain one.
        result["gaps"] = next((list(answer["gaps"]) for answer, _ in answers
                               if answer["status"] == "not_found" and answer.get("gaps")), [])
        if not result["gaps"]:
            query = arguments.get("query")
            result["gaps"] = [{"state": "unknown", "reason": "no_visible_match",
                               "subject": (f"query:{' '.join(query.split())}"
                                           if isinstance(query, str) and query.strip()
                                           else "plx.find")[:700],
                               "detail": "nothing matched among the records this reader can "
                                         "read now; absence from the source is not "
                                         "established"}]
    if cut:
        completeness.update(complete=False, truncated=True, reason="page_cap")
    elif layer_cut:
        cutting = layer_cut[0]
        completeness.update(complete=False, truncated=bool(cutting.get("truncated")) or
                            completeness.get("truncated", False))
        if cutting.get("reason") is not None:
            completeness["reason"] = cutting["reason"]
    result["completeness"] = completeness
    return result


HOPS_DEFERRED = "delta_overlay_hops_deferred"
NOT_AGGREGATED = "delta_overlay_not_aggregated"


def with_gap(result: dict[str, Any], gap: Mapping[str, Any]) -> dict[str, Any] | None:
    """``result`` with ``gap`` appended and its completeness not complete (reason: the
    gap's unless it had one); None when it has no room for it or is an error."""
    gaps = result.get("gaps")
    if "error" in result or not isinstance(gaps, list) or len(gaps) >= MAX_GAPS:
        return None
    out = dict(result, gaps=gaps + [dict(gap)])
    completeness = result.get("completeness")
    if isinstance(completeness, Mapping):
        out["completeness"] = dict(completeness, complete=False,
                                   reason=completeness.get("reason") or gap["reason"])
    return out


def with_unmerged_gap(base: dict[str, Any], subject: str = "plx.find"
                      ) -> dict[str, Any] | None:
    """``base`` saying it reads the served generation only (records changed since it could
    not be merged); None when it has no room for the gap (serve ``base`` as it is)."""
    gaps = base.get("gaps")
    if not isinstance(gaps, list) or len(gaps) >= MAX_GAPS or "error" in base:
        return None
    out = dict(base, gaps=gaps + [dict(_UNMERGED_GAP, subject=subject)])
    completeness = base.get("completeness")
    if isinstance(completeness, Mapping):
        out["completeness"] = dict(completeness, complete=False,
                                   reason=completeness.get("reason") or "delta_overlay_unmerged")
    return out


__all__ = ["DIR_ENV", "FORMAT", "HOPS_DEFERRED", "NOT_AGGREGATED", "PARTIAL", "Layer",
           "Overlay", "merge_find", "open_overlay", "with_gap", "with_partial_gap",
           "pointer_path", "pointer_stamp", "read_pointer", "with_unmerged_gap"]
