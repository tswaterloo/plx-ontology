"""Who owns an event and the per-intent handlers that draft its item
(``operating.autopilot``, which re-exports them)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from .autopilot_records import _addresses, _decimal, _instant, _internal, _linked, _read, _v
from .autopilot_queue import (
    CLOSED,
    HandlerTimeout,
    INVOICE_NUMBER,
    MAX_LINES,
    MAX_RECORDS,
    ORDER_NUMBER,
    PO_LINE_ITEM,
    PO_LINE_OF,
    PO_NUMBER,
    ROLES_ENV,
    ROLES_FILE,
    SO_LINE_ITEM,
    SO_LINE_OF,
    SUMMARY_CHARS,
)
from .autopilot_events import _parties


# ----------------------------------------------------------------------------- owners

def _roles(environ: Mapping[str, str], queue_dir: Path) -> dict[str, list[str]]:
    path = Path(environ.get(ROLES_ENV) or queue_dir / ROLES_FILE)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    roles = raw.get("roles") if isinstance(raw, Mapping) else None
    if not isinstance(roles, Mapping):
        return {}
    oid = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    return {str(k): [str(x).lower() for x in v if oid.match(str(x).lower())]
            for k, v in roles.items() if isinstance(v, list)}


DECIDER_DAYS = 30


def mc_project(audience: str) -> str:
    """An MC record's project: its audience unit's last segment
    (``mc:<scope>/approvals/<project>``); none: ""."""
    head, _, rest = str(audience or "").partition(":")
    return rest.rsplit("/", 1)[-1] if head == "mc" and "/" in rest else ""


def deciders(st: Any, directory: Mapping[str, str], now: datetime, *,
             days: int = DECIDER_DAYS, project: str | None = None) -> set[str]:
    """The people (directory ids) who decided an approval in the ``days`` before ``now``;
    ``project``: in that MC project only. A decider the directory does not know is no one
    (never a guess). The records are read once per store connection."""
    cache = st.__dict__.setdefault("_autopilot_decisions", None)
    if cache is None:
        rows = st.conn.execute(
            "SELECT object_id, audience_key FROM objects WHERE object_type = 'MCApproval'"
        ).fetchall()
        units = {r[0]: r[1] for r in rows}
        cache = []
        for oid, r in _read(st, list(units), ("decided_by", "decided_at")).items():
            at, by = _instant(_v(r, "decided_at")), _v(r, "decided_by").lower()
            if at is not None and by in directory:
                cache.append((at, directory[by], mc_project(units.get(oid, ""))))
        st.__dict__["_autopilot_decisions"] = cache
    since = now - timedelta(days=days)
    return {who for at, who, proj in cache
            if since <= at < now and (project is None or proj == project)}


# ----------------------------------------------------------------------------- handlers

class Ctx:
    """What a handler reads with: the store, the run's day, the deadline; every record it
    reads is noted (``objects``) so the item carries their audiences."""

    def __init__(self, st: Any, as_of: date, deadline: float, domains: Sequence[str]) -> None:
        self.st, self.as_of, self.deadline, self.domains = st, as_of, deadline, domains
        self.objects: set[str] = set()
        self.handles: list[str] = []
        self.capture: Any = getattr(st, "autopilot_capture", None)
        self._carried_pairs: dict[tuple[str, str], bool] = {}   # ``_carried``

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise HandlerTimeout()

    def read(self, ids: Iterable[str], props: Sequence[str]) -> dict[str, dict[str, Any]]:
        self.check()
        ids = [i for i in ids if i]
        found = _read(self.st, ids, props)
        self.objects.update(found)
        return found

    def linked(self, oid: str, types: Sequence[str] | None = None) -> list[str]:
        self.check()
        return [o for _t, o in _linked(self.st, oid, types)]

    def linked_many(self, oids: Iterable[str], types: Sequence[str] | None = None) -> list[str]:
        """The other ends of the current links at any of ``oids``."""
        self.check()
        oids = sorted(set(oids))
        if not oids:
            return []
        out = []
        for row in self.st.touching(oids, types):
            a, b = row[3], row[4]
            out.append(b if a in oids else a)
        return list(dict.fromkeys(out))

    def touching(self, oids: Iterable[str], types: Sequence[str] | None = None, *,
                 tiers: Sequence[str] | None = None) -> list[tuple[str, str, str]]:
        """(link type, from, to) of the current links at any of ``oids``; ``tiers``: only
        links at one of these stored tiers (None: any current, uncontradicted one)."""
        self.check()
        oids = sorted(set(oids))
        return [(row[2], row[3], row[4]) for row in self.st.touching(oids, types)
                if tiers is None or row[5] in tiers] if oids else []

    def mc_decisions(self) -> list[tuple[str, datetime, str, str]]:
        """The store's decided MC gates (``mc_digest._decisions``)."""
        from . import mc_digest as md
        return md._decisions(self.st)

    def cite(self, row: Mapping[str, Any] | None, *props: str) -> None:
        for p in props or tuple((row or {}).keys()):
            h = ((row or {}).get(p) or {}).get("l1_handle")
            if h and h not in self.handles:
                self.handles.append(h)

    def find(self, prefix: str, prop: str, value: str) -> list[str]:
        """The records of ``prefix`` whose ``prop`` is ``value`` (letters and digits alike,
        case aside). Read from the identifier table (its index on ``normalized``, the lookup
        Ask's number resolution reads) when it carries ``prop`` of ``prefix`` records, either
        on the records themselves or on a record a ``prefix.*`` link ties them to
        (``IDENTIFIED_VIA``): there "none" is an answer. Otherwise a SQLite store's value
        word index. A lookup that cannot answer raises ``FindUnavailable``, never ``[]``:
        "not found" lets a caller act as if the record did not exist (a second order
        draft for an order already keyed). On Postgres the word index read is not tried:
        a number there reads for minutes, past the handler's deadline (11-17 live)."""
        self.check()
        norm = re.sub(r"[^0-9a-z]", "", value.lower())
        if not norm:
            return []
        try:
            hits = self._identified_as(prefix, prop, value)
        except (FindUnavailable, HandlerTimeout):
            raise
        except Exception as exc:  # noqa: BLE001 - no identifier table: cannot say
            if "interrupt" in str(exc):
                raise
            raise FindUnavailable(f"identifiers: {type(exc).__name__}") from exc
        if hits is None:
            if getattr(self.st, "pg", False):         # ``proactive_engine._Store.pg``
                raise FindUnavailable(f"{prefix}.{prop} is not in the identifier table")
            from . import proactive_threads as pt
            words = [w for w in re.split(r"[^0-9A-Za-z]+", value) if w]
            hits = pt._word_ids(self.st, " AND ".join('"' + w + '"' for w in words), [prop],
                                prefix + ":%")
            if hits is None:
                raise FindUnavailable("no value word index")
        self.check()
        rows = _read(self.st, sorted(hits), [prop])
        return sorted(o for o, r in rows.items()
                      if re.sub(r"[^0-9a-z]", "", _v(r, prop).lower()) == norm)

    def _identified_as(self, prefix: str, prop: str, value: str) -> set[str] | None:
        """The records ``find`` reads ``prop`` of from the identifier table; None when the
        table does not carry ``prop`` of ``prefix`` (``_carried``)."""
        via = IDENTIFIED_VIA.get((prefix, prop))
        if not (self._carried(*via[:2]) if via else self._carried(prefix, prop)):
            return None
        stripped = value.strip()
        bare = re.sub(r"[^0-9A-Za-z]", "", stripped)
        forms = sorted({f for v in (stripped, bare) for f in (v, v.upper(), v.lower()) if f})
        rows = self.st.conn.execute(
            "SELECT object_id, property FROM identifiers WHERE normalized IN "
            "(SELECT value FROM json_each(?)) AND superseded_at IS NULL",
            (json.dumps(forms),)).fetchall()
        own = prefix + ":"
        if via is None:
            return {oid for oid, p in rows if oid.startswith(own) and p == prop}
        carrier, _carrier_prop, link = via
        others = sorted({oid for oid, _p in rows if oid.startswith(carrier + ":")})
        if len(others) > FIND_FOLLOW:
            raise FindUnavailable(f"{len(others)} {carrier} records carry the value")
        self.check()
        return {o for row in (self.st.touching(others, (link,)) if others else ())
                for o in (row[3], row[4]) if o.startswith(own)}

    def _carried(self, prefix: str, prop: str) -> bool:
        """Whether the identifier table holds ``prop`` of any ``prefix`` record (read once
        per handler run and pair: the object index's range, milliseconds)."""
        memo = self._carried_pairs
        if (prefix, prop) not in memo:
            memo[(prefix, prop)] = self.st.conn.execute(
                "SELECT 1 FROM identifiers WHERE object_id >= ? AND object_id < ? AND "
                "property = ? LIMIT 1", (prefix + ":", prefix + ";", prop)).fetchone() is not None
        return memo[(prefix, prop)]


