"""The ``workspace.object_view`` and ``session.replay`` live-check specs run over the fixture
world (``ObjectViewLiveCheckTest``, ``SessionReplayLiveCheckTest``) and a session's BC
capture reuse.

Split from ``tests/test_object_workspace_http.py`` (NW-16) so each part fits the per-module
limit. Evidence label: injected-transport (the Portal, BC and Graph transports are
injected), isolated-fixture; no network, nothing live.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
from operator_api.workspace import object_workspace as objects


# the SharePoint scope workspace.object_view captures (ontology.document_links' own)
SHAREPOINT_SCOPE = "examples/sharepoint_capture_scope.qcqa_aos.live-check.json"


# What workspace.object_view's expect may miss on the fixture world only: its live size
# floors and the live-read labels of injected transports.
FIXTURE_MISSES = frozenset({"roots_opened < 10", "denied_probed < 10",
                            "bc_capture.evidence_label != expected",
                            "sharepoint_capture.evidence_label != expected"})


# A view no captured SharePoint document reached: its SharePoint input is unproven.
NO_SHAREPOINT_INPUT = frozenset({"documents_linked_roots_sharepoint < 1",
                                 "sharepoint_withheld_read < 1"})


def sharepoint_scope_drive(files):
    """A FakeDrive (integrations.tests.test_capture_sharepoint) holding the folder
    ``SHAREPOINT_SCOPE`` captures with one synthetic text file per ``files``, and the
    scope's drive id; serve it with ``tc.SITE_DRIVE`` patched to that id."""
    from tests import test_live_wiring as wiring
    from integrations.tests import test_capture_sharepoint as tc

    target = json.loads((wiring.ROOT / SHAREPOINT_SCOPE).read_text("utf-8"))["targets"][0]
    drive_id = target["drive_id"]
    with mock.patch.object(tc, "SITE_DRIVE", drive_id):   # FakeDrive serves it
        drive = tc.FakeDrive()
        parent = tc.ROOT_ID
        for number, name in enumerate(target["folders"][0].strip("/").split("/"), 1):
            drive.folder(f"01DIR{number}", name, parent=parent, drive=drive_id)
            parent = f"01DIR{number}"
        for number, text in enumerate(files, 1):
            drive.file(f"01DOC{number}", f"packet-{number}.txt", parent,
                       text.encode("utf-8"), mime="text/plain", drive=drive_id)
    return drive, drive_id


def sharepoint_request(url: str) -> bool:
    """Whether a Graph request is the SharePoint capture's (its drive, site or download
    host), not the mail capture's."""
    from urllib.parse import urlsplit

    from integrations.tests import test_capture_sharepoint as tc

    parts = urlsplit(url)
    return parts.hostname == tc.SP_HOST or parts.path.startswith(
        ("/v1.0/drives/", "/v1.0/sites/"))


