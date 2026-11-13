"""The ingest runner keeps each capture child's stderr and, for a child it kills at its
timeout, where every thread was (2026-11-12: mail capture wrote nothing for a day; its
child was killed at the timeout each cycle and its stderr dropped, so nothing said
it sat in Graph folder listings). A mail run that stopped on a Graph outage is a failed
source run, so the capture alarm names it. Fake child processes, no network."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import unittest

from operating import ingest_forever as forever
from operating.ingest_forever import SourceSpec, summarize_mail

CHILD = textwrap.dedent("""
    import faulthandler, json, signal, sys, time
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    mode = sys.argv[1]
    print("opened 3 mailboxes", file=sys.stderr, flush=True)
    def waiting_on_graph():
        time.sleep(60)
    if mode == "hang":
        waiting_on_graph()
    reasons = ["source_unavailable", "time_budget"] if mode == "outage" else []
    print(json.dumps({"runs": [{"incomplete_reasons": reasons, "mailboxes": [{"folders": [
        {"complete": True, "resumed_from": "delta_url"},
        {"complete": False, "resumed_from": None, "reason": "time_budget"}]}]}],
        "requests_sent": 4, "blocked_upstream": []}))
""")


class ChildStderrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.script = self.state / "child.py"
        self.script.write_text(CHILD, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_child(self, mode: str, timeout: float = 30) -> dict:
        spec = SourceSpec("mail", "fake-source",
                          lambda state, options: [sys.executable, str(self.script), mode],
                          summarize_mail, {"timeout_seconds": "seconds"}, budget_field=None)
        return forever.run_source(spec, {"timeout_seconds": timeout}, self.state,
                                  {"PATH": os.environ.get("PATH", "")}, grace=0)

    def kept(self) -> str:
        path = self.state / "host" / "logs" / "mail.stderr"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        return path.read_text(encoding="utf-8")

    def test_a_child_killed_at_its_timeout_leaves_its_stderr_and_where_it_hung(self):
        result = self.run_child("hang", timeout=1.5)
        self.assertEqual((result["status"], result["error_code"]), ("failed", "child_timeout"))
        text = self.kept()
        self.assertIn("opened 3 mailboxes", text)
        self.assertIn("waiting_on_graph", text)   # the stack the runner asked for

    def test_a_finished_child_leaves_its_stderr(self):
        result = self.run_child("ok")
        self.assertEqual(result["status"], "partial")
        self.assertIn("opened 3 mailboxes", self.kept())

    def test_a_mail_run_stopped_by_a_graph_outage_is_a_failed_run(self):
        result = self.run_child("outage")
        self.assertEqual((result["status"], result["error_code"]),
                         ("failed", "source_unavailable"))
        runs = forever.SourceRuns(self.state)
        runs.record("mail", result)
        runs.record("mail", result)
        row = json.loads((self.state / "host" / "source-runs.json")
                         .read_text())["sources"]["mail"]
        self.assertEqual((row["consecutive_failures"], row["failing_reason"]),
                         (2, "source_unavailable"))

if __name__ == "__main__":
    unittest.main()
