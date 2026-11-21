import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from operator_api import insight_tool as it
from operator_api import insights
from operator_api import simple_ask

ME, OTHER = "0000-me", "0000-other"


class Backend:
    """insight_result as the Insights page runs it: insights.result with the reader's scope."""

    def __init__(self, everyone: bool):
        self.scope = (everyone, frozenset({ME}), lambda oid: False, True)

    def insight_result(self, reader, iid):
        spec = next(s for s in insights.builtin() if s.id == iid)
        if spec.scope == "full" and not self.scope[0]:
            raise insights.Refused("insight_withheld")
        columns = [spec.person or "person_id", "name", "waiting"]
        read = {"got": {"columns": columns, "rows": [
            {"cells": [ME, "Me Myself", 3]}, {"cells": [OTHER, "Other Person", 5]}],
            "as_of": None, "truncated": False}, "read_at": "2026-11-20T09:00:00+00:00"}
        return insights.result(self, reader, spec, self.scope, read)

    def insight_tools(self, reader):
        return it.tools(self, reader)


class InsightToolTest(unittest.TestCase):
    def setUp(self):
        drawn = mock.patch("operator_api.charts.draw", return_value=None)
        drawn.start()
        self.addCleanup(drawn.stop)

    def call(self, backend, iid):
        with mock.patch.dict(os.environ, {it.FLAG: "1"}):
            (tool,) = it.tools(backend, SimpleNamespace(key="r"))
        return tool.call({"id": iid})

    def test_off_unless_its_flag_is_set(self):
        with mock.patch.dict(os.environ, {it.FLAG: ""}):
            self.assertEqual(it.tools(Backend(False), None), [])
            self.assertEqual(simple_ask.write_tools(Backend(False), None), [])
        with mock.patch.dict(os.environ, {it.FLAG: "1"}):
            names = [t.spec["name"] for t in simple_ask.write_tools(Backend(False), None)]
        self.assertEqual(names, [it.NAME])

    def test_a_row_the_tile_hides_from_the_asker_never_comes_back(self):
        out = self.call(Backend(False), "colleagues_waiting")
        self.assertTrue(out["ok"], out)
        self.assertNotIn("Other Person", json.dumps(out))
        self.assertNotIn(OTHER, json.dumps(out))
        self.assertEqual(out["row_count"], 1)
        self.assertNotIn("person_id", out["columns"])   # the person column is dropped
        self.assertEqual(out["rows"][0]["cells"][0], {"untrusted_text": "Me Myself"})   # enveloped

    def test_a_reader_who_sees_everyone_gets_every_row(self):
        out = self.call(Backend(True), "colleagues_waiting")
        self.assertEqual(out["row_count"], 2)

    def test_a_tile_not_for_them_and_an_unknown_id_are_refused(self):
        full = next(s.id for s in insights.builtin() if s.scope == "full")
        self.assertEqual(self.call(Backend(False), full)["error"], "insight_withheld")
        self.assertEqual(self.call(Backend(True), "nope")["error"], "not_found")

    def test_through_the_real_insights_mixin(self):
        """The real InsightsMixin.insight_result (test_insights' backend: a fake read worker
        only): a full tile withheld before any read, other people's rows never returned."""
        from operator_api.tests import test_insights as ti
        b = ti.Backend(ti.sql_out(ti.Mixin.rows, ti.OUTPUT), everyone=False,
                       me=frozenset({ti.ME}))
        with mock.patch.dict(os.environ, {it.FLAG: "1"}):
            (tool,) = b.insight_tools(ti.reader("ana"))
        for full in (s.id for s in insights.builtin() if s.scope == "full"):
            self.assertEqual(tool.call({"id": full})["error"], "insight_withheld")
        self.assertEqual(b.reads.sql, [])   # nothing read for a withheld tile
        out = tool.call({"id": "team_output"})
        self.assertTrue(out["ok"], out)
        said = json.dumps(out)
        for gone in ("Other", "Hidden", ti.OTHER, ti.HIDDEN, ti.ME):
            self.assertNotIn(gone, said)
        self.assertEqual(out["row_count"], 1)

    def test_a_figure_the_tile_returned_binds_the_reply_and_is_cited(self):
        """The Ask's figure gate and citations read a plx.insight result as a plx.sql one:
        the tile's own number passes, a number it does not hold is still marked, and the
        citation holds only the rows the asker got (live 11-21 wt10, flag on: every tile
        number came out "(unverified)", 8/10 answers partial)."""
        out = self.call(Backend(False), "colleagues_waiting")
        events = [{"type": "tool_call", "name": it.NAME, "arguments": {"id": "colleagues_waiting"},
                   "result": json.dumps(out)}]
        text, marked = simple_ask.gate("Me Myself has 3 colleagues waiting.", events)
        self.assertEqual(marked, [], text)
        text, marked = simple_ask.gate("Me Myself has 7 colleagues waiting.", events)
        self.assertIn("7" + simple_ask.UNVERIFIED, text)
        (table,) = simple_ask.cited(events)
        self.assertTrue(table["about"].startswith("Insights: "), table)
        self.assertEqual(table["rows"], [["Me Myself", "3"]])
        self.assertNotIn("Other Person", json.dumps(table))
        failed = [{**events[0], "is_error": True}]
        self.assertEqual(simple_ask.cited(failed), [])
        self.assertIn("3" + simple_ask.UNVERIFIED, simple_ask.gate("Me Myself has 3.", failed)[0])

    def test_a_tile_that_fails_to_read_is_unreadable_and_logs_no_value(self):
        class Broken(Backend):
            def insight_result(self, reader, iid):
                raise ValueError("Secret Customer 4417")
        with self.assertLogs("operator_api.ask", "WARNING") as logs:
            out = self.call(Broken(True), "colleagues_waiting")
        self.assertEqual(out["error"], "insight_unreadable")
        said = json.dumps(out) + "".join(logs.output)
        self.assertNotIn("Secret", said)
        self.assertNotIn("4417", said)
        self.assertIn("ValueError", "".join(logs.output))

    def test_its_description_fits_the_listing(self):
        self.assertLessEqual(len(it.SPEC["description"]), 600)


if __name__ == "__main__":
    unittest.main()
