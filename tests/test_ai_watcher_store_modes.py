"""The watcher store opens when a sidecar (-wal/-shm) belongs to another user; the
watcher places what it judges and what a reader is shown (``Places``) in the served
SQLite generation, or, with Postgres the only store (``PLX_ONTOLOGY_PG=only``), in
Postgres alone: the generation's file is never opened, the reads match the SQLite
store's, the same-person link guard is on, and an excluded person's record is never
surfaced. Evidence label: isolated-fixture (synthetic rows; a throwaway Postgres)."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from typing import Any
import unittest
from unittest import mock

from operating import ai_watcher
from operating import ai_watcher as aw
from operating import truth_check as tc
from operating.tests.test_ai_watcher import NOW, Rows, row


class StoreModesTest(unittest.TestCase):
    def test_a_sidecar_owned_by_another_user_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            ai_watcher.open_store(directory).close()
            wal = directory / "watcher.sqlite-wal"
            wal.write_bytes(b"")
            os.chmod(wal, 0o660)
            attempted: list = []
            real_chmod, real_stat = os.chmod, os.stat

            def foreign_stat(path, *a, **k):
                result = real_stat(path, *a, **k)
                if str(path) == str(wal):
                    fields = list(result)
                    fields[4] = os.geteuid() + 1   # st_uid: someone else's file
                    return os.stat_result(fields)
                return result

            def chmod(path, mode, *a, **k):
                if str(path) == str(wal):
                    attempted.append(path)
                    raise PermissionError(1, "not owner")
                return real_chmod(path, mode, *a, **k)

            with mock.patch("operating.ai_watcher.os.stat", foreign_stat), \
                    mock.patch("operating.ai_watcher.os.chmod", chmod):
                conn = ai_watcher.open_store(directory)
                conn.close()
            self.assertEqual(attempted, [])   # never even tried to chmod the foreign file

    def test_our_own_files_are_narrowed_never_widened(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            ai_watcher.open_store(directory).close()
            db = directory / "watcher.sqlite"
            os.chmod(db, 0o664)
            ai_watcher.open_store(directory).close()
            self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o600)
            os.chmod(db, 0o600)
            ai_watcher.open_store(directory).close()
            self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o600)

    def test_a_setgid_state_dir_keeps_the_group_grant_but_drops_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            os.chmod(directory, 0o2770)
            ai_watcher.open_store(directory).close()
            db = directory / "watcher.sqlite"
            os.chmod(db, 0o664)
            ai_watcher.open_store(directory).close()
            self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o660)
            os.chmod(db, 0o640)
            ai_watcher.open_store(directory).close()
            self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o640)   # never widened


EXCLUDED_OID = "11111111-2222-3333-4444-555555555555"
KEPT_OID = "99999999-8888-7777-6666-555555555555"
HIDDEN = "api://public.fixture_docs/7@g7"   # not a message, no person in it: its unit names one


def _world(path: Path) -> Path:
    rows = Rows()
    rows.obj("SalesOrder:fx:9", "SalesOrder", row(9),
             {"status": ("text", "open"), "unit_cost": ("text", "41.45")},
             audience="api:a", value_markings={"unit_cost": '["finance"]'})
    rows.obj("PurchaseOrder:fx:1", "PurchaseOrder", row(1), {"status": ("text", "open")},
             audience="api:a", markings='["pii"]')
    # a record served under an excluded person's mailbox unit, cited by a handle that is
    # not a message (an attachment's field page): withheld by its audience, not its handle
    rows.obj("Attachment:fx:7", "Attachment", HIDDEN,
             {"name": ("text", "a.pdf")}, audience=f"mail:{EXCLUDED_OID}")
    rows.obj("Attachment:fx:8", "Attachment", "api://public.fixture_docs/8@g8",
             {"name": ("text", "b.pdf")}, audience=f"mail:{KEPT_OID}")
    return rows.build(path)


def _reads(places: Any) -> dict:
    return {
        "objects": [places.object_of(h) for h in (
            row(9), row(9) + "#page=2", row(1) + "#field=status", "api://nowhere@1")],
        "places": {o: places.place(o) for o in (
            "SalesOrder:fx:9", "PurchaseOrder:fx:1", "Attachment:fx:7", "Nothing:fx:0")},
        "markings": [places.markings_of(row(9), "SalesOrder:fx:9", ["row.unit_cost"]),
                     places.markings_of(row(9), "SalesOrder:fx:9", ["row.status"]),
                     places.markings_of(row(9), "SalesOrder:fx:9", None),
                     places.markings_of(row(1) + "@old", "PurchaseOrder:fx:1", None),
                     places.markings_of(row(1), "Nothing:fx:0", None)]}


def _judge(places: Any, groups: list, guard: Any = "guard") -> dict:
    item = {"title": "t", "subject": "", "text": "x", "why": "", "next_step": "",
            "departments": [], "priority": "medium", "claim_kind": "other",
            "gate": "passed", "claims": [], "groups": groups, "refused_claims": 0}
    src = tc.Sources(portal={}, bc=[], as_of=NOW.date())
    [judged] = aw.judge([item], places=places, inbox=Path("/nonexistent"),
                        sources=lambda objects: src, guard=guard,
                        excluded=lambda _g, text: EXCLUDED_OID in text)
    return judged


class PlacesStoreModesTest(unittest.TestCase):
    def test_sqlite_mode_reads_the_served_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": "1"}):
            served = _world(Path(tmp) / "projection" / "ontology.sqlite")
            places = aw.Places(served)
            try:
                reads = _reads(places)
                self.assertEqual(reads["objects"][:3], ["SalesOrder:fx:9", "SalesOrder:fx:9",
                                                        "PurchaseOrder:fx:1"])
                self.assertIsNone(reads["objects"][3])
                self.assertEqual(reads["places"]["PurchaseOrder:fx:1"], {
                    "object_type": "PurchaseOrder", "audience_key": "api:a",
                    "markings": '["pii"]'})
                self.assertIsNone(reads["places"]["Nothing:fx:0"])
                self.assertEqual(reads["markings"], ['["finance"]', "[]", '["finance"]',
                                                     '["pii"]', '["unreadable"]'])
            finally:
                places.close()

    def test_postgres_alone_never_opens_a_file_and_fails_closed_when_not_whole(self
                                                                               ) -> None:
        from operating.pg import ontology_reader
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": "only"}), \
                mock.patch.object(ontology_reader, "store_meta", return_value=None), \
                mock.patch.object(aw, "_ro", side_effect=AssertionError("file opened")):
            missing = Path(tmp) / "projection" / "ontology.sqlite"
            with self.assertRaises(ontology_reader.PostgresNotWhole):
                aw.Places(missing)
            with self.assertRaises(ontology_reader.PostgresNotWhole):
                aw._pointer(missing)

    def test_postgres_alone_reads_as_the_sqlite_generation_does(self) -> None:
        try:
            import contextlib, io
            import psycopg
            from operating.pg import ontology_load
            from operating.pg.migrate import MIGRATIONS_DIR
            from operating.tests import pg_testdb
            name, dsn = pg_testdb.create_db()
        except (ImportError, unittest.SkipTest) as exc:
            self.skipTest(f"no Postgres: {exc}")
        self.addCleanup(pg_testdb.drop_db, name)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        served = _world(Path(tmp.name) / "projection" / "ontology.sqlite")
        with psycopg.connect(dsn, autocommit=True) as pg:
            for migration in sorted(MIGRATIONS_DIR.glob("00*.sql")):
                if migration.name >= "0006":
                    pg.execute(migration.read_text(encoding="utf-8"))
        with contextlib.redirect_stderr(io.StringIO()):
            ontology_load.load(str(served), dsn, workers=1, log=io.StringIO())
        facts = tempfile.TemporaryDirectory()
        self.addCleanup(facts.cleanup)
        with mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": "1"}):
            places = aw.Places(served)
            try:
                expected = _reads(places)
                sqlite_judged = [_judge(places, g) for g in self.GROUPS]
            finally:
                places.close()
        # the generation's file is gone (as on the live host) and is never opened
        for path in served.parent.glob("ontology.sqlite*"):
            path.unlink()
        with mock.patch.dict(os.environ, {"PLX_ONTOLOGY_PG": "only", "PLX_PG_DSN": dsn,
                                          "PLX_PG_FACTS_DIR": facts.name}), \
                mock.patch.object(aw, "_ro", side_effect=AssertionError("file opened")):
            digest, layers = aw._pointer(served)
            self.assertTrue(digest.startswith("postgres:sha256:") or
                            digest.startswith("postgres:"), digest)
            self.assertEqual(layers, [])
            places = aw.Places(served)
            try:
                # the readers' own Postgres reader: the same-person link guard is on
                self.assertTrue(places._reader.conn.link_guard)
                self.assertEqual(_reads(places), expected)
                pg_judged = [_judge(places, g) for g in self.GROUPS]
            finally:
                places.close()
        for judged in (sqlite_judged, pg_judged):
            self.assertEqual([j["reason"] for j in judged], self.REASONS)
        self.assertEqual([j.get("markings") for j in pg_judged],
                         [j.get("markings") for j in sqlite_judged])

    GROUPS = [[[row(9)]], [[row(9)], [HIDDEN]],
              [["api://public.fixture_docs/8@g8"]]]
    # the excluded person's unit withholds the item even though no handle is a message
    REASONS = [None, "people_withheld", None]


class PersonAudienceTest(unittest.TestCase):
    def test_an_excluded_or_undecidable_person_unit_is_withheld(self) -> None:
        def excluded(_g, key):
            if "boom" in key:
                raise RuntimeError("guard cannot decide")
            return EXCLUDED_OID in key
        withheld = aw._person_audience_withheld
        self.assertTrue(withheld(f"mail:{EXCLUDED_OID}", object(), excluded))
        self.assertFalse(withheld(f"mail:{KEPT_OID}", object(), excluded))
        self.assertTrue(withheld("mail:boom", object(), excluded))     # fails closed
        self.assertTrue(withheld(None, object(), excluded))            # unknown shape
        self.assertTrue(withheld("", object(), excluded))
        # no people guard: every person family's unit is withheld, shared units are not
        for key in (f"mail:{KEPT_OID}", "mail-any:x", "teams:chat:19"):
            self.assertTrue(withheld(key, None, excluded), key)
        for key in ("api:a", "bc:production", "teams:channel:abc"):
            self.assertFalse(withheld(key, None, excluded), key)


if __name__ == "__main__":
    unittest.main()
