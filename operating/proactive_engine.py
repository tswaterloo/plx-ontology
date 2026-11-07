"""Proactive engine: scheduled, set-based gap detection over the whole projection store,
and a persisted per-employee gap inbox the console Home reads.

The detectors read the store with SQL over all its current records (never one object
read at a time), so ``scanned`` is the whole store; each says ``complete``, ``truncated``,
``blocked`` (a source the rule needs is not served), ``not_configured`` or ``failed``:

* ``late_or_unconfirmed_po``: a purchase order open by the one PO status definition
  (``operating.po_status``: each settled record by its own source, the Portal header word
  with its own lines' summed quantities, a BC order's fullyReceived; an unknown status,
  a Sage-only or FileMaker-only order included, is counted ``not_evaluated`` by its
  reason, never open) judged on its requested-by date (its ``expected_date``; none is
  ``no_need_date``, never a line's or the supplier's date in its place): past it while
  open is late (``late_po`` ``overdue``, or ``unconfirmed_po`` when no supplier
  confirmation is linked); not yet past it with a settled confirmation after it is at
  risk (``late_po`` ``confirmed_after_need_date``). A confirmation linked only as a lead
  (when not already late), two need dates or two statuses across one order's settled
  identities are counted ``not_evaluated``, never raised; the result's ``counts`` (and
  the run summary's ``po_lateness``) say how many were evaluated, late and at risk;
* ``systems_disagree`` (raised by ``late_or_unconfirmed_po``): two linked records of one
  purchase order disagree (``po_status.read``'s conflicts: receipt state, received
  quantity, requested-by date), ONE gap per pair and field naming both values and who
  governs; each side's citations are their own group, so a reader of one side never
  sees it. A BC side that governs receipt state also joins the late gap's evidence;
* ``late_customer_order``: a customer order open past its own required date, ONE gap per
  order across its records (settled ``SalesOrder`` identity links only): open when every
  record stating a status is open by its own system's word, closed when any record is
  closed (a closed word, BC fully shipped or kept only as posted documents) or a settled
  shipment or invoice is linked to any; an unknown word, no status, a lead twin that is
  closed or a lead shipment is ``not_evaluated``, never open. Its required date must be a
  requested one (``operating.source_defaults``: never a bulk default of its source nor the
  record's own creation or order day; such an order is undated, never late). Cited by the
  date and each status value (each its own group); it rests on no closure being recorded,
  so it is a ``REQUIRES_GRANT`` rule (every audience of orders, shipments and invoices);
* ``po_receipt_quantity_mismatch``: the received quantity of a PO line (its receipt
  lines through settled ``receives_line`` links, one quantity per receipt line identity)
  above the ordered one, or below it on an order recorded as fully received (closed
  ``RECEIVED`` by the one PO status definition); a unit that
  differs, a disputed quantity or a lead-only link is ``not_evaluated``;
* ``receipt_without_qc``: a received receipt with no QC record linked to it or its
  lines. ``blocked`` while the schema declares no QC link (the QC relations are not
  served: upstream ask Q3) unless the caller names the link types;
* ``order_without_customer_po``: the orders ``operating.customer_po_coverage``'s full
  mirror scan classifies ``absent_in_this_source`` (no linked FM row holding one), mapped
  to their projected record; ``complete`` only when that scan was;
* ``late_order``: ``operating.proactive_gaps``' late-order rule over every purchase order
  line (expected receipt date before ``as_of``, received below ordered; one cited value of
  each, else the line is ``not_evaluated``), ONE gap per order through its settled
  ``line_of`` links, with the never-sent supplier draft (``follow_up.draft``,
  ``late_order_draft``): the order's number, its late lines and the governing date (the
  supplier's confirmed delivery date through a settled ``SupplierConfirmation.confirms``,
  else "our expected date"), each fact in a side list of citations. A reader who may not
  open every fact the draft cites gets the gap without the draft; the console words it
  again for the person who proposes it (``order_draft``) and records it on the case;
* ``expired_quote`` and ``conflicting_quantity``: ``operating.proactive_gaps``' rules over
  every quote and every quantity of each settled identity cluster;
* ``orders_at_risk``: ``operating.impact_chain``'s walk from every late or slipping
  purchase order (``plx.process``'s own verdict, asked through ``call``) over recorded
  links only to the customer orders it feeds: ONE gap per order at risk (days at risk
  above 0; days at risk is a lower bound, no assembly lead time is recorded), citing the PO's lateness
  dates (each its own group), every record of the chain (each its own group, so a reader
  who may not open one link of the chain never sees the gap) and the order's required
  date; never a value. The verdict is the run principal's: it also rests on what the
  receipt and promise sources do NOT hold, so the gap further requires (``gap_requires``)
  a grant on every source audience the ``po_delivery_window`` graph reads
  (``_lateness_audiences``): a reader whose own verdict would be withheld never sees it.
  The summary's ``top_finding`` says "N customer orders at risk from M late POs, worst
  first" (counts only). With the run's process pool every PO reaching an assembly is
  asked (no ``card_bound``); one the budget did not reach is counted ``time_budget`` in
  the rule's coverage. Where ``plx.process`` judged the PO per line, the line feeding the
  order decides by its own promise (``impact_chain.Late.material``): the gap cites the
  confirmation's per-item date as its own group and its event names the line, how it was
  matched, its promised date and the order's required date. A gap whose date is a
  supplier's promise tells a reader who may not read every audience a promise sits in
  (supplier confirmations, mailboxes), or every reader when the run's own promises were
  not proven complete, that a later promise it cannot see may exist (``read_inbox``; the
  audiences are never shown). ``not_configured`` without a caller;
* the proactive events (``operating.proactive_events``): ``eta_slipped``,
  ``lot_attention`` (``lot_near_expiry``, ``lot_on_hold``), ``accepted_quote_no_order``,
  ``invoice_quantity_mismatch`` and ``vendor_no_reply`` (F28's question, set-based over
  the store's mail, people cleared first) over the store, and ``vendor_terms_unapplied``
  (F27) and ``negative_margin`` (F10/F31) through the caller's ``plx.investigate``,
  bounded, or over every candidate on the run's process pool (``run(pool=)``,
  ``operating.proactive_pool``). Each rule states its ``coverage`` (records judged of
  those it should judge, the rest by reason; the summary's ``coverage``). A gap about a purchase order, sales order, quote or
  purchase invoice carries the record's cited total and currency as its ranking amount
  (``_with_amounts``), shown only to a reader who may open both; ``operating.briefing``
  ranks and routes them per reader;
* ``card_rules``: ``missing_in_record`` and attested conflicts from the ``plx.object``
  cards of the documents that attest a record (bounded per-object reads, ``CARD_BOUND``).

A gap carries the citations its claim rests on, grouped (each group must stay cited for a
reader to see the gap), a stable id and a proposed ``follow_up``
(``operator_ui.object_workspace.FOLLOW_UP_FIELDS``) that only a person sends. ``run``
writes one inbox file next to the store: a gap found again keeps its id and first sight,
a gap a complete detector no longer finds is ``resolved`` (found again: reopened), and an
acknowledgement is per reader. ``read_inbox`` trims for one reader: a gap shows only when
its subject and at least one citation of every group sit in audiences the reader is
granted, at markings it holds, and pass the host's per-item decision (a gap of a
``REQUIRES_GRANT`` detector also needs its recorded audience requirements, every one
granted; none recorded: never shown); the counts it
returns are over what that reader may read.

``refresh`` is the full scheduled run (every detector: the customer PO coverage scan and
the tool caller's ``plx.process``/``plx.object`` reads as the run principal), shared by
``main`` and the host's post-build hook (``after_build(scheduled=)``), within a time
budget (``REFRESH_BUDGET_SECONDS``): a detector the budget stops is ``truncated`` with
reason ``time_budget``, never ``complete``, and leaves its gaps as they were.

``python3 -m operating.proactive_engine`` is the live check's command
(live check ``case.proactive_engine``): one host capture cycle, the engine
over its projection, then the inbox read per reader; counts and enums only. Evidence
label: that of the captures it reads (live-unverified until the verifier runs it).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from ontology import store as ostore
from ontology.units import unit_token

from . import po_status
from . import test_records as test_records_rule
from . import proactive_gaps as pg
from . import sales_order_status as so_status
from . import suspect_money

SCHEMA = "plx-proactive-engine/v1"
INBOX_SCHEMA = "plx-gap-inbox/v1"
MAX_SHOWN = 100
CARD_BOUND = 500
VALUES_CHUNK = 500   # object ids per value read (``_Store.values``)
SCOPE_CHUNK = 500    # object ids per scoped list or link read (an incremental pass)
# The link scan (``_Store.links``): threads (every core unless ``SCAN_THREADS_ENV`` says),
# rowid ranges per thread (a slow range is evened out by the others), the table size from
# which it splits, and the link rows a run keeps for the rules after it (~1 GB).
SCAN_THREADS_ENV = "PLX_PROACTIVE_SCAN_THREADS"
LINK_PARTS_PER_THREAD = 4
PARALLEL_LINK_ROWS = 200_000
LINK_CACHE_ROWS = 3_000_000
LINK_COLUMNS = ("link_id", "link_type", "from_object", "to_object", "tier", "markings")
KEEP_RUNS = 100
# The host cycle's full refresh (``after_build(scheduled=)``) starts no detector and makes
# no tool call after this many seconds: the cycle lock is not held unboundedly. Sized for a
# ~2.5M-object store (store-only detectors alone ~4 min there) next to a 1-4 h build.
REFRESH_BUDGET_SECONDS = 1500
# Of it, kept for the amounts and their plausibility check after the detectors (about 5 s
# warm and 50 s cold on the live store), so they too end within the bound.
AMOUNTS_RESERVE_SECONDS = 120
TIME_BUDGET = "time_budget"
SETTLED = ("verified", "reviewed")
CONFIRMING = ("verified", "reviewed", "corroborated")
QC_TYPES = ("RelPublicAppIncomingQcRecord", "QualityRecord")
PRIORITY = ("high", "medium", "low")
# The order they start in: the store-only checks (the proactive events among them) side by
# side on threads (``run(threads=)``), the tool-call rules one after another on the run's
# own thread; each in its own time window (``DETECTOR_SECONDS``), so a slow one never
# leaves another unrun.
# An open purchase order with no requested-by date: never late, its own Home group.
UNDATED_KIND = "undated_po"
LATE_RULE = "late_or_unconfirmed_po"   # the rule Home's late count is read from
DETECTORS = ("late_or_unconfirmed_po", "late_customer_order",
             # the store-only proactive events (``operating.proactive_events``)
             "eta_slipped", "lot_attention", "accepted_quote_no_order",
             "invoice_quantity_mismatch",
             "po_receipt_quantity_mismatch", "receipt_without_qc", "expired_quote",
             "conflicting_quantity", "late_order", "order_without_customer_po",
             # unanswered vendor requests over the whole store's mail (after the late rule:
             # it judges the orders that rule finds open)
             "vendor_no_reply",
             # the rules that make tool calls, the last three proactive events among them:
             # the quick margins first (what they leave carries on), the customer orders a
             # late PO hits next, the attested-document cards last
             "negative_margin", "orders_at_risk", "vendor_terms_unapplied",
             "card_rules")
# Each detector's own time window (seconds from its start, never past the run's deadline,
# ``Budget.open``): a detector that reaches it is ``truncated`` (``time_budget``) and the
# others still run. Measured on the live store (2.5M objects, 2026-11-06, a loaded host):
# every store-only check under 3 min (the quantity conflicts ~2 min), so 10 min each is
# room; the tool-call rules read on the process pool, the customer orders a late PO hits
# weighing most (the briefing's first criterion); the margins' reads start with the run
# (``_margin_head_start``), so their window is what is left of them at their turn.
DETECTOR_SECONDS_DEFAULT = 600.0
DETECTOR_SECONDS = {"conflicting_quantity": 900.0, "negative_margin": 900.0,
                    "orders_at_risk": 1500.0, "vendor_terms_unapplied": 600.0,
                    "card_rules": 900.0}
# Store-only detectors that run side by side on threads (each on its own store connection):
# every set-based rule but the customer-PO scan (it reads the runtime's mirror, on the
# run's own thread); the vendor rule after the late rule on the same thread (it reads that
# rule's order join).
THREADS_ENV = "PLX_PROACTIVE_DETECTOR_THREADS"
MAX_DETECTOR_THREADS = 6
CHAINED = {"vendor_no_reply": LATE_RULE}
# The vendor-terms check is incremental: it keeps its readings per purchase order in the
# inbox (``event_cache``) and investigates only the ones whose mail thread changed.
INCREMENTAL = ("vendor_terms_unapplied",)
# Rules that make tool calls (the run principal's ``plx.process``/``plx.object``/
# ``plx.investigate``): not configured without a caller, never part of the store scope.
CALLING = frozenset({"card_rules", "orders_at_risk", "vendor_terms_unapplied",
                     "negative_margin"})
SET_BASED = frozenset(DETECTORS) - CALLING
STATES = ("open", "acknowledged", "resolved")
# Detectors whose claim rests on what sources do not hold (an absence), so on the run
# principal's grant: their gaps show only to a reader granted every recorded audience.
REQUIRES_GRANT = frozenset({"orders_at_risk", "accepted_quote_no_order", "vendor_no_reply",
                            "late_customer_order"})
# Gap kinds whose wording sums over the whole run (money at stake): an open one is reworded
# only by a ``complete`` run of its detector.
WHOLE_ONLY = frozenset({"po_impact_margin"})
LATENESS_GRAPH = Path(__file__).resolve().parents[1] / "ontology" / "schema" / "process" \
    / "po_delivery_window.json"
UNREADABLE = '["unreadable"]'   # a markings value no reader holds (fail closed)
RECEIPT_LINKS = ("ReceiptLine.same_record_as", "ReceiptLine.receives_line",
                 "ReceiptLine.line_of")
CONFIRMATION_TYPE = "SupplierConfirmation"
CONFIRMATION_LINK = po_status.CONFIRMS   # confirmation -> the order it confirms
CONFIRMED_DATE = "delivery_date"
DRAFT_LABEL = "Draft supplier message (not sent)"
DRAFT_STATUS = "draft_not_sent"
MAX_DRAFT_TEXT = 1500
MAX_DRAFT_LINES = 6
MAX_DRAFT_CITATIONS = 20   # the catalog's workbench.draft.record citation_list max_items

Cite = tuple  # (l1_handle, object_id, canonical markings)


# --------------------------------------------------------------------------- store reads

def scan_threads(environ: Mapping[str, str] | None = None) -> int:
    """Threads a link scan reads with: every core (``PLX_PROACTIVE_SCAN_THREADS``
    overrides; 1: one connection, no thread)."""
    raw = (os.environ if environ is None else environ).get(SCAN_THREADS_ENV, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return 1
    return max(1, os.cpu_count() or 1)


def _connect(path: Path) -> sqlite3.Connection:
    from operating.pg import ontology_reader
    if ontology_reader.serves(path):
        # PLX_ONTOLOGY_PG: the Postgres ontology (current to the last delta layer), the
        # detectors' SQL run as written (operating/pg/sqlite_compat.py)
        from operating.pg.sqlite_compat import PgCompatConnection
        return PgCompatConnection(ontology_reader.dsn())   # type: ignore[return-value]
    # read only, one thread at a time (``_Store.conn`` is per thread); the run's own thread
    # may still set its progress handler and close it
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # No query of a detector may build a transient index over a whole table: on a
    # store of millions of value cells that index is a multi-GB temp file and minutes.
    conn.execute("PRAGMA automatic_index = OFF")
    return conn


def _link_row(row: Sequence[Any]) -> tuple:
    """A link row as kept (``_LinkRows``): its type, tier and markings shared strings."""
    return (row[0], row[1], sys.intern(row[2]), row[3], row[4], sys.intern(row[5]),
            sys.intern(row[6]), bool(row[7]))


class _LinkRows:
    """The run's link rows by link type, read once (``_Store.links``): each row
    (rowid, link_id, link_type, from_object, to_object, tier, markings, gone), ``gone``
    when either end is removed; every current link not contradicted, in rowid order.
    Kept up to ``LINK_CACHE_ROWS`` rows."""

    def __init__(self) -> None:
        self.by_type: dict[str, list[tuple]] = {}
        self.kept = 0
        self.lock = threading.Lock()


class _Store:
    """One store generation, read only, every read over current records not removed.

    The link table has no index by link type, so each link read scans it whole (about a
    minute on the live store, ~20 reads a run): ``links`` reads every type it is asked for
    once per run, the table split into rowid ranges read side by side on ``threads``
    connections, and keeps the rows for the rules after it (``prefetch_links``).

    ``conn`` is per thread (the detectors run side by side, ``run(threads=)``): each thread
    reads on its own read-only connection, all closed with the store."""

    def __init__(self, path: Path, *, threads: int | None = None,
                 scope: Iterable[str] | None = None,
                 audience_keys: Iterable[str] | None = None) -> None:
        if not Path(path).is_file():
            raise ostore.StoreError("store_missing")
        self.path = Path(path)
        # an incremental pass (``operating.proactive_incremental``): the records it may read
        # whole types of (every record a changed record's rules read, per their declared
        # inputs); a whole-type read lists only these, a link read only links touching
        # them. A read naming its objects (``objects=``, ``handles=``) is never narrowed.
        self.limit: frozenset[str] | None = None if scope is None else frozenset(scope)
        self._scope_ids: list[str] = sorted(self.limit) if self.limit is not None else []
        self._known_keys = None if audience_keys is None else frozenset(audience_keys)
        self.whole_rows = _LinkRows()   # links read whole in a scoped store (``whole``)
        # the populations the rules judged against (``population``): key -> digest, and the
        # rules that read each ("*": every rule, a filter over all their gaps)
        self.populations: dict[str, str] = {}
        self.population_readers: dict[str, set[str]] = defaultdict(set)
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self.interrupt: Callable[[], int] | None = None
        problem = ostore.format_problem(self.conn)
        if problem is not None:
            self.close()
            raise ostore.StoreError("store_format_mismatch", problem)
        self.digest = self.meta("content_digest")
        self.threads = scan_threads() if threads is None else max(1, threads)
        self.link_rows = _LinkRows()
        # the budget's SQLite progress handler (``Budget.interrupt``, ``interrupt``), on
        # every connection this store opens: a scan stops at the deadline as the detectors'
        # queries do (it reads the window of the thread running the statement)
        self._memo: dict[str, Any] = {}
        self._making: dict[str, threading.Lock] = {}

    @property
    def scoped(self) -> bool:
        """Whether this thread's reads are narrowed to the scope (not inside ``whole``)."""
        return self.limit is not None and not getattr(self._local, "whole", False)

    @contextlib.contextmanager
    def whole(self) -> Iterator[None]:
        """Reads on this thread over the whole store, even in an incremental pass: what a
        rule judges a subject against that sums over every record (a population)."""
        before = getattr(self._local, "whole", False)
        self._local.whole = True
        try:
            yield
        finally:
            self._local.whole = before

    def judging(self, rule: str | None) -> None:
        """The rule this thread judges now (``population`` names its readers)."""
        self._local.rule = rule

    def population(self, key: str, make: Callable[[], Any],
                   digest: Callable[[Any], Any] = lambda made: made) -> Any:
        """What ``make()`` reads over the whole store (never the scope), once per run, its
        ``digest`` kept with the rule reading it: an incremental pass judges a rule whole
        when one of its populations moved (``run(scope=)``), since that moves subjects
        it did not read."""
        rule = getattr(self._local, "rule", None) or "*"

        def read() -> Any:
            with self.whole():
                made = make()
            text = json.dumps(_canonical(digest(made)), sort_keys=True)
            with self.link_rows.lock:
                self.populations[key] = hashlib.sha256(text.encode()).hexdigest()[:32]
            return made
        made = self.memo("population:" + key, read)
        with self.link_rows.lock:
            self.population_readers[key].add(rule)
        return made

    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's read-only connection (opened on its first read)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _connect(self.path)
            if self.interrupt is not None:
                conn.set_progress_handler(self.interrupt, 10000)
            with self._conns_lock:
                self._conns.append(conn)
            self._local.conn = conn
        return conn

    def watch(self, interrupt: Callable[[], int] | None) -> None:
        self.interrupt = interrupt
        with self._conns_lock:
            conns = list(self._conns)
        for conn in conns:
            if interrupt is None:
                conn.set_progress_handler(None, 0)
            else:
                conn.set_progress_handler(interrupt, 10000)

    def memo(self, key: str, make: Callable[[], Any]) -> Any:
        """What ``make()`` reads, once per run (several rules ask for it; rules running
        side by side wait for the first one's read instead of reading it again)."""
        with self.link_rows.lock:
            if key in self._memo:
                return self._memo[key]
            making = self._making.setdefault(key, threading.Lock())
        with making:
            with self.link_rows.lock:
                if key in self._memo:
                    return self._memo[key]
            made = make()
            with self.link_rows.lock:
                return self._memo.setdefault(key, made)

    def close(self) -> None:
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            conn.close()

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def values(self, types: Sequence[str], props: Sequence[str] | None = None, *,
               objects: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
        """object_id -> {"type", "props": {property: [row, ...]}}; ``objects``: only these.

        Object first, in chunks of ``VALUES_CHUNK`` ids: the records of the types (not
        removed) are listed by the type index, then each chunk's values are read by object
        id (version, source, cell). One query over a whole type let the planner start from
        the property instead and index every value cell of the store on the fly (a
        multi-GB temp file; the home's engine run took 23 minutes on 2.5M objects)."""
        params: dict[str, Any] = {"types": json.dumps(list(types))}
        listed = ("SELECT o.object_id FROM objects o "
                  "WHERE o.object_type IN (SELECT value FROM json_each(:types)) AND NOT "
                  + ostore.removed_at_sql("o.object_id"))
        if objects is not None or self.scoped:
            # named objects, or the scope's (an incremental pass): listed by object id
            listed += " AND o.object_id IN (SELECT value FROM json_each(:objects))"
            named = sorted(set(objects)) if objects is not None else self._scope_ids
            ids = []
            for start in range(0, len(named), SCOPE_CHUNK):
                params["objects"] = json.dumps(named[start:start + SCOPE_CHUNK])
                ids.extend(row[0] for row in self.conn.execute(listed, params))
            ids.sort()
        else:
            ids = [row[0] for row in self.conn.execute(listed + " ORDER BY o.object_id",
                                                       params)]
        sql = ("SELECT o.object_id, o.object_type, v.property, v.value_type, v.value, v.unit, "
               "v.quantity_role, v.l1_handle, v.markings, v.lineage FROM objects o "
               "JOIN object_values v ON v.object_id = o.object_id "
               "WHERE o.object_id IN (SELECT value FROM json_each(:chunk)) "
               "AND v.superseded_at IS NULL")
        chunk_params: dict[str, Any] = {}
        if props is not None:
            sql += " AND v.property IN (SELECT value FROM json_each(:props))"
            chunk_params["props"] = json.dumps(list(props))
        sql += " ORDER BY o.object_id, v.property, v.l1_handle"
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), VALUES_CHUNK):
            chunk_params["chunk"] = json.dumps(ids[start:start + VALUES_CHUNK])
            for row in self.conn.execute(sql, chunk_params):
                entry = out.setdefault(row["object_id"],
                                       {"type": row["object_type"], "props": {}})
                entry["props"].setdefault(row["property"], []).append(dict(row))
        return out

    def links(self, types: Sequence[str], tiers: Sequence[str] | None = None, *,
              held: bool = True) -> list[dict[str, Any]]:
        """Current links of these types, never contradicted, in the store's (rowid)
        order; ``held``: only those between records not removed."""
        rows = self._link_rows(list(dict.fromkeys(types)))
        tier_set = None if tiers is None else frozenset(tiers)
        return [dict(zip(LINK_COLUMNS, row[1:7])) for row in rows
                if (tier_set is None or row[5] in tier_set) and not (held and row[7])]

    def prefetch_links(self, types: Iterable[str]) -> None:
        """Read the links of these types in one scan, kept for the rules after it."""
        self._link_rows(list(dict.fromkeys(types)))

    def _link_rows(self, types: list[str]) -> list[tuple]:
        cache = self.link_rows if self.limit is None or self.scoped else self.whole_rows
        with cache.lock:
            missing = [t for t in types if t not in cache.by_type]
            found = {t: cache.by_type[t] for t in types if t in cache.by_type}
            if missing:
                read = self._scan_links(missing)
                for name in missing:
                    rows = read.get(name, [])
                    found[name] = rows
                    if cache.kept + len(rows) <= LINK_CACHE_ROWS:
                        cache.by_type[name] = rows
                        cache.kept += len(rows)
        if len(types) == 1:
            return found[types[0]]
        return sorted((row for name in types for row in found[name]), key=lambda r: r[0])

    def _scan_links(self, types: Sequence[str]) -> dict[str, list[tuple]]:
        """type -> its rows (``_LinkRows``), in rowid order (``_ranged``)."""
        sql = ("SELECT l.rowid, l.link_id, l.link_type, l.from_object, l.to_object, l.tier, "
               "l.markings, (" + ostore.removed_at_sql("l.from_object") + " OR "
               + ostore.removed_at_sql("l.to_object") + ") AS gone FROM link_rows l "
               "WHERE l.link_type IN (SELECT value FROM json_each(:types)) "
               "AND l.superseded_at IS NULL AND l.tier <> 'contradicted'")
        out: dict[str, list[tuple]] = defaultdict(list)
        if self.scoped:
            # an incremental pass: the links with an end in its scope, by the end indexes
            for row in self.touching(self._scope_ids, types):
                out[row[2]].append(row)
            return out
        if self.limit is not None:
            # a population of an incremental pass: each type whole, by the end index of
            # its smaller end type (every record of it), never a scan of the link table
            rest = []
            for link_type, ends in _link_ends(types).items():
                sized = sorted((self._type_count(e), e) for e in ends)
                if not sized or sized[0][0] > WHOLE_BY_INDEX_MAX:
                    rest.append(link_type)
                    continue
                ids = [r[0] for r in self.conn.execute(
                    "SELECT object_id FROM objects WHERE object_type = ?", (sized[0][1],))]
                for row in self.touching(ids, [link_type]):
                    out[row[2]].append(row)
            if not rest:
                return out
            types = rest
        for rows in self._ranged("link_rows", "l", sql, " ORDER BY l.rowid",
                                 {"types": json.dumps(list(types))}, _link_row):
            for row in rows:   # the ranges in rowid order, each in rowid order
                out[row[2]].append(row)
        return out

    def _type_count(self, object_type: str) -> int:
        return self.memo("count:" + object_type, lambda: self.conn.execute(
            "SELECT count(*) FROM objects WHERE object_type = ?", (object_type,)).fetchone()[0])

    def touching(self, objects: Sequence[str], types: Sequence[str] | None = None,
                 *, every: bool = False) -> list[tuple]:
        """The current links (``_LinkRows`` rows) with an end at one of ``objects``, of
        ``types`` (None: every type), in rowid order, read by the link table's end indexes;
        ``every``: contradicted ones too (a walk of what a record is tied to)."""
        base = ("SELECT l.rowid, l.link_id, l.link_type, l.from_object, l.to_object, l.tier, "
                "l.markings, (" + ostore.removed_at_sql("l.from_object") + " OR "
                + ostore.removed_at_sql("l.to_object") + ") AS gone FROM link_rows l "
                "WHERE l.superseded_at IS NULL"
                + ("" if every else " AND l.tier <> 'contradicted'"))
        params: dict[str, Any] = {}
        if types is not None:
            base += " AND l.link_type IN (SELECT value FROM json_each(:types))"
            params["types"] = json.dumps(list(types))
        found: dict[str, tuple] = {}
        ids = sorted(set(objects))
        for start in range(0, len(ids), SCOPE_CHUNK):
            params["chunk"] = json.dumps(ids[start:start + SCOPE_CHUNK])
            for end in ("from_object", "to_object"):
                for r in self.conn.execute(
                        base + f" AND l.{end} IN (SELECT value FROM json_each(:chunk))",
                        params):
                    found.setdefault(r[1], _link_row(r))
        return sorted(found.values(), key=lambda r: r[0])

    def _ranged(self, table: str, alias: str, sql: str, tail: str,
                params: Mapping[str, Any], shape: Callable[[Any], Any]) -> list[list[Any]]:
        """``sql`` (over ``table`` as ``alias``, then ``tail``) read by rowid ranges of the
        table side by side, one connection per thread (each checked to read this
        generation), the ranges' rows in rowid order of the ranges; a small table, one
        thread or a generation replaced meanwhile: one read on this connection."""
        top = self.conn.execute(f"SELECT max(rowid) FROM {table}").fetchone()[0] or 0
        if self.threads > 1 and top >= PARALLEL_LINK_ROWS:
            from concurrent.futures import ThreadPoolExecutor

            step = top // (self.threads * LINK_PARTS_PER_THREAD) + 1
            ranged = sql + f" AND {alias}.rowid >= :lo AND {alias}.rowid < :hi" + tail

            def part(lo: int) -> list[Any]:
                conn = _connect(self.path)
                try:
                    if self.interrupt is not None:
                        conn.set_progress_handler(self.interrupt, 10000)
                    held = conn.execute("SELECT value FROM store_meta WHERE key = "
                                        "'content_digest'").fetchone()
                    if (held[0] if held else None) != self.digest:
                        raise ostore.StoreError("store_replaced")
                    return [shape(r) for r in conn.execute(ranged, {**params, "lo": lo,
                                                                    "hi": lo + step})]
                finally:
                    conn.close()
            try:
                with ThreadPoolExecutor(self.threads) as pool:
                    return list(pool.map(part, range(0, top + 1, step)))
            except ostore.StoreError:
                pass   # another generation is served now: read this one on this connection
        return [[shape(r) for r in self.conn.execute(sql + tail, dict(params))]]

    def versions(self, object_type: str | None = None, handles: Iterable[str] | None = None,
                 objects: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Current versions (handle, object, markings) of a type, of these handles or of
        these objects."""
        sql = ("SELECT v.l1_handle, v.object_id, v.markings, o.object_type FROM object_versions v "
               "JOIN objects o ON o.object_id = v.object_id WHERE v.superseded_at IS NULL AND NOT "
               + ostore.removed_at_sql("o.object_id"))
        params: dict[str, Any] = {}
        if object_type is not None:
            sql += " AND o.object_type = :type"
            params["type"] = object_type
            if handles is None and objects is None and self.scoped:
                objects = self._scope_ids   # a whole type in an incremental pass: its scope
        if handles is not None:
            sql += " AND v.l1_handle IN (SELECT value FROM json_each(:handles))"
            params["handles"] = json.dumps(sorted(set(handles)))
        if objects is not None:
            sql += " AND v.object_id IN (SELECT value FROM json_each(:objects))"
            params["objects"] = json.dumps(sorted(set(objects)))
        return [dict(row) for row in self.conn.execute(sql, params)]

    def audience_keys(self) -> set[str]:
        """Every audience key a record of the store sits in (removed ones too), read once
        per run by object rowid ranges side by side (``_ranged``): a scan of every record
        (minutes on the live store), asked by several rules."""
        if self._known_keys is not None:
            # an incremental pass: the generation's keys and its changed records' (kept
            # beside the inbox, ``operating.proactive_incremental``), never a whole scan
            return set(self._known_keys)

        def read() -> frozenset[str]:
            return frozenset(key for rows in self._ranged(
                "objects", "o", "SELECT DISTINCT o.audience_key FROM objects o WHERE 1",
                "", {}, lambda r: r[0]) for key in rows)
        return set(self.memo("audience_keys", read))

    def scope(self) -> list[tuple[str, str, int]]:
        """(audience key, current markings, objects): the whole store the run covered,
        counted by object rowid ranges side by side (``_ranged``) and summed."""
        counts: Counter = Counter()
        for rows in self._ranged(
                "objects", "o",
                "SELECT o.audience_key, v.markings, count(*) FROM objects o JOIN "
                "object_versions v ON v.object_id = o.object_id AND v.superseded_at IS NULL "
                "WHERE NOT " + ostore.removed_at_sql("o.object_id"),
                " GROUP BY o.audience_key, v.markings", {}, tuple):
            for key, marks, n in rows:
                counts[(key, marks)] += n
        return [(key, marks, n) for (key, marks), n in sorted(counts.items())]


WHOLE_BY_INDEX_MAX = 300_000   # an end type of up to this many records: read by index


def _link_ends(types: Sequence[str]) -> dict[str, list[str]]:
    """link type -> its declared end types (schema ``from``/``to``): a population reads
    the type whole through the end index of the smaller one (every link of the type has
    that end); none declared: a scan of the link table."""
    from ontology.schema import load_schema

    declared = load_schema().link_types
    return {t: [e for e in ((declared.get(t) or {}).get("from"), (declared.get(t) or {})
                            .get("to")) if isinstance(e, str) and e] for t in types}


def _canonical(value: Any) -> Any:
    """``value`` as JSON in one order (a population's digest)."""
    if isinstance(value, Mapping):
        return sorted(([_canonical(k), _canonical(v)] for k, v in value.items()), key=repr)
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(v) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _markings(*texts: str | None) -> str:
    """The union of markings values, canonical; anything unreadable never shows."""
    held: set[str] = set()
    for text in texts:
        try:
            parsed = json.loads(text) if text else []
        except (TypeError, ValueError):
            return UNREADABLE
        if not isinstance(parsed, list) or not all(isinstance(m, str) for m in parsed):
            return UNREADABLE
        held.update(parsed)
    try:
        return ostore.markings_text(held)
    except ostore.StoreError:
        return UNREADABLE


def _cites(rows: Iterable[Mapping[str, Any]], *extra_markings: str | None) -> list[Cite]:
    out: dict[str, Cite] = {}
    for row in rows:
        handle = row.get("l1_handle")
        if isinstance(handle, str) and handle and handle not in out:
            out[handle] = (handle, row.get("object_id"),
                           _markings(row.get("markings"), *extra_markings))
    return list(out.values())


_clusters = po_status.clusters   # member -> its settled cluster's least object id


def _day(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def _gap(kind: str, detector: str, object_id: str, object_type: str, key: str, *,
         summary: str, missing: str, why: str, priority: str, about: tuple[str, str],
         groups: Sequence[Sequence[Cite]], extra: Sequence[Cite] = (),
         days_late: int | None = None, event: Mapping[str, Any] | None = None,
         **facts: Any) -> dict[str, Any] | None:
    """``days_late``: the days a purchase order open by the one status definition is past its
    requested-by date (None: not late; ``late_order``'s line-level lateness is another gap
    kind and never carries it); the inbox lists every late purchase order first, longest late
    first, whatever its kind's priority. ``event``: the facts the briefing ranks by
    (``days``: days between two cited dates; ``amount``: a cited figure, shown only with
    every group or, with a ``handle``, only to a reader who may open that citation;
    ``orders_affected``: a count of cited records), kept beside the follow-up. ``facts``:
    enum words the pages read (``confirmation``: ``none_found``, ``found_unverified`` or
    ``confirmed``; ``due_basis``: where the due date comes from)."""
    groups = [list(group) for group in groups]
    if not groups or not all(groups):
        return None   # a claim with an uncited part is not raised
    handles = list(dict.fromkeys([c[0] for g in groups for c in g] + [c[0] for c in extra]))
    who = pg._who(object_type, {"about": about, "handles": handles})
    return {"id": "pe-" + hashlib.sha256(f"{kind}|{object_id}|{key}".encode("utf-8"))
            .hexdigest()[:16],
            "kind": kind, "detector": detector, "object_id": object_id,
            "object_type": object_type, "summary": pg._clip(summary),
            "groups": groups, "extra": list(extra),
            "follow_up": {"object_id": object_id, "missing": pg._clip(missing, 480),
                          "why": why, "who_can_answer": who, "state": "unknown",
                          "priority": priority,
                          **({"days_late": days_late} if days_late is not None else {}),
                          **({"event": dict(event)} if event else {}),
                          # enums only (``confirmation``, ``due_basis``): never a value
                          **{k: v for k, v in facts.items() if v is not None}}}


Part = tuple  # (object_id, canonical markings): one record a skip's cause reads


def _parts(rows: Iterable[Mapping[str, Any]], *extra_markings: str | None) -> list[Part]:
    """The records ``rows`` were read from, each at its row's markings joined with
    ``extra_markings`` (the links that tie it to the rest of the cause)."""
    return [(row.get("object_id"), _markings(row.get("markings"), *extra_markings))
            for row in rows]


class _Skips(Counter):
    """A rule's records it did not judge: the count per reason (the run summary) and each
    skip with its subjects (the records it left unjudged) and its cause (every record and
    link, at its markings, the decision to skip read: ``skips``). A reader is told how many
    of the records it may read a rule left unjudged, counted only over skips whose whole
    cause it may read, the way a gap shows only when the reader may open all of its
    evidence: a skip caused by a record or a link it may not read (an identity twin
    disputing the need date, a lead from a hidden confirmation) is not counted for it, so
    no count names or hints at a hidden record. A skip no record stands for (an order the
    store never projected) or whose cause names a value without its record is
    ``unattributed``: unknown for every reader alike."""

    def __init__(self) -> None:
        super().__init__()
        self.skips: list[tuple[tuple[str, ...], tuple[Part, ...]]] = []
        self.unattributed = 0

    def note(self, reason: str, subjects: Sequence[str] = (),
             cause: Iterable[Part] = ()) -> None:
        self[reason] += 1
        parts = {(subject, "[]") for subject in subjects}
        for object_id, markings in cause:
            if not isinstance(object_id, str) or not object_id:
                parts = set()   # a cause no reader can be checked against: fail closed
                break
            parts.add((object_id, markings if isinstance(markings, str) else UNREADABLE))
        if not subjects or not parts:
            self.unattributed += 1
            return
        self.skips.append((tuple(sorted(set(subjects))), tuple(sorted(parts))))


def _result(gaps: Iterable[dict[str, Any] | None], *, status: str = "complete",
            reasons: Sequence[str] = (), not_evaluated: Counter | None = None
            ) -> dict[str, Any]:
    skips = not_evaluated if isinstance(not_evaluated, _Skips) else None
    counted = sum((not_evaluated or Counter()).values())
    return {"status": status, "reasons": list(reasons),
            "gaps": [gap for gap in gaps if gap is not None],
            "not_evaluated": dict(sorted((not_evaluated or Counter()).items())),
            "unjudged": [{"subjects": list(subjects), "cause": [list(p) for p in cause]}
                         for subjects, cause in skips.skips] if skips is not None else [],
            # a count without its records (a plain Counter) is attributed to nobody
            "unattributed": skips.unattributed if skips is not None else counted}


def _coverage(total: int, evaluated: int, not_evaluated: Mapping[str, int], *,
              unit: str) -> dict[str, Any]:
    """A detector's coverage: of ``total`` records of ``unit`` it should judge, how many it
    judged and, by reason, how many it did not (never silently omitted)."""
    missing = {k: int(v) for k, v in sorted(not_evaluated.items()) if v}
    return {"unit": unit, "total": int(total), "evaluated": int(evaluated),
            "not_evaluated": missing}


@functools.lru_cache(maxsize=1)
def _run_link_types() -> list[str]:
    """The link types a full run's rules read, read in one scan up front (``_Store.links``;
    a type not listed is read when a rule asks for it, as fast, just not shared)."""
    from . import impact_chain as ic
    from . import proactive_events as ev

    amounts = [name for spec in suspect_money.SPECS.values()
               for name in (spec.get("line_of"), spec.get("item"), spec.get("uom"),
                            spec["party"][1] if (spec.get("party") or ("",))[0] == "link"
                            else None) if name]
    return list(dict.fromkeys([
        *po_status.TWIN_LINKS, po_status.LINE_OF, po_status.RECEIPT_RECEIVES_LINE,
        po_status.QC_RECEIVES_LINE, po_status.RECEIVED_LOT, *po_status.CONFIRMATION_LINKS,
        *TEST_ORDER_LINKS, *TEST_TWIN_LINKS, *RECEIPT_LINKS, *_identity_link_types(),
        *declared_qc_links(),
        pg.LINE_LINK, pg.SUPPLIER_LINK, CONFIRMATION_LINK, *so_status.twin_links(),
        so_status.LINE_OF,
        *so_status.FULFILLED_LINKS, *ev.LINK_TYPES,
        *ic.LINK_TYPES, ic.LINE_SELLS, *ic.LEAD_ROUTE_TYPES, *amounts]))


def _identity_link_types() -> tuple[str, ...]:
    from ontology.schema import load_schema
    from .ontology_tools import identity_link_type

    return tuple(sorted(lid for lid, link in load_schema().link_types.items()
                        if identity_link_type(link)))


@functools.lru_cache(maxsize=1)
def declared_qc_links() -> tuple[str, ...]:
    """Link types the schema declares between a QC record and anything (none today)."""
    from ontology.schema import load_schema

    return tuple(sorted(lid for lid, link in load_schema().link_types.items()
                        if link.get("from") in QC_TYPES or link.get("to") in QC_TYPES))


# --------------------------------------------------------------------------- detectors

PO_STATUS_PROPS = tuple(po_status.SOURCE_OF)
# the follow-up field naming the order an item is about (its key and its record's rank):
# never shown, read by ``read_inbox`` to list one item per order for each reader
_ORDER = "order"


def _po_source(st: _Store) -> po_status.StoreSource:
    """The whole store's purchase orders as the one PO reader reads them, read once per
    run (the late rule and the receipt quantity rule both read every order)."""
    # read whole even in an incremental pass: its number collisions and default dates
    # are populations every order is judged against
    return st.population("po_source", lambda: po_status.StoreSource(st),
                         lambda src: (sorted(src.colliding), src.defaults.values))


def _late_or_unconfirmed_po(st: _Store, as_of: date) -> dict[str, Any]:
    """Each settled PO read by the one purchase-order reader (``po_status.read`` over
    ``po_status.StoreSource``: status, lines, FM QC log, ranked need date, promise and
    verdict); this rule adds only its counts, skips, citations and gaps."""
    name = "late_or_unconfirmed_po"
    src = _po_source(st)
    skipped = _Skips()
    counts: Counter = Counter()
    classes: Counter = Counter()
    gaps = []
    disagreeing: set[tuple[str, frozenset]] = set()
    # one order across systems: a settled cluster and every record it is compared with
    # (``po_status.compared_twin``), joined transitively (one BC order, two Portal twins:
    # one order). Each record's late or unconfirmed item is kept with its order key and
    # rank; ``read_inbox`` shows one per order among those its reader may open (``_ORDER``)
    reads = [po_status.read(src, top, as_of) for top, _ in sorted(src.clusters.items())]
    joined = [(po.members[0], other) for po in reads
              for other in (*po.members[1:], *po.date_twins)]
    root = po_status.clusters(joined, [m for po in reads
                                       for m in (*po.members, *po.date_twins)])
    counted: dict[str, set[str]] = defaultdict(set)
    order_class: dict[str, tuple[list, str]] = {}
    # orders open by any read; those any read dates; each undated read's pending item
    open_orders: set[str] = set()
    dated_orders: set[str] = set()
    undated_reads: dict[str, list[tuple[str, list, Any]]] = defaultdict(list)
    for po in reads:
        members = po.members
        order = root.get(members[0], members[0])
        # BC's record governs; among others the least record id, so the choice is pinned
        # the copy that holds the number the supplier knows (a FileMaker-era order's
        # FileMaker PO number) is shown first, then a BC copy, then the least record id
        rank = [0 if any(src.props(m).get("fm_po_number") for m in members) else
                1 if any(po_status.system_of(m) == po_status.POSTED_COPY for m in members)
                else 2, min(members)]

        def order_gap(gap: Any, _order: str = order, _rank: list = rank) -> None:
            if gap is not None:
                gap["follow_up"][_ORDER] = {"key": _order, "rank": _rank}
                gaps.append(gap)

        def count(key: str, _order: str = order) -> int:
            if key in counted[_order]:
                return 0
            counted[_order].add(key)
            return 1
        marks = po.identity_marks
        state = po.status
        # one class per order: its governing record's (BC first, else the least id)
        if order not in order_class or rank < order_class[order][0]:
            order_class[order] = (rank, state.cls)
        for conflict in po.conflicts:
            records = frozenset(r for side in conflict["sides"] for r in side["records"])
            if not records or (conflict["field"], records) in disagreeing:
                continue   # the same disagreement read again from the other record's side
            disagreeing.add((conflict["field"], records))
            # an open copy of an order another copy records received is a reconciliation
            # note (a receipt in any system closes the order), never a delivery risk
            gaps.append((_receipt_reconciliation if conflict.get("reconcile")
                         else _systems_disagree)(name, conflict, records,
                                                 [*marks, *po.marks]))

        def rows(prop: str) -> list[dict[str, Any]]:
            return [r for m in members for r in src.props(m).get(prop, [])]
        # what every skip of this order read: its records and the identity links joining
        # them, their status, line quantity and date values (``skip``'s cause)
        read: list[Part] = [(m, _markings(*marks)) for m in members] + _parts(
            [*po.evidence, *rows("expected_date")], *marks, *po.marks)

        def skip(reason: str, *more: Part) -> None:
            skipped.note(reason, members, [*read, *more])
        if state.cls == po_status.UNKNOWN:
            skip(state.reason)   # neither open nor closed: counted, never guessed
            continue
        if state.cls != po_status.OPEN:
            continue
        open_orders.add(order)
        rep = min(m for m in members if any(src.props(m).get(p) for p in PO_STATUS_PROPS))
        for _ in po.need.unparseable:
            skip("date_unparseable")
        verdict, why = po.verdict
        if why == "no_requested_date":
            # the requested-by date is the reference or it is missing: never a line's or
            # the supplier's date in its place (nor a BC Draft twin's, decision
            # 2026-11-01-late-list-draft-twins): never late, listed apart as
            # "open, no due date recorded", cited by the status it is open by
            skip("no_need_date")
            # decided once every read of the order is done: an order another read dates
            # is never undated (one order, one verdict)
            status_cites = [[cite] for cite in _cites(po.evidence, *marks, *po.marks)]
            undated_reads[order].append((order, rank, _gap(
                UNDATED_KIND, name, rep, "PurchaseOrder", "undated",
                summary="An open purchase order has no due date recorded"
                        + (" (its only dates are import or record-creation defaults)"
                           if po.need is not None and po.need.defaulted else ""),
                missing="What is the requested-by date of this open purchase order?",
                why="The order is open, but no requested-by date, supplier-confirmed "
                    "date or lead time is recorded on it or on any released system record "
                    "of it, so it is never counted late.",
                priority="low", about=("PurchaseOrder", "expected_date"),
                groups=status_cites,
                extra=_cites(rows("po_number"), *marks)) if status_cites else None))
            continue
        dated_orders.add(order)
        if why == "need_date_disputed":
            skip("need_date_disputed")
            continue
        need = po.need.hi
        need_cites = _cites(po.need.governing, *marks, *po.need.marks)
        basis = po.need.basis
        # every status record and line quantity the verdict rests on is its own evidence:
        # a reader who may not open one of them sees no gap (none is an alternative)
        status_cites = [[cite] for cite in _cites(po.evidence, *marks, *po.marks)] or [[]]
        number = _cites(rows("po_number") + rows("fm_po_number"), *marks) + _cites(
            [r for t in po.date_twins for p in ("po_number", "fm_po_number")
             for r in src.props(t).get(p, [])], *marks, *po.marks)
        # confirmations linked to ANY copy of the order (its twins in every system)
        links = src.confirm_links([*members, *(t for t in po.date_twins if t not in members)])
        linked = [(link["from_object"], _markings(link["markings"], *marks)) for link in links]
        settled = [link for link in links if link["tier"] in CONFIRMING]
        late_now = verdict == po_status.LATE
        if late_now or not links:   # decided without a confirmation (none is at risk)
            counts["evaluated"] += count("evaluated")
            counts["late"] += count("late") if late_now else 0
        if late_now and links and not settled:
            # a confirmation is linked, but only as an unverified lead: never "none"
            counts["confirmation_unverified"] += count("confirmation_unverified")
            found = _link_cites(src, links)
            order_gap(_gap(
                "late_po", name, rep, "PurchaseOrder", "overdue",
                summary=_late_summary(basis, need),
                missing=f"When will the purchase order due {need.isoformat()} be received?",
                why="The order is still open after its due date.",
                priority="medium", about=("PurchaseOrder", "expected_date"),
                groups=[need_cites, *status_cites], extra=number + found,
                days_late=(as_of - need).days,
                # "found, unverified" only to a reader who may open one of the found
                # confirmations (``_shown``); never "none"
                confirmation="found_unverified", confirmation_cites=[c[0] for c in found],
                due_basis=basis))
            continue
        reused = [] if links else src.reused_links(
            [*members, *(t for t in po.date_twins if t not in members)])
        if late_now and reused:
            # only leads on a number another order also holds: evidence that cannot be
            # tied to this order, so "needs check", never "none found"
            found = _link_cites(src, reused)
            counts["confirmation_needs_check"] += count("confirmation_needs_check")
            order_gap(_gap(
                "late_po", name, rep, "PurchaseOrder", "overdue",
                summary=_late_summary(basis, need),
                missing=f"When will the purchase order due {need.isoformat()} be received?",
                why="The order is still open after its due date. A document that may be its "
                    "supplier's confirmation names a number another order also holds.",
                priority="medium", about=("PurchaseOrder", "expected_date"),
                groups=[need_cites, *status_cites], extra=number + found,
                days_late=(as_of - need).days, confirmation=po_status.NEEDS_CHECK,
                confirmation_cites=[c[0] for c in found], due_basis=basis))
            continue
        if not links:
            if need <= as_of:
                counts["unconfirmed"] += count("unconfirmed")
                order_gap(_gap(
                    "unconfirmed_po", name, rep, "PurchaseOrder", "unconfirmed",
                    summary=_unconfirmed_summary(basis, need),
                    missing=f"Has the supplier confirmed the purchase order due "
                            f"{need.isoformat()}?",
                    why="The order is open, its due date has come and no supplier "
                        "confirmation is linked to any copy of it in the store.",
                    priority="high", about=("PurchaseOrder", "expected_date"),
                    groups=[need_cites, *status_cites], extra=number,
                    days_late=(as_of - need).days if late_now else None,
                    confirmation="none_found", due_basis=basis))
            continue
        if late_now:   # late on the requested-by date alone, whatever was confirmed
            order_gap(_gap(
                "late_po", name, rep, "PurchaseOrder", "overdue",
                summary=_late_summary(basis, need),
                missing=f"When will the purchase order due {need.isoformat()} be "
                        "received?",
                why="The order is still open after its due date.",
                priority="medium", about=("PurchaseOrder", "expected_date"),
                groups=[need_cites, *status_cites], extra=number + _link_cites(src, settled),
                days_late=(as_of - need).days, confirmation="confirmed",
                confirmation_cites=[c[0] for c in _link_cites(src, settled)], due_basis=basis))
            continue
        if any(link["tier"] not in CONFIRMING for link in links):
            skip("confirmation_unsettled", *linked)
            continue   # a lead may be the confirmation: at risk or not is not known
        dated = {id(link) for _, link in po.promise.items}
        undated = any(id(link) not in dated for link in links)
        if verdict == po_status.AT_RISK:
            counts["evaluated"] += count("evaluated")
            counts["at_risk"] += count("at_risk")
            shown = po.promise.latest.isoformat()
            by_link: dict[int, tuple[dict[str, Any], list]] = {}
            for row, link in po.promise.items:
                if row["value"] is not None and po_status.day_of(row["value"]) \
                        == po.promise.latest:
                    by_link.setdefault(id(link), (link, []))[1].append(row)
            order_gap(_gap(
                "late_po", name, rep, "PurchaseOrder", "confirmed_after_need_date",
                summary=f"The supplier confirmed delivery on {shown}, after the need date "
                        f"{need.isoformat()}",
                missing=f"Can the purchase order needed by {need.isoformat()} arrive in time, "
                        f"or must the plan move to {shown}?",
                why="A linked supplier confirmation states a delivery date later than the "
                    "order's need date.",
                priority="high", about=("PurchaseOrder", "expected_date"),
                groups=[need_cites, *status_cites,
                        *[_cites(found, link["markings"]) for link, found in by_link.values()]],
                extra=number))
        elif undated:
            skip("confirmation_undated", *linked)
        elif po.promise.reason == "promises_disagree":
            # the confirmations' dates disagree and no document date orders them
            skip("promises_disagree", *linked)
        else:
            counts["evaluated"] += count("evaluated")
    for order, pending in undated_reads.items():
        if order in dated_orders:
            continue   # another read of this order dates it: never undated
        counts["undated"] += 1   # counted whether or not a status value is citable
        for key, rank, gap in pending:
            if gap is not None:
                gap["follow_up"][_ORDER] = {"key": key, "rank": rank}
                gaps.append(gap)
    late_orders = {o for o, keys in counted.items() if "late" in keys}
    result = _result(gaps, not_evaluated=skipped)
    # each record's order (the cross-system join) and that order's class (open when any
    # read of it is open): the vendor-mail rule judges open orders only, one per order
    result["po_orders"] = {m: (order, po_status.OPEN if order in open_orders
                               else order_class.get(order, ([], po_status.UNKNOWN))[1])
                           for m, order in root.items()}
    classes.update(cls for _rank, cls in order_class.values())
    result["counts"] = {"pos": len(order_class),
                        "classes": {cls: classes.get(cls, 0) for cls in po_status.CLASSES},
                        **{key: counts.get(key, 0) for key in (
                            "evaluated", "late", "at_risk", "unconfirmed", "undated")},
                        # orders open by any of their reads (a Portal Pending order beside
                        # its BC Draft twin is classed by the BC record, yet open here):
                        # open_by_read = late + undated + open_other
                        "open_by_read": len(open_orders),
                        "open_other": len(open_orders - late_orders
                                          - (set(undated_reads) - dated_orders))}
    return result


# where an order's due date comes from (``po_status.Need.basis``), as a gap words it
_DUE_WORDS = {"requested": "its need date",
              "vendor_eta": "the supplier's confirmed date",
              "lead_time": "its lead-time due date"}


def _late_summary(basis: str, need: date) -> str:
    return f"An open purchase order is past {_DUE_WORDS[basis]} {need.isoformat()}"


def _unconfirmed_summary(basis: str, need: date) -> str:
    due = ("needed by" if basis == "requested" else "due by its lead time on"
           if basis == "lead_time" else "due by")
    return f"An open purchase order {due} {need.isoformat()} has no supplier confirmation"


def _receipt_reconciliation(name: str, conflict: Mapping[str, Any], records: Iterable[str],
                            marks: Sequence[str]) -> dict[str, Any] | None:
    """A ``receipt_reconciliation`` note: one copy of a purchase order records the goods
    received while another copy (a BC order, say) still shows them outstanding. A receipt
    in any system closes the order, so this is never a late or at-risk claim: the open
    copy needs reconciling. Each side's citations are their own group."""
    field, sides = conflict["field"], conflict["sides"]
    received = conflict.get("governs")
    groups = [_cites(side["items"], *marks, *conflict.get("marks", ())) for side in sides]
    open_side = next((side for side in sides if side["system"] != received), sides[-1])
    subject = (open_side["records"] or [min(records)])[0]
    unit = f" {conflict['unit']}" if conflict.get("unit") else ""

    def said(side: Mapping[str, Any]) -> str:
        value = side["value"]
        return (_STATE_WORDS.get(value, value) if field == "receipt_state"
                else f"{value}{unit}")
    return _gap(
        "receipt_reconciliation", name, subject, "PurchaseOrder",
        f"{field}|{'|'.join(sorted(records))}",
        summary=(f"A received purchase order still shows {_FIELD_WORDS[field]} differently "
                 "in another copy: " + "; ".join(
                     f"{side['system']} says {said(side)}" for side in sides)
                 + (f" ({received} records the goods received; the other copy needs "
                    "reconciling)" if received else "")),
        missing=f"Can the open copy of this received purchase order be closed or "
                f"corrected ({_FIELD_WORDS[field]})?",
        why="A receipt in any system closes the order; the copy that still shows it open "
            "is a bookkeeping difference, not a late delivery.",
        priority="low",
        about=("PurchaseOrder", "status"), groups=groups)


def _link_cites(src: po_status.StoreSource, links: Sequence[Mapping[str, Any]]
                ) -> list[Cite]:
    """The confirming documents' own records, each cited through its link's markings."""
    out: list[Cite] = []
    for link in links:
        out += _cites(src.documents.get(link["from_object"], []), link["markings"])
    return list({c[0]: c for c in out}.values())


_FIELD_WORDS = {"receipt_state": "whether it is fully received",
                "received_quantity": "the quantity received",
                "expected_date": "the requested-by date"}
_STATE_WORDS = {po_status.FULLY_RECEIVED: "fully received",
                po_status.OPEN_LINES: "open lines still outstanding"}


def _systems_disagree(name: str, conflict: Mapping[str, Any], records: Iterable[str],
                      marks: Sequence[str]) -> dict[str, Any] | None:
    """A ``systems_disagree`` gap: two linked records of one purchase order state different
    values (``po_status.read``'s conflict). Each side's cited records are their own
    evidence group, so a reader who may not open either side sees no gap; the summary
    names both values and who governs, never one picked silently."""
    field, governs = conflict["field"], conflict["governs"]
    sides = conflict["sides"]
    unit = f" {conflict['unit']}" if conflict.get("unit") else ""

    def said(side: Mapping[str, Any]) -> str:
        value = side["value"]
        return (_STATE_WORDS.get(value, value) if field == "receipt_state"
                else f"{value}{unit}")
    groups = [_cites(side["items"], *marks, *conflict.get("marks", ())) for side in sides]
    rule = ("the declared date precedence governs" if field == "expected_date" and governs
            else f"{governs} records the goods received" if governs
            else "no system governs")
    subject = next((side["records"][0] for side in sides
                    if side["system"] == governs and side["records"]), min(records))
    return _gap(
        "systems_disagree", name, subject, "PurchaseOrder",
        f"{field}|{'|'.join(sorted(records))}",
        summary=(f"Systems disagree on {_FIELD_WORDS[field]} of a purchase order: "
                 + "; ".join(f"{side['system']} says {said(side)}" for side in sides)
                 + f" ({rule})"),
        missing=f"Which record of this purchase order is right about {_FIELD_WORDS[field]}, "
                "and can the other one be corrected?",
        why="Two linked records of the same purchase order state different values; the "
            "answer cites both and names the one that governs.",
        priority="medium",
        about=("PurchaseOrder", "expected_date" if field == "expected_date" else "status"),
        groups=groups)


# --------------------------------------------------------------------------- customer orders

LATE_SO = "late_customer_order"
SO_NUMBERS = ("order_number", "fm_erp_code")
SO_PROPS = (*so_status.READ_PROPS, so_status.DATE_PROP, *so_status.OWN_DAYS, *SO_NUMBERS)
# the records whose absence the verdict rests on (a shipment or invoice not linked, a
# closing status not held): a reader must be granted every audience they sit in
SO_ABSENCE_TYPES = (so_status.TYPE, "Shipment", "SalesInvoice")


def _late_customer_order(st: _Store, as_of: date) -> dict[str, Any]:
    """A customer order open past its own required date by the one sales-order definition
    (``operating.sales_order_status``): ONE gap per order across its records, joined by
    settled identity links only. A lead twin that is closed, or a lead shipment or invoice,
    leaves it unjudged; an unknown status is ``not_evaluated``, never open; an order no
    record of which holds a line states no goods (``no_order_lines``, not judged); an
    order whose every required date is a source default is undated, never late. Late when its
    requested date is before the business day ``as_of``: cited by the date and by each
    status value it is open by (each its own group), and shown only to a reader granted
    every audience the absence of a closure rests on (``REQUIRES_GRANT``)."""
    name = LATE_SO
    found = st.values((so_status.TYPE,), SO_PROPS)
    binding = {oid: po_status.binding_of(oid) for oid in found}
    twins = st.links(so_status.twin_links())
    settled = [l for l in twins if l["tier"] in SETTLED
               and l["from_object"] in found and l["to_object"] in found]
    root = _clusters(((l["from_object"], l["to_object"]) for l in settled), found)
    members: dict[str, list[str]] = defaultdict(list)
    for oid in sorted(found):
        members[root[oid]].append(oid)
    marks: dict[str, list[str]] = defaultdict(list)
    for link in settled:
        marks[root[link["from_object"]]].append(link["markings"])
    fulfilled: dict[str, bool] = {}   # record -> a settled (True) or lead shipment/invoice
    for link in st.links(so_status.FULFILLED_LINKS):
        if link["to_object"] in found:
            fulfilled[link["to_object"]] = fulfilled.get(link["to_object"], False) \
                or link["tier"] in SETTLED
    lined = {link["to_object"] for link in st.links([so_status.LINE_OF])
             if link["to_object"] in found}   # records holding at least one line
    lead_twins: dict[str, set[tuple[str, str]]] = defaultdict(set)   # order -> (twin, link)
    for link in twins:
        ends = (link["from_object"], link["to_object"])
        if link["tier"] not in SETTLED and all(e in found for e in ends) \
                and root[ends[0]] != root[ends[1]]:
            lead_twins[root[ends[0]]].add((ends[1], link["markings"]))
            lead_twins[root[ends[1]]].add((ends[0], link["markings"]))
    scoped = st.scoped

    def every_default() -> dict[Any, frozenset]:
        # the default required dates of each binding: a population (all its orders)
        held = st.values((so_status.TYPE,), (so_status.DATE_PROP,)) if scoped else found
        return so_status.date_defaults(_grouped(
            (po_status.binding_of(oid), _day(r.get("value"))) for oid, entry in held.items()
            for r in entry["props"].get(so_status.DATE_PROP, ())))
    defaults = st.population("so_default_dates", every_default)
    state = {oid: so_status.record_status(entry["props"], binding=binding[oid],
                                          fulfilled=fulfilled.get(oid, False))
             for oid, entry in found.items()}
    requires = sorted({r[0] for r in st.conn.execute(
        "SELECT DISTINCT audience_key FROM objects WHERE object_type IN "
        "(SELECT value FROM json_each(?))", (json.dumps(list(SO_ABSENCE_TYPES)),))}) or \
        ["sales_order:none_stored"]
    skipped = _Skips()
    classes: Counter = Counter()
    counts: Counter = Counter()
    gaps = []
    for top, group in sorted(members.items()):
        link_marks = marks.get(top, [])

        def rows(*names: str) -> list[dict[str, Any]]:
            return [r for m in group for n in names for r in found[m]["props"].get(n, ())]
        read: list[Part] = [(m, _markings(*link_marks)) for m in group] + _parts(
            rows(*so_status.READ_PROPS, so_status.DATE_PROP), *link_marks)
        order = so_status.order_status(state[m] for m in group)
        if order.cls == so_status.CLOSED:
            classes["closed"] += 1
            continue
        if any(m in fulfilled for m in group):   # a lead shipment or invoice only
            classes["unknown"] += 1
            skipped.note("fulfilment_unsettled", group, read)
            continue
        leads = sorted(lead_twins.get(top, ()))
        if any(state[t].cls == so_status.CLOSED or t in fulfilled for t, _m in leads):
            # a lead (a number match) may be this order's closed record: never judged open
            classes["unknown"] += 1
            skipped.note("closed_in_unsettled_twin", group,
                         [*read, *((t, _markings(m, *link_marks)) for t, m in leads)])
            continue
        if order.cls == so_status.UNKNOWN:
            classes["unknown"] += 1
            skipped.note(order.reason, group, read)
            continue
        if order.cls == so_status.DRAFT:
            classes["draft"] += 1
            continue
        if not any(m in lined for m in group):
            # no record of the order holds a line: no goods are stated on it (a header
            # copied without its lines, or an empty shell), so it is not judged
            classes["unknown"] += 1
            skipped.note(so_status.NO_LINES, group, read)
            continue
        classes["open"] += 1
        wanted = so_status.requested_date([
            (found[m]["props"].get(so_status.DATE_PROP, ()),
             [r.get("value") for n in so_status.OWN_DAYS for r in found[m]["props"].get(n, ())],
             defaults.get(binding[m], ())) for m in group])
        if wanted.day is None:
            if wanted.reason != "required_date_disputed":
                counts["undated"] += 1
            skipped.note(wanted.reason, group, read)
            continue
        required = wanted.day
        counts["evaluated"] += 1
        if required >= as_of:
            continue
        counts["late"] += 1
        late = (as_of - required).days
        by_system: dict[str, set[str]] = defaultdict(set)
        for row in order.open_by:
            by_system[so_status.SYSTEM_OF[row["property"]]].add(str(row["value"]).strip())
        said = "; ".join(f"{system}: {', '.join(sorted(words))}"
                         for system, words in sorted(by_system.items()))
        made = _gap(
            LATE_SO, name, min(r["object_id"] for r in wanted.governing), so_status.TYPE,
            "overdue",
            summary=f"A customer order is open {late} day{'' if late == 1 else 's'} past its "
                    f"required date {required.isoformat()} (open in {said})",
            missing=f"Has the customer order required by {required.isoformat()} shipped, and "
                    "if not, what date can the customer now expect?",
            why="Every record of the order that states a status says it is open, none is "
                "closed, shipped or invoiced, and no shipment or invoice is linked to it, "
                "while its required date has passed.",
            priority="medium", about=(so_status.TYPE, so_status.DATE_PROP),
            groups=[_cites(wanted.governing, *link_marks),
                    *[[cite] for cite in _cites(order.open_by, *link_marks)]],
            extra=_cites(rows(*SO_NUMBERS), *link_marks),
            event={"days": late, "required": required.isoformat()})
        if made is not None:
            made["requires"] = requires
        gaps.append(made)
    result = _result(gaps, not_evaluated=skipped)
    # every order is closed, a draft, judged on its date, or counted unjudged by reason
    judged = classes["closed"] + classes["draft"] + counts["evaluated"]
    result["coverage"] = _coverage(len(members), judged, dict(skipped), unit="sales_orders")
    result["counts"] = {"orders": len(members),
                        "classes": {c: classes.get(c, 0)
                                    for c in ("open", "closed", "draft", "unknown")},
                        **{k: counts.get(k, 0) for k in ("evaluated", "late", "undated")}}
    return result


def _grouped(pairs: Iterable[tuple[Any, Any]]) -> dict[Any, list[Any]]:
    out: dict[Any, list[Any]] = defaultdict(list)
    for key, value in pairs:
        out[key].append(value)
    return out


def _quantity(rows: Sequence[Mapping[str, Any]]) -> tuple[Decimal, str] | None | bool:
    """The one (number, unit) the rows state; None: none; False: they disagree."""
    found = set()
    for row in rows:
        number, unit = pg._decimal(row["value"]), unit_token(row["unit"])
        if number is None or unit is None:
            return False
        found.add((number, unit))
    if not found:
        return None
    return found.pop() if len(found) == 1 else False


def _po_receipt_quantity_mismatch(st: _Store, as_of: date) -> dict[str, Any]:
    name = "po_receipt_quantity_mismatch"
    ordered = st.values(("PurchaseOrderLine",), ("ordered_quantity",))
    received = st.values(("ReceiptLine",), ("received_quantity",))
    src = _po_source(st)
    twins = st.links((RECEIPT_LINKS[0],), SETTLED)
    root = _clusters(((l["from_object"], l["to_object"]) for l in twins), received)
    identity: dict[str, list[str]] = defaultdict(list)
    for link in twins:
        if link["from_object"] in root:
            identity[root[link["from_object"]]].append(link["markings"])
    receives = st.links((RECEIPT_LINKS[1],))
    targets: dict[str, set[str]] = defaultdict(set)
    link_marks: dict[str, list[str]] = defaultdict(list)
    unsettled: dict[str, list[Part]] = defaultdict(list)
    for link in receives:
        if link["from_object"] not in root:
            continue
        if link["tier"] not in CONFIRMING:
            unsettled[link["to_object"]].append((link["from_object"], link["markings"]))
            continue
        targets[root[link["from_object"]]].add(link["to_object"])
        link_marks[root[link["from_object"]]].append(link["markings"])
    members: dict[str, list[str]] = defaultdict(list)
    for object_id, top in root.items():
        members[top].append(object_id)
    skipped = _Skips()
    per_line: dict[str, list[tuple[tuple[Decimal, str], list[Cite]]]] = defaultdict(list)
    broken: set[str] = set(unsettled)
    for top, cluster in sorted(members.items()):
        lines = targets.get(top, set())
        if not lines:
            continue
        rows = [r for m in cluster for r in received[m]["props"].get("received_quantity", [])]
        # what a skip of this receipt read: its records and identity links, their
        # quantities, and the lines they receive (at the linking marks)
        marks = [*identity.get(top, []), *link_marks[top]]
        read = ([(m, _markings(*marks)) for m in cluster] + _parts(rows, *marks)
                + [(line, _markings(*marks)) for line in sorted(lines)])
        if len(lines) > 1:
            skipped.note("receipt_line_ambiguous", cluster, read)
            broken |= lines
            continue
        found = _quantity(rows)
        (line,) = lines
        if found is False:
            skipped.note("sources_unreconciled", cluster, read)
            broken.add(line)
            continue
        if found is not None:
            per_line[line].append((found, _cites(rows, *link_marks[top])))
    for line in sorted(set(unsettled) & set(ordered)):
        # a lead may be its receipt: not evaluated (the lead's receipt is its cause)
        skipped.note("receipt_link_unsettled", [line],
                     [(receipt, _markings(m)) for receipt, m in unsettled[line]])
    gaps = []
    for line, parts in sorted(per_line.items()):
        if line in broken or line not in ordered:
            continue
        rows = ordered[line]["props"].get("ordered_quantity", [])
        want = _quantity(rows)
        # the line's ordered quantities and every receipt quantity read against them
        read = _parts(rows) + [(c[1], c[2]) for _, cites in parts for c in cites]
        if not want:
            skipped.note("sources_unreconciled" if want is False else "no_ordered_quantity",
                         [line], read)
            continue
        if any(unit != want[1] for (_, unit), _ in parts):
            skipped.note("unit_differs", [line], read)
            continue
        got = sum((number for (number, _), _ in parts), Decimal(0))
        unit = want[1]
        shown = (format(got.normalize(), "f"), format(want[0].normalize(), "f"))
        groups = [_cites(rows), *[cites for _, cites in parts]]
        if got > want[0]:
            gaps.append(_gap(
                "po_receipt_quantity_mismatch", name, line, "PurchaseOrderLine", "over",
                summary=f"Received {shown[0]} {unit} against {shown[1]} {unit} ordered",
                missing=f"Why was more received ({shown[0]} {unit}) than ordered "
                        f"({shown[1]} {unit})?",
                why="The receipt lines linked to this PO line total more than its ordered "
                    "quantity, in the same unit.",
                priority="high", about=("PurchaseOrderLine", "ordered_quantity"),
                groups=groups))
            continue
        if got == want[0]:
            continue
        closed = []
        for po in src.po_of.get(line, []):
            if po not in src.pos:
                continue
            # the order by the one reader: its settled twins' status, lines and FM QC log
            found = po_status.read(src, po, dates=False)
            if found.status == po_status.PoStatus(po_status.CLOSED, po_status.RECEIVED):
                closed += [[cite] for cite in _cites(found.evidence, *found.identity_marks,
                                                     *found.marks)]
        if not closed:
            continue   # still receiving (or not known closed): not yet a gap
        gaps.append(_gap(
            "po_receipt_quantity_mismatch", name, line, "PurchaseOrderLine", "short",
            summary=f"Received {shown[0]} {unit} against {shown[1]} {unit} ordered on an "
                    "order recorded as fully received",
            missing=f"Is the shortfall of the PO line ({shown[0]} of {shown[1]} {unit}) "
                    "still coming, or should the order be corrected?",
            why="The order is recorded as received while its linked receipt lines total "
                "less than the ordered quantity.",
            priority="medium", about=("PurchaseOrderLine", "ordered_quantity"),
            groups=[*groups, *closed]))
    return _result(gaps, not_evaluated=skipped)


def _receipt_without_qc(st: _Store, as_of: date, link_types: Sequence[str]
                        ) -> dict[str, Any]:
    name = "receipt_without_qc"
    if not link_types:
        return _result([], status="blocked", reasons=["qc_link_not_declared"])
    receipts = st.values(("Receipt",), ("received_date", "receipt_number"))
    lines_of: dict[str, set[str]] = defaultdict(set)
    line_marks: dict[tuple[str, str], str] = {}
    for link in st.links((RECEIPT_LINKS[2],), CONFIRMING):
        lines_of[link["to_object"]].add(link["from_object"])
        line_marks[(link["to_object"], link["from_object"])] = link["markings"]
    qc: dict[str, set[str]] = defaultdict(set)
    qc_parts: dict[str, list[Part]] = defaultdict(list)   # end -> the QC link's other end
    for link in st.links(tuple(link_types)):
        for end, other in ((link["from_object"], link["to_object"]),
                           (link["to_object"], link["from_object"])):
            qc[end].add(link["tier"])
            qc_parts[end].append((other, link["markings"]))
    skipped = _Skips()
    gaps = []
    for receipt, entry in sorted(receipts.items()):
        dated = [r for r in entry["props"].get("received_date", [])
                 if (d := _day(r["value"])) is not None and d <= as_of]
        if not dated:
            continue
        tiers = set().union(*(qc.get(o, set()) for o in {receipt, *lines_of.get(receipt, ())}))
        if tiers & set(CONFIRMING):
            continue
        if tiers:
            # its cause: the receipt's lines and every QC link on them or on it
            cause = [(other, _markings(m)) for other, m in qc_parts.get(receipt, ())]
            for line in sorted(lines_of.get(receipt, ())):
                joined = line_marks.get((receipt, line))
                cause.append((line, _markings(joined)))
                cause += [(other, _markings(m, joined)) for other, m in qc_parts.get(line, ())]
            skipped.note("qc_link_unsettled", [receipt], cause)
            continue
        gaps.append(_gap(
            "receipt_without_qc", name, receipt, "Receipt", "no_qc",
            summary="A received receipt has no QC record linked to it",
            missing="Was the received material inspected, and where is its QC record?",
            why="No QC record is linked to the receipt or its lines in the store.",
            priority="medium", about=("Receipt", "received_date"), groups=[_cites(dated)],
            extra=_cites(entry["props"].get("receipt_number", []))))
    return _result(gaps, not_evaluated=skipped)


def _order_without_customer_po(st: _Store, coverage: Mapping[str, Any] | None
                               ) -> dict[str, Any]:
    name = "order_without_customer_po"
    if coverage is None:
        return _result([], status="not_configured", reasons=["coverage_not_given"])
    completeness = coverage.get("completeness") or {}
    versions = st.versions("SalesOrder")
    exact = {v["l1_handle"]: v for v in versions}
    stem = {v["l1_handle"].split("@", 1)[0]: v for v in versions}
    held = st.values(("SalesOrder",), ("customer_po",))
    skipped = _Skips()
    gaps = []
    for order in coverage.get("orders") or []:
        if (not isinstance(order, Mapping) or order.get("status") != "absent_in_this_source"
                or order.get("fm_order_entry_holds_customer_po")):
            continue
        citation = order.get("citation")
        found = (exact.get(citation) or stem.get(citation.split("@", 1)[0])
                 if isinstance(citation, str) else None)
        if found is None:
            skipped.note("not_projected")   # no record stands for it
            continue
        if any(r["value"] not in (None, "") for r in
               (held.get(found["object_id"]) or {"props": {}})["props"].get("customer_po", [])):
            continue   # the projection holds one (another source): not absent
        gaps.append(_gap(
            "order_without_customer_po", name, found["object_id"], "SalesOrder",
            "customer_po",
            summary="A sales order holds no customer PO in any field the Portal row carries",
            missing="Which customer PO does this sales order answer to?",
            why="The full scan of the Portal's sales orders found no customer PO on this "
                "order and no linked FM order entry holding one.",
            priority="medium", about=("SalesOrder", "customer_po"),
            groups=[[(found["l1_handle"], found["object_id"], _markings(found["markings"]))]]))
    status = "complete" if completeness.get("complete") is True else "truncated"
    return _result(gaps, status=status, not_evaluated=skipped,
                   reasons=[] if status == "complete" else list(
                       completeness.get("reasons") or ["scan_incomplete"]))


def _expired_quote(st: _Store, as_of: date) -> dict[str, Any]:
    _documents, quotes, _records = pg._roster()
    found = st.values(quotes, ("status", "valid_until", *pg.QUOTE_OUTCOME_FIELDS))
    gaps = []
    for object_id, entry in sorted(found.items()):
        rows = [r for prop_rows in entry["props"].values() for r in prop_rows]
        by_handle = {r["l1_handle"]: r for r in rows}
        items = [{**r, "citation": {"l1_handle": r["l1_handle"]}} for r in rows]
        for item in pg._expired_quote(entry["type"], items, as_of):
            gaps.append(_gap(
                item["kind"], "expired_quote", object_id, entry["type"], item["key"],
                summary=item["summary"], missing=item["missing"], why=item["why"],
                priority=item["priority"], about=item["about"],
                groups=[_cites(by_handle[h] for h in group) for group in item["requires"]]))
    return _result(gaps)


def _quantity_rows(st: _Store) -> list[dict[str, Any]]:
    """Every current quantity value of a record not removed: the ``object_values`` view's
    rows of ``value_type`` quantity, read from its tables quantity kind first (the
    layouts that hold a quantity, then their cells by layout). Through the view the
    planner scanned every value source of the store (51 s on 2.8M). The order is the
    view's scan order: value source, then cell, then the field's place in its layout."""
    if st.scoped:
        found = _scoped_quantity_rows(st)
    else:
        found = st.conn.execute(
        "SELECT s.src, c.field AS cell_field, lf.pos, v.object_id, o.object_type, "
        "f.property, CASE WHEN c.field IS NULL THEN json_extract(c.value, lf.path) "
        "ELSE c.value END AS value, k.unit, k.quantity_role, v.l1_handle, k.markings "
        "FROM value_kinds k CROSS JOIN value_layout_fields lf ON lf.kind = k.kind "
        "CROSS JOIN value_cells c ON c.layout = lf.layout "
        "JOIN value_sources s ON s.src = c.src JOIN object_versions v ON v.rowid = s.ver "
        "JOIN objects o ON o.object_id = v.object_id JOIN value_fields f ON f.field = lf.field "
        "WHERE k.value_type = 'quantity' AND s.superseded_at IS NULL AND NOT "
        + ostore.removed_at_sql("o.object_id")).fetchall()
    found = list(found)
    found.sort(key=lambda r: (r["src"], r["cell_field"] is not None, r["cell_field"] or 0,
                              r["pos"]))
    keep = ("object_id", "object_type", "property", "value", "unit", "quantity_role",
            "l1_handle", "markings")
    return [{key: row[key] for key in keep} for row in found]


def _scoped_quantity_rows(st: _Store) -> list[Any]:
    """``_quantity_rows``' rows of the scope's records (an incremental pass), read from
    each record's own value sources (its version, never a scan of the quantity cells)."""
    found: list[Any] = []
    sql = ("SELECT s.src, c.field AS cell_field, lf.pos, v.object_id, o.object_type, "
           "f.property, CASE WHEN c.field IS NULL THEN json_extract(c.value, lf.path) "
           "ELSE c.value END AS value, k.unit, k.quantity_role, v.l1_handle, k.markings "
           "FROM object_versions v JOIN objects o ON o.object_id = v.object_id "
           "JOIN value_sources s ON s.ver = v.rowid JOIN value_cells c ON c.src = s.src "
           "JOIN value_layout_fields lf ON lf.layout = c.layout "
           "JOIN value_kinds k ON k.kind = lf.kind JOIN value_fields f ON f.field = lf.field "
           "WHERE v.object_id IN (SELECT value FROM json_each(:chunk)) "
           "AND k.value_type = 'quantity' AND s.superseded_at IS NULL AND NOT "
           + ostore.removed_at_sql("o.object_id"))
    ids = st._scope_ids
    for start in range(0, len(ids), SCOPE_CHUNK):
        found.extend(st.conn.execute(sql, {"chunk": json.dumps(ids[start:start + SCOPE_CHUNK])}))
    return found


def _conflicting_quantity(st: _Store, as_of: date) -> dict[str, Any]:
    rows = _quantity_rows(st)
    objects = {row["object_id"] for row in rows}
    identity = [link for link in st.links(_identity_link_types(), SETTLED)
                if link["from_object"] in objects and link["to_object"] in objects]
    root = _clusters(((l["from_object"], l["to_object"]) for l in identity), objects)
    marks: dict[str, list[str]] = defaultdict(list)
    for link in identity:
        marks[root[link["from_object"]]].append(link["markings"])
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(root[row["object_id"]], row["object_type"], row["property"])].append(dict(row))
    disputed = {top: {m["object_id"] for m in members}
                for (top, _t, _p), members in grouped.items()
                if len({m["l1_handle"] for m in members}) >= 2}
    closed = _closed_clusters(st, disputed, {row["object_type"] for row in rows})
    gaps = []
    skipped: Counter[str] = Counter()
    for (top, object_type, prop), members in sorted(grouped.items()):
        if len({m["l1_handle"] for m in members}) < 2:
            continue
        by_handle = {m["l1_handle"]: m for m in members}
        subject = f"{object_type}.{prop}"
        conflict = {"kind": "value_disagreement", "subject": subject, "values": [
            {"value_type": "quantity", "value": m["value"], "unit": unit_token(m["unit"]),
             "quantity_role": m["quantity_role"], "citation": {"l1_handle": m["l1_handle"]}}
            for m in members]}
        items = pg._conflicting_quantities([conflict], skipped)
        if items and top in closed:
            skipped["record_closed"] += len(items)
            continue   # a finished or cancelled record: nothing left to act on
        for item in items:
            gaps.append(_gap(
                item["kind"], "conflicting_quantity", top, object_type, item["key"],
                summary=item["summary"], missing=item["missing"], why=item["why"],
                priority=item["priority"], about=(object_type, prop),
                groups=[_cites((by_handle[h] for h in group), *marks.get(top, []))
                        for group in item["requires"]]))
    return _result(gaps, not_evaluated=skipped)


def _closed_clusters(st: _Store, disputed: Mapping[str, set[str]],
                     types: Iterable[str]) -> set[str]:
    """The clusters (of ``disputed``: root -> its objects holding the disputed values) any
    of whose records' own ``status`` says closed (``proactive_gaps.status_closed``)."""
    owner = {object_id: top for top, members in disputed.items() for object_id in members}
    if not owner:
        return set()
    found = st.values(sorted(types), ("status",), objects=sorted(owner))
    return {owner[object_id] for object_id, entry in found.items()
            if any(pg.status_closed(row["value"])
                   for row in entry["props"].get("status", []))}


def _card_of(object_id: str, object_type: str, props: Mapping[str, Any]) -> dict[str, Any]:
    """A ``plx.object``-shaped card of the store's current values, for the shared rules."""
    return {"object": {"object_id": object_id, "object_type": object_type},
            "values": [{"property": prop, "value": row["value"], "unit": row["unit"],
                        "citation": {"l1_handle": row["l1_handle"]}}
                       for prop, rows in props.items() for row in rows]}


def _unreceived_draft(st: _Store, order: str, order_lines: Sequence[str],
                      lines: Mapping[str, Mapping[str, Any]]) -> bool:
    """``order`` is a BC Draft (its ``bc_status`` read as not issued) none of whose lines
    records a received quantity above 0 (an unreadable one counts as none: a Draft's
    date never decides late on a guess)."""
    values = (st.values((order.split(":", 1)[0],), ("bc_status",), objects=[order])
              .get(order) or {"props": {}})["props"]
    if not po_status.bc_draft({"bc_status": [r["value"] for r in values.get("bc_status", [])]}):
        return False
    for line_id in order_lines:
        for row in (lines.get(line_id) or {"props": {}})["props"].get(pg.LINE_RECEIVED, ()):
            number = po_status._number(row["value"])
            if number is not None and number > po_status.EPS:
                return False
    return True


def _late_orders(st: _Store, as_of: date, only: str | None = None
                 ) -> tuple[list[dict[str, Any]], "_Skips"] | None:
    """Each order with late lines (``pg._late_line``) and the facts its supplier draft
    states, every fact as ``(value, Cite)``: its number, its supplier's name, its
    confirmed delivery date and each late line. ``only``: that one order (the console's
    re-derivation). None when the schema does not declare the rule."""
    order_type = pg._late_target()
    if order_type is None:
        return None
    line_props = (pg.LINE_DATE, pg.LINE_ORDERED, pg.LINE_RECEIVED, pg.LINE_ITEM)
    orders_of: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for link in st.links((pg.LINE_LINK,), CONFIRMING):
        orders_of[link["from_object"]].append((link["to_object"], link["markings"]))
    wanted = None if only is None else sorted(
        line for line, reached in orders_of.items() if any(o == only for o, _m in reached))
    lines = st.values((pg.LINE_TYPE,), line_props, objects=wanted)
    skipped = _Skips()
    late: dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]] = defaultdict(list)
    for line_id, entry in sorted(lines.items()):
        found = pg._late_line(_card_of(line_id, pg.LINE_TYPE, entry["props"]), as_of)
        if found is None:
            if any(len(entry["props"].get(p, ())) > 1 for p in line_props[:3]):
                skipped.note("line_value_disputed", [line_id], _parts(
                    [r for p in line_props[:3] for r in entry["props"].get(p, ())]))
            continue
        reached = {order for order, _marks in orders_of.get(line_id, [])
                   if order.startswith(f"{order_type}:")}
        if len(reached) != 1:
            # unlinked: nothing beyond the line; ambiguous: each order and its link
            skipped.note("line_order_unlinked" if not reached else "line_order_ambiguous",
                         [line_id], [(order, _markings(m)) for order, m in
                                     orders_of.get(line_id, []) if order in reached])
            continue
        (order,) = reached
        if only is not None and order != only:
            continue
        marks = _markings(*[m for o, m in orders_of[line_id] if o == order])
        late[order].append((found, entry, marks))
    # A BC Draft never received against
    # dates nothing, so its lines are never late (a Draft any line of which records a
    # received quantity keeps its lines' dates)
    lines_of_order: dict[str, list[str]] = defaultdict(list)
    for line_id, reached in orders_of.items():
        for order, _m in reached:
            if order in late:
                lines_of_order[order].append(line_id)
    for order in sorted(o for o in late
                        if _unreceived_draft(st, o, lines_of_order[o], lines)):
        skipped.note("draft_dates_nothing", [order],
                     [(line["line_id"], m) for line, _e, m in late[order]])
        del late[order]
    if not late:
        return [], skipped
    picked = sorted(late)
    numbers = st.values((order_type,), (pg.ORDER_NUMBER,), objects=picked)
    supplier_type = pg._supplier_target()
    suppliers: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if supplier_type is not None:
        for link in st.links((pg.SUPPLIER_LINK,), CONFIRMING):
            if link["from_object"] in late:
                suppliers[link["from_object"]].append((link["to_object"], link["markings"]))
    names = st.values((supplier_type,), (pg.SUPPLIER_NAME,), objects=sorted(
        {t for found in suppliers.values() for t, _m in found})) if supplier_type else {}
    confirms: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
    for link in st.links((CONFIRMATION_LINK,), CONFIRMING):
        if link["to_object"] in late:
            confirms[link["to_object"]].append((link["from_object"], link["markings"],
                                                link["tier"] in SETTLED))
    dated = st.values((CONFIRMATION_TYPE,), (CONFIRMED_DATE,), objects=sorted(
        {c for found in confirms.values() for c, _m, _s in found}))
    out = []
    for order in picked:
        facts: dict[str, Any] = {"order": order, "order_type": order_type, "number": None,
                                 "supplier": None, "confirmed": [], "corroborated": [],
                                 "unresolved": []}
        card = _card_of(order, order_type, (numbers.get(order) or {"props": {}})["props"])
        number = pg._one_text(card, pg.ORDER_NUMBER)
        if number is None:
            facts["unresolved"].append("order_number:not_in_record")
        else:
            facts["number"] = (number[0], _cites([r for r in numbers[order]["props"][
                pg.ORDER_NUMBER] if r["l1_handle"] == number[1]])[0])
        if supplier_type is None:
            facts["unresolved"].append("supplier:link_undeclared")
        else:
            reached = {t: m for t, m in suppliers.get(order, [])}
            if len(reached) != 1 or not next(iter(reached)).startswith(f"{supplier_type}:"):
                # none, two, or one that is not a supplier record: no one supplier
                facts["unresolved"].append("supplier:not_linked" if not reached
                                           else "supplier:ambiguous")
            else:
                ((one, link_marks),) = reached.items()
                card = _card_of(one, supplier_type, (names.get(one) or {"props": {}})["props"])
                name = pg._one_text(card, pg.SUPPLIER_NAME)
                if name is None:
                    facts["unresolved"].append("supplier:name_not_in_record")
                else:
                    facts["supplier"] = (name[0], _cites([
                        r for r in names[one]["props"][pg.SUPPLIER_NAME]
                        if r["l1_handle"] == name[1]], link_marks)[0])
        for confirmation, link_marks, settled in sorted(confirms.get(order, [])):
            rows = (dated.get(confirmation) or {"props": {}})["props"].get(CONFIRMED_DATE, [])
            days = {_day(r["value"]) for r in rows}
            if len(days) != 1 or None in days:
                facts["unresolved"].append("confirmed_date:unreadable")
                continue
            # only a settled confirmation is told to the supplier as theirs; a corroborated
            # one stays an internal note
            facts["confirmed" if settled else "corroborated"].append(
                (next(iter(days)).isoformat(), _cites(rows[:1], link_marks)[0]))
        facts["lines"] = []
        for line, entry, marks in late[order]:
            props = entry["props"]

            def cite(prop: str, handle: str | None) -> list[Cite]:
                return _cites([r for r in props.get(prop, []) if r["l1_handle"] == handle],
                              marks)
            facts["lines"].append({
                **line, "date_cite": cite(pg.LINE_DATE, line["date_handle"]),
                "quantity_cites": cite(pg.LINE_ORDERED, line["ordered"][2])
                + cite(pg.LINE_RECEIVED, line["received"][2]),
                "item_cite": cite(pg.LINE_ITEM, line["item"][1]) if line.get("item") else [],
                "evidence": {prop: _cites([r for r in props[prop]
                                           if r["l1_handle"] in line["handles"]], marks)
                             for prop in (pg.LINE_DATE, pg.LINE_ORDERED, pg.LINE_RECEIVED)}})
        out.append(facts)
    return out, skipped


