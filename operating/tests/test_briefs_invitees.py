"""A meeting with no customer or supplier linked is prepared from the people invited: their
recent mail and Teams and the meetings they shared earlier, each line as the reader may read
it (never a record they cannot open), an invited person the exclusion names or cannot decide
left out, and every line cited. With the exclusion unavailable nothing about people is read.
A meeting that has a party keeps its party brief."""
from __future__ import annotations

import unittest

from operating import briefs as br
from operating.tests.test_briefs import FIN, NOW, OPS, ALL, People, Store, dumped, gap, section, world

INTERNAL = "CalendarEvent:meetings.calendar_events:synth-internal"


def internal_world(n_mail: int = 3):
    st = Store()
    st.add(INTERNAL, "Weekly sync", start="2026-11-12T14:00:00")
    people = {}
    for tag, ident in (("a", "synth-oid-a"), ("b", "synth-oid-excluded"), ("c", "undecidable"),
                       ("d", "synth-oid-d")):
        people[tag] = st.add(f"Person:graph.users:synth-{tag}", f"Person {tag.upper()}",
                             object_id=ident)
        st.link(INTERNAL, "CalendarEvent.with_person", people[tag])
    for i in range(n_mail):
        mail = st.add(f"Email:graph.mail:synth-inv-mail-{i}", f"mail {i}",
                      subject=f"Pricing note {i}", received_at=f"2026-11-0{1 + i % 8}T09:00:00")
        st.link(mail, "Email.sent_by_person", people["a"])
        st.link(mail, "Email.addressed_to_person", people["d"])   # one mail, two invited: listed once
    old = st.add("Email:graph.mail:synth-inv-old", "old", subject="Ancient note",
                 received_at="2025-01-01T09:00:00")
    st.link(old, "Email.sent_by_person", people["a"])
    secret = st.add("Email:graph.mail:synth-inv-secret", "s", aud="secret",
                    subject="Hidden thread", received_at="2026-11-05T09:00:00")
    st.link(secret, "Email.sent_by_person", people["a"])
    excluded_mail = st.add("Email:graph.mail:synth-inv-excl", "x", subject="Excluded person's mail",
                           received_at="2026-11-06T09:00:00")
    st.link(excluded_mail, "Email.sent_by_person", people["b"])
    past = st.add("CalendarEvent:meetings.calendar_events:synth-past", "Past sync",
                  subject="Last month sync", start="2026-10-12T14:00:00")
    st.link(past, "CalendarEvent.with_person", people["a"])
    future = st.add("CalendarEvent:meetings.calendar_events:synth-future", "Next sync",
                    subject="Next month sync", start="2026-12-12T14:00:00")
    st.link(future, "CalendarEvent.with_person", people["a"])
    return st


class InviteeBrief(unittest.TestCase):
    def brief(self, st, people="default", oid=INTERNAL, held=()):
        if people == "default":
            people = People(excluded={"synth-oid-excluded"})
        return br.meeting_brief(st, frozenset(held), {"status": "read", "gaps": []}, oid, now=NOW,
                                declared=None, people=people)

    def test_threads_and_earlier_meetings_come_from_the_people_invited(self):
        st = internal_world()
        st.sees = OPS
        out = self.brief(st)
        rows = section(out, "threads")["rows"]
        self.assertEqual(sorted(r["label"] for r in rows),
                         [f"Mail: Pricing note {i}" for i in range(3)])   # each once, though two invitees are on it
        self.assertTrue(all(r["detail"].startswith("With Person") for r in rows))
        earlier = section(out, "earlier")["rows"]
        self.assertEqual([r["label"] for r in earlier], ["Meeting: Last month sync"])   # not the future one
        self.assertIn("Person A", section(out, "threads")["note"])
        text = dumped(out)
        for hidden in ("Hidden thread", "Ancient note", "Excluded person", "Person B", "Person C", "Next month sync"):
            self.assertNotIn(hidden, text)
        # every line is cited
        for row in rows + earlier:
            self.assertTrue(row["sources"], row)

    def test_a_reader_who_may_open_more_reads_more_and_never_less(self):
        st = internal_world()
        st.sees = ALL
        full = {r["label"] for r in section(self.brief(st), "threads")["rows"]}
        self.assertIn("Mail: Hidden thread", full)
        st.sees = OPS
        narrow = {r["label"] for r in section(self.brief(st), "threads")["rows"]}
        self.assertLess(narrow, full)

    def test_with_the_people_exclusion_unavailable_no_person_is_read(self):
        for people in (None, People(unavailable="excluded_people_oids_absent")):
            st = internal_world()
            st.sees = ALL
            out = self.brief(st, people=people)
            sec = section(out, "threads")
            self.assertEqual((sec["state"], sec["rows"]), ("blocked", []))
            self.assertNotIn("Pricing note", dumped(out))
            self.assertFalse([c for c in st.calls if c[0] == "plx.around"
                              and "person" in c[1]["path"][0]["link_type"].lower()])

    def test_cardinality_no_thread_lost_or_listed_twice_at_1_10_60(self):
        for n in (1, 10, 60):
            st = internal_world(n_mail=n)
            st.sees = OPS
            sec = section(self.brief(st), "threads")
            labels = [r["label"] for r in sec["rows"]]
            self.assertEqual(len(labels), len(set(labels)), n)
            self.assertEqual(len(labels), min(n, br.MAX_ROWS), n)   # the section lists MAX_ROWS, the rest counted
            if n <= br.INVITEE_READS:
                self.assertEqual(len(labels) + sec["more"], n)   # none lost
            else:   # the per-person cap is said, never silent
                self.assertEqual(sec["state"], "partial")
                self.assertEqual(len(labels) + sec["more"], br.INVITEE_READS)

    def test_the_cap_keeps_the_newest_threads_not_the_first_read(self):
        st = internal_world(n_mail=0)
        st.sees = OPS
        a = "Person:graph.users:synth-a"
        # 50 mail linked oldest-first: the 40 newest must be the ones kept
        for i in range(50):
            mail = st.add(f"Email:graph.mail:synth-ord-{i:02d}", f"m{i}", subject=f"Dated {i:02d}",
                          received_at=f"2026-10-{1 + i // 2:02d}T{i % 2 * 5:02d}:00:00")
            st.link(mail, "Email.sent_by_person", a)
        sec = section(self.brief(st), "threads")
        kept = {r["label"] for r in sec["rows"]} | set()
        self.assertEqual(sec["state"], "partial")
        self.assertNotIn("Mail: Dated 00", kept)
        self.assertIn("Mail: Dated 49", kept)

    def test_a_meeting_with_a_party_keeps_its_party_brief_and_has_no_earlier_section(self):
        st, alerts, _orders = world()
        out = br.meeting_brief(st, frozenset(FIN), alerts, "CalendarEvent:meetings.calendar_events:synth-ev-1",
                               now=NOW, declared=None, people=People())
        self.assertNotIn("earlier", [s["key"] for s in out["sections"]])
        self.assertTrue(section(out, "orders")["rows"])

    def test_a_meeting_nobody_can_be_read_for_says_so_and_lists_nothing(self):
        st = Store()
        st.add(INTERNAL, "Solo", start="2026-11-12T14:00:00")
        out = self.brief(st)
        self.assertEqual(section(out, "threads")["rows"], [])
        self.assertIn("No one invited could be read for you", section(out, "threads")["note"])


if __name__ == "__main__":
    unittest.main()
