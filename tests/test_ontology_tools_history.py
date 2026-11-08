"""``plx.object`` history and as_known_at: a record's changes and the record as recorded
then, as the asking reader may read them (markings judged then and since), paged."""
from __future__ import annotations

import json
import time
import unittest

from operating import ontology_tools as ot
from operating import ontology_tools_history as oth
from tests.test_ontology_tools import (CONTRACTS, DOC, FM, FULL, INV, SALES, SO, SO_V1,
                                       SO_V2, ContractMixin, build, gate_denying, tools)

PO = "PurchaseOrder:portal.app_purchase_order:po-h"


def _handle(i: int) -> str:
    return f"api://public.app_purchase_order/po-h@00000000000000{i:02d}-1"


def _at(i: int) -> str:
    return f"2099-01-{1 + i // 24:02d}T{i % 24:02d}:00:00Z"


def churn(store, n: int, links: int = 0):
    """A purchase order captured ``n`` times, each version a new po_number; ``links``
    invoices linked to it, one per hour, every other one superseded later."""
    for i in range(n):
        sup = _at(i + 1) if i < n - 1 else None
        store.obj(PO, "api:public.app_purchase_order", _handle(i), "gold_etl_from_fm",
                  recorded=_at(i), superseded=sup, add_object=i == 0)
        store.val(PO, "po_number", "identifier", f"PO{7000 + i}", _handle(i), "row.po_number",
                  "gold_etl_from_fm", recorded=_at(i), superseded=sup)
    for j in range(links):
        inv = f"SalesInvoice:bc.sales_invoices:h-{j}"
        handle = f"bc://company_a/salesInvoices/h-{j}@cap"
        store.obj(inv, "api:public.app_sales_invoice", handle, "gold_etl_from_fm")
        store.link(f"lnk-h-{j:03d}", "SalesInvoice.invoices_order", inv, PO, "verified", "fk",
                   [handle])
        store.conn.execute("UPDATE link_rows SET recorded_at = ?, superseded_at = ? "
                           "WHERE link_id = ?", (_at(j), _at(j + 5) if j % 2 else None,
                                                 f"lnk-h-{j:03d}"))
    store.conn.commit()
    return store


