"""plx.query (operating/ontology_query.py): from an entity's records, walk links hop by
hop, total what is reached by a linked record's date within a window, every group cited;
each system's record walked on its own. isolated-fixture (a scripted host table)."""

from __future__ import annotations

import copy
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
        # an object_type start without where starts from every record of the type
        alone = tool.call({"from": {"object_type": "Product"}}, "actor")
        self.assertNotIn("error", alone)
        self.assertEqual(asked[-1], {"object_type": "Product", "filters": []})

    def test_every_group_is_kept_and_a_result_too_large_is_cut_where_it_says(self):
        """No group is dropped by plx.query itself: plx.aggregate's page, summary,
        bucket, having and cursor pass through; an answer past the result bound is cut
        (rows, groups) to fit, each cut stated with what was left, never refused."""
        table, asked = Table(), []
        cite = {"l1_handle": "api://sage.oe_invoice_detail/1@g", "field_path": "row.q",
                "digest": "a" * 64, "lineage": "sage_bronze"}
        many = [{"key": {"name": {"untrusted_text": f"customer {n:04d}"}}, "value": "6",
                 "inputs": 6, "markings": [], "inputs_citations": [cite] * 6}
                for n in range(150)]

        def call(name, arguments, actor):
            if name == "plx.aggregate":
                asked.append(dict(arguments))
                return {"groups": copy.deepcopy(many), "partial": False, "summary": {
                    "groups": 400, "with_value": 400}, "completeness": {
                        "complete": False, "truncated": True, "returned": 150,
                        "total": 400, "next_cursor": "g250", "reason": "group_page"}}
            return table(name, arguments, actor)
        tool = oq.QueryTool(oq.TableBackend(call, lambda actor, ids: "set:new00001", Schema()))
        out = tool.call({"from": {"set": "set:lines0001"},
                         "measures": [{"op": "count_distinct", "property": "order_date"}],
                         "group_by": ["name"], "bucket": "month",
                         "having": {"op": "eq", "value": 6}, "cursor": "g100"}, "actor")
        self.assertNotIn("error", out)
        self.assertEqual({k: asked[0][k] for k in ("bucket", "having", "cursor")},
                         {"bucket": "month", "having": {"op": "eq", "value": 6},
                          "cursor": "g100"})
        [result] = out["results"]
        self.assertEqual(result["summary"]["groups"], 400)
        self.assertLessEqual(len(oq.json.dumps(out)), oq.RESULT_BYTES)
        # every group of the page kept, each with its first input citations
        self.assertEqual(len(result["groups"]), 150)
        self.assertIn(result["inputs_citations_kept"], (1, 0))
        self.assertLessEqual(len(result["groups"][0].get("inputs_citations", [])), 1)
        self.assertEqual(result["groups"][0]["key"], many[0]["key"])
        self.assertEqual(result["completeness"]["next_cursor"], "g250")
        # groups too large even without citations are cut, the cut said
        for group in many:
            group["key"]["name"]["untrusted_text"] = "x" * 400
        out = tool.call({"from": {"set": "set:lines0001"},
                         "measures": [{"op": "count_distinct", "property": "order_date"}],
                         "group_by": ["name"], "cursor": "g100"}, "actor")
        [result] = out["results"]
        shown = len(result["groups"])
        self.assertTrue(0 < shown < 150)
        self.assertEqual(result["inputs_citations_kept"], 0)
        self.assertEqual(result["completeness"]["reason"], "result_size")
        self.assertEqual(result["completeness"]["next_cursor"], f"g{100 + shown}")
        # groups already missing inputs keep saying so beside the size cut
        partial = {"results": [{"groups": copy.deepcopy(many), "partial": True,
                                "completeness": {"complete": False, "truncated": False,
                                                 "returned": 150, "total": 150,
                                                 "reason": "partial_inputs"}}]}
        cut = oq._fitted(partial)["results"][0]["completeness"]
        self.assertEqual((cut["reason"], cut["truncated"]),
                         ("partial_inputs_and_result_size", True))
        # rows past the bound are cut, the cut counted, never result_too_large
        value = {"property": "p", "value_type": "text", "value": "x" * 900,
                 "citation": cite, "markings": []}

        def rows(arguments, actor):
            return {"count": 20, "rows": [{"object_id": f"L:{n}", "values": {
                p: value for p in ("a", "b", "c", "d")}} for n in range(20)],
                "summary": {}, "gaps": []}
        tool = oq.QueryTool(oq.TableBackend(call, lambda actor, ids: "set:new00001",
                                            Schema(), rows=rows))
        out = tool.call({"from": {"set": "set:lines0001"},
                         "rows": {"properties": ["a", "b", "c", "d"], "limit": 20}}, "actor")
        self.assertLessEqual(len(oq.json.dumps(out)), oq.RESULT_BYTES)
        self.assertEqual(len(out["rows"]["rows"]) + out["rows"]["more_rows"], 20)

    def test_a_hop_to_a_type_walks_each_declared_route_and_takes_the_first_reaching(self):
        """Quarantined lots to their suppliers: the lots' receipts name no supplier
        (Receipt.supplied_by reaches nothing), their POs do. A hop {"to": "Supplier"}
        walks the declared routes, shortest first and one longer, counts each and takes
        the first that reaches a record; a link named by its type ("supplier") does the
        same; an explicit hop that reaches nothing names the other routes."""
        class Links:
            link_types = {
                "Lot.received_in": {"from": "Lot", "to": "Receipt",
                                    "name": "received_in", "reverse_name": "received_lots"},
                "Receipt.supplied_by": {"from": "Receipt", "to": "Supplier",
                                        "name": "supplied_by", "reverse_name": "receipts"},
                "Receipt.receives": {"from": "Receipt", "to": "PurchaseOrder",
                                     "name": "receives", "reverse_name": "receipts"},
                "PurchaseOrder.ordered_from": {"from": "PurchaseOrder", "to": "Supplier",
                                               "name": "ordered_from",
                                               "reverse_name": "purchase_orders"},
                "Email.mentions_supplier": {"from": "Email", "to": "Supplier"},
                "Lot.same_record_as": {"from": "Lot", "to": "Lot"}}
        reach = {"Lot.received_in": ("set:receipts", 7), "Receipt.supplied_by": (None, 0),
                 "Receipt.receives": ("set:orders01", 7),
                 "PurchaseOrder.ordered_from": ("set:supplier", 3)}
        walked = []

        def call(name, arguments, actor):
            if name == "plx.aggregate":   # the default count of what is reached
                return {"groups": [{"key": {}, "value": "3", "inputs": 0, "markings": []}]}
            hop = arguments["path"][0]
            walked.append((arguments["from"], hop.get("link_type") or hop.get("link")))
            handle, count = reach.get(hop["link_type"], (None, 0))
            return {"set": handle, "hops": [{"link_type": hop["link_type"],
                                             "direction": hop["direction"],
                                             "count": count}]}

        def select(arguments, actor):
            return {"ids": ["Lot:x:1", "Lot:x:2"], "gaps": []}
        tool = oq.QueryTool(oq.TableBackend(call, lambda actor, ids: "set:lots0001", Links(),
                                            lambda a, ids: set(ids), select=select))
        quarantined = {"object_type": "Lot", "where": [
            {"property": "qc_status", "op": "eq", "value": "Quarantine"}]}
        out = tool.call({"from": quarantined, "traverse": [{"to": "Supplier"}]}, "actor")
        self.assertNotIn("error", out)
        self.assertEqual(out["set"], "set:supplier")
        self.assertEqual([(r["path"][-1]["link_type"], r["count"], r.get("taken", False))
                          for r in out["routes"]],
                         [("Receipt.supplied_by", 0, False),
                          ("PurchaseOrder.ordered_from", 3, True)])
        self.assertEqual([h["link_type"] for h in out["hops"]],
                         ["Lot.received_in", "Receipt.receives", "PurchaseOrder.ordered_from"])
        self.assertNotIn("Email.mentions_supplier", {link for _f, link in walked})
        named = tool.call({"from": quarantined, "traverse": [{"link": "supplier"}]}, "actor")
        self.assertEqual(named["set"], "set:supplier")
        # an explicit hop that reaches nothing names the other routes to its type
        tried = tool.call({"from": quarantined, "traverse": [
            {"link_type": "Lot.received_in", "direction": "forward"},
            {"link_type": "Receipt.supplied_by", "direction": "forward"}]}, "actor")
        [hint] = [g for g in tried["gaps"] if g["reason"] == "other_routes"]
        self.assertIn("Receipt.receives forward / PurchaseOrder.ordered_from forward",
                      hint["detail"])
        self.assertIn('{"to": "Supplier"}', hint["detail"])
        none = tool.call({"from": quarantined, "traverse": [{"to": "Customer"}]}, "actor")
        self.assertEqual(none["error"]["code"], "undeclared_link")
        # a start its where selects nothing of walks nothing
        walked.clear()
        empty = oq.QueryTool(oq.TableBackend(call, lambda actor, ids: "set:lots0001",
                                             Links(), lambda a, ids: set(ids),
                                             select=lambda arguments, actor: {"ids": [],
                                                                              "gaps": []}))
        out = empty.call({"from": quarantined, "traverse": [{"to": "Supplier"}]}, "actor")
        self.assertEqual(walked, [])
        self.assertIn("nothing_selected", [g["reason"] for g in out["gaps"]])

    def test_routes_left_out_are_said_and_a_hop_to_its_own_type_keeps_where(self):
        """Two routes of one length reach suppliers: the one reaching more is taken and the
        other is named as left out, with its count and set. A hop to the type the walk is
        already at still selects by the call's where."""
        class Links:
            link_types = {
                "Lot.received_in": {"from": "Lot", "to": "Receipt"},
                "Receipt.supplied_by": {"from": "Receipt", "to": "Supplier"},
                "Receipt.carried_by": {"from": "Receipt", "to": "Supplier"}}
        reach = {"Lot.received_in": ("set:receipts", 7), "Receipt.supplied_by": ("set:sup00003", 3),
                 "Receipt.carried_by": ("set:car00001", 1)}

        def call(name, arguments, actor):
            if name == "plx.aggregate":
                return {"groups": [{"key": {}, "value": "3", "inputs": 0, "markings": []}]}
            hop = arguments["path"][0]
            handle, count = reach.get(hop["link_type"], (None, 0))
            return {"set": handle, "hops": [{"link_type": hop["link_type"],
                                             "direction": hop["direction"], "count": count}]}
        selected = []

        def select(arguments, actor):
            selected.append(arguments)
            return {"ids": ["Lot:x:1", "Lot:x:2"], "gaps": []}
        tool = oq.QueryTool(oq.TableBackend(call, lambda actor, ids: "set:lots0001", Links(),
                                            lambda a, ids: set(ids), select=select))
        out = tool.call({"from": {"object_id": "Lot:x:1"}, "traverse": [{"to": "Supplier"}]},
                        "actor")
        self.assertEqual(out["set"], "set:sup00003")
        [left] = [g for g in out["gaps"] if g["reason"] == "routes_not_taken"]
        self.assertIn("Receipt.carried_by forward (1 records, set:car00001)", left["detail"])
        where = [{"property": "qc_status", "op": "eq", "value": "Quarantine"}]
        out = tool.call({"from": {"object_id": "Lot:x:1"}, "traverse": [{"to": "Lot"}],
                         "where": where}, "actor")
        self.assertEqual(selected[-1]["filters"], where)
        self.assertEqual(out["set"], "set:lots0001")
        # a link named with a direction too is that link walked that way
        self.assertEqual(tool._directed("Receipt", {"link": "received_in",
                                                    "direction": "reverse"}),
                         {"link_type": "Lot.received_in", "direction": "reverse"})
        self.assertEqual(tool._directed("Supplier", {"link": "received_in",
                                                     "direction": "reverse"}),
                         {"link": "received_in"})

    def test_a_route_may_cross_to_a_records_copy_and_an_unroutable_walk_reads_nothing(self):
        """A lot whose own system names no item reaches it through its copy in another
        system (its identity link, once a route); a walk to a type only text mentions
        reach is refused, naming them, before its start is selected."""
        class Links:
            link_types = {
                "Lot.of_item": {"from": "Lot", "to": "Product"},
                "Lot.same_record_as": {"from": "Lot", "to": "Lot"},
                "Note.mentions_product": {"from": "Note", "to": "Product"}}

        def call(name, arguments, actor):
            if name == "plx.aggregate":
                return {"groups": [{"key": {}, "value": "3", "inputs": 0, "markings": []}]}
            hop = arguments["path"][0]
            handle, count = {("Lot.of_item", "Lot:fm:1"): (None, 0),
                             ("Lot.same_record_as", "Lot:fm:1"): ("set:twins001", 1),
                             ("Lot.of_item", "set:twins001"): ("set:prods001", 3)}.get(
                (hop["link_type"], arguments["from"]), (None, 0))
            return {"set": handle, "hops": [{"link_type": hop["link_type"],
                                             "direction": hop["direction"], "count": count}]}
        selected = []
        tool = oq.QueryTool(oq.TableBackend(call, lambda actor, ids: "set:x0000001", Links(),
                                            lambda a, ids: set(ids),
                                            select=lambda a, actor: selected.append(a)
                                            or {"ids": ["Note:n:1"], "gaps": []}))
        out = tool.call({"from": {"object_id": "Lot:fm:1"}, "traverse": [{"to": "Product"}]},
                        "actor")
        self.assertEqual(out["set"], "set:prods001")
        self.assertEqual([h["link_type"] for h in out["hops"]],
                         ["Lot.same_record_as", "Lot.of_item"])
        refused = tool.call({"from": {"object_type": "Note", "where": [
            {"property": "stage", "op": "eq", "value": "open"}]},
            "traverse": [{"to": "Product"}]}, "actor")
        self.assertEqual(refused["error"]["code"], "undeclared_link")
        self.assertIn("Note.mentions_product", refused["error"]["hint"])
        self.assertEqual(selected, [])

    def test_a_hops_own_where_keeps_what_it_reaches_before_the_next_hop(self):
        """One call walks, filters along the way and counts at the end: a hop's where
        filters that hop's records, the top-level where the last hop's."""
        table = Table()
        tool = oq.QueryTool(oq.TableBackend(table, lambda actor, ids: "set:new00001", Schema()))
        mid = [{"property": "invoice_date", "op": "gte", "value": "2026-01-01"}]
        last = [{"property": "name", "op": "contains", "value": "north"}]
        tool.call({"from": {"object_id": SAGE}, "traverse": [
            {"link_type": "SalesInvoiceLine.for_item", "direction": "reverse"},
            {"link_type": "SalesInvoiceLine.line_of", "direction": "forward", "where": mid}],
            "where": last, "measures": [{"op": "count"}]}, "actor")
        walked = [args for name, args in table.calls if name == "plx.around"]
        self.assertNotIn("filters", walked[0])
        self.assertEqual(walked[1]["filters"], mid + last)
        hop_where = [{"link_type": "SalesInvoiceLine.for_item", "direction": "reverse",
                      "where": mid}]
        table.calls.clear()
        tool.call({"from": {"object_id": SAGE}, "traverse": hop_where}, "actor")
        self.assertEqual(next(a for n, a in table.calls if n == "plx.around")["filters"], mid)

    def test_a_measured_walk_asks_for_its_set_never_a_page_of_its_records(self):
        """A count, total or date range over a link set is one set per hop, never a page
        of edges per hop: a hop only walked on or measured asks plx.around for its
        smallest concise listing (the set holds every record the hop reaches) and the
        measure is plx.aggregate's over that whole set."""
        table = Table()
        tool = oq.QueryTool(oq.TableBackend(table, lambda actor, ids: "set:new00001", Schema()))
        got = tool.call({"from": {"object_id": SAGE}, "traverse": [
            {"link_type": "SalesInvoiceLine.for_item", "direction": "reverse"}],
            "measures": [{"op": "max", "property": "invoice_date"}]}, "actor")
        walked = [args for name, args in table.calls if name == "plx.around"]
        self.assertEqual([(a["limit"], a["response_format"]) for a in walked],
                         [(1, "concise")])
        measured = [args for name, args in table.calls if name == "plx.aggregate"]
        self.assertEqual(measured[0]["set"], "set:lines0001")
        self.assertEqual(got["hops"][0]["count"], 2)

    def test_a_bad_request_is_refused_by_the_contract(self):
        tool = oq.QueryTool(oq.TableBackend(Table(), None, Schema()))
        self.assertEqual(tool.call({"from": {}}, "actor")["error"]["code"], "invalid_input")


