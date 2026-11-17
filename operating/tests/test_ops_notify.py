"""Ops mail (operating.ops_notify): one mail per incident key, levels, re-send at 4 h,
recovery, the daily mail, disabled by default, the Graph transport (injected)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from operating import ops_notify as n

T0 = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)   # 7 AM ET: before the daily mail


class Outbox:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def __call__(self, mail):
        if self.fail:
            raise n.SendError(self.fail)
        self.sent.append(mail)


def inc(line, level, opened="2030-01-02T12:00:00Z"):
    return {"line": line, "level": n.LEVELS[level], "opened_at": opened}


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = dict(n.DEFAULTS, enabled=True, sender="ops@example.test",
                        owner_address="owner@example.test",
                        state_file=str(self.tmp / "notify.json"))
        self.box = Outbox()

    def run_at(self, minutes, incidents, **kw):
        return n.run_alarms(incidents, T0 + timedelta(minutes=minutes), cfg=self.cfg,
                            transport=self.box, **kw)

    def subjects(self):
        return [m["subject"] for m in self.box.sent if " Daily " not in m["subject"]]

    def test_one_mail_per_key_resend_at_four_hours_escalate_and_recover(self):
        api = {"unit:plx-api.service": inc("plx-api.service (system): failed", "critical")}
        self.run_at(0, api)
        self.run_at(5, api)
        self.run_at(3 * 60, api)
        self.assertEqual(self.subjects(), [
            "[PLX ops] OPEN critical: plx-api.service (system): failed"])
        self.assertEqual(self.box.sent[0]["to"], ["owner@example.test"])
        self.run_at(4 * 60, api)
        self.assertEqual(self.subjects()[-1],
                         "[PLX ops] STILL OPEN critical: plx-api.service (system): failed")
        self.run_at(4 * 60 + 5, {})
        self.assertEqual(self.subjects()[-1],
                         "[PLX ops] RECOVERED critical: plx-api.service (system): failed")
        self.assertEqual(len(self.subjects()), 3)
        # a warning: once, never re-sent; a step up mails again; info never
        self.box.sent.clear()
        key = "credential_expiry:k"
        self.run_at(300, {key: inc("k expires in 12 days", "warning"),
                          "memory_pressure": inc("memory low", "info")})
        self.run_at(300 + 24 * 60, {key: inc("k expires in 11 days", "warning"),
                                    "memory_pressure": inc("memory low", "info")})
        self.run_at(300 + 25 * 60, {key: inc("k expires in 6 days", "high")})
        self.assertEqual(self.subjects(), ["[PLX ops] OPEN warning: k expires in 12 days",
                                           "[PLX ops] WORSE high: k expires in 6 days"])

    def test_many_changes_in_one_pass_are_one_mail(self):
        many = {f"unit:plx-t{i}.timer": inc(f"plx-t{i}.timer: stopped", "warning")
                for i in range(12)}
        self.run_at(0, many)
        self.assertEqual(len(self.box.sent), 1)
        self.assertEqual(self.box.sent[0]["subject"],
                         "[PLX ops] 12 changes, worst warning: 12 open")
        self.assertEqual(self.box.sent[0]["body"].count("plx-t"), 12)

    def test_disabled_or_unset_sends_nothing_and_logs_why(self):
        for cfg, why in ((dict(self.cfg, enabled=False), "disabled in the config"),
                         (dict(self.cfg, sender=""), "no sender set"),
                         (dict(self.cfg, owner_address="", recipients=["owner"]),
                          "no recipient set")):
            Path(cfg["state_file"]).unlink(missing_ok=True)
            done = n.run_alarms({"backup": inc("Nightly backup: failed", "critical")}, T0,
                                cfg=cfg, transport=self.box)
            self.assertEqual(done["off"], why)
            self.assertEqual(self.box.sent, [])
            self.assertEqual((done["mails"][0]["sent"], done["mails"][0]["why"]), (False, why))
            # only the daily mail is logged while off (no log churn every pass)
            self.assertEqual(json.loads(Path(cfg["state_file"]).read_text())["log"], [])
        self.assertEqual(n.why_off(n.load_config(self.tmp / "absent.json")), "no config file")
        self.assertFalse(n.DEFAULTS["enabled"])

    def test_a_failed_send_is_retried_next_pass(self):
        self.box.fail = "sendMail refused (HTTP 503)"
        self.run_at(0, {"backup": inc("Nightly backup: failed", "critical")})
        state = json.loads(Path(self.cfg["state_file"]).read_text())
        self.assertEqual(len(state["pending"]), 1)
        self.box.fail = None
        self.run_at(5, {"backup": inc("Nightly backup: failed", "critical")})
        self.assertEqual(self.subjects(), ["[PLX ops] OPEN critical: Nightly backup: failed"])
        self.assertEqual(json.loads(Path(self.cfg["state_file"]).read_text())["pending"], [])

    def test_the_daily_mail_once_a_day_at_eight_et_all_clear_or_the_list(self):
        self.run_at(0, {})                                    # 7 AM ET: not yet
        self.assertEqual(self.box.sent, [])
        self.run_at(65, {})                                   # 8:05 AM ET
        self.run_at(70, {})
        self.assertEqual(len(self.box.sent), 1)
        self.assertEqual(self.box.sent[0]["subject"], "[PLX ops] Daily Wed Jan 2: all clear")
        self.assertIn("If it does not come, the alarms are not running",
                      self.box.sent[0]["body"])
        self.box.sent.clear()
        old = {"capture:sharepoint_lists": inc("sharepoint_lists stale", "high",
                                               opened="2029-12-28T12:00:00Z")}
        self.run_at(24 * 60 + 60, old, extra=["Disk forecast: / not growing."],
                    paused=[{"key": "unit:plx-delta.timer", "line": "plx-delta.timer stopped",
                             "until": "2030-01-03T23:00:00Z", "why": "migration"}])
        daily = [m for m in self.box.sent if "Daily" in m["subject"]]
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0]["subject"], "[PLX ops] Daily Thu Jan 3: 1 open, worst high")
        self.assertIn("open 3+ days: fix it or pause it", daily[0]["body"])
        self.assertIn("plx-delta.timer stopped (until", daily[0]["body"])
        self.assertIn("Disk forecast", daily[0]["body"])

    def test_host_conditions_and_unit_notices_share_the_keys(self):
        cfg = dict(self.cfg, max_mails_per_pass=10)
        stale = {"host:alarms_not_running": inc("in-app alarms stopped", "critical")}
        n.run_host(stale, T0, cfg=cfg, transport=self.box)
        n.run_host(stale, T0 + timedelta(minutes=10), cfg=cfg, transport=self.box)
        self.assertEqual(len(self.box.sent), 1)
        # the in-app pass never closes a host: key
        n.run_alarms({}, T0 + timedelta(minutes=20), cfg=cfg, transport=self.box)
        self.assertEqual(len(self.box.sent), 1)
        n.run_host({}, T0 + timedelta(minutes=30), cfg=cfg, transport=self.box)
        self.assertEqual(self.subjects()[-1],
                         "[PLX ops] RECOVERED critical: in-app alarms stopped")
        # a unit failure the host mailed: the in-app pass reporting it sends nothing more
        key = "unit:plx-api.service"
        n.run_host(None, T0, cfg=cfg, transport=self.box,
                   notices={key: {"line": "plx-api.service failed",
                                  "opened_at": "2030-01-02T12:00:00Z"}})
        n.run_host(None, T0, cfg=cfg, transport=self.box,
                   notices={key: {"line": "plx-api.service failed"}})
        n.run_alarms({key: inc("plx-api.service (system): failed", "critical")},
                     T0 + timedelta(minutes=5), cfg=cfg, transport=self.box)
        self.assertEqual(sum(1 for s in self.subjects() if "plx-api" in s), 1)
        # one the in-app pass never saw (restarted at once) recovers after the grace
        n.run_alarms({}, T0 + timedelta(minutes=6), cfg=cfg, transport=self.box)
        n.run_host(None, T0, cfg=cfg, transport=self.box,
                   notices={"unit:plx-x.service": {"line": "plx-x failed",
                                                   "opened_at": "2030-01-02T12:00:00Z"}})
        n.run_alarms({}, T0 + timedelta(minutes=10), cfg=cfg, transport=self.box)
        self.assertNotIn("RECOVERED high: plx-x failed", " ".join(self.subjects()))
        n.run_alarms({}, T0 + timedelta(minutes=16), cfg=cfg, transport=self.box)
        self.assertIn("[PLX ops] RECOVERED high: plx-x failed", self.subjects())

    def test_an_unreachable_graph_costs_a_pass_one_request_and_the_budget_bounds_it(self):
        calls = []

        def down(mail):
            calls.append(mail)
            raise n.SendError("request failed (URLError)")
        many = {f"backup{i}": inc(f"b{i}", "critical") for i in range(3)}
        cfg = dict(self.cfg, max_mails_per_pass=10)
        for minute in range(0, 25, 5):
            n.run_alarms(many, T0 + timedelta(minutes=minute), cfg=cfg, transport=down)
        self.assertEqual(len(calls), 5)               # one try per pass, not 3 + pending
        state = json.loads(Path(cfg["state_file"]).read_text())
        self.assertEqual(len(state["pending"]), 3)    # bounded, kept for later
        self.assertEqual(n.run_alarms(many, T0 + timedelta(minutes=30), cfg=cfg,
                                      transport=self.box, retry=False)["mails"], [])
        n.run_alarms(many, T0 + timedelta(minutes=35), cfg=cfg, transport=self.box)
        self.assertEqual(len(self.box.sent), 3)
        # the send budget: a slow transport stops the pass's sends once it is spent
        ticks = iter(range(0, 1000, 20))
        held = {"pending": []}
        mails = [{"subject": f"s{i}", "body": "b", "kind": "open"} for i in range(4)]
        out = n._deliver(held, mails, self.cfg, T0, lambda m: None,
                         clock=lambda: next(ticks))
        self.assertEqual([m["sent"] for m in out], [True, False, False, False])
        self.assertEqual(len(held["pending"]), 3)

    def test_incidents_open_while_mail_was_off_are_mailed_when_it_is_turned_on(self):
        api = {"unit:plx-api.service": inc("plx-api.service (system): failed", "critical")}
        off = dict(self.cfg, enabled=False)
        for minute in (0, 5, 10):
            n.run_alarms(api, T0 + timedelta(minutes=minute), cfg=off, transport=self.box)
        self.assertEqual(self.box.sent, [])
        n.run_alarms(api, T0 + timedelta(minutes=15), cfg=self.cfg, transport=self.box)
        n.run_alarms(api, T0 + timedelta(minutes=20), cfg=self.cfg, transport=self.box)
        self.assertEqual(self.subjects(),
                         ["[PLX ops] OPEN critical: plx-api.service (system): failed"])

    def test_a_paused_incident_is_not_mailed_as_recovered(self):
        key = "unit:plx-delta.timer"
        self.run_at(0, {key: inc("plx-delta.timer: stopped", "warning")})
        self.run_at(5, {}, paused=[{"key": key, "until": "2030-01-03T00:00:00Z"}])
        self.assertEqual(self.subjects(), ["[PLX ops] OPEN warning: plx-delta.timer: stopped"])
        self.run_at(10, {})                         # really recovered later
        self.assertEqual(self.subjects()[-1],
                         "[PLX ops] RECOVERED warning: plx-delta.timer: stopped")

    def test_a_pass_finding_another_mailing_skips(self):
        import fcntl
        path = Path(self.cfg["state_file"])
        with open(path.with_name(path.name + ".lock"), "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            done = self.run_at(0, {"backup": inc("Nightly backup: failed", "critical")})
        self.assertTrue(done["busy"])
        self.assertEqual(self.box.sent, [])

    def test_alarm_mail_goes_to_the_owner_only(self):
        """A recipient other than the owner's mailbox is refused,
        never mailed; the owner still gets the alarm, and the status page counts what was
        refused (never an address)."""
        cases = ((["owner"], ["owner@example.test"], 0),
                 (["Owner@Example.test "], ["owner@example.test"], 0),
                 (["owner", "vince@example.test"], ["owner@example.test"], 1),
                 (["vince@example.test", "ops@example.test"], [], 2),
                 (["someone@elsewhere.test", "owner"], ["owner@example.test"], 1))
        for listed, to, refused in cases:
            cfg = dict(self.cfg, recipients=listed)
            self.assertEqual((n.recipients(cfg), n.refused_recipients(cfg)), (to, refused),
                             listed)
        # no owner mailbox set: nobody, whatever else is listed
        cfg = dict(self.cfg, owner_address="", recipients=["owner", "vince@example.test"])
        self.assertEqual((n.recipients(cfg), n.why_off(cfg)), ([], "no recipient set"))
        # a pass with an extra recipient configured mails the owner alone
        self.cfg["recipients"] = ["vince@example.test", "owner"]
        self.run_at(0, {"backup": inc("Nightly backup: failed", "critical")})
        self.assertEqual([m["to"] for m in self.box.sent], [["owner@example.test"]])
        described = n.describe(self.cfg)
        self.assertEqual((described["recipients"], described["refused_recipients"]), (1, 1))
        self.assertNotIn("vince@", json.dumps(described))
        # only others listed: nothing is sent, and the log says why
        Path(self.cfg["state_file"]).unlink()
        self.box.sent.clear()
        cfg = dict(self.cfg, recipients=["vince@example.test"])
        done = n.run_alarms({"backup": inc("Nightly backup: failed", "critical")}, T0,
                            cfg=cfg, transport=self.box)
        self.assertEqual((self.box.sent, done["off"]), ([], "no recipient set"))

    def test_levels(self):
        self.assertEqual(n.level_for("unit:plx-api.service"), n.LEVELS["critical"])
        self.assertEqual(n.level_for("unit:user:plx-x.timer"), n.LEVELS["warning"])
        self.assertEqual(n.level_for("capture:mail"), n.LEVELS["critical"])
        self.assertEqual(n.level_for("capture:bc"), n.LEVELS["high"])
        self.assertEqual(n.level_for("memory_pressure"), n.LEVELS["info"])
        self.assertEqual(n.level_for("x", {"level": "critical"}), n.LEVELS["critical"])


class GraphMailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "plx.env").write_text(
            "AZURE_TENANT_ID=tenant-1\nexport AZURE_CLIENT_ID='client-1'\n"
            "AZURE_CLIENT_SECRET=\"s3cret-value\"\nOTHER=x\n")
        self.cfg = dict(n.DEFAULTS, sender="ops@example.test",
                        owner_address="owner@example.test",
                        credentials_file=str(self.tmp / "plx.env"))
        self.calls = []

    def post(self, replies):
        def post(url, headers, body):
            self.calls.append((url, dict(headers), body))
            return replies.pop(0)
        return post

    def test_client_credentials_then_send_mail_from_the_fixed_sender(self):
        send = n.GraphMail(self.cfg, self.post([(200, b'{"access_token": "tok"}'),
                                                (202, b"")]))
        send({"subject": "s", "body": "b", "to": ["owner@example.test"]})
        (token_url, _, token_body), (url, headers, body) = self.calls
        self.assertEqual(token_url,
                         "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token")
        self.assertIn(b"client_id=client-1", token_body)
        self.assertEqual(url, "https://graph.microsoft.com/v1.0/users/ops@example.test/sendMail")
        self.assertEqual(headers["Authorization"], "Bearer tok")
        message = json.loads(body)
        self.assertEqual(message["message"]["toRecipients"],
                         [{"emailAddress": {"address": "owner@example.test"}}])
        self.assertFalse(message["saveToSentItems"])

    def test_the_transport_refuses_anyone_but_the_owner_before_a_token(self):
        for to in (["vince@example.test"], ["owner@example.test", "x@example.test"], [],
                   ["owner@example.test.evil"]):
            send = n.GraphMail(self.cfg, self.post([(200, b'{"access_token": "tok"}'),
                                                    (202, b"")]))
            with self.assertRaisesRegex(n.SendError, "recipient refused"):
                send({"subject": "s", "body": "b", "to": to})
        self.assertEqual(self.calls, [])          # no token asked, nothing posted
        unset = n.GraphMail(dict(self.cfg, owner_address=""), self.post([]))
        with self.assertRaisesRegex(n.SendError, "recipient refused"):
            unset({"subject": "s", "body": "b", "to": ["owner@example.test"]})

    def test_refusals_name_the_step_never_a_value(self):
        for replies, said in (([(401, b"{}")], "token refused (HTTP 401)"),
                              ([(200, b'{"access_token": "tok"}'), (403, b"")],
                               "sendMail refused (HTTP 403)")):
            send = n.GraphMail(self.cfg, self.post(list(replies)))
            with self.assertRaises(n.SendError) as raised:
                send({"subject": "s", "body": "b", "to": ["owner@example.test"]})
            self.assertEqual(str(raised.exception), said)
            self.assertNotIn("s3cret", str(raised.exception))
        missing = n.GraphMail(dict(self.cfg, credentials_prefix="BC"), self.post([]))
        with self.assertRaisesRegex(n.SendError, "BC credentials incomplete"):
            missing({"subject": "s", "body": "b", "to": ["owner@example.test"]})


if __name__ == "__main__":
    unittest.main()
