"""The proactive engine: PO lateness, the home late count and recall, fast-read equivalence,
pass shares, business days, console drafts, the Postgres store, exclusion and statement
timeouts (split from test_proactive_engine so each module stays under the suite per-module
limit)."""

from __future__ import annotations

import contextlib
import functools
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest

# The bases by module attribute, so this module's loader does not collect their own tests
# (test_proactive_engine runs them).
from operating import proactive_engine_config, proactive_engine_quantities
from operating.tests import test_proactive_engine as _pe
from operating.tests.test_proactive_engine import (
    ALL_MARKINGS, AS_OF, ActionPath, CALLING_RULES, CARDINALITY_SECONDS,
    CHANNEL_CONSOLE, CaseStore, DRAFT_ACTION, Decimal, EXCLUDED_OID, FakeAccess,
    FakeResolver, FakeStatus, ON_HAND, OPS, Path, QC_LINK, World, admit, closing, date,
    datetime, everyone, fixture_session, handle, mock, objects, ostore, pe, people, po,
    po_status, seeded, single_query_links, single_query_values, timedelta, timezone,
    view_quantity_rows)


class PoLatenessTest(_pe.Engine):
    """The late/unconfirmed PO rule reads the one PO status definition on the real Portal
    header words and Vince's lateness rule on the requested-by date alone."""

    def confirm(self, w: World, po_id: str, day: str) -> None:
        w.link("SupplierConfirmation.confirms",
               w.obj("SupplierConfirmation", {"delivery_date": day}), po_id)

    def test_late_at_risk_or_neither_on_the_requested_by_date(self):
        w = World()
        late = po(w, "Pending", "2026-10-10", lines=(("10", "4"),))           # passed, short
        self.confirm(w, late, "2026-10-05")       # a confirmation never excuses lateness
        at_risk = po(w, "B.Order", "2026-10-25")                               # not yet due
        self.confirm(w, at_risk, "2026-11-02")    # supplier later than requested
        on_time = po(w, "Pending", "2026-10-25")
        self.confirm(w, on_time, "2026-10-24")    # neither
        received = po(w, "Pending", "2026-10-01", lines=(("10", "10"),))      # neither
        self.confirm(w, received, "2026-11-30")
        summary = self.run_engine(w)
        found = {(g["kind"], g["object_id"]): g for g in self.read()["gaps"]}
        self.assertEqual(set(found), {("late_po", late), ("late_po", at_risk)})
        self.assertIn("past its need date 2026-10-10", found[("late_po", late)]["summary"])
        self.assertIn("2026-11-02", found[("late_po", at_risk)]["summary"])
        self.assertEqual(summary["po_lateness"], {
            "pos": 4, "classes": {"open": 3, "closed": 1, "draft": 0, "unknown": 0},
            "evaluated": 3, "late": 1, "at_risk": 1, "unconfirmed": 0, "undated": 0,
            "historic": 0, "open_by_read": 3, "open_other": 2,
            # the received notes doubted by alerts-fix-1113 (f3312de714): none here
            "closed_elsewhere": 0, "received_header_open": 0, "received_without_receipt": 0})

    def test_an_undated_order_with_a_lead_confirmation_is_not_said_to_have_no_date(self):
        # 11-13 judging (undated_po): a supplier confirmation linked only as a lead stated
        # delivery 2027-01-25; "no supplier-confirmed date" was not known
        w = World()
        led = po(w, "Pending")
        w.link("SupplierConfirmation.confirms",
               w.obj("SupplierConfirmation", {"delivery_date": "2027-01-25"}), led, "lead")
        bare = po(w, "Pending")
        summary = self.run_engine(w)
        undated = {g["object_id"] for g in self.read()["gaps"] if g["kind"] == "undated_po"}
        self.assertEqual(undated, {bare})
        self.assertEqual(summary["not_evaluated"]["late_or_unconfirmed_po"]
                         ["confirmation_unsettled"], 1)

    def test_complete_closes_with_its_remainder_cancelled_and_sage_only_is_not_open(self):
        # Ross: a Complete header is closed whatever was received, the
        # outstanding quantity cancelled; only a still-issued word over it is open
        w = World()
        complete = po(w, "Complete", "2026-10-02", lines=(("10", "4"), ("5", "5")))
        outstanding = po(w, "B.Order", "2026-10-02", lines=(("10", "4"), ("5", "5")))
        fm_only = w.obj("PurchaseOrder", {"fm_status": "Complete",
                                          "expected_date": "2026-10-02"})
        sage_open = w.obj("PurchaseOrder", {"sage_is_complete": "0",
                                            "expected_date": "2026-10-02"})
        sage_done = w.obj("PurchaseOrder", {"sage_is_complete": "1",
                                            "expected_date": "2026-10-02"})
        summary = self.run_engine(w)
        shown = {g["object_id"] for g in self.read()["gaps"]}
        self.assertEqual(shown, {outstanding})
        self.assertFalse({complete, fm_only, sage_open, sage_done} & shown)
        # Sage complete is a closed historical record; Sage open (0) stays unknown
        self.assertEqual(summary["po_lateness"]["classes"],
                         {"open": 1, "closed": 3, "draft": 0, "unknown": 1})
        self.assertEqual(summary["not_evaluated"]["late_or_unconfirmed_po"],
                         {"sage_unmapped": 1})
        status = po_status.read(po_status.StoreSource(pe._Store(self.store)), complete,
                                AS_OF).status
        self.assertEqual((status.cls, status.code, status.canceled_remainder),
                         (po_status.CLOSED, po_status.COMPLETE, Decimal("6")))

    def test_unknown_words_and_unread_lines_fail_closed_and_are_counted(self):
        w = World()
        po(w, "On Hold", "2026-10-01")                                   # unmapped word
        w.obj("PurchaseOrder", {"status": "", "expected_date": "2026-10-01"})  # blank
        po(w, "Pending", "2026-10-01", lines=())                         # no lines read
        unit = w.obj("PurchaseOrder", {"status": "Pending", "expected_date": "2026-10-01"})
        w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
            "ordered_quantity": ("10", "kg"),
            "received_quantity": ("10", "lb", "received")}), unit)       # units differ
        po(w, "DRAFT", "2026-10-01")
        po(w, "Cancel", "2026-10-01")
        summary = self.run_engine(w)
        self.assertEqual(self.read()["gaps"], [])
        self.assertEqual(summary["po_lateness"]["evaluated"], 0)
        self.assertEqual(summary["po_lateness"]["classes"],
                         {"open": 0, "closed": 1, "draft": 1, "unknown": 4})
        reasons = summary["not_evaluated"]["late_or_unconfirmed_po"]
        self.assertEqual(sum(reasons.values()), 4)
        self.assertLessEqual(set(reasons), set(po_status.UNKNOWN_REASONS))

    def test_a_null_received_quantity_is_nothing_received_as_the_portal_sums_it(self):
        # the Portal's line column is null until a receipt posts (SUM/COALESCE reads 0):
        # an open PO, never quantities_unknown; a null ordered quantity stays unknown
        w = World()
        unreceived = w.obj("PurchaseOrder", {"status": "Pending", "expected_date": "2026-10-01"})
        w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
            "ordered_quantity": ("300", "EA"),
            "received_quantity": (None, None, "received")}), unreceived)
        unordered = w.obj("PurchaseOrder", {"status": "Pending", "expected_date": "2026-10-01"})
        w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
            "ordered_quantity": (None, None),
            "received_quantity": (None, None, "received")}), unordered)
        summary = self.run_engine(w)
        self.assertEqual({(g["kind"], g["object_id"]) for g in self.read()["gaps"]},
                         {("unconfirmed_po", unreceived)})
        self.assertEqual(summary["po_lateness"]["classes"],
                         {"open": 1, "closed": 0, "draft": 0, "unknown": 1})
        self.assertEqual(summary["not_evaluated"]["late_or_unconfirmed_po"],
                         {"quantities_unknown": 1})

    def test_po_dates_are_erased_only_with_provenance_and_an_own_day_is_doubted(self):
        """BIZ-007 for purchase orders: a delivery day many orders placed on many days
        share is a real date (judged); a bulk day a system computed from each order's own
        day (order day + 1) is a default (undated); a placeholder 1900-01-01 is no date;
        an expected date on the order's own day is judged at low priority with its doubt."""
        w = World()

        def order(expected: str, ordered: str) -> str:
            oid = w.obj("PurchaseOrder", {"status": "Pending", "expected_date": expected,
                                          "order_date": ordered})
            w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
                "ordered_quantity": ("10", "ea"),
                "received_quantity": ("0", "ea", "received")}), oid)
            return oid
        shared = [order("2026-09-15", f"2026-08-{1 + i:02d}") for i in range(12)]
        computed = [order(f"2026-08-{2 + i:02d}", f"2026-08-{1 + i:02d}") for i in range(1)]
        computed += [order("2026-08-21", "2026-08-20") for _ in range(12)]
        sentinel = [order("1900-01-01", f"2026-07-{1 + i:02d}") for i in range(12)]
        later = [order(f"2026-11-{2 + i % 20:02d}", f"2026-09-{1 + i:02d}") for i in range(20)]
        own = order("2026-09-30", "2026-09-30")
        summary = self.run_engine(w)
        gaps = self.read(limit=500)["gaps"]
        dated = {g["object_id"]: g for g in gaps if g["kind"] in ("late_po", "unconfirmed_po")}
        undated = {g["object_id"] for g in gaps if g["kind"] == "undated_po"}
        self.assertEqual(set(dated), {*shared, computed[0], own})
        self.assertTrue(set(computed[1:]) <= undated and set(sentinel) <= undated)
        self.assertFalse(set(later) & (set(dated) | undated))
        self.assertIn("the day the order was placed", dated[own]["summary"])
        self.assertEqual(dated[own]["follow_up"]["priority"], "low")
        self.assertNotIn("the day the order was placed", dated[shared[0]]["summary"])
        self.assertNotEqual(dated[shared[0]]["follow_up"]["priority"], "low")
        self.assertIn("system-computed or placeholder defaults",
                      next(g for g in gaps if g["object_id"] == sentinel[0])["summary"])
        self.assertEqual(summary["po_lateness"]["evaluated"], 34)   # 14 due + 20 not yet due

    def test_an_open_lines_expected_date_stands_in_only_below_every_header_date(self):
        """No header date: the open line's own expected date is the lowest tier (the
        lateness oracle's source), so the order is late on it, whatever was confirmed.
        With no date of any kind, the supplier's own confirmed date is a labelled due date
        (owner, 2026-11-06), never a requested-by date: late past it, said so."""
        w = World()
        dated = po(w, "Pending")
        w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
            "bc_expected_receipt_date": "2026-10-02", "ordered_quantity": ("1", "ea")}),
            dated)
        self.confirm(w, dated, "2026-10-03")
        bare = po(w, "Pending")
        self.confirm(w, bare, "2026-10-03")   # promise only: no date of any kind
        summary = self.run_engine(w)
        gaps = {g["object_id"]: g for g in self.read()["gaps"]}
        self.assertEqual({(g["kind"], oid) for oid, g in gaps.items()},
                         {("late_po", dated), ("late_po", bare)})
        self.assertIn("past its need date 2026-10-02", gaps[dated]["summary"])
        self.assertIn("past the supplier's confirmed date 2026-10-03", gaps[bare]["summary"])
        self.assertEqual(gaps[bare]["follow_up"]["due_basis"], "vendor_eta")
        self.assertNotIn("late_or_unconfirmed_po", summary["not_evaluated"])
        self.assertEqual(summary["po_lateness"]["evaluated"], 2)

    def test_a_reader_who_cannot_read_the_line_quantities_does_not_see_the_gap(self):
        w = World()
        subjects = []
        for need in ("2026-10-01", "2026-10-25"):        # unconfirmed late; at risk
            subject = w.obj("PurchaseOrder", {"status": "Pending", "expected_date": need})
            w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
                "ordered_quantity": ("10", "ea"), "received_quantity": ("4", "ea", "received")},
                markings='["finance"]'), subject)
            subjects.append(subject)
        self.confirm(w, subjects[1], "2026-11-02")
        twin = po(w, "Pending", "2026-10-01")             # a hidden twin's status decides too
        hidden_twin = w.obj("PurchaseOrder", {"fm_status": "Pending"}, markings='["finance"]')
        w.link("PurchaseOrder.same_record_as", twin, hidden_twin)
        self.run_engine(w)
        everything = {g["kind"]: g["object_id"] for g in self.read()["gaps"]
                      if g["object_id"] != min(twin, hidden_twin)}
        self.assertEqual(everything, {"unconfirmed_po": subjects[0], "late_po": subjects[1]})
        self.assertIn(min(twin, hidden_twin), {g["object_id"] for g in self.read()["gaps"]})
        hidden = self.read(held=frozenset())
        self.assertEqual(hidden["gaps"], [])
        for subject in subjects:
            self.assertNotIn(subject, json.dumps(hidden["gaps"]))

    def test_cardinality_each_open_po_is_judged_once(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                ids = {po(w, "Pending", (date(2026, 9, 1) + timedelta(days=i % 28)).isoformat(),
                          lines=(("10", str(i % 5)),)) for i in range(n)}
                started = time.process_time()
                summary = self.run_engine(w)
                self.assertLess(time.process_time() - started, CARDINALITY_SECONDS)
                gaps = self.read()["gaps"]
                self.assertEqual(sorted(g["object_id"] for g in gaps), sorted(ids))
                self.assertEqual(summary["po_lateness"]["evaluated"], n)
                self.store.unlink()
                self.inbox.unlink(missing_ok=True)


class GoLiveFirstTest(_pe.Engine):
    """A BC number series' first document (and its same-day siblings) is a go-live test
    record only on the go-live day the deployment states; past a thousand a number ending
    in 001 is an ordinary order, and a series begun on another day is ordinary business
    (AUTOMATION-015)."""

    def marked(self, numbers, go_live):
        w = World()
        ids = {}
        for number, day in numbers:
            ids[number] = w.obj("PurchaseOrder", {"po_number": number, "order_date": day},
                                binding="bc.po")
            line = w.obj("PurchaseOrderLine", {"ordered_quantity": ("100", "each")},
                         binding="bc.po")
            w.link("PurchaseOrderLine.line_of", line, ids[number])
        w.build(self.store)
        st = pe._Store(self.store, threads=1)
        self.addCleanup(st.close)
        env = {} if go_live is None else {"PLX_BC_GO_LIVE_DATE": go_live}
        with mock.patch.dict(os.environ, env):
            if go_live is None:
                os.environ.pop("PLX_BC_GO_LIVE_DATE", None)
            return {n: pe.test_records(st, AS_OF).get(oid) for n, oid in ids.items()
                    if pe.test_records(st, AS_OF).get(oid)}

    def test_a_number_ending_in_001_past_a_thousand_is_an_ordinary_order(self):
        marked = self.marked((
            ("PO100000", "2025-06-01"), ("PO100001", "2025-06-01"),
            ("PO100002", "2025-06-01"), ("PO100003", "2025-06-02"),
            ("PO100999", "2026-09-30"), ("PO101000", "2026-10-01"),
            ("PO101001", "2026-10-01"), ("PO101002", "2026-10-01"),
            ("106001", "2025-06-01"), ("106002", "2025-06-01"),
            ("107001", "2026-10-02"), ("107002", "2026-10-02")), "2025-06-01")
        self.assertEqual(set(marked), {"PO100000", "PO100001", "PO100002", "106001", "106002"})
        self.assertEqual(set(marked.values()), {"go_live_first"})

    def test_a_new_series_with_lines_on_another_day_stays_business(self):
        # PO1001/PO1002 order 100 and 200 on one day of a new series; only
        # the stated go-live day's firsts are go-live records
        numbers = (("PO1001", "2026-11-12"), ("PO1002", "2026-11-12"),
                   ("PO1003", "2026-11-13"), ("106001", "2026-03-28"),
                   ("106002", "2026-03-28"), ("106003", "2026-03-29"))
        self.assertEqual(set(self.marked(numbers, "2026-03-28")), {"106001", "106002"})

    def test_no_stated_go_live_day_marks_nothing(self):
        numbers = (("PO1001", "2026-11-12"), ("PO1002", "2026-11-12"),
                   ("106001", "2026-03-28"))
        for unreadable in (None, "", "28/03/2026", "2026-3-28", "2026-02-30", "soon"):
            with self.subTest(go_live=unreadable):
                self.assertEqual(self.marked(numbers, unreadable), {})

    def test_only_a_series_one_first_in_its_own_width_counts(self):
        # a different prefix or width is another series; two firsts in one series mark none
        mem = [("a", "SO-2024-001", "2025-01-01"), ("b", "SO-2024-002", "2025-01-01"),
               ("c", "SO-2025-001", "2025-01-01"), ("d", "1001", "2025-01-01"),
               ("e", "01001", "2025-01-01"), ("f", "X001", "2025-01-01"),
               ("g", "X001", "2025-01-01"), ("h", "Y001", None), ("i", "", "2025-01-01"),
               ("j", "Z001", "2025-01-02")]
        from operating.proactive_engine_population import series_firsts
        self.assertEqual(series_firsts(mem, date(2025, 1, 1)), {"a", "b", "c", "d", "e"})
        self.assertEqual(series_firsts(mem, None), set())


class HomeLateCountTest(_pe.Engine):
    """Home's late count (``plx.find`` home ``late_orders``, which Ask's count and the late
    list say) is the late rule's: exact when that rule ran over the served data and every
    order it left undecided is one the late-list rule decides is never late."""

    LATE_KINDS = ("late_po", "unconfirmed_po")

    def late_count(self) -> dict:
        from operating import mcp_server
        moment = datetime(2026, 10, 19, 7, tzinfo=timezone.utc)
        return mcp_server._home_answer(self.read(limit=500), self.LATE_KINDS, None, None,
                                       moment)["home"]["late_orders"]

    def world(self) -> tuple[World, str]:
        w = World()
        late = po(w, "Pending", "2026-10-10", lines=(("10", "4"),))
        po(w, "Generated", "2026-10-01")                                # unmapped word
        po(w, "Pending", "2026-10-01", lines=(("10", "UNREAD"),))       # quantities unknown
        w.obj("PurchaseOrder", {"sage_is_complete": "0", "expected_date": "2026-10-01"})
        po(w, "Pending")                                                # no due date
        return w, late

    def test_never_late_skips_and_other_rules_cut_short_leave_the_count_exact(self):
        w, late = self.world()
        summary = self.run_engine(w)   # no mirror coverage nor tool caller: rules not run
        self.assertIn("not_configured", summary["detectors"].values())
        self.assertEqual(set(summary["not_evaluated"]["late_or_unconfirmed_po"]),
                         {"status_unmapped", "quantities_unknown", "sage_unmapped",
                          "no_need_date"})
        read = self.read(limit=500)
        self.assertFalse(read["complete"])
        self.assertTrue(read["late_exact"])
        self.assertEqual(self.late_count(), {"count": 1, "complete": True})
        self.assertEqual({g["object_id"] for g in read["gaps"]
                          if type(g.get("days_late")) is int}, {late})

    def test_an_order_whose_lateness_is_undecided_makes_the_count_a_lower_bound(self):
        w, _late = self.world()
        # one order, two records of one rank due 2026-10-03 and 2026-10-25: late or not
        # is undecided on 2026-10-19, so it may be a late order the count misses
        w.link("PurchaseOrder.same_record_as", po(w, "Pending", "2026-10-03"),
               po(w, "Pending", "2026-10-25"))
        summary = self.run_engine(w)
        self.assertIn("need_date_disputed", summary["not_evaluated"]["late_or_unconfirmed_po"])
        self.assertFalse(self.read()["late_exact"])
        self.assertEqual(self.late_count(), {"count": 1, "complete": False})

    def test_a_late_rule_that_did_not_finish_or_ran_over_older_data_is_not_exact(self):
        w, _late = self.world()
        self.run_engine(w, only=("expired_quote",))   # the late rule never ran
        self.assertFalse(self.read()["late_exact"])
        self.assertFalse(self.late_count()["complete"])

    def test_unknown_or_unreadable_skips_fail_closed(self):
        exact = po_status.late_count_exact
        self.assertTrue(exact({}))
        self.assertTrue(exact({"no_need_date": 3, "status_disputed": 1, "sage_unmapped": 2}))
        self.assertTrue(exact({"need_date_disputed": 0}))
        for bad in ({"need_date_disputed": 1}, {"date_unparseable": 1},
                    {"a_reason_added_later": 1}, {"no_need_date": -1},
                    {"no_need_date": "1"}, {"no_need_date": True}, None, [], "{}"):
            self.assertFalse(exact(bad), bad)


class FastReadEquivalenceTest(_pe.Engine):
    """The engine's set-wise reads (object first in chunks; quantity kind first) find exactly
    the gaps, citations, counts and skips the single view queries found, at every chunk
    size, and a record its source no longer holds stays out of both."""

    def world(self, n: int) -> tuple[World, set]:
        world, ids = seeded()
        order = world.obj("PurchaseOrder", {"po_number": "SYNTH-PO-9"})   # line-level late
        world.link("PurchaseOrder.ordered_from", order,
                   world.obj("Supplier", {"name": "Synthetic Supplier"}))
        for i in range(3):
            world.link("PurchaseOrderLine.line_of", world.obj("PurchaseOrderLine", {
                "bc_expected_receipt_date": f"2026-10-0{i + 1}",
                "ordered_quantity": ("100", "kg"), "received_quantity": ("40", "kg"),
                "description": f"Synthetic item {i}"}), order)
        late = {po(world, "Pending", "2026-10-01") for _ in range(n)}
        gone = po(world, "Pending", "2026-10-01", lines=(("10", "0"), ("7", "9")))
        world.rows["objects"][[r["object_id"] for r in world.rows["objects"]].index(gone)][
            "missing_from_source_since"] = "2026-10-05T00:00:00.000000Z"
        # values a later read replaced: a superseded receipt quantity that would conflict
        # and a superseded header word that would open a completed order
        closed = po(world, "Complete", "2026-10-01", lines=(("10", "10"),))
        for oid, prop, value in ((ids["conflict_a"], "received_quantity", "99"),
                                 (closed, "status", "Pending")):
            # the record's earlier version (its own handle), superseded by the current one
            later = "2026-10-02T00:00:00.000000Z"
            version = next(r for r in world.rows["object_versions"] if r["object_id"] == oid)
            earlier = version["l1_handle"].replace("@g1", "@g0")
            world.rows["object_versions"].append({**version, "l1_handle": earlier,
                                                  "digest": "d0", "superseded_at": later})
            version["recorded_at"] = later
            for row in [r for r in world.rows["object_values"] if r["object_id"] == oid]:
                world.rows["object_values"].append({
                    **row, "l1_handle": earlier, "digest": "d0", "superseded_at": later,
                    **({"value": value} if row["property"] == prop else {})})
                row["recorded_at"] = later
        self.coverage = ids["coverage"]
        return world, late | {gone}

    def inbox_rows(self, inbox: Path) -> tuple:
        with contextlib.closing(sqlite3.connect(inbox)) as conn:
            gaps = conn.execute("SELECT * FROM gaps ORDER BY gap_id").fetchall()
            cites = conn.execute("SELECT * FROM gap_cites ORDER BY 1, 2, 3").fetchall()
            runs = conn.execute("SELECT detector, status, reasons, found, not_evaluated "
                                "FROM detector_runs ORDER BY detector").fetchall()
            causes = conn.execute("SELECT * FROM unjudged_causes ORDER BY 1, 2, 3, 4").fetchall()
            summary = json.loads(conn.execute(
                "SELECT summary FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()[0])
        return gaps, cites, runs, causes, summary

    def test_chunked_reads_find_exactly_what_the_view_queries_found(self):
        """Values read object first in chunks and links read once per type in rowid ranges
        on several threads (``_Store.links``, any range size) find what one query each
        over the views found: the same inbox, a removed record's links left out."""
        for n in (1, 10, 60):
            world, late = self.world(n)
            for path in self.dir.glob("*.sqlite*"):
                path.unlink()
            world.build(self.store)
            moment = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
            with mock.patch.object(pe._Store, "values", single_query_values), \
                    mock.patch.object(pe._Store, "links", single_query_links), \
                    mock.patch.object(proactive_engine_quantities, "_quantity_rows",
                                      view_quantity_rows):
                pe.run(self.store, self.dir / "old.sqlite", AS_OF, coverage=self.coverage,
                       qc_link_types=(QC_LINK,), clock=lambda: moment)
            old = self.inbox_rows(self.dir / "old.sqlite")
            self.assertTrue(old[0])
            for chunk, threads in ((1, "1"), (3, "3"), (pe.VALUES_CHUNK, "16")):
                with self.subTest(n=n, chunk=chunk), \
                        mock.patch.object(pe, "VALUES_CHUNK", chunk), \
                        mock.patch.object(pe, "PARALLEL_LINK_ROWS", 0), \
                        mock.patch.dict(os.environ, {proactive_engine_config.SCAN_THREADS_ENV: threads}):
                    new_inbox = self.dir / f"new-{chunk}.sqlite"
                    pe.run(self.store, new_inbox, AS_OF, coverage=self.coverage,
                           qc_link_types=(QC_LINK,), clock=lambda: moment)
                    new = self.inbox_rows(new_inbox)
                    self.assertEqual(new[:4], old[:4])
                    self.assertEqual({k: v for k, v in new[4].items() if k != "seconds"},
                                     {k: v for k, v in old[4].items() if k != "seconds"})
            shown = {g["object_id"] for g in pe.read_inbox(
                self.dir / "old.sqlite", self.store, reader="plx:ops", may_read=everyone,
                held=ALL_MARKINGS, decide=admit, limit=500, include_off=True)["gaps"]}
            gone = max(late, key=lambda o: int(o.rsplit(":", 1)[1]))
            self.assertEqual(late - shown, {gone})   # every late order, never the removed one
            st = pe._Store(self.store)
            try:
                with mock.patch.object(pe, "VALUES_CHUNK", 2):
                    for types, props, objects in (
                            (("PurchaseOrder",), None, None),
                            (("PurchaseOrder", "PurchaseOrderLine"), ("status", "ordered_quantity"),
                             None),
                            (("PurchaseOrderLine",), ("ordered_quantity",), sorted(late)[:3]),
                            (("PurchaseOrder",), ("status",), [gone, "PurchaseOrder:fx:nowhere"]),
                            (("NoSuchType",), None, None)):
                        self.assertEqual(st.values(types, props, objects=objects),
                                         single_query_values(st, types, props, objects=objects))
                with mock.patch.object(pe, "PARALLEL_LINK_ROWS", 0):
                    ranged = pe._Store(self.store, threads=4)
                    try:
                        for types, tiers, held in (
                                (("PurchaseOrderLine.line_of",), None, True),
                                (("PurchaseOrderLine.line_of",), None, False),
                                (("SupplierConfirmation.confirms", "PurchaseOrderLine.line_of"),
                                 pe.CONFIRMING, True),
                                (("NoSuchType.link",), None, True)):
                            self.assertEqual(ranged.links(types, tiers, held=held),
                                             single_query_links(st, types, tiers, held=held))
                        self.assertEqual(ranged.scope(), [tuple(r) for r in st.conn.execute(
                            "SELECT o.audience_key, v.markings, count(*) FROM objects o "
                            "JOIN object_versions v ON v.object_id = o.object_id AND "
                            "v.superseded_at IS NULL WHERE NOT "
                            + ostore.removed_at_sql("o.object_id")
                            + " GROUP BY o.audience_key, v.markings")])
                    finally:
                        ranged.close()
                self.assertEqual(proactive_engine_quantities._quantity_rows(st), view_quantity_rows(st))
            finally:
                st.close()

    def test_removed_ends_are_read_per_type_never_the_whole_store(self):
        """The link reads looked every removed record of the store
        up first (one statement over every record, 55-77 s live, past the statement
        timeout). Each end type's removals are read alone now; a link whose end is of a
        type the schema does not declare is still marked gone, never read as held."""
        w = World()
        order = w.obj("PurchaseOrder", {"po_number": "SYNTH-PO-R"})
        line = w.obj("PurchaseOrderLine", {"description": "Synthetic"})
        kept = w.obj("PurchaseOrderLine", {"description": "Synthetic kept"})
        loose = w.obj("SynthLoose", {"name": "x"})
        supplier = w.obj("Supplier", {"name": "Synthetic Supplier"})
        w.link("PurchaseOrderLine.line_of", line, order)
        w.link("PurchaseOrderLine.line_of", kept, w.obj("PurchaseOrder", {"po_number": "K"}))
        w.link("SynthUndeclared.ties", loose, supplier)
        w.link("SynthUndeclared.ties", supplier, kept)
        for gone in (order, loose):
            w.rows["objects"][[r["object_id"] for r in w.rows["objects"]].index(gone)][
                "missing_from_source_since"] = "2026-10-05T00:00:00.000000Z"
        w.build(self.store)
        whole = []
        real = ostore.removed_ids_sql

        def counted(*a, **k):
            if not k.get("object_type_sql"):
                whole.append(1)
            return real(*a, **k)
        types = ("PurchaseOrderLine.line_of", "SynthUndeclared.ties")
        single = pe._Store(self.store)
        try:
            want = {held: single_query_links(single, types, held=held)
                    for held in (True, False)}
        finally:
            single.close()
        with mock.patch.object(ostore, "removed_ids_sql", counted):
            for threads, parallel in ((1, 10 ** 9), (4, 0)):
                with self.subTest(threads=threads), \
                        mock.patch.object(pe, "PARALLEL_LINK_ROWS", parallel):
                    st = pe._Store(self.store, threads=threads)
                    try:
                        for held in (True, False):
                            self.assertEqual(st.links(types, held=held), want[held])
                        self.assertEqual(
                            {(r[3], r[4]): r[7] for r in st.of_type("SynthUndeclared.ties")},
                            {(loose, supplier): True, (supplier, kept): False})
                        self.assertEqual(st.removed(["PurchaseOrder"]), {order})
                        self.assertEqual(st.removed(["SynthLoose", "Supplier"]), {loose})
                    finally:
                        st.close()
        self.assertEqual(whole, [])
        self.assertEqual([(l["from_object"], l["to_object"]) for l in want[True]],
                         [(kept, w.rows["links"][1]["to_object"]), (supplier, kept)])

    def test_no_detector_read_builds_a_transient_index(self):
        seeded()[0].build(self.store)
        st = pe._Store(self.store)
        try:
            self.assertEqual(st.conn.execute("PRAGMA automatic_index").fetchone()[0], 0)
        finally:
            st.close()


class HomeLateRecallTest(_pe.Engine):
    """Every late purchase order is on the home before any other gap kind, longest late
    first, and a firm Portal-BC twin lends its line dates to a Portal order that has none
    (live 2026-10-26: the home listed 7 of 23 late orders; the late ones that carried a
    supplier confirmation had medium priority and fell behind the 100 cap)."""

    def confirm(self, w: World, po_id: str, day: str) -> None:
        w.link("SupplierConfirmation.confirms",
               w.obj("SupplierConfirmation", {"delivery_date": day}), po_id)

    def bc_twin(self, w: World, portal: str, day: str, tier: str = "corroborated") -> str:
        bc = w.obj("PurchaseOrder", {"status": "Pending"})
        w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
            "bc_expected_receipt_date": day, "ordered_quantity": ("10", "ea")}), bc)
        w.link("PurchaseOrder.same_record_as", portal, bc, tier)
        return bc

    def test_late_orders_rank_before_higher_priority_gaps_whatever_the_cap(self):
        w = World()
        late = {}
        for days, number in ((5, "a"), (12, "b"), (1, "c")):   # confirmed: medium priority
            oid = po(w, "Pending", (AS_OF - timedelta(days=days)).isoformat())
            self.confirm(w, oid, (AS_OF - timedelta(days=days + 1)).isoformat())
            late[number] = oid
        for _ in range(6):   # at risk: high priority, not late
            oid = po(w, "B.Order", "2026-10-25")
            self.confirm(w, oid, "2026-11-02")
        self.run_engine(w)
        capped = self.read(limit=3)
        self.assertTrue(capped["more"])
        self.assertEqual([g["object_id"] for g in capped["gaps"]],
                         [late["b"], late["a"], late["c"]])
        self.assertEqual([g["days_late"] for g in capped["gaps"]], [12, 5, 1])
        every = self.read()["gaps"]
        self.assertEqual(len(every), 9)   # no other kind is hidden
        self.assertEqual({g["object_id"] for g in every[:3]}, set(late.values()))
        self.assertTrue(all("days_late" not in g["follow_up"] for g in every))
        self.assertTrue(all("days_late" not in g for g in every[3:]))

    def test_line_level_late_orders_never_crowd_out_a_late_purchase_order(self):
        # live: 330 orders held a past line date (kind late_order, high priority) beside 23
        # open late POs; the late PO is the one status definition's. A line-level item
        # is raised only on an order that definition calls late, and every late order
        # (days late) is listed before any line-level item
        w = World()
        for n in range(4):
            order = w.obj("PurchaseOrder", {"po_number": f"PO-{n}", "bc_status": "Open",
                                            "bc_fully_received": "false"})
            w.link("PurchaseOrder.ordered_from", order, w.obj("Supplier", {"name": "Acme"}))
            w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
                "bc_expected_receipt_date": "2026-09-01", "ordered_quantity": ("100", "kg"),
                "received_quantity": ("40", "kg", "received")}), order)
        late = po(w, "Pending", "2026-10-18")
        self.confirm(w, late, "2026-10-17")      # medium priority: behind every high gap
        self.run_engine(w)
        shown = self.read(limit=100)["gaps"]
        kinds = [g["kind"] for g in shown]
        first_line = kinds.index("late_order")
        self.assertTrue(all("days_late" in g for g in shown[:first_line]))
        self.assertTrue(all("days_late" not in g for g in shown[first_line:]))
        self.assertIn(late, [g["object_id"] for g in shown[:first_line]])
        self.assertEqual(next(g for g in shown if g["object_id"] == late)["days_late"], 1)

    def test_a_late_order_without_a_confirmation_ranks_with_the_late_ones(self):
        w = World()
        low = po(w, "Pending", "2026-10-18")                  # unconfirmed, 1 day late
        high = po(w, "Pending", "2026-10-01")                 # unconfirmed, 18 days late
        today = po(w, "Pending", AS_OF.isoformat())           # due today: not late
        self.run_engine(w)
        got = self.read()["gaps"]
        self.assertEqual([g["object_id"] for g in got], [high, low, today])
        self.assertNotIn("days_late", got[2])

    def test_a_firm_twin_lends_its_open_line_date_to_a_portal_order_with_none(self):
        w = World()
        portal = po(w, "Pending")                              # no date of its own
        bc = self.bc_twin(w, portal, "2026-10-09")
        self.run_engine(w)
        gaps = {g["object_id"]: g for g in self.read()["gaps"]}
        self.assertEqual(gaps[portal]["days_late"], 10)
        self.assertIn("2026-10-09", gaps[portal]["summary"])
        line_handle = handle(int(bc.rsplit(":", 1)[1]) + 1)   # the twin's own line
        self.assertIn(line_handle, gaps[portal]["citations"])   # cited as the twin's
        # one item per order: the twin's lateness is this order's,
        # listed once, never a second item for the same order
        self.assertNotIn(bc, gaps)
        # the link lends dates only: a Portal order stays closed whatever the twin says
        w2 = World()
        done = po(w2, "Cancel")
        self.bc_twin(w2, done, "2026-10-09")
        self.run_engine(w2)
        self.assertNotIn(done, {g["object_id"] for g in self.read()["gaps"]})

    def test_a_lead_link_lends_no_date(self):
        w = World()
        by_lead = po(w, "Pending")
        self.bc_twin(w, by_lead, "2026-10-09", tier="lead")
        summary = self.run_engine(w)
        gaps = self.read()["gaps"]
        self.assertNotIn(by_lead, {g["object_id"] for g in gaps
                                   if g["kind"] in ("late_po", "unconfirmed_po")})
        self.assertIn(("undated_po", by_lead), {(g["kind"], g["object_id"]) for g in gaps})
        self.assertEqual(
            summary["not_evaluated"]["late_or_unconfirmed_po"]["no_need_date"], 1)


