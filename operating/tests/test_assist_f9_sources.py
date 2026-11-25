"""F9 / PLAN-B6 missing-source disclosure and PLAN-B13 opened-handle tracking tests.

Verifies:
1. Denied, unavailable, timed-out, withheld, and uncaptured sources are recorded by
   plain-English name on every prepared item (``plan.prepared.unread_sources``).
2. Fail-closed privacy: restricted record titles, handle IDs, email addresses, dollar
   amounts, raw Python tracebacks, and internal schema/table names NEVER leak into
   ``unread_sources``, ``unread_summary``, or ``provenance_summary``.
3. Clean runs persist an explicit empty list ``unread_sources: []`` (distinguishable
   from missing/unrecorded) and ``provenance_summary`` reports all sources read.
4. Zero em dashes (``\\u2014``) in formatted summaries and provenance output.
5. PLAN-B13: ``assist_plan.matter_inputs`` records every handle opened (including
   thread handles not cited/shown in the truncated text window) in ``opened_handles``
   and via ``note_opened``.
Evidence label: isolated-fixture."""
from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from operating import assist_plan as plan
from operating import assist_prepared as prep
from operating import assist_worker as aw
from operating import session_trace
from operating.ontology_tools_policy import current_snapshot, opened_in, snapshot_reads
from operating.tests import test_assist_matters as base

NOW, ANN, OUT = base.NOW, base.ANN, base.OUT
REPLY = [{"draft": "Thanks, I will check on this.", "note": "", "warning": ""}]


def _inputs(text: str = "Standard customer question", **extra):
    return lambda ms: {m["matter_id"]: {"text": text, "situation": "s", **extra} for m in ms}


def _nearest(ms, gs):
    return {m["matter_id"]: ([], 0) for m in ms}


