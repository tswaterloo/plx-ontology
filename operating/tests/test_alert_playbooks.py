"""Alert playbooks at their boundary: every alert type's next steps worded from the alert's
own cited sources as one reader reads them (a fake reader-scoped tool call), the finance
and pii markings bounding every value, deep links from configuration only, the change a
person makes in the source system (never written), and the playbook as an action card
(previewed only: no test confirms or sends anything).

SYNTHETIC records and handles only; evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import date
from operating.copy_gate import say_date
import json
from pathlib import Path
import unittest
import zlib

from operating import alert_playbooks as ap
from operating import briefing as bf
from operating import card_actions as ca
from operating.tests.test_card_actions import VINCE, ctx, desk

PO = "PurchaseOrder:bc.purchase_orders:synth-po-1"
SO = "SalesOrder:portal.app_sales_order:synth-so-1"
INV = "SalesInvoice:bc.sales_invoices:synth-inv-1"
ITEM = "Product:portal.app_product:synth-item-1"
SUP = "Supplier:bc.vendors:synth-sup-1"
CUS = "Customer:bc.customers:synth-cus-1"
BUYER = "Person:graph.users:synth-buyer"
FIN = '["finance"]'


def h(oid: str) -> str:
    return "synth://" + oid.replace(":", "/") + "@1"


class Store:
    """A reader-scoped ``call``: records, values (each with markings) and links."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[str, list[dict]]] = {}
        self.edges: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.handles: dict[str, str] = {}   # a display's citation other than h(oid)

    def add(self, oid: str, number: str, **values: tuple[str, list[str]]) -> str:
        self.objects[oid] = (number, [
            {"property": p, "value": {"untrusted_text": v}, "markings": marks,
             "citation": {"l1_handle": h(oid) + "#" + p}} for p, (v, marks) in values.items()])
        return oid

    def link(self, src: str, link_type: str, dst: str, tier: str = "verified") -> None:
        self.edges.append({"from": src, "to": dst, "link_type": link_type, "tier": tier})

    def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, dict(args)))
        if tool == "plx.object":
            found = self.objects.get(args["object_id"])
            if found is None:
                return {"error": {"code": "not_visible"}}
            return {"object": {"object_id": args["object_id"], "display": {
                "untrusted_text": found[0], "origin": {"citation": self.handles.get(
                    args["object_id"], h(args["object_id"]))}}},
                "values": found[1]}
        if tool == "plx.around":
            link = args["path"][0]["link_type"]
            return {"edges": [e for e in self.edges if e["from"] == args["from"]
                              and e["link_type"] == link]}
        raise AssertionError(tool)


def world() -> Store:
    s = Store()
    s.add(PO, "PO-SYNTH-1")
    s.add(SO, "SO-SYNTH-1")
    s.add(INV, "INV-SYNTH-1")
    s.add(ITEM, "ITEM-SYNTH")
    s.add(SUP, "Synth Supplies", rep_email=("rep@supplier.example", ["pii"]))
    s.add(CUS, "Synth Customer", email=("ap@customer.example", ["pii"]))
    s.add(BUYER, "Bea Buyer", email=("bea@plx.example", ["pii"]))
    s.link(PO, "PurchaseOrder.ordered_from", SUP)
    s.link(SO, "SalesOrder.ordered_by", CUS)
    s.link(INV, "SalesInvoice.billed_to", CUS)
    return s


def gap(kind: str, oid: str, **event) -> dict:
    # a stable id without digits (the hash of the run varied it, and a figure test looks
    # for digits in the whole playbook)
    tag = "".join(chr(97 + int(d)) for d in f"{zlib.crc32(oid.encode()) % 10**8:08d}")
    return {"id": f"pe-{kind[:6]}{tag}", "kind": kind,
            "object_id": oid, "object_type": oid.split(":", 1)[0],
            "summary": f"synthetic {kind}", "citations": [h(oid)], "event": event}


def build(g: dict, store: Store, held=("pii", "finance"), **kw) -> dict:
    return ap.build(g, bf.TYPE_OF_KIND.get(g["kind"], g["kind"]), call=store, held=held,
                    links=kw.pop("links", ap.load_links()), environ=kw.pop("environ", {}),
                    owner_links=kw.pop("owner_links", {}), **kw)