class PassShareTest(_pe.Engine):
    """Each detector's share of a pass (``proactive_engine._share``): the tool-call rules run
    one after another on the run's thread, and before, each took its whole window in turn:
    the margin rules (900 s, 600 s) spent the 1,380 s the detectors had of a 1,500 s pass,
    so the orders at risk, the vendor terms and the cards started with 0 s on every served
    pass (2026-11-12, passes 39-43). The margin rules also run at most every
    ``DETECTOR_CADENCE_HOURS`` (``_not_due``)."""

    def rules(self, now: list, started: list, spend: set):
        """Each tool-call rule records the window it starts with (``st.left()``) and, when
        named in ``spend``, uses all of it (a fake clock)."""
        real = pe._event_steps

        def step(name, st):
            left = st.left()
            started.append((name, left))
            if name in spend:
                now[0] += left
            return pe._result([])

        def events(st, *args, **kwargs):
            steps = real(st, *args, **kwargs)
            for name in ("negative_margin", "margin_erosion", "vendor_terms_unapplied"):
                steps[name] = functools.partial(step, name, st)
            return steps
        return contextlib.ExitStack(), [
            mock.patch.object(pe, "_event_steps", events),
            mock.patch.object(pe, "_orders_at_risk",
                              lambda st, *_a, **_k: step("orders_at_risk", st)),
            mock.patch.object(pe, "_card_rules", lambda st, *_a, **_k: step("card_rules", st))]

    def pass_(self, world, ids, now, started, spend, **options):
        stack, patches = self.rules(now, started, spend)
        with stack:
            for patch in patches:
                stack.enter_context(patch)
            return self.run_engine(world, ids["coverage"], only=list(CALLING_RULES),
                                   call=lambda _tool, _arguments: {}, **options)

    def test_no_tool_call_rule_starts_with_nothing_however_slow_the_ones_before(self):
        world, ids = seeded()
        weights = {name: pe.DETECTOR_SECONDS[name] for name in CALLING_RULES}
        total = sum(weights.values())
        # every rule uses all the time it is given: each starts with its weight's part
        now, started = [0.0], []
        summary = self.pass_(world, ids, now, started, set(CALLING_RULES), cadence={},
                             budget=pe.Budget(1500, monotonic=lambda: now[0], reserve=120))
        self.assertEqual([name for name, _ in started], list(CALLING_RULES))
        for name, window in started:
            self.assertAlmostEqual(window, 1380 * weights[name] / total, places=6, msg=name)
            self.assertEqual(summary["detectors"][name], "complete", name)
        self.assertAlmostEqual(now[0], 1380, places=6)   # never past the detectors' end
        # time a rule leaves unused carries to the ones after it, never past their window
        now, quick = [0.0], []
        self.pass_(world, ids, now, quick, set(), cadence={},
                   budget=pe.Budget(1500, monotonic=lambda: now[0], reserve=120))
        for (name, slow), (_same, fast) in zip(started[1:], quick[1:]):
            self.assertGreater(fast, slow, name)
            self.assertLessEqual(fast, weights[name], name)
        # no budget: each its own whole window, as before
        now, free = [0.0], []
        self.pass_(world, ids, now, free, set(CALLING_RULES), cadence={})
        self.assertEqual(set(dict(free)), set(weights))
        for name, window in free:   # (the real clock moves meanwhile)
            self.assertAlmostEqual(window, weights[name], delta=5, msg=name)

    def test_the_margin_rules_run_at_most_every_twelve_hours_by_the_same_code(self):
        world, ids = seeded()
        first = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
        margins = ["margin_erosion", "negative_margin"]

        def ran(at, **patches):
            now, started = [0.0], []
            with contextlib.ExitStack() as stack:
                for name, value in patches.items():
                    stack.enter_context(mock.patch.object(pe, name, value))
                summary = self.pass_(world, ids, now, started, set(), at=at,
                                     cadence=pe.DETECTOR_CADENCE_HOURS)
            return summary, sorted(n for n, _ in started if n in margins)

        def margin_runs():
            with closing(sqlite3.connect(self.inbox)) as conn:
                return dict(conn.execute(
                    "SELECT detector, run_at FROM detector_runs WHERE detector IN "
                    "('negative_margin', 'margin_erosion')"))
        summary, started = ran(first)
        self.assertEqual(started, margins)
        self.assertNotIn("not_due", summary)
        held = margin_runs()
        self.assertEqual(set(held), set(margins))
        # an hour on: not due, left exactly as they were; the other rules still run
        summary, started = ran(first + timedelta(hours=1))
        self.assertEqual((started, summary["not_due"]), ([], margins))
        self.assertFalse(set(margins) & set(summary["detectors"]))
        self.assertEqual(summary["detectors"]["orders_at_risk"], "complete")
        self.assertEqual(margin_runs(), held)
        # other code deployed since: due at once
        summary, started = ran(first + timedelta(hours=2), engine_code=lambda: "other-code")
        self.assertEqual(started, margins)
        # back on the deployed code: the last run was by the other code, so due again
        self.assertEqual(ran(first + timedelta(hours=3))[1], margins)
        # twelve hours after the last start: not before, then due again
        self.assertEqual(ran(first + timedelta(hours=14, minutes=59))[1], [])
        self.assertEqual(ran(first + timedelta(hours=15))[1], margins)
        # a record that cannot be read, or one dated ahead of the run: due (never skipped
        # unseen)
        for bad in ("not json", json.dumps({"at": "2027-01-01T00:00:00+00:00",
                                            "code": pe.engine_code()}),
                    json.dumps({"at": None, "code": pe.engine_code()})):
            with closing(sqlite3.connect(self.inbox)) as conn, conn:
                conn.execute("UPDATE meta SET value = ? WHERE key = ?",
                             (bad, "cadence:negative_margin"))
            summary, started = ran(first + timedelta(hours=15, minutes=30))
            self.assertIn("negative_margin", started, bad)
        # an explicit empty cadence runs them every pass
        now, started = [0.0], []
        self.pass_(world, ids, now, started, set(), at=first + timedelta(hours=15, minutes=31),
                   cadence={})
        self.assertEqual(sorted(n for n, _ in started if n in margins), margins)

    def test_a_rule_due_on_its_cadence_gets_its_whole_window_not_a_part_of_the_pass(self):
        world, ids = seeded()
        weights = {name: pe.DETECTOR_SECONDS[name] for name in CALLING_RULES}
        margins = ("negative_margin", "margin_erosion")
        now, started = [0.0], []
        self.pass_(world, ids, now, started, {"negative_margin"},
                   cadence=pe.DETECTOR_CADENCE_HOURS,
                   budget=pe.Budget(1500, monotonic=lambda: now[0], reserve=120))
        windows = dict(started)
        # due (never run): the whole window, never more (a part of the pass by weight would
        # be less); the second only what the first left of the deadline
        self.assertAlmostEqual(windows["negative_margin"], weights["negative_margin"], places=6)
        self.assertAlmostEqual(windows["margin_erosion"], 1380 - weights["negative_margin"],
                               places=6)
        self.assertLess(windows["margin_erosion"], weights["margin_erosion"])
        # the others still split what is left by weight, none starting with nothing
        for name in CALLING_RULES:
            if name not in margins:
                self.assertGreater(windows[name], 0, name)
                self.assertLessEqual(windows[name], weights[name], name)
        # a window is never more than the run's deadline leaves
        now, short = [0.0], []
        self.pass_(world, ids, now, short, set(), cadence=pe.DETECTOR_CADENCE_HOURS,
                   budget=pe.Budget(300, monotonic=lambda: now[0], reserve=0))
        for name, window in short:
            if name in margins:
                self.assertLessEqual(window, 300, name)

    def test_a_cadenced_rule_that_was_not_configured_or_failed_is_due_again_next_pass(self):
        world, ids = seeded()
        first = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
        for status in ("not_configured", "failed"):
            with self.subTest(status=status):
                self.setUp()
                real = pe._event_steps

                def events(st, *args, _status=status, **kwargs):
                    steps = real(st, *args, **kwargs)
                    steps["negative_margin"] = lambda: pe._result([], status=_status)
                    return steps
                with mock.patch.object(pe, "_event_steps", events):
                    summary = self.run_engine(world, ids["coverage"], only=["negative_margin"],
                                              call=lambda _t, _a: {}, at=first,
                                              cadence=pe.DETECTOR_CADENCE_HOURS)
                self.assertEqual(summary["detectors"]["negative_margin"], status)
                with closing(sqlite3.connect(self.inbox)) as conn:
                    held = conn.execute("SELECT 1 FROM meta WHERE key = ?",
                                        ("cadence:negative_margin",)).fetchall()
                self.assertEqual(held, [])
                summary = self.run_engine(world, ids["coverage"], only=["negative_margin"],
                                          call=lambda _t, _a: {}, at=first + timedelta(hours=1),
                                          cadence=pe.DETECTOR_CADENCE_HOURS)
                self.assertNotIn("not_due", summary)

    def test_a_run_thread_statement_past_its_window_is_cancelled(self):
        """Postgres has no progress handler: a run-thread rule's statement still running
        past its window is cancelled (``_Watchdog``), the rule ``truncated``, the next
        rule keeps its share. Simulated on SQLite by removing the progress handler."""
        world, ids = seeded()

        def endless(st, *_a, **_k):
            st.conn.set_progress_handler(None, 0)   # as on Postgres
            st.conn.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n "
                            "WHERE i < 400000000) SELECT count(*) FROM n").fetchone()
            return pe._result([])
        started = time.process_time()   # the statement runs in this process: CPU time
        with mock.patch.object(pe, "_orders_at_risk", endless), \
                mock.patch.object(pe, "RUN_THREAD_GRACE_SECONDS", 0.2):
            summary = self.run_engine(world, ids["coverage"],
                                      only=["orders_at_risk", "card_rules"],
                                      call=lambda _tool, _arguments: {}, budget=pe.Budget(600),
                                      detector_seconds={"orders_at_risk": 0.3})
        self.assertLess(time.process_time() - started, 30)
        self.assertEqual(summary["detectors"]["orders_at_risk"], "truncated")
        self.assertEqual(summary["detector_reasons"]["orders_at_risk"], [pe.TIME_BUDGET])
        self.assertLess(summary["seconds"]["orders_at_risk"], 10)
        self.assertNotEqual(summary["detectors"]["card_rules"], "failed")