class UnreadSourcesDisclosure(unittest.TestCase):
    setUpClass = classmethod(base.Matters.setUpClass.__func__)
    setUp = base.Matters.setUp
    biz_tables = base.Matters.biz_tables
    mail = base.Matters.mail
    read = base.Matters.read
    cycle = base.Matters.cycle

    def ask(self, thread: str, owner: str = ANN, hours: int = 3) -> None:
        at = NOW - dt.timedelta(hours=hours)
        h = self.mail(thread, OUT, [f"{owner[2:]}@ours.example"], at=at, box=owner)
        self.read(h, thread=thread, actor=owner, at=at)

    def healthy_view_state(self) -> None:
        self.pg.execute(
            "CREATE TABLE IF NOT EXISTS biz.view_state "
            "(view text, failing_since timestamptz, quarantined_until timestamptz)"
        )
        self.pg.execute("TRUNCATE biz.view_state")
        self.pg.execute(
            "INSERT INTO biz.view_state VALUES ('emails', NULL, NULL), ('sales_orders', NULL, NULL)"
        )

    def prepared_of(self, subject: str) -> dict:
        row = self.pg.execute(
            "SELECT plan->'prepared' FROM assist.matters WHERE subject_key = %s",
            (subject,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsInstance(row[0], dict)
        return row[0]

    def test_clean_run_records_explicit_empty_unread_sources(self):
        self.healthy_view_state()
        self.ask("t-clean")
        self.cycle()

        def session(owner, oid, extra="", as_of="", prepared=None):
            session_trace.note(
                "0.05",
                [{"tool": "plx.sql", "arguments": {"q": "SELECT 1 FROM biz.sales_orders"}, "failed": False}],
            )
            return REPLY

        out = prep.serve(
            self.pg, [ANN], session, _inputs(), _nearest,
            screen=lambda t: None, model="stand-in",
        )
        self.assertEqual((out["served"], out["drafted"]), (1, 1))
        self.assertEqual(out["unread_sources"], [])
        prepared = self.prepared_of("t-clean")
        self.assertIn("unread_sources", prepared)
        self.assertEqual(prepared["unread_sources"], [])
        self.assertEqual(prepared["unread_summary"], "")
        self.assertEqual(prep.provenance_summary(prepared), "All consulted sources were read.")

    def test_failing_quarantined_denied_timed_out_and_uncaptured_sources_are_recorded_by_plain_name(self):
        self.healthy_view_state()
        self.pg.execute(
            "INSERT INTO biz.view_state VALUES "
            "('emails', now() - interval '1 hour', NULL), "
            "('sales_orders', NULL, now() + interval '2 hours')"
        )
        self.pg.execute("CREATE TABLE IF NOT EXISTS biz.meeting_transcripts (handle text)")
        self.pg.execute("TRUNCATE biz.meeting_transcripts")
        self.ask("t-missing")
        self.cycle()

        def session(owner, oid, extra="", as_of="", prepared=None):
            session_trace.note(
                "0.08",
                [
                    {
                        "tool": "plx.sql",
                        "arguments": {"q": "SELECT * FROM biz.shipments WHERE id = 1"},
                        "failed": True,
                        "error": "TimeoutError: statement deadline exceeded",
                    },
                    {
                        "tool": "plx.open",
                        "arguments": {"handle": "bc://so/SO-9999"},
                        "failed": True,
                        "denied": True,
                        "error": "HTTP 403 Forbidden",
                    },
                ],
            )
            session_trace.unread("calendar", "access denied")
            return REPLY

        out = prep.serve(
            self.pg,
            [ANN],
            session,
            _inputs("Can we check Fiix maintenance and Freightcom shipping for this Amazon order?"),
            _nearest,
            screen=lambda t: None,
            model="stand-in",
        )
        self.assertEqual(out["drafted"], 1)
        prepared = self.prepared_of("t-missing")
        entries = prepared["unread_sources"]
        by_source = {e["source"]: e["reason"] for e in entries}

        self.assertEqual(by_source.get("Email"), "temporarily failing")
        self.assertEqual(by_source.get("Sales orders"), "temporarily quarantined")
        self.assertEqual(by_source.get("Shipments"), "timed out")
        self.assertEqual(by_source.get("Business Central"), "access denied")
        self.assertEqual(by_source.get("Calendar"), "access denied")
        self.assertEqual(by_source.get("Meeting transcripts"), "not captured yet")
        self.assertEqual(by_source.get("Fiix"), "not captured yet")
        self.assertEqual(by_source.get("Freightcom"), "not captured yet")
        self.assertEqual(by_source.get("Amazon"), "not captured yet")

        summary = prepared["unread_summary"]
        self.assertTrue(summary.startswith("Could not read: "))
        self.assertNotIn("\u2014", summary)
        self.assertNotIn("biz.", summary)
        self.assertNotIn("TimeoutError", summary)
        self.assertNotIn("SO-9999", summary)
        self.assertEqual(prep.provenance_summary(prepared), summary)

    def test_fail_closed_privacy_never_leaks_restricted_contents_or_tracebacks(self):
        self.healthy_view_state()
        self.ask("t-private")
        self.cycle()

        secrets = [
            "CEO Executive Bonus $450,000",
            "alice@secret.example",
            "confidential-layoff-plan-2026",
            "biz.secret_payroll",
            "Traceback",
            "PermissionError",
            "/app/secret.py",
        ]

        def session(owner, oid, extra="", as_of="", prepared=None):
            session_trace.withheld([
                {
                    "source": "mail://confidential-layoff-plan-2026",
                    "why": "pay_or_hr: CEO Executive Bonus $450,000 for alice@secret.example",
                    "title": "CEO Executive Bonus $450,000",
                    "body": "Confidential layoff plan 2026",
                },
                {
                    "why": "excluded_in_thread",
                    "chars": 340,
                },
            ])
            session_trace.note(
                "0.04",
                [
                    {
                        "tool": "plx.sql",
                        "arguments": {"q": "SELECT * FROM biz.secret_payroll WHERE email='alice@secret.example'"},
                        "failed": True,
                        "error": (
                            "Traceback (most recent call last):\n"
                            "  File '/app/secret.py', line 42\n"
                            "PermissionError: Access denied to 'CEO Executive Bonus $450,000'"
                        ),
                    }
                ],
            )
            return REPLY

        prep.serve(
            self.pg, [ANN], session, _inputs(), _nearest,
            screen=lambda t: None, model="stand-in",
        )
        prepared = self.prepared_of("t-private")
        entries = prepared["unread_sources"]
        self.assertTrue(len(entries) >= 2)

        serialized = repr(entries) + "\n" + prepared["unread_summary"] + "\n" + prep.provenance_summary(prepared)
        for secret in secrets:
            self.assertNotIn(secret, serialized, f"Leaked sensitive token: {secret}")
        self.assertNotIn("\u2014", serialized)
        # Verify safe plain-English reasons were still preserved
        reasons = {e["reason"] for e in entries}
        self.assertIn("withheld as a personal, pay, or HR record", reasons)
        self.assertIn("withheld because it involves an excluded person", reasons)
        self.assertIn("access denied", reasons)

    def test_failed_notes_read_records_personal_notes_unavailable_without_crashing(self):
        self.healthy_view_state()
        self.ask("t-notes")
        self.cycle()

        def broken_notes(owner):
            raise RuntimeError("sqlite3.OperationalError: database is locked at /secret/path.db")

        out = prep.serve(
            self.pg,
            [ANN],
            lambda *a, **k: REPLY,
            _inputs(),
            _nearest,
            screen=lambda t: None,
            notes_of=broken_notes,
            model="stand-in",
        )
        self.assertEqual(out["drafted"], 1)
        prepared = self.prepared_of("t-notes")
        self.assertIn({"source": "Personal notes", "reason": "unavailable"}, prepared["unread_sources"])
        self.assertNotIn("sqlite3", prepared["unread_summary"])
        self.assertNotIn("/secret/path.db", prepared["unread_summary"])

    def test_worker_outcome_always_persists_unread_sources_list(self):
        matter = {"oid": "o1", "kind": "conversation", "matter_id": "m1", "source": "emails"}
        drafted = aw.outcome(REPLY, matter)
        self.assertEqual(drafted["plan"]["prepared"]["unread_sources"], [])

        asked = aw.outcome([{"kind": "your_call", "ask": "Approve?", "options": []}], matter)
        self.assertEqual(asked["plan"]["prepared"]["unread_sources"], [])

        nothing = aw.outcome([{"why": "nothing_to_do"}], matter, {"nothing": "Done in SO-1", "reads": []})
        self.assertEqual(nothing["plan"]["prepared"]["unread_sources"], [])

    def test_plan_b13_matter_inputs_records_all_opened_handles_and_undecided_unread(self):
        from operating import assist_readings as ar

        self.healthy_view_state()
        self.ask("t-b13")
        self.cycle()
        mid, handle, said = self.pg.execute(
            "SELECT m.matter_id, r.handles[1], r.said_at FROM assist.matters m "
            "JOIN assist.readings r ON r.thread_key = 't-b13'"
        ).fetchone()
        oid = self.pg.execute(
            "SELECT object_ids[1] FROM biz.emails WHERE handle = %s", (handle,)
        ).fetchone()[0]
        before = said - dt.timedelta(days=2)
        rows = {
            ("emails", "t-b13"): [
                ("k0", said, "x", handle, oid),
                ("k1", before, "bob@ours.example", "h-shown", "o1"),
                ("k2", before - dt.timedelta(days=1), "who@outside.example", "h-truncated", "o2"),
            ]
        }
        words = {
            (handle, oid): ("Please check order 100", ""),
            ("h-shown", "o1"): ("Short recent note", ""),
            ("h-truncated", "o2"): ("Very old message that does not fit in the tight window " * 4, ""),
        }
        # Tight window leaves h-truncated out of text, yet PLAN-B13 requires it in opened_handles + note_opened.
        with mock.patch.object(plan, "WINDOW_CHARS", 60), \
                mock.patch.object(ar, "_covered", lambda pg, env: 7), \
                mock.patch.object(ar, "thread_rows", lambda pg, s, page, covered: rows), \
                mock.patch.object(ar, "message_texts", lambda pg, pairs: words), \
                snapshot_reads(None):
            got = plan.matter_inputs(self.pg, [{"matter_id": mid}])[mid]
            snap_opened = opened_in(current_snapshot())

        self.assertIn("older messages of the thread are not shown here", got["text"])
        for h in (handle, "h-shown", "h-truncated"):
            self.assertIn(h, got["opened_handles"])
            self.assertIn(h, snap_opened)
        self.assertEqual(got["unread_sources"], [])

        # When exclusion is undecided, matter_inputs records it in unread_sources and serve() persists it.
        with mock.patch.object(ar, "_covered", lambda pg, env: None), \
                mock.patch.object(ar, "message_texts", lambda pg, pairs: words):
            undecided_inputs = plan.matter_inputs(self.pg, [{"matter_id": mid}])
        self.assertEqual(
            undecided_inputs[mid]["unread_sources"],
            [{"source": "Earlier thread messages", "reason": "withheld because privacy exclusions are not decided"}],
        )
        prep.serve(
            self.pg,
            [ANN],
            lambda *a, **k: REPLY,
            lambda ms: undecided_inputs,
            _nearest,
            screen=lambda t: None,
            model="stand-in",
        )
        prepared = self.prepared_of("t-b13")
        self.assertIn(
            {"source": "Earlier thread messages", "reason": "withheld because privacy exclusions are not decided"},
            prepared["unread_sources"],
        )


if __name__ == "__main__":
    unittest.main()
