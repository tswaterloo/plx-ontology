"""Dev-set baseline wiring (evals/baseline.py, scripts/eval_dev_baseline.py).

Evidence labels: unit tests are isolated-fixture (synthetic rows, fake hosts); the
end-to-end tests run the real mirror, projector, case host and ontology tools over the
oracle's synthetic fixture world (isolated-fixture), and the live path with that world
injected as the transport (injected-transport). Nothing here is live data."""

from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from evals import baseline as bl
from evals import measure, runner, score as sc
from evals.tests import fixtures as fx

ROOT = Path(__file__).resolve().parents[2]
OBJ = "SalesOrder:portal.app_sales_order:{\"id\":\"so-t1\"}"
LINE = "SalesOrderLine:portal.app_sales_order_line:{\"id\":\"sol-t1\"}"
REF = "Customer:portal.app_customer:{\"id\":\"cu-t1\"}"


def _value(prop, value, handle, role=None, unit=None):
    out = {"property": prop, "value_type": "decimal", "value": value, "markings": [],
           "citation": {"l1_handle": handle, "field_path": f"row.{prop}"}}
    if role:
        out["quantity_role"] = role
    if unit:
        out["unit"] = unit
    return out


class FakeTools:
    """The ontology tools' shapes over three objects: a root order, one line (reached by
    a reverse link, only below the default tier floor) and a referenced customer."""

    def __init__(self, *, line_gap=None, below_floor=True):
        self.calls = []
        self.below_floor = below_floor
        self.line_gap = line_gap

    def tools(self):
        return [{"name": n, "read_only": True} for n in
                ("plx.find", "plx.object", "plx.around", "plx.timeline",
                 "plx.source_citation")]

    actor = {"tenant": "plx", "principals": ["staff:operations"]}

    def read(self, name, arguments):
        return self.call(name, arguments)  # results are bare already: no output boundary

    def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "plx.find":
            return {"status": "found", "candidates": [{"object": {"object_id": OBJ}}]}
        if name == "plx.object":
            oid = arguments["object_id"]
            if oid == OBJ:
                return {"values": [], "identity": [], "gaps": [], "linked_counts": [
                    {"link_type": "SalesOrderLine.line_of", "direction": "reverse", "count": 1},
                    {"link_type": "SalesOrder.ordered_by", "direction": "forward", "count": 1}]}
            if oid == LINE:
                return {"values": [_value("qty", "2400", fx.H_LINE, "ordered", "ea")],
                        "identity": [], "linked_counts": [
                            {"link_type": "SalesOrderLine.line_of", "direction": "forward",
                             "count": 1}],
                        "gaps": [self.line_gap] if self.line_gap else []}
            return {"values": [], "identity": [], "gaps": [], "linked_counts": [
                {"link_type": "SalesOrder.ordered_by", "direction": "reverse", "count": 9}]}
        if name == "plx.around":
            step = arguments["path"][0]
            if step["link_type"] == "SalesOrderLine.line_of":
                if self.below_floor and arguments.get("tier_floor") != "lead":
                    return {"objects": [], "gaps": [{"state": "unknown",
                                                     "reason": "below_tier_floor",
                                                     "subject": "x"}]}
                return {"objects": [{"object_id": LINE}], "gaps": []}
            return {"objects": [{"object_id": REF}], "gaps": []}
        if name == "plx.timeline":
            return {"events": [], "gaps": []}
        if name == "plx.source_citation":
            found = fx.ROWS.get(arguments["citation"])
            if not found:
                return {"ok": False, "error": "citation_unresolvable"}
            relation, key, row = found
            return {"ok": True, "relation": relation, "key": key, "row": dict(row)}
        return {"ok": False, "error": "tool_refused"}

    def mcp_server(self):
        return {}


