"""Proactive suggestions at their output contract: the ETA answer fails closed, a draft is
worded from cited values only (never a message's text, never an uncleared sender), every
finding is either a card or counted, and nothing is ever sent."""

from __future__ import annotations

import json
import unittest

from operating import proactive_suggestions as ps

PO = "PurchaseOrder:t:1"
H = "bc://t/purchaseOrders/1@v"
MAIL = "graph-mail://box/messages/m1"
SECRET = "Internal: margin is thin, push them hard"   # a message's own words


def item(rank, event_type="po_late", oid=PO, days=10, **extra):
    return {"rank": rank, "id": f"g{rank}", "event_type": event_type, "object_id": oid,
            "object_type": oid.split(":")[0], "summary": extra.pop(
                "summary", "An open purchase order needed by 2026-07-14 has no supplier "
                           "confirmation"),
            "citations": [H], "facts": {"days": days, **extra.pop("facts", {})},
            "next_action": {"text": "Ask the supplier.", "rule": "late_ask_supplier"},
            **extra}


def trail(eta=None, steps=(), contact=None, hidden=0, may_hide=False):
    src = {"handle": MAIL, "label": "Email", "system": "Email", "open_url": "/s"}
    return {"kind": "purchase_order", "trails": [{
        "subject": {"object_id": PO}, "state": "late", "days_late": 12,
        "verdict": "Late: needed by 2026-07-14.", "eta": eta, "steps": list(steps),
        "next_action": {"text": "Ask the vendor.", "rule": "no_confirmation",
                        "contact": contact, "contact_source": src if contact else None},
        "hidden_steps": hidden, "may_hide_more": may_hide, "not_found": []}]}


def message(sender, at="2026-10-01T09:00:00Z"):
    return {"kind": "follow_up", "at": at, "title": SECRET, "detail": SECRET,
            "sender": sender,
            "source": {"handle": MAIL, "label": "Email", "system": "Email", "open_url": "/s"}}


def card(items, **kw):
    return ps.build(items, labels={PO: "PO100"}, **{"trails": {}, "late_drafts": {}, **kw})


class EtaAnswer(unittest.TestCase):
    def test_states_map_and_unknown_fails_closed(self):
        src = {"handle": MAIL, "label": "Email", "system": "Email", "open_url": "/s"}
        cases = {
            ("confirmed", True): ("yes", "2026-11-20", True),
            ("confirmed", False): ("unknown", None, False),   # its record is missing
            ("no_confirmation_found", True): ("no", None, True),
            ("lead_only", True): ("possibly", None, True),
            ("promises_disagree", True): ("conflicting", None, True),
            ("withheld", False): ("hidden", None, False),
            ("not_determined", False): ("unknown", None, False),
            ("something_new", True): ("unknown", None, True),
        }
        for (state, has_source), want in cases.items():
            eta = {"state": state, "text": "Yes: the vendor confirmed 2026-11-20.",
                   "source": src if has_source else None}
            got = ps.eta_answer(trail(eta)["trails"][0])
            self.assertEqual((got["answer"], got["when"], got["in_mail"]), want, state)
        self.assertEqual(ps.eta_answer(None)["answer"], "unknown")


class Drafts(unittest.TestCase):
    def test_vendor_draft_uses_cited_facts_and_only_a_cleared_contact(self):
        draft = {"facts": [
            {"fact": "supplier", "value": "Acme Oils", "citation": H},
            {"fact": "order_number", "value": "PO100", "citation": H},
            {"fact": "line_item", "value": "Rose oil", "line_id": "L1", "citation": H + "#1"},
            {"fact": "ordered_quantity", "value": "5", "unit": "kg", "line_id": "L1",
             "citation": H + "#1"},
            {"fact": "received_quantity", "value": "0", "unit": "kg", "line_id": "L1",
             "citation": H + "#1"}]}
        eta = {"state": "no_confirmation", "text": "No.", "source": None}
        cleared = card([item(1)], trails={PO: trail(eta, [message("v@acme.example")],
                                                     contact="v@acme.example")},
                       late_drafts={PO: draft})["suggestions"][0]
        d = cleared["draft"]
        self.assertEqual((d["sent"], d["status"], d["audience"]),
                         (False, "draft_not_sent", "vendor"))
        self.assertEqual(d["to"]["address"], "v@acme.example")
        self.assertIn("Rose oil: ordered 5 kg, received 0 kg", d["body"])
        self.assertIn("2026-07-14", d["body"])          # the need date of the finding
        self.assertIn("message of 2026-10-01", d["body"])
        self.assertIn("We do not have a delivery confirmation from you on file.", d["body"])
        self.assertEqual(cleared["eta"]["answer"], "no")
        self.assertNotIn(SECRET, json.dumps(cleared))  # a message's words never appear
        self.assertTrue(all(f["citation"] for f in d["facts"]))
        # the same message with its sender not cleared (excluded or undecided): the draft
        # neither addresses nor mentions it
        hidden = card([item(1)], trails={PO: trail(eta, [message(None)], contact=None)},
                      late_drafts={PO: draft})["suggestions"][0]
        self.assertIsNone(hidden["draft"]["to"]["address"])
        self.assertNotIn("message of", hidden["draft"]["body"])
        self.assertNotIn(SECRET, json.dumps(hidden))
        self.assertIn("The vendor's contact address.", hidden["missing"])

    def test_unknown_eta_never_claims_there_is_no_confirmation(self):
        out = card([item(1)])["suggestions"][0]
        self.assertEqual(out["eta"]["answer"], "unknown")
        self.assertNotIn("confirmation from you", out["draft"]["body"])
        self.assertEqual(out["confidence"]["level"], "low")   # no trail was read

    def test_confirmed_date_passed_is_the_subject(self):
        eta = {"state": "confirmed", "text": "Yes: the vendor confirmed 2026-09-01.",
               "source": {"handle": MAIL, "label": "Email", "system": "Email",
                          "open_url": "/s"}, "promise": {"promised_date": "2026-09-01"}}
        out = card([item(1)], trails={PO: trail(eta)})["suggestions"][0]
        self.assertEqual((out["eta"]["answer"], out["eta"]["when"], out["eta"]["in_mail"]),
                         ("yes", "2026-09-01", True))
        self.assertIn("confirmed delivery date 2026-09-01 has passed", out["draft"]["subject"])
        self.assertEqual(out["confidence"]["level"], "high")

    def test_customer_update_names_no_supplier_or_price(self):
        so = "SalesOrder:t:9"
        risk = item(1, "customer_order_at_risk", oid=so, days=59,
                    summary="59 days at risk from 1 late purchase order",
                    why="... against the required date 2026-09-04.",
                    links={"pos": [PO], "required": "2026-09-04"})
        impact = item(2, "customer_order_at_risk", days=59, summary="affects 1 sales order",
                      links={"orders": [so]},
                      facts={"amount": {"value": "50000.00", "currency": "CAD",
                                        "basis": "total_cost"}, "orders_affected": 1})
        out = ps.build([risk, impact], labels={PO: "PO100", so: "SO-77"}, trails={},
                       late_drafts={PO: {"facts": [
                           {"fact": "supplier", "value": "Acme Oils", "citation": H}]}})
        cards = {c["kind"]: c for c in out["suggestions"]}
        cust = cards["customer_update"]
        self.assertIn("2026-09-04", cust["draft"]["body"])
        self.assertEqual(cust["draft"]["audience"], "customer")
        for leak in ("Acme", "PO100", "50000", "CAD"):
            self.assertNotIn(leak, cust["draft"]["body"] + cust["draft"]["subject"])
        po = cards["vendor_chase"]
        self.assertEqual([o["object_id"] for o in po["impact"]["orders"]], [so])
        self.assertIn("holds up 1 customer order (worst 59 days at risk)", po["headline"])
        self.assertEqual(po["impact"]["amount"]["value"], "50000.00")


