"""Dev-set baseline: the oracle's dev items run end to end on the product's generic tools.

Directive f6643c209267 (dev-set pass rates in ``state.status`` and the daily report);
10-DECISION decision 8. Harness: ``evals/baseline.py`` (agent surface, the deterministic
investigation path, the ``eval`` block); run pin: ``evals/runner.py`` ``make_run_pin``; scoring: ``evals/score.py`` (the
claim gate). No model is called: questions the deterministic path has no plan for are
counted ``needs_agent``, never as passes.

Per run:

1. **Product.** A GET-only Portal mirror sync (``integrations.source_mirror``, through
   ``WorkbenchReadApiClient``) of the Portal relations the curated schema binds for the
   items' root object types and every type up to ``--link-depth`` declared links away
   (``reachable_relations``, schema-derived, never a list), plus every relation the items'
   expected values cite (``cited_relations``: a cited relation past the link depth would
   leave its item no capture to pin, ``pin_drift``), into a throwaway case-runtime
   state directory; one projection (``ontology.project.build_store``) of the schema's
   relations in that mirror (the link neighbourhood and, live, the plans'), never of a
   relation mirrored only because the answer key cites it: the key never chooses what the
   product can search (architect-20261011T0821Z-kGaZpgjp), so an item whose evidence lies
   past the link depth is a product gap, measured as one. ``eval.mirror.cited_only`` names
   those relations. A live run mirrors the relations the items' families' plans read first
   (``plan_relations``), then the cited ones; only the plans' decide ``eval.complete``,
   and ``eval.mirror`` names every relation left unfinished (``needed_unfinished`` apart
   from the rest; the rest also puts ``mirror_unplanned_unfinished`` in ``eval.incomplete``,
   which leaves
   ``eval.complete`` as it is). ``--mirror-db``/``--bc-db`` (the verifier session's
   ``{session_state}``): the sync and the BC capture top up those shared stores instead
   of capturing cold, the run copies the mirror once after its sync and projects only its
   own relations and BC entity sets. ``--session-projection`` (the verifier session's
   ``ontology.session_projection``): when this session committed it the run captures
   no BC and projects nothing (``session_product``: the producer's mirror snapshot,
   copied once, its BC capture and its generation, opened read-only); it syncs (phase
   ``pin_sync``) only the cited relations whose last completed capture predates the
   items' ``as_known_at.time``, before opening the generation, so every run pin finds a
   capture and the product reads it; else the run builds its product as above.
2. **Items.** ``fixture``: the frozen dev set ``evals/dev`` (synthetic oracle world,
   ``evals/oracle/fixture_world.py``). ``live``: the oracle generates the day's dev items
   (``scripts.eval_oracle_dev`` live sources, run id ``live-<UTC date>`` of the run's
   wall clock) in memory.
3. **Run.** Every item through ``evals.runner.record`` with ``DeterministicInvestigator``
   on a fresh ``ToolHost`` (case host over a disposable state directory that links the
   run's mirror, never a copy per item, + the ontology tools over the store), its run pin written first (drift-checked against the oracle's own
   reads), scored
   with ``evals.score.score``.

Output (stdout): the report with a top-level ``eval`` block (``dev_set`` x/y, by family,
by failure reason, root resolution, run pins and pin_drift; counts, codes and hashes only, never a
row value, identifier or citation), with ``measure`` and ``vs_manual`` as diagnostics in no
pass rule (``evals.measure``), and ``item_rows``: one ``family-number:root:outcome:reason`` code per
item the oracle drew (``item_row``), unmeasured and blocked ones included, so each
``not_found`` root and ``wrong`` answer can be followed up. ``fixture --write-baseline`` writes
``evals/baseline/dev-fixture.json`` (without wall times) and ``--write-report`` ``devloop/EVAL-BASELINE.md``.
``--transcripts DIR`` (outside the repository) keeps the sealed transcripts and scores.

**A Portal outage blocks parts, not the run** (the landed portal-indep pattern, as in
``scripts/eval_unfamiliar_sample.py``). The product captures BC itself after the Portal sync
(``capture_bc``: the GET-only BC client, on the rest of the build budget, never less than its
floor and never past the run's deadline: ``eval_unfamiliar_sample.bc_budget``) and projects that
capture beside the mirror; captured BC citations are re-read from that capture store for the
scorer (``evals/capture_evidence.py``). When the Portal fails the whole sync (or has no
credentials), every item whose label reads the Portal is ``blocked_upstream`` (counted,
never run, never wrong) and the rest run on the BC (and document-rooted) captures; the
report lists the blocked parts in ``blocked_upstream`` (the oracle's Portal strata and the
product's Portal side), each blocked stratum counting its even share of ``--count`` as
sampled and blocked (``eval.blocked_quota``); ``eval.pass_rule`` is ``portal_outage`` and
``eval.answer`` keeps blocked, wrong, abstained and correct apart (accuracy beside
coverage) for the verifier's tiers.

**One budget, always a record.** ``--total-budget-seconds`` runs from the start. The
oracle's reads stop ``--run-reserve-seconds`` before its end (``BudgetExhausted``: a budget
cut, never an upstream outage), and so do the mirror sync and the product's BC capture: the
reserve is the projection's and the items'. The items stop at the end itself
(``run_budget_exhausted``). A phase no budget reaches (an oracle generation, a mirror copy,
a projection, one long item) is cut by the watchdog ``HARD_STOP_GRACE_SECONDS`` past the end:
it prints the record of what the run measured so far and exits 3, so the verifier records a
partial run instead of a timeout. Every live record carries ``eval.phases`` (seconds per
phase) and ``eval.stopped`` (null, or the phase the budget stopped and why).

**Live partition** (bb664f58910f section 2): a live run drops every dev item whose group
(shared document, root or identifier, ``evals/partition.py``) touches a frozen held-out
set's key digests (``eval.partition.moved_to_heldout``) and leaves its own digest manifest
in the host state directory (``--state-dir``, default the held-out state directory), which
the held-out check reads to keep its set disjoint from every live dev run.

**Held-out split** (``heldout``; 10-DECISION 8, decision 2026-10-01-tools-shape item 7,
ENG-12): the same scorer on the frozen held-out set ``scripts.eval_oracle_dev heldout``
froze (``--run-id``, default ``heldout-v2``; ``--source live`` or ``fixture``). Its items load
only through the runner's held-out load (``evals.runner.load_items`` split ``held_out``:
the credentialed environment, ``PLX_EVAL_HELD_OUT_DIR`` outside Git holding the state
directory, no document, root or identifier shared with a committed dev or exposed item);
the run refuses (exit 3, ``refused`` a code) a set that is not frozen, that differs from
its digest list or was frozen from another source, or that shares a partition key with a
live dev run's manifest. The standard items run on the run's product (live as ``live``
builds it; fixture as ``fixture``). Each metamorphic variant runs on that product's own
mirror replayed perturbed as its item records (``MirrorReplay``: the oracle's own
transform, ``evals.oracle.perturb.portal_port``, served through the GET-only client into a
mirror and projection of its own, its clock moved as the replica time moves): one
perturbed product per perturbation, never a second read of the source. Only the Portal is
perturbed, so a variant whose label reads BC or a document is refused by admission
(``perturbed_reads_not_served``, ``evals.admission.reads_only_portal``) and counted in
``admission_refused``. The run pins read no oracle: each checks the frozen label's value
against the pinned mirror row, so a label the source has since moved is ``pin_drift``.
Output: ``eval.held_out`` (the pass rate) and ``eval.by_family`` over the standard items
only, so a variant can neither inflate nor mask the product's score; ``eval.by_variant``,
``eval.answer`` and the set's ``counts`` and hash; aggregates and codes only, never an
item id, a value, a label or a per-item row. Transcripts (``--transcripts``) go only under
``PLX_EVAL_HELD_OUT_DIR``, their scores redacted. One budget and watchdog as ``live``; a
variant group the budget does not reach is counted (``variant_products``), never run.

Evidence: ``fixture`` is ``isolated-fixture``; ``live`` is ``live-read`` and stays
``live-unverified`` until the live verifier records a pass of
``devloop/live-checks/eval.dev_baseline.json`` (daily cadence); ``heldout --source live``
likewise of ``devloop/live-checks/eval.heldout_product.json``.

Usage::

    python3 -m scripts.eval_dev_baseline fixture [--write-baseline] [--write-report]
    python3 -m scripts.eval_dev_baseline live --work-dir DIR [--count 30]
        [--total-budget-seconds 1800] [--link-depth 2] [--no-bc]
    python3 -m scripts.eval_dev_baseline heldout --work-dir DIR [--source live|fixture]
        [--run-id heldout-v2] [--state-dir DIR] [--total-budget-seconds 1800]
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from evals import baseline as bl
from evals import measure as measuring
from evals import runner
from evals import score as scoring

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "plx-eval-dev-baseline-run/v1"
DEV_DIR = ROOT / "evals" / "dev"
BASELINE_JSON = ROOT / "evals" / "baseline" / "dev-fixture.json"
REPORT_MD = ROOT / "devloop" / "EVAL-BASELINE.md"
TENANT = "plx"
PRINCIPAL = "eval:dev-baseline"
FIXTURE_RUN_ID = "fixture-dev-2026-10-01"
FIXTURE_KEY = "plxwb_" + "f" * 40
FIXTURE_URL = "https://workbench-fixture.invalid"
DEFAULT_LINK_DEPTH = 3
# Past the run's deadline the watchdog records what was measured and exits (module docstring).
HARD_STOP_GRACE_SECONDS = 60.0
BUDGET_EXHAUSTED = "run_budget_exhausted"


# ----------------------------------------------------------------- product

def reachable_relations(schema: Any, root_relations: Iterable[str], depth: int) -> list[str]:
    """Portal relations of every object type bound to a root relation or up to ``depth``
    declared links away (either direction), from the curated schema alone."""
    roots = set(root_relations)
    types = {tid for tid, t in schema.object_types.items()
             for b in t.get("bindings", []) if b.get("relation") in roots}
    frontier = set(types)
    for _ in range(max(0, depth)):
        nxt = set()
        for link in schema.link_types.values():
            ends = (link.get("from"), link.get("to"))
            if ends[0] in frontier and ends[1] not in types:
                nxt.add(ends[1])
            if ends[1] in frontier and ends[0] not in types:
                nxt.add(ends[0])
        nxt.discard(None)
        types |= nxt
        frontier = nxt
    return sorted({b["relation"] for tid in types if tid in schema.object_types
                   for b in schema.object_types[tid].get("bindings", [])
                   if b.get("source") == "portal_api" and isinstance(b.get("relation"), str)})


def plan_relations(schema: Any, families: Iterable[str]) -> list[str] | None:
    """Portal relations the investigation plans of ``families`` can read: the bindings of
    every object type a plan names, itself or as an end of a declared link it names, and
    likewise of every process graph its ``plx.process`` steps run (from the plan and graph
    files and the curated schema alone, never a list). None when a family has no plan:
    its walk may read any reachable relation."""
    from operating.ontology_tools_investigate import load_plans, plan_root_types
    from operating.ontology_tools_process import load_graphs
    plans = {family: plan.doc for plan in load_plans().values()
             for family in plan.doc["families"]}
    wanted = list(families)
    if any(family not in plans for family in wanted):
        return None
    graphs = load_graphs()
    docs = [plans[family] for family in wanted]
    docs += [graphs[step["args"]["process"]] for doc in list(docs) for step in doc["steps"]
             if step.get("tool") == "plx.process" and step.get("args", {}).get("process")
             in graphs]
    # A root listing navigation names names no type: its root types come from the schema.
    types: set[str] = {t for family in wanted for t in plan_root_types(plans[family], schema)}
    stack: list[Any] = docs
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str) and node in schema.object_types:
            types.add(node)
        elif isinstance(node, str) and node in schema.link_types:
            types.update((schema.link_types[node].get("from"), schema.link_types[node].get("to")))
    return sorted({b["relation"] for tid in types if tid in schema.object_types
                   for b in schema.object_types[tid].get("bindings", [])
                   if b.get("source") == "portal_api" and isinstance(b.get("relation"), str)})


def cited_relations(items: Iterable[Mapping[str, Any]]) -> list[str]:
    """Relations the items' expected values cite as oracle row references: the ones each
    item's run pin needs a mirror capture of (``evals.runner.make_run_pin`` reads the same
    citations), so the sync captures what the oracle drew whatever its link distance."""
    out: set[str] = set()
    for item in items:
        oracle = {str(g) for g in item["as_known_at"].get("l1_generations", {}).values()}
        for value in (item.get("known_outcome") or {}).get("values") or []:
            for cite in value.get("citations") or []:
                row = scoring.row_identity(cite.get("l1_handle"), oracle)
                if row is not None:
                    out.add(row[0])
    return sorted(out)


def relation_plan(schema: Any, items: Sequence[Mapping[str, Any]], depth: int
                  ) -> dict[str, Any]:
    """What a run over ``items`` mirrors and projects: ``needed`` (the relations the items'
    families' plans read, ``plan_relations``) mirrored first, then the cited ones
    (``first``); ``projected`` (the link neighbourhood of the roots and the plans' relations);
    ``cited_only`` (mirrored for the run pins, never projected), ``cited`` (every relation
    the run pins read), ``pins_after`` (the latest item time: a capture completed at or
    after it serves every pin) and every ``relations``."""
    needed = plan_relations(schema, sorted({i["family"] for i in items}))
    cited = cited_relations(items)
    times = [t for t in (i["as_known_at"].get("time") for i in items) if runner._instant(t)]
    projected = sorted(set(reachable_relations(schema, {i["root"]["relation"] for i in items},
                                               depth)) | set(needed or ()))
    return {"needed": needed,
            "first": None if needed is None else needed + sorted(set(cited) - set(needed)),
            "projected": projected, "cited_only": sorted(set(cited) - set(projected)),
            "cited": cited, "pins_after": max(times, key=runner._instant, default=None),
            "relations": sorted(set(projected) | set(cited))}


def mirror_block(info: Mapping[str, Any], needed: Sequence[str] | None,
                 cited_only: Sequence[str] = ()) -> dict[str, Any]:
    """The mirror read as the eval block publishes it (relation names and counts only):
    how many relations were required and complete, the relations the plans read
    (``needed``: every required one without plans) and, by name, every served relation
    left unfinished, those the plans read apart (``needed_unfinished``: the run is then
    incomplete) from the rest (``unfinished``: read only by plx.find's corroboration and
    the walk, named and in ``unfinished_share``, never silently dropped; the run notes them
    in ``eval.incomplete`` as ``evals.baseline.MIRROR_UNPLANNED``) and ``cited_only``: the
    relations mirrored only for the run pins (the items cite them, the schema's rule does
    not reach them), never projected by the run."""
    required = list(info.get("relations") or ())
    needed_set = set(required if needed is None else needed)
    left = [name for name in required if name in set(info.get("unfinished") or ())]
    return {"required": len(required), "complete": int(info.get("relations_complete") or 0),
            "needed": len(needed_set & set(required)),
            "needed_unfinished": sorted(n for n in left if n in needed_set),
            "unfinished": sorted(n for n in left if n not in needed_set),
            "unfinished_share": round(len(left) / len(required), 4) if required else None,
            "cited_only": sorted(cited_only)}


class Product:
    """The throwaway case-runtime state (mirror + audience) and projection store.
    ``evidence``: ``principals -> reader``, the capture-store reader of captured citations
    (``evals.capture_evidence``) the harness re-reads them with as the item's actor;
    ``portal_down``: the Portal failed the whole sync (its
    HTTP status / error code), else None. ``generation`` (an incremental build's
    ``ontology.project_incremental.Generation``) and ``sources`` (``(stack, mirror) ->``
    the capture sources it projected, opened as it opened them over another Portal mirror
    file): what a held-out variant's delta build reuses (``variant_product``); None for a
    product built whole (``build_store``)."""

    def __init__(self, state_dir: Path, audience: Path, store: Path | None,
                 info: Mapping[str, Any], *,
                 evidence: Callable[[Sequence[str]], Callable[[str], Any]] | None = None,
                 portal_down: Mapping[str, Any] | None = None, generation: Any = None,
                 sources: Callable[[ExitStack, Path], dict[str, Any]] | None = None) -> None:
        self.state_dir, self.audience, self.store, self.info = state_dir, audience, store, \
            dict(info)
        self.evidence = evidence
        self.portal_down = None if portal_down is None else dict(portal_down)
        self.generation, self.sources = generation, sources

    @property
    def mirror_path(self) -> Path:
        from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE
        return self.state_dir / CASE_RUNTIME_DIR / MIRROR_FILE

    def open_mirror(self) -> Any:
        from integrations.source_mirror import SourceMirror
        return SourceMirror(self.mirror_path, None, self.audience, tenant=TENANT)

    def open_store(self) -> Any:
        """A read-only reader of the projection store, for ``host(..., store=)``, primed:
        the store-wide facts every item's table reads (``TraverseTools.prime``, reader- and
        policy-independent) are read now, before any item's window opens."""
        from operating import ontology_tools as ot
        from operating import ontology_tools_traverse as tt
        reader = ot.StoreReader.open(self.store)
        try:
            tt.TraverseTools(ot.OntologyTools(reader, ot.ReadPolicy(tenant=TENANT,
                                                                    audiences={}))).prime()
        except BaseException:
            reader.conn.close()
            raise
        return reader

    def host(self, principals: Sequence[str], *, store: Any = None) -> Any:
        """A host for one item as ``principals``. ``store``: an open reader (``open_store``)
        the caller keeps and closes, shared by every item of a run so the store-wide facts
        each table reads are read once per run (``TraverseTools._store_cached``), not once
        per item; None opens one this host closes."""
        from evals.capture_evidence import CaptureEvidenceHost
        # The run's mirror is linked, never copied per item: offline hosts fetch nothing.
        case_host = runner.CaseHost(self.state_dir, self.audience, TENANT, principals,
                                    link_mirror=True)
        read = None if self.evidence is None else self.evidence(principals)
        # The plan claim gate re-reads a citation as the scorer does: a captured BC citation
        # from the capture store the product projected, read as these principals (the bare
        # case host resolves it live with metadata only: every captured value would be
        # claim_uncited).
        cited = case_host if read is None else CaptureEvidenceHost(case_host, read)
        reader = store
        try:
            if reader is None:
                reader = self.open_store()
            table = bl.ontology_tool_table(reader, tenant=TENANT, principals=principals,
                                           runtime=bl.HostCitations(cited))
        except BaseException:
            case_host.close()
            if store is None and reader is not None:
                reader.conn.close()
            raise
        host = bl.ToolHost(case_host, table, TENANT, principals,
                           closing=() if store is not None else (reader.conn,))
        return host if read is None else CaptureEvidenceHost(host, read)


class OnlyRelations:
    """A capture source projected only for the run's own ``relations``: a shared store (the
    verifier session's) may hold more, which a store of the run's own would not."""

    def __init__(self, inner: Any, relations: Sequence[str]) -> None:
        self._inner, self._relations, self.source = inner, frozenset(relations), inner.source
        if not callable(getattr(inner, "fingerprint", None)):
            # ``ontology.project_incremental`` then digests the records it reads instead
            self.fingerprint = None   # type: ignore[assignment]

    def fingerprint(self, relations: Sequence[str]) -> dict[str, str]:
        """The inner source's fingerprints of the run's own relations (read without
        parsing a row); any other relation reads empty, so it digests as no records."""
        own = self._inner.fingerprint([r for r in relations if r in self._relations])
        return {r: own[r] if r in self._relations else hashlib.sha256().hexdigest()
                for r in relations}

    def capture_gap(self, relation: str) -> str | None:
        held = getattr(self._inner, "capture_gap", None)
        if relation not in self._relations:
            return "source_not_captured" if held is not None else None
        return held(relation) if held is not None else None

    def records(self, relations: Sequence[str]) -> Mapping[str, Any]:
        inner = self._inner.records([r for r in relations if r in self._relations])
        return {r: inner[r] if r in self._relations else () for r in relations}

    def describe(self) -> dict[str, Any]:
        described = self._inner.describe()
        return {**described, "relations": {r: v for r, v in (
            described.get("relations") or {}).items() if r in self._relations}}


def _audience(work: Path, relations: Sequence[str]) -> Path:
    """The run's mirror audience: every family of ``relations`` granted to ``PRINCIPAL``."""
    from integrations.source_mirror import AUDIENCE_SCHEMA

    audience = work / "audience.json"
    families = sorted({relation.split(".", 1)[0] + ".*" for relation in relations})
    audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": TENANT,
                                    "audiences": {f: [PRINCIPAL] for f in families}}),
                        encoding="utf-8")
    return audience


