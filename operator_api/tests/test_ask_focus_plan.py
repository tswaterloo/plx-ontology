"""Start on focus (A3 / S3) and streamed plan from tool calls (S5): warming a session on
Ask-box focus without any model call, immediate first-words plan at 0 ms, combined
plain-English plan line for multi-call turns, and immediate first-sentence streaming."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from operating.agent_session import AgentRun
from operator_api import ask_plan, simple_ask as sa


class PlanLineTest(unittest.TestCase):
    def test_initial_plan_names_records_and_entities_at_zero_ms(self):
        self.assertEqual(
            ask_plan.initial_plan("What is the status of PO100523 from Croda?"),
            "Reading PO100523 and Croda",
        )
        self.assertEqual(
            ask_plan.initial_plan("How many late purchase orders?"),
            "Checking the business records",
        )
        self.assertEqual(
            ask_plan.initial_plan("is this order late?", record="PurchaseOrder:bc:PO999"),
            "Reading PO999",
        )

    def test_calls_plan_combines_parallel_reads_into_one_readable_line(self):
        calls = [
            ("plx.sql", {"record": "PO100523"}),
            ("plx.sql", {"sql": "SELECT * FROM purchase_orders LIMIT 5"}),
            ("plx.search", {"query": "Croda", "sources": ["graph_mail"]}),
        ]
        self.assertEqual(
            ask_plan.calls_plan(calls),
            "Reading PO100523, purchase orders and mail for \u201cCroda\u201d",
        )

    def test_calls_plan_skips_legs_when_reads_run_in_same_turn_and_uses_legs_when_alone(self):
        with_reads = [
            ("plx.legs", {"legs": [{"part": "late POs", "sources": ["purchase_orders"]}]}),
            ("plx.sql", {"sql": "SELECT * FROM purchase_orders"}),
        ]
        self.assertEqual(ask_plan.calls_plan(with_reads), "Reading purchase orders")
        legs_only = [
            ("plx.legs", {"legs": [{"part": "late POs", "sources": ["purchase_orders"]},
                                   {"part": "Croda mail", "sources": ["graph_mail"]}]}),
        ]
        self.assertEqual(ask_plan.calls_plan(legs_only), "Checking late POs and Croda mail")


    def test_progress_plan_and_steps_never_leak_raw_uuids(self):
        uid = "a3b3038e-336f-f111-8070-002248ae586a"
        opaque = f'PurchaseOrder:bc.purchase_orders:{{"id":"{uid}"}}'
        plan_with_po = ask_plan.initial_plan("What is the status of PO 106001?", record=opaque)
        self.assertNotIn(uid, plan_with_po)
        self.assertIn("PO 106001", plan_with_po)
        plan_without_po = ask_plan.initial_plan("Is this order late?", record=opaque)
        self.assertEqual(plan_without_po, "Reading that purchase order")
        self.assertNotIn(uid, plan_without_po)
        self.assertNotIn(uid, ask_plan.calls_plan([("plx.sql", {"record": opaque})]) or "")
        self.assertNotIn(uid, ask_plan.calls_plan([("plx.sql", {"chain": uid})]) or "")
        self.assertNotIn(uid, ask_plan.calls_plan([("plx.sql", {"names": uid})]) or "")
        self.assertNotIn(uid, sa.call_step("plx.sql", {"record": opaque}))
        self.assertNotIn(uid, sa.call_step("plx.search", {"query": uid}))
        self.assertEqual(
            ask_plan.scrub_uuids(f'Reading "{uid}" and PO 106001'),
            "Reading that record and PO 106001",
        )


class StreamedPlanAndSentenceTest(unittest.TestCase):
    def test_multi_call_turn_attaches_combined_plan_and_keeps_call_events(self):
        msg = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "u1", "name": "mcp__plx__plx_sql",
             "input": {"record": "PO100523"}},
            {"type": "tool_use", "id": "u2", "name": "mcp__plx__plx_search",
             "input": {"query": "Croda", "sources": ["graph_mail"]}},
        ]}}

        class Agent:
            def __init__(self, on_line):
                self.on_line = on_line

            def run(self, prompt, host):
                self.on_line(json.dumps(msg))
                return AgentRun(events=[], final_text="Done.", usage={"subtype": "success"})

        emitted = []
        asker = SimpleNamespace(_host_for=lambda s: SimpleNamespace(),
                                _agent_for=lambda on: Agent(on))
        with mock.patch.object(sa, "warm_enabled", lambda: False):
            sa.run(asker, object(), "PO100523 Croda", emit=lambda k, d: emitted.append((k, d)))
        calls = [(k, d) for k, d in emitted if k == "call"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1].get("plan"),
                         "Reading PO100523 and mail for \u201cCroda\u201d")
        self.assertTrue(calls[1][1].get("batched"))

    def test_first_sentence_streams_immediately_even_when_split_inside_a_delta(self):
        emitted = []
        s = sa.Streamed(lambda k, d: emitted.append((k, d)), asked=[])
        # simulate a recent flush so only sentence-boundary detection can trigger flush
        s.at = 1e18
        s.line("", {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "PO 100523 is open. Next detail"}}})
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0][0], "text")
        self.assertIn("PO 100523 is open.", emitted[0][1]["text"])


class WarmStatusTest(unittest.TestCase):
    def test_warm_status_starts_background_session_and_reports_ready_state(self):
        warm = sa.WarmSessions()
        old = sa.WARM
        sa.WARM = warm
        try:
            class FakeSession:
                alive = True

                def close(self):
                    pass

            asker = SimpleNamespace(
                serves=lambda s: True,
                _reader_key=lambda s: "reader-focus",
                _start_digest=lambda s: "start-1",
                _host_for=lambda s: SimpleNamespace(close=lambda: None),
                _agent_for=lambda on=None: SimpleNamespace(
                    open_session=lambda h, prestart=False: FakeSession()),
            )
            backend = SimpleNamespace(agent_ask=asker)
            reader = SimpleNamespace(session=SimpleNamespace(canary=False))
            # prepare synchronously once to verify ready=True reporting
            self.assertTrue(warm.prepare(asker, reader.session, wait=True))
            status = sa.warm_status(backend, reader)
            self.assertTrue(status["ready"])
            self.assertFalse(status["warming"])
        finally:
            sa.WARM = old
            warm.close_all()


if __name__ == "__main__":
    unittest.main()
