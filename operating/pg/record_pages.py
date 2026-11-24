"""The precomputed record page: the ``record`` items of the served store (``<biz>.page_items``,
``operating.pg.page_items``), one narrow row per item a record's page shows (its header, its
lines, each cell a reader may be refused, the records it links to), built set-based inside
Postgres from the business tables into a stage and applied in place (``page_items.apply``: only
the rows that differ are written), read by one indexed statement (``page_reads`` ``record_page``)
through the page door (``<biz>_page.page_items``: the same reader scope every business view is
read through, never in the AI's schema).

One page per record id, keyed by the MERGED business record: a purchase order held by the
Portal, FileMaker and BC copies is one row of ``purchase_orders``, and every copy's record id
serves that row's page (the row's lineage names its member records). Rows:

* ``header``  : the order's row as one payload (every column of the view that no reader may be
  refused). ``source_systems`` and the row's ``handle`` (``src_handle``) are its citation.
* ``cell``    : one item per column this ROW'S column class marks or needs (the finance-marked
  total, a promised date read from a supplier's mail ...), the payload one key. Its column
  class is exactly that column's (``marks``/``needs`` of the source class, the key renamed
  ``payload``), so a reader lacking the marking reads ``payload`` NULL with ``payload`` in
  ``withheld``, never the value, and one column's refusal never hides another's.
* ``lines`` / ``line_cell``: the order's lines the same way (``src_handle`` = the line's handle).
* ``linked``  : the records the order's copies link to, both ways, labelled; the link's own
  class (both ends' and the evidence's) decides it, as in ``record_links``.

PERMISSION. Every item carries the access class of the business row it was built from
(``purchase_orders``, ``purchase_order_lines``, ``record_links``: the same ints, never decided in
Python) and its lineage (the source row's records and evidence plus the page's own record id,
``page_items.apply``), so
the people exclusion drops or owner-restricts it as it does the source row (``door.exclude``),
and a cell whose column was read from an excluded person's record is emptied. Nothing is decided
per reader here.

``build`` runs on the box: the items are small (~100 MB for all purchase orders, a few seconds;
an apply writes only the items that changed) and their class ids must be the served ones. The
business views it reads are the ones shipped from a build copy (``ship_swap``).

  python3 -m operating.pg.record_pages build [--schema S]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from typing import Any, Sequence

from operating.pg import business_views as bv
from operating.pg import page_items

TABLE = page_items.TABLE

KIND = "record"
# the views each kind of page reads (view -> the column holding its order's handle)
PO = {"header": "purchase_orders", "lines": "purchase_order_lines"}
LINEAGE_VIEW = {"header": "purchase_orders", "cell": "purchase_orders",
                "lines": "purchase_order_lines", "line_cell": "purchase_order_lines",
                "linked": "record_links"}
SOURCES = ("purchase_orders", "purchase_order_lines")


def _deny(pg: Any, s: str, view: str) -> list[str]:
    """The table columns of ``view`` that are not the view's own (the permission columns,
    the handle, lineage): never in a payload."""
    own = {c.name for c in bv.VIEWS[view].cols}
    return [r[0] for r in pg.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND "
        "table_name = %s", (s, view)) if r[0] not in own]


# (order handle, member record id, member rid, the order's status) of every purchase order's
# merged record: THE definition of a purchase order page's member records (``members``, the
# History builder's targets: ``operator_api.history_batch``, ``history_resident``). ``{s}``.
MEMBERS_SQL = (
    "SELECT DISTINCT p.handle AS hdr, x.record_id, x.rid, p.status "
    "FROM {s}.purchase_orders p JOIN {s}.lineage l ON l.view = 'purchase_orders' AND "
    "l.handle = p.handle CROSS JOIN LATERAL unnest(l.object_ids) o JOIN {s}.records x "
    "ON x.record_id = o AND x.type = 'PurchaseOrder'")


def members(pg: Any, s: str) -> None:
    """``pg_temp.rp_map``: (order handle, member record id, member rid) of every purchase
    order's merged record (also ``record_comms``' map of a page's member records)."""
    pg.execute("DROP TABLE IF EXISTS pg_temp.rp_map")
    pg.execute(
        "CREATE TEMP TABLE rp_map AS SELECT DISTINCT hdr, record_id, rid FROM ("
        f"{MEMBERS_SQL.format(s=s)}) m")
    pg.execute("CREATE INDEX ON rp_map (hdr)")
    pg.execute("CREATE INDEX ON rp_map (rid)")


