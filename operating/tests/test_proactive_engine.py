"""The scheduled proactive engine (operating/proactive_engine.py) and the Home inbox read.

SYNTHETIC fixtures only: projection stores built with ``StoreBuilder`` from rows written
here (synthetic handles, no business values), a fake grant per reader. Evidence label:
isolated-fixture. ``LiveCheckTest`` runs the live-check command over the injected-transport
world of ``case.customer_po_missing`` (fake Workbench, fake drive; network refused).
"""

from __future__ import annotations

from operating.copy_gate import say_date
import contextlib
from contextlib import closing
from decimal import Decimal
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import io
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from verify import live_verify as lv
from integrations.http import SourceHttpError
from ontology.core import Actor
from ontology import store as ostore
from ontology.store import StoreBuilder
from operating import po_status
from operating import alert_kinds as ak
from operating import proactive_engine as pe
from operating import proactive_engine_budget
from operating import proactive_engine_cards
from operating import proactive_engine_config
from operating import proactive_engine_inbox
from operating import proactive_engine_population
from operating.action_path import ActionPath
from operating.ontology_tools_investigate_follow_ups import DRAFT_ACTION
from operating.cases import CaseStore
from operating.session_identity import CHANNEL_CONSOLE, fixture_session
from operating.tests.test_cases import FakeResolver
from operator_api.workspace import object_workspace as objects

# The cardinality tests keep their row-count checks; the time limit scales with host load so a
# busy shared machine does not fail them (10 s floor, up to 10x that when the load average is high).
CARDINALITY_SECONDS = 5.0 * min(10.0, max(2.0, os.getloadavg()[0] / max(1, os.cpu_count() or 1)))

ROOT = Path(__file__).resolve().parents[2]
AS_OF = date(2026, 10, 19)
T0 = "2026-10-01T00:00:00.000000Z"
QC_LINK = "RelPublicAppIncomingQcRecord.inspects"
ON_HAND = ("InventoryLot", "quantity_on_hand", "InventoryLot.of_item")   # not served yet
OPS = Actor("plx", frozenset({"ops"}))


def handle(n: int) -> str:
    return f"api://public.fixture_rows/%7B%22id%22%3A{n}%7D@g1"


