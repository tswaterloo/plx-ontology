"""PO lateness: due, promised, revised and received dates of a
purchase order and its lines, the verdict against the governing date with its reason
chain, ``po_delivery_window`` for an unnamed ``plx.process`` call on an open PO, the
``processes`` section of ``plx.schema`` and the stored ``late_po`` investigate plan.

The store is hand-filled with synthetic records over the real compiled schema and the
shipped process graphs (``tests.test_ontology_tools_process`` fixture); the projection
test builds a store from hand-made capture rows. Every output is validated against
operating/ontology_tool_contracts.json. Evidence label: isolated-fixture.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import unittest

from ontology import identifiers
from ontology.json_contract import validate
from ontology.project import CapturedRecord, build_store, object_id_of
from ontology.schema import load_schema
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_process as op
from operating import ontology_tools_traverse as tt
from tests import test_ontology_tools_process as tp

CONTRACTS = tp.CONTRACTS
STAFF, SALES = tp.STAFF, tp.SALES
PROCESS = "po_delivery_window"
MAIL, FILES = "graph_mail.supplier_confirmations", "sharepoint.supplier_confirmations"


def setUpModule():
    tp.setUpModule()


# --------------------------------------------------------------------------- fixture

def sage_po(s, key="sage-1", due="2099-01-15", number="PO100001"):
    po = s.obj(f"PurchaseOrder:portal.sage_po_header:{key}", "sage_bronze")
    s.val(po, "po_number", "identifier", number)
    if due:
        s.event("PurchaseOrder.sage_expected", po, due)
    return po


def portal_po(s, key="po-1", status="Pending", expected=None, number="PO200001",
              status_markings="[]", ordered=("10", "0")):
    """A Portal PO with the header word the live column holds (FileMaker's) and, unless
    ``ordered`` is None, one line ``(ordered, received)``: openness is by quantity."""
    po = s.obj(f"PurchaseOrder:portal.app_purchase_order:{key}", "gold_etl_from_fm")
    s.val(po, "po_number", "identifier", number)
    if status is not None:
        s.val(po, "status", "text", status, markings=status_markings)
    if expected:
        s.event("PurchaseOrder.expected", po, expected)
    if ordered is not None:
        line(s, po, f"{key}-line", *ordered)
    return po


def bc_po(s, key="bc-1", fully_received="false", status="Open", expected=None):
    po = s.obj(f"PurchaseOrder:bc.purchase_orders:{key}")
    if status is not None:
        s.val(po, "bc_status", "text", status)
    if fully_received is not None:
        s.val(po, "bc_fully_received", "boolean", fully_received)
    if expected:
        s.event("PurchaseOrder.bc_expected", po, expected)
    return po


def confirm(s, po, key, promise, document_date, binding=MAIL, tier="corroborated",
            markings="[]"):
    sc = s.obj(f"SupplierConfirmation:{binding}:{key}",
               "mail" if binding == MAIL else "sharepoint")
    if document_date:
        s.val(sc, "document_date", "date", document_date)
    if promise:
        event = s.event("SupplierConfirmation.promised" if binding == MAIL
                        else "SupplierConfirmation.promised_on_file", sc, promise)
        if markings != "[]":
            s.conn.execute("UPDATE events SET markings = ? WHERE event_id = ?",
                           (markings, event))
    s.link("SupplierConfirmation.confirms", sc, po, tier=tier,
           kind="reviewed" if tier == "reviewed" else "mention")
    return sc


def receipt(s, po, key, day, binding="portal.app_receipt"):
    rc = s.obj(f"Receipt:{binding}:{key}")
    s.link("Receipt.receives", rc, po)
    if day:
        s.event("Receipt.posted" if binding == "bc.purchase_receipts" else "Receipt.received",
                rc, day)
    return rc


def line(s, po, key, ordered, received, binding="portal.app_purchase_order_line", due=None,
         unit="each"):
    pol = s.obj(f"PurchaseOrderLine:{binding}:{key}")
    s.val(pol, "ordered_quantity", "quantity", ordered, unit=unit, role="ordered")
    if received is not None:
        s.val(pol, "received_quantity", "quantity", received, unit=unit, role="received")
    if due:
        s.val(pol, "bc_expected_receipt_date", "date", due)
    s.link("PurchaseOrderLine.line_of", pol, po)
    return pol


def fm_twin(s, po, due, key="fm-1"):
    """An FM copy of a Portal PO holding its planned arrival (datearrive)."""
    fm = s.obj(f"PurchaseOrder:portal.fm_generated_purchase_order:{key}", "fm_bronze")
    if due:
        s.event("PurchaseOrder.fm_expected", fm, due)
    s.link("PurchaseOrder.same_record_as", po, fm, kind="same_record")
    return fm


def tools_at(store, now=None):
    base = ot.OntologyTools(ot.StoreReader(store.conn), tp.POLICY, schema=tp.SCHEMA,
                            contracts=CONTRACTS, clock=lambda: now or tp.NOW)
    return op.ProcessTools(base, tp.GRAPHS)


def at(day, hour):
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00")


class Base(tp.Mixin, unittest.TestCase):
    def run_process(self, store, root, actor=STAFF, process=PROCESS, now=None):
        args = {"from": root, **({"process": process} if process else {})}
        return self.answer(tools_at(store, now), args, actor).result

    def late(self, store, root, actor=STAFF, now=None):
        result = self.run_process(store, root, actor, now=now)
        (entry,) = result["lateness"]
        self.assertEqual((entry["stage"], entry["root"]), ("receipt", root))
        return entry


# --------------------------------------------------------------------------- due dates

class DueDateTests(Base):
    def test_a_po_whose_only_due_date_is_sage_exparrival_is_late_after_it(self):
        s = tp.Store()
        sage = sage_po(s, due="2099-01-15")
        # a Sage-only PO is unknown, not open: no verdict (decision 2026-10-21-po-open-sage-fm)
        entry = self.late(s, sage)
        self.assertEqual((entry["state"], entry["late"], entry["gap"]["reason"]),
                         ("undetermined", None, "status_unknown"))
        self.assertNotIn("at_risk", entry)
        po = portal_po(s)
        s.link("PurchaseOrder.same_record_as", po, sage, kind="same_record")
        # the due day is not over until it has ended in every time zone (UTC-12)
        entry = self.late(s, po, now=at("2099-01-16", 11))
        self.assertEqual((entry["state"], entry["late"], entry["measured_on"]),
                         ("not_due", False, "2099-01-15"))
        entry = self.late(s, po, now=at("2099-01-16", 13))
        self.assertEqual((entry["state"], entry["late"], entry["days_late"]), ("late", True, 1))
        entry = self.late(s, po)
        self.assertEqual((entry["state"], entry["days_late"], entry["governing"],
                          entry["governing_date"], entry["measured_against"]),
                         ("late", 17, "expected", "2099-01-15", "today"))
        (cited,) = entry["values"]
        self.assertEqual((cited["property"], cited["value"], cited["authority_rank"]),
                         ("sage_expected", "2099-01-15", 0))
        self.assertTrue(cited["citation"]["l1_handle"].startswith("api://sage.po_header/"))
        # the Portal root's document sources carry item-level permissions: none found
        self.assertEqual(entry["chain"], "no_confirmation_found > not_received")

    def test_fm_and_sage_dates_outrank_portal_and_bc_and_every_one_is_cited(self):
        s = tp.Store()
        po = portal_po(s, expected="2099-01-25")
        fm_twin(s, po, "2099-01-10")
        twin = bc_po(s, expected="2099-01-05")
        s.link("PurchaseOrder.same_record_as", po, twin, kind="same_record")
        entry = self.late(s, po)
        self.assertEqual((entry["governing"], entry["governing_date"], entry["days_late"]),
                         ("expected", "2099-01-10", 22))
        self.assertEqual([(v["property"], v["value"], v["authority_rank"])
                          for v in entry["values"]],
                         [("fm_expected", "2099-01-10", 0), ("expected", "2099-01-25", 1),
                          ("bc_expected", "2099-01-05", 2)])
        self.assertNotIn("expected_dates_disagree", entry["reasons"])

    def test_disagreeing_dates_of_the_governing_tier_decide_only_outside_their_range(self):
        s = tp.Store()
        po = portal_po(s, expected="2099-01-30")
        fm_twin(s, po, "2099-01-10", key="fm-1")
        fm_twin(s, po, "2099-01-20", key="fm-2")
        entry = self.late(s, po)
        self.assertEqual((entry["state"], entry["days_late"]), ("late", 12))  # past both
        self.assertIn("expected_dates_disagree", entry["reasons"])
        self.assertNotIn("governing_date", entry)
        entry = self.late(s, po, now=at("2099-01-15", 12))
        self.assertEqual((entry["state"], entry["late"], entry["gap"]["reason"]),
                         ("undetermined", None, "due_dates_disagree"))
        self.assertNotIn("measured_on", entry)
        entry = self.late(s, po, now=at("2099-01-08", 12))
        self.assertEqual((entry["state"], entry["late"]), ("not_due", False))

    def test_no_planned_date_anywhere_and_no_promise_is_a_gap_never_on_time(self):
        s = tp.Store()
        po = portal_po(s)
        entry = self.late(s, po)
        self.assertEqual((entry["state"], entry["late"], entry["gap"]["reason"]),
                         ("undetermined", None, "no_due_date"))
        # the document sources carry item-level permissions: none found, not proven none
        self.assertEqual(entry["reasons"], ["no_expected_date", "no_confirmation_found"])
        self.assertIn("promises_unproven", {g["reason"] for g in entry["gaps"]})


# --------------------------------------------------------------------------- promises

class PromiseTests(Base):
    def test_a_confirmation_delivery_date_is_the_promise_and_a_later_one_a_revision(self):
        s = tp.Store()
        po = portal_po(s)
        fm_twin(s, po, "2099-01-15")
        confirm(s, po, "a", "2099-01-20", "2099-01-02")
        entry = self.late(s, po)
        self.assertEqual(entry["promised_date"], "2099-01-20")
        self.assertNotIn("revised_date", entry)
        self.assertEqual((entry["governing"], entry["days_late"]), ("expected", 17))
        self.assertIn("promise_slipped", entry["reasons"])  # promised past the due date
        self.assertNotIn("no_confirmation", entry["reasons"])
        promised = [v for v in entry["values"] if v["property"] == "promised"]
        self.assertEqual([v["value"] for v in promised], ["2099-01-20"])
        self.assertTrue(promised[0]["citation"]["l1_handle"])
        # a later confirmation (a SharePoint copy) states a later date: the revision
        confirm(s, po, "b", "2099-01-28", "2099-01-08", binding=FILES)
        entry = self.late(s, po)
        self.assertEqual((entry["promised_date"], entry["revised_date"]),
                         ("2099-01-20", "2099-01-28"))
        self.assertEqual([(v["property"], _text(v["value"])) for v in entry["values"][1:]],
                         [("promised", "2099-01-20"), ("promised_on_file", "2099-01-28")])
        self.assertEqual(entry["chain"], "promise_slipped > not_received")
        # a later document pulling the date in is the revision too
        s = tp.Store()
        po = portal_po(s)
        fm_twin(s, po, "2099-01-15")
        confirm(s, po, "a", "2099-01-28", "2099-01-02")
        confirm(s, po, "b", "2099-01-20", "2099-01-08")
        entry = self.late(s, po)
        self.assertEqual((entry["promised_date"], entry["revised_date"]),
                         ("2099-01-28", "2099-01-20"))

    def test_a_promise_never_stands_in_for_a_missing_requested_by_date(self):
        s = tp.Store()
        po = portal_po(s)
        confirm(s, po, "a", "2099-01-20", "2099-01-02")
        confirm(s, po, "b", "2099-01-28", "2099-01-08")
        entry = self.late(s, po)
        # the requested-by date is the reference or it is reported missing (Vince)
        self.assertEqual((entry["state"], entry["late"], entry["gap"]["reason"]),
                         ("undetermined", None, "no_due_date"))
        for key in ("governing", "governing_date", "measured_on", "at_risk", "days_late"):
            self.assertNotIn(key, entry)
        self.assertEqual(entry["reasons"], ["no_expected_date", "promise_slipped"])
        self.assertEqual((entry["promised_date"], entry["revised_date"]),
                         ("2099-01-20", "2099-01-28"))

    def test_late_at_risk_or_neither_against_the_requested_by_date(self):
        def entry_for(promise, expected="2099-02-10", status="Pending", ordered=("10", "0"),
                      received_on=None):
            s = tp.Store()
            po = portal_po(s, status=status, expected=expected, ordered=ordered)
            if promise:
                confirm(s, po, "a", promise, "2099-01-02")
            if received_on:
                receipt(s, po, "rc-1", received_on)
            return self.late(s, po)

        # not received, the requested-by date not passed, the supplier confirmed later
        risky = entry_for("2099-02-15")
        self.assertEqual((risky["state"], risky["late"], risky["at_risk"]),
                         ("not_due", False, True))
        self.assertIn("promise_slipped", risky["reasons"])
        # confirmed on or before the requested-by date: neither
        for promise in ("2099-02-10", "2099-02-05", None):
            entry = entry_for(promise)
            self.assertEqual((entry["state"], entry["at_risk"]), ("not_due", False), promise)
        # the requested-by date has passed and not fully received: late, whatever the promise
        for ordered in (("10", "0"), ("10", "4")):
            late = entry_for("2099-02-15", expected="2099-01-20", status="Complete",
                             ordered=ordered)
            self.assertEqual((late["state"], late["late"], late["at_risk"]),
                             ("late", True, False), ordered)
        # fully received on time: neither, whatever the promise said
        done = entry_for("2099-02-15", expected="2099-01-20", status="Complete",
                         ordered=("10", "10"), received_on="2099-01-18")
        self.assertEqual((done["state"], done["late"], done["at_risk"]),
                         ("on_time", False, False))

    def test_undated_promises_that_differ_are_no_revision(self):
        s = tp.Store()
        po = portal_po(s)
        fm_twin(s, po, "2099-01-15")
        confirm(s, po, "a", "2099-01-20", "2099-01-02")
        confirm(s, po, "b", "2099-01-28", None)
        entry = self.late(s, po)
        self.assertIn("promises_disagree", entry["reasons"])
        self.assertNotIn("promised_date", entry)
        self.assertNotIn("revised_date", entry)
        self.assertEqual(entry["days_late"], 17)  # the PO's own date still decides

    def test_a_lead_link_is_never_a_promise_nor_proof_of_no_confirmation(self):
        s = tp.Store()
        po = portal_po(s)
        fm_twin(s, po, "2099-01-15")
        confirm(s, po, "a", "2099-01-20", "2099-01-02", tier="lead")
        entry = self.late(s, po)
        self.assertNotIn("promised_date", entry)
        self.assertIn("confirmation_lead_only", entry["reasons"])
        self.assertFalse({"no_confirmation", "no_confirmation_found"} & set(entry["reasons"]))
        self.assertNotIn("2099-01-20", json.dumps(entry))


# --------------------------------------------------------------------------- receipts

class ReceiptTests(Base):
    def test_a_full_receipt_is_measured_on_its_date(self):
        s = tp.Store()
        po = portal_po(s, status="Complete", expected="2099-01-10", ordered=("10", "10"))
        receipt(s, po, "rc-1", "2099-01-12")
        entry = self.late(s, po)
        self.assertEqual((entry["state"], entry["days_late"], entry["measured_against"],
                          entry["received_date"]), ("late", 2, "received", "2099-01-12"))
        self.assertNotIn("not_received", entry["reasons"])
        s = tp.Store()
        po = portal_po(s, status="Complete", expected="2099-01-10", ordered=("10", "10"))
        receipt(s, po, "rc-1", "2099-01-08")
        entry = self.late(s, po)
        self.assertEqual((entry["state"], entry["late"]), ("on_time", False))

    def test_a_partial_receipt_stays_measured_on_today(self):
        s = tp.Store()
        po = portal_po(s, status="B.Order", expected="2099-01-10", ordered=None)
        receipt(s, po, "rc-1", "2099-01-09")
        line(s, po, "l1", "100", "40")
        line(s, po, "l2", "50", "50")
        entry = self.late(s, po)
        self.assertEqual((entry["state"], entry["days_late"], entry["measured_against"]),
                         ("late", 22, "today"))
        self.assertIn("partial_receipt", entry["reasons"])
        self.assertEqual([ln["state"] for ln in entry["lines"]], ["partial", "received"])
        cited = entry["lines"][0]["values"]
        self.assertEqual({(v["property"], v["value"], v["unit"]) for v in cited},
                         {("ordered_quantity", "100", "each"),
                          ("received_quantity", "40", "each")})

    def test_a_bc_po_is_late_on_its_requested_receipt_date_never_a_lines_expected_one(self):
        # BC's line expectedReceiptDate follows the promised date when one is set: it is
        # the supplier's date, never the requested-by reference (requestedReceiptDate)
        def bc_world(requested):
            s = tp.Store()
            po = bc_po(s, fully_received="false", expected=requested)
            receipt(s, po, "rc-1", "2099-01-12", binding="bc.purchase_receipts")
            line(s, po, "bl1", "100", "100", binding="bc.purchase_order_lines",
                 due="2099-01-10")
            line(s, po, "bl2", "100", "0", binding="bc.purchase_order_lines",
                 due="2099-03-01")
            return s, po

        s, po = bc_world("2099-01-20")
        entry = self.late(s, po)
        self.assertEqual((entry["state"], entry["late"], entry["governing_date"],
                          entry["days_late"]), ("late", True, "2099-01-20", 12))
        self.assertIn("partial_receipt", entry["reasons"])   # some lines in, some not
        self.assertEqual({(v["property"], v["value"]) for v in entry["values"]
                          if v.get("authority_rank") is not None},
                         {("bc_expected", "2099-01-20")})   # requestedReceiptDate
        lines = {ln["line"].rsplit(":", 1)[1]: ln for ln in entry["lines"]}
        self.assertEqual([(lines[k]["state"], lines[k]["expected_date"]) for k in ("bl1", "bl2")],
                         [("received", "2099-01-10"), ("not_received", "2099-03-01")])
        for ln in lines.values():
            for key in ("due_date", "late", "days_late"):
                self.assertNotIn(key, ln)
        # no requested date: reported missing, the lines' expected dates never stand in
        s, po = bc_world(None)
        entry = self.late(s, po)
        self.assertEqual((entry["state"], entry["late"], entry["gap"]["reason"]),
                         ("undetermined", None, "no_due_date"))
        self.assertNotIn("governing_date", entry)
        # the stage's own due (overdue) reads the requested date alone
        (due,) = [d for d in self.run_process(s, po)["due"] if d["stage"] == "receipt"]
        self.assertNotIn("due_by", due)

    def test_quantities_in_different_units_decide_no_line_state_nor_the_po(self):
        s = tp.Store()
        po = bc_po(s, expected="2099-01-10")
        pol = s.obj("PurchaseOrderLine:bc.purchase_order_lines:l1")
        s.val(pol, "ordered_quantity", "quantity", "100", unit="each", role="ordered")
        s.val(pol, "received_quantity", "quantity", "1", unit="case", role="received")
        s.link("PurchaseOrderLine.line_of", pol, po)
        entry = self.late(s, po)
        self.assertEqual(entry["lines"][0]["state"], "unknown")
        self.assertNotIn("partial_receipt", entry["reasons"])
        # a Portal PO whose lines are in two units is unknown by the shared definition
        s = tp.Store()
        po = portal_po(s, expected="2099-01-10", ordered=None)
        pol = s.obj("PurchaseOrderLine:portal.app_purchase_order_line:l1")
        s.val(pol, "ordered_quantity", "quantity", "100", unit="each", role="ordered")
        s.val(pol, "received_quantity", "quantity", "1", unit="case", role="received")
        s.link("PurchaseOrderLine.line_of", pol, po)
        entry = self.late(s, po)
        self.assertEqual((entry["state"], entry["late"], entry["gap"]["reason"]),
                         ("undetermined", None, "status_unknown"))

    def test_a_receipt_without_a_readable_date_states_no_verdict(self):
        s = tp.Store()
        po = portal_po(s, status="Complete", expected="2099-01-10", ordered=("10", "10"))
        receipt(s, po, "rc-1", None)
        entry = self.late(s, po)
        self.assertEqual((entry["state"], entry["late"], entry["gap"]["reason"]),
                         ("undetermined", None, "received_date_unproven"))


def _text(value):
    return value.get("untrusted_text") if isinstance(value, dict) else value


class NotIssuedTests(Base):
    def test_cancelled_draft_and_bc_draft_pos_past_their_date_are_never_late(self):
        cases = []
        for status in ("Cancel", "CANCEL", "DRAFT"):
            s = tp.Store()
            po = portal_po(s, status=status, expected="2099-01-10")
            cases.append((s, po, "status", status))
        s = tp.Store()
        po = bc_po(s, fully_received="false", status="Draft", expected="2099-01-10")
        cases.append((s, po, "bc_status", "Draft"))
        for s, po, prop, status in cases:
            entry = self.late(s, po)
            self.assertEqual((entry["state"], entry["late"], entry["at_risk"], entry["reasons"]),
                             ("not_applicable", False, False, ["not_issued_or_closed"]), status)
            self.assertNotIn("days_late", entry)
            (because,) = entry["because"]
            self.assertEqual(because["reason"], "not_issued_or_closed")
            self.assertEqual([(v["property"], _text(v["value"])) for v in because["values"]],
                             [(prop, status)])
            self.assertTrue(because["values"][0]["citation"]["l1_handle"])

    def test_an_fm_draft_is_no_issued_po_in_either_process(self):
        # the FileMaker header is read through the shared definition, never by existence:
        # DRAFT is not issued, Cancel is ignored, an issued word is issued, an unmapped
        # one leaves the stage unknown
        for status, state, gap in (("DRAFT", "missing", ("absent_in_source", "status_not_reached")),
                                   ("draft", "missing", ("absent_in_source", "status_not_reached")),
                                   ("Pending", "present", None),
                                   ("Complete", "present", None),
                                   ("Revised", "missing", ("unknown", "status_unreadable"))):
            for process in (PROCESS, "purchase_to_invoice"):
                s = tp.Store()
                fm = s.obj("PurchaseOrder:portal.fm_generated_purchase_order:fm-1",
                           "fm_bronze")
                s.val(fm, "fm_status", "text", status)
                s.event("PurchaseOrder.issued", fm, "2099-01-02")
                stage = tp.stages(self.run_process(s, fm, process=process))["purchase_order"]
                self.assertEqual(stage["state"], state, (status, process))
                missing = stage.get("missing")
                self.assertEqual(missing and (missing["state"], missing["reason"]), gap,
                                 (status, process))

    def test_a_received_po_is_still_measured(self):
        s = tp.Store()
        po = bc_po(s, fully_received="true", expected="2099-01-10")
        receipt(s, po, "rc-1", "2099-01-12", binding="bc.purchase_receipts")
        entry = self.late(s, po)
        self.assertEqual((entry["state"], entry["days_late"]), ("late", 2))


class CoverageTests(Base):
    def test_a_receipt_with_no_status_or_lines_saying_it_covers_the_order_decides_nothing(self):
        s = tp.Store()
        po = portal_po(s, expected="2099-01-10")      # its lines say nothing received
        receipt(s, po, "rc-1", "2099-01-08")
        entry = self.late(s, po)
        self.assertEqual((entry["state"], entry["late"], entry["gap"]["reason"]),
                         ("undetermined", None, "receipt_coverage_unproven"))
        self.assertNotIn("not_received", entry["reasons"])
        # before the due date it is not due either way
        entry = self.late(s, po, now=at("2099-01-09", 12))
        self.assertEqual((entry["state"], entry["late"]), ("not_due", False))
        # line quantities covering every line make it fully received: on time
        s = tp.Store()
        po = portal_po(s, status="Complete", expected="2099-01-10", ordered=("10", "10"))
        receipt(s, po, "rc-1", "2099-01-08")
        entry = self.late(s, po)
        self.assertEqual((entry["state"], entry["late"], entry["received_date"]),
                         ("on_time", False, "2099-01-08"))

    def test_partial_receipt_and_promise_slipped_cite_their_source_values(self):
        s = tp.Store()
        po = portal_po(s, status="B.Order", ordered=None)
        fm_twin(s, po, "2099-01-15")
        receipt(s, po, "rc-1", "2099-01-09")
        line(s, po, "l1", "100", "40")
        confirm(s, po, "a", "2099-01-20", "2099-01-02")
        entry = self.late(s, po)
        because = {b["reason"]: b["values"] for b in entry["because"]}
        self.assertEqual(set(because), {"partial_receipt", "promise_slipped"})
        self.assertEqual([(v["property"], _text(v["value"])) for v in because["partial_receipt"]][:1],
                         [("status", "B.Order")])
        self.assertIn(("received_quantity", "40"),
                      {(v["property"], _text(v["value"])) for v in because["partial_receipt"]})
        self.assertEqual({(v["property"], _text(v["value"])) for v in because["promise_slipped"]},
                         {("promised", "2099-01-20"), ("fm_expected", "2099-01-15")})


# --------------------------------------------------------------------------- readers

class ReaderTests(Base):
    def test_a_restricted_reader_sees_no_bc_date_and_no_marked_promise(self):
        s = tp.Store()
        po = portal_po(s, expected="2099-01-25")
        twin = bc_po(s, expected="2099-01-05")
        s.link("PurchaseOrder.same_record_as", po, twin, kind="same_record")
        confirm(s, po, "a", "2099-01-29", "2099-01-02", markings='["finance"]')
        staff = self.late(s, po)
        self.assertEqual((staff["governing_date"], staff["promised_date"]),
                         ("2099-01-25", "2099-01-29"))
        self.assertIn("2099-01-05", json.dumps(staff))
        result = self.run_process(s, po, SALES)
        text = json.dumps(result)
        for hidden in ("2099-01-05", "2099-01-29", "bc-1", "bc.purchase_orders"):
            self.assertNotIn(hidden, text)
        (sales,) = result["lateness"]
        self.assertEqual((sales["state"], sales["late"], sales["gap"]["state"]),
                         ("undetermined", None, "inaccessible"))
        self.assertNotIn("no_confirmation", sales.get("reasons", []))
        self.assertIn(tt.WITHHELD, {g["reason"] for g in sales.get("gaps", [])})

    def test_a_marked_promise_that_would_govern_is_never_stated(self):
        s = tp.Store()
        po = portal_po(s)
        confirm(s, po, "a", "2099-01-20", "2099-01-02", markings='["finance"]')
        sales = self.late(s, po, SALES)
        self.assertEqual((sales["state"], sales["late"]), ("undetermined", None))
        self.assertNotIn("2099-01-20", json.dumps(sales))
        self.assertNotIn("no_confirmation", sales.get("reasons", []))


# --------------------------------------------------------------------------- choice

class UnnamedCallTests(Base):
    def chosen(self, store, root, actor=STAFF):
        return self.run_process(store, root, actor, process=None)["process"]

    def test_an_unnamed_call_on_an_open_po_returns_po_delivery_window(self):
        other = "purchase_to_invoice"
        for status, ordered, expected in (
                ("Pending", ("10", "0"), PROCESS), ("B.Order", ("10", "4"), PROCESS),
                (" pending ", ("10", "0"), PROCESS),
                ("Complete", ("10", "4"), PROCESS),          # FM Complete alone never closes
                ("Complete", ("10", "10"), other), ("Pending", ("10", "10"), other),
                ("DRAFT", ("10", "0"), other), ("Cancel", ("10", "0"), other),
                ("Revised", ("10", "0"), other), ("Generated", ("10", "0"), other),
                ("SOMETHING_NEW", ("10", "0"), other), (None, ("10", "0"), other),
                ("Pending", None, other)):                   # no lines: quantities unknown
            s = tp.Store()
            po = portal_po(s, status=status, expected="2099-01-10", ordered=ordered)
            self.assertEqual(self.chosen(s, po), expected, (status, ordered))
        s = tp.Store()
        po = portal_po(s, expected="2099-01-10")
        result = self.run_process(s, po, process=None)
        self.assertEqual(result["lateness"][0]["state"], "late")

    def test_bc_open_means_not_fully_received_and_not_draft(self):
        for fully, status, expected in (("false", "Open", PROCESS),
                                        ("true", "Open", "purchase_to_invoice"),
                                        ("false", "Draft", "purchase_to_invoice"),
                                        (None, "Open", "purchase_to_invoice")):
            s = tp.Store()
            po = bc_po(s, fully_received=fully, status=status)
            self.assertEqual(self.chosen(s, po), expected, (fully, status))

    def test_a_withheld_status_or_line_asks_for_the_process_a_sage_po_keeps_the_implicit(self):
        # Fail closed: a reader that may not read the status or the lines never gets a
        # graph other than the full-grant reader's (it was purchase_to_invoice here while
        # staff got po_delivery_window); it is asked to name the process.
        def refused(store, root, actor, policy=tp.POLICY):
            base = ot.OntologyTools(ot.StoreReader(store.conn), policy, schema=tp.SCHEMA,
                                    contracts=CONTRACTS, clock=lambda: tp.NOW)
            result = op.ProcessTools(base, tp.GRAPHS).process({"from": root}, actor).result
            self.assertEqual(result["error"]["code"], "ambiguous_process", result)
            self.assertNotIn("Pending", json.dumps(result))
            return result

        s = tp.Store()
        po = portal_po(s, status_markings='["finance"]')
        self.assertEqual(self.chosen(s, po, STAFF), PROCESS)
        refused(s, po, SALES)
        self.assertEqual(self.run_process(s, po, SALES, process=PROCESS)["process"], PROCESS)
        no_lines = ot.ReadPolicy("plx", {"api:*": frozenset({"staff"}),
                                         "api:public.app_purchase_order_line": frozenset()})
        refused(s, po, STAFF, no_lines)
        # a status every reader reads that decides alone needs no lines: closed, not
        # issued, or unknown by the word itself (unmapped, missing) whatever the lines say
        for status in ("Cancel", "DRAFT", "Revised", None):
            s = tp.Store()
            po = portal_po(s, status=status)
            base = ot.OntologyTools(ot.StoreReader(s.conn), no_lines, schema=tp.SCHEMA,
                                    contracts=CONTRACTS, clock=lambda: tp.NOW)
            self.assertEqual(self.answer(op.ProcessTools(base, tp.GRAPHS), {"from": po},
                                         STAFF).result["process"], "purchase_to_invoice",
                             status)
            self.assertEqual(self.chosen(s, po, STAFF), "purchase_to_invoice", status)
        s = tp.Store()
        self.assertEqual(self.chosen(s, sage_po(s)), "purchase_to_invoice")
        # an FM header alone carries no received quantity: Complete is unknown, not open
        s = tp.Store()
        fm = s.obj("PurchaseOrder:portal.fm_generated_purchase_order:fm-9", "fm_bronze")
        s.val(fm, "fm_status", "text", "Complete")
        self.assertEqual(self.chosen(s, fm), "purchase_to_invoice")


class TwinTests(Base):
    """An FM root is read with its settled same_record_as twins, as the proactive engine
    reads one PO (``po_status.record`` per record, ``combine`` across them): the FM header
    alone has no received quantity, its Portal twin's lines decide."""

    def world(self, fm_word="Pending", portal_word="Pending", tier="verified",
              status_markings="[]", link_markings="[]"):
        s = tp.Store()
        po = portal_po(s, status=portal_word, status_markings=status_markings)
        fm = s.obj("PurchaseOrder:portal.fm_generated_purchase_order:fm-1", "fm_bronze")
        s.val(fm, "fm_status", "text", fm_word)
        s.event("PurchaseOrder.fm_expected", fm, "2099-01-10")
        s.link("PurchaseOrder.same_record_as", po, fm, tier=tier, kind="same_record")
        if link_markings != "[]":
            s.conn.execute("UPDATE links SET markings = ? WHERE link_type = ?",
                           (link_markings, "PurchaseOrder.same_record_as"))
        return s, po, fm

    def chosen(self, store, root, actor=STAFF):
        return self.run_process(store, root, actor, process=None)["process"]

    def test_an_fm_root_is_open_and_late_by_its_settled_portal_twin(self):
        s, po, fm = self.world()
        self.assertEqual(self.chosen(s, fm), PROCESS)
        entry = self.late(s, fm)
        self.assertEqual((entry["state"], entry["late"], entry["days_late"]),
                         ("late", True, 22))
        # both roots of the one PO get one verdict
        self.assertEqual(self.late(s, po)["state"], "late")

    def test_a_twin_that_is_no_settled_identity_decides_nothing(self):
        for tier in ("corroborated", "lead"):
            s, po, fm = self.world(tier=tier)
            self.assertEqual(self.chosen(s, fm), "purchase_to_invoice", tier)
            entry = self.late(s, fm)
            self.assertEqual((entry["state"], entry["late"]), ("undetermined", None), tier)

    def test_twins_whose_classes_disagree_are_disputed_never_late(self):
        s, po, fm = self.world(fm_word="DRAFT")
        for root in (fm, po):
            self.assertEqual(self.chosen(s, root), "purchase_to_invoice")
            entry = self.late(s, root)
            self.assertEqual((entry["state"], entry["late"]), ("undetermined", None))

    def test_one_ten_and_sixty_twins_in_a_chain_are_each_read(self):
        for n in (1, 10, 60):
            s = tp.Store()
            po = portal_po(s)
            previous = po
            for i in range(n):
                fm = s.obj(f"PurchaseOrder:portal.fm_generated_purchase_order:fm-{i}",
                           "fm_bronze")
                s.val(fm, "fm_status", "text", "DRAFT" if i == n - 1 else "Pending")
                s.link("PurchaseOrder.same_record_as", previous, fm, kind="same_record")
                previous = fm
            started = time.monotonic()
            # the last twin of the chain says DRAFT: reached, it disputes the open Portal
            # record from either end
            for root in (po, previous):
                self.assertEqual(self.chosen(s, root), "purchase_to_invoice", n)
                self.assertEqual(self.late(s, root)["state"], "undetermined", n)
            self.assertLess(time.monotonic() - started, 20, n)

    def test_a_twin_status_or_identity_hidden_from_the_reader_asks_for_the_process(self):
        def refused(store, root, actor):
            result = tools_at(store).process({"from": root}, actor).result
            self.assertEqual(result["error"]["code"], "ambiguous_process", result)
            self.assertNotIn("Pending", json.dumps(result))

        s, po, fm = self.world(status_markings='["finance"]')
        self.assertEqual(self.chosen(s, fm, STAFF), PROCESS)
        refused(s, fm, SALES)
        s, po, fm = self.world(link_markings='["finance"]')
        self.assertEqual(self.chosen(s, fm, STAFF), PROCESS)
        refused(s, fm, SALES)


# --------------------------------------------------------------------------- declarations

class DeclarationTests(unittest.TestCase):
    def test_a_lateness_or_open_when_declaration_the_schema_does_not_back_fails(self):
        graph = copy.deepcopy(tp.GRAPHS[PROCESS])
        due = next(st for st in graph["stages"] if "due" in st)["due"]
        due["lateness"]["precedence"] = [["PurchaseOrder.expected"]]
        due["lateness"]["received"]["event_types"] = ["PurchaseOrder.expected"]
        due["lateness"]["lines"]["ordered"] = "bc_expected_receipt_date"
        graph["open_when"]["rules"][0]["property"] = "bc_status"
        problems = op.check_graph(graph, tp.SCHEMA)
        for text in ("lateness precedence must rank each of its event_types exactly once",
                     "PurchaseOrder.expected is not an event of Receipt",
                     "PurchaseOrderLine.bc_expected_receipt_date is not a quantity property",
                     "PurchaseOrder.bc_status is not read from portal.app_purchase_order"):
            self.assertTrue(any(text in p for p in problems), (text, problems))
        # a graph keeps no status word list of its own: the schema refuses one
        for broken in ({"open": ["PENDING"]}, {"surprise": True}):
            bad = copy.deepcopy(graph)
            bad["open_when"]["rules"][0].update(broken)
            with tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / f"{PROCESS}.json").write_text(json.dumps(bad), "utf-8")
                with self.assertRaises(Exception, msg=broken):
                    op.load_graphs(tmp, schema=tp.SCHEMA)

    def test_plx_process_names_every_graph_and_plx_schema_lists_processes(self):
        description = CONTRACTS["tools"]["plx.process"]["description"]
        for gid in tp.GRAPHS:
            self.assertIn(gid, description)
        store = tp.Store()
        base = ot.OntologyTools(ot.StoreReader(store.conn), tp.POLICY, schema=tp.SCHEMA,
                                contracts=CONTRACTS, clock=lambda: tp.NOW)
        bare = base.call("plx.schema", {"section": "processes"}, STAFF)
        self.assertEqual(bare["processes"], [])   # no plx.process served: none listed
        op.ProcessTools(base, tp.GRAPHS)
        out = base.call("plx.schema", {"section": "processes"}, STAFF)
        self.assertEqual(validate(out, CONTRACTS["tools"]["plx.schema"]["output"],
                                  root=CONTRACTS), [])
        listed = {p["id"]: p for p in out["processes"]}
        self.assertEqual(sorted(listed), sorted(tp.GRAPHS))
        self.assertEqual((listed[PROCESS]["implicit"], listed[PROCESS]["chosen_when_open"]),
                         (False, True))
        self.assertIn("receipt", listed[PROCESS]["stages"])
        scoped = base.call("plx.schema", {"object_type": "PurchaseOrder",
                                          "section": "processes"}, STAFF)
        self.assertEqual(sorted(p["id"] for p in scoped["processes"]),
                         [PROCESS, "purchase_to_invoice"])
        everything = base.call("plx.schema", {}, STAFF)
        self.assertEqual(validate(everything, CONTRACTS["tools"]["plx.schema"]["output"],
                                  root=CONTRACTS), [])


