"""The traverse probe (live check ``ontology.traverse``) calls plx.around and
plx.timeline through the served host (``operating.host.Host.served_tools``) as the
projection probe's reader, so a narrowed audience narrows what it reaches and the
withheld roots and objects are measured, and a served answer naming what the reader's
policy withholds is a leak.

Evidence label: ``injected-transport`` (the fixture Workbench of
``ontology/tests/test_projection.py``); the live check stays live-unverified.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
from unittest import mock

from scripts import ontology_projection_probe as projection
from scripts import ontology_traverse_probe as probe


class Believed:
    """The served tools, answering as served, with the reader's policy ``policy``."""

    def __init__(self, served, policy, clock=None, tick=0.0):
        self.served, self.tools, self.policy = served, served.tools, policy
        self.clock, self.tick = clock, tick

    def call(self, name, arguments, actor):
        if self.clock is not None:
            self.clock.now += self.tick
        return self.served.call(name, arguments, actor)

    def policy_now(self, actor):
        return self.policy

    def __getattr__(self, name):   # the citation reader (served_citations) reads through
        return getattr(self.served, name)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@contextlib.contextmanager
def slow_builds(*, before: float = 0.0, after: float = 0.0, stray: bool = False,
                raises: BaseException | None = None):
    """The projection probe behind a consumer's ``main`` on a fake clock: each projection
    build advances it ``before`` seconds as it starts and ``after`` once it finished;
    ``stray``: a budget stop's partial report also carries keys no consumer reviewed
    (``STRAY``); ``raises``: each build raises it. Yields the targets built."""
    from ontology import project

    clock, built = Clock(), []
    real_probe, real_build = projection.projection_probe, project.build_store

    def build(target, *args, **kwargs):
        built.append(target)
        clock.now += before
        if raises is not None:
            raise raises
        result = real_build(target, *args, **kwargs)
        clock.now += after
        return result

    def run_probe(*args, **kwargs):
        report = real_probe(*args, monotonic=clock, **kwargs)
        if stray and report.get("status") == "budget_exhausted":
            report["row_value"] = STRAY
            report["bc_capture"]["sample_key"] = STRAY
            report["completeness"]["bound_incomplete"].append(STRAY)
            report["build_seconds"] = {"seconds": report["build_seconds"], "first_id": STRAY}
        return report

    with mock.patch.object(projection, "projection_probe", run_probe), \
            mock.patch.object(project, "build_store", build):
        yield built


def consumer_run(main, tmp, *extra, before=0.0, after=0.0, budget=1000.0, stray=False,
                 raises=None):
    """``main`` (a consumer of the projection build) over the fixture world (Portal and BC
    injected) under ``slow_builds``."""
    from tests import test_live_wiring as wiring
    fake, books = wiring.fixture_bc()
    with wiring.fixture_portal(), slow_builds(before=before, after=after, stray=stray,
                                                 raises=raises) as built:
        code, document = wiring.run_main(
            main, ["--db", f"{tmp}/mirror.sqlite", "--store", f"{tmp}/ontology.sqlite",
                   "--budget-seconds", str(budget), "--bc-capture", *extra],
            environ={**wiring.PORTAL_ENV, **wiring.BC_ENV}, bc_sender=fake, bc_books=books)
    return code, document, built


STRAY = "api://unreviewed-value"