class ObjectViewLiveCheckTest(unittest.TestCase):
    """live check ``workspace.object_view``: the spec's own argv over the
    fixture world with the Portal, BC and Graph transports injected,
    its output checked by the verifier's own
    ``check_expectations``. Evidence: injected-transport."""

    # the second names a customer PO the spec's sample opens, so its related-mail panel
    # opens one
    MAIL_BODIES = ("Re: customer PO FXCPO-001, please confirm.",
                   "Customer PO FXCPO-SHARED-1 ships Friday.")
    # one synthetic text file per entry, filed in the scope's folder; naming no record
    FILES = ("Assembly packet scan, page 1.",)

    @classmethod
    @contextlib.contextmanager
    def fixture_sources(cls, spec_doc, *, bodies=MAIL_BODIES, lookup_refused=False,
                        files=FILES):
        """The fixture Portal, Graph mail and SharePoint transports, run from the repo
        root (the spec names its scope files relative to it); yields the environment to
        pass. The allowlisted mailbox holds one message per ``bodies``; ``lookup_refused``:
        every mailbox's directory lookup answers 403 (an app without User.Read.All). The
        SharePoint scope's drive holds its folder with one text file per ``files``."""
        import os

        from tests import test_live_wiring as wiring
        from integrations import graph_read
        from integrations.live_source_guard import BoundedBackoff
        from integrations.tests import test_capture_sharepoint as tc
        from integrations.tests.test_capture_mail import BOX_A, FakeGraph

        drive, drive_id = sharepoint_scope_drive(files)

        command = spec_doc["command"]
        scope = json.loads((wiring.ROOT / command[command.index("--mail-capture-scope") + 1])
                           .read_text("utf-8"))
        address = scope["mailboxes"][0]["address"]
        graph = FakeGraph()   # mailbox A serves as the spec's allowlisted mailbox
        graph.users[BOX_A].update(userPrincipalName=address, mail=address,
                                  proxyAddresses=[f"SMTP:{address}"])
        graph.folders[BOX_A]["sentitems"] = "AAMkFOLDER-SENT="
        graph.received = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        for number, body in enumerate(bodies, 1):
            graph.put(BOX_A, f"MSG-{number}", f"CK-{number}", body=body)
        if lookup_refused:
            graph.directory_forbidden = {"*"}
        real_build = graph_read.build_graph

        def sender(method, url, headers, body, timeout, max_bytes):
            return (drive if sharepoint_request(url) else graph)(
                method, url, headers, body, timeout, max_bytes)

        def build(environ=None, **_kw):
            # The served host reads offline: no Graph credential (its people exclusion only).
            if environ is not None and not environ.get("MICROSOFT_GRAPH_CLIENT_ID"):
                return real_build(environ)
            return real_build(tc.ENV, sender=sender,
                              retry=BoundedBackoff(sleep=lambda s: None, jitter=lambda: 0.0))

        cwd = os.getcwd()
        with wiring.fixture_portal(), mock.patch.object(graph_read, "build_graph", build), \
                mock.patch.object(tc, "SITE_DRIVE", drive_id):
            os.chdir(wiring.ROOT)
            try:
                yield {**wiring.PORTAL_ENV, **wiring.BC_ENV, **tc.ENV}
            finally:
                os.chdir(cwd)

    def test_spec_runs_end_to_end_and_records_panel_denominators_only(self):
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from scripts import object_workspace_probe as probe

        spec_doc = wiring.spec("workspace.object_view")
        fake, books = wiring.fixture_bc()
        with tempfile.TemporaryDirectory() as tmp, self.fixture_sources(spec_doc) as environ:
            code, document = wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                             environ=environ, bc_sender=fake, bc_books=books)
            # A served row whose content no longer hashes to its citation is caught.
            from scripts import ontology_projection_probe as projection
            store = f"{tmp}/ontology.sqlite"
            with sqlite3.connect(objects.mirror_path(projection.served_state_path(store))) as conn:
                conn.execute("UPDATE row_current SET row_json = json_set(row_json, "
                             "'$.updated_at', 'tampered')")
            conn.close()
            # A run whose roots yield no identifier probes nobody for Find: never a pass.
            with mock.patch.object(probe, "identifier_queries", lambda *a: []):
                tampered = probe.check_views(projection.served_host(store, environ), sample=40,
                                             per_type=4, max_opens=5, budget_seconds=None)
            # A "no-grant" reader holding the capture principal finds every root (counted
            # leaks), and a next step with no why or no citation is counted unexplained.
            host = projection.served_host(store, environ)
            offered = {"status": "read", "intents": [
                {"capability": objects.FOLLOW_UP_CAPABILITY, "cited_records": ["r"],
                 "next_safe_step": {"detail": "a person approves the exact call"}},
                {"capability": "x.y", "cited_records": ["r"], "next_safe_step": {}},
                {"capability": "x.y", "cited_records": [],
                 "next_safe_step": {"detail": "a person approves"}}]}
            listed = {"status": "read", "cases_listing_full": False, "cases": [
                {"evaluation_status": "current"}, {"evaluation_status": "withheld"}]}
            with mock.patch.object(probe, "DENIED_PRINCIPAL", host.config.principal), \
                    mock.patch.object(objects.ObjectWorkspace, "_intents",
                                      lambda self, records: offered), \
                    mock.patch.object(objects.ObjectWorkspace, "_cases",
                                      lambda self, records: listed):
                leaky = probe.check_views(host, sample=40, per_type=4, max_opens=0,
                                          budget_seconds=None)
        self.assertEqual(code, 0, document)
        # Beside the live size floors and labels, only the SharePoint input may miss: the
        # mail names a record and the captured file none, so no SharePoint-linked root and
        # the run fails, never passes on the mail-linked root alone.
        failures = lv.check_expectations(spec_doc, code, document)
        self.assertLessEqual(set(failures), FIXTURE_MISSES | NO_SHAREPOINT_INPUT, failures)
        self.assertLessEqual(NO_SHAREPOINT_INPUT, set(failures), failures)
        self.assertEqual(document["sources"], ["bc", "graph_mail", "portal_api", "sharepoint"])
        self.assertGreaterEqual(document["documents_linked_roots"], 1)
        self.assertEqual((document["documents_linked_roots_sharepoint"],
                          document["sharepoint_withheld_probed"]), (0, 0))
        self.assertGreater(document["roots_opened"], 1)
        self.assertEqual(document["denied_not_found"], document["denied_probed"])
        self.assertEqual(document["denied_probed"], document["roots_opened"])
        # Every panel was measured, and an opened Portal row re-hashes to its citation.
        originals = document["originals"]
        self.assertGreater(originals["opened"], 0)
        self.assertEqual(originals["rehash_mismatch"], 0)
        self.assertGreater(originals["rehash_matched"], 0)
        self.assertEqual(tampered["originals"]["rehash_matched"], 0)
        # Every opened row that can re-hash (not one opened without its withheld fields)
        # now mismatches; none of the opened rows is left unaccounted for.
        flat = tampered["originals"]
        self.assertGreater(flat["rehash_mismatch"], 0)
        self.assertEqual(flat["rehash_mismatch"] + flat["rehash_not_applicable"], flat["opened"])
        self.assertEqual((tampered["find_denied_probed"], tampered["find_denied_leaks"]), (0, 0))
        self.assertIn("find_denied_probed < 1", lv.check_expectations(
            spec_doc, 0, {**document, **tampered}))
        # A Portal outage drops the find floor for Portal-bound roots only: with a BC root
        # searched as the no-grant reader it is blocked (its parts named), not fail; with
        # only Portal-bound roots searched the floor over the sources still up fails.
        by_source = document["find_denied_probed_by_source"]
        self.assertEqual(sum(by_source.values()), document["find_denied_probed"])
        self.assertEqual(document["find_denied_probed_unblocked"],
                         document["find_denied_probed"])
        self.assertTrue(by_source.get("portal_api") and by_source.get("bc"), by_source)
        portal_down = [{"part": "portal_sync", "source": "portal-workbench-api",
                        "http_status": 503, "error_code": "unavailable"}]
        for searched, failures in (({"bc": 1}, []), (
                {"portal_api": by_source["portal_api"]}, ["find_denied_probed_unblocked < 1"])):
            outage = {**document, "find_by_type": tampered["find_by_type"],
                      "find_denied_probed": sum(searched.values()),
                      "find_denied_probed_by_source": searched, "blocked_upstream": portal_down}
            outage["find_denied_probed_unblocked"] = probe.denied_probed_unblocked(outage)
            self.assertEqual([p["cause"] for p in lv.blocked_parts(outage)],
                             ["upstream_outage"])
            self.assertEqual(lv.check_expectations(spec_doc, 0, outage,
                                                   spec_doc["expect_blocked"]), failures)
        # A part naming a source the probe does not project from drops no root's floor.
        self.assertEqual(probe.denied_probed_unblocked(
            {"find_denied_probed_by_source": {"portal_api": 2, "unknown": 1},
             "blocked_upstream": [{"source": "elsewhere"}, "portal-workbench-api"]}), 3)
        for path in ("timeline.events", "relationships.edges", "values.quantity",
                     "next_steps.stages"):
            self.assertGreater(lv.resolve(document, path)[0], 0, path)
        self.assertIn("panel_read", document["documents"])
        # The spec asserts each WS panel non-empty on a root, and the record keeps them.
        asserted = {path.split(".")[1] for path in spec_doc["expect"]["json_min"]
                    if path.startswith("panels.")}
        self.assertEqual(asserted, set(probe.PANEL_COUNTS) - {
            "markers_conflicts", "markers_leads", "markers_withheld"})
        self.assertEqual(set(document["panels"]), set(probe.PANEL_COUNTS))
        for panel in sorted(asserted):
            self.assertGreater(document["panels"][panel]["roots"], 0, panel)
            self.assertIn(f"panels.{panel}.roots < 1", lv.check_expectations(
                spec_doc, 0, {**document, "panels": {
                    **document["panels"], panel: {"items": 0, "roots": 0}}}))
        self.assertEqual(lv._recorded(spec_doc, document)["panels"], document["panels"])
        # The capped record keeps every asserted path; the per-type find counts it may not
        # fit are summed in find_totals, which it keeps.
        self.assertEqual(document["find_totals"], {key: sum(
            t[key] for t in document["find_by_type"].values()) for key in probe.FIND_COUNTS})
        recorded = lv._recorded(spec_doc, document)
        kept = lv._cap({"fields": recorded}, spec_doc)["fields"]
        self.assertGreater(len(json.dumps(recorded)), lv.MAX_DETAIL_CHARS)
        expect = spec_doc["expect"]
        for path in expect["json_fields"]:
            self.assertTrue(lv.resolve(kept, path), path)
        for path in [*expect["json_min"], *expect["json_equals"]]:
            self.assertEqual(lv.resolve(kept, path), lv.resolve(recorded, path), path)
        # Find & open: a root is found at rank 1 by its own identifier, and the reader with
        # no grant gets no candidate for any root's identifiers.
        found = document["find_by_type"]
        self.assertGreater(sum(t["found_top1"] for t in found.values()), 0, found)
        self.assertEqual(sum(t["searched"] for t in found.values()),
                         document["find_denied_probed"])
        self.assertGreater(document["find_denied_probed"], 0)
        self.assertEqual(document["find_denied_leaks"], 0)
        # A root with no identifier to search by is explained: its type declares no own
        # identifier property, its card gaps the value as not a typed identifier of its
        # kind, as a value the projection could not parse (unparsed) or left out for a
        # cause it cannot tell (unread), or the record holds no value (no_value).
        own = probe.own_identifiers()
        for object_type, counts in found.items():
            self.assertEqual(sum(counts[k] for k in (
                "searched", "no_identifier_property", "identifier_invalid", "unparsed",
                "unread", "no_value")),
                             document["roots_by_type"][object_type], object_type)
            self.assertEqual(counts["no_identifier_property"] > 0, not own[object_type],
                             object_type)
        for key in ("identifier_invalid", "unparsed", "no_value"):
            self.assertGreater(sum(t[key] for t in found.values()), 0, (key, found))
        # With no action store the next steps are unmeasured, never counted as 0 explained.
        self.assertEqual(document["next_steps_explained"], {
            "status": "unmeasured", "reason": "intents_not_configured",
            "roots": document["roots_opened"]})
        self.assertGreater(leaky["find_denied_probed"], 0)
        self.assertEqual(leaky["find_denied_leaks"], leaky["find_denied_probed"])
        opened = leaky["roots_opened"]
        self.assertEqual(leaky["next_steps_explained"], {
            "status": "measured", "follow_ups_offered": opened, "follow_ups_explained": opened,
            "follow_ups_unexplained": 0, "next_steps_offered": 2 * opened,
            "next_steps_explained": 0, "next_steps_unexplained": 2 * opened})
        # Each open's case panel is read; every listed case counts under one evaluation.
        evaluations = document["case_evaluations"]
        self.assertEqual(sum(evaluations[state] for state in objects.EVALUATION_STATES),
                         evaluations["cases"], evaluations)
        self.assertFalse([key for key in evaluations if key.startswith("panel_")])
        self.assertEqual(leaky["case_evaluations"], {
            **dict.fromkeys(objects.EVALUATION_STATES, 0), "cases": 2 * opened,
            "roots_with_cases": opened, "listing_full": 0, "current": opened,
            "withheld": opened})
        # Counts and names only: nothing the verifier records names a record.
        fields = lv._recorded(spec_doc, document)
        self.assertEqual(fields["next_steps_explained"]["reason"], "intents_not_configured")
        # The verifier requires and records the evaluation status counts.
        self.assertEqual(fields["case_evaluations"], evaluations)
        self.assertIn("missing case_evaluations", lv.check_expectations(
            spec_doc, 0, {key: value for key, value in document.items()
                          if key != "case_evaluations"}))
        recorded = json.dumps(fields)
        for value in ("PO92", "PO#9", "FXBC", "FXPI", "FXCPO", "fx-po-", "api://", "bc://",
                      "graph-mail://", "petrasoap", ":portal."):
            self.assertNotIn(value, recorded)

    def test_a_captured_sharepoint_file_naming_a_record_fills_the_documents_panel(self):
        """The spec captures the bounded SharePoint scope ontology.document_links reads and
        projects it with the same document steps, laid out in the served state: with mail
        naming no record, a captured file naming a customer PO links it, the sampled root
        opens its documents panel and the run passes expect (no blocked part). The no-grant
        reader is served none of the files (its capture read-back and every open)."""
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from integrations import capture_sharepoint
        from scripts import object_workspace_probe as probe
        from scripts import ontology_projection_probe as projection

        spec_doc = wiring.spec("workspace.object_view")
        fake, books = wiring.fixture_bc()
        with tempfile.TemporaryDirectory() as tmp, self.fixture_sources(
                spec_doc, bodies=("Thanks, see you on Friday.",),
                files=("Customer PO FXCPO-SHARED-1 ships Friday.",)) as environ:
            code, document = wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                             environ=environ, bc_sender=fake, bc_books=books)
            served = projection.served_state_path(f"{tmp}/ontology.sqlite")
            held = capture_sharepoint.store_exists(served)
            # A "records only" reader that holds the folder (the capture principal) is
            # named and opened the SharePoint documents: counted leaks.
            host = projection.served_host(f"{tmp}/ontology.sqlite", environ)
            with mock.patch.object(projection, "MAIL_WITHHELD_PRINCIPAL",
                                   host.config.principal):
                leaky = probe.check_views(host, sample=40, per_type=4, max_opens=5,
                                          budget_seconds=None)
        self.assertEqual(code, 0, document)
        self.assertGreaterEqual(document["mention_links"], 1)   # the file's, not the mail's
        self.assertGreaterEqual(document["documents_linked_roots"], 1, document)
        # the mail names no record: every linked root is SharePoint's
        self.assertEqual(document["documents_linked_roots_sharepoint"],
                         document["documents_linked_roots"])
        self.assertGreaterEqual(document["panels"]["documents_opened"]["roots"], 1)
        self.assertEqual(lv.blocked_parts(document), [])
        self.assertNotIn("partial_upstream", document)   # a complete capture
        # timing the first live run measures: the capture's and the build's seconds
        self.assertIsInstance(document["sharepoint_capture"]["seconds"], float)
        self.assertIsInstance(document["build_seconds"], float)
        failures = lv.check_expectations(spec_doc, code, document)
        self.assertLessEqual(set(failures), FIXTURE_MISSES, failures)
        # A reader granted the records (BC by the live audience, the Portal by the served
        # mirror audience) but not the SharePoint folder reads each SharePoint-linked root
        # and is named or opened none of its SharePoint documents.
        self.assertEqual(document["sharepoint_withheld_probed"],
                         document["documents_linked_roots_sharepoint"])
        self.assertEqual((document["sharepoint_withheld_read"],
                          document["sharepoint_withheld_leaks"]),
                         (document["sharepoint_withheld_probed"], 0), document)
        self.assertGreater(leaky["sharepoint_withheld_leaks"], 0, leaky)
        self.assertIn("sharepoint_withheld_leaks != expected",
                      lv.check_expectations(spec_doc, 0, {**document, **leaky}))
        self.assertTrue(held)   # where the served host and the probe's views read it
        self.assertEqual(document["sources"], ["bc", "graph_mail", "portal_api", "sharepoint"])
        capture = document["sharepoint_capture"]
        self.assertEqual((capture["evidence_label"], capture["complete"],
                          capture["files_current"], capture["read_back_leaks"]),
                         ("injected-transport", True, 1, 0), capture)
        self.assertEqual(document["denied_leaks"], 0)
        self.assertEqual(document["denied_not_found"], document["denied_probed"])
        self.assertNotIn("FXCPO", json.dumps(lv._recorded(spec_doc, document)))

    def test_sharepoint_capture_fails_closed_and_is_never_served_stale(self):
        """Each SharePoint capture outcome over the spec's own argv: not configured (no
        SHAREPOINT_SITE_ID) reads nothing; one that raises is a part naming its class, its
        message never printed; incomplete with no file is a part that drops no mail or
        record source's find floor; a read-back leak exits 1 whatever expect says;
        incomplete holding files is served and marked partial_upstream, never blocking; an
        entry at served-state/sharepoint-capture the probe did not link is refused and
        left alone; and a later run without SharePoint serves no earlier run's capture."""
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from integrations import capture_sharepoint
        from scripts import object_workspace_probe as probe
        from scripts import ontology_projection_probe as projection

        spec_doc = wiring.spec("workspace.object_view")
        real_cli = capture_sharepoint.run_cli
        secret = "synthetic-capture-message-7f3a"

        def edited(edit):
            def run_cli(*args, **kwargs):
                report = real_cli(*args, **kwargs)
                edit(report)
                return report
            return run_cli

        def incomplete(report, files=None):
            report["runs"][-1].update(complete=False, incomplete_reasons=["time_budget"])
            if files is not None:
                report["summary"]["files_current"] = files

        def raises(*_args, **_kwargs):
            raise RuntimeError(secret)

        def leaked(report):
            report["leak_check"] = {**report["leak_check"], "synthetic": 1}

        def strip_sharepoint(argv):
            kept, skip = [], False
            for arg in argv:
                if skip:
                    skip = False
                elif arg.startswith("--sharepoint-"):
                    skip = True
                else:
                    kept.append(arg)
            return kept

        def run(*, no_site=False, cli=None, occupied=False, again_without=False):
            fake, books = wiring.fixture_bc()
            calls = mock.Mock(wraps=cli or real_cli)
            with tempfile.TemporaryDirectory() as tmp, \
                    self.fixture_sources(spec_doc) as environ, \
                    mock.patch.object(capture_sharepoint, "run_cli", calls):
                if no_site:
                    environ = {k: v for k, v in environ.items() if k != "SHAREPOINT_SITE_ID"}
                served = projection.served_state_path(f"{tmp}/ontology.sqlite")
                entry = Path(served, capture_sharepoint.STORE_DIR)
                if occupied:   # a directory nobody linked: never served, never removed
                    entry.mkdir(parents=True)
                    (entry / "kept.txt").write_text("synthetic", "utf-8")
                argv = wiring.argv_of(spec_doc, tmp)
                code, document = wiring.run_main(probe.main, argv, environ=environ,
                                                 bc_sender=fake, bc_books=books)
                if again_without:
                    code, document = wiring.run_main(
                        probe.main, strip_sharepoint(argv), environ=environ,
                        bc_sender=fake, bc_books=books)
                left = (entry.is_dir() and not entry.is_symlink()
                        and (entry / "kept.txt").is_file())
                return (code, document, calls.call_count, left,
                        capture_sharepoint.store_exists(served))

        def sharepoint_parts(document, key="blocked_upstream"):
            return [part for part in document.get(key) or ()
                    if part.get("part") == "sharepoint_capture"]

        def floors_kept(document):
            return (document["find_denied_probed"] >= 1 and
                    document["find_denied_probed_unblocked"]
                    == document["find_denied_probed"])

        with self.subTest("no SHAREPOINT_SITE_ID: a not_configured part, nothing read"):
            code, document, calls, _, held = run(no_site=True)
            self.assertEqual(code, 0, document)
            self.assertEqual(calls, 0)
            self.assertEqual([p.get("error_code") for p in sharepoint_parts(document)],
                             ["not_configured"])
            self.assertNotIn("sharepoint", document["sources"])
            self.assertTrue(floors_kept(document), document)
            self.assertFalse(held)
        with self.subTest("a capture that raises: a part, its message never printed"):
            code, document, calls, _, held = run(cli=raises)
            self.assertEqual(code, 0, document)
            self.assertEqual(calls, 1)
            self.assertEqual(len(sharepoint_parts(document)), 1, document)
            self.assertNotIn(secret, json.dumps(document))
            self.assertNotIn("sharepoint", document["sources"])
            self.assertTrue(floors_kept(document), document)
            self.assertFalse(held)
        with self.subTest("incomplete with no file: a part, the floors kept"):
            code, document, _, _, held = run(cli=edited(lambda r: incomplete(r, files=0)))
            self.assertEqual(code, 0, document)
            self.assertEqual([p.get("error_code") for p in sharepoint_parts(document)],
                             ["time_budget"])
            self.assertNotIn("sharepoint", document["sources"])
            self.assertTrue(floors_kept(document), document)
            self.assertFalse(held)
        with self.subTest("a read-back leak exits 1"):
            code, document, _, _, _ = run(cli=edited(leaked))
            self.assertEqual(document["sharepoint_capture"]["read_back_leaks"], 1)
            self.assertEqual(code, 1, document)
        with self.subTest("incomplete holding files: served, marked, not blocking"):
            code, document, _, _, held = run(cli=edited(incomplete))
            self.assertEqual(code, 0, document)
            self.assertTrue(held)
            self.assertIn("sharepoint", document["sources"])
            self.assertEqual(sharepoint_parts(document), [])
            self.assertEqual(lv.blocked_parts(document), [])
            self.assertEqual([p.get("error_code") for p in
                              sharepoint_parts(document, "partial_upstream")],
                             ["time_budget"], document)
            failures = lv.check_expectations(spec_doc, code, document)
            # served, but its file names no record: the SharePoint input stays unproven
            self.assertLessEqual(set(failures), FIXTURE_MISSES | NO_SHAREPOINT_INPUT,
                                 failures)
            self.assertIn("partial_upstream", lv._recorded(spec_doc, document))
        with self.subTest("an entry the probe did not link: refused, typed, left alone"):
            code, document, _, left, _ = run(occupied=True)
            self.assertEqual(code, 1, document)
            self.assertEqual((document.get("error"), document.get("build_error"),
                              document.get("code")),
                             ("projection_build_failed", "ServedCaptureConflict",
                              "served_capture_not_a_link"), document)
            self.assertTrue(left)
        with self.subTest("a later run without SharePoint removes the served link"):
            code, document, calls, _, held = run(again_without=True)
            self.assertEqual(code, 0, document)
            self.assertEqual(calls, 1)   # the first run's capture only
            self.assertNotIn("sharepoint", document["sources"])
            self.assertFalse(held)

    def test_each_marker_kind_counts_apart_and_withheld_alone_never_satisfies_the_gap_floor(self):
        """gap-7942ca70a8df: gaps, conflicts, leads and withheld markers are four panels;
        a root showing only a withheld marker leaves markers_gaps empty, so the spec's
        panels.markers_gaps.roots >= 1 fails on it."""
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from scripts import object_workspace_probe as probe

        spec_doc = wiring.spec("workspace.object_view")

        def measure(view):
            counts = {"open_errors": {}, "panels": {
                panel: {"items": 0, "roots": 0} for panel in probe.PANEL_COUNTS}}
            probe.measure_panels(view, mock.Mock(), counts, 5, 0)
            return counts["panels"]
        edge = lambda tier: {"tier": tier}   # noqa: E731
        withheld_only = measure({
            "card": {"withheld": {"total": 1}},
            "originals": {"withheld": {"total": 2}},
            "documents": {"result": {"documents": [{"not_openable_reason": "x"}]}}})
        self.assertEqual({k: v["items"] for k, v in withheld_only.items()
                          if k.startswith("markers_")},
                         {"markers_gaps": 0, "markers_conflicts": 0, "markers_leads": 0,
                          "markers_withheld": 4})
        document = {"panels": withheld_only}
        self.assertIn("panels.markers_gaps.roots < 1",
                      lv.check_expectations(spec_doc, 0, document))
        each = measure({
            "gaps": {"total": 3}, "conflicts": [{}, {}],
            "around": {"hops": [{"result": {"edges": [
                edge("verified"), edge("corroborated"), edge("stored_by_text_join"),
                edge("lead"), edge("contradicted")]}}]}})
        self.assertEqual({k: v["items"] for k, v in each.items() if k.startswith("markers_")},
                         {"markers_gaps": 3, "markers_conflicts": 2, "markers_leads": 2,
                          "markers_withheld": 0})
        self.assertNotIn("panels.markers_gaps.roots < 1", lv.check_expectations(
            spec_doc, 0, {"panels": each}))
        # A link on two hops counts once; two links between the same pair count twice.
        link = lambda link_id, link_type: {   # noqa: E731
            "link_id": link_id, "link_type": link_type, "from": "a", "to": "b", "tier": "lead"}
        hops = measure({"around": {"hops": [
            {"result": {"edges": [link("l1", "billed_to"), link("l2", "same_record_as")]}},
            {"result": {"edges": [link("l1", "billed_to")]}}]}})
        self.assertEqual((hops["markers_leads"]["items"], hops["relationships_tiered"]["items"]),
                         (2, 2))
        self.assertNotIn("markers_shown", probe.PANEL_COUNTS)
        self.assertNotIn("panels.markers_shown.roots", spec_doc["expect"]["json_min"])

    def test_each_panel_counts_on_the_root_that_shows_it_and_a_withheld_root_adds_none(self):
        from tests import test_live_wiring as wiring
        from operating.host import projection_path
        from scripts import object_workspace_probe as probe
        from scripts import ontology_projection_probe as projection

        spec_doc = wiring.spec("workspace.object_view")
        fake, books = wiring.fixture_bc()
        with tempfile.TemporaryDirectory() as tmp, self.fixture_sources(spec_doc) as environ:
            code, document = wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                             environ=environ, bc_sender=fake, bc_books=books)
            store = f"{tmp}/ontology.sqlite"
            served = projection_path(projection.served_host(store, environ).state_dir)
            roots = probe.sample_roots(str(served), sample=40, per_type=4)

            def views(picked):
                with mock.patch.object(probe, "sample_roots", lambda *a, **k: picked):
                    return probe.check_views(projection.served_host(store, environ), sample=40,
                                             per_type=4, max_opens=5, budget_seconds=None)
            # ten roots one at a time, and each customer PO (where the related mail is)
            ten = roots[:10]
            alone = {root: views([root])["panels"] for root in dict.fromkeys(
                [*ten, *(root for root in roots if root[0] == "CustomerPurchaseOrder")])}
            together = views(ten)["panels"]
            one = probe.check_views(projection.served_host(store, environ), sample=1, per_type=1,
                                    max_opens=5, budget_seconds=None)
            linked = probe.document_linked_roots(str(served))
            documented = [root for root, panels in alone.items()
                          if panels["documents_opened"]["roots"]]
            hidden = documented[0]
            with sqlite3.connect(served) as conn:
                conn.execute("UPDATE objects SET audience_key = 'not-granted:panel-probe' "
                             "WHERE object_id = ?", (hidden[1],))
            conn.close()
            withheld, rest = views([hidden]), views(roots)
        self.assertEqual(code, 0, document)
        panels = document["panels"]
        asserted_panels = {path.split(".")[1] for path in spec_doc["expect"]["json_min"]
                           if path.startswith("panels.")}
        self.assertEqual(set(panels), set(probe.PANEL_COUNTS))
        # Each panel is non-empty on some root; ten roots one at a time and at once count
        # the same items and roots (none lost or counted twice), and 40 never more roots
        # than opened.
        for panel in probe.PANEL_COUNTS:
            if panel in asserted_panels or panel.startswith("markers_"):
                self.assertGreater(panels[panel]["roots"], 0, panel)
            self.assertLessEqual(panels[panel]["roots"], document["roots_opened"])
            for key in ("items", "roots"):
                self.assertEqual(sum(alone[root][panel][key] for root in ten),
                                 together[panel][key], (panel, key))
        self.assertEqual(alone[hidden]["documents_opened"], {"items": 1, "roots": 1})
        # gap-07d4b4d2292c: the roots a mail document is linked to are sampled first, each
        # once, and exactly those show the panel; a one-root sample (the hash order over
        # types alone takes a root no document names) still takes one and opens its mail.
        self.assertEqual(roots[:len(linked)], linked)
        self.assertEqual(len(set(roots)), len(roots))
        self.assertEqual(set(documented), set(linked))
        self.assertEqual(document["documents_linked_roots"], len(linked))
        self.assertEqual(panels["documents_opened"]["roots"], len(linked))
        self.assertEqual((one["roots_sampled"], one["documents_linked_roots"]), (1, 1))
        self.assertEqual(one["panels"]["documents_opened"], {"items": 1, "roots": 1})
        # Withheld, that root shows no panel and adds nothing: its mail is opened no more.
        self.assertEqual((withheld["withheld_roots"], withheld["roots_opened"]), (1, 0))
        self.assertEqual(withheld["panels"], {panel: {"items": 0, "roots": 0}
                                              for panel in probe.PANEL_COUNTS})
        others = len(linked) - 1
        self.assertEqual(rest["panels"]["documents_opened"], {"items": others, "roots": others})
        self.assertEqual((rest["withheld_roots"], rest["workspace_errors"]), (1, 0))

    def test_a_bc_outage_still_needs_a_portal_root_searched_as_the_no_grant_reader(self):
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from scripts import object_workspace_probe as probe

        def bc_down(*_):   # every BC request, the token's included, is refused
            raise ConnectionRefusedError("BC is down")
        spec_doc = wiring.spec("workspace.object_view")
        runs = []
        for queries in (probe.identifier_queries, lambda *a: []):
            with tempfile.TemporaryDirectory() as tmp, self.fixture_sources(spec_doc) as env, \
                    mock.patch.object(probe, "identifier_queries", queries):
                runs.append(wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                            environ=env, bc_sender=bc_down))
        (code, searched), (_, unsearched) = runs
        self.assertEqual(code, 0, searched)
        self.assertEqual([(p["part"], p["cause"]) for p in lv.blocked_parts(searched)],
                         [("bc_capture", "upstream_outage")])
        self.assertNotIn("bc", searched["sources"])
        self.assertGreater(searched["find_denied_probed_by_source"].get("portal_api", 0), 0)
        self.assertEqual(lv.check_expectations(spec_doc, 0, searched,
                                               spec_doc["expect_blocked"]), [])
        # No Portal root searched as the no-grant reader: the BC outage keeps that floor.
        self.assertEqual(unsearched["find_denied_probed_unblocked"], 0)
        self.assertEqual(lv.check_expectations(spec_doc, 0, unsearched,
                                               spec_doc["expect_blocked"]),
                         ["find_denied_probed_unblocked < 1"])

    def test_a_portal_outage_is_blocked_and_a_fixture_host_refuses_the_bc_only_store(self):
        """With the Portal not configured, or answering 503, the
        store is projected from BC and mail beside a fresh empty mirror, served with
        portal_api not projected, and the run is blocked by the outage (expect_blocked
        passes), never a fail. A fixture host still refuses that live store."""
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from integrations import workbench_read_api
        from integrations.http import SourceHttpError
        from operating import mcp_server
        from scripts import object_workspace_probe as probe

        real = workbench_read_api.WorkbenchReadApiClient

        def unavailable(url, key, **kwargs):
            def fetch(url, headers, **_):
                raise SourceHttpError("read-only source GET failed: HTTPError", status=503,
                                      error_code="unavailable")
            return real(url, key, fetch=fetch, **kwargs)
        spec_doc = wiring.spec("workspace.object_view")
        for configured in (False, True):
            fake, books = wiring.fixture_bc()
            with self.subTest(portal="503" if configured else "not configured"), \
                    tempfile.TemporaryDirectory() as tmp, \
                    self.fixture_sources(spec_doc) as environ, \
                    mock.patch.object(workbench_read_api, "WorkbenchReadApiClient",
                                      unavailable):
                if not configured:
                    environ = {k: v for k, v in environ.items() if k not in wiring.PORTAL_ENV}
                code, document = wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                                 environ=environ, bc_sender=fake,
                                                 bc_books=books)
                self.assertEqual(code, 0, document)
                parts = [p for p in document["blocked_upstream"] if p["part"] == "portal_mirror"]
                self.assertEqual(len(parts), 1, document["blocked_upstream"])
                if configured:
                    self.assertEqual((parts[0]["http_status"], parts[0]["error_code"]),
                                     (503, "unavailable"))
                    self.assertIn({"part": "portal_mirror", "cause": "upstream_outage"},
                                  [{k: p[k] for k in ("part", "cause")}
                                   for p in lv.blocked_parts(document)])
                self.assertEqual(document["sources"], ["bc", "graph_mail", "sharepoint"])
                self.assertEqual(document["workspace_errors"], 0)
                self.assertGreater(document["roots_opened"], 0)
                self.assertEqual(lv.check_expectations(spec_doc, 0, document,
                                                       spec_doc["expect_blocked"]), [])
                # A fixture host (its own state: no BC capture) refuses the live store.
                errors = io.StringIO()
                with self.assertRaises(SystemExit) as exited, \
                        contextlib.redirect_stderr(errors):
                    mcp_server.session_server([
                        "--state-dir", f"{tmp}/fixture-host", "--source-mirror-audience",
                        str(wiring.ROOT / "examples/source_mirror_audience.example.json"),
                        "--fixture", "--principal", "staff:purchasing",
                        "--ontology-store", f"{tmp}/ontology.sqlite"])
                self.assertEqual(exited.exception.code, 2)
                self.assertIn("ontology store refused: unknown", errors.getvalue())

    def test_no_document_link_never_passes_and_a_refused_mailbox_lookup_reads_blocked(self):
        """gap-d2abdf27e7f4, gap-07d4b4d2292c: the documents panel is asserted only over a
        sampled root a document is linked to. With mail captured but naming no record, no
        root is document-linked: a typed source_gap part, never a pass. With every
        mailbox's directory lookup refused the build's mail.mailbox_lookup part reaches
        the record and the run passes expect_blocked (blocked, never pass)."""
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from scripts import object_workspace_probe as probe

        spec_doc = wiring.spec("workspace.object_view")
        runs = {}
        for name, options in (("unlinked", {"bodies": ("Thanks, see you on Friday.",)}),
                              ("refused", {"lookup_refused": True})):
            fake, books = wiring.fixture_bc()
            with tempfile.TemporaryDirectory() as tmp, \
                    self.fixture_sources(spec_doc, **options) as environ:
                runs[name] = wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                             environ=environ, bc_sender=fake, bc_books=books)
        for name, (code, document) in runs.items():
            with self.subTest(name):
                self.assertEqual(code, 0, document)
                self.assertEqual(document["documents_linked_roots"], 0)
                self.assertEqual(document["panels"]["documents_opened"]["roots"], 0)
                parts = lv.blocked_parts(document)
                self.assertTrue(parts, "no document link must never read pass")
                self.assertEqual(lv.check_expectations(spec_doc, code, document,
                                                       spec_doc["expect_blocked"]), [])
                # the full expect names why, not only the empty panel
                self.assertIn("documents_linked_roots < 1",
                              lv.check_expectations(spec_doc, code, document))
                recorded = lv._recorded(spec_doc, document)
                self.assertEqual(recorded["documents_linked_roots"], 0)
                self.assertIn("mention_links", recorded)
        unlinked, refused = runs["unlinked"][1], runs["refused"][1]
        self.assertEqual([(p["part"], p["source"], p["cause"])
                          for p in lv.blocked_parts(unlinked)],
                         [("documents.linked_roots", "microsoft-graph", "source_gap")])
        self.assertGreater(unlinked["mail_messages_seen"], 0)
        self.assertEqual(unlinked["mention_links"], 0)
        self.assertEqual([(p["part"], p["http_status"], p["cause"])
                          for p in lv.blocked_parts(refused)],
                         [("mail.mailbox_lookup", 403, "host_misconfiguration")])
        self.assertEqual(refused["mail_messages_seen"], 0)
        self.assertIn("forbidden", refused["mail_capture_reasons"])
        # the refusal is no outage of the record sources: their find floor still holds
        self.assertEqual(refused["find_denied_probed_unblocked"],
                         refused["find_denied_probed"])

    def test_an_ambiguous_first_rank_is_recall_at_five_and_counted_apart_from_top1(self):
        from scripts import object_workspace_probe as probe

        class Finder:
            def __init__(self, answers):
                self.answers = answers

            def find(self, query):
                status, ids = self.answers.get(query, ("not_found", []))
                return {"find": {"status": status, "candidates": [
                    {"object": {"object_id": i}} for i in ids]}}

        denied = Finder({})
        report = {"find_by_type": {}, "find_denied_probed": 0, "find_denied_leaks": 0,
                  "workspace_errors": 0}
        granted = Finder({"A": ("ambiguous", ["root-1", "other"]),
                          "B": ("found", ["root-2"]),
                          "C": ("ambiguous", ["root-2", "other"])})
        for root, queries in (("root-1", ["A"]), ("root-2", ["C", "B"])):
            probe.measure_find("T", root, queries, granted, denied, report, {})
        counts = report["find_by_type"]["T"]
        # root-2's unambiguous answer wins over its ambiguous first rank.
        self.assertEqual({k: counts[k] for k in ("searched", "found_top1",
                                                 "found_top1_ambiguous", "found_top5",
                                                 "ambiguous")},
                         {"searched": 2, "found_top1": 1, "found_top1_ambiguous": 1,
                          "found_top5": 2, "ambiguous": 1})
        self.assertEqual(report["find_denied_probed"], 2)


