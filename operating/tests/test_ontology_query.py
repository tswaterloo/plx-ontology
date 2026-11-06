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

    def test_per_code_lists_each_codes_reached_records_with_their_cited_values(self):
        """A sheet's codes answered one by one: each code's records walked, the lines
        reached listed through the host's value reads (rows), the summary's values cited
        so a claim on one binds as evidence; a code no record holds is named."""
        from operating import answer_gate
        table, listed = Table(), []
        line = {"l1_handle": "api://public.app_purchase_order_line/1@g",
                "field_path": "row.unit_price", "digest": "b" * 64,
                "lineage": "gold_etl_from_fm"}
        price = {"property": "unit_price", "value_type": "money", "value": "0.541",
                 "currency": "CAD", "citation": line, "markings": []}

        def rows(arguments, actor):
            listed.append(dict(arguments))
            return {"count": 1, "rows": [], "summary": {"unit_price": {
                "with_value": 1, "last": {"value": price}}}, "gaps": []}
        tool = oq.QueryTool(oq.TableBackend(
            table, lambda actor, ids: "set:codes0001", Schema(), None,
            lambda actor, codes, object_type: (([SAGE] if codes != ["ZZ-9"] else []), []),
            rows))
        out = tool.call({"from": {"codes": ["AB-1", "ZZ-9"]}, "per_code": True,
                         "traverse": [{"link_type": "SalesInvoiceLine.for_item",
                                       "direction": "reverse"}],
                         "rows": {"properties": ["unit_price"], "date": {
                             "property": "order_date", "path": [{
                                 "link_type": "SalesInvoiceLine.line_of",
                                 "direction": "forward"}]}}}, "actor")
        self.assertNotIn("error", out)
        self.assertEqual([(e["code"], e["records"]) for e in out["by_code"]],
                         [("AB-1", 1), ("ZZ-9", 0)])
        self.assertEqual(out["by_code"][0]["rows"]["summary"]["unit_price"]["last"]["value"],
                         price)
        self.assertEqual(listed, [{"set": "set:lines0001", "properties": ["unit_price"],
                                   "limit": 0, "date": {"property": "order_date", "path": [
                                       {"link_type": "SalesInvoiceLine.line_of",
                                        "direction": "forward"}]}}])
        self.assertIn("codes_not_found", [g["reason"] for g in out["gaps"]])
        self.assertEqual(answer_gate.evidence_handles("plx.query", out),
                         {line["l1_handle"]})
        # a walk the schema refuses is refused once for the whole list
        def refusing(name, arguments, actor):
            if name == "plx.around" and "link" in arguments["path"][0]:
                return {"error": {"code": "undeclared_link", "message": "no link"}}
            return table(name, arguments, actor)
        tool.backend.call = refusing
        bad = tool.call({"from": {"codes": ["AB-1", "AB-2"]}, "per_code": True,
                         "traverse": [{"link": "purchased_on"}],
                         "rows": {"properties": ["unit_price"]}}, "actor")
        self.assertEqual(bad["error"]["code"], "undeclared_link")

    def test_a_summary_names_the_newest_value_and_never_compares_two_currencies(self):
        from operating.ontology_tools_aggregate import _row_summary

        def row(n, value, date, currency="CAD"):
            cite = {"l1_handle": f"api://x/{n}@g", "field_path": "row.p", "digest": "c" * 64,
                    "lineage": "l"}
            return {"object_id": f"L:{n}", "date": {"value": date},
                    "values": {"p": {"value": value, "currency": currency, "citation": cite}}}
        rows = [row(1, "0.60", "2026-09-30"), row(2, "0.50", "2026-05-01"),
                row(3, "0.70", "2026-01-01")]
        summary = _row_summary(rows, "p")
        self.assertEqual((summary["with_value"], summary["last"]["value"]["value"],
                          summary["last"]["date"]["value"], summary["lowest"]["value"],
                          summary["highest"]["value"]), (3, "0.60", "2026-09-30", "0.50",
                                                         "0.70"))
        mixed = _row_summary(rows + [row(4, "9", "2025-01-01", "USD")], "p")
        self.assertNotIn("lowest", mixed)
        self.assertEqual(mixed["mixed"], ["CAD", "USD"])

    def test_a_window_never_adds_groups_of_different_units(self):
        groups = [{"key": {"d": {"untrusted_text": "2026-01-02"}}, "value": "5", "unit": "EA"},
                  {"key": {"d": {"untrusted_text": "2026-01-03"}}, "value": "2", "unit": "KG"}]
        window = oq._window(groups, {"property": "d", "from": "2026-01-01"}, "sum")
        self.assertIsNone(window["value"])
        self.assertEqual(window["mixed"], ["EA", "KG"])

    def test_a_start_is_every_record_its_where_selects_and_a_where_never_goes_unread(self):
        """from an object_type with where: every record of the type the filters select
        (the host's select, scanned to the type's end), walked as one set; a where with
        nothing to walk filters the start, never dropped; with no where an object_type
        alone is refused, saying what to add."""
        table = Table()
        asked, made = [], []

        def select(arguments, actor):
            asked.append(dict(arguments))
            if "object_type" in arguments:
                return {"ids": [SAGE], "gaps": []}
            return {"ids": [], "gaps": []}

        def new_set(actor, ids):
            made.append(list(ids))
            return "set:picked01"
        tool = oq.QueryTool(oq.TableBackend(table, new_set, Schema(), lambda a, ids: set(ids),
                                            select=select))
        status = [{"property": "qc_status", "op": "eq", "value": "Quarantine"}]
        out = tool.call({"from": {"object_type": "Product", "where": status},
                         "traverse": [{"link_type": "SalesInvoiceLine.for_item",
                                       "direction": "reverse"}]}, "actor")
        self.assertNotIn("error", out)
        self.assertEqual(asked[0], {"object_type": "Product", "filters": status})
        self.assertEqual(out["set"], "set:lines0001")
        # a top-level where with no traverse filters the start: here it keeps nothing
        dated = [{"property": "order_date", "op": "gte", "value": "2026-01-01"}]
        kept = tool.call({"from": {"object_ids": [SAGE, FM]}, "where": dated}, "actor")
        self.assertEqual(asked[1], {"set": "set:picked01", "filters": dated})
        self.assertIsNone(kept["set"])
        self.assertIn("nothing_selected", [g["reason"] for g in kept["gaps"]])
        alone = tool.call({"from": {"object_type": "Product"}}, "actor")
        self.assertEqual(alone["error"]["code"], "invalid_input")
        self.assertIn("where", alone["error"]["message"])

    def test_a_bad_request_is_refused_by_the_contract(self):
        tool = oq.QueryTool(oq.TableBackend(Table(), None, Schema()))
        self.assertEqual(tool.call({"from": {}}, "actor")["error"]["code"], "invalid_input")


if __name__ == "__main__":
    unittest.main()
