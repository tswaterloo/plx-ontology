"""The status line's truth-check flag: counts read from the check's status file, and none
stated once the check is older than two hours."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from operator_api.backend import TRUTH_STATUS, truth_flag

NOW = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc)


def _write(state: Path, ran: datetime) -> None:
    path = state / TRUTH_STATUS
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"generated_at": ran.isoformat(), "late_claims": 22,
                                "late_contradicted": 22, "claims": 100, "contradicted": 50,
                                "detectors_incomplete": ["a", "b"]}))


class TruthFlag(unittest.TestCase):
    def test_fresh_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp), NOW - timedelta(minutes=20))
            flag = truth_flag(Path(tmp), NOW)
        self.assertFalse(flag.stale)
        self.assertEqual((flag.late_claims, flag.late_contradicted, flag.detectors_incomplete),
                         (22, 22, 2))

    def test_old_check_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(Path(tmp), NOW - timedelta(hours=3))
            self.assertTrue(truth_flag(Path(tmp), NOW).stale)

    def test_no_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(truth_flag(Path(tmp), NOW))


if __name__ == "__main__":
    unittest.main()
