"""Reply drafts written by the model (``operating.autopilot_drafter``) and the live cycle's
rule that a reply item exists only with a reply worth sending.

Owner walk 2026-11-17: 506 of 509 items had no draft text; mail produced none. The model
writes the reply from the message, the sender's own account and the person's style; a
figure the material does not hold withholds the draft; nothing useful, no item; excluded
people, pay/HR and Freightcom never reach the model. No test here calls a model: the
``claude`` run is a stand-in (``call``)."""
from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import subprocess
import unittest

from operating import autopilot as ap
from operating import autopilot_drafter as ad
from operating import proactive_engine as pe
from operating.tests import test_autopilot as ta
from operating.tests.test_autopilot_sender_scope import SenderScope
from operating.tests.test_payroll_marking import OFFER, PAYSTUB
from operating.tests.test_proactive_threads import OUTSIDE
from unittest import mock

COLLEAGUE = "jo.colleague@synth-tenant.example"
DOMAINS = ("synth-tenant.example",)

HOME = "/var/lib/plx-autopilot/claude-home"
BIN = "/usr/local/bin/claude"


class People:
    def __init__(self, barred=()) -> None:
        self.barred = set(barred)

    def identity_clear(self, who: str) -> bool:
        return who not in self.barred


class Model:
    """The ``claude -p`` stand-in: answers ``reply`` and keeps every prompt it was given."""

    def __init__(self, reply: str = "", *, raise_: BaseException | None = None,
                 answer: dict | None = None) -> None:
        self.reply, self.raise_, self.prompts = reply, raise_, []
        self.answer = answer

    def __call__(self, argv, *, input, **_kw):
        self.prompts.append(input)
        if self.raise_ is not None:
            raise self.raise_
        out = {"result": json.dumps(self.answer if self.answer is not None
                                    else {"reply": self.reply, "why": ""})}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr="")


def material(**kw) -> ad.Material:
    base = dict(reader="Vince", channel="mail", sender="Buyer at C100", subject="Order status",
                text="Hi Vince, where is order SO-7001? We need it by Friday.",
                account=["The sender is the customer Synth buyer C100.",
                         "Open order SO-7001, ordered 2026-10-10, status Open, due 2026-10-30."])
    base.update(kw)
    return ad.Material(**base)


EVENT = {"object_id": "Email:1", "sender": OUTSIDE, "cleared": True, "channel": "mail",
         "subject": "Order status", "text": "where is order SO-7001?"}


