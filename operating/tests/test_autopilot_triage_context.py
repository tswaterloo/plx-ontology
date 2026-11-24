"""What the triage model reads beside a message (``autopilot_triage.earlier_of``): the
conversation just before it, the reader's own marked, screened before any of it is shown,
and never kept on the event. Evidence: isolated-fixture."""
from __future__ import annotations

import unittest
from unittest import mock

from operating import autopilot_triage as at
from operating.tests import test_autopilot_chat as tc
from operating.tests import test_autopilot_replay as trp
from operating.tests.test_autopilot_triage import StubTriage as _Stub

READER = "maria@synth-tenant.example"
QUOTE = ("99 gallons, in WH 11.\nThanks,\n\nFrom: Maria Hooper <maria@synth-tenant.example>\n"
         "Sent: Tuesday\nTo: Vern\nSubject: drums\n\nHow many gallons came in for PO 20304?")


class People:
    def __init__(self, barred=()):
        self.barred = set(barred)

    def identity_clear(self, who):
        return who.lower() not in self.barred


def mail_earlier(body, people=People(), reader=READER):
    with mock.patch("operating.autopilot_work.body_text", lambda ctx, event: body):
        return at.earlier_of(None, None, {"channel": "mail", "object_id": "Email:x"}, reader,
                             ["maria"], {reader: {reader}}, people, lambda: None)


class Earlier(unittest.TestCase):
    def test_a_mail_shows_the_message_it_answers_and_whose_it_was(self):
        got = mail_earlier(QUOTE)
        self.assertEqual(got, ["the reader (their own): How many gallons came in for PO 20304?"])
        other = QUOTE.replace("Maria Hooper <maria@synth-tenant.example>", "Vern <v@x.example>")
        self.assertEqual(mail_earlier(other), ["Vern: How many gallons came in for PO 20304?"])
        self.assertEqual(mail_earlier("No quote here."), [])
        self.assertLessEqual(len(mail_earlier(QUOTE + "x" * 2000)[0]), at.EARLIER_CHARS)

    def test_a_screened_earlier_part_is_never_shown(self):
        """The earlier part passes the drafts' screen first: an excluded person's quoted
        message, pay or HR matter in it shows nothing of it."""
        barred = QUOTE.replace("Maria Hooper <maria@synth-tenant.example>",
                               "Boss <boss@synth-tenant.example>")
        self.assertEqual(mail_earlier(barred, People(barred={"boss@synth-tenant.example"})), [])
        from operating.tests.test_payroll_marking import PAYSTUB
        self.assertEqual(mail_earlier(QUOTE.replace("How many gallons came in for PO 20304?",
                                                    PAYSTUB)), [])
        with mock.patch("operating.autopilot_work.body_text", side_effect=RuntimeError):
            self.assertEqual(at.earlier_of(None, None, {"channel": "mail"}, READER, [], {},
                                           People(), lambda: None), [])

    def test_a_chat_shows_its_last_messages_and_nothing_without_the_capture(self):
        for n in (1, 10, 60):
            s = tc.Store()
            for i in range(n):
                s.add(tc.CHAT, tc._at(i), tc.ME if i % 2 else tc.JO, f"earlier {i}")
            got = at.earlier_of(None, s.state, tc.EVENT, tc.ME, ["vince"], {}, People(),
                                lambda: None)
            want = list(range(n))[-at.EARLIER:]
            notes = [x for x in got if x.startswith("[") and x.endswith("not shown]")]
            self.assertEqual(notes, [f"[{n - at.EARLIER} earlier messages not shown]"] if n > at.EARLIER else [])
            got = [x for x in got if x not in notes]

            self.assertEqual([int(x.rsplit(" ", 1)[1]) for x in got], want, n)
        self.assertEqual(at.earlier_of(None, None, tc.EVENT, tc.ME, [], {}, People(),
                                       lambda: None), [])


class Habit(unittest.TestCase):
    def test_the_readers_habit_with_this_sender_is_said_only_when_known(self):
        from operating import chat_ask_model as cam
        for seen, want in (([17, 18], {}), ([18, 17], {"answers_sender": "17 of 18"}),
                           ([4, 4], {}), ([5, 0], {"answers_sender": "0 of 5"}), (None, {}),
                           (["5", "4"], {}), ([True, 1], {}), ([5], {})):
            with self.subTest(seen=seen):
                got = cam.answers_of({"sender_history": seen} if seen is not None else {})
                self.assertEqual(got, {} if seen == [17, 18] else want)
        line = cam.prompt_of([{"text": "lol nice", "answers_sender": "17 of 18"}])
        self.assertIn('"answers_sender": "17 of 18"', line)
        self.assertNotIn('"answers_sender"', cam.prompt_of([{"text": "lol nice"}]).split(
            "Answer with only")[1])


