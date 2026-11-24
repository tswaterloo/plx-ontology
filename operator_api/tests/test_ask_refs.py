"""Answers by reference (Ask plan A2, ``operator_api.ask_refs``): the host prints the figures
the model takes from plx.sql results; a reference that does not resolve and a typed figure
no result holds are listed for the one repair. Replays the HARD20 hand-arithmetic facts."""
import datetime as dt
import json
import unittest

from operator_api import ask_refs as r

A = "sql://" + "a" * 20
B = "sql://" + "b" * 20
TODAY = dt.date(2026, 11, 23)


def sql(rid, columns, cells, profile=None):
    return {"type": "tool_call", "name": "plx.sql", "result": json.dumps({
        "result": rid, "columns": columns, "profile": profile or {},
        "rows": [{"cite": f"{rid}#row={i}", "cells": c} for i, c in enumerate(cells)]})}


LATE = sql(A, ["po_number", "supplier", "days_late", "total", "total_currency",
               "requested_date"],
           [["106001", "DSM", "152", "1993.33", "CAD", "2026-06-24"],
            ["PO100109", {"untrusted_text": "Croda Canada Ltd"}, "131", "664210", "CAD",
             "2026-07-15"]],
           {"rows": 2, "exact": True, "columns": [
               {"column": "total", "nulls": 0, "distinct": 2,
                "sum": [{"currency": "CAD", "sum": "666203.33"}]},
               {"column": "days_late", "nulls": 0, "distinct": 2, "sum": "283"},
               {"column": "supplier", "nulls": 0, "distinct": 2}]})
MAIL = {"type": "tool_call", "name": "plx.search", "result": json.dumps({"documents": [
    {"text": {"untrusted_text": "Croda: we ship 500 kg on 12 Nov 2026, ref R-0098"}}]})}


def render(text, events=(LATE, MAIL), **kw):
    return r.render(text, list(events), today=TODAY, **kw)


