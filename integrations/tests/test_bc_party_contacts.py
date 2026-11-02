"""BC contact relations: which customer or vendor each contact belongs
to, read GET-only as the party listings' ``$expand=contactsInformation`` and stored one
record per (party, contact). Injected transport (``FakeBc``); the live shape is the
bounded read of 2026-11-02 recorded in live check ``bc.capture_full``."""
from __future__ import annotations

import json
import unittest
from urllib.parse import unquote, urlsplit

from integrations import bc_read
from integrations import live_source_grammar as grammar
from integrations.capture_store import bc_unit
from integrations.live_source_guard import LiveSourceAudience
from integrations.tests.test_bc_capture import CO, SERVICE, FakeBc, Harness
from ontology.core import Actor

SET = bc_read.PARTY_CONTACT_SET


def contact(n: int) -> str:
    return f"0c000000-0000-4000-8000-{n:012x}"


def relation(party: dict, contact_id: str, kind: str, **extra) -> dict:
    return {"@odata.etag": f'W/"R{contact_id[-4:]}"', "contactId": contact_id,
            "contactNumber": f"CT{contact_id[-4:]}", "contactName": "A Company",
            "contactType": "Company", "relatedId": party["id"], "relatedType": kind, **extra}


def link(fake: FakeBc, company: str, party_set: str, index: int, *relations) -> dict:
    party = fake.data[(company, party_set)][index]
    fake.party_contacts[(company, party_set, party["id"])] = list(relations)
    return party


def rows(h: Harness, company: str = CO[0]) -> list[dict]:
    return [r.payload["row"] for r in h.store.current_records("bc", bc_unit(company, SET))]


class _Mutating(FakeBc):
    """A fake whose customer pages of the first company pass through ``mutate``."""
    mutate = staticmethod(lambda row: row)

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        status, head, data = super().__call__(method, url, headers, body, timeout, max_bytes)
        if f"{CO[0]})/customers?" in unquote(url) and status == 200:
            page = json.loads(data)
            page["value"] = [self.mutate(r) for r in page["value"]]
            data = json.dumps(page).encode()
        return status, head, data


