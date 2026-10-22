"""After the Portal stopped writing FileMaker orders, a ship release reaches its portal order.

plx-customer-portal@38cd3f3: a MAKE order confirmed from then on gets no FileMaker
order_entry (``portal/src/lib/sales-order/service.ts``, 898e084 TASK-2638), and a ship
release is keyed by the portal ``SalesOrder.id`` in ``sales_order_id`` with ``order_ref``
holding the same portal id (``portal/src/lib/ship-release/index.ts``,
``portal/src/lib/ship-release/auto-release.ts``, 8665d4d TASK-2641, 1e40b64 TASK-2400). A
release made before keeps ``order_ref`` = the FileMaker order id. The authority this pins
lives in ``ontology/schema/domains/fulfillment.json`` (``ShipRelease.for_sales_order``)
and ``ontology/schema/domains/orders.json`` (``SalesOrder.same_record_as``): the projection
is built from hand-made capture rows with the real compiled schema.
Evidence label: isolated-fixture.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ontology import identifiers
from ontology.project import CapturedRecord, build_store
from ontology.schema import load_schema

T = "2026-10-12T00:00:00.000000Z"
FM_ORDER = 4321
ROWS = {
    # Confirmed after TASK-2638: no fmId, no FileMaker twin.
    "public.app_sales_order": [
        {"id": "cmportalorder0001", "order_number": "SO-2026-0100", "status": "CONFIRMED",
         "fulfillment_mode": "MAKE", "custom_fields": {}},
        # A FileMaker order the Portal mirrored: its twin is the legacy order the Portal reads.
        {"id": "cmlegacyorder0002", "order_number": "SO100200", "status": "CONFIRMED",
         "fulfillment_mode": "MAKE", "custom_fields": {"fmId": FM_ORDER, "source": "fm-oe-ensure"}},
    ],
    "public.order_entry": [{"id": FM_ORDER, "erpcode": "SO100200", "status": "Open"}],
    "public.app_ship_release": [
        # TASK-2641 / TASK-2400: keyed by the portal order id in both columns.
        {"id": "sr-portal", "release_number": "SR-20261012-0001", "status": "RELEASED",
         "order_ref": "cmportalorder0001", "sales_order_id": "cmportalorder0001"},
        # A legacy release whose FileMaker order id the backfill could not map.
        {"id": "sr-legacy", "release_number": "SR-20261001-0002", "status": "RELEASED",
         "order_ref": str(FM_ORDER), "sales_order_id": None},
    ],
}


class Source:
    source = "portal_api"

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [CapturedRecord("portal_api", rel, json.dumps({"id": row["id"]}), row,
                                     f"api://{rel}/{row['id']}@1", "row",
                                     hashlib.sha256(str(row["id"]).encode()).hexdigest(), T,
                                     None, T, f"api:{rel}")
                      for row in ROWS.get(rel, [])]
                for rel in relations}


def oid(type_id: str, binding: str, key) -> str:
    return f"{type_id}:{binding}:{json.dumps({'id': key}, separators=(',', ':'))}"


class PortalOrderKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        path = Path(cls.temp.name) / "store.sqlite"
        build_store(path, load_schema(), {"portal_api": Source()},
                    normalizer=identifiers.normalize,
                    clock=lambda: datetime(2026, 10, 13, tzinfo=timezone.utc))
        conn = sqlite3.connect(str(path))
        try:
            cls.links = {(row[0], row[1], row[2]): row[3] for row in conn.execute(
                "SELECT link_type, from_object, to_object, tier FROM links")}
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def targets(self, link_type, source):
        return {to: tier for (kind, frm, to), tier in self.links.items()
                if kind == link_type and frm == source}

    def test_a_portal_keyed_release_reaches_its_portal_order_and_no_filemaker_order(self):
        release = oid("ShipRelease", "portal.app_ship_release", "sr-portal")
        self.assertEqual(self.targets("ShipRelease.for_sales_order", release),
                         {oid("SalesOrder", "portal.app_sales_order", "cmportalorder0001"):
                          "verified"})
        # The order has no FileMaker twin to reach it through either.
        portal_order = oid("SalesOrder", "portal.app_sales_order", "cmportalorder0001")
        self.assertEqual(self.targets("SalesOrder.same_record_as", portal_order), {})

    def test_a_legacy_release_still_reaches_the_filemaker_order_the_portal_reads(self):
        fm_order = oid("SalesOrder", "portal.fm_order_entry", FM_ORDER)
        release = oid("ShipRelease", "portal.app_ship_release", "sr-legacy")
        self.assertEqual(self.targets("ShipRelease.for_sales_order", release),
                         {fm_order: "verified"})
        legacy = oid("SalesOrder", "portal.app_sales_order", "cmlegacyorder0002")
        self.assertEqual(self.targets("SalesOrder.same_record_as", legacy),
                         {fm_order: "verified"})


if __name__ == "__main__":
    unittest.main()
