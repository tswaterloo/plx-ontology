"""The final reply holds only what the session's answer gate passed (``evals.runner.
gate_final_reply`` over ``operating.answer_gate.unbacked``): the reply a reader sees and
the one the scorer grades. Reproduces the two card-sample patterns that scored
confident_unsupported: a value reworded after the gate's final refusal (F24-0053) and a
number the gate's text check flagged written in the final text (F23-0056).

Evidence label: ``isolated-fixture`` (recorded-shape gate results, no host, no model); the
end-to-end path through the real stdio host is ``evals.tests.test_answer_gate_one_path``.
"""

from __future__ import annotations

import json
import time
import unittest

from evals import runner, score as sc
from evals.tests import fixtures as fx
from operating import answer_gate, claim_checks

DOC = "sharepoint://drive/item@%22v1%22"
MAP = "sharepoint://drive/map@%22v2%22"


def _wrap(value):
    """A value as the output boundary returns it (``untrusted_text`` envelope)."""
    return {"origin": {"citation": "workbench://plx.case_get"}, "untrusted_text": value}


def _submit(outcome, passed=(), refused=()):
    """One ``plx.case_get`` answer submission as the stream records it: ``passed`` (value,
    handle, field) the gate passed, ``refused`` (text, value) it refused."""
    claims = [{"id": f"m{n + 1}", "check": {"status": "passed", "failures": []},
               "citations": [_wrap(handle)], "text": _wrap(f"content text: {value}"),
               "value_claims": [{"citation": handle, "field": field, "kind": "text",
                                 "value": _wrap(value)}]}
              for n, (value, handle, field) in enumerate(passed)]
    unsupported = [{"reason": "claim_check_failed", "text": _wrap(text),
                    "failures": [{"code": "claim_value_mismatch", "value": _wrap(value),
                                  "detail": _wrap("not verbatim")}]}
                   for text, value in refused]
    return {"type": "tool_call", "name": "plx.case_get",
            "arguments": {"answer": {"claims": []}}, "is_error": False,
            "result": {"outcome": outcome, "claims": claims,
                       "unsupported_claims": unsupported}}


def _answer(text, values=(), gaps=(), answerability="partial"):
    return {"format": sc.ANSWER_FORMAT, "answerability": answerability, "text": text,
            "values": list(values), "gaps": list(gaps)}


def _value(name, value, handle, field="row.content_text", quote=None):
    cite = {"l1_handle": handle, "field_path": field, **({"quote": quote} if quote else {})}
    return {"name": name, "value": value, "citations": [cite]}


def _gated(answer, events, question="", prose="Here is the answer."):
    final = prose + "\n```json\n" + json.dumps(answer) + "\n```\n"
    text, report = runner.gate_final_reply(final, events, question)
    parsed, error = sc.parse_answer(text)
    return text, parsed, error, report


