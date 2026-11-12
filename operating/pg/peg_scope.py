"""The part of demand pegging a change can move, read from Postgres by index
(``operating.pg.whole_steps``).

Demand pegging (``ontology.demand_pegging``) allocates per item: every open customer order
line of an item takes the item's open purchase order supply in date order. A delta layer
changes a few orders, lines, products or assembly orders, so only the items those touch
are allocated again, by the step's own code (``peg_links``, unchanged), over a connection
whose two whole-table reads see the rows of that part alone (:class:`ScopedConnection`).
The part (:func:`scope`) is closed so that the step computes over it exactly what a full
run computes for its items:

* items (``G``): each with its item group as the step joins them (``item_twins``);
* every line of an item in ``G``, and every line an assembly order using one is made for
  (unless that order is closed: a closed one closes its twin group), their orders and
  those orders' firm same-record twins; of those, the twin groups holding an open order
  (the only ones whose lines the step reads as demand or supply) with every line of each
  (the step's choice of one twin's lines sees every twin), and the assembly orders made
  for those lines with their twins and components;
* a served peg link with an end among the lines of ``G``'s items (or made for an
  assembly order using one) or of an item in ``G`` adds its item
  and its ends' items and orders (a link that goes away frees supply for others of its
  item), until nothing new joins.

Of the step's rows over the part only the line links of ``G``'s items are kept
(:func:`scoped_links`); they replace the served line links of those items. Each order link
(``awaits_purchase_order``) of an order pair one of the changed line links' orders is in
is rolled up again from the line links the store then holds, as the step rolls them up
(:func:`_rolled`). Seeds: the objects of the pegging input types the layers since the
last run replaced, and the served links any of them is an end, an end's order, a cited
component, its assembly order or the item of. A twin link a layer removed whose other end
it does not own is not seen here: the periodic whole-store run (``whole_steps``) restores
that.

Stdlib and psycopg.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

from ontology.pg_step_inplace import SplitConnection, _read_only, pg_form

SCOPE_TABLE = "whole_steps_scope"
# the step's two whole-table reads (``demand_pegging._links``, ``sender_parties._CELLS``),
# answered over the scope's objects; any other read goes to Postgres as it is (whole: a
# superset, never a different answer)
_LINKS_SQL = ("SELECT from_object, to_object, tier FROM link_rows WHERE link_type = ? "
              "AND superseded_at IS NULL")
_CELLS_TAIL = "AND s.superseded_at IS NULL"
_IN_SCOPE = f"(SELECT object_id FROM {SCOPE_TABLE})"
_PG_CELLS_FROM = ("FROM value_cells c JOIN value_sources s ON s.src = c.src JOIN "
                  "object_versions v ON v.rowid = s.ver")
SOURCES_TABLE = SCOPE_TABLE + "_src"
# the scope objects' current value sources, read once per scope (``set_scope``)
_SCOPE_SOURCES = (f"INSERT INTO {SOURCES_TABLE} (ver, src) SELECT v.ver, s.src FROM "
                  f"{SCOPE_TABLE} sc CROSS JOIN LATERAL (SELECT ver FROM object_versions v0 "
                  "WHERE v0.object_id = sc.object_id) v CROSS JOIN LATERAL (SELECT src FROM "
                  "value_sources s0 WHERE s0.ver = v.ver AND s0.superseded_at IS NULL) s")
_SCOPED_CELLS_FROM = (f"FROM {SOURCES_TABLE} ss CROSS JOIN LATERAL (SELECT * FROM "
                      "value_sources s0 WHERE s0.src = ss.src) s CROSS JOIN LATERAL (SELECT * "
                      "FROM object_versions v0 WHERE v0.ver = ss.ver) v CROSS JOIN LATERAL "
                      "(SELECT * FROM value_cells c0 WHERE c0.src = ss.src) c")


class ScopedConnection(SplitConnection):
    """``SplitConnection`` whose whole link-type and current-value reads see the rows of
    the scope's objects only (``set_scope``): a link from one (with an end at one for the
    link types ``both_ends``), a value of one."""

    def __init__(self, local: Any, pg: Any, both_ends: Iterable[str] = ()) -> None:
        super().__init__(local, pg)
        self.both_ends = frozenset(both_ends)
        self.scoped_reads = 0
        raw = pg._pg.cursor()
        raw.execute(f"CREATE TEMP TABLE IF NOT EXISTS {SCOPE_TABLE} "
                    "(object_id text PRIMARY KEY) ON COMMIT DROP")
        raw.execute(f"CREATE TEMP TABLE IF NOT EXISTS {SOURCES_TABLE} "
                    "(ver bigint NOT NULL, src bigint PRIMARY KEY) ON COMMIT DROP")
        self._raw = raw
        self.ends: dict[str, tuple[str, str]] = {}

    def set_scope(self, ids: Iterable[str]) -> None:
        self._raw.execute(f"TRUNCATE {SCOPE_TABLE}")
        with self._raw.copy(f"COPY {SCOPE_TABLE} (object_id) FROM STDIN") as copy:
            for oid in sorted(set(ids)):
                copy.write_row((oid,))
        self._raw.execute(f"ANALYZE {SCOPE_TABLE}")
        self._raw.execute(f"TRUNCATE {SOURCES_TABLE}")
        self._raw.execute(_SCOPE_SOURCES)
        self._raw.execute(f"ANALYZE {SOURCES_TABLE}")

    def _prefix(self, link_type: str, end: str) -> str:
        """The id prefix of a ``link_type`` end's objects ('' when the schema has none)."""
        held = self.ends.get(link_type)
        return f"{held[0 if end == 'from' else 1]}:" if held else ""

    def _route(self, sql: str, params: Any, row_factory: Any = None) -> Any:
        if not _read_only(sql):
            return super()._route(sql, params, row_factory)
        flat = " ".join(sql.split())
        if flat == _LINKS_SQL and tuple(params)[:1] and params[0] not in self.both_ends:
            # the rows from a scope object, by index (the part holds every object whose
            # rows the step reads from: ``scope``); a join the planner may answer by
            # scanning the whole link type
            sql = (f"SELECT l.from_object, l.to_object, l.tier FROM {SCOPE_TABLE} sc CROSS "
                   f"JOIN LATERAL (SELECT from_object, to_object, tier FROM link_rows WHERE "
                   f"from_object = sc.object_id AND link_type = ? AND superseded_at IS NULL) l"
                   f" WHERE starts_with(sc.object_id, ?)")
            params = (params[0], self._prefix(params[0], "from"))
        elif flat == _LINKS_SQL:
            # rows at either end (an item's lead partners outside the part decide whether
            # its lead twins count: ``item_twins``)
            sql = " UNION ALL ".join(
                f"SELECT l.from_object, l.to_object, l.tier FROM {SCOPE_TABLE} sc CROSS "
                f"JOIN LATERAL (SELECT from_object, to_object, tier FROM link_rows WHERE "
                f"{end} = sc.object_id AND link_type = ? AND superseded_at IS NULL) l"
                + extra for end, extra in (
                    ("from_object", ""),
                    ("to_object", f" WHERE l.from_object NOT IN {_IN_SCOPE}")))
            params = tuple(params) * 2
        elif "FROM value_layout_fields lf JOIN value_cells c" in flat \
                and flat.endswith(_CELLS_TAIL):
            # the scope objects' versions, their sources and cells, each by index
            sql = pg_form(flat)
            if _PG_CELLS_FROM not in sql:
                raise ValueError("whole_steps_scope_cells_read_unknown")
            sql = sql.replace(_PG_CELLS_FROM, _SCOPED_CELLS_FROM, 1)
        else:
            return super()._route(sql, params, row_factory)
        self.scoped_reads += 1
        self.reads += 1
        return self.pg.execute(pg_form(sql), params)


