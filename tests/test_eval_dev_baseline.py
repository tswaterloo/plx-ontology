"""eval.dev_baseline: the live run stops on its budget with a whole record, and an item of the
class that dominated the live failures (an abstention on an answerable item whose identifier
one record holds in two systems) answers through the real investigation path.

The live check timed out at 2,400 s on 2026-10-10 and left no record; on 2026-10-07 every
answerability_mismatch (20 of 26) was an abstention on an item the oracle answers, each on a
root plx.find could not settle (the live-results branch results/daily/4ea6073508fc...: abstained
20, abstain_ok 0, root_resolution 10 found of 30). A Sage-loaded sales order is one record
held in Portal gold and in its Sage order header under the same number. On 2026-10-11 an F3
item cited a relation three links from its root while the spec mirrors two, so its run pin
found no capture (pin_drift no_capture_at_or_after_time): the sync now captures every
relation the drawn items cite, for the run pins only: the run projects the schema's
relations, never one the answer key alone cites. The
spec's ``--run-id auto`` reads the run's wall clock, which the tests pin.
Evidence: isolated-fixture / injected-transport, never live.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# The spec's ``--run-id auto`` draws the day's items: the tests pin the day, never today's.
PINNED_NOW = "2026-10-11T23:59:59+00:00"


SPEC = json.loads((ROOT / "devloop" / "live-checks" / "eval.dev_baseline.json").read_text())


def pinned_now():
    from datetime import datetime
    return datetime.fromisoformat(PINNED_NOW)


class _ArmedPortal:
    """The oracle's Portal reads with a hook on the run pins' lookups: once armed (the
    product's mirror sync started, so the oracle is done), the first lookup (the run pin of
    the first item citing a Portal row) spends the run's budget, and with ``block`` stays
    in it until the watchdog has cut the run (a phase no budget reaches)."""

    def __init__(self, inner, *, armed, spend, block):
        self._inner, self._armed, self._spend, self._block = inner, armed, spend, block

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def lookup(self, *args, **kwargs):
        if self._armed.is_set() and self._spend():
            if self._block is not None:
                self._block.wait(60)
        return self._inner.lookup(*args, **kwargs)


class BudgetStopTests(unittest.TestCase):
    """The spec's own argv over the fixture world (injected transports and clock)."""

    def run_spec(self, *, cut_by_watchdog: bool, in_projection: bool = False):
        """``in_projection``: the projection itself spends the budget and stays in it
        until the watchdog has cut the run (else the first run pin of an item does)."""
        from ontology import project
        from scripts import eval_dev_baseline as script
        from integrations import child_process
        from scripts import eval_oracle_dev as dev

        offset, exits = [0.0], []
        armed, cut = threading.Event(), threading.Event()

        def spend():
            if offset[0]:
                return False
            offset[0] = 10_000.0   # past the deadline and the watchdog's grace
            return True

        def hard_exit(code):
            exits.append(code)
            cut.set()

        def build(env, args, *, guard):
            sources, documents, configured, _ = dev.build_fixture(dev.eval_world())
            if not in_projection:
                sources.portal = _ArmedPortal(sources.portal, armed=armed, spend=spend,
                                              block=cut if cut_by_watchdog else None)
            return sources, documents, configured

        real_build = project.build_store

        def slow_build(*args, **kwargs):
            if in_projection and spend():
                cut.wait(60)
            return real_build(*args, **kwargs)

        def mirror_clock():
            armed.set()
            return script.fixture_clock()
        spec = json.loads((ROOT / "devloop" / "live-checks" / "eval.dev_baseline.json")
                          .read_text())
        out = io.StringIO()
        # The injected hard_exit returns, so the closed state the watchdog's abandon
        # leaves (os._exit would discard it) is restored for the tests after.
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out), \
                mock.patch.object(project, "build_store", slow_build), \
                mock.patch.object(child_process, "_closed", False):
            argv = [arg.replace("{tmp}", tmp).replace("{session_state}", tmp + "/session")
                    for arg in spec["command"][3:]]
            argv = argv[:argv.index("--count")] + ["--count", "8", "--min-items", "4"] + \
                argv[argv.index("--total-budget-seconds"):] + ["--state-dir", tmp + "/state"]
            code = script.main(argv, environ={}, live_build=build,
                               live_client=script.fixture_client(), live_clock=mirror_clock,
                               live_now=pinned_now, monotonic=lambda: time.monotonic() + offset[0],
                               hard_exit=hard_exit,
                               hard_stop_grace=60.0 if cut_by_watchdog else 1e9)
        text = out.getvalue()
        document = json.loads(text)   # exactly one record: a second would not parse
        return spec, code, exits, document

    def assert_partial_record(self, spec, code, document):
        from devloop import live_verify
        block = document["eval"]
        self.assertEqual(code, 3)
        self.assertEqual(block["stopped"], {"phase": "items", "reason": "run_budget_exhausted"})
        # The items measured before the cut are scored; the rest are counted, never scored.
        self.assertEqual(block["answer"]["sampled"], block["measured"])
        self.assertEqual(block["items"], block["measured"])
        self.assertGreater(block["items_total"], block["measured"])
        self.assertFalse(block["complete"])
        self.assertIn("run_budget_exhausted", block["incomplete"])
        self.assertLessEqual({"oracle", "mirror_sync", "projection", "items"},
                             set(block["phases"]))
        self.assertGreater(document["product"]["objects"], 0)
        # The verifier reads a whole record: it fails on the cut, never on a missing field.
        failures = live_verify.check_expectations(spec, code, document)
        self.assertIn("eval.complete != expected", failures)
        self.assertFalse([f for f in failures if f.startswith("missing")], failures)
        recorded = live_verify._recorded(spec, document)["eval"]
        for key in ("stopped", "phases", "measured", "items_total", "incomplete"):
            self.assertEqual(recorded[key], block[key], key)
        self.assert_item_rows(spec, document)

    def assert_item_rows(self, spec, document):
        """One family/root/outcome/reason row per drawn item, in the verifier's capped
        record: the measured ones as scored, the rest not measured where the cut stopped."""
        from devloop import live_verify
        block, rows = document["eval"], document["item_rows"]
        self.assertEqual(len(rows), block["items_total"])
        self.assertEqual(sum(":not_measured:" not in row for row in rows), block["measured"])
        self.assertTrue(all(row.endswith(":not_measured:" + block["stopped"]["phase"])
                            for row in rows if ":not_measured:" in row), rows)
        detail = live_verify._cap({"fields": live_verify._recorded(spec, document)})
        self.assertEqual(detail["fields"]["item_rows"], rows)

    def test_items_stop_on_the_deadline_with_the_measured_ones_recorded(self):
        spec, code, exits, document = self.run_spec(cut_by_watchdog=False)
        self.assertEqual(exits, [])
        self.assert_partial_record(spec, code, document)
        self.assertGreaterEqual(document["eval"]["measured"], 1)   # the item in flight ends

    def test_a_phase_past_the_deadline_is_cut_by_the_watchdog_with_a_record(self):
        spec, code, exits, document = self.run_spec(cut_by_watchdog=True)
        self.assertEqual(exits, [3])
        self.assert_partial_record(spec, code, document)

    def test_a_watchdog_cut_in_the_projection_still_counts_every_drawn_item(self):
        """102342432f14 items 2 and 3: a cut before any item ran records the oracle's
        items (items_total, item_rows), none measured, and the phase it stopped in."""
        from devloop import live_verify
        spec, code, exits, document = self.run_spec(cut_by_watchdog=True, in_projection=True)
        block = document["eval"]
        self.assertEqual((code, exits), (3, [3]))
        self.assertEqual(block["stopped"],
                         {"phase": "projection", "reason": "run_budget_exhausted"})
        self.assertEqual((block["measured"], block["items"]), (0, 0))
        self.assertGreater(block["items_total"], 0)
        self.assertFalse(block["complete"])
        self.assert_item_rows(spec, document)
        failures = live_verify.check_expectations(spec, code, document)
        self.assertFalse([f for f in failures if f.startswith("missing")], failures)


