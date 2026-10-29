"""L8: plx.schema / plx.find / plx.object over a fixture projection store.

The store is created with the frozen DDL (ontology/store_ddl.py) and filled by
hand; the identifier grammar and the domain schema are small fixtures in this
file (the real ones live on the identifiers and schema-domains branches). Every
output is validated against operating/ontology_tool_contracts.json with
ontology/json_contract.py. All identifiers are synthetic.
"""

from __future__ import annotations

from contextlib import closing
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from collections import namedtuple

from ontology import store_ddl
from ontology.json_contract import check_schema, validate
from ontology.types import Actor
from operating import ontology_tools as ot

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
FORMAT = json.loads((ROOT / "ontology/schema/_format.schema.json").read_text("utf-8"))
NOW = dt.datetime(2099, 2, 1, 12, 0, tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------- fixture grammar

Mention = namedtuple("Mention", "kind normalized start end")


class FixtureGrammar:
    """Three kinds with the identifiers.py interface (kinds/normalize/
    find_identifiers/kind_spec). PO1xxxxx parses as two kinds, as BC-renumbered
    Petra POs do."""

    _WHOLE = {
        "so_number": (re.compile(r"^\s*SO[-\s]?(\d{4})[-\s]?(\d{4})\s*$", re.I),
                      lambda m: f"SO-{m.group(1)}-{m.group(2)}"),
        "petra_po": (re.compile(r"^\s*(?:our\s+)?P\.?O\.?\s*#?\s*(\d{1,9})\s*$", re.I),
                     lambda m: f"PO{int(m.group(1))}"),
        "bc_document_no": (re.compile(r"^\s*(PSI|PO)[-\s]?(\d{6,9})\s*$", re.I),
                           lambda m: f"{m.group(1).upper()}{m.group(2)}"),
    }
    # Like several real kinds (customer PO, lot, item code), one fixture kind
    # accepts nearly any short text, so parsing alone never proves an identifier.
    _PERMISSIVE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9 #:.\-]{2,39})\s*$")
    _MENTION = {
        "so_number": re.compile(r"\bSO[-\s]?\d{4}[-\s]?\d{4}\b", re.I),
        "petra_po": re.compile(r"\b(?:our\s+)?P\.?O\.?\s*#?\s*\d{1,9}\b", re.I),
    }

    def kinds(self):
        return tuple(self._WHOLE) + ("customer_po",)

    def normalize(self, kind, value):
        if kind == "customer_po":
            found = self._PERMISSIVE.match(value) if isinstance(value, str) else None
            return found.group(1).upper() if found else None
        pattern, fmt = self._WHOLE[kind]
        found = pattern.match(value) if isinstance(value, str) else None
        return fmt(found) if found else None

    def find_identifiers(self, text):
        out = []
        for kind, pattern in self._MENTION.items():
            for found in pattern.finditer(text):
                out.append(Mention(kind, self.normalize(kind, found.group(0)),
                                   found.start(), found.end()))
        return out

    def kind_spec(self, kind):
        return {"alias_kinds": ["bc_document_no"] if kind == "petra_po" else []}


# --------------------------------------------------------------------------- fixture schema

EVIDENCE = [{"kind": "workbench_code", "path": "tests/test_ontology_tools.py",
             "note": "Synthetic fixture."}]


def _binding(bid, source, relation, lineage):
    lin = {"from_capture": "bc_company"} if source == "bc" else {"default": lineage}
    return {"id": bid, "source": source, "relation": relation, "key": ["id"], "lineage": lin,
            "evidence": EVIDENCE}


def _prop(name, value_type, binding, path, **extra):
    source = {"binding": binding, "path": path}
    if value_type == "money":
        source["tax_basis"] = "unknown"  # the frozen format requires one on money
    out = {"name": name, "value_type": value_type, "sources": [source]}
    out.update(extra)
    return out


def _type(tid, bindings, properties, display, **extra):
    out = {"id": tid, "description": f"Fixture {tid}.", "availability": {"state": "served"},
           "bindings": bindings, "properties": properties, "display": display,
           "evidence": EVIDENCE}
    out.update(extra)
    return out


DOMAIN = {
    "format": "plx-ontology-domain/v1", "domain": "fixture", "origin": "curated",
    "description": "L8 test fixture.",
    "object_types": [
        _type("SalesOrder",
              [_binding("portal.app_sales_order", "portal_api", "public.app_sales_order",
                        "gold_etl_from_fm"),
               _binding("fm.order_entry", "portal_api", "public.order_entry", "fm_bronze")],
              [_prop("order_number", "identifier", "portal.app_sales_order", "order_number",
                     identifier_kind="so_number"),
               _prop("customer_po", "text", "portal.app_sales_order", "po_reference"),
               _prop("total_amount", "money", "portal.app_sales_order", "total_amount",
                     currency="CAD", markings=["finance"]),
               _prop("ordered_quantity", "quantity", "portal.app_sales_order", "qty",
                     unit="each", quantity_role="ordered"),
               _prop("promised_date", "date", "portal.app_sales_order", "promised",
                     availability="not_served", availability_reason="Not in the API.")],
              ["order_number"]),
        _type("SalesInvoice",
              [_binding("bc.sales_invoices", "bc", "salesInvoices", None)],
              [_prop("number", "identifier", "bc.sales_invoices", "number",
                     identifier_kind="bc_document_no"),
               _prop("external_document_number", "identifier", "bc.sales_invoices",
                     "externalDocumentNumber", identifier_kind="so_number")],
              ["number"]),
        _type("PurchaseOrder",
              [_binding("portal.app_purchase_order", "portal_api", "public.app_purchase_order",
                        "gold_etl_from_fm"),
               _binding("bc.purchase_orders", "bc", "purchaseOrders", None)],
              [_prop("po_number", "identifier", "portal.app_purchase_order", "po_number",
                     identifier_kind="petra_po")],
              ["po_number"]),
        _type("MailMessage",
              [_binding("mail.po_inbox", "graph_mail", "messages", "mail")],
              [_prop("subject", "text", "mail.po_inbox", "subject")], ["subject"]),
        _type("Document",
              [_binding("sharepoint.docs", "sharepoint", "driveItems", "sharepoint")],
              [_prop("file_name", "text", "sharepoint.docs", "name")], ["file_name"]),
        {"id": "Formulation", "description": "Fixture not-served type.",
         "availability": {"state": "not_served", "reason": "Not exposed by the API."},
         "bindings": [], "properties": [], "evidence": EVIDENCE},
    ],
    "link_types": [
        {"id": "SalesInvoice.invoices_order", "from": "SalesInvoice", "to": "SalesOrder",
         "name": "invoices_order", "reverse_name": "invoices", "cardinality": "N:1",
         "description": "BC invoice externalDocumentNumber carries the Portal order number.",
         "bindings": [{"kind": "fk", "tier_ceiling": "verified",
                       "from_binding": "bc.sales_invoices",
                       "to_binding": "portal.app_sales_order",
                       "pairs": [{"from": "externalDocumentNumber", "to": "order_number",
                                  "compare": "identifier", "identifier_kind": "so_number"}],
                       "evidence": EVIDENCE}]},
        {"id": "SalesOrder.same_record_as", "from": "SalesOrder", "to": "SalesOrder",
         "name": "same_record_as", "reverse_name": "same_record_as", "cardinality": "1:1",
         "description": "Gold order and its FM source row.",
         "bindings": [{"kind": "same_record", "tier_ceiling": "verified",
                       "from_binding": "portal.app_sales_order", "to_binding": "fm.order_entry",
                       "pairs": [{"from": "fm_id", "to": "id", "compare": "integer_id"}],
                       "evidence": EVIDENCE}]},
    ],
    "event_types": [{"id": "SalesOrder.required", "object_type": "SalesOrder",
                     "description": "Required date.", "time_basis": "planned",
                     "time": {"binding": "portal.app_sales_order", "path": "required_date"},
                     "qualifiers": [{"qualifier": "order", "object_type": "SalesOrder",
                                     "via": "self"}], "evidence": EVIDENCE}],
}
SCHEMA = ot.SchemaView.from_documents([DOMAIN])


# --------------------------------------------------------------------------- fixture store

def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Store:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        store_ddl.create(self.conn)
        self.conn.execute("INSERT INTO store_meta VALUES ('schema_digest', ?)",
                          ("sha256:" + SCHEMA.digest_hex,))
        fresh = [{"source": s, "captured_at": "2099-02-01T11:00:00Z", "lag_seconds": 3600,
                  "completeness": "complete", "stale": False}
                 for s in ("portal_api", "bc", "graph_mail", "sharepoint")]
        self.conn.execute("INSERT INTO store_meta VALUES ('freshness', ?)", (json.dumps(fresh),))

    def obj(self, object_id, audience, handle, lineage, *, markings="[]",
            recorded="2099-01-01T00:00:00Z", superseded=None, missing=None, add_object=True):
        otype, binding, native = object_id.split(":", 2)
        if add_object:
            self.conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                              (object_id, otype, binding, json.dumps([native]), audience,
                               recorded, missing))
        self.conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)",
                          (object_id, handle, _h(handle), lineage, markings, recorded,
                           superseded))

    def val(self, object_id, prop, value_type, value, handle, path, lineage, *, unit=None,
            role=None, currency=None, rank=0, markings="[]", recorded="2099-01-01T00:00:00Z",
            superseded=None):
        binding = object_id.split(":")[1]
        self.conn.execute(
            "INSERT INTO object_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (object_id, prop, value_type, value, unit, role, currency, None, None, handle,
             path, _h(f"{handle}|{path}|{value}"), lineage, binding, "as_is", rank, markings,
             recorded, superseded))

    def ident(self, object_id, kind, normalized, raw, prop, handle, path, lineage, *,
              role="primary", markings="[]"):
        self.conn.execute("INSERT INTO identifiers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (kind, normalized, raw, role, object_id, prop, handle, path, lineage,
                           markings, "2099-01-01T00:00:00Z", None))

    def link(self, link_id, link_type, src, dst, tier, kind, handles, reason=None):
        reason = reason or [{"feature": "stored_key", "agrees": True}]
        self.conn.execute(
            "INSERT INTO links(link_id, link_type, from_object, to_object, tier, binding_kind, "
            "reason, why_not, evidence_handles, markings, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (link_id, link_type, src, dst, tier, kind, json.dumps(reason), "[]",
             json.dumps(handles), "[]", "2099-01-01T00:00:00Z"))


