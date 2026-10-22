"""A synthetic source world for keyless development of the oracle and the dev-set generator.

Every row is invented (identifiers start with ``FX``; companies and parties are named
``FX ...``; purchase-order numbers take the Portal's own forms in a ``9`` range clear of the
live series: ``PO9200001``, legacy FM ``PO#90001``, operations route ``PO-9FX003``; receipt
lab ids, assembly orders and shipments take theirs on days of 2098: ``98106-1``,
``AO-98038A``, ``SH900038``; Sage-loaded orders carry the Sage ETL's stamp); nothing
here is PLX data or a real case. The world exercises the traps the
pack names: mixed units on one order, an assembly order without ``yield_actual``, FM
status entries whose summed ``qtymanuf`` differs from ``yield_actual`` (larger worlds), a
cancelled shipment, an order with no shipment rows, a person's name in ``po_reference``,
one customer PO carried by two orders, a short receipt, invoices found only by vendor
invoice No., a PO with no invoice, another vendor's invoice reusing a vendor invoice No. in
a second BC company, one PO invoiced in two companies, Portal-native staging rows (Generate
PO rows carry an FM number and id but no FM source stamp), a QA smoke row whose source the
silver ETL overwrote, a demo replica, a row renumbered to a BC No. no PO grammar reads and open
transactions.

It serves three injected transports, so callers exercise their real read path:

* ``fetch(url, headers, max_bytes=...)``: the Portal Workbench reporting API routes
  (``/v1/health``, ``/v1/relations``, ``/v1/relations/{name}/rows`` with ``limit``,
  ``columns`` and an opaque ``cursor``) for ``WorkbenchReadApiClient(fetch=...)``;
* ``read_relation(name, columns)``: the same rows in the client's read-result shape
  (for oracle unit tests that must not import the integrations package);
* ``bc_port`` / ``documents``: in-memory BC documents and mail/SharePoint texts.

Evidence label of anything computed from it: ``injected-transport`` fixture, never live.
"""

from __future__ import annotations

from decimal import Decimal
import json
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

GENERATED_AT = "2099-01-02T03:04:05Z"
UPDATED_AT = "2099-01-01T12:00:00Z"
COMPANIES = (("11111111-1111-1111-1111-111111111111", "FX Company One"),
             ("22222222-2222-2222-2222-222222222222", "FX Distribution"))
KEYS = {"sage.oe_order_header": ["orduniq"], "sage.po_header": ["porhseq"]}
SINCE = {"public.order_entry_item_assembly": None}
# Columns the served catalog has even where this world holds no row or no value (the
# replica serves them): the client only reads catalog
# columns, so an undeclared one would fail the whole relation.
DECLARED_COLUMNS = {
    "public.assembly_order_status": ("audittimestampmodified", "id", "idassembly", "qtymanuf"),
    "sage.oe_order_header": ("complete", "customer"),
    "public.order_entry": ("id", "custpocode", "idcust", "audittimestampmodified"),
    "public.order_entry_item": ("id", "idoe", "audittimestampmodified"),
    "public.order_entry_item_assembly": ("id", "idassembly", "idoeitem"),
    "public.app_receipt": ("id", "receipt_number", "purchase_order_id", "status",
                           "vendor_invoice_number", "updated_at"),
    "public.app_receipt_line": ("id", "receipt_id", "product_id", "quantity", "uom",
                                "updated_at"),
    "public.receipt_raw_material": ("id", "idlabid", "idpogenitem", "status_c",
                                    "audittimestampmodified"),
    "public.generated_purchase_order_item": ("id", "idpogen", "audittimestampmodified"),
}


def _q(value: int | float | Decimal) -> str:
    """Quantities as the API sends them: float-formatted strings."""
    return format(float(value), ".1f")


