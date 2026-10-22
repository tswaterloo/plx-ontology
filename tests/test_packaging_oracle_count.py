"""The oracle consumes the real producer's integer line count, and resolves the requested
purchase order the way the served relations do (injected relation reads, no network)."""
import unittest

from scripts import eval_packaging_oracle_check as oracle


class PackagingOracleCountTest(unittest.TestCase):
    def case(self, lines):
        return {
            "root": {"relation": "public.app_purchase_order", "id": "synthetic-root"},
            "lines": lines,
            "checks": {"po_cited": True, "lines_cited": True},
        }

    def reading(self, lines=4, requested=True, found=1):
        return {
            "status": "read", "roots": 1, "requested": found,
            "requested_is_root": requested, "lines": lines, "subtotal": None,
        }

    def test_integer_producer_count_is_compared_and_reported(self):
        case = self.case(4)
        result = oracle.compare(case, 0, self.reading())
        self.assertEqual((result["status"], result["case_lines"]), ("agree", 4))
        self.assertTrue(result["lines_agree"])
        report = oracle.report(case, 0, result, "isolated-fixture")
        self.assertEqual(report["case"]["lines"], 4)

    def test_legacy_list_shape_stays_supported(self):
        case = self.case([{}, {}, {}, {}])
        result = oracle.compare(case, 0, self.reading())
        self.assertEqual((result["status"], result["case_lines"]), ("agree", 4))
        self.assertEqual(oracle.report(case, 0, result, "isolated-fixture")["case"]["lines"], 4)

    def test_wrong_counts_do_not_trust_the_case_flags(self):
        for lines in (3, 5, [{}, {}, {}], [{}, {}, {}, {}, {}]):
            with self.subTest(lines=lines):
                result = oracle.compare(self.case(lines), 0, self.reading())
                self.assertEqual(result["status"], "disagree")
                self.assertFalse(result["lines_agree"])
                self.assertIn("line_count_differs", result["reasons"])

    def test_explicit_zero_is_a_valid_count(self):
        case = self.case(0)
        result = oracle.compare(case, 0, self.reading(lines=0))
        self.assertEqual((result["status"], result["case_lines"]), ("agree", 0))
        self.assertEqual(oracle.report(case, 0, result, "isolated-fixture")["case"]["lines"], 0)

    def test_invalid_counts_cannot_agree_with_an_empty_oracle(self):
        for lines in (None, True, False, -1, "0", 0.0, {}, ()):
            with self.subTest(lines=lines):
                case = self.case(lines)
                result = oracle.compare(case, 0, self.reading(lines=0))
                self.assertEqual(result["status"], "disagree")
                self.assertFalse(result["lines_agree"])
                self.assertIsNone(result["case_lines"])
                self.assertIsNone(oracle.report(case, 0, result, "isolated-fixture")["case"]["lines"])

    def test_missing_count_is_unknown_not_zero(self):
        case = self.case(0)
        del case["lines"]
        result = oracle.compare(case, 0, self.reading(lines=0))
        self.assertEqual(result["status"], "disagree")
        self.assertIsNone(result["case_lines"])

    def test_wrong_root_stays_a_failure(self):
        for lines in (4, [{}, {}, {}, {}]):
            with self.subTest(lines=lines):
                result = oracle.compare(self.case(lines), 0, self.reading(requested=False))
                self.assertEqual(result["status"], "disagree")
                self.assertFalse(result["root_agrees"])
                self.assertFalse(result["lines_agree"])
                self.assertIn("root_not_requested_po", result["reasons"])

    def test_failed_case_stays_a_failure(self):
        self.assertEqual(oracle.compare(self.case(4), 1, self.reading())["status"], "case_failed")
        self.assertEqual(oracle.compare(None, 0, self.reading())["status"], "case_failed")


PO, PO_LINE = "public.app_purchase_order", "public.app_purchase_order_line"
FM_PO = "public.generated_purchase_order"


