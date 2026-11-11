"""Precision of the prepared receivables entries (``operating.autopilot_receivables``)
against what people actually entered in the accounting system, per workflow and day.

For each day the run prepares what the autopilot would have prepared that day (its own
event scan and handler, the day as ``as_of``) and looks up the entry made afterwards:

* ``invoice_entry``: the invoice of the shipment's order dated within ``AFTER_DAYS`` of
  the shipment (the one whose receivables document names the shipment, else the order's
  only one). Right lines: the same (item, quantity) of every non-zero item line; right
  amount: the item lines' amount to the cent, same currency (charge lines are named, not
  priced: ``subtotal_right`` counts entries whose amount is the invoice's whole subtotal).
* ``shipment_posting``: the order's first shipment dated on or after the day, within
  ``AFTER_DAYS``. Right lines: the same (item, quantity) of every non-zero line.
* ``cash_application``: the receipt whose application lines pay the first invoice the
  remittance names, dated within ``AFTER_DAYS``. Right invoices: the same set of invoices;
  right amounts: each invoice paid the same amount to the cent.

Exact = everything right. Precision = exact / prepared (a prepared entry nobody entered
counts against it); ``of_found`` = exact / entries found. Output holds counts only (no
amount, customer or document number). Resumable: one file per (workflow, day) in ``--out``.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from typing import Any, Mapping, Sequence

from . import autopilot as ap
from . import autopilot_receivables as ar

WORKFLOWS = ("invoice_entry", "shipment_posting", "cash_application")
AFTER_DAYS = {"invoice_entry": 14, "shipment_posting": 7, "cash_application": 30}
CENT = Decimal("0.01")


def _lines_of(work: Mapping[str, Any]) -> Counter:
    return Counter((ln["item"].strip().upper(), Decimal(ln["quantity"]).normalize())
                   for ln in (work.get("entry") or {}).get("lines") or ())


def truth_invoice(ctx: Any, shipment: str, order: str, day: date) -> dict[str, Any] | None:
    head = ap._read(ctx.st, [shipment], ("sage_shipment_number",)).get(shipment)
    number = ap._v(head, "sage_shipment_number").strip().upper()
    found = []
    for oid, r in ar.invoices_of(ctx, order).items():
        d = ar._day(ap._v(r, "invoice_date"))
        if d is None or not (day <= d <= day + timedelta(days=AFTER_DAYS["invoice_entry"])):
            continue
        doc = ar._open_doc(ctx, ap._v(r, "invoice_number").strip())
        found.append((ap._v(doc, "idshipnbr").strip().upper() == number, d, oid, r))
    named = [f for f in found if f[0]]
    pick = named[0] if len(named) == 1 else (found[0] if len(found) == 1 else None)
    if pick is None:
        return None
    _named, _d, oid, r = pick
    ids = [o for o in ctx.linked(oid, ("SalesInvoiceLine.line_of",))
           if o.startswith("SalesInvoiceLine:")]
    rows = ap._read(ctx.st, ids, ("sage_item_code", "invoiced_quantity", "line_type",
                                  "extended_amount"))
    lines = Counter()
    items = sum((ap._decimal(ap._v(rows.get(o), "extended_amount")) or Decimal(0)
                 for o in ids if ap._v(rows.get(o), "line_type").strip() != ar.MISC_LINE),
                Decimal(0))
    for o in ids:
        if ap._v(rows.get(o), "line_type").strip() == ar.MISC_LINE:
            continue
        q = ap._decimal(ap._v(rows.get(o), "invoiced_quantity"))
        if q:
            lines[(ap._v(rows.get(o), "sage_item_code").strip().upper(), q.normalize())] += 1
    return {"lines": lines, "items": items, "subtotal": ap._decimal(ap._v(r, "subtotal")),
            "currency": ap._v(r, "currency").strip()}


def judge_invoice(ctx: Any, event: Mapping[str, Any], work: Mapping[str, Any],
                  day: date) -> Counter:
    c: Counter = Counter(prepared=1)
    truth = truth_invoice(ctx, event["object_id"], event["order_id"], day)
    if truth is None:
        return c
    entry = work["entry"]
    c["found"] += 1
    lines = _lines_of(work) == truth["lines"]
    currency = truth["currency"] == entry["currency"]
    amount = currency and abs(Decimal(entry["subtotal"]) - truth["items"]) <= CENT
    # the whole invoice: no charge left to key, or none on the invoice either
    whole = amount and truth["subtotal"] is not None and \
        abs(Decimal(entry["subtotal"]) - truth["subtotal"]) <= CENT
    c["lines_right"] += lines
    c["amount_right"] += amount
    c["exact"] += lines and amount
    c["subtotal_right"] += lines and whole
    c["charges_to_key"] += bool(entry.get("charges"))
    return c


def judge_posting(ctx: Any, event: Mapping[str, Any], work: Mapping[str, Any],
                  day: date) -> Counter:
    c: Counter = Counter(prepared=1)
    order = event["object_id"]
    ids = [o for o in ctx.linked(order, ("Shipment.ships",)) if ar._accounting(o)]
    heads = ap._read(ctx.st, ids, ("ship_date",))
    dated = sorted((d, s) for s in ids if (d := ar._day(ap._v(heads.get(s), "ship_date")))
                   and day <= d <= day + timedelta(days=AFTER_DAYS["shipment_posting"]))
    if not dated:
        return c
    c["found"] += 1
    by_line = ar.order_lines(ctx, order)
    got = Counter(((ar.match_line(by_line, sl) or {}).get("item", "?").strip().upper(),
                   sl["qty"].normalize())
                  for sl in ar.shipped_lines(ctx, dated[0][1]) if sl["qty"])
    right = got == _lines_of(work)
    c["lines_right"] += right
    c["exact"] += right
    return c


def _receipt(ctx: Any, invoice: str, day: date) -> list[tuple[str, Decimal]] | None:
    """The application lines (invoice, amount) of the receipt that pays ``invoice``."""
    hits = ctx.find("RelSageArTcp", "idinvc", invoice)
    rows = ap._read(ctx.st, hits, ("idinvc", "amtpaym", "cntbtch", "cntitem", "audtdate"))
    lo, hi = day, day + timedelta(days=AFTER_DAYS["cash_application"])
    keys = sorted({(ap._v(r, "cntbtch"), ap._v(r, "cntitem")) for r in rows.values()
                   if (d := ar._day(ap._v(r, "audtdate"))) and lo <= d <= hi})
    if not keys:
        return None
    batch, item = keys[0]
    same = ctx.find("RelSageArTcp", "cntbtch", batch)
    got = ap._read(ctx.st, same, ("idinvc", "amtpaym", "cntbtch", "cntitem"))
    return [(ap._v(r, "idinvc").strip().upper(), ap._decimal(ap._v(r, "amtpaym")) or Decimal(0))
            for r in got.values() if ap._v(r, "cntbtch") == batch and ap._v(r, "cntitem") == item]


def judge_cash(ctx: Any, event: Mapping[str, Any], work: Mapping[str, Any],
               day: date) -> Counter:
    c: Counter = Counter(prepared=1)
    applied = work["entry"]["applied"]
    truth = _receipt(ctx, applied[0]["invoice"], day)
    if not truth:
        return c
    c["found"] += 1
    mine = {a["invoice"].strip().upper(): Decimal(a["amount"]) for a in applied}
    theirs: dict[str, Decimal] = {}
    for inv, amt in truth:
        theirs[inv] = theirs.get(inv, Decimal(0)) + amt
    invoices = set(mine) == set(theirs)
    amounts = invoices and all(abs(mine[i] - theirs[i]) <= CENT for i in mine)
    c["invoices_right"] += invoices
    c["amount_right"] += amounts
    c["exact"] += amounts
    return c


JUDGES = {"invoice_entry": (ar.invoice_entry_events, ar.h_invoice_entry, judge_invoice),
          "shipment_posting": (ar.shipment_posting_events, ar.h_shipment_posting,
                               judge_posting),
          "cash_application": (None, ar.h_cash_application, judge_cash)}


def score_day(st: Any, people: Any, registry: Mapping[str, Any], workflow: str,
              day: date) -> dict[str, Any]:
    finder, handler, judge = JUDGES[workflow]
    st.autopilot_people = people
    events = ar.cash_application_events(st, registry, day, people) \
        if workflow == "cash_application" else finder(st, registry, day)
    total: Counter = Counter(events=len(events))
    for event in events:
        ctx = ap.Ctx(st, day, time.monotonic() + 60, ())
        work = handler(ctx, event)
        if work.get("skip"):
            total["skip:" + work["skip"]] += 1
            continue
        total.update(judge(ctx, event, work, day))
    return {"workflow": workflow, "day": day.isoformat(), "counts": dict(total)}


def table(out: Path) -> dict[str, Any]:
    """Per workflow: the summed counts and precision over every finished day."""
    sums: dict[str, Counter] = {}
    days: dict[str, int] = Counter()
    for f in sorted(out.glob("*.json")):
        if f.name == "summary.json":
            continue
        got = json.loads(f.read_text())
        sums.setdefault(got["workflow"], Counter()).update(got["counts"])
        days[got["workflow"]] += 1
    res = {}
    for wf, c in sums.items():
        n = c.get("prepared", 0)
        res[wf] = {"days": days[wf], **dict(c),
                   "precision": round(c.get("exact", 0) / n, 3) if n else None,
                   "of_found": round(c.get("exact", 0) / c["found"], 3) if c.get("found")
                   else None}
    return res


def main(argv: Sequence[str] | None = None) -> int:
    from . import proactive_engine as pe
    p = argparse.ArgumentParser(prog="python3 -m operating.receivables_score")
    p.add_argument("--store", required=True)
    p.add_argument("--since", required=True, help="first day (YYYY-MM-DD)")
    p.add_argument("--until", required=True, help="day after the last")
    p.add_argument("--workflow", choices=(*WORKFLOWS, "all"), default="all")
    p.add_argument("--out", required=True)
    p.add_argument("--sheet-readings", default=None,
                   help="the sheet readings file (default: PLX_SHEET_READINGS, else beside "
                        "the store)")
    p.add_argument("--budget-seconds", type=float, default=200.0,
                   help="stop after this long (resumable: rerun to go on)")
    p.add_argument("--source", choices=ap.STORE_SOURCES, default=None,
                   help="the SQLite generation at --store, or the Postgres ontology the API "
                        "reads in mode 1 (refused when it does not hold that generation)")
    a = p.parse_args(argv)
    if a.sheet_readings:
        import os
        from . import proactive_sheets as ps
        os.environ[ps.READINGS_ENV] = a.sheet_readings
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    people = pe._people(Path(a.store))
    if people is None or getattr(people, "unavailable", None):
        print(json.dumps({"status": "blocked", "reason": "people_exclusion_unavailable"}))
        return 2
    st = ap._store(a.store, a.source)
    registry = ap.load_registry()
    flows = WORKFLOWS if a.workflow == "all" else (a.workflow,)
    status = "complete"
    try:
        day, end = date.fromisoformat(a.since), date.fromisoformat(a.until)
        while day < end and status == "complete":
            for wf in flows:
                f = out / f"{wf}-{day.isoformat()}.json"
                if f.exists():
                    continue
                if time.monotonic() - began > a.budget_seconds:
                    status = "partial"
                    break
                f.write_text(json.dumps(score_day(st, people, registry, wf, day)))
            day += timedelta(days=1)
    finally:
        st.close()
    summary = {"status": status, "workflows": table(out)}
    (out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    print(json.dumps(summary, sort_keys=True))
    return 0 if status == "complete" else 75


if __name__ == "__main__":
    sys.exit(main())
