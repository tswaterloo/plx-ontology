import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone

from operating import recurring_instructions as ri

ET = ri.EASTERN


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


class NextDue(unittest.TestCase):
    def test_daily_weekly_monthly_in_eastern_time(self):
        thu = at(2026, 11, 19, 9)   # a Thursday, after 8
        self.assertEqual(ri.next_due("schedule.daily", {}, 8, thu), at(2026, 11, 20, 8))
        fri = at(2026, 11, 20, 9)
        self.assertEqual(ri.next_due("schedule.daily", {"weekdays_only": True}, 8, fri),
                         at(2026, 11, 23, 8))
        self.assertEqual(ri.next_due("schedule.weekly", {"weekday": "monday"}, 8, thu),
                         at(2026, 11, 23, 8))
        self.assertEqual(ri.next_due("schedule.weekly", {"weekday": "thursday"}, 10, thu),
                         at(2026, 11, 19, 10))
        self.assertEqual(ri.next_due("schedule.monthly", {"day": 1}, 8, thu),
                         at(2026, 12, 1, 8))
        self.assertIsNone(ri.next_due("message.received", {}, 8, thu))

    def test_the_hour_holds_across_daylight_saving(self):
        before = at(2026, 10, 31, 9)   # EDT; 11-01 is the switch to EST
        got = ri.next_due("schedule.daily", {}, 8, before)
        self.assertEqual(got, at(2026, 11, 1, 8))
        self.assertEqual(got.astimezone(timezone.utc).hour, 13)


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ri.RecurringStore(self.tmp.name)
        self.now = at(2026, 11, 19, 9).timestamp()
        self.weekly = {"kind": "schedule.weekly", "words": "Every Monday",
                       "params": {"weekday": "monday"}}

    def test_owner_only_rows_edit_pause_stop(self):
        row = self.store.add("a", "every Monday chase open POs", "Chase", self.weekly,
                             now=self.now)
        self.assertEqual(row["next_run"], at(2026, 11, 23, 8).timestamp())
        self.assertEqual(stat.S_IMODE(os.stat(self.store.path).st_mode), 0o600)
        self.assertEqual(self.store.list("b"), [])
        self.assertIsNone(self.store.get("b", row["id"]))
        self.assertIsNone(self.store.change("b", row["id"], state="paused"))
        self.assertFalse(self.store.stop("b", row["id"]))
        moved = self.store.change("a", row["id"], weekday="friday", hour=7, now=self.now)
        self.assertEqual(moved["next_run"], at(2026, 11, 20, 7).timestamp())
        self.store.change("a", row["id"], state="paused", now=self.now)
        self.assertEqual(self.store.due(at(2026, 11, 21, 0).timestamp()), [])
        self.store.change("a", row["id"], state="active", now=self.now)
        self.assertEqual([r["id"] for r in self.store.due(at(2026, 11, 21, 0).timestamp())],
                         [row["id"]])
        self.assertTrue(self.store.stop("a", row["id"]))
        self.assertEqual(self.store.list("a"), [])

    def test_refused_shapes(self):
        for trigger, hour in (({"kind": "message.received", "params": {}}, 8),
                              (self.weekly, 24),
                              ({"kind": "schedule.weekly", "params": {"weekday": "x"}}, 8)):
            with self.assertRaises(ri.RecurringRefused):
                self.store.add("a", "t", "l", trigger, hour=hour, now=self.now)
        row = self.store.add("a", "t", "l", self.weekly, now=self.now)
        with self.assertRaises(ri.RecurringRefused):
            self.store.change("a", row["id"], state="stopped")

    def test_a_run_missed_while_down_runs_once_and_survives_a_new_store(self):
        row = self.store.add("a", "t", "l", {"kind": "schedule.daily", "params": {}},
                             now=self.now)
        late = at(2026, 11, 25, 12).timestamp()   # five runs missed
        again = ri.RecurringStore(self.tmp.name)   # a restart
        self.assertEqual([r["id"] for r in again.due(late)], [row["id"]])
        again.ran("a", row["id"], "prepared 1", now=late)
        self.assertEqual(again.due(late), [])
        self.assertEqual(again.get("a", row["id"])["next_run"], at(2026, 11, 26, 8).timestamp())


if __name__ == "__main__":
    unittest.main()