class PartyContactCaptureTests(unittest.TestCase):

    def test_relations_of_customers_and_vendors_are_stored_per_party_and_contact(self):
        fake = FakeBc({"customers": 3, "vendors": 2})
        customer = fake.data[(CO[0], "customers")][1]
        vendor = fake.data[(CO[0], "vendors")][0]
        # one contact related to a customer and a vendor (BC relation Multiple): two records
        link(fake, CO[0], "customers", 1, relation(customer, contact(1), "Customer"),
             relation(customer, contact(2), "Customer"))
        link(fake, CO[0], "vendors", 0, relation(vendor, contact(1), "Vendor"))
        h = Harness(self, fake)
        report = h.run(entity_sets=("customers", "vendors", SET), page_size=2)
        unit = h.unit(report, CO[0], SET)
        self.assertEqual((unit["state"], unit["records_current"]), ("complete", 3))
        held = sorted((r["partySet"], r["partyNumber"], r["contactId"], r["relatedId"])
                      for r in rows(h))
        self.assertEqual(held, sorted([
            ("customers", customer["number"], contact(1), customer["id"]),
            ("customers", customer["number"], contact(2), customer["id"]),
            ("vendors", vendor["number"], contact(1), vendor["id"])]))
        ids = {r["id"] for r in rows(h)}
        self.assertEqual(ids, {bc_read.party_contact_id("customers", customer["id"], contact(1)),
                               bc_read.party_contact_id("customers", customer["id"], contact(2)),
                               bc_read.party_contact_id("vendors", vendor["id"], contact(1))})
        # GET only, the one query shape, never a company-level listing BC refuses (400)
        sent = [unquote(u) for u in fake.bc_gets(SET)]
        self.assertTrue(sent)
        for url in sent:
            path = urlsplit(url).path
            self.assertRegex(path, r"/(customers|vendors)$")
            self.assertIn("$select=id,number&$expand=contactsInformation", url)
        self.assertTrue(all(m == "GET" for m, u, _ in fake.calls if "businesscentral" in u))
        # a second pass over unchanged relations adds no version
        again = h.run(entity_sets=(SET,), page_size=2)
        self.assertEqual(h.unit(again, CO[0], SET)["rows_changed"], 0)

    def test_a_relation_that_is_no_whole_record_of_its_party_is_dropped_and_the_unit_partial(self):
        for name, bad in (
                ("another party", lambda p: relation(p, contact(5), "Customer",
                                                     relatedId=contact(99))),
                ("wrong kind", lambda p: relation(p, contact(5), "Vendor")),
                ("no contact id", lambda p: relation(p, "not-a-guid", "Customer")),
                ("not a record", lambda p: "x")):
            with self.subTest(name):
                fake = FakeBc({"customers": 2, "vendors": 1})
                party = fake.data[(CO[0], "customers")][0]
                link(fake, CO[0], "customers", 0, relation(party, contact(1), "Customer"),
                     bad(party))
                h = Harness(self, fake)
                unit = h.unit(h.run(entity_sets=(SET,)), CO[0], SET)
                self.assertEqual(unit["state"], "partial")
                self.assertFalse(unit["complete"])
                self.assertEqual([r["contactId"] for r in rows(h)], [contact(1)])

    def test_a_party_whose_expansion_bc_paged_or_left_out_is_never_complete(self):
        for name, mutate in (("nested nextLink", lambda r: {
                                  **r, "contactsInformation@odata.nextLink": "https://x"}),
                             ("no expansion", lambda r: {k: v for k, v in r.items()
                                                         if k != "contactsInformation"})):
            with self.subTest(name):
                fake = _Mutating({"customers": 2, "vendors": 1})
                fake.mutate = mutate
                h = Harness(self, fake)
                unit = h.unit(h.run(entity_sets=(SET,)), CO[0], SET)
                self.assertEqual((unit["state"], unit["complete"]), ("partial", False))

    def test_cardinality_no_relation_lost_or_duplicated_across_pages(self):
        for n in (1, 10, 60):
            with self.subTest(parties=n):
                fake = FakeBc({"customers": n, "vendors": n})
                for i in range(n):
                    for party_set, kind in (("customers", "Customer"), ("vendors", "Vendor")):
                        party = fake.data[(CO[0], party_set)][i]
                        link(fake, CO[0], party_set, i,
                             relation(party, contact(1000 + i), kind))
                h = Harness(self, fake)
                unit = h.unit(h.run(entity_sets=(SET,), page_size=7), CO[0], SET)
                self.assertEqual((unit["state"], unit["records_current"]), ("complete", 2 * n))
                held = [(r["partySet"], r["relatedId"]) for r in rows(h)]
                self.assertEqual(len(held), len(set(held)))
                # one GET per page of parties (pages of 500), never one per party
                self.assertEqual(len(fake.bc_gets(rf"{CO[0]}\)/(customers|vendors)\?.*"
                                                  rf"{SET}")), 2)

    def test_a_reader_without_the_relation_set_reads_nothing(self):
        fake = FakeBc({"customers": 2, "vendors": 1})
        h = Harness(self, fake)
        actor = Actor("plx", frozenset({"staff:parties"}), "restricted")
        h.capture.reader._audience = LiveSourceAudience({
            "bc:production:companies": ["staff:parties"],
            f"bc:production:{CO[0]}:customers": ["staff:parties"],
            f"bc:production:{CO[0]}:vendors": ["staff:parties"]},
            tenant="plx", default_classification="restricted")
        report = h.capture.capture(actor, entity_sets=(SET,), page_size=2)
        self.assertEqual(h.unit(report, CO[0], SET)["state"], "not_visible")
        self.assertFalse(fake.bc_gets(SET))

    def test_the_grammar_admits_only_the_one_party_expansion(self):
        ok = grammar.bc_query_ok
        self.assertTrue(ok("customers", "%24select=id%2Cnumber&%24expand=contactsInformation"))
        self.assertTrue(ok("vendors", "%24select=id%2Cnumber&%24expand=contactsInformation"
                                      "&aid=FIN&%24skiptoken=abc"))
        for entity_set, query in (
                ("customers", "%24expand=contactsInformation"),
                ("customers", "%24select=id%2Cname&%24expand=contactsInformation"),
                ("customers", "%24select=id%2Cnumber&%24expand=contact"),
                ("customers", "%24select=id%2Cnumber&%24expand=contactsInformation"
                              "&%24filter=number+eq+%27X%27"),
                ("items", "%24select=id%2Cnumber&%24expand=contactsInformation"),
                ("contacts", "%24select=id%2Cnumber&%24expand=contactsInformation")):
            with self.subTest(entity_set=entity_set, query=query):
                self.assertFalse(ok(entity_set, query))
        with self.assertRaises(ValueError):
            grammar.bc_party_contacts_query("items")


if __name__ == "__main__":
    unittest.main()