class World:
    """Rows for one synthetic store: records, their one current version and values, links."""

    def __init__(self) -> None:
        self.rows = {"objects": [], "object_versions": [], "object_values": [], "links": [],
                     "events": [], "event_objects": []}
        self.n = 0

    def obj(self, object_type: str, values: dict, *, audience: str = "api:a",
            markings: str = "[]", binding: str = "fx") -> str:
        self.n += 1
        oid = f"{object_type}:{binding}:{self.n}"
        self.rows["objects"].append({
            "object_id": oid, "object_type": object_type, "binding_id": binding,
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

    def link(self, link_type: str, a: str, b: str, tier: str = "verified", *,
             reason: str = "[\"fixture\"]") -> None:
        self.rows["links"].append({
            "link_id": f"l{len(self.rows['links'])}", "link_type": link_type,
            "from_object": a, "to_object": b, "tier": tier,
            "binding_kind": "fk" if tier != "lead" else "mention", "reason": reason,
            "evidence_handles": json.dumps([handle(1)]), "why_not": "[]", "markings": "[]",
            "recorded_at": T0})

    def event(self, event_type: str, oid: str, time: str) -> None:
        """One event of a record (``Email.received`` at its time)."""
        eid = f"e{len(self.rows['events'])}"
        self.rows["events"].append({
            "event_id": eid, "event_type": event_type, "time_basis": "source_recorded",
            "time": time, "l1_handle": handle(1), "field_path": "row.time", "digest": eid,
            "lineage": "workbench", "markings": "[]", "recorded_at": T0})
        self.rows["event_objects"].append({"event_id": eid, "object_id": oid,
                                           "qualifier": "subject"})

    def build(self, path: Path) -> Path:
        builder = StoreBuilder(path)
        for table in ("objects", "object_versions", "object_values", "links", "events",
                      "event_objects"):
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
    # a back order with an outstanding quantity is open, its FileMaker twin decides nothing
    ids["overdue"] = po(w, "B.Order", "2026-10-05", lines=(("10", "4"),))
    w.link("SupplierConfirmation.confirms",
           w.obj("SupplierConfirmation", {"delivery_date": "2026-10-04"}), ids["overdue"])
    fm_twin = w.obj("PurchaseOrder", {"fm_status": "B.Order", "expected_date": "2026-10-05"})
    w.link("PurchaseOrder.same_record_as", ids["overdue"], fm_twin)
    ids["overdue_root"] = min(ids["overdue"], fm_twin)
    ids["line_dated"] = w.obj("PurchaseOrder", {"status": "Pending"})   # no requested-by date
    w.link("PurchaseOrderLine.line_of",
           w.obj("PurchaseOrderLine", {"bc_expected_receipt_date": "2026-10-02",
                                       "ordered_quantity": ("10", "ea")}),
           ids["line_dated"])
    twin_a = po(w, "Pending", "2026-10-03")
    twin_b = po(w, "Pending", "2026-10-25")
    # disputed: one rank's need dates disagree and today falls between them
    w.link("PurchaseOrder.same_record_as", twin_a, twin_b)
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
    open_po = ids["undated"] = w.obj("PurchaseOrder", {"status": "Pending"})   # no date
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
    # one source keeps only the whole boxes of the other's 32.5: one quantity, not raised
    halves = (w.obj("ReceiptLine", {"received_quantity": ("32.5", "box", "received")}),
              w.obj("ReceiptLine", {"received_quantity": ("32", "box", "received")},
                    audience="api:b"))
    w.link("ReceiptLine.same_record_as", *halves)
    # a real difference on a record one copy says is cancelled: nothing to act on
    w.link("AssemblyOrder.same_record_as",
           w.obj("AssemblyOrder", {"ordered_pieces": ("50000", "each"), "status": "Allocated"}),
           w.obj("AssemblyOrder", {"ordered_pieces": ("65000", "each"), "status": "CANCELLED"},
                 audience="api:b"))
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


# The people exclusion of the fixture tenant: one person of ours cleared, one excluded.
OURS_OID = "00000000-0000-4000-8000-0000000000a1"
EXCLUDED_OID = "00000000-0000-4000-8000-0000000000e1"


def people(*, unavailable: bool = False):
    from integrations.people_exclusion import PeopleExclusion
    from operating.chunking.permissions import Exclusion

    guard = (PeopleExclusion(unavailable="excluded_people_oids_absent") if unavailable
             else PeopleExclusion([EXCLUDED_OID]))
    return Exclusion(guard, directory={"ops@synth-tenant.example": OURS_OID,
                                       "boss@synth-tenant.example": EXCLUDED_OID},
                     internal_domains=["synth-tenant.example"])


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
        # a read counts relative days to the business day; the fixtures' day is AS_OF
        today = mock.patch.object(pe, "_today", return_value=AS_OF)
        today.start()
        self.addCleanup(today.stop)
        # The memory gate reads the runner's own cgroup: a test run inside a capped scope
        # (testslot, the landing batch) would queue the rules one at a time and fail the
        # timing budgets for a reason the test is not about. A run here has no limit; the
        # gate's own tests pass their gate or an environment (``for_process(environ)``).
        held = pe._MemoryGate.for_process

        def unlimited(environ=None):
            if environ is not None:
                return held(environ)
            return pe._MemoryGate(lambda: None, lambda: None)
        gate = mock.patch.object(pe._MemoryGate, "for_process", unlimited)
        gate.start()
        self.addCleanup(gate.stop)

    def run_engine(self, world: World, coverage=None, *, at=None, **options) -> dict:
        world.build(self.store)
        moment = at or datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
        options.setdefault("cadence", {})   # a test not about cadence runs every rule
        return pe.run(self.store, self.inbox, AS_OF, coverage=coverage,
                      qc_link_types=options.pop("qc_link_types", (QC_LINK,)),
                      on_hand_sources=options.pop("on_hand_sources", (ON_HAND,)),
                      clock=lambda: moment, people=options.pop("people", people()),
                      **options)

    def read(self, *, reader="plx:ops", may_read=everyone, held=ALL_MARKINGS, decide=admit,
             **options) -> dict:
        # detector tests read what the rules found, whether or not the alert registry shows
        # the type to a reader (``include_off``); the reader-facing filter is tested apart
        options.setdefault("include_off", True)
        return pe.read_inbox(self.inbox, self.store, reader=reader, may_read=may_read,
                             held=held, decide=decide, **options)

    def test_a_quantity_only_an_older_record_still_holds_is_superseded_not_a_conflict(self):
        """The newest system of record wins: a Portal manufacturing order edited after the
        legacy FileMaker assembly order it replaced states the quantity; the legacy value is
        superseded, no risk. Without an edit time on every side nothing is decided."""
        w = World()
        newer = w.obj("AssemblyOrder", {"ordered_pieces": ("400", "each"),
                                        "portal_updated_at": "2026-11-05T14:00:00"})
        older = w.obj("AssemblyOrder", {"ordered_pieces": ("770", "each"),
                                        "fm_modified_at": "2026-10-28T09:00:00Z"},
                      audience="api:b")
        w.link("AssemblyOrder.same_record_as", newer, older)
        undated = w.obj("AssemblyOrder", {"ordered_pieces": ("10", "each")})
        dated = w.obj("AssemblyOrder", {"ordered_pieces": ("12", "each"),
                                        "fm_modified_at": "2026-10-28T09:00:00Z"},
                      audience="api:b")
        w.link("AssemblyOrder.same_record_as", undated, dated)
        summary = self.run_engine(w)
        found = [g["object_id"] for g in self.read()["gaps"]
                 if g["kind"] == "conflicting_quantity"]
        self.assertEqual(found, [min(undated, dated)])
        self.assertEqual(summary["not_evaluated"]["conflicting_quantity"],
                         {"superseded_by_newer_record": 1})


class DetectorTest(Engine):

    def test_each_detector_finds_exactly_its_seeded_gaps_set_wise_with_citations(self):
        world, ids = seeded()
        summary = self.run_engine(world, ids["coverage"])
        found = {(g["kind"], g["object_id"]) for g in self.read()["gaps"]}
        conflict_root = min(ids["conflict_a"], ids["conflict_b"])
        self.assertEqual(found, {
            ("unconfirmed_po", ids["unconfirmed"]), ("late_po", ids["late_with_lead"]),
            ("unconfirmed_po", ids["line_dated"]),   # no header date: its open line's
            ("late_po", ids["confirmed_late"]), ("late_po", ids["overdue_root"]),
            ("po_receipt_quantity_mismatch", ids["over"]),
            ("po_receipt_quantity_mismatch", ids["short"]),
            ("conflicting_quantity", conflict_root),
            ("receipt_without_qc", ids["no_qc"]),
            ("undated_po", ids["undated"]),   # open, no due date: its own group, never late
            ("order_without_customer_po", ids["no_customer_po"]),
            ("expired_quote", ids["expired"])})
        # a reader's unnarrowed read (Home, plx.find's home mode) holds only kinds a
        # registered alert type names: a kind no one measured never reaches home, while a
        # page that names the kind still reads it (business rule 11-11)
        home = {(g["kind"], g["object_id"]) for g in self.read(include_off=False)["gaps"]}
        unmeasured = {k for k, _ in found} - ak.registered_gap_kinds()
        self.assertTrue({"receipt_without_qc", "conflicting_quantity"} <= unmeasured)
        hidden = unmeasured | ak.off_gap_kinds()
        self.assertEqual(home, {(k, o) for k, o in found if k not in hidden})
        self.assertIn("late_po", {k for k, _ in home})
        self.assertEqual(
            self.read(include_off=False, kinds=["receipt_without_qc"])["gaps"][0]["object_id"],
            ids["no_qc"])
        self.assertEqual(summary["scanned"], world.n)   # the whole store, not a sample
        self.assertEqual((summary["gaps_found"], summary["gaps_cited"],
                          summary["gaps_uncited"]), (12, 12, 0))
        self.assertFalse(summary["scope_truncated"])
        self.assertEqual(summary["detectors"]["card_rules"], "not_configured")
        self.assertEqual(summary["not_evaluated"]["late_or_unconfirmed_po"],
                         {"confirmation_unsettled": 1, "need_date_disputed": 1,
                          "no_need_date": 1, "status_missing": 1, "status_unmapped": 1})
        # the Sage PO complete in Sage is a closed historical record, never evaluated
        self.assertEqual(summary["po_lateness"], {
            "pos": 16, "classes": {"open": 9, "closed": 4, "draft": 1, "unknown": 2},
            "evaluated": 6, "late": 4, "at_risk": 1, "unconfirmed": 2, "undated": 1,
            "historic": 0, "open_by_read": 9, "open_other": 4,
            "closed_elsewhere": 0, "received_header_open": 0, "received_without_receipt": 0})
        self.assertEqual(summary["not_evaluated"]["po_receipt_quantity_mismatch"],
                         {"receipt_link_unsettled": 1, "sources_unreconciled": 1,
                          "unit_differs": 1})
        self.assertEqual(summary["not_evaluated"]["receipt_without_qc"],
                         {"qc_link_unsettled": 1})
        self.assertEqual(summary["not_evaluated"]["conflicting_quantity"],
                         {"integer_of_decimal": 1, "record_closed": 1})
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
        summary = self.run_engine(world, None, qc_link_types=())
        # the schema declares FileMaker's receiving QC decision as the one QC relation
        self.assertEqual(pe.declared_qc_links(), proactive_engine_config.QC_LINKS)
        self.assertEqual(summary["detectors"]["receipt_without_qc"], "blocked")
        # the seeded store holds QC records: only the missing relation is said
        self.assertEqual(summary["detector_reasons"]["receipt_without_qc"],
                         ["qc_link_not_declared"])
        bare = World()
        bare.obj("Receipt", {"received_date": "2026-10-01"})
        self.store = self.dir / "no-qc.sqlite"
        self.inbox = pe.inbox_path(self.store)
        none = self.run_engine(bare, None, qc_link_types=(), only=["receipt_without_qc"])
        # no QC record of any kind in the store: said, and still blocked (never "none")
        self.assertEqual(none["detectors"]["receipt_without_qc"], "blocked")
        self.assertEqual(none["detector_reasons"]["receipt_without_qc"],
                         ["qc_link_not_declared", "no_qc_record_captured"])
        self.assertEqual(summary["detectors"]["order_without_customer_po"], "not_configured")
        self.assertNotIn("receipt_without_qc", summary["gaps_by_kind"])

    def test_fm_receiving_qc_decision_link_settles_the_receipt_and_sage_bc_are_not_judged(self):
        qc = proactive_engine_config.QC_LINKS[0]
        w = World()
        fm = "portal.fm_receipt"

        def receipt(binding, day="2026-10-10"):
            return w.obj("Receipt", {"received_date": day}, binding=binding)

        def line(of, binding):
            ln = w.obj("ReceiptLine", {"lab_id": "x"}, binding=binding)
            w.link("ReceiptLine.line_of", ln, of)
            return ln

        decided = receipt(fm)
        w.link(qc, w.obj("Lot", {}, binding="portal.fm_receipt_raw_material.lotnoqc"),
               line(decided, "portal.fm_receipt_raw_material"))
        waiting = receipt(fm)                           # Quarantine/blank: no link, a real gap
        line(waiting, "portal.fm_receipt_raw_material")
        sage, bc = receipt("portal.sage_po_receipt_header"), receipt("bc.purchase_receipts")
        line(sage, "portal.sage_po_receipt_header")
        tied = receipt("portal.sage_po_receipt_header")   # Sage receipt tied to a decided FM line
        sage_line = line(tied, "portal.sage_po_receipt_header")
        fm_line = w.obj("ReceiptLine", {}, binding="portal.fm_receipt_raw_material")
        w.link("ReceiptLine.same_record_as", sage_line, fm_line)
        w.link(qc, w.obj("Lot", {}, binding="portal.fm_receipt_raw_material.lotnoqc"), fm_line)
        tied_open = receipt("bc.purchase_receipts")      # tied to an FM line with no decision
        open_line = line(tied_open, "bc.purchase_receipts")
        w.link("ReceiptLine.same_record_as", open_line,
               w.obj("ReceiptLine", {}, binding="portal.fm_receipt_packaging"))
        lead = receipt(fm)                                # a lead-tier link is no decision
        w.link(qc, w.obj("Lot", {}, binding="portal.fm_receipt_raw_material.lotnoqc"),
               line(lead, "portal.fm_receipt_raw_material"), "lead")
        summary = self.run_engine(w, None, qc_link_types=proactive_engine_config.QC_LINKS,
                                  only=["receipt_without_qc"])
        gapped = {g["object_id"] for g in self.read()["gaps"] if g["kind"] == "receipt_without_qc"}
        self.assertEqual(gapped, {waiting, tied_open})
        self.assertEqual(summary["not_evaluated"]["receipt_without_qc"],
                         {"no_qc_source_for_system": 2, "qc_link_unsettled": 1})
        self.assertNotIn(bc, gapped)

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
        # an issued order open by its own status (the one PO definition): its lines late
        order = w.obj("PurchaseOrder", {"po_number": "PO-77", "bc_status": "Open",
                                        "bc_fully_received": "false"}, audience="api:a")
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
                         (proactive_engine_config.DRAFT_LABEL, "draft_not_sent", False))
        self.assertTrue(draft["text"].startswith("Draft supplier message (not sent)"))
        self.assertNotIn("promis", draft["text"].lower())
        self.assertIsNone(re.search(r"\[\d+\]", draft["text"]))
        self.assertEqual(len(set(draft["citations"])), len(draft["citations"]))
        self.assertLessEqual(len(draft["citations"]), proactive_engine_config.MAX_DRAFT_CITATIONS)
        self.assertLessEqual(len(draft["text"]), proactive_engine_config.MAX_DRAFT_TEXT)
        for fact in draft["facts"]:
            self.assertNotIn("ref", fact)
            self.assertIn(fact["citation"], draft["citations"])
            # a date is written "Wed 12 Nov" (operating.copy_gate.say_date)
            self.assertTrue(fact["value"] in draft["text"]
                            or say_date(fact["value"]) in draft["text"], fact)


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
        self.assertIn(f"- Resin 0: our expected date {say_date('2026-10-01')}, ordered 100 kg, received 40 kg",
                      draft["text"])
        self.assertIn(f"- Resin 1: our expected date {say_date('2026-10-02')}, ordered 100 kg, received 40 kg",
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
        self.assertIn(f"Your confirmation gives the delivery date {say_date('2026-10-25')}", draft["text"])
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
        self.assertIn(f"our expected date {say_date('2026-10-01')}", draft["text"])
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
                self.assertIn(f"our expected date {say_date('2026-10-01')}", draft["text"])
                self.assertNotIn("2026-10-25", draft["text"])
                if not refuse:   # the inbox's own draft is the same wording
                    self.assertEqual(gap["follow_up"]["draft"]["text"], draft["text"])

    def test_the_draft_says_why_a_number_or_supplier_is_not_named(self):
        def world(*, number=True, suppliers=1, name=True, mail=False, other=False,
                  customer=False):
            w = World()
            # open by its own status (the one PO definition); its number read last
            order = w.obj("PurchaseOrder", {"bc_status": "Open", "bc_fully_received": "false", **({"po_number": "PO-9"} if number
                                                    else {"x": "1"})})
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
                order = w.obj("PurchaseOrder", {"bc_status": "Open", "bc_fully_received": "false", "po_number": "PO-5"})
                for i in range(n):
                    line = w.obj("PurchaseOrderLine", {
                        "bc_expected_receipt_date": f"2026-09-{1 + i % 28:02d}",
                        "ordered_quantity": ("9", "kg"), "received_quantity": ("1", "kg"),
                        "description": f"Item {i}"})
                    w.link("PurchaseOrderLine.line_of", line, order)
                start = time.monotonic()
                draft = self.draft_of(w)
                self.assertLess(time.monotonic() - start, CARDINALITY_SECONDS)
                self.assert_worded(draft)
                listed = min(n, proactive_engine_config.MAX_DRAFT_LINES)
                self.assertEqual((draft["lines_late"], draft["lines_listed"]), (n, listed))
                rows = [f["line_id"] for f in draft["facts"] if f["fact"] == "expected_receipt"]
                self.assertEqual(len(set(rows)), listed)
                self.assertEqual(draft["text"].count("\n- "), listed + (listed < n))
                if listed < n:
                    self.assertIn(f"- The other {n - listed} late lines are not listed here",
                                  draft["text"])

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
                for i in range(n):
                    order = w.obj("PurchaseOrder", {"po_number": "PO", "bc_status": "Open",
                                                    "bc_fully_received": "false"})
                    wanted.add(order)
                    # each order its own past day: one day on a bulk share of the lines is
                    # a sync default, dating nothing (``source_defaults``)
                    day = (date(2026, 9, 1) + timedelta(days=i % 28)).isoformat()
                    for _line in range(2):
                        line = w.obj("PurchaseOrderLine", {
                            "bc_expected_receipt_date": day,
                            "ordered_quantity": ("9", "kg"), "received_quantity": ("1", "kg")})
                        w.link("PurchaseOrderLine.line_of", line, order)
                start = time.monotonic()
                self.run_engine(w)
                shown = [g for g in self.read(limit=1000)["gaps"] if g["kind"] == "late_order"]
                self.assertLess(time.monotonic() - start, CARDINALITY_SECONDS)
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
        # a Postgres statement cut at its timeout, or a Postgres error, says so by class,
        # never by its message (R8: three rules failed with nothing but OperationalError)
        for error, why in ((sqlite3.OperationalError("interrupted"), "statement_timeout"),
                           (sqlite3.OperationalError("postgres: TooManyConnections: SYNTH-x"),
                            "postgres_TooManyConnections")):
            with mock.patch.object(pe, "_late_or_unconfirmed_po", side_effect=error):
                failed = self.run_engine(world, ids["coverage"])
            self.assertEqual(failed["detectors"]["late_or_unconfirmed_po"], "failed")
            self.assertEqual(failed["detector_reasons"]["late_or_unconfirmed_po"],
                             ["OperationalError", why])
            self.assertNotIn("SYNTH-x", json.dumps(failed))
        shown = self.read()
        self.assertIn("unconfirmed_po", {g["kind"] for g in shown["gaps"]})
        self.assertEqual(shown["unread"], {"late_or_unconfirmed_po_failed": 1})
        self.assertTrue(shown["truncated"])
        # its findings are the last good run's, never current: each says the check failed
        # and when, and when it was last checked
        mine = [g for g in shown["gaps"] if g["kind"] == "unconfirmed_po"]
        self.assertTrue(mine)
        for gap in mine:
            self.assertEqual(gap["carried"]["reason"], "check_failed")
            self.assertIn("check failed at 2026-10-19 2:00 AM ET", gap["summary"])
        self.assertEqual(set(shown["failed_at"]), {"late_or_unconfirmed_po"})
        self.assertNotIn("carried", next(g for g in shown["gaps"]
                                         if g["kind"] == "expired_quote"))

    def test_a_scheduled_run_with_rules_that_did_not_run_is_never_read_as_clean(self):
        # The store-only hook runs without the mirror coverage and without a card caller:
        # those rules did not run (QC runs: the schema declares FileMaker's QC link), so the read is incomplete
        # and names them, never a clean "nothing found".
        # the fixture tenant's excluded person, never the host's own exclusion: the hook
        # and priority_fill_unmet read it by ``_people`` (the passes' env file without
        # one: a host lacking it blocks every people-gated rule); the state holds no
        # mail directory, as a store-only run's
        from integrations import people_exclusion as px
        excluded = mock.patch.dict(os.environ, {px.ENV_IDS: EXCLUDED_OID})
        excluded.start()
        self.addCleanup(excluded.stop)
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
                                            "margin_erosion": "not_configured",
                                            # the schema (schema-next, L126) declares the
                                            # on-hand source, assembly order dates and MC
                                            # task / production due dates: those rules run;
                                            # lot expiry is still not declared
                                            "expiring_lot_stock": "blocked",
                                            # no tenant mail directory beside the store
                                            "vendor_no_reply": "blocked",
                                            "customer_chase": "blocked",
                                            "commitments": "blocked",
                                            "needs_reply": "blocked",
                                            "expedite_requests": "blocked",
                                            "supplier_shortage": "blocked",
                                            "payment_chase": "blocked",
                                            "return_requests": "blocked",
                                            "approvals_waiting": "blocked",
                                            "chase_escalation": "blocked",
                                            "internal_chases": "blocked",
                                            "party_chases": "blocked",
                                            "inbox_triage": "blocked",
                                            "label_changes": "blocked",
                                            "label_revisions": "blocked",
                                            "rd_projects": "blocked",
                                            "vendor_terms_unapplied": "not_configured",
                                            "negative_margin": "not_configured"})
        # every rule run: each one the schema serves is complete, and the read is complete
        # only when every rule is (a rule blocked until the schema declares its fields
        # keeps it incomplete and is named, never read as clean); the schema (L126) now
        # declares the MC task and production due dates and the assembly order dates
        schema_blocked = {}
        def card(_tool, arguments):
            return {"object": {"object_id": arguments["object_id"]}, "leads": [],
                    "conflicts": []}
        self.run_engine(world, _ids["coverage"], call=card)
        full = self.read()
        self.assertEqual(set(full["detectors"]), set(pe.DETECTORS))
        self.assertEqual((full["complete"], full["not_run"], full["truncated"]),
                         (not schema_blocked, schema_blocked, False), full["detectors"])
        # the sheet rules wait for the reading step: a setup state, never incomplete
        self.assertEqual({d: s for d, s in full["detectors"].items()
                          if s in pe.SETUP_STATES},
                         {"cash_application": "not_set_up",
                          "statement_reconciliation": "not_set_up"})
        self.assertEqual({d for d, state in full["detectors"].items()
                          if state not in ("complete", *pe.SETUP_STATES)},
                         set(schema_blocked))
        # a rule the inbox holds no row for (a run before it existed) did not run either
        with contextlib.closing(sqlite3.connect(self.inbox)) as conn, conn:
            conn.execute("DELETE FROM detector_runs WHERE detector = 'expired_quote'")
        gone = self.read()
        self.assertEqual((gone["complete"], gone["not_run"]),
                         (False, {**schema_blocked, "expired_quote": "not_run"}))

    def test_a_spent_time_budget_is_truncated_never_complete_and_keeps_gaps(self):
        world, ids = seeded()
        self.run_engine(world, ids["coverage"])
        before = {g["id"] for g in self.read()["gaps"]}
        now = [0.0]

        def slow(_tool, arguments):   # the first tool call spends the whole budget
            now[0] = 61.0
            return {"object": {"object_id": arguments.get("object_id")}, "leads": [],
                    "conflicts": []}
        def swallowing(_st, _as_of, call, _bound, first=(), many=None,  # a failed call is
                       cache=None):
            for _ in range(2):    # "undetermined" (as impact_chain), the scan complete
                with contextlib.suppress(Exception):
                    call("plx.process", {"object_id": "x"})
            return pe._result([])
        with mock.patch.object(pe, "_orders_at_risk", swallowing):
            summary = self.run_engine(world, ids["coverage"], call=slow,
                                      budget=pe.Budget(60, monotonic=lambda: now[0]))
        # a rule whose call was refused, even swallowed, or that did not start is
        # truncated for the budget; the rules run before it are complete
        for name in ("orders_at_risk", "card_rules"):
            self.assertEqual(summary["detectors"][name], "truncated", name)
            self.assertIn(pe.TIME_BUDGET, summary["detector_reasons"][name])
        self.assertEqual(summary["detectors"]["late_or_unconfirmed_po"], "complete")
        self.assertTrue(summary["truncated"])
        found = self.read()
        self.assertIs(found["complete"], False)
        self.assertTrue(before <= {g["id"] for g in found["gaps"]})   # none resolved
        # a hook whose budget is spent before it starts runs nothing as complete
        hook = pe.after_build(self.store, as_of=AS_OF, budget_seconds=0,
                              scheduled=lambda as_of, budget: pe.run(
                                  self.store, self.inbox, as_of, coverage=ids["coverage"],
                                  call=slow, budget=budget))
        self.assertEqual(set(hook["detectors"].values()), {"truncated"})
        self.assertEqual(hook["detector_reasons"]["order_without_customer_po"],
                         [pe.TIME_BUDGET])

    def test_detectors_side_by_side_find_what_one_thread_finds_and_one_slow_stops_alone(self):
        """The store-only detectors on threads (``run(threads=)``) find exactly what they
        find one after another; a detector whose own window ends (``DETECTOR_SECONDS``) is
        truncated while every other runs to complete."""
        world, ids = seeded()
        serial = self.run_engine(world, ids["coverage"], threads=1)
        found = {g["id"] for g in self.read()["gaps"]}
        side = self.run_engine(world, ids["coverage"], threads=4)
        self.assertEqual((side["detectors"], side["gaps_found"]),
                         (serial["detectors"], serial["gaps_found"]))
        self.assertEqual({g["id"] for g in self.read()["gaps"]}, found)

        def endless(st, _as_of):   # a query that never ends on its own
            st.conn.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) "
                            "SELECT count(*) FROM n").fetchone()
        with mock.patch.object(pe, "_expired_quote", endless):
            slow = self.run_engine(world, ids["coverage"], threads=4,
                                   budget=pe.Budget(600), detector_seconds={"expired_quote": 0.5})
        self.assertEqual(slow["detectors"]["expired_quote"], "truncated")
        self.assertEqual(slow["detector_reasons"]["expired_quote"], [pe.TIME_BUDGET])
        self.assertEqual({k: v for k, v in slow["detectors"].items() if k != "expired_quote"},
                         {k: v for k, v in serial["detectors"].items() if k != "expired_quote"})
        self.assertLess(slow["seconds"]["expired_quote"], 30)

    def test_over_the_memory_share_detectors_run_one_at_a_time_and_none_is_lost(self):
        """CC-P-04: a run whose cgroup is at or over ``MEMORY_GATE_SHARE`` of its limit
        starts no detector beside a running one (``_MemoryGate``), and still runs every
        detector to the same findings; under the share they run side by side."""
        world, ids = seeded()
        serial = self.run_engine(world, ids["coverage"], threads=1)
        found = {g["id"] for g in self.read()["gaps"]}
        full = pe._MemoryGate(lambda: 95, lambda: 100, poll=0.01)
        held = self.run_engine(world, ids["coverage"], threads=4, memory=full)
        self.assertEqual((held["detectors"], held["gaps_found"]),
                         (serial["detectors"], serial["gaps_found"]))
        self.assertEqual({g["id"] for g in self.read()["gaps"]}, found)
        self.assertEqual(full.most, 1)
        self.assertGreater(full.held, 0)
        free = pe._MemoryGate(lambda: 5, lambda: 100, reserve=0, poll=0.01)
        side = self.run_engine(world, ids["coverage"], threads=4, memory=free)
        self.assertEqual(side["detectors"], serial["detectors"])
        self.assertEqual(free.held, 0)

    def test_the_memory_gate_counts_what_the_host_has_left(self):
        """The 11-12/13 kills were the host's OOM killer, no unit at its MemoryMax: a run
        whose own cgroup is far under its limit starts no detector beside a running one
        once the host has little left, runs side by side when it has room, and keeps
        its cgroup's limit when the host figure cannot be read."""
        import tempfile
        gib = 1 << 30
        with tempfile.TemporaryDirectory() as tmp:
            cg = Path(tmp) / "cg"
            cg.mkdir()
            (cg / "memory.current").write_text(str(5 * gib))
            (cg / "memory.stat").write_text("anon 1\nfile 0\nshmem 0\n")
            (cg / "memory.high").write_text(str(15 * gib))
            (cg / "memory.max").write_text(str(16 * gib))
            meminfo = Path(tmp) / "meminfo"

            def gate(available_kb):
                if available_kb is None:
                    meminfo.write_text("MemTotal: 64000000 kB\n")
                else:
                    meminfo.write_text(f"MemTotal: 64000000 kB\nMemAvailable: "
                                       f"{available_kb} kB\n")
                with mock.patch.object(pe, "_own_cgroup", lambda: str(cg)):
                    made = pe._MemoryGate.for_process({})
                made.poll = 0.005
                return made
            hold = mock.patch.object(pe, "HOST_MEMINFO", str(meminfo))
            hold.start()
            self.addCleanup(hold.stop)
            # 5 GB used of a 15 GB limit, 2 GB left on the host: one at a time
            low = gate(2 * 1024 * 1024)
            self.assertTrue(low.over())
            self.assertEqual(low.limit(), 7 * gib)
            # 30 GB left: the cgroup's own limit (15 GB) is the lower one
            roomy = gate(30 * 1024 * 1024)
            self.assertFalse(roomy.over())
            self.assertEqual(roomy.limit(), 15 * gib)
            # no MemAvailable line: the cgroup's limit, as before
            self.assertEqual(gate(None).limit(), 15 * gib)
            # every task still runs on a starved host, one at a time (N = 1, 10, 60)
            import threading
            for n in (1, 10, 60):
                starved, done = gate(1024 * 1024), []

                def task(i, g=starved):
                    g.enter()
                    try:
                        done.append(i)
                    finally:
                        g.leave()
                workers = [threading.Thread(target=task, args=(i,)) for i in range(n)]
                for w in workers:
                    w.start()
                for w in workers:
                    w.join(60)
                self.assertEqual((sorted(done), starved.most), (list(range(n)), 1))

    def test_the_memory_gate_admits_every_task_and_fails_closed(self):
        import threading

        def run_tasks(gate, n):
            done = []

            def task(i: int) -> None:
                gate.enter()
                try:
                    time.sleep(0.02)
                    done.append(i)
                finally:
                    gate.leave()
            workers = [threading.Thread(target=task, args=(i,)) for i in range(n)]
            for w in workers:
                w.start()
            for w in workers:
                w.join(60)
            self.assertEqual((sorted(done), gate.running), (list(range(n)), 0))
            return gate.most
        for n in (1, 10, 60):
            with self.subTest(n=n):
                # over, or a use it cannot read: one at a time; none lost
                self.assertEqual(run_tasks(pe._MemoryGate(lambda: 95, lambda: 100, poll=0.005),
                                           n), 1)
                self.assertEqual(run_tasks(pe._MemoryGate(lambda: None, lambda: 100,
                                                          poll=0.005), n), 1)
                if n > 1:
                    # room: side by side; a reserve per running detector caps a cold start
                    # (nothing has grown yet) at what the share holds: 0 + 3 x 30 < 100
                    self.assertGreater(run_tasks(pe._MemoryGate(
                        lambda: 5, lambda: 100, reserve=0, poll=0.005), n), 1)
                    self.assertLessEqual(run_tasks(pe._MemoryGate(
                        lambda: 0, lambda: 100, share=1, reserve=30, poll=0.005), n), 4)
        gate = pe._MemoryGate(lambda: 95, lambda: None, poll=0.005)   # no limit: no wait
        gate.enter()
        gate.enter()
        self.assertEqual(gate.most, 2)
        stopped = pe._MemoryGate(lambda: 95, lambda: 100, poll=0.005)
        stopped.enter()
        stopped.enter(stop=lambda: True)   # the run's time is up: no wait
        self.assertEqual(stopped.running, 2)
        # a cut-off detector still running counts until its thread ends, without holding
        # anyone's turn by itself
        orphan = pe._MemoryGate(lambda: 0, lambda: 100, share=1, reserve=60, poll=0.005)
        ended = threading.Event()
        orphan.enter()
        orphan.abandon(ended)
        orphan.leave()
        orphan.enter()          # alone (the orphan holds no turn): admitted
        late = threading.Thread(target=orphan.enter)
        late.start()
        late.join(0.3)
        self.assertTrue(late.is_alive())   # 0 + (1 running + 1 orphan) x 60 >= 100
        ended.set()
        late.join(10)
        self.assertFalse(late.is_alive())
        self.assertEqual((orphan.running, orphan.orphans), (2, 0))
        with self.assertRaises(ValueError):
            pe._MemoryGate(lambda: 1, lambda: 1, share=0)
        for raw, share in (("0.4", 0.4), ("x", pe.MEMORY_GATE_SHARE),
                           ("7", pe.MEMORY_GATE_SHARE)):
            self.assertEqual(pe._MemoryGate.for_process({pe.MEMORY_SHARE_ENV: raw}).share, share)

    def test_the_gate_reads_its_own_cgroup_without_page_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp, "cgroup")
            proc.write_text("0::/system.slice/plx-proactive.service\n")
            here = Path(tmp, "system.slice", "plx-proactive.service")
            here.mkdir(parents=True)
            directory = pe._own_cgroup(str(proc), tmp)
            self.assertEqual(directory, str(here))
            self.assertIsNone(pe._cgroup_used(directory))      # nothing readable: None
            self.assertIsNone(pe._cgroup_limit(directory))
            (here / "memory.current").write_text("1000\n")
            (here / "memory.stat").write_text("anon 500\nfile 450\nshmem 50\n")
            (here / "memory.high").write_text("max\n")
            (here / "memory.max").write_text("2000\n")
            self.assertEqual(pe._cgroup_used(directory), 600)   # 1000 - (450 - 50)
            self.assertEqual(pe._cgroup_limit(directory), 2000)
            (here / "memory.high").write_text("1500\n")
            self.assertEqual(pe._cgroup_limit(directory), 1500)
            (here / "memory.stat").write_text("anon 500\n")    # no file line: unreadable
            self.assertIsNone(pe._cgroup_used(directory))
            proc.write_text("1:name=systemd:/x\n")             # no v2 line
            self.assertIsNone(pe._own_cgroup(str(proc), tmp))

    def test_the_ranged_audience_scan_reads_what_one_query_reads(self):
        """The run's one audience scan (``_Store.audience_firsts``), split into rowid ranges
        side by side, holds every audience key and its smallest record exactly as one
        GROUP BY over the record table does; the audience keys are its keys."""
        world, _ids = seeded()
        world.build(self.store)
        with closing(sqlite3.connect(self.store)) as conn:
            whole = dict(conn.execute(
                "SELECT audience_key, MIN(object_id) FROM objects GROUP BY audience_key"))
        self.assertGreater(len(whole), 1)
        for threads in (1, 4):
            with mock.patch.object(pe, "PARALLEL_LINK_ROWS", 1), \
                    mock.patch.object(pe, "LINK_PARTS_PER_THREAD", 3):
                st = pe._Store(self.store, threads=threads)
                try:
                    self.assertEqual(st.audience_firsts(), whole)
                    self.assertEqual(st.audience_keys(), set(whole))
                finally:
                    st.close()

    def test_ranged_scans_reuse_their_part_connections(self):
        """CC-P-09: the ranged scans' parts read on connections kept for the run, not one
        opened and closed per range: two scans of 12 ranges on 4 threads open at most 4
        part connections, all closed with the store; a part whose read failed is not
        reused."""
        world, _ids = seeded()
        world.build(self.store)
        opened = []
        real = pe._connect

        def counted(path, file_only=False):
            conn = real(path, file_only)
            opened.append(conn)
            return conn
        with mock.patch.object(pe, "PARALLEL_LINK_ROWS", 1), \
                mock.patch.object(pe, "LINK_PARTS_PER_THREAD", 3), \
                mock.patch.object(pe, "_connect", side_effect=counted):
            st = pe._Store(self.store, threads=4)
            try:
                first = st.audience_firsts()
                again = st._ranged("objects", "o", "SELECT o.object_id FROM objects o "
                                   "WHERE 1", " ORDER BY o.rowid", {}, lambda r: r[0])
            finally:
                st.close()
        self.assertTrue(first)
        self.assertGreater(sum(len(part) for part in again), 0)
        self.assertLessEqual(len(opened), 1 + 4)   # the run's own and at most one a thread
        for conn in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")   # every one closed with the store
        st = pe._Store(self.store, threads=2)
        try:
            bad = st._part_conn()
            st._part_done(bad, False)
            self.assertNotIn(bad, st._parts)
            good = st._part_conn()
            st._part_done(good, True)
            self.assertIs(st._part_conn(), good)
        finally:
            st.close()

    def test_a_full_run_scans_the_link_table_once_and_finds_the_same_gaps(self):
        """Every link type a detector reads is in the run's one up-front scan
        (``_run_link_types``): no detector scans the link table again at its turn (each scan
        is a minute or more on the live store; the price, order-to-cash and data-quality
        rules each scanned it again). The gaps are the ones each rule finds reading its own
        links (an ``only`` run, no up-front scan)."""
        world, ids = seeded()
        scans: list[list[str]] = []
        real = pe._Store._scan_links

        def counted(st, types):
            scans.append(list(types))
            return real(st, types)
        with mock.patch.object(pe._Store, "_scan_links", counted):
            whole = self.run_engine(world, ids["coverage"], threads=1)
        self.assertEqual(len(scans), 1, [sorted(t) for t in scans[1:]])
        found = sorted(json.dumps(g, sort_keys=True) for g in self.read()["gaps"])
        for name in ("price_consistency", "shipped_not_invoiced",
                     "duplicate_customer_order", "recurring_component_damage"):
            self.assertEqual(whole["detectors"][name], "complete", name)
        for path in Path(self.inbox).parent.glob(Path(self.inbox).name + "*"):
            path.unlink()
        with mock.patch.object(pe, "_run_link_types", lambda: []):
            own = self.run_engine(world, ids["coverage"], threads=1)
        self.assertEqual({k: own[k] for k in ("detectors", "gaps_found", "gaps_by_kind")},
                         {k: whole[k] for k in ("detectors", "gaps_found", "gaps_by_kind")})
        self.assertEqual(sorted(json.dumps(g, sort_keys=True) for g in self.read()["gaps"]),
                         found)

    def test_a_detector_that_never_reads_the_store_is_cut_off_at_its_window(self):
        """Every detector has a hard window, with or without a run budget: one stuck in a
        loop that reads nothing (no query to interrupt) is ``truncated`` at its window and
        the grace after it, its lane goes on, and every other detector ends as it would."""
        world, ids = seeded()
        serial = self.run_engine(world, ids["coverage"], threads=1)
        release = threading.Event()

        def stuck(_st, _as_of):
            release.wait(60)   # never reads the store; ends only when the test lets it
            return pe._result([])
        try:
            for threads in (1, 4):
                with mock.patch.object(pe, "_expired_quote", stuck), \
                        mock.patch.object(pe, "HARD_GRACE_SECONDS", 0.2):
                    started = time.monotonic()
                    cut = self.run_engine(world, ids["coverage"], threads=threads,
                                          detector_seconds={"expired_quote": 0.3})
                    self.assertLess(time.monotonic() - started, 30)
                self.assertEqual(cut["detectors"]["expired_quote"], "truncated")
                self.assertEqual(cut["detector_reasons"]["expired_quote"], [pe.TIME_BUDGET])
                self.assertEqual(cut["cut_off"], ["expired_quote"])
                self.assertEqual(
                    {k: v for k, v in cut["detectors"].items() if k != "expired_quote"},
                    {k: v for k, v in serial["detectors"].items() if k != "expired_quote"})
        finally:
            release.set()

    def test_the_document_cards_are_read_on_the_pool_as_one_by_one(self):
        """``card_rules`` with the run's pool asks the same cards as its serial reads, all at
        once; a card the time left did not reach is not read (never a finding)."""
        from operating import proactive_pool

        class St:
            class conn:
                @staticmethod
                def execute(_sql, _params):
                    return [("SupplierInvoice:a",), ("SupplierInvoice:b",)]

            @staticmethod
            def versions(handles):
                return []
        asked = []

        def call(tool, arguments):
            asked.append(("call", tool, arguments["object_id"]))
            return {"object": {"object_id": arguments["object_id"]}, "conflicts": []}

        def many(tool, arguments):
            asked.extend(("many", tool, a["object_id"]) for a in arguments)
            return [{"object": {"object_id": "SupplierInvoice:a"}, "conflicts": []},
                    proactive_pool.NOT_READ]
        with mock.patch.object(pe.pg, "card_candidates",
                               lambda _conn, _as_of: {"SupplierInvoice:a", "SupplierInvoice:b"}):
            serial = pe._card_rules(St, AS_OF, call, 5)
            pooled = pe._card_rules(St, AS_OF, call, 5, many)
        self.assertEqual([a[1:] for a in asked if a[0] == "call"],
                         [a[1:] for a in asked if a[0] == "many"])
        self.assertEqual(serial["status"], "complete")
        self.assertEqual(pooled["gaps"], [])   # the unread card: no finding (the run's
        # ``Budget.pooled`` marks the rule truncated for it)

    def test_a_value_many_documents_state_is_one_gap_per_record_and_closed_orders_drop(self):
        """11-13 judging: ``missing_in_record`` raised one order up to 4 times (once per
        assembly packet stating its date) and 13 of 15 on closed orders. One gap per
        (attested records, field), the newest document's; the order rides on the
        follow-up, so the active-population filter drops a closed one, counted."""
        for n in (1, 10, 60):
            with self.subTest(n=n):
                packets = [f"AssemblyOrderPacket:fx:{i}" for i in range(n * 3)]
                order_of = {p: f"SalesOrder:fx:o{i // 3}" for i, p in enumerate(packets)}

                class St:
                    class conn:
                        @staticmethod
                        def execute(_sql, _params):
                            return [(p,) for p in packets]

                    @staticmethod
                    def versions(handles):
                        return [{"l1_handle": h, "object_id": h.split("#", 1)[1],
                                 "markings": "[]"} for h in handles]

                    @staticmethod
                    def judging(_name):
                        return None

                def call(_tool, arguments):
                    packet = arguments["object_id"]
                    record = order_of[packet]
                    return {"object": {"object_id": packet,
                                       "object_type": "AssemblyOrderPacket"},
                            "conflicts": [],
                            "leads": [{"kind": "missing_in_record",
                                       "subject": "SalesOrder.required_date",
                                       "values": [
                                           {"value": "2026-10-01",
                                            "citation": {"l1_handle": f"doc#{packet}"}},
                                           {"value": None,
                                            "citation": {"l1_handle": f"rec#{record}"}}]}]}
                with mock.patch.object(pe.pg, "_roster",
                                       lambda: ([("AssemblyOrderPacket", "f")], None)), \
                        mock.patch.object(pe.pg, "card_candidates",
                                          lambda _conn, _as_of: set(packets)):
                    result = pe._card_rules(St, AS_OF, call, len(packets))
                gaps = result["gaps"]
                self.assertEqual(len(gaps), n)
                self.assertEqual(result["counts"]["repeats_merged"], 2 * n)
                self.assertEqual(sorted(g["follow_up"][pe._ATTESTS] for g in gaps),
                                 sorted(set(order_of.values())))
                # never the inbox's one-item-per-order key: a late item of that order is
                # neither displaced nor folded by the note
                self.assertFalse([g for g in gaps if pe._ORDER in g["follow_up"]])
                # the newest document (read first) is the one kept
                self.assertEqual({g["object_id"] for g in gaps}, set(packets[::3]))
                closed = {f"SalesOrder:fx:o{i}": "closed" for i in range(0, n, 2)}
                results = {"card_rules": result}
                pe._drop_inactive_orders(St, results, AS_OF, inactive=lambda: closed)
                left = {g["follow_up"][pe._ATTESTS] for g in results["card_rules"]["gaps"]}
                self.assertEqual(left, set(order_of.values()) - set(closed))
                self.assertEqual(results["card_rules"]["not_evaluated"]["closed_order"],
                                 len(closed))

    def test_the_cards_read_are_every_document_a_rule_could_flag_and_no_other(self):
        """AL-04: ``card_rules`` reads the cards of the documents ``card_candidates`` keeps,
        a set-based superset of what ``_detect`` can raise on (a stated value against an
        empty record read; two numbers of one quantity, unit and role; a quote past its
        validity), never the newest ``CARD_BOUND`` of every document. Each kept or dropped
        document below is one way a card can or cannot raise a gap."""
        w = World()
        keep, drop = set(), set()

        def packet(planned, record_values, *, link=True):
            doc = w.obj("AssemblyOrderPacket", {"planned_quantity": planned}
                        if planned is not None else {})
            if link:
                ao = w.obj("AssemblyOrder", record_values)
                w.link("AssemblyOrderPacket.packet_of", doc, ao)
            return doc
        # two numbers of one quantity in one unit and role: a conflict
        keep.add(packet(("10", "each"), {"expected_quantity": ("12", "each")}))
        # the same number written two ways, or in two units: never one group of two numbers
        drop.add(packet(("10", "each"), {"expected_quantity": ("10.0", "each")}))
        drop.add(packet(("10", "each"), {"expected_quantity": ("12", "kg")}))
        # two roles are two quantities
        drop.add(packet(("10", "each", "ordered"), {"expected_quantity": ("12", "each",
                                                                          "produced")}))
        # a stated value against an empty record read: a missing-field lead
        keep.add(packet(("10", "each"), {"expected_quantity": None}))
        # the field not stated at all, or no record attested: nothing to compare
        drop.add(packet(None, {"expected_quantity": ("12", "each")}))
        drop.add(packet(("10", "each"), {}, link=False))
        # a path rule (packet -> allocation -> sales order line -> sales order): a stated
        # customer PO the order holds empty
        doc = w.obj("AssemblyOrderPacket", {"customer_po": "PO-1"})
        ao, alloc = w.obj("AssemblyOrder", {}), w.obj("AssemblyOrderAllocation", {})
        line, so = w.obj("SalesOrderLine", {}), w.obj("SalesOrder", {"customer_po": None})
        w.link("AssemblyOrderPacket.packet_of", doc, ao)
        w.link("AssemblyOrderAllocation.allocates", alloc, ao)
        w.link("AssemblyOrderAllocation.for_order_line", alloc, line)
        w.link("SalesOrderLine.line_of", line, so)
        keep.add(doc)
        # a quote document past its validity, and one still valid
        keep.add(w.obj("ForwarderQuote", {"valid_until": "2026-09-01"}))
        drop.add(w.obj("ForwarderQuote", {"valid_until": "2027-01-01"}))
        w.build(self.store)
        st = pe._Store(self.store)
        try:
            found = pe.pg.card_candidates(st.conn, AS_OF)
            listed = proactive_engine_cards._card_documents(st, AS_OF, 100)
            self.assertEqual(found & (keep | drop), keep)
            self.assertEqual(set(listed), keep)
            self.assertEqual(proactive_engine_cards._card_documents(st, AS_OF, 2), listed[:2])
        finally:
            st.close()

    def test_a_missing_field_note_on_an_order_never_displaces_its_late_item(self):
        """The note carried the order on the inbox's one-item-per-
        order key, so a low note could replace or fold that order's late item. Both show."""
        w = World()
        late = po(w, "Pending", "2026-10-01")
        packet = w.obj("AssemblyOrderPacket", {"required_date": "2026-10-01"})
        n, p = (int(o.rsplit(":", 1)[1]) for o in (late, packet))

        def card_rules(st, *_a, **_k):
            gap = pe._gap("missing_in_record", "card_rules", packet,
                          "AssemblyOrderPacket", "k", summary="A captured document states",
                          missing="Confirm", why="w", priority="medium",
                          about=("AssemblyOrderPacket", ""),
                          groups=[[(handle(p), packet, "[]")], [(handle(n), late, "[]")]])
            gap["follow_up"][pe._ATTESTS] = late
            return pe._result([gap])
        with mock.patch.object(pe, "_card_rules", card_rules):
            self.run_engine(w, call=lambda *_a: {},
                            only=["late_or_unconfirmed_po", "card_rules"])
        shown = self.read()["gaps"]
        kinds = sorted(g["kind"] for g in shown)
        self.assertEqual(len(kinds), 2, kinds)
        self.assertIn("missing_in_record", kinds)
        self.assertTrue({"late_po", "unconfirmed_po"} & set(kinds), kinds)
        self.assertTrue(all(pe._ATTESTS not in g["follow_up"] for g in shown))

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
                self.assertLess(time.monotonic() - start, CARDINALITY_SECONDS)
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