class WriterBoundary(unittest.TestCase):

    def write(self, model: Model, m: ad.Material | None = None, event=EVENT, people=None):
        w = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4, call=model)
        got, why = w.write(m or material(), event, people or People(), "vince@plx.example")
        return (got.text if got is not None and got.kind == "reply" else got), why

    def test_a_decision_only_the_person_has_is_a_card_never_a_hedging_draft(self):
        card = {"kind": "your_call", "ask": "Can we take 2% off SO-7001 if they order both?",
                "facts": ["Open order SO-7001, due 2026-10-30.", "They offered 99 units."]}
        w = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4,
                      call=Model(answer=card))
        got, why = w.write(material(), EVENT, People(), "vince@plx.example")
        self.assertEqual((got.kind, got.text, why), ("your_call", "", ""))
        self.assertEqual(got.ask, card["ask"])
        # a fact holding a figure the material does not state is dropped, never shown
        self.assertEqual(got.facts, ["Open order SO-7001, due 2026-10-30."])
        # a card asking about a figure the material does not hold is withheld
        bad = {**card, "ask": "Can we take 7% off order 4411?"}
        w = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4,
                      call=Model(answer=bad))
        self.assertEqual(w.write(material(), EVENT, People(), "v@plx.example"),
                         (None, "figure_not_in_material"))

    def test_an_unknown_kind_or_shape_fails_closed(self):
        for answer in ({"kind": "send_now", "reply": "Done."}, {"kind": "your_call", "ask": ""},
                       {"kind": "reply", "reply": "x", "facts": "not a list"}):
            w = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4,
                          call=Model(answer=answer))
            self.assertEqual(w.write(material(), EVENT, People(), "v@plx.example"),
                             (None, "draft_failed"), answer)

    def test_the_persons_line_becomes_the_reply_with_only_its_and_the_materials_figures(self):
        text = "Hi,\n\nYes, we can ship SO-7001 Friday with 2% off if you take both pallets.\n\nVince"
        model = Model(text)
        w = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4, call=model)
        line = "tell him yes, ship Friday, 2% off if he takes both pallets"
        self.assertEqual(w.from_line(material(), line, EVENT, People(), "v@plx.example"),
                         (text, ""))
        self.assertIn(line, model.prompts[0])
        self.assertIn("SO-7001", model.prompts[0])
        # a figure neither the line nor the material states is withheld
        w = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4,
                      call=Model("Yes, 5% off and it ships on the 14th."))
        self.assertEqual(w.from_line(material(), line, EVENT, People(), "v@plx.example"),
                         (None, "figure_not_in_material"))
        self.assertEqual(w.from_line(material(), "  ", EVENT, People(), "v@plx.example"),
                         (None, "empty_line"))
        # the line goes through the same screen as the message (Freightcom never reaches it)
        w = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4, call=Model("x"))
        self.assertEqual(w.from_line(material(), "book it with Freightcom", EVENT, People(),
                                     "v@plx.example"), (None, "freightcom"))

    def test_kept_material_round_trips_and_an_unknown_key_is_refused(self):
        m = material(style=["signs Vince"])
        kept = m.kept()
        self.assertNotIn("style", kept)
        self.assertEqual(ad.Material.of(kept, ["x"]).text, m.text)
        with self.assertRaises(ValueError):
            ad.Material.of({**kept, "extra": 1})
        with self.assertRaises(ValueError):
            ad.Material.of({**kept, "text": ""})

    def test_a_reply_stating_only_the_material_is_offered(self):
        text = "Hi,\n\nSO-7001 is open and due 2026-10-30. I will confirm the ship date today.\n\nVince"
        model = Model(text)
        self.assertEqual(self.write(model), (text, ""))
        prompt = model.prompts[0]
        for given in ("SO-7001", "due 2026-10-30", "where is order SO-7001", "Vince"):
            self.assertIn(given, prompt)

    def test_a_figure_the_material_does_not_hold_withholds_the_whole_draft(self):
        for made_up in ("It ships 2026-11-02.", "We have 4,500 kg ready.",
                        "Tracking 1Z999AA10123456784.", "The balance is 1,240.50 CAD."):
            with self.subTest(made_up):
                self.assertEqual(self.write(Model(f"Hi, SO-7001: {made_up}")),
                                 (None, "figure_not_in_material"))

    def test_an_amount_written_another_way_is_still_the_materials(self):
        m = material(account=["Invoice INV-1: 1240.50 CAD open, due 2026-11-30."])
        got = self.write(Model("Hi, invoice INV-1 has 1,240.50 CAD open, due 2026-11-30."), m)
        self.assertEqual(got[1], "")

    def test_an_empty_or_hollow_reply_prepares_nothing(self):
        for text in ("", "   ", "Hi John, thanks! I'll look into it.", "Thanks."):
            with self.subTest(text):
                self.assertEqual(self.write(Model(text)), (None, "nothing_useful"))

    def test_barred_people_pay_and_freightcom_never_reach_the_model(self):
        cases = [
            ({**EVENT, "cleared": False}, None, material(), "not_cleared"),
            (EVENT, People({OUTSIDE}), material(), "not_cleared"),
            (EVENT, People({"vince@plx.example"}), material(), "not_cleared"),
            ({**EVENT, "text": "Freightcom quote for the load"}, None, material(), "freightcom"),
            # in the account notes or the memory only: the whole prompt is checked
            (EVENT, None, material(account=["Open order SO-7001 via Freightcom."]), "freightcom"),
            (EVENT, None, material(style=["Freightcom bookings go through Ana."]), "freightcom"),
        ]
        for event, people, m, why in cases:
            with self.subTest(why=why, event=event.get("text")):
                model = Model("Hi, SO-7001 is open.")
                self.assertEqual(self.write(model, m, event, people), (None, why))
                self.assertEqual(model.prompts, [])

    def test_pay_or_hr_anywhere_in_what_the_model_would_read_withholds_the_draft(self):
        """Review 11-18: the screen read the latest message's preview while the model got
        the whole body and quoted thread; a pay stub or an offer letter below a harmless
        first line, in an account line or in the person's line now never reaches it."""
        for matter in (PAYSTUB, OFFER):
            thread = f"Hi Vince, quick one below.\n\nFrom: HR <hr@plx.example>\n{matter}"
            for m in (material(text=thread), material(account=[matter])):
                with self.subTest(matter=matter[:20]):
                    model = Model("Hi, SO-7001 is open.")
                    self.assertEqual(self.write(model, m), (None, "pay_or_hr"))
                    self.assertEqual(model.prompts, [])
            model = Model("Hi, SO-7001 is open.")
            w = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4, call=model)
            self.assertEqual(w.from_line(material(), f"tell him: {matter}", EVENT, People(),
                                         "vince@plx.example"), (None, "pay_or_hr"))
            self.assertEqual(model.prompts, [])

    def test_a_thread_an_excluded_person_wrote_in_never_reaches_the_model(self):
        """Excluded people's data never reaches the model, quoted or
        forwarded; an address nothing can decide withholds too."""
        sam = "sam.sample@plx.example"

        class Guard:
            def is_excluded(self, oid):
                return oid == "oid-sam"

        class Directory(People):
            directory = {sam: "oid-sam", "vince@plx.example": "oid-vince"}
            guard = Guard()

        class Undecided(People):
            def identity_clear(self, who):
                if who == "ops@plx.example":
                    raise RuntimeError("no directory")
                return True
        quoted = "Hi Vince, see below.\n\n> From: Sam Sample <{}>\n> Ship it Friday.".format(sam)
        cases = [(material(text=quoted), People({sam})),
                 (material(text="Thanks.\n\nFrom: Sam Sample\nSent: Monday\nShip it."),
                  Directory()),
                 (material(text="See below.\n\nOn Mon, Nov 16, 2026 at 9:00 AM Sam Sample "
                                "wrote:\n> Ship it."), Directory()),
                 (material(text="Fwd from ops@plx.example: ship it."), Undecided())]
        for m, people in cases:
            with self.subTest(m.text[:40]):
                model = Model("Hi, SO-7001 is open.")
                self.assertEqual(self.write(model, m, people=people),
                                 (None, "excluded_in_thread"))
                self.assertEqual(model.prompts, [])
        # a thread of cleared people still drafts
        ok = material(text="Hi Vince, where is SO-7001?\n\nFrom: Vince <vince@plx.example>\nOn it.")
        self.assertEqual(self.write(Model("Hi, SO-7001 is open."), ok, people=Directory())[1], "")

    def test_a_figure_stands_only_as_a_whole_token_of_the_material(self):
        """Review 11-18: '20 pallets at $26' passed as a substring of the material's 2026
        dates and '$700 per drum' inside SO-7001."""
        for made_up in ("We can ship 20 pallets at $26 each.", "Price is $700 per drum.",
                        "SO-7001 is due 2026-10-31.", "It is due Oct 31.",
                        "Invoice INV-1 has 1,240.50 USD open.", "The year is 2026.",
                        "PO 7002 is open."):
            with self.subTest(made_up):
                m = material(account=["Open order SO-7001, ordered 2026-10-10, due 2026-10-30.",
                                      "Invoice INV-1: 1240.50 CAD open, due 2026-11-30."])
                self.assertEqual(ad.check(made_up, m), "figure_not_in_material")
        for real in ("SO-7001 is due 2026-10-30.", "Order 7001 is due Oct 30.",
                     "It is due October 30th, 2026.", "Due 10/30.",
                     "INV-1 has $1,240.50 open.", "INV-1 has 1240.50 CAD open."):
            with self.subTest(real):
                m = material(account=["Open order SO-7001, ordered 2026-10-10, due 2026-10-30.",
                                      "Invoice INV-1: 1240.50 CAD open, due 2026-11-30."])
                self.assertIsNone(ad.check(real, m))

    def test_a_timeout_or_unreadable_answer_fails_and_is_tried_again(self):
        self.assertEqual(self.write(Model(raise_=subprocess.TimeoutExpired("claude", 5))),
                         (None, "draft_failed"))

        def garbage(argv, **_kw):
            return subprocess.CompletedProcess(argv, 0, stdout='{"result": "sure!"}', stderr="")
        w = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4, call=garbage)
        self.assertEqual(w.write(material(), EVENT, People(), "v"), (None, "draft_failed"))

    def test_no_model_named_means_no_call_and_an_operator_home_is_refused(self):
        for value in ("", "off", "none", "0"):
            self.assertIsNone(ad.from_environ({ad.MODEL_ENV: value}))
        model = Model("Hi, SO-7001 is open.")
        w = ad.Writer("opus", binary=BIN, home=str(Path.home()), call=model)
        self.assertEqual(w.write(material(), EVENT, People(), "v"), (None, "draft_model_unsafe"))
        self.assertEqual(model.prompts, [])

    def test_calls_a_cycle_are_capped(self):
        model = Model("Hi, SO-7001 is open.")
        w = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=2, call=model)
        for _ in range(2):
            w.write(material(), EVENT, People(), "v")
        self.assertTrue(w.busy)
        w.new_cycle()
        self.assertFalse(w.busy)


