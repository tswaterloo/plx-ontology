"""BC-only unfamiliar three-way eval: seeded BC purchase documents, answered by the product.

live check ``eval.bc_three_way``.
It reads Business Central only (Production, GET only, through
``integrations.bc_read``), so it runs while the Portal reporting API is down.

Per run, seeded by the run id (``live-<UTC hour>`` by default):

1. **Sample.** Per BC company, one page of purchase orders, posted receipts (newest first)
   and purchase invoices; each names a PO number (its own number, or ``orderNumber``). The
   seeded draw takes up to ``--count`` distinct (company, PO number) roots, round robin
   over the three document sets. The sample is recorded as 16-hex hashes (``items``,
   ``items_digest``), never as numbers.
2. **Oracle.** ``oracle_reading`` reads the PO, its lines, its posted receipts and its
   invoices by ``orderNumber`` in that company through the oracle's own cached exact reads
   (``evals.oracle.source.BcReads``: whole records, no ``$select``) and decides
   the aspects of ``evals/oracle/three_way.py`` (``ASPECTS``) with its rules: PO status
   (``DRAFT`` not issued, cancelled undecided); receipt ``present`` when a receipt is
   posted; invoice ``present`` when a counted invoice of the PO's own vendor exists
   (undecided when only another vendor's is); either stage ``absent`` (reason
   ``absent_in_bc``, with the read time) when its document read finds none and every Item
   line of the PO records ``receivedQuantity`` / ``invoicedQuantity`` 0, every read
   complete (whether or not it names the ``orderNumber``),
   else undecided (an incomplete read, no Item line, an unreadable or a nonzero figure);
   and both sides per item with ``three_way``'s side rule (one-sided EPS for ordered vs
   received, exact for received vs invoiced; a one-sided item, a mixed unit or an empty
   quantity is undecided), once both stages are present.
3. **Product.** ``integrations.bc_purchase.read_purchase_evidence`` reads the same PO in
   that company with ``scoped_completeness`` (also invoices by ``orderId`` and the lines'
   item masters); its cited documents, lines and items become ``bc`` L1 captures
   (``EvidenceCaptures``, lineage ``bc_<book>`` from the pinned book map), with the read's
   per-PO claims (``bc_purchase.scope_claims``) as their relations' ``scopes``, projected
   into a throwaway store without the Portal; ``plx.process`` (``purchase_to_invoice``, the graph the
   ``three_way_match`` plan asks) answers from the root object through the case host's tool
   table, the path an MCP client uses. ``SharedPages`` answers a product listing whose
   request equals an oracle listing but for its ``$select`` from the oracle's whole
   records, trimmed to that ``$select`` as BC returns it, so the product sends only the
   reads the oracle does not make (invoices by ``orderId``, item masters). The oracle
   never reads the product's ``$select`` or a product-shaped response, so a field the
   product omits shows as a disagreement; a failed read is never kept, so an item blocked
   upstream stays blocked.
4. **Score** per item and per aspect: ``correct`` (every held decided aspect agrees),
   ``wrong`` (a decided aspect differs, or the product decided an aspect the oracle found
   unsupported: an incomplete read), ``abstain_ok`` (no wrong one, at least one left
   undecided by the product). Items not scored: ``blocked_upstream`` (a BC read failed, with
   HTTP status and error code), ``not_held`` (the product's store holds no root: unpinned
   company, projection gap; or a read the eval may not make: permission),
   ``product_error`` (``plx.process`` returned a tool error: never an abstention) and
   ``excluded`` by reason (PO no longer in BC, oracle decided nothing beyond the root;
   both known before the product read, which such an item never spends budget on).
   ``coverage`` (scored / sampled) is reported beside accuracy (correct / scored) and
   ``answered`` (oracle-held aspects beyond the root stage the product decided / held).
   **Root lookup** (``plx.find`` on the PO number) is scored separately and never counts as
   three-way success: the three-way answer is asked from the root the harness sampled.

**Cost** (``cost``, counts and seconds only): per phase (``sampling``, ``oracle``,
``oracle_excluded`` for items then excluded, ``product``, ``projection``) the wall seconds,
GETs sent and the seconds inside them, retries, failed attempts and their seconds, GETs
failed and GETs not sent; retries and failed-attempt seconds by cause (``transport``: no
HTTP status; ``http``: 429/502/503/504; ``retry_after``: one naming a wait); failed transport
attempts by class (``TRANSPORT_CLASSES``: a connect that timed out or failed, a read of the
answer that timed out or broke) and by entity set; and the failed attempts that ran past
the guarded sender's connect bound, a connect timeout only past it plus
``ATTEMPT_SLACK_SECONDS`` (``failed_attempts_over_bound``, the live check's bound; the slowest
stays recorded). Each GET attempt is timed from its send, the token POST before it apart. The
run's own counters measured live that the time grows with failed attempts (about 16 s
each), not with the number of reads. A company's entity set whose GETs fail for
availability is held for that company only: a throttle naming a wait (``Retry-After``;
beyond the backoff cap it is not retried) holds it until then, ``SET_FAILURE_BOUND`` failures in a row (a success
clears them) hold it for the rest of the run. A read of a timed hold waits it out when
the time left after the wait still covers the unread items (``RunCost.reserve``: per
item the priors weighed as ``PRIOR_ITEMS`` items beside the seconds of the items read,
plus one failed attempt), then is sent (``cost.holds``); otherwise, and for a set held
for the run, it is not sent and its item is ``blocked_upstream`` with the set's status
and a ``held.`` detail, so one long throttle never spends the time the rest of the run
needs.

The run passes when it completed inside its budget, at least ``--min-scored`` items were
scored, ``wrong <= floor(--max-wrong-rate * scored)``, at least one aspect beyond the root
stage was answered correctly (``no_correct_answer``: an all-abstain run never looks good), no
``plx.process`` call failed (``product_error``) and nothing leaked; otherwise exit 3
with ``eval.fail_reasons`` (a zero-scored run names its cause in ``eval.zero_scored_cause``).
Output: counts, enums, aspect names and hashes; never a number, id, citation or value
(``row_data_check``). Evidence label ``live-read``; the adapter is ``live-unverified`` until
the verifier records a pass. ``fixture`` runs nothing: tests inject a transport.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import random
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from evals.oracle import three_way
from evals.oracle.source import BcReads, ReadFailed, decimal_of, key_text
from integrations import bc_purchase, live_source_grammar as grammar
from integrations.bc_read import (RETRY_BUDGET_SECONDS, BusinessCentralReader, build_bc,
                                  load_company_books)
from integrations.capture_store import bc_unit
from integrations.live_source_guard import (BACKOFF_CAP_SECONDS, CONNECT_TIMEOUT_SECONDS,
                                            RETRY_STATUSES, BoundedBackoff,
                                            LiveSourceAudience, NotVisible, ReadOnlyGuardError,
                                            SourceUnavailable, failure_diagnostics,
                                            is_transport_failure, unavailable_detail)
from scripts.eval_unfamiliar_sample import (BudgetExhausted, Deadline, handle_identity,
                                            open_product, product_aspects, row_data_check)

SCHEMA = "plx-eval-bc-three-way/v1"
EVAL_PRINCIPAL = "eval:oracle"
EVAL_TENANT = "eval"
DOCUMENT_SETS = ("purchaseOrders", "purchaseReceipts", "purchaseInvoices")
LINE_SETS = {"purchaseOrders": "purchaseOrderLines", "purchaseReceipts": "purchaseReceiptLines",
             "purchaseInvoices": "purchaseInvoiceLines"}
SAMPLE_SELECT = {"purchaseOrders": ("number",), "purchaseReceipts": ("number", "orderNumber"),
                 "purchaseInvoices": ("number", "orderNumber")}
SAMPLE_ORDERBY = {"purchaseReceipts": "postingDate desc"}
OUTCOMES = ("correct", "wrong", "abstain_ok")
ROOT_STAGE = "stage:purchase_order"   # says only that the root exists: never scored alone
# Oracle reasons under which a decided product answer is unsupported (shown as wrong).
UNSUPPORTED = frozenset({"bc_read_incomplete"})
BLOCKED_KEYS = ("reason", "entity_set", "http_status", "error_code", "detail")
ROOT_LOOKUP = ("found_exact", "ambiguous", "wrong", "not_found", "error")
DEFAULT_COUNT = 15
DEFAULT_MIN_SCORED = 5
DEFAULT_MAX_WRONG_RATE = 0.10
# Measured on the live-results branch (eval.bc_three_way, 17 runs 2026-10-07/08): about 0.4 s
# per GET plus about 16 s per failed attempt (requests_sent - reads.sent - 1 retries; least
# squares), the failures rising from 0 to 24 per run with every BC check slowing alike. The
# slowest hour (10:33Z: 128 GETs, 24 failed attempts, exhausted 480 s at 14 of 15 items)
# replayed on an injected clock (tests/test_eval_bc_three_way.py) took 468 s for all 15
# when excluded items read no lines; since every PO's lines are read for the absent stages
# it takes 549 s, so 540 s became 600 s:
# about one more failed GET of margin.
DEFAULT_BUDGET = 600.0
DEFAULT_POOL = 200
# The product reads with the oracle's caps, so its listings can be served from the
# oracle's (``SharedPages``).
MAX_MATCHES = 50
LINE_READ = {"page_size": 100, "max_pages": 5, "max_items": 500}
# Consecutive availability failures (transport, 429/502/503/504) after which a company's
# entity set is held for the rest of the run.
SET_FAILURE_BOUND = 2
# Priors of ``RunCost.reserve``, from the slowest live hour (10:33Z): GETs per item, 119
# for 14 items, 8.5 then; every item now reads its PO lines for the absent stages (decision
# 2026-10-14-three-way-absence-proven): the replay spends 525 s on 15 items, 35 s or ~9.3
# GETs an item at GET_SECONDS, rounded up to 9.5; seconds per GET, 483 s over 128 GETs,
# failed attempts included. Still the slowest measured: the
# first run with a cost block in a slow hour (afddbcd, 2026-10-10 17:43Z, 25 failed
# attempts) spent 300 s on 15 items, 20 s an item against these 30. Their product weighs
# as ``PRIOR_ITEMS`` items read beside the items the run has read (decision
# 2026-10-10-followup-decisions 15, amended): an item that reads its receipts costs ~15
# times one the oracle excludes, so the first items read are no measure of the rest.
ITEM_GETS = 9.5
GET_SECONDS = 483 / 128
PRIOR_ITEMS = 3
ITEM_PHASES = ("oracle", "oracle_excluded", "product")
PHASES = ("sampling", "oracle", "oracle_excluded", "product", "projection")
RETRY_CAUSES = ("transport", "http", "retry_after")
# A failed attempt with no HTTP status, by where it failed (``transport_class``): urllib
# raises ``URLError`` for every failure before the request is sent (TCP connect, proxy
# CONNECT, TLS handshake), a connect timeout when it lasted the connect bound; any other
# transport failure (timeout, reset, close, body cut short) came while reading the answer.
TRANSPORT_CLASSES = ("connect_timeout", "connect_error", "read_timeout", "read_error",
                     "unclassified")
# Scheduling slack past the guarded sender's connect bound (CONNECT_TIMEOUT_SECONDS, read
# from integrations/live_source_guard.py) a connect timeout may take: live ones ended
# 0.07-0.10 s past it (results/8e61f782, 4 attempts at 10.07-10.10 s). Any other failed
# attempt past the bound (a read that broke or hung, a slow 429/503), or a connect timeout
# past both, counts in ``failed_attempts_over_bound``, which the live check bounds at 0.
ATTEMPT_SLACK_SECONDS = 0.5
COST_COUNTS = ("gets", "get_seconds", "retries", "failed_attempts", "failed_attempt_seconds",
               "failed_gets", "not_sent")
# Per phase beside COST_COUNTS: the slowest answered attempt (what a timeout must allow).
SLOWEST = "slowest_answer_seconds"


def blocked_detail(entity_set: str, exc: BaseException) -> dict[str, Any]:
    """What a blocked read may say: the entity set, its HTTP status, error-code token and
    fixed-vocabulary failure detail (``unavailable_detail``: a transport timeout or a
    malformed record has no HTTP status, and its cause must still show)."""
    found = failure_diagnostics(exc)
    if getattr(exc, "held", False):   # not sent: the set's own failure, marked held
        found["detail"] = f"held.{found['detail']}"
    return {"blocked": "upstream", "entity_set": entity_set,
            **{k: found[k] for k in ("http_status", "error_code", "detail")
               if found.get(k) is not None}}


def short_hash(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(map(str, parts)).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- sampling

def sample(reader: BusinessCentralReader, actor: Any, *, run_id: str, count: int,
           pool: int = DEFAULT_POOL, guard: Callable[[], None] = lambda: None
           ) -> dict[str, Any]:
    """Seeded (company id, PO number, drawn-from set) roots plus the per-set pool sizes and
    failures (entity set, HTTP status, error code). Raises on a failed company listing."""
    companies = [c["id"] for c in reader.companies(actor)["items"] if isinstance(c.get("id"), str)]
    rng = random.Random(hashlib.sha256(run_id.encode()).hexdigest())
    pools: dict[str, list[tuple[str, str]]] = {s: [] for s in DOCUMENT_SETS}
    failures: list[dict[str, Any]] = []
    no_order_number = 0
    for company in companies:
        for entity_set in DOCUMENT_SETS:
            guard()
            listing = None
            for orderby in dict.fromkeys((SAMPLE_ORDERBY.get(entity_set), None)):
                try:
                    listing = reader.list_entities(actor, company, entity_set, top=pool,
                                                   select=SAMPLE_SELECT[entity_set],
                                                   orderby=orderby, page_size=pool,
                                                   max_pages=1, max_items=pool)
                    break
                except (SourceUnavailable, NotVisible) as exc:
                    failures.append({"orderby": orderby is not None,
                                     **blocked_detail(entity_set, exc)})
            for record in (listing or {}).get("items", []):
                fields = record["fields"]
                number = key_text(fields.get("number" if entity_set == "purchaseOrders"
                                             else "orderNumber"))
                if number and grammar.bc_number_ok(number.strip()):
                    pools[entity_set].append((company, number.strip()))
                else:
                    no_order_number += 1
    for entity_set in DOCUMENT_SETS:
        pools[entity_set] = sorted(set(pools[entity_set]))
        rng.shuffle(pools[entity_set])
    drawn: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    cursors = {s: 0 for s in DOCUMENT_SETS}
    while len(drawn) < count and any(cursors[s] < len(pools[s]) for s in DOCUMENT_SETS):
        for entity_set in DOCUMENT_SETS:
            while cursors[entity_set] < len(pools[entity_set]) and len(drawn) < count:
                root = pools[entity_set][cursors[entity_set]]
                cursors[entity_set] += 1
                if root not in seen:
                    seen.add(root)
                    drawn.append({"company_id": root[0], "number": root[1],
                                  "from": entity_set})
                    break
    return {"companies": companies, "roots": drawn, "failures": failures,
            "pools": {s: len(pools[s]) for s in DOCUMENT_SETS},
            "no_order_number": no_order_number}


# --------------------------------------------------------------------------- oracle

class CompanyBcPort:
    """The oracle's BC port for one company (``evals.oracle.source.BcPort``), GET only."""

    def __init__(self, reader: BusinessCentralReader, actor: Any, company_id: str, *,
                 guard: Callable[[], None] = lambda: None) -> None:
        self.reader, self.actor, self.company_id, self._guard = reader, actor, company_id, guard
        # BcReads keeps only the error class; the port keeps what a blocked item may say.
        self.failure: dict[str, Any] | None = None
        self.budget_hit = False

    def guard(self) -> None:
        try:
            self._guard()
        except BudgetExhausted:
            self.budget_hit = True
            raise

    def _read(self, entity_set: str, call: Callable[[], Any]) -> Any:
        try:
            return call()
        except SourceUnavailable as exc:
            self.failure = blocked_detail(entity_set, exc)
            raise

    @staticmethod
    def _record(record: Mapping[str, Any]) -> dict[str, Any]:
        return {"company_id": record["company_id"], "entity_set": record["entity_set"],
                "id": record["id"], "fields": record["fields"],
                "citation": record["citation"]}

    def find(self, entity_set: str, field: str, value: str) -> dict[str, Any]:
        self.guard()
        found = self._read(entity_set, lambda: self.reader.find_by_number(
            self.actor, self.company_id, entity_set, value, field=field,
            max_matches=MAX_MATCHES))
        return {"records": [self._record(m["record"]) for m in found["matches"]],
                "complete": found["status"] not in ("truncated", "partially_withheld")}

    def lines(self, record: Mapping[str, Any]) -> dict[str, Any]:
        self.guard()
        listing = self._read(LINE_SETS[record["entity_set"]], lambda: self.reader.list_lines(
            self.actor, self.company_id, record["entity_set"], record["id"], **LINE_READ))
        done = listing["completeness"]
        return {"records": [self._record(r) for r in listing["items"]],
                "complete": not (done.get("truncated") or done.get("withheld_count")
                                 or done.get("provenance_dropped"))}

    def sample(self, entity_set: str, limit: int) -> dict[str, Any]:
        raise NotImplementedError("the BC three-way sample draws through sample()")


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


