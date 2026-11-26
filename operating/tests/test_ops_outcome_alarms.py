"""The outcome alarms (alarms-1115): each measures what a reader gets, not when a job last
ran. Searches ranked without their dense list, the live Ask canary's answer, the truth
check's checked share, the people exclusion's leak count, the mail capture against
Graph's counts and the newest record per source each open an incident in a real alarm
pass (shown in the app, mailed by level) and say why; an unreadable input is unknown or,
where silence is the danger (the leak scan, the canary), a breach. Evidence:
isolated-fixture and injected-transport; no API, Postgres or business system is reached,
nothing is sent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from operating import (ask_canary, exclusion_leak, ops_alarms, ops_health, ops_notify,
                       truth_check)
from operating.tests.test_ops_alarms import Fake, healthy_units, no_send, unit
from operating.tests.test_ops_health import NOW, _db, make_state

T = ops_health.targets({})


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_state(Path(self.tmp.name), delta_built="2030-01-02T13:00:00Z",
                               capture="2030-01-02T11:58:00Z")
        (self.root / "search").mkdir()
        (self.root / "search" / "follow.json").write_text(
            json.dumps({"updated_at": "2030-01-02T11:58:00+00:00"}))
        self.truth(claims=6358, checked=215, contradicted=2, at=NOW - timedelta(hours=1))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def truth(self, *, claims, checked, contradicted, at) -> None:
        body = {"claims": claims, "contradicted": contradicted,
                "generated_at": at.isoformat().replace("+00:00", "Z")}
        if checked is not None:
            body["checked"] = checked
        (self.root / "truth-check" / "status.json").write_text(json.dumps(body))

    def run_pass(self, fake, minutes=0):
        with no_send():
            return ops_alarms.run_pass(self.root, now=NOW + timedelta(minutes=minutes),
                                       environ={}, probes=fake.probes(), dsn="dbname=x")

    @staticmethod
    def check(status, name):
        return next((c for c in status["checks"] if c["name"] == name), None)


class DenseSearchTest(Base):
    def test_searches_ranked_without_the_dense_list_open_an_incident_with_the_reasons(self):
        fake = Fake()
        fake.notes = ([["dense_failed:QueryCanceled"]] * 7 + [["dense_unavailable:EmbedTimeout",
                                                               "rerank_off"]] * 3
                      + [["reranker_timeout"]] * 20)
        status = self.run_pass(fake)
        c = self.check(status, "search_dense_failing")
        self.assertEqual((c["state"], c["value"]), ("breach", 0.333))
        self.assertIn("10 of the last 30 searches", c["line"])
        self.assertIn("QueryCanceled 7", c["line"])
        self.assertIn("EmbedTimeout 3", c["line"])
        self.assertIn("search_dense_failing", {i["key"] for i in status["open"]})

    def test_few_searches_or_an_unreadable_log_are_unknown_and_a_healthy_log_is_ok(self):
        fake = Fake()
        fake.notes = [["dense_failed:QueryCanceled"]] * 5
        self.assertEqual(self.check(self.run_pass(fake), "search_dense_failing")["state"],
                         "unknown")
        fake.notes = [["rerank_off"]] * 40 + [["dense_failed:X"]] * 2
        self.assertEqual(self.check(self.run_pass(fake, 5), "search_dense_failing")["state"],
                         "ok")

        def broken(dsn):
            raise OSError("no log")
        probes = ops_alarms.Probes(search_notes=broken)
        out = ops_alarms.dense_check("dbname=x", T, probes)
        self.assertEqual(out[0]["state"], "unknown")
        self.assertEqual(ops_alarms.dense_check(None, T, probes), [])


class TruthCoverageTest(Base):
    def test_the_contradiction_share_is_over_checked_claims_with_the_checked_share_beside(self):
        report = ops_health.collect(self.root, now=NOW, environ={}, include_api=False)
        c = next(c for c in report["checks"] if c["name"] == "truth_contradicted_share")
        self.assertEqual(c["value"], round(2 / 215, 3))
        self.assertIn("215 of 6358 claims checked (3.4%)", c["detail"])
        # a report from before the count: over all claims, said so
        self.truth(claims=100, checked=None, contradicted=5, at=NOW)
        c = next(c for c in ops_health.collect(self.root, now=NOW, environ={},
                                               include_api=False)["checks"]
                 if c["name"] == "truth_contradicted_share")
        self.assertEqual(c["value"], 0.05)
        self.assertIn("checked count not reported", c["detail"])

    def test_the_truth_check_reports_how_many_claims_a_rule_judged(self):
        out = {"generated_at": "x", "as_of": "d", "detectors": [], "results": [
            {"verdict": "supported", "contradicted": False, "object_id": "a"},
            {"verdict": "expired", "contradicted": True, "object_id": "b"},
            {"verdict": "not_checked", "contradicted": False, "object_id": "c"}]}
        status = truth_check.status_of(out)
        self.assertEqual((status["claims"], status["checked"], status["contradicted"]),
                         (3, 2, 1))

    def test_a_checked_share_under_the_floor_or_far_under_its_week_best_opens(self):
        c = self.check(self.run_pass(Fake()), "truth_checked_share")
        self.assertEqual(c["state"], "ok")
        self.assertIn("215 of 6358 claims checked (3.4%", c["line"])
        self.truth(claims=1000, checked=300, contradicted=0, at=NOW + timedelta(minutes=50))
        self.assertEqual(self.check(self.run_pass(Fake(), 60), "truth_checked_share")["state"],
                         "ok")
        # a later report judging a third as many: above the floor, under half its best
        self.truth(claims=1000, checked=100, contradicted=0, at=NOW + timedelta(minutes=110))
        status = self.run_pass(Fake(), 120)
        c = self.check(status, "truth_checked_share")
        self.assertEqual(c["state"], "breach")
        self.assertIn("100 of 1000 claims checked (10.0%; week's best 30.0%", c["line"])
        self.assertIn("truth_checked_share", {i["key"] for i in status["open"]})
        # under the floor whatever came before
        held = {}
        self.truth(claims=10000, checked=100, contradicted=0, at=NOW)
        self.assertEqual(ops_alarms.truth_checked_check(self.root, NOW, T, held)["state"],
                         "breach")

    def test_a_stale_or_unreadable_report_is_never_fine(self):
        self.truth(claims=6358, checked=215, contradicted=2, at=NOW - timedelta(hours=9))
        c = ops_alarms.truth_checked_check(self.root, NOW, T, {})
        self.assertEqual(c["state"], "breach")
        self.assertIn("9 h old", c["detail"])
        self.truth(claims=6358, checked=None, contradicted=2, at=NOW)
        self.assertEqual(ops_alarms.truth_checked_check(self.root, NOW, T, {})["state"],
                         "unknown")
        (self.root / "truth-check" / "status.json").write_text("{")
        self.assertEqual(ops_alarms.truth_checked_check(self.root, NOW, T, {})["state"],
                         "unknown")


class Transport:
    """The Workbench API, injected: records each request, answers from a script."""

    def __init__(self, answer=None, *, me=200, login_cookie="plx_session=s1; Path=/api",
                 login_status=200, auth_config=None,
                 ask_status=200, error=None, steps=("understanding", "answering")):
        self.calls, self.answer, self.me, self.login_cookie = [], answer, me, login_cookie
        self.login_status, self.auth_config = login_status, auth_config
        self.bodies = []
        self.ask_status, self.error, self.steps = ask_status, error, steps

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("8780", 1)[1]
        self.calls.append((method, path, dict(headers)))
        self.bodies.append(body)
        if path == "/api/me":
            return self.me, {}, iter(())
        if path == "/api/auth/config":
            if self.auth_config is None:
                return 200, {}, iter((b'{"mode": "dev", "dev_login": true}',))
            return 200, {}, iter((json.dumps(self.auth_config).encode(),))
        if path == "/api/auth/dev-login":
            return self.login_status, {"set-cookie": self.login_cookie}, iter(())
        lines = []
        for step in self.steps:
            lines += [b"event: status\n", f'data: {{"step": "{step}"}}\n'.encode(), b"\n"]
        if self.error:
            lines += [b"event: error\n", f'data: {{"code": "{self.error}"}}\n'.encode(), b"\n"]
        elif self.answer is not None:
            lines += [b"event: answer\n", ("data: " + json.dumps(self.answer) + "\n").encode(),
                      b"\n"]
        return self.ask_status, {}, iter(lines)


class Clock:
    def __init__(self, step=1.0):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


class AskCanaryTest(Base):
    QUESTION = "What colour is the sample widget?"

    def setUp(self) -> None:
        super().setUp()
        self.cfg = Path(self.tmp.name) / "ask-canary.json"
        self.cfg.write_text(json.dumps({"question": self.QUESTION, "expect": ["Teal Blue"],
                                        "max_seconds": 60}))
        self.mints = 0

    def mint(self):
        self.mints += 1
        return "ticket-x"

    def canary(self, transport, step=1.0):
        return ask_canary.Canary(self.root, transport=transport,
                                 mint=self.mint, clock=Clock(step))

    def ask(self, transport, minutes=0, step=1.0):
        return ask_canary.run(self.root, self.cfg,
                              now=NOW + timedelta(minutes=minutes),
                              canary=self.canary(transport, step))

    @staticmethod
    def answer(text, outcome="answered", facts=()):
        return {"direct_answer": text, "outcome": outcome, "latency_ms": 900,
                "facts": [{"text": f, "sources": []} for f in facts]}

    def test_a_right_answer_passes_and_its_cost_is_measured_without_its_words(self):
        status = self.ask(Transport(self.answer("It is teal   blue.")))
        run = status["runs"][0]
        self.assertEqual((run["ok"], run["verdict"], run["model"]), (True, "ok", True))
        self.assertEqual(run["steps"], ["understanding", "answering"])
        kept = (self.root / "ask-canary" / "status.json").read_text()
        self.assertNotIn("teal", kept.lower())
        self.assertNotIn(self.QUESTION, kept)
        self.assertEqual(oct((self.root / "ask-canary" / "status.json").stat().st_mode)[-3:],
                         "600")
        # a fact the answer cites counts too
        status = self.ask(Transport(self.answer("See the record.", facts=["Teal Blue"])), 20)
        self.assertTrue(status["runs"][0]["ok"])

    def test_wrong_missing_refused_slow_and_failed_answers_each_fail(self):
        cases = [(Transport(self.answer("It is red.")), 1.0, "wrong"),
                 (Transport(self.answer("")), 1.0, "missing"),
                 (Transport(self.answer("Teal Blue", outcome="refused")), 1.0, "missing"),
                 (Transport(None), 1.0, "missing"),
                 (Transport(self.answer("Teal Blue")), 30.0, "slow"),
                 (Transport(error="busy"), 1.0, "error:busy"),
                 (Transport(ask_status=500), 1.0, "error:http_500")]
        for transport, step, verdict in cases:
            run = self.ask(transport, step=step)["runs"][0]
            self.assertEqual((run["ok"], run["verdict"]), (False, verdict), verdict)

    def test_the_session_is_reused_and_signed_in_again_only_when_refused(self):
        t = Transport(self.answer("Teal Blue"), me=401)
        self.ask(t)                      # no session yet: one sign-in
        self.assertEqual(self.mints, 1)
        sent = [h.get("Cookie") for m, p, h in t.calls if p == "/api/ask"]
        self.assertEqual(sent, ["plx_session=s1"])
        # a fresh conversation each run: no conversation id, no history (no earlier canary
        # answer can shape this one)
        self.assertEqual(json.loads(t.bodies[-1]), {"question": self.QUESTION})
        t2 = Transport(self.answer("Teal Blue"), me=200)
        self.ask(t2, 20)                 # the kept session answers /api/me: no mint
        self.assertEqual(self.mints, 1)
        self.assertNotIn("/api/auth/dev-login", [p for m, p, h in t2.calls])
        self.ask(Transport(self.answer("x"), me=200, ask_status=401), 40)
        self.assertFalse((self.root / "ask-canary" / "session").exists())
        self.mints_before = self.mints
        run = ask_canary.run(self.root, self.cfg, now=NOW,
                             canary=ask_canary.Canary(
                                 self.root,
                                 transport=Transport(self.answer("Teal Blue")),
                                 mint=lambda: None, clock=Clock()))["runs"][0]
        self.assertEqual(run["verdict"], "error:sign_in_failed")

    def test_the_alarm_opens_on_two_failed_runs_in_a_row_and_closes_on_answers(self):
        self.ask(Transport(self.answer("It is red.")))
        c = ops_alarms.canary_check(self.root, NOW, T, True)
        self.assertEqual(c["state"], "ok")   # one failure: said, not paged
        self.assertIn("last answer wrong", ops_alarms.line(c))
        self.ask(Transport(self.answer("It is red.")), 20)
        status = self.run_pass(Fake(), 20)
        c = self.check(status, "ask_canary")
        self.assertEqual(c["state"], "breach")
        self.assertEqual(next(i for i in status["open"] if i["key"] == "ask_canary")["level"],
                         "high")
        self.ask(Transport(self.answer("Teal Blue")), 40)
        self.assertEqual(ops_alarms.canary_check(self.root, NOW + timedelta(minutes=40), T,
                                                 True)["state"], "ok")

    def test_unconfigured_stale_or_never_run_is_a_breach_and_uninstalled_says_nothing(self):
        self.assertIsNone(ops_alarms.canary_check(self.root, NOW, T, False))
        self.assertEqual(ops_alarms.canary_check(self.root, NOW, T, True)["state"], "breach")
        self.cfg.write_text(json.dumps({"question": "q", "expect": []}))   # malformed
        self.ask(Transport(self.answer("Teal Blue")))
        c = ops_alarms.canary_check(self.root, NOW, T, True)
        self.assertEqual(c["state"], "breach")
        self.assertIn("no canary question configured", c["detail"])
        self.cfg.write_text(json.dumps({"question": "q", "expect": ["Teal Blue"]}))
        self.ask(Transport(self.answer("Teal Blue")))
        c = ops_alarms.canary_check(self.root, NOW + timedelta(minutes=90), T, True)
        self.assertEqual(c["state"], "breach")
        self.assertIn("last ran 90 min ago", c["detail"])
        # the installed timer is read from systemd: a pass with it and no status opens
        (self.root / "ask-canary" / "status.json").unlink()
        system = healthy_units() + [unit("plx-ask-canary.timer", "active", "waiting",
                                         UnitFileState="enabled",
                                         Unit="plx-ask-canary.service")]
        status = self.run_pass(Fake(system=system))
        self.assertEqual(self.check(status, "ask_canary")["state"], "breach")

    def test_entra_mode_skips_cleanly_without_minting_or_false_alarm(self):
        # Entra SSO mode (dev_login=false): skip before minting a ticket and report clean ok
        t = Transport(self.answer("Teal Blue"),
                      auth_config={"mode": "entra", "dev_login": False, "sign_in": "microsoft"})
        status = self.ask(t)
        run = status["runs"][0]
        self.assertEqual((run["ok"], run["skipped"], run["verdict"]),
                         (True, True, "skipped:entra_mode"))
        self.assertEqual(self.mints, 0)
        self.assertNotIn("/api/auth/dev-login", [p for _m, p, _h in t.calls])
        c = ops_alarms.canary_check(self.root, NOW, T, True)
        self.assertEqual(c["state"], "ok")
        self.assertIn("Entra SSO mode", c["detail"])
        # Fallback: if /api/auth/config wasn't decisive and dev-login returns 404, also skip
        t404 = Transport(self.answer("Teal Blue"), login_status=404,
                         auth_config={"mode": "dev", "dev_login": True})
        run404 = self.ask(t404, 20)["runs"][0]
        self.assertEqual((run404["ok"], run404["skipped"], run404["verdict"]),
                         (True, True, "skipped:entra_mode"))
        c2 = ops_alarms.canary_check(self.root, NOW + timedelta(minutes=20), T, True)
        self.assertEqual(c2["state"], "ok")


    def test_the_canary_mints_its_own_link_and_never_writes_the_owners(self):
        from unittest import mock
        home = Path(self.tmp.name) / "home"
        (home / ".config" / "plx").mkdir(parents=True)
        owner = home / ".config" / "plx" / "workbench-access-link"
        owner.write_text("https://x/#ticket=owners-own\n")
        owner.chmod(0o600)
        before = (owner.read_text(), owner.stat().st_mtime_ns)
        canary_link = home / ".config" / "plx" / "ask-canary-access-link"
        ran = []

        def fake_run(argv, **kw):   # plx-mint-link --canary writes the canary's file only
            ran.append(argv)
            canary_link.write_text("https://x/#ticket=canary-t\n")
        with mock.patch.object(ask_canary, "LINK", canary_link), \
                mock.patch.object(ask_canary, "OWNER_LINK", owner), \
                mock.patch.object(ask_canary.subprocess, "run", fake_run):
            self.assertEqual(ask_canary.mint_ticket(), "canary-t")
        self.assertEqual(ran, [["sudo", "-n", ask_canary.MINT, "--canary",
                                str(ask_canary.TTL_MINUTES)]])
        self.assertEqual((owner.read_text(), owner.stat().st_mtime_ns), before)
        # the module's own paths differ, and a canary pointed at the owner's file mints nothing
        self.assertNotEqual(ask_canary.LINK, ask_canary.OWNER_LINK)
        self.assertEqual(ask_canary.LINK.name, "ask-canary-access-link")
        with mock.patch.object(ask_canary, "LINK", owner), \
                mock.patch.object(ask_canary, "OWNER_LINK", owner), \
                mock.patch.object(ask_canary.subprocess, "run", fake_run):
            self.assertIsNone(ask_canary.mint_ticket())
        self.assertEqual(len(ran), 1)
        self.assertEqual((owner.read_text(), owner.stat().st_mtime_ns), before)

    def test_the_repo_default_question_is_valid_and_install_places_it_when_missing(self):
        root = Path(__file__).resolve().parents[2]
        cfg = ask_canary.load_config(root / "deploy" / "ops" / "ask-canary.json")
        self.assertIsNotNone(cfg)
        self.assertGreaterEqual(cfg["max_seconds"], 60)   # the fast path's p90 x 4 and more
        # the answer is read from the record at run time, never typed or echoed from the question
        self.assertTrue(cfg["truth"].lstrip().lower().startswith("select"))
        self.assertEqual(cfg["expect"], [])
        install = (root / "deploy" / "ops" / "install_keep_fresh.sh").read_text()
        self.assertIn('"$here/ask-canary.json" "$CANARY_CONFIG"', install)
        self.assertIn('if ! sudo -n test -e "$CANARY_CONFIG"', install)
        # the root mint script comes from the sealed release, never the repo checkout
        self.assertIn("MINT_SRC=$PLX_CURRENT/deploy/ops/plx-mint-link", install)
        self.assertNotIn('"$here/plx-mint-link"', install)


class CanarySenderTest(unittest.TestCase):
    def test_only_the_three_canary_routes_on_the_loopback_api_are_ever_requested(self):
        from operating import canary_sender as c
        self.assertTrue(c.allowed("GET", c.API_BASE + "me"))
        self.assertTrue(c.allowed("GET", c.API_BASE + "auth/config"))
        self.assertTrue(c.allowed("POST", c.API_BASE + "ask"))
        self.assertTrue(c.allowed("POST", c.API_BASE + "auth/dev-login"))
        for method, url in (("POST", c.API_BASE + "me"), ("GET", c.API_BASE + "ask"),
                            ("POST", c.API_BASE + "actions/confirm"),
                            ("POST", c.API_BASE + "ask?x=1"), ("DELETE", c.API_BASE + "ask"),
                            ("POST", "http://127.0.0.1:8781/api/ask"),
                            ("POST", "https://graph.microsoft.com/v1.0/users/x/sendMail")):
            self.assertFalse(c.allowed(method, url), (method, url))
            with self.assertRaises(c.SenderRefused):
                c.canary_request(method, url, {}, b"{}" if method != "GET" else None, 1)
        with self.assertRaises(c.SenderRefused):   # a body on a GET is no read
            c.canary_request("GET", c.API_BASE + "me", {}, b"x", 1)

    def test_a_proxy_in_the_environment_is_never_used(self):
        from unittest import mock
        import urllib.request
        from operating import canary_sender as c
        built = []
        real = urllib.request.build_opener

        def spy(*handlers):
            built.append(handlers)
            opener = real(*handlers)
            opener.open = lambda *a, **k: (_ for _ in ()).throw(OSError("no network in tests"))
            return opener
        with mock.patch.dict("os.environ", {"http_proxy": "http://proxy.invalid:3128",
                                            "HTTP_PROXY": "http://proxy.invalid:3128"}), \
                mock.patch.object(urllib.request, "build_opener", spy):
            with self.assertRaises(OSError):
                c.canary_request("GET", c.API_BASE + "me", {}, None, 1)
        proxies = [h for h in built[0] if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual([p.proxies for p in proxies], [{}])


class LeakTest(Base):
    def write(self, **status):
        home = self.root / "exclusion-leak"
        home.mkdir(exist_ok=True)
        (home / "status.json").write_text(json.dumps(
            {"checked_at": status.pop("at", NOW).isoformat().replace("+00:00", "Z"),
             **status}))

    def test_anything_served_of_an_excluded_person_is_critical_and_counted_only(self):
        # What of the people set a reader other than the owner
        # can reach (the capture holds their data by design: not a count)
        self.write(ok=True, excluded_people=2, served_total=7,
                   served={"business_rows_not_owners": 4, "comms_rows_unflagged": 3,
                           "mailbox_versions": 0})
        status = self.run_pass(Fake())
        c = self.check(status, "exclusion_leak")
        self.assertEqual((c["state"], c["value"]), ("breach", 7))
        self.assertEqual(c["line"], "People exclusion leak scan: served to others: 4 business "
                         "rows of the people set not the owner's; 3 mail and chat rows of the "
                         "people set unflagged")
        self.assertEqual(next(i for i in status["open"]
                              if i["key"] == "exclusion_leak")["level"], "critical")
        self.write(ok=True, excluded_people=2, served_total=0, served={})
        c = ops_alarms.leak_check(self.root, NOW, T, True)
        self.assertEqual(c["state"], "ok")
        self.assertIn("nothing of the 2 excluded people served", c["detail"])

    def test_a_scan_that_failed_is_stale_or_never_ran_fails_closed(self):
        self.assertIsNone(ops_alarms.leak_check(self.root, NOW, T, False))
        self.assertEqual(ops_alarms.leak_check(self.root, NOW, T, True)["state"], "breach")
        self.write(ok=False, error="exclusion unavailable (excluded_people_oids_absent)")
        c = ops_alarms.leak_check(self.root, NOW, T, False)
        self.assertEqual(c["state"], "breach")
        self.assertIn("could not run", c["detail"])
        self.write(ok=True, served_total=0, served={}, at=NOW - timedelta(hours=4))
        self.assertEqual(ops_alarms.leak_check(self.root, NOW, T, False)["state"], "breach")

    def test_the_scan_fails_closed_without_an_exclusion_list_and_records_a_crash(self):
        found = exclusion_leak.scan(self.root, "dbname=x", {},
                                    connect=lambda dsn: self.fail("no list: no read"))
        self.assertFalse(found["ok"])
        self.assertIn("exclusion unavailable", found["error"])

        # a list that loads but no people set built for it: the finding itself
        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, *a):
                pass

            def fetchone(self):
                return None   # no state row

        class Conn:
            def cursor(self):
                return Cur()

            def close(self):
                pass
        env = {"PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e1"}
        found = exclusion_leak.scan(self.root, "dbname=x", env, connect=lambda dsn: Conn())
        self.assertEqual((found["ok"], found["error"]),
                         (False, "people set not built for this list"))

        def crash(*a):
            raise RuntimeError("boom")
        status = exclusion_leak.run(self.root, "dbname=x", {}, now=NOW, scanner=crash)
        self.assertEqual((status["ok"], status["error"]), (False, "scan failed (RuntimeError)"))
        self.assertEqual(oct((self.root / "exclusion-leak" / "status.json").stat().st_mode)[-3:],
                         "600")
        self.assertEqual(ops_alarms.leak_check(self.root, NOW, T, False)["state"], "breach")


class LeakScanBoundsTest(Base):
    def test_a_held_window_skips_the_scan_and_both_connections_are_bounded(self):
        from unittest import mock
        from ontology import window_lock
        windows = Path(self.tmp.name) / "windows"
        with mock.patch.dict("os.environ", {"PLX_WINDOW_DIR": str(windows)}):
            window_lock.take("migration-x", blocks="all", lease_minutes=5)
            try:
                with mock.patch.object(exclusion_leak, "run",
                                       side_effect=AssertionError("scanned in a window")):
                    self.assertEqual(exclusion_leak.main(
                        ["run", "--state-dir", str(self.root)]), 0)
            finally:
                window_lock.release("migration-x")
            ran = []
            with mock.patch.object(exclusion_leak, "run",
                                   side_effect=lambda *a, **k: ran.append(a) or {"ok": True}):
                self.assertEqual(exclusion_leak.main(["run", "--state-dir", str(self.root)]), 0)
            self.assertEqual(len(ran), 1)   # no window: the scan runs
        # the purge's dry run gets the scan's statement bound
        from operating import mail_exclusion_purge as mp
        seen = []

        class Stop(Exception):
            pass

        def connect(dsn, **kw):
            seen.append(kw)
            raise Stop
        import psycopg
        with mock.patch.object(psycopg, "connect", connect):
            with self.assertRaises(Stop):
                mp.purge_pg("postgresql:///x", ["h"], apply=False,
                            statement_timeout_ms=exclusion_leak.STATEMENT_TIMEOUT_MS)
        self.assertEqual(seen[0]["options"],
                         f"-c statement_timeout={exclusion_leak.STATEMENT_TIMEOUT_MS}")


class NewestRecordTest(Base):
    def mail(self, rows):
        _db(self.root / "mail-capture" / "capture.db",
            "CREATE TABLE messages (record TEXT, recorded_at TEXT)", rows,
            "INSERT INTO messages VALUES (?, ?)")

    def test_business_hours_count_weekdays_seven_to_seven_eastern(self):
        fri_6pm = datetime(2030, 1, 4, 23, 0, tzinfo=timezone.utc)    # Fri 6 PM ET
        mon_9am = datetime(2030, 1, 7, 14, 0, tzinfo=timezone.utc)    # Mon 9 AM ET
        self.assertAlmostEqual(ops_alarms.business_hours(fri_6pm, mon_9am), 3.0)
        sat = datetime(2030, 1, 5, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(ops_alarms.business_hours(fri_6pm, sat), 1.0)
        self.assertEqual(ops_alarms.business_hours(mon_9am, fri_6pm), 0.0)

    def test_mail_is_judged_by_the_newest_received_time_not_a_backlog_write(self):
        # the newest stored row is a backlog message from last year: received time wins
        self.mail([(json.dumps({"received_at": "2030-01-02T06:00:00Z"}), "x"),
                   (json.dumps({"received_at": "2029-03-01T07:00:00Z"}), "2030-01-02T11:59Z")])
        c = ops_alarms.newest_checks(self.root, NOW, T, ["mail"])[0]
        self.assertEqual((c["name"], c["state"], c["value"]), ("capture_newest:mail",
                                                               "breach", 6.0))
        self.assertIn("newest mail received", ops_alarms.line(c))
        conn = sqlite3.connect(self.root / "mail-capture" / "capture.db")
        conn.execute("INSERT INTO messages VALUES (?, ?)",
                     (json.dumps({"received_at": "2030-01-02T11:30:00Z"}), "y"))
        conn.commit()
        conn.close()
        self.assertEqual(ops_alarms.newest_checks(self.root, NOW, T, ["mail"])[0]["state"],
                         "ok")

    def test_a_business_source_quiet_over_a_weekend_is_fine_and_quiet_on_a_weekday_is_not(self):
        _db(self.root / "case-runtime" / "source-mirror.sqlite",
            "CREATE TABLE capture (capture_id INTEGER PRIMARY KEY, started_at TEXT, "
            "finished_at TEXT, keys_new INTEGER, keys_changed INTEGER)",
            [(1, "x", "2030-01-04T21:00:00Z", 3, 0),     # Fri 4 PM ET: rows changed
             (2, "x", "2030-01-07T15:00:00Z", 0, 0)],    # Mon: a run that brought nothing
            "INSERT INTO capture VALUES (?, ?, ?, ?, ?)")
        sun = datetime(2030, 1, 6, 20, 0, tzinfo=timezone.utc)
        c = ops_alarms.newest_checks(self.root, sun, T, ["portal"])[0]
        self.assertEqual((c["state"], c["value"]), ("ok", 3.0))
        mon_noon = datetime(2030, 1, 7, 17, 0, tzinfo=timezone.utc)
        c = ops_alarms.newest_checks(self.root, mon_noon, T, ["portal"])[0]
        self.assertEqual((c["state"], c["value"]), ("breach", 8.0))
        self.assertIn("newest new or changed row", c["detail"])

    def bc_store(self, runs, marks=()):
        from operating.host import state_capture_paths
        path = state_capture_paths(self.root)["bc"]
        _db(path, "CREATE TABLE versions (capture_id INTEGER PRIMARY KEY, recorded_at TEXT)",
            [(1, "2030-01-01T00:12:00Z")],   # Mon 7:12 PM ET: the nightly re-aged balances
            "INSERT INTO versions VALUES (?, ?)")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE runs (run_id INTEGER PRIMARY KEY, kind TEXT, state TEXT, "
                     "finished_at TEXT, records INTEGER, error TEXT, detail TEXT)")
        conn.execute("CREATE TABLE run_marks (run_id INTEGER, mark TEXT)")
        conn.executemany("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)", runs)
        conn.executemany("INSERT INTO run_marks VALUES (?, ?)", marks)
        conn.commit()
        conn.close()

    def test_a_quiet_business_central_pages_only_when_no_recent_read_of_it_confirms(self):
        # 11-16: BC Production had no change since Fri 2:52 PM ET; its capture read it back
        # every ~30 min, yet "no new BC record" paged high at 1 PM Monday
        incremental = json.dumps({"mode": "incremental", "since": "2029-12-31T19:42:15.617Z",
                                  "watermark": "2029-12-31T19:52:15.617Z"})
        full = json.dumps({"mode": "full", "watermark": None})
        recent = "2030-01-02T18:40:00.000001Z"    # Wed 1:40 PM ET, 20 minutes before now
        cases = {   # runs -> paged?
            "an incremental read that returned rows": (
                [(1, "bc", "partial", recent, 151, None, incremental)], False),
            "a full content read that returned rows": (
                [(1, "bc", "complete", recent, 159, None, full)], False),
            "a read that returned nothing": (
                [(1, "bc", "complete", recent, 0, None, incremental)], True),
            "a failed read": ([(1, "bc", "failed", recent, 151, None, incremental)], True),
            "an errored read": ([(1, "bc", "partial", recent, 151, "HTTP 500", incremental)],
                                True),
            "a read 3 hours old": (
                [(1, "bc", "complete", "2030-01-02T16:00:00Z", 151, None, incremental)], True),
            "another source's run": ([(1, "graph", "complete", recent, 151, None, full)], True),
            "a run with an unreadable plan": (
                [(1, "bc", "complete", recent, 151, None, "{not json")], True),
            "no run at all": ([], True),
        }
        now = datetime(2030, 1, 2, 19, 0, tzinfo=timezone.utc)   # Wed 2 PM ET
        for label, (runs, paged) in cases.items():
            with self.subTest(label):
                from operating.host import state_capture_paths
                state_capture_paths(self.root)["bc"].unlink(missing_ok=True)
                self.bc_store(runs, [(1, "seen_max:2029-12-31T19:52:15.617Z")])
                c = ops_alarms.newest_checks(self.root, now, T, ["bc"])[0]
                self.assertEqual(c["state"], "breach" if paged else "ok")
                self.assertGreater(c["value"], 6)   # the age itself is still told
                if not paged and "incremental" in label:
                    self.assertIn("no change in Business Central since Dec 31 2:52 PM ET",
                                  c["detail"])
        # no BC store: nothing confirmed
        self.assertEqual(ops_alarms.bc_confirmed_quiet(self.root / "none", now), None)

    def test_a_configured_source_with_no_readable_store_is_unknown_in_a_pass(self):
        status = self.run_pass(Fake())   # make_state configures mail with no capture store
        c = self.check(status, "capture_newest:mail")
        self.assertEqual(c["state"], "unknown")
        completeness = self.check(status, "capture_completeness")
        self.assertIn("compared with the source's own count: mail", completeness["line"])


class MailCoverageAlarmTest(Base):
    def test_an_unexplained_shortfall_against_graph_counts_opens_an_incident(self):
        _db(self.root / "mail-capture" / "capture.db",
            "CREATE TABLE folder_coverage (status TEXT, total INTEGER, captured INTEGER, "
            "short INTEGER, short_since TEXT)",
            [("whole", 1000, 1000, 0, None), ("unexplained", 500, 400, 100, None)],
            "INSERT INTO folder_coverage VALUES (?, ?, ?, ?, ?)")
        status = self.run_pass(Fake())
        self.assertIn("capture_mail_coverage", {i["key"] for i in status["open"]})
        c = self.check(status, "capture_mail_coverage")
        self.assertTrue(c["line"].startswith("Mail capture against Graph's counts: 1,400 of "
                                             "1,500 messages held"))



class ChatListingCapAlarmTest(Base):
    def cycle(self, cycle_id: int, teams: object) -> None:
        db = sqlite3.connect(self.root / "host" / "host.sqlite")
        db.execute("CREATE TABLE IF NOT EXISTS cycle (cycle_id INTEGER PRIMARY KEY, "
                   "started_at TEXT, finished_at TEXT, state TEXT, report TEXT)")
        db.execute("INSERT INTO cycle VALUES (?, '2030-01-02T11:00:00Z', "
                   "'2030-01-02T11:05:00Z', 'complete', ?)",
                   (cycle_id, json.dumps({"sources": {"teams": teams,
                                                      "mail": {"state": "captured"}}})))
        db.commit()
        db.close()

    def test_a_teams_chat_listing_cut_at_its_cap_opens_a_high_incident_until_read_whole(self):
        """2026-11-16: one mailbox's chat listing stopped at 50 pages (799 chats) every
        run and nothing said so. The newest cycle's Teams outcome decides."""
        self.assertIsNone(self.check(self.run_pass(Fake()), "capture_listing_cap:teams"))
        self.cycle(1, {"state": "partial", "reasons": ["blocked_parts",
                                                       "cap:chat_listing_page_cap"]})
        status = self.run_pass(Fake())
        opened = {i["key"]: i for i in status["open"]}
        self.assertIn("capture_listing_cap:teams", opened)
        self.assertEqual(ops_notify.level_for("capture_listing_cap:teams",
                                              self.check(status, "capture_listing_cap:teams")),
                         ops_notify.LEVELS["high"])
        self.assertIn("cap:chat_listing_page_cap",
                      self.check(status, "capture_listing_cap:teams")["detail"])
        self.cycle(2, {"state": "partial", "reasons": "unreadable"})
        self.assertEqual(self.check(self.run_pass(Fake(), 5), "capture_listing_cap:teams")
                         ["state"], "unknown")
        self.cycle(3, {"state": "captured", "reasons": ["blocked_parts"]})
        self.assertEqual(self.check(self.run_pass(Fake(), 10), "capture_listing_cap:teams")
                         ["state"], "ok")


