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


if __name__ == "__main__":
    unittest.main()