class SessionReplayLiveCheckTest(unittest.TestCase):
    """live check ``session.replay``: the spec's own argv over the fixture
    world with the Portal, BC and Graph transports injected (``ObjectViewLiveCheckTest``'s
    wiring), its printed summary checked by the verifier's own ``check_expectations``; then,
    over the same built state, a cited row changed between record and replay, and a
    "withheld" reader who holds the recorder's grant, each fail it. Evidence:
    injected-transport (the planner is the probe's deterministic one, no model)."""

    @staticmethod
    @contextlib.contextmanager
    def rewriting_reextraction():
        """Re-extraction that leaves every earlier citation resolving to other content."""
        from operating import case_originals

        reextract, resolve = (case_originals.OriginalStore.reextract,
                              case_originals.OriginalStore.resolve)
        done = []

        def reextract_then_rewrite(store, *args, **kwargs):
            done.append(True)
            return reextract(store, *args, **kwargs)

        def resolve_rewritten(store, *args, **kwargs):
            view = resolve(store, *args, **kwargs)
            return {**view, "text": "rewritten"} if done and "text" in view else view

        with mock.patch.object(case_originals.OriginalStore, "reextract",
                               reextract_then_rewrite), \
                mock.patch.object(case_originals.OriginalStore, "resolve", resolve_rewritten):
            yield

    def test_spec_keeps_and_replays_sessions_and_withholds_them_from_the_case_reader(self):
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from scripts import object_workspace_probe as probe
        from scripts import ontology_projection_probe as projection

        spec_doc = wiring.spec("session.replay")
        fake, books = wiring.fixture_bc()
        with tempfile.TemporaryDirectory() as tmp, \
                ObjectViewLiveCheckTest.fixture_sources(spec_doc) as environ:
            code, document = wiring.run_main(probe.main, wiring.argv_of(spec_doc, tmp),
                                             environ=environ, bc_sender=fake, bc_books=books)
            self.assertEqual(code, 0, document)
            self.assertEqual(lv.check_expectations(spec_doc, code, document), [])
            replayed = document["session_replay"]
            self.assertEqual((replayed["sessions_recorded"], replayed["replay_identical"],
                              replayed["withheld_reader_markers"]), (3, 3, 3))
            self.assertGreater(replayed["citations"], 0)
            self.assertNotIn("api://", json.dumps(document))   # counts only
            store = f"{tmp}/ontology.sqlite"
            mirror = objects.mirror_path(projection.served_state_path(store))
            replay = objects.ObjectWorkspace.replay

            def changed_first(workspace, request):
                # Every captured row changes in place (same handles) before the replay.
                with sqlite3.connect(mirror) as db:
                    for table in ("row_version", "row_current"):
                        db.execute(f"UPDATE {table} SET row_json = json_set(row_json, "
                                   "'$.notes', 'changed after the session')")
                db.close()
                return replay(workspace, request)

            runs = {}
            for name, patch in (
                    ("changed", mock.patch.object(objects.ObjectWorkspace, "replay",
                                                  changed_first)),
                    ("rewritten", self.rewriting_reextraction()),
                    ("granted", mock.patch.object(probe, "REPLAY_PRINCIPAL",
                                                  projection.served_host(store, environ)
                                                  .config.principal))):
                with patch:
                    runs[name] = probe.check_session_replay(
                        projection.served_host(store, environ), f"{tmp}/cases-{name}", sessions=3,
                        sample=40, per_type=4, budget_seconds=None)
        changed, granted = runs["changed"], runs["granted"]
        # DOC-08: re-extraction made one new version and every earlier citation resolved to
        # its original content; one that resolves to other content afterwards is counted.
        kept = runs["granted"]["reextraction"]
        self.assertEqual((kept["attached"], kept["new_version"], kept["earlier_citations"] > 0,
                          kept["changed_earlier"], kept["bytes_changed"], kept["refused"]),
                         (1, 1, True, 0, 0, 0))
        rewritten = runs["rewritten"]["reextraction"]
        self.assertGreater(rewritten["changed_earlier"], 0)
        self.assertFalse(probe.session_replay_passed(runs["rewritten"]))
        self.assertIn("session_replay.reextraction.changed_earlier != expected",
                      lv.check_expectations(spec_doc, 0, {
                          **document, "session_replay": runs["rewritten"]}))
        self.assertGreater(changed["divergences_by_kind"].get("citation_content_changed", 0),
                           0)
        self.assertLess(changed["replay_identical"], changed["sessions_recorded"])
        self.assertIn("session_replay.divergences != expected",
                      lv.check_expectations(spec_doc, 1, {**document,
                                                          "session_replay": changed}))
        self.assertFalse(probe.session_replay_passed(changed))
        # A "withheld" reader holding the recorder's grant sees every session: counted.
        self.assertEqual(granted["withheld_reader_shown"], granted["sessions_recorded"])
        self.assertFalse(probe.session_replay_passed(granted))