def session_product(work: Path, relations: Sequence[str], session: Mapping[str, Any], *,
                    bc_environment: str | None = None, copy_mirror: bool = True,
                    client: Any = None, pin_relations: Sequence[str] = (),
                    pins_after: str | None = None, budget_seconds: float | None = None,
                    clock: Callable[[], Any] | None = None,
                    enter: Callable[[str], None] = lambda phase: None) -> Product:
    """The product over the verifier session's projection (``session``:
    ``scripts.ontology_projection_probe.open_session``): no sync, capture or projection of
    its own. The run copies the producer's mirror snapshot once (its items link that copy),
    opens the session generation read-only when every unit key matches its captures
    (``build_generation(reuse=True)``; else it projects only what differs into ``work``)
    and re-reads captured BC citations from the producer's BC capture. The mirror counts
    are the producer's (its manifest), for the run's own ``relations``. ``copy_mirror``
    False: the caller reads only the store and never opens a case host on the product
    (``scripts.eval_unfamiliar_sample``), so the producer's mirror is read in place,
    read-only, and never copied.

    ``pin_relations`` (the relations the items' run pins read, ``relation_plan`` ``cited``)
    and ``pins_after`` (the latest item time): the producer may have captured a relation
    before the oracle drew the items, and then no capture of it completes at or after an
    item's ``as_known_at.time`` and its pin drifts (``no_capture_at_or_after_time``). With a
    Portal ``client`` the run captures each such relation into its own copy (never the
    producer's file) before it opens the generation (``pin_sync``: the GET-only
    ``SourceMirror.sync`` the cold path runs, incremental where the catalog's ``since``
    allows, within ``budget_seconds``), so the product answers from the capture its pins
    read, as on the cold path; a relation the sync left unchanged keeps its unit keys and
    the generation is still reused. ``info["pin_sync"]``: relation counts and, when the
    Portal failed the sync, its error code."""
    from ontology.project import MirrorCaptures, capture_source
    from ontology.project_incremental import build_generation
    from ontology.schema import load_schema
    from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE

    started = time.monotonic()
    enter("session_projection")
    state = work / "state"
    (state / CASE_RUNTIME_DIR).mkdir(parents=True, exist_ok=True)
    audience = _audience(work, relations)
    manifest = session["manifest"]
    own = state / CASE_RUNTIME_DIR / MIRROR_FILE
    down: dict[str, Any] | None = None
    if session["mirror"] is not None and copy_mirror:
        runner.copy_mirror(Path(session["mirror"]), own)
    elif session["mirror"] is not None:
        own = Path(session["mirror"])
    else:   # the producer projected no Portal: its blocked part
        part = next((p for p in manifest["blocked_upstream"] if p.get("part") ==
                     "portal_mirror"), {})
        down = {k: part[k] for k in ("http_status", "error_code") if k in part} or \
            {"error_code": "not_projected"}
    synced = None
    if session["mirror"] is not None and copy_mirror and client is not None:
        stale = stale_relations(own, pin_relations, pins_after)
        if stale:
            enter("pin_sync")
            synced = pin_sync(client, own, audience, stale, budget_seconds=budget_seconds,
                              clock=clock)
            enter("session_projection")
    evidence = None
    if session["bc"] is not None and bc_environment:
        from evals.capture_evidence import bc_resolver, reader
        resolve_as = bc_resolver(Path(session["bc"]), bc_environment, granted=PRINCIPAL)
        evidence = lambda principals: reader(bc=resolve_as(principals))  # noqa: E731

    def open_sources(stack: ExitStack, mirror_file: Path) -> dict[str, Any]:
        opened: dict[str, Any] = {}
        if session["mirror"] is not None:
            opened["portal_api"] = stack.enter_context(MirrorCaptures(mirror_file))
        if session["bc"] is not None:
            opened["bc"] = stack.enter_context(capture_source("bc", session["bc"]))
        return opened
    with ExitStack() as stack:
        sources = open_sources(stack, own)
        generation = build_generation(work / "projection", load_schema(), sources,
                                      previous=session["generation"], reuse=True)
    mirror = manifest["mirror"] or {}
    complete = [n for n in relations if n in set(mirror.get("complete") or ())]
    served = [n for n in relations if n in set(mirror.get("served") or ())]
    info: dict[str, Any] = {
        "relations_required": len(relations), "relations_complete": len(complete),
        "relations_unserved": len(relations) - len(served), "relations": list(relations),
        "unfinished": sorted(set(served) - set(complete)),
        "mirror_complete": bool(served) and set(served) <= set(complete),
        "objects": (generation.report.get("counts") or {}).get("objects", 0),
        "captures": sorted(sources), "session_projection": generation.mode,
        "build_seconds": round(time.monotonic() - started, 1)}
    if synced is not None:
        info["pin_sync"] = synced
    if session["bc"] is not None:
        info["bc_capture"] = {"status": "captured" if (manifest.get("bc_capture") or {})
                              .get("all_complete") else "partial"}
    if down is not None:
        info["portal"] = {"status": "source_unavailable", **down}
    return Product(state, audience, generation.store, info, evidence=evidence,
                   portal_down=down, generation=generation, sources=open_sources)


def stale_relations(mirror_file: Path, relations: Sequence[str], after: str | None
                    ) -> list[str]:
    """``relations`` without a completed capture in ``mirror_file`` at or after ``after``
    (every one when ``after`` is None or unreadable): the ones a run pin would not find."""
    moment = runner._instant(after)
    if moment is None:
        return list(relations)
    with runner.MirrorPinReader(mirror_file) as reader:
        held = reader.first_captures(moment)
    return [name for name in relations if name not in held]


