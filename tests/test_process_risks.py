"""Schedule risks of plx.process (operating/process_risks.py, RSN-01 risks part) over the
fixture projection store, real compiled schema and real process graphs of
tests/test_ontology_tools_process.py; every output is validated against the plx.process
contract. Evidence label: isolated-fixture.
"""

from __future__ import annotations

import contextlib
from contextlib import contextmanager
import io
import copy
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
import unittest.mock

from operating import ontology_tools as ot
from operating import ontology_tools_process as op
from operating.host import projection_path
from operating.process_risks import undecided_cause
from scripts import process_dependencies_probe as probe
from tests import test_ontology_tools_process as base
from tests.test_ontology_tools_process import (POLICY, STAFF, Mixin, Store, material_fixture,
                                               stages)

BC_LINE = "PurchaseOrderLine:bc.purchase_order_lines:bcpo-mat-{:02d}-{}"


def setUpModule():
    base.setUpModule()


def served_probe(case, store, policy=POLICY):
    """process_dependencies_probe's report over ``store`` through a stand-in serving it as
    Host.served_tools does (the served table's plx.process handler, under ``policy``), and
    the tools it serves."""
    store.conn.commit()
    state = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, state)
    projection_path(state).parent.mkdir(parents=True)
    with sqlite3.connect(projection_path(state)) as copy_to:
        store.conn.backup(copy_to)
    copy_to.close()
    tools = ot.OntologyTools(ot.StoreReader(store.conn), policy, schema=base.SCHEMA,
                             contracts=base.CONTRACTS, clock=lambda: base.NOW)

    def call(name, arguments, actor):   # the served table's plx.process handler
        tools.policy = policy
        return op.ProcessTools(tools, base.GRAPHS).call(name, arguments, actor)

    served = SimpleNamespace(tools=tools, policy_now=lambda actor: policy, call=call)

    @contextmanager
    def served_tools(actor):
        yield served

    host = SimpleNamespace(state_dir=state, served_tools=served_tools,
                           config=SimpleNamespace(tenant="plx", principal="staff"))
    return probe.check_dependencies(host, per_type=20, scan=200, budget_seconds=None), tools


