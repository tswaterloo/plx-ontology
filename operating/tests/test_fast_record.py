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


if __name__ == "__main__":
    unittest.main()
