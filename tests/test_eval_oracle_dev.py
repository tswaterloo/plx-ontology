"""Oracle dev-set wiring: frozen fixture items, live ports over the real GET-only readers.

Evidence label: ``injected-transport``. The Portal path runs through the real
``WorkbenchReadApiClient`` with the fixture world's fetch; the BC path through the real
``BusinessCentralReader`` with the recorded-shape ``FakeSender``. Nothing here is live;
``verify/live-checks/eval.oracle_dev.json`` is live-unverified until the verifier
records a pass.
"""

from __future__ import annotations

from collections import Counter
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

from verify import live_verify
from evals.oracle import BcReads, PortalReads, Sources, perturb, run
from evals.oracle.fixture_world import World, build_live_shaped_world
from evals.oracle.keys import CLOSED_CITE, RELATIONS
from evals.oracle.source import ReadFailed, digest
from evals.oracle.generator import (HELD_OUT_ONLY, QUOTAS, SPLIT_SALT, STRATA,
                                    audit_requested, chain_digests, item_hash,
                                    item_root_digest, split_of)
from integrations.tests.test_bc_purchase import BUYER, PO, Fixture
from ontology.json_contract import validate
from ontology.types import Actor
from scripts import eval_oracle_dev as dev

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "evals" / "dev"
SCHEMA = json.loads((ROOT / "evals" / "item.schema.json").read_text(encoding="utf-8"))
# The fixture world's purchase-order numbers (fixture_world.po_number, fm_po_number and the
# operations-route form), the BC-renumbered staging row's number and the purchase-order
# row ids: none may reach stdout. Fixture-only markers ("PO92" is a live PO series too).
PO_MARKERS = ("PO92", "PO#9", "PO-9FX", "FXBC", "fx-po-")


class MailFake:
    """GraphMailReader's directory_cache on a mail reader fake (no lookup to share)."""
    @contextlib.contextmanager
    def directory_cache(self):
        yield


def committed_items() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(DEV.glob("dev-F*.json"))]


class FrozenDevItemsTest(unittest.TestCase):
    def test_thirty_schema_valid_fixture_items(self):
        items = committed_items()
        self.assertEqual(len(items), 30)
        for item in items:
            self.assertEqual(validate(item, SCHEMA, root=SCHEMA), [], item["id"])
            self.assertTrue(item["id"].endswith("-fixture"))
            self.assertIn("not live", item["label"]["note"])
            self.assertEqual(item["label"]["status"], "oracle_only")
            self.assertEqual(item["split"], "dev")
            # a gap's concerns names one of the oracle's own values (never product output)
            names = {value["name"] for value in item["known_outcome"]["values"]}
            for gap in item["known_outcome"]["gaps"]:
                if "concerns" in gap:
                    self.assertIn(gap["concerns"], names, item["id"])
        self.assertEqual({i["family"] for i in items}, {"F1", "F2", "F3", "F4"})
        self.assertEqual(sum(audit_requested(i) for i in items), 10)

    def test_manifest_names_every_item_and_says_fixture(self):
        manifest = json.loads((DEV / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], "plx-eval-dev-manifest/v1")
        self.assertIs(manifest["live"], False)
        self.assertEqual(manifest["evidence_label"], "injected-transport")
        items = {i["id"]: i for i in committed_items()}
        self.assertEqual({e["id"]: e["sha256"] for e in manifest["items"]},
                         {k: item_hash(v) for k, v in items.items()})
        self.assertEqual(sorted(manifest["audit_requested"]),
                         sorted(k for k, v in items.items() if audit_requested(v)))
        # Root digests let a held-out run prove disjointness without reading dev items.
        self.assertEqual(manifest["split_salt"], SPLIT_SALT)
        self.assertEqual({e["id"]: e["root_sha256"] for e in manifest["items"]},
                         {k: item_root_digest(v) for k, v in items.items()})
        self.assertEqual(manifest["chain_records_sha256"], sorted(set().union(*(
            chain_digests(i["root"], i["known_outcome"]) for i in items.values()))))
        for item in items.values():
            self.assertEqual(split_of(item["root"]["relation"], item["root"]["key"]), "dev")
            self.assertNotIn(item["family"], HELD_OUT_ONLY)

    def test_regeneration_reproduces_the_frozen_items(self):
        sources, documents, configured, _ = dev.build_fixture(dev.eval_world())
        _, items = dev.execute(sources, documents, run_id=dev.FIXTURE_RUN_ID,
                               now=dev.FIXTURE_NOW, count=30, audit=10, id_suffix="fixture",
                               evidence_note=dev.FIXTURE_NOTE,
                               evidence_label="injected-transport", configured=configured)

        def comparable(item: dict) -> dict:  # the oracle code digest may move with edits
            return {**item, "oracle": {k: v for k, v in item["oracle"].items() if k != "id"}}

        self.assertEqual([comparable(i) for i in items],
                         [comparable(i) for i in committed_items()])


class EntryPointTest(unittest.TestCase):
    def test_fixture_run_prints_counts_and_ids_only(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dev.main(["fixture"], environ={})
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["counts"]["items"], 30)
        self.assertEqual(report["counts"]["schema_valid"], 30)
        self.assertEqual(report["counts"]["deterministic"], 30)
        self.assertIs(report["values_published"], False)
        self.assertEqual(report["evidence_label"], "injected-transport")
        text = out.getvalue()
        for leaked in ("known_outcome", "FXSO", *PO_MARKERS, "FXCPO", "AO-98", "api://",
                       "bc://"):
            self.assertNotIn(leaked, text)

    def test_a_bc_read_that_fails_once_stays_failed_for_the_whole_run(self):
        """One run is one read generation: a BC find that failed (a transient 503) is
        kept for its key, so the determinism rerun meets the same failure instead of a
        fresh read that succeeds, and the hop stays an undecided lower bound in both
        answers (held-out run: variants are answered over the same kept reads)."""
        from integrations.http import SourceHttpError
        built, documents, _, world = dev.build_fixture(dev.eval_world())

        class FailsOnce:
            def __init__(self, port):
                self.port, self.asked = port, Counter()

            def find(self, entity_set, field, value):
                self.asked[(entity_set, field, value)] += 1
                if self.asked[(entity_set, field, value)] == 1:
                    raise SourceHttpError("read-only source GET failed", status=503)
                return self.port.find(entity_set, field, value)

            def lines(self, record):
                return self.port.lines(record)

            def sample(self, entity_set, limit):
                return self.port.sample(entity_set, limit)

        port = FailsOnce(world.bc_port())
        sources = Sources(built.portal, BcReads(port))
        report, items = dev.execute(
            sources, documents, run_id="heldout-v2", now=dev.FIXTURE_NOW, count=24,
            audit=0, id_suffix="fixture", evidence_note=dev.FIXTURE_NOTE,
            evidence_label="injected-transport", configured={}, split="held_out")
        counts = report["counts"]
        self.assertGreater(counts["variants"], 0)
        self.assertEqual(counts["deterministic"], counts["items"])
        self.assertEqual(counts["nondeterministic_by"], {"family": {}, "variant": {}})
        self.assertEqual(max(port.asked.values()), 1)    # never read a second time
        self.assertTrue(report["read_failures"])
        for failed in report["read_failures"]:          # entity_set.field, never a value
            self.assertRegex(failed, r"^bc:[A-Za-z]+\.[A-Za-z]+$")
        failed_hop = [i for i in items if any(
            g["state"] == "inaccessible" and g["reason"] == "read_failed_sourcehttperror"
            for g in i["known_outcome"]["gaps"])]
        self.assertTrue(failed_hop)
        for item, again in zip(failed_hop, dev.rerun(failed_hop, sources)):
            self.assertEqual(again, item["known_outcome"])
            self.assertEqual(again["answerability"], "partial", item["id"])
        # an item whose second answer differs is counted by its family and variant
        flip = lambda batch, _: [{} if n == 0 else i["known_outcome"]
                                 for n, i in enumerate(batch)]
        with mock.patch.object(dev, "rerun", flip):
            report, items = dev.execute(
                dev.build_fixture(dev.eval_world())[0], documents, run_id="heldout-v2",
                now=dev.FIXTURE_NOW, count=24, audit=0, id_suffix="fixture",
                evidence_note=dev.FIXTURE_NOTE, evidence_label="injected-transport",
                configured={}, split="held_out")
        self.assertEqual(report["counts"]["deterministic"], len(items) - 1)
        self.assertEqual(report["counts"]["nondeterministic_by"],
                         {"family": {items[0]["family"]: 1},
                          "variant": {items[0]["variant"]: 1}})

    def test_live_without_credentials_fails_closed_without_output(self):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = dev.main(["live", "--output-dir", tmp, "--no-bc"], environ={})
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(set(json.loads(err.getvalue())), {"error"})

    def test_a_dev_deadline_stops_reads_and_the_funnel_is_still_written(self):
        """Live 8e61f78: eval.oracle_dev timed out at 1500 s and left
        no record of the stage that stopped it. The spec's argv runs
        under a read deadline inside its timeout; reads cut by it stop (no transport call
        after it) and their strata say ``blocked: budget`` with their funnels, so the
        verifier reaches a verdict on a whole record."""
        from integrations.workbench_read_api import WorkbenchReadApiClient

        path = ROOT / "verify" / "live-checks" / "eval.oracle_dev.json"
        spec = json.loads(path.read_text(encoding="utf-8"))
        self.assertLess(dev.DEV_BUDGET_SECONDS, spec["timeout_seconds"])
        clock, fetched = [0.0], []

        class Guarded:
            """A fixture port whose every call runs the read guard first, as the live
            ports' do."""
            def __init__(self, inner, guard):
                self.inner, self.guard = inner, guard

            def __getattr__(self, name):
                def call(*args, **kwargs):
                    self.guard()
                    return getattr(self.inner, name)(*args, **kwargs)
                return call

        def build(env, args, *, guard):
            def read():   # each read takes a second of the fake clock
                clock[0] += 1.0
                guard()
            world = dev.eval_world()

            def fetch(*a, **k):
                fetched.append(clock[0])
                return world.fetch(*a, **k)
            client = WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                            "plxwb_" + "f" * 40, fetch=fetch)
            return (Sources(PortalReads(dev.portal_port(client, read)),
                            BcReads(Guarded(world.bc_port(), read))),
                    Guarded(world.documents(), read), {"portal": "live-read"})

        argv = [a for a in spec["command"][3:] if a != "{tmp}/items"]
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            argv.insert(argv.index("--output-dir") + 1, tmp)
            code = dev.main(argv + ["--budget-seconds", "20"], live_build=build,
                            environ={"PLX_WORKBENCH_API_URL": "u",
                                     "PLX_WORKBENCH_API_KEY": "k"},
                            monotonic=lambda: clock[0])
        report = json.loads(out.getvalue())
        self.assertEqual(report["budget"], {"seconds": 20.0, "exhausted": True})
        self.assertTrue(fetched)
        self.assertLess(max(fetched), 20)   # no Portal read past the deadline
        cut = {name for name, entry in report["strata"].items()
               if entry.get("blocked") == "budget"}
        self.assertTrue(cut)
        self.assertLess(len(cut), len(report["strata"]))   # what was read before still counts
        for name, entry in report["strata"].items():
            self.assertIn("funnel", entry, name)
        self.assertEqual(report["unexplained_strata"], [])
        failures = live_verify.check_expectations(spec, code, report)
        self.assertTrue(failures)
        self.assertFalse([f for f in failures if f.startswith("missing")], failures)
        self.assertIn(f"strata.{sorted(cut)[0]}.candidates < 1", failures)

    def test_a_read_blocked_past_the_deadline_is_abandoned_and_the_report_written(self):
        """Live 37e539e6 (gap-04f9b49ebae9): eval.oracle_dev was killed at the spec's 1500 s
        timeout with no JSON although the reads had a deadline: the guard ran only before a
        read, so one read started before it (a Portal relation paging on, a document's OCR)
        ran on past it. Every read stops at the deadline: a blocked Portal read and a
        blocked document text read each end the run within a reserve of it, with the typed
        report (``budget.exhausted``, the strata they cut ``blocked: budget``)."""
        import threading
        import time
        from integrations.workbench_read_api import WorkbenchReadApiClient

        spec = json.loads((ROOT / "verify" / "live-checks" / "eval.oracle_dev.json")
                          .read_text(encoding="utf-8"))
        budget, reserve = 1.0, 10.0
        release = threading.Event()
        self.addCleanup(release.set)   # the abandoned reads end with the test

        class Drive:   # one document whose text read (OCR) outlives the run
            def search(self, actor, query, **kwargs):
                return {"items": [{"citation": "sharepoint://d/1@e", "id": "1",
                                   "name": "Q.pdf", "kind": "file", "size": 10}],
                        "completeness": {"truncated": False}}

            def get_content(self, actor, item_id, **kwargs):
                return {"content": b"%PDF-1.4"}

        def run(slow_rows: int | None):
            entered = threading.Event()

            def build(env, args, *, guard):
                world, rows = dev.eval_world(), [0]

                def fetch(url, *a, **k):
                    rows[0] += "/rows" in url
                    if rows[0] == slow_rows:
                        entered.set()
                        release.wait(30)
                    return world.fetch(url, *a, **k)
                client = WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                                "plxwb_" + "f" * 40, fetch=fetch)
                drive = dev.LiveDocuments(BUYER, drive_reader=Drive(),
                                          drive_paths=["/Documents"], guard=guard)

                class Documents:   # mail from the fixture world, guarded as live mail is
                    def search(self, kind, queries, **kwargs):
                        if kind == "sharepoint":
                            return drive.search(kind, queries, **kwargs)
                        guard()
                        return world.documents().search(kind, queries, **kwargs)

                class Bc:   # the fixture world's BC, guarded as the live port is
                    port = world.bc_port()

                    def __getattr__(self, name):
                        def call(*args, **kwargs):
                            guard()
                            return getattr(self.port, name)(*args, **kwargs)
                        return call
                return (Sources(PortalReads(dev.portal_port(client, guard)), BcReads(Bc())),
                        Documents(), {"portal": "live-read"})

            def capture_text(*args, **kwargs):
                entered.set()
                release.wait(30)
                return "ok", "text"
            out = io.StringIO()
            with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out), \
                    mock.patch("integrations.captured_text.capture_text", capture_text):
                began = time.monotonic()
                code = dev.main(["live", "--output-dir", tmp, "--run-id", "r1",
                                 "--budget-seconds", str(budget)], live_build=build,
                                environ={"PLX_WORKBENCH_API_URL": "u",
                                         "PLX_WORKBENCH_API_KEY": "k"})
                spent = time.monotonic() - began
            self.assertTrue(entered.is_set())
            return code, json.loads(out.getvalue()), spent

        # the third Portal page read blocks; else the SharePoint document's text read
        for slow_rows, cut in ((3, None), (None, "sharepoint")):
            with self.subTest(slow="text" if cut else "portal"):
                code, report, spent = run(slow_rows)
                self.assertLess(spent, budget + reserve)
                self.assertEqual(report["budget"], {"seconds": budget, "exhausted": True})
                blocked = {name for name, entry in report["strata"].items()
                           if entry["status"] != "read"}
                self.assertTrue(blocked)
                for name in blocked:   # each unfinished stratum typed: blocked by the budget
                    self.assertEqual(report["strata"][name].get("blocked"), "budget", name)
                if cut:
                    self.assertIn(cut, blocked)
                self.assertEqual(report["unexplained_strata"], [])
                failures = live_verify.check_expectations(spec, code, report)
                self.assertFalse([f for f in failures if f.startswith("missing")], failures)

    def test_a_plain_guard_firing_mid_read_never_makes_a_portal_outage(self):
        """Only a ``Deadline`` guards each Portal page: a plain guard (a sampler's per-kind
        clock) that fires after a read's first page leaves that read whole, never
        ``blocked: upstream``."""
        import argparse
        world, pages = dev.eval_world(), [0]

        def get_json(url, headers, **kwargs):
            kwargs.pop("timeout_seconds", None)
            pages[0] += "/rows" in url
            return world.fetch(url, headers, **kwargs)

        def guard():
            if pages[0] >= 1:
                raise dev.BudgetExhausted("kind_budget_exhausted")
        args = argparse.Namespace(no_bc=True, mailbox=None, sharepoint_path=None, folder=None)
        env = {"PLX_WORKBENCH_API_URL": "https://workbench-fixture.invalid",
               "PLX_WORKBENCH_API_KEY": "plxwb_" + "f" * 40}
        with mock.patch("integrations.workbench_read_api.get_json", get_json):
            sources, _, _ = dev.build_live(env, args, guard=guard)
            read = sources.portal.read(next(iter(RELATIONS)))
        self.assertTrue(pages[0])
        self.assertIsInstance(read["rows"], list)

    def test_a_deadline_stops_a_portal_read_at_its_next_page_through_the_public_hook(self):
        """``build_live`` hands a ``Deadline`` to the client's public ``before_page`` hook:
        once the deadline passes inside a relation read, the
        read sends no further GET and is recorded ``blocked: budget``. The client is built
        with that hook (the read alone also stopped through the
        private wrapper it replaced)."""
        import argparse
        from integrations import workbench_read_api
        world, clock, urls, hooks = dev.eval_world(), [0.0], [], []
        client = workbench_read_api.WorkbenchReadApiClient

        def built(*args, **kwargs):
            hooks.append(kwargs.get("before_page"))
            return client(*args, **kwargs)

        def get_json(url, headers, **kwargs):
            kwargs.pop("timeout_seconds", None)
            urls.append(url)
            if url.endswith("/v1/relations"):
                clock[0] = 10.0   # the deadline passes between the catalog and the rows
            return world.fetch(url, headers, **kwargs)
        args = argparse.Namespace(no_bc=True, mailbox=None, sharepoint_path=None, folder=None)
        env = {"PLX_WORKBENCH_API_URL": "https://workbench-fixture.invalid",
               "PLX_WORKBENCH_API_KEY": "plxwb_" + "f" * 40}
        deadline = dev.Deadline(5, monotonic=lambda: clock[0])
        with mock.patch("integrations.workbench_read_api.get_json", get_json), \
                mock.patch.object(workbench_read_api, "WorkbenchReadApiClient", built):
            sources, _, _ = dev.build_live(env, args, guard=deadline)
            with self.assertRaises(ReadFailed) as failed:
                sources.portal.read(next(iter(RELATIONS)))
        self.assertEqual(hooks, [deadline])
        self.assertEqual(failed.exception.cause, "budget")
        self.assertTrue(any(u.endswith("/v1/relations") for u in urls))
        self.assertFalse([u for u in urls if "/rows" in u])   # no page past the deadline

    def test_live_spec_is_valid_allowlisted_and_records_no_values(self):
        path = ROOT / "verify" / "live-checks" / "eval.oracle_dev.json"
        spec = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(live_verify.validate_spec(spec, stem=path.stem), [])
        self.assertIn("scripts.eval_oracle_dev", live_verify.ALLOWED_MODULES)
        recorded = spec["record"] + spec["record_enums"] + spec["record_keys"]
        self.assertFalse(any("known_outcome" in p or "values" in p.split(".")
                             for p in recorded))
        self.assertEqual(spec["expect"]["json_equals"]["values_published"], False)


