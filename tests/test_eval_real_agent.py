"""The real-agent eval's live part (scripts/eval_real_agent.py ``--live``).

Evidence: isolated-fixture and injected-transport. The oracle reads the synthetic fixture
world (its BC port injected, its Portal port failing as during the outage); no agent session
or network read starts, and the persistent host is played by the fixture stdio case host, by
``ReaderHost`` (a reader who sees one BC company) or by a real host state its capture cycle
wrote through injected Graph and BC senders (``LiveHostCitationTests``). What these tests
guard: the live sample is BC-rooted and survives a Portal outage as blocked parts; the live
host is the persistent host's own MCP server for the named reader, never a fixture or
fallback principal; without an identity, or with a reader the real host refuses, nothing is
read and the check is blocked; the root probe has a deadline; access (one reader), capture,
projection, deterministic and agent tiers are reported apart; a root the reader cannot
resolve and an answer failing only on a served BC citation the harness cannot re-read are
never charged to the agent; the harness re-reads every citation scheme the scorer accepts as
the reader (a withheld, revoked or changed one fails closed), through a session no agent's
catalog lists.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

from evals import runner
from evals import score as sc
from operating import session_identity as si
from operating.tests import test_host as th
from scripts import eval_real_agent as era

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "devloop" / "live-checks" / "eval.real_agent.json"
ASK_SPEC = ROOT / "devloop" / "live-checks" / "eval.ask_agent.json"
ABSTAIN = "```json\n" + json.dumps({"format": sc.ANSWER_FORMAT, "answerability": "blocked",
                                    "text": "Not established.", "values": [],
                                    "gaps": []}) + "\n```"
SECRET = "s" * 48
READER = {"uid": "u-1", "email": "reader@example-plx.com",
          "entraTenantId": "11111111-2222-4333-8444-555555555555",
          "entraObjectId": "66666666-7777-4888-8999-000000000000"}


def portal_down_sources(calls):
    """The oracle's fixture sources with every Portal read failing (the outage)."""
    from evals.oracle import PortalReads, Sources
    from scripts import eval_oracle_dev as dev

    def build(environ, namespace, *, guard):
        calls.append(namespace)
        sources, documents, configured, _ = dev.build_fixture(dev.eval_world())

        def down(name, columns):
            raise ConnectionError("portal down")
        return Sources(PortalReads(down), sources.bc), documents, configured
    return build


def pinned_live_date():
    """The live part's run id ('live-<UTC date>') seeds its item sample from the wall
    clock: pin the date it reads to 2026-10-10 (the time of day stays real), so the
    sample these tests assert on is the same on any day."""
    from scripts import eval_oracle_dev as dev
    real = dev._utc_now
    return mock.patch.object(dev, "_utc_now", lambda: "2026-10-10" + real()[10:])


def refuse_reads(environ, namespace, *, guard):
    raise AssertionError("a source was read without a reader identity")


def scripted_ask(cited, value, asked):
    """An Ask planner: escalates when the question says so, else reads the first record
    a ``plx.find`` of ``identifier`` names (as the loop showed it) and answers
    ``value(question)`` from it; ``asked`` collects each question it planned for."""
    import re

    def plan(request):
        question = request["question"]
        asked.append(question)
        if "escalate" in question:
            return {"action": "escalate", "reason": "a person must decide"}, None
        own = [step for step in request["transcript"] if not step.get("seeded")]
        if not own:
            return {"action": "call", "calls": [{"tool": "plx.find", "arguments": {
                "query": cited["identifier"]}}]}, None
        last = own[-1]["observations"][0]
        if last["tool"] == "plx.find":
            text = json.dumps(last["result"]).replace('\\"', '"')
            handle = re.search(cited["pattern"] + r'[^"\\]+', text).group(0)
            return {"action": "call", "calls": [{"tool": "plx.read",
                                                 "arguments": {"citation": handle}}]}, None
        handle, stated = last["arguments"]["citation"], value(question)
        return {"action": "answer", "unknowns": [], "claims": [{
            "text": f"It is {stated}.", "citations": [handle], "value_claims": [{
                "value": stated, "citation": handle, "field": cited["field"],
                **({"role": cited["role"]} if cited.get("role") else {})}]}]}, None
    return plan


def card_claims(request):
    """An Ask planner: reads the card of the object in view, then claims each of its first
    two string values, citing the record the card cites for it."""
    if not request["transcript"]:
        return {"action": "call", "calls": [{"tool": "plx.object", "arguments": {
            "object_id": request["question"].rsplit(": ", 1)[1]}}]}, None
    card = request["transcript"][-1]["observations"][0]["result"]
    claims = []
    for value in card.get("values") or []:
        stated = value.get("value")
        stated = stated.get("untrusted_text") if isinstance(stated, dict) else stated
        if isinstance(stated, str) and stated and len(claims) < 2:
            cite = value["citation"]
            claims.append({"text": f"It reads {stated}.", "citations": [cite["l1_handle"]],
                           "value_claims": [{"value": stated, "citation": cite["l1_handle"],
                                             "field": cite["field_path"]}]})
    return {"action": "answer", "unknowns": [], "claims": claims}, None


# B1 on the fixture parts of an agent that abstains
# everywhere: nothing is answered, so every rate fails.
ABSTAINING_AGENT_RED = ["eval.total.accuracy < 0.8", "eval.total.coverage < 0.8",
                        "eval.total.confident_unsupported_rate > 0.02"]

class ReaderHost:
    """The persistent host as a reader granted one BC company: ``plx.find`` names the
    root of an identifier in the query only for that company's records; every other tool
    is refused and no citation re-reads. ``log`` (shared by every session) records each
    call's query."""

    TOOLS = ("plx.find", "plx.investigate", "plx.object", "plx.around", "plx.timeline")

    def __init__(self, roots, company, log):
        self.roots, self.company, self.log = roots, company, log
        self.actor = {"tenant": "plx", "reader": "named_employee"}

    def tools(self):
        return [{"name": name, "read_only": True} for name in self.TOOLS]

    def call(self, name, arguments):
        self.log.append((name, arguments.get("query")))
        if name != "plx.find":
            return {"ok": False, "error": "tool_refused"}
        candidates = [{"object": {"object_id": f"Bc:{root['key']['id']}", "display": {
            "origin": {"citation": f"bc://production/{root['key']['company_id']}/set/"
                                   f"{root['key']['id']}@v1"}}}}
            for identifier, root in self.roots.items()
            if identifier in str(arguments.get("query"))
            and root["key"]["company_id"] == self.company]
        return {"status": "found" if candidates else "not_found", "candidates": candidates}

    def read(self, name, arguments):
        return {"ok": False, "error": "not_a_row"}

    def close(self):
        pass


class LivePartTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "state").mkdir()
        self.config = self.root / "host.json"
        self.config.write_text(json.dumps({"schema": "plx-host-config/v1", "tenant": "plx"}))
        self.environ = {era.READER_ENV: json.dumps(READER),
                        si.SESSION_TICKET_SECRET_ENV: SECRET,
                        era.STATE_ENV: str(self.root / "state"),
                        era.HOST_CONFIG_ENV: str(self.config)}

    def args(self, **changes):
        base = dict(reader_env=era.READER_ENV, host_config=None, live_count=24,
                    live_read_seconds=600.0, mailbox=None, folder="inbox", sharepoint_path=[],
                    min_live_items=era.MIN_LIVE_ITEMS, deterministic_seconds=600.0,
                    root_probe_seconds=era.ROOT_PROBE_SECONDS)
        return argparse.Namespace(**{**base, **changes})

    def test_live_sampler_picks_bc_rooted_items_while_the_portal_is_down(self):
        calls, made = [], []

        def recording_host(*args, **kwargs):
            made.append((args, kwargs))
            return ReaderHost({}, None, [])
        with mock.patch.object(era, "LiveHost", recording_host):
            part, blocked, info = era.live_part(self.environ, self.args(),
                                                build_sources=portal_down_sources(calls))
        self.assertGreaterEqual(info["sampled"], era.MIN_LIVE_ITEMS)
        self.assertEqual(info["strata"]["bc_invoice"], "read")
        # The Portal strata did not run: blocked parts, the BC part measured; a reader
        # who resolves no sampled root leaves the part blocked (never run, never wrong).
        self.assertIsNone(part)
        self.assertEqual({(p["part"], p["cause"]) for p in blocked},
                         {("live_sales_order", "upstream_outage"),
                          ("live_purchase_order", "upstream_outage"),
                          ("live_assembly_order", "upstream_outage"),
                          ("live_receipt", "upstream_outage"),
                          (era.LIVE_PART, "missing_bindings")})
        self.assertTrue(all(p["source"] == "portal-workbench-api" for p in blocked
                            if p["cause"] == "upstream_outage"))
        # Every host the part starts is the persistent host for the named reader.
        self.assertTrue(made)
        self.assertTrue(all(args == (self.root / "state", self.config, READER, SECRET)
                            and kwargs["tenant"] == "plx" for args, kwargs in made))
        # The agent's server is the persistent host's own MCP server and the harness's is
        # its harness-only session, both for the named reader over the same state: a
        # fresh single-use agent ticket per session, never --fixture or --principal.
        host = era.LiveHost(self.root / "state", self.config, READER, SECRET, tenant="plx")
        first, second, harness = host.mcp_server(), host.mcp_server(), host.harness_server()
        self.assertEqual(first["args"][:3], ["-m", "operating.host", "mcp"])
        self.assertEqual(harness["args"][:2], ["-m", "evals.harness_reads"])
        self.assertEqual(first["args"][3:], harness["args"][2:])
        self.assertIn("--session-ticket-env", first["args"])
        self.assertFalse({"--fixture", "--principal", "--employee-oid"} & set(first["args"]))
        tickets = [server["env"][era.TICKET_ENV] for server in (first, second, harness)]
        for ticket in tickets:
            claims = si.verify_session_ticket(ticket, SECRET)
            self.assertEqual((claims["entraObjectId"], claims["author"]),
                             (READER["entraObjectId"], si.AUTHOR_AGENT))
        self.assertEqual(len(set(tickets)), 3)
        self.assertNotIn("reader@", json.dumps(info) + json.dumps(blocked))
        # The people-exclusion guard's settings reach both servers (without them the host
        # withholds every mailbox); a credential the config does not ask for never does.
        with mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": "oid-x",
                                          "MICROSOFT_GRAPH_CLIENT_SECRET": "s3cret"}):
            for server in (host.mcp_server(), host.harness_server()):
                self.assertEqual(server["env"]["PLX_EXCLUDED_PEOPLE_OIDS"], "oid-x")
                self.assertNotIn("MICROSOFT_GRAPH_CLIENT_SECRET", server["env"])
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(set(era.EXCLUSION_ENV) & set(host.mcp_server()["env"]))

    def test_roots_resolve_over_the_live_host_wire_and_too_few_block_the_part(self):
        """The fixture stdio case host stands in for ``operating.host mcp`` (its real
        ``display`` envelopes): the sampled roots the reader's projection holds resolve,
        a root it does not hold does not, and fewer than ``--min-live-items`` resolved
        roots block the part, never a pass."""
        from ontology.schema import load_schema
        from scripts import eval_dev_baseline as edb
        from scripts import eval_oracle_dev as dev
        items = runner.load_items(edb.DEV_DIR, split="dev")
        relations = edb.reachable_relations(load_schema(), {i["root"]["relation"] for i in items},
                                            edb.DEFAULT_LINK_DEPTH)
        product = edb.build_product(edb.fixture_client(), self.root / "p", relations,
                                    budget_seconds=None, clock=edb.fixture_clock)
        case = runner.CaseHost(product.state_dir, product.audience, edb.TENANT,
                               [edb.PRINCIPAL], fixture=True, ontology_store=product.store)
        self.addCleanup(case.close)

        def wired():
            host = era.LiveHost(self.root / "state", self.config, READER, SECRET, tenant="plx")
            host.harness_server = case.mcp_server
            return host
        item = next(i for i in items if i["family"] == "F3")
        elsewhere = json.loads(json.dumps(item))
        elsewhere["root"]["key"] = {"id": "not-this-row"}
        self.assertEqual(era.resolve_roots([item, elsewhere], wired()), ([True, False], None))
        # The oracle samples Portal-rooted items here (the live run samples BC roots,
        # which this fixture host grants no reader).
        # The floor is the whole sample, so any root the projection lacks blocks the part
        # however many of the fixture's roots resolve.
        floor = self.args().live_count
        with mock.patch.object(era, "bc_rooted", lambda sampled: True), pinned_live_date():
            part, blocked, info = era.live_part(
                self.environ, self.args(min_live_items=floor),
                build_sources=lambda e, n, *, guard: dev.build_fixture(dev.eval_world())[:3],
                make_host=wired)
        projection = info["tiers"]["projection"]
        resolved = projection["roots_resolved"]
        self.assertEqual(projection["roots_sampled"], info["sampled"])
        self.assertTrue(0 < resolved < floor <= info["sampled"], projection)
        self.assertEqual(len(part[0]), resolved)
        deterministic = info["tiers"]["deterministic"]
        self.assertEqual((deterministic["items"], deterministic["error"]), (resolved, {}))
        self.assertIn((era.LIVE_PART, "roots_unresolved", "missing_bindings"),
                      {(p["part"], p.get("error_code"), p["cause"]) for p in blocked})

    def test_without_an_identity_the_live_part_is_blocked_before_any_read(self):
        cases = [({k: v for k, v in self.environ.items() if k != era.READER_ENV},
                  "no_employee_identity"),
                 ({**self.environ, era.READER_ENV: "eng"}, "reader_identity_invalid"),
                 ({**self.environ, era.READER_ENV: json.dumps({**READER, "email": ""})},
                  "reader_identity_invalid"),
                 ({k: v for k, v in self.environ.items()
                   if k != si.SESSION_TICKET_SECRET_ENV}, "session_secret_missing"),
                 ({k: v for k, v in self.environ.items() if k != era.STATE_ENV},
                  "host_state_missing")]
        for environ, code in cases:
            with self.subTest(code):
                part, blocked, _ = era.live_part(environ, self.args(),
                                                 build_sources=refuse_reads)
                self.assertIsNone(part)
                self.assertEqual([(p["part"], p["error_code"], p["cause"]) for p in blocked],
                                 [(era.LIVE_PART, code, "host_misconfiguration")])

    def test_tiers_stay_apart_and_an_unresolved_root_is_never_the_agents_failure(self):
        from verify import live_verify as lv
        from evals.oracle.fixture_world import COMPANIES
        from operating import host as plx_host
        from scripts import eval_oracle_dev as dev
        plx_host.Host(self.root / "state")  # the persistent host's ledger, nothing captured
        records = {r["fields"]["number"]: {"key": {"company_id": r["company_id"],
                                                   "id": r["id"]}}
                   for rows in dev.eval_world().bc.values() for r in rows
                   if r["fields"].get("number")}
        # The primary company's expanded fixture now meets the live minimum. Use the
        # secondary company so this test still exercises too few resolved roots.
        company = COMPANIES[1][0]
        log = []
        agent = runner.ScriptedAgent(lambda prompt, call: ABSTAIN, name="injected")
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        argv = [arg.replace("{tmp}", str(self.root)) for arg in spec["command"][3:]]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pinned_live_date():
            code = era.main(argv, agent=agent, environ=self.environ,
                            live_sources=portal_down_sources([]),
                            live_host=lambda: ReaderHost(records, company, log))
        report = json.loads(out.getvalue())
        block, tiers = report["eval"], report["eval"]["tiers"]
        projection = tiers["projection"]
        resolved = projection["roots_resolved"]
        # Each sampled root asked once by the harness (no item lost or doubled); the
        # other company's roots are unresolved for this reader.
        probes = [q for name, q in log[:block["live"]["sampled"]] if name == "plx.find"]
        self.assertEqual(len(probes), block["live"]["sampled"])
        self.assertEqual(projection["roots_sampled"], block["live"]["sampled"])
        self.assertEqual(resolved + projection["roots_unresolved"], projection["roots_sampled"])
        self.assertTrue(0 < resolved < era.MIN_LIVE_ITEMS, projection)
        # An unresolved root gets no agent session and no deterministic run: the agent tier
        # counts only what the reader's projection holds.
        self.assertEqual(block[era.LIVE_PART]["runs"], resolved)
        self.assertEqual(tiers["agent"][era.LIVE_PART]["runs"], resolved)
        self.assertEqual(block["sessions"], resolved + block["exposed"]["runs"]
                         + block["fixture_sample"]["runs"])
        unresolved = set(probes) - {q for q in probes if records[q]["key"]["company_id"]
                                    == company}
        self.assertTrue(unresolved)
        later = " ".join(str(q) for _, q in log[len(probes):])
        self.assertFalse([q for q in unresolved if q in later])
        live_det = tiers["deterministic"][era.LIVE_PART]
        self.assertEqual((live_det["items"], live_det["error"]), (resolved, {}))
        self.assertEqual(live_det["not_run"] + sum(live_det[o] for o in (
            "pass", "fail", "needs_agent", "pin_drift")), resolved)
        self.assertEqual(tiers["deterministic"]["fixture_sample"],
                         block["baseline"]["sample"])
        # Access and capture stand on their own: the Portal strata the outage blocked and
        # the host ledger's sources (here configured none).
        self.assertEqual((tiers["access"]["scope"], tiers["access"]["readers"],
                          tiers["access"]["reader"], tiers["access"]["blocked_strata"]),
                         (era.ACCESS_SCOPE, 1, "named", 4))
        self.assertEqual(set(tiers["capture"]), set(plx_host.SOURCES))
        self.assertFalse(any(entry["configured"] for entry in tiers["capture"].values()))
        self.assertEqual(set(tiers["agent"]), {era.LIVE_PART, *era.PARTS})
        # Too few resolved roots: blocked (missing_bindings), never a pass and never red.
        self.assertEqual(code, 0, block.get("fail_reasons"))
        self.assertIn((era.LIVE_PART, "roots_unresolved", "missing_bindings"),
                      {(p["part"], p.get("error_code"), p["cause"])
                       for p in report["blocked_upstream"]})
        # The abstaining agent's fixture parts ran and are judged on B1's bar: red (nothing
        # answered: coverage 0, accuracy and the unsupported rate null, each a failure).
        self.assertEqual(lv.check_expectations(spec, code, report, spec["expect_blocked"]),
                         ABSTAINING_AGENT_RED)
        self.assertIsNone(report["agent_unmeasured"])  # an agent answered: never blocked whole
        self.assertTrue(lv.check_expectations(spec, code, report))

    def test_the_daily_check_reads_as_the_eval_principal_and_still_measures_fixture_parts(self):
        from verify import live_verify as lv
        agent = runner.ScriptedAgent(lambda prompt, call: ABSTAIN, name="injected")
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        self.assertEqual(lv.validate_spec(spec, stem="eval.real_agent"), [])
        self.assertIn("--live", spec["command"])
        self.assertIn(era.READER_ENV, spec["optional_env"])
        argv = [arg.replace("{tmp}", str(self.root)) for arg in spec["command"][3:]]
        out = io.StringIO()
        environ = {k: v for k, v in self.environ.items() if k != era.READER_ENV}
        with contextlib.redirect_stdout(out):
            code = era.main(argv, agent=agent, environ=environ, live_sources=refuse_reads)
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0, report["eval"].get("fail_reasons"))
        # No reader named: the live part reads as the labelled eval principal, here blocked
        # on the host's config (this fixture's has no employee policy), never on identity.
        self.assertEqual(report["blocked_upstream"][0]["error_code"],
                         "live_host_config_refused:host_config_invalid:mirror_audience")
        self.assertNotIn(era.LIVE_PART, report["eval"])
        self.assertEqual(report["eval"]["fixture_sample"]["runs"], len(era.FIXTURE_SAMPLE))
        access = report["eval"]["tiers"]["access"]
        self.assertEqual((access["reader"], access["readers"]), ("eval_principal", 1))
        self.assertIn("PLX_EVAL_READER", access["a6_permission_proof"])
        self.assertEqual(report["eval"]["live"]["principal"]["kind"], "eval_principal")
        self.assertEqual(report["eval"]["tiers"]["projection"], {"state": "not_run"})
        # The verifier records it blocked (never a pass), its blocked expectation met.
        self.assertTrue(lv.blocked_parts(report))
        # The abstaining agent's fixture parts ran and are judged on B1's bar: red (nothing
        # answered: coverage 0, accuracy and the unsupported rate null, each a failure).
        self.assertEqual(lv.check_expectations(spec, code, report, spec["expect_blocked"]),
                         ABSTAINING_AGENT_RED)
        self.assertIsNone(report["agent_unmeasured"])  # an agent answered: never blocked whole
        self.assertTrue(lv.check_expectations(spec, code, report))

    def test_the_verifier_provisioned_host_runs_the_live_part_and_names_what_it_was_given(
            self):
        """The three host settings provisioned as the live verifier's ``agent_host``
        provisions them (no PLX_EVAL_READER, as on the verifier; a fresh random secret, a
        state directory, a host config, and the names in ``AGENT_PROVISIONED_ENV``): the
        live part runs as the eval principal instead of ``session_secret_missing``, and
        the result names which settings were provisioned, never the secret."""
        import os
        from verify import live_verify as lv
        from evals.oracle.fixture_world import COMPANIES
        from operating.tests import test_session_identity as tsi
        from scripts import eval_oracle_dev as dev
        policy, portal = tsi.identity_files(self.root / "ids")
        (self.root / "mirror-audience.json").write_text(json.dumps({
            "schema": "plx-source-mirror-audience/v1", "tenant": "plx", "audiences": {}}))
        self.config.write_text(json.dumps({
            "schema": "plx-host-config/v1", "tenant": "plx",
            "mirror_audience": "mirror-audience.json", "employee_policy": str(policy),
            "portal_access": str(portal)}))
        secret = os.urandom(32).hex()  # as the verifier makes it: fresh, in memory
        provisioned = {si.SESSION_TICKET_SECRET_ENV: secret,
                       era.STATE_ENV: str(self.root / "state"),
                       era.HOST_CONFIG_ENV: str(self.config)}
        self.assertEqual(era.PROVISIONED_ENV, lv.AGENT_PROVISIONED_ENV)
        self.assertEqual(set(era.HOST_SETTINGS), {lv.AGENT_SECRET_ENV, lv.AGENT_STATE_ENV,
                                                  lv.AGENT_CONFIG_ENV})
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        self.assertTrue(set(provisioned) <= set(spec["optional_env"]))
        self.assertIn(spec["id"], lv.AGENT_HOST_CHECKS)
        records = {r["fields"]["number"]: {"key": {"company_id": r["company_id"],
                                                   "id": r["id"]}}
                   for rows in dev.eval_world().bc.values() for r in rows
                   if r["fields"].get("number")}
        agent = runner.ScriptedAgent(lambda prompt, call: ABSTAIN, name="injected")
        argv = [arg.replace("{tmp}", str(self.root)) for arg in spec["command"][3:]]

        def run(environ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), pinned_live_date():
                code = era.main(argv, agent=agent, environ=environ,
                                live_sources=portal_down_sources([]),
                                live_host=lambda: ReaderHost(records, COMPANIES[0][0], []))
            return code, out.getvalue()
        names = ",".join(sorted(provisioned))
        code, text = run({**provisioned, era.PROVISIONED_ENV: names})
        report = json.loads(text)
        codes = [p.get("error_code") for p in report.get("blocked_upstream") or []]
        self.assertNotIn("session_secret_missing", codes)
        self.assertFalse([c for c in codes if str(c).startswith("live_host_")], codes)
        # The live part ran: its items were answered by the agent as the eval principal.
        runs = report["eval"][era.LIVE_PART]["runs"]
        self.assertGreater(runs, 0)
        self.assertEqual(runs, report["eval"]["tiers"]["projection"]["roots_resolved"])
        live = report["eval"]["live"]
        self.assertEqual(live["reader"], "eval_principal")
        self.assertEqual(live["host_provisioned"], sorted(provisioned))
        self.assertNotIn("host_provisioned_refused", live)
        self.assertEqual(report["evidence_label"], "live-captures")
        self.assertNotIn(secret, text)
        # The verifier records the names (enums), never the secret.
        recorded = json.dumps(lv._recorded(spec, report))
        self.assertIn("PLX_WORKBENCH_SESSION_SECRET", recorded)
        self.assertNotIn(secret, recorded)
        # Not the verifier's run: nothing is claimed. A list naming another variable, or a
        # setting this run does not hold, claims nothing (fail closed).
        for marker, held, refused in (
                (None, provisioned, None),
                (names + ",BC_CLIENT_SECRET", {**provisioned, "BC_CLIENT_SECRET": "x"},
                 "provisioned_list_invalid"),
                (names, {k: v for k, v in provisioned.items()
                         if k != era.HOST_CONFIG_ENV}, "provisioned_list_invalid"),
                (f"{era.STATE_ENV},{era.STATE_ENV}", provisioned,
                 "provisioned_list_invalid")):
            with self.subTest(marker=marker):
                environ = {**held, **({era.PROVISIONED_ENV: marker} if marker else {})}
                self.assertEqual(era.host_provisioned(environ), {
                    "host_provisioned": [],
                    **({"host_provisioned_refused": refused} if refused else {})})
        # Without the provisioned secret the part is blocked exactly as before.
        code, text = run({k: v for k, v in provisioned.items()
                          if k != si.SESSION_TICKET_SECRET_ENV})
        report = json.loads(text)
        self.assertEqual([(p["part"], p["error_code"]) for p in report["blocked_upstream"]
                          if p["part"] == era.LIVE_PART],
                         [(era.LIVE_PART, "session_secret_missing")])
        self.assertEqual(report["eval"]["live"]["host_provisioned"], [])

    def test_a_reader_the_host_refuses_is_blocked_before_any_read(self):
        """The real ``operating.host mcp`` start-up: an inactive employee's ticket verifies
        (the session secret signs for anyone) but the host's resolver refuses the reader;
        the check is blocked, never exit 1, and no source is read."""
        from verify import live_verify as lv
        from operating.tests import test_session_identity as tsi
        reader = {"uid": "u-a", "email": tsi.EMAIL_A, "entraTenantId": tsi.TID,
                  "entraObjectId": tsi.OID_A}
        (self.root / "mirror-audience.json").write_text(json.dumps({
            "schema": "plx-source-mirror-audience/v1", "tenant": "plx", "audiences": {}}))

        def host_config(active):
            policy, portal = tsi.identity_files(self.root / f"id-{active}", users=[
                tsi.user_row(tsi.OID_A, tsi.EMAIL_A, isActive=active)])
            self.config.write_text(json.dumps({
                "schema": "plx-host-config/v1", "tenant": "plx",
                "mirror_audience": "mirror-audience.json", "employee_policy": str(policy),
                "portal_access": str(portal)}))
        environ = {**self.environ, era.READER_ENV: json.dumps(reader)}
        host_config(True)  # the active employee starts a session: the refusal is the host's
        self.assertIsNone(era.reader_refusal(era.LiveHost(
            self.root / "state", self.config, reader, SECRET, tenant="plx")))
        host_config(False)
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        argv = [arg.replace("{tmp}", str(self.root)) for arg in spec["command"][3:]]
        agent = runner.ScriptedAgent(lambda prompt, call: ABSTAIN, name="injected")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = era.main(argv, agent=agent, environ=environ, live_sources=refuse_reads)
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0, report["eval"].get("fail_reasons"))
        self.assertEqual([(p["part"], p["error_code"], p["cause"])
                          for p in report["blocked_upstream"]],
                         [(era.LIVE_PART, "reader_refused", "host_misconfiguration")])
        self.assertEqual(report["eval"]["tiers"]["access"]["reader"], "refused")
        # The abstaining agent's fixture parts ran and are judged on B1's bar: red (nothing
        # answered: coverage 0, accuracy and the unsupported rate null, each a failure).
        self.assertEqual(lv.check_expectations(spec, code, report, spec["expect_blocked"]),
                         ABSTAINING_AGENT_RED)
        self.assertIsNone(report["agent_unmeasured"])  # an agent answered: never blocked whole
        self.assertTrue(lv.check_expectations(spec, code, report))
        # A refusal that is not about the reader as an employee (here the ticket names
        # another Entra tenant than the policy's) is a harness fault: an error, never a
        # blocked check.
        host_config(True)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = era.main(argv, agent=agent, environ={**environ, era.READER_ENV: json.dumps(
                {**reader, "entraTenantId": READER["entraTenantId"]})},
                live_sources=refuse_reads)
        self.assertEqual((code, out.getvalue()), (1, ""))
        self.assertEqual(json.loads(err.getvalue()),
                         {"error": "RunnerError",
                          "code": "live_host_identity_refused:tenant_mismatch"})

    def test_an_ended_host_session_names_its_start_up_code_only(self):
        """A host that exits at start-up with a ``HostError`` code names it in the blocked
        reason; any other stderr text (a secret, a traceback) is never reported."""
        class Done:
            def wait(self, timeout=None):
                return 2

        host = era.LiveHost(self.root / "state", self.config, READER, SECRET, tenant="plx")
        host._proc = Done()
        for text, expected in (
                (b'{"error": "host_config_invalid:employee_policy"}\n',
                 "live_host_start_refused:host_config_invalid:employee_policy"),
                (b"session identity refused: tenant_mismatch\n",
                 "live_host_identity_refused:tenant_mismatch"),
                (b"Traceback: boom " + SECRET.encode() + b"\n", "live_host_session_ended"),
                (b"harness_reads.py: error: employee policy is invalid: "
                 b"employee_policy_data_key_unknown\n",
                 "live_host_start_refused:employee_policy_data_key_unknown"),
                (b"harness_reads.py: error: bad value: " + SECRET.encode() + b"\n",
                 "live_host_session_ended"),
                (b'{"error": "x y z secret"}\n', "live_host_session_ended")):
            with tempfile.TemporaryFile() as stderr:
                stderr.write(text)
                host._stderr = stderr
                self.assertEqual(host._ended(), expected)

    def test_the_ask_refuses_a_foreign_store_at_start_up(self):
        """The Ask checks each projection store at start-up, as the console does
        (``case_console.checked_store_status``): the live host's store projected from
        another mirror generation blocks the live part before any source is read or any
        session starts; the fixture sample's sessions run, each workspace checking its
        store again as the console checks each workspace it builds."""
        from verify import live_verify as lv
        from integrations.source_mirror import AUDIENCE_SCHEMA, SourceMirror
        from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE
        from operating.host import projection_path
        from operator_ui import case_console
        from operator_ui import object_workspace as objects
        audience = self.root / "mirror-audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {}}))

        def mirror(state):
            (state / CASE_RUNTIME_DIR).mkdir(parents=True, exist_ok=True)
            SourceMirror(state / CASE_RUNTIME_DIR / MIRROR_FILE, None, audience,
                         tenant="plx").close()
        state = self.root / "state"
        mirror(self.root / "elsewhere")
        mirror(state)
        projection_path(state).parent.mkdir(parents=True, exist_ok=True)
        objects.project_mirror(self.root / "elsewhere", projection_path(state))
        probes = []
        spec = json.loads(ASK_SPEC.read_text(encoding="utf-8"))
        argv = [arg.replace("{tmp}", str(self.root)) for arg in spec["command"][3:]]
        out = io.StringIO()
        checked = mock.patch.object(case_console, "checked_store_status",
                                    wraps=case_console.checked_store_status)
        with checked as check, contextlib.redirect_stdout(out):
            code = era.main(argv, planner=lambda request: (
                {"action": "escalate", "reason": "test"}, None), environ=self.environ,
                live_sources=refuse_reads, live_host=lambda: ReaderHost({}, "c", probes))
        report = json.loads(out.getvalue())
        self.assertEqual([(p["part"], p["error_code"], p["cause"])
                          for p in report["blocked_upstream"]],
                         [(era.LIVE_PART, "ontology_store_refused", "host_misconfiguration")])
        self.assertEqual(report["blocked_upstream"][0]["store_state"], "foreign")
        self.assertEqual((probes, report["eval"]["tiers"]["projection"]),
                         ([], {"state": "not_run"}))
        self.assertNotIn(era.LIVE_PART, report["eval"])
        ask = report["eval"]["fixture_sample"]["ask"]
        self.assertEqual((ask["sessions"] + ask["roots_withheld"], ask["sessions_failed"]),
                         (len(era.FIXTURE_SAMPLE), 0))
        # The live host's store first, then the fixture build's, then each session's
        # workspace (``object_workspace_for``) the fixture build's again.
        stores = [Path(call.args[0]) for call in check.call_args_list]
        self.assertEqual(stores[:1], [projection_path(state)])
        self.assertEqual(len(stores), 2 + ask["sessions"])
        self.assertEqual(len(set(stores[1:])), 1)
        self.assertNotIn(projection_path(state), stores[1:])
        # The abstaining agent's fixture parts ran and are judged on B1's bar: red (nothing
        # answered: coverage 0, accuracy and the unsupported rate null, each a failure).
        self.assertEqual(lv.check_expectations(spec, code, report, spec["expect_blocked"]),
                         ABSTAINING_AGENT_RED)
        self.assertIsNone(report["agent_unmeasured"])  # an agent answered: never blocked whole
        self.assertTrue(lv.check_expectations(spec, code, report))
        # An unreadable store fails closed; no store at all cannot serve an Ask either.
        for content, refusal in ((b"not a store", ("ontology_store_refused", "unknown")),
                                 (None, ("ask_needs_a_projection_store", "missing"))):
            projection_path(state).unlink()
            if content is not None:
                projection_path(state).write_bytes(content)
            part, blocked, _ = era.live_part(
                self.environ, self.args(), build_sources=refuse_reads, ask=True,
                make_host=lambda: ReaderHost({}, "c", probes))
            self.assertEqual((part, [(p["error_code"], p["store_state"]) for p in blocked],
                              probes), (None, [refusal], []))

    def test_the_root_probe_stops_at_its_deadline(self):
        """A host that accepts the reader but then stops answering: the probe gives up at
        ``--root-probe-seconds``, every unasked root is not probed (never unresolved), and
        the part is blocked on the projection tier."""
        from evals.oracle.fixture_world import COMPANIES
        made = []

        def make_host():
            if not made:
                made.append(ReaderHost({}, COMPANIES[0][0], []))
                return made[0]
            host = era.LiveHost(self.root / "state", self.config, READER, SECRET,
                                tenant="plx")
            host.harness_server = lambda: {
                "command": sys.executable, "args": ["-c", "import sys; sys.stdin.buffer.read()"],
                "cwd": str(ROOT), "env": {}}
            made.append(host)
            return host
        started = time.monotonic()
        part, blocked, info = era.live_part(self.environ, self.args(root_probe_seconds=1.0),
                                            build_sources=portal_down_sources([]),
                                            make_host=make_host)
        self.assertLess(time.monotonic() - started, 60)
        self.assertIsNone(part)
        projection = info["tiers"]["projection"]
        self.assertEqual((projection["roots_not_probed"], projection["roots_resolved"],
                          projection["roots_unresolved"]), (info["sampled"], 0, 0))
        self.assertIn((era.LIVE_PART, "root_probe_incomplete", "projection_timeout"),
                      {(p["part"], p.get("error_code"), p["cause"]) for p in blocked})
        self.assertNotIn("roots_unresolved", {p.get("error_code") for p in blocked})
        # A host that ends the session mid-probe is the host's fault, not a projection
        # timeout.
        made.clear()
        dead = {"command": sys.executable, "args": ["-c", "pass"], "cwd": str(ROOT), "env": {}}

        def dying_host():
            host = make_host()
            if len(made) > 1:
                host.harness_server = lambda: dead
            return host
        _, blocked, info = era.live_part(self.environ, self.args(),
                                         build_sources=portal_down_sources([]),
                                         make_host=dying_host)
        self.assertEqual(info["tiers"]["projection"]["roots_not_probed"], info["sampled"])
        self.assertIn((era.LIVE_PART, "live_host_session_ended", "host_misconfiguration"),
                      {(p["part"], p.get("error_code"), p["cause"]) for p in blocked})
        self.assertNotIn("projection_timeout", {p["cause"] for p in blocked})

    def test_an_answer_failing_only_on_an_unreadable_bc_citation_is_blocked_not_wrong(self):
        """A BC citation the harness cannot re-read as the reader (``ReaderHost`` re-reads
        nothing): an answer whose only fault is that, on a record the host served the agent
        and the item's chain names, is the access/capture tier's read gap, scored blocked
        by the scorer, on the agent and the deterministic path alike. A wrong value, a
        fabricated handle the host never served, another company's handle, a served record
        off the item's chain and a prefix of a served handle all stay wrong."""
        from evals import baseline as bl
        from evals.oracle.fixture_world import COMPANIES
        from scripts import eval_oracle_dev as dev
        records = {r["fields"]["number"]: {"key": {"company_id": r["company_id"],
                                                   "id": r["id"]}}
                   for rows in dev.eval_world().bc.values() for r in rows
                   if r["fields"].get("number")}
        part, _, _ = era.live_part(
            self.environ, self.args(), build_sources=portal_down_sources([]),
            make_host=lambda: ReaderHost(records, COMPANIES[0][0], []))
        items = part[0][:6]
        variant = {item["question"]["text"]: name for item, name in zip(items, (
            "served", "wrong_value", "fabricated", "other_company", "off_chain", "prefix"))}

        def answer(prompt, call):
            item = next(i for i in items if i["question"]["text"] in prompt)
            name, outcome = variant[item["question"]["text"]], item["known_outcome"]
            root = outcome["chain"][0]["key"]
            served = call("plx.find", {"query": item["root"]["identifier"]})
            handle = served["candidates"][0]["object"]["display"]["origin"]["citation"]
            if name == "fabricated":  # the chain's record, a handle the host never gave
                handle = f"bc://production/{root['company_id']}/salesInvoices/{root['id']}@v"
            elif name == "prefix":  # the served record at a revision the host never gave
                handle = handle[:-1]
            elif name == "other_company":
                handle = handle.replace(root["company_id"], COMPANIES[1][0])
            elif name == "off_chain":  # a record the host served, of another item
                other = items[0]["root"]["identifier"]
                handle = call("plx.find", {"query": other})["candidates"][0]["object"][
                    "display"]["origin"]["citation"]
            values = [{**value, "value": str(value["value"] + (
                n == 0 and name == "wrong_value")),
                "citations": [{"l1_handle": handle, "field_path": "row.number"}]}
                for n, value in enumerate(outcome["values"])]
            return "```json\n" + json.dumps({
                "format": sc.ANSWER_FORMAT, "answerability": outcome["answerability"],
                "text": "As read.", "values": values, "gaps": outcome["gaps"]}) + "\n```"
        report = era.evaluate({era.LIVE_PART: (items, part[1], None, "live-captures")},
                              runner.ScriptedAgent(answer, name="injected"),
                              out=self.root / "runs", budget_seconds=600, max_sessions=None,
                              agent_evidence="injected-transport")
        live = report["eval"][era.LIVE_PART]
        self.assertEqual(live["unreadable_citations"], {"bc": 6})
        self.assertEqual((live["blocked"], live["read_gap"], live["other_blocked"],
                          live["wrong"], live["confident_unsupported"]), (1, 1, 0, 5, 5))
        # One read gap in six runs trips the part's tripwire: the run fails as the dev
        # baseline fails, also when another part's runs dilute the whole run below it,
        # and records the flag that tripped under its own name.
        self.assertTrue(live["blocked_tripwire"])
        for runs in (6, 60):
            diluted = json.loads(json.dumps(report))
            diluted["eval"]["total"]["runs"] = runs
            self.assertEqual(era.verdict(diluted, min_scored=1), 3)
            self.assertEqual(diluted["eval"]["fail_reasons"], ["blocked_tripwire"])
        tiers = era.tier_block(report, {"reader": "named", "readers": 1}, [])
        self.assertEqual((tiers["access"]["scope"], tiers["access"]["readers"],
                          tiers["access"][era.READ_GAP]), (era.ACCESS_SCOPE, 1, 1))
        self.assertEqual(tiers["agent"][era.LIVE_PART]["wrong"], 5)
        # The scorer owns the outcome: the recorded score is blocked under the current
        # scorer version with a hash of what it says, and the deterministic path's run of
        # the same answer is blocked too (counted, in no rate).
        agent = {score["item_id"]: score for score in (
            json.loads(path.read_text()) for path in (self.root / "runs").rglob("*.score.json"))}
        row = bl.run_item(items[0], runner.ScriptedAgent(answer, name="injected"),
                          part[1](items[0]), run_pin=None, evidence_label="live-captures")
        for score in (agent[items[0]["id"]], row["score"]):
            self.assertEqual((score["outcome"], score["scorer"]),
                             ("blocked", sc.SCORER_VERSION))
            self.assertIn(era.READ_GAP, score["codes"])
            self.assertEqual(score["score_sha256"], sc.sha256(
                {k: v for k, v in score.items() if k != "score_sha256"}))
        self.assertEqual((row["outcome"], row["reasons"]), ("blocked", [era.READ_GAP]))
        block = bl.eval_block([row], split="dev", evidence_label="live-captures", run_id="r",
                              items_digest="0" * 16)
        self.assertEqual((block["blocked"], block["dev_set"]), (1, "0/0"))
        self.assertEqual({agent[item["id"]]["outcome"] for item in items[1:]}, {"wrong"})

    def test_the_ask_part_scores_a_scripted_planner_and_a_withheld_root_gets_no_session(self):
        """The daily ``eval.ask_agent`` command on the fixture world (no reader named:
        the live part is blocked), its sample swapped for four items rooted at one sales
        order: a scripted Ask planner's cited answer scores correct, a value the claim gate
        refuses is unsupported (never the answer), an escalation is scored with its stop,
        and a root the reader cannot see gets no session (blocked, never wrong). Each
        session record carries the effective agent definition's stamp and its stop."""
        import copy
        from verify import live_verify as lv
        from operating import agent_definitions
        base = next(i for i in era.sample_items()["fixture_sample"]
                    if i["id"] == "dev-F3-0006-fixture")
        # The handle carries the fixture snapshot the item was drawn from, never a pinned one.
        snapshot = re.search(r"@([0-9a-f]{16})\b", json.dumps(base)).group(1)
        handle = f"api://public.app_sales_order/%7B%22id%22%3A%22fx-so-70%22%7D@{snapshot}"

        def variant(number, text, **changes):
            item = copy.deepcopy(base)
            item.update(id=f"dev-F3-09{number}-fixture", question={"text": text}, **changes)
            item["known_outcome"] = {**item["known_outcome"], "answerability": "answerable",
                                     "gaps": [], "values": [{
                                         "name": "order_number", "value": "FXSO100070",
                                         "citations": [{"l1_handle": handle,
                                                        "field_path": "row.order_number"}]}]}
            return item
        items = [variant(11, "Which order number, as read?"),
                 variant(12, "Which order number, misread?"),
                 variant(13, "Which order number? escalate"),
                 variant(14, "Which order number, withheld?",
                         actor={"principals": ["staff:outsider"]})]
        asked = []
        planner = scripted_ask({"identifier": "FXSO100070", "field": "row.order_number",
                                "pattern": r"api://public\.app_sales_order/",
                                "role": "identifier"},
                               lambda q: "FXSO100071" if "misread" in q else "FXSO100070",
                               asked)
        spec = json.loads(ASK_SPEC.read_text(encoding="utf-8"))
        self.assertEqual(lv.validate_spec(spec, stem="eval.ask_agent"), [])
        self.assertEqual(lv.spec_tier(spec), "real_agent")
        self.assertNotIn("requires_env", spec)  # the login under either name
        self.assertEqual(spec["requires_env_any"], ["CLAUDE_CODE_OAUTH_TOKEN", "PLX_CLAUDE_CODE_OAUTH_TOKEN"])
        argv = [arg.replace("{tmp}", str(self.root)) for arg in spec["command"][3:]]
        environ = {k: v for k, v in self.environ.items() if k != era.READER_ENV}
        out = io.StringIO()
        with mock.patch.object(era, "sample_items",
                               lambda: {"exposed": [], "fixture_sample": items}), \
                contextlib.redirect_stdout(out):
            code = era.main(argv + ["--min-scored", "1"], planner=planner, environ=environ,
                            live_sources=refuse_reads)
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0, report["eval"].get("fail_reasons"))
        self.assertEqual((report["eval"]["agent"], report["agent_evidence"]),
                         ("agent-loop", "injected-transport"))
        self.assertEqual((report["blocked_upstream"][0]["error_code"],
                          report["eval"]["tiers"]["access"]["reader"]),
                         ("live_host_config_refused:host_config_invalid:mirror_audience",
                          "eval_principal"))
        # The withheld root is blocked; the misread is a confident unsupported value the
        # gate refused (answered) and
        # the escalation is unanswered: the sessions that ran miss B1's bar on every rate.
        total = report["eval"]["total"]
        self.assertGreaterEqual(total["gate_refused_answers"], 1)
        self.assertLessEqual(total["confident_unsupported"], total["answered"])
        self.assertEqual(lv.check_expectations(spec, code, report, spec["expect_blocked"]),
                         ["eval.total.accuracy < 0.8", "eval.total.coverage < 0.8",
                          "eval.total.confident_unsupported_rate > 0.02"])
        self.assertIsNone(report["agent_unmeasured"])
        self.assertTrue(lv.check_expectations(spec, code, report))
        self.assertNotIn("FXSO1000", out.getvalue())
        scores = {score["item_id"]: score for score in (
            json.loads(path.read_text()) for path in (self.root / "work" / "runs").rglob(
                "*.score.json"))}
        self.assertEqual([scores[item["id"]]["outcome"] for item in items],
                         ["correct", "wrong", "wrong", "blocked"])
        part = report["eval"]["fixture_sample"]
        stamp = agent_definitions.load().get().stamp()
        self.assertEqual(part["ask"]["agents"], [{key: stamp[key] for key in (
            "id", "version", "digest")}])
        self.assertEqual((part["ask"]["sessions"], part["ask"]["roots_withheld"],
                          part["ask"]["stops"], part["confident_unsupported"]),
                         (3, 1, {"answered": 1, "no_checked_claims": 1,
                                 "planner_escalated": 1}, 1))
        # The withheld root: no planner turn and no tool call of the session.
        self.assertFalse([q for q in asked if "withheld" in q])
        records = {t["item_id"]: t for t in (
            json.loads(path.read_text()) for path in (self.root / "work" / "runs").rglob(
                "*.transcript.json"))}
        withheld = records[items[3]["id"]]
        self.assertEqual((withheld["agent"]["root"], withheld["events"]), ("withheld", []))
        self.assertNotIn("plx/agent", withheld["agent"])
        for item, stop in zip(items, ("answered", "no_checked_claims", "planner_escalated")):
            record = records[item["id"]]["agent"]
            self.assertEqual({key: record["plx/agent"][key] for key in stamp}, stamp)
            self.assertEqual(record["stop"]["class"], stop)

    def test_the_ask_eval_scores_exactly_what_the_workspace_shows(self):
        """One Ask path (``ObjectWorkspace.ask_run``): on every fixture-sample item the eval's
        session and the console's own workspace for the same reader
        (``case_console.object_workspace_for``) give the same outcome and the same shown
        claims. A kept claim citing a record the workspace withholds is shown by neither:
        no value of it is scored, and it is counted beside the score."""
        from ontology.types import Actor
        from operating.case_runtime import CaseRuntime
        from operator_ui import case_console
        from operator_ui import object_workspace as objects
        from scripts import eval_dev_baseline as edb
        items, host_for, _ = era.fixture_parts(self.root / "work")["fixture_sample"]
        self.assertEqual([item["id"] for item in items], list(era.FIXTURE_SAMPLE))
        planner = card_claims

        def both(item):
            host = era.AskHost(host_for(item), item)
            try:
                run = era.AskAgent(planner).run("", host)
                if host.object_id is None:  # no object the employee could pick: no Ask
                    self.assertEqual(run.usage["subtype"], "root_withheld")
                    return None, None
                runtime = CaseRuntime(host.state_dir, client=None, audience=host.audience,
                                      tenant=edb.TENANT)
                view = case_console.object_workspace_for(
                    runtime, host.ontology_store, Actor(edb.TENANT, frozenset(host.principals)),
                    state_dir=host.state_dir, planner=planner)
                era.at_world_now(view.access, host.world_clock)
                try:
                    shown = view.ask({"object_id": host.object_id,
                                      "question": era.ask_question(item)})["answer"]
                finally:
                    view.close()
                    runtime.close()
            finally:
                host.close()
            answer, _ = sc.parse_answer(run.final_text)
            self.assertEqual((run.agent["stop"]["outcome"], run.agent["stop"]["reason"]),
                             (shown["outcome"], shown["reason"]), item["id"])
            # The eval scores the very text the workspace renders from its shown claims.
            self.assertEqual(answer["text"], shown["text"], item["id"])
            self.assertEqual(bool(shown["text"]), bool(shown["claims"]), item["id"])
            self.assertEqual({c["l1_handle"] for v in answer["values"]
                              for c in v.get("citations") or ()},
                             {c["l1_handle"] for claim in shown["claims"]
                              for c in claim["citations"]}, item["id"])
            return run.agent["claims"], answer
        ran = [(item, claims) for item in items for claims in [both(item)[0]] if claims]
        self.assertGreater(len(ran), len(items) // 2)
        self.assertTrue(all(c["kept"] == c["shown"] > 0 and not c["withheld"] for _, c in ran))
        # The loop keeps the claims (its reads resolved); the workspace withholds the cited
        # records (the two decide apart): neither shows them, the eval scores none of them.
        withhold = mock.patch.object(objects.HostAccess, "_decide", staticmethod(
            lambda handle, gated, policy, gate: {"state": "withheld", "reason": "test"}))
        with withhold:
            claims, answer = both(ran[0][0])
            self.assertEqual((claims["shown"], claims["withheld"], answer["values"],
                              answer["answerability"]), (0, claims["kept"], [], "blocked"))
            self.assertGreater(claims["kept"], 0)
            report = era.evaluate(era.ask_parts({"fixture_sample": (
                [item for item, _ in ran[:2]], host_for, None)}),
                                  era.AskAgent(planner), out=self.root / "runs",
                                  budget_seconds=600, max_sessions=None,
                                  agent_evidence="injected-transport", agent_name="agent-loop")
        ask = report["eval"]["fixture_sample"]["ask"]
        self.assertEqual((ask["sessions"], ask["stops"]), (2, {"no_cited_claims": 2}))
        self.assertEqual(ask["withheld_claims"], sum(c["kept"] for _, c in ran[:2]))
        self.assertEqual(report["eval"]["fixture_sample"]["correct"], 0)

    def test_the_ask_workspace_is_the_consoles_own_for_one_host_state(self):
        """One factory (``case_console.object_workspace_for``): for one host state with
        non-default SharePoint capture bounds, the eval's Ask workspace and the console's
        carry the same bounds, store and mirror paths, review store (read only, never
        created: no person reviews) and markings, apart only in the eval's recorder. With
        the host's store projected from another mirror generation both refuse it: the
        console with its refusal, the eval's session blocked with the same store state and
        the session host closed."""
        from datetime import timedelta
        from types import SimpleNamespace
        from integrations.source_mirror import AUDIENCE_SCHEMA, SourceMirror
        from ontology.reviews import STORE_FILE
        from ontology.types import Actor
        from operating import agent_loop
        from operating.build_steps import ReviewsIfPresent
        from operating.case_runtime import CASE_RUNTIME_DIR, CaseRuntime, MIRROR_FILE
        from operator_ui import case_console
        from operator_ui import object_workspace as objects
        audience = self.root / "mirror-audience.json"
        audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                        "audiences": {}}))
        state, captures = self.root / "state", self.root / "sharepoint-capture"
        captures.mkdir()
        for where in (state, self.root / "elsewhere"):
            (where / CASE_RUNTIME_DIR).mkdir(parents=True, exist_ok=True)
            SourceMirror(where / CASE_RUNTIME_DIR / MIRROR_FILE, None, audience,
                         tenant="plx").close()
        store, foreign = self.root / "ontology.sqlite", self.root / "foreign.sqlite"
        objects.project_mirror(state, store)
        objects.project_mirror(self.root / "elsewhere", foreign)
        runtime = CaseRuntime(state, client=None, audience=audience, tenant="plx")
        self.addCleanup(runtime.close)
        actor = Actor("plx", frozenset({"staff:ops"}))
        bounds = {"max_capture_age": timedelta(hours=6), "max_live_rereads": 3,
                  "max_live_gets": 7}   # mcp_server.check_host_arguments' shape
        ask_bounds, planner = agent_loop.Bounds(), card_claims
        closed = []

        def host(served):
            """A host whose session start-up (``mcp_server.SessionHost``) serves ``served``
            with a SharePoint capture under the bounds above."""
            started = SimpleNamespace(
                args=argparse.Namespace(ontology_store=served, state_dir=state,
                                        mail_capture_dir=None,
                                        sharepoint_capture_dir=captures),
                case_runtime=runtime, capture_bounds=bounds,
                reviews=ReviewsIfPresent(state / STORE_FILE),
                close=lambda: closed.append(served))
            return SimpleNamespace(agent_session=lambda: (
                started, SimpleNamespace(actor=actor, close=lambda: None)))

        def wiring(view):
            tools = view.access.host
            return (tools.max_capture_age, tools.max_live_rereads, tools.max_live_gets,
                    tools.sharepoint_capture_dir, view.store_status.store_path.resolve(),
                    view.store_status.mirror_path.resolve(), type(tools.reviews),
                    tools.reviews.path.resolve(), tools.policy_now(actor).markings,
                    view.ask_bounds)

        # The store projected elsewhere: the console refuses it, the eval's session too.
        with self.assertRaises(ValueError) as refused:
            case_console.object_workspace_for(runtime, foreign, actor, state_dir=state)
        self.assertEqual(era.STORE_REFUSED.match(str(refused.exception)).group(1), "foreign")
        item = {"id": "dev-F1-099-fixture", "question": {"text": "What is it?"},
                "as_known_at": {"time": "2026-01-01T00:00:00Z"}}
        asked = era.AskHost(host(foreign), item)
        asked.object_id = "portal:sales_order:1"
        run = era.AskAgent(planner).run("", asked)
        self.assertEqual((run.usage, run.events, run.agent["session"]),
                         ({"is_error": True, "subtype": "ontology_store_refused:foreign"}, [],
                          "ontology_store_refused:foreign"))
        # The store projected here: one wiring, the eval's recorder apart.
        session, ask, access = era.ask_workspace(host(store), planner, ask_bounds)
        console = case_console.object_workspace_for(
            runtime, store, actor, state_dir=state, sharepoint_capture_dir=captures,
            capture_bounds=bounds, planner=planner, ask_bounds=ask_bounds)
        try:
            self.assertIsInstance(access, era._RecordingAccess)
            self.assertIs(ask.access, access)
            self.assertEqual(wiring(ask), wiring(console))
            self.assertEqual(wiring(ask)[:7], (
                timedelta(hours=6), 3, 7, captures.resolve(), store.resolve(),
                objects.mirror_path(state).resolve(), ReviewsIfPresent))
            self.assertEqual(wiring(ask)[7], (state / STORE_FILE).resolve())
        finally:
            ask.close()
            console.close()
            session.close()
        self.assertFalse((state / STORE_FILE).exists())
        self.assertEqual(closed, [foreign, store])

    def test_one_failing_ask_session_blocks_only_itself(self):
        """A session whose start-up the host refuses, one that raises, one whose planner
        fails after a usable reply (a usage limit mid-session) and one whose workspace
        fails to close are each blocked with their code while the run goes on and scores
        the others; every session host started is closed, the failed close's too. A
        withheld root uses no session slot, so a cap of one slot per admitted root
        completes the run."""
        import copy
        from operator_ui import object_workspace as objects
        items, host_for, _ = era.fixture_parts(self.root / "work")["fixture_sample"]
        modes = ("answers", "refused", "raises", "usage_limit", "close_fails", "withheld")
        base = next(i for i in items if i["id"] == "dev-F1-0009-fixture")
        asked = [copy.deepcopy(base) for _ in modes]
        for n, (item, mode) in enumerate(zip(asked, modes)):
            item.update(id=f"dev-F1-09{n}-fixture", question={"text": f"What is it ({mode})?"})
        asked[-1]["actor"] = {"principals": ["staff:outsider"]}
        turns = []

        def planner(request):
            mode = next(m for m in modes if f"({m})" in request["question"])
            turns.append(mode)
            if mode == "raises":
                raise RuntimeError("planner crashed")
            if mode == "usage_limit":  # a usable first reply, then the limit
                if turns.count(mode) > 1:
                    return {"action": "escalate",
                            "reason": "planner_process_failed:usage_limit"}, None
                return {"action": "call", "calls": [{"tool": "plx.find", "arguments": {
                    "query": "usage"}}]}, None
            return card_claims(request)
        session, closed, mode_now = era.agent_session, [], []

        def refusing(host):
            mode = next(m for m in modes if f"({m})" in host.item["question"]["text"])
            if mode == "refused":
                raise runner.RunnerError("live_host_identity_refused:session_ticket_replayed")
            started, server = session(host)
            close = started.close
            started.close = lambda: (closed.append(mode), close())
            mode_now[:] = [mode]
            return started, server
        workspace_close = objects.ObjectWorkspace.close

        def failing_close(workspace):
            workspace_close(workspace)
            if mode_now == ["close_fails"]:
                raise OSError("workspace close failed")
        with mock.patch.object(era, "agent_session", refusing), \
                mock.patch.object(objects.ObjectWorkspace, "close", failing_close):
            report = era.evaluate(era.ask_parts({"fixture_sample": (asked, host_for, None)}),
                                  era.AskAgent(planner), out=self.root / "runs",
                                  budget_seconds=600, max_sessions=len(modes) - 1,
                                  agent_evidence="injected-transport", agent_name="agent-loop")
        block = report["eval"]
        self.assertEqual((block["complete"], block["sessions"], block["skipped"]),
                         (True, len(modes) - 1, 0))
        records = {t["item_id"]: t for t in (json.loads(path.read_text()) for path in (
            self.root / "runs").rglob("*.transcript.json"))}
        scores = {s["item_id"]: s for s in (json.loads(path.read_text()) for path in (
            self.root / "runs").rglob("*.score.json"))}
        self.assertNotEqual(scores[asked[0]["id"]]["outcome"], "blocked")
        self.assertEqual([(scores[i["id"]]["outcome"], records[i["id"]]["usage"]["subtype"])
                          for i in asked[1:]],
                         [("blocked", "live_host_identity_refused:session_ticket_replayed"),
                          ("blocked", "ask_session_failed:RuntimeError"),
                          ("blocked", "planner_process_failed"),
                          ("blocked", "ask_session_failed:OSError"),
                          ("blocked", "root_withheld")])
        self.assertNotIn("withheld", turns)
        self.assertNotIn("refused", turns)
        self.assertEqual(closed, ["answers", "raises", "usage_limit", "close_fails"])
        ask = block["fixture_sample"]["ask"]
        self.assertEqual((ask["sessions"], ask["sessions_failed"], ask["roots_withheld"]),
                         (5, 3, 1))

    def test_an_ask_planner_with_no_usable_reply_is_a_failed_session_never_scored(self):
        """Claude Code starts but answers nothing usable (an ``is_error`` result: not
        logged in): every Ask session is blocked, the run reports every_session_failed and
        the daily check can only record it blocked, never pass it."""
        from verify import live_verify as lv
        from operating import agent_loop
        failed = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                             "result": "Invalid API key - Please run /login"})
        spawned = []
        planner = agent_loop.ClaudeCodePlanner(
            spawn=lambda argv, env, timeout, prompt: spawned.append(1) or failed)
        spec = json.loads(ASK_SPEC.read_text(encoding="utf-8"))
        argv = [arg.replace("{tmp}", str(self.root)) for arg in spec["command"][3:]]
        environ = {k: v for k, v in self.environ.items() if k != era.READER_ENV}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = era.main(argv, planner=planner, environ=environ, live_sources=refuse_reads)
        report = json.loads(out.getvalue())
        part = report["eval"]["fixture_sample"]
        self.assertTrue(spawned)
        self.assertEqual((part["runs"], part["blocked"], report["eval"]["sessions_answered"]),
                         (len(era.FIXTURE_SAMPLE), len(era.FIXTURE_SAMPLE), 0))
        self.assertIn(("agent_session", "every_session_failed"),
                      {(p["part"], p["error_code"]) for p in report["blocked_upstream"]})
        # Nothing measured: the rates fail, but ``blocked_when`` records the run blocked
        # whole on the session (host_misconfiguration), never a pass.
        self.assertEqual(lv.check_expectations(spec, code, report, spec["expect_blocked"]),
                         ["eval.total.accuracy < 0.8", "eval.total.coverage < 0.8",
                          "eval.total.confident_unsupported_rate > 0.02"])
        self.assertEqual(lv.resolve(report, spec["blocked_when"]), ["every_session_failed"])
        self.assertEqual(spec["blocked_cause"], "host_misconfiguration")
        self.assertTrue(lv.check_expectations(spec, code, report))


