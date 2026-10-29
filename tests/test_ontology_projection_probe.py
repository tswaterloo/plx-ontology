"""The ontology projection probe: its budget (``--budget-seconds``) and that it measures
the served path (what the host's plx tools serve its reader, not a probe-only path).

Evidence label: ``injected-transport``. The mirror sync is a fake that advances a fake
clock, or the fixture Workbench and BC fakes; nothing here is live
(``devloop/live-checks/ontology.projection.json`` stays live-unverified).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ontology.project import WORK_LAYOUT
from ontology.tests.test_projection import (BASE, KEY, FakeWorkbench, compiled_schema,
                                                normalizer)
from scripts import ontology_projection_probe as probe

ROOT = Path(__file__).resolve().parents[1]


def synthetic_store(path: str, records: int, *, restricted: bool = False,
                    shuffled: bool = False) -> str:
    """A store in the real layout (``ontology.store_ddl``), its values written in key
    order: per source row ``api://public.t/<i>`` a ``T`` object (quantity ``ordered`` at
    ``row.c0``, two texts) and an ``S`` object citing the same row, whose ``row.c0`` is a
    ``received`` quantity, its audience withheld (``api:public.w``) for every third row.
    ``restricted``: also ``finance``-marked quantities (``S`` when ``i % 3 == 1``, ``T``
    when ``i % 5 == 4``) and a ``T`` withheld when ``i % 11 == 5`` (so some rows are cited
    by withheld objects only). ``shuffled``: the values written in a seeded random order,
    not key order (rowid order is then no key order)."""
    import random
    from ontology import store_ddl
    conn = sqlite3.connect(path)
    store_ddl.create(conn)
    conn.executemany("INSERT INTO value_fields VALUES (?, ?, ?)",
                     [(f + 1, f"p{f}", f"row.c{f}") for f in range(3)])
    conn.executemany("INSERT INTO value_kinds (kind, value_type, quantity_role, unit, parser, "
                     "markings) VALUES (?, ?, ?, ?, 'trim', ?)",
                     [(1, "text", None, None, "[]"), (2, "quantity", "ordered", "EA", "[]"),
                      (3, "quantity", "received", "EA", "[]"),
                      (4, "quantity", "received", "EA", '["finance"]'),
                      (5, "quantity", "ordered", "EA", '["finance"]')])
    objects, cells = [], []
    for kind in ("S", "T"):
        for i in range(records):
            withheld = (i % 3 == 0) if kind == "S" else (restricted and i % 11 == 5)
            objects.append((f"{kind}:b:{i:06d}", i,
                            "api:public.w" if withheld else "api:public.t"))
    for src, (object_id, i, audience) in enumerate(objects, 1):
        if object_id[0] == "S":
            quantity = 4 if restricted and i % 3 == 1 else 3
        else:
            quantity = 5 if restricted and i % 5 == 4 else 2
        cells += [(src, 1, quantity, str(i % 7)), (src, 2, 1, f"a{i}"), (src, 3, 1, f"b{i}")]
    if shuffled:
        random.Random(0).shuffle(cells)
    conn.executemany("INSERT INTO objects VALUES (?, 'T', 'b', ?, ?, '2026-01-01', NULL)",
                     [(o, json.dumps([o]), a) for o, _, a in objects])
    conn.executemany("INSERT INTO object_versions (object_id, l1_handle, digest, lineage, "
                     "recorded_at) VALUES (?, ?, 'd', 'workbench', '2026-01-01')",
                     [(o, f"api://public.t/{i}") for o, i, _ in objects])
    # v10: a source names its version by rowid (the versions above, in object order)
    conn.executemany("INSERT INTO value_sources (src, ver, binding_id, recorded_at) "
                     "VALUES (?, ?, 'b', '2026-01-01')",
                     [(src, src) for src in range(1, len(objects) + 1)])
    pairs = sorted({(field, kind) for _, field, kind, _ in cells})
    layout = {pair: n for n, pair in enumerate(pairs, 1)}
    conn.executemany("INSERT INTO value_layouts (layout, fields) VALUES (?, ?)",
                     [(n, store_ddl.singleton_fields(*pair)) for pair, n in layout.items()])
    conn.executemany("INSERT INTO value_layout_fields (layout, pos, field, kind) "
                     "VALUES (?, 0, ?, ?)", [(n, *pair) for pair, n in layout.items()])
    conn.executemany("INSERT INTO value_cells (src, field, layout, value) VALUES (?, ?, ?, ?)",
                     [(src, field, layout[(field, kind)], value)
                      for src, field, kind, value in cells])
    conn.execute("INSERT INTO store_meta VALUES ('audience_keys', ?)",
                 (json.dumps(["api:public.t", "api:public.w"]),))
    conn.commit()
    conn.close()
    return path


def served_rows(handle: str) -> dict:
    """The served claim gate's row for a synthetic store's citation (every one resolves,
    so a withheld one counts as a leak)."""
    i = int(handle.rsplit("/", 1)[1])
    return {"relation": "public.t", "row": {"c0": str(i % 7), "c1": f"a{i}", "c2": f"b{i}"}}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def slow_sync(clock: Clock, seconds: float):
    def fake(client, db, *, monotonic, budget_seconds, **options):
        fake.budget = budget_seconds
        clock.now += seconds
        return {"relation_count": 7, "rows_current_total": 123,
                "completeness": {"complete_bound_relation_count": 2, "bound_total": 5,
                                 "all_bound_complete": False},
                "relations": {}, "unstable_relations": []}
    return fake


class ProbeBudgetTest(unittest.TestCase):
    def run_probe(self, seconds: float, budget: float):
        clock = Clock()
        sync = slow_sync(clock, seconds)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("scripts.mirror_measure.ordered_full_coverage", sync), \
                mock.patch("ontology.project.build_store",
                           side_effect=AssertionError("no build after a spent budget")):
            result = probe.projection_probe(None, f"{tmp}/db", f"{tmp}/store",
                                            budget_seconds=budget, schema=object(),
                                            normalizer=lambda *a: None, monotonic=clock)
        return result, sync

    def test_a_slow_sync_stops_the_probe_with_a_partial_report(self):
        result, sync = self.run_probe(seconds=100.0, budget=100.0)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["phase"], "sync")
        self.assertEqual((result["mirror_relation_count"], result["rows_current_total"]),
                         (7, 123))
        self.assertEqual(result["completeness"]["bound_total"], 5)
        self.assertEqual(result["budget_seconds"], 100.0)
        self.assertLessEqual(result["elapsed_seconds"], 100.0 + 1e-6)
        json.dumps(result)  # a valid, serialisable report

    def test_the_sync_gets_its_share_so_later_phases_keep_time(self):
        _, sync = self.run_probe(seconds=100.0, budget=100.0)
        self.assertAlmostEqual(sync.budget, 100.0 * probe.SYNC_SHARE)

    def test_main_exits_with_the_distinct_budget_code(self):
        partial = {"probe": probe.PROBE, "status": "budget_exhausted", "phase": "sync"}
        env = {"PLX_WORKBENCH_API_URL": "https://x.invalid", "PLX_WORKBENCH_API_KEY": "plxwb_" + "f" * 40}
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(probe, "projection_probe", return_value=partial), \
                contextlib.redirect_stdout(out):
            code = probe.main(["--db", tmp + "/m", "--store", tmp + "/s",
                               "--budget-seconds", "5"], environ=env)
        self.assertEqual(code, probe.EXIT_BUDGET)
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "budget_exhausted")

    def run_builds(self, sync_seconds, build_seconds, budget, patches=(), **options):
        """Fast sync, then builds that advance the clock and call the checkpoint between
        batches as the real projector does."""
        clock = Clock()
        patches = dict(patches(clock)) if callable(patches) else {}
        sync = slow_sync(clock, sync_seconds)
        builds = []

        def fake_build(target, schema, sources, *, checkpoint=None, **options):
            builds.append(target)
            for _ in range(10):
                clock.now += build_seconds / 10
                checkpoint()
            return {"counts": {"objects": 3, "values": 9}, "content_digest": "d",
                    "gaps": []}

        class Captures:
            def __init__(self, db): pass
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("scripts.mirror_measure.ordered_full_coverage", sync), \
                mock.patch("ontology.project.build_store", fake_build), \
                mock.patch("ontology.project.MirrorCaptures", Captures), \
                contextlib.ExitStack() as stack:
            for target, value in patches.items():
                stack.enter_context(mock.patch(target, value))
            result = probe.projection_probe(None, f"{tmp}/db", f"{tmp}/store",
                                            budget_seconds=budget, schema=object(),
                                            normalizer=lambda *a: None, monotonic=clock,
                                            **options)
        return result, builds, clock

    def test_a_slow_build_stops_between_batches_inside_the_budget(self):
        result, builds, clock = self.run_builds(10.0, 300.0, 100.0)
        self.assertEqual((result["status"], result["phase"]), ("budget_exhausted",
                                                              "projection"))
        self.assertEqual(len(builds), 1)
        self.assertLess(clock.now, 100.0 + 300.0 / 10 + 1e-6)   # at most one batch over
        # the stop says where the budget went
        self.assertEqual(result["phase_seconds"]["sync"], 10.0)
        self.assertAlmostEqual(result["phase_seconds"]["projection"], clock.now - 10.0)

    def test_a_rebuild_that_cannot_fit_is_skipped_inside_the_budget(self):
        # sync 10 s + served config 2 s + build 85 s = 97 s of 100: a second 85 s build
        # must not start; the checks (faked here) still run on the first.
        def patches(clock):
            real = probe.prepare_served

            def slow_served(*args, **kwargs):
                clock.now += 2.0
                return real(*args, **kwargs)

            class Host:
                @contextlib.contextmanager
                def served_tools(self, actor):
                    yield mock.Mock()
            here = "scripts.ontology_projection_probe."
            return {here + "prepare_served": slow_served, here + "publish_served": mock.Mock(),
                    here + "served_host": lambda store, environ=None: Host(),
                    here + "reader_actor": mock.Mock(),
                    here + "check_store": mock.Mock(
                        return_value={"withheld_citations_resolved": 0}),
                    here + "denied_checks": mock.Mock(return_value={"denied_leaks": 0}),
                    here + "removal_checks": mock.Mock(return_value={"removal_stretches": {}}),
                    "scripts.mirror_measure.unstable_gaps": mock.Mock(return_value=[])}

        result, builds, clock = self.run_builds(10.0, 85.0, 100.0, patches)
        self.assertEqual((result["status"], result["phase"]), ("budget_exhausted",
                                                              "rebuild_skipped"))
        self.assertEqual(len(builds), 1)
        self.assertLessEqual(clock.now, 100.0)
        self.assertEqual(result["objects"], 3)   # the finished build's counts are kept
        self.assertEqual(result["denied_leaks"], 0)
        # the served config is its own phase, not part of the sync
        self.assertEqual((result["phase_seconds"]["sync"], result["phase_seconds"]["served"],
                          result["phase_seconds"]["projection"]), (10.0, 2.0, 85.0))

    def test_the_checks_budget_stops_the_checks_inside_the_overall_budget(self):
        # build 10 s, then a check that takes 50 s: 60 s of the 100 s overall, but over the
        # 40 s checks budget, which starts when the checks do (the build is not counted).
        def patches(clock):
            def slow_check(*args, **kwargs):
                clock.now += 50.0
                return {"withheld_citations_resolved": 0}

            class Host:
                @contextlib.contextmanager
                def served_tools(self, actor):
                    yield mock.Mock()
            here = "scripts.ontology_projection_probe."
            return {here + "publish_served": mock.Mock(), here + "prepare_served": mock.Mock(),
                    here + "served_host": lambda store, environ=None: Host(),
                    here + "reader_actor": mock.Mock(), here + "check_store": slow_check,
                    here + "denied_checks": mock.Mock(return_value={"denied_leaks": 0}),
                    here + "removal_checks": mock.Mock(
                        return_value={"removal_stretches": {}}),
                    "scripts.mirror_measure.unstable_gaps": mock.Mock(return_value=[])}

        # Without it the 65 s overall budget holds (the stop is the unfit rebuild's).
        for checks_budget, phase in ((40.0, "claims"), (None, "rebuild_skipped")):
            with self.subTest(checks_budget=checks_budget):
                result, _, _ = self.run_builds(0.0, 10.0, 65.0, patches,
                                               checks_budget_seconds=checks_budget)
                self.assertEqual((result["status"], result["phase"]),
                                 ("budget_exhausted", phase))
                self.assertEqual(result.get("checks_budget_seconds"), checks_budget)

    def test_check_store_samples_and_gates_without_reading_the_whole_store(self):
        """Live: the claims phase took 1,271 s of 1,588 s on 9.39M values (a sorted scan
        for the sample, then a scan of every value per claim for its role contract). Its
        sample's store work no longer grows with the store, the gate reads the store once
        whatever the sample, and the sample (on a store written in key order) and the
        verdicts are what the ordered scan and the per-claim lookup gave."""
        real_connect = sqlite3.connect

        def run(path, sample, *, per_claim=False):
            """check_store's output, handles asked and VM steps on each connection."""
            steps, asked = [], []

            def connect(*args, **kwargs):
                conn = real_connect(*args, **kwargs)
                steps.append(0)
                slot = len(steps) - 1

                def count():
                    steps[slot] += 1
                conn.set_progress_handler(count, 1)
                return conn

            def rows(handle):
                asked.append(handle)
                i = int(handle.rsplit("/", 1)[1])
                return {"relation": "public.t", "row": {"c0": str(i % 7), "c1": f"a{i}",
                                                        "c2": f"b{i}"}}
            per = (mock.patch.object(probe, "sampled_contracts",
                                     lambda store, actor, pairs: store.claim_contracts(actor))
                   if per_claim else contextlib.nullcontext())
            policy = mock.Mock(**{"may_read.side_effect":
                                  lambda reader, key: key != "api:public.w"})
            with mock.patch.object(sqlite3, "connect", connect), per:
                checks = probe.check_store(
                    path, rows, mock.Mock(tenant="plx", principals=frozenset({"x"})),
                    mock.Mock(object_types={}), normalizer=None, policy=policy,
                    claim_sample=sample)
            timing = checks.pop("claims_timing")
            self.assertEqual(sorted(timing), ["gate_seconds", "scan_seconds"])
            return checks, sorted(set(asked)), steps

        with tempfile.TemporaryDirectory() as tmp:
            small, large = synthetic_store(tmp + "/s.sqlite", 500), \
                synthetic_store(tmp + "/l.sqlite", 5000)
            (_, _, s_steps), (l_checks, l_asked, l_steps) = run(small, 60), run(large, 60)
            # the sample's connection (the first): no scan, no sort, whatever the size
            self.assertLess(l_steps[0], s_steps[0] * 1.5 + 2000, (s_steps, l_steps))
            # the gate never reads the whole store: it looks each claimed handle up by
            # index (store v10), so its work does not grow with the store (10x the values
            # here) and stays far below one read of every value
            self.assertLess(sum(l_steps[1:]), 1.5 * sum(s_steps[1:]) + 500, (s_steps, l_steps))
            self.assertLess(sum(l_steps[1:]), 6 * 5000 // 4, l_steps)
            for path in (small, large):
                checks, asked, _ = run(path, 60)
                conn = real_connect(path)
                ordered = [r[0] for r in conn.execute(
                    "SELECT v.l1_handle FROM object_values v JOIN objects o ON o.object_id = "
                    "v.object_id ORDER BY v.object_id, v.property, v.l1_handle, v.field_path")]
                conn.close()
                stride = ordered[::max(1, len(ordered) // 60)][:60]
                self.assertEqual(asked, sorted(set(stride)))
                self.assertEqual((checks["values_total"], checks["claim_sample"]),
                                 (len(ordered), len(stride)))
                self.assertEqual(checks, run(path, 60, per_claim=True)[0])
                # the shared handle's least visible object pins the role: both verdicts
                self.assertTrue(0 < checks["claims_passed"] < checks["claims_checked"],
                                checks)
                self.assertGreater(checks["claim_values_withheld"], 0, checks)
        self.assertEqual(l_checks["claim_failures"],
                         {"claim_role_unverified": l_checks["claims_checked"]
                          - l_checks["claims_passed"]})

    def test_check_store_stops_through_its_checkpoint(self):
        class Stop(Exception):
            pass
        calls = []

        def checkpoint():
            calls.append(1)
            raise Stop

        with tempfile.TemporaryDirectory() as tmp:
            path = synthetic_store(tmp + "/s.sqlite", 1)
            actor = mock.Mock(principals=frozenset({"x"}))
            schema = mock.Mock(object_types={})
            with self.assertRaises(Stop):
                probe.check_store(path, lambda handle: None, actor, schema, normalizer=None,
                                  policy=mock.Mock(**{"may_read.return_value": True}),
                                  claim_sample=5, checkpoint=checkpoint)
        self.assertEqual(len(calls), 1)

    def test_the_traverse_probe_passes_the_budget_status_through(self):
        from scripts import ontology_traverse_probe as traverse

        def spent(argv, **_sources):
            print(json.dumps({"probe": probe.PROBE, "status": "budget_exhausted",
                              "phase": "rebuild_skipped"}))
            return probe.EXIT_BUDGET

        out = io.StringIO()
        with mock.patch.object(probe, "main", spent), contextlib.redirect_stdout(out):
            code = traverse.main(["--db", "x", "--store", "y", "--budget-seconds", "5"])
        report = json.loads(out.getvalue())
        self.assertEqual(code, probe.EXIT_BUDGET)
        self.assertEqual((report["status"], report["phase"]),
                         ("budget_exhausted", "rebuild_skipped"))
        self.assertNotIn("error", report)



def sample_of(path: str, claim_sample: int) -> tuple[int, list[dict], int, int]:
    """``sample_values`` on ``path``: values_total, the sample, its seeks and VM steps."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    steps, seeks = [0], [0]

    def count():
        steps[0] += 1
    conn.set_progress_handler(count, 1)
    try:
        total, sample = probe.sample_values(
            conn, claim_sample, lambda: seeks.__setitem__(0, seeks[0] + 1))
    finally:
        conn.close()
    return total, sample, seeks[0], steps[0]


def store_rowids(path: str) -> list[int]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT rowid FROM value_cells ORDER BY rowid")]
    finally:
        conn.close()


def value_key(value: dict) -> tuple:
    return (value["object_id"], value["property"], value["l1_handle"], value["field_path"])


class ClaimSampleTest(unittest.TestCase):
    """``sample_values`` (the claims phase's sample) and ``sampled_contracts`` (its role
    contracts) on real stores: synthetic layout, real ``ProjectionStore``, real ``Actor``."""

    def assert_spread(self, path: str, sample: list[dict], want: int) -> None:
        """Every quarter of the store's values (in rowid order) holds a fair share."""
        conn = sqlite3.connect(path)
        try:
            rank = {key: n for n, key in enumerate(conn.execute(
                "SELECT object_id, property, l1_handle, field_path FROM object_values_numbered "
                "ORDER BY row_no"))}
        finally:
            conn.close()
        quarters = [0, 0, 0, 0]
        for value in sample:
            quarters[rank[value_key(value)] * 4 // len(rank)] += 1
        self.assertTrue(all(q >= want // 12 for q in quarters), quarters)

    def test_a_store_with_rowid_gaps_still_gives_the_full_spread_sample(self):
        """Review: with a large rowid gap (deleted values) the stride targets in the gap all
        met the same value, so 60 asked gave 31. The sample is the count asked (or every
        value), the same on every run, spread over what is left, and bounded seeks."""
        with tempfile.TemporaryDirectory() as tmp:
            stores = {}
            for name, records in (("small", 600), ("large", 6000)):
                path = synthetic_store(f"{tmp}/{name}.sqlite", records)
                high = records * 6
                conn = sqlite3.connect(path)
                # a block of 70% gone, and every other value of the last fifth
                conn.execute("DELETE FROM value_cells WHERE rowid > ? AND rowid <= ?",
                             (high // 10, high * 8 // 10))
                conn.execute("DELETE FROM value_cells WHERE rowid > ? AND rowid % 2 = 0",
                             (high * 8 // 10,))
                conn.commit()
                conn.close()
                stores[name] = path
            dense = synthetic_store(tmp + "/dense.sqlite", 6000)
            _, _, _, dense_steps = sample_of(dense, 60)
            measured = {}
            for name, path in stores.items():
                left = len(store_rowids(path))
                total, sample, seeks, steps = sample_of(path, 60)
                self.assertEqual(total, left)
                self.assertEqual(len(sample), 60, (name, len(sample)))
                self.assertEqual(len({value_key(v) for v in sample}), 60)
                self.assertEqual(sample, sample_of(path, 60)[1])
                self.assert_spread(path, sample, 60)
                self.assertLessEqual(seeks, 3 * 60)
                measured[name] = steps
            # a seek's cost, not the store's size: the gapped store costs about the dense
            # one, and ten times the values cost about the same
            self.assertLess(measured["large"], dense_steps * 3 + 2000, (measured, dense_steps))
            self.assertLess(measured["large"], measured["small"] * 1.5 + 2000, measured)
            # fewer values than asked: every one, once
            tiny = synthetic_store(tmp + "/tiny.sqlite", 4)
            conn = sqlite3.connect(tiny)
            conn.execute("DELETE FROM value_cells WHERE rowid BETWEEN 3 AND 15")
            conn.commit()
            conn.close()
            total, sample, seeks, _ = sample_of(tiny, 60)
            self.assertEqual((total, len(sample)), (11, 11))
            self.assertEqual(len({value_key(v) for v in sample}), 11)
            self.assertLessEqual(seeks, 3 * 60)
            self.assertEqual(sample_of(tiny, 0)[1], [])

    def test_a_store_not_written_in_key_order_is_sampled_whole_and_in_bounded_memory(self):
        """The stride test this replaced also covered a store whose rowids are not key
        order and the memory the sample holds: still the count asked, deterministic,
        spread, and the peak memory does not grow with the store."""
        import tracemalloc
        with tempfile.TemporaryDirectory() as tmp:
            small = synthetic_store(tmp + "/s.sqlite", 500, shuffled=True)
            large = synthetic_store(tmp + "/l.sqlite", 5000, shuffled=True)
            peaks = []
            for path in (small, large):
                tracemalloc.start()
                try:
                    total, sample, _, _ = sample_of(path, 60)
                    peaks.append(tracemalloc.get_traced_memory()[1])
                finally:
                    tracemalloc.stop()
                self.assertEqual((total, len(sample)), (len(store_rowids(path)), 60))
                self.assertEqual(sample, sample_of(path, 60)[1])
                self.assert_spread(path, sample, 60)
                # not key order: the sample is rowid order, so not sorted by key
                keys = [value_key(v) for v in sample]
                self.assertNotEqual(keys, sorted(keys))
            self.assertLess(peaks[1], peaks[0] * 2 + 200_000, peaks)

    def test_sampled_contracts_drift_guard_equals_the_store_lookup(self):
        """``sampled_contracts`` is the store's batched ``claim_contracts``. For a real reader, a withheld audience
        and a finance marking it does not hold, every (handle, field) of the store gets
        what the store's own lookup gives, across every kind of answer (the least visible
        object's role, a visible later object's, none); and ``check_store`` gives the per-claim path's verdicts."""
        from ontology.store import ProjectionStore, ReadPolicy as StorePolicy
        from ontology.types import Actor
        from operating.ontology_tools import ReadPolicy as ServedPolicy
        actor = Actor("plx", frozenset({"staff:reader"}), "restricted")
        with tempfile.TemporaryDirectory() as tmp:
            for shuffled in (False, True):
                path = synthetic_store(f"{tmp}/{shuffled}.sqlite", 120, restricted=True,
                                       shuffled=shuffled)
                conn = sqlite3.connect(path)
                pairs = set(conn.execute(
                    "SELECT l1_handle, field_path FROM object_values"))
                conn.close()
                policy = StorePolicy(audiences={"api": lambda reader, rest:
                                                rest != "public.w"})
                with ProjectionStore(path, policy) as store:
                    exact = store.claim_contracts(actor)
                    fast = probe.sampled_contracts(store, actor, pairs)
                    got = {pair: fast(pair[0], {}, pair[1]) for pair in sorted(pairs)}
                    want = {pair: exact(pair[0], {}, pair[1]) for pair in sorted(pairs)}
                    self.assertEqual(got, want)
                    # an answer of every kind, so a changed lookup cannot agree by chance
                    roles = {pair[1]: set() for pair in pairs}
                    for pair, contract in want.items():
                        roles[pair[1]].add(None if contract is None else contract["id"]
                                           + "/" + contract["role"])
                    self.assertEqual(roles["row.c0"], {"ontology:T.p0/received",
                                                       "ontology:T.p0/ordered", None})
                    self.assertEqual(roles["row.c1"], {None})
                    # a pair outside the sample asks the store
                    self.assertEqual(probe.sampled_contracts(store, actor, set())(
                        "api://public.t/1", {}, "row.c0"), exact("api://public.t/1", {},
                                                                  "row.c0"))
                served = ServedPolicy(tenant="plx",
                                      audiences={"api:public.t": {"staff:reader"}},
                                      markings={"finance": {"staff:finance"}})
                outputs = []
                for per_claim in (False, True):
                    per = (mock.patch.object(probe, "sampled_contracts",
                                             lambda store, reader, pairs:
                                             store.claim_contracts(reader))
                           if per_claim else contextlib.nullcontext())
                    with per:
                        checks = probe.check_store(
                            path, served_rows, actor, mock.Mock(object_types={}),
                            normalizer=None, policy=served, claim_sample=60)
                    checks.pop("claims_timing")
                    outputs.append(checks)
                self.assertEqual(outputs[0], outputs[1])
                checks = outputs[0]
                denied = probe.denied_checks(
                    mock.Mock(**{"trim_check.return_value": {
                        "state": "checked", "capture_principal_objects": 1,
                        "withheld_leaks": 0, "withheld_principal_objects": 0,
                        "capture_principal_leaks": 0, "objects_probed": 1,
                        "capture_principal_denied_probed": 1}}),
                    citation_leaks=checks["withheld_citations_resolved"])
                self.assertEqual(denied["denied_leaks"],
                                 outputs[1]["withheld_citations_resolved"])
                self.assertEqual(checks["claim_sample"], 60)
                self.assertTrue(0 < checks["claims_passed"] < checks["claims_checked"],
                                checks)
                self.assertIsNotNone(checks["claim_pass_rate"])
                self.assertGreater(checks["claim_values_withheld"], 0, checks)
                # real contracts this time (a mock reader has none): a pinned role decides
                self.assertEqual(checks["claim_failures"],
                                 {"claim_role_mismatch": checks["claims_checked"]
                                  - checks["claims_passed"]})
                self.assertGreater(checks["withheld_citations_resolved"], 0, checks)
                self.assertGreater(checks["citations"]["resolved"], 0, checks)

    def test_batched_claim_contracts_equal_per_call_with_ties_and_withheld(self):
        """``claim_contracts(actor, pairs)`` (one query) answers every (handle, field) of a
        store exactly as the per-call form, for every actor, on a store with ties (a handle
        cited by several objects, the least object_id wins) and withheld audiences and
        markings; the probe uses no private store attribute; ``check_store`` asks the
        batched form with the granted pairs."""
        from ontology.store import ProjectionStore, ReadPolicy as StorePolicy
        from ontology.types import Actor
        source = Path(probe.__file__).read_text()
        self.assertNotIn("._scope", source)
        self.assertNotIn("._conn", source)
        actors = [Actor("plx", frozenset({"staff:reader"}), "restricted"),
                  Actor("plx", frozenset({"staff:reader", "staff:finance"}), "restricted"),
                  Actor("plx", frozenset(), "restricted")]
        with tempfile.TemporaryDirectory() as tmp:
            path = synthetic_store(f"{tmp}/s.sqlite", 60, restricted=True)
            conn = sqlite3.connect(path)
            pairs = set(conn.execute("SELECT l1_handle, field_path FROM object_values"))
            conn.close()
            policy = StorePolicy(audiences={"api": lambda reader, rest:
                                            rest != "public.w" and bool(reader.principals)},
                                 marking_principals={"finance": frozenset({"staff:finance"})})
            seen = set()
            with ProjectionStore(path, policy) as store:
                for actor in actors:
                    per = store.claim_contracts(actor)
                    batched = store.claim_contracts(actor, pairs)
                    probed = probe.sampled_contracts(store, actor, pairs)
                    for h, f in sorted(pairs | {("api://public.t/none", "row.c0")}):
                        want = per(h, {}, f)
                        self.assertEqual(batched(h, {}, f), want, (h, f))
                        self.assertEqual(probed(h, {}, f), want, (h, f))
                        seen.add(None if want is None else want["role"])
                    # a pair outside the batch is still answered, per call
                    self.assertEqual(store.claim_contracts(actor, set())(
                        "api://public.t/1", {}, "row.c0"), per("api://public.t/1", {}, "row.c0"))
            self.assertEqual(seen, {None, "ordered", "received"})
            asked = []
            real = ProjectionStore.claim_contracts

            def spy(self, actor, pairs=None):
                asked.append(pairs)
                return real(self, actor, pairs)
            with mock.patch.object(ProjectionStore, "claim_contracts", spy):
                probe.check_store(path, served_rows, actors[0], mock.Mock(object_types={}),
                                  normalizer=None, policy=mock.Mock(**{
                                      "may_read.return_value": True}), claim_sample=10)
            self.assertTrue(asked and asked[0], asked)


    def test_the_drift_guard_fails_when_the_store_lookup_changes(self):
        """The probe has one claim path, ``store.claim_contracts``: ``check_store``'s verdict
        is derived from its answers, so a changed store lookup (every answer withheld, or
        a role changed) changes the verdict instead of passing unnoticed."""
        from ontology.store import ProjectionStore
        from ontology.types import Actor
        from operating.ontology_tools import ReadPolicy as ServedPolicy
        actor = Actor("plx", frozenset({"staff:reader"}), "restricted")
        real = ProjectionStore.claim_contracts

        def changed(mode: str):
            def claim_contracts(self, reader, pairs=None):
                inner = real(self, reader, pairs)

                def contract(handle, resolved, field_path):
                    got = inner(handle, resolved, field_path)
                    if got is None or mode == "withhold":
                        return None
                    return dict(got, role="produced")
                return contract
            return claim_contracts

        served = ServedPolicy(tenant="plx", audiences={"api:public.t": {"staff:reader"}},
                              markings={"finance": {"staff:finance"}})
        with tempfile.TemporaryDirectory() as tmp:
            path = synthetic_store(tmp + "/r.sqlite", 60, restricted=True)

            def run():
                checks = probe.check_store(path, served_rows, actor,
                                           mock.Mock(object_types={}), normalizer=None,
                                           policy=served, claim_sample=60)
                checks.pop("claims_timing")
                return checks
            base = run()
            self.assertGreater(base["claims_passed"], 0, base)
            for mode in ("withhold", "role"):
                with mock.patch.object(ProjectionStore, "claim_contracts", changed(mode)):
                    got = run()
                self.assertNotEqual(got, base, mode)
                self.assertLess(got["claims_passed"], base["claims_passed"], mode)


class MirrorCompleteRuleTest(unittest.TestCase):
    """``mirror_complete`` is owner rule 1b5e555dce17 (complete bound relations over
    ``bound_total``, ``scripts.mirror_measure.completeness``), as mirror.full_coverage
    gates it: never the 93-relation served-catalog figure."""

    def mirror_complete(self, complete_bound: int) -> bool:
        from scripts.mirror_measure import completeness
        bound = [f"public.bound_{i}" for i in range(63)]
        served = bound + [f"public.unbound_{i}" for i in range(30)]   # 93 served

        def sync(client, db, **options):
            return {"relation_count": len(served), "rows_current_total": 1,
                    "completeness": completeness(bound[:complete_bound], bound=bound),
                    # an unbound relation left incomplete: the 93-relation figure fails
                    "all_stable_served_complete": False,
                    "relations": {}, "unstable_relations": []}

        def build(target, schema, sources, **options):
            Path(target).touch()
            return {"counts": {"objects": 1}, "content_digest": "d", "gaps": []}

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("scripts.mirror_measure.ordered_full_coverage", sync), \
                mock.patch("ontology.project.build_store", build), \
                mock.patch("ontology.project.MirrorCaptures", mock.MagicMock()):
            result = probe.projection_probe(None, f"{tmp}/db", f"{tmp}/store",
                                            schema=mock.Mock(object_types={}),
                                            normalizer=lambda *a: None, build_only=True)
        self.assertEqual(result["completeness"]["bound_total"], 63)
        self.assertEqual(result["mirror_relation_count"], 93)
        return result["mirror_complete"]

    def test_every_bound_relation_complete_is_a_complete_mirror(self):
        self.assertIs(self.mirror_complete(63), True)
        self.assertIs(self.mirror_complete(62), False)


class ServedPathTest(unittest.TestCase):
    """The probe reads as the plx tools serve its principal: fixture Workbench (and BC)."""

    def run_probe(self, tmp, *, mirror_audience=None, bc=None, client=True, fixture=True):
        from integrations.workbench_read_api import WorkbenchReadApiClient
        client = WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()) if client else None
        return probe.projection_probe(client, f"{tmp}/mirror.sqlite", f"{tmp}/store.sqlite",
                                      schema=compiled_schema() if fixture else None,
                                      normalizer=normalizer if fixture else None,
                                      claim_sample=100_000, principal="staff:reader",
                                      mirror_audience=mirror_audience, bc=bc)

    def served(self, tmp):
        """What the served host's tools answer the probe's principal, call by call:
        objects ``plx.object`` shows and ``api://`` citations ``plx.read`` resolves."""
        host = probe.served_host(f"{tmp}/store.sqlite")
        actor = probe.reader_actor(host)
        self.assertEqual(actor.principals, frozenset({"staff:reader"}))
        conn = sqlite3.connect(f"{tmp}/store.sqlite")
        objects = [r[0] for r in conn.execute("SELECT object_id FROM objects")]
        handles = sorted({r[0] for r in conn.execute(
            "SELECT l1_handle FROM object_values WHERE l1_handle LIKE 'api://%'")})
        conn.close()
        with host.served_tools(actor) as tools:
            # an unknown scheme or a handle naming no captured row fails closed
            unknown = probe.served_citations(tools, actor)(
                ["nope://x/y", "api://public.app_purchase_order/no-such-row"])
            self.assertEqual((unknown["resolved"], unknown["unresolved"]), (0, 2))
            shown = sum("error" not in tools.call("plx.object", {"object_id": o}, actor)
                        for o in objects)
            read = sum(tools.call("plx.read", {"citation": h}, actor).get("status")
                       == "resolved" for h in handles)
        return shown, read, len(handles)

    def test_the_probe_counts_what_the_served_tools_serve(self):
        from ontology import project
        from operating import build_steps
        real, lists = build_steps.post_project, []

        def post_project(*args):
            lists.append((args, real(*args)))
            return lists[-1][1]

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(build_steps, "post_project", post_project), \
                mock.patch.object(project, "build_store", wraps=project.build_store) as built:
            result = self.run_probe(tmp)
            shown, read, api_handles = self.served(tmp)
            expected = sorted(real(tmp, probe.TENANT))
        # every build (projection and rebuild) runs the host's one step list over the
        # served state, not a step list of the probe's own
        state = probe.served_state_path(f"{tmp}/store.sqlite")
        self.assertEqual([args for args, _ in lists], [(state, probe.TENANT)] * 2)
        self.assertEqual([c.kwargs["post_project"] for c in built.call_args_list],
                         [steps for _, steps in lists])
        self.assertEqual([sorted(steps) for _, steps in lists], [expected] * 2)
        self.assertTrue(result["all_sampled_claims_pass"], result)
        self.assertEqual(result["claim_sample"], result["values"])   # every value
        self.assertEqual(result["reader_objects"], shown)
        self.assertEqual(shown, result["objects"])
        self.assertEqual(result["citations"]["by_scheme"], {"api": read})
        self.assertEqual((read, result["citations"]["unresolved"]), (api_handles, 0))
        self.assertEqual(result["denied_leaks"], 0)

    def test_a_restricted_principal_counts_only_what_it_is_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            audience = os.path.join(tmp, "narrow.json")
            with open(audience, "w", encoding="utf-8") as handle:
                json.dump({"schema": "plx-source-mirror-audience/v1", "tenant": "plx",
                           "audiences": {"public.app_purchase_order": ["staff:reader"]}},
                          handle)
            result = self.run_probe(tmp, mirror_audience=audience)
            shown, read, api_handles = self.served(tmp)
            conn = sqlite3.connect(f"{tmp}/store.sqlite")
            granted, granted_values = conn.execute(
                "SELECT COUNT(DISTINCT o.object_id), COUNT(v.object_id) FROM objects o "
                "LEFT JOIN object_values v ON v.object_id = o.object_id "
                "WHERE o.audience_key = 'api:public.app_purchase_order'").fetchone()
            conn.close()
        self.assertEqual(result["reader_objects"], shown)
        self.assertEqual(shown, granted)
        self.assertTrue(0 < granted < result["objects"])
        # the trim probes what the narrowed audience withholds, and nothing of it is served
        self.assertEqual(result["reader_denied_objects_probed"], result["objects"] - granted)
        self.assertEqual(result["denied_leaks"], 0)
        # a value the reader is withheld is no claim: never counted as passed, and its
        # citation resolves for nobody (each one that did would be a leak)
        cited = result["citations"]
        self.assertTrue(0 < read < api_handles)
        self.assertEqual((cited["checked"], cited["resolved"], cited["unresolved"]),
                         (read, read, 0))
        self.assertEqual(result["claim_values_withheld"], result["values"] - granted_values)
        self.assertEqual(result["withheld_citations_probed"], api_handles - read)
        self.assertEqual(result["withheld_citations_resolved"], 0)
        self.assertEqual(result["claims_passed"], result["claims_checked"])
        self.assertTrue(result["all_sampled_claims_pass"], result)

    def test_a_withheld_citation_the_reader_resolves_is_a_leak(self):
        """A served reader that resolves what the reader's policy withholds (a resolver
        and a policy that disagree) is counted, and fails the spec's denied_leaks."""
        with tempfile.TemporaryDirectory() as tmp:
            self.run_probe(tmp)
            host = probe.served_host(f"{tmp}/store.sqlite")
            actor = probe.reader_actor(host)
            only_po = mock.Mock(**{"may_read.side_effect": lambda reader, key:
                                   key == "api:public.app_purchase_order"})
            with host.served_tools(actor) as tools:
                served = probe.served_rows(tools, actor)
                checks = probe.check_store(f"{tmp}/store.sqlite", served, actor,
                                           compiled_schema(), normalizer=None, policy=only_po,
                                           claim_sample=100_000)
            denied = probe.denied_checks(
                host, citation_leaks=checks["withheld_citations_resolved"])
        self.assertGreater(checks["withheld_citations_probed"], 0)
        self.assertEqual(checks["withheld_citations_resolved"],
                         checks["withheld_citations_probed"])
        self.assertEqual(denied["denied_leaks"], checks["withheld_citations_resolved"])

    def test_a_trim_that_did_not_run_is_not_checked_and_fails_the_spec(self):
        from devloop import live_verify as lv
        with tempfile.TemporaryDirectory() as tmp:
            probe.prepare_served(f"{tmp}/store.sqlite", "staff:reader")
            denied = probe.denied_checks(probe.served_host(f"{tmp}/store.sqlite"))
        self.assertEqual(set(denied.values()), {probe.NOT_CHECKED})
        for spec_id in ("ontology.projection", "ontology.mentions"):
            spec = json.loads((ROOT / f"devloop/live-checks/{spec_id}.json").read_text("utf-8"))
            failures = lv.check_expectations(spec, 0, {**denied, "citations": {}})
            self.assertIn("denied_leaks != expected", failures)
            self.assertIn("denied_objects_probed < 200", failures)
            self.assertIn("reader_denied_objects_probed < 50", failures)

    def test_bc_is_captured_as_the_host_captures_it(self):
        from integrations import bc_read
        from integrations.tests import test_bc_capture as tb
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_probe(tmp, client=False, fixture=False, bc={
                "environ": tb.ENV, "sender": tb.FakeBc(), "books": tb.BOOKS})
            conn = sqlite3.connect(probe.bc_capture_path(f"{tmp}/store.sqlite"))
            units = {r[0] for r in conn.execute("SELECT DISTINCT unit FROM runs")}
            conn.close()
        self.assertEqual(result["sources"], ["bc"])
        # every set the host captures (lines and journals come with their documents)
        self.assertLessEqual(set(bc_read.CAPTURE_ENTITY_SETS),
                             {unit.rsplit("/", 1)[-1] for unit in units})
        # captured under the served host's audience, so the host resolves its handles
        self.assertGreater(result["citations"]["by_scheme"]["bc"], 0)
        self.assertEqual(result["citations"]["unresolved"], 0)

    def test_a_superseded_bc_revision_counts_as_a_failed_claim(self):
        from integrations.tests import test_bc_capture as tb
        from ontology.project import _default_normalizer
        from ontology.schema import load_schema
        fake = tb.FakeBc()
        with tempfile.TemporaryDirectory() as tmp:
            store = f"{tmp}/store.sqlite"
            first = self.run_probe(tmp, client=False, fixture=False, bc={
                "environ": tb.ENV, "sender": fake, "books": tb.BOOKS})
            self.assertTrue(first["all_sampled_claims_pass"], first)
            # Every BC record changes upstream; the host's capture supersedes the cited
            # revisions while the projection still cites them.
            for rows in [*fake.data.values(), *fake.lines.values()]:
                for row in rows:
                    row["@odata.etag"] = row["@odata.etag"][:-1] + 'x"'
                    row["amount" if "amount" in row else "quantity"] += "9"
            probe.capture_bc({"environ": tb.ENV, "sender": fake, "books": tb.BOOKS},
                             probe.bc_capture_path(store), None,
                             **self.capture_identity(store))
            host = probe.served_host(store)
            actor = probe.reader_actor(host)
            with host.served_tools(actor) as tools:
                checks = probe.check_store(store, probe.served_rows(tools, actor), actor,
                                           load_schema(), normalizer=_default_normalizer(),
                                           policy=tools.policy_now(actor),
                                           claim_sample=100_000)
            # Rebuilt, the projection holds those revisions as history: they pass.
            rebuilt = self.run_probe(tmp, client=False, fixture=False, bc={
                "environ": tb.ENV, "sender": fake, "books": tb.BOOKS})
        changed = checks["claim_failures"].get("claim_changed_since", 0)
        self.assertGreater(changed, 0, checks)
        self.assertFalse(checks["all_sampled_claims_pass"])
        self.assertEqual(checks["claims_passed"] + changed, checks["claims_checked"])
        self.assertGreater(rebuilt["claims_checked"], checks["claims_checked"])
        self.assertTrue(rebuilt["all_sampled_claims_pass"], rebuilt)

    @staticmethod
    def capture_identity(store):
        """The audience and actor the probe captures BC under (the served host's)."""
        from integrations.live_source_guard import LiveSourceAudience
        from ontology.types import Actor
        from operating.host import HostConfig
        config = HostConfig.from_file(os.path.join(probe.served_state_path(store),
                                                   probe.SERVED_CONFIG))
        return {"audience": LiveSourceAudience.from_file(config.live_audience),
                "actor": Actor(config.tenant, frozenset({config.principal}), "restricted")}

class SharePointReadBackTest(unittest.TestCase):
    """``capture_sharepoint`` reads its capture back as two readers apart: one granted
    nothing and one granted the captured folders only (``folder_only_audience``)."""

    def test_the_withheld_and_folder_only_readers_are_distinct_principals(self):
        from integrations import capture_sharepoint as cs

        audience_file = ROOT / "examples" / "projection_probe_audience.live-check.json"
        seen = []

        def run_cli(args, **_kw):
            seen.append(args)
            return {"evidence_label": "injected-transport", "requests_sent": 0,
                    "runs": [{"complete": True, "incomplete_reasons": []}],
                    "summary": dict.fromkeys(("roots", "roots_complete", "files_current",
                                              "texts_extracted", "texts_ocr"), 0),
                    "leak_check": {"withheld_actor_rows": 0, "folder_only_actor_rows": 0}}

        with mock.patch.object(cs, "run_cli", run_cli):
            probe.capture_sharepoint({"scope": "s.json", "state": "st",
                                      "live_audience": str(audience_file),
                                      "principal": "svc:mail-capture", "environ": {}}, None)
        args = seen[0]
        readers = {args.withheld_principal, args.folder_only_principal}
        self.assertEqual(len(readers), 2, readers)
        self.assertNotIn(args.principal, readers)
        # the live audience grants neither: the folder-only reader's folders come from the
        # read-back's own audience alone, and the withheld reader holds nothing
        granted = {p for principals in json.loads(audience_file.read_text("utf-8"))[
            "audiences"].values() for p in principals}
        self.assertFalse(readers & granted, readers & granted)


class RemovalSpecTest(unittest.TestCase):
    """The ontology.projection spec reads the probe's removal counts (ING-09, REC-04)."""

    def test_the_spec_records_removals_and_fails_closed_when_not_known(self):
        from devloop import live_verify as lv
        from tests.test_ontology_projection_probe_removals import probe_after_removals
        with tempfile.TemporaryDirectory() as tmp:
            _, after = probe_after_removals(Path(tmp), 3)
        spec = json.loads((ROOT / "devloop/live-checks/ontology.projection.json")
                          .read_text("utf-8"))
        failures = lv.check_expectations(spec, 0, after)
        self.assertFalse([f for f in failures if "remov" in f], failures)
        recorded = lv._recorded(spec, after)
        self.assertEqual((recorded["removal_stretches"],
                          recorded["removal_reviews"]["withdrawn_source_removed"],
                          recorded["removal_reader"]["readable_with_label"],
                          recorded["removals_reach_projection"]),
                         ({"source_soft_delete": 3}, 1, 3, True))
        for broken in ({"removals_reach_projection": None},
                       {"removals_reach_projection": False},
                       {"removal_reader": {**after["removal_reader"], "withheld_leaks": 1}}):
            self.assertTrue([f for f in lv.check_expectations(spec, 0, {**after, **broken})
                             if "remov" in f], broken)


class SessionProjectionOpenTest(unittest.TestCase):
    """A consumer opens the session projection only when this verifier session committed it
    and its manifest names exactly that generation; anything else fails closed (None: the
    consumer projects cold). Fixture Workbench; nothing live."""

    def test_a_projection_moved_with_its_mirror_opens_and_only_over_the_pinned_rows(self):
        """The warm store moves a session's mirror and projection into a later session
        directory together: the manifest's ``mirror_relpath`` finds the moved mirror; a
        relative path to other rows, or an absolute one, never opens."""
        from datetime import datetime, timezone
        import shutil

        from integrations.workbench_read_api import WorkbenchReadApiClient
        from scripts.mirror_measure import ordered_full_coverage

        start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "first")
            (first / "m").mkdir(parents=True)
            mirror = str(first / "m" / "mirror.sqlite")
            ordered_full_coverage(WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()),
                                  mirror, resolve_sample=0)
            with contextlib.redirect_stdout(io.StringIO()):
                code = probe.main(["--produce-session", "--session-projection",
                                   str(first / "p" / "projection"), "--db", mirror],
                                  environ={})
            self.assertEqual(code, 0)
            later = Path(tmp, "later")
            shutil.move(first, later)
            session = str(later / "p" / "projection")
            opened = probe.open_session(session, start)
            self.assertIsNotNone(opened)
            self.assertEqual(opened["mirror"], str((later / "m" / "mirror.sqlite").resolve()))
            manifest_path = Path(session, probe.SESSION_MANIFEST)
            good = json.loads(manifest_path.read_text())
            other = Path(tmp, "other.sqlite")
            fake = FakeWorkbench()
            fake.set("public.app_purchase_order", "po-b", status="CLOSED")
            ordered_full_coverage(WorkbenchReadApiClient(BASE, KEY, fetch=fake), str(other),
                                  resolve_sample=0)
            for name, bad in (("other rows", os.path.relpath(other, session)),
                              ("absolute", str(later / "m" / "mirror.sqlite")),
                              ("missing", "../../m/none.sqlite")):
                manifest_path.write_text(json.dumps({**good, "mirror_relpath": bad}))
                self.assertIsNone(probe.open_session(session, start), name)

    def test_only_this_sessions_named_generation_opens(self):
        from datetime import datetime, timedelta, timezone

        from integrations.workbench_read_api import WorkbenchReadApiClient
        from scripts.mirror_measure import ordered_full_coverage

        def stamp(moment):
            return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        start = stamp(datetime.now(timezone.utc))
        with tempfile.TemporaryDirectory() as tmp:
            session, mirror = f"{tmp}/projection", f"{tmp}/mirror.sqlite"
            ordered_full_coverage(WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()),
                                  mirror, resolve_sample=0)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = probe.main(["--produce-session", "--session-projection", session,
                                   "--db", mirror], environ={})
            produced = json.loads(out.getvalue())
            self.assertEqual((code, produced["session_projection"]["committed"]), (0, True))
            opened = probe.open_session(session, start)
            self.assertEqual((opened["generation"].id, opened["bc"]),
                             (json.loads(Path(session, probe.SESSION_MANIFEST).read_text())
                              ["generation"], None))
            # No session start (outside a verifier session), or a later one: never.
            self.assertIsNone(probe.open_session(session, None))
            self.assertIsNone(probe.open_session(
                session, stamp(datetime.now(timezone.utc) + timedelta(seconds=1))))
            manifest_path = Path(session, probe.SESSION_MANIFEST)
            good = json.loads(manifest_path.read_text())
            for name, bad in (("another generation", {**good, "generation": "99999999"}),
                              ("another digest", {**good, "content_digest": "sha256:x"}),
                              ("another format", {**good, "format": "v0"}),
                              ("no sources", {k: v for k, v in good.items()
                                              if k != "sources"}),
                              ("a source without its capture", {**good, "mirror": None}),
                              ("not an object", ["generation"])):
                manifest_path.write_text(json.dumps(bad))
                self.assertIsNone(probe.open_session(session, start), name)
            # The producer read the session mirror in place: no copy of it in the session.
            self.assertEqual((opened["mirror"], good["mirror_file"]),
                             (str(Path(mirror).resolve()),) * 2)
            self.assertEqual(sorted(p.name for p in Path(session).rglob("*mirror*")), [])
            manifest_path.write_text(json.dumps({**good, "mirror_state": None}))
            self.assertIsNone(probe.open_session(session, start))
            manifest_path.write_text(json.dumps(good))
            # A top-up of the session mirror after the producer pinned it: never opened.
            ordered_full_coverage(WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()),
                                  mirror, resolve_sample=0)
            self.assertIsNone(probe.open_session(session, start))

            def produce():
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = probe.main(["--produce-session", "--session-projection", session,
                                       "--db", mirror], environ={})
                return code, json.loads(out.getvalue())
            code, again = produce()   # over the topped-up mirror: opens again
            self.assertEqual((code, again["disk"]["peak"]["mirror_bytes"]),
                             (0, probe.file_bytes(mirror)))
            self.assertIsNotNone(probe.open_session(session, start))
            # A re-run that fails after it pinned the mirror leaves no manifest: never the
            # earlier run's completeness counts beside this run's captures.
            from ontology import project_incremental
            with mock.patch.object(project_incremental, "build_generation",
                                   side_effect=disk_full(f"{tmp}/full.sqlite")):
                code, failed = produce()
            self.assertEqual((code, failed["code"], failed["phase"]),
                             (1, "SQLITE_FULL", "projection"))
            self.assertFalse(manifest_path.exists())
            self.assertIsNone(probe.open_session(session, start))
            # A writer that commits to the mirror while the producer holds it: no manifest.
            real = project_incremental.build_generation

            def topped_up(*args, **kwargs):
                ordered_full_coverage(WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()),
                                      mirror, resolve_sample=0)
                return real(*args, **kwargs)
            with mock.patch.object(project_incremental, "build_generation", topped_up):
                code, changed = produce()
            self.assertEqual((code, changed["error"]), (1, "session_mirror_changed"))
            self.assertFalse(manifest_path.exists())
            # No session mirror at all: an error, nothing committed (its consumers go cold).
            with contextlib.redirect_stdout(io.StringIO()) as missing:
                code = probe.main(["--produce-session", "--session-projection",
                                   f"{tmp}/other", "--db", f"{tmp}/none.sqlite"], environ={})
            self.assertEqual((code, json.loads(missing.getvalue())["error"]),
                             (1, "session_mirror_missing"))
            self.assertIsNone(probe.open_session(f"{tmp}/other", start))


class DiskTest(unittest.TestCase):
    """One full store at a time, and no full build without the free space it needs
    (module text, "Disk"). Fixture Workbench; ``statvfs`` forced low; nothing live."""

    run_probe = ServedPathTest.run_probe

    def test_the_rebuild_starts_after_the_first_store_is_freed(self):
        from ontology import project
        real, held = project.build_store, []

        def build(target, *args, **kwargs):
            held.append(sorted(p.name for p in Path(target).parent.rglob("*.sqlite*")
                               if p.name.startswith(("store", "ontology"))))
            return real(target, *args, **kwargs)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(project, "build_store", build):
            result = self.run_probe(tmp)
            served = probe.served_state_path(f"{tmp}/store.sqlite")
            left = sorted(p.name for p in Path(tmp).rglob("*.sqlite*")
                          if p.name.startswith(("store", "ontology")))
        # The rebuild began with neither the first store nor its served link on disk; the
        # checks ran on the first and the rebuild took its place.
        self.assertEqual(held, [[], []])
        self.assertTrue(result["rebuild_digest_equal"], result)
        self.assertTrue(result["all_sampled_claims_pass"])
        self.assertEqual(left, ["ontology.sqlite", "store.sqlite"], served)
        peak = result["disk"]["peak"]
        self.assertEqual(sorted(peak), sorted(probe.PEAK_KEYS))
        self.assertTrue(all(type(v) is int and v >= 0 for v in peak.values()), peak)
        self.assertGreater(peak["store_bytes"], 0)

    def test_a_build_short_of_free_space_never_starts(self):
        from devloop import live_verify as lv
        from ontology import project
        low = {"free_bytes": 1, "total_bytes": 10}
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp, "daily.json")
            report.write_text(json.dumps({"checks": [
                {"id": "a", "detail": {"fields": {"disk": {"peak": {
                    "build_bytes": 5, "layout": WORK_LAYOUT}}}}},
                {"id": "b", "detail": {"fields": {"disk": {"peak": {
                    "build_bytes": 9, "layout": WORK_LAYOUT}}}}},
                {"id": "c", "detail": {"fields": {"disk": {"peak": {
                    "build_bytes": "9", "layout": WORK_LAYOUT}}}}},
                # measured under another working-file layout (or none): not this need
                {"id": "e", "detail": {"fields": {"disk": {"peak": {"build_bytes": 99}}}}},
                {"id": "f", "detail": {"fields": {"disk": {"peak": {
                    "build_bytes": 99, "layout": WORK_LAYOUT - 1}}}}},
                {"id": "d", "detail": "junk"}]}))
            for need in (None, probe.previous_peak(str(report))):
                with mock.patch.object(probe, "_space", return_value=low), \
                        mock.patch.object(project, "build_store") as built, \
                        self.assertRaises(probe.DiskInsufficient) as refused:
                    self._probe(tmp, need)
                self.assertEqual(built.call_count, 0)   # no build started
                line = probe.error_line(refused.exception)
                expected = (need if need is not None else probe.BUILD_BYTES_PER_SOURCE_BYTE
                            * probe.file_bytes(f"{tmp}/mirror.sqlite"))
                self.assertEqual((line["error"], line["code"], line["phase"],
                                  line["need_bytes"], line["free_bytes"]),
                                 ("disk_insufficient", "disk_insufficient", "projection",
                                  expected, 1))
                # The verifier records the code and both byte counts from the line.
                kept = lv._envelope(line)
                self.assertEqual((kept["code"], kept["need_bytes"], kept["free_bytes"]),
                                 ("disk_insufficient", expected, 1))
                self.assertGreater(expected, 1)
            self.assertEqual(probe.previous_peak(str(report)), 9)
            self.assertIsNone(probe.previous_peak(f"{tmp}/none.json"))

    def _probe(self, tmp, need):
        from integrations.workbench_read_api import WorkbenchReadApiClient
        return probe.projection_probe(
            WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()), f"{tmp}/mirror.sqlite",
            f"{tmp}/store.sqlite", schema=compiled_schema(), normalizer=normalizer,
            claim_sample=100_000, principal="staff:reader", disk_need=need)

    def test_a_rebuild_short_of_free_space_keeps_the_checks_and_no_verdict(self):
        calls = []
        real = probe._Disk.require

        def require(disk, directory, sources):
            calls.append(directory)
            if len(calls) == 2:   # the rebuild's
                raise probe.DiskInsufficient(7, 3, directory)
            return real(disk, directory, sources)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(probe._Disk, "require", require):
            result = self._probe(tmp, None)
        self.assertEqual((result["error"], result["code"], result["phase"],
                          result["need_bytes"], result["free_bytes"]),
                         ("disk_insufficient", "disk_insufficient", "rebuild", 7, 3))
        self.assertNotIn("rebuild_digest_equal", result)
        self.assertTrue(result["all_sampled_claims_pass"])   # the checks ran on the first


class SessionBuildSplitTest(unittest.TestCase):
    """Where the session build's seconds go (decision 2026-10-19-projection-shared-cost:
    the producer's build is 97% of its live run and no daily said which part): the
    producer's report splits it by build step and the verifier records that split and
    the phase timings under the spec. Fixture Workbench mirror; nothing live."""

    def test_the_session_build_reports_and_records_its_split(self):
        from devloop import live_verify as lv
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from scripts.mirror_measure import ordered_full_coverage

        spec = json.loads((ROOT / "devloop/live-checks/ontology.session_projection.json")
                          .read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            mirror = f"{tmp}/mirror.sqlite"
            ordered_full_coverage(WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()),
                                  mirror, resolve_sample=0)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = probe.main(["--produce-session", "--session-projection",
                                   f"{tmp}/projection", "--db", mirror], environ={})
            result = json.loads(out.getvalue())
        self.assertEqual((code, result["session_projection"]["committed"]), (0, True), result)
        split = result["build_seconds_split"]
        # every build step: the fingerprint pass, each projector stage and the finish
        # (post_project steps, digest, commit); the report prints its keys sorted
        self.assertEqual(set(split), {"fingerprint", *probe.STAGE_KEYS, "finish"})
        self.assertTrue(all(type(v) is float and v >= 0 for v in split.values()), split)
        self.assertLessEqual(sum(split.values()), result["build_seconds"] + 0.05, result)
        recorded = lv._recorded(spec, result)
        self.assertEqual(set(recorded["build_seconds_split"]), set(split))
        self.assertIn("projection", recorded["phase_seconds"])


class ColdPathSingleBuildTest(unittest.TestCase):
    """ontology.mentions (decision 2026-10-14-mentions-cold-path, option 3): the mail run
    builds once on the cold path and records ``rebuild_digest_equal`` as not asserted
    (``cold_path_single_build``); on the session path it still rebuilds cold and asserts it.
    A mail consumer that builds on its own earlier generation checks the disk before it
    copies that store. FakeWorkbench and FakeGraph (``ProbeWithMailTests.probe_world``);
    nothing live."""

    SPEC = json.loads((ROOT / "devloop/live-checks/ontology.mentions.json")
                      .read_text(encoding="utf-8"))

    def world(self, tmp, **options):
        from ontology.tests.test_mail_documents import ProbeWithMailTests
        return ProbeWithMailTests.probe_world(self, tmp, None, **options)[0]

    def rebuild_failures(self, result, code=0):
        from devloop import live_verify as lv
        return [f for f in lv.check_expectations(self.SPEC, code, result) if "rebuild" in f]

    def session(self, tmp):
        """The session mirror synced, the session projection and its mail produced into
        the shared store; returns the session's start stamp."""
        from datetime import datetime, timedelta, timezone

        from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from scripts.mirror_measure import ordered_full_coverage

        start = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        ordered_full_coverage(WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()),
                              f"{tmp}/mirror.sqlite", resolve_sample=0)
        produced = self.world(tmp, produce=True, session=f"{tmp}/projection",
                              identity="sha256:mail")
        self.assertEqual((produced["session_projection"]["committed"],
                          produced["session_mail"]["captured"]), (True, True), produced)
        return start

    def test_the_cold_path_builds_once_and_does_not_assert_the_rebuild(self):
        from ontology import project
        real, builds, now = project.build_store, [], [0.0]

        def slow_build(target, *args, **kwargs):   # live b593c374: one build 1,000 s of 1,500
            builds.append(target)
            now[0] += 1000.0
            return real(target, *args, **kwargs)
        for session in (None, "projection"):   # no session, or one this session never made
            with self.subTest(session=session), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(project, "build_store", slow_build):
                builds.clear()
                result = self.world(tmp, budget_seconds=1500.0, monotonic=lambda: now[0],
                                    session=session and f"{tmp}/{session}")
            self.assertEqual(len(builds), 1, result)
            self.assertNotIn("status", result)   # no budget stop: nothing was skipped
            self.assertNotIn("rebuild_skipped", result)
            self.assertNotIn("rebuild", result["phase_seconds"])
            self.assertNotIn("rebuild_digest_equal", result)
            self.assertEqual((result["rebuild_digest_not_asserted"],
                              result["rebuild_digest_mismatches"]),
                             ("cold_path_single_build", 0))
            self.assertEqual(result["mention_denied_leaks"], 0)   # the checks still ran
            self.assertTrue(result["all_sampled_claims_pass"], result)
            self.assertEqual(self.rebuild_failures(result), [])
        # The spec fails closed on a report that names no rebuild verdict at all.
        bare = {k: v for k, v in result.items() if not k.startswith("rebuild_digest")}
        self.assertIn("missing rebuild_digest_mismatches", self.rebuild_failures(bare))

    def test_the_session_path_rebuilds_cold_and_asserts_the_digest(self):
        from ontology import project
        real, builds = project.build_store, []

        def build(target, *args, **kwargs):
            builds.append(target)
            report = real(target, *args, **kwargs)
            return {**report, "content_digest": "sha256:other"} if differ else report
        for differ in (False, True):
            with self.subTest(differ=differ), tempfile.TemporaryDirectory() as tmp:
                start = self.session(tmp)
                builds.clear()
                with mock.patch.object(project, "build_store", build):
                    result = self.world(tmp, session=f"{tmp}/projection", fresh_since=start,
                                        identity="sha256:mail")
            self.assertEqual((result["session_projection"]["mode"],
                              result["session_projection"]["mail_capture"]),
                             ("incremental", "session"), result)
            self.assertEqual(len(builds), 1)   # the cold rebuild beside the session build
            self.assertNotIn("rebuild_digest_not_asserted", result)
            self.assertEqual((result["rebuild_digest_equal"],
                              result["rebuild_digest_mismatches"]), (not differ, int(differ)))
            self.assertEqual(self.rebuild_failures(result),
                             ["rebuild_digest_mismatches > 0"] if differ else [])

    def test_a_rebuild_the_session_build_time_says_cannot_fit_is_skipped(self):
        # The reused session build costs about 0 s here; the rebuild is a cold build, so it
        # is forecast from the build_seconds the session manifest recorded, not from that.
        from ontology import project
        builds = []
        real = project.build_store

        def build(target, *args, **kwargs):
            builds.append(target)
            return real(target, *args, **kwargs)
        for recorded, skipped in ((100000.0, True), (0.5, False)):
            with self.subTest(recorded=recorded), tempfile.TemporaryDirectory() as tmp:
                start = self.session(tmp)
                manifest = Path(tmp, "projection", probe.SESSION_MANIFEST)
                document = json.loads(manifest.read_text("utf-8"))
                self.assertGreaterEqual(document["build_seconds"], 0)   # the producer's
                manifest.write_text(json.dumps({**document, "build_seconds": recorded}), "utf-8")
                builds.clear()
                with mock.patch.object(project, "build_store", build):
                    result = self.world(tmp, session=f"{tmp}/projection", fresh_since=start,
                                        identity="sha256:mail", budget_seconds=5000.0)
                self.assertEqual(result["session_projection"]["mode"] in ("reused", "incremental"),
                                 True, result)
                self.assertEqual(result.get("rebuild_skipped", False), skipped, result)
                self.assertEqual(len(builds), 0 if skipped else 1)
                if skipped:
                    self.assertEqual((result["status"], result["phase"]),
                                     ("budget_exhausted", "rebuild_skipped"))
                    self.assertTrue(result["all_sampled_claims_pass"])   # checks still ran
                    self.assertNotIn("rebuild_digest_equal", result)

    def test_a_copy_of_the_own_generation_short_of_disk_is_refused_before_it(self):
        from ontology import project_incremental
        low = {"free_bytes": 1, "total_bytes": 10}
        with tempfile.TemporaryDirectory() as tmp:
            start = self.session(tmp)
            kept = Path(f"{tmp}/store.sqlite.generations", "generations")

            def consume():
                return self.world(tmp, session=f"{tmp}/projection", fresh_since=start,
                                  identity="sha256:mail", build_only=True)
            first = consume()
            self.assertEqual(first["session_projection"]["mode"], "incremental", first)
            [own] = sorted(p.name for p in kept.iterdir())
            # Its own generation as it is: reused, nothing copied, no space needed.
            with mock.patch.object(probe, "_space", return_value=low):
                self.assertEqual(consume()["session_projection"]["mode"], "reused")
            # A unit key that no longer matches: the next build copies that store first.
            units = kept / own / "units.json"
            doc = json.loads(units.read_text())
            doc["keys"][sorted(doc["keys"])[0]] = "sha256:changed"
            units.write_text(json.dumps(doc))
            with mock.patch.object(probe, "_space", return_value=low), \
                    mock.patch.object(project_incremental.StoreBuilder, "__init__",
                                      side_effect=AssertionError("copied")), \
                    self.assertRaises(probe.DiskInsufficient) as refused:
                consume()
            line = probe.error_line(refused.exception)
            self.assertEqual((line["code"], line["phase"], line["free_bytes"]),
                             (probe.DISK_INSUFFICIENT, "projection", 1))
            self.assertGreater(line["need_bytes"],
                               probe.file_bytes(str(kept / own / "store.sqlite")))
            self.assertEqual(sorted(p.name for p in kept.iterdir()), [own])
            again = consume()   # with the space: built incrementally on its own generation
            self.assertEqual(again["session_projection"]["mode"], "incremental", again)


def disk_full(path: str) -> sqlite3.OperationalError:
    """SQLite's own SQLITE_FULL: a store capped at its current size, written past it."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v BLOB)")
    conn.commit()
    conn.execute(f"PRAGMA max_page_count = {conn.execute('PRAGMA page_count').fetchone()[0]}")
    try:
        conn.execute("INSERT INTO t VALUES (?)", (b"row value" * 4096,))
    except sqlite3.OperationalError as exc:
        return exc
    finally:
        conn.close()
    raise AssertionError("no SQLITE_FULL")


def locked(path: str) -> sqlite3.OperationalError:
    """SQLite's own SQLITE_BUSY: a write while another connection holds the write lock."""
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("CREATE TABLE IF NOT EXISTS t (v)")
    holder.execute("BEGIN IMMEDIATE")
    try:
        sqlite3.connect(path, timeout=0).execute("INSERT INTO t VALUES ('row value')")
    except sqlite3.OperationalError as exc:
        return exc
    finally:
        holder.close()
    raise AssertionError("no SQLITE_BUSY")


class SqliteErrorLineTest(unittest.TestCase):
    """A SQLite error that stops the probe names its SQLite code, the phase reached and the
    store that phase writes, never its message (live gap-70a8164228a8 printed only
    ``OperationalError`` with code null). Real SQLite errors over temp stores."""

    def run_main(self, argv, environ, patches):
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            for target, value in patches.items():
                stack.enter_context(mock.patch(target, value))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = probe.main(argv, environ=environ)
        text = out.getvalue()
        self.assertNotIn("row value", text)
        for fragment, _ in probe.SQLITE_MESSAGES:
            self.assertNotIn(fragment, text.lower())
        return code, json.loads(text)

    def test_a_full_disk_in_the_session_build_names_sqlite_full(self):
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from scripts.mirror_measure import ordered_full_coverage

        with tempfile.TemporaryDirectory() as tmp:
            mirror = f"{tmp}/mirror.sqlite"
            ordered_full_coverage(WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()),
                                  mirror, resolve_sample=0)
            full = disk_full(f"{tmp}/full.sqlite")

            def build_generation(*args, **kwargs):
                raise full
            code, line = self.run_main(
                ["--produce-session", "--session-projection", f"{tmp}/projection",
                 "--db", mirror], {},
                {"ontology.project_incremental.build_generation": build_generation})
        self.assertEqual((code, line["error"], line["code"]), (1, "OperationalError",
                                                               "SQLITE_FULL"))
        self.assertEqual((line["phase"], line["store"]), ("projection", "session_projection"))
        # numbers only: free and total bytes where the store and SQLite's temp files live
        self.assertEqual(set(line["disk"]), {"store_dir", "sqlite_temp_dir"})
        for space in line["disk"].values():
            self.assertEqual({key: type(value) for key, value in space.items()},
                             {"free_bytes": int, "total_bytes": int})
        self.assertNotIn(tmp, json.dumps(line))

    def test_a_check_after_the_build_names_the_projection_it_reads(self):
        # Over the session's projection the claim and trim checks read the session
        # generation the store names, not a store of the probe's own.
        from integrations import bc_read
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from scripts.mirror_measure import ordered_full_coverage

        with tempfile.TemporaryDirectory() as tmp:
            session, mirror = f"{tmp}/projection", f"{tmp}/mirror.sqlite"
            ordered_full_coverage(WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()),
                                  mirror, resolve_sample=0)
            env = {"PLX_WORKBENCH_API_URL": "https://x.invalid",
                   "PLX_WORKBENCH_API_KEY": "plxwb_" + "f" * 40,
                   bc_read.FRESH_SINCE_ENV: "2000-01-01T00:00:00.000000Z"}
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(["--produce-session", "--session-projection",
                                             session, "--db", mirror], environ={}), 0)
            for check, phase in (("check_store", "claims"), ("denied_checks", "denied")):
                full = disk_full(f"{tmp}/{check}.sqlite")
                code, line = self.run_main(
                    ["--db", mirror, "--store", f"{tmp}/{check}/store.sqlite",
                     "--session-projection", session], env,
                    {f"scripts.ontology_projection_probe.{check}": mock.Mock(side_effect=full)})
                self.assertEqual((code, line["code"], line["phase"], line["store"]),
                                 (1, "SQLITE_FULL", phase, "session_projection"))

    def test_a_locked_store_in_the_cold_build_names_sqlite_busy(self):
        clock = Clock()

        class Captures:
            def __init__(self, db): pass
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        env = {"PLX_WORKBENCH_API_URL": "https://x.invalid",
               "PLX_WORKBENCH_API_KEY": "plxwb_" + "f" * 40}
        with tempfile.TemporaryDirectory() as tmp:
            busy = locked(f"{tmp}/locked.sqlite")
            code, line = self.run_main(
                ["--db", f"{tmp}/db", "--store", f"{tmp}/store"], env,
                {"scripts.mirror_measure.ordered_full_coverage": slow_sync(clock, 1.0),
                 "ontology.project.MirrorCaptures": Captures,
                 "ontology.project.build_store": mock.Mock(side_effect=busy)})
        self.assertEqual((code, line["error"], line["code"], line["phase"], line["store"]),
                         (1, "OperationalError", "SQLITE_BUSY", "projection",
                          "ontology_store"))
        # the error line times the phases up to the stop, as a report does
        self.assertEqual(set(line["phase_seconds"]), {"start", "sync", "served", "projection"})
        for seconds in line["phase_seconds"].values():
            self.assertIsInstance(seconds, float)

    def test_an_unknown_sqlite_error_is_unclassified_without_its_message(self):
        # No SQLite name (Python before 3.11, or raised by Python) and no known message.
        unknown = sqlite3.OperationalError("near row value: syntax error")
        named = sqlite3.OperationalError("x")
        named.sqlite_errorname = "row value"   # not a SQLite name: never copied
        for exc in (unknown, named):
            line = probe.error_line(exc)
            self.assertEqual((line["code"], line["phase"]), (probe.SQLITE_UNCLASSIFIED, None))
            self.assertNotIn("row value", json.dumps(line))
        # Before 3.11 the message classifies the code.
        self.assertEqual(probe.sqlite_code(sqlite3.OperationalError(
            "database or disk is full")), "SQLITE_FULL")


if __name__ == "__main__":
    unittest.main()