class Refs(unittest.TestCase):
    def test_counts_sums_cells_and_arithmetic_are_printed_by_the_host(self):
        got = render(f"{{{{count({A})}}}} late, {{{{sum({A}.total, CAD)}}}} CAD; "
                     f"{{{{{A}#row=1.supplier}}}} {{{{{A}#row=1.po_number}}}} is "
                     f"{{{{{A}#row=1.days_late}}}} days late (asked {{{{{A}#row=1.requested_date}}}}); "
                     f"share {{{{{A}#row=1.total / sum({A}.total) * 100 | 1}}}}%; "
                     f"{{{{days({A}#row=1.requested_date, today)}}}} days; "
                     f"{{{{distinct({A}.supplier)}}}} suppliers")
        self.assertEqual(got.text, "2 late, 666,203.33 CAD; Croda Canada Ltd PO100109 is "
                                   "131 days late (asked 15 Jul 2026); share 99.7%; 131 days; "
                                   "2 suppliers")
        self.assertEqual((got.bad, got.untraced), ([], []))
        # a number held in a text column (a PO number) prints as stored, never "106,001"
        self.assertEqual(render(f"{{{{{A}#row=0.po_number}}}} {{{{{A}#row=0.total}}}}").text,
                         "106001 1,993.33")

    def test_a_reference_that_does_not_resolve_is_listed_with_why(self):
        got = render(f"{{{{{A}#row=9.total}}}} {{{{count({B})}}}} {{{{{A}#row=0.nope}}}} "
                     f"{{{{sum({A}.supplier)}}}} {{{{1 / 0}}}} {{{{sum({A}.total, USD)}}}}")
        self.assertEqual(got.text.split(), ["[?]"] * 6)
        why = " | ".join(got.bad)
        for part in ("was not shown to you", "not a result you got", "no column nope",
                     "not summed", "division by zero", "no USD sum"):
            self.assertIn(part, why)

    def test_a_measures_stated_exclusion_is_printed_by_reference(self):
        late = json.loads(LATE["result"])
        # S2: the reference prints how many records the parameter leaves out (n), never
        # the measure's value over them (a sum measure's value is no count of records)
        late["excluded"] = [{"parameter": "bc_only", "says": "held only in Business Central",
                             "result": B, "n": 3, "value": "664210"}]
        got = render(f"{{{{count({A})}}}} late; {{{{excluded({A}, bc_only)}}}} more held "
                     f"only in Business Central; {{{{excluded({A}, nope)}}}}",
                     [dict(LATE, result=json.dumps(late))])
        self.assertEqual(got.text, "2 late; 3 more held only in Business Central; [?]")
        self.assertIn("no count for that exclusion", got.bad[0])

    def test_a_partial_profile_prints_no_whole_result_figure(self):
        """The whole-result statement failed: the profile covers the held rows only
        (exact false). No column figure of it is printed as the result's; the model is
        told to read it with a query."""
        part = sql(B, ["v"], [["1"]], {"rows": None, "exact": False,
                                        "note": "counted over the first 5,000 rows only",
                                        "columns": [{"column": "v", "sum": "123.4",
                                                     "min": "1", "max": "9", "nulls": 0,
                                                     "distinct": 4}]})
        for f in ("sum", "min", "max", "nulls", "distinct"):
            got = render(f"Total {{{{{f}({B}.v)}}}}", [part])
            self.assertEqual(got.text, "Total [?]", f)
            self.assertIn("first 5,000 rows", got.bad[0])
        self.assertIn("exact row count is not known", render(f"{{{{count({B})}}}}", [part]).bad[0])

    def test_a_figure_past_the_decimal_context_is_refused_not_raised(self):
        big = sql(B, ["v"], [["1" + "0" * 40]], {"rows": 1, "columns": [{"column": "v", "max": "1" + "0" * 40}]})
        got = render(f"{{{{max({B}.v) | 2}}}}", [big])
        self.assertEqual(got.text, "[?]")

    def test_money_in_several_currencies_must_name_one(self):
        two = sql(B, ["v", "c"], [["1", "CAD"], ["2", "USD"]], {"rows": 2, "columns": [
            {"column": "v", "sum": [{"currency": "CAD", "sum": "1"},
                                    {"currency": "USD", "sum": "2"}]}]})
        got = render(f"{{{{sum({B}.v)}}}}", [two])
        self.assertIn("per currency (CAD, USD)", got.bad[0])
        self.assertEqual(render(f"{{{{sum({B}.v, USD)}}}}", [two]).text, "2")

    def test_typed_figures_must_be_held_by_a_result_or_the_person(self):
        """HARD20 C1: '108 open POs' (the result held 109), '4 meetings' above 5 rows, a
        record number nobody returned: listed for repair. Held values, the person's own
        figures, a message's figures and dates, today and list numbers pass."""
        got = render("1. PO100109 is 131 days late; 108 POs open; PO999123 too; Croda "
                     "ships 500 kg on 12 Nov 2026 (R-0098); 3 Oct; as of 23 Nov; top 5.",
                     asked=["top 5 please"])
        self.assertEqual(got.untraced, ["3 Oct", "PO999123", "108"])

    def test_chart_blocks_and_links_are_left_as_written(self):
        got = render("```chart\n{\"a\": {\"b\": 77}}\n```\nSee [x](https://x/7777)")
        self.assertEqual(got.untraced, [])
        self.assertIn('{"a": {"b": 77}}', got.text)

    def test_a_stream_holds_back_an_unclosed_reference(self):
        self.assertEqual(render(f"So far {{{{count({A})}}}} and {{{{sum({A}.to",
                                partial=True).text, "So far 2 and ")

    def test_the_hard20_hand_arithmetic_cannot_be_written_by_reference(self):
        """Replay (model-free) of the HARD20 C1 facts A1's profile alone leaves open:
        h18's share summed from rounded rows (61.5% where the unrounded totals give
        61.3%) and h16's cross-counts read off a 48-row list (13 vs 10, 8 vs 6). By
        reference the share is taken from the unrounded totals and the counts from the
        query's own rows; typed, each is listed for repair."""
        top = sql(B, ["rev"], [["4869.83"], ["6130.17"]],
                  {"rows": 2, "columns": [{"column": "rev", "sum": "11000.00"}]})
        whole = sql(A, ["rev"], [["17945.00"]],
                    {"rows": 1, "columns": [{"column": "rev", "sum": "17945.00"}]})
        share = render(f"{{{{sum({B}.rev) / sum({A}.rev) * 100 | 1}}}}%", [top, whole])
        self.assertEqual(share.text, "61.3%")
        self.assertEqual(render("61.5%", [top, whole]).untraced, ["61.5"])
        counts = sql(B, ["status", "dated", "n"], [["open", "yes", "10"], ["open", "no", "6"]],
                     {"rows": 2, "columns": [{"column": "n", "sum": "16"}]})
        got = render(f"{{{{{B}#row=0.n}}}} of {{{{sum({B}.n)}}}} dated; 13 open dated, "
                     "8 undated", [counts])
        self.assertEqual(got.text.split(";")[0], "10 of 16 dated")
        self.assertEqual(got.untraced, ["13", "8"])


