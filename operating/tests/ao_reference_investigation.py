"""Reference AO investigation through the case MCP tools (test fixture only).

This is ``operating/ao_case_script.investigate`` as of the base of task allowlist-3,
moved out of product code when every product caller (``operating.case_workspace_live``,
the console's ``--open``) moved to the generic path (``plx.find``, ``plx.object``,
``plx.investigate``) and its live checks retired (devloop/ALLOWLIST-BURNDOWN.md). It stays as
the deterministic, realistic AO case the evaluator tests score: playbook
``ao_quantity_reconciliation`` v2-v4 (``test_playbook_eval``), the claim gate
(``test_claim_checks``) and the case tools' paging, originals and permission tests
(``test_ao_26005b_case``, ``test_ao_originals``). Its CLI was removed with the live checks.
Never import this outside tests.

What it does (unchanged): resolve the FM assembly order by its code through ``plx.find``,
follow the cited rows hop by hop (AO -> order-line link -> order line -> sales order),
derive planned and produced finished goods from a component consumed one per unit, compute
planned / produced / filled and ordered vs produced with ``plx.case_compute``, keep damage
per component (never one "pack" total), record lateness as an evidence conflict and paper
leads (``plx-paper-leads/v1``) as unverified leads, and attach a SharePoint original through
``operating.sharepoint_originals`` when asked. Data it cannot proceed without raises
``MissingData`` whose ``code`` names it exactly.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Any, Callable

from operating.absence_claims import record_gap
from operating.capability_guards import properties_of_fields
from operating.customer_po_coverage import parse_date_value
from operating.ontology_tools import shared_schema

# The six real packets (devloop/CONTEXT/02 section 3.1) all carry a letter suffix; a
# suffixless code is refused until a real one is confirmed. Input is case-insensitive.
AO_CODE_SHAPE = re.compile(r"AO-\d{5}[A-Z]")
# Bindings and identifier properties that name the record itself (ontology/schema/domains):
# a lot number or a PO/free-text match is never the AO, its Portal manufacturing order or
# the sales order. Each is (relation, binding, property) of the ``plx.find`` match.
AO_CODE_MATCH = ("public.assembly_order", "portal.fm_assembly_order", "ao_code")
MO_CODE_MATCH = ("public.app_manufacturing_order", "portal.app_manufacturing_order",
                 "mo_code")
ORDER_CODE_MATCH = ("public.order_entry", "portal.fm_order_entry", "fm_erp_code")


def normalise_ao_code(value: Any) -> str:
    """``value`` upper-cased and checked against ``AO_CODE_SHAPE``; ValueError otherwise."""
    code = value.strip().upper() if isinstance(value, str) else ""
    if AO_CODE_SHAPE.fullmatch(code) is None:
        raise ValueError("ao_code must look like AO-<5 digits><letter>")
    return code


def _find(call: Call, literal: str, relation: str) -> list[dict[str, Any]]:
    """Every candidate ``plx.find`` names for ``literal``, paged to the end. An answer
    that stays incomplete (a capped scan) is unreadable, never absence: ``MissingData``."""
    candidates: list[dict[str, Any]] = []
    arguments: dict[str, Any] = {"query": literal}
    while True:
        answer = call("plx.find", arguments)
        candidates.extend(answer.get("candidates", []))
        completeness = answer.get("completeness") or {}
        if completeness.get("next_cursor"):
            arguments = {"query": literal, "cursor": completeness["next_cursor"]}
        elif completeness.get("complete") is False:
            raise MissingData(relation, "find_incomplete", len(candidates))
        else:
            return candidates


def _found(candidates: list[dict[str, Any]], literal: str, match: tuple[str, str, str]
           ) -> list[dict[str, Any]]:
    """The candidates matched on exactly this binding's identifier property, one per
    cited row (each ``{"citation": ...}``). A match that is not the literal as written
    (normalised only) is a lead, never the record: ``MissingData``. Every candidate stays
    apart by its own binding and property; none is ranked or picked."""
    relation, binding, prop = match
    seen: dict[str, dict[str, Any]] = {}
    for item in candidates:
        matched = item.get("matched") or {}
        if matched.get("binding") != binding or matched.get("property") != prop:
            continue
        raw = matched.get("raw") or {}
        citation = (raw.get("origin") or {}).get("citation")
        if (item.get("reason") not in ("exact_identifier", "alias_match")
                or raw.get("untrusted_text") != literal or not citation):
            raise MissingData(relation, "code_match_not_exact", 1)
        seen.setdefault(citation, {"citation": citation})
    return list(seen.values())


# FM AO finished-good columns the Portal reads (03-PORTAL.md 4.8;
# portal/src/app/api/mrp/assembly/[id]/route.ts@8da33e4 writes qtyacc_c = total - sample;
# portal/src/app/mrp/assembly/assembly-queries.ts@8da33e4 reads both). The reporting API
# does not serve them today: a column missing from the cited row is unread, not absent.
FG_HEADER = {"totalprodqty_c": "produced finished goods including samples",
             "qtyacc_c": "accepted finished goods (produced minus samples)"}
# FG units are derived from a packaging row consumed one piece per finished unit.
PIECE, PER_FG = "pc", "pc/EA"

_PAPER_KINDS = ("component_damage", "filled", "produced_not_filled", "date_manufactured")
# Portal manufacturing-order columns divergence 6 asks for (03-PORTAL.md 3.8/4.8), by the
# spellings a served row may use; absent ones are recorded as a gap, never guessed.
MO_COLUMNS = {"qty_sample": ("qty_sample", "qtySample"),
              "qty_damaged": ("qty_damaged", "qtyDamaged"),
              "size_pieces": ("size_pieces", "sizePieces", "sizepieces")}
FM_AO_COLUMNS = {"qtytotal": ("qtytotal",), "sizepieces": ("sizepieces",),
                 "qtysample": ("qtysample", "qty_sample")}
# Roles the value claims state for these columns: those a source contract pins
# (operating/source_unit_contracts.json); the FM AO columns have none, so stay untyped.
MO_ROLES = {"qty_damaged": "damaged", "size_pieces": "planned"}
FM_AO_MEANING = {"qtytotal": "ordered at create", "sizepieces": "ordered pieces",
                 "qtysample": "samples"}

Call = Callable[[str, dict[str, Any]], dict[str, Any]]


class MissingData(LookupError):
    """Data the investigation cannot proceed without. ``code`` names it exactly as
    ``<relation>:<what>:found_<n>`` (enum-shaped, so a live-verify record keeps it)."""

    def __init__(self, relation: str, what: str, found: int) -> None:
        self.code = f"{relation}:{what}:found_{found}"
        super().__init__(f"expected exactly one {relation} row ({what}), found {found}")


def _exactly_one(rows: list[dict[str, Any]], relation: str, what: str) -> dict[str, Any]:
    if len(rows) != 1:
        raise MissingData(relation, what, len(rows))
    return rows[0]


def load_paper_leads(path: str | Path) -> dict[str, Any]:
    """[PAPER] leads of one AO (unverified; see ``examples/ao_*_paper_leads.json``).

    Labels and notes go into the case as words, so they must hold no digit (every number
    in a case statement must be in a cited row); paper values stay in the report."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != "plx-paper-leads/v1" or data.get("status") != "unverified_lead":
        raise ValueError("paper_leads_schema: expected plx-paper-leads/v1, unverified_lead")
    if "[PAPER]" not in str(data.get("provenance")) or "[PAPER]" not in str(data.get("note")):
        raise ValueError("paper_leads_provenance_required")
    if "case" in data and (not isinstance(data["case"], str)
                           or AO_CODE_SHAPE.fullmatch(data["case"]) is None):
        raise ValueError("paper_leads_case_invalid")  # the AO the leads were read from
    words = [lead.get("label") for lead in data.get("leads", [])] + [
        note for notes in (data.get("notes") or {}).values() for note in notes]
    ids = [lead.get("id") for lead in data.get("leads", [])]
    if len(set(ids)) != len(ids) or not all(isinstance(lead_id, str) and lead_id
                                            for lead_id in ids):
        raise ValueError("paper_lead_ids_must_be_unique")  # a lead id is its identity
    for lead in data.get("leads", []):
        if "unit_qty" in lead and _number(lead["unit_qty"]) is None:
            raise ValueError(f"paper_lead_unit_qty_invalid:{lead.get('id')}")
        if lead.get("kind") not in _PAPER_KINDS or not isinstance(lead.get("paper"), str):
            raise ValueError(f"paper_lead_invalid:{lead.get('id')}")
        # A component is named by its FM packaging row or by the code the paper prints.
        if lead["kind"] == "component_damage" and not (
                lead.get("packaging_id") not in (None, "") or isinstance(lead.get("code"), str)):
            raise ValueError(f"paper_lead_component_unnamed:{lead.get('id')}")
        if "page" in lead and not _page_number(lead["page"]):
            raise ValueError(f"paper_lead_page_invalid:{lead.get('id')}")
    for pages in (data.get("note_pages") or {}).values():
        if not isinstance(pages, list) or not all(_page_number(page) for page in pages):
            raise ValueError("paper_note_pages_invalid")
    if any(not isinstance(text, str) or not text.strip() or any(c.isdigit() for c in text)
           for text in words):
        raise ValueError("paper_lead_label_has_digits_or_is_empty")
    return data


