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
from unittest import mock

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
        # zero-width spaces in a person's signature are not a mailing's padding (a supplier's
        # reply was once cut for them); the preheader's joiners are
        self.assertFalse(it.bulk("jane@synth-supplier.example", "Can you confirm the freight? "
                                 "Jane Roe \u200b \u200b \u200b \u200b \u200b Sales"))
        self.assertTrue(it.bulk("jane@synth-supplier.example", "Our news \u200c\u200b\u034f"
                                "\u200b\u200c \u034f\u00ad"))
        # the shapes that reached "Needs your reply" (2026-11-17, counted on the live list):
        # a vendor's onboarding and workshop mail, a mailing's preheader, an issue tracker's
        # notice, a billing notice, a role or news sender, a platform's sending subdomain
        for sender, text, subject in (
                ("brand365@communication.synth-vendor.example",
                 "New to the suite? Chat helps you start strong. Want a tour?", "Welcome to Chat"),
                ("ops@synth-vendor.example", "View this email as a web page. Can a bot help?",
                 "Live workshops: what could a bot take off your plate?"),
                ("ops@synth-vendor.example", "Having trouble reading this e-mail? Ready?", ""),
                ("support@synth-desk.example", "--REPLY above this line to respond-- Awaiting "
                 "client response: can you confirm?", "SR#1"),
                ("site@synth-shop.example", "Hi Admin, there is a new subscription. This is a "
                 "notification that a form was submitted. Can you check?", "New Message"),
                ("no.reply@billing.synth-carrier.example", "Can you pay?", "Invoice"),
                ("wordpress@synth-shop.example", "Can you check?", ""),
                ("acmecanadaenews@synth-maker.example", "Can a scanner help you?", ""),
                ("info@campaign.synth-courses.example", "Can you join?", ""),
                ("a@synth.example", "Some people who received this message don't often get "
                 "email from a@synth.example. Will you join us?", ""),
                ("ebill@synth-gas.example", "Hello. Your eBill is ready. Can you pay by Friday?",
                 ""),
                ("a@ealerts.synth-bank.example", "Your account notices are now available. "
                 "Can you review?", "")):
            with self.subTest(sender=sender, subject=subject):
                self.assertTrue(it.bulk(sender, text, subject))
        # people: a customer, a supplier, a colleague's forward, a supplier's own mail host,
        # a person whose name ends in letters a news sender's would
        for sender, text, subject in (
                ("diana.wood@synth-group.example", "Please see the attached NDA, can you sign "
                 "and share back at your earliest?", "NDA"),
                ("dispatch@synth-logistics.example", "What is the latest they can come today?",
                 "Re: PO#5354"),
                ("andrews@synth-supplier.example", "Can you confirm the invoice?", ""),
                ("sales8@synth-supplier.example", "Now you change the cap in matte? pls double "
                 "check it.", "RE: FW: Art"),
                ("pat@synth-customer.example", "FYI, can you take this one?", "FW: Order")):
            with self.subTest(sender=sender, subject=subject):
                self.assertFalse(it.bulk(sender, text, subject))
        for text, want in (("Can you kindly update the case label format below?", True),
                           ("We need to update the case labels to include the origin.", True),
                           ("Is there any update on the proof of payment?", False),
                           ("This is a private label opportunity, can you update me?", False),
                           ("Please see revised labels.", False),
                           ("I already updated the labels, please check.", False),
                           ("No need to change the artwork.", False)):
            with self.subTest(text=text):
                self.assertIs(lc.change_request(text), want)