class EveryAlertType(unittest.TestCase):

    def test_every_alert_type_has_a_playbook_and_an_unknown_one_fails_closed(self):
        for event_type in bf.EVENT_TYPES:
            self.assertIsNotNone(ap.spec_of(event_type), event_type)
        found = build({**gap("late_po", PO), "kind": "made_up_kind"}, world())
        self.assertEqual((found["email"], found["teams"], found["change"]), (None, None, None))
        self.assertIn("no playbook for this alert type", found["missing"])
        self.assertEqual(found["local"], ["snooze", "assign", "mark_done"])
        self.assertEqual(found["links"][0]["url"], f"/object/PurchaseOrder/{ap.quote(PO, safe='')}")
        self.assertEqual((found["sent"], found["writes"]), (False, 0))

    def test_late_po_drafts_the_supplier_email_owner_note_and_the_bc_change(self):
        s = world()
        s.link(PO, "PurchaseOrder.buyer", BUYER)
        found = build(gap("late_po", PO), s,
                      owner_links={"PurchaseOrder": ("PurchaseOrder.buyer",)})
        email = found["email"]
        self.assertEqual(email["to"]["address"], "rep@supplier.example")
        self.assertEqual(email["party"], "supplier")
        self.assertIn("PO-SYNTH-1", email["body"])
        # the party is a firm: no greeting name and no fixed sign-off (the sender's voice)
        self.assertFalse(email["body"].startswith(("Hello", "Hi Synth")))
        self.assertFalse(email["body"].rstrip().endswith("Thank you."))
        self.assertTrue(email["copy_check"]["ok"])
        self.assertEqual(found["teams"]["owner"]["name"], "Bea Buyer")
        self.assertIn("Bea Buyer: Purchase order PO-SYNTH-1 is late", found["teams"]["body"])
        self.assertTrue(found["change"]["do_this"].startswith("In BC: "))
        self.assertEqual(found["change"]["writes"], 0)
        # every fact the drafts state is cited to the record it was read from
        self.assertEqual({f["fact"]: f["citation"] for f in found["facts"]}["record"], h(PO))
        # the reads were the reader's own tool calls, and none was a write
        self.assertTrue({t for t, _ in s.calls} <= {"plx.object", "plx.around"})

    def test_a_lead_or_ambiguous_party_is_never_addressed(self):
        s = world()
        s.edges = [e for e in s.edges if e["from"] != PO]
        s.link(PO, "PurchaseOrder.ordered_from", SUP, tier="lead")
        found = build(gap("late_po", PO), s)
        self.assertIsNone(found["email"]["to"])
        s.edges[-1]["tier"] = "verified"
        s.add("Supplier:bc.vendors:synth-sup-2", "Other", rep_email=("o@x.example", ["pii"]))
        s.link(PO, "PurchaseOrder.ordered_from", "Supplier:bc.vendors:synth-sup-2")
        found = build(gap("late_po", PO), s)
        self.assertIsNone(found["email"]["to"])
        self.assertIn("no single settled supplier is linked to the record for you",
                      found["missing"])

    def test_without_an_owner_link_the_note_says_so_and_names_nobody(self):
        found = build(gap("late_po", PO), world())
        self.assertFalse(found["teams"]["owner_known"])
        self.assertIsNone(found["teams"]["owner"])

    def test_stock_out_proposes_the_reorder_quantity_and_asks_the_inbound_supplier(self):
        g = gap("stock_out_risk", ITEM, shortfall="40", unit="kg", short_by="2026-11-05",
                first_short="2026-11-01", orders_affected=2, orders_hit=[SO], inbound=[PO])
        found = build(g, world())
        self.assertEqual(found["change"]["value"], "40")
        self.assertIn("Buy 40 kg more of item ITEM-SYNTH", found["change"]["do_this"])
        self.assertIn(f"needed by {say_date('2026-11-01')}", found["change"]["do_this"])
        self.assertEqual(found["email"]["to"]["address"], "rep@supplier.example")
        self.assertIn("PO-SYNTH-1", found["email"]["body"])
        self.assertIn(f"{say_date('2026-11-01')}", found["email"]["body"])

    def test_cost_jump_asks_the_supplier_of_the_latest_order(self):
        g = gap("unit_cost_jump", ITEM, change_pct="38.5", latest_order=PO, orders=[])
        found = build(g, world())
        self.assertIn("38.5%", found["email"]["body"])
        self.assertEqual(found["email"]["to"]["address"], "rep@supplier.example")
        self.assertEqual(found["change"]["field"], "Direct Unit Cost")

    def test_late_customer_order_and_at_risk_order_write_to_the_customer(self):
        found = build(gap("late_customer_order", SO, days=4, required="2026-10-30"), world())
        self.assertEqual(found["email"]["party"], "customer")
        self.assertEqual(found["email"]["to"]["address"], "ap@customer.example")
        self.assertIn(f"{say_date('2026-10-30')}", found["email"]["body"])
        found = build(gap("po_impact", PO, days=3, orders_affected=2), world())
        self.assertEqual(found["email"]["party"], "supplier")
        self.assertIn("holding up 2 customer orders", found["email"]["body"])


