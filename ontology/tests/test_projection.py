"""Projector + store: a fixture mirror projected through a fixture schema.

The L1 input is a real ``integrations.source_mirror.SourceMirror`` filled over an
injected Workbench transport (no network) in two captures; the schema is a small
multi-relation C0 domain file validated against ``_format.schema.json`` (and compiled
by the loader when ``ontology.schema`` is importable). Values are checked with the
real claim gate (``operating/claim_checks.check_items``) against the mirror's own
``resolve_citation``.
"""

from __future__ import annotations

import collections
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import unittest.mock
from urllib.parse import parse_qs, urlsplit

from integrations.source_mirror import AudienceMap, SourceMirror
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology import json_contract, store_ddl
from ontology.project import (MirrorCaptures, ProjectionError, Projector, _parse,
                              build_store)
from ontology.store import (ProjectionStore, ReadPolicy, StoreBuilder, StoreError,
                            content_digest)
from ontology.types import Actor
from operating import claim_checks

KEY = "plxwb_" + "p" * 40
BASE = "https://api.example.test"
FORMAT = Path(__file__).resolve().parents[1] / "schema" / "_format.schema.json"

CATALOG = {
    "public.app_purchase_order": ["id", "po_number", "supplier_id", "total_cost", "currency",
                                  "status", "order_date", "created_at", "custom_fields"],
    "public.generated_purchase_order": ["id", "ponumber", "podate", "curr"],
    "public.app_supplier": ["id", "code", "name"],
    "public.app_purchase_order_line": ["id", "purchase_order_id", "quantity", "uom",
                                       "unit_cost"],
    "public.app_receipt": ["id", "receipt_number", "po_reference", "received_at"],
    "public.app_receipt_line": ["id", "receipt_id", "line_id"],
}

TABLES = {
    "public.app_purchase_order": [
        {"id": "po-a", "po_number": "PO 1001", "supplier_id": "s3",
         "total_cost": "35.099999999999994", "currency": "USD", "status": "OPEN",
         "order_date": "2026-09-01", "created_at": "2026-09-01T10:00:00Z",
         "custom_fields": {"source": "etl", "fmGeneratedPoId": 1}},
        {"id": "po-b", "po_number": "PO-2002", "supplier_id": "s1", "total_cost": "120.00",
         "currency": "cad", "status": "OPEN", "order_date": "2026-09-05",
         "created_at": "2026-09-05T08:30:00+02:00", "custom_fields": {"source": "portal"}},
        {"id": "po-c", "po_number": "PO-3003", "supplier_id": "s3", "total_cost": 9.5,
         "currency": "USD", "status": "DRAFT", "order_date": None,
         "created_at": "2026-09-07T00:00:00Z",
         "custom_fields": {"source": "etl", "fmGeneratedPoId": "3"}},
        {"id": "po-d", "po_number": "PO1001", "supplier_id": "s3", "total_cost": "1",
         "currency": "USD", "status": "OPEN", "order_date": "2026-09-09",
         "created_at": "not a time", "custom_fields": None},
        {"id": "po-e", "po_number": "PO1001", "supplier_id": "s9", "total_cost": "2",
         "currency": "EUR", "status": "OPEN", "order_date": "2026-09-11",
         "created_at": "2026-09-11T00:00:00Z", "custom_fields": {"source": "portal"}},
    ],
    "public.generated_purchase_order": [
        {"id": 1, "ponumber": "PO1001", "podate": "20260902", "curr": "USD$"},
        {"id": 3, "ponumber": "PO-3999", "podate": "20260910"},
        {"id": 4, "ponumber": "UAT-4", "podate": "0"},
    ],
    "public.app_supplier": [
        {"id": "s1", "code": "ACME", "name": "Acme One"},
        {"id": "s2", "code": "ACME", "name": "Acme Two"},
        {"id": "s3", "code": "BOLT", "name": " Bolt Ltd "},
    ],
    "public.app_purchase_order_line": [
        {"id": "l1", "purchase_order_id": "po-a", "quantity": "1509", "uom": "EA",
         "unit_cost": "2.50"},
        {"id": "l2", "purchase_order_id": "po-b", "quantity": "12.0", "uom": None,
         "unit_cost": "3"},
        {"id": "l3", "purchase_order_id": "po-zz", "quantity": "5", "uom": "KG",
         "unit_cost": "1"},
    ],
    "public.app_receipt": [
        {"id": "r1", "receipt_number": "rc-1", "po_reference": "PO1001",
         "received_at": "2026-09-20T09:00:00Z"},
        {"id": "r2", "receipt_number": "rc-2", "po_reference": " PO-2002 ",
         "received_at": "2026-09-21T09:00:00Z"},
    ],
    "public.app_receipt_line": [
        {"id": "rl1", "receipt_id": "r1", "line_id": "l1"},
    ],
}

OPS = Actor("plx", frozenset({"staff:operations"}))
FINANCE = Actor("plx", frozenset({"staff:operations", "staff:finance"}))
OUTSIDER = Actor("plx", frozenset({"staff:marketing"}))
PO_ONLY = Actor("plx", frozenset({"staff:purchasing"}))
AUDIENCE = {"public.*": ["staff:operations", "staff:finance"],
            "public.app_purchase_order": ["staff:operations", "staff:finance",
                                          "staff:purchasing"]}
MARKINGS = {"finance": ["staff:finance"]}
T1 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
T2 = T1 + timedelta(hours=1)


class FakeWorkbench:
    """Health, catalog and keyset row pages of the Workbench API (full reload only)."""

    def __init__(self):
        self.tables = copy.deepcopy(TABLES)

    def __call__(self, url, headers, *, max_bytes):
        parts = urlsplit(url)
        if parts.path == "/v1/health":
            return {"ok": True, "recovery": True, "replayLagSeconds": 1, "unavailable": [],
                    "missingColumns": [], "generatedAt": "2026-09-29T12:00:00Z"}
        if parts.path == "/v1/relations":
            return {"relations": [
                {"name": name, "schema": name.split(".")[0], "relation": name.split(".")[1],
                 "unavailable": None, "columns": columns, "key": ["id"],
                 "domain": "test", "since": None} for name, columns in CATALOG.items()]}
        name = re.fullmatch(r"/v1/relations/([a-z0-9_.]+)/rows", parts.path)[1]
        query = parse_qs(parts.query)
        wanted = set(query["columns"][0].split(",")) | {"id"}
        rows = sorted(self.tables[name], key=lambda row: str(row["id"]))
        if "cursor" in query:
            after = json.loads(query["cursor"][0])[0]
            rows = [row for row in rows if str(row["id"]) > str(after)]
        limit = int(query["limit"][0])
        page, more = rows[:limit], len(rows) > limit
        return {"relation": name, "columns": [c for c in CATALOG[name] if c in wanted],
                "rows": [{c: row.get(c) for c in CATALOG[name] if c in wanted}
                         for row in page],
                "nextCursor": json.dumps([page[-1]["id"]]) if more else None}

    def set(self, name, key, **changes):
        next(row for row in self.tables[name] if row["id"] == key).update(changes)


# ----------------------------------------------------------------------- schema

EVIDENCE = [{"kind": "ref", "id": "fixture"}]


def _binding(bid, relation, lineage, key=("id",)):
    return {"id": bid, "source": "portal_api", "relation": relation, "key": list(key),
            "lineage": lineage, "evidence": EVIDENCE}


def _src(binding, path, parser="trim", **extra):
    return {"binding": binding, "path": path, "parser": parser, **extra}


def _pair(a, b, compare, kind=None):
    pair = {"from": a, "to": b, "compare": compare}
    if kind:
        pair["identifier_kind"] = kind
    return pair


def _link(lid, to, cardinality, bindings, reverse):
    frm, name = lid.split(".")
    return {"id": lid, "from": frm, "to": to, "name": name, "reverse_name": reverse,
            "cardinality": cardinality, "description": "fixture link",
            "bindings": [{**b, "evidence": EVIDENCE} for b in bindings]}


FM_FIRST = {"precedence": [{"lineage": ["fm_bronze"], "evidence": EVIDENCE},
                           {"lineage": ["gold_etl_from_fm"], "evidence": EVIDENCE}]}
