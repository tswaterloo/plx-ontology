"""A huge first fill merges in committed batches and never starves the other relations.

Host cycle 4 (2026-10-30): a 31.66M-row first fill staged completely, then its merge ran
as one transaction that could not finish inside the Portal child's budget; the kill
rolled it back and every cycle repeated it, while the relations sorted after it were
never refreshed. These tests pin the repair at the mirror's boundary:

- a merge killed between batches resumes, without re-reading or re-staging a page, into
  exactly the state an uninterrupted (single-transaction) merge leaves;
- while it is partial, no reader (rows, resolve, citations, coverage, the ontology
  projection) serves any of its rows, and coverage marks it ``merging``;
- under a budget, small relations go before a large in-progress one, and a per-relation
  time slice keeps an unknown huge fill from starving the relations after it.

Fake Workbench transport of ``test_source_mirror`` (isolated-fixture; no network).
"""

import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from integrations.source_mirror import MirrorError, SourceMirror, make_citation
from integrations.tests.test_source_mirror import ALL, BASE, KEY, OPS
from integrations.tests.test_source_mirror_sync import ScriptedFake, SyncTestCase
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.project import MirrorCaptures

BIG = "public.gen_big"
IDENTIFIERS = {BIG: [["code", "system_document_number", "system_document_number"]]}


def big_relation(fake, count):
    """``count`` keys with a two-row run at id 20 (a run is never split by a batch) and
    update times whose maximum sits in the middle of the key order."""
    fake.catalog[BIG] = {"columns": ["id", "code", "qty", "updated_at"], "key": ["id"],
                         "domain": "orders",
                         "since": {"column": "updated_at", "mode": "timestamptz"}}
    rows = []
    for index in range(count):
        day = 28 if index != count // 2 else 29
        rows.append({"id": index, "code": f"C-{index}", "qty": str(index % 7),
                     "updated_at": f"2026-09-{day}T0{index % 10}:00:00Z"})
        if index == 20:
            rows.append({"id": index, "code": f"C-{index}-b", "qty": "9",
                         "updated_at": "2026-09-28T00:00:00Z"})
    fake.tables[BIG] = rows


def small_relation(fake, name, count=3):
    fake.catalog[name] = {"columns": ["id", "code", "updated_at"], "key": ["id"],
                          "domain": "orders",
                          "since": {"column": "updated_at", "mode": "timestamptz"}}
    fake.tables[name] = [{"id": i, "code": f"{name[-1]}-{i}",
                          "updated_at": "2026-09-28T00:00:00Z"} for i in range(count)]


