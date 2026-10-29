"""Count and money claims at the claim gate (directive 03e9304b8807, gap-82b852bc58c9).

Each test is one defect the claim-count-sum-binding review found: a past-moment count
binding a present claim, one passing claim masking another exact restatement, a money sum
passing with a currency unit and no currency, cross-source records undercounted, the case
paths calling the gate without the tool evidence they hold, and number words read too
narrowly or too widely. Evidence label: ``isolated-fixture`` (synthetic receipt and order
rows; the case paths run over ``FakeWorkbench``). Nothing here is live data.
"""

from __future__ import annotations

import unittest

from operating import case_verification, claim_checks
from operating import playbook_eval as pe
from operating.tests import test_customer_po_coverage as cpo_tests
from operating.tests import test_playbook_eval as pe_tests
from operating.tests.test_claim_checks import (bc, bc_view, checked, codes, gate, money, op,
                                               receipt, sales_order)


def count_result(handles, value=None, **result):
    """A ``plx.aggregate`` count answer listing ``handles`` in one group (contract shape)."""
    group = {"key": {}, "value": None if value is False else str(
        len(handles) if value is None else value), "inputs": len(handles), "markings": [],
        "inputs_citations": [{"l1_handle": h, "field_path": "row.id", "digest": "0" * 64,
                              "lineage": "portal_native"} for h in handles]}
    return {"groups": [group], "partial": False, "gaps": [],
            "completeness": {"complete": True, "truncated": False, "returned": 1, "total": 1},
            "meta": {}, **result}


COUNT_CALL = {"set": "set:s1", "measure": {"property": "receipt_number", "op": "count"}}


def counted(handles, arguments=None, **result):
    return claim_checks.returned_evidence("plx.aggregate", {**COUNT_CALL, **(arguments or {})},
                                          count_result(handles, **result))


def count_of(handles, value=None, role=None):
    return op(str(len(handles) if value is None else value), "count",
              [{"citation": h} for h in handles], role=role)


class CountClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = dict(receipt(index) for index in range(3))
        self.handles = list(self.rows)
        self.claim = count_of(self.handles)

    def check(self, text, claims=None, returned=None, outcome=None, rows=None, handles=None):
        return gate(text, handles or self.handles, claims or [self.claim],
                    rows=rows or self.rows, calculations=(), returned=returned, outcome=outcome)

    def test_a_count_known_at_a_past_moment_binds_no_present_exact_count(self):
        # Item 1: members first captured after a past as_known_at are not in its count.
        past = counted(self.handles, {"as_known_at": "2026-01-01T00:00:00Z"})
        self.assertEqual(codes(self.check("3 receipts.", returned=past)),
                         ["claim_count_lower_bound"])
        self.assertTrue(self.check("At least 3 receipts.", returned=past)["passed"])
        now = self.check("3 receipts.", returned=counted(self.handles))
        self.assertTrue(now["passed"], now["failures"])
        self.assertEqual(checked(now)[0]["bound"], "exact")

    def test_each_exact_restatement_of_a_lower_bound_count_is_checked(self):
        # Item 2: a passing claim of the same number covers one occurrence, not all.
        first = self.handles[0]
        rows = {**self.rows, first: {**self.rows[first],
                                     "row": {**self.rows[first]["row"], "lines": 3}}}
        lines = {"value": "3", "citation": first, "field": "row.lines"}
        claims = [self.claim, lines]
        self.assertTrue(self.check("At least 3 receipts; the first has 3 lines.", claims,
                                   rows=rows)["passed"])
        for text, outcome in (("3 receipts; the first has 3 lines.", None),
                              ("At least 3 receipts; the first has 3 lines.",
                               "3 receipts arrived; the first has 3 lines.")):
            with self.subTest(text=text, outcome=outcome):
                self.assertEqual(codes(self.check(text, claims, rows=rows, outcome=outcome)),
                                 ["claim_count_lower_bound"])
        # The same claim made by two items covers one occurrence of the outcome only.
        parsed = claim_checks.parse_value_claims(claims, self.handles, [])
        items = [{"id": ident, "texts": ["At least 3 receipts; the first has 3 lines."],
                  "value_claims": parsed,
                  "citations": [{"handle": h, "status": "resolved"} for h in self.handles]}
                 for ident in ("F1", "F2")]
        report = claim_checks.check_items(items, rows=rows.get,
                                          outcome="3 receipts; the first has 3 lines.")
        self.assertEqual([(item["item"], item["code"]) for item in report["failures"]],
                         [("outcome", "claim_count_lower_bound")])

    def test_number_words_state_a_count_unless_they_count_something_else(self):
        # Item 6: hyphen compounds, dozens and words counting any noun that may name the
        # records state the count; the count's own word counting a noun that names no
        # records (systems, days) does not where another number states the count.
        dozen = dict(receipt(index) for index in range(12))
        twelve = count_of(list(dozen))
        for text, handles, claim, rows, failed in (
                ("A three-receipt delivery arrived.", None, None, None, True),
                ("Three arrived.", None, None, None, True),
                ("Receipts: three.", None, None, None, True),
                ("Three late receipts arrived.", None, None, None, True),
                ("Three shipments arrived.", None, None, None, True),
                ("We received three deliveries.", None, None, None, True),
                ("Three purchase orders were received.", None, None, None, True),
                ("The supplier sent three cartons.", None, None, None, True),
                ("At least 3 receipts; three shipments arrived.", None, None, None, True),
                ("Twelve shipments arrived.", list(dozen), twelve, dozen, True),
                ("A dozen receipts arrived.", list(dozen), twelve, dozen, True),
                ("At least a dozen receipts arrived.", list(dozen), twelve, dozen, False),
                ("At least 3 receipts, held in three systems.", None, None, None, False),
                ("At least 3 receipts across three days.", None, None, None, False),
                ("Receipts are held in three systems.", None, None, None, True)):
            with self.subTest(text=text):
                self.assertEqual(codes(self.check(text, [claim] if claim else None,
                                                  rows=rows, handles=handles)),
                                 ["claim_count_lower_bound"] if failed else [])
        pair = dict(receipt(index) for index in range(2))
        two = count_of(list(pair))
        self.assertTrue(self.check("At least 2 receipts, held in two systems.", [two],
                                   rows=pair, handles=list(pair))["passed"])
        self.assertEqual(codes(self.check("Two receipts, held in two systems.", [two],
                                          rows=pair, handles=list(pair))),
                         ["claim_count_lower_bound"])

    def test_records_of_two_sources_count_as_the_union_an_identity_lookup_told_apart(self):
        # Item 4: N records per source, as many again in BC, all listed by one count group a
        # tool's identity lookup left a value (a partial answer: no exact count binds).
        for size in (1, 10, 50):
            with self.subTest(size=size):
                rows = dict(receipt(index) for index in range(size))
                for index in range(size):
                    handle = bc("purchaseReceipts", f"pr-{index}")
                    rows[handle] = bc_view(handle, id=f"pr-{index}")
                handles = list(rows)
                union = count_of(handles)
                told = counted(handles, partial=True)
                result = self.check(f"At least {2 * size} receipts.", [union], told, rows=rows,
                                    handles=handles)
                self.assertTrue(result["passed"], result["failures"])
                self.assertEqual(checked(result)[0]["bound"], "at_least")
                # No lookup told them apart (none returned, twins, a past moment): they may be
                # one record held in two systems, so at least the larger source's number.
                for name, given in (("none", None), ("twins", counted(handles, value=False)),
                                    ("past", counted(handles, {"as_known_at": "2026-01-01"}))):
                    with self.subTest(lookup=name):
                        refused = self.check(f"At least {2 * size} receipts.", [union], given,
                                             rows=rows, handles=handles)
                        self.assertEqual(codes(refused), ["claim_value_mismatch"])
                        self.assertIn(f"at least {size},", refused["failures"][0]["detail"])
                        self.assertTrue(self.check(
                            f"At least {size} receipts.", [{**union, "value": str(size)}],
                            given, rows=rows, handles=handles)["passed"])

    def test_a_money_value_holds_the_currency_its_cells_are_in(self):
        # Item 3: a currency unit over values nobody typed money is no money; one cited
        # money value can state its currency.
        amounts = [{"citation": h, "field": "row.amount"} for h in self.handles]
        for name, claim in (
                ("sum unit", op("3.75", "sum", amounts, unit="USD")),
                ("input unit", op("3.75", "sum", [{**cell, "unit": "USD"} for cell in amounts])),
                ("price unit", op("3.75", "sum", amounts, unit="USD/EA"))):
            with self.subTest(case=name):
                self.assertEqual(codes(self.check("They total 3.75.", [claim])),
                                 ["claim_currency_mismatch"])
        self.assertTrue(self.check("They total 3.75.",
                                   [op("3.75", "sum", amounts, unit="BOX")])["passed"])
        orders = dict(sales_order(index, "100.50") for index in (1, 2))
        order = list(orders)[0]
        returned = {"counts": [], "currencies": {}}
        claim_checks.returned_evidence("plx.object", {}, {"values": [
            money(order, "row.total_amount", "USD")]}, returned)
        value = {"value": "100.50", "citation": order, "field": "row.total_amount",
                 "currency": "USD"}
        result = self.check("The order totals 100.50 USD.", [value], returned, rows=orders,
                            handles=list(orders))
        self.assertTrue(result["passed"], result["failures"])
        for name, claim, given in (("another currency", {**value, "currency": "CAD"}, returned),
                                   ("unit disagrees", {**value, "unit": "CAD"}, returned),
                                   ("no tool returned it", value, None)):
            with self.subTest(case=name):
                self.assertEqual(codes(self.check("The order totals 100.50.", [claim], given,
                                                  rows=orders, handles=list(orders))),
                                 ["claim_currency_mismatch"])
        first = self.handles[0]
        self.assertEqual(codes(self.check("0.25.", [{"value": "0.25", "citation": first,
                                                     "field": "row.amount",
                                                     "currency": "USD"}])),
                         ["claim_currency_mismatch"])


