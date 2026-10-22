"""MC write opt-in, per-call authorization, checkout, local audit and Retry-After.

Evidence label: injected-transport. No network I/O: every test injects the
sender and the clock. Contracts: docs/reference/mission-control.md
(PLX_MC @ 1fba0833: create-http-server.ts:267-332, actions.ts:431-458,
checkout-repo.ts:27-52, approval-actions.ts:14-56, frontier spec :606) and
vmc-brain-tools.md §0.6, §7.1.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from integrations.http import SourceUnavailable
from integrations.live_source_guard import ReadOnlyGuardError, Secret
from integrations.mc import (CheckoutResult, McAuditLog, MissionControlMcpClient,
                             MissionControlReadClient, WriteAuthorizationLedger,
                             checkout_repo_slug)
from integrations.plx_hub_guard import (APPROVAL_REQUEST_TOOL, BACKOFF_CAP_SECONDS,
                                        MC_APPROVABLE_WRITE_TOOLS, MC_NEVER_TOOLS,
                                        MC_WRITE_AUTHORIZATION_SCHEMA, HubTransport, McConfig,
                                        McWriteAuthorization, UpstreamError, check_mcp_body)


MC_KEY = "mc-test-key-0123456789"
OPERATOR = "cos@petrasoap.com"
APPROVER = "vince@petrasoap.com"
MC = "https://mc.plxcustomer.io"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
CHECKOUT_ID = "dsp_mg7abc12xyz9q"


class FakeSender:
    def __init__(self, responses=None, fn=None):
        self.calls = []
        self.responses = list(responses or [])
        self.fn = fn

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url, dict(headers), body))
        if self.fn is not None:
            return self.fn(method, url, headers, body)
        return self.responses.pop(0)


def ok_json(payload, status=200, headers=None):
    return status, {"content-type": "application/json", **(headers or {})}, json.dumps(payload).encode()


def tool_answer(payload, *, is_error=False):
    def respond(method, url, headers, body):
        result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
        if is_error:
            result["isError"] = True
        return ok_json({"jsonrpc": "2.0", "id": json.loads(body)["id"], "result": result})
    return respond


def make_hub(sender, *, now=NOW):
    sleeps = []
    transport = HubTransport(sender, environ={}, sleep=sleeps.append, jitter=lambda: 1.0,
                             clock=lambda: now)
    return transport, sleeps


_LEDGER_DIR = tempfile.TemporaryDirectory()


def fresh_ledger():
    return WriteAuthorizationLedger(Path(tempfile.mkdtemp(dir=_LEDGER_DIR.name)) / "ledger.jsonl")


def client(sender, *, write_tools=(), approvers=(APPROVER,), audit=None, now=NOW, ledger=None):
    transport, sleeps = make_hub(sender, now=now)
    mc = MissionControlMcpClient(McConfig(MC, Secret(MC_KEY), OPERATOR), hub=transport,
                                 audit=audit, write_tools=write_tools,
                                 write_approvers=approvers if write_tools else (),
                                 write_ledger=(ledger or fresh_ledger()) if write_tools else None)
    return mc, sleeps


def authorize(tool, arguments, *, approved_by=APPROVER, approved_at=NOW - timedelta(minutes=5),
              expires_at=NOW + timedelta(hours=1), evidence="advisory:2026-10-01#call-1"):
    return McWriteAuthorization.for_call(tool, arguments, approved_by=approved_by,
                                         approved_at=approved_at, expires_at=expires_at,
                                         evidence_ref=evidence)


APPROVAL_ARGS = {"taskId": "TASK-9", "reason": "Send PO5447 packet to supplier (reversible: no)"}


class WriteOptInTests(unittest.TestCase):
    def test_writes_are_refused_by_default_before_io_and_audited(self):
        sender = FakeSender(fn=lambda *a: self.fail("no I/O for a refused write"))
        mc, _ = client(sender)
        self.assertEqual(mc.write_tools, frozenset())
        auth = authorize(APPROVAL_REQUEST_TOOL, APPROVAL_ARGS)
        for name in sorted(MC_APPROVABLE_WRITE_TOOLS):
            with self.assertRaises(ReadOnlyGuardError, msg=name):
                mc.call_write_tool(name, APPROVAL_ARGS, authorization=auth)
            with self.assertRaises(ReadOnlyGuardError, msg=name):  # the read door too
                mc.call_tool(name, APPROVAL_ARGS)
        with self.assertRaises(ReadOnlyGuardError):
            mc.checkout_task("TASK-9", "PLX_MC",
                             authorization=authorize("mc_checkout_task",
                                                     {"taskId": "TASK-9", "repo": "petralabx/PLX_MC"}))
        self.assertEqual(sender.calls, [])
        records = mc.audit.records
        self.assertTrue(records)
        self.assertTrue(all(r["kind"] == "write" and r["outcome"] == "refused" for r in records))
        # The guard's default (no authorization) is unchanged: action_path relies on it.
        with self.assertRaises(ReadOnlyGuardError):
            check_mcp_body({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": APPROVAL_REQUEST_TOOL, "arguments": APPROVAL_ARGS}})

    def test_opt_in_is_limited_to_the_pack_write_tools_and_separates_duties(self):
        sender = FakeSender()
        self.assertEqual(set(MC_APPROVABLE_WRITE_TOOLS),
                         {"mc_create_task", "mc_checkout_task", "mc_report_progress",
                          "mc_complete_task", "mc_request_approval"})
        for never in sorted(MC_NEVER_TOOLS):
            with self.assertRaises(ValueError, msg=never):
                client(sender, write_tools={never})
        with self.assertRaises(ValueError):  # MC operator never approves its own writes
            client(sender, write_tools={APPROVAL_REQUEST_TOOL}, approvers=("COS@petrasoap.com",))
        with self.assertRaises(ValueError):  # opt-in without a human approver
            client(sender, write_tools={APPROVAL_REQUEST_TOOL}, approvers=())
        with self.assertRaises(ValueError):
            client(sender, write_tools={APPROVAL_REQUEST_TOOL}, approvers=("sp_mcp_cursor",))
        with self.assertRaises(ValueError):  # a plus-alias of the operator is the operator
            client(sender, write_tools={APPROVAL_REQUEST_TOOL}, approvers=("Cos+wb@petrasoap.com",))
        with self.assertRaises(ValueError):  # no durable single-use ledger -> no writes
            transport, _ = make_hub(sender)
            MissionControlMcpClient(McConfig(MC, Secret(MC_KEY), OPERATOR), hub=transport,
                                    write_tools={APPROVAL_REQUEST_TOOL},
                                    write_approvers=(APPROVER,))
        with self.assertRaises(ValueError):  # an authorization cannot name a NO tool
            authorize("mc_resolve_conflict", {"conflictId": "c1", "resolution": "keep_mc"})
        for bad in ("sp_mcp_cursor", "agent:cos", "not-an-email"):
            with self.assertRaises(ValueError, msg=bad):
                authorize(APPROVAL_REQUEST_TOOL, APPROVAL_ARGS, approved_by=bad)
        with self.assertRaises(ValueError):  # window over 24 h
            authorize(APPROVAL_REQUEST_TOOL, APPROVAL_ARGS, expires_at=NOW + timedelta(days=2))
        with self.assertRaises(ValueError):
            authorize(APPROVAL_REQUEST_TOOL, APPROVAL_ARGS, evidence="")
        self.assertEqual(sender.calls, [])

    def test_checkout_release_tools_are_never_tools(self):
        # PLX_MC @073f192 (TASK-2326) registers mc_release_checkout(s) on HTTP and stdio:
        # they end a lease on MC's dispatch ledger, a write no Workbench path may send.
        sender = FakeSender(fn=lambda *a: self.fail("no I/O for a release tool"))
        mc, _ = client(sender)
        release_args = {"checkoutRef": "dsp_…q9xy", "taskId": "TASK-9", "reason": "stray"}
        batch_args = {"items": [release_args]}
        for name, args in (("mc_release_checkout", release_args),
                           ("mc_release_checkouts", batch_args)):
            self.assertIn(name, MC_NEVER_TOOLS)
            with self.assertRaises(ValueError, msg=name):
                client(sender, write_tools={name})
            with self.assertRaises(ValueError, msg=name):
                authorize(name, args)
            with self.assertRaises(ReadOnlyGuardError, msg=name):
                mc.call_tool(name, args)
            with self.assertRaises(ReadOnlyGuardError, msg=name):
                check_mcp_body({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": name, "arguments": args}})
        self.assertEqual(sender.calls, [])

    def test_authorization_must_name_this_exact_call_in_its_window_once(self):
        payload = {"data": {"taskId": "TASK-9", "gateId": "apg_1", "status": "pending",
                            "inputRequired": True, "link": MC + "/tasks/TASK-9"}}
        sender = FakeSender(fn=tool_answer(payload))
        mc, _ = client(sender, write_tools={APPROVAL_REQUEST_TOOL})
        refused = [
            authorize(APPROVAL_REQUEST_TOOL, {**APPROVAL_ARGS, "reason": "Approve lunch"}),
            authorize(APPROVAL_REQUEST_TOOL, {**APPROVAL_ARGS, "taskId": "TASK-10"}),
            authorize("mc_report_progress", {"taskId": "TASK-9", "notes": "x"}),
            authorize(APPROVAL_REQUEST_TOOL, APPROVAL_ARGS,
                      approved_at=NOW - timedelta(hours=3), expires_at=NOW - timedelta(hours=1)),
            authorize(APPROVAL_REQUEST_TOOL, APPROVAL_ARGS,
                      approved_at=NOW + timedelta(minutes=1), expires_at=NOW + timedelta(hours=1)),
            authorize(APPROVAL_REQUEST_TOOL, APPROVAL_ARGS, approved_by="stephen@petrasoap.com"),
        ]
        for auth in refused:
            with self.assertRaises(ReadOnlyGuardError, msg=auth):
                mc.call_write_tool(APPROVAL_REQUEST_TOOL, APPROVAL_ARGS, authorization=auth)
        self.assertEqual(sender.calls, [])

        auth = authorize(APPROVAL_REQUEST_TOOL, dict(reversed(list(APPROVAL_ARGS.items()))))
        answer = mc.call_write_tool(APPROVAL_REQUEST_TOOL, APPROVAL_ARGS, authorization=auth)
        self.assertEqual(answer["data"]["gateId"], "apg_1")
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(json.loads(sender.calls[0][3])["params"],
                         {"name": APPROVAL_REQUEST_TOOL, "arguments": APPROVAL_ARGS})
        with self.assertRaises(ReadOnlyGuardError):  # single use
            mc.call_write_tool(APPROVAL_REQUEST_TOOL, APPROVAL_ARGS, authorization=auth)
        self.assertEqual(len(sender.calls), 1)
        record = [r for r in mc.audit.records if r["outcome"] == "ok"][0]
        self.assertEqual((record["kind"], record["tool"]), ("write", APPROVAL_REQUEST_TOOL))
        self.assertEqual(len(record["approver_hmac"]), 64)
        self.assertEqual(len(record["authorization_hmac"]), 64)
        self.assertNotIn(APPROVER, json.dumps(mc.audit.records))
        self.assertNotIn(auth.digest, json.dumps(mc.audit.records))  # salted, not plain

    def test_record_form_and_write_traps(self):
        record = {"schema": MC_WRITE_AUTHORIZATION_SCHEMA, "tool": "mc_report_progress",
                  "arguments": {"taskId": "TASK-9", "stage": "qa", "notes": "read-back ok"},
                  "approved_by": APPROVER, "approved_at": "2026-10-01T11:55:00Z",
                  "expires_at": "2026-10-01T13:00:00Z", "evidence_ref": "advisory#2"}
        auth = McWriteAuthorization.from_mapping(record)
        self.assertTrue(auth.names("mc_report_progress", record["arguments"]))
        with self.assertRaises(ValueError):
            McWriteAuthorization.from_mapping({**record, "schema": "other"})

        def body(name, args):
            return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": args}}

        traps = [
            ("mc_report_progress", {"taskId": "TASK-9"}),  # nothing to report (TASK-2560)
            ("mc_report_progress", {"taskId": "TASK-9", "stage": "done"}),
            # A cancel needs task.cancel, which only sp_mcp_cos holds among agent principals
            # (PLX_MC 6d0aa27, TASK-2635); these cases run as a non-COS agent.
            ("mc_report_progress", {"taskId": "TASK-9", "stage": "cancelled", "notes": "n"}),
            ("mc_report_progress", {"taskId": "TASK-9", "stage": "cancelled",
                                    "cancelReason": "obsolete"}),
            # A bearer checkout id never rides on an action gate (MC_WRITE_ARGUMENTS_NOT_SENT).
            ("mc_request_approval", {"taskId": "TASK-9", "reason": "r",
                                     "checkoutId": CHECKOUT_ID}),
            ("mc_create_task", {"title": "t", "bucket": "BKT-A", "idempotencyKey": "wb-1",
                                "reporter": OPERATOR, "assignee": "agent:cos"}),
            ("mc_create_task", {"title": "t", "bucket": "BKT-A", "reporter": OPERATOR}),
            # reporter: z.string().min(1), PLX_MC create-http-server.ts:272 @1fba083
            ("mc_create_task", {"title": "t", "bucket": "BKT-A", "idempotencyKey": "wb-1"}),
            ("mc_complete_task", {"checkoutId": CHECKOUT_ID, "summary": "s",
                                  "verificationCommands": ["python3 -m unittest x"]}),
            ("mc_complete_task", {"checkoutId": CHECKOUT_ID, "summary": "s", "rollback": "r",
                                  "verificationCommands": []}),
            ("mc_checkout_task", {"taskId": "TASK-9"}),  # binds X-MC-Repo unvalidated
            ("mc_checkout_task", {"taskId": "TASK-9", "repo": "tswaterloo/OntologyProject"}),
            ("mc_request_approval", {"taskId": "TASK-9", "reason": "x" * 501}),
            ("mc_request_approval", {"taskId": "TASK-9", "reason": "r", "extra": 1}),
        ]
        for name, args in traps:
            with self.assertRaises(ReadOnlyGuardError, msg=(name, args)):
                check_mcp_body(body(name, args), write_authorization=authorize(name, args), now=NOW)
        ok = {"checkoutId": CHECKOUT_ID, "summary": "s", "rollback": "git revert abc",
              "verificationCommands": ["python3 -m unittest x"]}
        self.assertEqual(check_mcp_body(body("mc_complete_task", ok),
                                        write_authorization=authorize("mc_complete_task", ok),
                                        now=NOW), "mc_complete_task")
        with self.assertRaises(ReadOnlyGuardError):  # no clock -> refused
            check_mcp_body(body("mc_complete_task", ok),
                           write_authorization=authorize("mc_complete_task", ok))

    def test_single_use_is_durable_across_clients_rewording_and_processes(self):
        payload = {"data": {"taskId": "TASK-9", "stage": "qa"}}
        args = {"taskId": "TASK-9", "stage": "qa"}
        record = {"schema": MC_WRITE_AUTHORIZATION_SCHEMA, "tool": "mc_report_progress",
                  "arguments": args, "approved_by": APPROVER,
                  "approved_at": "2026-10-01T11:55:00Z", "expires_at": "2026-10-01T13:00:00Z",
                  "evidence_ref": "advisory#7"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "write-ledger.jsonl"
            first = FakeSender(fn=tool_answer(payload))
            mc1, _ = client(first, write_tools={"mc_report_progress"},
                            ledger=WriteAuthorizationLedger(path))
            mc1.call_write_tool("mc_report_progress", args,
                                authorization=McWriteAuthorization.from_mapping(record))
            self.assertEqual(len(first.calls), 1)
            # A second client on the same ledger, a reload of the
            # persisted record, a reworded-evidence copy, a plus-alias approver,
            # a different expiry: all the same approval, none may send.
            variants = [record, {**record, "evidence_ref": " advisory#7 (copy) "},
                        {**record, "approved_by": "Vince+wb@petrasoap.com"},
                        {**record, "expires_at": "2026-10-01T12:30:00Z"}]
            for variant in variants:
                second = FakeSender(fn=lambda *a: self.fail("approval reused"))
                mc2, _ = client(second, write_tools={"mc_report_progress"},
                                ledger=WriteAuthorizationLedger(path))
                with self.assertRaises(ReadOnlyGuardError, msg=variant):
                    mc2.call_write_tool("mc_report_progress", args,
                                        authorization=McWriteAuthorization.from_mapping(variant))
                self.assertEqual(second.calls, [])
            # A fresh process reading the same file refuses too.
            script = (
                "import json,sys\n"
                "from integrations.mc import WriteAuthorizationLedger\n"
                "from integrations.plx_hub_guard import McWriteAuthorization\n"
                "from integrations.live_source_guard import ReadOnlyGuardError\n"
                "from datetime import datetime, timezone\n"
                "ledger = WriteAuthorizationLedger(sys.argv[1])\n"
                "auth = McWriteAuthorization.from_mapping(json.loads(sys.argv[2]))\n"
                "try:\n"
                "    ledger.consume(auth, at=datetime.now(timezone.utc))\n"
                "except ReadOnlyGuardError as e:\n"
                "    print('refused', e)\n"
                "else:\n"
                "    print('consumed')\n")
            env = {k: v for k, v in os.environ.items()}
            out = subprocess.run([sys.executable, "-c", script, str(path), json.dumps(record)],
                                 capture_output=True, text=True, timeout=60,
                                 cwd=str(Path(__file__).resolve().parents[2]), env=env)
            self.assertEqual(out.stdout.split(" ")[0].strip(), "refused", out.stderr)
            # A different approval (another approval time) is a new use.
            other = {**record, "approved_at": "2026-10-01T11:58:00Z"}
            ok = FakeSender(fn=tool_answer(payload))
            mc3, _ = client(ok, write_tools={"mc_report_progress"},
                            ledger=WriteAuthorizationLedger(path))
            mc3.call_write_tool("mc_report_progress", args,
                                authorization=McWriteAuthorization.from_mapping(other))
            self.assertEqual(len(ok.calls), 1)
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 3)  # header + two uses
            self.assertNotIn("TASK-9", path.read_text())
            self.assertNotIn(APPROVER, path.read_text())
            # A corrupt or missing ledger refuses before I/O.
            path.write_text(path.read_text() + "not json\n")
            bad = FakeSender(fn=lambda *a: self.fail("no I/O on a corrupt ledger"))
            ledger = WriteAuthorizationLedger.__new__(WriteAuthorizationLedger)
            ledger.__dict__.update(mc3._ledger.__dict__)
            mc4, _ = client(bad, write_tools={"mc_report_progress"}, ledger=ledger)
            fresh = {**record, "approved_at": "2026-10-01T11:59:00Z"}
            with self.assertRaises(ReadOnlyGuardError):
                mc4.call_write_tool("mc_report_progress", args,
                                    authorization=McWriteAuthorization.from_mapping(fresh))
            path.unlink()
            with self.assertRaises(ReadOnlyGuardError):
                mc4.call_write_tool("mc_report_progress", args,
                                    authorization=McWriteAuthorization.from_mapping(fresh))
            self.assertEqual(bad.calls, [])

    def test_a_write_is_sent_once_even_on_a_transient_status(self):
        sender = FakeSender([ok_json({}, 503), ok_json({}, 503)])
        mc, sleeps = client(sender, write_tools={APPROVAL_REQUEST_TOOL})
        with self.assertRaises(UpstreamError) as raised:
            mc.call_write_tool(APPROVAL_REQUEST_TOOL, APPROVAL_ARGS,
                               authorization=authorize(APPROVAL_REQUEST_TOOL, APPROVAL_ARGS))
        self.assertEqual((raised.exception.status, raised.exception.attempts, len(sender.calls),
                          sleeps), (503, 1, 1, []))
        self.assertEqual(mc.audit.records[-1]["outcome"], "error")


class CheckoutTests(unittest.TestCase):
    ARGS = {"taskId": "TASK-9", "repo": "petralabx/PLX_MC"}

    def test_checkout_always_passes_the_petralabx_slug(self):
        self.assertEqual(checkout_repo_slug("PLX_MC"), "petralabx/PLX_MC")
        self.assertEqual(checkout_repo_slug("petralabx/skills"), "petralabx/skills")
        for bad in ("tswaterloo/OntologyProject", "evil/PLX_MC", "", "..", "a/b/c", None):
            with self.assertRaises(ValueError, msg=bad):
                checkout_repo_slug(bad)
        answer = {"checkoutId": CHECKOUT_ID, "taskId": "TASK-9",
                  "prBodyLine": "MC-Checkout: " + CHECKOUT_ID, "link": MC + "/tasks/TASK-9",
                  "actor": {"repo": "petralabx/PLX_MC"}}
        sender = FakeSender(fn=tool_answer(answer))
        mc, _ = client(sender, write_tools={"mc_checkout_task"})
        result = mc.checkout_task("TASK-9", "PLX_MC", authorization=authorize("mc_checkout_task",
                                                                                self.ARGS))
        self.assertIsInstance(result, CheckoutResult)
        self.assertEqual((result.status, result.repo, result.pr_body_line),
                         ("checked_out", "petralabx/PLX_MC", "MC-Checkout: " + CHECKOUT_ID))
        self.assertEqual(json.loads(sender.calls[0][3])["params"]["arguments"], self.ARGS)
        self.assertNotIn(CHECKOUT_ID, repr(result))
        self.assertNotIn(CHECKOUT_ID, json.dumps(mc.audit.records))

    def test_mc_canonical_repo_case_is_accepted_and_kept(self):
        answer = {"checkoutId": CHECKOUT_ID, "taskId": "TASK-9",
                  "prBodyLine": "MC-Checkout: " + CHECKOUT_ID, "actor": {"repo": "petralabx/PLX_MC"}}
        mc, _ = client(FakeSender(fn=tool_answer(answer)), write_tools={"mc_checkout_task"})
        result = mc.checkout_task("TASK-9", "plx_mc", authorization=authorize(
            "mc_checkout_task", {"taskId": "TASK-9", "repo": "petralabx/plx_mc"}))
        self.assertEqual((result.status, result.repo), ("checked_out", "petralabx/PLX_MC"))

    def test_stamp_bound_to_another_repo_or_task_is_rejected(self):
        good = {"checkoutId": CHECKOUT_ID, "taskId": "TASK-9",
                "prBodyLine": "MC-Checkout: " + CHECKOUT_ID, "actor": {"repo": "petralabx/PLX_MC"}}
        for bad in ({**good, "actor": {"repo": "tswaterloo/OntologyProject"}},
                    {**good, "taskId": None}, {**good, "taskId": "TASK-10"},
                    {**good, "prBodyLine": "MC-Checkout: pending"},
                    {**good, "checkoutId": "pending"}):
            mc, _ = client(FakeSender(fn=tool_answer(bad)), write_tools={"mc_checkout_task"})
            with self.assertRaises(SourceUnavailable, msg=bad):
                mc.checkout_task("TASK-9", "petralabx/PLX_MC",
                                 authorization=authorize("mc_checkout_task", self.ARGS))

    def test_checkout_held_is_a_typed_result_over_http_and_mcp(self):
        message = ("TASK-9 is checked out on petralabx/PLX_MC by another principal "
                   "until 2026-10-01T19:50:13.000Z.")
        error = {"error": {"code": "checkout_held", "message": message}}
        for sender in (FakeSender([ok_json(error, 409)]),
                       FakeSender(fn=tool_answer(error, is_error=True))):
            mc, sleeps = client(sender, write_tools={"mc_checkout_task"})
            result = mc.checkout_task("TASK-9", "PLX_MC",
                                      authorization=authorize("mc_checkout_task", self.ARGS))
            self.assertEqual((result.status, result.checkout_id, result.pr_body_line,
                              result.held_until, len(sender.calls), sleeps),
                             ("checkout_held", None, None, "2026-10-01T19:50:13.000Z", 1, []))
            self.assertEqual(result.message, message)
            self.assertEqual(mc.audit.records[-1]["outcome"], "held")


class ReadAuditTests(unittest.TestCase):
    def test_read_tools_and_rest_reads_get_a_local_audit_record_without_values(self):
        context = {"data": {"topTasks": [], "buckets": []}, "meta": {"actor": {}}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mc-audit.jsonl"
            audit = McAuditLog(path, clock=lambda: NOW)
            responses = [ok_json({}, 503)]  # one transient answer, then the result

            def respond(method, url, headers, body):
                if responses:
                    return responses.pop(0)
                return tool_answer(context)(method, url, headers, body)
            mc, _ = client(FakeSender(fn=respond), audit=audit)
            mc.get_context(task_ids=["TASK-477"])
            with self.assertRaises(ReadOnlyGuardError):
                mc.call_tool("mc_get_task", {"id": "TASK-1", "stage": "merged"})

            def rest(url, headers):
                return {"data": {"ok": True}, "meta": {"actor": {"repo": "petralabx/PLX_MC"}}}
            MissionControlReadClient(MC_KEY, OPERATOR, "petralabx/PLX_MC", transport=rest,
                                     audit=audit).self_check()
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            salt_mode = (Path(tmp) / "mc-audit.jsonl.salt").stat().st_mode
            # Same log file, same salt: digests stay comparable across restarts.
            reopened = McAuditLog(path, clock=lambda: NOW)
            again = reopened.record(door="mcp", tool="mc_get_context", kind="read",
                                    arguments={"depth": "compact", "taskIds": ["TASK-477"]},
                                    outcome="ok")
            self.assertEqual(again["arguments_hmac"], lines[0]["arguments_hmac"])
            lines = lines[:3]
        self.assertEqual([(r["door"], r["tool"], r["kind"], r["outcome"], r["attempts"])
                          for r in lines],
                         [("mcp", "mc_get_context", "read", "ok", 2),
                          ("mcp", "mc_get_task", "read", "refused", 0),
                          ("rest", "GET /api/cursor/self-check", "read", "ok", 1)])
        self.assertEqual([r["seq"] for r in lines], [1, 2, 3])
        self.assertEqual(lines[0]["argument_names"], ["depth", "taskIds"])
        self.assertEqual(oct(salt_mode & 0o777), "0o600")
        self.assertEqual(lines[0]["at"], NOW.isoformat())
        text = json.dumps(lines)
        for secret_or_value in (MC_KEY, OPERATOR, "TASK-477"):
            self.assertNotIn(secret_or_value, text)


class RetryAfterTests(unittest.TestCase):
    def test_retry_after_http_date_uses_the_injected_clock_and_cap(self):
        ok = tool_answer({"ok": True})
        cases = [(NOW + timedelta(seconds=3), 3.0), (NOW + timedelta(hours=1), BACKOFF_CAP_SECONDS),
                 (NOW - timedelta(seconds=30), 0.5)]
        for when, expected in cases:
            responses = [ok_json({}, 429, {"Retry-After": format_datetime(when, usegmt=True)})]

            def respond(method, url, headers, body):
                return responses.pop(0) if responses else ok(method, url, headers, body)
            mc, sleeps = client(FakeSender(fn=respond))
            self.assertTrue(mc.self_check()["ok"])
            self.assertEqual(sleeps, [expected], when)
        responses = [ok_json({}, 503, {"Retry-After": "garbage"})]

        def respond(method, url, headers, body):
            return responses.pop(0) if responses else ok(method, url, headers, body)
        mc, sleeps = client(FakeSender(fn=respond))
        mc.self_check()
        self.assertEqual(sleeps, [0.5])  # base backoff, jitter=1.0


if __name__ == "__main__":
    unittest.main()
