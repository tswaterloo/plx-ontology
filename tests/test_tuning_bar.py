"""The one tuning bar (``operating.excluded_people.tuning_barred``): the two excluded people's data is kept and shown to the owner, but their emails
never feed a tuning run, an eval set or what is learned. Checked at the boundary of the eval
pickers that read captured mail: a message is barred when any identity on it (sender, a
recipient, the mailbox it was read from) is excluded or one of ours the guard cannot decide;
an external counterpart does not bar it. Synthetic ids and addresses."""
from __future__ import annotations

import unittest

from integrations.people_exclusion import PeopleExclusion
from operating.excluded_people import guard_clear, tuning_barred

PARTNER = "11111111-1111-4111-8111-111111111111"
STAFF = "22222222-2222-4222-8222-222222222222"
OURS = ("example-plx.com",)


def guard(*, usable=True):
    g = PeopleExclusion([PARTNER]) if usable else PeopleExclusion(
        unavailable="excluded_people_oids_absent")
    if usable:
        g.attach_directory([(PARTNER, "partner@example-plx.com", ["partner@example-plx.com"]),
                            (STAFF, "staff@example-plx.com", ["staff@example-plx.com"])])
    return g


class TuningBarTests(unittest.TestCase):
    def test_a_message_naming_an_excluded_person_anywhere_is_barred(self):
        clear = guard_clear(guard(), OURS)
        self.assertFalse(tuning_barred(clear, "staff@example-plx.com", ["buyer@outside.example"]))
        for named in (("partner@example-plx.com",),                        # the sender
                      ("staff@example-plx.com", [{"emailAddress": {"address":
                                                  "partner@example-plx.com"}}]),  # a recipient
                      ("staff@example-plx.com", PARTNER),                  # their mailbox
                      ("staff@example-plx.com", "gone@example-plx.com"),   # undecided, ours
                      ("staff@example-plx.com", "a@b@c.example"),          # malformed
                      ("just a display name",),                            # no identity at all
                      ()):
            with self.subTest(named=named):
                self.assertTrue(tuning_barred(clear, *named))

    def test_no_usable_guard_or_a_raising_decider_bars_everything(self):
        self.assertIsNone(guard_clear(guard(usable=False), OURS))
        self.assertTrue(tuning_barred(None, "buyer@outside.example"))

        def boom(_value):
            raise RuntimeError("undecidable")
        self.assertTrue(tuning_barred(boom, "buyer@outside.example"))

    def test_the_eval_mail_picker_bars_on_recipients_not_the_sender_alone(self):
        # Fails on the old SenderGuard: it decided the sender only, so a cleared person's
        # mail to an excluded one fed the eval sets once that mail is captured.
        from scripts.eval_live_ops import SenderGuard
        senders = SenderGuard(guard())
        row = {"to": ["buyer@outside.example"], "cc": [], "bcc": [], "mailbox_upn": STAFF}
        self.assertTrue(senders.cleared("staff@example-plx.com", OURS, row))
        to_partner = {**row, "bcc": ["partner@example-plx.com"]}
        self.assertFalse(senders.cleared("staff@example-plx.com", OURS, to_partner))
        from_box = {**row, "mailbox_upn": PARTNER}
        self.assertFalse(senders.cleared("staff@example-plx.com", OURS, from_box))
        self.assertFalse(senders.cleared("", OURS, row))
        self.assertFalse(SenderGuard(guard(usable=False)).cleared("staff@example-plx.com",
                                                                  OURS, row))

    def test_one_ten_sixty_messages_keep_every_cleared_and_no_barred_one(self):
        from scripts.eval_live_ops import SenderGuard
        senders = SenderGuard(guard())
        for n in (1, 10, 60):
            rows = [({"to": [f"c{i}@outside.example"], "mailbox_upn": STAFF}
                     if i % 3 else {"to": ["partner@example-plx.com"], "mailbox_upn": STAFF})
                    for i in range(n)]
            kept = [r for r in rows if senders.cleared("staff@example-plx.com", OURS, r)]
            self.assertEqual(len(kept), sum(1 for i in range(n) if i % 3), n)
            self.assertFalse(any("partner@example-plx.com" in r["to"] for r in kept), n)


if __name__ == "__main__":
    unittest.main()
