"""The payroll re-mark only ever adds finance and pii to the flagged documents' text fields:
no text, chunk or store_meta row changes; a rerun writes nothing; undo restores; an
incomplete or stale judge is refused (scratch Postgres database)."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from operating.pg import text_remark as tr

try:
    from operating.pg import scratch_db
    import psycopg
    _NAME, _DSN = scratch_db.create_db()
except Exception:  # noqa: BLE001 - no local Postgres: skipped
    _NAME = _DSN = None


def tearDownModule() -> None:
    if _NAME:
        scratch_db.drop_db(_NAME)


@unittest.skipIf(_DSN is None, "no scratch Postgres")
class TextRemarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pg = psycopg.connect(_DSN, autocommit=True)
        self.pg.execute("DROP SCHEMA IF EXISTS ontology CASCADE; CREATE SCHEMA ontology")
        self.pg.execute(
            "CREATE TABLE ontology.text_fields (field_id bigint PRIMARY KEY, object_id text, "
            "version_handle text NOT NULL, citation text, field_path text, document_kind text,"
            " sender_class text, markings text NOT NULL, text_digest text NOT NULL);"
            "CREATE TABLE ontology.text_chunks (chunk_id bigint PRIMARY KEY, field_id bigint, "
            "start integer, body bytea);"
            "CREATE TABLE ontology.store_meta (key text PRIMARY KEY, value text)")
        rows = [(1, "pay-1", "[]"), (2, "pay-1", '["finance"]'), (3, "pay-2", '["finance","pii"]'),
                (4, "memo-1", "[]")] + [(10 + i, f"pay-n{i % 7}", "[]") for i in range(60)]
        with self.pg.cursor() as cur:
            cur.executemany("INSERT INTO ontology.text_fields VALUES (%s, 'X:1', %s, 'c', 'row.body',"
                            " 'k', 's', %s, 'd')", rows)
            cur.executemany("INSERT INTO ontology.text_chunks VALUES (%s, %s, 0, 'x')",
                            [(r[0], r[0]) for r in rows])
        self.pg.execute("INSERT INTO ontology.store_meta VALUES ('steps_digest', 'steps:old')")
        self.work = Path(tempfile.mkdtemp())
        (self.work / "shards").mkdir()
        (self.work / "judge.json").write_text(json.dumps(
            {"code": tr._code(), "shards": 2, "state": "s"}))
        marked = ["pay-1", "pay-2"] + [f"pay-n{i}" for i in range(7)]
        for s in (0, 1):
            (self.work / "shards" / f"shard-{s}.json").write_text(json.dumps(
                {"shard": s, "shards": 2, "rule": tr.RULE, "keys": "k", "seconds": 1,
                 "counts": {"groups_listed": 4, "groups": 2, "documents": 5},
                 "marked": marked[s::2]}))

    def tearDown(self) -> None:
        self.pg.close()

    def _state(self) -> tuple:
        return (self.pg.execute("SELECT field_id, markings FROM ontology.text_fields ORDER BY 1")
                .fetchall(),
                self.pg.execute("SELECT * FROM ontology.text_chunks ORDER BY 1").fetchall(),
                self.pg.execute("SELECT * FROM ontology.store_meta").fetchall(),
                self.pg.execute("SELECT version_handle, citation, text_digest FROM "
                                "ontology.text_fields ORDER BY field_id").fetchall())

    def test_marks_only_added_rerun_writes_nothing_and_undo_restores(self) -> None:
        before = self._state()
        dry = tr.apply(self.work, _DSN, dry_run=True)
        self.assertEqual((dry["documents_marked"], dry["fields_of_marked"],
                          dry["fields_to_mark"]), (9, 63, 62))
        self.assertEqual(self._state(), before)
        got = tr.apply(self.work, _DSN, dry_run=False)
        self.assertEqual(got["fields_marked"], 62)
        after = self._state()
        marks = dict(after[0])
        self.assertEqual(marks[4], "[]")                      # not flagged: untouched
        self.assertTrue(all(marks[f] == '["finance","pii"]' for f in marks if f != 4))
        self.assertEqual(after[1:], before[1:])               # chunks, store_meta, text
        self.assertEqual(tr.apply(self.work, _DSN, dry_run=False)["fields_marked"], 0)
        checked = tr.check(self.work, _DSN)
        self.assertEqual((checked["applied"], checked["fields"], checked["marked"]),
                         (True, 62, 62))
        self.assertEqual(tr.main(["check", "--work", str(self.work), "--dsn", _DSN]), 0)
        self.assertEqual(tr.undo(self.work, _DSN)["restored"], 62)
        self.assertEqual(self._state(), before)               # the whole round trip
        # undone: the fields lack the marks again, said so (exit 1)
        undone = tr.check(self.work, _DSN)
        self.assertEqual((undone["applied"], undone["reason"]),
                         (False, "fields_without_the_marks"))
        self.assertEqual(tr.main(["check", "--work", str(self.work), "--dsn", _DSN]), 1)
        # applied again; a layer then removed one field and re-marked another with more:
        # still applied
        self.assertEqual(tr.apply(self.work, _DSN, dry_run=False)["fields_marked"], 62)
        self.pg.execute("DELETE FROM ontology.text_fields WHERE field_id = 10")
        self.pg.execute("UPDATE ontology.text_fields SET markings = "
                        "'[\"finance\",\"hr\",\"pii\"]' WHERE field_id = 11")
        checked = tr.check(self.work, _DSN)
        self.assertEqual((checked["applied"], checked["gone"]), (True, 1))
        self.pg.execute("UPDATE ontology.text_fields SET markings = '[\"hr\"]' "
                        "WHERE field_id = 11")
        self.assertFalse(tr.check(self.work, _DSN)["applied"])   # a field lost its marks
        # every logged field gone: nothing is said to be applied
        self.pg.execute("DELETE FROM ontology.text_fields WHERE field_id <> 4")
        gone = tr.check(self.work, _DSN)
        self.assertEqual((gone["applied"], gone["reason"]), (False, "every_field_gone"))

    def test_check_without_an_apply_or_with_a_bad_log_is_not_applied(self) -> None:
        self.assertEqual(tr.check(self.work, _DSN)["reason"], "no_apply_recorded")
        (self.work / "undo.jsonl").write_text("")
        self.assertEqual(tr.check(self.work, _DSN)["reason"], "no_apply_recorded")
        (self.work / "undo.jsonl").write_text("not json\n")
        self.assertEqual(tr.check(self.work, _DSN)["reason"], "undo_log_unreadable")
        (self.work / "undo.jsonl").write_text('{"field_id": 1, "old": "[]", "new": "x"}\n')
        self.assertEqual(tr.main(["check", "--work", str(self.work), "--dsn",
                                  "postgresql:///plx_no_such_database_x"]), 1)

    def test_an_incomplete_or_stale_judge_is_refused(self) -> None:
        (self.work / "shards" / "shard-1.json").unlink()
        with self.assertRaisesRegex(tr.Refused, "judge_incomplete"):
            tr.apply(self.work, _DSN, dry_run=True)
        (self.work / "judge.json").write_text(json.dumps({"code": "x", "shards": 2,
                                                          "state": "s"}))
        with self.assertRaisesRegex(tr.Refused, "code_changed"):
            tr.apply(self.work, _DSN, dry_run=True)


class ShardedListingTests(unittest.TestCase):
    """The judge's shards list the mail store's latest rows each with only its own handles
    (no whole-table sort per shard): over all shards every group the source lists is
    judged exactly once, with the source's own key and documents, at N=1/10/60."""

    def _mail(self, n: int) -> Path:
        import os
        import shutil
        from integrations import graph_read
        from integrations.capture_mail import MailCapture, MailCaptureStore
        from integrations.graph_read import GraphMailReader
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, SCOPE,
                                                           Clock, FakeGraph,
                                                           capture_audience)
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        graph = FakeGraph()
        for i in range(n):
            graph.put(BOX_A if i % 2 else BOX_B, f"MSG-{i}", f"CK-{i}", subject=f"Note {i}",
                      body=f"Payslip for period {i}" if i % 3 == 0 else f"Freight {i}")
        store = MailCaptureStore(root, clock=Clock())
        self.addCleanup(store.close)
        api, _ = graph_read.build_graph(ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(store, GraphMailReader(api, capture_audience()), CAPTURE,
                          sleep=lambda s: None).run(SCOPE)
        self.assertTrue(run["complete"], run)
        self.scope = SCOPE
        return root

    def _groups(self, root: Path, shard: int | None, shards: int) -> tuple[list, tuple]:
        from types import SimpleNamespace
        from ontology.mentions import source_document_groups
        from ontology.project import MailCaptures
        with MailCaptures(root, internal_domains=self.scope.internal_domains) as mail:
            projector = SimpleNamespace(sources={"graph_mail": mail})
            told: tuple = ()
            if shard is not None:
                sharded, digest = tr._shard_sources(projector, shard, shards)
                told = (list(sharded.values()), digest)
            got = [(key, [d.citation for d in load()[0]])
                   for key, load in source_document_groups(projector)]
        return got, told

    def test_every_group_once_over_all_shards(self) -> None:
        for n in (1, 10, 60):
            with self.subTest(n=n):
                root = self._mail(n)
                whole, _ = self._groups(root, None, 1)
                self.assertEqual(len(whole), n)
                parts, digests, mine = [], set(), 0
                for shard in range(4):
                    got, (counts, digest) = self._groups(root, shard, 4)
                    ((total, own),) = counts
                    self.assertEqual((total, own), (n, len(got)))
                    parts.extend(got)
                    digests.add(digest)
                    mine += own
                self.assertEqual(mine, n)
                self.assertEqual(len(digests), 1)            # every shard saw one listing
                self.assertEqual(sorted(parts), sorted(whole))   # none lost, none twice

    def test_a_shard_already_judged_is_not_judged_again(self) -> None:
        out = Path(tempfile.mkdtemp())
        held = {"shard": 1, "shards": 2, "keys": "k", "seconds": 3, "marked": ["x"],
                "counts": {"groups_listed": 2, "groups": 1, "documents": 4}}
        (out / "shard-1.json").write_text(json.dumps(held))
        got = tr.judge_shard("/nonexistent-state", 1, 2, str(out))   # no source opened
        self.assertEqual(got, {k: v for k, v in held.items() if k != "marked"})

    def test_a_listing_of_another_shape_is_refused(self) -> None:
        from types import SimpleNamespace

        class Odd:
            _LATEST = "SELECT m.* FROM messages m"
            _conn = object()
            sample = None

            def document_groups(self):
                return iter(())
        with self.assertRaisesRegex(tr.Refused, "listing_not_shardable:odd"):
            tr._shard_sources(SimpleNamespace(sources={"odd": Odd()}), 0, 2)


    def test_a_shard_in_progress_is_claimed_once(self) -> None:
        import multiprocessing as mp
        import os
        from operating.host import process_token
        out = Path(tempfile.mkdtemp())
        path = out / "shard-0.json"
        for n in (1, 10):
            with self.subTest(judges=n), mp.get_context("fork").Pool(n) as pool:
                won = pool.map(tr._claim, [path] * n)
                self.assertEqual(sum(won), 1)          # one judge per shard, never two
                (out / "shard-0.json.lock").unlink()
        # held by a running judge (this one): another judge leaves it, opening nothing
        lock = out / "shard-0.json.lock"
        lock.write_text(json.dumps({"pid": os.getpid(), "token": process_token(os.getpid())}))
        self.assertEqual(tr.judge_shard("/nonexistent-state", 0, 2, str(out)),
                         {"shard": 0, "shards": 2, "held_by_other_judge": True})
        self.assertTrue(lock.exists())
        # held by a judge that stopped: taken over
        lock.write_text(json.dumps({"pid": os.getpid(), "token": "gone"}))
        self.assertTrue(tr._claim(path))
        self.assertEqual(json.loads(lock.read_text())["token"], process_token(os.getpid()))
        self.assertEqual([p.name for p in out.iterdir()], ["shard-0.json.lock"])


class _Load:
    def __init__(self, method: str, source: object, handle: str, documents: list) -> None:
        self.method, self.source, self.args, self.documents = \
            method, source, ({"l1_handle": handle},), documents

    def __call__(self) -> tuple[list, bool]:
        return self.documents, True


class _Listed:
    """A capture source with a whole-table listing (mail ``messages``, SharePoint
    ``items``), and groups under a key outside it (SharePoint lists)."""
    _LATEST = ("SELECT l1_handle FROM {t} WHERE rowid IN (SELECT max(rowid) "
               "FROM {t} GROUP BY l1_handle)")
    sample = None

    def __init__(self, name: str, table: str, method: str, n: int, keyed: int) -> None:
        import sqlite3
        self.name, self.method, self.keyed = name, method, keyed
        type(self)._LATEST = _Listed._LATEST.format(t=table)
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(f"CREATE TABLE {table} (l1_handle TEXT)")
        self._conn.executemany(f"INSERT INTO {table} VALUES (?)",
                               [(f"H{i}",) for i in range(n)] * 2)

    def document_groups(self):
        from types import SimpleNamespace as Doc
        for (handle,) in self._conn.execute(self._LATEST + " ORDER BY l1_handle"):
            yield (self.name, handle), _Load(self.method, self, handle,
                                             [Doc(citation=f"{self.name}:{handle}")])
        for i in range(self.keyed):
            yield ("list", self.name, i), (
                lambda i=i: ([Doc(citation=f"{self.name}:list{i}:{j}") for j in range(2)],
                             True))


class _Plain:
    """A source without groups: each document its own group with no key."""
    def __init__(self, name: str, n: int) -> None:
        self.name, self.n = name, n

    def documents(self):
        from types import SimpleNamespace as Doc
        return iter([Doc(citation=f"{self.name}:{i}") for i in range(self.n)])


def _projector(n: int):
    from types import SimpleNamespace
    mail = type("Mail", (_Listed,), {})("a_mail", "messages", "_message_documents", n, 0)
    files = type("Files", (_Listed,), {})("c_sp", "items", "_file_documents", n, 3)
    return SimpleNamespace(sources={"a_mail": mail, "b_meetings": _Plain("b_meetings", n),
                                    "c_sp": files, "d_teams": _Plain("d_teams", 2 * n)})


def _old_rule_shard(projector, shard: int, shards: int, sensitive) -> dict:
    """A shard as the judge before ``RULE`` judged it: a group of no key in the shard of
    its ordinal among all the groups this shard listed."""
    import hashlib
    from ontology.mentions import source_document_groups
    sharded, listed = tr._shard_sources(projector, shard, shards)
    keys = hashlib.sha256(listed.encode("utf-8"))
    counts = {"groups_listed": sum(t for t, _m in sharded.values()), "groups": 0,
              "documents": 0, "failed_groups": 0}
    marked = set()
    for index, (key, load) in enumerate(source_document_groups(projector)):
        if getattr(load, "method", None) not in tr.SHARDED_LOADS:
            counts["groups_listed"] += 1
            keys.update(repr(key).encode("utf-8") if key is not None else b"\x00")
            if tr._key_hash(key, index) % shards != shard:
                continue
        counts["groups"] += 1
        documents, _ = load()
        counts["documents"] += len(documents)
        marked |= {d.citation for d in documents if sensitive(d)}
    return {"shard": shard, "shards": shards, "keys": keys.hexdigest(), "counts": counts,
            "marked": sorted(marked), "seconds": 1}


def _sensitive(document) -> bool:
    return document.citation.endswith(("1", "4"))


class GroupOfNoKeyTests(unittest.TestCase):
    """A group of no key (a meeting, Teams or Mission Control document) is judged in
    exactly one shard though its source sorts after mail and before or after SharePoint,
    whose groups each shard lists only its own of: none lost, none twice (N=1/10/60).
    Shard files judged before that rule are repaired by judging those groups once."""

    def _every(self, n: int) -> list[str]:
        from ontology.mentions import source_document_groups
        return sorted(d.citation for _k, load in source_document_groups(_projector(n))
                      for d in load()[0])

    def test_each_group_is_judged_in_exactly_one_shard(self) -> None:
        for n in (1, 10, 60):
            for shards in (4, 16):
                with self.subTest(n=n, shards=shards):
                    judged, digests, groups, listed = [], set(), 0, set()

                    def sensitive(document) -> bool:
                        judged.append(document.citation)
                        return _sensitive(document)
                    for shard in range(shards):
                        got = tr.judge_groups(_projector(n), shard, shards, sensitive)
                        self.assertEqual(got["rule"], tr.RULE)
                        digests.add(got["keys"])
                        listed.add(got["counts"]["groups_listed"])
                        groups += got["counts"]["groups"]
                    self.assertEqual(sorted(judged), self._every(n))   # each once
                    self.assertEqual((len(digests), listed), (1, {groups}))

    def _old_work(self, n: int, shards: int) -> Path:
        work = Path(tempfile.mkdtemp())
        (work / "shards").mkdir()
        (work / "judge.json").write_text(json.dumps(
            {"code": tr._code(), "shards": shards, "state": "s"}))
        for shard in range(shards):
            (work / "shards" / f"shard-{shard}.json").write_text(json.dumps(
                _old_rule_shard(_projector(n), shard, shards, _sensitive)))
        return work

    def _others(self, work: Path, n: int, shards: int, **change) -> None:
        got = tr.judge_other_groups(_projector(n), shards, _sensitive)
        got.update(code=tr._code(), seconds=1, **change)
        (work / "others.json").write_text(json.dumps(got))

    def test_old_rule_shards_are_repaired_by_judging_the_others_once(self) -> None:
        broken = 0
        for n in (1, 10, 60):
            shards = 16
            with self.subTest(n=n):
                work = self._old_work(n, shards)
                every = self._every(n)
                held = [json.loads(p.read_text()) for p in (work / "shards").iterdir()]
                broken += sum(s["counts"]["groups"] for s in held) != \
                    held[0]["counts"]["groups_listed"]
                with self.assertRaisesRegex(tr.Refused, "without_others"):
                    tr.handles(work)
                self._others(work, n, shards)
                marked, counts = tr.handles(work)
                self.assertEqual(marked, [c for c in every if c.endswith(("1", "4"))])
                self.assertEqual((counts["groups"], counts["documents"]),
                                 (counts["groups_listed"], len(every)))
                self.assertEqual(counts["groups_rejudged"], 3 * n + 3)
        self.assertGreater(broken, 0)          # the old rule did lose or repeat groups

    def test_inconsistent_repair_input_is_refused(self) -> None:
        n, shards = 10, 4
        work = self._old_work(n, shards)
        self._others(work, n, shards, keys="other listing")
        with self.assertRaisesRegex(tr.Refused, "others_of_other_judge"):
            tr.handles(work)
        good = tr.judge_other_groups(_projector(n), shards, _sensitive)
        old = good["old_rule"]
        moved = {"groups": [old["groups"][0] + 1] + old["groups"][1:],
                 "documents": old["documents"]}
        self._others(work, n, shards, old_rule=moved)
        with self.assertRaisesRegex(tr.Refused, "do_not_account_for_shard:0"):
            tr.handles(work)
        self._others(work, n, shards, sharded=good["sharded"][1:])
        with self.assertRaisesRegex(tr.Refused, "others_incomplete"):
            tr.handles(work)
        self._others(work, n, shards, counts=dict(good["counts"], failed_groups=1))
        with self.assertRaisesRegex(tr.Refused, "others_incomplete"):
            tr.handles(work)
        self._others(work, n, shards)
        tr.handles(work)
        first = work / "shards" / "shard-0.json"
        fixed = tr.judge_groups(_projector(n), 0, shards, _sensitive)
        first.write_text(json.dumps(dict(fixed, seconds=1)))
        with self.assertRaisesRegex(tr.Refused, "shard_rules_differ"):
            tr.handles(work)
        for shard in range(1, shards):
            (work / "shards" / f"shard-{shard}.json").write_text(json.dumps(dict(
                tr.judge_groups(_projector(n), shard, shards, _sensitive), seconds=1)))
        with self.assertRaisesRegex(tr.Refused, "others_beside_shards_of_rule"):
            tr.handles(work)
        (work / "others.json").unlink()
        self.assertEqual(tr.handles(work)[1]["groups"], 5 * n + 3)

    def test_a_group_of_no_key_in_a_listed_source_is_refused(self) -> None:
        projector = _projector(3)
        files = projector.sources["c_sp"]
        groups = files.document_groups
        files.document_groups = lambda: iter([*groups(), (None, lambda: ([], True))])
        with self.assertRaisesRegex(tr.Refused, "old_rule_unknown:c_sp"):
            tr.judge_other_groups(projector, 4, _sensitive)


class ReadingsSnapshotTests(unittest.TestCase):
    """The judge reads the kept readings only from a copy: the state directory's live
    files (by any name) are refused, as is a directory without readings or a bad stride,
    before any shard is judged."""

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.state, self.snap = root / "state", root / "snap"
        for d in (self.state / "projection" / "delta", self.snap):
            d.mkdir(parents=True)
        for name in tr.KEPT_FILES:
            (self.state / "projection" / name).write_bytes(b"live")
        self.work = root / "work"

    def test_live_kept_readings_are_refused(self) -> None:
        for live in (self.state / "projection", self.state / "projection" / "delta"):
            (live / "readings.sqlite").touch()
            with self.subTest(live=live), self.assertRaisesRegex(
                    tr.Refused, "readings_snapshot_is_live:readings.sqlite"):
                tr.readings_snapshot(self.state, live)
        # the live member file linked in under its own name is refused too
        (self.snap / "readings.sqlite").write_bytes(b"copy")
        (self.state / "projection" / "member-readings.sqlite").unlink()
        (self.state / "projection" / "member-readings.sqlite").hardlink_to(
            self.snap / "readings.sqlite")
        with self.assertRaisesRegex(tr.Refused, "readings_snapshot_is_live"):
            tr.readings_snapshot(self.state, self.snap)
        with self.assertRaisesRegex(tr.Refused, "readings_snapshot_is_live"):
            tr.judge(str(self.state), self.work, 2, 1, readings=self.snap)
        self.assertFalse((self.work / "shards").exists())   # refused before any shard

    def test_a_copy_is_served_and_unknown_input_fails_closed(self) -> None:
        with self.assertRaisesRegex(tr.Refused, "readings_snapshot_missing"):
            tr.readings_snapshot(self.state, self.snap)
        (self.snap / "readings.sqlite").write_bytes(b"copy")
        self.assertEqual(tr.readings_snapshot(self.state, self.snap), self.snap.resolve())
        for stride in ((2, 2), (0, 0), (-1, 3)):
            with self.subTest(stride=stride), self.assertRaisesRegex(
                    tr.Refused, "stride_invalid"):
                tr.judge(str(self.state), self.work, 2, 1, stride=stride)
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"PYTHONHASHSEED": "0"}):
            self.assertEqual(tr.main(["judge", "--state-dir", str(self.state), "--work",
                                      str(self.work), "--stride", "x"]), 2)
        self.assertFalse((self.work / "shards").exists())


if __name__ == "__main__":
    unittest.main()
