"""Supplier-lot trace, live: a certificate of analysis to its receipt line and on.

One GET-only SharePoint capture of the Portal's raw-material receiving folder (the
``ontology.document_fields_coa`` scope, ``integrations/capture_sharepoint.py`` through the
GET-only Graph client) and a GET-only Portal mirror of the relations the trace runs over
(``TRACE_RELATIONS``: gold and FileMaker receipt lines, receipts, suppliers, purchase orders
and the FM PO lines and the gold products) into the check's temporary host state; one
projection of both with the host's post-project steps, so each certificate's ``lot`` links
the receipt lines whose ``supplier_lot_number`` it states
(``CertificateOfAnalysis.for_receipt_line``, a lead;
``devloop/DECISIONS/2026-10-20-documents-lot-trace-supplier-lot.md``).

Measured through the served host (``plx.around``), as ``--principal`` reads it:

* ``certificates`` served, ``certificates_with_lot`` (a ``lot`` value stated) and
  ``certificates_linked`` (one receipt line or more through ``for_receipt_line``);
* ``receipt_lines`` projected and ``receipt_lines_with_supplier_lot``;
* per hop, of the linked certificates, how many reach a Supplier (``line_of`` then
  ``supplied_by``; else the vendor of the PO received against: ``line_of``, ``receives``,
  ``ordered_from`` on gold, ``receives_line``, ``line_of``, ``ordered_from`` on FM), a
  PurchaseOrder (``line_of`` then ``receives``, gold; ``receives_line`` then ``line_of``,
  FM) and a Product (``for_item``, gold only: FM receipt rows bind none), walked one served
  step at a time (``plx.around`` serves at most 3); ``hop_paths``: per hop, the
  certificates each path reaches;
* ``hop_shortfall``: per hop, the linked certificates reaching none of it, by cause
  (``SHORTFALL_CAUSES``) of the path that went deepest, so each hop's causes sum to
  ``certificates_linked - hops[hop]``; ``hop_shortfall_at``: per hop and path, the link type
  each such certificate stopped at and why (``ReceiptLine.line_of source_column_empty``: the
  FM line's ``idreceipt`` is null as served; ``ReceiptLine.for_item no_binding``: an FM
  line binds no product). A cause is read from the served gap and, for a ``no_match``, from
  the record's key columns of the link as ``plx.read`` serves the reader the row; what
  cannot be read is ``unknown``, never a cause;
* ``samples``: up to 3 linked certificates (in a hashed order, never by id) as the lines'
  sources (``gold`` / ``fm``) and one boolean per hop;
* ``leaks``: what ``--withheld-principal`` (the Portal relations granted, no SharePoint
  file) is served of a certificate from the receipt lines (``certificates_of_analysis``):
  must be 0.

``ok``: the capture and every trace relation read whole, at least ``--min-documents``
certificates, one linked and reaching a Supplier and a PurchaseOrder, no leak. Without a
Portal client (``PLX_WORKBENCH_API_URL`` / ``PLX_WORKBENCH_API_KEY``) the run is blocked
``no_record_source``. Counts, booleans and enums only: no names, ids, lots or text.
Run as ``python3 -m scripts.eval_document_fields_live --lot-trace ...``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable, Mapping

CHECK = "ontology.lot_trace"
COA = "CertificateOfAnalysis"
TRACE_LINK = "CertificateOfAnalysis.for_receipt_line"
TRACE_RELATIONS = (
    "public.app_receipt_line", "public.receipt_raw_material", "public.receipt_packaging",
    "public.app_receipt", "public.receipt", "public.app_supplier", "public.supplier",
    "public.app_purchase_order", "public.generated_purchase_order",
    "public.generated_purchase_order_item", "public.app_product")
# Hop name -> the alternative paths from a certificate (any one reaching an object counts).
# A Supplier is the receipt's (``supplied_by``) or, as far as the receipt does not say, the
# vendor of the purchase order the line was received against (``ordered_from``).
HOPS = {
    "supplier": (("for_receipt_line", "line_of", "supplied_by"),
                 ("for_receipt_line", "line_of", "receives", "ordered_from"),
                 ("for_receipt_line", "receives_line", "line_of", "ordered_from")),
    "purchase_order": (("for_receipt_line", "line_of", "receives"),
                       ("for_receipt_line", "receives_line", "line_of")),
    "product": (("for_receipt_line", "for_item"),),
}
# Why a linked certificate reaches no object of a hop, least conclusive first (a step whose
# frontier objects fail for several causes reports the first): ``unknown`` (the served gap
# says nothing more, or the record could not be read), ``source_unreadable`` (a key value
# the projector could not read), ``not_ingested`` (the binding or its target relation was
# not projected), ``target_not_held`` (the record states the key, no served record has it),
# ``source_column_empty`` (the record's key column of the link is null or blank) and
# ``no_binding`` (the record's source binds no such link: an FM receipt row names no
# product).
SHORTFALL_CAUSES = ("unknown", "source_unreadable", "not_ingested", "target_not_held",
                    "source_column_empty", "no_binding")
SAMPLES = 3
WITHHELD_PRINCIPAL = "staff:lot-trace-withheld"
NO_RECORD_SOURCE = "no_record_source"
HOST_CONFIG = "lot-trace-host.json"
# Bound on the column pages of one row read (plx.read column_offset) for a shortfall.
_ROW_PAGES = 20


def _reached(served: Any, reader: Any, start: str, path: tuple[str, ...]) -> int | None:
    """The objects ``plx.around`` serves ``reader`` at the end of ``path`` from ``start``
    (None: ``start`` is not served)."""
    out = served.call("plx.around", {"from": start, "limit": 50, "tier_floor": "lead",
                                     "path": [{"link": name} for name in path]}, reader)
    if "error" in out:
        return None
    return len(out["objects"])


def _line_source(object_id: str) -> str:
    return "gold" if ":portal.app_receipt_line:" in object_id else "fm"


def _blank(value: Any) -> bool:
    if isinstance(value, Mapping):
        value = value.get("untrusted_text")
    return value is None or (isinstance(value, str) and not value.strip())


class _Walk:
    """One served hop at a time (``plx.around`` serves at most 3 steps), each (object, link)
    asked once; a step that reaches nothing from an object is typed by its cause."""

    def __init__(self, served: Any, reader: Any, conn: sqlite3.Connection,
                 schema: Any) -> None:
        self.served, self.reader, self.conn, self.schema = served, reader, conn, schema
        self._steps: dict[tuple[str, str], tuple[tuple[str, ...], tuple[str, str]]] = {}

    def step(self, start: str, link: str) -> tuple[tuple[str, ...], tuple[str, str]]:
        """(objects reached, (link type, cause) when none is)."""
        key = (start, link)
        if key not in self._steps:
            out = self.served.call("plx.around", {
                "from": start, "limit": 50, "tier_floor": "lead", "path": [{"link": link}]},
                self.reader)
            link_type = f"{start.split(':', 1)[0]}.{link}"
            if "error" in out:
                self._steps[key] = ((), (link_type, "unknown"))
            else:
                objects = tuple(sorted(o["object_id"] for o in out["objects"]))
                self._steps[key] = (objects, (link_type, "" if objects else
                                              self._cause(start, link_type, out.get("gaps"))))
        return self._steps[key]

    def _cause(self, start: str, link_type: str, gaps: Any) -> str:
        subject = f"{link_type} forward"
        reasons = [g for g in gaps or () if isinstance(g, Mapping) and
                   g.get("subject") == subject]
        if len(reasons) != 1:
            return "unknown"
        gap = reasons[0]
        if gap.get("reason") == "not_applicable_to_source":
            return "no_binding"
        if gap.get("reason") == "unreadable_in_source":
            return "source_unreadable"
        if gap.get("reason") != "no_match":
            return "unknown"
        if gap.get("state") == "not_ingested":
            return "not_ingested"
        return self._key_columns(start, link_type)

    def _key_columns(self, start: str, link_type: str) -> str:
        """``source_column_empty`` when every key column of the link's binding on
        ``start``'s record is null or blank as the reader is served the row, else
        ``target_not_held``; ``unknown`` when that cannot be read."""
        binding = start.split(":", 2)[1]
        declared = [b for b in self.schema.link_types[link_type].get("bindings", ())
                    if b.get("from_binding") == binding]
        if len(declared) != 1 or "via" in declared[0]:
            return "unknown"
        columns = [pair.get("from") for pair in declared[0].get("pairs") or ()]
        if not columns or not all(isinstance(c, str) and c for c in columns):
            return "unknown"
        handles = [r[0] for r in self.conn.execute(
            "SELECT l1_handle FROM object_versions WHERE object_id = ? AND superseded_at IS "
            "NULL", (start,))]
        if len(handles) != 1:
            return "unknown"
        fields: dict[str, Any] = {}
        offset: int | None = 0
        for _ in range(_ROW_PAGES):
            args: dict[str, Any] = {"citation": handles[0]}
            if offset:
                args["column_offset"] = offset
            read = self.served.call("plx.read", args, self.reader)
            if read.get("status") != "resolved" or not isinstance(read.get("fields"), Mapping):
                return "unknown"
            fields.update(read["fields"])
            offset = (read.get("columns") or {}).get("next_offset")
            if offset is None:
                break
        else:
            return "unknown"
        keys = [f"row.{column}" for column in columns]
        if not all(key in fields for key in keys):
            return "unknown"
        return "source_column_empty" if all(_blank(fields[k]) for k in keys) \
            else "target_not_held"

    def path(self, start: str, path: tuple[str, ...]
             ) -> tuple[bool, int, list[tuple[str, str]]]:
        """(reached, steps taken, (link type, cause) of each object the last step failed
        from)."""
        frontier = [start]
        for depth, link in enumerate(path):
            reached: set[str] = set()
            failed: list[tuple[str, str]] = []
            for current in frontier:
                objects, why = self.step(current, link)
                reached.update(objects)
                if not objects:
                    failed.append(why)
            if not reached:
                return False, depth, failed
            frontier = sorted(reached)
        return True, len(path), []


def _least_conclusive(whys: Any) -> tuple[str, str]:
    """Of (link type, cause) pairs, the least conclusive cause (``SHORTFALL_CAUSES``)."""
    found = sorted(set(whys), key=lambda why: (SHORTFALL_CAUSES.index(why[1]), why[0]))
    return found[0] if found else ("unknown", "unknown")


def measure(store: Path, host: Any, principal: str, withheld: str,
            schema: Any = None) -> dict[str, Any]:
    """The trace counts (module docstring) as the served ``host`` reads them."""
    from ontology.types import Actor

    if schema is None:
        from ontology.schema import load_schema
        schema = load_schema()
    reader = Actor(host.config.tenant, frozenset({principal}), "restricted")
    other = Actor(host.config.tenant, frozenset({withheld}), "restricted")
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    try:
        return _measure(conn, host, reader, other, schema)
    finally:
        conn.close()


def _measure(conn: sqlite3.Connection, host: Any, reader: Any, other: Any,
             schema: Any) -> dict[str, Any]:
    certificates = [r[0] for r in conn.execute(
        "SELECT object_id FROM objects WHERE object_type = ? ORDER BY object_id", (COA,))]
    lots = {r[0] for r in conn.execute(
        "SELECT v.object_id FROM object_values v JOIN objects o ON o.object_id = "
        "v.object_id WHERE o.object_type = ? AND v.property = 'lot' AND "
        "v.superseded_at IS NULL AND v.value IS NOT NULL AND TRIM(v.value) <> ''",
        (COA,))}
    lines = conn.execute(
        "SELECT COUNT(*), COUNT(v.value) FROM objects o LEFT JOIN object_values v ON "
        "v.object_id = o.object_id AND v.property = 'supplier_lot_number' AND "
        "v.superseded_at IS NULL AND v.value IS NOT NULL WHERE o.object_type = "
        "'ReceiptLine'").fetchone()
    linked_lines = [r[0] for r in conn.execute(
        "SELECT DISTINCT to_object FROM links WHERE link_type = ? AND superseded_at IS "
        "NULL ORDER BY to_object", (TRACE_LINK,))]
    out: dict[str, Any] = {
        "certificates": 0, "certificates_withheld": 0, "certificates_with_lot": 0,
        "certificates_linked": 0, "links": 0, "receipt_lines": lines[0],
        "receipt_lines_with_supplier_lot": lines[1], "receipt_lines_linked": 0,
        "hops": dict.fromkeys(HOPS, 0),
        "hop_paths": {hop: {">".join(p): 0 for p in paths} for hop, paths in HOPS.items()},
        "hop_shortfall": {hop: dict.fromkeys(SHORTFALL_CAUSES, 0) for hop in HOPS},
        "hop_shortfall_at": {hop: {} for hop in HOPS},
        "line_sources": {"gold": 0, "fm": 0}, "samples": [], "leaks": 0}
    traced: list[tuple[str, dict[str, Any]]] = []
    with host.served_tools(reader) as served:
        walk = _Walk(served, reader, conn, schema)
        for certificate in certificates:
            first = served.call("plx.around", {
                "from": certificate, "limit": 50, "tier_floor": "lead",
                "path": [{"link": "for_receipt_line"}]}, reader)
            if "error" in first:
                out["certificates_withheld"] += 1
                continue
            out["certificates"] += 1
            out["certificates_with_lot"] += certificate in lots
            if not first["objects"]:
                continue
            out["certificates_linked"] += 1
            out["links"] += len(first["objects"])
            sources = sorted({_line_source(o["object_id"]) for o in first["objects"]})
            for source in sources:
                out["line_sources"][source] += 1
            trace: dict[str, Any] = {"line_sources": sources}
            for hop, paths in HOPS.items():
                failures: dict[str, tuple[int, tuple[str, str]]] = {}
                for path in paths:
                    reached, depth, whys = walk.path(certificate, path)
                    if reached:
                        out["hop_paths"][hop][">".join(path)] += 1
                    else:
                        failures[">".join(path)] = (depth, _least_conclusive(whys))
                trace[hop] = len(failures) < len(paths)
                if trace[hop]:
                    out["hops"][hop] += 1
                    continue
                # The hop's cause is its deepest path's; every path says where it stopped.
                deepest = max(depth for depth, _ in failures.values())
                out["hop_shortfall"][hop][_least_conclusive(
                    why for depth, why in failures.values() if depth == deepest)[1]] += 1
                for name, (_, (link_type, cause)) in failures.items():
                    at = out["hop_shortfall_at"][hop].setdefault(name, {})
                    at[f"{link_type} {cause}"] = at.get(f"{link_type} {cause}", 0) + 1
            traced.append((hashlib.sha256(certificate.encode()).hexdigest(), trace))
        out["receipt_lines_linked"] = sum(
            "error" not in served.call("plx.object", {"object_id": line}, reader)
            for line in linked_lines)
    with host.served_tools(other) as served:
        out["leaks"] = sum(
            "error" not in served.call("plx.object", {"object_id": c}, other)
            for c in certificates) + sum(
            _reached(served, other, line, ("certificates_of_analysis",)) or 0
            for line in linked_lines)
    out["samples"] = [trace for _, trace in sorted(traced, key=lambda t: t[0])[:SAMPLES]]
    return out


def run(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None,
        sender: Any = None, portal_client: Any = None,
        monotonic: Callable[[], float] | None = None) -> dict[str, Any]:
    """The ``--lot-trace`` run (module docstring): capture, mirror, project, measure."""
    from integrations import capture_sharepoint as cs
    from integrations.live_source_guard import LiveSourceAudience
    from integrations.source_mirror import AUDIENCE_SCHEMA
    from ontology.project import build_store
    from ontology.schema import load_schema
    from ontology.types import Actor
    from operating import build_steps
    from operating.host import CONFIG_SCHEMA, Host, HostConfig, projection_path
    from scripts import eval_document_fields_live as live
    from scripts.ontology_projection_probe import PORTAL, _part

    state = Path(args.state_dir).expanduser().resolve()
    capture = cs.run_cli(argparse.Namespace(
        state_dir=str(state), scope=args.scope, live_audience=args.live_audience,
        principal=args.principal, withheld_principal="staff:lot-trace-capture-withheld",
        folder_only_principal="staff:lot-trace-folder-only", runs=1, max_store_bytes=None,
        budget_seconds=args.capture_budget_seconds),
        environ=environ, sender=sender, monotonic=monotonic or time.monotonic)
    audience = LiveSourceAudience.from_file(args.live_audience)
    mirror_audience = state / "lot-trace-mirror-audience.json"
    mirror_audience.write_text(json.dumps({
        "schema": AUDIENCE_SCHEMA, "tenant": audience.tenant,
        "audiences": {"public.*": [args.principal, args.withheld_principal]}}), "utf-8")
    (state / HOST_CONFIG).write_text(json.dumps({
        "schema": CONFIG_SCHEMA, "tenant": audience.tenant,
        "capture_principal": args.principal, "mirror_audience": str(mirror_audience),
        "live_audience": str(Path(args.live_audience).resolve())}), "utf-8")
    blocked: list[dict[str, Any]] = []
    portal: set[str] = set()
    portal_sent = 0
    if portal_client is None:
        blocked.append(_part("portal_mirror", PORTAL, error_code="not_configured"))
    else:
        before = portal_client.requests_sent
        try:
            portal, down = live._portal_mirror(
                portal_client, state, mirror_audience, list(TRACE_RELATIONS),
                Actor(audience.tenant, frozenset({args.principal})),
                args.portal_budget_seconds)
        finally:
            portal_sent = portal_client.requests_sent - before
        if down is not None:
            blocked.append(down)
    schema = load_schema()
    store = projection_path(state)
    store.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        sources = live._captures(state, stack, portal)
        build_store(store, schema, sources,
                    post_project=build_steps.post_project(state, audience.tenant))
    host = Host(state, HostConfig.from_file(state / HOST_CONFIG), environ={})
    measured = measure(store, host, args.principal, args.withheld_principal, schema)
    summary = capture["summary"]
    report = {
        "check": CHECK, "evidence_label": capture["evidence_label"],
        "read_only_guard": capture["read_only_guard"], "sources": sorted(sources),
        "blocked_upstream": blocked,
        "capture": {key: summary[key] for key in ("files_current", "contents_captured",
                                                  "texts_extracted", "texts_ocr")},
        "capture_complete": summary["roots"] > 0 and summary["roots_complete"] ==
        summary["roots"],
        "portal_mirror": None if portal_client is None else {
            "relations": len(TRACE_RELATIONS), "relations_complete": len(portal),
            "requests_sent": portal_sent},
        "requests_sent": capture["requests_sent"] + portal_sent, **measured}
    if not portal:
        report["blocked"] = NO_RECORD_SOURCE
    report["ok"] = not report.get("blocked") and report["capture_complete"] and \
        len(portal) == len(TRACE_RELATIONS) and \
        measured["certificates"] >= args.min_documents and \
        measured["certificates_linked"] >= 1 and measured["hops"]["supplier"] >= 1 and \
        measured["hops"]["purchase_order"] >= 1 and not measured["leaks"]
    return report


def add_arguments(parser: argparse.ArgumentParser) -> None:
    from integrations import capture_sharepoint as cs

    parser.add_argument("--lot-trace", action="store_true", required=True)
    parser.add_argument("--state-dir", required=True,
                        help="throwaway Workbench state directory (outside every work tree)")
    parser.add_argument("--scope", required=True, help="plx-sharepoint-capture-scope/v1 file")
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting --principal the "
                             "SharePoint scope")
    parser.add_argument("--principal", default=cs.CAPTURE_PRINCIPAL)
    parser.add_argument("--withheld-principal", default=WITHHELD_PRINCIPAL)
    parser.add_argument("--min-documents", type=int, default=1)
    parser.add_argument("--capture-budget-seconds", type=float, default=None)
    parser.add_argument("--portal-budget-seconds", type=float, default=None)
