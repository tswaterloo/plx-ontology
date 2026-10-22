"""Live-check wiring: the allowlist, the read-only MC scope and the ontology probes.

* The MC probe (``integrations.mc_brain_probe``) may send
  ``tools/call`` only for read tools. Its tool allowlist is cross-checked against the
  machine-readable MC policy (``integrations/mc_tool_policy.json``), which the guard's read,
  approvable and never-sent sets must equal exactly, and, when the read-only PLX_MC
  checkout holds the policy's commit, against
  the upstream tool definitions at that commit (``src/lib/mcp/create-http-server.ts``,
  ``src/lib/mcp/read-actions.ts``, ``src/lib/mcp/actions.ts``).
* ``ontology.projection``, ``ontology.traverse`` and ``ontology.mentions`` are enabled;
  each spec's own argv runs end to end over the fixture world
  (``evals/oracle/fixture_world.py``) with the Portal, BC (and Graph) transport injected,
  under the curated schema, inside the spec's memory budget.
* A Portal outage blocks parts, not tiers: with the Portal
  answering 500 the traverse and mentions specs measure from BC (and mail), their cited
  ``bc://`` handles resolve against the capture, the runner scores them blocked in part
  (the projected tier partial), and with no record source at all the check is blocked,
  never a pass.

Evidence label: injected-transport. No network I/O, no credentials.
"""

from __future__ import annotations

import ast
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from verify import live_verify as lv
from ontology.generator.tool_registration import tool_registration

ROOT = Path(__file__).resolve().parents[2]
# The Workbench's MC tool policy, independent of the guard, and its PLX_MC commit.
MC_POLICY = json.loads((ROOT / "integrations/mc_tool_policy.json").read_text("utf-8"))
MC_COMMIT = MC_POLICY["commit"]
MC_PROBE = "integrations.mc_brain_probe"
ENABLED = {"mc.brain_probe": MC_PROBE,
           "ontology.projection": "scripts.ontology_projection_probe",
           "ontology.traverse": "scripts.ontology_traverse_probe",
           "ontology.mentions": "scripts.ontology_projection_probe",
           "ontology.link_tier_agree": "scripts.link_tier_agree_probe"}
# PLX_MC at that commit: each probe tool's registered handler, and the awaited
# calls of that action (all reads: the snapshot, the honesty oracle, the brain GET).
UPSTREAM_READS = {"mc_self_check": ("actionSelfCheck", {"snapshot", "buildHonestyFields"}),
                  "mc_get_context": ("actionGetContext", {"snapshot"}),
                  "mc_search_knowledge": ("actionSearchKnowledge", {"searchBrainAsk"})}
UPSTREAM_WRITES = ("actionCreateTask", "actionCheckout", "actionProgress", "actionComplete",
                   "actionRequestApproval", "actionCreateProject", "actionCreateBucket",
                   "actionUpdateBucket")


# Allowlisted ahead of its one spec, which stays disabled until a named precondition exists
# (mc.action_journal waits on the sp_mcp_workbench MC
# principal). The test fails once the spec is enabled, so the entry cannot outlive it.
PARKED_MODULES = {"operating.action_tools": "mc.action_journal"}


def spec(spec_id: str) -> dict:
    return json.loads((ROOT / lv.SPECS_DIR_NAME / f"{spec_id}.json").read_text("utf-8"))