DOMAIN = {
    "format": "plx-ontology-domain/v1", "domain": "fixture_purchasing", "origin": "curated",
    "description": "Projector test fixture (not a real domain).",
    "evidence_catalog": {"fixture": {"kind": "human", "who": "projector test",
                                     "when": "2026-10-01", "note": "fixture schema"}},
    "object_types": [
        {"id": "PurchaseOrder", "description": "PO per source record.", "anchor": True,
         "availability": {"state": "served"}, "evidence": EVIDENCE,
         "bindings": [
             _binding("t.gold_po", "public.app_purchase_order", {
                 "default": "gold_native_staging",
                 "rules": [{"when": [{"path": "custom_fields.source", "op": "eq",
                                      "value": "etl"}],
                            "lineage": "gold_etl_from_fm", "evidence": EVIDENCE}]}),
             _binding("t.fm_po", "public.generated_purchase_order", {
                 "default": "fm_bronze",
                 "rules": [{"when": [{"path": "ponumber", "op": "prefix", "value": "UAT-"}],
                            "lineage": "gold_native_staging", "evidence": EVIDENCE}]})],
         "properties": [
             {"name": "po_number", "value_type": "identifier", "identifier_kind": "petra_po",
              "sources": [_src("t.fm_po", "ponumber", "identifier"),
                          _src("t.gold_po", "po_number", "identifier")],
              "authority": FM_FIRST},
             {"name": "total_cost", "value_type": "money", "currency_from": "currency",
              "markings": ["finance"],
              "sources": [_src("t.gold_po", "total_cost", "decimal_text", tax_basis="unknown")]},
             {"name": "status", "value_type": "text",
              "sources": [_src("t.gold_po", "status")]},
             {"name": "order_date", "value_type": "date",
              "sources": [_src("t.fm_po", "podate", "yyyymmdd_date"),
                          _src("t.gold_po", "order_date", "iso_date")],
              "authority": FM_FIRST},
             {"name": "created_at", "value_type": "timestamp",
              "sources": [_src("t.gold_po", "created_at", "iso_timestamp")]}],
         "identity_keys": [{"name": "po_number", "properties": ["po_number"],
                            "identifier_kind": "petra_po", "tier": "lead", "unique": False,
                            "evidence": EVIDENCE}],
         "display": ["po_number"]},
        {"id": "Supplier", "description": "Supplier.", "availability": {"state": "served"},
         "evidence": EVIDENCE,
         "bindings": [_binding("t.supplier", "public.app_supplier",
                               {"default": "gold_native_staging"}, key=("code",))],
         "properties": [
             {"name": "code", "value_type": "identifier", "identifier_kind": "supplier_code",
              "sources": [_src("t.supplier", "code", "identifier")]},
             {"name": "name", "value_type": "text", "markings": ["pii"],
              "sources": [_src("t.supplier", "name")]}],
         "identity_keys": [{"name": "code", "properties": ["code"], "tier": "verified",
                            "unique": True, "evidence": EVIDENCE}],
         "display": ["name"]},
        {"id": "PurchaseOrderLine", "description": "PO line.",
         "availability": {"state": "served"}, "evidence": EVIDENCE,
         "bindings": [_binding("t.gold_line", "public.app_purchase_order_line", {
             "default": "gold_native_staging", "inherit": "PurchaseOrderLine.of_order",
             "rules": [{"when": [{"path": "custom_fields.source", "op": "eq",
                                  "value": "portal",
                                  "via_link": "PurchaseOrderLine.of_order"}],
                        "lineage": "gold_native_staging", "evidence": EVIDENCE}]})],
         "properties": [
             {"name": "quantity", "value_type": "quantity", "quantity_role": "ordered",
              "sources": [_src("t.gold_line", "quantity", "decimal_text", unit_from="uom")]},
             {"name": "unit_cost", "value_type": "money", "markings": ["finance"],
              "currency_from": {"via_link": "PurchaseOrderLine.of_order", "path": "currency"},
              "sources": [_src("t.gold_line", "unit_cost", "decimal_text",
                                       tax_basis="unknown")]}]},
        {"id": "Receipt", "description": "Receipt.", "anchor": True,
         "availability": {"state": "served"}, "evidence": EVIDENCE,
         "bindings": [_binding("t.receipt", "public.app_receipt",
                               {"default": "gold_native_staging"})],
         "properties": [
             {"name": "receipt_number", "value_type": "identifier",
              "identifier_kind": "receipt_no",
              "sources": [_src("t.receipt", "receipt_number", "identifier")]},
             {"name": "po_reference", "value_type": "text",
              "sources": [_src("t.receipt", "po_reference")]}]},
        {"id": "ReceiptAllocation", "description": "Receipt-to-line join record.",
         "availability": {"state": "served"}, "evidence": EVIDENCE,
         "bindings": [_binding("t.receipt_line", "public.app_receipt_line",
                               {"default": "gold_native_staging"})],
         "properties": [{"name": "line_ref", "value_type": "text",
                         "sources": [_src("t.receipt_line", "line_id")]}]},
    ],
    "link_types": [
        _link("PurchaseOrder.same_record_as", "PurchaseOrder", "1:1", [
            {"kind": "same_record", "tier_ceiling": "verified", "from_binding": "t.gold_po",
             "to_binding": "t.fm_po",
             "pairs": [_pair("custom_fields.fmGeneratedPoId", "id", "integer_id"),
                       _pair("po_number", "ponumber", "identifier", "petra_po")]},
            # Free text is a text_match (C0: stored-key bindings never compare text).
            {"kind": "text_match", "tier_ceiling": "lead", "from_binding": "t.gold_po",
             "to_binding": "t.fm_po", "pairs": [_pair("po_number", "ponumber", "text")]}],
              "same_record_as"),
        _link("PurchaseOrder.ordered_from", "Supplier", "N:1", [
            {"kind": "fk", "tier_ceiling": "verified", "from_binding": "t.gold_po",
             "to_binding": "t.supplier", "pairs": [_pair("supplier_id", "id", "text_id")]}],
              "purchase_orders"),
        _link("PurchaseOrderLine.of_order", "PurchaseOrder", "N:1", [
            {"kind": "fk", "tier_ceiling": "verified", "from_binding": "t.gold_line",
             "to_binding": "t.gold_po",
             "pairs": [_pair("purchase_order_id", "id", "text_id")]}], "lines"),
        _link("Receipt.for_order", "PurchaseOrder", "N:1", [
            {"kind": "text_match", "tier_ceiling": "lead", "from_binding": "t.receipt",
             "to_binding": "t.gold_po", "pairs": [_pair("po_reference", "po_number", "text")]}],
              "receipts"),
        _link("Receipt.covers_lines", "PurchaseOrderLine", "N:M", [
            {"kind": "fk", "tier_ceiling": "verified", "from_binding": "t.receipt",
             "to_binding": "t.gold_line",
             "via": {"binding": "t.receipt_line",
                     "from_pairs": [_pair("id", "receipt_id", "text_id")],
                     "to_pairs": [_pair("line_id", "id", "text_id")]}}], "receipts"),
    ],
    "event_types": [
        {"id": "Receipt.received", "object_type": "Receipt",
         "description": "Goods received; the PO is reachable only by a free-text reference.",
         "time_basis": "source_recorded",
         "time": {"binding": "t.receipt", "path": "received_at", "parser": "iso_timestamp"},
         "qualifiers": [{"qualifier": "receipt", "object_type": "Receipt", "via": "self"},
                        {"qualifier": "purchase_order", "object_type": "PurchaseOrder",
                         "via": "Receipt.for_order"}],
         "evidence": EVIDENCE},
        {"id": "PurchaseOrder.status_changed", "object_type": "PurchaseOrder",
         "description": "Gold status changed between captures.",
         "time_basis": "capture_observed", "observed_property": "status",
         "qualifiers": [{"qualifier": "order", "object_type": "PurchaseOrder", "via": "self"}],
         "evidence": EVIDENCE},
        {"id": "PurchaseOrder.placed", "object_type": "PurchaseOrder",
         "description": "PO created.", "time_basis": "source_recorded",
         "time": {"binding": "t.gold_po", "path": "created_at", "parser": "iso_timestamp"},
         "qualifiers": [{"qualifier": "order", "object_type": "PurchaseOrder", "via": "self"},
                        {"qualifier": "supplier", "object_type": "Supplier",
                         "via": "PurchaseOrder.ordered_from"}],
         "evidence": EVIDENCE},
    ],
}


def normalizer(kind, value):
    text = str(value).strip().upper()
    if kind == "petra_po":
        text = re.sub(r"[^A-Z0-9]", "", text)
    return text or None


def compiled_schema(document=DOMAIN):
    fmt = json.loads(FORMAT.read_text(encoding="utf-8"))
    problems = json_contract.validate(document, fmt, root=fmt)
    if problems:
        raise AssertionError(problems)
    try:
        from ontology.schema import compile_documents  # the loader, once it lands
    except ImportError:
        compile_documents = None
    if compile_documents is not None:
        return compile_documents([("domains/fixture_purchasing.json", document)])
    resolved = json.loads(json.dumps(document).replace(
        json.dumps(EVIDENCE[0]), json.dumps(document["evidence_catalog"]["fixture"])))
    return SimpleNamespace(
        object_types={t["id"]: t for t in resolved["object_types"]},
        link_types={link["id"]: link for link in resolved["link_types"]},
        event_types={e["id"]: e for e in resolved["event_types"]}, action_types={},
        digest="sha256:" + hashlib.sha256(json.dumps(
            resolved, sort_keys=True).encode()).hexdigest())


# ----------------------------------------------------------------------- harness

class Clock:
    def __init__(self):
        self.now = T1

    def __call__(self):
        return self.now


