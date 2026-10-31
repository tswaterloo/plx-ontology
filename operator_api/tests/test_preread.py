"""What the app reads before the agent's session (``operator_api.preread``): which record
an identifier names across systems (one record's copies resolve; different records sharing
a number are a choice, never a pick; a mapping record that only carries the number is not
the record), the did-you-mean those choices make, and the brief the agent is given from
the trail it must agree with. Synthetic ids; evidence label: isolated-fixture."""

from __future__ import annotations

import unittest

from operator_api import models as m
from operator_api import preread as pr

BC = 'PurchaseOrder:bc.purchase_orders:{"id":"b1"}'
PORTAL = 'PurchaseOrder:portal.app_purchase_order:{"id":"p1"}'
FM = 'PurchaseOrder:portal.fm_generated_purchase_order:{"id":7}'
MAP = 'BcEntityMapping:portal.app_bc_entity_map:{"id":"m1"}'
VALUES = {BC: {"vendor_name": "Synthetic Vendor A", "order_date": "2099-07-14",
               "po_number": "PO9"},
          PORTAL: {"fm_po_number": "PO77", "order_date": "2099-09-30", "po_number": "PO9",
                   "status": "Complete"}}


def call(name, args):
    if name == "plx.around":     # the Portal record's supplier, by its link
        return {"objects": [{"display": "Synthetic Vendor B"}]
                if args["from"] == PORTAL else []}
    assert name == "plx.object"
    return {"values": [{"property": k, "value": {"untrusted_text": v}}
                       for k, v in VALUES.get(args["object_id"], {}).items()]}


def found(*ids, kind="petra_po"):
    return {"roots": [], "unfound": [],
            "ambiguous": [{"mention": "PO9", "kind": kind, "candidates": [
                {"object_id": oid, "object_type": oid.split(":")[0]} for oid in ids]}]}


class Resolve(unittest.TestCase):
    def test_one_records_copies_resolve_and_different_records_are_a_choice(self):
        twins = {frozenset({PORTAL, FM})}
        same = lambda ids: frozenset(ids) in twins   # noqa: E731
        got, several = pr.resolve(call, found(PORTAL, FM, MAP), same=same)
        self.assertEqual(several, [])
        (root,) = got["roots"]
        self.assertEqual((root["object_id"], root["same_as"]), (PORTAL, [FM]))
        self.assertEqual(got["ambiguous"], [])
        # a BC record and a Portal record that only share the number: two records; the
        # mapping record carrying the number is not a third
        got, several = pr.resolve(call, found(BC, PORTAL, MAP), same=same)
        self.assertEqual(got["roots"], [])
        self.assertEqual(several, [{"mention": "PO9", "groups": [[BC], [PORTAL]]}])
        # the record the request is about (a pick) settles it
        got, several = pr.resolve(call, found(BC, PORTAL, MAP), PORTAL, same=same)
        self.assertEqual((several, got["roots"][0]["object_id"]), ([], PORTAL))
        # a record that settles none of them leaves the number to be asked back
        got, several = pr.resolve(call, found(BC, PORTAL, MAP), "Customer:x:1", same=same)
        self.assertEqual(several, [{"mention": "PO9", "groups": [[BC], [PORTAL]]}])
        # the mapping alone is kept when nothing of the kind's own type matched
        got, several = pr.resolve(call, found(MAP), same=same)
        self.assertEqual(got["roots"][0]["object_id"], MAP)

    def test_the_choice_names_each_record_by_its_system_and_own_values(self):
        answer = pr.choices_answer(
            call, m.AskRequest(question="why don't the invoice and PO match on PO9?"),
            [{"mention": "PO9", "groups": [[BC], [PORTAL, FM]]}], conversation="c",
            freshness=m.Freshness(), object_url=lambda kind, oid: f"/object/{kind}",
            latency_ms=1, answer_id="a")
        self.assertEqual(answer.direct_answer, "PO9 names 2 different records that share "
                                               "the number. Which did you mean?")
        self.assertEqual([c.label for c in answer.did_you_mean], [
            "PO9 in Business Central (Synthetic Vendor A, order date 2099-07-14)",
            "PO9 in the Portal (Synthetic Vendor B, order date 2099-09-30, fm po number "
            "PO77)"])
        self.assertEqual([c.object_id for c in answer.did_you_mean], [BC, PORTAL])
        self.assertEqual(answer.facts, [])

    def test_the_brief_carries_the_trails_steps_with_their_sources(self):
        src = m.Source(system="Email", handle="graph-mail://box/AAA@g-1", label="x")
        po = m.PoTrail(subject=m.ObjectRef(object_id=BC, type="PurchaseOrder", label="9",
                                           open_url="/o"),
                       eta=m.EtaCheck(state="no_confirmation_found", text="No confirmation"),
                       steps=[m.TrailStep(kind="follow_up", at="2099-09-02T14:57:25",
                                          title="Email: RE: PO 77 is ready", source=src,
                                          check="lead")])
        text = pr.brief(m.EvidenceTrail(kind="purchase_order", title="t", trails=[po]),
                        [{"mention": "PO 77", "object_id": PORTAL, "same_as": [FM]}])
        self.assertIn(f'"PO 77" names the record with object id {PORTAL} (the same record '
                      f"as {FM}).", text)
        self.assertIn("- 2099-09-02T14:57:25: Email: RE: PO 77 is ready [source "
                      "graph-mail://box/AAA@g-1]", text)
        self.assertEqual(pr.brief(None, []), "")


if __name__ == "__main__":
    unittest.main()
