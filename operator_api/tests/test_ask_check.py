"""The answer check (Ask plan A4 rev 2, ``operator_api.ask_check``): stage 0 lines, the trace
call's input and reply, the one repair message, never silent. The model's judgment is never
asserted: the call is a stand-in returning a fixed reply."""
import datetime as dt
import json
import time
import unittest
from types import SimpleNamespace

from operator_api import ask_check as c

RID = "sql://" + "a" * 20
TODAY = dt.date(2026, 11, 23)


def reply(claims):
    return json.dumps({"claims": claims})


def sql_call(n=1, rows=111, sql=None, extra=None, cells=None):
    profile = {"rows": rows, "exact": True, **(extra or {})}
    return {"type": "tool_call", "name": "plx.sql",
            "arguments": {"sql": sql or f"select count(*) from purchase_orders where n = {n}"},
            "result": json.dumps({"result": RID, "columns": ["n", "name"], "profile": profile,
                                  "rows": [{"cite": f"{RID}#row=0",
                                            "cells": cells or [111, "Apex"]}]})}


class Call:
    """A stand-in model call: records its prompts and replies as told."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        got = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(got, Exception):
            raise got
        return got


class Reply(unittest.TestCase):
    def test_a_good_reply_is_read_and_supported_statements_are_dropped(self):
        good = reply([{"claim": "109 open POs", "verdict": "contradicted", "cells": "111",
                       "read": 1, "would_settle": "count the whole set"},
                      {"claim": "Teams holds none", "verdict": "supported"}])
        got = c.parse(good)
        self.assertEqual([(x["claim"], x["verdict"], x["read"]) for x in got],
                         [("109 open POs", "contradicted", 1)])
        self.assertEqual(c.parse("```json\n" + good + "\n```"), got)
        self.assertEqual(c.parse(reply([])), [])

    def test_anything_else_is_unreadable_never_guessed(self):
        for bad in ("no object at all", "{not json}", '{"items": []}',
                    reply([{"claim": "x", "verdict": "maybe"}]),
                    reply([{"verdict": "contradicted"}]), reply(["x"])):
            with self.subTest(bad=bad), self.assertRaises(c.Unreadable):
                c.parse(bad)

    def test_an_unreadable_reply_is_retried_once_then_not_checked_and_logged(self):
        call = Call("garbage")
        with self.assertLogs("operator_api.ask", "WARNING") as logs:
            v = c.check(None, "q", "a", [], [sql_call()], call=call)
        self.assertEqual((v.status, v.why), ("not_checked", "could not read its reply"))
        self.assertEqual(len(call.prompts), 2)
        self.assertIn("ask_check_failed", "".join(logs.output))
        # the retry that reads: checked
        call = Call("garbage", reply([]))
        v = c.check(None, "q", "a", [], [sql_call()], call=call)
        self.assertEqual((v.status, len(call.prompts)), ("checked", 2))

    def test_the_check_has_no_time_limit_and_no_budget(self):
        for name in ("SECONDS", "BUDGET_SECONDS", "RETRY_LEAST", "TASK_MOST", "CUT_FLOOR",
                     "CUT_MARK"):
            self.assertFalse(hasattr(c, name), name)
        from operating import humanize
        seen = {}

        def run(argv, **kw):
            import subprocess
            seen.update(kw)
            return subprocess.CompletedProcess(argv, 0, '{"result": "{}"}', "")
        from unittest import mock
        with mock.patch.object(humanize.subprocess, "run", run):
            c.call_for(None)("p")
        self.assertIsNone(seen["timeout"])           # only a hung socket ends it

    def test_no_reply_or_a_failure_is_said_and_the_answer_stands(self):
        with self.assertLogs("operator_api.ask", "WARNING"):
            v = c.check(None, "q", "a", [], [sql_call()], call=Call(None))
        self.assertEqual((v.status, v.why), ("not_checked", "no reply"))
        with self.assertLogs("operator_api.ask", "WARNING") as logs:
            v = c.check(None, "q", "a", [], [sql_call()],
                        call=Call(RuntimeError("Secret Customer 4417")))
        self.assertEqual(v.status, "not_checked")
        self.assertIn("RuntimeError", v.why)
        self.assertNotIn("Secret", "".join(logs.output) + v.why)

    def test_a_stand_in_asker_with_no_check_call_is_not_checked_and_says_so(self):
        asker = type("A", (), {"_agent_for": staticmethod(lambda *a: None)})()
        v = c.check(asker, "q", "a", [], [sql_call()])
        self.assertEqual((v.status, v.why), ("not_checked", "no check call for this asker"))


class Repair(unittest.TestCase):
    def verdict(self):
        return c.Verdict("checked", c.parse(reply([
            {"claim": "108 of the 111 open POs have no date", "verdict": "contradicted",
             "cells": "109 of 111 (row 0)", "read": 1, "would_settle": "recount undated"},
            {"claim": "no Croda mail names PO100109", "verdict": "needs_cover",
             "cells": "LIMIT 40 reached", "read": 2, "would_settle": "count over all mail"}])))

    def test_the_repair_message_lists_each_claim_with_its_cells_and_what_would_settle_it(self):
        msg = c.repair_message(self.verdict())
        self.assertTrue(msg.startswith("A check of your answer against your own reads found "
                                       "these."))
        self.assertIn('- "108 of the 111 open POs have no date" | contradicted | the reads '
                      'hold: 109 of 111 (row 0) | to settle: recount undated', msg)
        self.assertIn("| needs_cover | the reads hold: LIMIT 40 reached | to settle: count "
                      "over all mail", msg)
        self.assertTrue(msg.endswith("Write the whole answer again."))
        self.assertIsNone(c.repair_message(c.Verdict("checked", [])))

    def test_the_lines_under_the_answer_are_off_until_a_run_passes(self):
        from unittest import mock
        v = self.verdict()
        self.assertFalse(c.SHOW_DISAGREES)
        self.assertEqual(c.disagreements(v, "108 of the 111 open POs have no date"), [])
        with mock.patch.object(c, "SHOW_DISAGREES", True):
            (d,) = c.disagreements(v, "Of all,  108 of the 111 Open POs have no date.")
            self.assertEqual(c.line(d), "Check: 108 of the 111 open POs have no date: the "
                                        "reads show 109 of 111 (row 0)")
            (n,) = c.disagreements(v, "no Croda mail names PO100109")
            self.assertEqual(c.line(n), "Not confirmed: no Croda mail names PO100109")


class Facts(unittest.TestCase):
    def test_each_reference_says_what_it_was_read_from_with_its_row(self):
        text = f"There are {{{{count({RID})}}}} open; the first is {{{{{RID}#row=0.name}}}}."
        lines = c.facts([sql_call(sql="select name from purchase_orders")], text, ["Deck: 7 x"],
                        TODAY)
        self.assertEqual(lines[0], f"111 = count({RID}) | {RID} = plx.sql statement: select "
                                   "name from purchase_orders")
        self.assertEqual(lines[1], f"Apex = {RID}#row=0.name | {RID} = plx.sql statement: "
                                   'select name from purchase_orders [row 0: n=111, name="Apex"]')
        self.assertIn(f"result {RID}: rows 111 (exact)", lines[2])
        self.assertEqual(lines[-1], "attached file: Deck: 7 x")

    def test_a_reached_limit_a_grouped_result_and_a_search_coverage_are_stated(self):
        e = sql_call(extra={"limit_note": "stopped at its own LIMIT 50: more rows may exist",
                            "grouped_by": ["supplier"], "total_over_groups": {"n": "109"}})
        search = {"type": "tool_call", "name": "plx.search", "arguments": {"query": "PO100109"},
                  "result": json.dumps({"coverage": {"matched": 51, "shown": 50}})}
        lines = c.facts([e, search], "text with no reference", [], TODAY)
        self.assertIn("LIMIT 50", lines[0])
        self.assertIn("grouped by supplier", lines[0])
        self.assertIn('total over groups {"n":"109"}', lines[0])
        self.assertIn('"matched":51', lines[1])

    def test_a_reference_that_does_not_resolve_is_not_a_line(self):
        self.assertEqual(c.facts([sql_call()], "{{count(sql://" + "b" * 20 + ")}}", [], TODAY),
                         [f"result {RID}: rows 111 (exact)"])

    def test_one_pass_over_1_10_60_references_loses_none(self):
        for n in (1, 10, 60):
            text = " ".join(f"{{{{{RID}#row=0.n}}}}" for _ in range(n))
            t0 = time.perf_counter()
            lines = c.facts([sql_call()], text, [], TODAY)
            self.assertEqual(sum(ln.startswith("111 = ") for ln in lines), n)
            self.assertLess(time.perf_counter() - t0, 2)


class Task(unittest.TestCase):
    def test_every_read_comes_with_its_whole_result_and_the_files_and_parts(self):
        parts = [{"part": "late POs", "sources": ["purchase_orders"], "period": "now",
                  "reads": 2, "found": 2}]
        t = c.task("how many?", "Two.", parts, [sql_call(1)], lines=["attached file: Deck 7"],
                   today=TODAY)
        self.assertIn("Today is 2026-11-23", t)
        self.assertIn("QUESTION:\nhow many?", t)
        self.assertIn("ANSWER:\nTwo.", t)
        self.assertIn("late POs: sources purchase_orders; period now; reads 2, found 2", t)
        self.assertIn("FILES:\nDeck 7", t)
        self.assertIn("READ 1: plx.sql", t)
        self.assertIn('"cells":[111,"Apex"]', t)          # the whole result, rows included
        self.assertIn('"profile":{"rows":111,"exact":true}', t)

    def test_reads_past_one_calls_context_go_in_several_parts_and_none_is_cut(self):
        big = {"type": "tool_call", "name": "plx.open", "arguments": {"citation": "m://1"},
               "result": json.dumps({"text": "x" * 50_000})}
        parts = c.reads([sql_call(), big, sql_call(2)], 20_000)
        text = "".join(b for p in parts for b in p)
        self.assertGreater(len(parts), 2)
        import re
        self.assertEqual(sum(len(m) for m in re.findall("x{100,}", text)), 50_000)   # every character is sent
        self.assertNotIn("cut]", text)
        self.assertIn("part 1 of 3 of this result", text)
        self.assertTrue(all(sum(len(b) for b in p) <= 20_000 for p in parts))
        self.assertIn("part 2 of", c.task("q", "a", [], [], part=parts[1], of=(2, len(parts))))
        self.assertIn("other reads are in the other", c.task("q", "a", [], [], part=parts[1],
                                                           of=(2, len(parts))))

    def test_one_call_a_part_the_claims_merged_and_a_missing_read_waits_for_its_part(self):
        events = [{**sql_call(i), "result": json.dumps({"result": RID, "text": "y" * 30_000})}
                  for i in range(3)]
        found = {"claim": "108 of 111", "verdict": "contradicted", "cells": "109"}
        missing = {"claim": "Croda mail", "verdict": "not_in_reads", "cells": ""}
        call = Call(reply([found, missing]), reply([]), reply([{**found, "claim": "x"}]))
        from unittest import mock
        with mock.patch.object(c, "CONTEXT_CHARS", 40_000):
            v = c.check(None, "q", "a", [], events, call=call, today=TODAY)
        self.assertEqual(len(call.prompts), 3)
        self.assertEqual(v.why, "read in 3 parts")
        # a contradiction stands wherever listed; a missing read only when every part lists it
        self.assertEqual(sorted(x["claim"] for x in v.claims), ["108 of 111", "x"])

    def test_reads_at_1_10_60_are_numbered_once_each_in_one_part(self):
        for n in (1, 10, 60):
            parts = c.reads([sql_call(i) for i in range(n)], 400_000)
            self.assertEqual(len(parts), 1)
            self.assertEqual(len(parts[0]), n)
            self.assertTrue(parts[0][-1].startswith(f"READ {n}: "))

    def test_the_call_reads_only_what_the_session_read(self):
        """No tools and no database: the prompt holds the session's own events and nothing
        else, so the check can never see more than the asker did."""
        call = Call(reply([]))
        c.check(None, "q", "a", [], [sql_call()], call=call, today=TODAY)
        (prompt,) = call.prompts
        self.assertTrue(prompt.startswith(c.TRACE_METHOD))
        self.assertIn("You have no\ntools.", c.TRACE_METHOD)
        self.assertIn("Apex", prompt)
        self.assertNotIn("Secret", prompt)


if __name__ == "__main__":
    unittest.main()