class LiveShapedStrataTest(unittest.TestCase):
    """The dev run over a world shaped like the served replica, through the real Portal
    client (catalog-checked columns): every stratum yields candidates and items, F1-F4
    are all asked, every item is schema-valid, and the report names no silent zero."""

    def run_world(self, world):
        sources, documents, configured, _ = dev.build_fixture(world)
        return dev.execute(sources, documents, run_id="live-2099-01-02T03", now=dev.FIXTURE_NOW,
                           count=30, audit=10, id_suffix="live", evidence_note=None,
                           evidence_label="injected-transport", configured=configured)

    def test_all_seven_strata_and_four_families_through_the_client(self):
        report, items = self.run_world(build_live_shaped_world())
        self.assertEqual(report["read_failures"], [])
        self.assertEqual(report["unexplained_strata"], [])
        for name, entry in report["strata"].items():
            self.assertEqual(entry["status"], "read", (name, entry))
            self.assertGreater(entry["candidates"], 0, name)
            self.assertGreaterEqual(entry["items"], 1, name)
            self.assertEqual(sum(entry["items_by_family"].values()), entry["items"], name)
            self.assertTrue(entry["funnel"], name)
        self.assertEqual(set(report["strata"]), {"sales_order", "purchase_order",
                                                 "assembly_order", "receipt", "bc_invoice",
                                                 "mail", "sharepoint"})
        self.assertEqual(report["by_family"], QUOTAS["dev"])
        self.assertTrue({"F1", "F2", "F3", "F4"} <= set(report["candidates_by_family"]))
        self.assertEqual(report["counts"]["schema_valid"], 30)
        self.assertEqual(report["counts"]["deterministic"], 30)
        for item in items:
            self.assertEqual(validate(item, SCHEMA, root=SCHEMA), [], item["id"])
        spec = json.loads((ROOT / "verify" / "live-checks" / "eval.oracle_dev.json")
                          .read_text(encoding="utf-8"))
        live = {**report, "evidence_label": "live-read"}
        self.assertEqual(live_verify.check_expectations(spec, 0, live), [])

    def spec(self):
        return json.loads((ROOT / "verify" / "live-checks" / "eval.oracle_dev.json")
                          .read_text(encoding="utf-8"))

    def test_a_named_empty_stratum_is_reported_with_its_source_and_still_fails(self):
        # An empty stratum names the source it tried, and the live
        # spec fails until all seven kinds have candidates.
        world = build_live_shaped_world()
        world.relations["public.receipt_raw_material"] = []
        report, _ = self.run_world(world)
        receipt = report["strata"]["receipt"]
        self.assertEqual((receipt["status"], receipt["reason"]), ("empty", "relation_empty"))
        self.assertEqual(receipt["tried"], ["public.app_receipt.receipt_number",
                                            "public.receipt_raw_material.idlabid"])
        # The fixture's SharePoint files name receipt lab ids only: no searched root is named.
        shared = report["strata"]["sharepoint"]
        self.assertEqual((shared["status"], shared["reason"], shared["tried"]),
                         ("no_candidate_in_sample", "no_document_names_root",
                          ["fixture:sharepoint"]))
        self.assertGreaterEqual(shared["funnel"]["roots_searched"], 20)
        self.assertEqual(report["unexplained_strata"], [])
        live = {**report, "evidence_label": "live-read"}
        failed = ["strata.receipt.candidates < 1", "strata.sharepoint.candidates < 1"]
        self.assertEqual(live_verify.check_expectations(self.spec(), 0, live), failed)
        silent = {**live, "strata": {**live["strata"],
                                     "receipt": {**receipt, "status": "read"}}}
        silent["unexplained_strata"] = dev.unexplained_strata(silent["strata"])
        self.assertEqual(silent["unexplained_strata"], ["receipt"])
        self.assertEqual(live_verify.check_expectations(self.spec(), 0, silent),
                         failed + ["unexplained_strata not empty"])

    def test_three_named_empty_kinds_fail_the_live_check_each(self):
        world = build_live_shaped_world(documents=False)
        world.relations["public.receipt_raw_material"] = []
        report, _ = self.run_world(world)
        self.assertEqual(report["unexplained_strata"], [])
        for kind in ("mail", "sharepoint"):
            entry = report["strata"][kind]
            self.assertEqual((entry["status"], entry["reason"], entry["tried"]),
                             ("no_candidate_in_sample", "no_document_names_root",
                              [f"fixture:{kind}"]))
        live = {**report, "evidence_label": "live-read"}
        self.assertEqual(live_verify.check_expectations(self.spec(), 0, live),
                         ["strata.receipt.candidates < 1", "strata.mail.candidates < 1",
                          "strata.sharepoint.candidates < 1"])
        # fewer than 20 roots searched per source is a failure of its own
        few = json.loads(json.dumps(live))
        few["strata"]["mail"]["funnel"]["roots_searched"] = 19
        self.assertIn("strata.mail.funnel.roots_searched < 20",
                      live_verify.check_expectations(self.spec(), 0, few))
        # The verifier records the named strata whole, within its detail budget.
        detail = live_verify._cap({"fields": live_verify._recorded(self.spec(), live)})
        self.assertNotIn("fields_truncated", detail)
        self.assertEqual(detail["fields"]["strata"]["receipt"]["tried"],
                         ["public.app_receipt.receipt_number",
                          "public.receipt_raw_material.idlabid"])

    def test_the_spec_requires_a_candidate_in_each_of_the_seven_strata(self):
        mins = self.spec()["expect"]["json_min"]
        self.assertEqual({k for k in mins if k.startswith("strata.")},
                         {f"strata.{k}.candidates" for k in STRATA} |
                         {f"strata.{k}.funnel.roots_searched" for k in ("mail", "sharepoint")})
        self.assertTrue(all(mins[f"strata.{k}.candidates"] == 1 for k in STRATA))
        # the live acceptance: else no_document_names_root after >= 20 roots per source
        self.assertTrue(all(mins[f"strata.{k}.funnel.roots_searched"] == 20
                            for k in ("mail", "sharepoint")))
        self.assertIn("unexplained_strata", self.spec()["expect"]["json_empty"])
        self.assertNotIn("po_list_status", json.dumps(self.spec()))

    def test_unreadable_documents_and_bc_are_never_empty(self):
        # A source without a reader, fully withheld or cut off is not an
        # empty source; the report says which and names what it tried.
        class Mail(MailFake):
            def list_messages(self, actor, mailbox, *, search=None, **kwargs):
                return {"items": [], "completeness": {"truncated": False, "withheld_count": 4}}

        class Reader:
            def companies(self, actor):
                return {"items": [{"id": "c1", "name": "FX"}], "completeness": {}}

            def list_entities(self, actor, company, entity_set, **kwargs):
                return {"items": [], "completeness": {"truncated": True, "withheld_count": 0}}

        world = build_live_shaped_world()
        sources, _, configured, _ = dev.build_fixture(world)
        sources = Sources(sources.portal, BcReads(dev.LiveBcPort(Reader(), BUYER)))
        docs = dev.LiveDocuments(BUYER, mail_reader=Mail(), mailbox="box", folder="inbox",
                                 drive_paths=["/Documents"])
        report, _ = dev.execute(sources, docs, run_id="live-2099-01-02T03", now=dev.FIXTURE_NOW,
                                count=30, audit=10, id_suffix="live", evidence_note=None,
                                evidence_label="injected-transport", configured=configured)
        strata = report["strata"]
        mail, shared, bc = strata["mail"], strata["sharepoint"], strata["bc_invoice"]
        # every search, and the listing read when they named no root (gap-f713876847b6),
        # found only messages the audience withheld (4 each)
        searches = mail["funnel"]["searches"] + mail["funnel"].get("fallback_searches", 0)
        self.assertEqual((mail["status"], mail["reason"], mail["withheld"], mail["tried"]),
                         ("unreadable", "withheld", 4 * (searches + 1), ["mail:box/inbox"]))
        self.assertEqual((shared["status"], shared["reason"], shared["tried"]),
                         ("not_configured", "sharepoint_drive_not_configured",
                          ["sharepoint:/Documents"]))
        self.assertEqual((bc["status"], bc["reason"], bc["complete"], bc["companies"],
                          bc["tried"]),
                         ("unreadable", "truncated", False, 1,
                          ["bc:salesInvoices", "bc:purchaseInvoices"]))
        self.assertEqual(report["unexplained_strata"], [])
        self.assertNotIn("empty", {e["status"] for e in strata.values()})
        live = {**report, "evidence_label": "live-read"}
        problems = live_verify.check_expectations(self.spec(), 0, live)
        for kind in ("bc_invoice", "mail", "sharepoint"):
            self.assertIn(f"strata.{kind}.candidates < 1", problems)

    def test_a_partly_failed_search_without_a_match_is_never_a_sample(self):
        world = build_live_shaped_world()
        world.docs["mail"] = [{"handle": "graph-mail://fx/1@x", "texts": ["Lunch on Friday?"]}]
        found = world.documents()

        class Cut:   # the first search of each source fails
            max_roots = found.max_roots

            def search(self, kind, queries, **kwargs):
                answer = found.search(kind, queries, **kwargs)
                answer["results"][0] = {"query": queries[0], "failed": "unavailable"}
                return answer

        sources, _, configured, _ = dev.build_fixture(world)
        report, _ = dev.execute(sources, Cut(), run_id="live-2099-01-02T03",
                                now=dev.FIXTURE_NOW, count=30, audit=10, id_suffix="live",
                                evidence_note=None, evidence_label="injected-transport",
                                configured=configured)
        mail = report["strata"]["mail"]
        self.assertEqual((mail["status"], mail["reason"], mail["complete"]),
                         ("unreadable", "search_failed", False))
        # a partly failed search that still found candidates stays read and says it was cut
        shared = report["strata"]["sharepoint"]
        self.assertEqual((shared["status"], shared["complete"]), ("read", False))
        self.assertEqual(report["unexplained_strata"], [])

    def mail_refusing(self, answered: int):
        """The fixture documents with the tenant refusing the mail searches (403), but the
        first ``answered`` of them."""
        world = build_live_shaped_world()
        found = world.documents()

        class Refusing:
            max_roots = found.max_roots

            def search(self, kind, queries, **kwargs):
                answer = found.search(kind, queries, **kwargs)
                if kind == "mail":
                    answer["results"] = [
                        r if n < answered else {"query": r["query"], "refused": "tenant",
                                                "http_status": 403,
                                                "error_code": "error_access_denied"}
                        for n, r in enumerate(answer["results"])]
                    if not answered:
                        answer["records"] = []
                return answer

        sources, _, configured, _ = dev.build_fixture(world)
        return dev.execute(sources, Refusing(), run_id="live-2099-01-02T03",
                           now=dev.FIXTURE_NOW, count=30, audit=10, id_suffix="live",
                           evidence_note=None, evidence_label="injected-transport",
                           configured=configured)

    def test_a_mailbox_that_refuses_every_search_is_blocked_upstream_not_failed(self):
        report, _ = self.mail_refusing(0)
        mail = report["strata"]["mail"]
        self.assertEqual((mail["status"], mail["reason"], mail["blocked"], mail["http_status"],
                          mail["error_code"], mail["candidates"]),
                         ("unavailable", "search_refused", "upstream", 403,
                          "error_access_denied", 0))
        self.assertEqual(report["blocked_upstream"], [
            {"part": "mail", "source": "microsoft-graph", "http_status": 403,
             "error_code": "error_access_denied"}])
        self.assertEqual(report["counts"]["blocked_upstream"], 1)
        # SharePoint is its own source: measured, and never filling the mail stratum
        self.assertEqual(report["strata"]["sharepoint"]["status"], "read")
        self.assertNotIn("blocked", report["strata"]["sharepoint"])
        self.assertFalse([r for r in report["items"] if r["stratum"] == "mail"])
        self.assertEqual(report["unexplained_strata"], [])
        # the spec against this run's real output: blocked (host misconfiguration), not
        # a pass, and not a failure of the mail floors the tenant made unreachable
        live = json.loads(json.dumps({**report, "evidence_label": "live-read"}))
        code = dev.exit_code(live)
        self.assertEqual(code, 0)
        parts = live_verify.blocked_parts(live)
        self.assertEqual([(p["part"], p["cause"]) for p in parts],
                         [("mail", "host_misconfiguration")])
        spec = self.spec()
        self.assertIn("strata.mail.candidates < 1", live_verify.check_expectations(spec, code, live))
        self.assertEqual(live_verify.check_expectations(spec, code, live,
                                                        spec["expect_blocked"]), [])
        self.assertEqual(spec["expect"]["json_min"]["strata.mail.candidates"], 1)

    def people_guarded_run(self, refuse):
        """One dev run whose --mailbox read raises ``refuse``; SharePoint from the fixture."""
        world = build_live_shaped_world()
        found = world.documents()
        box = DocumentOverlapTest.ScopedMailbox({}, refuse=refuse)
        mail = dev.LiveDocuments(BUYER, mail_reader=box, mailbox="box")

        class Guarded:   # mail through LiveDocuments, SharePoint from the fixture
            max_roots = found.max_roots

            def search(self, kind, queries, **kwargs):
                return (mail if kind == "mail" else found).search(kind, queries, **kwargs)

            def list_documents(self, kind, limit, **kwargs):
                if kind == "mail":
                    return mail.list_documents(kind, limit, **kwargs)
                return found.list_documents(kind, limit, **kwargs) \
                    if hasattr(found, "list_documents") else None

            def blocked_mail(self):
                return mail.blocked_mail()

        sources, _, configured, _ = dev.build_fixture(world)
        report, _ = dev.execute(sources, Guarded(), run_id="live-2099-01-02T03",
                                now=dev.FIXTURE_NOW, count=30, audit=10, id_suffix="live",
                                evidence_note=None, evidence_label="injected-transport",
                                configured=configured)
        return report, box

    def test_a_mailbox_the_people_guard_blocks_is_blocked_later_ask_not_failed(self):
        """gap-501f33145a0d: with no excluded-people list the one --mailbox read fails
        closed (ExclusionUnavailable) before any request. The mail stratum is blocked by
        its scope and listed in blocked_upstream with the later_ask, so the run reads
        blocked (never a fail); SharePoint stays measured."""
        from integrations.people_exclusion import ExclusionUnavailable
        report, box = self.people_guarded_run(ExclusionUnavailable("excluded_people_oids_absent"))
        self.assertEqual(len(box.asked), 1)   # refused once; the unit is then never asked
        stratum = report["strata"]["mail"]
        self.assertEqual((stratum["status"], stratum["blocked"], stratum["candidates"]),
                         ("unavailable", "scope", 0))
        self.assertEqual(report["blocked_upstream"], [
            {"part": "mail", "source": "people-exclusion", "cause": "later_ask",
             "later_ask": "excluded-people-oids"}])
        self.assertEqual(report["counts"]["blocked_upstream"], 1)
        self.assertEqual(report["strata"]["sharepoint"]["status"], "read")
        self.assertNotIn("box", json.dumps(report["blocked_upstream"]))
        live = json.loads(json.dumps({**report, "evidence_label": "live-read"}))
        code = dev.exit_code(live)
        self.assertEqual(code, 0)
        self.assertEqual([(p["part"], p["cause"]) for p in live_verify.blocked_parts(live)],
                         [("mail", "later_ask")])
        spec = self.spec()
        self.assertIn("strata.mail.candidates < 1", live_verify.check_expectations(spec, code, live))
        self.assertEqual(live_verify.check_expectations(spec, code, live,
                                                        spec["expect_blocked"]), [])

    def test_a_guard_whose_directory_listing_failed_is_not_typed_later_ask(self):
        """A refusal chained from a failed /users listing (a 503) waits on Graph, not on
        the ID list: the mailbox is blocked all the same but no blocked_upstream part
        names later_ask, so the run fails rather than reading blocked."""
        from integrations.live_source_guard import SourceUnavailable
        from integrations.people_exclusion import ExclusionUnavailable
        refusal = ExclusionUnavailable("excluded_people_unresolved")
        refusal.__cause__ = SourceUnavailable("http_503")
        for refuse in (refusal, ExclusionUnavailable("excluded_people_unresolved")):
            with self.subTest(chained=refuse.__cause__ is not None):
                report, box = self.people_guarded_run(refuse)
                self.assertEqual(len(box.asked), 1)
                self.assertEqual(report["strata"]["mail"]["candidates"], 0)
                self.assertEqual(report["blocked_upstream"], [])
                self.assertEqual(report["counts"]["blocked_upstream"], 0)

    def test_a_mailbox_that_answers_one_search_stays_measured(self):
        report, _ = self.mail_refusing(1)
        mail = report["strata"]["mail"]
        self.assertNotIn("blocked", mail)
        self.assertEqual(report["blocked_upstream"], [])
        self.assertEqual(report["counts"]["blocked_upstream"], 0)

    def test_a_listed_sharepoint_file_names_a_root_outside_the_searched_sample(self):
        """Live: 74 listed files, 1748 number spans, 24 of 6114 roots searched, no candidate.
        The listing is matched against every closed root, not the searched 24, so a file
        whose name carries a closed root's number between separators is a candidate."""
        world = build_live_shaped_world()
        found = world.documents()

        class Listed:   # the drive search finds nothing; the listing holds the file
            max_roots = 3

            def search(self, kind, queries, **kwargs):
                answer = found.search(kind, queries, **kwargs)
                if kind == "sharepoint":
                    answer = {**answer, "records": [], "results": [
                        {"query": r["query"], "handles": [], "complete": True, "withheld": 0}
                        for r in answer["results"]]}
                return answer

            def list_documents(self, kind, limit, *, seed=""):
                return None if kind != "sharepoint" else {
                    "records": [{"handle": "sharepoint://fx-drive/scan@1",
                                 "texts": ["scan_FX98032-1_page1.pdf"]}],
                    "complete": True, "source": ["sharepoint:/Documents"]}

        sources, _, configured, _ = dev.build_fixture(world)
        report, items = dev.execute(sources, Listed(), run_id="live-2099-01-02T03",
                                    now=dev.FIXTURE_NOW, count=30, audit=10, id_suffix="live",
                                    evidence_note=None, evidence_label="injected-transport",
                                    configured=configured)
        shared = report["strata"]["sharepoint"]
        self.assertEqual((shared["status"], shared["candidates"]), ("read", 1))
        self.assertEqual((shared["funnel"]["listed_documents"],
                          shared["funnel"]["documents_naming_one"]), (1, 1))
        by_id = {item["id"]: item for item in items}
        self.assertEqual([by_id[r["id"]]["root"]["identifier"] for r in report["items"]
                          if r["stratum"] == "sharepoint"], ["FX98032-1"])

    def test_funnel_rows_and_split_candidates_are_separate_fields(self):
        report, _ = self.run_world(build_live_shaped_world())
        for name in ("sales_order", "purchase_order", "assembly_order", "receipt"):
            entry = report["strata"][name]
            self.assertNotIn("candidates_dropped_by_split", entry["funnel"], name)
            self.assertIn("candidates_dropped_by_split", entry, name)
            funnel = entry["funnel"]
            stages = sum(v for k, v in funnel.items() if k not in ("rows", "roots"))
            self.assertEqual(stages + funnel["roots"], funnel["rows"], name)
        self.assertIn("other_split", report["strata"]["bc_invoice"]["funnel"])


