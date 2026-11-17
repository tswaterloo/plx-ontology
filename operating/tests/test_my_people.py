"""The people a person works with and their conversations going (``operating.my_people``):
ranking, whose turn, automated senders, pay/finance subjects, and the people exclusion
failing closed. SYNTHETIC fixtures only; evidence label: isolated-fixture."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from operating import my_people as mp

NOW = datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc)
ME = "oid-ana"


class People:
    unavailable = False
    internal = ("plx.example",)

    def __init__(self, excluded=("oid-sam",), fail=False):
        self.directory = {"ana@plx.example": ME, "joe@plx.example": "oid-joe",
                          "sam@plx.example": "oid-sam"}
        self.excluded, self.fail = set(excluded), fail

    def identity_clear(self, who):
        if self.fail:
            raise RuntimeError("down")
        return who not in self.excluded and who != "undecided@x.example"


def out(to, subject, days=1, cc=()):
    return {"at": (NOW - timedelta(days=days)).isoformat(), "to": list(to), "cc": list(cc),
            "subject": subject}


def came(sender, subject, days=1):
    return {"at": (NOW - timedelta(days=days)).isoformat(), "in": True, "from": sender,
            "to": ["ana@plx.example"], "cc": [], "subject": subject}


ROWS = [
    out(["bob@acme.example"], "Re: PO 4411 ship date", 1),
    came("bob@acme.example", "PO 4411 ship date", 2),
    out(["bob@acme.example"], "PO 4411 ship date", 3),
    came("lee@supply.example", "COA for lot 22", 1),
    out(["lee@supply.example"], "COA for lot 22", 2),
    came("joe@plx.example", "Lunch", 4),
    came("no-reply@portal.example", "Your report", 1),
    came("notifications@teams.example", "Digest", 1),
    out(["kim@acme.example"], "Invoice 77 overdue payment", 1),
    came("kim@acme.example", "RE: Invoice 77 overdue payment", 2),
    out(["joe@plx.example", "sam@plx.example"], "Plan", 1),
    came("joe@plx.example", "Plan", 2),
    out(["old@acme.example"], "Way back", 120),
]


class PeopleAndThreads(unittest.TestCase):
    def test_ranked_people_whose_turn_and_no_robots(self):
        got = mp.people_and_threads(People(), ME, ROWS, now=NOW,
                                    finance=lambda t: "payment" in t.lower())
        people = [p["address"] for p in got["people"]]
        self.assertEqual(people[0], "bob@acme.example")          # 2 sent + 1 in
        self.assertNotIn("no-reply@portal.example", people)
        self.assertNotIn("notifications@teams.example", people)
        self.assertNotIn("old@acme.example", people)             # past 90 days
        self.assertNotIn("ana@plx.example", people)              # never themself
        bob = got["people"][0]
        self.assertEqual((bob["sent"], bob["received"], bob["inside"]), (2, 1, False))
        self.assertEqual(bob["last_subject"], "Re: PO 4411 ship date")
        turns = {t["subject"]: t["turn"] for t in got["threads"]}
        self.assertEqual(turns, {"PO 4411 ship date": "theirs", "COA for lot 22": "yours"})

    def test_pay_or_finance_is_never_shown(self):
        got = mp.people_and_threads(People(), ME, ROWS, now=NOW,
                                    finance=lambda t: "payment" in t.lower())
        kim = next(p for p in got["people"] if p["address"] == "kim@acme.example")
        self.assertEqual(kim["last_subject"], "")
        self.assertFalse([t for t in got["threads"] if "Invoice" in t["subject"]])

    def test_an_excluded_person_and_every_mail_they_are_on_leave_no_trace(self):
        got = mp.people_and_threads(People(), ME, ROWS, now=NOW)
        people = {p["address"]: p for p in got["people"]}
        self.assertNotIn("sam@plx.example", people)
        # the "Plan" mail Sam was on tells nothing of Joe either, and is no thread
        self.assertEqual((people["joe@plx.example"]["sent"],
                          people["joe@plx.example"]["received"]), (0, 2))
        self.assertFalse([t for t in got["threads"] if t["subject"] == "Plan"])
        # a received mail with an excluded person on its Cc tells nothing either
        cc = dict(came("lee@supply.example", "Secret plan", 0), cc=["sam@plx.example"])
        got = mp.people_and_threads(People(), ME, ROWS + [cc], now=NOW)
        lee = next(p for p in got["people"] if p["address"] == "lee@supply.example")
        self.assertNotEqual(lee["last_subject"], "Secret plan")
        self.assertEqual(lee["received"], 1)
        undecided = mp.people_and_threads(
            People(), ME, [out(["undecided@x.example"], "Hi"), came("undecided@x.example", "Hi")],
            now=NOW)
        self.assertEqual(undecided, {"people": [], "threads": []})

    def test_nothing_at_all_when_the_exclusion_cannot_clear(self):
        self.assertIsNone(mp.people_and_threads(People(fail=True), ME, ROWS, now=NOW))
        self.assertIsNone(mp.people_and_threads(People(excluded=(ME,)), ME, ROWS, now=NOW))
        self.assertIsNone(mp.people_and_threads(None, ME, ROWS, now=NOW))
        broken = People()
        broken.unavailable = True
        self.assertIsNone(mp.people_and_threads(broken, ME, ROWS, now=NOW))

    def test_names_and_topics(self):
        self.assertEqual(mp.label("ana.lopez@x.example"), "Ana Lopez")
        self.assertEqual(mp.topic("RE: Fwd: re:  PO 1"), "PO 1")


if __name__ == "__main__":
    unittest.main()
