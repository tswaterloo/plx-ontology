"""plx.documents participant with a customer or vendor record also searches its BC
contacts' addresses: the contacts a visible Contact.bc_customer /
Contact.bc_vendor link joins to the record or its firm twins, and the people of those
company contacts (Contact.of_company). A reader without pii sees no contact. All
addresses are synthetic. Evidence label: isolated-fixture.
"""

from __future__ import annotations

import json
import unittest

from operating import ontology_tools as ot
from operating import ontology_tools_evidence as ev
from tests import test_mail_participants as mp
from tests import test_ontology_tools_evidence as fx

FIRM_DOMAIN = "acme-orders.test"
FIRM = "Contact:bc.contacts:c-firm"
PERSON = "Contact:bc.contacts:c-pat"
STRANGER = "Contact:bc.contacts:c-other"

DOMAIN = json.loads(json.dumps(mp.DOMAIN))
DOMAIN["object_types"].append(fx._type("Contact", [("display_name", "text"),
                                                   ("email", "text")],
                                       [("bc.contacts", "bc")], display=["display_name"]))
DOMAIN["link_types"] += [fx._link("Contact.bc_vendor", "Contact", "Supplier"),
                         fx._link("Contact.of_company", "Contact", "Contact")]
SCHEMA = ot.SchemaView.from_documents([DOMAIN])


def contacts(store, *, people=1):
    """The vendor's (S2, the BC twin of S1) company contact with its people, and one
    contact of nobody; every contact pii-marked as the curated Contact type is."""
    for oid, name, email in ((FIRM, "Acme Orders", f"orders@{FIRM_DOMAIN}"),
                             (STRANGER, "Other", "x@unrelated.test")):
        store.obj(oid, f"bc:{fx.CO}/contacts", f"bc://production/{fx.CO}/contacts/"
                  f"{oid[-6:]}@ck1", "bc_manufacturing", markings='["pii"]')
        store.val(oid, "display_name", "text", name)
        store.val(oid, "email", "text", email)
    store.link("l-cv", "Contact.bc_vendor", FIRM, mp.S2)
    for i in range(people):
        oid = PERSON if i == 0 else f"{PERSON}-{i}"
        store.obj(oid, f"bc:{fx.CO}/contacts", f"bc://production/{fx.CO}/contacts/p{i}@ck1",
                  "bc_manufacturing", markings='["pii"]')
        store.val(oid, "email", "text", f"pat{i}@{FIRM_DOMAIN}")
        store.link(f"l-pc{i}", "Contact.of_company", oid, FIRM)
    mp.message(store, "c-firm", sender=f"orders@{FIRM_DOMAIN}", to=[f"buyer@{mp.OURS}"],
               at="2099-02-12T09:00:00Z")
    mp.message(store, "c-pat", sender=f"buyer@{mp.OURS}", to=[f"pat0@{FIRM_DOMAIN}"],
               at="2099-02-27T09:00:00Z")
    mp.message(store, "c-other", sender="x@unrelated.test", to=[f"buyer@{mp.OURS}"],
               at="2099-02-27T10:00:00Z")


def tools(people=1, tenant=frozenset({mp.OURS})):
    store = fx.build()
    store.conn.execute("UPDATE store_meta SET value = ? WHERE key = 'schema_digest'",
                       ("sha256:" + SCHEMA.digest_hex,))
    mp.mail(store)
    contacts(store, people=people)
    store.conn.commit()
    base = ot.OntologyTools(ot.StoreReader(store.conn), mp.POLICY,
                            item_gate=lambda a, h: True, schema=SCHEMA, grammar=fx.identifiers,
                            contracts=fx.CONTRACTS, clock=lambda: fx.NOW)
    return ev.EvidenceTools(base, reader=mp.Reader(tenant))


class ParticipantContactTests(unittest.TestCase):
    def call(self, tools_, actor=mp.OPS, **arguments):
        return fx.ok(self, "plx.documents", tools_.call("plx.documents", arguments, actor))

    @staticmethod
    def keys(out):
        return [d["citation"].split("/")[3].split("@")[0] for d in out["documents"]]

    def test_a_vendor_record_finds_mail_with_its_contacts_and_their_people(self):
        t = tools()
        for root in (mp.S2, mp.S1):   # the BC vendor, and its Portal twin
            with self.subTest(root=root):
                out = self.call(t, participant=root)
                self.assertIn(f"orders@{FIRM_DOMAIN}", out["searched"]["participants"])
                self.assertIn(f"pat0@{FIRM_DOMAIN}", out["searched"]["participants"])
                self.assertNotIn("x@unrelated.test", out["searched"]["participants"])
                found = self.keys(out)
                # the last email with them is the one to their person
                self.assertEqual(found[0], "c-pat")
                self.assertIn("c-firm", found)
                self.assertNotIn("c-other", found)

    def test_a_reader_without_pii_reaches_no_contact_address(self):
        out = self.call(tools(), mp.NOPII, participant=mp.S2)
        self.assertEqual(out["documents"], [])
        self.assertEqual(out["searched"]["participants"], [])
        self.assertIn("participant_has_no_address", [g["reason"] for g in out["gaps"]])

    def test_many_people_are_searched_up_to_the_cap_and_the_cut_is_said(self):
        for people in (1, 10, 60):
            with self.subTest(people=people):
                out = self.call(tools(people=people, tenant=None), participant=mp.S2)
                searched = out["searched"]["participants"]
                self.assertLessEqual(len(searched), ev.MAX_PARTICIPANTS)
                self.assertEqual(len(searched), len(set(searched)))
                # S2's csr@, its twin S1's rep@ and backup@, orders@ and each person
                total = 4 + people
                cut = "participants_cut" in [g["reason"] for g in out["gaps"]]
                self.assertEqual(cut, total > ev.MAX_PARTICIPANTS)


if __name__ == "__main__":
    unittest.main()
