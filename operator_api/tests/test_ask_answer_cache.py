"""Watermark-keyed Ask answer cache (Ask spec 19 S8: same inputs, same answer).

Proves fail-closed freshness, per-view watermark invalidation, reader/door isolation,
mode/context/memory separation, SSE streaming provenance, and exclusion of follow-ups,
attachments, errors, refusals, side-effecting tools, and unreadable watermarks.
"""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from operating.agent_session import AgentRun
from operator_api import ask_answer_cache as ac
from operator_api import models as m
from operator_api import simple_ask as sa


def _sql_event(views=("purchase_orders",), cells=(("CAD", "1250.00", "4"),),
               as_of="2026-11-25T10:00:00+00:00", rid="sql://0123456789abcdef0123",
               is_error=False, extra_result=None, arguments=None):
    result = {
        "result": rid,
        "columns": ["currency", "total", "n"],
        "rows": [{"cite": f"{rid}#row={i}", "cells": list(c)} for i, c in enumerate(cells)],
        "row_count": len(cells),
        "as_of": as_of,
    }
    if views is not None:
        result["views"] = list(views)
    if extra_result:
        result.update(extra_result)
    ev = {
        "type": "tool_call",
        "name": "plx.sql",
        "is_error": is_error,
        "result": result,
    }
    if arguments is not None:
        ev["arguments"] = dict(arguments)
    return ev