class World:
    def __init__(self, relations: Mapping[str, list[dict[str, Any]]],
                 bc: Mapping[str, list[dict[str, Any]]] | None = None,
                 bc_lines: Mapping[str, list[dict[str, Any]]] | None = None,
                 documents: Mapping[str, list[dict[str, Any]]] | None = None,
                 generated_at: str = GENERATED_AT) -> None:
        self.relations = {k: [dict(r) for r in v] for k, v in relations.items()}
        self.bc = {k: [dict(r) for r in v] for k, v in (bc or {}).items()}
        self.bc_lines = {k: [dict(r) for r in v] for k, v in (bc_lines or {}).items()}
        self.docs = {k: [dict(r) for r in v] for k, v in (documents or {}).items()}
        self.generated_at = generated_at
        self.requests: list[str] = []
        self.fail: set[str] = set()

    # ------------------------------------------------------------- Portal API

    def _columns(self, name: str) -> list[str]:
        columns: dict[str, None] = dict.fromkeys(DECLARED_COLUMNS.get(name, ()))
        for row in self.relations.get(name, []):
            columns.update(dict.fromkeys(row))
        return sorted(columns) or ["id"]

    def _key(self, name: str) -> list[str]:
        return KEYS.get(name, ["id"])

    def catalog(self) -> list[dict[str, Any]]:
        out = []
        for name in sorted(self.relations):
            schema, relation = name.split(".")
            columns = sorted(set(self._columns(name)) | set(self._key(name)))
            since = SINCE.get(name, "updated_at" if "updated_at" in columns else
                              ("audittimestampmodified" if "audittimestampmodified" in columns
                               else ("audtdate" if "audtdate" in columns else None)))
            out.append({"name": name, "schema": schema, "relation": relation,
                        "columns": columns, "key": self._key(name), "since": since})
        return out

    def fetch(self, url: str, headers: Mapping[str, str], *, max_bytes: int = 0) -> Any:
        """The reporting API's three GET routes over this world (no network)."""
        parts = urlsplit(url)
        path = parts.path
        self.requests.append(path)
        if path == "/v1/health":
            return {"ok": True, "recovery": True, "replayLagSeconds": 1, "unavailable": [],
                    "missingColumns": [], "generatedAt": self.generated_at,
                    "keysExpiringSoon": []}
        if path == "/v1/relations":
            return {"relations": self.catalog()}
        prefix, suffix = "/v1/relations/", "/rows"
        if path.startswith(prefix) and path.endswith(suffix):
            name = unquote(path[len(prefix):-len(suffix)])
            if name in self.fail:
                raise RuntimeError("fixture relation failure")
            query = parse_qs(parts.query)
            limit = int(query.get("limit", ["1000"])[0])
            requested = query.get("columns", [""])[0].split(",")
            columns = sorted(set(c for c in requested if c) | set(self._key(name)))
            offset = int(query.get("cursor", ["o:0"])[0].split(":")[1])
            rows = sorted(self.relations.get(name, []),
                          key=lambda r: json.dumps([r.get(k) for k in self._key(name)]))
            page = rows[offset:offset + limit]
            more = offset + limit < len(rows)
            return {"relation": name, "columns": columns,
                    "rows": [{c: row.get(c) for c in columns} for row in page],
                    "nextCursor": f"o:{offset + limit}" if more else None}
        raise RuntimeError("fixture route not found")

    def read_relation(self, name: str, columns: Sequence[str]) -> dict[str, Any]:
        """The client's read-result shape, without the client (oracle unit tests)."""
        if name in self.fail:
            raise RuntimeError("fixture relation failure")
        key = self._key(name)
        wanted = sorted(set(columns) | set(key))
        rows = [{c: row.get(c) for c in wanted} for row in self.relations.get(name, [])]
        return {"relation": name, "columns": wanted, "key": key, "rows": rows,
                "row_count": len(rows), "catalog_revision": "fixture-" + name,
                "health_before": {"generated_at": self.generated_at,
                                  "replay_lag_seconds": 1}}

    # ------------------------------------------------------------- BC + documents

    def bc_port(self) -> "FixtureBc":
        return FixtureBc(self)

    def documents(self) -> "FixtureDocuments":
        return FixtureDocuments(self)


