"""The HTTP API served from a real Postgres, as a non-owner: ``GET /api/suggest`` reads
``search.entities`` through the reader's own scope (audience grants, markings, the people
exclusion; operating.pg.permissions), and a reader never gets another audience's record, a
marked record without the marking, a record of an excluded person, or anything while the
exclusion list cannot be read, whether named in the label, the sublabel, the id or the link.
Skips without a reachable Postgres (operating.tests.pg_testdb)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from operating.tests import pg_testdb

try:
    import psycopg
    from fastapi.testclient import TestClient
    from integrations.people_exclusion import PeopleExclusion  # noqa: F401
    from ontology.types import Actor
    from operating.ontology_tools import ReadPolicy
    from operating.pg import migrate
    from operator_api import backend as be
    from operator_api.app import create_app
    from operator_api.auth import DevAuth, fixture_identity
except ImportError:  # pragma: no cover
    psycopg = None

import contextlib
import dataclasses
import time
from concurrent.futures import Future
from types import SimpleNamespace

OID_GONE = "33333333-3333-4333-8333-333333333333"   # an excluded person
OID_CEO = "22222222-2222-4222-8222-222222222222"
OID_BUYER = "11111111-1111-4111-8111-111111111111"

BUYER, FINANCE = "staff:buyer", "staff:finance"
POLICY = ReadPolicy(
    tenant="plx",
    audiences={"api:public.app_*": frozenset({BUYER, FINANCE}),
               "api:finance.*": frozenset({FINANCE}),
               "mail:*": frozenset({BUYER, FINANCE}),
               f"mail:{OID_CEO}": frozenset(),                     # an exact deny inside mail:*
               f"mail:{OID_BUYER}": frozenset({BUYER})},
    markings={"purchasing_prices": frozenset({FINANCE})})

# (entity id, name, audience key, markings)
ENTITIES = [
    ("e1", "Zed Shared Supplies", "api:public.app_supplier", []),
    ("e2", "Zed Finance Holdings", "api:finance.ledger", []),
    ("e3", "Zed Priced Item", "api:public.app_item", ["purchasing_prices"]),
    ("e4", "Zed Ceo Private Contact", f"mail:{OID_CEO}", []),
    ("e5", "Zed Excluded Person Contact", f"mail:{OID_GONE}", []),
    ("e6", "Zed Buyer Mailbox Contact", f"mail:{OID_BUYER}", []),
]
EVERYONE = {n for _i, n, _a, _m in ENTITIES}
OBJECT = {n: f"Supplier:bc.vendors:{{\"id\":\"{eid}\"}}" for eid, n, _a, _m in ENTITIES}


def ITEM_OPENS(principals, ids):
    """The buyer's own read opens the buyer mailbox's record; nobody else's does."""
    return {i for i in ids if BUYER in principals and i == OBJECT["Zed Buyer Mailbox Contact"]}
FORBIDDEN_TO_ALL = {"Zed Excluded Person Contact"}


class Backend(be.WorkspaceBackend):
    """The real backend over the scratch Postgres; only the reader's policy is decided here
    (the access layer is not under test): by the principal the session carries."""

    def _reader_policy(self, reader):
        principals = set(reader.session.principals)
        held = frozenset(p for p in principals if p.startswith("staff:"))
        return Actor("plx", held), POLICY


