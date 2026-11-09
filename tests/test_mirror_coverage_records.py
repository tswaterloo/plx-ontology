"""The source mirror's coverage quotes records, not mirror rows: an ETL-reinserted
relation's copies (equal but for id, created_at, updated_at) are one record at N=1/10/60
copies, a relation with unique keys counts its keys, an absent row is not counted, and
the total sums records. Fake Workbench transport and temp files; every identifier is
synthetic. Evidence: isolated-fixture."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from integrations.source_mirror import SourceMirror
from integrations.tests.test_source_mirror import ALL, BASE, KEY, OPS, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient

TASKS = "public.app_production_task"     # in ontology.project.ETL_REINSERTED
CUSTOMERS = "public.app_customer"


def task(n: int, mo: str) -> dict:
    return {"id": f"t{n}", "created_at": f"2026-10-{n % 28 + 1:02d}T00:00:00",
            "updated_at": f"2026-11-{n % 28 + 1:02d}T00:00:00", "mo_id": mo, "qty": 3}


class CoverageRecordsTest(unittest.TestCase):
    def coverage(self, tasks: list[dict], customers: list[dict]) -> dict:
        fake = FakeWorkbench()
        fake.catalog = {
            TASKS: {"columns": ["id", "created_at", "updated_at", "mo_id", "qty"],
                    "key": ["id"], "domain": "production"},
            CUSTOMERS: {"columns": ["id", "code"], "key": ["id"], "domain": "customers"}}
        fake.tables = {TASKS: tasks, CUSTOMERS: customers}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mirror = SourceMirror(Path(tmp.name) / "m.sqlite",
                              WorkbenchReadApiClient(BASE, KEY, fetch=fake), ALL,
                              clock=Clock(), retry_delay_seconds=0)
        self.addCleanup(mirror.close)
        mirror.rows(OPS, TASKS)
        mirror.rows(OPS, CUSTOMERS)
        return mirror, fake

    def test_copies_count_once_at_1_10_60(self):
        for copies in (1, 10, 60):
            with self.subTest(copies=copies):
                tasks = [task(n, "mo-1") for n in range(copies)] + [task(900, "mo-2")]
                customers = [{"id": f"c{n}", "code": f"K{n}"} for n in range(copies)]
                mirror, _ = self.coverage(tasks, customers)
                report = mirror.coverage(OPS)
                by = {r["relation"]: r for r in report["relations"]}
                self.assertEqual(
                    {k: by[TASKS][k] for k in ("rows_current", "records", "records_basis")},
                    {"rows_current": copies + 1, "records": 2,
                     "records_basis": "natural_key_less_minted"})
                self.assertEqual((by[CUSTOMERS]["records"], by[CUSTOMERS]["records_basis"]),
                                 (copies, "distinct_key"))
                self.assertEqual(report["records_total"], 2 + copies)
                self.assertEqual(report["rows_current_total"], 2 * copies + 1)

    def test_a_new_capture_recounts_and_absent_rows_are_not_records(self):
        mirror, fake = self.coverage([task(1, "mo-1"), task(2, "mo-1"), task(3, "mo-2")],
                                     [{"id": "c1", "code": "K1"}, {"id": "c2", "code": "K2"}])
        self.assertEqual(mirror.coverage(OPS)["records_total"], 4)
        fake.tables[CUSTOMERS] = [{"id": "c1", "code": "K1"}]
        fake.tables[TASKS] = [task(1, "mo-1"), task(2, "mo-1"), task(4, "mo-3"),
                              task(5, "mo-4")]
        mirror.refresh(OPS, CUSTOMERS, full=True)
        mirror.refresh(OPS, TASKS, full=True)
        by = {r["relation"]: r for r in mirror.coverage(OPS)["relations"]}
        self.assertEqual((by[CUSTOMERS]["records"], by[TASKS]["records"]), (1, 3))


if __name__ == "__main__":
    unittest.main()
