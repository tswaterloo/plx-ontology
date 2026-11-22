"""One tile per catalogue measure (the agreed definitions in ``operating/measures.json``):
none dropped, none twice, each states the catalogue's own definition and runs the
catalogue's own statement, each passes the SQL checker, and none is read ahead with the
declared tiles (opened when asked)."""
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

    def test_the_declared_builtins_are_unchanged_and_measure_tiles_are_not_read_ahead(self):
        self.assertFalse([s for s in ins.builtin() if s.id.startswith(ins.MEASURE_TILE)])
        refs = [ins.ref(s) for s in ins.declared() if not s.id.startswith(ins.MEASURE_TILE)]
        self.assertTrue(refs and not any(ins.MEASURE_TILE in r for r in refs))


if __name__ == "__main__":
    unittest.main()
