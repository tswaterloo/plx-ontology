"""Operations alerts over stock, lots, assembly, certificates and freight. Run by
``operating.proactive_engine`` beside its other rules (each in its own time window,
``DETECTOR_SECONDS``), set-based over the whole store with indexed reads only (records by
the type index, links by their end indexes, ``_Store.touching``); every figure is a cited
value or a sum of cited values in one unit and one currency (``Decimal``, never converted).

Each rule reads what the loaded schema declares (``declares``): a field it needs that no
source serves leaves it ``blocked`` with ``not_served_<Type>__<property>`` reasons
(``waiting``), never a clean "nothing found".

* ``slow_moving_stock``: an item (a settled ``Product.same_record_as`` cluster) with stock on
  hand (``proactive_business.ON_HAND``; the first source stating it wins, never summed across
  sources) and no customer order, assembly use or ledger sale, consumption or transfer
  taking stock out (``MOVEMENTS``; a ledger entry dated by its posting event; a count
  correction or write-off is no movement) in ``SLOW_DAYS`` (90, 180 or
  365 days: the bucket names it), or none recorded. An item tied to another record by an
  unsettled (lead) identity or movement link is not judged (``identity_unsettled``,
  ``movement_unsettled``): its sales or use may sit on that record. The stock's value (on
  hand times the item's own unit cost, only when the cost is stated per the unit on hand) is
  in the event only, finance-marked. The claim rests on no later movement being recorded:
  ``REQUIRES_GRANT``.
* ``expiring_lot_stock``: a lot with quantity on hand (an ``ON_HAND`` source of lots) whose
  expiry falls within ``LOT_DAYS`` (30 high, 60 medium), with the open customer orders (the
  one sales-order definition) that name its item and could use it.
* ``assembly_component_shortfall``: an open assembly order (judged on the copy that keeps
  production, ``assembly_keeper``; done or deleted in any copy: not open) starting within
  ``ASSEMBLY_HORIZON_DAYS`` whose components (planned less consumed) need more than is on
  hand plus inbound on open purchase orders (``proactive_business.inbound_flows``) by its
  start date, the orders before it in start-date order served first. ``REQUIRES_GRANT``.
* ``certificate_expiry``: a certificate (``CERTIFICATES``: certificates of analysis, supplier
  certificates) stating an expiry or review date within ``CERT_DAYS``; copies of one
  certificate (same supplier, material, lot and date) are one. A date read from a printed
  numeric date whose day and month cannot be told apart (``cited_day``: 12/03/2026, with no
  other date of the document forcing the order) is no day (``date_order_ambiguous``,
  counted). Machine-read values are
  leads, said as read.
* ``freight_cost_anomaly``: a freight bill's amount per shipment against the median of the
  same carrier's and lane's other bills in one currency over ``FREIGHT_TRAILING_DAYS`` (at
  least ``FREIGHT_MIN_HISTORY``), ``FREIGHT_RATIO`` or more above it, billed within
  ``FREIGHT_RECENT_DAYS``. Every citation carries ``finance``.
* ``reorder_soon``: an item whose forecast demand over the next ``operating.demand_forecast``
  horizon (3 months, from invoiced quantities of the last 3 years, twins once) exceeds what
  is on hand (an ``ON_HAND`` source) plus what open purchase orders bring in by the
  horizon's end, in one unit; high when even the forecast's 80% low bound exceeds it. The
  claim rests on no other inbound order being recorded: ``REQUIRES_GRANT``.

Fixture runs are ``isolated-fixture``; no rule names a record, a customer or an item.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import functools
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from ontology.units import unit_token

from . import proactive_business as pb

SLOW_RULE = "slow_moving_stock"
LOT_RULE = "expiring_lot_stock"
ASSEMBLY_RULE = "assembly_component_shortfall"
CERT_RULE = "certificate_expiry"
FREIGHT_RULE = "freight_cost_anomaly"
RULES = (SLOW_RULE, LOT_RULE, ASSEMBLY_RULE, CERT_RULE, FREIGHT_RULE)
REORDER_RULE = "reorder_soon"     # its own window: it reads 3 years of invoice lines
SLOW = "slow_moving_stock"
LOT_EXPIRING = "expiring_lot_stock"
ASSEMBLY_SHORT = "assembly_blocked_by_component"
CERT_EXPIRING = "certificate_expiring"
FREIGHT_HIGH = "freight_cost_anomaly"
REORDER = "reorder_soon"
KINDS = (SLOW, LOT_EXPIRING, ASSEMBLY_SHORT, CERT_EXPIRING, FREIGHT_HIGH)
REORDER_BUCKET = "month"
REORDER_HISTORY_CITED = 6           # the latest invoice lines cited as the forecast's basis
FINANCE = pb.FINANCE
NOT_SERVED = "not_served_"   # + Type__property: a reason word the tool contracts carry

SLOW_DAYS = (365, 180, 90)          # buckets, oldest first; under the last: moving
LOT_DAYS = (30, 60)
ASSEMBLY_HORIZON_DAYS = 60
CERT_DAYS = (30, 60)
FREIGHT_TRAILING_DAYS = 365
FREIGHT_RECENT_DAYS = 90
FREIGHT_MIN_HISTORY = 3
FREIGHT_RATIO = Decimal("1.5")
FREIGHT_HIGH_RATIO = Decimal(2)
MAX_CITED = 20                      # compared bills, open orders or inbound lines cited
EVENT_CHUNK = 2000                  # records whose events one statement reads

ITEM = pb.ITEM
COMPONENT = "AssemblyOrderComponent"
COMPONENT_OF = "AssemblyOrderComponent.component_of"
COMPONENT_ITEM = "AssemblyOrderComponent.uses_item"
ASSEMBLY = "AssemblyOrder"
ASSEMBLY_TWINS = "AssemblyOrder.same_record_as"
ASSEMBLY_DAYS = ("fm_created_at", "portal_created_at")
ASSEMBLY_CODES = ("ao_code", "mo_code", "code")
# the assembly order's planned start: its own start date where a source fills it (the Portal
# MO's never is), else the day the system that keeps production scheduled it to be made
START_PROPS = ("start_date", "scheduled_date")
# an assembly order still to be made (its components not yet picked); another word is
# not judged (``status_unknown``), a done or cancelled one is never claimed
OPEN_ASSEMBLY = frozenset({"generated", "allocated", "committed", "created", "scheduled",
                           "released"})
DONE_ASSEMBLY = frozenset({"realized", "cancelled", "canceled", "in_production", "completed",
                           "closed", "finished"})
LOT_EXPIRY = "expiry_date"          # on an ``ON_HAND`` lot source
LOT_NUMBER = "lot_number"
FREIGHT = "FreightBill"
FREIGHT_TWINS = "FreightBill.same_record_as"
FREIGHT_SHIPMENT = "FreightBill.for_shipment"
FREIGHT_PROPS = ("amount", "carrier", "port_of_loading", "port_of_discharge",
                 "document_date", "ship_date", "invoice_number", "tracking_number")
# what an item's cost is stated per: (cost property, the item property naming its unit)
UNIT_COST = (("bc_unit_cost", "bc_base_uom"), ("average_cost", "uom"))
CODE = "code"


@dataclass(frozen=True)
class Movement:
    """Records that show an item sold or used: a line naming the item (``item_link``),
    dated on itself or on its parent (``parent_link``, ``dated_type``) by the first of
    ``day_props`` it states, or by its ``day_event`` (an event of the dated record, its
    time); ``status``: (property, words never counted, words required; Business Central
    enum escapes decoded; with both sets named, a word in neither leaves the item unjudged:
    ``movement_type_unknown``); ``outflow``: a quantity property that must be below zero (stock
    taken out) for the line to count."""
    label: str
    line_type: str
    item_link: str
    parent_link: str | None
    dated_type: str
    day_props: tuple[str, ...]
    status: tuple[str, frozenset, frozenset | None] | None = None
    day_event: str | None = None
    outflow: str | None = None


# the ledger entry types that are movement (``_enum_word``: BC's escapes decoded, folded)
LEDGER_MOVES = frozenset({"sale", "consumption", "assembly consumption", "transfer"})
# the other ledger entry types BC has: never movement (an adjustment is a count correction or
# a write-off); a type in neither set leaves its item unjudged (``movement_type_unknown``)
LEDGER_OTHER = frozenset({"purchase", "positive adjmt.", "negative adjmt.", "output",
                          "assembly output"})
MOVEMENTS = (
    Movement("customer order", "SalesOrderLine", pb.SO_LINE_ITEM, pb.SO_LINE_OF, "SalesOrder",
             ("order_date",), ("status", pb.VOID_ORDER, None)),
    Movement("assembly use", COMPONENT, COMPONENT_ITEM, COMPONENT_OF, ASSEMBLY, ASSEMBLY_DAYS,
             ("status", frozenset({"cancelled", "canceled"}), None)),
    # a ledger sale, consumption or transfer taking stock out (the quantity below zero),
    # dated by its posting event (the ledger states no date property). A count correction or
    # write-off (Negative Adjmt.) is no movement: it would reset the clock of a real slow
    # mover (3,022 live, 2,075 of them on one count day)
    Movement("ledger outflow (sale, use or transfer)", "ItemLedgerEntry",
             "ItemLedgerEntry.for_item", None, "ItemLedgerEntry", (),
             ("entry_type", LEDGER_OTHER, LEDGER_MOVES), day_event="ItemLedgerEntry.posted",
             outflow="quantity"),
)


@dataclass(frozen=True)
class Certificate:
    label: str
    type: str
    day_props: tuple[str, ...]      # expiry or review dates, any it states
    supplier: str | None
    material: str | None
    lot: str | None


CERTIFICATES = (
    Certificate("certificate of analysis", "CertificateOfAnalysis", ("expiry_date",),
                "supplier", "product", "lot"),
    Certificate("supplier certificate", "RelPublicAppSupplierCertification",
                ("expiry_date", "review_date"), None, None, None),
)

# plain words for a field a rule waits on (the briefing's check line names them)
FIELD_WORDS = {"Product.quantity_on_hand": "item stock on hand",
               "InventoryLot.quantity_on_hand": "lot stock on hand",
               "InventoryLot.expiry_date": "lot expiry date",
               "AssemblyOrder.start_date": "assembly order start date",
               "ItemLedgerEntry.posted": "item ledger posting date",
               "RelPublicAppSupplierCertification.expiry_date": "supplier certificate expiry",
               "RelPublicAppSupplierCertification.review_date": "supplier certificate review",
               "FreightBill.amount": "freight bill amount"}


def waiting(field: str) -> str:
    """The reason word for a field no source serves (``Type.property`` ->
    ``not_served_Type__property``, inside the contracts' reason pattern)."""
    return NOT_SERVED + field.replace(".", "__", 1)


def field_of(reason: str) -> str | None:
    """The ``Type.property`` a ``waiting`` reason names; None for another reason."""
    if not isinstance(reason, str) or not reason.startswith(NOT_SERVED):
        return None
    return reason[len(NOT_SERVED):].replace("__", ".", 1)


def field_words(reasons: Iterable[str]) -> list[str]:
    """The fields a blocked rule waits on, in plain words."""
    fields = [f for f in (field_of(r) for r in reasons) if f]
    return [FIELD_WORDS.get(f, f.split(".")[-1].replace("_", " ")) for f in fields]


# --------------------------------------------------------------------------- shared reads

@functools.lru_cache(maxsize=1)
def _schema() -> Any:
    from ontology.schema import load_schema

    return load_schema()


def declares_event(event_type: str) -> bool:
    """Whether the loaded schema declares the event type."""
    return event_type in (_schema().event_types or {})


def _served(m: Movement) -> bool:
    """A movement source the loaded schema serves whole: its item link, parent link and
    every day it is dated by."""
    return (declares(m.line_type, link=m.item_link)
            and (m.parent_link is None or declares(m.line_type, link=m.parent_link))
            and all(declares(m.dated_type, p) for p in m.day_props)
            and (m.day_event is None or declares_event(m.day_event))
            and (m.outflow is None or declares(m.line_type, m.outflow)))


def _unserved(m: Movement) -> list[str]:
    return [*(f"{m.dated_type}.{p}" for p in m.day_props if not declares(m.dated_type, p)),
            *((m.day_event,) if m.day_event and not declares_event(m.day_event) else ())]


def _enum_word(row: Mapping[str, Any] | None) -> str | None:
    """A status word folded, Business Central's XML escapes (``_x0020_``) decoded."""
    if row is None:
        return None
    import re
    text = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)),
                  str(row.get("value") or ""))
    return " ".join(text.split()).casefold() or None