class ProjectionCase(unittest.TestCase):
    """Two captures: T1 the fixture tables, T2 after po-a and l1 changed."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.fake = FakeWorkbench()
        cls.clock = Clock()
        cls.audience = AudienceMap(AUDIENCE, tenant="plx")
        cls.mirror_path = root / "mirror.sqlite"
        cls.mirror = SourceMirror(cls.mirror_path,
                                  WorkbenchReadApiClient(BASE, KEY, fetch=cls.fake),
                                  cls.audience, clock=cls.clock, retry_delay_seconds=0,
                                  page_size=2)
        cls.mirror.sync(OPS, relations=sorted(CATALOG), full=True)
        cls.fake.set("public.app_purchase_order", "po-a", status="RECEIVED",
                     total_cost="36.00")
        cls.fake.set("public.app_purchase_order_line", "l1", quantity="1510")
        cls.clock.now = T2
        cls.mirror.sync(OPS, relations=sorted(CATALOG), full=True)
        cls.schema = compiled_schema()
        cls.policy = ReadPolicy.from_audience_map(cls.audience, marking_principals=MARKINGS)
        cls.store_path = root / "store" / "ontology.sqlite"
        with MirrorCaptures(cls.mirror_path) as captures:
            cls.report = build_store(cls.store_path, cls.schema, {"portal_api": captures},
                                     normalizer=normalizer, clock=lambda: T2)

    @classmethod
    def tearDownClass(cls):
        cls.mirror.close()
        cls.tmp.cleanup()

    def store(self, path=None):
        store = ProjectionStore(path or self.store_path, self.policy)
        self.addCleanup(store.close)
        return store

    def raw(self, path=None):
        conn = sqlite3.connect(str(path or self.store_path))
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        return conn

    def build(self, name, **options):
        path = Path(self.tmp.name) / name
        with MirrorCaptures(self.mirror_path) as captures:
            report = build_store(path, self.schema, {"portal_api": captures},
                                 normalizer=normalizer, clock=lambda: T2, **options)
        return path, report


def oid(type_id, binding, **key):
    return f"{type_id}:{binding}:{json.dumps(key, sort_keys=True, separators=(',', ':'))}"


PO_A, PO_B = oid("PurchaseOrder", "t.gold_po", id="po-a"), oid("PurchaseOrder", "t.gold_po",
                                                               id="po-b")
PO_C, PO_D = oid("PurchaseOrder", "t.gold_po", id="po-c"), oid("PurchaseOrder", "t.gold_po",
                                                               id="po-d")
PO_E = oid("PurchaseOrder", "t.gold_po", id="po-e")
FM_1, FM_3 = oid("PurchaseOrder", "t.fm_po", id=1), oid("PurchaseOrder", "t.fm_po", id=3)
FM_4 = oid("PurchaseOrder", "t.fm_po", id=4)
BOLT = oid("Supplier", "t.supplier", code="BOLT")
L1, L2, L3 = (oid("PurchaseOrderLine", "t.gold_line", id=f"l{n}") for n in (1, 2, 3))
R1, R2 = oid("Receipt", "t.receipt", id="r1"), oid("Receipt", "t.receipt", id="r2")
T1_TEXT = "2026-09-29T12:00:00.000000Z"
T2_TEXT = "2026-09-29T13:00:00.000000Z"


# ----------------------------------------------------------------------- tests

class RebuildTests(ProjectionCase):
    def test_rebuild_twice_gives_identical_digest(self):
        first, report1 = self.build("again/one.sqlite")
        second, report2 = self.build("again/two.sqlite")
        self.assertEqual(report1["content_digest"], report2["content_digest"])
        self.assertEqual(report1["content_digest"], self.report["content_digest"])
        # Rebuilding in place swaps a new generation with the same content.
        _, report3 = self.build("again/one.sqlite")
        self.assertEqual(report3["content_digest"], report1["content_digest"])
        self.assertEqual(content_digest(self.raw(first)), report1["content_digest"])
        self.assertEqual(self.store(second).meta()["content_digest"], report2["content_digest"])
        self.assertEqual(sorted(p.name for p in first.parent.iterdir()),
                         ["one.sqlite", "two.sqlite"])  # no temp file left behind

    def test_counts_and_meta(self):
        counts = self.report["counts"]
        self.assertEqual(counts["objects"], 15)
        self.assertGreater(counts["values"], 30)
        meta = self.store().meta()
        self.assertEqual(meta["format"], store_ddl.FORMAT)
        self.assertEqual(meta["schema_digest"], self.schema.digest)
        self.assertEqual(meta["built_at"], T2_TEXT)
        raw = self.raw()
        generations = json.loads(raw.execute(
            "SELECT value FROM store_meta WHERE key = 'l1_generations'").fetchone()[0])
        self.assertEqual(generations, {"portal_api": {"generation": self.mirror.generation}})

    def test_relation_completeness_is_each_relations_own_capture_claim(self):
        # A relation never fully traversed is partial, one served from a persisted copy
        # is stale; the others keep their own completed full traversal, although the
        # family as a whole is partial and stale. Row counts play no part.
        def completeness(path):
            return json.loads(self.raw(path).execute(
                "SELECT value FROM store_meta WHERE key = 'relation_completeness'"
            ).fetchone()[0])

        whole = completeness(None)["portal_api"]
        self.assertTrue(whole)
        self.assertEqual({(f["completeness"], f["full_traversal_at"], f["stale"])
                          for f in whole.values()}, {("complete", T2_TEXT, False)})
        copy_path = Path(self.tmp.name) / "partial-mirror.sqlite"
        with sqlite3.connect(str(self.mirror_path)) as src, \
                sqlite3.connect(str(copy_path)) as dst:
            src.backup(dst)
        with sqlite3.connect(str(copy_path)) as conn:
            conn.execute("DELETE FROM relation_state WHERE relation = 'public.app_receipt_line'")
            conn.execute("CREATE TABLE IF NOT EXISTS persisted_relation (relation TEXT "
                         "PRIMARY KEY, freshness_json TEXT NOT NULL, attempted_at TEXT NOT NULL)")
            conn.execute("INSERT INTO persisted_relation VALUES ('public.app_receipt', ?, ?)",
                         (json.dumps({"captures_as_of": T1_TEXT}), T2_TEXT))
        path = Path(self.tmp.name) / "partial" / "store.sqlite"
        with MirrorCaptures(copy_path) as captures:
            build_store(path, self.schema, {"portal_api": captures},
                        normalizer=normalizer, clock=lambda: T2)
        part = completeness(path)["portal_api"]
        self.assertEqual(part.keys(), whole.keys())
        self.assertEqual(part.pop("public.app_receipt_line"),
                         {"completeness": "partial", "full_traversal_at": None,
                          "stale": False})
        self.assertEqual(part.pop("public.app_receipt"),
                         {"completeness": "complete", "full_traversal_at": T2_TEXT,
                          "stale": True})
        self.assertEqual(part, {k: v for k, v in whole.items() if k in part})
        fresh = json.loads(self.raw(path).execute(
            "SELECT value FROM store_meta WHERE key = 'freshness'").fetchone()[0])
        self.assertEqual([(f["source"], f["completeness"], f["stale"]) for f in fresh],
                         [("portal_api", "partial", True)])

    def test_swap_is_atomic_and_readers_keep_their_generation(self):
        path, _ = self.build("swap/store.sqlite")
        reader = self.store(path)
        self.assertIsNotNone(reader.object(OPS, PO_A))
        # A rebuild cut before the second capture is a different generation.
        _, cut = self.build("swap/store.sqlite", as_known_at=T1_TEXT)
        status = reader.values(OPS, PO_A, property="status")
        self.assertEqual([v["value"] for v in status], ["RECEIVED"])   # old generation
        self.assertEqual(reader.meta()["as_known_at"], "")
        reader.reopen()
        self.assertEqual(reader.meta()["as_known_at"], T1_TEXT)
        self.assertEqual(reader.meta()["content_digest"], cut["content_digest"])
        self.assertEqual([v["value"] for v in reader.values(OPS, PO_A, property="status")],
                         ["OPEN"])

    def test_failed_build_leaves_the_previous_generation(self):
        path, report = self.build("failed/store.sqlite")
        builder = StoreBuilder(path)
        builder.add("objects", [{"object_id": "X:b:{}", "object_type": "X", "binding_id": "b",
                                 "native_key": "{}", "audience_key": "api:public.x",
                                 "first_recorded_at": T1_TEXT}])
        builder.add("object_versions", [
            {"object_id": "X:b:{}", "l1_handle": "h1", "digest": "d", "lineage": "fm_bronze",
             "markings": "[]", "recorded_at": T1_TEXT, "superseded_at": T2_TEXT},
            {"object_id": "X:b:{}", "l1_handle": "h2", "digest": "d", "lineage": "fm_bronze",
             "markings": "[]", "recorded_at": "2026-09-29T12:30:00.000000Z",
             "superseded_at": None}])
        with self.assertRaises(StoreError) as caught:
            builder.commit(built_at=T2_TEXT)
        self.assertEqual(caught.exception.code, "store_invariant_violated")
        self.assertIn("overlapping_versions:X:b:{}", str(caught.exception))
        self.assertEqual(content_digest(self.raw(path)), report["content_digest"])
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["store.sqlite"])

    def test_foreign_keys_are_on_for_the_writing_connection(self):
        builder = StoreBuilder(Path(self.tmp.name) / "fk" / "store.sqlite")
        self.addCleanup(builder.abort)
        self.assertEqual(builder.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            builder.add("object_versions", [
                {"object_id": "missing", "l1_handle": "h", "digest": "d",
                 "lineage": "fm_bronze", "markings": "[]", "recorded_at": T1_TEXT}])


class EvidenceTests(ProjectionCase):
    TRANSFORMING = {"identifier", "yyyymmdd_date", "iso_timestamp", "boolean_text"}

    def resolver(self):
        """The mirror as the claim gate's reader of the store's values: a version the store
        holds as superseded is history, read as of its own moment (``changed_since``
        cleared, as ``plx.investigate`` does for ``as_known_at``); a current one is not."""
        cache = {}
        history = {row[0] for row in self.raw().execute(
            "SELECT l1_handle FROM object_versions WHERE superseded_at IS NOT NULL")}

        def rows(handle):
            if handle not in cache:
                result = self.mirror.resolve_citation(handle, FINANCE)
                if result.get("ok") and handle in history:
                    result = {**result, "changed_since": False}
                cache[handle] = result if result.get("ok") else None
            return cache[handle]
        return rows

    def test_every_value_cites_a_handle_the_claim_gate_accepts(self):
        rows = self.resolver()
        items, nulls = [], 0
        values = self.raw().execute("SELECT * FROM object_values").fetchall()
        self.assertEqual(len(values), self.report["counts"]["values"])
        for index, value in enumerate(values):
            resolved = rows(value["l1_handle"])
            self.assertIsNotNone(resolved, value["l1_handle"])
            found, cell = claim_checks.dig(resolved, value["field_path"])
            self.assertTrue(found, (value["l1_handle"], value["field_path"]))
            if value["value"] is None:
                self.assertIsNone(cell)   # a source null, cited where it is null
                nulls += 1
                continue
            claimed = value["value"]
            if value["parser"] in self.TRANSFORMING:
                parsed, problem = _parse(value["parser"], cell, kind=self._kind(value),
                                         normalizer=normalizer)
                self.assertEqual((parsed, problem), (value["value"], None))
                claimed = str(cell).strip()
            claim = {"value": claimed, "citation": value["l1_handle"],
                     "field": value["field_path"]}
            if value["value_type"] == "quantity":
                claim["role"] = value["quantity_role"]
                if value["unit"] is not None:
                    claim["unit"] = value["unit"]
            items.append({"id": f"F{index + 1}", "texts": [],
                          "citations": [{"handle": value["l1_handle"], "status": "resolved"}],
                          "value_claims": claim_checks.parse_value_claims(
                              [claim], [value["l1_handle"]], [])})
        # The schema's quantity roles ground role claims (claim_checks contracts).
        contracts = self.store().claim_contracts(FINANCE)
        result = claim_checks.check_items(items, rows=rows, contracts=contracts)
        self.assertEqual(result["failures"], [])
        self.assertTrue(result["passed"])
        self.assertEqual(result["counts"]["passed"], len(values) - nulls)
        self.assertEqual(result["counts"]["source_contract"],
                         sum(1 for v in values if v["value_type"] == "quantity"))
        # Without them the gate refuses to take the role on trust (fail closed).
        bare = claim_checks.check_items(items, rows=rows)
        self.assertEqual({f["code"] for f in bare["failures"]}, {"claim_role_unverified"})
        # A wrong role is refused against the schema's contract.
        wrong = [dict(item, value_claims=[dict(item["value_claims"][0], role="produced")])
                 for item in items if item["value_claims"][0].get("role")]
        refused = claim_checks.check_items(wrong, rows=rows, contracts=contracts)
        self.assertEqual({f["code"] for f in refused["failures"]}, {"claim_role_mismatch"})
        self.assertGreater(nulls, 0)

    def _kind(self, value):
        type_id = value["object_id"].split(":", 1)[0]
        return next(p.get("identifier_kind") for p in self.schema.object_types[type_id][
            "properties"] if p["name"] == value["property"])

    def test_identifier_index_raw_texts_are_claim_checked(self):
        rows = self.resolver()
        identifiers = self.raw().execute("SELECT * FROM identifiers").fetchall()
        self.assertTrue(identifiers)
        items = [{"id": f"F{i + 1}", "texts": [],
                  "citations": [{"handle": row["l1_handle"], "status": "resolved"}],
                  "value_claims": claim_checks.parse_value_claims(
                      [{"value": row["raw"].strip(), "citation": row["l1_handle"],
                        "field": row["field_path"]}], [row["l1_handle"]], [])}
                 for i, row in enumerate(identifiers)]
        self.assertTrue(claim_checks.check_items(items, rows=rows)["passed"])
        for row in identifiers:
            self.assertEqual(normalizer(row["identifier_kind"], row["raw"]), row["normalized"])

    def test_every_handle_and_link_evidence_resolves(self):
        raw = self.raw()
        handles = {row[0] for table in ("object_versions", "identifiers", "events")
                   for row in raw.execute(f"SELECT l1_handle FROM {table}")}
        for (evidence,) in raw.execute("SELECT evidence_handles FROM links"):
            handles |= set(json.loads(evidence))
        batch = self.mirror.resolve_citations(sorted(handles), FINANCE)
        self.assertEqual(batch["ok_count"], len(handles))

    def test_parsers_units_currency_and_lineage(self):
        store = self.store()
        po_a = {v["property"]: v for v in store.object(FINANCE, PO_A)["values"]}
        self.assertEqual(po_a["total_cost"]["value"], "36.00")
        self.assertEqual(po_a["total_cost"]["currency"], "USD")
        self.assertEqual(po_a["po_number"]["value"], "PO1001")
        self.assertEqual(po_a["created_at"]["value"], "2026-09-01T10:00:00.000000Z")
        self.assertEqual(po_a["total_cost"]["lineage"], "gold_etl_from_fm")
        po_b = {v["property"]: v for v in store.object(FINANCE, PO_B)["values"]}
        self.assertEqual(po_b["total_cost"]["currency"], "CAD")
        self.assertEqual(po_b["created_at"]["value"], "2026-09-05T06:30:00.000000Z")
        self.assertEqual(po_b["po_number"]["lineage"], "gold_native_staging")
        po_c = {v["property"]: v for v in store.object(FINANCE, PO_C)["values"]}
        self.assertEqual(po_c["total_cost"]["value"], "9.5")       # float source, exact text
        self.assertIsNone(po_c["order_date"]["value"])             # source null kept
        # Exact decimal text, never rounded (T1 version of po-a).
        old = store.values(FINANCE, PO_A, property="total_cost", as_known_at=T1_TEXT)
        self.assertEqual(old[0]["value"], "35.099999999999994")
        fm_1 = {v["property"]: v for v in store.object(FINANCE, FM_1)["values"]}
        self.assertEqual(fm_1["order_date"]["value"], "2026-09-02")
        self.assertEqual(fm_1["order_date"]["lineage"], "fm_bronze")
        fm_4 = {v["property"]: v for v in store.object(FINANCE, FM_4)["values"]}
        self.assertEqual(fm_4["po_number"]["lineage"], "gold_native_staging")  # UAT- rule
        self.assertNotIn("order_date", fm_4)    # zero date: a reported gap, not a source null
        line1 = {v["property"]: v for v in store.object(FINANCE, L1)["values"]}
        self.assertEqual((line1["quantity"]["value"], line1["quantity"]["unit"],
                          line1["quantity"]["quantity_role"]), ("1510", "EA", "ordered"))
        self.assertEqual(line1["unit_cost"]["currency"], "USD")     # via_link header currency
        self.assertEqual(line1["quantity"]["lineage"], "gold_etl_from_fm")   # inherited
        line2 = {v["property"]: v for v in store.object(FINANCE, L2)["values"]}
        self.assertIsNone(line2["quantity"]["unit"])                # uom null: typed gap
        self.assertEqual(line2["unit_cost"]["currency"], "CAD")
        self.assertEqual(line2["quantity"]["lineage"], "gold_native_staging")  # via-link rule
        line3 = {v["property"]: v for v in store.object(FINANCE, L3)["values"]}
        self.assertIsNone(line3["unit_cost"]["currency"])           # dangling header
        self.assertEqual(line3["quantity"]["lineage"], "gold_native_staging")   # default
        gaps = {(g["code"], g["where"]) for g in self.report["gaps"]}
        self.assertIn(("parse_not_iso_timestamp", "PurchaseOrder.created_at@t.gold_po"), gaps)
        self.assertIn(("currency_unresolved", "PurchaseOrderLine.unit_cost@t.gold_line"), gaps)
        self.assertIn(("parse_zero_date", "PurchaseOrder.order_date@t.fm_po"), gaps)

    def test_authority_rank_follows_precedence_and_lineage(self):
        raw = self.raw()
        ranks = {(row["object_id"], row["property"]): row["authority_rank"] for row in
                 raw.execute("SELECT * FROM object_values WHERE superseded_at IS NULL")}
        self.assertEqual(ranks[(FM_1, "po_number")], 0)
        self.assertEqual(ranks[(PO_A, "po_number")], 1)
        self.assertIsNone(ranks[(PO_B, "po_number")])          # staging-native: never authority
        self.assertIsNone(ranks[(FM_4, "po_number")])
        self.assertIsNone(ranks[(PO_A, "status")])             # no precedence declared

    def test_non_unique_binding_key_is_never_projected(self):
        raw = self.raw()
        suppliers = [row[0] for row in raw.execute(
            "SELECT object_id FROM objects WHERE object_type = 'Supplier'")]
        self.assertEqual(suppliers, [BOLT])
        gap = next(g for g in self.report["gaps"] if g["code"] == "binding_key_not_unique")
        self.assertEqual(gap["where"], "t.supplier")


class LinkTests(ProjectionCase):
    def links(self, actor=FINANCE, **query):
        return self.store().links(actor, **query)

    def test_same_record_stored_key_is_verified_with_reason(self):
        links = [l for l in self.links(object_id=PO_A, link_type="PurchaseOrder.same_record_as")
                 if l["to_object"] == FM_1]
        self.assertEqual([l["tier"] for l in links], ["verified"])
        verified = links[0]
        self.assertEqual([f["compare"] for f in verified["reason"]], ["integer_id", "identifier"])
        self.assertEqual(verified["reason"][0]["value"], "1")
        self.assertEqual(verified["reason"][0]["from_field"], "row.custom_fields.fmGeneratedPoId")
        self.assertEqual(len(verified["evidence_handles"]), 2)
        # Anchor records joined on free text (a text_match) are a lead; so is a record
        # whose stored key is absent (only the identifier pair agrees), and why_not says why.
        leads = self.links(object_id=PO_D, link_type="PurchaseOrder.same_record_as")
        self.assertEqual([(l["to_object"], l["tier"]) for l in leads], [(FM_1, "lead")] * 2)
        because = sorted(f.get("because") for l in leads for f in l["why_not"]
                         if f["feature"] == "tier_cap")
        self.assertEqual(because, ["a_declared_pair_is_unknown"])
        unknown = [f for l in leads for f in l["why_not"] if f.get("outcome") == "unknown"]
        self.assertEqual(unknown[0]["problem"], "value_absent")

    def test_text_compare_on_a_stored_key_binding_is_still_capped(self):
        # Defence in depth (DECISIONS/2026-10-02-stored-key-compare 3): the C0 format and
        # loader refuse a stored-key binding that compares text, but a schema that reached
        # the projector anyway is still capped, never promoted to its declared ceiling.
        with self.assertRaises(AssertionError):
            bypass = copy.deepcopy(DOMAIN)
            bypass["link_types"][0]["bindings"][1].update(kind="same_record",
                                                          tier_ceiling="verified")
            compiled_schema(bypass)
        schema = compiled_schema()
        forged = copy.deepcopy(schema.link_types["PurchaseOrder.same_record_as"])
        forged["bindings"][1].update(kind="same_record", tier_ceiling="verified")
        original, schema.link_types["PurchaseOrder.same_record_as"] = (
            schema.link_types["PurchaseOrder.same_record_as"], forged)
        self.addCleanup(schema.link_types.__setitem__, "PurchaseOrder.same_record_as", original)
        path = Path(self.tmp.name) / "forged-text-pair.sqlite"
        with MirrorCaptures(self.mirror_path) as captures:
            build_store(path, schema, {"portal_api": captures}, normalizer=normalizer,
                        clock=lambda: T2)
        leads = [l for l in self.store(path).links(FINANCE, object_id=PO_D,
                                                   link_type="PurchaseOrder.same_record_as")
                 if any(f["compare"] == "text" for f in l["reason"])]
        self.assertEqual([(l["to_object"], l["tier"]) for l in leads], [(FM_1, "lead")])
        self.assertEqual(sorted(f["because"] for f in leads[0]["why_not"]
                                if f["feature"] == "tier_cap"),
                         ["anchor_records_never_join_on_text",
                          "free_text_compare_is_not_a_stored_key"])

    def test_typed_disagreement_is_contradicted(self):
        links = self.links(object_id=PO_C, link_type="PurchaseOrder.same_record_as")
        self.assertEqual([(l["to_object"], l["tier"]) for l in links], [(FM_3, "contradicted")])
        differ = [f for f in links[0]["why_not"] if f.get("outcome") == "differ"]
        self.assertEqual((differ[0]["from_value"], differ[0]["to_value"]),
                         ("PO3003", "PO3999"))

    def test_property_conflict_is_recorded_on_the_same_record_link(self):
        verified = next(l for l in self.links(object_id=PO_A,
                                              link_type="PurchaseOrder.same_record_as")
                        if l["tier"] == "verified")
        conflicts = [f for f in verified["why_not"] if f["feature"] == "property_conflict"]
        self.assertEqual([c["property"] for c in conflicts], ["order_date"])
        self.assertEqual(conflicts[0]["authority"], "to")   # FM outranks the gold ETL copy
        self.assertNotIn("value", conflicts[0]["from"])     # no values on the link
        self.assertIn("conflict_across_records",
                      {g["code"] for g in self.report["gaps"]})

    def test_money_on_two_tax_bases_is_never_a_property_conflict(self):
        def build(name, fm_basis):
            domain = copy.deepcopy(DOMAIN)
            po = next(t for t in domain["object_types"] if t["id"] == "PurchaseOrder")
            cost = next(p for p in po["properties"] if p["name"] == "total_cost")
            cost["sources"][0]["tax_basis"] = "excl_tax"
            cost["sources"].append(_src("t.fm_po", "id", "decimal_text", currency="USD",
                                        tax_basis=fm_basis))
            cost["authority"] = FM_FIRST
            path = Path(self.tmp.name) / name
            with MirrorCaptures(self.mirror_path) as captures:
                report = build_store(path, compiled_schema(domain), {"portal_api": captures},
                                     normalizer=normalizer, clock=lambda: T2)
            link = next(l for l in self.store(path).links(
                FINANCE, object_id=PO_A, link_type="PurchaseOrder.same_record_as")
                if l["to_object"] == FM_1)
            return ({f["property"] for f in link["why_not"]
                     if f["feature"] == "property_conflict"},
                    {g["code"] for g in report["gaps"]})
        # po-a's 36.00 vs FM id 1 on one basis: a conflict, cited on the link.
        conflicts, codes = build("basis-same.sqlite", "excl_tax")
        self.assertIn("total_cost", conflicts)
        self.assertNotIn("conflict_not_compared_tax_basis", codes)
        for basis in ("incl_tax", "unknown"):
            conflicts, codes = build(f"basis-{basis}.sqlite", basis)
            self.assertNotIn("total_cost", conflicts)
            self.assertIn("order_date", conflicts)  # other properties still compare
            self.assertIn("conflict_not_compared_tax_basis", codes)

    def test_prices_per_two_units_are_never_a_property_conflict(self):
        def build(name, fm_per):
            domain = copy.deepcopy(DOMAIN)
            po = next(t for t in domain["object_types"] if t["id"] == "PurchaseOrder")
            cost = next(p for p in po["properties"] if p["name"] == "total_cost")
            cost["money_measure"] = "unit_price"  # read as a price, to reach the guard
            cost["sources"][0].update(tax_basis="excl_tax", per_unit="kg")
            cost["sources"].append(_src("t.fm_po", "id", "decimal_text", currency="USD",
                                        tax_basis="excl_tax", **fm_per))
            cost["authority"] = FM_FIRST
            path = Path(self.tmp.name) / name
            with MirrorCaptures(self.mirror_path) as captures:
                report = build_store(path, compiled_schema(domain), {"portal_api": captures},
                                     normalizer=normalizer, clock=lambda: T2)
            link = next(l for l in self.store(path).links(
                FINANCE, object_id=PO_A, link_type="PurchaseOrder.same_record_as")
                if l["to_object"] == FM_1)
            return ({f["property"] for f in link["why_not"]
                     if f["feature"] == "property_conflict"},
                    {g["code"] for g in report["gaps"]})
        # po-a's 35.10 per kg vs FM id 1's 1 per kg: one denominator, a conflict.
        conflicts, codes = build("per-same.sqlite", {"per_unit": "kg"})
        self.assertIn("total_cost", conflicts)
        self.assertNotIn("conflict_not_compared_price_denominator", codes)
        for name, fm_per in (("per-lb", {"per_unit": "lb"}),
                             ("per-unknown", {"per_unit_unknown": {"reason": "fixture"}})):
            conflicts, codes = build(f"{name}.sqlite", fm_per)
            self.assertNotIn("total_cost", conflicts)
            self.assertIn("order_date", conflicts)  # other properties still compare
            self.assertIn("conflict_not_compared_price_denominator", codes)
        # A denominator a rule decides is compared, and a conflict it makes is counted
        # (the live check's ruled_price_conflicts), never silent.
        domain = copy.deepcopy(DOMAIN)
        po = next(t for t in domain["object_types"] if t["id"] == "PurchaseOrder")
        cost = next(p for p in po["properties"] if p["name"] == "total_cost")
        cost["money_measure"] = "unit_price"
        cost["sources"][0].update(tax_basis="excl_tax", per_unit="kg")
        cost["sources"].append(_src(
            "t.fm_po", "id", "decimal_text", currency="USD", tax_basis="excl_tax",
            per_unit_unknown={"reason": "fixture"},
            per_unit_rules=[{"id": "fixture_rule", "when": [{"path": "id", "op": "present"}],
                             "per_unit": "kg", "evidence": EVIDENCE}]))
        cost["authority"] = FM_FIRST
        with MirrorCaptures(self.mirror_path) as captures:
            report = build_store(Path(self.tmp.name) / "per-rule.sqlite",
                                 compiled_schema(domain), {"portal_api": captures},
                                 normalizer=normalizer, clock=lambda: T2)
        self.assertGreater(report["counts"].get(
            "money_rule.price_denominator.t.fm_po.fixture_rule", 0), 0)
        self.assertGreaterEqual(report["counts"].get(
            "money_rule.price_denominator.conflicts.PurchaseOrder.total_cost", 0), 1)

    def test_money_in_an_unknown_currency_is_never_a_property_conflict(self):
        def build(name, fm_currency):
            domain = copy.deepcopy(DOMAIN)
            po = next(t for t in domain["object_types"] if t["id"] == "PurchaseOrder")
            cost = next(p for p in po["properties"] if p["name"] == "total_cost")
            cost["sources"][0]["tax_basis"] = "excl_tax"
            cost["sources"].append(_src("t.fm_po", "id", "decimal_text",
                                        tax_basis="excl_tax", **fm_currency))
            cost["authority"] = FM_FIRST
            path = Path(self.tmp.name) / name
            with MirrorCaptures(self.mirror_path) as captures:
                report = build_store(path, compiled_schema(domain), {"portal_api": captures},
                                     normalizer=normalizer, clock=lambda: T2)
            link = next(l for l in self.store(path).links(
                FINANCE, object_id=PO_A, link_type="PurchaseOrder.same_record_as")
                if l["to_object"] == FM_1)
            return ({f["property"] for f in link["why_not"]
                     if f["feature"] == "property_conflict"},
                    {g["code"] for g in report["gaps"]})
        conflicts, codes = build("cur-same.sqlite", {"currency": "USD"})
        self.assertIn("total_cost", conflicts)
        self.assertNotIn("conflict_not_compared_currency", codes)
        # A currency no rule decides is unknown, never a difference to report.
        conflicts, codes = build("cur-unknown.sqlite",
                                 {"currency_unknown": {"reason": "fixture"}})
        self.assertNotIn("total_cost", conflicts)
        self.assertIn("order_date", conflicts)  # other properties still compare
        self.assertIn("conflict_not_compared_currency", codes)
        # FM id 1's "USD$" was currency_invalid before; a rule reads it now, so the
        # conflict it makes is new and counted (the live check's ruled_currency_conflicts).
        domain = copy.deepcopy(DOMAIN)
        po = next(t for t in domain["object_types"] if t["id"] == "PurchaseOrder")
        cost = next(p for p in po["properties"] if p["name"] == "total_cost")
        cost["sources"][0]["tax_basis"] = "excl_tax"
        cost["sources"].append(_src("t.fm_po", "id", "decimal_text", tax_basis="excl_tax",
                                    currency_from="curr"))
        cost["authority"] = FM_FIRST
        with MirrorCaptures(self.mirror_path) as captures:
            report = build_store(Path(self.tmp.name) / "cur-rule.sqlite",
                                 compiled_schema(domain), {"portal_api": captures},
                                 normalizer=normalizer, clock=lambda: T2)
        self.assertEqual(report["counts"].get("money_rule.currency.t.fm_po.sign_attached"),
                         1)
        self.assertGreaterEqual(report["counts"].get(
            "money_rule.currency.conflicts.PurchaseOrder.total_cost", 0), 1)

    def test_text_match_is_a_lead_and_ambiguity_is_explained(self):
        links = self.links(object_id=R1, link_type="Receipt.for_order")
        # "PO 1001" is not "PO1001" as text; two POs answer the N:1 reference.
        self.assertEqual(sorted((l["to_object"], l["tier"]) for l in links),
                         [(PO_D, "lead"), (PO_E, "lead")])
        for link in links:
            ambiguous = [f for f in link["why_not"] if f["feature"] == "ambiguous_target"]
            self.assertEqual(ambiguous[0]["candidates"], 2)
        r2 = self.links(object_id=R2, link_type="Receipt.for_order")
        self.assertEqual([(l["to_object"], l["tier"]) for l in r2], [(PO_B, "lead")])
        self.assertEqual(r2[0]["why_not"], [])
        same = self.links(object_id=FM_1, direction="to",
                          link_type="PurchaseOrder.same_record_as")
        self.assertEqual(sorted((l["from_object"], l["tier"]) for l in same),
                         [(PO_A, "verified"), (PO_D, "lead"), (PO_D, "lead"),
                          (PO_E, "lead"), (PO_E, "lead")])

    def test_via_join_record_and_fk(self):
        covers = self.links(object_id=R1, link_type="Receipt.covers_lines")
        self.assertEqual([l["to_object"] for l in covers], [L1])
        stored = self.raw().execute("SELECT recorded_at, superseded_at FROM links WHERE "
                                    "link_type = 'Receipt.covers_lines' ORDER BY recorded_at")
        self.assertEqual([tuple(r) for r in stored], [(T1_TEXT, T2_TEXT), (T2_TEXT, None)])
        self.assertEqual(len(covers[0]["evidence_handles"]), 3)
        self.assertEqual({f["hop"] for f in covers[0]["reason"]}, {"from->via", "via->to"})
        supplier = self.links(object_id=PO_A, link_type="PurchaseOrder.ordered_from")
        self.assertEqual([(l["to_object"], l["tier"]) for l in supplier], [(BOLT, "verified")])
        self.assertEqual(self.links(object_id=PO_B, link_type="PurchaseOrder.ordered_from"), [])

    def test_link_ranges_never_overlap_for_one_feature_set(self):
        spans: dict = {}
        for row in self.raw().execute("SELECT * FROM links"):
            signature = (row["link_type"], row["from_object"], row["to_object"],
                         row["binding_kind"], tuple(
                             (f["hop"], f["from_path"], f["to_path"], f["compare"])
                             for f in json.loads(row["reason"])))
            spans.setdefault(signature, []).append((row["recorded_at"], row["superseded_at"]))
        for signature, ranges in spans.items():
            ranges.sort()
            for (_, end), (start, _) in zip(ranges, ranges[1:]):
                self.assertIsNotNone(end, signature)
                self.assertLessEqual(end, start, signature)
        self.assertTrue(any(len(r) > 1 for r in spans.values()))


class TimeTests(ProjectionCase):
    def test_as_known_at_two_captures(self):
        store = self.store()
        before = store.object(OPS, PO_A, as_known_at="2026-09-29T11:59:59Z")
        self.assertIsNone(before)                     # nothing known before the first capture
        at_t1 = store.values(OPS, PO_A, property="status", as_known_at=T1_TEXT)
        at_mid = store.values(OPS, PO_A, property="status", as_known_at="2026-09-29T12:30:00Z")
        at_t2 = store.values(OPS, PO_A, property="status", as_known_at=T2_TEXT)
        now = store.values(OPS, PO_A, property="status")
        self.assertEqual([v["value"] for v in at_t1], ["OPEN"])
        self.assertEqual([v["value"] for v in at_mid], ["OPEN"])
        self.assertEqual([v["value"] for v in at_t2], ["RECEIVED"])
        self.assertEqual([v["value"] for v in now], ["RECEIVED"])
        self.assertNotEqual(at_t1[0]["l1_handle"], at_t2[0]["l1_handle"])
        # An unchanged object keeps one version for both instants.
        self.assertEqual(store.values(OPS, PO_B, property="status", as_known_at=T1_TEXT),
                         store.values(OPS, PO_B, property="status"))
        # Line l1 changed too; the via link follows each version.
        covers = store.links(OPS, R1, link_type="Receipt.covers_lines", as_known_at=T1_TEXT)
        self.assertEqual(len(covers), 1)

    def test_no_overlapping_version_ranges(self):
        raw = self.raw()
        overlaps = raw.execute(
            """SELECT a.object_id FROM object_versions a JOIN object_versions b
               ON a.object_id = b.object_id AND a.recorded_at < b.recorded_at
               WHERE a.superseded_at IS NULL OR a.superseded_at > b.recorded_at""").fetchall()
        self.assertEqual(overlaps, [])
        versions = raw.execute(
            "SELECT recorded_at, superseded_at FROM object_versions WHERE object_id = ? "
            "ORDER BY recorded_at", (PO_A,)).fetchall()
        self.assertEqual([tuple(v) for v in versions],
                         [(T1_TEXT, T2_TEXT), (T2_TEXT, None)])
        current = raw.execute("SELECT object_id, COUNT(*) FROM object_versions WHERE "
                              "superseded_at IS NULL GROUP BY object_id HAVING COUNT(*) > 1"
                              ).fetchall()
        self.assertEqual(current, [])
        for at in (T1_TEXT, "2026-09-29T12:59:59.000000Z", T2_TEXT):
            per_object = raw.execute(
                """SELECT object_id, COUNT(*) FROM object_versions WHERE recorded_at <= ?
                   AND (superseded_at IS NULL OR superseded_at > ?) GROUP BY object_id
                   HAVING COUNT(*) > 1""", (at, at)).fetchall()
            self.assertEqual(per_object, [])

    def test_build_cut_matches_the_read_time_view(self):
        path, report = self.build("cut/store.sqlite", as_known_at=T1_TEXT)
        cut = self.store(path)
        full = self.store()
        self.assertEqual(report["as_known_at"], T1_TEXT)
        for object_id in (PO_A, PO_B, FM_1, L1):
            past = full.object(FINANCE, object_id, as_known_at=T1_TEXT)
            built = cut.object(FINANCE, object_id)
            self.assertEqual([(v["property"], v["value"], v["l1_handle"])
                              for v in past["values"]],
                             [(v["property"], v["value"], v["l1_handle"])
                              for v in built["values"]])

    def test_events_from_dates_and_captures(self):
        events = self.store().events(OPS, PO_A)
        kinds = [(e["event_type"], e["time_basis"]) for e in events]
        self.assertIn(("PurchaseOrder.placed", "source_recorded"), kinds)
        change = next(e for e in events if e["event_type"] == "PurchaseOrder.status_changed")
        self.assertIsNone(change["time"])
        self.assertEqual((change["window_start"], change["window_end"]), (T1_TEXT, T2_TEXT))
        self.assertEqual(change["field_path"], "row.status")
        placed = next(e for e in events if e["event_type"] == "PurchaseOrder.placed")
        self.assertEqual(placed["time"], "2026-09-01T10:00:00.000000Z")
        self.assertIn({"object_id": BOLT, "qualifier": "supplier"}, placed["objects"])
        self.assertEqual(self.store().events(OPS, PO_B, as_known_at=T2_TEXT)[0]["event_type"],
                         "PurchaseOrder.placed")


class PermissionTests(ProjectionCase):
    def all_object_ids(self):
        return [row[0] for row in self.raw().execute("SELECT object_id FROM objects")]

    def test_denied_actor_sees_nothing_on_any_read_path(self):
        store = self.store()
        types = [row[0] for row in self.raw().execute("SELECT DISTINCT object_type FROM objects")]
        normalized = [row[0] for row in self.raw().execute("SELECT normalized FROM identifiers")]
        for actor in (OUTSIDER, Actor("other", frozenset({"staff:operations"})), None, "eng"):
            for type_id in types:
                self.assertEqual(store.objects(actor, type_id)["object_ids"], [])
                self.assertEqual(store.count(actor, type_id), 0)
            for object_id in self.all_object_ids():
                for at in (None, T1_TEXT):
                    self.assertIsNone(store.object(actor, object_id, as_known_at=at))
                    self.assertEqual(store.values(actor, object_id, as_known_at=at), [])
                    self.assertEqual(store.links(actor, object_id, as_known_at=at), [])
                    self.assertEqual(store.events(actor, object_id, as_known_at=at), [])
            for value in normalized:
                self.assertEqual(store.find(actor, value), [])

    def test_partial_audience_hides_the_other_end_of_links(self):
        store = self.store()
        self.assertEqual(store.count(PO_ONLY, "PurchaseOrder"), 5)    # gold POs only
        self.assertEqual(store.count(PO_ONLY, "Supplier"), 0)
        self.assertIsNotNone(store.object(PO_ONLY, PO_A))
        self.assertIsNone(store.object(PO_ONLY, FM_1))
        self.assertEqual(store.links(PO_ONLY, PO_A), [])   # every other end is hidden
        hits = store.find(PO_ONLY, "PO1001")
        self.assertEqual(sorted(h["object_id"] for h in hits), [PO_A, PO_D, PO_E])
        self.assertEqual(len(store.find(OPS, "PO1001")), 4)
        events = store.events(PO_ONLY, PO_A)
        self.assertEqual([e["event_type"] for e in events],
                         ["PurchaseOrder.status_changed"])  # placed relates a hidden supplier

    def test_markings_trim_values_identifiers_and_links(self):
        store = self.store()
        ops = {v["property"] for v in store.object(OPS, PO_A)["values"]}
        fin = {v["property"] for v in store.object(FINANCE, PO_A)["values"]}
        self.assertNotIn("total_cost", ops)
        self.assertIn("total_cost", fin)
        self.assertEqual(store.object(OPS, PO_A)["withheld"]["properties"], None)
        self.assertEqual(store.values(OPS, L1, property="unit_cost"), [])
        # pii is held by nobody here: the supplier name never leaves the store.
        self.assertNotIn("name", {v["property"] for v in store.object(FINANCE, BOLT)["values"]})
        counting = ReadPolicy.from_audience_map(self.audience, marking_principals=MARKINGS,
                                                reveal_withheld_counts=True)
        with ProjectionStore(self.store_path, counting) as revealing:
            self.assertEqual(revealing.object(OPS, PO_A)["withheld"]["properties"], 1)

    def test_marked_link_is_withheld(self):
        raw = self.raw()
        marks = {row[0] for row in raw.execute("SELECT DISTINCT markings FROM links")}
        self.assertTrue(marks <= set(store_ddl.MARKING_SETS))
        for (value,) in raw.execute("SELECT markings FROM object_values"):
            self.assertIn(value, store_ddl.MARKING_SETS)


Mention = collections.namedtuple("Mention", "kind normalized start end")


class FixtureGrammar:
    """The identifiers.py interface the ontology tools read (kinds/normalize/find_identifiers/kind_spec)
    over the fixture normalizer."""

    def kinds(self):
        return ("petra_po", "receipt_no", "supplier_code")

    def normalize(self, kind, value):
        return normalizer(kind, value)

    def find_identifiers(self, text):
        return [Mention("petra_po", normalizer("petra_po", m.group(0)), m.start(), m.end())
                for m in re.finditer(r"\bPO[ -]?\d{3,9}\b", text, re.I)]

    def kind_spec(self, kind):
        return {"alias_kinds": []}


def ontology_tools_module():
    """The ontology tools (operating/ontology_tools.py), or None until they land."""
    try:
        from operating import ontology_tools
    except ImportError:
        return None
    return ontology_tools


@unittest.skipIf(ontology_tools_module() is None,
                 "operating/ontology_tools.py is not on this base yet")
class L8EndToEndTests(ProjectionCase):
    """Fixture mirror -> projector -> store -> plx.find / plx.object."""

    def tools(self):
        ot = ontology_tools_module()
        contracts = json.loads((Path(__file__).resolve().parents[2] / "operating" /
                                "ontology_tool_contracts.json").read_text(encoding="utf-8"))
        policy = ot.ReadPolicy("plx", audiences={
            "api:public.*": {"staff:operations", "staff:finance"},
            "api:public.app_purchase_order": {"staff:operations", "staff:finance",
                                              "staff:purchasing"}},
            markings={"finance": {"staff:finance"}})
        reader = ot.StoreReader.open(self.store_path)
        self.addCleanup(reader.conn.close)
        return ot.OntologyTools(reader, policy, schema=ot.SchemaView.from_compiled(self.schema),
                                grammar=FixtureGrammar(), contracts=contracts,
                                clock=lambda: T2 + timedelta(hours=1))

    @staticmethod
    def found(result):
        return sorted((c["object"]["object_id"], c["tier"]) for c in result.get("candidates", []))

    def test_find_returns_ranked_candidates_never_a_winner(self):
        tools = self.tools()
        result = tools.call("plx.find", {"query": "PO 1001"}, OPS)
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(self.found(result), sorted([
            (FM_1, "verified"), (PO_A, "verified"),          # fm_bronze, gold_etl_from_fm
            (PO_D, "lead"), (PO_E, "lead")]))                # gold_native_staging: never authority
        fresh = result["meta"]["freshness"]
        self.assertEqual([(f["source"], f["completeness"], f["captured_at"]) for f in fresh],
                         [("portal_api", "complete", T2_TEXT)])
        self.assertEqual(result["meta"]["schema_digest"], self.schema.digest.split(":")[-1])
        display = {c["object"]["object_id"]: c["object"]["display"] for c in
                   result["candidates"]}
        self.assertEqual(display[PO_A]["untrusted_text"], "PO1001")   # the display property
        self.assertEqual(display[PO_A]["origin"]["field_path"], "row.po_number")
        # Audience is applied before ranking: the FM record is not even a candidate.
        limited = tools.call("plx.find", {"query": "PO 1001"}, PO_ONLY)
        self.assertEqual(self.found(limited), sorted([(PO_A, "verified"), (PO_D, "lead"),
                                                      (PO_E, "lead")]))
        denied = tools.call("plx.find", {"query": "PO 1001"}, OUTSIDER)
        self.assertEqual((denied["status"], self.found(denied)), ("not_found", []))

    def test_find_as_known_at_bare_date_is_end_of_that_day(self):
        tools = self.tools()
        result = tools.call("plx.find", {"query": "PO-3003", "as_known_at": "2026-09-29"}, OPS)
        self.assertTrue(result["meta"]["as_known_at"].startswith("2026-09-29T23:59:59.999"))
        self.assertEqual(self.found(result), [(PO_C, "verified")])
        before = tools.call("plx.find", {"query": "PO-3003", "as_known_at": "2026-09-28"}, OPS)
        self.assertEqual(self.found(before), [])
        # The store's own reads use the same convention.
        self.assertEqual(len(self.store().find(OPS, "PO3003", as_known_at="2026-09-29")), 1)
        self.assertEqual(self.store().find(OPS, "PO3003", as_known_at="2026-09-28"), [])

    def test_object_values_conflicts_identity_and_markings(self):
        tools = self.tools()
        result = tools.call("plx.object", {"object_id": PO_A, "response_format": "detailed"},
                            FINANCE)
        values = {v["property"]: v for v in result["values"]}
        self.assertEqual((values["total_cost"]["value"], values["total_cost"]["currency"]),
                         ("36.00", "USD"))
        self.assertEqual(values["status"]["value"]["untrusted_text"], "RECEIVED")
        self.assertEqual(result["gaps"], [])
        conflict = next(c for c in result["conflicts"] if c["subject"] ==
                        "PurchaseOrder.order_date")
        self.assertEqual(sorted((v["value"], v["authority_rank"]) for v in conflict["values"]),
                         [("2026-09-01", 1), ("2026-09-02", 0)])
        identity = [e for e in result["identity"] if e["to"] == FM_1]
        self.assertEqual([e["tier"] for e in identity], ["verified"])
        reason = identity[0]["reason"]
        self.assertEqual([f["agrees"] for f in reason], [True, True])
        # The tools keep only citations of values the reader may read (the po_number pair).
        cited = [c["l1_handle"] for f in reason for c in f.get("citations", ())]
        self.assertTrue(cited)
        batch = self.mirror.resolve_citations(cited, FINANCE)
        self.assertEqual(batch["ok_count"], len(cited))
        why_not = {f["feature"]: f["agrees"] for f in identity[0]["why_not"]}
        self.assertEqual(why_not, {"property_conflict": False})
        # Every value the tool returns is claim-checkable at its citation.
        rows = {}
        items = []
        for index, value in enumerate(result["values"]):
            citation = value["citation"]
            rows[citation["l1_handle"]] = self.mirror.resolve_citation(citation["l1_handle"],
                                                                       FINANCE)
            if value["value_type"] in ("money", "decimal", "date"):
                items.append({"id": f"F{index + 1}", "texts": [], "citations": [
                    {"handle": citation["l1_handle"], "status": "resolved"}],
                    "value_claims": claim_checks.parse_value_claims(
                        [{"value": value["value"], "citation": citation["l1_handle"],
                          "field": citation["field_path"]}], [citation["l1_handle"]], [])})
        self.assertTrue(items)
        self.assertTrue(claim_checks.check_items(items, rows=rows.get)["passed"])
        # Without the finance marking the cost is trimmed; earlier captures read as known.
        ops = tools.call("plx.object", {"object_id": PO_A,
                                        "as_known_at": "2026-09-29T12:30:00Z"}, OPS)
        ops_values = {v["property"]: v for v in ops["values"]}
        self.assertNotIn("total_cost", ops_values)
        self.assertEqual(ops_values["status"]["value"]["untrusted_text"], "OPEN")
        self.assertEqual(tools.call("plx.object", {"object_id": PO_A}, OUTSIDER)["error"]["code"],
                         "object_not_found")


class CheckpointTests(ProjectionCase):
    def test_a_checkpoint_that_raises_stops_the_build_and_leaves_no_store(self):
        class Stop(Exception):
            pass
        ticks = []

        def checkpoint():
            ticks.append(1)
            if len(ticks) == 3:
                raise Stop

        path = Path(self.tmp.name) / "stopped.sqlite"
        with self.assertRaises(Stop):
            self.build_into(path, checkpoint)
        self.assertEqual(len(ticks), 3)
        self.assertFalse(path.exists())

    def test_a_quiet_checkpoint_is_called_between_batches_and_changes_nothing(self):
        ticks = []
        _, report = self.build("ticked.sqlite", batch_size=1, checkpoint=lambda: ticks.append(1))
        self.assertGreater(len(ticks), 5)
        self.assertEqual(report["content_digest"], self.report["content_digest"])

    def build_into(self, path, checkpoint):
        with MirrorCaptures(self.mirror_path) as captures:
            return build_store(path, self.schema, {"portal_api": captures},
                               normalizer=normalizer, clock=lambda: T2, batch_size=1,
                               checkpoint=checkpoint)


class ProbeTests(unittest.TestCase):
    """The live probe over an injected transport (no network): same acceptance checks."""

    def test_probe_reports_acceptance_counts_only(self):
        from scripts.ontology_projection_probe import main, projection_probe

        schema = compiled_schema()
        bound = sorted({b["relation"] for t in schema.object_types.values()
                        for b in t["bindings"] if b.get("source") == "portal_api"})
        with tempfile.TemporaryDirectory() as tmp:
            client = WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench())
            result = projection_probe(client, f"{tmp}/mirror.sqlite", f"{tmp}/store.sqlite",
                                      schema=schema, normalizer=normalizer,
                                      claim_sample=1000, bound=bound)
            self.assertTrue(result["mirror_complete"])   # every bound relation complete
            self.assertEqual(result["objects"], 15)
            self.assertTrue(result["rebuild_digest_equal"])
            self.assertTrue(result["all_sampled_claims_pass"], result)
            self.assertEqual(result["claim_sample"], result["values"])
            self.assertEqual(result["denied_leaks"], 0)
            self.assertEqual(result["denied_objects_probed"], 15)
            self.assertGreater(result["maxrss_mb"], 0)
            self.assertIsNone(result["maxrss_within_budget"])   # no budget asked
            text = json.dumps(result)
            for secret in ("po-a", "PO1001", "api://", "Bolt"):
                self.assertNotIn(secret, text)   # counts only, never a value or handle
        for budget, within in ((1e6, True), (1.0, False)):
            with tempfile.TemporaryDirectory() as tmp:
                client = WorkbenchReadApiClient(BASE, KEY, fetch=FakeWorkbench())
                result = projection_probe(client, f"{tmp}/mirror.sqlite",
                                          f"{tmp}/store.sqlite", schema=compiled_schema(),
                                          normalizer=normalizer, claim_sample=10,
                                          max_rss_mb=budget)
            self.assertEqual(result["max_rss_budget_mb"], budget)
            self.assertIs(result["maxrss_within_budget"], within)
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(main(["--db", "x", "--store", "y"], environ={}), 1)
        self.assertEqual(json.loads(out.getvalue())["error"], "LookupError")


class DerivedFactTierTests(ProjectionCase):
    """Event attachment, hops and via evidence follow only stored-key links."""

    def test_event_qualified_through_text_match_is_not_attached(self):
        raw = self.raw()
        attached = raw.execute(
            """SELECT eo.object_id, eo.qualifier FROM event_objects eo JOIN events e
               ON e.event_id = eo.event_id WHERE e.event_type = 'Receipt.received'
               ORDER BY eo.object_id""").fetchall()
        self.assertEqual([tuple(r) for r in attached], [(R1, "receipt"), (R2, "receipt")])
        for po in (PO_B, PO_D, PO_E):
            self.assertNotIn("Receipt.received",
                             [e["event_type"] for e in self.store().events(OPS, po)])
        gap = next(g for g in self.report["gaps"]
                   if g["code"] == "event_qualifier_below_tier")
        self.assertEqual(gap["where"], "Receipt.received.purchase_order")
        self.assertEqual(gap["count"], 3)   # r1 -> PO_D, PO_E (ambiguous leads); r2 -> PO_B

    def projector(self):
        projector = Projector(self.schema, {}, normalizer=normalizer)
        self.addCleanup(projector.close)
        return projector

    @staticmethod
    def version(projector, object_id, type_id="PurchaseOrder", handle="h", start=T1_TEXT,
                end=None):
        """A version in the projector's working file (where links and hops are read)."""
        from ontology.project import CapturedRecord, _Version
        rec = CapturedRecord("portal_api", "public.x", object_id, {}, handle, "row", "d",
                             start, end, start, "api:public.x")
        return projector._put_version(
            _Version(object_id, type_id, {"id": "t.gold_line"}, rec, start, end))

    @staticmethod
    def link(projector, line, head, tier="verified"):
        projector._store_link({"link_type": "PurchaseOrderLine.of_order", "tier": tier,
                               "from_object": line.object_id, "to_object": head.object_id,
                               "recorded_at": T1_TEXT, "superseded_at": None,
                               "_from": line, "_to": head})

    def test_single_valued_qualifier_resolving_to_several_objects_is_dropped(self):
        projector = self.projector()
        line = self.version(projector, "L", "PurchaseOrderLine")
        heads = [self.version(projector, f"P{n}") for n in (1, 2)]
        for head in heads:
            self.link(projector, line, head)
        self.assertEqual(projector._related(line, "PurchaseOrderLine.of_order",
                                            "PurchaseOrder", "ev.q"), [])
        self.assertEqual([g["code"] for g in projector.report.as_dict()["gaps"]],
                         ["event_qualifier_ambiguous"])
        # The 1:N reverse direction may attach several lines to one order.
        self.link(projector, self.version(projector, "L2", "PurchaseOrderLine"), heads[0])
        self.assertEqual(projector._related(heads[0], "PurchaseOrderLine.of_order",
                                            "PurchaseOrderLine", "ev.q"), ["L", "L2"])

    def test_hops_follow_only_stored_key_tiers(self):
        projector = self.projector()
        line, head = (self.version(projector, "L", "PurchaseOrderLine"),
                      self.version(projector, "P"))
        self.link(projector, line, head, tier="lead")
        self.assertIsNone(projector._hop(line, "PurchaseOrderLine.of_order", "w"))
        self.assertEqual([g["code"] for g in projector.report.as_dict()["gaps"]],
                         ["hop_below_tier"])
        strong = self.projector()
        line, head = self.version(strong, "L", "PurchaseOrderLine"), self.version(strong, "P")
        self.link(strong, line, head, tier="verified")
        self.assertIs(strong._hop(line, "PurchaseOrderLine.of_order", "w"), head)
        self.assertEqual(strong.report.as_dict()["gaps"], [])
        # A contradicted link is never a hop.
        contra = self.projector()
        line, head = self.version(contra, "L", "PurchaseOrderLine"), self.version(contra, "P")
        self.link(contra, line, head, tier="contradicted")
        self.assertIsNone(contra._hop(line, "PurchaseOrderLine.of_order", "w"))
        self.assertEqual(contra.report.as_dict()["gaps"], [])

    def test_via_link_is_hidden_when_its_join_record_is(self):
        rx = Actor("plx", frozenset({"staff:receiving"}))
        audience = AudienceMap({"public.app_receipt": ["staff:receiving"],
                                "public.app_purchase_order_line": ["staff:receiving"]},
                               tenant="plx")
        with ProjectionStore(self.store_path, ReadPolicy.from_audience_map(audience)) as store:
            self.assertIsNotNone(store.object(rx, R1))
            self.assertIsNotNone(store.object(rx, L1))
            self.assertEqual(store.links(rx, R1, link_type="Receipt.covers_lines"), [])
        self.assertEqual(len(self.store().links(OPS, R1, link_type="Receipt.covers_lines")), 1)