SO = "SalesOrder:portal.app_sales_order:so-1"
SO_V1 = "api://public.app_sales_order/%7B%22id%22%3A%22so-1%22%7D@0000000000000001-1"
SO_V2 = "api://public.app_sales_order/%7B%22id%22%3A%22so-1%22%7D@0000000000000001-2"
FM = "SalesOrder:fm.order_entry:9001"
FM_H = "api://public.order_entry/%7B%22id%22%3A9001%7D@0000000000000001-3"
INV = "SalesInvoice:bc.sales_invoices:inv-1"
INV_H = "bc://company_a/salesInvoices/inv-1@cap-1"
PO = "PurchaseOrder:portal.app_purchase_order:po-1"
PO_H = "api://public.app_purchase_order/%7B%22id%22%3A%22po-1%22%7D@0000000000000001-4"
PO2 = "PurchaseOrder:portal.app_purchase_order:po-2"
PO2_H = "api://public.app_purchase_order/%7B%22id%22%3A%22po-2%22%7D@0000000000000001-5"
BCPO = "PurchaseOrder:bc.purchase_orders:bcpo-1"
BCPO_H = "bc://company_a/purchaseOrders/bcpo-1@cap-2"
MAIL = "MailMessage:mail.po_inbox:m-1"
MAIL_H = "graph-mail://mbx-1/m-1@ck1"
DOC = "Document:sharepoint.docs:d-1"
DOC_H = "sharepoint://drive-1/item-1@etag1"


def build(skip=frozenset()):
    """The fixture store; ``skip`` names objects to leave out entirely (to
    compare a denied reader's answer with a store that never held them)."""
    s = Store()
    if SO not in skip:
        s.obj(SO, "api:public.app_sales_order", SO_V1, "gold_etl_from_fm",
              superseded="2099-01-05T00:00:00Z")
        s.obj(SO, "api:public.app_sales_order", SO_V2, "gold_etl_from_fm",
              recorded="2099-01-05T00:00:00Z", add_object=False)
        s.val(SO, "customer_po", "text", "old reference", SO_V1, "row.po_reference",
              "gold_etl_from_fm", superseded="2099-01-05T00:00:00Z")
        for handle, recorded in ((SO_V1, "2099-01-01T00:00:00Z"), (SO_V2, "2099-01-05T00:00:00Z")):
            sup = "2099-01-05T00:00:00Z" if handle == SO_V1 else None
            s.val(SO, "order_number", "identifier", "SO-2099-0042", handle, "row.order_number",
                  "gold_etl_from_fm", recorded=recorded, superseded=sup)
        s.val(SO, "customer_po", "text", "Jane at receiving", SO_V2, "row.po_reference",
              "gold_etl_from_fm", rank=1, recorded="2099-01-05T00:00:00Z")
        s.val(SO, "total_amount", "money", "1250.50", SO_V2, "row.total_amount",
              "gold_etl_from_fm", currency="CAD", markings='["finance"]',
              recorded="2099-01-05T00:00:00Z")
        s.val(SO, "ordered_quantity", "quantity", "1920", SO_V2, "row.qty", "gold_etl_from_fm",
              unit="each", role="ordered", recorded="2099-01-05T00:00:00Z")
        s.ident(SO, "so_number", "SO-2099-0042", "SO-2099-0042", "order_number", SO_V2,
                "row.order_number", "gold_etl_from_fm")
        s.val(SO, "fm_id", "integer", "9001", SO_V2, "row.fm_id", "gold_etl_from_fm",
              recorded="2099-01-05T00:00:00Z")
    if FM not in skip:
        s.obj(FM, "api:public.order_entry", FM_H, "fm_bronze")
        s.val(FM, "customer_po", "text", "PO 7781", FM_H, "row.custpocode", "fm_bronze")
        if SO not in skip:
            s.link("lnk-same-1", "SalesOrder.same_record_as", SO, FM, "verified", "same_record",
                   [SO_V2, FM_H],
                   [{"feature": "fm_id", "agrees": True,
                     "citations": [{"l1_handle": SO_V2, "field_path": "row.fm_id",
                                    "digest": _h("x"), "lineage": "gold_etl_from_fm"}]},
                    {"feature": "amount_close", "agrees": True,
                     "citations": [{"l1_handle": SO_V2, "field_path": "row.total_amount",
                                    "digest": _h("y"), "lineage": "gold_etl_from_fm"}]}])
    if INV not in skip:
        s.obj(INV, "bc:company_a/salesInvoices", INV_H, "bc_company_a")
        s.val(INV, "number", "identifier", "PSI2099001", INV_H, "row.number", "bc_company_a")
        s.val(INV, "external_document_number", "identifier", "SO-2099-0042", INV_H,
              "row.externalDocumentNumber", "bc_company_a")
        s.ident(INV, "bc_document_no", "PSI2099001", "PSI2099001", "number", INV_H,
                "row.number", "bc_company_a")
        s.ident(INV, "so_number", "SO-2099-0042", "SO-2099-0042", "external_document_number",
                INV_H, "row.externalDocumentNumber", "bc_company_a", role="alias")
        if SO not in skip:
            s.link("lnk-inv-1", "SalesInvoice.invoices_order", INV, SO, "verified", "fk",
                   [INV_H, SO_V2])
    if PO not in skip:
        s.obj(PO, "api:public.app_purchase_order", PO_H, "gold_etl_from_fm")
        s.val(PO, "po_number", "identifier", "PO100012", PO_H, "row.po_number",
              "gold_etl_from_fm")
        s.ident(PO, "petra_po", "PO100012", "PO100012", "po_number", PO_H, "row.po_number",
                "gold_etl_from_fm")
        s.ident(PO, "petra_po", "PO4821", "PO#4821", "po_number", PO_H,
                "row.custom_fields.fmPoNumber", "gold_etl_from_fm", role="alias")
    if PO2 not in skip:
        s.obj(PO2, "api:public.app_purchase_order", PO2_H, "gold_native_staging")
        s.ident(PO2, "petra_po", "PO100012", "PO 100012", "po_number", PO2_H,
                "row.custom_fields.renumberedTo", "gold_native_staging", role="alias")
    if BCPO not in skip:
        s.obj(BCPO, "bc:company_a/purchaseOrders", BCPO_H, "bc_company_a")
        s.ident(BCPO, "bc_document_no", "PO100012", "PO100012", "po_number", BCPO_H,
                "row.number", "bc_company_a")
    if MAIL not in skip:
        s.obj(MAIL, "mail:po-inbox@example.test", MAIL_H, "mail")
        s.val(MAIL, "subject", "text", "Order confirmation for blue widgets", MAIL_H,
              "row.subject", "mail")
    if DOC not in skip:
        s.obj(DOC, "sharepoint:drive-1", DOC_H, "sharepoint", recorded="2099-01-10T00:00:00Z",
              missing="2099-01-20T00:00:00Z")
        s.val(DOC, "file_name", "text", "Quality report 2099.pdf", DOC_H, "row.name",
              "sharepoint", recorded="2099-01-10T00:00:00Z")
    s.conn.commit()
    return s


POLICY = ot.ReadPolicy(
    tenant="plx",
    audiences={"api:*": {"sales", "finance_clerk"}, "bc:*": {"finance_clerk"},
               "mail:*": {"sales"}, "sharepoint:*": {"sales"}},
    markings={"finance": {"finance_clerk"}})
FULL = Actor("plx", frozenset({"sales", "finance_clerk"}))
SALES = Actor("plx", frozenset({"sales"}))
OUTSIDER = Actor("plx", frozenset({"nobody"}))
OTHER_TENANT = Actor("other", frozenset({"sales", "finance_clerk"}))


def gate_denying(*handles):
    """An item gate (the host's per-reader item ACL re-check) that denies these handles."""
    denied = set(handles)
    return lambda actor, handle: handle not in denied


def tools(store=None, policy=POLICY, **kwargs):
    store = store or build()
    kwargs.setdefault("schema", SCHEMA)
    kwargs.setdefault("grammar", FixtureGrammar())
    kwargs.setdefault("item_gate", gate_denying())
    return ot.OntologyTools(ot.StoreReader(store.conn), policy, clock=lambda: NOW, **kwargs)


def without_set(result):
    out = copy.deepcopy(result)
    out.pop("set", None)
    return out


class ContractMixin:
    def assertContract(self, tool, result):
        problems = validate(result, CONTRACTS["tools"][tool]["output"], root=CONTRACTS)
        self.assertEqual(problems, [], json.dumps(result)[:2000])
        self.assertLessEqual(ot._envelope_bytes(result), CONTRACTS["limits"]["max_result_bytes"])
        self.assertLessEqual(len(json.dumps(result).encode()),
                             CONTRACTS["limits"]["max_result_bytes"])

    def ok(self, t, tool, arguments, actor=FULL):
        result = t.call(tool, arguments, actor)
        self.assertContract(tool, result)
        self.assertNotIn("error", result, result)
        return result


# --------------------------------------------------------------------------- tests

