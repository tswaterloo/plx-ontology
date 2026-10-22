"""Read-back verification of case proposals (injected transport only).

Evidence label: ``injected-transport``. The real ``WorkbenchReadApiClient`` reads
the in-process ``FakeWorkbench``; a person "carrying out" a proposal is the test
editing the fake's rows, exactly what a later replica read would show. Socket
connections are refused for the whole module.
"""

from __future__ import annotations

import json
import unittest

from ontology.core import Actor, ValidationError
from operating import source_reads
from operating import case_verification as cv
from operating.cases import CaseClosed, effect_outcome, effect_values, parse_expected_effect
from operating.mcp_server import CASE_RUNTIME_TOOLS, TOOLS
from operating.tests.test_customer_po_coverage import (
    GRANTED, OPS, OPS_FINANCE, CoverageTestCase, ProposalTestCase)


def _set_row(fake, relation, ident_column, ident, **values):
    for row in fake.tables[relation]:
        if row[ident_column] == ident:
            row.update(values)
            return
    raise AssertionError(f"no {relation} row {ident}")


class MatchRuleTests(unittest.TestCase):
    def test_text_is_trimmed_and_exact_integers_compare_numerically(self):
        self.assertTrue(cv.matches(" PO-7788 ", "PO-7788"))
        self.assertFalse(cv.matches("po-7788", "PO-7788"))
        self.assertTrue(cv.matches(4500, "4500"))
        self.assertFalse(cv.matches(4500.0, "4500"))
        for cell in (5, "5", " 5 ", 5.0, "5.00"):
            self.assertTrue(cv.matches(cell, 5), cell)
        for cell in (True, None, "five", float("nan"), 5.5, [5], {"v": 5}):
            self.assertFalse(cv.matches(cell, 5), cell)
        self.assertFalse(cv.matches(True, "True"))

    def test_field_status_classifies_once_with_the_capability_guards(self):
        """Wire-host decision consequence 1: field_status reads fields through
        capability_guards.read_field/value_state (number_state for an integer), so a
        float, boolean or list never reads as empty or as a differing text value, and a
        float quantity is the number it says."""
        from operating import goal2_preconditions as gp
        row = {"po": None, "qty": 1509.0, "flag": False, "items": [1], "cf": '{"k": 2.5}',
               "bad": "{x", "text": " PO-1 ", "zero": 0.0}
        status = {path: cv.field_status(row, path, "")["status"] for path in row}
        self.assertEqual(status, {"po": "empty", "qty": "not_inspectable",
                                  "flag": "not_inspectable", "items": "not_inspectable",
                                  "cf": "differs", "bad": "differs",
                                  "text": "differs", "zero": "not_inspectable"})
        self.assertEqual(cv.field_status(row, "cf.k", "")["status"], "not_inspectable")
        self.assertEqual(cv.field_status(row, "bad.k", "")["reason"], "json_unparseable")
        self.assertEqual(cv.field_status(row, "cf.other", "")["status"], "field_absent")
        self.assertEqual(cv.field_status(row, "qty", 1509)["status"], "equals_expected")
        self.assertEqual(cv.field_status(row, "zero", 1509)["status"], "differs")
        self.assertEqual(cv.field_status(row, "qty", "1509")["reason"], "value_not_text")
        self.assertEqual(cv.field_status(row, "flag", 0)["reason"], "value_not_number")
        # An integer expected against text: text that parses as a plain number compares
        # numerically (equal or differs); any other text cannot be compared.
        self.assertEqual(cv.field_status({"q": "1510"}, "q", 1509)["status"], "differs")
        self.assertEqual(cv.field_status({"q": "1509.0"}, "q", 1509)["status"],
                         "equals_expected")
        for text in ("abc", "1.509e3", "1_509"):
            self.assertEqual(cv.field_status({"q": text}, "q", 1509)["status"],
                             "not_inspectable", text)
        self.assertEqual(cv.field_status(row, "text", "PO-1")["status"], "equals_expected")
        evidence = [{"handle": "api://r", "cited_digest": "d", "current_digest": "d",
                     "content": {"row": row}}]
        checks = [{"check": "field_empty", "citation": "api://r", "field": "qty"}]
        self.assertEqual(gp.evaluate(checks, evidence)["reason"],
                         "precondition_field_not_inspectable")
        checks = [{"check": "field_equals", "citation": "api://r", "field": "qty",
                   "value": 1509}]
        self.assertTrue(gp.evaluate(checks, evidence)["holds"])

    def test_expected_effect_shape_is_strict(self):
        good = {"relation": "public.app_sales_order", "key": {"id": 3},
                "fields": ["po_reference"], "equals": " PO-1 "}
        self.assertEqual(parse_expected_effect(good)["values"], {"po_reference": "PO-1"})
        many = {"relation": "public.assembly_order", "key": {"id": 5},
                "values": {"qtytotal": 1509, "finalqty": " 1503 "}}
        self.assertEqual(parse_expected_effect(many),
                         {"relation": "public.assembly_order", "key": {"id": 5},
                          "fields": ["qtytotal", "finalqty"],
                          "values": {"qtytotal": 1509, "finalqty": "1503"}})
        bad = [None, [], {**good, "extra": 1}, {k: v for k, v in good.items() if k != "key"},
               {**good, "relation": "no_schema"}, {**good, "relation": "Public.X"},
               {**good, "key": {}}, {**good, "key": [3]}, {**good, "fields": []},
               {**good, "fields": "po_reference"}, {**good, "fields": ["a.b.c"]},
               # One value per field: a multi-field write names each field's own value.
               {**good, "fields": ["po_reference", "custom_fields.custPoCode"]},
               {**good, "fields": ["po_reference", "po_reference"]},
               {**good, "fields": ["f1", "f2", "f3", "f4", "f5", "f6"]},
               {**good, "equals": True}, {**good, "equals": 1.5}, {**good, "equals": "  "},
               {**good, "equals": None}, {**good, "equals": 10 ** 19},
               {**many, "values": {}}, {**many, "values": ["qtytotal"]},
               {**many, "values": {"qtytotal": 1.5}}, {**many, "values": {"a.b.c": 1}},
               {**many, "values": {f"f{i}": i for i in range(1, 7)}},
               {**many, "fields": ["qtytotal"]}, {**many, "equals": 1}]
        for value in bad:
            with self.assertRaises(ValidationError, msg=value):
                parse_expected_effect(value)