@unittest.skipIf(psycopg is None, "psycopg or fastapi not installed")
class SuggestOverPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db, cls.dsn = pg_testdb.create_db()
        with psycopg.connect(cls.dsn) as conn:
            migrate.migrate(conn, values={"embed_model": "m", "embed_dim": 8})
            for eid, name, audience, markings in ENTITIES:
                conn.execute(
                    "INSERT INTO search.entities (entity_id, object_id, type, name, normalized,"
                    " audience_key, markings, alias) VALUES (%s, %s, 'supplier', %s, %s, %s,"
                    " %s, '{}')",
                    (eid, f"Supplier:bc.vendors:{{\"id\":\"{eid}\"}}", name, name.lower(),
                     audience, markings))
            conn.commit()
        cls.backend = Backend(lambda s: None, data_label="t", pg_dsn=cls.dsn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.backend.close() if hasattr(cls.backend, "close") else None
        pg_testdb.drop_db(cls.db)

    def setUp(self) -> None:
        patch = mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": OID_GONE})
        patch.start()
        self.addCleanup(patch.stop)
        self.item_check(ITEM_OPENS)

    def item_check(self, opens) -> None:
        """The reader's item check (IN-13: a mail row needs its record opened, not only
        the mailbox unit), answered at once on the read the suggest submits:
        ``opens(principals, ids)`` the records this reader's read opens."""
        def submit(reader, job, priority=0, deadline=None):
            principals = frozenset(reader.session.principals)
            view = SimpleNamespace(_request=contextlib.nullcontext, access=SimpleNamespace(
                visible_objects=lambda ids: frozenset(opens(principals, ids))))
            done: Future = Future()
            try:
                done.set_result(job(view))
            except Exception as exc:  # noqa: BLE001 - the check's own failure
                done.set_exception(exc)
            return done
        patch = mock.patch.object(self.backend.reads, "submit", side_effect=submit)
        patch.start()
        self.addCleanup(patch.stop)

    def client(self, *tags: str, login: bool = True) -> TestClient:
        session = dataclasses.replace(fixture_identity("plx", list(tags) or ["staff:none"])(),
                                      principals=frozenset(tags or ("staff:none",)))
        client = TestClient(create_app(self.backend, DevAuth(lambda: session),
                                       secure_cookie=False), base_url="http://testserver")
        if login:
            self.assertEqual(client.post("/api/auth/dev-login", json={}).status_code, 200)
        return client

    def names(self, client: TestClient, q: str = "zed") -> tuple[set[str], str]:
        found = client.get("/api/suggest", params={"q": q, "limit": 20})
        self.assertEqual(found.status_code, 200, found.text)
        self.assertEqual(found.json()["source"], "search_entities")   # served from Postgres
        return {s["label"] for s in found.json()["suggestions"]}, found.text

    def assert_never(self, text: str, hidden: set[str]) -> None:
        for name in hidden:
            self.assertNotIn(name, text)
        for gone in (OID_GONE,):
            self.assertNotIn(gone, text)

    def test_each_reader_gets_exactly_their_audiences(self) -> None:
        buyer, text = self.names(self.client(BUYER))
        self.assertEqual(buyer, {"Zed Shared Supplies", "Zed Buyer Mailbox Contact"})
        self.assert_never(text, EVERYONE - buyer)
        finance, text = self.names(self.client(FINANCE))
        # finance holds the marking and the finance audience, not the buyer's mailbox
        self.assertEqual(finance, {"Zed Shared Supplies", "Zed Finance Holdings",
                                   "Zed Priced Item"})
        self.assert_never(text, EVERYONE - finance)
        both, text = self.names(self.client(BUYER, FINANCE))
        self.assertEqual(both, EVERYONE - FORBIDDEN_TO_ALL - {"Zed Ceo Private Contact"})
        self.assert_never(text, {"Zed Ceo Private Contact"} | FORBIDDEN_TO_ALL)

    def test_a_claim_in_the_request_never_widens_the_scope(self) -> None:
        client = self.client(BUYER)
        for kw in ({"params": {"as": "owner", "owner": "1"}},
                   {"headers": {"X-Owner": "1", "X-Forwarded-User": "owner"}}):
            found = client.get("/api/suggest", params={"q": "zed", "limit": 20,
                                                       **kw.get("params", {})},
                               headers=kw.get("headers"))
            self.assertEqual(found.status_code, 200, found.text)
            self.assertEqual({s["label"] for s in found.json()["suggestions"]},
                             {"Zed Shared Supplies", "Zed Buyer Mailbox Contact"})
        self.assertEqual(self.client(BUYER, login=False).get(
            "/api/suggest", params={"q": "zed"}).status_code, 401)

    def test_a_mail_row_needs_the_item_check_beyond_the_mailbox_grant(self) -> None:
        """IN-13: the buyer holds the mailbox unit, but a check that refuses the record
        hides its name; an item check that never answers in time hides it too, and the
        keystroke waits about ITEM_CHECK_SECONDS, never a full read timeout."""
        self.item_check(lambda principals, ids: set())
        names, text = self.names(self.client(BUYER))
        self.assertEqual(names, {"Zed Shared Supplies"})
        self.assert_never(text, EVERYONE - names)
        never = mock.patch.object(self.backend.reads, "submit",
                                  side_effect=lambda *a, **k: Future())   # no worker answers
        never.start()
        self.addCleanup(never.stop)
        client = self.client(BUYER)
        started = time.monotonic()
        names, text = self.names(client)
        took = time.monotonic() - started
        self.assertEqual(names, {"Zed Shared Supplies"})
        self.assert_never(text, EVERYONE - names)
        self.assertLess(took, be.ITEM_CHECK_SECONDS + 5, took)

    def test_a_reader_with_no_grant_gets_nothing(self) -> None:
        names, text = self.names(self.client("staff:new-hire"))
        self.assertEqual(names, set())
        self.assert_never(text, EVERYONE)

    def test_an_excluded_person_is_never_returned_whatever_the_grant_or_the_word(self) -> None:
        for tags in ((BUYER,), (FINANCE,), (BUYER, FINANCE)):
            for q in ("zed", "zed excluded", "excluded person contact", "Zed Excluded Person Contact"):
                names, text = self.names(self.client(*tags), q)
                self.assertNotIn("Zed Excluded Person Contact", names, (tags, q))
                self.assertNotIn(OID_GONE, text)

    def test_an_unreadable_exclusion_list_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": "not-an-oid"}):
            names, text = self.names(self.client(BUYER, FINANCE))
        # personal sources (mail) are dropped for everyone; shared company records remain
        self.assertFalse({n for n in names if "Contact" in n}, names)
        self.assert_never(text, FORBIDDEN_TO_ALL | {"Zed Ceo Private Contact"})
        self.assertIn("Zed Shared Supplies", names)

    def test_one_ten_sixty_records_lose_none_and_leak_none(self) -> None:
        for total in (1, 10, 60):
            self._drop_bulk()
            with psycopg.connect(self.dsn) as conn:
                for n in range(total):
                    conn.execute(
                        "INSERT INTO search.entities (entity_id, object_id, type, name,"
                        " normalized, audience_key, markings, alias) VALUES (%s, %s,"
                        " 'supplier', %s, %s, %s, '{}', '{}')",
                        (f"bulk{n}", f"Supplier:bc.vendors:{{\"id\":\"bulk{n}\"}}",
                         f"Bulk {n:02d} Supplier", f"bulk {n:02d} supplier",
                         "api:public.app_supplier" if n % 4 == 0 else "api:finance.ledger"))
                conn.commit()
            # (a quarter are the buyer's: at most 15, under the suggest limit)
            visible = {f"Bulk {n:02d} Supplier" for n in range(total) if n % 4 == 0}
            names, text = self.names(self.client(BUYER), "bulk")
            self.assertEqual(names, visible, total)       # none lost, none leaked
            self.assertFalse(any(f"Bulk {n:02d}" in text for n in range(total) if n % 4), total)
        self._drop_bulk()

    def _drop_bulk(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute("DELETE FROM search.entities WHERE entity_id LIKE 'bulk%'")
            conn.commit()


if __name__ == "__main__":
    unittest.main()