class BusinessDayTest(_pe.Engine):
    """The engine's day is the business day in America/New_York (decided 2026-10-28),
    never the UTC day: from 20:00 (EDT) or 19:00 (EST) to midnight ET the UTC day is one
    ahead. Each moment is given in UTC, so the expected ET day is not derived by the code
    under test. Both daylight-saving changes of 2026 (Mar 8, Nov 1) are covered."""

    # (due day, the run's moment in UTC, its ET wall time, days late or None: not late)
    CASES = (
        ("2026-10-19", "2026-10-20T01:00:00+00:00", "21:00 EDT Oct 19", None),
        ("2026-10-19", "2026-10-20T04:30:00+00:00", "00:30 EDT Oct 20", 1),
        # spring forward (Mar 8 02:00 EST -> 03:00 EDT)
        ("2026-03-07", "2026-03-08T02:00:00+00:00", "21:00 EST Mar 7", None),
        ("2026-03-07", "2026-03-08T05:30:00+00:00", "00:30 EST Mar 8", 1),
        ("2026-03-08", "2026-03-09T01:00:00+00:00", "21:00 EDT Mar 8", None),
        ("2026-03-08", "2026-03-09T04:30:00+00:00", "00:30 EDT Mar 9", 1),
        # fall back (Nov 1 02:00 EDT -> 01:00 EST)
        ("2026-10-31", "2026-11-01T01:00:00+00:00", "21:00 EDT Oct 31", None),
        ("2026-10-31", "2026-11-01T04:30:00+00:00", "00:30 EDT Nov 1", 1),
        ("2026-11-01", "2026-11-02T02:00:00+00:00", "21:00 EST Nov 1", None),
        ("2026-11-01", "2026-11-02T05:30:00+00:00", "00:30 EST Nov 2", 1),
    )

    def test_a_po_due_today_et_is_late_only_once_the_et_day_has_ended(self):
        root = self.dir
        for due, moment, wall, days_late in self.CASES:
            with self.subTest(due=due, at=wall):
                self.dir = Path(tempfile.mkdtemp(dir=root))
                self.store = self.dir / "store.sqlite"
                self.inbox = pe.inbox_path(self.store)
                w = World()
                oid = po(w, "Pending", due)
                w.build(self.store)
                at = datetime.fromisoformat(moment)
                pe.run(self.store, self.inbox, qc_link_types=(QC_LINK,), clock=lambda: at)
                found = self.read()
                today = date.fromisoformat(due) + timedelta(days=days_late or 0)
                self.assertEqual(found["as_of"], today.isoformat())
                if days_late is None:   # the evening's UTC day is already the next day
                    self.assertEqual(at.date(), today + timedelta(days=1))
                (gap,) = [g for g in found["gaps"] if g["object_id"] == oid]
                self.assertEqual(gap.get("days_late"), days_late)
                # the host's hook judges on the same day
                hook = pe.after_build(self.store, clock=lambda: at)
                self.assertEqual(hook["state"], "ran")
                self.assertEqual(self.read()["as_of"], today.isoformat())

    def test_the_et_day_needs_an_aware_moment_and_a_given_day_must_be_a_date(self):
        w = World()
        po(w, "Pending", "2026-10-19")
        w.build(self.store)
        naive = datetime(2026, 10, 20, 1, 0)
        with self.assertRaisesRegex(ValueError, "aware"):
            pe.run(self.store, self.inbox, clock=lambda: naive)
        with self.assertRaisesRegex(ValueError, "aware"):
            po_status.business_today(naive)
        for wrong in ("2026-10-19", datetime(2026, 10, 19, tzinfo=timezone.utc)):
            with self.assertRaises(TypeError):
                pe.run(self.store, self.inbox, wrong)
        self.assertEqual(pe.after_build(self.store, clock=lambda: naive),
                         {"state": "failed", "error": "ValueError"})
        self.assertFalse(self.inbox.exists())   # nothing folded on a refused day
        # the ET day boundary itself, in both offsets
        utc = timezone.utc
        for moment, day in ((datetime(2026, 7, 16, 3, 59, 59, tzinfo=utc), "2026-07-15"),
                            (datetime(2026, 7, 16, 4, 0, tzinfo=utc), "2026-07-16"),
                            (datetime(2026, 1, 16, 4, 59, 59, tzinfo=utc), "2026-01-15"),
                            (datetime(2026, 1, 16, 5, 0, tzinfo=utc), "2026-01-16")):
            self.assertEqual(po_status.business_today(moment).isoformat(), day, moment)