class PortalOutageStrataTest(unittest.TestCase):
    """Portal answering HTTP 500 on every route, BC and the documents reachable (live
    2026-10-05: mail and SharePoint 0 candidates): the document strata search for the
    closed BC invoices sampled this run and score BC-rooted items; the four Portal strata
    are blocked upstream with their status, never empty or dropped."""

    def outage_run(self, split: str = "dev"):
        from integrations.http import SourceHttpError
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from evals.oracle.fixture_world import bc_record
        from evals.oracle.generator import generate

        def portal_500(url, headers, **_):
            raise SourceHttpError("read-only source GET failed: HTTPError", status=500,
                                  error_code="internal")

        world = build_live_shaped_world()
        for n in range(1, 25):  # BC orders and posted receipts no sampled invoice names
            world.bc["purchaseOrders"].append(bc_record(
                "purchaseOrders", n % 2, 1000 + n, number=f"FXPO{710000 + n}", status="Open",
                fullyReceived=n % 3 == 0, vendorNumber=f"FXV{1 + n % 3:03d}"))
            world.bc["purchaseReceipts"].append(bc_record(
                "purchaseReceipts", n % 2, 1100 + n, number=f"FXPR{810000 + n}",
                orderNumber=f"FXPO{710000 + n}", vendorNumber=f"FXV{1 + n % 3:03d}"))
        # one number in both companies: never one root
        world.bc["purchaseOrders"].append(bc_record("purchaseOrders", 0, 1200,
                                                    number="FXPO710011", status="Open"))
        for n in range(11, 25):  # open orders and receipts: never searched, never roots
            world.docs["mail"].append({"handle": f"graph-mail://fx-box/bc-{n}@x", "texts": [
                f"RE: PO FXPO{710000 + n} delivery date", "Confirmed, FXPO799999 next"]})
            world.docs["sharepoint"].append({"handle": f"sharepoint://fx-drive/pr-{n}@e",
                                             "texts": [f"FXPR{810000 + n}_packing-slip.pdf"]})
        for n, kind in enumerate(("mail", "sharepoint") * 4):  # the paid invoices
            invoice = world.bc[("salesInvoices", "purchaseInvoices")[n % 2]][n]
            world.docs[kind].append({"handle": f"{kind}://fx/inv-{n}@x", "texts": [
                f"Remittance {invoice['fields']['number']}.pdf"]})
        portal = WorkbenchReadApiClient("https://workbench-fixture.invalid",
                                        "plxwb_" + "f" * 40, fetch=portal_500)
        sources = Sources(PortalReads(dev.portal_port(portal)), BcReads(world.bc_port()))
        if split != "dev":
            return generate(sources, run_id="live-2099-01-02T03", now=dev.FIXTURE_NOW,
                            documents=world.documents(), split=split, count=120)
        return dev.execute(sources, world.documents(), run_id="live-2099-01-02T03",
                           now=dev.FIXTURE_NOW, count=30, audit=10, id_suffix="live",
                           evidence_note=None, evidence_label="injected-transport",
                           configured={"portal": "injected-transport",
                                       "bc": "injected-transport",
                                       "documents": "injected-transport"})

    def test_documents_score_bc_rooted_items_while_the_portal_strata_are_blocked(self):
        report, items = self.outage_run()
        portal = ("assembly_order", "purchase_order", "receipt", "sales_order")
        self.assertEqual([(b["part"], b["source"], b["http_status"], b["error_code"])
                          for b in report["blocked_upstream"]],
                         [(name, "portal-workbench-api", 500, "internal") for name in portal])
        for name in portal:
            entry = report["strata"][name]
            self.assertEqual((entry["status"], entry["blocked"], entry["candidates"]),
                             ("unavailable", "upstream", 0), name)
        by_id = {item["id"]: item for item in items}
        for kind in ("mail", "sharepoint"):
            entry = report["strata"][kind]
            self.assertEqual(entry["status"], "read", (kind, entry))
            self.assertGreater(entry["candidates"], 0, kind)
            self.assertGreater(entry["items"], 0, kind)
            rooted = [by_id[r["id"]] for r in report["items"] if r["stratum"] == kind]
            self.assertEqual(len(rooted), entry["items"])
            for item in rooted:  # BC-checked roots, cited, closed (a paid invoice)
                self.assertEqual(item["root"]["identifier_kind"], "bc_number")
                self.assertEqual(item["known_outcome"]["chain"][0]["relation"],
                                 item["root"]["relation"])
                self.assertIn("bc", item["as_known_at"]["l1_generations"])
                derived = item["oracle"]["derived_from"]
                self.assertTrue(any("BC document No." in c for c in derived))
                self.assertTrue(item["closed_transaction"])
                self.assertIn(CLOSED_CITE[item["root"]["relation"]], derived, item["id"])
        for kind in ("mail", "sharepoint"):   # BC's roots only: no Portal number searched
            funnel = report["strata"][kind]["funnel"]
            self.assertLessEqual(funnel["roots"], 2 * 10 * 2)   # 10 per set and company
        self.assertEqual(report["counts"]["schema_valid"], report["counts"]["items"])
        self.assertEqual(report["counts"]["deterministic"], report["counts"]["items"])
        self.assertEqual(report["unexplained_strata"], [])
        # The spec, against this run's real output: blocked parts, the rest measured.
        spec = json.loads((ROOT / "verify" / "live-checks" / "eval.oracle_dev.json")
                          .read_text(encoding="utf-8"))
        live = json.loads(json.dumps({**report, "evidence_label": "live-read"}))
        code = dev.exit_code(live)
        self.assertEqual(code, 0)
        self.assertTrue(live_verify.blocked_parts(live))
        self.assertEqual(live_verify.check_expectations(spec, code, live,
                                                        spec["expect_blocked"]), [])
        self.assertIn("strata.purchase_order.candidates < 1",
                      live_verify.check_expectations(spec, code, live))

    def test_no_split_takes_an_open_bc_order(self):
        """Decision oracle-strata-closed-roots-only: an open BC order a document names is
        never a root, in the dev split too: only closed records are searched for."""
        report, items = self.outage_run()
        generated = self.outage_run("held_out")
        for rooted in (items, generated.items):
            self.assertTrue(rooted)
            self.assertTrue(all(item["closed_transaction"] for item in rooted))
            self.assertFalse([i for i in rooted
                              if i["root"]["identifier"].startswith(("FXPO7", "FXPR8"))])