class FixtureTests(unittest.TestCase):
    def test_fixture_domain_matches_the_frozen_format(self):
        self.assertEqual(validate(DOMAIN, FORMAT, root=FORMAT), [])


class FindTests(ContractMixin, unittest.TestCase):
    def setUp(self):
        self.t = tools()

    def test_exact_identifier_of_another_system_is_found_with_key_binding_reason_tier(self):
        result = self.ok(self.t, "plx.find", {"query": "PSI2099001"})
        self.assertEqual(result["status"], "found")
        (cand,) = result["candidates"]
        self.assertEqual(cand["object"]["object_id"], INV)
        self.assertEqual(cand["object"]["lineage"], "bc_company_a")
        self.assertEqual(cand["matched"]["identifier_kind"], "bc_document_no")
        self.assertEqual(cand["matched"]["binding"], "bc.sales_invoices")
        self.assertEqual(cand["matched"]["raw"]["origin"],
                         {"citation": INV_H, "field_path": "row.number"})
        self.assertEqual((cand["tier"], cand["reason"], cand["rank"]),
                         ("verified", "exact_identifier", 1))
        self.assertEqual(cand["object"]["display"]["untrusted_text"], "PSI2099001")
        self.assertTrue(result["completeness"]["complete"])
        self.assertEqual(self.t.resolve_set(result["set"], FULL), (INV,))

    def test_ambiguous_returns_ranked_candidates_and_never_a_winner(self):
        result = self.ok(self.t, "plx.find", {"query": "PO100012"})
        self.assertEqual(result["status"], "ambiguous")
        ids = [c["object"]["object_id"] for c in result["candidates"]]
        self.assertEqual(sorted(ids), sorted([PO, PO2, BCPO]))
        self.assertEqual([c["rank"] for c in result["candidates"]], [1, 2, 3])
        by_id = {c["object"]["object_id"]: c for c in result["candidates"]}
        # gold_native_staging is never authority: its match is a lead, ranked last.
        self.assertEqual(by_id[PO2]["tier"], "lead")
        self.assertEqual(ids[-1], PO2)
        self.assertEqual({by_id[PO]["matched"]["binding"], by_id[BCPO]["matched"]["binding"]},
                         {"portal.app_purchase_order", "bc.purchase_orders"})
        self.assertEqual(set(self.t.resolve_set(result["set"], FULL)), {PO, PO2, BCPO})
        self.assertIsNone(self.t.resolve_set(result["set"], SALES))

    def test_the_owning_systems_literal_match_ranks_before_a_normalized_one(self):
        """find-root-exact-compare (b): one exact rule (ontology_tools.exact_match over
        identifiers.json exact_compare). A typed match whose stored text is not the asked
        text ranks after an equally strong literal one, whatever its object id."""
        store = build()
        po0 = "PurchaseOrder:portal.app_purchase_order:po-0"
        po0_h = "api://public.app_purchase_order/%7B%22id%22%3A%22po-0%22%7D@0000000000000001-6"
        store.obj(po0, "api:public.app_purchase_order", po0_h, "gold_etl_from_fm")
        store.ident(po0, "petra_po", "PO100012", "po 100012", "po_number", po0_h,
                    "row.po_number", "gold_etl_from_fm")
        store.conn.commit()
        result = self.ok(tools(store), "plx.find", {"query": "PO100012"})
        ranks = {c["object"]["object_id"]: c["rank"] for c in result["candidates"]}
        same = {c["object"]["object_id"]: (c["tier"], c["reason"])
                for c in result["candidates"]}
        self.assertEqual(same[po0], same[PO])
        self.assertLess(ranks[PO], ranks[po0])

    def test_exact_match_follows_each_kinds_declared_compare(self):
        from ontology import identifiers
        self.assertTrue(ot.exact_match("petra_po", " PO100012 ", "po100012", identifiers))
        self.assertFalse(ot.exact_match("petra_po", "PO100012", "PO1000%", identifiers))
        self.assertTrue(ot.exact_match("so_number", "SO100201 ", " SO100201", identifiers))
        self.assertFalse(ot.exact_match("so_number", "SO100201", "so100201", identifiers))
        self.assertFalse(ot.exact_match("petra_po", "PO#4821", "PO4821", identifiers))
        # No grammar (or one without exact_compare): trimmed, case-sensitive only.
        self.assertFalse(ot.exact_match("petra_po", "PO100012", "po100012", None))
        self.assertTrue(ot.exact_match("petra_po", "PO100012", " PO100012", FixtureGrammar()))

    def test_alias_written_in_text_form_is_found_through_the_grammar(self):
        result = self.ok(self.t, "plx.find", {"query": "Our P.O. #4821"})
        self.assertEqual(result["status"], "found")
        cand = result["candidates"][0]
        self.assertEqual(cand["object"]["object_id"], PO)
        self.assertEqual((cand["reason"], cand["matched"]["role"]), ("alias_match", "alias"))
        self.assertEqual(cand["matched"]["raw"]["untrusted_text"], "PO#4821")

    def test_free_text_finds_mail_subject_and_file_name_as_leads(self):
        mail = self.ok(self.t, "plx.find", {"query": "confirmation for blue"})
        self.assertEqual(mail["status"], "found")
        self.assertEqual(mail["candidates"][0]["object"]["object_id"], MAIL)
        self.assertEqual((mail["candidates"][0]["tier"], mail["candidates"][0]["reason"]),
                         ("lead", "display_text"))
        doc = self.ok(self.t, "plx.find", {"query": "quality report"})
        self.assertEqual(doc["candidates"][0]["object"]["object_id"], DOC)

    def test_text_with_an_identifier_mention_returns_both_without_a_winner(self):
        result = self.ok(self.t, "plx.find", {"query": "Re: PO#4821 confirmation for blue"})
        self.assertEqual(result["status"], "ambiguous")
        by_id = {c["object"]["object_id"]: c for c in result["candidates"]}
        self.assertEqual(set(by_id), {PO, MAIL})
        self.assertEqual(result["candidates"][0]["object"]["object_id"], PO)
        self.assertEqual(by_id[MAIL]["tier"], "lead")

    def test_a_label_kind_conflict_secondary_is_searched_as_a_lead_never_exact(self):
        """men-2 open decision: "Customer PO: 4821" is a customer PO by its label, but
        the grammar also reads the Petra PO form "PO: 4821" (secondary,
        label_kind_conflict). plx.find searches that reading too, as a lead only."""
        from ontology import identifiers
        t = tools(grammar=identifiers)
        result = self.ok(t, "plx.find", {"query": "Customer PO: 4821"})
        (cand,) = [c for c in result["candidates"] if c["object"]["object_id"] == PO]
        self.assertEqual((cand["tier"], cand["reason"]), ("lead", "secondary_reading"))
        self.assertEqual(cand["why_not"], [{"feature": "label_kind_conflict", "agrees": False}])
        self.assertEqual((cand["matched"]["identifier_kind"], cand["matched"]["normalized"]),
                         ("petra_po", "PO4821"))
        # A PO shape under the label: every candidate is a lead, none exact.
        shaped = self.ok(t, "plx.find", {"query": "Customer PO: PO100012"})
        self.assertEqual(shaped["status"], "ambiguous")
        self.assertEqual({c["object"]["object_id"] for c in shaped["candidates"]}, {PO, PO2})
        self.assertTrue(all(c["tier"] == "lead" and c["reason"] == "secondary_reading"
                            for c in shaped["candidates"]))
        # Without the conflicting label the same value is the reading: exact, no why_not.
        plain = self.ok(t, "plx.find", {"query": "PO 4821"})
        (cand,) = plain["candidates"]
        self.assertEqual((cand["object"]["object_id"], cand["tier"], cand["reason"]),
                         (PO, "verified", "alias_match"))
        self.assertNotIn("why_not", cand)
        # A query holding the value as a reading too keeps the exact tier.
        both = self.ok(t, "plx.find", {"query": "Customer PO: 4821 for PO 4821"})
        (cand,) = [c for c in both["candidates"] if c["object"]["object_id"] == PO]
        self.assertEqual((cand["tier"], cand["reason"]), ("verified", "alias_match"))

    def test_a_label_whose_kind_aliases_the_shape_binds_the_typed_identifier(self):
        """"Business Central document PO100012": the label's kind (bc_document_no) aliases
        the Petra PO its shape reads, so the reading agrees with the label: the typed
        identifier binds as it does bare, never a secondary-reading lead."""
        from ontology import identifiers
        t = tools(grammar=identifiers)
        bare = self.ok(t, "plx.find", {"query": "PO100012"})
        labelled = self.ok(t, "plx.find", {"query": "Business Central document PO100012"})
        self.assertLessEqual({PO, PO2}, {c["object"]["object_id"] for c in labelled["candidates"]})
        self.assertEqual([(c["object"]["object_id"], c["tier"], c["reason"])
                          for c in labelled["candidates"]],
                         [(c["object"]["object_id"], c["tier"], c["reason"])
                          for c in bare["candidates"]])
        self.assertTrue(all(c["reason"] in ot.ROOT_REASONS and "why_not" not in c
                            for c in labelled["candidates"]))

    def test_identifier_kind_and_object_type_narrow_the_search(self):
        result = self.ok(self.t, "plx.find", {"query": "SO-2099-0042"})
        self.assertEqual(result["status"], "ambiguous")  # order + invoice alias
        narrowed = self.ok(self.t, "plx.find", {"query": "SO 2099 0042",
                                                "object_type": "SalesOrder"})
        self.assertEqual(narrowed["status"], "found")
        self.assertEqual(narrowed["candidates"][0]["object"]["object_id"], SO)

    def test_not_found_states_what_was_searched(self):
        result = self.ok(self.t, "plx.find", {"query": "PO 99999"})
        self.assertEqual(result["status"], "not_found")
        # Mail and SharePoint items carry their own ACLs: absence is not proven.
        (gap,) = [g for g in result["gaps"] if g["reason"] == "no_visible_match"]
        self.assertEqual(gap["state"], "unknown")
        self.assertEqual(gap["subject"], "query:PO 99999")
        self.assertEqual(gap["searched"]["queries"][0], "PO 99999")
        self.assertIn("petra_po:PO99999", gap["searched"]["queries"])
        self.assertEqual(gap["searched"]["sources"], ["bc", "graph_mail", "portal_api",
                                                      "sharepoint"])

    def test_absence_is_claimed_only_with_full_coverage_now_and_a_complete_scan(self):
        whole = ot.ReadPolicy(tenant="plx", audiences={"api:*": {"sales"}})
        result = self.ok(tools(policy=whole), "plx.find", {"query": "PO 99999"}, SALES)
        (gap,) = [g for g in result["gaps"] if g["reason"] == "no_match"]
        self.assertEqual((gap["state"], gap["searched"]["sources"]),
                         ("absent_in_source", ["portal_api"]))
        # One unit of the family taken out -> a permission limit,
        # never "absent in source", although the reader cannot see what is missing.
        partial = ot.ReadPolicy(tenant="plx", audiences={
            "api:*": {"sales"}, "api:public.app_purchase_order": set()})
        cut = self.ok(tools(policy=partial), "plx.find",
                      {"query": "PO100012", "object_type": "PurchaseOrder"}, SALES)
        self.assertEqual(cut["status"], "not_found")
        self.assertEqual([g["state"] for g in cut["gaps"] if "match" in g["reason"]], ["unknown"])
        past = self.ok(tools(policy=whole), "plx.find",
                       {"query": "PO 99999", "as_known_at": "2099-01-03"}, SALES)
        self.assertNotIn("absent_in_source", [g["state"] for g in past["gaps"]])

    def test_a_key_no_match_is_absent_only_over_every_identifier_carrier(self):
        """Wire-host decision A (one absence prover): a key no-match is unknown
        (no_match) unless access is proven and the search covered every identifier
        carrier of the type: a carrier kind narrowed away, or a carrier value the build
        could not parse, leaves absence unshown."""
        whole = ot.ReadPolicy(tenant="plx", audiences={"api:*": {"sales"}})
        result = self.ok(tools(policy=whole), "plx.find", {"query": "PO 99999"}, SALES)
        (gap,) = [g for g in result["gaps"] if g["reason"] == "no_match"]
        self.assertEqual(gap["state"], "absent_in_source")
        self.assertEqual(gap["searched"]["scopes"], ["PurchaseOrder.po_number"])
        # PO1000999 also reads as a Petra PO, the kind the PO number carrier holds:
        # narrowing the search to BC document numbers leaves that carrier unsearched.
        narrowed = self.ok(tools(policy=whole), "plx.find",
                           {"query": "PO 1000999", "object_type": "PurchaseOrder",
                            "identifier_kind": "bc_document_no"}, SALES)
        (gap,) = [g for g in narrowed["gaps"] if g["reason"] == "no_match"]
        self.assertEqual(gap["state"], "unknown")
        self.assertIn("PurchaseOrder.po_number", gap["detail"])
        full = self.ok(tools(policy=whole), "plx.find",
                       {"query": "PO 1000999", "object_type": "PurchaseOrder"}, SALES)
        self.assertEqual([g["state"] for g in full["gaps"] if g["reason"] == "no_match"],
                         ["absent_in_source"])
        # A carrier value the projector could not parse may be the asked key.
        store = build()
        store.conn.execute("INSERT INTO store_meta VALUES ('build_report', ?)", (json.dumps(
            {"gaps": [{"code": "parse_not_identifier",
                       "where": "PurchaseOrder.po_number@portal.app_purchase_order",
                       "count": 1, "samples": []}]}),))
        unparsed = self.ok(tools(store, policy=whole), "plx.find", {"query": "PO 99999"},
                           SALES)
        (gap,) = [g for g in unparsed["gaps"] if g["reason"] == "no_match"]
        self.assertEqual(gap["state"], "unknown")
        self.assertIn("PurchaseOrder.po_number", gap["detail"])
        # A query no carrier kind reads as searched no carrier: unknown, typed or not.
        for extra in ({}, {"object_type": "PurchaseOrder"}):
            words = self.ok(tools(policy=whole), "plx.find", {"query": "zzzz qqqq", **extra},
                            SALES)
            self.assertNotIn("absent_in_source", [g["state"] for g in words["gaps"]], extra)

    def test_a_kind_narrowed_away_without_a_type_never_proves_absence(self):
        """Review f732d09 M1: PO1000999 also reads as a Petra PO; narrowing to BC
        document numbers without object_type leaves PurchaseOrder.po_number unsearched,
        and a readable PO holding PO1000999 would be reported absent."""
        whole = ot.ReadPolicy(tenant="plx", audiences={"api:*": {"sales"}})
        store = build()
        po9, po9_h = PO.replace("po-1", "po-9"), PO_H.replace("po-1", "po-9")
        store.obj(po9, "api:public.app_purchase_order", po9_h, "gold_etl_from_fm")
        store.ident(po9, "petra_po", "PO1000999", "PO1000999", "po_number", po9_h,
                    "row.po_number", "gold_etl_from_fm")
        store.conn.commit()
        plain = self.ok(tools(store, policy=whole), "plx.find", {"query": "PO 1000999"}, SALES)
        self.assertEqual(plain["status"], "found")
        found = self.ok(tools(store, policy=whole), "plx.find",
                        {"query": "PO 1000999", "identifier_kind": "bc_document_no"}, SALES)
        (gap,) = [g for g in found["gaps"] if g["reason"] == "no_match"]
        self.assertEqual(gap["state"], "unknown")
        self.assertIn("PurchaseOrder.po_number", gap["detail"])

    def test_a_carrier_out_of_the_readers_sources_or_degraded_was_not_searched(self):
        """SalesInvoice is bound only to BC. A reader
        granted the Portal API alone never searched it, so a BC document number no-match
        is unknown, typed or not; a degraded carrier was not searched either."""
        api_only = ot.ReadPolicy(tenant="plx", audiences={"api:*": {"sales"}})
        for extra in ({}, {"object_type": "SalesInvoice"}):
            result = self.ok(tools(policy=api_only), "plx.find",
                             {"query": "PSI 1000999", **extra}, SALES)
            (gap,) = [g for g in result["gaps"] if g["reason"] == "no_match"]
            self.assertEqual(gap["state"], "unknown", extra)
            self.assertIn("SalesInvoice.number", gap["detail"], extra)
        document = copy.deepcopy(DOMAIN)
        po = next(t for t in document["object_types"] if t["id"] == "PurchaseOrder")
        po["properties"][0].update(availability="degraded",
                                   availability_reason="Fixture: partly served.")
        degraded = ot.SchemaView.from_documents([document])
        result = self.ok(tools(policy=api_only, schema=degraded), "plx.find",
                         {"query": "PO 99999"}, SALES)
        (gap,) = [g for g in result["gaps"] if g["reason"] == "no_match"]
        self.assertEqual(gap["state"], "unknown")
        self.assertIn("PurchaseOrder.po_number", gap["detail"])
        # A typed no-match names the type it searched in its subject.
        typed = self.ok(tools(policy=api_only), "plx.find",
                        {"query": "PO 99999", "object_type": "PurchaseOrder"}, SALES)
        (gap,) = [g for g in typed["gaps"] if g["reason"] == "no_match"]
        self.assertEqual((gap["state"], gap["subject"]),
                         ("absent_in_source", "PurchaseOrder query:PO 99999"))

    def test_a_key_match_a_filter_excludes_is_never_absent(self):
        """Review f732d09 M3: the order exists and its key matches; a filter that
        excludes it says nothing about the source holding it."""
        whole = ot.ReadPolicy(tenant="plx", audiences={"api:*": {"sales"}})
        result = self.ok(tools(policy=whole), "plx.find", {
            "query": "SO-2099-0042", "object_type": "SalesOrder",
            "filters": [{"property": "ordered_quantity", "op": "gte",
                         "value": "999999999"}]}, SALES)
        self.assertEqual(result["status"], "not_found")
        states = {g["reason"]: g["state"] for g in result["gaps"]}
        self.assertNotIn("absent_in_source", states.values())
        self.assertEqual(states.get("excluded_by_filter"), "unknown")

    def test_filters_list_objects_of_one_type(self):
        result = self.ok(self.t, "plx.find", {"object_type": "SalesOrder", "filters": [
            {"property": "ordered_quantity", "op": "gte", "value": "1000"}]})
        self.assertEqual([c["object"]["object_id"] for c in result["candidates"]], [SO])
        none = self.ok(self.t, "plx.find", {"object_type": "SalesOrder", "filters": [
            {"property": "ordered_quantity", "op": "lt", "value": "1000"}]})
        self.assertEqual(none["status"], "not_found")

    def test_a_filter_only_no_match_is_absent_only_when_the_filtered_carrier_was_read(self):
        """A filter-only no-match is absent_in_source over a served property read in
        full; a degraded one (or one outside the reader's sources) was not, so unknown."""
        whole = ot.ReadPolicy(tenant="plx", audiences={"api:*": {"sales"}})
        asked = {"object_type": "SalesOrder", "filters": [
            {"property": "ordered_quantity", "op": "lt", "value": "1"}]}
        served = self.ok(tools(policy=whole), "plx.find", asked, SALES)
        (gap,) = [g for g in served["gaps"] if g["reason"] == "no_match"]
        self.assertEqual(gap["state"], "absent_in_source")
        document = copy.deepcopy(DOMAIN)
        order = next(t for t in document["object_types"] if t["id"] == "SalesOrder")
        next(p for p in order["properties"] if p["name"] == "ordered_quantity").update(
            availability="degraded", availability_reason="Fixture: partly served.")
        degraded = self.ok(tools(policy=whole, schema=ot.SchemaView.from_documents([document])),
                           "plx.find", asked, SALES)
        (gap,) = [g for g in degraded["gaps"] if g["reason"] == "no_match"]
        self.assertEqual(gap["state"], "unknown")
        self.assertIn("not read in full", gap["detail"])

    def test_not_served_type_is_an_actionable_error(self):
        result = self.t.call("plx.find", {"object_type": "Formulation", "filters": [
            {"property": "code", "op": "eq", "value": "F-1"}]}, FULL)
        self.assertContract("plx.find", result)
        self.assertEqual(result["error"]["code"], "not_served")

    def test_paging_keeps_ambiguous_pages_at_two_or_more_and_cursor_is_bound(self):
        first = self.ok(self.t, "plx.find", {"query": "PO100012", "limit": 1})
        self.assertEqual(first["status"], "ambiguous")
        self.assertGreaterEqual(len(first["candidates"]), 2)
        self.assertFalse(first["completeness"]["complete"])
        cursor = first["completeness"]["next_cursor"]
        second = self.ok(self.t, "plx.find", {"query": "PO100012", "limit": 1, "cursor": cursor})
        self.assertEqual(second["candidates"][-1]["rank"], 3)
        self.assertTrue(second["completeness"]["complete"])
        other = self.t.call("plx.find", {"query": "PO4821", "cursor": cursor}, FULL)
        self.assertEqual(other["error"]["code"], "invalid_cursor")

    def test_invalid_input_is_refused_by_the_contract(self):
        result = self.t.call("plx.find", {"query": "x", "bogus": 1}, FULL)
        self.assertContract("plx.find", result)
        self.assertEqual(result["error"]["code"], "invalid_input")

    def test_without_grammar_literal_identifiers_still_resolve(self):
        t = tools(grammar=None)
        result = self.ok(t, "plx.find", {"query": "psi2099001"})
        self.assertEqual(result["candidates"][0]["object"]["object_id"], INV)
        self.assertEqual(result["candidates"][0]["tier"], "lead")
        self.assertIn("grammar_unavailable", [g["reason"] for g in result["gaps"]])


