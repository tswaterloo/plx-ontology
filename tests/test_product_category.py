import unittest

from operating.pg.business_views import Cell, Rec, Row, Thing, fill_category


def _rec(binding, category):
    cells = {}
    if category is not None:
        cells["item_category"] = [Cell(category, None, None, "item_category", 0, "[]", "h:" + binding)]
    return Rec("o:" + binding, "Product", binding, "a", {"h:" + binding: "[]"}, set(), cells)


class CategoryPrecedence(unittest.TestCase):
    def read(self, *recs):
        row = Row()
        fill_category(row, Thing(list(recs)))
        return row.values["category"]

    def test_sage_item_master_beats_bc_even_when_bc_ranks_first(self):
        self.assertEqual(self.read(_rec("bc.items", "FG"), _rec("portal.sage_ic_item", "PLABEL")), "PLABEL")

    def test_bc_category_when_sage_states_none(self):
        self.assertEqual(self.read(_rec("bc.items", "FG"), _rec("portal.sage_ic_item", "  ")), "FG")

    def test_blank_sage_and_no_other_copy_is_unstated(self):
        self.assertIsNone(self.read(_rec("portal.sage_ic_item", None), _rec("portal.app_product", None)))


if __name__ == "__main__":
    unittest.main()