# --------------------------------------------------------------------------- the plan

class LatePoPlanTests(unittest.TestCase):
    def table(self, store):
        table: dict = {}
        base = ot.OntologyTools(ot.StoreReader(store.conn), tp.POLICY, schema=tp.SCHEMA,
                                contracts=CONTRACTS, clock=lambda: tp.NOW)
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        op.register(table, op.ProcessTools(base, tp.GRAPHS))
        oi.register(table, oi.InvestigateTools(table, schema=tp.SCHEMA, rows=None))
        return table

    def investigate(self, table, root, actor=STAFF):
        out = table["plx.investigate"]["handler"]({"family": "F14", "root": root}, actor)
        self.assertEqual(validate(out, CONTRACTS["tools"]["plx.investigate"]["output"],
                                  root=CONTRACTS), [], json.dumps(out)[:2000])
        self.assertNotIn("error", out, json.dumps(out)[:2000])
        return out

    def test_the_stored_plan_validates(self):
        plan = json.loads((tp.ROOT / "ontology/plans/late_po.json").read_text("utf-8"))
        self.assertEqual(oi.check_plan(plan, tp.SCHEMA, CONTRACTS), [])

    def test_late_po_answers_which_pos_are_late_by_how_much_and_why_with_citations(self):
        s = tp.Store()
        po = portal_po(s)
        fm_twin(s, po, "2099-01-15")
        confirm(s, po, "a", "2099-01-20", "2099-01-02")
        out = self.investigate(self.table(s), po)
        entries = {e["name"]: e for e in out["answer"]}
        self.assertEqual(entries["po_1"]["value"], "PO200001")
        self.assertIs(entries["late_1"]["value"], True)
        self.assertEqual(entries["days_late_1"]["value"], 17)
        self.assertEqual(entries["why_1"]["value"], "promise_slipped > not_received")
        cells = entries["late_1"]["cells"]
        self.assertIn(("fm_expected", "2099-01-15"),
                      {(c["property"], c["value"]) for c in cells})
        self.assertTrue(all(c["citation"]["l1_handle"] for c in cells))


    def test_late_po_never_lists_a_cancelled_po_as_late(self):
        s = tp.Store()
        po = portal_po(s, status="Cancel", expected="2099-01-10")
        out = self.investigate(self.table(s), po)
        entries = {e["name"]: e for e in out["answer"]}
        self.assertIsNot(entries["late_1"]["value"], True)
        self.assertNotIn("days_late_1", {n for n, e in entries.items()
                                         if e.get("value") is not None})
        self.assertEqual(entries["why_1"]["value"], "not_issued_or_closed")

    def test_a_po_root_reads_its_chain_and_a_supplier_root_its_pos(self):
        s = tp.Store()
        po = portal_po(s, expected="2099-01-10")
        s.val(po, "expected_date", "date", "2099-01-10")
        fm_twin(s, po, "2099-01-15")
        line(s, po, "l1", "10", "4", binding="bc.purchase_order_lines", due="2099-01-18")
        s.val(confirm(s, po, "a", "2099-01-20", "2099-01-02"), "delivery_date", "date",
              "2099-01-20")
        freight = s.obj("FreightBill:graph_mail.freight_bills:f1", "mail")
        s.val(freight, "ship_date", "date", "2099-01-12")
        s.link("FreightBill.for_purchase_order", freight, po, tier="corroborated", kind="mention")
        rc = receipt(s, po, "r1", "2099-01-25")
        s.val(rc, "received_date", "date", "2099-01-25")
        supplier = s.obj("Supplier:portal.app_supplier:sup-1")
        s.link("PurchaseOrder.ordered_from", po, supplier)
        table = self.table(s)
        out = self.investigate(table, po)
        values = {re.sub(r"_\d+$", "", e["name"]): e for e in out["answer"]
                  if e.get("value") is not None}
        self.assertEqual({n: values[n]["value"] for n in (
            "need_date", "line_expected_date", "confirmed_delivery_date", "freight_ship_date",
            "received_date") if n in values}, {
            "need_date": "2099-01-10", "line_expected_date": "2099-01-18",
            "confirmed_delivery_date": "2099-01-20", "freight_ship_date": "2099-01-12",
            "received_date": "2099-01-25"}, json.dumps(out["answer"])[:2000])
        for name in ("line_expected_date", "confirmed_delivery_date", "freight_ship_date",
                     "received_date"):
            self.assertTrue(values[name]["citation"]["l1_handle"], name)
        types = {o["object_type"] for o in out["chain"]["objects"]}
        self.assertLessEqual({"PurchaseOrder", "PurchaseOrderLine", "SupplierConfirmation",
                              "FreightBill", "Receipt"}, types)
        self.assertEqual(sum(o["object_type"] == "PurchaseOrder"
                             for o in out["chain"]["objects"]), 2)  # the PO and its twin
        # a supplier root keeps the per-PO summary: its PO's verdict, no chain reads
        whole = self.investigate(table, supplier)
        self.assertEqual({e["name"]: e["value"] for e in whole["answer"]}["po_1"], "PO200001")
        self.assertNotIn("FreightBill", {o["object_type"] for o in whole["chain"]["objects"]})
        # one plan answers F14, and every stored plan loads (plans_invalid is empty)
        self.assertEqual([p.id for p in oi.load_plans(schema=tp.SCHEMA).values()
                          if "F14" in p.doc["families"]],
                         ["late_po"])