class CustomerPoVerificationTests(ProposalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.receipt = self.propose()
        self.proposal_id = self.receipt["proposal"]["proposal_id"]
        self.order = self.orders[3]["citation"]

    def verify(self, actor=None, **kwargs):
        return cv.verify_proposal(self.rt, actor or self.both, self.case_id, self.proposal_id,
                                  **kwargs)

    def person_sets_po(self, value, **extra):
        """What a person doing the work in Portal leaves behind for the replica."""
        _set_row(self.fake, "public.app_sales_order", "id", 3, po_reference=value,
                 updated_at="2026-09-29T11:30:00", **extra)
        self.clock.advance(3600)

    def test_a_claim_on_a_superseded_mirror_row_is_changed_since_and_the_digest_stays(self):
        """A value claimed on an api:// row the source then changed (mirror refreshed):
        the case claim check and a proposal's evidence claims report claim_changed_since,
        never passed, while the case's recorded digest and its finding are unchanged."""
        def claim(citation, value="OR0003"):
            return self.rt.cases.finding(
                self.both, self.case_id, f"The order number is {value}.", "fact",
                "supported", citations=[citation], value_claims=[
                    {"value": value, "citation": citation, "field": "row.order_number",
                     "role": "identifier"}])["finding_id"]

        def refresh():
            self.clock.advance(3600)
            self.rt.mirror.refresh(self.both, "public.app_sales_order")

        finding = claim(self.order)
        refresh()  # nothing changed upstream: the cited revision is still current
        self.assertTrue(cv.check_claims(self.rt, self.both, self.case_id)["passed"])
        recorded = self.rt.cases.get(self.both, self.case_id)["findings"]
        _set_row(self.fake, "public.app_sales_order", "id", 3, order_number="OR0003-B",
                 updated_at="2026-09-29T11:30:00")
        refresh()
        current = self.rt.mirror.resolve_citation(self.order, self.both)["current_citation"]
        self.assertNotEqual(current, self.order)
        # The case record does not move: same finding bucket, same recorded digest.
        self.assertEqual(self.rt.cases.get(self.both, self.case_id)["findings"], recorded)
        report = cv.check_claims(self.rt, self.both, self.case_id)
        self.assertFalse(report["passed"])
        self.assertEqual([(item["item"], item["code"]) for item in report["failures"]],
                         [(finding, "claim_changed_since")])
        proposal = self.rt.cases.propose(
            self.both, self.case_id, "portal.sales_order.review", {"note": "review"},
            citations=[self.order], evidence={"statement": "The cited order is OR0003.",
                                              "value_claims": [
                {"value": "OR0003", "citation": self.order, "field": "row.order_number",
                 "role": "identifier"}]},
            expected_effect={"relation": "public.app_sales_order", "key": {"id": 3},
                             "fields": ["order_number"], "equals": "OR0003-B"})
        evidence = cv.verify_proposal(self.rt, self.both, self.case_id,
                                      proposal["proposal_id"])["evidence_claims"]
        self.assertEqual([item["code"] for item in evidence["failures"]],
                         ["claim_changed_since"])
        # A reader of the order but not of the Sage header: the same changed-since
        # verdicts, the proposal's Sage-citing evidence withheld (never checked).
        both = cv.check_claims(self.rt, self.both, self.case_id)
        ops = cv.check_claims(self.rt, self.ops, self.case_id)
        self.assertEqual((ops["passed"], ops["failures"]), (False, both["failures"]))
        self.assertEqual(len(both["failures"]), 2)
        self.assertNotIn(finding, ops["unchecked_items"])
        self.assertTrue(ops["unchecked_items"])
        # Citing the current revision with its current value checks as usual.
        fresh = claim(current, "OR0003-B")
        report = cv.check_claims(self.rt, self.both, self.case_id)
        self.assertNotIn(fresh, [item["item"] for item in report["failures"]])
        self.assertIn(fresh, [entry["item"] for entry in report["items"]])

    def test_customer_po_proposal_carries_its_expected_effect_on_the_cited_order(self):
        [proposal] = self.rt.cases.get(self.both, self.case_id)["proposals"]
        self.assertEqual(proposal["expected_effect"], {
            "relation": "public.app_sales_order", "key": {"id": 3},
            "fields": ["po_reference"], "values": {"po_reference": "PO-7788"},
            "target_citation": self.order})

    def test_not_observed_before_and_observed_after_the_row_changes(self):
        before = self.verify()
        self.assertEqual((before["outcome"], before["reason"]), ("effect_not_observed", None))
        self.assertEqual(before["fields"], {"po_reference": {"status": "empty"}})
        self.assertIs(before["held_when_proposed"], False)
        self.assertEqual(before["observed_citation"], self.order)
        self.assertEqual((before["dispatched"], before["approved"],
                          before["business_system_writes"], before["attribution"]),
                         (False, False, 0, "not_established"))

        self.person_sets_po("PO-7788")
        after = self.verify(references=["MC TASK-9999", "MC TASK-9999"])
        self.assertEqual(after["outcome"], "effect_observed")
        self.assertEqual(after["fields"]["po_reference"], {"status": "equals_expected"})
        self.assertNotEqual(after["observed_citation"], self.order)
        resolved = self.rt.mirror.resolve_citation(after["observed_citation"], self.both)
        self.assertEqual((resolved["status"], resolved["row"]["po_reference"]),
                         ("current", "PO-7788"))
        self.assertIs(after["held_when_proposed"], False)

        view = self.rt.cases.get(self.both, self.case_id)
        first, second = view["verifications"]
        self.assertEqual((first["id"], first["proposal_id"], first["outcome"]),
                         ("V1", self.proposal_id, "effect_not_observed"))
        self.assertEqual((second["id"], second["outcome"]), ("V2", "effect_observed"))
        self.assertEqual(second["citations_status"], "resolved")
        handles = {cite["handle"] for cite in second["citations"]}
        # The row read back plus every citation of the proposal.
        self.assertEqual(handles, {after["observed_citation"], self.order, self.sage["3"]})
        detail = second["detail"]
        self.assertEqual((detail["references"], detail["references_verified"],
                          detail["dispatched_by_workbench"]),
                         (["MC TASK-9999"], False, False))
        self.assertEqual([item["kind"] for item in view["timeline"]][-2:],
                         ["verification.recorded", "verification.recorded"])
        self.assertTrue(view["integrity"]["chain_ok"])

    def test_a_differing_value_is_reported_but_never_echoed(self):
        self.person_sets_po("Jane Buyer")
        result = self.verify()
        self.assertEqual(result["outcome"], "effect_not_observed")
        self.assertEqual(result["fields"]["po_reference"],
                         {"status": "differs", "value_withheld": True})
        self.assertNotIn("Jane Buyer", json.dumps(result))
        self.assertNotIn("Jane Buyer",
                         json.dumps(self.rt.cases.get(self.both, self.case_id)["verifications"]))

    def test_the_value_in_the_json_field_counts_too(self):
        self.proposal_id = self.propose(values={"custom_fields.custPoCode": "PO-7788"})[
            "proposal"]["proposal_id"]
        _set_row(self.fake, "public.app_sales_order", "id", 3,
                 custom_fields={"source": "sage.oe_order_header", "custPoCode": "PO-7788"},
                 updated_at="2026-09-29T11:30:00")
        self.clock.advance(3600)
        result = self.verify()
        self.assertEqual(result["outcome"], "effect_observed")
        self.assertEqual(result["fields"]["custom_fields.custPoCode"],
                         {"status": "equals_expected"})

    def test_an_unreadable_field_makes_the_outcome_unknown(self):
        self.proposal_id = self.propose(values={
            "po_reference": "PO-7788", "custom_fields.custPoCode": "PO-7788"})[
            "proposal"]["proposal_id"]
        self.person_sets_po("PO-7788", custom_fields="not json")
        result = self.verify()
        self.assertEqual((result["outcome"], result["reason"]),
                         ("unknown", "field_not_inspectable"))
        self.assertEqual(result["fields"]["custom_fields.custPoCode"],
                         {"status": "not_inspectable", "reason": "json_unparseable"})

    def test_a_multi_field_effect_is_observed_only_when_every_field_holds_its_value(self):
        # One value per field: the read-back verifies every field, never "any one".
        self.proposal_id = self.propose(values={
            "po_reference": "PO-7788", "custom_fields.custPoCode": "PO-7788"})[
            "proposal"]["proposal_id"]
        self.person_sets_po("PO-7788")
        partial = self.verify()
        self.assertEqual((partial["outcome"], partial["fields"]), ("effect_not_observed", {
            "po_reference": {"status": "equals_expected"},
            "custom_fields.custPoCode": {"status": "field_absent"}}))
        self.person_sets_po("PO-7788", custom_fields={"custPoCode": "PO-7788"})
        full = self.verify()
        self.assertEqual(full["outcome"], "effect_observed")
        self.assertEqual(full["fields"], {"po_reference": {"status": "equals_expected"},
                                          "custom_fields.custPoCode":
                                              {"status": "equals_expected"}})

    def test_a_legacy_multi_field_record_is_unknown_when_only_some_fields_hold(self):
        effect = {"relation": "public.app_sales_order", "key": {"id": 3},
                  "fields": ["po_reference", "custom_fields.custPoCode"], "equals": "X"}
        mixed = {"po_reference": "equals_expected", "custom_fields.custPoCode": "differs"}
        # The record does not say which field the write sets (the Portal owner writes
        # po_reference only): some-but-not-all is unknown, never "not observed", even for
        # the customer-PO entry whose catalog effect writes one value to every field.
        self.assertEqual(effect_outcome(effect, mixed),
                         ("unknown", "legacy_effect_one_value_several_fields"))
        self.assertEqual(effect_outcome(effect, {"po_reference": "differs",
                                                 "custom_fields.custPoCode": "empty"}),
                         ("effect_not_observed", None))
        self.assertEqual(effect_outcome(effect, dict.fromkeys(mixed, "equals_expected")),
                         ("effect_observed", None))
        # A per-field record is never ambiguous.
        per_field = {**effect, "values": {"po_reference": "X",
                                          "custom_fields.custPoCode": "X"}}
        del per_field["equals"]
        self.assertEqual(effect_outcome(per_field, mixed), ("effect_not_observed", None))
        self.assertEqual(effect_values(effect),
                         {"po_reference": "X", "custom_fields.custPoCode": "X"})

    def test_a_failed_refresh_is_unknown_never_not_observed(self):
        self.clock.advance(3600)
        self.fake.fail_row_requests = 1000
        result = self.verify()
        self.assertEqual((result["outcome"], result["reason"], result["fields"]),
                         ("unknown", "relation_refresh_failed", {}))
        self.assertIsNone(result["observed_citation"])
        [recorded] = self.rt.cases.get(self.both, self.case_id)["verifications"]
        self.assertEqual(recorded["outcome"], "unknown")

    def test_a_row_missing_from_a_full_traversal_is_unknown(self):
        self.fake.tables["public.app_sales_order"] = [
            row for row in self.fake.tables["public.app_sales_order"] if row["id"] != 3]
        self.rt.mirror.refresh(self.both, "public.app_sales_order", full=True)
        result = self.verify()
        self.assertEqual((result["outcome"], result["reason"]),
                         ("unknown", "row_not_returned_by_latest_full_traversal"))

    def test_value_already_held_when_proposed_is_said_so(self):
        # A generic proposal whose effect already holds in the cited row.
        row = self.orders[1]["citation"]
        receipt = self.rt.cases.propose(
            self.both, self.case_id, "portal.sales_order.noop", {"note": "already set"},
            citations=[row], expected_effect={"relation": "public.app_sales_order",
                                              "key": {"id": 1}, "fields": ["po_reference"],
                                              "equals": "PO-5501"})
        result = cv.verify_proposal(self.rt, self.both, self.case_id, receipt["proposal_id"])
        self.assertEqual(result["outcome"], "effect_observed")
        self.assertIs(result["held_when_proposed"], True)

    def test_reader_permissions_gate_verifications_like_proposals(self):
        self.person_sets_po("PO-7788")
        self.verify()
        # Operations cannot resolve the Sage evidence row the proposal cites.
        ops = Actor("plx", OPS)
        [withheld] = self.rt.cases.get(ops, self.case_id)["verifications"]
        self.assertEqual(withheld["content"], "withheld_citation_unavailable")
        self.assertIsNone(withheld["outcome"])
        self.assertIsNone(withheld["detail"])
        self.assertNotIn("PO-7788", json.dumps(withheld))
        self.assertTrue(all(set(cite) == {"index", "status"} for cite in withheld["citations"]))
        with self.assertRaises(cv.VerificationError) as caught:
            self.verify(actor=ops)
        self.assertEqual(caught.exception.code, "proposal_withheld")
        # Revoking the sales-order grant hides it from the full member too.
        self.audience_path.write_text(json.dumps({
            "schema": "plx-source-mirror-audience/v1", "tenant": "plx",
            "audiences": {"sage.*": ["staff:finance"]}}), encoding="utf-8")
        [hidden] = self.rt.cases.get(self.both, self.case_id)["verifications"]
        self.assertEqual(hidden["content"], "withheld_citation_unavailable")

    def test_refusals_record_nothing(self):
        codes = []
        for kwargs in ({"references": "TASK-1"}, {"references": ["x" * 201]},
                       {"references": [f"T{i}" for i in range(11)]}, {"references": [""]}):
            with self.assertRaises(cv.VerificationError) as caught:
                self.verify(**kwargs)
            codes.append(caught.exception.code)
        self.assertEqual(codes, ["invalid_references", "invalid_references",
                                 "too_many_references", "invalid_references"])
        with self.assertRaises(cv.VerificationError) as caught:
            cv.verify_proposal(self.rt, self.both, self.case_id, "P99")
        self.assertEqual(caught.exception.code, "proposal_not_found")
        plain = self.rt.cases.propose(self.both, self.case_id, "x.y", {"a": 1})
        with self.assertRaises(cv.VerificationError) as caught:
            cv.verify_proposal(self.rt, self.both, self.case_id, plain["proposal_id"])
        self.assertEqual(caught.exception.code, "proposal_has_no_expected_effect")
        with self.assertRaises(ValidationError):
            self.rt.cases.verification(self.both, self.case_id, plain["proposal_id"],
                                       "effect_observed", None, {}, [self.order])
        with self.assertRaises(ValidationError):
            self.rt.cases.verification(self.both, self.case_id, self.proposal_id,
                                       "failed", None, {}, [self.order])
        with self.assertRaises(ValidationError):
            self.rt.cases.verification(self.both, self.case_id, self.proposal_id,
                                       "effect_observed", None, {}, [])
        self.assertEqual(self.rt.cases.get(self.both, self.case_id)["verifications"], [])
        self.rt.cases.close(self.both, self.case_id, "done")
        with self.assertRaises(cv.VerificationError) as caught:
            self.verify()
        self.assertEqual(caught.exception.code, "case_closed")
        with self.assertRaises(CaseClosed):
            self.rt.cases.verification(self.both, self.case_id, self.proposal_id,
                                       "effect_observed", None, {}, [self.order])

    def test_invalid_max_staleness_is_refused_before_any_read(self):
        for value in (-1, float("nan"), float("inf"), True, "5", 4e8):
            with self.assertRaises(cv.VerificationError, msg=value) as caught:
                self.verify(max_staleness=value)
            self.assertEqual(caught.exception.code, "invalid_max_staleness")
        self.assertEqual(self.rt.cases.get(self.both, self.case_id)["verifications"], [])
        self.assertEqual(self.verify(max_staleness=0)["outcome"], "effect_not_observed")

    def test_a_row_changed_twice_is_read_at_its_latest_content(self):
        self.person_sets_po("PO-1")
        self.assertEqual(self.verify()["outcome"], "effect_not_observed")
        _set_row(self.fake, "public.app_sales_order", "id", 3, po_reference="PO-7788",
                 updated_at="2026-09-29T13:30:00")
        self.clock.advance(3600)
        result = self.verify()
        self.assertEqual(result["outcome"], "effect_observed")
        stale = self.rt.mirror.resolve_citation(self.order, self.both)
        self.assertEqual(stale["status"], "superseded")
        self.assertEqual(result["observed_citation"], stale["current_citation"])

    def test_idempotent_repeat_never_swaps_the_expected_effect(self):
        cases = self.rt.cases
        effect = {"relation": "public.app_sales_order", "key": {"id": 5},
                  "fields": ["po_reference"], "equals": "PO-1"}
        row = self.orders[5]["citation"]
        first = cases.propose(self.both, self.case_id, "x.y", {}, citations=[row],
                              idempotency_key="k1", expected_effect=effect)
        self.assertEqual((first["duplicate"], first["expected_effect_recorded"]),
                         (False, True))
        again = cases.propose(self.both, self.case_id, "x.y", {}, citations=[row],
                              idempotency_key="k1", expected_effect=effect)
        self.assertEqual((again["duplicate"], again["proposal_id"],
                          again["expected_effect_recorded"]),
                         (True, first["proposal_id"], True))
        with self.assertRaises(ValidationError) as caught:
            cases.propose(self.both, self.case_id, "x.y", {}, citations=[row],
                          idempotency_key="k1", expected_effect={**effect, "equals": "PO-2"})
        self.assertEqual(str(caught.exception),
                         "idempotency_key_reused_with_other_expected_effect")
        # A proposal recorded without an effect stays without one on a repeat.
        bare = cases.propose(self.both, self.case_id, "x.y", {}, citations=[row],
                             idempotency_key="k2")
        repeat = cases.propose(self.both, self.case_id, "x.y", {}, citations=[row],
                               idempotency_key="k2", expected_effect=effect)
        self.assertEqual((repeat["duplicate"], repeat["proposal_id"],
                          repeat["expected_effect_recorded"]),
                         (True, bare["proposal_id"], False))
        self.assertEqual(len(cases.get(self.both, self.case_id)["proposals"]), 3)

    def test_expected_effect_must_name_a_cited_row(self):
        cases = self.rt.cases
        effect = {"relation": "public.app_sales_order", "key": {"id": 5},
                  "fields": ["po_reference"], "equals": "PO-1"}
        for citations in ([], [self.order], [self.sage["3"]]):
            with self.assertRaises(ValidationError) as caught:
                cases.propose(self.both, self.case_id, "x.y", {}, citations=citations,
                              expected_effect=effect)
            self.assertEqual(str(caught.exception), "expected_effect_target_not_cited")
        # Same key, other relation: still not the cited row.
        with self.assertRaises(ValidationError):
            cases.propose(self.both, self.case_id, "x.y", {}, citations=[self.sage["3"]],
                          expected_effect={**effect, "relation": "sage.oe_order_header",
                                           "key": {"id": 3}})
        self.assertEqual(len(cases.get(self.both, self.case_id)["proposals"]), 1)


class NonUniqueKeyTests(CoverageTestCase):
    def test_a_key_that_stops_being_unique_is_unknown(self):
        self.audience_path.write_text(json.dumps({
            "schema": "plx-source-mirror-audience/v1", "tenant": "plx",
            "audiences": {**GRANTED, "silver.*": ["staff:operations"]}}), encoding="utf-8")
        runtime = self.runtime()
        actor = Actor("plx", OPS)
        [row] = [item for item in runtime.mirror.rows(actor, "silver.shipments")["rows"]
                 if item["key"] == {"id": 6}]
        case_id = runtime.cases.open(actor, "Was shipment 6 closed?")
        receipt = runtime.cases.propose(
            actor, case_id, "shipment.close", {}, citations=[row["citation"]],
            expected_effect={"relation": "silver.shipments", "key": {"id": 6},
                             "fields": ["status"], "equals": "Closed"})
        self.fake.tables["silver.shipments"].append(
            {"id": 6, "shipment_code": "S-6B", "status": "Closed"})
        self.clock.advance(901)  # no since column: the refresh is a full traversal
        result = cv.verify_proposal(runtime, actor, case_id, receipt["proposal_id"])
        self.assertEqual((result["outcome"], result["reason"], result["fields"]),
                         ("unknown", "key_not_unique", {}))


class McpVerificationTests(CoverageTestCase):
    def test_read_back_is_the_verify_mode_of_plx_case_propose(self):
        # host-budget: plx.case_verify_proposal merged into plx.case_propose (proposal_id).
        names = {tool["name"] for tool in TOOLS}
        self.assertNotIn("plx.case_verify_proposal", names | CASE_RUNTIME_TOOLS)
        propose = next(tool for tool in TOOLS if tool["name"] == "plx.case_propose")
        self.assertIn("Verify mode", propose["description"])
        self.assertLessEqual({"proposal_id", "references", "max_staleness"},
                             set(propose["inputSchema"]["properties"]))
        self.assertEqual(propose["inputSchema"]["required"], ["case_id"])

    def test_generic_proposal_on_another_relation_is_verified_through_mcp(self):
        runtime = self.runtime()
        server = self.server(OPS_FINANCE, runtime, test_capabilities=True)
        case_id = self.ok(server, "plx.case_open", {
            "question": "Did Sage order S4 get its PO corrected?"})["case_id"]
        [row] = source_reads.read(server.case_runtime, server.actor, "mirror.rows", {
            "relation": "sage.oe_order_header",
            "where": [["_sage_pk", "eq", "4"]]})["rows"]
        effect = {"relation": "sage.oe_order_header", "key": {"_sage_pk": "4"},
                  "fields": ["ponumber"], "equals": "PO-7790"}
        proposal = self.ok(server, "plx.case_propose", {
            "case_id": case_id, "capability": "test.record.set_field",
            "payload": {"ponumber": "PO-7790"}, "citations": [row["citation"]],
            "expected_effect": effect})["proposal"]
        first = self.ok(server, "plx.case_propose", {
            "case_id": case_id, "proposal_id": proposal["proposal_id"]})
        self.assertEqual(first["outcome"], "effect_not_observed")
        self.assertEqual(first["fields"]["ponumber"], {"status": "differs",
                                                       "value_withheld": True})
        _set_row(self.fake, "sage.oe_order_header", "_sage_pk", "4", ponumber="PO-7790",
                 audtdate="20260929")
        second = self.ok(server, "plx.case_propose", {
            "case_id": case_id, "proposal_id": proposal["proposal_id"], "max_staleness": 0,
            "references": ["approval 42"]})
        self.assertEqual((second["outcome"], second["business_system_writes"]),
                         ("effect_observed", 0))
        page = self.ok(server, "plx.case_get", {"case_id": case_id,
                                                "section": "verifications"})
        self.assertEqual([item["outcome"] for item in page["items"]],
                         ["effect_not_observed", "effect_observed"])
        self.assertTrue(all(cite.get("mirror") for item in page["items"]
                            for cite in item["citations"]))
        summary = self.ok(server, "plx.case_get", {"case_id": case_id, "section": "summary"})
        self.assertEqual(summary["counts"]["verifications"], 2)
        # The two modes never mix: a read-back records no proposal and the reverse.
        self.assertIn("verify_mode_takes_only", self.refused(server, "plx.case_propose", {
            "case_id": case_id, "proposal_id": proposal["proposal_id"],
            "capability": "test.record.set_field", "payload": {}}))
        self.assertIn("references_and_max_staleness_need_proposal_id", self.refused(
            server, "plx.case_propose", {"case_id": case_id,
                                         "capability": "test.record.set_field",
                                         "payload": {}, "references": ["approval 42"]}))
        self.assertEqual(self.ok(server, "plx.case_get", {"case_id": case_id, "section":
                                                          "summary"})["counts"],
                         summary["counts"])
        self.assertIn("verifications", summary["sections"])
        full = self.ok(server, "plx.case_get", {"case_id": case_id})
        self.assertEqual(full["proposals"][0]["expected_effect"]["target_citation"],
                         row["citation"])
        text = self.refused(server, "plx.case_propose",
                            {"case_id": case_id, "proposal_id": "P9"})
        self.assertIn("proposal_not_found", text)
        text = self.refused(server, "plx.case_propose", {
            "case_id": case_id, "capability": "test.record.set_field", "payload": {},
            "citations": [row["citation"]], "expected_effect": {**effect, "equals": 1.5}})
        self.assertIn("invalid_expected_effect_equals", text)
        # The fixture capability binds only fixture.* relations, even on a test host.
        text = self.refused(server, "plx.case_propose", {
            "case_id": case_id, "capability": "fixture.record.set_field",
            "payload": {"ponumber": "PO-7790"}, "citations": [row["citation"]],
            "expected_effect": effect})
        self.assertIn("effect_relation_not_bound", text)
        # A production host (no test capabilities) offers neither test-only capability.
        prod = self.server(OPS_FINANCE, runtime)
        for capability in ("test.record.set_field", "fixture.record.set_field"):
            text = self.refused(prod, "plx.case_propose", {
                "case_id": case_id, "capability": capability,
                "payload": {"ponumber": "PO-7790"}, "citations": [row["citation"]],
                "expected_effect": effect})
            self.assertIn("capability_not_available_on_this_host", text)
        text = self.refused(server, "plx.case_propose", {
            "case_id": case_id, "proposal_id": proposal["proposal_id"], "max_staleness": -1})
        self.assertIn("invalid_max_staleness", text)
        # Operations alone cannot see the Sage relation: the tool refuses without detail.
        ops_server = self.server(OPS, runtime)
        text = self.refused(ops_server, "plx.case_propose",
                            {"case_id": case_id, "proposal_id": proposal["proposal_id"]})
        self.assertTrue("case_not_found" in text or "proposal_withheld" in text, text)


if __name__ == "__main__":
    unittest.main()
