"""``X-PLX-Door`` / ``X-PLX-Session`` (SPEED-SPECS 10.9, the desktop store's wipe rules):
the door value is the session's own, moves when the reader's access decision moves and
with nothing else, is absent when any part is unknown, and a refused session says so only when it
sent credentials."""

from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from operator_api.app import create_app
from operator_api.auth import SESSION_COOKIE, DevAuth, fixture_identity
from operator_api.door_value import DOOR_HEADER, SESSION_HEADER
from operator_api.mock import MockBackend


def client(mark, principals=("staff:demo",)) -> TestClient:
    backend = MockBackend()
    backend.doors = SimpleNamespace(access_mark=lambda who: mark[0], decide=lambda who: True)
    app = create_app(backend, DevAuth(fixture_identity("plx", list(principals)), mode="mock"),
                     secure_cookie=False)
    c = TestClient(app, base_url="http://testserver")
    assert c.post("/api/auth/dev-login", json={}).status_code == 200
    return c


class DoorHeader(unittest.TestCase):
    def test_constant_for_an_unchanged_reader_and_moves_with_the_access_mark(self):
        mark = ["a" * 32]
        c = client(mark)
        first = c.get("/api/me").headers.get(DOOR_HEADER)
        self.assertRegex(first or "", r"^[0-9a-f]{32}$")
        for _ in range(5):   # reads and refresh cycles in between change nothing
            self.assertEqual(c.get("/api/me").headers.get(DOOR_HEADER), first)
        mark[0] = "b" * 32
        moved = c.get("/api/me").headers.get(DOOR_HEADER)
        self.assertNotEqual(moved, first)
        self.assertRegex(moved, r"^[0-9a-f]{32}$")

    def test_two_readers_never_share_a_value(self):
        a = client(["a" * 32], ("staff:demo",)).get("/api/me").headers.get(DOOR_HEADER)
        b = client(["a" * 32], ("staff:demo", "team:extra")).get("/api/me").headers.get(DOOR_HEADER)
        self.assertTrue(a and b)
        self.assertNotEqual(a, b)

    def test_the_exclusion_rule_moves_it_and_a_unreadable_rule_omits_it(self):
        c = client(["a" * 32])
        base = c.get("/api/me").headers.get(DOOR_HEADER)
        import operator_api.door_value as dv
        with mock.patch.object(dv, "_rule", (0.0, None)), \
                mock.patch("operating.pg.permissions.exclusion_rule_stamp", return_value="other-list"):
            self.assertNotEqual(c.get("/api/me").headers.get(DOOR_HEADER), base)
        with mock.patch.object(dv, "_rule", (0.0, None)), \
                mock.patch("operating.pg.permissions.exclusion_rule_stamp", side_effect=OSError):
            self.assertIsNone(c.get("/api/me").headers.get(DOOR_HEADER))

    def test_unknown_access_mark_or_no_doors_means_no_header_never_a_guess(self):
        self.assertIsNone(client([None]).get("/api/me").headers.get(DOOR_HEADER))
        self.assertIsNone(client([7]).get("/api/me").headers.get(DOOR_HEADER))
        plain = TestClient(create_app(MockBackend(), DevAuth(fixture_identity("plx", ["staff:demo"]),
                                                             mode="mock"), secure_cookie=False))
        plain.post("/api/auth/dev-login", json={})
        self.assertIsNone(plain.get("/api/me").headers.get(DOOR_HEADER))

    def test_canary_route_refusal_does_not_emit_session_refused_header(self):
        import dataclasses
        from operating import session_identity as si

        backend = MockBackend()
        canary_auth = DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock")
        orig_auth = canary_auth.authenticate

        def authenticate_as_canary(headers, cookies):
            who = orig_auth(headers, cookies)
            sess = copy.copy(who.session)
            object.__setattr__(sess, "service", si.SERVICE_CANARY)
            return dataclasses.replace(who, session=sess)

        canary_auth.authenticate = authenticate_as_canary  # type: ignore[method-assign]
        app = create_app(backend, canary_auth, secure_cookie=False)
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/api/auth/dev-login", json={}).status_code == 200
        # A route disallowed for a read-only service session returns 403 canary_route_refused
        # without X-PLX-Session: refused so sibling in-flight browser reads are never aborted.
        res = c.get("/api/assist/workflows")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["error"]["code"], "canary_route_refused")
        self.assertIsNone(res.headers.get(SESSION_HEADER))

    def test_access_mark_ignores_data_volume_and_encoding_but_not_access(self):
        from operating.pg import door as pgdoor
        decided = {"settings": {"plx.classes": "{1,4}", "plx.dclasses": "{}",
                                "plx.held": "{[1,2),[4,5)}", "plx.dheld": "{}",
                                "plx.u_amount": "{1,3}", "plx.r_amount": "{[1,2),[3,4)}",
                                "plx.d_hidden": "{doc-a,doc-b}"},
                   "withheld": {"orders": {"amount": 4}}, "refused": [], "docs": False,
                   "revoked": 0}
        base = pgdoor.access_mark(decided, "docs-digest", "grant-digest")
        self.assertRegex(base or "", r"^[0-9a-f]{32}$")
        same = copy.deepcopy(decided)
        same["withheld"]["orders"]["amount"] = 999       # rows arrived or left
        same["settings"]["plx.held"] = "{[1,2),[4,5)}"
        same["settings"]["plx.r_amount"] = "{[1,2),[3,4)}"
        self.assertEqual(pgdoor.access_mark(same, "docs-digest", "grant-digest"), base)

        # plx.d_hidden change MUST move access_mark (lineage added a hidden doc to a row)
        dhidden = copy.deepcopy(decided)
        dhidden["settings"]["plx.d_hidden"] = "{doc-a,doc-b,doc-z}"
        self.assertNotEqual(pgdoor.access_mark(dhidden, "docs-digest", "grant-digest"), base)

        # revocation epoch bump MUST move access_mark (even with identical classes/columns)
        revoked = copy.deepcopy(decided)
        revoked["revoked"] = 1
        self.assertNotEqual(pgdoor.access_mark(revoked, "docs-digest", "grant-digest"), base)

        # column allow-list change and documents_digest change MUST move access_mark
        moved = copy.deepcopy(decided)
        moved["settings"]["plx.u_amount"] = "{1}"       # a column's access changed
        self.assertNotEqual(pgdoor.access_mark(moved, "docs-digest", "grant-digest"), base)
        self.assertNotEqual(pgdoor.access_mark(decided, "other-documents", "grant-digest"), base)

        # fail closed when docs=True and documents_digest is missing or empty
        with_docs = copy.deepcopy(decided)
        with_docs["docs"] = True
        self.assertIsNone(pgdoor.access_mark(with_docs, None, "grant-digest"))
        self.assertIsNone(pgdoor.access_mark(with_docs, "", "grant-digest"))
        self.assertRegex(pgdoor.access_mark(with_docs, "docs-digest", "grant-digest") or "",
                         r"^[0-9a-f]{32}$")

        # fail closed when revoked is missing, None, bool, or negative, or settings incomplete
        for bad_revoked in (None, True, -1, "0"):
            bad = copy.deepcopy(decided)
            bad["revoked"] = bad_revoked
            self.assertIsNone(pgdoor.access_mark(bad, "docs-digest", "grant-digest"))
        no_revoked = copy.deepcopy(decided)
        del no_revoked["revoked"]
        self.assertIsNone(pgdoor.access_mark(no_revoked, "docs-digest", "grant-digest"))
        no_dhidden = copy.deepcopy(decided)
        del no_dhidden["settings"]["plx.d_hidden"]
        self.assertIsNone(pgdoor.access_mark(no_dhidden, "docs-digest", "grant-digest"))
        self.assertIsNone(pgdoor.access_mark({"settings": {}, "refused": [], "docs": False,
                                              "revoked": 0}))

    def test_decide_marks_access_before_range_settings_replace_the_arrays(self):
        from operating import ontology_tools_sql as sqlt
        from operating.pg import door as pgdoor
        scope = SimpleNamespace(audience_keys=("audience",), marking_sets=(), held=())
        grant = sqlt.reader_grant(scope)
        decided = {"settings": {"plx.classes": "{1}", "plx.dclasses": "{}",
                                "plx.held": "{[1,2)}", "plx.dheld": "{}",
                                "plx.u_amount": "{1}", "plx.r_amount": "{[1,2)}",
                                "plx.d_hidden": "{}"},
                   "withheld": {}, "refused": [], "docs": False, "revoked": 0}

        def decide_at(stamp, ranges_only):
            with mock.patch.object(pgdoor, "check", return_value=(stamp, True)), \
                    mock.patch.object(pgdoor, "reader_scope", return_value=copy.deepcopy(decided)), \
                    mock.patch.object(sqlt, "_ranges_only", return_value=ranges_only):
                return sqlt._decide(None, grant)

        arrays = decide_at(7, False)
        ranges = decide_at(8, True)     # the schema-wide stamp moved; this access did not
        self.assertEqual(arrays["access_mark"], ranges["access_mark"])
        self.assertNotIn("plx.u_amount", ranges["settings"])   # representation stripped after marking
        self.assertEqual(ranges["access_mark"], pgdoor.access_mark(decided, grant_digest=grant.digest))

    def test_real_doors_with_door_object_emits_header(self):
        from operating.ontology_tools_sql import Door
        from operator_api.doors import Doors

        class DoorWorker:
            def __init__(self):
                self.stamp = 7
                self.mark = "a" * 32

            def run(self, who, job, timeout=None):
                door_obj = Door(SimpleNamespace(digest="grant-1"))
                door_obj.decided = {"access_mark": self.mark}
                return door_obj, "dsn", self.stamp, {}

        worker = DoorWorker()
        backend = MockBackend()
        backend.access_worker = worker
        backend.doors = Doors(backend)
        app = create_app(backend, DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock"),
                         secure_cookie=False)
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/api/auth/dev-login", json={}).status_code == 200
        first = c.get("/api/me").headers.get(DOOR_HEADER)
        self.assertRegex(first or "", r"^[0-9a-f]{32}$")
        # ordinary refresh bumps schema stamp without changing access_mark -> header unchanged
        worker.stamp = 8
        backend.doors.decide(next(iter(backend.doors._held.values())).reader)
        self.assertEqual(c.get("/api/me").headers.get(DOOR_HEADER), first)
        # revocation changes access_mark -> header moves
        worker.stamp = 9
        worker.mark = "b" * 32
        backend.doors.decide(next(iter(backend.doors._held.values())).reader)
        moved = c.get("/api/me").headers.get(DOOR_HEADER)
        self.assertRegex(moved or "", r"^[0-9a-f]{32}$")
        self.assertNotEqual(moved, first)

    def test_view_as_skips_access_mark_lookup(self):
        calls = []
        backend = MockBackend()
        backend.doors = SimpleNamespace(
            access_mark=lambda who: calls.append(who) or ("a" * 32),
            decide=lambda who: True)
        app = create_app(backend, DevAuth(fixture_identity("plx", ["staff:demo"]), mode="mock"),
                         secure_cookie=False)
        c = TestClient(app, base_url="http://testserver")
        assert c.post("/api/auth/dev-login", json={}).status_code == 200
        calls.clear()
        got = c.get("/api/late", headers={"X-PLX-View-As": "0" * 20})
        self.assertEqual(got.status_code, 403)
        self.assertIsNone(got.headers.get(DOOR_HEADER))
        self.assertEqual(calls, [])

    def test_a_refused_session_says_so_only_when_it_sent_credentials(self):
        c = client(["a" * 32])
        bad = TestClient(c.app, base_url="http://testserver", cookies={SESSION_COOKIE: "x" * 20})
        refused = bad.get("/api/me")
        self.assertEqual((refused.status_code, refused.headers.get(SESSION_HEADER)), (401, "refused"))
        bare = TestClient(c.app, base_url="http://testserver").get("/api/me")
        self.assertEqual(bare.status_code, 401)
        self.assertIsNone(bare.headers.get(SESSION_HEADER))   # nobody signed in: says nothing about a person
        self.assertIsNone(bare.headers.get(DOOR_HEADER))

    def test_an_ordinary_route_403_is_not_a_refused_session(self):
        c = client(["a" * 32])
        got = c.get("/api/late", headers={"X-PLX-View-As": "0" * 20})   # a non-owner asking to view as
        self.assertEqual(got.status_code, 403)
        self.assertIsNone(got.headers.get(SESSION_HEADER))
        self.assertIsNone(got.headers.get(DOOR_HEADER))   # a view-as read carries no door: nothing of it is kept