# --------------------------------------------------------------------------- cardinality

class CardinalityTests(Base):
    def test_one_ten_and_sixty_receipts_lines_and_confirmations(self):
        for n in (1, 10, 60):
            s = tp.Store()
            po = portal_po(s, status="B.Order", expected="2099-01-10", ordered=None)
            for i in range(n):
                receipt(s, po, f"rc-{i}", f"2099-01-{1 + i % 28:02d}")
                line(s, po, f"l{i:03d}", "10", "5")
                confirm(s, po, f"c{i:03d}", "2099-01-20", f"2099-01-{1 + i % 28:02d}")
            started = time.monotonic()
            entry = self.late(s, po)
            self.assertLess(time.monotonic() - started, 20, n)
            self.assertEqual((entry["state"], entry["days_late"]), ("late", 22), n)
            listed = [ln["line"] for ln in entry["lines"]]
            self.assertEqual(len(listed), min(n, 8), n)
            self.assertEqual(len(set(listed)), len(listed), n)
            self.assertTrue(all(ln["state"] == "partial" for ln in entry["lines"]), n)
            self.assertEqual(entry["values"][0]["property"], "expected", n)  # governing first
            self.assertEqual(len(entry["values"]), min(1 + 2 * n, 10), n)
            if n == 1:
                self.assertEqual([v["property"] for v in entry["values"]],
                                 ["expected", "promised", "received"])
            truncated = {g["reason"] for g in entry.get("gaps", ())}
            self.assertEqual("lines_truncated" in truncated, n > 8, n)
            self.assertEqual("values_truncated" in truncated, 1 + 2 * n > 10, n)
            self.assertEqual(entry.get("promised_date"), "2099-01-20", n)