def _page_number(value: Any) -> bool:
    return type(value) is int and 1 <= value <= 200


def _rows(call: Call, relation: str, where: list[list[Any]]) -> list[dict[str, Any]]:
    """Every matching row, paged to the end."""
    rows: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        page = call("mirror.rows", {"relation": relation, "where": where,
                                        "offset": offset, "limit": 500})
        rows.extend(page["rows"])
        offset = page.get("next_offset")
    return rows


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _same(first: Any, second: Any) -> bool:
    # Ids arrive as ints or strings from different relations; compare as text.
    return first is not None and str(first) == str(second)


def _text(number: Decimal) -> str:
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _compute(call: Call, case_id: str, inputs: dict[str, Any], expression: str,
             unit: str, relation: tuple[str, str, str, str] | None = None,
             recorded: dict[str, str] | None = None) -> str:
    """One recorded calculation. Planned, actual, filled and ordered are different
    quantity roles (source contracts pin all of them), so a difference of two roles
    names its relation ``(left, op, right, as)``: a label; the result's quantity type is
    the case store's type algebra. ``recorded`` maps the result to the calculation id,
    for the value claims that state it."""
    arguments = {"case_id": case_id, "inputs": inputs, "expression": expression, "unit": unit}
    if relation is not None:
        arguments["relations"] = [dict(zip(("left", "op", "right", "as"), relation))]
    calculation = call("plx.case_compute", arguments)["calculation"]
    if recorded is not None and calculation.get("calc_id"):
        recorded[calculation["result"]] = calculation["calc_id"]
    return calculation["result"]


def claim(value: Any, citation: str, field: str, role: str | None = None) -> dict[str, Any]:
    """A structured value claim (``operating.claim_checks``): the number the statement
    writes, the cited row and field it comes from, and its role when the field is a
    quantity whose source contract pins one (``identifier`` for ids)."""
    text = _text(_number(value)) if _number(value) is not None else str(value)
    out = {"value": text, "citation": citation, "field": field}
    if role is not None:
        out["role"] = role
    return out


def fg_basis(items: list[dict[str, Any]], ao_id: Any) -> tuple[dict[str, Any] | None, str]:
    """The AO item that carries finished-good units, or ``(None, why)``.

    Candidates are the AO's own packaging rows with ``qty_per_batch`` 1 (one piece per
    finished unit). They must agree on planned ``quantity`` and ``actual_amount``; the
    lowest id is cited. Bulk rows (kg) and fractional components (boxes and case labels,
    rounded up when planned) are never the basis."""
    unit_ratio = [item for item in items
                  if _same(item["row"].get("assembly_order_id"), ao_id)
                  and _number(item["row"].get("qty_per_batch")) == 1]
    candidates = [item for item in unit_ratio
                  if item["row"].get("packaging_id") not in (None, "")]
    if not candidates:
        return None, "no packaging row of the AO is consumed one per finished unit"
    # Every unit-ratio row must agree (playbook v2 check basis_row_agrees does the same).
    values = {(_number(item["row"].get("quantity")), _number(item["row"].get("actual_amount")))
              for item in unit_ratio}
    if len(values) != 1 or None in next(iter(values)):
        return None, ("the packaging rows consumed one per finished unit disagree on planned "
                      "or actual quantity, or one is empty")
    return min(candidates, key=lambda item: _number(item["row"].get("id")) or 0), ""


def component_actual_agrees(row: dict[str, Any], produced: Decimal) -> bool:
    """Does a component row's actual_amount match ``produced`` finished goods?

    FM writes ``qty_per_batch x produced``; the Portal's cascade
    (``cascadeAssemblyActualQty``, portal/src/lib/inventory/assembly-lifecycle.ts@8da33e4)
    writes the same rounded to 4 places, except box-type packaging rows (packaging,
    no bulk, qty_per_batch < 1), which it floors to whole boxes after rounding to 6
    places. Either writer's value agrees; anything else implies another produced figure."""
    per_unit, actual = _number(row.get("qty_per_batch")), _number(row.get("actual_amount"))
    if not per_unit or actual is None:
        return False
    exact = per_unit * produced
    if actual == exact or actual == exact.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP):
        return True
    boxed = (row.get("packaging_id") not in (None, "") and
             row.get("bulk_product_id") in (None, "") and per_unit < 1)
    return boxed and actual == exact.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP).to_integral_value(
        rounding=ROUND_FLOOR)