class PermissionTests(ContractMixin, unittest.TestCase):
    QUERIES = ({"query": "PSI2099001"}, {"query": "PO100012"}, {"query": "SO-2099-0042"},
               {"query": "confirmation for blue"}, {"query": "PO 99999"},
               {"object_type": "SalesOrder", "filters": [{"property": "ordered_quantity",
                                                          "op": "present"}]})

    def test_denied_actor_sees_nothing_and_counts_nothing(self):
        full, empty = tools(), tools(build(skip={SO, FM, INV, PO, PO2, BCPO, MAIL, DOC}))
        for actor in (OUTSIDER, OTHER_TENANT):
            for arguments in self.QUERIES:
                denied = full.call("plx.find", arguments, actor)
                self.assertContract("plx.find", denied)
                self.assertEqual(denied["status"], "not_found")
                self.assertEqual(denied, empty.call("plx.find", arguments, actor))
                self.assertEqual(denied["withheld"],
                                 {"properties": None, "objects": None,
                                  "reason": "marking_and_audience"})

    def test_partial_grant_equals_a_store_without_the_denied_rows(self):
        # SALES has no BC grant: the invoice and the BC purchase order do not exist for it,
        # not even as a lower count, a shorter page or a different status.
        full, cut = tools(), tools(build(skip={INV, BCPO}))
        for arguments in self.QUERIES:
            self.assertEqual(without_set(full.call("plx.find", arguments, SALES)),
                             without_set(cut.call("plx.find", arguments, SALES)), arguments)
        result = full.call("plx.find", {"query": "SO-2099-0042"}, SALES)
        self.assertEqual(result["status"], "found")  # the invoice alias is not a rival

    def test_marked_value_is_not_searchable_without_the_marking(self):
        result = tools().call("plx.find", {"object_type": "SalesOrder", "filters": [
            {"property": "total_amount", "op": "present"}]}, SALES)
        self.assertEqual(result["status"], "not_found")
        found = tools().call("plx.find", {"object_type": "SalesOrder", "filters": [
            {"property": "total_amount", "op": "present"}]}, FULL)
        self.assertEqual(found["status"], "found")

    def test_audience_keys_are_read_once_per_store_change(self):
        """The store's audience keys (each call's scope) are kept until the store changes:
        a commit on another connection shows its new audience at the next call."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.sqlite"
            with closing(sqlite3.connect(path)) as writer:
                store_ddl.create(writer)
                insert = "INSERT INTO objects VALUES (?,?,?,?,?,?,?)"
                writer.execute(insert, ("PurchaseOrder:b:1", "PurchaseOrder", "b", "[1]",
                                        "portal_api:x", "2099-01-01T00:00:00Z", None))
                writer.commit()
                reader = ot.StoreReader.open(path)
                self.addCleanup(reader.conn.close)
                self.assertEqual(reader.audience_keys(), ["portal_api:x"])
                scans = []
                reader.conn.set_trace_callback(
                    lambda sql: scans.append(sql) if "DISTINCT audience_key" in sql else None)
                self.assertEqual(reader.audience_keys(), ["portal_api:x"])
                self.assertEqual(scans, [])
                writer.execute(insert, ("PurchaseOrder:b:2", "PurchaseOrder", "b", "[2]",
                                        "bc:y", "2099-01-01T00:00:00Z", None))
                writer.commit()
                self.assertEqual(reader.audience_keys(), ["bc:y", "portal_api:x"])
                self.assertEqual(len(scans), 1)

    def test_object_outside_the_audience_reads_like_a_missing_object(self):
        t = tools()
        denied = t.call("plx.object", {"object_id": INV}, SALES)
        missing = t.call("plx.object", {"object_id": "SalesInvoice:bc.sales_invoices:nope"},
                         SALES)
        self.assertContract("plx.object", denied)
        self.assertEqual(denied, missing)
        self.assertEqual(denied["error"]["code"], "object_not_found")

    def test_markings_trim_values_before_counting_and_links_to_hidden_objects_vanish(self):
        t = tools()
        result = self.ok(t, "plx.object", {"object_id": SO}, SALES)
        self.assertNotIn("total_amount", [v["property"] for v in result["values"]])
        self.assertEqual(result["withheld"], {"properties": 1, "objects": None,
                                              "reason": "marking"})
        self.assertNotIn("1250.50", json.dumps(result))
        self.assertEqual(result["linked_counts"], [])  # the invoice is BC, not granted
        full = self.ok(t, "plx.object", {"object_id": SO}, FULL)
        self.assertNotIn("withheld", full)
        self.assertEqual(full["linked_counts"],
                         [{"link_type": "SalesInvoice.invoices_order", "direction": "reverse",
                           "count": 1, "tiers": {"verified": 1}}])


class ObjectTests(ContractMixin, unittest.TestCase):
    def setUp(self):
        self.t = tools()

    def test_per_source_values_with_lineage_citations_and_envelopes(self):
        result = self.ok(self.t, "plx.object", {"object_id": SO, "response_format": "detailed"})
        values = {v["property"]: v for v in result["values"]}
        self.assertEqual(values["total_amount"]["value"], "1250.50")
        self.assertEqual(values["total_amount"]["currency"], "CAD")
        self.assertEqual(values["total_amount"]["markings"], ["finance"])
        self.assertEqual(values["ordered_quantity"]["quantity_role"], "ordered")
        text = values["customer_po"]
        # REC-07: a po_reference value that is not PO-like (a person's name) is withheld,
        # its citation kept.
        self.assertEqual(text["value"], {"untrusted_text": "",
                                         "origin": {"citation": SO_V2,
                                                    "field_path": "row.po_reference"},
                                         "withheld": "not_po_like"})
        self.assertNotIn("Jane", json.dumps(result))
        self.assertEqual(text["citation"]["lineage"], "gold_etl_from_fm")
        self.assertEqual(text["authority_rank"], 1)
        for value in result["values"]:
            self.assertEqual(value["citation"]["l1_handle"], SO_V2)
            self.assertRegex(value["citation"]["field_path"], r"^row\.")

    def test_values_cite_handles_claim_checks_accept(self):
        from operating import claim_checks
        from operating.case_runtime import SUPPORTED_SCHEMES
        result = self.ok(self.t, "plx.object", {"object_id": SO})
        for value in result["values"]:
            self.assertTrue(value["citation"]["l1_handle"].startswith(SUPPORTED_SCHEMES))
            self.assertTrue(claim_checks._FIELD.match(value["citation"]["field_path"]))

    def test_conflict_with_identity_peer_is_reported_never_resolved(self):
        result = self.ok(self.t, "plx.object", {"object_id": SO})
        (conflict,) = result["conflicts"]
        self.assertEqual((conflict["kind"], conflict["subject"]),
                         ("value_disagreement", "SalesOrder.customer_po"))
        texts = sorted((v["value"]["untrusted_text"], v["value"].get("withheld"))
                       for v in conflict["values"])
        # The po_reference side is withheld (REC-07); the disagreement is still reported.
        self.assertEqual(texts, [("", "not_po_like"), ("PO 7781", None)])
        self.assertNotIn("Jane", json.dumps(result))
        (edge,) = result["identity"]
        self.assertEqual((edge["from"], edge["to"], edge["tier"]), (SO, FM, "verified"))
        self.assertNotIn("citations", edge["reason"][0])  # concise
        detailed = self.ok(self.t, "plx.object", {"object_id": SO, "response_format": "detailed"})
        self.assertIn("citations", detailed["identity"][0]["reason"][0])
        self.assertIn("citations", detailed["identity"][0]["reason"][1])
        # A citation of a value the reader may not read is not echoed by the link.
        sales = self.ok(self.t, "plx.object", {"object_id": SO, "response_format": "detailed"},
                        SALES)
        reason = sales["identity"][0]["reason"]
        self.assertIn("citations", reason[0])
        self.assertNotIn("citations", reason[1])
        self.assertNotIn("row.total_amount", json.dumps(sales))

    def test_dropped_bc_versions_surface_a_gap_not_fewer_versions_silently(self):
        store = build()
        binding = SO.split(":")[1]
        store.conn.execute("INSERT INTO store_meta VALUES ('build_report', ?)", (json.dumps(
            {"gaps": [{"code": "bc_company_unknown", "where": binding, "count": 3,
                       "samples": ["bc:x"]}]}),))
        t = tools(store)
        result = self.ok(t, "plx.object", {"object_id": SO})
        (gap,) = [g for g in result["gaps"]
                  if g["reason"] == "versions_dropped_bc_company_unknown"]
        self.assertEqual((gap["state"], gap["subject"]), ("unknown", SO))
        self.assertNotIn("3", gap["detail"])  # no count
        clean = self.ok(self.t, "plx.object", {"object_id": SO})
        self.assertFalse([g for g in clean["gaps"]
                          if g["reason"] == "versions_dropped_bc_company_unknown"])

    def test_as_known_at_reads_the_version_known_then(self):
        result = self.ok(self.t, "plx.object", {"object_id": SO, "as_known_at": "2099-01-03"})
        values = {v["property"]: v for v in result["values"]}
        # The old version's po_reference ("old reference", not PO-like) is withheld; its
        # citation names the version known then.
        self.assertEqual(values["customer_po"]["value"],
                         {"untrusted_text": "", "withheld": "not_po_like",
                          "origin": {"citation": SO_V1, "field_path": "row.po_reference"}})
        self.assertNotIn("old reference", json.dumps(result))
        self.assertEqual(values["order_number"]["citation"]["l1_handle"], SO_V1)
        self.assertEqual(result["meta"]["as_known_at"], "2099-01-03T23:59:59.999Z")
        early = self.t.call("plx.object", {"object_id": DOC, "as_known_at": "2099-01-03"}, FULL)
        self.assertEqual(early["error"]["code"], "object_not_found")

    def test_gaps_name_not_served_missing_and_unvalued_properties(self):
        result = self.ok(self.t, "plx.object", {"object_id": SO})
        reasons = {(g["reason"], g["subject"]) for g in result["gaps"]}
        self.assertIn(("not_served", "SalesOrder.promised_date"), reasons)
        doc = self.ok(self.t, "plx.object", {"object_id": DOC})
        self.assertIn("missing_from_source", [g["reason"] for g in doc["gaps"]])
        asked = self.ok(self.t, "plx.object", {"object_id": INV, "properties": ["amount"]})
        # No stored read of a property never reads as absent in the source.
        self.assertIn(("unknown", "no_value", "SalesInvoice.amount"),
                      {(g["state"], g["reason"], g["subject"]) for g in asked["gaps"]})

    def test_freshness_and_stale_source_become_gaps(self):
        store = build()
        store.conn.execute("UPDATE store_meta SET value = ? WHERE key = 'freshness'",
                           (json.dumps({"portal_api": {"captured_at": "2099-01-01T00:00:00Z",
                                                       "completeness": "partial",
                                                       "stale": True}}),))
        result = self.ok(tools(store), "plx.object", {"object_id": SO})
        self.assertEqual(result["meta"]["freshness"][0]["source"], "portal_api")
        self.assertIn(("stale", "stale_source"),
                      {(g["state"], g["reason"]) for g in result["gaps"]})

    def test_a_capture_older_than_the_hosts_bound_is_stale_once_with_its_age(self):
        # A projected source is judged by the host's
        # capture bound (its ingest cadence); past it, one stale_source with its age.
        store = build()

        def answer(captured_at, bounds, **entry):
            store.conn.execute("UPDATE store_meta SET value = ? WHERE key = 'freshness'", (
                json.dumps([{"source": "portal_api", "captured_at": captured_at,
                             "completeness": "complete", "stale": False, **entry}]),))
            result = self.ok(tools(store, freshness_bounds=bounds), "plx.object",
                             {"object_id": SO})
            (fresh,) = [f for f in result["meta"]["freshness"] if f["source"] == "portal_api"]
            return fresh, [(g["reason"], g["subject"], g["detail"]) for g in result["gaps"]
                           if g["state"] == "stale"]

        old = "2099-02-01T09:30:00Z"   # 9000 s before NOW
        fresh, stale = answer(old, {"portal_api": 3480})
        label = ("host captures as of 2099-02-01T09:30:00Z, 9000 s old, past this host's "
                 "capture bound of 3480 s")
        self.assertEqual((fresh["stale"], fresh["label"]), (True, label))
        self.assertEqual(stale, [("stale_source", "portal_api", label)])
        # Within the bound, and with no bound configured, the capture is not stale.
        for bounds in ({"portal_api": 9001}, {}, {"graph_mail": 60}):
            fresh, stale = answer(old, bounds)
            self.assertEqual((fresh["stale"], "label" in fresh, stale), (False, False, []))
        # A copy served while the live source was out keeps its own label.
        outage = ("source-mirror captures as of 2099-02-01T09:30:00Z, live source "
                  "unavailable (500, internal)")
        fresh, stale = answer(old, {"portal_api": 60}, stale=True, label=outage,
                              served_from="persisted_capture")
        self.assertEqual(stale, [("stale_source", "portal_api", outage)])

    def test_a_find_over_many_copied_units_keeps_whole_labels_within_the_contract(self):
        # Live defect (v6 runs): six host-capture labels joined and cut at 599 chars broke
        # the "host captures as of" label bound, so every free-text plx.find erred with
        # contract_violation. The joined label now holds whole labels, then "+N more".
        from integrations.mirror_capture import capture_label, stale_label
        from ontology.resolve import Resolution, scope_entry

        pattern = CONTRACTS["$defs"]["freshness"]["properties"]["label"]["pattern"]
        tail = int(re.search(r"\{1,(\d+)\}\$$", pattern).group(1))

        def realistic(i):   # the longest each real label form gets
            at = f"2026-10-26T23:35:{i % 60:02d}.380798Z"
            return (capture_label(at, 10 ** 9 + i, 10 ** 9) if i % 2 else
                    stale_label(at, None, f"{i:02d}" + "x" * 62))

        def maximal(i):      # the longest label the contract's pattern accepts
            prefix = "host captures as of " if i % 2 else "source-mirror captures as of "
            return prefix + f"{i:02d}" + "y" * (tail - 2)

        class Copies:
            def __init__(self, labels):
                self.labels = labels

            def resolve(self, *args, **kwargs):
                units = {f"public.rel_{i:02d}": scope_entry(
                    f"public.rel_{i:02d}", "cached_mirror" if i % 2 else "persisted_capture",
                    f"2026-10-26T23:35:{i % 60:02d}Z", label=label, http_status=None,
                    error_code="source_unavailable") for i, label in enumerate(self.labels)}
                return Resolution(None, {}, 0, [], ("portal_api",), 1,
                                  scope_freshness={"portal_api": units})

        # 60 realistic units: the cardinality probe (32 listed units of maximal labels
        # pass the response bound and fail closed as result_too_large instead).
        for form, counts in ((realistic, [*range(1, 13), 60]), (maximal, range(1, 13))):
            for count in counts:
                labels = [form(i) for i in range(count)]
                self.assertTrue(all(re.search(pattern, label) for label in labels))
                result = self.ok(tools(sources=Copies(labels)), "plx.find",
                                 {"query": "PO999999"})
                (fresh,) = [f for f in result["meta"]["freshness"]
                            if f["source"] == "portal_api"]
                parts = fresh["label"].split("; ")
                more = re.fullmatch(r"\+(\d+) more", parts[-1])
                shown = parts[:-1] if more else parts
                left = int(more.group(1)) if more else 0
                # Whole labels only, in order, and the count names every one left out.
                self.assertEqual(shown, sorted(labels)[:len(shown)], (form, count))
                if form is realistic or count == 1:
                    self.assertEqual(len(shown) + left, count, (form, count))
                    self.assertTrue(len(shown) == count or more, (form, count))
                else:   # no room for a count beside one maximal label: the first, whole
                    self.assertEqual((shown, left), (sorted(labels)[:1], 0))
                # The full detail stays structured: every unit's whole label (up to the
                # contract's 32 listed units).
                listed = {u["relation"]: u.get("label") for u in fresh["relations"]}
                self.assertEqual(len(listed), min(count, 32))
                for relation, label in listed.items():
                    self.assertEqual(label, labels[int(relation.rsplit("_", 1)[1])])
                self.assertIn(("stale", "stale_source", "portal_api", fresh["label"]),
                              {(g["state"], g["reason"], g["subject"], g.get("detail"))
                               for g in result["gaps"]})

    def test_a_relation_the_live_source_refuses_is_inaccessible_although_a_copy_is_held(self):
        # The source no longer serves a relation the mirror still holds rows of: the copy
        # is stale, and the relation itself is inaccessible now (a refusal, not an
        # outage); a held copy kept for an outage is only stale.
        store = build()

        def gaps(code):
            store.conn.execute("UPDATE store_meta SET value = ? WHERE key = 'freshness'", (
                json.dumps([{"source": "portal_api", "captured_at": "2099-01-01T00:00:00Z",
                             "completeness": "complete", "stale": True,
                             "served_from": "cached_mirror",
                             "relations": [{"relation": "public.order_entry",
                                            "served_from": "cached_mirror",
                                            "captured_at": "2099-01-01T00:00:00Z",
                                            "http_status": None, "error_code": code}]}]),))
            result = self.ok(tools(store), "plx.object", {"object_id": SO})
            return {(g["state"], g["reason"], g["subject"]) for g in result["gaps"]
                    if g["subject"] in ("portal_api", "public.order_entry")}

        self.assertEqual(gaps("relation_unavailable"), {
            ("stale", "stale_source", "portal_api"),
            ("inaccessible", "relation_unavailable", "public.order_entry")})
        self.assertEqual(gaps("source_unavailable"),
                         {("stale", "stale_source", "portal_api")})

    def test_large_object_is_cut_to_48kb_and_says_so(self):
        store = build()
        for index in range(300):
            store.val(SO, f"note_{index:03d}", "text", "x" * 900, SO_V2, f"row.note_{index}",
                      "gold_etl_from_fm", recorded="2099-01-05T00:00:00Z")
        result = self.ok(tools(store), "plx.object", {"object_id": SO})
        self.assertTrue(result["completeness"]["truncated"])
        self.assertEqual(result["completeness"]["reason"], "result_size")
        self.assertEqual(result["completeness"]["total"], 305)  # 5 fixture values + 300
        self.assertGreater(result["completeness"]["returned"], 10)

    def test_long_source_text_is_enveloped_and_truncated(self):
        store = build()
        store.val(MAIL, "body", "text", "y" * 30000, MAIL_H, "row.body", "mail")
        result = self.ok(tools(store), "plx.object", {"object_id": MAIL})
        body = [v for v in result["values"] if v["property"] == "body"][0]["value"]
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["untrusted_text"]), CONTRACTS["limits"]["max_untrusted_text_chars"])

    def test_malformed_store_value_is_withheld_with_a_gap(self):
        store = build()
        store.val(SO, "weird_amount", "decimal", "1,250.5", SO_V2, "row.weird",
                  "gold_etl_from_fm", recorded="2099-01-05T00:00:00Z")
        result = self.ok(tools(store), "plx.object", {"object_id": SO})
        self.assertNotIn("weird_amount", [v["property"] for v in result["values"]])
        self.assertIn(("unparseable_value", "SalesOrder.weird_amount"),
                      {(g["reason"], g["subject"]) for g in result["gaps"]})


class ReviewL81Tests(ContractMixin, unittest.TestCase):
    """Regressions for deep review l8-1 (each reproduced the defect before the fix)."""

    def _mails(self, store, count, *, markings="[]", prefix="h", subject="blue widgets"):
        handles = []
        for index in range(count):
            oid = f"MailMessage:mail.po_inbox:{prefix}{index}"
            handle = f"graph-mail://mbx-1/{prefix}{index}@ck"
            store.obj(oid, "mail:po-inbox@example.test", handle, "mail", markings=markings)
            store.val(oid, "subject", "text", f"{subject} {prefix}{index}", handle, "row.subject",
                      "mail", markings=markings)
            handles.append(handle)
        return handles

    def test_hidden_rows_never_fill_the_text_scan_cap(self):
        def store(hidden):
            s = build()
            self._mails(s, 1, prefix="zz", subject="more blue widgets")
            if hidden == "marked":
                self._mails(s, 450, markings='["finance"]')
            elif hidden == "item_acl":
                return s, self._mails(s, 450)
            return s, []

        clean = tools(store(None)[0]).call("plx.find", {"query": "blue widgets"}, SALES)
        self.assertEqual(clean["status"], "ambiguous")
        self.assertEqual({c["object"]["object_id"] for c in clean["candidates"]},
                         {MAIL, "MailMessage:mail.po_inbox:zz0"})
        marked = tools(store("marked")[0]).call("plx.find", {"query": "blue widgets"}, SALES)
        self.assertContract("plx.find", marked)
        self.assertEqual(without_set(marked), without_set(clean))
        acl_store, denied = store("item_acl")
        gated = tools(acl_store, item_gate=gate_denying(*denied)).call(
            "plx.find", {"query": "blue widgets"}, SALES)
        self.assertEqual(without_set(gated), without_set(clean))

    def test_a_capped_text_scan_reports_no_exact_total(self):
        s = build()
        self._mails(s, 3, prefix="v")
        original = ot.TEXT_SCAN_CAP
        ot.TEXT_SCAN_CAP = 2
        try:
            result = self.ok(tools(s), "plx.find", {"query": "blue widgets", "limit": 1})
        finally:
            ot.TEXT_SCAN_CAP = original
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["completeness"]["total"], None)
        self.assertEqual(result["completeness"]["reason"], "scan_cap")
        self.assertTrue(result["completeness"]["truncated"])
        self.assertIn("scan_cap", [g["reason"] for g in result["gaps"]])

    def test_filter_listing_uses_the_same_tier_rule_and_drops_no_object(self):
        by_id = {}
        for op in ("absent", "present"):   # between them, every PurchaseOrder
            result = self.ok(tools(), "plx.find", {"object_type": "PurchaseOrder", "filters": [
                {"property": "po_number", "op": op}], "limit": 50})
            by_id.update({c["object"]["object_id"]: c
                          for c in result.get("candidates") or [result]
                          if "object" in c})
        self.assertEqual(set(by_id), {PO, PO2, BCPO})
        self.assertEqual(by_id[PO2]["tier"], "lead")  # gold_native_staging is never authority
        self.assertEqual(by_id[PO]["tier"], "verified")
        self.assertEqual(by_id[PO2]["matched"]["identifier_kind"], "object_key")

    def test_filter_listing_pages_through_every_match_beyond_five_hundred(self):
        s = build()
        for index in range(602):
            oid = f"PurchaseOrder:portal.app_purchase_order:bulk-{index:04d}"
            handle = f"api://public.app_purchase_order/bulk-{index:04d}@0000000000000001-9"
            s.obj(oid, "api:public.app_purchase_order", handle, "gold_etl_from_fm")
            s.val(oid, "po_number", "identifier", f"PO{200000 + index}", handle,
                  "row.po_number", "gold_etl_from_fm")
        t = tools(s)
        arguments = {"object_type": "PurchaseOrder",
                     "filters": [{"property": "po_number", "op": "present"}], "limit": 50}
        seen, cursor = set(), None
        while True:
            page = self.ok(t, "plx.find", dict(arguments, **({"cursor": cursor} if cursor else {})))
            self.assertEqual(page["completeness"]["total"], 603)
            seen |= {c["object"]["object_id"] for c in page["candidates"]}
            cursor = page["completeness"]["next_cursor"]
            if cursor is None:
                self.assertTrue(page["completeness"]["complete"])
                break
        self.assertEqual(len(seen), 603)
        original = ot.FILTER_SCAN_CAP
        ot.FILTER_SCAN_CAP = 100
        try:
            capped = self.ok(t, "plx.find", arguments)
        finally:
            ot.FILTER_SCAN_CAP = original
        self.assertEqual((capped["completeness"]["total"], capped["completeness"]["reason"]),
                         (None, "scan_cap"))

    def test_as_known_at_never_reads_around_a_later_marking(self):
        s = build()
        oid = "PurchaseOrder:portal.app_purchase_order:po-3"
        v1 = "api://public.app_purchase_order/po-3@0000000000000001-6"
        v2 = "api://public.app_purchase_order/po-3@0000000000000001-7"
        s.obj(oid, "api:public.app_purchase_order", v1, "gold_etl_from_fm",
              superseded="2099-01-10T00:00:00Z")
        s.obj(oid, "api:public.app_purchase_order", v2, "gold_etl_from_fm",
              markings='["finance"]', recorded="2099-01-10T00:00:00Z", add_object=False)
        s.val(oid, "po_number", "identifier", "PO3333", v1, "row.po_number", "gold_etl_from_fm",
              superseded="2099-01-10T00:00:00Z")
        s.ident(oid, "petra_po", "PO3333", "PO3333", "po_number", v1, "row.po_number",
                "gold_etl_from_fm")
        t = tools(s)
        for at in (None, "2099-01-05"):
            extra = {"as_known_at": at} if at else {}
            obj = t.call("plx.object", dict({"object_id": oid}, **extra), SALES)
            self.assertEqual(obj["error"]["code"], "object_not_found", at)
            found = t.call("plx.find", dict({"query": "PO 3333"}, **extra), SALES)
            self.assertEqual(found["status"], "not_found", at)
        self.assertEqual(t.call("plx.find", {"query": "PO 3333", "as_known_at": "2099-01-05"},
                                FULL)["status"], "found")

    def test_item_acl_is_rechecked_per_reader_and_denies_by_default(self):
        gated = tools(item_gate=gate_denying(DOC_H))
        cut = tools(build(skip={DOC}))
        for arguments in ({"query": "quality report"}, {"query": "confirmation for blue"}):
            self.assertEqual(without_set(gated.call("plx.find", arguments, SALES)),
                             without_set(cut.call("plx.find", arguments, SALES)))
        self.assertEqual(gated.call("plx.object", {"object_id": DOC}, SALES)["error"]["code"],
                         "object_not_found")
        ungated = tools(item_gate=None)
        self.assertEqual(ungated.call("plx.find", {"query": "confirmation for blue"},
                                      FULL)["status"], "not_found")
        self.assertEqual(ungated.call("plx.object", {"object_id": MAIL}, FULL)["error"]["code"],
                         "object_not_found")
        self.assertEqual(ungated.call("plx.find", {"query": "PSI2099001"}, FULL)["status"],
                         "found")  # bc:// records carry no item ACL

    def test_case_runtime_item_gate_denies_on_any_refusal(self):
        class Runtime:
            def resolve_citation(self, handle, actor):
                if "denied" in handle:
                    raise PermissionError("employee_acl_denied")
                return {"citation": handle}

        gate = ot.item_gate_from_case_runtime(Runtime())
        self.assertTrue(gate(SALES, "sharepoint://drive-1/ok@e"))
        self.assertFalse(gate(SALES, "sharepoint://drive-1/denied@e"))

    def test_unreadable_store_time_is_a_gap_not_a_silent_drop(self):
        s = build()
        s.val(SO, "broken_count", "integer", "5", SO_V2, "row.broken", "gold_etl_from_fm",
              recorded="garbage")
        result = self.ok(tools(s), "plx.object", {"object_id": SO})
        self.assertIn(("unreadable_store_time", "SalesOrder.broken_count"),
                      {(g["reason"], g["subject"]) for g in result["gaps"]})


class UnfamiliarOrderTests(ContractMixin, unittest.TestCase):
    """case.unfamiliar_order shape: from a BC sales invoice number to the Portal
    order through the schema's declared key binding; no step names a kind."""

    def test_invoice_number_reaches_the_portal_order_via_declared_key_binding(self):
        t = tools()
        start = self.ok(t, "plx.find", {"query": "PSI2099001"})
        self.assertEqual(start["status"], "found")
        root = start["candidates"][0]["object"]
        # Every declared link type leaving the root's type, with its key pairs.
        links = [l for l in SCHEMA.link_types.values() if l["from"] == root["object_type"]]
        found = []
        for link in links:
            for binding in link["bindings"]:
                if binding["kind"] not in ("fk", "json_path"):
                    continue
                for pair in binding["pairs"]:
                    page = self.ok(t, "plx.object", {"object_id": root["object_id"]})
                    for value in page["values"]:
                        if value["citation"]["field_path"] == f"row.{pair['from']}":
                            hit = self.ok(t, "plx.find", {"query": value["value"],
                                                          "object_type": link["to"]})
                            found.append((link["id"], hit))
        ((link_id, hit),) = found
        self.assertEqual(link_id, "SalesInvoice.invoices_order")
        self.assertEqual(hit["status"], "found")
        self.assertEqual(hit["candidates"][0]["object"]["object_id"], SO)
        self.assertEqual(hit["candidates"][0]["object"]["lineage"], "gold_etl_from_fm")
        counts = self.ok(t, "plx.object", {"object_id": root["object_id"]})["linked_counts"]
        self.assertIn({"link_type": link_id, "direction": "forward", "count": 1,
                       "tiers": {"verified": 1}}, counts)