class ToolsPrintReferences(unittest.TestCase):
    """One prompt, one renderer: a write, document or proposal tool of a session on the
    Ask's prompt gets the references in its arguments printed from the session's own
    results before it acts (A2a); one that does not resolve refuses the call, named."""

    def tool(self):
        from operating.agent_session import LocalTool
        calls = []
        made = LocalTool(spec={"name": "plx.make_document"},
                         call=lambda a: calls.append(a) or {"ok": True})
        return r.rendering(made), calls

    def test_a_document_holds_the_host_s_figures_and_a_hole_refuses_it(self):
        tool, calls = self.tool()
        self.assertTrue(tool.sees_results)
        ok = tool.call({"content": f"Late: {{{{count({A})}}}}, CAD {{{{sum({A}.total, CAD)}}}}",
                        "tables": [{"rows": [[f"{{{{{A}#row=1.po_number}}}}"]]}],
                        "template": "Dear {{supplier_name}}"}, [LATE])
        self.assertEqual(ok, {"ok": True})
        self.assertEqual(calls[0]["content"], "Late: 2, CAD 666,203.33")
        self.assertEqual(calls[0]["tables"][0]["rows"][0][0], "PO100109")
        # a template's own field is no reference: left as written
        self.assertEqual(calls[0]["template"], "Dear {{supplier_name}}")
        bad = tool.call({"content": f"Total {{{{sum({B}.total)}}}}"}, [LATE])
        self.assertEqual(bad["error"], r.REF_REFUSED)
        self.assertIn(B, bad["references"][0])
        self.assertEqual(len(calls), 1)   # the tool itself was never called for the hole

    def test_a_table_of_1_10_60_rows_prints_in_one_pass(self):
        """1/10/60 rows: the session's results are read once per call (one pass over the
        events), however many references the arguments hold."""
        from unittest import mock
        tool, calls = self.tool()
        real = r.results
        for n in (1, 10, 60):
            res = sql(B, ["v"], [[str(i)] for i in range(n)], {"rows": n, "exact": True})
            rows = [[f"{{{{{B}#row={i}.v}}}}"] for i in range(n)]
            with mock.patch.object(r, "results", side_effect=real) as read:
                tool.call({"tables": [{"rows": rows}]}, [res])
            self.assertEqual([c[0] for c in calls[-1]["tables"][0]["rows"]],
                             [str(i) for i in range(n)])
            self.assertEqual(read.call_count, 1, "the results are read once per call")

    def test_the_gate_hands_a_seeing_tool_the_session_s_results_so_far(self):
        """``serve_gate``: a local tool that ``sees_results`` is called with the results of
        the host reads the session made before it (as events), so a made file is printed
        from what the session read; a tool that does not see them is called as before."""
        import io, sys, tempfile, textwrap, threading
        from pathlib import Path
        from operating import agent_session
        got_seen = []
        seeing = agent_session.LocalTool(spec={"name": "plx.make_document"},
                                         call=lambda a, seen: got_seen.append(seen) or {"ok": 1},
                                         sees_results=True)
        plain = agent_session.LocalTool(spec={"name": "plx.memory"}, call=lambda a: {"ok": 2})
        payload = json.loads(LATE["result"])
        with tempfile.TemporaryDirectory() as tmp:
            host = Path(tmp) / "host.py"
            host.write_text(textwrap.dedent(f"""
                import json, sys
                for line in sys.stdin:
                    m = json.loads(line)
                    if "id" not in m:
                        continue
                    r = {{"tools": [{{"name": "plx.sql"}}]}} if m["method"] == "tools/list" else {{"content": [{{"type": "text", "text": {json.dumps(json.dumps(payload))}}}], "isError": False}}
                    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": m["id"], "result": r}}) + "\\n")
                    sys.stdout.flush()
            """), encoding="utf-8")
            out = io.StringIO()
            answered = threading.Event()

            class Out(io.StringIO):
                def write(self, text):
                    if '"id": 2' in text:
                        answered.set()
                    return out.write(text)

            def lines():
                yield json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}) + "\n"
                yield json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": "plx.sql", "arguments": {"sql": "select 1"}}}) + "\n"
                answered.wait(10)
                yield json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                    "name": "plx.make_document", "arguments": {"content": "x"}}}) + "\n"
                yield json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                    "name": "plx.memory", "arguments": {}}}) + "\n"
            agent_session.serve_gate(["plx.sql"], [sys.executable, str(host)],
                                     Path(tmp) / "refused.jsonl", stdin=lines(), stdout=Out(),
                                     local={"plx.make_document": seeing, "plx.memory": plain})
        (seen,) = got_seen
        self.assertEqual([e["name"] for e in seen], ["plx.sql"])
        self.assertEqual(r.print_refs(f"{{{{count({A})}}}}", seen)[0], "2")
        replies = {m["id"]: m["result"] for m in map(json.loads, out.getvalue().splitlines())}
        self.assertEqual(json.loads(replies[4]["content"][0]["text"]), {"ok": 2})