# The harness session of ``LiveHost`` with the synthetic test mailboxes in the case mail
# scope (``HostCase`` patches it in process; the session runs in its own process).
SYNTHETIC_MAIL_HARNESS = (
    "import sys; from unittest import mock; "
    "from integrations.tests import test_capture_mail as mail; "
    "from operating.case_mail_scope import MailScope; from evals import harness_reads; "
    "mock.patch.object(MailScope, 'builtin', return_value=mail.SYNTHETIC).start(); "
    "sys.exit(harness_reads.main(sys.argv[1:]))")


class AskAnswerTests(unittest.TestCase):
    def test_an_ask_count_claim_is_scored_as_the_count_the_scorer_recomputes(self):
        """A count claim the Ask's gate passed reaches the scorer
        as a calculated value, recomputed from the harness's re-reads (isolated-fixture)."""
        from evals.tests import test_score as ts
        claim = {"text": "The invoice has 2 lines.", "citations": [ts.BC_A, ts.BC_B],
                 "value_claims": [{"value": "2", "kind": "number", "op": "count",
                                   "role": "invoice_line_count",
                                   "of": [{"citation": ts.BC_A}, {"citation": ts.BC_B}]}]}
        for stated, codes in (("2", []),
                              ("3", ["calculation_mismatch", "claim_check_failed"])):
            with self.subTest(stated=stated):
                claimed = {**claim["value_claims"][0], "value": stated}
                value = era.loop_answer({"outcome": "answered", "unknowns": [], "claims": [
                    {**claim, "value_claims": [claimed]}]})["values"][0]
                answer = ts.f4_answer()
                answer["values"] = [value if v["name"] == "invoice_line_count" else v
                                    for v in answer["values"]]
                result = sc.score(*ts.run(item=ts.f4_item(), answer=answer,
                                          host=ts.captured_host()))
                self.assertEqual(ts.codes_of(result, "invoice_line_count"), codes)


    def test_a_cited_answer_off_the_oracle_is_counted_wrong_cited_not_unsupported(self):
        """Round 4: a count claim whose every citation checks out but whose value is not
        the oracle's is ``wrong_cited``; one the claim check refuses stays
        ``confident_unsupported``; the two never overlap and part_summary counts both."""
        from evals.tests import test_score as ts
        cases = (("2", [ts.BC_A, ts.BC_B], "2 lines", "correct", False, False),
                 ("1", [ts.BC_A], "1 line", "wrong", True, False),
                 ("3", [ts.BC_A, ts.BC_B], "2 lines", "wrong", False, True))
        scores = []
        for stated, cited, lines, outcome, cited_wrong, unsupported in cases:
            with self.subTest(stated=stated):
                claimed = {"value": stated, "kind": "number", "op": "count",
                           "role": "invoice_line_count",
                           "of": [{"citation": h} for h in cited]}
                value = era.loop_answer({"outcome": "answered", "unknowns": [], "claims": [
                    {"text": f"The invoice has {stated} lines.", "citations": cited,
                     "value_claims": [claimed]}]})["values"][0]
                answer = ts.f4_answer()
                answer["text"] = answer["text"].replace("across 2 lines", f"across {lines}")
                answer["values"] = [value if v["name"] == "invoice_line_count" else v
                                    for v in answer["values"]]
                result = sc.score(*ts.run(item=ts.f4_item(), answer=answer,
                                          host=ts.captured_host()))
                self.assertEqual(sc.outcome_of(result), outcome)
                self.assertEqual((era.wrong_cited(result), sc.confident_unsupported(result)),
                                 (cited_wrong, unsupported))
                scores.append(result)
        summary = era.part_summary(scores, [{} for _ in scores])
        self.assertEqual((summary["wrong_cited"], summary["confident_unsupported"]), (1, 1))

    def test_an_ask_sum_claim_carries_its_currency_to_the_scorer(self):
        """A sum claim the Ask's gate passed in USD is scored
        as a USD total; one stating no currency is a currency mismatch (isolated-fixture)."""
        from evals.tests import test_score as ts
        claimed = {"value": "1260.50", "kind": "number", "op": "sum", "currency": "USD",
                   "role": "invoiced_total",
                   "of": [{"citation": h, "field": "row.amount"} for h in (ts.BC_A, ts.BC_B)]}
        bare = {k: v for k, v in claimed.items() if k != "currency"}
        for value_claim, codes in ((claimed, []), (bare, ["currency_mismatch"])):
            with self.subTest(currency=value_claim.get("currency")):
                value = era.loop_answer({"outcome": "answered", "unknowns": [], "claims": [
                    {"text": "The invoice totals 1260.50 USD.",
                     "citations": [ts.BC_A, ts.BC_B],
                     "value_claims": [value_claim]}]})["values"][0]
                answer = ts.f4_answer()
                answer["values"] = [value if v["name"] == "invoiced_total" else v
                                    for v in answer["values"]]
                result = sc.score(*ts.run(item=ts.f4_item(), answer=answer,
                                          host=ts.captured_host()))
                self.assertEqual(ts.codes_of(result, "invoiced_total"), codes)