class SchemaToolTests(ContractMixin, unittest.TestCase):
    def test_lists_types_and_one_type_in_detail(self):
        t = tools()
        listing = self.ok(t, "plx.schema", {})
        self.assertEqual(listing["schema_digest"], SCHEMA.digest_hex)
        self.assertIn("Formulation", [o["id"] for o in listing["object_types"]])
        one = self.ok(t, "plx.schema", {"object_type": "SalesOrder"})
        (entry,) = one["object_types"]
        props = {p["name"]: p for p in entry["properties"]}
        self.assertEqual(props["total_amount"]["markings"], ["finance"])
        self.assertEqual(props["promised_date"]["availability"], "not_served")
        self.assertEqual({l["id"] for l in one["link_types"]},
                         {"SalesInvoice.invoices_order", "SalesOrder.same_record_as"})
        self.assertEqual(one["event_types"][0]["time_basis"], "planned")
        links = self.ok(t, "plx.schema", {"section": "links"})
        self.assertNotIn("object_types", links)

    def test_unknown_type_and_missing_schema_are_errors(self):
        result = tools().call("plx.schema", {"object_type": "Nope"}, FULL)
        self.assertContract("plx.schema", result)
        self.assertEqual(result["error"]["code"], "unknown_object_type")
        bare = ot.OntologyTools(ot.StoreReader(build().conn), POLICY, schema=None,
                                grammar=FixtureGrammar(), clock=lambda: NOW)
        self.assertEqual(bare.call("plx.schema", {}, FULL)["error"]["code"], "schema_unavailable")

    def test_large_schema_fits_one_response(self):
        big = ot.SchemaView(object_types={f"Type{i:03d}": {"id": f"Type{i:03d}",
                                                            "description": "d" * 1500,
                                                            "availability": {"state": "served"}}
                                          for i in range(200)}, digest="a" * 64)
        result = self.ok(tools(schema=big), "plx.schema", {})
        self.assertEqual(result["completeness"]["total"], 200)


