"""Graph change notifications through Event Hubs (AUD-05, ARCH-REVIEW L2): the receiver
turns valid notifications into mail / Teams wakes, refuses anything else, keeps no id, and
does nothing while unconfigured. Evidence: isolated-fixture (synthetic notifications)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from integrations import graph_notifications as gn

FIXTURE = Path(__file__).parent / "fixtures" / "graph_notifications.jsonl"
CLIENT_STATE = "fixture-client-state-0123456789"
TENANT = "00000000-0000-4000-8000-0000000000aa"
SUBS = "1b0c2f3e-0000-4000-8000-0000000000a1,1b0c2f3e-0000-4000-8000-0000000000A2"
CONNECTION = "Endpoint=sb://fixture.servicebus.windows.net/;SharedAccessKeyName=listen;" \
             "SharedAccessKey=fixture-not-a-key"
ENV = {gn.ENV_NAMESPACE: CONNECTION, gn.ENV_HUB: "graph-notify",
       gn.ENV_CLIENT_STATE: CLIENT_STATE, gn.ENV_SUBSCRIPTIONS: SUBS, gn.ENV_TENANT: TENANT}


def run(argv, environ, **kwargs) -> tuple[int, dict]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = gn.main(argv, environ, **kwargs)
    return code, json.loads(out.getvalue().strip().splitlines()[-1])


class ReceiverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp(prefix="graph-notify-"))
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)

    def files(self) -> list[str]:
        return sorted(str(p.relative_to(self.state)) for p in self.state.rglob("*")
                      if p.is_file())

    def test_unconfigured_does_nothing_and_partial_settings_are_refused(self) -> None:
        for command in (["serve"], ["replay", str(FIXTURE)]):
            code, out = run([*command[:1], "--state", str(self.state), *command[1:]], {},
                            source_factory=lambda *a: self.fail("no source while unconfigured"))
            self.assertEqual((code, out), (0, {"status": "not_configured"}))
        self.assertEqual(self.files(), [])
        self.assertEqual(gn.Wakes(self.state).latest(gn.SOURCES), {})
        for broken, reason in (({gn.ENV_HUB: "h"}, "settings_incomplete"),
                               ({**ENV, gn.ENV_CLIENT_STATE: "short"}, "client_state_length"),
                               ({**ENV, gn.ENV_HUB: "bad/hub"}, "hub_name_invalid")):
            code, out = run(["serve", "--state", str(self.state)], broken)
            self.assertEqual((code, out["status"]), (2, "refused"))
            self.assertTrue(out["reason"].startswith(reason), out)
        self.assertEqual(self.files(), [])

    def test_fixture_wakes_mail_and_teams_and_refuses_the_rest(self) -> None:
        code, out = run(["replay", "--state", str(self.state), str(FIXTURE)], ENV)
        self.assertEqual(code, 0)
        self.assertEqual(out["counts"], {
            "accepted": 4, "client_state_mismatch": 1, "ignored": 1, "tenant_mismatch": 1,
            "subscription_unknown": 1, "lifecycle_reauthorizationRequired": 1,
            "lifecycle_missed": 1, "event_malformed": 1})
        self.assertEqual(set(gn.Wakes(self.state).latest(gn.SOURCES)), {"mail", "teams"})
        status = json.loads((self.state / gn.STATUS_FILE).read_text())
        self.assertEqual(status["sources"]["teams"]["last_lifecycle"], "lifecycle_missed")
        self.assertEqual(status["sources"]["mail"]["subscription_expires"],
                         "2026-11-20T18:00:00.0000000Z")
        self.assertEqual(status["last_lifecycle"]["event"],
                         "lifecycle_reauthorizationRequired")
        # nothing written holds an id, a path, the client state or the key
        written = "".join(p.read_text() for p in self.state.rglob("*") if p.is_file())
        for secret in ("11111111-", "AAMk", "19:", "chats(", "Users/", CLIENT_STATE,
                       "fixture-not-a-key", "1b0c2f3e", TENANT):
            self.assertNotIn(secret, written)

    def test_a_forged_or_unknown_notification_wakes_nothing(self) -> None:
        settings = gn.Settings.from_env(ENV)
        good = {"clientState": CLIENT_STATE, "tenantId": TENANT.upper(),
                "subscriptionId": "1b0c2f3e-0000-4000-8000-0000000000A1",
                "changeType": "created", "resource": "users/x/messages/y"}
        self.assertEqual(gn.judge(good, settings), ("mail", "accepted"))
        for change in ({"clientState": None}, {"clientState": CLIENT_STATE + "x"},
                       {"changeType": "renamed"}, {"resource": "users/x/messages"},
                       {"resource": "users/x/messages/y/attachments/z"},
                       {"resource": "users/x/../../messages/y"}, {"resource": 7},
                       {"lifecycleEvent": "somethingNew"}, {"tenantId": None}):
            source, outcome = gn.judge({**good, **change}, settings)
            self.assertIsNone(source, (change, outcome))
        receiver = gn.Receiver(self.state, settings)
        self.assertEqual(receiver.handle([b"[]", b"{\"value\": 3}", b"\xff",
                                          b" " * (gn.MAX_EVENT_BYTES + 1)]),
                         {"event_malformed": 4})
        self.assertEqual(gn.Wakes(self.state).latest(gn.SOURCES), {})

    def test_a_batch_of_n_notifications_is_one_wake_per_source(self) -> None:
        settings = gn.Settings.from_env(ENV)
        for n in (1, 10, 60):
            receiver = gn.Receiver(self.state, settings, now=lambda: float(n))
            note = {"clientState": CLIENT_STATE, "tenantId": TENANT, "changeType": "created",
                    "subscriptionId": "1b0c2f3e-0000-4000-8000-0000000000a1"}
            body = json.dumps({"value": [
                {**note, "resource": f"users/u/messages/m{i}"} if i % 2 else
                {**note, "resource": f"chats('19:c')/messages('{i}')"} for i in range(n)]})
            counts = receiver.handle([body.encode()])
            self.assertEqual(counts, {"accepted": n})
            expect = {"teams"} if n == 1 else {"mail", "teams"}
            self.assertEqual({k for k, v in gn.Wakes(self.state).latest(gn.SOURCES).items()
                              if v == float(n)}, expect)

    def test_serve_checkpoints_offsets_locally_after_each_batch(self) -> None:
        bodies = list(gn.fixture_bodies(FIXTURE))

        class FixtureHub:
            def __init__(self, settings, offsets):
                self.offsets = offsets

            def run(self, handle, stop):
                for i, body in enumerate(bodies):
                    handle([body])
                    self.offsets.put("0", str(i))
                stop.set()
        code, out = run(["serve", "--state", str(self.state)], ENV, source_factory=FixtureHub)
        self.assertEqual((code, out["status"]), (0, "stopped"))
        self.assertEqual(out["counts"]["accepted"], 4)
        self.assertEqual(gn.Offsets(self.state).get("0"), str(len(bodies) - 1))
        # a restart reads the kept offsets (an unreadable file is a fresh start)
        (self.state / gn.OFFSETS_FILE).write_text("[1, 2]")
        self.assertEqual(gn.Offsets(self.state).offsets, {})

    def test_probe_reports_counts_ages_and_expiry_and_the_spec_loads(self) -> None:
        run(["replay", "--state", str(self.state), str(FIXTURE)], ENV)
        settings = gn.Settings.from_env(ENV)
        expiry = __import__("datetime").datetime(2026, 11, 20, 18, tzinfo=__import__(
            "datetime").timezone.utc).timestamp()
        report = gn.probe(settings, self.state, partitions=lambda s: ["0", "1"],
                          now=lambda: expiry - 7200)
        self.assertEqual((report["ok"], report["partitions"], report["events_accepted"],
                          report["events_refused"]), (True, 2, 4, 4))
        self.assertEqual(report["expires_in_hours"], {"mail": 2.0, "teams": 2.0})
        self.assertEqual(gn.probe(settings, None, partitions=lambda s: [])["ok"], False)
        from verify import live_verify
        spec = json.loads((Path(__file__).parents[2] / "devloop" / "live-checks" /
                           "graph.change_notifications.json").read_text())
        live_verify.validate_spec(spec, stem="graph.change_notifications")

    def test_the_event_hub_source_refuses_without_its_package(self) -> None:
        import builtins
        real = builtins.__import__

        def no_azure(name, *args, **kwargs):
            if name.startswith("azure"):
                raise ImportError(name)
            return real(name, *args, **kwargs)
        settings = gn.Settings.from_env(ENV)
        source = gn.EventHubSource(settings, gn.Offsets(self.state))
        with mock.patch.object(builtins, "__import__", no_azure):
            with self.assertRaises(gn.SettingsInvalid) as caught:
                source.run(lambda bodies: None, threading.Event())
        self.assertEqual(str(caught.exception), "dependency_absent:azure-eventhub")
        self.assertNotIn("fixture-not-a-key", repr(settings))


if __name__ == "__main__":
    unittest.main()
