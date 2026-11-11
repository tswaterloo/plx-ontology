"""Open customer order lines linked to the open purchase order lines expected to supply them
(``SalesOrderLine.supplied_by``) and their orders (``SalesOrder.awaits_purchase_order``):
demand pegging, a ``post_project`` step (``operating/build_steps.py`` ``supply_pegging``;
steps run in name order, so after ``reviews``), link id prefix ``PREFIX``, self-contained
(it removes the rows it wrote before and writes them again, so it runs in a full build or
alone on a store in place: the step migration).

No captured source records which purchase order supplies which customer order (BC
special-order and drop-ship fields and Portal/FileMaker order-to-PO references are not in
the capture), so every link is a **lead** with its pegging reason, never firm. The rules
are data (``ontology/schema/pegging.json``):

* Demand: each line of an open sales order (the order's open-rule properties, a closed
  list: any unlisted value is not open), its outstanding quantity (``outstanding_quantity``,
  else ordered less shipped), its need date (the line's ``shipment_date``, else the order's
  ``required_date``) and its item (``for_item``). Lines of twin orders count once (the
  first binding of ``line_preference`` holding lines). Each open component of an open
  assembly order made for the line (``AssemblyOrder.for_order_line`` and its twins, one
  maker binding) is demand too: planned less consumed quantity of the component's item by
  the line's need date.
* Supply: each line of an open purchase order (same rules), ordered less received, due
  on the line's expected receipt date, else the order's expected date.
* Items are one item across systems through firm ``Product.same_record_as`` links, and
  through a lead one only when both ends have that one lead partner (``item_twins``: a BC
  item and a Portal product whose BC company is not known).
* Allocation: per item, demand in need-date order takes supply in due-date order: lines
  due on or before the need date first (``covers_need_date``), then later ones
  (``due_after_need``: the material arrives late). Lines whose units are both known and
  differ are never allocated to each other. Quantities are Decimal.

A line with no item, two items, no date or no quantity is counted and pegs nothing; a
pair a person rejected (``contradicted``) is never linked. Each link cites the cells it
read (features, with the allocated quantity and the dates), its evidence handles are the
lines' versions' and its markings the union of every cited cell's and version's.

Stdlib only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ontology import mentions
from ontology.sender_parties import _digest, _q, _values, _versions

PREFIX = "peg:"
_END = PREFIX[:-1] + chr(ord(PREFIX[-1]) + 1)
RULES_PATH = Path(__file__).resolve().parent / "schema" / "pegging.json"
FIRM = ("verified", "reviewed", "corroborated")
LEAD = "lead"
BATCH = 5000


def rules(path: str | Path = RULES_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _binding(oid: str) -> str:
    parts = oid.split(":", 2)
    return parts[1] if len(parts) > 2 else ""


def _text(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, str):
        held = value.strip()
        if len(held) >= 2 and held[0] == held[-1] == '"':
            held = held[1:-1].strip()
        return held or None
    return None


def _decimal(value: Any) -> Decimal | None:
    held = _text(value)
    if held is None:
        return None
    try:
        out = Decimal(held)
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def _date(value: Any) -> str | None:
    held = _text(value)
    return held[:10] if held and len(held) >= 10 and held[4] == "-" and held[7] == "-" \
        and not held.startswith("0001") else None


class _Cells:
    """oid -> property -> the first current value row (by field path)."""

    def __init__(self, conn: Any, props: Iterable[str], types: Sequence[str]) -> None:
        self.by: dict[str, dict[str, Any]] = defaultdict(dict)
        props = sorted(set(props))
        if not props or not types:
            return
        rows = sorted(_values(conn, props, tuple(t + ":" for t in types)),
                      key=lambda rp: (rp[0]["object_id"], rp[1], rp[0]["field_path"] or ""))
        for row, prop in rows:
            if _text(row["value"]) is not None:
                self.by[row["object_id"]].setdefault(prop, row)

    def get(self, oid: str, prop: str | None) -> Any:
        return self.by.get(oid, {}).get(prop) if prop else None

    def value(self, oid: str, prop: str | None) -> Any:
        row = self.get(oid, prop)
        return None if row is None else row["value"]


def _links(conn: Any, link_type: str, tiers: Sequence[str] | None = None
           ) -> list[tuple[str, str, str]]:
    rows = _q(conn, "SELECT from_object, to_object, tier FROM link_rows WHERE link_type = ? "
                    "AND superseded_at IS NULL", [link_type])
    return [(r[0], r[1], r[2]) for r in rows
            if r[2] != "contradicted" and (tiers is None or r[2] in tiers)]


def _components(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Union-find: member -> component representative (the least id)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    return {x: find(x) for x in list(parent)}


def item_twins(links: Iterable[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """The item twin pairs pegging joins items by: every firm pair, and a lead pair (a BC
    item to a Portal product whose BC company is not known) only when each end has that
    one lead partner (an item two records claim is no one item)."""
    links = list(links)
    partners: dict[str, set[str]] = defaultdict(set)
    for a, b, tier in links:
        if tier == LEAD:
            partners[a].add(b)
            partners[b].add(a)
    return [(a, b) for a, b, tier in links
            if tier in FIRM or (tier == LEAD and len(partners[a]) == 1 and len(partners[b]) == 1)]


def _open(order: str, cells: _Cells, rule: Mapping[str, Sequence[str]]) -> str:
    """``open``, ``closed`` (a held status not in its list) or ``status_unknown``."""
    held = [(p, _text(cells.value(order, p))) for p in rule]
    held = [(p, v) for p, v in held if v is not None]
    if not held:
        return "status_unknown"
    return "open" if all(v in rule[p] for p, v in held) else "closed"


class _Side:
    """The open lines of one side (demand or supply): oid -> (date, quantity, uom, order,
    the cells cited)."""

    def __init__(self, conn: Any, spec: Mapping[str, Any], counts: Counter, tag: str) -> None:
        line_type = spec["line_type"]
        line_of = _links(conn, spec["order_link"], FIRM)
        self.order_of = {}
        for line, order, _tier in line_of:
            self.order_of.setdefault(line, order)
        order_types = sorted({o.split(":", 1)[0] for o in self.order_of.values()})
        line_props = [spec[k] for k in ("quantity", "done_quantity", "outstanding_quantity",
                                        "uom", "line_date") if spec.get(k)]
        self.lines = _Cells(conn, line_props, (line_type,))
        self.orders = _Cells(conn, [spec["order_date"], *spec["open"]], order_types)
        twins = _components((a, b) for a, b, _t in _links(conn, spec["order_twin_link"], FIRM))
        self._twins = twins
        self.item_of: dict[str, str] = {}
        by_component: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for line, order in sorted(self.order_of.items()):
            by_component[twins.get(order, order)][_binding(line)].append(line)
        prefer = list(spec["line_preference"])
        self.open: dict[str, dict[str, Any]] = {}
        for _comp, held in sorted(by_component.items()):
            chosen = next((b for b in prefer if b in held), sorted(held)[0])
            counts[f"{tag}_twin_lines_skipped"] += sum(len(v) for b, v in held.items()
                                                       if b != chosen)
            for line in held[chosen]:
                order = self.order_of[line]
                state = _open(order, self.orders, spec["open"])
                if state != "open":
                    counts[f"{tag}_{state}"] += 1
                    continue
                qty = _decimal(self.lines.value(line, spec.get("outstanding_quantity")))
                if qty is None:
                    whole = _decimal(self.lines.value(line, spec["quantity"]))
                    done = _decimal(self.lines.value(line, spec.get("done_quantity"))) \
                        or Decimal(0)
                    qty = None if whole is None else whole - done
                if qty is None:
                    counts[f"{tag}_quantity_unknown"] += 1
                    continue
                if qty <= 0:
                    counts[f"{tag}_fully_done"] += 1
                    continue
                date_cell = self.lines.get(line, spec.get("line_date")) \
                    if _date(self.lines.value(line, spec.get("line_date"))) else None
                if date_cell is None and _date(self.orders.value(order, spec["order_date"])):
                    date_cell = self.orders.get(order, spec["order_date"])
                if date_cell is None:
                    counts[f"{tag}_no_date"] += 1
                    continue
                uom = _text(self.lines.value(line, spec.get("uom")))
                self.open[line] = {"date": _date(date_cell["value"]), "quantity": qty,
                                   "uom": uom.lower() if uom else None, "order": order,
                                   "cells": [c for c in (date_cell, *(self.lines.get(line, p)
                                                                      for p in line_props))
                                             if c is not None]}
                counts[f"{tag}_open"] += 1


def _root_of(side: "_Side", line: str) -> str:
    order = side.order_of.get(line, line)
    return side._twins.get(order, order)


_Side.root_of = _root_of  # type: ignore[attr-defined]


def _items(conn: Any, link_type: str, holders: Iterable[str], group: Mapping[str, str],
           counts: Counter, tag: str) -> dict[str, str]:
    """holder -> its one item group (a holder naming two item groups names none)."""
    holders = set(holders)
    found: dict[str, set[str]] = defaultdict(set)
    for frm, item, _tier in _links(conn, link_type):
        if frm in holders:
            found[frm].add(group.get(item, item))
    out = {}
    for holder in sorted(holders):
        items = found.get(holder, set())
        if len(items) == 1:
            out[holder] = next(iter(items))
        else:
            counts[f"{tag}_item_{'unknown' if not items else 'ambiguous'}"] += 1
    return out


def _component_demand(conn: Any, spec: Mapping[str, Any], demand: _Side,
                      group: Mapping[str, str], counts: Counter) -> list[dict[str, Any]]:
    """The open components of the open assembly orders made for open demand lines."""
    # each maker path walks from an order line to the assembly orders made for it
    # (``~`` reads a link backwards); a line of a twin order stands for the open line of the
    # same item its order counts by (``_Side``: one binding per twin order)
    reached: dict[str, set[str]] = defaultdict(set)
    for path in spec["maker_paths"]:
        at: dict[str, set[str]] | None = None
        for step in path:
            back = step.startswith("~")
            hops: dict[str, set[str]] = defaultdict(set)
            for frm, to, _tier in _links(conn, step.lstrip("~"), FIRM):
                hops[to if back else frm].add(frm if back else to)
            at = ({k: set(v) for k, v in hops.items()} if at is None else
                  {start: {n for x in held for n in hops.get(x, ())}
                   for start, held in at.items()})
        for line, makers in (at or {}).items():
            reached[line] |= makers
    stands: dict[tuple[str, str], list[str]] = defaultdict(list)
    for line in demand.open:
        stands[(demand.root_of(line), demand.item_of.get(line, ""))].append(line)
    made_for: dict[str, set[str]] = defaultdict(set)
    for line, makers in sorted(reached.items()):
        if line in demand.open:
            made_for[line] |= makers
            continue
        held = stands.get((demand.root_of(line), demand.item_of.get(line, "")), [])
        if len(held) == 1 and demand.item_of.get(line):
            made_for[held[0]] |= makers
            counts["maker_via_twin_line"] += 1
    if not made_for:
        return []
    twins = _components((a, b) for a, b, _t in _links(conn, spec["maker_twin_link"], FIRM))
    members: dict[str, set[str]] = defaultdict(set)
    for x, root in twins.items():
        members[root].add(x)
    comps_of: dict[str, list[str]] = defaultdict(list)
    for comp, maker, _tier in _links(conn, spec["component_link"], FIRM):
        comps_of[maker].append(comp)
    makers = {m for held in made_for.values() for m in held}
    every = makers | {t for m in makers for t in members.get(twins.get(m, m), ())}
    maker_cells = _Cells(conn, list(spec["open"]), sorted({m.split(":", 1)[0] for m in every}))
    all_comps = sorted({c for m in every for c in comps_of.get(m, ())})
    comp_cells = _Cells(conn, [spec["quantity"], spec["done_quantity"]],
                        sorted({c.split(":", 1)[0] for c in all_comps}))
    items = _items(conn, spec["item_link"], all_comps, group, counts, "component")
    prefer = list(spec["maker_preference"])
    out: list[dict[str, Any]] = []
    for line in sorted(made_for):
        seen_roots: set[str] = set()
        for maker in sorted(made_for[line]):
            root = twins.get(maker, maker)
            if root in seen_roots:
                continue
            seen_roots.add(root)
            group_makers = sorted(members.get(root, {maker}) | {maker})
            states = {_open(m, maker_cells, spec["open"]) for m in group_makers}
            if "closed" in states or "open" not in states:
                counts["maker_not_open"] += 1
                continue
            with_comps = [m for m in group_makers if comps_of.get(m)]
            if not with_comps:
                continue
            chosen = next((m for b in prefer for m in with_comps if _binding(m) == b),
                          with_comps[0])
            for comp in sorted(comps_of[chosen]):
                if comp not in items:
                    continue
                whole = _decimal(comp_cells.value(comp, spec["quantity"]))
                done = _decimal(comp_cells.value(comp, spec["done_quantity"])) or Decimal(0)
                if whole is None:
                    counts["component_quantity_unknown"] += 1
                    continue
                if whole - done <= 0:
                    counts["component_fully_done"] += 1
                    continue
                need = demand.open[line]
                out.append({"line": line, "component": comp, "item": items[comp],
                            "date": need["date"], "quantity": whole - done, "uom": None,
                            "cells": [c for c in (comp_cells.get(comp, spec["quantity"]),
                                                  comp_cells.get(comp, spec["done_quantity"]),
                                                  need["cells"][0]) if c is not None]})
                counts["component_open"] += 1
    return out


def allocate(demands: Sequence[Mapping[str, Any]], supplies: Sequence[Mapping[str, Any]]
             ) -> list[tuple[int, int, Decimal, bool]]:
    """(demand index, supply index, quantity, covers need date) per allocation, for the
    demands and supplies of one item: demand in (date, line, component) order takes
    supply in (date, line) order, due on or before the need date first, then later."""
    d_order = sorted(range(len(demands)), key=lambda i: (demands[i]["date"], demands[i]["line"],
                                                         demands[i].get("component") or ""))
    s_order = sorted(range(len(supplies)), key=lambda j: (supplies[j]["date"],
                                                          supplies[j]["line"]))
    left = [Decimal(s["quantity"]) for s in supplies]
    out: list[tuple[int, int, Decimal, bool]] = []
    for i in d_order:
        need = Decimal(demands[i]["quantity"])
        d_uom = demands[i].get("uom")
        for covering in (True, False):
            for j in s_order:
                if need <= 0:
                    break
                if left[j] <= 0 or (supplies[j]["date"] <= demands[i]["date"]) != covering:
                    continue
                s_uom = supplies[j].get("uom")
                if d_uom and s_uom and d_uom != s_uom:
                    continue
                take = min(need, left[j])
                left[j] -= take
                need -= take
                out.append((i, j, take, covering))
    return out


def _cite(row: Any) -> dict[str, Any]:
    return {"l1_handle": row["l1_handle"], "field_path": row["field_path"],
            "digest": row["digest"], "lineage": row["lineage"]}


def peg_links(conn: Any, cfg: Mapping[str, Any] | None = None
              ) -> tuple[list[dict[str, Any]], Counter]:
    """The lead rows (module docstring) and the outcome counts."""
    cfg = cfg or rules()
    counts: Counter[str] = Counter()
    group = _components(item_twins(_links(conn, cfg["item_identity_link"])))
    demand = _Side(conn, cfg["demand"], counts, "demand")
    supply = _Side(conn, cfg["supply"], counts, "supply")
    d_items = _items(conn, cfg["demand"]["item_link"], demand.open, group, counts, "demand")
    demand.item_of = {line: item for line, item in _items(
        conn, cfg["demand"]["item_link"], demand.order_of, group, Counter(), "any").items()}
    s_items = _items(conn, cfg["supply"]["item_link"], supply.open, group, counts, "supply")
    by_item: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = \
        defaultdict(lambda: ([], []))
    for line, held in sorted(demand.open.items()):
        if line in d_items:
            by_item[d_items[line]][0].append({"line": line, "component": None,
                                              "item": d_items[line], **held})
    for comp in _component_demand(conn, cfg["components"], demand, group, counts):
        by_item[comp["item"]][0].append(comp)
    for line, held in sorted(supply.open.items()):
        if line in s_items:
            by_item[s_items[line]][1].append({"line": line, "item": s_items[line], **held})
    pairs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item, (ds, ss) in sorted(by_item.items()):
        if not ds or not ss:
            counts["item_without_" + ("supply" if ds else "demand")] += 1
            continue
        allocated: Counter[int] = Counter()
        for i, j, qty, covering in allocate(ds, ss):
            allocated[i] += 1
            pairs[(ds[i]["line"], ss[j]["line"])].append(
                {"demand": ds[i], "supply": ss[j], "quantity": qty, "covering": covering})
            counts["allocations"] += 1
            counts["allocations_" + ("covering" if covering else "due_after_need")] += 1
        counts["demand_unallocated"] += sum(1 for i in range(len(ds)) if not allocated[i])
    line_type, order_type = cfg["line_link"], cfg["order_link"]
    rejected = {(r[0], r[1], r[2]) for lt in (line_type, order_type) for r in _q(
        conn, "SELECT from_object, to_object, link_type FROM link_rows WHERE link_type = ? AND "
              "tier = 'contradicted'", [lt])}
    ends = {x for pair in pairs for x in pair}
    versions = _versions(conn, ends)
    out: dict[str, dict[str, Any]] = {}
    rolled: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for (d_line, s_line), allocations in sorted(pairs.items()):
        if (d_line, s_line, line_type) in rejected:
            counts["rejected"] += 1
            continue
        dv, sv = versions.get(d_line), versions.get(s_line)
        if dv is None or sv is None:
            counts["line_version_missing"] += 1
            continue
        reason: list[dict[str, Any]] = [{"feature": "same_item", "agrees": True,
                                         "item": allocations[0]["demand"]["item"]}]
        cells: list[Any] = []
        for a in allocations:
            d, s = a["demand"], a["supply"]
            cells += d["cells"] + s["cells"]
            reason.append({
                "feature": "pegged_quantity", "agrees": True,
                "quantity": str(a["quantity"]), "need_date": d["date"], "due_date": s["date"],
                "covers_need_date": a["covering"],
                "pegging": "covers_need_date" if a["covering"] else "due_after_need",
                **({"via_component": d["component"]} if d.get("component") else {}),
                "citations": [_cite(c) for c in d["cells"] + s["cells"]]})
        why_not = [{"feature": "pegged_not_recorded", "agrees": None,
                    "detail": "no source records this tie; allocated by item and date, "
                              "on-hand stock not netted: a lead"}]
        marks = mentions._marks(dv["markings"], sv["markings"],
                                *[m for c in cells for m in (c["vmarks"], c["kmarks"])])
        link_id = PREFIX + _digest(line_type, d_line, s_line)
        row = {"link_id": link_id, "link_type": line_type, "from_object": d_line,
               "to_object": s_line, "tier": LEAD, "binding_kind": mentions.BINDING_KIND,
               "link_contract": None,
               "reason": json.dumps(reason, sort_keys=True, separators=(",", ":")),
               "why_not": json.dumps(why_not, sort_keys=True, separators=(",", ":")),
               "evidence_handles": json.dumps(list(dict.fromkeys(
                   [dv["l1_handle"], sv["l1_handle"], *(c["l1_handle"] for c in cells)])),
                   separators=(",", ":")),
               "reviewed_by": None, "reviewed_at": None, "review_action_id": None,
               "markings": marks,
               "recorded_at": max([dv["recorded_at"] or "", sv["recorded_at"] or "",
                                   *(c["recorded_at"] or "" for c in cells)]),
               "superseded_at": None}
        out[link_id] = row
        d_order = demand.order_of.get(d_line)
        s_order = supply.order_of.get(s_line)
        if d_order and s_order:
            rolled[(d_order, s_order)].append(row)
    o_versions = _versions(conn, {x for pair in rolled for x in pair})
    for (d_order, s_order), rows in sorted(rolled.items()):
        if (d_order, s_order, order_type) in rejected:
            counts["rejected"] += 1
            continue
        dv, sv = o_versions.get(d_order), o_versions.get(s_order)
        if dv is None or sv is None:
            counts["order_version_missing"] += 1
            continue
        reason = [{"feature": "pegged_lines", "agrees": True,
                   "links": sorted(r["link_id"] for r in rows)}]
        link_id = PREFIX + _digest(order_type, d_order, s_order)
        out[link_id] = {
            **rows[0], "link_id": link_id, "link_type": order_type, "from_object": d_order,
            "to_object": s_order,
            "reason": json.dumps(reason, sort_keys=True, separators=(",", ":")),
            "evidence_handles": json.dumps(list(dict.fromkeys(
                [dv["l1_handle"], sv["l1_handle"],
                 *(h for r in rows for h in json.loads(r["evidence_handles"]))])),
                separators=(",", ":")),
            "markings": mentions._marks(dv["markings"], sv["markings"],
                                        *(r["markings"] for r in rows)),
            "recorded_at": max([dv["recorded_at"] or "", sv["recorded_at"] or "",
                                *(r["recorded_at"] for r in rows)])}
    written = Counter(r["link_type"] for r in out.values())
    counts.update({f"links:{k}": v for k, v in written.items()})
    return [out[k] for k in sorted(out)], counts


def _declared(links: Mapping[str, Any], cfg: Mapping[str, Any]) -> bool:
    return any(b.get("kind") == mentions.BINDING_KIND
               for b in (links.get(cfg["line_link"]) or {}).get("bindings", ()))


def supply_pegging_step(builder: Any, projector: Any) -> dict[str, Any]:
    """The ``supply_pegging`` step (module docstring)."""
    cfg = rules()
    conn = builder.connection
    conn.execute("DELETE FROM link_rows WHERE link_id >= ? AND link_id < ?", (PREFIX, _END))
    if not _declared(getattr(projector, "links", {}) or {}, cfg):
        return {"counts": {}, "link_tiers": {}}
    rows, counts = peg_links(conn, cfg)
    for start in range(0, len(rows), BATCH):
        builder.add("links", rows[start:start + BATCH])
    return {"counts": dict(counts), "link_tiers": {LEAD: len(rows)} if rows else {}}


__all__ = ["PREFIX", "RULES_PATH", "allocate", "peg_links", "rules", "supply_pegging_step"]