def dump(path, relation):
    """Everything a completed fill leaves for ``relation``, ordered."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        out = {}
        for table, order in (("row_current", "key_json, run_index"),
                             ("row_version", "key_json, capture_id, run_index"),
                             ("identifier_index", "norm_value, column_name, key_json"),
                             ("row_removal", "key_json, basis, since"),
                             ("relation_state", "relation"),
                             ("relation_lineage", "relation"),
                             ("relation_soft_delete", "relation")):
            out[table] = [dict(r) for r in conn.execute(
                f"SELECT * FROM {table} WHERE relation = ? ORDER BY {order}", (relation,))]
        out["capture"] = [dict(r) for r in conn.execute(
            "SELECT c.*, s.state FROM capture c JOIN capture_state s USING (capture_id) "
            "WHERE relation = ?", (relation,))]
        out["leftover"] = conn.execute(
            "SELECT (SELECT COUNT(*) FROM stage_row) + (SELECT COUNT(*) FROM stage_seen) + "
            "(SELECT COUNT(*) FROM sync_progress)").fetchone()[0]
        return out
    finally:
        conn.close()


class Killed(Exception):
    """Stands for the process being killed inside a merge transaction."""


def kill_after(calls):
    """Make the merge die inside its ``calls``-th key write (a rolled-back batch)."""
    real = SourceMirror._index_key
    count = [0]

    def index_key(mirror, *args, **kwargs):
        count[0] += 1
        if count[0] == calls:
            raise Killed()
        return real(mirror, *args, **kwargs)
    return patch.object(SourceMirror, "_index_key", index_key)


class BatchedMergeTests(SyncTestCase):
    kill_after = staticmethod(kill_after)

    def test_killed_merge_resumes_to_exactly_the_uninterrupted_result(self):
        big_relation(self.fake, 60)
        # The run stages every page, then dies in the merge's third batch.
        first = self.mirror(page_size=10, merge_batch_rows=8, identifiers=IDENTIFIERS)
        with self.kill_after(20):
            run = first.sync(OPS, relations=[BIG])["runs"][0]
        self.assertEqual((run["outcome"], run["exception_type"]), ("failed", "Killed"))
        conn = self.db()
        merge = conn.execute("SELECT merge_json FROM sync_progress").fetchone()[0]
        self.assertIn('"batches":2', merge)          # two batches committed, kept
        staged = conn.execute("SELECT COUNT(*) FROM stage_row").fetchone()[0]
        self.assertEqual(staged, 61 - 16)          # the merged rows left the stage
        requests = len(self.fake.row_requests(BIG))
        # Resumed by a fresh process: no page is read again, nothing merged twice.
        resumed = self.mirror(page_size=10, merge_batch_rows=8, identifiers=IDENTIFIERS)
        run = resumed.sync(OPS, relations=[BIG])["runs"][0]
        self.assertEqual((run["outcome"], run["resumed"]), ("captured", True))
        self.assertEqual(len(self.fake.row_requests(BIG)), requests)
        batched = dump(self.path, BIG)

        # The same read merged in one transaction, in another file.
        self.path = self.path.with_name("whole.sqlite")
        self.fake = ScriptedFake()
        big_relation(self.fake, 60)
        whole = self.mirror(page_size=10, merge_batch_rows=1_000_000, identifiers=IDENTIFIERS)
        self.assertEqual(whole.sync(OPS, relations=[BIG])["runs"][0]["outcome"], "captured")
        expected = dump(self.path, BIG)
        self.assertEqual(batched["leftover"], 0)
        self.assertEqual(len(batched["row_current"]), 61)
        self.assertEqual(batched["relation_state"][0]["watermark_raw"], "2026-09-29T00:00:00Z")
        self.assertEqual(batched["relation_state"][0]["key_unique"], 0)
        self.assertEqual(batched["capture"][0]["keys_new"], 60)
        self.assertEqual(batched, expected)

    def test_batched_and_whole_merges_agree_at_every_size(self):
        base = self.path
        for count in (1, 9, 10, 60):
            dumps = []
            for batch in (8, 1_000_000):
                with self.subTest(count=count, batch=batch):
                    self.path = base.with_name(f"n{count}-b{batch}.sqlite")
                    self.fake = ScriptedFake()
                    big_relation(self.fake, count)
                    mirror = self.mirror(page_size=10, merge_batch_rows=batch,
                                         identifiers=IDENTIFIERS)
                    self.assertEqual(mirror.sync(OPS, relations=[BIG])["runs"][0]["outcome"],
                                     "captured")
                    dumps.append(dump(self.path, BIG))
            self.assertEqual(len(dumps[0]["row_current"]), count + (count > 20))
            self.assertEqual(dumps[0], dumps[1])

    def test_readers_see_nothing_of_a_partial_merge_and_coverage_marks_it(self):
        small_relation(self.fake, "public.gen_held")
        big_relation(self.fake, 60)
        writer = self.mirror(page_size=10, merge_batch_rows=8, identifiers=IDENTIFIERS)
        self.assertEqual(writer.sync(OPS, relations=["public.gen_held"])["runs"][0]
                         ["outcome"], "captured")
        with self.kill_after(20):
            writer.sync(OPS, relations=[BIG])
        capture_id = self.db().execute(
            "SELECT capture_id FROM sync_progress WHERE relation = ?", (BIG,)).fetchone()[0]
        merged = self.db().execute("SELECT COUNT(*) FROM row_current WHERE relation = ? AND "
                                   "capture_id = ?", (BIG, capture_id)).fetchone()[0]
        self.assertEqual(merged, 16)   # the committed batches, held back from readers

        reader = SourceMirror(self.path, None, ALL, clock=self.clock, identifiers=IDENTIFIERS)
        self.addCleanup(reader.close)
        with self.assertRaises(MirrorError):
            reader.rows(OPS, BIG)
        found = reader.resolve(OPS, "C-10")   # a merged key, indexed
        self.assertEqual(found["candidates"], [])
        self.assertIn(BIG, found["coverage"]["not_mirrored"])
        cited = reader.resolve_citation(
            make_citation(BIG, {"id": 10}, capture_id, reader.generation), OPS)
        self.assertEqual((cited["ok"], cited["error"]), (False, "capture_in_progress"))
        item = {i["relation"]: i for i in reader.coverage(OPS)["relations"]}[BIG]
        self.assertEqual((item["mirrored"], item["complete"]), (False, False))
        self.assertEqual(item["sync_in_progress"]["phase"], "merging")
        self.assertEqual(item["sync_in_progress"]["rows_merged"], 16)
        self.assertEqual(reader.rows(OPS, "public.gen_held")["returned"], 3)
        with MirrorCaptures(self.path) as captures:
            relations = captures.records([BIG, "public.gen_held"])
            self.assertEqual(list(relations[BIG]), [])
            self.assertEqual(len(list(relations["public.gen_held"])), 3)
            # The fingerprint of a relation holding no completed row: it does not move
            # with each committed batch.
            self.assertEqual(captures.fingerprint([BIG])[BIG], hashlib.sha256(
                f"{captures.generation}\x00{BIG}\n".encode()).hexdigest())
        # A merging fill is completed, never abandoned, even for a changed projection.
        self.fake.catalog[BIG]["columns"].append("note")
        resumed = self.mirror(page_size=10, merge_batch_rows=8, identifiers=IDENTIFIERS)
        self.assertEqual(resumed.sync(OPS, relations=[BIG])["runs"][0]["outcome"], "captured")
        self.assertEqual(reader.rows(OPS, BIG, limit=100)["returned"], 61)
        self.assertEqual(resumed._plan(resumed._state(BIG), self.fake.catalog[BIG],
                                       ["code", "id", "note", "qty", "updated_at"],
                                       False)[0], "full_traversal")

    def test_a_persisted_copy_is_never_mixed_into_a_merging_fill(self):
        """An outage while a fill merges: the copy (here with other keys) is not imported
        over the merged batches, which would serve them under the copy's state."""
        big_relation(self.fake, 60)
        writer = self.mirror(page_size=10, merge_batch_rows=8)
        with self.kill_after(20):
            writer.sync(OPS, relations=[BIG])
        copy_path = self.path.with_name("copy.sqlite")
        other = ScriptedFake()
        big_relation(other, 5)
        for row in other.tables[BIG]:
            row["id"] += 1000
        copy = SourceMirror(copy_path, WorkbenchReadApiClient(BASE, KEY, fetch=other), ALL,
                            clock=self.clock, retry_delay_seconds=0)
        copy.sync(OPS, relations=[BIG])
        copy.close()
        with sqlite3.connect(str(copy_path)) as conn:   # a copy from before label digests
            conn.execute("DELETE FROM relation_lineage")
            conn.execute("DELETE FROM relation_soft_delete")

        class Store:
            def load(self, relations, **_):
                return {"path": str(copy_path), "captures_as_of": "2026-09-29T12:00:00Z"}
        reader = self.mirror(page_size=10, merge_batch_rows=8, persisted_store=Store())
        outage = MirrorError("source_unavailable", "down", status=503)
        self.assertFalse(reader._fill_from_persisted(BIG, "plx", outage))
        self.assertIsNone(reader._state(BIG))
        self.assertEqual(self.db().execute(
            "SELECT COUNT(*) FROM row_current WHERE relation = ?", (BIG,)).fetchone()[0], 16)

    def test_budget_stops_a_merge_between_batches_and_the_next_run_completes_it(self):
        big_relation(self.fake, 60)
        now = [0.0]
        writer = self.mirror(page_size=10, merge_batch_rows=8, monotonic=lambda: now[0])
        real = SourceMirror._merge_stage

        def slow_merge(mirror, *args, **kwargs):
            now[0] += 10.0   # each batch takes ten seconds
            return real(mirror, *args, **kwargs)
        with patch.object(SourceMirror, "_merge_stage", slow_merge):
            run = writer.sync(OPS, relations=[BIG], budget_seconds=25)["runs"][0]
        self.assertEqual(run["outcome"], "budget_exhausted")
        self.assertIn('"batches":3', self.db().execute(
            "SELECT merge_json FROM sync_progress").fetchone()[0])
        run = writer.sync(OPS, relations=[BIG], budget_seconds=1000)["runs"][0]
        self.assertEqual(run["outcome"], "captured")
        self.assertEqual(dump(self.path, BIG)["leftover"], 0)


