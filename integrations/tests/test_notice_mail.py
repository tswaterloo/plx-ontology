"""Notification mail as notice records: each declared family at N=1/10/60, the reader
markings (finance; finance and pii), the people exclusion failing closed, the derived
delivery-late alert and payment-received fact. Evidence label: isolated-fixture (made-up
messages; no real sender, customer or number)."""

from __future__ import annotations

from datetime import date
import unittest

from integrations import notice_mail as nm

FAMILIES, FORMATS = nm.load_families()


class Nobody:
    def excluded(self, identities):
        return False


class Excludes:
    def __init__(self, address):
        self.address = address

    def excluded(self, identities):
        return self.address in identities


def ups_number(n: int) -> str:
    """A made-up 1Z number with a valid check digit."""
    body = f"999AA1{n:09d}"
    total = sum(((int(c) if c.isdigit() else (ord(c) - 63) % 10) * (2 if i % 2 else 1))
                for i, c in enumerate(body))
    return f"1Z{body}{(10 - total % 10) % 10}"


def message(sender, subject, i=0, to=("buyer@example-plx.com",)):
    return {"from": sender, "to": list(to), "cc": [], "subject": subject,
            "received_at": f"2030-03-{1 + i % 27:02d}T10:00:00Z",
            "citation": f"mail://fixture/{sender.split('@')[1]}/{i}",
            "source_acl": {"identities": list(to)}}


CASES = {
    "carrier_ups": lambda i: (message("pkginfo@ups.com", "UPS Update: Package Scheduled for "
                                      "Delivery", i),
                              f"<p>Tracking Number: {ups_number(i)}</p><p>Scheduled Delivery:"
                              f" 03/20/2030</p><p>Reference Number 1: PO-{4000 + i}</p>"),
    "carrier_fedex": lambda i: (message("trackingupdates@fedex.com", "Your package has been "
                                        "delivered", i),
                                f"Tracking number: {781234560000 + i}\nDelivered on: "
                                f"03/21/2030\nPurchase order: SO{9000 + i}"),
    "carrier_dhl": lambda i: (message("noreply@dhl.com", "Shipment in transit", i),
                              f"Waybill: {1200000000 + i}\nEstimated delivery: March 22, 2030"),
    "payments": lambda i: (message("notify@apspayments.com", "Payment Received", i),
                           f"Invoice #: INV{5000 + i}\nAmount Paid: $1,2{i % 10}4.50 CAD\n"
                           f"Customer: Example Retail Ltd\n"),
    "zendesk": lambda i: (message("support@example.zendesk.com", f"[Example] Re: Order "
                                  f"question #{300 + i}", i),
                          "Status: pending\nOrder #: 77" + str(i).zfill(3)),
    "netstock": lambda i: (message("reports@netstock.co", "Replenishment proposal ready", i),
                           f"Item code: FG-{100 + i}\nItem code: RM-{200 + i}"),
    "edi": lambda i: (message("alerts@edisoft.com", "New 850 Purchase Order received", i),
                      f"PO Number: 45{i:05d}\nTrading Partner: Example Mart\n"),
    "concur": lambda i: (message("autonotification@concursolutions.com", "Expense report "
                                 "approved", i),
                         f"Report Name: Trip {i}\nAmount: $8{i % 10}.10 USD"),
}


