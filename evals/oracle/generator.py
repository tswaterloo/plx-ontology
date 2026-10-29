"""Dev-set item generator: sample closed transactions by seed, answer them with the oracle.

Sampling is stratified by where the root identifier comes from (directive 4190c1c690ce
item 5): ``sales_order``, ``purchase_order``, ``assembly_order``, ``receipt`` (Portal
records; receipts also FM raw receipts by lab id), ``bc_invoice`` (posted BC sales/purchase
invoices) and ``mail`` / ``sharepoint``, root first: the documents are searched (GET
only, ``DocumentPort.search``) for the numbers of the closed records of the split, round
robin over the record strata, newest first, and capped per source, and a hit counts only
when one of its separator-bounded spans names the record exactly or by a lead from another
shape of its number (``keys.DOCUMENT_VARIANTS``: prefix or zero padding; a label written
apart from the number, ``labelled_spans``) the oracle's exact root lookup confirms; the question is then asked from the record's own number. The random
generator is seeded by the run id, candidates are sorted before shuffling, so one run id
and one source state give the same items. Candidates are ranked by a digest of the run id
and the candidate (not shuffled), so a fixed run id keeps its items while the source grows.

Splits (10-DECISION 8): every root record belongs to one split by a salted hash of its
relation and key (``split_of``), so dev and held-out never share a root, whatever run
ids are used. The dev split asks F1-F4 (30 by default); the held-out split (120) adds F5
and F6, which have no plan on the Workbench side. Held-out items are written only where a
credentialed run is told to write them (``scripts/eval_oracle_dev.py heldout``), never
into Git.

Every stratum reports its status, a reason whenever it yields no candidate (``empty``,
``unavailable``, ``not_configured``; never ``read`` with zero), candidates and items per
family and a funnel of where rows dropped out (``unexplained_strata`` must stay empty).

Every candidate must be closed, in every split (decision
``devloop/DECISIONS/2026-10-07-oracle-strata-closed-roots-only.md``), must resolve back through the oracle's own root lookup
to exactly the sampled record (so the question has one root), and must not be a
Portal-native staging row (09-CRITIQUE R1: ``gold_native_staging`` stays out until a
person confirms it). Items follow ``evals/item.schema.json`` (plx-eval-item/v1); the
caller validates them. ``audit`` marks the requested number of labels for human audit
(09 Q9) without claiming an audit happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from itertools import zip_longest
from pathlib import Path
import random
import re
from typing import Any, Callable, Collection, Container, Iterable, Mapping, Sequence

from . import keys as K
from .families import (BC_VENDOR_SETS, INTERFACE, ORACLES, Context, Node, Sources,
                       resolve_root, run)
from .source import (BUDGET_STOP, DocumentPort, ReadFailed, canonical, decimal_of, digest,
                     field_value, instant, key_text, sql_trim)

ITEM_FORMAT = "plx-eval-item/v1"
STRATA = ("sales_order", "purchase_order", "assembly_order", "receipt", "bc_invoice", "mail",
          "sharepoint")
# Stratum -> (identifier kind, ((relation, identifier column), ...)) for record strata: the
# served relations whose rows are that kind's roots. Receipts are rooted in gold
# app_receipt and in FM raw receipts (lab ids): the MRP path writes app_receipt from the FM
# lab id (03 #3.5), and the FM rows hold every receipt the gold table has not received.
PORTAL_STRATA: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "sales_order": ("so_number", ((K.SO, "order_number"),)),
    "purchase_order": ("po_number", ((K.PO, "po_number"),)),
    "assembly_order": ("ao_number", ((K.MO, "mo_code"),)),
    "receipt": ("receipt_number", ((K.RECEIPT, "receipt_number"),
                                   (K.FM_RECEIPT_RAW, "idlabid"))),
}
BC_INVOICE_SETS = ("salesInvoices", "purchaseInvoices")
BC_CLOSED_STATUS = "Paid"
# The BC document sets a ``bc_number`` resolves through (keys.ROOT_KEYS, in precedence).
BC_ROOT_SETS = tuple(target[3:] for target, _ in K.ROOT_KEYS["bc_number"])
# BC deletes an order once it is fully invoiced; one still served is closed once fully
# received (purchase) or fully shipped (sales). A posted receipt is closed.
BC_ORDER_DONE = {"purchaseOrders": "fullyReceived", "salesOrders": "fullyShipped"}
# The other BC document sets a ``bc_number`` resolves through: their closed records are
# roots the documents are searched for (``BC_DOCUMENT_STRATUM``), never a ``bc_invoice``
# candidate. Live eval.oracle_dev 2b0ee89: 6088 closed Portal and BC invoice roots, 24
# searched per source, 0 named by the purchasing mail, whose PO values all sit above every
# Portal PO row read (case.po_mail_links); a closed PO the Portal has not read yet is a
# BC order or posted receipt (the Portal's BC drain numbers the BC order by the Portal PO
# number, align-portal-po-number.ts).
BC_DOCUMENT_ROOT_SETS = tuple(s for s in BC_ROOT_SETS if s not in BC_INVOICE_SETS)
BC_DOCUMENT_STRATUM = "bc_document"
# A BC document set -> the field of its record that holds a Portal PO number on a declared
# edge to that PO (keys.EDGES bc_purchase_receipt.po, bc_purchase_invoice.po).
BC_ORDER_EDGES = {e.source[3:]: e.source_field for e in K.EDGES
                  if e.source.startswith("bc:") and e.target == K.PO}
BC_DATE_FIELDS = ("postingDate", "documentDate", "orderDate")
# A number span (funnel ``number_spans``) and a searched number carry at least this many
# digits.
MIN_SHAPE_DIGITS = 3
DOCUMENT_STRATA = ("mail", "sharepoint")
# Root-first document strata (live 2026-10-13: across three full runs, 305/304/400 mails
# and 822/842/486 number spans named 4/4/3 Portal rows, all open; SharePoint none): per
# document source, the closed roots searched (round robin over the record strata; as many
# searches again at most for a number without its leading letters, ``fallback_query``) and
# the documents one search reads.
MAX_ROOT_SEARCHES = 24
MAX_SEARCH_HITS = 5
# A kind whose searches named no root: the documents one bounded listing read
# (``DocumentPort.list_documents``) are named against every searchable root. Live 2026-10-18 (daily bb20ab0f, gap-fdd76b96f8f6): 51
# SharePoint root searches found 3 documents (47 found none) naming no root.
MAX_LISTED_DOCUMENTS = 600
# Each stratum's dated roots, newest first, fall into windows of RECENT_WINDOW; a run
# searches a window's roots in a sample seeded by its run id (``search_roots``), so hourly
# runs draw different recent roots (directive a108c9b752a2). One source's searches take about
# MAX_ROOT_SEARCHES / 5 roots of a stratum, a fifth of its first window.
RECENT_WINDOW = MAX_ROOT_SEARCHES
_PREFIX = dict(K.DOCUMENT_VARIANTS)["prefix"]
# A root is dated by the earliest of its transaction dates and its relation's change time
# (``root_when``).
TRANSACTION_DATES = {K.SO: ("required_date",), K.PO: ("closed_at",), K.MO: ("date_realized",)}
# Every lineage stratum a sampled root gets, by prefix: ``lineage`` (``gold_native_staging``
# is never sampled) and a BC root's company slug (``_slug``). The strata name real data:
# they say where a root came from, never that an item was generated (its oracle id says
# that: ``ORACLE_ID_PREFIXES``). evals.admission reads both.
GOLD_LINEAGE_PREFIX = "gold_"
BC_LINEAGE_PREFIX = "bc_"
LINEAGE_PREFIXES = (GOLD_LINEAGE_PREFIX + "etl_", "fm_bronze", "sage_bronze", "silver_view",
                    BC_LINEAGE_PREFIX)
# Family -> (stratum, identifier source) pairs that can root it.
FAMILY_ROOTS = {
    "F1": tuple(STRATA),
    "F2": ("sales_order",),
    "F3": ("sales_order",),
    "F4": ("purchase_order",),
    "F5": ("purchase_order",),
    "F6": ("sales_order",),
}
# Families rooted at one record of one identifier kind (F1 takes any kind, F2 customer POs).
FAMILY_KIND = {"F3": "so_number", "F4": "po_number", "F5": "po_number", "F6": "so_number"}
# Per-split quotas (10-DECISION 8): the dev set asks F1-F4; the held-out set adds two
# families that have no plan on the Workbench side (F5, F6) and is never committed.
QUOTAS = {
    "dev": {"F1": 12, "F2": 6, "F3": 6, "F4": 6},
    "held_out": {"F1": 24, "F2": 16, "F3": 16, "F4": 16, "F5": 24, "F6": 24},
}
QUOTA = QUOTAS["dev"]
HELD_OUT_ONLY = frozenset(set(QUOTAS["held_out"]) - set(QUOTAS["dev"]))
HELD_OUT_COUNT = sum(QUOTAS["held_out"].values())
# Root partition: every root record belongs to exactly one split, by a salted hash of its
# relation and key, so no dev run (any run id, any hour) can sample a held-out root and
# the reverse. Changing the salt reshuffles both sets; it is part of the item format.
SPLIT_SALT = "plx-eval-split/v1"
QUESTION = {
    "F1": "Which records across Portal, FileMaker, Sage and Business Central are linked to "
          "{label} {identifier}?",
    "F2": "What is the status of customer PO {identifier}: which sales orders carry it and "
          "what has shipped?",
    "F3": "For {label} {identifier}, how many units were ordered, produced and shipped?",
    "F4": "Do the purchase order lines, receipts and supplier invoices for {label} "
          "{identifier} agree on quantities?",
    "F5": "Which supplier invoices booked against {label} {identifier} are not matched to "
          "any of its receipts?",
    "F6": "Did {label} {identifier} ship short of what was ordered, and on which items?",
}
AUDIT_NOTE = ("audit_requested (09 Q9): a person checks known_outcome against the sources, "
              "then sets status human_audited (audited_by, audited_at) or disputed.")
# Document texts: an identifier counts only as a whole run of word characters and the
# separators identifiers carry ("PO#1234", "AO-26005B", "25001-1"), cut at separators, so
# "PO#1234_packing-slip.pdf" names "PO#1234" but "FXPO2000099" never names "FXPO200009".
_RUN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9#/._-]*[A-Za-z0-9])?")
_SEPARATOR = re.compile(r"[#/._-]+")
MAX_SPAN_PARTS = 8
_DIGIT = re.compile(r"[0-9]")
# A number written apart from its label (synthetic "PO 1234", "P.O. # 1234": the live shapes of
# the purchasing mail, case.po_mail_links 2026-10-13 grammar_form_lead 12 of 76 PO values,
# as many as its exact claims): a run without digits and the next run, starting with a
# digit, apart from it by at most MAX_LABEL_GAP characters of _LABEL_GAP on one line, joined
# (``labelled_spans``), are a span of the lead stage only, never an exact name.
_LABEL_GAP = re.compile(r"[#/._:-]*[ \t]+(?:[#:][ \t]*)?")
MAX_LABEL_GAP = 8


@dataclass(frozen=True)
class Candidate:
    stratum: str
    family: str
    identifier: str
    kind: str
    lineage: str
    root_relation: str
    root_key: tuple[tuple[str, Any], ...]
    object_type: str
    # Every record the question is rooted at (F2: each sales order carrying the customer
    # PO); the split partition must place all of them in the same split.
    members: tuple[tuple[str, str], ...] = ()
    # A BC root's own record facts the documents are named and dated by (``bc_root``): the
    # PO number a posted receipt or invoice carries on its declared edge (``BC_ORDER_EDGES``;
    # BC deletes a PO once fully invoiced, so the number survives only here) and its
    # posting/document date (ISO text).
    order_number: str | None = None
    dated: str | None = None

    def root_records(self) -> tuple[tuple[str, str], ...]:
        return self.members or ((self.root_relation, canonical(dict(self.root_key))),)


@dataclass
class Generated:
    items: list[dict[str, Any]]
    strata: dict[str, dict[str, Any]] = field(default_factory=dict)
    item_strata: dict[str, str] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    split: str = "dev"
    dropped: dict[str, int] = field(default_factory=dict)


def oracle_code_digest() -> str:
    """Digest of the oracle's own code: an item names the oracle version that answered."""
    here = Path(__file__).resolve().parent
    parts = [(p.name, p.read_bytes().hex()) for p in sorted(here.glob("*.py"))
             if not p.name.startswith("fixture")]
    return sha256(canonical(parts).encode()).hexdigest()