class DocumentOverlapTest(unittest.TestCase):
    """Live 2026-10-08 (eval.oracle_dev): mail read the 200 (400) newest messages, SharePoint
    70 file names, 6,210 known numbers, documents_naming_one 0. The dev set's document
    strata and the cross-system sample search root first (the hits' mail bodies and
    SharePoint text read as below), through LiveDocuments."""

    def setUp(self):
        from evals.oracle import keys as K
        from evals.oracle.generator import (candidate_split, closed, date_columns,
                                            portal_candidates, root_date)
        from evals.oracle.source import key_text
        self.world = world = build_live_shaped_world(documents=False)
        self.sources, _, self.configured, _ = dev.build_fixture(world)
        portal = self.sources.portal
        self.closed = [c for c in portal_candidates(self.sources, "sales_order")
                       if c.family == "F1" and candidate_split(c) == "dev"][:2]
        self.dates = [root_date(row.get(column) for row in portal.lookup(
            K.SO, "id", dict(c.root_key)["id"]) for column in date_columns(K.SO))
                      for c in self.closed]
        self.open = next(key_text(r["order_number"]) for r in portal.rows(K.SO)
                         if key_text(r.get("order_number")) and not closed(K.SO, r, portal))

    def run_docs(self, docs):
        docs.max_roots = 10_000   # every root searched: the two closed ones among them
        return dev.execute(self.sources, docs, run_id="live-2099-01-02T03",
                           now=dev.FIXTURE_NOW, count=30, audit=10, id_suffix="live",
                           evidence_note=None, evidence_label="injected-transport",
                           configured=self.configured)

    def spec(self):
        return json.loads((ROOT / "verify" / "live-checks" / "eval.oracle_dev.json")
                          .read_text(encoding="utf-8"))

    def test_the_cross_sample_searches_the_mailbox_root_first(self):
        # Gap-f713876847b6: the unfamiliar sample takes its mail references the dev set's
        # way (generator.document_candidates): every mailbox read is a search, the whole
        # mailbox with its discarded folders left out; mail naming a closed order is found.
        class Mailbox(MailFake):
            def __init__(self, messages):
                self.messages, self.asked = messages, []

            def resolve_folder(self, actor, mailbox, name):
                return "fid-" + name

            def list_messages(self, actor, mailbox, *, folder=None, search=None,
                              max_items=100, **kwargs):
                self.asked.append((folder, search))
                found = [m for m in self.messages if search and search in m["subject"]]
                return {"items": found[:max_items], "completeness": {
                    "truncated": len(found) > max_items, "withheld_count": 0}}

        messages = [{"citation": f"graph-mail://box/m-{n}@c", "body_preview": "",
                     "parent_folder_id": "fid-inbox",
                     "subject": f"RE: order {self.closed[n % 2].identifier} shipped"}
                    for n in range(4)]
        box = Mailbox(messages + [{"citation": "graph-mail://box/new@c", "body_preview": "",
                                   "parent_folder_id": "fid-inbox",
                                   "subject": f"New order {self.open} confirmed"}])
        docs = dev.LiveDocuments(BUYER, mail_reader=box, mailbox="box")
        docs.max_roots = 10_000   # every root searched: the two closed ones among them
        from evals.oracle import cross_sample as cs
        drawn = cs.sample(self.sources, run_id="live-2099-01-02T03", documents=docs,
                          now=dev.FIXTURE_NOW)
        self.assertTrue(box.asked)
        self.assertTrue(all(folder is None and search for folder, search in box.asked))
        mail = drawn.kinds["mail"]
        self.assertGreaterEqual(mail["search"]["documents_naming_one"], 1, mail)
        self.assertGreaterEqual(mail["references"], 1, mail)

    class ScopedMailbox(MailFake):
        """Mailboxes by address: every search finds nothing, a listing (no search) returns
        the mailbox's own messages; ``refuse`` is raised by every read instead."""
        def __init__(self, listed, refuse=None):
            self.listed, self.refuse, self.asked = listed, refuse, []

        def resolve_folder(self, actor, mailbox, name):
            return "fid-" + name

        def list_messages(self, actor, mailbox, *, folder=None, search=None,
                          max_items=100, **kwargs):
            self.asked.append((mailbox, folder, search))
            if self.refuse is not None:
                raise self.refuse
            found = [] if search else self.listed.get(mailbox, [])
            return {"items": found[:max_items], "completeness": {
                "truncated": len(found) > max_items, "withheld_count": 0}}

    UNITS = [("mailbox-1", "first@fixture.invalid", "inbox"),
             ("mailbox-2", "second@fixture.invalid", "inbox")]

    def test_scoped_mailboxes_are_listed_when_their_searches_name_no_root(self):
        """gap-f713876847b6: mail reads the mailboxes of its capture scope, searched first
        and then listed (the SharePoint path): the root searches of both mailboxes find
        nothing, the second mailbox's listing names a closed order. Mail gets its reference
        and names what it read by scope id, never by address."""
        from evals.oracle import cross_sample as cs
        message = {"citation": "graph-mail://second/m-1@c", "body_preview": "",
                   "parent_folder_id": "fid-inbox",
                   "subject": f"RE: order {self.closed[0].identifier} shipped"}
        box = self.ScopedMailbox({"second@fixture.invalid": [message]})
        docs = dev.LiveDocuments(BUYER, mail_reader=box, mail_units=self.UNITS)
        docs.max_roots = 10_000
        drawn = cs.sample(self.sources, run_id="live-2099-01-02T03", documents=docs,
                          now=dev.FIXTURE_NOW)
        searched = {mailbox for mailbox, _, search in box.asked if search}
        self.assertEqual(searched, {"first@fixture.invalid", "second@fixture.invalid"})
        self.assertTrue(all(folder == "inbox" for _, folder, _ in box.asked))
        mail = drawn.kinds["mail"]
        self.assertGreaterEqual(mail["references"], 1, mail)
        self.assertGreaterEqual(mail["search"]["listed_documents"], 1, mail)
        self.assertEqual(mail["tried"], ["mail:mailbox-1/inbox", "mail:mailbox-2/inbox"])
        self.assertNotIn("fixture.invalid", json.dumps(mail, default=str))
        report, _ = self.run_docs(dev.LiveDocuments(BUYER, mail_reader=self.ScopedMailbox(
            {"second@fixture.invalid": [message]}), mail_units=self.UNITS))
        self.assertGreaterEqual(report["strata"]["mail"]["candidates"], 1)
        self.assertNotIn("fixture.invalid", json.dumps(report))

    def test_a_scoped_mailbox_the_exclusion_guard_cannot_clear_is_scope_blocked(self):
        """integrations/people_exclusion.py: with no excluded-people list every mailbox
        read fails closed (ExclusionUnavailable). Mail is blocked by its scope with the
        later_ask, never a guard break and never empty; the oracle_dev stratum says so."""
        from evals.oracle import cross_sample as cs
        from integrations.people_exclusion import ExclusionUnavailable
        box = self.ScopedMailbox({}, refuse=ExclusionUnavailable("excluded_people_oids_absent"))
        docs = dev.LiveDocuments(BUYER, mail_reader=box, mail_units=self.UNITS)
        docs.max_roots = 10_000
        drawn = cs.sample(self.sources, run_id="live-2099-01-02T03", documents=docs,
                          now=dev.FIXTURE_NOW)
        mail = drawn.kinds["mail"]
        self.assertEqual((mail["status"], mail["reason"], mail["blocked"]),
                         ("unavailable", "search_refused", "scope"), mail)
        self.assertEqual(mail["scope_blocked"], {"mailbox-1": "excluded-people-oids",
                                                 "mailbox-2": "excluded-people-oids"})
        self.assertNotIn("search_refused_guard", mail["search"])
        report, _ = self.run_docs(dev.LiveDocuments(BUYER, mail_reader=self.ScopedMailbox(
            {}, refuse=ExclusionUnavailable("excluded_people_oids_absent")),
            mail_units=self.UNITS))
        self.assertEqual(report["strata"]["mail"]["blocked"], "scope")

    def test_a_blocked_unit_beside_a_readable_one_is_not_a_refused_search(self):
        """Per query one answer plus, for the blocked unit, a row marked as a unit's, which
        the generator counts apart (search_unit_scope): search_refused is not inflated to
        one per unit, yet the read stays incomplete."""
        from evals.oracle import cross_sample as cs
        per_unit = [[{"query": "Q1", "handles": ["h"], "complete": True, "withheld": 0},
                     {"query": "Q2", "handles": [], "complete": True, "withheld": 0}],
                    [{"query": "Q1", "refused": "scope"}, {"query": "Q2", "refused": "scope"}]]
        merged = dev._merged(per_unit)
        self.assertEqual([("handles" in r, r.get("unit")) for r in merged],
                         [(True, None), (False, True)] * 2)
        self.assertEqual(dev._merged([per_unit[1]]), per_unit[1])   # alone: a plain refusal
        docs = dev.LiveDocuments(BUYER, mail_reader=self.ScopedMailbox({}),
                                 mail_units=[self.UNITS[1]],
                                 mail_blocked=[("mailbox-1", "mail-scope-grant")])
        docs.max_roots = 10_000
        mail = cs.sample(self.sources, run_id="live-2099-01-02T03", documents=docs,
                         now=dev.FIXTURE_NOW).kinds["mail"]
        search = mail["search"]
        self.assertNotIn("search_refused_scope", search)
        self.assertGreater(search["search_unit_scope"], 0)
        self.assertNotIn("blocked", mail)
        self.assertIs(mail.get("complete"), False)

    def test_a_mailbox_outside_the_grant_is_never_read_and_scope_blocked(self):
        """A scoped mailbox outside the grant (set aside by the capture scope) is never
        requested: mail is blocked by its scope with the grant later_ask."""
        from evals.oracle import cross_sample as cs
        box = self.ScopedMailbox({})
        docs = dev.LiveDocuments(BUYER, mail_reader=box, mail_units=[],
                                 mail_blocked=[("mailbox-1", "mail-scope-grant")])
        docs.max_roots = 10_000
        drawn = cs.sample(self.sources, run_id="live-2099-01-02T03", documents=docs,
                          now=dev.FIXTURE_NOW)
        mail = drawn.kinds["mail"]
        self.assertEqual(box.asked, [])
        self.assertEqual((mail["status"], mail["blocked"], mail["scope_blocked"]),
                         ("unavailable", "scope", {"mailbox-1": "mail-scope-grant"}), mail)

    def test_sharepoint_reads_document_text_within_the_read_units_and_its_acl(self):
        from evals.oracle import cross_sample as cs
        from integrations.live_source_guard import NotVisible
        texts = {"slip": f"Packing slip\nOrder {self.closed[0].identifier}\nQty 12",
                 "secret": f"Order {self.closed[1].identifier}"}

        class Drive:
            """One customer folder: files whose names name no record; one file's text is
            refused by the reader's visibility check. The drive's search finds a file by
            its text."""
            def __init__(self):
                self.listed, self.read = [], []

            @staticmethod
            def files():
                items = [{"kind": "file", "id": item, "name": name, "size": 99,
                          "mime_type": "text/plain", "citation": f"sharepoint://d/{item}@e"}
                         for item, name in (("slip", "packing-slip.txt"),
                                            ("secret", "notes.txt"),
                                            ("photo", "pallet.jpg"),
                                            ("huge", "archive.pdf"))]
                items[-1]["size"] = dev.MAX_TEXT_BYTES + 1
                return items

            def list_folder(self, actor, path, **kwargs):
                self.listed.append(path)
                items = ([{"kind": "folder", "path": "/Documents/C1"}]
                         if path == "/Documents" else self.files())
                return {"items": items, "completeness": {"truncated": False}}

            def search(self, actor, query, *, max_items):
                return {"items": [i for i in self.files() if query in texts.get(i["id"], "")],
                        "completeness": {"truncated": False}}

            def get_content(self, actor, item_id, *, max_bytes):
                self.read.append(item_id)
                if item_id == "secret":
                    raise NotVisible()
                return {"content": texts[item_id].encode(), "content_status": "read"}

        drive = Drive()
        docs = dev.LiveDocuments(BUYER, drive_reader=drive, drive_paths=["/Documents"])
        report, _ = self.run_docs(docs)
        shared = report["strata"]["sharepoint"]
        self.assertEqual((shared["status"], shared["candidates"]), ("read", 1))
        funnel = shared["funnel"]
        self.assertEqual((funnel["texts_read"], funnel["texts_unread"],
                          funnel["texts_unread_refused"]), (1, 1, 1))
        self.assertEqual(sorted(drive.read), ["secret", "slip"])  # the files found, once each
        self.assertNotIn(self.closed[0].identifier, json.dumps(report))
        live = {**report, "evidence_label": "live-read"}
        problems = live_verify.check_expectations(self.spec(), dev.exit_code(live), live)
        self.assertNotIn("strata.sharepoint.candidates < 1", problems)
        # the unfamiliar sample reads the same text; the refused file's never
        drive.read = []
        kinds = cs.sample(self.sources, run_id="live-2099-01-02T03", documents=docs).kinds
        self.assertEqual(kinds["sharepoint"]["references"], 1)
        # one read unit per folder listed or text read
        budget = dev.MAX_DRIVE_FOLDERS
        drive.listed, drive.read = [], []
        docs.sample("sharepoint", dev.DRIVE_FOLDERS_PER)
        self.assertLessEqual(len(drive.listed) + len(drive.read), budget)

    def test_a_scan_is_read_as_capture_reads_it_and_unread_texts_say_why(self):
        """Hourly 2026-10-10: SharePoint read 6 texts and left 16 unread, a scan was never
        OCR-read (the oracle had its own reader beside capture's capture_text)."""
        from integrations import document_understanding as du
        from integrations.live_source_guard import NotVisible
        files = {"scan": ("scan.pdf", "application/pdf", scanned_pdf(None)),
                 "secret": ("notes.txt", "text/plain", b"Order " + self.closed[1].identifier
                            .encode()),
                 "big": ("archive.pdf", "application/pdf", None),
                 "odd": ("moved.pdf", "application/pdf", None),
                 "broken": ("quote.docx", "application/vnd.openxmlformats-officedocument"
                            ".wordprocessingml.document", b"not a zip")}

        class Drive:   # every search finds every file
            def list_folder(self, actor, path, **kwargs):
                return {"items": [{"kind": "file", "id": item, "name": name, "size": 99,
                                   "mime_type": mime, "citation": f"sharepoint://d/{item}@e"}
                                  for item, (name, mime, _) in files.items()],
                        "completeness": {"truncated": False}}

            search = list_folder

            def get_content(self, actor, item_id, *, max_bytes):
                if item_id == "secret":
                    raise NotVisible()
                name, mime, content = files[item_id]
                # a content status the oracle does not know is a failed read
                status = "read" if content else "exceeds_cap" if item_id == "big" else "?"
                return {"record": {"name": name, "mime_type": mime}, "content": content,
                        "content_status": status}

        docs = dev.LiveDocuments(BUYER, drive_reader=Drive(), drive_paths=["/Documents"])
        with mock.patch.object(du, "local_ocr_available", return_value=False):
            report, _ = self.run_docs(docs)
        funnel = report["strata"]["sharepoint"]["funnel"]
        self.assertEqual(report["strata"]["sharepoint"]["candidates"], 0)
        causes = {k: v for k, v in funnel.items() if k.startswith("texts_unread_")}
        self.assertEqual(causes, {"texts_unread_ocr_unavailable": 1,
                                  "texts_unread_refused": 1,
                                  "texts_unread_exceeds_cap": 1,
                                  "texts_unread_unsupported": 1,
                                  "texts_unread_failed": 1})
        self.assertEqual((funnel["texts_read"], funnel["texts_unread"]), (0, 5))
        with mock.patch.object(dev, "MAX_TEXT_SECONDS", 0):
            self.assertEqual(docs.sample("sharepoint", 50)["texts_unread_by"],
                             {"time_budget": 5})
        if not du.local_ocr_available():
            self.skipTest("no local OCR engine (poppler + tesseract) on this host")
        files["scan"] = ("scan.pdf", "application/pdf",
                         scanned_pdf(f"Packing slip Order {self.closed[0].identifier}"))
        # a new run (a search reads each hit's text once per run)
        docs = dev.LiveDocuments(BUYER, drive_reader=Drive(), drive_paths=["/Documents"])
        report, items = self.run_docs(docs)
        shared = report["strata"]["sharepoint"]
        self.assertGreaterEqual(shared["candidates"], 1)
        self.assertEqual((shared["funnel"]["texts_read"], shared["funnel"]["texts_unread"]),
                         (1, 4))
        self.assertNotIn("texts_unread_ocr_unavailable", shared["funnel"])
        self.assertNotIn(self.closed[0].identifier, json.dumps(report))
        live = {**report, "evidence_label": "live-read"}
        problems = live_verify.check_expectations(self.spec(), dev.exit_code(live), live)
        self.assertNotIn("strata.sharepoint.candidates < 1", problems)

    def test_a_mail_body_beyond_its_preview_names_a_closed_root(self):
        from integrations.live_source_guard import NotVisible
        bodies = {"m0": f"Hello,\n{'x ' * 200}\nShipped against order "
                        f"{self.closed[0].identifier}.", "m1": NotVisible(), "m2": "  "}

        class Mailbox(MailFake):
            def __init__(self, count):
                self.count, self.bodies = count, []

            def list_messages(self, actor, mailbox, *, max_items=100, **kwargs):
                return {"items": [{"citation": f"graph-mail://box/m{n}@c", "id": f"m{n}",
                                   "change_key": "c",
                                   "parent_folder_id": "inbox-id", "subject": "Shipment",
                                   "body_preview": "Hello, x x x"}
                                  for n in range(self.count)][:max_items],
                        "completeness": {"truncated": False}}

            def get_message(self, actor, mailbox, message_id, *, include_body,
                            expected_folder_id):
                self.bodies.append((message_id, include_body, expected_folder_id))
                body = bodies.get(message_id, "no number here")
                if isinstance(body, Exception):
                    raise body
                return {"body_text": body, "change_key": "c"}

        box = Mailbox(3)
        docs = dev.LiveDocuments(BUYER, mail_reader=box, mailbox="box", folder="inbox")
        report, items = self.run_docs(docs)
        mail = report["strata"]["mail"]
        self.assertGreaterEqual(mail["candidates"], 1)
        self.assertEqual({k: v for k, v in mail["funnel"].items() if k.startswith("texts_")},
                         {"texts_read": 1, "texts_unread": 2, "texts_unread_refused": 1,
                          "texts_unread_no_text": 1})
        self.assertTrue(all(include and folder == "inbox-id"
                            for _, include, folder in box.bodies))
        self.assertNotIn(self.closed[0].identifier, json.dumps(report))
        live = {**report, "evidence_label": "live-read"}
        problems = live_verify.check_expectations(self.spec(), dev.exit_code(live), live)
        self.assertNotIn("strata.mail.candidates < 1", problems)
        # 1, 10, 60 messages: every record kept once, a quarter of those asked read
        for count in (1, 10, 60):
            box = Mailbox(count)
            found = dev.LiveDocuments(BUYER, mail_reader=box, mailbox="box",
                                      folder="inbox").sample("mail", 40, seed="s")
            handles = [r["handle"] for r in found["records"]]
            self.assertEqual((len(handles), len(set(handles))), (min(count, 40),) * 2)
            read = [m for m, *_ in box.bodies]
            self.assertEqual((len(read), len(set(read))), (min(count, 10),) * 2)
            self.assertEqual(found["texts_read"] + found["texts_unread"], min(count, 10))

    def test_a_listing_after_a_search_reads_each_text_once_a_run(self):
        """gap-fdd76b96f8f6: the SharePoint listing the generator reads when the root
        searches named no root (``list_documents``) reuses what a search read, and a search after
        it reuses what it read: each file's text is read and counted once a run. Mail has
        no such listing; ``sample`` stays an independent read."""
        reads = []
        files = {"slip": ("slip.txt", b"Packing slip"), "memo": ("memo.txt", b"Minutes"),
                 "logo": ("logo.png", b"")}

        class Drive:
            def list_folder(self, actor, path, **kwargs):
                return {"items": [{"kind": "file", "id": item, "name": name, "size": 99,
                                   "citation": f"sharepoint://d/{item}@e"}
                                  for item, (name, _) in files.items()],
                        "completeness": {"truncated": False}}

            def search(self, actor, query, **kwargs):
                listed = self.list_folder(actor, "/")
                return {**listed, "items": [e for e in listed["items"] if e["id"] == query]}

            def get_content(self, actor, item_id, *, max_bytes):
                reads.append(item_id)
                name, content = files[item_id]
                return {"record": {"name": name, "mime_type": "text/plain"},
                        "content": content, "content_status": "read"}

        docs = dev.LiveDocuments(BUYER, drive_reader=Drive(), drive_paths=["/Documents"])
        self.assertIsNone(docs.list_documents("mail", 600))
        first = docs.search("sharepoint", ["slip"], hits=5)
        self.assertEqual((first["texts_read"], reads), (1, ["slip"]))
        listed = docs.list_documents("sharepoint", 600, seed="s")
        self.assertEqual(sorted(reads), ["memo", "slip"])   # slip not read again
        self.assertEqual(listed["texts_read"], 1)
        self.assertEqual({r["handle"]: r.get("contents") for r in listed["records"]},
                         {"sharepoint://d/slip@e": ["Packing slip"],
                          "sharepoint://d/memo@e": ["Minutes"], "sharepoint://d/logo@e": None})
        again = docs.search("sharepoint", ["memo", "slip"], hits=5)
        self.assertEqual((again["texts_read"], again["texts_unread"]), (0, 0))
        self.assertEqual(sorted(reads), ["memo", "slip"])
        docs.sample("sharepoint", 600, seed="s")   # an independent read
        self.assertEqual(sorted(reads), ["memo", "memo", "slip", "slip"])

    def test_a_part_scanned_pdf_without_ocr_is_partial_and_csv_and_long_texts_are_read(self):
        """On a host without OCR a PDF with scanned pages counted as
        read; a .csv was no longer read and a text over the extractor's 100k characters
        failed (the old reader read 200k); a long OCR overran the text time.
        A PDF read on its first pages only (the page bound) is
        partial like a part-scanned one, its unread pages counted (all of them unknown when
        its page count cannot be told), never a whole read whose no-match is absence."""
        from integrations import captured_text as ct
        from integrations import document_understanding as du
        from integrations.tests.test_doc_convert import make_pdf
        long_text = "x " * 150_000 + f"Order {self.closed[1].identifier}"
        capped = [["Packing list"]] * du.MAX_PAGES + [[f"Order {self.closed[0].identifier}"]] * 5
        files = {"part": ("slip.pdf", "application/pdf",
                          scanned_pdf(None, text_page=f"Order {self.closed[0].identifier}")),
                 "table": ("orders.csv", "text/csv",
                           f"order,qty\n{self.closed[1].identifier},12\n".encode()),
                 "long": ("log.txt", "text/plain", long_text.encode()),
                 "capped": ("packet.pdf", "application/pdf", make_pdf(capped))}

        class Drive:   # every search finds every file
            def list_folder(self, actor, path, **kwargs):
                return {"items": [{"kind": "file", "id": item, "name": name, "size": 99,
                                   "mime_type": mime, "citation": f"sharepoint://d/{item}@e"}
                                  for item, (name, mime, _) in files.items()],
                        "completeness": {"truncated": False}}

            search = list_folder

            def get_content(self, actor, item_id, *, max_bytes):
                name, mime, content = files[item_id]
                return {"record": {"name": name, "mime_type": mime}, "content": content,
                        "content_status": "read"}

        docs = dev.LiveDocuments(BUYER, drive_reader=Drive(), drive_paths=["/Documents"])
        # independent of the host's readers: no OCR engine here
        with mock.patch.object(du, "local_ocr_available", return_value=False):
            found = docs.sample("sharepoint", 50, seed="s")
            report, _ = self.run_docs(docs)
        contents = {r["handle"]: r.get("contents") for r in found["records"]}
        self.assertEqual((found["texts_read"], found["texts_unread"], found["texts_unread_by"],
                          found["pages_unread"]),
                         (2, 2, {"skipped_by_cap": 1, "unread_scanned_pages": 1}, 6))
        self.assertNotIn(self.closed[0].identifier, contents["sharepoint://d/capped@e"][0])
        self.assertIn(self.closed[0].identifier, contents["sharepoint://d/part@e"][0])
        self.assertIn(self.closed[1].identifier, contents["sharepoint://d/table@e"][0])
        self.assertEqual(len(contents["sharepoint://d/long@e"][0]), dev.MAX_TEXT_CHARS)
        shared = report["strata"]["sharepoint"]
        self.assertGreaterEqual(shared["candidates"], 1)
        self.assertEqual({k: v for k, v in shared["funnel"].items()
                          if k.startswith("texts_")},
                         {"texts_read": 2, "texts_unread": 2,
                          "texts_unread_skipped_by_cap": 1,
                          "texts_unread_unread_scanned_pages": 1})
        with mock.patch.object(du, "local_ocr_available", return_value=False), \
                mock.patch.object(du, "pdf_page_count", return_value=None):
            unknown = docs.sample("sharepoint", 50, seed="s")
        self.assertEqual((unknown["texts_read"], unknown["texts_unread_by"],
                          unknown["pages_unread"]),
                         (2, {"skipped_by_cap": 1, "unread_scanned_pages": 1}, 1))
        live = {**report, "evidence_label": "live-read"}
        problems = live_verify.check_expectations(self.spec(), dev.exit_code(live), live)
        self.assertNotIn("strata.sharepoint.candidates < 1", problems)
        self.assertNotIn(self.closed[0].identifier, json.dumps(report))
        # a reading still running when the text time ends (a long OCR) is abandoned
        import threading
        import time
        release = threading.Event()

        def ocr(content, record, **kwargs):
            release.wait(5)
            return ct.TEXT_OCR, "late"
        with mock.patch.object(ct, "capture_text", ocr), \
                mock.patch.object(dev, "MAX_TEXT_SECONDS", 0.2):
            began = time.monotonic()
            found = docs.sample("sharepoint", 50, seed="s")
            spent = time.monotonic() - began
        release.set()
        self.assertLess(spent, 2)
        self.assertEqual((found["texts_read"], found["texts_unread_by"]),
                         (0, {"time_budget": 4}))

    def test_a_mail_body_of_another_change_key_is_not_attached_and_one_lookup_serves(self):
        """Over the real GraphMailReader: a message edited between list
        and body read lent its new body to the listed record, and every body read looked
        the mailbox up again."""
        from integrations import graph_read
        from integrations.live_source_guard import LiveSourceAudience
        from integrations.tests.test_live_source_adapters import (
            ACTOR, ENV, MAILBOX, MSG_PATH, VINCE_ID, FakeSender, j, message)

        def body(raw, text):
            return j(200, {**raw, "body": {"contentType": "text", "content": text}})
        sender = FakeSender({
            f"/v1.0/users/{VINCE_ID}/mailFolders/INBOX-V/messages": j(200, {"value": [
                message("m1", "ck1"), message("m2", "ck2")]}),
            f"{MSG_PATH}/m1": body(message("m1", "ck1"),
                                   f"Shipped order {self.closed[0].identifier}"),
            f"{MSG_PATH}/m2": body(message("m2", "ck2-edited"),
                                   f"Shipped order {self.closed[1].identifier}")})
        api, _ = graph_read.build_graph(ENV, sender=sender)
        reader = graph_read.GraphMailReader(
            api, LiveSourceAudience({f"mail:{MAILBOX}": ["staff:vince"]}, tenant="plx"))
        docs = dev.LiveDocuments(ACTOR, mail_reader=reader, mailbox=MAILBOX, folder="inbox")
        found = docs.sample("mail", 8, seed="s")
        contents = {r["handle"].split("/")[-1]: r.get("contents") for r in found["records"]}
        self.assertEqual(contents["m1@ck1"],
                         [f"Shipped order {self.closed[0].identifier}"])
        self.assertIsNone(contents["m2@ck2"])
        self.assertEqual((found["texts_read"], found["texts_unread_by"]),
                         (1, {"unavailable": 1}))
        calls = sender.api_calls()
        lookups = [c for c in calls if re.fullmatch(r"/v1\.0/users/[^/]+",
                                                    urlsplit(c[1]).path)]
        self.assertEqual(len(lookups), 1)
        self.assertTrue(all(c[0] == "GET" for c in calls))


