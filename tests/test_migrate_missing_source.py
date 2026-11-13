"""A migration build over a state directory that lacks a capture the served generation
read is refused (``missing_source:<family>``, SCHEMA-001): it would otherwise build a
valid, empty replacement of everything bound to that family (a unit layer), derive no
links from its documents (a step layer), or leave its documents unmarked (the text
re-mark). A family the served generation never read is not required, and is recorded."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ontology import pg_step_inplace as ip
from ontology import schema_migrate, step_migrate as sm
from ontology.schema import load_schema
from operating.host import state_capture_paths
from operating.pg import text_remark as tr

SCHEMA = load_schema()
FAMILIES = sorted(set(state_capture_paths("/x")))   # every capture family a state dir holds


def _bindings_of(family: str) -> list[str]:
    return [b["id"] for t in SCHEMA.object_types.values() for b in t.get("bindings", ())
            if b["source"] == family]


class StepSources(unittest.TestCase):
    def test_each_missing_family_is_refused_by_name(self):
        bound = sm.document_families({}, SCHEMA, every=True)
        self.assertEqual(bound, set(FAMILIES) & bound)   # never the Portal mirror
        for family in sorted(bound):
            held = {f: object() for f in bound if f != family}
            with self.subTest(family=family), \
                    self.assertRaisesRegex(ValueError, f"^missing_source:{family}$"):
                sm.require_state_sources({"mentions": None}, SCHEMA, held, bound)

    def test_a_family_never_served_is_not_required_and_is_reported(self):
        bound = sm.document_families({"mentions": None}, SCHEMA)
        never = sorted(bound)[0]
        held = {f: object() for f in bound if f != never}
        self.assertEqual(sm.require_state_sources({"mentions": None}, SCHEMA, held,
                                                  bound - {never}), [never])
        # unknown served set (Postgres records none): every family counts as served
        with self.assertRaisesRegex(ValueError, f"missing_source:{never}"):
            sm.require_state_sources({"mentions": None}, SCHEMA, held, None)

    def test_only_document_steps_and_their_types_need_sources(self):
        self.assertEqual(sm.document_families({"with_people": None, "roles": None}, SCHEMA),
                         set())
        self.assertEqual(sm.require_state_sources({"roles": None}, SCHEMA, {}, None), [])
        teams_types = [t for t, spec in SCHEMA.object_types.items()
                       if any(b["source"] == "teams" for b in spec.get("bindings", ()))]
        self.assertEqual(sm.document_families({"mentions": teams_types[:1]}, SCHEMA),
                         {"teams"})


class UnitLayerFromState(unittest.TestCase):
    def test_build_from_state_refuses_before_building(self):
        state = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(state, ignore_errors=True))
        for family in ("bc", "graph_mail", "sharepoint"):
            deps = _bindings_of(family)[:2]
            plan = {"from": "served", "deps": deps}
            held = {f: object() for f in FAMILIES if f != family}
            conn = mock.MagicMock()
            with self.subTest(family=family), \
                    mock.patch("psycopg.connect", return_value=conn), \
                    mock.patch("operating.pg.ontology_migrate.served_schema",
                               return_value="served"), \
                    mock.patch("operating.pg.ontology_migrate.delta_mark", return_value=1), \
                    mock.patch("operating.host.state_captures", return_value=held), \
                    mock.patch.object(schema_migrate, "served_families",
                                      return_value=set(FAMILIES)), \
                    mock.patch.object(schema_migrate, "build_layer",
                                      side_effect=AssertionError("built")):
                with self.assertRaisesRegex(ValueError, f"^missing_source:{family}$"):
                    schema_migrate.build_from_state(plan, SCHEMA, state, state / "out",
                                                    dsn="unused")


class TextRemark(unittest.TestCase):
    def test_the_re_mark_refuses_a_state_without_a_served_family(self):
        state = tempfile.mkdtemp()
        work = Path(tempfile.mkdtemp())
        with self.assertRaisesRegex(tr.Refused, "missing_source:graph_mail"):
            tr.judge(state, work, 2, 1, served={"graph_mail"})
        self.assertFalse((work / "shards").exists())       # refused before any shard
        with self.assertRaisesRegex(tr.Refused, "missing_source:teams"):
            tr.judge_others(state, work, served={"teams"})
        with mock.patch.dict(os.environ, {"PYTHONHASHSEED": "0"}), \
                self.assertRaisesRegex(ValueError, "missing_source:sharepoint"):
            ip.text_marks(state, work / "marks", 1, 1, served=["sharepoint"])
        self.assertFalse((work / "marks").exists())


if __name__ == "__main__":
    unittest.main()
