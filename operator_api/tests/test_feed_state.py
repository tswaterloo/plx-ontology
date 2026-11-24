"""Background feeds: a stopped job is said, never shown as an empty list."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from operating import ops_alarms
from operator_api import feed_state


def status(checks, *, minutes_old=1):
    now = datetime.now(timezone.utc)
    at = (now.replace(microsecond=0)).isoformat().replace("+00:00", "Z")
    if minutes_old:
        from datetime import timedelta
        at = ((now - timedelta(minutes=minutes_old)).replace(microsecond=0)
              ).isoformat().replace("+00:00", "Z")
    return {"schema": ops_alarms.STATUS_SCHEMA, "checked_at": at, "checks": checks,
            "open": [], "recovered": []}


def write(root: Path, body):
    folder = root / ops_alarms.ALARM_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ops_alarms.STATUS_FILE).write_text(json.dumps(body))


OK_CHECKS = [{"name": n, "state": "ok", "line": "x", "detail": ""}
             for n in ("proactive_last_pass", "proactive_detectors", "delta_last_cycle",
                       "units:system", "units:user")]


class FeedStateTest(unittest.TestCase):
    def feeds(self, body, *, absent=False):
        with tempfile.TemporaryDirectory() as tmp:
            if not absent:
                write(Path(tmp), body)
            return {f["key"]: f for f in feed_state.build(tmp)["feeds"]}

    def test_every_feed_runs_when_the_pass_is_fresh_and_names_no_stopped_unit(self):
        got = self.feeds(status(OK_CHECKS))
        self.assertEqual({k: f["state"] for k, f in got.items()},
                         {"reply_drafts": "running", "autopilot": "running",
                          "comms_findings": "running", "late_pos": "running"})

    def test_a_stopped_timer_is_not_running_with_the_checks_own_words(self):
        stopped = {"name": "unit:plx-reply-drafts.timer", "state": "breach",
                   "line": "plx-reply-drafts.timer (system): stopped: no next run",
                   "detail": "stopped: no next run"}
        got = self.feeds(status(OK_CHECKS + [stopped]))
        self.assertEqual(got["reply_drafts"]["state"], "not_running")
        # fixed plain words, never the alarm's own text or a unit name
        self.assertEqual(got["reply_drafts"]["why"], "Reply drafts have stopped.")
        self.assertNotIn("plx-", json.dumps(got))
        # no hours measured: no time is invented
        self.assertEqual(got["reply_drafts"]["since"], "")
        user = dict(stopped, name="unit:user:plx-reply-drafts.timer")
        self.assertEqual(self.feeds(status(OK_CHECKS + [user]))["reply_drafts"]["state"],
                         "not_running")
        self.assertEqual(got["autopilot"]["state"], "running")   # its own units are fine

    def test_the_autopilot_feed_stops_with_its_live_timer_not_with_the_fast_consumer(self):
        fast = {"name": "unit:plx-autopilot-fast-serve.service", "state": "breach", "line": "x",
                "detail": "enabled but not running", "unit": "h", "value": 82.1}
        self.assertEqual(self.feeds(status(OK_CHECKS + [fast]))["autopilot"]["state"], "running")
        live = {"name": "unit:plx-autopilot-follow.timer", "state": "breach", "line": "x",
                "detail": "stopped: no next run"}
        got = self.feeds(status(OK_CHECKS + [live]))["autopilot"]
        self.assertEqual(got["state"], "not_running")
        self.assertEqual(got["why"], "The autopilot's feed has stopped.")

    def test_a_stale_detector_pass_is_not_running_never_a_zero(self):
        stale = [dict(c, state="breach", detail="last good pass 9 h ago")
                 if c["name"] == "proactive_last_pass" else c for c in OK_CHECKS]
        self.assertEqual(self.feeds(status(stale))["late_pos"]["state"], "not_running")

    def test_no_pass_an_old_pass_or_a_missing_detector_is_unknown_never_running(self):
        self.assertEqual({f["state"] for f in self.feeds(None, absent=True).values()},
                         {"unknown"})
        old = self.feeds(status(OK_CHECKS, minutes_old=600))
        self.assertEqual({f["state"] for f in old.values()}, {"unknown"})
        self.assertEqual(self.feeds(status([]))["late_pos"]["state"], "unknown")
        # a unit with no check is healthy only on a fresh pass
        # ... and the unit lists were read: with no scope check a unit's absence says nothing
        self.assertEqual(self.feeds(status(OK_CHECKS))["reply_drafts"]["state"], "running")
        self.assertEqual(self.feeds(status(OK_CHECKS[:3]))["reply_drafts"]["state"], "unknown")
        skipped = {"name": "unit:plx-reply-drafts.timer", "state": "skipped", "detail": "not-found"}
        self.assertEqual(self.feeds(status(OK_CHECKS + [skipped]))["reply_drafts"]["state"],
                         "unknown")

    def test_no_state_dir_is_unknown(self):
        self.assertEqual({f["state"] for f in feed_state.build(None)["feeds"]}, {"unknown"})


if __name__ == "__main__":
    unittest.main()


class StoppedSinceTest(unittest.TestCase):
    def test_a_stopped_unit_says_when_it_last_ran_from_the_hours_measured(self):
        stopped = {"name": "unit:plx-autopilot-fast.timer", "state": "breach", "line": "x",
                   "detail": "stopped: no next run", "unit": "h", "value": 44.9}
        got = feed_state._stopped_since([stopped], "2026-11-23T03:21:10.938803Z")
        self.assertEqual(got, "2026-11-21T06:27:00Z")
        self.assertEqual(feed_state._stopped_since([stopped], "not a time"), "")
