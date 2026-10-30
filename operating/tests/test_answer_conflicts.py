"""Conflicts, duplicates, near matches and stale values in answers
(``operating.answer_conflicts``, RISHI-DIRECTION section 5), at the layer's boundary:
the contract conflict a reader is shown, the copies listed, the "did you mean" choices.
Fixtures only; nothing here reads a business system."""

from __future__ import annotations

import datetime as dt
import json
import unittest

from operating import answer_conflicts as ac
from operating import query_understanding as qu
from ontology.types import Actor
from operating.ontology_tools import ReadPolicy

NOW = dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc)
BC_PO = "bc://production/co-1/purchaseOrders/po-77@v1"
PORTAL_PO = "api://public.app_purchase_order/%7B%22id%22%3A77%7D@v3"
DOC = "original://co-abc123#page=2"
MAIL = "graph-mail://box-1/msg-9"
DIGEST = "0" * 64


def value(v, handle, *, rank=None, path="row.qty", **extra):
    return {"property": "received_quantity", "value_type": "decimal", "value": v,
            "citation": {"l1_handle": handle, "field_path": path, "digest": DIGEST,
                         "lineage": "bc"}, "authority_rank": rank, "markings": [], **extra}


def everything(handles):
    return set(handles)


def by_source(found):
    return {v["source"]: v for v in found["values"]}