class FinalReplyGateTests(unittest.TestCase):
    def assert_states_only(self, text, parsed):
        """Neither the reply nor its answer text states a number no kept value states."""
        kept = [v["value"] for v in parsed["values"] if v.get("value") is not None]
        self.assertEqual(answer_gate.unbacked(parsed["text"], kept), [], parsed["text"])
        self.assertEqual(answer_gate.unbacked(sc._JSON_FENCE.sub(" ", text), kept), [], text)

    def test_a_value_reworded_after_the_final_refusal_is_withheld_never_restated(self):
        # F24-0053: the gate refused the value twice (repair, then abstained); the agent's
        # final message stated it reworded, with a quote, in its values and its text.
        refused = "app_quote status REJECTED/EXPIRED · rejected_at · rejection_reason"
        events = [_submit("repair", refused=[("Lost quotes are REJECTED or EXPIRED.", "")]),
                  _submit("abstained", refused=[("The lost stage is on app_quote.", refused)])]
        answer = _answer(
            "A lost quote is recorded on app_quote as status REJECTED or EXPIRED, with "
            "rejected_at and a free-text rejection_reason. No source states when it expires.",
            [_value("lost_quote_recording",
                    "app_quote status REJECTED/EXPIRED, rejected_at, rejection_reason",
                    DOC, quote="Lost: rejected or expired,app_quote,status REJECTED/EXPIRED"),
             {"name": "quote_expiry_rule", "value": None, "citations": []}],
            [{"state": "absent_in_source", "subject": "expiry rule",
              "reason": "no_expiry_rule", "concerns": "quote_expiry_rule"}])
        text, parsed, error, report = _gated(
            answer, events, "How is a lost quote recorded, and when does a quote expire?",
            prose="**Lost quote:** stage 13 puts it on `app_quote`: REJECTED or EXPIRED.")
        self.assertIsNone(error)
        self.assertTrue(report["rewritten"])
        self.assertEqual(report["withheld"], ["lost_quote_recording"])
        withheld = parsed["values"][0]
        self.assertEqual((withheld["value"], withheld.get("citations")), (None, None))
        self.assertIn({"state": "unknown", "subject": "lost_quote_recording",
                       "reason": runner.WITHHELD_REASON, "concerns": "lost_quote_recording"},
                      parsed["gaps"])
        # Named as withheld, never paraphrased back in: no refused word, quote or number.
        for word in ("REJECTED", "EXPIRED", "app_quote", "rejected_at", "13"):
            self.assertNotIn(word, text)
        # Sentence by sentence: the backed prose stays as written, the withheld list ends it.
        self.assertEqual(parsed["text"].splitlines(), [
            "No source states when it expires.",
            f"Withheld: lost_quote_recording ({answer_gate.WITHHELD_WHY})",
            answer_gate.WITHHELD_STATEMENTS])
        # The message around the block had nothing backed: only the withheld note is left.
        self.assertEqual(text.split("```json")[0].strip(), answer_gate.WITHHELD_STATEMENTS)
        self.assertEqual(parsed["answerability"], "partial")
        self.assertEqual((report["sentences"], report["removed_sentences"]), (3, 2))
        self.assert_states_only(text, parsed)

    def test_a_number_the_text_check_flagged_never_leaves_in_the_final_text(self):
        # F23-0056: the gate passed one claim; its text check flagged "Draft 6" (a number
        # the question says); the final text still wrote it, and a 12-stage list beside.
        events = [_submit("answered", passed=[("Blank bottle PO", MAP, "row.content_text")])]
        question = ("What does the Screening Process Map (Draft 6) propose, from blank "
                    "purchase order through GL?")
        answer = _answer("The map starts with a blank bottle PO. No Draft 6 label is visible.",
                         [_value("stage_one", "Blank bottle PO", MAP),
                          {"name": "towel_screening", "value": None}],
                         [{"state": "absent_in_source", "subject": "towel screening",
                           "reason": "no_matching_document", "concerns": "towel_screening"}])
        text, parsed, error, report = _gated(
            answer, events, question,
            prose="It is a 12-stage flow:\n1. Blank bottle PO.\n10. Receive at new cost.")
        self.assertIsNone(error)
        self.assertEqual(report["unbacked"][:2], ["6", "12"])
        self.assertEqual(report["withheld"], [])
        # The passed claim stays, with its citation; the flagged number is gone everywhere.
        self.assertEqual(parsed["values"][0], answer["values"][0])
        self.assertEqual(parsed["text"].splitlines(), [
            "The map starts with a blank bottle PO.", answer_gate.WITHHELD_STATEMENTS])
        self.assertNotRegex(text.split("```json")[0], r"\d")
        self.assertEqual(claim_checks.prose_numbers(parsed["text"], ["Blank bottle PO"]), [])
        self.assert_states_only(text, parsed)

    def test_a_reply_stating_only_passed_claims_and_the_question_leaves_as_written(self):
        events = [_submit("answered", passed=[("Blank bottle PO", MAP, "row.content_text"),
                                              ("2026-01-07", MAP, "row.due_date")])]
        answer = _answer("The screening map starts with a Blank bottle PO, due 2026-01-07; "
                         "the towel screening question is not answered by it.",
                         [_value("stage_one", "Blank bottle PO", MAP),
                          _value("due", "2026-01-07", MAP, "row.due_date")])
        final = fx.final_text(answer)
        text, report = runner.gate_final_reply(final, events, "What does the map say on "
                                                               "towel screening?")
        self.assertEqual(text, final)
        self.assertFalse(report["rewritten"])

    def test_fail_closed_no_decision_a_repair_left_or_an_unreadable_result_passes_nothing(self):
        answer = _answer("It is the Blank bottle PO.", [_value("stage_one", "Blank bottle PO",
                                                               MAP)], answerability="answerable")
        passed = _submit("answered", passed=[("Blank bottle PO", MAP, "row.content_text")])
        truncated = {**passed, "result": {"truncated_text": "{...", "original_chars": 9}}
        other_cell = _submit("answered", passed=[("Blank bottle PO", DOC, "row.content_text")])
        other_field = _submit("answered", passed=[("Blank bottle PO", MAP, "row.name")])
        failed = {**passed, "is_error": True}
        for name, events in (("never submitted", []), ("repair left", [_submit("repair")]),
                             ("truncated", [truncated]), ("other record", [other_cell]),
                             ("other field", [other_field]), ("call failed", [failed]),
                             ("passed then refused", [passed, _submit("abstained")])):
            with self.subTest(name):
                text, parsed, error, report = _gated(answer, events)
                self.assertIsNone(error)
                self.assertEqual(report["withheld"], ["stage_one"])
                self.assertIsNone(parsed["values"][0]["value"])
                # Withheld: no longer answerable; its prose states nothing unbacked.
                self.assertEqual(parsed["answerability"], "partial")
                self.assertEqual(parsed["text"].splitlines()[0], "It is the Blank bottle PO.")
        with self.subTest("passed"):
            text, parsed, _, report = _gated(answer, [passed])
            self.assertEqual((report["withheld"], parsed["values"][0]["value"]),
                             ([], "Blank bottle PO"))
        with self.subTest("a quote the gate did not check"):
            quoted = _answer("It is the Blank bottle PO.", [_value(
                "stage_one", "Blank bottle PO", MAP, quote="Blank bottle PO at cost")])
            _, parsed, _, report = _gated(quoted, [passed])
            self.assertEqual(report["withheld"], ["stage_one"])
        with self.subTest("a boolean or calculation the gate never stated"):
            computed = _answer("Equal.", [{"name": "same", "value": True, "calculation": {
                "op": "equal", "inputs": [{"l1_handle": MAP, "field_path": "row.content_text"},
                                          {"l1_handle": DOC, "field_path": "row.content_text"}]}}])
            _, parsed, _, report = _gated(computed, [passed])
            self.assertEqual(report["withheld"], ["same"])
        with self.subTest("no answer block: prose stating a number"):
            text, report = runner.gate_final_reply("The PO has 12 lines.", [passed])
            self.assertTrue(report["rewritten"])
            self.assertNotIn("12", text)
        self.assertEqual(answer_gate.unbacked(None, []), ["<reply unreadable>"])
        self.assertEqual(answer_gate.unbacked("x" * (answer_gate.MAX_REPLY_CHARS + 1), []),
                         ["<reply unreadable>"])

    def test_an_unknown_answer_key_is_dropped_and_the_answer_kept(self):
        # RM-05 (F25-0026): a "gaps_note" key beside the answer's keys made the reply
        # prose; its screen cut the json block and the card scored no_answer. The key is
        # dropped (its text is never checked, so never stated) and the answer gated as
        # any other.
        passed = _submit("answered", passed=[("Cancel", MAP, "row.status")])
        answer = {**_answer("The status is Cancel.", [_value("status", "Cancel", MAP,
                                                              "row.status")]),
                  "gaps_note": "No receipt for PO 349 was found in 12 mailboxes."}
        self.assertEqual(sc.parse_answer(fx.final_text(answer))[1], "answer_malformed:unknown_keys")
        text, parsed, error, report = _gated(answer, [passed])
        self.assertIsNone(error, text)
        self.assertNotIn("gaps_note", parsed)
        self.assertEqual(report["dropped_keys"], ["gaps_note"])
        self.assertEqual((parsed["values"][0]["value"], parsed["text"]),
                         ("Cancel", "The status is Cancel."))
        # Its note states a number no passed claim backs: screened out, never a gap.
        self.assertNotIn("mailboxes", text)
        self.assertEqual([g for g in parsed["gaps"] if g["reason"] == runner.NOTE_REASON], [])
        # A note the screen passes stays visible as an unknown gap; a non-text one drops.
        noted = {**answer, "gaps_note": "No receipt linked to this order was found.",
                 "extra": {"x": 1}}
        _, parsed, error, report = _gated(noted, [passed])
        self.assertIsNone(error)
        self.assertEqual(report["dropped_keys"], ["extra", "gaps_note"])
        self.assertEqual([g for g in parsed["gaps"] if g["reason"] == runner.NOTE_REASON],
                         [{"state": "unknown", "subject": "No receipt linked to this order "
                           "was found.", "reason": runner.NOTE_REASON}])
        # The keys kept are exactly the ones the scorer reads (``score.answer_problems``).
        self.assertEqual(sc.answer_problems({key: answer[key] for key in runner.ANSWER_KEYS}),
                         [])
        for key in ("note", "values_note", "Format"):
            with self.subTest(key=key):
                self.assertIn("unknown_keys", sc.answer_problems({**answer, key: "x"}))
        # Still malformed once the extra key is dropped (a bad value): no answer kept.
        broken = {**answer, "values": "Cancel"}
        text, parsed, error, report = _gated(broken, [passed])
        self.assertIsNone(parsed)
        self.assertNotIn("dropped_keys", report)
        # A reply already well formed reports nothing dropped.
        _, _, _, report = _gated(_answer("The status is Cancel.", [_value(
            "status", "Cancel", MAP, "row.status")]), [passed])
        self.assertNotIn("dropped_keys", report)

    def test_only_the_sentences_carrying_an_unbacked_statement_are_removed(self):
        claims = [{"id": "m1", "check": {"status": "passed"}, "text": "qty: 2400",
                   "phrase": "2400", "value_claims": [{"value": "2400", "citation": MAP,
                                                       "field": "row.qty"}]}]
        text = ("The order asks for 2400 units, as the line states. It shipped 2350 on "
                "2026-01-09; the carrier is late.\n1. Pick the blanks.\n2. Ship them.\n"
                "Nothing else is open on the order.")
        reply = answer_gate.final_reply(text, claims, question="How many units are ordered?")
        self.assertEqual(reply["text"].splitlines(), [
            "The order asks for 2400 units, as the line states. the carrier is late.",
            "Nothing else is open on the order.", answer_gate.WITHHELD_STATEMENTS])
        self.assertEqual((reply["sentences"], reply["removed_sentences"]), (6, 3))
        self.assertEqual(reply["unbacked"], ["2350", "2026-01-09", "1", "2"])
        clean = answer_gate.final_reply("It asks for 2400 units.", claims)
        self.assertEqual((clean["text"], clean["modified"]), ("It asks for 2400 units.", False))
        # Nothing backed survives: the passed claims' lines and the withheld list alone.
        gone = answer_gate.final_reply("It shipped 2350.", claims)
        self.assertEqual((gone["text"], gone["replaced"]),
                         ("qty: 2400\n" + answer_gate.WITHHELD_STATEMENTS, True))
        # Unknowns are screened the same way, each whole.
        screened = answer_gate.final_reply("It asks for 2400 units.", claims, unknowns=[
            "not in the record: the ship date", "not in the record: line 3 receipts",
            "not in the record: the REJECTED reason"], refused=["status REJECTED"])
        self.assertEqual((screened["unknowns"], screened["removed_unknowns"]),
                         (["not in the record: the ship date"], 2))

    def test_a_unit_word_after_a_figure_stays_only_when_the_gate_verified_it(self):
        def claim(value, unit=None, basis=None):
            return {"id": f"m{value}", "text": f"weight: {value}", "phrase": value,
                    "check": {"status": "passed", **({"unit_basis": basis} if basis else {})},
                    "value_claims": [{"value": value, "citation": DOC, "field": "text",
                                      **({"unit": unit} if unit else {})}]}

        # The page says "68,160 grams"; a claim in kg it printed no unit for is unverified.
        unverified = [claim("68160", "kg", "unverified"), claim("7"), claim("12")]
        for said, kept in (
                ("Net weight is 68,160 kg.", "Net weight is 68,160."),
                ("Net weight is 68,160 kilograms, by the page.", "Net weight is 68,160, by "
                                                                 "the page."),
                ("It is 68160 metric tons.", "It is 68160."),
                # A figure with no unit claimed loses a unit word too; a count word stays.
                ("Case 12 holds 7 cases.", "Case 12 holds 7."),
                ("Case 12 holds 7 units.", None),
                # A letter inside a token or before a period is no unit word.
                ("Pack 7 t-shirts in case 12.", None), ("Pack 7 l. per line 12.", None)):
            with self.subTest(said=said):
                reply = answer_gate.final_reply(said, unverified)
                self.assertEqual((reply["text"], reply["modified"]),
                                 (kept or said, kept is not None))
        # A verified unit stays in any spelling; another unit after the figure goes.
        verified = [claim("68160", "g", "page_text")]
        for said, kept in (("It is 68,160 grams.", None), ("It is 68,160 g.", None),
                           ("It is 68,160 kg.", "It is 68,160.")):
            with self.subTest(said=said):
                self.assertEqual(answer_gate.final_reply(said, verified)["text"], kept or said)
        # By value: the word that matches the verified unit stays, wherever it is; a
        # different unit word after the same value is not what the gate checked.
        mixed = answer_gate.final_reply("30 cases and 30 kg left.",
                                        [claim("30", "kg", "page_text")])
        self.assertEqual(mixed["text"], "30 and 30 kg left.")
        # The eval's final reply: the value kept without its unverified unit, and the
        # reply prose and answer text without the unit word.
        event = {"type": "tool_call", "name": "plx.case_get", "is_error": False,
                 "arguments": {"answer": {"claims": []}},
                 "result": {"outcome": "answered", "unsupported_claims": [],
                            "claims": [claim("68160", "kg", "unverified")]}}
        answer = _answer("The net weight is 68,160 kg.",
                         [{**_value("net_weight", "68160", DOC, "text"), "unit": "kg"}],
                         answerability="answerable")
        text, parsed, error, _report = _gated(answer, [event], prose="It weighs 68160 kg.")
        self.assertIsNone(error)
        self.assertNotIn("unit", parsed["values"][0])
        self.assertEqual(parsed["text"], "The net weight is 68,160.")
        self.assertEqual(text.split("```json")[0].strip(), "It weighs 68160.")
        # A unit the gate verified stays in the value and in both texts.
        event["result"]["claims"] = [claim("68160", "kg", "page_text")]
        text, parsed, error, _report = _gated(answer, [event], prose="It weighs 68160 kg.")
        self.assertEqual(parsed["values"][0]["unit"], "kg")
        self.assertEqual(text.split("```json")[0].strip(), "It weighs 68160 kg.")
        self.assertEqual(parsed["text"], "The net weight is 68,160 kg.")

    def test_a_currency_beside_a_value_is_the_one_the_gate_checked_or_none(self):
        # A review found a reply writing EUR beside a value the gate checked in USD.
        def claim(value, currency=None):
            return {"id": "m1", "text": f"total: {value}", "phrase": value,
                    "check": {"status": "passed"},
                    "value_claims": [{"value": value, "citation": DOC, "field": "text",
                                      **({"currency": currency} if currency else {})}]}

        usd = [claim("1260.50", "USD"), claim("3")]
        for said, kept in (("The total is 1,260.50 USD.", None),
                           ("The total is USD 1,260.50.", None),
                           ("The total is $1,260.50 for 3 lines.", None),   # a dollar: agrees
                           ("It is EUR 1,260.50. It has 3 lines.", "It has 3 lines."),
                           ("It is €1,260.50. It has 3 lines.", "It has 3 lines."),
                           ("It is 1,260.50 GBP. It has 3 lines.", "It has 3 lines.")):
            with self.subTest(said=said):
                reply = answer_gate.final_reply(said, usd)
                self.assertEqual(reply["text"].splitlines()[0], kept or said)
                self.assertEqual(reply["removed_sentences"], 0 if kept is None else 1)
        self.assertEqual(answer_gate.final_reply("It is EUR 1,260.50.", usd)["unbacked"],
                         ["EUR"])
        # A value the gate checked no currency for keeps its value, never a currency word;
        # another value's currency stays its own business.
        bare = answer_gate.final_reply("It is €1,260.50 and USD 3.", [claim("1260.50"),
                                                                     claim("3", "USD")])
        self.assertEqual((bare["text"], bare["removed_sentences"]),
                         ("It is 1,260.50 and USD 3.", 0))
        # The eval's final reply: the value keeps only the checked currency, and the prose
        # around it is refused where it writes another.
        event = {"type": "tool_call", "name": "plx.case_get", "is_error": False,
                 "arguments": {"answer": {"claims": []}},
                 "result": {"outcome": "answered", "unsupported_claims": [],
                            "claims": [claim("1260.50", "USD")]}}
        value = {**_value("total", "1260.50", DOC, "text"), "currency": "USD"}
        text, parsed, error, report = _gated(
            _answer("The total is 1,260.50 EUR.", [value], answerability="answerable"),
            [event], prose="It totals USD 1,260.50.")
        self.assertIsNone(error)
        self.assertEqual(parsed["values"][0]["currency"], "USD")
        self.assertNotIn("EUR", text)
        self.assertEqual(text.split("```json")[0].strip(), "It totals USD 1,260.50.")
        self.assertIn("EUR", report["unbacked"])
        # The scorer reads the same contradiction in an answer's text (``screen_prose``).
        screened = claim_checks.screen_prose("The total is 1,260.50 EUR.",
                                             [{"value": "1260.50", "currency": "USD"}])
        self.assertIn("claim_currency_mismatch", [f["code"] for f in screened])
        self.assertNotIn("claim_currency_mismatch", [f["code"] for f in (
            claim_checks.screen_prose("The total is 1,260.50 USD.",
                                      [{"value": "1260.50", "currency": "USD"}]))])

    def test_a_unit_dropped_from_a_value_leaves_the_prose_keyed_to_that_value_only(self):
        # The gate kept 120 without the claim's unit "ctns", a spelling no unit table
        # knows; the prose still wrote "120 ctns". Another value's word stays.
        event = {"type": "tool_call", "name": "plx.case_get", "is_error": False,
                 "arguments": {"answer": {"claims": []}},
                 "result": {"outcome": "answered", "unsupported_claims": [], "claims": [
                     {"id": "m1", "text": "count: 120", "phrase": "120",
                      "check": {"status": "passed", "unit_basis": "unverified"},
                      "value_claims": [{"value": "120", "citation": DOC, "field": "text",
                                        "unit": "ctns"}]},
                     {"id": "m2", "text": "count: 30", "phrase": "30",
                      "check": {"status": "passed"},
                      "value_claims": [{"value": "30", "citation": DOC, "field": "text"}]}]}}
        answer = _answer("The order holds 120 ctns and 30 ctns more.",
                         [{**_value("cartons", "120", DOC, "text"), "unit": "ctns"},
                          _value("extra", "30", DOC, "text")], answerability="answerable")
        text, parsed, error, _report = _gated(answer, [event], prose="It has 120 CTNS.")
        self.assertIsNone(error)
        self.assertNotIn("unit", parsed["values"][0])
        self.assertEqual(parsed["text"], "The order holds 120 and 30 ctns more.")
        self.assertEqual(text.split("```json")[0].strip(), "It has 120.")

    def test_a_label_is_one_token_and_a_checked_percent_backs_its_printed_percent(self):
        """GP-05: the screen read "Q9" as an unbacked 9 and "100%" (a claim of 100
        percent the gate passed) as 1.00, and dropped the attribution and the gap
        sentence. A letter-prefixed label (``claim_checks.LABEL``) the question names is
        the question restated; one it does not name is an unbacked label, written whole;
        a real unbacked quantity is still refused."""
        def claim(value, unit=None, basis="page_text"):
            return {"id": f"m{value}", "text": f"share: {value}", "phrase": value,
                    "check": {"status": "passed", "unit_basis": basis},
                    "value_claims": [{"value": value, "citation": DOC, "field": "text",
                                      **({"unit": unit} if unit else {})}]}

        question = "What did the Q7 review reject, and which cost rows did it affect?"
        text = ("The Q7 review rejected statement references. It asked that AI charges be "
                "100% project-dedicated. No individual Q7 cost row is identified. Q8 likely "
                "carried more rows. They cost about 3,300. Code P-01234 and F-26-0001 were "
                "flagged. It covered 1 row.")
        reply = answer_gate.final_reply(text, [claim("100", "percent")], question=question)
        self.assertEqual(reply["text"].splitlines(), [
            "The Q7 review rejected statement references. It asked that AI charges be 100% "
            "project-dedicated. No individual Q7 cost row is identified.",
            answer_gate.WITHHELD_STATEMENTS])
        self.assertEqual(reply["unbacked"], ["Q8", "3,300", "P-01234", "F-26-0001", "1"])
        # the question names a label on token boundaries only (Q7 is not Q70 or AQ7)
        for asked in ("What did the Q70 review reject?", "What did AQ7 reject?"):
            with self.subTest(asked=asked):
                self.assertEqual(answer_gate.unbacked("The Q7 review.", [], question=asked),
                                 ["Q7"])
        # a label whose numbers a passed value states stays backed, as before
        self.assertEqual(answer_gate.unbacked("The Q7 review.", ["7"]), [])
        # a percent is backed only by a passed claim in a percent unit the gate verified,
        # at percent scale: never by a unit-less 100, an unverified percent, or 1
        for claims in ([claim("100")], [claim("100", "percent", "unverified")],
                       [claim("1", "percent")], [claim("10000", "percent")]):
            with self.subTest(claims=claims):
                self.assertEqual(answer_gate.final_reply(
                    "It must be 100% dedicated.", claims)["unbacked"], ["100%"])
        # the eval's fallback line writes a checked percent as the percent it is
        event = {"type": "tool_call", "name": "plx.case_get", "is_error": False,
                 "arguments": {"answer": {"claims": []}},
                 "result": {"outcome": "answered", "unsupported_claims": [],
                            "claims": [claim("100", "percent")]}}
        answer = _answer("Q8 carried 3,300 of rows.",
                         [{**_value("share", "100", DOC, "text"), "unit": "percent"}])
        _text, parsed, error, _report = _gated(answer, [event], question)
        self.assertIsNone(error)
        self.assertEqual(parsed["text"].splitlines()[0], "share: 100%")

    def test_n_values_1_10_60_half_backed_none_lost_duplicated_or_slow(self):
        for count in (1, 10, 60):
            with self.subTest(count=count):
                passed = [(f"Value {chr(65 + n % 26)}{chr(65 + n // 26)}", MAP, f"row.c{n}")
                          for n in range(count) if n % 2 == 0]
                values = [_value(f"v{n}", f"Value {chr(65 + n % 26)}{chr(65 + n // 26)}", MAP,
                                 f"row.c{n}") for n in range(count)]
                started = time.monotonic()
                text, parsed, error, report = _gated(_answer("As checked.", values),
                                                     [_submit("answered", passed=passed)])
                self.assertLess(time.monotonic() - started, 5)
                self.assertIsNone(error)
                self.assertEqual([v["name"] for v in parsed["values"]],
                                 [f"v{n}" for n in range(count)])
                self.assertEqual([v["value"] is not None for v in parsed["values"]],
                                 [n % 2 == 0 for n in range(count)])
                self.assertEqual(report["withheld"],
                                 [f"v{n}" for n in range(count) if n % 2])
                withheld_gaps = [g["concerns"] for g in parsed["gaps"]
                                 if g["reason"] == runner.WITHHELD_REASON]
                self.assertEqual(withheld_gaps, report["withheld"])


