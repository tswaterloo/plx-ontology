"""Tests for the fast Postgres-door meeting prep brief builder (``operating.meeting_brief_door``).

Covers:
1. Rich brief built from mail, Teams, open/late POs, open sales orders, open alerts/tasks,
   and prior-thread decisions/action items when ``meeting_transcripts`` has 0 rows.
2. Truthful transcript state (``state == "not_available"`` with exact message) when no
   transcript is captured, and ``state == "shown"`` when a transcript exists.
3. Guardrail 1: exact ``more`` counts (no silent truncation) and explicit scope / ``as_of``.
4. Permission & exclusion guarantees:
   - Excluded person's mail/Teams and another user's private mailbox NEVER appear in the brief
     (tested with real Postgres ``security_barrier`` guarded views when PG is available, and
     strict door-enforcing SQL fakes).
   - Unreadable meeting raises ``BriefRefused("not_found")``.
   - Non-finance reader gets ``finance_marked`` money section and zero leaked amounts.
5. Zero N+1: statement count is O(1) regardless of row count (N=1 vs N=30).

Evidence label: ``isolated-fixture``.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from operating import briefs as br
from operating import meeting_brief_door as mbd

NOW = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)
MEETING_OID = "CalendarEvent:meetings.calendar_events:ev-croda-1"
MEETING_H = "graph-cal://alice/ev-croda-1@1"
SUP_OID = "Supplier:bc.vendors:sup-croda"
SUP_H = "bc://vendors/croda@1"
CUS_OID = "Customer:bc.customers:cus-acme"
CUS_H = "bc://customers/acme@1"


def _sec(brief: dict, key: str) -> dict:
    return next(s for s in brief["sections"] if s["key"] == key)


class _FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakePg:
    """In-memory fake simulating ``biz_person.*`` (enforcing reader mailbox/chat scope +
    people exclusion) and ``biz_page.*`` (enforcing finance markings and record visibility)."""

    def __init__(
        self,
        *,
        reader_mailboxes: set[str],
        reader_chats: set[str],
        excluded_handles: set[str] = frozenset(),
        has_finance: bool = True,
        with_transcript: bool = False,
        n_emails: int = 2,
        n_pos: int = 2,
    ) -> None:
        self.reader_mailboxes = set(reader_mailboxes)
        self.reader_chats = set(reader_chats)
        self.excluded_handles = set(excluded_handles)
        self.has_finance = has_finance
        self.with_transcript = with_transcript
        self.n_emails = n_emails
        self.n_pos = n_pos
        self.queries: list[str] = []

    def transaction(self):
        class _Ctx:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        return _Ctx()

    def execute(self, sql: str, params=None):
        self.queries.append(sql)
        low = " ".join(sql.lower().split())

        if "set transaction" in low or "set local" in low:
            return _FakeResult([])

        if "from ontology.object_versions" in low and "where object_id =" in low:
            oid = params[0]
            mapping = {
                MEETING_OID: MEETING_H,
                SUP_OID: SUP_H,
                CUS_OID: CUS_H,
            }
            h = mapping.get(oid)
            return _FakeResult([(h,)] if h else [])

        if "from biz.lineage" in low:
            oid = params[1]
            mapping = {SUP_OID: SUP_H, CUS_OID: CUS_H}
            h = mapping.get(oid)
            return _FakeResult([(h,)] if h else [])

        if "from pg_class c join pg_namespace n" in low:
            names = params[1]
            return _FakeResult([(n,) for n in names])

        if "select to_regclass" in low:
            return _FakeResult([(True,)])

        # Guarded biz_person.meetings (single meeting lookup)
        if "from biz_person.meetings where handle =" in low:
            if "alice" not in self.reader_mailboxes:
                return _FakeResult([])
            return _FakeResult([(
                "Croda Q4 Supply Sync",
                datetime(2026, 11, 26, 14, 0, tzinfo=timezone.utc),
                datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc),
                "alice@petralabx.com",
                ["alice@petralabx.com", "rep@croda.com", "bob@petralabx.com"],
                "Teams",
                True,
                "ical-croda-series",
                [CUS_H],
                [SUP_H],
                [],
                MEETING_H,
            )])

        # Guarded biz_person.meeting_transcripts
        if "from biz_person.meeting_transcripts" in low:
            if not self.with_transcript:
                return _FakeResult([])
            if "limit 1" in low and "select 1" in low:
                return _FakeResult([(1,)])
            return _FakeResult([(
                "transcript://alice/t-1@1",
                "Croda Q3 Supply Sync",
                datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc),
                ["Alice", "Croda Rep"],
                42,
            )])

        # Earlier meetings
        if "from biz_person.meetings" in low and "start_at <" in low:
            if "alice" not in self.reader_mailboxes:
                return _FakeResult([])
            return _FakeResult([(
                "graph-cal://alice/ev-croda-prev@1",
                "Croda Q3 Supply Sync",
                datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc),
                "alice@petralabx.com",
                1,
            )])

        # Guarded biz_person.emails (enforces mailbox + people exclusion!)
        if "from biz_person.emails" in low and "assist.findings_feed" not in low:
            all_emails = []
            for i in range(self.n_emails):
                all_emails.append({
                    "handle": f"graph-mail://alice/m-{i}@1",
                    "mailbox": "alice",
                    "subject": f"Croda PO10010{i} delivery update",
                    "sender": "rep@croda.com",
                    "when": datetime(2026, 11, 20 - (i % 15), 10, 0, tzinfo=timezone.utc),
                    "summary": f"Shipment for PO10010{i} leaves warehouse Friday.",
                    "records": [f"bc://po/10010{i}@1"],
                })
            # Private mailbox email (another user's mailbox)
            all_emails.append({
                "handle": "graph-mail://carol/private-1@1",
                "mailbox": "carol",
                "subject": "Carol private salary discussion",
                "sender": "carol@petralabx.com",
                "when": datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc),
                "summary": "PRIVATE MAILBOX SECRET",
                "records": [],
            })
            # Excluded person's email
            all_emails.append({
                "handle": "graph-mail://alice/excluded-1@1",
                "mailbox": "alice",
                "subject": "Excluded person confidential note",
                "sender": "excluded@petralabx.com",
                "when": datetime(2026, 11, 24, 11, 0, tzinfo=timezone.utc),
                "summary": "EXCLUDED PERSON SECRET",
                "records": [],
            })
            visible = [
                e for e in all_emails
                if e["mailbox"] in self.reader_mailboxes and e["handle"] not in self.excluded_handles
            ]
            total = len(visible)
            return _FakeResult([
                (e["handle"], e["subject"], e["sender"], e["when"], e["summary"], e["records"], total)
                for e in visible[:params["lim"]]
            ])

        # Guarded biz_person.teams_messages (enforces chat access + people exclusion!)
        if "from biz_person.teams_messages" in low and "assist.findings_feed" not in low:
            all_teams = [
                {
                    "handle": "teams://chat-1/msg-1@1",
                    "chat": "chat-1",
                    "title": "Croda Sourcing",
                    "who": "Bob Buyer",
                    "when": datetime(2026, 11, 23, 16, 30, tzinfo=timezone.utc),
                    "summary": "Confirmed lead time is 14 days for batch B-9.",
                    "records": [],
                },
                {
                    "handle": "teams://chat-private/msg-2@1",
                    "chat": "chat-private",
                    "title": "Private Exec Chat",
                    "who": "Carol Exec",
                    "when": datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc),
                    "summary": "PRIVATE TEAMS SECRET",
                    "records": [],
                },
                {
                    "handle": "teams://chat-1/msg-excl@1",
                    "chat": "chat-1",
                    "title": "Croda Sourcing",
                    "who": "Excluded Person",
                    "when": datetime(2026, 11, 24, 13, 0, tzinfo=timezone.utc),
                    "summary": "EXCLUDED TEAMS SECRET",
                    "records": [],
                },
            ]
            visible = [
                t for t in all_teams
                if t["chat"] in self.reader_chats and t["handle"] not in self.excluded_handles
            ]
            total = len(visible)
            return _FakeResult([
                (t["handle"], t["title"], t["who"], t["when"], t["summary"], t["records"], total)
                for t in visible[:params["lim"]]
            ])

        # Guarded assist.findings_feed joined to biz_person.*
        if "assist.findings_feed" in low:
            all_findings = [
                {
                    "handle": "graph-mail://alice/m-0@1",
                    "mailbox": "alice",
                    "when": datetime(2026, 11, 22, 14, 0, tzinfo=timezone.utc),
                    "kind": "decision",
                    "topic": "pricing",
                    "summary": "Agreed to lock Q4 surfactant price at $4.20/kg.",
                    "who": "rep@croda.com",
                    "title": "Croda PO100100 delivery update",
                    "channel": "email",
                },
                {
                    "handle": "graph-mail://carol/private-1@1",
                    "mailbox": "carol",
                    "when": datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc),
                    "kind": "decision",
                    "topic": "hr",
                    "summary": "PRIVATE FINDING SECRET",
                    "who": "carol@petralabx.com",
                    "title": "Carol private",
                    "channel": "email",
                },
                {
                    "handle": "graph-mail://alice/excluded-1@1",
                    "mailbox": "alice",
                    "when": datetime(2026, 11, 24, 11, 0, tzinfo=timezone.utc),
                    "kind": "request",
                    "topic": "hr",
                    "summary": "EXCLUDED FINDING SECRET",
                    "who": "excluded@petralabx.com",
                    "title": "Excluded note",
                    "channel": "email",
                },
            ]
            visible = [
                f for f in all_findings
                if f["mailbox"] in self.reader_mailboxes and f["handle"] not in self.excluded_handles
            ]
            total = len(visible)
            return _FakeResult([
                (f["when"], f["kind"], f["topic"], f["summary"], f["who"], f["handle"],
                 f["title"], f["channel"], total)
                for f in visible[:params["lim"]]
            ])

        # biz_page.customers
        if "from customers where" in low:
            return _FakeResult([(CUS_H, "Acme Retail", "Acme Group")])

        # biz_page.suppliers
        if "from suppliers where" in low:
            return _FakeResult([(SUP_H, "Croda Inc.")])

        # biz_page.staff
        if "from staff where" in low:
            return _FakeResult([
                ("alice@petralabx.com", "Alice Ops", "oid-alice"),
                ("bob@petralabx.com", "Bob Buyer", "oid-bob"),
            ])

        # biz_page.purchase_orders
        if "from purchase_orders" in low:
            rows = []
            for i in range(self.n_pos):
                rows.append((
                    f"PO10010{i}",
                    "Croda Inc.",
                    date(2026, 11, 10 + (i % 10)),
                    date(2026, 11, 28),
                    "late" if i == 0 else "neither",
                    15 if i == 0 else None,
                    Decimal("12500.00") if self.has_finance else None,
                    "CAD" if self.has_finance else None,
                    "open",
                    f"bc://po/10010{i}@1",
                    [] if self.has_finance else ["total"],
                    self.n_pos,
                ))
            return _FakeResult(rows[:params["lim"]])

        # biz_page.sales_orders
        if "from sales_orders" in low:
            return _FakeResult([(
                "SO-9001",
                "Acme Retail",
                "ACME-PO-77",
                date(2026, 11, 20),
                5,
                Decimal("8400.00") if self.has_finance else None,
                "CAD" if self.has_finance else None,
                "open",
                "portal://so/9001@1",
                [] if self.has_finance else ["value"],
                1,
            )])

        # biz_page.alerts
        if "from alerts" in low:
            return _FakeResult([
                (
                    "po_late",
                    "late_po",
                    "purchase_orders",
                    "bc://po/100100@1",
                    "",
                    "PO100100 from Croda Inc. is 15 days past requested date",
                    datetime(2026, 11, 11, 0, 0, tzinfo=timezone.utc),
                    "oid-bob",
                    3,
                ),
                (
                    "planner_task_overdue",
                    "planner_task_overdue",
                    "records",
                    "planner://task/t-1@1",
                    "",
                    "Verify Croda COA before release",
                    datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc),
                    "oid-bob",
                    3,
                ),
                (
                    "negative_margin",
                    "negative_margin",
                    "sales_orders",
                    "portal://so/9001@1",
                    "",
                    "SO-9001 negative margin alert",
                    datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc),
                    "",
                    3,
                ),
            ])

        # biz_page.customer_balances / supplier_balances
        if "from customer_balances" in low:
            return _FakeResult([(CUS_H, "CAD", "31-60", Decimal("5000.00"), "sage://ar/1@1")])
        if "from supplier_balances" in low:
            return _FakeResult([(SUP_H, "CAD", "current", Decimal("3200.00"), "sage://ap/1@1")])

        # ontology.object_versions handle -> object_id lookup
        if "select v.l1_handle, o.object_id, o.object_type" in low:
            out = []
            for h_val in params[0]:
                if h_val == MEETING_H:
                    out.append((h_val, MEETING_OID, "CalendarEvent"))
                elif h_val == SUP_H:
                    out.append((h_val, SUP_OID, "Supplier"))
                elif h_val == CUS_H:
                    out.append((h_val, CUS_OID, "Customer"))
                elif h_val.startswith("graph-mail://"):
                    out.append((h_val, "Email:graph.mail:" + h_val.rsplit("/", 1)[-1], "Email"))
                elif h_val.startswith("teams://"):
                    out.append((h_val, "TeamsChatMessage:teams:" + h_val.rsplit("/", 1)[-1], "TeamsChatMessage"))
                elif h_val.startswith("bc://po/"):
                    out.append((h_val, "PurchaseOrder:bc.po:" + h_val.rsplit("/", 1)[-1], "PurchaseOrder"))
                elif h_val.startswith("portal://so/"):
                    out.append((h_val, "SalesOrder:portal.so:" + h_val.rsplit("/", 1)[-1], "SalesOrder"))
            return _FakeResult(out)

        return _FakeResult([])


def _run_fake_scoped_read(pg, door, work, seconds, *, after=None, **kw):
    got = work(pg)
    if after is not None:
        return got, after(pg, got)
    return got


class MeetingBriefDoorTests(unittest.TestCase):
    def _door(self, held=("finance",)):
        return SimpleNamespace(grant=SimpleNamespace(held=frozenset(held)))

    def test_rich_brief_without_transcripts_has_mail_teams_orders_alerts_decisions_and_truthful_note(self):
        pg = _FakePg(
            reader_mailboxes={"alice"},
            reader_chats={"chat-1"},
            excluded_handles={"graph-mail://alice/excluded-1@1", "teams://chat-1/msg-excl@1"},
            has_finance=True,
            with_transcript=False,
        )
        with mock.patch("operating.ontology_tools_sql.scoped_read", side_effect=_run_fake_scoped_read):
            out = mbd.build(
                pg,
                self._door(("finance",)),
                {"plx.reader_audience": '["mail:alice"]'},
                MEETING_OID,
                now=NOW,
                reader_addrs=["alice@petralabx.com"],
                biz_schema="biz",
            )

        self.assertEqual(out["schema"], br.MEETING_SCHEMA)
        self.assertEqual(out["title"], "Croda Q4 Supply Sync")
        self.assertEqual(out["as_of"], NOW.isoformat(timespec="seconds"))
        self.assertEqual({p["label"] for p in out["parties"]}, {"Croda Inc.", "Acme Retail"})

        # Truthful state when 0 transcripts exist
        notes = _sec(out, "notes")
        self.assertEqual(notes["state"], "not_available")
        self.assertEqual(notes["note"], mbd.NO_TRANSCRIPT_NOTE)
        self.assertEqual(notes["rows"], [])

        # Recent emails & Teams with date, sender, concise summary, and record link
        threads = _sec(out, "threads")
        self.assertEqual(threads["state"], "shown")
        labels = [r["label"] for r in threads["rows"]]
        self.assertTrue(any(l.startswith("Mail: Croda PO10010") for l in labels))
        self.assertTrue(any(l.startswith("Teams: Croda Sourcing") for l in labels))
        for r in threads["rows"]:
            self.assertTrue(r["date"])
            self.assertTrue(r["who"])
            self.assertTrue(r["detail"])
            self.assertTrue(r["object_id"])
            self.assertTrue(r["sources"])

        # Open POs (late PO first), open sales orders
        orders = _sec(out, "orders")
        self.assertEqual(orders["state"], "shown")
        self.assertEqual(orders["rows"][0]["label"], "PO PO100100")
        self.assertIn("15 days past its expected date", orders["rows"][0]["detail"])
        self.assertEqual(orders["rows"][0]["amount"], {"amount": "12500.00", "currency": "CAD"})
        self.assertTrue(any(r["label"] == "Order SO-9001" for r in orders["rows"]))

        # Open alerts & tasks
        alerts = _sec(out, "alerts")
        self.assertEqual(alerts["state"], "shown")
        self.assertTrue(any("Verify Croda COA" in (r["detail"] or "") for r in alerts["rows"]))

        # Decisions from prior threads
        decisions = _sec(out, "decisions")
        self.assertEqual(decisions["state"], "shown")
        self.assertTrue(any("lock Q4 surfactant price" in r["label"] for r in decisions["rows"]))

        # Suggested talking points populated from shown rows (not empty!)
        tp = _sec(out, "talking_points")
        self.assertEqual(tp["state"], "shown")
        self.assertGreaterEqual(len(tp["rows"]), 2)

        # ZERO leakage of excluded person or another user's private mailbox/chat!
        dumped = json.dumps(out)
        for secret in (
            "PRIVATE MAILBOX SECRET",
            "EXCLUDED PERSON SECRET",
            "PRIVATE TEAMS SECRET",
            "EXCLUDED TEAMS SECRET",
            "PRIVATE FINDING SECRET",
            "EXCLUDED FINDING SECRET",
            "Carol private salary",
        ):
            self.assertNotIn(secret, dumped)

    def test_transcript_present_shows_notes_section(self):
        pg = _FakePg(reader_mailboxes={"alice"}, reader_chats={"chat-1"}, with_transcript=True)
        with mock.patch("operating.ontology_tools_sql.scoped_read", side_effect=_run_fake_scoped_read):
            out = mbd.build(
                pg, self._door(), {"plx.reader_audience": '["mail:alice"]'},
                MEETING_OID, now=NOW, reader_addrs=["alice@petralabx.com"], biz_schema="biz",
            )
        notes = _sec(out, "notes")
        self.assertEqual(notes["state"], "shown")
        self.assertEqual(notes["rows"][0]["label"], "Notes: Croda Q3 Supply Sync")
        self.assertIn("42 spoken lines", notes["rows"][0]["detail"])

    def test_non_finance_reader_hides_money_and_finance_alerts(self):
        pg = _FakePg(
            reader_mailboxes={"alice"},
            reader_chats={"chat-1"},
            excluded_handles={"graph-mail://alice/excluded-1@1", "teams://chat-1/msg-excl@1"},
            has_finance=False,
        )
        with mock.patch("operating.ontology_tools_sql.scoped_read", side_effect=_run_fake_scoped_read):
            out = mbd.build(
                pg, self._door(held=()), {"plx.reader_audience": '["mail:alice"]'},
                MEETING_OID, now=NOW, reader_addrs=["alice@petralabx.com"], biz_schema="biz",
            )
        money = _sec(out, "money")
        self.assertEqual(money["state"], "finance_marked")
        self.assertEqual(money["rows"], [])
        for r in _sec(out, "orders")["rows"]:
            self.assertIsNone(r["amount"])
        self.assertNotIn("12500.00", json.dumps(out))
        self.assertNotIn("negative margin", json.dumps(out).lower())

    def test_unreadable_meeting_refuses_fail_closed(self):
        pg = _FakePg(reader_mailboxes={"bob"}, reader_chats=set())
        with mock.patch("operating.ontology_tools_sql.scoped_read", side_effect=_run_fake_scoped_read):
            with self.assertRaises(br.BriefRefused):
                mbd.build(
                    pg, self._door(), {"plx.reader_audience": '["mail:bob"]'},
                    MEETING_OID, now=NOW, reader_addrs=["bob@petralabx.com"], biz_schema="biz",
                )

    def test_guardrail1_no_silent_truncation_and_constant_query_count(self):
        pg_small = _FakePg(reader_mailboxes={"alice"}, reader_chats={"chat-1"}, n_emails=2, n_pos=2)
        pg_large = _FakePg(reader_mailboxes={"alice"}, reader_chats={"chat-1"}, n_emails=25, n_pos=20)
        with mock.patch("operating.ontology_tools_sql.scoped_read", side_effect=_run_fake_scoped_read):
            mbd.build(pg_small, self._door(), {"a": "1"}, MEETING_OID, now=NOW, biz_schema="biz")
            out_large = mbd.build(pg_large, self._door(), {"a": "1"}, MEETING_OID, now=NOW, biz_schema="biz")

        # O(1) SQL statements: exact same number of queries for N=2 vs N=25 (zero N+1!)
        self.assertEqual(len(pg_small.queries), len(pg_large.queries))
        self.assertLessEqual(len(pg_large.queries), 25)

        # Exact visible `more` count reported, never silently truncated
        orders = _sec(out_large, "orders")
        self.assertEqual(len(orders["rows"]), br.MAX_ROWS)
        self.assertEqual(orders["more"], (20 + 1) - br.MAX_ROWS)
        threads = _sec(out_large, "threads")
        self.assertEqual(len(threads["rows"]), br.MAX_ROWS)
        self.assertGreater(threads["more"], 0)


class MeetingBriefRealPostgresPermissionTest(unittest.TestCase):
    """End-to-end Postgres test proving security_barrier guarded views enforce mailbox/chat
    permissions and people exclusion when Postgres is available (runs on Spark)."""

    def test_guarded_views_block_excluded_person_and_private_mailbox(self):
        dsn = os.environ.get("PLX_PG_DSN") or os.environ.get("PLX_TEST_PG_DSN") or os.environ.get("PG_DSN")
        if not dsn:
            self.skipTest("no PLX_PG_DSN in environment")
        import psycopg

        with psycopg.connect(dsn, autocommit=True) as pg:
            schema = "mb_test_biz"
            person = f"{schema}_person"
            page = f"{schema}_page"
            try:
                pg.execute(f"DROP SCHEMA IF EXISTS {schema}, {person}, {page} CASCADE")
                pg.execute(f"CREATE SCHEMA {schema}; CREATE SCHEMA {person}; CREATE SCHEMA {page};")
                pg.execute(f"""
                    CREATE TABLE {schema}.meetings (
                        subject text, start_at timestamptz, end_at timestamptz,
                        organizer text, attendees text[], location text, is_online boolean,
                        is_cancelled boolean, ical_uid text, customers text[], suppliers text[],
                        records text[], handle text, unit text, excluded boolean
                    );
                    CREATE TABLE {schema}.meeting_transcripts (
                        handle text, subject text, meeting_start timestamptz,
                        ical_uid text, speakers text[], cue_count integer,
                        customers text[], suppliers text[], unit text, excluded boolean
                    );
                    CREATE TABLE {schema}.emails (
                        handle text, subject text, sender text, received_at timestamptz,
                        said text, thread text, message_id text, is_bulk boolean,
                        addresses text[], customers text[], suppliers text[], records text[],
                        unit text, excluded boolean
                    );
                    CREATE TABLE {schema}.teams_messages (
                        handle text, subject text, chat_topic text, author text, sender text,
                        created_at timestamptz, said text, chat text, addresses text[],
                        customers text[], suppliers text[], records text[],
                        unit text, excluded boolean
                    );
                    CREATE VIEW {person}.meetings WITH (security_barrier) AS
                        SELECT subject, start_at, end_at, organizer, attendees, location,
                               is_online, is_cancelled, ical_uid, customers, suppliers, records, handle
                        FROM {schema}.meetings
                        WHERE NOT excluded
                          AND unit = ANY(ARRAY(SELECT json_array_elements_text(
                              current_setting('plx.reader_audience', true)::json)));
                    CREATE VIEW {person}.meeting_transcripts WITH (security_barrier) AS
                        SELECT handle, subject, meeting_start, ical_uid, speakers, cue_count,
                               customers, suppliers
                        FROM {schema}.meeting_transcripts
                        WHERE NOT excluded
                          AND unit = ANY(ARRAY(SELECT json_array_elements_text(
                              current_setting('plx.reader_audience', true)::json)));
                    CREATE VIEW {person}.emails WITH (security_barrier) AS
                        SELECT handle, subject, sender, received_at, said, thread, message_id,
                               is_bulk, addresses, customers, suppliers, records
                        FROM {schema}.emails
                        WHERE NOT excluded
                          AND unit = ANY(ARRAY(SELECT json_array_elements_text(
                              current_setting('plx.reader_audience', true)::json)));
                    CREATE VIEW {person}.teams_messages WITH (security_barrier) AS
                        SELECT handle, subject, chat_topic, author, sender, created_at, said,
                               chat, addresses, customers, suppliers, records
                        FROM {schema}.teams_messages
                        WHERE NOT excluded
                          AND unit = ANY(ARRAY(SELECT json_array_elements_text(
                              current_setting('plx.reader_audience', true)::json)));
                """)
                pg.execute(
                    f"INSERT INTO {schema}.meetings VALUES "
                    "('Vendor Sync', '2026-11-26T14:00:00Z', '2026-11-26T15:00:00Z', "
                    "'alice@plx.test', ARRAY['alice@plx.test','rep@vendor.test'], 'Teams', true, "
                    "false, 'uid-1', '{}', ARRAY['sup-1'], '{}', 'cal://alice/1', 'mail:alice', false)"
                )
                pg.execute(
                    f"INSERT INTO {schema}.emails VALUES "
                    "('mail://alice/ok', 'Vendor quote', 'rep@vendor.test', '2026-11-24T10:00:00Z', "
                    "'Pricing attached', 'th-1', 'mid-1', false, ARRAY['rep@vendor.test','alice@plx.test'], "
                    "'{}', ARRAY['sup-1'], '{}', 'mail:alice', false), "
                    "('mail://bob/private', 'Bob private mail', 'rep@vendor.test', '2026-11-24T11:00:00Z', "
                    "'BOB PRIVATE BODY', 'th-2', 'mid-2', false, ARRAY['rep@vendor.test','bob@plx.test'], "
                    "'{}', ARRAY['sup-1'], '{}', 'mail:bob', false), "
                    "('mail://alice/excl', 'Excluded person mail', 'excl@plx.test', '2026-11-24T12:00:00Z', "
                    "'EXCLUDED BODY', 'th-3', 'mid-3', false, ARRAY['rep@vendor.test','excl@plx.test'], "
                    "'{}', ARRAY['sup-1'], '{}', 'mail:alice', true)"
                )
                pg.execute(
                    f"INSERT INTO {schema}.teams_messages VALUES "
                    "('teams://c1/ok', 'Vendor chat', 'Sourcing', 'Alice', 'alice@plx.test', "
                    "'2026-11-24T13:00:00Z', 'ETA is Monday', 'c1', ARRAY['rep@vendor.test'], "
                    "'{}', ARRAY['sup-1'], '{}', 'teams:c1', false), "
                    "('teams://c2/private', 'Exec chat', 'Exec', 'Bob', 'bob@plx.test', "
                    "'2026-11-24T14:00:00Z', 'PRIVATE EXEC CHAT', 'c2', ARRAY['rep@vendor.test'], "
                    "'{}', ARRAY['sup-1'], '{}', 'teams:c2', false), "
                    "('teams://c1/excl', 'Vendor chat', 'Sourcing', 'Excl', 'excl@plx.test', "
                    "'2026-11-24T15:00:00Z', 'EXCLUDED TEAMS BODY', 'c1', ARRAY['rep@vendor.test'], "
                    "'{}', ARRAY['sup-1'], '{}', 'teams:c1', true)"
                )

                with pg.transaction():
                    pg.execute(
                        "SELECT set_config('plx.reader_audience', %s, true)",
                        (json.dumps(["mail:alice", "teams:c1"]),),
                    )
                    got = mbd._read_comms_phase(
                        pg, person, "CalendarEvent", "cal://alice/1", NOW,
                        frozenset({"alice@plx.test"}),
                    )
                self.assertEqual([e["handle"] for e in got["emails"]], ["mail://alice/ok"])
                self.assertEqual([t["handle"] for t in got["teams"]], ["teams://c1/ok"])
                self.assertFalse(got["has_any_transcript"])
                self.assertEqual(got["transcripts"], [])
                dumped = json.dumps(got, default=str)
                self.assertNotIn("BOB PRIVATE BODY", dumped)
                self.assertNotIn("EXCLUDED BODY", dumped)
                self.assertNotIn("PRIVATE EXEC CHAT", dumped)
                self.assertNotIn("EXCLUDED TEAMS BODY", dumped)
            finally:
                pg.execute(f"DROP SCHEMA IF EXISTS {schema}, {person}, {page} CASCADE")


if __name__ == "__main__":
    unittest.main()