class FindUnavailable(Exception):
    """``Ctx.find`` could not look the value up (the handler holds, never reads "none")."""


# the most carrier records of one value ``Ctx.find`` follows links of (more: it cannot say);
# a short customer PO many customers use is carried by 678 records (live 11-17: 0.2 s)
FIND_FOLLOW = 2000
# a record property the identifier table carries on another record, tied by a link: (type,
# property) -> (the carrier's type, its property, the link). A sales order's customer PO is
# identified on its CustomerPurchaseOrder (``identified`` reads it the same way; 200 of 200
# recent orders' customer POs found so, live 11-17).
IDENTIFIED_VIA: dict[tuple[str, str], tuple[str, str, str]] = {
    ("SalesOrder", "customer_po"): ("CustomerPurchaseOrder", "po_number",
                                    "SalesOrder.references_customer_po")}


def _held(st: Any, ids: Iterable[str]) -> set[str]:
    """The ids of these objects ``st`` holds."""
    ids = sorted(set(ids))
    out: set[str] = set()
    for start in range(0, len(ids), 500):
        out.update(r[0] for r in st.conn.execute(
            "SELECT object_id FROM objects WHERE object_id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids[start:start + 500]),)))
    return out


class OverlayCtx(Ctx):
    """A handler's reads with ``over``'s records first and the served store's for the rest:
    the fast feed's scratch store holds a just-captured MC gate, its task and the task's
    other gates (``autopilot_fast``), which the served store does not hold yet (or holds
    older); what the handler reads is what ``follow`` reads once the delta holds them. A
    record ``over`` holds is read from it alone; links are both stores' current ones."""

    def __init__(self, st: Any, over: Any, as_of: date, deadline: float,
                 domains: Sequence[str]) -> None:
        super().__init__(st, as_of, deadline, domains)
        self.over = over

    def read(self, ids: Iterable[str], props: Sequence[str]) -> dict[str, dict[str, Any]]:
        self.check()
        ids = sorted({i for i in ids if i})
        mine = _held(self.over, ids)
        found = {**_read(self.st, [i for i in ids if i not in mine], props),
                 **_read(self.over, sorted(mine), props)}
        found = {o: found[o] for o in sorted(found)}
        self.objects.update(found)
        return found

    def _touching(self, oids: list[str], types: Sequence[str] | None) -> list[tuple]:
        seen: dict[tuple, tuple] = {}
        for r in [*self.over.touching(oids, types), *self.st.touching(oids, types)]:
            seen.setdefault((r[2], r[3], r[4]), r)
        return list(seen.values())

    def linked(self, oid: str, types: Sequence[str] | None = None) -> list[str]:
        self.check()
        return list(dict.fromkeys(r[4] if r[3] == oid else r[3]
                                  for r in self._touching([oid], types)))

    def linked_many(self, oids: Iterable[str], types: Sequence[str] | None = None) -> list[str]:
        self.check()
        oids = sorted(set(oids))
        if not oids:
            return []
        return list(dict.fromkeys(r[4] if r[3] in oids else r[3]
                                  for r in self._touching(oids, types)))

    def touching(self, oids: Iterable[str], types: Sequence[str] | None = None, *,
                 tiers: Sequence[str] | None = None) -> list[tuple[str, str, str]]:
        self.check()
        oids = sorted(set(oids))
        return [(r[2], r[3], r[4]) for r in self._touching(oids, types)
                if tiers is None or r[5] in tiers] if oids else []

    def mc_decisions(self) -> list[tuple[str, datetime, str, str]]:
        """Both stores' decided MC gates, a gate ``over`` holds as it holds it."""
        from . import mc_digest as md
        mine = md._decisions_of(self.over)
        return [*(d for o, d in md._decisions_of(self.st).items() if o not in mine),
                *mine.values()]


TOKEN = re.compile(r"(?<![\w-])([A-Za-z]{0,6}-?[A-Za-z0-9]*\d[A-Za-z0-9-]*)(?![\w-])")
ID_KINDS_SKIPPED = ("item_code",)          # an item code names a product, not a transaction
ID_TYPES = ("SalesOrder", "PurchaseOrder", "SalesInvoice", "Shipment", "Receipt", "Batch",
            "CustomerPurchaseOrder")
MAX_PER_TOKEN = 3


def identified(ctx: "Ctx", *texts: str) -> list[str]:
    """The transaction records the texts name by an identifier the store holds (the one
    identifier table: customer PO, order, PO, invoice, shipment, lot numbers), a customer
    PO resolved to its orders; a token naming more than ``MAX_PER_TOKEN`` records is
    ambiguous and left out (conservative identity)."""
    return list(dict.fromkeys(o for _t, o in identified_tokens(ctx, *texts)))[:MAX_RECORDS]


def identified_tokens(ctx: "Ctx", *texts: str) -> list[tuple[str, str]]:
    """``identified`` with the token each record was matched by: (token as written, record),
    in token order."""
    tokens = []
    for t in texts:
        for m in TOKEN.finditer(t or ""):
            tok = m.group(1).strip("-")
            if 4 <= len(tok) <= 25 and sum(c.isdigit() for c in tok) >= 3 and \
                    not re.fullmatch(r"(?:19|20)\d{2}(?:-\d{1,2}){0,2}", tok):
                tokens.append(tok)
    tokens = list(dict.fromkeys(tokens))[:20]
    if not tokens:
        return []
    ctx.check()
    wanted = sorted({x for t in tokens for x in (t, t.upper(), t.lower())})
    rows = ctx.st.conn.execute(
        "SELECT normalized, identifier_kind, object_id FROM identifiers WHERE normalized IN "
        "(SELECT value FROM json_each(?))", (json.dumps(wanted),)).fetchall()
    by_token: dict[str, set[str]] = defaultdict(set)
    for norm, kind, oid in rows:
        if kind not in ID_KINDS_SKIPPED and oid.split(":", 1)[0] in ID_TYPES:
            by_token[norm.upper()].add(oid)
    out: list[tuple[str, str]] = []
    for tok in tokens:
        found = by_token.get(tok.upper(), set())
        orders = set()
        for oid in sorted(found):
            if oid.startswith("CustomerPurchaseOrder:"):
                orders.update(o for o in ctx.linked(oid, ("SalesOrder.references_customer_po",))
                              if o.startswith("SalesOrder:"))
            else:
                orders.add(oid)
        if 0 < len(orders) <= MAX_PER_TOKEN:
            out.extend((tok, o) for o in sorted(orders))
    return out


def _numbers(rx: re.Pattern[str], *texts: str) -> list[str]:
    out: list[str] = []
    for t in texts:
        for m in rx.finditer(t or ""):
            if m.group(1) not in out and any(c.isdigit() for c in m.group(1)):
                out.append(m.group(1))
    return out[:5]


def _greeting(sender: str) -> str:
    local = sender.split("@", 1)[0].split(".")[0]
    return f"Hi {local.capitalize()}," if local.isalpha() and len(local) > 1 else "Hello,"


def _reply(event: Mapping[str, Any], lines: Sequence[str]) -> dict[str, Any]:
    subject = event.get("subject") or ""
    if subject and not re.match(r"^\s*re\s*:", subject, re.I):
        subject = "RE: " + subject
    body = "\n".join([_greeting(event.get("sender") or ""), "", *lines, "",
                      "Best regards,"])
    sender = str(event.get("sender") or "")
    others = event.get("reply_all") or {}
    more = [a for a in others.get("to") or () if isinstance(a, str) and "@" in a
            and a.lower() != sender.lower()]
    copied = [a for a in others.get("cc") or () if isinstance(a, str) and "@" in a
              and a.lower() != sender.lower() and a not in more]
    return {"channel": event.get("channel") or "email",
            "to": [{"address": a} for a in [event.get("sender"), *more]],
            **({"cc": [{"address": a} for a in copied]} if copied else {}),
            "subject": subject[:200], "body": body, "in_reply_to": event.get("object_id")}


def _order_facts(ctx: Ctx, order: str) -> dict[str, Any]:
    row = ctx.read([order], ("order_number", "customer_po", "status", "required_date",
                             "order_date")).get(order) or {}
    ctx.cite(row, "order_number", "status")
    facts: dict[str, Any] = {"object_id": order, "order_number": _v(row, "order_number"),
                             "customer_po": _v(row, "customer_po"), "status": _v(row, "status"),
                             "required_date": _v(row, "required_date")[:10]}
    ships = ctx.linked(order, ("Shipment.ships",))[:5]
    srows = ctx.read(ships, ("shipment_number", "ship_date", "carrier", "tracking_number",
                             "status"))
    facts["shipments"] = [{"object_id": s, "number": _v(r, "shipment_number"),
                           "ship_date": _v(r, "ship_date")[:10], "carrier": _v(r, "carrier"),
                           "tracking": _v(r, "tracking_number")} for s, r in sorted(srows.items())]
    for r in srows.values():
        ctx.cite(r, "ship_date", "tracking_number")
    invs = ctx.linked(order, ("SalesInvoice.bills",))[:5]
    irows = ctx.read(invs, ("invoice_number", "bc_number", "invoice_date"))
    facts["invoices"] = [{"object_id": i, "number": _v(r, "invoice_number") or _v(r, "bc_number"),
                          "date": _v(r, "invoice_date")[:10]} for i, r in sorted(irows.items())]
    return facts


def _qty_unit(row: Mapping[str, Any] | None, *props: str) -> str | None:
    """The folded unit the first stated of ``props`` is in (its cell's unit, else the
    row's ``uom`` text); None when no unit is stated."""
    from ontology.units import unit_token
    for prop in props:
        cell = (row or {}).get(prop)
        if isinstance(cell, Mapping) and cell.get("value") not in (None, ""):
            unit = unit_token(cell.get("unit")) if cell.get("unit") else None
            if unit:
                return unit
    return unit_token(_v(row, "uom")) if _v(row, "uom") else None


def supply_flows(ctx: Ctx, po_lines: Sequence[str], unit: str | None
                 ) -> tuple[list[tuple[date, Decimal, str]], str | None]:
    """The open purchase order supply of these PO lines: (day, quantity due, line), every
    line read with its order's header (a closed or cancelled order supplies nothing,
    whatever date the line holds), in ``unit`` only. A line in another unit or none
    makes the whole supply unknown (reason ``unit_mismatch``/``unit_unknown``): units are
    never converted."""
    prow = ctx.read(po_lines, ("ordered_quantity", "received_quantity",
                               "bc_expected_receipt_date", "uom"))
    # every line's order header, read once for all lines
    order_of: dict[str, str] = {}
    for _t, a, b in ctx.touching(sorted(prow), (PO_LINE_OF,)) if prow else ():
        order_of.setdefault(a, b)
    heads = ctx.read(sorted(set(order_of.values())), ("expected_date", "status"))
    flows = []
    for pl in sorted(prow):
        p = prow[pl]
        due = (_decimal(_v(p, "ordered_quantity")) or Decimal(0)) - \
            (_decimal(_v(p, "received_quantity")) or Decimal(0))
        head = heads.get(order_of.get(pl, ""))
        if head and CLOSED.search(_v(head, "status")):
            continue                       # a closed or cancelled order supplies nothing
        if due <= 0:
            continue
        at = _instant(_v(p, "bc_expected_receipt_date")) or \
            _instant(_v(head, "expected_date"))
        if at is None or at.date() < ctx.as_of - timedelta(days=30):
            continue
        line_unit = _qty_unit(p, "ordered_quantity", "received_quantity")
        if line_unit is None or unit is None:
            return [], "unit_unknown"
        if line_unit != unit:
            return [], "unit_mismatch"
        flows.append((max(at.date(), ctx.as_of), due, pl))
        ctx.cite(p, "bc_expected_receipt_date", "ordered_quantity")
    return sorted(flows), None


def availability(ctx: Ctx, order: str) -> dict[str, Any]:
    """Available-to-promise date for an order's open lines: on hand first, then open
    purchase order lines by their expected date, each consumed once across the order's
    lines (two lines of one item draw on one running allocation: 6 + 6 under 10 on hand is
    not both today). Quantities compare only in one stated unit (the order line's, the
    stock's, each PO line's; never converted). Another order's demand is not netted (stated
    in ``basis``). Unknown (``date`` None) when any line's item, quantity, unit or supply
    date is unstated, or the order has more lines than one check reads
    (``lines_not_read``)."""
    found = ctx.linked(order, (SO_LINE_OF,))
    lines = found[:MAX_LINES]
    rows = ctx.read(lines, ("ordered_quantity", "shipped_quantity", "outstanding_quantity",
                            "uom", "description"))
    dates: list[date] = []
    out_lines, unknown = [], []
    reasons: dict[str, int] = {}
    demand: dict[str, list[tuple[str, Decimal, str | None, Mapping[str, Any]]]] = {}
    for line in sorted(rows):
        r = rows[line]
        need = _decimal(_v(r, "outstanding_quantity"))
        if need is None:
            ordered, shipped = _decimal(_v(r, "ordered_quantity")), _decimal(
                _v(r, "shipped_quantity")) or Decimal(0)
            need = None if ordered is None else ordered - shipped
        if need is None:
            unknown.append(line)
            reasons["quantity_unknown"] = reasons.get("quantity_unknown", 0) + 1
            continue
        if need <= 0:
            continue
        items = ctx.linked(line, (SO_LINE_ITEM,))
        if len(items) != 1:
            unknown.append(line)
            reasons["item_unknown"] = reasons.get("item_unknown", 0) + 1
            continue
        demand.setdefault(items[0], []).append(
            (line, need, _qty_unit(r, "outstanding_quantity", "ordered_quantity",
                                   "shipped_quantity"), r))
    for item in sorted(demand):
        irow = ctx.read([item], ("quantity_on_hand", "code", "name")).get(item) or {}
        on_hand = _decimal(_v(irow, "quantity_on_hand")) or Decimal(0)
        stock_unit = _qty_unit(irow, "quantity_on_hand") if _v(irow, "quantity_on_hand") \
            else None
        ctx.cite(irow, "quantity_on_hand")
        units = {u for _l, _n, u, _r in demand[item]}
        unit = next(iter(units)) if len(units) == 1 else None
        why = None
        if unit is None:
            why = "unit_unknown" if None in units or not units else "unit_mismatch"
        elif on_hand and stock_unit != unit:
            why = "unit_unknown" if stock_unit is None else "unit_mismatch"
        # the item's supply, one running ledger: on hand today, then each open PO line by
        # its date; each line of the order draws on what the lines before it left
        ledger: list[tuple[date, Decimal]] = [(ctx.as_of, on_hand)] if on_hand > 0 else []
        drawn = Decimal(0)
        flows_read = False
        for line, need, _u, r in demand[item]:
            when: date | None = None
            if why is None:
                drawn += need
                covered = sum((q for _d, q in ledger), Decimal(0))
                if covered < drawn and not flows_read:
                    flows_read = True
                    po_lines = ctx.linked(item, (PO_LINE_ITEM,))[:MAX_LINES]
                    flows, why = supply_flows(ctx, po_lines, unit)
                    ledger += [(at, due) for at, due, _pl in flows]
                if why is None:
                    running = Decimal(0)
                    for at, qty in ledger:
                        running += qty
                        if running >= drawn:
                            when = at
                            break
            out_lines.append({"line": line, "item": _v(irow, "code") or _v(irow, "name"),
                              "need": str(need), "uom": _v(r, "uom"), "on_hand": str(on_hand),
                              "available": when.isoformat() if when else None,
                              **({"unknown": why} if why else {})})
            if when is None:
                unknown.append(line)
                key = why or "supply_short"
                reasons[key] = reasons.get(key, 0) + 1
            else:
                dates.append(when)
    if len(found) > MAX_LINES:
        reasons["lines_not_read"] = len(found) - MAX_LINES
    atp = max(dates).isoformat() if dates and not unknown and len(found) <= MAX_LINES \
        else None
    out = {"date": atp, "lines": out_lines, "unknown_lines": len(unknown),
           "basis": "on hand, then open purchase order lines by expected date, each "
                    "consumed once across this order's lines; other orders' demand is not "
                    "netted"}
    if reasons:
        out["unknown_reasons"] = reasons
    if len(found) > MAX_LINES:
        out["lines_read"], out["lines_total"] = MAX_LINES, len(found)
    return out


def _pack_link(oid: str) -> dict[str, str]:
    from urllib.parse import quote
    otype = oid.split(":", 1)[0]
    return {"label": "Document pack (COA, packing slip, invoice)",
            "href": f"/pack/{otype}/{quote(oid, safe='')}"}


def h_order_entry(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    pos = _numbers(PO_NUMBER, event.get("subject", ""), event.get("text", ""))
    customer = next(iter(ctx.linked(event["object_id"], ("Email.sent_by_customer",))), None)
    if customer is None:
        named = sorted(o for side, o in _parties(ctx.st, event.get("sender") or "")
                       if side == "customer")
        customer = named[0] if len(named) == 1 else None   # one customer, else unknown
    crow = ctx.read([customer], ("name", "code", "erp_code")).get(customer) if customer else None
    if not pos and re.match(r"^\s*(?:re|aw|sv)\s*:", event.get("subject") or "", re.I):
        return {"skip": "reply_without_po_number"}
    named = identified(ctx, event.get("subject", ""), event.get("text", ""))
    if named and all(o.startswith(("PurchaseOrder:", "Receipt:")) for o in named):
        return {"skip": "names_our_purchase_order"}   # a supplier writing about our PO
    entered = [(pos[0] if pos else "", o) for o in named if o.startswith("SalesOrder:")]
    for po in pos:
        entered += [(po, o) for o in ctx.find("SalesOrder", "customer_po", po)]
    out: dict[str, Any] = {"po_numbers": pos, "customer": _v(crow, "name") or None,
                           "links": [], "gaps": []}
    if entered:
        po, order = entered[0]
        facts = _order_facts(ctx, order)
        atp = availability(ctx, order)
        out.update(summary=f"PO {po} is already entered as order {facts['order_number']} "
                           f"({facts['status'] or 'status not stated'}).",
                   order=facts, availability=atp)
        when = (f"We can ship on or after {atp['date']}." if atp["date"] else
                "We will confirm the ship date shortly.")
        out["draft"] = _reply(event, [
            f"Thank you for your purchase order {po}. It is entered as our order "
            f"{facts['order_number']}.", when])
        out["links"] = [_pack_link(order)]
        out["done_hint"] = "order_entered"
        return out
    out["summary"] = (f"New PO {pos[0] if pos else '(number not found)'}"
                      + (f" from {out['customer']}" if out["customer"] else "")
                      + ": not entered yet; order entry prepared as a dry-run write intent.")
    if crow and (_v(crow, "code") or _v(crow, "erp_code")) and pos:
        ctx.cite(crow, "code", "erp_code")
        out["write"] = {"system": "bc", "operation": "sales_order_create",
                        "payload": {"customerNumber": _v(crow, "code") or _v(crow, "erp_code"),
                                    "externalDocumentNumber": pos[0][:35],
                                    "orderDate": ctx.as_of.isoformat()},
                        "reason": f"Customer PO {pos[0]} received by mail; lines to be "
                                  "entered from the PO document before approval.",
                        "precondition": {"party": _v(crow, "code") or _v(crow, "erp_code")}}
        out["gaps"].append("lines_from_po_document")
    else:
        out["gaps"].append("customer_or_po_number_unknown")
    out["availability"] = {"date": None, "basis": "the order's lines are not entered yet"}
    out["draft"] = _reply(event, [
        f"Thank you for your purchase order{(' ' + pos[0]) if pos else ''}. We have received "
        "it and will send the order confirmation with the ship date shortly."])
    return out


def _records_named(ctx: Ctx, event: Mapping[str, Any]) -> list[str]:
    texts = (event.get("subject", ""), event.get("text", ""))
    found = [o for o in event.get("records") or () if o.startswith(
        ("SalesOrder:", "Shipment:", "SalesInvoice:"))]
    found += [o for o in identified(ctx, *texts) if o.startswith(
        ("SalesOrder:", "Shipment:", "SalesInvoice:"))]
    for po in _numbers(PO_NUMBER, *texts):
        found += ctx.find("SalesOrder", "customer_po", po)
    for no in _numbers(ORDER_NUMBER, *texts):
        found += ctx.find("SalesOrder", "order_number", no)
    for no in _numbers(INVOICE_NUMBER, *texts):
        found += ctx.find("SalesInvoice", "invoice_number", no)
    return list(dict.fromkeys(found))[:MAX_RECORDS]


def _record_customers(ctx: Ctx, oid: str) -> set[str]:
    """The customer records a sales order, invoice or shipment belongs to: an order's
    ``ordered_by``; an invoice's ``billed_to`` and its orders'; a shipment's orders'."""
    orders = [oid] if oid.startswith("SalesOrder:") else []
    custs: set[str] = set()
    if oid.startswith("SalesInvoice:"):
        custs.update(ctx.linked(oid, ("SalesInvoice.billed_to",)))
        orders += ctx.linked(oid, ("SalesInvoice.bills",))
    elif oid.startswith("Shipment:"):
        orders += ctx.linked(oid, ("Shipment.ships",))
    for o in orders:
        if o.startswith("SalesOrder:"):
            custs.update(ctx.linked(o, ("SalesOrder.ordered_by",)))
    return {c for c in custs if c.startswith("Customer:")}


def senders_own(ctx: Ctx, event: Mapping[str, Any], records: Sequence[str]
                ) -> list[str] | None:
    """The records of ``records`` (sales orders, invoices, shipments) a reply to ``event``
    may name: every one, when each reader of the reply is internal (a mail whose sender and
    reply-all are single addresses of our domains; a Teams chat whose every member the
    directory holds); else only those whose every customer carries the number of the
    customer the outside reader resolves to (a PO, order or invoice number is no key across
    customers: another customer's record under the same number is never named to this
    one). ``None`` (hold: draft nothing) when a record matched and an outside reader's
    customer cannot be resolved (a Teams channel, whose members are not captured, or a
    chat member no address of whom is held)."""
    records = list(dict.fromkeys(records))
    if not records:
        return []
    if event.get("channel") == "teams":
        return _chat_readers_own(ctx, event, records)
    others = event.get("reply_all") or {}
    to = [str(event.get("sender") or "")] + [
        a for k in ("to", "cc") for a in others.get(k) or () if isinstance(a, str)]
    if all(_ours(a, ctx.domains) for a in to):
        return records          # every reader is ours: a mail with no outside recipient
    from .autopilot_work import resolve_customer
    text = f"{event.get('subject') or ''}\n{event.get('text') or ''}"
    customer = resolve_customer(ctx, event, text[:20000])
    want = str(customer.get("number") or "").strip().upper()
    if not want:
        return None
    return customers_own(ctx, records, want)


def _ours(address: str, domains: Sequence[str]) -> bool:
    """One address, exactly, of our domains (a multi-address string, a display-name form
    or a guest's ``#EXT#`` UPN is no internal reader)."""
    a = str(address or "").strip().lower()
    return bool(a) and "#ext#" not in a and _addresses(a) == [a] and _internal(a, domains)


def guest_address(member: str) -> str:
    """A chat member's address: a guest's UPN ``joe_buyer.example#EXT#@tenant`` is
    ``joe@buyer.example``; another address as is; an id: "" (no address held)."""
    m = str(member or "").strip().lower()
    if "#ext#" in m:
        local, _, domain = m.split("#ext#", 1)[0].rpartition("_")
        m = f"{local}@{domain}" if local and "." in domain else ""
    return m if _addresses(m) == [m] else ""


def _chat_readers_own(ctx: Ctx, event: Mapping[str, Any], records: list[str]
                      ) -> list[str] | None:
    """``senders_own`` for a Teams message: the chat's members outside the directory
    (``autopilot_events._teams_events``: ``outside``) are its outside readers. None:
    unknown membership (a channel, an event typed without it): hold. Each outside address
    resolves to its customer, every one to the same (two customers: no record is both's);
    a member id the directory does not hold is a reader only an address of the chat can
    stand for (a guest is listed by id and address), so more such ids than outside
    addresses: hold. An address of our domains the directory does not hold is ours."""
    outside = event.get("outside")
    if outside is None or not isinstance(outside, (list, tuple)):
        return None
    if not outside:
        return records          # every member is a directory person
    from .autopilot_work import resolve_customer
    addresses = [guest_address(m) for m in outside if "@" in str(m)]
    ids = [m for m in outside if "@" not in str(m)]
    if "" in addresses or len(ids) > len(addresses):
        return None
    wants = set()
    for a in addresses:
        if _ours(a, ctx.domains):
            continue        # an address of our domains the mail directory does not hold
        got = resolve_customer(ctx, {"sender": a, "object_id": event.get("object_id") or ""}, "")
        want = str(got.get("number") or "").strip().upper()
        if not want:
            return None
        wants.add(want)
    if not wants:
        return records      # outside the directory but of our domains only
    if len(wants) > 1:
        return []           # readers of two customers: no record is each one's own
    return customers_own(ctx, records, wants.pop())


def customers_own(ctx: Ctx, records: Sequence[str], want: str) -> list[str]:
    """The records of ``records`` whose every customer is the customer numbered ``want``:
    its number is ``want``, or it is a ship-to card whose declared payer link
    (``Customer.billed_to``, ship-to -> bill-to) ends at a card numbered ``want`` (the
    bill-to customer asking about its ship-to's order). A ``bill_to_erp_code`` value with
    no link is no relationship; a ship-to never reads its payer's or a sibling's orders.
    A record with no customer is never named."""
    want = want.strip().upper()
    if not want:
        return []
    by_record = {r: _record_customers(ctx, r) for r in records}
    custs = sorted({c for cs in by_record.values() for c in cs})
    payer = {c: set() for c in custs}
    for _t, a, b in (ctx.touching(custs, ("Customer.billed_to",)) if custs else ()):
        if a in payer and b.startswith("Customer:"):
            payer[a].add(b)      # forward only: ``a`` (ship-to) is billed to ``b``
    cards = sorted(set(custs) | {b for bs in payer.values() for b in bs})
    codes = {c: _v(r, "erp_code").strip().upper()
             for c, r in (ctx.read(cards, ("erp_code",)) if cards else {}).items()}

    def ours(c: str) -> bool:
        return codes.get(c) == want or any(codes.get(b) == want for b in payer[c])
    return [r for r in records if by_record[r] and all(ours(c) for c in by_record[r])]


def h_answer_request(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    named = _records_named(ctx, event)
    orders = [o for o in named if o.startswith("SalesOrder:")]
    for inv in (o for o in named if o.startswith("SalesInvoice:")):
        orders += ctx.linked(inv, ("SalesInvoice.bills",))
    for sh in (o for o in named if o.startswith("Shipment:")):
        orders += ctx.linked(sh, ("Shipment.ships",))
    seen: set[str] = set()
    unique = []
    for o in dict.fromkeys(orders):
        if o in seen:
            continue
        seen.update(ctx.linked(o, ("SalesOrder.same_record_as",)))
        unique.append(o)
    # only the asking customer's own orders are named back (never another customer's
    # order found by a shared PO, order or invoice number); unresolved sender: hold
    own = senders_own(ctx, event, unique)
    if own is None:
        return {"skip": "status_customer_ambiguous"}
    if unique and not own:
        return {"skip": "no_own_record_named"}
    orders = own[:5]
    wants_docs = "document_request" in (event.get("intents") or ())
    out: dict[str, Any] = {"orders": [], "links": [], "gaps": []}
    lines = []
    for order in orders:
        f = _order_facts(ctx, order)
        out["orders"].append(f)
        ship = f["shipments"][0] if f["shipments"] else None
        bits = [f"Order {f['order_number']}" + (f" (your PO {f['customer_po']})"
                                                 if f["customer_po"] else "")
                + f": {f['status'] or 'in progress'}"]
        if ship and ship["ship_date"]:
            bits.append(f"shipped {ship['ship_date']}" + (f" via {ship['carrier']}"
                                                           if ship["carrier"] else "")
                        + (f", tracking {ship['tracking']}" if ship["tracking"] else ""))
        elif f["required_date"]:
            bits.append(f"required by {f['required_date']}")
        lines.append("; ".join(bits) + ".")
        if wants_docs:
            out["links"].append(_pack_link(order))
    if not orders:
        return {"skip": "no_record_named"}       # nothing to answer from: no item
    if False:
        out["gaps"].append("no_record_named")
        out["summary"] = "Asks for status or documents; no order, invoice or shipment named."
        lines = ["Could you send the PO or order number this is about? We will reply with "
                 "the status and documents right away."]
    else:
        out["summary"] = f"Asks about {len(orders)} order(s): " + " ".join(lines)[:SUMMARY_CHARS]
        if wants_docs:
            # the send carries no attachments (autopilot_write builds the message from the
            # body alone): the reply never says they are attached. The reviewer attaches
            # the pack (``links``) before sending; the gap says so (AUTOMATION-010)
            out["gaps"].append("docs_not_attached")
    out["draft"] = _reply(event, lines)
    return out


def h_delay_impact(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    pos = [o for o in event.get("records") or () if o.startswith("PurchaseOrder:")]
    pos += [o for o in identified(ctx, event.get("subject", ""), event.get("text", ""))
            if o.startswith("PurchaseOrder:")]
    for no in _numbers(PO_NUMBER, event.get("subject", ""), event.get("text", "")):
        pos += ctx.find("PurchaseOrder", "po_number", no)
    pos = list(dict.fromkeys(pos))[:5]
    out: dict[str, Any] = {"purchase_orders": [], "impacted": [], "options": [], "gaps": []}
    impacted: dict[str, dict[str, Any]] = {}
    for po in pos:
        head = ctx.read([po], ("po_number", "expected_date", "status", "vendor_name")).get(po)
        ctx.cite(head, "po_number", "expected_date")
        out["purchase_orders"].append({"object_id": po, "number": _v(head, "po_number"),
                                       "expected": _v(head, "expected_date")[:10],
                                       "supplier": _v(head, "vendor_name")})
        for pl in ctx.linked(po, (PO_LINE_OF,))[:MAX_LINES]:
            for item in ctx.linked(pl, (PO_LINE_ITEM,))[:1]:
                irow = ctx.read([item], ("code", "name", "quantity_on_hand")).get(item)
                others = [x for x in ctx.linked(item, (PO_LINE_ITEM,)) if x != pl][:20]
                if others:
                    out["options"].append({"item": _v(irow, "code") or _v(irow, "name"),
                                           "other_open_po_lines": len(others),
                                           "on_hand": _v(irow, "quantity_on_hand") or None})
                for sl in ctx.linked(item, (SO_LINE_ITEM,))[:200]:
                    for order in ctx.linked(sl, (SO_LINE_OF,))[:1]:
                        if order in impacted:
                            continue
                        r = ctx.read([order], ("order_number", "status", "required_date")
                                     ).get(order)
                        if not r or CLOSED.search(_v(r, "status")):
                            continue
                        cust = ctx.linked(order, ("SalesOrder.ordered_by",))[:1]
                        crow = ctx.read(cust, ("name", "email")).get(cust[0]) if cust else None
                        impacted[order] = {"object_id": order,
                                           "order_number": _v(r, "order_number"),
                                           "required_date": _v(r, "required_date")[:10],
                                           "customer": _v(crow, "name"),
                                           "customer_email": (_addresses(_v(crow, "email"))
                                                              or [None])[0],
                                           "customer_id": cust[0] if cust else None,
                                           "item": _v(irow, "code") or _v(irow, "name")}
                        ctx.cite(r, "order_number", "required_date")
    out["impacted"] = sorted(impacted.values(), key=lambda x: x["required_date"] or "9")[:20]
    if not pos:
        return {"skip": "no_purchase_order_named"}
    out["summary"] = (f"Supplier delay or shortage on {len(pos)} PO(s); "
                      f"{len(out['impacted'])} open customer order(s) use the items.")
    first = out["impacted"][0] if out["impacted"] else None
    if first and first["customer_email"]:
        out["customer_notice"] = {
            "channel": "email", "to": [{"address": first["customer_email"]}],
            # the customer record the address is read from (``_recipients`` cites it)
            "source": first["customer_id"],
            "subject": f"Update on your order {first['order_number']}",
            "body": "\n".join(["Hello,", "", f"A supplier has told us of a delay affecting "
                               f"your order {first['order_number']}. We are working on "
                               "alternatives and will confirm the new date shortly.", "",
                               "Best regards,"])}
    out["draft"] = _reply(event, ["Thank you for letting us know. Please confirm the new ship "
                                  "date and the quantity you can ship now."])
    return out


def h_approval(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    from . import proactive_threads as pt
    out: dict[str, Any] = {"gaps": [], "evidence": []}
    if event.get("object_type") == "MCApproval":
        f = event.get("fields") or {}
        task = next(iter(ctx.linked(event["object_id"], ("MCApproval.gate_of",))), None)
        trow = ctx.read([task], ("title", "stage", "task_id")).get(task) if task else None
        siblings = [o for o in ctx.linked(task, ("MCApproval.gate_of",))
                    if o != event["object_id"]] if task else []
        srows = ctx.read(siblings, ("status", "requested_at", "decided_at", "reason"))
        newer = sorted(o for o, r in srows.items()
                       if _v(r, "requested_at") > f.get("requested_at", "") and
                       _v(r, "status").lower() == "pending")
        approved = sorted((o for o, r in srows.items()
                           if _v(r, "status").lower() in ("approved", "granted")
                           and _v(r, "requested_at") < f.get("requested_at", "")
                           # granted before this request (a replay reads today's records)
                           and _v(r, "decided_at")[:19] <= f.get("requested_at", "")[:19]),
                          key=lambda o: _v(srows[o], "requested_at"))
        stage = _v(trow, "stage")
        if newer:
            rec, reasons = "superseded", ["a newer pending approval exists for the same task"]
        elif approved:
            last = srows[approved[-1]]
            rec, reasons = "approve", [
                f"an earlier approval of this task was granted on "
                f"{_v(last, 'decided_at')[:10] or _v(last, 'requested_at')[:10]}",
                f"task stage: {stage or 'not stated'}"]
        else:
            rec, reasons = "review", [
                f"no earlier approval of this task; task stage: {stage or 'not stated'}",
                f"requested by {f.get('requested_by') or 'unknown'} on "
                f"{(f.get('requested_at') or '')[:10]}"]
        out.update(
            summary=f"Approval requested for {_v(trow, 'task_id') or f.get('task_id')}: "
                    f"{(f.get('reason') or '')[:120]}",
            recommendation=rec, reasons=reasons,
            evidence=[{"object_id": o} for o in [task, *newer, *approved[-2:]] if o])
        ctx.cite(trow, "title", "stage")
        from . import mc_digest as md
        ctx.check()
        out["mc"] = md.item_facts(f.get("reason"), f.get("task_id"), md.history(
            ctx.st, str(event.get("audience") or ""), f.get("requested_at"),
            decisions=ctx.mc_decisions()))
        # the gate's own MC scope ({kind: launch|merge|flags, ...}); the record's ``scope``
        # key is the operator scope it was captured under, never the gate's
        scope = f.get("gate_scope")
        if isinstance(scope, str):
            try:
                scope = json.loads(scope)
            except ValueError:
                scope = None
        out["action"] = {"kind": "mc_approval", "gate": f.get("gate_id"),
                         "task_id": f.get("task_id") or _v(trow, "task_id") or None,
                         "requested_by": f.get("requested_by") or None,
                         "scope": scope if isinstance(scope, dict) else None,
                         "proposal": {"action": str(f.get("proposal_action") or "") or None,
                                      "summary": str(f.get("proposal_summary") or "")
                                      or None,
                                      "risk": str(f.get("proposal_risk") or "").strip()
                                      .lower() or None},
                         "note": "Decide in Mission Control; the Workbench never decides it "
                                 "unless this workflow's write gate is live "
                                 "(operating.autopilot_write)."}
        return out
    family = pt.approval_family(event.get("text") or "")
    out.update(summary=f"Approval asked ({family}): {(event.get('subject') or '')[:120]}",
               recommendation="review", reasons=[f"family: {family}"],
               evidence=[{"object_id": o} for o in event.get("records") or ()])
    out["draft"] = _reply(event, ["Thanks, I am reviewing this and will confirm today."])
    return out


def h_task(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    f = event.get("fields") or {}
    text = f"{f.get('title', '')}\n{f.get('description', '')}"
    probe = {"subject": f.get("title", ""), "text": f.get("description", ""), "records": []}
    named = _records_named(ctx, probe)
    for no in _numbers(PO_NUMBER, text):
        named += ctx.find("PurchaseOrder", "po_number", no)
    named = list(dict.fromkeys(named))[:MAX_RECORDS]
    facts = [_order_facts(ctx, o) for o in named if o.startswith("SalesOrder:")][:5]
    if not facts:
        # a task naming no order we can answer from would only restate itself: no item
        return {"skip": "no_record_named" if not named else "nothing_to_answer"}
    out = {"summary": f"Task: {f.get('title', '')[:140]}",
           "records": named, "orders": facts, "gaps": [] if named else ["no_record_named"],
           "links": [{"label": "Open the task's records", "href": f"/object/{o}"}
                     for o in named[:5]]}
    if facts:
        out["answer"] = [f"Order {x['order_number']}: {x['status'] or 'in progress'}"
                         + (f", shipped {x['shipments'][0]['ship_date']}"
                            if x["shipments"] and x["shipments"][0]["ship_date"] else "")
                         for x in facts]
    return out


def h_question(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    """A question an MC agent asked the owner on a task: the question, its options, the
    asker's recommended option with its reason as the draft answer, the task, and the
    business records the question names with their state. The answer is the owner's to give
    in Mission Control: nothing here answers, writes or sends."""
    q = event.get("question") or {}
    text, options = str(q.get("text") or ""), [str(o) for o in q.get("options") or ()]
    if not text or not options:
        return {"skip": "question_without_options"}
    task = str(event.get("task") or "")
    title = ""
    if task:
        row = ctx.read([task], ("title", "stage")).get(task) or {}
        ctx.cite(row, "title")
        title = _v(row, "title")
    named = _records_named(ctx, {"subject": title, "text": f"{text}\n{q.get('context') or ''}",
                                 "records": []})[:MAX_RECORDS]
    facts = [_order_facts(ctx, o) for o in named if o.startswith("SalesOrder:")][:5]
    rec = q.get("recommended")
    out: dict[str, Any] = {
        "summary": text[:140], "question": text, "context": str(q.get("context") or ""),
        "options": options, "task_id": q.get("task_id"), "task_title": title[:200],
        "records": named, "orders": facts,
        "links": ([{"label": "Open the task", "href": f"/object/{task}"}] if task else [])
        + [{"label": "Open a record it names", "href": f"/object/{o}"} for o in named[:4]],
        "note": "Answer in Mission Control; nothing is answered from here."}
    if task:
        out["task"] = {"object_id": task}     # the question is the item, not the whole task
    if isinstance(rec, int) and 0 <= rec < len(options):
        out["draft"] = {"choice": rec, "option": options[rec],
                        "reason": str(q.get("reason") or ""),
                        "basis": "the asking agent's recommendation"}
    else:
        out["gaps"] = ["no_recommended_option"]
    return out


def h_ledger(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    named = identified(ctx, event.get("subject", ""), event.get("text", ""))
    invs = [o for o in [*(event.get("records") or ()), *named] if o.startswith("SalesInvoice:")]
    for order in (o for o in named if o.startswith("SalesOrder:")):
        invs += ctx.linked(order, ("SalesInvoice.bills",))
    for no in _numbers(INVOICE_NUMBER, event.get("subject", ""), event.get("text", "")):
        invs += ctx.find("SalesInvoice", "invoice_number", no) or \
            ctx.find("SalesInvoice", "bc_number", no)
    for order in (o for o in event.get("records") or () if o.startswith("SalesOrder:")):
        invs += ctx.linked(order, ("SalesInvoice.bills",))
    invs = list(dict.fromkeys(invs))
    if not invs:
        return {"skip": "no_invoice_named"}
    # only the asking customer's own invoices: another customer's invoice under a shared
    # number (its balance, due date) is never stated to this one; unresolved sender: hold
    own = senders_own(ctx, event, invs)
    if own is None:
        return {"skip": "invoice_customer_ambiguous"}
    if not own:
        return {"skip": "no_own_invoice_named"}
    invs = own[:5]
    rows = ctx.read(invs, ("invoice_number", "bc_number", "invoice_date", "due_date",
                           "total_amount", "remaining_amount", "currency", "status"))
    ledger, lines = [], []
    for inv, r in sorted(rows.items()):
        ctx.cite(r, "total_amount", "remaining_amount", "due_date")
        total, left = _decimal(_v(r, "total_amount")), _decimal(_v(r, "remaining_amount"))
        cur = _v(r, "currency")
        number = _v(r, "invoice_number") or _v(r, "bc_number")
        ledger.append({"object_id": inv, "number": number, "date": _v(r, "invoice_date")[:10],
                       "due": _v(r, "due_date")[:10], "total": str(total) if total is not None
                       else None, "remaining": str(left) if left is not None else None,
                       "currency": cur or None})
        if left is not None and left == 0:
            lines.append(f"Invoice {number} is paid in full.")
        elif left is not None:
            lines.append(f"Invoice {number}: {left} {cur} remains open"
                         + (f", due {_v(r, 'due_date')[:10]}." if _v(r, "due_date") else "."))
        else:
            lines.append(f"Invoice {number}: the open balance is not stated in our records.")
    out = {"ledger": ledger, "gaps": [] if ledger else ["no_invoice_named"],
           "summary": (" ".join(lines)[:SUMMARY_CHARS] if lines else
                       "Invoice or payment question; no invoice named.")}
    out["draft"] = _reply(event, lines or [
        "Could you send the invoice number? We will check the payment status right away."])
    return out


def h_qc(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    f = event.get("fields") or {}
    oid = event["object_id"]
    orders: list[str] = []
    for no in re.findall(r"[A-Z]{0,4}-?\d{3,12}", f.get("order_list", "")):
        orders += ctx.find("SalesOrder", "order_number", no)
    for ao in ctx.linked(oid, ("Batch.requested_by",))[:5]:
        orders += [o for o in ctx.linked(ao) if o.startswith("SalesOrder:")]
    orders = list(dict.fromkeys(orders))[:10]
    product = next(iter(ctx.linked(oid, ("Batch.makes_bulk",))), None)
    prow = ctx.read([product], ("code", "name")).get(product) if product else None
    affected, drafts = [], []
    for order in orders:
        r = ctx.read([order], ("order_number", "status", "required_date")).get(order)
        if not r or CLOSED.search(_v(r, "status")):
            continue
        cust = ctx.linked(order, ("SalesOrder.ordered_by",))[:1]
        crow = ctx.read(cust, ("name", "email")).get(cust[0]) if cust else None
        affected.append({"object_id": order, "order_number": _v(r, "order_number"),
                         "required_date": _v(r, "required_date")[:10],
                         "customer": _v(crow, "name")})
        email = (_addresses(_v(crow, "email")) or [None])[0]
        if email:
            drafts.append({"channel": "email", "to": [{"address": email}], "source": cust[0],
                           "subject": f"Update on your order {_v(r, 'order_number')}",
                           "body": "Hello,\n\nA quality check on a lot for your order "
                                   f"{_v(r, 'order_number')} needs more time. We will "
                                   "confirm the ship date shortly.\n\nBest regards,"})
    lot = f.get("lot_number") or f.get("batch_number") or ""
    return {"summary": f"Lot {lot} is {event.get('quality')}"
                       + (f" ({_v(prow, 'code') or _v(prow, 'name')})" if prow else "")
                       + f"; {len(affected)} open order(s) affected.",
            "lot": lot, "quality": event.get("quality"),
            "results": {k: v for k, v in f.items() if k.startswith("qc_")},
            "affected": affected, "customer_notices": drafts[:5],
            "gaps": [] if orders else ["no_order_named"]}


def _attendee_customer(ctx: Ctx, event: Mapping[str, Any], address: str) -> str:
    """The number of the one customer an outside attendee represents (the customer records
    stating its address, else its domain, ``autopilot_work.resolve_customer``); "" when
    none, several, or only a supplier states it."""
    from .autopilot_work import resolve_customer
    got = resolve_customer(ctx, {"object_id": event.get("object_id") or "",
                                 "sender": address.strip().lower()}, "")
    return str(got.get("number") or "").strip().upper()


def h_meeting(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
    """A follow-up to a meeting's outside attendees. A draft names only the open orders of
    the customer its addressees represent: attendees of one customer get one draft with
    that customer's orders; attendees of several customers (or an attendee no single
    customer states) get one draft per customer (``customer_notices``, each naming only its
    own customer's orders; an unresolved attendee's names none) and no single draft to all.
    With no outside attendee the internal draft lists the parties' open orders."""
    f = event.get("fields") or {}
    parties = event.get("parties") or []
    prow = ctx.read(parties, ("name",))
    open_orders = []
    for p in parties[:3]:
        for o in ctx.linked(p, ("SalesOrder.ordered_by",))[:200]:
            r = ctx.read([o], ("order_number", "status", "required_date")).get(o)
            if r and not CLOSED.search(_v(r, "status")):
                open_orders.append({"object_id": o, "order_number": _v(r, "order_number"),
                                    "status": _v(r, "status"),
                                    "required_date": _v(r, "required_date")[:10]})
    open_orders = sorted(open_orders, key=lambda x: x["required_date"] or "9")[:10]
    external = list(dict.fromkeys(
        a for a in event.get("attendees") or () if not _internal(a, ctx.domains)))
    subject = f"Follow-up: {f.get('subject', '')}"[:200]

    def draft(to: Sequence[str], orders: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        lines = ["Thank you for your time today. A short recap and next steps:", ""]
        lines += [f"- Order {o['order_number']}: {o['status']}"
                  + (f", required by {o['required_date']}" if o["required_date"] else "")
                  for o in orders] or ["- (add the agreed next steps)"]
        # every addressee: ``_recipients`` keeps the first and REPLY_ALL_MAX more, and
        # asks about the rest
        return {"channel": "email", "to": [{"address": a} for a in to], "subject": subject,
                "body": "\n".join(["Hello,", "", *lines, "", "Best regards,"])}

    out: dict[str, Any] = {"summary": f"Meeting ended: {f.get('subject', '')[:120]}",
                           "parties": [_v(r, "name") for r in prow.values()]}
    if not external:
        out.update(open_orders=open_orders, draft=draft([], open_orders),
                   gaps=["no_external_attendee"])
        return out
    # who each outside attendee speaks for: a customer number, or "" (no single customer)
    groups: dict[str, list[str]] = {}
    for a in external:
        groups.setdefault(_attendee_customer(ctx, event, a), []).append(a)
    ids = [o["object_id"] for o in open_orders]
    owned = {n: set(customers_own(ctx, ids, n)) if n else set() for n in groups}
    per = {n: [o for o in open_orders if o["object_id"] in owned[n]] for n in groups}
    gaps = []
    if "" in groups:
        gaps.append("attendee_customer_unknown")
    if len(groups) == 1:
        (n, to), = groups.items()
        out.update(open_orders=per[n], draft=draft(to, per[n]), gaps=gaps)
        return out
    # attendees of several customers: one draft each, never one listing all to all
    out["open_orders"] = [o for o in open_orders
                          if any(o["object_id"] in owned[n] for n in groups)]
    out["customer_notices"] = [dict(draft(to, per[n]), customer=n or None)
                               for n, to in sorted(groups.items(), key=lambda g: g[0] or "~")]
    out["gaps"] = ["attendees_span_customers", *gaps]
    return out


def _work(name: str) -> Callable[[Ctx, Mapping[str, Any]], dict[str, Any]]:
    def run(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
        from . import autopilot_work as aw
        return getattr(aw, name)(ctx, event)
    run.__name__ = name
    return run


def _receivables(name: str) -> Callable[[Ctx, Mapping[str, Any]], dict[str, Any]]:
    def run(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
        from . import autopilot_receivables as ar
        return getattr(ar, name)(ctx, event)
    run.__name__ = name
    return run


def _payables(name: str) -> Callable[[Ctx, Mapping[str, Any]], dict[str, Any]]:
    def run(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
        from . import autopilot_payables as pay
        return getattr(pay, name)(ctx, event)
    run.__name__ = name
    return run


def _collections(name: str) -> Callable[[Ctx, Mapping[str, Any]], dict[str, Any]]:
    def run(ctx: Ctx, event: Mapping[str, Any]) -> dict[str, Any]:
        from . import autopilot_collections as ac
        return getattr(ac, name)(ctx, event)
    run.__name__ = name
    return run


HANDLERS: dict[str, Callable[[Ctx, Mapping[str, Any]], dict[str, Any]]] = {
    "order_entry": _work("h_order_entry"), "po_followup": _work("h_po_followup"),
    "supplier_eta": _work("h_supplier_eta"), "desk": _work("h_desk"),
    "reply_prep": _work("h_reply_prep"), "invoice_send": _work("h_invoice_send"),
    "routine_update": _work("h_routine_update"),
    "rd_status_update": _work("h_rd_status_update"),
    "invoice_entry": _receivables("h_invoice_entry"),
    "shipment_posting": _receivables("h_shipment_posting"),
    "cash_application": _receivables("h_cash_application"),
    "ap_invoice_entry": _payables("h_ap_invoice_entry"),
    "collection_draft": _collections("h_collection_draft"),
    "answer_request": h_answer_request,
    "delay_impact": h_delay_impact, "approval_recommendation": h_approval,
    "task_prepare": h_task, "ledger_answer": h_ledger, "qc_impact": h_qc,
    "question_prepare": h_question,
    "meeting_followup": h_meeting}


def _meeting_extra(st: Any, event: dict[str, Any], domains: Sequence[str]) -> None:
    if event.get("event_type") == "meeting.ended":
        external = any(not _internal(a, domains) for a in event.get("attendees") or ())
        event["external_or_linked"] = bool(external or event.get("parties"))
        event["organizer_copy"] = True   # the business rule checks the organizer's own copy
