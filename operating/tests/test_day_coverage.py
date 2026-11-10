"""Day coverage: every system's attributed activity, conservative identity, the exclusion,
and the covered share (``operating.day_coverage``). Synthetic records only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import tempfile
from pathlib import Path
import unittest

from operating import autopilot as ap
from operating import day_coverage as dc
from operating.tests.test_autopilot_replay import World
from operating.tests.test_proactive_engine import EXCLUDED_OID, OURS_OID
from operating.tests.test_proactive_threads import APPROVER_OID, people

SINCE, UNTIL = "2026-10-20T07:00:00Z", "2026-10-23T07:00:00Z"


class Activity(World):
    def sage(self, otype: str, who: str, day: str = "20261021", **more: str) -> str:
        date_prop = dc.SAGE_ENTRIES[otype][1]
        return self.w.obj(otype, {date_prop: day, "enteredby": f"{who:<8}", **more},
                          binding=f"gen.sage.{otype.lower()}")

    def build_world(self) -> None:
        w = self.w
        w.obj("PlannerTask", {"created_at": "2026-10-21T14:00:00Z", "created_by": OURS_OID,
                              "completed_at": "2026-10-22T15:00:00Z",
                              "completed_by": APPROVER_OID})
        w.obj("PlannerTask", {"created_at": "2026-10-21T14:00:00Z",
                              "created_by": EXCLUDED_OID})
        w.obj("PlannerTask", {"created_at": "2026-09-01T14:00:00Z", "created_by": OURS_OID})
        self.sage("RelSageArTcr", "LEADQ")          # first name + last initial: one person
        self.sage("RelSageArTcr", "ADMIN")          # no person: counted, never given
        self.sage("RelSageArTcr", "BOSSZ")          # the excluded person: dropped whole
        self.sage("RelSageArTcr", "OPS", day="20261001")       # outside the window
        self.sage("RelSageGlJeHeader", "OPS", srceledger="AR", srcetype="IN")   # a mirror
        self.sage("RelSageGlJeHeader", "OPS", srceledger="GL", srcetype="JE")
        w.obj("CalendarEvent", {"start": "2026-10-21T15:00:00Z", "end": "2026-10-21T16:30:00Z",
                                "is_organizer": "true", "mailbox": OURS_OID})
        self.events.append(("GeneralLedgerEntry.posted",
                            w.obj("GeneralLedgerEntry", {"amount": "1"}), "2026-10-21T10:00:00Z"))

    def test_each_system_attributes_only_one_cleared_person(self):
        self.build_world()
        st = self.store()
        who = {OURS_OID, APPROVER_OID}
        acts, lost = dc.system_actions(st, people(), who, SINCE, UNTIL)
        got = sorted((a["person"], a["kind"], a["system"]) for a in acts)
        self.assertEqual(got, sorted([
            (OURS_OID, "planner_created", "planner"),
            (APPROVER_OID, "planner_completed", "planner"),
            (APPROVER_OID, "sage_ar_receipt", "sage"),
            (OURS_OID, "sage_gl_journal", "sage"),
            (OURS_OID, "meeting_organized", "calendar"),
            (OURS_OID, "meeting_attended", "calendar")]))
        meeting = next(a for a in acts if a["kind"] == "meeting_attended")
        self.assertEqual(meeting["minutes"], 90)
        self.assertEqual(lost[("sage", "sage_ar_receipt", "who_not_one_person")], 1)
        self.assertEqual(lost[("bc", "GeneralLedgerEntry.posted", "no_who_field")], 1)
        # the excluded person is neither attributed nor counted anywhere
        self.assertNotIn(EXCLUDED_OID, json.dumps([acts, sorted(map(str, lost.items()))]))
        self.assertEqual(sum(n for (s, k, _r), n in lost.items() if s in ("planner",)), 0)

    def test_cache_gives_the_same_rows_for_a_narrower_window(self):
        self.build_world()
        st = self.store()
        who = {OURS_OID, APPROVER_OID}
        with tempfile.TemporaryDirectory() as tmp:
            dc.system_actions(st, people(), who, "2026-10-01T07:00:00Z", UNTIL,
                              cache=Path(tmp))
            cached, _lost = dc.system_actions(st, people(), who, SINCE, UNTIL,
                                              cache=Path(tmp))
            self.assertNotIn(EXCLUDED_OID, "".join(p.read_text()
                                                   for p in Path(tmp).iterdir()))
        fresh, _lost = dc.system_actions(st, people(), who, SINCE, UNTIL)
        key = lambda a: (a["person"], a["kind"], a["at"])   # noqa: E731
        self.assertEqual(sorted(map(key, cached)), sorted(map(key, fresh)))


class Codes(unittest.TestCase):
    def test_a_code_two_people_share_is_nobody(self):
        code = dc._Code({"sam@t.example": "a", "sam.k@t.example": "b",
                         "lee@t.example": "c", "lee.j@t.example": "c"}, ["t.example"])
        self.assertIsNone(code("SAM"))
        self.assertEqual(code("SAMK"), "b")
        self.assertEqual(code("LEE"), "c")
        self.assertIsNone(code("LEEJX"))
        self.assertIsNone(code(""))
        self.assertIsNone(code("ZED"))
        self.assertIsNone(dc._Code({"sam@other.example": "a"}, ["t.example"])("SAM"))


class Shares(unittest.TestCase):
    def test_meetings_are_listed_never_counted_as_work(self):
        since = datetime(2026, 10, 20, 7, tzinfo=timezone.utc)
        until = since + timedelta(days=7)
        at = ap._iso(since + timedelta(hours=3))
        acts = [{"person": "p", "at": at, "kind": "mail_sent", "workflow": "quote_pricing"},
                {"person": "p", "at": at, "kind": "mail_sent", "workflow": "quote_pricing"},
                {"person": "p", "at": at, "kind": "sage_ar_receipt",
                 "workflow": "cash_remittance"},
                {"person": "p", "at": at, "kind": "meeting_attended", "workflow": "meeting",
                 "minutes": 600},
                {"person": "p", "at": ap._iso(until), "kind": "mail_sent", "workflow": "x"}]
        got = dc.coverage(acts, {id(acts[0])}, since=since, until=until)
        self.assertEqual(got["work"]["actions"], 3)
        self.assertEqual(got["work"]["covered"], 1)
        self.assertEqual(got["work"]["count_share"], "0.333")
        # 6 + 6 + 6 minutes in one week; 6 covered
        self.assertEqual(got["work"]["hours_week"], "0.3")
        self.assertEqual(got["work"]["hours_share"], "0.333")
        self.assertFalse(got["types"]["meeting_attended:meeting"]["work"])
        self.assertEqual(got["types"]["meeting_attended:meeting"]["hours_week"], "10.0")

    def test_an_entry_keyed_in_a_system_is_never_judged_against_mail(self):
        from operating import autopilot_replay as rp
        since = datetime(2026, 10, 20, 7, tzinfo=timezone.utc)
        made = ap._iso(since + timedelta(hours=1))
        events = {"e1": {"event_type": "record.due", "object_id": "Shipment:fx:1",
                         "object_type": "Shipment"}}
        items = [{"event_id": "e1", "autopilot": "invoice_entry", "owner": "p",
                  "created_at": made, "body": {}}]
        mail = [{"person": "p", "at": ap._iso(since + timedelta(hours=2)),
                 "kind": "mail_sent", "workflow": "ar_invoice_send",
                 "records": ["Shipment:fx:1"]}]
        got = rp.score(items, events, mail, horizon=timedelta(days=2),
                       data_end=since + timedelta(days=9), since=since,
                       until=since + timedelta(days=3))["ar_invoice_entry"]
        self.assertEqual((got["prepared"], got["judged"], got["matched"], got["unjudged"]),
                         (1, 0, 0, 1))

    def test_machine_and_scheduled_sends_are_not_a_persons_work(self):
        from operating import autopilot_replay as rp
        day = datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)

        def sent(n, subject, body, *, at, sender="p@ours.example", person="p"):
            return {"person": person, "at": ap._iso(day + at(n)), "kind": "mail_sent",
                    **rp.sent_shape(sender, subject, body)}
        notice = "Today's headcount has been logged for every staff member on the line."
        template = [sent(n, f"Logged {n}", notice, at=lambda n: timedelta(days=n, minutes=n))
                    for n in range(5)]
        # the same note four times, or written by two people: a person's work
        typed = [sent(n, f"Note {n}", "Could you check the second pallet before it leaves "
                      "the dock this afternoon", at=lambda n: timedelta(days=n, minutes=n))
                 for n in range(4)]
        shared = [sent(n, "Ask", notice, person=f"q{n}", at=lambda n: timedelta(days=n))
                  for n in range(5)]
        # one subject at 14:00 on three days: scheduled; at three different minutes: not
        scheduled = [sent(n, f"Day end report {n}", f"steps run {n}",
                          at=lambda n: timedelta(days=n, seconds=n)) for n in range(3)]
        by_hand = [sent(n, f"Stock count {n}", f"counted {n}",
                        at=lambda n: timedelta(days=n, minutes=7 * n)) for n in range(1, 4)]
        auto = [sent(0, "Automatic reply: away", "I am out of the office until Monday",
                     at=lambda n: timedelta())]
        got = dc.not_human(template + typed + shared + scheduled + by_hand + auto)
        self.assertEqual({id(a): "system_generated" for a in template + auto} |
                         {id(a): "scheduled_send" for a in scheduled}, got)
        self.assertFalse(any(k in str(a) for a in template for k in ("headcount", "Logged")))
        # a daily notice inside a short window is told from the person's longer history
        self.assertEqual({id(template[4]): "system_generated"},
                         dc.not_human(template[4:], history=template))


if __name__ == "__main__":
    unittest.main()


class InvoiceSend(World):
    """An invoice nobody has mailed yet is drafted for the person who mailed this
    customer's earlier invoices; a replay never reads mail sent after its day."""

    def invoice(self, number: str, day: str) -> str:
        inv = self.w.obj("SalesInvoice", {"invoice_number": number, "invoice_date": day,
                                          "order_number": "SO-1", "total_amount": "120.50",
                                          "currency": "CAD", "customer_po": "PO-9"})
        self.w.link("SalesInvoice.billed_to", inv, self.customer)
        self.events.append(("SalesInvoice.invoiced", inv, f"{day}T00:00:00Z"))
        return inv

    def mailed(self, inv: str, day: str, n: int) -> None:
        from operating.tests.test_proactive_threads import OURS, mail
        made = mail(self.w, datetime.fromisoformat(day).date(), sender=OURS,
                    to=("ap@synth-customer.example",), body="Invoice attached",
                    subject="Invoice", mailbox=OURS_OID, conversation=f"inv-{n}", n=80_000 + n)
        self.w.link("Email.mentions_sales_invoice", made, inv)

    def test_unsent_invoice_is_drafted_for_who_sends_this_customers_invoices(self):
        from datetime import date
        from operating import autopilot_work as aw
        old = self.invoice("IN-1", "2026-10-10")
        self.mailed(old, "2026-10-10", 1)
        due = self.invoice("IN-2", "2026-10-21")
        sent = self.invoice("IN-3", "2026-10-21")
        self.mailed(sent, "2026-10-21", 2)
        later = self.invoice("IN-4", "2026-10-21")
        self.mailed(later, "2026-10-23", 3)          # sent after the day judged
        st = self.store()
        shipped = ap.load_registry()
        # off in the shipped registry (precision measured too low): nothing is raised
        directory, _a, domains = ap._people_maps(people())
        self.assertEqual(aw.invoice_due_events(st, shipped, date(2026, 10, 21), domains,
                                               directory), [])
        reg = {**shipped, "autopilots": [{**a, "enabled": True} if a["id"] == "invoice_send"
                                         else a for a in shipped["autopilots"]]}
        got = {e["object_id"]: e for e in aw.invoice_due_events(
            st, reg, date(2026, 10, 21), domains, directory)}
        self.assertEqual(set(got), {due, later})
        self.assertEqual(got[due]["owner_hint"], OURS_OID)
        self.assertEqual(got[due]["customer_to"], "ap@synth-customer.example")
        spec = next(a for a in reg["autopilots"] if a["id"] == "invoice_send")
        self.assertTrue(ap.matches(spec["when"], got[due]))
        po_spec = next(a for a in reg["autopilots"] if a["id"] == "po_followup")
        self.assertFalse(ap.matches(po_spec["when"], got[due]))
        owners, why = ap.owners_of("hinted", {**got[due], "owner_hint": None},
                                   directory=directory, addresses_of={}, roles={})
        self.assertEqual((owners, why), ([], "owner_unknown"))
        ctx = ap.Ctx(st, date(2026, 10, 21), float("inf"), domains)
        work = aw.h_invoice_send(ctx, got[due])
        self.assertIn("IN-2", work["draft"]["body"])
        self.assertIn("120.50 CAD", work["draft"]["body"])
        self.assertEqual(work["draft"]["to"], [{"address": "ap@synth-customer.example"}])
        self.assertEqual(work["object"]["object_id"], due)
        self.assertEqual(aw.h_invoice_send(ctx, {**got[due], "customer_to": None}),
                         {"skip": "customer_contact_unknown"})
