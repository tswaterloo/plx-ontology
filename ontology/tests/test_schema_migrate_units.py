"""``schema_migrate.diff`` reads each affected unit by its kind of rows: a gaps-only link
unit (a link binding no projector unit writes: a build step's link) holds no rows and
touches no type; a kind of rows it does not know is refused, never guessed."""

from __future__ import annotations

import copy
import unittest
from unittest import mock


def _schemas():
    """The served schema and one whose first gaps-only link unit's binding changed."""
    from ontology.project_incremental import plan
    from ontology.schema import load_schema
    old = load_schema()
    unit = sorted(u for u, rows in plan(old).rows.items() if rows == ("gaps",))[0]
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