class NoticeMailTest(unittest.TestCase):
    def parse(self, msg, body, people=None):
        return nm.parse(FAMILIES, FORMATS, msg, body, people=people or Nobody())

    def test_each_family_at_cardinality(self):
        expected_kind = {"carrier_ups": "scheduled", "carrier_fedex": "delivered",
                         "carrier_dhl": "scheduled", "payments": "payment_received",
                         "zendesk": "updated", "netstock": "replenishment", "edi": "order",
                         "concur": "approved"}
        for family, make in CASES.items():
            for n in (1, 10, 60):
                notices = [self.parse(*make(i)) for i in range(n)]
                self.assertTrue(all(notices), (family, n))
                self.assertEqual({x.family for x in notices}, {family})
                self.assertEqual({x.kind for x in notices}, {expected_kind[family]}, family)
                self.assertEqual(len({x.citation for x in notices}), n)   # none lost or merged
                for i, x in enumerate(notices):
                    msg, body = make(i)
                    text = msg["subject"] + "\n" + nm.plain_text(body)
                    self.assertTrue(nm.validate(x, text), (family, i, x.fields))
        ups = self.parse(*CASES["carrier_ups"](3))
        self.assertEqual(ups.fields["tracking_numbers"], [ups_number(3)])
        self.assertEqual(ups.fields["scheduled_delivery"], "2030-03-20")
        self.assertIn({"field": "references", "identifier_kind": "reference",
                       "value": "PO-4003"}, ups.links)
        pay = self.parse(*CASES["payments"](2))
        self.assertEqual(pay.fields["amount"], {"amount": "1224.50", "currency": "CAD"})
        self.assertEqual(pay.fields["invoice_numbers"], ["INV5002"])
        self.assertEqual(self.parse(*CASES["edi"](4)).fields["po_numbers"], ["4500004"])

    def test_unknown_input_yields_nothing_and_bad_declarations_refuse(self):
        self.assertIsNone(self.parse(message("someone@example.com", "Delivered"), "1Z"))
        # a carrier notice whose number fails its check digit is no record
        bad = ups_number(5)[:-1] + str((int(ups_number(5)[-1]) + 1) % 10)
        self.assertIsNone(self.parse(message("pkginfo@ups.com", "Delivered"),
                                     f"Tracking Number: {bad}"))
        for doc in ({"schema": "x", "families": []},
                    {"schema": nm.SCHEMA, "families": [{"id": "a", "senders": ["x"],
                                                        "markings": ["secret"],
                                                        "kinds": [{"kind": "k", "any": ["."]}]}]},
                    {"schema": nm.SCHEMA, "families": [{"id": "a", "senders": ["x"],
                                                        "kinds": "@nosuch"}]},
                    {"schema": nm.SCHEMA, "families": [{
                        "id": "a", "senders": ["x"], "kinds": [{"kind": "k", "any": ["."]}],
                        "fields": {"f": {"type": "code", "patterns": ["no group"]}}}]}):
            with self.assertRaises(nm.NoticeError):
                nm.load_families(doc)

    def test_people_exclusion_fails_closed(self):
        msg, body = CASES["payments"](1)
        with self.assertRaises(nm.NoticeError):
            nm.parse(FAMILIES, FORMATS, msg, body, people=None)
        self.assertIsNone(self.parse(msg, body, people=Excludes("buyer@example-plx.com")))
        self.assertIsNotNone(self.parse(msg, body, people=Excludes("other@example-plx.com")))

    def test_restricted_readers(self):
        payment = self.parse(*CASES["payments"](1))
        expense = self.parse(*CASES["concur"](1))
        carrier = self.parse(*CASES["carrier_ups"](1))
        in_acl = lambda acl: "buyer@example-plx.com" in (acl or {}).get("identities", [])
        nobody = lambda acl: False
        notices = [payment, expense, carrier]
        self.assertEqual(nm.for_reader(notices, set(), in_acl), [carrier])
        self.assertEqual(nm.for_reader(notices, {"finance"}, in_acl), [payment, carrier])
        self.assertEqual(nm.for_reader(notices, {"pii"}, in_acl), [carrier])
        self.assertEqual(nm.for_reader(notices, {"finance", "pii"}, in_acl), notices)
        # a reader the source message is not visible to sees none, markings or not
        self.assertEqual(nm.for_reader(notices, {"finance", "pii"}, nobody), [])
        # derived facts keep the finance marking
        self.assertTrue(all("finance" in f["markings"] for f in nm.payment_received([payment])))

    def test_delivery_late_and_payment_received(self):
        number = ups_number(42)
        sched = self.parse(message("pkginfo@ups.com", "Scheduled for delivery", 1),
                           f"Tracking Number: {number}\nScheduled Delivery: 03/05/2030\n"
                           "Reference: PO-77")
        late = self.parse(message("pkginfo@ups.com", "Your package was delivered", 9),
                          f"Tracking Number: {number}\nDelivered On: 03/09/2030")
        alerts = nm.delivery_late([sched, late], date(2030, 3, 20))
        self.assertEqual([(a["state"], a["days_late"], a["references"]) for a in alerts],
                         [("delivered_late", 4, ["PO-77"])])
        self.assertEqual(len(alerts[0]["citations"]), 2)
        # not delivered: late only a day past the promise
        self.assertEqual(nm.delivery_late([sched], date(2030, 3, 6)), [])
        self.assertEqual(nm.delivery_late([sched], date(2030, 3, 7))[0]["state"],
                         "not_delivered")
        on_time = self.parse(message("pkginfo@ups.com", "Delivered", 4),
                             f"Tracking Number: {number}\nDelivered On: 03/05/2030")
        self.assertEqual(nm.delivery_late([sched, on_time], date(2030, 3, 20)), [])
        two = self.parse(message("notify@apspayments.com", "Payment received", 3),
                         "Invoice #: INV-1\nInvoice #: INV-2\nTotal: $100.00 USD")
        facts = nm.payment_received([two, self.parse(*CASES["payments"](4))])
        self.assertEqual([(f["invoice_number"], f["amount"]["amount"], f["shared_total"])
                          for f in facts],
                         [("INV-1", "100.00", True), ("INV-2", "100.00", True),
                          ("INV5004", "1244.50", False)])


