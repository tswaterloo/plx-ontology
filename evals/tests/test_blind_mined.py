"""The blind mined family: mining filters, the general oracle, the standalone question, the
perturbations, the split, the fail-closed exclusion and a scorer dry run.
Evidence label: isolated-fixture (made-up rows and questions, a scripted agent)."""

from __future__ import annotations

from datetime import date
import unittest

from evals import blind_mined as bm
from evals import live_ops as lo
from evals import runner
from evals.tests import fixtures as fx
from evals.tests import test_live_ops as tlo
from scripts import eval_blind_mined as script

AS_OF = date(2026, 11, 5)
H_PO = tlo.H_PO
H_BC = "bc://production/c0ffee00-0000-0000-0000-000000000001/purchaseOrders/" \
       "c0ffee00-0000-0000-0000-0000000000aa@fixture-1"


def row(prop: str, value, value_type: str = "text", handle: str = H_PO,
        lineage: str = "gold_native_staging", oid: str = "PurchaseOrder:fx:1") -> dict:
    return {"object_id": oid, "object_type": "PurchaseOrder", "property": prop,
            "value_type": value_type, "value": value, "l1_handle": handle, "lineage": lineage,
            "markings": "[]"}


class MiningTests(unittest.TestCase):
    def test_business_questions_kept_and_personal_or_chatter_dropped(self):
        self.assertTrue(bm.topical("did PO 9001 arrive yet?"))
        for text in ("are you taking vacation next week?", "how was the party on Friday?",
                     "can we talk about the order tomorrow morning?",
                     "is the invoice at bob@example.test?"):
            self.assertFalse(bm.topical(text), text)
        lines = bm.question_lines("Hi Ana, did PO 9001 arrive? Thanks.\nFrom: x\nold?")
        self.assertEqual(lines, ["did PO 9001 arrive?"])
        for text in ("can you please ship PO 9001 today?", "should we cancel PO 9001?",
                     "PO 9001 is late, can you please chase it?"):
            self.assertFalse(bm.topical(text), text)

    def test_codes_keep_their_kind_word_and_skip_dates_and_measures(self):
        codes = bm.codes_in("is lot ZZ-4471 released, and did 12/03 or 500kg ship?")
        self.assertEqual([(c.text, c.word) for c in codes], [("ZZ-4471", "lot")])
        self.assertIn("4471", bm.lookup_forms("zz-4471"))


class OracleTests(unittest.TestCase):
    def test_one_value_answers_and_mail_rows_never_count(self):
        question = "when is PO 9001 expected to arrive?"
        rows = [row("expected_date", "2026-09-01", "date"),
                row("expected_date", "2026-12-24", "date", lineage="mail")]
        got = bm.answer(bm.ask_of(question), rows, AS_OF, question)
        self.assertEqual((got.kind, got.facts[0]["kind"], got.facts[0]["value"]),
                         ("value", "date", "2026-09-01"))
        self.assertEqual(lo.key_problems({**tlo.key(*got.facts), "item_id": "x"}), [])

    def test_empty_everywhere_is_not_known_and_two_values_settle_nothing(self):
        question = "what is the ETA for PO 9001?"
        ask = bm.ask_of(question)
        empty = bm.answer(ask, [row("expected_date", None, "date"),
                                row("expected_date", "", "date", handle=H_BC,
                                    lineage="bc_manufacturing")], AS_OF, question)
        self.assertEqual((empty.kind, empty.facts[0]["kind"]), ("not_known", "absent"))
        self.assertEqual(sorted(empty.facts[0]["allowed"]), ["api", "bc"])
        split = bm.answer(ask, [row("expected_date", "2026-09-01", "date"),
                                row("expected_date", "2026-09-09", "date", handle=H_BC,
                                    lineage="bc_manufacturing")], AS_OF, question)
        self.assertIsNone(split)
        self.assertIsNone(bm.ask_of("why is PO 9001 late?"))

    def test_a_flag_reads_booleans_and_states_its_polarity(self):
        question = "did PO 9001 come in?"
        got = bm.answer(bm.ask_of(question),
                        [row("bc_fully_received", "false", "boolean", H_BC,
                             "bc_manufacturing")], AS_OF, question)
        # a linked shipment answers a shipping question; a draft one says nothing
        shipped = "did order 9001 ship?"
        linked = [dict(row("ship_date", "2026-10-02", "date", oid="Shipment:fx:9"),
                       via="Shipment")]
        self.assertEqual(bm.answer(bm.ask_of(shipped), linked, AS_OF, shipped)
                         .facts[0]["value"], "shipped")
        draft = linked + [dict(row("status", "DRAFT", oid="Shipment:fx:9"), via="Shipment")]
        self.assertIsNone(bm.answer(bm.ask_of(shipped), draft, AS_OF, shipped))
        self.assertEqual((got.facts[0]["value"], got.facts[0]["allowed"]),
                         ("not received", ["bc"]))
        self.assertIn("received", got.facts[0]["contradicts"])