def root_digest(relation: str, key: Any) -> str:
    """Digest naming one root record (relation + key; no value of the record)."""
    if isinstance(key, Mapping):
        key = canonical(dict(key))
    return digest([SPLIT_SALT, relation, key])


def split_of(relation: str, key: Any) -> str:
    """The split a root record belongs to: about half dev, half held-out."""
    return "held_out" if int(root_digest(relation, key)[:8], 16) % 2 else "dev"


def candidate_split(candidate: Candidate) -> str | None:
    """The split of a candidate, or None when its root records straddle both."""
    splits = {split_of(relation, key) for relation, key in candidate.root_records()}
    return splits.pop() if len(splits) == 1 else None


def item_root_digest(item: Mapping[str, Any]) -> str | None:
    """Root-record digest of a generated item (its pinned relation + key)."""
    root = item.get("root") or {}
    if not isinstance(root.get("relation"), str) or not isinstance(root.get("key"), Mapping):
        return None
    return root_digest(root["relation"], root["key"])


# ----------------------------------------------------------------- row rules

def _custom(row: Mapping[str, Any], name: str) -> Any:
    return field_value(row, "custom_fields." + name)


def lineage(relation: str, row: Mapping[str, Any], portal: Any) -> str:
    """Where a row came from (09 R1 strata; rules cited in keys.LINEAGE_CITE and
    keys.PO_LINEAGE_CITE). Rows of the replica's own tables are bronze (FM ``public.*``,
    ``sage.*``) or ``silver_view``."""
    schema, _, name = relation.partition(".")
    if schema == "sage":
        return "sage_bronze"
    if schema == "silver":
        return "silver_view"
    if relation in (K.FM_PO, K.FM_PO_ITEM, K.FM_RECEIPT_RAW):
        return fm_po_lineage(relation, row, portal)
    if schema == "public" and not name.startswith("app_"):
        return "fm_bronze"
    if relation == K.SO:
        if (_custom(row, "sageOrdUniq") or _custom(row, "sageSourceOrdUniq") or
                _custom(row, "source") == "sage.oe_order_header"):
            return "gold_etl_from_sage"
        fm_id = key_text(_custom(row, "fmId"))
        if fm_id is None:
            # Legacy FM migration rows: id ``fm-so-{oe.id}`` (keys.LEGACY_FM_SO_ID); their
            # custom_fields are often empty, so the id is the only lineage they carry.
            row_id = key_text(row.get("id")) or ""
            if row_id.startswith(K.LEGACY_FM_SO_ID):
                fm_id = row_id[len(K.LEGACY_FM_SO_ID):]
        if fm_id is not None and fm_id.isdigit():
            return "gold_native_staging" if int(fm_id) >= K.PORTAL_OE_ID_BASE \
                else "gold_etl_from_fm"
        return "gold_native_staging"
    if relation == K.PO:
        # keys.PO_LINEAGE_CITE: only an FM loader's source stamp places a row in FM.
        if _custom(row, "qaSmoke") is True:
            return "gold_native_staging"
        return "gold_etl_from_fm" if _custom(row, "source") in K.PO_FM_SOURCES \
            else "gold_native_staging"
    if relation == K.MO:
        code = row.get("mo_code")
        return "gold_etl_from_fm" if isinstance(code, str) and code.startswith("AO-") \
            else "gold_native_staging"
    if relation == K.RECEIPT:
        orders = portal.lookup(K.PO, "id", row.get("purchase_order_id"))
        return lineage(K.PO, orders[0], portal) if len(orders) == 1 else "gold_native_staging"
    return "gold_native_staging"


def fm_po_lineage(relation: str, row: Mapping[str, Any], portal: Any) -> str:
    """An FM purchase-order row (its items and raw receipts by their PO) is staging when
    the Portal wrote it: its gold twin (keys edge ``fm_po.po``, fmGeneratedPoId = id, the
    product's ``PurchaseOrder.same_record_as`` via_link) is a QA smoke row or a Generate PO
    row (keys.PO_LINEAGE_CITE). Otherwise it is FM bronze."""
    if relation == K.FM_RECEIPT_RAW:
        items = portal.lookup(K.FM_PO_ITEM, "id", row.get("idpogenitem"))
        return fm_po_lineage(K.FM_PO_ITEM, items[0], portal) if len(items) == 1 \
            else "fm_bronze"
    if relation == K.FM_PO_ITEM:
        orders = portal.lookup(K.FM_PO, "id", row.get("idpogen"))
        return fm_po_lineage(K.FM_PO, orders[0], portal) if len(orders) == 1 \
            else "fm_bronze"
    twins = portal.lookup(K.PO, "custom_fields.fmGeneratedPoId", row.get("id"))
    if any(_custom(t, "qaSmoke") is True or _custom(t, "source") in K.PO_PORTAL_WRITERS
           for t in twins):
        return "gold_native_staging"
    return "fm_bronze"


def closed(relation: str, row: Mapping[str, Any], portal: Any) -> bool:
    """A transaction whose answer should not move (09 R7 d), read the way the Portal reads
    its status: sales orders by ``keys.so_finished`` (gold vocabulary or FM status text) or,
    Sage-backed, by their Sage header's complete flag (``keys.SAGE_COMPLETE``); purchase
    orders by their fulfillment code over their lines (``keys.PO_STATUS_CITE``); assembly
    orders realized; receipts released/rejected."""
    if relation == K.SO:
        if K.so_finished(row.get("status")):
            return True
        uniq = _custom(row, "sageOrdUniq") or _custom(row, "sageSourceOrdUniq")
        if key_text(uniq) is None:
            return False
        headers = portal.lookup(K.SAGE_ORDER, "orduniq", uniq)
        return len(headers) == 1 and sql_trim(key_text(headers[0].get("complete")) or "") \
            in K.SAGE_COMPLETE
    if relation == K.PO:
        return bool(key_text(row.get("closed_at"))) or po_code(row, portal) == K.PO_CLOSED_CODE
    if relation == K.MO:
        return (row.get("deleted_at") in (None, "") and
                (K.assembly_status(row.get("status")) == "Realized" or
                 bool(key_text(row.get("date_realized")))))
    if relation == K.RECEIPT:
        return K.receipt_status(row.get("status")) in K.RECEIPT_CLOSED
    if relation == K.FM_RECEIPT_RAW:
        return K.receipt_status(row.get("status_c")) in K.RECEIPT_CLOSED
    return False


def po_code(row: Mapping[str, Any], portal: Any) -> str:
    """Portal's fulfillment code of one purchase order: header plus SUM(line quantity) and
    SUM(COALESCE(quantity_received, 0)) over its lines (SQL SUM skips an empty quantity)."""
    lines = portal.lookup(K.PO_LINE, "purchase_order_id", row.get("id"))
    ordered = sum((q for q in (decimal_of(n.get("quantity")) for n in lines) if q is not None),
                  Decimal(0))
    received = sum((q for q in (decimal_of(n.get("quantity_received")) for n in lines)
                    if q is not None), Decimal(0))
    return K.po_fulfillment_code(row.get("status"), ordered, received)


# The FM status labels the Portal's PO writers copy onto the gold header verbatim
# (mirror-fm-pos-to-gold.mjs:329 default "Pending", etl-silver-to-gold.ts PO upsert) and its
# PO list shows (P@d07f912:src/lib/mrp/po-fulfillment-status.ts PoFulfillmentLabel,
# src/app/mrp/purchase-orders/po-queries.ts PORTAL_PO_STATUS): words the Portal writes.
# Live 2b0ee89 counted 5504 of 5866 PO rows out of vocabulary without them; a PO's closure
# never reads them (its lines decide, ``po_code``), so they hid no root.
PO_FM_LABELS = frozenset({"Generated", "Pending", "B.Order", "Complete", "Cancel"})
VOCABULARY = {K.SO: K.SO_VOCABULARY, K.PO: K.PO_VOCABULARY | PO_FM_LABELS,
              K.RECEIPT: K.RECEIPT_VOCABULARY}


def out_of_vocabulary(relation: str, rows: Sequence[Mapping[str, Any]]) -> int | None:
    """Rows of a sampled relation whose status is no word the Portal writes
    (``keys.VOCABULARY_CITE``; an AO status is canonicalised, so every word has a class).
    Such a row is never closed (an FM status text a sales order reads as finished, by
    ``keys.so_finished``, is closed and not counted); the count shows how much the sample
    may miss."""
    words = VOCABULARY.get(relation)
    if words is None:
        return None
    status = K.so_status if relation == K.SO else (lambda v: v)
    return sum(1 for row in rows if status(row.get("status")) not in words and
               not (relation == K.SO and K.so_finished(row.get("status"))))


def identifier_spans(text: Any) -> set[str]:
    """Every separator-bounded span of every run in ``text`` (exact text, no case folding):
    the strings a document can name a record by. A span starts and ends at an
    alphanumeric character and never crosses whitespace."""
    out: set[str] = set()
    if not isinstance(text, str):
        return out
    for match in _RUN.finditer(text):
        run = match.group(0)
        cuts = [(m.start(), m.end()) for m in _SEPARATOR.finditer(run)]
        starts = [0] + [end for _, end in cuts]
        ends = [start for start, _ in cuts] + [len(run)]
        for i, start in enumerate(starts):
            for end in ends[i:i + MAX_SPAN_PARTS]:
                span = run[start:end]
                if span and span[0].isalnum() and span[-1].isalnum():
                    out.add(span)
    return out