def _stage(pg: Any, s: str) -> None:
    """``pg_temp.rp_map``: (order handle, member record id, member rid) of every purchase
    order's merged record; ``rp_cell``/``rp_cellc``: each (source column class, marked column)
    and the ``payload`` class of just that column (appended to ``col_classes``)."""
    pg.execute("DROP TABLE IF EXISTS pg_temp.rp_cell, pg_temp.rp_cellc")
    members(pg, s)
    pg.execute(
        "CREATE TEMP TABLE rp_cell AS SELECT c.id AS src, k.col, "
        "CASE WHEN c.marks ? k.col THEN jsonb_build_object('payload', c.marks -> k.col) "
        "ELSE '{}'::jsonb END AS marks, "
        "CASE WHEN c.needs ? k.col THEN jsonb_build_object('payload', c.needs -> k.col) "
        "ELSE '{}'::jsonb END AS needs "
        f"FROM {s}.col_classes c CROSS JOIN LATERAL (SELECT jsonb_object_keys(c.marks) AS col "
        "UNION SELECT jsonb_object_keys(c.needs)) k WHERE c.id IN (SELECT col_class FROM "
        f"{s}.purchase_orders UNION SELECT col_class FROM {s}.purchase_order_lines)")
    # append-only ids, under the lock every class allocation takes
    pg.execute(f"LOCK TABLE {s}.col_classes IN SHARE ROW EXCLUSIVE MODE")
    pg.execute(
        f"INSERT INTO {s}.col_classes (id, marks, needs) SELECT (SELECT COALESCE(max(id), 0) "
        f"FROM {s}.col_classes) + row_number() OVER (ORDER BY p.marks::text, p.needs::text), "
        "p.marks, p.needs FROM (SELECT DISTINCT marks, needs FROM rp_cell) p WHERE NOT EXISTS "
        f"(SELECT 1 FROM {s}.col_classes c WHERE c.marks = p.marks AND c.needs = p.needs)")
    pg.execute(
        "CREATE TEMP TABLE rp_cellc AS SELECT DISTINCT ON (r.src, r.col) r.src, r.col, "
        f"c.id AS cc FROM rp_cell r JOIN {s}.col_classes c ON c.marks = r.marks AND "
        "c.needs = r.needs ORDER BY r.src, r.col, c.id")
    top = pg.execute(f"SELECT max(id) FROM {s}.col_classes").fetchone()[0]
    if top is not None and top > bv.CLASS_MAX:
        raise RuntimeError(f"{s}.col_classes: more than {bv.CLASS_MAX} column patterns")