def _aspect(value: Any, reason: str, sources: Sequence[str]) -> dict[str, Any]:
    return {"value": value, "reason": reason, "sources": sorted(set(sources))}


def _item_lines(reads: BcReads, documents: Sequence[Mapping[str, Any]]
                ) -> tuple[dict[str | None, list[tuple[str | None, Any]]], bool]:
    """Item lines of ``documents`` by BC item No.: (unit, exact quantity) per line."""
    out: dict[str | None, list[tuple[str | None, Any]]] = {}
    complete = True
    for document in documents:
        lines, done = reads.lines(document)
        complete = complete and done
        for line in lines:
            fields = line["fields"]
            if fields.get("lineType") != "Item":
                continue
            out.setdefault(key_text(fields.get("lineObjectNumber")) or None, []).append(
                (key_text(fields.get("unitOfMeasureCode")) or None,
                 decimal_of(fields.get("quantity"))))
    return out, complete


def _line_absence(reads: BcReads, order: Mapping[str, Any], field: str, stage: str
                  ) -> tuple[str | None, str]:
    """BC proves ``stage`` absent from the PO's own lines (decision
    2026-10-14-three-way-absence-proven): BC raises ``receivedQuantity`` /
    ``invoicedQuantity`` of an order line when a receipt / an invoice of it posts, so a
    complete read whose every Item line records ``field`` 0 proves none posted. No Item
    line, an unreadable figure or a nonzero one (a partly received or invoiced line with
    no document found) stays undecided; the caller has checked the document reads."""
    lines, complete = reads.lines(order)
    if not complete:
        return None, "bc_read_incomplete"
    figures = [decimal_of(line["fields"].get(field)) for line in lines
               if line["fields"].get("lineType") == "Item"]
    if not figures:
        return None, "no_item_line"
    if any(f is None for f in figures):
        return None, f"{stage}_quantity_unreadable"
    if any(f != 0 for f in figures):
        return None, f"{stage}_quantity_recorded"
    return "absent", "absent_in_bc"


