"""The eval runner's prompt and pre-submit check: what the agent is told, and can check,
before it submits an answer.

Evidence label: isolated-fixture (synthetic rows, a fake host, a scripted agent)."""

from __future__ import annotations

import copy
import unittest
from unittest import mock

from evals import measure, runner, score as sc
from evals.tests import fixtures as fx


class RecordingHost(fx.FakeHost):
    """The fixture host, recording every read and call the check makes."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    def read(self, name, arguments):
        self.reads.append(name)
        return super().read(name, arguments)


def calculated(*, citations: bool) -> dict:
    """A drafted total over two cited lines (4800 = 2400 + 2400)."""
    value = {"name": "ordered_total", "value": "4800", "quantity_role": "ordered",
             "calculation": {"op": "sum", "inputs": [
                 {"l1_handle": fx.H_LINE, "field_path": "row.qty", "quantity_role": "ordered"},
                 {"l1_handle": fx.H_LINE_B, "field_path": "row.qty",
                  "quantity_role": "ordered"}]}}
    if citations:
        value["citations"] = [{"l1_handle": fx.H_LINE, "field_path": "row.qty"}]
    return fx.good_answer(text="Ordered 4800 units in total and planned 2350; shipment is "
                               "not ingested.",
                          values=[value, fx.good_answer()["values"][1],
                                  fx.good_answer()["values"][2]])


def scored(answer: dict) -> dict:
    item = fx.item()
    agent = runner.ScriptedAgent(fx.scripted(answer))
    return sc.score(item, runner.record(item, agent, fx.FakeHost()))


class PromptPresubmitTests(unittest.TestCase):
    def prompt(self) -> str:
        item = fx.item()
        return runner.build_prompt(item, item["question"]["text"])

    def test_the_prompt_asks_for_contradictory_source_statements_as_conflict_gaps(self):
        prompt = self.prompt()
        self.assertIn(measure.CONFLICT_REASON, prompt)
        sentence = next(line for line in prompt.splitlines()
                        if measure.CONFLICT_REASON in line)
        self.assertIn("contradict", sentence)
        self.assertIn("gap", sentence)
        self.assertIn("cite", sentence)  # both statements are cited, never silently chosen

    def test_the_conflict_instruction_adds_no_known_outcome_to_the_prompt(self):
        for leaked in ("oracle:", "known_outcome", "not_served"):
            self.assertNotIn(leaked, self.prompt())

    def test_the_prompts_number_rule_says_what_the_gate_flags(self):
        """Each example the prompt names is judged by the check as the prompt says."""
        prompt = self.prompt()
        base = "Ordered 2400 units and planned 2350; shipment is not ingested. "
        for written, flagged in (("x7342", True), ("NET30", True), ("Q3", True),
                                 ("QTY-12", True), ("USD-7342", True), ("10k", True),
                                 ("twelve", True), ("25%", True), ("2026-01-07", True),
                                 ("nine", False), ("SKU-7342", False), ("QRX-082", False),
                                 ("SO-61873Q", False), ("PO7700318264", False),
                                 ("10:30", False), ("January 7, 2026", False)):
            with self.subTest(written=written):
                if written in ("x7342", "NET30", "Q3", "QTY-12", "USD-7342", "SKU-7342",
                               "QRX-082", "10k", "25%", "SO-61873Q", "PO7700318264"):
                    self.assertIn(written, prompt)
                checked = runner.presubmit_check(fx.good_answer(text=base + written + "."),
                                                 fx.FakeHost())
                self.assertEqual("claim_uncited_number" in checked["codes"], flagged)
        for named in ("from ten up", "ISO date or timestamp", "clock times",
                      "month-name dates", "percent unit"):
            self.assertIn(named, prompt)

    def test_the_prompt_names_the_prose_number_and_calculation_rules(self):
        prompt = self.prompt()
        sentence = next(line for line in prompt.splitlines()
                        if "Every number in prose is a value claim" in line)
        self.assertIn("claim_uncited_number", sentence)
        self.assertIn("\"values\"", sentence)
        calculation = next(line for line in prompt.splitlines()
                           if "citations_beside_calculation" in line)
        self.assertIn("no \"citations\"", calculation)


class PresubmitCheckTests(unittest.TestCase):
    def test_a_citation_beside_a_calculation_is_reported_as_the_scorer_drops_it(self):
        draft = calculated(citations=True)
        checked = runner.presubmit_check(draft, fx.FakeHost())
        self.assertIn("citations_beside_calculation", checked["codes"])
        self.assertIn("answer_malformed", checked["codes"])
        self.assertFalse(checked["ok"])
        self.assertFalse(checked["gated"])   # the scorer gates no malformed answer either
        self.assertIn({"where": "values.0.calculation", "code": "citations_beside_calculation"},
                      checked["failures"])
        self.assertIn("no_answer", scored(draft)["codes"])
        # without the citations the same calculation is clean, here and in the scorer
        fixed = calculated(citations=False)
        self.assertEqual(runner.presubmit_check(fixed, fx.FakeHost())["codes"], [])
        self.assertNotIn("no_answer", scored(fixed)["codes"])

    def test_a_prose_number_without_a_value_claim_is_claim_uncited_number(self):
        draft = fx.good_answer(text="Ordered 2400 units and planned 2350, 17 damaged; "
                                    "shipment is not ingested.")
        checked = runner.presubmit_check(draft, fx.FakeHost())
        self.assertIn("claim_uncited_number", checked["codes"])
        self.assertIn("claim_unstructured", checked["codes"])
        self.assertIn({"where": "outcome", "code": "claim_unstructured", "value": "17"},
                      checked["failures"])
        self.assertFalse(checked["ok"])
        self.assertIn("claim_uncited_number", scored(draft)["violations"])

    def test_a_stated_value_with_no_citation_is_claim_uncited_number(self):
        draft = fx.good_answer()
        draft["values"].append({"name": "damaged", "value": "17"})
        checked = runner.presubmit_check(draft, fx.FakeHost())
        self.assertEqual({"claim_uncited", "claim_uncited_number"} - set(checked["codes"]),
                         set())
        self.assertIn("claim_uncited_number", scored(draft)["violations"])

    def test_a_clean_draft_has_no_codes_as_object_or_final_message(self):
        draft = fx.good_answer()
        for given in (draft, fx.final_text(draft)):
            checked = runner.presubmit_check(given, fx.FakeHost())
            self.assertEqual((checked["codes"], checked["failures"], checked["ok"],
                              checked["gated"]), ([], [], True, True))
        self.assertTrue(scored(draft)["passed"])

    def test_a_wrong_cited_value_or_an_unreadable_citation_is_not_clean(self):
        wrong = fx.good_answer(text="Ordered 2500 units and planned 2350; shipment is not "
                                    "ingested.")
        wrong["values"][0]["value"] = "2500"
        self.assertIn("claim_value_mismatch",
                      runner.presubmit_check(wrong, fx.FakeHost())["codes"])
        unread = fx.good_answer()
        unread["values"][0]["citations"][0]["l1_handle"] = fx.handle(
            "public.order_entry_item", {"id": "oi-none"}, 7)
        self.assertFalse(runner.presubmit_check(unread, fx.FakeHost())["ok"])

    def test_unknown_input_fails_closed(self):
        for given in (None, {}, [], 7, "", "no answer here", {"format": "other/v1"},
                      "```json\n{\"format\": \"plx-eval-answer/v1\"\n```"):
            with self.subTest(given=given):
                checked = runner.presubmit_check(given, fx.FakeHost())
                self.assertEqual((checked["codes"], checked["ok"]), (["answer_missing"], False))
        headless = [{"name": "x", "value": "1", "citations": [{"field_path": "row.qty"}]}]
        for broken in ({"values": "2400"}, {"values": headless}, {"answerability": "maybe"}):
            with self.subTest(broken=broken):
                checked = runner.presubmit_check(fx.good_answer(**broken), fx.FakeHost())
                self.assertIn("answer_malformed", checked["codes"])
                self.assertFalse(checked["ok"])
        with self.assertRaises(runner.RunnerError):
            runner.presubmit_check(fx.good_answer())  # no host, no evidence: never a guess

    def test_the_check_is_read_only_and_submits_nothing(self):
        host, draft = RecordingHost(), fx.good_answer(text="Ordered 2400 and 9 more.")
        before = copy.deepcopy(draft)
        runner.presubmit_check(draft, host)
        self.assertEqual(draft, before)
        self.assertEqual(host.calls, ["plx.source_citation"] * 2)   # the two cited rows
        self.assertEqual(set(host.reads), {"plx.source_citation"})

        seen = []

        def script(prompt, call):
            call("plx.find", {"relation": "public.order_entry_item"})
            seen.append(call(runner.PRESUBMIT_TOOL, {"answer": draft}))
            seen.append(call(runner.PRESUBMIT_TOOL, {"answer": before | {
                "text": "Ordered 2400 units and planned 2350; shipment is not ingested."}}))
            return fx.final_text(fx.good_answer())
        host = RecordingHost()
        item = fx.item(scoring={"max_tool_calls": 1})
        transcript = runner.record(item, runner.ScriptedAgent(script), host)
        # each check is a marker event, never a tool call, and never carries the draft
        self.assertEqual([(e["type"], e.get("name"), e.get("codes"), e.get("ok"))
                          for e in transcript["events"]],
                         [("tool_call", "plx.find", None, None),
                          ("answer_check", None,  # "9 more": prose no claim checks
                           ["claim_prose_unchecked", "claim_uncited_number",
                            "claim_unstructured"], False),
                          ("answer_check", None, [], True)])
        self.assertTrue(all(set(e) == {"seq", "type", "t_ms", "ok", "codes"}
                            for e in transcript["events"] if e["type"] == "answer_check"))
        self.assertEqual(transcript["usage"]["tool_calls"], 1)   # the check is no tool call
        self.assertNotIn("plx.case_propose", host.calls)
        self.assertEqual(transcript["answer"], fx.good_answer())  # the final message, only
        result = sc.score(item, transcript)
        self.assertTrue(result["passed"], result)          # within max_tool_calls: 1
        self.assertEqual((result["violations"], result["measure"]["tool_calls"]), ([], 1))
        self.assertEqual([check["codes"] for check in seen],
                         [["claim_prose_unchecked", "claim_uncited_number",
                           "claim_unstructured"], []])
        # the two checks read each cited row once between them; the harness re-reads after
        self.assertEqual(host.reads, ["plx.source_citation"] * 4)
        self.assertTrue(all(read["ok"] for read in transcript["evidence"].values()))

    def test_the_re_read_budget_caps_checks_and_never_starves_the_final_re_read(self):
        rows = {fx.handle("public.order_entry_item", {"id": f"oi-{n}"}, 7):
                ("public.order_entry_item", {"id": f"oi-{n}"}, {"id": f"oi-{n}", "qty": "2400"})
                for n in range(60)}

        def draft(count):
            return fx.good_answer(text="Ordered 2400 units.", gaps=[],
                                  answerability="answerable", values=[
                {"name": f"line_{n}", "value": "2400", "quantity_role": "ordered",
                 "citations": [{"l1_handle": handle, "field_path": "row.qty"}]}
                for n, handle in enumerate(list(rows)[:count])])
        for count, exhausted in ((1, False), (10, False), (40, False), (41, True),
                                 (60, True)):
            with self.subTest(count=count):
                host = fx.FakeHost(rows)
                checked = runner.presubmit_check(draft(count), host)
                self.assertEqual(checked["codes"],
                                 ["presubmit_reads_exhausted"] if exhausted else [])
                self.assertEqual((checked["ok"], checked["gated"]),
                                 (not exhausted, not exhausted))
                self.assertLessEqual(len(host.calls), runner.PRESUBMIT_MAX_READS)
        # one run's checks share the budget: re-checking reads nothing again, and once
        # spent no check reads more, while the harness's own re-read is untouched
        host = RecordingHost()
        host.rows = rows
        answer = draft(30)

        def script(prompt, call):
            for _ in range(3):
                call(runner.PRESUBMIT_TOOL, {"answer": answer})
            call(runner.PRESUBMIT_TOOL, {"answer": draft(60)})
            return fx.final_text(draft(60))
        transcript = runner.record(fx.item(), runner.ScriptedAgent(script), host)
        self.assertEqual([e["codes"] for e in transcript["events"]],
                         [[], [], [], ["presubmit_reads_exhausted"]])
        self.assertEqual(len(host.reads), runner.PRESUBMIT_MAX_READS + 60)
        self.assertEqual(len(transcript["evidence"]), 60)
        self.assertTrue(all(read["ok"] for read in transcript["evidence"].values()))

    def test_require_citations_follows_the_item_when_the_caller_knows_it(self):
        draft = fx.good_answer()
        draft["values"].append({"name": "note_count", "value": "3"})
        self.assertIn("claim_uncited", runner.presubmit_check(draft, fx.FakeHost())["codes"])
        waived = runner.presubmit_check(draft, fx.FakeHost(), require_citations=False)
        self.assertEqual(waived["codes"], [])
        item = fx.item(scoring={"citations_required": False})
        agent = runner.ScriptedAgent(fx.scripted(draft))
        self.assertTrue(sc.score(item, runner.record(item, agent, fx.FakeHost()))["passed"])

    def test_the_check_runs_the_scorer_functions_not_a_copy(self):
        draft = calculated(citations=False)
        with mock.patch.object(sc, "gate", wraps=sc.gate) as gate, \
                mock.patch.object(sc, "calculation_problem",
                                  wraps=sc.calculation_problem) as calculation, \
                mock.patch.object(sc, "answer_problems", wraps=sc.answer_problems) as form:
            runner.presubmit_check(draft, fx.FakeHost())
        self.assertEqual(gate.call_count, 1)
        self.assertTrue(gate.call_args.kwargs["require_citations"])
        self.assertGreaterEqual(calculation.call_count, 1)
        self.assertEqual(form.call_count, 1)
        # a gate code the scorer counts as an uncited number is one here too
        with mock.patch.object(sc, "UNCITED_NUMBER_GATE_CODES",
                               frozenset({"claim_value_mismatch"})):
            wrong = fx.good_answer()
            wrong["values"][0]["value"] = "2500"
            self.assertIn("claim_uncited_number",
                          runner.presubmit_check(wrong, fx.FakeHost())["codes"])

    def test_every_value_is_checked_at_1_10_and_60_values(self):
        for count in (1, 10, 60):
            with self.subTest(count=count):
                values = [{"name": f"line_{n}", "value": "2400", "quantity_role": "ordered",
                           "citations": [{"l1_handle": fx.H_LINE, "field_path": "row.qty"}]}
                          for n in range(count)]
                clean = fx.good_answer(text="Ordered 2400 units.", values=values,
                                       gaps=[], answerability="answerable")
                self.assertEqual(runner.presubmit_check(clean, fx.FakeHost())["codes"], [])
                last = copy.deepcopy(clean)
                del last["values"][-1]["citations"]
                checked = runner.presubmit_check(last, fx.FakeHost())
                self.assertEqual([f["where"] for f in checked["failures"]
                                  if f["code"] == "claim_uncited"], [f"V{count - 1}"])


def session(final: str, calls=()):
    """A fake Claude Code transport whose session makes ``calls`` (tool, arguments, result,
    is_error) and ends with ``final``."""
    def spawn(argv, cwd, env, timeout):
        from evals.tests.test_runner import _stream
        return 0, _stream(final, calls)
    return spawn


def gate_call(outcome, *, passed=0, refused=0, is_error=False, result=None):
    """One ``plx.case_get`` answer-mode call and the gate result the session got back."""
    answer = {"claims": [{"text": "Ordered 2400 units.", "citations": [fx.H_LINE]}]}
    if result is None:
        result = {"outcome": outcome, "reason": None,
                  "claims": [{"text": "Ordered 2400 units.", "citations": [fx.H_LINE]}] * passed,
                  "unsupported_claims": [{"text": "Ordered 2500 units.",
                                          "reason": "claim_value_mismatch"}] * refused}
    return (runner.ANSWER_GATE_TOOL, {"answer": answer}, result, is_error)


def gate_event(call):
    """A recorded tool-call event of one ``gate_call``/``passing_gate_call`` tuple."""
    name, arguments, result, is_error = call
    return {"type": "tool_call", "name": name, "arguments": arguments, "result": result,
            "is_error": is_error}


def passing_gate_call(answer, *, roles=False):
    """A ``plx.case_get`` answer-mode call whose gate decision passed every stated value of
    ``answer`` on the cell it cites (the shape the output boundary returns); ``roles``:
    each value claim carries the value's ``quantity_role`` (the gate checked it)."""
    def wrap(value):
        return {"origin": {"citation": "workbench://plx.case_get"}, "untrusted_text": value}
    claims = [{"id": f"m{n + 1}", "check": {"status": "passed", "failures": []},
               "citations": [wrap(cite["l1_handle"])], "text": wrap(str(value["value"])),
               "value_claims": [{"citation": cite["l1_handle"], "field": cite["field_path"],
                                 "kind": "number", "value": wrap(value["value"]),
                                 **({"role": value["quantity_role"]}
                                    if roles and value.get("quantity_role") else {})}]}
              for n, value in enumerate(answer["values"]) if value.get("value") is not None
              for cite in value["citations"]]
    return (runner.ANSWER_GATE_TOOL, {"answer": {"claims": []}},
            {"outcome": "answered", "reason": None, "claims": claims,
             "unsupported_claims": []}, False)