class SessionProjectionTests(unittest.TestCase):
    """With the verifier session's projection committed in this
    session, the spec's own argv neither syncs the whole plan, captures BC nor projects: it
    opens that generation read-only and every drawn item runs on it (isolated-fixture).
    Its one capture is a top-up of its own mirror copy for the relations the run pins read
    that the producer captured before the items' time (``pin_sync``): the 2026-10-20 daily reused a producer mirror captured before the oracle
    drew the items, and all 12 F3/F4 items drifted with no_capture_at_or_after_time."""

    def run_session(self, producer_clock, client=None):
        """The spec's argv over a session the producer captured at ``producer_clock``:
        (producer exit, document, projections built, session mirror's capture count before
        and after the run, the drawn items' root identifiers)."""
        import sqlite3
        from datetime import datetime, timedelta, timezone

        from devloop import live_verify
        from ontology import project
        from scripts import eval_dev_baseline as script
        from scripts import eval_oracle_dev as dev
        from scripts import ontology_projection_probe as projection
        from scripts.mirror_measure import ordered_full_coverage

        builds, roots = [], set()
        real = project.Projector.project_into

        def counted(projector, builder):
            builds.append(projector)
            return real(projector, builder)

        def build(env, args, *, guard):
            sources, documents, configured, _ = dev.build_fixture(dev.eval_world())
            return sources, documents, configured

        def captures(path):
            with contextlib.closing(sqlite3.connect(path)) as db:
                return db.execute("SELECT COUNT(*) FROM capture").fetchone()[0]
        real_execute = dev.execute

        def execute(*args, **kwargs):
            oracle, items = real_execute(*args, **kwargs)
            roots.update(str(i["root"]["identifier"]) for i in items)
            return oracle, items
        start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(project.Projector, "project_into", counted), \
                mock.patch.object(dev, "execute", execute):
            session = tmp + "/session"
            ordered_full_coverage(script.fixture_client(), session + "/mirror.sqlite",
                                  resolve_sample=0, clock=producer_clock)
            with contextlib.redirect_stdout(io.StringIO()):
                produced = projection.main(["--produce-session", "--session-projection",
                                            session + "/projection", "--db",
                                            session + "/mirror.sqlite"], environ={})
            before = captures(session + "/mirror.sqlite")
            argv = [arg.replace("{tmp}", tmp).replace("{session_state}", session)
                    for arg in SPEC["command"][3:]]
            argv = argv[:argv.index("--count")] + ["--count", "8", "--min-items", "4"] + \
                argv[argv.index("--total-budget-seconds"):] + ["--state-dir", tmp + "/state"]
            class RolledOver(datetime):
                @classmethod
                def now(cls, tz=None):
                    return pinned_now() + timedelta(seconds=2)
            with contextlib.redirect_stdout(out), \
                    mock.patch.object(script, "datetime", RolledOver):
                code = script.main(argv, environ={live_verify.CAPTURE_FRESH_SINCE_ENV: start},
                                   live_build=build,
                                   live_client=client or script.fixture_client(),
                                   live_clock=script.fixture_clock, live_now=pinned_now)
            after = captures(session + "/mirror.sqlite")
        return produced, code, json.loads(out.getvalue()), builds, (before, after), roots

    def test_the_run_opens_the_session_projection_instead_of_projecting(self):
        """The ``auto`` run id is the pinned clock's date while the host clock has rolled
        past midnight: the drawn items never depend on the day the test runs."""
        from devloop import live_verify
        from scripts import eval_dev_baseline as script

        produced, code, document, builds, _, _ = self.run_session(script.fixture_clock)
        block = document["eval"]
        self.assertEqual(produced, 0)
        self.assertEqual(document["run_id"], "live-2026-10-11")
        self.assertEqual(len(builds), 1)   # the producer's: the run projected nothing
        self.assertEqual(document["product"]["session_projection"], "reused")
        self.assertIn("session_projection", block["phases"])
        # The producer's captures already serve every pin: nothing is topped up.
        self.assertFalse({"mirror_sync", "projection", "pin_sync"} & set(block["phases"]))
        self.assertGreater(document["product"]["objects"], 0)
        self.assertEqual(block["measured"], block["items_total"])
        self.assertGreater(block["items_total"], 0)
        self.assertEqual(len(document["item_rows"]), block["items_total"])
        failures = live_verify.check_expectations(SPEC, code, document)
        # The deterministic baseline misses the accuracy bar on the fixture: red.
        self.assertEqual(set(failures) - {"eval.items < 20"},
                         {"eval.answer.accuracy_rate < 0.8"}, failures)

    def test_items_drawn_after_the_producer_captured_still_pin(self):
        """The producer's mirror completed before the items' time (its clock a day before
        the fixture world's replica time): the run tops up its own copy of the cited
        relations before it opens the generation, which an unchanged source leaves reused,
        so no item drifts for want of a capture, the producer's file is left as it was, and
        every drawn item has an item_rows code row the verifier records as it
        is, with no root identifier in it."""
        from datetime import datetime, timezone

        from devloop import live_verify

        produced, code, document, builds, (before, after), roots = self.run_session(
            lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
        block = document["eval"]
        self.assertEqual((produced, len(builds)), (0, 1))
        self.assertEqual(document["product"]["session_projection"], "reused")
        families = {row["family"] for row in document["items"]}
        self.assertTrue({"F3", "F4"} & families, families)   # not vacuous
        self.assertNotIn("pin_drift:no_capture_at_or_after_time", block["by_reason"])
        self.assertEqual(block["pin_drift"], 0, block["by_reason"])
        self.assertIn("pin_sync", block["phases"])
        self.assertEqual(block["mirror"]["pin_sync"]["complete"],
                         block["mirror"]["pin_sync"]["relations"])
        self.assertGreater(block["mirror"]["pin_sync"]["relations"], 0)
        self.assertEqual(before, after)   # the producer's mirror is never written
        rows = document["item_rows"]
        self.assertEqual(len(rows), block["items_total"])
        for row, item in zip(rows, document["items"]):
            label, root, outcome, reason = row.split(":", 3)
            self.assertRegex(label, r"^F\d+-[1-9]\d*$")
            self.assertEqual((label.split("-")[0], root, outcome),
                             (item["family"], str(item["root"] or "none"), item["outcome"]))
            self.assertEqual(reason, (item["reasons"] or ["none"])[0])
            self.assertFalse([r for r in roots if r and r in row], row)
        self.assertEqual(len(set(rows)), len(rows))   # one row per item, numbers apart
        from scripts.eval_dev_baseline import item_row
        self.assertEqual(item_row({"id": "x-12", "family": "F4"}, {
            "root": "ambiguous", "outcome": "unscored_other",
            "reasons": ["pin_drift:no_capture_at_or_after_time"]}, None),
            "F4-12:ambiguous:unscored_o:pin_drift:no_capture_at_or_after_time")
        recorded = live_verify._cap({"fields": live_verify._recorded(SPEC, document)})
        # Recorded as printed: never scrubbed as an opaque token, never reduced to a length.
        self.assertEqual(live_verify.scrub_tree(recorded["fields"]["item_rows"], []), rows)

        # The Portal down at the top-up: the run still records, the pins drift as they did
        # (counted, excluded from every rate) and the sync's failure is named.
        from integrations.workbench_read_api import SourceHttpError, WorkbenchReadApiClient
        from scripts import eval_dev_baseline as script

        def portal_500(url, headers, **_):
            raise SourceHttpError("read-only source GET failed: HTTPError", status=500)
        _, code, document, _, (before, after), _ = self.run_session(
            lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            client=WorkbenchReadApiClient(script.FIXTURE_URL, script.FIXTURE_KEY,
                                          fetch=portal_500))
        block = document["eval"]
        self.assertEqual(before, after)
        self.assertEqual(block["mirror"]["pin_sync"]["complete"], 0)
        self.assertTrue(block["mirror"]["pin_sync"].get("error_code"), block["mirror"])
        self.assertGreater(block["by_reason"].get("pin_drift:no_capture_at_or_after_time", 0), 0)
        self.assertEqual(block["measured"], block["items_total"])


class CapturedBcClaimGateTests(unittest.TestCase):
    """The product's run host checks a plan's captured BC citations against the capture
    store the product projected (``evals.capture_evidence``), as the scorer re-reads them:
    built over the bare case host, which resolves ``bc://`` citations live with metadata
    only, every captured BC value was ``claim_uncited`` and the answer failed its own
    claim gate. The fixture product with its BC capture (injected transport, fixture world
    of the frozen dev set), one F4 item whose invoice lines are BC records."""

    @classmethod
    def setUpClass(cls) -> None:
        from evals import runner
        from ontology.schema import load_schema
        from scripts import eval_dev_baseline as script
        from scripts import eval_oracle_dev as dev
        tmp = tempfile.TemporaryDirectory(prefix="plx-eval-bc-claims-")
        cls.addClassCleanup(tmp.cleanup)
        cls.item = next(i for i in runner.load_items(script.DEV_DIR, split="dev")
                        if i["id"] == "dev-F4-0001-fixture")
        schema = load_schema()
        relations = sorted(set(script.reachable_relations(
            schema, {cls.item["root"]["relation"]}, script.DEFAULT_LINK_DEPTH))
            | set(script.plan_relations(schema, ["F4"]) or ()))
        capture, environment = script.fixture_bc_capture(dev.eval_world())
        cls.product = script.build_product(script.fixture_client(), Path(tmp.name) / "w",
                                           relations, budget_seconds=None,
                                           clock=script.fixture_clock, bc_capture=capture,
                                           bc_environment=environment)
        cls.sources = dev.build_fixture(dev.eval_world())[0]

    def test_a_captured_bc_citation_passes_the_plan_claim_gate(self):
        from scripts import eval_dev_baseline as script
        from scripts import eval_oracle_dev as dev
        host = self.product.host([script.PRINCIPAL])
        try:
            result = host.call("plx.investigate", {
                "family": "F4", "as_known_at": dev.FIXTURE_NOW,
                "root": {"identifier": self.item["root"]["identifier"],
                         "identifier_kind": self.item["root"]["identifier_kind"]}})
        finally:
            host.close()
        cited = [e for e in result["answer"] if e["kind"] == "property"
                 and e["value"] is not None
                 and e["citation"]["l1_handle"].startswith("bc://")]
        self.assertTrue(cited, result["answer"])   # not vacuous
        for entry in cited:
            # The value checked against its captured row: passed, or unverified only for
            # a quantity role no BC role contract grounds yet; never uncited or failed.
            self.assertIn((entry["check"], entry.get("code")),
                          (("passed", None), ("unverified", "claim_role_unverified")), entry)
        self.assertTrue(result["claim_checks"]["passed"], result["claim_checks"])
        self.assertNotIn("claim_check_failed", result["answerability"]["reasons"])
        # The capture store answers the run's principal only: another reader gets no row.
        handle = cited[0]["citation"]["l1_handle"]
        self.assertIsNotNone(self.product.evidence([script.PRINCIPAL])(handle))
        self.assertIsNone(self.product.evidence(["eval:outsider"])(handle))
        # And the scorer counts those cited BC values right.
        self.sources.portal.prefetch()
        rows, _ = script.run_items([self.item], self.product, self.sources.portal,
                                   evidence_label="isolated-fixture")
        values = {v["name"]: v for v in rows[0]["score"]["values"]}
        invoiced = [n for n in values if n.startswith("invoiced_")]
        self.assertTrue(invoiced)
        self.assertTrue(all(values[n]["passed"] for n in invoiced),
                        {n: values[n] for n in invoiced})


    def test_each_item_plan_gets_its_share_of_the_time_left(self):
        """Under a deadline every item gets a window (its share of the time left over the
        items left) and its plan is asked with what is left of the window before the
        evidence reserve, so one slow plan answers partial instead of leaving the rest of
        the run unmeasured (live: 20 of 30 items, run_budget_exhausted); without a deadline
        the plan runs unbounded by time, as before."""
        from scripts import eval_dev_baseline as script
        self.sources.portal.prefetch()

        def asked(**kwargs):
            rows, incomplete = script.run_items([self.item], self.product, self.sources.portal,
                                                evidence_label="isolated-fixture", **kwargs)
            self.assertEqual(incomplete, [])
            self.assertFalse(rows[0]["over_time_budget"])
            return next(e["arguments"] for e in rows[0]["transcript"]["events"]
                        if e.get("name") == "plx.investigate")
        self.assertNotIn("time_budget_ms", asked())
        window = 100 * script.ITEM_SHARE
        self.assertEqual(asked(deadline=100.0, clock=lambda: 0.0)["time_budget_ms"],
                         int(window * (1 - script.ITEM_EVIDENCE_RESERVE) * 1000))

    def test_pin_and_search_time_count_against_the_item_deadline(self):
        """The item's window opens before its run pin: time the pin and the find take is
        spent from it, and an item whose agent deadline passes before the search stops
        with a recorded time_budget gap, blocked, never answered; a row that ran past its
        window is marked and counted in the report."""
        from scripts import eval_dev_baseline as script
        self.sources.portal.prefetch()
        now = [0.0]

        def clock():
            now[0] += 30.0            # every look at the clock is 30 s of work (pin, find, ...)
            return now[0]
        rows, incomplete = script.run_items([self.item], self.product, self.sources.portal,
                                            evidence_label="isolated-fixture",
                                            deadline=100.0, clock=clock)
        self.assertEqual(incomplete, [])
        row = rows[0]
        self.assertNotIn("plx.investigate", [e.get("name") for e in row["transcript"]["events"]])
        self.assertTrue(row["abstained"])
        self.assertIn("time_budget", json.dumps(row["transcript"]))
        self.assertTrue(row["over_time_budget"])
        result = script.report(rows, [self.item], split="dev", evidence_label="isolated-fixture",
                               run_id="r", product={}, incomplete=incomplete, with_ids=False,
                               sources={})
        self.assertEqual(result["eval"]["items_over_time_budget"], 1)

    def test_the_items_of_a_run_share_one_store_reader(self):
        """Failing first on fb57f04: each item opened its own store reader, so every table
        re-read the store-wide facts (about 25 s an item on the live projection, more than
        the agent's share of a 30-item window: every item stopped before its search). One
        reader is opened per run, kept across its items and closed when the run ends."""
        from scripts import eval_dev_baseline as script
        self.sources.portal.prefetch()
        opened = []
        real = self.product.open_store

        def open_store():
            opened.append(real())
            return opened[-1]
        with mock.patch.object(self.product, "open_store", open_store):
            rows, incomplete = script.run_items([self.item, self.item], self.product,
                                                self.sources.portal,
                                                evidence_label="isolated-fixture")
        self.assertEqual(incomplete, [])
        self.assertEqual(len(opened), 1)
        self.assertEqual(rows[1]["outcome"], rows[0]["outcome"])
        with self.assertRaises(sqlite3.ProgrammingError):   # closed with the run
            opened[0].conn.execute("SELECT 1")

    def test_the_time_share_follows_the_time_and_items_left(self):
        from scripts import eval_dev_baseline as script
        self.assertIsNone(script.item_window_seconds(None, 5))
        share = script.ITEM_SHARE
        self.assertAlmostEqual(script.item_window_seconds(1200.0, 30), 40 * share)
        # an early finisher leaves its time to the rest
        self.assertGreater(script.item_window_seconds(1190.0, 29),
                           script.item_window_seconds(1200.0, 30))
        # past the deadline or with no time left: the floor, never zero or negative
        for left in (0.0, -50.0, 0.001):
            self.assertEqual(script.item_window_seconds(left, 3),
                             script.ITEM_WINDOW_FLOOR_SECONDS)
        self.assertEqual(script.item_window_seconds(1e9, 1), 600.0)   # the contract's cap
        self.assertEqual(script.item_window_seconds(10.0, 0),
                         script.item_window_seconds(10.0, 1))


class MultiLineOrderTotalTests(unittest.TestCase):
    """Failing first on fb57f04: an F3 order whose manufacturing order has FileMaker status
    entries answered produced as the gold assembly order's final quantity, where the item
    asks the Portal's qty manufactured (the sum of the FM status entries' qtymanuf of its
    one FM assembly order when any is valued, else yield_actual): the answer failed
    ``cited_other_row``. The plan reads the status entries and states produced as one sum
    over the cited entries the scorer recomputes (fixture world of the frozen dev set)."""

    @classmethod
    def setUpClass(cls):
        from evals import runner
        from ontology.schema import load_schema
        from scripts import eval_dev_baseline as script
        from scripts import eval_oracle_dev as dev
        cls.ids = ("dev-F3-0003-fixture", "dev-F3-0006-fixture")
        items = {i["id"]: i for i in runner.load_items(script.DEV_DIR, split="dev")}
        cls.tmp = tempfile.TemporaryDirectory(prefix="plx-eval-f3-total-")
        relations = sorted(set(script.reachable_relations(
            load_schema(), {"public.app_sales_order"}, script.DEFAULT_LINK_DEPTH))
            | set(script.plan_relations(load_schema(), ["F3"]) or ()))
        product = script.build_product(script.fixture_client(), Path(cls.tmp.name), relations,
                                       budget_seconds=None, clock=script.fixture_clock)
        sources = dev.build_fixture(dev.eval_world())[0]
        sources.portal.prefetch()
        rows, _ = script.run_items([items[i] for i in cls.ids], product, sources.portal,
                                   evidence_label="isolated-fixture")
        cls.rows = {row["item_id"]: row for row in rows}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_produced_is_the_status_entries_sum_of_the_manufacturing_order(self):
        for item_id in self.ids:
            row = self.rows[item_id]
            values = {v["name"]: v for v in row["score"]["values"]}
            self.assertIn("produced", values, (item_id, row["score"]))
            self.assertTrue(values["produced"]["passed"], (item_id, values["produced"]))
            self.assertEqual(row["outcome"], "pass", (item_id, row["reasons"]))


class SageLoadedOrderRootTests(unittest.TestCase):
    """F3 items over the frozen fixture world where one order is Sage-loaded (its Sage order
    header holds the same number, joined by sageSourceOrdUniq as the Sage ETL writes it) and
    another order's number is also on a Sage header the ETL renamed on the clash (its gold
    copy is another order): the first resolves to its gold row and passes, the second stays
    ambiguous with no winner."""

    @classmethod
    def setUpClass(cls):
        from evals import runner
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from ontology.schema import load_schema
        from scripts import eval_dev_baseline as script
        from scripts import eval_oracle_dev as dev
        from scripts import eval_unfamiliar_sample as ev

        items = {i["id"]: i for i in runner.load_items(script.DEV_DIR, split="dev")
                 if i["family"] == "F3"}
        cls.loaded, cls.clash = "dev-F3-0002-fixture", "dev-F3-0001-fixture"
        world = dev.eval_world()
        orders = {r["id"]: r for r in world.relations["public.app_sales_order"]}
        headers = world.relations["sage.oe_order_header"]
        loaded = orders[items[cls.loaded]["root"]["key"]["id"]]
        loaded["custom_fields"] = {"sageOrdUniq": "97001", "sageSourceOrdUniq": "97001",
                                   "sageOrdNumber": loaded["order_number"],
                                   "source": "sage.oe_order_header"}
        headers.append({"orduniq": "97001", "ordnumber": loaded["order_number"],
                        "ponumber": loaded["po_reference"], "audtdate": "20981231"})
        number = orders[items[cls.clash]["root"]["key"]["id"]]["order_number"]
        headers.append({"orduniq": "97002", "ordnumber": number, "ponumber": "FXCPO-997",
                        "audtdate": "20981231"})
        world.relations["public.app_sales_order"].append({
            "id": "fx-so-sage-97002", "order_number": "DSO-" + number,
            "po_reference": "FXCPO-997", "status": "READY_TO_SHIP",
            "customer_id": loaded["customer_id"], "required_date": None,
            "custom_fields": {"sageOrdUniq": "97002", "sageSourceOrdUniq": "97002",
                              "sageOrdNumber": number, "source": "sage.oe_order_header"},
            "updated_at": loaded["updated_at"]})
        client = WorkbenchReadApiClient(script.FIXTURE_URL, script.FIXTURE_KEY,
                                        fetch=ev._fixture_fetch(world))
        cls.tmp = tempfile.TemporaryDirectory()
        relations = script.reachable_relations(load_schema(), {"public.app_sales_order"},
                                               script.DEFAULT_LINK_DEPTH)
        product = script.build_product(client, Path(cls.tmp.name), relations,
                                       budget_seconds=None, clock=script.fixture_clock)
        sources, _, _, _ = dev.build_fixture(world)
        sources.portal.prefetch()
        rows, _ = script.run_items([items[cls.loaded], items[cls.clash]], product,
                                   sources.portal, evidence_label="isolated-fixture")
        cls.rows = {row["item_id"]: row for row in rows}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def investigation(row):
        return next(e["result"] for e in row["transcript"]["events"]
                    if e.get("name") == "plx.investigate")

    def test_a_sage_loaded_order_resolves_to_its_gold_row_and_answers(self):
        row = self.rows[self.loaded]
        # plx.find lists both records; the plan settles on the gold order.
        self.assertEqual((row["root"], row["plan_root"]), ("ambiguous", "found"))
        self.assertEqual((row["outcome"], row["abstained"], row["reasons"]),
                         ("pass", False, []))
        result = self.investigation(row)
        self.assertTrue(result["root"]["object_id"].startswith(
            "SalesOrder:portal.app_sales_order:"))
        twins = [g for g in result["gaps"] if g["reason"] == "root_twins_not_roots"]
        self.assertEqual(len(twins), 1, result["gaps"])
        self.assertIn("verified same-record link", twins[0]["detail"])
        self.assertIn("api://sage.oe_order_header/", twins[0]["detail"])

    def test_a_sage_header_renamed_on_a_clash_leaves_the_root_ambiguous(self):
        row = self.rows[self.clash]
        self.assertEqual(row["plan_root"], "ambiguous")
        self.assertTrue(row["abstained"])
        result = self.investigation(row)
        self.assertEqual(result["answerability"],
                         {"state": "blocked", "reasons": ["ambiguous_root"]})
        self.assertNotIn("root", result)
        self.assertEqual(sorted(c["object"]["object_id"].split(":")[1]
                                for c in result["candidates"]),
                         ["portal.app_sales_order", "portal.sage_oe_order_header"])


class CitedRelationCoverageTests(unittest.TestCase):
    """The spec's argv at link depth 0 (injected transports and clock): items citing
    relations past the link neighbourhood still pin, because the sync captures every
    relation the drawn items cite; the run projects none of those it mirrors for the pins
    alone (the answer key never chooses what the product can search) and names them."""

    def test_a_cited_relation_past_the_link_depth_is_pinned_but_never_projected(self):
        import sqlite3

        from evals import runner
        from ontology.schema import load_schema
        from scripts import eval_dev_baseline as script

        schema = load_schema()
        items = {i["id"]: i for i in runner.load_items(script.DEV_DIR, split="dev")}
        reach = script.reachable_relations(schema, {i["root"]["relation"]
                                                    for i in items.values()}, 0)
        past = {iid: set(script.cited_relations([i])) - set(reach) for iid, i in items.items()}
        cited_only = set().union(*past.values())
        self.assertIn("public.app_shipment", cited_only)   # not vacuous
        bindings = {b["id"] for t in schema.object_types.values() for b in t.get("bindings", [])
                    if b.get("relation") in cited_only}
        held = {b["id"] for t in schema.object_types.values() for b in t.get("bindings", [])
                if b.get("relation") in reach}
        with tempfile.TemporaryDirectory() as tmp:
            result = script.run_fixture(Path(tmp), link_depth=0)
            with contextlib.closing(sqlite3.connect(Path(tmp) / "ontology.sqlite")) as db:
                projected = {row[0] for row in db.execute(
                    "SELECT DISTINCT binding_id FROM objects")}
        self.assertTrue(projected & held)
        self.assertFalse(projected & bindings, sorted(projected & bindings))
        pins = {row["item_id"]: row["pin"] for row in result["items"]}
        self.assertTrue(all(pins[iid] == "pinned" for iid, rels in past.items() if rels), pins)

    def test_items_citing_relations_past_the_link_depth_pin_without_drift(self):
        from devloop import live_verify
        from ontology.schema import load_schema
        from scripts import eval_dev_baseline as script
        from scripts import eval_oracle_dev as dev

        def build(env, args, *, guard):
            sources, documents, configured, _ = dev.build_fixture(dev.eval_world())
            return sources, documents, configured
        spec = json.loads((ROOT / "devloop" / "live-checks" / "eval.dev_baseline.json")
                          .read_text())
        # Every relation the sample cites is one an F1-F4 plan reads (F3 states produced
        # from the FM status entries): the cited-only case drops that relation from the plans.
        real, unplanned = script.plan_relations, "public.assembly_order_status"

        def plan_relations(schema, families):
            read = real(schema, families)
            return None if read is None else [r for r in read if r != unplanned]
        for run_id in ("live-2026-10-09", "live-2026-10-11"):
            out = io.StringIO()
            with self.subTest(run_id=run_id), tempfile.TemporaryDirectory() as tmp:
                argv = [arg.replace("{tmp}", tmp).replace("{session_state}", tmp + "/session")
                        for arg in spec["command"][3:]]
                argv = argv[:argv.index("--count")] + ["--count", "12", "--min-items", "4"] + \
                    argv[argv.index("--total-budget-seconds"):] + ["--state-dir", tmp + "/state"]
                argv[argv.index("--run-id") + 1] = run_id
                argv[argv.index("--link-depth") + 1] = "0"
                with contextlib.redirect_stdout(out), \
                        mock.patch.object(script, "plan_relations", plan_relations):
                    script.main(argv, environ={}, live_build=build,
                                live_client=script.fixture_client(),
                                live_clock=script.fixture_clock)
                document = json.loads(out.getvalue())
                block = document["eval"]
                # Not vacuous: the sample cites relations the depth-0 set leaves out.
                sources, documents, configured, _ = dev.build_fixture(dev.eval_world())
                _, items = dev.execute(sources, documents, run_id=run_id, now="",
                                       count=12, audit=0, id_suffix="live",
                                       evidence_note=None, evidence_label="live-read",
                                       configured=configured, exclude_keys=set())
                outside = set(script.cited_relations(items)) - set(script.reachable_relations(
                    load_schema(), {i["root"]["relation"] for i in items}, 0))
                self.assertTrue(outside)
                plans = plan_relations(load_schema(), {i["family"] for i in items})
                self.assertTrue(outside - set(plans or ()))
                self.assertEqual(block["mirror"]["cited_only"],
                                 sorted(outside - set(plans or ())))
                self.assertEqual(live_verify._recorded(spec, document)["eval"]["mirror"],
                                 block["mirror"])   # the verifier records the split
                self.assertEqual(block["items"], len(items))
                self.assertEqual(block["pin_drift"], 0, block["by_reason"])
                self.assertFalse([r for r in block["by_reason"] if r.startswith("pin_drift")])
                self.assertEqual(block["mirror"]["needed_unfinished"], [])
                self.assertTrue(block["complete"], block["incomplete"])


class HeldOutProductTests(unittest.TestCase):
    """The held-out split of the one scorer (``heldout``): a frozen held-out set in a
    temporary held-out directory, scored by family and by variant, with aggregates only,
    and refused whole when it is not frozen or overlaps the dev set. The spec's own argv
    runs over a live-shaped copy of the fixture set with the fixture world's transport and
    clock injected (nothing live; evidence injected-transport)."""

    @classmethod
    def setUpClass(cls):
        from scripts import eval_oracle_dev as dev
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.state = cls.root / "state"
        with contextlib.redirect_stdout(io.StringIO()):
            code = dev.main(["heldout", "--source", "fixture", "--count", "14",
                             "--state-dir", str(cls.state)])
        assert code == 0
        cls.fixture_set = cls.state / "fixture" / dev.HELDOUT_RUN_ID
        cls.items = [json.loads(p.read_text()) for p in sorted(
            (cls.fixture_set / "items").glob("ho-*.json"))]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def env(self, **values):
        from evals import runner
        base = {"PLX_WORKBENCH_API_KEY": "placeholder-not-a-key",
                runner.HELD_OUT_DIR_ENV: str(self.root)}
        return mock.patch.dict("os.environ", {**base, **values})

    def run_main(self, argv, **kwargs):
        from scripts import eval_dev_baseline as script
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = script.main(argv, environ={}, **kwargs)
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None)

    def live_shaped(self, name):
        """A copy of the fixture set frozen as the live source (the spec's ``--source
        live``): same items and digests, the frozen record's source names live."""
        import shutil
        state = self.root / name
        shutil.copytree(self.fixture_set, state / "live" / self.fixture_set.name)
        frozen = state / "live" / self.fixture_set.name / "frozen.json"
        record = json.loads(frozen.read_text())
        record.update(source="live", id_suffix="live", evidence_label="live-read")
        frozen.write_text(json.dumps(record))
        return state

    def test_the_spec_scores_the_frozen_set_by_family_and_variant_without_a_value(self):
        from devloop import live_verify
        from evals.oracle.perturb import TRANSFORMS
        from scripts import eval_dev_baseline as script
        from scripts import eval_status

        spec = json.loads((ROOT / "devloop" / "live-checks" / "eval.heldout_product.json")
                          .read_text())
        state = self.live_shaped("spec")
        argv = [arg.replace("{tmp}", str(self.root / "spec-tmp"))
                .replace("{session_state}", str(self.root / "spec-session"))
                for arg in spec["command"][3:]] + ["--state-dir", str(state)]
        with self.env():
            code, document = self.run_main(argv, live_client=script.fixture_client(),
                                           live_clock=script.fixture_clock)
        failures = live_verify.check_expectations(spec, code, document)
        # Every other expectation holds; the deterministic product misses B1's bar.
        self.assertEqual(failures, ["eval.answer.accuracy_rate < 0.8",
                                    "eval.answer.coverage_rate < 0.8"])
        block, counts = document["eval"], document["counts"]
        # Every frozen item is admitted or refused with a reason, and every admitted item
        # is measured once: none lost or duplicated across the standard and variant runs.
        self.assertEqual(counts["admitted"] + counts["refused"], len(self.items))
        self.assertEqual(counts["measured"], counts["admitted"])
        self.assertEqual(sum(v["items"] for v in block["by_variant"].values()), block["items"])
        self.assertEqual(block["by_variant"]["standard"]["items"],
                         sum(i.get("perturbation") is None for i in self.items))
        self.assertEqual(set(block["by_family"]), {i["family"] for i in self.items})
        variants = set(block["by_variant"]) - {"standard"}
        self.assertTrue(variants and variants <= set(TRANSFORMS), variants)
        self.assertEqual(counts["variants_measured"],
                         sum(block["by_variant"][v]["items"] for v in variants))
        self.assertEqual(document["admission_refused"]["reasons"],
                         {"perturbed_reads_not_served": counts["refused"]})
        built = document["variant_products"]
        self.assertEqual(sum(p["built"] for p in built.values()),
                         sum(p["groups"] for p in built.values()))
        self.assertNotIn("dev_set", block)
        self.assertTrue(eval_status.status({"eval": {spec["id"]: {**block, "result": "pass"}}})
                        ["line"].startswith(f"{spec['id']}: held_out {block['held_out']} ("))
        # No item id, question, identifier, key or label value anywhere in the record.
        text = json.dumps(document)
        secrets = set()
        for item in self.items:
            secrets |= {item["id"], item["question"]["text"], item["root"]["identifier"],
                        *map(str, item["root"]["key"].values())}
            for entry in item["known_outcome"].get("chain") or []:
                secrets |= set(map(str, (entry.get("key") or {}).values()))
            for value in item["known_outcome"]["values"]:
                if isinstance(value["value"], str) and len(value["value"]) >= 4:
                    secrets.add(value["value"])
        self.assertEqual(sorted(s for s in secrets if s in text), [])
        self.assertNotIn("items", document)

    def test_an_unfrozen_or_dev_overlapping_set_is_refused_unscored(self):
        import shutil

        from evals import runner
        from evals.oracle.source import digest
        from evals.partition import key_digests
        from scripts import eval_oracle_dev as dev

        def refused(state, **env):
            with self.env(**env), tempfile.TemporaryDirectory() as work:
                code, document = self.run_main(
                    ["heldout", "--source", "fixture", "--state-dir", str(state),
                     "--work-dir", work, "--min-items", "1"])
                self.assertFalse(list(Path(work).iterdir()))   # no product was built
            self.assertEqual(code, 3)
            self.assertEqual(set(document), {"schema", "split", "run_id", "values_published",
                                             "evidence_label", "adapter_status", "refused"})
            return document["refused"]

        def copy(name):
            state = self.root / name
            shutil.copytree(self.fixture_set, state / "fixture" / self.fixture_set.name)
            return state, state / "fixture" / self.fixture_set.name

        def refreeze(set_dir):
            frozen = json.loads((set_dir / "frozen.json").read_text())
            hashes = sorted(dev.content_hash(json.loads(p.read_text()))
                            for p in (set_dir / "items").glob("ho-*.json"))
            frozen.update(item_sha256=hashes, set_sha256=digest(hashes))
            (set_dir / "frozen.json").write_text(json.dumps(frozen))
        with self.subTest("not frozen"):
            state, set_dir = copy("unfrozen")
            (set_dir / "frozen.json").unlink()
            self.assertEqual(refused(state), "heldout_set_not_frozen")
        with self.subTest("an item changed after the freeze"):
            state, set_dir = copy("tampered")
            path = sorted((set_dir / "items").glob("ho-*.json"))[0]
            item = json.loads(path.read_text())
            item["question"]["text"] += " "
            path.write_text(json.dumps(item))
            self.assertEqual(refused(state), "heldout_frozen_set_does_not_match_its_digests")
        with self.subTest("an item shares its root with a committed dev item"):
            state, set_dir = copy("overlap")
            dev_item = next(i for i in runner.load_items(runner.VISIBLE_ITEM_DIRS[0],
                                                         split="dev", admit=False))
            path = next(p for p in sorted((set_dir / "items").glob("ho-*.json"))
                        if json.loads(p.read_text())["family"] == dev_item["family"])
            item = json.loads(path.read_text())
            item["root"] = dict(dev_item["root"])
            path.write_text(json.dumps(item))
            refreeze(set_dir)
            self.assertTrue(refused(state).startswith("held_out_overlaps_visible:"))
        with self.subTest("a frozen item fails the schema: the code, never its field"):
            state, set_dir = copy("invalid")
            path = sorted((set_dir / "items").glob("ho-*.json"))[0]
            item = json.loads(path.read_text())
            identifier = item["root"]["identifier"]
            item["root"]["key"][identifier] = ["not", "a", "key", "value"]
            path.write_text(json.dumps(item))
            refreeze(set_dir)
            with self.env(), tempfile.TemporaryDirectory() as work:
                out = io.StringIO()
                with contextlib.redirect_stdout(out), \
                        contextlib.redirect_stderr(io.StringIO()):
                    from scripts import eval_dev_baseline as script
                    code = script.main(["heldout", "--source", "fixture", "--state-dir",
                                        str(state), "--work-dir", work, "--min-items", "1"],
                                       environ={})
            self.assertEqual((code, json.loads(out.getvalue())["refused"]), (3, "item_invalid"))
            self.assertNotIn(identifier, out.getvalue())
        with self.subTest("an item shares a key with a live dev run"):
            state, _ = copy("live-dev")
            path = dev.live_dev_dir(state) / "live-2026-10-12.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"format": dev.LIVE_DEV_FORMAT, "root_sha256": [],
                                        "chain_records_sha256": [],
                                        "key_sha256": sorted(key_digests(self.items[0]))}))
            self.assertTrue(refused(state).startswith("heldout_overlaps_live_dev:"))
        with self.subTest("outside the credentialed environment"):
            self.assertEqual(refused(self.state, PLX_WORKBENCH_API_KEY=""),
                             "held_out_requires_credentialed_environment")

    def test_the_headline_counts_standard_items_only(self):
        """Every variant passes and every standard item fails: ``held_out`` and
        ``by_family`` read 0 passes over the standard items, the variants' passes stand
        only in ``by_variant``."""
        from scripts import eval_dev_baseline as script
        real = script.bl.run_item

        def forced(item, *args, **kwargs):
            row = real(item, *args, **kwargs)
            row["outcome"] = "fail" if row["variant"] == "standard" else "pass"
            return row
        with self.env(), tempfile.TemporaryDirectory() as work, \
                mock.patch.object(script.bl, "run_item", forced):
            code, document = self.run_main(["heldout", "--source", "fixture", "--state-dir",
                                            str(self.state), "--work-dir", work,
                                            "--min-items", "1"])
        block = document["eval"]
        standard = block["by_variant"]["standard"]["items"]
        variants = {v: e for v, e in block["by_variant"].items() if v != "standard"}
        self.assertTrue(standard and variants, block["by_variant"])
        self.assertTrue(all(e["pass"] == f"{e['items']}/{e['items']}"
                            for e in variants.values()), variants)
        self.assertEqual((block["held_out"], block["attempted"]),
                         (f"0/{standard}", f"0/{standard}"))
        self.assertEqual(block["headline_items"], standard)
        self.assertEqual(sum(int(e["pass"].split("/")[1]) for e in block["by_family"].values()),
                         standard)
        self.assertTrue(all(e["pass"].startswith("0/") for e in block["by_family"].values()))
        self.assertTrue(document["summary_line"].startswith(f"held_out 0/{standard} ("))
        self.assertEqual(code, 0)   # rates are measured, never the verdict

    def test_standard_items_leave_the_variants_their_share_of_the_budget(self):
        """Standard items slow enough to spend the whole budget stop at the variants' share
        (their fraction of the admitted items): every transform's groups are then built and
        measured, and the record publishes the variant build's own phase."""
        from scripts import eval_dev_baseline as script
        real, now = script.bl.run_item, [0.0]

        def slow(item, *args, **kwargs):
            row = real(item, *args, **kwargs)
            if item.get("perturbation") is None:
                now[0] += 450.0   # four of these spend the whole 1800 s budget
            return row
        with self.env(), tempfile.TemporaryDirectory() as work, \
                mock.patch.object(script.bl, "run_item", slow):
            code, document = self.run_main(
                ["heldout", "--source", "fixture", "--state-dir", str(self.state),
                 "--work-dir", work, "--min-items", "1", "--total-budget-seconds", "1800",
                 "--run-reserve-seconds", "600"],
                monotonic=lambda: now[0], hard_exit=lambda code: None)
        counts, products = document["counts"], document["variant_products"]
        self.assertGreater(counts["standard"], 4)        # alone they would spend it all
        self.assertLess(counts["measured"] - counts["variants_measured"], counts["standard"])
        self.assertGreaterEqual(counts["variants_measured"], 1)
        self.assertEqual(counts["variants_measured"], counts["variants"])
        self.assertTrue(products)
        for transform, built in products.items():
            self.assertEqual((built["built"], built["not_reached"]), (built["groups"], 0),
                             transform)
            self.assertEqual(built["over_share"], 0, transform)
        self.assertIn("variant_product", document["eval"]["phases"])
        self.assertEqual(code, 3)   # standard items left unmeasured: incomplete, never a pass

    def test_a_variant_group_is_a_delta_over_the_base_build_that_scores_as_a_full_one(self):
        """Each group's product copies the base mirror, replays only the relations its
        perturbation changes and is built over the base generation: far fewer rows than
        the base, the same item outcomes as a product replayed and projected whole (the
        build it replaces). The projection takes the mode that fits
        (``ontology.project_incremental``, "Mode"): only its changed units when they are
        at most ``COLD_SHARE`` of the base and the incremental estimate is not above the
        cold one (a seed copied, not cloned, costs the base store), else cold; shifted
        dates move every relation's dates, so most units change and it is cold."""
        from datetime import datetime

        from evals.oracle import perturb
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from ontology.project_incremental import COLD_SHARE
        from ontology.schema import load_schema
        from scripts import eval_dev_baseline as script

        plan = script.relation_plan(load_schema(), self.items, 2)

        def outcomes(group, product):
            return [(r["outcome"], r["reasons"]) for r in script.run_items(
                group, product, None, evidence_label="isolated-fixture")[0]]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            base = script.build_product(script.fixture_client(), work / "base",
                                        plan["relations"], budget_seconds=None,
                                        clock=script.fixture_clock, first=plan["first"],
                                        project=plan["projected"], incremental=True)
            whole = script.MirrorReplay(base.mirror_path)
            client = WorkbenchReadApiClient(script.REPLAY_URL, script.FIXTURE_KEY,
                                            fetch=whole.fetch)
            catalog = client.relations()
            base_rows = sum(len(client.read_relation(n, columns=catalog[n]["columns"])
                                ["rows"]) for n in whole.relations)
            whole.close()
            groups = script.variant_groups(self.items, script.variant_cost(base, 2))
            self.assertEqual({p["transform"] for p, _ in groups}, set(perturb.TRANSFORMS))
            for n, (perturbation, group) in enumerate(groups):
                transform, params = perturbation["transform"], perturbation["params"]
                with self.subTest(transform=transform, params=params):
                    variant = script.variant_product(
                        base, work / f"variant-{n}", group, perturbation, depth=2,
                        clock=script.fixture_clock, run_left=lambda: 3600.0)
                    info, own = variant.info, script.relation_plan(load_schema(), group, 2)
                    touched = perturb.touched_relations(transform, params, [
                        r for r in own["relations"] if r in catalog])
                    replay = script.MirrorReplay(base.mirror_path, perturbation)
                    client = WorkbenchReadApiClient(script.REPLAY_URL, script.FIXTURE_KEY,
                                                    fetch=replay.fetch)
                    served = sum(len(client.read_relation(r, columns=catalog[r]["columns"])
                                     ["rows"]) for r in touched
                                 if transform != "partial_source_failure")
                    self.assertEqual((info["relations_replayed"], info["rows_replayed"]),
                                     (len(touched), served))   # the perturbed rows only
                    # Built over the base generation: its changed units have an
                    # incremental estimate; no space guard here, so the preferred mode runs.
                    need = variant.generation.need
                    partial, cold = need["estimates"]["incremental"], need["estimates"]["cold"]
                    self.assertEqual(info["projection"], need["preferred"])
                    if transform == "shifted_dates":   # it moves every relation's dates
                        self.assertGreater(partial["changed_share"], COLD_SHARE)
                        self.assertEqual(info["projection"], "cold")
                    else:
                        self.assertLess(info["rows_replayed"], base_rows)
                        self.assertLessEqual(partial["changed_share"], COLD_SHARE)
                    if info["projection"] == "incremental":
                        self.assertLess(info["units_rebuilt"], info["units"])
                        if transform != "shifted_dates":
                            self.assertLess(info["units_rebuilt"] * 4, info["units"])
                    else:   # cold only by the mode rule; every unit projected again
                        self.assertEqual(info["projection"], "cold")
                        self.assertTrue(partial["changed_share"] > COLD_SHARE
                                        or (cold["needed_bytes"] is not None and
                                            partial["needed_bytes"] > cold["needed_bytes"]))
                        self.assertEqual(info["units_rebuilt"], info["units"])
                    moved = lambda: datetime.fromisoformat(perturb.replica_time(  # noqa: E731
                        script.fixture_clock().isoformat(), transform, params))
                    full = script.build_product(
                        client, work / f"full-{n}", own["relations"], budget_seconds=None,
                        clock=moved, first=own["first"], project=own["projected"])
                    replay.close()
                    self.assertEqual(outcomes(group, variant), outcomes(group, full))

    def test_a_touched_relation_the_variant_sync_loses_is_counted_and_the_run_incomplete(self):
        """A touched relation whose replayed sync fails (here every scaled_quantity read) is
        left empty in the variant's copy: counted sync_failed in variant_products and the
        run incomplete, never silent; the source partial_source_failure breaks on purpose
        is not one."""
        from scripts import eval_dev_baseline as script
        real, fetch = script.variant_product, script.MirrorReplay.fetch

        def down(replay, url, *args, **kwargs):
            if "/rows" in url:
                raise RuntimeError("replay_route_down")
            return fetch(replay, url, *args, **kwargs)

        def broken(base, work, group, perturbation, **kwargs):
            if perturbation["transform"] != "scaled_quantity":
                return real(base, work, group, perturbation, **kwargs)
            with mock.patch.object(script.MirrorReplay, "fetch", down):
                return real(base, work, group, perturbation, **kwargs)
        with self.env(), tempfile.TemporaryDirectory() as work, \
                mock.patch.object(script, "variant_product", broken):
            code, document = self.run_main(
                ["heldout", "--source", "fixture", "--state-dir", str(self.state),
                 "--work-dir", work, "--min-items", "1"])
        products = document["variant_products"]
        self.assertGreaterEqual(products["scaled_quantity"]["sync_failed"], 1)
        self.assertEqual({name: p["sync_failed"] for name, p in products.items()
                          if name != "scaled_quantity"},
                         dict.fromkeys(set(products) - {"scaled_quantity"}, 0))
        self.assertIn("partial_source_failure", products)
        self.assertFalse(document["eval"]["complete"])
        self.assertIn("variant_sync_failed", document["eval"]["incomplete"])
        self.assertEqual(code, 3)

    def test_a_variant_group_the_deadline_cuts_is_not_reached_and_the_run_incomplete(self):
        """The first group fits and is measured; the deadline passes while the second
        group projects: it and every later group are counted not_reached (never failed or
        assumed), the run stops on the measured subset and eval.complete is false."""
        from scripts import eval_dev_baseline as script
        real, now, builds = script.variant_product, [0.0], []

        def slow_second(*args, **kwargs):
            builds.append(1)
            if len(builds) == 2:
                now[0] = 1800.0 - 600.0   # the reserve: the run's deadline has 600 s left
                fetch = script.MirrorReplay.fetch

                def late(replay, *a, **k):   # each replayed page costs 120 s
                    now[0] += 120.0
                    return fetch(replay, *a, **k)
                with mock.patch.object(script.MirrorReplay, "fetch", late):
                    return real(*args, **kwargs)
            return real(*args, **kwargs)
        with self.env(), tempfile.TemporaryDirectory() as work, \
                mock.patch.object(script, "variant_product", slow_second):
            code, document = self.run_main(
                ["heldout", "--source", "fixture", "--state-dir", str(self.state),
                 "--work-dir", work, "--min-items", "1", "--total-budget-seconds", "1800",
                 "--run-reserve-seconds", "600"],
                monotonic=lambda: now[0], hard_exit=lambda code: None,
                # The fake clock runs past the watchdog's hard stop (since + 1800 + grace):
                # the real watchdog thread polls it every 0.5 s of wall time and would race
                # the run to print its own record. This test is the budget cut, not the
                # watchdog (its own tests): no hard stop inside the fake clock's range.
                hard_stop_grace=1e9)
        counts, products = document["counts"], document["variant_products"]
        groups = sum(p["groups"] for p in products.values())
        self.assertEqual(len(builds), 2)   # no group is started past the deadline
        self.assertGreaterEqual(groups, 2)
        self.assertEqual((sum(p["built"] for p in products.values()),
                          sum(p["not_reached"] for p in products.values()),
                          sum(p["failed"] for p in products.values())), (1, groups - 1, 0))
        self.assertGreaterEqual(counts["variants_measured"], 1)
        self.assertLess(counts["variants_measured"], counts["variants"])
        self.assertEqual(counts["measured"] - counts["variants_measured"], counts["standard"])
        self.assertFalse(document["eval"]["complete"])
        self.assertIn(script.BUDGET_EXHAUSTED, document["eval"]["incomplete"])
        self.assertEqual(code, 3)

    def test_a_variant_product_reads_what_the_oracle_read_perturbed(self):
        """The replayed mirror serves the product the oracle's own reads of the fixture
        world, and each transform's rows exactly as the oracle's perturbed port serves
        them: a variant's product and its label rest on one perturbed source."""
        from evals.oracle import perturb
        from integrations.workbench_read_api import SourceUnavailable, WorkbenchReadApiClient
        from scripts import eval_dev_baseline as script
        from scripts import eval_oracle_dev as dev

        relations = ["public.app_receipt_line", "public.app_sales_order", "public.app_supplier"]
        world = dev.eval_world()

        def rows(read):
            return sorted(json.dumps(r, sort_keys=True) for r in read["rows"])
        with tempfile.TemporaryDirectory() as tmp:
            base = script.build_product(script.fixture_client(), Path(tmp), relations,
                                        budget_seconds=None, clock=script.fixture_clock)
            catalog = script.fixture_client().relations()
            for unknown in ({"transform": "renamed_columns", "params": {}},
                            {"transform": "shifted_dates", "params": {"days": 0}}):
                with self.subTest(unknown=unknown), self.assertRaises(ValueError):
                    script.MirrorReplay(base.mirror_path, unknown)   # fails closed
            for transform in (None, *perturb.TRANSFORMS):
                params = dict(perturb.DEFAULTS.get(transform) or {})
                if transform == "partial_source_failure":
                    params["source"] = "public.app_supplier"
                perturbation = None if transform is None else {"transform": transform,
                                                               "params": params}
                replay = script.MirrorReplay(base.mirror_path, perturbation)
                client = WorkbenchReadApiClient(script.REPLAY_URL, script.FIXTURE_KEY,
                                                fetch=replay.fetch)
                oracle = world.read_relation if transform is None else \
                    perturb.portal_port(world.read_relation, transform, params)
                try:
                    for name in relations:
                        columns = catalog[name]["columns"]
                        with self.subTest(transform=transform, relation=name):
                            if transform == "partial_source_failure" and \
                                    name == params["source"]:
                                with self.assertRaises(SourceUnavailable):
                                    client.read_relation(name, columns=columns)
                                continue
                            self.assertEqual(rows(client.read_relation(name, columns=columns)),
                                             rows(oracle(name, columns)))
                    # every relation the transform leaves alone is served as read: a
                    # variant's delta build replays only touched_relations
                    if transform is not None:
                        touched = perturb.touched_relations(transform, params, list(catalog))
                        for name in sorted(set(catalog) - set(touched)):
                            columns = catalog[name]["columns"]
                            self.assertEqual(rows(oracle(name, columns)),
                                             rows(world.read_relation(name, columns)), name)
                    self.assertEqual(client.health()["generatedAt"],
                                     perturb.replica_time(world.generated_at, transform,
                                                          params) if transform else
                                     world.generated_at)
                finally:
                    replay.close()


if __name__ == "__main__":
    unittest.main()