def oracle_reading(reads: BcReads, number: str) -> dict[str, Any]:
    """The oracle's reading of one BC PO (``three_way.ASPECTS``, see module docstring):
    ``{"status": "read"|"blocked"|"excluded", "reason", "aspects", "order"}``."""
    try:
        orders, orders_done = reads.find("purchaseOrders", "number", number)
        if len(orders) != 1:
            return {"status": "excluded", "aspects": {},
                    "reason": "order_not_in_source" if not orders else "order_not_unique"}
        order = orders[0]
        # The read time of the absences: taken before the first document read, so no
        # posting the reads could have missed is older than it.
        read_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        receipts, receipts_done = reads.find("purchaseReceipts", "orderNumber", number)
        invoices, invoices_done = reads.find("purchaseInvoices", "orderNumber", number)
        vendor = key_text(order["fields"].get("vendorNumber"))
        counted = [i for i in invoices
                   if _upper(i["fields"].get("status")) not in three_way.INVOICE_NOT_COUNTED]
        own = [i for i in counted if key_text(i["fields"].get("vendorNumber")) == vendor]
        # The PO's own lines are always read: their received / invoiced figures decide an
        # absent stage. The receipt and invoice lines only once a receipt is posted.
        ordered, ordered_done = _item_lines(reads, [order])
        received, received_done = _item_lines(reads, receipts)
        invoiced, invoiced_done = _item_lines(reads, own if receipts else [])
        receipt_absence = invoice_absence = (None, "bc_read_incomplete")
        if orders_done and not receipts and receipts_done:
            receipt_absence = _line_absence(reads, order, "receivedQuantity", "received")
        if orders_done and not counted and invoices_done:
            invoice_absence = _line_absence(reads, order, "invoicedQuantity", "invoiced")
    except ReadFailed as failed:
        if failed.reason == "NotVisible":   # a permission, not an upstream failure
            return {"status": "not_held", "aspects": {}, "reason": "oracle_not_permitted"}
        return {"status": "blocked", "aspects": {}, "reason": "oracle_read_failed",
                "blocked": "upstream", "entity_set": failed.target.removeprefix("bc:")}
    aspects: dict[str, dict[str, Any]] = {}
    status = _upper(order["fields"].get("status"))
    aspects[ROOT_STAGE] = _aspect(
        None if status in three_way.PO_IGNORED else
        ("absent" if status in three_way.PO_NOT_ISSUED else "present"),
        "po_cancelled" if status in three_way.PO_IGNORED else
        ("po_draft" if status in three_way.PO_NOT_ISSUED else "po_issued"), ["purchaseOrders"])
    if receipts:
        aspects["stage:receipt"] = _aspect("present", "bc_posted_receipt", ["purchaseReceipts"])
    else:
        aspects["stage:receipt"] = _aspect(*receipt_absence,
                                           ["purchaseReceipts", "purchaseOrderLines"])
    if own:
        aspects["stage:vendor_invoice"] = _aspect("present", "counted_invoice",
                                                  ["purchaseInvoices"])
    elif counted:
        aspects["stage:vendor_invoice"] = _aspect(None, "bc_vendor_unverifiable",
                                                  ["purchaseInvoices"])
    else:
        aspects["stage:vendor_invoice"] = _aspect(*invoice_absence,
                                                  ["purchaseInvoices", "purchaseOrderLines"])
    for name in ("stage:receipt", "stage:vendor_invoice"):
        if aspects[name]["value"] == "absent":
            aspects[name]["read_at"] = read_at
    if aspects["stage:receipt"]["value"] != "present":
        aspects["side:ordered_received"] = _aspect(None, "receipt_stage_not_present",
                                                   ["purchaseOrderLines",
                                                    "purchaseReceiptLines"])
    elif not (orders_done and receipts_done and ordered_done and received_done):
        aspects["side:ordered_received"] = _aspect(None, "bc_read_incomplete",
                                                   ["purchaseOrderLines",
                                                    "purchaseReceiptLines"])
    else:
        value, reason = three_way._side(ordered, received,
                                        lambda o, r: r + three_way.RECEIVE_EPS >= o)
        aspects["side:ordered_received"] = _aspect(value, reason, ["purchaseOrderLines",
                                                                   "purchaseReceiptLines"])
    if aspects["stage:receipt"]["value"] != "present" or \
            aspects["stage:vendor_invoice"]["value"] != "present":
        aspects["side:received_invoiced"] = _aspect(None, "stage_not_present",
                                                    ["purchaseReceiptLines",
                                                     "purchaseInvoiceLines"])
    elif not (receipts_done and invoices_done and received_done and invoiced_done):
        aspects["side:received_invoiced"] = _aspect(None, "bc_read_incomplete",
                                                    ["purchaseReceiptLines",
                                                     "purchaseInvoiceLines"])
    else:
        value, reason = three_way._side(received, invoiced, lambda r, i: r == i)
        aspects["side:received_invoiced"] = _aspect(value, reason, ["purchaseReceiptLines",
                                                                    "purchaseInvoiceLines"])
    return {"status": "read", "reason": "read", "aspects": aspects, "order": order}


