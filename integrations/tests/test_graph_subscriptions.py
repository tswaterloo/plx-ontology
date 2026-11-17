"""Graph subscription renewal (AUD-05): renews at half the lifetime, recreates a missing or
expired subscription, verifies each write, reports failures to the status file and ops
alarms, and is inert unless configured. Evidence: isolated-fixture (a fake Graph; nothing
live)."""

from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from integrations import graph_subscription_sender as sender
from integrations import graph_subscriptions as gs
from operating import ops_alarms

NOW = datetime(2026, 11, 15, 18, 0, tzinfo=timezone.utc)
TENANT = "00000000-0000-4000-8000-0000000000aa"
SECRET = "fixture-app-secret-not-real"
STATE = "fixture-client-state-0123456789"
HUB = "EventHub:https://fixture.vault.azure.net/keyVaults/v/secrets/s?tenantId=" + TENANT
MAIL_RES = "users/aaaa-bbbb/mailFolders('inbox')/messages"
TEAMS_RES = "chats/getAllMessages"
CONFIG = {"subscriptions": [
    {"name": "mail-ops", "source": "mail", "resource": MAIL_RES, "notification_url": HUB,
     "lifetime_minutes": 4200},
    {"name": "teams-all", "source": "teams", "resource": "/chats('19:x@thread.v2')/messages",
     "notification_url": HUB, "lifecycle_url": HUB, "lifetime_minutes": 1000}]}


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")   # Graph's 7 fraction digits


class FakeGraph:
    def __init__(self, subs=None) -> None:
        self.subs = {s["id"]: dict(s) for s in (subs or [])}
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict] = []
        self.fail: dict[str, int] = {}          # method -> status to answer
        self.no_confirm = False
        self.next_link = False
        self.token_status = 200

    def __call__(self, method, url, headers, body, timeout):
        assert sender.allowed(method, url), (method, url)
        self.calls.append((method, url.replace(sender.SUBSCRIPTIONS_URL, "/subs")))
        if url.startswith(sender.TOKEN_PREFIX):
            return self.token_status, json.dumps({"access_token": "tok"}).encode() \
                if self.token_status == 200 else b""
        assert headers["Authorization"] == "Bearer tok"
        doc = json.loads(body) if body else {}
        self.bodies.append(doc)
        if method in self.fail:
            return self.fail[method], b""
        if method == "GET":
            out = {"value": list(self.subs.values())}
            if self.next_link:
                out["@odata.nextLink"] = "x"
            return 200, json.dumps(out).encode()
        if method == "POST":
            made = {"id": f"new-{len(self.subs)}", "resource": doc["resource"],
                    "notificationUrl": doc["notificationUrl"],
                    "expirationDateTime": doc["expirationDateTime"]}
            self.subs[made["id"]] = made
            return 201, json.dumps(made).encode()
        sid = url.rsplit("/", 1)[1]
        if sid not in self.subs:
            return 404, b""
        if not self.no_confirm:
            self.subs[sid]["expirationDateTime"] = doc["expirationDateTime"]
        return 200, json.dumps(self.subs[sid]).encode()


class RenewerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "subs.json").write_text(json.dumps(CONFIG))
        self.env = {gs.ENV_CONFIG: str(self.dir / "subs.json"), gs.ENV_CLIENT_STATE: STATE,
                    "AZURE_TENANT_ID": TENANT, "AZURE_CLIENT_ID": "cid",
                    "AZURE_CLIENT_SECRET": SECRET}
        self.state = self.dir / "state"

    def sub(self, sid, resource, left_minutes):
        return {"id": sid, "resource": resource, "notificationUrl": HUB,
                "expirationDateTime": stamp(NOW + timedelta(minutes=left_minutes))}

    def run_once(self, graph, now=NOW):
        return gs.Renewer(gs.Settings.from_env(self.env), self.state, graph,
                          lambda: now).run()

    def test_creates_missing_then_leaves_healthy_alone(self) -> None:
        graph = FakeGraph()
        done = self.run_once(graph)
        self.assertEqual({n: e["state"] for n, e in done["entries"].items()},
                         {"mail-ops": "created", "teams-all": "created"})
        created = [b for b in graph.bodies if "clientState" in b]
        self.assertEqual({b["clientState"] for b in created}, {STATE})
        self.assertEqual(created[0]["expirationDateTime"][:16], "2026-11-18T16:00")  # +4200 min
        graph.calls.clear()
        again = self.run_once(graph, NOW + timedelta(minutes=10))
        self.assertEqual({e["state"] for e in again["entries"].values()}, {"ok"})
        self.assertEqual([m for m, u in graph.calls if m != "GET" and "oauth2" not in u], [])

    def test_renews_at_half_lifetime_and_not_before(self) -> None:
        graph = FakeGraph([self.sub("m1", MAIL_RES, 2101),      # just over half of 4200
                           self.sub("t1", "/chats('19:x@thread.v2')/messages", 499)])
        done = self.run_once(graph)
        self.assertEqual(done["entries"]["mail-ops"]["state"], "ok")
        self.assertEqual(done["entries"]["teams-all"]["state"], "renewed")
        self.assertEqual([c for c in graph.calls if c[0] == "PATCH"], [("PATCH", "/subs/t1")])
        self.assertEqual(graph.subs["t1"]["expirationDateTime"][:16], "2026-11-16T10:40")

    def test_expired_or_404_is_recreated_and_other_notification_urls_are_ignored(self) -> None:
        other = self.sub("o1", MAIL_RES, 3000)
        other["notificationUrl"] = "EventHub:https://elsewhere"
        graph = FakeGraph([self.sub("m1", MAIL_RES, -5), other])
        done = self.run_once(graph)
        self.assertEqual(done["entries"]["mail-ops"]["state"], "created")
        self.assertNotIn(("PATCH", "/subs/o1"), graph.calls)
        gone = FakeGraph([self.sub("m1", MAIL_RES, 100)])         # renewable, but PATCH 404s
        gone.fail["PATCH"] = 404
        self.assertEqual(self.run_once(gone)["entries"]["mail-ops"]["state"], "created")

    def test_unconfirmed_renewal_and_refusals_fail_and_count(self) -> None:
        graph = FakeGraph([self.sub("m1", MAIL_RES, 100)])
        graph.no_confirm = True
        done = self.run_once(graph)
        self.assertEqual(done["entries"]["mail-ops"]["error"], "renewal_not_confirmed")
        done = self.run_once(graph, NOW + timedelta(minutes=10))
        self.assertEqual(done["entries"]["mail-ops"]["failures"], 2)
        graph.no_confirm = False
        done = self.run_once(graph, NOW + timedelta(minutes=20))
        self.assertEqual((done["entries"]["mail-ops"]["state"],
                          done["entries"]["mail-ops"]["failures"]), ("renewed", 0))
        denied = FakeGraph([self.sub("m1", MAIL_RES, 100)])
        denied.fail["PATCH"] = 403
        self.assertEqual(self.run_once(denied)["entries"]["mail-ops"]["error"],
                         "renew_refused_http_403")

    def test_token_refused_fails_every_entry_and_a_truncated_list_never_creates(self) -> None:
        graph = FakeGraph()
        graph.token_status = 401
        done = self.run_once(graph)
        self.assertEqual({e["error"] for e in done["entries"].values()}, {"token_refused"})
        cut = FakeGraph()
        cut.next_link = True
        done = self.run_once(cut)
        self.assertEqual({e["error"] for e in done["entries"].values()}, {"list_truncated"})
        self.assertNotIn(("POST", "/subs"), cut.calls)

    def test_status_file_holds_no_id_url_or_secret(self) -> None:
        graph = FakeGraph([self.sub("m1-secret-id", MAIL_RES, 100)])
        self.run_once(graph)
        text = (self.state / gs.STATUS_FILE).read_text()
        for leak in (SECRET, STATE, "m1-secret-id", "fixture.vault", MAIL_RES, "aaaa-bbbb"):
            self.assertNotIn(leak, text)

    def test_unconfigured_is_inert_and_a_bad_config_fails_closed(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = gs.main(["once", "--state", str(self.state)], environ={},
                         request=lambda *a: self.fail("no request when off"))
        self.assertEqual((rc, json.loads(out.getvalue())), (0, {"status": "not_configured"}))
        self.assertFalse(self.state.exists())
        bad_docs = [
            {"subscriptions": [dict(CONFIG["subscriptions"][0], notification_url="https://x")]},
            {"subscriptions": [dict(CONFIG["subscriptions"][1], lifetime_minutes=4321)]},
            {"subscriptions": [dict(CONFIG["subscriptions"][0], resource="users/a/drive")]},
            {"subscriptions": [dict(CONFIG["subscriptions"][0], source="teams")]},
            {"subscriptions": [CONFIG["subscriptions"][0]] * 2},
            {"subscriptions": []}]
        for doc in bad_docs:
            (self.dir / "subs.json").write_text(json.dumps(doc))
            with self.assertRaises(gs.SettingsInvalid, msg=doc):
                gs.Settings.from_env(self.env)
        (self.dir / "subs.json").write_text(json.dumps(CONFIG))
        for drop in ("AZURE_CLIENT_SECRET", gs.ENV_CLIENT_STATE):
            with self.assertRaises(gs.SettingsInvalid):
                gs.Settings.from_env({k: v for k, v in self.env.items() if k != drop})

    def test_the_sender_refuses_everything_but_the_subscription_calls(self) -> None:
        S = sender.SUBSCRIPTIONS_URL
        tenant = f"{sender.TOKEN_PREFIX}{TENANT}/oauth2/v2.0/token"
        for method, url in (("POST", tenant), ("GET", S), ("POST", S), ("PATCH", S + "/abc-1"),
                            ("GET", S + "/abc-1")):
            self.assertTrue(sender.allowed(method, url), (method, url))
        for method, url in (("DELETE", S + "/abc-1"), ("DELETE", S), ("PATCH", S),
                            ("POST", S + "/abc"), ("PUT", S + "/abc"),
                            ("GET", tenant), ("PATCH", S + "/abc/../me"),
                            ("GET", S + "?$top=1"), ("POST", S + "x"),
                            ("POST", "https://graph.microsoft.com/v1.0/users/a@b.co/sendMail"),
                            ("POST", "https://graph.microsoft.com/v1.0/me/messages")):
            self.assertFalse(sender.allowed(method, url), (method, url))
        with self.assertRaises(sender.SenderRefused):
            sender.send_subscription("POST", "https://graph.microsoft.com/v1.0/me/sendMail",
                                     {}, b"{}", 1)

    def test_a_differently_echoed_resource_or_url_is_matched_not_duplicated(self) -> None:
        graph = FakeGraph([
            {"id": "m1", "resource": "/v1.0/users/AAAA-bbbb/mailfolders('inbox')/Messages/",
             "notificationUrl": HUB.upper() + "/",
             "expirationDateTime": stamp(NOW + timedelta(minutes=4000))},
            {"id": "t1", "resource": "chats('19%3Ax%40thread.v2')/messages",
             "notificationUrl": HUB, "expirationDateTime": stamp(NOW + timedelta(minutes=900))}])
        done = self.run_once(graph)
        self.assertEqual({n: e["state"] for n, e in done["entries"].items()},
                         {"mail-ops": "ok", "teams-all": "ok"})
        self.assertEqual([c for c in graph.calls if c[1].startswith("/subs") and c[0] != "GET"], [])

    def test_duplicate_resource_and_url_in_the_config_is_refused(self) -> None:
        first = CONFIG["subscriptions"][0]
        twin = dict(first, name="mail-twin", resource="/users/AAAA-BBBB/mailFolders('inbox')/messages",
                    notification_url=HUB + "/")
        (self.dir / "subs.json").write_text(json.dumps({"subscriptions": [first, twin]}))
        with self.assertRaises(gs.SettingsInvalid) as caught:
            gs.Settings.from_env(self.env)
        self.assertIn("config_duplicate_subscription", str(caught.exception))
        other_url = dict(twin, notification_url=HUB + "&x=1")      # a different hub: allowed
        (self.dir / "subs.json").write_text(json.dumps({"subscriptions": [first, other_url]}))
        self.assertEqual(len(gs.Settings.from_env(self.env).wanted), 2)

    def test_teams_needs_a_lifecycle_url_over_an_hour_at_config_load(self) -> None:
        teams = {k: v for k, v in CONFIG["subscriptions"][1].items() if k != "lifecycle_url"}
        (self.dir / "subs.json").write_text(json.dumps({"subscriptions": [teams]}))
        with self.assertRaises(gs.SettingsInvalid) as caught:
            gs.Settings.from_env(self.env)
        self.assertEqual(str(caught.exception), "config_lifecycle_url_required:teams-all")
        (self.dir / "subs.json").write_text(json.dumps({"subscriptions": [
            dict(teams, lifetime_minutes=60)]}))
        self.assertEqual(len(gs.Settings.from_env(self.env).wanted), 1)   # 1 h: not required
        (self.dir / "subs.json").write_text(json.dumps({"subscriptions": [
            dict(CONFIG["subscriptions"][0])]}))                              # mail: optional
        self.assertEqual(len(gs.Settings.from_env(self.env).wanted), 1)

    def test_an_invalid_config_on_the_very_first_run_raises_the_ops_alarm(self) -> None:
        teams = {k: v for k, v in CONFIG["subscriptions"][1].items() if k != "lifecycle_url"}
        (self.dir / "subs.json").write_text(json.dumps({"subscriptions": [teams]}))
        self.assertFalse(self.state.exists())
        with redirect_stdout(io.StringIO()):
            rc = gs.main(["once", "--state", str(self.state)], environ=self.env,
                         request=lambda *a: self.fail("no request on a bad config"),
                         now=lambda: NOW)
        self.assertEqual(rc, 2)
        got = {c["name"]: c for c in ops_alarms.graph_subscription_checks(self.state, NOW)}
        self.assertEqual(got["graph_subscriptions_config"]["state"], "breach")
        self.assertIn("config_lifecycle_url_required", got["graph_subscriptions_config"]["detail"])
        self.assertNotIn("teams-all", json.dumps(got["graph_subscriptions_config"]).replace(
            "config_lifecycle_url_required:teams-all", ""))
        # the fixed config clears it on the next pass
        (self.dir / "subs.json").write_text(json.dumps(CONFIG))
        gs.Renewer(gs.Settings.from_env(self.env), self.state, FakeGraph(), lambda: NOW).run()
        got = {c["name"]: c for c in ops_alarms.graph_subscription_checks(self.state, NOW)}
        self.assertNotIn("graph_subscriptions_config", got)


class AlarmTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)

    def write(self, checked, **entry):
        base = {"source": "mail", "lifetime_minutes": 1000, "failures": 0, "state": "ok",
                "expires_at": stamp(NOW + timedelta(minutes=900))}
        base.update(entry)
        gs._write_json(self.state / gs.STATUS_FILE, {
            "schema": gs.STATUS_SCHEMA, "checked_at": stamp(checked), "entries": {"mail-ops": base}})

    def checks(self):
        return {c["name"]: c for c in ops_alarms.graph_subscription_checks(self.state, NOW)}

    def test_inert_until_the_renewer_has_run(self) -> None:
        self.assertEqual(ops_alarms.graph_subscription_checks(self.state, NOW), [])

    def test_healthy_is_ok_and_failure_expiry_and_stale_status_breach(self) -> None:
        self.write(NOW)
        got = self.checks()
        self.assertEqual({c["state"] for c in got.values()}, {"ok"})
        self.write(NOW, failures=1, state="failed", error="renew_refused_http_403")
        self.assertEqual(self.checks()["graph_subscription:mail-ops"]["state"], "ok")
        self.write(NOW, failures=2, state="failed", error="renew_refused_http_403")
        bad = self.checks()["graph_subscription:mail-ops"]
        self.assertEqual(bad["state"], "breach")
        self.assertIn("renew_refused_http_403", bad["detail"])
        self.write(NOW, expires_at=stamp(NOW + timedelta(minutes=200)))   # <= quarter of 1000
        self.assertEqual(self.checks()["graph_subscription:mail-ops"]["state"], "breach")
        self.write(NOW, expires_at=stamp(NOW - timedelta(minutes=5)))
        self.assertIn("expired", self.checks()["graph_subscription:mail-ops"]["detail"])
        self.write(NOW - timedelta(minutes=61))
        self.assertEqual(self.checks()["graph_subscriptions"]["state"], "breach")

    def test_garbage_status_is_inert_not_a_crash(self) -> None:
        (self.state / "host").mkdir()
        (self.state / gs.STATUS_FILE).write_text("{not json")
        self.assertEqual(ops_alarms.graph_subscription_checks(self.state, NOW), [])


if __name__ == "__main__":
    unittest.main()
