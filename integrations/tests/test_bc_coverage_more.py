"""BC coverage: the item-ledger lot page, PLX's own API page and the
new master sets, read GET-only through the one guarded client.

Evidence label: injected-transport (``test_bc_capture.FakeBc`` behind the real
``GuardedTransport``); the shapes are the live BC ``$metadata`` / ODataV4 ``$metadata``
names of 2026-11-01 (counts and names only).
"""

from __future__ import annotations

import unittest

from integrations import live_source_grammar as grammar
from integrations.bc_read import CAPTURE_ENTITY_SETS, LOT_SET
from integrations.capture_store import CaptureError, bc_unit
from integrations.live_source_guard import LiveSourceAudience, ReadOnlyGuardError, check_request
from integrations.tests.test_bc_capture import CO, FakeBc, Harness, SERVICE, gid
from integrations.tests.test_live_source_adapters import ENV, TENANT_ID
from ontology.core import Actor

HOST = "https://api.businesscentral.dynamics.com"
LOTS = (f"{HOST}/v2.0/{TENANT_ID}/Production/ODataV4/Company('My%20Company')"
        "/ItemLedgerEntries")
SHIP_TOS = (f"{HOST}/v2.0/{TENANT_ID}/Production/api/plx/integration/v1.0/"
            f"companies({CO[1]})/plxCustomerShipTos")
LEDGER = "itemLedgerEntries"


def guarded(url: str, method: str = "GET") -> bool:
    try:
        check_request(method, url, environ=ENV)
    except ReadOnlyGuardError:
        return False
    return True


def ledger_fake(lots: list[tuple[int, str, str]]) -> FakeBc:
    """A fake BC whose My Company (CO[1]) item ledger holds four entries (entry numbers
    1-4, items I1-I4) and whose ODataV4 page lists ``lots`` as (entry, item, lot)."""
    fake = FakeBc({LEDGER: 4})
    for n, row in enumerate(fake.data[(CO[1], LEDGER)]):
        row.update({"entryNumber": f"#{n + 1}", "itemNumber": f"I{n + 1}"})
    fake.lots[CO[1]] = [{"@odata.etag": f'W/"L{entry}"', "Entry_No": f"#{entry}",
                         "Item_No": item, "Lot_No": lot, "Expiration_Date": "2027-01-31",
                         "Location_Code": "MAIN", "Document_No": f"D{entry}"}
                        for entry, item, lot in lots]
    return fake


def harness(test, fake: FakeBc) -> Harness:
    h = Harness(test, fake)
    h.capture.books = {"production": {CO[1]: "manufacturing"}}
    h.capture.names = {"production": {CO[1]: "My Company"}}
    return h


class GrammarTests(unittest.TestCase):
    def test_the_lot_read_is_the_one_built_query_and_nothing_else(self):
        url = grammar.url("bc.odata_item_ledger_entries", grammar.bc_odata_item_lot_query(),
                          company="My Company", tenant=TENANT_ID, environment="Production")
        self.assertTrue(url.startswith(LOTS + "?"))
        self.assertTrue(guarded(url))
        built = url.split("?", 1)[1]
        for query in ("", "%24select=Entry_No%2CLot_No",                 # no filter / narrower
                      built + "&%24expand=X", built + "&%24top=5",
                      built.replace("ne", "eq"),                        # another filter
                      "%24select=Entry_No%2CItem_No%2CLot_No%2CExpiration_Date%2C"
                      "Location_Code%2CDocument_No%2CCost_Amount_Actual&%24filter=Lot_No"
                      "%20ne%20%27%27"):
            self.assertFalse(guarded(f"{LOTS}?{query}" if query else LOTS), query)
        for method in ("POST", "PATCH", "DELETE"):
            self.assertFalse(guarded(url, method), method)
        for bad in (LOTS + "(1)?" + built, LOTS.replace("My%20Company", "CRONUS%20Canada")
                    + "?" + built, LOTS + "/Microsoft.NAV.post?" + built):
            self.assertFalse(guarded(bad), bad)

    def test_a_plx_api_page_is_listed_whole_under_its_route_and_never_otherwise(self):
        self.assertTrue(guarded(SHIP_TOS))
        self.assertTrue(guarded(SHIP_TOS + "?%24skiptoken=2"))   # a continuation
        for bad in (SHIP_TOS + "?%24filter=code%20eq%20%27A%27", SHIP_TOS + "?%24top=1",
                    SHIP_TOS + f"({gid(1, 1, 1)})", SHIP_TOS.replace("plxCustomerShipTos",
                                                                     "plxEverything"),
                    SHIP_TOS.replace("/api/plx/integration/v1.0/", "/api/plx/other/v1.0/"),
                    # a PLX page is no api/v2.0 entity set, and employees stay out
                    SHIP_TOS.replace("/api/plx/integration/v1.0/", "/api/v2.0/"),
                    SHIP_TOS.replace("plxCustomerShipTos", "employees")):
            self.assertFalse(guarded(bad), bad)
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertFalse(guarded(SHIP_TOS, method), method)
        self.assertNotIn("employees", grammar.BC_ENTITY_SETS)