def labelled_spans(text: Any) -> set[str]:
    """A label and the number written apart from it, joined (``_LABEL_GAP``: "PO 1234" ->
    "PO1234", "P.O. # 1234" -> "P.O1234"), the number cut at its separators as
    ``identifier_spans`` cuts it: spans ``names_in`` takes leads from, never exact names."""
    out: set[str] = set()
    if not isinstance(text, str):
        return out
    label = None
    for match in _RUN.finditer(text):
        run = match.group(0)
        if (label is not None and run[0].isdigit()
                and match.start() - label.end() <= MAX_LABEL_GAP
                and _LABEL_GAP.fullmatch(text, label.end(), match.start())):
            ends = [m.start() for m in _SEPARATOR.finditer(run)] + [len(run)]
            out.update(label.group(0) + run[:end] for end in ends[:MAX_SPAN_PARTS])
        label = None if _DIGIT.search(run) else match
    return out


def document_key(relation: str, text: str) -> str:
    """How a document span is compared with a record's number: a purchase-order number as
    the Portal looks one up, TRIM ILIKE (keys.PO_DOCUMENT_CITE), every other number as
    exact text (03 §3.4 resolveOrderByNumber is exact and case-sensitive)."""
    return text.strip().casefold() if relation == K.PO else text


def document_names(sources: Sources, candidate: Candidate) -> list[tuple[int, str]]:
    """(precedence tier, number) a document may name ``candidate`` by: a purchase order by
    each of its numbers the Portal looks one up by (keys.PO_DOCUMENT_NAMES), every other
    record by its own number."""
    if candidate.root_relation != K.PO:
        # the order number of a closed posted document is a lead tier: a PO the Portal or BC
        # still holds names itself first, and two posted documents sharing it name neither
        extra = [(1, candidate.order_number)] if candidate.order_number not in (
            None, candidate.identifier) else []
        return [(0, candidate.identifier), *extra]
    row = root_row(sources, candidate)
    if row is None:
        return [(0, candidate.identifier)]
    return po_names(row)


def ordered_name(candidate: Candidate, text: str) -> bool:
    """Whether ``text`` is the order number a BC posted document carries
    (``Candidate.order_number``), not its own number."""
    return candidate.root_relation != K.PO and text == candidate.order_number != candidate.identifier


def superseded_orders(pool: Sequence[Candidate]) -> set[Candidate]:
    """The posted documents whose order number another posted document of the same order
    (same number, same company: a receipt and an invoice of one PO) names instead: that order
    is named once, by its newest receipt (a receipt before an invoice, then the later date).
    Documents of different companies keep the number and so share it, naming neither."""
    groups: dict[tuple[str, str], list[Candidate]] = {}
    for c in pool:
        if c.family == "F1" and c.root_relation != K.PO and c.order_number:
            company = dict(c.root_key).get("company_id", "")
            groups.setdefault((company, c.order_number.strip().casefold()), []).append(c)
    out: set[Candidate] = set()
    for members in groups.values():
        keep = max(members, key=lambda c: (c.root_relation == K.BC_OBJECTS["purchaseReceipts"][1],
                                           c.dated or "", c.identifier))
        out.update(c for c in members if c is not keep)
    return out


def po_names(row: Mapping[str, Any]) -> list[tuple[int, str]]:
    """(tier, number) of each name a purchase-order row is looked up by
    (keys.PO_DOCUMENT_NAMES)."""
    names = [(tier, key_text(field_value(row, path))) for path, tier in K.PO_DOCUMENT_NAMES]
    return [(tier, text) for tier, text in names if text is not None]


def root_date(values: Iterable[Any]) -> datetime | None:
    """The earliest instant among a root's date values (UTC; unparseable values skipped)."""
    return min((m for m in map(instant, values) if m is not None), default=None)


def date_columns(relation: str) -> tuple[str, ...]:
    changed = K.RELATIONS[relation].changed_at
    return TRANSACTION_DATES.get(relation, ()) + ((changed,) if changed else ())


def document_index(sources: Sources, pool: Sequence[Candidate]) -> dict[str, Candidate]:
    """``document_key`` -> the one F1 candidate a document span names: the candidates in
    the best tier that has the name; a name two candidates share there names neither."""
    tiers: dict[str, dict[int, dict[str, Candidate]]] = {}
    ordered: dict[str, dict[int, dict[str, Candidate]]] = {}   # order numbers (``ordered_name``)
    dropped = superseded_orders(pool)
    for c in pool:
        if c.family != "F1":
            continue
        for tier, text in document_names(sources, c):
            if c in dropped and ordered_name(c, text):
                continue
            into = ordered if ordered_name(c, text) else tiers
            by_root = into.setdefault(document_key(c.root_relation, text), {})
            by_root.setdefault(tier, {})[canonical([c.root_relation, c.root_key])] = c
    # a posted document's order number is a lead after every record's own name: where a
    # record (a PO compares case-folded) has that name, it names the record alone
    own = {key.casefold() for key in tiers}
    tiers.update({key: by_tier for key, by_tier in ordered.items() if key.casefold() not in own})
    index = {}
    for key, by_tier in tiers.items():
        best = by_tier[min(by_tier)]
        if len(best) == 1:
            index[key] = next(iter(best.values()))
    return index


# A Portal record's declared identity edges to a BC document's own No. (keys.EDGES whose
# target field is a ``bc_number`` root field): the number the Portal's BC drain writes on
# the BC copy of that record (so.bc_sales_order, po.bc_purchase_order).
_TWIN_EDGES: dict[str, tuple[K.Edge, ...]] = {}
for _edge in K.EDGES:
    if _edge.source in K.RELATIONS and (_edge.target, _edge.target_field) in \
            K.ROOT_KEYS["bc_number"]:
        _TWIN_EDGES[_edge.source] = _TWIN_EDGES.get(_edge.source, ()) + (_edge,)


def twins(sources: Sources, pool: Sequence[Candidate],
          numbers: Container[str]) -> dict[Candidate, Candidate]:
    """BC document -> the one Portal candidate of ``pool`` it is the twin of, for the BC
    documents of ``pool`` whose No. is in ``numbers`` (the spans the documents found wrote,
    so only a number a document names costs a BC read): one transaction the Portal
    drained to BC under its own number is one root for the documents, the Portal record
    (decision of directive 8574b3a82d8f item 1: a span naming a Portal PO and its
    same-numbered BC order named neither and lost the PO). A BC document is a twin when
    its No. is the Portal record's field on a declared identity edge (``_TWIN_EDGES``) and
    the oracle's own walk of that edge from the record (``families.walk``'s rule: the PO's
    own vendor for BC purchase documents, one company) finds exactly that document with no
    gap. A BC document of another vendor or company, one two Portal candidates claim, or
    one whose walk read failed is a record of its own, so a span naming both names
    neither."""
    by_number: dict[str, list[Candidate]] = {}
    for c in pool:
        if c.family == "F1" and c.root_relation not in K.RELATIONS and c.identifier in numbers:
            by_number.setdefault(c.identifier, []).append(c)
    if not by_number:
        return {}
    claims: dict[Candidate, set[Candidate]] = {}
    for c in pool:
        edges = _TWIN_EDGES.get(c.root_relation, ()) if c.family == "F1" else ()
        row = root_row(sources, c) if edges else None
        if row is None:
            continue
        for edge in edges:
            value = key_text(field_value(row, edge.source_field))
            chain = K.BC_OBJECTS[edge.target[3:]][1]
            named = [b for b in by_number.get(value or "", ()) if b.root_relation == chain]
            if not named:
                continue
            ctx = Context(sources, {})
            if edge.target in BC_VENDOR_SETS:
                found = ctx.vendor_filter(Node(c.root_relation, row), edge.target[3:],
                                          ctx.bc_find(edge.target[3:], edge.target_field,
                                                      value, check_span=False))
                ctx.check_span(edge.target[3:], found)
            else:
                found = ctx.lookup(edge.target, edge.target_field, value)
            if ctx.gaps or len(found) != 1:
                continue
            key = tuple(sorted(ctx.key(found[0]).items()))
            for twin in named:
                if twin.root_key == key:
                    claims.setdefault(twin, set()).add(c)
    return {twin: next(iter(owners)) for twin, owners in claims.items() if len(owners) == 1}


def variant_index(sources: Sources, pool: Sequence[Candidate]
                  ) -> tuple[dict[str, tuple[Candidate, frozenset[str], frozenset[str]] | None],
                             dict[str, dict[str, set[str]]]]:
    """(``keys.number_variant`` key -> (the one F1 candidate whose document name has it,
    rules that rewrote that name, the number series of its names with that key), or None
    when two candidates share the key (a lead to neither); root relation -> span prefix ->
    the series a document span with that prefix names: ``keys.DOCUMENT_PREFIXES`` and the
    series of the known numbers carrying it this run (``keys.document_series``)). The
    uniqueness of a key is checked over ``pool``, the sampled candidates only: a record
    outside the sample that shares a variant is not seen here, so a lead names a record
    only once the exact root lookup confirms it (``document_candidates`` ``verify``)."""
    roots: dict[str, dict[str, tuple[Candidate, set[str], set[str]]]] = {}
    # keys of the order numbers closed posted BC documents carry (``Candidate.order_number``):
    # a lead only where no record's own name has the key, so a PO's posted documents never
    # make its own leads ambiguous
    ordered: dict[str, dict[str, tuple[Candidate, set[str], set[str]]]] = {}
    prefixes: dict[str, dict[str, set[str]]] = {}
    dropped = superseded_orders(pool)
    for c in pool:
        if c.family != "F1":
            continue
        licensed = prefixes.setdefault(c.root_relation, {
            prefix: {series} for prefix, series in
            K.DOCUMENT_PREFIXES.get(c.root_relation, {}).items()})
        for _, text in document_names(sources, c):
            variant = K.number_variant(text)
            if variant is None or (c in dropped and ordered_name(c, text)):
                continue
            series = K.document_series(c.root_relation, variant[2])
            if variant[2]:
                licensed.setdefault(variant[2], set()).add(series)
            by_key = ordered if ordered_name(c, text) else roots
            entry = by_key.setdefault(variant[0], {}).setdefault(
                canonical([c.root_relation, c.root_key]), (c, set(), set()))
            entry[1].update(variant[1])
            entry[2].add(series)
    for key, by_root in ordered.items():
        roots.setdefault(key, by_root)
    index: dict[str, tuple[Candidate, frozenset[str], frozenset[str]] | None] = {}
    for key, by_root in roots.items():
        (candidate, rules, series), *others = by_root.values()
        index[key] = None if others else (candidate, frozenset(rules), frozenset(series))
    return index, prefixes


def bc_closed(entity_set: str, fields: Mapping[str, Any]) -> bool:
    """Whether a BC document is done: an invoice ``Paid``, an order fully received or
    shipped (``BC_ORDER_DONE``), a posted receipt always."""
    if entity_set in BC_INVOICE_SETS:
        return fields.get("status") == BC_CLOSED_STATUS
    if entity_set in BC_ORDER_DONE:
        return fields.get(BC_ORDER_DONE[entity_set]) is True
    return entity_set == "purchaseReceipts"


