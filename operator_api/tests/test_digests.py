import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from operating import digests
from operating import recurring_instructions as ri
from operator_api import backend as be
from operator_api import recurring_api as ra
from operator_api.errors import UserFacing


class Digests(unittest.TestCase):
    def setUp(self):
        self.backend = be.WorkspaceBackend(lambda session: mock.MagicMock(), data_label="t")
        self.addCleanup(self.backend.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.backend.data_dir = Path(tmp.name)
        far = int(time.time()) + 3600
        self.backend.known_readers = lambda: {"oid-a": {"ref": "a" * 64, "exp": far},
                                              "oid-b": {"ref": "b" * 64, "exp": far}}
        self.backend.revoked = lambda ref: False
        self.backend.identity_for = lambda oid, exp: oid.split("-")[1]
        patch = mock.patch.object(ra, "Reader", lambda ident, mode, a, b: mock.Mock(key=ident))
        patch.start()
        self.addCleanup(patch.stop)
        self.asked = []

        def ask(reader, request):
            self.asked.append((reader.key, request.question))
            yield "status", None
            yield "answer", mock.Mock(direct_answer=f"report for {reader.key}")
        self.backend.ask = ask
        self.a, self.b = mock.Mock(key="a"), mock.Mock(key="b")

    def test_all_off_until_switched_on_and_nothing_runs_for_an_off_digest(self):
        listed = self.backend.digest_list(self.a)
        self.assertEqual([d["id"] for d in listed], [d["id"] for d in digests.CATALOG])
        self.assertFalse(any(d["on"] for d in listed))
        self.assertEqual(self.backend.recurring_run_due(time.time() + 86400 * 14), 0)
        self.assertEqual(self.asked, [])

    def test_a_digest_runs_as_its_owner_and_its_report_is_theirs_alone(self):
        self.backend.digest_set(self.a, "morning", True, 6)
        self.backend.digest_set(self.b, "requests", True)
        got = {d["id"]: d for d in self.backend.digest_list(self.a)}
        self.assertTrue(got["morning"]["on"])
        self.assertEqual(got["morning"]["hour"], 6)
        self.assertFalse(got["requests"]["on"])          # b's switch is not a's
        self.assertEqual(self.backend.recurring_run_due(time.time() + 86400 * 14), 2)
        self.assertEqual(sorted(k for k, _ in self.asked), ["a", "b"])
        mine = {d["id"]: d for d in self.backend.digest_list(self.a)}
        theirs = {d["id"]: d for d in self.backend.digest_list(self.b)}
        self.assertEqual(mine["morning"]["report"], "report for a")
        self.assertIsNone(mine["requests"]["report"])
        self.assertEqual(theirs["requests"]["report"], "report for b")
        self.assertIsNone(theirs["morning"]["report"])

    def test_off_removes_the_row_and_the_report_and_an_unknown_digest_is_refused(self):
        self.backend.digest_set(self.a, "decisions", True)
        self.backend.recurring_run_due(time.time() + 86400 * 14)
        self.backend.digest_set(self.a, "decisions", False)
        self.assertFalse({d["id"]: d for d in self.backend.digest_list(self.a)}["decisions"]["on"])
        self.assertEqual(self.backend._recurring_store().list("a"), [])
        with self.assertRaises(UserFacing):
            self.backend.digest_set(self.a, "no-such", True)
        with self.assertRaises(UserFacing):
            self.backend.digest_set(self.a, "morning", True, 99)

    def test_a_failed_or_empty_run_keeps_no_report_and_says_why(self):
        self.backend.ask = lambda reader, request: iter([("answer", mock.Mock(direct_answer="  "))])
        self.backend.digest_set(self.a, "feedback", True)
        self.backend.recurring_run_due(time.time() + 86400 * 14)
        row = {d["id"]: d for d in self.backend.digest_list(self.a)}["feedback"]
        self.assertIsNone(row["report"])
        self.assertTrue(row["last_result"].startswith("Not run"))

    def test_catalog_questions_are_plain_words_and_every_trigger_is_valid(self):
        for spec in digests.CATALOG:
            trig = digests.trigger(spec)
            self.assertEqual(trig["params"]["digest"], spec["id"])
            self.assertIsNotNone(ri.next_due(trig["kind"], trig["params"], spec["hour"],
                                             datetime(2026, 11, 22, tzinfo=ri.EASTERN)))