def _event_days(st: Any, records: Sequence[str], event_type: str
                ) -> dict[str, tuple[Mapping[str, Any], date]]:
    """record -> (its latest ``event_type`` event as a cited row, its day), read by the
    event-object index in chunks."""
    out: dict[str, tuple[Mapping[str, Any], date]] = {}
    ids = sorted(set(records))
    sql = ("SELECT eo.object_id, e.time, e.l1_handle, e.markings FROM event_objects eo "
           "JOIN events e ON e.event_id = eo.event_id WHERE e.event_type = :type "
           "AND e.superseded_at IS NULL AND eo.object_id IN (SELECT value FROM json_each(:chunk))")
    import json
    for start in range(0, len(ids), EVENT_CHUNK):
        pb._tick(st)
        for oid, time, handle, markings in st.conn.execute(sql, {
                "type": event_type, "chunk": json.dumps(ids[start:start + EVENT_CHUNK])}):
            day = pb._day(time)
            if day is None or (oid in out and out[oid][1] >= day):
                continue
            out[oid] = ({"object_id": oid, "l1_handle": handle, "markings": markings}, day)
    return out


def _words(labels: Sequence[str]) -> str:
    """``a``, ``a or b``, ``a, b or c``."""
    labels = list(dict.fromkeys(labels))
    return labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} or {labels[-1]}"


def declares(object_type: str, prop: str | None = None, link: str | None = None) -> bool:
    """Whether the loaded schema declares the type (its property, the link)."""
    schema = _schema()
    spec = schema.object_types.get(object_type)
    if spec is None:
        return False
    if prop is not None and prop not in {p.get("name") for p in spec.get("properties", ())}:
        return False
    return link is None or link in schema.link_types


def assembly_keeper() -> str | None:
    """The binding prefix of the assembly order copy that keeps production (its status and
    scheduled day): ``proactive_ops.PRODUCTION_DUE``'s, one definition for both rules."""
    from . import proactive_ops

    return next((keeper for t, *_rest, keeper in proactive_ops.PRODUCTION_DUE
                 if t == ASSEMBLY), None)


def declared_starts() -> tuple[str, ...]:
    """The assembly order start-date properties the schema declares."""
    return tuple(p for p in START_PROPS if declares(ASSEMBLY, p))


def _touch(st: Any, objects: Iterable[str], types: Sequence[str],
           settled: Sequence[str]) -> list[dict[str, Any]]:
    """Settled current links of ``types`` with an end at one of ``objects`` (index reads)."""
    objects = sorted(set(objects))
    if not objects:
        return []
    keep = frozenset(settled)
    return [{"link_id": r[1], "link_type": r[2], "from_object": r[3], "to_object": r[4],
             "tier": r[5], "markings": r[6]}
            for r in st.touching(objects, list(types)) if r[5] in keep and not r[7]]


# Above this many records a link type is read whole by its type index (``_Store.of_type``,
# once per run, shared by every rule asking it) rather than by its end indexes at each
# record (two statements per 500 records: 40 s for the 18k customers, 2026-11-12).
BY_TYPE_FROM = 2000


