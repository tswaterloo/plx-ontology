"""plx.query (operating/ontology_query.py): from an entity's records, walk links hop by
hop, total what is reached by a linked record's date within a window, every group cited;
each system's record walked on its own. isolated-fixture (a scripted host table)."""

from __future__ import annotations

import unittest

from operating import ontology_query as oq

SAGE = 'Product:portal.sage_ic_item:{"itemno":"AB-1"}'
FM = 'Product:portal.fm_finished_product:{"id":7}'
CITE = [{"l1_handle": "api://sage.oe_invoice_detail/1", "field_path": "row.qtyshipped"}]


class Schema:
    link_types = {"SalesInvoiceLine.for_item": {"from": "SalesInvoiceLine", "to": "Product"},
                  "SalesInvoiceLine.line_of": {"from": "SalesInvoiceLine",
                                               "to": "SalesInvoice"}}


class Table:
    def __init__(self):
        self.calls = []

    def __call__(self, name, arguments, actor):
        self.calls.append((name, dict(arguments)))
        if name == "plx.find":
            return {"status": "resolved", "entities": [{
                "mention": "AB-1", "status": "match", "records": [
                    {"object": {"object_id": SAGE}, "status": "match"},
                    {"object": {"object_id": FM}, "status": "match"}]}]}
        if name == "plx.around":
            reached = 2 if arguments["from"] == SAGE else 0
            return {"set": "set:lines0001" if reached else "set:empty0001",
                    "hops": [{"link_type": "SalesInvoiceLine.for_item",
                              "direction": "reverse", "count": reached}]}
        if name == "plx.aggregate":
            return {"groups": [
                {"key": {"invoice_date": {"untrusted_text": "2026-08-31"}}, "value": "1200",
                 "inputs": 2, "unit": "EA", "inputs_citations": CITE},
                {"key": {"invoice_date": {"untrusted_text": "2026-10-13"}}, "value": "1200",
                 "inputs": 2, "unit": "EA", "inputs_citations": CITE}]}
        raise AssertionError(name)


class Query(unittest.TestCase):
    def test_an_entity_is_walked_per_record_and_totalled_in_its_window(self):
        table = Table()
        tool = oq.QueryTool(oq.TableBackend(table, lambda actor, ids: "set:new00001", Schema()))
        out = tool.call({
            "from": {"entity": "AB-1"},
            "traverse": [{"link_type": "SalesInvoiceLine.for_item", "direction": "reverse"}],
            "measures": [{"op": "sum", "property": "invoiced_quantity"}],
            "group_by": ["invoice_date"],
            "group_path": [{"link_type": "SalesInvoiceLine.line_of", "direction": "forward"}],
            "window": {"property": "invoice_date", "from": "2026-08-01", "to": "2026-10-06"}},
            "actor")
        self.assertNotIn("error", out)
        self.assertEqual(out["from"]["records"], [SAGE, FM])
        (walked,) = out["by_record"]          # the FM record reaches no invoice line
        self.assertEqual(walked["record"], SAGE)
        window = walked["results"][0]["window"]
        self.assertEqual((window["value"], window["groups_inside"], window["groups_outside"]),
                         ("1200", 1, 1))
        self.assertEqual(window["inputs_citations"], CITE)
        self.assertIn("nothing_reached", [g["reason"] for g in out["gaps"]])
        aggregate = next(args for name, args in table.calls if name == "plx.aggregate")
        self.assertEqual(aggregate["path"][0]["link_type"], "SalesInvoiceLine.line_of")

    def test_its_groups_are_evidence_the_answer_gate_binds_claims_to(self):
        """A total claimed as a sum over a group's inputs cites what this run returned:
        the session keeps plx.query's cited inputs as it keeps plx.aggregate's."""
        from operating import answer_gate, mcp_server
        self.assertIn("plx.query", mcp_server.RETURNING_TOOLS)
        result = {"from": {"records": []}, "hops": [], "set": "set:abcdefgh1", "results": [
            {"measure": {"op": "sum", "property": "q"}, "groups": [
                {"key": {}, "value": "5", "inputs": 1, "markings": [], "inputs_citations": [
                    {"l1_handle": "api://sage.oe_invoice_detail/1@g", "field_path": "row.q",
                     "digest": "a" * 64, "lineage": "sage_bronze"}]}]}]}
        self.assertEqual(answer_gate.evidence_handles("plx.query", result),
                         {"api://sage.oe_invoice_detail/1@g"})

    def test_a_hidden_record_is_answered_as_a_missing_one(self):
        """Start records the reader may not read are left out before anything is built
        from them; none readable is "no such record", whether hidden or absent."""
        table = Table()
        tool = oq.QueryTool(oq.TableBackend(table, lambda actor, ids: "set:new00001",
                                            Schema(), lambda actor, ids: {SAGE} & set(ids)))
        hidden = tool.call({"from": {"object_id": FM}}, "actor")
        missing = tool.call({"from": {"object_id": SAGE.replace("AB-1", "NONE")}}, "actor")
        self.assertEqual(hidden, missing)
        self.assertEqual(hidden["error"]["code"], "object_not_found")
        self.assertEqual(table.calls, [])
        out = tool.call({"from": {"object_ids": [SAGE, FM]},
                         "traverse": [{"link_type": "SalesInvoiceLine.for_item",
                                       "direction": "reverse"}]}, "actor")
        self.assertEqual(out["from"]["records"], [SAGE])

    def test_a_list_of_codes_starts_one_walk_and_names_the_codes_no_record_holds(self):
        table = Table()
        made = []

        def new_set(actor, ids):
            made.append(list(ids))
            return "set:codes0001"
        tool = oq.QueryTool(oq.TableBackend(
            table, new_set, Schema(), None,
            lambda actor, codes, object_type: ([SAGE, FM], ["ZZ-9"])))
        out = tool.call({"from": {"codes": ["AB-1", "AB-2", "ZZ-9"], "object_type": "Product"},
                         "traverse": [{"link_type": "SalesInvoiceLine.for_item",
                                       "direction": "reverse"}]}, "actor")
        self.assertEqual(made, [[SAGE, FM]])
        around = next(args for name, args in table.calls if name == "plx.around")
        self.assertEqual(around["from"], "set:codes0001")
        self.assertIn("codes_not_found", [g["reason"] for g in out["gaps"]])

    def test_a_window_never_adds_groups_of_different_units(self):
        groups = [{"key": {"d": {"untrusted_text": "2026-01-02"}}, "value": "5", "unit": "EA"},
                  {"key": {"d": {"untrusted_text": "2026-01-03"}}, "value": "2", "unit": "KG"}]
        window = oq._window(groups, {"property": "d", "from": "2026-01-01"}, "sum")
        self.assertIsNone(window["value"])
        self.assertEqual(window["mixed"], ["EA", "KG"])

    def test_a_bad_request_is_refused_by_the_contract(self):
        tool = oq.QueryTool(oq.TableBackend(Table(), None, Schema()))
        self.assertEqual(tool.call({"from": {}}, "actor")["error"]["code"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
