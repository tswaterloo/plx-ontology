"""Messages linked to the supplier or customer that sent them (``ontology.sender_parties``).

Evidence label: ``isolated-fixture`` (synthetic Portal rows and messages, the real
projector, the compiled schema and the step; no source read).

* A sender address a party stores is a ``corroborated`` link, even on a shared mail
  provider; a sender whose domain only one party holds is a ``lead``.
* A shared provider's domain, our own domain, a domain two different parties hold and a
  pair a person rejected link nothing; a domain two records of one party hold (one
  normalised name) links both, an address only the record storing it.
* The record a message names decides what the sender's address alone cannot, only where
  the sender's domain spells the named party's name: a domain no party holds is a ``lead``
  to that party, and a domain a customer and a supplier both hold reaches the holder of the
  named record's side. A broker's domain, which spells no name, links nothing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ontology import identifiers, schema
from ontology import sender_parties as sp
from ontology.project import CapturedRecord, build_store

T = "2026-10-01T00:00:00Z"
SUPPLIER, CUSTOMER = "Email.sent_by_supplier", "Email.sent_by_customer"


class Source:
    def __init__(self, source, rows):
        self.source, self.rows = source, rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [self._record(rel, row) for row in self.rows.get(rel, [])]
                for rel in relations}

    def _record(self, rel, row):
        key = (json.dumps([row["mailbox"], row["id"]]) if rel == "messages"
               else json.dumps({"id": row["id"]}))
        digest = hashlib.sha256(f"{rel}/{key}".encode()).hexdigest()
        audience = f"mail:{row['mailbox']}" if rel == "messages" else f"api:{rel}"
        scheme = "graph-mail" if rel == "messages" else "api"
        return CapturedRecord(self.source, rel, key, row,
                              f"{scheme}://{rel}/{quote(key, safe='')}@0123456789abcdef-1",
                              "row", digest, T, None, T, audience)


PORTAL = {
    "public.app_supplier": [
        {"id": "s-a", "code": "ACME01", "name": "Acme Polymers Inc.",
         "website": "https://www.acme-poly.example"},
        {"id": "s-d", "code": "DUPE01", "name": "Dupe Packaging", "website": "dupe.example"},
        {"id": "s-n", "code": "NOVA01", "name": "Novachem Ltd"},
        {"id": "s-c", "code": "CEDR01", "name": "Cedarline Packaging",
         "website": "cedarline.example"}],
    "public.app_purchase_order": [
        {"id": "po-n", "po_number": "PO#9101", "supplier_id": "s-n"},
        {"id": "po-c", "po_number": "PO#9102", "supplier_id": "s-c"}],
    "public.supplier": [{"id": 7, "name": "Birch Oils", "emailrep": "birch.rep@gmail.com"}],
    "public.customer": [
        {"id": 1, "name": "Zeta Hotels Ltd", "email": "orders@zeta.example"},
        {"id": 2, "name": "ZETA HOTELS LTD.", "email": "ap@zeta.example"},
        {"id": 3, "name": "Other Buyer", "email": "buyer@dupe.example"},
        {"id": 4, "name": "Cedarline Foods", "email": "orders@cedarline.example"}],
}


def message(i, sender):
    return {"id": f"m{i}", "mailbox": "box1", "from": sender,
            "received_at": "2026-09-30T10:00:00Z", "subject": "hello"}


MAIL = {"messages": [
    message(1, "jane@acme-poly.example"),        # domain only: lead to Acme
    message(2, "birch.rep@gmail.com"),            # stored address on gmail: corroborated
    message(3, "someone@gmail.com"),              # shared provider: nothing
    message(4, "me@petrasoap.com"),               # our own domain: nothing
    message(5, "x@dupe.example"),                 # held by two parties: nothing
    message(6, "orders@zeta.example"),            # stored address: both Zeta records
    message(7, "bob@sales.acme-poly.example"),    # rejected below: nothing
    message(8, "frank@zeta.example"),             # domain of one party's two records
    message(9, "ap@novachem.example"),            # held by none, names Novachem's PO: lead
    message(10, "desk@freightbroker.example"),    # names Novachem's PO, spells no name
    message(11, "ar@cedarline.example"),          # a customer's and a supplier's: PO side
]}


def oid(type_id, binding, key):
    return f"{type_id}:{binding}:{json.dumps(key, separators=(',', ':'))}"


def email(i):
    return f"Email:graph_mail.messages:{json.dumps({'id': f'm{i}', 'mailbox': 'box1'}, separators=(',', ':'))}"


ACME = oid("Supplier", "portal.app_supplier", {"id": "s-a"})
BIRCH = oid("Supplier", "portal.fm_supplier", {"id": 7})
NOVA = oid("Supplier", "portal.app_supplier", {"id": "s-n"})
CEDAR = oid("Supplier", "portal.app_supplier", {"id": "s-c"})
ZETA = [oid("Customer", "portal.fm_customer", {"id": i}) for i in (1, 2)]


def name_orders(builder, projector):
    """Messages 9-11 name a purchase order (as the mentions step's link would)."""
    po = {"po-n": oid("PurchaseOrder", "portal.app_purchase_order", {"id": "po-n"}),
          "po-c": oid("PurchaseOrder", "portal.app_purchase_order", {"id": "po-c"})}
    builder.add("links", [{
        "link_id": f"mention:t{i}", "link_type": "Email.mentions_purchase_order",
        "from_object": email(i), "to_object": po[key], "tier": "lead",
        "binding_kind": "mention", "link_contract": None,
        "reason": '[{"agrees":true,"feature":"mention"}]', "why_not": "[]",
        "evidence_handles": f'["mention:h{i}"]', "reviewed_by": None, "reviewed_at": None,
        "review_action_id": None, "markings": "[]", "recorded_at": T, "superseded_at": None}
        for i, key in ((9, "po-n"), (10, "po-n"), (11, "po-c"))])
    return {}


def reject(builder, projector):
    """A person's rejection of message 7 -> Acme (a ``contradicted`` link)."""
    builder.add("links", [{
        "link_id": "review:x", "link_type": SUPPLIER, "from_object": email(7),
        "to_object": ACME, "tier": "contradicted", "binding_kind": "reviewed",
        "link_contract": None, "reason": '[{"agrees":false,"feature":"review"}]',
        "why_not": "[]", "evidence_handles": '["review:a1"]', "reviewed_by": "tester", "reviewed_at": T,
        "review_action_id": "a1", "markings": "[]", "recorded_at": T,
        "superseded_at": None}])
    return {}


class SenderParties(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        cls.report = build_store(
            path, schema.load_schema(),
            {"portal_api": Source("portal_api", PORTAL), "graph_mail": Source("graph_mail", MAIL)},
            normalizer=identifiers.normalize,
            clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
            post_project={"mentions": name_orders, "reviews": reject,
                          "sender_parties": sp.sender_parties_step})
        cls.conn = sqlite3.connect(str(path))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def links(self):
        return {(f, t, lt, tier) for f, t, lt, tier in self.conn.execute(
            "SELECT from_object, to_object, link_type, tier FROM links WHERE link_id LIKE "
            "'sendparty:%' AND superseded_at IS NULL")}

    def test_each_message_is_linked_to_the_one_party_that_sent_it(self):
        self.assertEqual(self.links(), {
            (email(1), ACME, SUPPLIER, "lead"),
            (email(2), BIRCH, SUPPLIER, "corroborated"),
            (email(6), ZETA[0], CUSTOMER, "corroborated"),
            (email(8), ZETA[0], CUSTOMER, "lead"),
            (email(8), ZETA[1], CUSTOMER, "lead"),
            (email(9), NOVA, SUPPLIER, "lead"),
            (email(11), CEDAR, SUPPLIER, "lead")})
        counts = self.report["post_project"]["sender_parties"]["counts"]
        self.assertEqual((counts["shared_mail_provider"], counts["own_sender"],
                          counts["shared_by_parties"], counts["rejected"], counts["no_party"],
                          counts["named_record"], counts["named_side_domain"]),
                         (1, 1, 1, 1, 1, 1, 1))

    def test_a_link_by_the_named_record_cites_that_record(self):
        (reason, handles), = self.conn.execute(
            "SELECT reason, evidence_handles FROM links WHERE link_id LIKE 'sendparty:%' "
            "AND from_object = ?", (email(9),))
        (po,), = self.conn.execute(
            "SELECT l1_handle FROM object_versions WHERE object_id = ?",
            (oid("PurchaseOrder", "portal.app_purchase_order", {"id": "po-n"}),))
        self.assertEqual([f["feature"] for f in json.loads(reason)],
                         ["sender_domain", "named_record_party"])
        self.assertIn(po, json.loads(handles))

    def test_a_link_cites_what_it_read_and_never_copies_a_value(self):
        (reason, handles, marks), = self.conn.execute(
            "SELECT reason, evidence_handles, markings FROM links WHERE link_id LIKE "
            "'sendparty:%' AND from_object = ?", (email(2),))
        self.assertNotIn("birch.rep", reason)
        self.assertEqual([f["feature"] for f in json.loads(reason)],
                         ["sender_address", "party_address"])
        (birch,), = self.conn.execute("SELECT l1_handle FROM object_versions WHERE "
                                      "object_id = ?", (BIRCH,))
        self.assertEqual(json.loads(handles)[1:], [birch])
        self.assertTrue(json.loads(handles)[0].startswith("graph-mail://"))
        self.assertIn("pii", json.loads(marks))   # an FM rep address is pii

    def test_a_rebuild_replaces_the_rows_it_wrote(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE link_rows (link_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO link_rows VALUES (?)",
                         [("sendparty:old",), ("sendpartx",), ("docfirm:y",)])

        class Builder:
            connection = conn

        class Projector:
            links = {}
        sp.sender_parties_step(Builder(), Projector())
        self.assertEqual(sorted(r[0] for r in conn.execute("SELECT link_id FROM link_rows")),
                         ["docfirm:y", "sendpartx"])


class Judge(unittest.TestCase):
    def holding(self, party, domain, address=None, prop="website"):
        return sp.Holding(party, party.split(":", 1)[0], prop, domain, address, "h",
                          {}, "[]", T)

    def test_a_subdomain_sender_reaches_the_registered_domain_and_no_other_party(self):
        parties = sp.Parties([self.holding("Supplier:a", "acme.example"),
                              self.holding("Customer:b", "beta.example")],
                             {}, {"Supplier:a": "acme", "Customer:b": "beta"}, {})
        outcome, tier, held = parties.judge("x@mail.acme.example", ["petrasoap.com"])
        self.assertEqual((outcome, tier, [h.party for h in held]),
                         ("domain", "lead", ["Supplier:a"]))
        self.assertEqual(parties.judge("x@notacme.example", [])[0], "no_party")

    def test_identity_twins_are_one_party_and_different_names_two(self):
        holdings = [self.holding("Customer:a", "cedar.example"),
                    self.holding("Supplier:b", "cedar.example")]
        names = {"Customer:a": "cedar one", "Supplier:b": "maple two"}
        apart = sp.Parties(holdings, {}, names, {})
        self.assertEqual(apart.judge("x@cedar.example", [])[0], "shared_by_parties")
        joined = sp.Parties(holdings, {"Customer:a": "r", "Supplier:b": "r"}, names, {})
        self.assertEqual(joined.judge("x@cedar.example", [])[:2], ("domain", "lead"))

    def test_a_domain_the_party_only_uses_is_no_lead_but_its_stored_address_is_firm(self):
        # a customer location holding a large organisation's address (a government's)
        parties = sp.Parties([self.holding("Customer:a", "province.example",
                                           "buyer@province.example", "bc_email")],
                             {}, {"Customer:a": "lakeside correctional centre"}, {})
        self.assertEqual(parties.judge("someone@province.example", [])[0],
                         "domain_name_disagrees")
        self.assertEqual(parties.judge("buyer@province.example", [])[:2],
                         ("address", "corroborated"))
        self.assertTrue(sp.name_fits_domain("northwind freight", "northwindco.example"))

    def test_records_of_two_parties_name_no_sender(self):
        named = [self.holding(p, "", prop=sp.NAMED) for p in ("Supplier:a", "Supplier:b")]
        parties = sp.Parties([], {}, {"Supplier:a": "orbit", "Supplier:b": "orbit labs"}, {})
        self.assertEqual(parties.by_named("x@orbit.example", "no_party", [], named),
                         ("no_party", None, []))
        self.assertEqual(parties.by_named("x@orbit.example", "no_party", [], named[:1])[:2],
                         (sp.NAMED, "lead"))
        # a shared provider's domain spells no party, whatever the name
        self.assertEqual(parties.by_named("orbit@gmail.com", "no_party", [], named[:1])[0],
                         "no_party")
        self.assertFalse(sp.name_fits_domain("general", "ge.example"))


if __name__ == "__main__":
    unittest.main()


class MeetingAttendees(unittest.TestCase):
    """Made-up calendar events: each organizer or attendee address is judged as a sender;
    a stored address wins over a domain lead of the same party, and our own people and
    unknown addresses link nothing (``ontology.sender_parties.meeting_rows``)."""

    def test_an_event_links_each_party_its_people_name_once(self):
        from collections import Counter
        from unittest import mock
        from ontology import sender_parties as sp

        cust = "Customer:portal.app_customer:{\"id\":\"c-zeta\"}"

        def row(oid, value):
            return {"object_id": oid, "l1_handle": "h:" + oid, "field_path": "row.attendees",
                    "digest": "d", "lineage": "meetings", "vmarks": "[]", "kmarks": "[]",
                    "recorded_at": "2026-10-01T00:00:00Z", "value": value}

        event = "CalendarEvent:meetings.calendar_events:{\"id\":\"e1\"}"
        values = [(row(event, '{"address":"ann@zeta-made-up.example"}'), "organizer"),
                  (row(event, '[{"address":"bo@zeta-made-up.example"},'
                              '{"address":"me@petra.example"},{"address":"x@nowhere.example"}]'),
                   "attendees")]

        class Parties:
            twins: dict = {}

            def judge(self, address, own):
                if address.endswith("@petra.example"):
                    return "own_sender", None, []
                if address == "ann@zeta-made-up.example":
                    return "address", "corroborated", [holding]
                if address.endswith("@zeta-made-up.example"):
                    return "domain", "lead", [holding]
                return "no_party", None, []

        holding = sp.Holding(cust, "Customer", "email", "zeta-made-up.example",
                             "ann@zeta-made-up.example", "h:c", {"l1_handle": "h:c"}, "[]",
                             "2026-10-01T00:00:00Z")
        links = {"CalendarEvent.with_customer": {"bindings": [{"kind": "mention"}]}}
        versions = {cust: {"l1_handle": "h:c", "markings": "[]",
                           "recorded_at": "2026-10-01T00:00:00Z"}}
        counts = Counter()
        with mock.patch.object(sp, "_values", lambda conn, props, prefixes: iter(values)), \
                mock.patch.object(sp, "_versions", lambda conn, oids: versions):
            rows = sp.meeting_rows(None, Parties(), [], links, counts)
        self.assertEqual([(r["from_object"], r["link_type"], r["to_object"], r["tier"])
                          for r in rows],
                         [(event, "CalendarEvent.with_customer", cust, "corroborated")])
        self.assertEqual((counts["meeting_own_sender"], counts["meeting_no_party"]), (1, 1))