class SessionBcReuseTest(unittest.TestCase):
    """A session consumer reuses the producer's complete BC units (gap-fbb6c2dba861).

    ``workspace.object_view`` and ``ontology.mentions`` read the session state, but when the
    session projection (``ontology.session_projection``) captured BC and then ran out of budget
    in its projection before it committed, each consumer captured BC cold into its own served
    state: ``units_reused`` 0 and some 1,250 s of its 2,600 s budget gone before its
    projection. Now the consumer starts from the producer's capture and reads
    only what the session did not complete.

    Evidence label: ``injected-transport``. The verifier session (the live verifier
    ``SessionState``) runs the specs' own argv over the fixture Portal, the BC fake and the
    Graph fake; nothing live (both specs stay live-unverified)."""

    def run_session(self, *, fresh_shift: timedelta = timedelta(0), corrupt: bool = False):
        """One verifier session: the session mirror, the session projection producer (its
        budget runs out in its projection, after its BC capture), then workspace.object_view
        by its own argv with a fresh BC fake. Returns (producer report, consumer exit
        code, consumer report, the consumer's BC requests by path)."""
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from integrations import workbench_read_api
        from ontology import project_incremental
        from scripts import object_workspace_probe as workspace
        from scripts import ontology_projection_probe as projection
        from scripts.mirror_measure import ordered_full_coverage

        specs = lv.valid_specs(lv.load_specs(lv.ROOT / lv.SPECS_DIR_NAME))
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp, wiring.fixture_bound(), \
                ObjectViewLiveCheckTest.fixture_sources(specs["workspace.object_view"]) as env:
            os.chdir(lv.ROOT)
            session = lv.SessionState(lv.ROOT)
            try:
                mirror = specs["mirror.full_coverage"]
                plan = session.plan(mirror, specs)
                ordered_full_coverage(workbench_read_api.WorkbenchReadApiClient(
                    wiring.FIXTURE_URL, wiring.PORTAL_ENV["PLX_WORKBENCH_API_KEY"]),
                    session.bases(mirror, plan)["/mirror.sqlite"] + "/mirror.sqlite",
                    resolve_sample=0)
                session.record(mirror, plan, {"id": "mirror.full_coverage", "result": "pass",
                                              "exit_code": 0,
                                              "detail": {"fields": {"relation_count": 1}}})
                since = datetime.strptime(session.started_at, "%Y-%m-%dT%H:%M:%S.%fZ"
                                          ).replace(tzinfo=timezone.utc) + fresh_shift
                environ = {**env, lv.CAPTURE_FRESH_SINCE_ENV:
                           since.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}

                def run(spec_id, main, fake, books):
                    spec_doc = specs[spec_id]
                    step = session.plan(spec_doc, specs)
                    argv = lv._expand(spec_doc["command"][3:], f"{tmp}/{spec_id}", None,
                                      session.bases(spec_doc, step))
                    code, document = wiring.run_main(main, argv, environ=environ,
                                                     bc_sender=fake, bc_books=books)
                    session.record(spec_doc, step, {
                        "id": spec_id, "result": "pass" if code == 0 else "fail",
                        "exit_code": code, "detail": {"fields": document}})
                    return code, document, session.bases(spec_doc, step)

                fake, books = wiring.fixture_bc()
                # The producer's budget runs out in its projection, after its BC capture
                # (a filled store the verifier shares, never committed).
                with mock.patch.object(project_incremental, "build_generation",
                                       side_effect=projection._Budget.Spent("projection")):
                    _, produced, bases = run("ontology.session_projection", projection.main,
                                             fake, books)
                seed = Path(bases["/projection"], "projection", projection.SESSION_CAPTURES,
                            projection.SESSION_BC)
                self.assertTrue(seed.is_file())
                if corrupt:
                    seed.write_bytes(b"not a database" * 100)
                consumer, books = wiring.fixture_bc()
                code, document, _ = run("workspace.object_view", workspace.main,
                                        consumer, books)
            finally:
                os.chdir(cwd)
                session.close()
        reads = [call[1] for call in consumer.calls
                 if "login.microsoftonline.com" not in call[1]]
        return produced, code, document, reads

    def test_a_consumer_reuses_every_unit_the_session_producer_completed(self):
        produced, code, document, reads = self.run_session()
        # The producer captured BC in this session and did not commit its projection.
        self.assertEqual((produced["status"], produced["phase"]),
                         ("budget_exhausted", "projection"))
        made = produced["bc_capture"]
        self.assertGreater(made["units_complete"], 0, made)
        captured = document["bc_capture"]
        # Every unit the producer completed is reused but each company's own record,
        # which every call takes from the company listing it reads anyway (no request).
        self.assertEqual(captured["units_reused"] + captured["companies_captured"],
                         made["units_complete"], captured)
        self.assertEqual(captured["units_complete"], made["units_complete"])
        self.assertTrue(captured["session_seeded"])
        # Every unit was complete: the consumer read only the company listing, no unit.
        self.assertEqual(made["units_complete"], made["units_total"], made)
        self.assertTrue(reads)
        self.assertTrue(all(url.split("?")[0].endswith("/companies") for url in reads), reads)
        self.assertEqual(document["session_projection"]["mode"], "cold")
        self.assertIn("bc", document["sources"])
        # The reused capture is served to readers as before: nothing reaches no grant.
        self.assertEqual((code, document["denied_leaks"], document["find_denied_leaks"],
                          document["workspace_errors"]), (0, 0, 0, 0), document)
        self.assertGreater(document["roots_opened"], 0)
        # The spec holds on this output (only the labels tell the fake from a live read,
        # and the fixture's SharePoint file names no record) and its record keeps the
        # reuse counts.
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        spec_doc = wiring.spec("workspace.object_view")
        self.assertEqual(set(lv.check_expectations(spec_doc, code, document)),
                         {"bc_capture.evidence_label != expected",
                          "sharepoint_capture.evidence_label != expected"}
                         | NO_SHAREPOINT_INPUT)
        recorded = lv._recorded(spec_doc, document)["bc_capture"]
        self.assertEqual((recorded["units_reused"], recorded["session_seeded"]),
                         (captured["units_reused"], True), recorded)

    def test_a_capture_outside_the_freshness_window_or_unreadable_is_read_again(self):
        # Session start after the producer's runs began: copied, but nothing reused.
        _, code, late, reads = self.run_session(fresh_shift=timedelta(hours=1))
        self.assertEqual(code, 0, late)
        self.assertTrue(late["bc_capture"]["session_seeded"])
        self.assertEqual(late["bc_capture"]["units_reused"], 0)
        self.assertEqual(late["bc_capture"]["units_complete"], late["bc_capture"]["units_total"])
        self.assertTrue(any(not url.split("?")[0].endswith("/companies") for url in reads))
        # A producer file SQLite refuses: not copied, the capture runs cold, never an error.
        _, code, cold, _ = self.run_session(corrupt=True)
        self.assertEqual(code, 0, cold)
        self.assertFalse(cold["bc_capture"]["session_seeded"])
        self.assertEqual(cold["bc_capture"]["units_reused"], 0)
        self.assertEqual(cold["bc_capture"]["units_complete"], cold["bc_capture"]["units_total"])


if __name__ == "__main__":
    unittest.main()