class KeepsUp(unittest.TestCase):
    def test_only_a_one_to_one_chat_with_a_reader_who_answers_nearly_all(self):
        for event, want in (({"channel": "teams", "sender_history": [10, 9]}, True),
                            ({"channel": "teams", "sender_history": [10, 8]}, False),
                            ({"channel": "teams", "sender_history": [9, 9]}, False),
                            ({"channel": "teams", "group": True, "sender_history": [20, 20]},
                             False),
                            ({"channel": "mail", "sender_history": [20, 20]}, False),
                            ({"channel": "teams", "sender_history": [10, 11]}, False),
                            ({"channel": "teams", "sender_history": ["10", "10"]}, False),
                            ({"channel": "teams"}, False)):
            with self.subTest(event=event):
                self.assertEqual(at.keeps_up(event), want)
        # such a message is never sent to the model: the rules' reading stands
        ev = {"ask_check": "model", "cleared": True, "to_me": True, "channel": "teams",
              "sender_history": [12, 12], "mailbox": "m", "message_id": "x",
              "event_type": "message.received"}
        self.assertIsNone(at._ask_wanted(None, ev, [], now=None, live=False, addresses_of={}))


class Shown(trp.WaitingOnYou):
    def test_cardinality_the_model_reads_it_and_the_event_never_keeps_it(self):
        """Each mail read once with its earlier part; the queue's event bodies never hold
        it (it is read for the triage only). N=1/10/60."""
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.correspondent()
                for i in range(n):
                    self.thread(i, ask=self.FYI, earlier=False)
                self.store()
                model = _Stub({})
                with mock.patch.object(at, "earlier_of",
                                       lambda *a, **k: ["the reader (their own): did they ship?"]):
                    self.mail_run(model)
                sent = [m for b in model.sent for m in b]
                self.assertEqual(len(sent), n)
                self.assertTrue(all(m["earlier"] == ["the reader (their own): did they ship?"]
                                    for m in sent))
                q = self.queue_file()
                self.assertFalse(any("did they ship" in b for (b,) in
                                     q.conn.execute("SELECT body FROM events")))


for _name in dir(trp.WaitingOnYou):
    if _name.startswith("test") and _name not in Shown.__dict__:
        setattr(Shown, _name, None)
del _name


if __name__ == "__main__":
    unittest.main()


class _Ctx:
    def __init__(self, atts):
        self.atts, self.objects, self.handles, self.capture = atts, set(), [], None

    def linked(self, oid, links):
        return list(self.atts)


