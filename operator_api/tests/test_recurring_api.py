import tempfile
import unittest
from pathlib import Path
from unittest import mock

from operator_api import backend as be
from operator_api.errors import UserFacing


class RecurringApi(unittest.TestCase):
    def setUp(self):
        self.backend = be.WorkspaceBackend(lambda session: mock.MagicMock(), data_label="t")
        self.addCleanup(self.backend.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.backend.data_dir = Path(tmp.name)
        self.a, self.b = mock.Mock(key="a"), mock.Mock(key="b")

    def test_kept_from_the_line_and_only_the_owners(self):
        row = self.backend.recurring_keep(self.a, "every Monday chase the open POs")
        self.assertEqual((row["kind"], row["params"]), ("schedule.weekly", {"weekday": "monday"}))
        self.assertEqual([r["id"] for r in self.backend.recurring_list(self.a)], [row["id"]])
        self.assertEqual(self.backend.recurring_list(self.b), [])
        for call in (lambda: self.backend.recurring_change(self.b, row["id"], state="paused"),
                     lambda: self.backend.recurring_stop(self.b, row["id"])):
            with self.assertRaises(UserFacing) as got:
                call()
            self.assertEqual(got.exception.status, 404)
        self.assertEqual(self.backend.recurring_change(self.a, row["id"], state="paused")
                         ["state"], "paused")
        self.backend.recurring_stop(self.a, row["id"])
        self.assertEqual(self.backend.recurring_list(self.a), [])

    def test_a_line_that_does_not_repeat_or_waits_for_an_event_is_refused(self):
        for line in ("chase the open POs", "whenever a supplier confirms, update BC"):
            with self.assertRaises(UserFacing) as got:
                self.backend.recurring_keep(self.a, line)
            self.assertEqual(got.exception.status, 422)


class EventRules(unittest.TestCase):
    def test_a_whenever_line_is_kept_for_the_owners_mailbox_and_mirrored(self):
        import tempfile as tf
        from operating import recurring_events as rev
        backend = be.WorkspaceBackend(lambda session: mock.MagicMock(), data_label="t")
        self.addCleanup(backend.stop)
        tmp, state = tf.TemporaryDirectory(), tf.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(state.cleanup)
        backend.data_dir, backend.state_dir = Path(tmp.name), Path(state.name)
        me = mock.Mock(key="a", email="me@plx.ca")
        with self.assertRaises(UserFacing):   # no sender or subject: every mail
            backend.recurring_keep(me, "whenever a supplier writes, draft a reply")
        row = backend.recurring_keep(me, "whenever a supplier writes, draft a reply",
                                     sender="acme.com")
        self.assertEqual(row["params"]["email"], "me@plx.ca")
        rules = rev.read_rules(Path(state.name) / "autopilot")
        self.assertEqual([(r["rid"], r["owner"], r["from"]) for r in rules],
                         [(row["id"], "a", "acme.com")])
        backend.recurring_change(me, row["id"], state="paused")
        self.assertEqual(rev.read_rules(Path(state.name) / "autopilot"), [])


if __name__ == "__main__":
    unittest.main()
