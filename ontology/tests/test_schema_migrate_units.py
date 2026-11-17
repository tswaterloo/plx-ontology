"""``schema_migrate.diff`` reads each affected unit by its kind of rows: a gaps-only link
unit (a link binding no projector unit writes: a build step's link) holds no rows and
touches no type; a kind of rows it does not know is refused, never guessed."""

from __future__ import annotations

import copy
import unittest
from unittest import mock


_ENDS = ("from_binding", "to_binding", "via")


def _gaps_units(schema, *, naming_ends):
    """The gaps-only link units of ``schema`` whose binding names (or names no) binding of
    its ends: a key join's (``from_binding``/``to_binding``) is part of each end binding's
    unit key, so changing it changes those units too."""
    from ontology.project_incremental import plan
    out = []
    for unit in sorted(u for u, rows in plan(schema).rows.items() if rows == ("gaps",)):
        lid, index = unit[2:].rsplit("#", 1)
        binding = schema.link_types[lid]["bindings"][int(index)]
        if any(b.get("written_by_step") for b in schema.link_types[lid]["bindings"]):
            continue   # a step's own link type: its change re-runs that step's input types
        if any(binding.get(end) for end in _ENDS) == naming_ends:
            out.append(unit)
    return out


def _schemas(*, naming_ends=False):
    """The served schema and one whose first gaps-only link unit's binding changed (by
    default one naming no end binding: a build step's link no binding's unit holds)."""
    from ontology.schema import load_schema
    old = load_schema()
    unit = _gaps_units(old, naming_ends=naming_ends)[0]
    lid, index = unit[2:].rsplit("#", 1)
    new = copy.deepcopy(old)
    binding = new.link_types[lid]["bindings"][int(index)]
    binding["description"] = (binding.get("description") or "") + " (changed)"
    binding["tier_ceiling"] = "lead" if binding.get("tier_ceiling") != "lead" else "verified"
    return old, new, unit


class GapsUnitTests(unittest.TestCase):

    def test_a_gaps_only_link_unit_change_diffs_without_touching_a_type(self):
        from ontology.schema_migrate import diff
        old, new, unit = _schemas()
        out = diff(old, new, present=[])
        self.assertIn(unit, out["units"]["affected"])
        self.assertEqual(out["touched_types"], [])
        self.assertEqual(out["touched_links"], [])
        self.assertFalse([r for r in out["refused"] if r.startswith("unit_rows_unknown")])
        self.assertEqual(out["delete"]["links"], [])

    def test_a_key_join_change_touches_the_types_of_its_ends(self):
        """A gaps-only key-join unit names its ends' bindings: its change re-derives
        their units, so their types are touched, never left out of the steps to re-run."""
        from ontology.schema_migrate import diff
        old, new, unit = _schemas(naming_ends=True)
        lid, index = unit[2:].rsplit("#", 1)
        binding = old.link_types[lid]["bindings"][int(index)]
        ends = {old.link_types[lid]["from"], old.link_types[lid]["to"]}
        out = diff(old, new, present=[])
        self.assertIn(unit, out["units"]["affected"])
        for end in ("from_binding", "to_binding"):
            if binding.get(end):
                self.assertIn(f"b:{binding[end]}", out["units"]["affected"])
        self.assertLessEqual(ends, set(out["touched_types"]))
        self.assertFalse([r for r in out["refused"] if r.startswith("unit_rows_unknown")])

    def test_an_unknown_kind_of_unit_rows_is_refused(self):
        from ontology import project_incremental
        from ontology.schema_migrate import diff
        old, new, unit = _schemas()
        real = project_incremental.plan

        def odd(schema):
            got = real(schema)
            got.rows.update({u: ("mystery",) for u, r in got.rows.items() if r == ("gaps",)})
            return got
        with mock.patch.object(project_incremental, "plan", odd):
            out = diff(old, new, present=[])
        self.assertTrue([r for r in out["refused"] if r.startswith("unit_rows_unknown:")],
                        out["refused"])