class LotCaptureTests(unittest.TestCase):
    def test_lots_are_held_under_their_ledger_entry_and_mismatches_never_stored(self):
        fake = ledger_fake([(1, "I1", "LOT-A"), (2, "I2", "LOT-B"), (3, "WRONG", "LOT-C"),
                            (9, "I9", "LOT-Z")])
        h = harness(self, fake)
        report = h.run(entity_sets=(LEDGER,))
        unit = h.unit(report, CO[1], LOT_SET)
        self.assertEqual((unit["state"], unit["reason"]), ("partial", "withheld_or_dropped"))
        self.assertEqual(unit["lots"], {"no_entry_number": 0, "ambiguous_api_record": 0,
                                        "duplicate_entry": 0, "item_mismatch": 1,
                                        "no_api_record": 1, "stored": 2})
        rows = h.store.records(SERVICE, "bc", bc_unit(CO[1], LOT_SET), limit=10)["items"]
        ledger_ids = {r["row"]["entryNumber"]: r["row"]["id"] for r in h.store.records(
            SERVICE, "bc", bc_unit(CO[1], LEDGER), limit=10)["items"]}
        self.assertEqual({r["row"]["Lot_No"]: r["row"]["id"] for r in rows},
                         {"LOT-A": ledger_ids["1"], "LOT-B": ledger_ids["2"]})
        for row in rows:
            self.assertTrue(row["citation"].startswith(f"bc://production/{CO[1]}/{LOT_SET}/"))
        # every lot GET is the one built read, by the pinned company name
        lot_gets = fake.bc_gets(r"/ItemLedgerEntries")
        self.assertEqual(len(lot_gets), 1)
        for url in lot_gets:
            check_request("GET", url, environ=ENV)

    def test_bc_holding_no_lot_is_a_complete_empty_unit_and_an_unsettled_ledger_reads_none(self):
        h = harness(self, ledger_fake([]))
        unit = h.unit(h.run(entity_sets=(LEDGER,)), CO[1], LOT_SET)
        self.assertEqual((unit["state"], unit["records_current"]), ("complete", 0))
        fake = ledger_fake([(1, "I1", "LOT-A")])
        fake.fail(rf"{CO[1]}\)/itemLedgerEntries", (403, {}, b'{"error":{"code":"x"}}'))
        h = harness(self, fake)
        unit = h.unit(h.run(entity_sets=(LEDGER,)), CO[1], LOT_SET)
        self.assertEqual((unit["state"], unit["reason"]), ("skipped", "item_ledger_not_settled"))
        self.assertFalse(fake.bc_gets(r"/ItemLedgerEntries"))

    def test_a_reader_sees_the_lots_exactly_as_it_sees_the_item_ledger(self):
        h = harness(self, ledger_fake([(1, "I1", "LOT-A")]))
        h.run(entity_sets=(LEDGER,))
        audience = LiveSourceAudience({
            f"bc:production:{CO[1]}:itemledgerentries": ["staff:stock"],
            f"bc:production:{CO[1]}:purchaseorders": ["staff:buyer"]},
            tenant="plx", default_classification="restricted")
        h.store.audience = audience
        stock = Actor("plx", frozenset({"staff:stock"}), "restricted")
        buyer = Actor("plx", frozenset({"staff:buyer"}), "restricted")
        self.assertEqual(len(h.store.records(stock, "bc", bc_unit(CO[1], LOT_SET))["items"]), 1)
        with self.assertRaises(CaptureError):
            h.store.records(buyer, "bc", bc_unit(CO[1], LOT_SET))


class NewSetTests(unittest.TestCase):
    def test_the_new_sets_and_the_ship_to_page_are_captured_and_an_unpublished_page_is_a_gap(self):
        sets = ("dimensions", "dimensionValues", "projects", "fixedAssets", "bankAccounts",
                "plxCustomerShipTos")
        self.assertTrue(set(sets) <= set(CAPTURE_ENTITY_SETS))
        fake = FakeBc({name: 3 for name in sets})
        del fake.data[(CO[2], "plxCustomerShipTos")]   # not published there: 404
        h = Harness(self, fake)
        report = h.run(entity_sets=sets)
        for company in CO:
            for name in sets:
                unit = h.unit(report, company, name)
                if (company, name) == (CO[2], "plxCustomerShipTos"):
                    self.assertEqual((unit["state"], unit["reason"]), ("failed", "not_exposed"))
                else:
                    self.assertEqual((unit["state"], unit["records_current"]), ("complete", 3),
                                     (company, name))
        self.assertFalse(report["all_complete"])
        ship_tos = fake.bc_gets(r"/plxCustomerShipTos")
        self.assertTrue(ship_tos)
        for url in ship_tos:
            self.assertIn("/api/plx/integration/v1.0/companies(", url)
            check_request("GET", url, environ=ENV)
        self.assertEqual({m for m, u, _h in fake.calls if "businesscentral" in u}, {"GET"})


if __name__ == "__main__":
    unittest.main()
