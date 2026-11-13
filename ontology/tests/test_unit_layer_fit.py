"""The unit migration layer fitted to the host (``ontology.unit_layer_fit``) projects what
the plain build projects:

* a mirror relation the Portal ETL re-inserts read through ``CollapsingMirror`` gives the
  rows a full read gives (every table, the same order), with the copies the collapse
  drops left unread: copies of one record at 1, 10 and 60 per record; first-inserted and
  smallest-object-id winners (an id the object id escapes included); a copy with
  history, one removed from the source, one from an unfinished capture, a row the
  projection keeps whole (an infinite number) and a row that is not strict JSON; a
  relation with a row not keyed by its own id is read whole, as is one a binding reads
  with a condition;
* the link phase on worker processes writes the serial phase's working links (``seq``
  included), store rows and report;
* the fitted build command keeps the kept fields' key (no derivation module imports it).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ontology import project as P
from ontology import unit_layer_fit as fit
from ontology.tests import test_projection as tp

RELATION = "public.app_manufacturer"
BINDING = "gen.public.app_manufacturer"
GENERATION = "0123456789abcdef"


def _key(ident) -> str:
    """A mirror key as the mirror writes it (``source_mirror._canonical``)."""
    return json.dumps({"id": ident}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _mirror(path: Path, rows: list[dict], *, versions: list[dict] = (),
            relation: str = RELATION) -> None:
    """A minimal source mirror holding ``rows`` of ``relation`` (the tables and columns
    ``MirrorCaptures`` reads). Each row: id, row, capture (default 1), missing, key."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE mirror_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE capture (capture_id INTEGER PRIMARY KEY, finished_at TEXT NOT NULL);
        CREATE TABLE capture_state (capture_id INTEGER PRIMARY KEY, state TEXT NOT NULL);
        CREATE TABLE relation_state (relation TEXT PRIMARY KEY, last_refresh_at TEXT,
            last_capture_id INTEGER, last_full_traversal_at TEXT, columns_json TEXT,
            row_count INTEGER);
        CREATE TABLE row_current (relation TEXT NOT NULL, key_json TEXT NOT NULL,
            run_index INTEGER NOT NULL, run_length INTEGER NOT NULL, row_json TEXT NOT NULL,
            row_digest TEXT NOT NULL, capture_id INTEGER NOT NULL, last_seen_at TEXT NOT NULL,
            absent_since_capture INTEGER, missing_from_source_since TEXT,
            PRIMARY KEY (relation, key_json, run_index));
        CREATE TABLE row_version (relation TEXT NOT NULL, key_json TEXT NOT NULL,
            capture_id INTEGER NOT NULL, run_index INTEGER NOT NULL,
            run_length INTEGER NOT NULL, row_json TEXT NOT NULL, row_digest TEXT NOT NULL,
            last_seen_at TEXT NOT NULL, superseded_by_capture INTEGER NOT NULL,
            PRIMARY KEY (relation, key_json, capture_id, run_index));
        CREATE TABLE row_removal (relation TEXT NOT NULL, key_json TEXT NOT NULL,
            basis TEXT NOT NULL, since TEXT NOT NULL, until TEXT, replaced_basis TEXT,
            replaced_by TEXT);
    """)
    db.execute("INSERT INTO mirror_meta VALUES ('generation', ?)", (GENERATION,))
    for capture, at, state in ((1, "2026-11-01T00:00:00Z", "complete"),
                               (2, "2026-11-02T00:00:00Z", "complete"),
                               (3, "2026-11-03T00:00:00Z", "in_progress")):
        db.execute("INSERT INTO capture VALUES (?, ?)", (capture, at))
        db.execute("INSERT INTO capture_state VALUES (?, ?)", (capture, state))
    db.execute("INSERT INTO relation_state VALUES (?, '2026-11-02T00:00:00Z', 2, "
               "'2026-11-02T00:00:00Z', '[]', ?)", (relation, len(rows)))
    for item in rows:
        key = item.get("key", _key(item["id"]))
        text = item.get("text") or json.dumps(item["row"], separators=(",", ":"))
        db.execute("INSERT INTO row_current VALUES (?, ?, 0, 1, ?, ?, ?, "
                   "'2026-11-02T00:00:00Z', NULL, ?)",
                   (relation, key, text, "d-" + key, item.get("capture", 2),
                    item.get("missing")))
    for item in versions:
        key = _key(item["id"])
        db.execute("INSERT INTO row_version VALUES (?, ?, 1, 0, 1, ?, ?, "
                   "'2026-11-01T00:00:00Z', 2)",
                   (relation, key, json.dumps(item["row"], separators=(",", ":")),
                    "v-" + key))
    db.commit()
    db.close()


def _copy(ident: str, created: str, **values) -> dict:
    return {"id": ident, "row": {"id": ident, "created_at": created,
                                 "updated_at": "2026-10-01T00:00:00", "name": "Acme",
                                 **values}}


def _world(copies: int) -> tuple[list[dict], list[dict]]:
    """Records re-inserted ``copies`` times each, and the cases the collapse turns on."""
    rows: list[dict] = []
    for record in range(3):   # first inserted wins
        for n in range(copies):
            rows.append(_copy(f"r{record}c{copies - n:03d}", f"2026-0{1 + n % 9}-01T00:00:00",
                              name=f"rec{record}", rank=record))
    tie = "2026-01-01T00:00:00"
    # One instant: the smallest object id wins, as the id escapes it ('a b' -> 'a%20b').
    rows += [_copy("a b", tie, name="tie"), _copy("a!b", tie, name="tie"),
             _copy("aé", tie, name="tie")]
    # A copy with history: read whole; its latest version decides what it collapses with.
    rows += [_copy("h1", "2025-01-01T00:00:00", name="hist"),
             _copy("h2", "2026-01-01T00:00:00", name="hist"),
             _copy("h3", "2026-02-01T00:00:00", name="hist")]
    versions = [_copy("h1", "2025-01-01T00:00:00", name="before")]
    # Removed from the source: a partition of its own.
    gone = "2026-11-02T00:00:00Z"
    rows += [{**_copy("m1", "2026-03-01T00:00:00", name="gone"), "missing": gone},
             _copy("m2", "2026-01-01T00:00:00", name="gone"),
             {**_copy("m3", "2026-04-01T00:00:00", name="gone"), "missing": gone}]
    # An unfinished capture's copy (never read) would have won.
    rows += [{**_copy("u1", "2020-01-01T00:00:00", name="late"), "capture": 3},
             _copy("u2", "2026-01-01T00:00:00", name="late"),
             _copy("u3", "2026-02-01T00:00:00", name="late")]
    # Rows the projection never collapses (an infinite number): every copy is its own.
    rows += [{"id": f"x{n}", "text": '{"id":"x%d","created_at":"2026-01-0%dT00:00:00",'
              '"q":1e999}' % (n, n + 1)} for n in range(3)]
    # A blank id names no object (never staged): the copy after it is the record's.
    rows += [_copy("  ", "2019-01-01T00:00:00", name="blank"),
             _copy("b1", "2026-01-01T00:00:00", name="blank")]
    # No created_at, or not a text: sorts last.
    rows += [_copy("z1", None, name="late-less"), _copy("z2", 7, name="late-less"),
             _copy("z0", None, name="late-less")]
    return rows, versions


class CollapsingMirrorTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from ontology.schema import load_schema
        cls.schema = load_schema()
        cls.collapse = fit.collapsible(cls.schema)

    def project(self, path: Path, collapse: bool) -> tuple[dict, dict]:
        with tempfile.TemporaryDirectory() as scratch:
            source = (fit.CollapsingMirror(path, collapse=self.collapse) if collapse
                      else P.MirrorCaptures(path))
            with source:
                projector = P.Projector(self.schema, {"portal_api": source},
                                        scratch_dir=scratch, scope=[BINDING],
                                        emit=[P.binding_unit(BINDING)])
                rows = projector.project()
                unread = dict(getattr(source, "unread", {}))
        return rows, unread

    def test_the_relations_it_collapses_are_the_ones_keyed_by_their_minted_id(self):
        self.assertIn(RELATION, self.collapse)
        self.assertIn("public.app_manufacturing_order_line", self.collapse)
        self.assertEqual(set(self.collapse), set(P.ETL_REINSERTED["portal_api"]))
        schema = mock.Mock(object_types={"T": {"bindings": [
            {"id": "b1", "source": "portal_api", "relation": RELATION, "key": ["id"],
             "lineage": {}},
            {"id": "b2", "source": "portal_api", "relation": RELATION, "key": ["id"],
             "lineage": {}, "where": [{"path": "name", "op": "eq", "value": "x"}]}]}})
        self.assertNotIn(RELATION, fit.collapsible(schema))    # a condition reads more

    def test_unread_copies_never_change_the_projection(self):
        for copies in (1, 10, 60):
            with self.subTest(copies=copies), tempfile.TemporaryDirectory() as tmp:
                rows, versions = _world(copies)
                path = Path(tmp) / "mirror.sqlite"
                _mirror(path, rows, versions=versions)
                full, _ = self.project(path, collapse=False)
                fitted, unread = self.project(path, collapse=True)
                self.assertEqual(fitted, full)
                objects = {P.native_key_of(o["object_id"]) for o in full["objects"]}
                self.assertIn('{"id":"a!b"}', objects)        # escaped order, not raw
                self.assertIn('{"id":"h1"}', objects)         # history wins on its own
                self.assertTrue({'{"id":"m1"}', '{"id":"m2"}'} <= objects)   # gone apart
                self.assertIn('{"id":"b1"}', objects)
                self.assertGreaterEqual(len([o for o in objects if '"x' in o]), 3)
                # 3 x (copies - 1) record copies, 2 of the tie, h2 h3 vs one another,
                # m1 m3, u2 u3: what the collapse drops, never read
                self.assertEqual(unread, {RELATION: 3 * (copies - 1) + 2 + 1 + 1 + 1 + 2})

    def test_a_relation_with_a_row_it_cannot_check_is_read_whole(self):
        odd = (
            # keyed by another id than its row's
            {**_copy("k1", "2026-01-01T00:00:00", name="rec0", rank=0),
             "key": '{"id":"other"}'},
            # not strict JSON (the projection reads it; SQL cannot check its id)
            {"id": "n1", "text": '{"id":"n1","created_at":"2026-01-01T00:00:00","q":NaN}'},
            # an id that is not a text
            {"id": 5, "key": '{"id":5}', "row": {"id": 5, "created_at": "2026-01-01",
                                                  "name": "rec0", "rank": 0}})
        for row in odd:
            with self.subTest(row=row), tempfile.TemporaryDirectory() as tmp:
                rows, versions = _world(4)
                path = Path(tmp) / "mirror.sqlite"
                _mirror(path, rows + [row], versions=versions)
                full, _ = self.project(path, collapse=False)
                fitted, unread = self.project(path, collapse=True)
                self.assertEqual(fitted, full)
                self.assertEqual(unread, {})

    def test_kept_copies_computed_once_are_loaded_for_the_same_snapshot_only(self):
        """A parallel build's workers load the parent's kept copies (``kept_dir``) instead
        of each computing them: what they read is what computing gives; a file that is
        not whole is computed again; a mirror that moved names another file."""
        def opened(path, kept):
            with tempfile.TemporaryDirectory() as scratch, fit.CollapsingMirror(
                    path, collapse=self.collapse, kept_dir=kept) as source:
                rows = P.Projector(self.schema, {"portal_api": source}, scratch_dir=scratch,
                                   scope=[BINDING], emit=[P.binding_unit(BINDING)]).project()
                return rows, dict(source.unread), source.kept_loaded, source.kept_path
        for copies in (1, 10, 60):
            with self.subTest(copies=copies), tempfile.TemporaryDirectory() as tmp:
                rows, versions = _world(copies)
                path = Path(tmp) / "mirror.sqlite"
                _mirror(path, rows, versions=versions)
                full, _ = self.project(path, collapse=False)
                computed, unread, loaded, file = opened(path, None)
                self.assertEqual((computed, loaded, file), (full, False, None))
                kept = Path(tmp) / "kept"
                kept.mkdir()
                first = opened(path, kept)
                self.assertFalse(first[2])
                self.assertTrue(first[3].exists())
                again = opened(path, kept)
                self.assertTrue(again[2])                       # loaded, not computed
                self.assertEqual(again[0], full)
                self.assertEqual(again[1], unread)
                self.assertEqual(again[3], first[3])
                self.assertEqual(list(kept.glob("*.part")), [])
                with sqlite3.connect(first[3]) as db:          # not whole: computed
                    db.execute("UPDATE meta SET value = '0' WHERE name = 'complete'")
                broken = opened(path, kept)
                self.assertFalse(broken[2])
                self.assertEqual((broken[0], broken[1]), (full, unread))
                first[3].write_bytes(b"not a database")
                self.assertFalse(opened(path, kept)[2])
        with tempfile.TemporaryDirectory() as tmp:             # the mirror moved
            rows, versions = _world(4)
            path = Path(tmp) / "mirror.sqlite"
            _mirror(path, rows, versions=versions)
            kept = Path(tmp) / "kept"
            kept.mkdir()
            before = opened(path, kept)[3]
            with sqlite3.connect(path) as db:
                db.execute("UPDATE row_current SET last_seen_at = '2099-01-01' "
                           "WHERE rowid = (SELECT MIN(rowid) FROM row_current)")
            after = opened(path, kept)
            self.assertNotEqual(after[3], before)
            self.assertFalse(after[2])

    def test_a_worker_loads_the_parents_kept_copies_and_never_computes_them(self):
        """The parent computes the kept copies once, for every relation it collapses; a
        worker (``kept_file``) loads that file filtered to what it reads, never computes,
        and fails its open when the file is missing. A worker whose file marks moved (a
        WAL touched by a reader) reads the parent's records while its relation marks are
        the parent's, and only then."""
        def project(source, scratch):
            return P.Projector(self.schema, {"portal_api": source}, scratch_dir=scratch,
                               scope=[BINDING], emit=[P.binding_unit(BINDING)]).project()
        for copies in (1, 10, 60):
            with self.subTest(copies=copies), tempfile.TemporaryDirectory() as tmp:
                rows, versions = _world(copies)
                path = Path(tmp) / "mirror.sqlite"
                _mirror(path, rows, versions=versions)
                full, unread = self.project(path, collapse=True)
                kept = Path(tmp) / "kept"
                kept.mkdir()
                with fit.CollapsingMirror(path, collapse=self.collapse,
                                          kept_dir=kept) as parent:
                    self.assertEqual(project(parent, tmp), full)
                    self.assertEqual(parent.unread, unread)          # what it read only
                    file = parent.kept_path
                with sqlite3.connect(file) as db:                    # every collapse relation
                    self.assertEqual({r for (r,) in db.execute("SELECT relation FROM collapsed")},
                                     set(self.collapse))
                with mock.patch.object(fit, "_kept_copies",
                                       side_effect=AssertionError("computed in a worker")), \
                        fit.CollapsingMirror(path, collapse=self.collapse,
                                             kept_file=file) as worker:
                    self.assertEqual(project(worker, tmp), full)
                    self.assertTrue(worker.kept_loaded)
                    self.assertEqual(worker.unread, unread)
                file.unlink()
                with mock.patch.object(fit, "_kept_copies",
                                       side_effect=AssertionError("computed in a worker")), \
                        fit.CollapsingMirror(path, collapse=self.collapse,
                                             kept_file=file) as worker:
                    with self.assertRaisesRegex(P.ProjectionError, "kept_copies_missing"):
                        worker.records([RELATION])
        with tempfile.TemporaryDirectory() as tmp:                   # the file marks moved
            rows, versions = _world(4)
            path = Path(tmp) / "mirror.sqlite"
            _mirror(path, rows, versions=versions)
            marks = {RELATION: ["epoch", "gen", None, "seen", 1, 7, "s"]}
            with fit.CollapsingMirror(path, collapse=self.collapse) as parent:
                parent.records([RELATION])
                mark = parent.watermark()
            self.assertIsNotNone(mark)
            os.utime(path, ns=(1, 1))
            for theirs, expected in ((marks, mark), ({RELATION: ["epoch", "gen", None, "seen",
                                                                 2, 7, "s"]}, None), (None, None)):
                with self.subTest(theirs=theirs), mock.patch.object(
                        fit.CollapsingMirror, "relation_marks", return_value=theirs), \
                        fit.CollapsingMirror(path, collapse=self.collapse,
                                             pinned=[mark, marks]) as worker:
                    worker.records([RELATION])
                    own = P.MirrorCaptures.watermark(worker)
                    self.assertNotEqual(own, mark)
                    self.assertEqual(worker.watermark(), expected or own)

    def test_the_build_reads_through_it_while_fitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows, versions = _world(5)
            path = Path(tmp) / "mirror.sqlite"
            _mirror(path, rows, versions=versions)
            with fit.fitted(self.schema, workers=1) as seen:
                source = P.MirrorCaptures(path)
                self.assertIsInstance(source, fit.CollapsingMirror)
                with source:
                    P.Projector(self.schema, {"portal_api": source}, scratch_dir=tmp,
                                scope=[BINDING], emit=[P.binding_unit(BINDING)]).project()
            self.assertIs(P.MirrorCaptures, fit.CollapsingMirror.__mro__[1])
            self.assertEqual(seen["unread"][RELATION], 4 * 3 + 7)


