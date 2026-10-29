"""HS proposals: keyword table, lead tier, never a fact, a person's confirm makes it reviewed.

Evidence label: isolated-fixture (synthetic descriptions).
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ontology import reviews as rv
from operating import hs_proposals as hp
from operating import hs_tariff as hs


class ProposeTest(unittest.TestCase):
    def code(self, text):
        return hp.propose_hs("i", text)

    def test_each_family_of_the_brief(self):
        want = {"Volume shampoo 250ml": "330510", "Repair conditioner": "330590",
                "Hair serum 30ml": "330590", "Argan hair oil": "330590",
                "Face cream 50g": "330499", "Body lotion": "330499", "Hand soap bar": "340111",
                "Liquid soap refill": "340130", "PET bottle 250ml": "392330",
                "Glass jar 50ml": "392330", "Pump cap 24/410": "392350",
                "Printed label front": "482110", "Folding carton 60x60": "481920",
                "Corrugated shipper": "481910", "Floral fragrance oil": "330290",
                "Lavender essential oil": "330129"}
        for text, code in want.items():
            got = self.code(text)
            self.assertEqual(got["hs_code"], code, text)
            self.assertEqual((got["tier"], got["status"]), ("lead", hp.STATUS))

    def test_packaging_beats_product_and_weak_family_only_alone(self):
        self.assertEqual(self.code("Shampoo bottle 250ml")["family"], "bottle")
        self.assertEqual(self.code("Hair mask")["family"], "hair preparation")
        self.assertEqual(self.code("Hair shampoo")["family"], "shampoo")

    def test_ambiguous_and_unmatched_items_get_no_proposal(self):
        amb = self.code("2-in-1 shampoo conditioner")
        self.assertEqual((amb["hs_code"], amb["gap"]), (None, "AMBIGUOUS_FAMILIES"))
        self.assertEqual(self.code("Behentrimonium chloride")["gap"], "NO_KEYWORD_MATCH")
        self.assertEqual(self.code("  ")["gap"], "NO_DESCRIPTION")
        self.assertEqual(self.code("Capsule blend")["gap"], "NO_KEYWORD_MATCH")  # not "cap"

    def test_a_proposal_is_never_a_duty_input(self):
        proposal = self.code("Volume shampoo")
        table = hs.build_hs_category_table([], ["shampoo"])
        self.assertEqual(hs.cite_duty("shampoo", "CN", table, hs.build_tariff_table([]))["status"],
                         "gap")
        self.assertTrue(proposal["hs_code"])

    def test_coverage_by_family_skips_items_with_a_recorded_code(self):
        items = [{"item": "a", "description": "Shampoo", "hs_code": ""},
                 {"item": "b", "description": "Shampoo", "hs_code": "3305.10"},
                 {"item": "c", "description": "Conditioner", "hs_code": None},
                 {"item": "d", "description": "Mystery", "hs_code": None}]
        out = hp.propose_for_items(items)
        self.assertEqual(out["coverage"], {
            "items": 4, "with_recorded_code": 1, "without_code": 3, "proposed": 2,
            "by_family": {"conditioner": 1, "shampoo": 1}, "no_proposal": {"NO_KEYWORD_MATCH": 1}})
        self.assertEqual(sorted(p["item"] for p in out["proposals"]), ["a", "c"])

    def test_cardinality(self):
        for n in (1, 10, 60):
            items = [{"item": f"i{k}", "description": "Shampoo"} for k in range(n)]
            out = hp.propose_for_items(items)
            self.assertEqual(len(out["proposals"]), n)
            self.assertEqual(len({p["item"] for p in out["proposals"]}), n)

    def test_table_is_marked_and_every_code_is_a_six_digit_hs(self):
        self.assertEqual(hp.TABLE["status"], "proposed, needs human confirmation")
        for fam in hp.TABLE["families"]:
            self.assertEqual(len(hs.norm_hs(fam["hs_code"]) or ""), 6, fam["family"])


class ConfirmTest(unittest.TestCase):
    AUTHOR = {"principal": "u1", "name": "Reviewer"}
    EVIDENCE = [{"l1_handle": "h1", "digest": "d1", "field_path": "desc"}]

    def test_a_person_confirm_turns_a_lead_into_reviewed_through_the_review_store(self):
        proposal = hp.propose_hs("I1", "Volume shampoo")
        with tempfile.TemporaryDirectory() as d:
            store = rv.ReviewStore(Path(d) / "reviews.sqlite")
            self.assertEqual(hp.hs_view(None, proposal, None)["tier"], "lead")
            record, created = store.record(**hp.review_kwargs(
                proposal, tenant="t", author=self.AUTHOR, subject="obj-1",
                evidence=self.EVIDENCE))
            self.assertTrue(created)
            force = rv.effective(store.value_history("t", "obj-1"))
            [decision] = force.values()
            view = hp.hs_view(None, proposal, decision)
            self.assertEqual((view["hs_code"], view["tier"]), ("330510", "reviewed"))
            # a correction supersedes; a recorded code outranks both
            store.record(**hp.review_kwargs(proposal, tenant="t", author=self.AUTHOR,
                                            subject="obj-1", evidence=self.EVIDENCE,
                                            corrected_code="3305.90"))
            [latest] = rv.effective(store.value_history("t", "obj-1")).values()
            self.assertEqual(hp.hs_view(None, proposal, latest)["hs_code"], "330590")
            self.assertEqual(hp.hs_view("3304.99", proposal, latest)["tier"], "recorded")

    def test_nothing_to_confirm_or_a_bad_correction_is_refused(self):
        none = hp.propose_hs("I1", "Mystery")
        with self.assertRaises(ValueError):
            hp.review_kwargs(none, tenant="t", author=self.AUTHOR, subject="o",
                             evidence=self.EVIDENCE)
        with self.assertRaises(ValueError):
            hp.review_kwargs(none, tenant="t", author=self.AUTHOR, subject="o",
                             evidence=self.EVIDENCE, corrected_code="nonsense")


if __name__ == "__main__":
    unittest.main()