def _worded(facts: Mapping[str, Any], roles: Sequence[str],
            readable: Callable[[Sequence[Cite]], set[str]] | None = None
            ) -> dict[str, Any] | None:
    """The supplier draft of one order's facts as a reader may read them: a line is used
    only cited whole (its date and both quantities), a withheld item name is left out, a
    withheld number, supplier or confirmation is named in ``unresolved`` (a withheld
    confirmation drops the confirmed date: the reader cannot see whether it agrees). None
    when no late line is left."""
    every = [c for line in facts["lines"] for c in (*line["date_cite"],
                                                    *line["quantity_cites"],
                                                    *line["item_cite"])]
    every += [f[1] for f in (facts["number"], facts["supplier"]) if f is not None]
    every += [c for _day_text, c in (*facts["confirmed"], *facts["corroborated"])]
    every += [facts["due"][2]] if facts.get("due") else []
    shown = {c[0] for c in every} if readable is None else readable(every)
    opens = lambda cites: bool(cites) and all(c[0] in shown for c in cites)  # noqa: E731
    lines = []
    for line in facts["lines"]:
        if not opens(line["date_cite"] + line["quantity_cites"]):
            continue
        lines.append({**line, "item": line["item"] if opens(line["item_cite"]) else None})
    if not lines:
        return None
    unresolved = list(facts["unresolved"])
    told: dict[str, Any] = {}
    for name in ("number", "supplier"):
        fact = facts[name]
        if fact is not None and not opens([fact[1]]):
            unresolved.append(f"{'order_number' if name == 'number' else name}:withheld")
            fact = None
        told[name] = None if fact is None else (fact[0], fact[1][0])
    confirmed = facts["confirmed"]
    told["confirmed"] = None
    told["internal_notes"] = []
    if confirmed and not opens([c for _d, c in confirmed]):
        unresolved.append("confirmed_date:withheld")
    elif len({d for d, _c in confirmed}) > 1:
        unresolved.append("confirmed_date:conflict")
    elif confirmed and "confirmed_date:unreadable" not in unresolved:
        told["confirmed"] = (confirmed[0][0], confirmed[0][1][0])
    corroborated = facts["corroborated"]
    if (corroborated and told["confirmed"] is None and len({d for d, _c in corroborated}) == 1
            and opens([c for _d, c in corroborated])):
        told["internal_notes"] = [{"note": "confirmed_date:corroborated_only",
                                   "value": corroborated[0][0],
                                   "citation": corroborated[0][1][0]}]
    due = facts.get("due")
    told["due"] = (due[0], due[1], due[2][0]) if due is not None and opens([due[2]]) else None
    told["unresolved"] = unresolved
    return late_order_draft(lines, told, roles)