class ListingGapsLeaveTheSetWhole(unittest.TestCase):
    """A gap of the reached set's listing (``rows``: a property no member type declares)
    says what the rows show, never which records the set holds: the set stays complete,
    so a later count of it is exact (11-14 link eval: "AgedPayable declares no property
    as_of_date; listed without it" made a count of 1 "at least 1"). A gap of the walk
    itself (a capped scan) still marks it."""

    def run_query(self, around_gaps=()):
        table, marked = Table(), []

        def around(name, arguments, actor):
            out = Table.__call__(table, name, arguments, actor)
            if name == "plx.around":
                out = dict(out, gaps=list(around_gaps))
            return out

        def rows(arguments, actor):
            return {"count": 2, "rows": [], "gaps": [{
                "state": "unknown", "reason": "unknown_property", "subject": "as_of_date",
                "detail": "SalesInvoiceLine declares no property as_of_date; listed "
                          "without it."}]}
        backend = oq.TableBackend(around, lambda actor, ids: "set:new00001", Schema(),
                                  rows=rows)
        backend.set_incomplete = lambda actor, handle: marked.append(handle)
        out = oq.QueryTool(backend).call({
            "from": {"object_id": SAGE},
            "traverse": [{"link_type": "SalesInvoiceLine.for_item", "direction": "reverse"}],
            "rows": {"properties": ["as_of_date", "invoiced_quantity"]},
            "measures": [{"op": "count"}]}, "actor")
        return out, marked

    def test_a_listing_gap_is_said_and_the_set_stays_complete(self):
        out, marked = self.run_query()
        self.assertNotIn("error", out)
        self.assertIn("unknown_property", [g["reason"] for g in out["gaps"]])
        self.assertEqual(marked, [])

    def test_a_gap_of_the_walk_still_marks_the_set_incomplete(self):
        out, marked = self.run_query([{"state": "unknown", "reason": "scan_cap",
                                       "subject": "SalesInvoiceLine.for_item",
                                       "detail": "the walk stopped at its cap"}])
        self.assertEqual(marked, ["set:lines0001"])