class HostWiringTests(unittest.TestCase):
    def test_tool_specs_are_read_only_with_self_contained_schemas(self):
        specs = ot.tool_specs()
        self.assertEqual([spec["name"] for spec in specs], list(ot.TOOL_NAMES))
        for spec in specs:
            self.assertLessEqual(len(spec["description"]),
                                 CONTRACTS["limits"]["max_description_chars"])
            check_schema(spec["inputSchema"])  # every $ref resolves inside it
            self.assertTrue(spec["annotations"]["readOnlyHint"])
        result = tools().call("plx.find", {"query": "PSI2099001"}, FULL)
        self.assertEqual(result["status"], "found")

    def test_retirement_plan_matches_the_case_host(self):
        from operating.mcp_server import CASE_RUNTIME_TOOLS, FRESHNESS_META_KEY

        host = set(CASE_RUNTIME_TOOLS)
        self.assertLessEqual(set(ot.TOOL_NAMES), host)
        self.assertFalse(ot.DIRECTIVE_RETIRED & host)
        self.assertFalse(set(ot.HOST_RETIREMENTS) & host)
        self.assertLessEqual({"plx.around", "plx.timeline"}, host)  # L9
        self.assertEqual(FRESHNESS_META_KEY.split("/")[0], "plx")
        for name, plan in ot.HOST_RETIREMENTS.items():
            self.assertIn(plan["basis"], {"directive", "lead", "merge"}, name)
            # A successor exists now: a tool on the host, or the freshness _meta.
            self.assertTrue(plan["successor"] in host or plan["successor"] == "_meta",
                            (name, plan))
        budget = CONTRACTS["limits"]["max_case_host_tools"]
        self.assertLessEqual(len(host), budget)


