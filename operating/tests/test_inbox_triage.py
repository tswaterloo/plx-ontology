"""What needs my reply (``operating.inbox_triage``), who is overloaded (``workload``) and
label and artwork change control (``operating.label_changes``) through the engine and the
per-reader inbox.

SYNTHETIC fixtures only (``World`` stores, synthetic handles and addresses); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import time
import unittest

from operating import inbox_triage as it
from operating import label_changes as lc
from operating.tests.test_proactive_engine import (AS_OF, EXCLUDED_OID, OURS_OID, Engine,
                                                   World)
from operating.tests.test_proactive_threads import (APPROVER, APPROVER_OID, BOSS, OURS,
                                                    OUTSIDE, mail, people)

RULES = [it.RULE, lc.RULE]


def day(offset: int) -> date:
    return AS_OF - timedelta(days=offset)


def reader_of(oid: str) -> str:
    return f"synth-tenant:employee:{oid}"


def customer_of(w: World) -> str:
    if not hasattr(w, "_customer"):
        w._customer = w.obj("Customer", {"name": "Synth customer"})
    return w._customer


def ask(w: World, i: int, *, box: str = OURS_OID, to: str = OURS, sender: str = OUTSIDE,
        body: str | None = None,
        sent: int = 4, conversation: str | None = None, customer: bool = True) -> str:
    words = ("delivery date", "carton count", "pallet height", "lot number", "ship window",
             "invoice total", "freight quote", "pickup time", "label size", "batch size")
    body = body or (f"Hi, could you confirm the {words[i % len(words)]} for the next order? "
                    f"We also need the {words[(i // len(words)) % len(words)]}.")
    made = mail(w, day(sent), sender=sender, to=(to,), body=body, subject=f"Order {i}",
                mailbox=box, conversation=conversation or f"ask-{i}", n=20_000 + i)
    if customer:
        w.link("Email.sent_by_customer", made, customer_of(w))
    return made


class Classifiers(unittest.TestCase):

    def test_asks_negations_pleasantries_and_bulk_mail(self):
        for text, want in (
                ("Could you confirm the ship date for PO 12?", True),
                ("Please send the COA for the last lot.", True),
                ("Hey team, is there a reason this item is not on the website?", True),
                ("Let me know if you have any questions.", False),
                ("No need to reply, FYI: the truck left.", False),
                ("Hope you are well? Have a great weekend!", False),
                ("Please see attached.", False),
                ("For card payment, please call our office.", False),
                ("Going forward, please copy our AR inbox.", False),
                ("Restock hasn't left?? WTH!", False),
                ("From last month? I don't have it. Let me check the system.", False),
                ("Thanks, all received. Regards, Is your business in need of storage?", False),
                ("https://shop.example/item?ref=12&x=1", False),
                ("Thank you. Jane Roe Accounts Receivable. Please remit to our new address.",
                 False),
                ("Thanks Jane, do you think it can ship next week?", True),
                ("What is wrong with people? This goes back to spring.", False)):
            with self.subTest(text=text):
                self.assertIs(it.reply_ask(text), want)
        self.assertTrue(it.bulk("hello@email.synth-shop.example", "New arrivals"))
        self.assertTrue(it.bulk("a@synth.example", "Our sale ͏ ͏ ͏ ͏ ͏ starts"))
        self.assertFalse(it.bulk("buyer@synth-customer.example", "Can you ship Friday?"))
        for text, want in (("Can you kindly update the case label format below?", True),
                           ("We need to update the case labels to include the origin.", True),
                           ("Is there any update on the proof of payment?", False),
                           ("This is a private label opportunity, can you update me?", False),
                           ("Please see revised labels.", False),
                           ("I already updated the labels, please check.", False),
                           ("No need to change the artwork.", False)):
            with self.subTest(text=text):
                self.assertIs(lc.change_request(text), want)


class Triage(Engine):

    def run_rules(self, w: World, **options) -> dict:
        options.setdefault("people", people())
        return self.run_engine(w, only=RULES, **options)

    def mine(self, oid: str, *kinds: str, **read) -> list[dict]:
        return self.read(reader=reader_of(oid), kinds=list(kinds or (it.KIND,)), limit=1000,
                         **read)["gaps"]

    def world(self) -> tuple[World, str]:
        w = World()
        owed = ask(w, 1)                                                  # owed a reply
        ask(w, 2)                                                         # answered
        mail(w, day(2), sender=OURS, to=(OUTSIDE,), body="It ships Friday.",
             subject="Re: Order 2", conversation="ask-2", n=20_100)
        ask(w, 3, body="Let me know if you have any questions. Have a good day!")
        ask(w, 4, sender="news@mail.synth-shop.example", customer=False)  # a mailing
        ask(w, 5, body="No need to reply. Can you check it?")             # negated
        ask(w, 6, box=APPROVER_OID, to=APPROVER)                          # another person's
        ask(w, 7, box=EXCLUDED_OID, to=BOSS)                              # excluded box
        cc = mail(w, day(4), sender=OUTSIDE, to=(OURS,), cc=(BOSS,), mailbox=OURS_OID,
                  body="Can you confirm the price?", conversation="ask-8", n=20_008)
        w.link("Email.sent_by_customer", cc, customer_of(w))                    # excluded person
        ask(w, 9, sent=0)                                                 # not waited yet
        return w, owed

    def test_only_the_owner_sees_their_own_unanswered_asks_without_a_body(self):
        w, owed = self.world()
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][it.RULE], "complete")
        (gap,) = self.mine(OURS_OID)
        self.assertEqual(gap["object_id"], owed)
        self.assertEqual(gap["event"]["asked_by"], OUTSIDE)
        self.assertNotIn("could you confirm", json.dumps(gap).lower())
        # another user's mailbox is never shown: the approver sees only their own
        (theirs,) = self.mine(APPROVER_OID)
        self.assertNotEqual(theirs["object_id"], owed)
        # an administrator, a reader with no employee identity, sees nobody's
        self.assertEqual(self.read(kinds=[it.KIND], limit=1000)["gaps"], [])
        # the excluded person's mailbox and the thread naming them were never judged
        self.assertEqual(self.mine(EXCLUDED_OID), [])
        skipped = summary["not_evaluated"][it.RULE]
        self.assertEqual(skipped.get("participants_not_cleared"), 1)
        self.assertGreaterEqual(skipped.get("not_cleared_unread", 0), 1)
        # a restricted reader without the mailbox's grant sees nothing (it rests on absence)
        self.assertEqual(self.mine(OURS_OID, may_read=lambda key: key != f"mail:{OURS_OID}"),
                         [])
        # the reader's list drafts a reply, never sent
        listed = it.triage(self.mine(OURS_OID), AS_OF)
        (line,) = listed["lines"]
        self.assertEqual((line["draft"]["status"], line["draft"]["sent"], listed["sent"]),
                         ("draft_not_sent", False, 0))
        self.assertEqual(line["draft"]["to"], [{"address": OUTSIDE}])
        self.assertEqual(line["customer_tier"], "none")

    def test_the_people_exclusion_unavailable_blocks_both_rules(self):
        w, _ = self.world()
        summary = self.run_rules(w, people=people(unavailable=True))
        for rule in RULES:
            self.assertEqual(summary["detectors"][rule], "blocked")
        self.assertEqual(self.mine(OURS_OID), [])

    def test_one_ten_and_sixty_asks_each_raised_once_within_budget(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                for i in range(n):
                    ask(w, i)
                for k in range(2):   # the same ask chased twice more: one line, 2 chases
                    mail(w, day(3 - k), sender=OUTSIDE, to=(OURS,), conversation="ask-0",
                         body="Following up: could you confirm the date?",
                         subject="Re: Order 0", n=29_000 + k)
                started = time.monotonic()
                summary = self.run_rules(w)
                self.assertLess(time.monotonic() - started, 60)
                self.assertEqual(summary["detectors"][it.RULE], "complete")
                gaps = self.mine(OURS_OID)
                conversations = {g["event"]["messages"][0]["subject"] for g in gaps}
                self.assertEqual(len(conversations), n)
                top = it.triage(gaps, AS_OF)["lines"][0]
                self.assertEqual(top["chases"], 2)   # the repeat chases rank first


class Prospects(Engine):
    run_rules = Triage.run_rules
    mine = Triage.mine

    def test_a_prospects_first_question_is_a_low_new_inquiry_and_a_pitch_is_dropped(self):
        w = World()
        ask(w, 1)                                                           # a customer's
        prospect = mail(w, day(4), sender="ann@synth-prospect.example", to=(OURS,),
                        body="Hello, do you make 300 ml bottles with a pump?",
                        subject="Bottles", conversation="new-1", n=30_001)
        mail(w, day(4), sender="rep@synth-seller.example", to=(OURS,),
             body="Are you interested in a quick call about our freight rates?",
             subject="Re: Freight", conversation="pitch-1", n=30_002)
        mail(w, day(4), sender="bo@synth-known.example", to=(OURS,),    # a domain we mailed
             body="Hello, do you make 500 ml bottles?", subject="Bottles 2",
             conversation="known-1", n=30_003)
        mail(w, day(9), sender=OURS, to=("cy@synth-known.example",), body="Our catalogue.",
             subject="Catalogue", conversation="known-0", n=30_004)
        self.run_rules(w)
        lines = it.triage(self.mine(OURS_OID), AS_OF)["lines"]
        self.assertEqual([(l["object_id"], l["new_inquiry"]) for l in lines][-1],
                         (prospect, True))
        self.assertEqual(len(lines), 2)
        last = [g for g in self.mine(OURS_OID) if g["object_id"] == prospect][0]
        self.assertEqual(last["follow_up"]["priority"], "low")


class Workload(unittest.TestCase):
    TEAM = "SALES"
    OTHER = "00000000-0000-4000-8000-0000000000b1"
    THIRD = "00000000-0000-4000-8000-0000000000b2"

    @staticmethod
    def guard(*, unavailable: bool = False):
        from integrations.people_exclusion import PeopleExclusion
        return it.excluded_by(PeopleExclusion(unavailable="excluded_people_oids_absent")
                              if unavailable else PeopleExclusion([EXCLUDED_OID]))

    def rows(self):
        return ([{"kind": it.KIND, "owner": OURS_OID, "teams": []}] * 3
                + [{"kind": it.KIND, "owner": self.OTHER, "teams": []}] * 2
                + [{"kind": it.KIND, "owner": self.THIRD, "teams": []}]
                + [{"kind": it.KIND, "owner": EXCLUDED_OID, "teams": []}]
                + [{"kind": "mc_task_overdue", "owner": None, "teams": ["OPS"]}]
                + [{"kind": "late_po", "owner": None, "teams": ["PROC", "OPS"]}])

    def team(self, members, reader=OURS_OID, managers=None):
        grants = it.validate({"teams": {oid: self.TEAM for oid in members},
                              "managers": managers or {}})
        return it.workload(self.rows(), reader=[reader], grants=grants,
                           owner_kinds=[it.KIND], excluded=self.guard())

    def test_a_team_of_one_or_two_shows_no_reply_total_and_three_does(self):
        for members, total in (([OURS_OID], None), ([OURS_OID, self.OTHER], None),
                               ([OURS_OID, self.OTHER, self.THIRD], 6),
                               # an excluded member is not one of the three
                               ([OURS_OID, self.OTHER, EXCLUDED_OID], None)):
            with self.subTest(members=len(members)):
                out = self.team(members)
                row = {t["team"]: t for t in out["teams"]}[self.TEAM]
                self.assertEqual((row["awaiting_reply"], row["suppressed"]),
                                 (total, total is None))
                # the reader's own row is shown; it never sits beside a total that names
                # one other person (a total shown is three or more people's)
                self.assertEqual([p["person"] for p in out["people"]], [OURS_OID])
                if total is not None:
                    self.assertGreaterEqual(len(members), it.MIN_TEAM)
        # the unassigned: shown only when three or more people make it up
        out = self.team([])
        unassigned = {t["team"]: t for t in out["teams"]}[it.NOT_ASSIGNED]
        self.assertEqual(unassigned["awaiting_reply"], 6)
        lone = it.workload([{"kind": it.KIND, "owner": OURS_OID, "teams": []},
                            {"kind": it.KIND, "owner": self.OTHER, "teams": []}],
                           reader=[OURS_OID], grants={"teams": {}, "managers": {}},
                           owner_kinds=[it.KIND], excluded=self.guard())
        self.assertIsNone(lone["teams"][0]["awaiting_reply"])

    def test_department_counts_and_per_person_only_to_self_and_granted_managers(self):
        members = [OURS_OID, self.OTHER, self.THIRD]
        mine = self.team(members)
        teams = {t["team"]: t for t in mine["teams"]}
        self.assertEqual((teams["OPS"]["tasks"], teams["OPS"]["alerts"],
                          teams["PROC"]["alerts"]), (1, 1, 1))
        manager = self.team(members, reader=APPROVER_OID,
                            managers={APPROVER_OID: [self.TEAM]})
        self.assertEqual(sorted(p["person"] for p in manager["people"]), sorted(members))
        outsider = self.team(members, reader=self.OTHER.replace("b1", "c1"))
        self.assertEqual(outsider["people"], [])
        self.assertNotIn(EXCLUDED_OID, json.dumps([mine, manager, outsider]))
        # the real exclusion unavailable: nobody per person, no reply total anywhere
        closed = it.workload(self.rows(), reader=[OURS_OID],
                             grants=it.validate({"teams": {o: self.TEAM for o in members}}),
                             owner_kinds=[it.KIND], excluded=self.guard(unavailable=True))
        self.assertEqual(closed["people"], [])
        self.assertFalse([t for t in closed["teams"] if t["awaiting_reply"]])

    def test_grants_refuse_anything_but_the_shape_and_a_bad_file_grants_nothing(self):
        for bad in ({"teams": {"not-an-oid": "SALES"}}, {"managers": {OURS_OID: "SALES"}},
                    {"teams": {OURS_OID: "sales"}}, {"extra": {}}, []):
            with self.subTest(bad=bad), self.assertRaises(it.GrantsInvalid):
                it.validate(bad)
        with tempfile.TemporaryDirectory() as tmp:
            store = it.WorkloadGrants(Path(tmp) / it.GRANTS_FILE)
            self.assertEqual(store.load(), {"teams": {}, "managers": {}})
            (Path(tmp) / it.GRANTS_FILE).write_text('{"managers": {"x": ["SALES"]}}')
            self.assertEqual(store.load(), {"teams": {}, "managers": {}})
            store.assign({"teams": {OURS_OID: "SALES"}, "managers": {}})
            self.assertEqual(store.load()["teams"], {OURS_OID: "SALES"})


class Labels(Engine):

    def run_rules(self, w: World, **options) -> dict:
        options.setdefault("people", people())
        return self.run_engine(w, only=RULES, **options)

    def request(self, w: World, i: int, *, sent: int, conversation: str) -> str:
        made = mail(w, day(sent), sender=OUTSIDE, to=(OURS,), conversation=conversation,
                    body="Hi, can you please update the case label to add the lot code?",
                    subject=f"Label change {i}", n=40_000 + i)
        if not hasattr(w, "_item"):
            w._item = w.obj("Product", {"code": "SYN-1"})
        w.link("Email.mentions_product", made, w._item)
        return made

    def test_requests_are_tracked_stale_or_done_without_approval_on_the_item(self):
        w = World()
        self.request(w, 1, sent=25, conversation="lab-1")                 # stale
        self.request(w, 2, sent=25, conversation="lab-2")                 # done, no approval
        mail(w, day(20), sender=OURS, to=(OUTSIDE,), conversation="lab-2",
             body="The updated case label is attached.", subject="Re: Label change 2",
             n=40_102)
        self.request(w, 3, sent=25, conversation="lab-3")                 # approved and done
        mail(w, day(22), sender=OURS, to=(OUTSIDE,), conversation="lab-3",
             body="The revised label is attached.", subject="Re: Label change 3", n=40_103)
        mail(w, day(21), sender=OUTSIDE, to=(OURS,), conversation="lab-3",
             body="Approved, go ahead.", subject="Re: Label change 3", n=40_104)
        mail(w, day(25), sender=OUTSIDE, to=(OURS,), cc=(BOSS,), conversation="lab-4",
             body="Please change the artwork colour.", subject="Artwork", n=40_005)
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][lc.RULE], "complete")
        gaps = self.read(kinds=list(lc.KINDS), limit=1000)["gaps"]
        section = lc.item_section(gaps, w._item)
        self.assertEqual((section["total"], section["stale"], section["no_approval"]),
                         (3, 1, 1))
        self.assertEqual(section["by_state"], {"requested": 1, "done": 2})
        self.assertNotIn("lot code", json.dumps(gaps))
        self.assertEqual(summary["not_evaluated"][lc.RULE].get("participants_not_cleared"), 1)
        # a reader without the thread's mailbox sees none of it
        self.assertEqual(self.read(kinds=list(lc.KINDS), limit=1000,
                                   may_read=lambda key: key != f"mail:{OURS_OID}")["gaps"],
                         [])


if __name__ == "__main__":
    unittest.main()
