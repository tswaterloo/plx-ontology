"""Promises we made and mail waiting on a reply (``operating.proactive_commitments``)
through the engine and the per-reader inbox.

SYNTHETIC fixtures only (``World`` stores, synthetic handles and addresses); evidence label:
isolated-fixture.
"""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3
import time
import unittest

from operating import briefing
from operating import proactive_commitments as pcm
from operating import proactive_engine as pe
from operating.tests.test_proactive_chasers import (CUSTOMER_MAIL, TextWorld, chat_people,
                                                    customer, order)
from operating.tests.test_proactive_engine import (AS_OF, EXCLUDED_OID, OURS_OID, T0, Engine,
                                                   World, admit, everyone, people)
from ontology.store import StoreBuilder

RULES = [pcm.COMMITMENTS, pcm.NEEDS_REPLY]
OURS = "ops@synth-tenant.example"
OWNER = f"plx:employee:{OURS_OID}"
OTHER_EMPLOYEE = "plx:employee:00000000-0000-4000-8000-0000000000b2"
MAILBOX = f"mail:{OURS_OID}"
PROMISE = "Thanks for the order. We will ship it by Friday. Internal note: margin 41%."


def day(offset: int) -> date:
    return AS_OF - timedelta(days=offset)


def mail(w: World, so: str, sent: date, *, sender: str = OURS, to=(CUSTOMER_MAIL,), cc=(),
         body: str = PROMISE, subject: str = "Re: your order " + "y" * 80,
         mailbox: str = OURS_OID, conversation: str = "conv-1", n: int | None = None,
         tier: str = "verified") -> str:
    oid = w.obj("Email", {"subject": subject, "sender": sender,
                          "to_addresses": json.dumps(list(to)),
                          "cc_addresses": json.dumps(list(cc)),
                          "sent_at": f"{sent.isoformat()}T15:00:00Z", "body_preview": body,
                          "conversation_id": conversation, "mailbox": mailbox,
                          "internet_message_id": f"<c{n if n is not None else w.n}@synth>"},
                binding="graph_mail.messages", audience=f"mail:{mailbox}")
    w.link("Email.mentions_sales_order", oid, so, tier)
    return oid


class RecentWorld(World):
    """A store whose mail has the projection's ``Email.received`` events (the index the
    widened read lists recent mail by)."""

    def __init__(self, *, indexed: bool = True) -> None:
        super().__init__()
        self.rows.update(events=[], event_objects=[])
        self.indexed = indexed

    def build(self, path):
        builder = StoreBuilder(path)
        for table, rows in self.rows.items():
            builder.add(table, rows)
        builder.set_meta("audience_keys", sorted({r["audience_key"]
                                                   for r in self.rows["objects"]}))
        builder.commit(built_at=T0)
        if self.indexed:   # the Postgres store's events_by_type, on this SQLite file
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE INDEX events_by_type ON events(event_type, time)")
        return path


def sent(w: RecentWorld, when: date, *, so: str | None = None, sender: str = OURS,
         to=(CUSTOMER_MAIL,), body: str = "Your samples will be ready by Monday.",
         mailbox: str = OURS_OID, conversation: str = "r-1", n: int | None = None) -> str:
    """A message of the recent mail, with its received event; ``so``: an order it names."""
    oid = w.obj("Email", {"subject": "Samples", "sender": sender,
                          "to_addresses": json.dumps(list(to)), "cc_addresses": "[]",
                          "sent_at": f"{when.isoformat()}T15:00:00Z", "body_preview": body,
                          "conversation_id": conversation, "mailbox": mailbox,
                          "internet_message_id": f"<r{n if n is not None else w.n}@synth>"},
                binding="graph_mail.messages", audience=f"mail:{mailbox}")
    if so is not None:
        w.link("Email.mentions_sales_order", oid, so)
    event = f"E:{oid}"
    w.rows["events"].append({
        "event_id": event, "event_type": "Email.received", "time_basis": "source_recorded",
        "time": f"{when.isoformat()}T15:00:00.000000Z", "l1_handle": f"h:{oid}",
        "field_path": "row.received_at", "digest": "d", "lineage": "mail", "markings": "[]",
        "recorded_at": T0})
    w.rows["event_objects"].append({"event_id": event, "object_id": oid,
                                    "qualifier": "subject"})
    return oid