class GenericityTests(unittest.TestCase):
    def test_module_names_no_kind_type_or_binding(self):
        source = (ROOT / "operating/ontology_tools.py").read_text("utf-8")
        words = set(FixtureGrammar().kinds()) | set(SCHEMA.object_types) | {
            b["id"] for t in DOMAIN["object_types"] for b in t["bindings"]}
        real = ROOT / "ontology/schema/identifiers.json"
        if real.is_file():
            words |= set(json.loads(real.read_text("utf-8"))["kinds"])
        for word in sorted(words):
            self.assertIsNone(re.search(rf"(?<![A-Za-z0-9_.]){re.escape(word)}(?![A-Za-z0-9_])",
                                        source), word)


class LiveIdCapTests(unittest.TestCase):
    def test_one_readers_large_live_find_does_not_evict_another_readers_ids(self):
        sets = ot.SetRegistry()
        sets.new(SALES, ["obj:keep"], live=True)
        sets.new(FULL, [f"obj:{i}" for i in range(ot.MAX_SET_OBJECTS)], live=True)
        self.assertTrue(sets.live_id("obj:keep", SALES))
        self.assertTrue(sets.live_id(f"obj:{ot.MAX_SET_OBJECTS - 1}", FULL))
        sets.new(FULL, ["obj:later"], live=True)  # over the cap: FULL's oldest goes
        self.assertFalse(sets.live_id("obj:0", FULL))
        self.assertTrue(sets.live_id("obj:later", FULL))
        self.assertTrue(sets.live_id("obj:keep", SALES))