class NoticeProjectionTest(unittest.TestCase):
    """The mail projector's ``notices`` and ``noticeReferences`` relations (the schema's
    graph_mail.notices / graph_mail.notice_references bindings) over a scratch capture."""

    def captures(self, messages, exclusion):
        import json
        import sqlite3
        from ontology.project import MailCaptures
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE messages (version_id INTEGER PRIMARY KEY, l1_handle TEXT, "
                     "mailbox_id TEXT, message_id TEXT, record TEXT, digest TEXT, "
                     "body_sha256 TEXT, recorded_at TEXT, superseded_at TEXT, "
                     "removed_reason TEXT, last_seen_at TEXT)")
        bodies = {}
        for i, (box, (msg, body)) in enumerate(messages):
            bodies[f"b{i}"] = body
            conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                i + 1, f"graph-mail://{box}/m{i}@ck", box, f"m{i}",
                json.dumps({k: v for k, v in msg.items() if k != "citation"}), f"d{i}",
                f"b{i}", "2030-03-01T00:00:00Z", None, None, "2030-03-02T00:00:00Z"))
        mc = MailCaptures.__new__(MailCaptures)
        mc._conn, mc.exclusion = conn, exclusion
        mc._blob_text = bodies.get
        return mc

    class Usable:
        unavailable = None

        def __init__(self, excluded=(), undecided=()):
            self.out, self.undecided = set(excluded), set(undecided)

        def known_domains(self):
            return frozenset({"example-plx.com"})

        def excluded(self, identities):
            if self.undecided & set(identities):
                raise ValueError("unknown_identity")
            return bool(self.out & set(identities))

    def test_notices_take_their_messages_audience_markings_and_references(self):
        from ontology.project import NOTICE_REFERENCES, NOTICE_RELATION
        pay = CASES["payments"](1)
        concur = CASES["concur"](2)
        ups = CASES["carrier_ups"](3)
        msgs = [("box-a", pay), ("box-b", concur), ("box-a", ups),
                ("box-c", (message("someone@example.org", "Lunch?"), "hi"))]
        mc = self.captures(msgs, self.Usable())
        notices = list(mc._notice_records(NOTICE_RELATION))
        self.assertEqual([(n.audience_key, n.l1_handle, n.row["family"], n.row["markings"])
                          for n in notices],
                         [("mail:box-a", "graph-mail://box-a/m0@ck", "payments", ["finance"]),
                          ("mail:box-a", "graph-mail://box-a/m2@ck", "carrier_ups", []),
                          ("mail:box-b", "graph-mail://box-b/m1@ck", "concur",
                           ["finance", "pii"])])
        self.assertEqual(notices[0].row["amount"]["currency"], "CAD")
        refs = list(mc._notice_records(NOTICE_REFERENCES))
        self.assertTrue(refs and all(r.audience_key == f"mail:{r.row['mailbox']}" for r in refs))
        kinds = {(r.row["family"], r.row["identifier_kind"]) for r in refs}
        self.assertIn(("payments", "invoice_number"), kinds)
        self.assertIn(("carrier_ups", "tracking_number"), kinds)
        ups_refs = [r for r in refs if r.row["identifier_kind"] == "tracking_number"]
        self.assertEqual(len(ups_refs), 1)
        self.assertEqual(ups_refs[0].row["tracking_number"], ups_number(3))
        self.assertEqual(len({r.record_key for r in refs}), len(refs))   # one per value
        self.assertTrue(all(r.row["markings"] == ["finance", "pii"]
                            for r in refs if r.row["family"] == "concur"))

    def test_real_exclusion_decides_tenant_addresses_from_the_captured_mailboxes(self):
        """Notices are read with the real exclusion before any people read: the build loads
        the staff directory from the capture's mailboxes, so an outside carrier's notice to
        staff is read and one to an excluded person's mailbox address is withheld."""
        from integrations.people_exclusion import PeopleExclusion
        from ontology.project import NOTICE_RELATION
        excluded = "11111111-1111-4111-8111-111111111111"
        staff = "22222222-2222-4222-8222-222222222222"
        ups = CASES["carrier_ups"](3)
        away = (message("pkginfo@ups.com", ups[0]["subject"], 4,
                        to=("away@example-plx.com",)), ups[1])
        mc = self.captures([("box-a", ups), ("box-x", away)], PeopleExclusion([excluded]))
        mc._conn.execute("CREATE TABLE mailboxes (mailbox_id TEXT, upn TEXT, identities TEXT, "
                         "first_captured_at TEXT, identity_checked_at TEXT)")
        mc._conn.executemany("INSERT INTO mailboxes VALUES (?,?,?,?,?)", [
            (staff, "buyer@example-plx.com", "[]", None, None),
            (excluded, "away@example-plx.com", "[]", None, None)])
        notices = list(mc._notice_records(NOTICE_RELATION))
        self.assertEqual([n.l1_handle for n in notices], ["graph-mail://box-a/m0@ck"])

    def test_excluded_undecided_and_unavailable_exclusions_withhold(self):
        from ontology.project import NOTICE_REFERENCES, NOTICE_RELATION
        pay = CASES["payments"](1)
        msgs = [("box-a", pay)]
        for exclusion in (self.Usable(excluded={"buyer@example-plx.com"}),
                          self.Usable(undecided={"buyer@example-plx.com"})):
            mc = self.captures(msgs, exclusion)
            self.assertEqual(list(mc._notice_records(NOTICE_RELATION)), [])
            self.assertEqual(list(mc._notice_records(NOTICE_REFERENCES)), [])
        gone = self.Usable()
        gone.unavailable = "people_exclusion_unavailable"
        self.assertEqual(list(self.captures(msgs, gone)._notice_records(NOTICE_RELATION)), [])