def assert_build_partial_passed(test, code, document):
    """A budget stop inside the build: the consumer's own status next to the build's
    partial counts, and nothing outside ``projection.PARTIAL_FIELDS`` (``STRAY``: an
    unlisted key at the top and in a block, and a count of the wrong kind)."""
    test.assertEqual(code, projection.EXIT_BUDGET, document)
    test.assertEqual((document["status"], document["phase"], document["stage"]),
                     ("budget_exhausted", "projection", "projection_build"))
    test.assertEqual(document["bc_capture"]["evidence_label"], "injected-transport")
    test.assertGreater(document["bc_capture"]["records_current"], 0)
    test.assertEqual(document["blocked_upstream"], [])
    test.assertGreater(document["objects"], 0)
    test.assertGreater(document["values"], 0)
    test.assertIsNotNone(document["maxrss_mb"])
    test.assertGreater(document["completeness"]["bound_total"], 0)
    test.assertNotIn("api://", json.dumps(document))
    test.assertNotIn("bound_incomplete", document["completeness"])
    test.assertNotIn("build_seconds", document)   # a count of the wrong kind is no count
    test.assertEqual(set(document) - {"probe", "status", "stage"},
                     set(document) & set(projection.PARTIAL_FIELDS))


def assert_disk_counts(test, document, tmp):
    """An error line's ``disk``: free and total bytes of each ``projection.DISK_DIRS``."""
    test.assertEqual(set(document["disk"]), set(projection.DISK_DIRS))
    for space in document["disk"].values():
        test.assertEqual(set(space), {"free_bytes", "total_bytes"})
        for count in space.values():
            test.assertIs(type(count), int)
        test.assertGreater(space["total_bytes"], 0)
        test.assertLessEqual(space["free_bytes"], space["total_bytes"])
    test.assertNotIn(tmp, json.dumps(document))   # no path, only numbers


class BuildOnceTest(unittest.TestCase):
    """The traversal needs one build: the rebuild and the claim sample are the projection
    check's (``ontology.projection``), never a reason to stop the traversal."""

    def test_a_build_over_half_the_budget_still_traverses_without_a_rebuild(self):
        # 600 s of a 1000 s budget: a second build of that size could not fit.
        with tempfile.TemporaryDirectory() as tmp:
            code, document, built = consumer_run(probe.main, tmp, "--sample", "5",
                                                 before=600.0)
        self.assertEqual(built, [f"{tmp}/ontology.sqlite"])   # no rebuild
        self.assertNotIn("status", document)
        self.assertGreater(document["around_calls"], 0)
        self.assertFalse(document["budget_exhausted"])
        self.assertEqual(code, 0, document)

    def test_a_budget_stop_passes_the_builds_partial_counts_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, document, built = consumer_run(probe.main, tmp, after=1000.0, stray=True)
        self.assertEqual(len(built), 1)
        assert_build_partial_passed(self, code, document)