class LiveIdCeilingTests(unittest.TestCase):
    def registry(self):
        clock = {"t": 0.0}

        def tick():
            clock["t"] += 1.0
            return clock["t"]
        return ot.SetRegistry(monotonic=tick, ttl=10_000.0)

    def test_overall_ceiling_evicts_the_oldest_ids_across_readers(self):
        sets = self.registry()
        self.assertGreater(ot.MAX_LIVE_IDS, ot.MAX_SET_OBJECTS)
        readers = [Actor("t2", frozenset({f"p{i}"}))
                   for i in range(ot.MAX_LIVE_IDS // ot.MAX_SET_OBJECTS + 1)]
        for n, reader in enumerate(readers):
            sets.new(reader, [f"{n}:{i}" for i in range(ot.MAX_SET_OBJECTS)], live=True)
        self.assertLessEqual(len(sets._live_ids), ot.MAX_LIVE_IDS)
        self.assertFalse(sets.live_id("0:0", readers[0]))     # oldest reader's ids go first
        last = len(readers) - 1
        self.assertTrue(sets.live_id(f"{last}:0", readers[last]))
        self.assertTrue(sets.live_id(f"{last}:{ot.MAX_SET_OBJECTS - 1}", readers[last]))

    def test_evicting_a_set_drops_the_live_ids_only_it_held(self):
        sets = self.registry()
        first = sets.new(SALES, ["only-first", "shared"], live=True)
        sets.new(SALES, ["shared", "second"], live=True)
        for i in range(ot.MAX_SETS - 1):   # pushes only the first set out of the limit
            sets.new(FULL, [f"x:{i}"], live=False)
        self.assertIsNone(sets.resolve(first, SALES))
        self.assertFalse(sets.live_id("only-first", SALES))
        self.assertTrue(sets.live_id("shared", SALES))
        self.assertTrue(sets.live_id("second", SALES))


if __name__ == "__main__":
    unittest.main()
