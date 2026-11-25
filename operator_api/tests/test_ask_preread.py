"""Pre-reads before the model's first turn (Ask spec 19 S2, A1): deterministic
``plx.sql`` name-finder and record-card reads placed in the result store before the
model writes its first token."""
from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from operating import session_prefetch
from operating.agent_session import AgentRun, HostGate, PrereadJob
from operator_api import ask_legs, ask_preread, ask_refs, simple_ask as sa


CARD_RES = {
    "result": "sql://11111111111111111111",
    "scope": "record PO100523; 1 of 1 rows shown",
    "columns": ["hop", "direction", "type", "handle", "label", "facts"],
    "rows": [{"cite": "sql://11111111111111111111#row=0",
              "cells": ["0", "start", "PurchaseOrder", "api://po/100523", "PO100523",
                        '{"status":"open","total":1250}']}],
    "row_count": 1,
    "profile": {"rows": 1, "shown": 1},
    "views": ["records", "record_links"],
    "as_of": "2026-11-24T18:00:00+00:00",
    "card": {"records_named": 1, "messages": 0},
}
NAMES_RES = {
    "result": "sql://22222222222222222222",
    "scope": "names: 'croda'; 1 of 1 rows shown",
    "columns": ["kind", "status", "blocked", "active", "n", "kind_total", "example"],
    "rows": [{"cite": "sql://22222222222222222222#row=0",
              "cells": ["supplier", "active", "(none)", "-", "1", "1", "Croda Canada"]}],
    "row_count": 1,
    "profile": {"rows": 1, "shown": 1},
    "views": ["suppliers"],
    "as_of": "2026-11-24T18:00:00+00:00",
}


class ReadsForTest(unittest.TestCase):
    def test_codes_and_names_produce_record_and_names_reads(self):
        reads = ask_preread.reads_for("What is the status of PO100523 from Croda?")
        self.assertIn(("plx.sql", {"record": "PO100523"}), reads)
        self.assertIn(("plx.sql", {"names": "Croda"}), reads)

    def test_context_record_is_included_without_duplication(self):
        reads = ask_preread.reads_for("is this late?", context_record="PurchaseOrder:bc:PO100523")
        self.assertEqual(reads, [("plx.sql", {"record": "PO100523", "type": "PurchaseOrder"})])
        reads_h = ask_preread.reads_for("status?", context_record="api://po/100523")
        self.assertEqual(reads_h, [("plx.sql", {"record": "api://po/100523"})])

    def test_general_question_with_no_names_runs_no_prereads(self):
        self.assertEqual(ask_preread.reads_for("how many late purchase orders are open right now?"), [])
        self.assertEqual(ask_preread.reads_for("   "), [])

    def test_prompt_section_summarizes_oversized_result_with_paging_pointer(self):
        big = {**CARD_RES, "rows": [{"cite": f"sql://11111111111111111111#row={i}",
                                     "cells": ["0", "start", "PurchaseOrder", "x" * 200, "PO1", "{}"]}
                                    for i in range(400)]}
        ev = {"type": "tool_call", "name": "plx.sql", "arguments": {"record": "PO100523"},
              "result": big, "is_error": False, "prefetched": True}
        sec = ask_preread.prompt_section([ev], max_chars=2000)
        self.assertIn("sql://11111111111111111111", sec)
        self.assertIn('"rows_in_store":true', sec)
        self.assertIn('"offset":0', sec)

    def test_error_and_not_found_prereads_are_filtered_out(self):
        bad = {"type": "tool_call", "name": "plx.sql", "arguments": {"record": "NOPE1"},
               "result": {"error": {"code": "not_found", "message": "missing"}},
               "is_error": False, "prefetched": True}
        ok = {"type": "tool_call", "name": "plx.sql", "arguments": {"names": "Croda"},
              "result": NAMES_RES, "is_error": False, "prefetched": True}
        self.assertEqual(len(ask_preread.valid_events([bad, ok])), 1)

    def test_reads_for_shares_one_definition_with_session_prefetch_and_has_no_regex(self):
        q = "What is the status of PO100523 from Croda?"
        self.assertEqual(ask_preread.reads_for(q), session_prefetch.sql_reads(q))
        src = Path(ask_preread.__file__).read_text(encoding="utf-8")
        self.assertNotIn("re.compile", src)

    def test_both_paths_see_the_same_bare_number_leads(self):
        q = "what is 100523?"
        first = session_prefetch.first_reads(q)
        sql = ask_preread.reads_for(q)
        find_queries = [a["query"] for n, a in first if n == "plx.find" and "query" in a]
        sql_records = [a["record"] for n, a in sql if n == "plx.sql" and "record" in a]
        self.assertEqual(find_queries, ["100523"])
        self.assertTrue(sql_records)
        self.assertTrue(any("100523" in r for r in sql_records), sql_records)

    def test_prompt_section_reports_failed_and_skipped_prereads_to_the_model(self):
        sec = ask_preread.prompt_section(
            [],
            failed=[("plx.sql", {"record": "NOPE1"}, "not_found: missing")],
            skipped=[("plx.sql", {"names": "Croda"}, "still running in background")],
        )
        self.assertIn("NOPE1", sec)
        self.assertIn("not_found: missing", sec)
        self.assertIn("Croda", sec)
        self.assertIn("still running in background", sec)


