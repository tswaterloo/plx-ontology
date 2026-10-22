"""The scheduled proactive engine (operating/proactive_engine.py) and the Home inbox read.

SYNTHETIC fixtures only: projection stores built with ``StoreBuilder`` from rows written
here (synthetic handles, no business values), a fake grant per reader. Evidence label:
isolated-fixture. ``LiveCheckTest`` runs the live-check command over the injected-transport
world of ``case.customer_po_missing`` (fake Workbench, fake drive; network refused).
"""

from __future__ import annotations

import contextlib
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPConnection
import io
import json
import re
import sqlite3
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from verify import live_verify as lv
from integrations.http import SourceHttpError
from ontology.core import Actor
from ontology.store import StoreBuilder
from operating import po_status
from operating import proactive_engine as pe
from operating.action_path import ActionPath
from operating.ontology_tools_investigate import DRAFT_ACTION
from operating.cases import CaseStore
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operating.tests.test_cases import FakeResolver
from operator_ui import home as home_view
from operator_ui import object_workspace as objects
from operator_ui.case_workspace import CaseWorkspace
from operator_ui.server import make_server

ROOT = Path(__file__).resolve().parents[2]
AS_OF = date(2026, 10, 19)
T0 = "2026-10-01T00:00:00.000000Z"
QC_LINK = "RelPublicAppIncomingQcRecord.inspects"
OPS = Actor("plx", frozenset({"ops"}))


def handle(n: int) -> str:
    return f"api://public.fixture_rows/%7B%22id%22%3A{n}%7D@g1"


class World:
    """Rows for one synthetic store: records, their one current version and values, links."""

    def __init__(self) -> None:
        self.rows = {"objects": [], "object_versions": [], "object_values": [], "links": []}
        self.n = 0

    def obj(self, object_type: str, values: dict, *, audience: str = "api:a",
            markings: str = "[]") -> str:
        self.n += 1
        oid = f"{object_type}:fx:{self.n}"
        self.rows["objects"].append({
            "object_id": oid, "object_type": object_type, "binding_id": "fx",
            "native_key": json.dumps({"id": self.n}), "audience_key": audience,
            "first_recorded_at": T0})
        self.rows["object_versions"].append({
            "object_id": oid, "l1_handle": handle(self.n), "digest": f"d{self.n}",
            "lineage": "workbench", "markings": markings, "recorded_at": T0})
        for prop, raw in values.items():
            quantity = isinstance(raw, tuple)
            kind = ("quantity" if quantity else "date"
                    if prop.endswith("_date") or prop == "valid_until" else "text")
            self.rows["object_values"].append({
                "object_id": oid, "property": prop, "value_type": kind,
                "value": raw[0] if quantity else raw, "unit": raw[1] if quantity else None,
                "quantity_role": (raw[2] if len(raw) > 2 else "ordered") if quantity else None,
                "l1_handle": handle(self.n), "field_path": f"row.{prop}", "digest": f"d{self.n}",
                "lineage": "workbench", "binding_id": "fx", "parser": "fixture",
                "markings": markings, "recorded_at": T0})
        return oid

    def link(self, link_type: str, a: str, b: str, tier: str = "verified") -> None:
        self.rows["links"].append({
            "link_id": f"l{len(self.rows['links'])}", "link_type": link_type,
            "from_object": a, "to_object": b, "tier": tier,
            "binding_kind": "fk" if tier != "lead" else "mention", "reason": "[\"fixture\"]",
            "evidence_handles": json.dumps([handle(1)]), "why_not": "[]", "markings": "[]",
            "recorded_at": T0})

    def build(self, path: Path) -> Path:
        builder = StoreBuilder(path)
        for table in ("objects", "object_versions", "object_values", "links"):
            builder.add(table, self.rows[table])
        builder.set_meta("audience_keys", sorted({r["audience_key"]
                                                   for r in self.rows["objects"]}))
        builder.commit(built_at=T0)
        return path


def po(w: World, status: str, expected: str | None = None, *,
       lines=(("10", "0"),), **scope) -> str:
    """A Portal purchase order with its header word and its own lines (ordered,
    received), each linked ``line_of``; ``scope`` (audience, markings) is the header's
    and its lines'."""
    oid = w.obj("PurchaseOrder", {"status": status,
                                  **({"expected_date": expected} if expected else {})},
                **scope)
    for ordered, received in lines:
        w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
            "ordered_quantity": (ordered, "ea"),
            "received_quantity": (received, "ea", "received")}, **scope), oid)
    return oid


