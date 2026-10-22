"""The remote MCP host: Entra-signed-in employees over Streamable HTTP (D1, employee door).

Evidence label: ``isolated-fixture``. Access tokens are signed here with a generated
RSA key whose JWKS is injected into ``python3 -m operating.host mcp-http`` start-up;
the employees are Portal-shaped fixture rows; the data is the case console's synthetic
fixture world (``operator_ui.tests.case_fixtures``) projected into the host's projection
path. Nothing is live: no Entra, no network beyond loopback.
"""

from __future__ import annotations

import base64
import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import random
import secrets
import shutil
import tempfile
import threading
import time
import unittest

from operating import host as host_module
from operating import session_identity as si
from operating.oidc_auth import token_guard_entry, verify_access_token, EntraResource, StaticJwks
from operating.tests.test_session_identity import (EMAIL_A, EMAIL_B, OID_A, OID_B, OTHER_TID,
                                                   TID, identity_files, user_row)

AUDIENCE = "api://plx-workbench-test"
SCOPE = "Workbench.Read"
KID = "test-key-1"
_SMALL_PRIMES = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p ** .5) + 1, 2))]


def _probable_prime(n: int, rng: random.Random) -> bool:
    if any(n % p == 0 for p in _SMALL_PRIMES):
        return n in _SMALL_PRIMES
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(32):
        x = pow(rng.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def rsa_key(seed: int, bits: int = 2048) -> tuple[int, int, int]:
    """``(n, e, d)``: a test-only RSA key (pure Python, deterministic per seed)."""
    rng, e = random.Random(seed), 65537
    while True:
        primes = []
        while len(primes) < 2:
            candidate = rng.getrandbits(bits // 2) | (3 << (bits // 2 - 2)) | 1
            if _probable_prime(candidate, rng):
                primes.append(candidate)
        p, q = primes
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            return p * q, e, pow(e, -1, phi)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def jwks(key, kid=KID) -> dict:
    n, e, _ = key
    return {"keys": [{"kty": "RSA", "use": "sig", "kid": kid, "alg": "RS256",
                      "n": _b64(n.to_bytes((n.bit_length() + 7) // 8, "big")),
                      "e": _b64(e.to_bytes(3, "big"))}]}


def token(key, claims, header=None) -> str:
    n, _, d = key
    head = _b64(json.dumps(header or {"alg": "RS256", "typ": "JWT", "kid": KID}).encode())
    body = _b64(json.dumps(claims).encode())
    size = (n.bit_length() + 7) // 8
    digest = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(
        f"{head}.{body}".encode()).digest()
    encoded = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    signature = pow(int.from_bytes(encoded, "big"), d, n).to_bytes(size, "big")
    return f"{head}.{body}.{_b64(signature)}"


def claims(oid, *, tenant=TID, **override) -> dict:
    now = int(time.time())
    value = {"aud": AUDIENCE, "iss": f"https://login.microsoftonline.com/{tenant}/v2.0",
             "ver": "2.0", "tid": tenant, "oid": oid, "scp": f"openid {SCOPE}",
             "iat": now - 30, "nbf": now - 30, "exp": now + 3600,
             "uti": secrets.token_urlsafe(16)}
    value.update(override)
    return {k: v for k, v in value.items() if v is not None}


KEY = rsa_key(20261005)
OTHER_KEY = rsa_key(7)


def fixture_world(root: Path) -> tuple[Path, Path, Path]:
    """The fixture world projected where the host serves its projection: ``(state dir,
    mirror audience, projection store)``. The rows are synthetic; the host under test is
    the live employee host, so the seed's fixture marker is removed."""
    from operator_ui import object_workspace as objects
    from operator_ui.case_console import FIXTURE_MARKER
    from operator_ui.tests import case_fixtures

    state = root / "state"
    case_fixtures.seed(state, "plx")
    (state / FIXTURE_MARKER).unlink()
    store = host_module.projection_path(state)
    store.parent.mkdir(parents=True, exist_ok=True)
    objects.project_mirror(state, store)
    return state, state / case_fixtures.MIRROR_AUDIENCE_FILE, store


# A holds operations (every PO relation) and quality; B holds quality only.
USERS = [user_row(OID_A, EMAIL_A, "MANAGER"), user_row(OID_B, EMAIL_B, "STAFF", ("qa",))]
RULES = {"staff:operations": ("VIEW_OPERATIONS", "operations.sales", "ops"),
         "staff:quality": ("VIEW_OPERATIONS", "operations.quality", "qa")}


class EmployeeDoorTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="employee-door-"))
        cls.state, cls.mirror_audience, cls.store = fixture_world(cls.tmp)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)


class RemoteHostTests(EmployeeDoorTestCase):
    def setUp(self) -> None:
        self.policy, self.portal = identity_files(self.tmp / f"id-{self.id()}", RULES,
                                                  users=USERS)
        self.resource = self.tmp / f"entra-{self.id()}.json"
        self.resource.write_text(json.dumps({
            "schema": "plx-entra-resource/v1", "tenant_id": TID, "audiences": [AUDIENCE],
            "required_scope": SCOPE, "resource": "http://127.0.0.1:8766/mcp",
            "jwks_file": "absent-in-tests.json"}), "utf-8")
        config = self.tmp / f"host-{self.id()}.json"
        config.write_text(json.dumps({
            "schema": host_module.CONFIG_SCHEMA, "mirror_audience": str(self.mirror_audience),
            "employee_policy": str(self.policy), "portal_access": str(self.portal)}), "utf-8")
        ready: list = []
        started = threading.Event()

        def on_ready(server):
            ready.append(server)
            started.set()

        # The real start-up: python3 -m operating.host mcp-http, keys injected.
        thread = threading.Thread(target=host_module.main, kwargs={
            "argv": ["mcp-http", "--state-dir", str(self.state), "--config", str(config),
                     "--entra-resource", str(self.resource), "--port", "0"],
            "environ": {}, "jwks": StaticJwks(jwks(KEY)), "ready": on_ready}, daemon=True)
        thread.start()
        self.assertTrue(started.wait(60), "host did not start")
        self.server = ready[0]
        self.addCleanup(thread.join, 10)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.ids = iter(range(1, 1 << 30))

    def http(self, method, body=None, *, bearer=None, session=None, path="/mcp",
             headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=60)
        sent = {"Content-Type": "application/json", **(headers or {})}
        if bearer is not None:
            sent["Authorization"] = f"Bearer {bearer}"
        if session is not None:
            sent["Mcp-Session-Id"] = session
        connection.request(method, path, None if body is None else json.dumps(body), sent)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, raw

    def open(self, bearer):
        status, headers, raw = self.http("POST", {
            "jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"}}}, bearer=bearer)
        self.assertEqual(status, 200, raw)
        session = headers["mcp-session-id"]
        status, _, _ = self.http("POST", {"jsonrpc": "2.0",
                                          "method": "notifications/initialized"},
                                 bearer=bearer, session=session)
        self.assertEqual(status, 202)
        return session

    def call(self, bearer, session, name, arguments):
        status, _, raw = self.http("POST", {"jsonrpc": "2.0", "id": next(self.ids),
                                            "method": "tools/call", "params": {
                                                "name": name, "arguments": arguments}},
                                   bearer=bearer, session=session)
        return status, raw

    def test_two_employees_get_differently_trimmed_answers_and_nothing_leaks(self):
        a, b = token(KEY, claims(OID_A)), token(KEY, claims(OID_B))
        session_b = self.open(b)
        before_b = self.call(b, session_b, "plx.find", {"query": "PO5447"})
        session_a = self.open(a)
        status, raw_a = self.call(a, session_a, "plx.find", {"query": "PO5447"})
        self.assertEqual(status, 200)
        found = json.loads(raw_a)["result"]["structuredContent"]
        self.assertEqual(found["status"], "found")
        po = found["candidates"][0]["object"]["object_id"]
        status, raw_around = self.call(a, session_a, "plx.around", {"from": po, "path": [
            {"link_type": "PurchaseOrder.ordered_from", "direction": "forward"}]})
        self.assertEqual(status, 200)
        case = json.loads(self.call(a, session_a, "plx.case_open", {
            "question": "Why is PO5447 late?"})[1])["result"]["structuredContent"]
        # B after A: no cached answer, explanation, count or case of A's reaches B.
        status, raw_b = self.call(b, session_b, "plx.find", {"query": "PO5447"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw_b)["result"]["structuredContent"]["status"],
                         "not_found")
        self.assertEqual(json.loads(raw_b)["result"]["structuredContent"]["candidates"], [])
        self.assertEqual(json.loads(before_b[1])["result"]["structuredContent"]["withheld"],
                         json.loads(raw_b)["result"]["structuredContent"]["withheld"])
        from operating.answer_gate import HANDLE

        def handles(value):  # every handle-shaped string, wherever it sits
            if isinstance(value, dict):
                return set().union(set(), *map(handles, value.values()))
            if isinstance(value, list):
                return set().union(set(), *map(handles, value))
            return {value} if isinstance(value, str) and HANDLE.fullmatch(value) else set()

        a_only = {po} | handles(json.loads(raw_a)) | handles(json.loads(raw_around))
        self.assertTrue(len(a_only) > 2)
        status, raw_case = self.call(b, session_b, "plx.case_get",
                                     {"case_id": case["case_id"]})
        status_none, raw_none = self.call(b, session_b, "plx.case_get",
                                          {"case_id": "case-" + "0" * 16})
        self.assertEqual(json.loads(raw_case)["result"]["content"],
                         json.loads(raw_none)["result"]["content"])
        for raw in (raw_b, raw_case):
            text = raw.decode("utf-8")
            self.assertFalse([item for item in a_only if item in text], text[:400])
        # A's session is not B's to use, nor the reverse: it reads as unknown.
        status, _ = self.call(b, session_a, "plx.find", {"query": "PO5447"})
        self.assertEqual(status, 404)
        # The tool lists are the same; only what each employee may read differs.
        lists = [json.loads(self.http("POST", {"jsonrpc": "2.0", "id": next(self.ids),
                                               "method": "tools/list"}, bearer=t,
                                      session=s)[2])["result"]["tools"]
                 for t, s in ((a, session_a), (b, session_b))]
        self.assertEqual(lists[0], lists[1])

    def test_a_revoked_employee_or_token_loses_access_on_the_next_request(self):
        claims_a = claims(OID_A)
        a = token(KEY, claims_a)
        session = self.open(a)
        self.assertEqual(self.call(a, session, "plx.find", {"query": "PO5447"})[0], 200)
        # The Portal row goes inactive: the next request is refused and the session closed.
        identity_files(self.policy.parent, RULES,
                       users=[user_row(OID_A, EMAIL_A, "MANAGER", isActive=False), USERS[1]])
        status, raw = self.call(a, session, "plx.find", {"query": "PO5447"})
        self.assertEqual((status, json.loads(raw)["error"]["code"]), (403, "employee_refused"))
        identity_files(self.policy.parent, RULES, users=USERS)
        self.assertEqual(self.call(a, session, "plx.find", {"query": "PO5447"})[0], 404)
        # A revoked token id (the state directory's durable guard) is refused too.
        session = self.open(a)
        si.replay_guard_for(self.state).revoke(token_guard_entry(
            verify_access_token(a, EntraResource(TID, frozenset({AUDIENCE}), SCOPE,
                                                 "http://127.0.0.1:8766/mcp",
                                                 StaticJwks(jwks(KEY))))))
        self.assertEqual(self.call(a, session, "plx.find", {"query": "PO5447"})[0], 403)
        # Its sessions closed with it; a fresh token opens a new one.
        fresh = token(KEY, claims(OID_A))
        self.assertEqual(self.call(fresh, session, "plx.find", {"query": "PO5447"})[0], 404)
        self.assertEqual(self.call(fresh, self.open(fresh), "plx.find",
                                   {"query": "PO5447"})[0], 200)

    def test_bad_expired_foreign_and_app_only_tokens_are_refused_before_any_read(self):
        status, headers, raw = self.http("POST", {"jsonrpc": "2.0", "id": 1,
                                                  "method": "initialize", "params": {}})
        self.assertEqual(status, 401)
        self.assertIn('resource_metadata="http://127.0.0.1:8766/.well-known/'
                      'oauth-protected-resource/mcp"', headers["www-authenticate"])
        status, _, raw = self.http("GET", path="/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(json.loads(raw)["authorization_servers"],
                         [f"https://login.microsoftonline.com/{TID}/v2.0"])
        now = int(time.time())
        hs256 = token(KEY, claims(OID_A), {"alg": "HS256", "kid": KID})
        refused = {
            "expired": (token(KEY, claims(OID_A, iat=now - 7200, nbf=now - 7200,
                                          exp=now - 1)), 401),
            "forged": (token(OTHER_KEY, claims(OID_A)), 401),
            "foreign_tenant": (token(KEY, claims(OID_A, tenant=OTHER_TID)), 401),
            "foreign_tid_our_issuer": (token(KEY, claims(OID_A, tid=OTHER_TID)), 401),
            "other_audience": (token(KEY, claims(OID_A, aud="api://someone-else")), 401),
            "no_issuer_no_version": (token(KEY, claims(OID_A, iss=None, ver=None)), 401),
            "alg_hs256": (hs256, 401),
            "not_a_jwt": ("v1.abc.def", 401),
            "app_only": (token(KEY, claims(OID_A, scp=None, roles=["Workbench.Read"],
                                           idtyp="app")), 403),
            "scope_missing": (token(KEY, claims(OID_A, scp="openid")), 403),
            "unknown_employee": (token(KEY, claims("aaaaaaaa-0000-4000-8000-000000000009")),
                                 403),
        }
        for name, (bearer, expected) in refused.items():
            with self.subTest(name):
                status, headers, raw = self.http("POST", {
                    "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "t"}}}, bearer=bearer)
                self.assertEqual(status, expected, raw)
                self.assertNotIn("mcp-session-id", headers)
                self.assertNotIn(b"result", raw)


class StartupTests(EmployeeDoorTestCase):
    def test_the_remote_host_binds_loopback_only(self):
        from operating import mcp_http_host

        with self.assertRaises(SystemExit):
            mcp_http_host.main(["--state-dir", str(self.state), "--source-mirror-audience",
                                str(self.mirror_audience), "--employee-policy", "x",
                                "--entra-resource", "y", "--bind", "0.0.0.0"])


if __name__ == "__main__":
    unittest.main()