class BuildFailureTest(unittest.TestCase):
    """A failed build names its SQLite cause in every consumer's result (live
    ontology.traverse, workspace.object_view and process.dependencies printed only
    ``projection_build_failed``): the build's code, phase and store, and the free and total
    bytes of the store's and SQLite's temp directories; numbers and names only."""

    def test_a_full_disk_in_the_build_names_sqlite_full_in_every_consumer(self):
        import sqlite3

        from scripts import object_workspace_probe, process_dependencies_probe
        full = sqlite3.OperationalError("database or disk is full")
        for main in (probe.main, object_workspace_probe.main, process_dependencies_probe.main):
            with self.subTest(main.__module__), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {"SQLITE_TMPDIR": f"{tmp}/sqlite-temp"}):
                code, document, built = consumer_run(main, tmp, raises=full)
                self.assertEqual(built, [f"{tmp}/ontology.sqlite"])
                self.assertEqual(code, 1, document)
                self.assertEqual(
                    {key: document[key] for key in ("error", "stage", "build_error", "code",
                                                    "phase", "store")},
                    {"error": "projection_build_failed", "stage": "projection_build",
                     "build_error": "OperationalError", "code": "SQLITE_FULL",
                     "phase": "projection", "store": "ontology_store"})
                assert_disk_counts(self, document, tmp)

    def test_a_full_disk_after_the_build_names_sqlite_full_and_the_consumers_phase(self):
        # A built store, then the traversal / object view raises: the error line is the
        # projection's (code, phase, disk), never the message.
        import sqlite3

        from scripts import object_workspace_probe, process_dependencies_probe
        for module, check, phase in ((probe, "check_traversal", "traversal"),
                                     (object_workspace_probe, "check_views", "object_view"),
                                     (process_dependencies_probe, "check_dependencies",
                                      "process_dependencies")):
            full = sqlite3.OperationalError("database or disk is full")
            with self.subTest(module.__name__), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {"SQLITE_TMPDIR": f"{tmp}/sqlite-temp"}), \
                    mock.patch.object(module, check, side_effect=full):
                code, document, built = consumer_run(module.main, tmp)
                self.assertEqual(built, [f"{tmp}/ontology.sqlite"])
                self.assertEqual(code, 1, document)
                self.assertEqual(
                    {key: document[key] for key in ("probe", "error", "code", "phase",
                                                    "store")},
                    {"probe": module.PROBE, "error": "OperationalError",
                     "code": "SQLITE_FULL", "phase": phase, "store": None})
                assert_disk_counts(self, document, tmp)
                self.assertNotIn("full", json.dumps(document))   # no message text

    def test_only_reviewed_fields_of_a_build_line_pass(self):
        line = {"probe": projection.PROBE, "error": "OperationalError", "code": "SQLITE_FULL",
                "phase": "projection", "store": "ontology_store", "message": "row value",
                "disk": {"store_dir": {"free_bytes": 1, "total_bytes": 2, "path": "/tmp/x"},
                         "sqlite_temp_dir": None, "other_dir": {"free_bytes": 3}}}
        self.assertEqual(projection.build_failure(json.dumps(line)), {
            "build_error": "OperationalError", "code": "SQLITE_FULL", "phase": "projection",
            "store": "ontology_store",
            "disk": {"store_dir": {"free_bytes": 1, "total_bytes": 2}}})   # unmeasured: absent
        # free text or a wrong kind is dropped, and output that is no JSON object gives none
        self.assertEqual(projection.build_failure(json.dumps(
            {"error": "database or disk is full", "code": 13,
             "disk": {"store_dir": {"free_bytes": True, "total_bytes": 2.5}}})),
            {"disk": {"store_dir": {}}})
        for text in ("", "Traceback (most recent call last):", "[1]", '{"a": 1}\n{"b": 2}'):
            self.assertEqual(projection.build_failure(text), {}, text)