# --------------------------------------------------------------------------- product

class SharedPages:
    """The run's ``SourceApi`` with whole-record listings kept: a listing without
    ``$select`` (the oracle's) is kept once read in full; a listing that equals a kept one
    but for its ``$select`` (the product's) is answered from it, each record trimmed to the
    ``$select`` names and annotations as BC returns it. A ``$select`` response is never
    kept, so the oracle never reads a product-shaped record; a failure is never kept.
    Everything else is the wrapped api (GET only)."""

    def __init__(self, api: Any) -> None:
        self._api = api
        self._kept: dict[Any, tuple[list[Any], dict[str, Any]]] = {}
        self.sent = self.shared = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._api, name)

    def get_pages(self, url: str, *, max_pages: int, max_items: int,
                  headers: Mapping[str, str] | None = None) -> tuple[list[Any], dict[str, Any]]:
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        select = [v for k, v in query if k == "$select"]
        key = (parts._replace(query=urlencode(sorted(q for q in query if q[0] != "$select")))
               .geturl(), max_pages, max_items, tuple(sorted((headers or {}).items())))
        if key in self._kept:
            self.shared += 1
            raws, completeness = copy.deepcopy(self._kept[key])
            if select:
                names = set(select[0].split(","))
                raws = [{k: v for k, v in raw.items() if k in names or k.startswith("@")}
                        for raw in raws]
            return raws, completeness
        self.sent += 1
        raws, completeness = self._api.get_pages(url, max_pages=max_pages,
                                                 max_items=max_items, headers=headers)
        if not select:
            self._kept[key] = copy.deepcopy((raws, completeness))
        return raws, completeness


class RecordingReader:
    """``BusinessCentralReader`` for the product read: the budget guard before each GET and
    the last source failure kept (``bc_purchase`` reports only a status), so a blocked
    item can name its HTTP status and error code."""

    def __init__(self, reader: BusinessCentralReader, guard: Callable[[], None]) -> None:
        self._reader, self._guard = reader, guard
        self.environment = reader.environment
        self.failure: dict[str, Any] | None = None

    def _call(self, name: str, target: str, *args: Any, **kwargs: Any) -> Any:
        self._guard()
        try:
            return getattr(self._reader, name)(*args, **kwargs)
        except SourceUnavailable as exc:
            self.failure = blocked_detail(target, exc)
            raise

    def companies(self, actor: Any, **kwargs: Any) -> Any:
        return self._call("companies", "companies", actor, **kwargs)

    def find_by_number(self, actor: Any, company_id: str, entity_set: str, *args: Any,
                       **kwargs: Any) -> Any:
        return self._call("find_by_number", entity_set, actor, company_id, entity_set, *args,
                          **kwargs)

    def list_lines(self, actor: Any, company_id: str, document_set: str, *args: Any,
                   **kwargs: Any) -> Any:
        return self._call("list_lines", LINE_SETS[document_set], actor, company_id,
                          document_set, *args, **kwargs)


def _line_row(line: Mapping[str, Any], document_id: str) -> dict[str, Any]:
    """The BC line row ``bc_purchase`` read (its selected fields, exact text kept)."""
    row: dict[str, Any] = {"id": line["id"], "documentId": document_id,
                           "sequence": line["sequence"], "lineType": line["line_type"],
                           "lineObjectNumber": line["item_number"], "itemId": line["item_id"],
                           "description": line["description"],
                           "unitOfMeasureCode": line["unit_of_measure_code"],
                           "expectedReceiptDate": line["expected_receipt_date"]}
    for entry in (*line["quantities"], line["unit_cost"], line["amount_excluding_tax"]):
        row[entry["field"]] = entry["value"]
    return row


class EvidenceCaptures:
    """``bc`` L1 captures (``ontology.project.CaptureSource``) of the documents and lines
    ``integrations.bc_purchase`` read and cited: one current version per record, lineage
    ``bc_<book>`` from the pinned book map (an unpinned company has no lineage and is not
    projected, so its roots are not held)."""

    source = "bc"

    def __init__(self, environment: str, books: Mapping[str, str] | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        if books is None:
            books = load_company_books().get(environment.lower(), {})
        self.environment = environment
        self.books = {str(k).lower(): v for k, v in books.items()}
        self.clock = clock
        self._rows: dict[str, dict[tuple[str, str], Any]] = {}
        self._scopes: dict[str, list[dict[str, Any]]] = {}

    def pinned(self, company_id: str) -> bool:
        return company_id.lower() in self.books

    def add(self, evidence: Mapping[str, Any]) -> None:
        for relation, claims in bc_purchase.scope_claims(evidence).items():
            self._scopes.setdefault(relation, []).extend(claims)
        for company in evidence.get("companies", []):
            for bucket in ("orders", "receipts", "invoices", "items"):
                for document in company[bucket]:
                    row = {"id": document["id"], "number": document["number"],
                           **document["header"], **document["dates"]}
                    self._put(document["company_id"], document["entity_set"], document["id"],
                              row, document["citation"], document["content_sha256"])
                    for line in (document.get("lines") or {}).get("items", []):
                        self._put(document["company_id"], line["entity_set"], line["id"],
                                  _line_row(line, document["id"]), line["citation"],
                                  line["content_sha256"])

    def _put(self, company: str, entity_set: str, record_id: str, row: Mapping[str, Any],
             citation: str, content_sha256: str | None) -> None:
        from ontology.project import CapturedRecord

        company = company.lower()
        now = self.clock().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        book = self.books.get(company)
        self._rows.setdefault(entity_set, {})[(company, str(record_id).lower())] = \
            CapturedRecord(source=self.source, relation=entity_set,
                           record_key=f"{company}/{str(record_id).lower()}", row=dict(row),
                           l1_handle=citation, field_prefix="row",
                           digest=content_sha256 or hashlib.sha256(
                               json.dumps(row, sort_keys=True).encode()).hexdigest(),
                           recorded_at=now, superseded_at=None, last_seen_at=now,
                           audience_key=bc_unit(company, entity_set),
                           capture_lineage=f"bc_{book}" if book else None)

    def records(self, relations: Sequence[str]) -> dict[str, list[Any]]:
        return {r: list(self._rows.get(r, {}).values()) for r in relations}

    def describe(self) -> dict[str, Any]:
        """Per relation read: refreshed now, never a full traversal (one PO's documents are
        not the whole entity set), so the product never takes this capture as complete;
        each read's per-PO claims are the relation's ``scopes``."""
        now = self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"generation": f"bc-purchase-evidence-{self.environment.lower()}",
                "relations": {r: {"last_refresh_at": now, "last_capture_id": None,
                                  "last_full_traversal_at": None,
                                  "scopes": self._scopes.get(r, [])}
                              for r in sorted({*self._rows, *self._scopes})}}