def scanned_pdf(text: str | None, *, text_page: str | None = None) -> bytes:
    """A one-page PDF that is one 8-bit grey image and no text layer (a scan): ``text``
    rendered by reportlab and poppler's ``pdftoppm`` at 150 dpi, a blank page for None.
    ``text_page``: a first page with that text in its text layer (a part-scanned PDF)."""
    import subprocess
    import zlib
    width, height, pixels = 1250, 208, b"\xff" * 1250 * 208
    if text is not None:
        if importlib.util.find_spec("reportlab") is None:
            raise unittest.SkipTest("needs reportlab")
        from reportlab.pdfgen import canvas
        page = io.BytesIO()
        drawn = canvas.Canvas(page, pagesize=(600, 100))
        drawn.setFont("Helvetica", 24)
        drawn.drawString(20, 40, text)
        drawn.save()
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["pdftoppm", "-gray", "-r", "150", "-singlefile", "-",
                            f"{tmp}/page"], input=page.getvalue(), check=True,
                           capture_output=True)
            pgm = Path(tmp, "page.pgm").read_bytes()
        header = re.match(rb"P5\s(\d+)\s(\d+)\s255\s", pgm)
        width, height, pixels = int(header[1]), int(header[2]), pgm[header.end():]
    image = zlib.compress(pixels)
    kids = b"[6 0 R 3 0 R] /Count 2" if text_page is not None else b"[3 0 R] /Count 1"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids %s >>" % kids,
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 100] "
               b"/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace "
               b"/DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\n"
               b"stream\n%s\nendstream" % (width, height, len(image), image),
               b"<< /Length 31 >>\nstream\nq 600 0 0 100 0 0 cm /Im0 Do Q\nendstream"]
    if text_page is not None:
        stream = b"BT /F1 18 Tf 20 40 Td (%s) Tj ET" % text_page.encode("latin-1")
        objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 100] "
                    b"/Resources << /Font << /F1 7 0 R >> >> /Contents 8 0 R >>",
                    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                    b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref)
    return bytes(out)


