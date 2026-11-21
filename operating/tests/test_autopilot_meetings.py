"""Meeting follow-ups from the captured calendars (``autopilot.owners_of`` rule
"organizer"). Evidence: isolated-fixture."""
from __future__ import annotations

import json
import unittest
from datetime import timedelta

from operating import autopilot as ap
from operating.tests.test_autopilot import NOW, OURS, OURS_OID, OUTSIDE, Fixture
from operating.tests.test_proactive_engine import EXCLUDED_OID

BOSS = "boss@synth-tenant.example"     # the fixture exclusion's excluded person


class OutsideOrganizer(Fixture):
    def test_a_meeting_organized_outside_is_followed_up_by_its_calendars_person(self):
        """Captured calendars 11-06..21: 19 of 20 meetings with an outside attendee were
        organized outside the company, so the organizer rule made no follow-up for them.
        Such a meeting goes to the person whose calendar holds it; one a colleague
        organized stays the colleague's (no copy for the attendee)."""
        w = self.w
        got = {}
        for who in (OUTSIDE, "colleague@" + OURS.split("@")[1]):
            meeting = w.obj("Meeting", {"mailbox": OURS_OID, "subject": "Review " + who,
                                        "organizer": json.dumps({"address": who}),
                                        "attendees": json.dumps([{"address": OUTSIDE}]),
                                        "end": ap._iso(NOW - timedelta(hours=1))})
            w.link("Meeting.with_customer", meeting, self.customer)
            got[who] = self.cycle([meeting], now=NOW)["prepared"].get("meeting_followup", 0)
        self.assertEqual(list(got.values()), [1, 0])


    def meeting(self, *, mailbox=OURS_OID, organizer=OUTSIDE, attendees=(OUTSIDE,)):
        m = self.w.obj("Meeting", {"mailbox": mailbox, "subject": "Review",
                                   "organizer": json.dumps({"address": organizer}),
                                   "attendees": json.dumps([{"address": a} for a in attendees]),
                                   "end": ap._iso(NOW - timedelta(hours=1))})
        self.w.link("Meeting.with_customer", m, self.customer)
        return m

    def reasons(self):
        return {r[0] for r in self.queue().conn.execute(
            "SELECT reason FROM events WHERE event_type = 'meeting.ended'")}

    def test_an_excluded_persons_calendar_or_meeting_makes_nothing(self):
        """A meeting on an excluded person's calendar, written by them, or that they
        attended is left alone (as a message with them in its thread): no item, draft or
        session names, addresses or quotes them; the same meeting without them is
        followed up."""
        out = self.cycle([self.meeting(attendees=(OUTSIDE,))], now=NOW)
        self.assertEqual(out["prepared"].get("meeting_followup"), 1, out)
        for kw in ({"mailbox": EXCLUDED_OID}, {"organizer": BOSS},
                   {"attendees": (OUTSIDE, BOSS)}):
            with self.subTest(**kw):
                self.setUp()
                out = self.cycle([self.meeting(**kw)], now=NOW)
                self.assertNotIn("meeting_followup", out["prepared"])
                self.assertIn("participants_not_cleared", self.reasons())
                dump = json.dumps(self.queue().items()).lower()
                self.assertNotIn(BOSS, dump)
                self.assertNotIn(EXCLUDED_OID, dump)

    def test_a_calendar_of_no_known_person_makes_nothing(self):
        # a shared or unknown calendar (its id not one of our people's mailboxes)
        out = self.cycle([self.meeting(mailbox="00000000-0000-4000-8000-0000000000c9")],
                         now=NOW)
        self.assertNotIn("meeting_followup", out["prepared"])
        self.assertTrue({r for r in self.reasons() if r and "not_organizer_copy" in r}, out)


if __name__ == "__main__":
    unittest.main()