class StaticCaptures:
    """A CaptureSource over given records (the protocol BC/mail/SharePoint plug into)."""

    source = "portal_api"

    def __init__(self, records):
        self._records = records

    def records(self, relations):
        return {relation: [r for r in self._records if r.relation == relation]
                for relation in relations}

    def describe(self):
        return {"generation": "static", "relations": {}}


class CaptureSourceTests(unittest.TestCase):
    T0, T1, T2 = ("2026-09-29T10:00:00.000000Z", "2026-09-29T11:00:00.000000Z",
                  "2026-09-29T12:00:00.000000Z")

    def record(self, code, start, end, *, audience="api:public.app_supplier", missing=None,
               handle=None):
        from ontology.project import CapturedRecord
        return CapturedRecord(
            "portal_api", "public.app_supplier", f'{{"id":"{code}"}}',
            {"id": code, "code": code, "name": "n"}, handle or f"api://h/{code}@{start}",
            "row", "0" * 64, start, end, end or start, audience,
            missing_from_source_since=missing)

    def build(self, records):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.sqlite"
        report = build_store(path, compiled_schema(), {"portal_api": StaticCaptures(records)},
                             normalizer=normalizer, clock=lambda: T2)
        audience = AudienceMap({"public.app_supplier": ["staff:operations"]}, tenant="plx")
        store = ProjectionStore(path, ReadPolicy.from_audience_map(audience))
        self.addCleanup(store.close)
        return report, store

    def test_the_current_versions_audience_governs_every_read(self):
        sid = oid("Supplier", "t.supplier", code="AAA")
        _, store = self.build([
            self.record("AAA", self.T0, self.T1),
            self.record("AAA", self.T1, None, audience="api:public.restricted_supplier",
                        handle="api://h/AAA@2")])
        self.assertIsNone(store.object(OPS, sid))
        self.assertIsNone(store.object(OPS, sid, as_known_at=self.T0))   # narrowed ACL applies
        self.assertEqual(store.find(OPS, "AAA", as_known_at=self.T0), [])

    def test_a_zero_length_version_is_a_per_object_gap(self):
        report, store = self.build([
            self.record("AAA", self.T0, self.T0),                      # zero length
            self.record("AAA", self.T0, None, handle="api://h/AAA@2"),
            self.record("BBB", self.T0, None)])
        self.assertIsNone(store.object(OPS, oid("Supplier", "t.supplier", code="AAA")))
        self.assertIsNotNone(store.object(OPS, oid("Supplier", "t.supplier", code="BBB")))
        self.assertIn("capture_versions_overlap", {g["code"] for g in report["gaps"]})

    def test_a_value_equal_to_the_records_own_identifier_is_a_placeholder(self):
        """Wire-host decision A (one absence prover): a carrier holding the record's own
        identifier (``placeholder_when_equal_to``) is a self-reference, stored as a
        source null and reported, never projected as a value of the property."""
        from ontology.project import CapturedRecord
        document = copy.deepcopy(DOMAIN)
        supplier = next(t for t in document["object_types"] if t["id"] == "Supplier")
        supplier["properties"].append({"name": "alt_code", "value_type": "text",
                                       "placeholder_when_equal_to": ["code"],
                                       "sources": [_src("t.supplier", "alt")]})
        records = [CapturedRecord(
            "portal_api", "public.app_supplier", f'{{"id":"{code}"}}',
            {"id": code, "code": code, "name": "n", "alt": alt}, f"api://h/{code}@{self.T0}",
            "row", "0" * 64, self.T0, None, self.T0, "api:public.app_supplier")
            for code, alt in (("AAA", " AAA "), ("BBB", "ZZZ"), ("CCC", "aaa"))]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.sqlite"
        report = build_store(path, compiled_schema(document),
                             {"portal_api": StaticCaptures(records)},
                             normalizer=normalizer, clock=lambda: T2)
        audience = AudienceMap({"public.app_supplier": ["staff:operations"]}, tenant="plx")
        store = ProjectionStore(path, ReadPolicy.from_audience_map(audience))
        self.addCleanup(store.close)

        def alt(code):
            values = store.object(OPS, oid("Supplier", "t.supplier", code=code))["values"]
            [row] = [v for v in values if v["property"] == "alt_code"]
            return row["value"], row["field_path"]

        self.assertEqual(alt("AAA"), (None, "row.alt"))   # its own code: no value of it
        self.assertEqual(alt("BBB")[0], "ZZZ")
        self.assertEqual(alt("CCC")[0], "aaa")             # other text is a value
        gaps = {(g["code"], g["where"]): g["count"] for g in report["gaps"]}
        self.assertEqual(gaps[("placeholder_self_reference", "Supplier.alt_code@t.supplier")],
                         1)

    def test_a_source_when_reads_the_field_only_on_records_passing_its_tests(self):
        """A value source's ``when``: the field means the property only on records that
        pass every test; any other record gets no value of it (never a stored null) and a
        ``source_condition_unmet`` gap citing it, whatever the field holds."""
        from ontology.project import CapturedRecord
        document = copy.deepcopy(DOMAIN)
        supplier = next(t for t in document["object_types"] if t["id"] == "Supplier")
        supplier["properties"].append({"name": "vendor_ref", "value_type": "text", "sources": [
            _src("t.supplier", "ref", when=[{"path": "kind", "op": "eq", "value": "vendor"},
                                            {"path": "order", "op": "present"}])]})
        rows = {"AAA": {"kind": "vendor", "order": "O-1", "ref": "R-1"},
                "BBB": {"kind": "vendor", "ref": "R-2"},                 # no order
                "CCC": {"kind": "agent", "order": "O-3", "ref": "R-3"},
                "DDD": {"kind": "vendor", "order": "O-4", "ref": None}}
        records = [CapturedRecord(
            "portal_api", "public.app_supplier", f'{{"id":"{code}"}}',
            {"id": code, "code": code, "name": "n", **extra}, f"api://h/{code}@{self.T0}",
            "row", "0" * 64, self.T0, None, self.T0, "api:public.app_supplier")
            for code, extra in rows.items()]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.sqlite"
        report = build_store(path, compiled_schema(document),
                             {"portal_api": StaticCaptures(records)},
                             normalizer=normalizer, clock=lambda: T2)
        audience = AudienceMap({"public.app_supplier": ["staff:operations"]}, tenant="plx")
        store = ProjectionStore(path, ReadPolicy.from_audience_map(audience))
        self.addCleanup(store.close)
        read = {code: [v["value"] for v in store.object(
                    OPS, oid("Supplier", "t.supplier", code=code))["values"]
                    if v["property"] == "vendor_ref"] for code in rows}
        self.assertEqual(read, {"AAA": ["R-1"], "BBB": [], "CCC": [], "DDD": [None]})
        (gap,) = [g for g in report["gaps"] if g["code"] == "source_condition_unmet"]
        self.assertEqual((gap["where"], gap["count"], gap["samples"]),
                         ("Supplier.vendor_ref@t.supplier", 2,
                          [f"api://h/BBB@{self.T0}", f"api://h/CCC@{self.T0}"]))

    def test_a_card_reads_the_builds_record_of_the_source_when(self):
        """plx.object reads the build's record of every version failing a value source's
        ``when``, not only the build report's samples: with another cause (an absent
        path) reported at the source, a record the report does not sample that failed
        the tests reads ``source_condition_unmet``, never ``value_unread``. A reader who
        may not read a tested field gets the report's answer only."""
        from ontology.project import CapturedRecord
        ot = ontology_tools_module()
        document = copy.deepcopy(DOMAIN)
        supplier = next(t for t in document["object_types"] if t["id"] == "Supplier")
        supplier["properties"] += [
            {"name": "vendor_ref", "value_type": "text", "sources": [
                _src("t.supplier", "ref", when=[{"path": "kind", "op": "eq", "value": "vendor"},
                                                {"path": "order", "op": "present"}])]},
            {"name": "kind", "value_type": "text", "sources": [_src("t.supplier", "kind",
                                                                    "as_is")]},
            {"name": "order_ref", "value_type": "text", "markings": ["finance"],
             "sources": [_src("t.supplier", "order")]}]
        rows = {"AAA": {"kind": "vendor", "order": "O-1", "ref": "R-1"},
                "BBB": {"kind": "agent", "order": "O-2", "ref": "R-2"},
                "CCC": {"kind": "agent", "ref": "R-3"},
                "DDD": {"kind": "vendor", "order": None, "ref": "R-4"},
                "EEE": {"kind": "vendor", "order": "O-5"}}                # ref absent
        unsampled = [f"Z{n:02d}" for n in range(10)]
        rows.update({code: {"kind": "vendor", "order": None, "ref": f"R-{code}"}
                     for code in unsampled})
        records = [CapturedRecord(
            "portal_api", "public.app_supplier", f'{{"id":"{code}"}}',
            {"id": code, "code": code, "name": "n", **extra}, f"api://h/{code}@{self.T0}",
            "row", "0" * 64, self.T0, None, self.T0, "api:public.app_supplier")
            for code, extra in rows.items()]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.sqlite"
        schema = compiled_schema(document)
        report = build_store(path, schema, {"portal_api": StaticCaptures(records)},
                             normalizer=normalizer, clock=lambda: T2)
        (unmet,) = [g for g in report["gaps"] if g["code"] == "source_condition_unmet"]
        self.assertEqual(unmet["count"], 13)
        self.assertFalse({f"api://h/{c}@{self.T0}" for c in unsampled} & set(unmet["samples"]))
        contracts = json.loads((Path(__file__).resolve().parents[2] / "operating" /
                                "ontology_tool_contracts.json").read_text(encoding="utf-8"))
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        tools = ot.OntologyTools(
            reader, ot.ReadPolicy("plx", audiences={"api:public.*": {"staff:operations",
                                                                     "staff:finance"}},
                                  markings={"finance": {"staff:finance"}}),
            schema=ot.SchemaView.from_compiled(schema), grammar=FixtureGrammar(),
            contracts=contracts, clock=lambda: T2 + timedelta(hours=1))

        def card(code, actor):
            out = tools.call("plx.object", {"object_id": oid("Supplier", "t.supplier",
                                                             code=code)}, actor)
            self.assertNotIn("error", out, out)
            return ([v for v in out["values"] if v["property"] == "vendor_ref"],
                    [g["reason"] for g in out["gaps"] if g["subject"] == "Supplier.vendor_ref"])

        for code in unsampled:
            self.assertEqual(card(code, FINANCE), ([], ["source_condition_unmet"]), code)
            # Without the finance marking the tested field is not read: the report's
            # mixed causes stand, and nothing about the hidden field shows.
            self.assertEqual(card(code, OPS), ([], ["value_unread"]), code)
        self.assertEqual(card("EEE", FINANCE), ([], []))          # passes; path absent
        self.assertEqual(len(card("AAA", FINANCE)[0]), 1)
        # A store whose record is missing or malformed falls back to the report: never a
        # pass, so the mixed causes stand.
        for broken in ("{}", "[]", "not json", '{"Supplier.vendor_ref@t.supplier": 1}'):
            with sqlite3.connect(path) as conn:
                conn.execute("UPDATE store_meta SET value = ? "
                             "WHERE key = 'source_condition_unmet'", (broken,))
            self.assertEqual(card(unsampled[0], FINANCE), ([], ["value_unread"]), broken)

    def test_missing_from_source_is_not_known_before_it_was_seen(self):
        sid = oid("Supplier", "t.supplier", code="AAA")
        _, store = self.build([self.record("AAA", self.T0, None, missing=self.T2)])
        self.assertEqual(store.object(OPS, sid)["missing_from_source_since"], self.T2)
        self.assertIsNone(store.object(OPS, sid, as_known_at=self.T1)[
            "missing_from_source_since"])


