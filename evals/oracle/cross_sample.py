"""Cross-system root sampling for the unfamiliar-transaction find eval (eval.unfamiliar_sample).

Directive 4190c1c690ce item 5 / 4e839fb7cabf (3): every run samples real transactions
across kinds, seeded by the run id, and takes each question's identifier from a record of
**another** system than the root's. The product is then asked to find the root from that
identifier alone. This module is the sampling half and lives inside the oracle lane, so the
import ban (``scripts/check_oracle_imports.py``) keeps it independent of the projection and
resolver it grades: it reads only the injected oracle sources (Portal rows, BC documents,
mail/SharePoint texts) and joins only on ``keys.EDGES``.

Kinds (``KINDS``):

* ``sales_order``, ``purchase_order``, ``assembly_order``, ``receipt``: a record of the
  stratum's relations (``generator.PORTAL_STRATA``; receipts also FM raw receipts) is the
  root; a sales order also roots at a BC sales order (``BC_NATIVE_ROOT_SETS``,
  ``bc_native_roots``: a Portal row is optional, and the BC copy of a Portal order is that
  order, one root);
* ``bc_invoice``: a posted BC sales or purchase invoice is the root;
* ``po_three_way`` (directive 541a4f06b2dd): a Portal purchase order (``app_purchase_order``)
  with a Portal receipt or a sampled BC purchase invoice carrying its number is the root;
  the question is its three-way state (``plx.process``), and the item carries the oracle's
  own reading (``three_way.reading``: stages and sides as enums) for the scorer;
* ``mail``, ``sharepoint``: a document names the root (a record of any of the record
  kinds above, or a BC order or posted receipt, ``bc_document_roots``, as the generator's
  document roots), found the one way the generator's document strata find one
  (``generator.document_candidates``): the documents are searched for the numbers of the
  roots, and a hit names a root by the generator's span rules (a BC order the Portal
  drained under a Portal record's number is that record, ``generator.twins``).

Reference rule (one rule, no per-kind branch). For a record root ``R`` in relation ``Rr``,
every edge of ``keys.EDGES`` or ``keys.REFERENCE_EDGES`` that joins ``Rr.F`` to ``Xr.G``
with ``Xr`` in a different system (Portal gold, FileMaker, Sage, Business Central) is a
possible reference, unless ``G`` is a surrogate key of ``Xr`` (its key columns, ``id``): a
person never reads one.
The identifier is ``X.G`` exactly as the other system stores it. It counts only when the
oracle's own lookup of that identifier on ``Rr.F`` finds exactly ``R`` with no gap (one
record, one BC company, the PO's own vendor for BC purchase documents, complete reads), so
the question has one known root. A BC root whose edges give no such record (the other
system's read failed or holds no match) may use its own copy of the other system's
identifier instead (``carried_reference``). A document reference counts only when the
document names exactly one record across every root read (``generator.document_index``)
and the oracle's lookup of that record's own number finds exactly it; the question asks
that number.

BC records the run already sampled (the ``bc_invoice`` roots) are indexed by every field a
reference edge reads on their entity set (``sampled_index``), and a Portal root looks there
first (``sampled_reference``): a sampled record holding its identifier gives the reference
with the same checks and no BC probe; the probes (``bc_finds_total``) are spent only on the
roots the index does not name.

Per kind the report counts ``candidates`` (the roots the kind read, or the documents its
searches found, before any reference was looked for) and names what it ``tried`` (relation
columns, BC entity sets, mailbox/folder or drive paths; ``documents:<kind>`` when no
document port was asked), so a kind without items is never a silent zero; a document
kind's ``search`` holds its search funnel (``generator.document_candidates``). ``funnel`` breaks a record kind down per root
relation (``FUNNEL``): rows read, rows without an identifier, staging rows left out, roots,
then per root drawn whether it had no reference edge, no reference or a reference (of
those, how many came from a sampled BC record), and how many the slice end left untried.

Time (``KindClock``): after the shared Portal read, each kind that draws gets its own slice
of the sampling budget, in ``DRAW_ORDER`` (``bc_invoice`` first: its roots feed the
three-way candidates and the documents' index; the documents last: they need every root
indexed). In this first pass a slice is the kind's fair share: an even share of the time
left, never more than an even share of the time the first slice started with, so a kind
later in the order gets no more than an earlier one and the time a kind leaves is kept for
the second pass instead of going to the next kind (live 37e539e: the documents' further
reads took the time the sales order kind's slice left and it was blocked by the budget). A
slow kind stops at its slice end (``skipped.kind_budget``, ``budget_cut``; ``status:
unavailable`` with a budget block when it got no reference, ``within_read`` naming what the
roots or documents it did try lacked), never starving the kinds after it. Once every kind
has drawn, every kind its slice cut gets a second slice, in an even share of what is left
(and of the BC probes left): a record kind resumes its reference reads on the roots it left
untried, a document kind its slice left without a reference searches again, so each keeps
a share of its own whatever the other kinds' reads took.

A record kind left without a reference names the reference sources it looked up and found
no record in (``missing_reference``: other relation.field per declared edge; live receipts
are FM raw receipts whose one cross-system edge reads ``public.app_receipt.receipt_number``),
so "no reference" says which source holds no copy of the identifier.

Seeding: one ``random.Random`` seeded by the run id; every pool is sorted before it is
shuffled, so one run id over one source state gives the same sample. The sample carries
identifiers and keys for the caller's scoring; ``public_row`` is the only shape meant for
output (kinds, edge names and a 16-hex hash, never a value).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import random
from typing import Any, Callable, Container, Mapping, Sequence

from . import keys as K
from .families import Context, Node, Sources
from .generator import (BC_DOCUMENT_ROOT_SETS, BC_DOCUMENT_STRATUM, BC_INVOICE_SETS,
                        BC_ORDER_EDGES, MAX_ROOT_SEARCHES, PORTAL_STRATA, Candidate,
                        bc_dated, bc_read, block_refused_scope, block_refused_upstream,
                        document_candidates,
                        lineage, lookup_failed, record_tried, search_status, twins,
                        unread_status)
from .source import (BUDGET_STOP, DocumentPort, ReadFailed, canonical, digest, field_value,
                     key_text, sql_trim)
from .three_way import reading as three_way_reading

FORMAT = "plx-eval-cross-sample/v1"
# What an unavailable kind says about its failed read (source.ReadFailed.fields).
BLOCKED_FIELDS = ("blocked", "http_status", "error_code")
RECORD_KINDS = ("sales_order", "purchase_order", "assembly_order", "receipt", "bc_invoice")
DOCUMENT_KINDS = ("mail", "sharepoint")
# A process question over a purchase-order root (its roots are purchase_order roots).
THREE_WAY = "po_three_way"
THREE_WAY_ROOT = K.PO
KINDS = RECORD_KINDS + (THREE_WAY,) + DOCUMENT_KINDS
# The order in which kinds draw, each in its own time slice (``KindClock``).
DRAW_ORDER = ("bc_invoice", "sales_order", "purchase_order", "assembly_order", "receipt",
              THREE_WAY) + DOCUMENT_KINDS
# What the three-way stratum reads to find its candidates (Portal PO, its receipts, and the
# order No. of the sampled BC purchase invoices).
THREE_WAY_TRIED = (f"{K.PO}.po_number", f"{K.RECEIPT}.purchase_order_id",
                   "bc:purchaseInvoices.orderNumber")
# Own identifier field of a BC document root: its document No. (keys.ROOT_KEYS bc_number).
BC_OWN_FIELD = "number"
# Per root relation of a record kind (``funnel``): the read stages, then the draw stages.
FUNNEL = ("rows", "no_number", "native_staging", "portal_twin", "roots", "no_reference_edge",
          "no_reference", "references", "from_sampled_bc", "untried")
# A record kind's BC-native root sets (decision 2026-10-18-unfamiliar-roots-bc-native-
# sales-order-roots): a BC sales order is a sales_order root, Portal row or not.
BC_NATIVE_ROOT_SETS = {"sales_order": ("salesOrders",)}
# A kind stops drawing BC probes after this many consecutive probes that yielded no
# reference naming the root (decision 89aed11571b0; gap-51ebb6c78e90: a BC record found that
# names no root, e.g. another database's order carrying a shared customer PO, is a miss
# too): BC holds no copy of its roots, and its unspent share goes to the
# later kinds instead of being refused root after root. Below the even share of the default
# 150 probes over the six probing kinds (25), so the stop, not the budget, ends such a kind.
BC_MISS_STOP = 20


@dataclass(frozen=True)
class Root:
    """A sampled record root: where it lives and the field a person names it by."""

    kind: str
    relation: str               # Portal relation or ``bc:<entitySet>``
    key: tuple[tuple[str, Any], ...]
    field: str                  # the root's own identifier field
    identifier: str
    node: Node = field(compare=False, hash=False, repr=False)


@dataclass(frozen=True)
class Item:
    """One question: find ``root`` from ``identifier`` (taken from ``reference``)."""

    kind: str                   # the sampled kind (KINDS)
    root_kind: str              # the root's record kind (RECORD_KINDS, or
    #                             BC_DOCUMENT_STRATUM: a BC order or receipt a document named)
    relation: str
    key: tuple[tuple[str, Any], ...]
    identifier: str
    reference: str              # edge name, or the document kind
    reference_system: str
    hash16: str


@dataclass
class Sample:
    items: list[Item]
    kinds: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    # hash16 -> the oracle's three-way reading of a po_three_way item (enums only)
    readings: dict[str, dict[str, Any]] = field(default_factory=dict)
    # failed read target -> its cause (source.FAILURE_CAUSES)
    causes: dict[str, str] = field(default_factory=dict)
    # failed read target -> its typed cause label (``ReadFailed.label``)
    labels: dict[str, str] = field(default_factory=dict)


class KindClock:
    """Per-kind time slices of the sampling budget. ``start(kinds_left)`` opens the slice
    of the kind about to draw: an even share of ``seconds_left()``. With ``slices`` (the
    first pass's kind count) the share is at most the fair share, an even share of the time
    left at the first such slice, so a kind that ends early keeps its rest for the second
    pass and no kind gets more than its fair share because it draws later. ``spent()`` says
    the open slice is over; the caller's read guard checks it too, so a slow read stops at
    the slice end (a budget stop, recorded as a failed read, never as absence)."""

    def __init__(self, seconds_left: Callable[[], float],
                 monotonic: Callable[[], float]) -> None:
        self._left = seconds_left
        self._now = monotonic
        self._end: float | None = None
        self._fair: float | None = None

    def start(self, kinds_left: int, slices: int | None = None) -> float:
        left = max(0.0, self._left())
        share = left / max(1, kinds_left)
        if slices is not None:
            if self._fair is None:
                self._fair = left / max(1, slices)
            share = min(share, self._fair)
        self._end = self._now() + share
        return share

    def stop(self) -> None:
        self._end = None

    def spent(self) -> bool:
        return self._end is not None and self._now() >= self._end


def _never() -> bool:
    return False


def system_of(relation: str) -> str:
    """Which system holds a relation: ``bc``, ``portal`` (gold ``public.app_*``), ``fm``
    (other ``public.*``, the FileMaker replica) or the schema name (``sage``, ``silver``)."""
    if relation.startswith("bc:"):
        return "bc"
    schema, _, name = relation.partition(".")
    if schema == "public":
        return "portal" if name.startswith("app_") else "fm"
    return schema


def _surrogate(sources: Sources, relation: str, column: str) -> bool | None:
    """Whether ``column`` is a surrogate key of ``relation``; None when the relation's key
    columns cannot be read (its system is down): undecided, so the edge stays declared and
    only a carried reference (``record_reference``) can use it."""
    if column == "id" or column.endswith("_id"):
        return True
    if relation.startswith("bc:"):
        return False
    try:
        return column in sources.portal.key_columns(relation)
    except ReadFailed:
        return None


def reference_edges(sources: Sources, relation: str) -> list[tuple[str, str, str, str]]:
    """``(edge name, root field, other relation, other field)`` for every declared edge
    that joins ``relation`` to a record of another system on a non-surrogate field. An
    edge and its reverse are one reference (the first-declared name is kept)."""
    out: list[tuple[str, str, str, str]] = []
    seen: set[frozenset[tuple[str, str]]] = set()
    for edge in K.EDGES + K.REFERENCE_EDGES:
        if edge.terminal:
            continue
        if edge.source == relation:
            own, other, other_field = edge.source_field, edge.target, edge.target_field
        elif edge.target == relation:
            own, other, other_field = edge.target_field, edge.source, edge.source_field
        else:
            continue
        pair = frozenset({(relation, own), (other, other_field)})
        if pair in seen or system_of(other) == system_of(relation):
            continue
        if _surrogate(sources, other, other_field) is True:
            continue
        seen.add(pair)
        out.append((edge.name, own, other, other_field))
    return out


# ----------------------------------------------------------------- reads

def _nodes(ctx: Context, relation: str, column: str, value: Any) -> list[Node]:
    return ctx.lookup(relation, column, value)


def _vendor_checked(ctx: Context, portal_node: Node, bc_relation: str,
                    bc_nodes: list[Node]) -> list[Node]:
    """BC purchase documents count only for the PO's own vendor (families.Context rule)."""
    if bc_relation.startswith("bc:") and bc_relation in _VENDOR_SETS:
        return ctx.vendor_filter(portal_node, bc_relation[3:], bc_nodes)
    return bc_nodes


_VENDOR_SETS = frozenset({"bc:purchaseOrders", "bc:purchaseReceipts", "bc:purchaseInvoices"})


def names_exactly(sources: Sources, root: Root, column: str, identifier: str,
                  via: Node | None = None) -> bool:
    """The oracle's own lookup of ``identifier`` on ``root.relation.column`` finds exactly
    the root, with no gap (a failed, capped or cross-company read, or another vendor)."""
    return _names(sources, root, column, identifier, via) is True


def _names(sources: Sources, root: Root, column: str, identifier: str,
           via: Node | None = None) -> bool | None:
    """``names_exactly``, or None when a read of the lookup failed or was cut off
    (``generator.lookup_failed``): it decided nothing, so it is no ``not_unique``."""
    ctx = Context(sources, {})
    found = _nodes(ctx, root.relation, column, identifier)
    if via is not None and not via.is_bc and root.relation.startswith("bc:"):
        found = _vendor_checked(ctx, via, root.relation, found)
    if lookup_failed(ctx.gaps):
        return None
    if ctx.gaps or len(found) != 1:
        return False
    return tuple(sorted(ctx.key(found[0]).items())) == root.key


def _unnamed(named: bool | None, skipped: dict[str, int]) -> None:
    """Counts a lookup that did not name the root: ``reference_read_gap`` when its read
    failed (``_names`` None), else ``not_unique``."""
    stage = "reference_read_gap" if named is None else "not_unique"
    skipped[stage] = skipped.get(stage, 0) + 1


# ----------------------------------------------------------------- roots

def _funnel_row(funnel: dict[str, dict[str, int]] | None, relation: str) -> dict[str, int]:
    """The ``FUNNEL`` counts of ``relation`` (a throwaway row when no funnel is kept)."""
    row = {} if funnel is None else funnel.setdefault(relation, {})
    for stage in FUNNEL:
        row.setdefault(stage, 0)
    return row


def portal_roots(sources: Sources, kind: str,
                 funnel: dict[str, dict[str, int]] | None = None) -> list[Root]:
    _, relations = PORTAL_STRATA[kind]
    portal = sources.portal
    out = []
    for relation, column in relations:
        counts = _funnel_row(funnel, relation)
        for row in portal.rows(relation):
            counts["rows"] += 1
            identifier = key_text(row.get(column))
            if identifier is None:
                counts["no_number"] += 1
                continue
            if lineage(relation, row, portal) == "gold_native_staging":
                counts["native_staging"] += 1
                continue
            counts["roots"] += 1
            key = tuple(sorted(portal.key_of(relation, row).items()))
            out.append(Root(kind, relation, key, column, identifier, Node(relation, row)))
    return out


def bc_roots(sources: Sources, limit: int,
             funnel: dict[str, dict[str, int]] | None = None) -> list[Root]:
    if sources.bc is None:
        return []
    out = []
    for entity_set in BC_INVOICE_SETS:
        out += _bc_set_roots("bc_invoice", entity_set, sources.bc.sample(entity_set, limit),
                             _funnel_row(funnel, "bc:" + entity_set))
    return out


def bc_document_roots(sources: Sources, limit: int,
                      read: Mapping[str, Sequence[Mapping[str, Any]]] | None = None
                      ) -> list[Root]:
    """The BC orders and posted receipts (``generator.BC_DOCUMENT_ROOT_SETS``, ``limit``
    read per set and company, newest first on the live port) with a number: roots only
    the documents may name (kind ``BC_DOCUMENT_STRATUM``), as the generator's document
    strata take them; a set in ``read`` (the records a record kind's BC-native roots
    already read, ``bc_native_roots``) is not read again. The purchasing mail names POs the Portal does not hold (live
    case.po_mail_links 2026-10-13: its 34 unmatched PO values sit above every Portal PO row
    read), so without them the mail kind had no root to name. A set whose read fails is
    skipped; its failure stays in the run's read failures."""
    out: list[Root] = []
    for entity_set in BC_DOCUMENT_ROOT_SETS if sources.bc is not None else ():
        try:
            records = (read or {}).get(entity_set)
            if records is None:
                records = sources.bc.sample(entity_set, limit)
        except ReadFailed:
            continue
        out += _bc_set_roots(BC_DOCUMENT_STRATUM, entity_set, records, _funnel_row(None, ""))
    return out


def bc_native_roots(sources: Sources, kind: str, portal: Sequence[Root], limit: int,
                    funnel: dict[str, dict[str, int]] | None = None,
                    skipped: dict[str, int] | None = None
                    ) -> tuple[list[Root], list[Root], dict[str, list[Mapping[str, Any]]]]:
    """The BC records of ``kind``'s BC-native sets (``BC_NATIVE_ROOT_SETS``, ``limit``
    read per set and company) as roots of ``kind``: (the roots, every record read as a
    root, for ``sampled_index``, the records read per set). Twin rule: a BC record the
    Portal drained under one of ``portal``'s numbers is that Portal root, one transaction
    (``generator.twins``: the record's declared identity edge walked from the Portal row
    finds exactly it, one company, no gap, and no second Portal root claims it); it is
    counted ``portal_twin`` and is not a root, so a document or a lookup naming the
    number names one root. A BC record with no Portal row stays a root. A set whose read
    fails is skipped (``bc_read_failed``); its failure stays in the run's read failures."""
    if sources.bc is None:
        return [], [], {}
    skipped = {} if skipped is None else skipped
    roots: list[Root] = []
    read: dict[str, list[Mapping[str, Any]]] = {}
    for entity_set in BC_NATIVE_ROOT_SETS.get(kind, ()):
        try:
            read[entity_set] = sources.bc.sample(entity_set, limit)
        except ReadFailed:
            skipped["bc_read_failed"] = skipped.get("bc_read_failed", 0) + 1
            continue
        roots += _bc_set_roots(kind, entity_set, read[entity_set],
                               _funnel_row(funnel, "bc:" + entity_set))
    if not roots:
        return [], [], read
    by_candidate = {_candidate(r): r for r in roots}
    twinned = {by_candidate[bc] for bc in twins(
        sources, [*by_candidate, *(_candidate(r) for r in portal)],
        {r.identifier for r in roots})}
    for root in twinned:
        counts = _funnel_row(funnel, root.relation)
        counts["roots"] -= 1
        counts["portal_twin"] += 1
    if twinned:
        skipped["portal_twin"] = skipped.get("portal_twin", 0) + len(twinned)
    return [r for r in roots if r not in twinned], roots, read


def _bc_set_roots(kind: str, entity_set: str, records: Sequence[Mapping[str, Any]],
                  counts: dict[str, int]) -> list[Root]:
    out = []
    for record in records:
        counts["rows"] += 1
        identifier = key_text(record["fields"].get(BC_OWN_FIELD))
        if identifier is None:
            counts["no_number"] += 1
            continue
        counts["roots"] += 1
        key = tuple(sorted({"company_id": str(record.get("company_id")),
                            "id": str(record.get("id"))}.items()))
        out.append(Root(kind, "bc:" + entity_set, key, BC_OWN_FIELD, identifier,
                        Node("bc:" + entity_set, record["fields"], record)))
    return out


def _sorted_roots(roots: Sequence[Root]) -> list[Root]:
    return sorted(roots, key=lambda r: (r.relation, canonical(r.key), r.identifier))


# ----------------------------------------------------------------- references

class _Budget:
    """One kind's BC probes: ``bc_finds`` granted, ``misses`` consecutive probes that
    yielded no reference naming the root so far (carried into the kind's second pass).
    ``take`` refuses once the grant is spent or the kind ``stopped`` on ``BC_MISS_STOP``
    misses; ``refused`` counts the refusal under its reason (``bc_miss_stop`` or
    ``bc_probe_budget``)."""

    def __init__(self, bc_finds: int, misses: int = 0) -> None:
        self.left = bc_finds
        self.used = 0
        self.misses = misses

    @property
    def stopped(self) -> bool:
        return self.misses >= BC_MISS_STOP

    def take(self) -> bool:
        if self.stopped or self.left <= 0:
            return False
        self.left -= 1
        self.used += 1
        return True

    def found(self, hit: bool) -> None:
        """A probe's outcome: a hit (the probe yielded a reference naming the root) resets
        the misses, anything else (no record, or records that name no root) is a miss."""
        self.misses = 0 if hit else self.misses + 1

    def refused(self, skipped: dict[str, int]) -> None:
        reason = "bc_miss_stop" if self.stopped else "bc_probe_budget"
        skipped[reason] = skipped.get(reason, 0) + 1


def _looked(looked: dict[str, list[int]] | None, other: str, other_field: str,
            found: bool, unnamed: bool = False) -> None:
    """Counts one lookup of a reference source (``other.other_field``): [looked up, found,
    records found that named no root (``unnamed``)].
    For a BC source found means it held a record that named the root (or whose naming
    lookup failed, which decided nothing): records of another database that name no root
    are no copy of it (gap-51ebb6c78e90)."""
    if looked is not None:
        counts = looked.setdefault(f"{other}.{other_field}", [0, 0, 0])
        counts[0] += 1
        counts[1] += found
        counts[2] += unnamed


def missing_reference(looked: Mapping[str, Sequence[int]]) -> list[str]:
    """The reference sources looked up that never held a record of the identifier."""
    return sorted(name for name, (tried, found, *_) in looked.items() if tried and not found)


def unnamed_reference(looked: Mapping[str, Sequence[int]]) -> list[str]:
    """Of ``missing_reference``, the sources whose lookups found records that named no root
    (published as ``no_naming_record``, not ``no_record``: the source held records, none a
    copy of the root)."""
    return sorted(name for name, (tried, found, *more) in looked.items()
                  if tried and not found and any(more))


def record_reference(sources: Sources, root: Root, edges: Sequence[tuple[str, str, str, str]],
                     rng: random.Random, budget: _Budget, skipped: dict[str, int],
                     looked: dict[str, list[int]] | None = None
                     ) -> tuple[str, str, str] | None:
    """(identifier, edge name, other system) from another system's record, else the
    root's carried reference (``carried_reference``), or None. ``looked`` counts each
    complete lookup of a reference source and whether it found a record (``_looked``). A BC
    probe is a hit of ``budget`` only when it yields a reference naming the root, else a
    miss (one whose naming lookup failed is neither)."""
    order = sorted(edges)
    rng.shuffle(order)
    for name, own, other, other_field in order:
        if (root.relation, own) in K.SHARED_REFERENCE_FIELDS:
            continue   # not a key of the root's record set: it may name another root
        value = key_text(field_value(root.node.row, own))
        if value is None:
            continue
        if _surrogate(sources, other, other_field) is None:
            skipped["reference_read_gap"] = skipped.get("reference_read_gap", 0) + 1
            continue
        if other.startswith("bc:"):
            if sources.bc is None:
                skipped["bc_not_configured"] = skipped.get("bc_not_configured", 0) + 1
                continue
            if not budget.take():
                budget.refused(skipped)
                continue
        ctx = Context(sources, {})
        found = _nodes(ctx, other, other_field, value)
        if other.startswith("bc:") and not root.node.is_bc:
            found = _vendor_checked(ctx, root.node, other, found)
        if ctx.gaps:   # a failed read measured nothing: no hit, no miss
            skipped["reference_read_gap"] = skipped.get("reference_read_gap", 0) + 1
            continue
        # A BC probe is measured by its yield, not by a record's existence
        # (gap-51ebb6c78e90): BC orders of another database carry customer POs the
        # Portal orders share, so a record found that names no root is a miss.
        bc = other.startswith("bc:")
        if len(found) > 1 and (other, other_field) in K.SHARED_REFERENCE_FIELDS:
            skipped["not_unique"] = skipped.get("not_unique", 0) + 1
            if bc:
                budget.found(False)
            _looked(looked, other, other_field, not bc, bc)
            continue
        undecided = False   # a naming lookup whose read failed measured nothing
        for node in sorted(found, key=lambda n: canonical(ctx.key(n))):
            identifier = key_text(field_value(node.row, other_field))
            if identifier is None:
                continue
            via = node if root.relation.startswith("bc:") else None
            named = _names(sources, root, own, identifier, via=via)
            if named:
                if bc:
                    budget.found(True)
                _looked(looked, other, other_field, True)
                return identifier, name, system_of(other)
            undecided = undecided or named is None
            _unnamed(named, skipped)
        if bc and not undecided:
            budget.found(False)
        _looked(looked, other, other_field, bool(found) and (not bc or undecided),
                bc and bool(found) and not undecided)
    return carried_reference(sources, root, order, budget, skipped)


def carried_reference(sources: Sources, root: Root,
                      edges: Sequence[tuple[str, str, str, str]], budget: _Budget,
                      skipped: dict[str, int]) -> tuple[str, str, str] | None:
    """A BC root's own copy of another system's identifier, when no record of that system
    gave one (its read failed, e.g. a Portal outage, or it holds no such record, e.g. BC
    drained from another Portal database): the root field of a declared cross-system edge
    (externalDocumentNumber = the Portal order number, vendorInvoiceNumber = the vendor's
    invoice number) holds that system's identifier as BC stores it. It counts only when it
    is not the root's own identifier field, the other side's field is no root key of
    another record kind (``keys.ROOT_KEYS``: externalDocumentNumber echoes a sales order
    number and orderNumber a PO number, so they name the order first and are skipped), and
    the oracle's lookup of it finds exactly the root (``names_exactly``: one record, one company, a complete read); each lookup is a BC
    probe of ``budget``. The edge name is suffixed ``.carried`` so the output says the value
    was read from the root's record."""
    if not root.relation.startswith("bc:"):
        return None
    for name, own, other, other_field in edges:
        if own == root.field:
            continue
        if (other, other_field) in _ROOT_KEY_FIELDS:
            skipped["names_another_root"] = skipped.get("names_another_root", 0) + 1
            continue
        identifier = key_text(field_value(root.node.row, own))
        if identifier is None:
            continue
        if not budget.take():
            budget.refused(skipped)
            return None
        named = _names(sources, root, own, identifier)
        if named:
            budget.found(True)
            skipped["carried_reference"] = skipped.get("carried_reference", 0) + 1
            return identifier, name + ".carried", system_of(other)
        # The yield rule, applied to every BC-rooted kind (bc_invoice, receipts and the
        # rest alike, not only sales orders): a probe whose lookup read completely and
        # named no root is a miss of the budget, whatever the lookup found
        # (gap-51ebb6c78e90). The next live verifier result must confirm that the
        # invoice/receipt-style kinds do not stop early on it (BC_MISS_STOP).
        if named is False:
            budget.found(False)
        _unnamed(named, skipped)
    return None


_ROOT_KEY_FIELDS = frozenset(pair for pairs in K.ROOT_KEYS.values() for pair in pairs)

# (BC relation, field) -> trimmed value -> the sampled records holding it
SampledIndex = dict[tuple[str, str], dict[str, list[Node]]]


def sampled_index(bc_sampled: Sequence[Root]) -> SampledIndex:
    """The BC records this run already sampled, by every field a declared edge between a
    BC entity set and another system reads on that set; values are trimmed as BC number
    matches are (``source.BcReads.find``, keys.NUMBER_TRIM_CITE)."""
    fields: dict[str, set[str]] = {}
    for edge in K.EDGES + K.REFERENCE_EDGES:
        for relation, column, other in ((edge.source, edge.source_field, edge.target),
                                        (edge.target, edge.target_field, edge.source)):
            if relation.startswith("bc:") and not other.startswith("bc:"):
                fields.setdefault(relation, set()).add(column)
    index: SampledIndex = {}
    for root in bc_sampled:
        for column in sorted(fields.get(root.relation, ())):
            text = key_text(field_value(root.node.row, column))
            if text is not None and sql_trim(text):
                index.setdefault((root.relation, column), {}).setdefault(
                    sql_trim(text), []).append(root.node)
    return index


def sampled_reference(sources: Sources, root: Root,
                      edges: Sequence[tuple[str, str, str, str]], sampled: SampledIndex,
                      skipped: dict[str, int], looked: dict[str, list[int]] | None = None
                      ) -> tuple[str, str, str] | None:
    """(identifier, edge name, ``bc``) from a BC record the run already sampled, before any
    BC probe: the record holds the root's field value on a declared edge, belongs to the
    PO's own vendor (purchase documents) and its identifier names exactly the root
    (``names_exactly``), the same checks ``record_reference`` applies to a probe's records.
    None when no sampled record does (a probe may still find an unsampled one)."""
    if root.node.is_bc:
        return None
    for name, own, other, other_field in sorted(edges):
        if (other, other_field) in K.SHARED_REFERENCE_FIELDS:
            continue   # the sampled records may not be all that hold it: probe (record_reference)
        held = sampled.get((other, other_field))
        value = key_text(field_value(root.node.row, own))
        if not held or value is None or sql_trim(value) not in held:
            continue
        ctx = Context(sources, {})
        found = _vendor_checked(ctx, root.node, other, list(held[sql_trim(value)]))
        if ctx.gaps:
            skipped["reference_read_gap"] = skipped.get("reference_read_gap", 0) + 1
            continue
        _looked(looked, other, other_field, bool(found))
        for node in sorted(found, key=lambda n: canonical(ctx.key(n))):
            identifier = key_text(field_value(node.row, other_field))
            if identifier is None:
                continue
            named = _names(sources, root, own, identifier)
            if named:
                return identifier, name, system_of(other)
            _unnamed(named, skipped)
    return None


def _candidate(root: Root) -> Candidate:
    """A root as the generator's document search takes it (``generator.Candidate``: a BC
    root by its chain relation, as ``generator.bc_root``)."""
    if root.node.is_bc:
        object_type, relation = K.BC_OBJECTS[root.relation[3:]]
        kind = "bc_number"
        order_field = BC_ORDER_EDGES.get(root.relation[3:])
        return Candidate(root.kind, "F1", root.identifier, kind, "", relation, root.key,
                         object_type,
                         order_number=key_text(root.node.row.get(order_field))
                         if order_field else None, dated=bc_dated(root.node.row))
    relation, object_type = root.relation, K.RELATIONS[root.relation].object_type
    return Candidate(root.kind, "F1", root.identifier, PORTAL_STRATA[root.kind][0], "",
                     relation, root.key, object_type)


def document_references(sources: Sources, documents: DocumentPort | None, kind: str,
                        roots: Sequence[Root], skipped: dict[str, int],
                        funnel: dict[str, int], read: dict[str, Any],
                        drawn: Container[str] | None = None, spent: Callable[[], bool] = _never,
                        run_id: str = "", now: str | None = None) -> list[tuple[Root, str]]:
    """(root, identifier) per root of a ``drawn`` kind (default all) the documents of
    ``kind`` name, found by ``generator.document_candidates`` over every root read (so a
    name two records share names neither), as many roots searched as the port affords
    (``max_roots``, else ``MAX_ROOT_SEARCHES``). The identifier is the span that named the
    root, as a document wrote it (``generator.names_in``), and counts only when the oracle's
    own lookup of it on the root's field finds exactly the root (``_names``; else
    ``not_unique`` or ``reference_read_gap``; a lead a document wrote apart from its label
    has no such span: ``no_reference``). Once ``spent()`` a BC root, whose lookup is a BC
    read, is not looked up (``kind_budget``). ``skipped`` also counts the spans that named
    two records (``not_unique``); ``funnel`` and ``read`` receive the search's funnel and
    read state."""
    by_key = {(c.root_relation, c.root_key): (c, r) for r in roots
              for c in (_candidate(r),)}
    spans: dict[Candidate, str] = {}

    def verify(candidate: Candidate) -> bool | None:
        root = by_key[candidate.root_relation, candidate.root_key][1]
        if candidate not in spans:
            skipped["no_reference"] = skipped.get("no_reference", 0) + 1
            return False
        if root.node.is_bc and spent():
            skipped["kind_budget"] = skipped.get("kind_budget", 0) + 1
            return None
        named = _names(sources, root, root.field, spans[candidate])
        if not named:
            _unnamed(named, skipped)
        return named

    found = document_candidates(
        sources, documents, kind, [c for c, _ in by_key.values()],
        documents.max_roots if hasattr(documents, "max_roots") else MAX_ROOT_SEARCHES,
        funnel, read, verify, run_id,
        lambda c: drawn is None or c.stratum in drawn, now, spans)
    if funnel.get("span_shared"):
        skipped["not_unique"] = skipped.get("not_unique", 0) + funnel["span_shared"]
    out = []
    for candidate in found:
        pool, root = by_key[candidate.root_relation, candidate.root_key]
        out.append((root, spans[pool]))
    return out


# ----------------------------------------------------------------- sample

def _hash16(relation: str, key: Any, identifier: str, reference: str,
            question: str | None = None) -> str:
    """Item hash; a process question (``question``) hashes apart from a find question
    over the same root and identifier."""
    body = {"relation": relation, "key": dict(key), "identifier": identifier,
            "reference": reference}
    if question is not None:
        body["question"] = question
    return digest(body)[:16]


def three_way_candidates(sources: Sources, po_roots: Sequence[Root],
                         bc_invoice_roots: Sequence[Root]) -> list[Root]:
    """Portal purchase orders with something to match: a Portal receipt, or a sampled BC
    purchase invoice whose order No. is the PO number. Lineage-filtered like every root."""
    invoiced = {key_text(r.node.row.get("orderNumber")) for r in bc_invoice_roots
                if r.relation == "bc:purchaseInvoices"} - {None}
    out = []
    for root in po_roots:
        if root.relation != THREE_WAY_ROOT:
            continue
        if sources.portal.lookup(K.RECEIPT, "purchase_order_id", root.node.row.get("id")) \
                or key_text(root.node.row.get("po_number")) in invoiced:
            out.append(root)
    return out


def _draw(sources: Sources, roots: Sequence[Root], count: int, rng: random.Random,
          budget: "_Budget", skipped: dict[str, int], spent: Callable[[], bool] = _never,
          sampled: SampledIndex | None = None,
          funnel: dict[str, dict[str, int]] | None = None,
          looked: dict[str, list[int]] | None = None
          ) -> tuple[list[tuple], list[str], list[Root]]:
    """Up to ``count`` (root, identifier, edge, system) whose identifier another system's
    record holds (``sampled_reference`` first, then ``record_reference``), the reference
    edges looked at, and the roots ``spent()`` left untried: counted (``kind_budget``), not
    tried. ``funnel`` receives the draw stages per root relation, ``looked`` the lookups of
    each reference source.

    The slice bounds the kind's source reads, never its checks over what is already read:
    once ``spent()``, a root is still tried on the Portal (FileMaker, Sage) rows the run
    read up front and the sampled BC records, and only a root that needs a BC read (a BC
    root, or a BC edge when nothing read gave a reference) is left untried, so a kind whose
    references need no read is sampled however long the kinds before it took."""
    pool: list[tuple] = []
    edges_by_relation: dict[str, list[tuple[str, str, str, str]]] = {}
    deferred: list[Root] = []
    for done, root in enumerate(roots):
        if len(pool) >= count:
            for left in roots[done:]:
                _funnel_row(funnel, left.relation)["untried"] += 1
            break
        counts = _funnel_row(funnel, root.relation)
        if root.relation not in edges_by_relation:
            edges_by_relation[root.relation] = reference_edges(sources, root.relation)
        edges = edges_by_relation[root.relation]
        if not edges:
            skipped["no_reference_edge"] = skipped.get("no_reference_edge", 0) + 1
            counts["no_reference_edge"] += 1
            continue
        reads = not spent()
        bc_edges = any(e[2].startswith("bc:") for e in edges)
        if not reads and root.node.is_bc:
            deferred.append(root)
            continue
        found = sampled_reference(sources, root, edges, sampled or {}, skipped, looked)
        if found is not None:
            counts["from_sampled_bc"] += 1
        else:
            found = record_reference(sources, root, edges if reads else [
                e for e in edges if not e[2].startswith("bc:")], rng, budget, skipped, looked)
        if found is None and not reads and bc_edges:
            deferred.append(root)
            continue
        if found is None:
            skipped["no_reference"] = skipped.get("no_reference", 0) + 1
            counts["no_reference"] += 1
            continue
        counts["references"] += 1
        pool.append((root, *found))
    for root in deferred:
        _funnel_row(funnel, root.relation)["untried"] += 1
    if len(pool) >= count or not deferred:
        return pool, _edge_names(edges_by_relation), []
    skipped["kind_budget"] = len(deferred)
    return pool, _edge_names(edges_by_relation), deferred


def _edge_names(edges_by_relation: Mapping[str, Sequence[tuple[str, ...]]]) -> list[str]:
    return sorted({e[0] for v in edges_by_relation.values() for e in v})


def _quotas(count: int, rng: random.Random) -> dict[str, int]:
    quota = {k: count // len(KINDS) for k in KINDS}
    extra = list(KINDS)
    rng.shuffle(extra)
    for kind in extra[:count - sum(quota.values())]:
        quota[kind] += 1
    return quota


def sample(sources: Sources, *, run_id: str, count: int = 28,
           documents: DocumentPort | None = None, bc_sample_limit: int = 50,
           bc_finds_total: int = 150,
           record_kinds: Sequence[str] | None = None,
           clock: KindClock | None = None, now: str | None = None) -> Sample:
    """Seeded cross-system sample of ``count`` items spread over ``KINDS``; a kind with
    too few references spills its quota to the kinds that have more.

    ``bc_finds_total``: BC reference probes for the whole run, shared across the sampled
    record kinds (each kind may spend an equal share of what is left, and what it leaves
    unspent goes to the later kinds; a kind stops after ``BC_MISS_STOP`` consecutive probes
    that found no BC record, ``skipped.bc_miss_stop``, so a kind BC holds no copy of leaves
    its share to the others). ``record_kinds``: the record kinds to draw roots
    from (default all); the others are not read, report ``status: "not_sampled"`` and
    leave their quota to the kinds that are sampled. Their roots are still read and indexed,
    so whether an identifier names exactly one record does not depend on ``record_kinds``.
    ``clock``: per-kind time slices (``KindClock``) in ``DRAW_ORDER`` over the kinds that
    draw, each at most its fair share, then a second slice for each kind its first one cut
    (a record kind on the roots it left untried, a document kind left without a reference
    reading again); None: no time limit here (the ports' own guard still applies).
    The documents are searched as the generator's document strata search them
    (``document_references``), the roots dated up to ``now``."""
    if count < 1:
        raise ValueError("count must be >= 1")
    rng = random.Random(int(sha256(run_id.encode()).hexdigest()[:16], 16))
    sources.portal.prefetch()
    report: dict[str, dict[str, Any]] = {}
    pools: dict[str, list[tuple[Root, str, str, str]]] = {}
    index: dict[str, list[Root]] = {}
    drawn_kinds = [k for k in RECORD_KINDS if record_kinds is None or k in record_kinds]
    three_way = record_kinds is None or THREE_WAY in record_kinds
    # the kinds that draw, in their slice order; the record kinds also share the BC probes
    budget_kinds = [k for k in DRAW_ORDER if k in drawn_kinds or (k == THREE_WAY and three_way)]
    sliced = budget_kinds + (list(DOCUMENT_KINDS) if documents is not None else [])
    spent = clock.spent if clock is not None else _never

    def begin(kind: str) -> None:
        if clock is not None and kind in sliced:
            clock.start(len(sliced) - sliced.index(kind), len(sliced))

    def cut(entry: dict[str, Any], pool: list) -> None:
        """A kind its slice stopped: said so, and blocked by the budget when it got
        nothing (never ``empty``), naming what the roots or documents it tried lacked."""
        if entry["skipped"].get("kind_budget"):
            entry["budget_cut"] = True
            if not pool:
                entry.update(status="unavailable", reason=BUDGET_STOP, blocked="budget")
                tried = entry["skipped"]
                if tried.get("no_reference"):
                    entry["within_read"] = "no_reference"
                elif tried.get("no_reference_edge"):
                    entry["within_read"] = "no_reference_edge"

    all_roots: dict[str, list[Root]] = {}
    # kind -> (the roots its slice left untried, its funnel): drawn again at the end
    untried: dict[str, tuple[list[Root], dict[str, dict[str, int]]]] = {}
    looked: dict[str, dict[str, list[int]]] = {}   # kind -> reference source -> [tried, found]
    sampled: SampledIndex = {}   # the BC records sampled this run (sampled_index)
    bc_left = bc_finds_total
    budgets: dict[str, _Budget] = {}   # kind -> its first pass's probes (misses carried on)
    reads: dict[str, dict[str, Any]] = {}  # read_state of the BC and document samples
    native_read: dict[str, list[Mapping[str, Any]]] = {}   # BC-native sets read, by set
    for kind in [k for k in DRAW_ORDER if k in RECORD_KINDS]:
        skipped: dict[str, int] = {}
        entry: dict[str, Any] = {"status": "read", "roots": 0, "skipped": skipped,
                                 "tried": [f"bc:{s}" for s in BC_INVOICE_SETS]
                                 if kind == "bc_invoice" else record_tried(kind) + (
                                     [f"bc:{s}" for s in BC_NATIVE_ROOT_SETS.get(kind, ())]
                                     if sources.bc is not None else [])}
        report[kind] = entry
        pools[kind] = []
        looked[kind] = {}
        funnel: dict[str, dict[str, int]] = {}
        if kind not in drawn_kinds:
            entry["status"] = "not_sampled"
        begin(kind)
        try:
            if kind == "bc_invoice":
                if sources.bc is None:
                    entry["status"] = "not_configured"
                roots = bc_roots(sources, bc_sample_limit, funnel)
            else:
                roots = portal_roots(sources, kind, funnel)
                native, every_native, read = bc_native_roots(
                    sources, kind, roots, bc_sample_limit, funnel, skipped)
                roots += native
                native_read.update(read)
                for key, values in sampled_index(every_native).items():
                    for value, nodes in values.items():
                        sampled.setdefault(key, {}).setdefault(value, []).extend(nodes)
        except ReadFailed as exc:
            entry.update(status="unavailable", reason=exc.reason, **exc.fields())
            continue
        if kind == "bc_invoice" and sources.bc is not None:
            reads[kind] = bc_read(sources)
        roots = _sorted_roots(roots)
        all_roots[kind] = list(roots)
        if kind == "bc_invoice":
            sampled = sampled_index(roots)
        entry["funnel"] = funnel
        for root in roots:   # every kind is indexed: ground truth never depends on the product
            index.setdefault(root.identifier, []).append(root)
        entry["roots"] = entry["candidates"] = len(roots)
        if kind not in drawn_kinds:
            continue
        rng.shuffle(roots)
        kinds_left = len(budget_kinds) - budget_kinds.index(kind)
        budget = budgets[kind] = _Budget(-(-bc_left // kinds_left))
        entry["bc_finds_allotted"] = budget.left
        pools[kind], entry["reference_edges"], left = _draw(
            sources, roots, count, rng, budget, skipped, spent, sampled, funnel, looked[kind])
        untried[kind] = left, funnel
        entry["bc_finds"] = budget.used
        bc_left -= budget.used
    # The three-way stratum: purchase-order roots with a receipt or a BC invoice to match.
    skipped = {}
    entry = {"status": "read" if three_way else "not_sampled", "roots": 0,
             "skipped": skipped, "tried": list(THREE_WAY_TRIED)}
    report[THREE_WAY] = entry
    pools[THREE_WAY] = []
    looked[THREE_WAY] = {}
    source_entry = report["purchase_order"]
    begin(THREE_WAY)
    if "purchase_order" not in all_roots:   # its roots' read failed
        entry.update(status="unavailable", reason=source_entry.get("reason"),
                     **{k: source_entry[k] for k in BLOCKED_FIELDS if k in source_entry})
    else:
        po_roots = [r for r in all_roots.get("purchase_order", [])
                    if r.relation == THREE_WAY_ROOT]
        entry["roots"] = len(po_roots)
        try:
            candidates = three_way_candidates(sources, po_roots,
                                              all_roots.get("bc_invoice", []))
        except ReadFailed as exc:
            entry.update(status="unavailable", reason=exc.reason, **exc.fields())
            candidates = []
        entry["candidates"] = len(candidates)
        # rows: the purchase-order roots read; roots: those with something to match
        funnel = {THREE_WAY_ROOT: dict(_funnel_row(None, THREE_WAY_ROOT), rows=len(po_roots),
                                       roots=len(candidates))}
        entry["funnel"] = funnel
        if three_way and entry["status"] == "read":
            rng.shuffle(candidates)
            budget = budgets[THREE_WAY] = _Budget(bc_left)
            entry["bc_finds_allotted"] = budget.left
            pools[THREE_WAY], entry["reference_edges"], left = _draw(
                sources, candidates, count, rng, budget, skipped, spent, sampled, funnel,
                looked[THREE_WAY])
            untried[THREE_WAY] = left, funnel
            entry["bc_finds"] = budget.used
            bc_left -= budget.used
    # A BC-native record root is a document root as BC_DOCUMENT_STRATUM (its set is read
    # once, ``native_read``), as the documents name every BC order.
    every_root = [r for roots in all_roots.values() for r in roots
                  if r.kind == "bc_invoice" or not r.node.is_bc]
    # The BC orders and receipts the documents may name: read in the first document
    # slice, searched when the product holds BC (as the bc_invoice kind).
    searchable = [*drawn_kinds, BC_DOCUMENT_STRATUM] if "bc_invoice" in drawn_kinds \
        else drawn_kinds
    bc_documents: list[Root] | None = None

    def read_documents(kind: str) -> None:
        """One search of ``kind``'s documents: its entry, read state and pool."""
        nonlocal bc_documents
        skipped: dict[str, int] = {}
        funnel: dict[str, int] = {}
        entry: dict[str, Any] = {"status": "read" if documents is not None
                                 else "not_configured",
                                 "roots": len(every_root), "skipped": skipped}
        read: dict[str, Any] = {}
        try:
            if bc_documents is None:
                bc_documents = [] if documents is None else \
                    bc_document_roots(sources, bc_sample_limit, native_read)
            entry["roots"] = len(every_root) + len(bc_documents)
            pairs = document_references(sources, documents, kind,
                                        every_root + bc_documents, skipped, funnel, read,
                                        searchable, spent, run_id, now)
        except Exception as exc:  # the document port's error types are not imported here
            entry.update(status="unavailable", reason=type(exc).__name__)
            if type(exc).__name__ == BUDGET_STOP:
                entry["blocked"] = "budget"
            pairs = []
        entry["candidates"] = funnel.get("documents", 0)
        before = report.get(kind, {}).get("search", {})
        for key, value in before.items():   # the port reads a hit's text once a run: a
            if key.startswith("texts_"):      # search again counts only hits new to it
                funnel[key] = funnel.get(key, 0) + value
        if funnel:
            entry["search"] = funnel
        if entry["status"] == "read" or kind not in report:   # a stopped search again
            report[kind] = entry                                 # leaves the first one
            pools[kind] = [(root, token, kind, kind) for root, token in pairs]
            reads.pop(kind, None)
            if read:
                reads[kind] = read

    for kind in DOCUMENT_KINDS:
        begin(kind)
        read_documents(kind)

    def documents_cut(kind: str) -> bool:
        """A document kind its slice left without a reference: its search was stopped by
        the budget, or a root it named was left unchecked."""
        entry = report[kind]
        return clock is not None and documents is not None and not pools[kind] and (
            entry.get("blocked") == "budget" or bool(entry["skipped"].get("kind_budget")))

    # The second pass: each kind its slice cut gets a slice of what is left, an even share
    # (and the record kinds of the BC probes left): its reads keep a share of their own,
    # whatever the other kinds' reads took.
    resumed = [k for k in budget_kinds if untried.get(k, ((), {}))[0]] + \
        [k for k in DOCUMENT_KINDS if documents_cut(k)]
    for n, kind in enumerate(resumed):
        if clock is not None:
            clock.start(len(resumed) - n)
        if kind in DOCUMENT_KINDS:
            if clock is None or not clock.spent():   # time left to search with
                read_documents(kind)
            continue
        left, funnel = untried[kind]
        entry = report[kind]
        for root in left:
            _funnel_row(funnel, root.relation)["untried"] -= 1
        entry["skipped"].pop("kind_budget")
        record_left = sum(1 for k in resumed[n:] if k not in DOCUMENT_KINDS)
        budget = _Budget(-(-bc_left // record_left), budgets[kind].misses)
        entry["bc_finds_allotted"] += budget.left
        more, edges, _ = _draw(sources, left, count - len(pools[kind]), rng, budget,
                               entry["skipped"], spent, sampled, funnel, looked[kind])
        pools[kind] += more
        entry["reference_edges"] = sorted({*entry["reference_edges"], *edges})
        entry["bc_finds"] += budget.used
        bc_left -= budget.used
    for kind in sliced:
        if report[kind]["status"] == "read":
            cut(report[kind], pools[kind])
    if clock is not None:
        clock.stop()
    for kind in KINDS:
        entry = report[kind]
        read = reads.get(kind)
        if read is not None:
            entry.update(read)
        refused_all = entry.pop("search_blocked", None)
        scope_all = entry.pop("scope_blocked", None)
        entry.setdefault("candidates", 0)
        if kind in DOCUMENT_KINDS and not entry.get("tried"):
            entry["tried"] = [f"documents:{kind}"]   # no read said what it tried
        entry["references"] = len(pools[kind])
        if not pools[kind] and missing_reference(looked.get(kind, {})):
            entry["missing_reference"] = missing_reference(looked[kind])
            if unnamed_reference(looked[kind]):
                entry["unnamed_reference"] = unnamed_reference(looked[kind])
        if entry["status"] == "read" and not pools[kind] and kind in DOCUMENT_KINDS:
            # a search without a reference, as the generator's document strata say it
            entry["status"], entry["reason"] = search_status(
                read or {}, entry.get("search", {}))
            block_refused_upstream(entry, refused_all)
            block_refused_scope(entry, scope_all)
        elif entry["status"] == "read" and not pools[kind]:
            # Never a silent zero: say which stage left the kind without a reference and
            # what it tried; a withheld, cut-off or unconfigured read is not empty.
            basis = ("no_roots" if not entry.get("roots") else
                     "no_candidates" if kind == THREE_WAY and not entry.get("candidates")
                     else "no_reference_edge" if entry["skipped"].get(
                         "no_reference_edge", 0) >= entry.get("roots", 0)
                     else "no_reference")
            unread = unread_status(read, entry.get("roots", 0))
            entry["status"], entry["reason"] = unread or ("empty", basis)
            if unread is not None and unread[0] != "not_configured":
                entry["within_read"] = basis
        elif entry["status"] == "not_configured" and not entry.get("reason"):
            entry["reason"] = ("bc_not_configured" if kind == "bc_invoice"
                               else "documents_not_configured")

    chosen = _allocate(pools, _quotas(count, rng), rng)
    items = []
    readings: dict[str, dict[str, Any]] = {}
    for kind in KINDS:
        question = kind if kind == THREE_WAY else None
        for root, identifier, reference, origin in chosen[kind]:
            item = Item(kind, root.kind, root.relation, root.key, identifier, reference, origin,
                        _hash16(root.relation, root.key, identifier, reference, question))
            items.append(item)
            if question is not None:   # the oracle's own answer, read only for drawn items
                readings[item.hash16] = three_way_reading(sources, root.relation, root.key)
        report[kind]["items"] = len(chosen[kind])
    if readings:
        states = [r["status"] for r in readings.values()]
        report[THREE_WAY]["readings"] = {s: states.count(s) for s in sorted(set(states))}
    failures = sources.portal.failures()
    causes = sources.portal.causes()
    labels = sources.portal.labels()
    if sources.bc is not None:
        failures.update(sources.bc.failures())
        causes.update(sources.bc.causes())
        labels.update(sources.bc.labels())
    return Sample(items, report, failures, readings, causes, labels)


def _allocate(pools: Mapping[str, list], quota: Mapping[str, int], rng: random.Random
              ) -> dict[str, list]:
    chosen = {k: list(pools[k][:quota[k]]) for k in KINDS}
    short = sum(quota[k] - len(chosen[k]) for k in KINDS)
    spill = list(KINDS)
    rng.shuffle(spill)
    while short > 0:
        progressed = False
        for kind in spill:
            if short > 0 and len(chosen[kind]) < len(pools[kind]):
                chosen[kind].append(pools[kind][len(chosen[kind])])
                short -= 1
                progressed = True
        if not progressed:
            break
    return chosen


def public_row(item: Item) -> dict[str, Any]:
    """What may leave the eval: kinds, edge name and a hash; never a value."""
    return {"kind": item.kind, "root_kind": item.root_kind, "reference": item.reference,
            "reference_system": item.reference_system, "hash16": item.hash16}


__all__ = ["BLOCKED_FIELDS", "DOCUMENT_KINDS", "DRAW_ORDER", "FORMAT", "Item", "KINDS", "KindClock",
           "FUNNEL", "RECORD_KINDS", "THREE_WAY",
           "THREE_WAY_ROOT", "THREE_WAY_TRIED", "Root", "Sample", "missing_reference",
           "unnamed_reference",
           "BC_NATIVE_ROOT_SETS", "bc_document_roots", "bc_native_roots", "names_exactly",
           "public_row",
           "reference_edges", "sample", "sampled_index", "sampled_reference", "system_of",
           "three_way_candidates"]
