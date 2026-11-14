"""F3 (``operating.pg.f3_collapse``) on hand-made pre-F1/F2 rows in a throwaway Postgres:
a run of one record's versions differing only in stamps folds into its last version from
its first start (values, identifiers, events with it); a link's per-pair rows fold into one
row per stretch; a gap, another sighting, overlapping rows, a review or a step link's row
is left as it is; a second run changes nothing; no other row is touched (N = 1/10/60)."""
from __future__ import annotations

import collections
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import unittest.mock

from operating.tests import pg_testdb


# Stage A over a made-up capture source (module level: worker processes import it).
Rec = collections.namedtuple("Rec", "row l1_handle audience_key")


class FakeStamps:
    """Declared stamps of a made-up relation: the ``seen`` field is the stamp."""

    def sighting_key(self, row):
        return json.dumps({k: v for k, v in row.items() if k != "seen"}, sort_keys=True)


class FakeCaptures:
    """A capture source over a JSON file of [key, row, handle] (read whole each time),
    with the ``sample`` hook the real sources apply to their raw rows."""

    def __init__(self, path):
        self.path, self.sample = path, None

    def records(self, relations):
        raw = [tuple(r) for r in json.loads(Path(self.path).read_text())]

        def stream():
            for key, row, handle in (raw if self.sample is None else self.sample(raw)):
                yield Rec(row, handle, "api:r")
        return {relations[0]: stream()}


def open_fake(source, path):
    return FakeCaptures(path)


DECLARED = {("portal_api", "r"): FakeStamps()}



def t(n: int) -> str:
    return f"2026-10-{1 + n // 24:02d}T{n % 24:02d}:00:00.000000Z"