class Markings(unittest.TestCase):

    def overdue(self) -> dict:
        return gap("overdue_receivable", INV, days=12, amount={
            "value": "1200.5", "currency": "CAD", "basis": "remaining_amount",
            "handle": h(INV), "currency_handle": h(INV), "markings": FIN})

    def test_a_finance_reader_gets_the_amount_and_the_credit_hold(self):
        found = build(self.overdue(), world())
        self.assertIn("1,200.50 CAD", found["email"]["body"])
        self.assertIn("credit hold until 1,200.50 CAD", found["change"]["do_this"])
        self.assertEqual(found["change"]["value"], "credit_hold")

    def test_a_reader_without_finance_sees_no_figure_anywhere(self):
        found = build(self.overdue(), world(), held=("pii",))
        text = json.dumps(found)
        self.assertNotIn("1,200.50", text)
        self.assertNotIn("1200.5", text)
        # a step worded around a figure the reader may not see is not offered at all
        self.assertIsNone(found["email"])
        self.assertIsNone(found["change"])
        self.assertIn("a fact the message states is not readable for you", found["missing"])
        # unreadable markings fail closed for every reader
        g = self.overdue()
        g["event"]["amount"]["markings"] = "not json"
        self.assertIsNone(build(g, world())["email"])

    def test_an_address_needs_its_marking_and_an_unreadable_record_is_not_named(self):
        found = build(gap("late_po", PO), world(), held=("finance",))
        self.assertIsNone(found["email"]["to"])
        self.assertNotIn("rep@supplier.example", json.dumps(found))
        s = world()
        del s.objects[PO]   # the record itself is not this reader's to open
        found = build(gap("late_po", PO), s)
        self.assertIsNone(found["email"])
        self.assertNotIn("PO-SYNTH-1", json.dumps(found))


class ChiefExecutiveSignals(unittest.TestCase):

    def test_demand_drop_asks_the_customer_without_our_figures_and_tells_the_owner(self):
        g = gap("customer_demand_drop", CUS, change_pct="-45", amount={
            "value": "5000", "currency": "CAD", "basis": "demand_shortfall"})
        found = build(g, world())
        self.assertEqual(found["email"]["to"]["address"], "ap@customer.example")
        self.assertEqual(found["email"]["subject"], "Your recent orders")
        self.assertNotIn("5,000", found["email"]["body"])
        self.assertIn("45% less", found["teams"]["body"])
        self.assertIn("5,000.00 CAD", found["teams"]["body"])
        # the figure is exact (never rounded to cents) and an unreadable one is never said
        for value, said in (("5000.125", "5,000.125 CAD"), ("NaN", None), ("Infinity", None)):
            with self.subTest(value=value):
                body = build(gap("customer_demand_drop", CUS, change_pct="-45", amount={
                    "value": value, "currency": "CAD", "basis": "demand_shortfall"}),
                    world())["teams"]
                body = (body or {}).get("body") or ""
                if said:
                    self.assertIn(said, body)
                else:
                    self.assertNotIn("CAD", body)
                    self.assertNotIn("NaN", body)
                    self.assertNotIn("Infinity", body)
        # a reader without finance: no figure anywhere, even though this amount names no
        # markings of its own (the alert type is finance-marked)
        hidden = build(g, world(), held=("pii",))
        self.assertNotIn("5,000", json.dumps(hidden))
        self.assertNotIn("45", json.dumps(hidden))
        self.assertIsNone(hidden["teams"])

    def test_promise_slip_writes_to_the_customer_with_the_counts(self):
        found = build(gap("promise_date_slip", CUS, orders_affected=3, days=9,
                          slipped=[SO]), world())
        self.assertIn("3 of your recent orders missed the date we promised, the worst by 9",
                      found["email"]["body"])
        self.assertEqual(found["change"]["field"], "Promised Delivery Date")

    def test_margin_erosion_is_internal_and_finance_only(self):
        g = gap("margin_erosion", ITEM, change_points="-6.5", recent_pct="12.0",
                trailing_pct="18.5", amount={"value": "800", "currency": "USD",
                                             "basis": "margin_shortfall"})
        found = build(g, world())
        self.assertIsNone(found["email"])
        self.assertIn("item ITEM-SYNTH fell 6.5 points to 12.0%", found["teams"]["body"])
        self.assertIsNone(build(g, world(), held=("pii",))["teams"])

    def test_supplier_reliability_asks_the_supplier_about_lead_times(self):
        found = build(gap("supplier_reliability", SUP, late_share="40.0",
                          late_share_before="10.0", orders_affected=4, days=12), world())
        self.assertEqual(found["email"]["to"]["address"], "rep@supplier.example")
        self.assertEqual(found["email"]["subject"], "Your recent deliveries")
        self.assertIn("40.0% of your recent deliveries", found["email"]["body"])
        self.assertEqual(found["change"]["field"], "Lead Time Calculation")