class SameFactDifferentValues(unittest.TestCase):
    def test_declared_precedence_trusts_the_system_of_record_and_says_why(self):
        conflict = {"kind": "value_disagreement", "subject": "PurchaseOrder.received_quantity",
                    "values": [value("96", PORTAL_PO, rank=1), value("120", BC_PO, rank=0)]}
        [found] = ac.tool_conflicts([conflict], visible=everything, now=NOW)
        self.assertEqual(found["field"], "received quantity")
        self.assertEqual(found["rule"], "system_of_record")
        values = by_source(found)
        self.assertTrue(values[BC_PO]["trusted"])
        self.assertFalse(values[PORTAL_PO]["trusted"])
        self.assertIn("BC is the system of record", values[BC_PO]["why"])
        self.assertIn("Disagrees with BC", values[PORTAL_PO]["why"])
        self.assertEqual({v["value"] for v in found["values"]}, {"96", "120"})
        self.assertEqual(set(found["values"][0]) >= {"value", "source", "trusted", "why"},
                         True)

    def test_a_rule_that_says_who_governs_wins(self):
        # po_status: BC lines govern receipt state
        disagree = [{"field": "receipt_state", "governs": "bc", "sides": [
            {"system": "portal", "records": ["p"], "value": "fully_received",
             "values": [value("x", PORTAL_PO)]},
            {"system": "bc", "records": ["b"], "value": "open_lines_outstanding",
             "values": [value("y", BC_PO)]}]}]
        [found] = ac.systems_disagree_conflicts(disagree, visible=everything, now=NOW)
        values = by_source(found)
        self.assertTrue(values[BC_PO]["trusted"])
        self.assertEqual(values[BC_PO]["value"], "open_lines_outstanding")
        self.assertIn("governs", values[BC_PO]["why"])

    def test_without_a_rule_the_newest_reading_wins_and_its_age_is_stated(self):
        conflict = {"kind": "value_disagreement", "subject": "PurchaseOrder.expected_date",
                    "values": [value("2026-11-06", PORTAL_PO), value("2026-10-20", BC_PO)]}
        dated = {(PORTAL_PO, "row.qty"): "2026-10-27T09:00:00+00:00",
                 (BC_PO, "row.qty"): "2026-08-01T09:00:00+00:00"}
        [found] = ac.tool_conflicts([conflict], visible=everything, dated=dated, now=NOW)
        self.assertEqual(found["rule"], "newest")
        values = by_source(found)
        self.assertTrue(values[PORTAL_PO]["trusted"])
        self.assertIn("2 days ago", values[PORTAL_PO]["why"])
        self.assertIn("89 days ago", values[BC_PO]["why"])   # stale: its age is said
        self.assertEqual(values[BC_PO]["age"], "89 days ago")

    def test_readings_of_one_build_are_not_ordered_by_date(self):
        # one rebuild records both values minutes apart: no "newer" fact (measured live)
        conflict = {"kind": "value_disagreement", "subject": "PurchaseOrder.order_date",
                    "values": [value("2026-03-28", PORTAL_PO), value("2017-03-21", BC_PO)]}
        dated = {(PORTAL_PO, "row.qty"): "2026-10-28T22:45:48+00:00",
                 (BC_PO, "row.qty"): "2026-10-28T22:47:16+00:00"}
        [found] = ac.tool_conflicts([conflict], visible=everything, dated=dated, now=NOW)
        self.assertEqual(found["rule"], "undecided")

    def test_two_tables_of_one_system_are_named_apart(self):
        generated = "api://public.generated_purchase_order/%7B%22id%22%3A372%7D@v1"
        conflict = {"kind": "value_disagreement", "subject": "PurchaseOrder.order_date",
                    "values": [value("2026-03-28", PORTAL_PO, rank=1),
                               value("2017-03-21", generated, rank=0)]}
        [found] = ac.tool_conflicts([conflict], visible=everything, now=NOW)
        why = by_source(found)[PORTAL_PO]["why"]
        self.assertIn("the Portal generated purchase order record", why)

    def test_a_date_tie_falls_to_the_most_specific_document(self):
        readings = [ac.Reading("12.50", DOC, names_records=1, document=True),
                    ac.Reading("13.00", "sharepoint://site/price-list@v1", names_records=40,
                               document=True)]
        found = ac.resolve("unit price", readings, now=NOW)
        self.assertEqual(found["rule"], "most_specific")
        self.assertTrue(by_source(found)[DOC]["trusted"])
        self.assertIn("names 40 records", by_source(found)[DOC]["why"])

    def test_no_rule_no_pick_both_shown(self):
        conflict = {"kind": "value_disagreement", "subject": "Item.description",
                    "values": [value("Lavender soap", PORTAL_PO), value("Lavendar soap 2",
                                                                        BC_PO)]}
        [found] = ac.tool_conflicts([conflict], visible=everything, now=NOW)
        self.assertEqual(found["rule"], "undecided")
        self.assertFalse(any(v["trusted"] for v in found["values"]))
        self.assertTrue(all("No rule" in v["why"] for v in found["values"]))

    def test_a_document_never_outranks_the_record_it_attests(self):
        # an attested (lead) conflict: the document's rank is in its own precedence list
        conflict = {"kind": "value_disagreement", "subject": "PurchaseOrder.quantity",
                    "tier": "lead",
                    "values": [value("100", DOC, rank=0, path="fields.quantity.value"),
                               value("120", BC_PO, rank=1)]}
        [found] = ac.tool_conflicts([conflict], visible=everything, now=NOW)
        values = by_source(found)
        self.assertTrue(values[BC_PO]["trusted"])
        self.assertFalse(values[DOC]["trusted"])
        self.assertIn("machine-read", values[BC_PO]["why"])

    def test_a_side_the_reader_cannot_open_is_no_conflict_and_leaves_no_trace(self):
        conflict = {"kind": "value_disagreement", "subject": "PurchaseOrder.received_quantity",
                    "values": [value("96", PORTAL_PO, rank=1), value("120", BC_PO, rank=0)]}
        found = ac.tool_conflicts([conflict], visible=lambda hs: {h for h in hs
                                                                   if h != BC_PO}, now=NOW)
        self.assertEqual(found, [])
        # three sides, one hidden: the two visible decide among themselves only
        conflict["values"].append(value("90", MAIL))
        [found] = ac.tool_conflicts([conflict], visible=lambda hs: {h for h in hs
                                                                     if h != BC_PO}, now=NOW)
        text = json.dumps(found)
        self.assertNotIn(BC_PO, text)
        self.assertNotIn("120", text)
        self.assertNotIn("BC ", text)
        self.assertTrue(by_source(found)[PORTAL_PO]["trusted"])   # record over email

    def test_numbers_are_compared_as_decimals_and_blanks_are_not_sides(self):
        same = {"kind": "value_disagreement", "subject": "PurchaseOrder.received_quantity",
                "values": [value("96.00", PORTAL_PO), value("96", BC_PO),
                           value(None, MAIL)]}
        self.assertEqual(ac.tool_conflicts([same], visible=everything, now=NOW), [])
        self.assertEqual(ac.value_key("1,200.50"), ac.value_key("1200.5"))
        self.assertNotEqual(ac.value_key("0.1"), ac.value_key("0.10000001"))

    def test_unknown_conflict_kinds_and_malformed_values_fail_closed(self):
        odd = [{"kind": "identity_conflict", "subject": "x", "events": ["a", "b"]},
               {"kind": "value_disagreement", "subject": "x",
                "values": [{"value": "1"}, {"value": "2", "citation": {}}]},
               "not a conflict", None]
        self.assertEqual(ac.tool_conflicts(odd, visible=everything, now=NOW), [])

    def test_tool_resolution_indexes_the_trusted_values(self):
        conflict = {"kind": "value_disagreement", "subject": "PurchaseOrder.received_quantity",
                    "values": [value("96", PORTAL_PO, rank=1), value("120", BC_PO, rank=0)]}
        found = ac.resolution(conflict, dated={(BC_PO, "row.qty"): "2026-10-01T00:00:00Z"})
        self.assertEqual(found["rule"], "system_of_record")
        self.assertEqual(found["trusted"], [1])
        self.assertEqual(found["as_of"], [None, "2026-10-01T00:00:00+00:00"])
        # and a card built from the tool output keeps the tool's dates
        conflict["resolution"] = found
        [card] = ac.tool_conflicts([conflict], visible=everything, now=NOW)
        self.assertEqual(by_source(card)[BC_PO]["as_of"], "2026-10-01T00:00:00+00:00")