class WithoutHop(unittest.TestCase):
    """A ``without`` hop keeps the records that reach nothing over its link: walked out
    from all of them and back from what was reached; every other record has a link. A
    walk that may have missed a link refuses the hop (a count too high is never given)."""
    ORDERS = ["AssemblyOrder:fm:{\"id\":1}", "AssemblyOrder:fm:{\"id\":2}",
              "AssemblyOrder:fm:{\"id\":3}"]
    ALLOC = "AssemblyOrderAllocation.allocates"

    def run_query(self, back_gaps=(), reached=1, hop=None, where=None):
        sets = {"set:orders": list(self.ORDERS), "set:allocs": ["Alloc:1", "Alloc:2"],
                "set:back": [self.ORDERS[0], "AssemblyOrder:fm:{\"id\":9}"]}
        calls, made = [], []

        def call(name, arguments, actor):
            calls.append((name, dict(arguments)))
            if name == "plx.around":
                if arguments["from"] == "set:orders":
                    return {"set": "set:allocs" if reached else None,
                            "hops": [{"link_type": self.ALLOC, "direction": "reverse",
                                      "count": 2 * reached}]}
                return {"set": "set:back", "gaps": list(back_gaps),
                        "hops": [{"link_type": self.ALLOC, "direction": "forward",
                                  "count": 2}]}
            if name == "plx.aggregate":
                kept = sets[arguments["set"]]
                return {"groups": [{"key": {}, "value": str(len(kept)), "inputs": 0,
                                    "markings": [], "members_cited": kept}],
                        "partial": False, "completeness": {"complete": True}}
            raise AssertionError(name)

        def new_set(actor, ids):
            handle = f"set:kept{len(made)}"
            made.append(list(ids))
            sets[handle] = list(ids)
            return handle
        backend = oq.TableBackend(call, new_set, Schema(),
                                  members=lambda actor, handle: sets.get(handle))
        out = oq.QueryTool(backend).call({
            "from": {"set": "set:orders"},
            "traverse": [hop or {"link_type": self.ALLOC, "direction": "reverse",
                                 "without": True}],
            "measures": [{"op": "count"}], **({"where": where} if where else {})}, "actor")
        return out, made, calls

    def test_the_records_with_no_such_link_are_kept_and_counted(self):
        out, made, calls = self.run_query()
        self.assertNotIn("error", out)
        self.assertEqual(made, [self.ORDERS[1:]])
        self.assertEqual(out["hops"], [{"link_type": self.ALLOC, "direction": "reverse",
                                        "without": True, "count": 2}])
        self.assertEqual(out["results"][0]["groups"][0]["value"], "2")
        back = [args for name, args in calls if name == "plx.around"][1]
        self.assertEqual((back["from"], back["path"]),
                         ("set:allocs", [{"link_type": self.ALLOC, "direction": "forward"}]))

    def test_none_reached_keeps_every_record(self):
        out, made, _ = self.run_query(reached=0)
        self.assertEqual(made, [self.ORDERS])

    def test_a_walk_that_may_miss_a_link_refuses_the_hop(self):
        out, made, _ = self.run_query(back_gaps=[{"state": "unknown", "reason": "scan_cap",
                                                  "subject": "set:allocs",
                                                  "detail": "cut at its cap"}])
        self.assertEqual(out["error"]["code"], "without_unsettled")
        self.assertEqual(made, [])

    def test_a_top_level_where_after_a_without_hop_is_refused(self):
        """Review eaa8dc8ad0: the top-level where filtered the far end of the link (the
        allocations), never the orders kept: "open orders with no allocation" counted an
        open order whose allocations were all closed, and orders not open. Refused; the
        hop's own where still narrows the far end of the walk out."""
        status = [{"property": "status", "op": "eq", "value": "open"}]
        out, made, calls = self.run_query(where=status)
        self.assertEqual(out["error"]["code"], "invalid_input")
        self.assertEqual(made, [])
        self.assertFalse([name for name, _ in calls if name == "plx.around"])
        out, made, calls = self.run_query(hop={"link_type": self.ALLOC, "direction":
                                               "reverse", "without": True, "where": status})
        self.assertNotIn("error", out)
        walk_out, walk_back = [args for name, args in calls if name == "plx.around"]
        self.assertEqual(walk_out["filters"], status)
        self.assertNotIn("filters", walk_back)

    def test_without_needs_a_named_link(self):
        out, _, _ = self.run_query(hop={"to": "AssemblyOrderAllocation", "without": True})
        self.assertEqual(out["error"]["code"], "invalid_input")