def _amount(number: Decimal, unit: str | None) -> str:
    shown = format(number.normalize(), "f")
    return f"{shown} {pg._clip(' '.join(unit.split()), 20)}" if unit and unit.strip() else shown


def late_order_draft(lines: Sequence[Mapping[str, Any]], facts: Mapping[str, Any],
                     roles: Sequence[str]) -> dict[str, Any]:
    """The supplier message asking it to confirm delivery of one order's late lines, worded
    from cited facts only, never sent (``DRAFT_STATUS``, ``sent`` False). It names the
    purchase order, each late line (its item, ordered and received quantities) and the
    governing date: the supplier's confirmed delivery date when exactly one is readable
    (``date_basis`` ``confirmed``), else each line's expected receipt date worded "our
    expected date" (``expected``). Its citations are a side list: each stated fact one
    ``facts`` entry with its handle, no marker in the text; at most ``MAX_DRAFT_LINES``
    lines and ``MAX_DRAFT_CITATIONS`` handles (the catalog's draft citation bound)."""
    citations: list[str] = []
    listed: list[dict[str, Any]] = []

    def fact(name: str, value: str, handle: str, **extra: Any) -> None:
        if handle not in citations:
            citations.append(handle)
        listed.append({"fact": name, "value": value, **extra, "citation": handle})

    number, supplier, confirmed = facts.get("number"), facts.get("supplier"), \
        facts.get("confirmed")
    if supplier is not None:
        to = {"kind": "supplier", "name": supplier[0], "citation": supplier[1]}
        to_text = supplier[0]
        fact("supplier", supplier[0], supplier[1])
    else:
        role = roles[0] if roles else None
        to = {"kind": "answerer_role", "role": role,
              "reason": "the order's supplier name is not readable here"}
        to_text = (f"{role} (answerer role; the order's supplier name is not readable here)"
                   if role else "(no supplier name or answerer role is known)")
    if number is not None:
        fact("order_number", number[0], number[1])
        order_text, subject = (f"purchase order {number[0]}",
                               f"Please confirm the delivery date for purchase order {number[0]}")
    else:
        order_text, subject = ("our purchase order (its number is not readable here)",
                               "Please confirm the delivery date for our purchase order")
    due = facts.get("due")
    if confirmed is not None:
        fact("confirmed_delivery_date", confirmed[0], confirmed[1])
        basis = (f"Your confirmation gives the delivery date {confirmed[0]}, and these lines "
                 "are not fully received:\n")
    elif due is not None:
        fact("due_date", due[0], due[2], basis=due[1])
        words = {"requested": "our requested date", "vendor_eta": "your confirmed date",
                 "lead_time": "the order's lead time, due"}[due[1]]
        basis = f"These lines are past {words} {due[0]} and not fully received:\n"
    else:
        basis = "These lines are past our expected date and not fully received:\n"
    head = (f"{DRAFT_LABEL}\nTo: {to_text}\nSubject: {subject}\n\nHello,\n\n"
            f"Please confirm the delivery date for {order_text}. {basis}")
    tail = "\nPlease reply with the date each line will be delivered."
    ordered = sorted(lines, key=lambda line: (line["day"], str(line.get("line_id"))))
    body, shown = "", 0
    for line in ordered[:MAX_DRAFT_LINES]:
        mark = len(listed), list(citations)
        line_id = str(line.get("line_id") or "")
        item = line.get("item")
        if item:
            fact("line_item", item[0], item[1], line_id=line_id)
        name = item[0] if item else "a line (the record names no item)"
        when = ""
        if confirmed is None and due is None:
            day = line["day"].isoformat()
            fact("expected_receipt", day, line["date_handle"], line_id=line_id)
            when = f"our expected date {day}, "
        (q_o, u_o, h_o), (q_r, u_r, h_r) = line["ordered"], line["received"]
        fact("ordered_quantity", format(q_o.normalize(), "f"), h_o, line_id=line_id, unit=u_o)
        fact("received_quantity", format(q_r.normalize(), "f"), h_r, line_id=line_id, unit=u_r)
        row = (f"- {name}: {when}ordered {_amount(q_o, u_o)}, "
               f"received {_amount(q_r, u_r)}\n")
        if (len(head) + len(body) + len(row) + len(tail) + 60 > MAX_DRAFT_TEXT
                or len(citations) > MAX_DRAFT_CITATIONS):
            del listed[mark[0]:]   # this line does not fit: neither its text nor its facts
            citations[:] = mark[1]
            break
        body += row
        shown += 1
    if shown < len(ordered):
        body += f"- and {len(ordered) - shown} more late lines, not listed here\n"
    return {"label": DRAFT_LABEL, "status": DRAFT_STATUS, "sent": False, "to": to,
            "subject": pg._clip(subject, 200), "text": pg._clip(head + body + tail,
                                                                 MAX_DRAFT_TEXT),
            "date_basis": "confirmed" if confirmed is not None else
            ("due" if due is not None else "expected"),
            "internal_notes": [dict(n) for n in facts.get("internal_notes") or ()],
            "facts": listed, "citations": citations, "lines_late": len(ordered),
            "lines_listed": shown, "unresolved": list(facts.get("unresolved") or ())}


