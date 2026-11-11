"""``record_status``: a record named only by its own identifier ("status of OR1000154182",
the line Assist asks for each record a typed line names) is resolved exactly and its card
said as cited claims, as of the moment the reads are pinned to; anything unsure declines."""
from __future__ import annotations

import unittest

from operating import fast_answer
from operating.tests.test_fast_answer import StubReads, _cell

SO = 'SalesOrder:portal.app_sales_order:{"id":"1"}'
SO_SAGE = 'SalesOrder:portal.sage_oe_order_header:{"orduniq":"7"}'
H = "api://public.app_sales_order/1@g-1"
H_SAGE = "api://sage.oe_order_header/7@g-1"
SENT = "2026-10-19T14:00:00Z"


def _candidate(oid, handle, otype="SalesOrder", number="OR1000154182"):
    return {"object": {"object_id": oid, "object_type": otype,
                       "display": _cell(number, handle, "row.order_number")},
            "matched": {"identifier_kind": "so_number", "normalized": number,
                        "raw": _cell(number, handle, "row.order_number"),
                        "property": "order_number"},
            "reason": "exact_identifier", "tier": "verified"}


def _value(prop, value_type, value, handle=H, **extra):
    return {"property": prop, "value_type": value_type, "value": value,
            "citation": {"l1_handle": handle, "field_path": f"row.{prop}"},
            "authority_rank": 0, "markings": [], **extra}


CARD = [_value("order_number", "identifier", "OR1000154182"),
        _value("status", "text", _cell("READY_TO_SHIP", H, "row.status")),
        _value("order_date", "date", "2026-10-19"),
        _value("total_amount", "money", "296.00", currency="CAD"),
        _value("subtotal", "money", "250.00"),                  # no currency: never said
        _value("sage_ord_uniq", "identifier", "8025858"),       # an internal key
        _value("portal_updated_at", "text", _cell("2026-11-01", H, "row.updated_at")),
        _value("notes", "text", _cell("call Rita first", H, "row.notes"))]  # free text


def _answers(*, then=True, history=(), cursor=None, candidates=None, card=CARD,
             withheld=None):
    candidates = [_candidate(SO, H)] if candidates is None else candidates

    def answers(name, args):
        if name == "plx.find":
            return {"candidates": candidates if then or "as_known_at" not in args else []}
        if name == "plx.object" and args.get("history"):
            return {"history": list(history), "withheld": withheld,
                    "completeness": {"next_cursor": cursor}}
        if name == "plx.object":
            return {"object": {"object_id": args["object_id"]}, "values": list(card)}
        return None
    return answers


def _run(answers, token="OR1000154182", as_of=None):
    reads, draft = StubReads(answers), fast_answer.Draft()
    reads.as_of = as_of
    fast_answer._RECIPES["record_status"](reads, fast_answer.Ask("record_status", token, None),
                                          draft)
    return draft, reads