class TwinExpansion(unittest.TestCase):
    """A walk whose filter empties what it reached tries those records' twins in another
    system (``<Type>.same_record_as``, lead links included and said as leads), as the
    reader; a type with no identity link, or two trimmed answers it cannot mark as
    trimmed, reach nothing more (fail closed)."""

    class Links:
        link_types = {"SalesInvoice.billed_to": {"from": "SalesInvoice", "to": "Customer"},
                      "Customer.same_record_as": {"from": "Customer", "to": "Customer"}}

    WHERE = [{"property": "name", "op": "contains", "value": "Alliance"}]

    def script(self, reverse_twin=None, trimmed=False, reverse_lost=None, lead=False):
        """plx.around as it answers: a twin joined by a confirmed link is in its set and
        count; one joined only by a lead (``lead``) is set apart, named by a
        possibly_related gap whose subject is its lead set (here the record itself)."""
        calls = []

        def call(name, arguments, actor):
            calls.append((name, dict(arguments), actor))
            if name == "plx.aggregate":
                return {"groups": [{"key": {}, "value": "1", "inputs": 1, "markings": []}]}
            hop = arguments["path"][0]
            gaps = [{"state": "unknown", "reason": "withheld_by_permission",
                     "subject": "x"}] if trimmed else []
            if hop["link_type"] == "SalesInvoice.billed_to":
                hit = "filters" not in arguments
                return {"set": "Customer:sage:1" if hit else None,
                        "hops": [{**hop, "count": int(hit)}]}
            if hop["direction"] == "forward" and lead:
                return {"set": "set:empty0001", "gaps": gaps + [{
                            "state": "unknown", "reason": "possibly_related",
                            "subject": "Customer:fm:78", "detail": "1 more record"}],
                        "hops": [{**hop, "count": 0, "tiers": {"lead": 1}}]}
            if hop["direction"] == "forward":
                return {"set": "Customer:fm:78", "gaps": gaps,
                        "hops": [{**hop, "count": 1, "tiers": {"verified": 1}}]}
            twin = reverse_twin
            if reverse_lost:
                gaps = [{"state": "unknown", "reason": reverse_lost, "subject": "twin"}]
            return {"set": twin, "gaps": gaps,
                    "hops": [{**hop, "count": 1 if twin else 0,
                              "tiers": {"verified": 1} if twin else {}}]}
        return calls, call

    def ask(self, call, links=None, withheld_set=None, lead_set=None):
        backend = oq.TableBackend(call, lambda actor, ids: "set:union0001", links or self.Links(),
                                  members=lambda actor, handle: [handle])
        backend.withheld_set = withheld_set
        backend.lead_set = lead_set
        return oq.QueryTool(backend).call({
            "from": {"object_id": "SalesInvoice:sage:9"},
            "traverse": [{"link_type": "SalesInvoice.billed_to", "direction": "forward"}],
            "where": self.WHERE, "measures": [{"op": "count"}]}, "reader")

    def test_a_filter_emptied_hop_reaches_the_lead_twin_said_as_a_likely_match(self):
        from ontology.json_contract import validate
        calls, call = self.script(lead=True)
        made = []
        out = self.ask(call, lead_set=lambda actor, ids, withheld=False:
                       made.append((ids, withheld)) or "set:lead0001")
        # the twin only a lead joins is walked on as a marked lead set, its count a lead
        self.assertEqual(out["set"], "set:lead0001")
        self.assertEqual(made, [(["Customer:fm:78"], False)])
        self.assertIn("lead_basis", {g["reason"] for g in out["gaps"]})
        self.assertEqual(out["hops"][-1], {"link_type": "Customer.same_record_as",
                                           "direction": "forward", "count": 1,
                                           "tiers": {"lead": 1}, "via_twin": True})
        twin_calls = [a for n, a, _ in calls
                      if n == "plx.around" and "same_record_as" in a["path"][0]["link_type"]]
        self.assertEqual({a["tier_floor"] for a in twin_calls}, {"lead"})
        self.assertTrue(all(a["filters"] == self.WHERE for a in twin_calls))
        self.assertEqual({actor for _, _, actor in calls}, {"reader"})
        said = next(g for g in out["gaps"] if g["reason"] == "via_twin")
        self.assertIn("likely match", said["detail"])
        self.assertEqual(validate(out, oq._CONTRACT["output"], root=oq._CONTRACTS), [])
        # a host that cannot mark a lead set reaches nothing through a lead twin
        calls, call = self.script(lead=True)
        self.assertIsNone(self.ask(call)["set"])

    def test_a_confirmed_twin_answers_and_a_lead_twin_stays_apart(self):
        """One twin joined by a confirmed link, another only by a lead: the walk goes on
        from the confirmed one alone (no lead_basis), and the lead one is named apart
        (possibly_related), never merged into what is counted."""
        calls, call = self.script(lead=True, reverse_twin="Customer:bc:4")
        out = self.ask(call, lead_set=lambda *a, **k: self.fail("no lead set is made"))
        self.assertEqual(out["set"], "Customer:bc:4")
        self.assertEqual(out["hops"][-1]["tiers"], {"verified": 1})
        reasons = {g["reason"] for g in out["gaps"]}
        self.assertIn("possibly_related", reasons)
        self.assertNotIn("lead_basis", reasons)

    def test_no_identity_link_or_an_unmarkable_trimmed_union_reaches_nothing_more(self):
        class NoTwin:
            link_types = {"SalesInvoice.billed_to": {"from": "SalesInvoice",
                                                     "to": "Customer"}}
        calls, call = self.script()
        out = self.ask(call, NoTwin())
        self.assertIsNone(out["set"])
        self.assertIn("nothing_reached", {g["reason"] for g in out["gaps"]})
        self.assertFalse(any("same_record_as" in str(a) for _, a, _ in calls))
        # both twin directions answered trimmed: joined only as a trimmed set
        calls, call = self.script(reverse_twin="Customer:bc:4", trimmed=True)
        self.assertIsNone(self.ask(call)["set"])
        marked = []
        calls, call = self.script(reverse_twin="Customer:bc:4", trimmed=True)
        out = self.ask(call, withheld_set=lambda actor, ids: marked.append(ids) or "set:held0001")
        self.assertEqual(out["set"], "set:held0001")
        self.assertEqual(marked, [["Customer:fm:78", "Customer:bc:4"]])

    def test_a_twin_direction_trimmed_to_nothing_marks_the_other_side_trimmed(self):
        """The reverse twin answers no set, only the reader's grants withheld it: the
        forward twin's set is used only marked as trimmed, the withheld gap is carried,
        and the hop's superseded dead ends are not said beside what the twin reached;
        with no way to mark it, nothing is reached (fail closed)."""
        for reason in ("marking_withheld", "withheld_by_permission",
                       "policy_changed_since_handle"):
            with self.subTest(reason=reason):
                marked = []
                calls, call = self.script(reverse_lost=reason)
                out = self.ask(call, withheld_set=lambda actor, ids: marked.append(ids)
                               or "set:held0002")
                self.assertEqual(out["set"], "set:held0002")
                self.assertEqual(marked, [["Customer:fm:78"]])
                reasons = [g["reason"] for g in out["gaps"]]
                self.assertIn(reason, reasons)
                self.assertIn("via_twin", reasons)
                self.assertFalse({"no_match", "nothing_reached"} & set(reasons), reasons)
                calls, call = self.script(reverse_lost=reason)
                self.assertIsNone(self.ask(call)["set"])
        # an untrimmed empty reverse direction leaves the forward handle as it is
        calls, call = self.script()
        self.assertEqual(self.ask(call, withheld_set=lambda actor, ids: "set:held0003")["set"],
                         "Customer:fm:78")