class F3CollapseTests(unittest.TestCase):

    def setUp(self):
        import psycopg
        self.name, self.dsn = pg_testdb.create_db()
        self.addCleanup(pg_testdb.drop_db, self.name)
        from operating.pg.migrate import migrate
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            migrate(pg)
        self.ver = 0
        self.eid = {}   # version -> its event's id
        undo = tempfile.TemporaryDirectory()
        self.addCleanup(undo.cleanup)
        self.undo = undo.name

    def sql(self, statement, params=None):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            cur = pg.execute(statement, params)
            return cur.fetchall() if cur.description else None

    def version(self, obj, handle, start, end, sighting, relation="portal_api:r",
                value="v", event_basis="source_recorded", also=()):
        """One object version with one value, one identifier and one event (its id the
        projector's, ``event_id_of``; ``also``: other objects it names, as a qualifier
        reached through a link does), and its sighting (None: no stamps declared)."""
        from ontology.project import event_id_of
        self.ver += 1
        v = self.ver
        self.eid[v] = event_id_of("E", handle, obj, "f")
        self.sql("INSERT INTO ontology.object_versions VALUES (%s,%s,%s,'d','l','m',%s,%s)",
                 (v, obj, handle, start, end))
        self.sql("INSERT INTO ontology.value_sources (src, ver, binding_id, digest, lineage, "
                 "recorded_at, superseded_at) VALUES (%s,%s,'b','d','l',%s,%s)",
                 (v, v, start, end))
        self.sql("INSERT INTO ontology.value_cells (src, field, kind, value) VALUES "
                 "(%s, 1, 1, %s)", (v, value))
        self.sql("INSERT INTO ontology.identifiers VALUES ('k',%s,'raw','r',%s,'p',%s,'f','l',"
                 "'m',%s,%s)", (f"n{v}", obj, handle, start, end))
        self.sql("INSERT INTO ontology.events (event_id, event_type, time_basis, l1_handle, "
                 "field_path, digest, lineage, markings, recorded_at, superseded_at) VALUES "
                 "(%s,'E',%s,%s,'row.f','d','l','m',%s,%s)",
                 (self.eid[v], event_basis, handle, start, end))
        self.sql("INSERT INTO ontology.event_objects VALUES (%s, %s, 'q')", (self.eid[v], obj))
        for other in also:
            self.sql("INSERT INTO ontology.event_objects VALUES (%s, %s, 'item')",
                     (self.eid[v], other))
        if sighting is not None:
            self.sql("CREATE TABLE IF NOT EXISTS ontology.f3_sightings (handle text "
                     "PRIMARY KEY, relation text NOT NULL, sighting text NOT NULL)")
            self.sql("INSERT INTO ontology.f3_sightings VALUES (%s,%s,%s)",
                     (handle, relation, sighting))
        return v

    def link(self, link_id, start, end, handle, *, to="T:1", status="OPEN", review=None):
        reason = ('[{"feature":"status","agrees":true,"value":"%s","citations":[{"l1_handle":'
                  '"%s","digest":"x"}]}]' % (status, handle))
        self.sql("INSERT INTO ontology.link_rows (link_id, link_type, from_object, to_object, "
                 "tier, binding_kind, why_not, reviewed_by, markings, recorded_at, "
                 "superseded_at, reason, evidence_handles) VALUES "
                 "(%s,'A.has','A:1',%s,'verified','join','[]',%s,'m',%s,%s,%s,%s)",
                 (link_id, to, review, start, end, reason, f'["{handle}"]'))

    def run_f3(self, what="all"):
        from operating.pg import f3_collapse
        self.no_sightings()
        self.sql("ANALYZE")
        return f3_collapse.run(self.dsn, workers=2, what=what, log=io.StringIO(),
                               undo_dir=self.undo)

    def no_sightings(self):
        """Stage A as finished over the sightings made by hand (none: an empty table)."""
        self.sql("CREATE TABLE IF NOT EXISTS ontology.f3_sightings (handle text PRIMARY KEY, "
                 "relation text NOT NULL, sighting text NOT NULL)")
        self.sql("INSERT INTO ontology.load_state (name, value) VALUES "
                 "('f3:sightings:done', '{}') ON CONFLICT (name) DO NOTHING")

    def dump(self):
        return {n: [r[0] for r in self.sql(f"SELECT t::text FROM ontology.{n} t ORDER BY 1")]
                for n in ("object_versions", "value_sources", "value_cells", "identifiers",
                          "events", "event_objects", "link_rows")}

    def table(self, name, cols="*", order="1"):
        return self.sql(f"SELECT {cols} FROM ontology.{name} ORDER BY {order}")

    def test_a_restamp_run_folds_into_its_last_version_and_nothing_else_moves(self):
        a = [self.version("A:1", f"h{i}", t(i), t(i + 1), "s1", value=f"u{i}")
             for i in range(3)]                                       # restamps
        b = self.version("A:1", "h3", t(3), None, "s2", value="u3")   # a real change
        gap = [self.version("A:2", "g0", t(0), t(1), "s1"),
               self.version("A:2", "g1", t(2), None, "s1")]           # not contiguous
        over = [self.version("A:3", "o0", t(0), t(2), "s1"),
                self.version("A:3", "o1", t(1), None, "s1")]          # overlapping
        plain = [self.version("A:4", "p0", t(0), t(1), None),
                 self.version("A:4", "p1", t(1), None, None)]         # no stamps declared
        cap = self.version("A:5", "c0", t(0), t(1), "s1", event_basis="capture_observed")
        self.version("A:5", "c1", t(1), None, "s1")
        before = {n: self.table(n) for n in ("object_versions", "value_sources",
                                              "value_cells", "identifiers", "events")}
        report = self.run_f3()
        self.assertEqual(report["left"], 0)
        versions = self.table("object_versions", "ver, object_id, l1_handle, recorded_at, "
                                                 "superseded_at", "ver")
        kept = {r[0] for r in versions}
        # A:1: three restamps -> the last (h2) from the first's start; the change stays
        self.assertNotIn(a[0], kept)
        self.assertNotIn(a[1], kept)
        self.assertIn((a[2], "A:1", "h2", t(0), t(3)), versions)
        self.assertIn((b, "A:1", "h3", t(3), None), versions)
        for v in [*gap, *over, *plain]:
            self.assertIn(v, kept)
        self.assertNotIn(cap, kept)   # A:5 folds too: c0 + c1 -> c1
        # the dropped versions' values, identifiers and events went with them
        srcs = self.table("value_sources", "src, recorded_at", "src")
        self.assertIn((a[2], t(0)), srcs)
        self.assertNotIn(a[0], {s for s, _ in srcs})
        self.assertEqual({c[0] for c in self.table("value_cells", "src")},
                         {s for s, _ in srcs})
        ids = self.table("identifiers", "l1_handle, recorded_at")
        self.assertIn(("h2", t(0)), ids)
        self.assertFalse({"h0", "h1"} & {h for h, _ in ids})
        events = self.table("events", "event_id, l1_handle, recorded_at, time_basis")
        self.assertIn((self.eid[a[2]], "h2", t(0), "source_recorded"), events)
        self.assertFalse({self.eid[a[0]], self.eid[a[1]]} & {e[0] for e in events})
        # a capture-observed change event keeps citing the capture that first held it
        self.assertIn((self.eid[cap], "c0", t(0), "capture_observed"), events)
        self.assertEqual({r[0] for r in self.table("event_objects", "event_id")},
                         {e[0] for e in events})
        # rows of the objects left alone are unchanged
        for name, rows in before.items():
            now = self.table(name)
            untouched = [r for r in rows if not ({"h0", "h1", "h2", "c0", "c1"} & set(map(
                str, r))) and r[0] not in (a[0], a[1], a[2], cap, cap + 1, self.eid[cap + 1])]
            for row in untouched:
                if name in ("object_versions", "value_sources") and row[1] in (a[2], cap + 1):
                    continue
                self.assertIn(row, now, name)
        # a second run folds nothing more
        again = self.table("object_versions")
        self.assertEqual(self.run_f3()["totals"].get("versions_deleted", 0), 0)   # resumed
        self.sql("DELETE FROM ontology.load_state WHERE name LIKE 'f3:%%'")
        self.assertEqual(self.run_f3()["totals"].get("versions_deleted", 0), 0)   # re-run
        self.assertEqual(self.table("object_versions"), again)

    def test_an_event_naming_a_linked_object_folds_with_its_own_objects_versions(self):
        """Rehearsal 3 (11-13): ``ItemLedgerEntry.posted`` names the entry and its item
        (Product, through a link); F3 left such events on the dropped versions while a
        build folds them, so 4 events and 3 objects' event rows differed. An event is the
        version's object's own by its id (``event_id_of``), whatever else it names;
        another object's event citing the same handle stays as it is."""
        a = [self.version("A:1", f"h{i}", t(i), t(i + 1), "s1", also=("P:1",))
             for i in range(3)]
        self.version("A:1", "h3", t(3), None, "s2", also=("P:1",))
        # B:1's event read off A:1's record (same handle), naming A:1 too: never A:1's
        from ontology.project import event_id_of
        foreign = event_id_of("E", "h0", "B:1", "f")
        self.sql("INSERT INTO ontology.events (event_id, event_type, time_basis, l1_handle, "
                 "field_path, digest, lineage, markings, recorded_at, superseded_at) VALUES "
                 "(%s,'E','source_recorded','h0','row.f','d','l','m',%s,%s)",
                 (foreign, t(0), t(1)))
        self.sql("INSERT INTO ontology.event_objects VALUES (%s,'B:1','q'), (%s,'A:1','x')",
                 (foreign, foreign))
        before = self.dump()
        self.run_f3()
        events = self.table("events", "event_id, l1_handle, recorded_at, superseded_at")
        self.assertIn((self.eid[a[2]], "h2", t(0), t(3)), events)
        self.assertFalse({self.eid[a[0]], self.eid[a[1]]} & {e[0] for e in events})
        objects = self.table("event_objects", "event_id, object_id, qualifier")
        self.assertIn((self.eid[a[2]], "P:1", "item"), objects)
        self.assertIn((self.eid[a[2]], "A:1", "q"), objects)
        self.assertFalse({self.eid[a[0]], self.eid[a[1]]} & {o[0] for o in objects})
        self.assertIn((foreign, "h0", t(0), t(1)), events)
        self.assertEqual(sorted(r for r in objects if r[0] == foreign),
                         [(foreign, "A:1", "x"), (foreign, "B:1", "q")])
        # the undo puts every row back
        from operating.pg import f3_collapse
        f3_collapse.undo(self.dsn, self.undo, log=io.StringIO())
        self.assertEqual(self.dump(), before)

    def test_a_links_per_pair_rows_fold_into_one_row_per_stretch(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                to = f"T:{n}"
                for i in range(n):
                    self.link(f"L:{n}-{i}", t(i), t(i + 1) if i < n - 1 else None,
                              f"h{n}-{i}", to=to)
                rows = self.sql("SELECT link_id, recorded_at, superseded_at, evidence_handles"
                                " FROM ontology.links WHERE to_object = %s", (to,))
                if n > 1:
                    self.run_f3("links")
                    self.sql("DELETE FROM ontology.load_state WHERE name LIKE 'f3:%%'")
                    rows = self.sql("SELECT link_id, recorded_at, superseded_at, "
                                    "evidence_handles FROM ontology.links WHERE "
                                    "to_object = %s", (to,))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][1:], (t(0), None, f'["h{n}-{n - 1}"]'))
                # the kept row is the stretch's last under its own id (read elsewhere)
                self.assertEqual(rows[0][0], f"L:{n}-{n - 1}")

    def test_a_link_change_a_gap_an_overlap_a_review_or_a_step_link_is_left_alone(self):
        self.link("L:c0", t(0), t(1), "c0", to="T:c")
        self.link("L:c1", t(1), None, "c1", to="T:c", status="SHUT")       # value changed
        self.link("L:g0", t(0), t(1), "g0", to="T:g")
        self.link("L:g1", t(2), None, "g1", to="T:g")                     # gap
        self.link("L:o0", t(0), t(2), "o0", to="T:o")
        self.link("L:o1", t(1), t(3), "o1", to="T:o")
        self.link("L:o2", t(3), None, "o2", to="T:o")                     # two series
        self.link("L:r0", t(0), t(1), "r0", to="T:r", review="someone")
        self.link("L:r1", t(1), None, "r1", to="T:r")                     # reviewed
        self.link("mention:m0", t(0), t(1), "m0", to="T:m")
        self.link("mention:m1", t(1), None, "m1", to="T:m")               # a step's ids
        self.link("L:f0", t(0), t(1), "f0", to="T:f")
        self.link("L:f1", t(1), None, "f1", to="T:f")                     # folds
        before = self.table("link_rows", "link_id, recorded_at, superseded_at")
        from operating.pg import f3_collapse
        with self.assertRaisesRegex(RuntimeError, "f3_sightings_missing"):
            f3_collapse.run(self.dsn, undo_dir=self.undo, log=io.StringIO())
        report = self.run_f3("links")
        after = self.table("link_rows", "link_id, recorded_at, superseded_at")
        self.assertEqual(report["totals"]["deleted"], 1)
        self.assertEqual([r for r in before if not r[0].startswith("L:f")],
                         [r for r in after if r[0] in {b[0] for b in before}
                          and not r[0].startswith("L:f")])
        self.assertEqual(len(after), len(before) - 1)

    def test_a_lead_a_step_link_names_by_id_is_never_folded_away(self):
        # a confirmed lead (``ontology/key_links.py``) names the lead row it rests on;
        # ``ontology/link_keys.py`` reads that row's keys by id
        for p in ("a", "b"):
            self.link(f"L:{p}0", t(0), t(1), f"{p}0", to=f"T:{p}")
            self.link(f"L:{p}1", t(1), t(2), f"{p}1", to=f"T:{p}")
            self.link(f"L:{p}2", t(2), None, f"{p}2", to=f"T:{p}")
        named = ('[{"feature":"stored_lead","agrees":true,"link_id":"L:a1"},'
                 '{"feature":"confirming_path","agrees":true,"via":[],"twin":false}]')
        self.sql("INSERT INTO ontology.link_rows (link_id, link_type, from_object, to_object, "
                 "tier, binding_kind, why_not, markings, recorded_at, reason, evidence_handles) VALUES "
                 "('keyjoin:x','A.has','A:1','T:a','verified','keyjoin','[]','m',%s,%s,'[]')",
                 (t(1), named))
        self.sql("INSERT INTO ontology.objects (object_id, object_type, binding_id, "
                 "native_key, audience_key, first_recorded_at) VALUES "
                 "('A:1', 'A', 'b', 'A:1', 'api:r', %s)", (t(0),))
        from operating.pg import f3_collapse
        est = f3_collapse.estimate(self.dsn, percent=100, log=io.StringIO())
        before = self.table("link_rows", "link_id, recorded_at, superseded_at")
        report = self.run_f3("links")
        after = self.table("link_rows", "link_id, recorded_at, superseded_at")
        self.assertEqual(report["totals"]["deleted"], 2)   # only the b stretch folds
        self.assertEqual(est["deletable_rows"], 2)
        self.assertEqual([r for r in before if not r[0].startswith("L:b")],
                         [r for r in after if not r[0].startswith("L:b")])
        self.assertEqual([r for r in after if r[0].startswith("L:b")], [("L:b2", t(0), None)])

    def test_a_conflict_citing_folded_versions_folds_once_the_objects_have(self):
        """A link's property conflict names the end versions it compares (``from``/``to``
        with their handles). The projector cites the folded version, so the per-pair rows
        over a restamp run are one stretch once the objects stage folded that run; over
        a real change they stay apart."""
        for spaced in (False, True):
            with self.subTest(spaced=spaced):
                self.sql("TRUNCATE ontology.object_versions, ontology.value_sources, "
                         "ontology.value_cells, ontology.identifiers, ontology.events, "
                         "ontology.event_objects, ontology.link_rows")
                self.sql("DROP TABLE IF EXISTS ontology.f3_sightings, ontology.f3_handle_map, "
                         "ontology.f3_batches")
                self.sql("DELETE FROM ontology.load_state WHERE name LIKE 'f3:%%'")
                self._conflict_folds(spaced)

    def _conflict_folds(self, spaced):
        for i in range(3):
            self.version("A:1", f"h{i}", t(i), t(i + 1), "s1")
        self.version("A:1", "h3", t(3), None, "s2")

        def conflict(link_id, start, end, handle):
            why = ('[{"feature":"property_conflict","agrees":false,"property":"name",'
                   '"citations":[{"l1_handle":"%s"}],"from":{"l1_handle":"%s",'
                   '"field_path":"row.name"},"to":{"l1_handle":"w0","field_path":"x"}}]'
                   % (handle, handle))
            if spaced:   # json.dumps' default separators: the remap gate reads both
                why = json.dumps(json.loads(why))
            self.sql("INSERT INTO ontology.link_rows (link_id, link_type, from_object, "
                     "to_object, tier, binding_kind, why_not, markings, recorded_at, "
                     "superseded_at, reason, evidence_handles) VALUES (%s,'A.same','A:1',"
                     "'B:1','lead','join',%s,'m',%s,%s,'[]','[]')",
                     (link_id, why, start, end))
        for i in range(4):
            conflict(f"L:x{i}", t(i), t(i + 1) if i < 3 else None, f"h{i}")
        self.run_f3()
        rows = self.sql("SELECT recorded_at, superseded_at, why_not FROM ontology.links "
                        "WHERE link_type = 'A.same' ORDER BY recorded_at")
        self.assertEqual([(r[0], r[1]) for r in rows], [(t(0), t(3)), (t(3), None)])
        self.assertIn('"h2"', rows[0][2])   # the stretch cites the version folded into

    def test_a_resumed_run_reads_the_batches_the_first_run_saved(self):
        """A run stopped part way (here: its third batch fails) and resumed after the
        statistics moved (F3's own deletes re-analyze the table) reads the batches the
        first run saved: every range is folded once; with ranges recomputed, marked
        indexes named other ranges and whole ranges were skipped while left said 0."""
        from unittest import mock
        from operating.pg import f3_collapse

        def pairs(prefix, count):
            for i in range(count):
                obj = f"A:{prefix}{i:03d}"
                for k in range(2):
                    self.sql("INSERT INTO ontology.link_rows (link_id, link_type, from_object,"
                             " to_object, tier, binding_kind, why_not, markings, recorded_at, "
                             "superseded_at, reason, evidence_handles) VALUES (%s,'A.has',%s,"
                             "'T:1','verified','join','[]','m',%s,%s,'[]','[]')",
                             (f"L:{obj}-{k}", obj, t(k), t(k + 1) if k == 0 else None))
        pairs("a", 600)
        self.no_sightings()
        self.sql("ANALYZE ontology.link_rows")
        real, calls = f3_collapse.run_batch, []

        def failing(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("stopped")
            return real(*args, **kwargs)
        with mock.patch.object(f3_collapse, "run_batch", failing), \
                self.assertRaisesRegex(RuntimeError, "stopped"):
            f3_collapse.run(self.dsn, workers=1, what="links", undo_dir=self.undo,
                            log=io.StringIO())
        saved = self.table("f3_batches")
        self.assertGreater(len(saved), 3)
        # the statistics move (coarser here): recomputed ranges 0 and 1 would now span
        # far past what the first run folded, and be skipped as marked
        self.sql("ALTER TABLE ontology.link_rows ALTER COLUMN from_object SET STATISTICS 10")
        self.sql("ANALYZE ontology.link_rows")
        report = f3_collapse.run(self.dsn, workers=2, what="links", undo_dir=self.undo,
                                 log=io.StringIO())
        self.assertEqual(report["left"], 0)
        self.assertEqual(self.table("f3_batches"), saved)
        unfolded = self.sql("SELECT from_object FROM ontology.link_rows GROUP BY 1 "
                            "HAVING count(*) > 1")
        self.assertEqual(unfolded, [])
        self.assertEqual(self.sql("SELECT count(*) FROM ontology.link_rows")[0][0], 600)

    def test_the_undo_puts_every_row_back(self):
        """Each batch writes its rows' before-images (and an update's new keys) to a zstd
        undo file before it commits; ``undo`` puts back every table exactly, newest batch
        first, and removes the marks. A run that writes without an undo directory, an
        undo after a delta layer applied since, or a missing undo file is refused."""
        from operating.pg import f3_collapse
        for i in range(3):
            self.version("A:1", f"h{i}", t(i), t(i + 1), "s1", value=f"u{i}")
        self.version("A:1", "h3", t(3), None, "s2")
        for i in range(4):
            self.link(f"L:x{i}", t(i), t(i + 1) if i < 3 else None, f"h{i}")
        before = self.dump()
        with self.assertRaisesRegex(RuntimeError, "f3_undo_dir_missing"):
            f3_collapse.run(self.dsn, log=io.StringIO())
        report = self.run_f3()
        self.assertGreater(report["totals"]["link_rows.deleted"], 0, report)
        self.assertGreater(report["totals"]["object_versions.deleted"], 0, report)
        self.assertNotEqual(self.dump(), before)
        self.assertTrue(all(name.endswith(".jsonl.zst") for name in os.listdir(self.undo)))
        # a delta layer applied after the run: its rows would be overwritten
        self.sql("INSERT INTO ontology.load_state (name, value, updated_at) VALUES "
                 "('delta:later.sqlite', '{}', now() + interval '1 minute')")
        with self.assertRaisesRegex(RuntimeError, "f3_undo_after_delta"):
            f3_collapse.undo(self.dsn, self.undo, log=io.StringIO())
        self.sql("DELETE FROM ontology.load_state WHERE name = 'delta:later.sqlite'")
        hidden = sorted(os.listdir(self.undo))[0]
        os.rename(os.path.join(self.undo, hidden), os.path.join(self.undo, "x"))
        with self.assertRaisesRegex(RuntimeError, "f3_undo_file_missing"):
            f3_collapse.undo(self.dsn, self.undo, log=io.StringIO())
        os.rename(os.path.join(self.undo, "x"), os.path.join(self.undo, hidden))
        out = f3_collapse.undo(self.dsn, self.undo, log=io.StringIO())
        self.assertGreater(out["undone"], 0)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.sql("SELECT count(*) FROM ontology.load_state WHERE name LIKE "
                                  "'f3:objects:%%' OR name LIKE 'f3:links:%%'")[0][0], 0)
        self.assertEqual(self.table("f3_handle_map"), [])
        # and a run after the undo folds the same again
        self.run_f3()
        self.assertNotEqual(self.dump(), before)

    def test_the_working_tables_are_logged_and_a_lost_stage_a_is_refused(self):
        from operating.pg import f3_collapse
        self.sql("CREATE UNLOGGED TABLE ontology.f3_sightings (handle text PRIMARY KEY, "
                 "relation text NOT NULL, sighting text NOT NULL)")
        self.sql("INSERT INTO ontology.load_state (name, value) VALUES "
                 "('f3:sightings:done', '{\"rows\": 5}')")
        with self.assertRaisesRegex(RuntimeError, "f3_sightings_lost: 0 rows"):
            f3_collapse.run(self.dsn, undo_dir=self.undo, log=io.StringIO())
        kinds = dict(self.sql("SELECT relname, relpersistence FROM pg_class WHERE relname IN "
                              "('f3_sightings', 'f3_handle_map', 'f3_batches')"))
        self.assertEqual(kinds, {"f3_sightings": "p", "f3_handle_map": "p", "f3_batches": "p"})

    def test_the_estimate_counts_what_a_run_deletes(self):
        """The read-only estimate (inline plan, no temp objects) counts the rows a links
        run deletes, over every object sampled (100%), and writes nothing."""
        from operating.pg import f3_collapse
        for n in (1, 10, 60):
            for i in range(n):
                self.link(f"L:{n}-{i}", t(i), t(i + 1) if i < n - 1 else None, f"h{n}-{i}",
                          to=f"T:{n}")
        self.link("L:c0", t(0), t(1), "c0", to="T:c")
        self.link("L:c1", t(1), None, "c1", to="T:c", status="SHUT")
        self.sql("INSERT INTO ontology.objects (object_id, object_type, binding_id, "
                 "native_key, audience_key, first_recorded_at) SELECT DISTINCT from_object, "
                 "'A', 'b', from_object, 'api:r', %s FROM ontology.link_rows", (t(0),))
        before = self.table("link_rows")
        est = f3_collapse.estimate(self.dsn, percent=100, log=io.StringIO())
        self.assertEqual(self.table("link_rows"), before)
        self.assertEqual((est["rows"], est["deletable_rows"]), (73, 68), est)
        self.assertEqual(self.run_f3("links")["totals"]["deleted"], est["deletable_rows"])
        with self.assertRaisesRegex(ValueError, "f3_percent_invalid"):
            f3_collapse.estimate(self.dsn, percent=0)

    def test_stage_a_in_key_slices_equals_one_reader(self):
        """Stage A cut into key slices over worker processes loads the very sightings one
        reader does (N = 1/10/60 versions), marks each slice and then the whole; a re-run
        skips marked slices; another slice count, an unknown or a missing source is
        refused; the object stage refuses until stage A finished."""
        from operating.pg import f3_collapse
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for n in (1, 10, 60):
            with self.subTest(n=n):
                rows = [[f'{{"id":{i // 3}}}', {"id": i // 3, "v": i % 2, "seen": i},
                         f"r|{i}"] for i in range(n)]
                path = Path(tmp.name) / f"c{n}.json"
                path.write_text(json.dumps(rows))
                got = {}
                for slices in (1, 3):
                    self.sql("DROP TABLE IF EXISTS ontology.f3_sightings")
                    self.sql("DELETE FROM ontology.load_state WHERE name LIKE 'f3:%%'")
                    out = f3_collapse.load_sightings(
                        self.dsn, {"portal_api": str(path)}, workers=2, slices=slices,
                        opener=open_fake, declared=DECLARED, log=io.StringIO())
                    self.assertEqual(out["rows"], n)
                    got[slices] = self.table("f3_sightings")
                    marks = [r[0] for r in self.sql(
                        "SELECT name FROM ontology.load_state WHERE name LIKE 'f3:%%' "
                        "ORDER BY 1")]
                    self.assertEqual(marks, sorted(
                        [f"f3:sightings:portal_api:r:{k}/{slices}" for k in range(slices)]
                        + ["f3:sightings:done"]))
                self.assertEqual(got[1], got[3])
                self.assertEqual(len({r[2] for r in got[1]}),
                                 len({json.dumps({"id": i // 3, "v": i % 2}) for i in range(n)}))
                again = f3_collapse.load_sightings(
                    self.dsn, {"portal_api": str(path)}, workers=2, slices=3,
                    opener=open_fake, declared=DECLARED, log=io.StringIO())
                self.assertEqual(again["rows"], n)
                self.assertEqual(self.table("f3_sightings"), got[3])
        with self.assertRaisesRegex(RuntimeError, "f3_slices_changed"):
            f3_collapse.load_sighting_slice(
                self.dsn, ("portal_api", str(path), "r", 0, 2), open_fake, DECLARED)
        with self.assertRaisesRegex(ValueError, "f3_source_unknown"):
            f3_collapse.load_sightings(self.dsn, {"portal_api": str(path), "nosuch": "x"},
                                       opener=open_fake, declared=DECLARED)
        with self.assertRaisesRegex(ValueError, "f3_sources_missing: bc"):
            f3_collapse.load_sightings(self.dsn, {"portal_api": str(path)}, opener=open_fake,
                                       declared={**DECLARED, ("bc", "x"): FakeStamps()})
        # out of budget: no slice starts, no done mark; the next run finishes
        self.sql("DROP TABLE ontology.f3_sightings")
        self.sql("DELETE FROM ontology.load_state WHERE name LIKE 'f3:%%'")
        short = f3_collapse.load_sightings(
            self.dsn, {"portal_api": str(path)}, workers=2, slices=3, opener=open_fake,
            declared=DECLARED, budget_minutes=0, log=io.StringIO())
        self.assertEqual((short["done"], short["left"]), (False, 3))
        with self.assertRaisesRegex(RuntimeError, "f3_sightings_incomplete"):
            f3_collapse.run(self.dsn, workers=1, what="objects", undo_dir=tmp.name,
                            log=io.StringIO())
        whole = f3_collapse.load_sightings(
            self.dsn, {"portal_api": str(path)}, workers=2, slices=3, opener=open_fake,
            declared=DECLARED, log=io.StringIO())
        self.assertEqual((whole["done"], whole["rows"]), (True, 60))


class F3CommandTests(unittest.TestCase):
    """The stage A command refuses a source given without its capture path before it
    connects anywhere."""

    def test_a_source_without_a_path_is_refused(self):
        from operating.pg import f3_collapse
        for bad in ("portal_api", "portal_api=", "=x"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit), \
                    unittest.mock.patch("sys.stderr", io.StringIO()):
                f3_collapse.main(["sightings", "--dsn", "host=/nonexistent", "--source", bad])


if __name__ == "__main__":
    unittest.main()
