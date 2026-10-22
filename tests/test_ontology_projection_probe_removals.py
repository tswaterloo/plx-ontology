"""The ontology.projection live check reports source removals reaching the projection
(ING-09, REC-04): counts only, by the existing removal rule and withdrawal path.

A fake Workbench transport serves Portal sales orders and customers; N sales orders are
soft-deleted (``deleted_at``) after a person accepted one order's customer link. The probe
(``scripts.ontology_projection_probe.projection_probe``) over that mirror reports one
``source_soft_delete`` stretch per soft-deleted row, each record hidden from the mirror's
``rows`` and ``resolve`` yet readable through its citation with the label, the review
withdrawn ``source_removed`` and its link ended at the removal; a reader the audience
withholds the orders from gets none of them back. The live spec's reading of these
counts is tested in ``tests.test_ontology_projection_probe`` (RemovalSpecTest). Temp
files, synthetic identifiers.
Evidence: injected-transport.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology import identifiers, reviews
from ontology import schema as schema_loader
from operating.host import DEFAULT_PRINCIPAL
from scripts import ontology_projection_probe as probe

CUSTOMERS, ORDERS = "public.app_customer", "public.app_sales_order"
LINK = "SalesOrder.ordered_by"
SINCE = {"column": "updated_at", "mode": "timestamp-utc"}
T0 = "2026-09-29T09:00:00"
DELETED = "2026-10-01T00:00:00"


def workbench(count):
    fake = FakeWorkbench()
    fake.catalog = {
        CUSTOMERS: {"columns": ["id", "code", "name", "deleted_at", "updated_at"],
                    "key": ["id"], "domain": "customers", "since": SINCE},
        ORDERS: {"columns": ["id", "order_number", "customer_id", "deleted_at", "updated_at"],
                 "key": ["id"], "domain": "orders", "since": SINCE}}
    fake.tables = {
        CUSTOMERS: [{"id": "cu1", "code": "K-1", "name": "Customer One", "deleted_at": None,
                     "updated_at": T0}],
        ORDERS: [{"id": f"so{i:02d}", "order_number": f"SO-SECRET-{i:02d}",
                  "customer_id": "cu1", "deleted_at": None, "updated_at": T0}
                 for i in range(count + 2)]}   # two orders are never deleted
    return fake


def probe_after_removals(root, count):
    """The probe's report before and after ``count`` sales orders are soft-deleted, a
    person having accepted the first one's customer link in between (``root``: a fresh
    directory)."""
    schema = schema_loader.load_schema()
    fake, clock = workbench(count), Clock()
    client = WorkbenchReadApiClient(BASE, KEY, fetch=fake)

    def run(name):
        (root / name).mkdir(exist_ok=True)
        return probe.projection_probe(
            client, str(root / "mirror.sqlite"), str(root / name / "store.sqlite"),
            schema=schema, normalizer=identifiers.normalize, claim_sample=20,
            bound=[CUSTOMERS, ORDERS], clock=clock)

    before = run("before")
    with sqlite3.connect(root / "before" / "store.sqlite") as conn:
        ids = dict(conn.execute("SELECT object_type, MIN(object_id) FROM objects "
                                "GROUP BY object_type").fetchall())
        handle, digest = conn.execute(
            "SELECT l1_handle, digest FROM object_versions WHERE object_id = ? "
            "AND superseded_at IS NULL", (ids["SalesOrder"],)).fetchone()
    state = probe.served_state_path(str(root / "after" / "store.sqlite"))
    reviews.ReviewStore(reviews.store_path(state),
                        clock=lambda: datetime(2026, 9, 30, tzinfo=timezone.utc)).record(
        tenant=probe.TENANT, author={"principal": "probe:reviewer"}, decision="accept_link",
        subject=ids["SalesOrder"], target=ids["Customer"], link_type=LINK,
        evidence=[{"object_id": ids["SalesOrder"], "l1_handle": handle, "digest": digest,
                   "field_path": "row.customer_id"}])
    for i in range(count):
        fake.update(ORDERS, f"so{i:02d}", deleted_at=DELETED, updated_at=DELETED)
    clock.now = datetime(2026, 10, 1, 6, tzinfo=timezone.utc)
    return before, run("after")


class ProbeRemovalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_soft_deleted_rows_reach_the_projection_reviews_and_links_at_1_10_60(self):
        for count in (1, 10, 60):
            with self.subTest(deleted=count):
                root = self.root / f"n{count}"
                root.mkdir()
                started = time.monotonic()
                before, after = probe_after_removals(root, count)
                self.assertLess(time.monotonic() - started, 90)
                self.assertEqual((before["removal_stretches"], before["removed_objects_total"],
                                  before["removals_reach_projection"]), ({}, 0, True))
                self.assertEqual(after["removal_stretches"], {"source_soft_delete": count})
                self.assertEqual(after["removed_objects"], {"source_soft_delete": count})
                self.assertEqual(after["removal_reader"], {
                    "sampled": count, "withheld": 0, "withheld_leaks": 0, "not_mirrored": 0,
                    "hidden_from_rows": count, "resolve_probed": count,
                    "hidden_from_resolve": count, "readable_with_label": count})
                self.assertEqual(after["removal_reviews"], {
                    "checked": 1, "citing_removed": 1, "withdrawn_source_removed": 1})
                self.assertEqual(after["removal_links_ended"].get("reviewed"), 1)
                self.assertEqual(after["removal_links_open"].get("reviewed", 0), 0)
                self.assertEqual((after["mirror_soft_deleted_bound"],
                                  after["removals_reach_projection"]), (count, True))
                text = json.dumps(after)
                for secret in ("api://", "SO-SECRET", "Customer One", '"so0'):
                    self.assertNotIn(secret, text)   # counts only

    def test_a_reader_withheld_the_orders_gets_no_removed_record_back(self):
        fake, clock = workbench(5), Clock()
        client = WorkbenchReadApiClient(BASE, KEY, fetch=fake)
        for i in range(5):
            fake.update(ORDERS, f"so{i:02d}", deleted_at=DELETED, updated_at=DELETED)
        clock.now += timedelta(days=3)
        audience = self.root / "audience.json"
        audience.write_text(json.dumps({
            "schema": "plx-source-mirror-audience/v1", "tenant": probe.TENANT,
            "audiences": {"public.*": [DEFAULT_PRINCIPAL], ORDERS: []}}))
        result = probe.projection_probe(
            client, str(self.root / "mirror.sqlite"), str(self.root / "narrow.sqlite"),
            schema=schema_loader.load_schema(), normalizer=identifiers.normalize,
            claim_sample=20, bound=[CUSTOMERS, ORDERS], clock=clock,
            mirror_audience=str(audience))
        self.assertEqual(result["removal_stretches"], {"source_soft_delete": 5})
        self.assertEqual(result["removal_reader"]["withheld"], 5)
        self.assertEqual(result["removal_reader"]["withheld_leaks"], 0)
        self.assertEqual(result["removal_reader"]["readable_with_label"], 0)
        self.assertTrue(result["removals_reach_projection"])


if __name__ == "__main__":
    unittest.main()
