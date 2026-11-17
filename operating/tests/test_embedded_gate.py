"""AUD-13 (audit B #2): the follow report fails when the embedded share of any kind is
below 95%. Expected to fail until DI-06 (the GPU backfill) has run."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from operating.embed import follow


class Conn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=()):
        assert "FROM search.chunks GROUP BY kind" in sql
        return mock.Mock(fetchall=lambda: self.rows)

    def close(self):
        pass


AUDIT = [("attachment", 186686, 93700), ("mail", 555189, 438600), ("record_text", 7511, 7496),
         ("sharepoint", 341015, 26300), ("teams", 199866, 12400), ("tiny", 3, 0)]


class Gate(unittest.TestCase):
    def test_kinds_below_95_percent_fail_and_the_report_names_them(self):
        out = follow.embedded_gate(Conn(AUDIT))
        self.assertEqual(out["status"], "fail")
        self.assertEqual(out["failing"], ["attachment", "mail", "sharepoint", "teams"])
        self.assertEqual(out["by_kind"]["mail"]["share"], round(438600 / 555189, 4))
        self.assertNotIn("tiny", out["failing"])   # three rows are not judged
        self.assertEqual(out["by_kind"]["record_text"]["share"], round(7496 / 7511, 4))

    def test_every_judged_kind_at_95_percent_passes_and_just_below_fails(self):
        self.assertEqual(follow.embedded_gate(Conn([("mail", 100, 95)]))["status"], "pass")
        self.assertEqual(follow.embedded_gate(Conn([("mail", 100, 94)]))["status"], "fail")
        self.assertEqual(follow.embedded_gate(Conn([]))["status"], "pass")   # nothing indexed

    def test_an_unreadable_share_is_not_a_pass(self):
        with mock.patch.object(follow.st, "connect", side_effect=OSError("down")):
            out = follow._embedded_gate({})
        self.assertEqual(out, {"status": "unknown", "error": "OSError"})
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(follow.st, "connect", side_effect=OSError("down")):
            out = follow._embedded_gate({}, Path(tmp))
            self.assertFalse((Path(tmp) / follow.EMBEDDED_GATE_FILE).exists())
        self.assertEqual(out, {"status": "unknown", "error": "OSError"})


class Cached(unittest.TestCase):
    def test_the_scan_runs_at_most_once_an_hour_unless_forced(self):
        import tempfile
        from pathlib import Path
        clock = [1000.0]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(follow.st, "connect", side_effect=lambda dsn: Conn(AUDIT)) as c:
            state = Path(tmp)
            first = follow._embedded_gate({}, state, now=lambda: clock[0])
            clock[0] += 3599
            again = follow._embedded_gate({}, state, now=lambda: clock[0])
            self.assertEqual(c.call_count, 1)
            self.assertEqual((first.get("cached"), again["cached"], again["status"]),
                             (None, True, "fail"))
            clock[0] += 2
            follow._embedded_gate({}, state, now=lambda: clock[0])
            self.assertEqual(c.call_count, 2)
            follow._embedded_gate({}, state, force=True, now=lambda: clock[0])
            self.assertEqual(c.call_count, 3)


class InTheReport(unittest.TestCase):
    def run_main(self, rows, *extra, mode="layers"):
        with mock.patch.object(follow, "served", return_value={"generation": "g"}), \
                mock.patch.object(follow.config, "settings", return_value={}), \
                mock.patch.object(follow, "_exclusion",
                                  return_value=mock.Mock(unavailable=None)), \
                mock.patch.object(follow, "layers_follow"), \
                mock.patch.object(follow, "generation_follow"), \
                mock.patch.object(follow.st, "connect", return_value=Conn(rows)), \
                mock.patch("ontology.window_lock.skip_report", return_value=None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = follow.main(["--state", "/nonexistent", "--mode", mode, *extra])
        return code, json.loads(buf.getvalue())

    def test_the_report_carries_the_gate_and_the_exit_stays_0_unless_strict(self):
        code, report = self.run_main(AUDIT)
        self.assertEqual((code, report["embedded_gate"]["status"]), (0, "fail"))
        code, report = self.run_main(AUDIT, "--embedded-gate-strict")
        self.assertEqual(code, 3)
        code, report = self.run_main([("mail", 100, 99)], "--embedded-gate-strict")
        self.assertEqual((code, report["embedded_gate"]["status"]), (0, "pass"))

    def test_a_generation_only_run_does_not_read_the_chunks(self):
        _code, report = self.run_main(AUDIT, mode="generation")
        self.assertNotIn("embedded_gate", report)


if __name__ == "__main__":
    unittest.main()