def _product_blocked(reader: RecordingReader) -> dict[str, Any]:
    return {**(reader.failure or {}), "blocked": "product_read_failed"}


def product_read(reader: RecordingReader, actor: Any, root: Mapping[str, str]
                 ) -> dict[str, Any]:
    """``bc_purchase`` evidence for one root, or ``{"blocked"|"not_held": reason}``."""
    reader.failure = None
    # Scoped reads: also the invoices by orderId and the item masters of the lines (the
    # projection joins each line to its Product by BC item No.), so the read's claims can
    # cover every hop of the PO's three-way sides.
    evidence = bc_purchase.read_purchase_evidence(
        reader, actor, root["number"], company_ids=[root["company_id"]],
        scoped_completeness=True, max_documents=MAX_MATCHES,
        line_page_size=LINE_READ["page_size"], line_max_pages=LINE_READ["max_pages"],
        line_max_items=LINE_READ["max_items"])
    states = [r["status"] for c in evidence["companies"] for r in c["reads"]]
    states += [d["lines"]["status"] for c in evidence["companies"]
               for b in ("orders", "receipts", "invoices") for d in c[b]]
    if "unavailable" in states:
        return _product_blocked(reader)
    if "not_permitted" in states:
        return {"not_held": "product_not_permitted"}
    return {"evidence": evidence}


def root_object(store_path: Path, citation: str) -> str | None:
    with closing(sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT object_id FROM object_versions WHERE l1_handle = ? "
                           "LIMIT 1", (citation,)).fetchone()
    return row[0] if row else None


def root_lookup(product: Any, number: str, identity: tuple[str, str] | None) -> str:
    """``plx.find`` on the PO number against the sampled root (reported apart)."""
    status, candidates, error = product.find(number)
    if error is not None:
        return "error"
    hits = [c for c in candidates if identity in product.identities(c)]
    orders = [c for c in candidates
              if any(i[0] == "bc:purchaseOrders" for i in product.identities(c))]
    if not hits:
        return "wrong" if candidates else "not_found"
    return "found_exact" if len(orders) == 1 else "ambiguous"


# --------------------------------------------------------------------------- scoring

def score(reading: Mapping[str, Any], answer: Mapping[str, Any]) -> dict[str, Any]:
    """One item from the oracle reading and the product's ``product_aspects`` answer."""
    aspects = reading["aspects"]
    per: dict[str, str] = {}
    for name in three_way.ASPECTS:
        truth, given = aspects[name], answer.get(name)
        if truth["value"] is None:
            if given is not None and truth["reason"] in UNSUPPORTED:
                per[name] = "unsupported"
            continue
        per[name] = ("undecided" if given is None else
                     "agreed" if given == truth["value"] else "disagreed")
    wrong = sorted(a for a, v in per.items() if v in ("disagreed", "unsupported"))
    outcome = ("wrong" if wrong else
               "abstain_ok" if "undecided" in per.values() else "correct")
    return {"outcome": outcome, "aspects": per, "wrong_aspects": wrong}