class FixtureBc:
    """BC API v2.0 semantics the oracle relies on: equality filters compare Code fields
    ignoring case (the oracle must re-check exact text), lines by document."""

    def __init__(self, world: World) -> None:
        self._world = world

    def find(self, entity_set: str, field: str, value: str) -> dict[str, Any]:
        if entity_set in self._world.fail:
            raise RuntimeError("fixture BC failure")
        records = [r for r in self._world.bc.get(entity_set, [])
                   if str(r["fields"].get(field, "")).casefold() == value.casefold()]
        return {"records": records, "complete": True}

    def lines(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return {"records": self._world.bc_lines.get(str(record["id"]), []), "complete": True}

    def sample(self, entity_set: str, limit: int) -> dict[str, Any]:
        return {"records": self._world.bc.get(entity_set, [])[:limit], "complete": True,
                "source": [f"bc:{entity_set}"], "companies": len(COMPANIES)}


class FixtureDocuments:
    # An in-memory search reads no source, so a fixture run searches every root (the
    # frozen dev and held-out fixture sets are drawn from all of them); a live port keeps
    # the generator's cap.
    max_roots = 1_000_000

    def __init__(self, world: World) -> None:
        self._world = world

    def sample(self, kind: str, limit: int, *, windows: Sequence[str] = (),
               seed: str = "") -> dict[str, Any]:
        """Every fixture document up to ``limit``; the fixture mailbox is not time-ordered,
        so ``windows`` and ``seed`` change nothing."""
        return {"records": self._world.docs.get(kind, [])[:limit], "complete": True,
                "source": [f"fixture:{kind}"]}

    def search(self, kind: str, queries: Sequence[str], *, hits: int,
               seed: str = "") -> dict[str, Any]:
        """Each query's first ``hits`` fixture documents whose texts contain it, case
        folded (wider than a search engine's terms, never narrower); every document is
        visible."""
        records, results = {}, []
        for query in queries:
            found = [r for r in self._world.docs.get(kind, [])
                     if any(query.casefold() in str(t).casefold()
                            for t in [*r.get("texts", []), *r.get("contents", [])])]
            results.append({"query": query, "handles": [r["handle"] for r in found[:hits]],
                            "complete": len(found) <= hits, "withheld": 0})
            records.update((r["handle"], r) for r in found[:hits])
        return {"results": results, "records": list(records.values()),
                "source": [f"fixture:{kind}"]}


def bc_record(entity_set: str, company: int, serial: int, **fields: Any) -> dict[str, Any]:
    company_id, company_name = COMPANIES[company]
    record_id = f"cccccccc-0000-0000-{company:04d}-{serial:012d}"
    return {"company_id": company_id, "company_name": company_name, "entity_set": entity_set,
            "id": record_id, "fields": {"id": record_id, **fields},
            "last_modified": UPDATED_AT,
            "citation": f"bc://production/{company_id}/{entity_set}/{record_id}@W%2F%22{serial}%22"}


# The evaluation fixture world: large enough that both splits of the root partition
# (evals/oracle/generator.split_of) fill their quotas (dev 30, held-out 120) from one
# world, as the live sets do from one replica.
EVAL_SALES = 120
EVAL_PURCHASES = 108


def build_eval_world() -> World:
    """The world the frozen dev items and the held-out fixture manifest are drawn from."""
    return build_world(sales=EVAL_SALES, purchases=EVAL_PURCHASES)


SMALL_PURCHASES = 16
LARGE_SUPPLIERS = 9


def _supplier(i: int) -> int:
    """Supplier number of purchase order ``i``: three suppliers in the original small
    world; orders past it spread over nine more, so a supplier (a terminal chain record)
    is not shared by every purchase order of a larger world."""
    return 1 + i % 3 if i <= SMALL_PURCHASES else 4 + i % LARGE_SUPPLIERS


def _serial(i: int, small: int, wide: int) -> int:
    """BC record serial of order ``i``: the original small layout keeps its serials;
    orders from 100 on use a wide range so no two records share an id."""
    return small if i < 100 else wide


def po_number(i: int) -> str:
    """Gold number of purchase order ``i`` in the Portal's canonical series (formatPoNumber
    ``PO{n}``, P@6e76a5b:src/lib/mrp/po-number-series.ts:52); ``PO9...`` stays clear of
    the live series, which starts at PO100000."""
    return f"PO9{200000 + i}"


def fm_po_number(i: int) -> str:
    """FM number of purchase order ``i`` in the legacy FM form ``PO#{n}``."""
    return f"PO#9{i:04d}"


def receipt_lab_id(i: int) -> str:
    """Lab id of receipt ``i``: the raw form ``YYDDD-N`` (formatRawLabId,
    P@56d707dd5:src/lib/mrp/receipt-lab-id.ts), on day ``i`` of the fixture's year 2098."""
    return f"98{i:03d}-1"


def ao_code(i: int, letter: str) -> str:
    """FM assembly-order name ``AO-{YY}{day of year}{letter}``, the day padded as FM writes it
    (identifiers.json ao_code form fm_padded), on day ``i`` of 2098."""
    return f"AO-98{i:03d}{letter}"


def shipment_code(i: int) -> str:
    """Portal shipment code ``SH{n}`` (create-shipment.ts, from SH100000) in a ``9`` range
    clear of the live series."""
    return f"SH9{i:05d}"


def _po_custom(i: int) -> dict[str, Any]:
    """custom_fields of purchase order ``i`` as upstream writes them (P@6e76a5b): FM rows
    loaded by etl-silver-to-gold.ts:776-781 or mirror-fm-pos-to-gold.mjs:296-300; every
    fourth order is Portal-native, half from Generate PO (create-fm-purchase-order.ts:
    311-313, which also writes the FM number and id), half from the operations route
    (purchase-orders/route.ts:33,55-66, no custom_fields)."""
    if i % 4 != 3:
        return {"fmGeneratedPoId": 6000 + i, "fmPoNumber": fm_po_number(i),
                "source": "etl-silver-to-gold" if i % 2 else "mirror-fm-pos-to-gold"}
    if i % 8 == 7:
        return {"fmGeneratedPoId": 6000 + i, "fmPoNumber": fm_po_number(i),
                "source": "portal-generate-po"}
    return {}


def _staging_purchases(rel: dict[str, list[dict[str, Any]]]) -> None:
    """Three closed, fully received Portal-native rows only lineage keeps out of a sample
    (P@6e76a5b): a QA smoke row whose source the silver ETL merge overwrote
    (qa-po-receive-smoke.ts:111-116, 147-156; etl-silver-to-gold.ts:788-830), a demo
    replica (demo-july-p2p-replicas.ts:273-298, demoTag, no source) and a row renumbered to
    a BC document No. outside the PO series, its old number kept in fmPoNumber
    (align-portal-po-number.ts:48-61); no PO grammar reads that No."""
    qa, demo, bc = "PO#99901", "PO9299901", "FXBC9001"
    for po_id, number, status, custom in (
            ("fx-po-qa-1", qa, "SUBMITTED",
             {"qaSmoke": True, "qaTag": "fx-qa", "fmGeneratedPoId": 6990, "fmPoNumber": qa,
              "source": "etl-silver-to-gold"}),
            ("fx-po-demo-1", demo, "OPEN",
             {"demoTag": "fx-demo", "demoTemplatePoNumber": po_number(1)}),
            ("fx-po-bc-1", bc, "SUBMITTED",
             {"fmPoNumber": "PO-9FXB01", "renumberedTo": bc,
              "renumberSource": "bc-drain-align"})):
        rel["public.app_purchase_order"].append({
            "id": po_id, "po_number": number, "status": status, "supplier_id": "fx-supp-1",
            "closed_at": None, "custom_fields": custom, "updated_at": UPDATED_AT})
        rel["public.app_purchase_order_line"].append({
            "id": f"{po_id}-l1", "purchase_order_id": po_id, "product_id": "fx-prod-1",
            "quantity": _q(10), "quantity_received": _q(10), "unit_price": "1", "uom": "ea",
            "updated_at": UPDATED_AT})
    rel["public.generated_purchase_order"].append({"id": 6990, "ponumber": qa,
                                                   "audittimestampmodified": UPDATED_AT})


def build_world(sales: int = 24, purchases: int = 16) -> World:
    """The fixture world (deterministic; see module docstring for its traps). The default
    size is the original small world; ``build_eval_world`` repeats the same patterns over
    more orders."""
    rel: dict[str, list[dict[str, Any]]] = {k: [] for k in (
        "public.app_sales_order", "public.app_sales_order_line", "public.app_customer",
        "public.app_product", "public.app_manufacturing_order", "public.app_shipment",
        "public.app_shipment_line", "public.order_entry", "public.assembly_order",
        "public.order_entry_item", "public.order_entry_item_assembly", "sage.oe_order_header",
        "public.app_purchase_order", "public.app_purchase_order_line", "public.app_supplier",
        "public.app_receipt", "public.app_receipt_line", "public.generated_purchase_order",
        "public.assembly_order_status",
        # Served FM receipt tables; this world keeps its receipts in app_receipt.
        "public.generated_purchase_order_item", "public.receipt_raw_material")}
    bc: dict[str, list[dict[str, Any]]] = {"salesInvoices": [], "purchaseInvoices": [],
                                           "salesOrders": [], "purchaseOrders": [],
                                           "purchaseReceipts": []}
    bc_lines: dict[str, list[dict[str, Any]]] = {}
    docs: dict[str, list[dict[str, Any]]] = {"mail": [], "sharepoint": []}
    for c in range(1, 6):
        rel["public.app_customer"].append({"id": f"fm-cust-{c}", "code": f"FXC{c:03d}",
                                           "updated_at": UPDATED_AT})
    # The object_view unparsed-denominator case (tests/test_object_workspace_http.py
    # ObjectViewLiveCheckTest): the world's one declared malformed upstream identifier, an
    # erp_code keyed with an ampersand, outside the customer_erp_code grammar. A customer
    # is never a question root (keys.ROOT_KEYS); every question-root identifier parses as
    # its kind (evals/tests/test_dev_baseline_roots.py FixtureIdentifierFormTests). The
    # renumbered FXBC9001 below is a well-formed BC No. the PO grammar declines, not
    # malformed.
    rel["public.app_customer"][2]["erp_code"] = "FX B&C"
    for p in range(1, 21):
        rel["public.app_product"].append({"id": f"fx-prod-{p}", "code": f"FX-ITEM-{p:03d}",
                                          "updated_at": UPDATED_AT})
    for s in range(1, 4 + (LARGE_SUPPLIERS if purchases > SMALL_PURCHASES else 0)):
        rel["public.app_supplier"].append({"id": f"fx-supp-{s}", "code": f"FXV{s:03d}",
                                           "updated_at": UPDATED_AT})

    # ---------------------------------------------------------- sales side
    for i in range(1, sales + 1):
        so_id = f"fx-so-{i}"
        number = f"FXSO{100000 + i}"
        status = ("CONFIRMED" if i % 4 == 3 else ("COMPLETE" if i % 4 == 1 else "SHIPPED"))
        if i % 3 == 0:
            custom: dict[str, Any] = {"fmId": 5000 + i}
        elif i % 3 == 1:
            # The Sage ETL stamps the raw orduniq in sageSourceOrdUniq beside the key
            # (P@56d707dd5 scripts/etl-sage-distribution-gold.mjs:1500-1502).
            custom = {"sageOrdUniq": str(7000 + i), "sageSourceOrdUniq": str(7000 + i),
                      "source": "sage.oe_order_header"}
        else:
            custom = {"fmId": 900000 + i} if i % 2 == 0 else {}
        po_ref = "FX Person" if i % 5 == 0 else f"FXCPO-{i:03d}"
        if i in (4, 13):
            po_ref = "FXCPO-SHARED-1"
        rel["public.app_sales_order"].append({
            "id": so_id, "order_number": number, "po_reference": po_ref, "status": status,
            "customer_id": f"fm-cust-{1 + i % 5}", "custom_fields": custom,
            "required_date": f"2098-12-{1 + i % 28:02d}", "updated_at": UPDATED_AT})
        if "fmId" in custom:
            # Each system names the customer in its own column (the customer PO's scope).
            rel["public.order_entry"].append({"id": custom["fmId"], "custpocode": po_ref,
                                              "idcust": 1 + i % 5,
                                              "audittimestampmodified": UPDATED_AT})
        if "sageOrdUniq" in custom:
            rel["sage.oe_order_header"].append({"orduniq": custom["sageOrdUniq"],
                                                "ordnumber": f"FXS{i:05d}",
                                                "ponumber": po_ref,
                                                "customer": f"FXC{1 + i % 5:03d}",
                                                "audtdate": "20981231"})
        line_specs = [(1, 100 * i, "ea")]
        if i % 7 == 0:
            line_specs.append((2, 10, "cse"))
        shipped_total = 0
        for n, qty, uom in line_specs:
            line_id = f"fx-sol-{i}-{n}"
            rel["public.app_sales_order_line"].append({
                "id": line_id, "sales_order_id": so_id, "product_id": f"fx-prod-{1 + i % 20}",
                "quantity": _q(qty), "uom": uom, "updated_at": UPDATED_AT})
            if n == 1 and i % 8 != 5:
                code = ao_code(i, "A")
                yield_actual = None if i % 6 == 0 else _q(qty - i % 3)
                rel["public.app_manufacturing_order"].append({
                    "id": f"fx-mo-{i}", "mo_code": code, "sales_order_line_id": line_id,
                    "status": "Realized" if status != "CONFIRMED" else "Committed",
                    "yield_actual": yield_actual, "uom": uom, "date_realized": None,
                    "deleted_at": None, "updated_at": UPDATED_AT})
                rel["public.assembly_order"].append({"id": 8000 + i, "nameamo_c": code,
                                                     "audittimestampmodified": UPDATED_AT})
                if i > 24 and i % 4 == 2:
                    # Larger worlds: FM status entries, whose qtymanuf sum is the produced
                    # quantity before yield_actual; one entry carries no quantity.
                    for n, made in enumerate((qty // 2, None, qty - qty // 2 - 1), start=1):
                        rel["public.assembly_order_status"].append({
                            "id": 70000 + 10 * i + n, "idassembly": 8000 + i,
                            "qtymanuf": None if made is None else _q(made),
                            "audittimestampmodified": UPDATED_AT})
                if "fmId" in custom:
                    rel["public.order_entry_item"].append({
                        "id": 9000 + i, "idoe": custom["fmId"],
                        "audittimestampmodified": UPDATED_AT})
                    rel["public.order_entry_item_assembly"].append({
                        "id": 9500 + i, "idassembly": 8000 + i, "idoeitem": 9000 + i})
            shipped_total += qty
        if status != "CONFIRMED" and i % 8 != 0:
            ship_id = f"fx-shp-{i}"
            rel["public.app_shipment"].append({
                "id": ship_id, "sales_order_id": so_id, "shipment_code": shipment_code(i),
                "status": "SHIPPED", "ship_date": "2098-12-20", "updated_at": UPDATED_AT})
            for n, qty, uom in line_specs:
                rel["public.app_shipment_line"].append({
                    "id": f"fx-shl-{i}-{n}", "shipment_id": ship_id,
                    "product_id": f"fx-prod-{1 + i % 20}",
                    "quantity": _q(qty - (i % 2 if n == 1 else 0)), "uom": uom,
                    "updated_at": UPDATED_AT})
            if i % 5 == 1:
                rel["public.app_shipment"].append({
                    "id": f"fx-shp-{i}-x", "sales_order_id": so_id,
                    "shipment_code": shipment_code(50_000 + i), "status": "CANCELLED",
                    "ship_date": None, "updated_at": UPDATED_AT})
                rel["public.app_shipment_line"].append({
                    "id": f"fx-shl-{i}-x", "shipment_id": f"fx-shp-{i}-x",
                    "product_id": f"fx-prod-{1 + i % 20}", "quantity": _q(999), "uom": "ea",
                    "updated_at": UPDATED_AT})
            bc["salesInvoices"].append(bc_record(
                "salesInvoices", i % 2, _serial(i, 300 + i, 100_000 + i),
                number=f"FXSI{300000 + i}",
                externalDocumentNumber=number, status="Paid" if i % 6 != 2 else "Open",
                customerNumber=f"FXC{1 + i % 5:03d}"))

    # ---------------------------------------------------------- purchase side
    for i in range(1, purchases + 1):
        po_id = f"fx-po-{i}"
        number = po_number(i)
        custom = _po_custom(i)
        if not custom:
            number = f"PO-9FX{i:03d}"  # operations route: prefixRef("PO-" + base36)
        rel["public.app_purchase_order"].append({
            "id": po_id, "po_number": number, "status": "RECEIVED" if i % 6 else "SUBMITTED",
            "supplier_id": f"fx-supp-{_supplier(i)}", "closed_at": None, "custom_fields": custom,
            "updated_at": UPDATED_AT})
        if custom:
            rel["public.generated_purchase_order"].append({
                "id": 6000 + i, "ponumber": custom["fmPoNumber"],
                "audittimestampmodified": UPDATED_AT})
        specs = [(1, 50 + i, "ea", 1 + i % 20)]
        if i % 4 == 0:
            specs.append((2, 5, "kg", 1 + (i + 7) % 20))
        receipt_id = f"fx-rcpt-{i}"
        vendor_invoice = f"FXVINV-{i:03d}"
        # Past the small world: some receipts record another vendor invoice No. (the
        # invoice found by order No. is then unmatched) or none (unknown).
        on_receipt: str | None = vendor_invoice
        if i > 16 and i % 10 == 7:
            on_receipt = vendor_invoice + "-R"
        elif i > 16 and i % 10 == 2:
            on_receipt = None
        rel["public.app_receipt"].append({
            "id": receipt_id, "receipt_number": receipt_lab_id(i), "purchase_order_id": po_id,
            "status": "RELEASED" if i % 6 else "INTAKE",
            "vendor_invoice_number": on_receipt, "updated_at": UPDATED_AT})
        invoice_lines = []
        for n, qty, uom, product in specs:
            received = qty - (2 if i % 5 == 0 and n == 1 else 0)
            rel["public.app_purchase_order_line"].append({
                "id": f"fx-pol-{i}-{n}", "purchase_order_id": po_id,
                "product_id": f"fx-prod-{product}", "quantity": _q(qty),
                "quantity_received": _q(received), "unit_price": "2.5", "uom": uom,
                "updated_at": UPDATED_AT})
            rel["public.app_receipt_line"].append({
                "id": f"fx-rcl-{i}-{n}", "receipt_id": receipt_id,
                "product_id": f"fx-prod-{product}", "quantity": _q(received), "uom": uom,
                "updated_at": UPDATED_AT})
            invoice_lines.append((n, received, uom, product))
        if i % 7 != 0:
            fields = {"number": f"FXPI{400000 + i}", "vendorInvoiceNumber": vendor_invoice,
                      "status": "Paid" if i % 6 else "Open",
                      "vendorNumber": f"FXV{_supplier(i):03d}"}
            if i % 3 != 0:
                fields["orderNumber"] = number
            record = bc_record("purchaseInvoices", 0, _serial(i, 400 + i, 200_000 + i), **fields)
            bc["purchaseInvoices"].append(record)
            bc_lines[record["id"]] = [
                bc_record("purchaseInvoiceLines", 0, _serial(i, 5000 + 10 * i + n, 300_000 + 10 * i + n),
                          documentId=record["id"], lineType="Item",
                          lineObjectNumber=f"FX-ITEM-{product:03d}", quantity=received,
                          unitOfMeasureCode={"ea": "PCS", "kg": "KG"}[uom], unitCost=2.5)
                for n, received, uom, product in invoice_lines]
            bc_lines[record["id"]].append(bc_record(
                "purchaseInvoiceLines", 0, _serial(i, 5000 + 10 * i + 9, 300_000 + 10 * i + 9),
                documentId=record["id"],
                lineType="Charge", lineObjectNumber="FREIGHT", quantity=1,
                unitOfMeasureCode=None, unitCost=40))
    _staging_purchases(rel)

    # Number collisions across BC companies and vendors (SOURCE-NOTES: number matches
    # are leads): another vendor's invoice in the second company reuses PO 2's vendor
    # invoice No.; PO 10's own vendor booked a second invoice in the second company.
    foreign = bc_record("purchaseInvoices", 1, 900, number="FXPI490002",
                        vendorInvoiceNumber="FXVINV-002", vendorNumber="FXV999", status="Paid")
    bc["purchaseInvoices"].append(foreign)
    bc_lines[foreign["id"]] = [bc_record(
        "purchaseInvoiceLines", 1, 9001, documentId=foreign["id"], lineType="Item",
        lineObjectNumber="FX-ITEM-003", quantity=4, unitOfMeasureCode="PCS", unitCost=9)]
    twin = bc_record("purchaseInvoices", 1, 910, number="FXPI490010", orderNumber=po_number(10),
                     vendorInvoiceNumber="FXVINV-010B", vendorNumber="FXV002", status="Paid")
    bc["purchaseInvoices"].append(twin)
    bc_lines[twin["id"]] = [bc_record(
        "purchaseInvoiceLines", 1, 9101, documentId=twin["id"], lineType="Item",
        lineObjectNumber="FX-ITEM-011", quantity=1, unitOfMeasureCode="PCS", unitCost=2.5)]

    # ---------------------------------------------------------- documents
    docs["mail"] = [
        {"handle": "graph-mail://fx-box/msg-1@ck1", "texts": [
            "RE: confirmation for FXSO100005 and FXSO100006", "Thanks, see attached."]},
        {"handle": "graph-mail://fx-box/msg-2@ck2", "texts": [
            f"{po_number(2)} shipped today", "Tracking to follow."]},
        {"handle": "graph-mail://fx-box/msg-3@ck3", "texts": [
            "Lunch on Friday?", "No identifiers here."]},
        {"handle": "graph-mail://fx-box/msg-4@ck4", "texts": [
            f"{po_number(9)}9 is not a known order",
            f"{po_number(4).lower()} lower case is the same PO (Portal TRIM ILIKE)"]},
    ]
    docs["sharepoint"] = [
        {"handle": "sharepoint://fx-drive/item-1@e1",
         "texts": [f"{po_number(5)}_packing-slip.pdf"]},
        {"handle": "sharepoint://fx-drive/item-2@e2", "texts": [f"{ao_code(9, 'A')} batch record.pdf"]},
        {"handle": "sharepoint://fx-drive/item-3@e3", "texts": ["General notes.docx"]},
    ]
    # Larger worlds: more identifiers named verbatim in mail subjects and file names.
    for i in range(25, sales + 1, 9):
        docs["mail"].append({"handle": f"graph-mail://fx-box/msg-so-{i}@ck{i}", "texts": [
            f"Order FXSO{100000 + i} confirmation", "See attached."]})
    for i in range(17, purchases + 1, 7):
        docs["sharepoint"].append({"handle": f"sharepoint://fx-drive/po-{i}@e{i}",
                                   "texts": [f"{po_number(i)}_packing-slip.pdf"]})
    return World(rel, bc, bc_lines, docs)


def drain_purchase_orders_to_bc(world: World, po_ids: Sequence[str]) -> World:
    """``world`` with each named Portal purchase order also in BC as the Portal's BC drain
    leaves it: a ``purchaseOrders`` record whose No. is the Portal ``po_number`` (BC
    Purchase Header No. = po_number in the PO series,
    docs/reference/cases-and-artifacts.md), with one line per PO line. Each is the BC twin of its PO,
    never a second PO: the same number on the identifier's primary carrier (Portal) and an
    alias carrier (BC). Changes ``world`` in place and returns it."""
    products = {p["id"]: p["code"] for p in world.relations["public.app_product"]}
    orders = {row["id"]: row for row in world.relations["public.app_purchase_order"]}
    for po_id in po_ids:
        order = orders[po_id]
        serial = 700_000 + len(world.bc.setdefault("purchaseOrders", []))
        record = bc_record("purchaseOrders", 0, serial, number=order["po_number"],
                           vendorNumber=f"FXV{order['supplier_id'].rsplit('-', 1)[1]:0>3}",
                           fullyReceived=False)
        world.bc["purchaseOrders"].append(record)
        lines = [line for line in world.relations["public.app_purchase_order_line"]
                 if line["purchase_order_id"] == po_id]
        world.bc_lines[record["id"]] = [bc_record(
            "purchaseOrderLines", 0, serial * 10 + n, documentId=record["id"],
            lineType="Item", lineObjectNumber=products[line["product_id"]],
            quantity=float(line["quantity"]),
            unitOfMeasureCode={"ea": "PCS", "kg": "KG"}.get(line["uom"], line["uom"]))
            for n, line in enumerate(lines, start=1)]
    return world


# ----------------------------------------------------------------- live-shaped world

LIVE_SHAPED_SIZE = 48


def build_live_shaped_world(size: int = LIVE_SHAPED_SIZE, *, documents: bool = True) -> World:
    """A synthetic world shaped like the served replica (data profiles,
    2026-10-02), not like the oracle's first assumptions. Every value is
    invented (FX...). The shapes that left five strata empty on the first live run:

    * legacy FileMaker sales orders: id ``fm-so-{n}``, order number ``SO-FM-{n}``, no
      custom_fields, FM status text ("Completed", "Closed", "Open", "Cancelled");
    * Sage-loaded sales orders left READY_TO_SHIP, done only by the Sage header's flag;
    * ETL'd purchase orders: FM header status text, ``closed_at`` always empty, completion
      only visible in the line quantities (``quantity_received``), PO numbers with ``#``;
    * ``app_receipt`` empty: receipts exist only as FM raw receipts (lab ids) tied to FM PO
      items;
    * assembly orders without a sales-order line link;
    * mail subjects naming a ``#`` PO number, SharePoint files named by lab id.
    """
    rel: dict[str, list[dict[str, Any]]] = {k: [] for k in (
        "public.app_sales_order", "public.app_sales_order_line", "public.app_customer",
        "public.app_product", "public.app_manufacturing_order", "public.app_shipment",
        "public.app_shipment_line", "public.order_entry", "public.assembly_order",
        "public.order_entry_item", "public.order_entry_item_assembly",
        "sage.oe_order_header", "public.app_purchase_order", "public.app_purchase_order_line",
        "public.app_supplier", "public.app_receipt", "public.app_receipt_line",
        "public.generated_purchase_order", "public.generated_purchase_order_item",
        "public.receipt_raw_material", "public.assembly_order_status")}
    bc: dict[str, list[dict[str, Any]]] = {"salesInvoices": [], "purchaseInvoices": [],
                                           "salesOrders": [], "purchaseOrders": [],
                                           "purchaseReceipts": []}
    bc_lines: dict[str, list[dict[str, Any]]] = {}
    docs: dict[str, list[dict[str, Any]]] = {"mail": [], "sharepoint": []}
    for c in range(1, 4):
        rel["public.app_customer"].append({"id": f"fm-cust-{c}", "code": f"FXC{c:03d}",
                                           "updated_at": UPDATED_AT})
    for p in range(1, 9):
        rel["public.app_product"].append({"id": f"fx-prod-{p}", "code": f"FX-ITEM-{p:03d}",
                                          "updated_at": UPDATED_AT})
    for v in range(1, 4):
        rel["public.app_supplier"].append({"id": f"fm-supp-{v}", "code": f"FXV{v:03d}",
                                           "updated_at": UPDATED_AT})
    fm_status = ("Completed", "Closed", "Open", "Cancelled")
    for n in range(1, size + 1):
        # Legacy FM migration rows (no custom_fields) and Sage-loaded rows.
        if n % 4 != 0:
            so_id, number = f"fm-so-{100 + n}", f"SO-FM-{100 + n}"
            custom: Any = None
            status = fm_status[n % 4] if n % 4 != 1 else "Completed"
        else:
            so_id, number = f"fx-so-sage-{n}", f"FXS{n:05d}"
            # Sage-loaded orders stay READY_TO_SHIP in gold; completion is the Sage
            # header's complete flag. The ETL stamps the raw orduniq in sageSourceOrdUniq
            # (P@82549dd scripts/etl-sage-distribution-gold.mjs:1500-1504).
            custom = {"sageOrdUniq": str(7000 + n), "sageSourceOrdUniq": str(7000 + n),
                      "source": "sage.oe_order_header"}
            status = "READY_TO_SHIP"
            rel["sage.oe_order_header"].append({"orduniq": str(7000 + n),
                                                "ordnumber": number,
                                                "ponumber": f"FXCP-{n:03d}",
                                                "complete": "1" if n % 8 == 0 else "0",
                                                "audtdate": "20981231"})
        rel["public.app_sales_order"].append({
            "id": so_id, "order_number": number, "status": status,
            "po_reference": f"FXCP-{n:03d}" if n % 5 else "FX Person",
            "customer_id": f"fm-cust-{1 + n % 3}", "custom_fields": custom,
            "required_date": None, "updated_at": UPDATED_AT})
        line_id = f"fx-sol-{n}"
        rel["public.app_sales_order_line"].append({
            "id": line_id, "sales_order_id": so_id, "product_id": f"fx-prod-{1 + n % 8}",
            "quantity": _q(10 * n), "uom": "ea", "updated_at": UPDATED_AT})
        if n % 3:
            rel["public.app_shipment"].append({
                "id": f"fx-shp-{n}", "sales_order_id": so_id, "shipment_code": f"FXSH{n:05d}",
                "status": "SHIPPED", "ship_date": "2098-12-20", "updated_at": UPDATED_AT})
            rel["public.app_shipment_line"].append({
                "id": f"fx-shl-{n}", "shipment_id": f"fx-shp-{n}",
                "product_id": f"fx-prod-{1 + n % 8}", "quantity": _q(10 * n), "uom": "ea",
                "updated_at": UPDATED_AT})
        if n % 2:
            bc["salesInvoices"].append(bc_record(
                "salesInvoices", 0, 700 + n, number=f"FXSI{500000 + n}",
                externalDocumentNumber=number, status="Paid",
                customerNumber=f"FXC{1 + n % 3:03d}"))
        # Assembly orders: realized, no sales-order line link (served column empty).
        code = ao_code(n, "L")
        rel["public.app_manufacturing_order"].append({
            "id": f"fx-mo-{n}", "mo_code": code, "sales_order_line_id": None,
            "status": "Realized" if n % 5 else "Committed", "yield_actual": None,
            "uom": "ea", "date_realized": "2098-11-01T00:00:00Z" if n % 5 else None,
            "deleted_at": None, "updated_at": UPDATED_AT})
        rel["public.assembly_order"].append({"id": 8000 + n, "nameamo_c": code,
                                             "audittimestampmodified": UPDATED_AT})
        # Purchase orders loaded from FM: header text, closed_at empty, lines decide.
        po_id, po_number = f"fx-po-fm-{n}", f"FX#{2000 + n}"
        rel["public.app_purchase_order"].append({
            "id": po_id, "po_number": po_number, "status": ("Pending", "Complete")[n % 2],
            "supplier_id": f"fm-supp-{1 + n % 3}", "closed_at": None,
            "custom_fields": {"fmGeneratedPoId": 6000 + n, "fmPoNumber": po_number,
                              "source": "etl-silver-to-gold"},
            "updated_at": UPDATED_AT})
        ordered = 40 + n
        received = ordered if n % 6 else ordered - 5
        rel["public.app_purchase_order_line"].append({
            "id": f"fx-pol-{n}", "purchase_order_id": po_id,
            "product_id": f"fx-prod-{1 + n % 8}", "quantity": _q(ordered),
            "quantity_received": _q(received), "unit_price": "2.5", "uom": "ea",
            "updated_at": UPDATED_AT})
        rel["public.generated_purchase_order"].append({
            "id": 6000 + n, "ponumber": po_number, "audittimestampmodified": UPDATED_AT})
        rel["public.generated_purchase_order_item"].append({
            "id": 16000 + n, "idpogen": 6000 + n, "audittimestampmodified": UPDATED_AT})
        # Receipts exist only in FM: one raw receipt per lab id, tied to the PO item.
        rel["public.receipt_raw_material"].append({
            "id": 26000 + n, "idlabid": f"FX{98000 + n}-1", "idpogenitem": 16000 + n,
            "status_c": "Released" if n % 7 else "Quarantine",
            "audittimestampmodified": UPDATED_AT})
        if n % 3 == 0:
            invoice = bc_record("purchaseInvoices", 0, 800 + n, number=f"FXPI{600000 + n}",
                                orderNumber=po_number, vendorInvoiceNumber=f"FXVI-{n:03d}",
                                vendorNumber=f"FXV{1 + n % 3:03d}", status="Paid")
            bc["purchaseInvoices"].append(invoice)
            bc_lines[invoice["id"]] = [bc_record(
                "purchaseInvoiceLines", 0, 9000 + n, documentId=invoice["id"],
                lineType="Item", lineObjectNumber=f"FX-ITEM-{1 + n % 8:03d}",
                quantity=received, unitOfMeasureCode="PCS", unitCost=2.5)]
        if documents and n % 2 == 0:
            # Every other mail also names a sales order (two roots: it identifies neither);
            # the rest name the purchase order alone.
            texts = [f"RE: PO {po_number} confirmation"]
            if n % 4 == 2:
                texts.append(f"Order SO-FM-{100 + n - 1} shipped.")
            docs["mail"].append({"handle": f"graph-mail://fx-box/msg-po-{n}@ck{n}",
                                 "texts": texts})
            docs["sharepoint"].append({"handle": f"sharepoint://fx-drive/coa-{n}@e{n}",
                                       "texts": [f"FX{98000 + n}-1_CoA.pdf"]})
    return World(rel, bc, bc_lines, docs if documents else {})


__all__ = ["COMPANIES", "EVAL_PURCHASES", "EVAL_SALES", "FixtureBc", "FixtureDocuments",
           "GENERATED_AT", "LIVE_SHAPED_SIZE", "World", "bc_record", "build_eval_world",
           "drain_purchase_orders_to_bc",
           "ao_code", "build_live_shaped_world", "build_world", "fm_po_number", "po_number",
           "receipt_lab_id", "shipment_code"]