def seeded(world: World | None = None, *, receipt_markings: str = "[]") -> tuple[World, dict]:
    """One world with every detector's seeded gaps and near misses; the ids that matter."""
    w = world or World()
    ids: dict = {}
    # late_or_unconfirmed_po: the real Portal header words; a PO is open by its lines
    ids["unconfirmed"] = po(w, "Pending", "2026-10-10")
    po(w, "Pending", "2026-11-30")                                                 # not yet
    po(w, "Complete", "2026-10-01", lines=(("10", "10"),))                         # closed
    po(w, "Cancel", "2026-10-01")                                                  # closed
    po(w, "DRAFT", "2026-10-01")                                                   # not issued
    po(w, "Revised", "2026-10-01")                                                 # unknown
    w.obj("PurchaseOrder", {"sage_is_complete": "1", "expected_date": "2026-10-01"})  # Sage
    w.obj("PurchaseOrder", {"po_number": "SYNTH-PO-1", "expected_date": "2026-10-01"})
    lead_po = po(w, "B.Order", "2026-11-02", lines=(("10", "4"),))
    w.link("SupplierConfirmation.confirms",
           w.obj("SupplierConfirmation", {"delivery_date": "2026-10-01"}), lead_po, "lead")
    ids["late_with_lead"] = po(w, "B.Order", "2026-10-06")
    w.link("SupplierConfirmation.confirms",
           w.obj("SupplierConfirmation", {"delivery_date": "2026-10-01"}),
           ids["late_with_lead"], "lead")
    ids["confirmed_late"] = po(w, "Pending", "2026-11-01")
    w.link("SupplierConfirmation.confirms",
           w.obj("SupplierConfirmation", {"delivery_date": "2026-11-15"}), ids["confirmed_late"])
    # FM's Complete over an outstanding quantity is open, its FileMaker twin decides nothing
    ids["overdue"] = po(w, "Complete", "2026-10-05", lines=(("10", "4"),))
    w.link("SupplierConfirmation.confirms",
           w.obj("SupplierConfirmation", {"delivery_date": "2026-10-04"}), ids["overdue"])
    fm_twin = w.obj("PurchaseOrder", {"fm_status": "Complete", "expected_date": "2026-10-05"})
    w.link("PurchaseOrder.same_record_as", ids["overdue"], fm_twin)
    ids["overdue_root"] = min(ids["overdue"], fm_twin)
    ids["line_dated"] = w.obj("PurchaseOrder", {"status": "Pending"})   # no requested-by date
    w.link("PurchaseOrderLine.line_of",
           w.obj("PurchaseOrderLine", {"bc_expected_receipt_date": "2026-10-02",
                                       "ordered_quantity": ("10", "ea")}),
           ids["line_dated"])
    twin_a = po(w, "Pending", "2026-10-03")
    twin_b = po(w, "Pending", "2026-10-07")
    w.link("PurchaseOrder.same_record_as", twin_a, twin_b)                         # disputed
    # po_receipt_quantity_mismatch
    ids["over"] = w.obj("PurchaseOrderLine", {"ordered_quantity": ("100", "kg")})
    w.link("ReceiptLine.receives_line",
           w.obj("ReceiptLine", {"received_quantity": ("120", "kg", "received")}), ids["over"])
    received_po = w.obj("PurchaseOrder", {"status": "Complete"})       # received in full
    ids["short"] = w.obj("PurchaseOrderLine", {"ordered_quantity": ("100", "kg"),
                                               "received_quantity": ("100", "kg", "received")})
    w.link("PurchaseOrderLine.line_of", ids["short"], received_po)
    w.link("ReceiptLine.receives_line",
           w.obj("ReceiptLine", {"received_quantity": ("80", "kg", "received")}), ids["short"])
    open_po = w.obj("PurchaseOrder", {"status": "Pending"})
    receiving = w.obj("PurchaseOrderLine", {"ordered_quantity": ("100", "kg")})
    w.link("PurchaseOrderLine.line_of", receiving, open_po)                         # receiving
    w.link("ReceiptLine.receives_line",
           w.obj("ReceiptLine", {"received_quantity": ("80", "kg", "received")}), receiving)
    pounds = w.obj("PurchaseOrderLine", {"ordered_quantity": ("100", "kg")})
    w.link("ReceiptLine.receives_line",
           w.obj("ReceiptLine", {"received_quantity": ("300", "lb", "received")}), pounds)
    exact = w.obj("PurchaseOrderLine", {"ordered_quantity": ("50", "kg")})          # one receipt
    copy_a = w.obj("ReceiptLine", {"received_quantity": ("50", "kg", "received")})  # line twice
    copy_b = w.obj("ReceiptLine", {"received_quantity": ("50", "kg", "received")})
    w.link("ReceiptLine.same_record_as", copy_a, copy_b)
    w.link("ReceiptLine.receives_line", copy_a, exact)
    w.link("ReceiptLine.receives_line", copy_b, exact)
    leaded = w.obj("PurchaseOrderLine", {"ordered_quantity": ("10", "kg")})       # lead only
    w.link("ReceiptLine.receives_line",
           w.obj("ReceiptLine", {"received_quantity": ("20", "kg", "received")}), leaded, "lead")
    # conflicting_quantity (and an unreconciled receipt the mismatch rule leaves alone)
    disputed = w.obj("PurchaseOrderLine", {"ordered_quantity": ("30", "kg")})
    ids["conflict_a"] = w.obj("ReceiptLine", {"received_quantity": ("30", "kg", "received")})
    ids["conflict_b"] = w.obj("ReceiptLine", {"received_quantity": ("35", "kg", "received")},
                              audience="api:b")
    w.link("ReceiptLine.same_record_as", ids["conflict_a"], ids["conflict_b"])
    w.link("ReceiptLine.receives_line", ids["conflict_a"], disputed)
    # receipt_without_qc
    ids["no_qc"] = w.obj("Receipt", {"received_date": "2026-10-10"}, markings=receipt_markings)
    inspected = w.obj("Receipt", {"received_date": "2026-10-10"})
    inspected_line = w.obj("ReceiptLine", {"lab_id": "L-1"})
    w.link("ReceiptLine.line_of", inspected_line, inspected)
    w.link(QC_LINK, w.obj("RelPublicAppIncomingQcRecord", {}), inspected_line)
    w.obj("Receipt", {"received_date": "2026-12-01"})                                # future
    lead_qc = w.obj("Receipt", {"received_date": "2026-10-11"})
    w.link(QC_LINK, w.obj("RelPublicAppIncomingQcRecord", {}), lead_qc, "lead")
    # order_without_customer_po
    ids["no_customer_po"] = w.obj("SalesOrder", {"status": "CONFIRMED"})
    held = w.obj("SalesOrder", {"customer_po": "SYNTH-PO"})
    ids["coverage"] = {"orders": [
        {"status": "absent_in_this_source", "citation": handle(w.n - 1)},
        {"status": "absent_in_this_source", "citation": handle(w.n)},       # store holds one
        {"status": "absent_in_this_source", "citation": "api://public.nowhere/x@g1"},
        {"status": "has_customer_po", "citation": handle(w.n - 1)}],
        "completeness": {"complete": True}}
    del held
    # expired_quote
    ids["expired"] = w.obj("ForwarderQuote", {"valid_until": "2026-01-31"})
    w.obj("ForwarderQuote", {"valid_until": "2999-01-01"})
    return w, ids


def everyone(_key: str) -> bool:
    return True


def admit(handles):
    return {h: None for h in handles}


ALL_MARKINGS = frozenset({"finance", "l5", "pii", "quality_controlled"})


