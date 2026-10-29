"""Unfamiliar-transaction find eval: seeded cross-system roots, answered by the product.

Per run, inside one total time budget
(``--total-budget-seconds``, default 600; every source read and the mirror sync check it,
and a run that hits it stops cleanly and is labelled incomplete). The build takes its
``--build-share`` including the projection (``--projection-reserve-seconds`` is kept back
from the sync for it), the finds keep ``--find-reserve-seconds``, and the sampling time
between is sliced per kind (``cross_sample.KindClock``: the read guard stops a kind at its
slice end, a first slice is at most the kind's fair share and every kind its slice cut gets
a second one of what is left), so one slow kind never starves the others:

1. **Sample (oracle side).** ``evals.oracle.cross_sample.sample`` draws >= 25 real
   transactions across the kinds sales order, purchase order, assembly order, receipt, BC
   invoice, purchase-order three-way, mail and SharePoint document, seeded by the run id,
   through the oracle's own GET-only source ports (wired by
   ``scripts/eval_oracle_dev.build_live``). Each question's
   identifier is taken from a record of **another** system than the root's (a BC document,
   an FM row, a Portal row, a mail or a SharePoint file name), and the oracle confirms that
   it names exactly that root. The sampler lives in ``evals/oracle`` under the oracle
   import ban: it never reads the projection.
2. **Answer (product side).** The identifier alone goes to ``plx.find`` (then ``plx.object``
   when a candidate carries no citation) through the case host's tool table
   (``operating.ontology_host.tool_table``), so a resolver that later backs ``plx.find``
   plugs in unchanged. The product reads a projection store: ``--store`` (a maintained
   one, reused as is) or ``--build-store DIR`` (a GET-only Portal mirror sync of only the
   relations the curated schema binds for the sampled root kinds, derived from the schema
   by ``required_relations``, then a projection generation; DIR serves one run unless
   ``PLX_WORKBENCH_STATE_DIR`` names a state directory, see ``build_projection``). With BC
   configured the product also captures BC itself (``capture_bc``: the GET-only BC client,
   the entity sets the schema binds for the BC roots and the three-way graph, the roots
   of every company first, each in its share of the BC budget) on the rest of the build
   budget, never less than its floor and never into the finds' or the projection's reserve
   before the run's deadline (``bc_budget``), and projects that capture in the same
   generation. ``--session-projection`` (the verifier session's
   ``ontology.session_projection``): when this session committed it, the run neither
   syncs, captures nor projects (``product_from_session``: the dev baseline's
   ``session_product``, the producer's mirror read in place), so the build's share of the
   budget goes to sampling; else it builds into ``--build-store`` as above. The product
   block's ``completeness`` is the one figure every report uses
   (``scripts.mirror_measure.completeness``: ``complete_bound_relation_count /
   bound_total``, here over the required relations, ``scope: eval_required``). Relations
   the last daily report (``--previous-report``) names unstable are not synced, leave
   ``bound_total`` and carry the binding gap ``capture_unstable_ids``.
3. **Score.** Per item, on the root's own relation (its system of record), with
   find_root's gate (a root-making match reason, ``ontology_tools.ROOT_REASONS``, and the
   kind's literal comparison, ``ontology_tools.exact_match``): ``found_exact`` (the root
   passes the gate and is the only candidate in that relation that does), ``ambiguous``
   (others pass it too), ``found_lead`` (the root is a candidate, but only as a lead),
   ``wrong`` (candidates, none the root), ``not_found``. Candidates in
   other systems (the same order in BC, a Sage copy) never merge (R3); they are counted as
   ``other_system_candidates``, not penalised. An item is ``excluded`` from the rate, with
   a reason and a count, when the product never captured its root's relation
   (``product_not_holding_root``), the product is unavailable or the budget ran out. An
   item a failed upstream read touched is ``blocked`` (``blocked: upstream`` with the HTTP
   status and error code): the Portal failed the mirror sync of a relation it needs (or
   the whole sync), or the oracle's own read of it failed. Blocked items are never scored
   and never dropped; ``eval.blocked`` counts them apart from wrong answers and exclusions,
   with blocked items, blocked kinds and blocked three-way aspects tallied separately.

**Three-way kind** (``po_three_way``). The sampler draws Portal
purchase orders that have a Portal receipt or a sampled BC purchase invoice
(``cross_sample.three_way_candidates``), takes the identifier from another system's record
as above, and attaches the oracle's own reading (``evals/oracle/three_way.py``: stages
purchase_order / receipt / vendor_invoice, sides ordered-vs-received and
received-vs-invoiced, as enums). The product resolves the identifier with ``plx.find``
(find_root's gate; a root it cannot resolve exactly is ``root_not_resolved``), then asks
``plx.process`` (process ``purchase_to_invoice``) from the same case host tool table. Each
aspect the oracle decides and whose sources the product holds is compared:
``three_way_wrong`` when any decided product answer differs (a wrong stage or side; the
reason names it),
``three_way_undecided`` when none differs but the product left one undecided (a typed
gap is honest, not wrong, but not a pass; the item names each open aspect's product reason
in ``undecided_aspects``: a stage's gap reason, or the first gap naming a side's roles),
``three_way_correct`` otherwise. Its kind counts them in ``eval.by_kind``
(``undecided.<aspect name>.<reason>`` -> undecided items, the ``MAX_UNDECIDED_REASONS``
most frequent; the rest summed in ``undecided.other``), so a run names its live cause. An aspect whose
own source relation the Portal failed to sync is ``blocked`` alone (``aspects.blocked``,
``blocked_aspects``) and the other aspects are still scored: a wrong one keeps the item
``three_way_wrong`` (the run is then incomplete with ``aspects_blocked_upstream``);
otherwise the item is ``blocked`` (it could hide a wrong answer, so it never passes), and
its kind never passes while any of its items has a blocked aspect. Aspects whose
sources the product never captured (BC today) are counted per row (``aspects.not_held``),
not scored. The purchase_order stage only says the resolved root exists, so an item is
scored only on at least one other held, decided aspect; an item the oracle cannot read or
decide beyond it is ``excluded`` (``oracle_blocked``, ``oracle_undecided``), as is one whose
other aspects the product never captured (``product_not_holding_aspects``), apart from
wrong. Three-way rows stay out of the identity ``ranking`` (success@1, MRR). The projection then also syncs the Portal relations
of every object type the process graph names (``process_relations``).

Pass rates (``found_exact`` or ``three_way_correct`` over scored items, ``x/y``) are
reported by kind and by the path
that answered: ``untemplated`` (the generic ``plx.find``, the only path today),
``plan-family`` (a plan executed by ``plx.investigate``) and ``scripted`` (a kind case
script); the latter two stay ``0/0`` until such a path answers. The top-level ``eval``
block (rates, product state, exclusions) is the shape the live verifier lifts into
``results/latest.json`` (the live verifier ``eval_blocks``); its ``ranking`` counts
found_exact, found_lead and success@1 (rates over the block's ``items``, sampled, and
``scored``) and gives MRR over the scored items, from the root's plx.find rank.

Per kind (``eval.by_kind``): candidates tried, references, items,
scored, each outcome (a zero count left out), and one ``status``: ``passed`` (at least one
scored item found_exact, for the three-way kind three_way_correct), ``no_found_exact``
(``no_correct_answer``),
``unscored`` (items, all excluded), ``blocked`` (items, none scored, blocked upstream), or
for a kind without items
``empty`` (its stratum yielded no question: 0 candidates or none with a usable reference),
``no_candidate_in_sample`` (a mail or SharePoint sample whose readable remainder was read
to its end or cut only by the sampler's own caps, read once more further when capped, named
no root: a sample of what the reader can read, not unreadable; ``withheld`` counts what the
audience or ACL held back), ``not_held`` (the product holds no root of it),
``not_configured`` or ``blocked`` (``blocked: upstream`` when its source read failed, e.g. a
Portal non-2xx; ``budget`` when the run budget stopped it; ``unreadable`` when it read
nothing but withheld items or a gap other than the sampler's own caps cut it; an
upstream block carries ``http_status`` and ``error_code`` when the source gave them). A kind
without items names the sources it tried in ``eval.sources_tried`` (relation columns, BC
entity sets, ``mail``, SharePoint paths; names only, a shape the verifier keeps; at most
``MAX_SOURCES_TRIED`` per kind, a longer list counted in ``eval.sources_tried_total``), and in
``eval.missing_reference`` the reference sources (other relation.column) it looked its roots'
identifiers up in and found no record in, with a typed ``cause`` and counts
(``missing_reference_cause``): ``budget_exhausted`` when the shared BC probe budget refused
any BC probe of it (what was not asked measures no mismatch), else ``bc_source_mismatch`` when
its BC probes completed, or it stopped on ``cross_sample.BC_MISS_STOP`` consecutive misses,
and BC held no copy of the roots' numbers (gap-b01a86b2f438: the live
Portal sales orders), else ``no_reference``. A Portal
outage never crashes the run: the mirror sync's ``source_unavailable`` leaves the product
unavailable with that status, and every item is then ``blocked: upstream``; with a state
directory (``PLX_WORKBENCH_STATE_DIR``) the product is still built from the BC, mail and
SharePoint captures held there and only the Portal items are ``blocked: upstream``.

The run fails (exit 3; ``eval.fail_reasons`` names why) unless every kind in ``KINDS`` is
``passed`` or typed blocked, with at least 7 passed
(``eval.kinds_missing`` names the kinds not passed; ``blocked`` never counts as a pass). A
typed block (``TYPED_BLOCKS``, the acceptance bar) is a declared cause, reported per kind as
``eval.by_kind.<kind>.typed_block`` and in ``eval.kinds_typed_blocked`` with its code:
``mail`` blocked ``search_refused`` (``later_ask:graph-mail-directory-consent``),
``receipt`` empty ``no_reference`` while the served ``app_receipt`` read 0 rows
(``unserved:public.app_receipt``), and ``sales_order`` empty ``no_reference`` while the
reporting API's replica holds no Portal twin of a BC order
(``later_ask:workbench-api-replica-target``, gap-b01a86b2f438: the replica is staging while
BC orders drain from UAT). Only ``receipt`` (``TYPED_PASSES``) may stay typed blocked
and pass; while ``mail`` or ``sales_order`` is typed blocked the run reads blocked, never
pass: it exits 0 with ``eval.blocked_cause`` (one code, the verifier's enum shape; a second
typed kind's code is listed in ``eval.blocked_also``) and the live check reads ``blocked``
(``blocked_when``); ``mail`` is typed blocked only when the generator read it ``blocked:
upstream`` (the tenant refused every query, none answered, failed or scanned; the rule
``eval.oracle_dev`` applies), ``sales_order`` only when its Portal roots were read, no BC
order was a Portal twin, its BC probes completed (none refused by the budget) with no read
failed (``reference_read_gap``, ``bc_read_failed``) and it got no reference
(``missing_reference`` cause ``bc_source_mismatch``); any other unresolved kind fails. It also fails
when the product is unavailable or its projection holds no object, when fewer than 25
items were sampled or fewer than ``--min-scored`` (20) were scored, when the budget cut the
run, when wrong answers exceed 1/10 of the scored items (``eval.wrong_within_bound``), or
when row data leaked (``eval.pass_rule`` ``every_kind``). When the Portal failed the
product's whole mirror sync (``portal_outage``) the run is still
measured on what the product captured itself: each stratum blocked before any item counts
its even share of ``--count`` (summed in ``eval.blocked.kinds_quota``) as sampled and
blocked upstream in ``eval.find_root`` / ``eval.answer``, and names no
``eval.sources_tried`` (the Portal's failure is named once, in ``eval.product`` and each
kind's block); a kind blocked upstream or without items is excused,
a kind whose items were asked must still pass, and instead of the item floors at least one
BC-rooted item (``eval.bc_rooted``) must be scored; such a run never passes: it exits 0
with ``eval.blocked_cause`` ``upstream_outage`` and the live check reads it ``blocked``
(gap-51ebb6c78e90: green means rule ``every_kind``, which the spec pins).

Answer correctness: ``eval.answer`` (the three-way answers) and
``eval.find_root`` (root lookup, reported apart: it is not three-way success) put every
sampled item in one bucket: ``blocked_upstream``, ``not_held``, ``unscored_other``,
``product_error`` (``plx.find`` or ``plx.process`` returned a tool error: never an abstention,
and the run fails) or ``scored``, and a scored one is ``correct``, ``wrong``, ``abstain_ok`` (three-way undecided:
a typed gap) or ``missed`` (ambiguous, lead only, not found, root not resolved), with the unscored items' ``causes``
(upstream outage, projection timeout, missing bindings, host misconfiguration, run budget,
oracle); a count that is zero is left out. The live verifier sums these blocks into
``answer_correctness`` and publishes accuracy (correct/scored) beside coverage
(scored/sampled). Output: kinds, edge names,
outcomes, reason codes, counts and 16-hex hashes; never an identifier, key, citation or
row value. ``row_data_check`` re-reads the serialized report for every identifier, key
value, citation and candidate text the run saw (the ``operating.ingestion.row_data_check``
convention).

Evidence: ``live`` is ``live-read`` and stays ``live-unverified`` until the verifier records
a pass; ``fixture`` (keyless) is ``injected-transport`` over ``evals/oracle/fixture_world.py``.

Usage::

    python3 -m scripts.eval_unfamiliar_sample live (--build-store DIR
        [--session-projection DIR] | --store FILE) [--run-id auto] [--count 28]
        [--total-budget-seconds 600] [--no-bc]
        [--mailbox ADDR --folder inbox | --mail-scope FILE] [--sharepoint-path /Documents ...]
    python3 -m scripts.eval_unfamiliar_sample fixture [--run-id ID] [--no-product]
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote

from evals import measure as measuring
from evals.oracle.cross_sample import (BLOCKED_FIELDS, DOCUMENT_KINDS, KINDS, RECORD_KINDS, THREE_WAY,
                                       THREE_WAY_ROOT, Item, KindClock, Sample, public_row,
                                       sample)
from evals.oracle.generator import BC_DOCUMENT_STRATUM, BC_INVOICE_SETS, PORTAL_STRATA
from evals.oracle.source import SOURCE_FAILURE_CAUSES, read_failure_label
from evals.oracle.three_way import ASPECTS
from evals.score import canonical, row_identity
from integrations.bc_read import bc_entity_sets, capture_unit, company_key
from integrations.source_mirror import upstream_failures
from scripts.eval_oracle_dev import mail_scope_target

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "plx-eval-unfamiliar-sample/v1"
METRIC = "unfamiliar_find_root/v1"
OUTCOMES = ("found_exact", "found_lead", "ambiguous", "wrong", "not_found")
THREE_WAY_OUTCOMES = ("three_way_correct", "three_way_undecided", "three_way_wrong",
                      "root_not_resolved")
PASSING = frozenset({"found_exact", "three_way_correct"})
# Outcomes outside the rate: excluded (product or oracle could not ask) and blocked (a
# failed upstream read touched the item); both are counted apart from wrong answers.
UNSCORED = frozenset({"excluded", "blocked"})
# Answer correctness: a scored outcome is correct, wrong (a
# confident answer that disagrees with the oracle), abstain_ok (the product states the gap or
# the ambiguity instead of choosing) or missed (no usable answer where one exists). Accuracy is
# correct over scored, so abstaining never raises it.
ANSWER_CLASS = {"found_exact": "correct", "three_way_correct": "correct",
                "wrong": "wrong", "three_way_wrong": "wrong",
                # A typed gap is an
                # honest abstention; the oracle confirms each identifier names one root, so
                # an ambiguous find is a miss.
                "three_way_undecided": "abstain_ok", "ambiguous": "missed",
                "found_lead": "missed", "not_found": "missed", "root_not_resolved": "missed"}
ANSWER_COUNTS = ("sampled", "blocked_upstream", "not_held", "unscored_other",
                 "product_error", "scored", "correct", "wrong", "abstain_ok", "missed")
# A product tool call that returned an error (plx.find or plx.process): counted apart as
# product_error, never an abstention or a miss, and the run fails (as eval_bc_three_way).
TOOL_ERROR_PREFIX = "tool_error_"
# find outcomes that leave the root choice to a person (ENG-11 review items)
PERSON_CHOICE = ("ambiguous", "found_lead")
BC_ROOT_KINDS = frozenset({"bc_invoice", BC_DOCUMENT_STRATUM})   # items rooted in BC
NOT_HELD_REASONS = frozenset({"product_not_holding_root", "product_not_holding_aspects"})
MAX_WRONG_SHARE = (1, 10)  # wrong answers (find and three-way) <= 1/10 of the scored items
BLOCK_DETAIL = ("http_status", "error_code")  # what a blocked item or kind may say
# A published source name: identifier-shaped (relation.column, bc:EntitySet, a drive path).
_SOURCE_NAME = re.compile(r"[A-Za-z0-9_.:/\-]{1,64}")
PROCESS = "purchase_to_invoice"  # the plx.process graph the three-way kind asks
ROOT_STAGE = "stage:purchase_order"  # reached whenever the root resolves: never scored alone
# The answering path -> bucket. plx.find and plx.process are generic tools: untemplated.
PATH_BUCKET = {"find": "untemplated", "process": "untemplated", "plan": "plan-family",
               "script": "scripted"}
BUCKETS = ("scripted", "plan-family", "untemplated")
FIXTURE_RUN_ID = "fixture-unfamiliar-2026-10-01"
EVAL_PRINCIPAL = "eval:oracle"
EVAL_TENANT = "eval"
MIRROR_PRINCIPAL = "service:eval-unfamiliar-mirror"
FIND_LIMIT = 50
FIND_PAGES = 4
MIN_ITEMS = 25
MAX_READ_FAILURES = 5  # names listed (a failing kind also names its sources in
# eval.sources_tried, inside the same verifier detail cap); the total is separate
MAX_UNDECIDED_REASONS = 6  # aspect/reason counts per kind in eval.by_kind; the rest apart
UNDECIDED_PREFIX = "undecided."
MAX_SOURCES_TRIED = 4  # names per kind in eval.sources_tried; a longer list's count is apart
MIN_SCORED = 20
KIND_PASSED = "passed"  # the one per-kind status that passes
# A kind left without items for a declared, owner-known cause is typed blocked: its line in
# eval.by_kind carries the block code, it never counts as resolved, and under every_kind the
# run passes with every other kind passed or a TYPED_PASSES one typed blocked (the acceptance bar,
# gap-ca4afaf72198). kind -> (by_kind status, reasons, block code).
# Any other unresolved kind, or a typed one whose condition no longer holds, still fails.
# A typed block is a diagnosis; only the kinds in TYPED_PASSES may stay typed blocked and
# still pass. Another typed kind (mail) makes the run read blocked.
TYPED_BLOCKS = {
    # Graph mail search refused for most roots while the directory consent is a later_ask
    # (only when the generator read the kind blocked upstream, ``TYPED_BLOCK_UPSTREAM``: the
    # tenant refused every query and nothing was answered, failed or scanned; mostly refused
    # with documents found, or a refusal by our own grammar or read-only guard, is a break)
    "mail": ("blocked", ("search_refused",), "later_ask:graph-mail-directory-consent"),
    # receipts carry no cross-system reference until app_receipt is served (decision
    # 2026-10-08-unfamiliar-receipt-reference): only while the served app_receipt read 0 rows
    "receipt": ("empty", ("no_reference", "no_reference_edge"), "unserved:public.app_receipt"),
    # gap-b01a86b2f438: the reporting API's replica is plx-postgres-staging while BC orders
    # drain from UAT, so BC holds no copy of a sampled Portal order (later_ask
    # workbench-api-replica-target); only on the measured mismatch (``TYPED_BLOCK_REPLICA``)
    "sales_order": ("empty", ("no_reference",), "later_ask:workbench-api-replica-target"),
}
TYPED_BLOCK_UNSERVED = {"receipt": "public.app_receipt"}
TYPED_BLOCK_UPSTREAM = frozenset({"mail"})   # blocked only where the generator says upstream
# or where its capture scope blocked every mailbox (``blocked: scope``, gap-f713876847b6):
# one the people-exclusion guard cannot clear waits on the excluded people's object ids,
# one outside the eval's mail grant (access record 64fca27acc88) on that grant.
TYPED_BLOCK_SCOPE = {"excluded-people-oids": "later_ask:excluded-people-oids",
                     "mail-scope-grant": "later_ask:mail-scope-grant"}
# A mailbox of a capture scope as published: its scope id and folder, never its address.
_MAIL_SCOPE_UNIT = re.compile(r"mailbox-[1-9][0-9]{0,2}")
_MAIL_SCOPE_SOURCE = re.compile(r"mail:mailbox-[1-9][0-9]{0,2}(/[a-z]{0,24})?")
TYPED_BLOCK_REPLICA = frozenset({"sales_order"})   # Portal roots read, BC disjoint from them
TYPED_PASSES = frozenset({"receipt"})   # so a pass needs 7 of the 8 kinds passed
DEFAULT_TOTAL_BUDGET = 600.0
# Of the build's share, kept back from the mirror sync and BC capture for the projection
# (live 1303d122: a 300 s sync then a 112 s projection left sampling 63 s).
DEFAULT_PROJECTION_RESERVE = 120.0
# Of the whole run, kept back for the finds: sampling stops there (live 1303d122: 28 finds
# took seconds of a 120 s reserve while sampling was starved).
DEFAULT_FIND_RESERVE = 60.0
DEFAULT_BUILD_SHARE = 0.5
# Of the build budget, the share the product's own BC capture keeps when BC is configured;
# the Portal sync runs first on the rest (a Portal outage fails it at once, leaving BC
# nearly the whole build budget). The share is BC's floor: a Portal sync that overruns its
# own share never takes it (live 2026-10-07: BC got ~69 s of 396 s and held no root).
BC_CAPTURE_SHARE = 0.25
BC_ENV = ("BC_TENANT_ID", "BC_CLIENT_ID", "BC_CLIENT_SECRET", "BC_ENVIRONMENT")
# A BC root the product does not hold, by its own BC capture's status: cut by the budget
# (partial, no_budget) or its read failed, else (captured) the schema never bound it.
BC_NOT_HELD_CAUSE = {"partial": "projection_timeout", "no_budget": "projection_timeout",
                     "failed": "upstream_outage", "captured": "missing_bindings"}
# A root set's state in a company in the product's BC capture (``root_set_states``) -> the
# cause of a BC root of that set and company the product does not hold: read whole (or not
# exposed), the schema never bound it; cut by the budget; failed upstream; anything else is
# no gap the product could fix.
ROOT_SET_CAUSE = {"complete": "missing_bindings", "not_exposed": "missing_bindings",
                  "budget": "projection_timeout", "upstream": "upstream_outage"}
# A root unit's capture reason (or the call's stop, for a unit never reached) -> its state.
ROOT_UNIT_STATE = {"budget": "budget", "record_cap": "budget", "not_exposed": "not_exposed",
                   "throttled": "upstream", "source_unavailable": "upstream",
                   "source_refused": "upstream", "nextlink_refused": "upstream"}
# The BC capture figures eval.product carries (as bc_<field>): units reached, not the planned
# units (a constant per book map), to keep the record under the verifier cap.
BC_PRODUCT_FIELDS = ("units_reached", "units_complete", "seconds", "stopped_reason")
SAMPLING_EXCLUSIONS = ("not_unique", "no_reference", "no_reference_edge", "reference_read_gap",
                       "bc_probe_budget", "bc_miss_stop", "bc_not_configured", "kind_budget")
# --session-projection given but no session projection committed this session: the run
# built its own product (eval.product.session), and fails, so the spec's pinned
# eval.product.source never passes on a fallback build unnoticed
SESSION_NOT_COMMITTED = "not_committed"
_BC_HANDLE = re.compile(r"^bc://[^/]+/([^/]+)/([^/]+)/([^/@]+)@")
_TOKEN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
# Tool-result fields that carry source-derived text (ontology_tool_contracts.json $defs).
SOURCE_FIELDS = frozenset({"untrusted_text", "citation", "l1_handle", "object_id", "normalized",
                           "value", "from", "to", "digest"})
ToolCall = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]
# (capture store file, budget seconds or None) -> counts: the product's BC capture run
BcCapture = Callable[[Path, "float | None"], Mapping[str, Any]]


class BudgetExhausted(TimeoutError):
    """The run's total time budget is spent; reads stop and the run is incomplete."""


