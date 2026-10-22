"""A purchase-order case opened through the generic tools, checked against the evaluation
oracle's own reading (GET only). An evaluation harness (it imports the oracle), named
``eval_*`` so product code may not import it (``scripts/check_no_hardcoded_cases.py``).

``case.packaging_case_script`` used to pass on the script's own
``checks.*`` flags. The per-kind packaging script is retired (task allowlist-3); this
wiring now opens the purchase order the way every case opens: ``operating.ingestion`` fills
the state directory's source mirror (GET only), then ``operating.case_workspace_live``
opens ``petra_po=<--po>`` offline through ``plx.find``, ``plx.object``, ``plx.around`` and
``plx.investigate``. The case's own answer is read back from the case record: the row
``plx.find`` rooted it on and the line rows of that root the case cites. It then reads the
same purchase order independently through the evaluation oracle's Portal port
(``evals.oracle``: ``scripts.eval_oracle_dev.portal_port`` over a fresh
``WorkbenchReadApiClient``, the oracle's own key and edge rules, never the mirror, the
projection or a case tool):

* the root: the row the script rooted on must exist exactly once in its relation, and the
  oracle's own lookup of ``--po`` must find exactly one row, that same row: a case rooted on
  another purchase order disagrees. The lookup compares the number in the normal form
  ``plx.find`` searches it by (``ontology.identifiers.normalize`` of the field's carrier
  kind on both sides, the one normaliser: ``PO#5447`` is ``PO5447``): the root relation's
  own document names in their precedence (``keys.PO_DOCUMENT_NAMES``; FM ``ponumber``),
  else the other purchase-order relation's names carried over the oracle's own link edge
  into the root relation (``fm_po.po``: FM ``id`` = ``custom_fields.fmGeneratedPoId``;
  ``po.fm_po``). A number with no normal form is never looked up (``oracle_undecided``,
  ``number_not_normalizable``). When no served row carries the number at all, the check
  reports the typed gap ``requested_po_not_in_portal`` (state ``absent_in_source``); when
  an other-relation row carries it but no served link reaches the root relation,
  ``requested_po_served_unlinked`` (state ``unknown``: the case vocabulary,
  ``operating.cases.GAP_STATES``). Either is status ``oracle_gap``, ``root_agrees`` null,
  never a pass, and only while the lines agree: a gap never masks a line count that
  differs (``disagree``, the gap reason beside ``line_count_differs``);
* the lines: the oracle's line edge from that root (``po.lines`` for a Portal purchase order,
  ``fm_po.items`` for an FM one) must give as many lines as the case cites;
* the goods subtotal (sum of quantity x unit price over Portal lines, Decimal) is compared and
  recorded when both sides have one; it is not gated (units and pricing rules may differ).

``oracle_check.status`` is ``agree`` only when root and lines agree; otherwise
``disagree`` (with ``reasons``), ``oracle_gap`` (the number names no served root row and
the lines agree),
``oracle_blocked`` (the oracle's read failed), ``oracle_undecided`` (e.g. a number with no
normal form) or ``case_failed`` (no case: ``case.missing`` names why, the generic path's
typed code such as ``missing_data:not_found`` or ``ambiguous_identifier``). The case's
``checks`` are recorded, never trusted. A Portal outage at ingestion is reported in
``blocked_upstream`` (the live-check runner records the check blocked, never a pass). Exit 0 on
``agree`` and on an ``oracle_gap`` (the top-level ``oracle_gap`` then names the gap reason,
and the live spec's ``blocked_when`` records the check blocked with cause ``source_gap``,
never a pass), 3 otherwise, 1 when the run could not start. stdout holds counts, booleans,
relation names and reason codes only: never the PO number, a row id, a line value or a
citation.

    python3 -m scripts.eval_packaging_oracle_check --state-dir D --mirror-audience A \\
        --ingest-sources S --principal staff:purchasing --po <purchase order number>

``landed-cost`` (live check ``case.packaging_landed_cost``):
one capture cycle of a host (``operating.host``: the Portal purchase-order relations and the
SharePoint containers its config names, GET only) into the state directory, the purchase
order opened through the generic path offline (``CaseOpener``), then ``plx.investigate``
``landed_cost`` on the case's root as ``--principal`` (every page), graded by the
independent oracle ``evals.oracle.landed_cost`` against the pinned rows of
``--registry`` (``scripts.case_originals.load_registry``) the case must cite (``--proforma``,
``--quote``). The oracle hashes the bytes each cited value names, read as that reader from
the capture store (a container member re-read from the container's pinned bytes by
``operating.use_case_parts.members``, the reader ``scripts/case_originals.py`` pins
members with; never the digest the handle states). An expected pin no answer entry cites is
the top-level ``case.missing`` code (``quote_not_linked_to_po``, ``pi_not_linked_to_po``;
``oracle_check.missing`` names the value a person must supply): exit 0 and the live spec's
``blocked_when`` records the check blocked, never a pass. Exit 0 on ``agree`` or
``missing``, 3 on ``disagree`` or a case that did not open, 1 when the run could not start.
stdout holds pins, codes, counts and booleans only.

    python3 -m scripts.eval_packaging_oracle_check landed-cost --state-dir D \\
        --host-config H --principal staff:operations --po <number> --registry R \\
        --proforma <pin> --quote <pin> [--quote <pin>]

``landed-estimate`` (live check ``landed_cost.estimate``): one
host cycle as ``landed-cost``, then every PurchaseOrder this reader can read whose Gold
is open by the one PO status definition (``operating.po_status``: its header word with its
own lines' summed quantities, each read through ``plx.object``/``plx.around`` as this reader;
``plx.find`` lists every page) investigated ``landed_cost`` offline as ``--principal``, the
first ``--limit`` of them. The
report states ``open_pos`` (listed), ``open_pos_total`` (only from a listing the tool says is
complete; null for a capped scan, ``listing_reason`` saying why), ``investigated`` and
``not_investigated`` (past the limit). Per purchase order
it reads whether a labelled estimate (``operating.landed_cost.ESTIMATE_LABEL``) is stated,
its freight source and assumption kinds (``ASSUMPTION_STEPS``) and the missing inputs the
estimate names (gap reason and ``who_can_answer``); stdout holds counts, codes and
``ESTIMATE_SAMPLES`` samples of those codes, never a PO number, an id or an amount. Exit 0
when the listing is complete, every listed order was investigated, and every one states an
estimate or names a missing input with who can
give it, every estimate entry is labelled and every assumption is classified, or when no
open purchase order is readable (``estimate.blocked``: the live spec records the check
blocked, never a pass); 3 otherwise; 1 when the run could not start.

    python3 -m scripts.eval_packaging_oracle_check landed-estimate --state-dir D \\
        --host-config H --principal staff:operations [--limit 500]
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence

SCHEMA = "plx-packaging-oracle-check/v1"
# Root relation -> the oracle edge that gives its lines.
LINE_EDGES = {"public.app_purchase_order": "po.lines",
              "public.generated_purchase_order": "fm_po.items"}
# The case side's own reading of its cited rows (never the oracle's edges, so the two
# cannot share a bug): root relation -> (line relation, the line's root-id column).
CASE_LINES = {"public.app_purchase_order": ("public.app_purchase_order_line",
                                            "purchase_order_id"),
              "public.generated_purchase_order": ("public.generated_purchase_order_item",
                                                  "idpogen")}
# Relation -> the fields a purchase order number is looked up by, in precedence order (the
# first field with any match decides; compared in the normal form plx.find searches by).
NUMBER_FIELDS = {"public.app_purchase_order": ("po_number", "custom_fields.renumberedTo",
                                               "custom_fields.fmPoNumber"),
                 "public.generated_purchase_order": ("ponumber",)}
NO_PORTAL_ROW = "requested_po_not_in_portal"
SERVED_UNLINKED = "requested_po_served_unlinked"
NOT_NORMALIZABLE = "number_not_normalizable"


def run_case(args: argparse.Namespace, environ: Mapping[str, str]
             ) -> tuple[int, dict[str, Any] | None]:
    """Ingest into the state directory (unless ``--ingest-sources`` is left out: then the
    directory's captures as they are), open ``petra_po=<--po>`` through the generic path
    offline and read the case's own answer back: (0, case) when a case opened, else
    (1 or 3, ``{"missing": code}``; ``blocked_upstream`` parts when the Portal could not be
    read). ``case`` holds the root row (relation, id), the count of the root's line rows
    the case cites and the case's ``checks`` (recorded, never trusted)."""
    from integrations.source_mirror import AudienceMap, parse_citation
    from ontology.core import Actor
    from operating import case_workspace_live as live_ws
    from operating.cases import CitationUnresolvable

    if args.ingest_sources is not None:
        code, ingestion = live_ws._ingest(argparse.Namespace(
            state_dir=args.state_dir, ingest_sources=args.ingest_sources,
            mirror_audience=args.mirror_audience, ingest_runs=1,
            ingest_live_audience=None), environ)
        if code != 0:
            return 1, {"missing": f"ingestion:{ingestion.get('error') or 'failed'}"}
    actor = Actor(args.tenant, frozenset(args.principal), args.clearance)
    runtime = live_ws.offline_runtime(args.state_dir, args.mirror_audience, args.tenant)
    try:
        opener = live_ws.CaseOpener(runtime, actor, args.state_dir,
                                    audience=AudienceMap.from_file(args.mirror_audience))
        opened = opener.open_case("petra_po", args.po, reinvestigate=True)
        upstream = live_ws.upstream_parts(opener.upstream)
        root = opener.root or {}
        if opened.get("status") != "opened" or not root.get("relation") \
                or not isinstance(root.get("key"), Mapping) or "id" not in root["key"]:
            return 3, {"missing": opened.get("missing") or "root_not_a_portal_row",
                       "blocked_upstream": upstream}
        lines_of = CASE_LINES.get(root["relation"])
        record = runtime.cases.get(actor, opened["case_id"])
        handles = {cite["handle"] for bucket in record["findings"].values()
                   for item in bucket for cite in item.get("citations") or []
                   if isinstance(cite, Mapping) and isinstance(cite.get("handle"), str)}
        lines: set[str] = set()
        po_cited = False
        for handle in sorted(handles):
            try:
                relation, _canonical, key, _capture = parse_citation(handle)
            except ValueError:
                continue
            if relation == root["relation"] and key == root["key"]:
                po_cited = True
            if lines_of is None or relation != lines_of[0]:
                continue
            try:
                row = runtime.resolve_citation(handle, actor).get("row") or {}
            except CitationUnresolvable:
                continue
            if str(row.get(lines_of[1])) == str(root["key"]["id"]):
                lines.add(json.dumps(key, sort_keys=True))
        return 0, {"root": {"relation": root["relation"], "id": root["key"]["id"],
                            "selected_by": "plx.find"},
                   "lines": len(lines),
                   "checks": {"po_cited": po_cited, "lines_cited": bool(lines)},
                   "blocked_upstream": upstream}
    finally:
        runtime.close()


def _number_key(relation: str, field_name: str, value: Any) -> str | None:
    """``value`` in the normal form ``plx.find`` searches the field's identifier kind by
    (``ontology.identifiers.normalize`` of the field's carrier kind), or None: the field
    carries no single kind or the value is not a valid form of it."""
    from ontology import identifiers

    kind = identifiers.carrier_kind(relation, field_name)
    if kind is None or not isinstance(value, str):
        return None
    return identifiers.normalize(kind, value)


def oracle_reading(read_relation: Callable[[str, Sequence[str]], Mapping[str, Any]],
                   relation: str, row_id: Any, po_number: str) -> dict[str, Any]:
    """The oracle's reading of one purchase order row: how many rows carry its id, how many
    rows its own lookup of ``po_number`` finds and whether that one row is this root, its
    lines over the oracle's own edge, and the Portal lines' goods subtotal (None when a line
    lacks a quantity or price). ``status`` is ``read``, ``blocked`` (a failed read) or
    ``NOT_NORMALIZABLE`` (the number has no normal form in the root relation's fields)."""
    from evals.oracle import Sources, PortalReads
    from evals.oracle import keys as K
    from evals.oracle.families import Context, Node
    from evals.oracle.source import ReadFailed, decimal_of, field_value, key_text

    edge = next((e for e in K.EDGES if e.name == LINE_EDGES.get(relation)), None)
    if edge is None:
        return {"status": "unsupported_root"}
    ctx = Context(Sources(PortalReads(read_relation), None),
                  {"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    if not any(_number_key(relation, f, po_number) for f in NUMBER_FIELDS[relation]):
        return {"status": NOT_NORMALIZABLE, "reasons": [NOT_NORMALIZABLE]}

    def named(source: str) -> list[Node]:
        try:
            rows = ctx.portal.rows(source)
        except ReadFailed as exc:
            ctx.gap("inaccessible", source,
                    "read_failed_" + "".join(c if c.isalnum() else "_"
                                             for c in exc.reason.lower())[:40])
            return []
        for field_name in NUMBER_FIELDS[source]:
            wanted = _number_key(source, field_name, po_number)
            found = [Node(source, row) for row in rows if wanted is not None
                     and _number_key(source, field_name,
                                     key_text(field_value(row, field_name))) == wanted]
            if found:
                return found
        return []

    roots = ctx.lookup(relation, "id", row_id)
    requested = named(relation)
    via = "number" if requested else None
    if not requested:
        linked: dict[str, Node] = {}
        carriers = 0
        for other in NUMBER_FIELDS:
            links = [e for e in K.EDGES if e.source == other != relation
                     and e.target == relation]
            for carrier in named(other) if links else []:
                carriers += 1
                for link in links:
                    for node in ctx.lookup(relation, link.target_field,
                                           field_value(carrier.row, link.source_field)):
                        linked.setdefault(str(node.row.get("id")), node)
        requested, via = list(linked.values()), "link" if linked else None
    else:
        carriers = 0
    lines = ctx.lookup(edge.target, edge.target_field, row_id) if len(roots) == 1 else []
    failed = [g for g in ctx.gaps if g["reason"].startswith("read_failed_")]
    if failed:
        return {"status": "blocked", "reasons": sorted({g["reason"] for g in failed})}
    subtotal: Decimal | None = None
    if edge.target == K.PO_LINE and lines:
        values = [(decimal_of(n.row.get("quantity")), decimal_of(n.row.get("unit_price")))
                  for n in lines]
        if all(q is not None and p is not None for q, p in values):
            subtotal = sum((q * p for q, p in values), Decimal(0))
    return {"status": "read", "roots": len(roots), "requested": len(requested),
            "requested_via": via, "unlinked_carriers": 0 if requested else carriers,
            "requested_is_root": len(requested) == 1
            and str(requested[0].row.get("id")) == str(row_id),
            "lines": len(lines), "subtotal": subtotal}


def _case_line_count(case: Mapping[str, Any]) -> int | None:
    """Normalize the producer's count and the legacy list shape; unknown is not zero."""
    lines = case.get("lines")
    if isinstance(lines, list):
        return len(lines)
    if isinstance(lines, int) and not isinstance(lines, bool) and lines >= 0:
        return lines
    return None


def compare(case: Mapping[str, Any] | None, code: int,
            reading: Mapping[str, Any] | None) -> dict[str, Any]:
    """``oracle_check``: agree only when the oracle finds the case's root exactly once, its
    own lookup of the requested purchase order finds exactly that row, and as many lines as
    the case cites; the case's own flags play no part. A number no served row carries is the
    typed gap ``NO_PORTAL_ROW``, one an other-relation row carries without a served link to
    the root relation ``SERVED_UNLINKED`` (``root_agrees`` None either way): the oracle
    cannot confirm the root, which is not the case rooting on another purchase order. A gap
    never masks a real mismatch: lines that differ beside a gap ``disagree``."""
    if not case or code != 0 or not isinstance(case.get("root"), Mapping):
        return {"status": "case_failed", "case_exit_code": code}
    if reading is None or reading.get("status") != "read":
        return {"status": "oracle_blocked" if (reading or {}).get("status") == "blocked"
                else "oracle_undecided",
                "reasons": list((reading or {}).get("reasons") or [(reading or {}).get(
                    "status", "not_read")])}
    case_lines = _case_line_count(case)
    gap = reading["roots"] == 1 and not reading["requested"]
    root_agrees = None if gap else reading["roots"] == 1 and reading["requested_is_root"]
    lines_agree = (reading["roots"] == 1 if gap else bool(root_agrees)) \
        and case_lines is not None and reading["lines"] == case_lines
    goods = None
    if reading.get("subtotal") is not None and case.get("goods_subtotal") is not None:
        try:
            goods = Decimal(str(case["goods_subtotal"])) == reading["subtotal"]
        except ArithmeticError:
            goods = False
    if gap:
        state, reason = (("unknown", SERVED_UNLINKED)
                         if reading.get("unlinked_carriers") else
                         ("absent_in_source", NO_PORTAL_ROW))
        return {"status": "oracle_gap" if lines_agree else "disagree", "root_rows": 1,
                "requested_rows": 0, "requested_via": None, "root_agrees": None,
                "gap": {"state": state, "reason": reason},
                "oracle_lines": reading["lines"], "case_lines": case_lines,
                "lines_agree": lines_agree, "goods_subtotal_agrees": goods,
                "reasons": [reason] if lines_agree else [reason, "line_count_differs"]}
    reasons = ([] if reading["roots"] == 1 else ["root_not_unique" if reading["roots"] else
                                                 "root_not_found"]) + \
        ([] if reading["requested_is_root"] else
         ["requested_po_not_found" if not reading["requested"] else
          "requested_po_not_unique" if reading["requested"] > 1 else
          "root_not_requested_po"]) + \
        ([] if lines_agree or not root_agrees else ["line_count_differs"])
    return {"status": "agree" if root_agrees and lines_agree else "disagree",
            "root_rows": reading["roots"], "requested_rows": reading["requested"],
            "requested_via": reading.get("requested_via"), "root_agrees": root_agrees,
            "oracle_lines": reading["lines"], "case_lines": case_lines,
            "lines_agree": lines_agree, "goods_subtotal_agrees": goods, "reasons": reasons}


def report(case: Mapping[str, Any] | None, code: int, check: Mapping[str, Any],
           evidence_label: str) -> dict[str, Any]:
    """Counts, booleans and names only (the case report itself carries values)."""
    case = case or {}
    root = case.get("root") if isinstance(case.get("root"), Mapping) else {}
    checks = case.get("checks") if isinstance(case.get("checks"), Mapping) else {}
    return {"schema": SCHEMA, "evidence_label": evidence_label,
            "adapter_status": "live-unverified",
            "blocked_upstream": list(case.get("blocked_upstream") or []),
            "case": {"exit_code": code, "ok": code == 0 and bool(root),
                     "missing": case.get("missing") if isinstance(case.get("missing"), str)
                     else None,
                     "root_relation": root.get("relation"),
                     "root_selected_by": root.get("selected_by"),
                     "lines": _case_line_count(case),
                     "checks": {k: v for k, v in checks.items() if isinstance(v, bool)},
                     "authority_violations": case.get("authority_violations")},
            "oracle_check": dict(check),
            "oracle_gap": (check.get("gap") or {}).get("reason")
            if check.get("status") == "oracle_gap" else None}


LANDED_SCHEMA = "plx-landed-cost-oracle-check/v1"
# Pages one plx.investigate answer is merged from, at most: a landed_cost answer reads at most 50
# lines (ontology/plans/landed_cost.json), measured at 34 pages for a 50-line order on a kept
# capture; past it the investigation fails with ``pages_exceeded``, never a partial answer.
INVESTIGATE_PAGES = 100


def _member_bytes(content: bytes, name: str, path: str) -> bytes | None:
    """The bytes of the container member at ``path`` (nested paths included), else None."""
    from operating.use_case_parts import members
    from scripts.case_originals import MAX_PART_BYTES

    found = members(content, name, max_part_bytes=MAX_PART_BYTES) or {}
    return next((item["data"] for item in found.get("files") or []
                 if item.get("path") == path), None)


def cited_reader(state_dir: Path, actor: Any, audience: Any
                 ) -> tuple[Callable[[str], bytes | None], Callable[[], None]]:
    """``handle -> bytes`` of a SharePoint capture citation as ``actor`` may read it (the
    store's item gate on the container), the member's bytes re-read from the container's
    pinned bytes; None when not visible, not captured or not read. With its close."""
    from integrations import capture_sharepoint as cs
    from integrations.captured_text import split_member

    if not cs.store_exists(state_dir):
        return (lambda _handle: None), (lambda: None)
    store = cs.SharePointCaptureStore.open_read_only(state_dir)

    def read(handle: str) -> bytes | None:
        member = split_member(handle)
        view = store.resolve(handle if member is None else member[0], actor, audience,
                             text=False)
        digest = None if view is None else view["row"].get("content_sha256")
        if not isinstance(digest, str) or view.get("page") is not None:
            return None
        try:
            content = store.blob(digest)
        except (cs.CaptureError, OSError):
            return None
        return content if member is None else _member_bytes(
            content, str(view["row"].get("name") or ""), member[1])
    return read, store.close


def cited_text(data: bytes) -> str | None:
    """The text of a cited original's pinned bytes: a PDF through the one PDF reading
    (``integrations.document_understanding.read_pages``, the only place that chooses the
    reader), else the bytes as UTF-8 text; None when neither reads (the oracle then fails
    the cited number closed)."""
    if data.startswith(b"%PDF"):
        from integrations.document_understanding import read_pages
        from ontology.core import ValidationError

        try:
            reading = read_pages(data, "pdf")
        except (ValidationError, ValueError, OSError):
            return None
        return reading.text if reading.status == "extracted" else None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def investigate(call: Callable[[str, dict], Mapping[str, Any]], root: str
                ) -> dict[str, Any]:
    """Every page of ``plx.investigate`` ``landed_cost`` from ``root``, merged: entries by
    name, gaps and conflicts once each; ``error`` (the tool's code) when a page failed."""
    merged: dict[str, Any] = {"answer": [], "gaps": [], "conflicts": [], "pages": 0}
    names: set[Any] = set()
    seen: set[str] = set()
    cursor = None
    while merged["pages"] < INVESTIGATE_PAGES:
        page = call("plx.investigate", {"family": "landed_cost", "root": root,
                                        **({"cursor": cursor} if cursor else {})})
        if "error" in page:
            from operating.case_workspace_live import _code
            return {**merged, "error": _code(page)}
        merged["pages"] += 1
        for entry in page.get("answer") or []:
            if isinstance(entry, Mapping) and entry.get("name") not in names:
                names.add(entry.get("name"))
                merged["answer"].append(entry)
        for key in ("gaps", "conflicts"):
            for item in page.get(key) or []:
                text = json.dumps(item, sort_keys=True, default=str)
                if text not in seen:
                    seen.add(text)
                    merged[key].append(item)
        cursor = (page.get("completeness") or {}).get("next_cursor")
        if not cursor:
            return merged
    return {**merged, "error": "pages_exceeded"}


def run_landed_cost(args: argparse.Namespace, environ: Mapping[str, str],
                    host_options: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """Capture, open and investigate (module docstring): (0, case) with the merged answer
    and a reader of its citations, else (1 or 3, ``{"missing": code}``)."""
    from integrations.source_mirror import AudienceMap
    from ontology.core import Actor
    from operating import case_workspace_live as live_ws
    from operating.case_runtime import LIVE_READER_SOURCES, build_live_readers
    from operating.host import HostConfig
    from operating.ingestion import load_live_audience

    config = HostConfig.from_file(args.host_config)
    code, capture = live_ws._host_cycle(args, environ, host_options)
    if code != 0:
        return 1, {"missing": f"capture:{capture.get('error') or 'failed'}",
                   "capture": capture}
    actor = Actor(config.tenant, frozenset(args.principal), args.clearance)
    mirror = AudienceMap.from_file(config.mirror_audience)
    # The captured units are granted per reader from the host's live audience, as the
    # host serves its sessions (``operating.host._live_arguments``).
    audience = live = None
    if config.live_audience is not None:
        audience = load_live_audience(config.live_audience, environ)
        live = build_live_readers(audience, environ, graph_sender=host_options.get("sender"),
                                  bc_sender=host_options.get("sender"),
                                  sources=sorted(set(config.sources) & set(LIVE_READER_SOURCES)))
    runtime = live_ws.offline_runtime(args.state_dir, config.mirror_audience, config.tenant,
                                      live=live)
    try:
        opener = live_ws.CaseOpener(runtime, actor, args.state_dir, audience=mirror)
        opened = opener.open_case("petra_po", args.po, reinvestigate=True)
        upstream = live_ws.upstream_parts(opener.upstream)
        root = opener.root or {}
        if opened.get("status") != "opened" or not isinstance(root.get("object_id"), str):
            return 3, {"missing": opened.get("missing") or "root_not_found",
                       "capture": capture, "blocked_upstream": upstream}
        # The markings the host config grants (``marking_grants``), as its sessions hold them.
        session = live_ws._Session(runtime, actor, mirror, opener.projection(),
                                   captures=args.state_dir,
                                   marking_grants=config.marking_grants)
        try:
            result = investigate(session, root["object_id"])
        finally:
            session.close()
    finally:
        runtime.close()
    if "error" in result:
        return 3, {"missing": f"investigate:{result['error']}", "capture": capture,
                   "blocked_upstream": upstream}
    # The markings the reader holds: those the host config grants one of its principals.
    held = sorted(marking for marking, principals in config.marking_grants.items()
                  if set(principals) & set(actor.principals))
    return 0, {"root_type": root.get("object_type"), "result": result, "capture": capture,
               "blocked_upstream": upstream, "audience": audience, "actor": actor,
               "held_markings": held}


def landed_cost_main(argv: Sequence[str], *, environ: Mapping[str, str],
                     host_options: Mapping[str, Any] | None = None) -> int:
    """The ``landed-cost`` mode (module docstring)."""
    from evals.oracle import landed_cost as oracle
    from scripts.case_originals import load_registry

    parser = argparse.ArgumentParser(
        prog="python3 -m scripts.eval_packaging_oracle_check landed-cost",
        description="A purchase order's landed cost, graded against pinned originals.")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--host-config", required=True, type=Path)
    parser.add_argument("--clearance", default="confidential")
    parser.add_argument("--principal", action="append", required=True)
    parser.add_argument("--po", required=True)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--proforma", action="append", default=[])
    parser.add_argument("--quote", action="append", default=[])
    args = parser.parse_args(argv)
    label = "live-read" if host_options is None else "injected-transport"
    try:
        registry, registry_sha = load_registry(args.registry)
        code, case = run_landed_cost(args, environ, host_options or {})
        check: dict[str, Any] = {"status": "case_failed", "reasons": [case.get("missing")]}
        if code == 0:
            read, close = cited_reader(args.state_dir, case["actor"], case["audience"])
            try:
                check = oracle.check(case["result"], read, registry, quotes=args.quote,
                                     proformas=args.proforma, text_of=cited_text,
                                     held_markings=case["held_markings"])
            finally:
                close()
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
        return 1
    result = case.get("result") or {}
    missing = case.get("missing") if code else (check.get("missing_codes") or [None])[0]
    capture = case.get("capture") or {}
    print(json.dumps({
        "schema": LANDED_SCHEMA, "evidence_label": label, "adapter_status": "live-unverified",
        "registry_sha256": registry_sha,
        "blocked_upstream": list(case.get("blocked_upstream") or []),
        "capture": {"state": capture.get("state"),
                    "sources": {name: {"state": outcome.get("state"),
                                       "complete": outcome.get("complete")}
                                for name, outcome in (capture.get("sources") or {}).items()},
                    # counts and enums only: objects, the alias join, the reviews projected
                    "projection": capture.get("projection")},
        "case": {"exit_code": code, "ok": code == 0, "missing": missing,
                 "root_type": case.get("root_type"), "pages": result.get("pages"),
                 "entries": len(result.get("answer") or []),
                 "gaps": len(result.get("gaps") or []),
                 "conflicts": len(result.get("conflicts") or [])},
        "oracle_check": check}, indent=1, sort_keys=True))
    return 0 if code == 0 and check["status"] in ("agree", "missing") else 3


ESTIMATE_SCHEMA = "plx-landed-cost-estimate-check/v1"
# An open purchase order is one ``operating.po_status`` calls open: never a word list here.
ESTIMATE_SAMPLES = 3
# plx.find pages the open-order listing reads, at most (25-50 candidates each): past it the
# listing is ``incomplete`` (``listing_pages_exceeded``), never read as every open order.
LISTING_PAGES = 200
# The engine's estimate steps (``operating.landed_cost._pre_receipt_estimate``), read as assumption kinds:
# step prefix -> kind. An ``assumption:`` step no prefix names is ``unclassified`` (the check
# fails: a new assumption is never reported as none).
ASSUMPTION_STEPS = (
    ("assumption: the header duty estimate", "duty_header_estimate_prorated"),
    ("assumption: duty at the Portal default tariffRate", "duty_portal_default"),
    ("assumption: duty rate ", "duty_recorded_by_person"),
    ("tariff: the ontology projects no duty rate", "tariff_rate_not_projected"),
    ("range: ", "duty_range_stated"))
# Estimate gaps that are this reader's limit, not an input a person can give: never counted
# as a named missing input (the executor may still name the source's owners on them).
READER_LIMIT_CODES = frozenset({"estimate_read_scope_unproven"})
FREIGHT_STEPS = (("purchase order header shipping estimate", "freight_header_estimate"),
                 ("forwarder quote total", "freight_forwarder_quote"))


def estimate_reading(result: Mapping[str, Any]) -> dict[str, Any]:
    """One open purchase order's ``plx.investigate`` answer, as codes: whether a labelled
    estimate is stated, its freight source and assumption kinds, and the missing inputs the
    estimate names (gap reason and who can answer). Never a value, a number or an id."""
    from operating.landed_cost import ESTIMATE_LABEL

    entries = [e for e in result.get("answer") or [] if isinstance(e, Mapping)
               and e.get("kind") == "computed" and isinstance(e.get("name"), str)]
    totals = [e for e in entries if e["name"].endswith("_estimate")]
    subtotals = [e for e in entries if e["name"].endswith("_estimate_subtotal")]
    labelled = all((e.get("steps") or [None])[-1] == ESTIMATE_LABEL
                   for e in entries if "_estimate" in e["name"])
    assumptions: set[str] = set()
    for entry in totals:
        for step in entry.get("steps") or []:
            if not isinstance(step, str):
                continue
            kinds = [kind for prefix, kind in ASSUMPTION_STEPS if step.startswith(prefix)]
            if kinds:
                assumptions.update(kinds)
            elif step.startswith("assumption"):
                assumptions.add("unclassified")
    freight = sorted({kind for entry in subtotals for step in entry.get("steps") or []
                      if isinstance(step, str) and step.startswith("freight: ")
                      for text, kind in FREIGHT_STEPS if text in step})
    # A null estimate may cite the run's own gap for its cause ("no estimate: <CODE> ..."),
    # e.g. PO_LINES_INCOMPLETE when no line was reached; that gap names its input too.
    cited = {step[len("no estimate: "):].split(" ", 1)[0].lower()
             for entry in totals for step in entry.get("steps") or []
             if isinstance(step, str) and step.startswith("no estimate: ")}
    missing = sorted({(str(g.get("reason")), tuple(sorted(str(w) for w in
                                                          g.get("who_can_answer") or [])))
                      for g in result.get("gaps") or [] if isinstance(g, Mapping)
                      and (str(g.get("subject") or "").startswith("estimate")
                           or str(g.get("reason")) in cited)})
    return {"estimate": any(e.get("value") is not None for e in totals),
            "estimate_entry": bool(totals), "labelled": labelled,
            "freight_source": freight[0] if len(freight) == 1 else
            "none" if not freight else "ambiguous",
            "assumptions": sorted(assumptions),
            "missing": [{"code": code, "who_can_answer": list(who)} for code, who in missing]}


PO_LINE_LINK = "PurchaseOrderLine.line_of"
LINE_PAGES = 20


def _values(page: Mapping[str, Any]) -> dict[str, list[Any]] | None:
    """A ``plx.object`` answer's values per property, raw (a text value's untrusted text,
    a quantity with its unit); None when the answer withholds or cannot read one."""
    if "error" in page or page.get("withheld") or any(
            (g or {}).get("reason") in ("unparseable_value", "uncitable_value")
            for g in page.get("gaps") or []):
        return None
    out: dict[str, list[Any]] = {}
    for value in page.get("values") or []:
        raw = value.get("value")
        if isinstance(raw, Mapping):
            raw = raw.get("untrusted_text")
        out.setdefault(str(value.get("property")), []).append(
            (raw, value.get("unit")) if value.get("value_type") == "quantity" else raw)
    return out


def _po_lines(call: Callable[[str, dict], Mapping[str, Any]], oid: str
              ) -> list[tuple[Any, Any, Any]] | None:
    """The order's own lines as ``(ordered, received, unit)`` for ``po_status.record``;
    None when the line listing is not complete for this reader. A line whose quantities
    are withheld, disagree or are in two units is ``UNREAD``."""
    from operating import po_status

    ids: list[str] = []
    cursor = None
    for _ in range(LINE_PAGES):
        page = call("plx.around", {"from": oid, "limit": 50, "path": [
            {"link_type": PO_LINE_LINK, "direction": "reverse"}],
            **({"cursor": cursor} if cursor else {})})
        if "error" in page or (page.get("withheld") or {}).get("objects"):
            return None
        ids += [o["object_id"] for o in page.get("objects") or []
                if str(o.get("object_id", "")).startswith("PurchaseOrderLine:")]
        completeness = page.get("completeness") or {}
        cursor = completeness.get("next_cursor")
        if not cursor:
            if completeness.get("complete") is not True:
                return None
            break
    else:
        return None
    lines = []
    for line in dict.fromkeys(ids):
        values = _values(call("plx.object", {"object_id": line, "properties": [
            "ordered_quantity", "received_quantity"]}))
        if values is None:
            return None
        # a stored null (the Portal's empty received cell) states no quantity: null
        ordered = {v for v in values.get("ordered_quantity", []) if v[0] is not None}
        received = {v for v in values.get("received_quantity", []) if v[0] is not None}
        if len(ordered) > 1 or len(received) > 1 or (
                ordered and received and next(iter(ordered))[1] != next(iter(received))[1]):
            lines.append((po_status.UNREAD,) * 3)
            continue
        want = next(iter(ordered), (None, None))
        got = next(iter(received), (None, None))
        lines.append((want[0], got[0], want[1]))
    return lines


def po_class(call: Callable[[str, dict], Mapping[str, Any]], oid: str):
    """One purchase order record's ``po_status.PoStatus`` as this reader reads it: its
    status values (``po_status.SOURCE_OF``) and, for an issued Portal header word, its own
    lines. Anything unread is unknown, never open."""
    from operating import po_status

    values = _values(call("plx.object", {"object_id": oid,
                                         "properties": list(po_status.SOURCE_OF)}))
    if values is None:
        return po_status.PoStatus(po_status.UNKNOWN, reason="status_missing")
    words = values.get("status", [])
    issued = len(set(words)) == 1 and po_status.header_class(words[0]) == po_status.ISSUED
    return po_status.record(values, _po_lines(call, oid) if issued else None)


def open_purchase_orders(call: Callable[[str, dict], Mapping[str, Any]],
                         classes: dict[str, int] | None = None
                         ) -> tuple[list[str], str, str | None]:
    """The PurchaseOrder object ids this reader can read with a Portal header status
    that are open by the one PO status definition (``po_class``), every ``plx.find`` page (at most ``LISTING_PAGES``):
    (ids, ``complete`` | ``incomplete``, why it is incomplete: the tool's completeness reason
    such as ``scan_cap``, its error code, or ``listing_pages_exceeded``). Only a listing the
    tool says is complete is every open order; anything else is never read as one.
    ``classes`` (when given) counts every listed record by its class (open, closed, draft,
    unknown) and each unknown one by its reason."""
    from operating import po_status

    ids: list[str] = []
    seen: set[str] = set()
    cursor, short = None, None
    for _ in range(LISTING_PAGES):
        page = call("plx.find", {
            "object_type": "PurchaseOrder", "limit": 50,
            "filters": [{"property": "status", "op": "present"}],
            **({"cursor": cursor} if cursor else {})})
        if "error" in page:
            from operating.case_workspace_live import _code
            return ids, "incomplete", _code(page)
        for candidate in page.get("candidates") or []:
            oid = (candidate.get("object") or {}).get("object_id")
            if not (isinstance(oid, str) and oid.startswith("PurchaseOrder:")) or oid in seen:
                continue
            seen.add(oid)
            state = po_class(call, oid)
            if classes is not None:
                classes[state.cls] = classes.get(state.cls, 0) + 1
                if state.cls == po_status.UNKNOWN:
                    key = f"unknown:{state.reason}"
                    classes[key] = classes.get(key, 0) + 1
            if state.cls == po_status.OPEN:
                ids.append(oid)
        completeness = page.get("completeness") or {}
        cursor = completeness.get("next_cursor")
        # A scan cut at its per-call cap (``scan_cap``) with a cursor resumes on the next
        # page; one with none ends the listing cut (the last page says so). Any other
        # shortfall is read on (every listed order is still investigated) and stays
        # incomplete.
        if completeness.get("complete") is False and completeness.get("reason") != "paged" \
                and not (completeness.get("reason") == "scan_cap" and cursor):
            short = short or str(completeness.get("reason") or "not_complete")[:64]
        if not cursor:
            if short or completeness.get("complete") is not True:
                return ids, "incomplete", short or "not_complete"
            return ids, "complete", None
    return ids, "incomplete", "listing_pages_exceeded"


def run_landed_estimate(args: argparse.Namespace, environ: Mapping[str, str],
                        host_options: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
    """One host cycle, then every open purchase order this reader can read investigated
    (``landed_cost``) offline: (0, readings) or (1, ``{"missing": code}``)."""
    from integrations.source_mirror import AudienceMap
    from ontology.core import Actor
    from operating import case_workspace_live as live_ws
    from operating.host import HostConfig

    config = HostConfig.from_file(args.host_config)
    code, capture = live_ws._host_cycle(args, environ, host_options)
    if code != 0:
        return 1, {"missing": f"capture:{capture.get('error') or 'failed'}",
                   "capture": capture}
    actor = Actor(config.tenant, frozenset(args.principal), args.clearance)
    mirror = AudienceMap.from_file(config.mirror_audience)
    runtime = live_ws.offline_runtime(args.state_dir, config.mirror_audience, config.tenant)
    readings: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        opener = live_ws.CaseOpener(runtime, actor, args.state_dir, audience=mirror)
        upstream = live_ws.upstream_parts(opener.upstream)
        session = live_ws._Session(runtime, actor, mirror, opener.projection(),
                                   captures=args.state_dir,
                                   marking_grants=config.marking_grants)
        try:
            classes: dict[str, int] = {}
            ids, complete, listed_reason = open_purchase_orders(session, classes)
            for oid in ids[:args.limit]:
                result = investigate(session, oid)
                if "error" in result:
                    errors.append(str(result["error"]))
                    continue
                readings.append(estimate_reading(result))
        finally:
            session.close()
    finally:
        runtime.close()
    held = sorted(marking for marking, principals in config.marking_grants.items()
                  if set(principals) & set(actor.principals))
    listing = "limit" if complete == "complete" and len(ids) > args.limit else complete
    return 0, {"capture": capture, "blocked_upstream": upstream, "readings": readings,
               "listed": len(ids), "listing": listing, "listing_reason": listed_reason,
               "errors": errors, "held_markings": held, "po_classes": classes}


def estimate_summary(run: Mapping[str, Any], samples: int = ESTIMATE_SAMPLES
                     ) -> dict[str, Any]:
    """Counts over the readings (``estimate_reading``) and the first ``samples`` readings
    that state an estimate or name a missing input, assumptions and codes only."""
    readings = list(run.get("readings") or [])
    def names_input(reading: Mapping[str, Any]) -> bool:
        return any(m["who_can_answer"] and m["code"] not in READER_LIMIT_CODES
                   for m in reading["missing"])
    named = [r for r in readings if names_input(r)]
    unexplained = [r for r in readings if not r["estimate"] and not names_input(r)]
    count: Callable[[Any], dict[str, int]] = lambda items: dict(sorted(
        {item: sum(1 for x in items if x == item) for item in set(items)}.items()))
    picked = [r for r in readings if r["estimate"]][:samples]
    picked += [r for r in named if r not in picked][:samples - len(picked)]
    listed = run.get("listed") or 0
    errors = list(run.get("errors") or [])
    return {
        # open_pos: the open orders listed; open_pos_total only from a complete listing (a
        # capped scan has no true count); not_investigated: listed past the limit.
        "open_pos": listed,
        "open_pos_total": listed if run.get("listing") in ("complete", "limit") else None,
        "listing": run.get("listing"), "listing_reason": run.get("listing_reason"),
        # every listed PurchaseOrder record by its operating.po_status class
        "po_classes": dict(sorted((run.get("po_classes") or {}).items())),
        "investigated": len(readings),
        "not_investigated": max(0, listed - len(readings) - len(errors)),
        "investigate_errors": sorted(set(errors)),
        "with_estimate": sum(1 for r in readings if r["estimate"]),
        "with_named_missing_input": len(named),
        "without_estimate_or_named_input": len(unexplained),
        "without_estimate_entry": sum(1 for r in readings if not r["estimate_entry"]),
        "unlabelled": sum(1 for r in readings if not r["labelled"]),
        "reader_scope_unproven": sum(1 for r in readings if any(
            m["code"] in READER_LIMIT_CODES for m in r["missing"])),
        "unclassified_assumptions": sum(1 for r in readings
                                        if "unclassified" in r["assumptions"]),
        "missing_codes": count([m["code"] for r in readings for m in r["missing"]]),
        "who_can_answer": count([w for r in readings for m in r["missing"]
                                 for w in m["who_can_answer"]]),
        "assumptions": count([a for r in readings for a in r["assumptions"]]),
        "freight_sources": count([r["freight_source"] for r in readings if r["estimate"]]),
        "samples": [{key: r[key] for key in ("estimate", "freight_source", "assumptions",
                                             "missing")} for r in picked],
        "blocked": "no_open_purchase_orders" if listed == 0 and run.get("listing") == "complete"
        else None}


def landed_estimate_main(argv: Sequence[str], *, environ: Mapping[str, str],
                         host_options: Mapping[str, Any] | None = None) -> int:
    """The ``landed-estimate`` mode (module docstring)."""
    parser = argparse.ArgumentParser(
        prog="python3 -m scripts.eval_packaging_oracle_check landed-estimate",
        description="Every open purchase order's landed-cost estimate, as counts and codes.")
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--host-config", required=True, type=Path)
    parser.add_argument("--clearance", default="confidential")
    parser.add_argument("--principal", action="append", required=True)
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 500:
        parser.error("--limit must be 1..500")
    label = "live-read" if host_options is None else "injected-transport"
    try:
        code, run = run_landed_estimate(args, environ, host_options or {})
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
        return 1
    capture = run.get("capture") or {}
    summary = estimate_summary(run) if code == 0 else None
    # Every open order the reader can read, each investigated: a capped or partial listing,
    # or an order past the limit, never passes.
    ok = bool(summary) and summary["listing"] == "complete" \
        and summary["investigated"] == summary["open_pos"] \
        and not summary["investigate_errors"] and not any(summary[key] for key in (
            "without_estimate_or_named_input", "without_estimate_entry", "unlabelled",
            "unclassified_assumptions"))
    print(json.dumps({
        "schema": ESTIMATE_SCHEMA, "evidence_label": label, "adapter_status": "live-unverified",
        "blocked_upstream": list(run.get("blocked_upstream") or []),
        "capture": {"state": capture.get("state"),
                    "sources": {name: {"state": outcome.get("state"),
                                       "complete": outcome.get("complete")}
                                for name, outcome in (capture.get("sources") or {}).items()},
                    "projection": capture.get("projection")},
        "missing": run.get("missing") if code else None,
        "held_markings": run.get("held_markings") or [],
        "estimate": summary, "ok": ok}, indent=1, sort_keys=True))
    if code:
        return code
    return 0 if ok or (summary and summary["blocked"]) else 3


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         case_runner: Callable[..., tuple[int, dict | None]] = run_case,
         read_relation: Callable[[str, Sequence[str]], Mapping[str, Any]] | None = None,
         host_options: Mapping[str, Any] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["landed-estimate"]:
        return landed_estimate_main(argv[1:],
                                    environ=os.environ if environ is None else environ,
                                    host_options=host_options)
    if argv[:1] == ["landed-cost"]:
        return landed_cost_main(argv[1:], environ=os.environ if environ is None else environ,
                                host_options=host_options)
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_packaging_oracle_check",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--mirror-audience", required=True, type=Path)
    parser.add_argument("--ingest-sources", type=Path, default=None)
    parser.add_argument("--tenant", default="plx")
    parser.add_argument("--clearance", default="internal")
    parser.add_argument("--principal", action="append", required=True)
    parser.add_argument("--po", required=True)
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    label = "live-read" if read_relation is None else "injected-transport"
    try:
        code, case = case_runner(args, env)
        root = (case or {}).get("root") if isinstance((case or {}).get("root"), Mapping) else {}
        reading = None
        if code == 0 and root:
            if read_relation is None:
                from integrations.workbench_read_api import WorkbenchReadApiClient
                from scripts.eval_oracle_dev import portal_port
                read_relation = portal_port(WorkbenchReadApiClient(
                    env.get("PLX_WORKBENCH_API_URL", ""), env.get("PLX_WORKBENCH_API_KEY", "")))
            reading = oracle_reading(read_relation, str(root.get("relation")), root.get("id"),
                                     args.po)
        check = compare(case, code, reading)
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report(case, code, check, label), indent=1, sort_keys=True))
    return 0 if check["status"] in ("agree", "oracle_gap") else 3


if __name__ == "__main__":
    raise SystemExit(main())
