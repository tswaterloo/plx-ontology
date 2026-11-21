import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from operating import instruct as ins
from operating import recurring_instructions as ri
from operator_api import backend as be
from operator_api import recurring_api as ra


class OneOff(unittest.TestCase):
    def test_the_every_words_come_out(self):
        self.assertEqual(ins.one_off("every Monday at 8, chase the late POs"), "Chase the late POs")
        self.assertEqual(ins.one_off("Chase the late POs every Friday"), "Chase the late POs")
        self.assertEqual(ins.one_off("daily: list new NCRs"), "List new NCRs")
        self.assertEqual(ins.one_off("whenever a supplier confirms, update BC"), "Update BC")


class Runs(unittest.TestCase):
    def setUp(self):
        self.backend = be.WorkspaceBackend(lambda session: mock.MagicMock(), data_label="t")
        self.addCleanup(self.backend.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.backend.data_dir = Path(tmp.name)
        far = int(time.time()) + 3600
        self.backend.known_readers = lambda: {"oid-a": {"ref": "a" * 64, "exp": far},
                                              "oid-r": {"ref": "r" * 64, "exp": far}}
        self.backend.revoked = lambda ref: ref == "r" * 64
        self.backend.identity_for = lambda oid, exp: oid.split("-")[1]
        patch = mock.patch.object(ra, "Reader", lambda ident, mode, a, b: mock.Mock(key=ident))
        patch.start()
        self.addCleanup(patch.stop)
        self.asked = []

        def instruct_ask(reader, request):
            self.asked.append((reader.key, request.question))
            yield "status", None
            yield "answer", mock.Mock(instruction=mock.Mock(writes=[1, 2], documents=[]))
        self.backend.instruct_ask = instruct_ask
        self.store = ri.RecurringStore(self.backend.data_dir)
        self.thu = datetime(2026, 11, 19, 9, tzinfo=ri.EASTERN).timestamp()
        daily = {"kind": "schedule.daily", "words": "Every day", "params": {}}
        self.mine = self.store.add("a", "every day at 8, chase the late POs", "Chase", daily,
                                   now=self.thu)
        self.revoked = self.store.add("r", "every day list NCRs", "NCRs", daily, now=self.thu)
        self.gone = self.store.add("z", "every day list NCRs", "NCRs", daily, now=self.thu)

    def test_due_ones_run_once_as_their_signed_in_owner_and_never_for_a_revoked_one(self):
        late = datetime(2026, 11, 22, 9, tzinfo=ri.EASTERN).timestamp()   # three days missed
        self.assertEqual(self.backend.recurring_run_due(late), 3)
        self.assertEqual(self.asked, [("a", "Chase the late POs")])
        self.assertEqual(self.store.get("a", self.mine["id"])["last_result"],
                         "Prepared 2 for you to confirm")
        for owner, row in (("r", self.revoked), ("z", self.gone)):
            self.assertTrue(self.store.get(owner, row["id"])["last_result"].startswith("Not run"))
        self.assertEqual(self.backend.recurring_run_due(late), 0)   # moved on: once
        self.assertEqual(self.store.get("a", self.mine["id"])["next_run"],
                         datetime(2026, 11, 23, 8, tzinfo=ri.EASTERN).timestamp())

    def test_a_row_another_process_took_first_is_not_run_again(self):
        late = datetime(2026, 11, 22, 9, tzinfo=ri.EASTERN).timestamp()
        row = self.store.due(late)[0]
        self.assertTrue(self.store.claim(row, late))
        self.assertFalse(self.store.claim(row, late))   # the other server's stale read

    def test_a_run_past_its_bound_ends_and_the_next_owner_still_runs(self):
        def slow(reader, request):
            self.asked.append((reader.key, request.question))
            yield "status", None
            yield "status", None
            yield "answer", mock.Mock(instruction=mock.Mock(writes=[1], documents=[]))
        self.backend.instruct_ask = slow
        with mock.patch.object(ra, "RECURRING_RUN_SECONDS", -1.0):
            self.backend.recurring_run_due(
                datetime(2026, 11, 22, 9, tzinfo=ri.EASTERN).timestamp())
        self.assertTrue(self.store.get("a", self.mine["id"])["last_result"]
                        .startswith("Not run: it took too long"))

    def test_a_fire_runs_once_as_its_owner_on_the_mail_that_fired_it(self):
        from operating import recurring_events as rev
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.backend.state_dir = Path(state.name)
        self.store.change("a", self.mine["id"], state="paused", now=self.thu)
        ev = self.store.add("a", "whenever acme writes, draft a reply", "Acme",
                            {"kind": "message.received", "words": "w",
                             "params": {"from": "acme.com", "email": "me@plx.ca"}}, now=self.thu)
        evr = self.store.add("r", "whenever acme writes, draft a reply", "Acme",
                             {"kind": "message.received", "words": "w",
                              "params": {"from": "acme.com", "email": "r@plx.ca"}}, now=self.thu)
        rules = [{"rid": x["id"], "owner": o, "email": e, "from": "acme.com", "subject": ""}
                 for x, o, e in ((ev, "a", "me@plx.ca"), (evr, "r", "r@plx.ca"))]
        mails = [{"event_type": "message.received", "object_id": f"Email:{b}", "mailbox": b,
                  "sender": "x@acme.com", "subject": "hi"} for b in ("me@plx.ca", "r@plx.ca")]
        rev.Fires(Path(state.name) / "autopilot").note(rules, mails)
        self.backend.recurring_run_due(self.thu)
        self.assertEqual(self.asked, [("a", "Draft a reply")])
        self.assertTrue(self.store.get("r", evr["id"])["last_result"].startswith("Not run"))
        self.backend.recurring_run_due(self.thu)
        self.assertEqual(len(self.asked), 1)   # each fire once

    def test_a_paused_one_does_not_run(self):
        self.store.change("a", self.mine["id"], state="paused", now=self.thu)
        self.backend.recurring_run_due(datetime(2026, 11, 22, 9, tzinfo=ri.EASTERN).timestamp())
        self.assertEqual(self.asked, [])


if __name__ == "__main__":
    unittest.main()
