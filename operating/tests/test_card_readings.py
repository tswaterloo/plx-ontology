"""Record card stored thread state from ``assist.readings`` (ASK-GENIE 19 S9 / A5).

Checked at the boundary over real Postgres (``0062_assist_readings``, ``0063_assist_work``,
``0085_card_readings_feed``, real roles ``plx_api`` / ``plx_sql_reader`` / ``plx_eval``,
and the guarded comms views of ``test_biz_comms``):

* Migration ``0085_card_readings_feed.sql`` applies cleanly over ``0062``+``0063``; every
  column queried by ``ontology_tools_card`` matches both ``assist.card_readings`` and
  ``assist.readings``; ``plx_api`` reads ``assist.card_readings`` and is refused
  ``assist.readings``; ``plx_sql_reader`` is refused both.
* Card enriches visible mail and Teams messages with structured thread state ("did they
  reply, who owes whom", ``waiting_on``, ``promise``, ``due``, ``summary``, ``as_of``),
  deduplicating mailbox copies of the same message, and skips ``screened``/``failed`` rows.
* Permission isolation: readings for messages hidden from the reader NEVER appear in
  ``card.newest`` or ``card.thread_state``; excluded-person references and sensitive
  HR/payroll/financial text are blocked fail-closed; card numbers/secrets are masked;
  an unavailable people-exclusion guard fails closed for non-owner readers.
* Graceful degradation: missing ``assist.readings``, empty ``assist.readings``, and
  ``InsufficientPrivilege`` never break the card and surface typed status cleanly.
* Batched lookup: N = 1, 5, 10 visible messages execute the same single query count.

Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from integrations.people_exclusion import PeopleExclusion
from ontology.json_contract import validate
from operating import assist_readings as ar
from operating import ontology_tools_card as card_mod
from operating import ontology_tools_sql as ts
from operating.pg import business_views as bv
from operating.pg.migrate import MIGRATIONS_DIR
from operating.tests import test_biz_comms as tbc
from operating.tests import test_table_mail_parity as tmp
from operator_api import ask_legs
from tests import test_ontology_tools_evidence as ev

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


setUpModule = tbc.setUpModule
tearDownModule = tbc.tearDownModule

EXCLUDED_OID = "22222222-2222-2222-2222-222222222222"
OTHER_OID = "33333333-3333-3333-3333-333333333333"
STAFF_OID = "44444444-4444-4444-4444-444444444444"


def _bound_guard(oids=(EXCLUDED_OID,), addresses=("secret.partner@plx.example",)) -> PeopleExclusion:
    guard = PeopleExclusion(oids)
    guard._bound = True
    guard._addresses = frozenset(a.lower() for a in addresses)
    return guard


def _fake_card_host(messages_rows: list[dict], *, conn: Any = None,
                    guard: PeopleExclusion | None = None, owner: bool = False,
                    held_marks: frozenset[str] = frozenset(),
                    views: list[str] | None = None):
    """Lightweight tool/scope/actor harness exercising ``ontology_tools_card.card`` with a
    real Postgres ``conn`` while controlling the visible rows returned by ``messages_sql``."""
    from operating.ontology_tools_sql_rows import RESULTS
    chain_rid = "0123456789abcdef0123"
    RESULTS.put(chain_rid, {
        "actor": "test",
        "sql": "chain PO-100",
        "columns": ["hop", "direction", "link_type", "tier", "via", "type", "handle",
                    "label", "record_date", "system", "facts", "known"],
        "rows": [[0, "start", None, None, None, "PurchaseOrder", "rec://po/100",
                  "PO-100", "2026-11-18", "portal", {}, True]],
        "views": ["records", "record_links"],
        "profile": {"rows": 1, "shown": 1, "exact": True, "columns": []},
    })
    chain_out = {
        "result": f"sql://{chain_rid}",
        "scope": "chain from 'PO-100' (hops=1): reached PurchaseOrder (1); 1 of 1 rows shown",
        "columns": ["hop", "direction", "link_type", "tier", "via", "type", "handle",
                    "label", "record_date", "system", "facts", "known"],
        "rows": [{"cite": f"sql://{chain_rid}#row=0",
                  "cells": ["0", "start", None, None, None, "PurchaseOrder", "rec://po/100",
                            "PO-100", "2026-11-18", "portal", {"untrusted_text": "{}"}, True]}],
        "row_count": 1,
        "profile": {"rows": 1, "shown": 1, "exact": True, "columns": []},
        "truncated": False,
        "views": ["records", "record_links"],
        "as_of": "2026-11-20T12:00:00+00:00",
        "chain": {"start": "PO-100", "hops": 1, "reached": {"PurchaseOrder": 1},
                  "ambiguous": False, "guesses": 0, "per_hop": []},
    }
    msg_cols = ["source", "handle", "at", "title", "who"]
    msg_result = {
        "result": "sql://abcdef0123456789abcd",
        "columns": msg_cols,
        "rows": messages_rows,
        "profile": {"rows": len(messages_rows), "shown": len(messages_rows), "exact": True},
        "views": list(views if views is not None else ["emails", "teams_messages"]),
    }

    class FakeTool:
        def __init__(self) -> None:
            gate = SimpleNamespace(owner_reader=lambda _a: owner)
            runtime = SimpleNamespace(people_exclusion=guard)
            self.base = SimpleNamespace(item_gate=gate, runtime=runtime)

        def _call(self, args, _actor):
            self.last_sql = args.get("sql")
            return msg_result

    scope = SimpleNamespace(held=held_marks)
    actor = SimpleNamespace(tenant="plx", principals=frozenset({"owner" if owner else "staff"}))
    return FakeTool(), chain_out, scope, actor


def _msg_row(idx: int, source: str, handle: str, at: str, title: str, who: str) -> dict:
    return {
        "cite": f"sql://abcdef0123456789abcd#row={idx}",
        "cells": [source, handle, at, {"untrusted_text": title}, who],
    }


@unittest.skipIf(psycopg is None, "psycopg not installed")
class CardReadingsPostgresTest(unittest.TestCase):
    """Real Postgres tests: migration 0085, role grants, schema parity, guarded door
    isolation, thread-state derivation, exclusion & sensitive scrubbing, and degradation."""

    @classmethod
    def setUpClass(cls):
        tbc.EmailsThroughTheDoor.setUpClass.__func__(cls)
        with psycopg.connect(cls.dsn, autocommit=True) as pg:
            # Apply 0062, 0063, and 0085 on the real test database
            for name in ("0062_assist_readings.sql", "0063_assist_work.sql",
                         "0085_card_readings_feed.sql"):
                pg.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
            # Ensure roles exist for grant verification
            for role in ("plx_api", "plx_sql_reader", "plx_eval"):
                if not pg.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                                  (role,)).fetchone():
                    try:
                        pg.execute(f"CREATE ROLE {role} NOLOGIN")
                    except psycopg.errors.DuplicateObject:
                        pass
            # Re-apply 0062/0063 revokes and 0085 grants now that all roles exist
            pg.execute("REVOKE ALL ON ALL TABLES IN SCHEMA assist FROM PUBLIC, plx_api, plx_sql_reader")
            pg.execute((MIGRATIONS_DIR / "0085_card_readings_feed.sql").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        tbc.EmailsThroughTheDoor.tearDownClass.__func__(cls)

    tool = tbc.EmailsThroughTheDoor.tool
    sql = tbc.EmailsThroughTheDoor.sql

    def pg(self):
        conn = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(conn.close)
        return conn

    def _clear_readings(self):
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute("TRUNCATE assist.readings")

    def _insert_reading(self, **kw):
        defaults = {
            "source": "emails",
            "message_key": "msg-1",
            "handles": ["graph-mail://box/m1"],
            "thread_key": "thread-1",
            "said_at": dt.datetime(2026, 11, 19, 14, 0, tzinfo=dt.timezone.utc),
            "read_ver": 2,
            "model": "haiku",
            "text_basis": "whole",
            "needs_act": True,
            "actor": "vendor@acme.example",
            "actor_person": None,
            "actor_basis": "",
            "sender_person": STAFF_OID,
            "act": "confirm ship date",
            "act_class": "reply",
            "due": dt.date(2026, 11, 22),
            "counterpart": "Acme Packaging",
            "promise": "",
            "waiting_on": "ship date from Acme",
            "summary": "We asked Acme to confirm the ship date for PO-100.",
            "from_side": "us",
            "status": "ok",
            "reason": "",
            "read_at": dt.datetime(2026, 11, 19, 15, 0, tzinfo=dt.timezone.utc),
        }
        defaults.update(kw)
        cols = list(defaults.keys())
        placeholders = ", ".join(f"%({c})s" for c in cols)
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            pg.execute(
                f"INSERT INTO assist.readings ({', '.join(cols)}) VALUES ({placeholders})",
                defaults,
            )

    def test_schema_parity_and_role_grants_for_0085(self):
        """Migration 0085 exposes exact columns of assist.readings to plx_api/plx_eval,
        keeps assist.readings closed to plx_api, and keeps both closed to plx_sql_reader."""
        pg = self.pg()
        view_cols = [r[0] for r in pg.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'assist' AND table_name = 'card_readings' "
            "ORDER BY ordinal_position").fetchall()]
        table_cols = {r[0] for r in pg.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'assist' AND table_name = 'readings'").fetchall()}
        self.assertEqual(tuple(view_cols), card_mod._READINGS_COLS)
        self.assertTrue(set(card_mod._READINGS_COLS) <= table_cols)

        # plx_api can read assist.card_readings, NOT assist.readings
        with psycopg.connect(self.dsn) as c:
            c.execute("SET LOCAL ROLE plx_api")
            c.execute("SELECT count(*) FROM assist.card_readings").fetchone()
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                c.execute("SELECT count(*) FROM assist.readings")

        # plx_sql_reader cannot read either assist.card_readings or assist.readings
        for rel in ("assist.card_readings", "assist.readings"):
            with psycopg.connect(self.dsn) as c:
                c.execute("SET LOCAL ROLE plx_sql_reader")
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    c.execute(f"SELECT count(*) FROM {rel}")

        # plx_eval can read assist.card_readings
        with psycopg.connect(self.dsn) as c:
            c.execute("SET LOCAL ROLE plx_eval")
            c.execute("SELECT count(*) FROM assist.card_readings").fetchone()

    def test_card_enriches_mail_and_teams_with_who_owes_whom_and_reply_state(self):
        """Enriches visible mail & Teams messages with structured thread state, handles
        replied vs unreplied threads, deduplicates mailbox copies, ignores screened/failed,
        validates against the plx.sql output contract, and reports views cleanly."""
        self._clear_readings()
        # Thread 1 (mail): outbound inquiry on Nov 19, replied by vendor on Nov 20 (asking us for artwork)
        self._insert_reading(
            source="emails", message_key="m-ask",
            handles=["graph-mail://box1/m-ask", "graph-mail://box2/m-ask"],
            thread_key="t-po100",
            said_at=dt.datetime(2026, 11, 19, 10, 0, tzinfo=dt.timezone.utc),
            from_side="us", sender_person=STAFF_OID, needs_act=True,
            actor="sales@acme.example", actor_person=None, act="confirm ship date",
            act_class="reply", waiting_on="ship date from Acme",
            summary="Asked Acme for PO-100 ship date.",
            read_at=dt.datetime(2026, 11, 19, 11, 0, tzinfo=dt.timezone.utc),
        )
        self._insert_reading(
            source="emails", message_key="m-reply",
            handles=["graph-mail://box1/m-reply"],
            thread_key="t-po100",
            said_at=dt.datetime(2026, 11, 20, 9, 0, tzinfo=dt.timezone.utc),
            from_side="outside", sender_person=None, needs_act=True,
            actor="jane@plx.example", actor_person=STAFF_OID,
            act="send revised artwork", act_class="send_document",
            due=dt.date(2026, 11, 24), waiting_on="",
            summary="Acme replied asking Jane for revised artwork by Nov 24.",
            read_at=dt.datetime(2026, 11, 20, 9, 30, tzinfo=dt.timezone.utc),
        )
        # Thread 2 (mail): settled after vendor replied
        self._insert_reading(
            source="emails", message_key="m-s1", handles=["graph-mail://box1/m-s1"],
            thread_key="t-settled",
            said_at=dt.datetime(2026, 11, 17, 10, 0, tzinfo=dt.timezone.utc),
            from_side="us", sender_person=STAFF_OID, needs_act=True,
            actor="ops@carrier.example", actor_person=None, act="confirm pickup",
            act_class="reply", summary="Asked carrier to confirm pickup.",
        )
        self._insert_reading(
            source="emails", message_key="m-s2", handles=["graph-mail://box1/m-s2"],
            thread_key="t-settled",
            said_at=dt.datetime(2026, 11, 18, 10, 0, tzinfo=dt.timezone.utc),
            from_side="outside", sender_person=None, needs_act=False,
            actor="", actor_person=None, act="", act_class=None, waiting_on="",
            summary="Carrier confirmed pickup complete.",
        )
        # Thread 3 (Teams): internal request waiting on Bob
        self._insert_reading(
            source="teams_messages", message_key="teams-channel://c/1",
            handles=["teams-channel://c/1"], thread_key="chat-1",
            said_at=dt.datetime(2026, 11, 20, 11, 0, tzinfo=dt.timezone.utc),
            from_side="us", sender_person=OTHER_OID, needs_act=True,
            actor="Bob", actor_person=STAFF_OID, act="confirm lot release",
            act_class="reply", waiting_on="",
            summary="Asked Bob to confirm lot release for PO-100.",
            read_at=dt.datetime(2026, 11, 20, 11, 5, tzinfo=dt.timezone.utc),
        )
        # Screened & failed rows must NEVER be returned
        self._insert_reading(
            source="emails", message_key="m-screened", handles=["graph-mail://box1/m-screened"],
            thread_key="t-scr", status="screened", reason="bulk",
            summary="Should not appear", needs_act=None, act_class=None,
        )
        self._insert_reading(
            source="emails", message_key="m-failed", handles=["graph-mail://box1/m-failed"],
            thread_key="t-fail", status="failed", reason="timeout",
            summary="Should not appear", needs_act=None, act_class=None,
        )
        # Legacy read_ver=0 row (needs_act IS NULL) must degrade cleanly
        self._insert_reading(
            source="emails", message_key="m-v0", handles=["graph-mail://box1/m-v0"],
            thread_key="t-v0", read_ver=0, needs_act=None, act_class=None,
            actor="", act="", waiting_on="", summary="",
        )
        # Hidden message NOT in visible rows must NEVER leak
        self._insert_reading(
            source="emails", message_key="m-hidden", handles=["graph-mail://private/m-hidden"],
            thread_key="t-po100",
            said_at=dt.datetime(2026, 11, 21, 9, 0, tzinfo=dt.timezone.utc),
            from_side="outside", needs_act=False,
            summary="PRIVATE HIDDEN REPLY THAT MUST NEVER LEAK",
        )

        # Two mailbox copies of m-ask both visible: must NOT count as a reply to itself
        visible = [
            _msg_row(0, "teams", "teams-channel://c/1", "2026-11-20T11:00:00+00:00",
                     "PO-100 lot", "Alice"),
            _msg_row(1, "mail", "graph-mail://box1/m-reply", "2026-11-20T09:00:00+00:00",
                     "Re: PO-100", "sales@acme.example"),
            _msg_row(2, "mail", "graph-mail://box1/m-ask", "2026-11-19T10:00:00+00:00",
                     "PO-100", "jane@plx.example"),
            _msg_row(3, "mail", "graph-mail://box2/m-ask", "2026-11-19T10:00:00+00:00",
                     "PO-100", "jane@plx.example"),
            _msg_row(4, "mail", "graph-mail://box1/m-s2", "2026-11-18T10:00:00+00:00",
                     "Re: Pickup", "ops@carrier.example"),
            _msg_row(5, "mail", "graph-mail://box1/m-s1", "2026-11-17T10:00:00+00:00",
                     "Pickup", "jane@plx.example"),
            _msg_row(6, "mail", "graph-mail://box1/m-screened", "2026-11-16T10:00:00+00:00",
                     "Promo", "promo@x.example"),
            _msg_row(7, "mail", "graph-mail://box1/m-failed", "2026-11-15T10:00:00+00:00",
                     "Broken", "x@y.example"),
            _msg_row(8, "mail", "graph-mail://box1/m-v0", "2026-11-14T10:00:00+00:00",
                     "Old", "old@y.example"),
        ]
        conn = SimpleNamespace(dsn=self.dsn, postgres=True)
        tool, chain_out, scope, actor = _fake_card_host(visible, conn=conn, guard=_bound_guard())
        with mock.patch.object(card_mod.chain, "walk", return_value=chain_out):
            out = card_mod.card(tool, conn, scope, actor, {"record": "PO-100"})

        # Passes plx.sql output contract validation
        self.assertEqual(validate(out, ts._CONTRACT["output"], root=ev.CONTRACTS), [],
                         json.dumps(out)[:1500])
        # Views & source transparency (no synthetic assist_readings in views so A4 AnswerCache
        # treats out["views"] strictly as biz.view_state view names)
        self.assertNotIn("assist_readings", out["views"])
        self.assertIn("emails", out["views"])
        self.assertIn("teams_messages", out["views"])
        self.assertEqual(out["card"]["readings_source"], "assist.readings")
        self.assertEqual(out["card"]["thread_state"]["source"], "assist.readings")
        self.assertNotIn("PRIVATE HIDDEN REPLY", json.dumps(out))

        # Screened & failed rows have NO reading attached
        newest = out["card"]["newest"]
        self.assertNotIn("reading", newest[6])
        self.assertNotIn("reading", newest[7])
        # Legacy v0 row attached cleanly without guessing who_owes_whom
        self.assertIn("reading", newest[8])
        self.assertNotIn("who_owes_whom", newest[8]["reading"])

        # Per-message readings
        self.assertEqual(newest[0]["reading"]["who_owes_whom"], "internal")
        self.assertTrue(newest[0]["reading"]["needs_reply"])
        self.assertFalse(newest[0]["reading"]["replied"])
        self.assertEqual(newest[1]["reading"]["who_owes_whom"], "we_owe_them")
        self.assertFalse(newest[1]["reading"]["replied"])
        # m-ask was replied to by m-reply (NOT hidden m-hidden)
        self.assertTrue(newest[2]["reading"]["replied"])
        self.assertEqual(newest[2]["reading"]["replied_at"], "2026-11-20T09:00:00+00:00")

        # Thread summaries
        ts_block = out["card"]["thread_state"]
        self.assertEqual(ts_block["source"], "assist.readings")
        self.assertEqual(ts_block["readings_found"], 7)   # 9 visible minus screened & failed
        by_thread = {t["thread_key"]: t for t in ts_block["threads"]}
        # t-po100 deduplicated the two copies of m-ask -> visible_messages == 2 (not 3!)
        self.assertEqual(by_thread["t-po100"]["visible_messages"], 2)
        self.assertEqual(by_thread["t-po100"]["who_owes_whom"], "we_owe_them")
        self.assertEqual(by_thread["t-po100"]["due"], "2026-11-24")
        self.assertFalse(by_thread["t-po100"]["replied"])
        # t-settled shows replied=True, who_owes_whom=settled
        self.assertEqual(by_thread["t-settled"]["who_owes_whom"], "settled")
        self.assertTrue(by_thread["t-settled"]["replied"])
        self.assertEqual(by_thread["t-settled"]["visible_messages"], 2)
        # chat-1 shows Teams internal thread
        self.assertEqual(by_thread["chat-1"]["source"], "teams")
        self.assertEqual(by_thread["chat-1"]["who_owes_whom"], "internal")
        self.assertTrue(by_thread["chat-1"]["needs_reply"])

        # ask_legs.ledger ticks emails and teams_messages from out["views"] (never assist_readings)
        book = ask_legs.ledger([{"type": "tool_call", "name": "plx.sql", "is_error": False,
                                 "arguments": {"record": "PO-100"}, "result": out}])
        self.assertNotIn("assist_readings", book.seen)
        self.assertIn("emails", book.seen)
        self.assertIn("teams_messages", book.seen)

    def test_duplicate_mailbox_copies_alone_are_never_counted_as_a_reply(self):
        self._clear_readings()
        self._insert_reading(
            source="emails", message_key="m-only",
            handles=["graph-mail://box1/m-only", "graph-mail://box2/m-only"],
            thread_key="t-dup",
            said_at=dt.datetime(2026, 11, 19, 10, 0, tzinfo=dt.timezone.utc),
            from_side="us", sender_person=STAFF_OID, needs_act=True,
            actor="vendor@acme.example", actor_person=None, act="confirm date",
            act_class="reply", waiting_on="date from Acme",
            summary="Waiting on Acme.",
        )
        visible = [
            _msg_row(0, "mail", "graph-mail://box1/m-only", "2026-11-19T10:00:00+00:00",
                     "PO", "jane@plx.example"),
            _msg_row(1, "mail", "graph-mail://box2/m-only", "2026-11-19T10:00:00+00:00",
                     "PO", "jane@plx.example"),
        ]
        conn = SimpleNamespace(dsn=self.dsn, postgres=True)
        tool, chain_out, scope, actor = _fake_card_host(visible, conn=conn, guard=_bound_guard())
        with mock.patch.object(card_mod.chain, "walk", return_value=chain_out):
            out = card_mod.card(tool, conn, scope, actor, {"record": "PO-100"})
        t = out["card"]["thread_state"]["threads"][0]
        self.assertEqual(t["visible_messages"], 1)
        self.assertEqual(t["who_owes_whom"], "they_owe_us")
        self.assertFalse(t["replied"])
        self.assertFalse(out["card"]["newest"][0]["reading"]["replied"])
        self.assertFalse(out["card"]["newest"][1]["reading"]["replied"])

    def test_permission_isolation_through_real_guarded_emails_door(self):
        """Using the real guarded biz_sql.emails views from test_biz_comms: a narrower
        reader sees ONLY readings for messages their own door returned, NEVER readings
        for another reader's messages."""
        self._clear_readings()
        with psycopg.connect(self.dsn, autocommit=True) as pg:
            owner_handles = {r[0] for r in [
                (tbc._cell(row["cells"][1]),)
                for row in self.sql(tmp.OWNER, "SELECT 'mail', handle FROM emails")["rows"]
            ]}
            narrow_handles = {r[0] for r in [
                (tbc._cell(row["cells"][1]),)
                for row in self.sql(tmp.NARROW, "SELECT 'mail', handle FROM emails")["rows"]
            ]}
            only_owner = sorted(owner_handles - narrow_handles)
            both_see = sorted(narrow_handles)
            self.assertTrue(only_owner and both_see)
            h_shared, h_owner_only = both_see[0], only_owner[0]
            before = pg.execute(
                f"SELECT handle, records FROM {bv.SCHEMA}.emails WHERE handle = ANY(%s)",
                ([h_shared, h_owner_only],)).fetchall()
            for h in (h_shared, h_owner_only):
                pg.execute(f"UPDATE {bv.SCHEMA}.emails SET records = ARRAY['rec://po/perm'] WHERE handle = %s", (h,))
        self.addCleanup(lambda: [psycopg.connect(self.dsn, autocommit=True).execute(
            f"UPDATE {bv.SCHEMA}.emails SET records = %s WHERE handle = %s", (r, h))
            for h, r in before])

        self._insert_reading(
            source="emails", message_key="k-shared", handles=[h_shared],
            thread_key="t-perm-shared", summary="SHARED READING VISIBLE TO BOTH",
            needs_act=True, actor="vendor@acme.example", act_class="reply", from_side="us",
        )
        self._insert_reading(
            source="emails", message_key="k-owner-only", handles=[h_owner_only],
            thread_key="t-perm-owner", summary="SECRET OWNER ONLY READING DO NOT LEAK",
            needs_act=True, actor="boss@plx.example", act_class="reply", from_side="outside",
        )

        # Run real messages_sql through the guarded door for OWNER vs NARROW
        narrow_msgs = self.sql(tmp.NARROW, card_mod.messages_sql(["rec://po/perm"], ("emails",)))
        owner_msgs = self.sql(tmp.OWNER, card_mod.messages_sql(["rec://po/perm"], ("emails",)))
        conn = SimpleNamespace(dsn=self.dsn, postgres=True)
        guard = _bound_guard(oids=PeopleExclusion.from_env(tmp.EXCLUDED_B)._ids, addresses=())

        # Narrow reader gets ONLY k-shared
        tool_n, chain_n, scope_n, actor_n = _fake_card_host(narrow_msgs["rows"], conn=conn, guard=guard)
        with mock.patch.object(card_mod.chain, "walk", return_value=dict(chain_n)):
            out_n = card_mod.card(tool_n, conn, scope_n, actor_n, {"record": "PO-100"})
        dumped_n = json.dumps(out_n)
        self.assertIn("SHARED READING VISIBLE TO BOTH", dumped_n)
        self.assertNotIn("SECRET OWNER ONLY READING DO NOT LEAK", dumped_n)
        self.assertNotIn(h_owner_only, dumped_n)

        # Owner gets both
        tool_o, chain_o, scope_o, actor_o = _fake_card_host(owner_msgs["rows"], conn=conn, guard=guard, owner=True)
        with mock.patch.object(card_mod.chain, "walk", return_value=dict(chain_o)):
            out_o = card_mod.card(tool_o, conn, scope_o, actor_o, {"record": "PO-100"})
        dumped_o = json.dumps(out_o)
        self.assertIn("SHARED READING VISIBLE TO BOTH", dumped_o)
        self.assertIn("SECRET OWNER ONLY READING DO NOT LEAK", dumped_o)

    def test_excluded_people_and_sensitive_markings_are_scrubbed_or_blocked_fail_closed(self):
        """Excluded OIDs/addresses in reading fields are withheld fail-closed for non-owners;
        payroll/HR/SIN/confidential-label text is withheld when reader lacks finance+pii;
        payment card numbers in visible reading text are masked; unavailable guard fails closed."""
        self._clear_readings()
        # 1. Reading referencing an excluded person's OID in actor_person
        self._insert_reading(
            source="emails", message_key="m-excl-oid", handles=["graph-mail://b/m-excl-oid"],
            thread_key="t-e1", actor_person=EXCLUDED_OID,
            summary="Excluded partner action must never leak.",
        )
        # 2. Reading naming an excluded bound email address in waiting_on text
        self._insert_reading(
            source="emails", message_key="m-excl-addr", handles=["graph-mail://b/m-excl-addr"],
            thread_key="t-e2", actor_person=None, sender_person=STAFF_OID,
            waiting_on="approval from secret.partner@plx.example",
            summary="Waiting on excluded partner address.",
        )
        # 3. Reading with payroll / HR sensitive text
        self._insert_reading(
            source="emails", message_key="m-payroll", handles=["graph-mail://b/m-payroll"],
            thread_key="t-p1", actor_person=STAFF_OID, sender_person=OTHER_OID,
            summary="Employee pay stub shows gross pay, CPP, EI and YTD total deductions.",
        )
        # 4. Reading with ConfidentialPayrollInformation label
        self._insert_reading(
            source="emails", message_key="m-label", handles=["graph-mail://b/m-label"],
            thread_key="t-l1", actor_person=STAFF_OID, sender_person=OTHER_OID,
            summary="Filed under ConfidentialPayrollInformation folder.",
        )
        # 5. Normal business reading containing a payment card number (must be masked!)
        self._insert_reading(
            source="emails", message_key="m-card", handles=["graph-mail://b/m-card"],
            thread_key="t-c1", actor_person=STAFF_OID, sender_person=OTHER_OID,
            summary="Customer paid invoice using 4111-1111-1111-1111.",
            waiting_on="",
        )
        visible = [
            _msg_row(0, "mail", "graph-mail://b/m-excl-oid", "2026-11-20T10:00:00+00:00", "S1", "a@b.com"),
            _msg_row(1, "mail", "graph-mail://b/m-excl-addr", "2026-11-19T10:00:00+00:00", "S2", "a@b.com"),
            _msg_row(2, "mail", "graph-mail://b/m-payroll", "2026-11-18T10:00:00+00:00", "Pay stub", "hr@b.com"),
            _msg_row(3, "mail", "graph-mail://b/m-label", "2026-11-17T10:00:00+00:00", "Doc", "hr@b.com"),
            _msg_row(4, "mail", "graph-mail://b/m-card", "2026-11-16T10:00:00+00:00", "Payment", "ar@b.com"),
        ]
        conn = SimpleNamespace(dsn=self.dsn, postgres=True)
        # Non-owner WITHOUT finance/pii markings
        tool, chain_out, scope, actor = _fake_card_host(
            visible, conn=conn, guard=_bound_guard(), owner=False, held_marks=frozenset())
        with mock.patch.object(card_mod.chain, "walk", return_value=dict(chain_out)):
            out = card_mod.card(tool, conn, scope, actor, {"record": "PO-100"})
        dumped = json.dumps(out)
        self.assertNotIn("Excluded partner", dumped)
        self.assertNotIn("secret.partner@plx.example", dumped)
        self.assertNotIn("gross pay", dumped)
        self.assertNotIn("ConfidentialPayrollInformation", dumped)
        self.assertNotIn("4111-1111-1111-1111", dumped)
        self.assertEqual(out["card"]["thread_state"]["withheld"], 4)
        # Only m-card remains, and its card number is masked!
        self.assertEqual(out["card"]["thread_state"]["readings_found"], 1)
        self.assertIn("1111", out["card"]["newest"][4]["reading"]["summary"])

        # Reader WITH finance+pii markings sees payroll reading (still excludes excluded people & masks card!)
        tool_f, chain_f, scope_f, actor_f = _fake_card_host(
            visible, conn=conn, guard=_bound_guard(), owner=False,
            held_marks=frozenset({"finance", "pii"}))
        with mock.patch.object(card_mod.chain, "walk", return_value=dict(chain_f)):
            out_f = card_mod.card(tool_f, conn, scope_f, actor_f, {"record": "PO-100"})
        dumped_f = json.dumps(out_f)
        self.assertNotIn("Excluded partner", dumped_f)
        self.assertNotIn("secret.partner@plx.example", dumped_f)
        self.assertIn("gross pay", dumped_f)
        self.assertNotIn("4111-1111-1111-1111", dumped_f)
        self.assertEqual(out_f["card"]["thread_state"]["withheld"], 2)

        # Unavailable people-exclusion guard fails closed for non-owner
        bad_guard = PeopleExclusion(unavailable="excluded_people_oids_absent")
        tool_u, chain_u, scope_u, actor_u = _fake_card_host(
            visible, conn=conn, guard=bad_guard, owner=False)
        with mock.patch.object(card_mod.chain, "walk", return_value=dict(chain_u)):
            out_u = card_mod.card(tool_u, conn, scope_u, actor_u, {"record": "PO-100"})
        self.assertIn("people_exclusion_unavailable", out_u["card"]["readings_unavailable"])
        self.assertNotIn("thread_state", out_u["card"])
        self.assertFalse(any("reading" in m for m in out_u["card"]["newest"]))

    def test_degrades_cleanly_when_empty_missing_or_unprivileged_and_batches_queries(self):
        """Empty table -> 0 readings found; missing table or InsufficientPrivilege ->
        readings_unavailable without failing the card; N = 1, 5, 10 visible messages ->
        constant single query count (no N+1)."""
        self._clear_readings()
        conn = SimpleNamespace(dsn=self.dsn, postgres=True)
        visible = [_msg_row(i, "mail", f"graph-mail://b/m{i}",
                            f"2026-11-{10 + i:02d}T10:00:00+00:00", f"Sub {i}", "a@b.com")
                   for i in range(10)]
        # 1. Empty assist.readings
        tool, chain_out, scope, actor = _fake_card_host(visible[:3], conn=conn, guard=_bound_guard())
        with mock.patch.object(card_mod.chain, "walk", return_value=dict(chain_out)):
            out = card_mod.card(tool, conn, scope, actor, {"record": "PO-100"})
        self.assertEqual(out["card"]["thread_state"]["readings_found"], 0)
        self.assertEqual(out["card"]["thread_state"]["threads"], [])
        self.assertEqual(out["card"]["readings_source"], "assist.readings")
        self.assertEqual(out["card"]["thread_state"]["source"], "assist.readings")
        self.assertNotIn("assist_readings", out["views"])

        # 2. Constant query count for N = 1, 5, 10 visible messages
        for i in range(10):
            self._insert_reading(
                source="emails", message_key=f"mk-{i}", handles=[f"graph-mail://b/m{i}"],
                thread_key=f"tk-{i}", summary=f"Summary {i}.",
            )
        counts = {}
        for n in (1, 5, 10):
            executed = []
            real_lent = ts._lent

            class CountingLent:
                def __init__(self, c):
                    self._inner = real_lent(c)

                def __enter__(self):
                    pg = self._inner.__enter__()
                    orig = pg.execute

                    def wrapped(sql, params=None):
                        executed.append(sql)
                        return orig(sql, params)
                    self._pg, self._orig = pg, orig
                    pg.execute = wrapped
                    return pg

                def __exit__(self, *a):
                    self._pg.execute = self._orig
                    return self._inner.__exit__(*a)

            tool_n, chain_n, scope_n, actor_n = _fake_card_host(
                visible[:n], conn=conn, guard=_bound_guard())
            with mock.patch.object(card_mod.chain, "walk", return_value=dict(chain_n)), \
                    mock.patch.object(ts, "_lent", CountingLent):
                out_n = card_mod.card(tool_n, conn, scope_n, actor_n, {"record": "PO-100"})
            self.assertEqual(out_n["card"]["thread_state"]["readings_found"], n)
            counts[n] = len(executed)
        self.assertEqual(len(set(counts.values())), 1, counts)

        # 3. Missing assist.card_readings AND assist.readings -> degrades cleanly
        with mock.patch.object(card_mod, "CARD_READINGS_VIEW", "assist.no_such_view"), \
                mock.patch.object(card_mod, "CARD_READINGS_TABLE", "assist.no_such_table"):
            tool_m, chain_m, scope_m, actor_m = _fake_card_host(
                visible[:2], conn=conn, guard=_bound_guard())
            with mock.patch.object(card_mod.chain, "walk", return_value=dict(chain_m)):
                out_m = card_mod.card(tool_m, conn, scope_m, actor_m, {"record": "PO-100"})
        self.assertIn("not_built", out_m["card"]["readings_unavailable"])
        self.assertNotIn("thread_state", out_m["card"])
        self.assertNotIn("assist_readings", out_m["views"])
        self.assertEqual(len(out_m["card"]["newest"]), 2)

        # 4. Fallback to assist.readings when assist.card_readings view is not installed yet
        with mock.patch.object(card_mod, "CARD_READINGS_VIEW", "assist.no_such_view"):
            tool_fb, chain_fb, scope_fb, actor_fb = _fake_card_host(
                visible[:2], conn=conn, guard=_bound_guard())
            with mock.patch.object(card_mod.chain, "walk", return_value=dict(chain_fb)):
                out_fb = card_mod.card(tool_fb, conn, scope_fb, actor_fb, {"record": "PO-100"})
        self.assertEqual(out_fb["card"]["thread_state"]["readings_found"], 2)

        # 5. InsufficientPrivilege degrades cleanly without crashing the card
        with mock.patch.object(ts, "_lent", side_effect=psycopg.errors.InsufficientPrivilege("denied")):
            tool_p, chain_p, scope_p, actor_p = _fake_card_host(
                visible[:2], conn=conn, guard=_bound_guard())
            with mock.patch.object(card_mod.chain, "walk", return_value=dict(chain_p)):
                out_p = card_mod.card(tool_p, conn, scope_p, actor_p, {"record": "PO-100"})
        self.assertIn("not_readable", out_p["card"]["readings_unavailable"])
        self.assertEqual(len(out_p["card"]["newest"]), 2)

    def test_card_picks_up_item_gate_runtime_address_exclusion_and_fails_closed_unbound(self):
        """End-to-end card behavior: (a) ``item_gate.runtime.address_exclusion()`` is used
        even when ``base.runtime`` is absent and ``people_exclusion`` is unbound; (b) an
        unbound guard with an undecidable email address fails closed (withholds reading)."""
        self._clear_readings()
        self._insert_reading(
            source="emails", message_key="m-excl-addr", handles=["graph-mail://b/m-excl-addr"],
            thread_key="t-e-addr", actor="secret.partner@plx.example",
            actor_person=None, sender_person=STAFF_OID,
            summary="Excluded partner email must be withheld via address_exclusion().",
        )
        self._insert_reading(
            source="emails", message_key="m-ok-addr", handles=["graph-mail://b/m-ok-addr"],
            thread_key="t-ok-addr", actor="vendor@acme.example",
            actor_person=None, sender_person=STAFF_OID,
            summary="Clear vendor reading.",
        )
        visible = [
            _msg_row(0, "mail", "graph-mail://b/m-excl-addr", "2026-11-20T10:00:00+00:00",
                     "S1", "a@b.com"),
            _msg_row(1, "mail", "graph-mail://b/m-ok-addr", "2026-11-19T10:00:00+00:00",
                     "S2", "vendor@acme.example"),
        ]
        conn = SimpleNamespace(dsn=self.dsn, postgres=True)

        # (a) Production-like wiring: base.runtime is None; item_gate.runtime holds unbound
        # people_exclusion + address_exclusion() returning the Graph-bound guard.
        unbound = PeopleExclusion([EXCLUDED_OID])
        bound = _bound_guard()
        tool_b, chain_b, scope_b, actor_b = _fake_card_host(visible, conn=conn, guard=None)
        runtime_b = SimpleNamespace(people_exclusion=unbound, address_exclusion=lambda: bound)
        tool_b.base = SimpleNamespace(
            item_gate=SimpleNamespace(owner_reader=lambda _a: False, runtime=runtime_b),
        )
        with mock.patch.object(card_mod.chain, "walk", return_value=dict(chain_b)):
            out_b = card_mod.card(tool_b, conn, scope_b, actor_b, {"record": "PO-100"})
        dumped_b = json.dumps(out_b)
        self.assertNotIn("Excluded partner email", dumped_b)
        self.assertIn("Clear vendor reading.", dumped_b)
        self.assertEqual(out_b["card"]["thread_state"]["withheld"], 1)
        self.assertEqual(out_b["card"]["thread_state"]["readings_found"], 1)

        # (b) Unbound guard without address_exclusion(): undecidable email fails CLOSED!
        tool_u, chain_u, scope_u, actor_u = _fake_card_host(visible, conn=conn, guard=None)
        runtime_u = SimpleNamespace(people_exclusion=unbound, address_exclusion=lambda: None)
        tool_u.base = SimpleNamespace(
            item_gate=SimpleNamespace(owner_reader=lambda _a: False, runtime=runtime_u),
        )
        with mock.patch.object(card_mod.chain, "walk", return_value=dict(chain_u)):
            out_u = card_mod.card(tool_u, conn, scope_u, actor_u, {"record": "PO-100"})
        dumped_u = json.dumps(out_u)
        self.assertNotIn("Excluded partner email", dumped_u)
        self.assertNotIn("Clear vendor reading.", dumped_u)
        self.assertEqual(out_u["card"]["thread_state"]["withheld"], 2)
        self.assertEqual(out_u["card"]["thread_state"]["readings_found"], 0)


