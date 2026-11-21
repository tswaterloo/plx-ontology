"""Limits on a person's Auto switch (``operating.assist_switches``) at the write path's
boundary: an item the person's own limits do not cover waits for their click (never
performed by itself), limits never widen Auto, and limits that cannot be read hold Auto
back. A person's own confirm (an ``Approval``) is not touched by them.

SYNTHETIC items and addresses; fake transports only. Evidence label: injected-transport.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from operating import assist_switches as sw
from operating import autopilot_write as aw
from operating.tests.test_autopilot_write import NOW, OWNER, Base, mc_item

# NOW is Thursday 2026-11-12 10:00 AM US Eastern
THURSDAY = 3


def limits(state, **raw) -> None:
    sw.write(state, [OWNER], "approvals", sw.parse(raw), NOW)


class AutoLimits(Base):
    def live(self) -> dict:
        return self.go_live()

    def test_auto_performs_only_inside_every_limit_the_person_set(self):
        env = self.live()
        # no limits: performed by itself (the earned, routine, switched-on case)
        self.assertEqual(self.executor(env).run(mc_item(1), NOW)[0]["status"], "done")
        cases = [
            ({"senders": ["@elsewhere.example"]}, "switch:sender_not_chosen"),
            ({"days": [0, 1]}, "switch:outside_days"),
            ({"start": "13:00", "end": "17:00"}, "switch:outside_hours"),
            ({"min_percent": 99}, "switch:below_your_bar"),      # judged 14 of 15 = 93.3%
        ]
        for n, (raw, why) in enumerate(cases, start=2):
            limits(self.state, **raw)
            r = self.executor(env).run(mc_item(n), NOW)[0]
            self.assertEqual((r["status"], r["reasons"]), ("not_routine", [why]), raw)
        calls = len(self.mc.calls)
        # inside every limit: performed again
        limits(self.state, days=[THURSDAY], start="09:00", end="11:00", min_percent=90)
        self.assertEqual(self.executor(env).run(mc_item(9), NOW)[0]["status"], "done")
        self.assertEqual(len(self.mc.calls), calls + 1)

    def test_unreadable_limits_hold_auto_back_and_a_persons_confirm_is_untouched(self):
        env = self.live()
        path = self.state / sw.FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with aw.Journal(path).locked() as held:
            held.append({"event": "switch_conditions", "choice": "approvals", "by": OWNER,
                         "senders": ["not an address"], "at": NOW.isoformat()})
        r = self.executor(env).run(mc_item(1), NOW)[0]
        self.assertEqual((r["status"], r["reasons"]), ("not_routine", [sw.UNREADABLE]))
        self.assertEqual(self.mc.calls, [])
        # a torn line (a write cut short) holds Auto back too, never stops the run
        with path.open("a") as torn:
            torn.write('{"event": "switch_conditions", "choice": "appro')
        r = self.executor(env).run(mc_item(3), NOW)[0]
        self.assertEqual((r["status"], r["reasons"]), ("not_routine", [sw.UNREADABLE]))
        self.assertEqual(self.mc.calls, [])
        path.write_text("")
        # someone else's limits never touch this person's items
        sw.write(self.state, ["other@plx.example"], "approvals",
                 sw.parse({"senders": ["@x.example"]}), NOW)
        limits(self.state)
        self.assertEqual(self.executor(env).run(mc_item(2), NOW)[0]["status"], "done")

    def test_malformed_limits_are_refused_whole(self):
        for raw in ({"senders": ["bob"]}, {"days": [7]}, {"start": "9:00", "end": "17:00"},
                    {"start": "09:00"}, {"start": "17:00", "end": "09:00"},
                    {"min_percent": "lots"}, {"min_percent": 101},
                    {"min_percent": "NaN"}, {"senders": [f"a{i}@x.example" for i in range(51)]}):
            with self.assertRaises(ValueError, msg=raw):
                sw.parse(raw)
        got = sw.parse({"senders": ["Bob@X.example", "@y.example", "bob@x.example"],
                        "days": [4, 0, 4], "min_percent": "95.5"})
        self.assertEqual((got.senders, got.days, got.min_percent),
                         (("bob@x.example", "@y.example"), (0, 4), Decimal("95.5")))

    def test_a_domain_matches_only_its_own_senders(self):
        item = {"body": {"happened": {"sender": "Kim@Customer.example"}}}
        cond = sw.parse({"senders": ["@customer.example"]})
        self.assertEqual(sw.holds(cond, item, NOW, None), [])
        for sender in ("kim@notcustomer.example", "kim@customer.example.evil", ""):
            other = {"body": {"happened": {"sender": sender}}}
            self.assertEqual(sw.holds(cond, other, NOW, None), ["switch:sender_not_chosen"])
        # the window is read in US Eastern, across the UTC day boundary too
        late = datetime(2026, 11, 13, 3, 30, tzinfo=timezone.utc)   # Thu 10:30 PM ET
        night = sw.parse({"days": [THURSDAY], "start": "22:00", "end": "23:00"})
        self.assertEqual(sw.holds(night, item, late, None), [])


class MadeATask(unittest.TestCase):
    def test_a_task_made_from_an_item_is_no_verdict_on_what_was_prepared(self):
        from operating import autopilot_outcomes as ao
        item = mc_item(1)
        self.assertEqual(ao.classify(item, {"decision": "reject", "note": ao.TASK_NOTE}, NOW),
                         ("ignored", "made_a_task"))
        self.assertEqual(ao.classify(item, {"decision": "reject", "note": "wrong"}, NOW),
                         ("rejected", "rejected"))


if __name__ == "__main__":
    unittest.main()
