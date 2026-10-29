"""Lane plx-process: plx.process over a fixture projection store.

The store is created with the frozen DDL (ontology/store_ddl.py) and filled by
hand with synthetic records; the schema is the real compiled ontology schema and
the stage graphs are the real ones (ontology/schema/process/*.json), so the
tests exercise the shipped data, not a copy. Every output is validated against
operating/ontology_tool_contracts.json. Evidence label: isolated-fixture.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import unittest.mock

from ontology import schema as schema_loader
from ontology import store_ddl
from ontology.json_contract import validate
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_process as op
from operating import ontology_tools_traverse as tt

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
NOW = dt.datetime(2099, 2, 1, 12, 0, tzinfo=dt.timezone.utc)
T0 = "2099-01-01T00:00:00Z"
SCHEMA: ot.SchemaView | None = None
REAL_SCHEMA: ot.SchemaView | None = None
GRAPHS: dict | None = None
# A PO open by the shared definition (operating/po_status.py) gets po_delivery_window
# unnamed; a test of purchase_to_invoice's stages or checks names it.
P2I = {"process": "purchase_to_invoice"}

STAFF = Actor("plx", frozenset({"staff"}))
SALES = Actor("plx", frozenset({"sales"}))
POLICY = ot.ReadPolicy(
    "plx",
    {"api:*": frozenset({"staff", "sales"}), "bc:*": frozenset({"staff"}),
     # one Portal unit inside the granted family is withheld from sales
     "api:public.app_manufacturing_order": frozenset({"staff"})},
    {"finance": frozenset({"staff"}), "pii": frozenset({"staff"}),
     "l5": frozenset({"staff"}), "quality_controlled": frozenset({"staff"})})


def setUpModule():
    global SCHEMA, REAL_SCHEMA, GRAPHS
    REAL_SCHEMA = ot.SchemaView.from_compiled(schema_loader.load_schema())
    GRAPHS = op.load_graphs(schema=REAL_SCHEMA)
    # Fixture assumption: the prices the graphs compare share one declared tax basis, so
    # the price verdicts below are decidable. The shipped schema leaves most of them
    # unknown; tests.test_process_three_way.TaxBasisPriceTests reads it as it is.
    SCHEMA = with_price_basis(REAL_SCHEMA, GRAPHS, "excl_tax")


def with_price_basis(view, graphs, basis, only=None):
    """A copy of ``view`` whose process price sources declare ``basis`` (no lineage
    refinement); ``only``: limit to these (object_type, property) pairs."""
    types = copy.deepcopy(dict(view.object_types))
    priced = {(side["object_type"], name) for graph in graphs.values()
              for check in graph.get("quantity_checks", ()) for side in check["sides"]
              for name in side.get("price", ())}
    for tid, name in priced if only is None else only:
        for prop in types[tid]["properties"]:
            if prop["name"] == name:
                for source in prop["sources"]:
                    source["tax_basis"] = basis
                    source.pop("tax_basis_by_lineage", None)
    return dataclasses.replace(view, object_types=types)


def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()


_RELATION = {}  # binding id -> (source, relation)
BC_LINE_BINDINGS = frozenset({"bc.purchase_order_lines", "bc.purchase_receipt_lines",
                              "bc.purchase_invoice_lines"})


def _relation(binding):
    if not _RELATION:
        for tid, spec in SCHEMA.object_types.items():
            for b in spec.get("bindings", ()):
                _RELATION[b["id"]] = (b["source"], b["relation"])
    return _RELATION[binding]


class Store:
    """A hand-filled projection: objects with one version, values, links, events."""

    def __init__(self, *, report=None, freshness=None):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        store_ddl.create(self.conn)
        meta = {"schema_digest": "sha256:" + SCHEMA.digest_hex,
                "freshness": json.dumps(freshness if freshness is not None else [
                    {"source": s, "captured_at": "2099-02-01T11:00:00Z", "lag_seconds": 3600,
                     "completeness": "complete", "stale": False} for s in ("portal_api", "bc")]),
                "build_report": json.dumps(report if report is not None
                                           else {"counts": {}, "gaps": []})}
        for key, value in meta.items():
            if value is not None:
                self.conn.execute("INSERT INTO store_meta VALUES (?, ?)", (key, value))
        self.handles = {}
        self.n = 0

    def obj(self, object_id, lineage=None, *, markings="[]", missing=None, company="company_a"):
        otype, binding, native = object_id.split(":", 2)
        source, relation = _relation(binding)
        if source == "bc":
            audience = f"bc:{company}/{relation}"
            handle = f"bc://{company}/{relation}/{native}@cap-1"
            lineage = lineage or f"bc_{company}"
        else:
            audience = f"api:{relation}"
            handle = f"api://{relation}/{native}@0000000000000001-1"
            lineage = lineage or "gold_etl_from_fm"
        self.handles[object_id] = (handle, lineage)
        self.conn.execute("INSERT INTO objects VALUES (?,?,?,?,?,?,?)",
                          (object_id, otype, binding, json.dumps([native]), audience, T0, missing))
        self.conn.execute("INSERT INTO object_versions VALUES (?,?,?,?,?,?,?)",
                          (object_id, handle, _h(handle), lineage, markings, T0, None))
        if binding in BC_LINE_BINDINGS:   # BC always sends lineType (live profile: no nulls)
            self.val(object_id, "line_type", "text", "Item")
        return object_id

    def val(self, object_id, prop, value_type, value, *, unit=None, role=None, currency=None,
            markings="[]", path=None):
        handle, lineage = self.handles[object_id]
        binding = object_id.split(":")[1]
        path = path or f"row.{prop}"
        self.conn.execute(
            "INSERT INTO object_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (object_id, prop, value_type, value, unit, role, currency, None, None, handle,
             path, _h(f"{handle}|{path}|{value}"), lineage, binding, "trim", 0, markings, T0, None))

    def link(self, link_type, src, dst, tier="verified", kind="fk", why_not=()):
        self.n += 1
        handles = [self.handles[src][0], self.handles[dst][0]]
        self.conn.execute(
            "INSERT INTO links(link_id, link_type, from_object, to_object, tier, binding_kind, "
            "reason, why_not, evidence_handles, markings, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"lnk-{self.n}", link_type, src, dst, tier, kind,
             json.dumps([{"feature": "stored_key", "agrees": True}]), json.dumps(list(why_not)),
             json.dumps(handles), "[]", T0))

    def event(self, event_type, object_id, time="2099-01-10"):
        self.n += 1
        handle, lineage = self.handles[object_id]
        eid = f"ev-{self.n}"
        self.conn.execute(
            "INSERT INTO events(event_id, event_type, time_basis, time, l1_handle, field_path, "
            "digest, lineage, markings, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, event_type, "source_business_date", time, handle, "row.date", _h(eid),
             lineage, "[]", T0))
        self.conn.execute("INSERT INTO event_objects VALUES (?,?,?)", (eid, object_id, "subject"))
        return eid


def tools(store):
    base = ot.OntologyTools(ot.StoreReader(store.conn), POLICY, schema=SCHEMA,
                            contracts=CONTRACTS, clock=lambda: NOW)
    return op.ProcessTools(base, GRAPHS)


# --------------------------------------------------------------------------- fixture data

def sales_fixture(store):
    """A MAKE order through MO / release / shipment / BC invoice, a DISTRIBUTE order
    that skips MO and release, and their records (all synthetic). Lineages are the
    ones the curated schema's rules produce: an FM-ETL order (gold_etl_from_fm) and
    its lines (inherited), AO-coded MOs (gold_etl_from_fm), shipments with an FM
    silver twin (gold_etl_from_fm), and ship releases, which only the Portal
    creates (gold_native_staging: never authority, so a lead)."""
    ids = {}
    s = store
    ids["make"] = s.obj("SalesOrder:portal.app_sales_order:so-make", "gold_etl_from_fm")
    s.val(ids["make"], "status", "text", "CONFIRMED")
    s.val(ids["make"], "fulfillment_mode", "enum", "MAKE")
    line = s.obj("SalesOrderLine:portal.app_sales_order_line:so-make-l1", "gold_etl_from_fm")
    ids["line"] = line
    s.link("SalesOrderLine.line_of", line, ids["make"])
    ids["mo"] = mo(s, "mo-1", "COMPLETED", line)
    ids["mo_cancelled"] = mo(s, "mo-2", "cancelled", line)
    ids["release"] = s.obj("ShipRelease:portal.app_ship_release:sr-1", "gold_native_staging")
    s.val(ids["release"], "status", "enum", "FULFILLED")
    s.link("ShipRelease.for_sales_order", ids["release"], ids["make"])
    ids["release_event"] = s.event("ShipRelease.released", ids["release"])
    ids["shipment"] = shipment(s, "sh-1", "SHIPPED", ids["make"])
    ids["ship_event"] = s.event("Shipment.shipped", ids["shipment"])
    ids["bc_twin"] = s.obj("SalesOrder:bc.sales_orders:bc-so-1")
    s.link("SalesOrder.same_record_as", ids["make"], ids["bc_twin"], kind="same_record")
    ids["invoice"] = s.obj("SalesInvoice:bc.sales_invoices:inv-1")
    s.val(ids["invoice"], "status", "text", "Open")
    s.link("SalesInvoice.bills", ids["invoice"], ids["bc_twin"])

    ids["dist"] = s.obj("SalesOrder:portal.app_sales_order:so-dist", "gold_etl_from_fm")
    s.val(ids["dist"], "status", "text", "CONFIRMED")
    s.val(ids["dist"], "fulfillment_mode", "enum", "DISTRIBUTE")
    ids["dist_ship"] = shipment(s, "sh-2", "DELIVERED", ids["dist"])
    return ids


def mo(s, key, status, line, deleted=None):
    oid = s.obj(f"AssemblyOrder:portal.app_manufacturing_order:{key}", "gold_etl_from_fm")
    s.val(oid, "status", "text", status)
    s.val(oid, "deleted_at", "timestamp", deleted)  # the projector stores a source null
    s.link("AssemblyOrder.for_order_line", oid, line)
    return oid


def shipment(s, key, status, order):
    oid = s.obj(f"Shipment:portal.app_shipment:{key}", "gold_etl_from_fm")
    s.val(oid, "status", "text", status)
    s.link("Shipment.ships", oid, order)
    twin = s.obj(f"Shipment:portal.silver_shipments:{key}", "silver_view")
    s.link("Shipment.same_record_as", oid, twin, kind="same_record")
    return oid


def purchase_fixture(store):
    """BC chain: PO-A with receipt and invoice (quantity and price disagree), PO-B
    with a receipt but no invoice."""
    s, ids = store, {}
    item = s.obj("Product:bc.items:item-1")
    ids["item"] = item
    ids["po_a"] = s.obj("PurchaseOrder:bc.purchase_orders:po-a")
    s.val(ids["po_a"], "bc_status", "text", "Open")  # released: issued (a Draft is not)
    pol = s.obj("PurchaseOrderLine:bc.purchase_order_lines:po-a-1")
    ids["po_a_line"] = pol
    s.val(pol, "ordered_quantity", "quantity", "100", unit="each", role="ordered")
    s.val(pol, "bc_direct_unit_cost", "money", "2.50", unit="each", currency="USD",
          markings='["finance"]')
    s.link("PurchaseOrderLine.line_of", pol, ids["po_a"])
    s.link("PurchaseOrderLine.for_item", pol, item)
    rec = s.obj("Receipt:bc.purchase_receipts:rc-a")
    s.link("Receipt.receives", rec, ids["po_a"])
    for n, qty in ((1, "60"), (2, "30")):
        rl = s.obj(f"ReceiptLine:bc.purchase_receipt_lines:rc-a-{n}")
        ids[f"rc_a_{n}"] = rl
        s.val(rl, "received_quantity", "quantity", qty, unit="each", role="received")
        s.link("ReceiptLine.line_of", rl, rec)
        s.link("ReceiptLine.for_item", rl, item)
    inv = s.obj("PurchaseInvoice:bc.purchase_invoices:pi-a")
    ids["pi_a"] = inv
    s.val(inv, "status", "text", "Open")
    s.link("PurchaseInvoice.invoices", inv, ids["po_a"])
    pil = s.obj("PurchaseInvoiceLine:bc.purchase_invoice_lines:pi-a-1")
    ids["pi_a_line"] = pil
    s.val(pil, "invoiced_quantity", "quantity", "90", unit="each", role="invoiced")
    s.val(pil, "unit_cost", "money", "2.60", unit="each", currency="USD",
          markings='["finance"]')
    s.link("PurchaseInvoiceLine.line_of", pil, inv)
    s.link("PurchaseInvoiceLine.for_item", pil, item)

    ids["po_b"] = s.obj("PurchaseOrder:bc.purchase_orders:po-b")
    s.val(ids["po_b"], "bc_status", "text", "Open")
    polb = s.obj("PurchaseOrderLine:bc.purchase_order_lines:po-b-1")
    s.val(polb, "ordered_quantity", "quantity", "50", unit="each", role="ordered")
    s.link("PurchaseOrderLine.line_of", polb, ids["po_b"])
    s.link("PurchaseOrderLine.for_item", polb, item)
    recb = s.obj("Receipt:bc.purchase_receipts:rc-b")
    s.link("Receipt.receives", recb, ids["po_b"])
    rlb = s.obj("ReceiptLine:bc.purchase_receipt_lines:rc-b-1")
    s.val(rlb, "received_quantity", "quantity", "50", unit="each", role="received")
    s.link("ReceiptLine.line_of", rlb, recb)
    s.link("ReceiptLine.for_item", rlb, item)
    return ids


def ao_fixture(store, *, status="Realized", tasks=("Setup", "Filling"), required=("2099-01-20",),
               portal_required=None, shipped=None):
    """An FM assembly order serving an FM order line (allocation), filling from a bulk
    batch whose QC status is quality-controlled, with shop-floor rows; the order's FM
    required date(s), optionally a Portal twin with its own required date and shipment."""
    s, ids = store, {}
    ids["ao"] = ao = s.obj("AssemblyOrder:portal.fm_assembly_order:ao-1", "fm_bronze")
    s.val(ao, "status", "text", status)
    s.event("AssemblyOrder.scheduled", ao, "2099-01-05")
    so = ids["so"] = s.obj("SalesOrder:portal.fm_order_entry:so-1", "fm_bronze")
    for day in required:
        s.event("SalesOrder.fm_required", so, day)
    line = s.obj("SalesOrderLine:portal.fm_order_entry_item:l-1", "fm_bronze")
    s.link("SalesOrderLine.line_of", line, so)
    alloc = s.obj("AssemblyOrderAllocation:portal.fm_order_entry_item_assembly:al-1", "fm_bronze")
    s.link("AssemblyOrderAllocation.allocates", alloc, ao)
    s.link("AssemblyOrderAllocation.for_order_line", alloc, line)
    ids["batch"] = batch = s.obj("Batch:portal.fm_batch:b-1", "fm_bronze")
    s.val(batch, "quality_status", "text", "Released", markings='["quality_controlled"]')
    s.event("Batch.manufactured", batch, "2099-01-04")
    s.link("AssemblyOrder.fills_from", ao, batch)
    for n, kind in enumerate(tasks):
        task = s.obj(f"ProductionTask:portal.fm_production_ao:t-{n}", "fm_bronze")
        s.val(task, "task_type", "text", kind)
        s.link("ProductionTask.task_of_order", task, ao)
    if portal_required or shipped:
        twin = ids["twin"] = s.obj("SalesOrder:portal.app_sales_order:so-1", "gold_etl_from_fm")
        s.link("SalesOrder.same_record_as", so, twin, kind="same_record")
        if portal_required:
            s.event("SalesOrder.required", twin, portal_required)
        if shipped:
            ids["shipment"] = shipment(s, "sh-ao", shipped, twin)
    return ids


def material_fixture(store, *, pos=("open",), mo_status="IN_PROGRESS", bc_pos=(), twins=0):
    """A gold MAKE order whose line's Portal MO (with its FM twin AO) consumes one
    packaging item; one Portal PO of that item per ``pos`` entry: ``open`` (issued, no
    receipt), ``received`` (a receipt and its line's received figure) or ``cancelled``;
    one BC PO per ``bc_pos`` entry (``open``, ``received`` or ``draft``) on the item's BC
    twin, the first ``twins`` of them the BC twin of the Portal PO at their index, the
    others BC-only (all synthetic)."""
    s, ids = store, {}
    so = ids["so"] = s.obj("SalesOrder:portal.app_sales_order:so-mat", "gold_etl_from_fm")
    s.val(so, "status", "text", "CONFIRMED")
    s.val(so, "fulfillment_mode", "enum", "MAKE")
    line = s.obj("SalesOrderLine:portal.app_sales_order_line:so-mat-l1", "gold_etl_from_fm")
    s.link("SalesOrderLine.line_of", line, so)
    ids["mo"] = mo(s, "mo-mat", mo_status, line)
    ao = ids["ao"] = s.obj("AssemblyOrder:portal.fm_assembly_order:ao-mat", "fm_bronze")
    s.val(ao, "status", "text", "Committed")
    s.link("AssemblyOrder.same_record_as", ids["mo"], ao, kind="same_record")
    part = s.obj("AssemblyOrderComponent:portal.fm_assembly_order_item:c-1", "fm_bronze")
    s.link("AssemblyOrderComponent.component_of", part, ao)
    pack = s.obj("Product:portal.fm_packaging:pk-1", "fm_bronze")
    s.link("AssemblyOrderComponent.uses_item", part, pack)
    item = s.obj("Product:portal.app_product:p-mat", "gold_etl_from_fm")
    s.link("Product.same_record_as", item, pack, kind="same_record")
    ids["pos"] = []
    for n, kind in enumerate(pos):
        po = s.obj(f"PurchaseOrder:portal.app_purchase_order:po-mat-{n:02d}", "gold_etl_from_fm")
        s.val(po, "status", "text", "CANCELLED" if kind == "cancelled" else "Pending")
        pol = s.obj(f"PurchaseOrderLine:portal.app_purchase_order_line:po-mat-{n:02d}-1",
                    "gold_etl_from_fm")
        s.val(pol, "ordered_quantity", "quantity", "100", unit="each", role="ordered")
        s.val(pol, "received_quantity", "quantity", "100" if kind == "received" else "0",
              unit="each", role="received")
        s.link("PurchaseOrderLine.line_of", pol, po)
        s.link("PurchaseOrderLine.for_item", pol, item)
        if kind == "received":
            s.link("Receipt.receives", s.obj(f"Receipt:portal.app_receipt:rc-mat-{n:02d}",
                                             "gold_etl_from_fm"), po)
        ids["pos"].append(po)
    ids["bc_pos"] = []
    if bc_pos:
        bc_item = s.obj("Product:bc.items:item-mat")
        s.link("Product.same_record_as", item, bc_item, kind="same_record")
    for n, kind in enumerate(bc_pos):
        po = s.obj(f"PurchaseOrder:bc.purchase_orders:bcpo-mat-{n:02d}")
        s.val(po, "bc_status", "text", "Draft" if kind == "draft" else "Open")
        if n < twins:
            s.link("PurchaseOrder.same_record_as", ids["pos"][n], po, kind="same_record")
        pol = s.obj(f"PurchaseOrderLine:bc.purchase_order_lines:bcpo-mat-{n:02d}-1")
        s.val(pol, "ordered_quantity", "quantity", "100", unit="each", role="ordered")
        s.val(pol, "received_quantity", "quantity", "100" if kind == "received" else "0",
              unit="each", role="received")
        s.link("PurchaseOrderLine.line_of", pol, po)
        s.link("PurchaseOrderLine.for_item", pol, bc_item)
        if kind == "received":
            s.link("Receipt.receives", s.obj(f"Receipt:bc.purchase_receipts:bcrc-mat-{n:02d}"), po)
        ids["bc_pos"].append(po)
    return ids


def named_po_fixture(s, companies=("company_a",), *, portal_twins=0, also=()):
    """A gold PO received in full whose BC PO BC deleted once it was invoiced: no BC twin
    and no PurchaseInvoice.invoices link is left, only a BC invoice per company in
    ``companies`` naming the PO's number. The rows are as the projector writes
    PurchaseInvoice.names_order (capped at lead: the BC company is not known, and
    ambiguous_target when the number names ``portal_twins`` more gold POs; ``also``:
    further why_not features)."""
    cap = {"feature": "tier_cap", "agrees": None, "cap": "lead",
           "because": "bc_company_not_known"}
    gold_item = s.obj("Product:portal.app_product:p-n")
    po = s.obj("PurchaseOrder:portal.app_purchase_order:po-n")
    s.val(po, "status", "text", "ORDERED")
    pol = s.obj("PurchaseOrderLine:portal.app_purchase_order_line:po-n-1")
    s.val(pol, "ordered_quantity", "quantity", "100", unit="each", role="ordered")
    s.link("PurchaseOrderLine.line_of", pol, po)
    s.link("PurchaseOrderLine.for_item", pol, gold_item)
    rec = s.obj("Receipt:portal.app_receipt:rc-n")
    s.link("Receipt.receives", rec, po, tier="stored_by_text_join")
    rl = s.obj("ReceiptLine:portal.app_receipt_line:rc-n-1")
    s.val(rl, "received_quantity", "quantity", "100", unit="each", role="received")
    s.link("ReceiptLine.line_of", rl, rec)
    s.link("ReceiptLine.for_item", rl, gold_item)
    others = [s.obj(f"PurchaseOrder:portal.app_purchase_order:po-n{n}")
              for n in range(portal_twins)]
    for company in companies:
        bc_item = s.obj(f"Product:bc.items:{company}-item-n", company=company)
        s.link("Product.same_record_as", gold_item, bc_item, kind="same_record")
        inv = s.obj(f"PurchaseInvoice:bc.purchase_invoices:{company}-pi-n", company=company)
        s.val(inv, "status", "text", "Open")
        why = [cap] + ([{"feature": "ambiguous_target", "agrees": None,
                         "candidates": 1 + portal_twins, "cardinality": "N:1"}]
                       if portal_twins else []) + list(also)
        for named in [po] + others:
            s.link("PurchaseInvoice.names_order", inv, named, tier="lead", why_not=why)
        pil = s.obj(f"PurchaseInvoiceLine:bc.purchase_invoice_lines:{company}-pi-n-1",
                    company=company)
        s.val(pil, "invoiced_quantity", "quantity", "100", unit="each", role="invoiced")
        s.link("PurchaseInvoiceLine.line_of", pil, inv)
        s.link("PurchaseInvoiceLine.for_item", pil, bc_item)
    return po


def stages(result):
    return {s["stage"]: s for s in result["stages"]}


class Mixin:
    def answer(self, t, arguments, actor=STAFF):
        answer = t.process(arguments, actor)
        problems = validate(answer.result, CONTRACTS["tools"]["plx.process"]["output"],
                            root=CONTRACTS)
        self.assertEqual(problems, [])
        self.assertNotIn("error", answer.result, answer.result)
        return answer


# --------------------------------------------------------------------------- graphs

class GraphDataTests(unittest.TestCase):
    def test_shipped_graphs_validate_against_the_format_and_the_compiled_schema(self):
        self.assertEqual(sorted(GRAPHS), ["ao_packet", "manufacturing_order", "po_delivery_window",
                                          "purchase_to_invoice", "sales_order_lifecycle"])
        for graph in GRAPHS.values():
            self.assertEqual(op.check_graph(graph, SCHEMA), [])

    def test_sales_graph_mirrors_the_portal_rail_status_sets_and_skips(self):
        g = GRAPHS["sales_order_lifecycle"]
        by_id = {s["id"]: s for s in g["stages"]}
        self.assertEqual([s["id"] for s in g["stages"]][:4],
                         ["sales_order", "manufacturing_order", "release_order", "pick_list"])
        pred = lambda sid: by_id[sid]["evidence"][0]["predicate"]  # noqa: E731
        # SO_CONFIRMED plus COMPLETE (a completed order is no draft) and OPEN (the legacy
        # FileMaker-import alias of CONFIRMED); CANCELLED is no order.
        self.assertEqual(pred("sales_order")["done"], ["CONFIRMED", "IN_PRODUCTION", "READY",
                                                       "READY_TO_SHIP", "PARTIALLY_SHIPPED",
                                                       "SHIPPED", "COMPLETE", "OPEN"])
        self.assertEqual(pred("sales_order")["ignore"], ["CANCELLED"])
        self.assertEqual(pred("manufacturing_order")["done"], ["COMPLETED", "REALIZED", "CLOSED"])
        self.assertEqual(pred("manufacturing_order")["ignore"], ["CANCELLED"])
        self.assertEqual(pred("release_order")["done"], ["FULFILLED"])
        self.assertEqual(pred("pick_list")["done"], ["COMPLETED"])
        cond = g["conditions"][0]["evidence"][0]["predicate"]
        self.assertEqual((cond["done"], cond["quantifier"]), (["RELEASED", "FULFILLED"], "any"))
        skips = {v["id"]: v["skips"] for v in g["variants"]}
        self.assertEqual(skips, {"make": [], "distribute": ["manufacturing_order",
                                                             "release_order"]})
        self.assertEqual(g["variant_by"]["default"], "make")
        self.assertIn("plx-customer-portal:portal/src/lib/sales-order/order-lifecycle.ts@c3ffda3"
                      " (buildOrderLifecycle", " ".join(g["evidence"]))

    def test_loader_refuses_a_graph_naming_an_undeclared_link_or_role_mismatch(self):
        bad = copy.deepcopy(GRAPHS["purchase_to_invoice"])
        bad["stages"][1]["evidence"][0]["paths"][0][0]["link"] = "Receipt.not_a_link"
        self.assertTrue(any("undeclared link type" in p for p in op.check_graph(bad, SCHEMA)))
        bad = copy.deepcopy(GRAPHS["purchase_to_invoice"])
        bad["quantity_checks"][0]["sides"][1]["quantity"] = "bc_unit_cost"
        self.assertTrue(any("not a quantity property" in p for p in op.check_graph(bad, SCHEMA)))
        bad = copy.deepcopy(GRAPHS["purchase_to_invoice"])
        bad["quantity_checks"][0]["sides"][0]["role"] = "received"
        problems = op.check_graph(bad, SCHEMA)
        self.assertTrue(any("has role ordered, not received" in p for p in problems), problems)
        # a due date is a declared planned date column of the type its paths reach
        for due, problem in (
                ({"event_types": ["AssemblyOrder.realized"]}, "is not a planned date"),
                ({"event_types": ["SalesOrder.required"]}, "is not an event of AssemblyOrder"),
                ({"property": "status"}, "is not a date property")):
            bad = copy.deepcopy(GRAPHS["manufacturing_order"])
            stage = next(s for s in bad["stages"] if s["id"] == "filling")
            stage["due"] = dict({"object_type": "AssemblyOrder", "paths": [[]]}, **due)
            problems = op.check_graph(bad, SCHEMA)
            self.assertTrue(any(problem in p for p in problems), (due, problems))

    def test_loader_refuses_a_file_that_breaks_the_format(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "_process.schema.json").write_text(
                op.FORMAT_PATH.read_text("utf-8"), "utf-8")
            doc = copy.deepcopy(GRAPHS["purchase_to_invoice"])
            doc["stages"][0]["evidence"][0]["predicate"] = {"kind": "status", "property": "status",
                                                            "quantifier": "all"}
            Path(tmp, "purchase_to_invoice.json").write_text(json.dumps(doc), "utf-8")
            with self.assertRaises(op.ProcessGraphError):
                op.load_graphs(tmp, schema=SCHEMA)

    def test_module_has_no_process_or_kind_specific_names(self):
        source = (ROOT / "operating/ontology_tools_process.py").read_text("utf-8")
        code = source.split('"""', 2)[2]  # skip the module docstring
        for name in ("SalesOrder", "PurchaseOrder", "ShipRelease", "AssemblyOrder", "FULFILLED",
                     "COMPLETED", "manufacturing_order", "three_way", "DISTRIBUTE", "Receipt"):
            self.assertNotIn(name, code, name)


# --------------------------------------------------------------------------- sales order

class SalesOrderTests(Mixin, unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.ids = sales_fixture(self.store)
        self.t = tools(self.store)

    def test_make_order_through_mo_release_ship_and_invoice(self):
        answer = self.answer(self.t, {"from": self.ids["make"],
                                      "process": "sales_order_lifecycle"})
        st = stages(answer.result)
        for sid in ("sales_order", "manufacturing_order", "shipment", "customer_invoice"):
            self.assertEqual(st[sid]["state"], "present", sid)
        self.assertIn(self.ids["mo"], st["manufacturing_order"]["events"])
        self.assertNotIn(self.ids["mo_cancelled"], st["manufacturing_order"]["events"])
        self.assertIn(self.ids["ship_event"], st["shipment"]["events"])
        self.assertIn(self.ids["invoice"], st["customer_invoice"]["events"])
        # Ship releases exist only as Portal-native rows (gold_native_staging under the
        # schema's lineage rules), so a release is a lead: the release stage and the pick
        # gate are unknown on real data until those rows are confirmed (R1).
        self.assertEqual(st["release_order"]["state"], "missing")
        self.assertEqual(st["release_order"]["missing"]["state"], "unknown")
        self.assertNotIn(self.ids["release"], json.dumps(st["release_order"]))
        # pick lists are not served: unknown with a typed gap, never "missing in source"
        self.assertEqual(st["pick_list"]["state"], "missing")
        self.assertEqual((st["pick_list"]["missing"]["state"], st["pick_list"]["missing"]["reason"]),
                         ("not_ingested", "not_served"))
        (root,) = answer.roots
        self.assertEqual((root["variant"], root["current_stage"], root["current_certain"]),
                         ("make", "release_order", False))
        self.assertEqual(root["conditions"]["release_clears_pick"]["state"], "unknown")
        mo = next(s for s in root["stages"] if s["stage"] == "manufacturing_order")
        (cite,) = [c for c in mo["citations"] if c["l1_handle"].endswith("mo-1@0000000000000001-1")
                   and c["field_path"] == "row.status"]
        self.assertEqual(cite["lineage"], "gold_etl_from_fm")
        # the additive contract fields (single root)
        # current_stage is a decided field: release_order is unknown, so a fuller view
        # could place the order further on; the field is omitted (detail keeps the guess)
        self.assertNotIn("current_stage", answer.result)
        (cond,) = answer.result["conditions"]
        self.assertEqual((cond["condition"], cond["state"], cond["missing"]["state"]),
                         ("release_clears_pick", "missing", "unknown"))
        self.assertIn(cite, st["manufacturing_order"]["citations"])

    def test_release_confirmed_as_authority_reaches_the_stage_and_clears_the_gate(self):
        # Once a release row has an authoritative lineage the same data reaches the stage.
        self.store.conn.execute("UPDATE object_versions SET lineage = 'gold_etl_from_fm' "
                                "WHERE object_id = ?", (self.ids["release"],))
        answer = self.answer(self.t, {"from": self.ids["make"]})
        st = stages(answer.result)
        self.assertEqual(st["release_order"]["state"], "present")
        self.assertIn(self.ids["release_event"], st["release_order"]["events"])
        self.assertEqual(answer.roots[0]["conditions"]["release_clears_pick"]["state"], "reached")
        self.assertNotIn("current_stage", answer.result)  # pick_list is unknown (not served)
        self.assertEqual(answer.roots[0]["current_stage"], "pick_list")

    def test_distribute_order_skips_mo_and_release_not_missing(self):
        answer = self.answer(self.t, {"from": self.ids["dist"]})
        st = stages(answer.result)
        self.assertEqual(answer.result["process"], "sales_order_lifecycle")
        self.assertEqual(st["manufacturing_order"]["state"], "not_applicable")
        self.assertEqual(st["release_order"]["state"], "not_applicable")
        self.assertNotIn("missing", st["manufacturing_order"])
        self.assertEqual(st["sales_order"]["state"], "present")
        self.assertEqual(st["shipment"]["state"], "present")
        (root,) = answer.roots
        self.assertEqual(root["variant"], "distribute")
        self.assertNotIn("release_clears_pick", root["conditions"])
        self.assertEqual(answer.result["conditions"], [])

    def test_fm_order_without_gold_twin_is_decided_by_its_oe_status(self):
        # Portal's /mrp/orders/[id] rail synthesizes an FM order's lane from the OE
        # header status (synthesizeFmOrderLifecycle, order-lifecycle.ts@790256c).
        for status, state, reason in (("Open", "present", None), ("Closed", "present", None),
                                      ("Hold", "missing", "status_not_reached"),
                                      ("Cancelled", "missing", "no_live_record")):
            with self.subTest(status=status):
                fm = self.store.obj(f"SalesOrder:portal.fm_order_entry:{len(status)}{status}",
                                    "fm_bronze")
                self.store.val(fm, "fm_status", "text", status)
                answer = self.answer(self.t, {"from": fm, "process": "sales_order_lifecycle"})
                so = stages(answer.result)["sales_order"]
                self.assertEqual(so["state"], state)
                self.assertEqual(so.get("missing", {}).get("reason"), reason)
                # FM carries no fulfillment mode: Portal's MAKE default is not assumed
                self.assertIn("variant_not_carried", [g["reason"] for g in answer.result["gaps"]])
        # a gold root keeps its own status even when its FM twin's OE status disagrees
        twin = self.store.obj("SalesOrder:portal.fm_order_entry:88", "fm_bronze")
        self.store.val(twin, "fm_status", "text", "Hold")
        self.store.link("SalesOrder.same_record_as", self.ids["dist"], twin, kind="same_record")
        st = stages(self.answer(self.t, {"from": self.ids["dist"]}).result)
        self.assertEqual(st["sales_order"]["state"], "present")

    def test_open_mo_is_proven_not_reached_and_current(self):
        mo(self.store, "mo-3", "IN_PROGRESS", self.ids["line"])
        answer = self.answer(self.t, {"from": self.ids["make"]})
        st = stages(answer.result)
        self.assertEqual(st["manufacturing_order"]["state"], "missing")
        self.assertEqual(st["manufacturing_order"]["missing"]["state"], "absent_in_source")
        self.assertEqual(st["manufacturing_order"]["missing"]["reason"], "status_not_reached")
        self.assertEqual(answer.roots[0]["current_stage"], "manufacturing_order")
        self.assertTrue(answer.roots[0]["current_certain"])

    def test_soft_deleted_open_mo_is_dropped_like_portal(self):
        mo(self.store, "mo-4", "IN_PROGRESS", self.ids["line"], deleted="2099-01-05T00:00:00Z")
        st = stages(self.answer(self.t, {"from": self.ids["make"]}).result)
        self.assertEqual(st["manufacturing_order"]["state"], "present")
        self.assertNotIn("AssemblyOrder:portal.app_manufacturing_order:mo-4",
                         st["manufacturing_order"]["events"])

    def test_unreadable_soft_delete_marker_leaves_the_record_unknown(self):
        oid = mo(self.store, "mo-5", "COMPLETED", self.ids["line"])
        self.store.conn.execute("DELETE FROM object_values WHERE object_id = ? AND property = "
                                "'deleted_at'", (oid,))
        st = stages(self.answer(self.t, {"from": self.ids["make"]}).result)
        missing = st["manufacturing_order"]["missing"]
        self.assertEqual(missing["reason"], "deleted_marker_unreadable")
        self.assertIn("deleted marker (deleted_at)", missing["detail"])

    def test_hidden_evidence_never_counts(self):
        # sales holds no BC grant and no grant on manufacturing orders.
        answer = self.answer(self.t, {"from": self.ids["make"]}, actor=SALES)
        st = stages(answer.result)
        self.assertEqual(st["manufacturing_order"]["state"], "missing")
        self.assertEqual(st["manufacturing_order"]["missing"]["state"], "inaccessible")
        self.assertEqual(st["customer_invoice"]["state"], "missing")
        self.assertEqual(st["customer_invoice"]["missing"]["state"], "inaccessible")
        blob = json.dumps(answer.result) + json.dumps(answer.roots)
        for hidden in (self.ids["mo"], self.ids["invoice"], self.ids["bc_twin"]):
            self.assertNotIn(hidden, blob)
        # evidence the reader can fully see still counts
        self.assertEqual(st["shipment"]["state"], "present")

    def test_a_withheld_stage_reads_not_visible_to_you(self):
        st = stages(self.answer(self.t, {"from": self.ids["make"]}, actor=SALES).result)
        reading = st["manufacturing_order"]["reading"]
        self.assertEqual(reading["kind"], "not_visible")
        self.assertIn("Not visible to you", reading["text"])
        self.assertIn(st["manufacturing_order"]["owner_role"], reading["text"])
        self.assertEqual(st["manufacturing_order"]["missing"]["state"], "inaccessible")

    def test_hidden_open_record_blocks_every_done(self):
        # An open MO next to the completed one: staff sees the stage not reached; sales,
        # without the MO unit, must not see it reached (nor not reached) at all.
        mo(self.store, "mo-3", "IN_PROGRESS", self.ids["line"])
        st = stages(self.answer(self.t, {"from": self.ids["make"]}, actor=STAFF).result)
        self.assertEqual(st["manufacturing_order"]["missing"]["reason"], "status_not_reached")
        st = stages(self.answer(self.t, {"from": self.ids["make"]}, actor=SALES).result)
        self.assertEqual(st["manufacturing_order"]["missing"]["state"], "inaccessible")
        self.assertNotEqual(st["manufacturing_order"]["missing"]["reason"], "status_not_reached")

    def test_staging_root_answers_only_leads(self):
        self.store.conn.execute("UPDATE object_versions SET lineage = 'gold_native_staging' "
                                "WHERE object_id = ?", (self.ids["make"],))
        answer = self.answer(self.t, {"from": self.ids["make"]})
        st = stages(answer.result)
        self.assertEqual(st["sales_order"]["state"], "missing")
        self.assertEqual((st["sales_order"]["missing"]["state"],
                          st["sales_order"]["missing"]["reason"]), ("unknown", "staging_root"))
        self.assertIn("observed reached", st["sales_order"]["missing"]["detail"])
        self.assertNotIn("present", [s["state"] for s in answer.result["stages"]])
        self.assertIn("root_lineage_staging", [g["reason"] for g in answer.result["gaps"]])

    def test_staging_root_variant_skips_are_leads_too(self):
        self.store.conn.execute("UPDATE object_versions SET lineage = 'gold_native_staging' "
                                "WHERE object_id = ?", (self.ids["dist"],))
        st = stages(self.answer(self.t, {"from": self.ids["dist"]}).result)
        for sid in ("manufacturing_order", "release_order"):
            self.assertEqual(st[sid]["state"], "missing")
            self.assertEqual(st[sid]["missing"]["reason"], "staging_root")
            self.assertIn("observed skipped", st[sid]["missing"]["detail"])

    def test_staging_rows_are_leads_and_never_evidence(self):
        self.store.conn.execute("UPDATE object_versions SET lineage = 'gold_native_staging' "
                                "WHERE object_id = ?", (self.ids["shipment"],))
        st = stages(self.answer(self.t, {"from": self.ids["make"]}).result)
        self.assertEqual(st["shipment"]["state"], "missing")
        self.assertEqual(st["shipment"]["missing"]["state"], "unknown")

    def test_unknown_variant_leaves_variant_stages_unknown(self):
        self.store.conn.execute("DELETE FROM object_values WHERE object_id = ? AND property = "
                                "'fulfillment_mode'", (self.ids["dist"],))
        answer = self.answer(self.t, {"from": self.ids["dist"]})
        st = stages(answer.result)
        self.assertEqual(st["manufacturing_order"]["missing"]["reason"], "variant_unknown")
        self.assertIn("variant_unreadable", [g["reason"] for g in answer.result["gaps"]])

    def test_null_mode_defaults_to_make_like_portal(self):
        self.store.conn.execute("UPDATE object_values SET value = NULL WHERE object_id = ? AND "
                                "property = 'fulfillment_mode'", (self.ids["make"],))
        answer = self.answer(self.t, {"from": self.ids["make"]})
        self.assertEqual(answer.roots[0]["variant"], "make")

    def test_set_handle_reports_variants_and_mixed_stages(self):
        handle = self.t.base._new_set(STAFF, [self.ids["make"], self.ids["dist"]])
        answer = self.answer(self.t, {"from": handle})
        st = stages(answer.result)
        self.assertEqual(st["manufacturing_order"]["missing"]["reason"], "mixed_across_set")
        self.assertEqual(st["shipment"]["state"], "present")
        self.assertNotIn("current_stage", answer.result)
        self.assertNotIn("variants", answer.result)  # pick_list is unknown for both roots
        self.assertIn("variants_not_decided", [g["reason"] for g in answer.result["gaps"]])
        self.assertEqual(answer.result["completeness"]["returned"], 2)

    def test_a_set_its_reader_saw_trimmed_is_never_a_complete_process_count(self):
        # SALES lacks BC: plx.around from the customer reaches two of its three orders,
        # so plx.process over that set states no total; STAFF's is whole and exact.
        cust = self.store.obj("Customer:portal.app_customer:c-1")
        for order in (self.ids["make"], self.ids["dist"], self.ids["bc_twin"]):
            self.store.link("SalesOrder.ordered_by", order, cust)
        process = tools(self.store)
        traverse = tt.TraverseTools(process.base)
        path = {"from": cust, "path": [{"link_type": "SalesOrder.ordered_by",
                                        "direction": "reverse"}]}
        for actor, want in ((SALES, (False, None, "withheld_by_permission")),
                            (STAFF, (True, 3, None))):
            around = traverse.call("plx.around", path, actor)
            self.assertEqual(around["completeness"]["complete"], want[0], around)
            done = self.answer(process, {"from": around["set"]}, actor).result["completeness"]
            self.assertEqual((done["complete"], done["total"], done.get("reason")), want)
        self.assertIn("withheld_by_permission", [g["reason"] for g in process.call(
            "plx.process", {"from": traverse.call("plx.around", path, SALES)["set"]},
            SALES)["gaps"]])
        # A grant revoked after a whole set was made: STAFF's own set is no longer whole.
        whole = traverse.call("plx.around", path, STAFF)["set"]
        process.base.policy = dataclasses.replace(POLICY, audiences={
            k: v for k, v in POLICY.audiences.items() if not k.startswith("bc:")})
        stale = self.answer(process, {"from": whole}, STAFF).result
        self.assertEqual((stale["completeness"]["complete"], stale["completeness"]["total"],
                          stale["completeness"]["reason"]),
                         (False, None, "policy_changed_since_handle"))
        self.assertIn("policy_changed_since_handle", [g["reason"] for g in stale["gaps"]])

    def test_another_actors_set_handle_is_refused(self):
        handle = self.t.base._new_set(SALES, [self.ids["make"]])
        result = self.t.call("plx.process", {"from": handle}, STAFF)
        self.assertEqual(result["error"]["code"], "handle_expired")


# --------------------------------------------------------------------------- purchase

class PurchaseTests(Mixin, unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.ids = purchase_fixture(self.store)
        self.t = tools(self.store)

    def test_three_way_mismatch_is_found_and_cited(self):
        answer = self.answer(self.t, {"from": self.ids["po_a"]})
        st = stages(answer.result)
        self.assertEqual([st[s]["state"] for s in ("purchase_order", "receipt", "vendor_invoice")],
                         ["present", "present", "present"])
        checks = {tuple(c["roles"]): c for c in answer.result["quantity_checks"]}
        qty = checks[("ordered", "received")]
        self.assertIs(qty["agree"], False)
        self.assertIn("ordered 100 each (1 lines) vs received 90 each (2 lines)", qty["note"])
        self.assertIn("difference 10", qty["note"])
        cited = {v["citation"]["l1_handle"] for v in qty["values"]}
        self.assertEqual(cited, {f"bc://company_a/purchaseOrderLines/po-a-1@cap-1",
                                 "bc://company_a/purchaseReceiptLines/rc-a-1@cap-1",
                                 "bc://company_a/purchaseReceiptLines/rc-a-2@cap-1"})
        self.assertEqual({v["quantity_role"] for v in qty["values"]}, {"ordered", "received"})
        self.assertIs(checks[("received", "invoiced")]["agree"], True)
        price = checks[("ordered", "invoiced")]
        self.assertIs(price["agree"], False)
        self.assertIn("unit price 2.5 USD vs invoiced 2.6 USD; exact", price["note"])
        self.assertEqual({v["property"] for v in price["values"]},
                         {"bc_direct_unit_cost", "unit_cost"})
        findings = [c for c in answer.roots[0]["checks"] if c["finding"]]
        self.assertEqual(len(findings), 2)

    def test_set_of_decided_pos_reports_variants_and_current_stage(self):
        handle = self.t.base._new_set(STAFF, [self.ids["po_a"], self.ids["po_b"]])
        result = self.answer(self.t, {"from": handle}).result
        self.assertEqual(sorted((v["sequence"], v["count"]) for v in result["variants"]),
                         [(["purchase_order", "receipt"], 1),
                          (["purchase_order", "receipt", "vendor_invoice"], 1)])
        single = self.answer(self.t, {"from": self.ids["po_b"]}).result
        self.assertEqual(single["current_stage"], "vendor_invoice")

    def test_tolerance_is_only_the_declared_upstream_one(self):
        self.store.conn.execute("UPDATE object_values SET value = '89.99995' WHERE object_id = ?"
                                " AND property = 'received_quantity'", (self.ids["rc_a_2"],))
        self.store.conn.execute("UPDATE object_values SET value = '40.00005' WHERE object_id = ?"
                                " AND property = 'received_quantity'", (self.ids["rc_a_1"],))
        # 130.0000 vs ordered 100 -> still a mismatch; set ordered to the received sum + EPS/2
        self.store.conn.execute("UPDATE object_values SET value = '130.00004' WHERE object_id = ?"
                                " AND property = 'ordered_quantity'", (self.ids["po_a_line"],))
        checks = {tuple(c["roles"]): c for c in
                  self.answer(self.t, {"from": self.ids["po_a"]}).result["quantity_checks"]}
        self.assertIs(checks[("ordered", "received")]["agree"], True)
        self.assertIn("tolerance 0.0001", checks[("ordered", "received")]["note"])
        # received vs invoiced is exact: 130.00000 vs 90 disagrees
        self.assertIs(checks[("received", "invoiced")]["agree"], False)

    def test_po_with_receipt_but_no_invoice(self):
        answer = self.answer(self.t, {"from": self.ids["po_b"]})
        st = stages(answer.result)
        self.assertEqual(st["receipt"]["state"], "present")
        self.assertEqual(st["vendor_invoice"]["state"], "missing")
        self.assertEqual((st["vendor_invoice"]["missing"]["state"],
                          st["vendor_invoice"]["missing"]["reason"]),
                         ("absent_in_source", "no_evidence_record"))
        self.assertEqual(st["gl_posting"]["missing"]["state"], "absent_in_source")
        checks = {tuple(c["roles"]): c for c in answer.result["quantity_checks"]}
        self.assertIs(checks[("ordered", "received")]["agree"], True)
        self.assertIsNone(checks[("received", "invoiced")]["agree"])
        self.assertIn("invoiced side has no evidence", checks[("received", "invoiced")]["note"])
        self.assertEqual(answer.roots[0]["current_stage"], "vendor_invoice")
        self.assertTrue(answer.roots[0]["current_certain"])

    def test_uncaptured_evidence_is_unknown_not_missing(self):
        report = {"counts": {}, "gaps": [{"code": "source_not_captured",
                                          "where": "bc.purchase_invoices", "count": 1,
                                          "samples": []}]}
        store = Store(report=report)
        ids = purchase_fixture(store)
        st = stages(self.answer(tools(store), {"from": ids["po_b"]}).result)
        self.assertEqual(st["vendor_invoice"]["missing"]["state"], "unknown")
        self.assertEqual(st["vendor_invoice"]["missing"]["reason"], "absence_unproven")
        self.assertIn("source_not_captured", st["vendor_invoice"]["missing"]["detail"])

    def test_plain_readings_never_upgrade_a_typed_reason(self):
        # proven absence: not found in what we hold, with its as-of and sources searched
        st = stages(self.answer(self.t, {"from": self.ids["po_b"]}).result)
        proven = st["vendor_invoice"]["reading"]
        self.assertEqual(proven["kind"], "not_found_in_held")
        self.assertIn("Not found in what we hold, as of 2099-02-01T12:00:00Z", proven["text"])
        self.assertIn("searched: bc", proven["text"])
        self.assertEqual((st["receipt"]["state"], "reading" in st["receipt"]),
                         ("present", False))   # found and cited already
        # absence not proven: never "in what we hold"; names the source not fully captured
        report = {"counts": {}, "gaps": [{"code": "source_not_captured",
                                          "where": "bc.purchase_invoices", "count": 1,
                                          "samples": []}]}
        store = Store(report=report)
        ids = purchase_fixture(store)
        item = stages(self.answer(tools(store), {"from": ids["po_b"]}).result)["vendor_invoice"]
        self.assertEqual(item["reading"]["kind"], "not_found_yet")
        self.assertNotIn("what we hold", item["reading"]["text"])
        self.assertIn("not fully searched", item["reading"]["text"])
        self.assertIn("absence_unproven", item["reading"]["text"])
        self.assertEqual(item["missing"]["reason"], "absence_unproven")  # engine detail kept

    def test_root_stage_is_never_missing_in_its_own_process(self):
        self.store.conn.execute("UPDATE object_versions SET lineage = 'gold_native_staging' "
                                "WHERE object_id = ?", (self.ids["po_b"],))
        st = stages(self.answer(self.t, {"from": self.ids["po_b"]}).result)
        root = st["purchase_order"]
        self.assertEqual(root["missing"]["reason"], "staging_root")   # typed, never proven
        self.assertEqual(root["reading"]["kind"], "found")
        self.assertIn("staging data", root["reading"]["text"])
        self.assertIn("lead, not proven", root["reading"]["text"])

    def test_partial_capture_or_missing_build_report_is_unknown(self):
        fresh = [{"source": "portal_api", "captured_at": "2099-02-01T11:00:00Z",
                  "completeness": "complete", "stale": False},
                 {"source": "bc", "captured_at": "2099-02-01T11:00:00Z",
                  "completeness": "partial", "stale": False}]
        store = Store(freshness=fresh)
        ids = purchase_fixture(store)
        st = stages(self.answer(tools(store), {"from": ids["po_b"]}).result)
        self.assertEqual(st["vendor_invoice"]["missing"]["state"], "unknown")
        store = Store()
        store.conn.execute("DELETE FROM store_meta WHERE key = 'build_report'")
        ids = purchase_fixture(store)
        result = self.answer(tools(store), {"from": ids["po_b"]}).result
        self.assertEqual(stages(result)["vendor_invoice"]["missing"]["state"], "unknown")
        self.assertIn("build_report_missing", [g["reason"] for g in result["gaps"]])

    def test_past_as_known_at_never_claims_absence(self):
        st = stages(self.answer(self.t, {"from": self.ids["po_b"],
                                         "as_known_at": "2099-01-20"}).result)
        self.assertEqual(st["vendor_invoice"]["missing"]["state"], "unknown")

    def test_hidden_bc_records_never_counted(self):
        policy = ot.ReadPolicy("plx", {"api:*": frozenset({"sales"}),
                                       "bc:company_a/purchaseOrders": frozenset({"sales"})})
        base = ot.OntologyTools(ot.StoreReader(self.store.conn), policy, schema=SCHEMA,
                                contracts=CONTRACTS, clock=lambda: NOW)
        answer = self.answer(op.ProcessTools(base, GRAPHS), {"from": self.ids["po_a"]},
                             actor=SALES)
        st = stages(answer.result)
        for sid in ("receipt", "vendor_invoice"):
            self.assertEqual(st[sid]["state"], "missing")
            self.assertEqual((st[sid]["missing"]["state"], st[sid]["missing"]["reason"]),
                             ("inaccessible", tt.WITHHELD), sid)
        self.assertEqual(answer.result["quantity_checks"], [])
        self.assertNotIn(self.ids["pi_a"], json.dumps(answer.result) + json.dumps(answer.roots))

    def test_finance_marked_prices_are_withheld_without_finance(self):
        policy = ot.ReadPolicy("plx", {"api:*": frozenset({"ops"}), "bc:*": frozenset({"ops"})})
        base = ot.OntologyTools(ot.StoreReader(self.store.conn), policy, schema=SCHEMA,
                                contracts=CONTRACTS, clock=lambda: NOW)
        ops = Actor("plx", frozenset({"ops"}))
        answer = self.answer(op.ProcessTools(base, GRAPHS), {"from": self.ids["po_a"]}, actor=ops)
        for check in answer.result["quantity_checks"]:
            for value in check["values"]:
                self.assertNotEqual(value["value_type"], "money")
        # The price check (a disagreement for a finance reader) stays listed, undecided
        # and with nothing to cite, so no reduction over the checks reads it as agreement.
        price = [c for c in answer.result["quantity_checks"]
                 if c["roles"] == ["ordered", "invoiced"]]
        self.assertEqual([(c["agree"], c["values"]) for c in price], [(None, [])])
        self.assertIn("marking_withheld", {g["reason"] for g in answer.result["gaps"]})

    def _gold_po_mirrored_in_bc(self, bc_received="100"):
        """A gold PO with one receipt of 100 that the Portal pushed to BC: the BC twin
        PO carries the same receipt (and the vendor invoice, BC only)."""
        s = self.store
        gold_item = s.obj("Product:portal.app_product:p-1")
        bc_item = s.obj("Product:bc.items:item-g")
        s.link("Product.same_record_as", gold_item, bc_item, kind="same_record")
        po = s.obj("PurchaseOrder:portal.app_purchase_order:po-g")
        s.val(po, "status", "text", "ORDERED")
        pol = s.obj("PurchaseOrderLine:portal.app_purchase_order_line:po-g-1")
        s.val(pol, "ordered_quantity", "quantity", "100", unit="each", role="ordered")
        s.link("PurchaseOrderLine.line_of", pol, po)
        s.link("PurchaseOrderLine.for_item", pol, gold_item)
        rec = s.obj("Receipt:portal.app_receipt:rc-g")
        s.link("Receipt.receives", rec, po, tier="stored_by_text_join")
        rl = s.obj("ReceiptLine:portal.app_receipt_line:rc-g-1")
        s.val(rl, "received_quantity", "quantity", "100", unit="each", role="received")
        s.link("ReceiptLine.line_of", rl, rec)
        s.link("ReceiptLine.for_item", rl, gold_item)
        twin = s.obj("PurchaseOrder:bc.purchase_orders:po-g-bc")
        s.link("PurchaseOrder.same_record_as", po, twin, kind="same_record")
        brec = s.obj("Receipt:bc.purchase_receipts:rc-g-bc")
        s.link("Receipt.receives", brec, twin)
        brl = s.obj("ReceiptLine:bc.purchase_receipt_lines:rc-g-bc-1")
        s.val(brl, "received_quantity", "quantity", bc_received, unit="each", role="received")
        s.link("ReceiptLine.line_of", brl, brec)
        s.link("ReceiptLine.for_item", brl, bc_item)
        inv = s.obj("PurchaseInvoice:bc.purchase_invoices:pi-g")
        s.val(inv, "status", "text", "Open")
        s.link("PurchaseInvoice.invoices", inv, twin)
        pil = s.obj("PurchaseInvoiceLine:bc.purchase_invoice_lines:pi-g-1")
        s.val(pil, "invoiced_quantity", "quantity", "100", unit="each", role="invoiced")
        s.link("PurchaseInvoiceLine.line_of", pil, inv)
        s.link("PurchaseInvoiceLine.for_item", pil, bc_item)
        return po

    def test_an_invoice_naming_the_po_reaches_it_once_bc_deleted_the_bc_po(self):
        # gap-3d83c2e43cea: the Portal finds a PO's invoices by TRIM(order_number);
        # a Portal PO whose BC PO no longer exists reached no vendor invoice.
        s = Store()
        po = named_po_fixture(s)
        answer = self.answer(tools(s), {"from": po, **P2I})
        self.assertEqual(stages(answer.result)["vendor_invoice"]["state"], "present")
        checks = {tuple(c["roles"]): c for c in answer.result["quantity_checks"]}
        self.assertIs(checks[("received", "invoiced")]["agree"], True)
        self.assertIn("received 100 each (1 lines) vs invoiced 100 each (1 lines)",
                      checks[("received", "invoiced")]["note"])
        # a link capped for another reason too (or an unreadable cap) stays a lead
        for also in ([{"feature": "tier_cap", "agrees": None, "cap": "lead",
                       "because": "a_declared_pair_is_unknown"}], '["unreadable"]'):
            with self.subTest(also=also):
                s = Store()
                po = named_po_fixture(s, also=also if isinstance(also, list) else ())
                if not isinstance(also, list):
                    s.conn.execute("UPDATE links SET why_not = ? WHERE link_type = "
                                   "'PurchaseInvoice.names_order'", (also,))
                answer = self.answer(tools(s), {"from": po, **P2I})
                missing = stages(answer.result)["vendor_invoice"]
                self.assertEqual((missing["state"], missing["missing"]["reason"]),
                                 ("missing", "absence_unproven"))

    def test_the_projector_binds_an_invoice_to_the_po_its_trimmed_number_names(self):
        # the real projector over captured rows (synthetic): the Portal's TRIM(order_number)
        # rule, capped at lead for an unknown company, counted only for one company
        from ontology import identifiers
        from ontology.project import CapturedRecord, build_store

        class Static:
            def __init__(self, source, records):
                self.source, self.rows = source, records

            def records(self, relations):
                return {r: [x for x in self.rows if x.relation == r] for r in relations}

            def describe(self):
                return {"generation": "static", "relations": {}}

        def captured(relation, key, row, company=None):
            audience = f"bc:{company}/{relation}" if company else f"api:{relation}"
            handle = (f"bc://{company}/{relation}/{key}@c-1" if company
                      else f"api://{relation}/{key}@c-1")
            return CapturedRecord("bc" if company else "portal_api", relation,
                                  json.dumps({"id": key}), {"id": key, **row}, handle, "row",
                                  "0" * 64, T0, None, T0, audience,
                                  capture_lineage=f"bc_{company}" if company else None)

        po = captured("public.app_purchase_order", "p1", {
            "po_number": " PO-N1 ", "status": "ORDERED",
            "custom_fields": {"source": "etl-silver-to-gold"}})
        for companies, state in ((("company_a",), "present"),
                                 (("company_a", "company_b"), "ambiguous_target")):
            with self.subTest(companies=companies):
                invoices = [captured("purchaseInvoices", f"{c}-1", {
                    "number": "PI-1", "orderNumber": "PO-N1", "status": "Open"}, c)
                    for c in companies]
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                path = Path(tmp.name)
                build_store(path / "s.sqlite", schema_loader.load_schema(),
                            {"portal_api": Static("portal_api", [po]),
                             "bc": Static("bc", invoices)},
                            normalizer=identifiers.normalize, clock=lambda: NOW)
                reader = ot.StoreReader.open(path / "s.sqlite")
                self.addCleanup(reader.conn.close)
                rows = reader.conn.execute("SELECT tier, why_not FROM links WHERE link_type = "
                                           "'PurchaseInvoice.names_order'").fetchall()
                self.assertEqual([(r[0], json.loads(r[1])[0]["because"]) for r in rows],
                                 [("lead", "bc_company_not_known")] * len(companies))
                base = ot.OntologyTools(reader, POLICY, schema=SCHEMA, contracts=CONTRACTS,
                                        clock=lambda: NOW)
                (oid,) = [r[0] for r in reader.conn.execute(
                    "SELECT object_id FROM objects WHERE object_type = 'PurchaseOrder'")]
                answer = self.answer(op.ProcessTools(base, GRAPHS), {"from": oid})
                stage = stages(answer.result)["vendor_invoice"]
                self.assertEqual(stage.get("missing", {}).get("reason", stage["state"]), state)

    def test_a_number_naming_two_orders_is_an_ambiguous_gap_never_a_pick(self):
        # invoices of two BC companies name the number (an order of it in each), or one
        # invoice names two gold POs: neither invoice is counted for the PO
        for companies, twins in ((("company_a", "company_b"), 0), (("company_a",), 1)):
            with self.subTest(companies=companies, portal_twins=twins):
                s = Store()
                po = named_po_fixture(s, companies, portal_twins=twins)
                answer = self.answer(tools(s), {"from": po, **P2I})
                missing = stages(answer.result)["vendor_invoice"]["missing"]
                self.assertEqual((missing["state"], missing["reason"]),
                                 ("unknown", "ambiguous_target"))
                checks = {tuple(c["roles"]): c for c in answer.result["quantity_checks"]}
                self.assertIsNone(checks.get(("received", "invoiced"), {}).get("agree"))
        # a reader of one company cannot tell: withheld, the other company named nowhere
        s = Store()
        po = named_po_fixture(s, ("company_a", "company_b"))
        one = ot.ReadPolicy("plx", {"api:*": frozenset({"staff"}),
                                    "bc:company_a/*": frozenset({"staff"})},
                            {m: frozenset({"staff"}) for m in store_ddl.MARKINGS})
        base = ot.OntologyTools(ot.StoreReader(s.conn), one, schema=SCHEMA,
                                contracts=CONTRACTS, clock=lambda: NOW)
        answer = self.answer(op.ProcessTools(base, GRAPHS), {"from": po, **P2I})
        missing = stages(answer.result)["vendor_invoice"]["missing"]
        self.assertEqual((missing["state"], missing["reason"]),
                         ("inaccessible", "withheld_by_permission"))
        self.assertNotIn("company_b", json.dumps(answer.result))

    def test_receipt_mirrored_in_bc_is_counted_once(self):
        po = self._gold_po_mirrored_in_bc()
        # named: the PO is open (nothing received on its own line), so the unnamed call
        # gets its delivery window
        answer = self.answer(self.t, {"from": po, **P2I})
        checks = {tuple(c["roles"]): c for c in answer.result["quantity_checks"]}
        qty = checks[("ordered", "received")]
        self.assertIs(qty["agree"], True)
        self.assertIn("ordered 100 each (1 lines) vs received 100 each (1 lines)", qty["note"])
        self.assertNotIn("200", qty["note"])
        self.assertEqual({v["citation"]["l1_handle"].split("/")[0] for v in qty["values"]},
                         {"api:"})  # the gold receipt only: one system of record
        inv = checks[("received", "invoiced")]
        self.assertIs(inv["agree"], True)
        self.assertIn("invoiced figures come from the twin system", inv["note"])
        self.assertNotIn("cross_system_conflict", [g["reason"] for g in answer.result["gaps"]])

    def test_disagreeing_twin_receipt_is_a_conflict_not_a_sum(self):
        po = self._gold_po_mirrored_in_bc(bc_received="90")
        answer = self.answer(self.t, {"from": po, **P2I})
        checks = {tuple(c["roles"]): c for c in answer.result["quantity_checks"]}
        qty = checks[("ordered", "received")]
        self.assertIn("vs received 100 each (1 lines)", qty["note"])
        self.assertIsNone(qty["agree"])  # a conflicted side is never decided
        self.assertIn("cross-system conflict on the received side", qty["note"])
        self.assertIsNone(checks[("received", "invoiced")]["agree"])
        (gap,) = [g for g in answer.result["gaps"] if g["reason"] == "cross_system_conflict"]
        self.assertIn("own system says 100", gap["detail"])
        self.assertIn("twin system says 90", gap["detail"])
        (conflict,) = answer.roots[0]["conflicts"]
        self.assertEqual((conflict["role"], conflict["own"], conflict["twin"]),
                         ("received", "100", "90"))
        self.assertEqual(len(conflict["values"]), 2)

    def test_hidden_own_lines_never_fall_back_to_the_twin(self):
        # Repro (review of e8d312a): a reader without the gold receipt-line unit must
        # not get the twin's 90 in place of the gold 100; staff and the restricted
        # reader may differ only in agree=null, never in a decided answer.
        po = self._gold_po_mirrored_in_bc(bc_received="90")
        policy = ot.ReadPolicy("plx", {"api:*": frozenset({"x"}), "bc:*": frozenset({"x"}),
                                       "api:public.app_receipt_line": frozenset({"staff"})})
        base = ot.OntologyTools(ot.StoreReader(self.store.conn), policy, schema=SCHEMA,
                                contracts=CONTRACTS, clock=lambda: NOW)
        answer = self.answer(op.ProcessTools(base, GRAPHS), {"from": po, **P2I},
                             actor=Actor("plx", frozenset({"x"})))
        checks = [c for c in answer.result["quantity_checks"] if "received" in c["roles"]]
        self.assertTrue(checks)
        for check in checks:
            self.assertIsNone(check["agree"])
            self.assertNotIn("received figures come from the twin", check["note"])
            self.assertNotIn("bc://company_a/purchaseReceiptLines",
                             json.dumps(check["values"]))
        self.assertEqual(answer.roots[0]["conflicts"], [])
        self.assertFalse(any(c["finding"] for c in answer.roots[0]["checks"]))
        self.assertIn("evidence_not_fully_visible", [g["reason"] for g in answer.result["gaps"]])

    def test_draft_side_leaves_its_comparisons_undecided(self):
        self.store.conn.execute("UPDATE object_values SET value = 'Draft' WHERE object_id = ? "
                                "AND property = 'status'", (self.ids["pi_a"],))
        result = self.answer(self.t, {"from": self.ids["po_a"]}).result
        checks = {tuple(c["roles"]): c for c in result["quantity_checks"]}
        for roles in (("received", "invoiced"), ("ordered", "invoiced")):
            self.assertIsNone(checks[roles]["agree"])
            self.assertIn("the invoiced side is a draft", checks[roles]["note"])
        self.assertIs(checks[("ordered", "received")]["agree"], False)  # not a draft side
        (gap,) = [g for g in result["gaps"] if g["reason"] == "side_draft"]
        self.assertEqual(gap["subject"], "invoiced")

    def test_draft_po_side_is_undecided(self):
        store, (po,) = PermissionMonotonicityTests.po_world("DRAFT")
        result = self.answer(tools(store), {"from": po}).result
        (qty,) = [c for c in result["quantity_checks"] if c["roles"] == ["ordered", "received"]]
        self.assertIsNone(qty["agree"])
        self.assertIn("the ordered side is a draft", qty["note"])
        self.assertIn("side_draft", [g["reason"] for g in result["gaps"]])

    def test_lines_without_an_item_make_the_sum_unknown(self):
        s = self.store
        rec = s.obj("Receipt:bc.purchase_receipts:rc-a2")
        s.link("Receipt.receives", rec, self.ids["po_a"])
        rl = s.obj("ReceiptLine:bc.purchase_receipt_lines:rc-a2-1")
        s.val(rl, "received_quantity", "quantity", "10", unit="each", role="received")
        s.link("ReceiptLine.line_of", rl, rec)
        result = self.answer(self.t, {"from": self.ids["po_a"]}).result
        checks = {tuple(c["roles"]): c for c in result["quantity_checks"]}
        qty = [c for c in result["quantity_checks"] if c["roles"] == ["ordered", "received"]]
        self.assertTrue(qty)
        self.assertTrue(all(c["agree"] is None for c in qty))
        self.assertTrue(any("no single visible Product, so per-item quantity values" in c["note"]
                            for c in qty))
        # the ungrouped line is a receipt line: the PO-vs-invoice price comparison has
        # no ungrouped line on its sides and stands (prices refuse only for their sides)
        self.assertIs(checks[("ordered", "invoiced")]["agree"], False)

    def test_an_ungrouped_line_refuses_the_price_comparison_too(self):
        s = self.store
        pil = s.obj("PurchaseInvoiceLine:bc.purchase_invoice_lines:pi-a-9")
        s.val(pil, "invoiced_quantity", "quantity", "0", unit="each", role="invoiced")
        s.val(pil, "unit_cost", "money", "2.50", unit="each", currency="USD",
              markings='["finance"]')
        s.link("PurchaseInvoiceLine.line_of", pil, self.ids["pi_a"])
        result = self.answer(self.t, {"from": self.ids["po_a"]}).result
        prices = [c for c in result["quantity_checks"] if c["roles"] == ["ordered", "invoiced"]]
        self.assertTrue(prices)
        self.assertTrue(all(c["agree"] is None for c in prices))
        self.assertTrue(any("so per-item price values are not compared" in c["note"]
                            for c in prices))

    def test_over_receipt_is_complete_and_reported(self):
        self.store.conn.execute(
            "UPDATE object_values SET value = '55' WHERE object_id = "
            "'ReceiptLine:bc.purchase_receipt_lines:rc-b-1' AND property = 'received_quantity'")
        checks = {tuple(c["roles"]): c for c in
                  self.answer(self.t, {"from": self.ids["po_b"]}).result["quantity_checks"]}
        qty = checks[("ordered", "received")]
        self.assertIs(qty["agree"], True)
        self.assertIn("received exceeds ordered by 5 each", qty["note"])

    def test_receipt_on_a_draft_po_is_out_of_order(self):
        s = self.store
        po = s.obj("PurchaseOrder:portal.app_purchase_order:po-draft")
        s.val(po, "status", "text", "DRAFT")
        rec = s.obj("Receipt:portal.app_receipt:rc-draft")
        s.link("Receipt.receives", rec, po, tier="stored_by_text_join")
        answer = self.answer(self.t, {"from": po})
        st = stages(answer.result)
        self.assertEqual(st["purchase_order"]["missing"]["reason"], "status_not_reached")
        self.assertEqual(st["receipt"]["state"], "out_of_order")
        self.assertIn(rec, st["receipt"]["events"])
        self.assertEqual(answer.roots[0]["current_stage"], "purchase_order")

    def test_wrong_process_for_the_root_is_an_error(self):
        result = self.t.call("plx.process", {"from": self.ids["po_a"],
                                             "process": "sales_order_lifecycle"}, STAFF)
        self.assertEqual(result["error"]["code"], "root_type_mismatch")
        result = self.t.call("plx.process", {"from": self.ids["po_a"], "process": "nope"}, STAFF)
        self.assertEqual(result["error"]["code"], "unknown_process")
        self.assertEqual(validate(result, CONTRACTS["tools"]["plx.process"]["output"],
                                  root=CONTRACTS), [])


class ManufacturingTests(Mixin, unittest.TestCase):
    """plx.process on an FM assembly order: stage, next stage and overdue per stage, with
    citations, decided only from declared due dates at as_known_at (NOW = 2099-02-01)."""

    def run_ao(self, actor=STAFF, **world):
        store = Store()
        ids = ao_fixture(store, **world)
        return self.answer(tools(store), {"from": ids["ao"]}, actor), ids

    def run_mo(self, lineage="gold_etl_from_sage", required="2099-01-20", shipped=None,
               sage_shipped=False):
        """A Portal MO on a gold order of ``lineage`` with no FM record, its required
        date, and optionally a linked Portal shipment or a Sage twin's posted shipment."""
        s = Store()
        so = s.obj("SalesOrder:portal.app_sales_order:so-mo", lineage)
        s.event("SalesOrder.required", so, required)
        line = s.obj("SalesOrderLine:portal.app_sales_order_line:so-mo-l1", lineage)
        s.link("SalesOrderLine.line_of", line, so)
        oid = mo(s, "mo-ao", "COMPLETED", line)
        if shipped:
            shipment(s, "sh-mo", shipped, so)
        if sage_shipped:
            sage = s.obj("SalesOrder:portal.sage_oe_order_header:so-mo-sage", "sage_bronze")
            s.link("SalesOrder.same_record_as", so, sage, kind="same_record")
            posted = s.obj("Shipment:portal.sage_oe_shipment_header:sh-sage", "sage_bronze")
            s.link("Shipment.ships", posted, sage)
        return self.answer(tools(s), {"from": oid}).result

    def test_stage_next_and_overdue_follow_the_records(self):
        answer, ids = self.run_ao()
        result = answer.result
        self.assertEqual(result["process"], "manufacturing_order")   # the AO's one graph
        st = stages(result)
        self.assertEqual({k: v["state"] for k, v in st.items()},
                         {"sales_order": "present", "assembly_order": "present",
                          "bulk_batch": "present", "bulk_qc_release": "present",
                          "filling": "present", "assembly_realized": "present",
                          "shipment": "missing"})
        for stage in ("bulk_qc_release", "filling", "assembly_realized"):
            self.assertTrue(st[stage]["citations"], stage)   # evidence per stage, cited
        # An FM order with no Portal twin: no shipment source links to it (FM shipments
        # reach app_shipment without a sales_order_id), so nothing proves it unshipped,
        # where it stands is not stated and nothing is overdue.
        gap = st["shipment"]["missing"]
        self.assertEqual((gap["state"], gap["reason"]), ("unknown", "absence_unproven"))
        self.assertIn("etl-silver-to-gold.ts@12db891", gap["detail"])
        self.assertNotIn("current_stage", result)
        due = {d["stage"]: d for d in result["due"]}
        self.assertIsNone(due["shipment"]["overdue"])
        self.assertNotIn("days_overdue", due["shipment"])
        self.assertEqual(due["shipment"]["gap"]["reason"], "stage_undecided")
        self.assertEqual([v["value"] for v in due["shipment"]["due_by"]], ["2099-01-20"])
        self.assertTrue(due["shipment"]["due_by"][0]["citation"]["l1_handle"].startswith("api://"))
        self.assertIs(due["filling"]["overdue"], False)   # reached is never overdue

    def test_shipment_reads_the_sales_lifecycle_evidence_of_every_order_record(self):
        fm_twin_unlinked = self.run_ao(portal_required="2099-01-20")[0].result
        self.assertEqual(stages(fm_twin_unlinked)["shipment"]["missing"]["reason"],
                         "absence_unproven")      # the twin proves nothing for an FM order
        shipped = self.run_ao(shipped="SHIPPED")[0].result
        self.assertEqual(stages(shipped)["shipment"]["state"], "present")
        self.assertIs({d["stage"]: d for d in shipped["due"]}["shipment"]["overdue"], False)
        sage = self.run_mo(sage_shipped=True)
        self.assertEqual(stages(sage)["shipment"]["state"], "present")   # Sage posted shipment
        self.assertIn("Shipment:portal.sage_oe_shipment_header:sh-sage",
                      stages(sage)["shipment"]["events"])
        etl_from_fm = self.run_mo(lineage="gold_etl_from_fm")
        self.assertEqual(stages(etl_from_fm)["shipment"]["missing"]["reason"],
                         "absence_unproven")
        self.assertIsNone({d["stage"]: d for d in etl_from_fm["due"]}["shipment"]["overdue"])

    def test_overdue_is_decided_only_from_one_proven_due_date(self):
        # a Portal MO on a Sage-era gold order: the shipment's absence is provable
        # (world, shipment state, overdue, days overdue)
        cases = [
            ({}, "missing", True, 12),
            ({"required": "2099-03-01"}, "missing", False, None),
            ({"shipped": "SHIPPED"}, "present", False, None),
            ({"shipped": "DRAFT"}, "missing", True, 12),
        ]
        for world, state, overdue, days in cases:
            with self.subTest(world=world):
                result = self.run_mo(**world)
                shipment_stage = stages(result)["shipment"]
                self.assertEqual(shipment_stage["state"], state)
                if state == "missing":
                    self.assertEqual(shipment_stage["missing"]["state"], "absent_in_source")
                due = {d["stage"]: d for d in result["due"]}
                self.assertIs(due["shipment"]["overdue"], overdue)
                self.assertEqual(due["shipment"].get("days_overdue"), days)
                self.assertNotIn("gap", due["shipment"])
        for world, reason in (({"required": ()}, "no_due_date"),
                              ({"portal_required": "2099-01-25", "shipped": "DRAFT"},
                               "due_dates_disagree")):
            with self.subTest(world=world):
                due = {d["stage"]: d for d in self.run_ao(**world)[0].result["due"]}
                self.assertIsNone(due["shipment"]["overdue"])
                self.assertEqual(due["shipment"]["gap"]["reason"], reason)
        result = self.run_ao(status="Committed", tasks=("Setup",))[0].result
        self.assertEqual((result["current_stage"], result["next_stage"]),
                         ("filling", "assembly_realized"))
        due = {d["stage"]: d for d in result["due"]}
        self.assertEqual((due["filling"]["overdue"], due["filling"]["days_overdue"]), (True, 27))
        # a due day has passed once it has ended in ET (decided 2026-10-28), not UTC:
        # 2099-01-21T04:59Z is still Jan 20 in ET (EST), 05:00Z is Jan 21
        self.assertEqual(op._business_date(dt.datetime(2099, 1, 21, 4, 59,
                                                       tzinfo=dt.timezone.utc)),
                         dt.date(2099, 1, 20))
        self.assertEqual(op._business_date(dt.datetime(2099, 1, 21, 5, tzinfo=dt.timezone.utc)),
                         dt.date(2099, 1, 21))

    def test_a_date_a_document_contradicts_or_could_contradict_is_never_overdue(self):
        # The shipment stage as a proven absence (no SHIPPING row), so only the due date
        # decides; the FM order's required date is 2099-01-20, 12 days past.
        graph = copy.deepcopy(GRAPHS["manufacturing_order"])
        (ship,) = [s for s in graph["stages"] if s["id"] == "shipment"]
        del ship["evidence_of"]
        ship["evidence"] = [{"object_type": "ProductionTask",
                             "bindings": ["portal.fm_production_ao"],
                             "paths": [[{"link": "ProductionTask.task_of_order",
                                         "direction": "reverse"}]],
                             "predicate": {"kind": "status", "property": "task_type",
                                           "compare": "upper", "done": ["SHIPPING"],
                                           "quantifier": "any"}}]
        for packet_date, reason in (("2099-01-25", "due_date_conflict"),
                                    (None, "due_date_conflict"),   # blank on paper
                                    ("2099-01-20", "due_date_attestation_unproven"),
                                    (False, "due_date_attestation_unproven")):  # no packet
            with self.subTest(packet_date=packet_date):
                store = Store()
                ids = ao_fixture(store)
                store.val(ids["so"], "required_date", "date", "2099-01-20")
                if packet_date is not False:
                    pk = store.obj("AssemblyOrderPacket:sharepoint.ao_packets:pk-1")
                    store.val(pk, "required_date", "date", packet_date)
                    store.val(pk, "evidence", "text", json.dumps({"fields": {
                        "required_date": {"value": packet_date} if packet_date else
                        {"value": None, "gap": "value_missing"}}}))
                    store.link("AssemblyOrderPacket.packet_of", pk, ids["ao"], tier="lead")
                t = op.ProcessTools(tools(store).base, {**GRAPHS, "manufacturing_order": graph})
                result = self.answer(t, {"from": ids["ao"]}).result
                self.assertEqual(stages(result)["shipment"]["missing"]["state"],
                                 "absent_in_source")
                due = {d["stage"]: d for d in result["due"]}["shipment"]
                self.assertEqual((due["overdue"], due["gap"]["reason"]), (None, reason))
                self.assertNotIn("days_overdue", due)
                if reason == "due_date_conflict":
                    self.assertEqual(due["gap"]["subject"], "SalesOrder.required_date vs "
                                                            "AssemblyOrderPacket.required_date")
                    self.assertIn(due["gap"]["subject"],
                                  {c["subject"] for c in result["conflicts"]})

    def test_withheld_evidence_is_withheld_not_missing(self):
        # sales lacks the quality_controlled marking on the batch's QC status: the stage is
        # withheld, so where the AO stands is not stated to this reader (never missing)
        answer, ids = self.run_ao(actor=SALES)
        st = stages(answer.result)
        gap = st["bulk_qc_release"]["missing"]
        self.assertEqual((gap["state"], gap["reason"]), ("inaccessible", tt.WITHHELD))
        self.assertNotIn("current_stage", answer.result)
        self.assertNotIn("next_stage", answer.result)
        self.assertNotIn("Released", json.dumps(answer.result))
        # a reader without the grant on the evidence's unit sees the same, from the path
        store = Store()
        make = sales_fixture(store)["make"]
        st = stages(self.answer(tools(store), {"from": make}, SALES).result)
        self.assertEqual((st["manufacturing_order"]["missing"]["state"],
                          st["manufacturing_order"]["missing"]["reason"]),
                         ("inaccessible", tt.WITHHELD))

    def test_a_named_only_graph_never_makes_an_unnamed_call_ambiguous(self):
        store = Store()
        po = store.obj("PurchaseOrder:bc.purchase_orders:po-late")
        # Both graphs read the BC header by the shared PO status definition: released.
        store.val(po, "bc_status", "text", "Open")
        store.event("PurchaseOrder.bc_expected", po, "2099-01-15")
        t = tools(store)
        self.assertEqual(self.answer(t, {"from": po}).result["process"], "purchase_to_invoice")
        result = self.answer(t, {"from": po, "process": "po_delivery_window"}).result
        self.assertEqual(result["current_stage"], "receipt")
        due = {d["stage"]: d for d in result["due"]}
        self.assertEqual((due["receipt"]["overdue"], due["receipt"]["days_overdue"]), (True, 17))


class DependencyTests(Mixin, unittest.TestCase):
    """Cross-process prerequisites (RSN-01, WS-09, EXE-07): per unreached stage, the
    stages of other processes it waits on, proven or with their gap, and who owns each."""

    def dependency_answer(self, root_key="so", actor=STAFF, policy=None, graphs=None, process=None, **world):
        store = Store()
        ids = material_fixture(store, **world)
        base = ot.OntologyTools(ot.StoreReader(store.conn), policy or POLICY, schema=SCHEMA,
                                contracts=CONTRACTS, clock=lambda: NOW)
        root = ids["bc_pos"][0] if root_key == "bc_po" else ids[root_key]
        arguments = {"from": root, **({"process": process} if process else {})}
        return self.answer(op.ProcessTools(base, graphs or GRAPHS), arguments, actor).result, ids

    def test_unreceived_material_po_blocks_the_order_through_its_assembly_order(self):
        result, ids = self.dependency_answer(pos=("open", "received", "cancelled"),
                                             bc_pos=("open", "received"))
        st = stages(result)
        # who acts on each stage not reached: the Portal department, as the graph declares
        self.assertEqual({k: v.get("owner_role") for k, v in st.items()}, {
            "sales_order": None, "manufacturing_order": "PROD", "release_order": "SALES",
            "pick_list": "WH", "shipment": "SHIP", "customer_invoice": "FIN"})
        stage = st["manufacturing_order"]
        self.assertEqual((stage["state"], result["current_stage"]),
                         ("missing", "manufacturing_order"))
        self.assertEqual(stage["prerequisites"], "blocked")
        got = [(b["process"], b["stage"], b["owner_role"], b.get("object"), b.get("via"),
                b["missing"]["state"], b["missing"]["reason"]) for b in stage["blockers"]]
        # The MO is a proven blocker (pegged to the order line). The material POs are
        # linked by item only (Portal pegs no PO to an order): the purchasing entries are
        # undecided candidates, never proven, the Portal PO and the BC-only one reached
        # through the item's BC twin alike, and the link is itself an entry. The received
        # POs prove nothing and the cancelled one waits on nothing: one PO, once.
        via = f"manufacturing_order.assembly_realized {ids['mo']}"
        self.assertEqual(got, [
            ("manufacturing_order", "assembly_realized", "PROD", ids["mo"], None,
             "absent_in_source", "status_not_reached"),
            ("purchase_to_invoice", "receipt", "WH", None, via, "unknown",
             "dependency_not_pegged"),
            ("purchase_to_invoice", "receipt", "WH", ids["bc_pos"][0], via, "unknown",
             "dependency_unproven"),
            ("purchase_to_invoice", "receipt", "WH", ids["pos"][0], via, "unknown",
             "dependency_unproven")])
        self.assertIn("by item", stage["blockers"][1]["note"])   # what the link does not prove
        self.assertNotIn("blockers", st["sales_order"])           # reached: nothing to wait on
        # the assembly order's own answer names the same candidates, undecided; a BC-only
        # open PO is a blocker entry on its own
        for world in ({"pos": ("open",)}, {"pos": (), "bc_pos": ("open",)}):
            ao, ids = self.dependency_answer("ao", **world)
            realized = stages(ao)["assembly_realized"]
            self.assertEqual((realized["prerequisites"],
                              [(b.get("object"), b["owner_role"], b["missing"]["reason"])
                               for b in realized["blockers"]]),
                             ("undecided", [(None, "WH", "dependency_not_pegged"),
                                            ((ids["pos"] + ids["bc_pos"])[0], "WH",
                                             "dependency_unproven")]), world)
        # every stage owner is a Portal Department code, never a role such as MANAGER
        from operating.employee_permissions import PORTAL_DEPARTMENT_CODES
        departments = set(PORTAL_DEPARTMENT_CODES.values())
        self.assertEqual(set(op.load_format()["$defs"]["owner_role"]["enum"]), departments)
        owners = {s["owner_role"] for g in GRAPHS.values() for s in g["stages"]}
        self.assertLessEqual(owners, departments)
        self.assertNotIn("MANAGER", owners)

    def test_the_depth_cap_stops_the_walk_and_reports_it(self):
        # With one level allowed, the MO's own prerequisites are not read: an entry
        # says so instead of the PO below it vanishing silently.
        with unittest.mock.patch.object(op, "MAX_PREREQUISITE_DEPTH", 1):
            result, ids = self.dependency_answer(pos=("open",))
        stage = stages(result)["manufacturing_order"]
        self.assertEqual(stage["prerequisites"], "blocked")
        self.assertEqual(
            [(b["stage"], b.get("object"), b.get("via"), b["missing"]["state"],
              b["missing"]["reason"]) for b in stage["blockers"]],
            [("assembly_realized", ids["mo"], None, "absent_in_source", "status_not_reached"),
             ("assembly_realized", None, f"manufacturing_order.assembly_realized {ids['mo']}",
              "unknown", "prerequisite_depth_cap")])
        self.assertNotIn(ids["pos"][0], json.dumps(stage))

    def test_a_po_shared_by_item_never_blocks_unrelated_assembly_orders(self):
        # review of fba1d43b: two AOs use one packaging item; one PO is received, one is
        # open. Neither AO is proven to wait on the open PO.
        store = Store()
        ids = material_fixture(store, pos=("received", "open"))
        other = store.obj("AssemblyOrder:portal.fm_assembly_order:ao-other", "fm_bronze")
        store.val(other, "status", "text", "Committed")
        part = store.obj("AssemblyOrderComponent:portal.fm_assembly_order_item:c-2", "fm_bronze")
        store.link("AssemblyOrderComponent.component_of", part, other)
        store.link("AssemblyOrderComponent.uses_item", part, "Product:portal.fm_packaging:pk-1")
        for root in (ids["ao"], other):
            stage = stages(self.answer(tools(store), {"from": root}).result)["assembly_realized"]
            self.assertEqual(stage["prerequisites"], "undecided", root)
            self.assertEqual([(b.get("object"), b["missing"]["state"])
                              for b in stage["blockers"]],
                             [(None, "unknown"), (ids["pos"][1], "unknown")], root)

    def test_a_withheld_or_unproven_prerequisite_is_a_gap_never_clear(self):
        # direction review of 924cd826: every PO reached by item received is no proof the
        # AO's materials are in (a PO no path reaches, stock not read): undecided, with
        # the coverage entry, never clear
        for world in ({"pos": ("received",)}, {"pos": (), "bc_pos": ("received",)}):
            received, _ = self.dependency_answer("ao", **world)
            stage = stages(received)["assembly_realized"]
            self.assertEqual(stage["prerequisites"], "undecided", world)
            (entry,) = stage["blockers"]
            self.assertEqual((entry.get("object"), entry["missing"]["reason"],
                              op.not_pegged_waits_on(entry["missing"])),
                             (None, "dependency_not_pegged", "met"), world)
        # a PO whose line records goods its receipts do not hold: two measures disagree,
        # read as the purchase graph reads it (rule C), so never a proven blocker
        store = Store()
        ids = material_fixture(store)
        store.conn.execute("UPDATE object_values SET value = '40' "
                           "WHERE property = 'received_quantity'")
        result = self.answer(tools(store), {"from": ids["ao"]}).result
        (_, entry) = stages(result)["assembly_realized"]["blockers"]
        self.assertEqual((entry["missing"]["state"], entry["missing"]["reason"]),
                         ("unknown", "recorded_measure_conflict"))
        no_po = ot.ReadPolicy("plx", {"api:*": frozenset({"staff"}),
                                      "api:public.app_purchase_order": frozenset()},
                              POLICY.markings)
        for world in (("received",), ("open",)):
            result, _ = self.dependency_answer("ao", policy=no_po, pos=world)
            stage = stages(result)["assembly_realized"]
            self.assertEqual(stage["prerequisites"], "undecided", world)
            # the hidden PO is no live record read: the withheld reach is the one entry
            (entry,) = stage["blockers"]
            self.assertNotIn("object", json.dumps(stage["blockers"]))  # the hidden PO unnamed
            self.assertEqual((entry["process"], entry["owner_role"], entry["missing"]["state"]),
                             ("purchase_to_invoice", "WH", "inaccessible"))
        # a sales reader may not read the manufacturing orders: undecided, not clear
        result, ids = self.dependency_answer(actor=SALES)
        stage = stages(result)["manufacturing_order"]
        self.assertEqual(stage["prerequisites"], "undecided")
        self.assertEqual([b["missing"]["state"] for b in stage["blockers"]], ["inaccessible"])
        self.assertNotIn(ids["mo"], json.dumps(result))

    def test_a_new_process_file_declares_its_dependencies_without_code(self):
        import tempfile
        pegged = next(s for s in GRAPHS["sales_order_lifecycle"]["stages"]
                      if s["id"] == "manufacturing_order")["prerequisites"][0]
        by_item = next(s for s in GRAPHS["manufacturing_order"]["stages"]
                       if s["id"] == "assembly_realized")["prerequisites"][0]
        kitting = {
            "$schema": "_process.schema.json", "format": "plx-process-graph/v1",
            "id": "kitting", "description": "Synthetic: an order's goods kitted on the floor.",
            "root_type": "SalesOrder", "implicit": False,
            "stages": [{"id": "kitted", "description": "The order is marked kitted.",
                        "owner_role": "PROD",
                        "evidence": [{"object_type": "SalesOrder", "paths": [[]],
                                      "predicate": {"kind": "status", "property": "status",
                                                    "compare": "upper", "done": ["KITTED"],
                                                    "quantifier": "any"}}],
                        "prerequisites": [copy.deepcopy(pegged)]}],
            "evidence": ["synthetic test graph"]}

        def load(doc):
            tmp = tempfile.mkdtemp()
            for path in op.PROCESS_DIR.glob("*.json"):
                Path(tmp, path.name).write_text(path.read_text("utf-8"), "utf-8")
            Path(tmp, "kitting.json").write_text(json.dumps(doc), "utf-8")
            return op.load_graphs(tmp, schema=SCHEMA)

        result, ids = self.dependency_answer("so", graphs=load(kitting), process="kitting")
        stage = stages(result)["kitted"]
        self.assertEqual((stage["owner_role"], stage["prerequisites"]), ("PROD", "blocked"))
        self.assertEqual([(b["stage"], b["owner_role"], b.get("object"))
                          for b in stage["blockers"]],
                         [("assembly_realized", "PROD", ids["mo"]), ("receipt", "WH", None),
                          ("receipt", "WH", ids["pos"][0])])
        # unknown names fail closed at load: an undeclared stage, a role that is no Portal
        # department; a dependency proven without a served pegging record on every path
        unpegged = dict(copy.deepcopy(by_item), proves_dependency=True,
                        process="manufacturing_order", stage="assembly_realized",
                        object_type="AssemblyOrder", applies_from="assembly_order")
        for path, value in ((("prerequisites", 0, "stage"), "not_a_stage"),
                            (("owner_role",), "MANAGER"), (("owner_role",), "PLANNER"),
                            (("prerequisites", 0, "pegged_by"), ["PurchaseOrderLine.for_item"]),
                            (("prerequisites", 0, "pegged_by"),
                             ["AssemblyOrder.for_order_line", "AssemblyOrderPacket.packet_of"]),
                            (("prerequisites", 0, "proves_dependency"), False),
                            (("prerequisites", 0), {k: v for k, v in pegged.items()
                                                     if k != "pegged_by"})):
            bad = copy.deepcopy(kitting)
            target = bad["stages"][0]
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.assertRaises(op.ProcessGraphError, msg=value):
                load(bad)
        bad = copy.deepcopy(kitting)
        bad.update(root_type="AssemblyOrder")
        bad["stages"][0]["evidence"][0]["object_type"] = "AssemblyOrder"
        bad["stages"][0]["prerequisites"] = [unpegged]
        with self.assertRaises(op.ProcessGraphError) as refused:
            load(bad)
        self.assertIn("pegged_by goes with proves_dependency", str(refused.exception))

    def test_a_record_cycle_is_said_as_a_cycle_never_as_no_record(self):
        # an AO and its MO twin each wait on the other's stage through the identity
        # link: the record already waiting higher up the chain is not read again, and a
        # level reaching only it says so (no_prerequisite_record, cycle wording), undecided
        def graph(own, other):
            twin = [[{"link": "AssemblyOrder.same_record_as", "direction": d}]
                    for d in ("forward", "reverse")]
            return {"$schema": "_process.schema.json", "format": "plx-process-graph/v1",
                    "id": own, "description": "Synthetic: twins waiting on each other.",
                    "root_type": "AssemblyOrder", "implicit": False,
                    "stages": [{"id": "looped", "description": "Marked looped.",
                                "owner_role": "PROD",
                                "evidence": [{"object_type": "AssemblyOrder", "paths": [[]],
                                              "predicate": {"kind": "status",
                                                            "property": "status",
                                                            "compare": "upper",
                                                            "done": ["LOOPED"],
                                                            "quantifier": "any"}}],
                                "prerequisites": [{
                                    "process": other, "stage": "looped",
                                    "object_type": "AssemblyOrder", "paths": twin,
                                    "pegged_by": ["AssemblyOrder.same_record_as"],
                                    "note": "Synthetic: the twin's own stage."}]}],
                    "evidence": ["synthetic test graph"]}

        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp)
        for path in op.PROCESS_DIR.glob("*.json"):
            Path(tmp, path.name).write_text(path.read_text("utf-8"), "utf-8")
        for own, other in (("loop_a", "loop_b"), ("loop_b", "loop_a")):
            Path(tmp, f"{own}.json").write_text(json.dumps(graph(own, other)), "utf-8")
        graphs = op.load_graphs(tmp, schema=SCHEMA)
        with unittest.mock.patch.object(op, "MAX_PREREQUISITE_DEPTH", 4):
            result, ids = self.dependency_answer("mo", graphs=graphs, process="loop_a")
        stage = stages(result)["looped"]
        self.assertEqual(stage["prerequisites"], "blocked")
        got = [(b["process"], b.get("object"), b["missing"]["reason"])
               for b in stage["blockers"]]
        self.assertEqual(got, [
            ("loop_b", ids["ao"], "status_not_reached"),
            ("loop_a", ids["mo"], "status_not_reached"),
            ("loop_b", None, "no_prerequisite_record")])
        detail = stage["blockers"][-1]["missing"]["detail"]
        self.assertIn("the 1 reached already wait on loop_b.looped higher up it (a cycle",
                      detail)
        self.assertIn("no live AssemblyOrder is reached outside this chain", detail)
        self.assertNotIn("is reached, so", detail)  # never the plain no-record wording
        self.assertEqual(stage["blockers"][-1]["via"], f"loop_a.looped {ids['mo']}")

    def test_twins_are_one_candidate_and_a_draft_is_said_never_a_candidate(self):
        # directive 40cbdb929c97 (1): a Portal PO and its BC twin are one business record,
        # one candidate naming both handles in one record-cap slot (no prerequisite_cap)
        with unittest.mock.patch.object(op, "MAX_PREREQUISITE_RECORDS", 1):
            result, ids = self.dependency_answer("ao", pos=("open",), bc_pos=("open",), twins=1)
        stage = stages(result)["assembly_realized"]
        self.assertEqual([b["missing"]["reason"] for b in stage["blockers"]],
                         ["dependency_not_pegged", "dependency_unproven"])
        candidate = stage["blockers"][1]
        self.assertEqual(candidate["missing"]["subject"].split(),
                         sorted(ids["pos"] + ids["bc_pos"]))
        self.assertIn(candidate["object"], ids["pos"] + ids["bc_pos"])
        # a reader not granted BC sees the Portal record alone, its twin never named
        no_bc = ot.ReadPolicy("plx", {"api:*": frozenset({"staff"})}, POLICY.markings)
        result, ids = self.dependency_answer("ao", policy=no_bc, pos=("open",),
                                             bc_pos=("open",), twins=1)
        (candidate,) = [b for b in stages(result)["assembly_realized"]["blockers"]
                        if "object" in b]
        self.assertEqual((candidate["object"], candidate["missing"]["subject"]),
                         (ids["pos"][0], ids["pos"][0]))
        self.assertNotIn(ids["bc_pos"][0], json.dumps(result))
        # (2) a BC Draft PO is not issued: its own answer misses the purchase order stage,
        # and it is no candidate but a prerequisite_draft gap (a cancelled PO stays silent).
        # No live PO is read, so nothing is there to peg: the one entry says no record is
        # reached (stock unread), never also dependency_not_pegged
        result, ids = self.dependency_answer("ao", pos=("cancelled",), bc_pos=("draft",))
        stage = stages(result)["assembly_realized"]
        self.assertEqual((stage["prerequisites"],
                          [(b.get("object"), b["missing"]["reason"]) for b in stage["blockers"]]),
                         ("undecided", [(None, "no_prerequisite_record")]))
        self.assertIn("stock on hand is not read", stage["blockers"][0]["missing"]["detail"])
        self.assertEqual([(g["state"], g["subject"]) for g in result["gaps"]
                          if g["reason"] == "prerequisite_draft"],
                         [("absent_in_source", ids["bc_pos"][0])])
        own, _ = self.dependency_answer("bc_po", pos=(), bc_pos=("draft",))
        self.assertEqual(stages(own)["purchase_order"]["missing"]["reason"],
                         "status_not_reached")
        # a record issued in one system is live: its BC copy in Draft does not hide it
        result, ids = self.dependency_answer("ao", pos=("open",), bc_pos=("draft",), twins=1)
        self.assertEqual([(b.get("object"), b["missing"]["reason"])
                          for b in stages(result)["assembly_realized"]["blockers"]],
                         [(None, "dependency_not_pegged"), (ids["pos"][0], "dependency_unproven")])
        self.assertNotIn("prerequisite_draft", json.dumps(result))

    def test_every_open_po_is_one_blocker_at_1_10_60(self):
        import time
        for n, twins in ((1, 0), (10, 0), (60, 0), (10, 5), (60, 30)):
            started = time.monotonic()
            result, ids = self.dependency_answer("ao", pos=("open",) * (n - n // 2),
                                                 bc_pos=("open",) * (n // 2), twins=twins)
            elapsed = time.monotonic() - started
            stage = stages(result)["assembly_realized"]
            listed = [b["missing"]["subject"] for b in stage["blockers"]
                      if b["missing"]["reason"] == "dependency_unproven"]
            where = (n, twins)
            self.assertEqual(stage["prerequisites"], "undecided", where)
            self.assertEqual(stage["blockers"][0]["missing"]["reason"],
                             "dependency_not_pegged", where)   # first: never cut
            self.assertEqual(len(listed), len(set(listed)), where)   # never twice
            # a twin pair is one record: its handles in one entry
            records = ([" ".join(sorted(pair)) for pair in zip(ids["pos"], ids["bc_pos"][:twins])]
                       + ids["pos"][twins:] + ids["bc_pos"][twins:])
            if len(records) < op.MAX_BLOCKERS:
                self.assertEqual(sorted(listed), sorted(records), where)
                continue
            # 50 read, the rest an entry; 20 listed, and the cut is said, never silent
            self.assertEqual(len(stage["blockers"]), op.MAX_BLOCKERS)
            cut = next(g for g in result["gaps"] if g["reason"] == "blockers_truncated")
            self.assertIn("32 of 52" if len(records) > op.MAX_PREREQUISITE_RECORDS
                          else f"{len(records) + 1 - op.MAX_BLOCKERS} of {len(records) + 1}",
                          cut["detail"])
            self.assertLess(elapsed, 20, where)


def portal_receipt_fixture(*, relations=None, freshness=None, report=None):
    """A Portal PO (10 ordered) with one receipt line (8 received) in a store whose
    portal_api family is partial; ``relations``: its ``relation_completeness`` meta."""
    s = Store(report=report, freshness=freshness if freshness is not None else [
        {"source": "portal_api", "captured_at": "2099-02-01T11:00:00Z",
         "completeness": "partial", "stale": False},
        {"source": "bc", "captured_at": "2099-02-01T11:00:00Z",
         "completeness": "complete", "stale": False}])
    item = s.obj("Product:portal.app_product:p-1")
    po = s.obj("PurchaseOrder:portal.app_purchase_order:po-r")
    s.val(po, "status", "text", "Pending")
    pol = s.obj("PurchaseOrderLine:portal.app_purchase_order_line:po-r-1")
    s.val(pol, "ordered_quantity", "quantity", "10", unit="each", role="ordered")
    s.link("PurchaseOrderLine.line_of", pol, po)
    s.link("PurchaseOrderLine.for_item", pol, item)
    rec = s.obj("Receipt:portal.app_receipt:rc-r")
    s.link("Receipt.receives", rec, po)
    rl = s.obj("ReceiptLine:portal.app_receipt_line:rc-r-1")
    s.val(rl, "received_quantity", "quantity", "8", unit="each", role="received")
    s.link("ReceiptLine.line_of", rl, rec)
    s.link("ReceiptLine.for_item", rl, item)
    if relations is not None:
        s.conn.execute("INSERT INTO store_meta VALUES ('relation_completeness', ?)",
                       (json.dumps(relations),))
    return s, po


def every_portal_relation(**fact):
    """``relation_completeness`` naming every Portal relation the schema binds complete
    (fully traversed before NOW, not stale), with ``fact`` overrides."""
    rels = {b["relation"] for spec in SCHEMA.object_types.values()
            for b in spec.get("bindings", ()) if b["source"] == "portal_api"}
    return {"portal_api": {r: dict({"completeness": "complete", "stale": False,
                                    "full_traversal_at": "2099-02-01T10:00:00Z"}, **fact)
                           for r in sorted(rels)}}


class PortalRelationCompletenessTests(Mixin, unittest.TestCase):
    """devloop/DECISIONS/2026-10-17-answer-correctness-relation-completeness.md: a
    Portal comparison is decided on the completeness of the relations it reads, never
    on the family's alone, and never on row counts."""

    def check(self, store, root, actor=STAFF):
        answer = self.answer(tools(store), {"from": root, **P2I}, actor)
        checks = {tuple(c["roles"]): c for c in answer.result["quantity_checks"]}
        gaps = {g["subject"]: g["reason"] for g in answer.result["gaps"]}
        return checks[("ordered", "received")], gaps

    def test_read_relations_complete_decide_while_the_source_is_partial(self):
        qty, gaps = self.check(*portal_receipt_fixture())
        self.assertIsNone(qty["agree"])
        self.assertEqual(gaps["received"], "scope_not_read")
        qty, gaps = self.check(*portal_receipt_fixture(relations=every_portal_relation()))
        self.assertIs(qty["agree"], False, qty["note"])
        self.assertIn("ordered 10 each (1 lines) vs received 8 each (1 lines); difference 2",
                      qty["note"])
        self.assertNotIn("received", gaps)

    def test_one_read_relation_partial_stale_unclaimed_or_late_stays_undecided(self):
        read = [_relation(b)[1] for b in ("portal.app_purchase_order_line",
                                          "portal.app_receipt", "portal.app_receipt_line",
                                          "portal.app_product")]
        variants = {"partial": {"completeness": "partial", "full_traversal_at": None},
                    "stale": {"stale": True}, "stale_unstated": {"stale": None},
                    "after_the_read": {"full_traversal_at": "2099-02-01T12:00:01Z"},
                    "date_only": {"full_traversal_at": "2099-02-01"},
                    "unknown_state": {"completeness": "unknown"}}
        for relation in read:
            for name, override in [*variants.items(), ("no_claim", None)]:
                with self.subTest(relation=relation, variant=name):
                    relations = every_portal_relation()
                    if override is None:
                        del relations["portal_api"][relation]
                    else:
                        relations["portal_api"][relation].update(override)
                    qty, gaps = self.check(*portal_receipt_fixture(relations=relations))
                    self.assertIsNone(qty["agree"], qty["note"])
                    self.assertIn(gaps.get("ordered", gaps.get("received")),
                                  ("scope_not_read", "evidence_unproven"))
        # A stale family, a capture the build report calls incomplete, a malformed meta
        # and a reader without the receipt lines' grant (its receipt then has no line it
        # may see) stay undecided with every relation complete.
        stale = [{"source": "portal_api", "captured_at": "2099-02-01T11:00:00Z",
                  "completeness": "partial", "stale": True},
                 {"source": "bc", "captured_at": "2099-02-01T11:00:00Z",
                  "completeness": "complete", "stale": False}]
        incomplete = {"counts": {}, "gaps": [{"code": "source_capture_incomplete",
                                              "where": "portal.app_receipt_line",
                                              "count": 1, "samples": []}]}
        denied = ot.ReadPolicy("plx", {"api:*": frozenset({"staff"}),
                                       "api:" + read[2]: frozenset()})
        for name, store, root, policy, reason in (
                ("stale_family",
                 *portal_receipt_fixture(relations=every_portal_relation(), freshness=stale),
                 POLICY, "source_capture_incomplete"),
                ("report_incomplete",
                 *portal_receipt_fixture(relations=every_portal_relation(), report=incomplete),
                 POLICY, "source_capture_incomplete"),
                ("malformed", *portal_receipt_fixture(relations="complete"), POLICY,
                 "scope_not_read"),
                ("grant_withheld", *portal_receipt_fixture(relations=every_portal_relation()),
                 denied, "header_without_lines")):
            with self.subTest(name):
                base = ot.OntologyTools(ot.StoreReader(store.conn), policy, schema=SCHEMA,
                                        contracts=CONTRACTS, clock=lambda: NOW)
                answer = self.answer(op.ProcessTools(base, GRAPHS),
                                     {"from": root, **P2I})
                qty = {tuple(c["roles"]): c for c in answer.result["quantity_checks"]}[
                    ("ordered", "received")]
                self.assertIsNone(qty["agree"], qty["note"])
                self.assertEqual({g["subject"]: g["reason"]
                                  for g in answer.result["gaps"]}.get("received"), reason)

    def test_a_bc_relation_claim_never_proves_a_bc_hop(self):
        # BC absence stays decided by a family or scoped claim (2026-10-14 decision).
        bc = {b["relation"] for spec in SCHEMA.object_types.values()
              for b in spec.get("bindings", ()) if b["source"] == "bc"}
        store = Store(freshness=[{"source": s, "captured_at": "2099-02-01T11:00:00Z",
                                  "completeness": "partial", "stale": False}
                                 for s in ("portal_api", "bc")])
        ids = purchase_fixture(store)
        store.conn.execute("INSERT INTO store_meta VALUES ('relation_completeness', ?)", (
            json.dumps({"bc": {r: {"completeness": "complete", "stale": False,
                                   "full_traversal_at": "2099-02-01T10:00:00Z"}
                               for r in bc}}),))
        st = stages(self.answer(tools(store), {"from": ids["po_b"]}).result)
        self.assertEqual(st["vendor_invoice"]["missing"]["state"], "unknown")


class RegisterTests(unittest.TestCase):
    def test_register_adds_one_read_only_tool_and_refuses_shadowing(self):
        table = {}
        t = tools(Store())
        op.register(table, t)
        self.assertEqual(list(table), ["plx.process"])
        self.assertTrue(table["plx.process"]["annotations"]["readOnlyHint"])
        with self.assertRaises(ValueError):
            op.register(table, t)



# --------------------------------------------------------------------------- monotonicity

class PermissionMonotonicityTests(unittest.TestCase):
    """Contract (lead decision, review of b26efdb): for any reader, every stage,
    condition, check, current-stage and variants answer equals the full-grant
    reader's answer or is undecided (null / unknown with a gap); never a different
    decided value. Stage and condition event lists may only be subsets of the
    full-grant lists. Checked over fixture worlds with 16 seeded grant subsets
    each, plus every leave-one-out subset and the empty grant.

    Worlds marked *synthetic* use store contents the projector does not emit
    today (a value marking the schema does not declare, a confirmed ship-release
    lineage); they keep the guards honest for data the schema may produce later.
    Removing requirement_unproven, the variant-specific requirements, the identity
    link completeness or the every() proof makes this property fail (mutation runs
    in the lane report). A line whose item link the reader cannot see is ungrouped,
    and an ungrouped line on either side leaves every per-item comparison (sums
    and prices) undecided; removing that refusal fails the property on the
    hidden_item_link world. The item-hop completeness flag is defence in depth
    for the same case (a hidden link to a declared binding is already ungrouped)."""

    FULL = ot.ReadPolicy("plx", {"api:*": frozenset({"x"}), "bc:*": frozenset({"x"})},
                         {m: frozenset({"x"}) for m in store_ddl.MARKINGS})
    READER = Actor("plx", frozenset({"x"}))
    FIN = '["finance"]'

    # ------------------------------------------------------------------ worlds

    @staticmethod
    def worlds():
        def sales(soft_delete=False):
            s = Store()
            ids = sales_fixture(s)
            if soft_delete:
                mo(s, "mo-9", "IN_PROGRESS", ids["line"], deleted="2099-01-05T00:00:00Z")
            mo(s, "mo-8", "COMPLETED", ids["line"])
            return s, [ids["make"], ids["dist"]]

        def purchase():
            s = Store()
            ids = purchase_fixture(s)
            return s, [ids["po_a"], ids["po_b"]]

        def mirrored(bc_received):
            holder = PurchaseTests()
            holder.store = Store()
            holder.ids = purchase_fixture(holder.store)
            po = holder._gold_po_mirrored_in_bc(bc_received=bc_received)
            return holder.store, [po, holder.ids["po_a"]]

        P = PermissionMonotonicityTests
        return {"sales": sales(), "sales_soft_delete": sales(True), "purchase": purchase(),
                "mirrored_90": mirrored("90"), "mirrored_100": mirrored("100"),
                # reviewer worlds (review of 93da7ae), synthetic value markings
                "po_draft_status_marked": P.po_world("DRAFT"),
                "po_ordered_status_marked": P.po_world("ORDERED"),
                "bc_invoice_draft_marked_gl": P.bc_invoice_world("Draft"),
                "bc_invoice_open_marked_gl": P.bc_invoice_world("Open"),
                "make_variant_marked": P.variant_world(False),
                "make_variant_marked_release": P.variant_world(True),
                "make_variant_clear": P.variant_world(False, mode_marked=False),
                # guards: the every() proof, identity links
                "release_via_fm_twin": P.release_twin_world(),
                "identity_bridge": P.identity_bridge_world(),
                "hidden_item_link": P.hidden_item_link_world(),
                "bc_invoice_draft_clear": P.bc_invoice_world("Draft", marked=False),
                # set handles with variants
                "purchase_set": P.set_world("purchase"),
                "sales_set": P.set_world("sales"),
                # manufacturing: next stage and overdue (a quality-controlled stage,
                # disagreeing due dates across the FM order and its Portal twin)
                "ao_open": P.ao_world(status="Committed", tasks=("Setup",)),
                "ao_due_disagree": P.ao_world(portal_required="2099-01-25"),
                "ao_shipped": P.ao_world(shipped="SHIPPED"),
                # cross-process prerequisites: an open, a received and a cancelled PO
                "material": P.material_world(),
                # invoices naming a gold PO's number in one company, and in two
                "named_one_company": P.named_world(("company_a",)),
                "named_two_companies": P.named_world(("company_a", "company_b"))}

    @staticmethod
    def named_world(companies):
        s = Store()
        return s, [named_po_fixture(s, companies)]

    @classmethod
    def po_world(cls, status):
        """A gold PO whose status the restricted reader may not read (synthetic
        finance marking) with a received receipt: receipt requires purchase_order."""
        s = Store()
        item = s.obj("Product:portal.app_product:p-1")
        po = s.obj("PurchaseOrder:portal.app_purchase_order:po-x")
        s.val(po, "status", "text", status, markings=cls.FIN)
        pol = s.obj("PurchaseOrderLine:portal.app_purchase_order_line:po-x-1")
        s.val(pol, "ordered_quantity", "quantity", "10", unit="each", role="ordered")
        s.link("PurchaseOrderLine.line_of", pol, po)
        s.link("PurchaseOrderLine.for_item", pol, item)
        rec = s.obj("Receipt:portal.app_receipt:rc-x")
        s.link("Receipt.receives", rec, po)
        rl = s.obj("ReceiptLine:portal.app_receipt_line:rc-x-1")
        s.val(rl, "received_quantity", "quantity", "10", unit="each", role="received")
        s.link("ReceiptLine.line_of", rl, rec)
        s.link("ReceiptLine.for_item", rl, item)
        return s, [po]

    @staticmethod
    def hidden_item_link_world():
        """Reviewer world (review of da3cbf0), synthetic link marking: PO-A's invoice
        gets a second line for the same item at another price whose item link the
        reader may not see (l5). A full-grant reader sees the price disagree; a reader
        without l5 must not see it agree."""
        s = Store()
        ids = purchase_fixture(s)
        s.conn.execute("UPDATE object_values SET value = '2.50' WHERE object_id = ? AND "
                       "property = 'unit_cost'", (ids["pi_a_line"],))
        pil = s.obj("PurchaseInvoiceLine:bc.purchase_invoice_lines:pi-a-2")
        s.val(pil, "invoiced_quantity", "quantity", "0", unit="each", role="invoiced")
        s.val(pil, "unit_cost", "money", "2.60", unit="each", currency="USD",
              markings='["finance"]')
        s.link("PurchaseInvoiceLine.line_of", pil, ids["pi_a"])
        s.link("PurchaseInvoiceLine.for_item", pil, ids["item"])
        s.conn.execute("UPDATE links SET markings = '[\"l5\"]' WHERE link_type = "
                       "'PurchaseInvoiceLine.for_item' AND from_object = ?", (pil,))
        return s, [ids["po_a"]]

    @classmethod
    def bc_invoice_world(cls, status, marked=True):
        """PO-A with its invoice status marked (synthetic) and a G/L entry: gl_posting
        requires vendor_invoice."""
        s = Store()
        ids = purchase_fixture(s)
        s.conn.execute("UPDATE object_values SET value = ?, markings = ? WHERE object_id = ? "
                       "AND property = 'status'", (status, cls.FIN if marked else "[]",
                                                   ids["pi_a"]))
        gl = s.obj("GeneralLedgerEntry:bc.general_ledger_entries:gl-1")
        s.link("GeneralLedgerEntry.for_purchase_invoice", gl, ids["pi_a"], tier="corroborated",
               kind="same_record")
        return s, [ids["po_a"]]

    @classmethod
    def variant_world(cls, release, mode_marked=True):
        """A MAKE order whose fulfillment_mode the reader may not read (synthetic
        marking), with or without a release."""
        s = Store()
        so = s.obj("SalesOrder:portal.app_sales_order:so-v", "gold_etl_from_fm")
        s.val(so, "status", "text", "CONFIRMED")
        s.val(so, "fulfillment_mode", "enum", "MAKE", markings=cls.FIN if mode_marked else "[]")
        line = s.obj("SalesOrderLine:portal.app_sales_order_line:so-v-l1", "gold_etl_from_fm")
        s.link("SalesOrderLine.line_of", line, so)
        mo(s, "mo-v", "COMPLETED", line)
        if release:
            r = s.obj("ShipRelease:portal.app_ship_release:sr-v", "gold_native_staging")
            s.val(r, "status", "enum", "FULFILLED")
            s.link("ShipRelease.for_sales_order", r, so)
        shipment(s, "sh-v", "SHIPPED", so)
        return s, [so]

    @staticmethod
    def release_twin_world():
        """Synthetic confirmed releases: one FULFILLED on the gold order, one DRAFT on
        its FM twin. A reader without the FM order unit sees only the done one; the
        every() rule must not call the stage reached."""
        s = Store()
        so = s.obj("SalesOrder:portal.app_sales_order:so-r", "gold_etl_from_fm")
        s.val(so, "status", "text", "CONFIRMED")
        s.val(so, "fulfillment_mode", "enum", "MAKE")
        fm = s.obj("SalesOrder:portal.fm_order_entry:77", "fm_bronze")
        s.link("SalesOrder.same_record_as", so, fm, kind="same_record")
        for key, status, order in (("sr-a", "FULFILLED", so), ("sr-b", "DRAFT", fm)):
            r = s.obj(f"ShipRelease:portal.app_ship_release:{key}", "gold_etl_from_fm")
            s.val(r, "status", "enum", status)
            s.link("ShipRelease.for_sales_order", r, order)
        return s, [so]

    @staticmethod
    def identity_bridge_world():
        """Two gold products that share one Sage item (same erp_code): the ordered
        line names one, the receipt line the other. Only the Sage item joins them, so
        a reader without the Sage unit must not compare them apart."""
        s = Store()
        p1 = s.obj("Product:portal.app_product:p-a")
        p2 = s.obj("Product:portal.app_product:p-b")
        sage = s.obj("Product:portal.sage_ic_item:it-s", "sage_bronze")
        s.link("Product.same_record_as", p1, sage, kind="same_record")
        s.link("Product.same_record_as", p2, sage, kind="same_record")
        po = s.obj("PurchaseOrder:portal.app_purchase_order:po-i")
        s.val(po, "status", "text", "ORDERED")
        pol = s.obj("PurchaseOrderLine:portal.app_purchase_order_line:po-i-1")
        s.val(pol, "ordered_quantity", "quantity", "10", unit="each", role="ordered")
        s.link("PurchaseOrderLine.line_of", pol, po)
        s.link("PurchaseOrderLine.for_item", pol, p1)
        rec = s.obj("Receipt:portal.app_receipt:rc-i")
        s.link("Receipt.receives", rec, po, tier="stored_by_text_join")
        rl = s.obj("ReceiptLine:portal.app_receipt_line:rc-i-1")
        s.val(rl, "received_quantity", "quantity", "10", unit="each", role="received")
        s.link("ReceiptLine.line_of", rl, rec)
        s.link("ReceiptLine.for_item", rl, p2)
        return s, [po]

    @staticmethod
    def material_world():
        s = Store()
        ids = material_fixture(s, pos=("open", "received", "cancelled"))
        return s, [ids["so"], ids["ao"]]

    @staticmethod
    def ao_world(**world):
        s = Store()
        return s, [ao_fixture(s, **world)["ao"]]

    @staticmethod
    def set_world(kind):
        s = Store()
        if kind == "purchase":
            ids = purchase_fixture(s)
            return s, [(ids["po_a"], ids["po_b"])]
        ids = sales_fixture(s)
        return s, [(ids["make"], ids["dist"])]

    # ------------------------------------------------------------------ harness

    @staticmethod
    def answers(result, roots):
        out = {}
        for item in result.get("stages", ()):
            out[("stage", item["stage"])] = _decided(item)
            out[("events", item["stage"])] = ("EV", frozenset(item["events"]))
            if "prerequisites" in item:
                out[("prerequisites", item["stage"])] = (
                    _UNDECIDED if item["prerequisites"] == "undecided" else item["prerequisites"])
            for b in item.get("blockers", ()):  # under a decided summary (else: may not block)
                if item["prerequisites"] != "undecided" \
                        and b["missing"]["state"] == "absent_in_source":
                    out[("blocker", item["stage"], b["process"], b["stage"], b["object"],
                         b.get("via"))] = b["missing"]["reason"]
        for item in result.get("conditions", ()):
            out[("condition", item["condition"])] = _decided(item)
            out[("events_c", item["condition"])] = ("EV", frozenset(item["events"]))
        out[("current_stage",)] = result.get("current_stage", _UNDECIDED)
        out[("next_stage",)] = result.get("next_stage", _UNDECIDED)
        for item in result.get("due", ()):
            out[("overdue", item["stage"])] = (_UNDECIDED if item["overdue"] is None
                                               else (item["overdue"], item.get("days_overdue")))
        out[("variants",)] = (json.dumps(result["variants"], sort_keys=True)
                              if "variants" in result else _UNDECIDED)
        for root in roots:
            for check in root["checks"]:
                key = ("check", root["root"], check["check"], tuple(check["item"]["roles"]),
                       check["group"])
                agree = check["item"]["agree"]
                out[key] = _UNDECIDED if agree is None else agree
        return out

    def run_tool(self, store, policy, root, graphs=None, process=None):
        base = ot.OntologyTools(ot.StoreReader(store.conn), policy, schema=SCHEMA,
                                contracts=CONTRACTS, clock=lambda: NOW)
        arguments = {"from": root}
        if isinstance(root, tuple):
            arguments = {"from": base._new_set(self.READER, list(root)), "include_variants": True}
        if process is not None:
            arguments["process"] = process
        answer = op.ProcessTools(base, graphs or GRAPHS).process(arguments, self.READER)
        self.assertEqual(validate(answer.result, CONTRACTS["tools"]["plx.process"]["output"],
                                  root=CONTRACTS), [])
        return self.answers(answer.result, answer.roots), answer

    def assert_monotone(self, worlds, graphs=None, seed=20261002, process=None):
        import random
        rng = random.Random(seed)
        checked = decided = 0
        for name, (store, roots) in worlds.items():
            units = sorted(r[0] for r in store.conn.execute(
                "SELECT DISTINCT audience_key FROM objects")) + ["marking:finance", "marking:l5",
                                                          "marking:quality_controlled"]
            # (granted units, style): "exact" grants only the listed units (a family
            # wildcard when all of it is listed); "family" grants each family's
            # wildcard and denies the unlisted units by exact entries, so units the
            # store does not hold stay granted (the policy shape of a broad role).
            subsets = [(frozenset(u for u in units if rng.random() < 0.5),
                        rng.choice(("exact", "family"))) for _ in range(16)]
            subsets += [(frozenset(units) - {u}, style) for u in units
                        for style in ("exact", "family")]
            subsets += [(frozenset(), "exact")]
            for root in roots:
                full, full_answer = self.run_tool(store, self.FULL, root, graphs, process)
                self.assertNotIn("error", full_answer.result, (name, root))
                for granted, style in subsets:
                    audiences = {u: frozenset({"x"}) for u in granted if ":" in u
                                 and not u.startswith("marking:")}
                    for family in ("api", "bc"):
                        members = [u for u in units if u.startswith(family + ":")]
                        if style == "family":
                            audiences[family + ":*"] = frozenset({"x"})
                            audiences.update({u: frozenset() for u in members
                                              if u not in granted})
                        elif members and all(u in granted for u in members):
                            audiences[family + ":*"] = frozenset({"x"})
                    marks = {m.split(":", 1)[1]: frozenset({"x"}) for m in granted
                             if m.startswith("marking:")}
                    got, answer = self.run_tool(store, ot.ReadPolicy("plx", audiences, marks),
                                                root, graphs, process)
                    if "error" in answer.result:
                        # ambiguous_process: unnamed, whether a PO is open is withheld
                        # from this reader (it never gets another graph than the full one)
                        self.assertIn(answer.result["error"]["code"],
                                      ("object_not_found", "empty_set")
                                      + (("ambiguous_process",) if process is None else ()))
                        continue
                    for key, value in got.items():
                        checked += 1
                        if value is _UNDECIDED:
                            continue
                        where = (name, root, style, sorted(granted), key)
                        if isinstance(value, tuple) and value and value[0] == "EV":
                            self.assertLessEqual(value[1], full.get(key, ("EV", frozenset()))[1],
                                                 where)
                            continue
                        decided += 1
                        self.assertEqual(full.get(key, _UNDECIDED), value, where)
        return checked, decided

    def test_every_answer_is_the_full_grant_answer_or_undecided(self):
        checked, decided = self.assert_monotone(self.worlds())
        self.assertGreater(checked, 5000)
        self.assertGreater(decided, 1000)

    def test_holds_for_a_graph_with_a_variant_specific_requirement_on_a_reachable_stage(self):
        # Review of 93da7ae: with the variant unreadable, a variant-only requirement
        # must make a reached stage undecided, never be dropped.
        graphs = copy.deepcopy(GRAPHS)
        stage = next(s for s in graphs["sales_order_lifecycle"]["stages"]
                     if s["id"] == "shipment")
        stage["requires"] = {"make": ["condition:release_clears_pick"]}
        self.assertEqual(op.check_graph(graphs["sales_order_lifecycle"], SCHEMA), [])
        worlds = {"make_variant_marked": self.variant_world(False),
                  "make_variant_clear": self.variant_world(False, mode_marked=False)}
        checked, decided = self.assert_monotone(worlds, graphs)
        self.assertGreater(decided, 50)
        # and the full-grant reader does see the order out of order
        store, (root,) = worlds["make_variant_clear"]
        full, _ = self.run_tool(store, self.FULL, root, graphs)
        self.assertEqual(full[("stage", "shipment")][0], "out_of_order")
        # while a reader without the variant gets it undecided
        no_finance = ot.ReadPolicy("plx", {"api:*": frozenset({"x"}), "bc:*": frozenset({"x"})})
        store, (root,) = worlds["make_variant_marked"]
        got, answer = self.run_tool(store, no_finance, root, graphs)
        self.assertIs(got[("stage", "shipment")], _UNDECIDED)
        self.assertEqual(stages(answer.result)["shipment"]["missing"]["reason"],
                         "requirement_unproven")


    def test_holds_when_portal_relations_are_complete_and_the_family_partial(self):
        store, root = portal_receipt_fixture(relations=every_portal_relation())
        full, _ = self.run_tool(store, self.FULL, root, process="purchase_to_invoice")
        self.assertIn(False, [v for k, v in full.items() if k[0] == "check"])
        checked, decided = self.assert_monotone({"portal_relations_complete": (store, [root])},
                                                process="purchase_to_invoice")
        self.assertGreater(decided, 20)


_UNDECIDED = object()


def _decided(item):
    """A stage/condition answer, or _UNDECIDED (missing with an unknown-kind gap)."""
    if item["state"] == "missing" and item["missing"]["state"] != "absent_in_source":
        return _UNDECIDED
    missing = item.get("missing") or {}
    return (item["state"], missing.get("state"), missing.get("reason"))


if __name__ == "__main__":
    unittest.main()
