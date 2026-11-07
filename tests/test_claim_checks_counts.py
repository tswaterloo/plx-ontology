"""Count and money claims at the claim gate (gap-82b852bc58c9).

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


def split(handle):
    from integrations.captured_text import split_member
    from ontology.mentions import cited_page
    return split_member(cited_page(handle)[0])


def shown_member(handle):
    """The claim gate's row of a member the capture read from its container's pinned
    bytes (``captured_text.member_view`` through ``citation_rows``), as served."""
    _container, path, digest = split(handle)
    return {"citation": handle, "relation": "sharepoint.containerMembers",
            "fields": {"state": "unread"},
            "row": {"path": path, "name": path.rsplit("!/", 1)[-1], "content_sha256": digest,
                    "mime_type": "application/octet-stream", "content_text": None}}


class CountClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = dict(receipt(index) for index in range(3))
        self.handles = list(self.rows)
        self.claim = count_of(self.handles)

    def check(self, text, claims=None, returned=None, outcome=None, rows=None, handles=None):
        return gate(text, handles or self.handles, claims or [self.claim],
                    rows=rows or self.rows, calculations=(), returned=returned, outcome=outcome)

    def test_a_source_read_only_in_part_keeps_its_counts_a_lower_bound(self):
        from operating import ontology_query, ontology_tools
        fresh = [{"source": "portal_api", "stale": True, "label": "old",
                  "completeness": "partial", "relations": []}]
        gaps = ontology_tools._source_freshness_gaps(fresh)
        self.assertIn("source_partly_read", [g["reason"] for g in gaps])
        aged = [g for g in gaps if g["reason"] == "stale_source"]
        self.assertTrue(self.check("3 receipts.", returned=counted(self.handles,
                                                                    gaps=aged))["passed"])
        self.assertEqual(codes(self.check("3 receipts.",
                                          returned=counted(self.handles, gaps=gaps))),
                         ["claim_count_lower_bound"])
        # plx.query's gap cap drops age-only gaps first: the partly-read gap behind 25
        # stale ones is kept
        many = [{"state": "stale", "reason": "stale_source", "subject": f"s{i}"}
                for i in range(25)] + gaps[-1:]
        kept = ontology_query._capped(many)
        self.assertEqual(len(kept), ontology_query.MAX_QUERY_GAPS)
        self.assertEqual(kept[0]["reason"], "source_partly_read")

    def test_members_of_one_container_are_distinct_records(self):
        # A .msg's parts are cited by the file's handle and their path: each part is
        # its own record, so a complete count of them binds exactly and an uncovered one
        # is a lower bound of every part; one part cited under two pins is one record.
        from integrations.captured_text import member_handle
        container = "sharepoint://drive-1/item-1@%22e1%22#sha256=" + "a" * 64
        parts = [member_handle(container, name, digest) for name, digest in
                 (("MsoDataStore", None), ("\x05SummaryInformation", "b" * 64),
                  ("__substg1.0_0037001F", "c" * 64))]
        rows = {h: shown_member(h) for h in parts}
        claim = count_of(parts)
        exact = checked(self.check("3 parts.", [claim], counted(parts), rows=rows,
                                   handles=parts))
        self.assertEqual([(c["status"], c.get("bound")) for c in exact],
                         [("passed", "exact")])
        floor = self.check("At least 3 parts.", [claim], rows=rows, handles=parts)
        self.assertEqual([(c["status"], c.get("bound")) for c in checked(floor)],
                         [("passed", "at_least")])
        self.assertEqual(codes(self.check("3 parts.", [claim], rows=rows, handles=parts)),
                         ["claim_count_lower_bound"])
        twice = member_handle(container.replace("a" * 64, "d" * 64), "MsoDataStore", None)
        rows[twice] = shown_member(twice)
        self.assertEqual(codes(self.check("2 parts.", [count_of([parts[0], twice])],
                                          rows=rows, handles=[parts[0], twice])),
                         ["claim_value_mismatch"])
        # A citation that is not exactly a member's names its container: never two
        # records out of one file.
        bad = container + "&member=x&member=y"
        rows[bad] = {"citation": bad, "fields": {}}
        self.assertEqual(codes(self.check("2 parts.", [count_of([container + "&page=1", bad])],
                                          rows={**rows, container + "&page=1": {
                                              "citation": container + "&page=1",
                                              "fields": {}}},
                                          handles=[container + "&page=1", bad])),
                         ["claim_value_mismatch"])

    def test_one_containers_citations_never_count_as_more_records_than_it_backs(self):
        # Review probes: a part and the same part on a page, a file next to its own part,
        # a nested container's member next to that container, and paths the view never
        # showed exist (a gate-only resolve: no row read from the pinned bytes) each
        # count one record of the file, never a floor of two or more.
        from integrations.captured_text import member_handle
        container = "sharepoint://drive-1/item-1@%22e1%22#sha256=" + "a" * 64
        part = member_handle(container, "quote.pdf", "b" * 64)
        inner = member_handle(container, "inner.zip", "c" * 64)
        nested = member_handle(container, "inner.zip!/spec.json", "e" * 64)
        invented = [member_handle(container, f"made-up-{n}.pdf", None) for n in range(3)]
        rows = {container: {"citation": container, "fields": {}},
                part: shown_member(part), part + "&page=1": shown_member(part + "&page=1"),
                inner: shown_member(inner), nested: shown_member(nested),
                **{h: {"citation": h, "relation": "sharepoint.containerMembers",
                       "row": {"container": container, "path": split(h)[1],
                               "content_sha256": None, "content_text": None}}
                   for h in invented}}
        for cited in ([container, part], [part, container], [part, part + "&page=1"],
                      [inner, nested], [nested, inner], invented[:2], invented):
            with self.subTest(cited=cited):
                result = self.check(f"At least {len(cited)} records.", [count_of(cited)],
                                    rows=rows, handles=cited)
                self.assertEqual(codes(result), ["claim_value_mismatch"])
        # The parts the view shows stay separate records (two shown parts: at least 2).
        shown = self.check("At least 2 records.", [count_of([part, nested])], rows=rows,
                           handles=[part, nested])
        self.assertEqual([(c["status"], c.get("bound")) for c in checked(shown)],
                         [("passed", "at_least")])
        # An invented path counts as its file: with one shown part, still one record.
        self.assertEqual(codes(self.check("At least 2 records.",
                                          [count_of([part, invented[0]])], rows=rows,
                                          handles=[part, invented[0]])),
                         ["claim_value_mismatch"])

    def test_a_count_known_at_a_past_moment_binds_no_present_exact_count(self):
        # Item 1: members first captured after a past as_known_at are not in its count.
        past = counted(self.handles, {"as_known_at": "2026-01-01T00:00:00Z"})
        self.assertEqual(codes(self.check("3 receipts.", returned=past)),
                         ["claim_count_lower_bound"])
        self.assertTrue(self.check("At least 3 receipts.", returned=past)["passed"])
        # the refusal names how to make the count exact before settling for "at least"
        detail = self.check("3 receipts.", returned=past)["failures"][0]["detail"]
        self.assertIn("plx.aggregate", detail)
        self.assertIn("at least 3", detail)
        now = self.check("3 receipts.", returned=counted(self.handles))
        self.assertTrue(now["passed"], now["failures"])
        self.assertEqual(checked(now)[0]["bound"], "exact")

    def test_a_count_of_a_completely_listed_set_is_exact(self):
        # 2026-11-02 run: plx.around listed both open orders of a vendor (complete, no
        # gap) and plx.aggregate counted its set (no property: no value rows cited), yet
        # "2 open purchase orders" was refused as a lower bound.
        def listing(handles, **over):
            done = {"complete": True, "truncated": False, "next_cursor": None,
                    "returned": len(handles), "total": len(handles)}
            done.update(over.pop("completeness", {}))
            return {"set": "set:s1", "completeness": done, "gaps": [],
                    "withheld": {"objects": None, "properties": None,
                                 "reason": "marking_and_audience"},
                    "objects": [{"object_id": f"o{n}", "display": {"origin": {
                        "citation": h, "field_path": "row.id"}, "untrusted_text": "x"}}
                        for n, h in enumerate(handles)], **over}

        bare = {**count_result(self.handles), "groups": [{
            "key": {}, "value": "3", "inputs": 0, "markings": []}]}
        members = {"set": "set:s1", "measure": {"op": "count"}}

        def returned(around, aggregate=bare, call=members):
            into = claim_checks.returned_evidence("plx.around", {}, around)
            return claim_checks.returned_evidence("plx.aggregate", call, aggregate, into)

        exact = self.check("3 receipts.", returned=returned(listing(self.handles)))
        self.assertTrue(exact["passed"], exact["failures"])
        self.assertEqual(checked(exact)[0]["bound"], "exact")
        # Anything short of a whole listing of that set keeps the count a lower bound.
        gap = {"state": "unknown", "reason": "marking_withheld", "subject": "x"}
        for name, evidence in (
                ("paged", returned(listing(self.handles, completeness={
                    "complete": False, "truncated": True, "next_cursor": "c1"}))),
                ("gap", returned(listing(self.handles, gaps=[gap]))),
                ("one missing", returned(listing(self.handles[:2],
                                                 completeness={"total": 3}))),
                # an aggregate of the same set contradicting the listing: fail closed
                ("value differs", returned(listing(self.handles), aggregate={
                    **bare, "groups": [{**bare["groups"][0], "value": "4"}]}))):
            with self.subTest(name):
                self.assertEqual(codes(self.check("3 receipts.", returned=evidence)),
                                 ["claim_count_lower_bound"])
        # A whole listing is itself the exact count of its members (listing_count): no
        # aggregate is needed, so an aggregate of another set, a grouped or partial one
        # leaves it exact.
        for name, evidence in (
                ("listing alone", claim_checks.returned_evidence(
                    "plx.around", {}, listing(self.handles))),
                ("other set", returned(listing(self.handles, set="set:s2"))),
                ("grouped", returned(listing(self.handles),
                                     call={**members, "group_by": ["status"]})),
                ("aggregate partial", returned(listing(self.handles),
                                               aggregate={**bare, "partial": True}))):
            with self.subTest(name):
                got = self.check("3 receipts.", returned=evidence)
                self.assertTrue(got["passed"], got["failures"])
                self.assertEqual(checked(got)[0]["bound"], "exact")
        # Without a whole listing, an aggregate of another set or a grouped or partial
        # one still keeps the count a lower bound.
        paged = listing(self.handles, completeness={
            "complete": False, "truncated": True, "next_cursor": "c1"})
        for name, evidence in (
                ("other set", returned(dict(paged, set="set:s2"))),
                ("grouped", returned(paged, call={**members, "group_by": ["status"]})),
                ("aggregate partial", returned(paged, aggregate={**bare, "partial": True}))):
            with self.subTest("paged, " + name):
                self.assertEqual(codes(self.check("3 receipts.", returned=evidence)),
                                 ["claim_count_lower_bound"])

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


class LeadTierCountTests(unittest.TestCase):
    """A count backed in whole or in part by lead-tier links (11-07 dev 0014: emails of a
    second, unrelated PO reached through weak mention links swelled a count of 4 to "at
    least 8") passes the answer gate only stated as a lead; a count of the records solid
    links reach passes as it is. Evidence label ``isolated-fixture``: synthetic receipt
    rows and tool answers in the contract shapes plx.around and plx.aggregate return."""

    def setUp(self) -> None:
        self.rows = dict(receipt(index) for index in range(3))
        self.solid, self.leads = list(self.rows)[:2], list(self.rows)[2:]
        listed = {"set": "set:solid1", "hops": [], "edges": [],
                  "objects": [{"object_id": f"Receipt:x:{n}", "object_type": "Receipt",
                               "lineage": "portal_native",
                               "display": {"origin": {"citation": h}}}
                              for n, h in enumerate(self.solid)],
                  "gaps": [{"state": "unknown", "reason": "possibly_related",
                            "subject": "set:lead1", "detail": "1 more record"}],
                  "completeness": {"complete": True, "truncated": False,
                                   "returned": 2, "total": 2, "next_cursor": None},
                  "meta": {}}
        self.returned = claim_checks.returned_evidence(
            "plx.around", {"from": "Order:x:1", "tier_floor": "lead",
                           "path": [{"link": "receipts"}]}, listed)
        claim_checks.returned_evidence(
            "plx.aggregate", {"set": "set:lead1", "measure": {"op": "count"}},
            count_result(self.leads, gaps=[{"state": "unknown", "reason": "lead_basis",
                                            "subject": "set:lead1", "detail": "lead"}]),
            self.returned)

    def gate(self, handles, lead=False):
        from unittest import mock
        from operating import answer_gate
        claim = {"id": "n", **count_of(handles), **({"lead": True} if lead else {})}
        rows = lambda runtime, actor, **_kw: (lambda handle: (
            {"handle": handle, **self.rows[handle]} if handle in self.rows else None))
        with mock.patch.object(answer_gate, "citation_rows", lambda runtime, **_kw: None), \
                mock.patch.object(answer_gate, "gate_rows", rows):
            return answer_gate.gate_answer(
                {"claims": [claim], "text": "{n} receipts."},
                mock.Mock(case_runtime=None, actor=None), seen=set(self.rows),
                returned=self.returned, may_repair=False)

    @staticmethod
    def lead_refused(out):
        return "claim_lead_unmarked" in [f["code"] for u in out["unsupported"]
                                         for f in u["failures"]]

    def test_the_solid_listing_is_exact_and_the_lead_records_stay_apart(self):
        # the possibly_related records are no member left out of the listing's count
        self.assertIn({"value": "2", "records": self.solid, "distinct": True,
                       "complete": True, "listing": "set:solid1"}, self.returned["counts"])
        self.assertEqual(self.returned["lead_records"],
                         {**dict.fromkeys(self.solid, False), self.leads[0]: True})
        solid = self.gate(self.solid)
        self.assertFalse(self.lead_refused(solid), solid)
        self.assertEqual(solid["unsupported"], [])

    def test_a_count_partly_on_lead_links_passes_only_said_as_a_lead(self):
        for handles in (self.solid + self.leads, self.leads):
            with self.subTest(handles=len(handles)):
                self.assertTrue(self.lead_refused(self.gate(handles)))
                self.assertFalse(self.lead_refused(self.gate(handles, lead=True)))

    def test_an_around_answer_alone_marks_its_possibly_related_records_as_leads(self):
        # 51706fd32c review: plx.around at tier_floor lead lists the lead-only records
        # apart (possibly_related.objects, each cited); with no aggregate over that set a
        # count citing them still passes only stated as a lead.
        listed = {"set": "set:solid2", "hops": [], "edges": [],
                  "objects": [{"object_id": f"Receipt:x:{n}", "object_type": "Receipt",
                               "lineage": "portal_native",
                               "display": {"origin": {"citation": h}}}
                              for n, h in enumerate(self.solid)],
                  "possibly_related": {"set": "set:lead2", "count": 1, "edges": [],
                                       "objects": [{"object_id": "Receipt:x:2",
                                                    "object_type": "Receipt",
                                                    "display": {"origin": {
                                                        "citation": self.leads[0]}}}]},
                  "gaps": [{"state": "unknown", "reason": "possibly_related",
                            "subject": "set:lead2", "detail": "1 more record"}],
                  "completeness": {"complete": True, "truncated": False,
                                   "returned": 2, "total": 2, "next_cursor": None},
                  "meta": {}}
        self.returned = claim_checks.returned_evidence(
            "plx.around", {"from": "Order:x:1", "tier_floor": "lead",
                           "path": [{"link": "receipts"}]}, listed)
        self.assertEqual(self.returned["lead_records"],
                         {**dict.fromkeys(self.solid, False), self.leads[0]: True})
        for handles in (self.solid + self.leads, self.leads):
            with self.subTest(handles=len(handles)):
                self.assertTrue(self.lead_refused(self.gate(handles)))
                self.assertFalse(self.lead_refused(self.gate(handles, lead=True)))
        self.assertFalse(self.lead_refused(self.gate(self.solid)))

    def test_a_record_a_solid_result_also_reached_is_no_lead(self):
        claim_checks.returned_evidence(
            "plx.aggregate", {"set": "set:s9", "measure": {"op": "count"}},
            count_result(self.leads), self.returned)
        self.assertFalse(self.lead_refused(self.gate(self.solid + self.leads)))


class CountAskedWithRowsTests(unittest.TestCase):
    """11-07 regrade root cause 1 (21 answers "count: at least N" with the right N): a
    plx.query asking a count and rows naming the records came back with the rows alone,
    so no complete count entry existed and the gate bound the right count as a lower
    bound. A count asked beside rows is measured, and a complete one binds exactly; a
    partial or withheld one stays a lower bound. Evidence label ``isolated-fixture``:
    plx.query's own code over a scripted host in the contract shapes."""

    def query(self, handles, **aggregate):
        from operating import ontology_query as oq

        def call(name, arguments, actor):
            if name == "plx.around":
                return {"set": "set:reach0001", "hops": [{
                    "link_type": "Shipment.shipped_to", "direction": "forward",
                    "count": len(handles), "tiers": {"verified": len(handles)}}],
                    "gaps": [{"state": "stale", "reason": "stale_source",
                              "subject": "portal_api"}]}
            if name == "plx.aggregate":
                return {"groups": [{"key": {}, "value": str(len(handles)), "inputs": 0,
                                    "markings": [], "members_cited": list(handles)}],
                        "partial": False, "gaps": [], "meta": {},
                        "completeness": {"complete": True, "truncated": False,
                                         "returned": 1, "total": 1}, **aggregate}
            raise AssertionError(name)

        def rows(arguments, actor):
            return {"count": len(handles), "rows": [
                {"object_id": f"Receipt:x:{n}", "values": {"id": {
                    "citation": {"l1_handle": h, "field_path": "row.id"}}}}
                for n, h in enumerate(handles)], "gaps": []}

        tool = oq.QueryTool(oq.TableBackend(call, lambda actor, ids: "set:new00001",
                                            None, rows=rows))
        args = {"from": {"object_id": "SalesOrder:x:1"},
                "traverse": [{"link_type": "Shipment.shipped_to", "direction": "forward"}],
                "measures": [{"op": "count"}], "rows": {"properties": ["id"], "limit": 20}}
        out = tool.call(args, "actor")
        self.assertNotIn("error", out)
        return args, out

    def test_a_complete_count_asked_with_rows_is_stated_exactly(self):
        for size in (1, 10, 60):
            with self.subTest(size=size):
                rows = dict(receipt(index) for index in range(size))
                handles = list(rows)
                args, out = self.query(handles)
                self.assertEqual(out["rows"]["count"], size)
                self.assertEqual(out["results"][0]["groups"][0]["value"], str(size))
                returned = claim_checks.returned_evidence("plx.query", args, out)
                bound = gate(f"{size} receipts.", handles, [count_of(handles)], rows=rows,
                             calculations=(), returned=returned)
                self.assertTrue(bound["passed"], bound["failures"])
                self.assertEqual(checked(bound)[0]["bound"], "exact")

    def test_a_partial_or_withheld_count_asked_with_rows_stays_a_lower_bound(self):
        rows = dict(receipt(index) for index in range(3))
        handles = list(rows)
        for aggregate in ({"partial": True},
                          {"withheld": {"objects": 1}},
                          {"gaps": [{"state": "inaccessible", "reason": "withheld",
                                     "subject": "x"}]}):
            with self.subTest(aggregate=aggregate):
                args, out = self.query(handles, **aggregate)
                returned = claim_checks.returned_evidence("plx.query", args, out)
                self.assertEqual(codes(gate("3 receipts.", handles, [count_of(handles)],
                                            rows=rows, calculations=(),
                                            returned=returned)),
                                 ["claim_count_lower_bound"])