class NonBlockingAndCancelPrereadTest(unittest.TestCase):
    def test_slow_preread_runs_alongside_turn_without_serial_stall(self):
        script = textwrap.dedent("""
            import json, sys, time
            for line in sys.stdin:
                msg = json.loads(line)
                m = msg.get("method")
                if m == "initialize":
                    out = {"jsonrpc": "2.0", "id": msg["id"],
                           "result": {"protocolVersion": "2024-11-05", "capabilities": {},
                                      "serverInfo": {"name": "stub", "version": "1"}}}
                elif m == "tools/call":
                    args = msg["params"]["arguments"]
                    if args.get("names") == "SlowCo":
                        time.sleep(2.0)
                    body = {"result": "sql://aaaaaaaaaaaaaaaaaaaa", "columns": ["k"],
                            "rows": [{"cite": "sql://aaaaaaaaaaaaaaaaaaaa#row=0",
                                      "cells": [args.get("record") or args.get("names")]}],
                            "row_count": 1, "views": ["records"]}
                    out = {"jsonrpc": "2.0", "id": msg["id"],
                           "result": {"content": [{"type": "text", "text": json.dumps(body)}],
                                      "isError": False}}
                else:
                    continue
                sys.stdout.write(json.dumps(out) + "\\n")
                sys.stdout.flush()
        """)
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "stub.py"
            stub.write_text(script, encoding="utf-8")
            gate = HostGate(
                ["plx.sql"],
                {"command": sys.executable, "args": [str(stub)]},
                Path(tmp),
                log=Path(tmp) / "refused.jsonl",
                ready=True,
            )
            try:
                self.assertIsNone(gate.host_ready(5.0))
                t0 = time.monotonic()
                job = gate.start_reads([("plx.sql", {"record": "PO100523"}),
                                        ("plx.sql", {"names": "SlowCo"})])
                job.wait(0.25)
                elapsed = time.monotonic() - t0
                # returns right after the brief window instead of stalling for 2+ seconds
                self.assertLess(elapsed, 1.0)
                events, failed, skipped = job.snapshot()
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["arguments"], {"record": "PO100523"})
                self.assertEqual(failed, [])
                self.assertEqual(len(skipped), 1)
                self.assertEqual(skipped[0][1], {"names": "SlowCo"})
                self.assertIn("still running in background", skipped[0][2])
                # serve_gate lookup sees the in-flight SlowCo read and joins it
                joined = gate._lookup_preread("plx.sql", {"names": "SlowCo"})
                self.assertIsNotNone(joined)
                done_ev, get_raw = joined
                self.assertTrue(done_ev.wait(5.0))
                self.assertIsNotNone(get_raw())
            finally:
                gate.finish(None)

    def test_cancel_wakes_wait_immediately_and_skips_unstarted_reads(self):
        cancel = threading.Event()
        cancel.set()
        batch = ask_preread.start_on_session(
            SimpleNamespace(start_preread=lambda reads, cancel=None: self.fail("should not start")),
            [("plx.sql", {"record": "PO100523"})],
            cancel=cancel,
        )
        self.assertEqual(batch.events, [])
        self.assertEqual(len(batch.skipped), 1)
        self.assertIn("cancelled", batch.skipped[0][2])

        # mid-wait cancel wakes PrereadJob.wait immediately
        job = PrereadJob([("plx.sql", {"record": "PO100523"})])
        c2 = threading.Event()
        threading.Timer(0.05, c2.set).start()
        t0 = time.monotonic()
        finished = job.wait(5.0, cancel=c2)
        self.assertFalse(finished)
        self.assertLess(time.monotonic() - t0, 0.5)