class ServedLagAlarmTest(Base):
    """served-freshness 11-17: the capture ran and the delta's cycles finished, yet a host
    or a skipped source can leave captured records unserved with no alarm. Paged per
    source when the oldest record past the delta's cursor is over an hour old, or the
    delta's newest cycle skipped the source (``mail_skipped``)."""

    def setUp(self) -> None:
        super().setUp()
        self.delta = self.root / "projection" / "delta"
        self.meta({"mail_version": 0, "portal_capture": 0})

    def meta(self, cursors) -> None:
        db = sqlite3.connect(self.delta / "state.sqlite")
        db.execute("CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT)")
        db.execute("DELETE FROM meta")
        db.executemany("INSERT INTO meta VALUES (?, ?)",
                       [(k, json.dumps(v)) for k, v in cursors.items()])
        db.commit()
        db.close()

    def mail(self, stamps) -> None:
        (self.root / "mail-capture").mkdir(exist_ok=True)
        db = sqlite3.connect(self.root / "mail-capture" / "capture.db")
        db.execute("CREATE TABLE IF NOT EXISTS messages (version_id INTEGER PRIMARY KEY, "
                   "record TEXT, recorded_at TEXT)")
        db.executemany("INSERT INTO messages (record, recorded_at) VALUES ('{}', ?)",
                       [(s,) for s in stamps])
        db.commit()
        db.close()

    def cycle(self, **report) -> None:
        with (self.delta / "cycles.jsonl").open("a") as fh:
            fh.write(json.dumps({"command": "run", "started_at": "2030-01-02T11:50:00Z",
                                 "mail_skipped": None, **report}) + "\n")

    def lag(self, source="mail"):
        return {c["name"]: c for c in ops_alarms.served_lag_checks(self.root, NOW, T)}.get(
            f"served_lag:{source}")

    def test_a_captured_record_the_delta_has_not_reached_for_an_hour_pages(self):
        self.mail(["2030-01-02T10:40:00Z", "2030-01-02T11:50:00Z"])
        self.cycle()
        status = self.run_pass(Fake())
        found = self.check(status, "served_lag:mail")
        self.assertEqual((found["state"], found["value"]), ("breach", 1.33))
        self.assertIn("2 captured records the delta has not reached", found["detail"])
        self.assertIn("served_lag:mail", {i["key"] for i in status["open"]})
        self.assertEqual(ops_notify.level_for("served_lag:mail", found),
                         ops_notify.LEVELS["high"])
        # the delta reaches the old one: the newer one is within the hour
        self.meta({"mail_version": 1})
        self.assertEqual((self.lag()["state"], self.lag()["value"]), ("ok", 0.17))
        self.meta({"mail_version": 2})
        self.assertEqual((self.lag()["state"], self.lag()["value"]), ("ok", 0))

    def test_a_cycle_that_skipped_mail_pages_even_with_its_cursor_at_the_head(self):
        self.mail(["2030-01-02T11:50:00Z"])
        self.meta({"mail_version": 1})
        self.cycle()
        self.assertEqual(self.lag()["state"], "ok")
        self.cycle(mail_skipped="people_exclusion_unavailable")
        found = self.lag()
        self.assertEqual(found["state"], "breach")
        self.assertIn("skipped it (people_exclusion_unavailable)", found["detail"])
        self.cycle()   # the next cycle reads mail again
        self.assertEqual(self.lag()["state"], "ok")

    def test_an_unreadable_cursor_or_store_is_unknown_never_fine(self):
        self.mail(["2030-01-02T09:00:00Z"])
        self.meta({})   # no mail cursor
        self.assertEqual(self.lag()["state"], "unknown")
        self.meta({"mail_version": "x'; DROP"})
        self.assertEqual(self.lag()["state"], "unknown")
        (self.delta / "state.sqlite").write_bytes(b"not a database" * 100)
        self.assertEqual(self.lag()["state"], "unknown")
        # a source with no store on this host: no check; a host with no delta: none at all
        self.assertIsNone(self.lag("teams"))
        (self.delta / "state.sqlite").unlink()
        self.assertEqual(ops_alarms.served_lag_checks(self.root, NOW, T), [])

    def test_counts_hold_at_one_ten_and_sixty_records_and_the_portal_reads_its_captures(self):
        for n in (1, 10, 60):
            (self.root / "mail-capture" / "capture.db").unlink(missing_ok=True)
            self.mail(["2030-01-02T10:00:00Z"] * n)
            self.meta({"mail_version": 0})
            self.assertIn(f"{n:,} captured records", self.lag()["detail"], n)
        _db(self.root / "case-runtime" / "source-mirror.sqlite",
            "CREATE TABLE capture (capture_id INTEGER PRIMARY KEY, started_at TEXT, "
            "finished_at TEXT, keys_new INTEGER, keys_changed INTEGER)",
            [(1, "2030-01-02T09:00:00Z", "2030-01-02T09:05:00Z", 3, 0),
             (2, "2030-01-02T10:00:00Z", "2030-01-02T10:05:00Z", 0, 0),
             (3, "2030-01-02T11:30:00Z", "2030-01-02T11:35:00Z", 0, 4)],
            "INSERT INTO capture VALUES (?, ?, ?, ?, ?)")
        self.meta({"portal_capture": 0})
        self.assertEqual(self.lag("portal")["state"], "breach")
        self.meta({"portal_capture": 1})   # capture 2 brought nothing: 3 is the first
        self.assertEqual((self.lag("portal")["state"], self.lag("portal")["value"]),
                         ("ok", 0.42))



