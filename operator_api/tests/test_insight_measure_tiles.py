"""One tile per catalogue measure (the agreed definitions in ``operating/measures.json``):
none dropped, none twice, each states the catalogue's own definition and runs the
catalogue's own statement, each passes the SQL checker, and each is read ahead after the
declared tiles, for a signed-in reader (never a directory employee's round)."""
from __future__ import annotations

import unittest

from operating import measures as ms
from operating.ontology_tools_sql import check
from operator_api import insights as ins


class MeasureTiles(unittest.TestCase):
    def test_every_agreed_measure_has_exactly_one_tile_with_its_own_definition_and_statement(self):
        tiles = {s.id: s for s in ins.declared() if s.id.startswith(ins.MEASURE_TILE)}
        catalogue = ms.measures()
        self.assertEqual(sorted(tiles), sorted(f"{ins.MEASURE_TILE}{n}" for n in catalogue))
        ids = [s.id for s in ins.declared()]
        self.assertEqual(len(ids), len(set(ids)))   # none twice
        for name, entry in catalogue.items():
            tile = tiles[f"{ins.MEASURE_TILE}{name}"]
            self.assertTrue(tile.about.startswith(str(entry["definition"])))
            self.assertEqual(tile.sql, ms.statement(name, by="month" if entry.get("date") else None)[0])
            check(tile.sql)
            self.assertIn(str(entry.get("owner")), tile.group)
            self.assertEqual(tile.scope, "everyone")   # the reader's door decides the rows

    def test_a_dated_measure_with_no_currency_draws_a_line_by_month_and_the_rest_a_table(self):
        tiles = {s.id: s for s in ins.declared()}
        self.assertEqual(tiles["measure_invoices_issued"].chart.get("kind"), "line")
        self.assertFalse(tiles["measure_revenue"].chart)   # per currency: a table, never mixed currencies on one line
        self.assertFalse(tiles["measure_open_orders"].chart)

    def test_measure_tiles_are_read_ahead_after_every_declared_tile_and_never_for_a_directory_round(self):
        self.assertFalse([s for s in ins.builtin() if s.id.startswith(ins.MEASURE_TILE)])
        specs = list(ins.declared())
        measures = [ins.ref(s) for s in specs if s.id.startswith(ins.MEASURE_TILE)]
        first = [ins.ref(s) for s in specs if not s.id.startswith(ins.MEASURE_TILE)]
        self.assertTrue(measures and first)
        # a saved tile listed last is still read before any measure
        saved = ins.Spec(id="s1", title="Mine", about="", sql="SELECT 1", chart={}, group="",
                         person="", scope="everyone", saved=True)
        got = ins.ahead_refs(specs + [saved])
        self.assertEqual(got, first + ["SELECT 1"] + measures)
        self.assertEqual(ins.ahead_refs(specs + [saved], measures=False), first + ["SELECT 1"])

    def test_the_warm_up_reads_measure_tiles_only_for_a_signed_in_reader(self):
        from unittest import mock
        from types import SimpleNamespace

        class Host(ins.InsightsMixin):
            def _insight_scope(self, reader):
                return (True,)

            def insight_store(self):
                return None

        host, asked = Host(), []
        keeper = SimpleNamespace(ahead=lambda reader, sqls: asked.append(list(sqls)) or [])
        reader = SimpleNamespace(key="k", session=SimpleNamespace(view_as=False))
        with mock.patch.object(Host, "_insight_keeper", lambda self: keeper), \
                mock.patch.object(Host, "_findings_open", staticmethod(lambda r: True)), \
                mock.patch("operator_api.insights_cache.reader_key", lambda r: ("k",)):
            host._insights_ahead(reader)
            host._insights_ahead(reader, later=True)
        tiles = [ins.ref(s) for s in ins.declared() if s.id.startswith(ins.MEASURE_TILE)]
        self.assertTrue(set(tiles) <= set(asked[0]))
        self.assertFalse(set(tiles) & set(asked[1]))
        self.assertTrue(asked[1])

if __name__ == "__main__":
    unittest.main()