class AccountScope(ta.Fixture):
    """The account notes name only the sender's own customer's records."""
    new_customer = SenderScope.new_customer
    so = SenderScope.so
    invoice = SenderScope.invoice
    ctx = SenderScope.ctx

    def setUp(self) -> None:
        super().setUp()
        self.ids = []

    def lines(self, text: str, **kw) -> list[str]:
        msg = self.message(1, text, subject="Status", **kw)
        ctx = self.ctx()
        return ad.account_lines(ctx, {"object_id": msg, "subject": "Status", "text": text,
                                      "sender": kw.get("sender", OUTSIDE), "channel": "mail"},
                                text)

    def test_the_senders_open_orders_and_balance_and_never_another_customers(self):
        other = self.new_customer("C200")
        self.so("SO-9999", "45160", other)
        self.invoice("INV-9", other)
        self.so("SO-7001", "45160", self.customer)
        closed = self.w.obj("SalesOrder", {"order_number": "SO-6000", "status": "Closed",
                                           "order_date": "2026-10-01"})
        self.w.link("SalesOrder.ordered_by", closed, self.customer)
        self.invoice("INV-1", self.customer)
        got = "\n".join(self.lines("Where is PO 45160?"))
        self.assertIn("SO-7001", got)
        self.assertIn("INV-1: 40.00 CAD open, due 2026-11-30", got)
        for theirs in ("SO-9999", "INV-9", "C200", "SO-6000"):
            self.assertNotIn(theirs, got)

    def test_another_customers_record_never_reaches_the_material(self):
        """Review 11-18: a customer PO number another customer shares, or another
        customer's order named by number, put that customer's order in the material."""
        other = self.new_customer("C200")
        self.so("SO-9999", "45160", other)
        self.invoice("INV-9", other)
        self.so("SO-7001", "45160", self.customer)
        for i, text in enumerate(("Where is our PO 45160?", "Where is SO-9999?",
                                  "Is invoice INV-9 paid?")):
            with self.subTest(text):
                msg = self.message(10 + i, text, subject="Status")
                event = {"object_id": msg, "subject": "Status", "text": text,
                         "sender": OUTSIDE, "channel": "mail", "records": []}
                got = "\n".join(ad.material_of(self.ctx(DOMAINS), event, {}, text)["account"])
                for theirs in ("SO-9999", "INV-9", "C200"):
                    self.assertNotIn(theirs, got)
        # a sender no customer is held for gets no named record of anyone's
        msg = self.message(20, "Where is SO-9999?", subject="Status",
                           sender="someone@nowhere.example", customer=False)
        event = {"object_id": msg, "subject": "Status", "text": "Where is SO-9999?",
                 "sender": "someone@nowhere.example", "channel": "mail", "records": []}
        self.assertEqual(ad.named_lines(self.ctx(DOMAINS), event, event["text"]), [])

    def test_records_a_colleagues_message_names_by_number_reach_the_material(self):
        self.so("SO-7001", "45160", self.customer)
        msg = self.message(3, "Can you check SO-7001 before Friday?", sender=COLLEAGUE,
                           customer=False)
        event = {"object_id": msg, "subject": "Status", "side": "internal", "channel": "mail",
                 "text": "Can you check SO-7001 before Friday?", "sender": COLLEAGUE}
        got = ad.material_of(self.ctx(DOMAINS), event, {}, event["text"])["account"]
        self.assertTrue(any(line.startswith("Sales order SO-7001") for line in got), got)
        # a colleague still has no account of their own
        self.assertEqual(ad.account_lines(self.ctx(), event, event["text"]), [])

    def test_a_bare_number_names_a_record_only_when_something_backs_it(self):
        """The stray-PO case: a cancelled PO of another supplier whose digits sit bare in a
        customer's thread stays out; written as a PO, the sender's own, or open and recent,
        it comes in."""
        def po(number: str, status: str, day: str) -> str:
            oid = self.w.obj("PurchaseOrder", {"po_number": number, "status": status,
                                               "order_date": day})
            self.ids.append(("po_number", number, oid, "po_number"))
            return oid
        cancelled = po("2491", "Cancel", "2026-10-01")   # the word the source writes
        po("3301", "Open", "2026-05-05")            # open, but months old
        recent = po("3302", "Open", "2026-10-15")   # open and recent
        self.so("7001", "45160", self.customer)     # the sender's own order, digits only

        def named(text: str, sender: str = OUTSIDE) -> str:
            msg = self.message(4, text, subject="Re: pallets")
            event = {"object_id": msg, "subject": "Re: pallets", "text": text,
                     "sender": sender, "channel": "mail", "records": []}
            return "\n".join(ad.named_lines(self.ctx(DOMAINS), event, text))

        got = named("We can take 2491 cases and 3301 more next week; see 7001 and 3302.")
        self.assertNotIn("2491", got)
        self.assertNotIn("3301", got)
        self.assertIn("Sales order 7001", got)
        # our purchase orders are no customer's: a colleague's mail names them
        self.assertNotIn("3302", got)
        got = named("We can take 2491 cases and 3301 more next week; see 7001 and 3302.",
                    COLLEAGUE)
        self.assertNotIn("2491", got)
        self.assertNotIn("3301", got)
        self.assertIn("Purchase order 3302", got)
        got = named("About PO# 2491 and order no. 3301: are they still on?", COLLEAGUE)
        self.assertIn("Purchase order 2491", got)
        self.assertIn("Purchase order 3301", got)
        # a record the pipeline linked to the message stands on the same terms: a cancelled
        # PO whose number the message never writes stays out, an open recent one stays in
        msg = self.message(6, "Thanks, all good here.", subject="Re: pallets")
        event = {"object_id": msg, "subject": "Re: pallets", "text": "Thanks, all good here.",
                 "sender": COLLEAGUE, "channel": "mail", "records": [cancelled, recent]}
        got = "\n".join(ad.named_lines(self.ctx(DOMAINS), event, event["text"]))
        self.assertNotIn("2491", got)
        self.assertIn("Purchase order 3302", got)
        # the account material the writer gets carries no stray record either
        msg = self.message(5, "Ship 2491 units please", subject="Order")
        event = {"object_id": msg, "subject": "Order", "text": "Ship 2491 units please",
                 "sender": OUTSIDE, "channel": "mail", "records": []}
        self.assertFalse(any("2491" in line for line in
                             ad.material_of(self.ctx(), event, {}, event["text"])["account"]))

    def test_an_unresolved_sender_or_a_chat_gets_no_account(self):
        self.so("SO-7001", "45160", self.customer)
        self.assertEqual(self.lines("Where is PO 45160?", sender="someone@nowhere.example",
                                    customer=False), [])
        msg = self.message(2, "Where is it?")
        self.assertEqual(ad.account_lines(self.ctx(), {"object_id": msg, "channel": "teams",
                                                       "sender": OUTSIDE}, "x"), [])