class NotAskedOfReader(unittest.TestCase):
    """Rows A-D of the needs-reply list (2026-11-17, from a sample of the kept list)."""

    def test_mention_of_a_colleague_is_theirs(self) -> None:
        mine, staff = ["vince.alton@synth.example"], ["maria.hooper@synth.example"]
        ask = "@Maria Hooper can you issue a credit for the damaged pail?"
        self.assertTrue(it.mentions_another(ask, mine, staff))
        # the owner among the mentioned, a mention of no known person, or no mention: kept
        self.assertFalse(it.mentions_another("@Maria Hooper @Vince Alton can you both check?",
                                             mine, staff))
        self.assertFalse(it.mentions_another("@Synth Soap can you confirm?", mine, staff))
        self.assertFalse(it.mentions_another("Maria, can you check?", mine, staff))
        self.assertFalse(it.mentions_another("Mail jane@Maria.example please", mine, staff))
        # a role mailbox (info@) a colleague is @mentioned in: the colleague's
        self.assertTrue(it.mentions_another(ask, ["info@synth.example"], staff))
        # a last name the owner's initial-led address holds
        self.assertFalse(it.mentions_another("@Vince Alton can you check?",
                                             ["valton@synth.example"], staff))

    def test_a_note_greeting_another_by_name_is_theirs(self) -> None:
        mine = ["vince.alton@synth.example"]
        others = ["sw-tina@synth.example", "maria.hooper@synth.example", "all@synth.example"]
        for text in ("Tina, can you send the COA?", "Hi Maria, could you check the PO?",
                     "Good morning Tina: please confirm the pickup.",
                     "Good morning Maria and Tina, can you advise on the below?",
                     "Hi Maria You have the rendering, why do it again?",
                     "Hi Tina\uff0c the second shipment is ready, can you advise?"):
            self.assertTrue(it.greets_another(text, mine, others), text)
        # the owner among the greeted, or one greeted name no one holds: his
        self.assertFalse(it.greets_another("Hi Maria and Vince, can you?", mine, others))
        self.assertFalse(it.greets_another("Maria, Jane, can you?", mine, others))
        for text in ("Vince, can you send the COA?", "Vin, can you send the COA?",
                     "All, can you send the COA?", "Thanks, can you send the COA?",
                     "Tin, can you send the COA?",       # a bare part of a name: no
                     "Maria You have the rendering",     # a bare name without its comma
                     "Can you send the COA, Tina?"):     # not an opening
            self.assertFalse(it.greets_another(text, mine, others), text)

    def test_a_greeted_name_no_address_holds(self) -> None:
        # "Hi Jane," in mail To the owner and a supplier's sales8@: Jane's, not his
        mine, to = ["vince.alton@synth.example"], ["sales8@synth-supplier.example"]
        ask = "Hi Jane, can you send the COA?"
        self.assertTrue(it.greets_another(ask, mine, to, [*to, *mine]))
        # kept: To him alone (his nickname), a bare "Jane," (any opening word), a function
        # mailbox ("info@", "sales8@"), a shared initial ("Hi Al," to alton), our company
        for args in ((ask, mine, to, mine), ("Jane, can you send it?", mine, to, [*to, *mine]),
                     (ask, ["info@synth.example"], to, [*to, "info@synth.example"]),
                     (ask, ["sales8@synth.example"], to, [*to, "sales8@synth.example"]),
                     ("Hi Al, can you send it?", mine, to, [*to, *mine]),
                     ("Hi Synth, can you send it?", mine, to, [*to, *mine])):
            self.assertFalse(it.greets_another(*args), args[0])

    def test_an_indirect_question_asks(self) -> None:
        for text in ("I want to understand if the new labels are part of our next PO.",
                     "We\u2019d like to know whether the order ships Friday.",
                     "I was wondering if you had the COA for lot 5."):
            self.assertTrue(it.reply_ask(text), text)
        self.assertFalse(it.reply_ask("I want to understand the market better."))

    def test_offers_are_pitches_not_inquiries(self) -> None:
        for text in ("Would you like to receive a sample file of our contact list?",
                     "Can I stop by to show you our handheld measurement tool?",
                     "Could we help with your fabrication? Happy to quote.",
                     "Do you need ultra-high purity ingredients? Send me your price list."):
            self.assertFalse(it.inquiry(text), text)
        # a stranger's real inquiry is still one
        self.assertTrue(it.inquiry("Could you quote 5,000 units of the 300 ml body wash?"))
        self.assertTrue(it.inquiry("What MOQ do you need for private label hand soap?"))

    def test_closers_ask_nothing(self) -> None:
        for text in ("Attached are the time sheets. Please let me know if there are any "
                     "discrepancies or queries.",
                     "We will ship Friday. If anything changes, let me know.",
                     "Please kindly note our holiday schedule for the coming months.",
                     "Noted. Can you leave reps off PO emails in the future?",
                     "Please send a new PO for any additional ribbon required.",
                     "Want to pay through online banking with your bank?"):
            self.assertFalse(it.reply_ask(text), text)
        for text in ("Please let me know the ship date for PO 1234.",
                     "Please note the price changed; can you confirm the new PO?",
                     "Please note the price changed, is that OK?"):
            self.assertTrue(it.reply_ask(text), text)

    def test_person_chase_and_all_caps_ask_are_kept(self) -> None:
        # a customer's own chase is a person's, even with the phrase and a number
        self.assertFalse(it.bulk("buyer@synth-customer.example", "Any news on this?",
                                 "Re: Quote 4471 - waiting for your response"))
        self.assertFalse(it.bulk("buyer@synth-customer.example", "Any news?",
                                 "Quote 4471 - waiting for your response"))
        self.assertFalse(it.bulk("support@synth-vendor.example", "Hi", "Re: SR123456 | Waiting for your response"))
        shout = ("WHERE IS MY ORDER 4471 IT WAS PROMISED LAST WEEK AND I STILL HAVE NOTHING "
                 "PLEASE CALL ME TODAY")
        self.assertFalse(it.bulk("buyer@synth-customer.example", shout, "ORDER 4471"))
        self.assertTrue(it.bulk("noreply@synth-agency.example", shout, "ORDER 4471"))

    def test_ticket_reminder_subject_is_machine(self) -> None:
        self.assertTrue(it.bulk("support@synth-vendor.example", "Hello, any news on this?",
                                "SR123456 | Waiting for your response"))
        self.assertFalse(it.bulk("jane@synth-supplier.example", "Any news on PO 12?",
                                 "RE: PO 12 freight"))
        self.assertTrue(it.bulk("help@synth-vendor.example", "Your request was updated.",
                                "SR12345678 for Can you reset my login has been updated"))
        notice = ("PLEASE BE ADVISED THE COUNCIL PROCESSED A PAYMENT THAT WILL BE DEPOSITED IN "
                  "YOUR ACCOUNT IN THE NEXT FEW DAYS. PLEASE CONFIRM RECEPTION BY E-MAIL.")
        self.assertTrue(it.bulk("payments@synth-agency.example", notice, "DEPOSIT NOTICE"))
        self.assertFalse(it.bulk("jane@synth-supplier.example", "PO 12: PLEASE CONFIRM ASAP.",
                                 "URGENT"))   # a few capitals in a person's note


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

    def test_a_note_opening_on_a_colleagues_name_is_theirs(self):
        # E2: "Lead, could you ..." (no "Hi") in the owner's mailbox puts the ask to the
        # colleague whose address is lead@; one opening on the owner's own name stays his
        w = World()
        ask(w, 1, body="Lead, could you confirm the pallet height for the next order?")
        ask(w, 2, body="Hi lead - could you confirm the carton count for the next order?")
        kept = ask(w, 3, body="Ops, could you confirm the lot number for the next order?")
        named_no_one = ask(w, 4, body="Thanks, could you confirm the ship window for the "
                                      "next order?")
        self.assertEqual(self.run_rules(w)["detectors"][it.RULE], "complete")
        self.assertEqual({g["object_id"] for g in self.mine(OURS_OID)}, {kept, named_no_one})

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

