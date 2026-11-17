"""Two BC contact cards of one company in one BC company (``ontology.contact_twins``).

Evidence label: ``isolated-fixture`` (made-up BC contacts, customers and contact relations
in two BC companies through the real projector, the compiled repository schema and the
build's own step list; no source is read).

* Two Company cards with the same normalized name and the same e-mail or phone are a
  ``corroborated`` ``Contact.same_record_as`` for Customer+Customer, Vendor+Vendor,
  Customer+none and none+none, a ``lead`` for any other pair (Customer+Vendor, a Person
  card); each link cites both cards' name and the shared field.
* A name alone, cards of two BC companies, a pair a person rejected (either way round),
  own-domain and excluded people's cards are never linked; without a usable exclusion
  list nothing is.
* The customers two twin cards relate to stay two parties for the readers that join
  parties (``sender_parties``, ``source_precedence``).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ontology import contact_twins as ct
from ontology import identifiers
from ontology.project import CapturedRecord, build_store
from ontology.schema import load_schema
from tools.timing_policy import budget

T = "2026-10-01T00:00:00Z"
NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
SCHEMA = load_schema()
EXCLUSION = {"PLX_EXCLUDED_PEOPLE_OIDS": "11111111-1111-4111-8111-111111111111",
             "PLX_EXCLUSION_ENV_FILE": ""}


def card(cid, name, relation="Customer", email=None, phone=None, kind="Company", city=None):
    row = {"id": cid, "number": cid.upper(), "displayName": name, "type": kind,
           "companyName": name, "contactBusinessRelation": relation}
    if city:
        row["city"] = city
    if email:
        row["email"] = email
    if phone:
        row["phoneNumber"] = phone
    return row


def manufacturing(extra=()):
    contacts = [
        card("k1", "Acme Packaging Inc.", email="Dana@Acme.test"),
        card("k2", "ACME PACKAGING", email="dana@acme.test"),
        card("k3", "Birch Resins", "Vendor", phone="(905) 555-1234"),
        card("k4", "Birch Resins Ltd", "Vendor", phone="905.555.1234"),
        card("k5", "Cobalt Labs", "Customer", email="a@cobalt.test"),
        card("k6", "Cobalt Labs", "Vendor", email="a@cobalt.test"),
        card("k7", "Delta Fragrances", "None", email="x@delta.test"),
        card("k8", "Delta Fragrances", "None", email="y@delta.test"),
        card("k9", "Ember Polymers", email="e@ember.test"),
        card("k11", "Falcon Office", "None", email="sam@petrasoap.com"),
        card("k12", "Falcon Office", "None", email="sam@petrasoap.com"),
        card("k13", "Granite Supply", "None", email="g@granite.test"),
        card("k14", "Granite Supply", "None", email="g@granite.test"),
        card("k15", "Harbor Kim", "Customer", email="kim@harbor.test", kind="Person"),
        card("k16", "Harbor Kim", "Customer", email="kim@harbor.test", kind="Person"),
        card("k17", "Indigo Bottles", "None", email="i@indigo.test"),
        card("k18", "Indigo Bottles", "Customer", email="i@indigo.test"),
        card("k19", "Juniper Caps", "Customer", email="j@juniper.test"),
        card("k20", "Juniper Caps", "None", phone="+1 416 555 0000", email="j@juniper.test"),
        card("k21", "Kestrel Tubes", "Employee", email="k@kestrel.test"),
        card("k22", "Kestrel Tubes", "Employee", email="k@kestrel.test"),
        # a chain's central payables mailbox on cards of three names: no evidence
        card("c1", "Take Five Columbus", email="payables@chain.test", city="Columbus"),
        card("c2", "Take Five Columbus", email="payables@chain.test", city="Columbus"),
        card("c3", "Take Five Dayton", email="payables@chain.test", city="Dayton"),
        card("c4", "Take Five Akron", email="payables@chain.test", city="Akron"),
        # two sites of one name sharing a specific address: a lead at most
        card("l1", "Lumen Gyms", email="lumen@lumen.test", city="Toronto"),
        card("l2", "Lumen Gyms", email="lumen@lumen.test", city="Kitchener"),
        # a role mailbox counts only where both cards state one place
        card("o1", "Onyx Labels", "None", email="info@onyx.test", city="Laval"),
        card("o2", "Onyx Labels", "None", email="info@onyx.test"),
        card("p1", "Pioneer Inks", "Vendor", email="invoices@pioneer.test", city="Laval"),
        card("p2", "Pioneer Inks", "Vendor", email="Invoices@Pioneer.test", city="LAVAL"),
        *extra]
    return {"contacts": contacts,
            "customers": [{"id": "cu1", "number": "C1", "displayName": "Acme Packaging"},
                          {"id": "cu2", "number": "C2", "displayName": "Acme Packaging East"}],
            "contactsInformation": [
                {"relatedId": "cu1", "contactId": "k1", "contactNumber": "K1",
                 "contactName": "Acme Packaging Inc.", "contactType": "Company",
                 "relatedType": "Customer"},
                {"relatedId": "cu2", "contactId": "k2", "contactNumber": "K2",
                 "contactName": "ACME PACKAGING", "contactType": "Company",
                 "relatedType": "Customer"}]}


DISTRIBUTION = {"contacts": [card("d9", "Ember Polymers", email="e@ember.test")]}


class Source:
    def __init__(self, books):
        self.books = books

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        out = {}
        for rel in relations:
            out[rel] = []
            for book, rows in self.books.items():
                for row in rows.get(rel, []):
                    key = row.get("id") or f"{row['relatedId']}/{row['contactId']}"
                    digest = hashlib.sha256(json.dumps([book, rel, row], sort_keys=True)
                                            .encode()).hexdigest()
                    out[rel].append(CapturedRecord(
                        "bc", rel, f"{book}/{key}", row,
                        f"bc://production/{book}/{rel}/{key}@X", "row", digest, T, None, T,
                        f"bc:production:{book}:{rel}", capture_lineage=book))
        return out


def contradict(a, b):
    def step(builder, projector):
        builder.add("links", [{
            "link_id": "review:r1", "link_type": ct.LINK_TYPE, "from_object": a,
            "to_object": b, "tier": "contradicted", "binding_kind": "reviewed",
            "link_contract": None, "reason": '[{"agrees":false,"feature":"review"}]',
            "why_not": "[]", "evidence_handles": '["review:a1"]', "reviewed_by": "tester",
            "reviewed_at": T, "review_action_id": "a1", "markings": "[]", "recorded_at": T,
            "superseded_at": None}])
        return {}
    return step


def oid(cid):
    return f'Contact:bc.contacts:{{"id":"{cid}"}}'


class Guard:
    """An exclusion that decides one external address as an excluded person's."""
    def decides(self, value):
        return True

    def is_excluded(self, value):
        return value == "g@granite.test"


