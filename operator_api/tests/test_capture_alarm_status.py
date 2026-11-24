"""The capture-health line Home shows: an unreadable health read says so, never reads as healthy."""
import unittest
from unittest import mock

from operator_api import freshness


class CaptureAlarmStatus(unittest.TestCase):
    def test_no_state_dir_says_nothing(self):
        self.assertIsNone(freshness.capture_alarm_status(None))

    def test_an_unreadable_health_read_is_said(self):
        with mock.patch("operating.ops_health.targets", side_effect=OSError("gone")):
            got = freshness.capture_alarm_status("/nonexistent-state")
        self.assertIsNotNone(got)
        self.assertIn("could not be read", got.line)
        self.assertEqual(got.stalled, [])


if __name__ == "__main__":
    unittest.main()