def _insert(pg: Any, s: str, t: str, deny_hdr: list[str], deny_line: list[str],
            as_of: str) -> None:
    """Stage every purchase order's items into ``t`` (a ``page_items.stage``): page key = the
    member record id, handle = kind|key|section|seq, ``src_view`` = the business view of the
    source row (its lineage is the item's), ``lin_ids`` = the page's own record id."""
    cols = ("(page_key, section, seq, payload, src_handle, source_systems, access_class, "
            "col_class, ai_ok, ver, biz, cellcol, page_kind, handle, as_of, dep_ver, code_fp, "
            "src_view, lin_ids)")
    marked = ("ARRAY(SELECT jsonb_object_keys(cc.marks) UNION SELECT "
              "jsonb_object_keys(cc.needs))")

    def tail(view: str, m: str = "m") -> str:
        # the item's kind, its handle and code print (both set by ``apply``), its as-of, no
        # dependency stamp yet (SV4), the source row's view and the page's own record id
        return (f"'{KIND}', '', %s::timestamptz, 0, '', '{view}', ARRAY[{m}.record_id]")
    # the order's row: every column no reader of this row may be refused
    pg.execute(
        f"INSERT INTO {s}.{t} {cols} SELECT m.record_id, 'header', 0, "
        f"(to_jsonb(p) - %s::text[]) - {marked}, p.handle, p.source_systems, "
        f"p.access_class, 0, p.ai_ok, 0, true, NULL, {tail('purchase_orders')} "
        f"FROM rp_map m JOIN {s}.purchase_orders p ON p.handle = m.hdr "
        f"JOIN {s}.col_classes cc ON cc.id = p.col_class", (deny_hdr, as_of))
    pg.execute(
        f"INSERT INTO {s}.{t} {cols} SELECT m.record_id, 'cell', (row_number() OVER (PARTITION "
        "BY m.record_id ORDER BY rc.col) - 1)::int, jsonb_build_object(rc.col, "
        "to_jsonb(p) -> rc.col), p.handle, p.source_systems, p.access_class, rc.cc, p.ai_ok, "
        f"0, true, rc.col, {tail('purchase_orders')} FROM rp_map m JOIN {s}.purchase_orders p "
        "ON p.handle = m.hdr JOIN rp_cellc rc ON rc.src = p.col_class", (as_of,))
    pg.execute(
        f"INSERT INTO {s}.{t} {cols} SELECT m.record_id, 'lines', (row_number() OVER ("
        "PARTITION BY m.record_id ORDER BY l.item_code NULLS LAST, l.description, l.handle) "
        "- 1)::int, (to_jsonb(l) - %s::text[]) - "
        f"{marked}, l.handle, p.source_systems, l.access_class, 0, l.ai_ok, 0, true, NULL, "
        f"{tail('purchase_order_lines')} "
        f"FROM rp_map m JOIN {s}.purchase_order_lines l ON l.po_handle = m.hdr "
        f"JOIN {s}.purchase_orders p ON p.handle = m.hdr "
        f"JOIN {s}.col_classes cc ON cc.id = l.col_class", (deny_line, as_of))
    pg.execute(
        f"INSERT INTO {s}.{t} {cols} SELECT m.record_id, 'line_cell', (row_number() OVER ("
        "PARTITION BY m.record_id ORDER BY l.handle, rc.col) - 1)::int, "
        "jsonb_build_object(rc.col, to_jsonb(l) -> rc.col), l.handle, p.source_systems, "
        f"l.access_class, rc.cc, l.ai_ok, 0, true, rc.col, {tail('purchase_order_lines')} "
        f"FROM rp_map m JOIN {s}.purchase_order_lines l ON l.po_handle = m.hdr "
        f"JOIN {s}.purchase_orders p ON p.handle = m.hdr JOIN rp_cellc rc ON "
        "rc.src = l.col_class", (as_of,))
    # the records the order's copies link to (a link between two copies of one order is
    # said once, from the copy that is its from-end)
    link = (f"SELECT m.hdr, m.record_id, k.handle, k.link_type, k.tier, o.type, o.label, "
            "o.record_id AS other, k.access_class, k.ai_ok, k.ver, k.biz, '{d}' AS dir "
            f"FROM rp_map m JOIN {s}.record_links k ON k.{{a}}_rid = m.rid JOIN {s}.records o "
            f"ON o.rid = k.{{b}}_rid {{extra}}")
    out = link.format(d="out", a="from", b="to", extra="")
    inn = link.format(d="in", a="to", b="from",
                      extra="WHERE NOT EXISTS (SELECT 1 FROM rp_map m2 WHERE m2.hdr = m.hdr "
                            "AND m2.rid = o.rid)")
    pg.execute(
        f"INSERT INTO {s}.{t} {cols} SELECT q.record_id, 'linked', (row_number() OVER ("
        "PARTITION BY q.record_id ORDER BY q.link_type, q.other, q.dir, q.handle) - 1)::int, "
        "jsonb_build_object('dir', q.dir, 'link_type', q.link_type, 'tier', q.tier, "
        "'type', q.type, 'label', q.label, 'other_id', q.other), q.handle, NULL, "
        f"q.access_class, 0, q.ai_ok, q.ver, q.biz, NULL, {tail('record_links', 'q')} "
        f"FROM ({out} UNION ALL {inn}) q", (as_of,))


def code_fingerprint() -> str | None:
    from ontology.build_keys import code_digest
    digest = code_digest([("operating.pg.record_pages", "build"),
                          ("operating.pg.record_pages", "_insert"),
                          ("operating.pg.record_pages", "_stage"),
                          ("operating.pg.page_items", "apply")])
    return digest[:32] if digest else None