class ConsoleDraftTest(_pe.LateOrderWorld):
    """The console's propose-follow-up with a late order's draft: the client's draft is only
    a request; the server words it from the store for the reader and records it on the case
    as ``workbench.draft.record``, read back from the case store, never sent."""

    class Access(FakeAccess):
        def __init__(self, audiences, held=ALL_MARKINGS, refused=()) -> None:
            super().__init__(audiences, held)
            self.refused = set(refused)

        def decide(self, handles):
            return {h: {"withheld": "item_refused"} if h in self.refused else None
                    for h in handles}

    def console(self, w: World, access=None):
        self.fresh()
        self.run_engine(w)
        resolver = FakeResolver()
        for row in w.rows["object_versions"]:
            resolver.add(row["l1_handle"], {"row": {"id": row["object_id"]}}, {"ops"})
        self.cases = CaseStore(self.dir / f"cases-{time.monotonic_ns()}.db",
                               citation_resolver=resolver)
        self.path = ActionPath(self.dir / f"actions-{time.monotonic_ns()}.sqlite", self.cases)
        session = fixture_session("plx", OPS.principals, channel=CHANNEL_CONSOLE,
                                  subject_email="employee@isolated-fixture.example")

        def card(arguments, actor):
            oid = arguments["object_id"]
            return {"object": {"object_id": oid, "object_type": oid.split(":", 1)[0]},
                    "values": []}
        tools = {name: {"handler": card if name == "plx.object"
                        else (lambda arguments, actor: {})}
                 for name in ("plx.find", "plx.object", "plx.around", "plx.timeline")}
        return objects.ObjectWorkspace(
            tools, OPS, access=access or self.Access({"api:a"}),
            store_status=FakeStatus(self.store),
            follow_ups=objects.FollowUps(self.path, session))

    def gap(self, **read) -> dict:
        return next(g for g in self.read(**read)["gaps"] if g["kind"] == "late_order")

    def test_a_tampered_client_draft_is_reworded_and_the_case_holds_the_servers(self):
        w, order, _late = self.world()
        workspace = self.console(w)
        follow_up = dict(self.gap()["follow_up"])
        server = follow_up["draft"]
        tampered = {**server, "text": server["text"].replace("received 40 kg", "received 0 kg")
                    + "Wire the deposit to SYNTH-ACCOUNT today.\n"}
        answer = workspace.follow_up({**follow_up, "draft": tampered})
        self.assertEqual(answer["proposal"]["draft"]["text"], server["text"])
        # Nor do the client's answerers word it: a restricted reader's "To:" is the gap's.
        number = handle(int(order.rsplit(":", 1)[1]))
        supplier = handle(int(order.rsplit(":", 1)[1]) + 1)
        other = self.console(w, self.Access({"api:a"}, refused={number, supplier}))
        named = other.follow_up({**follow_up, "who_can_answer": ["SYNTH-ROLE"],
                                 "draft": tampered})["proposal"]["draft"]
        self.assertNotIn("SYNTH-ROLE", named["text"])
        self.assertEqual(named["to"]["kind"], "answerer_role")
        self.assertIn(follow_up["who_can_answer"][0], named["text"])
        workspace = self.console(w)
        answer = workspace.follow_up({**follow_up, "draft": tampered})
        draft = answer["proposal"]["draft"]
        self.assert_worded(draft)
        self.assertEqual(draft["text"], server["text"])
        self.assertIn("received 40 kg", draft["text"])
        self.assertIs(draft["reworded"], True)
        self.assertEqual((draft["recorded"], draft["read_back"]), ("recorded", "verified"))
        self.assertNotIn("SYNTH-ACCOUNT", json.dumps(answer))
        self.assertEqual((answer["proposal"]["executed"], answer["proposal"]["approved"]),
                         (False, False))
        case_id = answer["proposal"]["case_id"]
        # Read back from the case store: one workbench.draft.record, the server's words.
        listing = workspace.case_records(case_id)["case"]
        [stored] = listing["drafts"]
        self.assertEqual((stored["record_id"], stored["statement"], stored["superseded"]),
                         (draft["record_id"], server["text"], False))
        self.assertEqual({c["l1_handle"] for c in stored["citations"]}, set(server["citations"]))
        self.assertTrue(all(c["status"] == "resolved" for c in stored["citations"]))
        self.assertEqual(draft["case_record"]["status"], stored["status"])
        executed = [a for a in self.path.internal_actions(OPS, case_id)
                    if a["action_id"] == DRAFT_ACTION and a["outcome"] == "executed"]
        self.assertEqual(len(executed), 1)
        self.assertNotIn("SYNTH-ACCOUNT", json.dumps(self.cases.get(OPS, case_id)))
        # The untampered draft again: the same words, recorded once.
        again = workspace.follow_up(follow_up)["proposal"]
        self.assertEqual((again["case_id"], again["draft"]["record_id"],
                          again["draft"]["recorded"], again["draft"]["reworded"]),
                         (case_id, draft["record_id"], "exists", False))
        self.assertEqual(len(workspace.case_records(case_id)["case"]["drafts"]), 1)

    def test_cardinality_the_recorded_draft_for_n_late_lines_is_the_worded_one(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w, _order, late = self.world(n_late=0)
                order = w.rows["objects"][0]["object_id"]
                for i in range(n):
                    line = w.obj("PurchaseOrderLine", {
                        "bc_expected_receipt_date": f"2026-09-{1 + i % 28:02d}",
                        "ordered_quantity": ("9", "kg"), "received_quantity": ("1", "kg"),
                        "description": f"Item {i}"})
                    w.link("PurchaseOrderLine.line_of", line, order)
                workspace = self.console(w)
                start = time.process_time()
                proposal = workspace.follow_up(dict(self.gap()["follow_up"]))["proposal"]
                self.assertLess(time.process_time() - start, CARDINALITY_SECONDS)
                draft = proposal["draft"]
                self.assert_worded(draft)
                self.assertEqual((draft["lines_late"], draft["lines_listed"]),
                                 (n, min(n, proactive_engine_config.MAX_DRAFT_LINES)))
                [stored] = workspace.case_records(proposal["case_id"])["case"]["drafts"]
                self.assertEqual((stored["record_id"], stored["statement"]),
                                 (draft["record_id"], draft["text"]))
                self.assertEqual(len(stored["citations"]), len(draft["citations"]))

    def test_a_restricted_reader_gets_a_draft_of_only_what_they_may_open(self):
        w, order, late = self.world()
        number = handle(int(order.rsplit(":", 1)[1]))
        supplier = handle(int(order.rsplit(":", 1)[1]) + 1)
        workspace = self.console(w, self.Access({"api:a"}, refused={number, supplier}))
        follow_up = dict(self.gap()["follow_up"])
        answer = workspace.follow_up(follow_up)
        draft = answer["proposal"]["draft"]
        self.assert_worded(draft)
        self.assertEqual(draft["unresolved"], ["order_number:withheld", "supplier:withheld"])
        self.assertEqual(draft["to"]["kind"], "answerer_role")
        self.assertIs(draft["reworded"], True)
        listing = workspace.case_records(answer["proposal"]["case_id"])
        for shown in (answer, listing):
            text = json.dumps(shown)
            for secret in ("PO-77", "Acme Resins", number, supplier):
                self.assertNotIn(secret, text)
        # A reader who may open no late line whole gets no draft and nothing is written.
        lines = {handle(int(line.rsplit(":", 1)[1])) for line in late}
        hidden = World()
        hidden.rows = {k: [dict(r) for r in v] for k, v in w.rows.items()}
        for table in ("object_versions", "object_values"):
            for row in hidden.rows[table]:
                if row["l1_handle"] in lines:
                    row["markings"] = '["finance"]'
        for name, world, access in (
                ("lines refused", w, self.Access({"api:a"}, refused=lines)),
                ("audience not granted", w, self.Access(set())),
                ("line markings not held", hidden, self.Access({"api:a"}, held={"pii"}))):
            with self.subTest(name):
                workspace = self.console(world, access)
                with self.assertRaisesRegex(ValueError, "follow_up_draft_not_derived"):
                    workspace.follow_up(follow_up)
                self.assertEqual(self.cases.list(OPS), [])
        served = self.console(w)
        workspace = objects.ObjectWorkspace(served.tools, OPS, follow_ups=served.follow_ups)
        with self.assertRaisesRegex(LookupError, "follow_up_draft_not_served"):
            workspace.follow_up(follow_up)   # an API-only reader: no store, no grant

    def test_a_draft_that_is_sent_or_malformed_is_refused_before_anything_is_written(self):
        w, _order, _late = self.world()
        workspace = self.console(w)
        follow_up = dict(self.gap()["follow_up"])
        good = follow_up["draft"]
        bad = {"sent": {**good, "sent": True}, "status": {**good, "status": "sent"},
               "extra field": {**good, "send_to": "x"}, "no label": {**good, "text": "Hi"},
               "uncited fact": {**good, "facts": [*good["facts"], {"citation": handle(999)}]},
               "foreign citation": {**good, "citations": ["https://example.invalid/x"]},
               "not a dict": "Draft text"}
        for name, draft in bad.items():
            with self.subTest(name):
                with self.assertRaisesRegex(ValueError, "follow_up_draft_invalid"):
                    workspace.follow_up({**follow_up, "draft": draft})
        self.assertEqual(self.cases.list(OPS), [])
        # Without a draft the proposal records no draft at all.
        answer = workspace.follow_up({k: v for k, v in follow_up.items() if k != "draft"})
        self.assertNotIn("draft", answer["proposal"])
        self.assertEqual(workspace.case_records(answer["proposal"]["case_id"])["case"]["drafts"],
                         [])


class PostgresStoreTest(_pe.Engine):
    """The detectors read the Postgres ontology (``PLX_ONTOLOGY_PG``) as they read the
    SQLite generation: the same gaps, no detector failed on SQL only SQLite takes (an
    integer ``WHERE 1``, the packed value layouts Postgres does not hold)."""

    def postgres(self, world: World) -> str:
        """A throwaway Postgres database holding ``world``'s store (built here)."""
        from operating.pg import ontology_load
        from operating.tests import pg_testdb
        name, dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, name)
        import psycopg
        from operating.pg.migrate import MIGRATIONS_DIR
        with psycopg.connect(dsn, autocommit=True) as pg:   # every ontology migration: the
            for migration in sorted(MIGRATIONS_DIR.glob("00*.sql")):   # load fills the word tables
                if migration.name >= "0006":
                    pg.execute(migration.read_text(encoding="utf-8"))
        world.build(self.store)
        ontology_load.load(self.store, dsn, workers=1, replace=True, log=io.StringIO())
        return dsn

    def test_a_types_slice_is_read_alone_on_postgres_and_equals_the_whole_read(self):
        """Every delta layer is a new store state on Postgres: the hop checks read the
        slices of the types they ask by the type index, never the whole link table, and
        get exactly what the whole-table read holds for those types (none for a type
        with no rows)."""
        from operating import ontology_tools, ontology_tools_traverse as tt
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        world, _ids = seeded()
        conn = PgCompatConnection(self.postgres(world))
        self.addCleanup(conn.close)
        tools = tt.TraverseTools(ontology_tools.OntologyTools(
            PgStoreReader(conn), ontology_tools.ReadPolicy(tenant="plx", audiences={})))
        for kind in ("object", "link", "event"):
            whole = tools._slices(kind)
            if kind != "event":
                self.assertTrue(whole, kind)
            with mock.patch.object(tt.TraverseTools, "_slices",
                                   side_effect=AssertionError("whole-table read")):
                for name in whole:
                    self.assertEqual(tools._slices_of(kind, [name, "Nothing.no_such"]),
                                     {name: whole[name]})

    def test_a_value_read_on_postgres_joins_the_value_tables_and_reads_what_sqlite_reads(self):
        """AL-04: ``_Store.values`` on Postgres reads the value tables with the properties'
        fields named first (through the ``object_values`` view the planner looked them up
        again for every value source: 89 s for 32k assembly orders on the served store, past
        the detector's window) and a whole type in one statement; the records, properties,
        rows and their order are those the SQLite generation gives, for a whole type with
        and without properties, named records, and a property no record holds."""
        from operating.pg.sqlite_compat import PgCompatConnection
        world, ids = seeded()
        world.build(self.store)
        dsn = self.postgres(world)
        lite = pe._Store(self.store, threads=1)
        self.addCleanup(lite.close)
        with mock.patch.object(pe, "_connect", lambda _path, _file_only=False: PgCompatConnection(dsn)):
            pg = pe._Store(self.store, threads=1)
        self.addCleanup(pg.close)
        self.assertTrue(pg.pg)
        self.assertFalse(lite.pg)
        statements: list[str] = []
        real = PgCompatConnection.execute

        def execute(conn, sql, *args, **kwargs):
            statements.append(sql)
            return real(conn, sql, *args, **kwargs)
        named = sorted(ids[k] for k in ("unconfirmed", "overdue", "late_with_lead"))
        cases = [(["PurchaseOrder"], ("status", "expected_date", "fm_status"), None),
                 (["PurchaseOrder", "SupplierConfirmation"], None, None),
                 (["PurchaseOrder"], None, named),
                 (["PurchaseOrder"], ("status",), named),
                 (["PurchaseOrder"], ("no_such_property",), None)]
        for types, props, objects in cases:
            with self.subTest(types=types, props=props, objects=objects):
                want = lite.values(types, props, objects=objects)
                statements.clear()
                with mock.patch.object(PgCompatConnection, "execute", execute):
                    got = pg.values(types, props, objects=objects)
                self.assertEqual(got, want)
                self.assertEqual(list(got), list(want))
                self.assertEqual([list(v["props"]) for v in got.values()],
                                 [list(v["props"]) for v in want.values()])
                self.assertFalse([q for q in statements if "object_values" in q], statements)
                if props == ("status", "expected_date", "fm_status"):
                    self.assertTrue(want)
                    # the listing, the fields, then the whole type in one statement
                    self.assertEqual(len(statements), 3, statements)

    def test_a_scoped_quantity_read_on_postgres_reads_the_scope_by_key(self):
        """An incremental pass's quantity read on Postgres returns the rows the SQLite
        generation's scoped read gives (the scope's current quantity cells, an unknown id
        none) and reaches the value cells by key from the scope: one flat join of its nine
        relations passed join_collapse_limit and the planner joined every quantity cell of
        the store first (60-107 s, ~10 GB of temp per worker on the live store). With
        sequential scans priced out, that plan still scans value_cells (it has no index
        by kind); the keyed read takes it by its primary key."""
        import psycopg
        from operating.pg.sqlite_compat import PgCompatConnection
        world, ids = seeded()
        world.build(self.store)
        dsn = self.postgres(world)
        scope = sorted(v for v in ids.values() if isinstance(v, str)) + ["PurchaseOrder:no.such:{}"]
        lite = pe._Store(self.store, threads=1, scope=scope)
        self.addCleanup(lite.close)
        with mock.patch.object(pe, "_connect",
                               lambda _path, _file_only=False: PgCompatConnection(dsn)):
            pg = pe._Store(self.store, threads=1, scope=scope)
        self.addCleanup(pg.close)
        self.assertTrue(pg.pg and pg.scoped)
        want = proactive_engine_quantities._quantity_rows(lite)
        self.assertTrue(want)
        self.assertIn(ids["conflict_a"], {row["object_id"] for row in want})
        from operating.pg import sqlite_compat
        ran: list = []   # (connection, Postgres text, its parameters)
        real = PgCompatConnection._run

        def run(conn, cur, text, params):
            ran.append((conn, text, params))
            return real(conn, cur, text, params)
        # a scope as long as a live pass's (500 ids): read through the session's key set
        with mock.patch.object(PgCompatConnection, "_run", run), \
                mock.patch.object(sqlite_compat, "LONG_LIST_CHARS", 1):
            got = proactive_engine_quantities._quantity_rows(pg)
        self.assertEqual(got, want)
        reads = [r for r in ran if "value_cells" in r[1]]
        self.assertTrue(reads)
        self.assertTrue(any("sq_keys" in text for _c, text, _p in reads), reads)
        for conn, text, params in reads:
            with conn._conn.cursor() as cur:
                cur.execute("SET enable_seqscan = off")
                cur.execute("EXPLAIN (FORMAT JSON) " + text, params)
                plan = cur.fetchone()[0]
                cur.execute("RESET enable_seqscan")
            cells, stack = [], [plan[0]["Plan"]]
            while stack:
                node = stack.pop()
                stack.extend(node.get("Plans", ()))
                if node.get("Relation Name") == "value_cells":
                    cells.append(node)
            self.assertTrue(cells, plan)
            for node in cells:   # each cell read by the scope's value sources, never whole
                self.assertIn("src", node.get("Index Cond", ""), node)

    def test_every_detector_finds_on_postgres_what_it_finds_on_sqlite(self):
        from operating.pg.sqlite_compat import PgCompatConnection
        world, ids = seeded()
        lite = self.run_engine(world, ids["coverage"])
        expected = sorted((g["kind"], g["object_id"]) for g in self.read()["gaps"])
        dsn = self.postgres(world)
        self.inbox = self.dir / "pg-inbox.sqlite"
        opened: list = []

        def connect(_path, _file_only=False):
            opened.append(PgCompatConnection(dsn))
            return opened[-1]
        # rowid ranges side by side even on a small store: the ranged reads run too
        with mock.patch.object(pe, "_connect", connect), \
                mock.patch.dict(os.environ, {"PLX_PG_DSN": dsn}), \
                mock.patch.object(pe, "PARALLEL_LINK_ROWS", 1):
            summary = pe.run(self.store, self.inbox, AS_OF, coverage=ids["coverage"],
                             qc_link_types=(QC_LINK,), on_hand_sources=(ON_HAND,),
                             people=people(), threads=2,
                             clock=lambda: datetime(2026, 10, 19, 6, tzinfo=timezone.utc))
        self.assertTrue(opened)   # read from Postgres, not the SQLite file
        self.assertEqual({d: s for d, s in summary["detectors"].items() if s == "failed"},
                         {}, summary["detector_reasons"])
        self.assertEqual(summary["detectors"], lite["detectors"])
        # read as the host serving it reads (mode 1 and Postgres alone): the gaps of a run
        # over Postgres are current, none held back as of older data (in mode 1 they were all
        # held back: the inbox compared the run's Postgres stamp with the SQLite digest)
        for mode in ("1", "only"):
            with mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": mode, "PLX_PG_DSN": dsn}):
                got = self.read()
            self.assertEqual(got["refreshing"], [], mode)
            self.assertEqual(sorted((g["kind"], g["object_id"]) for g in got["gaps"]),
                             expected, mode)
        self.assertIn(("conflicting_quantity", min(ids["conflict_a"], ids["conflict_b"])),
                      expected)


