"""The served History builder's runner (speed specs 9.3 V2, 8.3 ``history``): every purchase order
page's change log computed ONCE, off the box, by the History tab's own code
(``operating.ontology_tools_history.object_history``) as the owner's full reader, in its builder
mode (``marks``: every change in one answer, none of the tab's display bounds, each change and
link saying what a reader must hold to see it). ``operating.pg.record_history load`` stores the
lines as page items whose classes carry those requirements; nothing here decides for a reader.

It runs on a Spark against a build copy (``deploy/spark/page_items_fill.sh run``) as the app's own
console builder (``case_console.main``, as ``plan_batch``), writes nothing to any business system
or to Postgres: one JSON line per record to ``--out``.

  python3 -m operator_api.history_batch run --out F [--ids FILE] [--shard I/N] [--host-config P]

The first line is ``{"stamps": ...}`` (the store's version watermark at the start). A record whose
history could not be built is one line ``{"record_id", "error"}`` (logged, counted, never stored:
its page then says the history is not built), never left out silently.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import time
from typing import Any, Sequence

HOST_CONFIG_ENV = "PLX_BATCH_HOST_CONFIG"
READER_ENV = "PLX_BATCH_READER"
# every purchase order page (its member records), the open and draft ones first (the gate starts
# on them while the rest fills; speed specs 9.4 item 4)
def ids_sql(schema: str) -> str:
    """Every purchase order page's member record (``record_pages.MEMBERS_SQL``, the one
    definition), the open and draft ones first."""
    from operating.pg.record_pages import MEMBERS_SQL
    return (f"SELECT record_id FROM ({MEMBERS_SQL.format(s=schema)}) m GROUP BY record_id "
            "ORDER BY bool_or(status IN ('open', 'draft')) DESC, record_id")
log = logging.getLogger("operator_api.history_batch")


def targets(dsn: str, schema: str = "biz") -> tuple[list[str], dict[str, str]]:
    import psycopg
    from operating.pg import plan_sections
    with psycopg.connect(dsn, autocommit=True) as pg:
        ids = [r[0] for r in pg.execute(ids_sql(schema))]
        return ids, {"ver": str(pg.execute(plan_sections.VER_SQL).fetchone()[0])}


def deps_of(pg: Any) -> dict[str, int]:
    """The watermarks a record's history is read at, taken just before it is read (``plan_sections``'
    three: object versions, link rows, the transaction horizon): stored with its head item, they
    decide when it must be read again (``record_history.moved_sql``)."""
    from operating.pg import plan_sections as ps
    return {"ver": int(pg.execute(ps.VER_SQL).fetchone()[0]),
            "link": int(pg.execute(ps.LINK_SQL).fetchone()[0]),
            "xmin": int(pg.execute(ps.XMIN_SQL).fetchone()[0])}


def history_of(view: Any, oid: str) -> dict[str, Any]:
    """One record's line: its kind and label as the tab shows them, the label's own markings, and
    every change of its history with what a reader must hold (``object_history``'s builder mode).
    Raises on anything that is not a whole answer."""
    from operating.ontology_tools_history import object_history
    from operator_api.history import _label
    with view._request():
        access = view.access
        gated, policy, _ = access._state()
        tools = access.host.tools
        tools.policy = policy        # the request's pinned grant, as the workspace's reads set it
        pin = getattr(tools.store, "one_version", None)
        with (pin() if pin is not None else contextlib.nullcontext()), tools.store.lock:
            got = object_history(tools, {"object_id": oid, "history": True, "marks": True},
                                 gated)
        if not isinstance(got, dict) or "error" in got or "history" not in got:
            raise RuntimeError(f"no history: {got!r}")
        nxt = (got.get("completeness") or {}).get("next_cursor")
        if nxt is not None:   # the builder reads whole: a cut answer is not the record's
            raise RuntimeError("not whole: a next page")
        kind, label = _label(got, oid)
        label_marks = _display_marks(tools, oid)
    # what even the owner's full read leaves out (a link to a record no reader may open, its
    # evidence refused): no reader sees it either, and every reader's tab says some are not shown
    return {"record_id": oid, "type": kind, "label": label, "label_marks": label_marks,
            "gaps": got.get("gaps") or [], "events": got["history"],
            "withheld": bool(got.get("withheld")), "conflicts": conflicts_of(view, oid)}


def conflicts_of(view: Any, oid: str) -> dict[str, Any]:
    """The record page's data conflicts as the OWNER's full read finds them (speed specs 8.3
    ``conflicts``, the default posted 11-23 7:21 PM): every raw conflict of its workspace (the
    tool conflicts of ``plx.object``, each ``plx.around`` hop, each ``plx.timeline`` call,
    ``plx.process``, ``plx.documents``) as ``operating.answer_conflicts.tool_conflicts`` reads
    them, each value with the handle it cites and the markings it carries, and the receipts
    dated before the order date (``backend.received_inputs``). Nothing is decided for a reader
    here and nothing is cut: ``record_history.items`` stores one item per value and per receipt,
    each with what a reader must hold to open its handle; the page runs the same rules on the
    values its reader holds (``served.record_conflicts``). A value whose handle no reader may
    open (``audience_unit`` none and no item gate: ``HostAccess._decide`` withholds it from
    everyone) is left out here as it is left out of every reader's page."""
    from operator_api.backend import received_inputs
    from operator_api.serving import citation_handle, plain
    from operator_api.titles import readable
    from operator_api.workspace.object_workspace import audience_unit
    from operating.ontology_tools_policy import _ITEM_ACL_SCHEMES
    # without ``plx.documents``: its answer carries no value conflict (proved on the build copy:
    # decision file open-serve-conflicts-builder-cost.md), and reading it opens every linked mail
    page = view.workspace(oid, documents=False)
    card = page.get("card") or {}
    obj = card.get("object") or {}
    kind = str(obj.get("object_type") or "Object")
    values = [v for v in card.get("values") or () if isinstance(v, dict)]
    # the card's subject is the record's display only, else blank: ``page_title`` falls back to the
    # owner's property values, which a narrower reader may not hold
    display = plain(obj.get("display"))
    label = str(display).strip() if readable(display, oid, kind) else ""

    def readable_by_anyone(handle: str) -> bool:
        return audience_unit(handle) is not None or handle.startswith(_ITEM_ACL_SCHEMES)

    raw = []
    for conflict in page.get("conflicts") or ():
        if not isinstance(conflict, dict) or conflict.get("kind") not in (None, "value_disagreement"):
            continue
        stated = list((conflict.get("resolution") or {}).get("as_of") or ())
        kept = []
        for at, value in enumerate(v for v in conflict.get("values") or ()):
            if not isinstance(value, dict):
                continue
            cite = value.get("citation") if isinstance(value.get("citation"), dict) else {}
            handle = cite.get("l1_handle")
            if not isinstance(handle, str) or not handle or not readable_by_anyone(handle):
                continue
            kept.append({"value": value, "as_of": stated[at] if at < len(stated) else None,
                         "unit": audience_unit(handle)})
        raw.append({"kind": conflict.get("kind"), "subject": conflict.get("subject"),
                    "tier": conflict.get("tier"), "from": conflict.get("from"), "values": kept})
    got = received_inputs(values, page)
    early = [[when, handle] for when, handle in (got or {}).get("early") or ()
             if readable_by_anyone(handle)]
    received = None
    if got is not None and early:
        order_unit = audience_unit(got["order_handle"])
        received = {"ordered": got["ordered"], "order_handle": got["order_handle"],
                    "order_markings": got["order_markings"], "order_unit": order_unit,
                    "early": [[w, h, audience_unit(h)] for w, h in early]}
    return {"label": label, "raw": raw, "received": received}


def _display_marks(tools: Any, oid: str) -> list[str]:
    """Every marking any value of the record's display properties carries (the label shown is
    read from one of them; the union is never less than that one's: fail closed)."""
    display = tools.schema.display() if tools.schema else {}
    props = set(display.get(oid.split(":", 1)[0], ()))
    out: set[str] = set()
    for row in tools.store.values([oid]):
        if row["property"] in props:
            out |= set(json.loads(row["markings"] or "[]"))
    return sorted(out)


def run(args: argparse.Namespace) -> int:
    from operator_api.plan_batch import console_args
    from operator_api.workspace import case_console
    host = args.host_config or os.environ.get(HOST_CONFIG_ENV)
    reader = os.environ.get(READER_ENV)
    dsn = os.environ.get("PLX_PG_DSN")
    if not host or not reader or not dsn:
        print(f"{HOST_CONFIG_ENV}, {READER_ENV} and PLX_PG_DSN are required", file=sys.stderr)
        return 2
    index, count = (int(x) for x in args.shard.split("/"))
    if not 0 <= index < count:
        print("shard is I/N with 0 <= I < N", file=sys.stderr)
        return 2
    ids, stamps = targets(dsn)
    if args.ids:
        with open(args.ids, encoding="utf-8") as fh:
            ids = [x.strip() for x in fh if x.strip()]
    ids = ids[index::count]
    argv = console_args(host, dsn)

    def serve(ctx: dict[str, Any]) -> int:
        from operating.session_identity import warm_session
        session = warm_session(ctx["resolver"], reader, expires_at=int(time.time()) + 12 * 3600)
        view = ctx["objects_for"](session)
        import psycopg
        failed = 0
        with open(args.out, "w", encoding="utf-8") as out, \
                psycopg.connect(dsn, autocommit=True) as pg:
            out.write(json.dumps({"stamps": stamps, "shard": args.shard, "ids": len(ids)}) + "\n")
            for oid in ids:
                began = time.monotonic()
                try:
                    deps = deps_of(pg)
                    line = {**history_of(view, oid), "deps": deps}
                except Exception as exc:  # noqa: BLE001 - one typed line per record, logged
                    failed += 1
                    log.warning("history not built for one record: %s", type(exc).__name__)
                    line = {"record_id": oid, "error": f"{type(exc).__name__}: {exc}"}
                line["took"] = round(time.monotonic() - began, 2)
                out.write(json.dumps(line, default=str) + "\n")
                out.flush()
        print(json.dumps({"records": len(ids), "failed": failed}))
        return 0
    return case_console.main(argv, serve_with=serve)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operator_api.history_batch")
    ap.add_argument("command", choices=("run",))
    ap.add_argument("--out", required=True)
    ap.add_argument("--ids")
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--host-config")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
