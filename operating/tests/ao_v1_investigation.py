"""Frozen v1-shaped AO-26005B investigation: the case shape playbook
``ao_quantity_reconciliation`` **v1** scores (test fixture only).

This is ``operating/ao_case_script.investigate`` as of 6fecb6e, kept verbatim so the
evaluator tests (``test_playbook_eval``) keep
exercising v1's mechanics (repeat sums over damage rows, row completeness, perturbed
arithmetic, gap attribution) and so adoption ledgers that bind the v1 definition stay
reproducible. Its business reading is superseded: it sums boxes, jugs and caps into one
"pack" total, reads planned/manufactured from a hardcoded component row and records
lateness as a fact. The reference investigation is ``operating/tests/ao_reference_investigation.py``, scored
by v2. Never import this outside tests.

One change since 6fecb6e: statements that quote row ids or dates carry structured value
claims (``value_claims``), because every evaluation now fails a number or ISO date no
claim covers (``operating.claim_checks``), and its absence gap names what is absent
(``property``), because plx.case_record_gap requires it; the case shape v1 scores is
otherwise unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from operating.customer_po_coverage import parse_date_value

AO_ID, LINE_ID, ORDER_ID, ITEM_ID = 10718, 1140, 351, 58383
EXPECTED = {"planned_minus_manufactured": "6", "manufactured_minus_filled": "1",
            "damaged_packs": "30", "ordered_minus_manufactured": "417"}

Call = Callable[[str, dict[str, Any]], dict[str, Any]]


def _one(rows: list[dict[str, Any]], what: str) -> dict[str, Any]:
    if len(rows) != 1:
        raise LookupError(f"expected exactly one {what}, found {len(rows)}")
    return rows[0]


def _rows(call: Call, relation: str, where: list[list[Any]]) -> list[dict[str, Any]]:
    return call("mirror.rows", {"relation": relation, "where": where})["rows"]


def _compute(call: Call, case_id: str, inputs: dict[str, Any], expression: str,
             unit: str, relation: tuple[str, str, str, str] | None = None) -> str:
    """One recorded calculation. Planned, actual, manufactured, filled and ordered are
    different quantity roles (source contracts pin most of them), so a difference of
    two roles names its relation ``(left, op, right, as)``."""
    arguments = {"case_id": case_id, "inputs": inputs, "expression": expression, "unit": unit}
    if relation is not None:
        arguments["relations"] = [dict(zip(("left", "op", "right", "as"), relation))]
    return call("plx.case_compute", arguments)["calculation"]["result"]


def _matched(call: Call, literal: str, binding: str) -> list[dict[str, str]]:
    """The rows ``plx.find`` matched through one binding, as ``{"citation": ...}``."""
    return [{"citation": item["matched"]["raw"]["origin"]["citation"]}
            for item in call("plx.find", {"query": literal})["candidates"]
            if item["matched"]["binding"] == binding]


def investigate(call: Call) -> dict[str, Any]:
    """Open the case, cite the rows, compute, record facts and gaps; return what was done."""
    case_id = call("plx.case_open", {
        "question": "Was AO-26005B produced as ordered for OR1000151885 "
                    "(planned vs manufactured vs filled vs damaged, and on time)?",
        "roots": ["AO-26005B", "OR1000151885"]})["case_id"]
    ao = _one(_matched(call, "AO-26005B", "portal.fm_assembly_order"),
              "public.assembly_order match")
    order = _one(_matched(call, "OR1000151885", "portal.fm_order_entry"),
                 "public.order_entry match")
    ao_row = call("plx.source_citation", {"citation": ao["citation"]})["row"]
    order_row = call("plx.source_citation", {"citation": order["citation"]})["row"]
    # Every hop is read by the identity the previous cited row carries, never
    # by the ids of the historical trace, and checked before anything is
    # recorded as a relationship.
    ao_id = ao_row.get("id")
    link = _one(_rows(call, "public.order_entry_item_assembly",
                      [["idassembly", "eq", ao_id]]), "AO link")
    line = _one(_rows(call, "public.order_entry_item",
                      [["id", "eq", link["row"].get("idoeitem")]]), "order line")
    item = _one(_rows(call, "silver.assembly_order_items", [["id", "eq", ITEM_ID]]),
                "AO item")
    filling = _one(_rows(call, "silver.production_tasks",
                         [["mo_id", "eq", ao_id], ["task_type", "eq", "Filling"]]),
                   "filling task")
    damage = _rows(call, "silver.assembly_order_damage", [["assembly_order_id", "eq", ao_id]])
    if not damage:
        raise LookupError("no damage rows for the AO")
    cite = {"ao": ao["citation"], "order": order["citation"], "link": link["citation"],
            "line": line["citation"], "item": item["citation"],
            "filling": filling["citation"], "damage": [row["citation"] for row in damage]}
    chain = {"link_ao": link["row"].get("idassembly"), "link_line": link["row"].get("idoeitem"),
             "line_id": line["row"].get("id"), "line_order": line["row"].get("idoe"),
             "ao_id": ao_id, "item_ao": item["row"].get("assembly_order_id"),
             "order_id": order_row.get("id")}

    def same(first: Any, second: Any) -> bool:
        # Ids arrive as ints or strings from different relations; compare as text.
        return first is not None and str(first) == str(second)

    ao_consistent = (same(ao_id, chain["link_ao"]) and same(ao_id, chain["item_ao"]))
    order_consistent = (ao_consistent and same(chain["link_line"], chain["line_id"]) and
                        same(chain["line_order"], chain["order_id"]))
    header_empty = {column: ao_row.get(column) in (None, "")
                    for column in ("qtytotal", "finalqty")}

    source = {"link_ao": (cite["link"], "row.idassembly"),
              "link_line": (cite["link"], "row.idoeitem"), "line_id": (cite["line"], "row.id"),
              "line_order": (cite["line"], "row.idoe"), "ao_id": (cite["ao"], "row.id"),
              "item_ao": (cite["item"], "row.assembly_order_id"),
              "order_id": (cite["order"], "row.id")}

    def ids(*names: str) -> list[dict[str, Any]]:
        return [{"value": str(chain[name]), "citation": source[name][0],
                 "field": source[name][1], "role": "identifier"} for name in names
                if chain[name] not in (None, "")]
    if order_consistent:
        call("plx.case_add_finding", {
            "case_id": case_id, "kind": "fact", "status": "supported",
            "statement": f"AO-26005B (FM AssemblyOrder {ao_id}) is linked by order line "
                         f"{chain['line_id']} to sales order {chain['order_id']} "
                         f"(OR1000151885).",
            "citations": [cite["ao"], cite["link"], cite["line"], cite["order"]],
            "value_claims": ids("ao_id", "line_id", "order_id")})
    elif ao_consistent:
        call("plx.case_add_finding", {
            "case_id": case_id, "kind": "fact", "status": "contradicted",
            "statement": f"AO-26005B is linked to OR1000151885: the cited link row points to "
                         f"order line {chain['link_line']}, which belongs to sales order "
                         f"{chain['line_order']}, while OR1000151885 resolves to sales order "
                         f"{chain['order_id']}.",
            "citations": [cite["ao"], cite["link"], cite["line"], cite["order"]],
            "value_claims": ids("link_line", "line_order", "order_id")})
    if not order_consistent:
        call("plx.case_record_gap", {
            "case_id": case_id, "state": "unknown",
            "missing": "Which sales order AO-26005B was produced for",
            "why": "The cited AO, link, line, item and order rows do not form one chain "
                   "(" + ", ".join(f"{name}={value}" for name, value in chain.items()) + "), "
                   "so order quantities and dates are not compared with this AO.",
            "who_can_answer": ["Production planning", "Customer service (Vince)"],
            "citations": [cite["ao"], cite["link"], cite["line"], cite["item"],
                          cite["order"]],
            "value_claims": ids(*chain)})
    results = {}
    if ao_consistent:
        results["planned_minus_manufactured"] = _compute(call, case_id, {
            "planned": {"citation": cite["item"], "field": "row.quantity", "unit": "EA"},
            "manufactured": {"citation": cite["item"], "field": "row.actual_amount",
                             "unit": "EA"}}, "planned - manufactured", "EA",
            ("planned", "-", "actual", "planned_not_produced"))
        results["manufactured_minus_filled"] = _compute(call, case_id, {
            "manufactured": {"citation": cite["link"], "field": "row.qtymfg_c", "unit": "EA"},
            "filled": {"citation": cite["filling"], "field": "row.qty", "unit": "EA",
                       "role": "filled"}},  # a Filling task's qty; no contract (task-typed)
            "manufactured - filled", "EA", ("manufactured", "-", "filled", "manufactured_not_filled"))
    results["damaged_packs"] = _compute(call, case_id, {
        f"d{index}": {"citation": handle, "field": "row.qty_damaged", "unit": "pack"}
        for index, handle in enumerate(cite["damage"])},
        " + ".join(f"d{index}" for index in range(len(cite["damage"]))), "pack")
    if order_consistent:
        results["ordered_minus_manufactured"] = _compute(call, case_id, {
            "ordered": {"citation": cite["line"], "field": "row.qty", "unit": "EA"},
            "manufactured": {"citation": cite["link"], "field": "row.qtymfg_c", "unit": "EA"}},
            "ordered - manufactured", "EA", ("ordered", "-", "manufactured", "ordered_not_manufactured"))
    dates = {"required": order_row.get("datereq"), "manufactured": ao_row.get("datemanuf"),
             "filled": filling["row"].get("date_start")}
    # Calendar dates, never strings (same parser as the customer-PO date filter).
    parsed = {name: parse_date_value(value) for name, value in dates.items()}
    days = {name: (value.date() if isinstance(value, datetime) else value)
            for name, value in parsed.items()}
    unreadable = sorted(name for name, value in days.items() if value is None)
    late: bool | None = None
    if not order_consistent:
        pass  # the gap above already says order dates are not compared
    elif unreadable:
        call("plx.case_record_gap", {
            "case_id": case_id, "state": "unknown",
            "missing": "Whether AO-26005B was produced by the required date",
            "why": "These dates on the cited rows are empty or not ISO dates, so on-time "
                   "delivery cannot be judged: " + ", ".join(unreadable) + ".",
            "who_can_answer": ["Production planning", "Customer service (Vince)"],
            "citations": [cite["ao"], cite["filling"], cite["order"]]})
    else:
        late = (days["manufactured"] > days["required"] or
                days["filled"] > days["required"])
    if late:
        call("plx.case_add_finding", {
            "case_id": case_id, "kind": "fact", "status": "supported",
            "statement": f"Production was recorded after the required date: AO manufactured "
                         f"{days['manufactured'].isoformat()} and filled "
                         f"{days['filled'].isoformat()}; the sales order required "
                         f"{days['required'].isoformat()}.",
            "citations": [cite["ao"], cite["filling"], cite["order"]],
            "value_claims": [
                {"value": days["manufactured"].isoformat(), "citation": cite["ao"],
                 "field": "row.datemanuf"},
                {"value": days["filled"].isoformat(), "citation": cite["filling"],
                 "field": "row.date_start"},
                {"value": days["required"].isoformat(), "citation": cite["order"],
                 "field": "row.datereq"}]})
        call("plx.case_record_gap", {
            "case_id": case_id, "state": "unknown",
            "missing": "Why AO-26005B was produced after the required date",
            "why": "No source row read here records a reason or a customer-agreed new date.",
            "who_can_answer": ["Production planning", "Customer service (Vince)"],
            "citations": [cite["ao"], cite["order"]]})
    if all(header_empty.values()):
        call("plx.case_record_gap", {
            "case_id": case_id, "state": "absent_in_source",
            "missing": "AO-26005B header quantities (qtytotal, finalqty)",
            "why": "Both are empty on the cited AO row; planned and manufactured quantities "
                   "are only on the AO item and link rows.",
            "who_can_answer": ["Ricardo (Quality)", "Production planning"],
            "citations": [cite["ao"]],
            "property": ["AssemblyOrder.ordered_quantity", "AssemblyOrder.final_quantity"]})
    call("plx.case_record_gap", {
        "case_id": case_id, "state": "not_ingested",
        "missing": "The original AO-26005B production scan",
        "why": "The signed paper record is not in any source the Workbench reads; it must "
               "be supplied by an operator (plx.case_attach_original).",
        "who_can_answer": ["Ricardo (Quality)"], "citations": [cite["ao"]]})
    checks = {
        "chain_consistent": order_consistent,
        "link_chain": order_consistent and {name: str(value) for name, value in chain.items()} ==
                      {"link_ao": str(AO_ID), "link_line": str(LINE_ID), "line_id": str(LINE_ID),
                       "line_order": str(ORDER_ID), "ao_id": str(AO_ID), "item_ao": str(AO_ID),
                       "order_id": str(ORDER_ID)},
        "results_match_live_trace": results == EXPECTED,
        "ao_header_quantities_empty": all(header_empty.values()),
        "produced_after_required_date": late is True,
        "dates_readable": order_consistent and not unreadable,
        "damage_rows": len(damage)}
    return {"case_id": case_id, "cite": cite, "results": results, "chain": chain,
            "header_empty": header_empty, "dates": dates, "late": late, "checks": checks}
