"""Raw-capture oracle for purchase-order lateness: the open PO lines past their governing date.

It reads the captured Portal mirror rows and Business Central records directly (never the
product's projection, links or tools) and applies the lateness model of
``ontology/schema/process/po_delivery_window.json`` by its own code:

* open: a Portal status ``SUBMITTED`` or ``PARTIAL``, or a BC purchase order with
  ``fullyReceived`` false; closed by a Portal status ``DRAFT``, ``RECEIVED`` or a cancel
  value, a BC ``fullyReceived`` true or a BC status ``Draft``. An order is open when one
  of its records says open and none says closed (FileMaker and Sage decide nothing);
* an open line: ordered quantity above zero and received (none read as 0) below it;
* the governing date, by the model's authority precedence: FileMaker ``datearrive``
  first, then Portal ``expected_date``, then BC (the line's ``expectedReceiptDate``, else
  the header's ``requestedReceiptDate``; a Portal line takes the BC date only when the BC
  order holds exactly one). A year-1 BC date is no date;
* late: an open line whose governing date is before ``as_of``. An open order holding no
  line is judged on its header date (``line`` false). ``as_of`` is a business day in
  America/New_York (``business_today``: the oracle's own reading of the clock, never the
  UTC day and never the Workbench's).

Records join one order only on stored identifiers, never on a number text match (a lead):
Portal ``custom_fields.fmGeneratedPoId`` = FileMaker ``id``, and Portal
``custom_fields.renumberedTo`` = the BC ``number`` when exactly one BC order holds it and
exactly one Portal order claims it (else ``ambiguous``, no join). Then the Portal's own
record of the posting joins them: a raw ``app_bc_entity_map`` row of ``entity_type``
``purchaseOrder``, ``source_system`` ``portal``, ``status`` ``LINKED`` and ``bc_environment``
``Production`` names the Portal order (``source_id``) and the BC order it was posted as
(``bc_system_id`` = the BC ``id`` in ``bc_company_id``); it joins only when no other row
maps either end and neither order is joined to another one (else
``entity_map_conflict``, no join; decision 2026-10-28-oracle-entity-map-po-groups). Then a
BC order not yet
joined joins the Portal order its ``Your_Reference`` (the captured ODataV4 Page 50 field
page ``purchaseOrderReferences``, ``No`` equal to the order's number) names, only when the
product's rule (``evals.oracle.po_reference.decide``, a parity-tested copy) finds it firm: the
Portal's own exact comparison (trimmed, case-insensitive) against ``fmPoNumber`` (an FM
clash-series number ``-FM-<id>``: against ``po_number``) finds exactly one Portal order of
FileMaker lineage that no other reference names, neither order is joined elsewhere, and
the supplier (the Portal's own vendor map ``app_bc_entity_map``: the Portal supplier's
LINKED BC vendor No. in the order's company) or the order dates (``date_generated`` and
``orderDate``) corroborate it. A lead never joins (``counts.your_reference_*``). Sage is
not read.

Input: ``portal`` maps a relation name (``evals.oracle.keys``, and ``ENTITY_MAP``) to rows
``{"row", "citation"}``; ``bc`` is a list of ``{"company_id", "entity_set", "fields",
"citation"}``. Output: every order group (its numbers and records) and the late ones with
their late lines, governing dates (with the record each date is read from) and receipts.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import re
from typing import Any, Mapping, Sequence

from . import po_reference
from .keys import FM_PO, PO, PO_FM_SOURCES, PO_LINE, RECEIPT
from .source import decimal_of, field_value, instant, key_text

# The business's time zone, America/New_York, by its own rule (the oracle may not import
# zoneinfo, scripts/check_oracle_imports.py): EST is UTC-5; EDT, UTC-4, runs from the
# second Sunday of March 02:00 EST (07:00Z) to the first Sunday of November 02:00 EDT
# (06:00Z), the US rule since 2007. A governing date has passed once its day ended here.
EST, EDT = timedelta(hours=-5), timedelta(hours=-4)
US_RULE_SINCE = 2007
PORTAL_OPEN = frozenset({"SUBMITTED", "PARTIAL"})
PORTAL_CLOSED = frozenset({"DRAFT", "RECEIVED", "CANCELLED", "CANCELED", "CANCEL"})
BC_ORDERS, BC_LINES, BC_RECEIPTS = "purchaseOrders", "purchaseOrderLines", "purchaseReceipts"
BC_REFERENCES = "purchaseOrderReferences"   # ODataV4 Page 50: No, Your_Reference
ENTITY_MAP = "public.app_bc_entity_map"     # the Portal's own BC identity bridge
_FM_CLASH = re.compile(r"-fm-?\d+$")       # PO2-FM-106: an FM clash-series number
ZERO = decimal_of("0")


def business_today(now: datetime | None = None) -> date:
    """The business day (ET) of ``now`` (timezone-aware; default the current moment): the
    ``as_of`` lateness is judged against. A naive moment is refused (its zone is unknown)."""
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None \
            or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError("business_today_needs_an_aware_moment")
    utc = moment.astimezone(timezone.utc).replace(tzinfo=None)
    if utc.year < US_RULE_SINCE:
        raise ValueError("business_today_before_the_us_dst_rule")
    return (utc + (EDT if _daylight(utc) else EST)).date()


def _sunday(year: int, month: int, nth: int) -> date:
    """The ``nth`` Sunday of ``month``."""
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (nth - 1))


def _daylight(utc: datetime) -> bool:
    """Whether the naive UTC moment ``utc`` falls in Eastern daylight time."""
    start = datetime.combine(_sunday(utc.year, 3, 2), datetime.min.time()) + timedelta(hours=7)
    end = datetime.combine(_sunday(utc.year, 11, 1), datetime.min.time()) + timedelta(hours=6)
    return start <= utc < end


def _day(value: Any) -> date | None:
    moment = instant(value)
    return moment.date() if moment is not None else None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().upper() if isinstance(value, str) else ""
    return {"TRUE": True, "FALSE": False}.get(text)


def _decides(group: Mapping[str, Any]) -> bool:
    says_open = says_closed = False
    for item in group["portal"]:
        status = item["row"].get("status")
        says_open |= status in PORTAL_OPEN
        says_closed |= status in PORTAL_CLOSED
    for item in group["bc"]:
        received = _bool(item["fields"].get("fullyReceived"))
        says_open |= received is False
        says_closed |= received is True
        says_closed |= str(item["fields"].get("status") or "").strip().upper() == "DRAFT"
    return says_open and not says_closed


def _open_quantity(ordered: Any, received: Any) -> bool:
    want = decimal_of(ordered)
    got = decimal_of(received) if received is not None else ZERO
    return want is not None and got is not None and want > ZERO and got < want


def late_orders(portal: Mapping[str, Sequence[Mapping[str, Any]]],
                bc: Sequence[Mapping[str, Any]], as_of: date) -> dict[str, Any]:
    """The order groups and the late ones (module doc)."""
    pos = list(portal.get(PO) or ())
    fm_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in portal.get(FM_PO) or ():
        key = key_text(item["row"].get("id"))
        if key is not None:
            fm_by_id[key].append(item)
    bc_orders = [r for r in bc if r["entity_set"] == BC_ORDERS]
    bc_by_number: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in bc_orders:
        number = key_text(record["fields"].get("number"))
        if number is not None:
            bc_by_number[number].append(record)
    fm_claims: dict[str, int] = defaultdict(int)
    bc_claims: dict[str, int] = defaultdict(int)
    for item in pos:
        for key, claims in ((key_text(field_value(item["row"], "custom_fields.fmGeneratedPoId")),
                             fm_claims),
                            (key_text(field_value(item["row"], "custom_fields.renumberedTo")),
                             bc_claims)):
            if key is not None:
                claims[key] += 1
    ambiguous = 0
    groups: list[dict[str, Any]] = []
    joined_bc: set[int] = set()
    for item in pos:
        group: dict[str, Any] = {"portal": [item], "fm": [], "bc": []}
        fm_key = key_text(field_value(item["row"], "custom_fields.fmGeneratedPoId"))
        if fm_key is not None and fm_key in fm_by_id:
            if fm_claims[fm_key] == 1 and len(fm_by_id[fm_key]) == 1:
                group["fm"] = list(fm_by_id[fm_key])
            else:
                ambiguous += 1
        bc_key = key_text(field_value(item["row"], "custom_fields.renumberedTo"))
        if bc_key is not None and bc_by_number.get(bc_key):
            if bc_claims[bc_key] == 1 and len(bc_by_number[bc_key]) == 1:
                group["bc"] = list(bc_by_number[bc_key])
                joined_bc.add(id(bc_by_number[bc_key][0]))
            else:
                ambiguous += 1
        groups.append(group)
    map_counts = _join_entity_map(portal, bc_orders, groups, joined_bc)
    reference_counts = _join_your_references(portal, bc, groups, joined_bc)
    groups += [{"portal": [], "fm": [], "bc": [record]} for record in bc_orders
               if id(record) not in joined_bc]

    portal_lines: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in portal.get(PO_LINE) or ():
        portal_lines[key_text(item["row"].get("purchase_order_id")) or ""].append(item)
    portal_receipts: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in portal.get(RECEIPT) or ():
        portal_receipts[key_text(item["row"].get("purchase_order_id")) or ""].append(item)
    bc_lines: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    bc_receipts: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for record in bc:
        company = str(record.get("company_id") or "").lower()
        if record["entity_set"] == BC_LINES:
            bc_lines[(company, str(record["fields"].get("documentId") or "").lower())
                     ].append(record)
        elif record["entity_set"] == BC_RECEIPTS:
            bc_receipts[(company, str(record["fields"].get("orderNumber") or ""))
                        ].append(record)

    out_groups, late, opened, undated = [], [], 0, 0
    for group in groups:
        numbers = sorted({n for n in (
            [key_text(i["row"].get("po_number")) for i in group["portal"]]
            + [key_text(field_value(i["row"], "custom_fields.fmPoNumber"))
               for i in group["portal"]]
            + [key_text(i["row"].get("ponumber")) for i in group["fm"]]
            + [key_text(r["fields"].get("number")) for r in group["bc"]]) if n})
        headers = ([{"system": "portal", "citation": i["citation"]} for i in group["portal"]]
                   + [{"system": "fm", "citation": i["citation"]} for i in group["fm"]]
                   + [{"system": "bc", "citation": r["citation"]} for r in group["bc"]])
        lines = ([("portal", i["citation"], i["row"].get("quantity"),
                   i["row"].get("quantity_received"), None)
                  for p in group["portal"]
                  for i in portal_lines.get(key_text(p["row"].get("id")) or "", ())]
                 + [("bc", r["citation"], r["fields"].get("quantity"),
                     r["fields"].get("receivedQuantity"),
                     _day(r["fields"].get("expectedReceiptDate")))
                    for h in group["bc"]
                    for r in bc_lines.get((str(h.get("company_id") or "").lower(),
                                           str(h["fields"].get("id") or "").lower()), ())])
        receipts = ([i["citation"] for p in group["portal"]
                     for i in portal_receipts.get(key_text(p["row"].get("id")) or "", ())]
                    + [r["citation"] for h in group["bc"]
                       for r in bc_receipts.get((str(h.get("company_id") or "").lower(),
                                                 str(h["fields"].get("number") or "")), ())])
        record = {"numbers": numbers, "headers": headers, "bc": bool(group["bc"]),
                  "records": sorted({h["citation"] for h in headers}
                                    | {line[1] for line in lines} | set(receipts))}
        out_groups.append(record)
        if not _decides(group):
            continue
        opened += 1
        # The governing header date by precedence, with the record it is read from.
        header_date: tuple[date, str] | None = None
        for item, field in ([(i, "datearrive") for i in group["fm"]]
                            + [(i, "expected_date") for i in group["portal"]]):
            day = _day(item["row"].get(field))
            if day is not None:
                header_date = (day, item["citation"])
                break
        bc_header = next(((d, r["citation"]) for r in group["bc"]
                          for d in [_day(r["fields"].get("requestedReceiptDate"))]
                          if d is not None), None)
        bc_dates = {(line[4], line[1]) for line in lines if line[0] == "bc" and line[4]}
        if not bc_dates and bc_header is not None:
            bc_dates = {bc_header}
        single_bc = next(iter(bc_dates)) if len({d for d, _ in bc_dates}) == 1 else None
        open_lines = [line for line in lines if _open_quantity(line[2], line[3])]
        units = []
        for origin, citation, _q, _r, own in open_lines:
            due = header_date or ((own, citation) if own is not None else
                                  bc_header if origin == "bc" else single_bc)
            units.append((origin, citation, due))
        if not lines:   # an open order holding no line: judged on its header date
            units = [(h["system"], h["citation"], header_date or bc_header) for h in headers]
        if not any(due for *_x, due in units):
            undated += 1
        late_units = [{"system": origin, "citation": citation, "due": due[0].isoformat(),
                       "date_source": due[1], "line": bool(lines)}
                      for origin, citation, due in units if due and due[0] < as_of]
        if late_units:
            late.append({**record, "late_lines": late_units, "receipts": sorted(receipts),
                         "governing_dates": sorted({u["due"] for u in late_units})})
    return {"groups": out_groups, "late": late,
            "counts": {"groups": len(out_groups), "open": opened, "late": len(late),
                       "undated": undated, "ambiguous": ambiguous, **map_counts,
                       **reference_counts}}


def _join_entity_map(portal: Mapping[str, Sequence[Mapping[str, Any]]],
                     bc_orders: Sequence[Mapping[str, Any]], groups: list[dict[str, Any]],
                     joined_bc: set[int]) -> dict[str, int]:
    """Join each BC order the Portal's entity map says a Portal order was posted as into
    that order's group (module doc); the counts: joined, already joined (the same pair by
    ``renumberedTo``), conflict (a second row for either end, or either order joined to
    another) and unmatched (the map names no captured BC order)."""
    counts = Counter({"entity_map_joined": 0, "entity_map_already_joined": 0,
                      "entity_map_conflict": 0, "entity_map_unmatched": 0})
    by_id = {(str(r.get("company_id") or "").lower(), str(r["fields"].get("id") or "").lower()):
             r for r in bc_orders}
    group_of = {key_text(item["row"].get("id")): group
                for group in groups for item in group["portal"]}
    pairs = []
    for item in portal.get(ENTITY_MAP) or ():
        row = item["row"]
        if (row.get("entity_type"), row.get("source_system"), row.get("status"),
                row.get("bc_environment")) != ("purchaseOrder", "portal", "LINKED",
                                               "Production"):
            continue
        source = key_text(row.get("source_id"))
        target = (str(row.get("bc_company_id") or "").lower(),
                  str(row.get("bc_system_id") or "").strip().lower())
        if source is None or not all(target):
            continue
        pairs.append((source, target))
    sources = Counter(source for source, _ in pairs)
    targets = Counter(target for _, target in pairs)
    for source, target in pairs:
        group, record = group_of.get(source), by_id.get(target)
        if group is None or record is None:
            counts["entity_map_unmatched"] += 1
            continue
        if any(r is record for r in group["bc"]):
            counts["entity_map_already_joined"] += 1
            continue
        if sources[source] > 1 or targets[target] > 1 or group["bc"] \
                or id(record) in joined_bc:
            counts["entity_map_conflict"] += 1
            continue
        group["bc"].append(record)
        joined_bc.add(id(record))
        counts["entity_map_joined"] += 1
    return dict(counts)


def _fold(value: Any) -> str | None:
    """The Portal's exact comparison of a PO number: trimmed, case-insensitive."""
    return value.strip().casefold() if isinstance(value, str) and value.strip() else None


