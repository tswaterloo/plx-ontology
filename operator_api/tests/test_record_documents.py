"""The documents section of a record's page (``operator_api.record_documents``) over the
real ``business_documents`` aggregate on a synthetic store, read through a reader's view:
each record type's lists, the rows narrowed to the record (a customer's twins, never
another customer's), names only as the reader reads them, finance-marked price sheets
withheld from a reader without finance, the people exclusion failing closed, an unknown
type with no section, and N=1/10/60 issues with no row lost or doubled.

SYNTHETIC fixtures only (``operating.tests.test_business_documents`` worlds); evidence
label: isolated-fixture.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import tempfile
import time
import unittest
from pathlib import Path

from operating import business_documents as bd
from operating import document_tables as dt
from operating.tests.test_business_documents import (AS_OF, EVERY, Fixture, Guard,
                                                     forecast_world, sheet)
from operator_api import record_documents as rd
from tools.timing_policy import budget


class ReaderView:
    """A reader's view of the store: ``plx.aggregate`` is the real business_documents
    mode under this reader's grants; ``plx.object`` gives a record's display only when the
    reader may read it (else an error, as the host answers)."""

    def __init__(self, conn, *, may_read=lambda k: True, held=EVERY, guard=None,
                 broken=False) -> None:
        self.conn, self.may_read, self.held = conn, may_read, held
        self.guard = Guard() if guard is None else guard
        self.broken = broken
        self.calls: list[dict] = []

    @contextmanager
    def _request(self):
        yield

    def _call(self, tool: str, args: dict) -> dict:
        if tool == "plx.aggregate":
            self.calls.append(dict(args))
            if self.broken:
                return {"error": {"code": "timeout", "message": "x"}}
            return bd.answer(self.conn, view=args["view"], may_read=self.may_read,
                             held=self.held, guard=self.guard,
                             as_of=date.fromisoformat(args.get("as_of") or AS_OF.isoformat()),
                             args={k: args[k] for k in ("subject", "question") if k in args})
        if tool == "plx.object":
            oid = args["object_id"]
            gate = dt.Gate(self.conn, may_read=self.may_read, held=self.held, guard=self.guard)
            gate.load([oid])
            if not gate.visible(oid):
                return {"error": {"code": "not_found"}}
            v = dt.values(self.conn, [oid], ("name", "title", "subject", "lot_number")).get(oid) or {}
            return {"object": {"display": dt.first(v, "name", "title", "subject", "lot_number")}}
        raise AssertionError(tool)


def one(docs, view_name):
    [found] = [x for x in docs.lists if x.view == view_name]
    return found


class RecordDocuments(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.n = 0

    def conn(self, f: Fixture):
        self.n += 1
        conn = f.build(self.dir / f"s{self.n}.sqlite")
        self.addCleanup(conn.close)
        return conn

    def issues_world(self, n: int = 1, *, item_audience: str = "api:a"):
        f = Fixture()
        w = f.w
        sup = w.obj("Supplier", {"name": "Synth Resin Co", "bc_email": "qa@resin.example"})
        other = w.obj("Supplier", {"name": "Other Glass Co", "bc_email": "qa@glass.example"})
        item = w.obj("Product", {"code": "RES-1001", "name": "Resin base"},
                     audience=item_audience)
        f.codes[item] = "RES-1001"
        tasks = []
        for i in range(n):
            lot = w.obj("Lot", {"lot_number": f"LOT-{7700 + i}"})
            f.codes[lot] = f"LOT-{7700 + i}"
            rc = w.obj("Receipt", {"receipt_date": "2026-10-02"})
            rl = w.obj("ReceiptLine", {"received_quantity": ("40", "KG", "received")})
            w.link("ReceiptLine.line_of", rl, rc)
            w.link("ReceiptLine.received_lot", rl, lot)
            w.link("Receipt.supplied_by", rc, sup)
            tasks.append(w.obj("PlannerTask", {
                "title": f"Quarantine lot LOT-{7700 + i} RES-1001 contamination found",
                "created_at": f"2026-10-{1 + i % 28:02d}T09:00:00Z"}))
        # another supplier's issue: never on this supplier's page
        olot = w.obj("Lot", {"lot_number": "LOT-9900"})
        f.codes[olot] = "LOT-9900"
        orc = w.obj("Receipt", {"receipt_date": "2026-10-02"})
        orl = w.obj("ReceiptLine", {"received_quantity": ("1", "EA", "received")})
        w.link("ReceiptLine.line_of", orl, orc)
        w.link("ReceiptLine.received_lot", orl, olot)
        w.link("Receipt.supplied_by", orc, other)
        w.obj("PlannerTask", {"title": "Reject lot LOT-9900 cracked glass",
                              "created_at": "2026-10-03T09:00:00Z"})
        return f, sup, item, tasks

    def test_a_suppliers_page_lists_its_own_issues_once_each_n_1_10_60(self) -> None:
        for n in (1, 10, 60):
            f, sup, item, tasks = self.issues_world(n)
            view = ReaderView(self.conn(f))
            started = time.monotonic()
            docs = rd.page_documents(view, sup, "Supplier", AS_OF)
            self.assertLess(time.monotonic() - started, budget(30))
            issues = one(docs, "supplier_issues")
            self.assertEqual([x.view for x in docs.lists], ["supplier_issues"])
            self.assertEqual(sorted(r.source.object_id for r in issues.rows), sorted(tasks), n)
            row = issues.rows[0]
            self.assertEqual((row.status, row.tone, row.source.label),
                             ("Quarantine", "bad", "Planner task"))
            self.assertTrue(row.title.startswith("Quarantine lot LOT-"))
            names = {r.type: r.label for r in row.records}
            self.assertEqual(names["Product"], "Resin base")
            self.assertTrue(names["Lot"].startswith("LOT-77"))
            self.assertNotIn(sup, [r.object_id for r in row.records])   # the page is it
            self.assertTrue(all("9900" not in r.title for r in issues.rows))
            self.assertEqual(view.calls[0]["subject"], sup)

    def test_a_restricted_reader_sees_no_name_or_row_it_may_not_read(self) -> None:
        f, sup, item, tasks = self.issues_world(2, item_audience="api:secret")
        conn = self.conn(f)
        view = ReaderView(conn, may_read=lambda k: k != "api:secret")
        issues = one(rd.page_documents(view, sup, "Supplier", AS_OF), "supplier_issues")
        self.assertEqual(len(issues.rows), 2)
        for row in issues.rows:
            # the item the task names is not the reader's to read: not named, not linked
            self.assertNotIn(item, [r.object_id for r in row.records])
            self.assertNotIn("Resin base", [r.label for r in row.records])
        # every record hidden: no row at all, the rows counted as withheld
        blind = ReaderView(conn, may_read=lambda k: False)
        issues = one(rd.page_documents(blind, sup, "Supplier", AS_OF), "supplier_issues")
        self.assertEqual(issues.rows, [])
        self.assertGreater(issues.withheld, 0)

    def test_a_customers_page_holds_only_its_own_forecasts_scores_and_prices(self) -> None:
        f, cust, items = forecast_world(3)
        other = f.customer("Other Buyer Ltd", "cust-b.example")
        f.mailed("Forecast Q1.xlsx", sheet([["Item", "Jan-26"], ["SYN-000", "999"]]),
                 sender="plan@cust-b.example")
        f.mailed("Price list 2026.xlsx",
                 sheet([["Item #", "Description", "Price"], ["SYN-000", "a", "USD 10.00"]]),
                 sender="rep@synth-tenant.example", to="buyer@cust-a.example")
        f.invoice(cust, items[0], "10", "105", "2026-02-01")   # 10.50: off the 10.00 sheet
        conn = self.conn(f)
        conn.execute("UPDATE value_cells SET currency = 'USD' WHERE field IN (SELECT field "
                     "FROM value_fields WHERE property = 'amount_excluding_tax')")
        conn.commit()
        docs = rd.page_documents(ReaderView(conn), cust, "Customer", AS_OF)
        self.assertEqual([x.view for x in docs.lists],
                         ["forecasts", "forecast_accuracy", "price_sheets", "answer_bank"])
        fc = one(docs, "forecasts")
        self.assertEqual(len(fc.rows), 3)                       # one row per item
        self.assertEqual([(x.label, x.value) for x in fc.rows[0].figures],
                         [("Jan 2026", "10"), ("Feb 2026", "20"), ("Mar 2026", "30")])
        self.assertNotIn("999", [x.value for r in fc.rows for x in r.figures])
        acc = one(docs, "forecast_accuracy")
        self.assertEqual(len(acc.rows), 3)
        figs = {x.label: (x.value, x.unit) for x in acc.rows[0].figures}
        self.assertEqual(figs["Error (weighted)"], ("68.4", "%"))   # 26 over 38, Decimal
        self.assertEqual(figs["Bias"], ("+57.9", "%"))
        self.assertEqual(acc.rows[0].status, "Over-forecast")
        price = one(docs, "price_sheets")
        [row] = price.rows
        self.assertEqual((row.status, row.tone), ("Billed off the sheet", "bad"))
        self.assertEqual({x.label: (x.value, x.unit) for x in row.figures}["Sheet price"],
                         ("10.00", "USD"))
        # the other customer's page holds its own forecast, not this one's
        theirs = one(rd.page_documents(ReaderView(conn), other, "Customer", AS_OF), "forecasts")
        self.assertEqual([x.value for r in theirs.rows for x in r.figures], ["999"])
        # an item's page: that item's rows only
        item_docs = rd.page_documents(ReaderView(conn), items[1], "Product", AS_OF)
        self.assertEqual(len(one(item_docs, "forecasts").rows), 1)
        self.assertEqual(one(item_docs, "price_sheets").rows, [])
        # without finance: no price sheet is read, the list says what it needs
        blind = one(rd.page_documents(ReaderView(conn, held=frozenset({"pii"})), cust,
                                      "Customer", AS_OF), "price_sheets")
        self.assertEqual((blind.rows, blind.needs), ([], "finance"))

    def test_unreadable_lists_fail_closed_and_other_types_have_none(self) -> None:
        f, cust, _ = forecast_world(1)
        conn = self.conn(f)
        for view in (ReaderView(conn, guard=Guard(unavailable="excluded_people_oids_absent")),
                     ReaderView(conn, broken=True)):
            for x in rd.page_documents(view, cust, "Customer", AS_OF).lists:
                self.assertEqual((x.rows, x.complete), ([], False))
                self.assertTrue(x.error)
        self.assertIsNone(rd.page_documents(ReaderView(conn), cust, "PurchaseOrder", AS_OF))
        self.assertFalse(rd.applies("Person"))
        # a subject of another kind keeps no row (never every row)
        out = bd.answer(conn, view="forecasts", may_read=lambda k: True, held=EVERY,
                        guard=Guard(), as_of=AS_OF, args={"subject": "PurchaseOrder:fx:1"})
        self.assertEqual(out["rows"], [])
        self.assertEqual(out["counts"]["subject_not_customer_or_item"], 1)

    def test_the_templates_answer_bank_lists_only_answers_any_customer_may_get(self) -> None:
        f = Fixture()
        cust = f.customer("Synth Buyer Co", "cust-a.example")
        f.mailed("Supplier questionnaire.xlsx",
                 sheet([["Do you hold a GMP certificate?", "Yes, since 2019"],
                        ["What is your account number with us?", "Account 4471-SYN"]]),
                 sender="qa@cust-a.example")
        conn = self.conn(f)
        bank = rd.answer_bank(ReaderView(conn), "GMP certificate?", AS_OF)
        self.assertEqual([(r.title, r.detail, r.status) for r in bank.rows],
                         [("Do you hold a GMP certificate?", "Yes, since 2019", "Any customer")])
        self.assertEqual(bank.rows[0].records[0].object_id, cust)
        # the customer's own page holds the answer specific to it too
        theirs = one(rd.page_documents(ReaderView(conn), cust, "Customer", AS_OF), "answer_bank")
        self.assertIn("Account 4471-SYN", [r.detail for r in theirs.rows])
        self.assertNotIn("Account 4471-SYN", [r.detail for r in
                                              rd.answer_bank(ReaderView(conn), None, AS_OF).rows])
        blind = rd.answer_bank(ReaderView(conn, may_read=lambda k: False), None, AS_OF)
        self.assertEqual(blind.rows, [])


if __name__ == "__main__":
    unittest.main()


class DocumentsSection(unittest.TestCase):
    """``read_section(..., "documents")`` returns the section itself, polished: a Supplier
    page reads it (applies), a Customer page never falls through into the trail walk."""

    def test_supplier_and_customer_read_the_section_polished_and_no_trail(self) -> None:
        from types import SimpleNamespace
        from unittest import mock
        from operator_api import models as m
        from operator_api import trail
        from operator_api.backend import read_section

        def documents(view, oid, kind, today=None):
            return m.RecordDocuments(lists=[m.DocList(
                view="supplier_issues", title="claim check failed",
                rows=[m.DocRow(title="claim check failed")])])

        backend = SimpleNamespace(today=None, page_cache=None)
        for kind in ("Supplier", "Customer"):
            with self.subTest(kind=kind), \
                    mock.patch.object(rd, "page_documents", documents), \
                    mock.patch.object(trail, "for_page",
                                      side_effect=AssertionError("trail reached")) as walk:
                page = m.ObjectPage.model_construct(object_id=f"{kind}:x", type=kind,
                                                    label="x", not_found=[])
                out = read_section(backend, ReaderView(None), page, "documents")
                self.assertTrue(out.applies)
                self.assertFalse(walk.called)
                self.assertIsNone(out.trail)
                [listed] = out.documents.lists
                said = "figures not confirmed against the records"
                self.assertEqual((listed.title, listed.rows[0].title), (said, said))

    def test_a_list_still_being_read_says_so_and_the_section_is_not_kept(self) -> None:
        """A list whose documents the aggregate is still reading (``still_reading``) is
        an error line, never an empty list, and the section holding it is not kept, so the
        next open reads it again; a whole section is kept."""
        from types import SimpleNamespace
        from unittest import mock
        from operator_api import backend as be
        from operator_api import models as m
        from operator_api.serving import ReaderCache

        view = ReaderView(None)
        view._call = lambda tool, args: {"rows": [], "counts": {"still_reading": 1},
                                         "withheld_counts": {}, "complete": False}
        got = rd.doc_list(view, "forecasts", "Customer:x", AS_OF)
        self.assertEqual((got.rows, got.error, got.complete), ([], rd.STILL_READING, False))
        view._call = lambda tool, args: {"rows": [], "counts": {}, "withheld_counts": {},
                                         "complete": True}
        whole = rd.doc_list(view, "forecasts", "Customer:x", AS_OF)
        self.assertEqual((whole.error, whole.complete), (None, True))

        reads = []

        def documents(view, oid, kind, today=None):
            reads.append(oid)
            listed = got if len(reads) == 1 else whole
            return m.RecordDocuments(lists=[listed])

        backend = SimpleNamespace(today=None, page_cache=ReaderCache())
        page = m.ObjectPage.model_construct(object_id="Customer:x", type="Customer",
                                            label="x", not_found=[])
        with mock.patch.object(rd, "page_documents", documents), \
                mock.patch.object(be, "page_key", lambda view, what: ("k",) + what):
            first = be.kept_section(backend, view, page, "documents")
            self.assertEqual(first.documents.lists[0].error, rd.STILL_READING)
            second = be.kept_section(backend, view, page, "documents")
            self.assertIsNone(second.documents.lists[0].error)
            be.kept_section(backend, view, page, "documents")
        self.assertEqual(len(reads), 2)   # read again after "still reading", then kept
