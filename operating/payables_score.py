"""Precision of the prepared AP invoice entries (``operating.autopilot_payables``) against
what people actually entered in Sage, over a held-out period.

Each Sage AP invoice entry dated in the period is replayed as the invoice received: the
handler sees only what the invoice states (vendor, number, date, PO, currency, total, tax,
item lines) and the entries keyed before it (by batch and entry number), never the entry's
own coding. The judge then compares with the entry as keyed:

* ``account_right``: the same multiset of G/L accounts on the distribution lines;
* ``exact``: the same multiset of (account, amount to the cent) and the same due date;
* ``amounts_predicted``: entries whose amounts include a line the handler distributed
  itself (``vendor_account``: the total less tax and item lines), as opposed to item lines
  whose amounts the invoice states.

A duplicate flag counts as prepared and never exact (the entry replayed was keyed, so a
flag on it is a miss unless proven otherwise). Also counted: the match status per entry,
skips by reason, duplicates flagged, and the
header calibration (``total_less_tax_is_lines``: the header's total less its tax equals
the lines keyed; the handler's remainder rule rests on it). Precision = exact / prepared.
Output holds counts only (no amount, supplier or document number); one file per day.
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
from . import autopilot_handlers
from . import autopilot_queue
from . import autopilot_reading
from . import autopilot_records
from . import autopilot_payables as pay

CENT = Decimal("0.01")
HANDLER_SECONDS = 60
GATE = 0.90
GATE_ITEMS = 15


def _lines(lines: Sequence[tuple[str, Decimal]]) -> Counter:
    return Counter((pay._norm(a), amt.quantize(CENT)) for a, amt in lines)


def truth(entry: pay.Entry) -> dict[str, Any] | None:
    got = []
    for ln in entry.lines:
        account = autopilot_records._v(ln["row"], "idglacct").strip()
        amount = autopilot_records._decimal(autopilot_records._v(ln["row"], "amtdist"))
        if not account or amount is None:
            return None
        got.append((account, amount))
    return {"lines": got, "due": pay._day(entry.v("datedue"))}


def judge(entry: pay.Entry, work: Mapping[str, Any]) -> Counter:
    out: Counter = Counter()
    status = (work.get("match") or {}).get("status") or "none"
    out["match:" + status] += 1
    if work.get("duplicate_of"):
        # judged: the replayed invoice was entered and kept, so a flag on it is a miss
        out["prepared"] += 1
        out["flagged_duplicate"] += 1
        return out
    want = truth(entry)
    if want is None:
        out["truth_unread"] += 1
        return out
    e = work["entry"]
    got = [(ln["sage_account"], Decimal(ln["amount"])) for ln in e["lines"]]
    out["prepared"] += 1
    if any(ln["basis"] == "vendor_account" for ln in e["lines"]):
        out["amounts_predicted"] += 1
    accounts = Counter(pay._norm(a) for a, _ in got) == \
        Counter(pay._norm(a) for a, _ in want["lines"])
    out["account_right"] += accounts
    amounts = _lines(got) == _lines(want["lines"])
    out["lines_right"] += amounts
    due = e.get("due_date") and want["due"] and e["due_date"] == want["due"].isoformat()
    out["due_right"] += bool(due)
    out["exact"] += bool(amounts and due)
    out["bc_account_given"] += all(ln.get("bc_account") for ln in e["lines"])
    return out


def calibrate(entry: pay.Entry) -> Counter:
    total, tax = autopilot_records._decimal(entry.v("amtinvctot")), autopilot_records._decimal(entry.v("amttaxtot"))
    lines = [autopilot_records._decimal(autopilot_records._v(ln["row"], "amtdist")) for ln in entry.lines]
    if total is None or tax is None or None in lines:
        return Counter(calibration_unread=1)
    return Counter(total_less_tax_is_lines=int(total - tax == sum(lines)),
                   total_is_lines=int(total == sum(lines)))


def score_day(st: Any, people: Any, registry: Mapping[str, Any], day: date) -> dict[str, Any]:
    st.autopilot_people = people
    events = pay.entry_events(st, registry, day, people, force=True)
    index = pay._entries(st)
    total: Counter = Counter(events=len(events))
    for event in events:
        entry = index["by_oid"][event["object_id"]]
        total.update(calibrate(entry))
        try:
            work = pay.h_ap_invoice_entry(
                autopilot_handlers.Ctx(st, day, time.monotonic() + HANDLER_SECONDS, ()), event)
        except autopilot_queue.HandlerTimeout:
            work = {"skip": "handler_timeout"}
        if work.get("skip"):
            total["skip:" + work["skip"]] += 1
            continue
        total.update(judge(entry, work))
    return {"workflow": pay.HANDLER, "day": day.isoformat(), "counts": dict(total)}


def table(out: Path) -> dict[str, Any]:
    c: Counter = Counter()
    days = 0
    for f in sorted(out.glob("*.json")):
        if f.name == "summary.json":
            continue
        c.update(json.loads(f.read_text())["counts"])
        days += 1
    n = c.get("prepared", 0)
    precision = round(c.get("exact", 0) / n, 3) if n else None
    return {"days": days, **dict(c), "precision": precision,
            "account_precision": round(c.get("account_right", 0) / n, 3) if n else None,
            "coverage": round(n / c["events"], 3) if c.get("events") else None,
            "gate": "pass" if n >= GATE_ITEMS and precision is not None and precision >= GATE
            else "fail"}


def main(argv: Sequence[str] | None = None) -> int:
    from . import proactive_engine as pe
    p = argparse.ArgumentParser(prog="python3 -m operating.payables_score")
    p.add_argument("--store", required=True)
    p.add_argument("--since", required=True, help="first day (YYYY-MM-DD)")
    p.add_argument("--until", required=True, help="day after the last")
    p.add_argument("--out", required=True)
    p.add_argument("--crosswalk", default=None, help=f"default: {pay.CROSSWALK_ENV}, else "
                   f"{pay.CROSSWALK_DEFAULT}")
    p.add_argument("--budget-seconds", type=float, default=600.0)
    p.add_argument("--source", choices=autopilot_reading.STORE_SOURCES, default=None)
    a = p.parse_args(argv)
    if a.crosswalk:
        import os
        os.environ[pay.CROSSWALK_ENV] = a.crosswalk
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    people = pe._people(Path(a.store))
    if people is None or getattr(people, "unavailable", None):
        print(json.dumps({"status": "blocked", "reason": "people_exclusion_unavailable"}))
        return 2
    st = ap._store(a.store, a.source)
    registry = ap.load_registry()
    status = "complete"
    try:
        day, end = date.fromisoformat(a.since), date.fromisoformat(a.until)
        while day < end:
            f = out / f"{pay.HANDLER}-{day.isoformat()}.json"
            if not f.exists():
                if time.monotonic() - began > a.budget_seconds:
                    status = "partial"
                    break
                f.write_text(json.dumps(score_day(st, people, registry, day)))
            day += timedelta(days=1)
    finally:
        st.close()
    summary = {"status": status, pay.HANDLER: table(out)}
    (out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    print(json.dumps(summary, sort_keys=True))
    return 0 if status == "complete" else 75


if __name__ == "__main__":
    sys.exit(main())