class OnePartNeverRefusesTheRead(unittest.TestCase):
    def test_an_undeclared_property_is_named_and_the_declared_ones_still_listed(self):
        """A row listing asking for one property the type does not declare lists the
        ones it declares and names the unknown one with the valid names; a listing of
        undeclared names alone stays refused, and plx.query passes the hint on."""
        from operating import ontology_tools_aggregate as oa
        from operating.tests.test_find_scan_resume import READER, _oid, listing_store
        from tests.test_ontology_tools import tools
        store = listing_store(2)
        self.addCleanup(store.conn.close)
        t = tools(store)
        agg = oa.AggregateTools(t)
        some = t._new_set(READER, [_oid("bulk-0000a"), _oid("bulk-0001a")], complete=True)
        got = agg.rows({"set": some, "properties": ["po_number", "line_amount_x"]}, READER)
        self.assertNotIn("error", got, got)
        self.assertEqual(sorted(r["values"]["po_number"]["value"] for r in got["rows"]),
                         ["PO300000", "PO300001"])
        [gap] = [g for g in got["gaps"] if g["reason"] == "unknown_property"]
        self.assertEqual(gap["subject"], "line_amount_x")
        self.assertIn("po_number", gap["detail"])
        empty = agg.rows({"set": t._new_set(READER, [], complete=True), "properties": ["po_number"],
                          "date": {"property": "order_date"}}, READER)
        self.assertNotIn("error", empty, empty)
        self.assertEqual((empty["count"], empty["rows"]), (0, []))
        refused = agg.rows({"set": some, "properties": ["line_amount_x"]}, READER)
        self.assertEqual(refused["error"]["code"], "unknown_property")
        self.assertIn("po_number", refused["error"]["hint"])

        tool = oq.QueryTool(oq.TableBackend(Table(), lambda actor, ids: "set:new00001",
                                            Schema(), rows=lambda a, actor: refused))
        out = tool.call({"from": {"set": "set:lines0001"},
                         "rows": {"properties": ["line_amount_x"]}}, "actor")
        [gap] = [g for g in out["gaps"] if g["reason"] == "rows_refused"]
        self.assertIn("po_number", gap["detail"])

    def test_an_entity_record_no_route_leaves_is_named_and_its_twins_still_walked(self):
        """A name held by a supplier record and a vendor record of a system no route
        leaves: the supplier is walked, the other is named (no_route), never the
        refusal of the whole walk; when no record routes, the walk is refused."""
        class Links:
            link_types = {"Lot.of_item": {"from": "Lot", "to": "Product"},
                          "Note.mentions_product": {"from": "Note", "to": "Product"}}
        names = {"Acme": ["Lot:fm:1", "Note:n:1"], "Solo": ["Note:n:1"]}

        def call(name, arguments, actor):
            if name == "plx.find":
                return {"status": "resolved", "entities": [{
                    "mention": arguments["text"], "status": "match", "records": [
                        {"object": {"object_id": oid}, "status": "match"}
                        for oid in names[arguments["text"]]]}]}
            if name == "plx.aggregate":
                return {"groups": [{"key": {}, "value": "2", "inputs": 0, "markings": []}]}
            hop = arguments["path"][0]
            return {"set": "set:prods001", "hops": [{"link_type": hop["link_type"],
                                                     "direction": hop["direction"],
                                                     "count": 2}]}
        tool = oq.QueryTool(oq.TableBackend(call, lambda actor, ids: "set:x0000001", Links(),
                                            lambda a, ids: set(ids)))
        out = tool.call({"from": {"entity": "Acme"}, "traverse": [{"to": "Product"}]}, "actor")
        self.assertNotIn("error", out, out)
        self.assertEqual([r["record"] for r in out["by_record"]], ["Lot:fm:1"])
        self.assertIn(("no_route", "Note:n:1"),
                      [(g["reason"], g["subject"]) for g in out["gaps"]])
        refused = tool.call({"from": {"entity": "Solo"}, "traverse": [{"to": "Product"}]},
                            "actor")
        self.assertEqual(refused["error"]["code"], "undeclared_link")