def identity_rows(st: Any, ids: Iterable[str], link: str) -> list[tuple]:
    """The current ``link`` rows (``_LinkRows``, contradicted ones too) with an end at one
    of ``ids``, in rowid order: by the type index when ``ids`` are many, else by the end
    indexes at ``ids``, never a whole-table scan."""
    ids = set(ids)
    if not ids:
        return []
    if len(ids) > BY_TYPE_FROM and callable(getattr(st, "of_type", None)):
        return [r for r in st.of_type(link) if r[3] in ids or r[4] in ids]
    return st.touching(sorted(ids), [link], every=True)


def _clusters(st: Any, ids: Iterable[str], link: str, settled: Sequence[str]
              ) -> tuple[dict[str, str], dict[str, set[str]]]:
    """member -> its settled identity cluster's least id over ``link`` (``identity_rows``);
    member -> the records a lead (unsettled) identity link alone ties it to. A link a
    person rejected (contradicted) ties nothing."""
    from . import po_status

    ids = set(ids)
    keep = frozenset(settled)
    pairs, leads = [], defaultdict(set)
    for r in identity_rows(st, ids, link):
        if r[7] or r[5] == "contradicted":
            continue
        if r[5] in keep:
            pairs.append((r[3], r[4]))
        else:
            leads[r[3]].add(r[4])
            leads[r[4]].add(r[3])
    return po_status.clusters(pairs, ids | {e for p in pairs for e in p}), leads


def _item_identity(st: Any, settled: Sequence[str]
                   ) -> tuple[dict[str, str], dict[str, set[str]]]:
    """(item record -> its settled ``Product.same_record_as`` cluster's least id, item
    record -> the records a lead identity link alone ties it to), read once per run over
    every item record by index."""
    def read() -> tuple[dict[str, str], dict[str, set[str]]]:
        ids = [r[0] for r in st.conn.execute(
            "SELECT object_id FROM objects WHERE object_type = ?", (ITEM,))]
        return _clusters(st, ids, pb.ITEM_TWINS, settled)
    return st.memo("stock:item_identity", read)


def _item_roots(st: Any, settled: Sequence[str]) -> dict[str, str]:
    """Item record -> its settled ``Product.same_record_as`` cluster's least id."""
    return _item_identity(st, settled)[0]


def _members(item_root: Mapping[str, str], roots: Iterable[str]) -> set[str]:
    wanted = set(roots)
    return wanted | {m for m, r in item_root.items() if r in wanted}


def _fold(raw: Any) -> str:
    return " ".join(str(raw or "").split()).casefold()


def _first(props: Mapping[str, list], names: Sequence[str]) -> tuple[Mapping | None, date | None]:
    """The first of ``names`` the record states one readable day for."""
    for name in names:
        row = pb._one(props.get(name))
        day = pb._day(row["value"]) if row is not None else None
        if day is not None:
            return row, day
    return None, None


def _money(n: Decimal) -> str:
    """A money figure to the cent, never normalized away ("250.00")."""
    return format(n.quantize(Decimal("0.01")), "f")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _stock(st: Any, sources: Sequence[tuple[str, str, str | None]],
           item_root: Mapping[str, str], settled: Sequence[str], cites: Callable,
           roots: Iterable[str] | None = None
           ) -> tuple[dict[str, tuple[Decimal, str | None, list]], dict[str, str]]:
    """Item cluster -> (quantity on hand, unit, its cited group), from the first source that
    states it (an item's own record: on its governing record, BC first; lots: summed in one
    unit); and item cluster -> why its stock cannot be read. ``roots``: only these items."""
    def item_of(r: str) -> str:
        return item_root.get(r, r)

    wanted = None if roots is None else set(roots)
    out: dict[str, tuple[Decimal, str | None, list]] = {}
    broken: dict[str, str] = {}
    for object_type, prop, via in sources:
        pb._tick(st)
        if via is None:
            held = (st.values([object_type], [prop]) if wanted is None else
                    st.values([object_type], [prop],
                              objects=sorted(_members(item_root, wanted))))
            stating: dict[str, list[str]] = defaultdict(list)
            for oid, entry in held.items():
                if entry["props"].get(prop):
                    stating[item_of(oid)].append(oid)
            for top, holders in sorted(stating.items()):
                if top in out:
                    continue
                from . import po_status
                row = pb._one(held[pb._governing(holders, po_status.binding_of)]["props"][prop])
                qty = pb._dec(row["value"]) if row is not None else None
                if qty is None:
                    broken.setdefault(top, "on_hand_unreadable")
                    continue
                out[top] = (qty, unit_token(row.get("unit")), cites([row]))
            continue
        held = st.values([object_type], [prop])
        ties = {l["from_object"]: l for l in _touch(st, held, [via], settled)
                if l["from_object"] in held}
        lots: dict[str, list] = defaultdict(list)
        for oid, entry in sorted(held.items()):
            tie = ties.get(oid)
            if tie is None or (wanted is not None and item_of(tie["to_object"]) not in wanted):
                continue
            row = pb._one(entry["props"].get(prop))
            qty = pb._dec(row["value"]) if row is not None else None
            top = item_of(tie["to_object"])
            if qty is None:
                broken.setdefault(top, "on_hand_unreadable")
                continue
            lots[top].append((qty, unit_token(row.get("unit")), row, tie))
        for top, found in lots.items():
            if top in out or top in broken:
                continue
            units = {u for _q, u, _r, _t in found}
            if len(units) != 1:
                broken[top] = "unit_mismatch"
                continue
            out[top] = (sum((q for q, *_ in found), Decimal(0)), units.pop(),
                        cites([r for _q, _u, r, _t in found],
                              *(t["markings"] for *_, t in found)))
    return out, broken


def _sources(on_hand: Sequence[tuple[str, str, str | None]] | None
             ) -> tuple[tuple[tuple[str, str, str | None], ...], list[str]]:
    """The on-hand sources to read and, when none, the fields waited on."""
    sources = pb.declared_on_hand() if on_hand is None else tuple(on_hand)
    return sources, ([] if sources else [f"{t}.{p}" for t, p, _v in pb.ON_HAND])


def _blocked(result: Callable, fields: Iterable[str]) -> dict[str, Any]:
    out = result([], status="blocked",
                 reasons=[waiting(f) for f in dict.fromkeys(fields)])
    out["counts"] = {}
    return out


def _code(values: Mapping[str, Any], members: Iterable[str], props: Sequence[str]
          ) -> Mapping[str, Any] | None:
    for prop in props:
        for oid in sorted(members):
            row = pb._one(((values.get(oid) or {}).get("props") or {}).get(prop))
            if row is not None and str(row.get("value") or "").strip():
                return row
    return None


# --------------------------------------------------------------------------- slow stock