class ContactTwins(unittest.TestCase):
    def build(self, books=None, guard=None, env=EXCLUSION, reviews=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "store.sqlite"
        steps = {"twins_contacts": ct.contact_twins_step}
        if reviews:
            steps["reviews"] = reviews
        patches = [mock.patch.dict(os.environ, env)]
        if guard is not None:
            patches.append(mock.patch.object(ct, "_excluder", lambda *a: guard))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        report = build_store(
            path, SCHEMA, {"bc": Source(books or {"bc_manufacturing": manufacturing(),
                                                  "bc_distribution": DISTRIBUTION})},
            normalizer=identifiers.normalize, clock=lambda: NOW, post_project=steps)
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn, report

    def twins(self, conn):
        return {(r["from_object"], r["to_object"]): r for r in conn.execute(
            "SELECT * FROM links WHERE link_id >= ? AND link_id < ?",
            (ct.PREFIX, ct._end(ct.PREFIX)))}

    def test_twins_found_tiered_and_cited(self):
        conn, report = self.build(guard=Guard())
        links = self.twins(conn)
        self.assertEqual({k: r["tier"] for k, r in links.items()}, {
            (oid("k1"), oid("k2")): "corroborated",     # customer + customer, e-mail
            (oid("k3"), oid("k4")): "corroborated",     # vendor + vendor, phone
            (oid("k17"), oid("k18")): "corroborated",   # none + customer
            (oid("k19"), oid("k20")): "corroborated",   # customer + none
            (oid("k5"), oid("k6")): "lead",             # customer + vendor
            (oid("k15"), oid("k16")): "lead",           # person cards
            (oid("k21"), oid("k22")): "lead",           # employee relation
            (oid("l1"), oid("l2")): "lead",             # two places
            (oid("p1"), oid("p2")): "corroborated"})    # role mailbox, one place
        for row in links.values():
            self.assertEqual((row["link_type"], row["binding_kind"]),
                             (ct.LINK_TYPE, "mention"))
            self.assertIn("pii", json.loads(row["markings"]))
        first = links[(oid("k1"), oid("k2"))]
        reason = {f["feature"]: f for f in json.loads(first["reason"])}
        cites = {c["field_path"] for f in ("display_name", "email")
                 for c in reason[f]["citations"]}
        self.assertEqual(cites, {"row.displayName", "row.email"})
        self.assertEqual(len(reason["email"]["citations"]), 2)
        self.assertEqual(set(json.loads(first["evidence_handles"])),
                         {"bc://production/bc_manufacturing/contacts/k1@X",
                          "bc://production/bc_manufacturing/contacts/k2@X"})
        self.assertEqual(json.loads(first["why_not"]), [])
        phone = {f["feature"] for f in json.loads(links[(oid("k3"), oid("k4"))]["reason"])}
        self.assertIn("phone", phone)
        self.assertNotIn("email", phone)
        self.assertTrue(json.loads(links[(oid("k5"), oid("k6"))]["why_not"]))
        counts = report["post_project"]["twins_contacts"]["counts"]
        self.assertEqual((counts["name_only"], counts["own_domain_card"],
                          counts["excluded_card"], counts["common_contact_only"],
                          counts["location_differs"]), (1, 2, 2, 2, 1))
        self.assertEqual(report["post_project"]["twins_contacts"]["link_tiers"],
                         {"corroborated": 5, "lead": 4})
        moved = links[(oid("l1"), oid("l2"))]
        self.assertEqual(json.loads(moved["why_not"])[0]["fields"], ["city"])
        place = {f["feature"]: f for f in json.loads(moved["reason"])}["location"]
        self.assertEqual({c["field_path"] for c in place["citations"]}, {"row.city"})

    def test_name_alone_other_company_excluded_and_own_cards_never_linked(self):
        conn, _ = self.build(guard=Guard())
        ends = {e for pair in self.twins(conn) for e in pair}
        for cid in ("k7", "k8", "k9", "d9", "k11", "k12", "k13", "k14", "c1", "c2", "o1",
                    "o2"):
            self.assertNotIn(oid(cid), ends, cid)

    def test_rejected_pair_either_way_round_is_never_linked(self):
        conn, report = self.build(reviews=contradict(oid("k2"), oid("k1")))
        self.assertNotIn((oid("k1"), oid("k2")), self.twins(conn))
        self.assertEqual(report["post_project"]["twins_contacts"]["counts"]["rejected"], 1)

    def test_no_exclusion_list_writes_nothing(self):
        conn, report = self.build(env={"PLX_EXCLUDED_PEOPLE_OIDS": "",
                                       "PLX_EXCLUSION_ENV_FILE": ""})
        self.assertEqual(self.twins(conn), {})
        self.assertEqual(report["post_project"]["twins_contacts"]["counts"],
                         {"exclusion_unavailable": 1})

    def test_real_exclusion_drops_own_domain_cards(self):
        conn, _ = self.build()
        ends = {e for pair in self.twins(conn) for e in pair}
        self.assertNotIn(oid("k11"), ends)
        self.assertIn((oid("k13"), oid("k14")), self.twins(conn))   # not an excluded one

    def test_twin_cards_never_make_their_customers_one_party(self):
        from ontology import sender_parties as sp
        from operating import source_precedence
        conn, _ = self.build(guard=Guard())
        self.assertEqual(self.twins(conn)[(oid("k1"), oid("k2"))]["tier"], "corroborated")
        cu1, cu2 = (f'Customer:bc.customers:{{"id":"{c}"}}' for c in ("cu1", "cu2"))
        linked = {(r[0], r[1]) for r in conn.execute(
            "SELECT from_object, to_object FROM links WHERE link_type = 'Contact.bc_customer'")}
        self.assertEqual(linked, {(oid("k1"), cu1), (oid("k2"), cu2)})
        parties = sp.read_parties(conn)
        self.assertNotEqual(parties.roots.get(cu1, cu1), parties.roots.get(cu2, cu2))
        self.assertEqual(parties.twins.get(cu1, []), [])
        clusters = source_precedence._clusters(conn, [cu1, cu2], "Customer")
        self.assertNotEqual(clusters[cu1], clusters[cu2])

    def test_cardinality_one_ten_sixty_cards(self):
        """N cards of one name and e-mail: every pair once, none lost, within budget."""
        import time
        for n in (1, 10, 60):
            extra = [card(f"m{i}", "Meridian Glass", "Customer", email="m@meridian.test")
                     for i in range(n)]
            start = time.monotonic()
            conn, _ = self.build({"bc_manufacturing": manufacturing(extra)}, guard=Guard())
            took = time.monotonic() - start
            mine = {oid(f"m{i}") for i in range(n)}
            got = [k for k in self.twins(conn) if set(k) <= mine]
            self.assertEqual(len(got), n * (n - 1) // 2, n)
            self.assertEqual(len(set(got)), len(got))
            self.assertLess(took, budget(60), n)

    def test_undeclared_link_type_writes_nothing(self):
        class Projector:
            links = {}
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE link_rows (link_id TEXT)")
        conn.execute("INSERT INTO link_rows VALUES ('crb-contact-old')")

        class Builder:
            connection = conn
        self.assertEqual(ct.contact_twins_step(Builder(), Projector()),
                         {"counts": {}, "link_tiers": {}})
        self.assertEqual(conn.execute("SELECT count(*) FROM link_rows").fetchone()[0], 0)

    def test_the_build_reads_the_capture_directory_for_outside_addresses(self):
        """``post_project`` hands the step its state directory; the guard then carries
        the mail capture's directory and listing (an excluded person's outside address)."""
        from integrations import people_exclusion as pe
        from operating import build_steps
        seen = []

        def attach(guard, state_dir):
            seen.append(state_dir)
            return Guard()
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, EXCLUSION), \
                mock.patch.object(pe, "with_directory", attach):
            self.assertIsInstance(ct._excluder(state), Guard)
            step = build_steps.post_project(state, "plx")["twins_contacts"]
            self.assertTrue(callable(step))
        self.assertEqual(seen, [state])

    def test_relation_values_fail_closed(self):
        self.assertEqual(ct.relation("Bank_x0020_Account"), "bank_account")
        self.assertEqual(ct.relation("Something New"), "unknown")
        self.assertEqual(ct.relation(None), "none")


if __name__ == "__main__":
    unittest.main()
