"""Unified persistent memory engine tests (``operating.autopilot_memory``):
- Stale, archived, deleted, superseded, inactive, and expired entries are never returned.
- Duplicate lines (case, whitespace, punctuation, ``[pref N]`` tags) collapse cleanly.
- Strict precedence: user-specific instructions > role/domain guidance > company-wide
  preferences, with user rules overriding lower-precedence defaults on the same subject.
- Strict cross-user isolation both in memory and over Postgres ``memory.item``.
- Shared ``memory_context_for_reader`` produces clean, em-dash-free formatted context ready
  for both Assist planning and Ask context.
Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import os
import unittest
from unittest import mock

from operating import autopilot_memory as apm
from operating import learning as L
from operating.tests import pg_testdb

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

NOW = dt.datetime(2026, 11, 25, 18, 0, tzinfo=dt.timezone.utc)
VINCE = "p-vince"
RICARDO = "p-ricardo"


class PersistentMemoryUnitTests(unittest.TestCase):
    def test_filters_stale_archived_superseded_deleted_and_expired_entries(self):
        entries = [
            {"id": 1, "scope": "user", "kind": "instruction", "text": "Always keep replies concise."},
            {"id": 2, "scope": "user", "kind": "instruction", "text": "Deleted rule",
             "deleted_at": "2026-11-20T00:00:00Z"},
            {"id": 3, "scope": "user", "kind": "instruction", "text": "Superseded rule",
             "superseded_by": 99},
            {"id": 4, "scope": "user", "kind": "preference", "text": "Archived rule",
             "archived": True},
            {"id": 5, "scope": "user", "kind": "preference", "text": "Inactive status rule",
             "status": "archived"},
            {"id": 6, "scope": "user", "kind": "preference", "text": "Active false rule",
             "active": False},
            {"id": 7, "scope": "user", "kind": "fact", "text": "Expired travel note",
             "expires_at": "2026-11-24T00:00:00Z"},
            {"id": 8, "scope": "user", "kind": "fact", "text": "Future valid note",
             "expires_at": "2026-12-01T00:00:00Z"},
            {"id": 9, "scope": "user", "kind": "preference", "text": "   "},
        ]
        got = apm.resolve_memory_entries(VINCE, entries=entries, now=NOW)
        self.assertEqual([g["text"] for g in got], [
            "Always keep replies concise.",
            "Future valid note",
        ])

    def test_precedence_user_over_role_domain_over_company_and_subject_override(self):
        entries = [
            {
                "id": 10,
                "scope": "company",
                "kind": "preference",
                "subject": "signoff",
                "text": "Company default sign-off: Best regards, Petra Soap Team.",
                "importance": 0.9,
            },
            {
                "id": 11,
                "scope": "company",
                "kind": "instruction",
                "subject": "currency",
                "text": "Always state CAD or USD next to quoted prices.",
                "importance": 0.8,
            },
            {
                "id": 12,
                "scope": "domain",
                "domain": "purchasing",
                "kind": "instruction",
                "subject": "po_lead_time",
                "text": "Ask vendors for confirmed ship dates on every PO.",
                "importance": 0.8,
            },
            {
                "id": 13,
                "scope": "domain",
                "domain": "finance",
                "kind": "instruction",
                "subject": "ap_terms",
                "text": "Finance-only AP rule should not appear for purchasing.",
                "importance": 0.9,
            },
            {
                "id": 14,
                "scope": "user",
                "kind": "instruction",
                "subject": "signoff",
                "text": "Sign off with Thanks, Vince.",
                "importance": 0.7,
            },
            {
                "id": 15,
                "scope": "user",
                "kind": "fact",
                "text": "Vince manages the SIDIA account directly.",
                "importance": 0.6,
            },
        ]
        resolved = apm.resolve_memory_entries(VINCE, domain="purchasing", entries=entries, now=NOW)
        self.assertEqual([r["id"] for r in resolved], [14, 15, 12, 11])
        self.assertEqual([r["scope"] for r in resolved], ["user", "user", "role", "company"])

        ctx = apm.memory_context_for_reader(VINCE, domain="purchasing", entries=entries, now=NOW)
        self.assertIn("User instructions and preferences:\n- Sign off with Thanks, Vince.", ctx)
        self.assertIn("Role and domain guidance:\n- Ask vendors for confirmed ship dates on every PO.", ctx)
        self.assertIn("Company-wide preferences:\n- Always state CAD or USD next to quoted prices.", ctx)
        # User signoff overrides company default signoff; finance domain excluded
        self.assertNotIn("Company default sign-off", ctx)
        self.assertNotIn("Finance-only AP rule", ctx)
        self.assertNotIn("\u2014", ctx)
        self.assertLess(ctx.index("User instructions"), ctx.index("Role and domain"))
        self.assertLess(ctx.index("Role and domain"), ctx.index("Company-wide"))

    def test_deduplicates_case_whitespace_punctuation_pref_tags_and_em_dashes(self):
        entries = [
            {"id": 1, "scope": "user", "kind": "instruction",
             "text": "[pref 42] Keep replies under 3 sentences \u2014 no filler."},
            {"id": 2, "scope": "user", "kind": "preference",
             "text": "keep replies under 3 sentences - no filler"},
            {"id": 3, "scope": "company", "kind": "preference",
             "text": "Keep replies under 3 sentences - no filler!"},
        ]
        resolved = apm.resolve_memory_entries(VINCE, entries=entries, now=NOW)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["scope"], "user")
        self.assertNotIn("\u2014", resolved[0]["text"])

    def test_strict_cross_user_isolation_in_entries(self):
        entries = [
            {"id": 1, "reader": RICARDO, "kind": "instruction", "text": "Ricardo private note"},
            {"id": 2, "reader": VINCE, "kind": "instruction", "text": "Vince private note"},
            {"id": 3, "reader": "company", "kind": "preference", "text": "Shared company note"},
        ]
        resolved = apm.resolve_memory_entries(VINCE, entries=entries, now=NOW)
        self.assertEqual([r["text"] for r in resolved], [
            "Vince private note",
            "Shared company note",
        ])

    def test_learned_style_ranks_by_specificity_and_deduplicates_case_insensitively(self):
        def lesson(key, text, conf=0.9):
            return L.Lesson(
                lesson_id=key, scope="person", who=VINCE, kind="style", key=key,
                value="v", confidence=conf, weight=1, total=1, seen=5,
                first_at="2026-10-01T00:00:00Z", last_at="2026-10-02T00:00:00Z",
                evidence=[], text=text,
            )
        sender = "greg@petrasoap.com"
        lessons = L.Lessons([
            lesson("length", "keeps replies short."),
            lesson("side:internal:length", "Keeps replies short"),
            lesson("side:internal:signoff", "Ends internal messages without a sign-off"),
            lesson(f"sender:{L.sender_key(sender)}:greeting", "Starts replies to Greg with Hey Greg"),
        ])
        got = apm.learned_style(lessons, VINCE, {"side": "internal", "sender": sender})
        # Sender-specific rule comes first, then side-specific; duplicate length collapsed
        self.assertEqual(got, [
            "Learned from their replies: Starts replies to Greg with Hey Greg",
            "Learned from their replies: Keeps replies short",
            "Learned from their replies: Ends internal messages without a sign-off",
        ])


@unittest.skipIf(psycopg is None, "psycopg not installed")
class PersistentMemoryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.name, cls.dsn = pg_testdb.create_db()
        cls.addClassCleanup(pg_testdb.drop_db, cls.name)

    def setUp(self):
        self.pg = psycopg.connect(self.dsn, autocommit=True)
        self.addCleanup(self.pg.close)
        from operating.pg.migrate import MIGRATIONS_DIR
        self.pg.execute("DROP SCHEMA IF EXISTS memory CASCADE")
        self.pg.execute((MIGRATIONS_DIR / "0020_memory.sql").read_text(encoding="utf-8"))

    def insert_item(self, reader, kind, text, *, subject=None, importance=0.7,
                    superseded_by=None, deleted_at=None):
        self.pg.execute(
            "INSERT INTO memory.item (reader, kind, source, subject, text, importance, "
            "superseded_by, deleted_at) VALUES (%s, %s, 'user', %s, %s, %s, %s, %s)",
            (reader, kind, subject, text, importance, superseded_by, deleted_at),
        )

    def test_postgres_retrieval_precedence_isolation_and_replay_ceiling(self):
        # Vince's active user instruction (using composite principals key)
        self.insert_item(f"employee:{VINCE},role:sales", "instruction",
                         "Always include lot numbers in customer updates.", subject="updates")
        # Superseded and deleted Vince entries MUST be excluded
        self.insert_item(f"employee:{VINCE}", "instruction", "Old superseded rule",
                         superseded_by=999)
        self.insert_item(f"employee:{VINCE}", "preference", "Deleted preference",
                         deleted_at=NOW)
        # Ricardo's private memory MUST NEVER leak to Vince
        self.insert_item(f"employee:{RICARDO}", "instruction",
                         "Ricardo confidential note")
        # Role/domain and company-wide preferences
        self.insert_item("domain:sales", "preference",
                         "Confirm lead time in business days.", subject="lead_time")
        self.insert_item("company", "preference",
                         "Never promise ship dates before QC release.", subject="qc_gate")
        # Company-wide rule on same subject 'updates' is overridden by Vince's user rule
        self.insert_item("company", "preference",
                         "Generic company update rule.", subject="updates")

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLX_READ_CEILING", None)
            ctx = apm.memory_context_for_reader(VINCE, domain="sales", pg=self.pg, now=NOW)
        self.assertIn("Always include lot numbers in customer updates.", ctx)
        self.assertIn("Confirm lead time in business days.", ctx)
        self.assertIn("Never promise ship dates before QC release.", ctx)
        self.assertNotIn("Old superseded rule", ctx)
        self.assertNotIn("Deleted preference", ctx)
        self.assertNotIn("Ricardo confidential note", ctx)
        self.assertNotIn("Generic company update rule", ctx)
        self.assertLess(ctx.index("Always include lot numbers"), ctx.index("Confirm lead time"))
        self.assertLess(ctx.index("Confirm lead time"), ctx.index("Never promise ship dates"))

        # Replay ceiling hides live Postgres memory so replays never leak future preferences
        with mock.patch.dict(os.environ, {"PLX_READ_CEILING": "2026-10-01T00:00:00Z"}):
            self.assertEqual(apm.memory_context_for_reader(VINCE, domain="sales", pg=self.pg, now=NOW), "")


if __name__ == "__main__":
    unittest.main()