class PackagingOracleRootTest(unittest.TestCase):
    """``oracle_reading`` + ``compare`` over injected served relations: the requested number
    reaches the case's root in the normal form ``plx.find`` searches by, directly or over
    the FM link; a number no served row carries is a typed gap."""

    def run_check(self, gold, fm, number, root_id="gold-a", case_lines=2):
        tables = {PO: gold, FM_PO: fm,
                  PO_LINE: [{"id": f"line-{i}", "purchase_order_id": "gold-a"}
                            for i in range(2)]}
        reads = []

        def read_relation(relation, columns):
            reads.append(relation)
            return {"rows": [dict(r) for r in tables.get(relation, [])], "key": ["id"]}

        reading = oracle.oracle_reading(read_relation, PO, root_id, number)
        case = {"root": {"relation": PO, "id": root_id}, "lines": case_lines}
        return reading, oracle.compare(case, 0, reading), reads

    def test_root_reached_over_the_fm_link_with_a_case_different_number_agrees(self):
        gold = [{"id": "gold-a", "po_number": "BC-0001",
                 "custom_fields": {"fmGeneratedPoId": 71}},
                {"id": "gold-b", "po_number": "BC-0002", "custom_fields": {}}]
        fm = [{"id": 71, "ponumber": " po-71 "}, {"id": 72, "ponumber": "PO72"}]
        reading, check, _ = self.run_check(gold, fm, "PO71")
        self.assertEqual((reading["requested_via"], reading["requested"]), ("link", 1))
        self.assertEqual((check["status"], check["root_agrees"], check["lines_agree"]),
                         ("agree", True, True))
        # The same link reaching another gold row is a disagreement, not a gap.
        _, check, _ = self.run_check(gold, fm, "po71", root_id="gold-b")
        self.assertEqual((check["status"], check["root_agrees"], check["reasons"]),
                         ("disagree", False, ["root_not_requested_po"]))

    def test_a_direct_document_name_wins_and_is_case_insensitive(self):
        gold = [{"id": "gold-a", "po_number": "po9", "custom_fields": {}},
                {"id": "gold-b", "po_number": "BC-2", "custom_fields": {"fmGeneratedPoId": 9}}]
        reading, check, reads = self.run_check(gold, [{"id": 9, "ponumber": "PO9"}], "PO9")
        self.assertEqual((reading["requested_via"], check["status"]), ("number", "agree"))
        self.assertNotIn(FM_PO, reads)

    def test_a_spelling_plx_find_normalises_is_the_same_row_never_a_gap(self):
        """The fixture's own spelling PO#5447 is the row
        ``plx.find`` resolves PO5447 to (``ontology.identifiers.normalize``), so the oracle
        reaches a verdict on it: agree on that row, disagree on any other, never a gap."""
        from ontology import identifiers

        self.assertEqual(identifiers.normalize("petra_po", "PO#5447"),
                         identifiers.normalize("petra_po", "PO5447"))
        gold = [{"id": "gold-a", "po_number": "PO#5447", "custom_fields": {}},
                {"id": "gold-b", "po_number": "PO5448", "custom_fields": {}}]
        reading, check, _ = self.run_check(gold, [], "PO5447")
        self.assertEqual((reading["requested_via"], check["status"]), ("number", "agree"))
        _, check, _ = self.run_check(gold, [], "PO5447", root_id="gold-b")
        self.assertEqual((check["status"], check["reasons"]),
                         ("disagree", ["root_not_requested_po"]))
        # A number with no normal form is never looked up literally: undecided, never a gap.
        _, check, _ = self.run_check(gold, [], "not a po")
        self.assertEqual((check["status"], check["reasons"]),
                         ("oracle_undecided", [oracle.NOT_NORMALIZABLE]))

    def test_a_number_no_served_row_carries_is_a_typed_gap(self):
        from operating.cases import GAP_STATES

        gold = [{"id": "gold-a", "po_number": "PO#72",
                 "custom_fields": {"fmGeneratedPoId": 71}}]
        for fm in ([{"id": 71, "ponumber": "71"}], []):
            with self.subTest(fm=fm):
                reading, check, _ = self.run_check(gold, fm, "PO71")
                self.assertEqual(reading["requested"], 0)
                self.assertEqual(check["status"], "oracle_gap")
                self.assertIsNone(check["root_agrees"])
                self.assertEqual(check["gap"], {"state": "absent_in_source",
                                                "reason": oracle.NO_PORTAL_ROW})
                self.assertEqual(check["reasons"], [oracle.NO_PORTAL_ROW])
                self.assertTrue(check["lines_agree"])
                self.assertIn(check["gap"]["state"], GAP_STATES)
        # A gap never masks a real mismatch: lines that differ beside it disagree, and the
        # report names no gap (the spec's blocked_when cannot fire).
        for case_lines in (3, None):
            with self.subTest(case_lines=case_lines):
                _, check, _ = self.run_check(gold, [], "PO71", case_lines=case_lines)
                self.assertEqual((check["status"], check["lines_agree"], check["reasons"]),
                                 ("disagree", False,
                                  [oracle.NO_PORTAL_ROW, "line_count_differs"]))
                report = oracle.report({"root": {"relation": PO, "id": "gold-a"},
                                        "lines": case_lines}, 0, check, "isolated-fixture")
                self.assertIsNone(report["oracle_gap"])
        # A gap is never a pass, and a case root the oracle cannot find stays a failure.
        _, check, _ = self.run_check(gold, [], "PO71", root_id="gold-x")
        self.assertEqual((check["status"], check["root_agrees"]), ("disagree", False))
        self.assertIn("root_not_found", check["reasons"])

    def test_a_served_fm_row_with_no_link_to_the_root_relation_is_its_own_gap(self):
        gold = [{"id": "gold-a", "po_number": "BC-1",
                 "custom_fields": {"fmGeneratedPoId": 71}}]
        reading, check, _ = self.run_check(gold, [{"id": 80, "ponumber": "po71"}], "PO71")
        self.assertEqual((reading["requested"], reading["unlinked_carriers"]), (0, 1))
        self.assertEqual((check["status"], check["root_agrees"], check["reasons"]),
                         ("oracle_gap", None, [oracle.SERVED_UNLINKED]))
        # The unconfirmed link is an unknown in the case's one gap vocabulary.
        self.assertEqual(check["gap"], {"state": "unknown", "reason": oracle.SERVED_UNLINKED})
        report = oracle.report({"root": {"relation": PO, "id": "gold-a"}, "lines": 2}, 0,
                               check, "isolated-fixture")
        self.assertEqual(report["oracle_gap"], oracle.SERVED_UNLINKED)
        # Agreement and disagreement name no gap.
        _, check, _ = self.run_check(gold, [{"id": 71, "ponumber": "po71"}], "PO71")
        self.assertEqual(check["status"], "agree")
        self.assertIsNone(oracle.report({}, 0, check, "isolated-fixture")["oracle_gap"])

    def test_an_unreadable_link_relation_blocks_instead_of_gapping(self):
        def read_relation(relation, columns):
            if relation == FM_PO:
                raise TimeoutError("down")
            return {"rows": [{"id": "gold-a", "po_number": "BC-1", "custom_fields": {}}]}

        reading = oracle.oracle_reading(read_relation, PO, "gold-a", "PO71")
        check = oracle.compare({"root": {"relation": PO, "id": "gold-a"}, "lines": 0}, 0,
                               reading)
        self.assertEqual(check["status"], "oracle_blocked")


if __name__ == "__main__":
    unittest.main()