class LiveCycle(ta.Fixture):
    """In the live queue a reply item carries the model's draft or is not made at all; a
    replay's queue keeps the handler's work and never calls a model."""

    def live(self, ids, writer, name="s.sqlite"):
        path = self.dir / name
        if not path.exists():
            self.w.build(path)
        st = pe._Store(path, threads=1)
        q = ap.Queue(self.dir / "autopilot" / ap.QUEUE_FILE)
        q.__dict__.setdefault("_draft_context", {})["writer"] = writer
        try:
            out = ap.run_cycle(st, q, ids, people=ta.people(), registry=None,
                               environ=self.env, now=ta.NOW,
                               landed_at=ap._iso(ta.NOW - timedelta(minutes=2)))
            items = [dict(r) for r in q.conn.execute("SELECT * FROM items")]
            return out, items
        finally:
            st.close()
            q.close()

    def writer(self, reply: str) -> tuple[ad.Writer, Model]:
        model = Model(reply)
        return ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4,
                         call=model), model

    def test_the_item_carries_the_models_reply_and_the_message(self):
        msg = self.message(60, "Could you send me the status of SO-1? Thanks")
        writer, model = self.writer("Hi,\n\nSO-1 is open; I will send the ship date today.")
        out, items = self.live([msg], writer)
        self.assertEqual(out["prepared"], {"customer_request": 1}, out)
        body = json.loads(items[0]["body"])["prepared"]
        self.assertEqual(body["draft"]["body"], "Hi,\n\nSO-1 is open; I will send the ship date today.")
        self.assertEqual(body["drafted_by"], "model")
        self.assertIn("status of SO-1", body["message"]["text"])
        self.assertNotIn("material", body)
        self.assertEqual(len(model.prompts), 1)

    def test_no_useful_reply_or_no_model_makes_no_item(self):
        msg = self.message(61, "Could you send me the status of SO-1? Thanks")
        writer, _ = self.writer("")
        out, items = self.live([msg], writer)
        self.assertEqual((out["prepared"], items), ({}, []))
        self.assertEqual(out["skipped"].get("customer_request:nothing_useful"), 1)
        again = self.message(62, "Could you send me the status of SO-1? Thanks")
        out, items = self.live([again], None, "t.sqlite")
        self.assertEqual(items, [])
        self.assertEqual(out["skipped"].get("customer_request:draft_model_off"), 1)

    def test_a_your_call_item_keeps_the_card_and_material_but_no_draft_text(self):
        msg = self.message(64, "Could you send me the status of SO-1? Thanks")
        card = {"kind": "your_call", "ask": "Do we ship SO-1 early?", "facts": []}
        model = Model(answer=card)
        writer = ad.Writer("opus", binary=BIN, home=HOME, seconds=5, per_cycle=4, call=model)
        out, items = self.live([msg], writer)
        body = json.loads(items[0]["body"])["prepared"]
        self.assertEqual(body["your_call"]["ask"], "Do we ship SO-1 early?")
        self.assertIn("status of SO-1", body["your_call"]["material"]["text"])
        self.assertFalse((body.get("draft") or {}).get("body"))
        # the envelope stays (the line's reply is confirmed on the usual path) and the card
        # keeps what the screens read again
        self.assertEqual(body["draft"]["body"], "")
        self.assertTrue(body["draft"].get("to"), body["draft"])
        kept = body["your_call"]["event"]
        self.assertTrue(kept.get("cleared"))
        self.assertTrue(kept.get("sender"))
        self.assertLessEqual(set(kept), set(ap.CARD_EVENT))

    def test_a_dry_run_writes_nothing_and_sends_nothing(self):
        msg = self.message(65, "Could you send me the status of SO-1? Thanks")
        path = self.dir / "d.sqlite"
        self.w.build(path)
        st = pe._Store(path, threads=1)
        model = Model("Hi,\n\nSO-1 is open.")
        queue_path = self.dir / "dry" / "autopilot" / ap.QUEUE_FILE
        env = {**self.env, ad.MODEL_ENV: "opus"}
        before = sorted(p for p in self.dir.rglob("*") if p.is_file())
        try:
            with mock.patch.object(ad, "from_environ", lambda _e: ad.Writer(
                    "opus", binary=BIN, home=HOME, seconds=5, per_cycle=4, call=model)):
                rows = ap.dry_drafts(st, ap.load_registry(), ta.people(), [msg], queue_path,
                                     environ=env)
        finally:
            st.close()
        self.assertTrue(rows and all(r.get("why") is not None for r in rows), rows)
        self.assertEqual(len(model.prompts), sum(1 for r in rows if r.get("draft")))
        self.assertFalse(queue_path.parent.exists())
        after = sorted(p for p in self.dir.rglob("*") if p.is_file())
        self.assertEqual([p for p in after if p not in before and "ledger" not in str(p)], [])

    def test_a_replay_queue_never_calls_the_model(self):
        msg = self.message(63, "Could you send me the status of SO-1? Thanks")
        self.assertEqual(self.cycle([msg])["prepared"], {"customer_request": 1})