class WarmPrereadTest(unittest.TestCase):
    def test_warm_turn_runs_prereads_before_first_turn_and_resolves_refs_without_legs_repair(self):
        seen = {}
        emitted: list[tuple[str, dict]] = []

        class FakeSession:
            alive = True

            def preread(self, reads):
                seen["prereads"] = list(reads)
                return [
                    {"seq": 0, "type": "tool_call", "name": "plx.sql",
                     "arguments": {"record": "PO100523"}, "result": CARD_RES,
                     "is_error": False, "prefetched": True},
                    {"seq": 1, "type": "tool_call", "name": "plx.sql",
                     "arguments": {"names": "Croda"}, "result": NAMES_RES,
                     "is_error": False, "prefetched": True},
                ]

            def turn(self, prompt, on_line=None, cancel=None, **kw):
                seen["prompt"] = prompt
                seen.setdefault("turns", 0)
                seen["turns"] += 1
                # simulate streaming a reference to the pre-read result on turn 1
                if on_line is not None:
                    on_line(json.dumps({"type": "stream_event", "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta",
                                  "text": "Found {{sql://22222222222222222222#row=0.example}}."}}}))
                    on_line(json.dumps({"type": "stream_event", "event": {
                        "type": "content_block_stop"}}))
                return AgentRun(
                    events=[],
                    final_text="PO100523 for {{sql://22222222222222222222#row=0.example}} "
                               "has {{count(sql://11111111111111111111)}} row.",
                    usage={"subtype": "success"})

            def close(self):
                pass

        warm = sa.WarmSessions()
        old = sa.WARM
        sa.WARM = warm
        try:
            asker = SimpleNamespace(
                _reader_key=lambda s: "r1",
                _scope_digest=lambda s: "d1",
                _start_digest=lambda s: "s1",
                _host_for=lambda s: SimpleNamespace(close=lambda: None),
                _agent_for=lambda on: SimpleNamespace(
                    open_session=lambda h, prestart=False: FakeSession()),
            )
            ran = sa.run(asker, object(), "Status of PO100523 from Croda?",
                         emit=lambda k, d: emitted.append((k, dict(d))),
                         conversation="conv-1", tools=[sa.legs_tool()])
        finally:
            sa.WARM = old
            warm.close_all()

        self.assertIn(("plx.sql", {"record": "PO100523"}), seen["prereads"])
        self.assertIn(("plx.sql", {"names": "Croda"}), seen["prereads"])
        self.assertIn(ask_preread.HEADING, seen["prompt"])
        self.assertIn("sql://11111111111111111111", seen["prompt"])
        self.assertIn("sql://22222222222222222222", seen["prompt"])
        # answered in 1 turn with no NO_LEGS repair triggered by prefetched reads
        self.assertEqual(seen["turns"], 1)
        self.assertEqual(ran.text, "PO100523 for Croda Canada has 1 row.")
        self.assertEqual(ran.unverified, [])
        # streamed text resolved the pre-read reference cleanly during turn 1
        streamed_texts = [d["text"] for k, d in emitted if k == "text"]
        self.assertTrue(any("Croda Canada" in t for t in streamed_texts), streamed_texts)
        # repair_part ignores prefetched-only events
        self.assertIsNone(ask_legs.repair_part(ask_legs.ledger(ran.events), ran.events))


class HostGateRunReadsTest(unittest.TestCase):
    def test_host_gate_run_reads_executes_before_client_connects_through_early_start(self):
        script = textwrap.dedent("""
            import json, sys
            for line in sys.stdin:
                msg = json.loads(line)
                m = msg.get("method")
                if m == "initialize":
                    out = {"jsonrpc": "2.0", "id": msg["id"],
                           "result": {"protocolVersion": "2024-11-05", "capabilities": {},
                                      "serverInfo": {"name": "stub", "version": "1"}}}
                elif m == "tools/call":
                    args = msg["params"]["arguments"]
                    body = {"result": "sql://aaaaaaaaaaaaaaaaaaaa", "columns": ["k"],
                            "rows": [{"cite": "sql://aaaaaaaaaaaaaaaaaaaa#row=0",
                                      "cells": [args.get("record") or args.get("names")]}],
                            "row_count": 1, "views": ["records"]}
                    out = {"jsonrpc": "2.0", "id": msg["id"],
                           "result": {"content": [{"type": "text", "text": json.dumps(body)}],
                                      "isError": False}}
                else:
                    continue
                sys.stdout.write(json.dumps(out) + "\\n")
                sys.stdout.flush()
        """)
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "stub.py"
            stub.write_text(script, encoding="utf-8")
            gate = HostGate(
                ["plx.sql"],
                {"command": sys.executable, "args": [str(stub)]},
                Path(tmp),
                log=Path(tmp) / "refused.jsonl",
                ready=True,
            )
            try:
                self.assertIsNone(gate.host_ready(5.0))
                events = gate.run_reads([("plx.sql", {"record": "PO100523"})])
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["name"], "plx.sql")
                self.assertTrue(events[0]["prefetched"])
                self.assertEqual(events[0]["result"]["result"], "sql://aaaaaaaaaaaaaaaaaaaa")
                self.assertEqual(events[0]["result"]["rows"][0]["cells"], ["PO100523"])
            finally:
                gate.finish(None)


if __name__ == "__main__":
    unittest.main()