def _type(oid: str) -> str:
    return oid.split(":", 1)[0]


class _Graph:
    """Indexed link reads over Postgres (any tier but contradicted, current rows)."""

    def __init__(self, cur: Any) -> None:
        self.cur = cur

    def ends(self, link_type: str, ids: Iterable[str], *, by: str,
             tiers: Sequence[str] | None = None) -> list[tuple[str, str]]:
        """(from, to) of ``link_type`` rows whose ``by`` end (``from``/``to``) is in ids
        (of ``tiers`` only, when given)."""
        ids = sorted(set(ids))
        if not ids:
            return []
        column = {"from": "from_object", "to": "to_object"}[by]
        out: list[tuple[str, str]] = []
        for i in range(0, len(ids), 20_000):
            self.cur.execute(
                f"SELECT from_object, to_object FROM ontology.link_rows WHERE {column} = "
                "ANY(%s) AND link_type = %s AND superseded_at IS NULL AND "
                "tier <> 'contradicted'" + (" AND tier = ANY(%s)" if tiers else ""),
                (ids[i:i + 20_000], link_type, *([list(tiers)] if tiers else [])))
            out += [(a, b) for a, b in self.cur.fetchall()]
        return out

    def tiered(self, link_type: str, ids: Iterable[str]) -> list[tuple[str, str, str]]:
        """(from, to, tier) of ``link_type`` rows with an end in ids."""
        ids = sorted(set(ids))
        out: list[tuple[str, str, str]] = []
        for column in ("from_object", "to_object"):
            for i in range(0, len(ids), 20_000):
                self.cur.execute(
                    f"SELECT from_object, to_object, tier FROM ontology.link_rows WHERE "
                    f"{column} = ANY(%s) AND link_type = %s AND superseded_at IS NULL AND "
                    "tier <> 'contradicted'", (ids[i:i + 20_000], link_type))
                out += [tuple(r) for r in self.cur.fetchall()]
        return out

    def item_group(self, link_type: str, seeds: Iterable[str]) -> set[str]:
        """Every item the step joins to a seed (``demand_pegging.item_twins``): a firm
        pair, or a lead pair whose ends each have that one lead partner."""
        from ontology import demand_pegging as dp
        rows: dict[str, list[tuple[str, str, str]]] = {}

        def fetch(nodes: Iterable[str]) -> None:
            nodes = {x for x in nodes if x not in rows}
            for x in nodes:
                rows[x] = []
            for a, b, tier in set(self.tiered(link_type, nodes)):
                for x in (a, b):
                    if x in nodes:
                        rows[x].append((a, b, tier))

        def partners(x: str) -> set[str]:
            return {b if a == x else a for a, b, tier in rows[x] if tier == dp.LEAD}

        seen = set(seeds)
        frontier = set(seen)
        while frontier:
            fetch(frontier)
            fetch({b if a == x else a for x in frontier for a, b, t in rows[x]
                   if t == dp.LEAD})
            nxt: set[str] = set()
            for x in frontier:
                for a, b, tier in rows[x]:
                    y = b if a == x else a
                    if tier in dp.FIRM or (tier == dp.LEAD and len(partners(x)) == 1
                                           and len(partners(y)) == 1):
                        nxt.add(y)
            frontier = nxt - seen
            seen |= frontier
        return seen

    def targets(self, link_type: str, ids: Iterable[str]) -> set[str]:
        return {b for _a, b in self.ends(link_type, ids, by="from")}

    def sources(self, link_type: str, ids: Iterable[str]) -> set[str]:
        return {a for a, _b in self.ends(link_type, ids, by="to")}

    def closure(self, link_type: str, seeds: Iterable[str]
                ) -> tuple[set[str], list[tuple[str, str]]]:
        """Every object a chain of firm ``link_type`` rows joins to a seed (the step's
        twin groups: ``demand_pegging._components`` over firm rows), and those rows."""
        from ontology.demand_pegging import FIRM
        seen = set(seeds)
        frontier = set(seen)
        rows: set[tuple[str, str]] = set()
        while frontier:
            found = self.ends(link_type, frontier, by="from", tiers=FIRM) + \
                self.ends(link_type, frontier, by="to", tiers=FIRM)
            rows.update(found)
            frontier = {x for pair in found for x in pair} - seen
            seen |= frontier
        return seen, sorted(rows)

    def walk(self, path: Sequence[str], ids: Iterable[str], *, back: bool = False
             ) -> tuple[set[str], set[str]]:
        """(the ends a maker path reaches from ``ids``, every object on the way); ``back``
        walks it from the assembly orders to the order lines."""
        at, via = set(ids), set()
        for step in (reversed(path) if back else path):
            reverse = step.startswith("~") != back
            name = step.lstrip("~")
            at = self.sources(name, at) if reverse else self.targets(name, at)
            via |= at
        return at, via


