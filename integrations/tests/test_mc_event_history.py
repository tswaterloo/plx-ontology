"""Mission Control task events beyond MC's 25-event window (coverage audit 2026-10-30).

MC's read tools serve a task's latest 25 events only (``mc_get_task`` takes the task id
alone), so the capture keeps every event it ever read, never marks one missing because
it left the window, and accounts each task's history: ``whole`` (a read returned fewer
than the window, and every later full window reached back to an event held),
``since_first_read`` (first read a full window) or ``gap`` (a full window reaching back
to nothing held). Fixtures: the fake MC of ``test_mc_sources``. Evidence label:
injected-transport."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from integrations import mc_sources
from integrations.capture_store import CaptureStore
from integrations.mc_sources import KIND, CaptureSpec, McSourcesCapture, unit_of
from integrations.tests.test_mc_sources import Clock, FakeMc, audience, mc_client


class EventHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeMc()
        self.store = CaptureStore(Path(self.tmp.name) / "mc.sqlite", audience(), clock=Clock())
        self.addCleanup(self.store.close)

    def events(self, task: str, first: int, last: int) -> None:
        """MC's window of ``task``: its events ``first``..``last`` (at most 25)."""
        self.fake.events[task] = [self.fake.event(task, str(n), "task.progress",
                                                  f"2026-10-01T{n // 60:02d}:{n % 60:02d}:00Z")
                                  for n in range(last, first - 1, -1)]   # newest first

    def run_capture(self) -> dict:
        McSourcesCapture(self.store, mc_client(self.fake)).capture(CaptureSpec("ops"))
        unit = unit_of("ops", "task_events")
        run = self.store.run(KIND, unit)
        held = {path[-1] for path, _c in self.store.current_versions(KIND, unit)}
        return {"state": run["state"], "reason": run["reason"],
                "counts": json.loads(run["detail"])["event_history"]
                if isinstance(run["detail"], str) else run["detail"]["event_history"],
                "history": json.loads(self.store.state(mc_sources.HISTORY_STATE + "ops")),
                "held": held}

    def test_events_past_the_window_are_kept_and_each_tasks_history_is_accounted(self):
        self.events("TASK-1", 0, 24)       # a full window on its first read
        self.events("TASK-2", 0, 9)        # its whole history
        first = self.run_capture()
        self.assertEqual(first["history"], {"TASK-1": "since_first_read", "TASK-2": "whole",
                                            "TASK-3": "whole"})
        self.assertEqual((first["state"], first["reason"]), ("partial", "source_window:last_25"))
        self.assertEqual(first["counts"], {"whole": 2, "since_first_read": 1, "gap": 0})
        # Both grow by 20: each window reaches back to events already held.
        self.events("TASK-1", 20, 44)
        self.events("TASK-2", 5, 29)
        second = self.run_capture()
        self.assertEqual(second["history"]["TASK-1"], "since_first_read")
        self.assertEqual(second["history"]["TASK-2"], "whole")
        # Nothing that left MC's window is lost or marked missing.
        self.assertEqual({e for e in second["held"] if e.startswith("TASK-1~")},
                         {f"TASK-1~{n}" for n in range(45)})
        self.assertEqual({e for e in second["held"] if e.startswith("TASK-2~")},
                         {f"TASK-2~{n}" for n in range(30)})
        # More than a window between two reads: the events between are lost, said so.
        self.events("TASK-1", 80, 104)
        third = self.run_capture()
        self.assertEqual(third["history"]["TASK-1"], "gap")
        self.assertEqual(third["counts"], {"whole": 2, "since_first_read": 0, "gap": 1})
        self.events("TASK-1", 90, 114)     # a gap stays a gap
        self.assertEqual(self.run_capture()["history"]["TASK-1"], "gap")

    def test_a_store_whose_every_task_history_is_whole_says_so(self):
        self.events("TASK-1", 0, 9)
        self.run_capture()
        self.events("TASK-1", 5, 29)       # a full window, contiguous with what is held
        found = self.run_capture()
        self.assertEqual(set(found["history"].values()), {"whole"})
        self.assertEqual((found["state"], found["reason"]),
                         ("partial", "source_window:accumulated"))
        self.assertEqual(len([e for e in found["held"] if e.startswith("TASK-1~")]), 30)

    def test_unreadable_history_state_starts_over_never_whole_on_a_guess(self):
        self.events("TASK-1", 0, 24)
        self.run_capture()
        self.store.set_state(mc_sources.HISTORY_STATE + "ops", "{not json")
        self.events("TASK-1", 10, 34)
        self.assertEqual(self.run_capture()["history"]["TASK-1"], "since_first_read")
        self.store.set_state(mc_sources.HISTORY_STATE + "ops", json.dumps({"TASK-1": "x"}))
        self.events("TASK-1", 60, 84)      # nothing held reached: a gap, not a fresh start
        self.assertEqual(self.run_capture()["history"]["TASK-1"], "gap")


if __name__ == "__main__":
    unittest.main()