def pin_sync(client: Any, mirror_file: Path, audience: Path, relations: Sequence[str], *,
             budget_seconds: float | None, clock: Callable[[], Any] | None = None
             ) -> dict[str, Any]:
    """Capture ``relations`` into ``mirror_file`` now (GET only, within the budget), so the
    run pins of items the oracle drew earlier find a capture completed after their time:
    ``{"relations", "complete"}`` counts (``complete``: captured by this sync), plus ``error_code`` when the Portal failed the
    sync (the pins then drift as before, counted, never hidden)."""
    from integrations.source_mirror import MirrorError, SourceMirror
    from ontology.types import Actor

    out: dict[str, Any] = {"relations": len(relations), "complete": 0}
    with SourceMirror(mirror_file, client, audience, tenant=TENANT,
                      **({"clock": clock} if clock else {})) as mirror:
        try:
            synced = mirror.sync(Actor(TENANT, frozenset({PRINCIPAL})),
                                 relations=list(relations), budget_seconds=budget_seconds)
        except MirrorError as exc:
            if exc.code != "source_unavailable":
                raise
            return {**out, "error_code": exc.upstream_code or exc.code}
    out["complete"] = sum(run.get("outcome") == "captured" for run in synced["runs"])
    return out


def build_product(client: Any, work: Path, relations: Sequence[str], *,
                  budget_seconds: float | None,
                  clock: Callable[[], Any] | None = None,
                  bc_capture: Callable[[Path, float | None], Mapping[str, Any]] | None = None,
                  bc_environment: str | None = None,
                  first: Sequence[str] | None = None, mirror_db: Path | None = None,
                  bc_db: Path | None = None, bc_sets: Sequence[str] | None = None,
                  project: Sequence[str] | None = None,
                  run_left: Callable[[], float] | None = None,
                  incremental: bool = False,
                  enter: Callable[[str], None] = lambda phase: None) -> Product:
    """Mirror ``relations`` (GET only, within the budget; ``first``, the relations the
    plans read, before the rest) and project ``project`` of them (default all): the
    schema's relations, never one mirrored only for the run pins (``cited_relations``),
    which the mirror keeps and the projection leaves out. ``mirror_db``/``bc_db``: the
    Portal mirror and BC capture store to top up instead of capturing cold (the verifier
    session's ``{session_state}``): the run projects only its own relations of the mirror
    and ``bc_sets`` of the BC store, and its items read one copy of the mirror taken after
    the sync, never the shared file.
    ``clock``: the mirror's clock (fixture runs use the fixture world's own time, so its
    captures complete after the items' replica time, as a real sync does). ``client``
    None: the Portal has no credentials here (blocked ``not_configured``). ``bc_capture``
    (``scripts.eval_unfamiliar_sample.capture_bc`` bound to the BC credentials): the product
    captures BC itself after the sync and projects it too, on the budget
    ``eval_unfamiliar_sample.bc_budget`` gives it (never past ``run_left()``, the seconds the
    run's deadline has left); its citations are re-read from that capture for the scorer. A
    Portal that fails the whole sync is ``portal_down``, never raised. ``enter(phase)``: called
    as each phase starts (``mirror_sync``, ``mirror_copy``, ``bc_capture``, ``projection``).
    ``incremental``: project as a generation (``ontology.project_incremental``) a held-out
    variant's delta build reuses (``Product.generation``), not a store built whole."""
    from integrations.source_mirror import MirrorError, SourceMirror
    from ontology.project import MirrorCaptures, build_store, capture_source
    from ontology.project_incremental import build_generation
    from ontology.schema import load_schema
    from ontology.types import Actor
    from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE
    from scripts.eval_unfamiliar_sample import bc_budget, portal_budget, run_bc_capture

    started = time.monotonic()
    state = work / "state"
    (state / CASE_RUNTIME_DIR).mkdir(parents=True, exist_ok=True)
    audience = _audience(work, relations)
    info: dict[str, Any] = {"relations_required": len(relations), "relations_complete": 0,
                            "relations_unserved": 0, "mirror_complete": False, "objects": 0}
    actor = Actor(TENANT, frozenset({PRINCIPAL}))
    down: dict[str, Any] | None = None if client is not None else \
        {"error_code": "not_configured"}
    synced: dict[str, Any] = {"coverage": {}, "runs": []}
    own = state / CASE_RUNTIME_DIR / MIRROR_FILE
    enter("mirror_sync")
    with SourceMirror(own if mirror_db is None else mirror_db, client, audience,
                      tenant=TENANT, **({"clock": clock} if clock else {})) as mirror:
        if client is not None:
            try:
                synced = mirror.sync(actor, relations=list(relations),
                                     budget_seconds=portal_budget(budget_seconds,
                                                                  bc_capture is not None),
                                     order=list(first) if first else None)
            except MirrorError as exc:
                if exc.code != "source_unavailable":
                    raise
                # The Portal answered non-2xx (or not at all) before any relation was read.
                down = {"error_code": exc.upstream_code or exc.code,
                        **({"http_status": exc.status} if exc.status is not None else {})}
    if mirror_db is not None:
        enter("mirror_copy")
        runner.copy_mirror(Path(mirror_db), own)  # the run's one copy: items never write it
    bc_db, evidence = bc_db or work / "bc-capture" / "capture.sqlite", None
    if bc_capture is not None:
        enter("bc_capture")
        info["bc_capture"] = run_bc_capture(bc_capture, bc_db, bc_budget(
            budget_seconds, time.monotonic() - started, None if run_left is None else run_left()))
    rows = {r["relation"]: r for r in synced["coverage"].get("relations", [])}
    complete = [n for n in relations if (rows.get(n) or {}).get("complete")]
    served = [n for n in relations if (rows.get(n) or {}).get("served")]
    if down is None and relations and not served:
        from scripts.eval_unfamiliar_sample import upstream_failures
        failed = upstream_failures(synced.get("runs", []))
        if set(relations) <= set(failed):   # the Portal failed every relation's sync
            down = dict(failed[relations[0]])
    store, generation = work / "ontology.sqlite", None
    with_bc = (info.get("bc_capture") or {}).get("status") in ("captured", "partial")
    if with_bc and bc_environment:
        from evals.capture_evidence import bc_resolver, reader
        resolve_as = bc_resolver(bc_db, bc_environment, granted=PRINCIPAL)
        evidence = lambda principals: reader(bc=resolve_as(principals))  # noqa: E731

    def open_sources(stack: ExitStack, mirror_file: Path) -> dict[str, Any]:
        opened: dict[str, Any] = {"portal_api": stack.enter_context(MirrorCaptures(mirror_file))}
        if mirror_db is not None or project is not None:
            opened["portal_api"] = OnlyRelations(
                opened["portal_api"], relations if project is None else
                [r for r in relations if r in set(project)])
        if with_bc:
            opened["bc"] = stack.enter_context(capture_source("bc", bc_db))
            if bc_sets is not None:
                opened["bc"] = OnlyRelations(opened["bc"], bc_sets)
        return opened
    enter("projection")
    with ExitStack() as stack:
        sources = open_sources(stack, own)
        if incremental:
            generation = build_generation(work / "projection", load_schema(), sources,
                                          previous=None)
            store, built = generation.store, generation.report
        else:
            built = build_store(store, load_schema(), sources)
    info.update(relations_complete=len(complete), relations_unserved=len(relations) - len(served),
                relations=list(relations),
                unfinished=sorted(set(served) - set(complete)),
                mirror_complete=bool(served) and set(served) <= set(complete),
                objects=(built.get("counts") or {}).get("objects", 0),
                captures=sorted(sources), build_seconds=round(time.monotonic() - started, 1))
    if down is not None:
        info["portal"] = {"status": "source_unavailable", **down}
    return Product(state, audience, store, info, evidence=evidence, portal_down=down,
                   generation=generation, sources=None if generation is None else open_sources)


# ----------------------------------------------------------------- oracle reads

def oracle_field(portal: Any) -> runner.OracleField:
    """(relation, key, field_path) -> (found, the field as the oracle read it), for the run
    pin's drift check. Reads only the oracle's cached reads; never the mirror."""
    from evals.oracle.source import ReadFailed, key_text

    def lookup(relation: str, key: Mapping[str, Any], field_path: str) -> tuple[bool, Any]:
        if not key or not field_path.startswith("row."):
            return False, None
        first = sorted(key)[0]
        try:
            rows = portal.lookup(relation, first, key[first])
        except (ReadFailed, KeyError):
            return False, None
        hits = [row for row in rows
                if all(key_text(row.get(c)) == key_text(v) for c, v in key.items())]
        if len(hits) != 1:
            return False, None
        return runner._field(hits[0], field_path)
    return lookup


# ----------------------------------------------------------------- run

def item_digest(items: Sequence[Mapping[str, Any]]) -> str:
    """16-hex fingerprint of the item set (sorted item hashes)."""
    from evals.oracle.generator import item_hash
    return hashlib.sha256(",".join(sorted(item_hash(i) for i in items)).encode()) \
        .hexdigest()[:16]


# Each item gets a window: ``ITEM_SHARE`` of its fair slice of the time left (never less than
# the floor). The window starts before the item's run pin, so the pin, the find, the plan and the
# harness's claim-gate re-reads all count against it; the agent's own deadline stops
# ``ITEM_EVIDENCE_RESERVE`` of the window early to leave the re-reads their time. An agent past its
# deadline answers blocked or partial with a time_budget gap (``evals.baseline``), and a row that
# still ran past the window is marked ``over_time_budget`` and counted in the report.
ITEM_SHARE = 0.9
ITEM_EVIDENCE_RESERVE = 0.25
ITEM_WINDOW_FLOOR_SECONDS = 1.0


def item_window_seconds(left_seconds: float | None, items_left: int) -> float | None:
    """The next item's window: ``ITEM_SHARE`` of the time left over the items left (at least
    ``ITEM_WINDOW_FLOOR_SECONDS``, at most the contract's 600 s); None with no deadline. Items
    that finish early leave their time to the rest."""
    if left_seconds is None:
        return None
    share = max(left_seconds, 0.0) / max(items_left, 1) * ITEM_SHARE
    return max(ITEM_WINDOW_FLOOR_SECONDS, min(share, 600.0))


def run_items(items: Sequence[Mapping[str, Any]], product: Product, portal: Any, *,
              evidence_label: str, deadline: float | None = None,
              transcripts: Path | None = None, rows: list[dict[str, Any]] | None = None,
              clock: Callable[[], float] = time.monotonic
              ) -> tuple[list[dict[str, Any]], list[str]]:
    """Every item (one split) on the product until ``deadline`` (``clock``); returns (rows,
    incomplete reasons). ``rows``: the list each item's row is appended to as it finishes.
    ``portal``: the oracle's reads the run pins' drift check reads; None (a frozen held-out
    set, whose labels were read on the day it froze) checks each pin against the item's
    own expected value (``evals.runner.make_run_pin``). Held-out transcripts go only under
    the runner's held-out directory, their scores redacted."""
    rows = [] if rows is None else rows
    incomplete: list[str] = []
    by_id = {item["id"]: item for item in items}
    split = "held_out" if any(item["split"] == "held_out" for item in items) else "dev"
    out = runner._out_dir(transcripts, split) if transcripts is not None else None
    read = oracle_field(portal) if portal is not None else None
    with runner.MirrorPinReader(product.mirror_path) as reader, ExitStack() as stack:
        store = None
        if items:
            store = product.open_store()
            stack.callback(store.conn.close)
        for index, item in enumerate(items):
            if deadline is not None and clock() > deadline:
                incomplete.append(BUDGET_EXHAUSTED)
                break
            window = item_window_seconds(None if deadline is None else deadline - clock(),
                                         len(items) - index)
            item_deadline = None if window is None else clock() + window
            agent = bl.DeterministicInvestigator(
                deadline=None if window is None
                else item_deadline - window * ITEM_EVIDENCE_RESERVE, clock=clock)
            principals = (item.get("actor") or {}).get("principals") or [PRINCIPAL]
            pin = runner.make_run_pin(item, reader, oracle_field=read)
            row = bl.run_item(item, agent, product.host(principals, store=store), run_pin=pin,
                              evidence_label=evidence_label,
                              parent=by_id.get(item.get("parent_id") or ""))
            row["over_time_budget"] = item_deadline is not None and clock() > item_deadline
            if out is not None:
                base = out / item["id"] / "p0-r0"
                runner._write(base.with_suffix(".transcript.json"), row["transcript"])
                runner._write(base.with_suffix(".score.json"), scoring.redact(row["score"])
                              if split == "held_out" else row["score"])
            rows.append(row)
    return rows, incomplete


def public_rows(rows: Sequence[Mapping[str, Any]], *, with_ids: bool,
                hashes: Mapping[str, str]) -> list[dict[str, Any]]:
    """Per-item rows without transcripts, scores or values (dev ids only when allowed)."""
    out = []
    for row in rows:
        entry = {key: row[key] for key in ("family", "outcome", "plan", "abstained",
                                           "answerability", "reasons",
                                           "root", "root_kind", "plan_root", "chain_rows",
                                           "chain_rows_cited", "pin", "pin_checked",
                                           "tool_calls")}
        entry.update({key: row.get(key, 0) for key in ("values_expected", "values_stated",
                                                        "values_right")})
        entry["item_sha16"] = hashes.get(row["item_id"], "")[:16]
        if with_ids:
            entry = {"item_id": row["item_id"], **entry}
        out.append(entry)
    return out