class Deadline:
    """One total budget for the whole run (oracle reads, mirror sync, projection, finds)."""

    def __init__(self, seconds: float, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._end = monotonic() + max(0.0, float(seconds))
        self.hit = False

    def remaining(self) -> float:
        return max(0.0, self._end - self._monotonic())

    def expired(self) -> bool:
        if self.remaining() <= 0:
            self.hit = True
        return self.hit

    def guard(self) -> None:
        if self.expired():
            raise BudgetExhausted("run_budget_exhausted")


# ----------------------------------------------------------------- identities

def handle_identity(handle: Any) -> tuple[str, str] | None:
    """(relation, canonical key) of an ``api://`` or ``bc://`` L1 handle, capture ignored;
    BC company and record ids compare without case (BC GUIDs)."""
    found = row_identity(handle)
    if found is not None:
        return found
    match = _BC_HANDLE.match(handle) if isinstance(handle, str) else None
    if match is None:
        return None
    company, entity_set, record = (unquote(g) for g in match.groups())
    return "bc:" + entity_set, canonical({"company_id": company.lower(), "id": record.lower()})


def root_identity(item: Item) -> tuple[str, str]:
    key = {str(k): str(v) for k, v in item.key}
    if item.relation.startswith("bc:"):
        key = {k: v.lower() for k, v in key.items()}
    return item.relation, canonical(key)


def _candidate_handles(candidate: Mapping[str, Any]) -> list[str]:
    out = []
    for path in (("matched", "raw", "origin", "citation"),
                 ("object", "display", "origin", "citation")):
        node: Any = candidate
        for part in path:
            node = node.get(part) if isinstance(node, Mapping) else None
        if isinstance(node, str) and node not in out:
            out.append(node)
    return out


# ----------------------------------------------------------------- product

class Product:
    """The product path: tools looked up by name in a case-host tool table.

    ``audience_keys``: the audience units the projection holds objects of;
    ``complete_relations``: when the store was built here, the Portal relations whose
    mirror sync completed (a relation cut by the budget is not held);
    ``upstream_failed``: relation -> HTTP status / error code of the Portal relations whose
    sync the source itself failed; ``close``: releases the store the tools read (``close()``
    or ``with``, once the product's last call is made)."""

    def __init__(self, call: ToolCall | None, info: Mapping[str, Any], *,
                 audience_keys: Iterable[str] = (),
                 complete_relations: Iterable[str] | None = None,
                 upstream_failed: Mapping[str, Mapping[str, Any]] | None = None,
                 close: Callable[[], None] | None = None) -> None:
        self.call = call
        self._close = close
        self.info = dict(info)
        self.upstream_failed = {k: dict(v) for k, v in (upstream_failed or {}).items()}
        self.audience_keys = frozenset(k.lower() for k in audience_keys)
        self.complete_relations = (None if complete_relations is None
                                   else frozenset(complete_relations))
        self.seen: set[str] = set()  # texts the product returned, for row_data_check
        self.grammar: Any = None  # the identifier grammar plx.find's exact rule reads

    def close(self) -> None:
        if self._close is not None:
            self._close, close = None, self._close
            close()

    def __enter__(self) -> "Product":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    @property
    def available(self) -> bool:
        return self.call is not None

    @property
    def object_count(self) -> int:
        return int(self.info.get("objects") or 0)

    def upstream_block(self, relations: Iterable[str]) -> dict[str, Any] | None:
        """``{"blocked": "upstream", http_status, error_code}`` when the Portal failed the
        whole sync (every item is touched) or the sync of one of ``relations``."""
        if self.call is None and self.info.get("status") == "source_unavailable":
            return {"blocked": "upstream", **{k: self.info[k] for k in BLOCK_DETAIL
                                              if self.info.get(k) is not None}}
        for relation in relations:
            if relation in self.upstream_failed:
                return {"blocked": "upstream", **self.upstream_failed[relation]}
        return None

    def holds_relation(self, relation: str) -> bool:
        """Whether the projection captured this Portal relation at all."""
        if self.complete_relations is not None and relation not in self.complete_relations:
            return False
        return f"api:{relation}".lower() in self.audience_keys

    def holds(self, item: Item) -> bool:
        """Whether the projection captured the root's relation at all."""
        if item.relation.startswith("bc:"):
            company = dict(item.key).get("company_id", "")
            return f"bc:{company}/{item.relation[3:]}".lower() in self.audience_keys
        return self.holds_relation(item.relation)

    def holds_source(self, source: str) -> bool:
        """Whether the projection captured a source an oracle aspect reads: a Portal
        relation, or a ``bc:<entitySet>`` in any company."""
        if source.startswith("bc:"):
            tail = "/" + source[3:].lower()
            return any(k.startswith("bc:") and k.endswith(tail) for k in self.audience_keys)
        return self.holds_relation(source)

    def held_record_kinds(self) -> list[str] | None:
        """The record kinds worth sampling: Portal kinds whose relation the product holds
        or the Portal failed to sync (their items are then blocked upstream, never
        silently not held), ``bc_invoice`` when it holds any BC audience unit (per company,
        checked per item), and the three-way kind when it holds the Portal purchase orders
        (or their sync failed upstream). None when there is no product to ask, so the
        sample stays unfiltered."""
        if not self.available:
            return None
        has_bc = any(key.startswith("bc:") for key in self.audience_keys)

        def wanted(relation: str) -> bool:
            return self.holds_relation(relation) or relation in self.upstream_failed

        kinds = [kind for kind in RECORD_KINDS
                 if (any(wanted(relation) for relation, _ in PORTAL_STRATA[kind][1])
                     if kind in PORTAL_STRATA else has_bc)]
        return kinds + ([THREE_WAY] if wanted(THREE_WAY_ROOT) else [])

    def _note(self, value: Any) -> None:
        """Remember the source-derived strings of a tool result (texts, citations, object
        ids, normalized identifiers, values); contract enums are not row data."""
        stack = [value]
        while stack:
            node = stack.pop()
            if isinstance(node, Mapping):
                for key, inner in node.items():
                    if key in SOURCE_FIELDS and isinstance(inner, str):
                        self.seen.add(inner)
                    else:
                        stack.append(inner)
            elif isinstance(node, list):
                stack.extend(node)

    def find(self, identifier: str) -> tuple[str, list[Mapping[str, Any]], str | None]:
        """(status, candidates over at most FIND_PAGES pages, error code)."""
        assert self.call is not None
        arguments: dict[str, Any] = {"query": identifier, "limit": FIND_LIMIT}
        candidates: list[Mapping[str, Any]] = []
        status = "not_found"
        for _ in range(FIND_PAGES):
            result = self.call("plx.find", arguments)
            if "error" in result:
                code = result["error"].get("code") if isinstance(result["error"], Mapping) \
                    else None
                return "error", [], code if isinstance(code, str) else "error"
            for candidate in result.get("candidates", []):
                self._note(candidate)
            status = result.get("status", status) if not candidates else status
            candidates += list(result.get("candidates", []))
            cursor = (result.get("completeness") or {}).get("next_cursor")
            if not cursor:
                break
            arguments = {"query": identifier, "limit": FIND_LIMIT, "cursor": cursor}
        return status, candidates, None

    def process(self, object_id: str) -> Mapping[str, Any]:
        """``plx.process`` from one resolved root, through the same tool table."""
        assert self.call is not None
        result = self.call("plx.process", {"from": object_id, "process": PROCESS})
        self._note(result)
        return result

    def identities(self, candidate: Mapping[str, Any]) -> set[tuple[str, str]]:
        out = {i for i in map(handle_identity, _candidate_handles(candidate)) if i}
        object_id = (candidate.get("object") or {}).get("object_id")
        if out or not isinstance(object_id, str) or self.call is None:
            return out
        result = self.call("plx.object", {"object_id": object_id})
        self._note(result)
        for value in result.get("values", []) if "error" not in result else []:
            identity = handle_identity((value.get("citation") or {}).get("l1_handle"))
            if identity:
                out.add(identity)
        return out


def _excluded(reason: str) -> dict[str, Any]:
    return {"outcome": "excluded", "reason": reason, "path": None, "candidates": 0,
            "other_system_candidates": 0, "root_rank": None}


def _blocked(reason: str, block: Mapping[str, Any]) -> dict[str, Any]:
    """An item a failed read touched: never scored, counted apart (``blocked``: upstream
    or budget, with the HTTP status and error code when the source gave them)."""
    return {**_excluded(reason), "outcome": "blocked",
            **{k: block[k] for k in ("blocked", *BLOCK_DETAIL) if block.get(k) is not None}}


def roots_exactly(candidate: Mapping[str, Any], identifier: str, grammar: Any) -> bool:
    """find_root's gate on one plx.find candidate: ``ontology_tools.roots_exactly``, the
    one root gate (a match reason that may make a root and the owning system's literal
    comparison for its kind, a scoped kind only within its one scope). Anything else is a
    lead."""
    from operating import ontology_tools as ot

    return ot.roots_exactly(candidate, identifier, grammar)


def score_item(product: Product, item: Item, deadline: Deadline | None = None
               ) -> dict[str, Any]:
    """One item on the root's own relation: ``found_exact`` (the root passes find_root's
    gate and is the only gate-passing candidate there), ``ambiguous`` (others pass it
    too), ``found_lead`` (the root is a candidate but only as a lead), ``wrong`` (the
    root is no candidate), ``not_found``. ``root_rank`` is the root's plx.find rank (for
    success@1 and MRR), whatever its reason."""
    return find_root(product, item, deadline)[0]


def find_root(product: Product, item: Item, deadline: Deadline | None = None
              ) -> tuple[dict[str, Any], str | None]:
    """``score_item``'s row and, when the root passed the gate, the object id of its
    gate-passing candidate (kept internal: an object id is never output)."""
    block = product.upstream_block([item.relation])
    if block is not None:
        return _blocked("product_sync_failed", block), None
    if not product.available:
        return _excluded("product_path_unavailable"), None
    if not product.holds(item):
        return _excluded("product_not_holding_root"), None
    if deadline is not None and deadline.expired():
        return _excluded("run_budget_exhausted"), None
    status, candidates, error = product.find(item.identifier)
    if error is not None:
        return {"outcome": "not_found", "reason": _code("tool_error_", error), "path": "find",
                "candidates": 0, "other_system_candidates": 0, "root_rank": None}, None
    root = root_identity(item)
    exact_same, others, root_exact, root_rank, uncited = 0, 0, False, None, 0
    root_object: str | None = None
    for position, candidate in enumerate(candidates, start=1):
        identities = product.identities(candidate)
        uncited += not identities  # offered with no citation naming a source record
        exact = roots_exactly(candidate, item.identifier, product.grammar)
        if root in identities:
            rank = candidate.get("rank")
            rank = rank if isinstance(rank, int) and rank > 0 else position
            root_rank = rank if root_rank is None else min(root_rank, rank)
            root_exact = root_exact or exact
            object_id = (candidate.get("object") or {}).get("object_id")
            if exact and root_object is None and isinstance(object_id, str):
                root_object = object_id
        if any(rel == root[0] for rel, _ in identities):
            exact_same += exact
        else:
            others += 1
    if not candidates:
        outcome, reason = "not_found", "find_" + (status if status in (
            "not_found", "found", "ambiguous") else "not_found")
    elif root_rank is None:
        outcome, reason = "wrong", "root_not_among_candidates"
    elif not root_exact:
        outcome, reason = "found_lead", "root_only_a_lead"
    elif exact_same == 1:
        outcome, reason = "found_exact", "root_unique_in_its_system"
    else:
        outcome, reason = "ambiguous", "root_among_exact_candidates"
    return {"outcome": outcome, "reason": reason, "path": "find",
            "candidates": len(candidates), "other_system_candidates": others,
            "root_rank": root_rank, "uncited_candidates": uncited}, \
        (root_object if outcome == "found_exact" else None)


def _code(prefix: str, text: str) -> str:
    return prefix + re.sub(r"[^a-z0-9_]", "_", text.lower())[:40]


def product_aspects(result: Mapping[str, Any]) -> dict[str, Any]:
    """The product's answer per oracle aspect, from one plx.process result: a stage is
    ``present`` (reached, also out_of_order), ``absent`` (missing with a proven
    ``absent_in_source`` gap) or None (any other gap, not_applicable, not listed); a side is
    True when every quantity check of its two roles agrees, False when one disagrees, None
    otherwise (a null ``agree`` is undecided, never a disagreement)."""
    out: dict[str, Any] = {}
    stages = {s.get("stage"): s for s in result.get("stages") or [] if isinstance(s, Mapping)}
    for aspect in ASPECTS:
        kind, _, name = aspect.partition(":")
        if kind == "stage":
            state = (stages.get(name) or {}).get("state")
            gap = (stages.get(name) or {}).get("missing") or {}
            out[aspect] = ("present" if state in ("present", "out_of_order") else
                           "absent" if state == "missing"
                           and gap.get("state") == "absent_in_source" else None)
            continue
        roles = set(name.split("_"))
        agrees = [c.get("agree") for c in result.get("quantity_checks") or []
                  if isinstance(c, Mapping) and set(c.get("roles") or []) == roles]
        out[aspect] = (False if False in agrees else
                       True if agrees and all(a is True for a in agrees) else None)
    return out


def undecided_reason(result: Mapping[str, Any], aspect: str) -> str:
    """The product's typed reason one aspect of a plx.process result is undecided, as a
    code: a stage's gap reason (its state when it carries none, ``not_listed`` when the
    stage is not listed); a side's first gap naming one of its roles (``no_check`` when
    no check compares them, ``agree_null`` when no gap names them)."""
    kind, _, name = aspect.partition(":")
    if kind == "stage":
        stage = next((s for s in result.get("stages") or [] if isinstance(s, Mapping)
                      and s.get("stage") == name), None)
        if stage is None:
            return "not_listed"
        gap = stage.get("missing") if isinstance(stage.get("missing"), Mapping) else {}
        return _code("", str(gap.get("reason") or stage.get("state") or "unknown"))
    roles = set(name.split("_"))
    if not any(isinstance(c, Mapping) and set(c.get("roles") or []) == roles
               for c in result.get("quantity_checks") or []):
        return "no_check"
    for gap in result.get("gaps") or []:
        if isinstance(gap, Mapping) and str(gap.get("subject")).split(" ", 1)[0] in roles:
            return _code("", str(gap.get("reason")))
    return "agree_null"


def undecided_key(aspect: str, why: str) -> str:
    """``undecided.<aspect name>.<reason>`` for ``eval.by_kind``: an enum-shaped key of at
    most 64 characters, the verifier's limit (``live_verify.ENUM_STRING``)."""
    return f"{UNDECIDED_PREFIX}{aspect.partition(':')[2]}.{why}"[:64]


def score_process_item(product: Product, item: Item, reading: Mapping[str, Any] | None,
                       deadline: Deadline | None = None) -> dict[str, Any]:
    """One three-way item: the root is resolved through plx.find (find_root's gate), then
    plx.process answers from it; every aspect the oracle decided and the product holds the
    sources of is compared (module docstring). ``aspects`` counts agreed / disagreed /
    undecided / not_held (and, when any, blocked) aspects; ``wrong_aspects`` names the
    disagreements, ``blocked_aspects`` the aspects whose source sync failed upstream."""
    if reading is not None and reading.get("blocked"):
        # the oracle's own read failed: blocked (upstream or budget), never excluded
        return _blocked("oracle_read_failed", reading)
    if reading is None or reading.get("status") != "read":
        return _excluded("oracle_blocked")
    aspects = reading.get("aspects") or {}
    decided = [a for a in ASPECTS if (aspects.get(a) or {}).get("value") is not None]
    block = product.upstream_block([item.relation])
    if block is not None:
        return _blocked("product_sync_failed", block)
    # The purchase_order stage alone says only that the resolved root exists: an item is
    # scored on at least one other aspect (a receipt or invoice stage, or a side).
    if not [a for a in decided if a != ROOT_STAGE]:
        return _excluded("oracle_undecided")
    # An aspect whose own source sync failed upstream is blocked alone; the others are
    # still scored. Only an item left with no other scorable aspect is blocked whole.
    blocks = {a: b for a in decided
              if (b := product.upstream_block(aspects[a].get("sources") or [])) is not None}
    held = [a for a in decided if a not in blocks
            and all(product.holds_source(s) for s in aspects[a].get("sources") or [])]
    counts = {"agreed": 0, "disagreed": 0, "undecided": 0,
              "not_held": len(decided) - len(held) - len(blocks),
              **({"blocked": len(blocks)} if blocks else {})}
    marked = {"blocked_aspects": sorted(blocks)} if blocks else {}
    if blocks and not [a for a in held if a != ROOT_STAGE]:
        return {**_blocked("product_sync_failed", next(iter(blocks.values()))),
                "aspects": counts, **marked}
    found, object_id = find_root(product, item, deadline)
    if found["outcome"] in UNSCORED:
        return found
    if not [a for a in held if a != ROOT_STAGE]:
        return {**_excluded("product_not_holding_aspects"), "aspects": counts}
    if object_id is None:
        return {**found, "outcome": "root_not_resolved", "reason": "find_" + found["outcome"],
                "aspects": counts, **marked}
    result = product.process(object_id)
    if "error" in result:
        error = result["error"] if isinstance(result["error"], Mapping) else {}
        code = error.get("code") if isinstance(error.get("code"), str) else "error"
        return {**found, "outcome": "three_way_undecided", "path": "process",
                "reason": _code("tool_error_", code), "aspects": counts, **marked}
    answer = product_aspects(result)
    wrong, open_ = [], []
    for aspect in held:
        if answer[aspect] is None:
            counts["undecided"] += 1
            open_.append(aspect)
        elif answer[aspect] == aspects[aspect]["value"]:
            counts["agreed"] += 1
        else:
            counts["disagreed"] += 1
            wrong.append(aspect)
    if wrong:
        outcome, reason = "three_way_wrong", _code("wrong_", wrong[0].replace(":", "_"))
    elif blocks:
        # No scored aspect is wrong, but a blocked one could be: never a pass, counted
        # apart from wrong answers (blocked upstream with the first block's status).
        return {**found, "outcome": "blocked", "reason": "aspect_sync_failed",
                "path": "process",
                **{k: v for k, v in next(iter(blocks.values())).items()
                   if k in ("blocked", *BLOCK_DETAIL) and v is not None},
                "aspects": counts, "wrong_aspects": [], **marked}
    elif open_:
        outcome, reason = "three_way_undecided", _code("undecided_", open_[0].replace(":", "_"))
        # why each held aspect is open, so a run names its live cause (kind_result)
        marked["undecided_aspects"] = {a: undecided_reason(result, a) for a in open_}
    else:
        outcome, reason = "three_way_correct", "every_held_aspect_agrees"
    return {**found, "outcome": outcome, "reason": reason, "path": "process",
            "aspects": counts, "wrong_aspects": wrong, **marked}


def open_product(store_path: str | Path, *, tenant: str = EVAL_TENANT,
                 principal: str = EVAL_PRINCIPAL, info: Mapping[str, Any] | None = None,
                 complete_relations: Iterable[str] | None = None,
                 upstream_failed: Mapping[str, Mapping[str, Any]] | None = None) -> Product:
    """``plx.find``/``plx.object``/``plx.process`` over a projection store, from the case
    host's tool table (``operating.ontology_host.tool_table``). The eval principal reads
    every audience unit and holds every marking (it measures finding, not entitlements;
    leaks are a separate eval)."""
    import sqlite3

    from ontology import store_ddl
    from ontology.types import Actor
    from operating import ontology_host, ontology_tools as ot

    policy = ot.ReadPolicy(tenant=tenant, audiences={"api:*": {principal},
                                                     "bc:*": {principal}},
                           markings={m: {principal} for m in store_ddl.MARKINGS})
    grammar = ot.default_grammar()
    reader = ot.StoreReader.open(store_path)
    try:
        objects = reader.conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        keys = reader.audience_keys()
    except sqlite3.Error:
        reader.conn.close()
        return Product(None, {**(info or {}), "status": "store_unreadable", "objects": 0})
    tools = ot.OntologyTools(reader, policy, schema=ot.default_schema(), grammar=grammar)
    # The case host's own table (plx.find/object, plx.process, ...); no case runtime here,
    # so plx.investigate's claim gate reads no citation row.
    table = ontology_host.tool_table(tools, None)
    actor = Actor(tenant, frozenset({principal}), "restricted")

    def call(name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        spec = table.get(name)
        if spec is None:
            return {"error": {"code": "tool_not_registered"}}
        return spec["handler"](dict(arguments), actor)

    details = {**(info or {}), "objects": objects, "grammar": grammar is not None,
               "tools": sorted(table)}
    if "plx.find" not in table:
        reader.conn.close()
        return Product(None, {**details, "status": "find_tool_missing"})
    product = Product(call, {**details, "status": "available"}, audience_keys=keys,
                      complete_relations=complete_relations, upstream_failed=upstream_failed,
                      close=reader.conn.close)
    product.grammar = grammar
    return product


def required_relations(schema: Any, root_relations: Iterable[str]) -> list[str]:
    """The Portal relations a projection needs to hold the sampled roots, from the curated
    schema alone: every ``portal_api`` binding of each object type bound to a root relation
    (its records in other replicas, so other-system candidates are visible), plus the
    bindings those bindings' lineage rules read one link away (``via_link``)."""
    roots = set(root_relations)
    types = {tid for tid, t in schema.object_types.items()
             for b in t.get("bindings", []) if b.get("relation") in roots}
    by_binding = {b["id"]: b for t in schema.object_types.values()
                  for b in t.get("bindings", []) if isinstance(b.get("id"), str)}
    wanted: set[str] = set()
    hops: set[str] = set()
    for tid in types:
        for binding in schema.object_types[tid].get("bindings", []):
            if binding.get("source") == "portal_api" and binding.get("relation"):
                wanted.add(binding["relation"])
            for rule in (binding.get("lineage") or {}).get("rules", []):
                for condition in rule.get("when", []):
                    if isinstance(condition.get("via_link"), str):
                        hops.add(condition["via_link"])
    for link_id in hops:
        for lb in (schema.link_types.get(link_id) or {}).get("bindings", []):
            for end in (lb.get("from_binding"), lb.get("to_binding"), lb.get("via")):
                target = by_binding.get(end) if isinstance(end, str) else None
                if target and target.get("source") == "portal_api" and target.get("relation"):
                    wanted.add(target["relation"])
    return sorted(wanted)


def process_relations(schema: Any, process: str = PROCESS) -> list[str]:
    """The Portal relations the three-way question reads: every ``portal_api`` binding of
    each object type the process graph names (stage evidence, quantity sides, the item
    type it groups by), from the graph file and the curated schema alone."""
    from operating.ontology_tools_process import PROCESS_DIR

    graph = json.loads((PROCESS_DIR / f"{process}.json").read_text(encoding="utf-8"))
    types = {e["object_type"] for stage in graph.get("stages", [])
             for e in stage.get("evidence", [])}
    for check in graph.get("quantity_checks", []):
        types.add(check["group_by"]["object_type"])
        types.update(side["object_type"] for side in check.get("sides", []))
    return sorted({b["relation"] for tid in types
                   for b in (schema.object_types.get(tid) or {}).get("bindings", [])
                   if b.get("source") == "portal_api" and b.get("relation")})


def build_projection(client: Any, directory: Path, relations: Sequence[str], *,
                     budget_seconds: float | None, unstable: Iterable[str] = (),
                     state_dir: Path | None = None, bc_capture: BcCapture | None = None,
                     projection_reserve: float = 0.0,
                     run_left: Callable[[], float] | None = None
                     ) -> tuple[Path | None, dict[str, Any], frozenset[str]]:
    """GET-only Portal mirror sync of ``relations`` within the budget less
    ``projection_reserve`` (kept for the projection, so the whole build stays inside its
    budget), then the next projection generation under the curated schema
    (``ontology.project_incremental.build_generation``). Returns (store or None, info,
    complete relations). ``unstable`` relations are never synced; they leave the
    completeness denominator.

    ``state_dir`` (the host's, ``PLX_WORKBENCH_STATE_DIR``): the mirror and the
    generations persist in ``<state_dir>/eval-product`` (a later run projects only what
    changed) and the BC, mail and SharePoint captures the host holds there are projected
    too (``operating.host.state_captures``), so a Portal outage still yields a product:
    its relations are then blocked upstream, never held. Without one, DIR serves this
    run alone (``product_state: per_run``).

    ``bc_capture`` (``capture_bc`` bound to the BC credentials): the product captures BC
    itself after the Portal sync, into ``<DIR>/bc-capture`` (persistent with a state dir,
    so a later run reads deltas), on what is left of the build budget, never less than
    its floor and never more than ``run_left()`` (the seconds the run's deadline has left)
    less ``projection_reserve`` (``bc_budget``: the Portal sync keeps ``1 - BC_CAPTURE_SHARE``
    of the build budget); that capture is the projected ``bc`` source in place of the
    host's. A BC run that fails or gets no budget is reported
    (``bc_capture``), never raised. The projection's wall time and the process's peak
    resident memory are reported as diagnostics (``projection_seconds``,
    ``peak_rss_mib``)."""
    from contextlib import ExitStack

    from integrations.source_mirror import AudienceMap, MirrorError, SourceMirror
    from ontology.project import MirrorCaptures, capture_source
    from ontology.project_incremental import build_generation
    from ontology.schema import load_schema
    from ontology.types import Actor
    from operating import build_steps
    from operating.host import state_captures
    from scripts.mirror_measure import completeness, unstable_gaps

    if state_dir is not None:
        directory = state_dir / "eval-product"
    directory.mkdir(parents=True, exist_ok=True)
    mirror_path = directory / "mirror.sqlite"
    started = time.monotonic()
    unstable_set = set(unstable) & set(relations)
    staged = [name for name in relations if name not in unstable_set]
    schema = load_schema()
    gaps = unstable_gaps(unstable_set, schema=schema)
    info: dict[str, Any] = {"source": "projection_build", "captures": ["portal_api"],
                            "product_state": "per_run" if state_dir is None else "persistent",
                            "relations_required": len(relations), "relations_complete": 0,
                            "relations_unserved": 0,
                            "mirror_complete": False, "objects": 0,
                            "completeness": completeness((), bound=relations,
                                                         unstable=unstable_set,
                                                         scope="eval_required"),
                            "capture_unstable_bindings": [gap["where"] for gap in gaps]}
    if budget_seconds is not None:
        budget_seconds -= projection_reserve
    if budget_seconds is not None and budget_seconds <= 0:
        # The build never ran: not a budget cut mid-sync.
        return None, {**info, "status": "not_built", "reason": "no_budget_for_build",
                      "build_seconds": 0.0}, frozenset()
    actor = Actor("plx", frozenset({MIRROR_PRINCIPAL}))
    audience = AudienceMap({name: [MIRROR_PRINCIPAL] for name in staged}, tenant="plx")
    down: dict[str, Any] | None = None
    with SourceMirror(mirror_path, client, audience, tenant="plx") as mirror:
        try:
            synced = mirror.sync(actor, relations=staged,
                                 budget_seconds=portal_budget(budget_seconds,
                                                              bc_capture is not None))
        except MirrorError as exc:
            if exc.code != "source_unavailable":
                raise
            # The Portal answered non-2xx (or not at all) before any relation was read.
            down = {"error_code": exc.code,
                    **({"http_status": exc.status} if exc.status is not None else {})}
            synced = {"coverage": {}, "runs": []}
    bc_db = directory / "bc-capture" / "capture.sqlite"
    bc_info = None
    if bc_capture is not None:
        bc_info = run_bc_capture(bc_capture, bc_db, bc_budget(
            budget_seconds, time.monotonic() - started,
            None if run_left is None else run_left() - projection_reserve))
        info["bc_capture"] = bc_info
    with ExitStack() as stack:
        sources = {} if state_dir is None else state_captures(state_dir, stack)
        if bc_info and bc_info["status"] in ("captured", "partial"):
            sources["bc"] = stack.enter_context(capture_source("bc", bc_db))
        if down is not None and not sources:
            # Nothing else to project: the product is unavailable, the run still reports
            # every kind.
            return None, {**info, "status": "source_unavailable", **down,
                          "build_seconds": round(time.monotonic() - started, 1)}, frozenset()
        sources["portal_api"] = stack.enter_context(MirrorCaptures(mirror_path))
        projected = time.monotonic()
        generation = build_generation(directory / "projection", schema, sources,
                                      post_project=build_steps.post_project(
                                          directory if state_dir is None else state_dir,
                                          "plx"))
        projection_seconds = round(time.monotonic() - projected, 1)
    rows = {r["relation"]: r for r in synced["coverage"].get("relations", [])}
    complete = frozenset(n for n in staged if (rows.get(n) or {}).get("complete"))
    # A whole-sync outage blocks every Portal relation upstream; what the mirror kept
    # from an earlier run is projected (so its units are reused) but never held.
    upstream = upstream_failures(synced.get("runs", [])) if down is None else {
        name: dict(down) for name in staged}
    served = [n for n in staged if (rows.get(n) or {}).get("served")]
    outcomes: dict[str, int] = {}
    for run in synced.get("runs", []):
        outcomes[run["outcome"]] = outcomes.get(run["outcome"], 0) + 1
    counts = generation.report.get("counts", {})
    # A relation the upstream catalog does not serve cannot be captured: counted, and it
    # does not make the mirror incomplete.
    return generation.store, {
        **info, "captures": sorted(sources), "relations_complete": len(complete),
        "completeness": completeness(complete, bound=relations,
                                     unstable=unstable_set, scope="eval_required"),
        "relations_unserved": len(staged) - len(served),
        "mirror_complete": bool(served) and set(served) <= complete,
        "sync_outcomes": outcomes, "objects": counts.get("objects", 0),
        # relation names and codes only: which sync the Portal failed, and how
        "upstream_failed": upstream,
        **({"portal": {"status": "source_unavailable", **down}} if down is not None else {}),
        "build": {"mode": generation.mode, "units": generation.units,
                  "units_rebuilt": generation.units_rebuilt},
        "projection_seconds": projection_seconds, "peak_rss_mib": peak_rss_mib(),
        "build_seconds": round(time.monotonic() - started, 1)}, complete


def capture_bc(environ: Mapping[str, str], db: Path, budget_seconds: float | None, *,
               sender: Any = None, books: Mapping[str, Mapping[str, str]] | None = None,
               host_tenant: str | None = None) -> dict[str, Any]:
    """The product's own BC capture: one pass of ``bc_entity_sets`` in every pinned company
    through the GET-only BC client (``integrations.bc_read.capture_probe``, mode ``auto``:
    delta reads once a unit had its full pass) into ``db``. Counts and states only, with
    each root set's state per company (``root_set_states``) and, after a 429/503, the
    capture's ``throttle`` block (its HTTP status and the sets it left unread).
    ``host_tenant``: the store is bound to that Workbench tenant, as a host's own capture
    (``operating.host``) is, so a host of the tenant reads it through its capture gate;
    default: bound to the BC tenant itself (``bc_read.capture_probe``)."""
    from integrations import bc_read
    from ontology.schema import load_schema

    sets = bc_entity_sets(load_schema(), PROCESS, BC_INVOICE_SETS)
    bound: dict[str, Any] = {}
    if host_tenant is not None:
        from integrations.live_source_guard import LiveSourceAudience
        from ontology.types import Actor
        environment = str(environ.get("BC_ENVIRONMENT") or "").lower()
        bound = {"audience": LiveSourceAudience({f"bc:{environment}:*": ["capture:operator"]},
                                                tenant=host_tenant),
                 "actor": Actor(host_tenant, frozenset({"capture:operator"}), "restricted")}
    report = bc_read.capture_probe(environ, sender=sender, db=db,
                                   budget_seconds=budget_seconds, entity_sets=sets,
                                   resolve_sample=0, books=books, mode="auto",
                                   roots=BC_INVOICE_SETS, **bound)
    return {"entity_sets": len(sets), "companies": report["companies_captured"],
            "units": report["units_total"], "units_reached": report["units_reached"],
            "units_complete": report["units_complete"],
            "complete": bool(report["all_complete"]),
            **({"stopped_reason": report["stopped_reason"]}
               if report.get("stopped_reason") else {}),
            **({"throttle": report["throttle"]} if report.get("throttle") else {}),
            "requests": report.get("requests_sent"),
            "seconds": round(float(report.get("capture_seconds") or 0.0), 1),
            "root_sets": root_set_states(report, BC_INVOICE_SETS)}


def root_set_states(report: Mapping[str, Any], roots: Sequence[str]
                    ) -> dict[str, dict[str, str]]:
    """Each root set's state per company (its ``company_key``) in a ``capture_probe``
    report: ``complete`` when that company's unit read it whole, else ``ROOT_UNIT_STATE``
    of the unit's reason (a unit never reached takes the call's stop reason). ``*`` is the
    state of a company the report holds no entry for: the call's stop when it stopped
    before any company (a throttled listing), else ``other`` (a pinned company the listing
    did not show)."""
    stop = report.get("stopped_reason")
    unlisted = "other" if report.get("companies") or not stop \
        else ROOT_UNIT_STATE.get(stop, "other")
    out = {}
    for name in roots:
        states = {"*": unlisted}
        for company in report.get("companies") or ():
            unit = capture_unit(company, name)
            reason = stop if unit is None else unit.get("reason")
            states[str(company.get("company_key"))] = (
                "complete" if unit is not None and unit.get("complete")
                else ROOT_UNIT_STATE.get(reason, "other"))
        out[name] = states
    return out


def portal_budget(build_seconds: float | None, bc: bool) -> float | None:
    """The Portal sync's share of the build budget: all of it, or ``1 - BC_CAPTURE_SHARE``
    when the product captures BC itself after it (``bc``). None: no budget."""
    return build_seconds if build_seconds is None or not bc \
        else build_seconds * (1 - BC_CAPTURE_SHARE)


def bc_budget(build_seconds: float | None, elapsed: float, run_left: float | None = None
              ) -> float | None:
    """The BC capture's budget after the Portal sync: what the build budget has left after
    ``elapsed`` seconds, never less than its ``BC_CAPTURE_SHARE`` floor (an overrunning
    Portal sync costs the later stages, never BC its roots), and never more than
    ``run_left``, what the run's deadline leaves the BC capture: the floor yields to the
    deadline, so a capture it cuts stops on its budget (``partial`` or ``no_budget``, its
    unread roots a coded budget cut), never past the deadline. None: no budget. The one
    budget rule of every build that captures BC (``eval_dev_baseline`` too)."""
    if build_seconds is None:
        return run_left
    budget = max(build_seconds - elapsed, build_seconds * BC_CAPTURE_SHARE)
    return budget if run_left is None else min(budget, run_left)


def run_bc_capture(capture: BcCapture, db: Path, budget_seconds: float | None
                   ) -> dict[str, Any]:
    """One BC capture run for the product: ``captured`` (every unit complete),
    ``partial`` (projected as far as it got; the projector labels the rest incomplete),
    ``no_budget`` or ``failed`` (with the error class, never a message)."""
    if budget_seconds is not None and budget_seconds <= 0:
        return {"status": "no_budget"}
    db.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = dict(capture(db, budget_seconds))
    except Exception as exc:  # noqa: BLE001 - a source read: reported as a class name
        return {"status": "failed", "error": type(exc).__name__}
    return {"status": "captured" if report.get("complete") else "partial", **report}


def peak_rss_mib() -> float | None:
    """The process's peak resident memory so far (diagnostic; None where unknown)."""
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


# ----------------------------------------------------------------- report

def unscored_cause(row: Mapping[str, Any], product: Product, relation: str,
                   company: str | None = None) -> str | None:
    """Why an item was not scored, kept apart:
    ``upstream_outage`` (a source failed the read), ``projection_timeout`` (the build ran out
    of budget or never ran, or its mirror sync was cut), ``missing_bindings`` (the product never
    captures the root's or aspects' source), ``host_misconfiguration`` (no product configured,
    store unreadable, tool missing), ``run_budget`` or ``oracle`` (the oracle could not ask).
    A not-held BC root (``relation`` ``bc:...``) follows its root set's own state in its
    ``company`` in the product's BC capture (``ROOT_SET_CAUSE``; the capture's status,
    ``BC_NOT_HELD_CAUSE``, when it has none), never the Portal mirror: a root set its
    company read whole under a partial capture is a binding gap, not a timeout, whatever
    another company's unit did. None for a scored item."""
    if row["outcome"] not in UNSCORED:
        return None
    if row["outcome"] == "blocked":
        return "upstream_outage" if row.get("blocked") == "upstream" else "run_budget"
    reason, status = row.get("reason"), product.info.get("status")
    if reason in NOT_HELD_REASONS and relation.startswith("bc:"):
        capture = product.info.get("bc_capture")
        if capture is None:  # no own BC capture: the host's captures never bound it
            return "missing_bindings"
        states = (capture.get("root_sets") or {}).get(relation[3:])
        state = None if states is None else \
            states.get(company_key(str(company)), states.get("*"))
        # a state this eval never writes is no binding gap the product could fix
        return (ROOT_SET_CAUSE.get(state, "host_misconfiguration") if state is not None
                else BC_NOT_HELD_CAUSE.get(capture.get("status"), "host_misconfiguration"))
    if reason in NOT_HELD_REASONS:
        return ("projection_timeout" if product.info.get("mirror_complete") is False
                else "missing_bindings")
    if reason == "product_path_unavailable":
        return {"not_built": "projection_timeout",
                "source_unavailable": "upstream_outage"}.get(status, "host_misconfiguration")
    if reason == "run_budget_exhausted":
        return "run_budget"
    return "oracle"


def answer_tally(rows: Sequence[Mapping[str, Any]],
                 strata: Iterable[Mapping[str, Any]] = (), quota: int = 0) -> dict[str, Any]:
    """``eval.answer`` / ``eval.find_root``: every sampled item in one bucket (blocked
    upstream, not held, other unscored, or scored as correct / wrong / abstain_ok / missed)
    and the unscored items' causes. Sparse, to fit the verifier's detail cap: ``sampled`` and
    ``scored`` always, another count only when not zero (the verifier sums these blocks and
    publishes accuracy beside coverage). ``strata``: ``eval.by_kind`` entries of kinds
    blocked before any item was drawn and ``quota``, the items each could not give: counted
    as sampled and unscored, so an outage lowers coverage instead of vanishing."""
    out = {key: 0 for key in ANSWER_COUNTS}
    causes: dict[str, int] = {}
    for stratum in strata:
        upstream = stratum.get("blocked") == "upstream"
        out["sampled"] += quota
        out["blocked_upstream" if upstream else "unscored_other"] += quota
        cause = STRATUM_CAUSE.get(stratum.get("blocked"), "oracle")
        causes[cause] = causes.get(cause, 0) + quota
    for row in rows:
        out["sampled"] += 1
        if str(row.get("reason") or "").startswith(TOOL_ERROR_PREFIX):
            out["product_error"] += 1
        elif row["outcome"] in UNSCORED:
            bucket = ("blocked_upstream" if row["outcome"] == "blocked"
                      and row.get("blocked") == "upstream" else
                      "not_held" if row.get("reason") in NOT_HELD_REASONS else "unscored_other")
            out[bucket] += 1
            cause = str(row.get("cause") or "unknown")
            causes[cause] = causes.get(cause, 0) + 1
        else:
            out["scored"] += 1
            out[ANSWER_CLASS[row["outcome"]]] += 1
    return {key: value for key, value in out.items()
            if value or key in ("sampled", "scored")} | (
        {"causes": dict(sorted(causes.items()))} if causes else {})


def wrong_within_bound(answer: Mapping[str, Any], roots: Mapping[str, Any]) -> bool:
    """Wrong answers (three-way and find) are at most ``MAX_WRONG_SHARE`` of all scored
    items: a confident wrong answer stays visible and fails the run."""
    wrong = answer.get("wrong", 0) + roots.get("wrong", 0)
    top, bottom = MAX_WRONG_SHARE
    return wrong * bottom <= (answer["scored"] + roots["scored"]) * top


def _rate(rows: Sequence[Mapping[str, Any]]) -> str:
    """Passing items (``PASSING``: found_exact, three_way_correct) over the scored, ``x/y``."""
    scored = [r for r in rows if r["outcome"] not in UNSCORED]
    return f"{sum(1 for r in scored if r['outcome'] in PASSING)}/{len(scored)}"


def ranking(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Identity (find) figures over the find items only (three-way items are scored on
    plx.process and stay out): their ``items`` (sampled) and ``scored`` counts,
    found_exact, found_lead, success@1 (the root is plx.find's first candidate; scored
    items only), and MRR over the scored items (mean of 1/root rank, 0 when the root is no
    candidate)."""
    rows = [r for r in rows if r["kind"] != THREE_WAY]
    scored = [r for r in rows if r["outcome"] not in UNSCORED]
    ranks = [r.get("root_rank") for r in scored]
    return {"items": len(rows), "scored": len(scored),
            "found_exact": sum(1 for r in rows if r["outcome"] == "found_exact"),
            "found_lead": sum(1 for r in rows if r["outcome"] == "found_lead"),
            "success_at_1": sum(1 for k in ranks if k == 1),
            "mrr": (round(sum(1 / k for k in ranks if isinstance(k, int)) / len(scored), 4)
                    if scored else None)}


def product_state(product: Product) -> dict[str, Any]:
    """The lifted product state. ``completeness`` is ``complete_bound_relation_count/
    bound_total`` over the required relations (the full block is in the report's
    ``product``); ``unstable_excluded`` counts the required relations that left it."""
    from scripts.mirror_measure import completeness_ratio

    info = product.info
    block = info.get("completeness")
    bc = info.get("bc_capture") or {}
    optional = {"source": info.get("source"), "session": info.get("session"),
                "mirror_complete": info.get("mirror_complete"),
                "completeness": completeness_ratio(block),
                "unstable_excluded": (len(block["unstable_excluded"])
                                      if isinstance(block, Mapping) else None),
                "relations_unserved": info.get("relations_unserved"),
                "build_seconds": info.get("build_seconds"),
                # diagnostics: the projection's own time and the process's peak memory
                "projection_seconds": info.get("projection_seconds"),
                "peak_rss_mib": info.get("peak_rss_mib"),
                "bc_capture": bc.get("status"),
                # how far the product's own BC capture got: units reached and complete,
                # its seconds and why it stopped (absent: it did not stop)
                **{f"bc_{key}": bc.get(key) for key in BC_PRODUCT_FIELDS},
                # the Portal failed these relations' sync (absent: none)
                "relations_upstream_failed": len(info.get("upstream_failed") or ()) or None}
    # sparse (an unknown figure is left out) to fit the verifier's detail cap
    return {"available": product.available, "status": info.get("status"),
            "object_count": product.object_count,
            **{k: v for k, v in optional.items() if v is not None}}


def kind_result(group: Sequence[Mapping[str, Any]], sampled: Mapping[str, Any],
                kind: str | None = None) -> dict[str, Any]:
    """One kind's line of ``eval.by_kind`` from its rows and its sampling entry: counts
    (an outcome or exclusion count that is zero is left out, as in ``eval.answer``, so the
    record keeps every field under the verifier's cap), the rate, and a ``status`` that is
    ``passed`` only when a scored item passed (found_exact; three_way_correct for the
    three-way kind, whose outcomes are its own).
    Blocked items are counted apart (``blocked_items``, when any); a kind whose items are all
    unscored and some blocked is ``blocked`` with the first block's cause and codes. A
    kind without items says why (the sampler's status) with its candidates and references
    instead of outcome counts (nothing was scored; the sources it read are in
    ``eval.sources_tried``); a failed source read is ``blocked``, never empty and never a
    pass."""
    scored = [r for r in group if r["outcome"] not in UNSCORED]
    blocked = [r for r in group if r["outcome"] == "blocked"]
    outcomes = THREE_WAY_OUTCOMES if kind == THREE_WAY else OUTCOMES
    counts = {o: sum(1 for r in group if r["outcome"] == o) for o in (*outcomes, "excluded")}
    out: dict[str, Any] = {"candidates": int(sampled.get("candidates") or 0),
                           "references": int(sampled.get("references") or 0),
                           "items": len(group), "scored": len(scored), "pass": _rate(group),
                           **{o: n for o, n in counts.items() if n},
                           **({"blocked_items": len(blocked)} if blocked else {})}
    reasons: dict[str, int] = {}
    for r in group:
        if r["outcome"] == "three_way_undecided":
            for aspect, why in (r.get("undecided_aspects") or {}).items():
                key = undecided_key(aspect, why)
                reasons[key] = reasons.get(key, 0) + 1
    # undecided items per aspect and product reason, flat in the kind's line (the verifier
    # keeps three levels of the eval block), the most frequent first and the remainder
    # counted apart, so the record stays under the cap
    ranked = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
    out.update(ranked[:MAX_UNDECIDED_REASONS])
    if len(ranked) > MAX_UNDECIDED_REASONS:
        out[UNDECIDED_PREFIX + "other"] = sum(n for _, n in ranked[MAX_UNDECIDED_REASONS:])
    held_back = [r for r in group if r.get("blocked_aspects")]
    if held_back:
        # an item with a blocked aspect may hide a wrong answer: the kind cannot pass
        first = held_back[0]
        return {"status": "blocked", "reason": "aspect_sync_failed",
                **{k: first[k] for k in ("blocked", *BLOCK_DETAIL) if k in first}, **out}
    if any(r["outcome"] in PASSING for r in scored):
        return {"status": KIND_PASSED, **out}
    if scored:
        return {"status": "no_correct_answer" if kind == THREE_WAY else "no_found_exact",
                **out}
    if blocked:
        first = blocked[0]
        return {"status": "blocked", "reason": first["reason"],
                **{k: first[k] for k in ("blocked", *BLOCK_DETAIL) if k in first}, **out}
    if group:
        reasons = sorted({r["reason"] for r in group})
        return {"status": "unscored", "reason": reasons[0], **out}
    status, reason = sampled.get("status"), sampled.get("reason")
    if status == "unavailable":
        cause = "budget" if reason == BudgetExhausted.__name__ else \
            sampled.get("blocked") or "upstream"
        status_out = {"status": "blocked", "blocked": cause,
                      **{k: sampled[k] for k in BLOCK_DETAIL if k in sampled}}
    elif status in ("unreadable", "incomplete"):
        status_out = {"status": "blocked", "blocked": "unreadable"}
    elif status == "no_candidate_in_sample":   # a sample of what the reader can read
        status_out = {"status": "no_candidate_in_sample",
                      **({"withheld": sampled["withheld"]} if sampled.get("withheld") else {})}
    elif status == "not_sampled":
        status_out, reason = {"status": "not_held"}, "product_not_holding_kind"
    elif status == "not_configured":
        status_out = {"status": "not_configured"}
    else:   # read to the end, no question: the stratum is empty
        status_out = {"status": "empty"}
    # a blocked stratum read nothing: its zero counts are left out (the record stays under
    # the verifier's cap with every kind blocked)
    # the sampler's stage that left a read kind without a question (within_read)
    return {**status_out, "reason": reason or "no_items",
            **({"within_read": sampled["within_read"]} if sampled.get("within_read") else {}),
            **{k: out[k] for k in ("candidates", "references", "items")
               if out[k] or status_out["status"] != "blocked"}}


def public_source(name: str) -> str:
    """A source name as published: identifier-shaped names as they are (relation.column,
    ``bc:<entitySet>``, a drive path), a mailbox only as ``mail`` (an address is never
    published), any other name by its scheme."""
    scheme, sep, _ = name.partition(":")
    if scheme == "mail":
        return name if _MAIL_SCOPE_SOURCE.fullmatch(name) else "mail"
    if _SOURCE_NAME.fullmatch(name):
        return name
    return scheme if sep and _SOURCE_NAME.fullmatch(scheme) else "unnamed"


def blocked_summary(rows: Sequence[Mapping[str, Any]],
                    by_kind: Mapping[str, Mapping[str, Any]], quota: int = 0
                    ) -> dict[str, Any]:
    """``eval.blocked``: blocked items (apart from wrong answers and exclusions), blocked
    kinds and (when any) the three-way aspects blocked inside otherwise scored items; items
    and kinds are each tallied by cause, HTTP status and error code on their own
    (``items_*`` and ``kinds_*``; a kind's tally is never added to its items'). The tallies
    appear only when something is blocked."""
    def tally(entries: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in entries:
            if entry.get(key) is not None:
                out[str(entry[key])] = out.get(str(entry[key]), 0) + 1
        return dict(sorted(out.items()))

    items = [r for r in rows if r["outcome"] == "blocked"]
    kinds = [v for v in by_kind.values() if v.get("status") == "blocked"]
    tallies = {f"{unit}_{key}": tally(entries, field)
               for unit, entries in (("items", items), ("kinds", kinds))
               for key, field in (("by_cause", "blocked"), ("http_status", "http_status"),
                                  ("error_code", "error_code"))}
    aspects = sum(int((r.get("aspects") or {}).get("blocked") or 0) for r in rows)
    # the items the strata blocked before any item could not give (``stratum_quota`` each)
    quota *= sum(1 for v in kinds if not v.get("items"))
    return {"items": len(items), "kinds": len(kinds), **({"aspects": aspects} if aspects else {}),
            **({"kinds_quota": quota} if quota else {}),
            **{k: v for k, v in tallies.items() if v}}


# How a stratum blocked before any item was blocked -> the cause its quota counts under.
STRATUM_CAUSE = {"upstream": "upstream_outage", "budget": "run_budget"}
# eval.blocked_cause of a portal_outage run: the live check reads it blocked, never pass
OUTAGE_CAUSE = "upstream_outage"


def stratum_quota(count: int) -> int:
    """A kind's even share of the requested item count (never 0)."""
    return max(1, count // len(KINDS))


def portal_outage(product: Product) -> bool:
    """Whether the Portal failed the product's whole mirror sync (non-2xx or unreachable
    before any relation was read): the run is then measured on what else it holds."""
    info = product.info
    return info.get("status") == "source_unavailable" or \
        (info.get("portal") or {}).get("status") == "source_unavailable"


def typed_block(kind: str, line: Mapping[str, Any],
                sampled: Mapping[str, Any]) -> str | None:
    """The block code of a kind that is typed blocked (``TYPED_BLOCKS``), else None: the
    kind has no item, its ``eval.by_kind`` status and reason are the declared ones, and a
    block that lasts until a relation is served needs that relation's funnel row read with
    0 rows (a missing row, or a served one, is not the declared cause); a block by refused
    searches needs the generator's own verdict (``blocked: upstream`` on the kind's sampling
    entry, set when the tenant refused every query and nothing was answered, failed or
    scanned): the one rule ``eval.oracle_dev`` applies, not re-derived from the counts; a
    block by a replica disjoint from BC (``TYPED_BLOCK_REPLICA``) needs Portal roots read,
    no Portal twin among the BC rows, and every BC probe made and none of them a hit
    (``missing_reference_cause`` ``bc_source_mismatch``: a refused probe is a budget)."""
    declared = TYPED_BLOCKS.get(kind)
    if declared is None or line.get("items") or line.get("status") == KIND_PASSED:
        return None
    status, reasons, code = declared
    if line.get("status") != status or line.get("reason") not in reasons:
        return None
    relation = TYPED_BLOCK_UNSERVED.get(kind)
    if relation is not None:
        row = (sampled.get("funnel") or {}).get(relation)
        if row is None or int(row.get("rows") or 0) != 0:
            return None
    if kind in TYPED_BLOCK_UPSTREAM and sampled.get("blocked") == "scope":
        return scope_block(sampled.get("scope_blocked"))
    if kind in TYPED_BLOCK_UPSTREAM and sampled.get("blocked") != "upstream":
        return None
    if kind in TYPED_BLOCK_REPLICA and not replica_disjoint(line, sampled):
        return None
    return code


def scope_block(blocked: Any) -> str | None:
    """The later_ask of mail its capture scope blocked: every blocked mailbox names a
    ``TYPED_BLOCK_SCOPE`` code (else None, never typed); the people exclusion first, since
    no granted mailbox is read until it is answered."""
    if not isinstance(blocked, Mapping) or not blocked \
            or any(code not in TYPED_BLOCK_SCOPE for code in blocked.values()):
        return None
    codes = set(blocked.values())
    return TYPED_BLOCK_SCOPE["excluded-people-oids" if "excluded-people-oids" in codes
                             else "mail-scope-grant"]


def public_scope_blocked(blocked: Any) -> dict[str, str]:
    """The ``scope_blocked`` of a kind as published: scope ids with a ``TYPED_BLOCK_SCOPE``
    code only (anything else, an address included, is dropped, and the kind is then not
    typed blocked by ``scope_block``: a unit with an unknown code makes it ``unknown``)."""
    if not isinstance(blocked, Mapping):
        return {}
    return {str(unit): (code if code in TYPED_BLOCK_SCOPE else "unknown")
            for unit, code in sorted(blocked.items())
            if _MAIL_SCOPE_UNIT.fullmatch(str(unit))}


def replica_disjoint(line: Mapping[str, Any], sampled: Mapping[str, Any]) -> bool:
    """Whether a kind's Portal roots were read and BC, probed in full, holds no copy of any:
    a Portal relation read with roots, no BC row a Portal twin, no reference, the reference
    cause ``bc_source_mismatch`` (BC sources looked up, no probe refused by the budget), and
    no read failed (no ``reference_read_gap``, no ``bc_read_failed``): a partly failing BC
    measured no mismatch."""
    funnel = sampled.get("funnel") or {}
    portal_roots = sum(int(row.get("roots") or 0) for name, row in funnel.items()
                       if not name.startswith("bc:"))
    twins = int((sampled.get("skipped") or {}).get("portal_twin") or 0) + sum(
        int(row.get("portal_twin") or 0) for row in funnel.values())
    skipped = sampled.get("skipped") or {}
    cause = missing_reference_cause(sampled) or {}
    return (portal_roots > 0 and twins == 0 and not line.get("references")
            and not sampled.get("references") and cause.get("cause") == "bc_source_mismatch"
            and not skipped.get("reference_read_gap") and not skipped.get("bc_read_failed"))


def blocked_cause(block: Mapping[str, Any]) -> str | None:
    """The one block code a run reads blocked by (``blocked_causes``' first), else None."""
    causes = blocked_causes(block)
    return causes[0] if causes else None


def blocked_causes(block: Mapping[str, Any]) -> list[str]:
    """The block codes a run reads blocked by, else empty. Rule ``portal_outage``: always
    ``upstream_outage`` (``OUTAGE_CAUSE``): with the Portal strata unasked the run
    measures the outage rule at most, never every kind, so it is never a pass
    (gap-51ebb6c78e90); it still fails on its own rule's failures (``fail_reasons``). Rule
    ``every_kind``: at least one kind typed blocked that may not pass that way (mail,
    sales_order), and the run would pass were those kinds resolved (every other missing kind
    a typed ``TYPED_PASSES`` one), one code per such kind in kind order. Blocked is not a
    pass: the causes name what is still open."""
    if block["pass_rule"] == "portal_outage":
        return [OUTAGE_CAUSE]
    by_kind = block["by_kind"]
    held = sorted(k for k in block["kinds_missing"]
                  if by_kind[k].get("typed_block") and k not in TYPED_PASSES)
    rest = [k for k in block["kinds_missing"] if k not in held]
    if not held or any(k not in TYPED_PASSES or not by_kind[k].get("typed_block")
                       for k in rest):
        return []
    return [by_kind[k]["typed_block"] for k in held]


def kinds_failing(block: Mapping[str, Any]) -> list[str]:
    """The kinds of ``eval.kinds_missing`` that fail the run. Under rule ``every_kind``
    (the acceptance bar): every kind not a typed ``TYPED_PASSES`` one
    (``eval.kinds_typed_blocked``). Under ``portal_outage`` a kind blocked upstream,
    typed blocked or without any item is excused (its stratum could not be asked), but a
    kind whose items were asked and none passed still fails."""
    by_kind = block["by_kind"]
    missing = list(block["kinds_missing"])
    if block["pass_rule"] == "portal_outage":
        return [kind for kind in missing
                if not (by_kind[kind].get("blocked") == "upstream"
                        or by_kind[kind].get("typed_block") or not by_kind[kind].get("items"))]
    return [kind for kind in missing
            if not (by_kind[kind].get("typed_block") and kind in TYPED_PASSES)]


def bc_rooted(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """``eval.bc_rooted``: the items whose root is a BC record (``bc_invoice`` roots, also
    when a mail or SharePoint document named them, and the BC orders and receipts a
    document named), bucketed as ``answer_tally`` (wrong, blocked and abstained apart;
    accuracy is correct/scored beside coverage scored/sampled)."""
    return answer_tally([r for r in rows if r.get("root_kind") in BC_ROOT_KINDS])


def item_measure(scored: Mapping[str, Any], log: measuring.CallLog,
                 evidence_label: str) -> dict[str, Any]:
    """ENG-11 per scored item (``evals.measure.of_calls``): a correct or wrong answer is
    answered, a typed three-way gap a justified abstention, a miss or a tool error
    unanswered; unsupported assertions are candidates offered with no citation; an
    ambiguous or lead-only root (also one a three-way item could not resolve) leaves one
    identity choice to a person."""
    answer = ANSWER_CLASS.get(scored["outcome"])
    resolution = ("unanswered" if str(scored.get("reason", "")).startswith(TOOL_ERROR_PREFIX)
                  else {"correct": "answered", "wrong": "answered",
                        "abstain_ok": "justified_abstention"}.get(answer, "unanswered"))
    return measuring.of_calls(log, evidence_label=evidence_label, resolution=resolution,
                              unsupported=int(scored.get("uncited_candidates") or 0),
                              identity_reviews=int(scored["outcome"] in PERSON_CHOICE or (
                                  scored["outcome"] == "root_not_resolved"
                                  and scored.get("reason") in {
                                      f"find_{o}" for o in PERSON_CHOICE})))


def eval_block(rows: Sequence[Mapping[str, Any]], *, run_id: str, evidence_label: str,
               product: Product, kinds: Mapping[str, Mapping[str, Any]],
               incomplete: Sequence[str], count: int | None = None) -> dict[str, Any]:
    by_kind = {kind: kind_result([r for r in rows if r["kind"] == kind], kinds.get(kind, {}),
                                 kind)
               for kind in KINDS}
    for kind, line in by_kind.items():
        code = typed_block(kind, line, kinds.get(kind, {}))
        if code is not None:
            line["typed_block"] = code
    quota = stratum_quota(len(rows) if count is None else count)
    # a stratum blocked before any item: its share is counted, never dropped
    blocked_strata = {kind: e for kind, e in by_kind.items()
                      if e["status"] == "blocked" and not e.get("items")}
    outage = portal_outage(product)
    missing = [kind for kind in KINDS if by_kind[kind]["status"] != KIND_PASSED]
    excluded: dict[str, int] = {}
    for row in rows:
        if row["outcome"] == "excluded":
            excluded[row["reason"]] = excluded.get(row["reason"], 0) + 1
    not_held = sorted(k for k, v in kinds.items() if v.get("status") == "not_sampled")
    # zero counts left out (the record stays under the verifier's cap)
    sampling = {name: n for name in SAMPLING_EXCLUSIONS
                if (n := sum(int((k.get("skipped") or {}).get(name, 0)) for k in kinds.values()))}
    scored = [r for r in rows if r["outcome"] not in UNSCORED]
    # what each kind without items read, as names (a list one level deep, which the
    # verifier records as names, not as a count); a kind not held or not configured read
    # nothing, and during a whole-Portal outage a kind blocked upstream read only the
    # Portal, whose failure eval.product and the kind's own block already name
    tried = {kind: [public_source(str(n)) for n in kinds.get(kind, {}).get("tried") or []]
             for kind in KINDS if not by_kind[kind].get("items")
             and by_kind[kind]["status"] not in ("not_held", "not_configured")
             and not (outage and by_kind[kind].get("blocked") == "upstream")}
    # the reference sources a kind without items looked its roots' identifiers up in and
    # found no record in (names, as sources_tried): which source holds no copy of them
    missing_ref = {kind: missing_reference_cause(kinds.get(kind, {}))
                   for kind in KINDS if not by_kind[kind].get("items")}
    # Answer correctness is the three-way answer; root lookup is reported apart (amendment
    # bb664f58910f section 2: root lookup is not three-way success).
    answer = answer_tally([r for r in rows if r["kind"] == THREE_WAY],
                          [e for k, e in blocked_strata.items() if k == THREE_WAY], quota)
    roots = answer_tally([r for r in rows if r["kind"] != THREE_WAY],
                         [e for k, e in blocked_strata.items() if k != THREE_WAY], quota)
    return {"metric": METRIC, "run_id": run_id, "evidence_label": evidence_label,
            # every_kind: each kind must pass; portal_outage: the Portal failed the whole
            # sync, so BC-rooted items must be scored and kinds that were asked must pass,
            # and even then the run reads blocked upstream_outage, never pass
            "pass_rule": "portal_outage" if outage else "every_kind",
            "answer": answer, "find_root": roots, "bc_rooted": bc_rooted(rows),
            "wrong_within_bound": wrong_within_bound(answer, roots),
            "complete": not incomplete, "incomplete": list(incomplete),
            "product": product_state(product),
            "pass": _rate(rows), "items": len(rows), "scored": len(scored),
            "ranking": ranking(rows),
            "scored_kinds": len({r["kind"] for r in scored}),
            "kinds_required": len(KINDS), "kinds_found_exact": len(KINDS) - len(missing),
            "kinds_missing": missing,
            # the missing kinds typed blocked, with their block codes (never resolved)
            "kinds_typed_blocked": {k: by_kind[k]["typed_block"] for k in missing
                                    if by_kind[k].get("typed_block")},
            "excluded": dict(sorted(excluded.items())),
            "blocked": blocked_summary(rows, by_kind, quota), "by_kind": by_kind,
            **({"sources_tried": {k: v[:MAX_SOURCES_TRIED] for k, v in tried.items() if v}}
               if any(tried.values()) else {}),
            # a kind that tried more sources than are named: how many it tried
            **({"sources_tried_total": {k: len(v) for k, v in tried.items()
                                        if len(v) > MAX_SOURCES_TRIED}}
               if any(len(v) > MAX_SOURCES_TRIED for v in tried.values()) else {}),
            **({"missing_reference": {k: v for k, v in missing_ref.items() if v}}
               if any(missing_ref.values()) else {}),
            "by_bucket": {b: _rate([r for r in rows if r.get("bucket") == b])
                          for b in BUCKETS},
            "other_system_candidates": {
                "items_with": sum(1 for r in scored if r["other_system_candidates"]),
                "total": sum(r["other_system_candidates"] for r in scored)},
            "sampling_exclusions": sampling,
            # kinds never sampled because the product holds no root of them: a coverage
            # gap reported once, not items excluded from the score
            **({"kinds_not_held": not_held} if not_held else {}),
            # ENG-11 diagnostics, in no pass rule, zero counts left out (the verifier's
            # detail cap); items are drawn fresh each run, so no manual baseline can pair
            # with one and the block carries no comparison
            "measure": _nonzero(measuring.summarize(r.get("measure") for r in rows))}


def missing_reference_cause(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """Why a kind without items got no reference (``eval.missing_reference``), or None
    when it looked nothing up: a typed ``cause``, counts (``roots``: roots left without a
    reference; ``bc_finds``: BC probes made; ``bc_probe_budget``: BC probes the shared budget
    refused; ``bc_miss_stop``: BC probes not made once the kind stopped on
    ``cross_sample.BC_MISS_STOP`` consecutive misses; zero counts left out) and each reference source looked up and holding no record
    of the roots' identifiers (``no_record``; names as keys, so the verifier keeps them).
    ``budget_exhausted``: the shared BC probe budget refused a BC probe of the kind (checked
    first: a probe not made measures no mismatch, gap-b01a86b2f438); ``bc_source_mismatch``:
    a BC source was probed, every probe made or the kind stopped on consecutive misses
    (``bc_finds`` probes made, all missed: a measurement), and held
    none of them (a source whose lookups found records that named no root is
    ``no_naming_record``, one that found no record at all ``no_record``); ``no_reference``: only
    other systems' sources were looked up."""
    names = [public_source(str(n)) for n in entry.get("missing_reference") or []]
    unnamed = {public_source(str(n)) for n in entry.get("unnamed_reference") or []}
    skipped = entry.get("skipped") or {}
    if skipped.get("bc_probe_budget"):
        cause = "budget_exhausted"
    elif any(n.startswith("bc:") for n in names):
        cause = "bc_source_mismatch"
    elif names:
        cause = "no_reference"
    else:
        return None
    counts = {"roots": skipped.get("no_reference"), "bc_finds": entry.get("bc_finds"),
              "bc_probe_budget": skipped.get("bc_probe_budget"),
              "bc_miss_stop": skipped.get("bc_miss_stop")}
    return {"cause": cause, **{k: int(v) for k, v in counts.items() if v},
            **{n: "no_naming_record" if n in unnamed else "no_record"
               for n in names[:MAX_SOURCES_TRIED]}}


def _nonzero(summary: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _nonzero(v) if isinstance(v, Mapping) else v for k, v in summary.items()
            if v != 0}


def row_data_check(text: str, values: Iterable[str]) -> dict[str, Any]:
    """Whether ``text`` holds any of ``values`` (identifiers, key values, citations,
    product texts): a value of >= 4 characters leaks when it equals an output string or
    an identifier-shaped token of one, or (>= 8 characters) appears inside one."""
    strings: list[str] = []

    def collect(node: Any) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                strings.append(str(key))
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)
        elif isinstance(node, str):
            strings.append(node)

    collect(json.loads(text))
    tokens = set(strings)
    for value in strings:
        tokens.update(_TOKEN.findall(value))
    checked = leaked = 0
    for value in {v for v in values if isinstance(v, str) and len(v.strip()) >= 4}:
        checked += 1
        if value in tokens or value.strip() in tokens or (
                len(value) >= 8 and any(value in s for s in strings)):
            leaked += 1
    return {"values_checked": checked, "leaked": leaked}


def run_eval(drawn: Sample, product: Product, *, run_id: str, evidence_label: str,
             configured: Mapping[str, str], deadline: Deadline | None = None,
             incomplete: Sequence[str] = (), count: int | None = None,
             min_items: int = MIN_ITEMS, min_scored: int = MIN_SCORED) -> dict[str, Any]:
    """The report of one sample answered by ``product``; ``eval.fail_reasons`` is
    ``fail_reasons`` with ``min_items`` / ``min_scored`` (``count``: the requested item
    count, whose even share a stratum blocked before any item counts)."""
    rows = []
    for number, item in enumerate(drawn.items, start=1):
        log = measuring.CallLog(product.call) if product.call is not None else None
        product.call = log or product.call
        try:
            scored = (score_process_item(product, item, drawn.readings.get(item.hash16),
                                         deadline)
                      if item.kind == THREE_WAY else score_item(product, item, deadline))
        finally:
            product.call = log.call if log is not None else product.call
        bucket = PATH_BUCKET.get(scored["path"]) if scored["path"] else None
        row = {"n": number, **public_row(item), "bucket": bucket, **scored}
        if log is not None and scored["outcome"] not in UNSCORED:
            row["measure"] = item_measure(scored, log, evidence_label)
        cause = unscored_cause(row, product, item.relation,
                               dict(item.key).get("company_id"))
        rows.append({**row, **({"cause": cause} if cause else {})})
    reasons = list(incomplete)
    # expired(), not hit: a build that overran the deadline leaves no item to notice it
    if deadline is not None and deadline.expired() and "run_budget_exhausted" not in reasons:
        reasons.append("run_budget_exhausted")
    if product.info.get("status") == "not_built":
        reasons.append("product_not_built")
    if product.info.get("status") == "source_unavailable":
        reasons.append("product_source_unavailable")
    if product.info.get("mirror_complete") is False and \
            product.info.get("status") != "not_built":   # no sync ran: nothing to be incomplete
        reasons.append("mirror_incomplete")
    causes = read_causes(drawn)
    failure_causes = read_failure_causes(drawn)
    if any(cause in SOURCE_FAILURE_CAUSES for cause in causes.values()):
        reasons.append("source_read_failures")
    if any(cause == "budget" for cause in causes.values()) or any(
            v.get("budget_cut") for v in drawn.kinds.values()):
        reasons.append("sampling_budget_cut")
    if any(r["outcome"] == "blocked" and r.get("blocked") == "upstream" for r in rows):
        reasons.append("items_blocked_upstream")
    if any(r.get("blocked_aspects") and r["outcome"] != "blocked" for r in rows):
        reasons.append("aspects_blocked_upstream")
    counts = {"items": len(rows), "excluded": sum(r["outcome"] == "excluded" for r in rows),
              "blocked": sum(r["outcome"] == "blocked" for r in rows),
              **{o: sum(1 for r in rows if r["outcome"] == o)
                 for o in OUTCOMES + THREE_WAY_OUTCOMES}}
    kinds = {k: {"status": v.get("status"), "roots": v.get("roots", 0),
                 "candidates": v.get("candidates", 0),
                 "references": v.get("references", 0), "items": v.get("items", 0),
                 "skipped": dict(sorted((v.get("skipped") or {}).items())),
                 **({"bc_finds": v["bc_finds"]} if "bc_finds" in v else {}),
                 **({"reference_edges": v["reference_edges"]} if v.get("reference_edges")
                    else {}),
                 # per root relation: rows, roots and what the draw found for them
                 **({"funnel": {public_source(r): dict(c) for r, c in v["funnel"].items()}}
                    if v.get("funnel") else {}),
                 **({"reason": v["reason"]} if v.get("reason") else {}),
                 # a document kind's refused searches by cause (guard / tenant / other)
                 **({"search_refused": refused} if (refused := {
                     name[len("search_refused_"):]: int(n)
                     for name, n in (v.get("search") or {}).items()
                     if name.startswith("search_refused_")}) else {}),
                 # a mail unit that could not be asked beside units that answered, by cause
                 # (counted apart from the searches, never one refused search per unit)
                 **({"search_unit_unanswered": unit} if (unit := {
                     name[len("search_unit_"):]: int(n)
                     for name, n in (v.get("search") or {}).items()
                     if name.startswith("search_unit_")}) else {}),
                 **{f: v[f] for f in BLOCKED_FIELDS if v.get(f) is not None},
                 # mail its capture scope blocked: scope id -> later_ask code, never an address
                 **({"scope_blocked": scoped} if (scoped := public_scope_blocked(
                     v.get("scope_blocked"))) else {}),
                 **({"within_read": v["within_read"]} if v.get("within_read") else {}),
                 **({"missing_reference": list(v["missing_reference"])}
                    if v.get("missing_reference") else {}),
                 **({"unnamed_reference": list(v["unnamed_reference"])}
                    if v.get("unnamed_reference") else {}),
                 **({"budget_cut": True} if v.get("budget_cut") else {}),
                 # what a kind without references tried; a short read says so
                 **({"tried": list(v.get("tried", []))}
                    if v.get("status") != "read" else {}),
                 **({"complete": False} if v.get("complete") is False else {}),
                 **({"withheld": v["withheld"]} if v.get("withheld") else {}),
                 **({"readings": v["readings"]} if v.get("readings") else {})}
             for k, v in drawn.kinds.items()}
    report = {"schema": SCHEMA, "evidence_label": evidence_label,
              "adapter_status": "live-unverified", "values_published": False,
              "run_id": run_id, "sources": dict(configured),
              "product": dict(product.info, available=product.available),
              "eval": eval_block(rows, run_id=run_id, evidence_label=evidence_label,
                                 product=product, kinds=kinds, incomplete=reasons,
                                 count=count),
              "counts": counts, "kinds": kinds,
              # target -> cause (budget / not_exposed / not_queryable / upstream), source
              # failures first; bounded so the verifier's capped detail keeps items_digest
              "read_failures": dict(sorted(causes.items(), key=lambda kv: (
                  kv[1] not in SOURCE_FAILURE_CAUSES, kv[0]))[:MAX_READ_FAILURES]),
              "read_failures_total": len(causes),
              **({"read_failure_causes": failure_causes} if failure_causes else {}),
              "document_funnel": document_funnel(drawn),
              # one fingerprint of the sample (sorted item hashes), so runs compare cheaply
              "items_digest": hashlib.sha256(",".join(sorted(
                  r["hash16"] for r in rows)).encode()).hexdigest()[:16],
              "items": rows}
    seen = set(product.seen)
    for item in drawn.items:
        seen.add(item.identifier)
        seen.update(str(v) for _, v in item.key)
    report["row_data_check"] = row_data_check(json.dumps(report), seen)
    report["eval"]["fail_reasons"] = fail_reasons(report, min_items=min_items,
                                                  min_scored=min_scored)
    causes = blocked_causes(report["eval"])
    if causes and not report["eval"]["fail_reasons"]:
        # only a run that does not fail reads blocked; a failing one fails. One code is
        # what the verifier reads (an enum string: no comma, at most 64 characters); the
        # other typed kinds' codes are listed beside it
        report["eval"]["blocked_cause"] = causes[0]
        if len(causes) > 1:
            report["eval"]["blocked_also"] = causes[1:]
    return report


def read_causes(drawn: Sample) -> dict[str, str]:
    """Each failed oracle read's cause (``source.FAILURE_CAUSES``); a failure the sampler
    gave no cause for is ``upstream``."""
    return {target: drawn.causes.get(target, "upstream") for target in drawn.failures}


def read_failure_causes(drawn: Sample) -> dict[str, dict[str, int]]:
    """Per source, the typed cause of its failed reads and how many failed that way
    (``http_<status>``, ``error_<code>``, ``timeout``, ``budget``, ``upstream``; a document
    kind's search that failed, and ``withheld`` for what its reader withheld); counts only,
    bounded to ``MAX_READ_FAILURES`` sources, source failures first. Empty: nothing failed."""
    out: dict[str, dict[str, int]] = {}

    def add(source: str, label: str, number: int = 1) -> None:
        tally = out.setdefault(source, {})
        tally[label] = tally.get(label, 0) + number

    for target in drawn.failures:
        add(public_source(target), drawn.labels.get(target, "upstream"))
    for kind in DOCUMENT_KINDS:
        entry = drawn.kinds.get(kind) or {}
        reason = str(entry.get("reason") or "")
        if entry.get("status") == "unavailable":
            add(kind, read_failure_label(
                reason, entry.get("http_status"), entry.get("error_code"),
                "budget" if entry.get("blocked") == "budget" else "upstream"))
        if entry.get("withheld"):
            add(kind, "withheld", int(entry["withheld"]))
    return dict(sorted(out.items())[:MAX_READ_FAILURES])


def document_funnel(drawn: Sample) -> dict[str, dict[str, int]]:
    """The mail and SharePoint document funnel, counts only: ``listed`` (distinct documents
    the search found), ``read`` (their texts read), ``classified`` (documents naming exactly one
    root of those searched this run; ones naming two are excluded, ``documents_naming_many``)
    and ``candidates`` (references drawn from them); a kind not configured is left out."""
    out: dict[str, dict[str, int]] = {}
    for kind in DOCUMENT_KINDS:
        entry = drawn.kinds.get(kind) or {}
        if entry.get("status") == "not_configured" or not entry:
            continue
        search = entry.get("search") or {}
        out[kind] = {"listed": int(search.get("documents", 0)),
                     "read": int(search.get("texts_read", 0)),
                     "classified": int(search.get("documents_naming_one", 0)),
                     "candidates": int(entry.get("candidates", 0))}
    return out


def fail_reasons(report: Mapping[str, Any], *, min_items: int = MIN_ITEMS,
                 min_scored: int = MIN_SCORED) -> list[str]:
    """Why the run fails (empty: it passes). Always: the product unavailable or its
    projection empty, a failing kind (``kinds_failing``), wrong answers above the bound
    (``eval.wrong_within_bound``: <= 1/10 of the scored items), a failed product tool call
    (``product_error``), a budget cut, a leak, no scored BC-rooted item
    (``no_bc_rooted_scored``). Rule ``every_kind`` also needs ``min_items`` sampled and
    ``min_scored`` scored (a rate over fewer says nothing); under rule ``portal_outage``
    (the Portal failed the whole sync) the scored BC-rooted item is what is measured: the
    Portal strata are blocked and counted, and a run without failures reads blocked
    (``blocked_cause`` ``upstream_outage``), never a pass. ``session_not_committed``: the run
    was given the session projection, found none committed and built its own product.
    Rates are measured, never asserted."""
    block = report["eval"]
    product = block["product"]
    checks = [
        ("product_unavailable", not product["available"]),
        ("projection_empty", product["available"] and product["object_count"] <= 0),
        # a typed block (mail) excuses its kinds; the outage rule excuses its own in
        # kinds_failing, so a kind it asked that failed still fails
        ("kinds_failing", bool(kinds_failing(block)) and (
            block["pass_rule"] == "portal_outage" or blocked_cause(block) is None)),
        ("wrong_above_bound", not block["wrong_within_bound"]),
        ("product_error", bool(block["answer"].get("product_error")
                               or block["find_root"].get("product_error"))),
        ("run_budget_exhausted", "run_budget_exhausted" in block["incomplete"]),
        ("session_not_committed", product.get("session") == SESSION_NOT_COMMITTED),
        ("row_data_leaked", report["row_data_check"]["leaked"] != 0)]
    # BC-rooted items are scored under either rule (bc_invoice roots, or a document naming
    # one): a run that never asked BC measures nothing of it
    checks.append(("no_bc_rooted_scored", block["bc_rooted"]["scored"] < 1))
    if block["pass_rule"] != "portal_outage":
        checks += [("too_few_items", report["counts"]["items"] < min_items),
                   ("too_few_scored", block["scored"] < min_scored)]
    return [name for name, failed in checks if failed]


def verdict(report: Mapping[str, Any], *, min_items: int = MIN_ITEMS,
            min_scored: int = MIN_SCORED) -> int:
    """0 when ``fail_reasons`` is empty, else 3."""
    return 3 if fail_reasons(report, min_items=min_items, min_scored=min_scored) else 0


# ----------------------------------------------------------------- wiring

def root_relations() -> list[str]:
    """Portal relations of the sampled record kinds (the oracle's strata)."""
    return sorted({relation for _, roots in PORTAL_STRATA.values() for relation, _ in roots})


def _fixture_fetch(world: Any) -> Callable[..., Any]:
    """The fixture world's API routes with the live catalog's ``since`` shape
    (``{"column", "mode"}``), which the source mirror requires; the oracle never reads it."""
    modes = {"audtdate": "yyyymmdd-text", "audittimestampmodified": "timestamp-utc"}

    def fetch(url: str, headers: Mapping[str, str], *, max_bytes: int = 0) -> Any:
        result = world.fetch(url, headers, max_bytes=max_bytes)
        if isinstance(result, Mapping) and isinstance(result.get("relations"), list):
            for entry in result["relations"]:
                if isinstance(entry.get("since"), str):
                    entry["since"] = {"column": entry["since"],
                                      "mode": modes.get(entry["since"], "timestamptz")}
        return result
    return fetch


def sampling_guard(deadline: Deadline, reserve_seconds: float,
                   clock: KindClock | None = None) -> Callable[[], None]:
    """The oracle reads' guard: it fires ``reserve_seconds`` before the run deadline, so
    sampling never spends the share kept for the finds, and at the end of the drawing
    kind's time slice (``clock``), so one slow kind never starves the others."""
    def guard() -> None:
        if deadline.remaining() <= reserve_seconds:
            raise BudgetExhausted("sampling_budget_exhausted")
        if clock is not None and clock.spent():
            raise BudgetExhausted("kind_budget_exhausted")
    return guard


def sampling_clock(deadline: Deadline, reserve_seconds: float) -> KindClock:
    """Per-kind slices of the sampling time: what the run has left before the finds'
    reserve, shared evenly by the kinds still to draw."""
    return KindClock(lambda: deadline.remaining() - reserve_seconds, deadline._monotonic)


def product_from_build(client: Any, directory: Path, deadline: Deadline,
                       reserve_seconds: float, unstable: Iterable[str] = (), *,
                       total_seconds: float | None = None,
                       share: float = DEFAULT_BUILD_SHARE,
                       state_dir: Path | None = None,
                       bc_capture: BcCapture | None = None,
                       projection_reserve: float = 0.0) -> Product:
    """Build the product before anything is sampled, so sampling and the BC probes only
    get what is left. The build's budget is its ``share`` of ``total_seconds`` (the run
    budget) when that is known, never more than the run has left after ``reserve_seconds``
    kept for the finds; ``projection_reserve`` of it is kept back from the sync for the
    projection. ``bc_capture``: the product captures BC itself (``build_projection``),
    never past ``deadline`` less the finds' reserve, even when its floor binds."""
    budget = deadline.remaining() - reserve_seconds
    if total_seconds is not None:
        budget = min(budget, total_seconds * share)
    store, info, complete = build_projection(client, directory, product_relations(),
                                             budget_seconds=budget, unstable=unstable,
                                             state_dir=state_dir, bc_capture=bc_capture,
                                             projection_reserve=projection_reserve,
                                             run_left=lambda: (deadline.remaining()
                                                               - reserve_seconds))
    if store is None:
        return Product(None, info)
    return open_product(store, info=info, complete_relations=complete,
                        upstream_failed=info.get("upstream_failed"))


def product_relations() -> list[str]:
    """The Portal relations the product holds for the sampled roots and the three-way
    question (``required_relations`` and ``process_relations``, from the schema alone)."""
    from ontology.schema import load_schema

    schema = load_schema()
    return sorted(set(required_relations(schema, root_relations()))
                  | set(process_relations(schema)))


def product_from_session(directory: Path, session: Mapping[str, Any]) -> Product:
    """The product over the verifier session's committed projection (``session``:
    ``scripts.ontology_projection_probe.open_session``), through the dev baseline's one
    session path (``eval_dev_baseline.session_product``, the producer's mirror read in
    place): no sync, BC capture or projection of its own, so the build's share of the run
    budget goes to sampling. A Portal relation is held when the producer's mirror completed
    it; a Portal the producer did not project blocks every relation upstream, as a
    whole-sync outage does (``build_projection``)."""
    from scripts.eval_dev_baseline import session_product
    from scripts.mirror_measure import completeness

    relations = product_relations()
    built = session_product(directory, relations, session, copy_mirror=False)
    mirror = session["manifest"].get("mirror") or {}
    complete = frozenset(set(relations) & set(mirror.get("complete") or ()))
    unstable = set(relations) & set(mirror.get("unstable_relations") or ())
    down = built.portal_down
    upstream = {} if down is None else {name: dict(down) for name in relations}
    info = {**built.info, "source": "session_projection", "upstream_failed": upstream,
            "completeness": completeness(complete, bound=relations, unstable=unstable,
                                         scope="eval_required")}
    return open_product(built.store, info=info, complete_relations=complete,
                        upstream_failed=upstream)


def fixture_product(world: Any, directory: Path, *, budget_seconds: float = 600.0,
                    reserve_seconds: float = 0.0) -> Product:
    from integrations.workbench_read_api import WorkbenchReadApiClient

    client = WorkbenchReadApiClient("https://workbench-fixture.invalid", "plxwb_" + "f" * 40,
                                    fetch=_fixture_fetch(world))
    return product_from_build(client, directory, Deadline(budget_seconds), reserve_seconds,
                              total_seconds=budget_seconds)


def scope_target(path: Path) -> tuple[str, list[str], dict[str, int]]:
    """The drive id, folders and walk bounds of the one drive target of a SharePoint
    capture scope file (``examples/sharepoint_capture_scope.*.json``), parsed by the
    capture's own ``CaptureScope.from_file`` so its ``max_depth``, ``max_folders`` and
    ``max_content_bytes`` bound the SharePoint kind's reads. A scope the capture refuses,
    a site or discovery target, or more than one target is a ValueError, so the kind
    never reads a drive the scope does not name exactly."""
    from integrations.capture_sharepoint import CaptureError, CaptureScope

    scope = CaptureScope.from_file(path)
    if len(scope.targets) != 1 or scope.discover is not None \
            or scope.targets[0].drive_id is None:
        raise CaptureError("scope_not_one_drive_target")
    target = scope.targets[0]
    return target.drive_id, list(target.folders), {
        "max_drive_depth": target.max_depth, "max_drive_folders": target.max_folders,
        "max_text_bytes": scope.max_content_bytes}


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_unfamiliar_sample",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("live", help="credentialed, GET only; counts only")
    live.add_argument("--run-id", default="auto",
                      help="sampling seed; 'auto' = live-<UTC hour> (a new sample each hour)")
    live.add_argument("--no-bc", action="store_true")
    live.add_argument("--measure-sales-order-numbers", type=int, default=0, metavar="PROBES",
                      help="gap-b01a86b2f438: add measure_sales_order_numbers to the report "
                           "(shape classes and match counts of the Portal sales-order "
                           "numbers against BC fields, never a value; PROBES exact BC "
                           "lookups per field pair, up to 7 x 6 x PROBES reads, stopped "
                           "at the run deadline)")
    live.add_argument("--mailbox")
    live.add_argument("--folder")
    live.add_argument("--mail-scope", type=Path, default=None,
                      help="a mail capture scope (plx-mail-capture-scope/v1, named mailboxes "
                           "only): its allowlisted mailboxes and folders are the mail read, "
                           "instead of --mailbox/--folder, each reported by scope id; one "
                           "outside the allowlist is blocked (later_ask mail-scope-grant)")
    live.add_argument("--sharepoint-path", action="append", default=[])
    live.add_argument("--sharepoint-scope", type=Path, default=None,
                      help="a SharePoint capture scope (plx-sharepoint-capture-scope/v1, one "
                           "drive target): its drive and folders are the SharePoint paths "
                           "read, instead of SHAREPOINT_DRIVE_ID and --sharepoint-path, "
                           "within its max_depth, max_folders and max_content_bytes")
    product = live.add_mutually_exclusive_group()
    product.add_argument("--store", type=Path, help="a maintained projection store (reused)")
    product.add_argument("--build-store", type=Path,
                         help="throwaway directory: schema-derived Portal mirror + projection")
    live.add_argument("--session-projection", type=Path, default=None,
                      help="the verifier session's projection (ontology.session_projection, "
                           "{session_state}): opened read-only when this session committed "
                           "it, else the run builds into --build-store")
    live.add_argument("--previous-report", default=None,
                      help="last daily verifier report: its unstable relations are not "
                           "synced and leave the completeness denominator")
    fixture = commands.add_parser("fixture", help="keyless, injected transport")
    fixture.add_argument("--run-id", default=FIXTURE_RUN_ID)
    fixture.add_argument("--no-product", action="store_true")
    for sub in (live, fixture):
        sub.add_argument("--count", type=int, default=28)
        sub.add_argument("--min-scored", type=int, default=MIN_SCORED,
                         help="fail unless at least this many items were scored")
        sub.add_argument("--total-budget-seconds", type=float, default=DEFAULT_TOTAL_BUDGET)
        sub.add_argument("--build-share", type=float, default=DEFAULT_BUILD_SHARE,
                         help="share of the total budget reserved for the product build, "
                              "which runs before sampling")
        sub.add_argument("--projection-reserve-seconds", type=float,
                         default=DEFAULT_PROJECTION_RESERVE,
                         help="of the build's share, kept back from the mirror sync and BC "
                              "capture for the projection")
        sub.add_argument("--find-reserve-seconds", type=float, default=DEFAULT_FIND_RESERVE,
                         help="of the whole run, kept back for the finds (sampling stops "
                              "there; the rest of the sampling time is sliced per kind)")
    args = parser.parse_args(argv)
    if getattr(args, "session_projection", None) is not None and args.build_store is None:
        parser.error("--session-projection needs --build-store (its cold fallback)")
    scope = None
    if getattr(args, "sharepoint_scope", None) is not None:
        if args.sharepoint_path:
            parser.error("--sharepoint-scope replaces --sharepoint-path")
        try:
            scope = scope_target(args.sharepoint_scope)
        except (OSError, ValueError) as exc:
            parser.error(f"--sharepoint-scope: {type(exc).__name__}")
    if getattr(args, "mail_scope", None) is not None:
        if args.mailbox or args.folder:
            parser.error("--mail-scope replaces --mailbox and --folder")
        try:
            args.mail_units, args.mail_blocked = mail_scope_target(args.mail_scope)
        except (OSError, ValueError) as exc:
            parser.error(f"--mail-scope: {type(exc).__name__}")
    if args.min_scored > args.count:
        parser.error(f"--min-scored {args.min_scored} exceeds --count {args.count}: "
                     "the check could never pass")
    env = os.environ if environ is None else environ
    deadline = Deadline(args.total_budget_seconds)
    floors = {"count": args.count, "min_items": min(MIN_ITEMS, args.count),
              "min_scored": args.min_scored}
    try:
        from scripts import eval_oracle_dev as dev

        if args.command == "fixture":
            import tempfile
            from evals.oracle.fixture_world import build_world

            sources, documents, configured, _ = dev.build_fixture()
            with tempfile.TemporaryDirectory() as tmp:
                if args.no_product:
                    product_path = Product(None, {"status": "disabled"})
                else:
                    from integrations.workbench_read_api import WorkbenchReadApiClient
                    client = WorkbenchReadApiClient(
                        "https://workbench-fixture.invalid", "plxwb_" + "f" * 40,
                        fetch=_fixture_fetch(build_world()))
                    product_path = product_from_build(
                        client, Path(tmp) / "product", deadline, args.find_reserve_seconds,
                        total_seconds=args.total_budget_seconds, share=args.build_share,
                        projection_reserve=args.projection_reserve_seconds)
                with product_path:
                    drawn = sample(sources, run_id=args.run_id, count=args.count,
                                   documents=documents,
                                   record_kinds=product_path.held_record_kinds())
                    report = run_eval(drawn, product_path, run_id=args.run_id,
                                      evidence_label="injected-transport",
                                      configured=configured, deadline=deadline, **floors)
        else:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            run_id = args.run_id if args.run_id != "auto" else "live-" + now[:13]
            clock = sampling_clock(deadline, args.find_reserve_seconds)
            if scope is not None:
                env = {**env, "SHAREPOINT_DRIVE_ID": scope[0]}
                args.sharepoint_path, args.sharepoint_bounds = list(scope[1]), scope[2]
            sources, documents, configured = dev.build_live(
                env, args, guard=sampling_guard(deadline, args.find_reserve_seconds, clock))
            session = None
            if args.session_projection is not None:
                from integrations import bc_read
                from scripts.ontology_projection_probe import open_session
                session = open_session(str(args.session_projection),
                                       bc_read.session_fresh_since(env))
            if args.store is not None:
                product_path = open_product(args.store, info={"source": "store"})
            elif session is not None:
                product_path = product_from_session(args.build_store, session)
            elif args.build_store is not None:
                from integrations.workbench_read_api import WorkbenchReadApiClient
                client = WorkbenchReadApiClient(env.get("PLX_WORKBENCH_API_URL", ""),
                                                env.get("PLX_WORKBENCH_API_KEY", ""))
                from integrations.mirror_capture import default_state_dir
                from scripts.mirror_measure import previous_unstable
                bc_capture = None if args.no_bc or not all(env.get(n) for n in BC_ENV) \
                    else (lambda db, seconds: capture_bc(env, db, seconds))
                product_path = product_from_build(client, args.build_store, deadline,
                                                  args.find_reserve_seconds,
                                                  previous_unstable(args.previous_report),
                                                  total_seconds=args.total_budget_seconds,
                                                  share=args.build_share,
                                                  state_dir=default_state_dir(env),
                                                  bc_capture=bc_capture,
                                                  projection_reserve=(
                                                      args.projection_reserve_seconds))
                if args.session_projection is not None:   # the session's was not committed
                    product_path.info["session"] = SESSION_NOT_COMMITTED
            else:
                product_path = Product(None, {"status": "not_configured"})
            with product_path:
                drawn = sample(sources, run_id=run_id, count=args.count, documents=documents,
                               record_kinds=product_path.held_record_kinds(), clock=clock,
                               now=now)
                report = run_eval(drawn, product_path, run_id=run_id,
                                  evidence_label="live-read", configured=configured,
                                  deadline=deadline, **floors)
            if args.measure_sales_order_numbers > 0:
                from evals.oracle.number_shapes import measure
                report["measure_sales_order_numbers"] = measure(
                    sources, args.measure_sales_order_numbers, remaining=deadline.remaining)
    except Exception as exc:  # noqa: BLE001 - a typed error line, never a value
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1, sort_keys=True))
    return 3 if report["eval"]["fail_reasons"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