class LiveBcPortTest(unittest.TestCase):
    """F4 through the real BC reader: the invoice is found by vendor invoice No. only,
    its G/L freight line is not an item, quantities stay per item and unit."""

    def test_three_way_match_over_the_real_reader(self):
        bc = Fixture()
        portal = World({
            "public.app_purchase_order": [{"id": "po", "po_number": PO, "status": "PARTIAL",
                                           "supplier_id": "v", "custom_fields": {}}],
            "public.app_purchase_order_line": [
                {"id": "pl1", "purchase_order_id": "po", "product_id": "p1",
                 "quantity": "3000.0", "uom": "ea"}],
            "public.app_receipt": [{"id": "r1", "receipt_number": "R1", "purchase_order_id": "po",
                                    "status": "RELEASED", "vendor_invoice_number": "INV-9"}],
            "public.app_receipt_line": [{"id": "rl1", "receipt_id": "r1", "product_id": "p1",
                                         "quantity": "1800.0", "uom": "each"}],
            "public.app_product": [{"id": "p1", "code": "P-0821"}],
        })
        port = dev.LiveBcPort(bc.reader, BUYER)
        sources = Sources(PortalReads(portal.read_relation), BcReads(port))
        answer = run("F4", {"identifier": PO, "identifier_kind": "po_number"},
                     {"time": "2099-01-01T00:00:00Z", "l1_generations": {"portal_api": "x"}},
                     sources)
        got = {v["name"]: v for v in answer.result["values"]}
        self.assertEqual(got["item_1"]["value"], "P-0821")
        self.assertEqual((got["ordered_1"]["value"], got["received_1"]["value"],
                          got["invoiced_1"]["value"]), ("3000", "1800", "1800"))
        self.assertEqual(got["invoiced_1"]["unit"], "PCS")
        self.assertIs(got["match_1"]["value"], False)
        self.assertTrue(got["invoiced_1"]["citations"][0]["l1_handle"].startswith("bc://"))
        self.assertIn("bc", answer.generations)
        methods = {call[0] for call in bc.sender.api_calls()}
        self.assertEqual(methods, {"GET"})


class LiveDocumentsTest(unittest.TestCase):
    def test_maps_subjects_previews_and_file_names(self):
        class Mail(MailFake):
            def list_messages(self, actor, mailbox, **kwargs):
                return {"items": [{"citation": "graph-mail://m/1@c", "subject": "S",
                                   "body_preview": "P"}], "completeness": {"truncated": False}}

        # A library folder holds one folder per customer, the files sit below it.
        tree = {"/x": [{"citation": "sharepoint://d/2@e", "name": "C", "kind": "folder",
                        "path": "/x/C"}],
                "/x/C": [{"citation": "sharepoint://d/1@e", "name": "N.pdf", "kind": "file"},
                         {"citation": "sharepoint://d/3@e", "name": "D", "kind": "folder"}]}

        class Drive:
            asked: list = []

            def list_folder(self, actor, path, **kwargs):
                self.asked.append(path)
                return {"items": tree[path], "completeness": {"truncated": False}}

        docs = dev.LiveDocuments(BUYER, mail_reader=Mail(), mailbox="box", folder="inbox",
                                 drive_reader=Drive(), drive_paths=["/x"])
        self.assertEqual(docs.sample("mail", 50)["records"],
                         [{"handle": "graph-mail://m/1@c", "texts": ["S", "P"]}])
        shared = docs.sample("sharepoint", 50)
        self.assertEqual(shared["records"], [{"handle": "sharepoint://d/1@e",
                                              "texts": ["N.pdf"]}])
        self.assertEqual(Drive.asked, ["/x", "/x/C"])
        self.assertFalse(shared["complete"])  # folder D (no path) was left unread
        self.assertEqual(shared["source"], ["sharepoint:/x"])
        del tree["/x/C"][1]
        self.assertTrue(docs.sample("sharepoint", 50)["complete"])
        self.assertEqual(dev.LiveDocuments(BUYER).sample("mail", 5)["records"], [])

    def test_a_kind_without_a_reader_is_not_configured_never_an_empty_read(self):
        asked = dev.LiveDocuments(BUYER, mailbox="box", folder="inbox", drive_paths=["/D"])
        mail, shared = asked.sample("mail", 5), asked.sample("sharepoint", 5)
        self.assertEqual((mail["not_configured"], mail["source"], mail["complete"]),
                         ("mail_reader_not_configured", ["mail:box/inbox"], False))
        self.assertEqual((shared["not_configured"], shared["source"]),
                         ("sharepoint_drive_not_configured", ["sharepoint:/D"]))
        bare = dev.LiveDocuments(BUYER)
        self.assertEqual(bare.sample("mail", 5)["not_configured"], "mailbox_not_given")
        self.assertEqual(bare.sample("sharepoint", 5)["not_configured"],
                         "sharepoint_paths_not_given")


class LiveSearchTest(unittest.TestCase):
    """``LiveDocuments.search`` over the real GET-only readers (recorded-shape
    ``FakeSender``; injected transport, not live): mail ``$search`` in the folder, drive
    search, every hit through the reader's audience, and typed refusals."""

    def test_mail_search_is_get_only_folder_bound_and_refusals_are_typed(self):
        from integrations import graph_read
        from integrations.live_source_guard import LiveSourceAudience
        from integrations.tests.test_live_source_adapters import (
            ACTOR, ENV, MAILBOX, MSG_PATH, OTHER, VINCE_ID, FakeSender, j, message)

        def listing(url):
            if "BADQ" in url:
                return j(400, {"error": {"code": "BadRequest"}})
            if "DOWN" in url:
                return j(503, {"error": {"code": "ServiceUnavailable"}})
            return j(200, {"value": [message("m1", "ck1", "Shipped PO#12345"),
                                     message("x9", "xk9", folder="INBOX-C")]})
        sender = FakeSender({
            f"/v1.0/users/{VINCE_ID}/mailFolders/INBOX-V/messages": listing,
            f"{MSG_PATH}/m1": j(200, {**message("m1", "ck1"), "body": {
                "contentType": "text", "content": "Body naming PO#12345"}})})
        api, _ = graph_read.build_graph(ENV, sender=sender)
        reader = graph_read.GraphMailReader(
            api, LiveSourceAudience({f"mail:{MAILBOX}": ["staff:vince"]}, tenant="plx"))
        docs = dev.LiveDocuments(ACTOR, mail_reader=reader, mailbox=MAILBOX, folder="inbox")
        found = docs.search("mail", ["PO#12345", "a/b", "BADQ1", "DOWN1"], hits=5, seed="s")
        self.assertEqual(found["results"], [
            {"query": "PO#12345", "handles": [found["records"][0]["handle"]],
             "complete": True, "withheld": 1},          # another folder's message: dropped
            {"query": "a/b", "refused": "guard"},       # outside the search grammar
            {"query": "BADQ1", "refused": "tenant", "http_status": 400,
             "error_code": "bad_request"},              # Graph 400 on the search
            {"query": "DOWN1", "failed": "unavailable"}])
        self.assertEqual(found["records"][0]["contents"], ["Body naming PO#12345"])
        calls = sender.api_calls()
        self.assertTrue(all(c[0] == "GET" for c in calls))
        searched = [urlsplit(c[1]) for c in calls if c[1].split("?")[0].endswith("/messages")]
        self.assertEqual(len(searched), 3)   # the refused query sent nothing
        for url in searched:
            self.assertIn("%24search=%22", url.query)
            self.assertNotIn("orderby", url.query)
        # a second search finding the same message reads its body no more
        again = docs.search("mail", ["PO#12345"], hits=5, seed="s")
        bodies = [c for c in sender.api_calls() if c[1].split("?")[0].endswith("/m1")]
        self.assertEqual((again["texts_read"], len(bodies)), (0, 1))
        self.assertEqual(again["records"][0]["contents"], ["Body naming PO#12345"])
        # a reader the audience refuses the mailbox: refused before any message read
        sender.calls.clear()
        other = dev.LiveDocuments(OTHER, mail_reader=reader, mailbox=MAILBOX, folder="inbox")
        self.assertEqual(other.search("mail", ["PO#12345"], hits=5)["results"],
                         [{"query": "PO#12345", "refused": "guard"}])
        self.assertFalse([c for c in sender.api_calls() if "/messages" in c[1]])

    def test_mail_search_a_tenant_refuses_is_answered_from_the_listing(self):
        """Live daily 8ecf8680c: every mail ``$search`` came back 400/403/501 (refused:
        tenant) and the mail stratum had no candidate. The listing answers instead."""
        from integrations import graph_read
        from integrations.live_source_guard import LiveSourceAudience
        from integrations.tests.test_live_source_adapters import (
            ACTOR, ENV, MAILBOX, MSG_PATH, VINCE_ID, FakeSender, j, message)

        def listing(url):
            if "search" in url:
                return j(403, {"error": {"code": "ErrorAccessDenied"}})
            return j(200, {"value": [message("m1", "ck1", "Shipped PO#12345"),
                                     message("m2", "ck2", "Weekly update")]})
        sender = FakeSender({
            f"/v1.0/users/{VINCE_ID}/mailFolders/INBOX-V/messages": listing,
            f"{MSG_PATH}/m1": j(200, {**message("m1", "ck1"), "body": {
                "contentType": "text", "content": "Body naming PO#12345"}})})
        api, _ = graph_read.build_graph(ENV, sender=sender)
        reader = graph_read.GraphMailReader(
            api, LiveSourceAudience({f"mail:{MAILBOX}": ["staff:vince"]}, tenant="plx"))
        docs = dev.LiveDocuments(ACTOR, mail_reader=reader, mailbox=MAILBOX, folder="inbox")
        found = docs.search("mail", ["PO#12345", "NOPE99"], hits=5, seed="s")
        handles = [r["handle"] for r in found["records"]]
        self.assertEqual([(r["query"], len(r["handles"])) for r in found["results"]],
                         [("PO#12345", 1), ("NOPE99", 0)])
        self.assertTrue(all("refused" not in r for r in found["results"]))
        self.assertEqual(found["results"][0]["handles"], handles[:1])
        self.assertEqual(found["records"][0]["contents"], ["Body naming PO#12345"])
        self.assertTrue(all(c[0] == "GET" for c in sender.api_calls()))

    def test_scanned_mail_lists_once_marks_a_cut_match_list_and_a_failed_listing_stays_refused(self):
        from integrations import graph_read
        from integrations.live_source_guard import LiveSourceAudience
        from integrations.tests.test_live_source_adapters import (
            ACTOR, ENV, MAILBOX, VINCE_ID, FakeSender, j, message)
        state = {"fail": False}

        def listing(url):
            if "search" in url:
                return j(403, {"error": {"code": "ErrorAccessDenied"}})
            if state["fail"]:
                return j(503, {"error": {"code": "ServiceUnavailable"}})
            return j(200, {"value": [message("m1", "ck1", "PO#12345 a"),
                                     message("m2", "ck2", "PO#12345 b")]})
        sender = FakeSender({f"/v1.0/users/{VINCE_ID}/mailFolders/INBOX-V/messages": listing})
        api, _ = graph_read.build_graph(ENV, sender=sender)
        reader = graph_read.GraphMailReader(
            api, LiveSourceAudience({f"mail:{MAILBOX}": ["staff:vince"]}, tenant="plx"))
        docs = dev.LiveDocuments(ACTOR, mail_reader=reader, mailbox=MAILBOX, folder="inbox")

        def lists():
            return [c for c in sender.api_calls() if "search" not in c[1]
                    and c[1].split("?")[0].endswith("/messages")]
        cut = docs.search("mail", ["PO#12345"], hits=1, seed="s")
        self.assertEqual((len(cut["results"][0]["handles"]), cut["results"][0]["complete"],
                          cut["results"][0]["scan"]), (1, False, True))
        docs.search("mail", ["PO#12345"], hits=5, seed="s")
        self.assertEqual(len(lists()), 1)   # one listing for the instance
        state["fail"] = True
        again = dev.LiveDocuments(ACTOR, mail_reader=reader, mailbox=MAILBOX, folder="inbox")
        failed = again.search("mail", ["PO#12345"], hits=5, seed="s")
        self.assertEqual(failed["results"], [
            {"query": "PO#12345", "refused": "tenant", "http_status": 403,
             "error_code": "error_access_denied", "scan_failed": True}])
        # a failed listing is tried again on a later search (a transient 503), at most
        # MAIL_SCAN_TRIES listings per instance
        state["fail"] = False
        retried = again.search("mail", ["PO#12345"], hits=5, seed="s")
        self.assertEqual(len(retried["results"][0]["handles"]), 2)
        state["fail"] = True
        third = dev.LiveDocuments(ACTOR, mail_reader=reader, mailbox=MAILBOX, folder="inbox")
        before = len(lists())
        for _ in range(3):
            third.search("mail", ["PO#12345"], hits=5, seed="s")
        self.assertEqual(len(lists()) - before, dev.MAIL_SCAN_TRIES)

    def test_drive_search_opens_each_hit_through_the_audience(self):
        from integrations.tests.test_live_source_adapters import (
            ACTOR, DRIVE, DRIVE_BASE, FakeSender, audience, drive_item, graph_api, j)
        from integrations.graph_read import SharePointReader
        hit = drive_item("F1", "FXSO1001_packing-slip.pdf")
        secret = drive_item("S1", "Secret-FXSO1001.xlsx")
        folder = drive_item("D1", "FXSO1001", is_folder=True)
        sender = FakeSender({
            f"{DRIVE_BASE}/root/search(q='FXSO1001')": j(200, {"value": [hit, secret,
                                                                         folder]}),
            f"{DRIVE_BASE}/items/F1": j(200, hit), f"{DRIVE_BASE}/items/S1": j(200, secret)})
        api, transport = graph_api(sender)
        reader = SharePointReader(api, audience(), DRIVE, transport=transport)
        docs = dev.LiveDocuments(ACTOR, drive_reader=reader, drive_paths=["/Customers"])
        with mock.patch.object(dev, "MAX_SEARCH_TEXTS", 0):
            found = docs.search("sharepoint", ["FXSO1001"], hits=5, seed="s")
        (result,) = found["results"]
        self.assertEqual([r["texts"] for r in found["records"]],
                         [["FXSO1001_packing-slip.pdf"]])   # the secret file withheld
        self.assertEqual((result["handles"], result["withheld"]),
                         ([found["records"][0]["handle"]], 2))   # + the folder, dropped
        self.assertEqual(found["source"], ["sharepoint:/"])
        calls = sender.api_calls()
        self.assertTrue(all(c[0] == "GET" for c in calls))
        self.assertFalse([c for c in calls if "/items/S1/" in c[1]])   # no ACL, no content
        # a reader without any grant on the drive: refused, no I/O
        sender.calls.clear()
        nobody = dev.LiveDocuments(Actor("plx", frozenset({"staff:x"}), "restricted"),
                                   drive_reader=reader, drive_paths=["/Customers"])
        self.assertEqual(nobody.search("sharepoint", ["FXSO1001"], hits=5)["results"],
                         [{"query": "FXSO1001", "refused": "guard"}])
        self.assertEqual(sender.calls, [])
        # not configured is said, never an empty search
        self.assertEqual(dev.LiveDocuments(ACTOR, drive_reader=reader).search(
            "sharepoint", ["FXSO1001"], hits=5)["not_configured"], "sharepoint_paths_not_given")