# --------------------------------------------------------------------------- projection

T = "2099-01-01T00:00:00.000000Z"


class _Source:
    def __init__(self, source, rows):
        self.source, self.rows = source, rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        out = {}
        for rel in relations:
            out[rel] = [CapturedRecord(self.source, rel, json.dumps(key), row,
                                       f"api://{rel}/{json.dumps(key)}@1", "row",
                                       hashlib.sha256(json.dumps(row).encode()).hexdigest(),
                                       T, None, T, f"api:{rel}")
                        for r, key, row in self.rows if r == rel]
        return out


class ProjectionTests(unittest.TestCase):
    def test_sage_exparrival_fm_datearrive_and_a_confirmation_date_project_as_events(self):
        portal = _Source("portal_api", [
            ("sage.po_header", {"porhseq": "77"},
             {"porhseq": "77", "ponumber": "PO100001", "exparrival": "20990115"}),
            ("public.generated_purchase_order", {"id": "9"},
             {"id": "9", "ponumber": "PO100002", "datearrive": "2099-01-18"})])
        mail = _Source("graph_mail", [
            ("documentFields", {"source_key": "sk-1"},
             {"source_key": "sk-1", "profile": "supplier_confirmation",
              "fields": {"delivery_date": {"value": "2099-01-20"},
                         "document_date": {"value": "2099-01-02"}}})])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "store.sqlite"
            build_store(path, load_schema(), {"portal_api": portal, "graph_mail": mail},
                        normalizer=identifiers.normalize,
                        clock=lambda: datetime(2099, 1, 2, tzinfo=timezone.utc))
            conn = sqlite3.connect(str(path))
            try:
                events = {(r[0], r[1], str(r[2])[:10]) for r in conn.execute(
                    "SELECT e.event_type, eo.object_id, e.time FROM events e JOIN "
                    "event_objects eo ON eo.event_id = e.event_id")}
            finally:
                conn.close()
        sage = object_id_of("PurchaseOrder", "portal.sage_po_header", {"porhseq": "77"})
        fm = object_id_of("PurchaseOrder", "portal.fm_generated_purchase_order", {"id": "9"})
        sc = object_id_of("SupplierConfirmation", MAIL, {"source_key": "sk-1"})
        self.assertIn(("PurchaseOrder.sage_expected", sage, "2099-01-15"), events)
        self.assertIn(("PurchaseOrder.fm_expected", fm, "2099-01-18"), events)
        self.assertIn(("SupplierConfirmation.promised", sc, "2099-01-20"), events)


if __name__ == "__main__":
    unittest.main()
