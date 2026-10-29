"""The Portal twin: the Portal's cataloged write routes and its reporting read view.

Built from the generator's facts, not written by hand per route:

* the routes are the action catalog's ``real_capability`` of every Portal type dispatched
  through a gated adapter (method, path, path keys, the API fields it writes and reads
  back, ``owner_state``, ``owner_warnings``);
* the app record of each route's relation is its Prisma model in
  ``ontology/schema/generated/meta/portal_prisma.json`` (column -> API field, type,
  optional), so a GET answers the Prisma record as the route's ``getSalesOrder`` does;
* the reporting read view (``resolve``, ``peers``) serves the relations and columns of
  ``ontology/schema/generated/meta/portal_catalog.json``, keyed as the catalog keys them.

It is seeded from fixture captures (``seed_captures``; ``CAPTURES`` holds synthetic
reporting-API rows): a captured column the catalog does not serve, or an app record without
a required Prisma field, is refused.

A request follows the Portal route it mirrors, each rule pinned in
the upstream pin file (drift-checked) at plx-customer-portal@728a995, re-read at 5cb95e1
and 38cd3f3 (unchanged rules; lines moved: route GET 89-111 and PATCH 114, editability.ts 45-70 and
116-160, updateSalesOrderHeader service.ts 663-725; the PATCH also accepts ``releaseRequired``,
which no cataloged action writes):

* ``portal/src/app/api/mrp/sales-orders/[id]/route.ts:83-84,103-104`` (88-89,113-114@5cb95e1):
  GET and PATCH ``requireRole("STAFF")``: no session is a 401 (``Unauthorized``), a session
  role below
  the STAFF floor of ``portal/src/lib/auth/roles.ts:44-51,238-244`` (or unknown) a 403
  (``Forbidden``). ``session_role`` is the role the Portal holds for the session the
  transport presents (legacy role mode of ``resolveRuntimeAccess``);
* a GET answers the record with the order's edit lock as ``getSalesOrderEditability``
  computes it (``portal/src/lib/sales-order/editability.ts:42-67,113-157@5cb95e1``, served
  by ``route.ts:88-111@5cb95e1``: ``editable``, ``editLock`` ``{reason, message}`` or null,
  ``linesLocked``): ``STATUS`` while ``canEditSalesOrderUntilShipped``
  (``portal/src/lib/sales-order/status.ts:28-33@31a2623``) is false, else ``SHIPPED`` once a
  shipment of the order (``public.app_shipment.sales_order_id``) is SHIPPED or DELIVERED
  (upper-cased; CANCELLED ignored) or one of its lines (``public.app_sales_order_line``)
  has an allocation (``allocate``) on such a shipment;
* a body field the catalog does not map is a 400 (the twin serves the catalog's write
  surface only), and a record whose GET would not admit the write
  (``action_catalog.owner_state_refusal``, the adapter's own pre-write rule) is a 409
  ``ORDER_NOT_EDITABLE`` ``Order edits are locked after the first shipment or cancel``
  (``portal/src/lib/sales-order/service.ts:620-630@eb964b99``, 634-641@5cb95e1),
  a value Prisma's column type refuses is a 500 with nothing applied, and a text value is
  stored as sent, untrimmed (``service.ts:655-657@eb964b99``, ``updateSalesOrderHeader``);
  ``updatedAt`` moves. The read-back
  (``case_verification.matches``) trims the stored value only, so an expected value with
  outer whitespace reads ``differs``: it fails closed, never a false ``effect_observed``;
* the answer's ``warnings`` (``route.ts:70-81`` ``poWarningsFor``) are computed only when
  the body sets a non-empty ``poReference`` (JavaScript ``trim()``, ``action_catalog.js_trim``), as ``portal/src/lib/orders/
  customer-po.ts:17-20,47-89,96-116`` builds them: ``PO_REUSED_BY_CUSTOMER`` with
  ``message``, the normalized ``poReference`` and up to 10 other ``orders`` of the same
  customer (``salesOrderId``, ``orderNumber``, ``status``; by order number; not
  cancelled), or ``PO_REUSE_CHECK_FAILED`` when the check itself failed (the save stands).
  The twin orders order numbers by Python code point, Postgres by the database collation:
  with mixed case or punctuation the 10 listed (and their order) can differ from the
  Portal's. Nothing on the action path reads the answer's warnings.

``faults`` reproduce what a real owner can answer to the next write: ``lost_answer``
(applied, the answer lost), ``etag_mismatch`` (412, nothing applied: an owner's
compare-and-set conflict; the Portal route takes no If-Match today, so this proves the
adapter's 412 contract), ``unavailable`` (no answer, nothing applied), ``server_error``
(500, nothing applied) and ``reuse_check_failed`` (applied; the reuse check failed).
Evidence label: ``twin-transport``.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote

from ontology.core import Actor
from operating.action_catalog import (GATED_ADAPTER, ActionCatalog, js_trim, normalized,
                                      owner_state_admits, owner_state_refusal)
from operating.cases import CitationError

from .mode import TWIN_LABEL

ROOT = Path(__file__).resolve().parents[1]
META = ROOT / "ontology" / "schema" / "generated" / "meta"
PRISMA_FACTS = META / "portal_prisma.json"
CATALOG_FACTS = META / "portal_catalog.json"
CAPTURES = Path(__file__).with_name("fixtures") / "portal_captures.json"
FAULTS = ("lost_answer", "etag_mismatch", "unavailable", "server_error",
          "reuse_check_failed")
# plx-customer-portal@728a995 portal/src/lib/auth/roles.ts:44-51 (ROLE_LEVEL) and the
# requireRole("STAFF") floor of portal/src/app/api/mrp/sales-orders/[id]/route.ts:83,103.
ROLE_LEVEL = {"CUSTOMER": 0, "SUPPLIER": 0, "STAFF": 1, "MANAGER": 2, "ADMIN": 3,
              "SUPER_ADMIN": 4}
ROUTE_ROLE = "STAFF"
# Prisma scalar type of a written field -> the Python values Prisma takes for it (another
# value: Prisma refuses the update, the route answers 500); a route writing another type is
# not emulated (``route_field_not_emulated``).
_PRISMA_TYPES: Mapping[str, tuple[type, ...]] = {"String": (str,)}
# The owner warnings the twin emulates (catalog ``owner_warnings`` ids).
_WARNINGS = frozenset({"po_reused_by_customer"})
# The owner edit locks the twin emulates (catalog ``owner_state.edit_lock`` refusal ->
# the route relation it is computed for) and the relations it reads.
_EDIT_LOCKS = {"order_locked_shipped": "public.app_sales_order"}
SHIPMENTS = "public.app_shipment"
ORDER_LINES = "public.app_sales_order_line"
# editability.ts:42@5cb95e1 (SHIPPED_SHIPMENT) and :70-78 (editLockMessage).
_SHIPPED = frozenset({"SHIPPED", "DELIVERED"})
_LOCK_MESSAGES = {
    "STATUS": "This order is shipped, complete or cancelled, so it can no longer be edited.",
    "SHIPPED": "This order has a shipment that has already shipped, so it can no longer "
               "be edited."}
# customer-po.ts:107-114 (salesOrderPoWarnings): the reuse check failed, the order saved.
CHECK_FAILED = {"code": "PO_REUSE_CHECK_FAILED",
                "message": "The order saved, but the portal could not check this "
                           "customer's PO for reuse."}
HANDLE = re.compile(r"^api://(?P<relation>[a-z_]+\.[a-z0-9_]+)/(?P<key>[^/@]{1,200})@twin$")


class TwinError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class PortalTwin:
    """``readers``: principals that may read the reporting view (``None``: any actor).
    ``session_role``: the Portal role of the session the transport presents (``None``: no
    session; every app route answers 401)."""

    SYSTEM = "portal"

    def __init__(self, *, readers: frozenset[str] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 catalog: ActionCatalog | None = None,
                 session_role: str | None = None) -> None:
        catalog = catalog or ActionCatalog.builtin()
        models = {model["table"]: model for model in _load(PRISMA_FACTS)["models"].values()}
        self.served = {item["name"]: {"columns": frozenset(item["columns"]),
                                      "key": tuple(item["key"])}
                       for item in _load(CATALOG_FACTS)["relations"]}
        self.readers = readers
        self.session_role = session_role
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.faults: list[str] = []
        self.writes = 0
        # ShipmentAllocation rows: {"line", "shipment", "quantity"} (``allocate``)
        self.allocations: list[dict[str, Any]] = []
        # relation -> its Prisma columns {column: {field, type, optional}}
        self.models: dict[str, dict[str, Mapping[str, Any]]] = {}
        # (method, compiled path, relation, path keys, real_capability)
        self.routes: list[tuple[str, re.Pattern[str], str, Mapping[str, str],
                                Mapping[str, Any]]] = []
        for name in catalog.proposal_ids():
            spec = catalog.real_capability(name)
            if (spec is None or spec["dispatch"] != GATED_ADAPTER
                    or spec["target_system"] != self.SYSTEM):
                continue
            for relation in spec["target_relations"]:
                model = models.get(relation.partition(".")[2])
                if model is None or relation not in self.served:
                    raise TwinError("route_relation_without_facts:" + relation)
                self.models[relation] = model["columns"]
                fields = {item["field"]: item for item in model["columns"].values()}
                for api in spec["write"]["fields"].values():
                    if fields.get(api, {}).get("type") not in _PRISMA_TYPES:
                        raise TwinError("route_field_not_emulated:" + api)
                for warning in spec.get("owner_warnings", ()):
                    if warning["id"] not in _WARNINGS:
                        raise TwinError("owner_warning_not_emulated:" + warning["id"])
                lock = (spec.get("owner_state") or {}).get("edit_lock")
                if lock is not None and _EDIT_LOCKS.get(lock["refusal"]) != relation:
                    raise TwinError("owner_lock_not_emulated:" + lock["refusal"])
                for part in ("write", "read_back"):
                    http = spec[part]
                    if list(http["path_keys"].values()) != list(self.served[relation]["key"]):
                        raise TwinError("route_key_is_not_the_relation_key:" + relation)
                    pattern = re.escape(http["path"])
                    for placeholder in http["path_keys"]:
                        pattern = pattern.replace(re.escape("{" + placeholder + "}"),
                                                  f"(?P<{placeholder}>[^/]+)")
                    self.routes.append((http["method"], re.compile(pattern + "$"), relation,
                                        http["path_keys"], spec))
        # relation -> key text -> record (app relations: Prisma fields; others: columns)
        self.records: dict[str, dict[str, dict[str, Any]]] = {}

    # ---- seeding (fixture captures) -------------------------------------

    def seed_captures(self, records: Iterable[Any]) -> list[str]:
        """Seed reporting-API captures (``{"relation", "row"}`` mappings or
        ``CapturedRecord``-like objects); returns their citation handles."""
        handles = []
        for record in records:
            relation = record["relation"] if isinstance(record, Mapping) else record.relation
            row = record["row"] if isinstance(record, Mapping) else record.row
            handles.append(self.seed(relation, row))
        return handles

    def seed(self, relation: str, row: Mapping[str, Any]) -> str:
        served = self.served.get(relation)
        if served is None:
            raise TwinError("relation_not_served:" + relation)
        unknown = sorted(set(row) - served["columns"])
        if unknown:
            raise TwinError("column_not_served:" + ",".join(unknown))
        if len(served["key"]) != 1 or row.get(served["key"][0]) in (None, ""):
            raise TwinError("capture_without_key:" + relation)
        key = str(row[served["key"][0]])
        model = self.models.get(relation)
        if model is None:
            record = copy.deepcopy(dict(row))
        else:
            # A capture can only miss a required column the catalog serves; one it does not
            # serve is never captured (app_sales_order.release_required was such a column
            # until the manifest at Portal 0323e19c, c2ff912f TASK-3355, served it).
            missing = sorted(column for column, spec in model.items()
                             if not spec["optional"] and column in served["columns"]
                             and row.get(column) is None)
            if missing:
                raise TwinError("required_field_missing:" + ",".join(missing))
            record = {spec["field"]: copy.deepcopy(row.get(column))
                      for column, spec in model.items()}
        self.records.setdefault(relation, {})[key] = record
        return self.citation(relation, key)

    def allocate(self, line: str, shipment: str, quantity: Any) -> None:
        """A ``ShipmentAllocation``: ``quantity`` of the order line ``line`` on the shipment
        ``shipment`` (both seeded, by key). Portal app state the reporting API does not
        serve (no relation in ``portal_catalog.json``), so it is set here, never captured."""
        if (line not in self.records.get(ORDER_LINES, {})
                or shipment not in self.records.get(SHIPMENTS, {})
                or isinstance(quantity, bool) or not isinstance(quantity, (int, float))
                or quantity != quantity or quantity in (float("inf"), float("-inf"))):
            raise TwinError("allocation_invalid")
        self.allocations.append({"line": line, "shipment": shipment, "quantity": quantity})

    @staticmethod
    def citation(relation: str, key: Any) -> str:
        return f"api://{relation}/{key}@twin"

    # ---- the reporting read view ----------------------------------------

    def _columns(self, relation: str, record: Mapping[str, Any]) -> dict[str, Any]:
        model = self.models.get(relation)
        if model is None:
            return copy.deepcopy(dict(record))
        served = self.served[relation]["columns"]   # the read view serves served columns only
        return {column: copy.deepcopy(record.get(spec["field"]))
                for column, spec in model.items() if column in served}

    def _visible(self, actor: Actor) -> bool:
        return self.readers is None or bool(set(actor.principals) & set(self.readers))

    def resolve(self, handle: str, actor: Actor) -> dict[str, Any]:
        """The case store's citation resolver for ``api://<relation>/<key>@twin``."""
        if not self._visible(actor):
            raise CitationError("not visible")
        found = HANDLE.fullmatch(handle) if isinstance(handle, str) else None
        if found is None:
            raise CitationError("not_a_twin_handle")
        relation, key = found["relation"], found["key"]
        record = self.records.get(relation, {}).get(key)
        if record is None:
            raise CitationError("twin_row_absent")
        row = self._columns(relation, record)
        key_column = self.served[relation]["key"][0]
        return {"relation": relation, "key": {key_column: row[key_column]}, "row": row,
                "evidence": TWIN_LABEL}

    def peers(self, actor: Actor, relation: str) -> list[dict[str, Any]] | None:
        """Every record of ``relation``, as ``{"key", "row"}`` (the owner-warning peer read
        of ``ActionPath``); ``None`` for an actor who may not read the view."""
        return self._rows(relation) if self._visible(actor) else None

    def _rows(self, relation: str) -> list[dict[str, Any]]:
        key_column = self.served[relation]["key"][0]
        return [{"key": {key_column: row[key_column]}, "row": row}
                for row in (self._columns(relation, record)
                            for record in self.records.get(relation, {}).values())]

    # ---- the app routes ---------------------------------------------------

    def handle(self, request: Any) -> dict[str, Any]:
        """Answer one ``OwnerRequest`` as the Portal route would (``TwinTransport``)."""
        for method, pattern, relation, path_keys, spec in self.routes:
            found = pattern.fullmatch(request.path)
            if found is None or method != request.method:
                continue
            refused = self._session_refusal()
            if refused is not None:
                return refused
            [placeholder] = path_keys
            record = self.records.get(relation, {}).get(unquote(found[placeholder]))
            if record is None:
                return {"status": 404, "body": {"error": "Record not found"}}
            if method == "GET":
                return {"status": 200, "body": self._view(record, spec)}
            return self._write(relation, record, request.body, spec)
        return {"status": 404, "body": {"error": "Route not served by the twin"}}

    def _view(self, record: Mapping[str, Any], spec: Mapping[str, Any]) -> dict[str, Any]:
        """The route's GET body: the record and, for a route with an ``edit_lock``, the
        order's editability (``loadEditabilityForOrder``, editability.ts:113-157@5cb95e1;
        ``route.ts:96-103@5cb95e1``)."""
        body = copy.deepcopy(dict(record))
        state = spec.get("owner_state") or {}
        lock = state.get("edit_lock")
        if lock is None:
            return body
        order = record.get("id")
        shipments = self.records.get(SHIPMENTS, {})
        lines = {key for key, line in self.records.get(ORDER_LINES, {}).items()
                 if line.get("sales_order_id") == order}
        shipped: dict[str, Any] = {}
        allocated: dict[str, Any] = {}
        for item in self.allocations:
            status = _upper(shipments[item["shipment"]].get("status"))
            if item["line"] not in lines or status == "CANCELLED":
                continue
            allocated[item["line"]] = allocated.get(item["line"], 0) + item["quantity"]
            if status in _SHIPPED:
                shipped[item["line"]] = shipped.get(item["line"], 0) + item["quantity"]
        statuses = [_upper(row.get("status")) for row in shipments.values()
                    if row.get("sales_order_id") == order]
        # getSalesOrderEditability (editability.ts:48-67@5cb95e1): status first, then shipped.
        reason = ("STATUS" if not owner_state_admits(state, record.get(state["api_field"]))
                  else "SHIPPED" if any(s in _SHIPPED for s in statuses)
                  or any(quantity > 0 for quantity in shipped.values())
                  else None)
        body[lock["flag"]] = reason is None
        body[lock["api_field"]] = (None if reason is None
                                   else {"reason": reason, "message": _LOCK_MESSAGES[reason]})
        body["linesLocked"] = reason is not None or any(q > 0 for q in allocated.values())
        return body

    def _session_refusal(self) -> dict[str, Any] | None:
        """``requireRole(ROUTE_ROLE)`` (plx-customer-portal@728a995
        portal/src/lib/auth/api-auth.ts:73-86,99-124): 401 without a session, 403 below
        the floor."""
        if self.session_role is None:
            return {"status": 401, "body": {"error": "Unauthorized"}}
        if ROLE_LEVEL.get(self.session_role, -1) < ROLE_LEVEL[ROUTE_ROLE]:
            return {"status": 403, "body": {"error": "Forbidden"}}
        return None

    def _write(self, relation: str, record: dict[str, Any], body: Any,
               spec: Mapping[str, Any]) -> dict[str, Any]:
        fault = self.faults.pop(0) if self.faults else None
        if fault is not None and fault not in FAULTS:
            raise TwinError("unknown_fault")
        writable = set(spec["write"]["fields"].values())
        if not isinstance(body, Mapping) or not body or set(body) - writable:
            return {"status": 400, "body": {"error": "Provide only the header fields this "
                                                     "route takes"}}
        if fault == "unavailable":
            raise ConnectionError("portal twin unavailable before accepting the request")
        if fault == "server_error":
            return {"status": 500, "body": {"error": "Failed to update sales order"}}
        if fault == "etag_mismatch":
            return {"status": 412, "body": {"error": "Precondition failed"}}
        state = spec.get("owner_state")
        if state is not None and owner_state_refusal(state, self._view(record, spec)):
            return {"status": 409, "body": {"error": "Order edits are locked after the "
                                                     "first shipment or cancel",
                                            "code": "ORDER_NOT_EDITABLE"}}
        types = {item["field"]: item for item in self.models[relation].values()}
        for name, value in body.items():
            column = types[name]
            if not (value is None and column["optional"]
                    or isinstance(value, _PRISMA_TYPES[column["type"]])):
                return {"status": 500, "body": {"error": "Failed to update sales order"}}
        # service.ts:655-657: the value is stored as sent (no trim, "" stays "").
        record.update(copy.deepcopy(dict(body)))
        updated = self.models[relation].get("updated_at")
        if updated is not None:
            record[updated["field"]] = self.clock().isoformat(timespec="milliseconds")
        self.writes += 1
        if fault == "lost_answer":
            raise TimeoutError("portal twin saved; the answer was lost")
        warnings = self._warnings(relation, record, body, spec,
                                  check_failed=fault == "reuse_check_failed")
        key_field = self.models[relation][self.served[relation]["key"][0]]["field"]
        answer = {name: record.get(name) for name in (key_field, "orderNumber", "status")
                  if name in record}
        return {"status": 200, "body": {**answer, "warnings": warnings}}

    def _warnings(self, relation: str, record: Mapping[str, Any], body: Mapping[str, Any],
                  spec: Mapping[str, Any], *, check_failed: bool) -> list[dict[str, Any]]:
        """The owner warnings the catalog declares for this route, each as the Portal
        builds it (``_WARNINGS``; the constructor refuses a route declaring another)."""
        out = []
        for warning in spec.get("owner_warnings", ()):
            # route.ts:70-81@728a995 (poWarningsFor): only when this PATCH set a PO.
            sent = body.get(self.models[relation][warning["field"]]["field"])
            if not (isinstance(sent, str) and js_trim(sent)):
                continue
            if check_failed:
                out.append(dict(CHECK_FAILED))
                continue
            reuse = self._po_reuse(relation, record)
            if reuse is not None:
                out.append(reuse)
        return out

    def _po_reuse(self, relation: str, record: Mapping[str, Any]) -> dict[str, Any] | None:
        """``findCustomerPoReuse`` (plx-customer-portal@728a995
        portal/src/lib/orders/customer-po.ts:47-89): the same customer's other
        orders, not cancelled, whose PO normalizes to this one; ordered by order number,
        at most 10."""
        customer = record.get("customerId")
        customer = js_trim(customer) if isinstance(customer, str) else ""
        po = normalized("trim_collapse_upper", record.get("poReference"))
        if not customer or po is None:
            return None
        key_field = self.models[relation][self.served[relation]["key"][0]]["field"]
        others = sorted(
            (other for other in self.records.get(relation, {}).values()
             if other.get("customerId") == customer
             and normalized("trim_collapse_upper", other.get("poReference")) == po
             # SQL ``status <> 'CANCELLED'``: a NULL status is not selected either.
             and isinstance(other.get("status"), str) and other["status"] != "CANCELLED"
             and other.get(key_field) != record.get(key_field)),
            key=lambda other: str(other.get("orderNumber")))[:10]
        if not others:
            return None
        orders = [{"salesOrderId": other.get(key_field),
                   "orderNumber": other.get("orderNumber"), "status": other.get("status")}
                  for other in others]
        return {"code": "PO_REUSED_BY_CUSTOMER",
                "message": f"This customer already used PO {po} on "
                           + ", ".join(str(item["orderNumber"]) for item in orders)
                           + ". Check that this is not a duplicate order.",
                "poReference": po, "orders": orders}


def _upper(status: Any) -> str:
    """``String(status ?? "").toUpperCase()`` (editability.ts:45,138,154@5cb95e1)."""
    return "" if status is None else str(status).upper()


def load_captures(path: Path = CAPTURES) -> list[dict[str, Any]]:
    document = _load(path)
    if document.get("format") != "plx-twin-captures/v1":
        raise TwinError("captures_format")
    return list(document["records"])


__all__ = ["CAPTURES", "FAULTS", "PortalTwin", "TwinError", "load_captures"]