def slow_moving_stock(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                      result: Callable, settled: Sequence[str], skips: Callable,
                      absence: Callable,
                      on_hand: Sequence[tuple[str, str, str | None]] | None = None,
                      movements: Sequence[Movement] = MOVEMENTS) -> dict[str, Any]:
    sources, missing = _sources(on_hand)
    served = [m for m in movements if _served(m)]
    unserved = [f for m in movements if m not in served for f in _unserved(m)]
    if not served:
        missing += unserved
    if missing:
        return _blocked(result, missing)
    skipped = skips()
    counts: Counter = Counter()
    item_root, item_leads = _item_identity(st, settled)
    stock, broken = _stock(st, sources, item_root, settled, cites)
    stocked = {top for top, (qty, _u, _g) in stock.items() if qty > 0}
    counts["stocked"] = len(stocked)
    for top, why in sorted(broken.items()):
        skipped.note(why, [top], [(top, "[]")])
    members = _members(item_root, stocked)
    # item cluster -> why its movements cannot all be read: a lead identity link ties a
    # member to another record (its sales may sit there), or a lead link names it on a
    # movement line
    unsettled: dict[str, str] = {}
    for m in sorted(members):
        if item_leads.get(m):
            unsettled.setdefault(item_root.get(m, m), "identity_unsettled")
    keep = frozenset(settled)
    # item cluster -> (day, rows, markings, label) of its latest sale or use
    latest: dict[str, tuple] = {}
    undated: dict[str, list[str]] = defaultdict(list)   # item cluster -> undated movements
    for move in served:
        pb._tick(st)
        lines = []
        for r in st.touching(sorted(members), [move.item_link]) if members else ():
            if r[7] or r[4] not in members:
                continue
            if r[5] in keep:
                lines.append({"link_id": r[1], "link_type": r[2], "from_object": r[3],
                              "to_object": r[4], "tier": r[5], "markings": r[6]})
            else:
                unsettled.setdefault(item_root.get(r[4], r[4]), "movement_unsettled")
        parent_of: dict[str, dict] = {}
        if move.parent_link is not None:
            parent_of = {l["from_object"]: l for l in _touch(
                st, {l["from_object"] for l in lines}, [move.parent_link], settled)
                if l["link_type"] == move.parent_link}
        dated = sorted({(parent_of[l["from_object"]]["to_object"] if move.parent_link
                         else l["from_object"]) for l in lines
                        if move.parent_link is None or l["from_object"] in parent_of})
        props = [*move.day_props, *([move.status[0]] if move.status else []),
                 *([move.outflow] if move.outflow else [])]
        found = st.values([move.dated_type], props, objects=dated)
        posted = _event_days(st, dated, move.day_event) if move.day_event else {}
        for link in lines:
            tie = parent_of.get(link["from_object"]) if move.parent_link else None
            record = tie["to_object"] if tie else link["from_object"]
            entry = (found.get(record) or {}).get("props") or {}
            unknown = False
            if move.status:
                word = _enum_word(pb._one(entry.get(move.status[0])))
                if word in move.status[1]:
                    continue
                if move.status[2] is not None and word not in move.status[2]:
                    if not move.status[1]:
                        continue
                    # neither a movement word nor a named other one: never read as "no
                    # movement" (the item is not judged), once it takes stock out
                    unknown = True
            if move.outflow:
                taken = pb._one(entry.get(move.outflow))
                qty = pb._dec(taken["value"]) if taken is not None else None
                if qty is None or qty >= 0:
                    continue
            if unknown:
                unsettled.setdefault(item_root.get(link["to_object"], link["to_object"]),
                                     "movement_type_unknown")
                continue
            row, day = _first(entry, move.day_props)
            if day is None and record in posted:
                row, day = posted[record]
            top = item_root.get(link["to_object"], link["to_object"])
            if day is None or day > as_of:
                counts["undated_movement" if day is None else "future_movement"] += 1
                # a known movement whose day is unknown: the item's recency is unproven
                # (BIZ-016), never "no movement recorded" nor the oldest bucket
                undated[top].append(record)
                continue
            marks = (link["markings"], *((tie["markings"],) if tie else ()))
            if top not in latest or day > latest[top][0]:
                latest[top] = (day, [row], marks, move.label)
    costs = st.values([ITEM], [CODE, *(p for pair in UNIT_COST for p in pair)],
                      objects=sorted(members))
    codes = pb._currencies(st, [r for e in costs.values() for rows in e["props"].values()
                                for r in rows])
    requires = absence([*{t for m in served for t in (m.line_type, m.dated_type)},
                        *(t for t, _p, _v in sources)])
    words = _words([m.label for m in served])
    gaps = []
    for top in sorted(stocked):
        pb._tick(st)
        qty, unit, group = stock[top]
        last = latest.get(top)
        age = (as_of - last[0]).days if last else None
        if age is not None and age < SLOW_DAYS[-1]:
            counts["moving"] += 1
            continue
        if top in unsettled:
            skipped.note(unsettled[top], [top], [(top, "[]")])
            continue
        if undated.get(top):
            # a later movement may be the undated one: not judged, said per item
            skipped.note("movement_date_unknown", [top], [(top, "[]")])
            continue
        bucket = next(d for d in SLOW_DAYS if age is None or age >= d)
        counts[f"over_{bucket}"] += 1
        held = _members(item_root, [top])
        code = _code(costs, held, (CODE,))
        label = f"Item {str(code['value']).strip()}" if code else "An item"
        stock_words = f"{pb._text(qty)} {unit or 'units'} on hand"
        if last:
            summary = (f"{label}: {stock_words} and no {words} in {age} days "
                       f"(last {last[0].isoformat()})")
        else:
            summary = f"{label}: {stock_words} and no {words} recorded"
        event: dict[str, Any] = {"days": age} if age is not None else {}
        value = _stock_value(costs, codes, held, qty, unit, cites)
        if value is not None:
            event["amount"] = value
        made = gap(SLOW, SLOW_RULE, top, ITEM, f"slow|{bucket}",
                   summary=summary,
                   missing="Should this stock be sold off, used in another product, "
                           "returned to the supplier or written down?",
                   why=f"The item has stock on hand and no {words} in the last {bucket} "
                       "days in any system (the stock and the last movement cited).",
                   priority="high" if bucket >= 365 else "medium" if bucket >= 180 else "low",
                   about=(ITEM, sources[0][1]),
                   groups=[group] + ([cites(last[1], *last[2])] if last else []),
                   extra=cites([code]) if code else (), event=event)
        if made is not None:
            made["requires"] = requires
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped,
                 reasons=[waiting(f) for f in dict.fromkeys(unserved)])
    out["counts"] = {k: counts.get(k, 0) for k in (
        "stocked", "moving", "over_90", "over_180", "over_365", "undated_movement")}
    return out


def _stock_value(costs: Mapping[str, Any], codes: Mapping[tuple, str | None],
                 members: Iterable[str], qty: Decimal, unit: str | None,
                 cites: Callable) -> dict[str, Any] | None:
    """On hand times the item's unit cost, when one cost is stated per the unit on hand
    (its unit named on the same record) in one currency; finance-marked; else None."""
    from . import po_status

    held = [m for m in members if m in costs]
    if not held or unit is None:
        return None
    record = pb._governing(held, po_status.binding_of)
    props = costs[record]["props"]
    for cost_prop, unit_prop in UNIT_COST:
        cost, per = pb._one(props.get(cost_prop)), pb._one(props.get(unit_prop))
        price = pb._dec(cost["value"]) if cost else None
        if price is None or per is None or unit_token(per["value"]) != unit:
            continue
        currency = pb._currency(codes, cost)
        if currency is None:
            continue
        return {"value": _money(qty * price),
                "currency": currency, "basis": cost_prop, "handle": cost["l1_handle"],
                "currency_handle": cost["l1_handle"],
                "markings": (cites([cost, per], FINANCE)
                             or [(None, None, '["unreadable"]')])[0][2]}
    return None


# --------------------------------------------------------------------------- lots

