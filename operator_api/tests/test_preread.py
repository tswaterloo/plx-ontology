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

    def test_a_party_named_in_the_question_picks_its_record_or_is_said_to_match_none(self):
        # "synthetic po 9" was asked back as a choice whatever supplier it named
        never = lambda ids: False   # noqa: E731

        def with_find(suppliers):
            def tools(name, args):
                if name == "plx.find":
                    return {"candidates": [{"object": {"display": n}} for n in suppliers
                                           if args["query"] in n.casefold()]}
                if name == "plx.around":   # the Portal record's supplier, by its link
                    return {"objects": [{"display": "Quillon Labs Inc"}]
                            if args["from"] == PORTAL else []}
                if name == "plx.object" and args.get("properties") == ["vendor_name",
                                                                        "customer_name"]:
                    return {"values": [{"property": "vendor_name", "value": {
                        "untrusted_text": VALUES[args["object_id"]]["vendor_name"]}}]
                        if "vendor_name" in VALUES.get(args["object_id"], {}) else []}
                return call(name, args)
            return tools

        tools = with_find(["Synthetic Vendor A", "Zephyrine Oils Ltd"])
        got, several = pr.resolve(tools, found(BC, PORTAL), same=never,
                                  question="Synthetic po 9 - did they ever confirm?")
        self.assertEqual(several, [])
        (root,) = got["roots"]
        self.assertEqual((root["object_id"], root["named_party"], root["others"]),
                         (BC, "Synthetic Vendor A", [PORTAL]))
        self.assertIn("different records", pr.brief(None, [{**root, "found_by": "identifier"}]))
        # the Portal record's supplier by its link (no vendor name of its own)
        got, several = pr.resolve(tools, found(BC, PORTAL), same=never,
                                  question="quillon PO9 status")
        self.assertEqual((several, got["roots"][0]["object_id"]), ([], PORTAL))
        # a supplier none of them has is said, beside the choice; never a pick
        got, several = pr.resolve(tools, found(BC, PORTAL), same=never,
                                  question="zephyrine po 9 status")
        self.assertEqual(several, [{"mention": "PO9", "groups": [[BC], [PORTAL]],
                                    "party": "Zephyrine Oils Ltd"}])
        answer = pr.choices_answer(
            tools, m.AskRequest(question="zephyrine po 9 status"), several, conversation="c",
            freshness=m.Freshness(), object_url=lambda kind, oid: f"/object/{kind}",
            latency_ms=1, answer_id="a")
        self.assertEqual(answer.direct_answer, "PO9 names 2 different records that share the "
                         "number, and none of them is from Zephyrine Oils Ltd. Which did you "
                         "mean?")
        # no party word in the question: unchanged, a choice with no mismatch said
        got, several = pr.resolve(tools, found(BC, PORTAL), same=never,
                                  question="whats going on with po 9")
        self.assertEqual(several, [{"mention": "PO9", "groups": [[BC], [PORTAL]]}])

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
