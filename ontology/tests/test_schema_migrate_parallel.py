"""A unit migration layer (``schema_migrate.build_layer``) whose captured documents are
parsed by worker processes (``ontology.unit_layer_workers``) equals the serial one:
every row of every table in its order, its content digest, and what the build keeps
(the fields, readings and member readings: same keys, same values, same use marks).
A worker that dies leaves its documents to the build's own process; a worker count that
is not a positive integer is refused, and the derivation code (the kept fields' key) is
the one ``ontology.schema_migrate`` builds under."""

from __future__ import annotations

from contextlib import ExitStack, closing
import contextlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ontology.tests.test_mail_documents import _die

_TABLES = ("objects", "object_versions", "object_removals", "identifiers", "events",
           "event_objects", "value_cells", "link_rows")


def _rows(path: Path) -> dict[str, list[tuple]]:
    with closing(sqlite3.connect(path)) as db:
        out = {t: db.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in _TABLES}
        out["schema_migration"] = sorted(
            db.execute("SELECT key, value FROM schema_migration "
                       "WHERE key NOT IN ('built_at')").fetchall())
    return out


def _kept(projection: Path) -> dict[str, list[tuple]]:
    out = {}
    for name, table in (("fields.sqlite", "fields"), ("readings.sqlite", "readings"),
                        ("member-readings.sqlite", "members")):
        with closing(sqlite3.connect(projection / name)) as db:
            out[name] = sorted(db.execute(f"SELECT key, code, value, used FROM {table}"))
    return out


class LayerDocumentWorkersTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        from ontology.project_incremental import plan
        from ontology.schema import load_schema
        from ontology.schema_migrate import diff
        cls.schema = load_schema()
        units = plan(cls.schema)
        cls.units = sorted(u for u, rows in units.rows.items()
                           if rows and rows[0] == "binding"
                           and units.bindings[rows[1]][1]["source"] == "graph_mail"
                           and units.bindings[rows[1]][1]["relation"] == "documentFields")
        cls.migration = diff(cls.schema, cls.schema, force=cls.units, present=[])

    def build(self, root: Path, name: str, workers: int) -> tuple[dict, Path]:
        from integrations import captured_text
        from integrations.captured_text import kept_readings
        from integrations.kept_members import kept_members
        from ontology import document_fields, project
        from ontology.document_fields import kept_fields
        from ontology.project import MailCaptures
        from ontology.project_incremental import FIELDS, MEMBERS, READINGS
        from ontology.schema_migrate import build_layer
        from ontology.unit_layer_workers import document_workers
        from integrations.tests.test_capture_mail import SCOPE

        projection = root / name / "projection"
        projection.mkdir(parents=True)
        out = root / name / "layer.sqlite"
        with ExitStack() as stack:
            for module, memo in ((captured_text, "_CACHE"),
                                 (captured_text, "_MEMBER_READINGS"),
                                 (document_fields, "_FIELDS")):
                stack.enter_context(mock.patch.object(module, memo,
                                                      getattr(module, memo).empty()))
            mail = stack.enter_context(MailCaptures(
                root / "mail" / "capture", internal_domains=SCOPE.internal_domains))
            stack.enter_context(kept_readings(projection / READINGS))
            stack.enter_context(kept_members(projection / MEMBERS, [mail.blobs]))
            stack.enter_context(kept_fields(projection / FIELDS, project.derivation_code()))
            stack.enter_context(document_workers(workers))
            report = build_layer(self.migration, self.schema, {"graph_mail": mail}, out,
                                 sequence=7)
        return report, projection

    def test_parallel_document_parsing_writes_the_serial_layer(self):
        from ontology import project_parallel as par
        from ontology.tests.test_mail_documents import captured_mail_with_documents

        self.assertGreaterEqual(len(self.units), 2)
        self.assertEqual(self.migration["refused"], [])
        mail = captured_mail_with_documents(self)
        pools = []
        held = par.document_pool

        @contextlib.contextmanager
        def watched(count):
            with held(count) as pool:
                pools.append(pool)
                yield pool

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mail").symlink_to(mail)
            serial, serial_kept = self.build(root, "serial", 1)
            expected = _rows(Path(serial["store"]))
            self.assertTrue(expected["objects"])         # the documents' own objects
            self.assertTrue(_kept(serial_kept)["fields.sqlite"])
            small = mock.patch.multiple(par, MIN_PARSE_RECORDS=1, PARSE_CHUNK=1)
            with small, mock.patch.object(par, "document_pool", watched):
                parallel, parallel_kept = self.build(root, "parallel", 3)
                pool = pools[-1]
                self.assertGreater(pool.tasks, 1)
                self.assertEqual((pool.fallback, pool.broken), (0, False))
                with mock.patch.object(par, "_parse", _die):
                    died, died_kept = self.build(root, "died", 3)
                self.assertTrue(pools[-1].broken)
            self.assertIsNone(par.DOCUMENTS)             # the pool is closed with the layer
            for report, kept in ((parallel, parallel_kept), (died, died_kept)):
                self.assertEqual(report["id"], serial["id"])
                self.assertEqual(report["counts"], serial["counts"])
                self.assertEqual(_rows(Path(report["store"])), expected)
                self.assertEqual(_kept(kept), _kept(serial_kept))

    def test_the_build_command_runs_with_engaged_workers_or_refuses_a_bad_count(self):
        from ontology import project_parallel as par
        from ontology import schema_migrate
        from ontology.project import ProjectionError
        from ontology import unit_layer_workers

        seen = []

        def build(argv):
            seen.append((argv, par.DOCUMENTS))
            return 0

        with mock.patch.object(schema_migrate, "main", build), \
                mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "4"}):
            self.assertEqual(unit_layer_workers.main(["build", "--x"]), 0)
        (argv, pool), = seen
        self.assertEqual(argv, ["build", "--x"])
        self.assertEqual((pool.count, pool.engaged), (4, True))
        self.assertIsNone(par.DOCUMENTS)
        for raw in ("0", "-3", "many"):
            with mock.patch.object(schema_migrate, "main", build), \
                    mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": raw}), \
                    self.assertRaises(ProjectionError):
                unit_layer_workers.main(["build"])
        self.assertEqual(len(seen), 1)    # refused before anything is read
        with mock.patch.dict(os.environ, {"PLX_BUILD_WORKERS": "1"}), \
                unit_layer_workers.document_workers() as serial:
            self.assertIsNone(serial)
            self.assertIsNone(par.DOCUMENTS)

    def test_the_kept_fields_key_is_the_one_the_layer_builds_under(self):
        """No module the derivation code covers imports the workers' entry point, so
        running the layer through it keeps every field a build kept before."""
        from ontology.project import derivation_modules
        self.assertIn("ontology.schema_migrate", derivation_modules())
        self.assertNotIn("ontology.unit_layer_workers", derivation_modules())

if __name__ == "__main__":
    unittest.main()
