"""Known, owned alarms move out of the open list while dated; bad lists never hide one."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from operating import ops_known

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)


def status():
    return {"open": [{"key": "restore_test"}, {"key": "unit:plx-suite.service"}]}


class KnownAlarmsTest(unittest.TestCase):
    def apply(self, body, raw=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "known.json"
            path.write_text(raw if raw is not None else json.dumps(body))
            return ops_known.apply(status(), NOW, path)

    def entry(self, until, **kw):
        return dict({"key": "restore_test", "owner": "o", "why": "w", "until": until}, **kw)

    def test_a_dated_owned_alarm_moves_to_known_and_the_rest_stay_open(self):
        got = self.apply([self.entry("2026-11-25T00:00:00Z")])
        self.assertEqual([i["key"] for i in got["open"]], ["unit:plx-suite.service"])
        self.assertEqual(got["known"][0]["known"], {"owner": "o", "why": "w", "until": "2026-11-25T00:00:00Z"})

    def test_a_real_failure_with_an_owner_stays_open_and_carries_who_and_what_to_do(self):
        got = self.apply([self.entry("2026-11-25T00:00:00Z", needs_action=True)])
        self.assertEqual(len(got["open"]), 2)
        self.assertEqual(got["known"], [])
        self.assertEqual(got["open"][0]["known"]["owner"], "o")

    def test_an_alarm_that_got_worse_than_when_it_was_owned_opens_again(self):
        body = [self.entry("2026-11-25T00:00:00Z", level="warning")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "k.json"
            path.write_text(json.dumps(body))
            worse = ops_known.apply({"open": [{"key": "restore_test", "level": "critical"}]}, NOW, path)
            same = ops_known.apply({"open": [{"key": "restore_test", "level": "warning"}]}, NOW, path)
        self.assertEqual(len(worse["open"]), 1)
        self.assertEqual(worse["known"], [])
        self.assertEqual(len(same["known"]), 1)

    def test_an_expired_entry_opens_the_alarm_again(self):
        got = self.apply([self.entry("2026-11-24T11:00:00Z")])
        self.assertEqual(len(got["open"]), 2)
        self.assertEqual(got["known"], [])

    def test_an_unreadable_list_or_an_entry_without_owner_or_date_hides_nothing(self):
        for body, raw in (([], "{not json"), ([self.entry("soon")], None),
                          ([self.entry("2026-11-25T00:00:00Z", owner="")], None),
                          ([self.entry("2026-11-25T00:00:00")], None)):
            self.assertEqual(len(self.apply(body, raw)["open"]), 2)

    def test_the_shipped_list_is_valid_and_every_entry_is_dated(self):
        entries = json.loads(ops_known.KNOWN_FILE.read_text())
        far = datetime(2000, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(len(ops_known.load(now=far)), len(entries))


if __name__ == "__main__":
    unittest.main()