def _vendor_facing(src: po_status.StoreSource, po: po_status.PoRead,
                   facts: dict[str, Any]) -> None:
    """The order number the supplier knows (the number on the document we sent: a
    FileMaker-era order's FileMaker PO number, held by its FileMaker and Portal copies),
    in place of a copy's own internal number, when exactly one is readable and cited."""
    row = po_status.vendor_facing(
        (r.get("value"), r) for m in (*po.members, *po.date_twins)
        for r in src.props(m).get(po_status.VENDOR_NUMBER_PROP, []))
    if row is not None:
        facts["number"] = (str(row["value"]).strip(), _cites([row])[0])


def _late_order(st: _Store, as_of: date) -> dict[str, Any]:
    """``late_order``: late lines (``pg._late_line``) grouped by their one order, each gap
    with its supplier draft (``late_order_draft``) as the store holds the facts."""
    name = "late_order"
    found = _late_orders(st, as_of)
    if found is None:
        return _result([], status="not_configured", reasons=["rule_not_declared"])
    orders, skipped = found
    gaps = []
    src = _po_source(st)
    for facts in orders:
        # the order is late only by the one purchase-order definition (a receipt in any
        # copy closes it; a default date dates nothing): a late line of an order that
        # rule does not call late is no claim (one rule, never a second copy of it)
        po = po_status.read(src, facts["order"], as_of)
        if po.verdict is None or po.verdict[0] != po_status.LATE:
            skipped.note("order_not_late", [facts["order"]],
                         [(facts["order"], _markings(*po.identity_marks))])
            continue
        _vendor_facing(src, po, facts)
        by_lines = all(isinstance(r, Mapping) and r.get("property") == pg.LINE_DATE
                       for r in po.need.governing)
        due_cites = _cites(po.need.governing, *po.need.marks)
        if due_cites and not by_lines:
            # dated by its header or a labelled source: the draft states that one date
            facts["due"] = (po.need.hi.isoformat(), po.need.basis, due_cites[0])
        # a line whose own date is a default (``po_status``'s date provenance) is never
        # stated as its expected date
        defaulted = {(r.get("object_id"), str(r.get("value"))) for r in po.need.defaulted
                     if isinstance(r, Mapping)}
        facts["lines"] = [line for line in facts["lines"] if not by_lines
                          or (line.get("line_id"), line["day"].isoformat()) not in defaulted]
        if not facts["lines"]:
            skipped.note("order_not_late", [facts["order"]],
                         [(facts["order"], _markings(*po.identity_marks))])
            continue
        rows = facts["lines"]
        groups = [[c for line in rows for c in line["evidence"][prop]]
                  for prop in (pg.LINE_DATE, pg.LINE_ORDERED, pg.LINE_RECEIVED)]
        extra = [c for line in rows for c in line["item_cite"]]
        extra += [f[1] for f in (facts["number"], facts["supplier"]) if f is not None]
        extra += [c for _d, c in facts["confirmed"]]
        count = len(rows)
        noun = "line" if count == 1 else "lines"
        due = po.need.hi.isoformat()
        gap = _gap(
            "late_order", name, facts["order"], facts["order_type"], "late_lines",
            summary=f"{count} {noun} not fully received past {_DUE_WORDS[po.need.basis]} "
                    f"{due}",
            missing="Confirm a new delivery date for the lines not yet received",
            why=pg._clip(f"The order is past {_DUE_WORDS[po.need.basis]} {due} and {count} "
                         f"{noun} still have a received quantity below the ordered "
                         "quantity in every copy of the order, as cited."),
            priority="high", about=(pg.LINE_TYPE, pg.LINE_DATE), groups=groups,
            extra=list({c[0]: c for c in extra}.values()))
        if gap is not None:
            draft = _worded(facts, gap["follow_up"]["who_can_answer"])
            if draft is not None:
                gap["follow_up"]["draft"] = draft
            gaps.append(gap)
    return _result(gaps, not_evaluated=skipped)


def order_draft(store_path: Any, order_id: str, as_of: date, *,
                may_read: Callable[[str], bool], held: Iterable[str],
                decide: Callable[[Sequence[str]], Mapping[str, Any]]) -> dict[str, Any] | None:
    """The supplier draft of one order (``late_order_draft``), derived again from the store
    for one reader: a fact is stated only when its record sits in an audience
    ``may_read`` admits, at markings the reader ``held`` covers, and ``decide`` (None per
    handle it may open) lets it open, as ``read_inbox`` shows a gap. The console words a
    proposed draft with this, never from the text or answerers a client sent: the
    answerer roles are the gap's own (``pg._who``, as ``_gap`` resolves them). None when
    the order has no late line the reader may open whole (or the rule is not declared)."""
    st = _Store(Path(store_path))
    try:
        found = _late_orders(st, as_of, only=order_id)
        if not found or not found[0]:
            return None
        (facts,) = found[0]
        allowed = set(ostore.allowed_marking_sets(held))

        def readable(cites: Sequence[Cite]) -> set[str]:
            ids = sorted({c[1] for c in cites if isinstance(c[1], str)})
            seen: dict[str, bool] = {}
            for row in st.conn.execute(
                    "SELECT o.object_id, o.audience_key, v.markings FROM objects o JOIN "
                    "object_versions v ON v.object_id = o.object_id AND v.superseded_at IS "
                    "NULL WHERE o.object_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(ids),)):
                try:
                    ok = may_read(row["audience_key"]) is True
                except Exception:  # noqa: BLE001 - a failing grant check denies
                    ok = False
                seen[row["object_id"]] = (seen.get(row["object_id"], True) and ok
                                          and row["markings"] in allowed)
            open_ = [c for c in cites if seen.get(c[1]) is True and c[2] in allowed]
            handles = sorted({c[0] for c in open_})
            refused = {h for h, why in decide(handles).items() if why is not None} \
                if handles else set()
            return {c[0] for c in open_ if c[0] not in refused}

        first = facts["lines"][0]["evidence"][pg.LINE_DATE][0][0]
        roles = pg._who(facts["order_type"], {"about": (pg.LINE_TYPE, pg.LINE_DATE),
                                              "handles": [first]})
        return _worded(facts, roles, readable)
    finally:
        st.close()


def _lateness_audiences(st: _Store) -> list[str]:
    """Every audience a reader needs for ``plx.process`` to decide a purchase order's
    lateness for it, as the tool decides a source is withheld (a Portal relation's
    ``api:`` key, a BC relation's stored ``bc:<company>/`` keys or, none stored, the
    family-wide ``bc:*``; else ``<family>:*`` and the family's stored keys;
    item-ACL sources are unproven for every reader alike): over each binding the
    ``po_delivery_window`` graph names and each binding of the link types it walks, plus
    the audience of every stored record of those bindings."""
    from ontology.schema import load_schema
    from operating.ontology_tools import _AUDIENCE_SOURCE, _ITEM_ACL_AUDIENCES

    schema = load_schema()
    strings: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            strings.add(node)
        elif isinstance(node, Mapping):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(LATENESS_GRAPH.read_text(encoding="utf-8")))
    bindings = {s for s in strings if s in schema.bindings}
    for link in (schema.link_types[s] for s in strings if s in schema.link_types):
        for lb in link.get("bindings", ()):
            bindings |= {lb.get("from_binding"), lb.get("to_binding"),
                         (lb.get("via") or {}).get("binding")}
    bindings.discard(None)
    row = st.meta("audience_keys")
    stored = set(json.loads(row)) if row else set()
    stored |= st.audience_keys()
    prefix_of = {source: prefix for prefix, source in _AUDIENCE_SOURCE.items()}
    need: set[str] = set()
    for bid in sorted(bindings):
        if bid not in schema.bindings:
            raise ValueError(f"lateness_binding_unknown:{bid}")   # fail closed
        tid, binding = schema.bindings[bid]
        source, relation = binding["source"], binding["relation"]
        prefix = prefix_of.get(source)
        if source == "portal_api":
            need.add(f"api:{relation}")
        elif source == "bc":
            # per company where the store holds that relation's companies; else the tool
            # asks whether the grant covers the whole family: only a family-wide pattern
            # grants the literal ``bc:*`` key
            company = {k for k in stored if k.startswith("bc:") and k.endswith(f"/{relation}")}
            need |= company or {"bc:*"}
        elif prefix is not None and prefix not in _ITEM_ACL_AUDIENCES:
            need |= {f"{prefix}:*"} | {k for k in stored if k.startswith(f"{prefix}:")}
        prefix_id = f"{tid}:{bid}:"
        need |= {r[0] for r in st.conn.execute(
            "SELECT DISTINCT audience_key FROM objects WHERE object_type = ? AND "
            "substr(object_id, 1, ?) = ?", (tid, len(prefix_id), prefix_id))}
    return sorted(need)