class DoorRevocationStabilityPostgres(unittest.TestCase):
    """Real-Postgres regression tests proving:
    (i) ordinary non-revoking data refreshes do NOT move ``access_mark``, and
    (ii) row-level exclusion (``door.exclude``), column marking tightening, audience
    tightening (all reusing EXISTING class IDs), and ``plx.d_hidden`` lineage updates
    ALL move ``access_mark``."""

    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_testdb
        cls.pg_testdb = pg_testdb
        cls.db_name, cls.dsn = pg_testdb.create_db()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "db_name", None):
            cls.pg_testdb.drop_db(cls.db_name)

    def _setup_schema(self, conn, schema: str) -> None:
        from operating.pg import door as pgdoor, people_excluded as pe
        from operating.pg.access_classes import OWNER_KEY
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute("CREATE SCHEMA IF NOT EXISTS ontology")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {pe.TABLE} ("
                     "l1_handle text NOT NULL, object_id text NOT NULL, reason text NOT NULL)")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {pe.STATE} ("
                     "one boolean PRIMARY KEY DEFAULT true CHECK (one), "
                     "exclusion text, covered_ver bigint)")
        conn.execute(f"TRUNCATE {pe.TABLE}")
        conn.execute(f"DELETE FROM {pe.STATE}")
        conn.execute(f"INSERT INTO {pe.STATE} (one, exclusion, covered_ver) VALUES (true, 't1', 1)")
        pgdoor.ensure(conn, schema)
        # Pre-seed ALL access_classes and col_classes up front so later exclusions /
        # tightening REUSE existing class IDs without appending new rows.
        conn.execute(
            f"INSERT INTO {schema}.access_classes (id, audience_keys, rec_marks, doc_marks, blocked) VALUES "
            "(1, ARRAY['bc:plx'], ARRAY['[]'], '{}', false), "
            f"(2, ARRAY['bc:plx', '{OWNER_KEY}'], ARRAY['[]'], '{{}}', false), "
            "(3, ARRAY['bc:plx', 'dept:hr'], ARRAY['[]'], '{}', false), "
            f"(4, ARRAY['bc:plx'], ARRAY['[]'], ARRAY['{pgdoor.UNREADABLE}'], false)")
        conn.execute(
            f"INSERT INTO {schema}.col_classes (id, marks, needs) VALUES "
            "(5, '{\"margin\": \"[\\\"finance\\\"]\"}'::jsonb, '{}'::jsonb)")
        conn.execute(
            f"CREATE TABLE {schema}.orders ("
            "handle text PRIMARY KEY, total numeric, margin numeric, "
            "access_class integer NOT NULL, col_class integer NOT NULL, ai_ok boolean NOT NULL)")
        conn.execute(
            f"INSERT INTO {schema}.orders VALUES "
            "('PO:1', 100, 20, 1, 0, true), "
            "('PO:2', 200, 40, 1, 0, true), "
            "('PO:3', 300, 60, 4, 5, true)")
        conn.execute(
            f"INSERT INTO {schema}.lineage (view, handle, object_ids, read_ids, col_objects) VALUES "
            "('orders', 'PO:1', ARRAY['obj:po1'], '{}', '{}'::jsonb), "
            "('orders', 'PO:2', ARRAY['obj:po2'], '{}', '{}'::jsonb), "
            "('orders', 'PO:3', ARRAY['obj:po3'], '{}', '{}'::jsonb)")
        conn.execute(f"INSERT INTO {schema}.door_views VALUES ('orders', ARRAY['margin'])")
        pgdoor.recount(conn, schema, "orders")
        conn.execute(f"UPDATE {schema}.door_state SET excluded_at = %s",
                     (pgdoor.exclusion_stamp(conn),))

    def _staff_mark(self, conn, schema: str, *, hidden_docs=(), docs_digest="docs-d1:1") -> str:
        from operating.pg import door as pgdoor
        decided = pgdoor.reader_scope(
            conn, schema, audience_keys=("bc:plx",), marking_sets=("[]",), held=(),
            holds_every_doc=False, hidden_docs=lambda: (list(hidden_docs), True))
        mark = pgdoor.access_mark(decided, docs_digest if decided["docs"] else None, "staff-grant")
        self.assertIsNotNone(mark)
        return mark

    def test_ordinary_refresh_keeps_access_mark_and_reused_class_revocations_move_it(self):
        import psycopg
        from operating.pg import door as pgdoor, people_excluded as pe

        schema = "biz_door_rev"
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            self._setup_schema(conn, schema)
            base_mark = self._staff_mark(conn, schema, hidden_docs=("Attachment:secret",))
            base_classes = conn.execute(
                f"SELECT count(*) FROM {schema}.access_classes").fetchone()[0]
            base_cols = conn.execute(
                f"SELECT count(*) FROM {schema}.col_classes").fetchone()[0]

            # (1) Ordinary non-revoking data refresh: new row added (with finance col_class 5,
            # changing withheld counts), business values updated, table rebuilt with identical
            # (access_class, col_class) on surviving handles, door._bump moves door_state.stamp.
            conn.execute(f"CREATE TABLE {schema}.orders_next (LIKE {schema}.orders)")
            conn.execute(
                f"INSERT INTO {schema}.orders_next VALUES "
                "('PO:1', 150, 25, 1, 0, true), "
                "('PO:2', 250, 45, 1, 0, true), "
                "('PO:3', 350, 65, 4, 5, true), "
                "('PO:4', 400, 80, 1, 5, true)")
            self.assertFalse(pgdoor.classes_changed(conn, schema, "orders", f"{schema}.orders_next"))
            conn.execute(f"DROP TABLE {schema}.orders")
            conn.execute(f"ALTER TABLE {schema}.orders_next RENAME TO orders")
            pgdoor.recount(conn, schema, "orders")
            pgdoor._bump(conn, schema)
            after_refresh = self._staff_mark(conn, schema, hidden_docs=("Attachment:secret",))
            self.assertEqual(after_refresh, base_mark)

            # (2) Row-level exclusion (door.exclude) reusing EXISTING OWNER_KEY class 2 MUST move it
            conn.execute(f"INSERT INTO {pe.TABLE} VALUES ('h:po1', 'obj:po1', 'excluded')")
            conn.execute(f"UPDATE {pe.STATE} SET covered_ver = 2")
            gone = pgdoor.exclude(conn, schema)
            self.assertEqual(gone, 1)
            self.assertEqual(conn.execute(
                f"SELECT count(*) FROM {schema}.access_classes").fetchone()[0], base_classes)
            after_exclude = self._staff_mark(conn, schema, hidden_docs=("Attachment:secret",))
            self.assertNotEqual(after_exclude, after_refresh)

            # (3) Row-level column marking tightening reusing EXISTING col_class 5 MUST move it
            conn.execute(f"CREATE TABLE {schema}.orders_next (LIKE {schema}.orders)")
            conn.execute(f"INSERT INTO {schema}.orders_next SELECT * FROM {schema}.orders")
            conn.execute(f"UPDATE {schema}.orders_next SET col_class = 5 WHERE handle = 'PO:2'")
            self.assertTrue(pgdoor.classes_changed(conn, schema, "orders", f"{schema}.orders_next"))
            conn.execute(f"DROP TABLE {schema}.orders")
            conn.execute(f"ALTER TABLE {schema}.orders_next RENAME TO orders")
            pgdoor.recount(conn, schema, "orders")
            pgdoor.bump_revoked(conn, schema)
            self.assertEqual(conn.execute(
                f"SELECT count(*) FROM {schema}.col_classes").fetchone()[0], base_cols)
            after_col_mark = self._staff_mark(conn, schema, hidden_docs=("Attachment:secret",))
            self.assertNotEqual(after_col_mark, after_exclude)

            # (4) Row-level audience tightening reusing EXISTING access_class 3 MUST move it
            conn.execute(f"CREATE TABLE {schema}.orders_next (LIKE {schema}.orders)")
            conn.execute(f"INSERT INTO {schema}.orders_next SELECT * FROM {schema}.orders")
            conn.execute(f"UPDATE {schema}.orders_next SET access_class = 3 WHERE handle = 'PO:2'")
            self.assertTrue(pgdoor.classes_changed(conn, schema, "orders", f"{schema}.orders_next"))
            conn.execute(f"DROP TABLE {schema}.orders")
            conn.execute(f"ALTER TABLE {schema}.orders_next RENAME TO orders")
            pgdoor.recount(conn, schema, "orders")
            pgdoor.bump_revoked(conn, schema)
            self.assertEqual(conn.execute(
                f"SELECT count(*) FROM {schema}.access_classes").fetchone()[0], base_classes)
            after_aud = self._staff_mark(conn, schema, hidden_docs=("Attachment:secret",))
            self.assertNotEqual(after_aud, after_col_mark)

            # (5) plx.d_hidden lineage update reusing existing dclass & same documents_digest MUST move it
            conn.execute(
                f"UPDATE {schema}.lineage SET read_ids = ARRAY['Attachment:secret'] "
                "WHERE view = 'orders' AND handle = 'PO:3'")
            after_dhidden = self._staff_mark(conn, schema, hidden_docs=("Attachment:secret",))
            self.assertNotEqual(after_dhidden, after_aud)


if __name__ == "__main__":
    unittest.main()