class NoticeBuildExclusionTests(unittest.TestCase):
    """The build's notice exclusion with the real directory-backed exclusion: an outside
    notice sender is no tenant person's, a tenant address is decided, an undecidable one
    (or no known tenant domain) withholds the message."""

    EXCLUDED = "11111111-1111-4111-8111-111111111111"
    STAFF = "22222222-2222-4222-8222-222222222222"

    def people(self, directory: bool = True):
        from integrations.people_exclusion import PeopleExclusion
        people = PeopleExclusion([self.EXCLUDED])
        if directory:
            people.attach_directory([(self.EXCLUDED, "away@own.example", []),
                                     (self.STAFF, "ops@own.example", [])])
        return people

    def test_outside_sender_to_staff_is_read_and_excluded_party_is_withheld(self):
        from ontology.project import _UndecidedWithheld
        guard = _UndecidedWithheld(self.people())
        self.assertFalse(guard.excluded(["notices@carrier.example", "ops@own.example"]))
        self.assertTrue(guard.excluded(["notices@carrier.example", "away@own.example"]))
        self.assertTrue(guard.excluded(["notices@carrier.example", "nobody@own.example"]))

    def test_without_tenant_domains_every_message_is_withheld(self):
        from ontology.project import _UndecidedWithheld
        guard = _UndecidedWithheld(self.people(directory=False))
        self.assertTrue(guard.excluded(["notices@carrier.example", "ops@own.example"]))


if __name__ == "__main__":
    unittest.main()