def report(rows: Sequence[Mapping[str, Any]], items: Sequence[Mapping[str, Any]], *,
           split: str, evidence_label: str, run_id: str, product: Mapping[str, Any],
           incomplete: Sequence[str], with_ids: bool, sources: Mapping[str, str]
           ) -> dict[str, Any]:
    from evals.oracle.generator import item_hash
    hashes = {item["id"]: item_hash(item) for item in items}
    digest = item_digest(items)
    block = bl.eval_block(rows, split=split, evidence_label=evidence_label, run_id=run_id,
                          items_digest=digest, complete=len(rows) == len(items),
                          incomplete=incomplete)
    block["items_total"] = len(items)
    block["items_over_time_budget"] = sum(bool(row.get("over_time_budget")) for row in rows)
    return {"schema": SCHEMA, "format": bl.BASELINE_FORMAT, "evidence_label": evidence_label,
            "adapter_status": "isolated-fixture" if evidence_label == "isolated-fixture"
            else "live-unverified",
            "values_published": False, "run_id": run_id, "sources": dict(sources),
            "product": {k: product.get(k) for k in (
                "relations_required", "relations_complete", "relations_unserved",
                "mirror_complete", "objects")},
            "scorer": scoring.SCORER_VERSION, "runner": runner.RUNNER_VERSION,
            "eval": block, "summary_line": bl.summary_line(block),
            # held out: aggregates only, never a per-item row (evals/runner.py "Splits")
            **({"items": public_rows(rows, with_ids=with_ids, hashes=hashes)}
               if split == "dev" else {})}


ANSWER_COUNTS = ("sampled", "blocked_upstream", "not_held", "unscored_other",
                 "product_error", "scored", "correct", "wrong", "abstain_ok", "missed")


def answer_block(rows: Sequence[Mapping[str, Any]], blocked: int) -> dict[str, Any]:
    """The verifier's answer counts (``devloop/live_verify.py`` ``ANSWER_COUNTS``): blocked
    upstream, unscored (no plan, pin drift), wrong, abstained and correct kept apart
    (``scored`` is every labelled outcome, ``abstain_ok`` the justified abstentions), with
    the numbers ROADMAP B1 judges from ``evals.score.answer_rates``, the one computation
    the agent tiers report (decision 2026-10-19-b1-one-answer-rate): ``answered`` is correct
    plus wrong (an abstention, justified or not, is not covered), ``coverage_rate`` answered
    over the items that ran (sampled less blocked upstream, so a blocked run judges the part
    that ran), ``accuracy_rate`` correct/answered and ``confident_unsupported_rate``
    (``evals.score.confident_unsupported``/answered: a wrong answer stated without
    abstaining that the claim gate refused); ``accuracy`` and ``coverage`` give them as
    counts. A rate with nothing under it is null, never a pass."""
    out = dict.fromkeys(ANSWER_COUNTS, 0)
    out["sampled"], out["blocked_upstream"] = len(rows) + blocked, blocked
    unsupported = 0
    for row in rows:
        score = row["score"]
        outcome = score.get("outcome") if row["outcome"] in ("pass", "fail") else None
        bucket = {"correct": "correct", "justified_abstention": "abstain_ok",
                  "wrong": "missed" if score.get("abstained") else "wrong"}.get(
                      outcome, "unscored_other")
        out[bucket] += 1
        unsupported += bucket == "wrong" and scoring.confident_unsupported(score)
    out["scored"] = sum(out[k] for k in ("correct", "wrong", "abstain_ok", "missed"))
    answered = out["correct"] + out["wrong"]
    rates = scoring.answer_rates(runs=len(rows), answered=answered, correct=out["correct"],
                                 unsupported=unsupported)
    return {**out, "answered": answered, "accuracy": f"{out['correct']}/{answered}",
            "coverage": f"{answered}/{len(rows)}",
            "confident_unsupported": unsupported, "accuracy_rate": rates["accuracy"],
            "coverage_rate": rates["coverage"],
            "confident_unsupported_rate": rates["confident_unsupported_rate"]}


def verdict(result: Mapping[str, Any], *, min_items: int) -> int:
    """0 when the run completed over at least ``min_items`` items on a non-empty projection;
    under a Portal outage (``eval.pass_rule`` ``portal_outage``) over at least one item the
    BC and document captures could measure; an outage that left nothing to measure exits 0
    with ``outage_unmeasured`` (the check is then blocked whole, never passed). Any
    ``eval.fail_reasons`` (``blocked_tripwire``: read-gap blocked items above the tripwire)
    fails it. Pass rates are measured, never asserted."""
    block = result["eval"]
    if result.get("outage_unmeasured"):
        return 0  # blocked whole by the spec's blocked_when: the outage left nothing
    floor = 1 if block.get("pass_rule") == "portal_outage" else min_items
    ok = (block["complete"] and block.get("measured", block["items"]) >= floor
          and (result["product"].get("objects") or 0) > 0 and not block.get("fail_reasons"))
    return 0 if ok else 3


# ----------------------------------------------------------------- report file

# The last hand diagnosis of the fixture run; render_markdown prints it only while the run
# still matches it (so a regenerated report never carries a stale diagnosis).
DIAGNOSED = {
    "date": "2026-10-22",
    "run": {"items_digest": "f03852a80198817b", "dev_set": "19/30",
            "by_reason": {"answerability_mismatch": 6, "gap_missing": 2,
                          "value:absence_not_reported": 2, "value:reported_absent": 5,
                          "value:value_missing": 9}},
    "notes": [
        "Lane b1-dev-baseline-correct. Before -> after: dev_set 17/30 -> 19/30 (F3 4/6 -> "
        "6/6; F1 10/12, F2 3/6, F4 0/6 unchanged), answerability_mismatch 7 -> 6. The "
        "order_quantities plan states produced as the Portal's qty manufactured (the valued "
        "FM status entries of the one FM assembly order, else the MO's yield_actual: "
        "`prefer`) and an order's total over its cited lines as one `sum` the scorer "
        "recomputes (`total`), so both F3 `cited_other_row` items pass; a whole-record "
        "read's `not_served` property the plan never asks for no longer leaves the answer "
        "partial; the plan root is the typed identifier plx.find bound, never a free-text "
        "lead ranked above it. Run pins 30/30 `pinned`: the live "
        "`no_capture_at_or_after_time` class (all 12 F3/F4 items on 2026-10-20) is the "
        "session product's `pin_sync` top-up (f898a85b2f) and main's Portal catalog bound "
        "(raised 200 to 1000 in f04889ee90), which already covers the 246-relation allowlist. 11 fail, "
        "by class:",
        "Proof of completeness (7 items, the largest class): with relation-level "
        "completeness on main (devloop/DECISIONS/2026-10-17-answer-correctness-relation-"
        "completeness.md) every F4 side still reads undecided: ordered "
        "`evidence_unproven`, received and invoiced `evidence_not_fully_visible` "
        "(ontology_tools_process.py `_carrier_problem`: a hop from a Portal record into BC "
        "needs `covers_family('bc')`, a `bc:*` grant, and the eval reader, like the case "
        "host, grants each BC unit it holds), so F4 match_n and three_way_match "
        "`value_missing` / `reported_absent` / `answerability_mismatch` on 5, an unproven "
        "absence (`absence_not_reported`, `gap_missing`) on 1; F1 `gap_not_ingested` "
        "partial on 1. Owned by ontology_tools_process.py.",
        "Counts of zero (2, F2 `sage_order_count`): the plan proves the zero by a complete "
        "search, but the answer format has no citation of a complete search, so it is not "
        "stated (devloop/DECISIONS/2026-10-28-plan-party-twins-proven-zero-zero-citation.md).",
        "Wrong root (1): F1 an assembly-order number plx.find resolves to its FM twin "
        "(`root_twins_not_roots`), so the plan's five counts are not reached "
        "(`value_missing`).",
        "Gaps not stated (1): F2 an order with no shipment whose absence is unproven "
        "(`no_object_reached`: partial, never stated absent).",
        "Not taken: the three_way_match plan could reach the invoice lines through a "
        "receipt's vendor invoice number (PurchaseInvoice.invoices_receipt, +2 invoiced "
        "values, 0 items) but that text-match lead carries no vendor check, so another "
        "vendor's invoice reusing the number would be stated as the PO's (the chain plan's "
        "`party_check` is the vendor check it would need).",
    ],
}


