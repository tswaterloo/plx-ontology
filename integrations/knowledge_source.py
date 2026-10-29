"""Brain knowledge items as a namespace-trimmed L1 capture (GOV-13).

Which knowledge service this reuses (evidence, 2026-10-05):

* The Portal Knowledge Service (``plx-customer-portal@790256c``
  ``portal/src/lib/knowledge/service/``) is an in-process library facade with no Workbench
  route: ``read.ts:55-112`` serves ``knowledge.hub.public`` from a bundled seed,
  ``knowledge.agent.*`` from Portal UAT agent-run patterns (``triple-t/brain/search.ts:155``)
  and ``knowledge.graph`` from the brain's key-authed ``/api/vmc/knowledge/agent/*`` routes
  (``vmc-client.ts:1-9``), gated by ``namespaces.ts:22-31`` (STAFF+, an agent namespace
  only when the caller lists it, ``personal/`` never). The reporting API serves none of
  it: its 93-relation allowlist (``services/workbench-api/manifest/allowlist.json:17``)
  holds no knowledge, gap or agent relation. So the graph namespace is read here from the
  same upstream the Portal reads, the brain, through the reviewed GET-only
  ``integrations.vmc.BrainReadClient`` (one route: ``agent/items`` through
  ``integrations.vmc.items_page``, which, unlike ``agent/search``, bumps no access
  counter). No embedding store, no write, no Neo4j.
* Brain items carry ``namespaceKey`` (agentic-swarm ``apps/vmc-web/src/lib/vmc/knowledge/
  items-core.ts:70-91``); the brain has no per-employee ACL and every key reads the whole
  non-personal corpus, so the Workbench
  owns the authority.

Authority (caller/namespace, decided before retrieval). A :class:`NamespaceAuthority`
document (``plx-knowledge-namespaces/v1``) maps namespace patterns (an exact
``namespaceKey`` or a trailing-``*`` prefix; the most specific wins, so an exact entry
with no principals denies one namespace inside a granted family) to principals. A
capture names the namespaces it reads; each must be known (a configured pattern matches
it) and granted to the capture actor, else the capture fails closed before any request
(``knowledge_namespace_unknown`` / ``_denied`` / ``_personal``). Personal namespaces
(the brain's own classifier, ``namespace-scope.ts:58-90``) are never configurable and
never stored. Of what the brain returns, only items of a requested namespace the actor
may read are stored. The same document grants readers the ``brain:<namespaceKey>``
audience units of the projection (:meth:`NamespaceAuthority.grants`), so ``plx.find``
trims per reader in SQL before ranking; a namespace no pattern names grants nobody.

Capture (L1). Kind ``brain`` in ``integrations.capture_store`` (one versioned file under
``<state>/knowledge-capture/``): record path ``(namespaceKey, item id)``, cited
``brain://<namespaceKey>/<id>@<generation>-<capture id>``, audience unit and resource
``brain:<namespaceKey>``, lineage ``brain``, source version the item's ``updatedAt``.
Offset pages of ``agent/items`` (``order=recent``) are not an atomic snapshot: an item
edited mid-read moves to the front, past the offset already read. A unit's run is
``complete`` only when the listing reached the brain's ``total``, the total did not move
and, for a multi-page read, the first page re-read at the end is unchanged (ids and
``updatedAt``); otherwise it is ``partial`` and nothing is marked missing.

This module is the only brain-item capture path: ``integrations.mc_sources`` keeps
``mc_search_knowledge`` hits only.

Trust: every item is derived engineering/agent knowledge, an untrusted T3 lead
(``integrations.vmc.UNTRUSTED_LEAD``), never a business fact; the ontology projects it as
``KnowledgeItem`` with no links to business records.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

from integrations.capture_store import (CaptureStore, CapturedRecord, SourceKind,
                                        register_source_kind)
from integrations.http import SourceUnavailable
from integrations.live_source_guard import ReadOnlyGuardError
from integrations.plx_hub_guard import UpstreamError
from integrations.vmc import items_page
from ontology.types import Actor

if __name__ == "__main__":
    # ``python -m`` runs this file as ``__main__``; the probe's ontology imports load it
    # again as ``integrations.knowledge_source``, and two copies would register two
    # ``brain`` kinds (``source_kind_conflict``). Run the one registered copy instead.
    from integrations.knowledge_source import main as _main
    sys.exit(_main())

SCHEMA = "plx-knowledge-namespaces/v1"
SOURCE = "brain"
LINEAGE = "brain"
STORE_DIR = "knowledge-capture"
STORE_FILE = "capture.db"
PAGE_SIZE = 100
MAX_ITEMS = 5000
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NAMESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,199}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
# Personal roots after the brain strips one leading ``swarm/`` (namespace-scope.ts:58-90).
_PERSONAL_ROOTS = ("personal", "personal-notes")


class NamespaceDenied(Exception):
    """A namespace the actor may not read; ``code`` is stable."""

    def __init__(self, code: str, namespace: str) -> None:
        super().__init__(f"{code}: {namespace}")
        self.code = code
        self.namespace = namespace


def is_personal(namespace: str) -> bool:
    """The brain's own classifier (one leading ``swarm/`` stripped), case-folded so a
    case variant fails closed too."""
    key = namespace.strip().lower()
    key = key[len("swarm/"):] if key.startswith("swarm/") else key
    return any(key == root or key.startswith(root + "/") for root in _PERSONAL_ROOTS)


def _may_cover_personal(pattern: str) -> bool:
    """Whether a configured pattern could name a personal namespace, case-folded like
    :func:`is_personal` so ``Swarm/Personal/*`` is refused at load as ``swarm/personal/*``."""
    if not pattern.endswith("*"):
        return is_personal(pattern)
    stem = pattern[:-1].lower()
    roots = [r for root in _PERSONAL_ROOTS for r in (root, "swarm/" + root)]
    return any(root.startswith(stem) or stem.startswith(root) and is_personal(stem + "x")
               for root in roots)


def _check_pattern(pattern: Any) -> str:
    body = pattern[:-1] if isinstance(pattern, str) and pattern.endswith("*") else pattern
    if not isinstance(body, str) or not body or _NAMESPACE.fullmatch(body) is None:
        raise ValueError("knowledge_namespace_pattern_invalid")
    if _may_cover_personal(pattern):
        raise ValueError("knowledge_namespace_personal")
    return pattern


class NamespaceAuthority:
    """Namespace pattern -> principals for one tenant; default deny."""

    def __init__(self, namespaces: Mapping[str, Iterable[str]], *, tenant: str) -> None:
        if not isinstance(tenant, str) or not tenant.strip():
            raise ValueError("knowledge_authority_tenant_required")
        if not isinstance(namespaces, Mapping):
            raise ValueError("knowledge_authority_invalid")
        self.tenant = tenant.strip()
        self._grants: dict[str, frozenset[str]] = {}
        for pattern, principals in namespaces.items():
            _check_pattern(pattern)
            if isinstance(principals, (str, bytes)) or not isinstance(principals, Iterable):
                raise ValueError("knowledge_authority_principals_invalid")
            values = frozenset(principals)
            if any(not isinstance(p, str) or not p.strip() or "*" in p for p in values):
                raise ValueError("knowledge_authority_principal_invalid")
            self._grants[pattern] = values

    @classmethod
    def from_json(cls, document: Any) -> "NamespaceAuthority":
        if (not isinstance(document, dict) or document.get("schema") != SCHEMA
                or set(document) - {"schema", "tenant", "namespaces", "note"}
                or not isinstance(document.get("namespaces"), dict)
                or not all(isinstance(v, list) for v in document["namespaces"].values())):
            raise ValueError("knowledge_authority_document_invalid")
        return cls(document["namespaces"], tenant=document.get("tenant"))

    @classmethod
    def from_file(cls, path: str | Path) -> "NamespaceAuthority":
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))

    def principals_for(self, namespace: str) -> frozenset[str] | None:
        """Principals of the most specific pattern naming ``namespace``; None if unknown."""
        if namespace in self._grants:
            return self._grants[namespace]
        best: tuple[int, frozenset[str]] | None = None
        for pattern, principals in self._grants.items():
            if pattern.endswith("*") and namespace.startswith(pattern[:-1]):
                if best is None or len(pattern) > best[0]:
                    best = (len(pattern), principals)
        return None if best is None else best[1]

    def authorize(self, actor: Any, namespace: Any) -> None:
        """Raise :class:`NamespaceDenied` unless ``actor`` may read ``namespace``."""
        if not isinstance(namespace, str) or not namespace:
            raise NamespaceDenied("knowledge_namespace_unknown", str(namespace))
        if is_personal(namespace.rstrip("*")):
            raise NamespaceDenied("knowledge_namespace_personal", namespace)
        principals = self.principals_for(namespace)
        if principals is None:
            raise NamespaceDenied("knowledge_namespace_unknown", namespace)
        if (not isinstance(actor, Actor) or actor.tenant != self.tenant
                or not principals & actor.principals):
            raise NamespaceDenied("knowledge_namespace_denied", namespace)

    def readable(self, actor: Any, namespace: Any) -> bool:
        try:
            self.authorize(actor, namespace)
        except NamespaceDenied:
            return False
        return True

    def allows(self, actor: Any, resource: str) -> bool:
        """Capture-store audience check: resource ``brain:<namespaceKey>``."""
        prefix, sep, namespace = str(resource).partition(":")
        return prefix == SOURCE and bool(sep) and self.readable(actor, namespace)

    def grants(self, actor: Any, audience_keys: Iterable[str]) -> dict[str, frozenset[str]]:
        """``brain:<namespaceKey>`` audience grants for the tools' ``ReadPolicy``: the
        actor's principals where it may read the namespace, else none (exact keys)."""
        principals = frozenset(actor.principals) if isinstance(actor, Actor) else frozenset()
        return {key: principals if self.allows(actor, key) else frozenset()
                for key in sorted(set(audience_keys)) if key.startswith(SOURCE + ":")}


# ---------------------------------------------------------------- capture kind

def _path(parts: Sequence[str]) -> tuple[str, ...]:
    if isinstance(parts, (str, bytes)) or len(parts) != 2:
        raise ValueError("brain_path_invalid")
    namespace, item = parts
    if (not isinstance(namespace, str) or _NAMESPACE.fullmatch(namespace) is None
            or is_personal(namespace) or not isinstance(item, str)
            or _UUID.fullmatch(item.lower()) is None):
        raise ValueError("brain_path_invalid")
    return namespace, item.lower()


BRAIN_KIND = register_source_kind(SourceKind(
    name=SOURCE, scheme="brain", path_parts=2, canonical_path=_path,
    unit=lambda path: f"{SOURCE}:{path[0]}", resources=lambda path: (f"{SOURCE}:{path[0]}",),
    key=lambda path: {"id": path[1]}))


def store_path(state_dir: str | Path) -> Path:
    return Path(state_dir).expanduser() / STORE_DIR / STORE_FILE


def store_exists(state_dir: str | Path) -> bool:
    return store_path(state_dir).is_file()


def _matches(namespace: str, requested: Sequence[str]) -> bool:
    return any(namespace == p or p.endswith("*") and namespace.startswith(p[:-1])
               for p in requested)


def _row(item: Mapping[str, Any]) -> dict[str, Any]:
    tags = item.get("tags")
    return {"id": item["id"].lower(), "namespace": item["namespaceKey"],
            "title": item.get("title"), "snippet": item.get("snippet"),
            "memory_type": item.get("memoryType"), "project_slug": item.get("projectSlug"),
            "source_type": item.get("sourceType"), "external_url": item.get("externalUrl"),
            "tags": [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [],
            "created_at": item.get("createdAt"), "updated_at": item.get("updatedAt")}


def _page(client: Any, offset: int, limit: int,
          counts: dict[str, int]) -> tuple[list[Any], int]:
    response = items_page(client, offset=offset, limit=limit)
    counts["requests"] += 1
    data = response.get("data") if isinstance(response, Mapping) else None
    if not isinstance(data, Mapping) or data.get("enabled") is False:
        raise SourceUnavailable("brain items unavailable")
    rows, total = data.get("items"), data.get("total")
    if not isinstance(rows, list) or not isinstance(total, int) or isinstance(total, bool):
        raise SourceUnavailable("brain items response shape invalid")
    return rows, total


def _signature(rows: list[Any], total: int) -> tuple[Any, ...]:
    return (total, *((r.get("id"), r.get("updatedAt")) if isinstance(r, Mapping) else None
                     for r in rows))


def capture(client: Any, store: CaptureStore, authority: NamespaceAuthority, actor: Actor,
            namespaces: Sequence[str], *, max_items: int = MAX_ITEMS,
            page_size: int = PAGE_SIZE) -> dict[str, Any]:
    """Read ``agent/items`` and keep the items of ``namespaces`` the actor may read.

    Every requested namespace is authorized first; a refusal raises
    :class:`NamespaceDenied` before any request. A source failure raises before anything
    is stored. A listing that reached the brain's ``total`` without the total or (re-read
    at the end of a multi-page read) the first page moving is ``complete`` for every
    granted, requested namespace, also one captured before that now holds nothing (its
    records are marked missing). Returns counts only."""
    requested = list(dict.fromkeys(namespaces))
    if not requested:
        raise NamespaceDenied("knowledge_namespace_unknown", "")
    for namespace in requested:
        authority.authorize(actor, namespace)
    counts = {"requests": 0, "items_read": 0, "kept": 0, "dropped_personal": 0,
              "dropped_unrequested": 0, "withheld": 0, "invalid": 0}
    kept: dict[str, dict[str, CapturedRecord]] = {}
    seen: set[str] = set()
    offset, reached, first, total = 0, False, None, None
    while offset < max_items:
        limit = min(page_size, max_items - offset)
        rows, total = _page(client, offset, limit, counts)
        first = (limit, _signature(rows, total)) if first is None else first
        for item in rows:
            counts["items_read"] += 1
            if (not isinstance(item, Mapping) or item.get("kind") != "memory"
                    or not isinstance(item.get("id"), str)
                    or not isinstance(item.get("namespaceKey"), str)
                    or not isinstance(item.get("updatedAt"), str)):
                counts["invalid"] += 1
                continue
            namespace = item["namespaceKey"]
            if is_personal(namespace):
                counts["dropped_personal"] += 1
                continue
            if not _matches(namespace, requested):
                counts["dropped_unrequested"] += 1
                continue
            if not authority.readable(actor, namespace):
                counts["withheld"] += 1
                continue
            try:
                path = BRAIN_KIND.canonical_path((namespace, item["id"]))
            except ValueError:
                counts["invalid"] += 1
                continue
            if path[1] in seen:  # offset pages shift under concurrent writes
                continue
            seen.add(path[1])
            kept.setdefault(namespace, {})[path[1]] = CapturedRecord(
                path=path, payload={"row": _row(item)}, source_version=item["updatedAt"],
                lineage=LINEAGE)
            counts["kept"] += 1
        offset += len(rows)
        if not rows or offset >= total:
            reached = True
            break
    complete = reached and total == first[1][0]
    if complete and counts["requests"] > 1:
        # An edit during the read moves its item to the front (order=recent), past the
        # pages already read: the front re-read at the end must be unchanged.
        rows, total = _page(client, 0, first[0], counts)
        complete = _signature(rows, total) == first[1]
    reason = None if complete else ("source_changed_during_read" if reached else "item_cap")
    units = {f"{SOURCE}:{namespace}": records for namespace, records in kept.items()}
    if complete:
        for unit in store.units(SOURCE):
            namespace = unit.partition(":")[2]
            if (unit not in units and _matches(namespace, requested)
                    and authority.readable(actor, namespace)):
                units[unit] = {}
    for unit, records in sorted(units.items()):
        run = store.begin_run(SOURCE, unit, [unit])
        store.save_page(run["run_id"], list(records.values()), None)
        store.finish_run(run["run_id"], "complete" if complete else "partial", reason=reason)
    return {**counts, "complete": complete, "brain_total": total,
            "units": len(units), "namespaces_requested": len(requested),
            # The ontology relation (source family brain) this run holds records of: a
            # passing check names it, so the source registry counts it evaluated.
            "relations": {"items": counts["kept"]} if counts["kept"] else {}}


# ---------------------------------------------------------------- live probe

def _probe(args: argparse.Namespace, client: Any = None) -> dict[str, Any]:
    from integrations.plx_hub_guard import BrainConfig
    from integrations.vmc import BrainReadClient
    from ontology import schema as schema_loader
    from ontology.project import build_store
    from ontology.project_knowledge import KnowledgeCaptures
    from operating.ontology_tools import OntologyTools, ReadPolicy, StoreReader

    principal = "probe:knowledge"
    actor = Actor("plx", frozenset({principal}))
    authority = NamespaceAuthority({pattern: [principal] for pattern in args.grant},
                                   tenant="plx")
    state = Path(args.state_dir)
    store_path(state).parent.mkdir(parents=True, exist_ok=True)
    if client is None:
        client = BrainReadClient(BrainConfig.from_env(os.environ))
    with CaptureStore(store_path(state), authority) as store:
        report = capture(client, store, authority, actor, args.grant)
    projection = state / "projection.sqlite"
    with KnowledgeCaptures(store_path(state)) as source:
        built = build_store(projection, schema_loader.load_schema(), {SOURCE: source})
    reader = StoreReader.open(projection)
    keys = [k for k in reader.audience_keys() if k.startswith(SOURCE + ":")]
    report["projected_objects"] = built["counts"].get("objects", 0)
    report["all_units_granted"] = all(authority.allows(actor, k) for k in keys)
    for label, reader_actor in (("granted", actor),
                                ("ungranted", Actor("plx", frozenset({"probe:nobody"})))):
        tools = OntologyTools(reader, ReadPolicy("plx", authority.grants(reader_actor, keys)))
        found = tools.call("plx.find", {"query": args.query, "object_type": "KnowledgeItem"},
                           reader_actor)
        report[f"{label}_reader_candidates"] = len(found.get("candidates") or [])
    reader.conn.close()
    return report


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    """The live probe; ``client`` replaces the brain client (tests only)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True, help="fresh directory outside Git")
    parser.add_argument("--grant", action="append", required=True,
                        help="namespace pattern the probe principal reads (repeatable)")
    parser.add_argument("--query", default="lesson", help="plx.find text for the denied reader")
    args = parser.parse_args(argv)
    try:
        report = {"ok": True, **_probe(args, client)}
    except NamespaceDenied as exc:
        report = {"ok": False, "error": exc.code}
    except (SourceUnavailable, UpstreamError, ReadOnlyGuardError) as exc:
        report = {"ok": False, "error": "source_unavailable", "error_class": type(exc).__name__}
    except ValueError as exc:  # configuration (a grant, the authority): a code, no traceback
        code = str(exc.args[0]) if exc.args else ""
        report = {"ok": False, "error": code if _ERROR_CODE.fullmatch(code)
                  else "configuration_invalid", "error_class": type(exc).__name__}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


__all__ = ["BRAIN_KIND", "NamespaceAuthority", "NamespaceDenied", "capture", "is_personal",
           "store_exists", "store_path"]