class Form(unittest.TestCase):
    """Spec 13.4 P3: money prints with 2 decimals, any other stored number without trailing
    zeros or more than 4 decimals; money is the profile's own kind, never a name or a value."""

    def sql(self):
        return sql(B, ["supplier", "owed", "unit_price", "ratio", "n"],
                   [["Apex", "873195.750", "114.21044689095", "3.123456", "12.500"]],
                   {"rows": 1, "exact": True, "columns": [
                       {"column": "owed", "kind": "money", "nulls": 0, "distinct": 1,
                        "sum": [{"currency": "CAD", "sum": "873195.750"}]},
                       {"column": "unit_price", "kind": "money", "nulls": 0, "distinct": 1,
                        "sum": [{"currency": "CAD", "sum": "114.21044689095"}]},
                       {"column": "ratio", "nulls": 0, "distinct": 1, "sum": "3.123456"},
                       {"column": "n", "nulls": 0, "distinct": 1, "sum": "12.500"}]})

    def test_money_has_two_decimals_and_other_numbers_are_trimmed(self):
        got = render(f"{{{{{B}#row=0.owed}}}} | {{{{{B}#row=0.unit_price}}}} | "
                     f"{{{{{B}#row=0.ratio}}}} | {{{{{B}#row=0.n}}}} | "
                     f"{{{{sum({B}.owed, CAD)}}}}", events=[self.sql()])
        self.assertEqual(got.text, "873,195.75 | 114.21 | 3.1235 | 12.5 | 873,195.75")
        self.assertEqual(got.bad, [])

    def test_a_column_the_profile_does_not_call_money_is_never_rounded_to_cents(self):
        ev = sql(B, ["x"], [["1.23456"]], {"rows": 1, "exact": True, "columns": [
            {"column": "x", "nulls": 0, "distinct": 1, "sum": "1.23456"}]})
        self.assertEqual(render(f"{{{{{B}#row=0.x}}}}", events=[ev]).text, "1.2346")


if __name__ == "__main__":
    unittest.main()
