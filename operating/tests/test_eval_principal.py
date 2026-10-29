"""The eval principal (operating.eval_principal): with PLX_EVAL_READER unset the answer
evals read the persistent host as the declared eval principal, labelled in every result;
with it set, as that employee. The host resolves the eval principal from its committed
declaration alone, serves it no action path and no undeclared marking, and it reads only
what its declared roles grant: never an item-ACL'd mail or file, never a Finance file.
Every host here is a real ``operating.host``/``evals.harness_reads`` start-up over a state
directory a capture cycle wrote through injected senders (no network); the agent is an
injected transport, never a session."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
from unittest import mock

from devloop import live_verify as lv
from operating import eval_principal as ep
from operating import host as plx_host
from operating import session_identity as si
from operating.employee_permissions import EmployeePermissionPolicy, EmployeePrincipal
from operating.tests import test_host as th
from operating.tests import test_session_identity as tsi
from scripts import eval_case_cards as cards
from scripts import eval_real_agent as era

SPEC = ep.ROOT / "devloop" / "live-checks" / "eval.reader_coverage.json"
EXCLUDED = "PLX_EXCLUDED_PEOPLE_OIDS"
SYNTHETIC_MAIL_HARNESS = (
    "import sys; from unittest import mock; "
    "from integrations.tests import test_capture_mail as mail; "
    "from operating.case_mail_scope import MailScope; from evals import harness_reads; "
    "mock.patch.object(MailScope, 'builtin', return_value=mail.SYNTHETIC).start(); "
    "sys.exit(harness_reads.main(sys.argv[1:]))")


def card_agent(calls: list):
    """A ClaudeCodeAgent whose injected spawn answers every card with a gap-only partial."""
    from evals import runner, score
    from evals.tests import fixtures as fx
    final = {"format": score.ANSWER_FORMAT, "answerability": "partial", "values": [],
             "gaps": [{"state": "not_ingested", "subject": "UseCase", "reason": "not_served"}],
             "text": "What the served sources say, and what they do not."}

    def spawn(argv, cwd, env, timeout):
        calls.append(argv)
        lines = [{"type": "system", "subtype": "init", "model": "injected"},
                 {"type": "result", "subtype": "success", "result": fx.final_text(final),
                  "usage": {"input_tokens": 1, "output_tokens": 1}}]
        return 0, "\n".join(json.dumps(line) for line in lines) + "\n"
    return runner.ClaudeCodeAgent(spawn=spawn)


class EvalPrincipalHostTest(th.HostCase):
    def setUp(self):
        super().setUp()
        from integrations.tests import test_capture_sharepoint as sp_t
        policy, portal = tsi.identity_files(self.root / "config" / "readers", users=[
            tsi.user_row(tsi.OID_A, sp_t.RICARDO), tsi.user_row(tsi.OID_B, th.mail_t.UPN_A)])
        self.write("reader-audience.json", {
            "schema": "plx-live-source-audience/v1", "tenant": "plx",
            "default_classification": "internal",
            "audiences": {unit: [th.SVC, "staff:operations"]
                          for unit in ("bc:production:*", "sharepoint:*", "teams:*",
                                       f"mail:{th.mail_t.UPN_A}")}})
        self.policy, self.portal_access = policy, portal
        self.config = self.write_config(live_audience="reader-audience.json",
                                        employee_policy=str(policy), portal_access=str(portal))
        self.run_cycle()
        self.owner = {"uid": "cmuser" + tsi.OID_B[-7:], "email": th.mail_t.UPN_A,
                      "entraTenantId": tsi.TID, "entraObjectId": tsi.OID_B}
        self.environ = {si.SESSION_TICKET_SECRET_ENV: th.SECRET,
                        era.STATE_ENV: str(self.state), era.HOST_CONFIG_ENV: str(self.config)}
        self.started: list[list[str]] = []
        original = era.LiveHost.harness_server
        started = self.started

        def harness_server(host):
            server = original(host)
            started.append(server["args"])
            return {**server, "args": ["-c", SYNTHETIC_MAIL_HARNESS, *server["args"][2:]]}
        patcher = mock.patch.object(era.LiveHost, "harness_server", harness_server)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cards(self, *extra, environ, budget="600"):
        cards_dir = self.root / "cards"
        if not cards_dir.exists():
            shutil.copytree(cards.CARDS_DIR / "screening", cards_dir / "screening")
            path = cards_dir / "screening" / cards.CARDS_FILE
            doc = json.loads(path.read_text("utf-8"))
            doc["real_example"]["cards"] = doc["real_example"]["cards"][:2]
            path.write_text(json.dumps(doc), "utf-8")
        out, calls = io.StringIO(), []
        self.started.clear()
        with contextlib.redirect_stdout(out):
            code = cards.main(["run", "--work-dir", str(self.root / "work"), "--all",
                               "--budget-seconds", budget, *extra],
                              agent=card_agent(calls), environ=environ, cards_dir=cards_dir)
        return code, json.loads(out.getvalue()), calls

    def test_without_a_reader_the_cards_run_as_the_labelled_eval_principal(self):
        code, doc, calls = self.cards("--measure-only", environ=self.environ)
        self.assertEqual((code, doc["status"]), (0, "run"), doc.get("blocked_upstream"))
        self.assertGreater(doc["totals"]["run"], 0)
        self.assertEqual(len(calls), doc["totals"]["run"])
        reader = doc["reader"]
        self.assertEqual((reader["kind"], reader["principals"], reader["markings"]),
                         ("eval_principal", ["staff:operations", "staff:sales"], []))
        self.assertTrue(reader["config"].startswith("examples/eval_principal.json@sha256:"))
        self.assertIn("PLX_EVAL_READER", reader["a6_permission_proof"])
        # Every host session the run started (the reader check and each card's) was the
        # eval principal's, resolved from its declaration by the real host start-up.
        self.assertTrue(self.started)
        self.assertTrue(all("--eval-principal" in args for args in self.started))
        # The new live check holds on this output but for the injected transport.
        spec = json.loads(SPEC.read_text("utf-8"))
        self.assertEqual(lv.validate_spec(spec, stem="eval.reader_coverage"), [])
        self.assertEqual(lv.check_expectations(spec, code, doc),
                         ["agent_evidence != expected"])

    def test_a_host_that_never_captured_blocks_the_run_and_no_session_starts(self):
        # The verifier's run on main 1e2f02bd read an empty state dir (no host cycle, no
        # projection): 86 cards abstained and the check read fail. That measures the host,
        # not the reader's coverage: a typed capture gap, never a fail and never a pass.
        empty = self.root / "empty-state"
        empty.mkdir()
        code, doc, calls = self.cards("--measure-only",
                                      environ={**self.environ, era.STATE_ENV: str(empty)})
        self.assertEqual((code, doc["status"], doc["blocked_reason"]),
                         (0, "blocked", "host_captures_absent"))
        self.assertEqual([p["cause"] for p in doc["blocked_upstream"]], ["capture_gap"])
        self.assertEqual((calls, self.started, doc["totals"]["run"]), ([], [], 0))
        spec = json.loads(SPEC.read_text("utf-8"))
        self.assertEqual(lv.check_expectations(spec, code, doc, spec["expect_blocked"]), [])
        self.assertEqual([p["cause"] for p in lv.blocked_parts(doc)], ["capture_gap"])

    def test_a_named_reader_is_used_and_never_replaced_by_the_eval_principal(self):
        named = {**self.environ, era.READER_ENV: json.dumps(self.owner)}
        code, doc, _ = self.cards("--measure-only", environ=named, budget="0")
        self.assertEqual((code, doc["status"], doc["reader"]),
                         (0, "run", {"kind": "employee"}))
        self.assertTrue(self.started)
        self.assertFalse(any("--eval-principal" in args for args in self.started))
        # --eval-principal reads as the eval principal even with a reader named.
        code, doc, _ = self.cards("--eval-principal", environ=named, budget="0")
        self.assertEqual(doc["reader"]["kind"], "eval_principal")
        self.assertTrue(all("--eval-principal" in args for args in self.started))
        # A named but invalid reader blocks; it never falls back to the eval principal.
        code, doc, calls = self.cards(environ={**self.environ, era.READER_ENV: "{}"})
        self.assertEqual((code, doc["status"], doc["blocked_reason"], doc["reader"], calls,
                          self.started),
                         (0, "blocked", "reader_identity_invalid", {"kind": "none"}, [], []))

    def test_the_eval_principal_reads_only_what_its_declared_roles_grant(self):
        from evals import runner
        import sqlite3
        identity = ep.ticket_claims(self.config)
        host = era.LiveHost(self.state, self.config, identity, th.SECRET, tenant="plx",
                            eval_principal=True)
        self.addCleanup(host.close)
        # The owner's host serves its captured mail under the people exclusion it names.
        owner = era.LiveHost(self.state, self.config, self.owner, th.SECRET, tenant="plx",
                             extra_env={EXCLUDED: th.ENV[EXCLUDED]})
        self.addCleanup(owner.close)
        with contextlib.closing(sqlite3.connect(plx_host.projection_path(self.state))) as db:
            cited = [row[0] for row in db.execute(
                "SELECT DISTINCT l1_handle FROM object_versions ORDER BY l1_handle")]

        def first(prefix, part=""):
            return next(h for h in cited if h.startswith(prefix) and part in h)
        # What its roles' audience grants it, it reads as the employee holding them does.
        for handle in (first("api://"), first("bc://", "/purchaseReceiptLines/")):
            with self.subTest(granted=handle.split(":")[0]):
                self.assertIs(runner._whole_row(host, handle).get("ok"), True)
        # A mailbox (the owner's own, which the owner reads) and files whose item
        # permissions name people: the eval principal is no person and reads none.
        mail = first("graph-mail://")
        self.assertIs(runner._whole_row(owner, mail).get("ok"), True)
        for handle in (mail, first("sharepoint://", "01FILEB"),
                       first("sharepoint://", "01FILEC")):
            with self.subTest(withheld=handle.split(":")[0]):
                self.assertIs(runner._whole_row(host, handle).get("ok"), False)
        # Its own agent server (operating.host mcp) lists the catalog the harness reads.
        agent = era.LiveHost(self.state, self.config, identity, th.SECRET, tenant="plx",
                             eval_principal=True)
        agent.harness_server = agent.mcp_server
        self.addCleanup(agent.close)
        names = {tool["name"] for tool in agent.tools()}
        self.assertEqual(names, {tool["name"] for tool in host.tools()})

    def test_the_host_never_serves_it_the_action_path_or_an_undeclared_marking(self):
        from operating import mcp_server
        config = self.write_config(live_audience="reader-audience.json",
                                   employee_policy=str(self.policy),
                                   portal_access=str(self.portal_access), action_path=True,
                                   marking_grants={"finance": ["staff:operations"],
                                                   "pii": ["staff:quality"]})
        host = plx_host.Host(self.state, plx_host.HostConfig.from_file(config))
        employee = plx_host.mcp_arguments(host, session_ticket_env="T")
        argv = plx_host.mcp_arguments(host, session_ticket_env="T", eval_principal=True)
        self.assertIn("--enable-action-path", employee)
        self.assertIn("finance=staff:operations", employee)
        self.assertNotIn("--enable-action-path", argv)
        self.assertFalse([arg for arg in argv if arg.startswith(("finance=", "pii="))])
        self.assertNotIn("--portal-access", argv)
        principal = ep.load()
        claims = ep.ticket_claims(config)

        def start(extra, identity=claims, base=argv):
            ticket = si.mint_session_ticket(
                uid=identity["uid"], email=identity["email"],
                tenant_id=identity["entraTenantId"], object_id=identity["entraObjectId"],
                author=si.AUTHOR_AGENT, secret=th.SECRET)
            err = io.StringIO()
            with mock.patch.dict("os.environ", {"T": ticket,
                                                si.SESSION_TICKET_SECRET_ENV: th.SECRET}), \
                    contextlib.redirect_stderr(err):
                try:
                    return mcp_server.session_server(base + extra)[1], err.getvalue()
                except SystemExit:
                    return None, err.getvalue()
        server, _ = start([])
        self.addCleanup(server.close)
        session = server.session
        self.assertEqual((session.proof, session.describe()["eval_principal"],
                          session.portal_user_id),
                         ("eval_principal", True, None))
        self.assertEqual(session.intent_identity(session.actor)["sub"]["kind"],
                         "eval_principal")
        for extra, identity, reason in (
                (["--enable-action-path"], claims, "eval_principal_action_path_refused"),
                (["--marking-grant", "finance=staff:sales"], claims,
                 "eval_principal_marking:finance"),
                ([], {**claims, "entraObjectId": tsi.OID_B, "email": th.mail_t.UPN_A},
                 "eval_principal_only"),
                (["--fixture"], claims, "--eval-principal takes")):
            with self.subTest(reason):
                refused, err = start(extra, identity)
                self.assertIsNone(refused)
                self.assertIn(reason, err)
        # Without --eval-principal its ticket is an employee's, and no Portal user has it.
        self.assertIsNone(start([], base=employee)[0])
        served, _ = start([], self.owner, base=employee)
        self.assertIsNotNone(served)
        self.addCleanup(served.close)
        # Its resolver grants the declared roles to the sentinel id only: no Portal row
        # (no Scratch), no file clearance (no Finance file), every other id denied.
        resolver = ep.EvalPrincipalResolver(EmployeePermissionPolicy.from_file(self.policy),
                                            principal)
        mine = resolver.resolve(EmployeePrincipal(tsi.TID, principal.object_id))
        self.assertEqual(mine.require_actor().principals,
                         principal.principals | {"employee:" + principal.object_id})
        self.assertEqual((mine.portal_user_id, mine.file_clearance), ("", frozenset()))
        for other in (EmployeePrincipal(tsi.TID, tsi.OID_A),
                      EmployeePrincipal(tsi.OID_A, principal.object_id)):
            self.assertEqual(resolver.resolve(other).denied_reason, "eval_principal_only")


class DeclarationTest(th.HostCase):
    def test_the_committed_declaration_and_anything_else_fails_closed(self):
        principal = ep.load()
        self.assertEqual((sorted(principal.principals), principal.markings),
                         (["staff:operations", "staff:sales"], frozenset()))
        doc = json.loads(ep.CONFIG.read_text("utf-8"))
        for change, code in (({"extra": 1}, "eval_principal_config_invalid"),
                             ({"label": "employee"}, "eval_principal_config_invalid"),
                             ({"markings": ["finance"]},
                              "eval_principal_marking_refused:finance"),
                             ({"markings": ["secret"]}, "eval_principal_markings_invalid"),
                             ({"principals": ["staff:operations", "group:finance"]},
                              "eval_principal_principals_invalid"),
                             ({"principals": ["employee:x"]},
                              "eval_principal_principals_invalid"),
                             ({"principals": ["*"]}, "eval_principal_principals_invalid"),
                             ({"clearance": "restricted"}, "eval_principal_clearance_invalid"),
                             ({"email": "someone@petralabx.com"},
                              "eval_principal_email_invalid")):
            with self.subTest(code=code, change=change):
                path = self.write("eval_principal.json", {**doc, **change})
                with self.assertRaisesRegex(ValueError, f"^{code}$"):
                    ep.load(path)