# The audience prefixes of the record systems a lateness verdict decides on.
_DECIDING_AUDIENCES = ("api:", "bc:")


def _reader_judges_lateness(st: _Store, may_read: Callable[[str], bool] | None) -> bool:
    """Whether the run principal (``may_read``: its grant; None: not checked) reads every
    record-system audience (Portal ``api:``, ``bc:``: the status, lines, requested-by dates
    and receipts a verdict decides on) the ``po_delivery_window`` graph reads that the
    store holds records of (``_lateness_audiences``; a source with no record here blinds
    no verdict). A supplier promise read from a document source (mail, SharePoint: item
    permissions, gated per message) never decides late and is a stated caveat
    (``promises_unproven``), so it is not required here."""
    if may_read is None:
        return True
    row = st.meta("audience_keys")
    held = set(json.loads(row)) if row else set()
    held |= st.audience_keys()
    return all(may_read(key) for key in _lateness_audiences(st)
               if key in held and key.startswith(_DECIDING_AUDIENCES))


# A gap whose date is a supplier's promise says, to a reader who may not read every source
# a promise sits in (or when the run's own reader could not see every promise), that a
# later promise it cannot see may exist.
PROMISE_CAVEAT = "later_promise_may_exist"
PROMISE_CAVEAT_TEXT = " (a later promise you cannot see may exist)"


def _promise_audiences(st: _Store) -> list[str]:
    """Every audience a supplier's promise may sit in: the stored supplier confirmations'
    and the mailboxes' (a promise is read from the carrying message too)."""
    row = st.meta("audience_keys")
    keys = {k for k in (json.loads(row) if row else ()) if isinstance(k, str)}
    keys |= st.audience_keys()
    found = {r[0] for r in st.conn.execute(
        "SELECT DISTINCT audience_key FROM objects WHERE object_type = ?",
        (po_status.CONFIRMATION_TYPE,))}
    return sorted(found | {k for k in keys if k.startswith("mail:")})


def _promise_caveat(gap: dict[str, Any] | None, unless: Sequence[str], always: bool) -> None:
    """Record on ``gap`` the audiences a reader must read for the promise it rests on to
    be every promise there is (``read_inbox`` adds the caveat for any other reader;
    ``always``: the run's own reader could not see every promise either)."""
    if gap is not None:
        gap["follow_up"]["promise_caveat"] = {"unless": sorted(set(unless)),
                                              "always": bool(always)}


def _caveat_all(gap: dict[str, Any] | None, *, unless: Sequence[str]) -> None:
    _promise_caveat(gap, unless, False)


def _line_cites(verdict: Any, chain: Any) -> list[Mapping[str, Any]]:
    """The cited per-item promise the chain's line is judged by, when its own promise
    decides (``impact_chain.Late.own_promise``)."""
    own = verdict.own_promise(chain.line)
    return list(own.cites) if own is not None else []


def _line_event(own: Any, chain: Any, required: date | None) -> dict[str, Any]:
    """The evidence trail of a per-line verdict: the PO line, how it was matched to the
    confirmation's item, its promised date and the order's required date."""
    if own is None or own.promised is None:
        return {}
    return {"line": chain.line, "matched_by": own.matched_by,
            "line_promised": own.promised.isoformat(),
            **({"required": required.isoformat()} if required is not None else {})}


def _orders_at_risk(st: _Store, as_of: date, call: Callable | None, bound: int,
                    first: Iterable[str] = (), many: Callable | None = None
                    ) -> dict[str, Any]:
    """``orders_at_risk``: one gap per customer order a late PO puts at risk, and per late
    PO one ``po_impact`` gap (the orders at risk it feeds, never money) and, when every one
    of those orders has a stated margin in one currency, one ``po_impact_margin`` gap with
    the margin at stake whose every figure is its own finance-marked citation group (only a
    reader holding finance, who may open every figure, sees it). ``first``: the purchase
    orders the late rules found, walked first within ``bound``. Each gap's event names the
    chain's ends its groups cite (``pos``: the late PO an order's worst chain starts from;
    ``orders``: the orders a PO's impact reaches), so a reader's list joins a late PO to
    the customer orders it holds up only among gaps that reader may see. ``many``: the
    run's process pool (``run(pool=)``): every PO reaching an assembly is asked, not
    ``bound`` of them; one the time left did not reach is ``po_not_checked``."""
    if call is None:
        return _result([], status="not_configured", reasons=["tool_caller_not_given"])
    from . import impact_chain as ic

    found = ic.scan(st, call, bound=None if many is not None else bound,
                    margin_values=ic.store_margin_values(st),
                    landed_values=ic.investigate_landed_values(call), first=frozenset(first),
                    call_many=many)
    risky = found["assessment"].at_risk
    requires = _lateness_audiences(st) if risky else []
    late = found["late"]
    handles = {v["citation"]["l1_handle"] for order in risky
               for v in late[order.worst].cites}
    # per line: the confirmation's cited per-item promise of the line each chain leaves by
    handles |= {v["citation"]["l1_handle"] for order in risky for chain in order.chains
                for v in _line_cites(late[chain.po], chain)}
    handles |= {ref for order in risky for ref in order.required_from}
    records = {r for order in risky for chain in order.chains if chain.po == order.worst
               for r in chain.records}
    by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
    impact_versions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for version in st.versions(objects=records | {r for order in risky for chain in order.chains
                                                  for r in chain.records}):
        impact_versions[version["object_id"]].append(version)
        if version["object_id"] in records:
            by_object[version["object_id"]].append(version)
    handles |= {v["citation"]["l1_handle"] for order in risky for chain in order.chains
                for v in late[chain.po].cites}
    handles |= {r for margin in found["margins"].values() for f in margin.figures
                for r in (f.refs or (f.ref,))}
    handles |= {ref for order in risky for chain in order.chains for ref in chain.refs}
    held = {v["l1_handle"]: v for v in st.versions(handles=handles)}

    def cite_value(fig: Any) -> Cite:
        """A margin figure's citation at its value's own markings (finance) joined with its
        record's: a reader who does not hold finance never sees the money claim."""
        version = held.get(fig.ref)   # a handle the store does not hold: shown to nobody
        return ((fig.ref, version["object_id"], _markings(version["markings"], fig.markings))
                if version else (fig.ref, None, UNREADABLE))

    def cite(handle: str) -> Cite:
        version = held.get(handle)   # a handle the store does not hold: shown to nobody
        return ((handle, version["object_id"], _markings(version["markings"])) if version
                else (handle, None, UNREADABLE))

    unless = _promise_audiences(st) if risky else []
    gaps = []
    skipped: Counter[str] = Counter()
    for order in risky:
        chain = order.chain_of(order.worst)
        verdict = late[order.worst]
        own = verdict.own_promise(chain.line)
        into: dict[str, list[str]] = defaultdict(list)
        for edge in chain.edges:
            into[edge.frm].append(edge.markings)
            into[edge.to].append(edge.markings)
        # each lateness value its own group: a reader who may not open one of them never
        # sees the verdict that rests on it
        groups = [[cite(v["citation"]["l1_handle"])] for v in late[order.worst].cites]
        # the line's own promise: the confirmation's cited per-item date, its own group
        groups += [[cite(v["citation"]["l1_handle"])] for v in _line_cites(verdict, chain)]
        groups += [[(v["l1_handle"], record, _markings(v["markings"], *into[record]))
                    for v in by_object.get(record, ())] for record in chain.records]
        groups.append([cite(ref) for ref in order.required_from])
        # a lead's own basis (the planned date it was paired by) is cited too
        groups += [[cite(ref)] for ref in chain.refs]
        pos = len({c.po for c in order.chains})
        noun = "purchase order" if pos == 1 else "purchase orders"
        firm = chain.tier == "firm"
        how = ("an assembly order made or allocated for this order (recorded links, each "
               "cited)" if firm else
               f"an assembly order tied to this order only by a lead ({ic.CHAIN_BASES[chain.basis]}; "
               "each record it rests on cited)")
        gap = _gap(
            "order_at_risk", "orders_at_risk", order.key, "SalesOrder", "at_risk",
            summary=("" if firm else "Lead, not firm: ")
                    + f"{order.days} days at risk from {pos} late {noun} (days at risk is a "
                    "lower bound: no assembly lead time is recorded)",
            missing="Confirm whether the order's required date still holds, or when the "
                    "late purchase order's material will arrive"
                    + ("" if firm else ", and whether this assembly order is made for it"),
            why=pg._clip(f"The item of a late purchase order is consumed by {how}: material "
                         f"{order.material.isoformat()} ({order.material_how}) against the "
                         f"required date {order.required.isoformat()}."),
            priority=("high" if order.days >= 7 else "medium") if firm else
                     ("medium" if order.days >= 7 else "low"),
            about=("SalesOrder", "required_date"), groups=groups,
            event={"days": order.days, "orders_affected": 1, "pos": [order.worst],
                   "material": order.material.isoformat(),
                   **({"required": order.required.isoformat()} if order.required else {}),
                   **_line_event(own, chain, order.required)})
        if gap is None:
            skipped["order_at_risk_uncited"] += 1
        else:
            gap["requires"] = requires
            if verdict.rests_on_promise(chain.line):
                _promise_caveat(gap, unless, verdict.unseen_promise)
            gaps.append(gap)
    by_key = {order.key: order for order in risky}
    for impact in found["po_impacts"]:
        orders = [by_key[k] for k in impact["orders"]]
        if not orders:
            continue
        po = impact["po"]
        # the PO's lateness values, then every record of each chain from it, each its own
        # group: a reader who may not open one never sees the claim
        groups = [[cite(v["citation"]["l1_handle"])] for v in late[po].cites]
        for order in orders:
            chain = order.chain_of(po)
            groups += [[cite(v["citation"]["l1_handle"])] for v in _line_cites(late[po], chain)]
            into: dict[str, list[str]] = defaultdict(list)
            for edge in chain.edges:
                into[edge.frm].append(edge.markings)
                into[edge.to].append(edge.markings)
            groups += [[(v["l1_handle"], record, _markings(v["markings"], *into[record]))
                        for v in impact_versions.get(record, ())] for record in chain.records]
            groups.append([cite(ref) for ref in order.required_from])
        n = len(orders)
        noun = "sales order" if n == 1 else "sales orders"
        worst = max(o.days for o in orders)
        missing = ("Confirm when the late purchase order's material will arrive, or whether "
                   "the sales orders' required dates still hold")
        leads = sum(1 for o in orders if o.chain_of(po).tier != "firm")
        groups += [[cite(ref)] for o in orders for ref in o.chain_of(po).refs]
        why = pg._clip(f"An item of this purchase order is consumed by assembly orders made or "
                       f"allocated for {n} {noun} at risk (recorded links, each cited"
                       + (f"; {leads} of them tied by a lead only, not firm: inferred by "
                          "item and date or mentioned in a document" if leads else "")
                       + f"); the worst is {worst} days at risk (a lower bound: no assembly "
                       "lead time is recorded).")
        priority = "high" if worst >= 7 else "medium"
        plain = _gap("po_impact", "orders_at_risk", po, "PurchaseOrder", "impact",
                     summary=f"affects {n} {noun}, worst {worst} days at risk",
                     missing=missing, why=why, priority=priority,
                     about=("PurchaseOrder", "lateness"), groups=groups,
                     event={"days": worst, "orders_affected": n,
                            "orders": [o.key for o in orders]})
        if plain is None:
            skipped["po_impact_uncited"] += 1
            continue
        plain["requires"] = requires
        promised = any(late[po].rests_on_promise(o.chain_of(po).line) for o in orders)
        if promised:
            _promise_caveat(plain, unless, late[po].unseen_promise)
        gaps.append(plain)
        margin = impact["margin"]
        if margin is None or impact["orders_with_margin"] != n:
            continue   # the money claim needs every order's margin: never a part as the whole
        figures = [f for order in orders for f in found["margins"][order.key].figures]
        # a landed-cost engine figure: each cited value it was computed from is its own
        # finance-marked group (a reader who may not open one never sees the claim)
        value_groups = [[cite_value(f)] if not f.refs else [cite_value(ic.Figure(
            f.amount, f.currency, f.unit, ref, f.object_id, f.markings))] for f in figures
            for ref in (f.refs or (f.ref,))]
        money = _gap("po_impact_margin", "orders_at_risk", po, "PurchaseOrder", "margin",
                     summary=f"affects {n} {noun}, {margin['amount']} {margin['currency']} "
                             f"margin at stake ({margin['basis']}), worst {worst} days at "
                             "risk",
                     missing=missing, why=pg._clip(why + " " + ic.MARGIN_BASIS + "."),
                     priority=priority, about=("PurchaseOrder", "lateness"),
                     groups=groups + value_groups,
                     event={"days": worst, "orders_affected": n,
                            "amount": {"value": str(margin["amount"]),
                                       "currency": margin["currency"],
                                       "basis": "margin_at_stake"}})
        if money is None:
            skipped["po_impact_margin_uncited"] += 1
        else:
            money["requires"] = requires
            if promised:
                _promise_caveat(money, unless, late[po].unseen_promise)
            gaps.append(money)
    if found["lateness_undetermined"]:
        skipped["lateness_undetermined"] = found["lateness_undetermined"]
    if found["pos_not_checked"]:
        skipped["po_not_checked"] = found["pos_not_checked"]
    unread = found.get("pos_not_checked_unread", 0)
    failed = found.get("pos_not_checked_failed", 0)
    cut_by_bound = found["pos_not_checked"] - unread - failed
    result = _result(gaps, status="truncated" if found["truncated"] else "complete",
                     reasons=(["impact_bound"] if cut_by_bound else [])
                     + ([TIME_BUDGET] if unread else [])
                     + (["worker_failed"] if failed else []),
                     not_evaluated=skipped)
    total = found.get("pos_reaching_assembly", found["pos_checked"] + found["pos_not_checked"])
    result["coverage"] = _coverage(
        total, total - found["pos_not_checked"] - found["lateness_undetermined"],
        {"lateness_undetermined": found["lateness_undetermined"],
         **({"impact_bound": cut_by_bound} if cut_by_bound else {}),
         **({TIME_BUDGET: unread} if unread else {}),
         **({"worker_failed": failed} if failed else {})},
        unit="purchase_orders_reaching_an_assembly")
    result["finding"] = {"orders_at_risk": len(risky),
                         "orders_at_risk_by_tier": found["orders_at_risk_by_tier"],
                         "late_pos": found["late_pos_with_orders_at_risk"],
                         "text": ic.headline(len(risky),
                                             found["late_pos_with_orders_at_risk"])}
    return result


LATE_FOUND_KINDS = frozenset({"late_po", "late_order", "unconfirmed_po", "eta_slipped"})