class ServedTraversalTest(unittest.TestCase):

    def traverse(self, tmp, *, audience=None, policy=None, alter=None, budget=None,
                 tick=0.0):
        """Build the fixture store through the projection probe, then traverse it through
        the served host as its reader (``policy``: what the probe believes the reader is
        granted, default the served one; ``alter``: called with a writable connection to
        the built store before it is served; ``tick``: seconds each served call takes on
        the probe's clock, under ``budget``)."""
        import sqlite3

        from integrations.workbench_read_api import WorkbenchReadApiClient
        from ontology.tests.test_projection import (BASE, KEY, FakeWorkbench,
                                                    compiled_schema, normalizer)
        from operating import ontology_tools as ot
        store = f"{tmp}/store.sqlite"
        projection.projection_probe(
            WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench()), f"{tmp}/mirror.sqlite",
            store, schema=compiled_schema(), normalizer=normalizer, claim_sample=10,
            principal="staff:reader", mirror_audience=audience)
        if alter is not None:
            with contextlib.closing(sqlite3.connect(store)) as writable:
                alter(writable)
                writable.commit()
        host = projection.served_host(store)
        reader = projection.reader_actor(host)
        conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True, check_same_thread=False)
        clock = Clock()
        try:
            with host.served_tools(reader) as served:
                served = Believed(served, policy or served.policy_now(reader), clock, tick)
                return probe.check_traversal(
                    served, reader, conn, ot.SchemaView.from_compiled(compiled_schema()),
                    sample=10, resolve=projection.served_citations(served, reader),
                    budget_seconds=budget, monotonic=clock)
        finally:
            conn.close()

    def test_a_reader_granted_everything_reaches_and_leaks_nothing(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            report = self.traverse(tmp)
            with contextlib.closing(sqlite3.connect(f"{tmp}/store.sqlite")) as conn:
                ids = [oid for (oid,) in conn.execute("SELECT object_id FROM objects")]
        self.assertEqual((report["contract_failures"], report["denied_leaks"],
                          report["order_violations"], report["reader_denied_probed"]),
                         (0, 0, 0, 0))
        self.assertGreater(report["around_reached"], 0)
        self.assertEqual(report["denied_probed"], report["around_calls"]
                         + report["timeline_calls"])
        self.assertEqual(report["citations"]["unresolved"], 0)
        self.assertNotIn("api://", json.dumps(report))
        self.assertEqual((report["unexpected_not_found"], report["budget_exhausted"]),
                         (0, False))
        self.assertEqual(report["around_types_probed"], report["around_types"])
        self.assertGreater(report["timeline_calls"], 0)
        # per-phase counts: every answered call passes each phase of its tool once,
        # and the store facts are read once for the whole run (one store state)
        phases = report["phases"]
        answered = report["around_calls"] - sum(report["error_codes"].values())
        self.assertEqual({p["calls"] for p in phases["around"].values()}, {answered})
        self.assertEqual({p["calls"] for p in phases["timeline"].values()},
                         {report["timeline_calls"]})
        self.assertEqual({p["calls"] for p in phases["store"].values()}, {1})
        # the slowest around calls' work: types, link types, plan names and counts only
        slowest = report["slowest_around"]
        self.assertEqual(len(slowest), min(probe.SLOWEST_AROUND, report["around_calls"]))
        self.assertEqual(slowest, sorted(slowest, key=lambda e: -e["seconds"]))
        for entry in slowest:
            self.assertEqual(set(entry), {"root_type", "hops", "seconds", "vm_steps",
                                          "heaviest", "heaviest_steps", "plan"})
            self.assertRegex(entry["root_type"], r"^[A-Z][A-Za-z0-9]*$")
            for hop in entry["hops"]:
                self.assertRegex(hop, r"^[A-Z][A-Za-z0-9]*\.[a-z_]+:(forward|reverse)$")
            self.assertGreaterEqual(entry["vm_steps"], entry["heaviest_steps"])
            self.assertTrue(all(":" in step for step in entry["plan"]))
        rendered = json.dumps(slowest)
        self.assertTrue(ids)
        for oid in ids:
            self.assertNotIn(oid, rendered)

    def test_edges_comparing_join_columns_no_property_stores_count_as_cited(self):
        """Live, most fk links compare a join column no property projects: no stored
        value holds the cited cell, and the edges must still carry their citations."""
        def unstore(conn):
            cells = {(c["l1_handle"], c["field_path"]) for (reason,) in conn.execute(
                "SELECT reason FROM links") for f in json.loads(reason)
                for c in f.get("citations", ())}
            conn.executemany("DELETE FROM object_values WHERE l1_handle = ? AND "
                             "field_path = ?", sorted(cells))
        with tempfile.TemporaryDirectory() as tmp:
            report = self.traverse(tmp, alter=unstore)
        self.assertGreater(report["around_cited_edges"], 0)
        self.assertEqual(report["around_cited_edges"], report["around_edges"])
        self.assertEqual((report["denied_leaks"], report["citations"]["unresolved"]), (0, 0))

    def test_a_budget_the_around_half_would_use_up_still_runs_the_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            full = self.traverse(tmp)
        with tempfile.TemporaryDirectory() as tmp:
            # each probe step is two served calls (reader, withheld): 2 s on the clock
            report = self.traverse(tmp, budget=10.0, tick=1.0)
        self.assertGreater(2 * full["around_calls"], 10.0)   # the around half alone
        self.assertEqual(report["around_calls"], 3)   # stops at its half (5 s)
        self.assertGreaterEqual(report["timeline_calls"], 1)
        self.assertTrue(report["budget_exhausted"])
        self.assertEqual(report["budget_unsampled"]["around"],
                         full["around_calls"] - report["around_calls"])
        self.assertEqual(report["around_types_probed"], 3)   # breadth first: 3 types once
        self.assertEqual(report["call_seconds"]["around"], 6.0)
        self.assertEqual(report["max_call_seconds"]["around"], 2.0)

    def test_roots_the_store_does_not_hold_are_unsampled_not_object_not_found(self):
        """A link end the store does not hold or holds with no current version is never
        a root (counted per reason); a root a marking withholds reads as not found and is
        counted as withheld, never as a lookup failure."""
        def alter(conn):
            ends = [row[0] for row in conn.execute(
                "SELECT DISTINCT from_object FROM links ORDER BY 1")]
            conn.execute("INSERT INTO links SELECT link_id || '-dangling', link_type, "
                         "from_object || '-absent', to_object, tier, binding_kind, "
                         "link_contract, reason, why_not, evidence_handles, reviewed_by, "
                         "reviewed_at, review_action_id, markings, recorded_at, superseded_at "
                         "FROM links WHERE from_object = ?", (ends[0],))
            conn.execute("UPDATE object_versions SET superseded_at = recorded_at "
                         "WHERE object_id = ?", (ends[1],))
            conn.execute("UPDATE object_versions SET markings = '[\"finance\"]' "
                         "WHERE object_id = ?", (ends[2],))
        with tempfile.TemporaryDirectory() as tmp:
            report = self.traverse(tmp, alter=alter)
        self.assertGreaterEqual(report["unsampled"].get("absent_from_store", 0), 1)
        self.assertGreaterEqual(report["unsampled"].get("no_current_version", 0), 1)
        self.assertGreater(report["reader_denied_by"].get("marking", 0), 0)
        self.assertEqual(report["error_codes"].get("object_not_found", 0),
                         report["reader_denied_probed"])
        self.assertEqual((report["unexpected_not_found"], report["denied_leaks"]), (0, 0))

    def test_a_narrowed_reader_is_served_only_its_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            audience = os.path.join(tmp, "narrow.json")
            with open(audience, "w", encoding="utf-8") as handle:
                json.dump({"schema": "plx-source-mirror-audience/v1", "tenant": "plx",
                           "audiences": {"public.app_purchase_order": ["staff:reader"]}},
                          handle)
            narrowed = self.traverse(tmp, audience=audience)
        with tempfile.TemporaryDirectory() as tmp:
            full = self.traverse(tmp)
        self.assertEqual(narrowed["around_calls"], full["around_calls"])   # the same roots
        self.assertGreater(narrowed["reader_denied_probed"], 0)
        # every withheld root reads as not found, and no answer names a withheld object
        self.assertGreaterEqual(narrowed["error_codes"].get("object_not_found", 0),
                                narrowed["reader_denied_probed"])
        self.assertEqual(narrowed["denied_leaks"], 0)
        self.assertLess(narrowed["around_reached"], full["around_reached"])
        self.assertLess(narrowed["citations"]["checked"], full["citations"]["checked"])
        self.assertEqual(narrowed["citations"]["unresolved"], 0)

    def test_a_served_answer_the_readers_policy_withholds_is_a_leak(self):
        """The served tools answer under the host's audience (everything granted), but the
        reader's policy grants it nothing: every root it is answered and every object an
        answer names is counted."""
        from operating.ontology_tools import ReadPolicy
        with tempfile.TemporaryDirectory() as tmp:
            report = self.traverse(tmp, policy=ReadPolicy("plx", {}))
        self.assertEqual(report["reader_denied_probed"],
                         report["around_calls"] + report["timeline_calls"])
        self.assertGreater(report["denied_leaks"], 0)


if __name__ == "__main__":
    unittest.main()