def reread(fake):
    """The source after the first capture: most rows changed (one key's code too),
    two keys gone, two added."""
    rows = [row for row in fake.tables[BIG] if row["id"] not in (5, 40)]
    for row in rows:
        if row["id"] % 4:
            row["qty"] = f"{row['qty']}-r"
        if row["id"] == 10:
            row["code"] = "N-10"
    rows += [{"id": 100 + i, "code": f"C-{100 + i}", "qty": "1",
              "updated_at": "2026-09-28T05:00:00Z"} for i in range(2)]
    fake.tables[BIG] = rows


class FullRereadTests(SyncTestCase):
    kill_after = staticmethod(kill_after)

    """A full re-read of a relation with a completed capture merges in batches too."""

    def captured_then_reread(self, batch):
        mirror = self.mirror(page_size=10, merge_batch_rows=batch, identifiers=IDENTIFIERS)
        self.assertEqual(mirror.sync(OPS, relations=[BIG])["runs"][0]["outcome"], "captured")
        reread(self.fake)
        self.clock.advance(3600)
        return mirror

    def test_killed_reread_resumes_to_exactly_the_one_transaction_result(self):
        big_relation(self.fake, 60)
        first = self.captured_then_reread(8)
        with self.kill_after(15):
            run = first.sync(OPS, relations=[BIG], full=True)["runs"][0]
        self.assertEqual(run["exception_type"], "Killed")
        merge = json.loads(self.db().execute("SELECT merge_json FROM sync_progress"
                                             ).fetchone()[0])
        self.assertEqual((merge["kind"], merge["batches"]), ("full_read", 1))
        requests = len(self.fake.row_requests(BIG))
        resumed = self.mirror(page_size=10, merge_batch_rows=8, identifiers=IDENTIFIERS)
        run = resumed.sync(OPS, relations=[BIG], full=True)["runs"][0]
        self.assertEqual((run["outcome"], run["resumed"]), ("captured", True))
        self.assertEqual(len(self.fake.row_requests(BIG)), requests)   # nothing re-read
        batched = dump(self.path, BIG)
        batched_path = self.path

        self.path = self.path.with_name("whole.sqlite")
        self.fake = ScriptedFake()
        big_relation(self.fake, 60)
        self.clock.advance(-3600)
        whole = self.captured_then_reread(1_000_000)
        self.assertEqual(whole.sync(OPS, relations=[BIG], full=True)["runs"][0]["outcome"],
                         "captured")
        expected = dump(self.path, BIG)
        self.assertEqual(batched["leftover"], 0)
        self.assertEqual(batched["capture"][-1]["keys_absent"], 2)
        self.assertEqual(sum(1 for r in batched["row_removal"] if r["until"] is None), 2)
        # The entry the batches kept for the replaced identifier is gone once it completed.
        self.assertEqual(resumed.resolve(OPS, "C-10")["candidates"], [])
        self.assertEqual(len(resumed.resolve(OPS, "N-10")["candidates"]), 1)
        self.assertEqual(self.db_at(batched_path).execute(
            "SELECT COUNT(*) FROM mirror_meta WHERE name LIKE 'index-cleanup:%'"
            ).fetchone()[0], 0)
        self.assertEqual(batched, expected)

    def test_a_reader_sees_the_previous_capture_until_the_reread_completes(self):
        big_relation(self.fake, 60)
        writer = self.captured_then_reread(8)
        before = writer.rows(OPS, BIG, limit=100, max_staleness=10 ** 6)   # no refresh
        old_capture = before["rows"][0]["capture_id"]
        with MirrorCaptures(self.path) as captures:
            old_records = sorted((r.record_key, r.digest, r.l1_handle)
                                 for r in captures.records([BIG])[BIG])
        with self.kill_after(15):
            writer.sync(OPS, relations=[BIG], full=True)
        merged = self.db().execute(
            "SELECT COUNT(*) FROM row_current WHERE relation = ? AND capture_id <> ?",
            (BIG, old_capture)).fetchone()[0]
        self.assertGreater(merged, 0)   # batches are in row_current, not yet served

        # A reader with a live client: served at once, the merge left to the capture child.
        reader = self.mirror(page_size=10, merge_batch_rows=8, identifiers=IDENTIFIERS)
        requests = len(self.fake.row_requests(BIG))
        during = reader.rows(OPS, BIG, limit=100, max_staleness=0)
        self.assertEqual(len(self.fake.row_requests(BIG)), requests)
        self.assertEqual(during["freshness"]["sync_in_progress"]["phase"], "merging")
        self.assertIn("merge in progress", during["freshness"]["refresh_reason"])
        self.assertEqual([(r["citation"], r["row"], r["content_sha256"]) for r in during["rows"]],
                         [(r["citation"], r["row"], r["content_sha256"]) for r in before["rows"]])
        self.assertEqual(len(reader.resolve(OPS, "C-10")["candidates"]), 1)
        self.assertEqual(reader.resolve(OPS, "N-10")["candidates"], [])
        cited = reader.resolve_citation(before["rows"][3]["citation"], OPS)
        self.assertEqual((cited["ok"], cited["status"], cited["changed_since"]),
                         (True, "current", False))
        with MirrorCaptures(self.path) as captures:
            self.assertEqual(sorted((r.record_key, r.digest, r.l1_handle)
                                    for r in captures.records([BIG])[BIG]), old_records)
        # Completed by the capture child: the new capture, removals included.
        self.assertEqual(writer.sync(OPS, relations=[BIG], full=True)["runs"][0]["outcome"],
                         "captured")
        after = reader.rows(OPS, BIG, limit=100)
        self.assertEqual(after["returned"], 61)
        self.assertNotIn({"id": 5}, [r["key"] for r in after["rows"]])
        self.assertTrue(reader.resolve_citation(before["rows"][3]["citation"], OPS)
                        ["changed_since"])

    def db_at(self, path):
        connection = sqlite3.connect(str(path))
        self.addCleanup(connection.close)
        return connection

    def test_citations_during_a_reread_are_judged_against_the_content_served(self):
        """Review b9d7c3fc4a: key 0 read 0 -> X -> 0 again; while the third read merges,
        X is served. A citation of the first read is superseded by X (not equal to it),
        its current citation is X's and resolves to it; X's own citation is current."""
        big_relation(self.fake, 60)
        mirror = self.mirror(page_size=10, merge_batch_rows=8, identifiers=IDENTIFIERS)
        mirror.sync(OPS, relations=[BIG])
        first = self.by_id(mirror.rows(OPS, BIG, limit=100, max_staleness=10 ** 6))[0]
        original = dict(first["row"])
        reread(self.fake)
        self.fake.update(BIG, 0, qty="X")
        self.clock.advance(3600)
        mirror.sync(OPS, relations=[BIG], full=True)
        served = self.by_id(mirror.rows(OPS, BIG, limit=100, max_staleness=10 ** 6))[0]
        self.assertEqual(served["row"]["qty"], "X")
        with MirrorCaptures(self.path) as captures:
            captures.records([BIG])
            before = captures.fingerprint([BIG])[BIG]
        lineage = self.covered(mirror, BIG)["lineage_counts"]
        for row in self.fake.tables[BIG]:
            row["qty"] = f"{row['qty']}-s"
        self.fake.update(BIG, 0, qty=original["qty"])   # back to what the first read saw
        self.clock.advance(3600)
        with self.kill_after(10):
            mirror.sync(OPS, relations=[BIG], full=True)
        written = self.db().execute(
            "SELECT row_json FROM row_current WHERE relation = ? AND key_json = ?",
            (BIG, '{"id":0}')).fetchone()[0]
        self.assertIn(f'"qty":"{original["qty"]}"', written)   # merged, not served

        reader = SourceMirror(self.path, None, ALL, clock=self.clock, identifiers=IDENTIFIERS)
        self.addCleanup(reader.close)
        old = reader.resolve_citation(first["citation"], OPS)
        self.assertEqual((old["status"], old["content_equal_to_current"]),
                         ("superseded", False))
        self.assertEqual(old["current_citation"], served["citation"])
        followed = reader.resolve_citation(old["current_citation"], OPS)
        self.assertEqual((followed["ok"], followed["status"], followed["row"]["qty"]),
                         (True, "current", "X"))
        self.assertEqual(followed["upstream_presence"], "observed_in_latest_full_traversal")
        # Nothing readers see moved: the projection's fingerprint and the coverage counts.
        with MirrorCaptures(self.path) as captures:
            captures.records([BIG])
            self.assertEqual(captures.fingerprint([BIG])[BIG], before)
        self.assertEqual(self.covered(reader, BIG)["lineage_counts"], lineage)

    def test_a_read_of_a_merging_first_fill_fails_fast_and_leaves_it_alone(self):
        big_relation(self.fake, 60)
        writer = self.mirror(page_size=10, merge_batch_rows=8)
        with self.kill_after(20):
            writer.sync(OPS, relations=[BIG])
        before = self.db().execute("SELECT merge_json, owner FROM sync_progress").fetchone()
        reader = self.mirror(page_size=10, merge_batch_rows=8)
        with self.assertRaises(MirrorError) as raised:
            reader.rows(OPS, BIG)
        self.assertEqual(raised.exception.code, "merge_in_progress")
        self.assertEqual(self.db().execute("SELECT merge_json, owner FROM sync_progress"
                                           ).fetchone(), before)