class HistoryTests(ContractMixin, unittest.TestCase):
    def walk(self, t, actor, oid, **extra):
        """Every page of the history, by cursor; each page within the contract."""
        events, cursor, pages = [], None, 0
        while True:
            args = dict({"object_id": oid, "history": True}, **extra)
            if cursor:
                args["cursor"] = cursor
            page = self.ok(t, "plx.object", args, actor)
            events += page["history"]
            pages += 1
            cursor = page["completeness"]["next_cursor"]
            self.assertEqual(page["completeness"]["truncated"], cursor is not None)
            if cursor is None:
                return events, page, pages
            self.assertLess(pages, 50)

    def test_changes_are_listed_newest_first_with_values_before_and_after_and_their_source(self):
        events, page, _ = self.walk(tools(), FULL, SO)
        kinds = [(e["at"], e["kind"]) for e in events]
        self.assertEqual(kinds[0], ("2099-01-05T00:00:00Z", "changed"))
        self.assertIn(("2099-01-01T00:00:00Z", "first_recorded"), kinds)
        changed = events[0]
        self.assertEqual(changed["source"]["l1_handle"], SO_V2)
        self.assertEqual(changed["source"]["system"], "portal_api")
        po = {c["property"]: c for c in changed["changes"]}["customer_po"]
        self.assertEqual([v["citation"]["l1_handle"] for v in po["before"]], [SO_V1])
        self.assertEqual([v["citation"]["l1_handle"] for v in po["after"]], [SO_V2])
        # an unchanged value across the two versions is no change
        self.assertNotIn("order_number", {c["property"] for c in changed["changes"]})
        links = {e["link"]["other"]["object_id"] for e in events if e["kind"] == "link_added"}
        self.assertIn(INV, links)
        self.assertNotIn("withheld", page)

    def test_a_restricted_reader_sees_no_value_or_link_it_may_not_read_then_or_since(self):
        s = build()
        # order_number: plain when first recorded, marked finance on a later capture
        s.conn.execute("UPDATE object_values SET markings = '[\"finance\"]' "
                       "WHERE object_id = ? AND property = 'order_number' "
                       "AND l1_handle = ?", (SO, SO_V2))
        s.conn.execute("UPDATE link_rows SET markings = '[\"finance\"]' "
                       "WHERE link_id = 'lnk-same-1'")
        s.conn.commit()
        t = tools(s)
        events, page, _ = self.walk(t, SALES, SO)
        text = json.dumps(events)
        for hidden in ("1250.50", "total_amount", "SO-2099-0042", INV, FM):
            self.assertNotIn(hidden, text)
        self.assertEqual(page["withheld"]["properties"], None)   # said, never counted
        # the same reader's as-of read of the earlier version does not read around the
        # later marking either; the full reader reads it
        early = self.ok(t, "plx.object", {"object_id": SO, "as_known_at": "2099-01-03"}, SALES)
        self.assertNotIn("order_number", {v["property"] for v in early["values"]})
        full = self.ok(t, "plx.object", {"object_id": SO, "as_known_at": "2099-01-03"}, FULL)
        self.assertIn("order_number", {v["property"] for v in full["values"]})
        # a reader that may not read the record gets the not-found every reader gets
        outsider = tools(s).call("plx.object", {"object_id": SO, "history": True},
                                 ot.Actor("plx", frozenset({"nobody"})))
        self.assertEqual(outsider["error"]["code"], "object_not_found")

    def test_a_version_the_item_gate_denies_shows_none_of_its_values(self):
        s = build()
        draft = "sharepoint://drive-1/item-1@etag0"
        s.obj(DOC, "sharepoint:drive-1", draft, "sharepoint", recorded="2099-01-05T00:00:00Z",
              superseded="2099-01-10T00:00:00Z", add_object=False)
        s.val(DOC, "file_name", "text", "Draft layoffs list.pdf", draft, "row.name",
              "sharepoint", recorded="2099-01-05T00:00:00Z", superseded="2099-01-10T00:00:00Z")
        s.conn.commit()
        events, page, _ = self.walk(tools(s, item_gate=gate_denying(draft)), SALES, DOC)
        self.assertNotIn(draft, json.dumps(events))
        self.assertNotIn("layoffs", json.dumps(events))
        self.assertIn("withheld", page)
        # the source stopped holding it: said with its basis
        self.assertEqual([(e["at"], e["basis"]) for e in events
                          if e["kind"] == "removed_from_source"],
                         [("2099-01-20T00:00:00Z", "missing_from_source")])
        shown, _, _ = self.walk(tools(s), SALES, DOC)
        self.assertIn("layoffs", json.dumps(shown))   # the gate, not the fixture, hid it

    def test_a_link_whose_only_evidence_is_a_denied_mail_item_is_never_listed(self):
        mail = "graph-mail://user-x/msg-secret@1"
        s = churn(build(), 2)
        for name, superseded in (("added", None), ("removed", "2099-01-03T00:00:00Z")):
            inv = f"SalesInvoice:bc.sales_invoices:mail-{name}"
            s.obj(inv, "api:public.app_sales_invoice", f"bc://company_a/salesInvoices/{name}@c",
                  "gold_etl_from_fm")
            s.link(f"lnk-mail-{name}", "SalesInvoice.invoices_order", inv, PO, "verified",
                   "fk", [mail])
            s.conn.execute("UPDATE link_rows SET superseded_at = ? WHERE link_id = ?",
                           (superseded, f"lnk-mail-{name}"))
        s.conn.commit()
        for actor in (FULL, SALES):
            with self.subTest(principals=sorted(actor.principals)):
                shown, _, _ = self.walk(tools(s), actor, PO)
                kinds = {(e["kind"], e["link"]["other"]["object_id"].rsplit("-", 1)[-1])
                         for e in shown if "link" in e}
                self.assertEqual(kinds, {("link_added", "added"), ("link_added", "removed"),
                                         ("link_removed", "removed")})
                denied, page, _ = self.walk(tools(s, item_gate=gate_denying(mail)), actor, PO)
                self.assertNotIn("mail-", json.dumps(denied))
                self.assertFalse([e for e in denied if "link" in e])
                self.assertEqual(page["withheld"], {"properties": None, "objects": None,
                                                    "reason": "audience"})

    def test_every_change_and_link_is_listed_once_across_pages(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                t = tools(churn(build(), n, links=n))
                began = time.monotonic()
                events, _, pages = self.walk(t, FULL, PO)
                spent = time.monotonic() - began
                values = [e for e in events if e["kind"] in ("changed", "first_recorded")]
                self.assertEqual(len(values), n)
                self.assertEqual(sorted(e["changes"][0]["after"][0]["value"] for e in values),
                                 sorted(f"PO{7000 + i}" for i in range(n)))
                added = [e["link"]["other"]["object_id"] for e in events
                         if e["kind"] == "link_added"]
                removed = [e for e in events if e["kind"] == "link_removed"]
                self.assertEqual(sorted(added), sorted(
                    f"SalesInvoice:bc.sales_invoices:h-{j}" for j in range(n)))
                self.assertEqual(len(removed), n // 2)
                keys = [(e["at"], e["kind"], json.dumps(e.get("link"))) for e in events]
                self.assertEqual(len(keys), len(set(keys)))
                self.assertEqual([e["at"] for e in events],
                                 sorted((e["at"] for e in events), reverse=True))
                self.assertLessEqual(max(1, -(-len(events) // oth.PAGE)), pages)
                self.assertLess(spent, 10.0)

    def test_a_tight_link_scan_still_lists_every_change_once(self):
        # the link scan ends a page at its frontier (rows read before every row is):
        # nothing past it is listed out of order, nothing before it is skipped
        saved = (oth.PAGE, oth.LINK_BATCH, oth.LINK_SCAN_CAP)
        oth.PAGE, oth.LINK_BATCH, oth.LINK_SCAN_CAP = 4, 3, 5
        try:
            t = tools(churn(build(), 10, links=30))
            events, _, pages = self.walk(t, FULL, PO)
        finally:
            oth.PAGE, oth.LINK_BATCH, oth.LINK_SCAN_CAP = saved
        self.assertEqual(sum(e["kind"] == "link_added" for e in events), 30)
        self.assertEqual(sum(e["kind"] == "link_removed" for e in events), 15)
        self.assertEqual(sum(e["kind"] in ("changed", "first_recorded") for e in events), 10)
        keys = [(e["at"], e["kind"], json.dumps(e.get("link"))) for e in events]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual([e["at"] for e in events],
                         sorted((e["at"] for e in events), reverse=True))
        self.assertGreater(pages, 55 // 4)

    def test_the_history_as_known_at_a_past_time_ends_there(self):
        t = tools(churn(build(), 10, links=10))
        events, _, _ = self.walk(t, FULL, PO, as_known_at="2099-01-01T04:30:00Z")
        self.assertTrue(events)
        self.assertTrue(all(e["at"] <= "2099-01-01T04:30:00Z" for e in events))

    def test_an_unknown_or_foreign_cursor_fails_closed(self):
        t = tools(churn(build(), 60))
        first = self.ok(t, "plx.object", {"object_id": PO, "history": True})
        cursor = first["completeness"]["next_cursor"]
        self.assertIsNotNone(cursor)
        for bad, args in (("forged", {"object_id": PO, "history": True, "cursor": "h1.e30"}),
                          ("garbage", {"object_id": PO, "history": True, "cursor": "zz"}),
                          ("other record", {"object_id": SO, "history": True, "cursor": cursor}),
                          ("no history", {"object_id": PO, "cursor": cursor})):
            with self.subTest(bad):
                out = t.call("plx.object", args, FULL)
                self.assertContract("plx.object", out)
                self.assertEqual(out["error"]["code"], "invalid_input")

    def test_the_contract_lists_history_on_plx_object_and_no_new_tool(self):
        listed = CONTRACTS["tools"]["plx.object"]["input"]["properties"]
        self.assertEqual(listed["history"], {"type": "boolean"})
        self.assertNotIn("plx.history", CONTRACTS["tools"])


if __name__ == "__main__":
    unittest.main()
