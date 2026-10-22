"""The po_three_way kind of the unfamiliar sample: plx.process scored against the oracle.

Evidence label: ``isolated-fixture`` (injected transport). The oracle side reads the
synthetic world of ``evals/oracle/fixture_world.py``; the product side is an injected tool
call (plx.find + plx.process answers shaped like the contract) or the real case host tool
table over a projection built from the same world. Nothing here is live;
``verify/live-checks/eval.unfamiliar_sample.json`` stays live-unverified.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from evals.oracle.cross_sample import THREE_WAY, Sample
from evals.oracle.fixture_world import build_world
from evals.oracle.three_way import ASPECTS
from scripts import eval_unfamiliar_sample as ev
from tests.test_eval_unfamiliar_sample import (ScriptedFind, fixture_sample, holding,
                                               process_answer, root_handle,
                                               three_way_holding)


def three_way_items(drawn: Sample) -> list:
    return [i for i in drawn.items if i.kind == THREE_WAY]


class ScoreTest(unittest.TestCase):
    def setUp(self):
        self.drawn, _, _ = fixture_sample()
        self.items = three_way_items(self.drawn)
        self.assertTrue(self.items)
        # An item whose reading decides every aspect, and one with a short receipt.
        self.full = next(i for i in self.items
                         if all(v["value"] is not None
                                for v in self.drawn.readings[i.hash16]["aspects"].values()))

    def score(self, item, answer=None, *, product=None, reading=None):
        reading = self.drawn.readings[item.hash16] if reading is None else reading
        answer = process_answer(reading) if answer is None else answer
        call = ScriptedFind({item.identifier: [root_handle(item)]},
                            processes={root_handle(item): answer})
        product = product or three_way_holding(call, [item])
        return ev.score_process_item(product, item, reading), call

    def test_a_correct_three_way_answer_passes(self):
        row, call = self.score(self.full)
        self.assertEqual((row["outcome"], row["reason"], row["path"]),
                         ("three_way_correct", "every_held_aspect_agrees", "process"))
        self.assertEqual(row["aspects"], {"agreed": len(ASPECTS), "disagreed": 0,
                                          "undecided": 0, "not_held": 0})
        # Asked through the real tool names: plx.find, then plx.process from its object.
        names = [n for n, _ in call.calls]
        self.assertEqual(names, ["plx.find", "plx.process"])
        self.assertEqual(call.calls[1][1]["process"], ev.PROCESS)

    def test_a_wrong_stage_fails_and_names_it(self):
        reading = self.drawn.readings[self.full.hash16]
        row, _ = self.score(self.full, process_answer(reading, flip=("stage:receipt",)))
        self.assertEqual((row["outcome"], row["reason"]),
                         ("three_way_wrong", "wrong_stage_receipt"))
        self.assertEqual(row["wrong_aspects"], ["stage:receipt"])
        self.assertEqual(row["aspects"]["disagreed"], 1)

    def test_a_wrong_side_fails(self):
        reading = self.drawn.readings[self.full.hash16]
        row, _ = self.score(self.full,
                            process_answer(reading, flip=("side:ordered_received",)))
        self.assertEqual((row["outcome"], row["reason"]),
                         ("three_way_wrong", "wrong_side_ordered_received"))

    def test_an_undecided_answer_is_neither_pass_nor_wrong(self):
        reading = self.drawn.readings[self.full.hash16]
        row, _ = self.score(self.full,
                            process_answer(reading, undecided=("stage:vendor_invoice",)))
        self.assertEqual((row["outcome"], row["reason"]),
                         ("three_way_undecided", "undecided_stage_vendor_invoice"))
        self.assertEqual(row["aspects"]["undecided"], 1)

    def test_aspects_whose_sources_the_product_never_captured_are_not_scored(self):
        # Portal-only product (the fixture build today): BC aspects are counted not_held.
        reading = self.drawn.readings[self.full.hash16]
        answer = process_answer(reading, undecided=("stage:vendor_invoice",
                                                    "side:received_invoiced"))
        call = ScriptedFind({self.full.identifier: [root_handle(self.full)]},
                            processes={root_handle(self.full): answer})
        product = holding(call, [self.full], extra=tuple(
            f"api:{r}" for r in ("public.app_purchase_order_line", "public.app_receipt",
                                 "public.app_receipt_line")))
        row, _ = self.score(self.full, answer, product=product)
        self.assertEqual(row["outcome"], "three_way_correct")
        self.assertEqual(row["aspects"], {"agreed": 3, "disagreed": 0, "undecided": 0,
                                          "not_held": 2})

    def test_the_purchase_order_stage_alone_never_scores_a_pass(self):
        # Review F1 reproduction: a PO with a BC invoice but no Portal receipt, product
        # holding Portal only -> only stage:purchase_order is held and decided.
        reading = {"status": "read", "aspects": {
            "stage:purchase_order": {"value": "present", "sources": ["public.app_purchase_order"]},
            "stage:receipt": {"value": None, "sources": ["public.app_receipt"]},
            "stage:vendor_invoice": {"value": "present", "sources": ["bc:purchaseInvoices"]},
            "side:ordered_received": {"value": None, "sources": []},
            "side:received_invoiced": {"value": None, "sources": []}}}
        call = ScriptedFind({self.full.identifier: [root_handle(self.full)]},
                            processes={root_handle(self.full): process_answer(reading)})
        product = holding(call, [self.full])      # Portal PO only, no BC
        row = ev.score_process_item(product, self.full, reading)
        self.assertEqual((row["outcome"], row["reason"]),
                         ("excluded", "product_not_holding_aspects"))
        self.assertEqual(row["aspects"], {"agreed": 0, "disagreed": 0, "undecided": 0,
                                          "not_held": 1})
        self.assertNotIn("plx.process", [n for n, _ in call.calls])
        # Held BC: the vendor invoice stage is scored, so the item passes on it.
        row = ev.score_process_item(three_way_holding(call, [self.full]), self.full, reading)
        self.assertEqual((row["outcome"], row["aspects"]["agreed"]), ("three_way_correct", 2))
        # An oracle deciding only the purchase_order stage leaves the item unscored.
        only_root = {"status": "read", "aspects": {
            a: dict(v, value=v["value"] if a == "stage:purchase_order" else None)
            for a, v in reading["aspects"].items()}}
        self.assertEqual(ev.score_process_item(product, self.full, only_root)["reason"],
                         "oracle_undecided")

    def test_an_unresolved_root_is_not_resolved_and_process_is_never_asked(self):
        call = ScriptedFind({})
        row = ev.score_process_item(three_way_holding(call, [self.full]), self.full,
                                    self.drawn.readings[self.full.hash16])
        self.assertEqual((row["outcome"], row["reason"]), ("root_not_resolved",
                                                           "find_not_found"))
        self.assertEqual([n for n, _ in call.calls], ["plx.find"])

    def test_blocked_or_undecided_oracle_readings_are_excluded_apart_from_wrong(self):
        call = ScriptedFind({})
        product = three_way_holding(call, [self.full])
        blocked = ev.score_process_item(product, self.full,
                                        {"status": "blocked", "reason": "root_not_found"})
        self.assertEqual((blocked["outcome"], blocked["reason"]),
                         ("excluded", "oracle_blocked"))
        empty = {"status": "read", "aspects": {a: {"value": None, "sources": []}
                                               for a in ASPECTS}}
        self.assertEqual(ev.score_process_item(product, self.full, empty)["reason"],
                         "oracle_undecided")
        self.assertEqual(call.calls, [])

    def test_an_upstream_failure_blocks_the_item_with_its_status_never_scores_it(self):
        from evals.oracle import PortalReads, Sources
        from evals.oracle import keys as K
        from evals.oracle.three_way import reading as oracle_reading

        world = build_world()

        class PortalDown(RuntimeError):
            status, error_code = 502, "replica_lag"

        def read(name, columns):
            if name == K.PO_LINE:
                raise PortalDown("not kept")
            return world.read_relation(name, columns)

        # The oracle's own read failed: blocked upstream with the Portal's status and code.
        failed = oracle_reading(Sources(PortalReads(read), None), K.PO,
                                (("id", "fx-po-1"),))
        self.assertEqual((failed["status"], failed["blocked"], failed["http_status"],
                          failed["error_code"]), ("blocked", "upstream", 502, "replica_lag"))
        call = ScriptedFind({})
        row = ev.score_process_item(three_way_holding(call, [self.full]), self.full, failed)
        self.assertEqual((row["outcome"], row["blocked"], row["http_status"],
                          row["error_code"]), ("blocked", "upstream", 502, "replica_lag"))
        self.assertEqual(call.calls, [])
        # The product's sync of the root's own relation failed upstream: the item is
        # blocked whole, not wrong and not not_held, and nothing is asked.
        product = three_way_holding(call, [self.full])
        product.upstream_failed[K.PO] = {"http_status": 500}
        row = ev.score_process_item(product, self.full, self.drawn.readings[self.full.hash16])
        self.assertEqual((row["outcome"], row["blocked"], row["http_status"],
                          row["reason"]), ("blocked", "upstream", 500, "product_sync_failed"))
        self.assertEqual(call.calls, [])

    def test_an_aspect_whose_sync_failed_is_blocked_alone_and_the_rest_still_scored(self):
        # One aspect's failed source sync must not hide a wrong answer on
        # another aspect. Only the aspects reading the failed relation are blocked.
        from evals.oracle import keys as K

        reading = self.drawn.readings[self.full.hash16]
        failed = sorted(a for a, v in reading["aspects"].items()
                        if K.RECEIPT in v["sources"])
        scorable = [a for a in ASPECTS if a not in failed and a != ev.ROOT_STAGE]
        self.assertTrue(failed and scorable)
        flip = (scorable[0],)
        call = ScriptedFind({self.full.identifier: [root_handle(self.full)]},
                            processes={root_handle(self.full): process_answer(reading, flip)})
        product = three_way_holding(call, [self.full])
        product.upstream_failed[K.RECEIPT] = {"http_status": 500, "error_code": "sync_failed"}
        row = ev.score_process_item(product, self.full, reading)
        self.assertEqual((row["outcome"], row["wrong_aspects"], row["blocked_aspects"]),
                         ("three_way_wrong", list(flip), failed))
        self.assertEqual(row["aspects"], {"agreed": len(ASPECTS) - len(failed) - 1,
                                          "disagreed": 1, "undecided": 0, "not_held": 0,
                                          "blocked": len(failed)})
        self.assertEqual([n for n, _ in call.calls], ["plx.find", "plx.process"])
        # eval.blocked counts the blocked aspects apart from blocked items.
        self.assertEqual(ev.blocked_summary([row], {}),
                         {"items": 0, "kinds": 0, "aspects": len(failed)})
        # The product is wrong only on the blocked aspects and right on the
        # rest. The item is blocked (never three_way_correct) and its kind cannot pass.
        call = ScriptedFind({self.full.identifier: [root_handle(self.full)]},
                            processes={root_handle(self.full):
                                       process_answer(reading, tuple(failed))})
        product = three_way_holding(call, [self.full])
        product.upstream_failed[K.RECEIPT] = {"http_status": 500, "error_code": "sync_failed"}
        row = ev.score_process_item(product, self.full, reading)
        self.assertEqual((row["outcome"], row["blocked"], row["http_status"],
                          row["blocked_aspects"], row["aspects"]["disagreed"]),
                         ("blocked", "upstream", 500, failed, 0))
        self.assertNotIn(row["outcome"], ev.PASSING)
        kind = ev.kind_result([dict(row, kind=THREE_WAY)], {"candidates": 1}, THREE_WAY)
        self.assertEqual((kind["status"], kind["pass"]), ("blocked", "0/0"))
        correct, _ = self.score(self.full)
        kind = ev.kind_result([correct, row], {"candidates": 2}, THREE_WAY)
        self.assertNotEqual(kind["status"], "passed")   # one item's aspect is blocked
        # With every other aspect blocked or not held, the item is blocked whole.
        call = ScriptedFind({})
        portal_only = ev.Product(call, {"source": "injected", "objects": 10},
                                 audience_keys={f"api:{K.PO}"},
                                 upstream_failed={K.RECEIPT: {"http_status": 500}})
        row = ev.score_process_item(portal_only, self.full, reading)
        self.assertEqual((row["outcome"], row["blocked"], row["http_status"],
                          row["blocked_aspects"]), ("blocked", "upstream", 500, failed))
        self.assertEqual(call.calls, [])

    def test_a_process_tool_error_is_undecided_with_its_code(self):
        row, _ = self.score(self.full, {"error": {"code": "object_not_found"}})
        self.assertEqual((row["outcome"], row["reason"]),
                         ("three_way_undecided", "tool_error_object_not_found"))

    def test_by_kind_passes_on_a_correct_answer_and_not_on_wrong_ones(self):
        def report(flip):
            answers = {i.identifier: [root_handle(i)] for i in self.drawn.items}
            processes = {root_handle(i): process_answer(self.drawn.readings[i.hash16], flip)
                         for i in self.items}
            product = three_way_holding(ScriptedFind(answers, processes=processes),
                                        self.drawn.items)
            return ev.run_eval(self.drawn, product, run_id="seed-a",
                               evidence_label="injected-transport", configured={})

        good = report(())["eval"]["by_kind"][THREE_WAY]
        self.assertEqual(good["status"], "passed")
        self.assertEqual(good["three_way_correct"], len(self.items))
        self.assertEqual(good["pass"], f"{len(self.items)}/{len(self.items)}")
        bad_report = report(("stage:purchase_order",))
        bad = bad_report["eval"]["by_kind"][THREE_WAY]
        self.assertEqual((bad["status"], bad["three_way_wrong"], bad["pass"]),
                         ("no_correct_answer", len(self.items), f"0/{len(self.items)}"))
        self.assertIn(THREE_WAY, bad_report["eval"]["kinds_missing"])
        self.assertEqual(ev.verdict(bad_report), 3)
        self.assertEqual(bad_report["row_data_check"]["leaked"], 0)


class EmptyStratumTest(unittest.TestCase):
    def test_no_candidates_reports_empty_with_the_source_tried(self):
        from evals.oracle import BcReads, PortalReads, Sources
        from evals.oracle import keys as K
        from evals.oracle.cross_sample import THREE_WAY_TRIED, sample
        world = build_world()
        world.relations[K.RECEIPT] = []
        sources = Sources(PortalReads(world.read_relation), None)
        drawn = sample(sources, run_id="seed-a", count=28, documents=world.documents())
        report = ev.run_eval(drawn, ev.Product(None, {"status": "disabled"}), run_id="seed-a",
                             evidence_label="injected-transport", configured={})
        entry = report["eval"]["by_kind"][THREE_WAY]
        self.assertEqual((entry["status"], entry["reason"], entry["candidates"], entry["items"]),
                         ("empty", "no_candidates", 0, 0))
        self.assertEqual(report["eval"]["sources_tried"][THREE_WAY], list(THREE_WAY_TRIED))
        self.assertIn(THREE_WAY, report["eval"]["kinds_missing"])


class RealProductPathTest(unittest.TestCase):
    """plx.process from the case host's tool table over a projection of the fixture world."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.product = ev.fixture_product(build_world(), Path(cls.tmp.name) / "product")
        cls.drawn, _, _ = fixture_sample()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_build_syncs_the_process_graph_relations_derived_from_the_schema(self):
        from ontology.schema import load_schema
        wanted = ev.process_relations(load_schema())
        for relation in ("public.app_purchase_order", "public.app_purchase_order_line",
                         "public.app_receipt", "public.app_receipt_line", "public.app_product"):
            self.assertIn(relation, wanted)
            self.assertTrue(self.product.holds_relation(relation), relation)
        self.assertIn("plx.process", self.product.info["tools"])
        self.assertIn(THREE_WAY, self.product.held_record_kinds())
        self.assertFalse(self.product.holds_source("bc:purchaseInvoices"))

    def test_the_real_tool_answers_from_a_resolved_root_in_contract_shape(self):
        from ontology.project import object_id_of
        item = three_way_items(self.drawn)[0]
        result = self.product.process(object_id_of("PurchaseOrder", "portal.app_purchase_order",
                                                   dict(item.key)))
        self.assertNotIn("error", result)
        self.assertEqual(result["process"], ev.PROCESS)
        self.assertLessEqual({"purchase_order", "receipt", "vendor_invoice"},
                             {s["stage"] for s in result["stages"]})
        answer = ev.product_aspects(result)
        self.assertEqual(set(answer), set(ASPECTS))
        # Measured, not asserted: whatever it decides is checked against the oracle in the
        # run below; nothing it leaves undecided is counted wrong.

    def test_a_run_measures_the_kind_without_leaking_row_data(self):
        report = ev.run_eval(self.drawn, self.product, run_id="seed-a",
                             evidence_label="injected-transport", configured={})
        rows = [r for r in report["items"] if r["kind"] == THREE_WAY]
        self.assertEqual(len(rows), self.drawn.kinds[THREE_WAY]["items"])
        for row in rows:
            self.assertIn(row["outcome"], ev.THREE_WAY_OUTCOMES + ("excluded",))
            # BC is not captured by this build: its aspects are never scored.
            self.assertGreaterEqual(row["aspects"]["not_held"], 1)
        self.assertEqual(report["row_data_check"]["leaked"], 0)
        text = json.dumps(report)
        for item in three_way_items(self.drawn):
            self.assertNotIn(item.identifier, text)


if __name__ == "__main__":
    unittest.main()