def render_markdown(result: Mapping[str, Any]) -> str:
    block = result["eval"]
    lines = [
        "# Evaluation baseline: dev set (fixture)",
        "",
        "Generated by `python3 -m scripts.eval_dev_baseline fixture --write-baseline "
        "--write-report`; machine copy `evals/baseline/dev-fixture.json`.",
        "",
        f"**Evidence: {result['evidence_label']}.** The 30 dev items are the oracle's "
        "synthetic fixture world (`evals/oracle/fixture_world.py`), not PLX data, and the "
        "agent is the deterministic investigation path (`evals/baseline.py`), not a model "
        f"({block['llm_calls']} LLM calls). This is not a live measurement and not "
        "`live-verified`; the live run is `devloop/live-checks/eval.dev_baseline.json` "
        "(daily).",
        "",
        "## Result",
        "",
        f"- dev_set pass: **{block['dev_set']}** (needs_agent count in the denominator, never "
        "as passes)",
        f"- attempted pass (items the deterministic path answered): {block['attempted']}",
        f"- needs_agent: {block['needs_agent']}; fail: {block['fail']}; pass: {block['pass']}; "
        f"abstained (attempted, no value stated): {block['abstained']}",
        "- plans run: " + ", ".join(f"{plan} {count}"
                                    for plan, count in block["plans"].items()),
        f"- root resolved by `plx.find` (exact root row): "
        f"{sum(int(e['root_found'].split('/')[0]) for e in block['by_family'].values())}"
        f"/{block['items']}; run pin `pinned`: {block['pinned']}; pin_drift (excluded): "
        f"{block['pin_drift']}",
        f"- product: {result['product']['objects']} objects from "
        f"{result['product']['relations_complete']} mirrored relations "
        f"({result['product']['relations_unserved']} of "
        f"{result['product']['relations_required']} schema relations not served by the "
        "fixture API)" + (f"; BC capture `{result['product']['bc_capture']}` (the GET-only "
                          "BC client over the fixture world's BC fake, injected transport)"
                          if result["product"].get("bc_capture") else ""),
        "",
        "Coverage beside accuracy: `values stated` counts the expected values the answers "
        "state, `values right` those stated right (an item passes only with every value right, "
        "its answerability and its gaps).",
        "",
        "| family | pass | attempted | fail | needs_agent | root found | chain rows cited | "
        "values stated | values right |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for family, entry in sorted(block["by_family"].items()):
        lines.append(f"| {family} | {entry['pass']} | {entry['attempted']} | {entry['fail']} | "
                     f"{entry['needs_agent']} | {entry['root_found']} | "
                     f"{entry['chain_rows_cited']} | {entry['values_stated']} | "
                     f"{entry['values_right']} |")
    lines += ["", "Failure reasons (attempted items that failed; one item can carry several):",
              ""]
    lines += [f"- `{code}`: {count}" for code, count in block["by_reason"].items()] or \
        ["- none"]
    lines += ["", "Per item:", "",
              "| item | outcome | plan | root | chain cited | values right | reasons |",
              "|---|---|---|---|---|---|---|"]
    for row in result["items"]:
        lines.append(f"| {row.get('item_id', row['item_sha16'])} | {row['outcome']} | "
                     f"{row['plan'] or '-'} | {row['root']} | "
                     f"{row['chain_rows_cited']}/{row['chain_rows']} | "
                     f"{row['values_right']}/{row['values_expected']} | "
                     f"{', '.join(row['reasons']) or '-'} |")
    lines += ["", "## Reading it", "",
              "- Plans: each item runs the `plx.investigate` plan of its family (routed by "
              "the item's family: no model reads the question) and publishes the plan's cited "
              "values, nulls and gaps; a Workbench count or verdict cites no row and is left "
              "out with a gap, never stated uncited. `needs_agent`: the family has no plan "
              "and the question is no count question (the find/object/around/timeline walk "
              "still ran), or the plan answered only with uncited counts or verdicts (F1 "
              "chain counts). They wait for a Claude Code session (`evals.runner run`).",
              "- Expected values without citations (F1/F2 counts, F4 match booleans) are "
              "answered by a calculation (`count` of the cited records, `equal` over the "
              "cited quantities, `sum` for totals): the scorer recomputes it from the cited "
              "values (`evals/score.py`), so a right answer passes and a miscalculated one "
              "is wrong. A plan's count names the objects it counted, not their cited rows, "
              "so the deterministic path cannot state it as a calculation yet.",
              "- Gaps match on their state and the value they concern (`concerns`: the "
              "oracle names its own expected value, the plan the answer entry the gap "
              "leaves null), else on subject text: `gap_state_mismatch` is the same gap "
              "stated in another state, `gap_missing` one the answer does not state.",
              "- Citations: items pin the oracle's read digests, which no tool returns. The "
              "runner pins, per relation the expected citations name, the first completed "
              "mirror capture at or after the item's time (run pin, "
              "devloop/DECISIONS/2026-10-02-oracle-pinning.md); a cited field whose pinned "
              "mirror version differs from the oracle's read makes the item `pin_drift`, "
              "excluded from every rate and counted.",
              "- Time: under a run deadline each item gets a window (`ITEM_SHARE` of its "
              "fair share of the time left) that opens before its run pin, so the pin, "
              "`plx.find`, the plan and the claim gate's re-reads all count against it; the "
              "agent stops `ITEM_EVIDENCE_RESERVE` of the window early with a `time_budget` "
              "gap, and a row past its window is counted (`items_over_time_budget`: "
              f"{block.get('items_over_time_budget', 0)} here; the fixture run has no "
              "deadline).",
              "- Answer gate (`operating/answer_gate.py` `gate_answer`, one function): the "
              "Ask loop and the real agent's MCP session (`plx.case_get` answer mode) gate "
              "the final claims through it, a refused claim is dropped and repaired once. "
              "The real agent's final reply holds only what that gate passed "
              "(`evals.runner.gate_final_reply`: a value it refused or never checked is "
              "withheld and named) and the runner records what the gate returned "
              "(`answer_gate` in the eval summary, counts only).",
              ""]
    lines += ["## Diagnosis", ""]
    if all(block.get(k) == v for k, v in DIAGNOSED["run"].items()):
        lines += [f"Hand-written for the run above ({DIAGNOSED['date']}; items_digest "
                  f"{DIAGNOSED['run']['items_digest']}, dev_set {DIAGNOSED['run']['dev_set']}, "
                  f"by_reason as listed):", ""] + [f"- {text}" for text in DIAGNOSED["notes"]]
    else:
        lines += ["No current diagnosis: this run differs from the one last diagnosed "
                  f"({DIAGNOSED['run']}). Re-diagnose and update `DIAGNOSED` in "
                  "`scripts/eval_dev_baseline.py`."]
    lines += ["", "Regenerate after any change to the tools, projection, schema, scorer or "
              "dev set; the numbers are measured, never asserted.", ""]
    return "\n".join(lines)


# ----------------------------------------------------------------- CLI

def fixture_client() -> Any:
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from scripts.eval_oracle_dev import eval_world
    from scripts.eval_unfamiliar_sample import _fixture_fetch
    # The same world the frozen dev items were drawn from (evals/oracle build_eval_world).
    return WorkbenchReadApiClient(FIXTURE_URL, FIXTURE_KEY, fetch=_fixture_fetch(eval_world()))


def fixture_clock() -> Any:
    """The fixture world's "now" (scripts.eval_oracle_dev.FIXTURE_NOW): its items' replica
    time is earlier, so the mirror's captures pin as a real sync's would."""
    from scripts.eval_oracle_dev import FIXTURE_NOW
    return datetime.fromisoformat(FIXTURE_NOW.replace("Z", "+00:00"))


def fixture_bc_capture(world: Any, *, host_tenant: str | None = None
                       ) -> tuple[Callable[[Path, float | None], Mapping[str, Any]], str]:
    """The fixture product's own BC capture and its BC environment: ``capture_bc`` (the
    GET-only BC client) over ``evals.fixture_bc``, the fixture world's BC served as BC API
    v2.0 (injected transport with fixture credentials; the fixture run only, never a live
    path). ``host_tenant``: bind the store to that Workbench tenant (``capture_bc``), for a
    fixture host that reads it as its own capture."""
    from evals import fixture_bc
    from scripts.eval_unfamiliar_sample import capture_bc

    send = fixture_bc.sender(world)
    return (lambda db, seconds: capture_bc(fixture_bc.ENV, db, seconds, sender=send,
                                           books=fixture_bc.BOOKS, host_tenant=host_tenant),
            fixture_bc.ENV["BC_ENVIRONMENT"])


def run_fixture(work: Path, *, items_dir: Path = DEV_DIR, transcripts: Path | None = None,
                link_depth: int = DEFAULT_LINK_DEPTH) -> dict[str, Any]:
    from ontology.schema import load_schema
    from scripts import eval_oracle_dev as dev

    items = runner.load_items(items_dir, split="dev")
    projected = reachable_relations(load_schema(), {i["root"]["relation"] for i in items},
                                    link_depth)
    relations = sorted(set(projected) | set(cited_relations(items)))
    bc_capture, bc_environment = fixture_bc_capture(dev.eval_world())
    product = build_product(fixture_client(), work, relations, budget_seconds=None,
                            clock=fixture_clock, project=projected,
                            bc_capture=bc_capture, bc_environment=bc_environment)
    sources, _, configured, _ = dev.build_fixture(dev.eval_world())
    sources.portal.prefetch()
    rows, incomplete = run_items(items, product, sources.portal,
                                 evidence_label="isolated-fixture", transcripts=transcripts)
    result = report(rows, items, split="dev", evidence_label="isolated-fixture",
                    run_id=FIXTURE_RUN_ID, product=product.info, incomplete=incomplete,
                    with_ids=True, sources=configured)
    result["product"]["bc_capture"] = (product.info.get("bc_capture") or {}).get("status")
    return result


BC_ENV = ("BC_TENANT_ID", "BC_CLIENT_ID", "BC_CLIENT_SECRET", "BC_ENVIRONMENT")


class Progress:
    """How far a live run got, so its record can be made at any point: by the run when it
    ends, or by the watchdog (``watchdog``) while a phase still runs past the deadline.
    ``clock`` is the run's monotonic clock (every budget reads it)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.emitted: int | None = None   # the exit code of the record printed, once
        self.phase: str | None = None
        self.since = clock()
        self.done: dict[str, float] = {}
        self.stopped: str | None = None
        self.run_id = ""
        self.configured: dict[str, str] = {}
        self.oracle: dict[str, Any] = {}
        self.items: list[dict[str, Any]] = []      # the items the run measures
        self.blocked: list[dict[str, Any]] = []    # blocked upstream: counted, never run
        self.needed: list[str] | None = None
        self.cited_only: list[str] = []
        self.product: Product | None = None
        self.rows: list[dict[str, Any]] = []
        self.incomplete: list[str] = []
        # held out (run_heldout): admission refusals, the frozen set, each variant product
        self.refused: list[dict[str, Any]] = []
        self.frozen: dict[str, Any] = {}
        self.variant_products: dict[str, dict[str, Any]] = {}

    def enter(self, phase: str) -> None:
        now = self.clock()
        if self.phase is not None:
            self.done[self.phase] = self.done.get(self.phase, 0.0) + now - self.since
        self.phase, self.since = phase, now

    def phases(self) -> dict[str, float]:
        """Seconds per phase in the order they ran, the running one up to now."""
        out = dict(self.done)
        if self.phase is not None:
            out[self.phase] = out.get(self.phase, 0.0) + self.clock() - self.since
        return {phase: round(seconds, 1) for phase, seconds in out.items()}


def live_result(progress: Progress, args: argparse.Namespace, *,
                stopped: str | None = None) -> dict[str, Any]:
    """The live record of what ``progress`` holds: the whole run, or (``stopped``: the phase
    the budget cut) a partial one whose items not measured are counted, never scored."""
    stopped = stopped or progress.stopped
    rows, product = list(progress.rows), progress.product
    info = product.info if product is not None else {}
    down = product is not None and product.portal_down is not None
    incomplete = list(progress.incomplete)
    if stopped is not None and BUDGET_EXHAUSTED not in incomplete:
        incomplete.append(BUDGET_EXHAUSTED)
    mirror = mirror_block(info, progress.needed, progress.cited_only)
    if "pin_sync" in info:
        mirror["pin_sync"] = info["pin_sync"]
    if product is not None and (mirror["needed_unfinished"] or not info.get(
            "relations_complete")) and not down:
        incomplete.append("mirror_incomplete")
    if mirror["unfinished"]:
        incomplete.append(bl.MIRROR_UNPLANNED)
    result = report(rows, progress.items, split="dev", evidence_label="live-read",
                    run_id=progress.run_id, product=info, incomplete=incomplete,
                    with_ids=False, sources=progress.configured)
    measured = {row["item_id"]: row for row in rows}
    result["item_rows"] = [item_row(item, measured.get(item["id"]), stopped)
                           for item in progress.items] + \
        [item_row(item, None, None, blocked=True) for item in progress.blocked]
    oracle = progress.oracle
    parts = list(oracle.get("blocked_upstream") or [])
    if down:
        parts.append({"part": "portal_product", "source": "portal-workbench-api",
                      **product.portal_down})
    # A stratum blocked before any item carries its even share of --count, so an outage
    # lowers coverage instead of vanishing (as the unfamiliar eval counts it).
    quota = (args.count // max(1, len(oracle.get("strata") or ()))) * len(
        oracle.get("blocked_upstream") or [])
    result["blocked_upstream"] = parts
    result["product"]["captures"] = info.get("captures")
    if "session_projection" in info:
        result["product"]["session_projection"] = info["session_projection"]
    if "bc_capture" in info:
        result["product"]["bc_capture"] = info["bc_capture"].get("status")
    result["eval"].update(
        mirror=mirror, pass_rule="portal_outage" if down else "all_sources",
        measured=len(rows), blocked_upstream=len(progress.blocked), blocked_quota=quota,
        answer=answer_block(rows, len(progress.blocked) + quota),
        partition={"moved_to_heldout": (oracle.get("partition_dropped") or {}).get(
            "related_to_other", 0), "heldout_sets": oracle.get("heldout_sets", 0)},
        phases=progress.phases(),
        stopped=None if stopped is None else {"phase": stopped, "reason": BUDGET_EXHAUSTED})
    # A Portal outage and nothing the other sources could measure: blocked whole.
    result["outage_unmeasured"] = "portal_outage" if down and not rows and \
        stopped is None else None
    return result


def item_row(item: Mapping[str, Any], row: Mapping[str, Any] | None, stopped: str | None, *,
             blocked: bool = False) -> str:
    """One item as ``family-number:root:outcome:reason`` codes (``ENUM_STRING``-shaped, at
    most 64 characters, a long row's outcome shortened before its reason): the item's family and its number within the family in the run
    (the oracle's item id without the run's affixes, so the same row is followed up on a
    rerun of the day), a measured item's root resolution, outcome and first reason
    (``none``), a blocked one ``blocked_upstream``, one the run did not reach
    ``not_measured`` with the phase the budget stopped (``not_reached`` when the run ended
    first). ``:`` separates the codes: the verifier's scrubber reads a run of 32 letters,
    digits and ``/`` with a digit in it as an opaque token and would redact the row. Never
    a business identifier, value or citation."""
    number = next((part.lstrip("0") or "0" for part in str(item["id"]).split("-")
                   if part.isdigit()), None)
    label = item["family"] if number is None else f"{item['family']}-{number}"
    if blocked:
        parts = [label, "none", "blocked_upstream", "portal"]
    elif row is None:
        parts = [label, "none", "not_measured", stopped or "not_reached"]
    else:
        parts = [label, str(row.get("root") or "none"), row["outcome"],
                 (row.get("reasons") or ["none"])[0]]
    parts = [str(part) for part in parts]
    over = len(":".join(parts)) - 64
    if over > 0:   # the outcome gives way before the reason, the follow-up's lead
        parts[2] = parts[2][:max(1, len(parts[2]) - over)]
    return ":".join(parts)[:64]


def run_live(env: Mapping[str, str], args: argparse.Namespace, *,
             build: Callable[..., Any] | None = None, client: Any = None,
             clock: Callable[[], Any] | None = None,
             bc_capture: Callable[[Path, float | None], Mapping[str, Any]] | None = None,
             progress: Progress | None = None,
             wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
             ) -> dict[str, Any]:
    """The live run. ``build``/``client``/``bc_capture`` (tests): an injected oracle source
    builder with ``scripts.eval_oracle_dev.build_live``'s signature, a Portal client and the
    product's BC capture; by default the reviewed GET-only clients from the environment
    (the Portal and BC each only when their credentials are set). ``progress``: where the
    run records how far it got (the watchdog's record reads it); ``clock``: the mirror's;
    ``wall_clock``: the UTC time the oracle draws at (``now``, the ``auto`` run id's date)."""
    from ontology.schema import load_schema
    from scripts import eval_oracle_dev as dev

    progress = progress or Progress()
    now_s = progress.clock
    deadline = progress.since + args.total_budget_seconds
    # The oracle's reads, the mirror sync and the BC capture stop here; the reserve is the
    # projection's and the items'.
    reserved = deadline - args.run_reserve_seconds
    reads_until = [reserved]

    def guard() -> None:
        if now_s() >= reads_until[0]:
            raise dev.BudgetExhausted(BUDGET_EXHAUSTED)
    now = wall_clock().strftime("%Y-%m-%dT%H:%M:%SZ")
    progress.run_id = args.run_id if args.run_id != "auto" else "live-" + now[:10]
    state = args.state_dir if args.state_dir is not None else dev.default_state_dir(env)
    if not dev._outside_repository(state):
        # its digest manifest would be lost to the held-out check (never committed)
        raise ValueError("live_dev_state_inside_repository")
    progress.enter("oracle")
    heldout_keys, heldout_sets = dev.frozen_heldout_keys(state)
    try:
        sources, documents, progress.configured = (build or dev.build_live)(env, args,
                                                                            guard=guard)
        oracle, items = dev.execute(sources, documents, run_id=progress.run_id, now=now,
                                    count=args.count, audit=0, id_suffix="live",
                                    evidence_note=None, evidence_label="live-read",
                                    configured=progress.configured,
                                    exclude_keys=heldout_keys)
    except dev.BudgetExhausted:
        progress.stopped = "oracle"
        return live_result(progress, args)
    progress.oracle = {**oracle, "heldout_sets": heldout_sets}
    # Later reads (the run pins' drift checks) may use the rest of the run.
    reads_until[0] = deadline
    # Oracle-generated live items are dev-format items held only in memory here; their
    # digests go to the host state directory for the held-out check. A cut before the
    # items still counts every one the oracle drew (items_total, item_rows).
    progress.items = list(items)
    dev.write_live_dev_manifest(state, progress.run_id, items)
    schema = load_schema()
    # The relations the families' plans read are mirrored first and decide completeness, the
    # ones the items cite (their run pins') next; the rest of the link-depth neighbourhood
    # follows in the same budget. Only the schema's (the neighbourhood and the plans') are
    # projected: a cited-only relation is mirrored for its pin, never made searchable.
    plan = relation_plan(schema, items, args.link_depth)
    progress.needed, progress.cited_only = plan["needed"], plan["cited_only"]
    product = live_product(env, args, plan, client=client, clock=clock,
                           bc_capture=bc_capture, progress=progress,
                           run_left=lambda: reserved - now_s())
    outage = dev.portal_blocked(sources)
    if product.portal_down is None and outage is not None:
        # The oracle's own Portal reads all failed upstream (the items' roots may need no
        # Portal relation, so the mirror sync alone cannot tell).
        product.portal_down = {k: outage[k] for k in ("http_status", "error_code")
                               if k in outage}
    progress.product = product
    # A Portal outage blocks the items whose label reads the Portal: counted, never run.
    down = product.portal_down is not None
    progress.blocked = [i for i in items if down and dev.needs_portal(i)]
    progress.items = [i for i in items if not (down and dev.needs_portal(i))]
    progress.enter("items")
    _, incomplete = run_items(progress.items, product, sources.portal,
                              evidence_label="live-captures", deadline=deadline,
                              rows=progress.rows, clock=now_s)
    progress.incomplete += incomplete
    if BUDGET_EXHAUSTED in incomplete:
        progress.stopped = "items"
    progress.enter("report")
    return live_result(progress, args)


# ----------------------------------------------------------------- held-out set

HELDOUT_SCHEMA = "plx-eval-heldout-product-run/v1"
REPLAY_URL = "https://workbench-replay.invalid"


class Refused(ValueError):
    """A held-out set the run will not score; the message is a code, never a value."""


def _instant_text(text: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class MirrorReplay:
    """The reporting API's three GET routes (``integrations.workbench_read_api``) served from
    a source mirror's current rows, read-only and without a network: the source a
    held-out variant's product syncs from, so it reads the run's own capture again, never
    the Portal. ``perturbation`` (a variant's ``{"transform", "params"}``): every relation
    is read whole and served as ``evals.oracle.perturb.portal_port`` serves it to the
    oracle, and the replica time moves with it (``perturb.replica_time``). Pages never
    split a key's rows (a repeated event stays one run); a ``since`` read is refused.
    ``rows_served``: the rows its pages served (a variant's delta build reads only the
    relations its transform touches); ``relations``: the mirror's relations it serves."""

    def __init__(self, path: Path, perturbation: Mapping[str, Any] | None = None) -> None:
        from evals.oracle import perturb
        self._port: Callable[[str, Sequence[str]], Any] = self._read
        if perturbation is not None:   # an unknown transform or parameter fails here
            transform, params = perturbation["transform"], perturbation["params"]
            self._port = perturb.portal_port(self._read, transform, params)
        self._db = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
        self._entries: dict[str, dict[str, Any]] = {}
        times = []
        for name, entry, health in self._db.execute(
                "SELECT relation, catalog_entry_json, health_before_json FROM relation_state "
                "ORDER BY relation"):
            self._entries[name] = json.loads(entry)
            moment = _instant_text((json.loads(health) or {}).get("generated_at"))
            if moment is not None:
                times.append(moment)
        if not times:
            self._db.close()
            raise RuntimeError("replay_mirror_has_no_replica_time")
        self.generated_at: Any = max(times).strftime("%Y-%m-%dT%H:%M:%SZ")
        if perturbation is not None:
            self.generated_at = perturb.replica_time(self.generated_at, transform, params)
        self._runs: dict[tuple[str, tuple[str, ...]], list[list[dict[str, Any]]]] = {}
        self.rows_served = 0

    @property
    def relations(self) -> list[str]:
        return list(self._entries)

    def close(self) -> None:
        self._db.close()

    def _read(self, name: str, columns: Sequence[str]) -> dict[str, Any]:
        """One relation's current rows in the oracle's read shape (``source.PortalReads``)."""
        key = list(self._entries[name]["key"])
        wanted = sorted(set(columns) | set(key))
        rows = [json.loads(text) for (text,) in self._db.execute(
            "SELECT row_json FROM row_current WHERE relation = ? AND "
            "absent_since_capture IS NULL ORDER BY key_json, run_index", (name,))]
        return {"relation": name, "columns": wanted, "key": key,
                "rows": [{c: row.get(c) for c in wanted} for row in rows],
                "health_before": {"generated_at": self.generated_at}}

    def _relation_runs(self, name: str, columns: Sequence[str]) -> list[list[dict[str, Any]]]:
        cache = (name, tuple(sorted(columns)))
        if cache not in self._runs:
            self._runs.clear()   # one relation at a time: the sync reads them in turn
            read = self._port(name, list(columns))
            key = list(self._entries[name]["key"])
            runs: dict[str, list[dict[str, Any]]] = {}
            for row in read["rows"]:
                runs.setdefault(json.dumps([row.get(k) for k in key], sort_keys=True,
                                           default=str), []).append(row)
            self._runs[cache] = list(runs.values())
        return self._runs[cache]

    def fetch(self, url: str, headers: Mapping[str, str], *, max_bytes: int = 0) -> Any:
        parts = urlsplit(url)
        if parts.path == "/v1/health":
            return {"ok": True, "recovery": True, "replayLagSeconds": None, "unavailable": [],
                    "missingColumns": [], "generatedAt": self.generated_at,
                    "keysExpiringSoon": []}
        if parts.path == "/v1/relations":
            return {"relations": list(self._entries.values())}
        prefix, suffix = "/v1/relations/", "/rows"
        name = unquote(parts.path[len(prefix):-len(suffix)]) \
            if parts.path.startswith(prefix) and parts.path.endswith(suffix) else None
        if name not in self._entries:
            raise RuntimeError("replay_route_not_served")
        query = parse_qs(parts.query)
        if "since" in query:
            raise RuntimeError("replay_since_not_served")
        columns = [c for c in query.get("columns", [""])[0].split(",") if c]
        limit = int(query.get("limit", ["1000"])[0])
        start = int(query.get("cursor", ["o:0"])[0].split(":", 1)[1])
        runs = self._relation_runs(name, columns)
        expected = sorted(set(columns) | set(self._entries[name]["key"]))
        page: list[dict[str, Any]] = []
        end = start
        while end < len(runs) and (not page or len(page) + len(runs[end]) <= limit):
            page.extend(runs[end])
            end += 1
        self.rows_served += len(page)
        return {"relation": name, "columns": expected,
                "rows": [{c: row.get(c) for c in expected} for row in page],
                "nextCursor": f"o:{end}" if end < len(runs) else None}


def load_heldout(state: Path, source: str, run_id: str
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """The frozen held-out set ``run_id`` of ``source`` (``scripts.eval_oracle_dev heldout``
    froze it under ``state``): (admitted items, refused ``{id, reasons}``, frozen record).
    Refused whole (``Refused``, a code) when the set is not frozen, does not match its
    digest list, was frozen from another source, fails the runner's held-out load
    (``evals.runner.load_items``: the credentialed ``PLX_EVAL_HELD_OUT_DIR`` outside Git,
    every item schema-valid, none sharing a document, root or identifier with a committed
    dev or exposed item) or shares a partition key with a live dev run's manifest in
    ``state``. Admission (``evals.admission.admissible``) runs with this run's perturbed
    Portal, so a variant whose label reads only the Portal is scored."""
    from evals.admission import admissible
    from evals.partition import key_digests
    from scripts import eval_oracle_dev as dev

    if not dev.HELDOUT_RUN_ID_SHAPE.fullmatch(run_id):
        raise Refused("heldout_run_id_shape")
    if not dev._outside_repository(state):
        raise Refused("heldout_state_inside_repository")
    set_dir = state / source / run_id
    try:
        frozen = dev._load_frozen(set_dir)
    except RuntimeError:
        raise Refused("heldout_frozen_set_does_not_match_its_digests") from None
    except (OSError, ValueError):
        raise Refused("heldout_frozen_set_unreadable") from None
    if frozen is None:
        raise Refused("heldout_set_not_frozen")
    record = frozen[0]
    if record.get("source") != source or record.get("id_suffix") != source:
        raise Refused("heldout_frozen_source_mismatch")
    try:
        items = runner.load_items(set_dir / "items", split="held_out", admit=False)
    except runner.RunnerError as exc:
        # item_invalid carries the schema error, which can quote a field value: code only
        code = str(exc)
        raise Refused("item_invalid" if code.startswith("item_invalid") else code) from None
    if sorted(dev.content_hash(i) for i in items) != record.get("item_sha256"):
        raise Refused("heldout_items_differ_from_frozen_set")
    try:
        live_keys = dev.live_dev_records(state)[2]
    except (KeyError, ValueError):
        raise Refused("live_dev_manifest_unreadable") from None
    overlap = sum(1 for item in items if key_digests(item) & live_keys)
    if overlap:
        raise Refused(f"heldout_overlaps_live_dev:{overlap}")
    admitted, refused = [], []
    for item in items:
        reasons = admissible(item, perturbed_portal=True)
        if reasons:
            refused.append({"id": item["id"], "reasons": reasons})
        else:
            admitted.append(item)
    return admitted, refused, record


def variant_groups(items: Sequence[Mapping[str, Any]],
                   cost: Callable[[Mapping[str, Any], Sequence[Mapping[str, Any]]], int]
                   = lambda perturbation, group: 0
                   ) -> list[tuple[dict[str, Any], list[Mapping[str, Any]]]]:
    """The metamorphic variants grouped by perturbation (one perturbed product per group;
    ``partial_source_failure`` has one group per failed source), the transforms in turn
    (each transform's first group, then each one's second, ...), so a budget that stops
    early still leaves every transform measured it reached; within a turn (and within a
    transform) the cheaper build first (``cost(perturbation, group)``: the rows its delta
    build replays, ``variant_cost``), so a budget measures as many groups as it can."""
    from evals.oracle.perturb import TRANSFORMS, perturbation_key
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for item in items:
        if item.get("perturbation") is not None:
            groups.setdefault(perturbation_key(item), []).append(item)
    ordered = sorted(((TRANSFORMS.index(group[0]["perturbation"]["transform"]),
                       cost(group[0]["perturbation"], group), key, group)
                      for key, group in groups.items()), key=lambda g: g[:3])
    turn: dict[int, int] = {}
    ranked = []
    for index, price, key, group in ordered:
        n = turn[index] = turn.get(index, -1) + 1
        ranked.append(((n, price, index, key), dict(group[0]["perturbation"]), group))
    return [(perturbation, group) for _, perturbation, group in
            sorted(ranked, key=lambda r: r[0])]


def variant_share_seconds(left_seconds: float, items: Sequence[Mapping[str, Any]]) -> float:
    """The seconds of ``left_seconds`` the standard items leave the metamorphic variants:
    the variants' fraction of ``items`` (the admitted items the run measures)."""
    variants = sum(item.get("perturbation") is not None for item in items)
    return max(left_seconds, 0.0) * variants / max(len(items), 1)


class VariantCut(Exception):
    """The run's deadline came before a variant group's product was built (its sync or its
    projection was cut): the group is counted ``not_reached``, never ``failed``."""


# The mirror tables with no ``relation`` column: the file's own meta, the catalog and
# AUTOINCREMENT counters kept whole, and the per-capture / per-scan rows removed with the
# capture or scan they belong to (``_forget_relations``).
_MIRROR_WHOLE = frozenset({"mirror_meta", "upstream_catalog", "sqlite_sequence"})
_MIRROR_BY_PARENT = {"capture_state": ("capture_id", "capture"),
                     "absence_seen": ("scan_id", "absence_scan")}


def _forget_relations(path: Path, relations: Sequence[str]) -> None:
    """Remove every row of ``relations`` from the mirror file ``path`` (a variant's own copy)
    and its upstream pin, so the next sync captures those relations cold from the replay
    and re-pins the file to it; every other relation stays exactly as captured. A table it
    does not know fails closed (``mirror_table_unknown``), never left half-forgotten."""
    db = sqlite3.connect(path)
    try:
        with db:
            names = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
            marks = ",".join("?" * len(relations))
            for table, (column, parent) in _MIRROR_BY_PARENT.items():
                if table in names:
                    db.execute(f"DELETE FROM {table} WHERE {column} IN (SELECT {column} "
                               f"FROM {parent} WHERE relation IN ({marks}))", list(relations))
            for table in names:
                if table in _MIRROR_WHOLE or table in _MIRROR_BY_PARENT:
                    continue
                columns = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                if "relation" not in columns:
                    raise RuntimeError(f"mirror_table_unknown:{table}")
                db.execute(f"DELETE FROM {table} WHERE relation IN ({marks})", list(relations))
            db.execute("DELETE FROM mirror_meta WHERE name = 'source_digest'")
    finally:
        db.close()


def _replayed(base: Product, items: Sequence[Mapping[str, Any]],
              perturbation: Mapping[str, Any], depth: int,
              held: Sequence[str]) -> tuple[dict[str, Any], list[str]]:
    """(``relation_plan`` of ``items``, the relations of it ``base``'s mirror holds that the
    perturbation changes: the only ones a variant's delta build replays)."""
    from evals.oracle.perturb import touched_relations
    from ontology.schema import load_schema

    plan = relation_plan(load_schema(), items, depth)
    wanted = [r for r in plan["relations"] if r in set(held)]
    return plan, touched_relations(perturbation["transform"], perturbation["params"], wanted)


def variant_cost(base: Product, depth: int) -> Callable[[Mapping[str, Any],
                                                         Sequence[Mapping[str, Any]]], int]:
    """``variant_groups``' cost of a group: the rows ``base``'s mirror holds of the relations
    its delta build replays (``_replayed``)."""
    db = sqlite3.connect(f"{base.mirror_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = dict(db.execute("SELECT relation, row_count FROM relation_state"))
    finally:
        db.close()

    def cost(perturbation: Mapping[str, Any], group: Sequence[Mapping[str, Any]]) -> int:
        return sum(rows[r] for r in _replayed(base, group, perturbation, depth, list(rows))[1])
    return cost


def variant_product(base: Product, work: Path, items: Sequence[Mapping[str, Any]],
                    perturbation: Mapping[str, Any], *, depth: int,
                    clock: Callable[[], datetime], run_left: Callable[[], float]) -> Product:
    """The product for one group of variants, built as a delta over ``base`` (built
    ``incremental``): a copy of ``base``'s mirror in which only the relations the
    perturbation changes (``perturb.touched_relations`` of the group's ``relation_plan``)
    are forgotten and synced again from ``base``'s own mirror replayed perturbed
    (``MirrorReplay``, through the GET-only client, its clock moved as the replica time
    moves), then projected over ``base``'s generation with ``base``'s own capture sources,
    so only the units those relations feed are rebuilt (``ontology.project_incremental``)
    and every other relation reads as the base product read it. Its sync's budget is
    ``run_left()``, the seconds the run's deadline has left; a sync or projection that
    deadline cuts raises ``VariantCut``. Portal only (``evals.admission.reads_only_portal``
    keeps every other variant out); a base BC capture is projected unchanged. ``info``:
    ``rows_replayed`` (the rows the replay served), ``relations_replayed``, ``sync_failed``
    (touched relations the sync did not capture again, so the copy holds them empty; the
    source ``partial_source_failure`` fails on purpose is not one), ``units`` and
    ``units_rebuilt``. Its phases are the caller's one ``variant_product`` phase."""
    from evals.oracle.perturb import replica_time
    from integrations.source_mirror import SourceMirror
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from ontology.project_incremental import build_generation
    from ontology.schema import load_schema
    from ontology.types import Actor
    from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE

    if base.generation is None or base.sources is None:
        raise ValueError("variant_base_not_incremental")
    started = time.monotonic()
    transform, params = perturbation["transform"], perturbation["params"]

    def moved() -> datetime:
        return datetime.fromisoformat(replica_time(clock().isoformat(), transform, params))

    def checkpoint() -> None:
        if run_left() <= 0:
            raise VariantCut("variant_projection_cut")
    replay = MirrorReplay(base.mirror_path, perturbation)   # an unknown transform fails here
    try:
        _, touched = _replayed(base, items, perturbation, depth, replay.relations)
        own = work / "state" / CASE_RUNTIME_DIR / MIRROR_FILE
        runner.copy_mirror(base.mirror_path, own)
        if touched:
            _forget_relations(own, touched)
            client = WorkbenchReadApiClient(REPLAY_URL, FIXTURE_KEY, fetch=replay.fetch)
            with SourceMirror(own, client, base.audience, tenant=TENANT,
                              clock=moved) as mirror:
                synced = mirror.sync(Actor(TENANT, frozenset({PRINCIPAL})),
                                     relations=touched,
                                     budget_seconds=min(max(0.0, run_left()), 7 * 86400.0))
            if any(run.get("outcome") == "budget_exhausted" for run in synced["runs"]):
                raise VariantCut("variant_sync_cut")
            # A touched relation not captured again is left empty in the copy: counted, never
            # silent, but for the source partial_source_failure breaks on purpose.
            meant = {params["source"]} if transform == "partial_source_failure" else set()
            sync_failed = len(set(touched) - meant - {
                run["relation"] for run in synced["runs"] if run.get("outcome") == "captured"})
        else:
            sync_failed = 0
    finally:
        replay.close()
    with ExitStack() as stack:
        generation = build_generation(work / "projection", load_schema(),
                                      base.sources(stack, own), previous=base.generation,
                                      checkpoint=checkpoint)
    info = {"objects": (generation.report.get("counts") or {}).get("objects", 0),
            "relations_replayed": len(touched), "rows_replayed": replay.rows_served,
            "sync_failed": sync_failed,
            "units": generation.units, "units_rebuilt": generation.units_rebuilt,
            "projection": generation.mode, "captures": sorted(base.info.get("captures") or ()),
            "build_seconds": round(time.monotonic() - started, 1)}
    return Product(work / "state", base.audience, generation.store, info,
                   evidence=base.evidence, generation=generation, sources=base.sources)


def run_heldout(env: Mapping[str, str], args: argparse.Namespace, *, client: Any = None,
                clock: Callable[[], Any] | None = None,
                bc_capture: Callable[[Path, float | None], Mapping[str, Any]] | None = None,
                progress: Progress | None = None) -> dict[str, Any]:
    """The product scored on a frozen held-out set (``load_heldout``). Its standard items
    run on the run's product, as a live dev run's do (``--source fixture``: the fixture
    world's, as ``run_fixture``); each group of metamorphic variants runs on that product's
    own mirror perturbed (``variant_product``: a delta over the product's own incremental
    build, only the relations the perturbation changes replayed and re-projected), never on
    a second read of the source. Run
    pins read no oracle: each checks the item's frozen value against the pinned mirror row.
    One budget as ``run_live``: the standard items stop early enough to leave the variants
    their share of what the product build left (``variant_share_seconds``), so a slow
    standard set never starves every transform; a group the budget does not reach is
    counted ``not_reached`` (and so is one the deadline cuts mid-build, ``VariantCut``),
    never run, leaving ``eval.complete`` false, and a group whose build alone took longer
    than the whole share is counted ``over_share``."""
    from ontology.schema import load_schema
    from scripts import eval_oracle_dev as dev

    progress = progress or Progress()
    now_s = progress.clock
    deadline = progress.since + args.total_budget_seconds
    reserved = deadline - args.run_reserve_seconds
    progress.run_id = args.run_id
    state = args.state_dir if args.state_dir is not None else dev.default_state_dir(env)
    progress.enter("load")
    items, progress.refused, record = load_heldout(state, args.source, args.run_id)
    progress.frozen = {k: record.get(k) for k in ("frozen_at", "set_sha256")}
    progress.items = list(items)
    plan = relation_plan(load_schema(), items, args.link_depth)
    progress.needed, progress.cited_only = plan["needed"], plan["cited_only"]
    fixture = args.source == "fixture"
    if fixture:
        # The fixture world's product, as run_fixture's: its client and clock, no BC and
        # no session store (an empty environment: nothing credentialed is read).
        if any(getattr(args, a) is not None for a in ("mirror_db", "bc_db",
                                                     "session_projection")):
            raise ValueError("fixture_heldout_takes_no_session_state")
        env, client, clock, bc_capture = {}, fixture_client(), fixture_clock, None
        progress.configured = {"portal": "isolated-fixture"}
    else:
        progress.configured = {"portal": "live-read" if client is not None or
                               dev.portal_configured(env) else "not_configured",
                               "bc": "live-read" if bc_capture is not None or (
                                   not args.no_bc and all(env.get(n) for n in BC_ENV))
                               else "not_configured"}
    product = live_product(env, args, plan, client=client, clock=clock,
                           bc_capture=bc_capture, progress=progress,
                           run_left=lambda: reserved - now_s(), incremental=True)
    progress.product = product
    down = product.portal_down is not None
    progress.blocked = [i for i in items if down and dev.needs_portal(i)]
    progress.items = [i for i in items if not (down and dev.needs_portal(i))]
    label = "isolated-fixture" if fixture else "live-captures"
    progress.enter("items")
    share = variant_share_seconds(deadline - now_s(), progress.items)
    # A standard item cut at the share leaves the run incomplete, recorded once the variants
    # had their share: until then it must not stop them (the deadline alone does).
    _, standard = run_items([i for i in progress.items if i.get("perturbation") is None],
                            product, None, evidence_label=label, deadline=deadline - share,
                            rows=progress.rows, clock=now_s, transcripts=args.transcripts)
    cost = variant_cost(product, args.link_depth) if product.portal_down is None else \
        (lambda perturbation, group: 0)
    for n, (perturbation, group) in enumerate(variant_groups(progress.items, cost)):
        products = progress.variant_products.setdefault(
            perturbation["transform"], dict.fromkeys(("groups", "built", "failed",
                                                      "not_reached", "over_share",
                                                      "objects", "rows_replayed",
                                                      "units_rebuilt", "sync_failed"), 0))
        products["groups"] += 1
        if now_s() >= deadline:
            products["not_reached"] += 1
            if BUDGET_EXHAUSTED not in progress.incomplete:
                progress.incomplete.append(BUDGET_EXHAUSTED)
            continue
        progress.enter("variant_product")
        work, started = args.work_dir / f"variant-{n}", now_s()
        try:
            try:
                variant = variant_product(product, work, group, perturbation,
                                          depth=args.link_depth,
                                          clock=clock or (lambda: datetime.now(timezone.utc)),
                                          run_left=lambda: deadline - now_s())
            except VariantCut:
                # the deadline came mid-build: measured as far as it went, never assumed
                products["not_reached"] += 1
                if BUDGET_EXHAUSTED not in progress.incomplete:
                    progress.incomplete.append(BUDGET_EXHAUSTED)
                continue
            except Exception as exc:  # noqa: BLE001 - recorded; its items stay unmeasured
                products["failed"] += 1
                products.setdefault("errors", [])
                products["errors"] = sorted({*products["errors"], type(exc).__name__})
                if "variant_product_failed" not in progress.incomplete:
                    progress.incomplete.append("variant_product_failed")
                continue
            products["built"] += 1
            # One delta build longer than the whole share: the base build itself is too big.
            products["over_share"] += now_s() - started > share
            for key in ("objects", "rows_replayed", "units_rebuilt", "sync_failed"):
                products[key] += int(variant.info.get(key) or 0)
            if variant.info.get("sync_failed") and \
                    "variant_sync_failed" not in progress.incomplete:
                # its items are scored on a world missing a relation: never a complete run
                progress.incomplete.append("variant_sync_failed")
            progress.enter("items")
            _, incomplete = run_items(group, variant, None, evidence_label=label,
                                      deadline=deadline, rows=progress.rows, clock=now_s,
                                      transcripts=args.transcripts)
            progress.incomplete += [c for c in incomplete if c not in progress.incomplete]
        finally:
            shutil.rmtree(work, ignore_errors=True)   # one perturbed copy on disk at a time
    progress.incomplete += [c for c in standard if c not in progress.incomplete]
    if BUDGET_EXHAUSTED in progress.incomplete:
        progress.stopped = "items"
    progress.enter("report")
    return heldout_result(progress, args)


def heldout_result(progress: Progress, args: argparse.Namespace, *,
                   stopped: str | None = None) -> dict[str, Any]:
    """The held-out record of what ``progress`` holds: the headline pass rate
    (``eval.held_out``, ``attempted``) and ``by_family`` over the standard items only,
    every variant beside them in ``eval.by_variant``, answer counts, the set's counts and
    its admission refusals by reason; aggregates, codes and hashes only, never an item id,
    a value or a label."""
    stopped = stopped or progress.stopped
    rows, product = list(progress.rows), progress.product
    info = product.info if product is not None else {}
    down = product is not None and product.portal_down is not None
    incomplete = list(progress.incomplete)
    if stopped is not None and BUDGET_EXHAUSTED not in incomplete:
        incomplete.append(BUDGET_EXHAUSTED)
    mirror = mirror_block(info, progress.needed, progress.cited_only)
    if "pin_sync" in info:
        mirror["pin_sync"] = info["pin_sync"]
    if product is not None and (mirror["needed_unfinished"] or not info.get(
            "relations_complete")) and not down:
        incomplete.append("mirror_incomplete")
    if mirror["unfinished"]:
        incomplete.append(bl.MIRROR_UNPLANNED)
    fixture = args.source == "fixture"
    result = report(rows, progress.items, split="held_out",
                    evidence_label="isolated-fixture" if fixture else "live-read",
                    run_id=progress.run_id, product=info, incomplete=incomplete,
                    with_ids=False, sources=progress.configured)
    result["schema"] = HELDOUT_SCHEMA
    # The headline is the product's score on standard items: a metamorphic variant passing
    # (or failing) beside its parent stays in by_variant and moves no headline rate.
    standard_rows = [row for row in rows if row["variant"] == "standard"]
    standard = bl.eval_block(standard_rows, split="held_out",
                             evidence_label=result["evidence_label"],
                             run_id=progress.run_id, items_digest="")
    result["eval"].update({key: standard[key] for key in ("held_out", "attempted",
                                                          "by_family")},
                          headline_items=len(standard_rows))
    result["summary_line"] = bl.summary_line(result["eval"])
    variants = [i for i in progress.items if i.get("perturbation") is not None]
    result["set"] = {"run_id": progress.run_id, "source": args.source, **progress.frozen}
    result["counts"] = {
        "admitted": len(progress.items) + len(progress.blocked),
        "refused": len(progress.refused),
        "standard": len(progress.items) - len(variants), "variants": len(variants),
        "measured": len(rows),
        "variants_measured": sum(row["variant"] != "standard" for row in rows),
        "blocked_upstream": len(progress.blocked),
        "not_measured": len(progress.items) - len(rows)}
    result["admission_refused"] = runner.refused_summary(progress.refused, "held_out")
    result["variant_products"] = dict(sorted(progress.variant_products.items()))
    result["blocked_upstream"] = [{"part": "portal_product", "source": "portal-workbench-api",
                                   **product.portal_down}] if down else []
    result["product"]["captures"] = info.get("captures")
    if "session_projection" in info:
        result["product"]["session_projection"] = info["session_projection"]
    result["eval"].update(
        mirror=mirror, pass_rule="portal_outage" if down else "all_sources",
        measured=len(rows), blocked_upstream=len(progress.blocked),
        answer=answer_block(rows, len(progress.blocked)), phases=progress.phases(),
        stopped=None if stopped is None else {"phase": stopped, "reason": BUDGET_EXHAUSTED})
    result["outage_unmeasured"] = "portal_outage" if down and not rows and \
        stopped is None else None
    return result


def live_product(env: Mapping[str, str], args: argparse.Namespace, plan: Mapping[str, Any], *,
                 client: Any, clock: Callable[[], Any] | None,
                 bc_capture: Callable[[Path, float | None], Mapping[str, Any]] | None,
                 progress: Progress, run_left: Callable[[], float],
                 incremental: bool = False) -> Product:
    """A live run's product over ``plan`` (``relation_plan``) in ``args.work_dir``: the
    verifier session's projection when this session committed it (``session_product``),
    else its own GET-only Portal sync and BC capture (``build_product``, within
    ``run_left()`` seconds), each only when its credentials are set (``client`` and
    ``bc_capture``: injected in tests). ``incremental``: its own build is a generation a
    variant's delta reuses (``build_product``; the session's projection always is one)."""
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from ontology.schema import load_schema
    from scripts import eval_oracle_dev as dev

    if client is None and dev.portal_configured(env):
        client = WorkbenchReadApiClient(env["PLX_WORKBENCH_API_URL"],
                                        env["PLX_WORKBENCH_API_KEY"])
    if bc_capture is None and not args.no_bc and all(env.get(n) for n in BC_ENV):
        from scripts.eval_unfamiliar_sample import capture_bc
        bc_capture = lambda db, seconds: capture_bc(env, db, seconds)  # noqa: E731
    bc_sets = None
    if args.bc_db is not None:
        from scripts.eval_unfamiliar_sample import BC_INVOICE_SETS, PROCESS, bc_entity_sets
        bc_sets = bc_entity_sets(load_schema(), PROCESS, BC_INVOICE_SETS)  # what capture_bc reads
    session = None
    if args.session_projection is not None:
        from integrations import bc_read
        from scripts.ontology_projection_probe import open_session
        session = open_session(str(args.session_projection), bc_read.session_fresh_since(env))
    if session is not None:
        return session_product(args.work_dir, plan["relations"], session,
                               bc_environment=env.get("BC_ENVIRONMENT"), client=client,
                               pin_relations=plan["cited"], pins_after=plan.get("pins_after"),
                               budget_seconds=max(1.0, run_left()),
                               clock=clock, enter=progress.enter)
    return build_product(client, args.work_dir, plan["relations"],
                         budget_seconds=max(1.0, run_left()), clock=clock,
                         bc_capture=bc_capture, bc_environment=env.get("BC_ENVIRONMENT"),
                         first=plan["first"], mirror_db=args.mirror_db, bc_db=args.bc_db,
                         bc_sets=bc_sets, project=plan["projected"], run_left=run_left,
                         incremental=incremental, enter=progress.enter)


def watchdog(progress: Progress, args: argparse.Namespace, at: float,
             hard_exit: Callable[[int], Any], poll: float = 0.5,
             record: Callable[..., dict[str, Any]] = live_result) -> threading.Event:
    """Past ``at`` (``progress.clock``) a phase no budget reaches is still running: print the
    record of what the run measured, flush, abandon every thread's OCR children and folders
    (``child_process.abandon``) and ``hard_exit(3)`` (``os._exit``: the phase cannot be
    interrupted, and a killed run would leave the verifier a timeout). The record
    (``record``: ``live_result``, or ``heldout_result`` for a held-out run) is printed once
    (``progress.emitted``); setting the returned event stops the watch."""
    from integrations import child_process
    done = threading.Event()

    def watch() -> None:
        while not done.wait(poll):
            if progress.clock() < at:
                continue
            with progress.lock:
                if progress.emitted is not None:
                    return
                progress.emitted = 3
                try:
                    result = record(progress, args, stopped=progress.phase or "start")
                except Exception as exc:  # noqa: BLE001 - the record of a cut run, whatever it held
                    result = {"schema": SCHEMA, "evidence_label": "live-read",
                              "values_published": False, "run_id": progress.run_id,
                              "eval": {"complete": False, "incomplete": [BUDGET_EXHAUSTED],
                                       "measured": len(progress.rows),
                                       "items_total": len(progress.items),
                                       "phases": progress.phases(),
                                       "stopped": {"phase": progress.phase or "start",
                                                   "reason": BUDGET_EXHAUSTED},
                                       "record_error": type(exc).__name__}}
                print(json.dumps(result, indent=1, sort_keys=True), flush=True)
            child_process.abandon()   # os._exit runs no atexit: stop OCR children first
            hard_exit(3)
            return
    threading.Thread(target=watch, name="eval-dev-baseline-watchdog", daemon=True).start()
    return done


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         live_build: Callable[..., Any] | None = None, live_client: Any = None,
         live_clock: Callable[[], Any] | None = None,
         live_now: Callable[[], datetime] | None = None,
         live_bc_capture: Callable[[Path, float | None], Mapping[str, Any]] | None = None,
         monotonic: Callable[[], float] = time.monotonic,
         hard_exit: Callable[[int], Any] = os._exit,
         hard_stop_grace: float = HARD_STOP_GRACE_SECONDS) -> int:
    """The CLI. Tests inject the live sources (``live_*``; ``live_now``: the wall clock the
    oracle draws at and the ``auto`` run id reads), the run's ``monotonic`` clock
    and the watchdog's ``hard_exit`` (``watchdog``) with its ``hard_stop_grace``."""
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_dev_baseline",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    fixture = commands.add_parser("fixture", help="keyless: evals/dev on the fixture world")
    fixture.add_argument("--write-baseline", action="store_true")
    fixture.add_argument("--write-report", action="store_true")
    fixture.add_argument("--transcripts", type=Path, default=None,
                         help="keep sealed transcripts and scores here (outside the repo)")
    live = commands.add_parser("live", help="credentialed, GET only; counts only")
    live.add_argument("--run-id", default="auto", help="'auto' = live-<UTC date>")
    live.add_argument("--count", type=int, default=30)
    live.add_argument("--mailbox")
    live.add_argument("--folder")
    live.add_argument("--sharepoint-path", action="append", default=[])
    live.add_argument("--state-dir", type=Path, default=None,
                      help="host state directory outside the repository (default the "
                           "held-out state directory): frozen held-out key digests are read "
                           "and this run's digest manifest is kept there")
    heldout = commands.add_parser(
        "heldout", help="credentialed: the product on a frozen held-out set; aggregates only")
    heldout.add_argument("--source", choices=("live", "fixture"), default="live",
                         help="the frozen set's source (fixture: the fixture world's product)")
    heldout.add_argument("--run-id", default=None,
                         help="the frozen set (default scripts.eval_oracle_dev's HELDOUT_RUN_ID)")
    heldout.add_argument("--state-dir", type=Path, default=None,
                         help="host state directory where scripts.eval_oracle_dev heldout froze "
                              "the set (default $PLX_EVAL_HELDOUT_DIR or "
                              "$XDG_STATE_HOME/plx-workbench/eval-heldout); it must lie under "
                              "$PLX_EVAL_HELD_OUT_DIR (evals.runner)")
    heldout.add_argument("--transcripts", type=Path, default=None,
                         help="keep sealed transcripts and redacted scores here (under "
                              "$PLX_EVAL_HELD_OUT_DIR)")
    for sub in (live, heldout):
        sub.add_argument("--work-dir", type=Path, required=True,
                         help="throwaway directory (the verifier's {tmp})")
        sub.add_argument("--min-items", type=int, default=20)
        sub.add_argument("--total-budget-seconds", type=float, default=1800.0)
        sub.add_argument("--run-reserve-seconds", type=float, default=600.0,
                         help="the end of the budget kept for projection and items: the "
                              "oracle's reads, the mirror sync and the BC capture stop before it")
        sub.add_argument("--mirror-db", type=Path, default=None,
                         help="Portal mirror to top up (the verifier's {session_state}); "
                              "default a throwaway one in --work-dir")
        sub.add_argument("--bc-db", type=Path, default=None,
                         help="BC capture store to top up (the verifier's {session_state}); "
                              "default a throwaway one in --work-dir")
        sub.add_argument("--session-projection", type=Path, default=None,
                         help="the verifier session's projection (ontology.session_projection, "
                              "{session_state}): opened read-only when this session committed "
                              "it, else the run builds its own product")
        sub.add_argument("--no-bc", action="store_true")
    for sub in (fixture, live, heldout):
        sub.add_argument("--link-depth", type=int, default=DEFAULT_LINK_DEPTH)
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    progress = Progress(monotonic)
    watching = None
    try:
        if args.command == "fixture":
            with tempfile.TemporaryDirectory(prefix="plx-eval-baseline-") as work:
                result = run_fixture(Path(work), transcripts=args.transcripts,
                                     link_depth=args.link_depth)
            if args.write_baseline:
                BASELINE_JSON.parent.mkdir(parents=True, exist_ok=True)
                # Wall times differ on every run: the committed baseline keeps the rest.
                BASELINE_JSON.write_text(json.dumps(measuring.without_wall_clock(result),
                                                    indent=1, sort_keys=True) + "\n",
                                         encoding="utf-8")
            if args.write_report:
                REPORT_MD.write_text(render_markdown(result), encoding="utf-8")
            min_items = 1
        elif args.command == "heldout":
            from scripts.eval_oracle_dev import HELDOUT_RUN_ID
            args.run_id = args.run_id or HELDOUT_RUN_ID
            args.work_dir.mkdir(parents=True, exist_ok=True)
            watching = watchdog(progress, args, progress.since + args.total_budget_seconds
                                + hard_stop_grace, hard_exit, record=heldout_result)
            result = run_heldout(env, args, client=live_client, clock=live_clock,
                                 bc_capture=live_bc_capture, progress=progress)
            min_items = args.min_items
        else:
            args.work_dir.mkdir(parents=True, exist_ok=True)
            watching = watchdog(progress, args, progress.since + args.total_budget_seconds
                                + hard_stop_grace, hard_exit)
            result = run_live(env, args, build=live_build, client=live_client,
                              clock=live_clock, bc_capture=live_bc_capture,
                              progress=progress, **({"wall_clock": live_now} if live_now
                                                    else {}))
            min_items = min(args.min_items, args.count)
    except Refused as exc:
        # A held-out set the run will not score: its code, never a value (exit 3).
        with progress.lock:
            if progress.emitted is None:
                progress.emitted = 3
                print(json.dumps({"schema": HELDOUT_SCHEMA, "split": "held_out",
                                  "run_id": args.run_id, "values_published": False,
                                  "evidence_label": "isolated-fixture" if args.source ==
                                  "fixture" else "live-read",
                                  "adapter_status": "isolated-fixture" if args.source ==
                                  "fixture" else "live-unverified",
                                  "refused": str(exc)}, indent=1, sort_keys=True))
            return progress.emitted
    except (runner.RunnerError, scoring.ScoreError, ValueError, OSError, RuntimeError) as exc:
        with progress.lock:
            if progress.emitted is None:
                progress.emitted = 1
                print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
            return progress.emitted
    finally:
        if watching is not None:
            watching.set()
    with progress.lock:
        if progress.emitted is None:   # else the watchdog recorded the run already
            progress.emitted = verdict(result, min_items=min_items)
            print(json.dumps(result, indent=1, sort_keys=True))
        return progress.emitted


if __name__ == "__main__":
    raise SystemExit(main())
