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
* the business alerts (``operating.proactive_business``): ``stock_out_risk`` (open customer
  orders an item's on hand plus inbound purchase orders cannot cover by their ship dates;
  ``REQUIRES_GRANT``), ``overdue_receivable`` and ``unit_cost_jump`` (finance-marked);
* the operations alerts (``operating.proactive_ops``): overdue and stuck Mission Control
  tasks, MC approvals waiting, overdue Planner tasks, aged receivables and payables
  worsening (finance-marked), late production and assembly damage spikes, and Teams asks a
  person left unanswered (people cleared first; ``REQUIRES_GRANT``);
* the chief-executive signals (``operating.proactive_ceo``): ``customer_demand_drop`` (a
  customer's last 90 days of orders against its run-rate, finance-marked),
  ``promise_date_slip`` (a customer's orders missing their promised date, with a trend) and
  ``supplier_reliability`` (a supplier's late receipts getting worse), all
  ``REQUIRES_GRANT``; ``margin_erosion`` (recent invoiced margin against the earlier
  orders', from the negative-margin rule's order margins, finance-marked);
  ``shipped_not_invoiced`` (``REQUIRES_GRANT``), ``duplicate_customer_order`` and
  ``recurring_component_damage``;
* ``operating.proactive_chasers``: ``customer_chase`` (customers chasing where an order is,
  and orders to tell them about first; people cleared first, ``REQUIRES_GRANT``) and
  ``quality_holds`` (batches and lots on hold past their days, reject trends);
* ``operating.proactive_cash``: ``customer_payment_behaviour`` (a customer paying slower
  than its own prior year), ``early_pay_discounts`` (a supplier discount missed) and
  ``duplicate_payables`` (a supplier invoice or payment entered twice),
  ``returns_and_credits`` (a customer's credit notes rising against its own year), finance-marked;
* ``operating.proactive_stock``: ``slow_moving_stock`` and ``assembly_component_shortfall``
  (``REQUIRES_GRANT``), ``expiring_lot_stock``, ``certificate_expiry`` and
  ``freight_cost_anomaly`` (finance-marked), each ``blocked`` naming a field not served;
  ``reorder_soon`` (forecast demand, ``operating.demand_forecast``, past on hand plus
  inbound; ``REQUIRES_GRANT``, ``blocked`` while on hand is not served);
* ``operating.proactive_prices``: ``price_consistency`` (a customer paying far below the
  typical price of a product, or below its cost, over the last 180 days), finance-marked;
* ``operating.proactive_sales``: ``stale_quote`` (a Portal or Business Central quote open past
  its days with no order; ``REQUIRES_GRANT``; the value finance-marked in the event);
* ``operating.proactive_quality``: ``duplicate_parties`` (customers and suppliers that may be
  one, each a review proposal a person confirms), ``key_fields_missing`` (an active record
  without a key field, ``REQUIRES_GRANT``), ``twin_disagreement`` (copies in two systems
  stating different values) and ``stale_captures`` (a source not captured lately);
* ``operating.proactive_sync``: ``sync_gaps`` (a record missing from, or differing on a key
  field in, the system it must reach ``SYNC_DAYS`` after it was made; ``REQUIRES_GRANT``);
* every gap about a sales or purchase order passes the one active-population predicate
  (``inactive_records``, ``_drop_inactive_orders``): a closed or historic order is counted
  ``closed_order``/``historic_order``, never listed (``CLOSED_ORDER_RULES`` keep closed ones);
* ``operating.proactive_commitments``: ``commitments`` (promises in our messages about an
  open customer order, due or missed) and ``needs_reply`` (inbound mail and one-to-one
  Teams messages waiting on their addressee's reply, shown to that person only); people
  cleared first, ``REQUIRES_GRANT``;
* ``card_rules``: ``missing_in_record`` and attested conflicts from the ``plx.object``
  cards of the documents that attest a record (bounded per-object reads, ``CARD_BOUND``).

A gap carries the citations its claim rests on, grouped (each group must stay cited for a
reader to see the gap), a stable id and a proposed ``follow_up``
(``operator_api.workspace.object_workspace.FOLLOW_UP_FIELDS``) that only a person sends. ``run``
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
from collections import Counter, OrderedDict, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import functools
import hashlib
import json
import math
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

from . import alert_kinds, copy_gate, drafts
from .copy_gate import say_date
from . import active_population as active
from . import po_status
from . import test_records as test_records_rule
from . import proactive_gaps as pg
from . import sales_order_status as so_status
from . import suspect_money
from . import order_value
from operating import declared_schema as _schemas
from operating.declared_schema import proactive_inbox as _PROACTIVE_INBOX
from operating.declared_schema.proactive_inbox import _VISITS_DDL
from . import time_window

from .proactive_engine_config import (
    AMOUNTS_RESERVE_SECONDS, _CADENCE_KEY, CARD_BOUND, Cite, CONFIRMATION_LINK, CONFIRMING,
    DETECTOR_CADENCE_HOURS, DETECTOR_SECONDS, DETECTOR_SECONDS_DEFAULT, DETECTORS,
    HARD_GRACE_SECONDS, INBOX_SCHEMA, LATE_RULE, LINK_CACHE_ROWS, LINK_COLUMNS,
    LINK_PARTS_PER_THREAD, MAX_SHOWN, ON_RUN_THREAD, PARALLEL_LINK_ROWS, PG_TYPE_READ_MAX,
    PG_VALUES_CHUNK, RECEIPT_LINKS, REFRESH_BUDGET_SECONDS, RUN_THREAD_GRACE_SECONDS, SCHEMA,
    SCOPE_CHUNK, SET_BASED, SETTLED, SETUP_STATES, STATES, TIME_BUDGET, VALUES_CHUNK)
from .proactive_engine_connections import (
    _connect, _link_row, _LinkRows, PassCache, _PG_VALUES_SQL, scan_threads, TEST_POPULATION,
    _ThreadConn,
)
from .proactive_engine_findings import (
    _canonical, _cites, declared_qc_links, _gap, _identity_link_types, _link_ends, _result,
    _rule_link_types, _Skips, WHOLE_BY_INDEX_MAX)
from .proactive_engine_purchasing import (
    _ATTESTS, _late_or_unconfirmed_po, _mention_resolver, _ORDER, _po_source)
from .proactive_engine_sales import (
    _absence_audiences, _customer_orders, _late_customer_order, SHIP_LINKS, WORK_ALLOCATION,
    WORK_LINE_LINKS)
from .proactive_engine_quantities import (
    _conflicting_quantity, _expired_quote, _order_without_customer_po,
    _po_receipt_quantity_mismatch, _receipt_without_qc)
from .proactive_engine_drafts import (
    _caveat_all, _late_order, _late_orders, _promise_audiences, _reader_judges_lateness, _worded)
from .proactive_engine_cards import (
    _card_rules, IMPACT_KINDS, _late_found, _margin_keeps, _margin_reader, _ops_steps, _order_state,
    _orders_at_risk, _sheets, _with_amounts)
from .proactive_engine_inbox import (
    _attach, _built_table, _carried, _COVERAGE_KEPT, _coverage_kept, _coverage_key, _coverage_lock,
    _current_since, engine_code, engine_marking_grants, _fold, _fold_scoped, _generation_of,
    GROUP_SECONDS, group_seconds, _held_unchecked, inbox_path, _learned_order, _OBJECT_VISIBLE,
    _of_served, _open_inbox, _owned, _OWNER_KINDS, PASS_KEY, pass_record, _people, _populations,
    _POPULATIONS_KEY, _read_cache, READ_WHOLE_DATA, reader_key, _reader_view, _resumable,
    _served_digest, _shown, _summary, _table_from_disk, _tables_building, _TABLES_KEPT,
    _tables_kept, _today, UNJUDGED_SHOWN, UnknownCheck, _visible, _write_pass)
from .proactive_engine_budget import (
    Budget, _cgroup_limit, _cgroup_used, detector_reserve, DETECTOR_RESERVE_BYTES, detector_threads,
    _failure_cause, HELPER_JOIN_SECONDS, HOST_MEMINFO, _lanes, _margin_head_start,
    MEMORY_GATE_POLL_SECONDS, MEMORY_GATE_SHARE, MEMORY_SHARE_ENV, _not_due, _own_cgroup, pass_lock,
    _Runner, _share, TimeBudgetSpent, _Watchdog)
from .proactive_engine_population import (
    _drop_inactive_orders, inactive_records, _po_classes, TEST_ORDER_LINKS, test_records,
    TEST_TWIN_LINKS)


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
                 audience_keys: Iterable[str] | None = None, file_only: bool = False,
                 shared: PassCache | None = None) -> None:
        from operating.pg import ontology_reader
        self.file_only = file_only   # the file at ``path`` only (``_connect``)
        if not Path(path).is_file() and (file_only or not ontology_reader.pg_only()):
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
        self._conn_thread: dict[int, threading.Thread] = {}   # id(conn) -> the thread it reads on
        self._helpers: list[threading.Thread] = []
        self._conns_lock = threading.Lock()
        # idle connections the ranged scans' parts read on (``_part_conn``): kept for the
        # run, never one opened and closed per range
        self._parts: list[Any] = []
        self.interrupt: Callable[[], int] | None = None
        self.left: Callable[[], float] | None = None
        # the file the recent-mail read keeps what it judged in (set by a run over an inbox)
        self.cursor_path: Path | None = None
        self.pin: Callable[[], Callable[[], int]] | None = None
        problem = ostore.format_problem(self.conn)
        if problem is not None:
            self.close()
            raise ostore.StoreError("store_format_mismatch", problem)
        from operating.pg.sqlite_compat import PgCompatConnection
        # the Postgres ontology (``_connect``): value reads join its value tables directly
        self.pg = isinstance(self.conn, PgCompatConnection)
        self.digest = self.meta("content_digest")
        self.threads = scan_threads() if threads is None else max(1, threads)
        self.link_rows = _LinkRows()
        # a pass's groups share what one read (``PassCache``): never in an incremental
        # pass (its reads are narrowed to its scope) nor a scratch file of a few records
        self._pass = shared if self.limit is None and not file_only else None
        if self._pass is not None:   # under this group's pass lock, before any read
            self._pass.current(self.data_stamp())
        self._asked: set[str] = set()   # the link types this store asked for
        # the budget's SQLite progress handler (``Budget.interrupt``, ``interrupt``), on
        # every connection this store opens: a scan stops at the deadline as the detectors'
        # queries do (it reads the window of the thread running the statement)
        self._memo: dict[str, Any] = {}
        self._making: dict[str, threading.Lock] = {}
        self._removed: frozenset[str] | None = None
        self._removed_of: dict[str, frozenset[str]] = {}
        self._removed_lock = threading.Lock()
        if self._pass is not None:
            self._epoch = self._pass.epoch
            self.link_rows = self._pass.link_rows
            self._removed_of = self._pass.removed_of
            self._removed_lock = self._pass.removed_lock
            self._removed = self._pass.removed_all

    def _live_pass(self) -> PassCache | None:
        """The pass's cache while it holds the reads this store's group bound to
        (``PassCache.clear`` moves its epoch); else None (read and keep its own)."""
        live = self._pass
        return live if live is not None and live.epoch == self._epoch else None

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
        shared: dict[str, tuple[Any, str]] | None = None
        if key.startswith(TEST_POPULATION):
            with self._removed_lock:   # the epoch and its dict together (``PassCache.clear``)
                live = self._live_pass()
                shared = None if live is None else live.populations

        def read() -> Any:
            if shared is not None:
                with self.link_rows.lock:
                    held = shared.get(key)
                if held is not None:   # an earlier group of the pass read it
                    with self.link_rows.lock:
                        self.populations[key] = held[1]
                    return held[0]
            with self.whole():
                made = make()
            text = json.dumps(_canonical(digest(made)), sort_keys=True)
            with self.link_rows.lock:
                self.populations[key] = hashlib.sha256(text.encode()).hexdigest()[:32]
                if shared is not None:
                    shared.setdefault(key, (made, self.populations[key]))
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
            conn = _connect(self.path, self.file_only)
            if self.interrupt is not None:
                conn.set_progress_handler(self.interrupt, 10000)
            with self._conns_lock:
                self._conns.append(conn)
                self._conn_thread[id(conn)] = threading.current_thread()
            self._local.conn = holder = _ThreadConn(conn, self._release)
            return holder.conn
        return conn.conn

    def _part_conn(self) -> Any:
        """An idle connection of the ranged scans' parts, else a new one (opened once per
        part thread at most and kept for the run, closed with the store; each is one of
        the store's connections, so ``interrupt_all`` reaches it)."""
        with self._conns_lock:
            if self._parts:
                return self._parts.pop()
        conn = _connect(self.path, self.file_only)
        with self._conns_lock:
            self._conns.append(conn)
        return conn

    def _part_done(self, conn: Any, ok: bool) -> None:
        """A part's connection back to the idle ones; after a failed read (an interrupt,
        a lost server, a replaced generation) it is closed instead."""
        with contextlib.suppress(Exception):
            conn.set_progress_handler(None, 0)
        with self._conns_lock:
            if ok and conn in self._conns:
                self._parts.append(conn)
                return
            self._conns = [c for c in self._conns if c is not conn]
        with contextlib.suppress(Exception):
            conn.close()

    def _release(self, conn: Any) -> None:
        """A thread ended: its connection is closed and no longer kept (a Postgres server
        has a fixed number of connections; threads of the lanes and pools come and go)."""
        with self._conns_lock:
            self._conns = [c for c in self._conns if c is not conn]
            self._conn_thread.pop(id(conn), None)
        with contextlib.suppress(Exception):
            conn.close()

    def watch(self, interrupt: Callable[[], int] | None,
              pin: Callable[[], Callable[[], int]] | None = None,
              left: Callable[[], float] | None = None) -> None:
        """Every connection's progress handler (None: none); ``pin()``: the handler for
        another thread reading for this one (``Budget.pinned``); ``left()``: the seconds
        left of the asking thread's detector window (a read that can stop early bounds
        itself by it; Postgres has no progress handler to cut a read short)."""
        self.interrupt = interrupt
        self.left = left
        self.pin = pin if interrupt is not None else None
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

    def interrupt_all(self) -> None:
        """Abort whatever statement each of this store's connections is running."""
        with self._conns_lock:
            conns = list(self._conns)
        for conn in conns:
            with contextlib.suppress(Exception):
                conn.interrupt()

    def interrupt_thread(self, thread: threading.Thread) -> None:
        """Abort the statement ``thread``'s own connection is running (a run-thread rule
        past its window on Postgres, which has no progress handler)."""
        with self._conns_lock:
            conns = [c for c in self._conns if self._conn_thread.get(id(c)) is thread]
        for conn in conns:
            with contextlib.suppress(Exception):
                conn.interrupt()

    def spawn(self, name: str, work: Callable[[], None]) -> None:
        """Run ``work`` on a thread of its own that reads this store (a read a rule starts
        early and collects later); ``close`` stops and waits for it before it closes the
        connections it reads on (a connection closed under a running statement can crash
        the process)."""
        thread = threading.Thread(target=work, name=name, daemon=True)
        with self._conns_lock:
            self._helpers.append(thread)
        thread.start()

    def close(self) -> None:
        with self._conns_lock:
            helpers, self._helpers = self._helpers, []
        if any(t.is_alive() for t in helpers):
            self.interrupt_all()
            for thread in helpers:
                thread.join(HELPER_JOIN_SECONDS)
        running = {t for t in helpers if t.is_alive()}
        here = threading.current_thread()
        with self._conns_lock:
            conns, self._conns = self._conns, []
            self._parts = []
            owners = {id(c): self._conn_thread.pop(id(c), None) for c in conns}
        for conn in conns:
            owner = owners.get(id(conn))
            if owner in running:
                # a helper still reading past its join: its connection is never closed
                # under the running statement; it closes when that thread ends (_release)
                print(f"proactive_engine: helper {owner.name} still running at "
                      "close; its connection is left to close with it", file=sys.stderr)
                continue
            if owner is not None and owner is not here and owner.is_alive():
                # another live thread's own connection (a detector thread given up on at
                # its time budget may still be in a statement): closing it under that
                # statement frees what SQLite is reading (SIGSEGV, 11-14); it closes when
                # its thread ends (``_ThreadConn``'s release)
                continue
            conn.close()

    def data_stamp(self) -> str | None:
        """The data this store reads now, as one text that moves with every change (a
        pass's held reads are kept only while it stands, ``PassCache.current``): on
        Postgres its change stamp (``ontology_reader.change_stamp``: every load and delta
        layer moves it); on a file its content digest and the file's and its write-ahead
        log's size and modification time (any commit moves them). None when it cannot
        be told (the held reads are then let go)."""
        if self.pg:
            from operating.pg import ontology_reader
            return ontology_reader.change_stamp()
        parts = [str(self.meta("content_digest"))]
        for path in (Path(self.path), Path(f"{self.path}-wal")):
            try:
                st = path.stat()
            except FileNotFoundError:
                parts.append("-")
                continue
            except OSError:
                return None
            parts.append(f"{st.st_size}:{st.st_mtime_ns}")
        return "|".join(parts)

    def meta(self, key: str) -> str | None:
        if key == "content_digest" and not isinstance(self.conn, sqlite3.Connection):
            # Postgres: the data it holds now (``ontology_reader.change_stamp``), so a run
            # over data a delta layer or migration changed since is told apart
            from operating.pg import ontology_reader
            stamp = ontology_reader.change_stamp()
            if stamp is not None:
                return stamp
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
        from operating.pg import own_issued
        if own_issued.TYPES.keys() & set(types):
            # PLX's own sales documents held as supplier documents: removed (``removed``)
            own = own_issued.hidden_ids()
            ids = [i for i in ids if i not in own] if own else ids
        chunk_params: dict[str, Any] = {}
        size = VALUES_CHUNK
        if getattr(self, "pg", False):
            # the view's columns from its tables, and the properties' fields named first:
            # through the ``object_values`` view the planner looked the fields up again for
            # every value source of every record (1.3 s per 500 records)
            fields = ""
            if props is not None:
                fields = " AND c.field IN (SELECT value FROM json_each(:fields))"
                chunk_params["fields"] = json.dumps(sorted(row[0] for row in self.conn.execute(
                    "SELECT field FROM value_fields WHERE property IN (SELECT value FROM "
                    "json_each(:props))", {"props": json.dumps(list(props))})))
            order = " ORDER BY o.object_id, f.property, v.l1_handle"
            if objects is None and not self.scoped and props is not None \
                    and len(ids) <= PG_TYPE_READ_MAX:
                # the whole type in one statement, the records the listing gave (same
                # order: the store's own object id order)
                return self._shape(self.conn.execute(
                    _PG_VALUES_SQL + "o.object_type IN (SELECT value FROM json_each(:types)) "
                    "AND NOT " + ostore.removed_at_sql("o.object_id") + fields + order,
                    {**chunk_params, "types": params["types"]}), ids)
            sql = (_PG_VALUES_SQL + "o.object_id IN (SELECT value FROM json_each(:chunk))"
                   + fields + order)
            size = PG_VALUES_CHUNK
        else:
            sql = ("SELECT o.object_id, o.object_type, v.property, v.value_type, v.value, "
                   "v.unit, v.quantity_role, v.l1_handle, v.markings, v.lineage FROM objects o "
                   "JOIN object_values v ON v.object_id = o.object_id "
                   "WHERE o.object_id IN (SELECT value FROM json_each(:chunk)) "
                   "AND v.superseded_at IS NULL")
            if props is not None:
                sql += " AND v.property IN (SELECT value FROM json_each(:props))"
                chunk_params["props"] = json.dumps(list(props))
            sql += " ORDER BY o.object_id, v.property, v.l1_handle"
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(ids), size):
            chunk_params["chunk"] = json.dumps(ids[start:start + size])
            self._shape(self.conn.execute(sql, chunk_params), None, out)
        return out

    @staticmethod
    def _shape(rows: Iterable[Any], listed: Sequence[str] | None,
               out: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
        """``values``' rows by object (``listed``: only the records the listing gave)."""
        out = {} if out is None else out
        keep = None if listed is None else frozenset(listed)
        for row in rows:
            if keep is not None and row["object_id"] not in keep:
                continue
            entry = out.setdefault(row["object_id"], {"type": row["object_type"], "props": {}})
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
            if cache is self.link_rows:
                self._asked.update(types)
                for name in types:   # most recently asked last
                    cache.asked.pop(name, None)
                    cache.asked[name] = None
            missing = [t for t in types if t not in cache.by_type]
            found = {t: cache.by_type[t] for t in types if t in cache.by_type}
            if missing:
                read = self._scan_links(missing)
                for name in missing:
                    rows = read.get(name, [])
                    found[name] = rows
                    if cache is self.link_rows and self._pass is not None:
                        self._make_room(cache, len(rows))
                    if cache.kept + len(rows) <= LINK_CACHE_ROWS:
                        cache.by_type[name] = rows
                        cache.kept += len(rows)
        if len(types) == 1:
            return found[types[0]]
        return sorted((row for name in types for row in found[name]), key=lambda r: r[0])

    def _make_room(self, cache: _LinkRows, rows: int) -> None:
        """A pass's shared cache (``PassCache``) over its bound with ``rows`` more: the
        types this store never asked for (an earlier group's), least recently asked
        first, are let go until they fit, so a group keeps at least what it kept on a
        store of its own; a type let go is read again if a later group asks for it."""
        for name in list(cache.asked):
            if cache.kept + rows <= LINK_CACHE_ROWS:
                return
            if name in self._asked or name not in cache.by_type:
                continue
            cache.kept -= len(cache.by_type.pop(name))
            del cache.asked[name]

    def _scan_links(self, types: Sequence[str]) -> dict[str, list[tuple]]:
        """type -> its rows (``_LinkRows``), in rowid order (``_ranged``)."""
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
        # a whole scan: the records removed now are read once per end type and each row's
        # ends looked up in them (``_gone``), never a removal probe per end of every link
        # row (two index probes per row, a correlated subquery per row on Postgres)
        self._prefetch_gone(types)
        gone = self._gone
        plain = ("SELECT l.rowid, l.link_id, l.link_type, l.from_object, l.to_object, l.tier, "
                 "l.markings FROM link_rows l "
                 "WHERE l.link_type IN (SELECT value FROM json_each(:types)) "
                 "AND l.superseded_at IS NULL AND l.tier <> 'contradicted'")

        def shape(r: Sequence[Any]) -> tuple:
            return _link_row((*r[:7], gone(r[3]) or gone(r[4])))
        for rows in self._ranged("link_rows", "l", plain, " ORDER BY l.rowid",
                                 {"types": json.dumps(list(types))}, shape):
            # the ranges in rowid order, each in rowid order
            for row in self._settle_gone(rows):
                out[row[2]].append(row)
        return out

    def removed(self, types: Iterable[str] | None = None) -> frozenset[str]:
        """The records of ``types`` not held by its source now (``ostore.removed_at_sql``
        at the store's latest knowledge), one indexed read per type, each read once per
        run. None: every record in the store (one scan of every record: 55-77 s on the
        served store, 2026-11-12), only for a read whose record types are unknown."""
        # its own lock: a link scan asks for it while holding the link cache's lock,
        # which ``memo`` takes too
        # PLX's own sales documents the served store holds as supplier documents
        # (``operating.pg.own_issued``) count as removed: no rule reads them as a supplier's
        from operating.pg import own_issued
        own = own_issued.hidden_ids()
        with self._removed_lock:
            if types is None:
                live = self._live_pass()
                if self._removed is None and live is not None:
                    self._removed = live.removed_all   # an earlier group's read
                if self._removed is None:
                    self._removed = frozenset(
                        r[0] for r in self.conn.execute(ostore.removed_ids_sql())) | own
                    if live is not None:
                        live.removed_all = self._removed
                return self._removed
            out: set[str] = set()
            for object_type in sorted(set(types)):
                if object_type not in self._removed_of:
                    self._removed_of[object_type] = frozenset(
                        r[0] for r in self.conn.execute(
                            ostore.removed_ids_sql(object_type_sql="?"),
                            (object_type, object_type))) | (
                        frozenset(o for o in own if o.startswith(object_type + ":"))
                        if object_type in own_issued.TYPES else frozenset())
                out |= self._removed_of[object_type]
            return frozenset(out)

    def _gone(self, object_id: str) -> bool:
        """Whether a record is not held by its source now, among the types ``removed``
        read so far (a record id opens with its type: ``project.object_id_of``); a type not
        read yet: False here, settled by ``_settle_gone`` after the read (the link scan's
        threads never read the store for it)."""
        known = self._removed_of.get(object_id.split(":", 1)[0])
        return known is not None and object_id in known

    def _prefetch_gone(self, link_types: Iterable[str]) -> None:
        """``removed`` of the declared end types of ``link_types`` (``_link_ends``), read
        before a link scan on this thread."""
        self.removed({e for ends in _link_ends(list(link_types)).values() for e in ends})

    def _settle_gone(self, rows: list[tuple]) -> list[tuple]:
        """``rows`` with ``gone`` read again for each row with an end whose type was not
        read before the scan (an end type the schema does not declare): that type read
        now, on this thread."""
        def kind(oid: str) -> str:
            return oid.split(":", 1)[0]
        missing = {kind(o) for r in rows for o in (r[3], r[4])} - set(self._removed_of)
        if not missing:
            return rows
        self.removed(missing)
        return [(*r[:7], self._gone(r[3]) or self._gone(r[4]))
                if kind(r[3]) in missing or kind(r[4]) in missing else r for r in rows]

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

    def of_type(self, link_type: str) -> list[tuple]:
        """Every current link of one type, contradicted ones too (``_LinkRows`` rows, in
        rowid order), read once per run by the link table's type index; each row's ends
        looked up in the records removed now (``_gone``), never a removal probe per row.
        A type of tens of thousands of rows reads in seconds this way, where a read by the
        end indexes at every record of its end type took 40 s a type (2026-11-12 live
        store: ``Customer.same_record_as`` 30k rows 1.5 s by type, 40 s by end)."""
        def read() -> list[tuple]:
            self._prefetch_gone([link_type])
            gone = self._gone
            sql = ("SELECT l.rowid, l.link_id, l.link_type, l.from_object, l.to_object, "
                   "l.tier, l.markings FROM link_rows l WHERE l.link_type = ? "
                   "AND l.superseded_at IS NULL")
            if self.scoped:
                # an incremental pass: its scope's ends only, as ``touching`` reads them
                return self.touching(self._scope_ids, [link_type], every=True)
            return sorted(self._settle_gone(
                [_link_row((*r[:7], gone(r[3]) or gone(r[4])))
                 for r in self.conn.execute(sql, (link_type,))]), key=lambda r: r[0])
        return self.memo("of_type:" + link_type, read)

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
            # the asking thread's window, not the part's own thread's
            handler = self.pin() if self.pin is not None else self.interrupt

            def part(lo: int) -> list[Any]:
                conn, ok = self._part_conn(), False
                try:
                    conn.set_progress_handler(handler, 10000) if handler is not None \
                        else conn.set_progress_handler(None, 0)
                    # a generation replaced under the run (Postgres is never replaced: a
                    # delta layer changes it in place, as the run's own reads see)
                    held = conn.execute("SELECT value FROM store_meta WHERE key = "
                                        "'content_digest'").fetchone()
                    if isinstance(conn, sqlite3.Connection) and \
                            (held[0] if held else None) != self.digest:
                        raise ostore.StoreError("store_replaced")
                    got = [shape(r) for r in conn.execute(ranged, {**params, "lo": lo,
                                                                   "hi": lo + step})]
                    ok = True
                    return got
                finally:
                    self._part_done(conn, ok)
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

        return set(self.memo("audience_keys", lambda: frozenset(self.audience_firsts())))

    def audience_firsts(self) -> dict[str, str]:
        """audience key -> the smallest object id in it, over every record (removed ones
        too), in one scan per run by object rowid ranges side by side (``_ranged``): the
        audience keys (``audience_keys``) and the data-quality rule's one record per
        audience (``proactive_quality.stale_captures``) read it, never a scan each."""
        def read() -> dict[str, str]:
            firsts: dict[str, str] = {}
            for rows in self._ranged(
                    "objects", "o",
                    "SELECT o.audience_key, MIN(o.object_id) FROM objects o WHERE 1 = 1",
                    " GROUP BY o.audience_key", {}, tuple):
                for key, oid in rows:   # each range's smallest, the smallest of them kept
                    if key not in firsts or oid < firsts[key]:
                        firsts[key] = oid
            return firsts
        return dict(self.memo("audience_firsts", read))

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
        so_status.LINE_OF, *WORK_LINE_LINKS, *WORK_ALLOCATION,
        *so_status.FULFILLED_LINKS, *SHIP_LINKS, *ev.LINK_TYPES,
        *ic.LINK_TYPES, ic.LINE_SELLS, *ic.LEAD_ROUTE_TYPES, *amounts,
        *_rule_link_types()]))


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


def _late_list_only(st: _Store, results: dict[str, dict[str, Any]], as_of: date) -> None:
    """One late rule for Late, Home and the briefing: a customer order is at risk from a
    purchase order that is not received only while that order is on the late list
    (``LATE_RULE``'s gaps that state days late, Home's ``late_total``), across its copies
    (the rule's ``po_orders`` join). ``plx.process`` may judge a PO late that the list does
    not hold (a line's bulk import date, or no requested date at all) and then dates the
    material by the day measured: such an impact is dropped, counted
    ``po_not_on_late_list``. A material date that is a cited fact (a receipt, a supplier's
    promise for the line) stands on its own citation. When the late rule did not run this
    run it is read once; when it failed nothing is on the list."""
    found = results.get("orders_at_risk")
    if not found or not found.get("gaps"):
        return
    def event_of(gap: Mapping[str, Any]) -> Mapping[str, Any]:
        return (gap.get("follow_up") or {}).get("event") or {}

    late = results.get(LATE_RULE)
    if late is None:
        try:
            late = _late_or_unconfirmed_po(st, as_of)
        except Exception:  # noqa: BLE001 - a late check that cannot run drops nothing
            late = None
    if late is None or late.get("status") in ("failed", "truncated"):
        # the late list is unknown or incomplete: every impact stays, and the ones resting
        # on the day measured say their late status was not checked
        for gap in found["gaps"]:
            event = event_of(gap)
            if isinstance(event, dict) and (event.get("material_pending")
                                            or gap.get("kind") != "order_at_risk"):
                event["late_status_not_checked"] = True
        return
    joined = late.get("po_orders") or {}
    listed = {((gap.get("follow_up") or {}).get(_ORDER) or {}).get("key") or gap["object_id"]
              for gap in late.get("gaps") or ()
              if (gap.get("follow_up") or {}).get("days_late") is not None}

    def on_list(po: Any) -> bool:
        return isinstance(po, str) and (joined.get(po) or (po,))[0] in listed

    # a PO with an order whose material date is a cited fact, not the day measured
    dated = {p for gap in found["gaps"] if gap.get("kind") == "order_at_risk"
             and not event_of(gap).get("material_pending") for p in event_of(gap).get("pos") or ()}

    def stands(gap: Mapping[str, Any]) -> bool:
        if gap.get("kind") == "order_at_risk":
            pos = event_of(gap).get("pos") or ()
            return bool(pos) and (not event_of(gap).get("material_pending")
                                  or all(on_list(p) for p in pos))
        po = gap.get("object_id")
        return on_list(po) or po in dated

    kept, dropped = [], 0
    for gap in found["gaps"]:
        if gap.get("kind") not in IMPACT_KINDS or stands(gap):
            kept.append(gap)
        else:
            dropped += 1
    if dropped:
        found["gaps"] = kept
        skipped = found.setdefault("not_evaluated", {})
        if isinstance(skipped, dict):
            skipped["po_not_on_late_list"] = skipped.get("po_not_on_late_list", 0) + dropped
        finding = found.get("finding")
        if isinstance(finding, dict):
            risky = sum(1 for g in kept if g.get("kind") == "order_at_risk")
            pos = len({g["object_id"] for g in kept if g.get("kind") == "po_impact"})
            from . import impact_chain as ic
            finding.update({"orders_at_risk": risky, "late_pos": pos,
                            "text": ic.headline(risky, pos)})


def _business_steps(st: _Store, as_of: date,
                    on_hand: Sequence[tuple[str, str, str | None]] | None,
                    people: Callable[[], Any] = lambda: None,
                    moment: datetime | None = None, sheets: Path | None = None
                    ) -> dict[str, Callable[[], dict[str, Any]]]:
    """The business alerts (``operating.proactive_business``) as engine steps; ``on_hand``:
    where on hand is read (None: what the schema declares, ``declared_on_hand``);
    ``sheets``: the sheet readings file (``proactive_sheets.readings_path``)."""
    from . import proactive_business as pb
    from . import proactive_ceo as ceo
    from . import proactive_chasers as chasers
    from . import proactive_mail_pains as mail_pains
    from . import proactive_cash as cash
    from . import proactive_stock as stock
    from . import proactive_prices as prices
    from . import proactive_sales as sales
    from . import business_documents as docs
    from . import proactive_quality as quality
    from . import proactive_sync as sync
    from . import proactive_commitments as commit
    from . import proactive_integrity as integrity
    from . import proactive_sheets as sheet_rules

    common = {"gap": _gap, "cites": _cites, "result": _result, "settled": SETTLED,
              "skips": _Skips}
    return {
        # with sheet readings served, stock reads mail: people first, fail closed
        "stock_out_risk": lambda: (sheets is not None and sheet_rules.people_blocked(
            people(), _result))
        or pb.stock_out_risk(
            st, as_of, orders=lambda: _customer_orders(st), po_source=_po_source(st),
            absence=lambda types: _absence_audiences(st, types, "stock:none_stored"),
            on_hand=on_hand, sheet_on_hand=None if sheets is None else (
                lambda item_of, wanted, cites, skipped: sheet_rules.sheet_on_hand(
                    st, as_of, sheets, people(), item_of, wanted, cites, skipped)),
            **common),
        "overdue_receivable": lambda: pb.overdue_receivable(
            st, as_of, chases=lambda invoices: mail_pains.receivable_chases(
                st, people(), as_of, invoices, {}), **common),
        "unit_cost_jump": lambda: pb.unit_cost_jump(st, as_of, po_source=_po_source(st),
                                                    **common),
        # the chief-executive signals (``operating.proactive_ceo``)
        "customer_demand_drop": lambda: ceo.customer_demand_drop(
            st, as_of, orders=lambda: _customer_orders(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common),
        "promise_date_slip": lambda: ceo.promise_date_slip(
            st, as_of, orders=lambda: _customer_orders(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common),
        "supplier_reliability": lambda: ceo.supplier_reliability(
            st, as_of, po_source=_po_source(st),
            absence=lambda types: _absence_audiences(st, types, "receipt:none_stored"),
            **common),
        "shipped_not_invoiced": lambda: pb.shipped_not_invoiced(
            st, as_of, absence=lambda types: _absence_audiences(st, types,
                                                                "invoice:none_stored"),
            **common),
        "duplicate_customer_order": lambda: pb.duplicate_customer_order(st, as_of,
                                                                    **common),
        "recurring_component_damage": lambda: pb.recurring_component_damage(
            st, as_of, **common),
        # the customer chase and quality-hold alerts (``operating.proactive_chasers``)
        "customer_chase": lambda: chasers.customer_chase(
            st, as_of, people=people(), orders=lambda: _customer_orders(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common),
        "quality_holds": lambda: chasers.quality_holds(st, as_of, **common),
        # pains found in the mail (``operating.proactive_mail_pains``)
        "expedite_requests": lambda: mail_pains.expedite_requests(
            st, as_of, people=people(), orders=lambda: _customer_orders(st),
            po_source=lambda: _po_source(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common),
        "supplier_shortage": lambda: mail_pains.supplier_shortage(
            st, as_of, people=people(), orders=lambda: _customer_orders(st),
            po_source=lambda: _po_source(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common),
        "payment_chase": lambda: mail_pains.payment_chase(st, as_of, people=people(),
                                                          **common),
        "return_requests": lambda: mail_pains.return_requests(
            st, as_of, people=people(), orders=lambda: _customer_orders(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common),
        # receivables and payables (``operating.proactive_cash``)
        "customer_payment_behaviour": lambda: cash.customer_payment_behaviour(
            st, as_of, **common),
        "early_pay_discounts": lambda: cash.early_pay_discounts(st, as_of, **common),
        "duplicate_payables": lambda: cash.duplicate_payables(st, as_of, **common),
        "returns_and_credits": lambda: cash.returns_and_credits(st, as_of, **common),
        # operations (``operating.proactive_stock``)
        "slow_moving_stock": lambda: stock.slow_moving_stock(
            st, as_of, absence=lambda types: _absence_audiences(st, types, "stock:none_stored"),
            on_hand=on_hand, **common),
        "expiring_lot_stock": lambda: (sheets is not None and sheet_rules.people_blocked(
            people(), _result))
        or stock.expiring_lot_stock(
            st, as_of, orders=lambda: _customer_orders(st), on_hand=on_hand,
            sheet_lots=None if sheets is None else (
                lambda item_of, horizon, exclude, cites, skipped: sheet_rules.sheet_lots(
                    st, as_of, sheets, people(), item_of, horizon, exclude, cites,
                    skipped)),
            **common),
        "assembly_component_shortfall": lambda: stock.assembly_component_shortfall(
            st, as_of, po_source=lambda: _po_source(st),
            absence=lambda types: _absence_audiences(st, types, "stock:none_stored"),
            on_hand=on_hand, **common),
        "certificate_expiry": lambda: stock.certificate_expiry(st, as_of, **common),
        "freight_cost_anomaly": lambda: stock.freight_cost_anomaly(st, as_of, **common),
        # what each customer pays for the same product (``operating.proactive_prices``)
        "price_consistency": lambda: prices.price_consistency(st, as_of, **common),
        # sales (``operating.proactive_sales``)
        "stale_quote": lambda: sales.stale_quote(st, as_of, **common),
        # priority / fill lists (``operating.business_documents``)
        "priority_fill_unmet": lambda: docs.priority_fill_unmet(st, as_of, **common),
        # data quality (``operating.proactive_quality``)
        "duplicate_parties": lambda: quality.duplicate_parties(st, as_of, **common),
        "key_fields_missing": lambda: quality.key_fields_missing(
            st, as_of, absence=lambda types: _absence_audiences(st, types,
                                                                "record:none_stored"),
            status_of={"PurchaseOrder": lambda: _po_classes(st, as_of),
                       "SalesOrder": lambda: st.memo("quality:so_open", lambda: {
                           m: v.cls for v in _customer_orders(st)[1] for m in v.group})},
            **common),
        "twin_disagreement": lambda: quality.twin_disagreement(st, as_of, **common),
        "stale_captures": lambda: quality.stale_captures(st, as_of, now=moment, **common),
        "sync_gaps": lambda: sync.sync_gaps(
            st, as_of, inactive=lambda: inactive_records(st, as_of),
            tests=lambda: test_records(st, as_of), **common),
        "reorder_soon": lambda: (sheets is not None and sheet_rules.people_blocked(
            people(), _result))
        or stock.reorder_soon(
            st, as_of, po_source=lambda: _po_source(st),
            absence=lambda types: _absence_audiences(st, types, "stock:none_stored"),
            on_hand=on_hand, sheet_on_hand=None if sheets is None else (
                lambda item_of, wanted, cites, skipped: sheet_rules.sheet_on_hand(
                    st, as_of, sheets, people(), item_of, wanted, cites, skipped)),
            **common),
        # integrity (``operating.proactive_integrity``)
        "inventory_adjustment": lambda: integrity.inventory_adjustment(st, as_of, **common),
        "blocked_account_activity": lambda: integrity.blocked_account_activity(
            st, as_of, order_state=_order_state(st, as_of),
            absence=lambda types: _absence_audiences(st, types, "order:none_stored"),
            **common),
        "gl_posting_errors": lambda: integrity.gl_posting_errors(st, as_of, **common),
        "recurring_task_missed": lambda: integrity.recurring_task_missed(
            st, as_of, people=people(), **common),
        # sheet readings (``operating.proactive_sheets``)
        "cash_application": lambda: sheet_rules.cash_application(
            st, as_of, people=people(), readings=sheets or Path("/nonexistent"), **common),
        "statement_reconciliation": lambda: sheet_rules.statement_reconciliation(
            st, as_of, people=people(), readings=sheets or Path("/nonexistent"), **common),
        # promises we made and mail waiting on a reply (``operating.proactive_commitments``)
        **{name: (lambda rule=rule: rule(
            st, as_of, people=people(), orders=lambda: _customer_orders(st),
            absence=lambda types: _absence_audiences(st, types, "sales_order:none_stored"),
            **common))
           for name, rule in (("commitments", commit.commitments),
                              ("needs_reply", commit.needs_reply))}}


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
            return st.memo("recent_margins", margins.collect)
        return pmap(task, items)

    def roots() -> list[str]:
        return st.memo("margin_roots", lambda: ev._recent_invoiced_orders(st, as_of))

    def order_margins(items: Sequence[str]) -> list[Any]:
        """Each order's checked margin (``read_margin``), the recent ones read once for
        the negative-margin rule and the erosion rule alike; without a pool one by one
        (an order whose run fails is undecided, never a figure)."""
        items = list(items)
        if margins is not None:
            return margin_map(ev.read_margin, items)
        if pmap is not None:
            if items == roots():
                return st.memo("recent_margins", lambda: pmap(ev.read_margin, items))
            return pmap(ev.read_margin, items)
        from .proactive_pool import NOT_READ

        out = []
        for n, root in enumerate(items):
            if n >= ev.CALL_BOUND:   # without a pool as bounded as the negative-margin rule
                out.append(NOT_READ)
                continue
            try:
                out.append(one_margin(root))
            except Exception as error:  # noqa: BLE001 - one order fails alone
                if type(error).__name__ == "TimeBudgetSpent":
                    raise
                out.append(None)
        return out

    def one_margin(root: str) -> Any:
        """One order's margin read once per run without a pool (both margin rules)."""
        return st.memo(f"margin:{root}", lambda: ev.read_margin(call, root))

    def erosion() -> dict[str, Any]:
        from . import customer_profit as cp
        from . import proactive_ceo as ceo
        from .proactive_pool import NOT_READ

        recent = roots() if call is not None else []
        trailing = st.memo("margin_trailing", lambda: ev._recent_invoiced_orders(
            st, as_of, days=ev.MARGIN_DAYS + ceo.MARGIN_TRAILING_DAYS,
            since=ev.MARGIN_DAYS)) if call is not None else []
        reader = _margin_reader(call)
        kept: dict[str, Any] = {}
        margins_of = order_margins
        if reader is not None and (recent or trailing):
            # the cross-run reading cache: an order whose fingerprint (every row its margin
            # run can read, ``ev.margin_fingerprints``) is unchanged keeps its reading; the
            # recent orders are read as the negative-margin rule reads them (shared)
            everything = list(dict.fromkeys([*recent, *trailing]))
            fingerprints = ev.margin_fingerprints(
                st, everything, ev.margin_context(st, reader, st.path.parent.parent))
            prior = (cache or {}).get("margin_erosion") or {}

            def margins_of(items: Sequence[str]) -> list[Any]:
                items = list(items)
                hits: dict[str, Any] = {}
                if items != list(recent):
                    for root in items:
                        held = prior.get(root)
                        if isinstance(held, Mapping) and held.get("fp") == fingerprints[root]:
                            try:
                                hits[root] = ev.margin_from_reading(held["order"])
                            except (KeyError, TypeError, ValueError, ArithmeticError):
                                pass   # a reading of another shape is read again
                misses = [root for root in items if root not in hits]
                read = dict(zip(misses, order_margins(misses) if misses else []))
                out = [hits[root] if root in hits else read[root] for root in items]
                for root, order in zip(items, out):
                    if _margin_keeps(order, cp):
                        kept[root] = {"fp": fingerprints[root],
                                      "order": ev.margin_reading(order)}
                st.memo("margin_cache_hits", dict).update(dict.fromkeys(hits, True))
                return out
        try:
            found = ceo.margin_erosion(
                st, as_of, call, recent=recent, trailing=trailing,
                margins=margins_of, not_read=NOT_READ, settled=SETTLED, skips=_Skips,
                **common)
        except TimeBudgetSpent:
            if not kept:
                raise
            # cut at its window's end after reading: the readings stay for the next run
            # (a pass spans runs until every order is held), nothing judged on a part
            found = _result([], status="truncated", reasons=[TIME_BUDGET])
        if reader is not None and (recent or trailing):
            found["cache"], found["roots"] = kept, list(dict.fromkeys([*recent, *trailing]))
            found.setdefault("counts", {})["margins_reused"] = len(
                st.memo("margin_cache_hits", dict))
        return found

    def no_reply() -> dict[str, Any]:
        found = orders()
        if found is None:
            return _result([], status="blocked", reasons=["order_status_not_read"])
        src = _po_source(st)
        exclusion = people()
        return ev.vendor_no_reply(st, as_of, people=exclusion, orders=found,
                                  order_key=_ORDER, colliding=src.colliding.__contains__,
                                  resolver=_mention_resolver(st, exclusion), **common)
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
            st, as_of, call, pmap=None if pmap is None else (
                lambda task, items: order_margins(items) if task is ev.read_margin
                else (margin_map if margins is not None else pmap)(task, items)),
            roots=roots(), read=one_margin, **common),
        "margin_erosion": erosion}


def _host_available(meminfo: str = HOST_MEMINFO) -> int | None:
    """The host's MemAvailable in bytes; None when it cannot be read."""
    try:
        with open(meminfo, encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    return int(parts[1]) * (1024 if parts[2:3] == ["kB"] else 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


def _host_room(directory: str | None, meminfo: str = HOST_MEMINFO) -> int | None:
    """The memory this process's cgroup may grow to: the lower of its own limit and its
    use now plus what the host still has available (the host's OOM killer, not the
    unit's limit, ends a pass on a loaded host). A host figure that cannot be read leaves
    the cgroup's limit; a use that cannot be read with a readable host figure: the host
    figure alone (never no limit)."""
    limit = _cgroup_limit(directory)
    available = _host_available(meminfo)
    if available is None:
        return limit
    room = (_cgroup_used(directory) or 0) + available
    return room if limit is None else min(limit, room)


class _MemoryGate:
    """Admission of detectors by memory (``MEMORY_GATE_SHARE``): ``enter`` waits while
    another detector runs and ``used()`` plus ``reserve`` for each running or cut-off
    (``abandon``) detector is at or over ``share`` of ``limit()``; ``leave`` when it ends.
    ``held`` counts the detectors that waited, ``most`` the most that ran at once."""

    def __init__(self, used: Callable[[], int | None], limit: Callable[[], int | None],
                 share: float = MEMORY_GATE_SHARE, reserve: int = DETECTOR_RESERVE_BYTES,
                 poll: float = MEMORY_GATE_POLL_SECONDS) -> None:
        if not 0 < share <= 1 or reserve < 0:
            raise ValueError("memory_gate_invalid")
        self.used, self.limit, self.share, self.reserve = used, limit, share, reserve
        self.poll = poll
        self.running = self.most = self.held = self.orphans = 0
        self._cond = threading.Condition()

    @classmethod
    def for_process(cls, environ: Mapping[str, str] | None = None) -> "_MemoryGate":
        env = os.environ if environ is None else environ
        share = MEMORY_GATE_SHARE
        raw = (env.get(MEMORY_SHARE_ENV) or "").strip()
        if raw:
            try:
                share = float(raw)
            except ValueError:
                share = MEMORY_GATE_SHARE   # unreadable: the default, never no gate
            if not 0 < share <= 1:
                share = MEMORY_GATE_SHARE
        directory = _own_cgroup()
        return cls(lambda: _cgroup_used(directory), lambda: _host_room(directory, HOST_MEMINFO),
                   share)

    def over(self) -> bool:
        limit = self.limit()
        if limit is None:
            return False
        used = self.used()
        if used is None:
            return True   # a limit whose use cannot be read: over (one at a time)
        return used + (self.running + self.orphans) * self.reserve >= self.share * limit

    def enter(self, stop: Callable[[], bool] = lambda: False) -> None:
        with self._cond:
            waited = False
            while self.running > 0 and not stop() and self.over():
                waited = True
                self._cond.wait(self.poll)
            self.held += waited
            self.running += 1
            self.most = max(self.most, self.running)

    def leave(self) -> None:
        with self._cond:
            self.running -= 1
            self._cond.notify_all()

    def abandon(self, ended: threading.Event) -> None:
        """A detector cut off at its window whose thread still runs: its memory is counted
        (a reserve) until ``ended`` is set, without holding up the others' turns."""
        with self._cond:
            self.orphans += 1

        def wait() -> None:
            ended.wait()
            with self._cond:
                self.orphans -= 1
                self._cond.notify_all()
        threading.Thread(target=wait, name="proactive-gate-orphan", daemon=True).start()


def run(store_path: Any, inbox: Any, as_of: date | None = None, *,
        locked: bool = False, **options: Any) -> dict[str, Any]:
    """Run the detectors and fold their gaps into the inbox (``_run``), holding the
    inbox's pass lock (``pass_lock``; ``locked``: the caller holds it): two runs never
    read and fold over each other, so a fold is never of older data than the last."""
    if locked:
        return _run(store_path, inbox, as_of, **options)
    with pass_lock(inbox) as _held:
        return _run(store_path, inbox, as_of, **options)


def _run(store_path: Any, inbox: Any, as_of: date | None = None, *,
         coverage: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
        call: Callable | None = None, card_bound: int = CARD_BOUND,
        qc_link_types: Sequence[str] | None = None, budget: Budget | None = None,
        on_hand_sources: Sequence[tuple[str, str, str | None]] | None = None,
        ops_sources: Mapping[str, Any] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        only: Sequence[str] | None = None, vendor_bound: int | None = None,
        reader_may_read: Callable[[str], bool] | None = None,
        pool: Any = None, people: Any = None, threads: int | None = 1,
        detector_seconds: Mapping[str, float] | None = None,
        scope: "Scope | None" = None, sheet_readings: Any = None,
        cadence: Mapping[str, float] | None = None,
        memory: "_MemoryGate | None" = None,
        finish_pass: bool = False,
        checkpoint: "tuple[str, Mapping[str, Any]] | None" = None,
        pass_cache: PassCache | None = None) -> dict[str, Any]:
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
    ``detector_threads()``), the tool-call rules on this thread. Every detector runs in its
    own window (``DETECTOR_SECONDS``, ``detector_seconds`` overriding some), within the
    run's deadline when there is a ``budget``: a store-only one still running
    ``HARD_GRACE_SECONDS`` past it is cut off (``truncated``, named in the summary's
    ``cut_off``) and its lane goes on; the summary's ``seconds`` holds each one's time.
    ``scope``: an incremental pass (``Scope``, ``operating.proactive_incremental``): its
    rules read only the scope's records and keep only the gaps about their own subjects;
    only those subjects' gaps are folded and resolved (``_fold``), nothing else of the
    inbox (the full run's records, counts and generation) changes. With a ``budget`` each
    detector is given its share of what is left at its turn (``_share``). ``cadence``:
    hours per rule (default ``DETECTOR_CADENCE_HOURS``; ``{}``: none) a rule waits after
    its last run started before it runs again (``_not_due``); one not due is left as it
    was and named in the summary's ``not_due``. ``finish_pass``: this ``only`` run is the
    last rule group of a whole pass run group by group (``refresh_pass``): it records the
    run and the scope as a whole run does. ``checkpoint`` (meta key, pass record): the
    pass record, with this run's rules added to its ``done``, is written in the fold's
    own transaction (gone once ``done`` holds its whole ``plan``), so a killed pass
    resumes after the last group folded. ``pass_cache``: what the groups of one pass share
    (``PassCache``: the link rows, the records removed, the test records), read by the
    first group that needs it, never again by a later one."""
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
    shared = pass_cache if scope is None else None
    st = _Store(Path(store_path), scope=None if scope is None else scope.read,
                audience_keys=None if scope is None else scope.audience_keys, shared=shared)
    # every detector runs in its own window, with or without a run budget: an unbounded
    # run is one whose deadline never comes (its tool calls are not guarded)
    window = budget if budget is not None else Budget(math.inf)
    gate = memory if memory is not None else _MemoryGate.for_process()
    if memory is None:
        gate.reserve = detector_reserve(store_path)
    st.watch(window.interrupt, window.pinned, window.left)
    st.cursor_path = Path(str(inbox) + ".commitments-recent.json")
    try:
        results: dict[str, dict[str, Any]] = {}
        seconds: dict[str, float] = {}
        unknown = set(only or ()) - set(DETECTORS)
        if unknown:
            raise ValueError("detector_unknown")
        order = list(DETECTORS) if only is None else [d for d in DETECTORS if d in only]
        hours = DETECTOR_CADENCE_HOURS if cadence is None else cadence
        not_due = [] if scope is not None else _not_due(
            Path(inbox), [d for d in order if d in hours], hours, moment)
        order = [d for d in order if d not in not_due]
        started_at: dict[str, str] = {}   # the cadenced rules this run started: when
        first = shared is not None and shared.prefetch and not shared.prefetched
        if first:
            shared.prefetched = True   # tried once a pass: a later group reads what it asks
        if (only is None or scope is not None or first) \
                and not (budget is not None and budget.spent()):
            # every link type the rules read, in one scan of the link table (``_Store``)
            started = time.monotonic()
            try:
                # with the run's own QC links and on-hand links, when it is handed them
                st.prefetch_links([*_run_link_types(), *(qc_link_types or ()),
                                   *(link for _type, _prop, link in on_hand_sources or ()
                                     if link)])
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
                st, coverage() if callable(coverage) else coverage, as_of),
            "expired_quote": lambda: _expired_quote(st, as_of),
            **_business_steps(st, as_of, on_hand_sources,
                              people=lambda: people if people is not None
                              else _people(Path(store_path)), moment=moment,
                              sheets=_sheets(store_path, sheet_readings)),
            **_ops_steps(st, as_of, lambda: people if people is not None
                         else st.memo("ops:people", lambda: _people(Path(store_path))),
                         ops_sources),
            "conflicting_quantity": lambda: _conflicting_quantity(st, as_of),
            "late_order": lambda: _late_order(st, as_of),
            "orders_at_risk": lambda: (
                _orders_at_risk(st, as_of, call, card_bound, first=_late_found(results),
                                many=many, cache=cache.get("orders_at_risk"))
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

        def body(name: str, seconds_: float) -> tuple[dict[str, Any], float]:
            """One detector in its own window (``seconds_``), on the thread it is handed
            to; never raising: (its result, its seconds)."""
            refused = window.refused_here()
            window.open(seconds_)
            started = time.monotonic()
            if name in hours:
                started_at[name] = clock().isoformat()
            st.judging(name)
            try:
                result = steps[name]()
            except TimeBudgetSpent:
                result = _result([], status="truncated", reasons=[TIME_BUDGET])
            except Exception as error:  # noqa: BLE001 - one detector fails alone, typed
                if window.left() <= 0 and isinstance(error, sqlite3.OperationalError):
                    # its query was interrupted at its window's end: cut short, not failed
                    result = _result([], status="truncated", reasons=[TIME_BUDGET])
                else:
                    result = _result([], status="failed",
                                     reasons=[type(error).__name__, *_failure_cause(error)])
                    # the cause by class only (never the message: it may quote values), so
                    # the next failed run says why in the journal (R8: three rules failed
                    # three full runs running with nothing but "OperationalError")
                    print(f"proactive_engine: {name} failed: "
                          f"{' '.join(result['reasons'])}", file=sys.stderr, flush=True)
            finally:
                st.judging(None)
                window.unshare()
            if window.refused_here() > refused and result["status"] != "failed":
                # a call it made was refused: what it found stands, never all it would find
                result["status"] = "truncated"
                if TIME_BUDGET not in result["reasons"]:
                    result["reasons"].append(TIME_BUDGET)
            return result, round(time.monotonic() - started, 1)

        def judge(name: str, runner: list, rest: Sequence[str] = ()) -> None:
            """A store-only ``name`` on its lane's runner thread (``runner[0]``), waited for no longer
            than its window (and the run's deadline) and ``HARD_GRACE_SECONDS``: one that
            does not end by then (a loop that never reads the store) is ``truncated``, its
            thread left to end alone (its result never read) and the lane goes on on a new
            thread, so no detector holds up the ones after it. ``rest``: the detectors
            this thread runs after it (its window is its share, ``_share``)."""
            if budget is not None and budget.spent():
                results[name] = _result([], status="truncated", reasons=[TIME_BUDGET])
                return
            # beside the running detectors only while the memory allows (``_MemoryGate``):
            # the wait is the run's time, not this detector's window
            gate.enter(lambda: (budget is not None and budget.spent())
                       or window.deadline - window.monotonic() <= 0)
            try:
                judged(name, runner, rest)
            finally:
                gate.leave()

        def judged(name: str, runner: list, rest: Sequence[str]) -> None:
            if budget is not None and budget.spent():
                results[name] = _result([], status="truncated", reasons=[TIME_BUDGET])
                return
            started = time.monotonic()
            limit = _share(name, rest, windows, window.deadline - window.monotonic(),
                           cadenced=name in hours)
            if name in ON_RUN_THREAD:
                # on the run's own thread (the caller's session and the runtime's mirror
                # may be bound to it): every tool call and pool map is refused past the
                # window (``Budget.guard``, ``Budget.pooled``; an investigation is handed
                # its time left) and every store read interrupted at it (on Postgres, by
                # cancelling the statement still running ``RUN_THREAD_GRACE_SECONDS``
                # after it)
                with _Watchdog(st, limit + RUN_THREAD_GRACE_SECONDS):
                    results[name], seconds[name] = body(name, limit)
                return
            done = runner[0].submit(body, name, limit)
            if done.wait(None if math.isinf(limit) else limit + HARD_GRACE_SECONDS):
                results[name], seconds[name] = done.value or (
                    _result([], status="failed", reasons=["detector_thread_ended"]),
                    round(time.monotonic() - started, 1))
            else:
                results[name] = _result([], status="truncated", reasons=[TIME_BUDGET])
                seconds[name] = round(time.monotonic() - started, 1)
                gate.abandon(done.event)   # its thread still holds what it read
                runner[0].stop()
                runner[0] = _Runner()
                abandoned.append(name)

        abandoned: list[str] = []
        runners: list[list] = []

        def runner_() -> list:
            made = [_Runner()]
            runners.append(made)
            return made

        lanes = _lanes(order, detector_threads() if threads is None else threads)
        try:
            if lanes:
                def lane(names: Sequence[str]) -> None:
                    mine = runner_()
                    for n, name in enumerate(names):
                        judge(name, mine, names[n + 1:])
                side = [d for names in lanes for d in names]
                width = min(len(lanes), detector_threads() if threads is None else threads)
                own = runner_()
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=width,
                                        thread_name_prefix="proactive-lane") as threads_:
                    running = {name: future for names in lanes
                               for future in [threads_.submit(lane, names)] for name in names}
                    mine_ = [name for name in order if name not in side]
                    for n, name in enumerate(mine_):
                        if name == "orders_at_risk" and LATE_RULE in running:
                            running[LATE_RULE].result()   # it asks the late POs first
                        judge(name, own, mine_[n + 1:])
                    for future in set(running.values()):
                        future.result()
            else:
                own = runner_()
                for n, name in enumerate(order):
                    judge(name, own, order[n + 1:])
        finally:
            if head is not None:
                head.stop()   # its rule did not run (the time ran out): no read goes on
            for made in runners:
                made[0].stop()
            if abandoned:
                # a cut-off detector's statement still running stops now, never reads on
                st.interrupt_all()
        if gate.held:
            # counts only: the journal says when memory, not the store, set the pace
            print(f"proactive_engine: {gate.held} detector(s) waited for memory "
                  f"(at most {gate.most} side by side)", file=sys.stderr, flush=True)
        if budget is not None:
            budget.release()   # the amounts run in the reserve, still inside the bound
        if scope is not None:
            # an incremental pass: a gap is this pass's only when its subject is one the
            # pass judged whole (the others' inputs were not all read)
            for name, result in results.items():
                mine = scope.subjects.get(name, frozenset())
                result["gaps"] = [g for g in result["gaps"] if g["object_id"] in mine]
        _late_list_only(st, results, as_of)
        _with_amounts(st, results)
        # one test-record filter for every detector (never a page's own): a claim about a
        # test, UAT, demo or proof record, a go-live first document or a carried-over old
        # empty draft is not a business claim
        try:
            _drop_test_records(st, results, as_of)
        except sqlite3.Error:
            pass   # the store could not be read for it: the gaps stand as found
        # one active-population filter for every record-level rule (R3): a claim about a
        # closed or historic order is not current work
        _drop_inactive_orders(st, results, as_of)
        for result in results.values():
            result.pop("counted", None)   # per-order counts: the filter's only
        populations = {name: {key: digest for key, digest in sorted(st.populations.items())
                              if name in st.population_readers[key]
                              or "*" in st.population_readers[key]}
                       for name in results}
        # the bound is spent on the detectors and the amounts only: the run's record (scope,
        # digest) is always read whole, so a run cut short still folds what it found
        st.watch(None)
        # the scope is folded only by a whole run (or the last group of one): a group
        # run of a pass does not read it (every record of the store, each group)
        held = st.scope() if scope is None and (
            only is None or finish_pass or set(SET_BASED) <= set(results)) else []
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
            if not_due:
                summary["not_due"] = sorted(not_due)   # their cadence: left as they were
            code = engine_code()
            conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", [
                (_CADENCE_KEY + name, json.dumps({"at": at, "code": code}, sort_keys=True))
                for name, at in sorted(started_at.items())
                if name in results and results[name].get("status") not in
                ("not_configured", "failed")])
            if abandoned:
                summary["cut_off"] = sorted(abandoned)   # past their hard window
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
                      record_run=only is None or finish_pass, whole_scope=finish_pass)
            if checkpoint is not None:
                _write_pass(conn, checkpoint[0], checkpoint[1], order + not_due)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return summary


def _drop_test_records(st: _Store, results: dict[str, dict[str, Any]], as_of: date) -> None:
    """Remove every gap about a test record (its subject, or the order it is about) from
    every detector's result, counted ``test_record`` in its coverage."""
    tests = test_records(st, as_of)
    if not tests:
        return
    for result in results.values():
        kept, dropped = [], 0
        for gap in result.get("gaps") or ():
            follow = gap.get("follow_up") or {}
            order = (follow.get(_ORDER) or {}).get("key")
            if gap.get("object_id") in tests or order in tests \
                    or follow.get(_ATTESTS) in tests:
                dropped += 1
            else:
                kept.append(gap)
        if dropped:
            result["gaps"] = kept
            skipped = result.setdefault("not_evaluated", {})
            if isinstance(skipped, dict):
                skipped["test_record"] = skipped.get("test_record", 0) + dropped


@contextlib.contextmanager
def _schedule_view(path: Path, store: Path) -> Iterator[tuple[sqlite3.Connection, str | None]]:
    """(the inbox read only, the served data's stamp) for the scheduler's checks, which
    read only the inbox's own tables. When Postgres is the only store the inbox is not
    attached to a copy of the store's objects (``_attach_postgres``: 2-5 minutes on a
    loaded host, at the start of every pass, 11-14): the stamp is asked of Postgres
    directly (None when it cannot say: every rule is then due)."""
    from operating.pg import ontology_reader
    if not ontology_reader.pg_only():
        with closing(_attach(path, store)) as conn:
            served = _served_digest(conn, store)
            yield conn, served[0] if served else None
        return
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None,
                           timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if row is None or row[0] != INBOX_SCHEMA:
            raise ValueError("gap_inbox_schema_mismatch")
        yield conn, ontology_reader.change_stamp()
    finally:
        conn.close()


def rules_behind(store_path: Any, inbox: Any = None) -> set[str]:
    """The rules whose findings in the inbox are not of the served generation and the
    deployed code (``_current_since``): each needs a whole run before it is current. By
    generation, not by Postgres's data version: that one moves with every applied delta
    layer (every few minutes), so every rule read as behind after each layer and every
    incremental pass judged every store-only rule whole (11-13/14)."""
    store = Path(store_path)
    path = Path(inbox) if inbox is not None else inbox_path(store)
    if not path.is_file():
        return set(DETECTORS)
    with _schedule_view(path, store) as (conn, served):
        older: set[str] = set()
        _current, behind = _current_since(conn, served, older, by_generation=True)
    return (set(behind) | older) & set(DETECTORS)


def served_behind(store_path: Any, inbox: Any = None) -> bool:
    """Whether the inbox's last full run is over another generation than the store served
    now (or there is none), or a rule's findings are of older code: the scheduled refresh
    of every rule is due (``operating.vendor_watch``). Read only. By generation, not by
    Postgres's data version (``rules_behind``): with that, every pass read as behind and
    ran all ~70 rules, so the 4-hourly whole pass (meant for its ~50 whole-store rules)
    spent its 45 minutes on the tool-call rules and ran past its unit's timeout (11-14)."""
    store = Path(store_path)
    path = inbox_path(store) if inbox is None else Path(inbox)
    if not path.is_file():
        return True
    with _schedule_view(path, store) as (conn, served):
        last = conn.execute("SELECT store_digest FROM main.runs ORDER BY run_id DESC "
                            "LIMIT 1").fetchone()
        older: set[str] = set()
        _current_since(conn, served, older, by_generation=True)
    # or a rule's findings are of older code than the engine deployed
    return last is None or _generation_of(last[0]) is None or \
        _generation_of(last[0]) != _generation_of(served) or bool(older)


def _rule_coverage(conn: sqlite3.Connection, params: Mapping[str, str],
                   detectors: Mapping[str, str], *, whole: bool = False
                   ) -> list[dict[str, Any]]:
    """Per rule, its last run's status and how many records this reader may read it left
    unjudged (``not_evaluated``): counted over the records the reader's grant shows, of
    the skips whose whole cause (every record at its markings) the reader may read, never
    the store's: a skip a hidden record or link caused is not counted, so the count never
    hints at it. None (unknown, never 0) when the rule left a skip no record stands for,
    or the inbox predates per-skip causes while its run counted skips.

    ``checked`` and ``judged`` (the records the rule had to judge, and judged) are counts
    over the whole store: given only to a reader whose grant shows every record the run
    scanned (``whole``), else None, so a count never hints at records hidden from it."""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    counts: dict[str, tuple[Any, Any]] = {}
    if whole and "detector_checked" in tables:
        counts = {row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT detector, checked, judged FROM detector_checked")}
    recorded: dict[str, int] = {}
    if {"rule_coverage", "unjudged_causes"} <= tables:
        recorded = {row[0]: row[1] for row in conn.execute(
            "SELECT detector, unattributed FROM rule_coverage")}
    visible, by_reason = _visible_unjudged(conn, params, tables)
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
        checked, judged = counts.get(name, (None, None))
        if not (isinstance(checked, int) and isinstance(judged, int)
                and 0 <= judged <= checked):
            checked = judged = None   # not counted, or unreadable: never a made-up share
        out.append({"rule": name, "status": detectors.get(name, "not_run"),
                    "not_evaluated": shown, "reasons": reasons,
                    "checked": checked, "judged": judged,
                    # per reason, only when the whole count is known for this reader
                    "not_evaluated_by_reason": dict(sorted(by_reason.get(name, {}).items()))
                    if shown else {}})
    return out


def _visible_unjudged(conn: sqlite3.Connection, params: Mapping[str, str], tables: set[str]
                      ) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """Per rule, the records this reader may read it left unjudged, of the skips whose
    whole cause the reader may read; and the same per reason. Kept per
    ``_coverage_key`` (never for a key that cannot be said)."""
    if "unjudged_causes" not in tables:
        return {}, {}
    key = _coverage_key(conn, params) if {"detector_runs", "rule_coverage"} <= tables else None
    if key is not None:
        with _coverage_lock:
            hit = _coverage_kept.get(key)
            if hit is not None:
                _coverage_kept.move_to_end(key)
                return hit[0], {k: dict(v) for k, v in hit[1].items()}
    # what every reader's counts are made of, read once per inbox and store (not per
    # reader); this reader's grant then decides each kind of cause, never each record
    table = _unjudged_table(conn, tables, key[:4] if key is not None else None)
    marks = set(json.loads(params["marks"]))
    auds = set(json.loads(params["aud"]))
    shown_cause = [cause_marks in marks and any(a in auds and m in marks for a, m in pairs)
                   for cause_marks, pairs in table["causes"]]
    shown_skip = [all(shown_cause[c] for c in kinds) for kinds in table["skips"]]
    counted: dict[str, int] = defaultdict(int)
    per_reason: dict[tuple[str, str], int] = defaultdict(int)
    for (name, reason, skips), n in table["counts"].items():
        if any(shown_skip[k] for k in skips):
            if reason is None:
                counted[name] += n
            else:
                per_reason[(name, reason)] += n
    visible = dict(counted) if "rule_coverage" in tables else {}
    by_reason: dict[str, dict[str, int]] = defaultdict(dict)
    for (name, reason), n in per_reason.items():
        by_reason[name][reason] = n
    by_reason = dict(by_reason)
    if key is not None:
        with _coverage_lock:
            _coverage_kept[key] = (dict(visible), {k: dict(v) for k, v in by_reason.items()})
            while len(_coverage_kept) > _COVERAGE_KEPT:
                _coverage_kept.popitem(last=False)
    return visible, by_reason


def _unjudged_table(conn: sqlite3.Connection, tables: set[str],
                    key: tuple[Any, ...] | None) -> dict[str, Any]:
    """The rule coverage's unjudged records sorted by what a grant decides of them, for
    every reader at once: ``causes``, each kind of cause (its own markings, and the
    (audience, markings) of every current version of its record: none when the record is
    not in the store, never readable); ``skips``, each kind of skip (the cause kinds it
    holds: a skip is readable only when every cause is); ``counts``, per (rule, reason or
    None, the skip kinds a record is counted under) how many records. A reader's count of
    a rule is the records with at least one readable skip kind, as ``_visible_unjudged``
    counted them one by one. Kept per ``key`` (the inbox and store parts of
    ``_coverage_key``), never for a key that cannot be said."""
    if key is not None:
        with _coverage_lock:
            hit = _tables_kept.get(key)
            if hit is not None:
                _tables_kept.move_to_end(key)
                return hit
        hit = _table_from_disk(key)
        if hit is not None:
            with _coverage_lock:
                _tables_kept[key] = hit
                while len(_tables_kept) > _TABLES_KEPT:
                    _tables_kept.popitem(last=False)
            return hit
        # one build per key at a time: the page reads that ask at once wait for it and
        # read what it kept, rather than each building it again beside it
        with _coverage_lock:
            building = _tables_building.setdefault(key, threading.Lock())
        with building:
            with _coverage_lock:
                hit = _tables_kept.get(key)
            if hit is not None:
                return hit
            try:
                return _built_table(conn, tables, key)
            finally:
                with _coverage_lock:
                    if _tables_building.get(key) is building:
                        del _tables_building[key]
    return _built_table(conn, tables, key)


def read_inbox(inbox: Any, store: Any, *, reader: str, may_read: Callable[[str], bool],
               held: Iterable[str], decide: Callable[[Sequence[str]], Mapping[str, Any]],
               states: Sequence[str] = ("open", "acknowledged"),
               limit: int = MAX_SHOWN, kinds: Sequence[str] | None = None,
               today: date | None = None, since: str | None = None,
               all_owners: bool = False, include_off: bool = False,
               enabled: Iterable[str] = ()) -> dict[str, Any]:
    """The gaps one reader may see, trimmed: every late order first (longest late first), then
    by priority; what the last run
    covered of what this reader may read. ``may_read(audience_key)``, ``held`` (the
    markings the reader holds) and ``decide(handles)`` (None per handle it may open)
    are the reader's grant; anything they refuse is never shown nor counted. ``complete``
    is true only when every rule ran to the end; ``not_run`` names each rule that did not
    (not configured on this schedule, blocked, or never run), so no reader takes an
    inbox some rules never looked at for a clean result. ``kinds``: only gaps of these
    kinds are read (the briefing's events), so other kinds never use up ``limit``.

    A rule whose findings are not of the served data and deployed code (``refreshing``)
    or whose last run failed keeps its last good findings on the list, each marked
    (``carried``: why, and when it was last checked; a failed rule's failure time), never
    shown as current and never hidden all at once (a code change would otherwise empty
    the briefing until the next pass). Relative wording ("12 days past", "in 3 days") is
    counted to ``today`` (default the business day now) when read, never frozen at the
    run (``_relative``).
    ``since``: only gaps first seen at or after this ISO moment, newest first (an alert
    rule's new findings: an older backlog never uses up ``limit``).
    ``all_owners``: a person's own-mail kinds are listed whoever owns them (everything else
    about the trim is unchanged); only the Team drill-down sets it: for the owner and the named Team
    readers, after the route has refused every other reader."""

    # an alert type the registry keeps off (``alert_kinds``) yields nothing to a reader
    # unless they switched it on; only a page that lists it on purpose reads it
    # (``include_off``: the Late POs page's at-risk panel)
    # An unnarrowed read (Home, plx.find's home mode) returns only kinds a registered type
    # names: a kind no one measured never reaches home (business rule 11-11: an alert reaches
    # home only once measured at the bar); a page naming its kinds still reads them.
    off_kinds: list[str] = []
    if not include_off:
        off = alert_kinds.off_gap_kinds(enabled)
        if kinds is None:
            kinds = sorted(alert_kinds.registered_gap_kinds() - off)
        kinds = [k for k in kinds if k not in off] or ["\x00none"]
        off_kinds = sorted(off)

    path = Path(inbox)
    today = _today() if today is None else today
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
        digest = _served_digest(conn, store)
        older: set[str] = set()
        current, behind = _current_since(conn, digest[0] if digest else None, older)
        scanned = conn.execute(
            "SELECT COALESCE(SUM(objects), 0) FROM scope WHERE audience_key IN (SELECT value "
            "FROM json_each(:aud)) AND markings IN (SELECT value FROM json_each(:marks))",
            params).fetchone()[0]
        runs = conn.execute("SELECT detector, status, reasons, not_evaluated, run_at FROM "
                            "detector_runs ORDER BY detector").fetchall()
        detectors = {row["detector"]: row["status"] for row in runs}
        failed_at = {row["detector"]: row["run_at"] for row in runs
                     if row["status"] == "failed"}
        # the rules whose last good findings are carried, marked, until a run of the
        # deployed code over the served data replaces them
        carried_rules = (set(behind) | older) & set(detectors)
        # the rules whose last run was cut short (its window or the run's budget spent):
        # what they found stands; what they did not reach again is their last good
        # finding over older data, carried and marked, never shown as current nor
        # dropped unseen (a rule slower than its window would otherwise show nothing)
        cut_short = {name for name, status in detectors.items() if status == "truncated"}
        # likewise a subject a rule's last run read but could not judge (``unchecked``)
        unchecked = _held_unchecked(conn)
        # each rule's last run (a full, 4-hourly, calling or incremental pass): its time
        run_at = {row["detector"]: row["run_at"] for row in runs
                  if isinstance(row["run_at"], str)}
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
                "json_each(:kinds))) AND g.kind NOT IN (SELECT value FROM json_each(:off)) "
                "AND (:since IS NULL OR julianday(g.first_seen) >= "
                "julianday(:since)) AND " + _OBJECT_VISIBLE.format(o="g.object_id")
                + " ORDER BY CASE WHEN :since IS NOT NULL THEN julianday(g.first_seen) END "
                "DESC, json_extract(g.follow_up, '$.days_late') IS NULL, "
                "json_extract(g.follow_up, '$.days_late') DESC, "
                # a list of near-expiry lots alone: soonest expiry first, so a page cut
                # short still holds every lot expiring before its last one
                "CASE WHEN :soonest THEN json_extract(g.follow_up, '$.event.days') END, "
                "g.priority, g.last_seen DESC, g.gap_id LIMIT :page OFFSET :offset",
                {**params, "reader": reader, "states": global_states, "page": page,
                 "soonest": kinds is not None and list(kinds) == ["lot_near_expiry"],
                 "offset": offset, "since": since,
                 "kinds": None if kinds is None else json.dumps(list(kinds)),
                 "off": json.dumps(off_kinds)}).fetchall()
            if not rows:
                break
            offset += len(rows)
            # a gap found over older data than the store served now is never shown: the
            # record page reads the served data, so the two would disagree
            rows = [row for row in rows if _of_served(row, current) or (
                (row["detector"] in carried_rules | cut_short
                 or (row["detector"], row["object_id"]) in unchecked)
                and row["state"] == "open")]
            for row, citations in _visible(conn, rows, params, decide):
                state = ("resolved" if row["state"] == "resolved"
                         else "acknowledged" if row["acked"] else "open")
                if state not in states or hide(row["gap_id"], reader) is not None:
                    continue
                follow_up = json.loads(row["follow_up"])
                if not all_owners and not _owned(follow_up, row["kind"], reader):
                    continue   # another person's own mail: never theirs to see
                order = follow_up.pop(_ORDER, None)
                follow_up.pop(_ATTESTS, None)   # the filters' key, never shown
                held = orders.get(order["key"]) if isinstance(order, dict) else None
                if held is not None:
                    # this order is listed: its governing record's item replaces a twin's,
                    # never a second item (each was decided visible to this reader)
                    if order["rank"] < held[0]:
                        index = next(i for i, g in enumerate(shown) if g is held[1])
                        shown[index] = entry = _shown(row, citations, state, follow_up, allowed, readable, today=today,
                                   carried=_carried(row, carried_rules, older, failed_at,
                                                    cut_short, current, unchecked))
                        orders[order["key"]] = (order["rank"], entry)
                    continue
                if len(shown) >= limit:
                    more = True
                    break
                entry = _shown(row, citations, state, follow_up, allowed, readable, today=today,
                                   carried=_carried(row, carried_rules, older, failed_at,
                                                    cut_short, current, unchecked))
                shown.append(entry)
                if isinstance(order, dict):
                    orders[order["key"]] = (order["rank"], entry)
        # the store-wide judged counts only for a reader shown every record the run
        # scanned (the scope's every audience and marking)
        whole = scanned > 0 and scanned == conn.execute(
            "SELECT COALESCE(SUM(objects), 0) FROM scope").fetchone()[0]
        rules = _rule_coverage(conn, params, detectors, whole=whole)
    try:
        _learned_order(shown, store, reader)
    except Exception:  # noqa: BLE001 - lessons never break a read: the order stands
        pass
    failed = sorted(name for name, status in detectors.items() if status == "failed")
    truncated = any(status == "truncated" for status in detectors.values())
    # A rule that did not run (not configured on this schedule, blocked on a source, or
    # with no row at all) looked for nothing: the read is never complete while one remains.
    # a rule whose input this host has not set up yet (``SETUP_STATES``: the sheet readings
    # before the reading step runs here) is a setup state, named in ``detectors``, never a
    # rule that looked and missed: the read stays complete
    not_run = {name: detectors.get(name, "not_run") for name in DETECTORS
               if detectors.get(name) not in ("complete", "truncated", "failed",
                                              *SETUP_STATES)}
    # the rules whose findings are of older data than the store served now: theirs are not
    # shown until they run over it (the scheduled refresh), and the read says so
    refreshing = sorted(name for name in behind if name in detectors)
    checked = sorted(at for name in refreshing if isinstance(at := behind[name], str))
    return {"status": "read", "as_of": last["as_of"] if last else None,
            # each failed rule's failure time: its findings shown are its last good ones
            "failed_at": dict(sorted(failed_at.items())),
            "carried": sum(1 for g in shown if g.get("carried")),
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
            "detector_run_at": run_at,
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
        rows = [row for row in rows if _owned(json.loads(row["follow_up"]), row["kind"], reader)]
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
    off = alert_kinds.off_gap_kinds()   # an off type is never counted to a reader either
    kinds = [k for k in kinds if k not in off]
    path = Path(inbox)
    if not path.is_file():
        return {}
    with closing(_attach(path, Path(store))) as conn:
        aud, marks = _reader_view(conn, may_read, held)
        params = {"aud": aud, "marks": marks, "kinds": json.dumps(list(kinds))}
        served = _served_digest(conn, store)
        current, _ = _current_since(conn, served[0] if served else None)
        # a gap of older data than the store served is shown to no one (``read_inbox``):
        # it is not counted as one hidden from this reader either
        # a person's own mail (``OWNER_KINDS``) is nobody else's: never counted as hidden
        total = Counter(row["kind"] for row in conn.execute(
            "SELECT kind, detector, last_seen FROM gaps WHERE state = 'open' AND kind IN "
            "(SELECT value FROM json_each(:kinds))", params) if _of_served(row, current)
            and row["kind"] not in _OWNER_KINDS)
        rows = [row for row in conn.execute(
            "SELECT g.* FROM gaps g WHERE g.state = 'open' AND g.kind IN (SELECT value FROM "
            "json_each(:kinds)) AND " + _OBJECT_VISIBLE.format(o="g.object_id"),
            params).fetchall() if _of_served(row, current)]
        seen = Counter(row["kind"] for row, _ in _visible(conn, rows, params, decide)
                       if row["kind"] not in _OWNER_KINDS)
    return {kind: total[kind] - seen[kind] for kind in sorted(total)
            if total[kind] - seen[kind] > 0}


def unjudged_records(inbox: Any, store: Any, *, rule: str, reason: str | None = None,
                     may_read: Callable[[str], bool], held: Iterable[str],
                     offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """The records one rule left unjudged that this reader is counted (``_rule_coverage``'s
    ``not_evaluated``, per ``reason`` its ``not_evaluated_by_reason``): each a subject of a
    skip whose whole cause the reader may read, so the list never names or hints at a
    record the count leaves out, and its ``total`` is that count. ``complete`` is false
    when the rule left a skip no record stands for (its count is unknown to every reader).
    An unknown rule or a malformed reason is refused (``UnknownCheck``); no inbox or no
    recorded causes: nothing listed, not complete."""
    if rule not in DETECTORS:
        raise UnknownCheck("rule_unknown")
    if reason is not None and (not isinstance(reason, str)
                               or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason)):
        raise UnknownCheck("reason_unknown")
    offset = max(0, int(offset))
    limit = max(0, min(int(limit), UNJUDGED_SHOWN))
    out: dict[str, Any] = {"rule": rule, "reason": reason, "total": 0, "records": [],
                           "complete": False}
    path = Path(inbox)
    if not path.is_file():
        return out
    with closing(_attach(path, Path(store))) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"unjudged_causes", "rule_coverage"} <= tables \
                or (reason is not None and "unjudged_reasons" not in tables):
            return out
        aud, marks = _reader_view(conn, may_read, held)
        params = {"aud": aud, "marks": marks, "rule": rule, "reason": reason}
        row = conn.execute("SELECT unattributed FROM rule_coverage WHERE detector = :rule",
                           params).fetchone()
        if row is None:   # never run with per-skip causes: no count, so no list
            return out
        out["complete"] = int(row[0] or 0) == 0
        hidden = ("SELECT c.skip FROM unjudged_causes c WHERE c.detector = :rule AND NOT "
                  "(c.markings IN (SELECT value FROM json_each(:marks)) AND "
                  + _OBJECT_VISIBLE.format(o="c.object_id") + ")")
        shown = ("SELECT DISTINCT u.object_id FROM unjudged_causes u "
                 + ("JOIN unjudged_reasons r ON r.detector = u.detector AND r.skip = u.skip "
                    "AND r.reason = :reason " if reason is not None else "")
                 + f"WHERE u.detector = :rule AND u.subject = 1 AND u.skip NOT IN ({hidden})")
        out["total"] = conn.execute(f"SELECT count(*) FROM ({shown})", params).fetchone()[0]
        out["records"] = [r[0] for r in conn.execute(
            f"{shown} ORDER BY u.object_id LIMIT :limit OFFSET :offset",
            {**params, "limit": limit, "offset": offset})]
    return out


def refresh(runtime: Any, investigator: Any, audience: Any, projection: Path, as_of: date,
            *, captures: Path | None, scan_coverage: bool = True,
            budget: Budget | None = None,
            clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
            vendor_bound: int | None = None, pool: Any = None, threads: int | None = None,
            detector_seconds: Mapping[str, float] | None = None,
            only: Sequence[str] | None = None, **pass_options: Any) -> dict[str, Any]:
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

    # no SharePoint warm-up beside the rules: a batch reader, not one answering questions
    session = live._Session(runtime, investigator, audience, projection, captures=captures,
                            marking_grants=engine_marking_grants(investigator), warm=False)
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
                   pool=pool, threads=threads, detector_seconds=detector_seconds, only=only,
                   **pass_options)
    finally:
        session.close()


def rule_groups(rules: Sequence[str], *, threads: int = 1,
                group_seconds: float | None = None) -> list[list[str]]:
    """``rules`` (in ``DETECTORS`` order) cut into groups of about ``group_seconds``
    each: a tool-call rule (``ON_RUN_THREAD``, one after another on the run's thread)
    costs its whole window, a store-only one its window over ``threads``. The tool-call
    rules come first, as in a whole run; a rule longer than a group is a group alone."""
    group_seconds = GROUP_SECONDS if group_seconds is None else group_seconds
    ranked = [d for d in DETECTORS if d in set(rules)]
    ranked.sort(key=lambda d: d not in ON_RUN_THREAD)   # stable: DETECTORS order within
    groups: list[list[str]] = []
    load = 0.0
    for name in ranked:
        cost = DETECTOR_SECONDS.get(name, DETECTOR_SECONDS_DEFAULT)
        if name not in ON_RUN_THREAD:
            cost /= max(1, threads)
        if not groups or load + cost > group_seconds:
            groups.append([])
            load = 0.0
        groups[-1].append(name)
        load += cost
    return groups


def served_generation(store_path: Any, inbox: Any = None) -> str | None:
    """The generation served now (read only; None when it cannot be told)."""
    store = Path(store_path)
    path = inbox_path(store) if inbox is None else Path(inbox)
    if not path.is_file():
        return None
    with _schedule_view(path, store) as (_conn, served):
        return _generation_of(served)


def refresh_pass(runtime: Any, investigator: Any, audience: Any, projection: Path,
                 as_of: date, *, kind: str, seconds: float, only: Sequence[str] | None = None,
                 reserve: float = 0.0, threads: int | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 monotonic: Callable[[], float] = time.monotonic,
                 run_group: Callable[..., dict[str, Any]] | None = None,
                 **options: Any) -> dict[str, Any]:
    """The scheduled refresh of ``only`` (None: every rule, a whole run) as a pass of
    ``kind`` (``vendor_watch``'s refresh mode, or ``whole`` for every rule), group by group
    (``rule_groups``), within ``seconds``: a group starts only when its planned length
    fits what is left (the first of this process always starts, so a pass always moves
    on); the rest wait for the next pass of the kind, which resumes this one. The last
    group of a pass of every rule records the run as a whole run does. The groups share
    one ``PassCache`` (``run_group(pass_cache=)``): the link table is scanned once a
    pass, not once a group, and the test records read once; a pass of every rule reads
    every link type of a whole run in its first group's one scan. The summary: the
    groups' counts merged, and ``pass`` (kind, resumed, groups run, rules left)."""
    plan = list(DETECTORS) if only is None else [d for d in DETECTORS if d in set(only)]
    inbox = inbox_path(projection)
    moment = clock()
    generation = served_generation(projection, inbox)
    code = engine_code()
    key = PASS_KEY + ("all" if only is None else kind)   # every rule: one pass, any mode
    held = pass_record(inbox, key[len(PASS_KEY):])
    resumed = _resumable(held, plan, generation, code, moment)
    record = dict(held) if resumed and held else {
        "plan": plan, "generation": generation, "code": code, "done": [],
        "started": moment.isoformat()}
    done = set(record["done"])
    lanes = detector_threads() if threads is None else max(1, threads)
    groups = rule_groups([d for d in plan if d not in done], threads=lanes)
    run_group = run_group or refresh
    end = monotonic() + seconds
    merged: dict[str, Any] = {"detectors": {}, "detector_reasons": {}, "seconds": {},
                              "gaps_found": 0}
    ran = 0
    shared = PassCache(prefetch=only is None)
    try:
        for n, group in enumerate(groups):
            left = end - monotonic()
            if ran and left < group_seconds(group, lanes):
                break   # the next pass of this kind resumes here
            if left <= 0:
                break
            last = only is None and n == len(groups) - 1
            out = run_group(runtime, investigator, audience, projection, as_of,
                            budget=Budget(left, monotonic, reserve=reserve), only=group,
                            threads=threads, clock=clock, finish_pass=last,
                            checkpoint=(key, record), pass_cache=shared, **options)
            ran += 1
            record = {**record, "done": sorted(set(record["done"]) | set(group))}
            if record["generation"] is None:   # the first pass of a new inbox: made just now
                record["generation"] = served_generation(projection, inbox)
            for field in ("detectors", "detector_reasons", "seconds"):
                if isinstance(out.get(field), Mapping):
                    merged[field].update(out[field])
            merged["gaps_found"] += int(out.get("gaps_found") or 0)
    finally:
        # the pass's reads let go before the caller's next stage: the groups' stores
        # outlive the pass (their bound methods), and they hold the cache, not its rows
        shared.clear()
    left_rules = [d for d in plan if d not in set(record["done"])]
    merged["pass"] = {"kind": key[len(PASS_KEY):], "resumed": resumed, "groups": len(groups),
                      "groups_run": ran, "rules": len(plan), "left": left_rules,
                      "complete": not left_rules}
    return merged


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


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         host_options: Mapping[str, Any] | None = None, graph_sender: Any = None) -> int:
    from integrations.source_mirror import AudienceMap
    from ontology.core import Actor
    from operator_api.workspace.object_workspace import HostAccess

    from . import case_workspace_live as live
    from . import case_workspace_live_projection

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
        case_workspace_live_projection.project_state(args.state_dir, projection, tenant=args.tenant)
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