class ScheduleRiskTests(Mixin, unittest.TestCase):
    """A prerequisite record due after the waiting record needs it is a schedule_risk
    citing both dates; an undated side is a gap; on time is nothing."""

    def risks(self, root="ao", policy=POLICY, needed=("2099-01-05",), expected=(),
              bc_expected=(), on="ao", lines=(), **world):
        """``lines``: (BC PO index, line number, its expectedReceiptDate or None); line 1
        is the fixture's, a higher one is added (both of the PO's item)."""
        store = Store()
        ids = material_fixture(store, **world)
        for n, number, day in lines:
            line = BC_LINE.format(n, number)
            if number > 1:
                store.obj(line)
                store.link("PurchaseOrderLine.line_of", line, ids["bc_pos"][n])
            if day:
                store.val(line, "bc_expected_receipt_date", "date", day)
        for day in needed:
            store.event("AssemblyOrder.scheduled", ids[on], day)
        for po, day in zip(ids["pos"], expected):
            store.event("PurchaseOrder.expected", po, day)
        for po, day in zip(ids["bc_pos"], bc_expected):
            store.event("PurchaseOrder.bc_expected", po, day)
        tools = op.ProcessTools(ot.OntologyTools(
            ot.StoreReader(store.conn), policy, schema=base.SCHEMA, contracts=base.CONTRACTS,
            clock=lambda: base.NOW), base.GRAPHS)
        result = self.answer(tools, {"from": ids[root]}).result
        stage = stages(result)["assembly_realized" if root == "ao" else "manufacturing_order"]
        gaps = [g for g in result["gaps"] if g["reason"] == "schedule_undecided"]
        return stage.get("risks", []), gaps, ids, result

    def test_late_prerequisite_is_a_risk_citing_both_dates(self):
        risks, gaps, ids, _ = self.risks(expected=("2099-01-09",))
        self.assertEqual(gaps, [])
        (risk,) = risks
        self.assertEqual((risk["kind"], risk["object"], risk["proven"], risk["days_late"]),
                         ("schedule_risk", ids["pos"][0], False, 4))   # linked by item only
        need, have = risk["needed_by"], risk["available_by"]
        self.assertEqual((need["process"], need["stage"], need["object"], need["date"]),
                         ("manufacturing_order", "filling", ids["ao"], "2099-01-05"))
        self.assertEqual((have["process"], have["stage"], have["object"], have["date"]),
                         ("po_delivery_window", "receipt", ids["pos"][0], "2099-01-09"))
        # each date cites the record it was read from
        self.assertIn("/ao-mat@", need["dates"][0]["citation"]["l1_handle"])
        self.assertIn("/po-mat-00@", have["dates"][0]["citation"]["l1_handle"])
        # one level down: the order waits on its MO (a proven blocker); the MO's own
        # planned start is what the PO is late against, via that MO
        risks, _, ids, _ = self.risks(root="so", on="mo", expected=("2099-01-09",))
        (risk,) = risks
        self.assertEqual((risk["via"], risk["needed_by"]["object"], risk["object"]),
                         (f"manufacturing_order.assembly_realized {ids['mo']}", ids["mo"],
                          ids["pos"][0]))

    def test_undated_side_is_a_gap_never_a_guess(self):
        """Each gap names the side without a date and its cause, as the probe counts it."""
        for world, side, cause in (({"expected": ()}, "available_by", "undated"),
                                   ({"needed": (), "expected": ("2099-01-09",)}, "needed_by",
                                    "undated"),
                                   # twins disagree: no date is chosen
                                   ({"expected": ("2099-01-09",),
                                     "bc_expected": ("2099-01-02",), "bc_pos": ("open",),
                                     "twins": 1}, "available_by", "disagreeing")):
            risks, gaps, ids, _ = self.risks(**world)
            self.assertEqual(risks, [], world)
            (gap,) = gaps
            # a twin pair is one blocker entry, named by its first handle; an undated
            # waiting record is named itself, by the stage it needs it by
            self.assertEqual((gap["state"], gap["subject"]), (
                "unknown", f"manufacturing_order.filling {ids['ao']}" if side == "needed_by"
                else "purchase_to_invoice.receipt " + (ids["bc_pos"] + ids["pos"])[0]), world)
            self.assertIn(side, gap["detail"], world)
            self.assertEqual(undecided_cause(gap), (side, cause), world)
        # a due-date gap reason this module does not know is unproven, never a date; a
        # detail it did not write is no cause
        new = op.Dated([], None, ot._gap("unknown", "due_date_new_reason", "s"), None)
        with unittest.mock.patch.object(op.ProcessTools, "_due_date", lambda *a: new):
            risks, gaps, _, _ = self.risks(expected=("2099-01-09",))
        self.assertEqual((risks, [undecided_cause(g) for g in gaps]),
                         ([], [("needed_by", "unproven")]))
        for gap in ({**gaps[0], "detail": "needed_by undated"},
                    {**gaps[0], "reason": "due_date_unproven"}):
            self.assertIsNone(undecided_cause(gap), gap)

    def test_an_undated_waiting_record_is_one_gap_however_many_it_waits_on(self):
        risks, gaps, ids, result = self.risks(pos=("open",) * 30, needed=())
        self.assertEqual(risks, [])
        (gap,) = gaps
        self.assertEqual(gap["subject"], f"manufacturing_order.filling {ids['ao']}")
        self.assertIn("any of the 30 prerequisite record(s)", gap["detail"])
        self.assertIn("needed_by", gap["detail"])
        self.assertTrue(result["completeness"]["complete"], result["completeness"])

    def test_a_bc_line_date_never_stands_for_its_order_header_date(self):
        # The PO's planned arrival is its requested-by date (po_delivery_window's receipt
        # due): a BC line's expectedReceiptDate follows the supplier's promise and is never
        # put in its place, earlier or later, with or without a header date.
        world = {"pos": (), "bc_pos": ("open",)}
        line = BC_LINE.format(0, 1)
        # header on time, line later: no risk, and the line date is not read
        risks, gaps, ids, result = self.risks(lines=[(0, 1, "2099-01-09")],
                                              bc_expected=("2099-01-03",), **world)
        self.assertEqual((risks, gaps), ([], []))
        self.assertNotIn("2099-01-09", json.dumps(result))
        # header late, line earlier: the header's date is the risk, cited from the header
        risks, gaps, ids, result = self.risks(lines=[(0, 1, "2099-01-04"), (0, 2, None)],
                                              bc_expected=("2099-01-09",), **world)
        self.assertEqual(gaps, [])
        (risk,) = risks
        have = risk["available_by"]
        self.assertEqual((risk["object"], risk["days_late"], have["date"]),
                         (ids["bc_pos"][0], 4, "2099-01-09"))
        self.assertEqual([d["value"] for d in have["dates"]], ["2099-01-09"])
        self.assertNotIn(f"/{line.rsplit(':', 1)[1]}@", json.dumps(have))
        self.assertNotIn("2099-01-04", json.dumps(result))
        # no header date: undated, never the line's date
        risks, gaps, ids, result = self.risks(lines=[(0, 1, "2099-01-09")], **world)
        self.assertEqual(risks, [])
        (gap,) = gaps
        self.assertEqual(undecided_cause(gap), ("available_by", "undated"))
        self.assertNotIn("2099-01-09", json.dumps(result))
        # a reader granted only the BC headers (of a Portal PO's BC twin): a due date of
        # the stage is withheld, so the side is withheld, never a date, with or without a
        # line date, and the line date never shows
        hidden = ot.ReadPolicy("plx", {"api:*": frozenset({"staff"}),
                                       "bc:company_a/purchaseOrders": frozenset({"staff"})},
                               POLICY.markings)
        for lines in ([(0, 1, "2099-01-09")], []):
            risks, gaps, _, result = self.risks(
                policy=hidden, lines=lines, pos=("open",), bc_pos=("open",),
                twins=1, expected=("2099-01-03",), bc_expected=("2099-01-03",))
            self.assertEqual(risks, [])
            self.assertEqual([g["state"] for g in gaps], ["inaccessible"])
            self.assertEqual(undecided_cause(gaps[0]), ("available_by", "withheld"))
            self.assertNotIn("2099-01-09", json.dumps(result))

    def test_on_time_or_same_day_is_no_risk(self):
        for day in ("2099-01-03", "2099-01-05"):
            risks, gaps, _, _ = self.risks(expected=(day,))
            self.assertEqual((risks, gaps), ([], []), day)

    def test_a_twin_date_this_reader_may_not_read_is_withheld_never_a_risk(self):
        world = {"expected": ("2099-01-09",), "bc_expected": ("2099-01-09",),
                 "bc_pos": ("open",), "twins": 1}
        risks, _, ids, _ = self.risks(**world)
        self.assertEqual([r["object"] for r in risks], [ids["bc_pos"][0]])   # the twins agree
        # without BC the twin's date could differ: undecided, and the twin is never named
        portal_only = ot.ReadPolicy("plx", {"api:*": frozenset({"staff"})}, POLICY.markings)
        risks, gaps, ids, result = self.risks(policy=portal_only, **world)
        self.assertEqual(risks, [])
        self.assertEqual([g["state"] for g in gaps], ["inaccessible"])
        self.assertNotIn(ids["bc_pos"][0], json.dumps(result))

    def test_every_late_po_is_one_risk_at_1_10_60(self):
        for n in (1, 10, 60):
            started = time.monotonic()
            risks, gaps, ids, result = self.risks(pos=("open",) * n,
                                                  expected=("2099-01-09",) * n)
            elapsed = time.monotonic() - started
            objects = [r["object"] for r in risks]
            self.assertEqual(len(objects), len(set(objects)), n)   # never twice
            self.assertLessEqual(set(objects), set(ids["pos"]), n)
            self.assertEqual(gaps, [], n)
            read = min(n, op.MAX_PREREQUISITE_RECORDS)
            if result["completeness"]["complete"]:
                self.assertEqual(sorted(objects), sorted(ids["pos"]), n)
            else:   # every cut is said: past the record or list cap, or the response size
                self.assertEqual(result["completeness"]["reason"], "result_size", n)
            if read > op.MAX_RISKS:
                cut = next(g for g in result["gaps"] if g["reason"] == "risks_truncated")
                self.assertIn(f"{read - op.MAX_RISKS} of {read}", cut["detail"])
            self.assertLess(elapsed, 20, n)

    def test_the_probe_counts_undecided_comparisons_past_the_gap_cap(self):
        """process_dependencies_probe over the served host (a stand-in serving this
        store as Host.served_tools does): 30 undated POs are 30 undecided comparisons
        although the served answer lists at most MAX_GAPS gaps."""
        store = Store()
        ids = material_fixture(store, pos=("open",) * 30)
        store.event("AssemblyOrder.scheduled", ids["ao"], "2099-01-05")
        report, tools = served_probe(self, store)
        self.assertEqual((report["contract_failures"], report["process_errors"]), (0, 0))
        graph = base.GRAPHS["manufacturing_order"]
        listed = uncut = 0
        for (oid,) in store.conn.execute("SELECT object_id FROM objects WHERE object_type = ?",
                                         (graph["root_type"],)):
            answer = op.ProcessTools(tools, base.GRAPHS).process(
                {"from": oid, "process": graph["id"]}, STAFF)
            listed += sum(g["reason"] == "schedule_undecided" for g in answer.result["gaps"])
            uncut += sum(g["reason"] == "schedule_undecided"
                         for d in answer.roots for g in d["item_gaps"])
        self.assertGreaterEqual(uncut, 30)
        self.assertLess(listed, uncut)   # the served list is cut
        self.assertEqual(report["risks"]["manufacturing_order.assembly_realized"]["undecided"],
                         uncut)
        # each one by its missing side and cause, the uncut ones too: the 30 POs, and once
        # the AO's MO twin, whose own planned start is not recorded (filling reads the root)
        self.assertEqual(report["undecided_causes"]["manufacturing_order.assembly_realized"],
                         {"available_by": {"undated": 30}, "needed_by": {"undated": 1}})

    def test_a_schedule_must_name_due_dated_stages(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        for path in op.PROCESS_DIR.glob("*.json"):
            shutil.copy(path, tmp / path.name)
        doc = json.loads((tmp / "manufacturing_order.json").read_text("utf-8"))
        pre = next(s for s in doc["stages"] if s["id"] == "assembly_realized")["prerequisites"][0]
        for key, value, problem in (("needed_by", "assembly_realized", "needed_by"),
                                    ("available_by", {"process": "purchase_to_invoice",
                                                      "stage": "receipt"}, "available_by")):
            bad = copy.deepcopy(doc)
            next(s for s in bad["stages"] if s["id"] == "assembly_realized")[
                "prerequisites"][0]["schedule"][key] = value
            (tmp / "manufacturing_order.json").write_text(json.dumps(bad), "utf-8")
            with self.assertRaisesRegex(op.ProcessGraphError, problem):
                op.load_graphs(tmp, schema=base.REAL_SCHEMA)
        self.assertIn("schedule", pre)

    def test_a_sqlite_error_opening_the_served_host_is_not_the_probe_phase(self):
        """Directive 6face43dd5f9: the probe's own phase starts once the served host is
        open, so a SQLite error opening it is never labelled process_dependencies."""
        from unittest import mock
        from scripts import ontology_projection_probe as projection

        def built(argv, **kwargs):
            print(json.dumps({"mirror_complete": True}))
            return 0

        def broken(store, environ=None):
            raise sqlite3.OperationalError("database or disk is full")

        def line(served):
            out = io.StringIO()
            with mock.patch.object(projection, "main", built), \
                    mock.patch.object(projection, "served_host", served), \
                    mock.patch.object(probe, "check_dependencies",
                                      side_effect=sqlite3.OperationalError("disk full")), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(probe.main(["--db", "m.sqlite", "--store", "s.sqlite"],
                                            environ={}), 1)
            return json.loads(out.getvalue())

        self.assertEqual(line(broken), {"probe": probe.PROBE, "error": "OperationalError"})
        self.assertEqual(line(lambda store, environ=None: object())["phase"],
                         "process_dependencies")


class NotPeggedTypingTests(Mixin, unittest.TestCase):
    """Lane process-deps-pegging (RSN-01, gap-979bf8f96c6a): a prerequisite linked by item
    only is a dependency_not_pegged entry only over a live record it reads, typed by those
    records (open / met / unread); reaching none, its reach's own entry is the one cause.
    Never clear either way, and the probe counts each entry by its type."""

    def realized(self, policy=POLICY, **world):
        store = Store()
        ids = material_fixture(store, **world)
        tools = op.ProcessTools(ot.OntologyTools(
            ot.StoreReader(store.conn), policy, schema=base.SCHEMA, contracts=base.CONTRACTS,
            clock=lambda: base.NOW), base.GRAPHS)
        result = self.answer(tools, {"from": ids["ao"]}).result
        return stages(result)["assembly_realized"], ids, result, store

    @staticmethod
    def causes(stage):
        return [(b.get("object") is not None, b["missing"]["reason"],
                 op.not_pegged_waits_on(b["missing"])) for b in stage["blockers"]]

    def test_each_not_pegged_entry_is_typed_by_the_records_it_waits_on(self):
        for world, want in (
                # no PO, or only a cancelled one: nothing to peg, the reach says so once
                ({"pos": ()}, [(False, "no_prerequisite_record", None)]),
                ({"pos": ("cancelled",)}, [(False, "no_prerequisite_record", None)]),
                # every live PO received: only the missing pegging keeps it undecided
                ({"pos": ("received",)}, [(False, "dependency_not_pegged", "met")]),
                ({"pos": ("received", "cancelled")},
                 [(False, "dependency_not_pegged", "met")]),
                # an open PO is its own candidate entry, after the typed one
                ({"pos": ("received", "open")}, [(False, "dependency_not_pegged", "open"),
                                                 (True, "dependency_unproven", None)]),
                ({"pos": (), "bc_pos": ("open",)}, [(False, "dependency_not_pegged", "open"),
                                                    (True, "dependency_unproven", None)])):
            stage, ids, result, _ = self.realized(**world)
            self.assertEqual(stage["prerequisites"], "undecided", world)   # never clear
            self.assertEqual(self.causes(stage), want, world)
            entry = stage["blockers"][0]["missing"]
            if entry["reason"] == "no_prerequisite_record":
                self.assertIn("stock on hand is not read", entry["detail"], world)
            else:
                live = sum(k != "cancelled" for k in world["pos"] + world.get("bc_pos", ()))
                self.assertIn(f"ties the {live} live PurchaseOrder record(s)", entry["detail"])
        # records past the cap are not read: none read open is unread, never met, and no
        # entry claims that no record is reached
        with unittest.mock.patch.object(op, "MAX_PREREQUISITE_RECORDS", 1):
            for pos in (("received", "received"), ("cancelled", "received")):
                stage, *_ = self.realized(pos=pos)   # read first: po-mat-00
                self.assertEqual(self.causes(stage),
                                 [(False, "dependency_not_pegged", "unread"),
                                  (False, "prerequisite_cap", None)], pos)

    def test_a_reader_not_granted_the_pos_sees_no_typed_entry_and_no_po(self):
        no_po = ot.ReadPolicy("plx", {"api:*": frozenset({"staff"}),
                                      "api:public.app_purchase_order": frozenset()},
                              POLICY.markings)
        for world in (("received",), ("open",), ("open",) * 3):
            stage, ids, result, store = self.realized(policy=no_po, pos=world)
            self.assertEqual(stage["prerequisites"], "undecided", world)
            # the withheld reach is the one cause: a count of hidden POs would leak them
            self.assertEqual([(b["missing"]["state"], b["missing"]["reason"])
                              for b in stage["blockers"]],
                             [("inaccessible", "withheld_by_permission")], world)
            text = json.dumps(result)
            for po in ids["pos"]:
                self.assertNotIn(po, text, world)
            self.assertNotIn("live PurchaseOrder record", text, world)
            report, _ = served_probe(self, store, policy=no_po)
            self.assertEqual(report["not_pegged"]["manufacturing_order.assembly_realized"],
                             {}, world)
            self.assertEqual(report["undecided_candidates"], {}, world)

    def test_the_typed_count_holds_at_1_10_60_and_the_probe_counts_each_once(self):
        for n in (1, 10, 60):
            started = time.monotonic()
            pos = ("open", "received") * (n // 2) + ("open",) * (n % 2)
            stage, ids, result, store = self.realized(pos=pos)
            read = min(n, op.MAX_PREREQUISITE_RECORDS)
            (typed,) = [b["missing"] for b in stage["blockers"]
                        if b["missing"]["reason"] == "dependency_not_pegged"]
            self.assertEqual(stage["blockers"][0]["missing"], typed, n)   # first: never cut
            self.assertEqual(op.not_pegged_waits_on(typed), "open", n)
            self.assertIn(f"ties the {read} live PurchaseOrder record(s)", typed["detail"], n)
            self.assertEqual("more past the record cap" in typed["detail"],
                             n > op.MAX_PREREQUISITE_RECORDS, n)
            report, _ = served_probe(self, store)
            got = report["not_pegged"]
            # one entry per open root that reads the AO's POs: the AO and its MO twin, and
            # the order's manufacturing stage, which reads them below its MO
            self.assertNotIn("unclassified", json.dumps(got), n)
            self.assertEqual(sum(sum(v.values()) for v in got.values()),
                             report["gap_reasons"]["undecided"]["dependency_not_pegged"]
                             + report["gap_reasons"].get("blocked", {}).get(
                                 "dependency_not_pegged", 0), n)
            self.assertGreater(got["manufacturing_order.assembly_realized"].get("open", 0), 0)
            self.assertLess(time.monotonic() - started, 40, n)

    def test_an_entry_it_does_not_read_is_no_type(self):
        detail = op._not_pegged_detail
        for gap, want in (
                ({"reason": "dependency_not_pegged", "detail": detail("X", 3, 1, False)},
                 "open"),
                ({"reason": "dependency_not_pegged", "detail": detail("X", 3, 0, False)},
                 "met"),
                ({"reason": "dependency_not_pegged", "detail": detail("X", 1, 0, True)},
                 "unread"),
                ({"reason": "dependency_not_pegged", "detail": detail("X", 1, 1, True)},
                 "open"),
                # the old untyped wording, a drift, another reason, nothing: never a type
                ({"reason": "dependency_not_pegged", "detail": "no pegging record ties the "
                  "PurchaseOrder records its paths reach to this record"}, None),
                ({"reason": "dependency_not_pegged"}, None),
                ({"reason": "dependency_unproven", "detail": detail("X", 3, 1, False)}, None),
                ({}, None)):
            self.assertEqual(op.not_pegged_waits_on(gap), want, gap)


if __name__ == "__main__":
    unittest.main()