class Links(unittest.TestCase):

    def test_source_link_only_from_configured_https_base(self):
        g = gap("late_po", PO)
        none = build(g, world())["links"][1]
        self.assertEqual((none["status"], none["url"]), ("not_configured", None))
        self.assertIn("PLX_BC_WEB_URL", none["reason"])
        plain = build(g, world(), environ={"PLX_BC_WEB_URL": "http://bc.example/x"})["links"][1]
        self.assertEqual(plain["status"], "not_configured")
        base = {"PLX_BC_WEB_URL": "https://businesscentral.example/t/env?company=Synth"}
        # the record's company is read from its cited number's handle, never the base's:
        # every company the books file names opens in its own company
        books = json.loads((Path(__file__).resolve().parents[2] / "integrations"
                            / "bc_company_books.json").read_text())["names"]
        for env, companies in books.items():
            for guid, company in companies.items():
                with self.subTest(company=company):
                    store = world()
                    store.handles[PO] = f"bc://{env}/{guid}/purchaseOrders/synth-po-1@g"
                    ready = build(g, store, environ=base)["links"][1]
                    self.assertEqual(ready["url"], "https://businesscentral.example/t/env?"
                                     f"company={ap.quote(company, safe='')}&page=50&filter="
                                     "'Purchase%20Header'.'No.'%20IS%20'PO-SYNTH-1'")
        # a handle naming no known company (another system's, an unknown company id, an
        # environment the books do not name) gets no link: never the base's company
        for handle in ("synth://po@1", "bc://production/00000000-0000-0000-0000-000000000000/"
                       "purchaseOrders/x@g", f"bc://elsewhere/{guid}/purchaseOrders/x@g"):
            with self.subTest(handle=handle):
                store = world()
                store.handles[PO] = handle
                unknown = build(g, store, environ=base)["links"][1]
                self.assertEqual((unknown["status"], unknown["url"]), ("not_configured", None))
                self.assertIn("company is not known", unknown["reason"])
        portal = build(gap("late_customer_order", SO, required="2026-10-30"), world(),
                       environ={"PLX_PORTAL_WEB_URL": "https://portal.example"})["links"][1]
        self.assertEqual(portal["status"], "not_configured")


