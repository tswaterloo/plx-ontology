"""BC line entity specs must not select cost fields the live tenant lacks."""
import unittest

from integrations.live_source_grammar import BC_QUERY_SPECS

# Live-reported cost fields per line entity: live run
# live-2026-10-01T05:28:46Z, `python3 -m integrations.bc_read probe --case-entities`
# (it reported select_allowlist_absent=["directUnitCost"] for purchaseInvoiceLines).
LIVE_COST_FIELDS = {
    "purchaseOrderLines": {"directUnitCost"},
    "purchaseReceiptLines": {"unitCost"},
    "purchaseInvoiceLines": {"unitCost"},
}
ALL_COST_NAMES = {"directUnitCost", "unitCost"}


class BcLineCostFieldTests(unittest.TestCase):
    def test_line_specs_select_only_live_cost_fields(self):
        for entity, live in LIVE_COST_FIELDS.items():
            selected = BC_QUERY_SPECS[entity].select & ALL_COST_NAMES
            self.assertEqual(selected, live, entity)

    def test_invoice_lines_do_not_select_direct_unit_cost(self):
        self.assertNotIn("directUnitCost", BC_QUERY_SPECS["purchaseInvoiceLines"].select)


if __name__ == "__main__":
    unittest.main()