class ParallelLinksTests(tp.ProjectionCase):

    def project(self, workers: int | None) -> tuple[dict, list, dict]:
        held: dict = {}
        serial = P.Projector._project_links

        def links(projector):
            if workers is None:
                serial(projector)
            else:
                held["stats"] = fit.parallel_links(projector, workers)
            held["links"] = projector._db.execute(
                "SELECT * FROM links ORDER BY seq").fetchall()

        with tempfile.TemporaryDirectory() as scratch, \
                P.MirrorCaptures(self.mirror_path) as captures, \
                mock.patch.object(P.Projector, "_project_links", links):
            projector = P.Projector(self.schema, {"portal_api": captures},
                                    normalizer=tp.normalizer, scratch_dir=scratch)
            rows = projector.project()
            report = {"counts": projector.report.counts,
                      "gaps": {k: {**g, "handles": sorted(g.get("handles", ()))}
                               for k, g in projector.report.gaps.items()},
                      "parts": {u: p.as_dict() for u, p in projector.report.parts.items()},
                      "unattributed": projector.report.unattributed}
            self.assertEqual(os.listdir(scratch), [])   # parts and work file removed
        return rows, held, report

    def test_worker_link_phase_writes_the_serial_links_and_rows(self):
        rows, held, report = self.project(None)
        self.assertTrue(held["links"])
        self.assertTrue(rows["links"])
        for workers in (2, 3):
            with self.subTest(workers=workers):
                par_rows, par_held, par_report = self.project(workers)
                self.assertGreater(par_held["stats"]["workers"], 1)
                self.assertGreaterEqual(par_held["stats"]["jobs"], 2)
                self.assertEqual(par_held["links"], held["links"])
                self.assertEqual(par_rows, rows)
                self.assertEqual(par_report, report)

    def test_one_worker_runs_the_serial_phase_in_the_build(self):
        rows, held, _ = self.project(None)
        one_rows, one_held, _ = self.project(1)
        self.assertEqual(one_held["stats"]["workers"], 1)
        self.assertEqual((one_rows, one_held["links"]), (rows, held["links"]))