def expiring_lot_stock(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                       result: Callable, settled: Sequence[str], skips: Callable,
                       orders: Callable,
                       on_hand: Sequence[tuple[str, str, str | None]] | None = None,
                       sheet_lots: Callable | None = None) -> dict[str, Any]:
    """``sheet_lots(item_of, horizon, exclude, cites, skipped)``: lots read from the
    hand-kept on-hand sheets (``proactive_sheets.sheet_lots``) for the items no declared
    lot source states; None: no sheets are read."""
    injected = on_hand is not None
    sources = [(t, p, v) for t, p, v in (pb.declared_on_hand() if on_hand is None
                                         else on_hand)
               if v is not None and (injected or declares(t, LOT_EXPIRY))]
    if not sources and sheet_lots is None:
        lots = [(t, p) for t, p, v in pb.ON_HAND if v is not None]
        return _blocked(result, [f for t, p in lots for f in (
            *((f"{t}.{p}",) if not declares(t, p) else ()),
            *((f"{t}.{LOT_EXPIRY}",) if not declares(t, LOT_EXPIRY) else ()))])
    skipped = skips()
    counts: Counter = Counter()
    item_root = _item_roots(st, settled)
    horizon = as_of + timedelta(days=LOT_DAYS[-1])
    near: list[tuple] = []   # (day, lot, item cluster, rows, tie)
    declared_items: set[str] = set()
    for object_type, prop, via in sources:
        pb._tick(st)
        held = st.values([object_type], [prop, LOT_EXPIRY, LOT_NUMBER])
        ties = {l["from_object"]: l for l in _touch(st, held, [via], settled)
                if l["from_object"] in held}
        for oid, entry in sorted(held.items()):
            props = entry["props"]
            row = pb._one(props.get(prop))
            qty = pb._dec(row["value"]) if row is not None else None
            if qty is None or qty <= 0:
                continue
            counts["lots_on_hand"] += 1
            if oid in ties:
                declared_items.add(item_root.get(ties[oid]["to_object"],
                                                 ties[oid]["to_object"]))
            expiry, day = _first(props, (LOT_EXPIRY,))
            if day is None:
                skipped.note("expiry_unknown", [oid], [(oid, "[]")])
                continue
            if not as_of <= day <= horizon:
                continue
            tie = ties.get(oid)
            if tie is None:
                skipped.note("item_unknown", [oid], [(oid, "[]")])
                continue
            near.append((day, oid, item_root.get(tie["to_object"], tie["to_object"]),
                         [row, expiry, *(props.get(LOT_NUMBER) or ())[:1]], tie, qty,
                         unit_token(row.get("unit"))))
    # lots of the hand-kept sheets, for items no declared source states a lot of
    sheet_found: list[dict[str, Any]] = []
    if sheet_lots is not None:
        sheet_found = sheet_lots(lambda r: item_root.get(r, r), horizon, declared_items,
                                 cites, skipped)
        counts["lots_on_hand"] += len(sheet_found)
    # the open customer orders naming each lot's item
    roots = {n[2] for n in near} | {f["item"] for f in sheet_found}
    members = _members(item_root, roots)
    wanting: dict[str, dict[str, list]] = defaultdict(dict)
    if near or sheet_found:
        _found, verdicts = orders()
        top_of = {m: v for v in verdicts if v.cls == "open" for m in v.group}
        lines = [l for l in _touch(st, members, [pb.SO_LINE_ITEM], settled)
                 if l["to_object"] in members]
        line_of = {l["from_object"]: l for l in _touch(
            st, {l["from_object"] for l in lines}, [pb.SO_LINE_OF], settled)
            if l["link_type"] == pb.SO_LINE_OF}
        qty_rows = pb._line_rows(st, "SalesOrderLine", ("ordered_quantity",),
                                 [l["from_object"] for l in lines])
        for link in lines:
            tie = line_of.get(link["from_object"])
            verdict = top_of.get(tie["to_object"]) if tie else None
            row = pb._one((qty_rows.get(link["from_object"]) or {}).get("ordered_quantity"))
            if verdict is None or row is None:
                continue
            top = item_root.get(link["to_object"], link["to_object"])
            wanting[top].setdefault(verdict.top, cites(
                [row], link["markings"], tie["markings"], *verdict.link_marks))
    gaps = []
    for day, oid, top, rows, tie, qty, unit in sorted(near):
        pb._tick(st)
        left = (day - as_of).days
        counts["expiring"] += 1
        number = pb._one(rows[2:]) if len(rows) > 2 else None
        name = f"Lot {str(number['value']).strip()}" if number else "A lot"
        users = wanting.get(top, {})
        summary = (f"{name} with {pb._text(qty)} {unit or 'units'} on hand expires "
                   f"{day.isoformat()} (in {_plural(left, 'day')})")
        if users:
            summary += f"; {_plural(len(users), 'open customer order')} could use it"
        made = gap(LOT_EXPIRING, LOT_RULE, oid, sources[0][0], f"expiry|{day.isoformat()}",
                   summary=summary,
                   missing="Which order will use this lot before it expires, or should it "
                           "be retested, reworked or written off?",
                   why=f"The lot has stock on hand and its expiry date is within "
                       f"{LOT_DAYS[-1]} days; open customer orders name its item.",
                   priority="high" if left <= LOT_DAYS[0] else "medium",
                   about=(sources[0][0], LOT_EXPIRY),
                   groups=[cites(rows, tie["markings"]),
                           *list(users.values())[:MAX_CITED]],
                   event={"days": left, "orders_affected": len(users)})
        gaps.append(made)
    for f in sheet_found:
        pb._tick(st)
        left = (f["expiry"] - as_of).days
        counts["expiring"] += 1
        top = f["item"]
        unit = unit_token(f["unit"]) if f["unit"] else None
        name = f"Lot {f['lot']}" if f["lot"] else "A lot"
        users = wanting.get(top, {})
        summary = (f"{name} with {pb._text(f['qty'])} {unit or 'units'} on hand (hand-kept "
                   f"sheet of {f['day'].isoformat()}) expires {f['expiry'].isoformat()} "
                   f"(in {_plural(left, 'day')})")
        if users:
            summary += f"; {_plural(len(users), 'open customer order')} could use it"
        made = gap(LOT_EXPIRING, LOT_RULE, top, ITEM,
                   f"sheet|{f['lot']}|{f['expiry'].isoformat()}",
                   summary=summary,
                   missing="Which order will use this lot before it expires, or should it "
                           "be retested, reworked or written off?",
                   why=f"A hand-kept sheet shows the lot with stock on hand and its expiry "
                       f"date is within {LOT_DAYS[-1]} days; open customer orders name its "
                       "item.",
                   priority="high" if left <= LOT_DAYS[0] else "medium",
                   about=(ITEM, LOT_EXPIRY),
                   groups=[f["cite"], *list(users.values())[:MAX_CITED]],
                   event={"days": left, "orders_affected": len(users),
                          "on_hand_sheet_day": f["day"].isoformat(), "cell": f["cell"]})
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("lots_on_hand", "expiring")}
    return out


# --------------------------------------------------------------------------- assembly