class DocumentContradictsItself(unittest.TestCase):
    def test_same_label_two_values_cites_both_places(self):
        parts = [("page 1", "Purchase order 4410\nDelivery date: 2026-11-06\nQty: 120"),
                 ("page 3", "Terms\nDelivery date: 2026-11-20\nQty: 120 ")]
        [flag] = ac.self_contradictions(DOC, parts, kind="original")
        self.assertEqual(flag["field"], "Delivery date")
        self.assertEqual([(v["place"], v["value"]) for v in flag["values"]],
                         [("page 1", "2026-11-06"), ("page 3", "2026-11-20")])
        self.assertFalse(any(v["trusted"] for v in flag["values"]))

    def test_quoted_replies_sheets_and_wordy_labels_are_not_contradictions(self):
        mail = [("message", "Ship date: 2026-11-06\nThanks\n\nOn Mon, Oct 20, 2026 Ana "
                            "wrote:\n> Ship date: 2026-10-30")]
        self.assertEqual(ac.self_contradictions(MAIL, mail, kind="mail"), [])
        rows = [("row 1", "Lot: L-1"), ("row 2", "Lot: L-2")]
        self.assertEqual(ac.self_contradictions(DOC, rows, kind="sheet_row"), [])
        words = [("p1", "Status: open\nStatus: closed")]   # no figure: not compared
        self.assertEqual(ac.self_contradictions(DOC, words, kind="original"), [])
        headers = [("p1", "Sent: 2026-10-01\nSent: 2026-10-02")]
        self.assertEqual(ac.self_contradictions(DOC, headers, kind="original"), [])
        # per-line or per-model figures repeat by design (measured on live spec sheets)
        specs = [("p1", "Weight: 1700 lb\nTotal: $2,408,000"),
                 ("p2", "Weight: 27 lb\nTotal: $4,520,000")]
        self.assertEqual(ac.self_contradictions(DOC, specs, kind="original"), [])
        # formatting is no difference; a run-together label is not a value
        same = [("p1", "Delivery date: 6 Nov 2026"), ("p2", "Delivery date: 6 Nov, 2026"),
                ("p3", "LOT#: AO-26098A"), ("p3", "LOT#: SIZE: 11000EA; 917CS")]
        self.assertEqual(ac.self_contradictions(DOC, same, kind="original"), [])


class TypeSpellings(unittest.TestCase):
    def test_a_type_filter_accepts_the_schema_and_the_entity_table_spelling(self):
        class Scope:
            withheld = None
            countable = {}

            def params(self, **extra):
                return extra
        index = qu.PgEntityIndex.__new__(qu.PgEntityIndex)
        params = index._params(Scope(), ["PurchaseOrder"], forms=["PO1"])
        self.assertEqual(params["types"], ["PurchaseOrder", "purchase_order"])
        self.assertIsNone(index._params(Scope(), None)["types"])


class Duplicates(unittest.TestCase):
    ITEMS = [
        {"handle": "graph-mail://ana/m1", "dup_group": "d:1", "authority": 2,
         "occurred_at": "2026-10-20"},
        {"handle": "graph-mail://ben/m1", "dup_group": "d:1", "authority": 2,
         "occurred_at": "2026-10-20"},
        {"handle": "graph-mail://cy/m1-fwd", "dup_group": "d:1", "authority": 2,
         "occurred_at": "2026-10-21"},
        {"handle": "sharepoint://site/a.pdf", "dup_group": None, "authority": 3},
    ]

    def test_one_representative_and_only_readable_copies_listed(self):
        hidden = "graph-mail://ben/m1"
        out = ac.collapse_copies(self.ITEMS, may_open=lambda h: h != hidden,
                                 location=lambda m: "mailbox of " + m["handle"].split("/")[2])
        self.assertEqual([o["handle"] for o in out],
                         ["graph-mail://ana/m1", "sharepoint://site/a.pdf"])
        self.assertEqual(out[0]["also_in"], {"count": 1, "copies": [
            {"handle": "graph-mail://cy/m1-fwd", "location": "mailbox of cy"}]})
        self.assertNotIn("ben", json.dumps(out))

    def test_a_hidden_representative_gives_way_and_a_hidden_cluster_is_gone(self):
        out = ac.collapse_copies(self.ITEMS, may_open=lambda h: "ana" not in h)
        self.assertEqual(out[0]["handle"], "graph-mail://ben/m1")
        self.assertNotIn("ana", json.dumps(out))
        out = ac.collapse_copies(self.ITEMS, may_open=lambda h: h.startswith("sharepoint"))
        self.assertEqual([o["handle"] for o in out], ["sharepoint://site/a.pdf"])
        self.assertEqual(out[0]["also_in"]["count"], 0)

    def test_cardinality_nothing_lost_or_doubled(self):
        for n in (1, 10, 60):
            items = [{"handle": f"graph-mail://u{i}/m", "dup_group": f"d:{i % 7}"}
                     for i in range(n)] + [{"handle": f"original://doc-{i}", "dup_group":
                                            None} for i in range(n)]
            out = ac.collapse_copies(items, may_open=lambda h: True)
            listed = [o["handle"] for o in out] + [c["handle"] for o in out
                                                   for c in o["also_in"]["copies"]]
            self.assertEqual(len(out), min(n, 7) + n)
            self.assertEqual(len(listed), len(set(listed)))
            self.assertEqual(sum(1 + o["also_in"]["count"] for o in out), 2 * n)