class AppliedLag(unittest.TestCase):
    """11-22: Teams looked stopped for 34 h; it was a quiet weekend, but nothing would have
    told a stuck delta -> Postgres hop from quiet. A published delta layer the Postgres
    ontology has not applied for 30 min pages; all applied is green; unreadable is unknown."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        d = self.root / "projection" / "delta"
        d.mkdir(parents=True)
        self.names = ["delta-20261122T040000000001.sqlite", "delta-20261122T041500000002.sqlite"]
        (d / "CURRENT.json").write_text(json.dumps(
            {"layers": [{"store": str(d / n), "owned": 1} for n in self.names]}))

    def run_check(self, applied, now):
        return ops_alarms.applied_lag_checks(self.root, now, T, "dbname=x",
                                             read_applied=applied)[0]

    def test_every_layer_applied_is_green(self):
        got = self.run_check(lambda dsn: set(self.names),
                             datetime(2026, 11, 22, 6, 0, tzinfo=timezone.utc))
        self.assertEqual((got["value"], got["state"]), (0, "ok"))

    def test_an_unapplied_layer_past_30_min_pages_and_under_it_waits(self):
        late = self.run_check(lambda dsn: {self.names[1]},
                              datetime(2026, 11, 22, 4, 31, tzinfo=timezone.utc))
        self.assertEqual(late["state"], "breach")
        self.assertEqual(late["level"], "high")
        self.assertIn(self.names[0], late["detail"])
        soon = self.run_check(lambda dsn: {self.names[1]},
                              datetime(2026, 11, 22, 4, 20, tzinfo=timezone.utc))
        self.assertEqual(soon["state"], "ok")

    def test_unreadable_postgres_is_unknown_never_green(self):
        def boom(dsn):
            raise RuntimeError("down")
        got = self.run_check(boom, datetime(2026, 11, 22, 6, 0, tzinfo=timezone.utc))
        self.assertEqual(got["state"], "unknown")

    def test_no_postgres_ontology_means_no_check(self):
        self.assertEqual(ops_alarms.applied_lag_checks(self.root, datetime.now(timezone.utc),
                                                       T, None), [])

if __name__ == "__main__":
    unittest.main()