class InvoiceWhenRecordTests(unittest.TestCase):
    """The SalesInvoice.customer_po source's ``when`` (orders.json: orderNumber present) is
    decided once, by the build, which records every invoice failing it; plx.object reads
    that record, never re-deciding the tests on stored values. Evidence label:
    ``isolated-fixture`` (synthetic BC rows over the curated schema)."""

    WHERE = "SalesInvoice.customer_po@bc.sales_invoices"

    def project(self, rows, schema=None):
        from ontology import identifiers
        from ontology.schema import load_schema
        from ontology.tests.test_bc_mapping import MAN, StaticCaptures, bc
        ot = ontology_tools_module()
        schema = schema or load_schema()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.sqlite"
        report = build_store(path, schema, {"bc": StaticCaptures("bc", [
            bc("salesInvoices", MAN, {"number": f"PSI-{i}", "externalDocumentNumber":
                                      f"PO-{i}", **row, "id": i}) for i, row in rows.items()])},
            normalizer=identifiers.normalize)
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        tools = ot.OntologyTools(reader, ot.ReadPolicy("plx", audiences={"bc:*": {"staff:ar"}}),
                                 schema=ot.SchemaView.from_compiled(schema),
                                 grammar=identifiers,
                                 contracts=ot.load_contracts())

        def card(i):
            out = tools.call("plx.object", {
                "object_id": f'SalesInvoice:bc.sales_invoices:{{"id":"{i}"}}'},
                Actor("plx", frozenset({"staff:ar"}), "restricted"))
            self.assertNotIn("error", out, out)
            return ({v["property"]: v["value"] for v in out["values"]},
                    [g["reason"] for g in out["gaps"]
                     if g["subject"] == "SalesInvoice.customer_po"])
        return report, reader, card

    def test_a_blank_identifier_is_a_source_null_and_its_card_reads_the_unmet_when(self):
        rows = {"blank": {"orderNumber": ""}, "space": {"orderNumber": " \t "},
                "bad": {"orderNumber": "!!"}, "posted": {"orderNumber": "SO100200"}}
        report, reader, card = self.project(rows)
        stored = dict(reader.conn.execute(
            "SELECT object_id, value FROM object_values WHERE property = 'order_number'"))
        self.assertEqual({k.split('"')[3]: v for k, v in stored.items()},
                         {"blank": None, "space": None, "posted": "SO100200"})
        gaps = {(g["code"], g["where"]): g for g in report["gaps"]}
        unparsed = gaps[("parse_identifier_unparsed",
                         "SalesInvoice.order_number@bc.sales_invoices")]
        self.assertEqual((unparsed["count"], len(unparsed["samples"])), (1, 1))
        self.assertIn("/bad@", unparsed["samples"][0])
        self.assertNotIn("handles", gaps[("source_condition_unmet", self.WHERE)])
        for invoice in ("blank", "space"):
            values, reasons = card(invoice)
            self.assertNotIn("customer_po", values)
            self.assertEqual(reasons, ["source_condition_unmet"], invoice)
            self.assertIsNone(values["order_number"])
        self.assertEqual(card("bad")[1], [])
        self.assertEqual(card("posted")[0]["customer_po"]["untrusted_text"], "PO-posted")

    def test_gte_on_a_bool_or_dict_cell_is_decided_once_by_the_build(self):
        """A ``gte`` test on a raw bool or dict cell fails (never a text compare of its
        stored form): the build records it unmet and the card reads that record, for
        every one of N=1/10/60 invoices (none sampled-only, none duplicated)."""
        from ontology.schema import load_schema
        schema = copy.deepcopy(load_schema())
        invoice = schema.object_types["SalesInvoice"]
        invoice["properties"].append({"name": "flag", "value_type": "text", "sources": [
            {"binding": "bc.sales_invoices", "path": "flag", "parser": "as_is"}]})
        (po,) = [p for p in invoice["properties"] if p["name"] == "customer_po"]
        (source,) = [s for s in po["sources"] if s["binding"] == "bc.sales_invoices"]
        source["when"] = [{"path": "orderNumber", "op": "present"},
                          {"path": "flag", "op": "gte", "value": "5"}]
        for count in (1, 10, 60):
            rows = {f"i{n:02d}": {"orderNumber": "SO100200",
                                  "flag": True if n % 2 else {"a": 1}} for n in range(count)}
            rows["passes"] = {"orderNumber": "SO100200", "flag": 7}
            report, reader, card = self.project(rows, schema)
            (unmet,) = [g for g in report["gaps"] if g["code"] == "source_condition_unmet"]
            self.assertEqual(unmet["count"], count)
            for i in rows:
                values, reasons = card(i)
                self.assertEqual(reasons, [] if i == "passes" else ["source_condition_unmet"],
                                 (count, i))
            recorded = json.loads(reader.meta()["source_condition_unmet"])[self.WHERE]
            self.assertEqual(len(recorded), len(set(recorded)))
            self.assertEqual(len(recorded), count)
            self.assertEqual(card("passes")[0]["customer_po"]["untrusted_text"], "PO-passes")