class GateHost(fx.FakeHost):
    """The fixture host listing the tool the gate is reached through."""

    def tools(self):
        return super().tools() + [{"name": runner.ANSWER_GATE_TOOL, "read_only": True},
                                  {"name": "plx.read", "read_only": True}]


class AnswerGateTests(unittest.TestCase):
    """The harness and the Workbench's one final-answer gate (``operating.answer_gate``,
    reached in the session as ``plx.case_get`` answer mode): the runner records what the
    gate returned to the agent, and the final reply holds only what it passed
    (``gate_final_reply``)."""

    def submitted(self, given, calls=()):
        item = fx.item()
        agent = runner.ClaudeCodeAgent(spawn=session(given if isinstance(given, str)
                                                     else fx.final_text(given), calls))
        transcript = runner.record(item, agent, GateHost())
        return transcript, sc.score(item, transcript)

    def test_the_scored_reply_holds_only_what_the_gate_passed(self):
        # The final reply leaves through ``gate_final_reply``: a value no gate decision of
        # the session passed is withheld (null, a gap), never scored as stated; a reply
        # the gate passed whole is scored exactly as written.
        wrong = fx.good_answer(text="Ordered 2500 units and planned 2350; shipment is not "
                                    "ingested.")
        wrong["values"][0]["value"] = "2500"
        # The gate checked each value with its role: the reply is kept as written. A
        # gate that passed the values without their roles never checked the role the
        # reply adds: the value is withheld (below).
        passed = passing_gate_call(fx.good_answer(), roles=True)
        unchecked = passing_gate_call(fx.good_answer())
        for given, calls, kept, code in (
                (fx.final_text(wrong), (), False, None),
                (fx.final_text(fx.good_answer()), (), False, None),
                (fx.final_text(wrong), (passed,), False, None),
                (fx.final_text(fx.good_answer()), (passed,), True, None),
                ("no answer here", (), True, "no_answer"),
                (fx.final_text(fx.good_answer(answerability="maybe")), (), False,
                 "no_answer")):
            with self.subTest(given=given[:40], gated=bool(calls)):
                transcript, result = self.submitted(given, calls)
                reply = transcript["agent"]["final_reply"]
                self.assertEqual(transcript["final_text"] == given, kept)
                self.assertEqual(reply["rewritten"], not kept)
                if not kept:
                    self.assertEqual(reply["agent_final_text"], given)
                    # Neither the refused value nor one stated without its passed claim.
                    self.assertNotIn("2500", transcript["final_text"])
                    self.assertNotIn("2400", transcript["final_text"])
                self.assertEqual(result["passed"], kept and code is None)
                # Never a confident unsupported answer: what was not passed is withheld.
                self.assertNotIn("unsupported_assertion", result["codes"])
                self.assertFalse(sc.confident_unsupported(result))
                if code:
                    self.assertIn(code, result["codes"])
                if not calls:
                    self.assertEqual(transcript["agent"]["answer_gate"], {
                        "submissions": 0, "outcomes": [], "passed_claims": [],
                        "refused_claims": [], "final": None, "repaired": False})
                self.assertFalse([e for e in transcript["events"]
                                  if e["type"] == runner.PRESUBMIT_EVENT])
        # a role the gate never checked is withheld with its value, never dropped alone:
        # the reply's "Ordered 2400" would then state a role nothing checked
        transcript, result = self.submitted(fx.final_text(fx.good_answer()), (unchecked,))
        self.assertEqual(transcript["agent"]["final_reply"]["withheld"],
                         ["ordered_qty", "planned_qty"])
        self.assertNotIn("2400", transcript["final_text"])
        self.assertFalse(sc.confident_unsupported(result))
        self.assertFalse(hasattr(runner, "gate_answer"))
        with self.assertRaises(TypeError):
            runner.ClaudeCodeAgent(spawn=session("x"), answer_gate=True)

    def test_the_reply_states_the_gates_answerability_and_the_checked_quote(self):
        """A reply states what the gate decided: the less certain of its answerability
        and the answer's own (GP-05: the gate never makes an agent's partial or blocked
        answerable; a blocked beside a passed claim is partial), and each kept value as
        the gate checked it (the claim's quote on its citation)."""
        good = fx.good_answer()
        call = passing_gate_call(good, roles=True)
        name, arguments, result, error = call
        quoted = {**result, "claims": [
            {**claim, "value_claims": [{**vc, "quote": {
                "origin": {"citation": "workbench://plx.case_get"},
                "untrusted_text": f"qty {vc['value']['untrusted_text']}"}}
                for vc in claim["value_claims"]]} for claim in result["claims"]]}
        for state, given, want in (("partial", "answerable", "partial"),
                                   ("answerable", "partial", "partial"),
                                   ("answerable", "blocked", "partial"),
                                   ("partial", "blocked", "partial"),
                                   ("answerable", "answerable", "answerable"),
                                   ("maybe", "answerable", "answerable")):
            with self.subTest(state=state):
                gate = (name, arguments, {**quoted, "answerability": {"state": state}}, error)
                text, report = runner.gate_final_reply(
                    fx.final_text({**good, "answerability": given}), [gate_event(gate)])
                reply, _ = sc.parse_answer(text)
                self.assertEqual(reply["answerability"], want)
                # the answer's own gaps stay: the gate refuted none of them
                self.assertEqual(reply["gaps"], good["gaps"])
                stated = [v for v in reply["values"] if v.get("value") is not None]
                self.assertEqual([c.get("quote") for v in stated for c in v["citations"]],
                                 ["qty 2400", "qty 2350"])
                self.assertEqual(report["withheld"], [])
        # a value the gate never passed is withheld: an answerable decision is partial
        gate = (name, arguments, {**quoted, "claims": quoted["claims"][:1],
                                  "answerability": {"state": "answerable"}}, error)
        text, report = runner.gate_final_reply(fx.final_text(good), [gate_event(gate)])
        self.assertEqual((sc.parse_answer(text)[0]["answerability"], report["withheld"]),
                         ("partial", ["planned_qty"]))

    def test_a_unit_or_currency_the_gate_did_not_verify_is_dropped_never_the_value(self):
        """68160 "units" on a page that prints no unit
        beside the figure came back null. The gate passes the figure with the unit
        ``unverified``: the reply keeps the value, with its checked quote, and drops only
        the unit (a currency the same)."""
        page, quote = "original://co-526103c21a4e#page=11", "| Total | 68,304 | 68,160 | 144 |"

        def wrap(text, origin="workbench://plx.case_get"):
            return {"origin": {"citation": origin}, "untrusted_text": text}
        for key, given in (("unit", "units"), ("currency", "CAD")):
            with self.subTest(key=key):
                value = {"name": "final_units_sum", "value": "68160", key: given,
                         "citations": [{"l1_handle": page, "field_path": "text",
                                        "quote": quote}]}
                answer = fx.good_answer(answerability="answerable", gaps=[],
                                        text="The final units sum to 68160.",
                                        values=[value])
                result = {"outcome": "answered", "reason": None, "unsupported_claims": [],
                          "answerability": {"state": "answerable"},
                          "claims": [{"id": "m1", "check": {"status": "passed",
                                                            "failures": [],
                                                            "unit_basis": "unverified"},
                                      "citations": [wrap(page)],
                                      "value_claims": [{"citation": page, "field": "text",
                                                        "kind": "number",
                                                        "value": wrap("68160", page),
                                                        "quote": wrap(quote),
                                                        # the gate records a unit it
                                                        # could not verify; a currency
                                                        # it cannot verify fails the
                                                        # claim, so a passed one has none
                                                        **({key: wrap(given)}
                                                           if key == "unit" else {})}]}]}
                gate = gate_event((runner.ANSWER_GATE_TOOL, {"answer": {"claims": []}},
                                   result, False))
                text, report = runner.gate_final_reply(fx.final_text(answer), [gate])
                reply, _ = sc.parse_answer(text)
                self.assertEqual(report["withheld"], [])
                kept = reply["values"][0]
                self.assertEqual((kept["value"], kept["citations"][0]["quote"]),
                                 ("68160", quote))
                self.assertNotIn(key, kept)
                self.assertEqual(reply["answerability"], "answerable")

    def test_the_record_counts_each_gate_result_at_1_10_and_60_submissions(self):
        kinds = [gate_call("repair", refused=1), gate_call("answered", passed=2),
                 gate_call("abstained"), gate_call(None, is_error=True,
                                                   result={"error": "answer_invalid"}),
                 gate_call(None, result={"outcome": "maybe"}),
                 gate_call(None, result="not a gate result")]
        expected = ["repair", "answered", "abstained", "refused", "unreadable", "unreadable"]
        for count in (1, 10, 60):
            with self.subTest(count=count):
                calls = [kinds[n % len(kinds)] for n in range(count)]
                # Not answer mode, or another tool: never a submission.
                calls.insert(count // 2, (runner.ANSWER_GATE_TOOL, {}, {"case": {}}, False))
                calls.insert(0, ("plx.read", {"citation": fx.H_LINE}, {"outcome": "answered"},
                                 False))
                transcript, _ = self.submitted(fx.good_answer(), calls)
                gate = transcript["agent"]["answer_gate"]
                self.assertEqual(gate["outcomes"],
                                 [expected[n % len(kinds)] for n in range(count)])
                self.assertEqual(gate["submissions"], count)
                self.assertEqual(gate["passed_claims"],
                                 [[0, 2, 0, 0, 0, 0][n % len(kinds)] for n in range(count)])
                self.assertEqual(gate["refused_claims"],
                                 [[1, 0, 0, 0, 0, 0][n % len(kinds)] for n in range(count)])
                self.assertEqual(gate["final"], expected[(count - 1) % len(kinds)])
                self.assertEqual(gate["repaired"], count > 1)
                text = sc.canonical(gate)
                self.assertNotIn(fx.H_LINE, text)
                self.assertNotIn("2500", text)

    def test_the_eval_summary_counts_what_the_gate_returned_never_its_claims(self):
        from scripts import eval_real_agent as era
        runs = [self.submitted(fx.good_answer(), [gate_call("repair", refused=1),
                                                  gate_call("answered", passed=1)]),
                self.submitted(fx.good_answer(), [gate_call("repair", refused=2),
                                                  gate_call("abstained")]),
                self.submitted(fx.good_answer(), [gate_call(None, is_error=True,
                                                            result={"error": "x"})]),
                self.submitted(fx.good_answer())]
        summary = era.part_summary([r for _, r in runs], [t for t, _ in runs])
        self.assertEqual(summary["answer_gate"], {
            "runs": 4, "submitted": 3, "submissions": 5, "repairs": 2, "repaired": 1,
            "refused_claims": 3, "final": {"answered": 1, "repair": 0, "abstained": 1,
                                           "refused": 1, "unreadable": 0}})
        self.assertNotIn("2500", sc.canonical(summary["answer_gate"]))


if __name__ == "__main__":
    unittest.main()
