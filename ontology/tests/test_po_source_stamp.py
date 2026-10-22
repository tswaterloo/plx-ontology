"""A QA smoke PO keeps its staging lineage after the silver ETL re-stamps its source.

plx-customer-portal@6e76a5b: ``portal/scripts/qa-po-receive-smoke.ts`` (lines 111-116)
writes customFields {qaSmoke: true, source 'qa-po-receive-smoke'}; the silver ETL merge
(``portal/scripts/etl-silver-to-gold.ts`` lines 776-830) overwrites ``source`` with
'etl-silver-to-gold' and keeps ``qaSmoke``. The projection is built from hand-made capture
rows with the real compiled schema. Evidence label: isolated-fixture.
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
from ontology.project import CapturedRecord, build_store, object_id_of
from ontology.schema import load_schema

T = "2026-10-01T00:00:00.000000Z"
REL = "public.app_purchase_order"
ROWS = [
    {"id": "po-qa", "po_number": "PO-QA", "currency": "CAD",
     "custom_fields": {"qaSmoke": True, "qaTag": "qa", "source": "etl-silver-to-gold",
                       "fmGeneratedPoId": "1", "fmPoNumber": "PO-QA"}},
    {"id": "po-etl", "po_number": "PO-ETL", "currency": "CAD",
     "custom_fields": {"source": "etl-silver-to-gold", "fmGeneratedPoId": "2",
                       "fmPoNumber": "PO-ETL"}},
]


class Source:
    source = "portal_api"

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [CapturedRecord("portal_api", rel, json.dumps({"id": row["id"]}), row,
                                     f"api://{rel}/{row['id']}@1", "row",
                                     hashlib.sha256(row["id"].encode()).hexdigest(), T,
                                     None, T, f"api:{rel}")
                      for row in (ROWS if rel == REL else [])]
                for rel in relations}


class QaSmokeLineageTests(unittest.TestCase):
    def test_qa_smoke_reads_as_staging_and_a_real_etl_row_as_fm(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "store.sqlite"
            build_store(path, load_schema(), {"portal_api": Source()},
                        normalizer=identifiers.normalize,
                        clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))
            conn = sqlite3.connect(str(path))
            try:
                def lineage(native):
                    oid = object_id_of("PurchaseOrder", "portal.app_purchase_order",
                                       {"id": native})
                    return {r[0] for r in conn.execute(
                        "SELECT lineage FROM object_versions WHERE object_id = ?", (oid,))}
                self.assertEqual(lineage("po-qa"), {"gold_native_staging"})
                self.assertEqual(lineage("po-etl"), {"gold_etl_from_fm"})
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