class GuardTests(unittest.TestCase):
    def test_identifier_schema_without_normalizer_fails_visibly(self):
        schema = compiled_schema()
        import ontology.project as project
        original = project._default_normalizer
        project._default_normalizer = lambda: None
        try:
            with self.assertRaises(ProjectionError) as caught:
                Projector(schema, {})
        finally:
            project._default_normalizer = original
        self.assertEqual(caught.exception.code, "identifier_normalizer_unavailable")

    def test_where_via_link_is_refused(self):
        document = copy.deepcopy(DOMAIN)
        document["object_types"][1]["bindings"][0]["where"] = [
            {"path": "code", "op": "present", "via_link": "PurchaseOrder.ordered_from"}]
        schema = SimpleNamespace(object_types={t["id"]: t for t in document["object_types"]},
                                 link_types={}, event_types={}, digest="sha256:x")
        with self.assertRaises(ProjectionError) as caught:
            Projector(schema, {}, normalizer=normalizer)
        self.assertEqual(caught.exception.code, "where_via_link_unsupported")

    def test_source_without_captures_is_a_reported_gap(self):
        projector = Projector(compiled_schema(), {}, normalizer=normalizer)
        projector.project()
        codes = {g["code"] for g in projector.report.as_dict()["gaps"]}
        self.assertEqual(codes, {"source_not_captured"})


if __name__ == "__main__":
    unittest.main()