def _late_found(results: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """The purchase orders this run's earlier rules found late, unconfirmed or slipping
    (their gaps' subjects): the impact walk asks their verdicts first."""
    return {str(gap.get("object_id")) for result in results.values()
            for gap in result.get("gaps") or ()
            if gap.get("kind") in LATE_FOUND_KINDS and gap.get("object_id")}


def _card_rules(st: _Store, as_of: date, call: Callable | None, bound: int,
                many: Callable[[str, Sequence[Any]], list[Any]] | None = None
                ) -> dict[str, Any]:
    """``missing_in_record`` and attested conflicts on the attesting documents' cards (the
    newest ``bound``). ``many``: the run's process pool reads the cards side by side (one
    the time left did not reach is not read: the budget marks the rule truncated)."""
    if call is None:
        return _result([], status="not_configured", reasons=["tool_caller_not_given"])
    documents = [name for name, _field in pg._roster()[0]]
    ids = [row[0] for row in st.conn.execute(
        "SELECT o.object_id FROM objects o WHERE o.object_type IN (SELECT value FROM "
        "json_each(?)) AND NOT " + ostore.removed_at_sql("o.object_id")
        + " ORDER BY o.first_recorded_at DESC, o.object_id LIMIT ?",
        (json.dumps(documents), bound + 1))]
    unread: Counter[str] = Counter()
    skipped = _Skips()
    found: list[tuple[dict[str, Any], list[list[str]]]] = []
    asked = [{"object_id": object_id, "response_format": "detailed"} for object_id in ids[:bound]]
    pooled = many("plx.object", asked) if many is not None else None
    for n, object_id in enumerate(ids[:bound]):
        if pooled is None:
            answer = call("plx.object", asked[n])
        else:
            from . import proactive_pool
            answer = pooled[n]
            if answer is proactive_pool.NOT_READ:
                continue   # the time left did not reach it (``Budget.pooled`` noted it)
        error = answer.get("error") if isinstance(answer, Mapping) else {"code": "error"}
        if error:
            code = error.get("code") if isinstance(error, Mapping) else None
            unread[code if isinstance(code, str) else "error"] += 1
            skipped.note(f"unread_{code if isinstance(code, str) else 'error'}", [object_id])
            continue
        conflicts = answer.get("conflicts") if isinstance(answer.get("conflicts"), list) else []
        card = {"object": answer.get("object"), "leads": answer.get("leads"), "values": [],
                "conflicts": [c for c in conflicts if isinstance(c, Mapping) and c.get("tier")]}
        found.extend(pg._detect(card, as_of))
    held = {v["l1_handle"]: v for v in st.versions(handles={
        h for gap, requires in found for h in gap["citations"]})}

    def cite(handle: str) -> Cite:
        version = held.get(handle)   # a handle the store does not hold: shown to nobody
        return ((handle, version["object_id"], _markings(version["markings"])) if version
                else (handle, None, UNREADABLE))

    gaps = []
    for gap, requires in found:
        follow = gap["follow_up"]
        gaps.append(_gap(
            gap["kind"], "card_rules", gap["object_id"], gap["object_type"],
            gap["id"], summary=gap["summary"], missing=follow["missing"], why=follow["why"],
            priority=follow["priority"], about=(gap["object_type"], ""),
            groups=[[cite(h) for h in group] for group in requires],
            extra=[cite(h) for h in gap["citations"]]))
    status = "truncated" if len(ids) > bound or unread else "complete"
    return _result(gaps, status=status,
                   reasons=(["card_bound"] if len(ids) > bound else [])
                   + [f"unread:{code}" for code in sorted(unread)],
                   not_evaluated=skipped)


# --------------------------------------------------------------------------- the inbox

# The cited amount an event about a record of these types is ranked by (the record's own
# total and its currency, both one value across its sources): (amount, currency) property.
AMOUNT_OF = {"PurchaseOrder": ("total_cost", "currency"),
             "SalesOrder": ("total_amount", "currency"),
             "Quote": ("total_amount", "currency"),
             "PurchaseInvoice": ("total_amount", "currency")}


def _event_steps(st: _Store, as_of: date, call: Callable | None,
                 cache: Mapping[str, Mapping[str, Any]] | None = None,
                 vendor_bound: int | None = None, *,
                 pmap: Callable[[Callable, Sequence[Any]], list[Any]] | None = None,
                 results: Callable[[], Mapping[str, Mapping[str, Any]]] = dict,
                 people: Callable[[], Any] = lambda: None, margins: Any = None
                 ) -> dict[str, Callable[[], dict[str, Any]]]:
    """The proactive event detectors (``operating.proactive_events``) as engine steps.
    ``results()``: this run's results so far (the vendor rule reads the late rule's order
    join, else reads it itself); ``people()``: the people exclusion with the tenant's
    mail directory (``operating.chunking.permissions.Exclusion``)."""
    from . import proactive_events as ev

    common = {"gap": _gap, "cites": _cites, "result": _result}
    mail: dict[str, Any] = {} if vendor_bound is None else {"bound": vendor_bound}
    if pmap is not None:
        mail["pmap"] = pmap

    def orders() -> Mapping[str, tuple[str, str]] | None:
        """The late rule's order join; None when it ran this run and did not finish (the
        orders open are then not known: the vendor rule is blocked, never guessed)."""
        ran = results()
        if LATE_RULE in ran:
            return ran[LATE_RULE].get("po_orders")
        return _late_or_unconfirmed_po(st, as_of)["po_orders"]

    def margin_map(task: Callable, items: Sequence[Any]) -> list[Any]:
        """The margins read since the run's start (``margins``: ``_HeadStart``)."""
        if task is margins.task and list(items) == margins.items:
            return margins.collect()
        return pmap(task, items)

    def no_reply() -> dict[str, Any]:
        found = orders()
        if found is None:
            return _result([], status="blocked", reasons=["order_status_not_read"])
        src = _po_source(st)
        return ev.vendor_no_reply(st, as_of, people=people(), orders=found,
                                  order_key=_ORDER, colliding=src.colliding.__contains__,
                                  **common)
    return {
        "eta_slipped": lambda: ev.eta_slipped(
            st, as_of, settled=SETTLED, confirming=CONFIRMING,
            caveat=functools.partial(_caveat_all, unless=_promise_audiences(st)),
            orders=_po_source(st), **common),
        "lot_attention": lambda: ev.lot_attention(st, as_of, **common),
        "accepted_quote_no_order": lambda: ev.accepted_quote_no_order(st, as_of, **common),
        "invoice_quantity_mismatch": lambda: ev.invoice_quantity_mismatch(
            st, as_of, settled=SETTLED, orders=_po_source(st), **common),
        "vendor_no_reply": no_reply,
        "vendor_terms_unapplied": lambda: ev.vendor_mail(
            st, as_of, call, family="F27", cache=(cache or {}).get("vendor_terms_unapplied"),
            **mail, **common),
        "negative_margin": lambda: ev.negative_margin(
            st, as_of, call, pmap=pmap if margins is None else margin_map,
            roots=st.memo("margin_roots", lambda: ev._recent_invoiced_orders(st, as_of)),
            **common)}


def currency_word(raw: Any) -> str | None:
    """A currency cell as its ISO 4217 code by the ontology's one table
    (``ontology.units.currency_code``): ``CAD``, the Portal's ``CAD$``/``USD$``, ``C$``,
    ``US$``; None for anything no rule reads or a code ISO 4217 does not list (``CDN``,
    ``$``): such an amount is never stated nor ranked."""
    from ontology.units import ISO_4217, currency_code

    code, _ = currency_code(str(raw)) if raw not in (None, "") else (None, None)
    return code if code in ISO_4217 else None


def _suspects(st: _Store, object_type: str, objects: Sequence[str] = ()) -> dict[str, Any]:
    """``suspect_money.suspects`` of ``objects``, judged once over every record of the type
    that states a total (its medians are populations: an incremental pass compares them
    with the last fold's, ``_Store.population``)."""
    every = st.population(f"suspects:{object_type}", lambda: suspect_money.suspects(
        st, object_type, sorted(st.values([object_type], AMOUNT_OF[object_type][:1]))))
    return {oid: every[oid] for oid in objects if oid in every}


def _with_amounts(st: _Store, results: Mapping[str, Mapping[str, Any]]) -> None:
    """Each gap about a record ``AMOUNT_OF`` names that states no amount gets the record's
    own cited total with its cited currency: both citations join the gap as extra (they
    never decide who sees the gap) and the amount names them, so a reader shown the gap
    sees the figure only when it may open both (``_shown``). A total missing, disputed
    across sources, not a decimal, or without one 3-letter currency gives none. A total
    implausible against its lines or history (``operating.suspect_money``), or one that
    could not be checked, carries ``suspect`` (why): the briefing never ranks by it."""
    from . import proactive_events as ev

    for object_type in AMOUNT_OF:   # every run reads the same populations, gaps or none
        try:
            _suspects(st, object_type)
        except Exception:  # noqa: BLE001 - checked again below, per gap
            pass
    wanted: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results.values():
        for gap in result["gaps"]:
            event = gap["follow_up"].get("event") or {}
            if gap["object_type"] in AMOUNT_OF and "amount" not in event:
                wanted[gap["object_type"]].append(gap)
    for object_type, gaps in wanted.items():
        amount, currency = AMOUNT_OF[object_type]
        try:
            checked = _suspects(st, object_type, sorted({g["object_id"] for g in gaps}))
        except Exception:  # noqa: BLE001 - an unchecked total is never stated as fact
            checked = None
        try:
            found = st.values([object_type], (amount, currency),
                              objects=sorted({g["object_id"] for g in gaps}))
        except sqlite3.OperationalError:
            continue   # interrupted at the deadline: no amount stated, none ranked
        for gap in gaps:
            props = (found.get(gap["object_id"]) or {}).get("props") or {}
            total, code = ev._one(props.get(amount)), ev._one(props.get(currency))
            value = ev._decimal(total["value"]) if total else None
            word = currency_word(code["value"]) if code else None
            if value is None or value <= 0 or word is None:
                # no figure, none entered yet (a total of 0: an order not priced), or a
                # currency no rule reads: nothing stated or ranked
                continue
            extra = _cites([total, code])
            stated: dict[str, Any] = {
                "value": str(value), "currency": word, "basis": f"{amount}",
                "handle": total["l1_handle"], "currency_handle": code["l1_handle"],
                # the figure's own markings (a value may be marked apart from its record)
                "markings": _markings(total.get("markings"), code.get("markings"))}
            found_of = None if checked is None else checked.get(gap["object_id"]) or {}
            if found_of is None:
                stated["suspect"] = "its plausibility could not be checked"
            elif found_of.get("suspect"):
                # implausible against its history: shown as suspect, never ranked; the
                # reason's ratio band only for a reader who may see every peer it rests on.
                # Checked first: an outlier is never shown as a convention
                finding = found_of["suspect"]
                stated.update(suspect=finding["reason"], suspect_plain=finding["plain"],
                              suspect_scope=finding["scope"])
            if found_of and found_of.get("lines"):
                # checked against its own lines and not simply theirs (the Portal's unit-
                # price convention, a disagreement, or no priced line): never ranked; what
                # the lines say is stated only to a reader who may open every one of them
                lines = found_of["lines"]
                rows = lines.get("rows")
                line_cites = _cites(rows) if rows else []
                extra = [*extra, *line_cites]
                stated["lines"] = {
                    "verdict": lines["verdict"],
                    **({"lines_sum": lines["lines_sum"]} if "lines_sum" in lines else {}),
                    "handles": None if rows is None else [c[0] for c in line_cites],
                    "markings": lines["markings"]}
            gap["extra"] = list(dict.fromkeys([*gap["extra"], *extra]))
            gap["follow_up"]["event"] = {**(gap["follow_up"].get("event") or {}),
                                         "amount": stated}


_INBOX_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL,
  as_of TEXT NOT NULL, store_digest TEXT, summary TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scope (audience_key TEXT NOT NULL, markings TEXT NOT NULL,
  objects INTEGER NOT NULL, PRIMARY KEY (audience_key, markings));
CREATE TABLE IF NOT EXISTS detector_runs (detector TEXT PRIMARY KEY, status TEXT NOT NULL,
  reasons TEXT NOT NULL, found INTEGER NOT NULL, not_evaluated TEXT NOT NULL,
  run_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rule_coverage (detector TEXT PRIMARY KEY,
  unattributed INTEGER NOT NULL, run_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unjudged_causes (detector TEXT NOT NULL, skip INTEGER NOT NULL,
  object_id TEXT NOT NULL, markings TEXT NOT NULL, subject INTEGER NOT NULL,
  PRIMARY KEY (detector, skip, object_id, markings));
CREATE TABLE IF NOT EXISTS gaps (gap_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  detector TEXT NOT NULL, object_id TEXT NOT NULL, object_type TEXT NOT NULL,
  summary TEXT NOT NULL, follow_up TEXT NOT NULL, priority INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open', 'resolved')), first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL, resolved_at TEXT);
CREATE INDEX IF NOT EXISTS gaps_open ON gaps (state, priority, last_seen);
CREATE TABLE IF NOT EXISTS gap_cites (gap_id TEXT NOT NULL, grp INTEGER NOT NULL,
  handle TEXT NOT NULL, object_id TEXT, markings TEXT NOT NULL,
  PRIMARY KEY (gap_id, grp, handle));
CREATE TABLE IF NOT EXISTS gap_requires (gap_id TEXT NOT NULL, audience_key TEXT NOT NULL,
  PRIMARY KEY (gap_id, audience_key));
CREATE TABLE IF NOT EXISTS acks (gap_id TEXT NOT NULL, reader TEXT NOT NULL, at TEXT NOT NULL,
  PRIMARY KEY (gap_id, reader));
CREATE TABLE IF NOT EXISTS visits (reader TEXT PRIMARY KEY, at TEXT NOT NULL, previous TEXT);
CREATE TABLE IF NOT EXISTS event_cache (detector TEXT NOT NULL, root TEXT NOT NULL,
  reading TEXT NOT NULL, PRIMARY KEY (detector, root));
CREATE TABLE IF NOT EXISTS detector_history (detector TEXT NOT NULL, run_at TEXT NOT NULL,
  status TEXT NOT NULL, found INTEGER NOT NULL, PRIMARY KEY (detector, run_at));
CREATE TABLE IF NOT EXISTS kind_history (kind TEXT NOT NULL, run_at TEXT NOT NULL,
  found INTEGER NOT NULL, PRIMARY KEY (kind, run_at));
"""


def _read_cache(inbox: Path) -> dict[str, dict[str, Any]]:
    """The incremental detectors' last readings (``event_cache``): detector -> root ->
    reading. None stored (or no inbox yet): every root is read."""
    if not inbox.is_file():
        return {}
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    with closing(_open_inbox(inbox)) as conn:
        for row in conn.execute("SELECT detector, root, reading FROM event_cache"):
            try:
                out[row["detector"]][row["root"]] = json.loads(row["reading"])
            except ValueError:
                continue   # an unreadable reading is read again
    return dict(out)


def inbox_path(store_path: Any) -> Path:
    """The inbox file of a projection store: beside it, named after it."""
    store = Path(store_path)
    return store.with_name(store.name + ".gap-inbox.sqlite")


def reader_key(actor: Any) -> str:
    """The reader an acknowledgement belongs to: tenant and sorted principals."""
    return f"{actor.tenant}:" + ",".join(sorted(actor.principals))


def _open_inbox(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_INBOX_DDL)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    if row is None:
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)",
                     (INBOX_SCHEMA,))
    elif row[0] != INBOX_SCHEMA:
        conn.close()
        raise ValueError("gap_inbox_schema_mismatch")
    return conn


def _people(store_path: Path) -> Any:
    """The people exclusion (environment) with the tenant's mail directory and domains
    from the mail capture of the state directory ``store_path`` sits in (the projection
    file, or a generation's store under it); unreadable: an unavailable guard."""
    from .chunking.permissions import load_exclusion

    parent = store_path.parent
    state = parent.parent.parent.parent if parent.parent.name == "generations" \
        else parent.parent
    try:
        return load_exclusion(state)
    except Exception:  # noqa: BLE001 - no directory: the rule is blocked, never guessed
        return None


class TimeBudgetSpent(Exception):
    """A tool call refused because the run's time budget is spent."""


TOOL_BUDGET_MAX_MS = 600000   # plx.investigate's schema maximum for time_budget_ms


class Budget:
    """A run's time budget (``seconds`` from now on ``monotonic``): ``guard`` wraps a tool
    caller so that once it is spent every call raises ``TimeBudgetSpent`` and is noted
    (``refused``), so a detector that swallows a failing call is still not complete.

    Each detector runs in its own window (``open``: its own seconds, ``DETECTOR_SECONDS``,
    never past the run's deadline), kept per thread: detectors running side by side
    (``run(threads=)``) each stop at their own window, and a refusal counts against the
    detector whose thread it happened on (``refused_here``)."""

    def __init__(self, seconds: float, monotonic: Callable[[], float] = time.monotonic,
                 *, reserve: float = 0.0) -> None:
        self.monotonic = monotonic
        # the detectors end ``reserve`` seconds early (at most a quarter of the budget): the
        # amounts and their plausibility check run in that reserve, inside the bound
        self.end = monotonic() + seconds
        self.deadline = self.end - min(max(reserve, 0.0), seconds / 4)
        self.refused = 0
        self._local = threading.local()
        self._lock = threading.Lock()

    @property
    def until(self) -> float | None:
        """This thread's detector window's end, when it has one."""
        return getattr(self._local, "until", None)

    def spent(self) -> bool:
        return self.monotonic() >= self.deadline

    def open(self, seconds: float | None) -> None:
        """This thread's next detector runs for at most ``seconds`` (None: to the run's
        deadline), so no one slow detector starves the rest."""
        self._local.until = None if seconds is None else self.monotonic() + max(0.0, seconds)

    def share(self, weight: float, of: float) -> None:
        """Give the next detector ``weight``/``of`` of what is left (time it leaves unused
        carries to the ones after it)."""
        now = self.monotonic()
        self._local.until = now + max(0.0, self.deadline - now) * weight / max(weight, of)

    def unshare(self) -> None:
        self._local.until = None

    def note_refused(self) -> None:
        with self._lock:
            self.refused += 1
        self._local.refused = getattr(self._local, "refused", 0) + 1

    def refused_here(self) -> int:
        """Refusals noted on this thread (its detector's calls and maps)."""
        return getattr(self._local, "refused", 0)

    def release(self) -> None:
        """The detectors are done: what is left up to the end is the reserve's."""
        self._local.until, self.deadline = None, self.end

    def guard(self, call: Callable | None) -> Callable | None:
        if call is None:
            return None

        def guarded(tool: str, arguments: dict) -> Any:
            left = self.left()
            if left <= 0:
                self.note_refused()
                raise TimeBudgetSpent(tool)
            if tool == "plx.investigate":
                # the investigation stops itself at the deadline (between its steps), never
                # only the next call: the budget bounds the call in flight too
                # (the tool refuses more than TOOL_BUDGET_MAX_MS: a long pass asks for that much)
                arguments = {**arguments,
                             "time_budget_ms": max(1, min(int(left * 1000), TOOL_BUDGET_MAX_MS))}
            found = call(tool, arguments)
            reasons = ((found.get("answerability") or {}).get("reasons") or ()
                       if isinstance(found, Mapping) else ())
            if tool == "plx.investigate" and "time_budget_exhausted" in reasons:
                self.note_refused()   # a part-read investigation is no reading at all
                raise TimeBudgetSpent(tool)
            return found
        return guarded

    def pooled(self, pool: Any) -> Callable[[Callable, Sequence[Any]], list[Any]]:
        """``pool.map`` within this budget: a map that leaves items unread (the time left
        did not reach them) is noted ``refused``, so its detector is not complete."""
        from . import proactive_pool

        def run_map(task: Callable, items: Sequence[Any]) -> list[Any]:
            out = pool.map(task, items, left=self.left)
            if any(r is proactive_pool.NOT_READ for r in out):
                self.note_refused()
            return out
        return run_map

    def left(self) -> float:
        """Seconds left for this thread's detector: to its window's end, else the
        deadline."""
        until = self.until
        end = self.deadline if until is None else min(self.deadline, until)
        return end - self.monotonic()

    def interrupt(self) -> int:
        """SQLite's progress handler: nonzero aborts the statement running (a set-based
        rule's query stops at the deadline, not after it)."""
        return int(self.left() <= 0)


class _HeadStart:
    """``pool.map(task, items)`` started before its rule's turn, on a thread that only
    waits on the pool: the workers read while the store-only rules run on the main thread
    (the pool idles otherwise). Until the rule's turn the map reads to the detectors'
    deadline; from it (``collect``) to the rule's share of the time, as ``Budget.pooled``;
    ``stop`` ends it unread (the rule never ran)."""

    def __init__(self, pool: Any, task: Callable, items: Sequence[Any],
                 budget: Budget | None) -> None:
        self.task, self.items, self.budget = task, list(items), budget
        self.turn = self.stopped = False
        self.until: float | None = None   # the collecting detector's window end
        self.out: list[Any] | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run_map, args=(pool,), daemon=True,
                                       name="proactive-head-start")
        self.thread.start()

    def left(self) -> float:
        if self.stopped:
            return 0.0
        if self.budget is None:
            return float("inf")
        if self.turn:
            end = self.budget.deadline if self.until is None \
                else min(self.budget.deadline, self.until)
            return end - self.budget.monotonic()
        return self.budget.deadline - self.budget.monotonic()

    def _run_map(self, pool: Any) -> None:
        try:
            self.out = pool.map(self.task, self.items, left=self.left)
        except BaseException as error:  # noqa: BLE001 - raised in the rule that collects
            self.error = error

    def collect(self) -> list[Any]:
        from . import proactive_pool

        if self.budget is not None:
            self.until = self.budget.until   # read on the collecting detector's thread
        self.turn = True
        self.thread.join()
        if self.error is not None:
            raise self.error
        out = list(self.out or [])
        if self.budget is not None and any(r is proactive_pool.NOT_READ for r in out):
            self.budget.note_refused()   # as ``Budget.pooled``: its rule is not complete
        return out

    def stop(self) -> None:
        self.stopped = True
        self.thread.join()


def _margin_head_start(st: _Store, as_of: date, pool: Any, budget: Budget | None,
                       order: Sequence[str]) -> _HeadStart | None:
    """The negative-margin rule's order margins read on the pool from the run's start
    (``_HeadStart``): each read is one order's margin investigation (minutes for a Sage
    order on the live store), the slowest rule of a run. None without a pool, when the rule
    does not run, the time is spent or no order was invoiced recently."""
    if pool is None or "negative_margin" not in order or (budget is not None
                                                           and budget.spent()):
        return None
    from . import proactive_events as ev

    try:
        roots = st.memo("margin_roots", lambda: ev._recent_invoiced_orders(st, as_of))
    except sqlite3.OperationalError:
        return None   # interrupted at the deadline: the rule reads them at its turn
    return _HeadStart(pool, ev.read_margin, roots, budget) if roots else None


def detector_threads(environ: Mapping[str, str] | None = None) -> int:
    """Threads the store-only detectors run on side by side: every core but two, at most
    ``MAX_DETECTOR_THREADS`` (``PLX_PROACTIVE_DETECTOR_THREADS`` overrides; 1: one after
    another)."""
    raw = (os.environ if environ is None else environ).get(THREADS_ENV, "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return 1
    return max(1, min(MAX_DETECTOR_THREADS, (os.cpu_count() or 1) - 2))


def _lanes(order: Sequence[str], threads: int) -> list[list[str]]:
    """None with one thread (every detector then runs in ``order`` on the run's thread)."""
    if threads <= 1:
        return []
    side = [d for d in order if d in SET_BASED and d != "order_without_customer_po"]
    lanes: list[list[str]] = []
    for name in side:
        after = CHAINED.get(name)
        lane = next((ln for ln in lanes if after in ln), None)
        if lane is not None:
            lane.append(name)
        else:
            lanes.append([name])
    first = {"conflicting_quantity": 0, LATE_RULE: 1}
    return sorted(lanes, key=lambda ln: first.get(ln[0], 2))


def run(store_path: Any, inbox: Any, as_of: date | None = None, *,
        locked: bool = False, **options: Any) -> dict[str, Any]:
    """Run the detectors and fold their gaps into the inbox (``_run``), holding the
    inbox's pass lock (``pass_lock``; ``locked``: the caller holds it): two runs never
    read and fold over each other, so a fold is never of older data than the last."""
    if locked:
        return _run(store_path, inbox, as_of, **options)
    with pass_lock(inbox) as _held:
        return _run(store_path, inbox, as_of, **options)


@contextlib.contextmanager
def pass_lock(inbox: Any, *, wait: bool = True) -> Iterator[bool]:
    """The inbox's pass lock (``<inbox>.lock``); ``wait`` False:
    yields False at once when another pass holds it."""
    import fcntl

    path = Path(inbox).with_name(Path(inbox).name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _run(store_path: Any, inbox: Any, as_of: date | None = None, *,
         coverage: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
        call: Callable | None = None, card_bound: int = CARD_BOUND,
        qc_link_types: Sequence[str] | None = None, budget: Budget | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        only: Sequence[str] | None = None, vendor_bound: int | None = None,
        reader_may_read: Callable[[str], bool] | None = None,
        pool: Any = None, people: Any = None, threads: int | None = 1,
        detector_seconds: Mapping[str, float] | None = None,
        scope: "Scope | None" = None) -> dict[str, Any]:
    """Run every detector over the store and fold the gaps into the inbox; the summary
    (counts and enums only). A detector that fails is ``failed`` and leaves its gaps as
    they were; only a ``complete`` detector resolves the gaps it no longer finds. The
    run's day (``as_of``, what late and days late are counted to) defaults to the
    business day (ET) of ``clock()`` (``po_status.business_today``), never its UTC day.
    ``coverage`` may be a callable, scanned at its detector's turn (its failure fails that
    detector alone). With a ``budget``, a detector not started before it is spent, or one
    whose tool call it refused, is ``truncated`` (``time_budget``). ``only``: run these
    detectors alone (the others' gaps and records are left as they are: the separate
    vendor-mail pass, ``operating.vendor_watch``). ``vendor_bound``: how many purchase
    orders the incremental vendor checks may investigate this run (0: reuse their
    readings only, the build's run when the vendor pass is scheduled apart).
    ``reader_may_read(audience_key)``: the grant of the principal ``call`` reads as; a
    rule whose verdict needs a source it may not read is ``blocked``
    (``reader_lacks_lateness_access``), never judged blind (None: not checked).
    ``pool``: an ``operating.proactive_pool.ToolPool`` reading as the same principal as
    ``call``: the tool-call rules ask every record of the store on its workers (no
    per-run record bound; the budget still bounds them), else one call at a time.
    ``people``: the people exclusion the vendor-mail rule clears senders and mailboxes
    with (``operating.chunking.permissions.Exclusion``); None: read from the environment
    and the mail capture of the state directory the store sits in (``_people``).
    A full run reads every link type its rules read in one scan first (``_Store.links``);
    with a pool, the margins of the recently invoiced orders are read on it from the start
    (``_margin_head_start``), while the store-only rules run. ``threads``: the store-only
    rules run side by side on this many threads (``_lanes``; 1: one after another; None:
    ``detector_threads()``), the tool-call rules on this thread. With a ``budget`` every
    detector runs in its own window (``DETECTOR_SECONDS``, ``detector_seconds`` overriding
    some), within the run's deadline; the summary's ``seconds`` holds each one's time.
    ``scope``: an incremental pass (``Scope``, ``operating.proactive_incremental``): its
    rules read only the scope's records and keep only the gaps about their own subjects;
    only those subjects' gaps are folded and resolved (``_fold``), nothing else of the
    inbox (the full run's records, counts and generation) changes."""
    moment = clock()
    as_of = po_status.business_today(moment) if as_of is None else as_of
    if type(as_of) is not date:   # a datetime or text is no day: refused, never coerced
        raise TypeError("as_of_must_be_a_date")
    run_at = moment.isoformat()
    call = call if budget is None else budget.guard(call)
    pmap: Callable[[Callable, Sequence[Any]], list[Any]] | None = None
    many: Callable[[str, Sequence[Any]], list[Any]] | None = None
    if pool is not None and call is not None:
        from . import proactive_pool

        pmap = pool.map if budget is None else budget.pooled(pool)

        def many(tool: str, arguments: Sequence[Any]) -> list[Any]:
            return pmap(proactive_pool.tool_call, [(tool, a) for a in arguments])
    cache = _read_cache(Path(inbox))
    if scope is not None:
        only = [d for d in DETECTORS if d in scope.subjects]
    st = _Store(Path(store_path), scope=None if scope is None else scope.read,
                audience_keys=None if scope is None else scope.audience_keys)
    if budget is not None:
        st.watch(budget.interrupt)
    try:
        results: dict[str, dict[str, Any]] = {}
        seconds: dict[str, float] = {}
        unknown = set(only or ()) - set(DETECTORS)
        if unknown:
            raise ValueError("detector_unknown")
        order = list(DETECTORS) if only is None else [d for d in DETECTORS if d in only]
        if (only is None or scope is not None) and not (budget is not None and budget.spent()):
            # every link type the rules read, in one scan of the link table (``_Store``)
            started = time.monotonic()
            try:
                st.prefetch_links(_run_link_types())
            except sqlite3.OperationalError:
                pass   # stopped at the deadline: each rule then stops at its own read
            seconds["link_scan"] = round(time.monotonic() - started, 1)
        head = _margin_head_start(st, as_of, pool if call is not None else None, budget,
                                  order)
        steps: dict[str, Callable[[], dict[str, Any]]] = {
            "late_or_unconfirmed_po": lambda: _late_or_unconfirmed_po(st, as_of),
            "late_customer_order": lambda: _late_customer_order(st, as_of),
            "po_receipt_quantity_mismatch": lambda: _po_receipt_quantity_mismatch(st, as_of),
            "receipt_without_qc": lambda: _receipt_without_qc(
                st, as_of, declared_qc_links() if qc_link_types is None else qc_link_types),
            "order_without_customer_po": lambda: _order_without_customer_po(
                st, coverage() if callable(coverage) else coverage),
            "expired_quote": lambda: _expired_quote(st, as_of),
            "conflicting_quantity": lambda: _conflicting_quantity(st, as_of),
            "late_order": lambda: _late_order(st, as_of),
            "orders_at_risk": lambda: (
                _orders_at_risk(st, as_of, call, card_bound, first=_late_found(results),
                                many=many)
                if call is None or _reader_judges_lateness(st, reader_may_read) else
                # the run principal cannot read a source a lateness verdict rests on (a
                # PO's BC twin): its verdicts would be undetermined, never late, for every
                # reader, so it does not judge (its gaps stay as they were); never a clean
                # "none at risk"
                _result([], status="blocked", reasons=["reader_lacks_lateness_access"])),
            "card_rules": lambda: _card_rules(st, as_of, call, card_bound, many),
            **_event_steps(st, as_of, call, cache, vendor_bound, pmap=pmap,
                           results=lambda: results,
                           people=lambda: people if people is not None
                           else _people(Path(store_path)), margins=head)}
        windows = {**DETECTOR_SECONDS, **(detector_seconds or {})}

        def judge(name: str) -> None:
            """One detector in its own window, on this thread; never raising."""
            if budget is not None and budget.spent():
                results[name] = _result([], status="truncated", reasons=[TIME_BUDGET])
                return
            refused = budget.refused_here() if budget is not None else 0
            if budget is not None:
                budget.open(windows.get(name, DETECTOR_SECONDS_DEFAULT))
            started = time.monotonic()
            st.judging(name)
            try:
                results[name] = steps[name]()
            except TimeBudgetSpent:
                results[name] = _result([], status="truncated", reasons=[TIME_BUDGET])
            except Exception as error:  # noqa: BLE001 - one detector fails alone, typed
                if budget is not None and budget.left() <= 0 \
                        and isinstance(error, sqlite3.OperationalError):
                    # its query was interrupted at its window's end: cut short, not failed
                    results[name] = _result([], status="truncated", reasons=[TIME_BUDGET])
                else:
                    results[name] = _result([], status="failed",
                                            reasons=[type(error).__name__])
            finally:
                st.judging(None)
                if budget is not None:
                    budget.unshare()
                seconds[name] = round(time.monotonic() - started, 1)
            if budget is not None and budget.refused_here() > refused \
                    and results[name]["status"] != "failed":
                # a call it made was refused: what it found stands, never all it would find
                results[name]["status"] = "truncated"
                if TIME_BUDGET not in results[name]["reasons"]:
                    results[name]["reasons"].append(TIME_BUDGET)

        lanes = _lanes(order, detector_threads() if threads is None else threads)
        try:
            if lanes:
                from concurrent.futures import ThreadPoolExecutor

                def lane(names: Sequence[str]) -> None:
                    for name in names:
                        judge(name)
                side = [d for names in lanes for d in names]
                width = min(len(lanes), detector_threads() if threads is None else threads)
                with ThreadPoolExecutor(max_workers=width,
                                        thread_name_prefix="proactive-detector") as threads_:
                    running = {name: future for names in lanes
                               for future in [threads_.submit(lane, names)] for name in names}
                    for name in order:
                        if name in side:
                            continue
                        if name == "orders_at_risk" and LATE_RULE in running:
                            running[LATE_RULE].result()   # it asks the late POs first
                        judge(name)
                    for future in set(running.values()):
                        future.result()
            else:
                for name in order:
                    judge(name)
        finally:
            if head is not None:
                head.stop()   # its rule did not run (the time ran out): no read goes on
        if budget is not None:
            budget.release()   # the amounts run in the reserve, still inside the bound
        if scope is not None:
            # an incremental pass: a gap is this pass's only when its subject is one the
            # pass judged whole (the others' inputs were not all read)
            for name, result in results.items():
                mine = scope.subjects.get(name, frozenset())
                result["gaps"] = [g for g in result["gaps"] if g["object_id"] in mine]
        _with_amounts(st, results)
        # one test-record filter for every detector (never a page's own): a claim about a
        # test, UAT, demo or proof record, a go-live first document or a carried-over old
        # empty draft is not a business claim
        try:
            _drop_test_records(st, results, as_of)
        except sqlite3.Error:
            pass   # the store could not be read for it: the gaps stand as found
        populations = {name: {key: digest for key, digest in sorted(st.populations.items())
                              if name in st.population_readers[key]
                              or "*" in st.population_readers[key]}
                       for name in results}
        # the bound is spent on the detectors and the amounts only: the run's record (scope,
        # digest) is always read whole, so a run cut short still folds what it found
        st.watch(None)
        held = st.scope() if scope is None else []
        digest = st.meta("content_digest")
    finally:
        st.close()
    gaps: dict[str, dict[str, Any]] = {}
    for result in results.values():
        for gap in result["gaps"]:
            gaps.setdefault(gap["id"], gap)
    with closing(_open_inbox(Path(inbox))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            held_pops = _populations(conn)
            moved: list[str] = []
            if scope is not None:
                # a population a rule judges against moved since the rule's last fold:
                # subjects this pass did not read may move with it, so the rule is not
                # folded here but judged whole (``proactive_incremental``)
                moved = sorted(name for name in results
                               if populations.get(name) != held_pops.get(name))
                for name in moved:
                    del results[name]
                gaps = {gid: gap for gid, gap in gaps.items() if gap["detector"] in results}
            summary = _summary(results, gaps, held)
            summary["seconds"] = seconds   # each detector's run time (counts and enums only)
            if moved:
                summary["population_moved"] = moved
                summary["populations_moved"] = sorted({
                    key for name in moved for key in set(populations.get(name, {}))
                    | set(held_pops.get(name) or {})
                    if populations.get(name, {}).get(key)
                    != (held_pops.get(name) or {}).get(key)})
            for name, result in results.items():
                if result["status"] in READ_WHOLE_DATA:
                    held_pops[name] = populations.get(name, {})
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                         (_POPULATIONS_KEY, json.dumps(held_pops, sort_keys=True)))
            if scope is not None:
                _fold_scoped(conn, results, gaps, run_at, scope)
            else:
                _fold(conn, results, gaps, held, run_at, as_of, digest, summary,
                      record_run=only is None)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return summary


# The one test-record rule (``operating.test_records``): marks, names and numbers that say a
# record is a test or placeholder. The engine reads the marks on the documents an order owns
# (BC invoices, receipts, sales orders' external numbers, order lines) and the names on the
# order and its party; a record carrying one, or the order it belongs to, is no claim.
TEST_MARK = test_records_rule.TEST_MARK
# Where a test mark is read: (record type, property).
TEST_MARK_PROPS = (("PurchaseInvoice", "vendor_invoice_number"),
                   ("PurchaseOrderLine", "description"),
                   ("SalesOrder", "customer_po"),
                   ("SalesInvoice", "external_document_number"),
                   ("SalesInvoice", "customer_po"),
                   ("Receipt", "vendor_invoice_number"))
# An order's own numbers and names the name rules read, and the links to its party.
TEST_NAME_PROPS = ("po_number", "fm_po_number", "order_number")
TEST_PARTY_LINKS = ("PurchaseOrder.ordered_from", "SalesOrder.ordered_by")
# The links from a marked record to the order it belongs to (each system's own stored
# order reference) and the identity links joining an order's copies.
TEST_ORDER_LINKS = (po_status.LINE_OF, "PurchaseInvoice.invoices", "PurchaseInvoice.names_order",
                    "Receipt.receives")
TEST_TWIN_LINKS = (*po_status.TWIN_LINKS, "SalesOrder.same_record_as")
# A carried-over draft: an order whose Portal copy is a draft ordering nothing, last changed
# more than this many days before the run's day (an old draft carried into BC at go-live).
OLD_DRAFT_DAYS = 365
# Number series: the letters (or leading digits) of a document number before its running
# number, and the run's width (a BC series ``106001``: series ``106``, first ``001``).
_SERIES = re.compile(r"^(.*?)(\d{3})$")


def test_records(st: _Store, as_of: date) -> dict[str, str]:
    """object id -> why it is a test or carried-over record (``mark``, ``go_live_first``,
    ``old_empty_draft``), every copy of the order included: the one filter every detector
    and every page shares (``_drop_test_records``; the operator API reads it too)."""
    # a population (number series across records): read whole in an incremental pass
    return st.population(f"test_records:{as_of.isoformat()}",
                         lambda: _find_test_records(st, as_of))


def _find_test_records(st: _Store, as_of: date) -> dict[str, str]:
    found: dict[str, str] = {}
    for kind, prop in TEST_MARK_PROPS:
        for oid, entry in st.values((kind,), (prop,)).items():
            if any(TEST_MARK.search(str(r["value"] or "")) for r in entry["props"].get(prop, [])):
                found.setdefault(oid, "mark")
    links = st.links(TEST_ORDER_LINKS, (*CONFIRMING, "stored_by_text_join"))
    for link in links:
        if link["from_object"] in found:
            found.setdefault(link["to_object"], found[link["from_object"]])
    pos = st.values(("PurchaseOrder",), ("po_number", "order_date", "status",
                                          "portal_updated_at"))
    # an order with a line that orders something (a line of nothing is an empty line)
    line_links = st.links((po_status.LINE_OF,), CONFIRMING)
    ordered = st.values(("PurchaseOrderLine",), ("ordered_quantity",), objects=sorted(
        {l["from_object"] for l in line_links}))
    lines = {l["to_object"] for l in line_links if any(
        (pg._decimal(r["value"]) or 0) > 0
        for r in (ordered.get(l["from_object"]) or {"props": {}})["props"].get(
            "ordered_quantity", []))}
    # go-live first documents: in each BC number series, the first number the series
    # issued (its running number 001) and the documents of the series dated that same day
    twins = st.links(TEST_TWIN_LINKS, (*SETTLED, "corroborated"))
    twinned = {end for link in twins for end in (link["from_object"], link["to_object"])}
    series: dict[str, list[tuple[str, int, date | None]]] = defaultdict(list)
    for oid, entry in pos.items():
        # a BC-native document (no other system holds a copy): a copy's number is the
        # other system's series, whose first document is a real order
        if not oid.startswith("PurchaseOrder:bc.") or oid in twinned:
            continue
        for row in entry["props"].get("po_number", []):
            m = _SERIES.match(str(row["value"] or "").strip())
            if m:
                days = {_day(r["value"]) for r in entry["props"].get("order_date", [])}
                series[m.group(1)].append((oid, int(m.group(2)),
                                           next(iter(days)) if len(days) == 1 else None))
    for members in series.values():
        first = [m for m in members if m[1] == 1]
        if len(first) == 1 and first[0][2] is not None:
            for oid, _n, day in members:
                if day == first[0][2]:
                    found.setdefault(oid, "go_live_first")
    # old empty drafts: a Portal draft ordering nothing, untouched for a year
    for oid, entry in pos.items():
        props = entry["props"]
        kinds = {po_status.header_class(r["value"]) for r in props.get("status", [])}
        changed = [_day(r["value"]) for r in props.get("portal_updated_at", [])]
        if kinds == {po_status.NOT_ISSUED} and oid not in lines and changed and all(
                d is not None and (as_of - d).days > OLD_DRAFT_DAYS for d in changed):
            found.setdefault(oid, "old_empty_draft")
    # names and numbers that say so (``operating.test_records.is_test``): an order's own
    # number or name (a "(DO NOT USE)" marker, a TEST name, a UAT- prefixed number, the
    # deployment's own list named by ``PLX_APP_TEST_RECORDS``), or its supplier's or
    # customer's TEST name (a party's "(DO NOT USE)" marks a retired card, never a test)
    named = test_records_rule.listed(None)
    parties = {oid: [str(r["value"]) for r in entry["props"].get("name", []) if r["value"]]
               for oid, entry in st.values(("Supplier", "Customer"), ("name",)).items()}
    party_of: dict[str, list[str]] = defaultdict(list)
    for link in st.links(TEST_PARTY_LINKS, (*SETTLED, "corroborated")):
        party_of[link["from_object"]] += parties.get(link["to_object"], [])
    orders = st.values(("PurchaseOrder", "SalesOrder"), TEST_NAME_PROPS)
    for oid in sorted({*orders, *party_of}):
        entry = orders.get(oid) or {"props": {}}
        texts = [str(r["value"]) for p in TEST_NAME_PROPS for r in entry["props"].get(p, [])
                 if r["value"]]
        if test_records_rule.is_test(texts, named, party_of.get(oid, ())):
            found.setdefault(oid, "name")
    # every copy of a test order is the same test order
    for _ in range(2):
        for link in twins:
            a, b = link["from_object"], link["to_object"]
            if a in found and b not in found:
                found[b] = found[a]
            elif b in found and a not in found:
                found[a] = found[b]
    return found


def _drop_test_records(st: _Store, results: dict[str, dict[str, Any]], as_of: date) -> None:
    """Remove every gap about a test record (its subject, or the order it is about) from
    every detector's result, counted ``test_record`` in its coverage."""
    tests = test_records(st, as_of)
    if not tests:
        return
    for result in results.values():
        kept, dropped = [], 0
        for gap in result.get("gaps") or ():
            order = ((gap.get("follow_up") or {}).get(_ORDER) or {}).get("key")
            if gap.get("object_id") in tests or order in tests:
                dropped += 1
            else:
                kept.append(gap)
        if dropped:
            result["gaps"] = kept
            skipped = result.setdefault("not_evaluated", {})
            if isinstance(skipped, dict):
                skipped["test_record"] = skipped.get("test_record", 0) + dropped


def _summary(results: Mapping[str, Mapping[str, Any]], gaps: Mapping[str, Mapping[str, Any]],
             scope: Sequence[tuple[str, str, int]]) -> dict[str, Any]:
    cited = [g for g in gaps.values() if all(g["groups"])]
    return {"scanned": sum(n for _, _, n in scope),
            "scope_truncated": any(results[d]["status"] in ("truncated", "failed")
                                   for d in SET_BASED if d in results),
            "truncated": any(r["status"] in ("truncated", "failed") for r in results.values()),
            "gaps_found": len(gaps), "gaps_cited": len(cited),
            "gaps_uncited": len(gaps) - len(cited),
            "with_follow_up": sum(1 for g in gaps.values() if g["follow_up"]["who_can_answer"]),
            "gaps_by_kind": dict(sorted(Counter(g["kind"] for g in gaps.values()).items())),
            "detectors": {name: results[name]["status"] for name in DETECTORS
                          if name in results},
            "detector_reasons": {name: results[name]["reasons"] for name in DETECTORS
                                 if name in results and results[name]["reasons"]},
            "not_evaluated": {name: results[name]["not_evaluated"] for name in DETECTORS
                              if name in results and results[name]["not_evaluated"]},
            "reused": {name: results[name]["reused"] for name in INCREMENTAL
                       if "reused" in results.get(name, {})},
            "po_lateness": results.get("late_or_unconfirmed_po", {}).get("counts"),
            # per detector: of the records it should judge, how many it judged and why
            # the others were not (``_coverage``)
            "coverage": {name: results[name]["coverage"] for name in DETECTORS
                         if name in results and results[name].get("coverage")},
            "top_finding": results.get("orders_at_risk", {}).get("finding")}


_POPULATIONS_KEY = "populations"   # inbox meta: per rule, the populations of its last fold


def _populations(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (_POPULATIONS_KEY,)).fetchone()
    try:
        held = json.loads(row[0]) if row else {}
    except ValueError:
        held = {}
    return held if isinstance(held, dict) else {}


@dataclasses.dataclass(frozen=True)
class Scope:
    """An incremental pass (``operating.proactive_incremental``): ``subjects``, per rule,
    the records it judges again (every subject whose declared inputs hold a changed
    record, or whose truth moved with the day); ``read``, every record those judgements
    read; ``audience_keys``: the store's keys (the generation's and its changed records')."""
    subjects: Mapping[str, frozenset[str]]
    read: frozenset[str]
    audience_keys: frozenset[str] | None = None


def _fold_scoped(conn: sqlite3.Connection, results: Mapping[str, Mapping[str, Any]],
                 gaps: Mapping[str, Mapping[str, Any]], run_at: str, scope: Scope) -> None:
    """Fold an incremental pass: the gaps it found are written as a full run writes them
    (``_upsert_gaps``), and of each rule that judged its subjects whole (``complete``) an
    open gap about one of them that it no longer finds is resolved. The run records,
    counts, coverage and generation stay the last full run's."""
    _upsert_gaps(conn, results, gaps, run_at)
    for name, result in results.items():
        if result["status"] != "complete":
            continue
        conn.execute(
            "UPDATE gaps SET state = 'resolved', resolved_at = ? WHERE state = 'open' "
            "AND detector = ? AND object_id IN (SELECT value FROM json_each(?)) "
            "AND gap_id NOT IN (SELECT value FROM json_each(?))",
            (run_at, name, json.dumps(sorted(scope.subjects.get(name, ()))),
             json.dumps(sorted(gaps))))


def _fold(conn: sqlite3.Connection, results: Mapping[str, Mapping[str, Any]],
          gaps: Mapping[str, Mapping[str, Any]], scope: Sequence[tuple[str, str, int]],
          run_at: str, as_of: date, digest: str | None, summary: Mapping[str, Any], *,
          record_run: bool = True) -> None:
    if record_run:
        # a run of some detectors only (the vendor-mail pass) is no build's run: it never
        # moves the run the inbox is read as of ("new this generation", stale, as of)
        conn.execute("INSERT INTO runs (run_at, as_of, store_digest, summary) VALUES "
                     "(?, ?, ?, ?)", (run_at, as_of.isoformat(), digest,
                                      json.dumps(summary, sort_keys=True)))
        conn.execute("DELETE FROM runs WHERE run_id NOT IN (SELECT run_id FROM runs "
                     "ORDER BY run_id DESC LIMIT ?)", (KEEP_RUNS,))
    if set(SET_BASED) <= set(results):   # a run of some detectors only keeps the scope
        conn.execute("DELETE FROM scope")
        conn.executemany("INSERT INTO scope VALUES (?, ?, ?)", scope)
    for name, result in results.items():
        if result["status"] == "not_configured" and conn.execute(
                "SELECT 1 FROM detector_runs WHERE detector = ?", (name,)).fetchone():
            continue   # a run without this detector's input says nothing about its gaps
        conn.execute(
            "INSERT INTO detector_runs VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (detector) DO "
            "UPDATE SET status = excluded.status, reasons = excluded.reasons, found = "
            "excluded.found, not_evaluated = excluded.not_evaluated, run_at = excluded.run_at",
            (name, result["status"], json.dumps(result["reasons"]), len(result["gaps"]),
             json.dumps(result["not_evaluated"], sort_keys=True), run_at))
        # The records this run's rule left unjudged with each skip's cause, replacing the
        # last run's: a reader is told the count of those it may read whose whole cause it
        # may read (``read_inbox``), never of the others.
        conn.execute("DELETE FROM unjudged_causes WHERE detector = ?", (name,))
        conn.executemany(
            "INSERT OR IGNORE INTO unjudged_causes VALUES (?, ?, ?, ?, ?)",
            [(name, n, object_id, markings, int(object_id in skip["subjects"]))
             for n, skip in enumerate(result.get("unjudged") or ())
             for object_id, markings in skip["cause"]])
        if result["status"] != "not_configured":
            conn.execute("INSERT OR REPLACE INTO detector_history VALUES (?, ?, ?, ?)",
                         (name, run_at, result["status"], len(result["gaps"])))
            # what was found per gap kind, and per kind whether late by the status rule
            # (``kind:late``/``kind:not_late``): "seen live" is said per event type
            found = Counter()
            for gap in result["gaps"]:
                late = gap["follow_up"].get("days_late") is not None
                found.update([gap["kind"], f"{gap['kind']}:{'late' if late else 'not_late'}"])
            conn.executemany("INSERT OR REPLACE INTO kind_history VALUES (?, ?, ?)",
                             [(kind, run_at, n) for kind, n in sorted(found.items())])
        if isinstance(result.get("cache"), Mapping):
            # merged, never replaced wholesale (a build's run and the vendor pass may fold
            # one after the other): a root no longer named by recent mail drops, every
            # reading this run holds is stored, the others stay as they were
            conn.execute("DELETE FROM event_cache WHERE detector = ? AND root NOT IN (SELECT "
                         "value FROM json_each(?))", (name, json.dumps(result["roots"])))
            conn.executemany("INSERT OR REPLACE INTO event_cache VALUES (?, ?, ?)", [
                (name, root, json.dumps(reading, sort_keys=True))
                for root, reading in result["cache"].items()])
        conn.execute(
            "INSERT INTO rule_coverage VALUES (?, ?, ?) ON CONFLICT (detector) DO UPDATE SET "
            "unattributed = excluded.unattributed, run_at = excluded.run_at",
            (name, int(result.get("unattributed") or 0), run_at))
        if result["status"] in READ_WHOLE_DATA:
            _note_generation(conn, name, digest, run_at)
    _upsert_gaps(conn, results, gaps, run_at)
    complete = [name for name, result in results.items() if result["status"] == "complete"]
    conn.execute(
        "UPDATE gaps SET state = 'resolved', resolved_at = ? WHERE state = 'open' "
        "AND detector IN (SELECT value FROM json_each(?)) "
        "AND gap_id NOT IN (SELECT value FROM json_each(?))",
        (run_at, json.dumps(complete), json.dumps(sorted(gaps))))


def _upsert_gaps(conn: sqlite3.Connection, results: Mapping[str, Mapping[str, Any]],
                 gaps: Mapping[str, Mapping[str, Any]], run_at: str) -> None:
    """Write each found gap (a resolved one reopened, its acknowledgements dropped)."""
    for gap in gaps.values():
        before = conn.execute("SELECT state FROM gaps WHERE gap_id = ?", (gap["id"],)).fetchone()
        if before is not None and before["state"] == "resolved":
            conn.execute("DELETE FROM acks WHERE gap_id = ?", (gap["id"],))   # reopened
        elif before is not None and gap["kind"] in WHOLE_ONLY \
                and results[gap["detector"]]["status"] != "complete":
            # a money claim summed over a run that did not finish may be a part: the open
            # gap keeps the wording and citations of the last run that did
            conn.execute("UPDATE gaps SET last_seen = ? WHERE gap_id = ?", (run_at, gap["id"]))
            continue
        conn.execute(
            "INSERT INTO gaps VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, NULL) "
            "ON CONFLICT (gap_id) DO UPDATE SET summary = excluded.summary, follow_up = "
            "excluded.follow_up, priority = excluded.priority, state = 'open', last_seen = "
            "excluded.last_seen, resolved_at = NULL",
            (gap["id"], gap["kind"], gap["detector"], gap["object_id"], gap["object_type"],
             gap["summary"], json.dumps(gap["follow_up"], sort_keys=True),
             PRIORITY.index(gap["follow_up"]["priority"]), run_at, run_at))
        conn.execute("DELETE FROM gap_cites WHERE gap_id = ?", (gap["id"],))
        rows = {(grp, c[0]): (gap["id"], grp, c[0], c[1], c[2])
                for grp, group in enumerate(gap["groups"]) for c in group}
        for c in gap["extra"]:
            rows.setdefault((-1, c[0]), (gap["id"], -1, c[0], c[1], c[2]))
        conn.executemany("INSERT INTO gap_cites VALUES (?, ?, ?, ?, ?)", rows.values())
        conn.execute("DELETE FROM gap_requires WHERE gap_id = ?", (gap["id"],))
        conn.executemany("INSERT INTO gap_requires VALUES (?, ?)",
                         [(gap["id"], key) for key in sorted(set(gap.get("requires", ())))])


# A detector whose run ended in one of these read the store it ran over: what it found is
# that store's (a truncated run's findings stand, its unfound older gaps do not).
READ_WHOLE_DATA = ("complete", "truncated")
_GENERATION_KEY = "generation:"   # inbox meta key per detector: the data its gaps are of


def _note_generation(conn: sqlite3.Connection, detector: str, digest: str | None,
                     run_at: str) -> None:
    """Record that ``detector`` ran over the store of ``digest`` at ``run_at``: the first
    run over that data is kept (``since``), so a gap last seen before it is of older data."""
    key = _GENERATION_KEY + detector
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    try:
        held = json.loads(row[0]) if row is not None else None
    except ValueError:
        held = None
    # and the code it ran: a gap found by older code than the deployed engine is of
    # older code (``_current_since``), never shown as current (``operating.derived_status``)
    code = engine_code()
    if isinstance(held, dict) and held.get("digest") == digest \
            and isinstance(held.get("since"), str) and held.get("code") == code:
        return
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (key, json.dumps({"digest": digest, "since": run_at, "code": code},
                                  sort_keys=True)))


def engine_code() -> str | None:
    """The deployed engine's code version (``operating.derived_status``): what each rule's
    record names, so findings of older code are told apart from this code's."""
    from . import derived_status
    return derived_status.version("proactive-inbox")


def _moment(text: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text) if isinstance(text, str) else None
    except ValueError:
        return None
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _current_since(conn: sqlite3.Connection, served: str | None,
                   older: set[str] | None = None
                   ) -> tuple[dict[str, datetime | None], dict[str, str | None]]:
    """Per detector, from when its gaps are of the data served now (``served``: the store's
    content digest): ``{detector: since}`` for those that ran over it (None: every gap, an
    inbox written before per-detector records whose last full run was over this data), and
    ``{detector: last run}`` for those that did not (their gaps are of older data: never
    shown as current, ``read_inbox``'s ``refreshing``). A rule whose last run was by
    other code than the deployed engine's is behind too (``older``: its name added)."""
    older = set() if older is None else older
    held: dict[str, Any] = {}
    for key, value in conn.execute("SELECT key, value FROM main.meta WHERE key LIKE ?",
                                   (_GENERATION_KEY + "%",)):
        try:
            held[key[len(_GENERATION_KEY):]] = json.loads(value)
        except ValueError:
            held[key[len(_GENERATION_KEY):]] = None   # unreadable: of no known data
    last = conn.execute("SELECT store_digest FROM main.runs ORDER BY run_id DESC "
                        "LIMIT 1").fetchone()
    ran = {row[0]: row[1] for row in conn.execute(
        "SELECT detector, run_at FROM main.detector_runs")}
    statuses = {row[0]: row[1] for row in conn.execute(
        "SELECT detector, status FROM main.detector_runs")}
    current: dict[str, datetime | None] = {}
    behind: dict[str, str | None] = {}
    code = engine_code()
    for name in sorted(set(DETECTORS) | set(ran) | set(held)):
        record = held.get(name, ...)
        if (isinstance(record, dict) and (code is None or record.get("code") != code)) or (
                record is ... and statuses.get(name) in READ_WHOLE_DATA):
            # found by older code than the engine deployed (or one not known): its gaps
            # wait for a run of this code (the deploy's refresh), as older data's do
            behind[name] = ran.get(name)
            older.add(name)
            continue
        if record is ...:
            # no per-detector record (an older engine's inbox): its last full run says
            if last is not None and last[0] == served:
                current[name] = None
            else:
                behind[name] = ran.get(name)
            continue
        since = _moment(record.get("since")) if isinstance(record, dict) else None
        if isinstance(record, dict) and record.get("digest") == served and since is not None:
            current[name] = since
        else:
            behind[name] = ran.get(name)
    return current, behind


def _of_served(row: Mapping[str, Any], current: Mapping[str, datetime | None]) -> bool:
    """Whether a gap was found over the data served now (else it may say what the record
    pages no longer do: never shown as current)."""
    if row["detector"] not in current:
        return False
    since = current[row["detector"]]
    if since is None:
        return True
    # an open gap was found again over it; a resolved one was resolved by a run over it
    keys = row.keys() if hasattr(row, "keys") else ()
    resolved = "state" in keys and row["state"] == "resolved"
    seen = _moment(row["resolved_at"] if resolved else row["last_seen"])
    return seen is not None and seen >= since


def rules_behind(store_path: Any, inbox: Any = None) -> set[str]:
    """The rules whose findings in the inbox are not of the served generation and the
    deployed code (``_current_since``): each needs a whole run before it is current."""
    store = Path(store_path)
    path = Path(inbox) if inbox is not None else inbox_path(store)
    if not path.is_file():
        return set(DETECTORS)
    with closing(_attach(path, store)) as conn:
        served = conn.execute(
            "SELECT value FROM st.store_meta WHERE key = 'content_digest'").fetchone()
        older: set[str] = set()
        _current, behind = _current_since(conn, served[0] if served else None, older)
    return (set(behind) | older) & set(DETECTORS)


def served_behind(store_path: Any, inbox: Any = None) -> bool:
    """Whether the inbox's last full run is over other data than the store served now (or
    there is none): the scheduled refresh is due (``operating.vendor_watch``). Read only."""
    store = Path(store_path)
    path = inbox_path(store) if inbox is None else Path(inbox)
    if not path.is_file():
        return True
    with closing(_attach(path, store)) as conn:
        last = conn.execute("SELECT store_digest FROM main.runs ORDER BY run_id DESC "
                            "LIMIT 1").fetchone()
        served = conn.execute(
            "SELECT value FROM st.store_meta WHERE key = 'content_digest'").fetchone()
        older: set[str] = set()
        _current_since(conn, served[0] if served else None, older)
    # or a rule's findings are of older code than the engine deployed
    return last is None or last[0] != (served[0] if served else None) or bool(older)


def _reader_view(conn: sqlite3.Connection, may_read: Callable[[str], bool],
                 held: Iterable[str]) -> tuple[str, str]:
    """(visible audience keys, allowed markings values) as JSON, from the store's keys."""
    row = conn.execute("SELECT value FROM st.store_meta WHERE key = 'audience_keys'").fetchone()
    keys = json.loads(row[0]) if row is not None else None
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        keys = [r[0] for r in conn.execute("SELECT DISTINCT audience_key FROM st.objects")]
    if _has_requires(conn):   # a gap's required audiences, held by no record maybe
        keys = list(dict.fromkeys([*keys, *(r[0] for r in conn.execute(
            "SELECT DISTINCT audience_key FROM gap_requires ORDER BY audience_key"))]))
    visible = []
    for key in keys:
        try:
            if may_read(key) is True:
                visible.append(key)
        except Exception:  # noqa: BLE001 - a failing grant check denies
            continue
    return json.dumps(visible), json.dumps(ostore.allowed_marking_sets(held))


def _has_requires(conn: sqlite3.Connection) -> bool:
    """An inbox written before ``gap_requires`` has none: its ``REQUIRES_GRANT`` gaps
    then show to nobody until the next run records their requirements."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = "
                        "'gap_requires'").fetchone() is not None


_OBJECT_VISIBLE = (
    "EXISTS (SELECT 1 FROM st.objects so JOIN st.object_versions sv ON sv.object_id = "
    "so.object_id AND sv.superseded_at IS NULL WHERE so.object_id = {o} "
    "AND so.audience_key IN (SELECT value FROM json_each(:aud)) "
    "AND sv.markings IN (SELECT value FROM json_each(:marks)))")


def _visible(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row], params: Mapping[str, str],
             decide: Callable[[Sequence[str]], Mapping[str, Any]]
             ) -> list[tuple[sqlite3.Row, list[str]]]:
    """Each gap the reader may see, with the citations it may open."""
    ids = json.dumps([row["gap_id"] for row in rows])
    cites: dict[str, list[tuple[int, str]]] = defaultdict(list)
    groups: dict[str, set[int]] = defaultdict(set)
    for cite in conn.execute(
            "SELECT gc.gap_id, gc.grp, gc.handle, gc.object_id IS NOT NULL AND gc.markings IN "
            "(SELECT value FROM json_each(:marks)) AND "
            + _OBJECT_VISIBLE.format(o="gc.object_id")
            + " AS shown FROM gap_cites gc WHERE gc.gap_id IN (SELECT value FROM "
            "json_each(:ids)) ORDER BY gc.gap_id, gc.grp, gc.handle", {**params, "ids": ids}):
        if cite["grp"] >= 0:
            groups[cite["gap_id"]].add(cite["grp"])
        if cite["shown"]:
            cites[cite["gap_id"]].append((cite["grp"], cite["handle"]))
    requires: dict[str, set[str]] = defaultdict(set)
    if _has_requires(conn):
        for req in conn.execute(
                "SELECT gap_id, audience_key FROM gap_requires WHERE gap_id IN (SELECT value "
                "FROM json_each(:ids))", {"ids": ids}):
            requires[req["gap_id"]].add(req["audience_key"])
    granted = set(json.loads(params["aud"]))
    handles = sorted({h for found in cites.values() for _, h in found})
    refused = {h for h, why in decide(handles).items() if why is not None} if handles else set()
    out = []
    for row in rows:
        open_ = [(grp, h) for grp, h in cites.get(row["gap_id"], []) if h not in refused]
        need = groups.get(row["gap_id"], set())
        required = requires.get(row["gap_id"], set())
        if row["detector"] in REQUIRES_GRANT and not (required and required <= granted):
            continue   # a verdict on what a source does not hold: the run's grant only
        if need and need <= {grp for grp, _ in open_}:
            out.append((row, list(dict.fromkeys(h for _, h in open_))))
    return out


def _attach(path: Path, store: Path, *, write: bool = False) -> sqlite3.Connection:
    mode = "rw" if write else "ro"
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, isolation_level=None,
                           timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if not write:
            conn.execute("PRAGMA query_only = ON")
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if row is None or row[0] != INBOX_SCHEMA:
            raise ValueError("gap_inbox_schema_mismatch")
        if not Path(store).is_file():
            raise ostore.StoreError("store_missing")
        conn.execute("ATTACH DATABASE ? AS st", (f"file:{store}?mode=ro",))
        row = conn.execute("SELECT value FROM st.store_meta WHERE key = 'format'").fetchone()
        if row is None or row[0] != ostore.FORMAT:
            raise ostore.StoreError("store_format_mismatch")
    except BaseException:
        conn.close()
        raise
    return conn


def _rule_coverage(conn: sqlite3.Connection, params: Mapping[str, str],
                   detectors: Mapping[str, str]) -> list[dict[str, Any]]:
    """Per rule, its last run's status and how many records this reader may read it left
    unjudged (``not_evaluated``): counted over the records the reader's grant shows, of
    the skips whose whole cause (every record at its markings) the reader may read, never
    the store's: a skip a hidden record or link caused is not counted, so the count never
    hints at it. None (unknown, never 0) when the rule left a skip no record stands for,
    or the inbox predates per-skip causes while its run counted skips."""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    recorded: dict[str, int] = {}
    visible: dict[str, int] = {}
    if {"rule_coverage", "unjudged_causes"} <= tables:
        recorded = {row[0]: row[1] for row in conn.execute(
            "SELECT detector, unattributed FROM rule_coverage")}
        visible = {row[0]: row[1] for row in conn.execute(
            "SELECT u.detector, count(DISTINCT u.object_id) FROM unjudged_causes u "
            "WHERE u.subject = 1 AND NOT EXISTS (SELECT 1 FROM unjudged_causes c WHERE "
            "c.detector = u.detector AND c.skip = u.skip AND NOT (c.markings IN (SELECT "
            "value FROM json_each(:marks)) AND " + _OBJECT_VISIBLE.format(o="c.object_id")
            + ")) GROUP BY u.detector", params)}
    counted = {row[0]: row[1] for row in conn.execute(
        "SELECT detector, not_evaluated FROM detector_runs")}
    said = {row[0]: row[1] for row in conn.execute(
        "SELECT detector, reasons FROM detector_runs")}
    out = []
    for name in DETECTORS:
        try:
            skips = sum(int(v) for v in json.loads(counted.get(name) or "{}").values())
        except (ValueError, TypeError, AttributeError):
            skips = 1   # unreadable: never read as nothing skipped
        if name not in recorded:
            shown = None if skips else 0
        else:
            shown = None if recorded[name] else visible.get(name, 0)
        try:
            given = json.loads(said.get(name) or "[]")
            reasons = [r for r in given if isinstance(r, str)] if isinstance(given, list) else []
        except (ValueError, TypeError):
            reasons = []    # unreadable: no reason is said, never a made-up one
        out.append({"rule": name, "status": detectors.get(name, "not_run"),
                    "not_evaluated": shown, "reasons": reasons})
    return out


def read_inbox(inbox: Any, store: Any, *, reader: str, may_read: Callable[[str], bool],
               held: Iterable[str], decide: Callable[[Sequence[str]], Mapping[str, Any]],
               states: Sequence[str] = ("open", "acknowledged"),
               limit: int = MAX_SHOWN, kinds: Sequence[str] | None = None) -> dict[str, Any]:
    """The gaps one reader may see, trimmed: every late order first (longest late first), then
    by priority; what the last run
    covered of what this reader may read. ``may_read(audience_key)``, ``held`` (the
    markings the reader holds) and ``decide(handles)`` (None per handle it may open)
    are the reader's grant; anything they refuse is never shown nor counted. ``complete``
    is true only when every rule ran to the end; ``not_run`` names each rule that did not
    (not configured on this schedule, blocked, or never run), so no reader takes an
    inbox some rules never looked at for a clean result. ``kinds``: only gaps of these
    kinds are read (the briefing's events), so other kinds never use up ``limit``."""
    path = Path(inbox)
    if not path.is_file():
        return {"status": "not_scheduled", "gaps": [], "scanned": 0, "truncated": False,
                "max_objects": None, "unscanned": None, "unread": {}, "complete": False,
                "not_run": {name: "not_run" for name in DETECTORS}}
    unknown = set(states) - set(STATES)
    if unknown:
        raise ValueError("gap_state_unknown")
    from . import feedback_signals
    # people's feedback on a gap (not useful, wrong): suppressed for whom it covers
    hide = feedback_signals.suppressor(feedback_signals.state_of(store))
    with closing(_attach(path, Path(store))) as conn:
        aud, marks = _reader_view(conn, may_read, held)
        params = {"aud": aud, "marks": marks}
        allowed = set(json.loads(marks))
        readable = set(json.loads(aud))   # the audiences this reader may read
        last = conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        digest = conn.execute(
            "SELECT value FROM st.store_meta WHERE key = 'content_digest'").fetchone()
        older: set[str] = set()
        current, behind = _current_since(conn, digest[0] if digest else None, older)
        scanned = conn.execute(
            "SELECT COALESCE(SUM(objects), 0) FROM scope WHERE audience_key IN (SELECT value "
            "FROM json_each(:aud)) AND markings IN (SELECT value FROM json_each(:marks))",
            params).fetchone()[0]
        runs = conn.execute("SELECT detector, status, reasons, not_evaluated FROM "
                            "detector_runs ORDER BY detector").fetchall()
        detectors = {row["detector"]: row["status"] for row in runs}
        reasons = {row["detector"]: json.loads(row["reasons"]) for row in runs
                   if row["reasons"] not in (None, "[]")}
        # the late rule's skips by reason (store-wide, never per reader: a restricted
        # reader is told at most that the count is not exact, never what was skipped)
        late_skips = next((row["not_evaluated"] for row in runs
                           if row["detector"] == LATE_RULE), None)
        try:
            late_skips = json.loads(late_skips) if isinstance(late_skips, str) else None
        except ValueError:
            late_skips = None   # unreadable: never an exact count
        # each rule's recorded runs over this data: whether one ever found something, and
        # when first (the counts are the run's, never per reader)
        # an inbox an earlier engine wrote may lack the history tables (its next run adds
        # them): that history is unknown, the rest of the inbox still reads
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type = 'table'")}
        has_history = "detector_history" in tables
        first_found = {row[0]: row[1] for row in (conn.execute(
            "SELECT kind, min(run_at) FROM kind_history WHERE found > 0 GROUP BY kind")
            if "kind_history" in tables else ())}
        history = {row["detector"]: {"runs": row["runs"], "first_found": row["first"]}
                   for row in (conn.execute(
                       "SELECT detector, count(*) AS runs, min(CASE WHEN found > 0 THEN "
                       "run_at END) AS first FROM detector_history GROUP BY detector")
                       if has_history else ())}
        stored = {"open"} if set(states) & {"open", "acknowledged"} else set()
        stored |= {"resolved"} & set(states)
        global_states = json.dumps(sorted(stored))
        shown: list[dict[str, Any]] = []
        more = False
        orders: dict[str, tuple[Any, dict[str, Any]]] = {}   # order key -> (rank, entry)
        offset, page = 0, max(limit + 1, 200)
        while not more:
            rows = conn.execute(
                "SELECT g.*, (SELECT at FROM acks a WHERE a.gap_id = g.gap_id AND a.reader = "
                ":reader) AS acked FROM gaps g WHERE g.state IN (SELECT value FROM "
                "json_each(:states)) AND (:kinds IS NULL OR g.kind IN (SELECT value FROM "
                "json_each(:kinds))) AND " + _OBJECT_VISIBLE.format(o="g.object_id")
                + " ORDER BY json_extract(g.follow_up, '$.days_late') IS NULL, "
                "json_extract(g.follow_up, '$.days_late') DESC, "
                # a list of near-expiry lots alone: soonest expiry first, so a page cut
                # short still holds every lot expiring before its last one
                "CASE WHEN :soonest THEN json_extract(g.follow_up, '$.event.days') END, "
                "g.priority, g.last_seen DESC, g.gap_id LIMIT :page OFFSET :offset",
                {**params, "reader": reader, "states": global_states, "page": page,
                 "soonest": kinds is not None and list(kinds) == ["lot_near_expiry"],
                 "offset": offset,
                 "kinds": None if kinds is None else json.dumps(list(kinds))}).fetchall()
            if not rows:
                break
            offset += len(rows)
            # a gap found over older data than the store served now is never shown: the
            # record page reads the served data, so the two would disagree
            rows = [row for row in rows if _of_served(row, current)]
            for row, citations in _visible(conn, rows, params, decide):
                state = ("resolved" if row["state"] == "resolved"
                         else "acknowledged" if row["acked"] else "open")
                if state not in states or hide(row["gap_id"], reader) is not None:
                    continue
                follow_up = json.loads(row["follow_up"])
                order = follow_up.pop(_ORDER, None)
                held = orders.get(order["key"]) if isinstance(order, dict) else None
                if held is not None:
                    # this order is listed: its governing record's item replaces a twin's,
                    # never a second item (each was decided visible to this reader)
                    if order["rank"] < held[0]:
                        index = next(i for i, g in enumerate(shown) if g is held[1])
                        shown[index] = entry = _shown(row, citations, state, follow_up, allowed, readable)
                        orders[order["key"]] = (order["rank"], entry)
                    continue
                if len(shown) >= limit:
                    more = True
                    break
                entry = _shown(row, citations, state, follow_up, allowed, readable)
                shown.append(entry)
                if isinstance(order, dict):
                    orders[order["key"]] = (order["rank"], entry)
        rules = _rule_coverage(conn, params, detectors)
    failed = sorted(name for name, status in detectors.items() if status == "failed")
    truncated = any(status == "truncated" for status in detectors.values())
    # A rule that did not run (not configured on this schedule, blocked on a source, or
    # with no row at all) looked for nothing: the read is never complete while one remains.
    not_run = {name: detectors.get(name, "not_run") for name in DETECTORS
               if detectors.get(name) not in ("complete", "truncated", "failed")}
    # the rules whose findings are of older data than the store served now: theirs are not
    # shown until they run over it (the scheduled refresh), and the read says so
    refreshing = sorted(name for name in behind if name in detectors)
    checked = sorted(at for name in refreshing if isinstance(at := behind[name], str))
    return {"status": "read", "as_of": last["as_of"] if last else None,
            "run_at": last["run_at"] if last else None,
            "stale": bool(refreshing) or (bool(last) and (digest[0] if digest else None)
                                          != last["store_digest"]),
            "refreshing": refreshing, "checked_on": checked[0] if checked else None,
            # of those, the rules whose findings older code made: held back until the
            # deployed engine's run (``operating.derived_status refresh``)
            "older_code": sorted(name for name in older if name in detectors),
            "scanned": scanned, "truncated": truncated or bool(failed),
            "max_objects": None, "unscanned": None,
            "unread": {f"{name}_failed": 1 for name in failed},
            "detectors": detectors, "detector_reasons": reasons, "history": history,
            "kind_history": first_found,
            "not_run": not_run, "rules": rules,
            "complete": not (truncated or failed or not_run or refreshing),
            # the late count is the late rule's alone: exact when that rule ran to the end
            # over the data served and left no order its rule could not decide (the one
            # late-list rule, ``po_status.late_count_exact``); another rule cut short
            # never makes it inexact
            "late_exact": (detectors.get(LATE_RULE) == "complete"
                           and LATE_RULE not in refreshing
                           and po_status.late_count_exact(late_skips)),
            "gaps": shown, "more": more, "limit": limit}


def _shown(row: Mapping[str, Any], citations: Sequence[str], state: str,
           follow_up: dict[str, Any], allowed: Iterable[str] = (),
           audiences: Iterable[str] = ()) -> dict[str, Any]:
    """One inbox row as its reader sees it (``follow_up`` without the order field)."""
    days_late = follow_up.pop("days_late", None)
    event = follow_up.pop("event", None)
    summary = row["summary"]
    caveat = follow_up.pop("promise_caveat", None)
    if caveat is not None:
        unless = caveat.get("unless") if isinstance(caveat, dict) else None
        # a reader who may not read every audience a promise sits in (or a caveat this
        # engine cannot read) is told a later promise may exist; the audiences are never
        # shown
        if not isinstance(unless, list) or caveat.get("always") is not False \
                or not set(unless) <= set(audiences):
            summary = pg._clip(summary, pg.MAX_TEXT - len(PROMISE_CAVEAT_TEXT)) \
                + PROMISE_CAVEAT_TEXT
            event = {**(event if isinstance(event, dict) else {}), "caveat": PROMISE_CAVEAT}
    if isinstance(event, dict) and isinstance(event.get("amount"), dict):
        amount = event["amount"]
        handle = amount.get("handle")
        needed = {h for h in (handle, amount.get("currency_handle")) if h}
        if handle is not None and (not needed <= set(citations) or amount.get(
                "markings", UNREADABLE) not in set(allowed)):
            event.pop("amount")   # a figure this reader may not open is never shown
        else:
            scope = amount.pop("suspect_scope", None)
            plain = amount.pop("suspect_plain", None)
            if scope is not None and (scope.get("audience") not in set(audiences)
                                      or scope.get("markings") not in set(allowed)):
                amount["suspect"] = plain   # its peers' scope is not this reader's: no band
            lines = amount.get("lines")
            if isinstance(lines, dict):
                hidden = lines.get("verdict") != "unpriced" and (
                    lines.get("handles") is None
                    or not set(lines["handles"]) <= set(citations)
                    or lines.get("markings", UNREADABLE) not in set(allowed))
                # a line this reader may not open: nothing its lines say is stated, only
                # that the total was not checked against them
                amount["lines"] = {"verdict": "not_open"} if hidden else {
                    k: v for k, v in lines.items() if k in ("verdict", "lines_sum")}
    shown_by = follow_up.pop("confirmation_cites", None)
    if isinstance(shown_by, list) and not set(shown_by) & set(citations):
        follow_up.pop("confirmation", None)   # a confirmation this reader may not open
    draft = follow_up.get("draft")
    if isinstance(draft, dict) and not set(draft.get("citations") or ()) <= set(citations):
        follow_up.pop("draft")   # a fact it states is not this reader's to open
    return {"id": row["gap_id"], "kind": row["kind"],
            "object_id": row["object_id"], "object_type": row["object_type"],
            "summary": summary, "citations": citations,
            "detected_by": "rule", "state": state,
            "first_seen": row["first_seen"], "last_seen": row["last_seen"],
            **({"days_late": days_late} if days_late is not None else {}),
            **({"event": event} if isinstance(event, dict) and event else {}),
            "follow_up": follow_up}


def acknowledge(inbox: Any, store: Any, gap_id: str, *, reader: str,
                may_read: Callable[[str], bool], held: Iterable[str],
                decide: Callable[[Sequence[str]], Mapping[str, Any]],
                clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
                ) -> dict[str, Any]:
    """Record that this reader saw an open gap; a gap it may not see is refused alike
    whether it exists or not."""
    path = Path(inbox)
    if not isinstance(gap_id, str) or not path.is_file():
        raise PermissionError("gap_not_visible")
    with closing(_attach(path, Path(store), write=True)) as conn:
        aud, marks = _reader_view(conn, may_read, held)
        params = {"aud": aud, "marks": marks}
        rows = conn.execute(
            "SELECT g.* FROM gaps g WHERE g.gap_id = :id AND g.state = 'open' AND "
            + _OBJECT_VISIBLE.format(o="g.object_id"), {**params, "id": gap_id}).fetchall()
        if not _visible(conn, rows, params, decide):
            raise PermissionError("gap_not_visible")
        at = clock().isoformat()
        conn.execute("INSERT OR IGNORE INTO acks VALUES (?, ?, ?)", (gap_id, reader, at))
    return {"id": gap_id, "state": "acknowledged"}


def hidden_counts(inbox: Any, store: Any, *, kinds: Sequence[str],
                  may_read: Callable[[str], bool], held: Iterable[str],
                  decide: Callable[[Sequence[str]], Mapping[str, Any]]) -> dict[str, int]:
    """Per kind, the open gaps this reader may NOT see (its subject, a citation group or a
    required grant withheld): a count, never an id, a subject or a summary. No inbox: {}."""
    path = Path(inbox)
    if not path.is_file():
        return {}
    with closing(_attach(path, Path(store))) as conn:
        aud, marks = _reader_view(conn, may_read, held)
        params = {"aud": aud, "marks": marks, "kinds": json.dumps(list(kinds))}
        served = conn.execute(
            "SELECT value FROM st.store_meta WHERE key = 'content_digest'").fetchone()
        current, _ = _current_since(conn, served[0] if served else None)
        # a gap of older data than the store served is shown to no one (``read_inbox``):
        # it is not counted as one hidden from this reader either
        total = Counter(row["kind"] for row in conn.execute(
            "SELECT kind, detector, last_seen FROM gaps WHERE state = 'open' AND kind IN "
            "(SELECT value FROM json_each(:kinds))", params) if _of_served(row, current))
        rows = [row for row in conn.execute(
            "SELECT g.* FROM gaps g WHERE g.state = 'open' AND g.kind IN (SELECT value FROM "
            "json_each(:kinds)) AND " + _OBJECT_VISIBLE.format(o="g.object_id"),
            params).fetchall() if _of_served(row, current)]
        seen = Counter(row["kind"] for row, _ in _visible(conn, rows, params, decide))
    return {kind: total[kind] - seen[kind] for kind in sorted(total)
            if total[kind] - seen[kind] > 0}


_VISITS_DDL = ("CREATE TABLE IF NOT EXISTS visits (reader TEXT PRIMARY KEY, at TEXT NOT NULL, "
               "previous TEXT)")


def last_visit(inbox: Any, reader: str) -> str | None:
    """When this reader last marked its briefing seen (``record_visit``); None: never."""
    path = Path(inbox)
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = "
                        "'visits'").fetchone() is None:
            return None
        row = conn.execute("SELECT at FROM visits WHERE reader = ?", (reader,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def record_visit(inbox: Any, reader: str, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> str:
    """Mark this reader's briefing seen now (the Workbench's own inbox; nothing else is
    written). An inbox the engine never wrote is refused."""
    path = Path(inbox)
    if not path.is_file():
        raise FileNotFoundError("gap_inbox_missing")
    at = clock().isoformat()
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if row is None or row[0] != INBOX_SCHEMA:
            raise ValueError("gap_inbox_schema_mismatch")
        conn.execute(_VISITS_DDL)
        conn.execute("INSERT INTO visits (reader, at, previous) VALUES (?, ?, NULL) ON CONFLICT "
                     "(reader) DO UPDATE SET previous = visits.at, at = excluded.at",
                     (reader, at))
    finally:
        conn.close()
    return at


# The markings the engine's own reader holds in its tool calls: an invoiced order's margin
# rests on finance-marked money (BC and Sage revenue and cost), so without it every margin
# is withheld and ``negative_margin`` never decides one. Its findings cite each figure at
# its value's markings, so a reader who does not hold finance never sees the loss
# (``read_inbox`` trims per reader).
ENGINE_MARKINGS = ("finance",)


def engine_marking_grants(actor: Any) -> dict[str, list[str]]:
    """``marking_grants`` of the engine's case sessions: ``ENGINE_MARKINGS`` held by the
    run principal (``actor``'s principals) only."""
    return {marking: sorted(actor.principals) for marking in ENGINE_MARKINGS}


def refresh(runtime: Any, investigator: Any, audience: Any, projection: Path, as_of: date,
            *, captures: Path | None, scan_coverage: bool = True,
            budget: Budget | None = None,
            clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
            vendor_bound: int | None = None, pool: Any = None, threads: int | None = None,
            detector_seconds: Mapping[str, float] | None = None,
            only: Sequence[str] | None = None) -> dict[str, Any]:
    """The full scheduled run over ``projection`` into its inbox, as ``investigator``: the
    customer PO coverage scan of the runtime's mirror (``scan_coverage``; its order
    statuses ``absent_in_this_source``) and a case session over the projection and the
    state directory's captures as the tool caller (``orders_at_risk``, ``card_rules``).
    ``investigator`` must hold every source the engine's verdicts rest on (the host's
    capture principal with its capture audience: Portal and BC, ``host.engine_readers``);
    ``orders_at_risk`` is ``blocked`` (``reader_lacks_lateness_access``) when it does not.
    The inbox stores each gap with its citations' audiences; ``read_inbox`` trims it per
    reader. Shared by ``main`` and the host's ``after_build(scheduled=)``. The store-only
    detectors run side by side (``threads``, default ``detector_threads()``), each detector
    in its own window (``DETECTOR_SECONDS``, ``detector_seconds`` overriding some)."""
    from . import case_workspace_live as live
    from . import customer_po_coverage

    def coverage() -> Mapping[str, Any]:
        return customer_po_coverage.coverage(runtime.mirror, investigator,
                                             statuses=("absent_in_this_source",))

    session = live._Session(runtime, investigator, audience, projection, captures=captures,
                            marking_grants=engine_marking_grants(investigator))
    try:
        # the reader the engine's verdicts are read as (the capture principal, granted
        # what it captured: ``host.engine_readers``): every reader's inbox and briefing
        # are trimmed from them, so a rule it cannot judge for lack of a grant says so.
        # Decided over the units the store holds (the keys it checks are stored ones):
        # no live BC company listing is requested for it.
        policy = session.host.stored_policy(investigator)
        return run(projection, inbox_path(projection), as_of,
                   coverage=coverage if scan_coverage else None, call=session,
                   budget=budget, clock=clock, vendor_bound=vendor_bound,
                   reader_may_read=lambda key: policy.may_read(investigator, key),
                   pool=pool, threads=threads, detector_seconds=detector_seconds, only=only)
    finally:
        session.close()


def after_build(store_path: Any, *, as_of: date | None = None,
                clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                scheduled: Callable[[date, Budget], Mapping[str, Any]] | None = None,
                budget_seconds: float | None = None,
                monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """The host's hook after a projection build, never raising; counts and enums only.
    ``scheduled(as_of, budget)``: the full run (``refresh``) within a ``budget_seconds``
    budget; without it the store-only detectors (no mirror scan, no cards). Judged on
    ``as_of``, else the business day (ET) of ``clock()``."""
    try:
        if scheduled is None:
            found = run(store_path, inbox_path(store_path), as_of, clock=clock)
        else:
            found = scheduled(po_status.business_today(clock()) if as_of is None else as_of,
                              Budget(REFRESH_BUDGET_SECONDS if budget_seconds is None
                                     else budget_seconds, monotonic,
                                     reserve=AMOUNTS_RESERVE_SECONDS))
    except Exception as error:  # noqa: BLE001 - detection never fails a capture cycle
        return {"state": "failed", "error": type(error).__name__}
    return {"state": "ran", "gaps_found": found["gaps_found"], "detectors": found["detectors"],
            **({"detector_reasons": found["detector_reasons"]}
               if found.get("detector_reasons") else {})}


# --------------------------------------------------------------------------- live command

def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         host_options: Mapping[str, Any] | None = None, graph_sender: Any = None) -> int:
    from integrations.source_mirror import AudienceMap
    from ontology.core import Actor
    from operator_ui.object_workspace import HostAccess

    from . import case_workspace_live as live

    parser = argparse.ArgumentParser(prog="python3 -m operating.proactive_engine",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--mirror-audience", required=True, type=Path)
    parser.add_argument("--tenant", default="plx")
    parser.add_argument("--principal", action="append", required=True,
                        help="the investigator's principals (repeat)")
    parser.add_argument("--clearance", default="internal")
    parser.add_argument("--reader", action="append", default=[], metavar="P1,P2",
                        help="another reader whose inbox is counted (repeat)")
    parser.add_argument("--host-config", type=Path, default=None,
                        help="run one host capture cycle into --state-dir first")
    parser.add_argument("--live-source-audience", type=Path, default=None)
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        audience = AudienceMap.from_file(args.mirror_audience)
        readers = [live._reader(text, args.tenant, args.clearance) for text in args.reader]
    except (OSError, ValueError) as error:
        parser.error(f"invalid input: {type(error).__name__}")
    if audience.tenant != args.tenant:
        parser.error("mirror audience tenant must equal --tenant")
    as_of = args.as_of or po_status.business_today()
    summary: dict[str, Any] = {"schema": SCHEMA, "evidence": live.MIRROR_EVIDENCE["label"],
                               "as_of": as_of.isoformat(), "capture": None}
    if args.host_config is not None:
        code, summary["capture"] = live._host_cycle(args, env, host_options or {})
        if code != 0:
            print(json.dumps(summary, indent=1, sort_keys=True))
            return 1
    investigator = Actor(args.tenant, frozenset(args.principal), args.clearance)
    readers_live = None
    if args.live_source_audience is not None:
        from .case_runtime import build_live_readers
        from .ingestion import IngestError, load_live_audience
        try:
            readers_live = build_live_readers(
                load_live_audience(args.live_source_audience, env), env,
                graph_sender=graph_sender, bc_sender=(host_options or {}).get("sender"))
        except IngestError as error:
            parser.error(f"live source audience is invalid: {error.code}")
    runtime = live.offline_runtime(args.state_dir, args.mirror_audience, args.tenant,
                                   live=readers_live, environ=env)
    projection = live.projection_file(args.state_dir, args.tenant)
    inbox = inbox_path(projection)
    rendered = []
    try:
        live.project_state(args.state_dir, projection, tenant=args.tenant)
        summary["blocked_upstream"] = live.upstream_parts(live.portal_upstream(args.state_dir))
        summary.update(refresh(runtime, investigator, audience, projection, as_of,
                               captures=args.state_dir))
        for actor in (investigator, *readers):
            session = live._Session(runtime, actor, audience, projection,
                                    captures=args.state_dir)
            try:
                access = HostAccess(session.host, runtime, actor)
                with access.request():
                    may_read, held = access.grant()
                    found = read_inbox(inbox, projection, reader=reader_key(actor),
                                       may_read=may_read, held=held, decide=access.decide)
            finally:
                session.close()
            rendered.append({"reader": sorted(actor.principals),
                             "investigator": actor is investigator,
                             "scanned": found["scanned"], "shown": len(found["gaps"]),
                             "gaps_by_kind": dict(sorted(Counter(
                                 g["kind"] for g in found["gaps"]).items()))})
    finally:
        runtime.close()
    summary["readers"] = rendered
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 1 if "failed" in summary.get("detectors", {}).values() else 0


if __name__ == "__main__":
    sys.exit(main())