class _Harness:
    """In-process Ask test harness with controllable reader doors, watermarks, and model runs."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.watermarks: dict[str, object] = {
            "view:purchase_orders": {"watermark": 100, "last_ok_at": "2026-11-25T10:00:00+00:00"},
            "view:suppliers": {"watermark": 200, "last_ok_at": "2026-11-25T10:00:00+00:00"},
            "view:sales_orders": {"watermark": 300, "last_ok_at": "2026-11-25T10:00:00+00:00"},
            "view:emails": {"watermark": 400, "last_ok_at": "2026-11-25T10:00:00+00:00"},
            "view:attachments": {"watermark": 401, "last_ok_at": "2026-11-25T10:00:00+00:00"},
            "view:teams_messages": {"watermark": 402, "last_ok_at": "2026-11-25T10:00:00+00:00"},
            "view:records": {"watermark": 403, "last_ok_at": "2026-11-25T10:00:00+00:00"},
            "view:record_links": {"watermark": 404, "last_ok_at": "2026-11-25T10:00:00+00:00"},
            "assist:matters": "2026-11-25T10:05:00+00:00",
            "assist:notes_and_optin": {"notes": [], "shown": True},
            "assist:readings": {"card_readings": ["2026-11-25T10:00:00+00:00", 5]},
            "store:data_as_of": {"vs": [400, "2026-11-25T10:00:00+00:00"]},
        }
        self.doors: dict[str, str] = {"alice": "door-alice-1", "bob": "door-bob-1"}
        self.comms_scope: dict[str, object] = {
            "teams_windows": {"teams:chat:c1": "-infinity"},
            "transcript_reader": {"full": False, "ids": []},
            "hidden_docs": (),
        }
        self.code_digest = "code-v1"
        self.fail_watermarks = False
        self.fail_door = False
        self.fail_comms = False
        self.responses: list[AgentRun] = []
        self.cache = ac.AnswerCache()

    def _next_run(self) -> AgentRun:
        if self.responses:
            return self.responses.pop(0)
        return AgentRun(
            events=[_sql_event(("purchase_orders",))],
            final_text="There are 4 open purchase orders totaling 1,250.00 CAD.",
            usage={"subtype": "success", "input_tokens": 500, "output_tokens": 40,
                   "cost_usd_estimate": 0.02},
        )

    def asker(self) -> SimpleNamespace:
        harness = self

        class FakeSession:
            alive = True

            def turn(self, prompt, on_line=None, cancel=None, **kw):
                harness.calls.append({"prompt": prompt, "kw": kw})
                return harness._next_run()

            def close(self):
                self.alive = False

        class FakeAgent:
            def run(self, prompt, host, **kw):
                harness.calls.append({"prompt": prompt, "kw": kw})
                return harness._next_run()

            def open_session(self, host, prestart=False):
                return FakeSession()

        def read_marks(sources):
            if harness.fail_watermarks:
                return None
            return {s: harness.watermarks.get(s) for s in sources}

        def read_door(session):
            if harness.fail_door:
                return None
            return harness.doors.get(getattr(session, "who", ""))

        def read_comms(session):
            if harness.fail_comms:
                return None
            return harness.comms_scope

        return SimpleNamespace(
            _host_for=lambda s: SimpleNamespace(close=lambda: None),
            _agent_for=lambda on: FakeAgent(),
            _reader_key=lambda s: f"reader:{s.who}",
            _scope_digest=lambda s: f"scope:{s.who}",
            _start_digest=lambda s: f"start:{s.who}",
            _door_mark=read_door,
            _comms_scope=read_comms,
            _code_digest=lambda: harness.code_digest,
            _watermarks=read_marks,
        )

    def run_ask(self, who="alice", question="How many open purchase orders?", **kwargs):
        session = kwargs.pop("session", SimpleNamespace(who=who))
        return sa.run(
            self.asker(),
            session,
            question,
            cache=self.cache,
            **kwargs,
        )


class RepeatHitAndWatermarkTest(unittest.TestCase):
    def test_repeat_question_with_identical_watermarks_returns_cached_instantly(self):
        h = _Harness()
        emitted_1: list[tuple[str, dict]] = []
        first = h.run_ask(
            question="How many open purchase orders?",
            emit=lambda k, d: emitted_1.append((k, dict(d))),
        )
        self.assertFalse(first.cached)
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(first.as_of, "2026-11-25T10:00:00+00:00")

        # Repeat with extra whitespace and different case: 0 new model calls!
        emitted_2: list[tuple[str, dict]] = []
        second = h.run_ask(
            question="  how   MANY open purchase orders?  ",
            emit=lambda k, d: emitted_2.append((k, dict(d))),
        )
        self.assertTrue(second.cached)
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(second.text, first.text)
        self.assertEqual(second.as_of, first.as_of)
        self.assertTrue(second.usage.get("cached"))
        self.assertEqual(second.usage.get("cost_usd_estimate"), 0.0)
        # Streaming callbacks receive provenance note + answer text cleanly
        kinds = [k for k, _ in emitted_2]
        self.assertIn("note", kinds)
        self.assertIn("text", kinds)
        note_text = next(d["text"] for k, d in emitted_2 if k == "note")
        self.assertIn("unchanged", note_text.lower())
        self.assertNotIn("cache", note_text.lower())
        self.assertNotIn("watermark", note_text.lower())
        # Payload surfaces as_of + plain provenance label
        payload = sa.answer_payload(second, "how many open purchase orders?", "c2", 1, "a" * 24)
        self.assertEqual(payload.freshness.as_of, "2026-11-25T10:00:00+00:00")
        self.assertIsNotNone(payload.freshness.label)
        self.assertIn("unchanged", payload.freshness.label.lower())
        self.assertNotIn("cache", payload.freshness.label.lower())
        # Mutating returned lists does not corrupt cached entry
        second.events.clear()
        third = h.run_ask(question="How many open purchase orders?")
        self.assertTrue(third.cached)
        self.assertEqual(len(third.events), 1)
        self.assertEqual(len(h.calls), 1)

    def test_moving_any_read_view_watermark_invalidates_and_reruns_live(self):
        h = _Harness()
        h.responses = [
            AgentRun(
                events=[_sql_event(("purchase_orders", "suppliers"))],
                final_text="4 open purchase orders.",
                usage={"subtype": "success"},
            ),
            AgentRun(
                events=[_sql_event(("purchase_orders", "suppliers"),
                                   cells=(("CAD", "2000.00", "5"),),
                                   as_of="2026-11-25T10:30:00+00:00")],
                final_text="5 open purchase orders.",
                usage={"subtype": "success"},
            ),
            AgentRun(
                events=[_sql_event(("purchase_orders", "suppliers"),
                                   cells=(("CAD", "2000.00", "5"),),
                                   as_of="2026-11-25T10:45:00+00:00")],
                final_text="5 open purchase orders (updated supplier).",
                usage={"subtype": "success"},
            ),
        ]
        r1 = h.run_ask()
        self.assertFalse(r1.cached)
        self.assertEqual(len(h.calls), 1)

        # Move purchase_orders watermark -> invalidates
        h.watermarks["view:purchase_orders"] = {"watermark": 101, "last_ok_at": "2026-11-25T10:30:00+00:00"}
        r2 = h.run_ask()
        self.assertFalse(r2.cached)
        self.assertEqual(len(h.calls), 2)
        self.assertIn("5 open", r2.text)

        # Move suppliers watermark (the other read view) -> invalidates
        h.watermarks["view:suppliers"] = {"watermark": 201, "last_ok_at": "2026-11-25T10:45:00+00:00"}
        r3 = h.run_ask()
        self.assertFalse(r3.cached)
        self.assertEqual(len(h.calls), 3)
        self.assertIn("updated supplier", r3.text)

    def test_moving_unread_view_still_hits_and_whole_store_invalidates(self):
        h = _Harness()
        # First answer reads ONLY purchase_orders
        r1 = h.run_ask(question="Open PO count?")
        self.assertFalse(r1.cached)
        self.assertEqual(len(h.calls), 1)

        # Move an UNREAD view (sales_orders and emails) -> STILL HITS!
        h.watermarks["view:sales_orders"] = {"watermark": 999}
        h.watermarks["view:emails"] = {"watermark": 999}
        r2 = h.run_ask(question="Open PO count?")
        self.assertTrue(r2.cached)
        self.assertEqual(len(h.calls), 1)

        # Second question uses plx.search (which reads whole-store data_as_of)
        h.responses = [
            AgentRun(
                events=[{
                    "type": "tool_call", "name": "plx.search", "is_error": False,
                    "result": {
                        "documents": [{"citation": "graph-mail://m/1", "snippet": {"untrusted_text": "Shipped"}}],
                        "coverage": {"sources": ["graph_mail"], "shown": 1, "matched": 1},
                    },
                }],
                final_text="Mail confirms it shipped.",
                usage={"subtype": "success"},
            ),
            AgentRun(
                events=[{
                    "type": "tool_call", "name": "plx.search", "is_error": False,
                    "result": {
                        "documents": [{"citation": "graph-mail://m/2", "snippet": {"untrusted_text": "Delayed"}}],
                        "coverage": {"sources": ["graph_mail"], "shown": 1, "matched": 1},
                    },
                }],
                final_text="Newer mail says delayed.",
                usage={"subtype": "success"},
            ),
        ]
        s1 = h.run_ask(question="Did it ship?")
        self.assertFalse(s1.cached)
        self.assertEqual(len(h.calls), 2)
        s2 = h.run_ask(question="Did it ship?")
        self.assertTrue(s2.cached)
        self.assertEqual(len(h.calls), 2)

        # Moving store:data_as_of invalidates search-backed answer cleanly
        h.watermarks["store:data_as_of"] = {"vs": [405, "2026-11-25T10:15:00+00:00"]}
        s3 = h.run_ask(question="Did it ship?")
        self.assertFalse(s3.cached)
        self.assertEqual(len(h.calls), 3)
        self.assertIn("delayed", s3.text)

    def test_open_work_tracks_assist_matters_heartbeat(self):
        h = _Harness()
        open_work_ev = {
            "type": "tool_call", "name": "plx.my_open_work", "is_error": False,
            "result": {"status": "ok", "as_of": "2026-11-25T10:05:00+00:00",
                       "matters_total": 1, "matters": [{"id": "m1", "kind": "task", "state": "open"}]},
        }
        h.responses = [
            AgentRun(events=[open_work_ev], final_text="You have 1 open matter.",
                     usage={"subtype": "success"}),
            AgentRun(events=[open_work_ev], final_text="You now have 1 updated matter.",
                     usage={"subtype": "success"}),
        ]
        r1 = h.run_ask(question="What is on my plate today?")
        self.assertFalse(r1.cached)
        r2 = h.run_ask(question="What is on my plate today?")
        self.assertTrue(r2.cached)
        self.assertEqual(len(h.calls), 1)
        # Heartbeat moves -> invalidates
        h.watermarks["assist:matters"] = "2026-11-25T10:10:00+00:00"
        r3 = h.run_ask(question="What is on my plate today?")
        self.assertFalse(r3.cached)
        self.assertEqual(len(h.calls), 2)


class PermissionsAndIsolationTest(unittest.TestCase):
    def test_different_reader_never_hits_another_readers_cache(self):
        h = _Harness()
        r_alice = h.run_ask(who="alice", question="Open POs?")
        self.assertFalse(r_alice.cached)
        self.assertEqual(len(h.calls), 1)

        # Bob asking the exact same question must NOT hit Alice's entry
        r_bob = h.run_ask(who="bob", question="Open POs?")
        self.assertFalse(r_bob.cached)
        self.assertEqual(len(h.calls), 2)

        # Alice still hits her own cached answer
        r_alice_2 = h.run_ask(who="alice", question="Open POs?")
        self.assertTrue(r_alice_2.cached)
        self.assertEqual(len(h.calls), 2)

    def test_changed_door_or_exclusion_mark_never_hits_cache(self):
        h = _Harness()
        r1 = h.run_ask(who="alice", question="Open POs?")
        self.assertFalse(r1.cached)
        self.assertEqual(len(h.calls), 1)

        # Simulate permission change / column marking / exclusion revocation moving door_mark
        h.doors["alice"] = "door-alice-revoked-2"
        r2 = h.run_ask(who="alice", question="Open POs?")
        self.assertFalse(r2.cached)
        self.assertEqual(len(h.calls), 2)

        # Unreadable door_mark fails closed (never serves cached answer)
        h.fail_door = True
        r3 = h.run_ask(who="alice", question="Open POs?")
        self.assertFalse(r3.cached)
        self.assertEqual(len(h.calls), 3)

    def test_resolve_door_mark_reflects_access_mark_exclusion_rule_and_revoked_epoch(self):
        reader = SimpleNamespace(
            mode="employee",
            session=SimpleNamespace(
                tenant="t1", principals=("u:alice",), clearance="std",
                access=None, service=None, view_as=False, canary=False,
            ),
        )
        state = {"access": "acc-1", "rule": "excl-rule-1", "db": (3, "excl-tag-1")}
        backend = SimpleNamespace(
            doors=SimpleNamespace(access_mark=lambda r: state["access"]),
            _door_db_stamp=lambda: state["db"],
        )
        asker = SimpleNamespace()
        with mock.patch("operator_api.door_value._rule_stamp", side_effect=lambda: state["rule"]):
            m0 = ac.resolve_door_mark(asker, reader.session, backend=backend, reader=reader)
            self.assertIsNotNone(m0)
            # Same state -> same mark
            self.assertEqual(m0, ac.resolve_door_mark(asker, reader.session, backend=backend, reader=reader))
            # Access mark moves -> different mark
            state["access"] = "acc-2"
            m1 = ac.resolve_door_mark(asker, reader.session, backend=backend, reader=reader)
            self.assertIsNotNone(m1)
            self.assertNotEqual(m0, m1)
            # Exclusion rule stamp moves -> different mark
            state["rule"] = "excl-rule-2"
            m2 = ac.resolve_door_mark(asker, reader.session, backend=backend, reader=reader)
            self.assertNotEqual(m1, m2)
            # DB door_state revoked epoch bumps -> different mark
            state["db"] = (4, "excl-tag-1")
            m3 = ac.resolve_door_mark(asker, reader.session, backend=backend, reader=reader)
            self.assertNotEqual(m2, m3)
            # Exclusion tag changes -> different mark
            state["db"] = (4, "excl-tag-2")
            m4 = ac.resolve_door_mark(asker, reader.session, backend=backend, reader=reader)
            self.assertNotEqual(m3, m4)
            # Missing access_mark or unreadable DB door -> None (fail closed)
            state["access"] = None
            self.assertIsNone(ac.resolve_door_mark(asker, reader.session, backend=backend, reader=reader))
            state["access"] = "acc-2"
            state["db"] = None
            self.assertIsNone(ac.resolve_door_mark(asker, reader.session, backend=backend, reader=reader))


class FailClosedBypassTest(unittest.TestCase):
    def test_follow_ups_attachments_refresh_canary_view_as_and_memory_instructions_bypass(self):
        h = _Harness()
        # Prime cache with clean single-turn answer
        r1 = h.run_ask(question="Open POs?")
        self.assertFalse(r1.cached)
        self.assertEqual(len(h.calls), 1)

        # Follow-up history bypasses cache
        hist = [SimpleNamespace(role="user", text="Earlier"),
                SimpleNamespace(role="assistant", text="Answer")]
        r_fu = h.run_ask(question="Open POs?", history=hist)
        self.assertFalse(r_fu.cached)
        self.assertEqual(len(h.calls), 2)

        # Attachments bypass cache
        fake_file = SimpleNamespace(name="quote.pdf", text="PO 123", unread=None, image=None)
        r_att = h.run_ask(question="Open POs?", files=[fake_file])
        self.assertFalse(r_att.cached)
        self.assertEqual(len(h.calls), 3)

        # Explicit refresh bypasses cache
        r_ref = h.run_ask(question="Open POs?", refresh=True)
        self.assertFalse(r_ref.cached)
        self.assertEqual(len(h.calls), 4)

        # Canary and view-as sessions bypass cache
        r_can = h.run_ask(question="Open POs?", session=SimpleNamespace(who="alice", canary=True))
        self.assertFalse(r_can.cached)
        r_va = h.run_ask(question="Open POs?", session=SimpleNamespace(who="alice", view_as=True))
        self.assertFalse(r_va.cached)
        self.assertEqual(len(h.calls), 6)

        # Memory instruction ("Remember that ...") bypasses cache
        r_mem = h.run_ask(question="Remember that our fiscal year starts Oct 1")
        self.assertFalse(r_mem.cached)
        r_mem2 = h.run_ask(question="Remember that our fiscal year starts Oct 1")
        self.assertFalse(r_mem2.cached)
        self.assertEqual(len(h.calls), 8)

        # Env toggle disables cache
        with mock.patch.dict(os.environ, {ac.ANSWER_CACHE_ENV: "0"}):
            r_off = h.run_ask(question="Open POs?")
            self.assertFalse(r_off.cached)
            self.assertEqual(len(h.calls), 9)

    def test_deep_record_context_memory_and_code_digest_separate_keys(self):
        h = _Harness()
        # Base run
        h.run_ask(question="Status?", deep=False, record="PO1001", context=["ctx1"], memory="mem1")
        self.assertEqual(len(h.calls), 1)
        # Same inputs -> hit
        hit = h.run_ask(question="Status?", deep=False, record="PO1001", context=["ctx1"], memory="mem1")
        self.assertTrue(hit.cached)
        self.assertEqual(len(h.calls), 1)

        # deep mismatch -> miss
        self.assertFalse(h.run_ask(question="Status?", deep=True, record="PO1001",
                                   context=["ctx1"], memory="mem1").cached)
        self.assertEqual(len(h.calls), 2)
        # record mismatch -> miss
        self.assertFalse(h.run_ask(question="Status?", deep=False, record="PO2002",
                                   context=["ctx1"], memory="mem1").cached)
        self.assertEqual(len(h.calls), 3)
        # context mismatch -> miss
        self.assertFalse(h.run_ask(question="Status?", deep=False, record="PO1001",
                                   context=["ctx2"], memory="mem1").cached)
        self.assertEqual(len(h.calls), 4)
        # memory mismatch -> miss
        self.assertFalse(h.run_ask(question="Status?", deep=False, record="PO1001",
                                   context=["ctx1"], memory="mem2").cached)
        self.assertEqual(len(h.calls), 5)
        # code/prompt digest change -> miss
        h.code_digest = "code-v2"
        self.assertFalse(h.run_ask(question="Status?", deep=False, record="PO1001",
                                   context=["ctx1"], memory="mem1").cached)
        self.assertEqual(len(h.calls), 6)

    def test_errors_refusals_untraced_clarify_gaps_and_side_effects_never_cache(self):
        bad_runs = [
            # Tool error flag
            AgentRun(events=[_sql_event(is_error=True)], final_text="Failed.",
                     usage={"subtype": "success"}),
            # Error object in result
            AgentRun(events=[_sql_event(extra_result={"error": {"code": "timeout", "message": "slow"}})],
                     final_text="Timed out.", usage={"subtype": "success"}),
            # Session timeout
            AgentRun(events=[_sql_event()], final_text="Partial.",
                     usage={"subtype": "timeout"}),
            # Untraced figure (999 not in result)
            AgentRun(events=[_sql_event()], final_text="There are 999 open purchase orders.",
                     usage={"subtype": "success"}),
            # Clarification block
            AgentRun(events=[_sql_event()],
                     final_text='```clarify\n{"question": "Which currency?", "options": ["CAD", "USD"]}\n```',
                     usage={"subtype": "success"}),
            # Gap block
            AgentRun(events=[_sql_event()],
                     final_text='No data.\n```gap\n{"missing": "freight cost", '
                                '"would_live": "carrier invoices"}\n```',
                     usage={"subtype": "success"}),
            # Side-effecting write tool
            AgentRun(events=[_sql_event(), {
                "type": "tool_call", "name": "plx.prepare_write", "is_error": False,
                "result": {"prepared_id": "pw_123456"},
            }], final_text="Prepared for confirm.", usage={"subtype": "success"}),
            # Zero evidence reads
            AgentRun(events=[], final_text="Hello, how can I help?",
                     usage={"subtype": "success"}),
            # Partial search gap (timeout/unfinished leg)
            AgentRun(events=[{
                "type": "tool_call", "name": "plx.search", "is_error": False,
                "result": {
                    "documents": [],
                    "coverage": {"sources": ["graph_mail"]},
                    "gaps": [{"reason": "timeout", "state": "failed"}],
                },
            }], final_text="Nothing found.", usage={"subtype": "success"}),
            # Open work tool not ok
            AgentRun(events=[{
                "type": "tool_call", "name": "plx.my_open_work", "is_error": False,
                "result": {"status": "stale", "reason": "behind", "matters": []},
            }], final_text="Store is stale.", usage={"subtype": "success"}),
        ]
        for idx, bad in enumerate(bad_runs):
            h = _Harness()
            h.responses = [bad]
            r1 = h.run_ask(question=f"Bad case {idx}?")
            self.assertFalse(r1.cached, f"case {idx} should not be cached")
            self.assertEqual(len(h.cache), 0, f"case {idx} must not populate cache")

    def test_unreadable_watermarks_and_mid_run_refresh_fail_closed(self):
        # 1. Unreadable watermarks on lookup fail closed (run live instead of serving stale)
        h = _Harness()
        r1 = h.run_ask(question="Open POs?")
        self.assertFalse(r1.cached)
        self.assertEqual(len(h.cache), 1)
        h.fail_watermarks = True
        r2 = h.run_ask(question="Open POs?")
        self.assertFalse(r2.cached)
        self.assertEqual(len(h.calls), 2)

        # 2. Unreadable watermarks on store never cache
        h2 = _Harness()
        h2.fail_watermarks = True
        h2.run_ask(question="Open POs?")
        self.assertEqual(len(h2.cache), 0)

        # 3. Missing watermark for one of the read views fails closed
        h3 = _Harness()
        del h3.watermarks["view:purchase_orders"]
        h3.run_ask(question="Open POs?")
        self.assertEqual(len(h3.cache), 0)

        # 4. Mid-run refresh race: view watermark moved while model ran -> do not store!
        h4 = _Harness()
        before = {"view:purchase_orders": ac._canonical_mark(h4.watermarks["view:purchase_orders"])}
        h4.watermarks["view:purchase_orders"] = {"watermark": 999}
        ran = sa.SimpleRun(
            text="4 open purchase orders.", agent_text="4 open purchase orders.",
            unverified=[], events=[_sql_event(("purchase_orders",))],
            seconds=1.0, stopped=False, failure=None,
            as_of="2026-11-25T10:00:00+00:00",
        )
        stored, why = h4.cache.put(
            "k1", reader_key="r1", door_mark="d1", ran=ran,
            asker=h4.asker(), before_marks=before,
        )
        self.assertFalse(stored)
        self.assertEqual(why, "moved_during_run")
        self.assertEqual(len(h4.cache), 0)


class StreamingSseCacheTest(unittest.TestCase):
    def test_simple_ask_sse_stream_returns_cached_answer_without_acquiring_session(self):
        h = _Harness()
        acquired = []

        class CountingSemaphore:
            def acquire(self, timeout=None):
                acquired.append("acquire")
                return True

            def release(self):
                acquired.append("release")

        asker = h.asker()

        def drain(events_q, future, seconds, cancel=None):
            while True:
                name, payload = events_q.get(timeout=5)
                if name == "done":
                    return
                yield name, payload

        backend = SimpleNamespace(
            agent_ask=asker,
            _sessions=CountingSemaphore(),
            _drain=drain,
            _memory_turn=lambda reader, req, conv, roots: ("", None),
            _clarified=lambda reader, req: req,
            memory=None,
        )
        reader = SimpleNamespace(key="reader:alice", session=SimpleNamespace(who="alice"))
        req = m.AskRequest(question="How many open purchase orders?")
        legs_ev = {
            "type": "tool_call", "name": "plx.legs", "is_error": False,
            "arguments": {"legs": [{"part": "open POs", "sources": ["purchase_orders"]}]},
            "result": {"legs": 1, "note": "Kept."},
        }
        h.responses = [
            AgentRun(
                events=[legs_ev, _sql_event(("purchase_orders",))],
                final_text="There are 4 open purchase orders totaling 1,250.00 CAD.",
                usage={"subtype": "success", "input_tokens": 500, "output_tokens": 40,
                       "cost_usd_estimate": 0.02},
            ),
        ]

        warm = sa.WarmSessions()
        old_warm = sa.WARM
        sa.WARM = warm
        try:
            # First call: live run, acquires session once, populates cache
            gen1 = sa.ask(backend, reader, req, "conv-1", lambda: 5, cache=h.cache)
            ev1 = []
            try:
                while True:
                    ev1.append(next(gen1))
            except StopIteration as done1:
                ans1 = done1.value
            self.assertEqual(len(h.calls), 1)
            self.assertEqual(acquired, ["acquire", "release"])
            self.assertEqual(ans1.outcome, "answered")
            self.assertIsNone(ans1.freshness.label)

            # Second call: instant SSE hit, 0 new model calls, 0 new session acquires!
            gen2 = sa.ask(backend, reader, req, "conv-2", lambda: 1, cache=h.cache)
            ev2 = []
            try:
                while True:
                    ev2.append(next(gen2))
            except StopIteration as done2:
                ans2 = done2.value
            self.assertEqual(len(h.calls), 1)
            self.assertEqual(acquired, ["acquire", "release"])
            self.assertEqual(ans2.direct_answer, ans1.direct_answer)
            self.assertEqual(ans2.freshness.as_of, "2026-11-25T10:00:00+00:00")
            self.assertIsNotNone(ans2.freshness.label)
            self.assertIn("unchanged", ans2.freshness.label.lower())
            self.assertEqual(len(ans2.coverage), len(ans1.coverage))
            # Emits checking status + token, NOT a false searching status
            steps = [p.step for k, p in ev2 if k == "status"]
            self.assertIn("checking", steps)
            self.assertNotIn("searching", steps)
            tokens = [p.text for k, p in ev2 if k == "token"]
            self.assertTrue(any(ans1.direct_answer in t for t in tokens))
        finally:
            sa.WARM = old_warm
            warm.close_all()


class PostgresWatermarkReaderTest(unittest.TestCase):
    def test_read_pg_watermarks_reads_view_state_state_matters_and_fails_closed(self):
        class FakeCursor:
            def __init__(self, rows):
                self._rows = rows

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def fetchall(self):
                return list(self._rows)

        class FakePg:
            def __init__(self, vs_rows, st_rows, heartbeat, readings_row=("2026-11-25T10:03:00+00:00", 7),
                         card_readings_present=True):
                self.vs_rows = vs_rows
                self.st_rows = st_rows
                self.heartbeat = heartbeat
                self.readings_row = readings_row
                self.card_readings_present = card_readings_present

            def execute(self, sql, params=()):
                s = " ".join(sql.split())
                if s.startswith("SELECT to_regclass"):
                    rel = params[0] if params else "ok"
                    if rel == "assist.card_readings" and not self.card_readings_present:
                        return FakeCursor([(None,)])
                    return FakeCursor([(rel,)])
                if "FROM biz.view_state WHERE view = ANY" in s:
                    want = set(params[0])
                    return FakeCursor([r for r in self.vs_rows if r[0] in want])
                if "FROM biz.state WHERE view = ANY" in s:
                    want = set(params[0])
                    return FakeCursor([r for r in self.st_rows if r[0] in want])
                if "FROM assist.matters_heartbeat" in s:
                    return FakeCursor([(self.heartbeat,)])
                if "FROM assist.card_readings" in s:
                    if isinstance(self.readings_row, Exception):
                        raise self.readings_row
                    return FakeCursor([self.readings_row])
                if "FROM assist.readings" in s:
                    if isinstance(self.readings_row, Exception):
                        raise self.readings_row
                    return FakeCursor([self.readings_row])
                if "FROM assist.reading_runs" in s:
                    return FakeCursor([self.readings_row])
                if "max(watermark)" in s:
                    return FakeCursor([(100, "2026-11-25T10:00:00+00:00")])
                if "max(built_at) FROM biz.state" in s:
                    return FakeCursor([("2026-11-25T10:00:00+00:00",)])
                if "FROM biz.comm_text_state" in s:
                    return FakeCursor([("2026-11-25T10:00:00+00:00", 55)])
                if "FROM ontology.load_state" in s:
                    return FakeCursor([("change_seq", "999")])
                return FakeCursor([])

        pg = FakePg(
            vs_rows=[("purchase_orders", 42, "2026-11-25T09:59:00+00:00",
                      "2026-11-25T10:00:00+00:00", "v1")],
            st_rows=[("purchase_orders", "2026-11-25T10:00:00+00:00", "d1"),
                     ("records", "2026-11-25T10:01:00+00:00", "d2")],
            heartbeat="2026-11-25T10:02:00+00:00",
        )
        got = ac._read_pg_watermarks(
            pg, ["purchase_orders", "records"], need_matters=True, need_store=True,
            need_readings=True,
        )
        self.assertIsNotNone(got)
        self.assertIn("view:purchase_orders", got)
        self.assertIn("view:records", got)
        self.assertIn("assist:matters", got)
        self.assertIn("assist:readings", got)
        self.assertIn("store:data_as_of", got)

        # Unreadable assist.card_readings fails closed (None)
        pg_bad_readings = FakePg(
            pg.vs_rows, pg.st_rows, pg.heartbeat,
            readings_row=RuntimeError("permission denied"),
        )
        self.assertIsNone(ac._read_pg_watermarks(
            pg_bad_readings, ["purchase_orders"], need_matters=False, need_store=False,
            need_readings=True,
        ))

        # snapshot_all_watermarks captures assist:readings cleanly
        class FakePool:
            def connection(self_inner):
                class CM:
                    def __enter__(self_cm): return pg
                    def __exit__(self_cm, *a): pass
                return CM()

        snap = ac.snapshot_all_watermarks(backend=SimpleNamespace(pg=lambda: FakePool()))
        self.assertIsNotNone(snap)
        self.assertEqual(snap["assist:readings"], got["assist:readings"])

        # Missing view watermark in both tables -> fails closed (None)
        missing = ac._read_pg_watermarks(
            pg, ["purchase_orders", "unknown_view"], need_matters=False, need_store=False,
        )
        self.assertIsNone(missing)

        # Missing heartbeat -> fails closed (None)
        pg_no_hb = FakePg(pg.vs_rows, pg.st_rows, None)
        self.assertIsNone(ac._read_pg_watermarks(
            pg_no_hb, ["purchase_orders"], need_matters=True, need_store=False,
        ))


class SecurityAndReviewFindingsTest(unittest.TestCase):
    def test_comms_access_rules_invalidate_teams_and_search_immediately_and_fail_closed(self):
        # 1. Real resolve_door_mark folds teams_windows, transcript_reader, and hidden docs
        reader = SimpleNamespace(
            mode="employee",
            session=SimpleNamespace(
                tenant="t1", principals=("u:alice",), clearance="std",
                access=None, service=None, view_as=False, canary=False,
                actor="actor-alice", held=(),
            ),
        )
        state = {
            "teams": {"teams:chat:c1": "-infinity", "teams:chat:c2": "2026-11-01T00:00:00Z"},
            "transcript": {"full": False, "ids": ["alice@plx.example"]},
            "withheld": (frozenset({"Attachment:1"}), frozenset({"orig://1"})),
            "own": frozenset({"SupplierInvoice:own1"}),
        }
        backend = SimpleNamespace(
            doors=SimpleNamespace(access_mark=lambda r: "acc-1"),
            _door_db_stamp=lambda: (3, "excl-tag-1"),
            runtime=SimpleNamespace(),
        )
        asker = SimpleNamespace()
        with mock.patch("operator_api.door_value._rule_stamp", return_value="excl-rule-1"), \
                mock.patch("operating.pg.business_comms.teams_windows",
                           side_effect=lambda rt, act: dict(state["teams"])), \
                mock.patch("operating.pg.business_comms.transcript_reader",
                           side_effect=lambda rt, act: dict(state["transcript"])), \
                mock.patch("operating.search.document_marks.withheld_set",
                           side_effect=lambda held: state["withheld"]), \
                mock.patch("operating.search.document_marks._own_issued",
                           side_effect=lambda: state["own"]):
            mark0, ok0 = ac.resolve_door_state(asker, reader.session, backend=backend, reader=reader)
            self.assertTrue(ok0)
            self.assertIsNotNone(mark0)

            # Removing user from a Teams chat changes teams_windows -> door_mark moves immediately
            state["teams"] = {"teams:chat:c1": "-infinity"}
            mark1, ok1 = ac.resolve_door_state(asker, reader.session, backend=backend, reader=reader)
            self.assertTrue(ok1)
            self.assertNotEqual(mark0, mark1)

            # Changing transcript_reader moves door_mark immediately
            state["transcript"] = {"full": True, "ids": ["alice@plx.example"]}
            mark2, ok2 = ac.resolve_door_state(asker, reader.session, backend=backend, reader=reader)
            self.assertTrue(ok2)
            self.assertNotEqual(mark1, mark2)

            # Changing hidden docs (even when door docs=False) moves door_mark immediately
            state["withheld"] = (frozenset({"Attachment:1", "Attachment:2"}), frozenset({"orig://1"}))
            mark3, ok3 = ac.resolve_door_state(asker, reader.session, backend=backend, reader=reader)
            self.assertTrue(ok3)
            self.assertNotEqual(mark2, mark3)

            # Search store unreadable -> comms_ok False (fail closed for comms)
            state["withheld"] = None
            mark4, ok4 = ac.resolve_door_state(asker, reader.session, backend=backend, reader=reader)
            self.assertFalse(ok4)
            self.assertIsNotNone(mark4)  # non-comms SQL can still resolve a door_mark

        # 2. Removing user from a Teams chat immediately invalidates cached Teams / search answer
        h = _Harness()
        teams_ev = _sql_event(("teams_messages",), cells=(("chat1", "hello", "1"),))
        search_ev = {
            "type": "tool_call", "name": "plx.search", "is_error": False,
            "result": {
                "documents": [{"citation": "teams-chat://c1/m1", "snippet": {"untrusted_text": "hello"}}],
                "coverage": {"sources": ["teams"], "shown": 1, "matched": 1},
            },
        }
        h.responses = [
            AgentRun(events=[teams_ev], final_text="Teams chat c1 had 1 message.",
                     usage={"subtype": "success"}),
            AgentRun(events=[search_ev], final_text="Search found Teams message saying hello.",
                     usage={"subtype": "success"}),
            AgentRun(events=[teams_ev], final_text="No Teams messages visible.",
                     usage={"subtype": "success"}),
        ]
        t1 = h.run_ask(question="What did Teams chat c1 say?")
        self.assertFalse(t1.cached)
        t2 = h.run_ask(question="What did Teams chat c1 say?")
        self.assertTrue(t2.cached)
        self.assertEqual(len(h.calls), 1)

        s1 = h.run_ask(question="Search Teams for hello")
        self.assertFalse(s1.cached)
        s2 = h.run_ask(question="Search Teams for hello")
        self.assertTrue(s2.cached)
        self.assertEqual(len(h.calls), 2)

        # User removed from teams:chat:c1 -> BOTH Teams and search answers immediately miss!
        h.comms_scope = {
            "teams_windows": {},
            "transcript_reader": {"full": False, "ids": []},
            "hidden_docs": (),
        }
        t3 = h.run_ask(question="What did Teams chat c1 say?")
        self.assertFalse(t3.cached)
        self.assertEqual(len(h.calls), 3)
        h.responses = [
            AgentRun(events=[search_ev], final_text="Updated search.",
                     usage={"subtype": "success"}),
        ]
        s3 = h.run_ask(question="Search Teams for hello")
        self.assertFalse(s3.cached)
        self.assertEqual(len(h.calls), 4)

        # 3. Fail closed when comms scope cannot be digested for Teams / transcripts / plx.search
        for idx, ev in enumerate([
            _sql_event(("teams_messages",)),
            _sql_event(("meeting_transcripts",)),
            search_ev,
        ]):
            h_bad = _Harness()
            h_bad.fail_comms = True
            h_bad.responses = [AgentRun(events=[ev], final_text="Answer.", usage={"subtype": "success"})]
            res = h_bad.run_ask(question=f"Comms unverified {idx}?")
            self.assertFalse(res.cached)
            self.assertEqual(len(h_bad.cache), 0, f"comms case {idx} must fail closed when unverified")

    def test_record_card_rejects_messages_unavailable_and_tracks_emails_and_teams(self):
        # 1. messages_unavailable in card -> rejected (uncacheable)
        h = _Harness()
        bad_card_ev = {
            "type": "tool_call", "name": "plx.sql", "is_error": False,
            "arguments": {"record": "PO1001"},
            "result": {
                "result": "sql://0123456789abcdef0123",
                "columns": ["hop", "direction", "handle"],
                "rows": [{"cite": "sql://0123456789abcdef0123#row=0",
                          "cells": ["0", "start", "api://po/1"]}],
                "row_count": 1,
                "views": ["purchase_orders"],
                "card": {"records_named": 1, "messages_unavailable": "not_ready: comms down"},
            },
        }
        h.responses = [AgentRun(events=[bad_card_ev], final_text="PO1001 is open.",
                                usage={"subtype": "success"})]
        r_bad = h.run_ask(question="Where is PO1001?")
        self.assertFalse(r_bad.cached)
        self.assertEqual(len(h.cache), 0)

        # 2. Clean record card tracks view:emails and view:teams_messages in addition to chain views
        good_card_ev = {
            "type": "tool_call", "name": "plx.sql", "is_error": False,
            "arguments": {"record": "PO1001"},
            "result": {
                "result": "sql://0123456789abcdef0123",
                "columns": ["hop", "direction", "handle"],
                "rows": [{"cite": "sql://0123456789abcdef0123#row=0",
                          "cells": ["0", "start", "api://po/1"]}],
                "row_count": 1,
                "views": ["purchase_orders"],
                "as_of": "2026-11-25T10:00:00+00:00",
                "card": {"records_named": 1, "messages": 2, "newest": []},
            },
        }
        h.responses = [
            AgentRun(events=[good_card_ev], final_text="PO1001 is open and has messages.",
                     usage={"subtype": "success"}),
            AgentRun(events=[good_card_ev], final_text="PO1001 has a new email.",
                     usage={"subtype": "success"}),
            AgentRun(events=[good_card_ev], final_text="PO1001 has a new Teams message.",
                     usage={"subtype": "success"}),
        ]
        c1 = h.run_ask(question="Where is PO1001?")
        self.assertFalse(c1.cached)
        self.assertEqual(len(h.cache), 1)
        c2 = h.run_ask(question="Where is PO1001?")
        self.assertTrue(c2.cached)
        self.assertEqual(len(h.calls), 2)
        self.assertIn("view:emails", c2.usage["cache_sources"])
        self.assertIn("view:teams_messages", c2.usage["cache_sources"])
        self.assertIn("view:purchase_orders", c2.usage["cache_sources"])

        # Moving view:emails invalidates record-card answer
        h.watermarks["view:emails"] = {"watermark": 405}
        c3 = h.run_ask(question="Where is PO1001?")
        self.assertFalse(c3.cached)
        self.assertEqual(len(h.calls), 3)

        # Moving view:teams_messages invalidates record-card answer
        h.watermarks["view:teams_messages"] = {"watermark": 406}
        c4 = h.run_ask(question="Where is PO1001?")
        self.assertFalse(c4.cached)
        self.assertEqual(len(h.calls), 4)

    def test_record_card_with_assist_readings_caches_invalidates_and_fails_closed(self):
        # 1. readings_unavailable in card -> rejected (uncacheable, fails closed)
        h = _Harness()
        unavail_card_ev = {
            "type": "tool_call", "name": "plx.sql", "is_error": False,
            "arguments": {"record": "PO1001"},
            "result": {
                "result": "sql://0123456789abcdef0123",
                "columns": ["hop", "direction", "handle"],
                "rows": [{"cite": "sql://0123456789abcdef0123#row=0",
                          "cells": ["0", "start", "api://po/1"]}],
                "row_count": 1,
                "views": ["records", "record_links", "emails", "teams_messages"],
                "as_of": "2026-11-25T10:00:00+00:00",
                "card": {
                    "records_named": 1,
                    "messages": 1,
                    "newest": [],
                    "readings_unavailable": "not_built: assist.readings is not built in this store",
                },
            },
        }
        h.responses = [AgentRun(events=[unavail_card_ev], final_text="PO1001 is open.",
                                usage={"subtype": "success"})]
        r_unavail = h.run_ask(question="Where is PO1001?")
        self.assertFalse(r_unavail.cached)
        self.assertEqual(len(h.cache), 0)

        # 2. Record card with readings_source / thread_state caches cleanly and tracks assist:readings
        readings_card_ev = {
            "type": "tool_call", "name": "plx.sql", "is_error": False,
            "arguments": {"record": "PO1001"},
            "result": {
                "result": "sql://0123456789abcdef0123",
                "columns": ["hop", "direction", "handle"],
                "rows": [{"cite": "sql://0123456789abcdef0123#row=0",
                          "cells": ["0", "start", "api://po/1"]}],
                "row_count": 1,
                "views": ["records", "record_links", "emails", "teams_messages"],
                "as_of": "2026-11-25T10:00:00+00:00",
                "card": {
                    "records_named": 1,
                    "messages": 1,
                    "newest": [],
                    "readings_source": "assist.readings",
                    "thread_state": {
                        "source": "assist.readings",
                        "messages_checked": 1,
                        "readings_found": 1,
                        "threads": [{"thread_key": "t1", "who_owes_whom": "they_owe_us"}],
                    },
                },
            },
        }
        h.responses = [
            AgentRun(events=[readings_card_ev],
                     final_text="PO1001 is waiting on Acme's ship date.",
                     usage={"subtype": "success"}),
            AgentRun(events=[readings_card_ev],
                     final_text="Acme replied with ship date Nov 28.",
                     usage={"subtype": "success"}),
        ]
        c1 = h.run_ask(question="Where is PO1001 and did they reply?")
        self.assertFalse(c1.cached)
        self.assertEqual(len(h.cache), 1)
        c2 = h.run_ask(question="Where is PO1001 and did they reply?")
        self.assertTrue(c2.cached)
        self.assertEqual(len(h.calls), 2)
        self.assertIn("assist:readings", c2.usage["cache_sources"])
        self.assertIn("view:emails", c2.usage["cache_sources"])
        self.assertIn("view:teams_messages", c2.usage["cache_sources"])

        # Moving assist:readings watermark invalidates cached card answer and re-runs live
        h.watermarks["assist:readings"] = {"card_readings": ["2026-11-25T10:15:00+00:00", 6]}
        c3 = h.run_ask(question="Where is PO1001 and did they reply?")
        self.assertFalse(c3.cached)
        self.assertEqual(len(h.calls), 3)
        self.assertIn("Acme replied", c3.text)

        # Unreadable assist:readings watermark fails closed (never serves cached answer)
        del h.watermarks["assist:readings"]
        c4 = h.run_ask(question="Where is PO1001 and did they reply?")
        self.assertFalse(c4.cached)
        self.assertEqual(len(h.calls), 4)

    def test_pg_prerunsnapshot_failure_refuses_to_cache(self):
        # Real PG snapshot failure returns None (not _HOOK_NO_SNAPSHOT) and put() refuses to cache
        failing_backend = SimpleNamespace(
            pg=lambda: mock.MagicMock(
                connection=mock.MagicMock(side_effect=RuntimeError("pg down"))
            )
        )
        snap = ac.snapshot_all_watermarks(asker=SimpleNamespace(), backend=failing_backend)
        self.assertIsNone(snap)

        h = _Harness()
        ran = sa.SimpleRun(
            text="4 open purchase orders.", agent_text="4 open purchase orders.",
            unverified=[], events=[_sql_event(("purchase_orders",))],
            seconds=1.0, stopped=False, failure=None,
            as_of="2026-11-25T10:00:00+00:00",
        )
        stored, why = h.cache.put(
            "k-pg-fail", reader_key="r1", door_mark="d1", ran=ran,
            asker=h.asker(), before_marks=snap,
        )
        self.assertFalse(stored)
        self.assertEqual(why, "pre_run_snapshot_failed")
        self.assertEqual(len(h.cache), 0)

        # _watermarks_all hook error also fails closed
        bad_hook_asker = SimpleNamespace(_watermarks_all=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertIsNone(ac.snapshot_all_watermarks(asker=bad_hook_asker))

    def test_open_work_notes_and_optin_changes_invalidate_and_fail_closed(self):
        state = {
            "notes": [{"id": 1, "kind": "fact", "at": "2026-11-25T09:00:00Z",
                       "text": "Already paid PO1001", "records": ["PurchaseOrder:1"]}],
            "optin": [{"person": "alice@plx.example", "flow": "reply", "on": True}],
            "notes_fail": False,
            "optin_fail": False,
        }

        class FakeMemory:
            def items(self, reader, limit=None, decide=None):
                if state["notes_fail"]:
                    raise OSError("memory down")
                return list(state["notes"])

        backend = SimpleNamespace(
            memory=FakeMemory(),
            _apa_state=lambda: None,
            _apa_rows=lambda s: (_ for _ in ()).throw(OSError("journal down"))
            if state["optin_fail"] else list(state["optin"]),
            _apa_me=lambda r: {"alice@plx.example"},
            _apa_pools=lambda r: (),
            _apa_is_owner=lambda r: False,
        )
        reader = SimpleNamespace(key="reader:alice", session=SimpleNamespace(who="alice"))

        with mock.patch("operating.autopilot_engine.load_draft", return_value={}), \
                mock.patch("operating.assist_matters.shown_to",
                           side_effect=lambda ids, rows, draft: (
                               None if rows is None else any(r.get("on") for r in rows))):
            m0 = ac._read_notes_and_optin(backend, reader)
            self.assertIsNotNone(m0)

            # Adding or editing a personal note moves the watermark
            state["notes"].append({"id": 2, "kind": "commitment", "at": "2026-11-25T10:00:00Z",
                                   "text": "Call Azelis Friday", "records": []})
            m1 = ac._read_notes_and_optin(backend, reader)
            self.assertIsNotNone(m1)
            self.assertNotEqual(m0, m1)

            state["notes"][1]["text"] = "Call Azelis Monday"
            m2 = ac._read_notes_and_optin(backend, reader)
            self.assertNotEqual(m1, m2)

            # Toggling opt-in moves the watermark
            state["optin"] = [{"person": "alice@plx.example", "flow": "reply", "on": False}]
            m3 = ac._read_notes_and_optin(backend, reader)
            self.assertIsNotNone(m3)
            self.assertNotEqual(m2, m3)

            # Unreadable notes or opt-in journal fails closed (None)
            state["notes_fail"] = True
            self.assertIsNone(ac._read_notes_and_optin(backend, reader))
            state["notes_fail"] = False
            state["optin_fail"] = True
            self.assertIsNone(ac._read_notes_and_optin(backend, reader))

        # End-to-end cache invalidation for plx.my_open_work via AnswerCache
        open_work_ev = {
            "type": "tool_call", "name": "plx.my_open_work", "is_error": False,
            "result": {"status": "ok", "as_of": "2026-11-25T10:05:00+00:00",
                       "matters_total": 1, "matters": [{"id": "m1", "kind": "task", "state": "open"}]},
        }
        h = _Harness()
        h.responses = [
            AgentRun(events=[open_work_ev], final_text="1 open matter, 1 note.",
                     usage={"subtype": "success"}),
            AgentRun(events=[open_work_ev], final_text="1 open matter, 2 notes.",
                     usage={"subtype": "success"}),
            AgentRun(events=[open_work_ev], final_text="Assist opted out.",
                     usage={"subtype": "success"}),
        ]
        w1 = h.run_ask(question="What is on my plate?")
        self.assertFalse(w1.cached)
        w2 = h.run_ask(question="What is on my plate?")
        self.assertTrue(w2.cached)
        self.assertIn("assist:notes_and_optin", w2.usage["cache_sources"])

        # Note added -> invalidates
        h.watermarks["assist:notes_and_optin"] = {"notes": ["n1", "n2"], "shown": True}
        w3 = h.run_ask(question="What is on my plate?")
        self.assertFalse(w3.cached)
        self.assertEqual(len(h.calls), 2)

        # Opt-in toggled -> invalidates
        h.watermarks["assist:notes_and_optin"] = {"notes": ["n1", "n2"], "shown": False}
        w4 = h.run_ask(question="What is on my plate?")
        self.assertFalse(w4.cached)
        self.assertEqual(len(h.calls), 3)

    def test_simple_ask_calls_brief_context_lines_once(self):
        h = _Harness()

        class CountingSemaphore:
            def acquire(self, timeout=None): return True
            def release(self): pass

        def drain(events_q, future, seconds, cancel=None):
            while True:
                name, payload = events_q.get(timeout=5)
                if name == "done":
                    return
                yield name, payload

        backend = SimpleNamespace(
            agent_ask=h.asker(),
            _sessions=CountingSemaphore(),
            _drain=drain,
            _memory_turn=lambda reader, req, conv, roots: ("", None),
            _clarified=lambda reader, req: req,
            memory=None,
        )
        reader = SimpleNamespace(key="reader:alice", session=SimpleNamespace(who="alice"))
        req = m.AskRequest(question="How many open purchase orders?")
        brief_calls = []

        warm = sa.WarmSessions()
        old_warm = sa.WARM
        sa.WARM = warm
        try:
            with mock.patch("operator_api.brief_context.lines",
                            side_effect=lambda b, r, a: brief_calls.append(1) or ["brief-1"]):
                gen = sa.ask(backend, reader, req, "conv-brief", lambda: 5, cache=h.cache)
                try:
                    while True:
                        next(gen)
                except StopIteration:
                    pass
            self.assertEqual(len(brief_calls), 1)
        finally:
            sa.WARM = old_warm
            warm.close_all()


if __name__ == "__main__":
    unittest.main()