class AnswerRateTests(unittest.TestCase):
    def test_the_same_scores_give_the_same_b1_rates_on_the_agent_and_baseline_paths(self):
        """Decision 2026-10-19-b1-one-answer-rate: ``part_summary`` (the agent tiers) and
        the dev baseline's ``answer_block`` report one coverage, accuracy and
        confident_unsupported_rate (``evals.score.answer_rates``); abstaining everywhere
        is coverage 0 on both, and a rate with no denominator is null."""
        from scripts import eval_dev_baseline as edb

        def score(outcome, *, abstained=False, codes=(), failed=0):
            return {"outcome": outcome, "passed": outcome == "correct",
                    "abstained": abstained, "codes": list(codes), "family": "F3",
                    "claim_checks": {"failed": failed}, "action_calls": 0}
        mixed = ([score("correct")] * 3 + [score("wrong", codes=["unsupported_assertion"])]
                 + [score("wrong", failed=1), score("wrong")]
                 + [score("justified_abstention")] * 2
                 + [score("wrong", abstained=True), score("blocked")])
        keys = ("coverage", "accuracy", "confident_unsupported_rate")
        for name, scores, want in (("mixed", mixed, (0.6, 0.5, 0.3333)),
                                   ("all_abstain", [score("justified_abstention")] * 4,
                                    (0.0, None, None))):
            with self.subTest(name):
                agent = era.part_summary(scores, [])
                era._unsupported_rate(agent)
                rows = [{"outcome": "pass" if s["passed"] else
                         "blocked" if s["outcome"] == "blocked" else "fail", "score": s}
                        for s in scores]
                block = edb.answer_block(rows, 0)
                self.assertEqual(tuple(agent[k] for k in keys), want)
                self.assertEqual((block["coverage_rate"], block["accuracy_rate"],
                                  block["confident_unsupported_rate"]), want)

    def test_a_gate_refused_ask_answer_is_answered_and_unsupported_so_no_rate_exceeds_one(self):
        """The Ask's claim gate refuses a stated answer whole: scored an abstention, it
        counts as answered and confident unsupported both, so the rate is at most 1 and
        errs towards failure; counts that would lift a rate above 1 are refused."""
        from evals import score as scoring
        abstain = {"outcome": "justified_abstention", "passed": False, "abstained": True,
                   "codes": [], "family": "F3", "claim_checks": {"failed": 0},
                   "action_calls": 0}
        correct = dict(abstain, outcome="correct", passed=True, abstained=False)
        summary = era.part_summary([correct, abstain, abstain, abstain], [])
        summary["ask"] = {"confident_unsupported": 3}   # all three abstentions were refused
        era._unsupported_rate(summary)
        self.assertEqual((summary["answered"], summary["confident_unsupported"],
                          summary["gate_refused_answers"]), (4, 3, 3))
        self.assertEqual((summary["coverage"], summary["accuracy"],
                          summary["confident_unsupported_rate"]), (1.0, 0.25, 0.75))
        for counts in ({"runs": 4, "answered": 1, "correct": 1, "unsupported": 3},
                       {"runs": 2, "answered": 3, "correct": 1, "unsupported": 0},
                       {"runs": 4, "answered": 1, "correct": 2, "unsupported": 0}):
            with self.subTest(**counts), self.assertRaisesRegex(
                    ValueError, "answer_rates_inconsistent"):
                scoring.answer_rates(**counts)


