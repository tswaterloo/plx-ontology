import unittest

from operating import insight_statements as ins
from operating.ontology_tools_identity import ToolError
from operator_api import insights


class InsightStatementsTest(unittest.TestCase):
    def test_each_tile_is_described_as_the_statement_it_runs(self):
        every = {t.id: t for t in insights.builtin()}
        tiles = {k: t for k, t in every.items() if not t.person and not t.rollup}
        self.assertEqual(set(ins.listed()["insights"]), set(tiles))
        self.assertGreater(len(tiles), 10)
        for hidden in ("colleagues_waiting", "team_output", "org_span", "org_depth"):
            with self.assertRaises(ToolError):   # rows dropped after the query: never handed out
                ins.described(ins.INSIGHT + hidden)
        for tid, tile in tiles.items():
            got = ins.described(ins.INSIGHT + tid)
            self.assertEqual(got["sql"], tile.sql, tid)            # placeholders filled
            self.assertNotIn("{business_tz}", got["sql"])
        with self.assertRaises(ToolError):
            ins.described("insight:nope")

    def test_no_file_lists_nothing(self):
        from unittest import mock
        from pathlib import Path
        with mock.patch.object(ins, "FILE", Path("/nonexistent/insights.json")):
            self.assertEqual(ins.listed(), {})


if __name__ == "__main__":
    unittest.main()