HELDOUT = ROOT / "evals" / "heldout"
FIXTURE_MANIFEST = HELDOUT / "manifest.fixture.json"


def run_heldout(state: Path, *extra: str, environ: dict | None = None) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
        code = dev.main(["heldout", "--source", "fixture", "--state-dir", str(state), *extra],
                        environ=environ or {})
    return code, stdout.getvalue()


class HeldOutTest(unittest.TestCase):
    """The held-out path, keyless over the fixture world (``--source fixture``); live it
    runs only in the credentialed verifier (verify/live-checks/eval.heldout.json)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name) / "state"
        cls.code, cls.text = run_heldout(cls.state)
        cls.manifest = json.loads(cls.text)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def items(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8"))
                for p in sorted((self.state / "fixture" / dev.HELDOUT_RUN_ID / "items")
                                .glob("ho-*.json"))]

    def test_120_valid_items_over_six_families_frozen_in_the_state_dir(self):
        self.assertEqual(self.code, 0)
        counts = self.manifest["counts"]
        self.assertEqual(self.manifest["by_variant"]["standard"], 120)
        self.assertEqual((counts["schema_valid"], counts["deterministic"]),
                         (counts["items"], counts["items"]))
        self.assertEqual(self.manifest["by_family"], QUOTAS["held_out"])
        self.assertEqual(counts["families_without_plan"], 2)
        self.assertEqual(self.manifest["frozen"]["reused"], False)
        items = self.items()
        self.assertEqual(len(items), counts["items"])
        for item in items:
            self.assertEqual(validate(item, SCHEMA, root=SCHEMA), [], item["id"])
            self.assertEqual(item["split"], "held_out")
        frozen = json.loads((self.state / "fixture" / dev.HELDOUT_RUN_ID / "frozen.json")
                            .read_text("utf-8"))
        self.assertEqual(frozen["set_sha256"], self.manifest["set_sha256"])
        self.assertEqual(frozen["item_sha256"], sorted(dev.content_hash(i) for i in items))

    def test_variants_are_emitted_with_their_labels_and_scored_on_their_own_reads(self):
        counts, by_variant = self.manifest["counts"], self.manifest["by_variant"]
        self.assertEqual(set(by_variant), {"standard", *perturb.TRANSFORMS})
        self.assertEqual(counts["variants"], counts["items"] - by_variant["standard"])
        self.assertGreaterEqual(counts["variants"], 100)   # one per item, transforms in turn
        self.assertEqual(Counter(i["variant"] for i in self.manifest["items"]), by_variant)
        items = {i["id"]: i for i in self.items()}
        variants = [i for i in items.values() if i["variant"] != "standard"]
        for item in variants:
            parent = items[item["parent_id"]]
            self.assertEqual((parent["variant"], parent["root"]),
                             ("standard", item["root"]))
            self.assertEqual(item["perturbation"]["transform"], item["variant"])
        # Scored on the reads each was answered from (its parent's reads perturbed again):
        # the same label; over the unperturbed reads most labels would differ.
        sources, _, _, _ = dev.build_fixture(dev.eval_world())
        sources.portal.prefetch()
        self.assertEqual(dev.rerun(variants, sources), [i["known_outcome"] for i in variants])
        plain = sum(run(i["family"], i["root"], i["as_known_at"], sources).result
                    != i["known_outcome"] for i in variants)
        self.assertGreater(plain, len(variants) // 2)

    def test_no_transaction_record_shared_with_the_frozen_dev_set(self):
        dev_items = committed_items()
        dev_records = set().union(*(chain_digests(i["root"], i["known_outcome"])
                                    for i in dev_items))
        for item in self.items():
            self.assertEqual(chain_digests(item["root"], item["known_outcome"]) & dev_records,
                             set(), item["id"])
        counts = self.manifest["counts"]
        self.assertEqual((counts["dev_root_overlap"], counts["dev_chain_overlap"],
                          counts["partition_violations"]), (0, 0, 0))
        self.assertGreater(self.manifest["partition_dropped"]["shared_chain"], 0)
        self.assertEqual(self.manifest["dev_records_excluded"], len(dev_records))

    def test_overlap_with_the_dev_set_is_counted_and_fails(self):
        items = self.items()
        report = {**self.manifest, "counts": {k: v for k, v in self.manifest["counts"].items()
                                              if not k.startswith(("dev_", "partition_v",
                                                                   "families_"))}}
        record = sorted(chain_digests(items[0]["root"], items[0]["known_outcome"]))[-1]
        clash = dev.heldout_manifest(report, items, (set(), {record}),
                                     budget={"seconds": 1, "exhausted": False})
        self.assertGreaterEqual(clash["counts"]["dev_chain_overlap"], 1)
        self.assertFalse(dev.heldout_ok(clash))
        spent = dev.heldout_manifest(report, items, (set(), set()),
                                     budget={"seconds": 1, "exhausted": True})
        self.assertFalse(dev.heldout_ok(spent))

    def test_stdout_is_counts_and_hashes_only(self):
        for leaked in ("known_outcome", "question", "FXSO", *PO_MARKERS, "FXCPO", "AO-98",
                       "api://", "bc://", "identifier", "root_sha256", "ho-F", "held_out",
                       self.tmp.name):
            self.assertNotIn(leaked, self.text)
        self.assertIs(self.manifest["values_published"], False)
        self.assertEqual(self.manifest["evidence_label"], "injected-transport")
        self.assertEqual(self.manifest["adapter_status"], "live-unverified")
        self.assertRegex(self.manifest["set_sha256"], r"^[0-9a-f]{64}$")

    def test_second_run_reuses_the_frozen_set_and_never_resamples(self):
        code, text = run_heldout(self.state)
        again = json.loads(text)
        self.assertEqual(code, 0)
        self.assertIs(again["frozen"]["reused"], True)
        self.assertEqual(again["set_sha256"], self.manifest["set_sha256"])
        self.assertEqual(again["counts"]["label_drift"], 0)

    def test_frozen_set_survives_a_grown_source(self):
        from evals.oracle.fixture_world import build_world
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with mock.patch.object(dev, "eval_world",
                                   lambda: build_world(sales=110, purchases=88)):
                code, first = run_heldout(state)
            self.assertEqual(code, 0)
            code, second = run_heldout(state)  # the full eval world: more orders
            self.assertEqual(code, 0)
            first, second = json.loads(first), json.loads(second)
            self.assertIs(second["frozen"]["reused"], True)
            self.assertEqual(second["set_sha256"], first["set_sha256"])
            # A fresh generation over the grown source would differ: freezing matters.
            self.assertNotEqual(first["set_sha256"], self.manifest["set_sha256"])
            # An explicit version bump draws a new set beside the frozen one.
            code, bumped = run_heldout(state, "--run-id", "heldout-v3")
            self.assertEqual(code, 0)
            self.assertIs(json.loads(bumped)["frozen"]["reused"], False)
            self.assertTrue((state / "fixture" / dev.HELDOUT_RUN_ID / "frozen.json").exists())

    def test_default_run_id_is_the_one_the_live_spec_draws(self):
        spec = json.loads((ROOT / "verify" / "live-checks" / "eval.heldout.json")
                          .read_text(encoding="utf-8"))
        command = spec["command"]
        self.assertEqual(command[command.index("--run-id") + 1], dev.HELDOUT_RUN_ID)
        self.assertEqual(self.manifest["run_id"], dev.HELDOUT_RUN_ID)

    def test_tampered_frozen_set_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertEqual(run_heldout(state)[0], 0)
            victim = sorted((state / "fixture" / dev.HELDOUT_RUN_ID / "items").glob("ho-*.json"))[0]
            item = json.loads(victim.read_text(encoding="utf-8"))
            item["known_outcome"]["answerability"] = "blocked"
            victim.write_text(json.dumps(item), encoding="utf-8")
            code, text = run_heldout(state)
            self.assertEqual((code, text), (1, ""))

    def live_stub(self, world=None):
        """build_live replaced by the fixture ports (keyless): the run believes it is live."""
        def build(env, args, *, guard):
            sources, documents, configured, _ = dev.build_fixture(world or dev.eval_world())
            return sources, documents, configured
        return mock.patch.object(dev, "build_live", build)

    LIVE_ENV = {"PLX_WORKBENCH_API_URL": "https://fixture.invalid",
                "PLX_WORKBENCH_API_KEY": "plxwb_" + "0" * 40}

    def run_live(self, state: Path, world=None, *extra: str) -> tuple[int, dict]:
        stdout = io.StringIO()
        with self.live_stub(world), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            code = dev.main(["heldout", "--state-dir", str(state), "--no-bc", *extra],
                            environ=self.LIVE_ENV)
        return code, json.loads(stdout.getvalue())

    def test_a_fixture_frozen_set_is_never_reported_as_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertEqual(run_heldout(state)[0], 0)  # fixture freezes under fixture/
            code, live = self.run_live(state)
            self.assertEqual(live["evidence_label"], "live-read")
            self.assertIs(live["frozen"]["reused"], False)  # its own live/ directory
            self.assertEqual(live["frozen"]["source"], "live")
            self.assertTrue((state / "live" / dev.HELDOUT_RUN_ID / "frozen.json").exists())
            frozen = json.loads((state / "live" / dev.HELDOUT_RUN_ID / "frozen.json")
                                .read_text("utf-8"))
            self.assertEqual((frozen["source"], frozen["evidence_label"], frozen["id_suffix"]),
                             ("live", "live-read", "live"))

    def test_reuse_with_another_origin_is_refused(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertEqual(run_heldout(state)[0], 0)
            shutil.copytree(state / "fixture", state / "live")  # a misplaced fixture set
            code, refused = self.run_live(state)
            self.assertEqual(code, 3)
            self.assertEqual(refused["refused"], "heldout_frozen_source_mismatch")
            self.assertEqual((refused["frozen"]["frozen_source"],
                              refused["frozen"]["frozen_evidence_label"]),
                             ("fixture", "injected-transport"))
            self.assertNotIn("counts", refused)
            self.assertNotIn("FX", json.dumps(refused))

    def test_reuse_recomputes_root_resolves_and_fails_on_label_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertEqual(run_heldout(state)[0], 0)
            world = dev.eval_world()
            world.relations["public.app_sales_order"] = []  # every SO root is gone today
            with mock.patch.object(dev, "eval_world", lambda: world):
                code, text = run_heldout(state)
            again = json.loads(text)
            self.assertEqual(code, 3)
            self.assertIs(again["frozen"]["reused"], True)
            so_rooted = sum(1 for i in self.items()
                            if i["root"]["relation"] == "public.app_sales_order")
            self.assertGreater(so_rooted, 12)
            self.assertLessEqual(again["counts"]["root_resolves"], len(self.items()) - so_rooted)
            self.assertGreater(again["counts"]["label_drift"], 12)

    def outage_stub(self):
        """The fixture world's BC and documents with the Portal answering 500."""
        from integrations.http import SourceHttpError

        def portal_500(name, columns):
            raise SourceHttpError("read-only source GET failed: HTTPError", status=500)

        def build(env, args, *, guard):
            world = dev.eval_world()
            return (Sources(PortalReads(portal_500), BcReads(world.bc_port())),
                    world.documents(), {"portal": "live-read", "bc": "injected-transport"})
        return mock.patch.object(dev, "build_live", build)

    def run_outage(self, state: Path, *extra: str) -> tuple[int, dict]:
        stdout = io.StringIO()
        with self.outage_stub(), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            code = dev.main(["heldout", "--state-dir", str(state), *extra], environ={
                "BC_TENANT_ID": "t", "BC_CLIENT_ID": "c", "BC_CLIENT_SECRET": "s",
                "BC_ENVIRONMENT": "Production"})   # no Portal credentials: optional now
        return code, json.loads(stdout.getvalue())

    def test_a_portal_outage_blocks_parts_measures_the_rest_and_never_freezes(self):
        spec = json.loads((ROOT / "verify" / "live-checks" / "eval.heldout.json")
                          .read_text(encoding="utf-8"))
        self.assertNotIn("requires", spec)
        # A fixture draw that holds a BC-rooted variant whose failed hop is a Portal one
        # (the draw is seeded by the run id; the default's draw holds none).
        draw = ("--run-id", "heldout-v1")
        with tempfile.TemporaryDirectory() as tmp:
            code, fresh = self.run_outage(Path(tmp), *draw)
            self.assertFalse((Path(tmp) / "live" / draw[1] / "frozen.json").exists())
            self.assertEqual(self.run_live(Path(tmp), None, *draw)[0], 0)   # a clean run freezes
            code_again, reused = self.run_outage(Path(tmp), *draw)
            # The same set without the labels that read BC alone: nothing left to measure.
            set_dir = Path(tmp) / "live" / draw[1]
            frozen = []
            for path in sorted((set_dir / "items").glob("ho-*.json")):
                frozen.append(json.loads(path.read_text(encoding="utf-8")))
                if not dev.needs_portal(frozen[-1]):
                    path.unlink()
            record = json.loads((set_dir / "frozen.json").read_text(encoding="utf-8"))
            hashes = sorted(dev.content_hash(i) for i in frozen if dev.needs_portal(i))
            kept = {**record["report"]["counts"], "items": len(hashes),
                    "deterministic": len(hashes)}
            (set_dir / "frozen.json").write_text(json.dumps(
                {**record, "item_sha256": hashes, "set_sha256": digest(hashes),
                 "report": {**record["report"], "counts": kept}}), encoding="utf-8")
            code_whole, whole = self.run_outage(Path(tmp), *draw)
        for code, manifest in ((code, fresh), (code_again, reused), (code_whole, whole)):
            self.assertEqual(code, 0, manifest)
            parts = live_verify.blocked_parts(manifest)
            self.assertTrue(parts)
            self.assertEqual({p["source"] for p in parts}, {"portal-workbench-api"})
            self.assertTrue(live_verify.check_expectations(spec, 0, manifest))  # never a pass
        # Fresh: the BC stratum measured (blocked in part); the set waits for the Portal.
        self.assertGreaterEqual(fresh["counts"]["measured"], 1)
        self.assertIsNone(fresh["outage_unmeasured"])
        self.assertEqual(live_verify.check_expectations(spec, 0, fresh,
                                                        spec["expect_blocked"]), [])
        self.assertEqual(fresh["frozen"]["deferred"], "blocked_upstream")
        # Reuse: a frozen label that reads the Portal is blocked today (never label drift);
        # one that reads BC alone (a BC-rooted item whose variant failed its Portal hop)
        # still measures, so the check is blocked in part.
        counts = reused["counts"]
        bc_only = [i for i in frozen if not dev.needs_portal(i)]
        self.assertTrue(bc_only)
        self.assertEqual({i["perturbation"]["params"]["source"] in RELATIONS
                          for i in bc_only}, {True})
        self.assertIs(reused["frozen"]["reused"], True)
        self.assertEqual((counts["label_drift"], counts["measured"], counts["blocked_upstream"]),
                         (0, len(bc_only), counts["items"] - len(bc_only)))
        self.assertEqual(live_verify.check_expectations(spec, 0, reused,
                                                        spec["expect_blocked"]), [])
        # Every frozen label reads the Portal: nothing measured, blocked whole (blocked_when).
        counts = whole["counts"]
        self.assertIs(whole["frozen"]["reused"], True)
        self.assertEqual((counts["label_drift"], counts["measured"], counts["blocked_upstream"]),
                         (0, 0, counts["items"]))
        self.assertEqual(live_verify.resolve(whole, spec["blocked_when"]), ["portal_outage"])

    def test_live_dev_runs_make_disjointness_real_and_related_groups_leave_whole(self):
        from evals.partition import key_digests, related
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_heldout(Path(tmp) / "f")[0], 0)
            drawn = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(
                (Path(tmp) / "f" / "fixture" / dev.HELDOUT_RUN_ID / "items").glob("ho-*.json"))]
            drawn = [i for i in drawn if i["variant"] == "standard"]
            group = max((sorted(related(drawn, key_digests(i))) for i in drawn), key=len)
            self.assertGreater(len(group), 1)   # near-duplicates exist in the draw
            state = Path(tmp) / "s"
            seen = drawn[group[0]]
            # A live dev run that held one member of that group (digests only); its record
            # digests are left out here, so only the partition keys keep the group apart.
            path = dev.write_live_dev_manifest(state, "live-2099-01-02", [seen])
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["key_sha256"], sorted(key_digests(seen)))
            path.write_text(json.dumps({**written, "root_sha256": [],
                                        "chain_records_sha256": []}), encoding="utf-8")
            code, manifest = self.run_live(state)
            self.assertEqual(code, 0, manifest)
            self.assertEqual((manifest["live_dev_manifests"], manifest["disjointness_vacuous"],
                              manifest["dev_manifest_evidence_label"]), (1, False, "live-read"))
            self.assertGreaterEqual(manifest["partition_dropped"]["related_to_other"],
                                    len(group))
            self.assertEqual(manifest["counts"]["dev_key_overlap"], 0)
            held = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(
                (state / "live" / dev.HELDOUT_RUN_ID / "items").glob("ho-*.json"))]
            self.assertEqual(related([*held, seen], key_digests(seen)), {len(held)})
            frozen = json.loads((state / "live" / dev.HELDOUT_RUN_ID / "frozen.json")
                                .read_text(encoding="utf-8"))
            self.assertTrue(frozen["key_sha256"])   # what the dev runs must not hold

    def test_label_view_ignores_only_the_portal_read_generation(self):
        a = {"citations": [{"l1_handle": "api://public.x/%7B%22id%22%3A%221%22%7D@aaaa-9"},
                           {"l1_handle": "bc://production/c/purchaseInvoices/i@W%2F%221%22"}]}
        b = json.loads(json.dumps(a).replace("@aaaa-9", "@bbbb-3"))
        self.assertEqual(dev.label_view(a), dev.label_view(b))
        c = json.loads(json.dumps(a).replace("W%2F%221%22", "W%2F%222%22"))
        self.assertNotEqual(dev.label_view(a), dev.label_view(c))  # a BC record version is

    def test_label_view_leaves_out_only_annotations_the_frozen_label_lacks(self):
        """A set frozen before gaps named their value and verdicts cited their cells does
        not drift by them; a changed state or value still does."""
        frozen = {"answerability": "partial", "values": [{"name": "match_1", "value": True}],
                  "gaps": [{"state": "unknown", "subject": "x", "reason": "r"}]}
        cells = [{"l1_handle": "api://public.x/%7B%7D@aaaa-1", "field_path": "row.quantity"}]
        later = {"answerability": "partial",
                 "values": [{"name": "match_1", "value": True, "citations": cells}],
                 "gaps": [{"state": "unknown", "subject": "x", "reason": "r",
                           "concerns": "match_1"}]}
        self.assertEqual(dev.label_view(later, frozen), dev.label_view(frozen))
        self.assertNotEqual(dev.label_view(later), dev.label_view(frozen))
        for change in ({"values": [{"name": "match_1", "value": False, "citations": cells}]},
                       {"gaps": [{"state": "not_ingested", "subject": "x", "reason": "r",
                                  "concerns": "match_1"}]}):
            self.assertNotEqual(dev.label_view({**later, **change}, frozen),
                                dev.label_view(frozen))

    def test_label_drift_threshold_is_ten_percent(self):
        base = {"counts": {"items": 100, "schema_valid": 100, "deterministic": 100,
                           "dev_root_overlap": 0, "dev_chain_overlap": 0,
                           "partition_violations": 0, "label_drift": 10},
                "budget": {"exhausted": False}, "frozen": {"reused": True}}
        self.assertTrue(dev.heldout_ok(base))
        base["counts"]["label_drift"] = 11
        self.assertFalse(dev.heldout_ok(base))

    def test_disjointness_against_a_fixture_dev_manifest_is_flagged_vacuous_on_live(self):
        self.assertEqual(self.manifest["dev_manifest_evidence_label"], "injected-transport")
        self.assertIs(self.manifest["disjointness_vacuous"], False)  # same fixture world
        with tempfile.TemporaryDirectory() as tmp:
            _, live = self.run_live(Path(tmp))
        self.assertIs(live["disjointness_vacuous"], True)
        self.assertEqual(live["dev_manifest_evidence_label"], "injected-transport")

    def test_committed_fixture_manifest_matches_a_fresh_fixture_run(self):
        committed = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))

        def comparable(manifest: dict) -> dict:  # the oracle code digest moves with edits
            return {k: v for k, v in manifest.items() if k != "oracle"}

        self.assertEqual(comparable(committed), comparable(self.manifest))
        self.assertEqual(committed["evidence_label"], "injected-transport")

    def test_git_holds_no_held_out_item(self):
        self.assertEqual(sorted(p.name for p in HELDOUT.iterdir()),
                         [".gitignore", "manifest.fixture.json"])
        stray = [p for p in ROOT.rglob("ho-F*") if ".git" not in p.parts]
        self.assertEqual(stray, [])
        self.assertIn("*", (HELDOUT / ".gitignore").read_text(encoding="utf-8").split())

    def test_state_dir_inside_the_repository_is_refused(self):
        target = ROOT / "evals" / "heldout" / "not-here"
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = dev.main(["heldout", "--source", "fixture", "--state-dir", str(target)],
                            environ={})
        self.assertEqual(code, 1)
        self.assertFalse(target.exists())
        self.assertEqual(json.loads(err.getvalue()), {"error": "ValueError"})

    def test_default_state_dir_is_the_host_state_dir(self):
        self.assertEqual(dev.default_state_dir({"PLX_EVAL_HELDOUT_DIR": "/srv/x"}),
                         Path("/srv/x"))
        self.assertEqual(dev.default_state_dir({"XDG_STATE_HOME": "/s"}),
                         Path("/s/plx-workbench/eval-heldout"))
        self.assertEqual(dev.default_state_dir({"HOME": "/h"}),
                         Path("/h/.local/state/plx-workbench/eval-heldout"))

    def test_held_out_dir_name_wins_over_the_legacy_name(self):
        both = {"PLX_EVAL_HELD_OUT_DIR": "/srv/new", "PLX_EVAL_HELDOUT_DIR": "/srv/old"}
        self.assertEqual(dev.default_state_dir(both), Path("/srv/new"))
        self.assertEqual(dev.default_state_dir({"PLX_EVAL_HELD_OUT_DIR": "/srv/new"}),
                         Path("/srv/new"))

    def test_frozen_set_at_the_legacy_location_is_adopted_not_resampled(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp) / "old", Path(tmp) / "new"
            _, text = run_heldout(old)
            frozen = json.loads(text)["set_sha256"]
            stdout = io.StringIO()
            env = {"PLX_EVAL_HELD_OUT_DIR": str(new), "PLX_EVAL_HELDOUT_DIR": str(old)}
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                code = dev.main(["heldout", "--source", "fixture"], environ=env)
            again = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertIs(again["frozen"]["reused"], True)
            self.assertEqual(again["set_sha256"], frozen)
            record = new / "fixture" / dev.HELDOUT_RUN_ID / "frozen.json"
            self.assertEqual(json.loads(record.read_text("utf-8"))["set_sha256"], frozen)

    def test_live_without_credentials_fails_closed(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = dev.main(["heldout", "--state-dir", tmp, "--no-bc"], environ={})
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")

    def test_missing_dev_manifest_is_an_error_not_an_empty_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = run_heldout(Path(tmp) / "o", "--dev-manifest",
                                  str(Path(tmp) / "absent.json"))
        self.assertEqual(code, 1)

    def test_run_id_must_be_a_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_heldout(Path(tmp), "--run-id", "../x")[0], 1)

    def test_deadline_stops_reads(self):
        now = [0.0]
        deadline = dev.Deadline(5, monotonic=lambda: now[0])
        deadline.guard()
        now[0] = 5.0
        with self.assertRaises(dev.BudgetExhausted):
            deadline.guard()
        self.assertTrue(deadline.hit)

    def test_live_spec_is_daily_bounded_and_records_no_values(self):
        path = ROOT / "verify" / "live-checks" / "eval.heldout.json"
        spec = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(live_verify.validate_spec(spec, stem=path.stem), [])
        self.assertEqual(spec["cadence"], "daily")
        self.assertIn("scripts.eval_oracle_dev", live_verify.ALLOWED_MODULES)
        command = spec["command"]
        self.assertEqual(command[3], "heldout")
        self.assertNotIn("--output-dir", command)  # items persist in the host state dir
        budget = float(command[command.index("--budget-seconds") + 1])
        self.assertLess(budget, spec["timeout_seconds"])
        recorded = spec["record"] + spec["record_enums"] + spec["record_keys"]
        self.assertFalse(any("known_outcome" in p or "values" in p.split(".") or
                             "question" in p or "root" in p.split(".") for p in recorded))
        live = {**self.manifest, "evidence_label": "live-read"}
        self.assertEqual(live_verify.check_expectations(spec, 0, live), [])
        bad = {**live, "counts": {**live["counts"], "dev_chain_overlap": 1}}
        self.assertTrue(live_verify.check_expectations(spec, 0, bad))

    def test_no_plan_exists_for_the_held_out_families(self):
        plans = ROOT / "ontology" / "plans"
        names = {"F5", "F6", "f5", "f6", "unmatched_invoices", "shipped_short",
                 "f5_unmatched_invoices", "f6_shipped_short"}
        if plans.is_dir():
            self.assertFalse({p.stem for p in plans.glob("*.json")} & names)


if __name__ == "__main__":
    unittest.main()