class ServedGenerationTest(Engine):
    """A new projection generation served after the engine's last run: what the older data
    found is never listed as current (the record pages read the new data), the read says
    the checks are re-running and since when; the next run over the new data lists again."""

    def late_ids(self, found: dict) -> set[str]:
        return {g["object_id"] for g in found["gaps"] if g["kind"] == "unconfirmed_po"}

    def test_a_new_generation_marks_older_findings_until_the_engine_runs_over_it(self):
        from operating import briefing as bf
        world, ids = seeded()
        first = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
        self.run_engine(world, ids["coverage"], at=first)
        self.assertIn(ids["unconfirmed"], self.late_ids(self.read()))
        self.assertFalse(pe.served_behind(self.store))
        # the next generation: the supplier confirmed on time, so the record page says
        # "not late"; the engine has not run over it yet
        world.link("SupplierConfirmation.confirms",
                   world.obj("SupplierConfirmation", {"delivery_date": "2026-10-09"}),
                   ids["unconfirmed"])
        world.build(self.store)
        self.assertTrue(pe.served_behind(self.store))
        stale = self.read()
        # the older data's findings stay listed, each marked as not rechecked (never as
        # current, never all hidden)
        self.assertIn(ids["unconfirmed"], self.late_ids(stale))
        self.assertTrue(all(g["carried"]["reason"] == "older_data" for g in stale["gaps"]))
        self.assertTrue(stale["stale"])
        self.assertFalse(stale["complete"])
        self.assertIn("late_or_unconfirmed_po", stale["refreshing"])
        self.assertEqual(stale["checked_on"], first.isoformat())
        brief = bf.read(self.inbox, self.store, reader="plx:ops", may_read=everyone,
                        held=ALL_MARKINGS, decide=admit,
                        routing=bf.Routing(basis="fixture", everything=True))
        self.assertEqual((brief["refreshing"] != [], brief["checked_on"]),
                         (True, first.isoformat()))
        self.assertGreater(brief["count"], 0)
        self.assertEqual(brief["carried"], len(brief["items"]))
        self.assertTrue(all(i["carried"]["reason"] == "older_data" for i in brief["items"]))
        self.assertIn("from an earlier check", brief["today"])
        self.assertIn("last checked 2026-10-19", brief["today"])
        self.assertNotIn("Nothing", brief["today"])
        # the engine runs over the served generation: the list is current again, and the
        # order the new data shows confirmed on time is not on it
        self.run_engine(world, ids["coverage"], at=first + timedelta(hours=2))
        fresh = self.read()
        self.assertFalse(fresh["stale"])
        self.assertEqual((fresh["refreshing"], fresh["checked_on"]), ([], None))
        self.assertNotIn(ids["unconfirmed"], self.late_ids(fresh))
        self.assertIn(ids["overdue_root"], {g["object_id"] for g in fresh["gaps"]})
        self.assertFalse(pe.served_behind(self.store))

    def test_a_postgres_data_version_alone_never_makes_the_rules_behind(self):
        """On Postgres the served data's stamp is ``<generation digest>@<data version>``,
        the version moving with every applied delta layer. The scheduler's checks compare
        the generation only: a new data version leaves every rule current (the incremental
        pass folds the layers; the whole and calling passes run their own rules, not all
        ~70, 11-14), a new generation or an unreadable stamp still makes every rule due."""
        world, ids = seeded()
        self.run_engine(world, ids["coverage"])
        self.assertFalse(pe.served_behind(self.store))
        self.assertEqual(pe.rules_behind(self.store), set())
        conn = sqlite3.connect(self.inbox)
        generation = conn.execute(
            "SELECT store_digest FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()[0]
        conn.close()
        self.assertNotIn("@", generation)

        def served(stamp):
            return mock.patch.object(pe, "_served_digest", lambda conn, store: (
                (stamp,) if stamp is not None else None))
        with served(f"{generation}@1794613852931"):
            self.assertFalse(pe.served_behind(self.store))
            self.assertEqual(pe.rules_behind(self.store), set())
            # a reader's list still says which findings predate the newest layer
            self.assertTrue(self.read()["refreshing"])
        for stamp in ("sha256:another-generation@1794613852931", "@5", None):
            with self.subTest(stamp=stamp), served(stamp):
                self.assertTrue(pe.served_behind(self.store))
                self.assertIn("late_or_unconfirmed_po", pe.rules_behind(self.store))
        # findings recorded at one data version, served at a later one: current
        conn = sqlite3.connect(self.inbox)
        conn.execute("UPDATE runs SET store_digest = ?", (f"{generation}@5",))
        conn.execute("UPDATE meta SET value = json_set(value, '$.digest', ?) "
                     "WHERE key LIKE 'generation:%'", (f"{generation}@5",))
        conn.commit()
        conn.close()
        with served(f"{generation}@6"):
            self.assertFalse(pe.served_behind(self.store))
            self.assertEqual(pe.rules_behind(self.store), set())
        # Postgres the only store: the stamp is asked of Postgres, the inbox is never
        # attached to a copy of the store's objects (minutes per pass on the live host)
        from operating.pg import ontology_reader

        def no_attach(*a, **k):
            raise AssertionError("attached")
        for stamp, behind in ((f"{generation}@7", False), ("sha256:next@1", True),
                              (None, True)):
            with self.subTest(pg_stamp=stamp), \
                    mock.patch.object(ontology_reader, "pg_only", lambda: True), \
                    mock.patch.object(ontology_reader, "change_stamp", lambda: stamp), \
                    mock.patch.object(pe, "_attach", no_attach):
                self.assertEqual(pe.served_behind(self.store), behind)
                self.assertEqual(bool(pe.rules_behind(self.store)), behind)

    def test_a_rule_cut_short_on_new_data_lists_its_older_gaps_marked_never_current(self):
        # the live failure (2026-11-11): three rules slower than their windows were cut
        # short on every pass, so their last good findings were of older data. Those stay
        # listed, each marked with when it was last checked, never as current; what the
        # cut-short run did find again is current.
        from operating import briefing as bf
        world, ids = seeded()
        first = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
        self.run_engine(world, ids["coverage"], at=first)
        before = self.late_ids(self.read())
        self.assertIn(ids["unconfirmed"], before)
        world.obj("ForwarderQuote", {"valid_until": "2026-02-01"})   # new data: new digest
        with mock.patch.object(pe, "_late_or_unconfirmed_po",
                               side_effect=pe.TimeBudgetSpent()):
            summary = self.run_engine(world, ids["coverage"], at=first + timedelta(hours=1))
        self.assertEqual(summary["detectors"]["late_or_unconfirmed_po"], "truncated")
        shown = self.read()
        self.assertFalse(shown["complete"])
        late = [g for g in shown["gaps"] if g["kind"] == "unconfirmed_po"]
        self.assertEqual({g["object_id"] for g in late} & before, before)
        for g in late:
            self.assertEqual(g["carried"]["reason"], "check_incomplete")
            self.assertEqual(g["carried"]["last_checked"], first.isoformat())
            self.assertIn("the latest check did not finish; last checked", g["summary"])
        # a rule that ran to the end over the new data is current: nothing of it marked
        self.assertTrue(any(g["kind"] == "expired_quote" and "carried" not in g
                            for g in shown["gaps"]))
        brief = bf.read(self.inbox, self.store, reader="plx:ops", may_read=everyone,
                        held=ALL_MARKINGS, decide=admit,
                        routing=bf.Routing(basis="fixture", everything=True))
        marked = [i for i in brief["items"] if i.get("carried")]
        self.assertTrue(marked)
        self.assertTrue(all(i["carried"]["reason"] == "check_incomplete" for i in marked))
        # the next run that reaches the end lists them as current again
        self.run_engine(world, ids["coverage"], at=first + timedelta(hours=2))
        again = self.read()
        self.assertIn(ids["unconfirmed"], self.late_ids(again))
        self.assertFalse(any(g.get("carried") for g in again["gaps"]))

    def test_a_rule_that_fails_on_new_data_keeps_its_older_gaps_unlisted(self):
        world, ids = seeded()
        first = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
        self.run_engine(world, ids["coverage"], at=first)
        world.obj("ForwarderQuote", {"valid_until": "2026-02-01"})   # new data: new digest
        # the late rule fails over the new data: its older gaps are listed marked as the
        # last good check's (never as current, never all hidden)
        with mock.patch.object(pe, "_late_or_unconfirmed_po",
                               side_effect=RuntimeError("SYNTH")):
            self.run_engine(world, ids["coverage"], at=first + timedelta(hours=1))
        shown = self.read()
        carried = [g for g in shown["gaps"] if g["object_id"] in self.late_ids(shown)]
        self.assertIn(ids["unconfirmed"], self.late_ids(shown))
        self.assertTrue(carried and all(g["carried"]["reason"] == "check_failed"
                                        for g in carried))
        # the vendor-mail rule judges the orders the late rule finds open: it waits too
        self.assertEqual(shown["refreshing"], ["late_or_unconfirmed_po", "vendor_no_reply"])
        self.assertIn("expired_quote", {g["kind"] for g in shown["gaps"]})
        # stored, not lost: the next run over this data lists it again
        self.run_engine(world, ids["coverage"], at=first + timedelta(hours=2))
        self.assertIn(ids["unconfirmed"], self.late_ids(self.read()))

    def test_an_inbox_without_per_rule_records_is_of_older_code(self):
        world, ids = seeded()
        self.run_engine(world, ids["coverage"])
        with sqlite3.connect(self.inbox) as conn:   # as an earlier engine wrote it
            conn.execute("DELETE FROM meta WHERE key LIKE 'generation:%'")
        # an earlier engine's findings are never shown as the deployed code's: shown
        # marked as not rechecked since the rules changed, so the list is never emptied
        found = self.read()
        self.assertIn(ids["unconfirmed"], self.late_ids(found))
        self.assertTrue(found["gaps"] and all(
            g["carried"]["reason"] == "older_code" for g in found["gaps"]))
        self.assertTrue(all("not rechecked since the rules changed" in g["summary"]
                            for g in found["gaps"]))
        self.assertTrue(found["older_code"] and found["stale"])
        # the deployed engine's run records its code: listed again
        self.run_engine(world, ids["coverage"])
        self.assertIn(ids["unconfirmed"], self.late_ids(self.read()))
        world.obj("ForwarderQuote", {"valid_until": "2026-02-01"})
        world.build(self.store)
        found = self.read()
        self.assertTrue(found["stale"])
        self.assertTrue(found["gaps"] and all(
            g["carried"]["reason"] == "older_data" for g in found["gaps"]))
        with sqlite3.connect(self.inbox) as conn:   # an unreadable record: of no known data
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('generation:expired_quote', '{')")
        self.assertIn("expired_quote", self.read()["refreshing"])


class PassResumeTest(Engine):
    """A whole pass run rule group by rule group (``refresh_pass``, Fable audit 11-14 item
    10): each group folds with the pass's checkpoint, so a pass killed after some groups is
    resumed by the next from the first group not folded, and what the two leave in the
    inbox is what one uninterrupted pass leaves (gaps, rule statuses, scope, the run)."""

    def snapshot(self) -> dict:
        with closing(sqlite3.connect(self.inbox)) as conn:
            return {
                "gaps": sorted(conn.execute(
                    "SELECT gap_id, detector, state FROM gaps").fetchall()),
                "rules": sorted(conn.execute(
                    "SELECT detector, status, found FROM detector_runs").fetchall()),
                "scope": sorted(conn.execute("SELECT * FROM scope").fetchall()),
                "runs": conn.execute("SELECT count(*), max(as_of) FROM runs").fetchone(),
                "passes": conn.execute("SELECT count(*) FROM meta WHERE key LIKE 'pass:%'"
                                       ).fetchone()[0]}

    def group_runner(self, world, coverage, moment, *, die_at=None, share=True):
        calls = []

        def run_group(_runtime, _inv, _aud, projection, as_of, *, budget, only, threads,
                      clock, finish_pass, checkpoint, pass_cache):
            calls.append(list(only))
            if die_at is not None and len(calls) == die_at:
                raise KeyboardInterrupt("killed")   # the unit's timeout, mid-pass
            # ``share=False``: each group on a store of its own (before 11-16)
            return pe.run(projection, pe.inbox_path(projection), as_of, coverage=coverage,
                          qc_link_types=(QC_LINK,), on_hand_sources=(ON_HAND,),
                          clock=clock, people=people(), cadence={}, budget=budget,
                          only=only, threads=threads, finish_pass=finish_pass,
                          checkpoint=checkpoint, pass_cache=pass_cache if share else None)
        return run_group, calls

    def full_pass(self, world, coverage, moment) -> dict:
        world.build(self.store)
        pe.run(self.store, self.inbox, AS_OF, coverage=coverage, qc_link_types=(QC_LINK,),
               on_hand_sources=(ON_HAND,), clock=lambda: moment, people=people(),
               cadence={})
        return self.snapshot()

    def test_a_killed_pass_resumes_and_equals_one_full_pass(self):
        moment = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
        world, ids = seeded()
        expected = self.full_pass(world, ids["coverage"], moment)
        self.assertGreater(len(expected["gaps"]), 0)
        # the same data in a fresh store and inbox, judged as a pass of small groups killed
        # at its third (the first group makes the inbox)
        (self.dir / "pass").mkdir()
        self.store = self.dir / "pass" / "store.sqlite"
        self.inbox = pe.inbox_path(self.store)
        world.build(self.store)
        groups = mock.patch.object(pe, "GROUP_SECONDS", 1500.0)
        groups.start()
        self.addCleanup(groups.stop)
        plan = pe.rule_groups(pe.DETECTORS, threads=1)
        self.assertGreater(len(plan), 3)
        self.assertEqual(sorted(sum(plan, [])), sorted(pe.DETECTORS))   # each rule once
        run_group, calls = self.group_runner(world, ids["coverage"], moment, die_at=3)
        with self.assertRaises(KeyboardInterrupt):
            pe.refresh_pass(None, None, None, self.store, AS_OF, kind="always",
                            seconds=10_000, threads=1, clock=lambda: moment,
                            run_group=run_group)
        held = pe.pass_record(self.inbox, "all")
        self.assertEqual(held["done"], sorted(plan[0] + plan[1]))
        self.assertTrue(pe.served_behind(self.store))   # no whole run recorded yet
        later = moment + timedelta(minutes=90)
        run_group, calls = self.group_runner(world, ids["coverage"], later)
        out = pe.refresh_pass(None, None, None, self.store, AS_OF, kind="whole",
                              seconds=10_000, threads=1, clock=lambda: later,
                              run_group=run_group)
        self.assertTrue(out["pass"]["resumed"])
        self.assertTrue(out["pass"]["complete"])
        self.assertEqual(calls, plan[2:])   # only the groups not folded before
        self.assertEqual(self.snapshot(), expected)
        self.assertFalse(pe.served_behind(self.store))

    def test_a_pass_stops_at_its_share_and_a_stale_record_starts_over(self):
        moment = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
        world, ids = seeded()
        self.full_pass(world, ids["coverage"], moment)
        clock = [0.0]
        run_group, calls = self.group_runner(world, ids["coverage"], moment)

        def timed(*a, **k):
            clock[0] += 1000.0   # each group takes 1000 s
            return run_group(*a, **k)
        only = sorted(pe.DETECTORS)[:12]
        groups = pe.rule_groups(only, threads=1, group_seconds=900.0)
        with mock.patch.object(pe, "GROUP_SECONDS", 900.0):
            out = pe.refresh_pass(None, None, None, self.store, AS_OF, kind="calling",
                                  seconds=1100.0, only=only, threads=1,
                                  clock=lambda: moment, monotonic=lambda: clock[0],
                                  run_group=timed)
            # the first group always runs; the second no longer fits the 100 s left
            self.assertEqual(out["pass"]["groups_run"], 1)
            self.assertFalse(out["pass"]["complete"])
            self.assertEqual(pe.pass_record(self.inbox, "calling")["done"], sorted(groups[0]))
            # past the resume window (or another plan, code or generation): a new pass
            calls.clear()
            late = moment + timedelta(hours=proactive_engine_inbox.PASS_RESUME_HOURS + 1)
            out = pe.refresh_pass(None, None, None, self.store, AS_OF, kind="calling",
                                  seconds=1e9, only=only, threads=1, clock=lambda: late,
                                  run_group=run_group)
        self.assertFalse(out["pass"]["resumed"])
        self.assertEqual(calls, groups)
        self.assertIsNone(pe.pass_record(self.inbox, "calling"))   # done: record gone


class PassCacheTest(Engine):
    """The groups of one pass share what one read (``PassCache``, 11-16: a store per group
    scanned the link table 20-25 min a group on the served store): the same inbox as a
    pass whose every group reads on its own, the link scan and the test records read
    once a pass, the shared link rows bounded as one whole run's."""

    snapshot = PassResumeTest.snapshot
    group_runner = PassResumeTest.group_runner

    def world(self):
        world, ids = seeded()
        # a test record (a UAT line) under a late purchase order: its gap is dropped in
        # every group's test-record filter
        ids["uat"] = world.obj("PurchaseOrder", {"status": "Pending",
                                                 "expected_date": "2026-10-10"})
        world.link("PurchaseOrderLine.line_of", world.obj("PurchaseOrderLine", {
            "ordered_quantity": ("10", "ea"), "received_quantity": ("0", "ea", "received"),
            "description": "UAT-SH100034 line"}), ids["uat"])
        return world, ids

    def inbox_of(self) -> dict:
        with closing(sqlite3.connect(self.inbox)) as conn:
            return {**self.snapshot(),
                    "gap_rows": sorted(conn.execute(
                        "SELECT gap_id, kind, detector, object_id, summary, follow_up, "
                        "priority, state FROM gaps").fetchall()),
                    "cites": sorted(conn.execute("SELECT * FROM gap_cites").fetchall()),
                    "requires": sorted(conn.execute(
                        "SELECT * FROM gap_requires").fetchall()),
                    "populations": conn.execute(
                        "SELECT value FROM meta WHERE key = ?",
                        (proactive_engine_inbox._POPULATIONS_KEY,)).fetchone()}

    def one_pass(self, name, *, share, cap=None, between=None):
        """A whole pass in groups over a fresh copy of the world: the inbox it leaves,
        the link types each ``_scan_links`` read and the test-record reads. ``between``:
        called with the store before the second group (the data a delta cycle and the
        incremental pass move between two groups of a pass of hours)."""
        moment = datetime(2026, 10, 19, 6, tzinfo=timezone.utc)
        world, ids = self.world()
        (self.dir / name).mkdir()
        self.store = self.dir / name / "store.sqlite"
        self.inbox = pe.inbox_path(self.store)
        world.build(self.store)
        scans, tests, kept = [], [], []
        scan, find = pe._Store._scan_links, proactive_engine_population._find_test_records
        make = pe.PassCache

        def counted_scan(st, types):
            scans.append(list(types))
            return scan(st, types)

        def counted_find(st, as_of):
            tests.append(as_of)
            return find(st, as_of)

        def made(**k):
            cache = make(**k)
            kept.append(cache)
            return cache
        inner, calls = self.group_runner(world, ids["coverage"], moment, share=share)
        self.held = []   # the shared cache after each group: (rows kept, rows held)

        def run_group(*a, **k):
            if between is not None and len(calls) == 1:
                between(self.store)
            out = inner(*a, **k)
            if kept:
                rows = kept[0].link_rows
                self.held.append((rows.kept, sum(len(r) for r in rows.by_type.values())))
            return out
        with mock.patch.object(pe, "GROUP_SECONDS", 1500.0), \
                mock.patch.object(pe._Store, "_scan_links", counted_scan), \
                mock.patch.object(proactive_engine_population, "_find_test_records",
                                  counted_find), \
                mock.patch.object(pe, "PassCache", made), \
                mock.patch.object(pe, "LINK_CACHE_ROWS",
                                  cap if cap is not None else pe.LINK_CACHE_ROWS):
            out = pe.refresh_pass(None, None, None, self.store, AS_OF, kind="whole",
                                  seconds=10_000, threads=1, clock=lambda: moment,
                                  run_group=run_group)
        self.assertTrue(out["pass"]["complete"])
        self.assertGreater(len(calls), 3)   # several groups
        return ids, self.inbox_of(), scans, tests, kept[0]

    def test_a_shared_pass_leaves_the_inbox_a_store_per_group_leaves(self):
        ids, alone, alone_scans, alone_tests, _ = self.one_pass("alone", share=False)
        _, shared, shared_scans, shared_tests, cache = self.one_pass("shared", share=True)
        self.assertGreater(len(shared["gap_rows"]), 0)
        self.assertNotIn(ids["uat"], {row[3] for row in shared["gap_rows"]})   # dropped
        self.assertIsNotNone(shared["populations"])
        self.assertEqual(shared, alone)
        # the link table scanned once a pass: every type in one scan, none again
        read = [t for types in shared_scans for t in types]
        self.assertEqual(len(read), len(set(read)))
        self.assertEqual(len(shared_scans), 1)
        self.assertEqual(len(shared_tests), 1)   # the test records read once
        self.assertTrue(all(rows > 0 for rows, _ in self.held))
        # the pass's end lets every held read go (the groups' stores outlive the pass)
        self.assertEqual((cache.link_rows.by_type, cache.link_rows.kept), ({}, 0))
        self.assertEqual((cache.removed_all, cache.removed_of, cache.populations),
                         (None, {}, {}))
        # what a store per group read: types read again group after group (the cost)
        again = [t for types in alone_scans for t in types]
        self.assertGreater(len(again), len(set(again)))
        self.assertGreater(len(alone_tests), 1)

    def test_a_bounded_cache_lets_earlier_groups_types_go_and_judges_the_same(self):
        _, alone, *_ = self.one_pass("alone", share=False, cap=40)
        _, shared, _scans, _tests, cache = self.one_pass("shared", share=True, cap=40)
        self.assertEqual(shared, alone)
        self.assertGreater(len(self.held), 3)
        for kept, held in self.held:   # within the bound after every group
            self.assertLessEqual(kept, 40)
            self.assertEqual(kept, held)

    def test_data_moved_between_groups_is_judged_as_a_store_per_group_judges_it(self):
        # a delta cycle supersedes every PO line link after the first group and the
        # incremental pass folds it: a later group never judges from the links, records
        # removed or test records held from before (the review's reproduction, 11-16:
        # 7 gaps raised again on links that no longer exist)
        def supersede(store):
            with closing(sqlite3.connect(store)) as conn:
                conn.execute("UPDATE link_rows SET superseded_at = '2026-10-19T06:30:00Z' "
                             "WHERE link_type = 'PurchaseOrderLine.line_of'")
                conn.commit()
        _, alone, *_ = self.one_pass("alone", share=False, between=supersede)
        _, unmoved, *_ = self.one_pass("unmoved", share=False)
        _, shared, shared_scans, shared_tests, _ = self.one_pass(
            "shared", share=True, between=supersede)
        self.assertNotEqual(alone["gap_rows"], unmoved["gap_rows"])   # the change matters
        self.assertEqual(shared, alone)
        self.assertEqual(len(shared_tests), 2)   # read again after the change, once
        # after the change a later group reads only the types it asks for, each once
        later = [t for types in shared_scans[1:] for t in types]
        self.assertEqual(len(later), len(set(later)))

    def test_an_unreadable_stamp_lets_the_held_reads_go(self):
        # a stamp that cannot be told is never taken as unchanged
        self.store.parent.mkdir(exist_ok=True)
        world, _ = self.world()
        world.build(self.store)
        cache = pe.PassCache()
        with mock.patch.object(pe._Store, "data_stamp", lambda st: None):
            first = pe._Store(self.store, shared=cache)
            try:
                first.links(["PurchaseOrderLine.line_of"], held=False)
                first.removed()
                self.assertIsNotNone(cache.removed_all)
                self.assertTrue(cache.link_rows.by_type)
            finally:
                first.close()
            second = pe._Store(self.store, shared=cache)
            second.close()
        self.assertEqual((cache.link_rows.by_type, cache.removed_all), ({}, None))

    def test_a_later_group_makes_room_by_letting_an_earlier_groups_type_go(self):
        self.store.parent.mkdir(exist_ok=True)
        world, _ = self.world()
        world.build(self.store)
        lines, receives = "PurchaseOrderLine.line_of", "ReceiptLine.receives_line"
        cache = pe.PassCache()
        first = pe._Store(self.store, shared=cache)
        try:
            held = len(first.links([lines], held=False))
            self.assertGreater(held, 0)
        finally:
            first.close()
        second = pe._Store(self.store, shared=cache)
        try:
            with mock.patch.object(pe, "LINK_CACHE_ROWS", held + 1):
                read = second.links([receives], held=False)
                self.assertGreater(len(read), 1)
                # the earlier group's type let go, this group's kept, within the bound
                self.assertEqual(set(cache.link_rows.by_type), {receives})
                self.assertEqual(cache.link_rows.kept, len(read))
                # a type this group asked is never let go for another of its own
                second.links([lines], held=False)
                self.assertIn(receives, cache.link_rows.by_type)
                self.assertLessEqual(cache.link_rows.kept, held + 1)
        finally:
            second.close()

    def test_an_incremental_store_never_takes_the_pass_cache(self):
        self.store.parent.mkdir(exist_ok=True)
        world, _ = self.world()
        world.build(self.store)
        cache = pe.PassCache()
        st = pe._Store(self.store, scope=(), shared=cache)
        try:
            self.assertIsNot(st.link_rows, cache.link_rows)
            st.links(["PurchaseOrderLine.line_of"])
        finally:
            st.close()
        self.assertEqual(cache.link_rows.by_type, {})


class PermissionTest(Engine):
    """A restricted reader sees no gap whose subject or any evidence group it may not read,
    and its counts cover only what it may read."""

    def test_a_missing_in_record_gap_needs_the_records_empty_read_too(self):
        # the card rule run as the capture principal: the document states a customer PO
        # the order holds empty; a reader of the document alone never sees the claim
        world = World()
        doc = world.obj("CustomerPoDocument", {"po_number": "SYNTH-PO-1"},
                        audience="sharepoint:b")
        order = world.obj("SalesOrder", {"customer_po": None}, audience="api:a")
        world.link("CustomerPoDocument.for_sales_order", doc, order)
        stated, empty = handle(1), handle(2)

        def card(_tool, arguments):
            if arguments["object_id"] != doc:
                return {"object": {"object_id": arguments["object_id"]}, "leads": [],
                        "conflicts": []}
            return {"object": {"object_id": doc, "object_type": "CustomerPoDocument"},
                    "conflicts": [], "leads": [{
                        "kind": "missing_in_record", "subject": "SalesOrder.customer_po",
                        "values": [{"property": "customer_po", "value": "SYNTH-PO-1",
                                    "citation": {"l1_handle": stated}},
                                   {"property": "customer_po", "value": None,
                                    "citation": {"l1_handle": empty}}],
                        "document_citations": [{"l1_handle": stated}]}]}
        summary = self.run_engine(world, call=card)
        self.assertEqual(summary["gaps_by_kind"].get("missing_in_record"), 1, summary)
        both = self.read(may_read=lambda key: key in ("sharepoint:b", "api:a"))
        [gap] = [g for g in both["gaps"] if g["kind"] == "missing_in_record"]
        self.assertEqual((gap["object_id"], set(gap["citations"])), (doc, {stated, empty}))
        document_only = self.read(may_read=lambda key: key == "sharepoint:b")
        self.assertNotIn("missing_in_record", {g["kind"] for g in document_only["gaps"]})
        self.assertNotIn(empty, json.dumps(document_only))
        self.assertNotIn(order, json.dumps(document_only))

    def test_an_alert_never_cites_a_document_the_readers_markings_withhold(self):
        """A cited document marked beyond the reader (document_marks) trims the gap, as a
        refusal by the host does; a reader holding the marking still gets it."""
        from operating.search import document_marks
        world, ids = seeded()
        self.run_engine(world, ids["coverage"])
        full = {g["object_id"]: g for g in self.read()["gaps"]}
        need = full[ids["unconfirmed"]]["citations"][0]

        with mock.patch.object(document_marks, "withheld_set",
                               return_value=(frozenset(), frozenset({need}))):
            cut = self.read(held=frozenset())
        self.assertNotIn(ids["unconfirmed"], {g["object_id"] for g in cut["gaps"]})
        self.assertNotIn(need, json.dumps(cut))
        # unmarked, the same gap is shown
        self.assertIn(ids["unconfirmed"], {g["object_id"] for g in self.read()["gaps"]})

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




def single_query_links(self, types, tiers=None, *, held=True):
    """The engine's link read before it read each type once in rowid ranges side by side:
    one query over the ``links`` view per call (``held``: both ends not removed)."""
    sql = ("SELECT l.link_id, l.link_type, l.from_object, l.to_object, l.tier, l.markings "
           "FROM links l WHERE l.link_type IN (SELECT value FROM json_each(:types)) "
           "AND l.superseded_at IS NULL AND l.tier <> 'contradicted'")
    if held:
        sql += (" AND NOT " + ostore.removed_at_sql("l.from_object")
                + " AND NOT " + ostore.removed_at_sql("l.to_object"))
    params = {"types": json.dumps(list(types))}
    if tiers is not None:
        sql += " AND l.tier IN (SELECT value FROM json_each(:tiers))"
        params["tiers"] = json.dumps(list(tiers))
    return [dict(row) for row in self.conn.execute(sql, params)]


def single_query_values(self, types, props=None, *, objects=None):
    """The engine's value read before it went object first in chunks: one query over the
    ``object_values`` view per call (the reference the faster read must equal)."""
    sql = ("SELECT o.object_id, o.object_type, v.property, v.value_type, v.value, v.unit, "
           "v.quantity_role, v.l1_handle, v.markings, v.lineage FROM objects o "
           "JOIN object_values v ON v.object_id = o.object_id "
           "WHERE o.object_type IN (SELECT value FROM json_each(:types)) "
           "AND v.superseded_at IS NULL AND NOT " + ostore.removed_at_sql("o.object_id"))
    params = {"types": json.dumps(list(types))}
    if props is not None:
        sql += " AND v.property IN (SELECT value FROM json_each(:props))"
        params["props"] = json.dumps(list(props))
    if objects is not None:
        sql += " AND o.object_id IN (SELECT value FROM json_each(:objects))"
        params["objects"] = json.dumps(list(objects))
    out = {}
    for row in self.conn.execute(sql + " ORDER BY o.object_id, v.property, v.l1_handle",
                                 params):
        entry = out.setdefault(row["object_id"], {"type": row["object_type"], "props": {}})
        entry["props"].setdefault(row["property"], []).append(dict(row))
    return out


def view_quantity_rows(st):
    """The conflicting-quantity read before it went quantity kind first: the view."""
    return [dict(row) for row in st.conn.execute(
        "SELECT o.object_id, o.object_type, v.property, v.value, v.unit, v.quantity_role, "
        "v.l1_handle, v.markings FROM objects o JOIN object_values v ON v.object_id = o.object_id "
        "WHERE v.value_type = 'quantity' AND v.superseded_at IS NULL AND NOT "
        + ostore.removed_at_sql("o.object_id"))]








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




CALLING_RULES = ("negative_margin", "margin_erosion", "orders_at_risk",
                 "vendor_terms_unapplied", "card_rules")








def _pool_init(_spec) -> None:   # a worker without a session (spawned: by name)
    from operating import proactive_pool as pp
    pp._CALL = lambda _tool, _args: None


def _die_on_two(_call, item):
    if item == 2:
        os._exit(1)    # the worker process dies under this item
    return item * 10


def _sleep_for(_call, item):
    time.sleep(item)
    return item


# An entry script without a ``__main__`` guard (as an operator's one-off pass): each process
# that runs it appends its pid; it maps on the pool and prints the answers.
_UNGUARDED = """
import os, sys, json
sys.path.insert(0, {root!r})
with open({marks!r}, "a") as handle:
    handle.write(str(os.getpid()) + "\\n")
from unittest import mock
from operating import proactive_pool as pp
from operating.tests import test_proactive_engine as t
pool = pp.ToolPool(None, 2)
with mock.patch.object(pp, "_init", t._pool_init):
    try:
        print(json.dumps(pool.map(t._die_on_two, [1, 3, 4])))
    finally:
        pool.close()
"""


class DetectorReserveTest(unittest.TestCase):
    def test_a_small_store_reserves_the_floor_a_large_one_or_none_the_full_reserve(self):
        """A rule over a fixture world is not counted at the live store's 1.5 GB (a capped
        test scope queued 63 rules one at a time); a large store and a store with no file
        (Postgres) keep the full reserve, and the reserve grows with the store between."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            small = Path(tmp) / "small.sqlite"
            small.write_bytes(b"x" * 1000)
            self.assertEqual(pe.detector_reserve(small), proactive_engine_budget.DETECTOR_RESERVE_FLOOR_BYTES)
            middle = Path(tmp) / "middle.sqlite"
            with open(middle, "wb") as handle:
                handle.truncate(100_000_000)
            self.assertEqual(pe.detector_reserve(middle), 800_000_000)
            big = Path(tmp) / "big.sqlite"
            with open(big, "wb") as handle:
                handle.truncate(10_000_000_000)   # sparse
            self.assertEqual(pe.detector_reserve(big), pe.DETECTOR_RESERVE_BYTES)
            empty = Path(tmp) / "empty.sqlite"
            empty.write_bytes(b"")
            for path in (Path(tmp) / "absent.sqlite", empty, None):
                self.assertEqual(pe.detector_reserve(path), pe.DETECTOR_RESERVE_BYTES)


class PoolSizeTest(unittest.TestCase):

    def test_the_pool_size_is_the_hosts_and_an_unreadable_override_runs_serially(self):
        from operating import proactive_pool as pp
        self.assertEqual(pp.workers({pp.WORKERS_ENV: "3"}), 3)
        self.assertEqual(pp.workers({pp.WORKERS_ENV: "SYNTH"}), 0)   # never a guess
        self.assertEqual(pp.workers({pp.WORKERS_ENV: "-2"}), 0)
        self.assertTrue(1 <= pp.workers({}) <= pp.MAX_WORKERS)
        # never more than a quarter of the free memory (nor POOL_BYTES) at WORKER_BYTES each
        with mock.patch.object(pp, "_available_bytes", return_value=10 * pp.WORKER_BYTES), \
                mock.patch.object(pp, "_cgroup_cap", return_value=None), \
                mock.patch.object(pp.os, "cpu_count", return_value=64):
            self.assertEqual(pp.workers({}), 2)
        with mock.patch.object(pp, "_available_bytes", return_value=10 ** 13), \
                mock.patch.object(pp, "_cgroup_cap", return_value=None), \
                mock.patch.object(pp.os, "cpu_count", return_value=64):
            self.assertEqual(pp.workers({}) * pp.WORKER_BYTES <= pp.POOL_BYTES, True)
            # under a unit's memory cap: what it leaves beside the engine's own process
            with mock.patch.object(pp, "_cgroup_cap", return_value=12 * 2 ** 30):
                self.assertEqual(pp.workers({}), 2)
        self.assertIsNone(pp.from_host_config(None, Path("."), Path("."),
                                              {pp.WORKERS_ENV: "1"}))

    def test_a_dying_worker_fails_only_its_own_item(self):
        from operating import proactive_pool as pp
        pool = pp.ToolPool(None, 2)
        with mock.patch.object(pp, "_init", _pool_init):
            try:
                self.assertEqual(pool.map(_die_on_two, [1, 2, 3, 4, 5]),
                                 [10, pp.Failed(pp.WORKER_FAILED), 30, 40, 50])
                self.assertEqual(pool.map(_die_on_two, [7]), [70])   # the pool goes on
            finally:
                pool.close()

    def test_a_worker_never_reruns_the_entry_script_that_started_the_pool(self):
        # 2026-11-07: an unguarded entry script ran a whole engine pass in each spawned
        # worker before its first task; the margins read nothing for ~4,000 s
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            script, marks = Path(temp) / "entry.py", Path(temp) / "marks"
            script.write_text(_UNGUARDED.format(root=str(ROOT), marks=str(marks)), "utf-8")
            done = subprocess.run([sys.executable, str(script)], capture_output=True,
                                  text=True, timeout=300, cwd=temp,
                                  env={k: v for k, v in os.environ.items()
                                       if k.startswith(("PATH", "HOME", "LANG"))})
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            self.assertEqual(json.loads(done.stdout.strip().splitlines()[-1]), [10, 30, 40])
            self.assertEqual(len(marks.read_text().split()), 1)   # the parent only

    def test_a_map_ends_at_a_window_that_shrinks_and_frees_its_workers(self):
        # the margins' head start: its map reads to the run's deadline until its rule's turn
        # opens a shorter window; it must end then, not when a running read next finishes,
        # and the next detector's map must not queue behind the abandoned reads
        import threading
        from operating import proactive_pool as pp
        pool = pp.ToolPool(None, 2)
        end = [time.monotonic() + 600]
        box: dict = {}
        with mock.patch.object(pp, "_init", _pool_init):
            try:
                self.assertEqual(pool.map(_sleep_for, [0]), [0])   # the workers are up
                runner = threading.Thread(target=lambda: box.setdefault(
                    "out", pool.map(_sleep_for, [120, 120, 120],
                                    left=lambda: end[0] - time.monotonic())))
                runner.start()
                time.sleep(1.5)
                started = time.monotonic()
                end[0] = started   # the window closes now
                runner.join(30)
                self.assertFalse(runner.is_alive())
                self.assertLess(time.monotonic() - started, 30)
                self.assertEqual(box["out"], [pp.NOT_READ] * 3)
                started = time.monotonic()
                self.assertEqual(pool.map(_die_on_two, [7], left=lambda: 300), [70])
                self.assertLess(time.monotonic() - started, 100)   # fresh workers, no queue
            finally:
                pool.close()


class LiveCheckTest(unittest.TestCase):
    """live check ``case.proactive_engine`` over injected sources: its own
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
        self.spec = json.loads((ROOT / "devloop" / "live-checks" / f"{self.SPEC_ID}.json")
                               .read_text("utf-8"))

    def world(self, *, fetch=None) -> tuple[dict, dict, object]:
        """The host config file (written to the temp dir), environ and host options of
        ``case.customer_po_missing``'s world (FakeWorkbench plus a fake drive)."""
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
        drive = _customer_drive()
        environ = {**tsp.ENV, "PLX_WORKBENCH_API_URL": BASE, "PLX_WORKBENCH_API_KEY": KEY}
        return environ, {"sender": drive, "workbench_fetch": fetch or fake,
                         "sleep": lambda _s: None}, drive

    def run_spec(self, *, fetch=None) -> tuple[int, dict, str]:
        environ, options, drive = self.world(fetch=fetch)
        argv = [str(self.temp / "host.json") if arg.endswith("_host.live-check.json")
                else str(ROOT / arg) if arg.startswith("examples/")
                else arg.replace("{tmp}", str(self.temp)) for arg in self.spec["command"][3:]]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pe.main(argv, environ=environ, graph_sender=drive, host_options=options)
        self.assertTrue(all(method == "GET" for method, _url in drive.calls))
        return code, json.loads(out.getvalue()), out.getvalue()

    def test_a_host_cycle_runs_every_detector_into_its_inbox_within_its_budget(self):
        # The host's own post-build refresh (no proactive_engine.main): the coverage rule
        # and the tool-caller rules run as the capture principal, never not_configured
        from operating.host import Host, HostConfig, projection_path

        environ, options, drive = self.world()
        state = self.temp / "state"
        report = Host(state, HostConfig.from_file(self.temp / "host.json"),
                      environ=environ, **options).cycle()
        self.assertTrue(all(method == "GET" for method, _url in drive.calls))
        self.assertEqual((report["state"], report["projection"]["state"]),
                         ("complete", "built"))
        hook = report["projection"]["proactive"]
        self.assertEqual(hook["state"], "ran", hook)
        for name in ("order_without_customer_po", "orders_at_risk", "card_rules"):
            self.assertEqual(hook["detectors"][name], "complete", name)
        store = projection_path(state)
        with contextlib.closing(sqlite3.connect(pe.inbox_path(store))) as conn:
            ran = dict(conn.execute("SELECT detector, status FROM detector_runs"))
            kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM gaps")}
        self.assertEqual(ran, hook["detectors"])
        self.assertTrue({"order_without_customer_po", "missing_in_record"} <= kinds, kinds)
        # a reader without any grant is shown nothing of what the capture principal found
        found = pe.read_inbox(pe.inbox_path(store), store, reader="plx:nobody",
                              may_read=lambda _key: False, held=frozenset(),
                              decide=admit)
        self.assertEqual((found["gaps"], found["scanned"]), ([], 0))
        # the budget spent: every rule is truncated for it, the cycle still complete
        with mock.patch.object(pe, "REFRESH_BUDGET_SECONDS", 0):
            spent = Host(state, HostConfig.from_file(self.temp / "host.json"),
                         environ=environ, **self.world()[1]).cycle()
        self.assertEqual(spent["state"], "complete")
        hook = spent["projection"]["proactive"]
        self.assertEqual(set(hook["detectors"].values()), {"truncated"}, hook)
        self.assertEqual(set(map(tuple, hook["detector_reasons"].values())),
                         {(pe.TIME_BUDGET,)})

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


