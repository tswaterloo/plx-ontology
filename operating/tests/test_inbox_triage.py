"""What needs my reply (``operating.inbox_triage``), who is overloaded (``workload``) and
label and artwork change control (``operating.label_changes``) and revision control
(``operating.label_revisions``) through the engine and the per-reader inbox.

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
from operating import label_revisions as lr
from operating import proactive_engine as pe
from operating import proactive_threads as pt
from operating.tests.test_proactive_engine import (AS_OF, EXCLUDED_OID, OURS_OID, Engine,
                                                   World)
from operating.tests.test_proactive_threads import (APPROVER, APPROVER_OID, BOSS, OURS,
                                                    OUTSIDE, mail, people)

RULES = [it.RULE, lc.RULE, lr.RULE]


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

class Feeds(unittest.TestCase):
    """A system feed is found from the mail itself (templates, no replies, never written to),
    not from a name or domain (``feed_senders``); a person who writes as often is not one."""

    OWN = "me@synth-own.example"

    @staticmethod
    def msgs(sender, n, *, subject=lambda i: f"Case {i} needs review",
             body=lambda i: f"Case {i}: please confirm this record is correct in the system.",
             to=("me@synth-own.example",)):
        return [it._Msg(oid=f"o-{sender}-{i}", channel="mail", owner="p", audience="a",
                        sender=sender, to=list(to), at=None, subject=subject(i), text=body(i),
                        conversation=f"c-{i}", ident=f"{sender}-{i}") for i in range(n)]

    def found(self, *batches):
        return it.feed_senders([m for b in batches for m in b], {self.OWN})

    def test_n_1_10_60_a_feed_is_found_from_the_mail_whatever_its_name(self):
        for n in (1, 10, it.FEED_MIN, 60):
            with self.subTest(n=n):
                feed = self.msgs("any-name@synth-system.example", n)
                person = self.msgs("pat@synth-human.example", n,
                                   subject=lambda i: f"topic {'abcdefghij'[i % 10]}{'klmnopqrst'[i // 3 % 10]} {i}",
                                   body=lambda i: f"Can you check item {'zyxwvutsrq'[i % 10]} with supplier {i * 7} soon?")
                want = {"any-name@synth-system.example"} if n >= it.FEED_MIN else set()
                self.assertEqual(self.found(feed, person), want)

    def test_replies_or_our_own_writing_to_the_sender_keep_it_a_person(self):
        n = it.FEED_MIN + 5
        replies = self.msgs("a@synth-system.example", n, subject=lambda i: f"Re: Case {i}")
        self.assertEqual(self.found(replies), set())
        feed = self.msgs("b@synth-system.example", n)
        ours = self.msgs(self.OWN, 1, to=("b@synth-system.example",))
        self.assertEqual(self.found(feed, ours), set())
        self.assertEqual(self.found(feed), {"b@synth-system.example"})
        no_template = self.msgs("c@synth-system.example", n, body=lambda i: f"Note {'abcdefghij'[i % 10]}{i * 13} about lot {i * 31}.")
        self.assertEqual(self.found(no_template), set())
        self.assertEqual(self.found(self.msgs("", n)), set())   # no sender: nothing to name


class AutomatedMail(Feeds):
    """``automated_mail`` is the one test triage and the autopilot share: a message's form
    (``bulk``, the JSON report and tagged alert rules) or a feed among the mail read together."""

    def test_form_or_feed_flags_a_person_does_not(self):
        feed = self.msgs("Any-Name@synth-system.example", it.FEED_MIN)
        machine = it.automated_mail([it.Seen(m.sender, m.to, m.subject, m.text) for m in feed],
                                    {self.OWN.upper()})
        self.assertTrue(machine("any-name@synth-system.example", "anything", "Case 1"))
        # a JSON report (the whole body, under a title line repeating the subject) is one;
        # member lines inside a person's own words are not
        self.assertTrue(machine("ops@synth-other.example",
                                'Report\n{\n"a": 1,\n"b": 2,\n"c": 3\n}', "Report"))
        self.assertFalse(machine("ops@synth-other.example",
                                 'x\n"a": 1\n"b": 2\n"c": 3', "Report"))
        self.assertFalse(machine("pat@synth-human.example", "Can you check lot 4?", "Lot 4"))
        # a tagged alert with no machine form is the feed's when its sender is a feed; the
        # same tag and echo from anyone else is a person's mail
        alert = ("[ACTION] Loop BLOCKED: job-7 waiting on the nightly sweep",
                 "Loop BLOCKED: job-7 waiting on the nightly sweep\n- x")
        self.assertTrue(machine("any-name@synth-system.example", alert[1], alert[0]))
        self.assertFalse(machine("pat@synth-human.example", alert[1], alert[0]))
        # our own message to the sender keeps it a person, whatever the case of either address
        ours = it.Seen(self.OWN.upper(), ["ANY-NAME@synth-system.example"], "Re: Case 1", "ok")
        machine = it.automated_mail([*feed, ours], {self.OWN})
        self.assertFalse(machine("any-name@synth-system.example", "anything", "Case 1"))
        # the thresholds are the caller's to set; a smaller feed needs a lower floor, and
        # our writing to the sender still keeps it a person
        few = feed[:5]
        self.assertFalse(it.automated_mail(few, {self.OWN})("any-name@synth-system.example", "x", "Case 1"))
        self.assertTrue(it.automated_mail(few, {self.OWN}, least=5)("any-name@synth-system.example", "x", "Case 1"))
        self.assertFalse(it.automated_mail([*few, ours], {self.OWN}, least=5)("any-name@synth-system.example", "x", "Case 1"))


class OneToOneChats(Engine):
    """The Teams capture lists a chat's members as object ids AND addresses and names a
    one-to-one chat ``19:<id>_<id>@unq.gbl.spaces``; its sender is ``{"display_name", "id"}``.
    Before, every such chat read as a group of four and no Teams ask was triaged
    (reproduced bug, same as the autopilot's)."""

    PAIR = "19:{}_{}@unq.gbl.spaces"
    run_rules, mine = Triage.run_rules, Triage.mine

    def guard(self, n: int):
        guard = people()
        for i in range(n):
            other = f"{i:08x}-0000-4000-8000-00000000a{i:03x}"
            guard.directory[f"colleague{i}@synth-tenant.example"] = other
            guard.chat_members[self.PAIR.format(OURS_OID, other)] = [
                OURS_OID, other, OURS, f"colleague{i}@synth-tenant.example"]
        guard.chat_members[self.PAIR.format(EXCLUDED_OID, OURS_OID)] = [
            EXCLUDED_OID, OURS_OID, BOSS, OURS]
        # a group whose members appear by id and by address: never a one-to-one
        guard.chat_members["19:group@thread.v2"] = [OURS_OID, APPROVER_OID, OURS, APPROVER,
                                                    "00000000-0000-4000-8000-0000000000c3"]
        return guard

    def chat(self, w: World, chat: str, sender: str, sent: int, text: str) -> str:
        return w.obj("TeamsChatMessage", {
            "created_at": f"{day(sent).isoformat()}T10:00:00Z", "subject": text,
            "from": json.dumps({"display_name": "Synth person", "id": sender})},
            audience=f"teams:chat:{chat}", binding="graph_teams.messages")

    def test_cardinality_each_one_to_one_ask_reaches_its_owner_once(self):
        ask_text = "Could you confirm the pallet count for tomorrow?"
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w, guard = World(), self.guard(n)
                for i in range(n):
                    other = f"{i:08x}-0000-4000-8000-00000000a{i:03x}"
                    self.chat(w, self.PAIR.format(OURS_OID, other), other, 4, ask_text)
                barred = self.chat(w, self.PAIR.format(EXCLUDED_OID, OURS_OID), EXCLUDED_OID,
                                   4, ask_text)
                unknown = self.chat(w, self.PAIR.format(APPROVER_OID, OURS_OID),
                                    APPROVER_OID, 4, ask_text)   # members never captured
                group = self.chat(w, "19:group@thread.v2", APPROVER_OID, 4, ask_text)
                started = time.monotonic()
                summary = self.run_rules(w, people=guard)
                self.assertLess(time.monotonic() - started, 60)
                self.assertEqual(summary["detectors"][it.RULE], "complete")
                gaps = self.mine(OURS_OID)
                self.assertEqual(len(gaps), n)
                self.assertEqual({g["event"]["channel"] for g in gaps}, {"teams"})
                self.assertEqual(len({g["object_id"] for g in gaps}), n)
                for oid in (barred, unknown, group):
                    self.assertNotIn(oid, {g["object_id"] for g in gaps})
                self.assertEqual(self.mine(EXCLUDED_OID), [])
                # the people exclusion unavailable: blocked, nothing raised
                self.assertEqual(self.run_rules(w, people=people(unavailable=True))
                                 ["detectors"][it.RULE], "blocked")


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


def people_with_drive():
    made = people()
    made.drive_owners = {"drive-1": ""}   # a site library (no OneDrive owner): cleared
    return made


def attach(w: World, email: str, name: str, *, box: str = OURS_OID) -> str:
    made = w.obj("Attachment", {"name": name}, binding="graph_mail.attachments",
                 audience=f"mail:{box}")
    w.link("Attachment.attached_to", made, email)
    return made


class Revisions(Engine):
    """Every label revision per item, its approval, the open orders on a superseded one,
    and the change rule reading the register as its source."""

    def test_names_parse_to_family_and_rank_and_documents_about_labels_do_not(self):
        for name, want in (
                ("Synth Shampoo Front Label Rev 2.pdf", ("synth shampoo front label", ("n", 2))),
                ("Synth Shampoo Front Label rev.1-06.jpg",
                 ("synth shampoo front label", ("n", 1))),
                ("v11 SYN01_0001_B_proof.pdf", ("syn01 0001 b proof", ("n", 11))),
                ("Synth Carton Artwork Rev B (2).ai", ("synth carton artwork", ("a", 2))),
                ("Synth Shampoo Front Label Revisions.pdf", None),     # no revision token
                ("Synth Shipping Label v2.pdf", None),                 # no artwork
                ("Private Label Inquiry v1.pdf", None),
                ("Synth Front Label Rev 2.docx", None),                # a document about it
                ("Synth Label Approval Form v1.pdf", None),
                ("Synth Front Label 2026-10-01.pdf", None)):            # a date is no revision
            with self.subTest(name=name):
                got = lr.parse(name)
                self.assertEqual(got and got[:2], want)
        for text, want in (("This is approved! Thanks.", True), ("Looks great.", True),
                           ("Please confirm the attached proof is approved.", False),
                           ("Received with the approved color chips.", False),
                           ("Looks good to me but maybe check the barcode.", False),
                           ("Artworks were usually approved in three rounds.", False),
                           ("Can we go ahead and order the labels?", False)):
            with self.subTest(text=text):
                self.assertIs(lr.approves(text), want)

    def world(self) -> tuple[World, dict]:
        w = World()
        ids = {"item": w.obj("Product", {"code": "SYN-1"})}
        for key, status in (("open_ao", "scheduled"), ("done_ao", "realized"),
                            ("both_ao", "allocated"), ("odd_ao", "released")):
            ids[key] = w.obj("AssemblyOrder", {"status": status})
        ids["so"] = w.obj("SalesOrder", {"status": "CONFIRMED",
                                         "order_date": day(30).isoformat()})
        # revision 1: mailed by the customer, naming the open sales order
        m1 = mail(w, day(40), sender=OUTSIDE, to=(OURS,), conversation="art-1",
                  body="Here is the front label.", subject="Front label", n=50_001)
        w.link("Email.mentions_product", m1, ids["item"])
        w.link("Email.mentions_sales_order", m1, ids["so"])
        ids["rev1"] = attach(w, m1, "Synth Shampoo Front Label Rev 1.pdf")
        # revision 1 in the artwork library too, naming three manufacturing orders
        f1 = w.obj("SharePointFile", {"name": "Synth Shampoo Front Label Rev 1.ai",
                                      "last_modified": f"{day(39).isoformat()}T10:00:00Z",
                                      "path": "/Artwork/Synth/Synth Shampoo Front Label Rev 1.ai"},
                   binding="sharepoint.drive_items", audience="sharepoint:drive-1")
        w.link("SharePointFile.mentions_product", f1, ids["item"])
        for key in ("open_ao", "done_ao", "both_ao", "odd_ao"):
            w.link("SharePointFile.mentions_assembly_order", f1, ids[key])
        # a lead-tier order mention is no pointer
        ids["lead_ao"] = w.obj("AssemblyOrder", {"status": "scheduled"})
        w.link("SharePointFile.mentions_assembly_order", f1, ids["lead_ao"], tier="lead")
        # revision 2, sent by us, approved by the customer in the thread
        m2 = mail(w, day(20), sender=OURS, to=(OUTSIDE,), conversation="art-2",
                  body="The revised front label is attached.", subject="Front label rev 2",
                  n=50_002)
        w.link("Email.mentions_product", m2, ids["item"])
        w.link("Email.mentions_assembly_order", m2, ids["both_ao"])
        ids["rev2"] = attach(w, m2, "Synth Shampoo Front Label Rev 2.pdf")
        mail(w, day(19), sender=OUTSIDE, to=(OURS,), conversation="art-2",
             body="Approved, OK to print.", subject="Re: Front label rev 2", n=50_003)
        # noise: no revision, no artwork; and a revision in the excluded person's mailbox
        m3 = mail(w, day(18), sender=OURS, to=(OUTSIDE,), conversation="art-3",
                  body="Shipping label attached.", subject="Shipping", n=50_004)
        attach(w, m3, "Synth Shipping Label v2.pdf")
        attach(w, m3, "Synth Shampoo Front Label Revisions.pdf")
        m4 = mail(w, day(10), sender=BOSS, to=(OUTSIDE,), conversation="art-4",
                  body="Front label attached.", subject="Front label rev 3",
                  mailbox=EXCLUDED_OID, n=50_005)
        w.link("Email.mentions_product", m4, ids["item"])
        ids["rev3_excluded"] = attach(w, m4, "Synth Shampoo Front Label Rev 3.pdf",
                                      box=EXCLUDED_OID)
        # a change request before revision 2: done by the new revision, approved with it
        ids["ask"] = mail(w, day(25), sender=OUTSIDE, to=(OURS,), conversation="art-5",
                          body="Hi, can you please update the front label to add the lot code?",
                          subject="Label change", n=50_006)
        w.link("Email.mentions_product", ids["ask"], ids["item"])
        return w, ids

    def test_history_approval_superseded_orders_and_the_change_rule_source(self):
        w, ids = self.world()
        summary = self.run_engine(w, only=RULES, people=people_with_drive())
        self.assertEqual(summary["detectors"][lr.RULE], "complete")
        gaps = self.read(kinds=list(lr.KINDS) + list(lc.KINDS), limit=1000)["gaps"]
        text = json.dumps(gaps)
        # the excluded person's copy is never read, and no body is kept
        self.assertNotIn(ids["rev3_excluded"], text)
        self.assertNotIn("Rev 3", text)
        self.assertNotIn("lot code", text)
        page = lr.history(gaps, ids["item"])
        self.assertEqual(len(page["families"]), 1)
        family = page["families"][0]
        self.assertEqual([r["revision"] for r in family["revisions"]], ["Rev 2", "Rev 1"])
        current, old = family["revisions"]
        self.assertTrue(current["current"])
        self.assertEqual((current["approved_on"], current["approved_by"]),
                         (day(19).isoformat(), OUTSIDE))
        self.assertEqual(old["superseded_on"], day(20).isoformat())
        self.assertIsNone(old["approved_on"])
        # open orders a superseded copy names: the sales order and the scheduled AO; not
        # the realized one, not one the new revision names too, not an unknown status,
        # not a lead-tier mention
        flagged = {g["object_id"] for g in gaps if g["kind"] == lr.SUPERSEDED}
        self.assertEqual(flagged, {ids["so"], ids["open_ao"]})
        self.assertEqual(summary["not_evaluated"][lr.RULE].get("order_status_unknown"), 1)
        on_order = lr.history(gaps, ids["open_ao"])["superseded_on_orders"]
        self.assertEqual([(o["revision"], o["current"]) for o in on_order], [("Rev 1", "Rev 2")])
        # the change rule reads the register: done by the new revision, approved with it
        change = lc.item_section(gaps, ids["item"])
        self.assertEqual((change["total"], change["no_approval"]), (1, 0))
        self.assertEqual(change["changes"][0]["done_by"], "file")
        self.assertEqual(change["changes"][0]["approved_on"], day(19).isoformat())
        # a reader without the library sees no superseded alert resting on it (the AO's),
        # still the one resting on mail it reads (the sales order's), and no revision whose
        # copies it cannot all read
        narrow = self.read(kinds=list(lr.KINDS), limit=1000,
                           may_read=lambda key: key != "sharepoint:drive-1")["gaps"]
        self.assertEqual({g["object_id"] for g in narrow if g["kind"] == lr.SUPERSEDED},
                         {ids["so"]})
        self.assertEqual([r["revision"] for f in lr.history(narrow, ids["item"])["families"]
                          for r in f["revisions"]], ["Rev 2"])
        # the superseded alert is off for readers until it is measured (alert registry)
        self.assertEqual(self.read(kinds=[lr.SUPERSEDED], include_off=False)["gaps"], [])

    def test_without_a_word_index_the_names_are_scanned_to_the_same_revisions(self):
        """Postgres serves no value word index yet: the names are read by type, a chunk of
        ids at a time, the conversations matched in the store and their mail read by id, and the
        same revisions come out (every chunk boundary crossed)."""
        from unittest import mock

        w, ids = self.world()
        # a revision file its mailbox no longer holds (removed at the source): neither
        # read finds it
        m = mail(w, day(20), sender=OUTSIDE, to=(OURS,), conversation="art-gone",
                 body="Here is the label.", subject="Label", n=59_999)
        w.link("Email.mentions_product", m, ids["item"])
        gone = attach(w, m, "Synth Gone Back Label Rev 7.pdf")
        w.rows["objects"][[r["object_id"] for r in w.rows["objects"]].index(gone)][
            "missing_from_source_since"] = "2026-10-05T00:00:00.000000Z"
        self.run_engine(w, only=[lr.RULE], people=people_with_drive())
        indexed = sorted((g["id"], g["summary"]) for g in self.read(
            kinds=list(lr.KINDS), limit=1000)["gaps"])
        self.assertTrue(indexed)
        self.assertNotIn("Gone Back Label", " ".join(s for _i, s in indexed))
        # one chunk after another and side by side on the store's threads: the same
        for threads in (1, 4):
            with self.subTest(threads=threads), \
                    mock.patch.object(pt, "_word_ids", return_value=None), \
                    mock.patch.object(lr, "SCAN_CHUNK", 1), \
                    mock.patch.object(pt, "MEMBER_CHUNK", 1), \
                    mock.patch.object(pe, "scan_threads", return_value=threads):
                summary = self.run_engine(w, only=[lr.RULE], people=people_with_drive())
                self.assertEqual(summary["detectors"][lr.RULE], "complete")
                scanned = sorted((g["id"], g["summary"]) for g in self.read(
                    kinds=list(lr.KINDS), limit=1000)["gaps"])
                self.assertEqual(scanned, indexed)

    def test_fails_closed_without_the_exclusion_and_reads_each_copy_once(self):
        w, _ids = self.world()
        blocked = self.run_engine(w, only=[lr.RULE], people=people(unavailable=True))
        self.assertEqual(blocked["detectors"][lr.RULE], "blocked")
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = World()
                item = w.obj("Product", {"code": "SYN-1"})
                for i in range(n):
                    m = mail(w, day(30 - i % 20), sender=OUTSIDE, to=(OURS,),
                             conversation=f"bulk-{i}", body="Label attached.",
                             subject=f"Label {i}", n=60_000 + i)
                    w.link("Email.mentions_product", m, item)
                    attach(w, m, f"Synth Item {i} Front Label Rev {1 + i % 3}.pdf")
                started = time.monotonic()
                self.run_engine(w, only=[lr.RULE], people=people_with_drive())
                gaps = self.read(kinds=[lr.REVISION], limit=1000)["gaps"]
                self.assertEqual(len(gaps), n)
                self.assertEqual(len({g["id"] for g in gaps}), n)
                self.assertLess(time.monotonic() - started, 30)


if __name__ == "__main__":
    unittest.main()