class RecordStatusTests(unittest.TestCase):
    def test_understood_only_as_one_bare_identifier_another_family_does_not_read(self):
        for question in ("status of OR1000154182", "What is the status of W1000005463?",
                         "status of PO8741-75", "details of MC3025-GR"):
            self.assertEqual(fast_answer.understand(question).intent, "record_status", question)
            self.assertTrue(fast_answer.serves(question), question)
        # a PO, a task or a bare number is its own family's; two records, a question about
        # something else, or more than the status is the agent's
        self.assertEqual(fast_answer.understand("status of PO5447").intent, "po_status")
        self.assertEqual(fast_answer.understand("status of TASK-12").intent, "task_status")
        self.assertEqual(fast_answer.understand("status of 154074").intent, "po_status")
        for question in ("status of OR1000154182 OR1000154183", "is OR1000154182 late?",
                         "status of OR1000154182 and who ships it", "status of the order"):
            self.assertFalse(fast_answer.serves(question), question)

    def test_the_card_is_said_as_cited_claims_and_nothing_else(self):
        draft, reads = _run(_answers())
        said = {v["name"]: v["value"] for v in draft.values}
        self.assertEqual(said, {"record": "OR1000154182", "status": "READY_TO_SHIP",
                                "order_date": "2026-10-19", "total_amount": "296"})
        money = next(v for v in draft.values if v["name"] == "total_amount")
        self.assertEqual(money["currency"], "CAD")
        self.assertTrue(all(v["citations"][0]["l1_handle"] == H for v in draft.values))
        self.assertEqual(draft.sentences, ["Sales order OR1000154182: status READY_TO_SHIP; "
                                           "order date 2026-10-19; total amount 296 CAD."])
        for hidden in ("250", "8025858", "Rita", "2026-11-01"):
            self.assertNotIn(hidden, draft.sentences[0])
        self.assertEqual(reads.as_of_reads, [])

    def test_unknown_or_unsure_records_decline(self):
        with self.assertRaises(fast_answer.Unanswerable):
            _run(_answers(candidates=[]))                       # nothing found
        lead = dict(_candidate(SO, H), tier="lead", reason="display_text")
        lead["matched"] = dict(lead["matched"], identifier_kind="display_text",
                               property="notes")
        with self.assertRaises(fast_answer.Unanswerable):
            _run(_answers(candidates=[lead]))                   # a text mention only
        with self.assertRaises(fast_answer.Unanswerable):
            _run(_answers(card=[CARD[0], CARD[5], CARD[7]]))    # nothing a message says
        # one identifier on two kinds of record: the reader's choice, never picked
        lot = _candidate('Lot:portal.app_lot:{"id":"9"}', "api://lot/9@g", "Lot")
        with self.assertRaises(fast_answer._Choice):
            _run(_answers(candidates=[_candidate(SO, H), lot]))

    def test_one_order_in_two_systems_is_said_apart(self):
        sage = _candidate(SO_SAGE, H_SAGE)

        def answers(name, args):
            if name == "plx.object":
                handle = H_SAGE if args["object_id"] == SO_SAGE else H
                return {"values": [_value("order_date", "date", "2026-10-19", handle),
                                   _value("total_amount", "money", "296.00", handle,
                                          currency="USD" if handle == H_SAGE else "CAD")]}
            return _answers(candidates=[_candidate(SO, H), sage])(name, args)
        draft, _reads = _run(answers)
        self.assertEqual(len(draft.sentences), 2)
        self.assertIn("in the Portal", draft.sentences[0])
        self.assertIn("296 CAD", draft.sentences[0])
        self.assertIn("in Sage", draft.sentences[1])
        self.assertIn("296 USD", draft.sentences[1])

    def test_as_of_the_send_moment(self):
        # known then: the find and the card are read as known at that moment
        _draft, reads = _run(_answers(), as_of=SENT)
        self.assertEqual(reads.as_of_reads, ["then"])
        self.assertTrue(all(a.get("as_known_at") == SENT for n, a in reads.calls))
        # captured later, its status changed after the moment: the status is left out
        history = [{"at": "2026-10-25T00:00:00Z", "kind": "changed",
                    "changes": [{"property": "status"}]},
                   {"at": "2026-10-01T00:00:00Z", "kind": "first_recorded", "changes": []}]
        draft, reads = _run(_answers(then=False, history=history), as_of=SENT)
        self.assertNotIn("status", {v["name"] for v in draft.values})
        self.assertIn("order_date", {v["name"] for v in draft.values})
        self.assertEqual(reads.as_of_reads, ["now_unchanged"])
        asked = next(a for n, a in reads.calls if a.get("history"))
        self.assertEqual(sorted(asked["properties"]),
                         ["order_date", "status", "total_amount"])    # no link read
        # first captured after the moment: what the status was then is unknown
        draft, reads = _run(_answers(then=False, history=[
            {"at": "2026-11-05T00:00:00Z", "kind": "first_recorded", "changes": []}]),
            as_of=SENT)
        self.assertEqual({v["name"] for v in draft.values},
                         {"record", "order_date", "total_amount"})
        self.assertEqual(reads.as_of_reads, ["now_captured_after"])
        # a history withheld in part, read only in part, or a record removed since: nothing
        for kwargs in ({"withheld": {"properties": 1}},
                       {"history": [{"at": "2026-11-05T00:00:00Z", "kind": "changed",
                                     "changes": []}], "cursor": "more"},
                       {"history": [{"at": "2026-11-01T00:00:00Z",
                                     "kind": "removed_from_source"}]}):
            with self.assertRaises(fast_answer.Unanswerable, msg=str(kwargs)):
                _run(_answers(then=False, **kwargs), as_of=SENT)

    def test_a_record_not_held_as_written_is_found_in_its_other_forms(self):
        ao = 'AssemblyOrder:portal.fm_assembly_order:{"id":1}'
        cpo = 'CustomerPurchaseOrder:portal.sage_oe_order_header.ponumber:{"orduniq":"7"}'
        item = 'Product:bc.items:{"id":"8739"}'
        held = {"AO-26278A": [_candidate(ao, H, "AssemblyOrder", "AO-26278A")],
                "8739": [_candidate(item, H, "Product", "8739"),
                         _candidate(cpo, H_SAGE, "CustomerPurchaseOrder", "8739")]}

        def answers(name, args):
            if name == "plx.find":
                return {"candidates": held.get(args["query"], [])}
            return _answers()(name, args)
        draft, reads = _run(answers, token="AO26278A")
        self.assertEqual([a["query"] for n, a in reads.calls if n == "plx.find"],
                         ["AO26278A", "AO-26278A"])
        self.assertTrue(draft.sentences[0].startswith("Assembly order AO-26278A:"))
        # the number alone is read only as a record its document word names: the
        # customer's PO, never the product coded the same number
        draft, _reads = _run(answers, token="CPO8739")
        self.assertEqual(draft.roots[0]["object_type"], "CustomerPurchaseOrder")
        with self.assertRaises(fast_answer.Unanswerable):
            _run(answers, token="XY8739")        # a word no type's initials end with
        with self.assertRaises(fast_answer.Unanswerable):
            _run(answers, token="MC3025-GR")     # no other form: not found stays not found

    def test_one_product_in_many_systems_its_own_code_before_a_reference(self):
        bc = 'Product:bc.items:{"id":"1"}'
        own = 'Product:portal.app_product:{"id":"a"}'
        ref = 'Product:portal.app_product:{"id":"b"}'
        sage = 'Product:portal.sage_ic_item:{"itemno":"DVS-016"}'
        candidates = [_candidate(bc, H, "Product", "DVS-016"),
                      _candidate(own, H, "Product", "DVS-016"),
                      dict(_candidate(ref, H, "Product", "DVS-016"),
                           matched={"identifier_kind": "erp_item_code", "normalized": "DVS-016",
                                    "raw": _cell("DVS-016", H, "row.erp_code")}),
                      dict(_candidate(sage, H_SAGE, "Product", "DVS-016"),
                           matched={"identifier_kind": "erp_item_code", "normalized": "DVS-016",
                                    "raw": _cell("DVS-016", H_SAGE, "row.itemno")})]
        card = [_value("name", "text", _cell("Zogic Euca 4L", H, "row.name")),
                _value("description", "text", _cell("long free text", H, "row.description"))]
        draft, _reads = _run(_answers(candidates=candidates, card=card), token="DVS-016")
        self.assertEqual(len(draft.sentences), 3)    # BC, the Portal's own, Sage's own
        self.assertNotIn(ref, [r["object_id"] for r in draft.roots])
        self.assertIn("name Zogic Euca 4L", draft.sentences[0])
        self.assertNotIn("free text", " ".join(draft.sentences))
        # two records of one system are two records: the reader's choice, never picked
        with self.assertRaises(fast_answer._Choice):
            _run(_answers(candidates=candidates + [_candidate(
                'Product:bc.items:{"id":"2"}', H, "Product", "DVS-016")], card=card),
                token="DVS-016")

    def test_a_record_that_says_nothing_is_said_through_the_one_record_carrying_it(self):
        cpo = 'CustomerPurchaseOrder:portal.sage_oe_order_header.ponumber:{"orduniq":"7"}'
        link = {"link_type": "SalesOrder.references_customer_po", "direction": "reverse",
                "count": 1, "tiers": {"verified": 1}}

        def answers(links, objects):
            def run(name, args):
                if name == "plx.find":
                    return {"candidates": [_candidate(cpo, H_SAGE, "CustomerPurchaseOrder",
                                                      "8739")]}
                if name == "plx.object" and args["object_id"] == cpo:
                    return {"values": [_value("po_text", "identifier", "8739", H_SAGE)],
                            "linked_counts": links}
                if name == "plx.around":
                    return {"objects": objects}
                return _answers()(name, args)
            return run
        so = {"object_id": SO, "object_type": "SalesOrder",
              "display": _cell("OR1000154182", H, "row.order_number")}
        draft, reads = _run(answers([link], [so]), token="8739")
        self.assertEqual(len(draft.sentences), 1)
        self.assertTrue(draft.sentences[0].startswith(
            "Sales order OR1000154182 references customer purchase order 8739:"))
        self.assertIn("READY_TO_SHIP", draft.sentences[0])
        # a lead link, two links, or two carrying records: nothing is said
        for links, objects in (([dict(link, tiers={"lead": 1})], [so]),
                               ([link, dict(link, link_type="X.y")], [so]),
                               ([link], [so, dict(so, object_id=SO_SAGE)])):
            with self.assertRaises(fast_answer.Unanswerable):
                _run(answers(links, objects), token="8739")


    def test_a_card_quantity_is_never_said_a_name_is_as_held(self):
        card = [_value("ordered_pieces", "quantity", "5000", unit="each",
                       quantity_role="ordered"),
                _value("name", "text", _cell("Z101 Spray  4G/Cse", H, "row.desc"))]
        draft, _reads = _run(_answers(card=card))
        said = {v["name"]: v for v in draft.values}
        # its role contract is the source's (a planned yield, ordered pieces): the gate
        # refuses a card's quantity whose role the card cannot settle
        self.assertNotIn("ordered_pieces", said)
        self.assertEqual(said["name"]["value"], "Z101 Spray  4G/Cse")   # the cell's own text


if __name__ == "__main__":
    unittest.main()