class InvestigatorTests(unittest.TestCase):
    def test_parse_prompt_reads_question_pin_and_role_list(self):
        item = fx.item()
        question, at, roles = bl.parse_prompt(runner.build_prompt(item, item["question"]["text"]))
        self.assertEqual(question, item["question"]["text"])
        self.assertEqual(at, item["as_known_at"]["time"])
        schema_roles = runner._schema()["$defs"]["quantity_role"]["enum"]
        self.assertEqual(roles, schema_roles)
        self.assertEqual(bl.asked_roles(question, roles), ["ordered", "planned", "shipped"])

    def test_role_plan_answers_cited_values_and_honest_gaps(self):
        tools = FakeTools(line_gap={"state": "inaccessible", "reason": "not_served",
                                    "subject": "SalesOrderLine.planned_quantity"})
        item = fx.item()
        transcript = runner.record(item, bl.DeterministicInvestigator(), tools)
        answer = transcript["answer"]
        self.assertEqual(transcript["agent"]["plan"], bl.PLAN_ROLE_QUANTITIES)
        self.assertFalse(transcript["agent"]["needs_agent"])
        by_name = {v["name"]: v for v in answer["values"]}
        self.assertEqual(by_name["ordered"]["value"], "2400")
        self.assertEqual(by_name["ordered"]["citations"],
                         [{"l1_handle": fx.H_LINE, "field_path": "row.qty"}])
        self.assertIsNone(by_name["planned"]["value"])
        self.assertIsNone(by_name["shipped"]["value"])
        gaps = {g["subject"]: g for g in answer["gaps"]}
        # The tool's own gap names the role: its state is reported, not guessed.
        self.assertEqual(gaps["planned quantity"]["state"], "inaccessible")
        self.assertEqual(gaps["shipped quantity"]["state"], "not_ingested")
        # The links existed only below the default floor: asked again at lead, and said so.
        self.assertIn(("plx.around", {"from": OBJ, "limit": bl.AROUND_LIMIT,
                                      "as_known_at": item["as_known_at"]["time"],
                                      "tier_floor": "lead", "path": [
                                          {"link_type": "SalesOrderLine.line_of",
                                           "direction": "reverse"}]}), tools.calls)
        self.assertEqual(gaps["link tier"]["reason"], "lead_tier_links_followed")
        self.assertEqual(answer["answerability"], "partial")
        # The answer goes through the claim gate: the ordered value is supported.
        result = sc.score(item, transcript)
        ordered = next(v for v in result["values"] if v["name"] == "ordered")
        self.assertTrue(ordered["passed"], result)

    def test_references_are_leaves_and_the_arriving_link_is_not_walked_back(self):
        tools = FakeTools(below_floor=False)
        runner.record(fx.item(), bl.DeterministicInvestigator(), tools)
        arounds = [(a["from"], a["path"][0]["link_type"], a["path"][0]["direction"])
                   for name, a in tools.calls if name == "plx.around"]
        # From the root: both links. From the line (arrived by line_of): not line_of again.
        # From the referenced customer (arrived forward): nothing (no sibling explosion).
        self.assertEqual(arounds, [(OBJ, "SalesOrderLine.line_of", "reverse"),
                                   (OBJ, "SalesOrder.ordered_by", "forward")])
        reads = [a["object_id"] for name, a in tools.calls if name == "plx.object"]
        self.assertEqual(reads, [OBJ, LINE, REF])

    def test_question_without_a_plan_is_needs_agent_and_never_a_pass(self):
        item = fx.item(question={"text": "What is the status of order SO-TEST-77?"})
        tools = FakeTools()
        transcript = runner.record(item, bl.DeterministicInvestigator(), tools)
        self.assertTrue(transcript["agent"]["needs_agent"])
        self.assertIsNone(transcript["answer"])
        # The tool layer still ran (find/object/around/timeline) and is measured.
        self.assertEqual({name for name, _ in tools.calls},
                         {"plx.find", "plx.object", "plx.around", "plx.timeline"})
        row = bl.run_item(item, bl.DeterministicInvestigator(), FakeTools(), run_pin=None,
                          evidence_label="isolated-fixture")
        self.assertEqual(row["outcome"], "needs_agent")
        self.assertEqual(row["reasons"], [])

    def test_a_planned_family_answers_through_its_plan_and_an_unplanned_one_needs_an_agent(self):
        class Planned(FakeTools):
            """FakeTools plus plx.investigate: F3 has a plan (a cited ordered value, an
            uncited Workbench count, a missing role); F1's plan answers only with counts;
            any other family has none."""

            def tools(self):
                return super().tools() + [{"name": "plx.investigate", "read_only": True}]

            def call(self, name, arguments):
                if name == "plx.find":
                    self.calls.append((name, dict(arguments)))
                    return {"status": "found", "candidates": [{"object": {"object_id": OBJ},
                            "reason": "exact_identifier",
                            "matched": {"identifier_kind": "so_number",
                                        "normalized": "SO-TEST-77"}}]}
                if name != "plx.investigate":
                    return super().call(name, arguments)
                self.calls.append((name, dict(arguments)))
                count = {"name": "line_count", "kind": "count", "value": 1,
                         "basis": [LINE], "check": "not_applicable"}
                if arguments["family"] == "F3":
                    return {"procedure": "procedure:order_quantities@0123456789ab",
                            "answerability": {"state": "partial", "reasons": ["missing"]},
                            "answer": [
                                {"name": "ordered", "kind": "property", "value": "2400",
                                 "quantity_role": "ordered", "unit": "ea",
                                 "object_id": LINE, "property": "qty", "check": "passed",
                                 "citation": {"l1_handle": fx.H_LINE,
                                              "field_path": "row.qty"}},
                                {"name": "shipped", "kind": "property", "value": None,
                                 "quantity_role": "shipped", "check": "not_applicable"},
                                count],
                            "gaps": [{"state": "not_ingested", "subject": "Shipment",
                                      "reason": "not_served"}], "conflicts": []}
                if arguments["family"] == "F1":
                    return {"procedure": "procedure:chain@0123456789ab",
                            "answerability": {"state": "answerable", "reasons": []},
                            "answer": [count], "gaps": [], "conflicts": []}
                return {"error": {"code": "no_plan", "message": "no plan"}}

        tools = Planned()
        item = fx.item()
        row = bl.run_item(item, bl.DeterministicInvestigator(), tools, run_pin=None,
                          evidence_label="isolated-fixture")
        transcript = row["transcript"]
        self.assertEqual((row["plan"], transcript["agent"]["routing"]),
                         ("order_quantities", bl.ROUTING))
        self.assertNotEqual(row["outcome"], "needs_agent")
        investigated = [a for n, a in tools.calls if n == "plx.investigate"]
        self.assertEqual(investigated, [{"family": "F3", "root": {
            "identifier": "SO-TEST-77", "identifier_kind": "so_number"},
            "as_known_at": item["as_known_at"]["time"]}])
        answer = transcript["answer"]
        by_name = {v["name"]: v for v in answer["values"]}
        self.assertEqual(by_name["ordered"]["citations"],
                         [{"l1_handle": fx.H_LINE, "field_path": "row.qty"}])
        self.assertIsNone(by_name["shipped"]["value"])
        # The uncited Workbench count is never stated; it is a visible gap instead.
        self.assertNotIn("line_count", by_name)
        self.assertIn("uncited_entries_not_stated", {g["reason"] for g in answer["gaps"]})
        ordered = next(v for v in row["score"]["values"] if v["name"] == "ordered")
        self.assertTrue(ordered["passed"], row["score"])
        # A plan whose answer cites nothing (only counts) leaves the item to an agent.
        chain = bl.run_item(fx.item(family="F1"), bl.DeterministicInvestigator(), Planned(),
                            run_pin=None, evidence_label="isolated-fixture")
        self.assertEqual((chain["outcome"], chain["plan"]), ("needs_agent", "chain"))
        # A family without a plan falls back to the walk: no count question, no plan.
        unplanned = fx.item(family="F9", question={"text": "Who approved order SO-TEST-77?"})
        tools = Planned()
        row = bl.run_item(unplanned, bl.DeterministicInvestigator(), tools, run_pin=None,
                          evidence_label="isolated-fixture")
        self.assertEqual((row["outcome"], row["plan"]), ("needs_agent", None))
        self.assertIn("plx.object", {name for name, _ in tools.calls})
        self.assertEqual(bl.eval_block([row, chain], split="dev", evidence_label="x",
                                       run_id="r", items_digest="d")["plans"],
                         {"None": 1, "chain": 1})

    def test_a_plan_total_over_cited_lines_is_a_sum_the_scorer_recomputes(self):
        """Failing first on fb57f04: a plan's computed total over the cited line values it
        read (F3 ordered over an order's lines) was an uncited figure and the line values
        were stated one by one (``ordered_1..n``, each ``cited_other_row``). The total is a
        ``sum`` calculation over the lines it consumed, which are not stated again; a total
        its inputs do not add up to, or one resting on a lead or a failed check, is never
        stated as one."""
        def line(name, handle, **extra):
            return {"name": name, "kind": "property", "value": "2400", "unit": "ea",
                    "quantity_role": "ordered", "object_id": handle, "property": "qty",
                    "check": "passed",
                    "citation": {"l1_handle": handle, "field_path": "row.qty"}, **extra}

        def total(value="4800", **extra):
            return {"name": "ordered", "kind": "computed", "value": value, "unit": "ea",
                    "quantity_role": "ordered", "inputs": ["ordered_1", "ordered_2"],
                    "steps": ["ordered = ordered_1 2400 + ordered_2 2400 = 4800 ea"],
                    "check": "not_applicable", **extra}

        def answer(entries):
            return bl.DeterministicInvestigator.plan_answer({
                "procedure": "procedure:order_quantities@0123456789ab",
                "answerability": {"state": "answerable", "reasons": []},
                "answer": entries, "gaps": [], "conflicts": []})
        stated, uncited = answer([line("ordered_1", fx.H_LINE), line("ordered_2", fx.H_LINE_B),
                                  total()])
        self.assertEqual(uncited, 0)
        self.assertEqual(stated["values"], [{
            "name": "ordered", "value": "4800", "unit": "ea", "quantity_role": "ordered",
            "calculation": {"op": "sum", "inputs": [
                {"l1_handle": h, "field_path": "row.qty", "unit": "ea",
                 "quantity_role": "ordered"} for h in (fx.H_LINE, fx.H_LINE_B)]}}])
        self.assertEqual(stated["answerability"], "answerable")

        class Planned(FakeTools):
            def tools(self):
                return super().tools() + [{"name": "plx.investigate", "read_only": True}]

            def call(self, name, arguments):
                if name == "plx.source_citation":
                    found = fx.ROWS[arguments["citation"]]
                    return {"ok": True, "relation": found[0], "key": found[1],
                            "row": dict(found[2])}
                if name != "plx.investigate":
                    return super().call(name, arguments)
                return {"procedure": "procedure:order_quantities@0123456789ab",
                        "answerability": {"state": "answerable", "reasons": []},
                        "answer": [line("ordered_1", fx.H_LINE),
                                   line("ordered_2", fx.H_LINE_B), total()],
                        "gaps": [], "conflicts": []}
        item = fx.item(known_outcome={
            "answerability": "answerable", "gaps": [],
            "values": [{"name": "ordered", "value": "4800", "quantity_role": "ordered",
                        "unit": "ea", "citations": [
                            {"l1_handle": h, "field_path": "row.qty"}
                            for h in (fx.H_LINE, fx.H_LINE_B)]}]})
        row = bl.run_item(item, bl.DeterministicInvestigator(), Planned(), run_pin=None,
                          evidence_label="isolated-fixture")
        self.assertEqual({v["name"]: (v["passed"], v["codes"]) for v in row["score"]["values"]},
                         {"ordered": (True, [])}, row["score"])
        # Not a sum of its inputs, or on a lead or a failed check: never stated as one.
        for lines, figure in (
                ([line("ordered_1", fx.H_LINE), line("ordered_2", fx.H_LINE_B)], total("5000")),
                ([line("ordered_1", fx.H_LINE, lead_basis=True), line("ordered_2", fx.H_LINE_B)],
                 total(lead_basis=True)),
                ([line("ordered_1", fx.H_LINE, check="failed"), line("ordered_2", fx.H_LINE_B)],
                 total()),
                ([line("ordered_1", fx.H_LINE), line("ordered_2", fx.H_LINE_B, unit="kg")],
                 total())):
            stated, _ = answer(lines + [figure])
            self.assertNotIn("calculation", json.dumps(stated["values"]), stated)

    def test_plan_counts_and_verdicts_are_calculations_the_scorer_recomputes(self):
        """A count citing each counted record is a ``count`` calculation and a verdict over
        the cells its primitive compared an ``equal`` one, both recomputed by the scorer
        from its own re-reads (F1 counts, F4 match_n). Inputs that do not recompute the
        figure (a record missing, a summed or tolerated comparison, one measure twice,
        mixed units) are never stated."""
        received_row = fx.handle("public.app_receipt_line", {"id": "rl-t1"}, 7)
        rows = dict(fx.ROWS, **{received_row: ("public.app_receipt_line", {"id": "rl-t1"},
                                               {"id": "rl-t1", "quantity": "2400"})})

        def cell(handle, field, role, unit=None):
            out = {"property": field, "value_type": "quantity", "value": rows[handle][2][
                field], "quantity_role": role, "markings": [],
                "citation": {"l1_handle": handle, "field_path": f"row.{field}"}}
            return dict(out, unit=unit) if unit else out
        ordered = cell(fx.H_LINE, "qty", "ordered")
        other = cell(received_row, "quantity", "received")
        planned = cell(fx.H_MO, "quantity", "planned")
        records = [{"l1_handle": h, "field_path": "row.id"} for h in (fx.H_LINE, fx.H_LINE_B)]

        def entry(name, kind, value, **extra):
            return {"name": name, "kind": kind, "value": value, "check": "not_applicable",
                    **extra}
        answer = [
            entry("line_count", "count", 2, basis=[LINE, OBJ], records=records),
            entry("lines_seen", "count", 3, basis=[LINE, OBJ, REF], records=records),
            entry("match_1", "select", True, cells=[ordered, other]),
            entry("match_2", "select", False, cells=[ordered, planned]),
            entry("match_3", "select", True, cells=[ordered, planned]),
            entry("match_4", "select", True, cells=[ordered, dict(other, quantity_role="ordered")]),
            entry("match_5", "select", True, cells=[cell(fx.H_LINE, "qty", "ordered", "ea"),
                                                    cell(received_row, "quantity", "received",
                                                         "kg")])]

        class Planned(FakeTools):
            row_b = None

            def tools(self):
                return super().tools() + [{"name": "plx.investigate", "read_only": True}]

            def call(self, name, arguments):
                if name == "plx.source_citation" and arguments["citation"] == received_row:
                    relation, key, row = rows[received_row]
                    return {"ok": True, "relation": relation, "key": key,
                            "row": self.row_b or dict(row)}
                if name != "plx.investigate":
                    return super().call(name, arguments)
                return {"procedure": "procedure:three_way_match@0123456789ab",
                        "answerability": {"state": "partial", "reasons": []},
                        "answer": copy.deepcopy(answer), "gaps": [], "conflicts": []}

        item = fx.item(family="F4", known_outcome={
            "answerability": "partial", "gaps": [],
            "values": [{"name": "line_count", "value": 2}, {"name": "match_1", "value": True},
                       {"name": "match_2", "value": False}]})
        row = bl.run_item(item, bl.DeterministicInvestigator(), Planned(), run_pin=None,
                          evidence_label="isolated-fixture")
        stated = {v["name"]: v for v in row["transcript"]["answer"]["values"]}
        self.assertEqual(stated["line_count"]["calculation"], {"op": "count", "inputs": records})
        self.assertEqual(stated["match_1"]["calculation"], {"op": "equal", "inputs": [
            {"l1_handle": fx.H_LINE, "field_path": "row.qty", "quantity_role": "ordered"},
            {"l1_handle": received_row, "field_path": "row.quantity",
             "quantity_role": "received"}]})
        self.assertEqual(set(stated), {"line_count", "match_1", "match_2"})
        self.assertEqual(row["transcript"]["agent"]["uncited_entries"], 4)
        self.assertEqual({v["name"]: (v["passed"], v["codes"]) for v in row["score"]["values"]},
                         {n: (True, []) for n in ("line_count", "match_1", "match_2")})
        self.assertEqual(row["score"]["claim_checks"]["failed"], 0)
        # The verdict is recomputed from the cells as the harness re-reads them: a cell
        # that reads otherwise makes the stated verdict wrong, never a pass.
        tampered = Planned()
        tampered.row_b = {"id": "rl-t1", "quantity": "2399"}
        row = bl.run_item(item, bl.DeterministicInvestigator(), tampered, run_pin=None,
                          evidence_label="isolated-fixture")
        match = next(v for v in row["score"]["values"] if v["name"] == "match_1")
        self.assertIn("calculation_mismatch", match["codes"])
        self.assertEqual(row["outcome"], "fail")

    def test_a_typed_identifier_binds_the_plan_root_and_free_text_never_does(self):
        """The plan's root is the typed identifier plx.find bound (a root reason), wherever
        it ranks; candidates sharing only free text bind nothing, so the plan resolves the
        question itself and two such stay ambiguous."""
        def cand(reason, kind, value):
            return {**({"reason": reason} if reason else {}), "tier": "lead" if reason not in (
                "exact_identifier", "alias_match") else "verified",
                "matched": {"identifier_kind": kind, "normalized": value}}
        question = "Which records are linked to purchase order PO100192?"
        typed = {"status": "ambiguous", "candidates": [
            cand("display_text", "display_text", "PO100192 TOTAL PAC"),
            cand("exact_identifier", "petra_po", "PO100192"),
            cand("exact_identifier", "petra_po", "PO100192")]}
        self.assertEqual(bl.DeterministicInvestigator.plan_root(typed, question),
                         {"identifier": "PO100192", "identifier_kind": "petra_po"})
        # A lead, or a candidate stating no reason (an unknown shape), binds nothing.
        for reason, kind in (("display_text", "display_text"),
                             ("secondary_reading", "petra_po"),
                             ("document_text", "display_text"),
                             (None, "petra_po")):
            free = {"status": "ambiguous", "candidates": [cand(reason, kind, "TOTAL PAC"),
                                                          cand(reason, kind, "TOTAL PAC")]}
            self.assertEqual(bl.DeterministicInvestigator.plan_root(free, question),
                             {"identifier": question}, reason)

    def test_unresolved_root_is_a_blocked_answer_scored_as_a_failure(self):
        class NotFound(FakeTools):
            def call(self, name, arguments):
                if name == "plx.find":
                    self.calls.append((name, dict(arguments)))
                    return {"status": "not_found", "candidates": [],
                            "gaps": [{"state": "unknown", "reason": "no_visible_match",
                                      "subject": "q"}]}
                return super().call(name, arguments)
        row = bl.run_item(fx.item(), bl.DeterministicInvestigator(), NotFound(),
                          run_pin=None, evidence_label="isolated-fixture")
        self.assertEqual(row["outcome"], "fail")
        self.assertEqual(row["root"], "not_found")
        self.assertIn("answerability_mismatch", row["reasons"])
        self.assertIn("value:reported_absent", row["reasons"])

    def test_tool_metrics_root_and_chain(self):
        item = fx.item(known_outcome={**fx.item()["known_outcome"], "chain": [
            {"object_type": "SalesOrder", "relation": "public.app_sales_order",
             "key": {"id": "so-t1"}},
            {"object_type": "SalesOrderLine", "relation": "public.order_entry_item",
             "key": {"id": "oi-t1"}, "hop_from": 0, "fk": "x"}]},
            root={**fx.item()["root"], "relation": "public.app_sales_order",
                  "key": {"id": "so-t1"}})
        transcript = {"events": [
            {"type": "tool_call", "name": "plx.find", "result": {
                "status": "found", "candidates": [{"object": {"display": {"origin": {
                    "citation": fx.H_SO}}}, "matched": {"binding": "portal.app_sales_order"}}]}},
            {"type": "tool_call", "name": "plx.object", "result": {"values": [
                {"citation": {"l1_handle": fx.H_LINE}}]}}]}
        metrics = bl.tool_metrics(item, transcript)
        self.assertEqual(metrics, {"root": "found", "root_kind": item["root"]["identifier_kind"],
                                   "find_relations": ["public.app_sales_order"],
                                   "plan_root": "none", "chain_rows": 2, "chain_rows_cited": 2,
                                   "tool_calls": 2})
        transcript["events"][0]["result"]["candidates"][0]["object"]["display"]["origin"][
            "citation"] = fx.H_MO
        self.assertEqual(bl.tool_metrics(item, transcript)["root"], "wrong")
        # The plan's own root lookup: the item's root row, another row, or none resolved.
        for root, reasons, want in ((OBJ, [], "found"), (LINE, [], "wrong"),
                                    (None, ["ambiguous_root"], "ambiguous"),
                                    (None, ["root_not_found"], "not_found")):
            result = {"answerability": {"state": "blocked" if reasons else "answerable",
                                        "reasons": reasons}}
            if root:
                result["root"] = {"object_id": root}
            events = transcript["events"] + [{"type": "tool_call", "name": "plx.investigate",
                                              "result": result}]
            self.assertEqual(bl.tool_metrics(item, {"events": events})["plan_root"], want)