class TimedFake(ScriptedFake):
    """Every row request takes ten seconds of the mirror's monotonic clock."""

    def __init__(self):
        super().__init__()
        self.now = [0.0]

    def __call__(self, url, headers, *, max_bytes):
        if "/rows" in url:
            self.now[0] += 10.0
        return super().__call__(url, headers, max_bytes=max_bytes)


class OrderingTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.fake = TimedFake()

    def timed(self, **options):
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        mirror = SourceMirror(self.path, client, ALL, clock=self.clock, retry_delay_seconds=0,
                              sleep=self.sleeps.append, monotonic=lambda: self.fake.now[0],
                              page_size=5, **options)
        self.addCleanup(mirror.close)
        return mirror

    def test_small_relations_refresh_before_a_large_fill_in_progress(self):
        # "public.gen_a" sorts first and is a 200-row fill (40 pages, 400 s) in progress.
        generated = [{"id": i, "code": f"A-{i}", "updated_at": "2026-09-28T00:00:00Z"}
                     for i in range(200)]
        self.fake.catalog["public.gen_a"] = {"columns": ["id", "code", "updated_at"],
                                             "key": ["id"], "domain": "orders", "since": None}
        self.fake.tables["public.gen_a"] = generated
        for name in ("public.gen_b", "public.gen_c", "public.gen_d"):
            small_relation(self.fake, name)
        mirror = self.timed(large_relation_rows=20)
        first = mirror.sync(OPS, relations=["public.gen_a"], budget_seconds=50)
        self.assertEqual(first["runs"][0]["outcome"], "budget_exhausted")
        names = ["public.gen_a", "public.gen_b", "public.gen_c", "public.gen_d"]
        report = mirror.sync(OPS, relations=names, budget_seconds=100)
        order = [(run["relation"], run["outcome"]) for run in report["runs"]]
        self.assertEqual(order, [("public.gen_b", "captured"), ("public.gen_c", "captured"),
                                 ("public.gen_d", "captured"),
                                 ("public.gen_a", "budget_exhausted")])
        # Its traversal is kept: it continues where it stopped, never from the start.
        report = mirror.sync(OPS, relations=["public.gen_a"], budget_seconds=1000)
        self.assertEqual(report["runs"][0]["outcome"], "captured")
        cursors = [query.get("cursor", [None])[0]
                   for query in self.fake.queries_for("public.gen_a")]
        self.assertEqual(len(cursors), len(set(cursors)))
        self.assertEqual(self.covered(mirror, "public.gen_a")["rows_current"], 200)

    def test_a_relation_slice_lets_the_rest_run_then_returns_the_leftover_budget(self):
        self.fake.catalog["public.gen_a"] = {"columns": ["id", "code", "updated_at"],
                                             "key": ["id"], "domain": "orders", "since": None}
        self.fake.tables["public.gen_a"] = [
            {"id": i, "code": f"A-{i}", "updated_at": "2026-09-28T00:00:00Z"}
            for i in range(60)]   # 12 pages, 120 s; never mirrored, so its size is unknown
        small_relation(self.fake, "public.gen_b")
        mirror = self.timed()
        report = mirror.sync(OPS, relations=["public.gen_a", "public.gen_b"],
                             budget_seconds=200, relation_budget_seconds=30)
        runs = {run["relation"]: run for run in report["runs"]}
        self.assertEqual(runs["public.gen_b"]["outcome"], "captured")
        self.assertEqual(runs["public.gen_a"]["outcome"], "captured")
        self.assertEqual(runs["public.gen_a"]["slices"], 2)
        self.assertNotIn("sliced", runs["public.gen_b"])
        # Without a slice the unknown fill would have taken the whole budget first.
        self.assertEqual([run["relation"] for run in report["runs"]],
                         ["public.gen_a", "public.gen_b"])

    def test_invalid_slice_fails_closed(self):
        small_relation(self.fake, "public.gen_b")
        mirror = self.timed()
        for value in (-1, True, "30", 10 ** 9):
            with self.assertRaises(MirrorError):
                mirror.sync(OPS, relation_budget_seconds=value)
        with self.assertRaises(ValueError):
            self.timed(merge_batch_rows=0)


class HostConfigTests(unittest.TestCase):
    def test_the_portal_child_passes_its_relation_slice_to_the_sync(self):
        import tempfile
        from pathlib import Path

        from integrations.source_mirror import main
        from operating import ingest_forever as forever

        spec = forever.SOURCES["portal"]
        self.assertEqual(spec.fields["relation_budget_seconds"], "seconds")
        with tempfile.TemporaryDirectory() as state:
            argv = spec.argv(Path(state), {"audience": "/a.json", "budget_seconds": 3600,
                                           "relation_budget_seconds": 600})
            self.assertEqual(argv[argv.index("--relation-budget-seconds") + 1], "600")
            self.assertNotIn("--relation-budget-seconds",
                             spec.argv(Path(state), {"audience": "/a.json"}))
            # The child's CLI takes the flag (a bad value is refused before any request).
            with self.assertRaises(SystemExit):
                main(["--db", str(Path(state) / "m.sqlite"), "--audience", "/a.json",
                      "--principal", "p", "sync", "--relation-budget-seconds", "soon"])


if __name__ == "__main__":
    unittest.main()