PAGE = "original://co-1#page=10"
FIGURES = (("1,584.00", "| IN1 | 20615844 | 1,584.00 |"),
           ("8,316.00", "| IN2 | 20615852 | 8,316.00 |"))


def _expression_decision(value="9900", expression="x0 + x1", unit=None):
    """A final decision that passed one expression claim over two figures printed on
    ``PAGE`` (the recorded shape: the figure and quote as ``untrusted_text``)."""
    claim = {"value": _wrap(value), "op": "expression", "kind": "number",
             "expression": _wrap(expression), **({"unit": unit} if unit else {}),
             "of": [{"citation": PAGE, "field": "text", "value": _wrap(figure),
                     "quote": _wrap(quote)} for figure, quote in FIGURES]}
    return {"type": "tool_call", "name": "plx.case_get",
            "arguments": {"answer": {"claims": []}}, "is_error": False,
            "result": {"outcome": "answered", "answerability": {"state": "answerable"},
                       "unsupported_claims": [],
                       "claims": [{"id": "m1", "check": {"status": "passed", "failures": []},
                                   "citations": [_wrap(PAGE)],
                                   "text": _wrap(f"expression: {value}"),
                                   "value_claims": [claim]}]}}


AS_CHECKED = {"op": "expression", "expression": "x0 + x1", "inputs": [
    {"l1_handle": PAGE, "field_path": "text", "value": figure, "quote": quote}
    for figure, quote in FIGURES]}


