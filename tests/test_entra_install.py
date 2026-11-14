"""operating.entra_install: the Entra sign-in install renders, keeps its keys and checks
itself without signing anyone in.

Evidence label: ``isolated-fixture`` (a synthetic key, an injected fetch, temp files).
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from operating import entra_install as ei
from operating import employee_permissions as ep

COMMITTED = json.loads(ei.COMMITTED.read_text())
TENANT = COMMITTED["tenant_id"]
HOST = "workbench.plx.example"
OWNER = "22222222-2222-4222-8222-222222222222"
OTHER = "55555555-5555-4555-8555-555555555555"
EXCLUDED = "33333333-3333-4333-8333-333333333333"
JWKS = {"keys": [{"kty": "RSA", "kid": "k1", "e": "AQAB",
                  "n": base64.urlsafe_b64encode(b"\xc1" * 256).decode().rstrip("=")}]}
DEV_ENV = ("# serve flags\nPLX_API_ARGS=--dev --dev-ticket-login --state-dir /s "
           "--host-config {host} --ask\nPLX_ACTIONS_LIVE=1\n")


def install(tmp: Path, *, source="portal-reporting-api people relations (live)",
            owners=(OWNER,), users=(OWNER,), bound=36) -> Path:
    """A complete install in ``tmp``: resource, JWKS, host config, policy, snapshot, env."""
    (tmp / "entra-resource.json").write_text(json.dumps(ei.resource_document(HOST, COMMITTED)))
    (tmp / "entra-jwks.json").write_text(json.dumps(JWKS))
    (tmp / "policy.json").write_text(json.dumps({
        "schema": ep.POLICY_SCHEMA, "tenant": "plx", "entra_tenant_id": TENANT,
        "clearance_by_access_level": {k: "internal" for k in ("L1", "L2", "L3", "L4", "L5")},
        "principals": {"staff:floor": {"portal_roles": ["STAFF"]}},
        "full_read": [{"object_id": o, "granted_by": "owner", "granted_at": "2026-11-01T00:00:00Z",
                       "reason": "owner", "scopes": ["mail"]} for o in owners]}))
    (tmp / "snapshot.json").write_text(json.dumps({
        "schema": ep.PORTAL_ACCESS_SNAPSHOT_SCHEMA, "source": source,
        "read_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "users": [{"id": f"u{i}", "email": f"p{i}@plx.example", "role": "STAFF",
                   "accessLevel": "L2", "isActive": True, "entraTenantId": TENANT,
                   "entraObjectId": o} for i, o in enumerate(users)]}))
    host = {"employee_policy": "policy.json", "portal_access": "snapshot.json"}
    if bound is not None:
        host["portal_access_max_age_hours"] = bound
    (tmp / "host.json").write_text(json.dumps(host))
    env = ei.swap_api_args(DEV_ENV.format(host=tmp / "host.json"),
                           str(tmp / "entra-resource.json"), ei.redirect_uri(HOST))
    (tmp / "api.env").write_text(env)
    return tmp / "api.env"


def failing(results) -> dict[str, str]:
    return {name: outcome for name, outcome in results if outcome != "ok"}


EXCLUSION = {"PLX_EXCLUSION_ENV_FILE": "", "PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED}


class RenderTest(unittest.TestCase):
    def test_swap_leaves_no_dev_login_and_keeps_every_other_line(self):
        env = ei.swap_api_args(DEV_ENV.format(host="/h/host.json"), "/etc/plx/entra/r.json",
                               "https://w.example/signin", "/h2/host.json")
        args = ei.read_env_text(env)["PLX_API_ARGS"].split()
        self.assertEqual(args[:2], ["--entra-resource", "/etc/plx/entra/r.json"])
        self.assertFalse({"--dev", "--dev-ticket-login"} & set(args))
        self.assertIn("/h2/host.json", args)
        self.assertIn("PLX_ACTIONS_LIVE=1", env)
        self.assertEqual(ei.read_env_text(env)["PLX_APP_REDIRECT_URI"], "https://w.example/signin")
        with self.assertRaises(ei.InstallError):
            ei.swap_api_args("PLX_SEARCH=hybrid\n", "/r.json", "https://w.example/signin")
        with self.assertRaises(ei.InstallError):
            ei.resource_document("https://w.example/", COMMITTED)


class JwksTest(unittest.TestCase):
    def test_only_a_document_with_a_key_replaces_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            install(tmp)
            resource = tmp / "entra-resource.json"
            asked = []
            self.assertEqual(ei.refresh_jwks(resource, fetch=lambda u: asked.append(u) or JWKS), 1)
            self.assertEqual(asked, [f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"])
            before = (tmp / "entra-jwks.json").read_text()
            for bad in ({"keys": []}, {"nope": 1}, {"keys": [{"kty": "RSA", "kid": "k",
                                                              "e": "AQAB", "n": "AQAB"}]}):
                with self.assertRaises(ei.InstallError):
                    ei.refresh_jwks(resource, fetch=lambda u, bad=bad: bad)
            def boom(url):
                raise OSError("down")
            with self.assertRaises(ei.InstallError):
                ei.refresh_jwks(resource, fetch=boom)
            self.assertEqual((tmp / "entra-jwks.json").read_text(), before)
        with self.assertRaises(ei.InstallError):
            ei._fetch("https://evil.example/keys")

    def test_the_installs_reads_are_gets_through_the_shared_client_and_refuse_elsewhere(self):
        """The JWKS read goes through integrations.http.get_json (GET, no redirect,
        bounded); the local config read only to a loopback host, refusing a redirect."""
        from unittest import mock
        import http.server
        import threading
        from integrations import http as ih
        seen = []
        with mock.patch.object(ih, "get_json", lambda url, headers, **k: (
                seen.append((url, k["max_bytes"])), {"keys": []})[1]):
            ei._fetch(ei.jwks_url("00000000-0000-0000-0000-000000000001"))
        self.assertEqual(seen, [("https://login.microsoftonline.com/"
                                 "00000000-0000-0000-0000-000000000001/discovery/v2.0/keys",
                                 256 * 1024)])
        with mock.patch.object(ih, "get_json", side_effect=ih.SourceUnavailable(
                "source response exceeds configured bound")):
            with self.assertRaises(ei.InstallError):
                ei._fetch(ei.jwks_url("00000000-0000-0000-0000-000000000001"))
        with self.assertRaises(ei.InstallError):
            ei._api_config("http://example.com:8000")
        for bad in ("http://example.com/api", "http://user:pw@127.0.0.1/x",
                    "file:///etc/passwd", "ftp://127.0.0.1/x"):
            with self.assertRaises(ValueError, msg=bad):
                ih.get_local_json(bad)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/away"):
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/api/auth/config")
                    self.end_headers()
                    return
                body = json.dumps({"mode": "entra", "method": self.command}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        self.assertEqual(ei._api_config(base)(), {"mode": "entra", "method": "GET"})
        with self.assertRaises(ih.SourceUnavailable):
            ei._api_config(base + "/away")()


class CheckTest(unittest.TestCase):
    def test_a_complete_install_passes_and_the_live_probe_must_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = install(Path(tmp))
            good = {"mode": "entra", "sign_in_available": True,
                    "entra": {"redirect_uri": ei.redirect_uri(HOST)}}
            self.assertEqual(failing(ei.check(env, app_host=HOST, environ=EXCLUSION,
                                              api_config=lambda: good)), {})
            dev = {"mode": "dev", "sign_in_available": True}
            self.assertIn("api_auth_config", failing(ei.check(
                env, app_host=HOST, environ=EXCLUSION, api_config=lambda: dev)))

    def test_each_rollout_blocker_is_named(self):
        cases = {
            "portal_snapshot_live": dict(source="scripts/eval_owner_host.py (claims)"),
            "owners_in_snapshot": dict(owners=(OWNER, OTHER)),
            "excluded_not_in_snapshot": dict(users=(OWNER, EXCLUDED)),
            "snapshot_age_bound": dict(bound=None),
        }
        for name, kw in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                env = install(Path(tmp), **kw)
                self.assertEqual(set(failing(ei.check(env, app_host=HOST, environ=EXCLUSION))),
                                 {name})
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            env = install(tmp)
            self.assertEqual(set(failing(ei.check(env, app_host=HOST,
                                                  environ={"PLX_EXCLUSION_ENV_FILE": ""}))),
                             {"people_exclusion"})
            old = time.time() - 3 * 86400
            os.utime(tmp / "entra-jwks.json", (old, old))
            self.assertEqual(set(failing(ei.check(env, app_host=HOST, environ=EXCLUSION))),
                             {"jwks_fresh"})
            self.assertIn("redirect_uri", failing(ei.check(env, app_host="other.example",
                                                           environ=EXCLUSION)))
            env.write_text(DEV_ENV.format(host=tmp / "host.json"))
            self.assertIn("api_args_entra", failing(ei.check(env, environ=EXCLUSION)))

    def test_no_reason_carries_an_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = install(Path(tmp), owners=(OWNER, OTHER), users=(OWNER, EXCLUDED))
            text = json.dumps(ei.check(env, app_host=HOST, environ=EXCLUSION))
            for value in (OWNER, OTHER, EXCLUDED, TENANT, COMMITTED["client_id"]):
                self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