class ExclusionReachesTheEngine(unittest.TestCase):
    """A pass started without the exclusion in its environment (outside the units) reads it
    from the passes' env file, in the engine and in every pool worker; without one it stays
    blocked, saying why."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state / "projection").mkdir()
        self.store = self.state / "projection" / "ontology.sqlite"
        self.env = self.state / "proactive.env"
        self.env.write_text(f"PLX_EXCLUDED_PEOPLE_OIDS={EXCLUDED_OID}\n", encoding="utf-8")
        os.chmod(self.env, 0o600)

    def environ(self, **extra: str):
        base = {k: v for k, v in os.environ.items() if not k.startswith("PLX_EXCLU")}
        return mock.patch.dict(os.environ, {**base, **extra}, clear=True)

    def test_engine_and_workers_get_the_exclusion_from_the_env_file(self) -> None:
        from operating import proactive_pool
        config = mock.Mock(tenant="plx", principal="svc:engine", mirror_audience="m.json",
                           capture_audience=None)
        with self.environ(PLX_EXCLUSION_ENV_FILE=str(self.env)):
            self.assertIsNone(pe._people(self.store).unavailable)
            spec = proactive_pool.WorkerSpec.from_host_config(config, self.state, self.store)
        self.assertEqual(dict(spec.environ), {"PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED_OID})
        with self.environ(PLX_EXCLUSION_ENV_FILE=str(self.state / "absent.env")):
            guard = pe._people(self.store)
            spec = proactive_pool.WorkerSpec.from_host_config(config, self.state, self.store)
        self.assertEqual(guard.unavailable, "excluded_people_oids_absent")
        self.assertEqual(spec.environ, ())

    def test_the_worker_spec_reads_only_the_exclusion_variables(self) -> None:
        """Building a worker's spec reads the value of no variable but the exclusion's: a
        Graph or BC credential in the environment is neither read nor carried (the
        verifier's env guard counts every read of an undeclared name)."""
        from operating import proactive_pool
        from tests.test_live_readers_filter import GuardedEnv, Undeclared
        config = mock.Mock(tenant="plx", principal="svc:engine", mirror_audience="m.json",
                           capture_audience=None)
        env = GuardedEnv({"PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED_OID,
                          "MICROSOFT_GRAPH_CLIENT_ID": "graph-client",
                          "SHAREPOINT_DRIVE_ID": "drive", "BC_TENANT_ID": "bc-tenant",
                          "MICROSOFT_GRAPH_CLIENT_SECRET": "hidden"},
                         hidden={"MICROSOFT_GRAPH_CLIENT_SECRET"})
        try:
            spec = proactive_pool.WorkerSpec.from_host_config(config, self.state, self.store,
                                                              env)
        except Undeclared as read:
            self.fail(f"read the hidden {read.args[0]}")
        self.assertEqual(dict(spec.environ), {"PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED_OID})
        self.assertEqual(env.read, {"PLX_EXCLUDED_PEOPLE_OIDS"})

    def test_vendor_rule_blocked_without_the_exclusion_says_why(self) -> None:
        from operating import proactive_events as ev
        found = ev.vendor_no_reply(None, AS_OF, gap=pe._gap, cites=pe._cites,
                                   result=pe._result, people=people(unavailable=True),
                                   orders={})
        self.assertEqual((found["status"], found["reasons"]),
                         ("blocked", ["people_exclusion_unavailable",
                                      "excluded_people_oids_absent"]))


class StatementTimeoutTests(unittest.TestCase):
    def test_detector_statement_limit_is_ten_minutes_unless_set(self):
        import os
        from unittest import mock
        from operating import proactive_engine as engine
        from operating import proactive_engine_budget
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLX_PROACTIVE_STATEMENT_SECONDS", None)
            self.assertEqual(proactive_engine_budget.statement_timeout_ms(), 600_000)
        for value, want in (("90", 90_000), ("0", 600_000), ("-5", 600_000), ("x", 600_000)):
            with mock.patch.dict(os.environ, {"PLX_PROACTIVE_STATEMENT_SECONDS": value}):
                self.assertEqual(proactive_engine_budget.statement_timeout_ms(), want)



if __name__ == "__main__":
    import unittest
    unittest.main()