def order_count(handles):
    return {"value": str(len(handles)), "op": "count",
            "of": [{"citation": h} for h in handles], "role": "order_count"}


class CaseVerificationEvidenceTests(cpo_tests.ProposalTestCase):
    def test_check_claims_and_verify_proposal_bind_counts_to_the_tool_evidence_given(self):
        # Item 5: the case paths reach the gate with the tool evidence their caller holds.
        rows = self.rt.mirror.rows(self.both, "public.app_sales_order")["rows"]
        handles = [row["citation"] for row in rows][:3]
        statement = "Three sales orders are open."
        finding = self.rt.cases.finding(self.both, self.case_id, statement, "fact",
                                        "supported", citations=handles,
                                        value_claims=[order_count(handles)])["finding_id"]
        returned = claim_checks.returned_evidence("plx.aggregate", COUNT_CALL,
                                                  count_result(handles))
        bare = case_verification.check_claims(self.rt, self.both, self.case_id)
        self.assertIn((finding, "claim_count_lower_bound"),
                      [(item["item"], item["code"]) for item in bare["failures"]])
        given = case_verification.check_claims(self.rt, self.both, self.case_id,
                                               returned=returned)
        self.assertNotIn(finding, [item["item"] for item in given["failures"]])
        proposal = self.rt.cases.propose(
            self.both, self.case_id, "portal.sales_order.review", {"note": "review"},
            citations=handles, evidence={"statement": statement,
                                         "value_claims": [order_count(handles)]},
            expected_effect={"relation": "public.app_sales_order", "key": {"id": 3},
                             "fields": ["order_number"], "equals": "OR0003"})
        for evidence, failed in ((None, ["claim_count_lower_bound"]), (returned, [])):
            with self.subTest(returned=evidence is not None):
                report = case_verification.verify_proposal(
                    self.rt, self.both, self.case_id, proposal["proposal_id"],
                    returned=evidence)["evidence_claims"]
                self.assertEqual([item["code"] for item in report["failures"]], failed)


class PlaybookEvalEvidenceTests(pe_tests.CustomerPoEvalBase, unittest.TestCase):
    def test_a_transcript_s_tool_results_bind_the_case_counts(self):
        # Item 5: the evaluator passes what the transcript's tools returned to the gate.
        case_id = self.build()
        rows = self.actual("mirror.rows", {"relation": "public.app_sales_order"})["rows"]
        handles = [row["citation"] for row in rows][:3]
        self.actual("plx.case_add_finding", {
            "case_id": case_id, "statement": "Three sales orders are open.", "kind": "fact",
            "status": "supported", "citations": handles,
            "value_claims": [order_count(handles)]})
        call = {"tool": "plx.aggregate", "arguments": COUNT_CALL,
                "result": count_result(handles)}

        def lower_bound(transcript):
            report = pe.evaluate(pe_tests.CPO, self.call, case_id, transcript=transcript)
            return [item["code"] for item in report["claim_checks"]["failures"]
                    if item["code"] == "claim_count_lower_bound"]
        self.assertEqual(lower_bound(list(self.actual.names)), ["claim_count_lower_bound"])
        self.assertEqual(lower_bound(iter(list(self.actual.names) + [call])), [])
        self.assertEqual(lower_bound(list(self.actual.names) + [{**call, "is_error": True}]),
                         ["claim_count_lower_bound"])


if __name__ == "__main__":
    unittest.main()