class Ranking(unittest.TestCase):
    def test_customer_impact_then_late_then_due_then_undated_whatever_the_money(self):
        items = [item(1, "po_undated", oid="PurchaseOrder:t:u", days=None,
                      facts={"amount": {"value": "900000", "currency": "CAD",
                                        "basis": "total_cost"}}),
                 item(2, "po_due", oid="PurchaseOrder:t:d", days=None),
                 item(3, "po_late", oid="PurchaseOrder:t:l"),
                 item(4, "customer_order_at_risk", oid="SalesOrder:t:s", days=3),
                 item(5, "po_late", oid="PurchaseOrder:t:x", acknowledged=True)]
        out = ps.build(items, labels={}, trails={}, late_drafts={})
        self.assertEqual([c["subject"]["object_id"].rsplit(":", 1)[1]
                          for c in out["suggestions"]], ["s", "l", "d", "u", "x"])
        customer = out["suggestions"][0]["draft"]["body"]
        self.assertNotIn("waiting", customer)   # nothing is said that no record states

    def test_lines_naming_no_item_are_listed_by_quantity(self):
        draft = {"facts": [{"fact": "ordered_quantity", "value": "5", "unit": "KG",
                            "line_id": "L1", "citation": H + "#1"},
                           {"fact": "received_quantity", "value": "0", "unit": "KG",
                            "line_id": "L1", "citation": H + "#1"}]}
        out = card([item(1)], late_drafts={PO: draft})["suggestions"][0]
        self.assertIn("- Line 1: ordered 5 KG, received 0 KG", out["draft"]["body"])
        self.assertEqual({f["fact"] for f in out["draft"]["facts"]},
                         {"ordered_quantity", "received_quantity", "need_date"})
        self.assertTrue(any("name no item" in m for m in out["missing"]))


class Cardinality(unittest.TestCase):
    def test_every_finding_is_a_card_or_counted_once_in_rank_order(self):
        for n in (1, 10, 60):
            items = []
            for i in range(n):
                oid = f"PurchaseOrder:t:{i}"
                items.append(item(2 * i + 1, oid=oid, days=n - i))
                items.append(item(2 * i + 2, "vendor_no_reply", oid=oid))   # merged
                items.append(item(1000 + i, "lot_on_hold", oid=f"Lot:t:{i}"))
            items.append(item(5000, "po_late", oid=f"PurchaseOrder:t:0", acknowledged=True))
            out = ps.build(items, labels={}, trails={}, late_drafts={}, top=n)
            self.assertEqual(out["total"], n)
            self.assertEqual(out["collapsed"], {"lot_on_hold": n})
            ids = [c["subject"]["object_id"] for c in out["suggestions"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(ids, [f"PurchaseOrder:t:{i}" for i in range(n)])
            self.assertEqual(out["suggestions"][0]["event_types"],
                             ["po_late", "vendor_no_reply"])
            self.assertTrue(all(c["draft"]["sent"] is False for c in out["suggestions"]))
        capped = ps.build(items, labels={}, trails={}, late_drafts={}, top=12)
        self.assertEqual((len(capped["suggestions"]), capped["more"]), (12, 48))


if __name__ == "__main__":
    unittest.main()
