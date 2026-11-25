"""Unit tests for operating.measure_candidates and live governed overlay in operating.measures."""
from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

from operating import measure_candidates as mc
from operating import measures as ms


class _Guard:
    """Deterministic people-exclusion stub for tests."""

    def __init__(
        self,
        *,
        excluded_ids: set[str] | None = None,
        excluded_addrs: set[str] | None = None,
        known_domains: set[str] | None = None,
        unavailable: bool = False,
    ) -> None:
        self.excluded_ids = {x.lower() for x in (excluded_ids or set())}
        self.excluded_addrs = {x.lower() for x in (excluded_addrs or set())}
        self._domains = {d.lower() for d in (known_domains or {"company.example"})}
        self.unavailable = unavailable

    def known_domains(self) -> set[str]:
        return set(self._domains)

    def decides(self, value: str) -> bool:
        low = value.lower()
        return low in self.excluded_addrs or low.endswith("@company.example")

    def is_excluded(self, value: str) -> bool:
        low = value.lower()
        return low in self.excluded_ids or low in self.excluded_addrs


class MeasureCandidatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._prev_env = os.environ.get(mc.ENV)
        os.environ[mc.ENV] = self._tmp.name
        self.addCleanup(self._restore_env)
        self.store = mc.CandidateStore(Path(self._tmp.name))
        self.guard = _Guard(
            excluded_ids={"11111111-1111-1111-1111-111111111111"},
            excluded_addrs={"hidden@company.example"},
        )

    def _restore_env(self) -> None:
        if self._prev_env is None:
            os.environ.pop(mc.ENV, None)
        else:
            os.environ[mc.ENV] = self._prev_env
        mc.clear_memory_overlay()

    def test_capture_deduplicates_by_normalized_sql_and_extracts_metadata(self) -> None:
        sql_a = "SELECT customer, sum(revenue) AS total_rev FROM sales_invoice_lines GROUP BY customer"
        sql_b = "select   CUSTOMER,  SUM(revenue) as total_rev\nfrom sales_invoice_lines\ngroup by customer;"
        t1 = dt.datetime(2026, 11, 24, 12, 0, tzinfo=dt.timezone.utc)
        t2 = dt.datetime(2026, 11, 24, 14, 0, tzinfo=dt.timezone.utc)

        first = self.store.capture(
            sql=sql_a,
            source_kind="accepted_answer",
            question="Revenue by customer?",
            proposer_key="u1",
            proposer_name="Ana",
            markings=["finance"],
            guard=self.guard,
            now=t1,
        )
        second = self.store.capture(
            sql=sql_b,
            source_kind="saved_tile",
            title="Customer revenue",
            proposer_key="u2",
            proposer_name="Ben",
            markings=["pii"],
            guard=self.guard,
            now=t2,
        )

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["usage_count"], 2)
        self.assertEqual(second["first_seen_at"], "2026-11-24T12:00:00Z")
        self.assertEqual(second["last_seen_at"], "2026-11-24T14:00:00Z")
        self.assertEqual(second["views"], ["sales_invoice_lines"])
        self.assertEqual(second["sql_mode"], "structured")
        self.assertEqual(second["proposed_view"], "sales_invoice_lines")
        self.assertEqual(second["proposed_value"], "sum(revenue)")
        self.assertEqual(second["proposed_per"], "revenue_currency")
        self.assertEqual(second["proposed_unit"], "money")
        self.assertEqual(second["proposed_dimensions"], ["customer"])
        self.assertEqual(second["markings"], ["finance", "pii"])

    def test_exclusion_and_secret_masking_on_capture_and_reclean(self) -> None:
        bad_sql = "SELECT count(*) FROM mail_messages WHERE sender = 'hidden@company.example'"
        with self.assertRaises(mc.CandidateError) as caught:
            self.store.capture(sql=bad_sql, guard=self.guard)
        self.assertEqual(caught.exception.code, "excluded_content")

        # Unavailable exclusion fails closed on any email/UUID in SQL or text.
        unavail = _Guard(unavailable=True)
        with self.assertRaises(mc.CandidateError):
            self.store.capture(
                sql="SELECT count(*) FROM mail_messages WHERE sender = 'anyone@company.example'",
                guard=unavail,
            )

        cand = self.store.capture(
            sql="SELECT status, count(*) FROM sales_orders GROUP BY status",
            question="Orders asked by hidden@company.example with token=abc123secret and 11111111-1111-1111-1111-111111111111",
            proposer_name="Ana",
            guard=self.guard,
        )
        self.assertNotIn("hidden@company.example", cand["question"])
        self.assertNotIn("abc123secret", cand["question"])
        self.assertNotIn("11111111-1111-1111-1111-111111111111", cand["question"])
        self.assertIn("[withheld]", cand["question"])
        self.assertIn("[removed]", cand["question"])

        # Re-cleaning when another person is excluded later hides their address too.
        cand2 = self.store.capture(
            sql="SELECT status, count(*) FROM purchase_orders GROUP BY status",
            question="Asked by open@company.example",
            guard=self.guard,
        )
        self.assertIn("open@company.example", cand2["question"])
        stricter = _Guard(excluded_addrs={"open@company.example"})
        recleaned = mc.reclean_candidate(cand2, stricter)
        self.assertNotIn("open@company.example", recleaned["question"])
        self.assertIn("[withheld]", recleaned["question"])

    def test_approve_promotes_structured_candidate_into_live_measures_without_editing_measures_json(self) -> None:
        before_json = ms.CATALOGUE.read_text(encoding="utf-8")
        cand = self.store.capture(
            sql="SELECT customer, sum(revenue) FROM sales_invoice_lines WHERE line_type = 'item' GROUP BY customer",
            source_kind="saved_tile",
            title="Item revenue by customer",
            question="Item revenue by customer",
            guard=self.guard,
        )
        approved = self.store.approve(
            cand["id"],
            reviewer_name="Owner",
            edits={
                "id": "item_revenue_governed",
                "name": "Item revenue",
                "description": "Net item-line revenue on posted sales invoices.",
                "owner": "Finance",
            },
            guard=self.guard,
        )
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["approved_measure_id"], "item_revenue_governed")

        live = ms.measures()
        self.assertIn("item_revenue_governed", live)
        entry = live["item_revenue_governed"]
        self.assertEqual(entry["name"], "Item revenue")
        self.assertEqual(entry["owner"], "Finance")
        self.assertEqual(entry["view"], "sales_invoice_lines")
        self.assertEqual(entry["value"], "sum(revenue)")
        self.assertEqual(entry["per"], "revenue_currency")
        # Statement works immediately through operating.measures!
        sql, cited = ms.statement("item_revenue_governed", by="customer", unchecked=True)
        self.assertIn("sales_invoice_lines", sql)
        self.assertEqual(cited["id"], "item_revenue_governed")
        self.assertEqual(cited["name"], "Item revenue")
        # Listing and prompt lines include it immediately!
        self.assertIn("item_revenue_governed", {m["id"] for m in ms.listing()["measures"]})
        self.assertTrue(any("item_revenue_governed" in line for line in ms.lines()))
        # Git-tracked measures.json is untouched!
        self.assertEqual(ms.CATALOGUE.read_text(encoding="utf-8"), before_json)

    def test_approve_promotes_custom_sql_join_candidate_into_live_measures(self) -> None:
        join_sql = (
            "SELECT o.customer, count(*) AS cnt FROM sales_orders o "
            "JOIN sales_invoices i ON i.customer = o.customer GROUP BY o.customer"
        )
        cand = self.store.capture(
            sql=join_sql,
            source_kind="accepted_answer",
            question="Orders matched to invoices by customer",
            guard=self.guard,
        )
        self.assertEqual(cand["sql_mode"], "custom_sql")
        self.assertEqual(cand["views"], ["sales_orders", "sales_invoices"])

        approved = self.store.approve(
            cand["id"],
            reviewer_name="Owner",
            edits={"id": "orders_invoiced_join", "name": "Orders matched to invoices", "owner": "Sales"},
            guard=self.guard,
        )
        self.assertEqual(approved["approved_measure_id"], "orders_invoiced_join")
        self.assertIn("orders_invoiced_join", ms.measures())
        sql, cited = ms.statement("orders_invoiced_join")
        self.assertEqual(sql, join_sql)
        self.assertEqual(cited["name"], "Orders matched to invoices")

    def test_approve_refuses_built_in_id_collision_and_dismiss_works(self) -> None:
        cand = self.store.capture(
            sql="SELECT status, count(*) FROM sales_orders GROUP BY status",
            title="Revenue",
            guard=self.guard,
        )
        with self.assertRaises(mc.CandidateError) as caught:
            self.store.approve(cand["id"], reviewer_name="Owner", edits={"id": "revenue"}, guard=self.guard)
        self.assertEqual(caught.exception.code, "measure_id_exists")
        self.assertEqual(caught.exception.status, 409)

        dismissed = self.store.dismiss(cand["id"], reviewer_name="Owner", reason="Duplicate of open orders", guard=self.guard)
        self.assertEqual(dismissed["status"], "dismissed")
        self.assertEqual(dismissed["dismiss_reason"], "Duplicate of open orders")
        self.assertEqual(dismissed["reviewed_by"], "Owner")

    def test_approve_refuses_built_in_report_id_collision(self) -> None:
        report_id = next(iter(ms.reports()))
        cand = self.store.capture(
            sql="SELECT status, count(*) FROM sales_orders GROUP BY status",
            title="Report collision",
            guard=self.guard,
        )
        with self.assertRaises(mc.CandidateError) as caught:
            self.store.approve(cand["id"], reviewer_name="Owner", edits={"id": report_id}, guard=self.guard)
        self.assertEqual(caught.exception.code, "measure_id_exists")
        self.assertEqual(caught.exception.status, 409)

    def test_schema_qualified_tables_rejected_at_capture_promote_and_validate(self) -> None:
        bad_queries = (
            "SELECT * FROM biz_person.emails",
            'SELECT * FROM "biz_person"."emails"',
            "SELECT count(*) FROM sales_orders o JOIN biz_person.emails e ON e.sender = o.customer",
            "SELECT count(*) FROM sales_orders WHERE EXISTS (SELECT 1 FROM biz_person.emails)",
            "WITH c AS (SELECT * FROM biz_person.emails) SELECT * FROM c",
            "SELECT biz_person.emails.sender FROM sales_orders",
        )
        for bad_sql in bad_queries:
            with self.assertRaises(mc.CandidateError, msg=bad_sql) as caught:
                self.store.capture(sql=bad_sql, guard=self.guard)
            self.assertEqual(caught.exception.code, "invalid_sql")

        # Valid alias.column, extract(... FROM col), and IS DISTINCT FROM pass.
        good_extract = self.store.capture(
            sql="SELECT extract(epoch FROM o.order_date) FROM sales_orders o WHERE o.status IS DISTINCT FROM 'cancelled'",
            guard=self.guard,
        )
        self.assertEqual(good_extract["views"], ["sales_orders"])

        # Promote-time guard rejects schema-qualified SQL even if injected into store.
        with self.assertRaises(mc.CandidateError) as caught_prom:
            mc._governed_entry_from_candidate(
                {
                    "id": "mc-bad",
                    "sql": "SELECT * FROM biz_person.emails",
                    "sql_mode": "custom_sql",
                    "proposed_id": "bad_schema_ref",
                    "proposed_name": "Bad schema ref",
                    "proposed_description": "Should fail",
                    "proposed_owner": "Company",
                    "views": [],
                },
                {},
                guard=self.guard,
            )
        self.assertEqual(caught_prom.exception.code, "invalid_sql")

        # measures._validate_entry also rejects schema-qualified SQL and bad where clauses.
        with self.assertRaises(Exception):
            ms._validate_entry({
                "id": "bad_sql_entry",
                "name": "Bad",
                "owner": "Company",
                "unit": "count",
                "definition": "Bad",
                "dimensions": [],
                "sql": "SELECT * FROM biz_person.emails",
            })
        with self.assertRaises(Exception):
            ms._validate_entry({
                "id": "bad_where_entry",
                "name": "Bad",
                "owner": "Company",
                "unit": "count",
                "definition": "Bad",
                "view": "sales_orders",
                "value": "count(*)",
                "per": "",
                "date": "",
                "where": "EXISTS (SELECT 1 FROM biz_person.emails)",
                "dimensions": [],
            })

    def test_approve_rejects_malicious_invalid_and_excluded_where(self) -> None:
        cand = self.store.capture(
            sql="SELECT customer, sum(revenue) FROM sales_invoice_lines WHERE line_type = 'item' GROUP BY customer",
            title="Item revenue",
            guard=self.guard,
        )
        for bad_where, expected_code in (
            ("customer = 'hidden@company.example'", "excluded_content"),
            ("line_type = 'item') UNION SELECT * FROM emails WHERE ('1'='1", "invalid_sql"),
            ("line_type = 'item' UNION ALL SELECT 1", "invalid_sql"),
            ("EXISTS (SELECT 1 FROM biz_person.emails)", "invalid_sql"),
            ("pg_sleep(1) IS NOT NULL", "invalid_sql"),
            ("line_type = 'item'; DROP TABLE sales_orders", "invalid_sql"),
        ):
            with self.assertRaises(mc.CandidateError, msg=bad_where) as caught:
                self.store.approve(
                    cand["id"],
                    reviewer_name="Owner",
                    edits={"id": "bad_where_test", "where": bad_where},
                    guard=self.guard,
                )
            self.assertEqual(caught.exception.code, expected_code, bad_where)

        # Candidate remained pending and nothing was written to governed_overlay.json.
        self.assertEqual(self.store.get(cand["id"])["status"], "pending")
        self.assertEqual(mc.governed_overlay(), [])

    def test_dedup_does_not_overwrite_first_proposer_metadata(self) -> None:
        sql = "SELECT customer, sum(revenue) FROM sales_invoice_lines GROUP BY customer"
        first = self.store.capture(
            sql=sql,
            name="Original revenue",
            description="Original description",
            owner="Finance",
            proposer_key="user-a",
            proposer_name="Ana",
            markings=["finance"],
            guard=self.guard,
        )
        second = self.store.capture(
            sql=sql,
            name="Hijacked name",
            description="Hijacked description",
            owner="Other",
            proposer_key="user-b",
            proposer_name="Ben",
            markings=["pii"],
            guard=self.guard,
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["usage_count"], 2)
        self.assertEqual(second["proposer_key"], "user-a")
        self.assertEqual(second["proposer_name"], "Ana")
        self.assertEqual(second["proposed_name"], "Original revenue")
        self.assertEqual(second["proposed_description"], "Original description")
        self.assertEqual(second["proposed_owner"], "Finance")
        self.assertEqual(second["markings"], ["finance", "pii"])

    def test_readonly_directory_tolerated_and_flock_serializes_access(self) -> None:
        import fcntl
        from unittest.mock import patch

        ro_parent = Path(self._tmp.name) / "readonly_parent"
        ro_parent.mkdir(mode=0o555)
        self.addCleanup(lambda: ro_parent.chmod(0o755))
        ro_store = mc.CandidateStore(ro_parent / "measure-candidates")
        rows, counts = ro_store.list()
        self.assertEqual(rows, [])
        self.assertEqual(counts["pending"], 0)
        self.assertIsNone(ro_store.get("mc-missing"))

        self.store.capture(sql="SELECT count(*) FROM sales_orders", guard=self.guard)
        lock_file = Path(self._tmp.name) / "candidates.lock"
        self.assertTrue(lock_file.exists())
        fd = os.open(lock_file, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(mc, "LOCK_WAIT", 0.1):
                with self.assertRaises(mc.CandidateError) as caught:
                    self.store.capture(sql="SELECT count(*) FROM purchase_orders", guard=self.guard)
            self.assertEqual(caught.exception.code, "busy")
            self.assertEqual(caught.exception.status, 503)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