def _order_id(citation: Any) -> str:
    """The BC order a Page 50 record belongs to: the last path part of its handle."""
    base = citation.rsplit("@", 1)[0] if isinstance(citation, str) else ""
    return base.rsplit("/", 1)[-1].lower()


def _join_your_references(portal: Mapping[str, Sequence[Mapping[str, Any]]],
                          bc: Sequence[Mapping[str, Any]], groups: list[dict[str, Any]],
                          joined_bc: set[int]) -> dict[str, int]:
    """Join each BC order whose reference ``po_reference.decide`` finds firm into its
    Portal order's group (module doc); the counts by verdict code and corroborating field."""
    counts = Counter({"your_reference_firm": 0, "your_reference_lead": 0,
                      "your_reference_already_joined": 0,
                      "your_reference_firm_by_supplier": 0,
                      "your_reference_firm_by_order_date": 0})
    orders = {(str(r.get("company_id") or "").lower(), str(r["fields"].get("id") or "").lower()):
              r for r in bc if r["entity_set"] == BC_ORDERS}
    refs: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for record in bc:
        if record["entity_set"] == BC_REFERENCES:
            refs[(str(record.get("company_id") or "").lower(),
                  _order_id(record.get("citation")))].append(record)
    by_fm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_number: dict[str, list[dict[str, Any]]] = defaultdict(list)
    group_of: dict[int, dict[str, Any]] = {}
    for group in groups:
        for item in group["portal"]:
            group_of[id(item)] = group
            for key, index in ((_fold(field_value(item["row"], "custom_fields.fmPoNumber")),
                                by_fm), (_fold(item["row"].get("po_number")), by_number)):
                if key is not None:
                    index[key].append(item)
    vendors: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in portal.get(ENTITY_MAP) or ():
        row = item["row"]
        number = _fold(row.get("bc_number"))
        if (row.get("entity_type"), row.get("source_system"), row.get("status")) == (
                "vendor", "portal", "LINKED") and number and key_text(row.get("source_id")):
            vendors[(key_text(row.get("source_id")) or "",
                     str(row.get("bc_company_id") or "").lower())].add(number)

    pending = []
    for key, record in sorted(orders.items()):
        found = refs.get(key) or []
        if len(found) != 1:
            continue
        fields = found[0]["fields"]
        reference = _fold(fields.get("Your_Reference"))
        if reference is None or _fold(fields.get("No")) != _fold(record["fields"].get("number")):
            continue
        candidates = by_fm.get(reference) or (
            by_number.get(reference, []) if _FM_CLASH.search(reference) else [])
        unique = {id(item): item for item in candidates}
        if unique:
            pending.append((record, list(unique.values())))
    claimants = Counter(id(item) for _record, found in pending for item in found)
    for record, found in pending:
        company = str(record.get("company_id") or "").lower()
        for item in found:
            group = group_of[id(item)]
            if any(r is record for r in group["bc"]):
                counts["your_reference_already_joined"] += 1   # by renumberedTo
                continue
            row = item["row"]
            vendor = _fold(record["fields"].get("vendorNumber"))
            mapped = vendors.get((key_text(row.get("supplier_id")) or "", company))
            supplier = None if not (vendor and mapped) else vendor in mapped
            verdict = po_reference.decide(
                portal_matches=len(found), bc_claimants=claimants[id(item)],
                authority=(field_value(row, "custom_fields.source") in PO_FM_SOURCES
                           and field_value(row, "custom_fields.qaSmoke") is not True),
                other_partner=bool(group["bc"]) or id(record) in joined_bc,
                supplier=supplier,
                order_days_apart=po_reference.days_apart(
                    _day(row.get("date_generated")), _day(record["fields"].get("orderDate"))))
            counts[f"your_reference_{verdict.tier or verdict.code}"] += 1
            if verdict.tier == po_reference.FIRM:
                for field in verdict.corroborated_by:
                    counts[f"your_reference_firm_by_{field}"] += 1
                group["bc"].append(record)
                joined_bc.add(id(record))
            elif verdict.tier == po_reference.LEAD:
                counts[f"your_reference_lead_{verdict.code}"] += 1
    return dict(counts)