def _slug(text: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return (BC_LINEAGE_PREFIX + (slug or "company"))[:50]


# ----------------------------------------------------------------- candidates

def _single_root(sources: Sources, identifier: str, kind: str, relation: str,
                 key: Mapping[str, Any]) -> bool | None:
    """The oracle's own lookup finds exactly this record for the identifier; None (falsy)
    when a read of the lookup failed (``lookup_failed``)."""
    probe = Context(sources, {})
    found = resolve_root(probe, {"identifier": identifier, "identifier_kind": kind},
                         many_ok=kind == "customer_po")
    if lookup_failed(probe.gaps):
        return None
    if probe.gaps:
        return False
    if kind == "customer_po":
        return any(probe.chain_relation(n) == relation and probe.key(n) == dict(key)
                   for n in found)
    return (len(found) == 1 and probe.chain_relation(found[0]) == relation and
            probe.key(found[0]) == dict(key))


def portal_candidates(sources: Sources, stratum: str,
                      funnel: dict[str, int] | None = None) -> list[Candidate]:
    """Closed, non-staging, single-root records of one record stratum, one candidate per
    family that can root at it. ``funnel`` counts rows by the stage they dropped out at, so
    an empty stratum says why (candidates are counted outside it)."""
    kind, roots = PORTAL_STRATA[stratum]
    portal = sources.portal
    counts = funnel if funnel is not None else {}
    stages = ("rows", "no_number", "not_closed") + \
        (("closed_by_header_only",) if stratum == "purchase_order" else ()) + \
        ("native_staging", "not_single_root", "roots")
    for name in stages:
        counts.setdefault(name, 0)
    out: list[Candidate] = []
    for relation, column in roots:
        object_type = K.RELATIONS[relation].object_type
        for row in portal.rows(relation):
            counts["rows"] += 1
            identifier = key_text(row.get(column))
            if identifier is None:
                counts["no_number"] += 1
                continue
            if not closed(relation, row, portal):
                if relation == K.PO and K.po_closed_by_header_only(row.get("status")) and \
                        not portal.lookup(K.PO_LINE, "purchase_order_id", row.get("id")):
                    counts["closed_by_header_only"] += 1  # a lead, never a closed root
                else:
                    counts["not_closed"] += 1
                continue
            origin = lineage(relation, row, portal)
            if origin == "gold_native_staging":
                counts["native_staging"] += 1
                continue
            key = portal.key_of(relation, row)
            if not _single_root(sources, identifier, kind, relation, key):
                counts["not_single_root"] += 1
                continue
            counts["roots"] += 1
            frozen_key = tuple(sorted(key.items()))
            for family in FAMILY_ROOTS:
                if family == "F2" or stratum not in FAMILY_ROOTS[family]:
                    continue
                if FAMILY_KIND.get(family, kind) == kind:
                    out.append(Candidate(stratum, family, identifier, kind, origin, relation,
                                         frozen_key, object_type))
            po_ref = row.get("po_reference") if relation == K.SO else None
            if isinstance(po_ref, str) and po_ref and _DIGIT.search(po_ref):
                siblings = portal.lookup(K.SO, "po_reference", po_ref)
                if (siblings and all(closed(K.SO, s, portal) for s in siblings) and
                        all(lineage(K.SO, s, portal) != "gold_native_staging"
                            for s in siblings) and
                        _single_root(sources, po_ref, "customer_po", relation, key)):
                    members = tuple(sorted({(K.SO, canonical(portal.key_of(K.SO, s)))
                                            for s in siblings}))
                    out.append(Candidate(stratum, "F2", po_ref, "customer_po", origin,
                                         relation, frozen_key, object_type, members))
    return out


# Funnel stage -> the reason an empty stratum reports when every row stopped there.
_RECORD_REASONS = (("rows", "relation_empty"), ("no_number", "no_number"),
                   ("not_closed", "none_closed"),
                   ("closed_by_header_only", "only_closed_by_header"),
                   ("native_staging", "only_native_staging"),
                   ("not_single_root", "no_single_root"))


def record_tried(stratum: str) -> list[str]:
    """The root relation columns a record stratum read (``relation.column``)."""
    return [f"{relation}.{column}" for relation, column in PORTAL_STRATA[stratum][1]]


def empty_reason(funnel: Mapping[str, int]) -> str:
    """Why a record stratum has no candidate, from its funnel counts (row stages first;
    roots that passed them were all in the other split or excluded)."""
    if not funnel.get("rows"):
        return "relation_empty"
    if funnel.get("roots"):
        return "other_split_or_excluded"
    left = funnel["rows"]
    for stage, reason in _RECORD_REASONS[1:]:
        left -= funnel.get(stage, 0)
        if left <= 0:
            return reason
    return "other_split_or_excluded"


def bc_candidates(sources: Sources, limit: int, run_id: str, want: int,
                  accept: Callable[[Candidate], bool] = lambda c: True,
                  funnel: dict[str, int] | None = None,
                  closed_pool: list[Candidate] | None = None) -> list[Candidate]:
    """Posted (``Paid``) BC invoices whose number resolves back to exactly that document.
    Each check costs BC GETs, so records are ranked by the run id and checked until
    ``want`` pass. ``funnel`` counts records (one candidate each); ``other_split`` are the
    records whose candidate belongs to the other split or an excluded root.
    ``closed_pool`` receives every closed record's candidate, checked or not: the roots the
    documents are searched for (``document_candidates`` checks a named one itself)."""
    counts = funnel if funnel is not None else {}
    for name in ("rows", "not_closed", "not_single_root", "other_split"):
        counts.setdefault(name, 0)
    if sources.bc is None:
        return []
    closed_records = []
    for entity_set in BC_INVOICE_SETS:
        for record in sources.bc.sample(entity_set, limit):
            counts["rows"] += 1
            candidate = bc_root(entity_set, record, "bc_invoice")
            if candidate is not None:
                closed_records.append((digest([run_id, entity_set, *dict(
                    candidate.root_key).values()]), candidate))
            else:
                counts["not_closed"] += 1
    closed_records.sort(key=lambda r: r[0])
    if closed_pool is not None:
        closed_pool.extend(c for _, c in closed_records)
    out = []
    for _, candidate in closed_records:
        if len(out) >= want:
            break
        if not accept(candidate):  # checked first: a rejected record costs no BC GET
            counts["other_split"] += 1
            continue
        if not bc_single_root(sources, candidate):
            counts["not_single_root"] += 1
            continue
        out.append(candidate)
    return out


def bc_root(entity_set: str, record: Mapping[str, Any], stratum: str) -> Candidate | None:
    """The F1 candidate of a closed (``bc_closed``) BC document with a number, else None."""
    fields = record["fields"]
    if key_text(fields.get("number")) is None or not bc_closed(entity_set, fields):
        return None
    key = {"company_id": str(record.get("company_id")), "id": str(record.get("id"))}
    object_type, chain_relation = K.BC_OBJECTS[entity_set]
    order_field = BC_ORDER_EDGES.get(entity_set)
    return Candidate(stratum, "F1", str(key_text(fields.get("number"))), "bc_number",
                     _slug(record.get("company_name")), chain_relation,
                     tuple(sorted(key.items())), object_type,
                     order_number=key_text(fields.get(order_field)) if order_field else None,
                     dated=bc_dated(fields))


def bc_dated(fields: Mapping[str, Any]) -> str | None:
    """The first dated field of a BC record (``BC_DATE_FIELDS``) that holds a date."""
    return next((str(fields[f]) for f in BC_DATE_FIELDS if instant(fields.get(f))), None)


def bc_document_roots(sources: Sources, limit: int) -> tuple[list[Candidate], int]:
    """(closed records of ``BC_DOCUMENT_ROOT_SETS``, ``limit`` read per set and company,
    the documents may name; how many records were read). A set whose read fails is
    skipped: its failure stays in the run's ``read_failures`` (``BcReads.failures``)."""
    if sources.bc is None:
        return [], 0
    out, read = [], 0
    for entity_set in BC_DOCUMENT_ROOT_SETS:
        try:
            records = sources.bc.sample(entity_set, limit)
        except ReadFailed:
            continue
        read += len(records)
        out += [c for c in (bc_root(entity_set, r, BC_DOCUMENT_STRATUM) for r in records)
                if c is not None]
    return out, read


def lookup_failed(gaps: Iterable[Mapping[str, Any]]) -> bool:
    """Whether an oracle lookup's gaps (``families.Context.gaps``) say a read failed or
    was cut off: the lookup did not happen, so it decides nothing (never absence, never
    another root) and a sample it ran in is no clean sample."""
    return any(str(gap.get("reason")).startswith("read_failed_")
               or gap.get("reason") == "bc_read_incomplete" for gap in gaps)


def bc_single_root(sources: Sources, candidate: Candidate) -> bool | None:
    """The oracle's own lookup of a BC document number finds exactly that document (None:
    a read of the lookup failed)."""
    return _single_root(sources, candidate.identifier, "bc_number", candidate.root_relation,
                        dict(candidate.root_key))


def bc_read(sources: Sources) -> dict[str, Any]:
    """``read_state`` of the invoice samples ``bc_candidates`` took, both entity sets."""
    states = [sources.bc.sample_state(s) for s in BC_INVOICE_SETS] if sources.bc else []
    read = [s for s in states if s is not None]
    return {"tried": [f"bc:{s}" for s in BC_INVOICE_SETS],
            "complete": len(read) == len(BC_INVOICE_SETS) and all(s["complete"] for s in read),
            "withheld": sum(s["withheld"] for s in read),
            "companies": max((s.get("companies", 0) for s in read), default=0)}


def bc_empty_reason(funnel: Mapping[str, int]) -> str:
    if not funnel.get("rows"):
        return "no_records"
    if funnel.get("not_closed", 0) >= funnel["rows"]:
        return "none_closed"
    if funnel.get("not_single_root"):
        return "no_single_root"
    return "other_split_or_excluded"


def search_roots(pool: Sequence[Candidate], run_id: str,
                 when: Mapping[Candidate, datetime] | None = None) -> list[Candidate]:
    """The F1 roots of ``pool`` in the order the documents are searched for them: round
    robin over the record strata (``STRATA`` order, any other after), each stratum's dated
    roots (``when``, ``root_when``) window by window from the newest (``RECENT_WINDOW``
    roots each), the roots of one window ranked by the run id (``rank``), then the undated
    ranked by the run id, each root record once. Recent first: documents name recent
    transactions (live 2026-10-13: every one of the 34 unmatched PO values of the newest
    purchasing mail sits above every PO row read, case.po_mail_links), and a draw ranked
    over all history found 0-1 mail in 23-24 searches (eval.oracle_dev 2bf15f5, 2d141d7)."""
    dated = when or {}
    by_stratum: dict[str, list[Candidate]] = {}
    seen: set[tuple[str, Any]] = set()
    for c in sorted((c for c in pool if c.family == "F1"),
                    key=lambda c: (c not in dated, -dated[c].timestamp() if c in dated else 0,
                                   rank(run_id, c))):
        if (c.root_relation, c.root_key) not in seen:
            seen.add((c.root_relation, c.root_key))
            by_stratum.setdefault(c.stratum, []).append(c)
    for stratum, roots in by_stratum.items():
        window = {c: i // RECENT_WINDOW for i, c in enumerate(c for c in roots if c in dated)}
        by_stratum[stratum] = sorted(roots, key=lambda c: (c not in window, window.get(c, 0),
                                                           rank(run_id, c)))
    order = sorted(by_stratum, key=lambda s: (s not in STRATA, s in STRATA and STRATA.index(s),
                                              s))
    queues = [by_stratum[s] for s in order]
    return [c for row in zip_longest(*queues) for c in row if c is not None]


def root_when(sources: Sources, candidate: Candidate, now: str | None) -> datetime | None:
    """The date of a Portal root (``root_date`` over its relation's ``date_columns``, the
    date the mail windows use; a BC root's posting/document date, ``Candidate.dated``) when
    it is no later than ``now``, else None (a row not found or not dated)."""
    if candidate.root_relation not in K.RELATIONS:
        when = instant(candidate.dated)
    else:
        row = root_row(sources, candidate)
        when = None if row is None else root_date(
            row.get(column) for column in date_columns(candidate.root_relation))
    end = instant(now)
    return None if when is None or (end is not None and when > end) else when


def root_row(sources: Sources, candidate: Candidate) -> Mapping[str, Any] | None:
    """The one Portal row of ``candidate``'s root key, else None."""
    key = dict(candidate.root_key)
    column, value = next(iter(sorted(key.items())), ("id", None))
    rows = [r for r in sources.portal.lookup(candidate.root_relation, column, value)
            if sources.portal.key_of(candidate.root_relation, r) == key]
    return rows[0] if len(rows) == 1 else None


def root_queries(sources: Sources, candidate: Candidate) -> list[str]:
    """The numbers a document may name ``candidate`` by (``document_names``, best tier
    first), as searched: each once, with at least ``MIN_SHAPE_DIGITS`` digits."""
    out: list[str] = []
    for _, text in sorted(document_names(sources, candidate)):
        if len(_DIGIT.findall(text)) >= MIN_SHAPE_DIGITS and text not in out:
            out.append(text)
    return out


def fallback_query(text: str) -> str | None:
    """A number without its leading letters (``keys.DOCUMENT_VARIANTS`` ``prefix``:
    "PO#12345" -> "12345"), searched for a root whose numbers as written named nothing or
    whose search was refused; None when it has no such prefix or fewer than
    ``keys.VARIANT_MIN_DIGITS`` digits are left. A hit still names the root only by the
    span rules (``names_in``)."""
    stripped = _PREFIX.sub("", text.strip(), count=1)
    if stripped == text.strip() or len(_DIGIT.findall(stripped)) < K.VARIANT_MIN_DIGITS:
        return None
    return stripped


def names_in(spans: Collection[str], index: Mapping[str, Candidate],
             variants: Mapping[str, tuple[Candidate, frozenset[str], frozenset[str]] | None],
             prefixes: Mapping[str, Mapping[str, set[str]]],
             shape_spans: dict[str, set[str]],
             labelled: Collection[str] = (),
             named: dict[Candidate, str] | None = None) -> list[Candidate]:
    """The roots one document's spans name: every root a span names exactly
    (``document_index``, compared as ``document_key``); a document naming none so takes
    leads from the other shapes of its spans and of its ``labelled`` spans
    (``labelled_spans``; ``variant_index``): a variant one known record has alone, written
    with a prefix of that record's own number series (a DPO span never leads to a PO
    record), the span starting with a letter (a bare date or quantity is never a lead). A
    span that names two records exactly (one compared case-folded) names neither.
    ``shape_spans`` collects the spans per shape (``number_spans``, ``span_exact``,
    ``span_<rule>``, ``span_shared``, ``span_labelled``: a lead from a labelled span);
    ``named`` receives, per root first named, the span as the document wrote it (none for a
    lead from a labelled span, which the document wrote apart)."""
    named = {} if named is None else named
    exact: list[Candidate] = []
    for span in sorted(spans):
        if len(_DIGIT.findall(span)) >= MIN_SHAPE_DIGITS:
            shape_spans["number_spans"].add(span)
        bases = list(dict.fromkeys(
            base for key in (span, span.strip().casefold())
            if (base := index.get(key)) is not None
            and document_key(base.root_relation, span) == key))
        if len(bases) > 1:   # one span, two records (a PO number compared case-folded)
            shape_spans["span_shared"].add(span)
        elif bases:
            exact.append(bases[0])
            named.setdefault(bases[0], span)
            shape_spans["span_exact"].add(span)
    if exact:
        return exact
    leads: list[Candidate] = []
    for span in sorted(spans) + sorted(set(labelled) - set(spans)):
        variant = K.number_variant(span) if span[0].isalpha() else None
        if variant is None or variant[0] not in variants:
            continue
        lead = variants[variant[0]]
        if lead is None:
            shape_spans["span_shared"].add(span)
        elif prefixes.get(lead[0].root_relation, {}).get(variant[2], set()) & lead[2]:
            leads.append(lead[0])
            for rule in lead[1] | variant[1]:
                shape_spans["span_" + rule].add(span)
            if span not in spans:
                shape_spans["span_labelled"].add(span)
            else:
                named.setdefault(lead[0], span)
    return leads


_CAUSE = re.compile(r"[a-z_]{1,40}")
# A scope-blocked mail unit (its scope id, never an address) and the later_ask it waits on.
_CAUSE_UNIT = re.compile(r"mailbox-[1-9][0-9]{0,2}")
_CODE = re.compile(r"[a-z][a-z0-9-]{0,47}")


def document_candidates(sources: Sources, documents: DocumentPort | None, kind: str,
                        pool: Sequence[Candidate], limit: int = MAX_ROOT_SEARCHES,
                        funnel: dict[str, int] | None = None,
                        read: dict[str, Any] | None = None,
                        verify: Callable[[Candidate], bool | None] | None = None,
                        run_id: str = "",
                        searchable: Callable[[Candidate], bool] = lambda c: True,
                        now: str | None = None,
                        named: dict[Candidate, str] | None = None) -> list[Candidate]:
    """Closed records the documents of ``kind`` name, root first: the documents are
    searched (``DocumentPort.search``, GET only, at most ``MAX_SEARCH_HITS`` documents per
    search) for the numbers (``root_queries``) of the first ``limit`` searchable roots of
    ``pool`` (``search_roots``: round robin over the record strata, newest first by
    ``root_when`` up to ``now``), and for the roots
    none of whose hits names them, once more for each number without its leading letters
    (``fallback_query``, at most ``limit`` such searches). A hit names a root only when
    one of its separator-bounded spans of a subject, preview, file name or text read
    (``identifier_spans``, and for a lead its ``labelled_spans``) names it (``names_in``)
    and the root is one searched:
    "PO#12345" never names PO#1234. A document found for one root names whichever
    searchable root its spans name, searched or not (a search's hit is a sample of the
    documents, as a listing's is). When the searches name no usable root, the port's
    listing of ``kind`` (``DocumentPort.list_documents``, at most ``MAX_LISTED_DOCUMENTS``
    documents; a port without one, or None for the kind, lists nothing) is named the same
    way (funnel ``listed_documents``; ``listing_failed`` when it failed or answered no
    listing, a budget stop raised; ``listing_incomplete`` when it was cut and not
    ``capped``). A document naming a searchable root and another closed record (one
    not searchable too) names neither.
    ``pool`` holds every closed record known this run
    (Portal and BC), so a name two of them share names neither (a BC twin of a Portal
    record is that record, ``twins``); ``verify`` checks a named
    root that was not checked yet (a BC number: the single-root lookup), counted
    ``not_single_root`` when it fails (``bc_lookup_failed`` when a read of its lookup
    failed). The question is asked from the record's own number; ``named`` receives, per
    root a document named, the span that named it (``names_in``).

    The funnel counts, never values: ``roots`` (searchable), ``roots_searched`` (the roots
    at least one of whose searches was answered: a refused or failed search searched
    nothing), ``searches`` and ``fallback_searches`` (queries asked), when a search was
    refused ``roots_asked`` (the roots searched for) and ``roots_refused`` (those none of
    whose searches was answered and one was refused: ``search_status`` judges a stratum
    mostly refused per root), ``search_refused`` (and
    by cause, ``search_refused_<cause>``: the guard or the tenant refused the search),
    ``search_failed``, ``documents`` (distinct documents found), ``documents_excluded`` (hits
    the port left out by a rule of its own, e.g. a discarded mail folder, when not 0),
    ``documents_naming_one`` (documents naming exactly one searchable root, the
    ``classified`` count; one naming two closed records, one of them not searchable too,
    is ``documents_naming_many``, excluded),
    ``search_scanned``
    (tenant-refused searches answered from a mailbox listing) and ``search_scan_failed``
    (those whose listing failed too; they stay refused),
    the spans per shape of the documents found (``number_spans`` always; ``span_exact``,
    ``span_prefix``, ``span_zeros``, ``span_shared``, ``span_labelled`` when not 0) and the
    port's text read
    counts. ``read`` receives what the search read (``tried``, ``complete``, ``withheld``;
    ``capped`` when only this sampler's own caps left roots or hits unread;
    ``not_configured`` from the port, or ``document_search_not_configured`` for a port that
    cannot search)."""
    counts = funnel if funnel is not None else {}
    for name in ("roots", "roots_searched", "searches", "documents", "documents_naming_one"):
        counts.setdefault(name, 0)
    state: dict[str, Any] = read if read is not None else {}
    if documents is None:
        return []
    if not hasattr(documents, "search"):
        state.update(tried=[], complete=False, withheld=0,
                     not_configured="document_search_not_configured")
        return []
    search = documents.search
    index = document_index(sources, pool)
    variants, prefixes = variant_index(sources, pool)
    eligible = [c for c in pool if searchable(c)]
    roots = search_roots(eligible, run_id, {c: when for c in eligible
                                            if (when := root_when(sources, c, now))})
    searched = roots[:limit]
    counts["roots"] = len(roots)
    queries = {root: root_queries(sources, root) for root in searched}
    asked = list(dict.fromkeys(q for root in searched for q in queries[root]))
    counts["searches"] = len(asked)
    sent = set(asked)
    found = [search(kind, asked, hits=MAX_SEARCH_HITS, seed=run_id)]
    records: dict[str, Mapping[str, Any]] = {}
    refusal: dict[str, Any] = {}   # the first tenant refusal's HTTP status and error code
    outcome = {"hits": set(), "refused": {}, "failed": 0, "cut": False, "withheld": 0,
               "answered": set(), "refused_queries": set(), "failed_queries": set(),
               "excluded": 0, "scanned": 0, "scan_failed": 0, "unit_unanswered": {}}

    def take(answer: Mapping[str, Any]) -> None:
        for record in answer.get("records") or []:
            if isinstance(record, Mapping) and isinstance(record.get("handle"), str):
                records.setdefault(record["handle"], record)
        for result in answer.get("results") or []:
            if isinstance(result, Mapping) and result.get("scan"):
                outcome["scanned"] += 1   # a tenant-refused search answered by a listing
            elif isinstance(result, Mapping) and result.get("scan_failed"):
                outcome["scan_failed"] += 1
            if not isinstance(result, Mapping):
                outcome["failed"] += 1
            elif result.get("unit") and ("refused" in result or "failed" in result):
                # a unit that could not be asked beside units that answered the query:
                # counted apart, so the searches are not counted once per unit
                key = str(result.get("refused") or "failed")
                key = key if _CAUSE.fullmatch(key) else "other"
                outcome["unit_unanswered"][key] = outcome["unit_unanswered"].get(key, 0) + 1
            elif "refused" in result:
                cause = str(result["refused"])
                cause = cause if _CAUSE.fullmatch(cause) else "other"
                outcome["refused"][cause] = outcome["refused"].get(cause, 0) + 1
                if cause == "tenant" and not refusal:
                    refusal.update({k: result[k] for k in ("http_status", "error_code")
                                    if result.get(k)})
                if isinstance(result.get("query"), str):
                    outcome["refused_queries"].add(result["query"])
            elif "failed" in result or not isinstance(result.get("handles"), list):
                outcome["failed"] += 1
                if isinstance(result.get("query"), str):
                    outcome["failed_queries"].add(result["query"])
            else:
                outcome["hits"].update(h for h in result["handles"] if isinstance(h, str))
                if isinstance(result.get("query"), str):
                    outcome["answered"].add(result["query"])
                outcome["withheld"] += int(result.get("withheld") or 0)
                outcome["excluded"] += int(result.get("excluded") or 0)
                outcome["cut"] = outcome["cut"] or result.get("complete") is not True

    take(found[0])
    checked: dict[Candidate, bool] = {}

    def usable(root: Candidate) -> bool:
        if verify is None:
            return True
        if root not in checked:
            verdict = verify(root)
            checked[root] = bool(verdict)
            if not verdict:
                stage = "bc_lookup_failed" if verdict is None else "not_single_root"
                counts[stage] = counts.get(stage, 0) + 1
        return checked[root]

    shape_spans: dict[str, set[str]] = {name: set() for name in (
        "number_spans", "span_exact", *("span_" + rule for rule, _ in K.DOCUMENT_VARIANTS),
        "span_shared", "span_labelled", "span_order_number")}
    order_numbers = {c.order_number for c in pool if c.order_number}
    wanted = set(eligible)   # a document found names any searchable root
    named_by: dict[str, list[Candidate]] = {}
    merged: dict[Candidate, Candidate] = {}   # BC twin -> its Portal record (``twins``)

    def name_hits() -> set[Candidate]:
        nonlocal index, variants, prefixes
        read: dict[str, tuple[set[str], set[str]]] = {}
        for handle in sorted(outcome["hits"] - set(named_by)):
            record = records.get(handle)
            if record is None:
                continue
            spans: set[str] = set()
            labelled: set[str] = set()
            for text in [*(record.get("texts") or []), *(record.get("contents") or [])]:
                spans |= identifier_spans(text)
                labelled |= labelled_spans(text)
            shape_spans["span_order_number"] |= spans & order_numbers
            read[handle] = spans, labelled
        found_twins = twins(sources, [c for c in pool if c not in merged],
                            {span for spans, _ in read.values() for span in spans})
        if found_twins:   # a twin's number names its Portal record, searched for either
            merged.update(found_twins)
            kept = [c for c in pool if c not in merged]
            index = document_index(sources, kept)
            variants, prefixes = variant_index(sources, kept)
            wanted.update(owner for twin, owner in found_twins.items() if twin in wanted)
        for handle, (spans, labelled) in read.items():
            # every closed record named, searchable or not: one more names neither
            named_by[handle] = names_in(spans, index, variants, prefixes, shape_spans,
                                        labelled, named)
        return {c for names in named_by.values() for c in names if c in wanted}

    first = name_hits()
    fallback = list(dict.fromkeys(
        f for root in searched if root not in first
        for f in map(fallback_query, queries[root]) if f is not None and f not in asked))
    capped = len(searched) < len(roots) or len(fallback) > limit
    fallback = fallback[:limit]
    if fallback and not found[0].get("not_configured"):
        counts["fallback_searches"] = len(fallback)
        sent.update(fallback)
        found.append(search(kind, fallback, hits=MAX_SEARCH_HITS, seed=run_id))
        take(found[-1])
        name_hits()

    def identities(names: list[Candidate]) -> set[Any]:
        # the closed records a document names (an unsearchable one too), less a
        # searchable root that failed its single-root check
        return {c.root_records() for c in names if c not in wanted or usable(c)}

    def named_roots() -> dict[Candidate, Candidate]:
        named: dict[Candidate, Candidate] = {}
        for handle in sorted(named_by):
            who = identities(named_by[handle])
            usable_roots = [c for c in named_by[handle] if c in wanted and usable(c)]
            if len(who) != 1 or not usable_roots:
                # a document naming two closed records identifies neither
                if len(who) > 1:
                    counts["documents_naming_many"] = \
                        counts.get("documents_naming_many", 0) + 1
                continue
            counts["documents_naming_one"] += 1
            for root in usable_roots:
                # lineage is where the root record came from; the document stratum (where
                # the identifier was found) stays in Generated.item_strata.
                named.setdefault(root, replace(root, stratum=kind, family="F1", members=()))
        return named

    listing_gap = False
    if hasattr(documents, "list_documents") and not found[0].get("not_configured") and not any(
            len(identities(names)) == 1 and any(c in wanted and usable(c) for c in names)
            for names in named_by.values()):
        try:
            listing = documents.list_documents(kind, MAX_LISTED_DOCUMENTS, seed=run_id)
        except Exception as exc:  # the document port's error types are not imported here
            if type(exc).__name__ == BUDGET_STOP:
                raise
            counts["listing_failed"] = 1
            listing = None
        if isinstance(listing, Mapping) and not listing.get("not_configured"):
            listed = [r for r in listing.get("records") or []
                      if isinstance(r, Mapping) and isinstance(r.get("handle"), str)]
            counts["listed_documents"] = len(listed)
            for record in listed:
                records.setdefault(record["handle"], record)
            outcome["hits"].update(r["handle"] for r in listed)
            outcome["withheld"] += int(listing.get("withheld") or 0)
            found.append(listing)
            listing_gap = listing.get("complete") is not True and not listing.get("capped")
            if listing_gap:   # cut by no limit of the port's own: never a sample
                counts["listing_incomplete"] = 1
            outcome["cut"] = outcome["cut"] or listing.get("complete") is not True
            name_hits()
        elif listing is not None and "listing_failed" not in counts:
            counts["listing_failed"] = 1   # not a listing: never a silent empty one
    out = named_roots()
    counts["documents"] = len(named_by)
    # A result for a query never asked credits none. A root is searched when one of its
    # searches (its numbers and their fallbacks) was answered, refused when none was and
    # one was refused, or none failed and a refusal names no query asked (it may be the
    # root's: directive f6427307e9b0, a root whose search failed is unavailable,
    # ``search_failed``, never refused by a refusal it may not have had).
    answered = outcome["answered"] & sent
    refused_queries = outcome["refused_queries"] & sent
    failed_queries = outcome["failed_queries"]
    unattributed = sum(outcome["refused"].values()) > len(refused_queries)
    own = {root: {*queries[root], *map(fallback_query, queries[root])} for root in searched
           if queries[root]}
    counts["roots_searched"] = sum(1 for asked in own.values() if answered & asked)
    roots_refused = sum(1 for asked in own.values()
                        if not answered & asked and (refused_queries & asked or (
                            unattributed and not failed_queries & asked)))
    counts.update({name: len(spans) for name, spans in shape_spans.items()
                   if spans or name == "number_spans"})
    refused = outcome["refused"]
    if refused:
        counts["roots_asked"], counts["roots_refused"] = len(own), roots_refused
        counts["search_refused"] = sum(refused.values())
        counts.update({"search_refused_" + cause: n for cause, n in refused.items()})
    for name in ("scanned", "scan_failed"):
        if outcome[name]:   # tenant-refused searches answered by (or failed to scan) a listing
            counts["search_" + name] = outcome[name]
    if outcome["failed"]:
        counts["search_failed"] = outcome["failed"]
    counts.update({"search_unit_" + cause: n for cause, n in outcome["unit_unanswered"].items()})
    if outcome["excluded"]:
        counts["documents_excluded"] = outcome["excluded"]
    texts: dict[str, int] = {}
    for answer in found:
        for name in ("texts_read", "texts_unread"):
            if isinstance(answer.get(name), int):
                texts[name] = texts.get(name, 0) + answer[name]
        unread_by = answer.get("texts_unread_by")
        for cause, number in (unread_by.items() if isinstance(unread_by, Mapping) else ()):
            if isinstance(number, int) and number and _CAUSE.fullmatch(str(cause)):
                texts["texts_unread_" + cause] = texts.get("texts_unread_" + cause, 0) + number
    counts.update({k: v for k, v in texts.items() if v or k == "texts_read"})
    sources_tried = list(dict.fromkeys(str(s) for answer in found
                                       for s in answer.get("source") or ()))
    failed = bool(outcome["failed"] or refused or outcome["unit_unanswered"] or counts.get("bc_lookup_failed")
                  or counts.get("listing_failed") or listing_gap)
    capped = capped or outcome["cut"]
    state.update(tried=sources_tried, complete=not failed and not capped,
                 withheld=outcome["withheld"])
    if capped and not failed:
        state["capped"] = True
    if refused and set(refused) == {"tenant"} and not (
            outcome["answered"] or outcome["failed"] or outcome["scanned"]):
        # the source refused every query it was asked: nothing was measured, not a sample
        state["search_blocked"] = dict(refusal)
    if refused and set(refused) == {"scope"} and not (
            outcome["answered"] or outcome["failed"] or outcome["scanned"]):
        # every query was refused by the capture scope (a unit outside the grant, or one
        # the people-exclusion guard cannot clear): what each blocked unit waits on
        blocked: dict[str, str] = {}
        for answer in found:
            named = answer.get("scope_blocked")
            for unit, code in (named.items() if isinstance(named, Mapping) else ()):
                if _CAUSE_UNIT.fullmatch(str(unit)) and _CODE.fullmatch(str(code)):
                    blocked.setdefault(str(unit), str(code))
        state["scope_blocked"] = dict(sorted(blocked.items()))
    if found[0].get("not_configured"):
        state["not_configured"] = str(found[0]["not_configured"])
    return list(out.values())


def block_refused_upstream(entry: dict[str, Any], refused_all: Mapping[str, Any] | None) -> None:
    """The one rule for mail blocked by tenant refusal, shared by ``eval.oracle_dev`` and
    ``eval.unfamiliar_sample``: a document stratum read ``search_refused`` whose searches
    the tenant all refused (``refused_all``, set by ``document_candidates`` when nothing was
    answered, failed or scanned) is ``blocked: upstream``. Mostly refused with documents
    found, or any answered search, is not."""
    if refused_all is not None and entry.get("reason") == "search_refused":
        entry.update(blocked="upstream", **refused_all)


def block_refused_scope(entry: dict[str, Any], scope_all: Mapping[str, str] | None) -> None:
    """The one rule for mail its capture scope blocked, shared like
    ``block_refused_upstream``: a document stratum read ``search_refused`` whose searches
    the scope all refused (``scope_all``: scope id -> later_ask code, set by
    ``document_candidates`` when nothing was answered, failed or scanned) is ``blocked:
    scope`` and names each blocked unit. Any unit answered, or any other refusal, is not."""
    if scope_all is not None and entry.get("reason") == "search_refused" \
            and "blocked" not in entry:
        entry.update(blocked="scope", scope_blocked=dict(scope_all))


def search_status(read: Mapping[str, Any], funnel: Mapping[str, int]) -> tuple[str, str]:
    """(status, reason) of a document stratum whose search yielded no candidate:
    ``not_configured`` (the port's reason); ``empty`` when there was no closed root to
    search (``no_record_candidates``) or none carried a searchable number
    (``no_searchable_number``); ``search_refused`` when most roots searched for were
    refused;
    ``unreadable`` (nothing found) or ``incomplete`` when a search or a named root's lookup
    failed (``search_failed``, ``root_lookup_failed``), the listing read when the searches
    named no root failed (``listing_failed``) or was cut by no limit of the port's own
    (``listing_incomplete``) or only withheld
    documents were found (``withheld``). Otherwise the searches are a sample of what names
    the roots: ``no_candidate_in_sample`` with ``no_single_root`` (a named root failed its
    single-root check) or ``no_document_names_root``, beside the funnel's
    ``roots_searched``. Searches mostly refused are no sample either, judged per root
    (directive d0125cbdcdf6: more of the roots asked were refused, ``roots_refused``, than
    not, so a root with several numbers weighs as one): ``search_refused``,
    ``unavailable`` when nothing was found, else ``incomplete``."""
    if read.get("not_configured"):
        return "not_configured", str(read["not_configured"])
    if not funnel.get("roots"):
        return "empty", "no_record_candidates"
    asked = funnel.get("searches", 0) + funnel.get("fallback_searches", 0)
    if not asked:
        return "empty", "no_searchable_number"
    found = funnel.get("documents")
    if 2 * funnel.get("roots_refused", 0) > funnel.get("roots_asked", 0):
        return ("incomplete" if found else "unavailable"), "search_refused"
    status = "incomplete" if found else "unreadable"
    if funnel.get("search_failed"):
        return status, "search_failed"
    if funnel.get("bc_lookup_failed"):
        return status, "root_lookup_failed"
    if funnel.get("listing_failed"):
        return status, "listing_failed"
    if funnel.get("listing_incomplete"):
        return status, "listing_incomplete"
    if not funnel.get("documents") and read.get("withheld"):
        return "unreadable", "withheld"
    if funnel.get("not_single_root"):
        return "no_candidate_in_sample", "no_single_root"
    return "no_candidate_in_sample", "no_document_names_root"


def document_cause(funnel: Mapping[str, int]) -> str:
    """The measured cause of a document stratum that read documents and none named a root
    (``no_document_names_root``); its counts are the funnel's ``texts_*``: ``text_budget``
    (documents past the text budget stayed unread, at least as many as OCR left), ``ocr``
    (the OCR left as many documents unread as were read) or ``no_root_in_sample`` (what
    could be read was read and names none)."""
    budget = funnel.get("texts_unread_text_budget", 0)
    ocr = sum(n for name, n in funnel.items() if name.startswith("texts_unread_ocr_")) \
        + funnel.get("texts_unread_unread_scanned_pages", 0)
    return ("text_budget" if budget and budget >= ocr else
            "ocr" if ocr and ocr >= funnel.get("texts_read", 0) else "no_root_in_sample")


def unread_status(read: Mapping[str, Any] | None, records: int) -> tuple[str, str] | None:
    """(status, reason) of a sample read that yielded no candidate but may not be called
    empty (unreadable is not empty, CONTEXT/01 §6 #3): a kind its port has no reader for is
    ``not_configured``; one the run budget stopped (``blocked: budget``) is ``unavailable``. A read whose readable remainder was read to its end or cut only by the
    sampler's own limits (``capped``) is a sample of what the reader can read:
    ``no_candidate_in_sample`` (``sample_cap``, or ``withheld`` with the withheld count
    beside it), neither empty nor unreadable. Any other cut, and a read that returned
    nothing but withheld items, is ``unreadable`` when it returned nothing and ``incomplete``
    when it returned some. None: a full read."""
    if read is None:
        return None
    if read.get("not_configured"):
        return "not_configured", read["not_configured"]
    if read.get("blocked") == "budget":
        return "unavailable", BUDGET_STOP
    if (read["complete"] or read.get("capped")) and (records or not read["withheld"]):
        if read.get("capped"):
            return "no_candidate_in_sample", "sample_cap"
        if read["withheld"]:
            return "no_candidate_in_sample", "withheld"
        return None
    return ("incomplete" if records else "unreadable",
            "withheld" if read["withheld"] else "truncated")


# ----------------------------------------------------------------- generation

def generate(sources: Sources, *, run_id: str, now: str, count: int = 30,
             documents: DocumentPort | None = None, id_suffix: str | None = None,
             split: str = "dev", audit: int = 10, evidence_note: str | None = None,
             bc_sample_limit: int = 10, bc_want: int = 6,
             exclude_roots: Collection[str] = (),
             perturb: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None
             ) -> Generated:
    """Sample ``count`` items of one split. Only roots of that split's partition
    (``split_of``) and families in its quota are candidates. ``exclude_roots`` (record
    digests, e.g. a frozen dev manifest's roots and chain records) drops a candidate whose
    root is one of them, and an item whose answer chain touches one (``shared_chain``), so
    a held-out run shares no transaction record with the dev set. ``perturb`` (held-out
    only, e.g. ``perturb.variants`` over the fixture world) adds metamorphic variants of
    the sampled items after them. The document strata search for as many roots per
    source as its port affords (``max_roots``, else ``MAX_ROOT_SEARCHES``;
    ``document_candidates``)."""
    if split not in QUOTAS:
        raise ValueError("split must be dev or held_out")
    if perturb is not None and split != "held_out":
        raise ValueError("perturbed variants are held-out only")
    quota = QUOTAS[split]
    excluded = frozenset(exclude_roots)
    dropped = {"other_split": 0, "excluded_root": 0}

    def refusal(candidate: Candidate) -> str | None:
        """Why a candidate is no root of this run (None: it is one)."""
        if candidate.family not in quota:
            return "family"
        if candidate_split(candidate) != split:
            return "other_split"
        if any(root_digest(r, k) in excluded for r, k in candidate.root_records()):
            return "excluded_root"
        return None

    def accept(candidate: Candidate) -> bool:
        why = refusal(candidate)
        if why in dropped:
            dropped[why] += 1
        return why is None

    if id_suffix is not None and not re.fullmatch(r"[a-z0-9]{1,16}", id_suffix):
        raise ValueError("id_suffix must be 1..16 of [a-z0-9]")
    rng = random.Random(int(sha256(run_id.encode()).hexdigest()[:16], 16))
    sources.portal.prefetch()
    report: dict[str, dict[str, Any]] = {}
    pools: dict[str, list[Candidate]] = {}
    funnels: dict[str, dict[str, int]] = {s: {} for s in STRATA}
    reads: dict[str, dict[str, Any]] = {}  # what the BC sample and the searches read
    for stratum in PORTAL_STRATA:
        try:
            pools[stratum] = portal_candidates(sources, stratum, funnels[stratum])
            report[stratum] = {"status": "read", "tried": record_tried(stratum)}
            unknown = [n for n in (out_of_vocabulary(r, sources.portal.rows(r))
                                   for r, _ in PORTAL_STRATA[stratum][1]) if n is not None]
            if unknown:
                report[stratum]["status_out_of_vocabulary"] = sum(unknown)
        except ReadFailed as exc:
            pools[stratum] = []
            report[stratum] = {"status": "unavailable", "reason": exc.reason,
                               "tried": record_tried(stratum), **exc.fields()}
    bc_closed_pool: list[Candidate] = []
    try:
        pools["bc_invoice"] = bc_candidates(sources, bc_sample_limit, run_id, bc_want, accept,
                                            funnels["bc_invoice"], bc_closed_pool)
        report["bc_invoice"] = ({"status": "read"} if sources.bc is not None else
                                {"status": "not_configured", "reason": "bc_not_configured"})
    except ReadFailed as exc:
        pools["bc_invoice"] = []
        report["bc_invoice"] = {"status": "unavailable", "reason": exc.reason,
                                **exc.fields()}
    if sources.bc is not None:
        reads["bc_invoice"] = bc_read(sources)
    for stratum in PORTAL_STRATA:
        before = len(pools[stratum])
        pools[stratum] = [c for c in pools[stratum] if accept(c)]
        report[stratum]["candidates_dropped_by_split"] = before - len(pools[stratum])
    # The roots the documents are searched for: every record stratum's candidates and the
    # closed BC records of this run not checked yet, the closed BC orders and posted
    # receipts among them (``bc_document_roots``; a document naming one checks it), so a
    # Portal outage leaves the documents BC's roots instead of none. Every closed record
    # known this run keys the span rules, so a name two records share names neither.
    checked_pool = [c for s in (*PORTAL_STRATA, "bc_invoice") for c in pools[s]]
    checked_set = set(checked_pool)
    bc_documents, bc_documents_read = bc_document_roots(sources, bc_sample_limit)
    if bc_documents_read:   # the closed ones among them
        report["bc_invoice"]["document_roots"] = len(bc_documents)
    known_pool = checked_pool + [c for c in bc_closed_pool if c not in checked_set] + \
        bc_documents

    def verify(candidate: Candidate) -> bool | None:
        return candidate in checked_set or bc_single_root(sources, candidate)

    for kind in DOCUMENT_STRATA:
        read: dict[str, Any] = {}
        try:
            pools[kind] = document_candidates(
                sources, documents, kind, known_pool,
                documents.max_roots if hasattr(documents, "max_roots") else MAX_ROOT_SEARCHES,
                funnels[kind], read,
                verify, run_id, lambda c: refusal(c) is None, now)
            report[kind] = ({"status": "read"} if documents is not None else
                            {"status": "not_configured", "reason": "documents_not_configured"})
        except Exception as exc:  # the document port's error types are not imported
            pools[kind], read = [], {}
            report[kind] = {"status": "unavailable", "reason": type(exc).__name__}
            if type(exc).__name__ == BUDGET_STOP:
                report[kind]["blocked"] = "budget"
        if read:
            reads[kind] = read
    for stratum in STRATA:
        # Ranked by a digest of run id and candidate, not shuffled: one run id keeps its
        # items while the source grows, except where a new candidate ranks in.
        pools[stratum] = sorted(pools[stratum], key=lambda c: rank(run_id, c))
        entry = report[stratum]
        read = reads.get(stratum)
        if read is not None:
            entry.update(read)
        refused_all = entry.pop("search_blocked", None)
        scope_all = entry.pop("scope_blocked", None)
        entry.setdefault("tried", [])
        entry["candidates"] = len(pools[stratum])
        entry["candidates_by_family"] = _by_family(pools[stratum])
        entry["funnel"] = dict(funnels[stratum])
        if entry["status"] == "read" and not pools[stratum]:
            # A stratum read without a single candidate says why and what it tried; it is
            # never a silent 0, and a withheld, cut-off or unconfigured read is not empty.
            funnel = funnels[stratum]
            if stratum in DOCUMENT_STRATA:
                entry["status"], entry["reason"] = search_status(read or {}, funnel)
                if entry["reason"] == "no_document_names_root":
                    cause = document_cause(funnel)   # its counts are the funnel's texts_*
                    if cause != "no_root_in_sample":   # else the reason itself says it
                        entry["blocked"] = cause
                block_refused_upstream(entry, refused_all)
                block_refused_scope(entry, scope_all)
                continue
            if stratum == "bc_invoice":
                basis, records = bc_empty_reason(funnel), funnel.get("rows", 0)
            else:
                basis, records = empty_reason(funnel), funnel.get("rows", 0)
            unread = unread_status(read, records)
            entry["status"], entry["reason"] = unread or ("empty", basis)
            if unread is not None and unread[0] != "not_configured":
                entry["within_read"] = basis

    # Transaction-level exclusion: an item whose answer chain touches an excluded record
    # (a dev root or a record of a dev item's chain) is dropped and replaced, round by
    # round, until no chosen item touches one. Answers are computed once per candidate.
    answers: dict[Candidate, Any] = {}
    banned: set[Candidate] = set()
    dropped["shared_chain"] = 0
    while True:
        live_pools = {k: [c for c in v if c not in banned] for k, v in pools.items()}
        chosen = _allocate(live_pools, count, run_id, quota)
        rejects = []
        for candidate in chosen:
            if candidate not in answers:
                answers[candidate] = _answer(sources, candidate, now)
            if excluded and _touches(answers[candidate][2], answers[candidate][0], excluded):
                rejects.append(candidate)
        if not rejects:
            break
        banned.update(rejects)
        dropped["shared_chain"] += len(rejects)
    code = oracle_code_digest()
    counters: dict[str, int] = {}
    items = []
    for candidate in chosen:
        counters[candidate.family] = counters.get(candidate.family, 0) + 1
        items.append(_item(sources, candidate, counters[candidate.family], now, code,
                           split=split, id_suffix=id_suffix, evidence_note=evidence_note,
                           answered=answers[candidate]))
    _mark_audit(items, audit, rng)
    item_strata = {item["id"]: c.stratum for item, c in zip(items, chosen)}
    for stratum in STRATA:
        report[stratum]["items"] = sum(1 for c in chosen if c.stratum == stratum)
        report[stratum]["items_by_family"] = _by_family([c for c in chosen
                                                         if c.stratum == stratum])
    if perturb is not None:
        variants = perturb(items)
        item_strata.update({v["id"]: item_strata[v["parent_id"]] for v in variants})
        items += variants
    failures = sources.portal.failures()
    if sources.bc is not None:
        failures.update(sources.bc.failures())
    return Generated(items, report, item_strata, failures, split, dict(dropped))


def _by_family(candidates: Sequence[Candidate]) -> dict[str, int]:
    out: dict[str, int] = {}
    for candidate in candidates:
        out[candidate.family] = out.get(candidate.family, 0) + 1
    return dict(sorted(out.items()))


def unexplained_strata(strata: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Strata that read no candidate without saying why: status ``read`` with zero
    candidates, a status other than ``read`` without a reason, or ``empty`` without the
    source it tried. Must be empty."""
    return sorted(name for name, entry in strata.items()
                  if (entry.get("status") == "read" and not entry.get("candidates"))
                  or (entry.get("status") != "read" and not entry.get("reason"))
                  or (entry.get("status") == "empty" and not entry.get("tried")))


def rank(run_id: str, candidate: Candidate) -> str:
    return digest([run_id, candidate.family, candidate.identifier, candidate.root_key])


def _allocate(pools: Mapping[str, list[Candidate]], count: int, run_id: str,
              family_quota: Mapping[str, int]) -> list[Candidate]:
    """Fill each family's quota (scaled to ``count``) round-robin over its strata; spill
    an unfillable quota to the families that still have candidates."""
    total = sum(family_quota.values())
    quota = {f: (family_quota[f] * count) // total for f in family_quota}
    for family in list(family_quota)[:count - sum(quota.values())]:
        quota[family] += 1
    used: set[tuple[str, str]] = set()
    picked: list[Candidate] = []
    queues = {f: {s: [c for c in pools[s] if c.family == f] for s in FAMILY_ROOTS[f]}
              for f in family_quota}

    def take(family: str, limit: int) -> int:
        strata = sorted(FAMILY_ROOTS[family], key=lambda s: digest([run_id, family, s]))
        got = 0
        while got < limit:
            progressed = False
            for stratum in strata:
                queue = queues[family][stratum]
                while queue:
                    candidate = queue.pop(0)
                    if (family, candidate.identifier) in used:
                        continue
                    used.add((family, candidate.identifier))
                    picked.append(candidate)
                    got += 1
                    progressed = True
                    break
                if got >= limit:
                    break
            if not progressed:
                break
        return got

    short = 0
    for family in family_quota:
        short += quota[family] - take(family, quota[family])
    for family in family_quota:
        if short <= 0:
            break
        short -= take(family, short)
    order = {f: i for i, f in enumerate(family_quota)}
    return sorted(picked, key=lambda c: order[c.family])


def _answer(sources: Sources, candidate: Candidate, now: str) -> tuple[dict, str, Any]:
    """(root, as-known-at time, oracle answer) of one candidate."""
    root = {"identifier": candidate.identifier, "identifier_kind": candidate.kind,
            "object_type": candidate.object_type, "relation": candidate.root_relation,
            "key": dict(candidate.root_key)}
    when = sources.portal.replica_time() or now
    return root, when, run(candidate.family, root, {"time": when, "l1_generations": {}},
                           sources)


def chain_digests(root: Mapping[str, Any], result: Mapping[str, Any]) -> set[str]:
    """Record digests of an answer: its root and every chain record."""
    out = {root_digest(root["relation"], root["key"])}
    for entry in result.get("chain") or []:
        out.add(root_digest(entry["relation"], entry["key"]))
    return out


def _touches(answer: Any, root: Mapping[str, Any], excluded: Collection[str]) -> bool:
    return bool(chain_digests(root, answer.result) & set(excluded))


def _item(sources: Sources, candidate: Candidate, number: int, now: str, code: str, *,
          split: str, id_suffix: str | None, evidence_note: str | None,
          answered: tuple[dict, str, Any] | None = None) -> dict[str, Any]:
    prefix = "dev" if split == "dev" else "ho"
    item_id = f"{prefix}-{candidate.family}-{number:04d}" + (f"-{id_suffix}" if id_suffix else "")
    root, when, answer = answered or _answer(sources, candidate, now)
    as_known_at = {"time": when, "l1_generations": answer.generations}
    label: dict[str, Any] = {"status": "oracle_only"}
    if evidence_note:
        label["note"] = evidence_note
    oracle = ORACLES[candidate.family]
    item = {
        "format": ITEM_FORMAT,
        "id": item_id,
        "family": candidate.family,
        "split": split,
        "variant": "standard",
        "question": {"text": QUESTION[candidate.family].format(
            label=K.KIND_LABEL[candidate.kind], identifier=candidate.identifier)},
        "root": root,
        "as_known_at": as_known_at,
        "closed_transaction": True,
        "lineage_stratum": candidate.lineage,
        "known_outcome": answer.result,
        "must_not": ["claim_uncited_number", "invent_absent_value"],
        "oracle": {"id": oracle_id_prefix(oracle) + code[:12], "interface": INTERFACE,
                   "derived_from": _with_closed_cite(answer.derived_from,
                                                     candidate.root_relation),
                   "computed_at": now,
                   "query_digest": digest({"family": candidate.family, "root": root,
                                           "generations": answer.generations})},
        "label": label,
        "scoring": {"exact_values": True, "gap_honesty": True, "citations_required": True},
        "created_at": now,
    }
    return item


def oracle_id_prefix(oracle: Any) -> str:
    """The id prefix of every item a family oracle answered (``oracle:<name>@``)."""
    return f"oracle:{oracle.name}@"


# The oracle ids a generated item carries, one per family: the generator's own mark.
ORACLE_ID_PREFIXES = tuple(oracle_id_prefix(oracle) for oracle in ORACLES.values())

MAX_CITES = 40  # item.schema.json oracle.derived_from maxItems


def _with_closed_cite(derived_from: Sequence[str], relation: str) -> list[str]:
    """The answer's rules plus the rule that made the root a closed transaction, within
    ``MAX_CITES``: when they do not fit, the last derivation slot counts the cites cut."""
    cite = K.CLOSED_CITE.get(relation)
    closure = [cite] if cite and cite not in derived_from else []
    room = MAX_CITES - len(closure)
    out = list(derived_from)
    if len(out) > room:
        out = out[:room - 1] + [f"{len(out) - room + 1} more derivation cites not listed"]
    return out + closure


def _mark_audit(items: list[dict[str, Any]], audit: int, rng: random.Random) -> None:
    by_family: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_family.setdefault(item["family"], []).append(item)
    for group in by_family.values():
        rng.shuffle(group)
    marked = 0
    while marked < min(audit, len(items)):
        for family in sorted(by_family):
            if by_family[family] and marked < audit:
                item = by_family[family].pop(0)
                note = item["label"].get("note")
                item["label"]["note"] = (note + " " if note else "") + AUDIT_NOTE
                marked += 1


def audit_requested(item: Mapping[str, Any]) -> bool:
    note = (item.get("label") or {}).get("note")
    return isinstance(note, str) and "audit_requested" in note


def item_hash(item: Mapping[str, Any]) -> str:
    return digest(item)


__all__ = ["AUDIT_NOTE", "Candidate", "Generated", "HELD_OUT_COUNT", "HELD_OUT_ONLY",
           "ITEM_FORMAT", "LINEAGE_PREFIXES", "ORACLE_ID_PREFIXES", "QUOTAS", "STRATA", "audit_requested", "candidate_split", "closed",
           "chain_digests", "generate", "identifier_spans", "item_hash", "item_root_digest",
           "lineage", "lookup_failed", "oracle_code_digest", "out_of_vocabulary", "po_code",
           "root_digest", "split_of", "unexplained_strata"]