class IdentifierRoute(unittest.TestCase):
    """A walk no stored link completes resolves the identifiers its records' own values
    hold: an exact identifier of one thing (one record, or its records in several
    systems) is firm and walked on; a number two things share is a lead, never walked;
    what was searched is said either way."""

    class Links:
        link_types = {"Lot.of_item": {"from": "Lot", "to": "Item", "cardinality": "N:1"},
                      "Order.for_item": {"from": "Order", "to": "Item", "cardinality": "N:1"},
                      "Shipment.ships": {"from": "Shipment", "to": "Order",
                                         "cardinality": "N:1"}}
        object_types = {"Lot": {"properties": [{"name": "order_list", "value_type": "text"}]},
                        "Item": {"properties": []}}

    def tool(self, entities):
        calls = []
        sets = {"set:orders01": ["Order:sage:7", "Order:portal:7"],
                "set:ships001": ["Shipment:sage:3"]}

        def call(name, arguments, actor):
            calls.append((name, dict(arguments)))
            if name == "plx.find":
                ids = ["Order:sage:7", "Order:portal:7"]
                return {"status": "ambiguous", "candidates": [
                    {"object": {"object_id": i}, "reason": "exact_identifier",
                     "tier": "verified"} for i in ids],
                    "entities": [{"records": [{"object": {"object_id": i}, "status": "match"}
                                              for i in group]} for group in entities(ids)]}
            if name == "plx.aggregate":
                return {"groups": [{"key": {}, "value": "1", "inputs": 0, "markings": []}]}
            hop = arguments["path"][0]
            ships = hop["link_type"] == "Shipment.ships" and arguments["from"] == "set:orders01"
            return {"set": "set:ships001" if ships else None,
                    "hops": [dict(hop, count=1 if ships else 0)]}

        def rows(arguments, actor):
            return {"rows": [{"object_id": "Lot:fm:1", "values": {"order_list": {
                "value": {"untrusted_text": "OR1000777777 sample run"}}}}]}

        new = iter(["set:orders01", "set:other001", "set:other002"])
        backend = oq.TableBackend(call, lambda actor, ids: next(new), self.Links(),
                                  lambda a, ids: set(ids), rows=rows,
                                  members=lambda actor, handle: sets.get(handle))
        return oq.QueryTool(backend), calls

    def test_an_order_number_a_lot_holds_reaches_its_shipments(self):
        tool, calls = self.tool(lambda ids: [ids])
        out = tool.call({"from": {"object_id": "Lot:fm:1"},
                         "traverse": [{"to": "Shipment"}]}, "actor")
        self.assertEqual(out["set"], "Shipment:sage:3")
        [gap] = [g for g in out["gaps"] if g["reason"] == "matched_by_identifier"]
        self.assertIn("OR1000777777", gap["detail"])
        self.assertIn("Lot.order_list", gap["searched"]["scopes"])
        self.assertNotIn("nothing_reached", {g["reason"] for g in out["gaps"]})

    def test_a_number_two_things_share_is_a_lead_never_walked(self):
        tool, calls = self.tool(lambda ids: [[i] for i in ids])
        out = tool.call({"from": {"object_id": "Lot:fm:1"},
                         "traverse": [{"to": "Shipment"}]}, "actor")
        self.assertIsNone(out["set"])
        reasons = {g["reason"] for g in out["gaps"]}
        self.assertTrue({"identifier_leads", "no_identifier_match", "nothing_reached"}
                        <= reasons)
        self.assertNotIn(("plx.around", "set:orders01"),
                         [(n, a.get("from")) for n, a in calls])

    def test_a_cut_identifier_search_says_what_it_left_and_claims_no_absence(self):
        tool, calls = self.tool(lambda ids: [[i] for i in ids])
        numbers = [f"OR10007777{n:02d}" for n in range(oq.IDENTIFIER_QUERIES + 3)]
        tool.backend.rows = lambda arguments, actor: {"rows": [
            {"object_id": "Lot:fm:1", "values": {"order_list": {
                "value": {"untrusted_text": " ".join(numbers)}}}}]}
        out = tool.call({"from": {"object_id": "Lot:fm:1"},
                         "traverse": [{"to": "Shipment"}]}, "actor")
        reasons = {g["reason"] for g in out["gaps"]}
        self.assertIn("identifier_search_cut", reasons)
        self.assertNotIn("no_identifier_match", reasons)   # never an absence after a cut
        [cut] = [g for g in out["gaps"] if g["reason"] == "identifier_search_cut"]
        self.assertIn(f"3 of {len(numbers)} identifiers", cut["detail"])
        self.assertEqual(len(cut["searched"]["queries"]), oq.IDENTIFIER_QUERIES)
        finds = [a["query"] for n, a in calls if n == "plx.find"]
        self.assertEqual(sorted(cut["searched"]["queries"]), sorted(finds))


if __name__ == "__main__":
    unittest.main()