class WholeKeyCandidatesTests(unittest.TestCase):
    """A foreign key of several pairs: the candidates found on every pair at once are the
    serial candidates that match on every pair, in the serial order (the only ones its
    link spec keeps), however few values one pair holds."""

    def stub(self, rows):
        db = sqlite3.connect(":memory:", isolation_level=None)
        db.execute("CREATE TABLE k (sig INTEGER NOT NULL, norm TEXT NOT NULL, "
                   "vid INTEGER NOT NULL)")
        db.execute("CREATE INDEX k_by_norm ON k(sig, norm, vid)")
        db.execute("CREATE INDEX k_by_vid ON k(sig, vid)")
        db.execute("CREATE TABLE cand (a INTEGER NOT NULL, i INTEGER NOT NULL, "
                   "b INTEGER NOT NULL)")
        db.executemany("INSERT INTO k VALUES (?, ?, ?)", rows)
        self.addCleanup(db.close)
        return mock.Mock(_db=db, batch=7, _tick=lambda: None, _fit_whole=True)

    def test_whole_key_candidates_are_the_serial_full_matches(self):
        import random
        for seed, pairs in ((1, 2), (2, 3), (3, 4)):
            rng = random.Random(seed)
            rows = []
            for side, base in ((0, 1), (1, 10)):
                for vid in range(1 + side * 1000, 301 + side * 1000):
                    for n in range(pairs):
                        if rng.random() < 0.08:
                            continue                      # this version lacks the pair
                        spread = (2, 3, 25, 60)[n]          # pair 0: two values only
                        rows.append((base + n, str(rng.randrange(spread)), vid))
            joins = [(1 + n, 10 + n) for n in range(pairs)]
            stub = self.stub(rows)
            serial = list(fit._SERIAL_CANDIDATES(stub, joins))
            expected = []
            for vid, matches in serial:
                held = {}
                for index, other in matches:
                    held.setdefault(other, set()).add(index)
                kept = [(i, b) for i, b in matches if len(held[b]) == pairs]
                if kept:
                    expected.append((vid, kept))
            with self.subTest(pairs=pairs):
                self.assertTrue(expected)
                self.assertLess(sum(map(len, (m for _, m in expected))),
                                sum(map(len, (m for _, m in serial))) // 2)
                self.assertEqual(list(fit._candidates(stub, joins)), expected)
                stub._fit_whole = False      # any other link spec: the serial candidates
                self.assertEqual(list(fit._candidates(stub, joins)), serial)

    def test_only_a_several_pair_foreign_key_is_found_whole(self):
        seen = []

        def pairs(self, lid, link, index, lb):
            seen.append(self._fit_whole)
        projector = mock.Mock(_fit_whole=False)
        with mock.patch.object(fit, "_SERIAL_PAIRS", pairs):
            for kind, count in (("fk", 2), ("fk", 1), ("same_record", 2), ("text_match", 3)):
                fit._link_pairs(projector, "L", {}, 0,
                                {"kind": kind, "pairs": [{}] * count})
        self.assertEqual(seen, [True, False, False, False])
        self.assertFalse(projector._fit_whole)


class ResumeTests(tp.ProjectionCase):
    """A unit layer build stopped at any point after its load resumes from its last
    checkpoint (``unit_layer_fit.Resume``) and writes the layer an uninterrupted build
    writes, row for row: stopped among the link jobs (the finished ones are not run
    again), while their links go in, and in a phase after the links; the load is not
    run again. A checkpoint of another identity, or one another build holds, is
    refused and kept."""

    IDENTITY = {"plan": "p1", "schema": "s1"}

    def build(self, scratch: Path, resume_dir: Path | None, *, stop: dict | None = None,
              identity: dict | None = None) -> tuple[dict, dict]:
        from ontology.store import StoreBuilder
        out = scratch / "layer.sqlite"
        calls = {"bindings": 0, "jobs": 0}
        serial_binding = P.Projector._project_binding
        job_done = fit.Resume.job_done
        values = P.Projector._project_values
        absorb = fit._absorb

        def counted_binding(self, *args, **kwargs):
            calls["bindings"] += 1
            return serial_binding(self, *args, **kwargs)

        def counted_job(self, out_):
            calls["jobs"] += 1
            job_done(self, out_)
            if stop and stop.get("after_jobs") == calls["jobs"]:
                raise KeyboardInterrupt("stopped among the jobs")

        def stopped_values(self, builder):
            if stop and stop.get("values"):
                raise KeyboardInterrupt("stopped after the links")
            return values(self, builder)

        merged = [0]

        def stopped_absorb(report, other):
            merged[0] += 1
            if stop and stop.get("merge") == merged[0]:
                raise KeyboardInterrupt("stopped while the links go in")
            return absorb(report, other)

        resume = (fit.Resume(resume_dir, identity or self.IDENTITY)
                  if resume_dir is not None else None)
        stats: dict = {}
        try:
            with P.MirrorCaptures(self.mirror_path) as captures, \
                    mock.patch.object(P.Projector, "_project_binding", counted_binding), \
                    mock.patch.object(fit.Resume, "job_done", counted_job), \
                    mock.patch.object(P.Projector, "_project_values", stopped_values), \
                    mock.patch.object(fit, "_absorb", stopped_absorb), \
                    fit.fitted(self.schema, workers=2, resume=resume) as seen:
                projector = P.Projector(self.schema, {"portal_api": captures},
                                        normalizer=tp.normalizer, scratch_dir=scratch)
                builder = StoreBuilder(out, foreign_keys=False)
                P.open_generation(builder)
                try:
                    projector.project_into(builder)
                    path = builder.keep_as_part()
                except BaseException:
                    builder.abort()
                    raise
                stats = dict(seen["links"] or {})
                report = {"counts": dict(projector.report.counts),
                          "gaps": {k: {**g, "handles": sorted(g.get("handles", ()))}
                                   for k, g in projector.report.gaps.items()}}
        finally:
            if resume is not None:
                resume.close()
        conn = sqlite3.connect(path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND sql NOT LIKE "
            "'CREATE VIRTUAL%' ORDER BY name")]
        rows = {}
        for t in tables:   # rowid tables with their rowids; the others in key order
            try:
                rows[t] = conn.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid").fetchall()
            except sqlite3.OperationalError:
                rows[t] = sorted(conn.execute(f"SELECT * FROM {t}").fetchall(), key=repr)
        conn.close()
        return {"rows": rows, "report": report, "stats": stats}, calls

    def test_a_stopped_build_resumes_to_the_uninterrupted_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a").mkdir()
            plain, _ = self.build(Path(tmp) / "a", None)
            whole_dir = Path(tmp) / "b"
            whole_dir.mkdir()
            whole, _ = self.build(whole_dir, whole_dir / "resume")
            self.assertEqual(whole["rows"], plain["rows"])
            self.assertEqual(whole["report"], plain["report"])
            jobs = whole["stats"]["jobs"]
            self.assertGreaterEqual(jobs, 3)
            for name, stop in (("among the jobs", {"after_jobs": 2}),
                               ("while the links go in", {"merge": jobs - 1}),
                               ("after the links", {"values": True})):
                with self.subTest(stop=name):
                    scratch = Path(tmp) / f"s-{len(name)}"
                    scratch.mkdir()
                    resume_dir = scratch / "resume"
                    with self.assertRaises(KeyboardInterrupt):
                        self.build(scratch, resume_dir, stop=stop)
                    self.assertTrue((resume_dir / "manifest.json").is_file())
                    with self.assertRaises(fit.ResumeRefused):   # another identity
                        self.build(scratch, resume_dir, identity={"plan": "p2"})
                    resumed, calls = self.build(scratch, resume_dir)
                    self.assertEqual(calls["bindings"], 0)      # the load not run again
                    if "after_jobs" in stop:
                        self.assertEqual(resumed["stats"]["resumed_jobs"], 2)
                        self.assertEqual(calls["jobs"], jobs - 2)
                    else:
                        self.assertEqual(calls["jobs"], 0)
                    self.assertEqual(resumed["rows"], plain["rows"])
                    self.assertEqual(resumed["report"], plain["report"])

    def test_a_checkpoint_another_build_holds_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            held = fit.Resume(Path(tmp) / "r", self.IDENTITY)
            try:
                with self.assertRaises(fit.ResumeRefused) as caught:
                    fit.Resume(Path(tmp) / "r", self.IDENTITY)
                self.assertIn("resume_dir_in_use", str(caught.exception))
            finally:
                held.close()

    def test_a_fresh_start_removes_the_earlier_runs_working_files(self):
        """SCHEMA-003: a fresh start over an earlier run's checkpoint removes the working
        and builder files its manifest names (and only those)."""
        with tempfile.TemporaryDirectory() as tmp:
            files = {k: Path(tmp, f"{k}.sqlite") for k in ("work", "builder")}
            other = Path(tmp, "unrelated.sqlite")
            for path in (*files.values(), other, Path(f"{files['builder']}-journal")):
                path.write_bytes(b"x")
            first = fit.Resume(Path(tmp) / "r", self.IDENTITY)
            first.manifest.update(phase="loaded", **{k: str(v) for k, v in files.items()})
            first._write()
            first.close()
            fit.Resume(Path(tmp) / "r", {**self.IDENTITY, "plan": "another"},
                       fresh=True).close()
            self.assertEqual([p for p in files.values() if p.exists()], [])
            self.assertFalse(Path(f"{files['builder']}-journal").exists())
            self.assertTrue(other.exists())
            self.assertFalse(Path(tmp, "r", "manifest.json").exists())