def assembly_component_shortfall(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                                 result: Callable, settled: Sequence[str], skips: Callable,
                                 po_source: Callable, absence: Callable,
                                 on_hand: Sequence[tuple[str, str, str | None]] | None = None,
                                 starts: Sequence[str] | None = None) -> dict[str, Any]:
    sources, missing = _sources(on_hand)
    starts = tuple(declared_starts() if starts is None else starts)
    if not starts:
        missing += [f"{ASSEMBLY}.{p}" for p in START_PROPS]
    if missing:
        return _blocked(result, missing)
    skipped = skips()
    counts: Counter = Counter()
    horizon = as_of + timedelta(days=ASSEMBLY_HORIZON_DAYS)
    found = st.values([ASSEMBLY], ("status", "deleted_at", *starts, *ASSEMBLY_CODES))
    root, leads = _clusters(st, found, ASSEMBLY_TWINS, settled)
    groups: dict[str, list[str]] = defaultdict(list)
    for oid in found:
        groups[root.get(oid, oid)].append(oid)
    from . import po_status
    orders: dict[str, tuple] = {}   # governing record -> (start day, start row, code row)
    keeper = assembly_keeper()
    for top, members in sorted(groups.items()):
        pb._tick(st)
        # done or deleted in any copy: a lagging copy never reopens it
        if any(found[m]["props"].get("deleted_at") or any(
                (pb._word(r) or "").replace(" ", "_") in DONE_ASSEMBLY
                for r in found[m]["props"].get("status", ())) for m in members):
            continue
        kept = [m for m in members
                if keeper is None or (po_status.binding_of(m) or "").startswith(keeper)]
        if not kept:
            skipped.note("status_source_absent", [min(members)], [(min(members), "[]")])
            continue
        record = min(kept)
        props = found[record]["props"]
        word = (pb._word(pb._one(props.get("status"))) or "").replace(" ", "_")
        if word not in OPEN_ASSEMBLY:
            skipped.note("status_unknown", [record], [(record, "[]")])
            continue
        counts["open"] += 1
        row, day = _first(props, starts)
        if day is None:
            skipped.note("start_date_unknown", [record], [(record, "[]")])
            continue
        if day > horizon:
            continue
        orders[record] = (day, row, _code(found, [record], ASSEMBLY_CODES))
    parts = [l for l in _touch(st, orders, [COMPONENT_OF], settled)
             if l["link_type"] == COMPONENT_OF and l["to_object"] in orders]
    uses = {l["from_object"]: l for l in _touch(st, {l["from_object"] for l in parts},
                                                [COMPONENT_ITEM], settled)
            if l["link_type"] == COMPONENT_ITEM}
    comp = pb._line_rows(st, COMPONENT, ("planned_quantity", "consumed_quantity"),
                         [l["from_object"] for l in parts])
    item_root = _item_roots(st, settled)
    need: dict[str, list[tuple]] = defaultdict(list)   # item -> (day, order, qty, unit, group)
    for part in parts:
        use = uses.get(part["from_object"])
        if use is None:
            skipped.note("component_item_unknown", [part["to_object"]],
                         [(part["to_object"], "[]")])
            continue
        got = pb._outstanding(comp.get(part["from_object"], {}), "planned_quantity",
                              "consumed_quantity")
        if isinstance(got, str):
            skipped.note(got, [part["to_object"]], [(part["to_object"], "[]")])
            continue
        qty, unit, rows = got
        if qty <= 0:
            continue
        order = part["to_object"]
        need[item_root.get(use["to_object"], use["to_object"])].append(
            (orders[order][0], order, qty, unit,
             cites(rows, part["markings"], use["markings"])))
    stock, broken = _stock(st, sources, item_root, settled, cites, roots=need)
    # the purchase orders are read only when a component is needed (a whole-store read)
    inbound = pb.inbound_flows(st, po_source() if need else None, as_of, set(need),
                               lambda r: item_root.get(r, r), cites, settled)
    items = st.values([ITEM], [CODE], objects=sorted(_members(item_root, need)))
    short: dict[str, list[tuple]] = defaultdict(list)   # order -> (item, words, groups)
    for item, wants in sorted(need.items()):
        pb._tick(st)
        counts["items"] += 1
        subjects = sorted({w[1] for w in wants})
        cause = [(s, "[]") for s in subjects]
        flows, _undated, bad = inbound.get(item, ([], 0, None))
        why = broken.get(item) or bad
        if why or item not in stock:
            skipped.note(why or "on_hand_unknown", subjects, cause)
            continue
        have, unit, have_group = stock[item]
        units = {unit, *(w[3] for w in wants), *(f.unit for f in flows)}
        if len(units) != 1 or None in units:
            skipped.note("unit_mismatch" if None not in units else "unit_not_stated",
                         subjects, cause)
            continue
        code = _code(items, _members(item_root, [item]), (CODE,))
        total = Decimal(0)
        for day, order, qty, _u, group in sorted(wants, key=lambda w: (w[0], w[1])):
            total += qty
            arriving = [f for f in flows if f.day is not None and f.day <= day]
            supply = have + sum((f.qty for f in arriving), Decimal(0))
            if total > supply:
                label = str(code["value"]).strip() if code else "a component"
                short[order].append((
                    label, f"{label} needs {pb._text(total)} {unit} by then, "
                           f"{pb._text(supply)} on hand or due in",
                    [group, have_group, *(g for f in arriving[:MAX_CITED] for g in f.groups)],
                    total - supply))
    requires = absence([*pb.STOCK_ABSENCE_TYPES, ASSEMBLY, COMPONENT,
                        *(t for t, _p, _v in sources)])
    gaps = []
    for order, hits in sorted(short.items()):
        day, start_row, code_row = orders[order]
        counts["short"] += 1
        name = (f"Assembly order {str(code_row['value']).strip()}" if code_row
                else "An assembly order")
        made = gap(ASSEMBLY_SHORT, ASSEMBLY_RULE, order, ASSEMBLY, f"short|{day.isoformat()}",
                   summary=f"{name} starts {day.isoformat()} and "
                           f"{_plural(len(hits), 'component')} will be short: "
                           + "; ".join(h[1] for h in hits[:3])
                           + (f"; and {len(hits) - 3} more" if len(hits) > 3 else ""),
                   missing="Can the components arrive in time (expedite the purchase "
                           "order, use another lot), or should the start move?",
                   why="The order's components need more than is on hand plus what open "
                       "purchase orders bring by its start date, after the orders that "
                       "start before it.",
                   priority="high" if (day - as_of).days <= 14 else "medium",
                   about=(ASSEMBLY, starts[0]),
                   groups=[cites([start_row]), *(g for h in hits for g in h[2])],
                   extra=cites([code_row]) if code_row else (),
                   event={"days": (day - as_of).days, "orders_affected": 1})
        if made is not None:
            made["requires"] = requires
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("open", "items", "short")}
    return out


# --------------------------------------------------------------------------- certificates

def certificate_expiry(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                       result: Callable, settled: Sequence[str], skips: Callable,
                       certificates: Sequence[Certificate] = CERTIFICATES) -> dict[str, Any]:
    served = [(c, tuple(p for p in c.day_props if declares(c.type, p))) for c in certificates]
    unserved = [f"{c.type}.{p}" for c, props in served for p in c.day_props
                if p not in props]
    served = [(c, props) for c, props in served if props]
    if not served:
        return _blocked(result, unserved)
    skipped = skips()
    counts: Counter = Counter()
    horizon = as_of + timedelta(days=CERT_DAYS[-1])
    seen: dict[tuple, str] = {}
    gaps = []
    for cert, props in served:
        labels = [p for p in (cert.supplier, cert.material, cert.lot) if p]
        names = [p for p in (cert.material, cert.lot) if p]   # what the certificate is of
        evidence = ("evidence",) if declares(cert.type, "evidence") else ()
        found = st.values([cert.type], [*props, *labels, *evidence])
        # per supplier, material and lot, the latest date any of its certificates states:
        # an older certificate of the same supplier's lot is superseded by a newer one
        # stating a later date. Only a certificate stating every one of them is matched:
        # a lot string is not proven unique across suppliers, and a blank part is no
        # identity, so an ambiguous newer certificate never erases an expiry (BIZ-015)
        def identity(values: Mapping[str, Any]) -> tuple | None:
            ident = tuple(_fold(_stated(values, p)) for p in labels)
            return ident if names and all(ident) else None
        latest: dict[tuple, date] = {}
        for entry in found.values():
            ident = identity(entry["props"])
            if ident is not None:
                for prop in props:
                    row = pb._one(entry["props"].get(prop) or ())
                    day = pb._day(row["value"]) if row is not None else None
                    day, _doubt = cited_day(entry["props"], prop, day)
                    if day is not None:
                        latest[(prop, *ident)] = max(latest.get((prop, *ident), day), day)
        for oid, entry in sorted(found.items()):
            pb._tick(st)
            values = entry["props"]
            for prop in props:
                rows = values.get(prop) or ()
                row = pb._one(rows)
                day = pb._day(row["value"]) if row is not None else None
                if day is None:
                    if any(str(r.get("value") or "").strip() for r in rows):
                        skipped.note("date_unreadable", [oid], [(oid, "[]")])
                    continue
                day, doubt = cited_day(values, prop, day)
                if day is None:
                    # live 11-12: 2 of 15 expiries were printed 12/03/2026 and 01/11/2027
                    # with nothing on the page to say which part is the month
                    skipped.note(doubt, [oid], [(oid, "[]")])
                    continue
                counts["dated"] += 1
                if not as_of <= day <= horizon:
                    continue
                said = {p: (row_ if _stated(values, p) else None)
                        for p in labels for row_ in [pb._one(values.get(p))]}
                of = tuple(_fold(_stated(values, p)) for p in names)
                if names and not any(of):
                    # neither material nor lot read from the document: no one can tell
                    # what expires (live 11-10: 4 of 15 certificates)
                    skipped.note("certificate_unidentified", [oid], [(oid, "[]")])
                    continue
                ident = identity(values)
                if ident is not None and latest.get((prop, *ident), day) > day:
                    counts["superseded"] += 1
                    continue
                key = (cert.type, prop, day,
                       *(_fold(r["value"]) if r else "" for r in said.values()))
                if key in seen:
                    counts["copies"] += 1
                    continue
                seen[key] = oid
                counts["expiring"] += 1
                left = (day - as_of).days
                text = {p: str(r["value"]).strip() for p, r in said.items()
                        if r and r.get("value") is not None and str(r["value"]).strip()}
                what = cert.label[0].upper() + cert.label[1:]
                if cert.material in text:
                    what += f" for {text[cert.material]}"
                if cert.lot in text:
                    what += f" lot {text[cert.lot]}"
                if cert.supplier in text:
                    what += f" from {text[cert.supplier]}"
                verb = "is due for review" if prop.startswith("review") else "expires"
                made = gap(CERT_EXPIRING, CERT_RULE, oid, cert.type,
                           f"{prop}|{day.isoformat()}",
                           summary=f"{what} {verb} {day.isoformat()} "
                                   f"(in {_plural(left, 'day')}, as read from the document)",
                           missing="Is a renewed certificate on file, or does the supplier "
                                   "need to send one before the material is used or "
                                   "shipped?",
                           why=f"The {cert.label} states this date (machine-read, to be "
                               f"confirmed against the document), within {CERT_DAYS[-1]} "
                               "days.",
                           priority="high" if left <= CERT_DAYS[0] else "medium",
                           about=(cert.type, prop),
                           groups=[cites([row])],
                           extra=cites([r for r in said.values() if r]),
                           event={"days": left})
                gaps.append(made)
    out = result(gaps, not_evaluated=skipped,
                 reasons=[waiting(f) for f in dict.fromkeys(unserved)])
    out["counts"] = {k: counts.get(k, 0) for k in ("dated", "expiring", "copies",
                                                   "superseded")}
    return out


