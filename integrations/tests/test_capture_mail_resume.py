"""Mail capture resumes folders across runs and one bad folder never stalls the stream
(core gap C; gap-8d2fdf5ec18b).

Injected-transport tests over the in-process fake Graph of ``test_capture_mail``
(evidence label ``injected-transport``; no network, no credentials). Live: the
``graph.mail_capture`` check (daily, host state) measures the same on the real tenant.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import tempfile
import unittest
import unittest.mock
from urllib.parse import unquote

from devloop import live_verify as lv
from devloop.tests.spec_verdict import verdict
from integrations import capture_mail, graph_read
from integrations.capture_mail import CaptureScope, MailCapture, MailCaptureStore
from integrations.tests import test_capture_mail as base
from integrations.tests.test_capture_mail import BOX_A, ENV, SCOPE, UPN_A

ROOT = Path(__file__).resolve().parents[2]
SECRET_IN_ERROR = "Quarterly pricing for Contoso widgets -- must never reach a report"


class FailingMessages:
    """Raises on the messages of one mailbox while ``armed``; a stand-in for a store,
    parse or page error the round does not type (its text carries mail content)."""

    def __init__(self, capture: MailCapture, mailbox: str, error: BaseException) -> None:
        self.capture, self.mailbox, self.error, self.armed = capture, mailbox, error, True
        self.original = capture._capture_message
        capture._capture_message = self

    def __call__(self, mailbox_id, folder_id, item, counts):
        if self.armed and mailbox_id == self.mailbox and item["id"] in self.fails_on:
            raise self.error
        return self.original(mailbox_id, folder_id, item, counts)

    fails_on: set[str] = set()


class ResumeTests(base.CaptureCase):
    def test_a_folder_interrupted_mid_round_resumes_from_its_stored_cursor(self):
        # Seven messages, two per page: a process killed while reading page 2 (an
        # interrupt, not an error the round could type) leaves page 1 captured and the
        # cursor at page 2; a new process resumes there, not from the start.
        self.seed(7)
        first = self.capture()
        hook = FailingMessages(first, BOX_A, KeyboardInterrupt())
        hook.fails_on = {"MSG-3"}
        with self.assertRaises(KeyboardInterrupt):
            first.run(SCOPE)
        state = self.store.folder_state(BOX_A, "inbox")
        self.assertIn("$skiptoken=", unquote(state["next_url"]))
        self.assertEqual(state["status"], "in_progress")
        pages_read = len(self.graph.delta_calls())
        self.store.close()
        self.store = MailCaptureStore(self.state, clock=self.clock)   # a new process
        run = self.capture().run(SCOPE)
        folder = self.folder(run)
        self.assertEqual((folder["resumed_from"], folder["round"], folder["complete"]),
                         ("next_url", "initial", True))
        resumed = self.graph.delta_calls()[pages_read:]
        # No second initial query: every read of box A carries a stored skip token.
        a_reads = [c for c in resumed if f"/users/{BOX_A}/" in c[1]]
        self.assertTrue(a_reads and all("$skiptoken=" in unquote(c[1]) for c in a_reads))
        self.assertTrue(all("$filter" not in c[1] for c in a_reads))
        self.assertEqual(self.store.counts(base.READER_A, base.reader_audience())[
            "messages_current"], 7)
        rows = self.store.db.execute("SELECT COUNT(*), COUNT(DISTINCT message_id) "
                                     "FROM messages WHERE mailbox_id=?", (BOX_A,)).fetchone()
        self.assertEqual(tuple(rows), (7, 7))   # no message lost or duplicated

    def test_an_erroring_folder_is_recorded_typed_and_the_next_folder_is_captured(self):
        self.seed(5)
        capture = self.capture()
        hook = FailingMessages(capture, BOX_A, RuntimeError(SECRET_IN_ERROR))
        hook.fails_on = {"MSG-3"}
        run = capture.run(SCOPE)
        bad, good = run["mailboxes"][0]["folders"][0], run["mailboxes"][1]["folders"][0]
        self.assertEqual((bad["status"], bad["complete"], bad["reason"]),
                         ("incomplete", False, "folder_error:RuntimeError"))
        self.assertEqual(bad["cursor"], "next_url")
        self.assertEqual((good["status"], good["complete"]), ("complete", True))
        self.assertEqual(self.store.counts(base.CAPTURE, base.capture_audience())[
            "messages_current"], 2 + 1)   # page 1 of the failing folder, the whole other
        self.assertFalse(run["complete"])
        self.assertEqual((run["complete_bound"], run["bound_total"]), (1, 2))
        self.assertEqual(run["incomplete_reasons"], ["folder_error:RuntimeError"])
        # The cause is a code and a class, never the error's text (mail content).
        self.assertNotIn("Contoso", json.dumps(run))
        self.assertNotIn("Contoso", json.dumps(self.store.folder_state(BOX_A, "inbox")))
        # The stored state says the same, so a later reader sees an incomplete capture.
        completeness = self.store.completeness(base.CAPTURE, base.capture_audience())
        reasons = {m["folders"][0]["reason"] for m in completeness["mailboxes"]
                   if not m["complete"]}
        self.assertEqual(reasons, {"folder_error:RuntimeError"})
        # The error clears: the next run resumes the cursor and completes the folder.
        hook.armed = False
        again = capture.run(SCOPE)
        self.assertTrue(again["complete"])
        self.assertEqual(self.folder(again)["resumed_from"], "next_url")
        self.assertEqual(self.store.counts(base.CAPTURE, base.capture_audience())[
            "messages_current"], 5 + 1)

    def test_a_capture_error_is_typed_by_its_code(self):
        self.seed(3)
        capture = self.capture()
        hook = FailingMessages(capture, BOX_A,
                               capture_mail.CaptureError("attachment_digest_mismatch"))
        hook.fails_on = {"MSG-1"}
        folder = self.folder(capture.run(SCOPE))
        self.assertEqual(folder["reason"], "folder_error:attachment_digest_mismatch")
        self.assertFalse(folder["complete"])

    def test_one_bad_folder_among_n_never_loses_the_others(self):
        # N = 1 / 10 / 60 healthy mailboxes behind a failing one: every healthy folder
        # is captured exactly once, the failing one is reported once.
        for healthy in (1, 10, 60):
            with self.subTest(healthy=healthy), tempfile.TemporaryDirectory() as tmp:
                self.graph = base.FakeGraph()
                self.store.close()
                self.store = MailCaptureStore(Path(tmp) / "state", clock=self.clock)
                self.graph.put(BOX_A, "BAD-1", "CK-1")
                upns = [UPN_A]
                for n in range(1, healthy + 1):
                    box, upn, folder = (f"12345678-aaaa-bbbb-cccc-{n:012d}",
                                        f"box{n}@example-plx.com", f"AAMkIN-{n:03d}=")
                    self.graph.users[box] = {"id": box, "userPrincipalName": upn,
                                             "mail": upn, "proxyAddresses": [f"SMTP:{upn}"]}
                    self.graph.folders[box] = {"inbox": folder, folder.lower(): folder}
                    self.graph.messages[box] = {}
                    self.graph.put(box, f"S-{n}", "CK-1")
                    upns.append(upn)
                scope = CaptureScope([{"address": u, "folders": ["inbox"]} for u in upns],
                                     internal_domains=["example-plx.com"],
                                     allowlist=base.MailScope(upns))
                rights = base.LiveSourceAudience(
                    {f"mail:{u}": ["svc:mail-capture"] for u in upns}, tenant="plx",
                    classifications={"mail:*": "restricted"})
                api, _t = graph_read.build_graph(ENV, sender=self.graph,
                                                 retry=base.BoundedBackoff(
                                                     sleep=self.sleeps.append,
                                                     jitter=lambda: 0.0))
                capture = MailCapture(self.store, graph_read.GraphMailReader(api, rights),
                                      base.CAPTURE, page_size=2, sleep=self.sleeps.append)
                hook = FailingMessages(capture, BOX_A, ValueError("x"))
                hook.fails_on = {"BAD-1"}
                run = capture.run(scope)
                folders = [m["folders"][0] for m in run["mailboxes"]]
                self.assertEqual(sum(f["complete"] for f in folders), healthy)
                self.assertEqual([f["reason"] for f in folders if not f["complete"]],
                                 ["folder_error:ValueError"])
                self.assertEqual((run["complete_bound"], run["bound_total"]),
                                 (healthy, healthy + 1))
                rows = self.store.db.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT message_id) FROM messages "
                    "WHERE message_id LIKE 'S-%'").fetchone()
                self.assertEqual(tuple(rows), (healthy, healthy))

    def test_a_refused_restart_after_an_expired_token_is_typed_not_raised(self):
        # The delta token expires (HTTP 410) and the fresh round's folder lookup is
        # refused: before, the refusal escaped the round and failed the whole run.
        self.seed(3)
        capture = self.capture()
        capture.run(SCOPE)
        state = self.store.folder_state(BOX_A, "inbox")
        self.store.save_folder(BOX_A, "inbox", delta_url=re.sub(
            r"deltatoken=[^&]+", "deltatoken=expired", state["delta_url"]))
        served = self.graph._delta

        def gone_then_refused(user, folder, query, headers):
            answer = served(user, folder, query, headers)
            if answer[0] == 410:   # from now on the folder lookup fails (HTTP 500)
                self.graph.fail_get[r"/v1\.0/users/[^/]+/mailFolders/[^/]+"] = 50
            return answer

        self.graph._delta = gone_then_refused
        run = capture.run(SCOPE)
        folder = run["mailboxes"][0]["folders"][0]
        self.assertFalse(folder["complete"])
        self.assertTrue(folder["reason"].startswith("source_error:"), folder["reason"])
        self.assertEqual(folder["counts"]["round_restarts"], 1)
        # The other mailbox was still read this run.
        self.assertEqual(run["mailboxes"][1]["folders"][0]["round"], "incremental")


class CliCase(unittest.TestCase):
    """``run_cli`` as the live check runs it, over a one-folder scope in host state."""

    def args(self, state_dir, **extra):
        parser = argparse.ArgumentParser()
        capture_mail.add_cli_arguments(parser)
        argv = ["--scope", str(self.scope), "--live-audience", str(self.audience),
                "--runs", "1", "--page-size", "2"]
        if state_dir is not None:
            argv += ["--state-dir", str(state_dir)]
        return parser.parse_args(argv + [f"--{k.replace('_', '-')}={v}"
                                         for k, v in extra.items()])

    def setUp(self) -> None:
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.scope = root / "scope.json"
        self.scope.write_text(json.dumps({
            "schema": "plx-mail-capture-scope/v1", "window_days": 90,
            "mailboxes": [{"address": UPN_A, "folders": ["inbox"]}]}))
        self.audience = root / "audience.json"
        self.audience.write_text(json.dumps({
            "schema": "plx-live-source-audience/v1", "tenant": "plx",
            "default_classification": "restricted",
            "audiences": {f"mail:{UPN_A}": ["svc:mail-capture"]}}))
        self.host = root / "host"
        self.env = {**ENV, "PLX_WORKBENCH_STATE_DIR": str(self.host)}
        self.graph = base.FakeGraph()
        for n in range(1, 6):
            self.graph.put(BOX_A, f"MSG-{n}", f"CK-{n}-1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def invoke(self, ticks=None, hook=None, **extra):
        """``run_cli`` as the live check runs it; ``ticks``: a fake monotonic clock;
        ``hook(capture)``: applied to the MailCapture it builds."""
        real = capture_mail.MailCapture

        def capture(*a, **kw):
            if ticks is not None:
                kw["monotonic"] = lambda: next(ticks)
            built = real(*a, **kw)
            if hook is not None:
                hook(built)
            return built

        with unittest.mock.patch.object(capture_mail, "MailCapture", capture), \
                unittest.mock.patch.object(base.MailScope, "builtin", lambda: base.SYNTHETIC):
            return capture_mail.run_cli(self.args(None, **extra), environ=self.env,
                                        sender=self.graph, sleep=lambda _s: None)



class CliHostStateTests(CliCase):
    """The live check keeps its store in the host state directory across invocations,
    so a budget-cut or interrupted folder resumes on the next daily run."""

    def test_without_a_state_dir_the_second_invocation_carries_on_the_first(self):
        # The time budget (one tick per clock read: the mailbox open, the folder, the
        # page) cuts invocation 1 after its first page; invocation 2, a new
        # process with the same host state, resumes from the stored cursor and ends
        # complete. A {tmp} state directory would have started over.
        first = self.invoke(ticks=iter(range(1000)), budget_seconds=4)
        self.assertEqual((first["storage"]["host_state"], first["storage"]["carried"],
                          first["storage"]["state_base"]),
                         (True, False, "workbench_state_dir"))
        self.assertTrue(first["storage"]["outside_git_tree"])
        self.assertTrue(first["storage"]["under_state_dir"])
        self.assertFalse(first["ok"])
        self.assertEqual(first["runs"][0]["mailboxes"][0]["folders"][0]["reason"],
                         "time_budget")
        second = self.invoke()
        self.assertTrue(second["storage"]["carried"])
        folder = second["runs"][0]["mailboxes"][0]["folders"][0]
        self.assertEqual((folder["resumed_from"], folder["complete"]), ("next_url", True))
        self.assertTrue(second["ok"])
        self.assertEqual(second["summary"]["messages_current"], 5)
        # An injected fixture never shares the live store.
        self.assertIn("injected-transport", os.listdir(self.host / "mail-capture-check"))

    def test_a_host_state_inside_the_git_tree_is_refused(self):
        self.env["PLX_WORKBENCH_STATE_DIR"] = str(ROOT / "scratch-state")
        with self.assertRaises(capture_mail.CaptureError):
            capture_mail.run_cli(self.args(None), environ=self.env, sender=self.graph,
                                 sleep=lambda _s: None)
        self.assertFalse((ROOT / "scratch-state").exists())

    def test_no_state_dir_and_no_host_state_is_refused(self):
        env = {k: v for k, v in self.env.items() if k != "PLX_WORKBENCH_STATE_DIR"}
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                capture_mail.run_cli(self.args(None), environ=env, sender=self.graph,
                                     sleep=lambda _s: None)


class ColdStoreTests(CliCase):
    """One rule over every capture check that resumes across runs
    (capture_mail.budget_cut_block): a run the budget alone cut over a store that was
    not carried is blocked later_ask live-host-persistent-state-dir, with its progress;
    any other gap, or a cut over a carried store, is a typed fail; never a pass. Judged
    on real ``run_cli`` output through the committed graph.mail_capture spec."""

    def spec(self):
        return json.loads((ROOT / "devloop" / "live-checks" / "graph.mail_capture.json")
                          .read_text())

    def judged(self, report):
        return verdict(self.spec(), 0 if report["ok"] else 1, json.loads(json.dumps(report)))

    def test_budget_cut_on_new_store_is_blocked_later_ask(self):
        report = self.invoke(ticks=iter(range(1000)), budget_seconds=3)
        self.assertFalse(report["storage"]["carried"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["runs"][0]["mailboxes"][0]["folders"][0]["reason"],
                         capture_mail.TIME_BUDGET)
        self.assertEqual(report["blocked_upstream"], [{
            "part": "mail.mailbox0.folder0", "source": "microsoft-graph",
            "cause": "later_ask", "later_ask": "live-host-persistent-state-dir"}])
        result, failures = self.judged(report)
        self.assertEqual((result, failures), ("blocked", []))
        self.assertEqual([p["cause"] for p in lv.blocked_parts(report)], ["later_ask"])
        # Progress is shown: what the cut run brought in and what is left.
        self.assertEqual(report["summary"]["bound_total"], 1)
        self.assertGreater(report["summary"]["messages_current"], 0)
        # A leak under the blocked verdict still fails.
        leaked = {**report, "leak_check": {**report["leak_check"], "withheld_actor_rows": 1}}
        self.assertEqual(self.judged(leaked)[0], "fail")

    def test_folder_error_on_new_store_fails_not_blocked(self):
        def arm(capture):
            hook = FailingMessages(capture, BOX_A, RuntimeError(SECRET_IN_ERROR))
            hook.fails_on = {"MSG-3"}

        # One reader thread: the hook replaces the per-message step that path calls.
        report = self.invoke(hook=arm, workers=1)
        self.assertFalse(report["storage"]["carried"])
        self.assertEqual(report["runs"][0]["mailboxes"][0]["folders"][0]["reason"],
                         "folder_error:RuntimeError")
        self.assertEqual(report["blocked_upstream"], [])
        self.assertEqual(self.judged(report)[0], "fail")
        self.assertNotIn(SECRET_IN_ERROR, json.dumps(report))

    def test_budget_cut_on_carried_store_fails_not_blocked(self):
        self.invoke(ticks=iter(range(1000)), budget_seconds=3)
        self.graph.put(BOX_A, "MSG-6", "CK-6-1")
        report = self.invoke(ticks=iter(range(1000)), budget_seconds=3)
        self.assertTrue(report["storage"]["carried"])
        self.assertEqual(report["runs"][0]["mailboxes"][0]["folders"][0]["reason"],
                         capture_mail.TIME_BUDGET)
        self.assertEqual(report["blocked_upstream"], [])
        result, failures = self.judged(report)
        self.assertEqual(result, "fail")
        self.assertTrue(failures)


class LiveCheckSpecTests(unittest.TestCase):
    """graph.mail_capture is in the daily set and resumes (spec fields, then the real
    ``run_cli`` summary through ``check_expectations``)."""

    def spec(self):
        return json.loads((ROOT / "devloop" / "live-checks" / "graph.mail_capture.json")
                          .read_text())

    def test_the_spec_is_daily_validates_and_keeps_host_state(self):
        spec = self.spec()
        self.assertEqual(spec["cadence"], "daily")
        self.assertNotIn("--state-dir", spec["command"])
        self.assertIn("--budget-seconds", spec["command"])
        budget = float(spec["command"][spec["command"].index("--budget-seconds") + 1])
        self.assertLess(budget, spec["timeout_seconds"])
        lv.validate_spec(spec)   # raises SpecError when the verifier would refuse it

    def test_expectations_hold_on_real_run_cli_output_and_fail_when_a_folder_errors(self):
        spec = self.spec()
        cases = CliHostStateTests(
            "test_without_a_state_dir_the_second_invocation_carries_on_the_first")
        cases.setUp()
        self.addCleanup(cases.tearDown)
        # The check's expectations over the real summary of a complete run. The shipped
        # scope names ten or more folders; this fixture names one, so only that
        # assertion is relaxed, nothing else.
        report = cases.invoke(runs=2)
        relaxed = json.loads(json.dumps(spec["expect"]))
        relaxed["json_min"]["summary.folders_complete"] = 1
        document = json.loads(json.dumps(report))
        failures = lv.check_expectations({**spec, "expect": relaxed}, 0, document)
        self.assertEqual(failures, [], failures)
        # A folder that errored is a failed assertion, never a pass.
        document["runs"][-1]["mailboxes"][0]["folders"][0]["complete"] = False
        document["runs"][-1]["mailboxes"][0]["folders"][0]["reason"] = "folder_error:KeyError"
        self.assertTrue(lv.check_expectations({**spec, "expect": relaxed}, 0, document))


if __name__ == "__main__":
    unittest.main()