class RestatedCalculationTests(unittest.TestCase):
    """v9 A03, A08, A11: the gate passed an expression; the final JSON stated its result as
    a plain cited figure and the reply withheld it. A01 added an unchecked role."""

    def test_a_cited_figure_the_gate_passed_as_an_expression_is_kept_as_its_calculation(self):
        stated = _value("total", "9900", PAGE, "text", quote=FIGURES[1][1])
        stated["unit"] = "USD"   # no unit the gate checked: dropped, never the value
        text, parsed, error, report = _gated(
            _answer("The statement totals 9900.", [stated], answerability="answerable"),
            [_expression_decision()])
        self.assertIsNone(error)
        self.assertEqual(report["withheld"], [])
        self.assertEqual(parsed["values"], [{"name": "total", "value": "9900",
                                             "calculation": AS_CHECKED}])
        self.assertEqual(parsed["answerability"], "answerable")
        self.assertIn("9900", parsed["text"])

    def test_a_restated_figure_is_withheld_when_anything_differs_from_the_checked_claim(self):
        role = _value("ordered", "9900", PAGE, "text")
        role["quantity_role"] = "ordered"
        for name, stated in (
                ("other value", _value("total", "9901", PAGE, "text")),
                ("cell outside the inputs", _value("total", "9900", MAP, "text")),
                ("one cell inside, one outside", {"name": "total", "value": "9900",
                 "citations": [{"l1_handle": PAGE, "field_path": "text"},
                               {"l1_handle": MAP, "field_path": "text"}]}),
                ("other field", _value("total", "9900", PAGE, "row.amount")),
                ("unchecked role", role)):
            with self.subTest(name):
                text, parsed, error, report = _gated(
                    _answer("As checked.", [stated], answerability="answerable"),
                    [_expression_decision()])
                self.assertEqual(report["withheld"], [stated["name"]])
                self.assertIsNone(parsed["values"][0]["value"])
        # No expression passed (a refused or repaired decision): nothing to restate.
        decision = _expression_decision()
        decision["result"]["outcome"] = "repair"
        text, parsed, error, report = _gated(
            _answer("As checked.", [_value("total", "9900", PAGE, "text")]), [decision])
        self.assertEqual(report["withheld"], ["total"])

    def test_a_unit_the_gate_verified_stays_on_the_restated_calculation(self):
        decision = _expression_decision(unit="USD")
        decision["result"]["claims"][0]["check"]["unit_basis"] = "printed"
        stated = _value("total", "9900", PAGE, "text")
        stated["unit"] = "USD"
        _, parsed, _, report = _gated(_answer("As checked.", [stated]), [decision])
        self.assertEqual(parsed["values"], [{"name": "total", "value": "9900", "unit": "USD",
                                             "calculation": AS_CHECKED}])

    def test_each_answer_value_the_gate_returns_is_kept_whole_by_the_final_reply(self):
        # The final decision's ready-to-copy form (``answer_values``): an expression as its
        # calculation, a cited passage with its quote, a count over its inputs; stated
        # as given (name added), the reply keeps each one unchanged.
        rows = ["api://public.t/%7B%22id%22%3A1%7D@1", "api://public.t/%7B%22id%22%3A2%7D@1"]
        decision = _expression_decision()
        claims = decision["result"]["claims"]
        claims.append({"id": "m2", "check": {"status": "passed", "failures": []},
                       "value_claims": [{"citation": DOC, "field": "row.content_text",
                                         "value": _wrap("Lot release for PO1"),
                                         "quote": _wrap("Lot release for PO1 signed")}]})
        claims.append({"id": "m3", "check": {"status": "passed", "failures": []},
                       "value_claims": [{"value": 2, "op": "count",
                                         "of": [{"citation": h} for h in rows]}]})
        ready = answer_gate.answer_values(decision["result"])
        self.assertEqual([entry["claim"] for entry in ready], ["m1", "m2", "m3"])
        self.assertEqual(ready[0]["answer_value"], {"value": "9900", "calculation": AS_CHECKED})
        self.assertEqual(ready[1]["answer_value"], {"value": "Lot release for PO1", "citations": [
            {"l1_handle": DOC, "field_path": "row.content_text",
             "quote": "Lot release for PO1 signed"}]})
        self.assertEqual(ready[2]["answer_value"], {"value": 2, "calculation": {
            "op": "count", "inputs": [{"l1_handle": h} for h in rows]}})
        values = [{"name": f"v{n}", **entry["answer_value"]} for n, entry in enumerate(ready)]
        _, parsed, _, report = _gated(_answer("As checked.", values, answerability="answerable"),
                                      [decision])
        self.assertEqual((parsed["values"], report["withheld"]), (values, []))
        # Not answered: no ready value.
        decision["result"]["outcome"] = "abstained"
        self.assertEqual(answer_gate.answer_values(decision["result"]), [])


