import os
import stat
import tempfile
import unittest
from pathlib import Path

from operating import recurring_events as rev

RULE = {"rid": "r1", "owner": "a", "email": "me@plx.ca", "from": "acme.com", "subject": "ship date"}
MAIL = {"event_type": "message.received", "object_id": "Email:1", "mailbox": "me@plx.ca",
        "sender": "Bob@Acme.com", "subject": "New ship date for PO 90412"}


class Match(unittest.TestCase):
    def test_only_what_the_person_set_and_only_their_mailbox(self):
        self.assertTrue(rev.matches(RULE, MAIL))
        self.assertFalse(rev.matches(RULE, {**MAIL, "mailbox": "other@plx.ca"}))
        self.assertFalse(rev.matches(RULE, {**MAIL, "sender": "x@other.com"}))
        self.assertFalse(rev.matches(RULE, {**MAIL, "subject": "Invoice"}))
        self.assertFalse(rev.matches(RULE, {**MAIL, "unaddressed": True}))
        self.assertFalse(rev.matches({**RULE, "from": "", "subject": ""}, MAIL))   # every mail
        self.assertFalse(rev.matches({**RULE, "email": ""}, {**MAIL, "mailbox": ""}))


class Files(unittest.TestCase):
    def test_rules_and_fires_are_private_and_each_fire_is_taken_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "autopilot"
            rev.write_rules(d, [RULE])
            self.assertEqual(rev.read_rules(d), [RULE])
            fires = rev.Fires(d)
            self.assertEqual(fires.note(rev.read_rules(d), [MAIL, MAIL], now=1.0), 1)
            self.assertEqual(fires.note([RULE], [MAIL], now=2.0), 0)   # once per mail
            for name in (rev.RULES_FILE, rev.FIRES_FILE):
                self.assertEqual(stat.S_IMODE(os.stat(d / name).st_mode), 0o600)
            self.assertEqual(fires.take(), [{"rid": "r1", "owner": "a", "object_id": "Email:1",
                                             "at": 1.0}])
            self.assertEqual(fires.take(), [])


if __name__ == "__main__":
    unittest.main()