class FakePinReader:
    """MirrorPinReader's interface over fx.ROWS: captures 7 (order lines) and 8 (orders)."""

    generation = fx.GEN

    def __init__(self, rows=None, captures=None):
        self.rows = {(rel, scoring_key(key)): dict(row) for handle, (rel, key, row) in
                     (rows or fx.ROWS).items() if sc.handle_generation(handle) == fx.GEN}
        self.captures = captures if captures is not None else {
            "public.order_entry_item": (7, "2099-01-02T03:10:00Z"),
            "public.app_manufacturing_order": (8, "2099-01-02T03:11:00Z")}

    def first_captures(self, moment):
        return {rel: cap for rel, cap in self.captures.items()
                if runner._instant(cap[1]) >= moment}

    def row_at(self, relation, key, capture_id):
        row = self.rows.get((relation, scoring_key(key)))
        return ("ok", row) if row is not None else ("row_missing_at_pin", None)


def scoring_key(key):
    return json.dumps({k: str(v) for k, v in dict(key).items()}, sort_keys=True)


class MirrorPinReaderTests(unittest.TestCase):
    """The real MirrorPinReader over a two-capture sqlite mirror (the row changed in
    capture 2): a pin at capture 1 reads the old version from row_version, a pin at
    capture 2 the current one."""

    def build(self, directory):
        import sqlite3
        path = Path(directory) / "source-mirror.sqlite"
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE mirror_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE capture (capture_id INTEGER PRIMARY KEY, relation TEXT,
                                  finished_at TEXT);
            CREATE TABLE capture_state (capture_id INTEGER PRIMARY KEY, state TEXT);
            CREATE TABLE row_current (relation TEXT, key_json TEXT, run_index INTEGER,
                run_length INTEGER, row_json TEXT, capture_id INTEGER,
                absent_since_capture INTEGER);
            CREATE TABLE row_version (relation TEXT, key_json TEXT, capture_id INTEGER,
                run_index INTEGER, run_length INTEGER, row_json TEXT,
                superseded_by_capture INTEGER);
        """)
        key = json.dumps({"id": "1"}, sort_keys=True, separators=(",", ":"))
        db.execute("INSERT INTO mirror_meta VALUES ('generation', 'gen-test')")
        db.executemany("INSERT INTO capture VALUES (?, 'public.t', ?)",
                       [(1, "2099-01-01T00:00:00Z"), (2, "2099-01-02T00:00:00Z")])
        db.executemany("INSERT INTO capture_state VALUES (?, 'complete')", [(1,), (2,)])
        db.execute("INSERT INTO row_current VALUES ('public.t', ?, 0, 1, ?, 2, NULL)",
                   (key, json.dumps({"qty": "9"})))
        db.execute("INSERT INTO row_version VALUES ('public.t', ?, 1, 0, 1, ?, 2)",
                   (key, json.dumps({"qty": "5"})))
        db.commit()
        db.close()
        return path

    def test_row_at_reads_the_version_current_at_each_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            with runner.MirrorPinReader(self.build(tmp)) as reader:
                self.assertEqual(reader.row_at("public.t", {"id": "1"}, 1),
                                 ("ok", {"qty": "5"}))
                self.assertEqual(reader.row_at("public.t", {"id": "1"}, 2),
                                 ("ok", {"qty": "9"}))
                self.assertEqual(reader.row_at("public.t", {"id": "2"}, 2),
                                 ("row_missing_at_pin", None))
                first = reader.first_captures(runner._instant("2099-01-01T12:00:00Z"))
                self.assertEqual(first["public.t"][0], 2)


class RunPinTests(unittest.TestCase):
    """devloop/DECISIONS/2026-10-02-oracle-pinning.md: the runner pins mirror captures."""

    ORACLE_GEN = "0123456789abcdef"

    def item(self):
        line = f"api://public.order_entry_item/%7B%22id%22%3A%22oi-t1%22%7D@{self.ORACLE_GEN}"
        mo = f"api://public.app_manufacturing_order/%7B%22id%22%3A%22mo-t1%22%7D@{self.ORACLE_GEN}"
        base = fx.item(as_known_at={"time": "2099-01-02T03:04:05Z",
                                    "l1_generations": {"portal_api": self.ORACLE_GEN}})
        base["known_outcome"]["values"][0]["citations"] = [{"l1_handle": line,
                                                            "field_path": "row.qty"}]
        base["known_outcome"]["values"][1]["citations"] = [{"l1_handle": mo,
                                                            "field_path": "row.quantity"}]
        return base

    def record(self, item, pin, answer=None):
        return runner.record(item, runner.ScriptedAgent(fx.scripted(answer or fx.good_answer())),
                             fx.FakeHost(), run_pin=pin)

    def test_oracle_digests_differ_from_mirror_ids_and_the_cited_mirror_row_passes(self):
        item = self.item()
        pin = runner.make_run_pin(item, FakePinReader())
        self.assertEqual(pin["status"], "pinned", pin)
        self.assertEqual(pin["relations"], {
            "public.order_entry_item": {"capture_id": 7, "finished_at": "2099-01-02T03:10:00Z"},
            "public.app_manufacturing_order": {"capture_id": 8,
                                               "finished_at": "2099-01-02T03:11:00Z"}})
        self.assertEqual((pin["checked"], pin["mirror_generation"]), (2, fx.GEN))
        transcript = self.record(item, pin)
        # The agent is told the mirror pin, never the oracle digests or the relations.
        self.assertIn(f"portal_api={fx.GEN}", transcript["prompt"])
        self.assertIn("2099-01-02T03:11:00Z", transcript["prompt"])
        self.assertNotIn(self.ORACLE_GEN, transcript["prompt"])
        self.assertNotIn("order_entry_item", transcript["prompt"])
        result = sc.score(item, transcript)
        self.assertTrue(result["passed"], result)
        self.assertIsNone(result["excluded"])
        # Without the run pin the same mirror citations are off the oracle's generation.
        bare = runner.record(item, runner.ScriptedAgent(fx.scripted(fx.good_answer())),
                             fx.FakeHost())
        codes = {c for v in sc.score(item, bare)["values"] for c in v["codes"]}
        self.assertEqual(codes, {"citation_off_generation"})

    def test_a_drifted_row_is_pin_drift_excluded_from_every_rate(self):
        item = self.item()
        rows = dict(fx.ROWS)
        relation, key, row = rows[fx.H_LINE]
        rows[fx.H_LINE] = (relation, key, {**row, "qty": "2401"})
        pin = runner.make_run_pin(item, FakePinReader(rows))
        self.assertEqual((pin["status"], pin["problems"]), ("pin_drift", {"field_differs": 1}))
        check = next(c for c in pin["checks"] if c["status"] == "field_differs")
        self.assertNotEqual(check["oracle_digest"], check["mirror_digest"])
        self.assertNotIn("2401", json.dumps(pin))  # digests, never values
        result = sc.score(item, self.record(item, pin))
        self.assertEqual(result["excluded"], "pin_drift")
        self.assertIn("pin_drift", result["codes"])
        self.assertFalse(result["passed"])
        aggregate = sc.aggregate([result])["dev"]["F3"]
        self.assertEqual((aggregate["runs"], aggregate["pin_drift_runs"]), (0, 1))
        row = bl.run_item(item, runner.ScriptedAgent(fx.scripted(fx.good_answer())),
                          fx.FakeHost(), run_pin=pin, evidence_label="isolated-fixture")
        self.assertEqual((row["outcome"], row["reasons"]), ("pin_drift", ["pin_drift:field_differs"]))
        passing = dict(row, outcome="pass", reasons=[])
        block = bl.eval_block([row, passing], split="dev", evidence_label="isolated-fixture",
                              run_id="r", items_digest="0" * 16)
        self.assertEqual((block["dev_set"], block["pin_drift"]), ("1/1", 1))
        self.assertEqual(block["by_reason"], {"pin_drift:field_differs": 1})

    def test_a_relation_with_no_capture_after_the_time_or_a_missing_row_is_drift(self):
        late = FakePinReader(captures={
            "public.order_entry_item": (7, "2099-01-02T03:00:00Z"),
            "public.app_manufacturing_order": (8, "2099-01-02T03:11:00Z")})
        pin = runner.make_run_pin(self.item(), late)
        self.assertEqual(pin["problems"], {"no_capture_at_or_after_time": 1})
        rows = {h: v for h, v in fx.ROWS.items() if h != fx.H_MO}
        pin = runner.make_run_pin(self.item(), FakePinReader(rows))
        self.assertEqual(pin["problems"], {"row_missing_at_pin": 1})

    def test_a_citation_of_a_capture_after_the_pin_is_off_generation(self):
        item = self.item()
        reader = FakePinReader(captures={
            "public.order_entry_item": (6, "2099-01-02T03:10:00Z"),
            "public.app_manufacturing_order": (8, "2099-01-02T03:11:00Z")})
        pin = runner.make_run_pin(item, reader)
        result = sc.score(item, self.record(item, pin))
        ordered = next(v for v in result["values"] if v["name"] == "ordered")
        self.assertEqual(ordered["codes"], ["citation_off_generation"])  # capture 7 > 6

    def test_handle_forms_parse_unambiguously(self):
        tail16 = "1234567890123456"  # all digits: also a legal legacy capture id
        handle = f"api://public.order_entry_item/%7B%22id%22%3A%22oi-t1%22%7D@{tail16}"
        # Read as an oracle reference only for an item that pins exactly that digest ...
        self.assertEqual(sc.row_identity(handle, {tail16}), sc.row_identity(fx.H_LINE))
        # ... and as the mirror's legacy capture form everywhere else.
        self.assertEqual(sc.row_identity(handle), sc.row_identity(fx.H_LINE))
        self.assertIsNone(sc.handle_generation(handle))
        self.assertEqual(sc.handle_capture(handle), int(tail16))
        oracle = f"api://public.order_entry_item/%7B%22id%22%3A%22oi-t1%22%7D@{self.ORACLE_GEN}"
        self.assertIsNone(sc.row_identity(oracle))  # not a tool's citation form
        self.assertIsNone(sc.handle_generation(oracle))
        self.assertEqual(sc.row_identity(oracle, {self.ORACLE_GEN}), sc.row_identity(fx.H_LINE))
        self.assertEqual(sc.handle_generation(fx.H_LINE), fx.GEN)
        # An answer citing another row than the expected one is still cited_other_row.
        item = self.item()
        answer = fx.good_answer()
        answer["values"][0]["citations"] = [{"l1_handle": fx.H_LINE_B, "field_path": "row.qty"}]
        rows = dict(fx.ROWS)
        result = sc.score(item, self.record(item, runner.make_run_pin(item, FakePinReader(rows)),
                                            answer))
        ordered = next(v for v in result["values"] if v["name"] == "ordered")
        self.assertEqual(ordered["codes"], ["cited_other_row"])


class ToolHostTests(unittest.TestCase):
    def table(self):
        def boom(arguments, actor):
            raise RuntimeError("bug")
        return {"plx.find": {"annotations": {"readOnlyHint": True},
                             "handler": lambda a, actor: {"status": "not_found",
                                                          "actor": sorted(actor.principals)}},
                "plx.source_citation": {"annotations": {"readOnlyHint": True},
                                        "handler": lambda a, actor: {"from": "table"}},
                "plx.around": {"annotations": {"readOnlyHint": True}, "handler": boom}}

    def test_case_host_tools_win_unknown_tools_are_refused_and_crashes_are_results(self):
        host = bl.ToolHost(fx.FakeHost(), self.table(), "plx", ["staff:operations"])
        names = [tool["name"] for tool in host.tools()]
        self.assertEqual(names.count("plx.source_citation"), 1)
        self.assertIn("plx.find", names)
        self.assertEqual(host.call("plx.source_citation", {"citation": fx.H_LINE})["row"],
                         fx.ROWS[fx.H_LINE][2])
        self.assertEqual(host.call("plx.find", {"query": "x"})["actor"], ["staff:operations"])
        self.assertEqual(host.call("plx.nope", {}), {"ok": False, "error": "tool_refused"})
        self.assertEqual(host.call("plx.around", {})["error"]["code"], "tool_exception")
        with self.assertRaisesRegex(runner.RunnerError, "not_served_by_the_case_host"):
            host.mcp_server()


class EvalBlockTests(unittest.TestCase):
    def rows(self):
        def row(family, outcome, reasons=(), root="found"):
            return {"family": family, "outcome": outcome, "reasons": list(reasons),
                    "root": root, "chain_rows": 3, "chain_rows_cited": 1,
                    "pin": "pinned", "pin_checked": 0}
        return [row("F1", "needs_agent", root="not_found"), row("F1", "needs_agent"),
                row("F3", "pass"), row("F3", "fail", ["gap_missing", "value:reported_absent"]),
                row("F3", "fail", ["gap_missing"]), row("F3", "blocked", ["citation_read_gap"])]

    def test_block_counts_rates_by_family_and_reason(self):
        block = bl.eval_block(self.rows(), split="dev", evidence_label="isolated-fixture",
                              run_id="r", items_digest="0" * 16)
        self.assertEqual((block["dev_set"], block["attempted"]), ("1/5", "1/3"))
        self.assertEqual((block["pass"], block["fail"], block["needs_agent"]), (1, 2, 2))
        self.assertEqual(block["by_family"]["F1"]["pass"], "0/2")
        self.assertEqual(block["by_family"]["F1"]["attempted"], "0/0")
        self.assertEqual(block["by_family"]["F3"]["attempted"], "1/3")
        self.assertEqual(block["by_family"]["F1"]["root_found"], "1/2")
        self.assertEqual((block["blocked"], block["by_family"]["F3"]["blocked"]), (1, 1))
        self.assertEqual(block["by_reason"], {"citation_read_gap": 1, "gap_missing": 2,
                                              "value:reported_absent": 1})
        self.assertEqual(block["llm_calls"], 0)
        self.assertEqual(bl.summary_line(block),
                         "dev_set 1/5 (F1 0/2 F3 1/3) attempted 1/3 needs_agent 2 "
                         "pin_drift 0 blocked 1 [isolated-fixture]")


    def test_answer_rates_cover_answers_only_and_null_without_a_denominator(self):
        """Decision 2026-10-19-b1-one-answer-rate: a justified abstention is a labelled
        (``scored``) outcome but not an answer, so all-abstain is coverage 0 with accuracy
        null, never a good score; nothing ran is null on every rate."""
        from scripts import eval_dev_baseline as script
        abstain = {"outcome": "pass", "score": {"outcome": "justified_abstention",
                                                "codes": []}}
        block = script.answer_block([abstain] * 5, 2)
        self.assertEqual((block["scored"], block["abstain_ok"], block["answered"]), (5, 5, 0))
        # the counts are the rates' own fractions: coverage over the 5 that ran, not the
        # 2 blocked upstream
        self.assertEqual((block["accuracy"], block["coverage"]), ("0/0", "0/5"))
        self.assertEqual((block["coverage_rate"], block["accuracy_rate"],
                          block["confident_unsupported_rate"]), (0.0, None, None))
        empty = script.answer_block([], 3)
        self.assertEqual((empty["coverage_rate"], empty["accuracy_rate"],
                          empty["confident_unsupported_rate"]), (None, None, None))


class FixtureBaselineTests(unittest.TestCase):
    """The 30 frozen dev items end to end on the real product path (isolated-fixture)."""

    @classmethod
    def setUpClass(cls):
        from scripts import eval_dev_baseline as script
        cls.script = script
        cls.tmp = tempfile.TemporaryDirectory()
        cls.transcripts = Path(cls.tmp.name) / "transcripts"
        cls.result = script.run_fixture(Path(cls.tmp.name) / "work",
                                        transcripts=cls.transcripts)
        cls.items = runner.load_items(script.DEV_DIR, split="dev")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_dev_item_runs_and_is_classified_once(self):
        block = self.result["eval"]
        manifest = json.loads((self.script.DEV_DIR / "manifest.json").read_text())
        self.assertEqual(block["items"], manifest["counts"]["items"])
        self.assertEqual(block["pass"] + block["fail"] + block["needs_agent"], block["items"])
        self.assertTrue(block["complete"])
        self.assertEqual({f: int(e["pass"].split("/")[1]) for f, e in block["by_family"].items()},
                         manifest["by_family"])
        self.assertRegex(block["dev_set"], r"^\d+/30$")
        self.assertEqual(block["llm_calls"], 0)
        self.assertEqual(self.result["evidence_label"], "isolated-fixture")
        self.assertEqual(self.result["product"]["objects"] > 0, True)
        # Coverage beside accuracy: per family, the expected values the answers state and
        # state right, over every expected value of the family's items.
        for family, entry in block["by_family"].items():
            expected = sum(len(i["known_outcome"]["values"]) for i in self.items
                           if i["family"] == family)
            stated, of = map(int, entry["values_stated"].split("/"))
            right, also = map(int, entry["values_right"].split("/"))
            self.assertEqual((of, also), (expected, expected), family)
            self.assertLessEqual(right, stated, family)
        # ENG-11: every question measured from its real run; a needs_agent question is an
        # unanswered one; a fixture run is never compared with a manual baseline.
        spent = block["measure"]
        self.assertEqual(spent["runs"], block["items"])
        self.assertEqual(spent["answered"] + spent["justified_abstention"]
                         + spent["unanswered"] + spent["blocked"], block["items"])
        self.assertGreaterEqual(spent["unanswered"], block["needs_agent"])
        self.assertEqual(spent["answer_ms"]["measured"], block["items"])
        self.assertGreater(spent["first_cited_evidence_ms"]["measured"], 0)
        self.assertEqual(spent["tool_calls"], sum(r["tool_calls"] for r in self.result["items"]))
        self.assertEqual(block["vs_manual"]["status"], "unmeasured")
        self.assertNotIn("time_to_answer_ms", block["vs_manual"])

    def test_each_family_runs_its_plan_and_needs_an_agent_only_without_a_cited_answer(self):
        from operating import ontology_tools_investigate as oi
        plans = {f: p.id for p in oi.load_plans().values() for f in p.doc["families"]}
        for row in self.result["items"]:
            self.assertEqual(row["plan"], plans[row["family"]], row)
            transcript = json.loads((self.transcripts / row["item_id"] / "p0-r0.transcript.json")
                                    .read_text())
            self.assertIn("plx.investigate", {e["name"] for e in transcript["events"]})
            if row["outcome"] == "needs_agent":
                # The plan answered, but only with figures no source row cites.
                self.assertIsNone(transcript["answer"])
                self.assertGreater(transcript["agent"]["uncited_entries"], 0)
                continue
            # Every value the deterministic answer states is cited, or calculated from
            # cited inputs (never stated uncited).
            for value in transcript["answer"]["values"]:
                self.assertEqual(value["value"] is None,
                                 not (value.get("citations") or value.get("calculation")), value)
            if row["outcome"] == "fail":
                self.assertTrue(row["reasons"], row)
        self.assertEqual(self.result["eval"]["abstained"],
                         sum(bool(r["abstained"]) for r in self.result["items"]))

    def test_an_f1_chain_with_a_root_is_scored_on_counts_recomputed_from_their_records(self):
        rows = [r for r in self.result["items"] if r["family"] == "F1" and r["root"] == "found"]
        self.assertTrue(rows)
        for row in rows:
            self.assertNotEqual(row["outcome"], "needs_agent", row)
            path = self.transcripts / row["item_id"] / "p0-r0.transcript.json"
            transcript = json.loads(path.read_text())
            score = json.loads(path.with_name("p0-r0.score.json").read_text())
            counts = [v for v in transcript["answer"]["values"]
                      if (v.get("calculation") or {}).get("op") == "count"]
            self.assertTrue(counts, row)
            for value in counts:  # the scorer recomputes each from the harness re-reads
                again = sc.recompute(value["calculation"], transcript["evidence"])
                self.assertEqual((again["code"], again["value"]), (None, value["value"]))
            passed = {v["name"] for v in score["values"] if v["passed"]}
            self.assertIn("root_record_count", passed)
            self.assertFalse({c for v in score["values"] for c in v["codes"]
                              if c.startswith("calculation_")}, score)

    def test_generic_tools_ran_and_citations_resolve_through_the_case_host(self):
        f3 = sorted(self.transcripts.glob("dev-F3-*/p0-r0.transcript.json"))
        self.assertEqual(len(f3), 6)
        other_row = set()
        for path in f3:
            transcript = json.loads(path.read_text())
            self.assertTrue(runner.verify_seal(transcript))
            names = {event["name"] for event in transcript["events"]}
            self.assertLessEqual({"plx.find", "plx.investigate"}, names)
            cited = [c["l1_handle"] for v in (transcript["answer"] or {}).get("values", [])
                     for c in v.get("citations", [])]
            self.assertTrue(cited)
            for handle in cited:  # re-read by the harness through plx.source_citation
                self.assertTrue(transcript["evidence"][handle]["ok"], handle)
            # The run pin (real mirror file, MirrorPinReader) pinned and drift-checked the
            # expected rows; the agent's mirror citations are on the pinned generation.
            pin = transcript["run_pin"]
            self.assertEqual(pin["status"], "pinned", pin)
            self.assertGreater(pin["checked"], 0)
            self.assertTrue(all(c["status"] == "equal" for c in pin["checks"]), pin)
            self.assertTrue(all(sc.handle_generation(h) == pin["mirror_generation"]
                                for h in cited if h.startswith("api://")))
            score = json.loads(path.with_name("p0-r0.score.json").read_text())
            codes = {c for v in score["values"] for c in v["codes"]}
            self.assertNotIn("citation_off_generation", codes, score)
            if "cited_other_row" in codes:
                other_row.add(score["item_id"])
        rows = [r for r in self.result["items"] if r["family"] == "F3"]
        # The order_quantities plan states produced as the oracle derives it, from the FM
        # status entries where they carry a value (``prefer``), so the plan cites the row
        # the oracle cites: the earlier yield-versus-status disagreement is gone.
        self.assertEqual(other_row, set())
        self.assertFalse([r["item_id"] for r in rows
                          if "value:cited_other_row" in r["reasons"]])
        self.assertTrue(all(r["root"] == "found" and r["pin"] == "pinned" for r in rows), rows)
        # Replaying the recorded transcripts scores them identically.
        replayed = runner.replay(self.items, self.transcripts)
        self.assertTrue(replayed["ok"], {k: replayed[k] for k in ("compared", "identical")})

    def test_report_publishes_no_row_values_identifiers_or_citations(self):
        text = json.dumps(self.result)
        self.assertNotIn("api://", text)
        self.assertNotIn("bc://", text)
        for item in self.items:
            self.assertNotIn(item["root"]["identifier"], text)
            for value in item["known_outcome"]["values"]:
                if isinstance(value.get("value"), str) and len(value["value"]) >= 6:
                    self.assertNotIn(value["value"], text)

    def test_committed_baseline_has_the_shape_and_names_this_dev_set(self):
        committed = json.loads(self.script.BASELINE_JSON.read_text())
        self.assertEqual(committed["format"], bl.BASELINE_FORMAT)
        self.assertEqual(committed["evidence_label"], "isolated-fixture")
        self.assertFalse(committed["values_published"])
        block = committed["eval"]
        for key in ("dev_set", "attempted", "needs_agent", "by_family", "by_reason",
                    "root_resolution", "pinned", "pin_drift", "items_digest", "llm_calls"):
            self.assertIn(key, block)
        self.assertEqual(block["items_digest"], self.result["eval"]["items_digest"])
        self.assertEqual(len(committed["items"]), block["items"])
        self.assertEqual(committed["summary_line"], bl.summary_line(block))

    def test_committed_baseline_equals_a_fresh_fixture_run(self):
        # Regenerate with: python3 -m scripts.eval_dev_baseline fixture --write-baseline
        # --write-report (after any change to the tools, projection, schema or scorer).
        # The committed file keeps everything but the wall times (evals.measure).
        committed = json.loads(self.script.BASELINE_JSON.read_text())
        fresh = measure.without_wall_clock(json.loads(json.dumps(self.result)))
        self.assertEqual(committed["eval"], fresh["eval"])
        self.assertEqual(committed["product"], fresh["product"])
        self.assertEqual(committed["items"], fresh["items"])
        self.assertEqual(committed, fresh)


class LiveSpecTests(unittest.TestCase):
    """The live path over the fixture world as an injected transport, through the live
    verifier's recording and eval-block lifting (injected-transport, not live)."""

    def test_live_run_emits_an_eval_block_the_verifier_lifts_and_status_prints(self):
        from devloop import live_verify
        from scripts import eval_dev_baseline as script
        from scripts import eval_oracle_dev as dev
        from scripts import eval_status

        def build(env, args, *, guard):
            sources, documents, configured, _ = dev.build_fixture(dev.eval_world())
            return sources, documents, configured
        spec = json.loads((ROOT / "devloop" / "live-checks" / "eval.dev_baseline.json")
                          .read_text())
        live_verify.validate_spec(spec)
        self.assertEqual(spec["cadence"], "daily")
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            argv = [arg.replace("{tmp}", tmp).replace("{session_state}", tmp + "/session")
                for arg in spec["command"][3:]]
            argv = argv[:argv.index("--count")] + ["--count", "6", "--min-items", "4"] + \
                argv[argv.index("--total-budget-seconds"):] + ["--state-dir", tmp + "/state"]
            # The run id seeds the item sample: pin it, so the sample is not the wall-clock
            # date's (the auto 'live-<UTC date>' drew an unpinned F3 item on 2026-10-11).
            argv[argv.index("--run-id") + 1] = "live-2026-10-10"
            code = script.main(argv, environ={}, live_build=build,
                               live_client=script.fixture_client(),
                               live_clock=script.fixture_clock)
        document = json.loads(out.getvalue())
        self.assertEqual(code, 0, document.get("eval"))
        for path, want in spec["expect"]["json_equals"].items():
            self.assertEqual(live_verify.resolve(document, path)[0], want, path)
        self.assertGreaterEqual(document["eval"]["items"], 4)
        self.assertEqual(document["eval"]["pin_drift"], 0, document["eval"])
        # The spec on this real output: the item floor this short run is under and the
        # deterministic baseline's miss of ROADMAP B1's accuracy bar fail.
        self.assertEqual(live_verify.check_expectations(spec, code, document),
                         ["eval.items < 20", "eval.answer.accuracy_rate < 0.8"])
        tripped = copy.deepcopy(document)
        tripped["eval"]["fail_reasons"] = ["blocked_tripwire"]
        self.assertIn("eval.fail_reasons not empty",
                      live_verify.check_expectations(spec, 3, tripped))
        self.assertNotIn("item_id", json.dumps(document["items"]))
        fields = live_verify._recorded(spec, document)
        self.assertEqual(fields["eval"]["dev_set"], document["eval"]["dev_set"])
        self.assertEqual(fields["eval"]["by_family"], document["eval"]["by_family"])
        lifted = live_verify.eval_blocks([{"id": "eval.dev_baseline", "result": "pass",
                                           "spec": "devloop/live-checks/eval.dev_baseline.json",
                                           "exit_code": 0, "detail": {"fields": fields}}])
        line = eval_status.status({"eval": lifted})["line"]
        self.assertTrue(line.startswith("eval.dev_baseline: dev_set "
                                        + document["eval"]["dev_set"]), line)
        self.assertTrue(line.endswith("[live-read] pass"), line)