class AsCard(unittest.TestCase):

    def test_the_playbook_card_previews_through_the_action_desk_and_checks_figures(self):
        found = build(gap("eta_slipped", PO, days=6, date_was="2026-10-20",
                          date_now="2026-10-26"), world())
        card = ap.card(found)
        kinds = [o["kind"] for o in ca.offers(card)]
        self.assertEqual(kinds[:2], ["send_email", "teams_message"])
        self.assertIn("assign", kinds)
        d, graph = desk()
        shown = d.preview(card, "send_email", None, ctx())
        self.assertEqual(shown["problems"], [])
        self.assertEqual(shown["payload"]["to"], ["rep@supplier.example"])
        self.assertIn(f"{say_date('2026-10-20')}", shown["payload"]["body"])
        edited = d.preview(card, "send_email", {"body": shown["payload"]["body"]
                                                + " Ship 500 by 2026-12-01."}, ctx())
        self.assertTrue(any("Not backed by a citation" in p for p in edited["problems"]))
        teams = d.preview(card, "teams_message", None, ctx())
        self.assertEqual(teams["payload"]["body"], found["teams"]["body"])
        self.assertEqual(graph.calls, [])   # a preview sends nothing

    def test_assign_is_a_colleague_only_kept_in_our_journal_and_shown_to_them(self):
        card = ap.card(build(gap("late_po", PO), world()))
        d, graph = desk()
        self.assertIn("outside the company", " ".join(d.preview(
            card, "assign", {"assignee": "rep@supplier.example"}, ctx())["problems"]))
        self.assertIn("not available", " ".join(d.preview(
            card, "assign", {"assignee": "ana@plx.example"},
            ctx(exclusion=None))["problems"]))
        ok = d.preview(card, "assign", {"assignee": "ana@plx.example"}, ctx())
        self.assertEqual(ok["problems"], [])
        self.assertFalse(ok["outbound"])
        # the journal row a confirmed assignment writes (appended directly: no confirm)
        with d.journal.locked() as held:
            held.append({"schema": ca.SCHEMA, "card_id": card["id"], "kind": "assign",
                         "idempotency_key": "a" * 20, "payload_hash": ok["payload_hash"],
                         "actor": {"name": "Vince", "email": VINCE}, "status": "assigned",
                         "assignee": "ana@plx.example", "at": "2026-11-04T15:00:00+00:00"})
        theirs = d.states("ana@plx.example", "Ana", date(2026, 11, 4))
        self.assertEqual(theirs[card["id"]]["text"], "Assigned to ana@plx.example by Vince")
        self.assertEqual(d.states("other@plx.example", "Other", date(2026, 11, 4)), {})
        self.assertEqual(graph.calls, [])


class Cardinality(unittest.TestCase):

    def test_one_playbook_per_alert_at_1_10_60(self):
        for n in (1, 10, 60):
            s = world()
            gaps = []
            for i in range(n):
                oid = f"PurchaseOrder:bc.purchase_orders:synth-po-n{i}"
                s.add(oid, f"PO-N{i}")
                s.link(oid, "PurchaseOrder.ordered_from", SUP)
                gaps.append({**gap("late_po", oid), "id": f"pe-n{i:014d}"})
            built = [build(g, s) for g in gaps]
            self.assertEqual(len({b["id"] for b in built}), n)
            self.assertEqual([b["email"]["body"].count(f"PO-N{i}") for i, b in enumerate(built)],
                             [1] * n)
            # each record is read once per playbook at most twice (record + party)
            self.assertLessEqual(len(s.calls), n * 6)


if __name__ == "__main__":
    unittest.main()


class OverTheApi(unittest.TestCase):

    def test_the_route_serves_only_alerts_on_your_list_and_actions_take_its_id(self):
        from operator_api.tests.test_app_api import signed_in
        client = signed_in()
        top = client.get("/api/briefing").json()["items"]
        found = client.get(f"/api/alerts/{top[0]['id']}/playbook")
        self.assertEqual(found.status_code, 200, found.text)
        body = found.json()
        self.assertEqual((body["sent"], body["writes"]), (False, 0))
        self.assertEqual(body["id"], ap.CARD_PREFIX + top[0]["id"])
        self.assertEqual(client.get("/api/alerts/pe-not-yours/playbook").status_code, 404)
        # an action names a playbook card by id; one not on the reader's list is refused
        refused = client.post("/api/actions/preview", json={"card_id": "pb-pe-not-yours",
                                                            "kind": "snooze"})
        self.assertEqual(refused.status_code, 404)
        shown = client.post("/api/actions/preview", json={"card_id": body["id"],
                                                          "kind": "snooze"})
        self.assertEqual(shown.status_code, 200, shown.text)
        self.assertFalse(shown.json()["outbound"])