if __name__ == "__main__":
    unittest.main()


class ReaderAndSenderTests(unittest.TestCase):
    """The model is told who it writes as and who the sender is to them (11-18 grade:
    drafts signed with a name from the thread, a colleague taken for a customer)."""

    def test_reader_name_comes_from_the_thread_beside_their_own_address(self):
        from operating import autopilot_drafter as ad
        text = ("@Kim Lee<mailto:kim@ours.example> FYI.\nFrom: Kim Lee <kim@ours.example>\n"
                "From: Pat Roe <pat@other.example>")
        self.assertEqual(ad.name_in(text, "kim@ours.example"), "Kim Lee")
        self.assertEqual(ad.name_in(text, "nobody@ours.example"), "")
        self.assertEqual(ad.name_in(text, ""), "")

    def test_prompt_names_the_reader_their_address_and_the_sender_relation(self):
        from operating import autopilot_drafter as ad
        m = ad.Material(reader="Kim Lee", channel="mail", sender="pat@ours.example",
                        subject="s", text="t", address="kim@ours.example",
                        relation=ad.relation_of({"side": "internal"}))
        p = ad.prompt_of(m)
        self.assertIn("Kim Lee (kim@ours.example)", p)
        self.assertIn("a colleague at their own company", p)
        self.assertIn("never with another person's name", p)
        self.assertEqual(ad.relation_of({"side": "weird"}), "an outside contact")
        self.assertEqual(ad.relation_of({}), "an outside contact")

    def test_cc_prepares_only_when_the_message_asks_the_person_by_name(self):
        from operating.autopilot_events import named_in
        mine = {"vince@plx.example", "vince2614@plx.onmicrosoft.example"}
        maps = dict(directory={a: "oid-v" for a in mine}, addresses_of={"oid-v": mine},
                    roles={})
        cc = {"event_type": "message.received", "mailbox": "oid-v", "to_me": False,
              "asks": True, "channel": "mail"}
        named = named_in("Vince, can you confirm the price?", mine)
        self.assertTrue(named)
        self.assertEqual(ap.owners_of("recipient", {**cc, "named_me": named}, **maps),
                         (["oid-v"], None))
        for text in ("Hi V! can you confirm?", "Please write to vince@plx.example",
                     "Vincent, can you confirm?"):
            self.assertFalse(named_in(text, mine), text)
            self.assertEqual(ap.owners_of("recipient", {**cc, "named_me": named_in(text, mine)},
                                          **maps), ([], "owner_not_addressed"))
        # named but asking nothing: still the To person's
        self.assertEqual(ap.owners_of("recipient", {**cc, "named_me": True, "asks": False},
                                      **maps), ([], "owner_not_addressed"))
        self.assertFalse(named_in("Sales, can you confirm?", {"sales@plx.example"}))

    def test_a_colleague_has_no_account(self):
        from operating import autopilot_drafter as ad
        # never reaches the store: an internal sender is nobody's customer or supplier
        self.assertEqual(ad._party(None, {"side": "internal", "channel": "mail"}, "x"),
                         ("", [], ""))

    def test_only_the_handlers_nothing_found_skip_lets_the_model_decide(self):
        from operating import autopilot_engine as ae
        self.assertEqual(ae.MODEL_DECIDES, frozenset({"nothing_to_prepare"}))
