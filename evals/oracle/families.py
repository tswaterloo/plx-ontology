"""Ground truth per question family, computed directly from raw source reads.

Interface ``plx-eval-oracle/v1`` (evals/item.schema.json ``oracle_result``):
``oracle(root, as_known_at) -> {"answerability", "values", "chain", "gaps"}``.
``make_oracle(family, sources)`` returns that callable for one family; ``ORACLES`` names
each family's oracle. Families (09-CRITIQUE R7):

* F1 chain: the records linked to the root across Portal gold, FM, Sage and BC, walked
  over the declared keys in ``keys.EDGES`` (one walker, no per-kind branch);
* F2 customer-PO status: the sales orders carrying that customer PO, their status and
  shipments;
* F3 ordered / produced / shipped quantities of a sales order (never mixed across roles
  or units);
* F4 three-way match: purchase-order lines vs Portal receipts vs BC purchase invoices;
* F5 (held-out only) supplier invoices of a purchase order that no receipt is tied to;
* F6 (held-out only) whether a sales order shipped short, per item.

Honesty rules: a value the sources do not hold is ``null`` with a gap saying why (F3/F4: the
gap's ``concerns`` names that value, one of the oracle's own, so the scorer matches gaps on
state and concerns rather than on the subject's wording); a
failed read is ``inaccessible``, never absence; a sum over rows with an empty quantity
is not a sum; quantities with different units are never added; ``answerable`` only when
every value is present and no gap remains.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from itertools import zip_longest
import re
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

from . import keys as K
from .source import (BcReads, PortalReads, ReadFailed, canonical, decimal_of, decimal_text,
                     digest, field_value, instant, key_text, sql_trim)

INTERFACE = "plx-eval-oracle/v1"
MAX_CHAIN = 100
MAX_VALUES = 100  # evals/item.schema.json oracle_result.values maxItems
MAX_DEPTH = 6
MAX_CITATIONS = 8
# The cells one verdict (or one F4 leg) names: every one an ``equal`` (a ``sum``) must read,
# never cut to MAX_CITATIONS
# (evals/score.py MAX_CALCULATION_INPUTS, the inputs one calculation states).
MAX_VERDICT_CELLS = 100
BC_VENDOR_SETS = frozenset({"bc:purchaseOrders", "bc:purchaseReceipts", "bc:purchaseInvoices"})


@dataclass
class Sources:
    portal: PortalReads
    bc: BcReads | None = None


@dataclass(frozen=True)
class Node:
    relation: str          # "schema.relation" or "bc:<entitySet>"
    row: Mapping[str, Any]  # Portal row, or a BC record's ``fields``
    record: Mapping[str, Any] | None = None  # the whole BC record

    @property
    def is_bc(self) -> bool:
        return self.relation.startswith("bc:")


class Context:
    """One oracle call: reads, the gaps found and the rules used."""

    def __init__(self, sources: Sources, as_known_at: Mapping[str, Any]) -> None:
        self.portal = sources.portal
        self.bc = sources.bc
        self.cutoff = instant((as_known_at or {}).get("time"))
        self.gaps: list[dict[str, str]] = []
        self.cites: list[str] = []
        self.generation = self.portal.generation()
        self.bc_seen: dict[str, str] = {}
        self.unverified: set[str] = set()  # BC entity sets kept without a vendor check

    def _saw(self, records: Iterable[Mapping[str, Any]]) -> None:
        for record in records:
            self.bc_seen[str(record.get("citation"))] = digest(record.get("fields"))

    def bc_generation(self) -> str | None:
        """Digest of the BC records this answer read (cited etags + field content)."""
        return digest(sorted(self.bc_seen.items()))[:16] if self.bc_seen else None

    def gap(self, state: str, subject: str, reason: str, concerns: str | None = None) -> None:
        entry = {"state": state, "subject": subject[:700], "reason": reason}
        if concerns is not None:
            entry["concerns"] = concerns
        if entry not in self.gaps:
            self.gaps.append(entry)

    def cite(self, text: str) -> None:
        if text not in self.cites:
            self.cites.append(text)

    # ----------------------------------------------------------------- reads

    def portal_lookup(self, relation: str, field_name: str, value: Any) -> list[Node]:
        try:
            rows = self.portal.lookup(relation, field_name, value)
        except ReadFailed as exc:
            self.gap("inaccessible", relation, "read_failed_" + _token(exc.reason))
            return []
        return [Node(relation, row) for row in rows]

    def bc_find(self, entity_set: str, field_name: str, value: Any, *,
                check_span: bool = True) -> list[Node]:
        if self.bc is None:
            self.gap("not_ingested", "Business Central documents", "bc_not_read")
            return []
        try:
            records, complete = self.bc.find(entity_set, field_name, value)
        except ReadFailed as exc:
            # a typed cause (the field is not exposed, the value not queryable) names
            # itself; any other failure names its error class
            typed = exc.cause in ("not_exposed", "not_queryable")
            self.gap("inaccessible", "bc:" + entity_set,
                     "read_failed_" + (exc.cause if typed else _token(exc.reason)))
            return []
        if not complete:
            self.gap("unknown", "bc:" + entity_set, "bc_read_incomplete")
        self._saw(records)
        nodes = [Node("bc:" + entity_set, r["fields"], r) for r in records]
        if check_span:
            self.check_span(entity_set, nodes)
        return nodes

    def check_span(self, entity_set: str, nodes: Sequence[Node]) -> None:
        """A number found in several BC companies is a lead, not an identity."""
        companies = {str((n.record or {}).get("company_id")).lower() for n in nodes}
        if len(companies) > 1:
            self.cite(K.BC_IDENTITY_CITE)
            self.gap("unknown", f"bc:{entity_set} matches in {len(companies)} companies",
                     "bc_match_spans_companies")

    def po_vendor(self, node: Node) -> tuple[bool, str | None]:
        """(resolved, BC Vendor No.) of the purchase order behind a PO or receipt row."""
        order = node
        if node.relation == K.RECEIPT:
            orders = self.portal_lookup(K.PO, "id", node.row.get("purchase_order_id"))
            if len(orders) != 1:
                return False, None
            order = orders[0]
        if order.relation != K.PO:
            return False, None
        suppliers = self.portal_lookup(K.SUPPLIER, "id", order.row.get("supplier_id"))
        if len(suppliers) != 1:
            return False, None
        self.cite(K.VENDOR_CITE)
        vendor = K.vendor_bc_number(suppliers[0].row.get("code"))
        if vendor is None:
            return True, None
        # Supplier codes are unique (app_supplier.code @unique) and a shared name is no
        # identity, but the BC vendor No. is the code trimmed and cut, and BC compares Code
        # fields ignoring case: two suppliers whose codes give one vendor No. leave it
        # naming neither, a lead, not the PO's vendor (conservative identity).
        try:
            namesakes = [row for row in self.portal.rows(K.SUPPLIER)
                         if (K.vendor_bc_number(row.get("code")) or "").casefold()
                         == vendor.casefold()]
        except ReadFailed as exc:
            self.gap("inaccessible", K.SUPPLIER, "read_failed_" + _token(exc.reason))
            return False, None
        if len(namesakes) > 1:
            self.gap("unknown", f"{len(namesakes)} suppliers' codes give the BC vendor No. "
                     "of the PO's supplier", "supplier_code_not_unique")
            return False, None
        return True, vendor

    def vendor_filter(self, source: Node, entity_set: str, nodes: list[Node]) -> list[Node]:
        """Keep BC purchase documents of the PO's own vendor; a document of another vendor
        that merely shares the number is dropped and reported; an unverifiable vendor keeps
        the documents with a gap."""
        if not nodes:
            return nodes
        self.cite(K.BC_IDENTITY_CITE)
        resolved, vendor = self.po_vendor(source)
        if not resolved or vendor is None:
            self.unverified.add(entity_set)
            self.gap("unknown", f"vendor of bc:{entity_set} matches cannot be checked",
                     "bc_vendor_unverifiable")
            return nodes
        # BC Code fields are upper-cased and compared ignoring case.
        keep = [n for n in nodes
                if str(n.row.get("vendorNumber") or "").strip().casefold() == vendor.casefold()]
        if len(keep) < len(nodes):
            self.gap("unknown", f"{len(nodes) - len(keep)} bc:{entity_set} records share the "
                     "number but belong to another vendor", "bc_vendor_differs")
        return keep

    def bc_lines(self, node: Node) -> list[dict[str, Any]]:
        assert self.bc is not None and node.record is not None
        try:
            records, complete = self.bc.lines(node.record)
        except ReadFailed as exc:
            self.gap("inaccessible", node.relation + " lines", "read_failed_" + _token(exc.reason))
            return []
        if not complete:
            self.gap("unknown", node.relation + " lines", "bc_read_incomplete")
        self._saw(records)
        return records

    def lookup(self, target: str, field_name: str, value: Any) -> list[Node]:
        if target.startswith("bc:"):
            return self.bc_find(target[3:], field_name, value)
        return self.portal_lookup(target, field_name, value)

    # ----------------------------------------------------------------- shapes

    def key(self, node: Node) -> dict[str, Any]:
        if node.is_bc:
            record = node.record or {}
            return {"company_id": str(record.get("company_id")), "id": str(record.get("id"))}
        return self.portal.key_of(node.relation, node.row)

    def object_type(self, node: Node) -> str:
        if node.is_bc:
            return K.BC_OBJECTS[node.relation[3:]][0]
        return K.RELATIONS[node.relation].object_type

    def chain_relation(self, node: Node) -> str:
        return K.BC_OBJECTS[node.relation[3:]][1] if node.is_bc else node.relation

    def handle(self, node: Node) -> str:
        if node.is_bc:
            return str((node.record or {}).get("citation"))
        return (f"api://{node.relation}/{quote(canonical(self.key(node)), safe='')}"
                f"@{self.generation}")

    def citations(self, nodes: Iterable[Node], column: str | Callable[[Node], str],
                  limit: int = MAX_CITATIONS) -> list[dict[str, str]]:
        """Citations of ``nodes`` (at most ``limit``); ``column`` names the cited column, or
        picks it per row (a derived value cites each row's own source column)."""
        out = []
        for node in nodes:
            name = column(node) if callable(column) else column
            entry = {"l1_handle": self.handle(node), "field_path": f"row.{name}"}
            if entry not in out:
                out.append(entry)
            if len(out) == limit:
                break
        return out

    def check_fresh(self, nodes: Iterable[Node]) -> None:
        """Rows changed after ``as_known_at.time`` make the answer stale, not wrong."""
        if self.cutoff is None:
            return
        changed = 0
        for node in nodes:
            if node.is_bc:
                moment = instant((node.record or {}).get("last_modified"))
            else:
                column = K.RELATIONS[node.relation].changed_at
                moment = instant(node.row.get(column)) if column else None
            if moment is not None and moment > self.cutoff:
                changed += 1
        if changed:
            self.gap("stale", f"{changed} cited records changed after as_known_at",
                     "changed_after_as_known_at")


def _token(text: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", text.lower())[:40] or "error"


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# ----------------------------------------------------------------- roots

def resolve_root(ctx: Context, root: Mapping[str, Any], *, many_ok: bool = False) -> list[Node]:
    kind = root.get("identifier_kind")
    identifier = root.get("identifier")
    if kind not in K.ROOT_KEYS or not isinstance(identifier, str) or not identifier:
        ctx.gap("unknown", f"identifier kind {kind}", "identifier_kind_unsupported")
        return []
    ctx.cite(K.ROOT_CITE[kind])
    found: list[Node] = []
    for target, column in K.ROOT_KEYS[kind]:
        found = ctx.lookup(target, column, identifier)
        if found:
            break
    if not found:
        ctx.gap("absent_in_source", f"{K.KIND_LABEL[kind]} {identifier}", "root_not_found")
        return []
    if len(found) > 1 and not many_ok:
        ctx.gap("unknown", f"{len(found)} records carry {identifier}", "ambiguous_root")
    pinned_relation, pinned_key = root.get("relation"), root.get("key")
    if isinstance(pinned_relation, str) and isinstance(pinned_key, Mapping):
        if not any(ctx.chain_relation(n) == pinned_relation and ctx.key(n) == dict(pinned_key)
                   for n in found):
            ctx.gap("stale", f"{identifier} no longer names the pinned record", "root_moved")
    return found


# ----------------------------------------------------------------- F1 chain

def walk(ctx: Context, roots: Sequence[Node]) -> tuple[list[dict[str, Any]], list[Node]]:
    """Breadth-first over ``keys.EDGES`` from the roots; terminal targets are not
    expanded. Returns the chain entries and their nodes (same order)."""
    chain: list[dict[str, Any]] = []
    nodes: list[Node] = []
    seen: dict[tuple[str, str], int] = {}
    queue: list[tuple[Node, int | None, str | None, int, bool]] = [
        (node, None, None, 0, False) for node in roots]
    truncated = False
    while queue:
        node, parent, edge_name, depth, terminal = queue.pop(0)
        identity = (node.relation, canonical(ctx.key(node)))
        if identity in seen:
            continue
        if len(chain) >= MAX_CHAIN:
            truncated = True
            break
        seen[identity] = len(chain)
        entry: dict[str, Any] = {"object_type": ctx.object_type(node),
                                 "relation": ctx.chain_relation(node), "key": ctx.key(node)}
        if parent is not None:
            entry["hop_from"] = parent
            entry["fk"] = edge_name
        chain.append(entry)
        nodes.append(node)
        if terminal or depth >= MAX_DEPTH:
            continue
        index = len(chain) - 1
        for edge in K.EDGES:
            if edge.source != node.relation:
                continue
            value = field_value(node.row, edge.source_field)
            if key_text(value) is None:
                continue
            if edge.target in BC_VENDOR_SETS:
                entity_set = edge.target[3:]
                targets = ctx.vendor_filter(node, entity_set, ctx.bc_find(
                    entity_set, edge.target_field, value, check_span=False))
                ctx.check_span(entity_set, targets)
            else:
                targets = ctx.lookup(edge.target, edge.target_field, value)
            if targets:
                ctx.cite(edge.cite)
            for target in sorted(targets, key=lambda n: canonical(ctx.key(n))):
                queue.append((target, index, edge.name, depth + 1, edge.terminal))
    if truncated:
        ctx.gap("unknown", f"chain capped at {MAX_CHAIN} records", "chain_truncated")
    return chain, nodes


def _returns(edge: K.Edge) -> bool:
    """Whether a hop out of a BC document set leads back to the record that found the
    document: it reverses (same key pair) every edge that finds documents of that set."""
    found_by = [e for e in K.EDGES if e.target == edge.source]
    return bool(found_by) and all((e.source, e.source_field, e.target_field)
                                  == (edge.target, edge.target_field, edge.source_field)
                                  for e in found_by)


def _object_type(node: str) -> str | None:
    if node.startswith("bc:"):
        objects = K.BC_OBJECTS.get(node[3:])
        return objects[0] if objects else None
    relation = K.RELATIONS.get(node)
    return relation.object_type if relation else None


def _unread_types(subject: str) -> set[str]:
    """Object types a failed read of ``subject`` (a Portal relation or ``bc:<entitySet>``)
    may have hidden: its own and those its records lead to, in either source. A hop into
    BC is followed (a receipt's vendor invoice number finds BC purchase invoices that no
    other record of the walk reaches); a hop out of a BC document set is not when it only
    leads back to the record that found the document (``_returns``)."""
    own = _object_type(subject)
    if own is None:
        return set()
    seen, queue, types = {subject}, [subject], {own}
    while queue:
        node = queue.pop()
        for edge in K.EDGES:
            if edge.source != node or (node.startswith("bc:") and _returns(edge)):
                continue
            target = _object_type(edge.target)
            if target is None:
                continue
            types.add(target)
            if not edge.terminal and edge.target not in seen:
                seen.add(edge.target)
                queue.append(edge.target)
    return types


def f1_chain(ctx: Context, root: Mapping[str, Any]) -> dict[str, Any]:
    roots = resolve_root(ctx, root)
    if not roots:
        return _result(ctx, "blocked", [], [])
    chain, nodes = walk(ctx, roots)
    ctx.check_fresh(nodes)
    counts: dict[str, int] = {}
    for entry in chain:
        counts[entry["object_type"]] = counts.get(entry["object_type"], 0) + 1
    values: list[dict[str, Any]] = [{"name": "root_record_count", "value": len(roots)}]
    # A capped walk found a lower bound of every type; a source it could not read, a lower
    # bound of the types that source holds or leads to: the count found, typed ``bound:
    # lower`` (never exact; the scorer scores a count below it wrong and leaves any other
    # undecided, evals/score.py). Documents kept without their
    # vendor check may include another vendor's: no count. Every other count is exact.
    unchecked = {K.BC_OBJECTS[entity_set][0] for entity_set in ctx.unverified}
    lower: set[str] = set()
    for g in ctx.gaps:
        if g["reason"] == "chain_truncated":
            lower.update(counts)
        elif g["state"] == "inaccessible":
            lower.update(_unread_types(g["subject"]) or counts)  # unknown subject: all
    for object_type in sorted(counts):
        value: dict[str, Any] = {"name": f"{_snake(object_type)}_count"[:64],
                                 "object_type": object_type,
                                 "value": None if object_type in unchecked
                                 else counts[object_type]}
        if object_type in lower and object_type not in unchecked:
            value["bound"] = "lower"
        values.append(value)
    return _result(ctx, None, values, chain)


# ----------------------------------------------------------------- F2 customer-PO status

def f2_customer_po_status(ctx: Context, root: Mapping[str, Any]) -> dict[str, Any]:
    if root.get("identifier_kind") != "customer_po":
        ctx.gap("unknown", "F2 roots are customer POs", "family_root_unsupported")
        return _result(ctx, "blocked", [], [])
    orders = resolve_root(ctx, root, many_ok=True)
    if not orders:
        return _result(ctx, "blocked", [], [])
    orders.sort(key=lambda n: (str(n.row.get("order_number")), canonical(ctx.key(n))))
    ctx.cite(K.SO_STATUS_CITE)
    ctx.cite(K.SHIPPED_CITE)
    values: list[dict[str, Any]] = [{"name": "order_count", "object_type": "SalesOrder",
                                     "value": len(orders)}]
    chain = [{"object_type": "SalesOrder", "relation": K.SO, "key": ctx.key(n)} for n in orders]
    touched = list(orders)
    # Shipments that could not be read are unknown, never none (a failed read is not absence).
    lost = ctx.portal.failure(K.SHIPMENT) is not None
    for i, order in enumerate(orders, 1):
        suffix = "" if len(orders) == 1 else f"_{i}"
        cite = lambda column, n=order: ctx.citations([n], column)  # noqa: E731
        shipments = [s for s in ctx.portal_lookup(K.SHIPMENT, "sales_order_id",
                                                  order.row.get("id"))
                     if K.shipped(s.row.get("status"))]
        touched.extend(shipments)
        statuses = sorted({str(s.row.get("status")) for s in shipments
                           if s.row.get("status") is not None})
        values += [
            {"name": "order_number" + suffix, "object_type": "SalesOrder",
             "property": "order_number", "value": key_text(order.row.get("order_number")),
             "citations": cite("order_number")},
            {"name": "order_status" + suffix, "object_type": "SalesOrder", "property": "status",
             "value": K.so_status(order.row.get("status")), "citations": cite("status")},
            {"name": "required_date" + suffix, "object_type": "SalesOrder",
             "property": "required_date", "value": key_text(order.row.get("required_date")),
             "citations": cite("required_date")},
            {"name": "shipment_count" + suffix, "object_type": "Shipment",
             "value": None if lost else len(shipments)},
            {"name": "shipment_statuses" + suffix, "object_type": "Shipment",
             "property": "status", "value": ",".join(statuses) if statuses else None,
             "citations": ctx.citations(shipments, "status")},
        ]
        for value in values[-5:]:
            if value["value"] is None and not (lost and value["object_type"] == "Shipment"):
                ctx.gap("absent_in_source", f"{value['name']} of order {i}",
                        "field_empty" if value["name"] != "shipment_statuses" + suffix
                        else "no_shipment_rows")
        for shipment in shipments:
            chain.append({"object_type": "Shipment", "relation": K.SHIPMENT,
                          "key": ctx.key(shipment), "hop_from": i - 1, "fk": "so.shipments"})
    sage = ctx.portal_lookup(K.SAGE_ORDER, "ponumber", root.get("identifier"))
    if not any(g["subject"] == K.SAGE_ORDER for g in ctx.gaps):
        ctx.cite(f"{K.PACK}#3.3 sage.oe_order_header.ponumber (exact)")
        values.append({"name": "sage_order_count", "object_type": "SageOrder",
                       "value": len(sage)})
    ctx.check_fresh(touched)
    return _result(ctx, None, values, chain, keep=("order_count", "sage_order_count"))


# ----------------------------------------------------------------- F3 quantities

def _groups(nodes: Sequence[Node], unit_column: str, qty_column: str
            ) -> list[tuple[str | None, Decimal | None, list[Node]]]:
    """(unit, exact sum or None when any quantity is empty, rows) per unit, sorted."""
    grouped: dict[str | None, list[Node]] = {}
    for node in nodes:
        unit = node.row.get(unit_column)
        grouped.setdefault(unit.strip() if isinstance(unit, str) and unit.strip() else None,
                           []).append(node)
    out = []
    for unit in sorted(grouped, key=lambda u: (u is None, u or "")):
        rows = grouped[unit]
        amounts = [decimal_of(n.row.get(qty_column)) for n in rows]
        total = None if any(a is None for a in amounts) else sum(amounts, Decimal(0))
        out.append((unit, total, rows))
    return out


def _quantity_values(ctx: Context, base: str, role: str, object_type: str, prop: str,
                     groups: list[tuple[str | None, Decimal | None, list[Node]]],
                     empty_state: str | None, empty_reason: str,
                     cited: Callable[[Node], str] | None = None) -> list[dict[str, Any]]:
    """The values of one quantity role, per unit; none: one null value and its gap (no
    ``empty_state``: a gap the caller already gave it)."""
    if not groups:
        if empty_state is not None:
            ctx.gap(empty_state, f"{role} quantity", empty_reason, base)
        return [{"name": base, "object_type": object_type, "property": prop, "value": None,
                 "quantity_role": role}]
    values = []
    for i, (unit, total, rows) in enumerate(groups, 1):
        name = base if len(groups) == 1 else f"{base}_{i}"
        value: dict[str, Any] = {"name": name, "object_type": object_type, "property": prop,
                                 "value": decimal_text(total) if total is not None else None,
                                 "quantity_role": role,
                                 "citations": ctx.citations(rows, cited or prop)}
        if unit is not None:
            value["unit"] = unit[:40]
        else:
            ctx.gap("absent_in_source", f"unit of {name}", "unit_empty", name)
        if total is None:
            ctx.gap("absent_in_source", f"{name}: a row has no {prop}", f"{prop}_empty"[:64],
                    name)
        values.append(value)
    return values


def produced_groups(ctx: Context, assemblies: Sequence[Node]
                    ) -> tuple[list[tuple[str | None, Decimal | None, list[Node]]],
                               list[tuple[Node, int, Node | None]]]:
    """Produced quantity per unit by the Portal's qty_manufactured chain (``K.PRODUCED_CITE``):
    per AO, the sum of its FM status entries' ``qtymanuf`` (SQL SUM: empty entries are
    skipped, no entry with a value means no sum), else its ``yield_actual``. Returns the
    groups (cited rows: the status entries or the AO) and the FM records read, as
    ``(node, position of its AO in assemblies, its FM assembly order or None)``."""
    per_unit: dict[str | None, list[tuple[Decimal | None, list[Node]]]] = {}
    links: list[tuple[Node, int, Node | None]] = []
    for position, assembly in enumerate(assemblies):
        unit = assembly.row.get("uom")
        unit = unit.strip() if isinstance(unit, str) and unit.strip() else None
        fm = ctx.portal_lookup(K.FM_AO, "nameamo_c", assembly.row.get("mo_code"))
        entries = []
        for node in fm:
            links.append((node, position, None))
            for entry in ctx.portal_lookup(K.FM_AO_STATUS, "idassembly", node.row.get("id")):
                links.append((entry, position, node))
                entries.append(entry)
        counted = [e for e in entries if decimal_of(e.row.get("qtymanuf")) is not None]
        if len(fm) > 1:
            ctx.gap("unknown", f"{len(fm)} FM assembly orders carry one AO code",
                    "fm_assembly_not_unique")
            amount, rows = None, [assembly]
        elif counted:
            amount = sum((decimal_of(e.row.get("qtymanuf")) for e in counted), Decimal(0))
            rows = counted
        else:  # an AO with neither leaves the sum empty (gap qty_manufactured_empty)
            amount, rows = decimal_of(assembly.row.get("yield_actual")), [assembly]
        per_unit.setdefault(unit, []).append((amount, rows))
    groups = []
    for unit in sorted(per_unit, key=lambda u: (u is None, u or "")):
        parts = per_unit[unit]
        total = None if any(a is None for a, _ in parts) else sum(
            (a for a, _ in parts), Decimal(0))
        groups.append((unit, total, [n for _, rows in parts for n in rows]))
    return groups, links


def f3_quantities(ctx: Context, root: Mapping[str, Any]) -> dict[str, Any]:
    if root.get("identifier_kind") != "so_number":
        ctx.gap("unknown", "F3 roots are sales orders", "family_root_unsupported")
        return _result(ctx, "blocked", [], [])
    orders = resolve_root(ctx, root)
    if len(orders) != 1:
        return _result(ctx, "blocked" if not orders else "partial", [], [])
    order = orders[0]
    lines = ctx.portal_lookup(K.SO_LINE, "sales_order_id", order.row.get("id"))
    assemblies = [m for line in lines
                  for m in ctx.portal_lookup(K.MO, "sales_order_line_id", line.row.get("id"))
                  if m.row.get("deleted_at") in (None, "")
                  and K.assembly_status(m.row.get("status")) != "Cancelled"]
    shipments, shipment_lines, unlined = shipped_lines(ctx, order)
    for cite in (K.ORDERED_CITE, K.PRODUCED_CITE, K.SHIPPED_CITE):
        ctx.cite(cite)
    for edge in K.EDGES:
        if edge.name in ("so.lines", "so_line.assembly", "so.shipments", "shipment.lines",
                         "assembly.fm_assembly"):
            ctx.cite(edge.cite)
    ctx.cite(K.STATUS_ENTRY_CITE)
    values = _quantity_values(ctx, "ordered", "ordered", "SalesOrderLine", "quantity",
                              _groups(lines, "uom", "quantity"), "absent_in_source",
                              "no_order_lines")
    produced, fm_links = produced_groups(ctx, assemblies)
    # The FM ETL never sets sales_order_line_id (03 §3.8), so an order without a linked AO
    # says nothing about production: not ingested, not absent (oracle-f3-definitions).
    produced_values = _quantity_values(
        ctx, "produced", "produced", "AssemblyOrder", "qty_manufactured", produced,
        "not_ingested", "no_linked_assembly_order",
        cited=lambda n: "qtymanuf" if n.relation == K.FM_AO_STATUS else "yield_actual")
    for value in produced_values:
        value["basis"] = "derived"
    values += produced_values
    shipped_groups = _groups(shipment_lines, "uom", "quantity")
    # these gaps leave the shipped value null (several units: several values, none named)
    unlined_gaps(ctx, shipments, shipment_lines, unlined,
                 "shipped" if len(shipped_groups) <= 1 else None)
    # One state per value: shipped headers none of which has lines are the shipped value's
    # only gap (absent_in_source), never also "no shipment rows" (directive c1ff4b736a47).
    shipped = _quantity_values(ctx, "shipped", "shipped", "ShipmentLine", "quantity",
                               shipped_groups, None if shipments else "not_ingested",
                               "no_shipment_rows")
    if unlined and shipment_lines:
        # Some shipped headers have lines, others none: the lined sum is a lower bound,
        # never the shipped quantity (Dot finding oracle-f3-partial-shipment-lines).
        for value in shipped:
            value["value"] = None
    values += shipped
    chain = [{"object_type": "SalesOrder", "relation": K.SO, "key": ctx.key(order)}]
    position: dict[str, int] = {}

    def link(node: Node, parent: int, edge_name: str) -> None:
        position[canonical([node.relation, ctx.key(node)])] = len(chain)
        chain.append({"object_type": ctx.object_type(node), "relation": node.relation,
                      "key": ctx.key(node), "hop_from": parent, "fk": edge_name})

    for line in lines:
        link(line, 0, "so.lines")
    assembly_at = []
    for assembly in assemblies:
        parent = next((position[canonical([n.relation, ctx.key(n)])] for n in lines
                       if key_text(n.row.get("id")) ==
                       key_text(assembly.row.get("sales_order_line_id"))), 0)
        assembly_at.append(len(chain))
        link(assembly, parent, "so_line.assembly")
    fm_at: dict[int, int] = {}
    for node, owner, fm_node in fm_links:
        if fm_node is None:
            fm_at[id(node)] = len(chain)
            link(node, assembly_at[owner], "assembly.fm_assembly")
        else:
            link(node, fm_at[id(fm_node)], K.STATUS_ENTRY_EDGE)
    for shipment in shipments:
        link(shipment, 0, "so.shipments")
    ctx.check_fresh([order, *lines, *assemblies, *(n for n, _, _ in fm_links), *shipments,
                     *shipment_lines])
    return _result(ctx, None, values, chain)


def shipped_lines(ctx: Context, order: Node) -> tuple[list[Node], list[Node], list[Node]]:
    """(qualifying shipments, their lines, the shipments without any line) of a sales
    order. A shipped header without lines is a gap, never a zero: when every shipment
    lacks lines the shipped quantity has no rows at all; when only some do, each one is
    named, so a sum over the others is not certified."""
    shipments = [s for s in ctx.portal_lookup(K.SHIPMENT, "sales_order_id", order.row.get("id"))
                 if K.shipped(s.row.get("status"))]
    lines: list[Node] = []
    unlined: list[Node] = []
    for shipment in shipments:
        found = ctx.portal_lookup(K.SHIPMENT_LINE, "shipment_id", shipment.row.get("id"))
        lines.extend(found)
        if not found:
            unlined.append(shipment)
    return shipments, lines, unlined


def unlined_gaps(ctx: Context, shipments: Sequence[Node], lines: Sequence[Node],
                 unlined: Sequence[Node], concerns: str | None = None) -> None:
    if shipments and not lines:
        ctx.gap("absent_in_source", "shipped quantity", "shipment_without_lines", concerns)
    elif unlined:
        for shipment in unlined:
            label = key_text(shipment.row.get("shipment_code")) or canonical(ctx.key(shipment))
            ctx.gap("absent_in_source", f"shipped quantity of shipment {label}",
                    "shipment_without_lines", concerns)


# ----------------------------------------------------------------- F4 three-way match

def po_invoices(ctx: Context, order: Node, receipts: Sequence[Node], receipt_hop: int
                ) -> tuple[dict[tuple[str, str], Node], dict[tuple[str, str], tuple[int, str]]]:
    """The BC purchase invoices of a purchase order, found the way Portal finds them
    (03 §3.11 fetchPurchaseOrderFinance: by BC orderNumber and by the vendor invoice Nos.
    on the PO's receipts), kept only for the PO's own vendor and checked for a company
    span. Returns invoices by (company, id) and, per invoice, the chain position it hangs
    from (0 = the PO; ``receipt_hop + n`` = the n-th receipt) and the edge that found it."""
    invoices: dict[tuple[str, str], Node] = {}
    found_by: dict[tuple[str, str], tuple[int, str]] = {}
    searches = [(0, "po.bc_purchase_invoice", order.row.get("po_number"))] + [
        (receipt_hop + n, "receipt.bc_purchase_invoice", r.row.get("vendor_invoice_number"))
        for n, r in enumerate(receipts)]
    edges = {e.name: e for e in K.EDGES}
    for parent, edge_name, value in searches:
        if key_text(value) is None:
            continue
        edge = edges[edge_name]
        nodes = ctx.bc_find("purchaseInvoices", edge.target_field, value, check_span=False)
        if nodes:
            ctx.cite(edge.cite)
            ctx.cite(K.NUMBER_TRIM_CITE)
        for node in nodes:
            record = node.record or {}
            identity = (str(record.get("company_id")), str(record.get("id")))
            invoices.setdefault(identity, node)
            found_by.setdefault(identity, (parent, edge_name))
    kept = ctx.vendor_filter(order, "purchaseInvoices", list(invoices.values()))
    ctx.check_span("purchaseInvoices", kept)
    kept_ids = {id(n) for n in kept}
    return {k: n for k, n in invoices.items() if id(n) in kept_ids}, found_by


def f4_three_way_match(ctx: Context, root: Mapping[str, Any]) -> dict[str, Any]:
    if root.get("identifier_kind") != "po_number":
        ctx.gap("unknown", "F4 roots are purchase orders", "family_root_unsupported")
        return _result(ctx, "blocked", [], [])
    orders = resolve_root(ctx, root)
    if len(orders) != 1:
        return _result(ctx, "blocked" if not orders else "partial", [], [])
    order = orders[0]
    for cite in (K.PO_ORDERED_CITE, K.RECEIVED_CITE, K.INVOICED_CITE, K.UOM_CITE):
        ctx.cite(cite)
    lines = ctx.portal_lookup(K.PO_LINE, "purchase_order_id", order.row.get("id"))
    receipts = ctx.portal_lookup(K.RECEIPT, "purchase_order_id", order.row.get("id"))
    receipt_lines = [line for r in receipts
                     for line in ctx.portal_lookup(K.RECEIPT_LINE, "receipt_id", r.row.get("id"))]

    def code(node: Node) -> str | None:
        products = ctx.portal_lookup(K.PRODUCT, "id", node.row.get("product_id"))
        return key_text(products[0].row.get("code")) if len(products) == 1 else None

    edges = {e.name: e for e in K.EDGES}
    for name in ("po.lines", "po.receipts", "receipt.lines"):
        ctx.cite(edges[name].cite)
    invoices, found_by = po_invoices(ctx, order, receipts, 1 + len(lines))
    invoice_lines: list[Node] = []
    for node in invoices.values():
        for line in ctx.bc_lines(node):
            if line["fields"].get("lineType") == "Item":
                invoice_lines.append(Node("bc:purchaseInvoiceLines", line["fields"], line))

    items: dict[str | None, dict[str, list[Node]]] = {}
    for kind, nodes in (("ordered", lines), ("received", receipt_lines)):
        for node in nodes:
            items.setdefault(code(node), {}).setdefault(kind, []).append(node)
    for node in invoice_lines:
        items.setdefault(key_text(node.row.get("lineObjectNumber")), {}).setdefault(
            "invoiced", []).append(node)
    if None in items:
        ctx.gap("unknown", "a line without a resolvable item code", "item_code_unresolved")

    values: list[dict[str, Any]] = []
    verdicts: list[bool | None] = []
    for n, item in enumerate(sorted(k for k in items if k is not None), 1):
        legs = items[item]
        values.append({"name": f"item_{n}", "object_type": "Product", "property": "code",
                       "value": item})
        amounts: dict[str, tuple[Decimal | None, str | None]] = {}
        cells: list[list[dict[str, str]]] = []
        for kind, prop, object_type, role, unit_field in (
                ("ordered", "quantity", "PurchaseOrderLine", "ordered", "uom"),
                ("received", "quantity", "ReceiptLine", "received", "uom"),
                ("invoiced", "quantity", "BcPurchaseInvoiceLine", None, "unitOfMeasureCode")):
            rows = legs.get(kind, [])
            groups = _groups(rows, unit_field, prop)
            # a leg cites every cell it sums, as the verdict does (never cut to MAX_CITATIONS)
            cites = ctx.citations(rows, prop, MAX_VERDICT_CELLS + 1)
            value: dict[str, Any] = {"name": f"{kind}_{n}", "object_type": object_type,
                                     "property": prop, "value": None}
            if role:
                value["quantity_role"] = role
            if len(cites) > MAX_VERDICT_CELLS:
                ctx.gap("unknown", f"{kind} quantity of item {n} sums more than "
                        f"{MAX_VERDICT_CELLS} cells", "value_cells_over_cap", f"{kind}_{n}")
            elif len(groups) == 1 and groups[0][1] is not None:
                unit, total, _ = groups[0]
                value["value"] = decimal_text(total)
                if unit:
                    value["unit"] = unit[:40]
                    bc_unit = unit if kind == "invoiced" else K.bc_unit_code(unit)
                    amounts[kind] = (total, bc_unit)
                else:
                    # A quantity without a unit cannot be compared (same rule as F3):
                    # the leg is not matched, so the verdict stays unknown.
                    ctx.gap("unknown", f"{kind} quantity of item {n}", "unit_missing",
                            f"{kind}_{n}")
            elif not rows:
                reason = {"ordered": "no_order_line", "received": "no_receipt_line",
                          "invoiced": "no_invoice_line"}[kind]
                if kind != "invoiced" or ctx.bc is not None:
                    ctx.gap("absent_in_source", f"{kind} quantity of item {n}", reason,
                            f"{kind}_{n}")
            else:
                ctx.gap("unknown", f"{kind} quantity of item {n}",
                        "mixed_units" if len(groups) > 1 else "quantity_empty", f"{kind}_{n}")
            if rows:
                cells.append(cites)
                if len(cites) <= MAX_VERDICT_CELLS:
                    value["citations"] = cites
            values.append(value)
        if len(amounts) < 3:
            verdict = None
        elif len({unit for _, unit in amounts.values()}) != 1:
            verdict = None
            ctx.gap("unknown", f"units of item {n} differ across documents", "unit_differs",
                    f"match_{n}")
        else:
            verdict = len({total for total, _ in amounts.values()}) == 1
        # the cells it was decided on, each leg in turn: an ``equal`` over them matches it.
        # All of them are required; more than one calculation states leave it unknown.
        decided = [cite for turn in zip_longest(*cells) for cite in turn if cite is not None]
        if verdict is not None and len(decided) > MAX_VERDICT_CELLS:
            verdict = None
            ctx.gap("unknown", f"item {n} was decided on more than {MAX_VERDICT_CELLS} cells",
                    "verdict_cells_over_cap", f"match_{n}")
        verdicts.append(verdict)
        match: dict[str, Any] = {"name": f"match_{n}", "value": verdict}
        if verdict is not None:
            match["citations"] = decided
        values.append(match)
    overall = (False if False in verdicts else
               (True if verdicts and all(v is True for v in verdicts) else None))
    if not items:
        ctx.gap("absent_in_source", "purchase order lines", "no_order_lines")
    values.append({"name": "three_way_match", "value": overall})
    chain = [{"object_type": "PurchaseOrder", "relation": K.PO, "key": ctx.key(order)}]
    for node, edge_name in ([(n, "po.lines") for n in lines] +
                            [(n, "po.receipts") for n in receipts]):
        chain.append({"object_type": ctx.object_type(node), "relation": node.relation,
                      "key": ctx.key(node), "hop_from": 0, "fk": edge_name})
    for identity, node in invoices.items():
        parent, edge_name = found_by[identity]
        chain.append({"object_type": ctx.object_type(node), "relation": ctx.chain_relation(node),
                      "key": ctx.key(node), "hop_from": parent, "fk": edge_name})
    ctx.check_fresh([order, *lines, *receipts, *receipt_lines, *invoices.values(),
                     *invoice_lines])
    return _result(ctx, None, values, chain, keep=("three_way_match",))


# ----------------------------------------------------------------- F5 unmatched invoices

def f5_unmatched_invoices(ctx: Context, root: Mapping[str, Any]) -> dict[str, Any]:
    """Which BC purchase invoices of a purchase order no receipt of that PO is tied to.

    An invoice is matched when some receipt of the PO carries its vendor invoice No.
    (trimmed, exact; the Portal's own receipt-to-invoice key). An invoice without a vendor
    invoice No., or an unmatched invoice while a receipt lacks one, is unknown: that
    receipt may be the one."""
    if root.get("identifier_kind") != "po_number":
        ctx.gap("unknown", "F5 roots are purchase orders", "family_root_unsupported")
        return _result(ctx, "blocked", [], [])
    orders = resolve_root(ctx, root)
    if len(orders) != 1:
        return _result(ctx, "blocked" if not orders else "partial", [], [])
    order = orders[0]
    edges = {e.name: e for e in K.EDGES}
    ctx.cite(edges["po.receipts"].cite)
    ctx.cite(K.RECEIPT_INVOICE_MATCH_CITE)
    receipts = ctx.portal_lookup(K.RECEIPT, "purchase_order_id", order.row.get("id"))
    receipts.sort(key=lambda n: canonical(ctx.key(n)))
    def trimmed(value: Any) -> str:
        return sql_trim(value) if isinstance(value, str) else ""

    numbers = {trimmed(n.row.get("vendor_invoice_number")) for n in receipts} - {""}
    blank = [n for n in receipts if not trimmed(n.row.get("vendor_invoice_number"))]
    # Receipts that could not be read may carry any invoice No.: no invoice is unmatched.
    lost = ctx.portal.failure(K.RECEIPT) is not None
    invoices, found_by = po_invoices(ctx, order, receipts, 1)
    # Invoices are counted only when every search for them was read: BC answered, and the
    # receipts whose vendor invoice Nos. find the rest were read.
    unread = ctx.bc is None or lost or any(
        g["state"] == "inaccessible" and g["subject"] == "bc:purchaseInvoices" for g in ctx.gaps)
    ordered = sorted(invoices.items(), key=lambda kv: (str(kv[1].row.get("number")), kv[0]))
    values: list[dict[str, Any]] = [{"name": "invoice_count",
                                     "object_type": "BcPurchaseInvoice",
                                     "value": None if unread else len(ordered)}]
    verdicts: list[bool | None] = []
    for n, (_, node) in enumerate(ordered, 1):
        own = node.row.get("vendorInvoiceNumber")
        own = trimmed(own)
        if not own:
            verdict = None
            ctx.gap("unknown", f"vendor invoice No. of invoice {n}", "vendor_invoice_number_empty")
        elif own in numbers:
            verdict = False
        elif lost:
            verdict = None
        elif blank:
            verdict = None
            ctx.gap("unknown", f"invoice {n} vs {len(blank)} receipts without a vendor "
                    "invoice No.", "receipt_invoice_number_empty")
        else:
            verdict = True
        verdicts.append(verdict)
        values += [
            {"name": f"invoice_{n}", "object_type": "BcPurchaseInvoice", "property": "number",
             "value": key_text(node.row.get("number")),
             "citations": ctx.citations([node], "number")},
            {"name": f"unmatched_{n}", "object_type": "BcPurchaseInvoice",
             "property": "vendor_invoice_number", "value": verdict,
             "citations": ctx.citations([node], "vendorInvoiceNumber")},
        ]
    count = None if None in verdicts or unread else sum(1 for v in verdicts if v)
    values.append({"name": "unmatched_invoice_count", "object_type": "BcPurchaseInvoice",
                   "value": count})
    chain = [{"object_type": "PurchaseOrder", "relation": K.PO, "key": ctx.key(order)}]
    for node in receipts:
        chain.append({"object_type": ctx.object_type(node), "relation": node.relation,
                      "key": ctx.key(node), "hop_from": 0, "fk": "po.receipts"})
    for identity, node in ordered:
        parent, edge_name = found_by[identity]
        chain.append({"object_type": ctx.object_type(node), "relation": ctx.chain_relation(node),
                      "key": ctx.key(node), "hop_from": parent, "fk": edge_name})
    ctx.check_fresh([order, *receipts, *invoices.values()])
    return _result(ctx, None, values, chain, keep=("invoice_count", "unmatched_invoice_count"))


# ----------------------------------------------------------------- F6 shipped short

def f6_shipped_short(ctx: Context, root: Mapping[str, Any]) -> dict[str, Any]:
    """Did a sales order ship short, and on which items: per item, the shipped quantity
    (lines of shipped shipments) against the ordered quantity, in one unit. A missing
    unit, mixed or different units, an empty quantity, a shipped header without lines or
    no shipment rows leave the item's verdict unknown."""
    if root.get("identifier_kind") != "so_number":
        ctx.gap("unknown", "F6 roots are sales orders", "family_root_unsupported")
        return _result(ctx, "blocked", [], [])
    orders = resolve_root(ctx, root)
    if len(orders) != 1:
        return _result(ctx, "blocked" if not orders else "partial", [], [])
    order = orders[0]
    edges = {e.name: e for e in K.EDGES}
    for name in ("so.lines", "so.shipments", "shipment.lines"):
        ctx.cite(edges[name].cite)
    for cite in (K.ORDERED_CITE, K.SHIPPED_CITE, K.SHIPPED_SHORT_CITE, K.UOM_CITE):
        ctx.cite(cite)
    lines = ctx.portal_lookup(K.SO_LINE, "sales_order_id", order.row.get("id"))
    shipments, shipment_lines, unlined = shipped_lines(ctx, order)
    unlined_gaps(ctx, shipments, shipment_lines, unlined)
    if not shipments:
        ctx.gap("not_ingested", "shipped quantity", "no_shipment_rows")
    shipped_known = bool(shipments) and not unlined

    def code(node: Node) -> str | None:
        products = ctx.portal_lookup(K.PRODUCT, "id", node.row.get("product_id"))
        return key_text(products[0].row.get("code")) if len(products) == 1 else None

    items: dict[str | None, dict[str, list[Node]]] = {}
    for kind, nodes in (("ordered", lines), ("shipped", shipment_lines)):
        for node in nodes:
            items.setdefault(code(node), {}).setdefault(kind, []).append(node)
    if None in items:
        ctx.gap("unknown", "a line without a resolvable item code", "item_code_unresolved")
    if not lines:
        ctx.gap("absent_in_source", "sales order lines", "no_order_lines")

    values: list[dict[str, Any]] = []
    verdicts: list[bool | None] = []
    unresolved_shipped = bool(items.get(None, {}).get("shipped"))
    if unresolved_shipped:
        ctx.gap("unknown", "shipped totals are lower bounds: a shipped line has no resolvable "
                "item", "shipped_item_unresolved")
    for n, item in enumerate(sorted(k for k in items if k is not None), 1):
        legs = items[item]
        values.append({"name": f"item_{n}", "object_type": "Product", "property": "code",
                       "value": item})
        amounts: dict[str, tuple[Decimal, str]] = {}
        for kind, object_type in (("ordered", "SalesOrderLine"), ("shipped", "ShipmentLine")):
            rows = legs.get(kind, [])
            groups = _groups(rows, "uom", "quantity")
            value: dict[str, Any] = {"name": f"{kind}_{n}", "object_type": object_type,
                                     "property": "quantity", "value": None,
                                     "quantity_role": kind}
            if kind == "shipped" and not shipped_known:
                pass  # the gaps above already say why the shipped total is unknown
            elif len(groups) == 1 and groups[0][1] is not None:
                unit, total, _ = groups[0]
                value["value"] = decimal_text(total)
                if unit:
                    value["unit"] = unit[:40]
                    amounts[kind] = (total, str(K.bc_unit_code(unit)))
                else:
                    ctx.gap("unknown", f"{kind} quantity of item {n}", "unit_missing")
            elif not rows:
                if kind == "shipped":
                    value["value"] = "0"  # every shipped shipment has lines; none is this item
                    amounts[kind] = (Decimal(0), "")
                else:
                    ctx.gap("absent_in_source", f"ordered quantity of item {n}", "no_order_line")
            else:
                ctx.gap("unknown", f"{kind} quantity of item {n}",
                        "mixed_units" if len(groups) > 1 else "quantity_empty")
            if rows:
                value["citations"] = ctx.citations(rows, "quantity")
            values.append(value)
        verdict: bool | None = None
        if len(amounts) == 2:
            (ordered, unit_o), (shipped, unit_s) = amounts["ordered"], amounts["shipped"]
            if unit_s and unit_s != unit_o:
                ctx.gap("unknown", f"units of item {n} differ between order and shipment",
                        "unit_differs")
            else:
                verdict = shipped < ordered
        if unresolved_shipped:
            # Shipped lines of unknown items may be this item's: the shipped total is a
            # lower bound. "Not short" still holds; "short" cannot be certified.
            for value in values[-1:]:
                value["value"] = None
            if verdict is True:
                verdict = None
        verdicts.append(verdict)
        values.append({"name": f"short_{n}", "value": verdict})
    # A line whose item does not resolve has no verdict of its own: it may never have
    # shipped, so no count and no "not short" can be certified (only a known short can).
    if None in items:
        verdicts.append(None)
    known = [v for v in verdicts if v is not None]
    values.append({"name": "short_item_count",
                   "value": None if len(known) < len(verdicts) or not verdicts
                   else sum(1 for v in known if v)})
    values.append({"name": "shipped_short", "value": True if True in verdicts else (
        False if verdicts and len(known) == len(verdicts) else None)})
    chain = [{"object_type": "SalesOrder", "relation": K.SO, "key": ctx.key(order)}]
    for node, edge_name in ([(n, "so.lines") for n in lines] +
                            [(n, "so.shipments") for n in shipments]):
        chain.append({"object_type": ctx.object_type(node), "relation": node.relation,
                      "key": ctx.key(node), "hop_from": 0, "fk": edge_name})
    ctx.check_fresh([order, *lines, *shipments, *shipment_lines])
    return _result(ctx, None, values, chain, keep=("short_item_count", "shipped_short"))


# ----------------------------------------------------------------- result

def _result(ctx: Context, answerability: str | None, values: list[dict[str, Any]],
            chain: list[dict[str, Any]], *, keep: Sequence[str] = ()) -> dict[str, Any]:
    """The ``oracle_result``. Values and chain over the item caps are never cut silently:
    the summary values named in ``keep`` (computed over everything) always stay, the rest
    is cut in order and a gap makes the answer partial (Dot finding
    oracle-f4-silent-value-truncation)."""
    if len(values) > MAX_VALUES:
        pinned = [v for v in values if v.get("name") in keep]
        room = MAX_VALUES - len(pinned)
        listed = {id(v) for v in [v for v in values if v.get("name") not in keep][:room]}
        listed |= {id(v) for v in pinned}
        ctx.gap("unknown", f"{len(values) - MAX_VALUES} of {len(values)} per-item values "
                f"not listed (item cap {MAX_VALUES})", "values_truncated")
        values = [v for v in values if id(v) in listed]
    if len(chain) > MAX_CHAIN:
        ctx.gap("unknown", f"chain capped at {MAX_CHAIN} records", "chain_truncated")
        chain = chain[:MAX_CHAIN]
    if answerability is None:
        complete = all(v.get("value") is not None for v in values)
        answerability = "answerable" if complete and not ctx.gaps else "partial"
    cleaned = []
    for value in values:
        entry = {k: v for k, v in value.items() if not (k == "citations" and not v)}
        cleaned.append(entry)
    result: dict[str, Any] = {"answerability": answerability, "values": cleaned,
                              "gaps": list(ctx.gaps[:50])}
    if chain:
        result["chain"] = chain
    return result


@dataclass(frozen=True)
class Oracle:
    family: str
    name: str
    compute: Callable[[Context, Mapping[str, Any]], dict[str, Any]]


ORACLES: dict[str, Oracle] = {o.family: o for o in (
    Oracle("F1", "f1_chain", f1_chain),
    Oracle("F2", "f2_customer_po_status", f2_customer_po_status),
    Oracle("F3", "f3_quantities", f3_quantities),
    Oracle("F4", "f4_three_way_match", f4_three_way_match),
    # Held-out only (10-DECISION 8: two question families with no plan).
    Oracle("F5", "f5_unmatched_invoices", f5_unmatched_invoices),
    Oracle("F6", "f6_shipped_short", f6_shipped_short),
)}


@dataclass
class Answer:
    result: dict[str, Any]
    derived_from: list[str] = field(default_factory=list)
    generations: dict[str, str] = field(default_factory=dict)


def run(family: str, root: Mapping[str, Any], as_known_at: Mapping[str, Any],
        sources: Sources) -> Answer:
    """One oracle answer with the rules it used and the read generations it saw."""
    oracle = ORACLES[family]
    sources.portal.prefetch()
    ctx = Context(sources, as_known_at)
    result = oracle.compute(ctx, root)
    generations = {"portal_api": sources.portal.generation()}
    if ctx.bc_generation():
        generations["bc"] = str(ctx.bc_generation())
    # Uncapped: the generator fits them to the item schema and counts what it cuts.
    return Answer(result, list(ctx.cites) or [K.PACK], generations)


def make_oracle(family: str, sources: Sources
                ) -> Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]:
    """The ``plx-eval-oracle/v1`` callable: ``oracle(root, as_known_at) -> oracle_result``."""
    if family not in ORACLES:
        raise KeyError(f"no oracle for family {family}")
    return lambda root, as_known_at: run(family, root, as_known_at, sources).result


__all__ = ["Answer", "INTERFACE", "ORACLES", "Oracle", "Sources", "make_oracle", "run", "walk"]