def _count(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        if row.get(key) is not None:
            out[str(row[key])] = out.get(str(row[key]), 0) + 1
    return dict(sorted(out.items()))


def eval_block(rows: Sequence[Mapping[str, Any]], *, min_scored: int, max_wrong_rate: float,
               incomplete: Sequence[str], run_blocked: Mapping[str, Any] | None
               ) -> dict[str, Any]:
    """Counts, coverage beside accuracy, per-aspect agreement, root lookup and the verdict."""
    status = _count(rows, "status")
    scored = [r for r in rows if r["status"] == "scored"]
    outcome = _count(scored, "outcome")
    n = len(scored)
    by_aspect = {a: {"agreed": 0, "disagreed": 0, "undecided": 0, "unsupported": 0}
                 for a in three_way.ASPECTS}
    for row in scored:
        for name, verdict in row["aspects"].items():
            by_aspect[name][verdict] += 1
    beyond_root = [v for a, v in by_aspect.items() if a != ROOT_STAGE]
    held = sum(sum(v.values()) for v in beyond_root)
    answered = held - sum(v["undecided"] for v in beyond_root)
    lookups = [r["root_lookup"] for r in rows if r.get("root_lookup")]
    wrong = outcome.get("wrong", 0)
    wrong_max = math.floor(max_wrong_rate * n)
    block: dict[str, Any] = {
        "sampled": len(rows), "blocked_upstream": status.get("blocked", 0),
        "not_held": status.get("not_held", 0), "excluded": status.get("excluded", 0),
        "product_error": status.get("product_error", 0),
        "scored": n, **{o: outcome.get(o, 0) for o in OUTCOMES},
        "coverage": f"{n}/{len(rows)}", "accuracy": f"{outcome.get('correct', 0)}/{n}",
        "answered": f"{answered}/{held}", "wrong_rate": f"{wrong}/{n}", "wrong_max": wrong_max,
        "wrong_within_bound": wrong <= wrong_max,
        "by_aspect": by_aspect,
        "not_held_by_reason": _count([r for r in rows if r["status"] == "not_held"], "reason"),
        "excluded_by_reason": _count([r for r in rows if r["status"] == "excluded"], "reason"),
        "product_error_by_code": _count([r for r in rows if r["status"] == "product_error"],
                                        "reason"),
        "blocked": {"by_reason": _count([r for r in rows if r["status"] == "blocked"],
                                        "reason"),
                    "by_entity_set": _count([r for r in rows if r["status"] == "blocked"],
                                            "entity_set"),
                    "by_http_status": _count([r for r in rows if r["status"] == "blocked"],
                                             "http_status"),
                    "by_error_code": _count([r for r in rows if r["status"] == "blocked"],
                                            "error_code"),
                    "by_detail": _count([r for r in rows if r["status"] == "blocked"],
                                        "detail")},
        "root_lookup": {"note": "separate from three-way success", "attempted": len(lookups),
                        **{k: lookups.count(k) for k in ROOT_LOOKUP}},
        "complete": not incomplete and run_blocked is None, "incomplete": list(incomplete)}
    reasons = []
    if run_blocked is not None:
        reasons.append("run_blocked_upstream")
        block["run_blocked"] = dict(run_blocked)
    if incomplete:
        reasons.append("incomplete")
    if n == 0:
        reasons.append("no_scored_items")
        unscored = {k: v for k, v in (("blocked_upstream", block["blocked_upstream"]),
                                      ("not_held", block["not_held"]),
                                      ("excluded", block["excluded"]),
                                      ("product_error", block["product_error"])) if v}
        block["zero_scored_cause"] = (
            "run_blocked_upstream" if run_blocked is not None else
            "nothing_sampled" if not rows else max(sorted(unscored), key=unscored.get))
    elif n < min_scored:
        reasons.append("below_min_scored")
    if n and not outcome.get("correct"):   # a correct item agrees beyond the root stage
        reasons.append("no_correct_answer")   # coverage alone is no result
    if block["product_error"]:
        reasons.append("product_error")
    if not block["wrong_within_bound"]:
        reasons.append("wrong_above_bound")
    block["fail_reasons"] = reasons
    block["pass"] = not reasons
    return block


def answer(pending: Sequence[tuple[dict[str, Any], Mapping[str, Any], Mapping[str, Any]]],
           captures: EvidenceCaptures, work_dir: Path, report: dict[str, Any]
           ) -> list[dict[str, Any]]:
    """Project the captured evidence and score each pending item from ``plx.process``."""
    from ontology import identifiers
    from ontology.project import build_store
    from ontology.schema import load_schema

    rows: list[dict[str, Any]] = []
    store = work_dir / "bc-three-way.sqlite"
    work_dir.mkdir(parents=True, exist_ok=True)
    build_store(store, load_schema(), {"bc": captures}, normalizer=identifiers.normalize)
    with open_product(store, tenant=EVAL_TENANT, principal=EVAL_PRINCIPAL,
                      info={"source": "bc_purchase"}) as product:
        report["product"] = {"status": product.info.get("status"),
                             "objects": product.info.get("objects", 0)}
        for row, root, reading in pending:
            order = reading["order"]
            object_id = root_object(store, order["citation"]) if product.call else None
            if object_id is None:
                rows.append({**row, "status": "not_held", "reason":
                             "root_not_projected" if captures.pinned(root["company_id"])
                             else "bc_company_unpinned"})
                continue
            row["root_lookup"] = root_lookup(product, root["number"],
                                             handle_identity(order["citation"]))
            result = product.call("plx.process", {"from": object_id,
                                                  "process": "purchase_to_invoice"})
            if "error" in result:
                rows.append({**row, "status": "product_error", "reason":
                             str((result["error"] or {}).get("code") or "error")[:40]})
                continue
            rows.append({**row, "status": "scored", **score(reading, product_aspects(result))})
    return rows


# --------------------------------------------------------------------------- run cost

class Span:
    """One stretch of the run attributed to a phase (``name`` may change before it ends)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.counts: dict[str, float] = {**dict.fromkeys(COST_COUNTS, 0), SLOWEST: 0.0}


class RunCost:
    """Where the run's time goes, from its own clock: per phase (``PHASES``) the wall
    seconds, ``COST_COUNTS`` and ``SLOWEST``; retries and failed-attempt seconds by cause
    (``RETRY_CAUSES``); GETs not sent per held entity set. Counts and seconds only."""

    def __init__(self, clock: Callable[[], float], budget_seconds: float | None = None) -> None:
        self.clock, self.budget_seconds = clock, budget_seconds
        self.started = clock()
        self.phases: dict[str, dict[str, float]] = {}
        for name in PHASES:
            self._total(name)
        self.retries = dict.fromkeys(RETRY_CAUSES, 0)
        self.failed_seconds = dict.fromkeys(RETRY_CAUSES, 0.0)
        self.slowest_failed_attempt = 0.0
        self.over_bound = 0
        self.transport = dict.fromkeys(TRANSPORT_CLASSES, 0)
        self.transport_by_set: dict[str, dict[str, int]] = {}
        self.retry_after_not_retried = 0
        self.slept = 0.0
        self.held: dict[str, int] = {}
        self.hold_waits, self.hold_wait_seconds = 0, 0.0
        self.items_read, self.items_unread = 0, 0
        self._items_seconds, self._item_waits = 0.0, 0.0
        self._item_mark: tuple[float, float] | None = None
        self._span: Span | None = None

    def _total(self, name: str) -> dict[str, float]:
        return self.phases.setdefault(name, {"seconds": 0.0, **Span(name).counts})

    @staticmethod
    def _add(target: dict[str, float], counts: Mapping[str, float]) -> None:
        for key, value in counts.items():
            target[key] = max(target[key], value) if key == SLOWEST else target[key] + value

    @contextmanager
    def phase(self, name: str) -> Iterator[Span]:
        span, started, outer = Span(name), self.clock(), self._span
        self._span = span
        try:
            yield span
        finally:
            self._span = outer
            total = self._total(span.name)
            total["seconds"] += self.clock() - started
            self._add(total, span.counts)

    def count(self, **counts: float) -> None:
        # A read outside every phase still shows, as ``unattributed``.
        self._add(self._span.counts if self._span is not None
                  else self._total("unattributed"), counts)

    def get(self, seconds: float, *, answer_seconds: float | None) -> None:
        """One GET sent: its seconds with every attempt and backoff; ``answer_seconds``,
        those of the attempt that answered (None: the GET failed)."""
        self.count(gets=1, get_seconds=seconds, failed_gets=int(answer_seconds is None),
                   **{SLOWEST: answer_seconds or 0.0})

    def failed_attempt(self, cause: str, seconds: float, *, transport: str | None = None,
                       entity_set: str = "") -> None:
        """One failed attempt; ``transport``: its class when it had no HTTP status."""
        self.count(failed_attempts=1, failed_attempt_seconds=seconds)
        self.failed_seconds[cause] += seconds
        self.slowest_failed_attempt = max(self.slowest_failed_attempt, seconds)
        slack = ATTEMPT_SLACK_SECONDS if transport == "connect_timeout" else 0.0
        self.over_bound += seconds > CONNECT_TIMEOUT_SECONDS + slack
        if transport is not None:
            self.transport[transport] += 1
            by_class = self.transport_by_set.setdefault(entity_set, {})
            by_class[transport] = by_class.get(transport, 0) + 1

    def retried(self, cause: str) -> None:
        self.count(retries=1)
        self.retries[cause] += 1

    def not_sent(self, entity_set: str) -> None:
        self.count(not_sent=1)
        self.held[entity_set] = self.held.get(entity_set, 0) + 1

    def reserve(self) -> float:
        """Seconds the unread items (the one being read included) need: per item, the
        priors' ``ITEM_GETS`` x ``GET_SECONDS`` weighted as ``PRIOR_ITEMS`` items beside the
        seconds of the items read (every attempt and backoff, not the throttle waits), less
        what the one being read has spent, plus one more failed attempt at the slowest
        measured: a mean is no bound, and the run's time grows by them."""
        if not self.items_unread:
            return 0.0
        per_item = ((PRIOR_ITEMS * ITEM_GETS * GET_SECONDS + self._items_seconds)
                    / (PRIOR_ITEMS + self.items_read))
        spent = 0.0
        if self._item_mark is not None:
            started, waits = self._item_mark
            spent = min(per_item, self.clock() - started - (self._item_waits - waits))
        return self.items_unread * per_item - spent + self.slowest_failed_attempt

    def items(self, read: int, unread: int) -> None:
        """Between items: ``read`` items are read (their GETs complete), ``unread`` are not."""
        self.items_read, self.items_unread = read, unread
        self._items_seconds = (sum(self.phases[name]["seconds"] for name in ITEM_PHASES)
                               - self._item_waits)
        self._item_mark = (self.clock(), self._item_waits)

    def waited(self, seconds: float) -> None:
        self.hold_waits += 1
        self.hold_wait_seconds += seconds
        if self._span is not None and self._span.name in ITEM_PHASES:
            self._item_waits += seconds

    def report(self) -> dict[str, Any]:
        def r(value: float) -> float:
            return round(value, 2)

        return {"budget_seconds": self.budget_seconds,
                "elapsed_seconds": r(self.clock() - self.started),
                "phases": {name: {k: r(v) if isinstance(v, float) else v
                                  for k, v in counts.items()}
                           for name, counts in self.phases.items()},
                "retries": {"total": sum(self.retries.values()),
                            "by_cause": dict(self.retries),
                            "failed_attempt_seconds_by_cause": {
                                k: r(v) for k, v in self.failed_seconds.items()},
                            "slowest_failed_attempt_seconds": r(self.slowest_failed_attempt),
                            "failed_attempt_bound_seconds": CONNECT_TIMEOUT_SECONDS,
                            "connect_timeout_slack_seconds": ATTEMPT_SLACK_SECONDS,
                            "failed_attempts_over_bound": self.over_bound,
                            "transport_by_class": dict(self.transport),
                            "sleep_seconds": r(self.slept),
                            "retry_after_not_retried": self.retry_after_not_retried},
                "holds": {"waited": self.hold_waits,
                          "wait_seconds": r(self.hold_wait_seconds)},
                "held_not_sent": dict(sorted(self.held.items())),
                "transport_failed_by_set": {k: dict(sorted(v.items())) for k, v
                                            in sorted(self.transport_by_set.items())}}


class RunBackoff(BoundedBackoff):
    """The BC api's bounded retry (``BoundedBackoff``, sleep budget ``RETRY_BUDGET_SECONDS``)
    with the run's clock: each failed attempt's seconds and each retry go to ``cost`` by
    cause (one with no HTTP status also by ``transport_class`` and entity set). A throttle
    naming a wait beyond ``BACKOFF_CAP_SECONDS`` is not retried (a retry sooner is throttled
    again); ``MeteredApi`` holds its set for that wait. Each GET attempt (every page and
    document read alike) is timed from ``sent`` to its failure or ``answered``, never the
    token POST before it (``build_reader``); ``slowest_answer`` is the slowest answered
    attempt since its holder last set it to 0."""

    def __init__(self, cost: RunCost, *, sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(sleep=sleep, budget_seconds=RETRY_BUDGET_SECONDS)
        self.cost = cost
        self._mark = cost.clock()
        self._entity_set = ""
        self.slowest_answer = 0.0

    def sent(self, url: str) -> None:
        """A GET attempt of ``url`` is sent now."""
        self._mark = self.cost.clock()
        self._entity_set = entity_set_of(url)

    def answered(self) -> None:
        """The attempt sent last answered with a status that is not retried."""
        self.slowest_answer = max(self.slowest_answer, self.cost.clock() - self._mark)

    @staticmethod
    def _cause(headers: Mapping[str, str] | None) -> str:
        if headers is None:
            return "transport"
        named = {str(k).lower() for k in headers}
        return "retry_after" if "retry-after" in named else "http"

    def may_retry(self, attempt: int, headers: Mapping[str, str] | None = None) -> bool:
        # Called once after every retryable failed attempt: that attempt ends now.
        seconds, cause = self.cost.clock() - self._mark, self._cause(headers)
        # SourceApi asks this inside the ``except`` of a failure with no status: the
        # exception being handled is that attempt's.
        transport = transport_class(sys.exc_info()[1], seconds) if cause == "transport" \
            else None
        self.cost.failed_attempt(cause, seconds, transport=transport,
                                 entity_set=self._entity_set)
        lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
        wait = self.retry_after(lowered.get("retry-after"))
        if wait is not None and wait > BACKOFF_CAP_SECONDS:
            self.cost.retry_after_not_retried += 1
            return False
        return super().may_retry(attempt, headers)

    def pause(self, attempt: int, headers: Mapping[str, str] | None = None) -> float:
        self.cost.retried(self._cause(headers))
        delay = super().pause(attempt, headers)
        self.cost.slept += delay
        return delay


def transport_class(exc: BaseException | None, seconds: float) -> str:
    """One of ``TRANSPORT_CLASSES`` for a failed attempt with no HTTP status, from the
    guard's fixed-vocabulary diagnosis of ``exc`` and the attempt's ``seconds``."""
    detail = unavailable_detail(exc) if exc is not None and is_transport_failure(exc) else None
    if detail == "urlerror":
        return "connect_timeout" if seconds >= CONNECT_TIMEOUT_SECONDS else "connect_error"
    if detail == "timeout":
        return "read_timeout"
    return "unclassified" if detail in (None, "unclassified") else "read_error"


def entity_set_of(url: str) -> str:
    """The entity set a BC GET reads: the last path segment without its key."""
    return unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1].split("(", 1)[0]


def hold_key(url: str) -> tuple[str, str]:
    """What a hold covers: the company a BC GET reads in ("" for the company listing)
    and its entity set."""
    path = unquote(urlsplit(url).path)
    company = path.split("/companies(", 1)[1].split(")", 1)[0] if "/companies(" in path else ""
    return company.strip("'").lower(), entity_set_of(url)


class MeteredApi:
    """The run's ``SourceApi`` (GET only) with every GET timed into ``RunCost`` and a
    company's entity set held after availability failures (``hold_key``): a throttle's
    ``Retry-After`` holds it until then, ``SET_FAILURE_BOUND`` failures in a row hold it for
    the rest of the run; a success clears its streak. A read of a timed hold sleeps it out
    when the time ``deadline`` leaves after it still covers ``RunCost.reserve`` and is then
    sent, so at most ``SET_FAILURE_BOUND`` sends per read; any other read of a held set is
    not sent: it raises a copy of the set's last failure marked ``held``."""

    def __init__(self, api: Any, cost: RunCost, backoff: RunBackoff, deadline: Deadline,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._api, self._cost, self._backoff = api, cost, backoff
        self._deadline, self._sleep = deadline, sleep
        self._streak: dict[tuple[str, str], int] = {}
        self._held: dict[tuple[str, str], tuple[float | None, SourceUnavailable]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._api, name)

    def _wait_out(self, until: float) -> bool:
        """Sleep until ``until`` when the time left after it still covers the unread items
        at the measured cost (``RunCost.reserve``); False otherwise."""
        wait = until - self._cost.clock()
        if wait <= 0:
            return True
        if wait + self._cost.reserve() >= self._deadline.remaining():
            return False
        self._sleep(wait)
        self._cost.waited(wait)
        return True

    def get_pages(self, url: str, *, max_pages: int, max_items: int,
                  headers: Mapping[str, str] | None = None) -> tuple[list[Any], dict[str, Any]]:
        key = hold_key(url)
        held = self._held.get(key)
        if held is not None and (held[0] is None or not self._wait_out(held[0])):
            self._cost.not_sent(key[1])
            refusal = copy.copy(held[1])
            refusal.held = True
            raise refusal
        while True:
            now = self._cost.clock()
            self._backoff.slowest_answer = 0.0   # over every page of this read
            try:
                result = self._api.get_pages(url, max_pages=max_pages, max_items=max_items,
                                             headers=headers)
            except SourceUnavailable as exc:
                done = self._cost.clock()
                self._cost.get(done - now, answer_seconds=None)
                if (is_transport_failure(exc)
                        or getattr(exc, "http_status", None) in RETRY_STATUSES):
                    streak = self._streak[key] = self._streak.get(key, 0) + 1
                    wait = getattr(exc, "retry_after_seconds", None)
                    if streak >= SET_FAILURE_BOUND:
                        self._held[key] = (None, exc)
                    elif wait:
                        self._held[key] = (done + wait, exc)
                        if self._wait_out(done + wait):
                            continue
                raise
            self._cost.get(self._cost.clock() - now,
                           answer_seconds=self._backoff.slowest_answer)
            self._streak.pop(key, None)
            self._held.pop(key, None)
            return result


# --------------------------------------------------------------------------- run

def run(reader: BusinessCentralReader, *, run_id: str, count: int, work_dir: Path,
        deadline: Deadline, evidence_label: str, min_scored: int = DEFAULT_MIN_SCORED,
        max_wrong_rate: float = DEFAULT_MAX_WRONG_RATE, pool: int = DEFAULT_POOL,
        books: Mapping[str, str] | None = None, cost: RunCost | None = None
        ) -> dict[str, Any]:
    from ontology.core import Actor

    actor = Actor(EVAL_TENANT, frozenset({EVAL_PRINCIPAL}), "restricted")
    cost = cost or RunCost(time.monotonic)
    incomplete: list[str] = []
    rows: list[dict[str, Any]] = []
    secrets: list[str] = []
    report: dict[str, Any] = {"schema": SCHEMA, "run_id": run_id, "source": "business-central",
                              "evidence_label": evidence_label,
                              "adapter_status": "live-unverified", "values_published": False,
                              "read_only_guard": "GET-only; token POST only",
                              "environment_is_production":
                                  str(reader.environment).lower() == "production"}
    cost.items_unread = count   # a wait while sampling must leave the items their time
    try:
        with cost.phase("sampling"):
            drawn = sample(reader, actor, run_id=run_id, count=count, pool=pool,
                           guard=deadline.guard)
    except (SourceUnavailable, NotVisible) as exc:
        report["eval"] = eval_block([], min_scored=min_scored, max_wrong_rate=max_wrong_rate,
                                    incomplete=[], run_blocked=blocked_detail("companies", exc))
        report["cost"] = cost.report()
        return report
    except BudgetExhausted:
        drawn = {"companies": [], "roots": [], "failures": [], "pools": {},
                 "no_order_number": 0}
        incomplete.append("run_budget_exhausted")
    captures = EvidenceCaptures(str(reader.environment), books)
    product_reader = RecordingReader(reader, deadline.guard)
    pending: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for done, root in enumerate(drawn["roots"]):
        cost.items(done, len(drawn["roots"]) - done)
        row = {"item": short_hash(root["company_id"].lower(), root["number"]),
               "from": root["from"]}
        secrets += [root["number"], root["company_id"]]
        port = CompanyBcPort(reader, actor, root["company_id"], guard=deadline.guard)
        try:
            with cost.phase("oracle") as span:
                reading = oracle_reading(BcReads(port), root["number"])
                if reading["status"] == "read" and not [
                        a for a in three_way.decided(reading) if a != ROOT_STAGE]:
                    # nothing to score: the product read would only spend the run's budget
                    reading = {"status": "excluded", "reason": "oracle_undecided"}
                if reading["status"] == "excluded":
                    span.name = "oracle_excluded"
            if reading["status"] == "blocked":
                if port.budget_hit:
                    raise BudgetExhausted("run_budget_exhausted")
                reading.update(port.failure or {})
                rows.append({**row, "status": "blocked",
                             **{k: v for k, v in reading.items() if k in BLOCKED_KEYS}})
                continue
            if reading["status"] in ("excluded", "not_held"):
                rows.append({**row, "status": reading["status"], "reason": reading["reason"]})
                continue
            secrets += [reading["order"]["id"], reading["order"]["citation"]]
            with cost.phase("product"):
                got = product_read(product_reader, actor, root)
        except BudgetExhausted:
            incomplete.append("run_budget_exhausted")
            break
        if "blocked" in got:
            rows.append({**row, "status": "blocked", "reason": got["blocked"],
                         **{k: v for k, v in got.items() if k in BLOCKED_KEYS[1:]}})
        elif "not_held" in got:
            rows.append({**row, "status": "not_held", "reason": got["not_held"]})
        else:
            captures.add(got["evidence"])
            pending.append((row, root, reading))
    cost.items_unread = 0   # every item is read (or the budget cut the run)
    report["sample"] = {"companies": len(drawn["companies"]), "pools": drawn["pools"],
                        "drawn": len(drawn["roots"]), "count": count,
                        "drawn_from": _count(drawn["roots"], "from"),
                        "no_order_number": drawn["no_order_number"],
                        "pool_failures": drawn["failures"]}
    if pending:
        with cost.phase("projection"):
            rows += answer(pending, captures, work_dir, report)
    report["eval"] = eval_block(rows, min_scored=min_scored, max_wrong_rate=max_wrong_rate,
                                incomplete=incomplete, run_blocked=None)
    report["items"] = rows
    report["items_digest"] = short_hash(*sorted(r["item"] for r in rows))
    report["cost"] = cost.report()
    report["row_data_check"] = row_data_check(json.dumps(report), secrets)
    if report["row_data_check"]["leaked"]:
        report["eval"]["fail_reasons"].append("values_leaked")
        report["eval"]["pass"] = False
    return report


def build_reader(environ: Mapping[str, str], cost: RunCost, deadline: Deadline, *,
                 sender: Any = None, sleep: Callable[[float], None] = time.sleep
                 ) -> tuple[BusinessCentralReader, Any, SharedPages]:
    backoff = RunBackoff(cost, sleep=sleep)
    api, transport, tenant_id, environment = build_bc(environ, sender=sender, retry=backoff)
    request = transport.request

    def timed_request(method: str, url: str, headers: Mapping[str, str],
                      **kwargs: Any) -> tuple[int, Mapping[str, str], bytes]:
        # Every GET attempt (each page of ``get_pages``, ``get_document`` alike) is timed at
        # its send; the token POST that may precede it is no part of it.
        if method.upper() != "GET":
            return request(method, url, headers, **kwargs)
        backoff.sent(url)
        answer = request(method, url, headers, **kwargs)
        if answer[0] not in RETRY_STATUSES:
            backoff.answered()
        return answer
    transport.request = timed_request
    audience = LiveSourceAudience({f"bc:{environment.lower()}:*": [EVAL_PRINCIPAL]},
                                  tenant=EVAL_TENANT)
    pages = SharedPages(MeteredApi(api, cost, backoff, deadline, sleep))
    return BusinessCentralReader(pages, audience, tenant_id=tenant_id,
                                 environment=environment), transport, pages


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None, books: Mapping[str, str] | None = None,
         clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep) -> int:
    """``clock`` and ``sleep`` are the run's time (tests inject both with the sender)."""
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_bc_three_way",
                                     description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    live = commands.add_parser("live", help="credentialed, BC GET only; counts only")
    live.add_argument("--work-dir", type=Path, required=True,
                      help="throwaway directory for the product's projection store")
    live.add_argument("--run-id", default="auto",
                      help="sampling seed; 'auto' = live-<UTC hour> (a new sample each hour)")
    live.add_argument("--count", type=int, default=DEFAULT_COUNT)
    live.add_argument("--pool", type=int, default=DEFAULT_POOL,
                      help="documents listed per BC company and document set (1..1000)")
    live.add_argument("--min-scored", type=int, default=DEFAULT_MIN_SCORED)
    live.add_argument("--max-wrong-rate", type=float, default=DEFAULT_MAX_WRONG_RATE)
    live.add_argument("--total-budget-seconds", type=float, default=DEFAULT_BUDGET)
    args = parser.parse_args(argv)
    if args.min_scored > args.count:
        parser.error("--min-scored exceeds --count: the check could never pass")
    env = os.environ if environ is None else environ
    run_id = args.run_id if args.run_id != "auto" else \
        "live-" + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    try:
        cost = RunCost(clock, args.total_budget_seconds)
        deadline = Deadline(args.total_budget_seconds, clock)
        reader, transport, pages = build_reader(env, cost, deadline, sender=sender,
                                                sleep=sleep)
        report = run(reader, run_id=run_id, count=args.count, work_dir=args.work_dir,
                     deadline=deadline,
                     evidence_label="injected-transport" if sender else "live-read",
                     min_scored=args.min_scored, max_wrong_rate=args.max_wrong_rate,
                     pool=args.pool, books=books, cost=cost)
        report["requests_sent"] = transport.requests_sent
        report["reads"] = {"sent": pages.sent, "shared": pages.shared}
    except ReadOnlyGuardError:
        print(json.dumps({"error": "read_only_guard"}), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - a typed error line, never a value
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report["eval"]["pass"] else 3


__all__ = ["EvidenceCaptures", "main", "oracle_reading", "root_lookup", "run", "sample",
           "score"]


if __name__ == "__main__":
    raise SystemExit(main())
