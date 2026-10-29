"""Incremental projection by generation (``ontology/project_incremental.py``).

What must hold: a generation built incrementally from the last one equals a cold build
of the same captures (every row, link, citation, marking and tier, and the build
report: one content digest); an interrupted build leaves the committed generation
current and readable, and the next build recovers; the build stays inside a generous
time bound at fixture scale (diagnostic, not a target).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from integrations.source_mirror import AudienceMap, SourceMirror
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology import project_incremental as inc
from ontology.project import (CapturedRecord, MirrorCaptures, ProjectionError, Projector,
                              build_store)
from ontology.store import (ProjectionStore, ReadPolicy, StoreError, _TABLE_ORDER,
                            content_digest)
from ontology.tests import synthetic_captures
from ontology.tests.test_projection import (AUDIENCE, BASE, CATALOG, KEY, MARKINGS, OPS, T1,
                                            T2, Clock, FakeWorkbench, StaticCaptures,
                                            compiled_schema, normalizer)


def tables(path):
    """Every row of every store table (without the volatile build time)."""
    conn = sqlite3.connect(str(path))
    try:
        return {table: [tuple(row) for row in conn.execute(
                    f"SELECT * FROM {table} ORDER BY {order}")
                    if not (table == "store_meta" and row[0] in ("built_at", "content_digest"))]
                for table, order in _TABLE_ORDER}
    finally:
        conn.close()


def incremental_path(need):
    """A ``space`` guard that refuses a cold build where an incremental one can run: a
    world of a few rows changes most of its units at once, so the mode choice (its own
    test) would build it cold and leave the incremental path these tests pin unexercised."""
    if need["mode"] == "cold" and "incremental" in need["estimates"]:
        raise OSError("incremental_path_pinned")


class MirrorGenerationTests(unittest.TestCase):
    """The fixture Portal mirror (real ``SourceMirror``, injected transport) captured
    three times; a generation after each capture is compared with a cold build."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.fake, self.clock = FakeWorkbench(), Clock()
        self.mirror = SourceMirror(self.root / "mirror.sqlite",
                                   WorkbenchReadApiClient(BASE, KEY, fetch=self.fake),
                                   AudienceMap(AUDIENCE, tenant="plx"), clock=self.clock,
                                   retry_delay_seconds=0, page_size=2)
        self.addCleanup(self.mirror.close)
        self.schema = compiled_schema()
        self.state = self.root / "state"

    def capture(self, at):
        self.clock.now = at
        self.mirror.sync(OPS, relations=sorted(CATALOG), full=True)

    def generation(self, **options):
        with MirrorCaptures(self.root / "mirror.sqlite") as captures:
            return inc.build_generation(self.state, self.schema, {"portal_api": captures},
                                        normalizer=normalizer, clock=lambda: T2,
                                        **{"space": incremental_path, **options})

    def cold(self, name):
        with MirrorCaptures(self.root / "mirror.sqlite") as captures:
            return build_store(self.root / name, self.schema, {"portal_api": captures},
                               normalizer=normalizer, clock=lambda: T2)

    def assertSameAsCold(self, generation, name):
        report = self.cold(name)
        self.assertEqual(tables(generation.store), tables(self.root / name))
        self.assertEqual(generation.content_digest, report["content_digest"])

    def test_each_generation_equals_a_cold_build_of_the_same_captures(self):
        self.capture(T1)
        first = self.generation()
        self.assertEqual(first.mode, "cold")
        self.assertSameAsCold(first, "cold1.sqlite")

        # One purchase order and one of its lines change: only the units reading
        # those two relations (and what hops through them) are projected again.
        self.fake.set("public.app_purchase_order", "po-a", status="RECEIVED",
                      total_cost="36.00")
        self.fake.set("public.app_purchase_order_line", "l1", quantity="1510")
        self.capture(T2)
        second = self.generation()
        self.assertEqual(second.mode, "incremental")
        self.assertLess(second.units_rebuilt, second.units)
        self.assertNotEqual(second.content_digest, first.content_digest)
        self.assertSameAsCold(second, "cold2.sqlite")

        # A capture that changes nothing moves every row's last sighting, not its
        # content: nothing is projected again, and the generation still equals a cold
        # build (its freshness metadata is the new capture's).
        self.capture(T2 + timedelta(hours=1))
        third = self.generation()
        self.assertEqual((third.mode, third.units_rebuilt, third.bindings_read),
                         ("incremental", 0, 0))
        self.assertSameAsCold(third, "cold3.sqlite")

        # A receipt now names another purchase order: its text-join leads move.
        self.fake.set("public.app_receipt", "r2", po_reference="PO-3003")
        self.capture(T2 + timedelta(hours=2))
        fourth = self.generation()
        self.assertEqual(fourth.mode, "incremental")
        self.assertSameAsCold(fourth, "cold4.sqlite")
        self.assertEqual(inc.current_generation(self.state).id, fourth.id)

    def test_an_interrupted_build_keeps_the_previous_generation_and_the_next_recovers(self):
        self.capture(T1)
        first = self.generation()
        before = tables(first.store)
        self.fake.set("public.app_purchase_order", "po-b", status="CLOSED")
        self.capture(T2)

        class Stop(Exception):
            pass
        ticks = []

        def checkpoint():
            ticks.append(1)
            if len(ticks) == 4:
                raise Stop

        with self.assertRaises(Stop):
            self.generation(checkpoint=checkpoint, batch_size=1)
        # The committed generation is untouched, current, and readable.
        current = inc.current_generation(self.state)
        self.assertEqual((current.id, current.content_digest),
                         (first.id, first.content_digest))
        self.assertEqual(tables(first.store), before)
        conn = sqlite3.connect(str(first.store))
        self.assertEqual(content_digest(conn), first.content_digest)
        conn.close()
        store = ProjectionStore(first.store, ReadPolicy.from_audience_map(
            AudienceMap(AUDIENCE, tenant="plx"), marking_principals=MARKINGS))
        self.addCleanup(store.close)
        self.assertGreater(store.count(OPS, "PurchaseOrder"), 0)
        self.assertEqual(sorted(p.name for p in (self.state / "generations").iterdir()),
                         [first.id])

        # A failure at the commit point itself (CURRENT not replaced) changes nothing.
        real = inc._write_json

        def fail_on_current(path, value):
            if path.name == inc.CURRENT:
                raise OSError("disk full")
            real(path, value)

        with mock.patch.object(inc, "_write_json", side_effect=fail_on_current):
            with self.assertRaises(OSError):
                self.generation()
        self.assertEqual(inc.current_generation(self.state).id, first.id)

        # A killed build leaves an unfinished directory behind: the restart removes
        # it and builds from the committed generation.
        dead = self.state / "generations" / "99999999"
        dead.mkdir()
        (dead / "store.sqlite").write_bytes(b"half a store")
        recovered = self.generation()
        self.assertEqual(recovered.mode, "incremental")
        self.assertFalse(dead.exists())
        self.assertSameAsCold(recovered, "recovered.sqlite")
        # The previous generation stays on disk for readers that still hold it.
        self.assertTrue(first.store.exists())

    def test_a_second_build_on_the_same_state_dir_is_refused_while_one_runs(self):
        self.capture(T1)
        self.state.mkdir()
        with inc._Lock(self.state):
            with self.assertRaises(ProjectionError) as caught:
                self.generation()
        self.assertEqual(caught.exception.code, "generation_build_in_progress")

    def test_a_report_entry_outside_every_unit_makes_the_build_cold(self):
        # A projection hook that reports without naming its unit: the rows of a partial
        # build would be right but its report would lose that entry, so it is cold.
        self.capture(T1)
        self.generation()
        self.fake.set("public.app_purchase_order", "po-b", status="CLOSED")
        self.capture(T2)
        real = Projector._project_events

        def unattributed(projector, builder):
            projector.report.gap("hook_gap", "somewhere")
            return real(projector, builder)

        with mock.patch.object(Projector, "_project_events", unattributed):
            built = self.generation()
            self.assertEqual(built.mode, "cold")
            self.assertIn("hook_gap", {g["code"] for g in built.report["gaps"]})
            self.assertSameAsCold(built, "hooked.sqlite")
        self.assertEqual(self.generation().mode, "cold")   # its parts are never reused
        self.assertEqual(self.generation().mode, "incremental")

    def test_a_missing_or_mismatched_previous_generation_means_a_cold_build(self):
        self.capture(T1)
        first = self.generation()
        conn = sqlite3.connect(str(first.store))
        conn.execute("UPDATE store_meta SET value = 'sha256:tampered' "
                     "WHERE key = 'content_digest'")
        conn.commit()
        conn.close()
        self.assertIsNone(inc.current_generation(self.state))
        again = self.generation()
        self.assertEqual(again.mode, "cold")
        self.assertSameAsCold(again, "cold.sqlite")
        # A store at the previous store FORMAT (v2 may name a successor paired on the
        # owning parent alone) is never read nor built on: the next build is cold.
        conn = sqlite3.connect(str(again.store))
        conn.execute("UPDATE store_meta SET value = 'plx-ontology-store/v2' "
                     "WHERE key = 'format'")
        conn.commit()
        conn.close()
        with self.assertRaises(StoreError) as caught:
            ProjectionStore(again.store, ReadPolicy.from_audience_map(
                AudienceMap(AUDIENCE, tenant="plx"), marking_principals=MARKINGS))
        self.assertEqual(caught.exception.code, "store_format_mismatch")
        self.assertIsNone(inc.current_generation(self.state))
        rebuilt = self.generation()
        self.assertEqual(rebuilt.mode, "cold")
        self.assertSameAsCold(rebuilt, "rebuilt.sqlite")

    def test_a_session_generation_is_reused_unprojected_and_only_within_its_session(self):
        """One producer build per verifier session (``ontology.session_projection``): a
        consumer whose captures match opens it as it is (counted builds = 1); a capture
        changed since projects only that change; a generation committed before the
        session's start is never a session generation."""
        session = self.root / "session"
        start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        builds = []
        real = Projector.project_into

        def counted(projector, builder):
            builds.append(projector)
            return real(projector, builder)

        def consumer(name):
            produced = inc.current_generation(session, committed_since=start)
            self.assertIsNotNone(produced)
            with MirrorCaptures(self.root / "mirror.sqlite") as captures:
                return inc.build_generation(self.root / name, self.schema,
                                            {"portal_api": captures}, previous=produced,
                                            reuse=True, normalizer=normalizer,
                                            space=incremental_path)
        self.capture(T1)
        with mock.patch.object(Projector, "project_into", counted):
            with MirrorCaptures(self.root / "mirror.sqlite") as captures:
                produced = inc.build_generation(session, self.schema,
                                                {"portal_api": captures},
                                                normalizer=normalizer, clock=lambda: T2)
            reused = [consumer("consumer1"), consumer("consumer2")]
        self.assertEqual(len(builds), 1)
        for generation in reused:
            self.assertEqual((generation.mode, generation.units_rebuilt),
                             ("reused", 0))
            self.assertEqual((generation.store, generation.content_digest),
                             (produced.store, produced.content_digest))
            self.assertEqual(generation.report["counts"], produced.report["counts"])
        self.assertEqual(list((self.root / "consumer1" / "generations").iterdir()), [])
        # A consumer reading a capture the producer did not project gets the change
        # projected on top, equal to a cold build of what it reads.
        self.fake.set("public.app_purchase_order", "po-a", status="RECEIVED")
        self.capture(T2)
        changed = consumer("consumer3")
        self.assertEqual(changed.mode, "incremental")
        self.assertLess(changed.units_rebuilt, changed.units)
        report = self.cold("cold.sqlite")
        self.assertEqual(changed.content_digest, report["content_digest"])
        # Committed before this session started (or at an unreadable time): none.
        later = (datetime.now(timezone.utc) + timedelta(seconds=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        self.assertIsNone(inc.current_generation(session, committed_since=later))
        self.assertIsNone(inc.current_generation(session, committed_since="not-a-time"))
        self.assertEqual(inc.current_generation(session).id, produced.id)

    def test_v3_build_from_unreopened_v2_labelled_mirror_has_no_parent_only_successor(self):
        """A PO line deleted and another added under the same order, labelled replaced
        on that parent alone (the earlier rule) in a file no writer reopened since: the
        build is refused, the committed generation stays, and once a writer opens the
        file the next build names no successor and equals a cold build."""
        self.capture(T1)
        first = self.generation()
        lines = self.fake.tables["public.app_purchase_order_line"]
        lines[:] = [dict(row, id="l4") if row["id"] == "l3" else row for row in lines]
        self.capture(T2)
        conn = sqlite3.connect(str(self.root / "mirror.sqlite"))
        with conn:
            forged = conn.execute(
                "UPDATE row_removal SET replaced_basis = 'source_replacement', "
                "replaced_by = (SELECT json_array(json_array(json(key_json), capture_id)) "
                "FROM row_current WHERE relation = row_removal.relation AND "
                "key_json = '{\"id\":\"l4\"}') WHERE relation = ? AND key_json = ?",
                ("public.app_purchase_order_line", '{"id":"l3"}'))
            self.assertEqual(forged.rowcount, 1)
            conn.execute("DELETE FROM mirror_meta WHERE name = 'replacement_rule'")
        conn.close()
        with self.assertRaises(ProjectionError) as caught:
            self.generation()
        self.assertEqual(caught.exception.code, "mirror_replacement_rule_stale")
        self.assertEqual(inc.current_generation(self.state).id, first.id)
        SourceMirror(self.root / "mirror.sqlite", WorkbenchReadApiClient(
            BASE, KEY, fetch=self.fake), AudienceMap(AUDIENCE, tenant="plx"),
            clock=self.clock).close()
        rebuilt = self.generation()
        conn = sqlite3.connect(str(rebuilt.store))
        try:
            removals = conn.execute("SELECT object_id, replaced_basis, replaced_by FROM "
                                    "object_removals WHERE until IS NULL").fetchall()
        finally:
            conn.close()
        self.assertEqual([(basis, successor) for object_id, basis, successor in removals
                          if '"l3"' in object_id], [(None, None)], removals)
        self.assertSameAsCold(rebuilt, "rebuilt.sqlite")


class SyntheticGenerationTests(unittest.TestCase):
    """The curated schema over generated captures (``synthetic_captures``): one changed
    relation at a time, for relations whose change reaches far (a product every
    line and conflict reads), hops a lineage (a header its lines inherit from), a
    BC company-scoped book, a mail relation."""

    ROWS = 8
    RELATIONS = ("public.app_product", "public.app_purchase_order", "purchaseOrders",
                 "messages", "sage.ap_pjd")   # the last: a packed binding (value_storage)

    @classmethod
    def setUpClass(cls):
        from ontology.schema import load_schema
        cls.schema = load_schema()

    def sources(self, changed=None):
        return synthetic_captures.sources(self.schema, self.ROWS, [changed] if changed else [])

    def test_one_changed_relation_rebuilds_its_units_and_equals_a_cold_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = inc.build_generation(root / "state", self.schema, self.sources(),
                                         clock=lambda: T2)
            cold = build_store(root / "cold.sqlite", self.schema, self.sources(),
                               clock=lambda: T2)
            self.assertEqual(first.content_digest, cold["content_digest"])
            for relation in self.RELATIONS:
                with self.subTest(relation=relation):
                    # Each build reuses the one before (one more relation changed).
                    built = inc.build_generation(root / "state", self.schema,
                                                 self.sources(relation), clock=lambda: T2)
                    cold = build_store(root / "cold.sqlite", self.schema,
                                       self.sources(relation), clock=lambda: T2)
                    self.assertEqual(built.mode, "incremental")
                    self.assertGreater(built.units_rebuilt, 0)
                    self.assertLess(built.units_rebuilt, built.units)
                    self.assertNotEqual(built.content_digest, first.content_digest)
                    self.assertEqual(tables(built.store), tables(root / "cold.sqlite"))
                    self.assertEqual(built.content_digest, cold["content_digest"])

    def test_an_incremental_build_clones_its_seed_when_it_can_and_checks_its_estimate(self):
        """No byte copy of the served generation where the file system clones (the seed
        shares its pages); a copy where it cannot, and then the estimate counts it; a
        space guard that refuses leaves the committed generation and nothing else."""
        def clone(source, target):   # a stand-in clone: no shutil.copyfile, same bytes
            with open(source, "rb") as src, open(target, "wb") as dst:
                while block := src.read(1 << 20):
                    dst.write(block)
            return True

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            first = inc.build_generation(state, self.schema, self.sources(), clock=lambda: T2)
            measured = json.loads((first.store.parent / "units.json").read_text())["bytes"]
            self.assertEqual(measured["store"], first.store.stat().st_size)
            self.assertGreater(measured["work_peak"], 0)
            cold = inc.space_need(state)
            self.assertEqual((cold["mode"], cold["needed_bytes"]),
                             ("cold", measured["store"] + measured["work_peak"]))
            seen = []
            with mock.patch("ontology.project_incremental.can_clone", return_value=True), \
                    mock.patch("ontology.store.clone_file", side_effect=clone), \
                    mock.patch("ontology.store.shutil.copyfile",
                               side_effect=AssertionError("a full copy")):
                cloned = inc.build_generation(state, self.schema, self.sources("sage.ap_pjd"),
                                              clock=lambda: T2, space=seen.append)
            self.assertEqual((cloned.mode, cloned.seed, cloned.need["seed"]),
                             ("incremental", "clone", "clone"))
            self.assertEqual(seen, [cloned.need])
            base = first.store.stat().st_size
            self.assertLess(cloned.need["needed_bytes"], base)   # no seed bytes
            self.assertTrue(0 < cloned.need["changed_share"] < 1, cloned.need)
            with mock.patch("ontology.project_incremental.can_clone", return_value=False):
                copied = inc.build_generation(state, self.schema, self.sources("messages"),
                                              clock=lambda: T2)
            self.assertEqual((copied.mode, copied.seed), ("incremental", "copy"))
            self.assertGreaterEqual(copied.need["needed_bytes"], cloned.store.stat().st_size)

            def refuse(need):
                raise OSError("disk_low")
            with self.assertRaises(OSError):
                inc.build_generation(state, self.schema, self.sources("purchaseOrders"),
                                     clock=lambda: T2, space=refuse)
            self.assertEqual(inc.current_generation(state).id, copied.id)
            # Before it checked, the build dropped the generation before the committed one
            # (room for the next attempt; a reader holding it keeps its file): the committed
            # generation is all that is left, and nothing of the refused build.
            self.assertEqual(sorted(d.name for d in (state / "generations").iterdir()),
                             [copied.id])

    def test_the_build_takes_the_mode_that_fits_and_fails_only_when_neither_does(self):
        """Live 2026-10-27: 87% of the units changed and the incremental estimate (a copy
        of the served store plus twice that share) was 16 GB where a cold build needs 8;
        the guard skipped the build. A build where most units changed, or whose
        incremental estimate exceeds the cold one, is cold beside the committed
        generation; a guard that refuses that mode gets the other; only refusing both
        fails, and the committed generation stays current with nothing else on disk."""
        relations = sorted({b["relation"] for t in self.schema.object_types.values()
                            for b in t.get("bindings", [])})

        def changed(names):
            return synthetic_captures.sources(self.schema, self.ROWS, names)
        most = [r for r in relations if r != "messages"]
        half = relations[:len(relations) // 2]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            first = inc.build_generation(state, self.schema, self.sources(), clock=lambda: T2)
            served = []
            refused = []

            def note(need):   # the committed generation is current while the guard decides
                served.append(inc.current_generation(state).id)

            def refuse(*modes):
                def guard(need):
                    refused.append(need["mode"])
                    if need["mode"] in modes:
                        raise OSError("disk_low")
                return guard
            # Most units changed: cold, though a cloned seed would need less.
            with mock.patch("ontology.project_incremental.can_clone", return_value=True):
                built = inc.build_generation(state, self.schema, changed(most),
                                             clock=lambda: T2, space=note)
            estimates = built.need["estimates"]
            self.assertGreater(estimates["incremental"]["changed_share"], inc.COLD_SHARE)
            self.assertEqual((built.mode, built.need["mode"], built.need["preferred"],
                              built.units_rebuilt, served),
                             ("cold", "cold", "cold", built.units, [first.id]))
            self.assertEqual(estimates["cold"]["needed_bytes"],
                             first.store.stat().st_size + json.loads(
                                 (first.store.parent / "units.json").read_text()
                             )["bytes"]["work_peak"])
            cold = build_store(root / "cold.sqlite", self.schema, changed(most),
                               clock=lambda: T2)
            self.assertEqual(built.content_digest, cold["content_digest"])
            self.assertEqual(tables(built.store), tables(root / "cold.sqlite"))
            # The cold working-file peak is carried through an incremental build (its own
            # peak is only its share), so the cold estimate stays a cold build's.
            work = json.loads((built.store.parent / "units.json").read_text())["bytes"]
            self.assertEqual(work["cold_work_peak"], work["work_peak"])
            with mock.patch("ontology.project_incremental.can_clone", return_value=True):
                fallback = inc.build_generation(state, self.schema, self.sources(),
                                                clock=lambda: T2, space=refuse("cold"))
            self.assertEqual((fallback.mode, fallback.need["mode"], fallback.need["preferred"],
                              refused), ("incremental", "incremental", "cold",
                                         ["cold", "incremental"]))
            self.assertEqual(json.loads((fallback.store.parent / "units.json").read_text())
                             ["bytes"]["cold_work_peak"], work["cold_work_peak"])
            # Under half changed, but a byte-copied seed needs more than a cold build.
            refused.clear()
            with mock.patch("ontology.project_incremental.can_clone", return_value=False):
                smaller = inc.build_generation(state, self.schema, changed(half),
                                               clock=lambda: T2, space=refuse())
            estimates = smaller.need["estimates"]
            self.assertLessEqual(estimates["incremental"]["changed_share"], inc.COLD_SHARE)
            self.assertGreater(estimates["incremental"]["needed_bytes"],
                               estimates["cold"]["needed_bytes"])
            self.assertEqual((smaller.mode, refused), ("cold", ["cold"]))
            # Neither fits: the guard's refusal, the committed generation as it was.
            refused.clear()
            with mock.patch("ontology.project_incremental.can_clone", return_value=False), \
                    self.assertRaises(OSError):
                inc.build_generation(state, self.schema, changed(most),
                                     clock=lambda: T2, space=refuse("cold", "incremental"))
            self.assertEqual(sorted(refused), ["cold", "incremental"])
            self.assertEqual(inc.current_generation(state).id, smaller.id)
            self.assertEqual(sorted(d.name for d in (state / "generations").iterdir()),
                             [smaller.id])

    def test_build_time_stays_inside_a_generous_bound(self):
        # Diagnostic, not a target (EVIDENCE/perf/2026-10-04-proj-budget.md has the
        # live-scale figures): about 2,400 objects cold, and an unchanged rebuild that
        # projects nothing again. A regression of an order of magnitude trips it.
        with tempfile.TemporaryDirectory() as tmp:
            started = time.monotonic()
            first = inc.build_generation(Path(tmp) / "state", self.schema,
                                         synthetic_captures.sources(self.schema, 20),
                                         clock=lambda: T2)
            cold = time.monotonic() - started
            started = time.monotonic()
            again = inc.build_generation(Path(tmp) / "state", self.schema,
                                         synthetic_captures.sources(self.schema, 20),
                                         clock=lambda: T2)
            warm = time.monotonic() - started
        self.assertGreater(first.report["counts"]["objects"], 2000)
        self.assertEqual(again.units_rebuilt, 0)
        self.assertLess(cold, 120, (cold, warm))
        self.assertLess(warm, 60, (cold, warm))


class FingerprintTests(unittest.TestCase):
    T0, T1 = "2026-09-29T10:00:00.000000Z", "2026-09-29T11:00:00.000000Z"

    def records(self, last_seen):
        def record(code, start, end, seen):
            return CapturedRecord("portal_api", "public.app_supplier", f'{{"id":"{code}"}}',
                                  {"id": code, "code": code, "name": f"n{start[12]}"},
                                  f"api://h/{code}@{start}", "row", "0" * 64, start, end,
                                  seen, "api:public.app_supplier")
        return [record("AAA", self.T0, self.T1, self.T0),
                record("AAA", self.T1, None, last_seen), record("BBB", self.T0, None, last_seen)]

    def test_a_current_versions_last_sighting_is_not_read_so_it_is_not_fingerprinted(self):
        # The fingerprint leaves a current version's last_seen_at out because the
        # projection never reads it: the two builds below must stay identical.
        with tempfile.TemporaryDirectory() as tmp:
            digests = [build_store(Path(tmp) / f"{n}.sqlite", compiled_schema(),
                                   {"portal_api": StaticCaptures(self.records(seen))},
                                   normalizer=normalizer, clock=lambda: T2)["content_digest"]
                       for n, seen in enumerate((self.T1, "2026-10-01T00:00:00.000000Z"))]
        self.assertEqual(digests[0], digests[1])
        prints = [inc._stream_fingerprint(self.records(seen))
                  for seen in (self.T1, "2026-10-01T00:00:00.000000Z")]
        self.assertEqual(prints[0], prints[1])
        changed = self.records(self.T1)
        changed[0] = dataclasses.replace(changed[0], last_seen_at=self.T0[:-2] + "1Z")
        self.assertNotEqual(inc._stream_fingerprint(changed), prints[0])

    def test_a_cyclic_lineage_inheritance_is_never_built_incrementally(self):
        # An inherited lineage is computed lazily and memoized, so in a cycle it depends
        # on which record is visited first: no unit boundary is sound there.
        def binding(bid, inherit):
            return {"id": bid, "source": "portal_api", "relation": f"public.{bid}",
                    "key": ["id"], "lineage": {"inherit": inherit, "default": "portal_gold"}}

        def link(frm, to):
            return {"from": "T", "to": "T", "cardinality": "N:1", "bindings": [
                {"kind": "fk", "from_binding": frm, "to_binding": to, "tier_ceiling": "verified",
                 "pairs": [{"from": "ref", "to": "id", "compare": "text_id"}]}]}

        schema = SimpleNamespace(
            digest="sha256:test", event_types={},
            object_types={"T": {"bindings": [binding("a", "T.a_to_b"), binding("b", "T.b_to_a")],
                                "properties": []}},
            link_types={"T.a_to_b": link("a", "b"), "T.b_to_a": link("b", "a")})
        self.assertFalse(inc.plan(schema).acyclic)
        schema.object_types["T"]["bindings"][1]["lineage"].pop("inherit")
        self.assertTrue(inc.plan(schema).acyclic)
        self.assertEqual(inc.plan(schema).units["b:a"], frozenset({"a", "b"}))

if __name__ == "__main__":
    unittest.main()