# a numeric date as printed: 12/03/2026, 01.11.27, 3-12-2026
NUMERIC_DAY = re.compile(r"^\s*(\d{1,2})([/.-])(\d{1,2})\2(\d{4}|\d{2})\s*$")


def _numeric(text: Any) -> tuple[int, str, int, int] | None:
    m = NUMERIC_DAY.match(str(text or ""))
    if m is None:
        return None
    year = int(m.group(4))
    return int(m.group(1)), m.group(2), int(m.group(3)), year + 2000 if year < 100 else year


def _order(first: int, second: int) -> str | None:
    """Which part a printed numeric date leads with: "dm" (day first), "md", or None (both
    could be a month and they differ: the text alone cannot tell)."""
    if first > 12 >= second:
        return "dm"
    if second > 12 >= first:
        return "md"
    return "same" if first == second else None


def cited_day(values: Mapping[str, list], prop: str, stored: date | None
              ) -> tuple[date | None, str | None]:
    """The day a document-read date stands for, from the text it was read from
    (``evidence.fields.<prop>.citations``): a worded or ISO date, or a numeric one with a
    part over 12, is the stored day; a numeric one whose two parts could each be the month
    (12/03/2026) takes the order another numeric date of the same document forces with the
    same separator (19/11/2024 beside it: day first), else it is no day
    (``date_order_ambiguous``). A record with no cited text (a structured source) keeps its
    stored day."""
    row = pb._one(values.get("evidence"))
    try:
        fields = json.loads(row["value"]).get("fields") if row is not None else None
    except (TypeError, ValueError, AttributeError):
        fields = None
    if not isinstance(fields, Mapping) or stored is None:
        return stored, None
    field = fields.get(prop)
    texts = [c.get("text") for c in (field or {}).get("citations") or ()
             if isinstance(c, Mapping)] if isinstance(field, Mapping) else []
    printed = [_numeric(t) for t in texts]
    if not texts or any(p is None for p in printed):
        return stored, None   # worded or ISO somewhere: the reading is not in doubt
    if any(_order(a, b) for a, _sep, b, _y in printed):
        return stored, None
    separators = {sep for _a, sep, _b, _y in printed}
    forced = set()
    for name, other in fields.items():
        if name == prop or not isinstance(other, Mapping):
            continue
        for c in other.get("citations") or ():
            got = _numeric(c.get("text")) if isinstance(c, Mapping) else None
            if got is not None and got[1] in separators:
                order = _order(got[0], got[2])
                if order in ("dm", "md"):
                    forced.add(order)
    if len(forced) != 1 or len(separators) != 1:
        return None, "date_order_ambiguous"
    first, _sep, second, year = printed[0]
    day, month = (first, second) if forced == {"dm"} else (second, first)
    try:
        read = date(year, month, day)
    except ValueError:
        return None, "date_order_ambiguous"
    if any((d, m, y) != (day, month, year) for a, _s, b, y in printed
           for d, m in [((a, b) if forced == {"dm"} else (b, a))]):
        return None, "date_order_ambiguous"
    if read != stored:
        # the document's own order says the other reading than the one stored: the stored
        # day is no statement of it, and no alert cites a row saying another day
        return None, "date_order_misread"
    return read, None


NOT_STATED = frozenset({"", "none", "null", "n/a"})


def _stated(props: Mapping[str, list], name: str) -> str:
    """The text a record states for ``name``; "" when it states none or a reader wrote
    its own empty ("None", "null", "n/a": a field the document did not hold)."""
    row = pb._one(props.get(name))
    text = str(row["value"]).strip() if row and row.get("value") is not None else ""
    return "" if text.casefold() in NOT_STATED else text


# --------------------------------------------------------------------------- freight