class CardExclusionGuardUnitTest(unittest.TestCase):
    """Pure unit tests for ``_people_guard`` and ``_reading_excluded``."""

    def test_people_guard_accepts_reloading_exclusion_duck_typed_and_item_gate_runtime(self):
        import tempfile
        from integrations.people_exclusion import ENV_IDS, ReloadingExclusion

        # 1. ReloadingExclusion is resolved cleanly via .current() on item_gate.runtime
        with tempfile.TemporaryDirectory() as tmpdir:
            empty_cfg = Path(tmpdir) / "people_exclusion.json"
            empty_cfg.write_text(
                json.dumps({"schema": "plx-people-exclusion/v1", "object_ids": []}),
                encoding="utf-8",
            )
            reloading = ReloadingExclusion({ENV_IDS: EXCLUDED_OID}, config=empty_cfg)
            tool = SimpleNamespace(
                base=SimpleNamespace(
                    item_gate=SimpleNamespace(
                        runtime=SimpleNamespace(people_exclusion=reloading),
                    ),
                ),
            )
            picked = card_mod._people_guard(tool)
            self.assertIsInstance(picked, PeopleExclusion)
            self.assertIsNone(picked.unavailable)
            self.assertTrue(card_mod._reading_excluded(
                {"actor_person": EXCLUDED_OID, "summary": "ok"}, reloading))
            # Clear OID with no email address is allowed
            self.assertFalse(card_mod._reading_excluded(
                {"actor_person": STAFF_OID, "actor": "Bob", "summary": "Confirm lot"}, reloading))

        # 2. item_gate.runtime.address_exclusion() wins over unbound people_exclusion
        unbound = PeopleExclusion([EXCLUDED_OID])
        bound = _bound_guard()
        tool_addr = SimpleNamespace(
            base=SimpleNamespace(
                item_gate=SimpleNamespace(
                    runtime=SimpleNamespace(
                        people_exclusion=unbound,
                        address_exclusion=lambda: bound,
                    ),
                ),
            ),
        )
        picked_addr = card_mod._people_guard(tool_addr)
        self.assertIs(picked_addr, bound)
        self.assertTrue(card_mod._reading_excluded(
            {"waiting_on": "reply from secret.partner@plx.example"}, picked_addr))
        self.assertFalse(card_mod._reading_excluded(
            {"actor": "vendor@acme.example", "summary": "Follow up"}, picked_addr))

        # 3. Duck-typed exclusion guard (.is_excluded / .decides / .unavailable) is accepted
        class DuckGuard:
            unavailable = None

            def decides(self, ident: str) -> bool:
                return ident != "undecidable@mystery.example"

            def is_excluded(self, ident: str) -> bool:
                return ident == "blocked@plx.example"

        duck = DuckGuard()
        tool_duck = SimpleNamespace(
            base=SimpleNamespace(item_gate=SimpleNamespace(people_exclusion=duck)),
        )
        self.assertIs(card_mod._people_guard(tool_duck), duck)
        self.assertTrue(card_mod._reading_excluded({"actor": "blocked@plx.example"}, duck))
        self.assertFalse(card_mod._reading_excluded({"actor": "clear@acme.example"}, duck))
        # Undecidable email on duck guard also fails closed!
        self.assertTrue(card_mod._reading_excluded({"summary": "cc undecidable@mystery.example"}, duck))

    def test_reading_excluded_fails_closed_on_undecidable_email_address(self):
        """An unbound guard that cannot decide an email address MUST fail closed (True),
        never silently skipping the address."""
        unbound = PeopleExclusion([EXCLUDED_OID])
        self.assertFalse(unbound.decides("anyone@acme.example"))
        self.assertTrue(card_mod._reading_excluded(
            {"actor": "anyone@acme.example", "summary": "Status ok"}, unbound))
        self.assertTrue(card_mod._reading_excluded(
            {"actor": "Bob", "summary": "Ask someone@vendor.example for ship date"}, unbound))

        # With an attached directory that decides only known internal addresses:
        # known clear -> False; known excluded -> True; unknown email -> fail closed (True)!
        with_dir = PeopleExclusion([EXCLUDED_OID])
        with_dir.attach_directory([
            (STAFF_OID, "alice@plx.example", ["alice@plx.example"]),
            (EXCLUDED_OID, "secret@plx.example", ["secret@plx.example"]),
        ])
        self.assertFalse(card_mod._reading_excluded(
            {"actor": "alice@plx.example", "summary": "Alice will reply"}, with_dir))
        self.assertTrue(card_mod._reading_excluded(
            {"actor": "secret@plx.example", "summary": "Excluded user"}, with_dir))
        self.assertTrue(card_mod._reading_excluded(
            {"actor": "unknown@outside.example", "summary": "Undecided email"}, with_dir))


if __name__ == "__main__":
    unittest.main()