def _stamps(pg: Any, s: str) -> dict[str, str]:
    """What the items are built from: the two order views' own build stamps and the order
    records' links (their count and newest version; the generic refresh rewrites its own
    state every cycle, so that stamp says nothing)."""
    out = {v: b for v, b in pg.execute(
        f"SELECT view, built_at FROM {s}.state WHERE view = ANY(%s)", (list(SOURCES),))}
    count, newest = pg.execute(
        f"SELECT count(*), COALESCE(max(q.ver), 0) FROM (SELECT k.ver FROM {s}.records r JOIN "
        f"{s}.record_links k ON k.from_rid = r.rid WHERE r.type = 'PurchaseOrder' UNION ALL "
        f"SELECT k.ver FROM {s}.records r JOIN {s}.record_links k ON k.to_rid = r.rid WHERE "
        "r.type = 'PurchaseOrder') q").fetchone()
    out["links"] = f"{count}:{newest}"
    return out


def stale(dsn: str, schema: str | None = None) -> bool:
    """Whether the served table is behind the views it is built from or the code that builds
    it (nothing before a first ``build``)."""
    import psycopg
    s = schema or bv.SCHEMA
    with psycopg.connect(dsn, autocommit=True) as pg:
        if pg.execute("SELECT to_regclass(%s)", (f"{s}.state",)).fetchone()[0] is None:
            return False
        held = pg.execute(f"SELECT notes FROM {s}.state WHERE view = %s",
                          (page_items.state_view(KIND),)).fetchone()
        if held is None:
            return False
        notes = held[0] or {}
        return (notes.get("from") != _stamps(pg, s)
                or (code_fingerprint() or "") != (notes.get("code") or ""))


def build(dsn: str, schema: str | None = None) -> dict[str, Any]:
    """Stage every purchase order's items and apply them to the served store in place
    (``page_items.apply``: one transaction, only the items that differ are written; a reader
    sees the old items or the new). Any failure leaves the served items as they are."""
    import psycopg
    s = schema or bv.SCHEMA
    started = time.monotonic()
    with psycopg.connect(dsn, autocommit=True) as pg:
        for needed in ("purchase_orders", "purchase_order_lines", "records", "record_links",
                       "lineage", "state"):
            if pg.execute("SELECT to_regclass(%s)", (f"{s}.{needed}",)).fetchone()[0] is None:
                raise RuntimeError(f"{s}.{needed} is not built: no record pages")
        with page_items.room(pg, 1, "record_pages.build"):   # the WAL budget and the one disk budget
            page_items.ensure(pg, s)
            from_stamps = _stamps(pg, s)
            built = [dt.datetime.fromisoformat(from_stamps[v]) for v in SOURCES if from_stamps.get(v)]
            if len(built) != len(SOURCES):
                raise RuntimeError(f"{s}.state has no build time for {SOURCES}: no record pages")
            as_of = min(b if b.tzinfo else b.replace(tzinfo=dt.timezone.utc) for b in built)
            deny_hdr, deny_line = _deny(pg, s, "purchase_orders"), _deny(pg, s, "purchase_order_lines")
            pg.execute("SET statement_timeout = '900s'")
            stage = page_items.stage(pg, KIND, s)
            with pg.transaction():
                _stage(pg, s)
                _insert(pg, s, stage.split(".", 1)[1], deny_hdr, deny_line, as_of.isoformat())
            out = page_items.apply(pg, KIND, stage, keys=None, code_fp=code_fingerprint() or "",
                                   job="record_pages.build", schema=s,
                                   notes={"from": from_stamps})
    return {**out, "items": out["staged"], "seconds": round(time.monotonic() - started, 1)}


def follow(dsn: str, schema: str | None = None) -> dict[str, Any]:
    """After the business views' refresh: build again when the views it reads (or its code)
    moved. Nothing before a first deliberate ``build``; a failure is reported, never the
    refresh's."""
    try:
        if not stale(dsn, schema):
            return {}
        return {"record_pages": build(dsn, schema)}
    except Exception as exc:  # noqa: BLE001 - speed only: the page reads the old path
        return {"record_pages_failed": f"{type(exc).__name__}: {exc}"[:300]}


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.record_pages")
    ap.add_argument("command", choices=("build",))
    ap.add_argument("--schema")
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    from operating.pg import config
    print(json.dumps(build(args.dsn or config.dsn(), args.schema)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