class ParallelUnitsTests(tp.ProjectionCase):
    """A unit layer's units projected by worker processes, a task each, merged as they
    end (``unit_layer_fit.parallel_units``) hold the logical rows the one projector of the
    units writes: for every unit and for part of them; a build stopped among the merges
    resumes merging only the tasks not merged, to the same rows."""

    def layer(self, scratch: Path, units, *, mode: str, resume_dir: Path | None = None,
              stop_at: int | None = None):
        from ontology import project_incremental as pi
        from ontology import project_parallel as par
        from ontology.store import StoreBuilder
        from scripts.schema_migration_proof import logical_rows
        the_plan = pi.plan(self.schema)
        scope = set().union(*(the_plan.units[u] for u in units))
        out = scratch / f"layer-{mode}.sqlite"
        merges = [0]
        merge_rows = par._merge_rows

        def stopping(builder, main, links):
            merges[0] += 1
            if stop_at is not None and merges[0] == stop_at:
                raise KeyboardInterrupt("stopped among the merges")
            return merge_rows(builder, main, links)
        resume = fit.Resume(resume_dir, {"units": sorted(units)}) if resume_dir else None
        seen = {}
        try:
            with mock.patch.dict(os.environ, {fit.UNITS_ENV: mode}), \
                    mock.patch.object(par, "_merge_rows", stopping), \
                    fit.fitted(self.schema, workers=3, resume=resume) as seen, \
                    P.MirrorCaptures(self.mirror_path) as captures:   # as the build opens it
                projector = P.Projector(self.schema, {"portal_api": captures},
                                        normalizer=tp.normalizer, scratch_dir=scratch,
                                        scope=scope, emit=units)
                builder = StoreBuilder(out, foreign_keys=False)
                P.open_generation(builder)
                try:
                    projector.project_into(builder)
                    path = builder.keep_as_part()
                except BaseException:
                    builder.abort()
                    raise
        finally:
            if resume is not None:
                resume.close()
        return logical_rows(str(path)), dict(seen.get("units") or {}), merges[0]

    def test_units_on_workers_write_the_one_projectors_rows_and_resume(self):
        from ontology import project_incremental as pi
        every = sorted(pi.plan(self.schema).units)
        some = every[::2]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, units in (("every", every), ("some", some)):
                with self.subTest(units=name):
                    scratch = root / name
                    scratch.mkdir()
                    one, _, _ = self.layer(scratch, units, mode="serial")
                    many, stats, merges = self.layer(scratch, units, mode="parallel")
                    self.assertGreaterEqual(stats.get("tasks", 0), 2, stats)
                    self.assertTrue(any(one.values()))
                    self.assertEqual(many, one)
            scratch = root / "resume"
            scratch.mkdir()
            one, _, _ = self.layer(scratch, every, mode="serial")
            with self.assertRaises(KeyboardInterrupt):
                self.layer(scratch, every, mode="parallel", resume_dir=scratch / "r",
                           stop_at=3)
            resumed, stats, merges = self.layer(scratch, every, mode="parallel",
                                                resume_dir=scratch / "r")
            self.assertGreater(stats["resumed_units"], 0)
            self.assertEqual(resumed, one)


