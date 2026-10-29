"""Material sample requests linked to the raw material and supplier they name.

Process map 'Raw material samples: from request to used up' (Draft 5, 20 Oct 2026): until
formulating creates an R-number, a sample is identified by supplier + trade name + supplier
code; the R-number is attached when it exists. :func:`link_requests` links each cited
request through the generic primitives as the asking reader:

* by supplier code: ``SupplierItem.supplier_item_number`` (FileMaker
  ``raw_material_supplier.itemnumber``), then ``SupplierItem.for_item`` to the raw material
  (``Product``, its ``code`` the R-number) and ``SupplierItem.offered_by`` to the
  ``Supplier``;
* by R-number: ``Product.code``;
* both given: they must reach the same raw material, else ``r_number_disagrees`` and no
  link is chosen. Several supplier offerings under one code are ``supplier_code_ambiguous``
  with every candidate listed, never a pick. Neither given (a new development with no code
  yet) is ``material_unidentified``; what it was based on is kept as the request states it.

Requests are the caller's cited records (a mail, a Teams message, a list row): the Portal
sourcing request and sample types are not served, so every answer carries
``sample_requests_not_served`` until they are. A request without a citation is refused.

Quantity left is ``received - sum(recorded usage)`` in Decimal, in one unit, never
converted, and only when both terms are recorded: the map's biggest gap is that usage is
never recorded (``usage_not_recorded``) and the received quantity is a count ("1",
"1 bottle"), not an amount (``received_is_a_count``). The value says it subtracts recorded
usage only.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from ontology.units import unit_token

from .cited_reads import CitedReads, Record, gap

ANSWER_FORMAT = "plx-material-sample-links/v1"
REQUEST_TYPES = ("RelPublicAppSourcingRequest", "RelPublicAppSourcingSample")
# ontology/units.py UOM_CANONICAL: the amounts (mass, volume); every other unit is a count.
AMOUNT_UNITS = frozenset({"kg", "g", "mg", "lb", "oz", "l", "ml", "gal"})
_REQUEST_KEYS = {"request_id", "citation", "supplier_code", "trade_name", "r_number",
                 "based_on", "received", "used"}


def _cell(record: Record, name: str) -> dict[str, Any] | None:
    cell = record.get(name)
    return {"value": cell["value"], "citation": cell["citation"]} if cell else None


def _amount(entry: Any) -> tuple[Decimal, str, str] | str:
    """(amount, unit, citation) of a cited quantity, or the gap code that refuses it."""
    if not isinstance(entry, Mapping) or not entry.get("citation"):
        return "quantity_uncited"
    unit = unit_token(entry.get("unit"))
    if unit is None or unit not in AMOUNT_UNITS:
        return "received_is_a_count"
    try:
        value = Decimal(str(entry.get("amount")).strip())
    except (InvalidOperation, ValueError):
        return "amount_not_a_number"
    if not value.is_finite() or value < 0:
        return "amount_not_a_number"
    return value, unit, str(entry["citation"])


def quantity_left(received: Any, used: Sequence[Any] | None) -> dict[str, Any]:
    """Remaining after recorded usage, with its arithmetic, or null with named gaps."""
    out: dict[str, Any] = {"value": None, "unit": None,
                           "basis": "received minus recorded usage", "inputs": [], "steps": [],
                           "gaps": []}
    states = {"usage_not_recorded": "absent_in_source", "received_is_a_count": "absent_in_source",
              "received_amount_not_recorded": "absent_in_source"}

    def fail(code: str, detail: str) -> dict[str, Any]:
        out["gaps"].append(gap(code, states.get(code, "unknown"), "quantity_left", detail))
        return out
    if received is None:
        fail("received_amount_not_recorded", "no received amount is recorded for the sample")
    else:
        got = _amount(received)
        if isinstance(got, str):
            return fail(got, "the received quantity is not an amount with a mass or volume "
                        "unit; nothing can be subtracted from it")
        out["inputs"].append({"role": "received", "amount": str(got[0]), "unit": got[1],
                              "citation": got[2]})
    if not used:
        fail("usage_not_recorded", "no lab batch usage is recorded against the sample; "
             "remaining = received - used cannot be computed")
    if out["gaps"]:
        return out
    total, unit = Decimal(out["inputs"][0]["amount"]), out["inputs"][0]["unit"]
    for entry in used:
        got = _amount(entry)
        if isinstance(got, str):
            code = "usage_not_an_amount" if got == "received_is_a_count" else got
            return fail(code, "a recorded usage is not a cited amount with a mass or volume unit")
        if got[1] != unit:
            return fail("units_disagree", f"usage in {got[1]} against a receipt in {unit}; "
                        "units are never converted here")
        out["inputs"].append({"role": "used", "amount": str(got[0]), "unit": got[1],
                              "citation": got[2]})
        out["steps"].append(f"{total} - {got[0]} = {total - got[0]}")
        total -= got[0]
    if total < 0:
        return fail("usage_exceeds_received", "recorded usage is more than the received "
                    "amount; one of them is wrong")
    out["value"], out["unit"] = str(total), unit
    return out


def _offerings(reads: CitedReads, code: str) -> list[dict[str, Any]]:
    """Every visible supplier offering under ``code`` with its raw material and supplier."""
    found = []
    for item in reads.read_all(reads.find("SupplierItem", [
            {"property": "supplier_item_number", "op": "eq", "value": code}],
            subject=f"supplier code {code}") or ()):
        materials = reads.read_all(reads.around(item.object_id, "SupplierItem.for_item",
                                                "forward") or ())
        suppliers = reads.read_all(reads.around(item.object_id, "SupplierItem.offered_by",
                                                "forward") or ())
        found.append({"supplier_item": {"object_id": item.object_id,
                                        "supplier_code": _cell(item, "supplier_item_number")},
                      "materials": materials, "suppliers": suppliers})
    return found


def _material(record: Record) -> dict[str, Any]:
    return {"object_id": record.object_id, "r_number": _cell(record, "code"),
            "name": _cell(record, "name")}


def _supplier(record: Record) -> dict[str, Any]:
    return {"object_id": record.object_id, "name": _cell(record, "name"),
            "code": _cell(record, "code")}


def _link_one(reads: CitedReads, request: Mapping[str, Any]) -> dict[str, Any]:
    rid = str(request.get("request_id") or "")
    out: dict[str, Any] = {"request_id": rid, "citation": request.get("citation"),
                           "stated": {k: request[k] for k in ("supplier_code", "trade_name",
                                                              "r_number", "based_on")
                                      if request.get(k)},
                           "material": None, "supplier": None, "supplier_item": None,
                           "link_basis": None, "candidates": [], "gaps": []}
    code = str(request.get("supplier_code") or "").strip()
    r_number = str(request.get("r_number") or "").strip()
    by_r = None
    if r_number:
        products = reads.read_all(reads.find("Product", [
            {"property": "code", "op": "eq", "value": r_number}], subject=f"R-number {r_number}")
            or ())
        if len(products) == 1:
            by_r = products[0]
        elif len(products) > 1:
            out["gaps"].append(gap("r_number_ambiguous", "unknown", rid,
                                   f"{len(products)} raw materials carry the R-number"))
        else:
            out["gaps"].append(gap("r_number_not_found", "unknown", rid,
                                   "no raw material this reader can read carries the R-number"))
    if code:
        offers = _offerings(reads, code)
        if r_number and by_r is not None:
            agree = [o for o in offers if any(m.object_id == by_r.object_id
                                              for m in o["materials"])]
            if offers and not agree:
                out["gaps"].append(gap("r_number_disagrees", "unknown", rid,
                                       "the supplier code reaches another raw material than "
                                       "the R-number; no link is chosen"))
                out["candidates"] = [_candidate(o) for o in offers]
                return out
            offers = agree or offers
        if len(offers) == 1 and len(offers[0]["materials"]) == 1:
            offer = offers[0]
            out["supplier_item"] = offer["supplier_item"]
            out["material"] = _material(offer["materials"][0])
            if len(offer["suppliers"]) == 1:
                out["supplier"] = _supplier(offer["suppliers"][0])
            else:
                out["gaps"].append(gap("supplier_not_linked", "unknown", rid,
                                       "the offering reaches no single supplier this reader "
                                       "can read"))
            out["link_basis"] = "supplier_code+r_number" if by_r is not None else "supplier_code"
            return out
        if len(offers) > 1:
            out["gaps"].append(gap("supplier_code_ambiguous", "unknown", rid,
                                   f"{len(offers)} supplier offerings carry the code; every "
                                   "candidate is listed, none is picked"))
            out["candidates"] = [_candidate(o) for o in offers]
            return out
        if offers:
            out["gaps"].append(gap("material_not_linked", "unknown", rid,
                                   "the offering reaches no single raw material this reader "
                                   "can read"))
            return out
        if by_r is None:
            out["gaps"].append(gap("supplier_code_not_found", "unknown", rid,
                                   "no supplier offering this reader can read carries the "
                                   "code; absence from the source is not established"))
            return out
    if by_r is not None:
        out["material"], out["link_basis"] = _material(by_r), "r_number"
        out["gaps"].append(gap("supplier_not_linked", "unknown", rid,
                               "an R-number alone names the raw material, not which "
                               "supplier's sample this is"))
        return out
    if not code and not r_number:
        out["gaps"].append(gap("material_unidentified", "absent_in_source", rid,
                               "the request names neither a supplier code nor an R-number "
                               "(a new development has no code until it arrives)"))
    return out


def _candidate(offer: Mapping[str, Any]) -> dict[str, Any]:
    return {"supplier_item": offer["supplier_item"],
            "materials": [_material(m) for m in offer["materials"]],
            "suppliers": [_supplier(s) for s in offer["suppliers"]]}


def link_requests(call: Callable[..., Mapping[str, Any]], actor: Any,
                  requests: Sequence[Mapping[str, Any]], *,
                  max_records: int = 200) -> dict[str, Any]:
    """Each cited request linked to its raw material and supplier, with quantity left."""
    reads = CitedReads(call, actor, max_records=max_records)
    for type_id in REQUEST_TYPES:
        probe = call("plx.find", {"object_type": type_id, "limit": 1}, actor)
        if (probe.get("error") or {}).get("code") == "not_served":
            reads.add(gap("sample_requests_not_served", "not_ingested", type_id,
                          "Portal sourcing requests and samples are not served: open requests "
                          "come only from the cited records the caller gives"))
    linked = []
    for request in requests:
        rid = str((request or {}).get("request_id") or "") if isinstance(request, Mapping) else ""
        if not isinstance(request, Mapping) or not rid or not request.get("citation") \
                or set(request) - _REQUEST_KEYS:
            reads.add(gap("request_refused", "unknown", rid or "request",
                          "a request needs request_id and the citation of the record it is "
                          "in, and only the declared keys"))
            continue
        out = _link_one(reads, request)
        out["quantity_left"] = quantity_left(request.get("received"), request.get("used"))
        linked.append(out)
    reads.withheld_gap("material samples")
    return {"format": ANSWER_FORMAT, "requests": linked, "gaps": reads.gaps,
            "withheld": reads.withheld}


__all__ = ["AMOUNT_UNITS", "ANSWER_FORMAT", "REQUEST_TYPES", "link_requests", "quantity_left"]
