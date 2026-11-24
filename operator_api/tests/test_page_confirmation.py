"""A PO page's status and supplier line follow the one confirmation rule
(``operating.order_confirmation``): the process counts settled links only, so a
confirmation linked as a lead, or on a reused number, is never said to be missing
(ALL-LIMITS-AUDIT-5 #3: PO100523's page said "no supplier confirmation" beside three
lead-linked confirmations)."""
import unittest

from operating import order_confirmation as oc
from operating import po_status
from operator_api import presentation
from operator_api.workspace import object_workspace as ow


def _stage(sid: str, missing: bool, kind: str = "not_found_yet") -> dict:
    stage = {"stage": sid, "owner_role": "purchasing", "citations": [],
             "reading": {"kind": "found" if not missing else kind, "text": "x"}}
    if missing:
        stage["missing"] = {"state": "unknown"}
    return stage


CARD = {"object": {"object_id": "PurchaseOrder:portal.app_purchase_order:{\"id\":\"p1\"}",
                   "object_type": "PurchaseOrder", "display": "PO1"}}
AROUND = {"hops": [{"link_type": "PurchaseOrder.ordered_from", "direction": "forward",
                    "result": {"objects": [{"object_id": "Supplier:x:1", "display": "Acme"}],
                               "edges": [{"to": "Supplier:x:1", "tier": "verified"}]}}]}


def _process(*stages):
    return {"status": "read", "result": {"process": "po_delivery_window", "stages": list(stages)}}


class ConfirmationByTheOneRule(unittest.TestCase):
    def answers(self, told, *stages):
        asked = []

        def confirmations():
            asked.append(1)
            return told
        out = ow.object_answers(CARD, AROUND, _process(*stages), confirmations=confirmations)
        return out, asked

    def test_a_lead_confirmation_is_found_unverified_never_missing(self):
        out, _ = self.answers(po_status.FOUND_UNVERIFIED, _stage("purchase_order", False),
                              _stage("supplier_confirmation", True),
                              _stage("receipt", True))
        self.assertEqual(out["missing"]["state"], "missing")   # the receipt still is
        text = presentation.status_words(out["missing"]["text"])
        self.assertNotIn("No supplier confirmation", text)
        self.assertIn("No receipt on record yet.", text)
        self.assertIn("supplier confirmation was found but is unverified", text)
        self.assertEqual(out["supplier"]["text"], "Acme; supplier confirmation found, unverified")
        # nobody is asked to find what is already found: the follow-up is the verify one
        self.assertEqual([f["missing"] for f in out["missing"]["follow_ups"]], ["Receipt of Purchase order PO1"])

    def test_a_reused_number_needs_a_check(self):
        out, _ = self.answers(po_status.NEEDS_CHECK, _stage("supplier_confirmation", True))
        self.assertEqual(out["missing"]["state"], "not_established")
        self.assertEqual(presentation.status_words(out["missing"]["text"]),
                         "A supplier confirmation needs a check: it names this order's number, "
                         "but another order holds that number too.")
        self.assertEqual(out["supplier"]["text"], "Acme; a supplier confirmation needs a check")

    def test_a_settled_link_on_a_copy_confirms(self):
        out, _ = self.answers(po_status.CONFIRMED, _stage("supplier_confirmation", True))
        self.assertEqual(out["missing"]["state"], "complete")
        self.assertEqual(out["supplier"]["text"], "Acme; confirmed")
        self.assertEqual(out["supplier"]["follow_ups"], [])

    def test_none_or_unreadable_leaves_the_process_verdict(self):
        for told in (po_status.NONE_FOUND, None, "an unknown state"):
            out, _ = self.answers(told, _stage("supplier_confirmation", True))
            self.assertEqual(out["missing"]["text"], "Missing: supplier confirmation")
            self.assertEqual(out["supplier"]["text"], "Acme; no supplier confirmation found yet")

    def test_the_rule_is_read_only_when_the_process_finds_none(self):
        out, asked = self.answers(po_status.FOUND_UNVERIFIED, _stage("supplier_confirmation", False))
        self.assertEqual(asked, [])
        self.assertEqual(out["supplier"]["text"], "Acme; confirmed")


class OneReader(unittest.TestCase):
    """``order_confirmation`` over a tool stub: a lead on a number another order holds
    counts for nothing; a listing not complete says nothing."""

    def source(self, links, colliding):
        class Stub(po_status.ToolSource):
            def twins(self, oid):
                return [oid], False, []

            def date_twins(self, oid, members):
                return [], []

            def confirm_links(self, members):
                return links

            def colliding(self, members):
                return colliding
        return Stub

    def read(self, links, colliding):
        real = po_status.ToolSource
        po_status.ToolSource = self.source(links, colliding)
        try:
            return oc.order_confirmation(lambda *_: {}, "PurchaseOrder:a")
        finally:
            po_status.ToolSource = real

    def test_states(self):
        lead = [{"tier": "lead", "to_object": "PurchaseOrder:a", "from_object": "SC:1"}]
        settled = [{"tier": "verified", "to_object": "PurchaseOrder:a", "from_object": "SC:1"}]
        self.assertEqual(self.read(lead, False)["state"], po_status.FOUND_UNVERIFIED)
        self.assertEqual(self.read(lead, True)["state"], po_status.NEEDS_CHECK)
        self.assertEqual(self.read(settled, True)["state"], po_status.CONFIRMED)
        self.assertEqual(self.read([], False)["state"], po_status.NONE_FOUND)
        unread = self.read(None, False)
        self.assertIsNone(unread["state"])
        self.assertIsNone(unread["links"])


if __name__ == "__main__":
    unittest.main()