class CandidateBudgetTests(tp.ProjectionCase):
    """A link type whose key join would write more candidates than the budget stops the
    build before writing any, naming the link type and the count; a budget that is not a
    positive integer is refused."""

    def run_links(self, budget: str) -> None:
        with tempfile.TemporaryDirectory() as scratch, \
                P.MirrorCaptures(self.mirror_path) as captures, \
                mock.patch.dict(os.environ, {fit.CANDIDATE_BUDGET_ENV: budget}), \
                fit.fitted(self.schema, workers=1):
            projector = P.Projector(self.schema, {"portal_api": captures},
                                    normalizer=tp.normalizer, scratch_dir=scratch)
            projector.project()

    def test_an_over_budget_join_stops_the_build_naming_it(self):
        self.run_links("100000")
        with self.assertRaises(fit.OverBudget) as caught:
            self.run_links("1")
        self.assertRegex(str(caught.exception),
                         r"^link_candidates_over_budget:[^:]+#\d+:\d+>1$")
        for bad in ("0", "-5", "lots"):
            with self.subTest(budget=bad), self.assertRaises(fit.OverBudget):
                self.run_links(bad)


class FittedCommandTests(unittest.TestCase):

    def test_a_build_without_the_people_exclusion_is_refused_before_reading(self):
        # unavailable, the people directory builds no Person and no link to one
        # (the lp2 migration served 0 of every *.with_person link)
        from ontology import step_layer_workers, unit_layer_workers
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLX_EXCLUDED_PEOPLE")}
        args = ["build", "--from-ref", "HEAD", "--state-dir", "/nonexistent", "--out",
                "/nonexistent/out.sqlite"]
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("ontology.step_migrate.plan") as plan:
            self.assertEqual(unit_layer_workers.main(args), 4)
            self.assertEqual(step_layer_workers.main(args), 4)
            plan.assert_not_called()

    def test_no_derivation_module_imports_the_fitted_build(self):
        from ontology.project import derivation_modules
        modules = derivation_modules()
        self.assertIn("ontology.project", modules)
        self.assertNotIn("ontology.unit_layer_fit", modules)
        self.assertNotIn("ontology.unit_layer_workers", modules)
        self.assertNotIn("ontology.step_layer_workers", modules)
        self.assertNotIn("ontology.build_budget", modules)


if __name__ == "__main__":
    unittest.main()