def served_peg_links(cur: Any, start: str, end: str) -> dict[str, dict[str, Any]]:
    """The served links of the id range ``[start, end)``, every column (``ontology.links``
    unpacks the packed texts)."""
    from ontology import store_ddl
    cur.execute(f"SELECT {', '.join(store_ddl.LINK_COLUMNS)} FROM ontology.links WHERE "
                "link_id COLLATE \"C\" >= %s AND link_id COLLATE \"C\" < %s", (start, end))
    names = [d.name for d in cur.description]
    return {row[0]: dict(zip(names, row)) for row in cur.fetchall()}


def _reason(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        held = json.loads(row.get("reason") or "[]")
    except (TypeError, ValueError):
        return []
    return [r for r in held if isinstance(r, dict)] if isinstance(held, list) else []


def scope(cur: Any, conn: ScopedConnection, cfg: Mapping[str, Any],
          links: Mapping[str, Any], touched: Iterable[str],
          served: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The part of the pegging ``touched`` can move (module docstring): ``{"universe",
    "replace_from", "replace_to", "items", "rounds"}``. ``links``: the schema's link
    types (their ``from``/``to`` types); ``served``: the served peg links by id."""
    from ontology import demand_pegging as dp
    g = _Graph(cur)
    touched = set(touched)
    sides = {"d": cfg["demand"], "s": cfg["supply"]}
    comp = cfg["components"]
    line_type = {k: s["line_type"] for k, s in sides.items()}
    order_type = {k: links[s["order_link"]]["to"] for k, s in sides.items()}
    item_types = {links[s["item_link"]]["to"] for s in (*sides.values(), comp)}
    maker_type = links[comp["component_link"]]["to"]
    comp_type = links[comp["component_link"]]["from"]
    path_types = {links[step.lstrip("~")][end] for path in comp["maker_paths"]
                  for step in path for end in ("from", "to")} - {line_type["d"], maker_type}

    def orders_of(k: str, lines: Iterable[str]) -> set[str]:
        return g.targets(sides[k]["order_link"], lines)

    def lines_for(makers: Iterable[str]) -> set[str]:
        out: set[str] = set()
        for path in comp["maker_paths"]:
            out |= g.walk(path, makers, back=True)[0]
        return out

    def makers_for(lines: Iterable[str]) -> tuple[set[str], set[str]]:
        makers, via = set(), set()
        for path in comp["maker_paths"]:
            reached, on = g.walk(path, lines)
            makers |= reached
            via |= on
        return makers, via

    items: set[str] = {x for x in touched if _type(x) in item_types}
    orders: dict[str, set[str]] = {k: set() for k in sides}
    makers_t: set[str] = set()
    for k in sides:
        orders[k] |= {x for x in touched if _type(x) == order_type[k]}
        orders[k] |= orders_of(k, {x for x in touched if _type(x) == line_type[k]})
    makers_t |= {x for x in touched if _type(x) == maker_type}
    makers_t |= g.targets(comp["component_link"], {x for x in touched if _type(x) == comp_type})
    for path in comp["maker_paths"]:   # an allocation: its lines and its assembly orders
        mids = {x for x in touched if _type(x) in path_types}
        if mids and len(path) > 1:
            orders["d"] |= orders_of("d", g.walk(path[:1], mids, back=True)[0])
            makers_t |= g.walk(path[1:], mids)[0]
    makers_t = g.closure(comp["maker_twin_link"], makers_t)[0] if makers_t else set()
    orders["d"] |= orders_of("d", lines_for(makers_t))

    # the served links: their ends, ends' orders, cited components and their assembly
    # orders, and item (``same_item``)
    line_links = {lid: r for lid, r in served.items() if r["link_type"] == cfg["line_link"]}
    ends = {x for r in line_links.values() for x in (r["from_object"], r["to_object"])}
    end_order = {**{a: b for a, b in g.ends(sides["d"]["order_link"], ends, by="from")},
                 **{a: b for a, b in g.ends(sides["s"]["order_link"], ends, by="from")}}
    cited: dict[str, set[str]] = {}
    item_of_link: dict[str, str | None] = {}
    for lid, r in line_links.items():
        reason = _reason(r)
        item_of_link[lid] = next((x.get("item") for x in reason
                                  if x.get("feature") == "same_item"), None)
        cited[lid] = {x["via_component"] for x in reason if x.get("via_component")}
    comp_maker = dict(g.ends(comp["component_link"], {c for v in cited.values() for c in v},
                             by="from"))
    facts = {}
    for lid, r in line_links.items():
        held = {r["from_object"], r["to_object"], item_of_link[lid]} | cited[lid]
        held |= {end_order.get(r["from_object"]), end_order.get(r["to_object"])}
        held |= {comp_maker.get(c) for c in cited[lid]}
        facts[lid] = held - {None}
        if facts[lid] & (touched | makers_t):
            items.add(item_of_link[lid]) if item_of_link[lid] else None
            orders["d"] |= {end_order[r["from_object"]]} if r["from_object"] in end_order \
                else set()
            orders["s"] |= {end_order[r["to_object"]]} if r["to_object"] in end_order \
                else set()

    rounds = 0
    trace: list[dict[str, Any]] = []
    seen_lines: dict[str, set[str]] = {k: set() for k in sides}
    checked: dict[str, set[str]] = {k: set() for k in sides}
    live_orders: dict[str, set[str]] = {k: set() for k in sides}
    live_lines: dict[str, set[str]] = {k: set() for k in sides}
    twin_rows: dict[str, set[tuple[str, str]]] = {k: set() for k in sides}
    makers: set[str] = set()
    via: set[str] = set()
    comps: set[str] = set()
    clock = time.monotonic()

    def lap(name: str) -> None:
        nonlocal clock
        trace[-1][name + "_s"] = round(time.monotonic() - clock, 1)
        clock = time.monotonic()

    while True:
        rounds += 1
        trace.append({})
        items = g.item_group(cfg["item_identity_link"], items) if items else set()
        found = {k: g.sources(s["item_link"], items) for k, s in sides.items()}
        lap("item_lines")
        # the assembly orders using an item in the part, but those closed (a closed one
        # makes its twin group closed: it demands nothing, ``_component_demand``)
        makers_g = g.targets(comp["component_link"], g.sources(comp["item_link"], items))
        if makers_g:
            conn.set_scope(makers_g)
            cells = dp._Cells(conn, list(comp["open"]), sorted({_type(m) for m in makers_g}))
            makers_g = {m for m in makers_g if dp._open(m, cells, comp["open"]) != "closed"}
        if makers_g | makers_t:
            found["d"] |= lines_for(g.closure(comp["maker_twin_link"], makers_g | makers_t)[0])
        lap("item_makers")
        for k in sides:
            seen_lines[k] |= found[k]
            orders[k] |= orders_of(k, found[k])
        # only an order whose twin group holds an open order has lines the step reads as
        # demand or supply: those groups' lines are the part's
        new_items: set[str] = set()
        for k, s in sides.items():
            fresh = orders[k] - checked[k]
            if not fresh:
                continue
            group, rows = g.closure(s["order_twin_link"], fresh)
            twin_rows[k] |= set(rows)
            todo = group - checked[k]
            conn.set_scope(todo)
            cells = dp._Cells(conn, list(s["open"]), sorted({_type(o) for o in todo}))
            twins = dp._components(sorted(twin_rows[k]))
            live_roots = {twins.get(o, o) for o in todo
                          if dp._open(o, cells, s["open"]) == "open"}
            live = {o for o in group if twins.get(o, o) in live_roots} - live_orders[k]
            checked[k] |= group
            orders[k] |= group
            live_orders[k] |= live
            got = g.sources(s["order_link"], live)
            live_lines[k] |= got
        lap("live_orders")
        made, on = makers_for(live_lines["d"])
        made = g.closure(comp["maker_twin_link"], made)[0] if made else set()
        if made - makers:
            makers |= made
            via |= on
            comps = g.sources(comp["component_link"], makers)
        lap("live_makers")
        # served links in the part, or of an item in it: their ends' orders and items
        new_orders: dict[str, set[str]] = {k: set() for k in sides}
        for lid, r in line_links.items():
            if r["from_object"] in seen_lines["d"] or r["to_object"] in seen_lines["s"] \
                    or item_of_link[lid] in items:
                for k, end in (("d", r["from_object"]), ("s", r["to_object"])):
                    if end in end_order:
                        new_orders[k].add(end_order[end])
                    new_items |= g.targets(sides[k]["item_link"], {end})
                if item_of_link[lid]:
                    new_items.add(item_of_link[lid])
        trace[-1].update({"items": len(items), "seen_lines": sum(map(len, seen_lines.values())),
                          "orders": sum(map(len, orders.values())),
                          "live_orders": sum(map(len, live_orders.values())),
                          "live_lines": sum(map(len, live_lines.values())),
                          "makers": len(makers), "comps": len(comps)})
        if os.environ.get("PLX_PEG_SCOPE_TRACE"):
            print(json.dumps(trace[-1]), file=sys.stderr, flush=True)
        if new_items <= items and all(new_orders[k] <= orders[k] for k in sides):
            break
        items |= new_items
        for k in sides:
            orders[k] |= new_orders[k]
    universe = items | makers | comps | via
    for k in sides:
        universe |= live_lines[k] | live_orders[k]
    return {"universe": universe, "items": items, "rounds": rounds, "trace": trace}


def _root(row: Mapping[str, Any]) -> str | None:
    return next((x.get("item") for x in _reason(row) if x.get("feature") == "same_item"), None)


def scoped_links(cur: Any, conn: ScopedConnection, part: Mapping[str, Any],
                 cfg: Mapping[str, Any], served: Mapping[str, Mapping[str, Any]]
                 ) -> tuple[list[dict[str, Any]], dict[str, int], Any]:
    """The step's rows over ``part`` (``scope``), the counts (over the part only) and
    which served rows they replace: the line links of the part's items (their
    ``same_item``), and the order links of every order pair one of those lines' orders is
    in, rolled up again from the line links the store then holds (``_rolled``)."""
    from ontology import demand_pegging as dp
    items = part["items"]
    conn.set_scope(part["universe"])
    rows, counts = dp.peg_links(conn, cfg)
    line_type = cfg["line_link"]
    new_lines = [r for r in rows if r["link_type"] == line_type and _root(r) in items]
    out = dict(counts)
    gone = {lid for lid, r in served.items() if r["link_type"] == line_type
            and _root(r) in items}
    held = {lid: r for lid, r in served.items()
            if r["link_type"] == line_type and lid not in gone}
    held.update({r["link_id"]: r for r in new_lines})
    g = _Graph(cur)
    ends = {x for r in [*new_lines, *(served[lid] for lid in gone)]
            for x in (r["from_object"], r["to_object"])}
    ends |= {x for r in held.values() for x in (r["from_object"], r["to_object"])}
    sides = {"d": cfg["demand"], "s": cfg["supply"]}
    order_of = {k: {} for k in sides}
    for k, s in sides.items():
        for line, order in g.ends(s["order_link"], ends, by="from", tiers=dp.FIRM):
            order_of[k].setdefault(line, order)
    touched = {"d": set(), "s": set()}
    for r in [*new_lines, *(served[lid] for lid in gone)]:
        touched["d"].add(order_of["d"].get(r["from_object"]))
        touched["s"].add(order_of["s"].get(r["to_object"]))
    for lid, r in served.items():   # an order link naming a replaced line link
        if r["link_type"] != line_type and any(
                x in gone for f in _reason(r) for x in (f.get("links") or ())):
            touched["d"].add(r["from_object"])
            touched["s"].add(r["to_object"])
    touched = {k: v - {None} for k, v in touched.items()}
    orders = _rolled(conn, cur, cfg, held, order_of, touched)
    out["rolled_orders"] = len(orders)

    def replaced(row: Mapping[str, Any]) -> bool:
        if row["link_type"] == line_type:
            return _root(row) in items
        return row["from_object"] in touched["d"] or row["to_object"] in touched["s"]
    return new_lines + orders, out, replaced


def _rolled(conn: Any, cur: Any, cfg: Mapping[str, Any],
            lines: Mapping[str, Mapping[str, Any]], order_of: Mapping[str, Mapping[str, str]],
            touched: Mapping[str, set[str]]) -> list[dict[str, Any]]:
    """The order links (``SalesOrder.awaits_purchase_order``) of every order pair with an
    order in ``touched``, from the line links ``lines``, as ``demand_pegging.peg_links``
    rolls them up (the same fields, order and rejection)."""
    from ontology import mentions
    from ontology.demand_pegging import PREFIX
    from ontology.sender_parties import _digest, _versions
    order_type = cfg["order_link"]
    rolled: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for r in sorted(lines.values(), key=lambda r: (r["from_object"], r["to_object"])):
        d_order = order_of["d"].get(r["from_object"])
        s_order = order_of["s"].get(r["to_object"])
        if d_order and s_order and (d_order in touched["d"] or s_order in touched["s"]):
            rolled.setdefault((d_order, s_order), []).append(r)
    if not rolled:
        return []
    cur.execute("SELECT from_object, to_object FROM ontology.link_rows WHERE link_type = %s "
                "AND tier = 'contradicted' AND from_object = ANY(%s)",
                (order_type, sorted({d for d, _s in rolled})))
    rejected = {tuple(r) for r in cur.fetchall()}
    versions = _versions(conn, {x for pair in rolled for x in pair})
    out = []
    for (d_order, s_order), rows in sorted(rolled.items()):
        dv, sv = versions.get(d_order), versions.get(s_order)
        if (d_order, s_order) in rejected or dv is None or sv is None:
            continue
        reason = [{"feature": "pegged_lines", "agrees": True,
                   "links": sorted(r["link_id"] for r in rows)}]
        out.append({
            **{k: rows[0][k] for k in rows[0]},
            "link_id": PREFIX + _digest(order_type, d_order, s_order),
            "link_type": order_type, "from_object": d_order, "to_object": s_order,
            "reason": json.dumps(reason, sort_keys=True, separators=(",", ":")),
            "evidence_handles": json.dumps(list(dict.fromkeys(
                [dv["l1_handle"], sv["l1_handle"],
                 *(h for r in rows for h in json.loads(r["evidence_handles"]))])),
                separators=(",", ":")),
            "markings": mentions._marks(dv["markings"], sv["markings"],
                                        *(r["markings"] for r in rows)),
            "recorded_at": max([dv["recorded_at"] or "", sv["recorded_at"] or "",
                                *(r["recorded_at"] for r in rows)])})
    return out


__all__ = ["SCOPE_TABLE", "ScopedConnection", "scope", "scoped_links", "served_peg_links"]