class Engine(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = self.dir / "store.sqlite"
        self.inbox = pe.inbox_path(self.store)

    def run_engine(self, world: World, coverage=None, *, at=None, **options) -> dict:
        world.build(self.store)
        moment = at or datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
        return pe.run(self.store, self.inbox, AS_OF, coverage=coverage,
                      qc_link_types=options.pop("qc_link_types", (QC_LINK,)),
                      clock=lambda: moment, **options)

    def read(self, *, reader="plx:ops", may_read=everyone, held=ALL_MARKINGS, decide=admit,
             **options) -> dict:
        return pe.read_inbox(self.inbox, self.store, reader=reader, may_read=may_read,
                             held=held, decide=decide, **options)


class DetectorTest(Engine):

    def test_each_detector_finds_exactly_its_seeded_gaps_set_wise_with_citations(self):
        world, ids = seeded()
        summary = self.run_engine(world, ids["coverage"])
        found = {(g["kind"], g["object_id"]) for g in self.read()["gaps"]}
        conflict_root = min(ids["conflict_a"], ids["conflict_b"])
        self.assertEqual(found, {
            ("unconfirmed_po", ids["unconfirmed"]), ("late_po", ids["late_with_lead"]),
            ("late_po", ids["confirmed_late"]), ("late_po", ids["overdue_root"]),
            ("po_receipt_quantity_mismatch", ids["over"]),
            ("po_receipt_quantity_mismatch", ids["short"]),
            ("conflicting_quantity", conflict_root),
            ("receipt_without_qc", ids["no_qc"]),
            ("order_without_customer_po", ids["no_customer_po"]),
            ("expired_quote", ids["expired"])})
        self.assertEqual(summary["scanned"], world.n)   # the whole store, not a sample
        self.assertEqual((summary["gaps_found"], summary["gaps_cited"],
                          summary["gaps_uncited"]), (10, 10, 0))
        self.assertFalse(summary["scope_truncated"])
        self.assertEqual(summary["detectors"]["card_rules"], "not_configured")
        self.assertEqual(summary["not_evaluated"]["late_or_unconfirmed_po"],
                         {"confirmation_unsettled": 1, "need_date_disputed": 1,
                          "no_need_date": 2, "sage_unmapped": 1, "status_missing": 1,
                          "status_unmapped": 1})
        self.assertEqual(summary["po_lateness"], {
            "pos": 16, "classes": {"open": 9, "closed": 3, "draft": 1, "unknown": 3},
            "evaluated": 5, "late": 3, "at_risk": 1, "unconfirmed": 1})
        self.assertEqual(summary["not_evaluated"]["po_receipt_quantity_mismatch"],
                         {"receipt_link_unsettled": 1, "sources_unreconciled": 1,
                          "unit_differs": 1})
        self.assertEqual(summary["not_evaluated"]["receipt_without_qc"],
                         {"qc_link_unsettled": 1})
        self.assertEqual(summary["not_evaluated"]["order_without_customer_po"],
                         {"not_projected": 1})
        for gap in self.read()["gaps"]:
            self.assertTrue(gap["citations"], gap["kind"])
            self.assertLessEqual(set(gap["follow_up"]), objects.FOLLOW_UP_FIELDS)
            self.assertTrue(gap["follow_up"]["missing"] and gap["follow_up"]["why"])
            self.assertEqual(gap["follow_up"]["object_id"], gap["object_id"])
        over = next(g for g in self.read()["gaps"] if g["object_id"] == ids["over"])
        self.assertIn("120 kg against 100 kg", over["summary"])

    def test_qc_is_blocked_without_a_declared_link_and_coverage_absent_is_not_configured(self):
        world, _ids = seeded()
        summary = self.run_engine(world, None, qc_link_types=None)
        self.assertEqual(pe.declared_qc_links(), ())   # the schema serves no QC relation yet
        self.assertEqual(summary["detectors"]["receipt_without_qc"], "blocked")
        self.assertEqual(summary["detector_reasons"]["receipt_without_qc"],
                         ["qc_link_not_declared"])
        self.assertEqual(summary["detectors"]["order_without_customer_po"], "not_configured")
        self.assertNotIn("receipt_without_qc", summary["gaps_by_kind"])

    def test_an_incomplete_mirror_scan_is_truncated_and_resolves_nothing(self):
        world, ids = seeded()
        self.run_engine(world, ids["coverage"])
        partial = {**ids["coverage"], "orders": [], "completeness": {"complete": False}}
        summary = self.run_engine(world, partial)
        self.assertEqual(summary["detectors"]["order_without_customer_po"], "truncated")
        self.assertTrue(summary["scope_truncated"])
        kinds = [g["kind"] for g in self.read()["gaps"]]
        self.assertIn("order_without_customer_po", kinds)   # kept open, not resolved


class LateOrderWorld(Engine):
    """One late order (PO-77 from Acme Resins, two late lines) and the draft's wording rules."""

    def world(self, n_late=2):
        w = World()
        order = w.obj("PurchaseOrder", {"po_number": "PO-77"}, audience="api:a")
        supplier = w.obj("Supplier", {"name": "Acme Resins"}, audience="api:a")
        w.link("PurchaseOrder.ordered_from", order, supplier)
        late = []
        for i in range(n_late):
            line = w.obj("PurchaseOrderLine", {
                "bc_expected_receipt_date": f"2026-10-0{i + 1}",
                "ordered_quantity": ("100", "kg"), "received_quantity": ("40", "kg"),
                "description": f"Resin {i}"}, audience="api:a")
            w.link("PurchaseOrderLine.line_of", line, order)
            late.append(line)
        done = w.obj("PurchaseOrderLine", {   # received in full: not late
            "bc_expected_receipt_date": "2026-10-01", "ordered_quantity": ("5", "kg"),
            "received_quantity": ("5", "kg")})
        future = w.obj("PurchaseOrderLine", {   # not due
            "bc_expected_receipt_date": "2999-12-01", "ordered_quantity": ("5", "kg"),
            "received_quantity": ("0", "kg")})
        for line in (done, future):
            w.link("PurchaseOrderLine.line_of", line, order)
        return w, order, late

    def fresh(self) -> None:
        for path in self.dir.glob("store.sqlite*"):
            path.unlink()

    def draft_of(self, w: World, **read) -> dict:
        self.fresh()
        self.run_engine(w)
        gap = next(g for g in self.read(**read)["gaps"] if g["kind"] == "late_order")
        return gap["follow_up"]["draft"]

    def assert_worded(self, draft: dict) -> None:
        """Never sent; no "promised"; citations a side list (no "[n]" marker in the text),
        each stated fact one entry citing one of them, within the catalog's bound."""
        self.assertEqual((draft["label"], draft["status"], draft["sent"]),
                         (pe.DRAFT_LABEL, "draft_not_sent", False))
        self.assertTrue(draft["text"].startswith("Draft supplier message (not sent)"))
        self.assertNotIn("promis", draft["text"].lower())
        self.assertIsNone(re.search(r"\[\d+\]", draft["text"]))
        self.assertEqual(len(set(draft["citations"])), len(draft["citations"]))
        self.assertLessEqual(len(draft["citations"]), pe.MAX_DRAFT_CITATIONS)
        self.assertLessEqual(len(draft["text"]), pe.MAX_DRAFT_TEXT)
        for fact in draft["facts"]:
            self.assertNotIn("ref", fact)
            self.assertIn(fact["citation"], draft["citations"])
            self.assertIn(fact["value"], draft["text"])


class LateOrderTest(LateOrderWorld):
    """``late_order`` (operating.proactive_gaps' rule) is a rule of the scheduled engine:
    one gap per order, with the never-sent supplier draft worded from the store."""

    def test_one_gap_per_order_with_a_cited_draft_that_is_never_sent(self):
        w, order, late = self.world()
        summary = self.run_engine(w)
        self.assertEqual(summary["detectors"]["late_order"], "complete")
        (gap,) = [g for g in self.read()["gaps"] if g["kind"] == "late_order"]
        self.assertEqual(gap["object_id"], order)
        draft = gap["follow_up"]["draft"]
        self.assertIs(draft["sent"], False)
        self.assertEqual(draft["status"], "draft_not_sent")
        self.assertEqual((draft["to"]["kind"], draft["to"]["name"]), ("supplier", "Acme Resins"))
        self.assertIn("PO-77", draft["text"])
        self.assertEqual(draft["lines_late"], 2)
        self.assertEqual(draft["unresolved"], [])
        self.assertLessEqual(set(draft["citations"]), set(gap["citations"]))
        self.assertLessEqual(set(gap["follow_up"]), objects.FOLLOW_UP_FIELDS)

    def test_a_reader_who_cannot_open_a_fact_the_draft_cites_gets_no_draft(self):
        w, order, late = self.world()
        self.run_engine(w)
        full = next(g for g in self.read()["gaps"] if g["kind"] == "late_order")
        number = handle(int(order.rsplit(":", 1)[1]))
        refused = self.read(decide=lambda hs: {h: ({"withheld": "item_refused"}
                                                   if h == number else None) for h in hs})
        gap = next(g for g in refused["gaps"] if g["kind"] == "late_order")
        self.assertNotIn("draft", gap["follow_up"])
        self.assertNotIn("PO-77", json.dumps(gap))
        self.assertIn("draft", full["follow_up"])

    def test_the_governing_date_is_the_confirmed_date_else_our_expected_date(self):
        w, order, late = self.world()
        draft = self.draft_of(w)
        self.assert_worded(draft)
        self.assertEqual(draft["date_basis"], "expected")
        self.assertIn("purchase order PO-77", draft["text"])
        self.assertIn("- Resin 0: our expected date 2026-10-01, ordered 100 kg, received 40 kg",
                      draft["text"])
        self.assertIn("- Resin 1: our expected date 2026-10-02, ordered 100 kg, received 40 kg",
                      draft["text"])
        self.assertEqual(sorted(f["line_id"] for f in draft["facts"]
                                if f["fact"] == "expected_receipt"), sorted(late))
        self.assertNotIn("confirmed", draft["text"].lower())

        w, order, _late = self.world()
        confirmation = w.obj("SupplierConfirmation", {"delivery_date": "2026-10-25"})
        w.link("SupplierConfirmation.confirms", confirmation, order)
        draft = self.draft_of(w)
        self.assert_worded(draft)
        self.assertEqual(draft["date_basis"], "confirmed")
        self.assertIn("Your confirmation gives the delivery date 2026-10-25", draft["text"])
        self.assertNotIn("our expected date", draft["text"])
        self.assertIn("- Resin 0: ordered 100 kg, received 40 kg", draft["text"])
        [fact] = [f for f in draft["facts"] if f["fact"] == "confirmed_delivery_date"]
        self.assertEqual((fact["value"], fact["citation"]),
                         ("2026-10-25", handle(int(confirmation.rsplit(":", 1)[1]))))
        self.assertEqual([f for f in draft["facts"] if f["fact"] == "expected_receipt"], [])

        # A corroborated-only confirmation is not the supplier's word to quote: the text keeps
        # our expected date and the date is an internal note only.
        w, order, _late = self.world()
        w.link("SupplierConfirmation.confirms",
               w.obj("SupplierConfirmation", {"delivery_date": "2026-10-25"}), order,
               "corroborated")
        draft = self.draft_of(w)
        self.assert_worded(draft)
        self.assertEqual(draft["date_basis"], "expected")
        self.assertNotIn("2026-10-25", draft["text"])
        self.assertNotIn("confirmation gives", draft["text"])
        self.assertIn("our expected date 2026-10-01", draft["text"])
        self.assertEqual([(n["note"], n["value"]) for n in draft["internal_notes"]],
                         [("confirmed_date:corroborated_only", "2026-10-25")])

        # A confirmation linked only as a lead is no confirmation; two that disagree, or one
        # this reader may not open, leave the expected date and say why.
        for name, dates, tier, refuse, unresolved in (
                ("lead", ["2026-10-25"], "lead", False, []),
                ("conflict", ["2026-10-25", "2026-11-02"], "verified", False,
                 ["confirmed_date:conflict"]),
                ("withheld", ["2026-10-25"], "verified", True, ["confirmed_date:withheld"]),
                ("unreadable", ["soon"], "verified", False, ["confirmed_date:unreadable"])):
            with self.subTest(name):
                w, order, _late = self.world()
                made = []
                for day in dates:
                    made.append(w.obj("SupplierConfirmation", {"delivery_date": day}))
                    w.link("SupplierConfirmation.confirms", made[-1], order, tier)
                hidden = {handle(int(c.rsplit(":", 1)[1])) for c in made} if refuse else set()
                self.fresh()
                self.run_engine(w)
                gap = next(g for g in self.read()["gaps"] if g["kind"] == "late_order")
                draft = pe.order_draft(self.store, order, AS_OF,
                                       may_read=everyone, held=ALL_MARKINGS,
                                       decide=lambda hs: {h: {"withheld": "x"} if h in hidden
                                                          else None for h in hs})
                self.assert_worded(draft)
                self.assertEqual((draft["date_basis"], draft["unresolved"]),
                                 ("expected", unresolved))
                self.assertIn("our expected date 2026-10-01", draft["text"])
                self.assertNotIn("2026-10-25", draft["text"])
                if not refuse:   # the inbox's own draft is the same wording
                    self.assertEqual(gap["follow_up"]["draft"]["text"], draft["text"])

    def test_the_draft_says_why_a_number_or_supplier_is_not_named(self):
        def world(*, number=True, suppliers=1, name=True, mail=False, other=False,
                  customer=False):
            w = World()
            order = w.obj("PurchaseOrder", {"po_number": "PO-9"} if number else {"x": "1"})
            values = w.rows["object_values"]
            if other:   # a second, different reading of the number on the record
                values.append({**values[-1], "value": "PO-OTHER", "field_path": "row.po_alt"})
            if mail:    # a number read from mail is not the record's
                for row in (*w.rows["object_versions"], *values):
                    row["l1_handle"] = "graph-mail://fixture/message@v1"
                values[-1]["value"] = "PO-MAIL"
            for _ in range(suppliers):
                w.link("PurchaseOrder.ordered_from", order,
                       w.obj("Customer" if customer else "Supplier",
                             {"name": "Acme Resins"} if name else {"x": "1"}))
            line = w.obj("PurchaseOrderLine", {
                "bc_expected_receipt_date": "2026-10-01", "ordered_quantity": ("9", "kg"),
                "received_quantity": ("1", "kg"), "description": "Resin\n\x07 lump " + "x" * 90})
            w.link("PurchaseOrderLine.line_of", line, order)
            return w
        for name, options, unresolved in (
                ("no number", {"number": False}, ["order_number:not_in_record"]),
                ("no supplier", {"suppliers": 0}, ["supplier:not_linked"]),
                ("two suppliers", {"suppliers": 2}, ["supplier:ambiguous"]),
                ("nameless supplier", {"name": False}, ["supplier:name_not_in_record"]),
                ("mail number", {"mail": True}, ["order_number:not_in_record"]),
                ("two numbers", {"other": True}, ["order_number:not_in_record"]),
                ("not a supplier", {"customer": True}, ["supplier:ambiguous"])):
            with self.subTest(name):
                draft = self.draft_of(world(**options))
                self.assert_worded(draft)
                self.assertEqual(draft["unresolved"], unresolved)
                if "supplier" in name:
                    self.assertEqual(draft["to"]["kind"], "answerer_role")
                    self.assertNotIn("Acme Resins", draft["text"])
                self.assertNotIn("PO-MAIL", draft["text"])
                self.assertNotIn("PO-OTHER", draft["text"])
                if "number" in name:
                    self.assertIn("our purchase order (its number is not readable here)",
                                  draft["text"])
                # Record text is flattened and clipped before it is stated.
                [item] = [f["value"] for f in draft["facts"] if f["fact"] == "line_item"]
                self.assertTrue(item.startswith("Resin lump x"))
                self.assertLessEqual(len(item), 60)
                self.assertNotIn("\x07", draft["text"])

    def test_cardinality_every_late_line_counted_listed_to_the_bound_none_twice(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                order = w.obj("PurchaseOrder", {"po_number": "PO-5"})
                for i in range(n):
                    line = w.obj("PurchaseOrderLine", {
                        "bc_expected_receipt_date": f"2026-09-{1 + i % 28:02d}",
                        "ordered_quantity": ("9", "kg"), "received_quantity": ("1", "kg"),
                        "description": f"Item {i}"})
                    w.link("PurchaseOrderLine.line_of", line, order)
                start = time.monotonic()
                draft = self.draft_of(w)
                self.assertLess(time.monotonic() - start, 5.0)
                self.assert_worded(draft)
                listed = min(n, pe.MAX_DRAFT_LINES)
                self.assertEqual((draft["lines_late"], draft["lines_listed"]), (n, listed))
                rows = [f["line_id"] for f in draft["facts"] if f["fact"] == "expected_receipt"]
                self.assertEqual(len(set(rows)), listed)
                self.assertEqual(draft["text"].count("\n- "), listed + (listed < n))
                if listed < n:
                    self.assertIn(f"- and {n - listed} more late lines", draft["text"])

    def test_unreadable_or_unlinked_lines_are_not_evaluated_never_raised(self):
        w = World()
        order = w.obj("PurchaseOrder", {"po_number": "PO-1"})
        w.obj("PurchaseOrderLine", {   # late but linked to no order
            "bc_expected_receipt_date": "2026-10-01", "ordered_quantity": ("9", "kg"),
            "received_quantity": ("1", "kg")})
        bad = w.obj("PurchaseOrderLine", {   # an unreadable quantity is never late
            "bc_expected_receipt_date": "2026-10-01", "ordered_quantity": ("many", "kg"),
            "received_quantity": ("1", "kg")})
        w.link("PurchaseOrderLine.line_of", bad, order)
        summary = self.run_engine(w)
        self.assertEqual(summary["gaps_found"], 0)
        self.assertEqual(summary["not_evaluated"]["late_order"], {"line_order_unlinked": 1})

    def test_cardinality_one_gap_per_order_for_n_orders(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                for path in self.dir.glob("store.sqlite*"):
                    path.unlink()
                w = World()
                wanted = set()
                for _ in range(n):
                    order = w.obj("PurchaseOrder", {"po_number": "PO"})
                    wanted.add(order)
                    for _line in range(2):
                        line = w.obj("PurchaseOrderLine", {
                            "bc_expected_receipt_date": "2026-10-01",
                            "ordered_quantity": ("9", "kg"), "received_quantity": ("1", "kg")})
                        w.link("PurchaseOrderLine.line_of", line, order)
                start = time.monotonic()
                self.run_engine(w)
                shown = [g for g in self.read(limit=100)["gaps"] if g["kind"] == "late_order"]
                self.assertLess(time.monotonic() - start, 5.0)
                self.assertEqual([g["object_id"] for g in shown].__len__(), n)
                self.assertEqual({g["object_id"] for g in shown}, wanted)


class InboxTest(Engine):

    def test_runs_dedupe_resolve_reopen_and_acks_are_per_reader(self):
        world, ids = seeded()
        first = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
        self.run_engine(world, ids["coverage"], at=first)
        before = {g["id"]: g for g in self.read()["gaps"]}
        self.run_engine(world, ids["coverage"], at=first + timedelta(hours=6))
        after = {g["id"]: g for g in self.read()["gaps"]}
        self.assertEqual(set(before), set(after))          # one gap per finding, stable id
        self.assertTrue(all(after[i]["first_seen"] == before[i]["first_seen"] for i in after))
        target = next(i for i, g in after.items() if g["object_id"] == ids["unconfirmed"])
        pe.acknowledge(self.inbox, self.store, target, reader="plx:ops", may_read=everyone,
                       held=ALL_MARKINGS, decide=admit)
        mine = {g["id"]: g["state"] for g in self.read()["gaps"]}
        theirs = {g["id"]: g["state"] for g in self.read(reader="plx:other")["gaps"]}
        self.assertEqual((mine[target], theirs[target]), ("acknowledged", "open"))
        self.assertNotIn(target, {g["id"] for g in self.read(states=("open",))["gaps"]})
        # the supplier confirms on time: the complete detector resolves it
        world.link("SupplierConfirmation.confirms",
                   world.obj("SupplierConfirmation", {"delivery_date": "2026-10-09"}),
                   ids["unconfirmed"])
        self.run_engine(world, ids["coverage"], at=first + timedelta(days=1))
        self.assertNotIn(target, {g["id"] for g in self.read()["gaps"]})
        resolved = self.read(states=("resolved",))["gaps"]
        self.assertEqual([g["id"] for g in resolved if g["id"] == target], [target])
        # the confirmation is withdrawn (contradicted): reopened, the old ack cleared
        world.rows["links"][-1]["tier"] = "contradicted"
        self.run_engine(world, ids["coverage"], at=first + timedelta(days=2))
        reopened = {g["id"]: g for g in self.read()["gaps"]}
        self.assertEqual(reopened[target]["state"], "open")
        self.assertEqual(reopened[target]["first_seen"], before[target]["first_seen"])

    def test_a_failing_detector_is_failed_and_keeps_its_gaps_open(self):
        world, ids = seeded()
        self.run_engine(world, ids["coverage"])
        with mock.patch.object(pe, "_late_or_unconfirmed_po",
                               side_effect=RuntimeError("SYNTH-detail")):
            summary = self.run_engine(world, ids["coverage"])
        self.assertEqual(summary["detectors"]["late_or_unconfirmed_po"], "failed")
        self.assertNotIn("SYNTH-detail", json.dumps(summary))
        shown = self.read()
        self.assertIn("unconfirmed_po", {g["kind"] for g in shown["gaps"]})
        self.assertEqual(shown["unread"], {"late_or_unconfirmed_po_failed": 1})
        self.assertTrue(shown["truncated"])

    def test_a_scheduled_run_with_rules_that_did_not_run_is_never_read_as_clean(self):
        # The host hook runs without the mirror coverage and without a card caller: those
        # rules (and QC, with no declared link) did not run, so the read is incomplete and
        # names them, never a clean "nothing found".
        world, _ids = seeded()
        world.build(self.store)
        hook = pe.after_build(self.store, as_of=AS_OF)
        self.assertEqual(hook["state"], "ran")
        found = self.read()
        self.assertEqual(found["status"], "read")
        self.assertIs(found["complete"], False)
        self.assertEqual(found["not_run"], {"card_rules": "not_configured",
                                            "order_without_customer_po": "not_configured",
                                            "orders_at_risk": "not_configured",
                                            "receipt_without_qc": "blocked"})
        # every rule run, each complete: only then is the read complete
        def card(_tool, arguments):
            return {"object": {"object_id": arguments["object_id"]}, "leads": [],
                    "conflicts": []}
        self.run_engine(world, _ids["coverage"], call=card)
        full = self.read()
        self.assertEqual(set(full["detectors"]), set(pe.DETECTORS))
        self.assertEqual((full["complete"], full["not_run"], full["truncated"]),
                         (True, {}, False), full["detectors"])
        # a rule the inbox holds no row for (a run before it existed) did not run either
        with contextlib.closing(sqlite3.connect(self.inbox)) as conn, conn:
            conn.execute("DELETE FROM detector_runs WHERE detector = 'expired_quote'")
        gone = self.read()
        self.assertEqual((gone["complete"], gone["not_run"]),
                         (False, {"expired_quote": "not_run"}))

    def test_cardinality_no_gap_lost_or_duplicated_within_the_budget(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                for path in self.dir.glob("store.sqlite*"):
                    path.unlink()
                world = World()
                wanted = {po(world, "Pending", "2026-10-01") for _ in range(n)}
                start = time.monotonic()
                summary = self.run_engine(world)
                shown = self.read()["gaps"]
                self.assertLess(time.monotonic() - start, 5.0)
                self.assertEqual(summary["gaps_found"], n)
                self.assertEqual({g["object_id"] for g in shown}, wanted)
                self.assertEqual(len({g["id"] for g in shown}), n)
        limited = self.read(limit=7)
        self.assertEqual((len(limited["gaps"]), limited["more"]), (7, True))

    def test_unknown_inputs_fail_closed(self):
        self.assertEqual(self.read()["status"], "not_scheduled")
        world, ids = seeded()
        self.run_engine(world, ids["coverage"])
        with self.assertRaises(ValueError):
            self.read(states=("open", "snoozed"))
        for gap_id in ("pe-0000000000000000", None):
            with self.assertRaises(PermissionError):
                pe.acknowledge(self.inbox, self.store, gap_id, reader="plx:ops",
                               may_read=everyone, held=ALL_MARKINGS, decide=admit)
        # a reader whose grant check raises is denied, never admitted
        def broken(_key):
            raise RuntimeError("grant down")
        self.assertEqual(self.read(may_read=broken)["gaps"], [])
        # another schema's file is refused, not read
        other = self.dir / "other.sqlite"
        import sqlite3
        with contextlib.closing(sqlite3.connect(other)) as conn:
            conn.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
                               "INSERT INTO meta VALUES ('schema', 'plx-gap-inbox/v0');")
        with self.assertRaises(ValueError):
            pe.read_inbox(other, self.store, reader="r", may_read=everyone,
                          held=ALL_MARKINGS, decide=admit)
        with self.assertRaises(ValueError):
            pe.run(self.store, other, AS_OF)
        self.assertEqual(pe.after_build(self.dir / "missing.sqlite", as_of=AS_OF),
                         {"state": "failed", "error": "StoreError"})


class PermissionTest(Engine):
    """A restricted reader sees no gap whose subject or any evidence group it may not read,
    and its counts cover only what it may read."""

    def test_audience_marking_and_item_decisions_each_trim_the_inbox(self):
        world, ids = seeded(receipt_markings='["quality_controlled"]')
        self.run_engine(world, ids["coverage"])
        full = {g["object_id"]: g for g in self.read()["gaps"]}
        conflict_root = min(ids["conflict_a"], ids["conflict_b"])
        self.assertIn(conflict_root, full)
        only_a = self.read(may_read=lambda key: key == "api:a", held=frozenset())
        shown = {g["object_id"] for g in only_a["gaps"]}
        # one disagreeing value sits in api:b: the conflict is not shown, not half-shown
        self.assertNotIn(conflict_root, shown)
        # the receipt is quality_controlled: hidden from a reader without that marking
        self.assertNotIn(ids["no_qc"], shown)
        self.assertIn(ids["unconfirmed"], shown)
        self.assertLess(only_a["scanned"], self.read()["scanned"])
        with_marking = self.read(may_read=lambda key: key == "api:a",
                                 held=frozenset({"quality_controlled"}))
        self.assertIn(ids["no_qc"], {g["object_id"] for g in with_marking["gaps"]})
        text = json.dumps(only_a)
        self.assertNotIn(handle(int(ids["conflict_b"].rsplit(":", 1)[1])), text)
        # the host refuses one cited record: the gap resting on it is not shown
        need = full[ids["unconfirmed"]]["citations"][0]
        refused = self.read(decide=lambda hs: {h: ({"withheld": "item_refused"}
                                                   if h == need else None) for h in hs})
        self.assertNotIn(ids["unconfirmed"], {g["object_id"] for g in refused["gaps"]})
        self.assertNotIn(need, json.dumps(refused))
        # nothing granted: nothing shown, nothing counted, nothing acknowledgeable
        none = self.read(may_read=lambda key: False, held=frozenset())
        self.assertEqual((none["gaps"], none["scanned"]), ([], 0))
        with self.assertRaises(PermissionError):
            pe.acknowledge(self.inbox, self.store, full[ids["unconfirmed"]]["id"],
                           reader="plx:x", may_read=lambda key: False, held=frozenset(),
                           decide=admit)


class PoLatenessTest(Engine):
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
            "evaluated": 3, "late": 1, "at_risk": 1, "unconfirmed": 0})

    def test_fm_complete_with_an_outstanding_quantity_is_open_and_sage_only_is_not(self):
        w = World()
        outstanding = po(w, "Complete", "2026-10-02", lines=(("10", "4"), ("5", "5")))
        fm_only = w.obj("PurchaseOrder", {"fm_status": "Complete",
                                          "expected_date": "2026-10-02"})
        sage_open = w.obj("PurchaseOrder", {"sage_is_complete": "0",
                                            "expected_date": "2026-10-02"})
        sage_done = w.obj("PurchaseOrder", {"sage_is_complete": "1",
                                            "expected_date": "2026-10-02"})
        summary = self.run_engine(w)
        shown = {g["object_id"] for g in self.read()["gaps"]}
        self.assertEqual(shown, {outstanding})
        self.assertNotIn(fm_only, shown)
        self.assertFalse({sage_open, sage_done} & shown)
        self.assertEqual(summary["po_lateness"]["classes"],
                         {"open": 1, "closed": 0, "draft": 0, "unknown": 3})
        self.assertEqual(summary["not_evaluated"]["late_or_unconfirmed_po"],
                         {"quantities_unknown": 1, "sage_unmapped": 2})

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

    def test_no_line_or_supplier_date_stands_in_for_a_missing_requested_by_date(self):
        w = World()
        missing = po(w, "Pending")
        w.link("PurchaseOrderLine.line_of", w.obj("PurchaseOrderLine", {
            "bc_expected_receipt_date": "2026-10-02", "ordered_quantity": ("1", "ea")}),
            missing)
        self.confirm(w, missing, "2026-10-03")
        summary = self.run_engine(w)
        self.assertEqual(self.read()["gaps"], [])
        self.assertEqual(summary["not_evaluated"]["late_or_unconfirmed_po"],
                         {"no_need_date": 1})
        self.assertEqual(summary["po_lateness"]["evaluated"], 0)

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
                ids = {po(w, "Pending", "2026-10-01", lines=(("10", str(i % 5)),))
                       for i in range(n)}
                started = time.monotonic()
                summary = self.run_engine(w)
                self.assertLess(time.monotonic() - started, 5)
                gaps = self.read()["gaps"]
                self.assertEqual(sorted(g["object_id"] for g in gaps), sorted(ids))
                self.assertEqual(summary["po_lateness"]["evaluated"], n)
                self.store.unlink()
                self.inbox.unlink(missing_ok=True)


class FakeAccess:
    """A host grant: audiences, held markings and per-item refusals for one reader."""

    def __init__(self, audiences, held=frozenset()) -> None:
        self.audiences, self.held = set(audiences), frozenset(held)

    @contextmanager
    def request(self):
        yield self

    def grant(self):
        return (lambda key: key in self.audiences), self.held

    def decide(self, handles):
        return {h: None for h in handles}

    def store_meta(self):
        return {}

    def withheld_sources(self):
        return []


class FakeStatus:
    def __init__(self, path: Path) -> None:
        self.store_path = path

    def read(self, _meta):
        return {"state": "current"}


class ConsoleDraftTest(LateOrderWorld):
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
                start = time.monotonic()
                proposal = workspace.follow_up(dict(self.gap()["follow_up"]))["proposal"]
                self.assertLess(time.monotonic() - start, 5.0)
                draft = proposal["draft"]
                self.assert_worded(draft)
                self.assertEqual((draft["lines_late"], draft["lines_listed"]),
                                 (n, min(n, pe.MAX_DRAFT_LINES)))
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


class HomeTest(Engine):
    """GET /api/home reads the persisted inbox for the session's reader, under a second."""

    def home(self, actor: Actor, access) -> dict:
        tools = {name: {"handler": lambda arguments, actor: {}}
                 for name in ("plx.find", "plx.object", "plx.around", "plx.timeline")}
        cases = CaseStore(self.dir / "cases.db", citation_resolver=FakeResolver())
        workspace = CaseWorkspace(mock.Mock(cases=cases), actor)
        server = make_server(
            case_workspace=workspace,
            session=fixture_session("plx", actor.principals, channel=CHANNEL_CONSOLE),
            object_workspace=objects.ObjectWorkspace(
                tools, actor, access=access, store_status=FakeStatus(self.store)))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=10),
                                 server.server_close()))
        connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=60)
        start = time.monotonic()
        connection.request("GET", "/api/home",
                           headers={"Authorization": "Bearer " + server.operator_access_token})
        response = connection.getresponse()
        body = json.loads(response.read())
        self.elapsed = time.monotonic() - start
        connection.close()
        self.assertEqual(response.status, 200, body)
        return body["proactive"]

    def test_home_renders_the_readers_inbox_from_the_store_in_under_a_second(self):
        world, ids = seeded()
        for n in range(300):   # a busy inbox: Home shows a bounded page, never scans
            po(world, "Pending", "2026-10-01")
        self.run_engine(world, ids["coverage"])
        found = self.home(OPS, FakeAccess({"api:a"}, ALL_MARKINGS))
        self.assertLess(self.elapsed, 1.0)
        self.assertEqual(found["status"], "read")
        self.assertEqual((len(found["gaps"]), found["more"]), (pe.MAX_SHOWN, True))
        self.assertEqual(found["gaps"][0]["follow_up"]["priority"], "high")
        self.assertEqual(found["scanned"], sum(1 for r in world.rows["objects"]
                                               if r["audience_key"] == "api:a"))
        self.assertTrue(all(g["citations"] for g in found["gaps"]))
        other = self.home(Actor("plx", frozenset({"other"})), FakeAccess(set()))
        self.assertEqual((other["gaps"], other["scanned"]), ([], 0))

    def test_no_grant_or_no_inbox_is_said_and_cases_still_serve(self):
        self.assertEqual(self.home(OPS, None)["status"], "not_configured")   # API-only
        seeded()[0].build(self.store)
        self.assertEqual(self.home(OPS, FakeAccess({"api:a"}))["status"], "not_scheduled")
        self.assertEqual(home_view._found(None), {"status": "not_configured"})