class LiveHostCitationTests(th.HostCase):
    """``LiveHost`` over a persistent host's own state (its capture cycle wrote the Portal
    mirror and the BC, mail and SharePoint captures through injected senders; no network
    in the harness session either: it holds no source credential). Two employees: the
    owner of the captured mailbox, and Ricardo, whom the captured SharePoint file's
    permissions name."""

    def setUp(self):
        super().setUp()
        from integrations.tests import test_capture_sharepoint as sp_t
        from operating.tests import test_session_identity as tsi
        policy, portal = tsi.identity_files(self.root / "config" / "readers", users=[
            tsi.user_row(tsi.OID_A, sp_t.RICARDO), tsi.user_row(tsi.OID_B, th.mail_t.UPN_A)])
        self.grant(("bc:production:*", "sharepoint:*", f"mail:{th.mail_t.UPN_A}"))
        self.config = self.write_config(live_audience="reader-audience.json",
                                        employee_policy=str(policy), portal_access=str(portal))
        self.run_cycle()
        self.ricardo = {"uid": "cmuser" + tsi.OID_A[-7:], "email": sp_t.RICARDO,
                        "entraTenantId": tsi.TID, "entraObjectId": tsi.OID_A}
        self.owner = {"uid": "cmuser" + tsi.OID_B[-7:], "email": th.mail_t.UPN_A,
                      "entraTenantId": tsi.TID, "entraObjectId": tsi.OID_B}

    def grant(self, units):
        """The live audience: the capture principal everywhere, the readers' principal on
        ``units``."""
        self.write("reader-audience.json", {
            "schema": "plx-live-source-audience/v1", "tenant": "plx",
            "default_classification": "internal",
            "audiences": {unit: [th.SVC, *(["staff:operations"] if unit in units else [])]
                          for unit in ("bc:production:*", "sharepoint:*", "teams:*",
                                       f"mail:{th.mail_t.UPN_A}")}})

    def live(self, reader):
        excluded = "PLX_EXCLUDED_PEOPLE_OIDS"   # what the served captures are re-checked under
        host = era.LiveHost(self.state, self.config, reader, th.SECRET, tenant="plx",
                            extra_env={excluded: th.ENV[excluded]})
        server = host.harness_server()
        host.harness_server = lambda: {**server, "args": [
            "-c", SYNTHETIC_MAIL_HARNESS, *server["args"][2:]], "env": {
            **server["env"], era.TICKET_ENV: host.ticket()}}
        self.addCleanup(host.close)
        return host

    def handles(self):
        import sqlite3
        from operating.host import projection_path
        with contextlib.closing(sqlite3.connect(projection_path(self.state))) as db:
            cited = [row[0] for row in db.execute("SELECT DISTINCT l1_handle FROM "
                                                  "object_versions ORDER BY l1_handle")]
        def first(prefix, part=""):
            return next(h for h in cited if h.startswith(prefix) and part in h)
        return {"api": first("api://"), "bc": first("bc://", "/purchaseReceiptLines/"),
                "mail": first("graph-mail://"), "sharepoint": first("sharepoint://", "01FILEB"),
                "ricardo_only": first("sharepoint://", "01FILEC")}

    def reference(self, principals):
        """``runner._whole_row`` on the case host as the dev baseline reads captured
        citations (``evals.capture_evidence`` over the same stores and live audience)."""
        from evals import capture_evidence as ce
        from integrations.capture_mail import MailCaptureStore
        from integrations.capture_sharepoint import SharePointCaptureStore
        from integrations.capture_store import CaptureStore
        from integrations.live_source_guard import LiveSourceAudience
        from ontology.types import Actor
        audience = LiveSourceAudience.from_file(self.root / "config" / "reader-audience.json")
        actor = Actor("plx", frozenset(principals), "restricted")
        bc = CaptureStore.open_read_only(self.state / "bc-capture" / "capture.sqlite", audience)
        mail = MailCaptureStore(self.state)
        drive = SharePointCaptureStore.open_read_only(self.state)
        for store in (bc, mail, drive):
            self.addCleanup(store.close)
        case = runner.CaseHost(self.state, self.root / "config" / "mirror-audience.json", "plx",
                               sorted(principals))
        self.addCleanup(case.close)
        return case, ce.CaptureEvidenceHost(case, ce.reader(
            bc=lambda h: bc.resolve(h, actor),
            mail=lambda h: mail.resolve(h, actor, audience, text=True),
            sharepoint=lambda h: drive.resolve(h, actor, audience, text=True)))

    def test_every_scheme_re_reads_as_the_reader_and_withheld_or_changed_fails_closed(self):
        handles = self.handles()
        ricardo, owner = self.live(self.ricardo), self.live(self.owner)
        case, captured = self.reference({"staff:operations", "staff:quality"})
        shape = ("ok", "relation", "key", "row", "page", "text", "provenance")
        # A Portal row as the case host's own host-code read gives it.
        wire, local = runner._whole_row(ricardo, handles["api"]), runner._whole_row(
            case, handles["api"])
        self.assertTrue(wire["ok"], wire)
        self.assertEqual({k: wire[k] for k in ("relation", "key", "row")},
                         {k: local[k] for k in ("relation", "key", "row")})
        # A BC record, a mail and a SharePoint file each reader holds: the row the scorer
        # checks, as the dev baseline's harness reads it, never the live metadata view.
        for reader, scheme in ((ricardo, "bc"), (owner, "bc"), (owner, "mail"),
                               (ricardo, "sharepoint")):
            with self.subTest(scheme):
                wire = runner._whole_row(reader, handles[scheme])
                self.assertIs(wire.get("ok"), True, wire)
                self.assertTrue(wire["row"])
                expected = runner._whole_row(captured, handles[scheme])
                self.assertEqual({k: wire.get(k) for k in shape},
                                 {k: expected.get(k) for k in shape})
        # What the reader may not read stays unreadable: another employee's mailbox, a
        # file whose permissions name only Ricardo, and a record no session catalog lists.
        for reader, scheme in ((ricardo, "mail"), (owner, "sharepoint"),
                               (owner, "ricardo_only")):
            with self.subTest(withheld=scheme):
                self.assertIs(runner._whole_row(reader, handles[scheme]).get("ok"), False)
        self.assertIs(ricardo.read("plx.source_citation", {
            "citation": handles["bc"], "column_offset": 1}).get("ok"), False)
        self.assertNotIn("plx.source_citation", {tool["name"] for tool in ricardo.tools()})
        agent = era.LiveHost(self.state, self.config, self.ricardo, th.SECRET, tenant="plx")
        agent.harness_server = agent.mcp_server   # the agent's own server
        self.addCleanup(agent.close)
        self.assertEqual(agent.tools(), ricardo.tools())
        # Revoked: the grant withdrawn from the live audience; the next session reads
        # nothing of BC or SharePoint.
        self.grant((f"mail:{th.mail_t.UPN_A}",))
        revoked = self.live(self.ricardo)
        for scheme in ("bc", "sharepoint"):
            with self.subTest(revoked=scheme):
                self.assertIs(runner._whole_row(revoked, handles[scheme]).get("ok"), False)
        # Changed: a later capture of the message under a new changeKey; the cited version
        # is no longer current, so it is not support.
        self.router.mail.put(th.mail_t.BOX_A, "m1", "ck2", received=self.router.mail.messages[
            th.mail_t.BOX_A]["m1"]["receivedDateTime"])
        self.clock.advance(60)
        self.run_cycle()
        self.assertIs(runner._whole_row(self.live(self.owner), handles["mail"]).get("ok"),
                      False)

    def test_a_bc_cited_answer_is_scored_on_the_readers_re_read(self):
        """The live part's shape on the host's own state: an answer citing the BC capture
        handle the host served scores on its re-read (correct), with no unreadable
        citation and no read gap, never unsupported by its scheme."""
        from urllib.parse import quote
        from evals.tests import fixtures as fx
        handle = self.handles()["bc"]
        _, captured = self.reference({"staff:operations", "staff:quality"})
        read = runner._whole_row(captured, handle)
        pinned = handle.rsplit("@", 1)[0] + "@" + quote(read["provenance"]["revision"], safe="")
        item = fx.item(id="dev-F3-0902", root={
            "identifier": "RCPT-1", "identifier_kind": "document_number",
            "object_type": "PurchaseReceipt", "relation": "bc.purchaseReceiptLines",
            "key": read["key"]}, known_outcome={
            "answerability": "answerable", "gaps": [], "values": [{
                "name": "received", "value": read["row"]["quantity"],
                "citations": [{"l1_handle": pinned, "field_path": "row.quantity"}]}]})

        def answer(prompt, call):
            self.assertEqual(call("plx.read", {"citation": handle})["status"], "resolved")
            value = read["row"]["quantity"]
            return fx.final_text({
                "format": sc.ANSWER_FORMAT, "answerability": "answerable",
                "text": f"{value} received.", "gaps": [], "values": [{
                    "name": "received", "value": value,
                    "citations": [{"l1_handle": handle, "field_path": "row.quantity"}]}]})
        report = era.evaluate({era.LIVE_PART: ([item], lambda _: self.live(self.ricardo), None,
                                               "live-captures")},
                              runner.ScriptedAgent(answer, name="injected"),
                              out=self.root / "runs", budget_seconds=600, max_sessions=None,
                              agent_evidence="injected-transport")
        live = report["eval"][era.LIVE_PART]
        self.assertEqual((live["runs"], live["correct"], live["read_gap"],
                          live["unreadable_citations"]), (1, 1, 0, {}), live)

    def test_a_card_session_runs_the_investigate_agent_and_lists_the_agenda(self):
        """The case-card runner's host (``eval_case_cards.live_host``) starts both sessions
        with the investigate agent: the harness's catalog (what the runner's tool gate
        admits) lists ``plx.agenda`` read-only, which the default agent's lacks; the agent's
        own server runs the same definition. An unknown agent refuses the session start-up,
        never falls back to the default (isolated-fixture)."""
        import argparse
        from operating import session_identity as si
        from scripts import eval_case_cards as ecc

        def catalog(host):
            server = host.harness_server()
            host.harness_server = lambda: {**server, "args": [
                "-c", SYNTHETIC_MAIL_HARNESS, *server["args"][2:]], "env": {
                **server["env"], era.TICKET_ENV: host.ticket()}}
            self.addCleanup(host.close)
            return {tool["name"]: tool["read_only"] for tool in host.tools()}
        environ = {si.SESSION_TICKET_SECRET_ENV: th.SECRET, era.STATE_ENV: str(self.state)}
        make, blocked = ecc.live_host(environ, argparse.Namespace(host_config=str(self.config)),
                                      self.owner)
        self.assertIsNone(blocked)
        card_host = make()
        args = card_host.mcp_server()["args"]
        self.assertEqual(args[args.index("--agent") + 1], "plx.agent.investigate")
        cards = catalog(card_host)
        default = catalog(era.LiveHost(self.state, self.config, self.owner, th.SECRET,
                                       tenant="plx"))
        self.assertIs(cards.get("plx.agenda"), True)
        self.assertNotIn("plx.agenda", default)
        self.assertIn("plx.agenda", runner.allowed_tools(
            [{"name": n, "read_only": r} for n, r in cards.items()])[0])
        # Every read-only tool the default session lists stays listed to the card session.
        self.assertLessEqual({n for n, r in default.items() if r}, set(cards))
        with self.assertRaises(runner.RunnerError) as refused:
            catalog(era.LiveHost(self.state, self.config, self.owner, th.SECRET,
                                 tenant="plx", agent="plx.agent.nobody"))
        self.assertIn("agent_unknown", str(refused.exception))

    def test_the_ask_session_is_the_hosts_own_server_for_the_named_reader(self):
        """The Ask on the persistent host's own state: the harness's ``plx.find`` roots the
        session at the BC receipt, the session is the host's MCP server for the named
        reader in process (its start-up ticket never left in the environment), and a
        cited BC answer scores correct on the reader's re-read; once the reader's BC grant
        is withdrawn the root is withheld and no planner turn runs."""
        import os
        from urllib.parse import quote
        from evals.tests import fixtures as fx
        handle = self.handles()["bc"]
        _, captured = self.reference({"staff:operations", "staff:quality"})
        read = runner._whole_row(captured, handle)
        pinned = handle.rsplit("@", 1)[0] + "@" + quote(read["provenance"]["revision"], safe="")
        item = fx.item(id="dev-F3-0903", root={
            "identifier": "N010000", "identifier_kind": "bc_document_no",
            "object_type": "Receipt", "relation": "bc.purchaseReceipts",
            "key": {"company_id": handle.split("/")[3], "id": read["row"]["documentId"]}},
            known_outcome={"answerability": "answerable", "gaps": [], "values": [{
                "name": "received", "value": read["row"]["quantity"],
                "citations": [{"l1_handle": pinned, "field_path": "row.quantity"}]}]})
        asked = []

        def planner(request):
            """Around the receipt to its lines, the line's card, its cited quantity."""
            asked.append(request["question"])
            seen = [step for step in request["transcript"] if not step.get("seeded")]
            if not seen:
                return {"action": "call", "calls": [{"tool": "plx.around", "arguments": {
                    "from": request["question"].rsplit(": ", 1)[1], "path": [
                        {"link_type": "ReceiptLine.line_of", "direction": "reverse"}]}}]}, None
            result = seen[-1]["observations"][0]["result"]
            if len(seen) == 1:
                line = next(o["object_id"] for o in result["objects"]
                            if read["row"]["id"] in o["object_id"])
                return {"action": "call", "calls": [{"tool": "plx.object",
                                                     "arguments": {"object_id": line}}]}, None
            cited = next(v for v in result["values"] if v.get("quantity_role") == "received")
            quantity, cite = str(cited["value"]), cited["citation"]
            return {"action": "answer", "unknowns": [], "claims": [{
                "text": f"The line's quantity is {quantity}.", "citations": [cite["l1_handle"]],
                "value_claims": [{"value": quantity, "citation": cite["l1_handle"],
                                  "field": cite["field_path"]}]}]}, None

        def ask(reader):
            report = era.evaluate(
                era.ask_parts({era.LIVE_PART: ([item], lambda _: self.live(reader), None,
                                               "live-captures")}),
                era.AskAgent(planner), out=self.root / f"runs-{len(asked)}",
                budget_seconds=600, max_sessions=None, agent_evidence="injected-transport",
                agent_name="agent-loop")
            return report["eval"][era.LIVE_PART]
        live = ask(self.ricardo)
        self.assertEqual((live["runs"], live["correct"], live["unreadable_citations"]),
                         (1, 1, {}), live)
        self.assertIn("(plx object id): Receipt:bc.purchase_receipts:", asked[0])
        self.assertEqual((live["ask"]["sessions"], live["ask"]["stops"]),
                         (1, {"answered": 1}))
        # The receipt's lines came from the shared projection's verified links (AGT-01).
        self.assertEqual(live["ask"]["context_reuse"]["sessions_with_verified_or_reviewed"], 1)
        self.assertNotIn(era.TICKET_ENV, os.environ)
        self.grant((f"mail:{th.mail_t.UPN_A}",))
        turns = len(asked)
        live = ask(self.ricardo)
        self.assertEqual((live["blocked"], live["ask"]["roots_withheld"], len(asked)),
                         (1, 1, turns))

if __name__ == "__main__":
    unittest.main()