class NewIdentifierKindTests(unittest.TestCase):
    """IN-10: declaring a property an identifier of a kind never read from text changes
    only its own units' rows, so the unit migration re-derives them; any other identifier
    kind change on a property stays refused."""

    def _pair(self, kind):
        from ontology.schema import load_schema
        new = copy.deepcopy(load_schema())
        old = copy.deepcopy(new)
        prop = next(p for p in old.object_types["RelSageArOpenDocument"]["properties"]
                    if p["name"] == "idinvc")
        prop.pop("identifier_kind")
        prop["value_type"] = "text"
        if kind:
            changed = next(p for p in new.object_types["RelSageArOpenDocument"]["properties"]
                           if p["name"] == "idinvc")
            changed["identifier_kind"] = kind
        return old, new

    def test_a_kind_never_read_from_text_is_declared_in_place(self):
        from ontology.schema_migrate import diff
        out = diff(*self._pair(None), present=[])
        self.assertIn("b:gen.sage.ar_open_document", out["units"]["affected"])
        self.assertFalse([r for r in out["refused"] if r.startswith("identifier_kind")])

    def test_a_kind_mentions_read_stays_refused(self):
        from ontology.schema_migrate import diff
        out = diff(*self._pair("so_number"), present=[])
        self.assertIn("identifier_kind_changes:RelSageArOpenDocument.idinvc", out["refused"])


class RowsSliceTests(unittest.TestCase):
    """``diff`` rewrites a unit only when what its rows are a function of moved
    (``build_keys.rows_slice``); the projection's side of it is proved against a full build
    in ``operating.tests.test_pg_schema_migrate``."""

    def _domain(self):
        from ontology.tests.test_projection import DOMAIN
        return copy.deepcopy(DOMAIN)

    def _diff(self, old, new):
        from ontology.schema_migrate import diff
        from ontology.tests.test_projection import compiled_schema
        return diff(compiled_schema(old), compiled_schema(new), present=[])

    def test_documentation_alone_rewrites_nothing(self):
        old = self._domain()
        new = copy.deepcopy(old)
        for t in new["object_types"]:
            t["description"] = "reworded"
            for p in t["properties"]:
                p["description"] = "reworded"
        for link in new["link_types"]:
            link["description"] = "reworded"
        out = self._diff(old, new)
        self.assertEqual(out["units"]["affected"], [])
        self.assertTrue(out["units"]["same_rows"])

    def test_a_same_record_unit_is_rewritten_with_its_ends(self):
        # a property of PurchaseOrder: the same_record unit compares its values, so it
        # and both of its ends' units are rewritten; the text_match unit of the same ends
        # compares no value and reads no property of either: kept
        old = self._domain()
        new = copy.deepcopy(old)
        po = next(t for t in new["object_types"] if t["id"] == "PurchaseOrder")
        po["properties"].append({"name": "po_probe", "value_type": "text", "sources": [
            {"binding": "t.fm_po", "path": "ponumber", "parser": "trim"}]})
        out = self._diff(old, new)
        affected = out["units"]["affected"]
        self.assertIn("l:PurchaseOrder.same_record_as|t.gold_po|t.fm_po|same_record", affected)
        self.assertIn("b:t.fm_po", affected)
        self.assertIn("b:t.gold_po", affected)   # unchanged rows, read by the comparison
        self.assertIn("l:PurchaseOrder.same_record_as|t.gold_po|t.fm_po|text_match",
                      out["units"]["same_rows"])

    def test_an_end_bindings_lineage_rewrites_its_links(self):
        # a lineage rule on an end binding: every link citing its versions is rewritten
        old = self._domain()
        new = copy.deepcopy(old)
        po = next(t for t in new["object_types"] if t["id"] == "PurchaseOrder")
        gold = next(b for b in po["bindings"] if b["id"] == "t.gold_po")
        rules = gold.setdefault("lineage", {}).setdefault("rules", [])
        rules.insert(0, {"when": [{"path": "po_number", "op": "prefix", "value": "ZZ-"}],
                         "lineage": "gold_etl_from_sage",
                         "evidence": [{"kind": "ref", "id": "fixture"}]})
        out = self._diff(old, new)
        for unit in ("l:PurchaseOrderLine.of_order|t.gold_line|t.gold_po|fk",
                     "l:PurchaseOrder.ordered_from|t.gold_po|t.supplier|fk"):
            self.assertIn(unit, out["units"]["affected"])


if __name__ == "__main__":
    unittest.main()