class PlatformMail(Engine):
    """Mail a mailing platform sent (the envelope the capture stored) waits on no one, unless
    a platform relays one person's message or one of ours wrote in its conversation first;
    the envelope is read from the capture's own records, read-only (``mail_envelope``)."""

    ESP = ["mx.synth-tenant.example; spf=pass smtp.mailfrom=bounce.s7.exacttarget.com; "
           "dkim=pass header.d=s7.y.mc.salesforce.com"]
    OWN = ["mx.synth-tenant.example; spf=pass smtp.mailfrom=synth-shop.example; "
           "dkim=pass header.d=synth-shop.example"]

    def capture(self, records) -> Path:
        """A mail capture holding ``records`` ((mailbox, id, auth results, current))."""
        import sqlite3
        from operating import declared_schema
        from operating.declared_schema import capture_mail
        path = Path(tempfile.mkdtemp()) / "capture.db"
        conn = sqlite3.connect(path)
        declared_schema.apply(conn, capture_mail)
        for n, (box, ident, auth, current) in enumerate(records):
            record = {"id": ident, "mailbox": box, "from": OUTSIDE}
            if auth is not None:
                record["authentication_results"] = auth
            conn.execute(
                "INSERT INTO messages (l1_handle, mailbox_id, message_id, change_key, folder_id,"
                " record, digest, body_status, recorded_at, superseded_at, last_seen_at) "
                "VALUES (?, ?, ?, 'ck', 'f', ?, 'd', 'complete', 't', ?, 't')",
                (f"h{n}", box, ident, json.dumps(record), None if current else "t2"))
        conn.commit()
        conn.close()
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    run_rules, mine = Triage.run_rules, Triage.mine

    @staticmethod
    def oid(box: str, ident: str) -> str:
        from ontology.project import object_id_of
        return object_id_of("Email", "graph_mail.messages", {"id": ident, "mailbox": box})

    def test_the_stored_envelope_names_the_platform_read_only_and_n_1_10_60(self):
        from operating import mail_envelope as me
        box = "00000000-0000-4000-8000-0000000000aa"
        path = self.capture([
            (box, "bulk", self.ESP, True), (box, "own", self.OWN, True),
            (box, "bare", None, True), (box, "odd", "spf=pass smtp.mailfrom=em1.x.example", True),
            (box, "moved", self.ESP, False), (box, "moved", self.OWN, True)])
        read = me.reader(path)
        got = read([self.oid(box, i) for i in ("bulk", "own", "bare", "odd", "moved", "gone")]
                   + ["Email:fx:1", "Customer:graph_mail.messages:{}", "Email:graph_mail."
                      "messages:not-json"])
        # the platform's envelope, a company's own, the current version only; a record with
        # no header (or not a list), one not held, an id of any other shape: no answer
        self.assertEqual(got, {self.oid(box, "bulk"): True, self.oid(box, "own"): False,
                               self.oid(box, "moved"): False})
        self.assertEqual(me.reader(path.parent / "absent.db")([self.oid(box, "bulk")]), {})
        before = path.read_bytes()
        for n in (1, 10, 60):
            with self.subTest(n=n):
                many = self.capture([(box, f"m{i}", self.ESP if i % 2 else self.OWN, True)
                                     for i in range(n)])
                started = time.monotonic()
                got = me.reader(many)([self.oid(box, f"m{i}") for i in range(n)])
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(got, {self.oid(box, f"m{i}"): bool(i % 2) for i in range(n)})
        self.assertEqual(path.read_bytes(), before)   # never written

    def test_platform_mail_is_automated_unless_relayed_or_in_our_thread(self):
        w = World()
        bulk = ask(w, 1)
        relay = ask(w, 2, body="Could you confirm the ship window? For your safety, always "
                                   "communicate through the site.")
        mail(w, day(6), sender=OURS, to=(OUTSIDE,), body="Here are the specs.",
             subject="Order 3", conversation="ask-3", n=20_103)
        threaded = ask(w, 3)
        unknown = ask(w, 4)
        verdict = {bulk: True, relay: True, threaded: True, unknown: False}
        guard = people()
        guard.mail_platform = lambda oids: {o: verdict[o] for o in oids if o in verdict}
        summary = self.run_rules(w, people=guard)
        self.assertEqual({g["object_id"] for g in self.mine(OURS_OID)},
                         {relay, threaded, unknown})
        self.assertEqual(summary["detectors"][it.RULE], "complete")
        guard.mail_platform = lambda oids: 1 / 0   # an unreadable capture drops nothing
        self.run_rules(w, people=guard)
        self.assertEqual({g["object_id"] for g in self.mine(OURS_OID)},
                         {bulk, relay, threaded, unknown})


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


    def test_only_the_window_chat_messages_are_read_whole(self):
        """The chat messages before the detector's window: only their time is read, never
        their sender or text (before, every one-to-one message ever captured was read
        whole, then filtered by date: 77 s of a 60 s window live). N=1/10/60 in the window
        beside as many older ones: each in-window ask found once, no older one read."""
        ask_text = "Could you confirm the pallet count for tomorrow?"
        heavy = set(it.TEAMS_PROPS) - set(it.WHEN_PROPS) | {"subject"}
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w, guard = World(), self.guard(n)
                old, new = set(), set()
                for i in range(n):
                    other = f"{i:08x}-0000-4000-8000-00000000a{i:03x}"
                    pair = self.PAIR.format(OURS_OID, other)
                    new.add(self.chat(w, pair, other, 4, ask_text))
                    old.add(self.chat(w, pair, other, it.WINDOW_DAYS + 10, ask_text))
                asked: list[tuple[set, set]] = []
                real = pt._read

                def spy(st, ids, props):
                    asked.append((set(ids), set(props)))
                    return real(st, ids, props)
                with mock.patch.object(it.pt, "_read", spy):
                    summary = self.run_rules(w, people=guard)
                self.assertEqual(summary["detectors"][it.RULE], "complete")
                found = {g["object_id"] for g in self.mine(OURS_OID)}
                self.assertEqual(found, new)
                self.assertFalse([ids & old for ids, props in asked
                                  if props & heavy and ids & old])
                # every in-window message was read whole
                self.assertTrue(any(new <= ids and props & heavy for ids, props in asked))


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
        # a stranger's first note that only questions the reader is no inquiry: a pitch
        # offering its own goods, a newsletter asking in its subject, a seller's "do you need"
        for i, (subject, body) in enumerate((
                ("Re: Your website ranking", "Hi, may I send you our packages and price "
                 "list? Please let me know."),
                ("Is a first home in your future?", "Why opening an account before year-end "
                 "could help. Visit my website."),
                ("Any lab equipment on your list?", "Hi, is there any equipment your lab "
                 "needs to purchase before December? Can you let me know?"),
                ("Hair dryer details", "Adding a high-speed option to your range? I can send "
                 "product photos and samples. Would you like them?"))):
            mail(w, day(4), sender=f"rep{i}@synth-pitch{i}.example", to=(OURS,), body=body,
                 subject=subject, conversation=f"pitch-q{i}", n=30_010 + i)
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
                + [{"kind": "mc_task_stuck", "owner": None, "teams": ["OPS"]}]
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
            # named: a request is stale only when it names its item by a stated name
            # (alerts-fix-1113, label_changes._subject_names)
            w._item = w.obj("Product", {"code": "SYN-1", "name": "Synthetic case label"})
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
                # live 11-12: documents about a label asked for an artwork approval
                ("2026-08-19_GEN_Synth Cleaner Label Specs_v01.pdf", None),
                ("2026-08-19_GEN_Synth Bottle Drawing and Labeling Specs_v01.pdf", None),
                ("2026-08-19_GEN_Synth Packaging Dieline Instructions_v01.pdf", None),
                ("2026-08-19_GEN_PO_Purchase Order PO-SYN-1 - Synth labels reprint_v01.pdf",
                 None),
                ("Synth Dieline Instructions and Approval_v02.pdf", None),
                ("1700000000000-UAT-SYN-20260907-R5-SYNTHETIC-ARTWORK-REFERENCE v1.pdf",
                 None),                                             # a UAT test file
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

    def test_only_the_newest_unapproved_revision_asks_once_across_its_scopes(self):
        """live 11-12: 8 of 15 label_revision items asked for an approval of a revision a
        higher one superseded, or one approved, or the same revision once per thread, party
        and folder. The ask is read across every scope holding the family's name; the rest
        stays page history."""
        w = World()

        def library(name, at):
            return w.obj("SharePointFile", {"name": name,
                                            "last_modified": f"{at.isoformat()}T10:00:00Z",
                                            "path": f"/Artwork/Synth/{name}"},
                         binding="sharepoint.drive_items", audience="sharepoint:drive-1")

        back1 = attach(w, mail(w, day(40), sender=OUTSIDE, to=(OURS,), conversation="b-1",
                               body="Back label attached.", subject="Back", n=51_001),
                       "Synth Soap Back Label Rev 1.pdf")
        back2 = library("Synth Soap Back Label Rev 2.ai", day(20))
        back2_mail = attach(w, mail(w, day(18), sender=OURS, to=(APPROVER,),
                                    conversation="b-2", body="Back label rev 2 attached.",
                                    subject="Back rev 2", n=51_002),
                            "Synth Soap Back Label Rev 2.pdf")
        lotion = attach(w, mail(w, day(15), sender=OURS, to=(OUTSIDE,), conversation="l-1",
                                body="Lotion label attached.", subject="Lotion", n=51_003),
                        "Synth Lotion Front Label v3.pdf")
        mail(w, day(14), sender=OUTSIDE, to=(OURS,), conversation="l-1",
             body="Approved, OK to print.", subject="Re: Lotion", n=51_004)
        lotion_lib = library("Synth Lotion Front Label v3.ai", day(16))
        summary = self.run_engine(w, only=[lr.RULE], people=people_with_drive())
        self.assertEqual(summary["detectors"][lr.RULE], "complete")
        asks = self.read(kinds=[lr.REVISION], limit=1000)["gaps"]
        self.assertEqual([g["object_id"] for g in asks], [back2])
        kept = self.read(kinds=[lr.HISTORY], limit=1000)["gaps"]
        self.assertEqual(sorted(g["object_id"] for g in kept),
                         sorted([back1, back2_mail, lotion, lotion_lib]))
        superseded = next(g for g in kept if g["object_id"] == back1)
        self.assertIn("superseded by revision Rev 2", superseded["summary"])
        # a reader of the mail alone still sees no ask resting on a copy it cannot open
        mail_only = self.read(kinds=[lr.REVISION], limit=1000,
                              may_read=lambda key: key != "sharepoint:drive-1")["gaps"]
        self.assertEqual(mail_only, [])

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
