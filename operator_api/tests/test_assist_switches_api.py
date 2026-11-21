"""``POST /api/autopilot/conditions`` and ``POST /api/autopilot/items/{id}/always``: a
person sets limits on their own Auto (senders, days, hours, a stricter bar) and says
"always do this" for an item's sender or category. Only their own; only where their replay
score earned Auto; limits only narrow Auto, an unreadable limit is never replaced by none;
nothing is sent or written to a system.

SYNTHETIC people and items. Evidence label: isolated-fixture.
"""
from __future__ import annotations

import unittest

from operating import assist_switches as sw
from operating import autopilot_write as aw
from operator_api.tests import test_autopilot_area as area_tests

COLLEAGUE, OWNER, ROLE = area_tests.COLLEAGUE, area_tests.OWNER, area_tests.ROLE


def choice(area: dict, cid: str = "customer_replies") -> dict:
    return next(c for c in area["my_drafts"] if c["choice"] == cid)


class AutoLimitsApi(unittest.TestCase):
    client = area_tests.AutopilotArea.client
    score_fresh = area_tests.AutopilotArea.score_fresh

    def setUp(self) -> None:
        area_tests.AutopilotArea.setUp(self)

    def auto_on(self) -> set:
        return {w for (p, w), o in aw.opt_ins(aw.Journal(self.state / aw.AUDIT_FILE).rows())
                .items() if o["on"]}

    def test_limits_are_the_persons_own_checked_and_shown(self) -> None:
        owner, sam = self.client(OWNER), self.client(COLLEAGUE)
        body = {"choice": "customer_replies", "senders": ["@customer.example"],
                "days": [0, 1, 2, 3, 4], "start": "08:00", "end": "17:00"}
        got = owner.post("/api/autopilot/conditions", json=body)
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(choice(got.json())["conditions"]["senders"], ["@customer.example"])
        # the colleague sees none of the owner's limits; no person can be named
        self.assertIsNone(choice(sam.get("/api/autopilot").json()).get("conditions"))
        for bad, code in (({**body, "person": COLLEAGUE[1]}, 422),
                          ({**body, "choice": "nope"}, 404),
                          ({**body, "senders": ["kim"]}, 422),
                          ({**body, "start": "18:00"}, 422),
                          ({**body, "min_percent": 50}, 422)):      # below the gate's 90%
            self.assertEqual(owner.post("/api/autopilot/conditions", json=bad).status_code,
                             code, bad)
        nobody = self.client(("nobody@plx.example", None))
        self.assertEqual(nobody.post("/api/autopilot/conditions", json=body).status_code, 403)
        self.assertEqual(choice(owner.get("/api/autopilot").json())["conditions"]["days"],
                         [0, 1, 2, 3, 4])
        self.assertEqual(self.bc.calls, [])

    def test_always_needs_earned_auto_and_narrows_to_the_sender(self) -> None:
        owner = self.client(OWNER)
        # not earned yet: refused, nothing switched, no limit written
        got = owner.post("/api/autopilot/items/ap_own/always", json={"scope": "sender"})
        self.assertEqual(got.status_code, 409, got.text)
        self.assertEqual(self.auto_on(), set())
        self.assertFalse((self.state / sw.FILE).exists())
        self.score_fresh({ROLE: ([OWNER[1]], {"customer_order_intake": (40, 38)})})
        # someone else's item, or an unknown scope: refused
        self.assertEqual(owner.post("/api/autopilot/items/ap_sam/always",
                                    json={"scope": "sender"}).status_code, 404)
        self.assertEqual(owner.post("/api/autopilot/items/ap_own/always",
                                    json={"scope": "everything"}).status_code, 422)
        got = owner.post("/api/autopilot/items/ap_own/always", json={"scope": "sender"})
        self.assertEqual(got.status_code, 200, got.text)
        row = choice(got.json())
        self.assertEqual((row["mode"], row["conditions"]["senders"]),
                         ("auto", ["buyer@customer.example"]))
        self.assertEqual(self.auto_on(), {"customer_order_intake"})
        # the whole category: the sender limit goes, other limits stay
        owner.post("/api/autopilot/conditions", json={
            "choice": "customer_replies", "senders": ["buyer@customer.example"],
            "min_percent": 95})
        got = owner.post("/api/autopilot/items/ap_own/always", json={"scope": "category"})
        cond = choice(got.json())["conditions"]
        self.assertEqual((cond["senders"], cond["min_percent"]), ([], 95.0))
        self.assertEqual(self.bc.calls, [])

    def test_unreadable_limits_are_never_replaced_by_none(self) -> None:
        self.score_fresh({ROLE: ([OWNER[1]], {"customer_order_intake": (40, 38)})})
        path = self.state / sw.FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with aw.Journal(path).locked() as held:
            held.append({"event": "switch_conditions", "choice": "customer_replies",
                         "by": OWNER[1], "days": [9]})
        owner = self.client(OWNER)
        got = owner.post("/api/autopilot/items/ap_own/always", json={"scope": "category"})
        self.assertEqual(got.status_code, 409, got.text)
        self.assertEqual(self.auto_on(), set())


if __name__ == "__main__":
    unittest.main()