class Content(unittest.TestCase):
    """Beyond the 255-character preview (content audit 11-22): the mail's own words in full,
    its attachments by name and text start, the states of the orders it names; each piece
    screened, unreadable files counted, nothing when it cannot be read."""

    def files(self, texts):
        from operating import autopilot_work as aw
        names = {f"Attachment:{i}": (f"file{i}.pdf", t) for i, t in enumerate(texts)}
        ctx = _Ctx(names)
        with mock.patch.object(aw, "_handle", lambda c, o: o), \
                mock.patch.object(aw, "_store_text", lambda c, h, p:
                                  names[h][0] if p == "row.name" else names[h][1]):
            unread = []
            got = aw.attachments(ctx, {"object_id": "Email:x"}, unread=unread)
        return got, unread

    def test_cardinality_attachments_never_skipped_silently(self):
        from operating import autopilot_work as aw
        for n in (1, 10, 60):
            texts = [("PO 4410 lines" if i % 2 == 0 else None) for i in range(n)]
            got, unread = self.files(texts)
            with_text = sum(1 for t in texts if t)
            self.assertEqual(len(got), min(with_text, aw.MAX_ATTACHMENTS))
            # every file is read or said: none left out unsaid
            self.assertEqual(len(got) + len(unread), n, n)
            self.assertTrue({u["why"] for u in unread} <= {"no_text", "over_limit"})

    def content(self, body, files=(), lines=(), people=None):
        from operating import autopilot_work as aw, autopilot_handlers as ah

        def atts(ctx, event, unread=None):
            if unread is not None:
                unread.append({"object_id": "Attachment:scan", "why": "no_text"})
            return [{"name": n, "text": t, "form": "pdf", "handle": n, "object_id": n}
                    for n, t in files]
        with mock.patch.object(aw, "body_text", lambda c, e: body), \
                mock.patch.object(aw, "attachments", atts), \
                mock.patch.object(ah, "_records_named", lambda c, p: ["SalesOrder:1"]), \
                mock.patch.object(aw, "_current", lambda c, o: o), \
                mock.patch.object(ah, "senders_own", lambda c, e, o: o), \
                mock.patch.object(aw, "_status_lines", lambda c, o: (list(lines), {})):
            return at.content_of({"channel": "mail", "text": body[:20]},
                                 people or People(), lambda: object())

    def test_a_marked_order_state_cell_never_reaches_the_model(self):
        """The order state the triage reads goes into the prompt before any reader check:
        only unmarked cells (finance or pii marked status/carrier cells exist on the live
        store, 11-22), an unreadable marking counts as marked."""
        from operating import autopilot_work as aw, autopilot_handlers as ah

        def cell(value, markings="[]"):
            return {"value": value, "markings": markings, "l1_handle": None}

        class Ctx:
            as_of = __import__("datetime").date(2026, 11, 21)

            def __init__(self, status_marks, carrier_marks, ship):
                self.ship = ship
                self.rows = {
                    "SalesOrder:1": {"order_number": cell("SO-1"),
                                     "status": cell("Released", status_marks),
                                     "required_date": cell("2026-11-24")},
                    "Shipment:1": {"ship_date": cell("2026-11-20"),
                                   "carrier": cell("Day Freight", carrier_marks),
                                   "tracking_number": cell("TRK9")}}

            def read(self, ids, props):
                return {i: {p: c for p, c in self.rows[i].items() if p in props}
                        for i in ids if i in self.rows}

            def linked(self, oid, types=None):
                return ["Shipment:1"] if self.ship and types == ("Shipment.ships",) else []

            def cite(self, row, *props):
                pass

        def records(status_marks="[]", carrier_marks="[]", ship=True):
            with mock.patch.object(aw, "body_text", lambda c, e: "Where is SO-1? " * 30), \
                    mock.patch.object(aw, "attachments", lambda c, e, unread=None: []), \
                    mock.patch.object(ah, "_records_named", lambda c, p: ["SalesOrder:1"]), \
                    mock.patch.object(aw, "_current", lambda c, o: o), \
                    mock.patch.object(ah, "senders_own", lambda c, e, o: o):
                return at.content_of({"channel": "mail", "text": "Where"}, People(),
                                     lambda: Ctx(status_marks, carrier_marks, ship)).get("records")
        self.assertIn("Day Freight", " ".join(records()))
        self.assertIn("Released", " ".join(records(ship=False)))
        for marks in ('["finance"]', '["finance","pii"]', ["finance"], "not json"):
            got = " ".join(records(carrier_marks=marks) or [])
            self.assertNotIn("Day Freight", got, marks)
            self.assertIn("TRK9", got)          # the unmarked cells still read
            got = " ".join(records(status_marks=marks, ship=False) or [])
            self.assertNotIn("Released", got, marks)
            self.assertIn("SO-1", got)

    def test_the_model_reads_the_whole_mail_its_files_and_orders_screened(self):
        from operating import chat_ask_model as cam
        from operating.tests.test_payroll_marking import PAYSTUB
        body = "Please see the attached PO. " * 40 + "\nFrom: Old <o@x.example>\nquoted"
        got = self.content(body, files=[("po.pdf", "PO 4410: 200 units"),
                                         ("stub.pdf", PAYSTUB)],
                           lines=["Order 1 is open, due Nov 24."])
        self.assertTrue(got["full_text"].startswith("Please see the attached PO."))
        self.assertNotIn("quoted", got["full_text"])
        self.assertLessEqual(len(got["full_text"]), at.BODY_CHARS)
        self.assertEqual(got["attachments"], ["po.pdf: PO 4410: 200 units"])   # stub screened
        self.assertEqual((got["files_unread"], got["records"]),
                         (1, ["Order 1 is open, due Nov 24."]))
        line = cam.prompt_of([{"text": "short", **got}])
        for want in ('"attachments"', '"files_unread": 1', '"records"', "Please see the attached"):
            self.assertIn(want, line)
        # a mail no longer than its preview, Teams, or a read that fails: nothing added
        self.assertNotIn("full_text", self.content("short"))
        self.assertEqual(at.content_of({"channel": "teams"}, People(), lambda: None), {})
        with mock.patch("operating.autopilot_work.body_text", side_effect=RuntimeError):
            self.assertEqual(at.content_of({"channel": "mail"}, People(), lambda: object()), {})
