"""The covering-read pass (operator_api.ask_cover)."""
import json
import unittest

from operator_api import ask_cover as ac, ask_patch


def sql_event(rows):
    result = {"result": "sql://" + "a" * 20, "scope": {"untrusted_text": "reads x; where a = 1; 2 of 2 rows shown"},
              "columns": ["a"], "rows": [{"cite": "c", "cells": ["SECRET-ROW-CELL"]}] * rows,
              "profile": {"rows": rows, "limit_hit": True,
                          "columns": [{"column": "a", "values": [{"cell": "x", "n": 2}]}]}}
    return {"type": "tool_call", "name": "plx.sql", "arguments": {"sql": "SELECT a FROM x WHERE a = 1 LIMIT 2"},
            "result": json.dumps(result)}


class CoverTests(unittest.TestCase):
    def test_the_check_sees_each_reads_scope_and_counts_never_its_rows(self):
        text = ac.task("q?", "Only two exist.", [sql_event(2)])
        self.assertIn("scope: reads x; where a = 1", text)
        self.assertIn("limit reached: True", text)
        self.assertIn('"x" x2', text)
        self.assertNotIn("SECRET-ROW-CELL", text)

    def test_the_prompt_is_the_probes_and_a_flag_of_another_kind_is_refused(self):
        self.assertIn("Find only these three kinds of statement", ac.PROMPT)
        with self.assertRaises(ac.Unreadable):
            ac.parse('{"flags": [{"kind": "D", "sentence": "s"}]}')
        with self.assertRaises(ac.Unreadable):
            ac.parse("no json")
        self.assertEqual(ac.parse('{"flags": []}'), [])

    def test_a_failed_call_is_not_checked_and_says_why(self):
        got = ac.check(object(), "q", "a", [sql_event(1)], call=lambda p: None)
        self.assertEqual((got.status, got.why), ("not_checked", "no reply"))
        got = ac.check(object(), "q", "a", [sql_event(1)], call=lambda p: "garbage")
        self.assertEqual((got.status, got.why), ("not_checked", "could not read its reply"))

    def test_a_flag_goes_back_as_one_turn_that_asks_for_the_whole_answer_again(self):
        printed = "Intro here. We have 7 open orders.\nTail."
        cover = ac.Cover("checked", [{"kind": "C", "sentence": "We have 7 open orders.",
                                      "why": "the read leaves out cancelled", "would_settle": "count without it"},
                                     {"kind": "A", "sentence": "Not in the answer.", "why": "x", "would_settle": ""}])
        flags = ac.sentences(printed, cover)
        # a quoted sentence the answer does not hold is never flagged on a guess
        self.assertEqual([f["sentence"] for f in flags], ["We have 7 open orders."])
        said = ac.message(flags)
        self.assertIn("write the complete answer again", said)
        self.assertIn("single word stands", said)
        self.assertIn("no mention of this check", said)

    def test_the_reply_is_the_answer_never_spliced_into_the_old_text(self):
        from dataclasses import dataclass, field
        from operator_api import simple_ask as sa

        @dataclass
        class Ran:
            final_text: str
            events: list = field(default_factory=list)
            usage: dict = field(default_factory=dict)

        first = Ran("Old answer. Both lines have no price.")
        # h17 (11-24): a reply that began "stands." was spliced into a bullet; now it IS the
        # whole reply or nothing
        kept = sa.covered(first, Ran("stands. Both of the lines have no price, so it holds."))
        self.assertEqual(kept.final_text, "Old answer. Both lines have no price.")
        self.assertEqual(sa.covered(first, Ran("stands")).final_text, first.final_text)
        self.assertEqual(sa.covered(first, Ran("")).final_text, first.final_text)
        new = sa.covered(first, Ran("New whole answer. Two lines have no price."))
        self.assertEqual(new.final_text, "New whole answer. Two lines have no price.")
        self.assertEqual(new.usage["cover_repair"], "rewritten")

    def test_a_patch_reply_that_starts_with_stands_keeps_the_sentence(self):
        written = "One. Two sentence here. Three."
        got = [ask_patch.Patch(5, 22, "Two sentence here.", "why")]
        done = ask_patch.apply(written, got, "1. stands. Both of the lines hold, so the sentence stands as written.")
        self.assertEqual((done.text, done.stood, done.replaced), (written, 1, 0))
        done = ask_patch.apply(written, got, "1. Standing orders are two.")
        self.assertEqual(done.replaced, 1)

    def test_message_requires_standalone_reply_without_revision_or_process_narration(self):
        flags = [{"kind": "A", "sentence": "None found.", "why": "limit reached",
                  "would_settle": "count all rows"}]
        said = ac.message(flags)
        self.assertIn("standalone reply", said)
        self.assertIn("for the first time", said)
        self.assertIn("never frame it as a revision, update, or diff", said)
        self.assertIn("search passes, cursors, filtering steps", said)

    def test_meta_framing_detects_revision_openings_and_ignores_clean_answers(self):
        for bad in (
            "I've rewritten the one-page Word memo. Three things changed:\n- item",
            "Short update: I've read last week's mail and Teams.",
            "**Updated answer:** Here are the open orders.",
            "Here is the corrected summary of open POs.",
            "Three things changed:\n1. First",
            "What changed: one figure was updated.",
        ):
            self.assertTrue(ac.meta_framing(bad), bad)
        for clean in (
            "",
            "**3 open orders remain.** All are priced in CAD.",
            "The invoice was updated on 21 Nov 2026 to 1,200.00 CAD.",
            "No matching sales order was found in the system.",
        ):
            self.assertFalse(ac.meta_framing(clean), clean)

    def test_covered_retry_replaces_only_when_non_empty_and_not_stands(self):
        from dataclasses import dataclass, field
        from operator_api import simple_ask as sa

        @dataclass
        class Ran:
            final_text: str
            events: list = field(default_factory=list)
            usage: dict = field(default_factory=dict)

        prev = Ran("I've rewritten the memo. Three things changed:\n- A",
                   usage={"cover_repair": "rewritten"})
        cleaned = sa.covered_retry(prev, Ran("Clean standalone answer."))
        self.assertEqual(cleaned.final_text, "Clean standalone answer.")
        self.assertEqual(cleaned.usage["cover_retry"], "rewritten")
        self.assertEqual(sa.covered_retry(prev, Ran("")).final_text, prev.final_text)
        self.assertEqual(sa.covered_retry(prev, Ran("stands")).final_text, prev.final_text)
        self.assertEqual(sa.covered_retry(prev, Ran("stands")).usage["cover_retry"], "kept")

    def test_warm_turn_retries_once_when_cover_rewrite_leaks_meta_framing(self):
        from dataclasses import dataclass, field
        from types import SimpleNamespace
        from unittest import mock
        from operator_api import simple_ask as sa

        @dataclass
        class FakeRun:
            final_text: str
            events: list = field(default_factory=list)
            usage: dict = field(default_factory=dict)

        class FakeSession:
            alive = True

            def __init__(self, replies):
                self.replies = list(replies)
                self.prompts = []

            def turn(self, prompt, on_line, cancel):
                self.prompts.append(prompt)
                return FakeRun(self.replies.pop(0))

            def close(self):
                self.alive = False

        def run_with(replies):
            warm = sa.WarmSessions()
            sess = FakeSession(replies)
            host = SimpleNamespace(close=lambda: None)
            asker = SimpleNamespace(
                _reader_key=lambda s: "r1",
                _scope_digest=lambda s: "d1",
                _start_digest=lambda s: "s1",
                _host_for=lambda s: host,
                _agent_for=lambda on: SimpleNamespace(open_session=lambda h: sess),
            )
            with mock.patch.object(sa, "spares_per_reader", return_value=0):
                ran = warm.turn(
                    asker, object(), "c1", "q?", (), lambda line: None, None,
                    check=lambda r, h: ("cover msg", sa.covered),
                )
            warm.close_all()
            return ran, sess.prompts

        ran, prompts = run_with([
            "Initial answer.",
            "I've rewritten the memo. Three things changed:\n- One",
            "Clean standalone answer.",
        ])
        self.assertEqual(ran.final_text, "Clean standalone answer.")
        self.assertEqual(ran.usage["cover_repair"], "rewritten")
        self.assertEqual(ran.usage["cover_retry"], "rewritten")
        self.assertEqual(len(prompts), 3)
        self.assertEqual(prompts[2], ac.STANDALONE_RETRY)

        # clean cover rewrite does not trigger a retry turn
        ran2, prompts2 = run_with(["Initial answer.", "Clean revised answer."])
        self.assertEqual(ran2.final_text, "Clean revised answer.")
        self.assertNotIn("cover_retry", ran2.usage)
        self.assertEqual(len(prompts2), 2)

    def test_single_flagged_sentence_triggers_targeted_patch_and_preserves_chart(self):
        """PLAN-A2: a flagged sentence (even containing {{refs}} and beside a chart block)
        must trigger ONLY a targeted 1-sentence patch, never a whole-answer rewrite."""
        from dataclasses import dataclass, field
        from types import SimpleNamespace
        from unittest import mock
        from operator_api import ask_check, simple_ask as sa

        rid = "sql://0123456789abcdef0123"
        chart_block = (
            '```chart\n'
            f'{{"type": "bar", "title": "2026 Monthly Revenue (CAD)", "result": "{rid}", '
            '"x": "month", "y": ["total"]}\n'
            '```'
        )
        sql_ev = {
            "type": "tool_call",
            "name": "plx.sql",
            "arguments": {"sql": "SELECT month, total FROM rev WHERE currency = 'CAD'"},
            "result": json.dumps({
                "result": rid,
                "columns": ["month", "total"],
                "rows": [
                    {"cite": "c1", "cells": ["2026-01", 100.0]},
                    {"cite": "c2", "cells": ["2026-02", 200.0]},
                ],
                "row_count": 2,
                "profile": {"rows": 2, "limit_hit": False, "columns": []},
            }),
        }
        written = (
            f"Monthly revenue for 2026 in CAD totals {{{{sum({rid}.total, CAD)}}}}. "
            f"All {{{{count({rid})}}}} months of 2026 are complete.\n\n{chart_block}"
        )

        @dataclass
        class FakeRun:
            final_text: str
            events: list = field(default_factory=list)
            usage: dict = field(default_factory=dict)

        emitted: list[tuple[str, dict]] = []
        cover_json = json.dumps({
            "flags": [{
                "kind": "A",
                "sentence": "All 2 months of 2026 are complete.",
                "why": "only Jan and Feb are in the read",
                "would_settle": "count distinct months in 2026",
            }]
        })
        asker = SimpleNamespace(_cover_call=lambda _prompt: cover_json)
        checking = sa.Checking(asker, "Plot monthly revenue for 2026 in CAD",
                               lambda k, d: emitted.append((k, dict(d))))
        first = FakeRun(final_text=written, events=[sql_ev], usage={"subtype": "success"})

        with mock.patch.dict("os.environ", {ask_check.ENABLE_ENV: "cover"}):
            got = checking.hook(first)

        self.assertIsNotNone(got)
        msg, join = got
        # Must use targeted sentence patch, NOT whole-answer rewrite
        self.assertIn('Reply with one line per number', msg)
        self.assertNotIn("write the complete answer again", msg)
        self.assertIn("Do not rewrite the rest of your answer or any charts", msg)
        # Validated chart event must be emitted BEFORE check starts (never starved by repair)
        event_kinds = [k for k, _ in emitted]
        self.assertIn("charts", event_kinds)
        self.assertLess(event_kinds.index("charts"), event_kinds.index("check"))
        chart_ev = next(d for k, d in emitted if k == "charts")
        self.assertEqual(len(chart_ev["charts"]), 1)
        self.assertEqual(chart_ev["charts"][0]["title"], "2026 Monthly Revenue (CAD)")

        # Apply targeted 1-sentence reply: only the flagged sentence changes; chart stays intact
        patched = join(
            first,
            FakeRun(final_text=f"1. Revenue covers {{{{count({rid})}}}} months of 2026 in CAD."),
        )
        self.assertIn("Monthly revenue for 2026 in CAD totals {{sum(", patched.final_text)
        self.assertIn("Revenue covers {{count(", patched.final_text)
        self.assertNotIn("All {{count(", patched.final_text)
        self.assertIn(chart_block, patched.final_text)
        self.assertEqual(patched.usage["cover_repair"], "patched")
        self.assertEqual(patched.usage["cover_patch"]["replaced"], 1)

    def test_patched_cover_handles_stands_meta_framing_and_timeout(self):
        from dataclasses import dataclass, field
        from operator_api import simple_ask as sa

        @dataclass
        class FakeRun:
            final_text: str
            events: list = field(default_factory=list)
            usage: dict = field(default_factory=dict)

        written = "Intro. All items are shipped. Tail."
        p = [ask_patch.Patch(7, 29, "All items are shipped.", "limit hit")]
        first = FakeRun(final_text=written, usage={"subtype": "success"})

        # Bare "stands" keeps original text cleanly
        kept = sa.patched_cover(first, FakeRun("stands. All rows were checked."), p, written)
        self.assertEqual(kept.final_text, written)
        self.assertEqual(kept.usage["cover_repair"], "stands")

        # Meta-framing in a numbered replacement is rejected (original sentence kept)
        meta = sa.patched_cover(first, FakeRun("1. Updated answer: Two items shipped."), p, written)
        self.assertEqual(meta.final_text, written)
        self.assertEqual(meta.usage["cover_repair"], "stands")

        # Timeout preserves original answer without modification
        timed = sa.patched_cover(first, FakeRun("", usage={"subtype": "timeout"}), p, written)
        self.assertEqual(timed.final_text, written)
        self.assertEqual(timed.usage["cover_repair"], "timeout")

    def test_warm_turn_bounds_cover_repair_timeout_and_keeps_answer(self):
        """A hung cover_repair turn must time out at cover_repair_seconds and preserve the
        already-complete answer instead of stalling for 60 s."""
        import threading
        from dataclasses import dataclass, field
        from types import SimpleNamespace
        from unittest import mock
        from operator_api import simple_ask as sa

        @dataclass
        class FakeRun:
            final_text: str
            events: list = field(default_factory=list)
            usage: dict = field(default_factory=dict)

        closed_proc = threading.Event()

        class HungRepairSession:
            alive = True

            def __init__(self):
                self.calls = 0
                self.state = {"process": SimpleNamespace(close=closed_proc.set)}

            def turn(self, prompt, on_line, cancel):
                self.calls += 1
                if self.calls == 1:
                    return FakeRun("Initial answer with chart.", usage={"subtype": "success"})
                # Simulate a hung repair turn waiting for timeout cancellation
                closed_proc.wait(timeout=2.0)
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("repair timed out")
                return FakeRun("Late rewrite")

            def close(self):
                self.alive = False

        warm = sa.WarmSessions()
        sess = HungRepairSession()
        host = SimpleNamespace(close=lambda: None)
        asker = SimpleNamespace(
            _reader_key=lambda s: "r1",
            _scope_digest=lambda s: "d1",
            _start_digest=lambda s: "s1",
            _host_for=lambda s: host,
            _agent_for=lambda on: SimpleNamespace(open_session=lambda h: sess),
        )
        with mock.patch.object(sa, "spares_per_reader", return_value=0), \
                mock.patch.dict("os.environ", {sa.COVER_REPAIR_SECONDS_ENV: "0.05"}):
            ran = warm.turn(
                asker, object(), "c1", "Plot revenue", (), lambda line: None, None,
                check=lambda r, h: ("patch msg", sa.covered),
            )
        warm.close_all()
        self.assertTrue(closed_proc.is_set())
        self.assertEqual(ran.final_text, "Initial answer with chart.")
        self.assertEqual(ran.usage["cover_repair"], "timeout")
        self.assertIn("cover_repair_seconds", ran.usage)

    def test_call_for_uses_low_effort_and_bounded_timeout(self):
        from unittest import mock

        captured = {}

        def fake_rewriter(**kwargs):
            captured.update(kwargs)
            return lambda prompt: '{"flags": []}'

        with mock.patch("operating.humanize.rewriter", side_effect=fake_rewriter), \
                mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop(ac.MODEL_ENV, None)
            ac.call_for(object())

        self.assertEqual(captured["effort"], "low")
        self.assertEqual(captured["seconds"], ac.SECONDS)
        self.assertEqual(captured["feature"], ac.FEATURE)
        self.assertEqual(captured["model"], "haiku")
        self.assertEqual(captured["system_prompt"], ac.PROMPT)
        self.assertEqual(captured["caller"], "operator_api.ask_cover")
        with mock.patch.dict("os.environ", {ac.MODEL_ENV: "sonnet"}):
            self.assertEqual(ac.model(), "sonnet")

    def test_needs_sync_check_skips_low_risk_and_triggers_on_risky_or_untraced_answers(self):
        complete_sql = {
            "type": "tool_call",
            "name": "plx.sql",
            "arguments": {"names": "Croda"},
            "result": json.dumps({
                "result": "sql://" + "b" * 20,
                "columns": ["kind", "n"],
                "rows": [{"cite": "c", "cells": ["supplier", "2"]}],
                "row_count": 1,
                "profile": {"rows": 1, "limit_hit": False, "columns": []},
            }),
        }
        # Plain factual lookup with complete reads and no untraced figures skips blocking sync cover
        sync, reason = ac.needs_sync_check("Croda Canada is listed as a supplier.", [complete_sql])
        self.assertFalse(sync)
        self.assertEqual(reason, "low_risk_reads_complete")

        # Non-empty untraced figures ALWAYS require synchronous check
        sync, reason = ac.needs_sync_check("Croda Canada has 2 records.", [complete_sql], untraced=["2"])
        self.assertTrue(sync)
        self.assertEqual(reason, "untraced_figures")

        # Limit-hit SQL ALWAYS requires synchronous check
        sync, reason = ac.needs_sync_check("Here are the orders.", [sql_event(2)])
        self.assertTrue(sync)
        self.assertEqual(reason, "sql_limit_or_truncated")

        # Widened meaning_continue cursor after exhaustive exact match does NOT trigger truncation sync
        widened_search = {
            "type": "tool_call",
            "name": "plx.search",
            "arguments": {"query": "20311 Terminal City"},
            "result": json.dumps({
                "documents": [],
                "searched": {"sources": ["graph_mail"], "widened": "also items holding some of the words"},
                "completeness": {
                    "complete": False,
                    "truncated": True,
                    "returned": 0,
                    "next_cursor": "m1:1:35:w",
                    "reason": "meaning_continue",
                },
            }),
        }
        sync, reason = ac.needs_sync_check("Here are the related orders.", [widened_search])
        self.assertFalse(sync)
        self.assertEqual(reason, "low_risk_reads_complete")

    def test_smart_cover_defers_low_risk_to_background_and_populates_claims_on_sync(self):
        from dataclasses import dataclass, field
        from types import SimpleNamespace
        from unittest import mock
        from operator_api import ask_check, simple_ask as sa

        @dataclass
        class FakeRun:
            final_text: str
            events: list = field(default_factory=list)
            usage: dict = field(default_factory=dict)

        complete_sql = {
            "type": "tool_call",
            "name": "plx.sql",
            "arguments": {"names": "Croda"},
            "result": json.dumps({
                "result": "sql://" + "c" * 20,
                "columns": ["kind", "n"],
                "rows": [{"cite": "c", "cells": ["supplier", "2"]}],
                "row_count": 1,
                "profile": {"rows": 1, "limit_hit": False, "columns": []},
            }),
        }
        bg_called = []
        asker = SimpleNamespace(_cover_call=lambda p: (bg_called.append(p) or '{"flags": []}'))
        emitted = []
        checking = sa.Checking(asker, "Who is Croda?", lambda k, d: emitted.append((k, d)))
        with mock.patch.dict("os.environ", {ask_check.ENABLE_ENV: "cover", ac.MODE_ENV: "smart"}):
            got = checking.hook(FakeRun("Croda Canada is a supplier.", events=[complete_sql]))
        self.assertIsNone(got)
        self.assertIsNotNone(checking.shadow)
        checking.shadow.join(timeout=2.0)
        self.assertEqual(len(bg_called), 1)
        self.assertEqual(checking.verdict.status, "checked")
        self.assertEqual(checking.verdict.seconds, 0.0)
        self.assertNotIn("check", [k for k, _ in emitted])

        # When sync runs and finds a flag, Verdict.claims is populated (no longer hardcoded 0)
        flag_json = json.dumps({
            "flags": [{
                "kind": "A",
                "sentence": "Only two exist.",
                "why": "limit reached",
                "would_settle": "count all rows",
            }]
        })
        asker2 = SimpleNamespace(_cover_call=lambda p: flag_json)
        checking2 = sa.Checking(asker2, "How many exist?", lambda k, d: None)
        with mock.patch.dict("os.environ", {ask_check.ENABLE_ENV: "cover", ac.MODE_ENV: "smart"}):
            got2 = checking2.hook(FakeRun("Only two exist.", events=[sql_event(2)]))
        self.assertIsNotNone(got2)
        self.assertEqual(len(checking2.verdict.claims), 1)
        self.assertEqual(checking2.verdict.payload()["claims"], 1)
        self.assertEqual(checking2.verdict.claims[0]["claim"], "Only two exist.")

    def test_cover_card_notes_widened_meaning_continue_and_untraced_figures(self):
        widened_ev = {
            "type": "tool_call",
            "name": "plx.search",
            "arguments": {"query": "20311 Terminal City", "since": "2026-11-01"},
            "result": json.dumps({
                "documents": [],
                "searched": {"sources": ["graph_mail"], "widened": "also items holding some words"},
                "completeness": {
                    "complete": False,
                    "truncated": True,
                    "returned": 0,
                    "next_cursor": "m1:1:35:w",
                    "reason": "meaning_continue",
                },
            }),
        }
        prompt = ac.task("Is PO 20311 in mail?", "No exact match.", [widened_ev], untraced=["20311"])
        self.assertIn("exact_matches_complete", prompt)
        self.assertIn("widened: also items holding some words", prompt)
        self.assertIn("UNTRACED FIGURES", prompt)
        self.assertIn("20311", prompt)


if __name__ == "__main__":
    unittest.main()