def upstream(path: str) -> str | None:
    """PLX_MC ``path`` at ``MC_COMMIT`` from the read-only checkout, or None."""
    checkout = os.environ.get("PLX_DRIFT_CHECKOUT_PLX_MC", "/home/user/PLX_MC")
    try:
        done = subprocess.run(["git", "-C", checkout, "show", f"{MC_COMMIT}:{path}"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def upstream_mcp_tools() -> set[str] | None:
    """Every MCP tool PLX_MC registers under ``src/lib/mcp/`` at ``MC_COMMIT``."""
    checkout = os.environ.get("PLX_DRIFT_CHECKOUT_PLX_MC", "/home/user/PLX_MC")
    try:
        done = subprocess.run(["git", "-C", checkout, "ls-tree", "-r", "--name-only",
                               MC_COMMIT, "src/lib/mcp/"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    names: set[str] = set()
    for path in done.stdout.split():
        if path.endswith(".ts") and ".test." not in path:
            source = upstream(path)
            if source is None:
                return None
            names |= set(tool_registration(r"mc_[a-z_]+").findall(source))
    return names


class McReadOnlyScopeTests(unittest.TestCase):
    def setUp(self):
        from integrations import mc_brain_probe
        from integrations import plx_hub_guard
        self.probe, self.guard = mc_brain_probe, plx_hub_guard

    def test_guard_sets_are_exactly_the_policy_and_probe_tools_are_reads(self):
        self.assertEqual(set(self.guard.MC_READ_TOOLS), set(MC_POLICY["reads"]))
        self.assertEqual(set(self.guard.MC_APPROVABLE_WRITE_TOOLS),
                         set(MC_POLICY["approvable_writes"]))
        self.assertEqual(set(self.guard.MC_NEVER_TOOLS), set(MC_POLICY["never_sent"]))
        self.assertEqual(self.guard.MC_WRITE_TOOLS,
                         set(MC_POLICY["approvable_writes"]) | set(MC_POLICY["never_sent"]))
        self.assertEqual(set(MC_POLICY["reads"]) & self.guard.MC_WRITE_TOOLS, set())
        self.assertEqual(self.probe.PROBE_TOOLS, {"mc_self_check", "mc_get_context",
                                                  "mc_search_knowledge"})
        self.assertLessEqual(self.probe.PROBE_TOOLS, set(MC_POLICY["reads"]))

    def test_policy_names_exactly_the_tools_registered_upstream(self):
        """Every HTTP MCP tool at the policy's commit is in the policy, no more."""
        registered = upstream_mcp_tools()
        if registered is None:
            self.skipTest("PLX_MC checkout at the pinned commit is absent")
        self.assertIn("mc_release_checkout", registered)
        policy = (set(MC_POLICY["reads"]) | set(MC_POLICY["approvable_writes"])
                  | set(MC_POLICY["never_sent"]))
        self.assertEqual(registered, policy - set(MC_POLICY["stdio_only"]))

    def test_probe_source_names_no_other_mc_tool(self):
        """No code path in the probe can name a tool outside the allowlist."""
        tree = ast.parse((ROOT / "integrations/mc_brain_probe.py").read_text("utf-8"))
        named = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                 and isinstance(node.value, str) and re.fullmatch(r"mc_[a-z_]+", node.value)}
        self.assertEqual(named, set(self.probe.PROBE_TOOLS))

    def test_calls_sent_match_the_upstream_read_tool_definitions(self):
        from integrations.tests.test_mc_brain_guard import (MC_KEY, OPERATOR, VMC_KEY,
                                                            FakeSender, hub, ok_json,
                                                            tool_result)
        sent: list[tuple[str, str, frozenset]] = []
        payloads = {"mc_self_check": {"ok": True, "dataSource": "live"},
                    "mc_get_context": {"data": {"topTasks": [], "buckets": []}},
                    "mc_search_knowledge": {"data": {"status": "ok", "hits": []}}}

        def respond(method, url, headers, body):
            if "/api/cursor/mcp" in url:
                request = json.loads(body)
                params = request["params"]
                sent.append((request["method"], params["name"],
                             frozenset(params.get("arguments") or {})))
                return ok_json(tool_result(payloads[params["name"]], request_id=request["id"]))
            return ok_json({"data": {"available": True, "flags": {}}})

        environ = {"MC_MCP_API_KEY": MC_KEY, "VMC_API_KEY": VMC_KEY}
        transport, _ = hub(FakeSender(fn=respond), environ=environ)
        report = self.probe.run_probe(environ, operator_email_default=OPERATOR, hub=transport)
        self.assertTrue(report["ok"], report)
        self.assertEqual({method for method, _n, _a in sent}, {"tools/call"})
        self.assertEqual({name for _m, name, _a in sent}, set(self.probe.PROBE_TOOLS))
        for _method, name, args in sent:
            self.assertLessEqual(args, self.guard.MC_READ_TOOLS[name], name)

        server = upstream("src/lib/mcp/create-http-server.ts")
        reads = upstream("src/lib/mcp/read-actions.ts")
        actions = upstream("src/lib/mcp/actions.ts")
        approvals = upstream("src/lib/mcp/approval-actions.ts")
        if None in (server, reads, actions, approvals):
            self.skipTest("PLX_MC checkout at the pinned commit is absent (guard "
                          "cross-check above still ran)")
        sources = server + reads
        for _method, name, args in sent:
            handler, awaited = UPSTREAM_READS[name]
            registration = re.search(r'server\.tool\(\s*"%s",(.*?)(?=server\.tool\(|\Z)'
                                     % name, sources, re.S)
            self.assertIsNotNone(registration, f"{name} not registered upstream")
            block = registration[1]
            self.assertIn(handler + "(", block, name)
            schema_keys = set(re.findall(r"^\s+([A-Za-z]+): z\.", block, re.M))
            self.assertLessEqual(args, schema_keys, name)
            self.assertLessEqual(schema_keys, self.guard.MC_READ_TOOLS[name], name)
            self.assertEqual(self._awaited(actions + reads, handler), awaited, name)
        writes = set().union(*(self._awaited(actions + reads + approvals, w)
                               for w in UPSTREAM_WRITES))
        self.assertIn("patchTask", writes)   # the write extraction really reads bodies
        for _handler, awaited in UPSTREAM_READS.values():
            self.assertFalse(awaited & writes)
        # Upstream labels the brain search read-only in its own tool description.
        self.assertRegex(reads, r'"mc_search_knowledge",\s*"[^"]*Read-only\."')

    @staticmethod
    def _awaited(source: str, function: str) -> set[str]:
        body = re.search(r"export async function %s\(.*?(?=\nexport |\n// ─|\Z)"
                         % function, source, re.S)
        assert body, function
        return set(re.findall(r"await ([\w.]+)\(", body[0]))


class AllowlistTests(unittest.TestCase):
    def test_enabled_specs_are_allowed_valid_and_listed_by_the_dry_run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = lv.main(["--dry-run"], environ={"PATH": "/usr/bin"})
        self.assertEqual(code, 0)
        rows = {row["id"]: row for row in json.loads(out.getvalue())["specs"]}
        for spec_id, module in ENABLED.items():
            with self.subTest(spec=spec_id):
                self.assertTrue(rows[spec_id]["valid"], rows[spec_id])
                self.assertFalse(rows[spec_id]["disabled"])
                self.assertEqual(rows[spec_id]["module"], module)
                self.assertIn(module, lv.ALLOWED_MODULES)
                self.assertNotIn("disabled", spec(spec_id))

    def test_every_allowlisted_module_is_run_by_an_enabled_spec_and_vice_versa(self):
        entries = lv.load_specs(ROOT / lv.SPECS_DIR_NAME)
        used = {e["spec"]["command"][2] for e in entries
                if e["spec"] and not e["spec"].get("disabled")}
        self.assertLessEqual(used, set(lv.ALLOWED_MODULES))
        for module, spec_id in PARKED_MODULES.items():
            self.assertEqual(spec(spec_id)["command"][2], module)
            self.assertTrue(spec(spec_id).get("disabled"),
                            f"{spec_id} is enabled: drop it from PARKED_MODULES")
        self.assertEqual(set(lv.ALLOWED_MODULES) - used - set(PARKED_MODULES), set(),
                         "an allowlisted module no enabled spec runs is dead credentialed "
                         "scope")

    def test_runner_stays_get_only_and_mc_read_only(self):
        self.assertIn("read-tool tools/call only", lv.MODE)
        tree = ast.parse((ROOT / "verify/live_verify.py").read_text("utf-8"))
        imported = {alias.name for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in node.names} | {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module}
        for banned in ("urllib", "urllib.request", "http", "http.client", "socket",
                       "requests", "integrations", "operating"):
            self.assertNotIn(banned, imported)   # it runs probes; it never sends itself
        for spec_id in ENABLED:
            command = spec(spec_id)["command"]
            self.assertEqual(command[:2], ["python3", "-m"])
            self.assertFalse({"-c", "--shell"} & set(command))


# ------------------------------------------------------------- fixture world runs

FIXTURE_URL = "https://workbench-fixture.invalid"
PORTAL_ENV = {"PLX_WORKBENCH_API_URL": FIXTURE_URL, "PLX_WORKBENCH_API_KEY": "plxwb_" + "f" * 40}


def argv_of(spec_doc: dict, tmp: str) -> list[str]:
    """The spec's own arguments, as the verifier expands them without a daily report."""
    return lv._expand(spec_doc["command"][3:], tmp, None)


def arg(spec_doc: dict, name: str) -> float:
    command = spec_doc["command"]
    return float(command[command.index(name) + 1])


@contextlib.contextmanager
def fixture_portal():
    """``WorkbenchReadApiClient`` over the fixture world's API routes (no network)."""
    from evals.oracle.fixture_world import build_world
    from integrations import workbench_read_api
    from scripts.eval_unfamiliar_sample import _fixture_fetch

    real, fetch = workbench_read_api.WorkbenchReadApiClient, _fixture_fetch(build_world())

    def client(url, key, **kwargs):
        assert url == FIXTURE_URL
        return real(url, key, fetch=fetch, **kwargs)

    with mock.patch.object(workbench_read_api, "WorkbenchReadApiClient", client):
        yield


@contextlib.contextmanager
def fixture_bound():
    """Owner rule 1b5e555dce17's denominator over the fixture world: it serves only part
    of the curated bound relations, so the bound relations it serves (live, the catalog
    serves every one, and one it drops is incomplete)."""
    from evals.oracle.fixture_world import build_world
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from scripts import mirror_measure
    from scripts.eval_unfamiliar_sample import _fixture_fetch

    served = set(WorkbenchReadApiClient(FIXTURE_URL, PORTAL_ENV["PLX_WORKBENCH_API_KEY"],
                                        fetch=_fixture_fetch(build_world())).relations())
    bound = sorted(set(mirror_measure.bound_relations()) & served)
    with mock.patch.object(mirror_measure, "bound_relations", return_value=bound):
        yield


def run_main(main, argv, **kwargs) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(argv, **kwargs)
    return code, json.loads(out.getvalue())


class FixtureWorldProbeTests(unittest.TestCase):
    """Each enabled ontology spec's argv over the fixture world (injected transport)."""

    def assert_spec_holds(self, spec_doc: dict, code: int, document: dict) -> None:
        self.assertEqual(code, 0, document)
        failures = lv.check_expectations(spec_doc, code, document)
        # The fixture world is far smaller than PLX: only the live size floors may miss,
        # and each of those counts is still positive; an evidence label asked to be
        # live-read is the injected transport's own.
        floors = {f"{path} < {minimum}" for path, minimum in
                  spec_doc["expect"].get("json_min", {}).items()}
        labels = [path for path in spec_doc["expect"].get("json_equals", {})
                  if path.endswith("evidence_label")]
        floors |= {f"{path} != expected" for path in labels}
        self.assertLessEqual(set(failures), floors, failures)
        for path in labels:
            self.assertEqual(lv.resolve(document, path), ["injected-transport"])
        for path in spec_doc["expect"].get("json_min", {}):
            self.assertTrue(all(value > 0 for value in lv.resolve(document, path)), path)
        if "--max-rss-mb" in spec_doc["command"]:
            self.assertLessEqual(document["maxrss_mb"], arg(spec_doc, "--max-rss-mb"))
            self.assertIs(document["maxrss_within_budget"], True)
        recorded = json.dumps(lv._recorded(spec_doc, document))
        for value in ("PO92", "PO#9", "PO-9FX", "FXBC", "FXPI", "fx-po-", "api://", "bc://",
                      "graph-mail://", "petrasoap"):
            self.assertNotIn(value, recorded)

    def test_projection_spec_runs_end_to_end(self):
        from scripts import ontology_projection_probe as probe
        spec_doc = spec("ontology.projection")
        with tempfile.TemporaryDirectory() as tmp, fixture_bound(), fixture_portal():
            code, document = run_main(probe.main, argv_of(spec_doc, tmp), environ=PORTAL_ENV)
        self.assert_spec_holds(spec_doc, code, document)
        self.assertTrue(document["mirror_complete"])
        self.assertLess(document["build_seconds"], arg(spec_doc, "--budget-seconds"))
        self.assertEqual(lv.spec_cadence(spec_doc), "daily")

    def test_traverse_spec_runs_end_to_end(self):
        from scripts import ontology_traverse_probe as probe
        spec_doc = spec("ontology.traverse")
        fake, books = fixture_bc()
        from operating import ontology_host, ontology_tools_traverse as tt
        opening, reads, opens = [False], [], []
        opened = ontology_host.CaseHostTools.open_generation
        open_table = ontology_host.CaseHostTools._open_table

        def open_generation(host):
            first = not opens
            opened(host)
            if first:   # publish the store again: the first call finds a new generation
                staged = Path(str(host.store_path) + ".next")
                shutil.copyfile(host.store_path, staged)
                os.replace(staged, host.store_path)

        def _open_table(host):
            opens.append(1)
            opening[0] = True
            try:
                open_table(host)
            finally:
                opening[0] = False
        metered = tt._metered
        with tempfile.TemporaryDirectory() as tmp, fixture_bound(), fixture_portal(), \
                mock.patch.object(ontology_host.CaseHostTools, "open_generation",
                                  open_generation), \
                mock.patch.object(ontology_host.CaseHostTools, "_open_table", _open_table), \
                mock.patch.object(tt, "_metered", lambda tool, phase, seconds: (
                    reads.append(opening[0]) if tool == "store" else None,
                    metered(tool, phase, seconds))):
            code, document = run_main(probe.main, argv_of(spec_doc, tmp),
                                      environ={**PORTAL_ENV, **BC_ENV}, bc_sender=fake,
                                      bc_books=books)
        self.assert_spec_holds(spec_doc, code, document)
        # Every store-wide read lands in the host's opening of a generation (the session's
        # and the swapped one's, before its call), none in an around or timeline call.
        self.assertGreaterEqual(len(opens), 2)   # the session open and the swap's request()
        self.assertTrue(reads)
        self.assertTrue(all(reads), reads)
        self.assertGreaterEqual(document["open_generation_seconds"], 0)
        self.assertEqual((document["blocked_upstream"], document["sources"]),
                         ([], ["bc", "portal_api"]))
        self.assertTrue(document["mirror_complete"])
        self.assertFalse(document["budget_exhausted"])
        self.assertLess(document["seconds"], arg(spec_doc, "--traverse-budget-seconds"))
        self.assertGreater(document["around_cited_edges"], 0)
        self.assertGreater(document["timeline_events"], 0)

    def test_traverse_probe_fails_over_its_memory_budget(self):
        from scripts import ontology_traverse_probe as probe
        with tempfile.TemporaryDirectory() as tmp, fixture_portal(), \
                mock.patch.dict(os.environ, PORTAL_ENV):
            code, document = run_main(probe.main, ["--db", f"{tmp}/m.sqlite", "--store",
                                                   f"{tmp}/s.sqlite", "--sample", "2",
                                                   "--max-rss-mb", "1"])
        self.assertEqual(code, 1)
        self.assertIs(document["maxrss_within_budget"], False)
        self.assertEqual(document["max_rss_budget_mb"], 1.0)

    def test_mentions_spec_runs_end_to_end(self):
        spec_doc = spec("ontology.mentions")
        with fixture_portal():
            code, document = run_mentions(spec_doc, "Re: customer PO FXCPO-001, please confirm.")
        self.assert_spec_holds(spec_doc, code, document)
        self.assertEqual(document["blocked_upstream"], [])
        self.assertGreaterEqual(document["email_objects"], 1)
        self.assertGreaterEqual(document["mention_links"], 1)
        self.assertEqual(document["mention_denied_leaks"], 0)
        self.assertNotIn("FXCPO", json.dumps(document))


class PortalOutageProbeTests(unittest.TestCase):
    """The Portal answers 500 on every route; the traverse and
    mentions specs' own argv measure from the fixture world's BC (and mail), and the real
    verifier decides the result from the real spec (``replay``)."""

    def assert_measured_in_part(self, spec_doc: dict, document: dict) -> None:
        self.assertEqual(document["blocked_upstream"], [
            {"part": "portal_mirror", "source": "portal-workbench-api", "http_status": 500,
             "error_code": "internal"}])
        self.assertNotIn("portal_api", document["sources"])
        check = replay(spec_doc, document)
        self.assertEqual(check["result"], "blocked", check["detail"])
        self.assertEqual((check["detail"]["blocked"]["cause"],
                          check["detail"]["blocked"].get("measured")), ("upstream_outage", True))
        self.assertNotIn("failures", check["detail"])
        tier = lv.tier_summary([check])["projected"]
        self.assertEqual((tier["status"], tier["measured_in_part"]), ("partial", [spec_doc["id"]]))
        recorded = json.dumps(check["detail"])
        for value in ("bc://", "graph-mail://", "FXPI", "FXSI", "FXVINV", "petrasoap"):
            self.assertNotIn(value, recorded)

    def test_traverse_measures_bc_and_its_citations_resolve_while_the_portal_is_down(self):
        from scripts import ontology_projection_probe as projection
        from scripts import ontology_traverse_probe as probe
        spec_doc = spec("ontology.traverse")
        fake, books = fixture_bc()
        with tempfile.TemporaryDirectory() as tmp, portal_down():
            code, document = run_main(probe.main, argv_of(spec_doc, tmp),
                                      environ={**PORTAL_ENV, **BC_ENV}, bc_sender=fake,
                                      bc_books=books)
            store = argv_of(spec_doc, tmp)[argv_of(spec_doc, tmp).index("--store") + 1]
            import sqlite3
            conn = sqlite3.connect(store)
            handle = conn.execute("SELECT l1_handle FROM object_versions ORDER BY "
                                  "l1_handle LIMIT 1").fetchone()[0]
            conn.close()
            # a handle naming a capture that never happened does not resolve
            forged = handle.rsplit("-", 1)[0] + "-999999"
            host = projection.served_host(store)
            with host.served_tools(projection.reader_actor(host)) as served:
                count = projection.served_citations(served, projection.reader_actor(host))
                self.assertEqual(count([handle, forged])["unresolved"], 1)
        self.assertEqual(code, 0, document)
        self.assertTrue(handle.startswith("bc://"))
        cited = document["citations"]
        self.assertEqual(set(cited["by_scheme"]), {"bc"})   # only BC answered
        self.assertEqual((cited["resolved"], cited["unresolved"]), (cited["checked"], 0))
        self.assertGreater(cited["checked"], 0)
        self.assertGreater(document["around_reached"], 0)
        self.assertEqual(document["bc_capture"]["evidence_label"], "injected-transport")
        self.assert_measured_in_part(spec_doc, document)

    def test_mentions_reach_bc_records_while_the_portal_is_down(self):
        spec_doc = spec("ontology.mentions")
        number = fixture_world().bc["purchaseInvoices"][0]["fields"]["vendorInvoiceNumber"]
        with portal_down():
            code, document = run_mentions(spec_doc,
                                          f"Re: invoice number {number}, please confirm.")
        self.assertEqual(code, 0, document)
        self.assertEqual(document["sources"], ["bc", "graph_mail"])
        self.assertGreaterEqual(document["mention_links"], 1)   # mail -> a BC record
        self.assertEqual(document["mention_denied_leaks"], 0)
        cited = document["citations"]
        self.assertLessEqual(set(cited["by_scheme"]), {"bc", "graph-mail"})
        self.assertGreater(cited["by_scheme"].get("bc", 0), 0)
        self.assertEqual(cited["unresolved"], 0)
        self.assert_measured_in_part(spec_doc, document)

    def test_with_every_record_source_blocked_the_check_is_blocked_never_a_pass(self):
        from scripts import ontology_traverse_probe as probe
        spec_doc = spec("ontology.traverse")
        fake, books = fixture_bc()
        fake.fail(r"/companies$", (401, {}, b'{"error":{"code":"Unauthorized"}}'))
        with tempfile.TemporaryDirectory() as tmp, portal_down():
            code, document = run_main(probe.main, argv_of(spec_doc, tmp),
                                      environ={**PORTAL_ENV, **BC_ENV}, bc_sender=fake,
                                      bc_books=books)
        self.assertEqual((code, document["sources_blocked"]), (0, "no_record_source"))
        self.assertEqual([p["part"] for p in document["blocked_upstream"]],
                         ["portal_mirror", "bc_capture"])
        self.assertEqual([(p["http_status"], p["cause"]) for p in lv.blocked_parts(document)][1:],
                         [(401, "host_misconfiguration")])   # BC refused the credential
        check = replay(spec_doc, document)
        self.assertEqual(check["result"], "blocked")
        self.assertEqual(check["detail"]["reason"],
                         "upstream portal-workbench-api+business-central no_record_source")
        self.assertNotIn("measured", check["detail"]["blocked"])
        self.assertEqual(lv.tier_summary([check])["projected"]["status"], "blocked")


BC_ENV = {name: value for name, value in __import__(
    "integrations.tests.test_live_source_adapters", fromlist=["ENV"]).ENV.items()
    if name.startswith("BC_")}


def fixture_world():
    from evals.oracle.fixture_world import build_world
    return build_world()


def fixture_bc():
    """The BC fake serving the fixture world's companies, documents and lines."""
    from tests.test_eval_unfamiliar_sample import fixture_bc_sender
    return fixture_bc_sender(fixture_world())


@contextlib.contextmanager
def portal_down():
    """``WorkbenchReadApiClient`` whose every GET answers HTTP 500 ``internal``."""
    from integrations import workbench_read_api
    from integrations.http import SourceHttpError

    real = workbench_read_api.WorkbenchReadApiClient

    def fetch(url, headers, **_):
        raise SourceHttpError("read-only source GET failed: HTTPError", status=500,
                              error_code="internal")

    def client(url, key, **kwargs):
        return real(url, key, fetch=fetch, **kwargs)

    with mock.patch.object(workbench_read_api, "WorkbenchReadApiClient", client):
        yield


def run_mentions(spec_doc: dict, body: str) -> tuple[int, dict]:
    """The mentions spec's argv with the fixture BC and one allowlisted-mailbox message."""
    from integrations import graph_read
    from integrations.live_source_guard import BoundedBackoff
    from integrations.tests.test_capture_mail import BOX_A, ENV, FakeGraph
    from scripts import ontology_projection_probe as probe

    scope = json.loads((ROOT / spec_doc["command"][spec_doc["command"].index(
        "--mail-capture-scope") + 1]).read_text("utf-8"))
    address = scope["mailboxes"][0]["address"]
    graph = FakeGraph()   # mailbox A serves as the spec's allowlisted mailbox
    graph.users[BOX_A].update(userPrincipalName=address, mail=address,
                              proxyAddresses=[f"SMTP:{address}"])
    graph.folders[BOX_A]["sentitems"] = "AAMkFOLDER-SENT="
    graph.received = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    graph.put(BOX_A, "MSG-1", "CK-1", body=body)
    real_build = graph_read.build_graph
    fake, books = fixture_bc()

    def build(environ=None, **_kw):
        if environ is not None and not environ.get("MICROSOFT_GRAPH_CLIENT_ID"):
            # the probe's served host reads offline: no credentials (its people exclusion
            # only), no reader
            return real_build(environ)
        return real_build(ENV, sender=graph,
                          retry=BoundedBackoff(sleep=lambda s: None, jitter=lambda: 0.0))

    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(graph_read, "build_graph", build):
        cwd = os.getcwd()
        os.chdir(ROOT)   # the spec names its scope files relative to the repo
        try:
            return run_main(probe.main, argv_of(spec_doc, tmp),
                            environ={**PORTAL_ENV, **BC_ENV, **ENV}, bc_sender=fake,
                            bc_books=books)
        finally:
            os.chdir(cwd)


REPLAY_MODULE = "scripts.replay_probe"


def replay(spec_doc: dict, document: dict) -> dict:
    """The real verifier's result for ``spec_doc`` when its command prints ``document``
    (a stand-in module in a temporary root; the spec's own expectations decide)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        (root / "scripts" / "__init__.py").write_text("")
        (root / "scripts" / "replay_probe.py").write_text(
            "import sys\nprint(open(sys.argv[1]).read())\n")
        (root / "out.json").write_text(json.dumps(document))
        stand_in = {key: value for key, value in spec_doc.items()
                    if key not in ("requires_env", "optional_env", "session_audience",
                                   "session_reads")}
        stand_in["command"] = ["python3", "-m", REPLAY_MODULE, str(root / "out.json")]
        return lv.run_spec(stand_in, root=root, environ={"PATH": os.environ.get("PATH", "")},
                           allowed_modules={REPLAY_MODULE})


if __name__ == "__main__":
    unittest.main()
