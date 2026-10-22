"""``--max-rss-mb`` is enforced: a probe over its resident-memory limit prints a partial
report naming the phase and exits non-zero, instead of being killed from outside without a
word; any other failure's error line names the phase too."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from scripts import ontology_projection_probe as probe

ENV = {"PLX_WORKBENCH_API_URL": "https://invalid.example", "PLX_WORKBENCH_API_KEY": "k"}


def run_main(stalled, extra=()):
    """``main`` with ``_run_probe`` entering the ``sync`` phase and then ``stalled``."""
    def run_probe(budget, partial, *args, **kwargs):
        partial["objects"] = 7
        budget.at("sync", "mirror", tempfile.gettempdir())
        return stalled()

    out = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(probe, "_run_probe", run_probe), \
            mock.patch.object(probe, "open_session", return_value=None), \
            mock.patch("integrations.workbench_read_api.WorkbenchReadApiClient"), \
            mock.patch.object(probe, "RSS_SAMPLE_SECONDS", 0.01), \
            contextlib.redirect_stdout(out):
        code = probe.main(["--db", os.path.join(tmp, "m.db"), "--store",
                           os.path.join(tmp, "s.db"), *extra], environ=ENV)
    return code, out.getvalue()


class RssWatchdogTest(unittest.TestCase):
    def test_over_the_limit_prints_the_phase_and_exits(self):
        exited = threading.Event()
        codes = []

        def hard_exit(code):
            codes.append(code)
            exited.set()

        def stall():
            self.assertTrue(exited.wait(10), "the watchdog never fired")
            return {"probe": probe.PROBE}

        with mock.patch.object(probe, "_current_rss_mb", return_value=900.0), \
                mock.patch.object(probe, "_hard_exit", hard_exit):
            _code, text = run_main(stall, ["--max-rss-mb", "512"])
        report = json.loads(text.splitlines()[0])
        self.assertEqual(codes, [probe.EXIT_RSS])
        self.assertNotEqual(probe.EXIT_RSS, 0)
        self.assertEqual((report["status"], report["phase"], report["store"]),
                         ("rss_exceeded", "sync", "mirror"))
        self.assertEqual((report["rss_mb"], report["max_rss_budget_mb"], report["objects"]),
                         (900.0, 512.0, 7))
        self.assertIn("sync", report["phase_seconds"])

    def test_under_the_limit_runs_on(self):
        with mock.patch.object(probe, "_current_rss_mb", return_value=100.0), \
                mock.patch.object(probe, "_hard_exit", side_effect=AssertionError("fired")):
            code, text = run_main(lambda: {"probe": probe.PROBE, "ok": True},
                                  ["--max-rss-mb", "512"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(text)["ok"])

    def test_a_failure_error_line_names_the_phase(self):
        def fail():
            raise RuntimeError("boom")

        code, text = run_main(fail)
        line = json.loads(text)
        self.assertEqual(code, 1)
        self.assertEqual((line["error"], line["phase"]), ("RuntimeError", "sync"))
        self.assertNotIn("boom", text)


if __name__ == "__main__":
    unittest.main()