def _packaging_codes(call: Call, items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """AO item id -> its packaging row's code (``public.packaging.packcode``) and
    citation, read for the report only (the v2 playbook declares no packaging role, so
    the case does not cite these rows). Unreadable -> no entry."""
    codes: dict[str, dict[str, Any]] = {}
    for item in items:
        packaging = item["row"].get("packaging_id")
        if packaging in (None, ""):
            continue
        try:
            rows = _rows(call, "public.packaging", [["id", "eq", packaging]])
        except RuntimeError:  # relation not granted or not served: the code stays unread
            return codes
        if len(rows) == 1 and rows[0]["row"].get("packcode"):
            codes[str(item["row"].get("id"))] = {"code": rows[0]["row"]["packcode"],
                                                 "citation": rows[0]["citation"],
                                                 "packaging_id": packaging}
    return codes


def resolve_chain(call: Call, ao_code: str, order_code: str | None = None) -> dict[str, Any]:
    """The AO, its order link, line and sales order, each read by the identity the previous
    cited row carries (never by remembered ids).

    The AO is found by its code through ``plx.find`` (exact identifier match; the live
    fallback pages the relation in when it is not yet projected), on ``nameamo_c`` only
    (never ``lotno``); its Portal manufacturing order on ``mo_code`` only (never
    ``lot_number``); ``order_code`` on the FM ``erpcode`` only. Its
    ``order_entry_item_assembly`` links lead to the order lines and the line's ``idoe`` to
    the sales order. ``order_code``, when given, is resolved the same way and selects the
    link of that order (an AO can serve several); a link whose line belongs to another
    order then stays visible as a contradiction. Without it, an AO linked to several lines
    is refused (``MissingData``) and an AO with no link has ``link``/``line``/``order`` None,
    recorded by the case as a gap."""
    ao_found = _find(call, ao_code, AO_CODE_MATCH[0])
    ao = _exactly_one(_found(ao_found, ao_code, AO_CODE_MATCH), AO_CODE_MATCH[0], "code_match")
    mo_candidates = _found(ao_found, ao_code, MO_CODE_MATCH)
    ao_row = call("plx.source_citation", {"citation": ao["citation"]})["row"]
    ao_id = ao_row.get("id")
    links = _rows(call, "public.order_entry_item_assembly", [["idassembly", "eq", ao_id]])
    lines = [_exactly_one(_rows(call, "public.order_entry_item",
                                [["id", "eq", link["row"].get("idoeitem")]]),
                          "public.order_entry_item", "line_of_link") for link in links]
    pairs = list(zip(links, lines))
    order: dict[str, Any] | None = None
    order_row: dict[str, Any] = {}
    link = line = None
    if order_code is not None:
        order = _exactly_one(
            _found(_find(call, order_code, ORDER_CODE_MATCH[0]), order_code, ORDER_CODE_MATCH),
            ORDER_CODE_MATCH[0], "code_match")
        order_row = call("plx.source_citation", {"citation": order["citation"]})["row"]
        if len(pairs) > 1:  # one AO can serve several order lines: take the one of this order
            pairs = [(link, line) for link, line in pairs
                     if _same(line["row"].get("idoe"), order_row.get("id"))]
        if pairs:  # no link at all: the same order_link_absent gap as without --order
            link, line = _exactly_one(pairs, "public.order_entry_item_assembly",
                                      "link_to_order")
    elif pairs:
        link, line = _exactly_one(pairs, "public.order_entry_item_assembly", "link_of_ao")
        order = _exactly_one(_rows(call, "public.order_entry",
                                   [["id", "eq", line["row"].get("idoe")]]),
                             "public.order_entry", "order_of_line")
        order_row = order["row"]
    return {"ao": ao, "ao_row": ao_row, "ao_id": ao_id, "mo_candidates": mo_candidates,
            "link": link, "line": line, "order": order, "order_row": order_row}


def investigate(call: Call, paper: dict[str, Any] | None = None,
                originals: dict[str, Any] | None = None, *, ao_code: str,
                order_code: str | None = None) -> dict[str, Any]:
    """Resolve and cite the AO's rows, open the case, compute, record facts, gaps and
    conflicts.

    ``ao_code`` is the FM AO code (``AO-<5 digits><letter>``); ``order_code`` optionally
    names the sales order (see ``resolve_chain``). ``paper`` is a loaded [PAPER] leads file
    (``load_paper_leads``) or None. ``originals`` (``sharepoint_originals.folder_source``)
    asks the case to attach the AO's packet from SharePoint through
    ``operating.sharepoint_originals`` (the read-only reader the host configured); its pages are
    then cited beside the FM rows in every paper/FM evidence conflict. None: the packet is
    recorded as not attached. Raises ``MissingData`` naming what it could not read."""
    requested = normalise_ao_code(ao_code)
    if paper is not None and paper.get("case") not in (None, requested):
        raise ValueError("paper_leads_of_another_ao")
    resolved = resolve_chain(call, requested, order_code)
    ao, ao_row, ao_id = resolved["ao"], resolved["ao_row"], resolved["ao_id"]
    # Statements name the AO as FM stores it (the match is case-insensitive); the packet
    # file is looked up by the canonical upper-case code its file name carries.
    ao_code = str(ao_row.get("nameamo_c") or requested)
    mo_candidates = resolved["mo_candidates"]
    link, line, order = resolved["link"], resolved["line"], resolved["order"]
    order_row = resolved["order_row"]
    order_label = order_code or order_row.get("erpcode") or (
        f"sales order {order_row.get('id')}" if order is not None else None)
    items = _rows(call, "silver.assembly_order_items", [["assembly_order_id", "eq", ao_id]])
    basis, basis_problem = fg_basis(items, ao_id)
    filling = _exactly_one(_rows(call, "silver.production_tasks",
                                 [["mo_id", "eq", ao_id], ["task_type", "eq", "Filling"]]),
                           "silver.production_tasks", "Filling")
    clearance = _rows(call, "silver.production_tasks",
                      [["mo_id", "eq", ao_id], ["task_type", "eq", "Line clearance"]])
    damage = _rows(call, "silver.assembly_order_damage", [["assembly_order_id", "eq", ao_id]])
    order_root = order_code or order_row.get("erpcode")
    roots = [ao_code] + ([order_root] if order_root else [])
    case_id = call("plx.case_open", {
        "question": f"Was {ao_code} produced as ordered for "
                    + (order_label or "its sales order")
                    + " (planned vs produced vs filled finished goods, component damage, "
                    "and on time)?",
        "roots": roots})["case_id"]
    cite = {"ao": ao["citation"], "order": order["citation"] if order else None,
            "link": link["citation"] if link else None,
            "line": line["citation"] if line else None,
            "item": basis["citation"] if basis else None,
            "filling": filling["citation"], "damage": [row["citation"] for row in damage],
            "clearance": [row["citation"] for row in clearance], "mo": None}
    chain = {"link_ao": link["row"].get("idassembly") if link else None,
             "link_line": link["row"].get("idoeitem") if link else None,
             "line_id": line["row"].get("id") if line else None,
             "line_order": line["row"].get("idoe") if line else None,
             "ao_id": ao_id, "item_ao": basis["row"].get("assembly_order_id") if basis else None,
             "order_id": order_row.get("id")}
    ao_consistent = _same(ao_id, chain["link_ao"])
    order_consistent = (ao_consistent and _same(chain["link_line"], chain["line_id"]) and
                        _same(chain["line_order"], chain["order_id"]))
    # Served and empty only: a column the row does not carry is unread, never empty.
    header_empty = {column: column in ao_row and ao_row[column] in (None, "")
                    for column in ("qtytotal", "finalqty")}
    # Portal FG columns: served with a value, served but empty, or not served (unread).
    fg_header = {column: ("unread" if column not in ao_row else
                          "empty" if ao_row.get(column) in (None, "") else "read")
                 for column in FG_HEADER}
    mo_row = (call("plx.source_citation", {"citation": mo_candidates[0]["citation"]})["row"]
              if len(mo_candidates) == 1 else None)
    original: dict[str, Any] | None = None
    if originals is not None:
        from operating.sharepoint_originals import attach_original
        original = attach_original(call, case_id, requested, drive_id=originals["drive_id"],
                                   folder=originals["folder"],
                                   name_pattern=originals["name_pattern"],
                                   expected=originals.get("expected"))

    gap_codes: list[str] = []

    def finding(statement: str, citations: list[str], status: str = "supported",
                claims: list[dict[str, Any]] | None = None) -> None:
        arguments = {"case_id": case_id, "kind": "fact", "status": status,
                     "statement": statement, "citations": citations}
        if claims:
            arguments["value_claims"] = claims
        call("plx.case_add_finding", arguments)

    def gap(state: str, missing: str, why: str, who: list[str], citations: list[str], *,
            code: str, claims: list[dict[str, Any]] | None = None,
            fields: tuple[str, list[str]] | None = None) -> None:
        # fields: (relation, columns) an absence names; the schema says which
        # properties they carry (the tool requires property for absent_in_source). A
        # column the schema binds to no property cannot be a structured absence: unknown.
        names = properties_of_fields(shared_schema(), *fields) if fields else []
        if (fields and len(names) < len(fields[1])) or (
                state == "absent_in_source" and not citations):
            # An absence citing nothing it is absent from is refused by the gap guard.
            state = "unknown"
        arguments = {"case_id": case_id, "state": state, "missing": missing, "why": why,
                     "who_can_answer": who, "citations": citations}
        if names and state == "absent_in_source":
            arguments["property"] = names
        if claims:
            arguments["value_claims"] = claims
        record_gap(call, arguments)  # a refused absence is recorded unknown, never dropped
        gap_codes.append(code)  # report only: which gap, enum-shaped

    ident = "identifier"
    # Where each chain value was read (for the value claims of statements that quote it).
    chain_source = {"link_ao": (cite["link"], "row.idassembly"),
                    "link_line": (cite["link"], "row.idoeitem"),
                    "line_id": (cite["line"], "row.id"), "line_order": (cite["line"], "row.idoe"),
                    "ao_id": (cite["ao"], "row.id"),
                    "item_ao": (cite["item"], "row.assembly_order_id"),
                    "order_id": (cite["order"], "row.id")}

    def chain_claims(*names: str) -> list[dict[str, Any]]:
        return [claim(chain[name], *chain_source[name], ident) for name in names
                if chain[name] not in (None, "") and chain_source[name][0]]

    planning = ["Production planning", "Customer service (Vince)"]
    quality = ["Ricardo (Quality)", "Production planning"]
    order_cites = [handle for handle in (cite["ao"], cite["link"], cite["line"], cite["order"])
                   if handle]
    if order_consistent:
        finding(f"{ao_code} (FM AssemblyOrder {ao_id}) is linked by order line "
                f"{chain['line_id']} to sales order {chain['order_id']} ({order_label}).",
                order_cites, claims=chain_claims("ao_id", "line_id", "order_id"))
    elif ao_consistent:
        finding(f"{ao_code} is linked to {order_label}: the cited link row points to order line "
                f"{chain['link_line']}, which belongs to sales order {chain['line_order']}, "
                f"while {order_label} resolves to sales order {chain['order_id']}.",
                order_cites, "contradicted",
                claims=chain_claims("link_line", "line_order", "order_id"))
    if link is None:
        gap("unknown", f"Which sales order {ao_code} was produced for",
            "The mirror read found no order_entry_item_assembly row linking this AO to an "
            "order line (the read is non-atomic and does not prove that none exists), so "
            "order quantities and dates are not compared with this AO.", planning,
            [cite["ao"]], code="order_link_absent")
    elif not order_consistent:
        gap("unknown", f"Which sales order {ao_code} was produced for",
            "The cited AO, link, line and order rows do not form one chain ("
            + ", ".join(f"{name}={value}" for name, value in chain.items()) + "), so order "
            "quantities and dates are not compared with this AO.", planning,
            [handle for handle in (*order_cites, cite["item"]) if handle],
            code="order_chain_inconsistent", claims=chain_claims(*chain))

    results: dict[str, str] = {}
    unfilled_calc: dict[str, str] = {}  # produced - filled -> its calculation id
    damage_calc: dict[str, str] = {}  # damage unit -> the calculation id of its sum
    planned: dict[str, Any] = {"derived": None, "sources": {}, "agree": [], "differ": []}
    produced: Decimal | None = None
    qc_retain = False
    if basis is None:
        gap("unknown", f"The finished-good quantities {ao_code} planned and produced",
            f"Not derived: {basis_problem}. Component rows are never taken as finished-good "
            "units, and the AO header quantities are read from the cited AO row.",
            ["Ricardo (Quality)", "Production planning"], [cite["ao"]],
            code="fg_quantities_not_derived")
    elif link is None or ao_consistent:
        row = basis["row"]
        planned_in = {"citation": cite["item"], "field": "row.quantity", "unit": PIECE}
        produced_in = {"citation": cite["item"], "field": "row.actual_amount", "unit": PIECE}
        per_unit = {"citation": cite["item"], "field": "row.qty_per_batch", "unit": PER_FG}
        results["planned_minus_produced"] = _compute(call, case_id, {
            "planned": planned_in, "produced": produced_in, "per_unit": per_unit},
            "planned / per_unit - produced / per_unit", "EA",
            ("planned", "-", "actual", "planned_not_produced"))
        results["produced_minus_filled"] = _compute(call, case_id, {
            "produced": produced_in, "per_unit": per_unit,
            # A Filling task's qty: pinned "filled" by a conditional source contract.
            "filled": {"citation": cite["filling"], "field": "row.qty", "unit": "EA",
                       "role": "filled"}},
            "produced / per_unit - filled", "EA",
            ("actual", "-", "filled", "produced_not_filled"), recorded=unfilled_calc)
        produced = _number(row.get("actual_amount")) / _number(row.get("qty_per_batch"))
        planned = planned_fg(basis, ao_row, mo_row, cite["ao"],
                             mo_candidates[0]["citation"] if mo_row is not None else None)
        finding(f"{ao_code}'s finished-good quantities are derived from its component row "
                f"{row.get('id')}, consumed one per finished unit (qty_per_batch "
                f"{_text(_number(row.get('qty_per_batch')))}): planned "
                f"{_text(_number(row.get('quantity')))} and produced "
                f"{_text(_number(row.get('actual_amount')))} including samples (FM actual_amount "
                "is qty_per_batch times the produced finished goods: derived, not counted)."
                + (" The planned figure equals the " + " and the ".join(
                    label for label, _ in planned["agree"]) + ", the Portal's planned "
                   "production quantity (component quantity is qty_per_batch times it)."
                   if planned["agree"] else ""),
                [cite["ao"], cite["item"], *[handle for _, handle in planned["agree"]
                                             if handle != cite["ao"]]],
                claims=[claim(row.get("id"), cite["item"], "row.id", ident),
                        claim(row.get("qty_per_batch"), cite["item"], "row.qty_per_batch"),
                        claim(row.get("quantity"), cite["item"], "row.quantity", "planned"),
                        claim(row.get("actual_amount"), cite["item"], "row.actual_amount",
                              "actual")])
        if planned["differ"]:
            gap("unknown", f"Which planned finished-good figure of {ao_code} is right: "
                "evidence conflict", "The Portal plans component quantity as qty_per_batch "
                "times size in pieces, but the " + " and the ".join(
                    label for label, _ in planned["differ"]) + " differ from the planned "
                "figure derived from the cited component row.", quality,
                [cite["item"], *[handle for _, handle in planned["differ"]]],
                code="planned_fg_conflict")
        link_made = _number(link["row"].get("qtymfg_c")) if link else None
        # qtyplan_c is the same kind of FM link calc (03-PORTAL.md 4.8); never "planned".
        plan_note = (" Its qtyplan_c is likewise an FM link-table calculation no Portal code "
                     "reads, not the planned figure." if link and "qtyplan_c" in link["row"]
                     else "")
        if link is None:
            pass  # no link row: the order-link gap already says so
        elif link_made is not None and link_made == produced:
            finding(f"The AO link's qtymfg_c ({_text(link_made)}), an FM link-table calculation "
                    "no Portal code reads, equals the produced finished goods derived from "
                    f"component row {row.get('id')}." + plan_note, [cite["link"], cite["item"]],
                    claims=[claim(link_made, cite["link"], "row.qtymfg_c", "manufactured"),
                            claim(row.get("id"), cite["item"], "row.id", ident)])
        else:
            gap("unknown", f"Which produced figure of {ao_code} is right: evidence conflict",
                "The AO link's qtymfg_c (an FM link-table calculation no Portal code reads) "
                "is empty or differs from the produced finished goods derived from the cited "
                "component row.", quality, [cite["link"], cite["item"]],
                code="produced_link_conflict")
        filled = _number(filling["row"].get("qty"))
        cleared = [_number(row["row"].get("qty")) for row in clearance]
        if filled is not None and produced != filled:
            if clearance and None not in cleared and sum(cleared, Decimal(0)) == produced - filled:
                # Samples are produced but not filled (Portal: accepted = produced -
                # samples); a sample count equal to the unfilled units names them.
                sample, sample_cite = _sample(ao_code, mo_row, mo_candidates, ao_row,
                                              cite["ao"])
                retain = sample is not None and sample == produced - filled
                qc_retain = retain
                finding(f"FM accounts for every finished unit {ao_code} produced: the produced "
                        f"figure derived from component row {row.get('id')} minus the Filling "
                        f"task's {_text(filled)} equals what its Line clearance task row(s) "
                        f"record ({', '.join(_text(value) for value in cleared)}), units taken "
                        "at line clearance rather than filled."
                        + (f" The {sample_cite[0]} records the same number of samples "
                           f"({_text(sample)}), so the unfilled unit is the AO's retained "
                           "sample (a QC retain sample), not a loss." if retain else ""),
                        [cite["item"], cite["filling"], *cite["clearance"]]
                        + ([sample_cite[1]] if retain else []),
                        claims=[claim(row.get("id"), cite["item"], "row.id", ident),
                                claim(filled, cite["filling"], "row.qty", "filled"),
                                *[claim(value, task["citation"], "row.qty")
                                  for value, task in zip(cleared, clearance)]]
                        + ([claim(sample, sample_cite[1], f"row.{sample_cite[2]}")]
                           if retain else []))
            else:
                gap("unknown", f"What the finished goods of {ao_code} produced but not filled "
                    "were", "The cited Filling task records a different count than the "
                    "produced finished goods derived from the cited component row, and no "
                    "cited FM row (including any Line clearance task) accounts for the "
                    "difference.", quality, [cite["item"], cite["filling"], *cite["clearance"]],
                    code="unfilled_unaccounted")
        disagreeing = [item for item in items if _number(item["row"].get("qty_per_batch"))
                       and not component_actual_agrees(item["row"], produced)]
        if disagreeing:
            gap("unknown", f"Whether every component of {ao_code} consumed the same produced "
                "finished goods: evidence conflict",
                "FM's actual_amount is qty_per_batch times the produced finished goods (the "
                "Portal floors box-type rows to whole boxes), but these cited component rows "
                "imply a different produced figure than the cited basis row.", quality,
                [cite["item"], *[item["citation"] for item in disagreeing][:18]],
                code="component_produced_conflict")
    fg_values = fg_header_findings(ao_code, ao_row, fg_header, produced, cite, finding, gap,
                                   quality)

    # Damage per component, each in its own unit: never one "pack" total.
    by_item: dict[str, list[dict[str, Any]]] = {}
    for row in damage:
        by_item.setdefault(str(row["row"].get("assembly_item_id")), []).append(row)
    damaged: dict[str, str] = {}
    damage_claims: list[dict[str, Any]] = []
    for item_id, rows in sorted(by_item.items()):
        unit = f"item{item_id}" if item_id.isdigit() else "item_unknown"
        ids: dict[str, str] = {}
        damaged[unit] = _compute(call, case_id, {
            f"d{index}": {"citation": row["citation"], "field": "row.qty_damaged", "unit": unit}
            for index, row in enumerate(rows)},
            " + ".join(f"d{index}" for index in range(len(rows))), unit, recorded=ids)
        if damaged[unit] in ids:
            damage_calc[unit] = ids[damaged[unit]]
            damage_claims.append({"value": damaged[unit], "calculation": ids[damaged[unit]],
                                  "role": "damaged", "unit": unit})
        damage_claims.append(claim(rows[0]["row"].get("assembly_item_id"), rows[0]["citation"],
                                   "row.assembly_item_id", ident))
    if damage:
        finding("FM records damage per component, each in that component's own unit, so the "
                "rows are never summed into one total: " + "; ".join(
                    f"AO item {unit[4:]}: {value}" for unit, value in damaged.items()) +
                " (damage_type names the damage category, not a shared unit).", cite["damage"],
                claims=damage_claims)
    else:
        gap("unknown", f"Whether {ao_code} recorded any component damage",
            "The mirror read found no silver.assembly_order_damage row for this AO; the read "
            "is non-atomic, so that is not evidence of zero damage.", quality, [cite["ao"]],
            code="damage_rows_absent")
    # The same groups keyed by the component's packaging code (the code the paper prints);
    # codes are read for the report, an unreadable code keeps the AO item unit.
    codes = _packaging_codes(call, items)
    damaged_by_component = [{"unit": unit, "assembly_item_id": unit[4:], "damaged": value,
                             "code": (codes.get(unit[4:]) or {}).get("code"),
                             "packaging_citation": (codes.get(unit[4:]) or {}).get("citation")}
                            for unit, value in damaged.items()]
    # One entry per AO item row: a packaging code printed on two items (one AO packet lists
    # the same label code twice) keeps both rows, never one overwriting the other. The
    # code-keyed dict holds a bare code only when it is unique among the items.
    code_counts: dict[str, int] = {}
    for entry in damaged_by_component:
        key = entry["code"] or entry["unit"]
        code_counts[key] = code_counts.get(key, 0) + 1
    damaged_by_code = {}
    for entry in damaged_by_component:
        key = entry["code"] or entry["unit"]
        damaged_by_code[key if code_counts[key] == 1 else f"{key} ({entry['unit']})"] = \
            entry["damaged"]

    related: list[dict[str, Any]] = []
    line_produced: dict[str, Any] = {"state": "not_compared"}
    if order_consistent and basis is not None:
        results["ordered_minus_produced"] = _compute(call, case_id, {
            "ordered": {"citation": cite["line"], "field": "row.qty", "unit": "EA"},
            "produced": {"citation": cite["item"], "field": "row.actual_amount", "unit": PIECE},
            "per_unit": {"citation": cite["item"], "field": "row.qty_per_batch",
                         "unit": PER_FG}},
            "ordered - produced / per_unit", "EA",
            ("ordered", "-", "actual", "ordered_not_produced_by_this_ao"))
    if order_consistent:
        for other in _rows(call, "public.order_entry_item_assembly",
                           [["idoeitem", "eq", line["row"].get("id")]]):
            if _same(other["row"].get("idassembly"), ao_id):
                continue
            found = _rows(call, "public.assembly_order",
                          [["id", "eq", other["row"].get("idassembly")]])
            related.append({"link_citation": other["citation"],
                            "ao_id": other["row"].get("idassembly"),
                            "ao_code": found[0]["row"].get("nameamo_c") if found else None,
                            "ao_citation": found[0]["citation"] if found else None,
                            "link_qtymfg_c": other["row"].get("qtymfg_c")})
        named = ", ".join(sorted(str(item["ao_code"] or "an AO whose row was not found")
                                 for item in related))
        line_produced = order_line_produced(line["row"])
        gap("unknown", f"Whether the rest of order line {chain['line_id']} was produced by "
            "other assembly orders",
            f"Ordered minus produced compares {ao_code} with the whole order line, so it is "
            "this AO against the line, not a proven shortfall. "
            + (f"Other assembly orders linked to the same line, reported as related and not "
               f"merged: {named}." if related else
               "The mirror read found no other assembly order linked to this line; that read "
               "is non-atomic and does not prove absence.")
            + (" The Portal's produced figure for the line (order_entry_item qtyaomfg_c plus "
               "qtybcmfg_c, its orders view) is on the cited line row and is reported."
               if line_produced["state"] == "read" else
               " The Portal's produced figure for the line (order_entry_item qtyaomfg_c plus "
               "qtybcmfg_c, its orders view) was not read: "
               + ("the cited line row does not serve those columns." if
                  line_produced["state"] == "unread" else "it is empty on the cited line row.")),
            planning, [cite["line"], cite["order"]] + ([cite["item"]] if basis else []),
            code="order_line_rest", claims=chain_claims("line_id"))

    dates = {"required": order_row.get("datereq"), "manufactured": ao_row.get("datemanuf"),
             "filled": filling["row"].get("date_start")}
    # Calendar dates, never strings (same parser as the customer-PO date filter).
    parsed = {name: parse_date_value(value) for name, value in dates.items()}
    days = {name: (value.date() if isinstance(value, datetime) else value)
            for name, value in parsed.items()}
    unreadable = sorted(name for name, value in days.items() if value is None)
    fm_late: bool | None = None
    if not order_consistent:
        pass  # the chain gap already says order dates are not compared
    elif unreadable:
        gap("unknown", f"Whether {ao_code} was produced by the required date",
            "These dates on the cited rows are empty or not ISO dates, so on-time delivery "
            "cannot be judged: " + ", ".join(unreadable) + ".", planning,
            [cite["ao"], cite["filling"], cite["order"]], code="dates_unreadable")
    else:
        fm_late = (days["manufactured"] > days["required"] or
                   days["filled"] > days["required"])
    attached = original is not None and original.get("status") == "attached"
    if fm_late:
        # Not a lateness fact: the dates that say late are themselves in conflict.
        po = order_row.get("custpocode")
        dated = [lead for lead in (paper_leads(paper, items, damage, filling, ao_row, produced,
                                               codes) if paper else [])
                 if lead["kind"] == "date_manufactured"]
        notes = (paper or {}).get("notes", {}).get("lateness", [])
        pages = sorted({lead["page"] for lead in dated if lead.get("page")} |
                       set((paper or {}).get("note_pages", {}).get("lateness", [])))
        paper_cites = _original_cites(original, pages) if attached else []
        gap("unknown", f"Whether {ao_code} was late against the governing required date: "
            "evidence conflict",
            f"FM dates say late: the AO records date manufactured "
            f"{days['manufactured'].isoformat()} and the Filling task "
            f"{days['filled'].isoformat()}, while the sales order requires "
            f"{days['required'].isoformat()}"
            + (f" and holds customer PO {po}" if po else "") + "."
            + (" The AO's date manufactured is not its Filling task's date, so it may be a "
               "plan date rather than the production date."
               if days["manufactured"] != days["filled"] else "")
            + (" Paper leads (unverified" + (", the original's pages are cited" if paper_cites
                                              else ", original not attached") + "): "
               + "; ".join([f"{lead['label']}: {'agrees' if lead['agrees'] else 'disagrees'}"
                            for lead in dated if lead["agrees"] is not None] + list(notes)) + "."
               if dated or notes else "")
            + " Which required date governs, and when the goods were produced, need the "
            "paper original and Production planning.", planning,
            [cite["ao"], cite["filling"], cite["order"], *paper_cites], code="lateness_conflict",
            claims=[claim(days["manufactured"].isoformat(), cite["ao"], "row.datemanuf"),
                    claim(days["filled"].isoformat(), cite["filling"], "row.date_start"),
                    claim(days["required"].isoformat(), cite["order"], "row.datereq")])
    if all(header_empty.values()):
        gap("absent_in_source", f"{ao_code} header quantities (qtytotal, finalqty)",
            "Both are empty on the cited AO row, so the AO's ordered-at-create figure is "
            "not stated there and its planned and produced finished-good quantities are "
            "derived from a component row (quantity or actual_amount divided by "
            "qty_per_batch).", quality, [cite["ao"]], code="header_quantities_empty",
            fields=(AO_CODE_MATCH[0], list(header_empty)))

    mo = manufacturing_order(call, ao_code, mo_candidates, ao_row, cite, finding, gap, quality)
    leads = paper_leads(paper, items, damage, filling, ao_row, produced, codes) if paper else []
    if leads:
        disagree = [lead["label"] for lead in leads if lead["agrees"] is False]
        agree = [lead["label"] for lead in leads if lead["agrees"] is True]
        unread = [lead["label"] for lead in leads if lead["agrees"] is None]
        compared = ("Compared with the cited FM rows, the paper leads disagree on: "
                    + ("; ".join(disagree) or "nothing") + ". They agree on: "
                    + ("; ".join(agree) or "nothing") + "."
                    + (" Not comparable from the cited rows: " + "; ".join(unread) + "."
                       if unread else ""))
    else:
        compared = "No paper leads were supplied to this investigation."
    conflicts: list[dict[str, Any]] = []
    if attached:
        def fm_claims(lead: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
            """The value claims of a lead's FM figure, and any citation they need."""
            fm, cites = lead["fm"], lead["fm_citations"]
            if lead["kind"] == "component_damage" and cites:
                rows = [row for row in damage if row["citation"] in cites]
                if len(rows) == 1:
                    return [claim(fm, rows[0]["citation"], "row.qty_damaged", "damaged")], []
                unit = f"item{rows[0]['row'].get('assembly_item_id')}" if rows else ""
                if damaged.get(unit) == fm and unit in damage_calc:
                    return [{"value": fm, "calculation": damage_calc[unit], "role": "damaged",
                             "unit": unit}], []
            if lead["kind"] == "filled" and fm is not None:
                return [claim(fm, cite["filling"], "row.qty", "filled")], []
            if lead["kind"] == "date_manufactured" and fm is not None:
                return [claim(fm, cite["ao"], "row.datemanuf")], []
            if lead["kind"] == "produced_not_filled" and fm in unfilled_calc and cite["item"]:
                return [{"value": fm, "calculation": unfilled_calc[fm],
                         "role": "produced_not_filled", "unit": "EA"}], [cite["item"]]
            return [], []

        conflicts = original_conflicts(call, leads, original, cite["ao"], gap, quality,
                                       fm_claims)
        gap("unknown", f"The paper {ao_code} packet's figures, to be confirmed by Quality",
            "The packet's SharePoint original is attached and cited (its SHA-256 is "
            "re-checked on every read). Its pages are scans, so a figure read from them is "
            "machine-read or handwritten and stays a lead until a person confirms it; each "
            "disagreement is recorded as an evidence conflict citing the FM rows and the "
            "original's page. " + compared, quality,
            [original["citation"], cite["ao"], *cite["damage"]], code="paper_figures_to_confirm")
    else:
        tried = ("" if original is None else
                 f" Reading it from SharePoint was attempted: {original.get('status')}.")
        gap("not_ingested", f"The paper {ao_code} packet original (scan) is not attached",
            "Its SharePoint original (the QCQA site's AO folder) was not attached to this case; attach "
            "it with plx.case_attach_original drive_id + item_id (or an operator copy "
            f"with inbox_name).{tried} Until then its figures are unverified leads "
            "(the context pack's offline OCR, in the investigation report's paper_leads, not "
            "in this case). " + compared, quality, [cite["ao"], *cite["damage"]],
            code="paper_original_not_attached")

    checks = {
        "chain_consistent": order_consistent,
        "order_link_found": link is not None,
        "fg_basis_found": basis is not None,
        "component_actuals_consistent": produced is not None and all(
            component_actual_agrees(item["row"], produced)
            for item in items if _number(item["row"].get("qty_per_batch"))),
        "link_qtymfg_matches_derived": produced is not None and link is not None and
            _number(link["row"].get("qtymfg_c")) == produced,
        "ao_header_quantities_empty": all(header_empty.values()),
        "fm_dates_after_required_date": fm_late is True,
        "dates_readable": order_consistent and not unreadable,
        "damage_rows": len(damage),
        "related_aos": len(related),
        "mo_found": mo["found"],
        "qc_retain_sample": qc_retain}
    if paper:
        checks["paper_damage_conflicts"] = sum(
            1 for lead in leads if lead["kind"] == "component_damage" and lead["agrees"] is False)
    if originals is not None:
        match = (original or {}).get("expected_match")
        checks["original_attached"] = attached and (match is None or all(match.values()))
    header_total = _number(fg_values["values"].get("totalprodqty_c"))
    produced_fg = header_total if header_total is not None else produced
    return {"case_id": case_id, "cite": cite, "results": results, "damaged": damaged,
            "damaged_by_code": damaged_by_code, "damaged_by_component": damaged_by_component,
            "produced_fg": _text(produced_fg) if produced_fg is not None else None,
            "produced_fg_basis": ("assembly_order.totalprodqty_c" if header_total is not None
                                  else "component row actual_amount / qty_per_batch"
                                  if produced is not None else None),
            "fg_header": fg_values, "qc_retain": qc_retain,
            "chain": chain, "header_empty": header_empty, "dates": dates, "fm_late": fm_late,
            "related": related, "line_produced": line_produced,
            "planned_fg": {"derived": planned["derived"], "sources": planned["sources"]},
            "mo": mo, "paper_leads": leads,
            "paper_damage": paper_damage(leads), "original": original,
            "original_conflicts": conflicts, "gap_codes": gap_codes, "checks": checks}


def paper_damage(leads: list[dict[str, Any]]) -> dict[str, Any] | None:
    """[PAPER] damage per component beside FM's, for the report (never a case value).

    The paper packet prints damage per component (box, jug, cap). Counts of different
    components are never added: there is no cross-component total here, so no "packs"
    headline can be rebuilt from this report. Per-component values are the evidence."""
    rows = [lead for lead in leads if lead["kind"] == "component_damage"]
    if not rows:
        return None
    # Keyed by the component: its code when no other lead prints the same code (a packet
    # may print one code twice), else code plus the lead's id, so no entry overwrites another.
    counts: dict[str, int] = {}
    for lead in rows:
        key = lead.get("code") or lead["id"]
        counts[key] = counts.get(key, 0) + 1
    by_component: dict[str, dict[str, Any]] = {}
    for lead in rows:
        key = lead.get("code") or lead["id"]
        entry = {"paper": lead["paper"], "fm": lead["fm"], "agrees": lead["agrees"],
                 "page": lead.get("page")}
        if lead.get("fm_ambiguous"):
            entry["fm_ambiguous"] = lead["fm_ambiguous"]
        by_component[key if counts[key] == 1 else f"{key} ({lead['id']})"] = entry
    return {"by_component": by_component,
            "no_total_note": "damage counts of different components (boxes, jugs, caps) are "
                             "not added: per-component values are the evidence",
            "status": "unverified_lead"}


def _original_cites(original: dict[str, Any] | None, pages: list[int]) -> list[str]:
    """Page citations of the attached original for ``pages`` (the file citation when its
    extraction has none of them, e.g. no OCR on this host)."""
    from operating.sharepoint_originals import page_citation
    found = [handle for handle in (page_citation(original, page) for page in pages) if handle]
    return found or ([original["citation"]] if original and original.get("citation") else [])


def _token_in(value: str, text: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(value)}(?![\w.])", text) is not None


# A figure token ends the field: no further digit group joined to it ("11/20", "11-2",
# "11,5", "11.5") and no second bare number right after it ("11 20"): such text is not
# one figure, so it is never taken as the lead's.
_FIGURE = r"(\d+(?:\.\d+)?)(?![\w.,/\-])(?!\s+[\d.,/\-])"


def _figure_in_labelled_field(lead: dict[str, Any], text: str) -> bool:
    """The paper figure sits in the lead's own field on the page, not just anywhere on it.

    A component-damage lead is anchored on its packaging code: the figure is the one
    number right after the code (``<code> 11``, ``<code> = 11``, ``<code>: 07`` equals
    7), never a compound like ``<code> 11/20``. Any other lead has no field anchor, so
    it is not located here (a date lead is checked by ``_date_on_page``)."""
    value = _number(lead["paper"])
    if lead["kind"] != "component_damage" or not lead.get("code") or value is None:
        return False
    return any(_number(match.group(1)) == value for match in re.finditer(
        rf"(?<![\w.\-]){re.escape(str(lead['code']))}(?:\s*[=:]\s*|\s+){_FIGURE}", text))


def _date_on_page(lead: dict[str, Any], text: str) -> bool:
    """A date lead's ISO date appears as a token in the page text (no field label is
    matched, so the wording says it appears on the page, not in its field)."""
    return lead["kind"] == "date_manufactured" and _token_in(str(lead["paper"]), text)


def original_conflicts(call: Call, leads: list[dict[str, Any]], original: dict[str, Any],
                       ao_citation: str, gap: Callable[..., None], who: list[str],
                       fm_claims: Callable[[dict[str, Any]], tuple[list[dict[str, Any]],
                                                                   list[str]]] | None = None
                       ) -> list[dict[str, Any]]:
    """One evidence-conflict gap per paper lead that disagrees with the cited FM rows,
    citing both sources: the FM row(s) and the attached original's page. The paper
    figure is written only when the page's machine-read text shows it in the lead's
    labelled field (so the cited page carries it); otherwise the gap says a person must read it on that page.
    Value claims: the FM figure through ``fm_claims`` (its damage row, damage sum, Filling
    task, calculation or date manufactured), the paper figure (or date) on the page
    citation's machine-read text (``field: "text"``, untyped: a lead); "page N" is a
    locator, not a number."""
    from operating.sharepoint_originals import page_text
    recorded = []
    for lead in leads:
        if lead["agrees"] is not False:
            continue
        page = lead.get("page")
        [handle] = _original_cites(original, [page] if page else [])
        text = page_text(call, handle) if "#page=" in handle else None
        shown = text is not None and _figure_in_labelled_field(lead, text)
        dated = not shown and text is not None and _date_on_page(lead, text)
        where = f"page {page} of the attached original" if "#page=" in handle else \
            "the attached original"
        fm_cites = lead["fm_citations"] or [ao_citation]
        claims, extra = fm_claims(lead) if fm_claims else ([], [])
        if shown or dated:
            claims = claims + [{"value": str(lead["paper"]), "citation": handle,
                                "field": "text"}]
        gap("unknown", f"Which figure for {lead['label']} is right: evidence conflict between "
            "FM and the paper original",
            f"The cited FM row(s) record {lead['fm']}; "
            + (f"{where} reads {lead['paper']} in its labelled field in the machine-read "
               "text, to be confirmed by a person." if shown else
               f"{where} reads {lead['paper']} as a date in its machine-read text (its "
               "field label was not matched), to be confirmed by a person." if dated else
               f"{where} holds the paper figure; it was not located in its labelled field "
               "in the machine-read text, so a person must read it there.")
            + " Neither source is taken as the fact.", who,
            [*fm_cites, *[item for item in extra if item not in fm_cites], handle],
            code=f"paper_fm_conflict:{lead['id']}", claims=claims)
        recorded.append({"lead": lead["id"], "fm_citations": fm_cites, "original": handle,
                         "paper_figure_in_page_text": shown or dated,
                         "paper_figure_in_labelled_field": shown})
    return recorded


def _sample(ao_code: str, mo_row: dict[str, Any] | None, mo_candidates: list[dict[str, Any]],
            ao_row: dict[str, Any], ao_citation: str
            ) -> tuple[Decimal | None, tuple[str, str, str]]:
    """The AO's sample count: the Portal manufacturing order's qty_sample when its one row
    holds it, else the FM AO row's qtysample; with (who records it, its citation)."""
    if mo_row is not None:
        for spelling in MO_COLUMNS["qty_sample"]:
            value = _number(mo_row.get(spelling))
            if value is not None:
                return value, (f"Portal manufacturing order {ao_code}",
                               mo_candidates[0]["citation"], spelling)
    for spelling in FM_AO_COLUMNS["qtysample"]:
        value = _number(ao_row.get(spelling))
        if value is not None:
            return value, (f"FM AO row of {ao_code}", ao_citation, spelling)
    return None, ("", "", "")


def fg_header_findings(ao_code: str, ao_row: dict[str, Any], states: dict[str, str],
                       produced: Decimal | None, cite: dict[str, Any],
                       finding: Callable[..., None], gap: Callable[..., None],
                       who: list[str]) -> dict[str, Any]:
    """The Portal's finished-good figures on the FM AO row (``FG_HEADER``).

    Read: a cited finding, and a conflict gap when ``totalprodqty_c`` differs from the
    produced figure derived from the component row. Not served by the reporting API:
    an ``inaccessible`` gap (unread, never "absent"). Served but empty: absent."""
    values = {column: ao_row[column] for column, state in states.items() if state == "read"}
    unread = [column for column, state in states.items() if state == "unread"]
    empty = [column for column, state in states.items() if state == "empty"]
    if values:
        finding(f"The FM AO row of {ao_code} records " + ", ".join(
            f"{column} {_text(_number(value)) if _number(value) is not None else value} "
            f"({FG_HEADER[column]})" for column, value in values.items()) + ".", [cite["ao"]],
            claims=[claim(value, cite["ao"], f"row.{column}") for column, value in values.items()])
        total = _number(values.get("totalprodqty_c"))
        if total is not None and produced is not None and total != produced and cite["item"]:
            gap("unknown", f"Whether {ao_code} produced what totalprodqty_c says or what its "
                "component rows imply: evidence conflict",
                "The Portal reads produced finished goods from the AO's totalprodqty_c; the "
                "cited component row consumed one per unit implies a different figure "
                "(actual_amount divided by qty_per_batch).", who, [cite["ao"], cite["item"]],
                code="produced_header_conflict")
    if unread:
        gap("inaccessible", f"{ao_code} produced and accepted finished goods as the Portal "
            f"reads them ({', '.join(unread)}): unread",
            "The reporting API does not serve these FM AO columns, so they were not read; "
            "that is not evidence that they are empty. Produced finished goods are derived "
            "from a component row instead, and accepted (produced minus samples) is not "
            "stated by any cited row.", who, [cite["ao"]], code="fg_header_unread")
    if empty:
        gap("absent_in_source", f"{ao_code} " + ", ".join(empty),
            "Served but empty on the cited AO row.", who, [cite["ao"]], code="fg_header_empty",
            fields=(AO_CODE_MATCH[0], empty))
    return {"values": values, "unread": unread, "empty": empty}


# Portal planned finished goods: component quantity is qtybatch x size_pieces
# (portal/src/app/api/mrp/assembly/[id]/route.ts@878705b PATCH doc: "qty_c = qtybatch x
# size_pieces"); assembly-queries.ts@878705b:448 names sizepieces "planned production qty"
# (both unchanged at 71f7214: route.ts:141, assembly-queries.ts:472).
PLANNED_SOURCES = (("fm_sizepieces", "FM AO row's sizepieces", "ao"),
                   ("mo_size_pieces", "Portal manufacturing order's size_pieces", "mo"))
# Portal "qty produced" for an order line: qtyaomfg_c + qtybcmfg_c
# (portal/src/app/mrp/orders/order-queries.ts@878705b:238-239, 239-240@38cd3f3; wms/shipments/orders/route.ts:171).
LINE_PRODUCED = ("qtyaomfg_c", "qtybcmfg_c")


def order_line_produced(line_row: dict[str, Any]) -> dict[str, Any]:
    """The Portal's produced figure for an order line, from the cited line row, or why not.

    Report only: ``unread`` (columns not served), ``empty``, or ``read`` with the sum (a
    part that is empty counts 0, as the Portal's COALESCE does, when the other is read)."""
    if not all(column in line_row for column in LINE_PRODUCED):
        return {"state": "unread", "columns": list(LINE_PRODUCED)}
    values = {column: _number(line_row.get(column)) for column in LINE_PRODUCED}
    if all(value is None for value in values.values()):
        return {"state": "empty", "columns": list(LINE_PRODUCED)}
    total = sum((value or Decimal(0) for value in values.values()), Decimal(0))
    return {"state": "read", "values": {column: None if value is None else _text(value)
                                        for column, value in values.items()},
            "qty_produced": _text(total)}


def planned_fg(basis: dict[str, Any], ao_row: dict[str, Any],
               mo_row: dict[str, Any] | None, ao_citation: str,
               mo_citation: str | None) -> dict[str, Any]:
    """Planned finished goods as the Portal states them (``PLANNED_SOURCES``) beside the
    figure derived from the unit-ratio component row (``quantity / qty_per_batch``).

    Returns the derived figure, each source's state (``unread`` when the row does not serve
    the column, ``empty``, else the value) and which cited sources agree or differ. A
    source that is unread or empty is never counted as agreement."""
    derived = _number(basis["row"].get("quantity")) / _number(basis["row"].get("qty_per_batch"))
    raw = {"fm_sizepieces": "unread" if "sizepieces" not in ao_row else ao_row.get("sizepieces"),
           "mo_size_pieces": "unread" if mo_row is None else next(
               (mo_row.get(spelling) for spelling in MO_COLUMNS["size_pieces"]
                if spelling in mo_row), "unread")}
    out: dict[str, Any] = {"derived": _text(derived), "sources": {}, "agree": [], "differ": []}
    for name, label, where in PLANNED_SOURCES:
        number = None if raw[name] == "unread" else _number(raw[name])
        out["sources"][name] = ("unread" if raw[name] == "unread" else
                                "empty" if number is None else _text(number))
        if number is not None:
            out["agree" if number == derived else "differ"].append(
                (label, ao_citation if where == "ao" else mo_citation))
    return out


def manufacturing_order(call: Call, ao_code: str, candidates: list[dict[str, Any]],
                        ao_row: dict[str, Any],
                        cite: dict[str, Any], finding: Callable[..., None],
                        gap: Callable[..., None], who: list[str]) -> dict[str, Any]:
    """Divergence 6: the served Portal manufacturing order (= the AO) and the FM AO header's
    finished-good columns (samples, FG damage, size in pieces), cited where present."""
    out: dict[str, Any] = {"found": False, "values": {}, "absent": [], "unread": [],
                           "fm_ao": {}}
    fm = {name: ao_row.get(spelling) for name, spellings in FM_AO_COLUMNS.items()
          for spelling in spellings if ao_row.get(spelling) not in (None, "")}
    out["fm_ao"] = fm
    if fm:
        spelled = {name: next(spelling for spelling in FM_AO_COLUMNS[name]
                              if ao_row.get(spelling) not in (None, "")) for name in fm}
        finding(f"The FM AO row of {ao_code} records " + ", ".join(
            f"{name} {value} ({FM_AO_MEANING[name]})" for name, value in sorted(fm.items()))
            + ".", [cite["ao"]],
            claims=[claim(value, cite["ao"], f"row.{spelled[name]}")
                    for name, value in sorted(fm.items())])
    if len(candidates) != 1:
        gap("unknown", f"The Portal manufacturing order of {ao_code} (samples, finished-good "
            "damage, size in pieces)",
            ("No public.app_manufacturing_order row with this code was found in the mirror read "
             "visible to this reader" if not candidates else
             "Several public.app_manufacturing_order rows carry this code") +
            "; its sample, finished-good damage and size figures are not compared with FM.",
            who, [cite["ao"]], code="mo_not_found" if not candidates else "mo_ambiguous")
        return out
    cite["mo"] = candidates[0]["citation"]
    row = call("plx.source_citation", {"citation": cite["mo"]})["row"]
    out["found"] = True
    fields: dict[str, str] = {}
    for name, spellings in MO_COLUMNS.items():
        spelling = next((spelling for spelling in spellings
                         if row.get(spelling) not in (None, "")), None)
        if spelling is None:
            # Served but empty (a spelling is a column of the row) is absent; a column
            # the row does not carry at all is unread, never empty.
            served = next((spelling for spelling in spellings if spelling in row), None)
            if served is None:
                out["unread"].append(name)
            else:
                out["absent"].append(name)
                fields[name] = served
        else:
            out["values"][name] = row[spelling]
            fields[name] = spelling
    if out["values"]:
        finding(f"The Portal manufacturing order {ao_code} records " + ", ".join(
            f"{name} {_text(_number(value)) if _number(value) is not None else value}"
            for name, value in out["values"].items())
            + " (qty_damaged there counts finished goods, not components).", [cite["mo"]],
            claims=[claim(value, cite["mo"], f"row.{fields[name]}", MO_ROLES.get(name))
                    for name, value in out["values"].items()])
    if out["absent"]:
        gap("absent_in_source", f"Portal manufacturing order {ao_code}: "
            + ", ".join(out["absent"]),
            "These finished-good columns are empty on the cited manufacturing order row.",
            who, [cite["mo"]], code="mo_columns_absent",
            fields=(MO_CODE_MATCH[0], [fields[name] for name in out["absent"]]))
    if out["unread"]:
        gap("inaccessible", f"Portal manufacturing order {ao_code}: "
            + ", ".join(out["unread"]),
            "The cited manufacturing order row does not carry these finished-good columns, "
            "so they were not read; that is not evidence that they are empty.",
            who, [cite["mo"]], code="mo_columns_unread")
    return out


def paper_leads(paper: dict[str, Any], items: list[dict[str, Any]],
                damage: list[dict[str, Any]], filling: dict[str, Any], ao_row: dict[str, Any],
                produced: Decimal | None,
                codes: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The [PAPER] leads beside the cited FM values (report only, never a case value).
    ``agrees`` is None when the FM side cannot be read. A component lead names its FM
    packaging row (``packaging_id``) or, failing that, the code the paper prints, matched
    against the AO items' packaging codes (``codes``: AO item id -> code, read from
    ``public.packaging``); a misprinted code matches nothing and stays not comparable."""
    by_id = {str(item["row"].get("id")): item["row"] for item in items}
    filled = _number(filling["row"].get("qty"))
    made = parse_date_value(ao_row.get("datemanuf"))
    leads: list[dict[str, Any]] = []
    for lead in paper["leads"]:
        citations: list[str] = []
        fm: str | None = None
        ambiguous = None
        if lead["kind"] == "component_damage":
            # The AO items this lead's component is: same packaging row and, when the lead
            # carries the paper's UNIT QTY, the same qty_per_batch. Damage is summed only
            # within one AO item (one unit); a lead matching several items is ambiguous.
            named = (lambda item_id, row: _same(row.get("packaging_id"), lead["packaging_id"])
                     ) if lead.get("packaging_id") not in (None, "") else (
                lambda item_id, row: _same((codes or {}).get(item_id, {}).get("code"),
                                           lead.get("code")))
            matched = {item_id for item_id, row in by_id.items()
                       if named(item_id, row) and (
                           "unit_qty" not in lead or
                           _number(row.get("qty_per_batch")) == _number(lead["unit_qty"]))}
            rows = [row for row in damage if str(row["row"].get("assembly_item_id")) in matched]
            values = [_number(row["row"].get("qty_damaged")) for row in rows]
            if len(matched) > 1:
                ambiguous = ("the component's packaging row is used by several AO items ("
                             + ", ".join(sorted(matched)) + "); their damage is in different "
                             "units and is not added, so the paper figure is not compared")
            elif rows and None not in values:
                fm = _text(sum(values, Decimal(0)))  # type: ignore[arg-type]
            citations = [row["citation"] for row in rows]
        elif lead["kind"] == "filled":
            fm, citations = (None if filled is None else _text(filled)), [filling["citation"]]
        elif lead["kind"] == "produced_not_filled":
            fm = None if produced is None or filled is None else _text(produced - filled)
            citations = [filling["citation"]]
        elif lead["kind"] == "date_manufactured":
            fm = None if made is None else made.isoformat()[:10]
        paper_value = lead["paper"]
        agrees = None if fm is None else (
            _number(fm) == _number(paper_value) if _number(paper_value) is not None
            else fm == paper_value)
        leads.append({"id": lead["id"], "kind": lead["kind"], "label": lead["label"],
                      "code": lead.get("code"), "page": lead.get("page"),
                      "fm": fm, "fm_citations": citations,
                      "paper": paper_value, "agrees": agrees,
                      **({"fm_ambiguous": ambiguous} if ambiguous else {}),
                      "paper_source": paper["provenance"], "status": "unverified_lead"})
    return leads