class QuestionTests(unittest.TestCase):
    def test_standalone_question_has_the_record_and_the_anchor(self):
        code = bm.codes_in("can you check if po#9001 came in?")[0]
        asked = bm.normalize("can you check if po#9001 came in?", code, "PO 9001", AS_OF)
        self.assertEqual(asked, "As of November 5, 2026, PO 9001 came in?")
        pronoun = bm.normalize("did you receive it?", bm.Code("", "", 0, 0), "PO 9001", AS_OF)
        self.assertEqual(pronoun, "As of November 5, 2026, did we receive PO 9001?")

    def test_perturbations_keep_the_code_resolvable(self):
        question = "As of November 5, 2026, when is PO ZZ9001 expected?"
        self.assertIn("ZZ9001", bm.typo(question, "s", keep="PO ZZ9001"))
        other = bm.recode("ZZ9001", "s")
        self.assertNotEqual(other, "ZZ9001")
        self.assertIn("9001", other)
        self.assertTrue(bm.relative(question, AS_OF, "s").endswith(
            ", when is PO ZZ9001 expected?"))
        self.assertEqual(bm.vague_name("Fixturetown Packaging Co", ["Other Soap Ltd"]),
                         "Fixturetown")
        self.assertIsNone(bm.vague_name("Fixturetown Packaging Co", ["Fixturetown Labs"]))

    def test_deal_never_splits_a_group_and_keeps_most_items_held_out(self):
        groups = [[0, 1, 2], [3, 4], [5], [6, 7], [8], [9, 10, 11], [12], [13, 14]]
        dealt = bm.deal(groups, 0.2, "seed", forced_dev={12})
        for group in groups:
            self.assertEqual(len({dealt[i] for i in group}), 1)
        self.assertEqual(dealt[12], "dev")
        self.assertGreater(sum(v == "held_out" for v in dealt.values()), 10)


class FakeProjection:
    def __init__(self, rows):
        self.rows = rows

    def resolve(self, code):
        if code.text.upper() == "ZZ9001":
            return "PurchaseOrder", "ZZ9001", [("PurchaseOrder:fx:1", "fx")]
        return None

    def values(self, objects, object_type):
        return self.rows

    def neighbours(self, objects, types):
        return []

    def binding_rows(self, binding, props):
        return [(r["object_id"], r["property"], r["value"]) for r in self.rows
                if r["property"] in props]


class BuildTests(unittest.TestCase):
    ENTRY = {"question": "any update on when PO ZZ9001 is expected to arrive?", "system": "graph-mail",
             "handle": "graph-mail://fixture/1", "at": "2026-10-01T00:00:00Z",
             "subject": "", "people": []}

    def test_refuses_without_a_usable_exclusion_list(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as state:
            with self.assertRaises(script.Refused):
                script.mined(Path(state), {})

    def test_built_items_validate_and_grade_through_the_live_ops_scorer(self):
        rows = [row("expected_date", "2026-09-01", "date"),
                row("vendor_name", "Fixturetown Packaging Co"),
                row("order_date", "2026-08-03", "date")]
        items, keys, stats = script.build([self.ENTRY], FakeProjection(rows), as_of=AS_OF,
                                          generation="00000001", now="2026-11-05T00:00:00Z",
                                          dev_share=0.5)
        self.assertEqual(stats["bases"], 1)
        self.assertEqual(len({i["split"] for i in items}), 1)   # one record: one group
        for item, key in zip(items, keys):
            self.assertEqual(runner.validate_item(item), [])
            self.assertEqual(lo.key_problems(key), [])
            self.assertEqual(key["suite"], "blind")
        base = items[0]
        self.assertEqual(base["question"]["text"],
                         "As of November 5, 2026, when PO ZZ9001 is expected to arrive?")
        # a scorer dry run on the fixture host: the right day, cited, is correct
        item = dict(base, id="dev-F29-0001")
        key = dict(keys[0], item_id="dev-F29-0001")

        def script_(prompt, call):
            call("plx.find", {"relation": "public.app_purchase_order"})
            return fx.final_text(tlo.answer("PO ZZ9001 is expected 2026-09-01.",
                                            tlo.PO_VALUES))
        transcript = runner.record(item, runner.ScriptedAgent(script_), tlo.Host())
        self.assertEqual(lo.verdict(item, key, transcript, reader="vince")["outcome"],
                         "correct")


if __name__ == "__main__":
    unittest.main()
