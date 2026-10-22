"""gap-b01a86b2f438 measurement: shape classes and counts only, no identifier.
Injected fixture world; not live."""

from __future__ import annotations

import json
import unittest

from evals.oracle import BcReads, PortalReads, Sources
from evals.oracle import number_shapes as ns
from evals.oracle.fixture_world import bc_record, build_world
from evals.oracle.source import field_value, key_text


class NumberShapesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = build_world()
        self.sources = Sources(PortalReads(self.world.read_relation),
                               BcReads(self.world.bc_port()))

    def test_shape_classes(self):
        self.assertEqual(ns.shape("SO-2026-0042"), "U2-D4-D4")
        self.assertEqual(ns.shape("Jo Smith"), "U1L1_1U1L4")
        # no character of a name survives: non-ASCII is X, other punctuation ?
        self.assertEqual(ns.shape("José Ñ#12/AB'"), "U1L2X1_1X1#D2/U2?")

    def test_probing_stops_at_the_run_deadline(self):
        from evals.oracle.cross_sample import portal_roots
        rows = portal_roots(self.sources, "sales_order")
        out = ns.measure(self.sources, probes=len(rows), remaining=lambda: 0.0)
        self.assertEqual(out["probes_stopped"], "run_budget_exhausted")
        self.assertNotIn('"probe_found": 1', json.dumps(out))
        cell = out["portal"]["po_reference"]["against"]["salesOrders.externalDocumentNumber"]
        self.assertEqual(cell["probed"], 0)

    def test_counts_and_shapes_carry_no_value_and_count_exact_matches(self):
        from evals.oracle.cross_sample import portal_roots
        rows = [r.node.row for r in portal_roots(self.sources, "sales_order")]
        po = rows[0]["po_reference"]
        self.world.bc["salesOrders"] = [bc_record(
            "salesOrders", 0, 1, number="BC-ONLY-1", externalDocumentNumber=po)]
        out = ns.measure(self.sources, probes=len(rows))
        text = json.dumps(out)
        cell = out["portal"]["po_reference"]["against"]["salesOrders.externalDocumentNumber"]
        held = sum(1 for r in rows if r["po_reference"] == po)
        self.assertEqual((cell["in_bc_sample"], cell["probe_found"]), (held, held))
        self.assertEqual(out["portal"]["order_number"]["against"]["salesOrders.number"][
            "probe_found"], 0)
        self.assertEqual(out["portal_roots"], len(rows))
        values = {key_text(field_value(r, f)) for r in rows for f in ns.PORTAL_FIELDS}
        values |= {"BC-ONLY-1"}
        values.discard(None)
        self.assertEqual(sorted(v for v in values if len(v) >= 4 and v in text), [])

    def test_no_bc_says_so_and_probes_nothing(self):
        out = ns.measure(Sources(self.sources.portal, None), probes=5)
        self.assertEqual(out["bc_read"], {"all": "not_configured"})
        self.assertNotIn("probe_found", json.dumps(out))


if __name__ == "__main__":
    unittest.main()
