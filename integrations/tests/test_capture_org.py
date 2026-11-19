"""Org chart capture over an injected Graph transport (evidence: injected-transport).

No network and no credentials: an in-process fake directory. Covers the boundary the
capture must hold: GET only through the closed grammar; the people guard fails closed
before any request; an excluded person is never stored and no request names them; a
manager who is excluded, deny-listed or a guest is withheld (no id kept); a 404 manager
is "none", any other failure "unread" and the run partial (nothing marked missing);
every user survives paging at N=1/10/60; a reader the audience does not grant sees
nothing; the CLI prints no id, name or address.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from integrations import capture_org as co
from integrations import graph_read
from integrations.live_source_guard import LiveSourceAudience
from ontology.types import Actor

X_ID = "0e0e0e0e-0000-4000-8000-00000000e0e1"   # the excluded person
ENV = {"PLX_EXCLUDED_PEOPLE_OIDS": X_ID,
       "MICROSOFT_GRAPH_TENANT_ID": "11111111-2222-3333-4444-555555555555",
       "MICROSOFT_GRAPH_CLIENT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
       "MICROSOFT_GRAPH_CLIENT_SECRET": "s3cr3t~GRAPH~value~do-not-leak"}
CAPTURE = Actor("plx", frozenset({co.CAPTURE_PRINCIPAL}), "restricted")
OTHER = Actor("plx", frozenset({"staff:org-withheld"}), "restricted")


def oid(n: int) -> str:
    return f"0000{n:04x}-0000-4000-8000-{n:012x}"


def user(ident, name, **extra):
    address = f"{name.lower()}@example-plx.com"
    return {"id": ident, "mail": address, "userPrincipalName": address,
            "displayName": name.title() + " Test", "proxyAddresses": [f"SMTP:{address}"],
            "userType": "Member", "accountEnabled": True, "jobTitle": "Planner",
            "department": "Operations", "officeLocation": "Plant 1", **extra}


class FakeDirectory:
    def __init__(self, n: int = 3) -> None:
        self.users = {oid(i): user(oid(i), f"user{i}") for i in range(1, n + 1)}
        self.users[X_ID] = user(X_ID, "xena")
        self.managers: dict[str, str] = {}
        self.fail_manager: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.page_cap = 1000

    def _page(self, parts, items):
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        top = min(int(query.get("$top", "1000")), self.page_cap)
        skip = int(query.get("$skip", "0"))
        page = {"value": items[skip:skip + top]}
        if skip + top < len(items):
            rest = {k: v for k, v in query.items() if k != "$skip"}
            page["@odata.nextLink"] = (f"https://graph.microsoft.com{parts.path}?"
                                       + urlencode({**rest, "$skip": skip + top}))
        return 200, {}, json.dumps(page).encode()

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url))
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return 200, {}, json.dumps({"access_token": "eyJ.T.s", "expires_in": 3599}).encode()
        assert method == "GET", method
        path = unquote(parts.path)
        if path == "/v1.0/users":
            return self._page(parts, list(self.users.values()))
        if path in ("/v1.0/groups", "/v1.0/organization"):
            return self._page(parts, [])
        found = re.fullmatch(r"/v1\.0/users/([^/]+)/manager", path)
        if found:
            ref = found[1].lower()
            if ref in self.fail_manager:
                return 503, {}, b'{"error": {"code": "serviceUnavailable"}}'
            boss = self.managers.get(ref)
            if boss is None:
                return 404, {}, b'{"error": {"code": "Request_ResourceNotFound"}}'
            return 200, {}, json.dumps({"id": boss}).encode()
        return 404, {}, b'{"error": {"code": "notFound"}}'


class OrgCaptureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.audience = LiveSourceAudience({"mail:directory": [co.CAPTURE_PRINCIPAL]},
                                           tenant="plx")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def capture(self, graph, env=ENV):
        api, _transport = graph_read.build_graph(env, sender=graph)
        store = co.open_store(self.state, self.audience)
        try:
            return co.OrgCapture(store, api).run()
        finally:
            store.close()

    def rows(self):
        return co.current_rows(self.state / co.STORE_DIR / co.DB_FILE)

    def test_reporting_lines_and_exclusion(self) -> None:
        graph = FakeDirectory(4)
        a, b, c, d = (oid(i) for i in range(1, 5))
        graph.users[d]["department"] = "Payroll"      # the HR / payroll deny list's
        graph.users[oid(9)] = user(oid(9), "guest", userType="Guest")
        graph.managers = {a: b, b: X_ID, c: d, d: a, oid(9): a}
        report = self.capture(graph)
        rows = self.rows()
        self.assertEqual(report["status"], "complete", report)
        self.assertNotIn(X_ID, rows)
        self.assertNotIn(oid(9), rows)
        self.assertEqual(rows[a]["manager_object_id"], b)
        self.assertEqual((rows[a]["job_title"], rows[a]["department"], rows[a]["office"]),
                         ("Planner", "Operations", "Plant 1"))
        # an excluded manager and a deny-listed one: withheld, no id
        self.assertEqual((rows[b]["manager_object_id"], rows[b]["manager_state"]),
                         (None, "withheld"))
        if d in rows:
            self.fail("a deny-listed user is never stored")
        self.assertEqual((rows[c]["manager_object_id"], rows[c]["manager_state"]),
                         (None, "withheld"))
        self.assertEqual(report["counts"]["users_excluded"], 1)
        self.assertEqual(report["counts"]["users_guest"], 1)
        # no request ever named the excluded person
        self.assertFalse([u for _m, u in graph.calls if X_ID in unquote(u).lower()])
        self.assertTrue(all(m == "GET" for m, u in graph.calls if "login." not in u))

    def test_exclusion_unusable_blocks_before_any_request(self) -> None:
        graph = FakeDirectory(2)
        env = {k: v for k, v in ENV.items() if k != "PLX_EXCLUDED_PEOPLE_OIDS"}
        report = self.capture(graph, env)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual([u for _m, u in graph.calls if "graph.microsoft.com" in u], [])
        self.assertEqual(self.rows(), {})

    def test_manager_failure_is_partial_and_marks_nothing_missing(self) -> None:
        graph = FakeDirectory(3)
        a, b, c = oid(1), oid(2), oid(3)
        graph.managers = {a: b, c: b}
        self.assertEqual(self.capture(graph)["status"], "complete")
        self.assertEqual(self.rows()[b]["manager_state"], "none")
        graph.fail_manager = {a}
        del graph.users[c]                       # gone, but this run is partial
        report = self.capture(graph)
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("managers_unread", report["reasons"])
        self.assertEqual(self.rows()[a]["manager_state"], "unread")
        self.assertIn(c, self.rows())
        graph.fail_manager = set()
        self.assertEqual(self.capture(graph)["status"], "complete")
        self.assertNotIn(c, self.rows())         # a complete run marks it missing

    def test_cardinality_paging(self) -> None:
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.tmp.cleanup()
                self.tmp = tempfile.TemporaryDirectory()
                self.state = Path(self.tmp.name)
                graph = FakeDirectory(n)
                graph.page_cap = 7
                graph.managers = {oid(i): oid(1) for i in range(2, n + 1)}
                report = self.capture(graph)
                rows = self.rows()
                self.assertEqual(report["status"], "complete")
                self.assertEqual(len(rows), n)
                self.assertEqual(sum(r["manager_state"] == "listed" for r in rows.values()),
                                 n - 1)

    def test_cli_counts_only_and_withheld_reader_sees_nothing(self) -> None:
        graph = FakeDirectory(3)
        graph.managers = {oid(1): oid(2)}
        aud = self.state / "aud.json"
        aud.write_text(json.dumps({"schema": "plx-live-source-audience/v1", "tenant": "plx",
                                   "audiences": {"mail:directory": [co.CAPTURE_PRINCIPAL]}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = co.main(["--state-dir", str(self.state / "s"), "--live-audience", str(aud)],
                           environ=ENV, sender=graph)
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0, report)
        self.assertEqual(report["capture_actor_rows"], 3)
        self.assertEqual(report["withheld_actor_rows"], 0)
        self.assertEqual(report["evidence_label"], "injected-transport")
        text = out.getvalue().lower()
        for leak in ("example-plx.com", "user1", oid(1), "planner", "s3cr3t"):
            self.assertNotIn(leak, text)

    def test_current_rows_reads_current_directory_rows_only(self) -> None:
        import sqlite3
        self.assertEqual(co.current_rows(self.state / "none.db"), {})
        path = self.state / "c.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE versions (kind TEXT, payload TEXT, superseded_at TEXT,"
                     " missing_from_source_since TEXT)")
        good = {"object_id": oid(1).upper(), "manager_state": "none"}
        at = "2026-11-18T00:00:00Z"
        conn.executemany("INSERT INTO versions VALUES (?, ?, ?, ?)", [
            ("org_user", json.dumps({"row": good}), None, None),
            ("org_user", json.dumps({"row": {**good, "object_id": oid(2)}}), at, None),
            ("org_user", json.dumps({"row": {**good, "object_id": oid(3)}}), None, at),
            ("org_user", json.dumps({"row": {**good, "object_id": oid(4),
                                             "manager_state": "x"}}), None, None),
            ("org_user", json.dumps({"row": {**good, "object_id": "not-an-id"}}), None, None),
            ("other", json.dumps({"row": {**good, "object_id": oid(5)}}), None, None),
            ("org_user", "not json", None, None)])
        conn.commit()
        conn.close()
        self.assertEqual(list(co.current_rows(path)), [oid(1)])


if __name__ == "__main__":
    unittest.main()