class OneGapPerValueTests(unittest.TestCase):
    def test_a_second_gap_on_one_value_loses_its_concerns_and_stays(self):
        # AG-05: one null value, two gaps in two states (not_ingested and unknown), which
        # the scorer reads as contradictory. The first keeps concerns; none is dropped.
        gaps = [{"state": "not_ingested", "subject": "agenda map", "reason": "original_unheld",
                 "concerns": "agenda_lines"},
                {"state": "unknown", "subject": "Teams lane text", "reason": "not_projected",
                 "concerns": "agenda_lines"},
                {"state": "unknown", "subject": "owner", "reason": "absent", "concerns": "owner"},
                {"state": "stale", "subject": "source", "reason": "freshness"}]
        answer = _answer("No line traces.", [{"name": "agenda_lines", "value": None},
                                             {"name": "owner", "value": None}], gaps)
        text, parsed, error, report = _gated(answer, [_submit("answered", passed=[
            ("Lot release for PO1", DOC, "row.content_text")])])
        self.assertIsNone(error)
        self.assertEqual(parsed["gaps"], [gaps[0], {key: item for key, item in gaps[1].items()
                                                    if key != "concerns"}, gaps[2], gaps[3]])
        self.assertTrue(report["rewritten"])
        self.assertTrue(sc._contradictory(answer["gaps"], answer["values"]))
        self.assertFalse(sc._contradictory(parsed["gaps"], parsed["values"]))
        # Distinct values keep their own gap: the reply leaves the answer as written.
        clean = _answer("No line traces.", [{"name": "agenda_lines", "value": None}], gaps[:1])
        final = "Here.\n```json\n" + json.dumps(clean) + "\n```\n"
        self.assertEqual(runner.gate_final_reply(final, [])[0], final)


if __name__ == "__main__":
    unittest.main()