class LiveCheckTest(unittest.TestCase):
    """``verify/live-checks/case.proactive_engine.json`` over injected sources: its own
    command over the host config of ``case.customer_po_missing`` (FakeWorkbench plus a fake
    drive), the network refused. Evidence label: injected-transport."""

    SPEC_ID = "case.proactive_engine"

    def setUp(self) -> None:
        import socket

        def refuse(*_args, **_kwargs):
            raise AssertionError("network access attempted in an offline test")

        for target in ("connect", "connect_ex"):
            patcher = mock.patch.object(socket.socket, target, refuse)
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.spec = json.loads((ROOT / "verify" / "live-checks" / f"{self.SPEC_ID}.json")
                               .read_text("utf-8"))

    def run_spec(self, *, fetch=None) -> tuple[int, dict, str]:
        from integrations.tests import test_capture_sharepoint as tsp
        from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
        from operating.tests import order_fixture as fixture
        from operating.tests.test_case_workspace_live import _customer_drive

        fake = FakeWorkbench()
        fake.catalog = json.loads(json.dumps(fixture.CATALOG))
        fake.tables = fixture.tables()
        fake.tables["public.app_sales_order"].append(
            fixture._so("so-m", "OR1000888001", "cust-1", "CONFIRMED", "28"))
        config = json.loads((ROOT / "examples" / "customer_po_missing_host.live-check.json")
                            .read_text("utf-8"))
        for key in ("capture_audience", "live_audience", "mirror_audience"):
            config[key] = str(ROOT / "examples" / config[key])
        config["sharepoint"]["scope"] = str(ROOT / "examples" / config["sharepoint"]["scope"])
        (self.temp / "host.json").write_text(json.dumps(config), "utf-8")
        argv = [str(self.temp / "host.json") if arg.endswith("_host.live-check.json")
                else str(ROOT / arg) if arg.startswith("examples/")
                else arg.replace("{tmp}", str(self.temp)) for arg in self.spec["command"][3:]]
        drive = _customer_drive()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pe.main(argv, environ={
                **tsp.ENV, "PLX_WORKBENCH_API_URL": BASE, "PLX_WORKBENCH_API_KEY": KEY},
                graph_sender=drive, host_options={
                    "sender": drive, "workbench_fetch": fetch or fake, "sleep": lambda _s: None})
        self.assertTrue(all(method == "GET" for method, _url in drive.calls))
        return code, json.loads(out.getvalue()), out.getvalue()

    def test_the_spec_passes_on_the_whole_store_with_a_cited_gap_and_reads_blocked(self):
        lv.validate_spec(self.spec, stem=self.SPEC_ID)
        code, summary, text = self.run_spec()
        self.assertEqual(lv.check_expectations(self.spec, code, summary), [])
        self.assertGreaterEqual(summary["gaps_cited"], 1)
        self.assertFalse(summary["scope_truncated"])
        self.assertNotIn("OR1000888001", text)   # counts and enums only
        mine, other = summary["readers"]
        self.assertTrue(mine["investigator"])
        self.assertLessEqual(other["shown"], mine["shown"])
        self.assertLessEqual(other["scanned"], mine["scanned"])

        def portal_down(url, headers, *, max_bytes):
            raise SourceHttpError("HTTP 500", status=500, error_code="internal")

        code, summary, _text = self.run_spec(fetch=portal_down)
        self.assertTrue(lv.blocked_parts(summary))
        self.assertEqual(lv.check_expectations(
            self.spec, code, summary, self.spec["expect_blocked"]), [])


if __name__ == "__main__":
    unittest.main()