class LiveOutageAndPartitionTests(unittest.TestCase):
    """Directive 2db2fe9bf1f8 PT2 (injected transport, not live): with the Portal answering
    500 the live baseline still measures on the product's own BC capture, its Portal parts
    blocked; a dev item related to a frozen held-out set moves out of the dev run."""

    SPEC = ROOT / "devloop" / "live-checks" / "eval.dev_baseline.json"

    def run_live(self, tmp: str, *, outage: bool) -> tuple[int, dict]:
        from evals.oracle import BcReads, PortalReads, Sources
        from integrations.http import SourceHttpError
        from integrations.tests.test_live_source_adapters import ENV
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from scripts import eval_dev_baseline as script
        from scripts import eval_oracle_dev as dev
        from scripts import eval_unfamiliar_sample as ev
        from tests.test_eval_unfamiliar_sample import fixture_bc_sender

        def portal_500(url, headers, **_):
            raise SourceHttpError("read-only source GET failed: HTTPError", status=500)

        world = dev.eval_world()
        client = (WorkbenchReadApiClient(script.FIXTURE_URL, script.FIXTURE_KEY,
                                         fetch=portal_500) if outage
                  else script.fixture_client())

        def build(env, args, *, guard):
            sources = Sources(PortalReads(dev.portal_port(client)), BcReads(world.bc_port()))
            return sources, world.documents(), {"portal": "injected-transport",
                                                "bc": "injected-transport"}
        fake, books = fixture_bc_sender(world)
        spec = json.loads(self.SPEC.read_text())
        argv = [arg.replace("{tmp}", tmp).replace("{session_state}", tmp + "/session")
                for arg in spec["command"][3:]]
        argv = argv[:argv.index("--count")] + ["--count", "12", "--min-items", "4"] + \
            argv[argv.index("--total-budget-seconds"):] + ["--state-dir", tmp + "/state"]
        # Pinned run id: the item sample is the same on any date, and two calls straddling
        # midnight still draw one sample.
        argv[argv.index("--run-id") + 1] = "live-2026-10-10"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = script.main(argv, environ={"BC_ENVIRONMENT": ENV["BC_ENVIRONMENT"]},
                               live_build=build, live_client=client,
                               live_clock=script.fixture_clock,
                               live_bc_capture=lambda db, seconds: ev.capture_bc(
                                   ENV, db, seconds, sender=fake, books=books))
        return code, json.loads(out.getvalue())

    def test_a_portal_500_blocks_parts_and_the_rest_measures_on_the_bc_capture(self):
        from devloop import live_verify
        spec = json.loads(self.SPEC.read_text())
        # Its requires are its session producers only: an outage upstream never blocks it.
        specs = live_verify.valid_specs(live_verify.load_specs(
            live_verify.ROOT / live_verify.SPECS_DIR_NAME))
        self.assertEqual(live_verify.blocking_requires(spec, specs), [])
        with tempfile.TemporaryDirectory() as tmp:
            code, document = self.run_live(tmp, outage=True)
        block = document["eval"]
        self.assertEqual(code, 0, block)
        self.assertEqual(block["pass_rule"], "portal_outage")
        self.assertEqual(document["product"]["captures"], ["bc", "portal_api"])
        self.assertGreaterEqual(block["measured"], 1)
        parts = {(p.get("part"), p.get("http_status"))
                 for p in live_verify.blocked_parts(document)}
        self.assertLessEqual({("portal_product", 500), ("sales_order", 500)}, parts)
        answer = block["answer"]
        # blocked items and the blocked strata's share of --count stay in the denominator
        self.assertGreater(block["blocked_quota"], 0)
        self.assertEqual(answer["blocked_upstream"],
                         block["blocked_upstream"] + block["blocked_quota"])
        self.assertEqual(answer["sampled"], block["measured"] + answer["blocked_upstream"])
        self.assertEqual(answer["scored"], sum(answer[k] for k in (
            "correct", "wrong", "abstain_ok", "missed")))
        live = dict(document, evidence_label="live-read")
        self.assertTrue(live_verify.check_expectations(spec, 0, live))  # never a full pass
        # the items that ran are judged on ROADMAP B1's bar: every one abstained (missed),
        # so nothing is answered: coverage 0 and null accuracy and unsupported rates, each a
        # failure (decision 2026-10-19-b1-one-answer-rate), never a pass
        self.assertEqual((answer["answered"], answer["coverage_rate"], answer["accuracy_rate"]),
                         (0, 0.0, None))
        self.assertEqual(live_verify.check_expectations(spec, 0, live,
                                                        spec["expect_blocked"]),
                         ["eval.answer.accuracy_rate < 0.8", "eval.answer.coverage_rate < 0.8",
                          "eval.answer.confident_unsupported_rate > 0.02"])
        recorded = live_verify._recorded(spec, live)
        self.assertEqual(recorded["eval"]["answer"], answer)

    def test_dev_items_related_to_a_frozen_held_out_set_move_out_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, first = self.run_live(tmp, outage=False)
            self.assertEqual(code, 0, first["eval"])
            self.assertEqual(first["eval"]["pass_rule"], "all_sources")
            manifest, = (Path(tmp) / "state" / "live" / "dev").glob("live-*.json")
            keys = json.loads(manifest.read_text())["key_sha256"]
            frozen = Path(tmp) / "state" / "live" / "heldout-v1" / "frozen.json"
            frozen.parent.mkdir(parents=True)
            frozen.write_text(json.dumps({"key_sha256": keys[:3]}))
            code, again = self.run_live(tmp, outage=False)
            moved = again["eval"]["partition"]
            self.assertEqual(moved["heldout_sets"], 1)
            self.assertGreaterEqual(moved["moved_to_heldout"], 1)
            kept = set(json.loads(manifest.read_text())["key_sha256"])
            self.assertEqual(kept & set(keys[:3]), set())
            self.assertEqual(again["eval"]["items_total"],
                             first["eval"]["items_total"] - moved["moved_to_heldout"])
        # a state directory inside the repository would lose the run's digests to the
        # held-out check: refused before any source read
        from scripts import eval_dev_baseline as script
        inside = ROOT / "evals" / "baseline" / "live-state-probe"
        spec = json.loads(self.SPEC.read_text())
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            argv = [arg.replace("{tmp}", tmp).replace("{session_state}", tmp + "/session")
                for arg in spec["command"][3:]]
            code = script.main(argv + ["--state-dir", str(inside)], environ={},
                               live_build=lambda *a, **k: self.fail("read before refusal"))
        self.assertEqual((code, json.loads(err.getvalue())), (1, {"error": "ValueError"}))
        self.assertFalse(inside.exists())

    def test_the_harness_re_reads_the_product_bc_capture_as_the_items_actor(self):
        """A captured BC citation re-read for the scorer resolves for the eval product's
        own principal (with its BC revision) and for no other reader."""
        import sqlite3
        from urllib.parse import unquote
        from integrations.capture_store import BC_KIND, make_handle
        from integrations.tests.test_live_source_adapters import ENV
        from scripts import eval_dev_baseline as script
        from scripts import eval_oracle_dev as dev
        from scripts import eval_unfamiliar_sample as ev
        from tests.test_eval_unfamiliar_sample import fixture_bc_sender

        fake, books = fixture_bc_sender(dev.eval_world())
        reads = {}
        with tempfile.TemporaryDirectory() as tmp:
            product = script.build_product(   # Portal not configured: BC capture only
                None, Path(tmp), [], budget_seconds=None,
                bc_capture=lambda db, seconds: ev.capture_bc(ENV, db, seconds, sender=fake,
                                                             books=books),
                bc_environment=ENV["BC_ENVIRONMENT"])
            with sqlite3.connect(Path(tmp) / "bc-capture" / "capture.sqlite") as conn:
                generation, = conn.execute(
                    "SELECT value FROM meta WHERE key = 'generation'").fetchone()
                path, capture_id, version = conn.execute(
                    "SELECT path, capture_id, source_version FROM versions WHERE kind = 'bc' "
                    "ORDER BY capture_id LIMIT 1").fetchone()
            handle = make_handle(BC_KIND, [unquote(p) for p in path.split("/")], generation,
                                 capture_id)
            for principal in (script.PRINCIPAL, "eval:other-employee"):
                host = product.host([principal])
                try:
                    reads[principal] = host.read("plx.source_citation", {"citation": handle})
                finally:
                    host.close()
        own, other = reads[script.PRINCIPAL], reads["eval:other-employee"]
        self.assertEqual((own["ok"], own["provenance"]["revision"]), (True, version))
        self.assertTrue(own["row"])
        self.assertFalse(isinstance(other, dict) and other.get("ok") and other.get("row"),
                         other)


if __name__ == "__main__":
    unittest.main()