class FakeIndex:
    """An entity index over a list of (object_id, type, name, alias, audience) rows."""

    def __init__(self, rows):
        self.rows = rows

    def scope(self, policy, actor):
        return {r[4] for r in self.rows if policy.may_read(actor, r[4])}

    def _row(self, r, matched):
        return qu.EntityRow(r[0], r[0], r[1], r[2], qu.fold(r[2]), tuple(r[3]), matched, 1.0)

    def by_identifier(self, forms, scope, types=None):
        hits = [r for r in self.rows if set(r[3]) & {f.upper() for f in forms}]
        return ([self._row(r, next(a for a in r[3] if a in {f.upper() for f in forms}))
                 for r in hits if r[4] in scope], {r[0] for r in hits if r[4] not in scope})

    def near_identifier(self, forms, scope, types=None):
        return [self._row(r, a) for r in self.rows if r[4] in scope for a in r[3]
                if any(qu.near_id(f, a) for f in forms)]

    def by_terms(self, terms, scope, types=None):
        return [], {}

    def by_object_ids(self, object_ids, scope):
        return []

    def distinctive_words(self, words, scope):
        return set()


class NearMatches(unittest.TestCase):
    def test_near_id_is_one_edit_with_the_same_letters(self):
        self.assertTrue(qu.near_id("PO10421", "PO10412"))      # swapped digits
        self.assertTrue(qu.near_id("PO10421", "PO1042"))       # one missing
        self.assertTrue(qu.near_id("PO10421", "PO10427"))      # one changed
        self.assertFalse(qu.near_id("PO10421", "SO10421"))     # another series
        self.assertFalse(qu.near_id("PO10421", "PO10421"))     # exact is not near
        self.assertFalse(qu.near_id("PO10421", "PO12401"))     # two edits
        self.assertFalse(qu.near_id("123", "124"))             # too short to guess

    def test_a_missing_id_offers_the_close_ones_never_resolves_and_hides_hidden(self):
        policy = ReadPolicy("t1", {"bc:purchaseOrders": frozenset({"buyer"}),
                                   "bc:secret": frozenset({"controller"})}, {})
        actor = Actor("t1", frozenset({"buyer"}))
        index = FakeIndex([
            ("PurchaseOrder:bc:10412", "PurchaseOrder", "PO 10412", ["PO10412", "10412"],
             "bc:purchaseOrders"),
            ("PurchaseOrder:bc:10427", "PurchaseOrder", "PO 10427", ["PO10427", "10427"],
             "bc:purchaseOrders"),
            ("PurchaseOrder:bc:10420", "PurchaseOrder", "PO 10420", ["PO10420", "10420"],
             "bc:secret")])
        out = qu.understand("where is PO 10421", actor=actor, policy=policy, index=index,
                            llm=False)
        [mention] = [m for m in out.entities if m.kind == "identifier"]
        self.assertIsNone(mention.resolved)
        self.assertTrue(mention.ambiguous)
        self.assertEqual(sorted(mention.choices),
                         ["PurchaseOrder:bc:10412", "PurchaseOrder:bc:10427"])
        self.assertIn("Did you mean", mention.prompt)
        self.assertNotIn("10420", json.dumps(out.to_dict()))
        [choices] = ac.did_you_mean(out)
        self.assertEqual({c["object_id"] for c in choices["choices"]}, set(mention.choices))
        # the exact id resolves only to itself: no near-miss offered
        exact = qu.understand("where is PO 10412", actor=actor, policy=policy, index=index,
                              llm=False)
        self.assertNotIn("PurchaseOrder:bc:10427", json.dumps(exact.to_dict()))


if __name__ == "__main__":
    unittest.main()
