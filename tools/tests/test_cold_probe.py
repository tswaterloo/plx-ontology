"""SV0 (speed specs 1.4-1.6): the cold probe's PASS / FAIL / INVALID lines against a stub API, what it
refuses to guess, the 1/10/60 scale helper on a real Postgres, and page equivalence's permission
finding."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from tools import cold_probe as cp
from tests import scale
from tools import page_equiv as pe

B = lambda ms, family="computed": SimpleNamespace(budget_ms=ms, family=family)  # noqa: E731
BUDGETS = {"/api/fast": B(60), "/api/slow": B(60), "/api/late": B(60),
           "/api/object/{object_type}/{object_id:path}": B(60),
           "/api/page/{kind}/{key:path}": B(60),
           "/api/team/{key}": B(60), "/api/files/{file_id}": B(None, "file")}
OPENAPI = {"paths": {"/api/page/{kind}/{key}": {"get": {"parameters": [
    {"in": "path", "name": "kind", "required": True, "schema": {"enum": ["record", "home"]}}]}}}}


class Stub(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        self.send_response(200)
        self.send_header("Set-Cookie", "plx_app_session=tok123; HttpOnly; Path=/api")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):
        Stub.seen.append((self.path, self.headers.get("Cookie")))
        if "plx_app_session=tok123" not in (self.headers.get("Cookie") or ""):
            self.send_response(401)
            self.end_headers()
            return
        if self.path.startswith("/api/slow"):
            time.sleep(0.12)
        body = {"status": "not_run"} if self.path.startswith("/api/late") else {"ok": True}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


class ColdProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.link = Path(tempfile.mkdtemp()) / "link"
        cls.link.write_text("https://example.invalid/#ticket=abc")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def owner(self):
        return cp.sessions_for(self.base, ["owner"], {"owner": str(self.link)}, {})

    def run_probe(self, wanted, sessions=None, ids=None, n=5):
        return cp.probe(cp.routes(wanted, BUDGETS, OPENAPI), sessions or self.owner(), ids or {},
                        n=n, given={})

    def test_pass_line_when_every_route_meets_its_budget(self):
        lines = self.run_probe("/api/fast")
        self.assertEqual(len(lines[0].all_ms), 5 + cp.CONCURRENT)
        code, out = cp.verdict(lines, load=1.0, cores=16, inline_door=0)
        self.assertEqual(code, 0)
        self.assertRegex(out[0], r"^COLD PROBE PASS routes=1 requests=13 worst=/api/fast p99=\d+ "
                                 r"over_budget=0 not_run=0 inline_door=0 load=1\.0$")

    def test_fail_lists_each_failing_route_slow_not_run_unmeasured(self):
        lines = self.run_probe("/api/fast,/api/slow,/api/late,/api/team")
        code, out = cp.verdict(lines, load=1.0, cores=16, inline_door=0, scale=1.0)
        self.assertEqual(code, 1)
        self.assertTrue(out[0].startswith("COLD PROBE FAIL routes=4 "), out[0])
        self.assertIn("over_budget=1 not_run=13", out[0])
        text = "\n".join(out)
        self.assertRegex(text, r"FAIL /api/slow reader=owner p99=1\d\d budget=60 .*why=over_budget")
        self.assertIn("FAIL /api/late reader=owner", text)
        self.assertIn("why=not_run 13", text)
        # a parameter nothing fills is a failing line naming it, never a guessed value
        self.assertIn("FAIL /api/team/{key} reader=owner p99=0 budget=60 n=0 first_ms=None "
                      "why=not_measured (no value for {key})", text)
        self.assertNotIn("/api/fast", "\n".join(out[1:]))

    def test_invalid_under_overload_or_unknown_load_and_door_unmeasured_fails(self):
        lines = self.run_probe("/api/fast")
        self.assertEqual(cp.verdict(lines, load=17.5, cores=16, inline_door=0),
                         (3, ["COLD PROBE INVALID load=17.5 cores=16"]))
        self.assertEqual(cp.verdict(lines, load=None, cores=16, inline_door=0)[0], 3)
        code, out = cp.verdict(lines, load=1.0, cores=16, inline_door=None)
        self.assertEqual(code, 1)
        self.assertIn("inline_door=unmeasured", out[0])
        code, out = cp.verdict(lines, load=1.0, cores=16, inline_door=2)
        self.assertEqual(code, 1)
        self.assertIn("FAIL inline_door=2", out[1])

    def test_readers_without_a_session_and_view_as_outside_its_routes_are_failing_lines(self):
        sessions = cp.sessions_for(self.base, ["owner", "lead", "l3"], {"owner": str(self.link)},
                                   {"l3": "k3"})
        lines = cp.probe(cp.routes("/api/fast", BUDGETS, OPENAPI), sessions, {}, n=2, given={},
                         view_as_routes={"/api/home"})
        text = "\n".join(cp.verdict(lines, load=1.0, cores=16, inline_door=0)[1])
        self.assertIn("reader=lead p99=0 budget=60 n=0 first_ms=None why=not_measured (reader "
                      "lead has no session", text)
        self.assertIn("reader=l3 p99=0 budget=60 n=0 first_ms=None why=not_measured (view-as "
                      "does not serve this route)", text)

    def test_record_routes_fill_from_drawn_ids_and_route_prefix_fills_kind(self):
        Stub.seen.clear()
        ids = {"PurchaseOrder": ["po-1", "po-2"], "SalesOrder": ["so/1"]}
        rs = cp.routes("/api/page/record", BUDGETS, OPENAPI)
        self.assertEqual([r.label for r in rs], ["/api/page/record/{key:path}"])
        cp.probe(rs, self.owner(), ids, n=2, given={})
        paths = {p for p, _ in Stub.seen}
        self.assertTrue({"/api/page/record/po-1", "/api/page/record/so%2F1"} <= paths, paths)
        # an enum the contract lists is probed one line per value
        self.assertEqual(sorted(r.label for r in cp.routes("/api/page", BUDGETS, OPENAPI)),
                         ["/api/page/home/{key:path}", "/api/page/record/{key:path}"])
        with self.assertRaises(cp.Usage):
            cp.routes("/api/nothing", BUDGETS, OPENAPI)
        with self.assertRaises(cp.Usage):
            cp.routes("/api/files", BUDGETS, OPENAPI)

    def test_main_on_the_real_registry_prints_pass_and_records_seen_ids(self):
        out, seen = io.StringIO(), Path(tempfile.mkdtemp()) / "seen.txt"
        with mock.patch.object(cp, "load_of_host", return_value=(1.0, 16)), redirect_stdout(out):
            self.assertEqual(cp.main(["--routes", "/api/health", "--base", self.base]), 2)
            code = cp.main(["--routes", "/api/health", "--n", "3", "--base", self.base,
                            "--deploy-gate",
                            "--session", f"owner={self.link}", "--server-log-cmd", "true",
                            "--seen", str(seen)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue(out.getvalue().startswith("COLD PROBE PASS routes=1 requests=11 "
                                                  "worst=/api/health"), out.getvalue())
        self.assertIn(f"seen file {seen} absent", out.getvalue())


try:
    import psycopg
    from operating.pg import scratch_db
    scratch_db.use_test_cluster()
    _NAME, _DSN = scratch_db.create_db()
except Exception as _err:  # noqa: BLE001 - no local Postgres: the scale case is skipped, said
    _NAME = _DSN = None
    _WHY = f"no scratch Postgres: {type(_err).__name__}: {_err}"


def tearDownModule() -> None:
    if _NAME:
        scratch_db.drop_db(_NAME)


@unittest.skipIf(_DSN is None, globals().get("_WHY", ""))
class ScaleCase(unittest.TestCase):
    def setUp(self):
        self.pg = psycopg.connect(_DSN, autocommit=True)
        self.addCleanup(self.pg.close)
        self.pg.execute("CREATE TABLE IF NOT EXISTS t (k int PRIMARY KEY, v text)")
        self.pg.execute("INSERT INTO t SELECT g, 'v' || g FROM generate_series(1, 60) g "
                        "ON CONFLICT DO NOTHING")

    def test_records_are_never_drawn_from_a_primary(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            code = cp.main(["--routes", "/api/object", "--deploy-gate", "--base",
                            "http://127.0.0.1:9", "--dsn", _DSN])
        self.assertEqual(code, 2)
        self.assertIn("--dsn is the primary", err.getvalue())

    def test_set_based_read_passes_and_a_per_record_loop_fails(self):
        got = scale.assert_flat(lambda n: self.pg.execute(
            "SELECT v FROM t WHERE k = ANY(%s)", (list(range(1, n + 1)),)).fetchall(), pg=self.pg)
        self.assertEqual({c for c, _ in got.values()}, {1})

        def per_record(n):
            with self.pg.cursor() as cur:
                for k in range(1, n + 1):
                    cur.execute("SELECT v FROM t WHERE k = %s", (k,))
        with self.assertRaisesRegex(AssertionError, r"statements grow with N: \{1: 1, 10: 10, 60: 60\}"):
            scale.assert_flat(per_record, pg=self.pg)
        with self.assertRaisesRegex(AssertionError, "no statement ran"):
            scale.assert_flat(lambda n: None, pg=self.pg)


class Equivalence(unittest.TestCase):
    def test_classes_and_a_staff_extra_is_a_permission_finding(self):
        old = {"lines": [{"handle": "a", "q": 1}, {"handle": "b", "q": 2}],
               "mail": [{"v": {"handle": "m1", "subject": "x"}, "withheld": []}]}
        new = {"lines": [{"handle": "b", "q": 2}, {"handle": "a", "q": 1}, {"handle": "c", "q": 3}],
               "mail": [{"v": None, "withheld": ["payload"]}, {"handle": "x"}]}
        kinds = sorted(d["kind"] for d in pe.classify(old, {"lines": new["lines"]}))
        # c is new, the mail section is gone, and a / b swapped places
        self.assertEqual(kinds, ["order", "stored_extra", "stored_missing"])
        same_order = pe.classify({"s": [{"handle": "a"}, {"handle": "b"}]},
                                 {"s": [{"handle": "b"}, {"handle": "a"}]})
        self.assertEqual([d["kind"] for d in same_order], ["order"])
        withheld = pe.classify({"s": [{"handle": "a", "v": {"p": 1}, "withheld": []}]},
                               {"s": [{"handle": "a", "v": None, "withheld": ["payload"]}]})
        self.assertEqual([d["kind"] for d in withheld], ["withheld"])

        readers = [cp.Session("owner", ""), cp.Session("l2", "")]
        ids = {"owner": ["r1"], "l2": ["r1", "r2"]}
        old_src = lambda s, t, i: (200, {"s": [{"handle": "a"}]})  # noqa: E731
        new_src = lambda s, t, i: (200, {"s": [{"handle": "a"}, {"handle": "leak"}]})  # noqa: E731
        counts, found = pe.compare(readers, ids, "PurchaseOrder", old_src, new_src)
        self.assertEqual(counts, {"same": 0, "different": 3, "old_failed": 0})
        self.assertEqual({f["reader"] for f in pe.permission_findings(found)}, {"l2"})

        def broken(s, t, i):
            raise RuntimeError("old page down")
        counts, found = pe.compare(readers, ids, "PurchaseOrder", broken, old_src)
        self.assertEqual(counts, {"same": 0, "different": 0, "old_failed": 3})
        self.assertEqual(found[0]["old_status"], "error RuntimeError")


class ProbeTicketsTest(unittest.TestCase):
    """The probe's own readers: tickets signed with a throwaway secret, valid for no other."""
    SNAP = {"users": [
        {"id": "u1", "email": "a@x.test", "entraTenantId": "t1", "entraObjectId": "o1",
         "isActive": True},
        {"id": "u2", "email": "b@x.test", "entraTenantId": "t1", "entraObjectId": "o2",
         "isActive": False},
        {"id": "u3", "email": "c@x.test", "entraTenantId": "t1", "entraObjectId": "o3",
         "isActive": True},
        {"id": "u4", "email": "d@x.test", "entraTenantId": "t1", "entraObjectId": "o3",
         "isActive": True}]}

    def test_a_ticket_verifies_only_with_the_probes_secret(self):
        from operating import session_identity as si
        ok, refused = cp.mint_tickets("a" * 64, self.SNAP, {"owner": "o1", "lead": "o2",
                                                              "l3": "o3", "l2": "nobody"})
        self.assertEqual(list(ok), ["owner"])
        claims = si.verify_session_ticket(ok["owner"], "a" * 64)
        self.assertEqual((claims["email"], claims["entraObjectId"], claims["author"]),
                         ("a@x.test", "o1", si.AUTHOR_HUMAN))
        self.assertLessEqual(claims["exp"] - claims["iat"], cp.TICKET_TTL_SECONDS)
        with self.assertRaises(si.SessionIdentityRefused):
            si.verify_session_ticket(ok["owner"], "b" * 64)
        # an inactive row, two rows for one id and no row: refused with a reason, never guessed
        self.assertEqual(sorted(refused), ["l2", "l3", "lead"])
        self.assertTrue(all("no single active Portal row" in v for v in refused.values()))

    def test_sessions_in_ticket_mode_sign_in_by_ticket_and_never_by_the_real_links(self):
        seen = []
        sessions = cp.sessions_for("http://x", ["owner", "lead", "l2"], {"owner": "/real/link"},
                                   {}, tickets={"owner": "T1"},
                                   refused={"lead": "reader lead: no single active Portal row"},
                                   ticket_signer=lambda b, t: seen.append(t) or "cookie-" + t)
        self.assertEqual(seen, ["T1"])
        self.assertEqual([(s.name, s.cookie) for s in sessions],
                         [("owner", "cookie-T1"), ("lead", None), ("l2", None)])
        self.assertIn("no single active", sessions[1].missing)
        self.assertIn("no --reader-id", sessions[2].missing)

    def test_reader_id_needs_a_fresh_process_and_a_host_config(self):
        base = ["--routes", "/api/fast", "--reader-id", "owner=o1"]
        self.assertEqual(cp.main(base + ["--dsn", "x"]), 2)                      # no --fresh-process
        self.assertEqual(cp.main(base + ["--fresh-process", "--dsn", "x"]), 2)   # no --host-config


if __name__ == "__main__":
    unittest.main()