class Commitments(Engine):

    def run_rules(self, w: World, **options) -> dict:
        return self.run_engine(w, only=RULES, **options)

    def kinds(self, *kinds: str, **read) -> list[dict]:
        return self.read(kinds=list(kinds), limit=1000, **read)["gaps"]

    # ------------------------------------------------------------ phrases and dates

    def test_promise_families_and_dates_read_from_the_message_day(self):
        sent = date(2026, 11, 4)   # a Wednesday
        for text, family, what, due in (
                ("We will ship by Friday.", "ship", "the order", date(2026, 11, 6)),
                ("I'll send the COA tomorrow.", "send", "COA", date(2026, 11, 5)),
                ("PO to follow next week.", "to_follow", "PO", date(2026, 11, 13)),
                ("We'll confirm the ETA by end of day", "confirm", "date", sent),
                ("I will send tracking within 2 business days", "send", "tracking",
                 date(2026, 11, 6)),
                ("Your order will be shipped on November 20th", "ship", "the order",
                 date(2026, 11, 20)),
                ("We will email the invoice next Monday", "send", "invoice",
                 date(2026, 11, 9)),
                ("We'll get back to you by the end of the month", "confirm", "an update",
                 date(2026, 11, 30)),
                ("I will check with the plant and get back to you tomorrow", "confirm",
                 "an update", date(2026, 11, 5)),
                ("We will process the order today", "arrange", "the order", sent),
                ("I will get it to you by Friday", "send", "what was asked",
                 date(2026, 11, 6)),
                ("It ships on Friday.", "ship_state", "the order", date(2026, 11, 6)),
                ("Your order is expected to ship next week", "ship_state", "the order",
                 date(2026, 11, 13)),
                ("It will be picked up tomorrow", "ship_state", "the order",
                 date(2026, 11, 5)),
                ("The samples will be ready by Monday", "ready", "samples",
                 date(2026, 11, 9)),
                ("ETA is Friday.", "arrive", "the order", date(2026, 11, 6)),
                ("I will send the quote on 11/12", "send", "quote", None)):   # ambiguous
            with self.subTest(text=text):
                found = pcm.promise(text, sent)
                self.assertEqual((found["family"], found["what"], found["due"]),
                                 (family, what, due))
        for text in ("We will not be able to ship by Friday.", "Can you ship by Friday?",
                     "We will need your PO before we can ship.",
                     "> We will ship by Friday\nThanks, received.",
                     "Please find the invoice attached.", "Will it ship on Friday?",
                     "It should arrive soon.", "What is the ETA",
                     "Please let me know the ETA by Friday.", "We need it ready by Monday.",
                     "Unfortunately it is not going to be ready by Monday.",
                     "If we receive your PO today, it ships on Friday.",
                     "We can ship by Friday if you approve the quote.",
                     "Can we ship by Friday", "Is it possible to have it ready by Monday",
                     "The ETA from the supplier was Friday.",
                     "Once approved we will ship Monday.",
                     "As soon as the COA is in, we will send it tomorrow."):
            with self.subTest(text=text):
                self.assertIsNone(pcm.promise(text, sent))

    # ------------------------------------------------------------ commitments

    def test_a_missed_promise_is_raised_from_our_mail_without_its_body(self):
        w = World()
        who = customer(w)
        so = order(w, who, (AS_OF + timedelta(days=30)).isoformat())
        promised = mail(w, so, day(10), n=1)                       # Friday after it: missed
        mail(w, so, day(10), mailbox="00000000-0000-4000-8000-0000000000b2", n=1)   # copy
        mail(w, so, day(10), mailbox=EXCLUDED_OID, n=2)             # excluded mailbox
        mail(w, so, day(10), to=("boss@synth-tenant.example",), n=3)   # excluded person on it
        mail(w, so, day(10), to=(OURS,), n=4)                       # ours only
        mail(w, so, day(10), sender=CUSTOMER_MAIL, to=(OURS,), n=5)    # the customer's
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pcm.COMMITMENTS], "complete")
        (gap,) = self.kinds(pcm.MISSED, pcm.DUE)
        self.assertEqual((gap["kind"], gap["object_id"]), (pcm.MISSED, so))
        friday = day(10) + timedelta(days=(4 - day(10).weekday()) % 7)
        event = gap["event"]
        self.assertEqual(event["promise"], f"Ship the order by {friday.isoformat()} "
                                           "(order SYNTH-SO-1)")
        self.assertLessEqual(len(event["promise"]), pcm.SUMMARY_CHARS)
        self.assertEqual(event["to"], who)
        self.assertEqual(event["promised_by"], OURS)
        self.assertEqual([m["id"] for m in event["messages"]], [promised])
        self.assertTrue(all(len(m["subject"]) <= pcm.SUBJECT_CHARS for m in event["messages"]))
        self.assertTrue(gap["summary"].startswith("We promised: Ship the order by "))
        self.assertNotIn("margin", json.dumps(gap))
        self.assertNotIn("Thanks for the order", json.dumps(gap))
        skipped = summary["not_evaluated"][pcm.COMMITMENTS]
        self.assertEqual(skipped.get("message_not_cleared"), 1)
        self.assertEqual(skipped.get("participants_not_cleared"), 1)
        self.assertEqual(skipped.get("internal_only"), 1)

    def test_a_promise_due_today_is_due_and_one_answered_later_is_met(self):
        w = World()
        who = customer(w)
        so = order(w, who, (AS_OF + timedelta(days=30)).isoformat(), number="SYNTH-DUE")
        mail(w, so, day(1), body="I'll send the COA today, sorry for the wait.")   # yesterday
        mail(w, so, AS_OF, body="We will confirm the date today.")   # a new promise: not met
        met = order(w, who, (AS_OF + timedelta(days=30)).isoformat(), number="SYNTH-MET")
        mail(w, met, day(6), body="I'll send the COA tomorrow.", conversation="conv-2")
        mail(w, met, day(4), body="Attached as promised.", conversation="conv-2")
        self.run_rules(w)
        by_kind = {g["kind"]: g for g in self.kinds(pcm.MISSED, pcm.DUE)}
        self.assertEqual(set(by_kind), {pcm.MISSED, pcm.DUE})
        self.assertEqual(by_kind[pcm.DUE]["event"]["promise"],
                         f"Confirm date by {AS_OF.isoformat()} (order SYNTH-DUE)")
        self.assertEqual({g["object_id"] for g in self.kinds(pcm.MISSED, pcm.DUE)}, {so})

    def test_a_dated_promise_to_a_customer_naming_no_open_order_comes_from_recent_mail(self):
        w = RecentWorld()
        who = customer(w)
        open_so = order(w, who, (AS_OF + timedelta(days=30)).isoformat(), number="SYNTH-OPEN")
        shipped = order(w, who, day(40).isoformat(), number="SYNTH-SHIPPED", status="Shipped")
        promised = sent(w, day(10), n=1)                         # samples ready: missed
        sent(w, day(10), mailbox="00000000-0000-4000-8000-0000000000b2", n=1)   # its copy
        sent(w, day(10), mailbox=EXCLUDED_OID, n=2)              # excluded mailbox
        sent(w, day(10), to=("someone@synth-unknown.example",), n=3)   # not a customer
        sent(w, day(10), so=shipped, body="It ships on Friday.", conversation="r-2", n=4)
        sent(w, day(12), body="We will send the COA tomorrow.", conversation="r-3", n=5)
        sent(w, day(11), body="Attached as promised.", conversation="r-3", n=6)   # met
        sent(w, day(10), sender=CUSTOMER_MAIL, to=(OURS,), n=7)   # the customer's
        sent(w, day(10), body="Thanks, noted.", conversation="r-4", n=8)   # promises nothing
        sent(w, day(60), n=9)                                    # before the window
        sent(w, day(10), so=open_so, body="It ships on Friday.", n=10)   # the order rule's
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pcm.COMMITMENTS], "complete")
        gaps = self.kinds(pcm.MISSED, pcm.DUE)
        self.assertEqual(sorted(g["object_id"] for g in gaps), sorted([promised, open_so]))
        (gap,) = [g for g in gaps if g["object_id"] == promised]
        monday = day(10) + timedelta(days=(0 - day(10).weekday()) % 7)
        self.assertEqual(gap["kind"], pcm.MISSED)
        self.assertEqual(gap["event"]["promise"], f"Have ready samples by {monday.isoformat()}")
        self.assertEqual(gap["event"]["to"], who)
        self.assertNotIn("Your samples", json.dumps(gap))
        stored = json.loads(json.dumps(gap))
        self.assertLessEqual(len(stored["event"]["promise"]), pcm.SUMMARY_CHARS)
        counts = summary["not_evaluated"][pcm.COMMITMENTS]
        self.assertEqual(counts.get("recent_message_not_cleared"), 1)
        # a reader without the customer's records or the mailbox sees nothing of it
        for hidden in (MAILBOX, "api:customers"):
            with self.subTest(hidden=hidden):
                self.assertEqual([g["object_id"] for g in self.kinds(
                    pcm.MISSED, pcm.DUE, may_read=lambda a, h=hidden: a != h)
                    if g["object_id"] == promised], [])

    def test_recent_mail_past_its_time_budget_is_counted_never_judged(self):
        w = RecentWorld()
        customer(w)
        sent(w, day(10), n=1)
        budget = pcm.RECENT_SECONDS
        pcm.RECENT_SECONDS = -1.0
        try:
            summary = self.run_rules(w)
        finally:
            pcm.RECENT_SECONDS = budget
        self.assertEqual(self.kinds(pcm.MISSED, pcm.DUE), [])
        self.assertEqual(summary["not_evaluated"][pcm.COMMITMENTS]
                         .get("recent_mail_over_budget"), 1)

    def test_a_recent_read_longer_than_the_window_stops_inside_it_and_needs_reply_refreshes(self):
        """The shared read is bound by what is left of the promise rule's window: it keeps
        the chunks it read, counts the rest unread, and finishes in time, so the reply rule
        (reading the same population from the memo) is not cut off and still refreshes."""
        import time
        w = RecentWorld()
        who = customer(w)
        so = order(w, who, (AS_OF + timedelta(days=30)).isoformat(), number="SYNTH-ASK")
        mail(w, so, day(6), sender=CUSTOMER_MAIL, to=(OURS,), conversation="conv-9",
             body="Could you confirm the delivery date?")
        recent = {sent(w, day(10), to=(CUSTOMER_MAIL,), conversation=f"r-{n}", n=100 + n)
                  for n in range(20)}
        slow, chunk = pcm._Population._audiences, pcm.RECENT_CHUNK

        def audiences(self, ids):   # a store read slower than the window
            if set(ids) & recent:
                time.sleep(1.5)   # the recent mail's chunks
            return slow(self, ids)
        pcm._Population._audiences, pcm.RECENT_CHUNK = audiences, 1
        started = time.monotonic()
        try:
            summary = self.run_rules(w, detector_seconds={r: 6.0 for r in RULES})
        finally:
            pcm._Population._audiences, pcm.RECENT_CHUNK = slow, chunk
        self.assertLess(time.monotonic() - started, 6.0)
        for rule in RULES:
            self.assertEqual(summary["detectors"][rule], "complete")
        self.assertGreater(summary["not_evaluated"][pcm.COMMITMENTS]
                           .get("recent_mail_over_budget", 0), 0)
        self.assertEqual(len(self.kinds(pcm.NEEDS_MY_REPLY, reader=OWNER)), 1)

    def test_mail_left_unread_by_one_pass_is_continued_by_the_next_never_read_twice(self):
        """A saved cursor: every pass reads the newest mail not yet judged, within its time;
        a message judged is not read again; within a few passes all of it is judged, and the
        promise is found; a pass over a window that moved on forgets what fell out of it."""
        import time
        w = RecentWorld()
        customer(w)
        ids = [sent(w, day(10 + n % 5), body="Thanks, noted.", conversation=f"r-{n}", n=n)
               for n in range(2, 14)]
        promised = sent(w, day(30), n=1)   # the oldest: read last
        slow, chunk = pcm._Population._audiences, pcm.RECENT_CHUNK
        seen: list[str] = []

        def audiences(self, got):
            seen.extend(o for o in got if o in ids or o == promised)
            if set(got) & ({promised, *ids}):
                time.sleep(2.0)
            return slow(self, got)
        pcm._Population._audiences, pcm.RECENT_CHUNK = audiences, 1
        unread = []
        try:
            for _ in range(8):
                summary = self.run_rules(w, detector_seconds={r: 6.0 for r in RULES})
                left = summary["not_evaluated"][pcm.COMMITMENTS].get("recent_mail_over_budget", 0)
                unread.append(left)
                if not left:
                    break
        finally:
            pcm._Population._audiences, pcm.RECENT_CHUNK = slow, chunk
        self.assertGreater(unread[0], 0)
        self.assertEqual(unread[-1], 0)
        self.assertLess(len(unread), 8)
        self.assertEqual(len(seen), len(set(seen)))   # none read twice
        self.assertEqual([g["object_id"] for g in self.kinds(pcm.MISSED, pcm.DUE)], [promised])

    def test_a_newly_excluded_mailbox_leaves_no_id_in_the_saved_cursor(self):
        """The cursor rests on the people exclusion: once a person is excluded, no id of
        their mailbox's mail stays saved (kept or rejected), and an exclusion that cannot
        say whom it excludes reuses no verdict."""
        from integrations.people_exclusion import PeopleExclusion
        from operating.chunking.permissions import Exclusion
        other = "00000000-0000-4000-8000-0000000000b1"
        w = RecentWorld()
        customer(w)
        to_customer = sent(w, day(10), mailbox=other, n=1)
        not_customer = sent(w, day(10), mailbox=other, to=("x@synth-unknown.example",),
                            conversation="r-2", n=2)
        mine = sent(w, day(10), to=("y@synth-unknown.example",), conversation="r-3", n=3)
        path = Path(str(self.inbox) + ".commitments-recent.json")

        def saved() -> set[str]:
            body = json.loads(path.read_text())
            return {*body["rejected"], *body["kept"], *body["days"]}

        self.run_rules(w)
        self.assertLessEqual({to_customer, not_customer, mine}, saved())
        both = Exclusion(PeopleExclusion([EXCLUDED_OID, other]),
                         directory=people().directory, internal_domains=["synth-tenant.example"])
        summary = self.run_rules(w, people=both)
        self.assertEqual(summary["detectors"][pcm.COMMITMENTS], "complete")
        self.assertEqual(saved() & {to_customer, not_customer}, set())
        self.assertIn(mine, saved())
        self.assertNotIn(other, path.read_text())

        class Opaque:   # an exclusion with no fingerprint: no verdict reused
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                if name == "fingerprint":
                    raise AttributeError(name)
                return getattr(self.inner, name)
        digests = set()
        for _ in range(2):
            self.run_rules(w, people=Opaque(people()))
            digests.add(json.loads(path.read_text())["digest"])
        self.assertEqual(len(digests), 2)

    def test_a_message_judged_unnamed_is_read_again_once_a_link_names_an_order(self):
        """Ours to an outside address of no customer is rejected for now; an order link
        applied after that verdict makes the next pass read it again."""
        w = RecentWorld()
        who = customer(w)
        shipped = order(w, who, day(40).isoformat(), number="SYNTH-SHIPPED", status="Shipped")
        note = sent(w, day(10), to=("z@synth-unknown.example",), body="It ships on Friday.",
                    n=1)
        self.run_rules(w)
        body = json.loads(Path(str(self.inbox) + ".commitments-recent.json").read_text())
        self.assertIn(note, body["unnamed"])
        w.link("Email.mentions_sales_order", note, shipped)
        self.run_rules(w)
        body = json.loads(Path(str(self.inbox) + ".commitments-recent.json").read_text())
        self.assertIn(note, body["kept"])
        self.assertNotIn(note, body["rejected"])

    def test_a_failed_address_book_read_is_an_error_not_a_wait(self):
        w = RecentWorld()
        customer(w)
        sent(w, day(10), n=1)
        book = pcm._Population._book

        def broken(self):
            raise RuntimeError("synthetic")
        pcm._Population._book = broken
        try:
            started = time.monotonic()
            summary = self.run_rules(w, detector_seconds={r: 30.0 for r in RULES})
        finally:
            pcm._Population._book = book
        self.assertLess(time.monotonic() - started, 15.0)
        counts = summary["not_evaluated"][pcm.COMMITMENTS]
        self.assertEqual(counts.get("recent_mail_unread_error"), 1)
        self.assertNotIn("recent_mail_over_budget", counts)

    def test_a_cursor_forgets_a_changed_book_and_messages_past_the_window(self):
        import tempfile
        path = Path(tempfile.mkdtemp()) / "c.json"
        cur = pcm._Cursor(path, "d1", "2026-10-01")
        cur.rejected, cur.kept = {"Email:a", "Email:old"}, {"Email:k": (["c"], "mail:x", True)}
        cur.save({"Email:a": "2026-11-01", "Email:old": "2026-09-01", "Email:k": "2026-11-02"})
        again = pcm._Cursor(path, "d1", "2026-10-01")
        self.assertEqual((again.rejected, set(again.kept)), ({"Email:a"}, {"Email:k"}))
        self.assertEqual(pcm._Cursor(path, "d2", "2026-10-01").rejected, set())   # book changed
        self.assertEqual(pcm._Cursor(path, "d1", "2026-11-02").rejected, set())   # window moved
        path.write_text("not json")
        self.assertEqual(pcm._Cursor(path, "d1", "2026-10-01").kept, {})

    def test_a_cursor_of_the_whole_recent_mail_saves_and_loads_in_seconds(self):
        import tempfile
        path = Path(tempfile.mkdtemp()) / "c.json"
        cur = pcm._Cursor(path, "d", "2026-10-01")
        days = {f"Email:{n}": "2026-11-01" for n in range(100000)}
        cur.rejected = {o for n, o in enumerate(days) if n % 4}
        cur.kept = {o: (["c"], "mail:x", False) for n, o in enumerate(days) if not n % 4}
        started = time.monotonic()
        cur.save(days)
        again = pcm._Cursor(path, "d", "2026-10-01")
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual(len(again.rejected) + len(again.kept), 100000)

    def test_a_recent_read_in_many_chunks_judges_what_the_one_chunk_read_judges(self):
        w = RecentWorld()
        customer(w)
        promised = sent(w, day(10), n=1)
        for n in range(2, 9):
            sent(w, day(10), body="Thanks, noted.", conversation=f"r-{n}", n=n)
        sent(w, day(10), mailbox=EXCLUDED_OID, n=20)
        chunk = pcm.RECENT_CHUNK
        pcm.RECENT_CHUNK = 2
        try:
            summary = self.run_rules(w)
        finally:
            pcm.RECENT_CHUNK = chunk
        self.assertEqual([g["object_id"] for g in self.kinds(pcm.MISSED, pcm.DUE)],
                         [promised])
        counts = summary["not_evaluated"][pcm.COMMITMENTS]
        self.assertEqual(counts.get("recent_message_not_cleared"), 1)
        self.assertNotIn("recent_mail_over_budget", counts)

    def test_a_store_without_the_events_time_index_does_not_read_recent_mail(self):
        w = RecentWorld(indexed=False)   # a SQLite generation: no (event_type, time) index
        customer(w)
        sent(w, day(10), n=1)
        summary = self.run_rules(w)
        self.assertEqual(self.kinds(pcm.MISSED, pcm.DUE), [])
        self.assertEqual(summary["not_evaluated"][pcm.COMMITMENTS]
                         .get("recent_mail_not_read_no_index"), 1)

    def test_without_the_people_exclusion_both_rules_are_blocked(self):
        w = World()
        so = order(w, customer(w), (AS_OF + timedelta(days=30)).isoformat())
        mail(w, so, day(10))
        summary = self.run_rules(w, people=people(unavailable=True))
        for rule in RULES:
            self.assertEqual(summary["detectors"][rule], "blocked")
        self.assertEqual(self.kinds(pcm.MISSED, pcm.DUE, pcm.NEEDS_MY_REPLY, reader=OWNER), [])

    def test_a_restricted_reader_sees_no_promise_or_reply_it_may_not_open(self):
        w = World()
        so = order(w, customer(w), (AS_OF + timedelta(days=30)).isoformat())
        mail(w, so, day(10))
        mail(w, so, day(6), sender=CUSTOMER_MAIL, to=(OURS,), conversation="conv-9",
             body="Could you confirm the delivery date?")
        w.obj("Shipment", {"shipment_number": "SYNTH-SH"}, audience="bc:shipments")
        self.run_rules(w)
        self.assertEqual(len(self.kinds(pcm.MISSED, pcm.NEEDS_MY_REPLY, reader=OWNER)), 2)
        for hidden in (MAILBOX, "bc:shipments", "api:orders"):
            with self.subTest(hidden=hidden):
                self.assertEqual(self.kinds(pcm.MISSED, pcm.DUE, pcm.NEEDS_MY_REPLY,
                                            reader=OWNER,
                                            may_read=lambda a, h=hidden: a != h), [])

    # ------------------------------------------------------------ needs my reply

    def test_needs_my_reply_is_the_owners_alone_and_ranked_by_business_value(self):
        w = World()
        who = customer(w)
        quiet = order(w, who, (AS_OF + timedelta(days=30)).isoformat(), number="SYNTH-Q")
        late = order(w, who, day(3).isoformat(), number="SYNTH-LATE")
        asks = "Hi, could you confirm when this ships? Our price was 777."
        first = mail(w, quiet, day(9), sender=CUSTOMER_MAIL, to=(OURS,), body=asks,
                     conversation="c-q")
        second = mail(w, late, day(7), sender=CUSTOMER_MAIL, to=(OURS,), body=asks,
                      conversation="c-l")
        answered = order(w, who, (AS_OF + timedelta(days=30)).isoformat(), number="SYNTH-A")
        mail(w, answered, day(9), sender=CUSTOMER_MAIL, to=(OURS,), body=asks,
             conversation="c-a")
        mail(w, answered, day(8), body="Thanks, it ships next week.", conversation="c-a")
        mail(w, quiet, day(9), sender=CUSTOMER_MAIL, to=("other@synth-customer.example",),
             cc=(OURS,), body=asks, conversation="c-cc")              # a copy to them only
        mail(w, quiet, AS_OF, sender=CUSTOMER_MAIL, to=(OURS,), body=asks,
             conversation="c-new")                                    # not waiting yet
        mail(w, quiet, day(9), sender=CUSTOMER_MAIL, to=(OURS,), body="Thanks, received.",
             conversation="c-thx")                                    # asks nothing
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pcm.NEEDS_REPLY], "complete")
        gaps = self.kinds(pcm.NEEDS_MY_REPLY, reader=OWNER)
        self.assertEqual([g["object_id"] for g in gaps], [second, first])   # late order first
        self.assertEqual(gaps[0]["follow_up"]["priority"], "high")
        self.assertEqual(gaps[0]["event"]["overdue_orders"], 1)
        self.assertNotIn("777", json.dumps(gaps))
        # nobody else sees or counts it: an administrator, another employee, a service
        for reader in ("plx:ops", OTHER_EMPLOYEE, f"plx:service:{OURS_OID}",
                       f"plx:employee:{OURS_OID}x"):
            with self.subTest(reader=reader):
                self.assertEqual(self.kinds(pcm.NEEDS_MY_REPLY, reader=reader), [])
                with self.assertRaises(PermissionError):
                    pe.acknowledge(self.inbox, self.store, gaps[0]["id"], reader=reader,
                                   may_read=everyone, held=frozenset(), decide=admit)
        self.assertEqual(pe.hidden_counts(self.inbox, self.store, kinds=[pcm.NEEDS_MY_REPLY],
                                          may_read=lambda a: False, held=frozenset(),
                                          decide=admit), {})
        found = briefing.read(self.inbox, self.store, reader=OTHER_EMPLOYEE, may_read=everyone,
                              held=frozenset({"finance", "l5", "pii", "quality_controlled"}),
                              decide=admit, routing=briefing.Routing(everything=True))
        self.assertNotIn(pcm.NEEDS_MY_REPLY, [i["event_type"] for i in found["items"]])
        mine = briefing.read(self.inbox, self.store, reader=OWNER, may_read=everyone,
                             held=frozenset({"finance", "l5", "pii", "quality_controlled"}),
                             decide=admit, routing=briefing.Routing(everything=True))
        self.assertIn(pcm.NEEDS_MY_REPLY, [i["event_type"] for i in mine["items"]])

    def test_an_inbound_question_naming_no_order_is_judged_from_recent_mail(self):
        w = RecentWorld()
        customer(w)
        ask = "Hi, could you tell me when the samples will arrive?"
        waiting = sent(w, day(9), sender="buyer@synth-stranger.example", to=(OURS,), body=ask,
                       conversation="q-1", n=1)
        sent(w, day(9), sender="buyer@synth-stranger.example", to=(OURS,), body=ask,
             conversation="q-2", n=2)                             # answered below
        sent(w, day(8), sender=OURS, to=("buyer@synth-stranger.example",),
             body="They arrive next week.", conversation="q-2", n=3)   # a reply to no customer
        sent(w, day(9), sender="no-reply@synth-stranger.example", to=(OURS,), body=ask,
             conversation="q-3", n=4)                             # automated: skipped
        sent(w, AS_OF, sender="buyer@synth-stranger.example", to=(OURS,), body=ask,
             conversation="q-4", n=5)                             # not waiting yet
        sent(w, day(9), sender="buyer@synth-stranger.example", to=("other@synth-tenant.example",),
             body=ask, conversation="q-5", mailbox=OURS_OID, n=6)   # not to the owner
        sent(w, day(9), sender="buyer@synth-stranger.example", to=(OURS,), body=ask,
             conversation="q-6", mailbox=EXCLUDED_OID, n=7)       # an excluded mailbox
        sent(w, day(9), sender="buyer@synth-stranger.example", to=(OURS,),
             body="Thanks, received.", conversation="q-7", n=8)   # asks nothing
        summary = self.run_rules(w)
        self.assertEqual(summary["detectors"][pcm.NEEDS_REPLY], "complete")
        gaps = self.kinds(pcm.NEEDS_MY_REPLY, reader=OWNER)
        self.assertEqual([g["object_id"] for g in gaps], [waiting])
        gap = gaps[0]
        self.assertEqual(gap["follow_up"]["priority"], "low")
        self.assertEqual(gap["event"]["orders_affected"], 0)
        self.assertNotIn("open order", gap["follow_up"]["why"])
        self.assertNotRegex(gap["summary"], r"SYNTH|SO-|\border\b")
        self.assertNotIn("samples will arrive", json.dumps(gap))
        # the owner's alone, as the order-bound ones are
        self.assertEqual(self.kinds(pcm.NEEDS_MY_REPLY, reader=OTHER_EMPLOYEE), [])
        # the commitments rule does not read them as promises
        self.assertEqual(self.kinds(pcm.MISSED, pcm.DUE), [])

    def test_mail_whose_owner_or_recipients_are_not_known_fails_closed(self):
        w = World()
        so = order(w, customer(w), (AS_OF + timedelta(days=30)).isoformat())
        asks = "Could you confirm the date?"
        bad = mail(w, so, day(8), sender=CUSTOMER_MAIL, to=(OURS,), body=asks,
                   conversation="c1")
        w.rows["object_values"] = [r for r in w.rows["object_values"]
                                   if not (r["object_id"] == bad and r["property"] == "mailbox")]
        mail(w, so, day(8), sender=CUSTOMER_MAIL, to=(OURS,), body=asks, conversation="c2",
             mailbox="not-an-oid")
        mail(w, so, day(8), sender=CUSTOMER_MAIL, body=asks, conversation="c3",
             mailbox="00000000-0000-4000-8000-0000000000b2",
             to=("someone@synth-customer.example",))           # an owner with no address
        oid = mail(w, so, day(8), sender=CUSTOMER_MAIL, body=asks, conversation="c4")
        for r in w.rows["object_values"]:
            if r["object_id"] == oid and r["property"] == "to_addresses":
                r["value"] = "{not json"
        summary = self.run_rules(w)
        self.assertEqual(self.kinds(pcm.NEEDS_MY_REPLY, reader=OWNER), [])
        self.assertEqual(self.kinds(pcm.NEEDS_MY_REPLY,
                                    reader="plx:employee:00000000-0000-4000-8000-0000000000b2"),
                         [])
        skipped = summary["not_evaluated"][pcm.NEEDS_REPLY]
        self.assertEqual(skipped.get("mailbox_owner_unknown"), 1)   # no mailbox stated
        self.assertEqual(skipped.get("message_not_cleared"), 1)     # a mailbox no one owns
        self.assertEqual(skipped.get("owner_address_unknown"), 1)

    def test_a_one_to_one_teams_question_waits_on_the_other_member(self):
        w = TextWorld()
        so = order(w, customer(w), (AS_OF + timedelta(days=30)).isoformat())
        guest = "00000000-0000-4000-8000-0000000000c3"
        asked = w.teams(so, day(8).isoformat(), "19:one", guest, body="Can you check this?")
        w.teams(so, day(8).isoformat(), "19:group", guest, body="Can you check this?")
        w.teams(so, day(8).isoformat(), "19:done", guest, body="Can you check this?")
        w.teams(so, day(7).isoformat(), "19:done", OURS_OID, body="Done, it ships Monday.")
        members = {"19:one": (OURS_OID, guest), "19:done": (OURS_OID, guest),
                   "19:group": (OURS_OID, guest, "00000000-0000-4000-8000-0000000000c4")}
        summary = self.run_rules(w, people=chat_people(members))
        self.assertEqual([g["object_id"] for g in self.kinds(pcm.NEEDS_MY_REPLY,
                                                             reader=OWNER)], [asked])
        self.assertEqual(summary["not_evaluated"][pcm.NEEDS_REPLY]["chat_not_one_to_one"], 1)

    # ------------------------------------------------------------ briefing

    def test_every_new_kind_has_a_next_step_and_its_words(self):
        for kind in (pcm.DUE, pcm.MISSED, pcm.NEEDS_MY_REPLY):
            with self.subTest(kind=kind):
                self.assertIn(kind, briefing.EVENT_TYPES)
                self.assertIn(kind, briefing.NEXT_ACTIONS)
                self.assertIn(kind, briefing.TODAY_WORDS)
                self.assertEqual(briefing.HIDDEN_POLICY[kind], "none")
        self.assertTrue(set(RULES) <= set(pe.REQUIRES_GRANT) & set(pe.DETECTOR_SECONDS))

    # ------------------------------------------------------------ cardinality

    def test_every_promise_and_question_is_judged_once_at_1_10_and_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                w = RecentWorld()
                promised, asked = [], []
                for i in range(n):
                    address = f"buyer{i}@synth-c{i}.example"
                    so = order(w, customer(w, address),
                               (AS_OF + timedelta(days=30)).isoformat(), number=f"SYNTH-{i}")
                    mail(w, so, day(10), to=(address,), conversation=f"p{i}")
                    promised.append(so)
                    promised.append(sent(w, day(10), to=(address,), conversation=f"s{i}"))
                    asked.append(mail(w, so, day(8), sender=address, to=(OURS,),
                                      body="Could you send the COA?", conversation=f"q{i}"))
                started = time.monotonic()
                summary = self.run_rules(w)
                self.assertLess(time.monotonic() - started, 60)
                for rule in RULES:
                    self.assertEqual(summary["detectors"][rule], "complete")
                self.assertEqual(sorted(g["object_id"] for g in self.kinds(pcm.MISSED)),
                                 sorted(promised))
                self.assertEqual(sorted(g["object_id"] for g in self.kinds(
                    pcm.NEEDS_MY_REPLY, reader=OWNER)), sorted(asked))
                self.inbox.unlink()
                self.store.unlink()


if __name__ == "__main__":
    unittest.main()
