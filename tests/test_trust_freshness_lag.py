"""AUD-16: the Trust page's lag is the age of each system's newest capture, never measured
against the served build's stamp."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest

from operator_api import trust


class _Backend:
    def __init__(self, state_dir, sources):
        self.state_dir = state_dir
        self._sources = sources

    def freshness(self):
        return types.SimpleNamespace(sources=self._sources)


def _src(system, as_of, stale=False):
    return types.SimpleNamespace(system=system, as_of=as_of, stale=stale, capture="complete")


class TrustLag(unittest.TestCase):
    def _state(self, rows):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        (Path(d.name) / "host").mkdir()
        c = sqlite3.connect(Path(d.name) / "host" / "host.sqlite")
        c.execute("CREATE TABLE source_state (source TEXT, captured_at TEXT, configured INTEGER)")
        c.executemany("INSERT INTO source_state VALUES (?,?,1)", rows)
        c.commit()
        c.close()
        return Path(d.name)

    def test_lag_is_capture_age_not_served_gap(self):
        now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        state = self._state([("bc", "2026-11-15T11:50:00+00:00"),
                             ("teams", "2026-11-15T11:59:00+00:00"),
                             ("mail", "2026-11-15T11:00:00+00:00"),
                             ("mc", "not a time")])
        be = _Backend(state, [_src("BC", "2026-11-14T04:59:00+00:00"),
                              _src("Teams", "2026-10-30T00:00:00+00:00", True),
                              _src("Portal", "2026-11-14T00:00:00+00:00")])
        rows = {r.system: r for r in trust.freshness(be, now)}
        self.assertEqual(rows["BC"].lag_seconds, 600)       # not 107,885 s against served_at
        self.assertEqual(rows["Teams"].lag_seconds, 60)
        self.assertEqual(rows["Email"].lag_seconds, 3600)   # ledger only, no served entry
        self.assertIsNone(rows["Portal"].lag_seconds)       # no capture time: null, never 0
        self.assertNotIn("MC", rows)                        # unreadable time: no row, no lag
        self.assertEqual(rows["BC"].served_at, "2026-11-14T04:59:00+00:00")

    def test_future_capture_never_negative(self):
        now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
        state = self._state([("bc", "2026-11-15T12:05:00+00:00")])
        rows = trust.freshness(_Backend(state, []), now)
        self.assertEqual(rows[0].lag_seconds, 0)


if __name__ == "__main__":
    unittest.main()


class TrustOverdueUnits(unittest.TestCase):
    def test_a_late_unit_is_behind_though_the_newest_read_is_recent(self):
        from operator_api import trust
        now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
        read = {"state": "complete", "served_through": now.isoformat(), "left": 0}
        _at, ok = trust._source_served("graph_mail", now, dict(read), now)
        self.assertIsNone(ok)
        _at, late = trust._source_served("graph_mail", now, dict(read, refresh_overdue=True), now)
        self.assertIn("not read within their usual interval", late)
