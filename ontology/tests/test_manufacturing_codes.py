"""Manufacturing-order and assembly-order codes through the projection and plx.find.

FM AO names parse as ``ao_code``; every Portal manufacturing-order code form keeps its own
declared ``mo_code`` form (MO-{base36}, MO-FM-{id}, the FM name the ETL copies, each
optionally UAT-prefixed) and a prefixed number is never the unprefixed one. A code that
parses nowhere would drop its value and make an AO search report unknown, not absent.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ontology import identifiers
from ontology.core import Actor
from ontology.project import CapturedRecord, build_store
from ontology.schema import load_schema
from operating import ontology_tools as ot

T = "2026-10-01T00:00:00+00:00"
MO_CODES = ["AO-26005B", "MO-LX8K2Z", "MO-FM-10718", "UAT-MO-LX8K2Z"]
ROWS = {"public.app_manufacturing_order": [{"id": f"m{i}", "mo_code": code}
                                           for i, code in enumerate(MO_CODES)],
        "public.assembly_order": [{"id": 1, "nameamo_c": "AO-26005B"}]}
# plx-customer-portal@acc0c53 portal/src/lib/mrp/test-receiving/batch-ids.ts:29-31: a
# staging batch that uses a test lot takes an id >= 9000000 and a TEST-BC- number;
# silver.batches copies every public.batch row, so the gold ETL carries it too.
BATCH_ROWS = {"public.batch": [{"id": 12000, "namebatch_c": "BC-26119A"},
                               {"id": 9000001, "namebatch_c": "TEST-BC-26278A"}],
              "public.app_batch_record": [{"id": "r1", "batch_name": "BC-26119A"},
                                          {"id": "r2", "batch_name": "TEST-BC-26278A"}]}


class Source:
    source = "portal_api"

    def __init__(self, rows=ROWS):
        self.rows = rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        return {rel: [CapturedRecord("portal_api", rel, json.dumps({"id": row["id"]}), row,
                                     f"api://{rel}/{row['id']}@X", "row", f"d{row['id']}", T,
                                     None, T, f"api:{rel}") for row in self.rows.get(rel, [])]
                for rel in relations}


class ManufacturingCodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load_schema()
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name) / "store.sqlite"
        cls.report = build_store(
            cls.path, cls.schema, {"portal_api": Source()}, normalizer=identifiers.normalize,
            clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))
        cls.conn = sqlite3.connect(str(cls.path), check_same_thread=False)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.temp.cleanup()

    def test_every_real_code_form_is_kept_under_its_own_kind(self):
        idents = set(tuple(r) for r in self.conn.execute(
            "SELECT identifier_kind, normalized FROM identifiers "
            "WHERE object_id LIKE 'AssemblyOrder%'"))
        self.assertEqual(idents, {("mo_code", code) for code in MO_CODES}
                         | {("ao_code", "AO-26005B")})
        gaps = [g for g in self.report.get("gaps", ())
                if str(g.get("code", "")).startswith("parse_")]
        self.assertEqual(gaps, [])

    def test_a_prefixed_number_is_not_the_unprefixed_one(self):
        self.assertNotEqual(identifiers.normalize("mo_code", "UAT-MO-LX8K2Z"),
                            identifiers.normalize("mo_code", "MO-LX8K2Z"))

    def test_an_ao_search_finds_the_code_and_reports_absence_for_another(self):
        policy = ot.ReadPolicy(tenant="plx", audiences={"api:*": {"ops"}})
        tools = ot.OntologyTools(
            ot.StoreReader(self.conn), policy, schema=ot.SchemaView.from_compiled(self.schema),
            grammar=identifiers, item_gate=lambda actor, handle: True)
        actor = Actor("plx", frozenset({"ops"}))
        found = tools.call("plx.find", {"query": "AO-26005B"}, actor)
        self.assertTrue({"ao_code", "mo_code"}
                        <= {c["matched"]["property"] for c in found["candidates"]})
        absent = tools.call("plx.find", {"query": "AO-99999Z"}, actor)
        self.assertEqual(absent["candidates"], [])
        # Every AO carrier was searched and none missed: a carrier whose values failed to
        # parse would be listed as not covered (unknown), never as searched.
        [gap] = [g for g in absent["gaps"] if g["subject"] == "query:AO-99999Z"]
        self.assertTrue({"AssemblyOrder.ao_code", "AssemblyOrder.mo_code"}
                        <= set(gap["searched"]["scopes"]))
        self.assertNotIn("did not cover every identifier carrier", gap.get("detail", ""))


class TestBatchLineageTests(unittest.TestCase):
    def test_a_portal_test_batch_is_staging_data_and_never_an_fm_batch_code(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "store.sqlite"
            build_store(path, load_schema(), {"portal_api": Source(BATCH_ROWS)},
                        normalizer=identifiers.normalize,
                        clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc))
            conn = sqlite3.connect(str(path))
            try:
                lineage = dict(conn.execute("SELECT object_id, lineage FROM object_versions"))
                codes = {row[0] for row in conn.execute(
                    "SELECT normalized FROM identifiers WHERE identifier_kind = 'batch_code'")}
            finally:
                conn.close()
        self.assertEqual(lineage, {
            'Batch:portal.fm_batch:{"id":12000}': "fm_bronze",
            'Batch:portal.fm_batch:{"id":9000001}': "gold_native_staging",
            'Batch:portal.app_batch_record:{"id":"r1"}': "gold_etl_from_fm",
            'Batch:portal.app_batch_record:{"id":"r2"}': "gold_native_staging"})
        self.assertEqual(codes, {"BC-26119A"})


if __name__ == "__main__":
    unittest.main()