def freight_cost_anomaly(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                         result: Callable, settled: Sequence[str], skips: Callable
                         ) -> dict[str, Any]:
    if not declares(FREIGHT, "amount"):
        return _blocked(result, [f"{FREIGHT}.amount"])
    skipped = skips()
    counts: Counter = Counter()
    found = st.values([FREIGHT], FREIGHT_PROPS)
    root, _leads = _clusters(st, found, FREIGHT_TWINS, settled)
    groups: dict[str, list[str]] = defaultdict(list)
    for oid in found:
        groups[root.get(oid, oid)].append(oid)
    from . import po_status
    bills = {pb._governing(m, po_status.binding_of) for m in groups.values()}
    shipments: dict[str, set] = defaultdict(set)
    for link in _touch(st, bills, [FREIGHT_SHIPMENT], settled):
        if link["from_object"] in bills:
            shipments[link["from_object"]].add(link["to_object"])
    money = pb._currencies(st, [r for oid in bills
                                for r in found[oid]["props"].get("amount", ())])
    floor = as_of - timedelta(days=FREIGHT_TRAILING_DAYS)
    lanes: dict[tuple, list[tuple]] = defaultdict(list)
    seen: set[tuple] = set()
    for oid in sorted(bills):
        pb._tick(st)
        props = found[oid]["props"]
        amount = pb._one(props.get("amount"))
        value = pb._dec(amount["value"]) if amount else None
        if value is None or value <= 0:
            counts["amount_not_stated"] += 1
            continue
        currency = pb._currency(money, amount)
        carrier = pb._one(props.get("carrier"))
        when, day = _first(props, ("document_date", "ship_date"))
        if currency is None or carrier is None or not _fold(carrier["value"]) or day is None:
            skipped.note("currency_unknown" if currency is None else "carrier_unknown"
                         if carrier is None or not _fold(carrier["value"]) else "date_unknown",
                         [oid], [(oid, FINANCE)])
            continue
        number = pb._one(props.get("invoice_number")) or pb._one(props.get("tracking_number"))
        twin = (_fold(carrier["value"]), _fold(number["value"]) if number else oid, value)
        if twin in seen:
            counts["copies"] += 1
            continue
        seen.add(twin)
        if day < floor or day > as_of:
            continue
        ends = [pb._one(props.get(p)) for p in ("port_of_loading", "port_of_discharge")]
        lane = (tuple(_fold(r["value"]) for r in ends) if all(r and _fold(r["value"])
                                                               for r in ends) else None)
        per = value / max(1, len(shipments.get(oid, ())))
        rows = [amount, carrier, when, *(r for r in ends if r), *((number,) if number else ())]
        lanes[(twin[0], lane, currency)].append((day, oid, per, rows, ends))
        counts["evaluated"] += 1
    gaps = []
    for (carrier_key, lane, currency), billed in sorted(lanes.items(), key=lambda kv: str(kv[0])):
        pb._tick(st)
        if len(billed) < FREIGHT_MIN_HISTORY + 1:
            counts["thin_history"] += len(billed)
            continue
        for day, oid, per, rows, ends in billed:
            if (as_of - day).days > FREIGHT_RECENT_DAYS:
                continue
            others = sorted(b[2] for b in billed if b[1] != oid)
            norm = _median(others)
            if norm <= 0 or per < norm * FREIGHT_RATIO:
                continue
            counts["anomalies"] += 1
            ratio = (per / norm).quantize(Decimal("0.1"))
            carrier = str(rows[1]["value"]).strip()
            lane_words = (f" from {str(ends[0]['value']).strip()} to "
                          f"{str(ends[1]['value']).strip()}" if lane else "")
            nearest = sorted((b for b in billed if b[1] != oid),
                             key=lambda b: (-b[0].toordinal(), b[1]))[:MAX_CITED]
            made = gap(FREIGHT_HIGH, FREIGHT_RULE, oid, FREIGHT, f"freight|{day.isoformat()}",
                       summary=f"Freight with {carrier}{lane_words} cost "
                               f"{_money(per)} {currency} per "
                               f"shipment on {day.isoformat()}, {pb._text(ratio)} times the "
                               f"usual {_money(norm)} {currency} "
                               f"over {_plural(len(others), 'other bill')}",
                       missing="Was the bill right (extra charges, a rush, a wrong rate), or "
                               "should the carrier be asked to correct it?",
                       why="The bill's amount per shipment is well above the median of the "
                           "same carrier's other bills on the lane in the same currency "
                           f"over {FREIGHT_TRAILING_DAYS} days (each one cited).",
                       priority="high" if ratio >= FREIGHT_HIGH_RATIO else "medium",
                       about=(FREIGHT, "amount"),
                       groups=[cites(rows, FINANCE),
                               *(cites(b[3][:1], FINANCE) for b in nearest)],
                       event={"days": (as_of - day).days,
                              "amount": {"value": _money(per),
                                         "currency": currency, "basis": "amount",
                                         "handle": rows[0]["l1_handle"],
                                         "currency_handle": rows[0]["l1_handle"],
                                         "markings": cites(rows[:1], FINANCE)[0][2]}})
            gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("evaluated", "amount_not_stated", "copies",
                                                    "thin_history", "anomalies")}
    return out


# --------------------------------------------------------------------------- reorder soon

def reorder_soon(st: Any, as_of: date, *, gap: Callable, cites: Callable, result: Callable,
                 settled: Sequence[str], skips: Callable, po_source: Callable,
                 absence: Callable,
                 on_hand: Sequence[tuple[str, str, str | None]] | None = None,
                 sheet_on_hand: Callable | None = None) -> dict[str, Any]:
    """``sheet_on_hand(item_of, items, cites, skipped)``: as for ``stock_out_risk``; a sheet
    reading is on hand only for an item no declared source serves, in its own unit."""
    from . import demand_forecast as df

    sources, missing = _sources(on_hand)
    if not sources and sheet_on_hand is None:
        return _blocked(result, missing)
    skipped = skips()
    counts: Counter = Counter()
    book = df.history(st, as_of, settled)
    bucket = REORDER_BUCKET
    through = date.fromisoformat(book["through"]) if book.get("through") else None
    series: dict[tuple, dict[int, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    latest: dict[tuple, list] = defaultdict(list)
    for line in book["lines"]:
        key = (line.item, line.unit)
        series[key][df.period(line.day, bucket)] += line.qty
        latest[key].append(line)
    units_of: dict[str, set[str]] = defaultdict(set)
    for item, unit in series:
        units_of[item].add(unit)
    state = df.reorder_state(st, as_of, set(units_of), settled, on_hand=sources,
                             po_source=po_source, sheet_on_hand=sheet_on_hand,
                             skipped=skipped)
    end = df.horizon_end(as_of, bucket)
    last = df.last_complete(through, as_of, bucket) if through else None
    requires = absence(["SalesInvoice", "SalesInvoiceLine", "PurchaseOrder",
                        "PurchaseOrderLine", *(t for t, _p, _v in sources)])
    gaps = []
    for (item, unit), per in sorted(series.items()):
        pb._tick(st)
        cause = [(item, "[]")]
        if len(units_of[item]) > 1:
            skipped.note("unit_mismatch", [item], cause)
            continue
        got = df.forecast_series(per, last, as_of, bucket)
        if isinstance(got, str):
            skipped.note(got, [item], cause)
            continue
        counts["forecast"] += 1
        supply = df.supply_by(state, item, unit, end)
        if isinstance(supply, str):
            skipped.note(supply, [item], cause)
            continue
        counts["items"] += 1
        need = Decimal(got["forecast_total"])
        have = supply["on_hand"] + supply["inbound"]
        if need <= have:
            continue
        counts["reorder_soon"] += 1
        short = need - have
        hold = got["_holdout"]
        wape = (df._q(hold["abs_error"] / hold["actual"]) if hold and hold["actual"]
                else None)
        method = got["method"].replace("_", " ")
        orders = supply["orders"]
        history_lines = sorted(latest[(item, unit)], key=lambda l: (l.day, l.records[0]),
                               reverse=True)[:REORDER_HISTORY_CITED]
        history_groups = [[c for c in line.handles] for line in history_lines]
        made = gap(
            REORDER, REORDER_RULE, item, ITEM, "reorder",
            summary=f"Reorder soon: forecast demand of {got['forecast_total']} {unit} over "
                    f"the next {df.HORIZON[bucket]} months ({method}"
                    + (f", holdout error {int(Decimal(wape) * 100)}%" if wape else "")
                    + f") exceeds on hand {pb._text(supply['on_hand'])} {unit} plus "
                    f"{pb._text(supply['inbound'])} {unit} due in on {_plural(len(orders), 'open purchase order')} "
                    f"by {end.isoformat()}, {pb._text(short)} {unit} short",
            missing="Should more be bought now, and how much, before the forecast demand "
                    "uses up what is on hand and on order?",
            why="Invoiced demand of the last 3 years, projected over the next 3 months, "
                "needs more of the item than is on hand plus what open purchase orders "
                "bring in by then (every quantity cited, one unit).",
            priority="high" if Decimal(got["low_80_total"]) > have else "medium",
            about=(ITEM, "on_hand"),
            groups=[*supply["groups"], *history_groups],
            event={"forecast": got["forecast_total"], "supply": pb._text(have),
                   "shortfall": pb._text(short), "unit": unit, "short_by": end.isoformat(),
                   "method": got["method"], "inbound": orders,
                   **({"holdout_wape": wape} if wape else {}),
                   **({"on_hand_sheet_day": state["sheet_days"][item]}
                      if item in state.get("sheet_days", {}) else {})})
        if made is not None:
            made["requires"] = requires
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {"series": len(series), "forecast": counts["forecast"],
                     "items": counts["items"], "reorder_soon": counts["reorder_soon"]}
    return out


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
