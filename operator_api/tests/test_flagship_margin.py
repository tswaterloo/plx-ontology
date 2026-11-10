"""Flagship step 5 (``scripts/flagship_margin.py``): the margin verdict over ``plx.investigate``.

Synthetic data only: the BC sales world of ``evals.oracle.order_margin`` captured GET-only over
the in-process fake BC transport and answered by the real ``plx.investigate`` ``order_margin``
family, once for a reader holding every marking and once for a reader without ``finance``.
Evidence label: isolated-fixture / injected-transport, never live PLX data. A margin passes
only with its citations; a finance-withheld reader gets the typed gap and never a figure; the
run's sample is never a pass.
"""

from __future__ import annotations

import json
import unittest

from evals.oracle import order_margin as oracle_world
from integrations.tests import test_bc_capture as tb
from ontology import store_ddl
from ontology.tests.test_project_bc import FULL, READER, BcWorld
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt
from scripts import flagship_margin as fm
from tests.test_investigate_order_margin import fake_bc, resolver


class Workspace:
    """The two calls ``margin_sample`` makes, served by the real tools over the world."""

    def __init__(self, table):
        self.table = table

    def _call(self, tool, arguments):
        return self.table[tool]["handler"](arguments, FULL)

    def investigate(self, family, root):
        assert family == fm.FAMILY
        return {"investigation": self._call("plx.investigate", {"family": "F10",
                                                                 "root": root})}


class MarginOverInvestigate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = oracle_world.fixture_world(companies=tb.CO[:2])

    def workspace(self, markings):
        world = BcWorld(self, fake_bc(self.records))
        world.tools(ot.ReadPolicy(tenant="plx", audiences={"bc:*": {READER}, "api:*": {READER}},
                                  markings={m: {READER} for m in markings}))
        table: dict = {}
        ot.register(table, world.base)
        tt.register(table, world.traverse)
        oi.register(table, oi.InvestigateTools(table, rows=resolver(world),
                                               scope=oi.read_scope(world.base),
                                               captured=oi.capture_claims(world.base)))
        return Workspace(table)

    def test_a_full_reader_sees_a_cited_margin_and_the_sample_is_never_a_pass(self):
        workspace = self.workspace(store_ddl.MARKINGS)
        out = fm.margin_sample(workspace, identify=True)
        self.assertEqual((out["status"], out["reason"]),
                         ("unrelated_sample", "sample_not_fed_by_the_purchase_order"))
        self.assertEqual(out["sample"]["reason"], "margin_shown", out)
        self.assertGreater(out["sample"]["citations"], 0)
        self.assertTrue(out["object_id"].startswith("SalesOrder:"))
        # The same order every time, and counts and enums only without identify.
        again = fm.margin_sample(workspace)
        self.assertEqual(again["sample"]["reason"], "margin_shown")
        self.assertNotIn("object_id", again)
        self.assertNotIn("figures", again["sample"])

    def test_a_finance_withheld_reader_gets_the_typed_gap_never_a_figure(self):
        full = fm.margin_sample(self.workspace(store_ddl.MARKINGS), identify=True)
        out = fm.margin_sample(self.workspace([m for m in store_ddl.MARKINGS
                                               if m != "finance"]), identify=True)
        self.assertEqual(out["object_id"], full["object_id"])
        sample = out["sample"]
        self.assertEqual((sample["status"], sample["reason"], sample["gap"]),
                         ("blocked", "margin_withheld", fm.WITHHELD))
        self.assertEqual(set(sample["figures"].values()), {None})
        shown = {v for v in full["sample"]["figures"].values() if v is not None}
        dumped = json.dumps(out)
        for value in shown:
            self.assertNotIn(f'"{value}"', dumped)

    def test_the_verdict_rules(self):
        self.assertEqual(fm.answer_step({"investigation": {"answer": [], "gaps": []}},
                                        identify=False)["reason"],
                         "no_margin_and_no_missing_input_named")
        named = fm.answer_step({"investigation": {"answer": [], "gaps": [
            {"reason": "no_item_ledger_entries", "subject": "cost"}]}}, identify=False)
        self.assertEqual((named["status"], named["reason"], named["missing_inputs"]),
                         ("fail", "margin_names_only_missing_inputs", ["no_item_ledger_entries"]))
        bare = fm.answer_step({"investigation": {"answer": [
            {"kind": "computed", "name": "order_margin", "value": "1.00", "inputs": ["line"]},
            {"kind": "computed", "name": "line", "value": "1.00", "basis": ["SalesOrderLine:x"]}],
            "gaps": []}}, identify=False)
        self.assertEqual(bare["reason"], "margin_without_citation")   # no cited property
        refused = fm.answer_step({"investigation": {"error": {"code": "invalid_input"}}},
                                 identify=False)
        self.assertEqual(refused["reason"], "investigate_error:invalid_input")


if __name__ == "__main__":
    unittest.main()
