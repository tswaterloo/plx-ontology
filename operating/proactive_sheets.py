"""Alerts over sheet readings (``integrations.sheet_readings``): remittances matched to open
invoices, supplier statements reconciled with our payables, and the hand-kept on-hand sheets
``stock_out_risk`` reads beside the records' own stock. Run by ``operating.proactive_engine``
beside its other rules; the readings are read by index from their own file
(``readings_path``), the ledgers through ``operating.proactive_cash`` (``ar_docs``,
``ap_docs``). Nothing here writes; every figure is a cited value or a sum of cited values of
one document in its own currency (``Decimal``).

Which readings count
--------------------
A reading counts only through a source the store holds (its ``Attachment`` or
``SharePointFile`` record: what is cited, so a reader who may not open the file never sees
what it supports). People first, fail closed: a source of a person's mailbox or drive the
exclusion does not clear (``unit_clear``), or a message with one of our people on it who is
not cleared or an address that cannot be decided, is never read; with the exclusion not
loaded the sheet rules are ``blocked`` and ``stock_out_risk`` reads no sheet. Of several
copies of one file version, the earliest cleared source is cited.

* ``cash_application`` (``unapplied_remittance``, ``short_payment``): each remittance line's
  invoice reference is matched to our receivables (Sage AR open documents and BC sales
  invoices, numbers compared once punctuation, case and leading zeros go). A remittance
  whose references match supplier invoices instead is our own advice to a supplier and is
  counted (``outbound_advice``), never raised. A line paying an invoice still open in our
  books by the full open balance (amount paid plus discount), ``APPLY_GRACE_DAYS`` or more
  after its payment date, is unapplied cash; a line paying less than the open balance is a
  short payment. One alert per remittance and kind, about the largest invoice hit (a payer
  may pay several of its accounts at once); references matching nothing or more than one
  customer, and amounts in a currency other than the invoice's are counted by reason. Finance-marked.
* ``statement_reconciliation`` (``statement_difference``): a statement whose references
  match our payables is a supplier's (one that matches our receivables is one we sent and is
  counted); each of its lines is compared with the supplier's documents in our books: a
  line we do not hold (``not_in_our_books``), one we hold as paid
  (``paid_in_our_books``), one whose open amount differs (``amount_differs``), and our
  open invoices of the period the statement does not list (``not_on_statement``). A running
  balance column is read as such, never as a line's amount. Only a supplier's latest
  statement in the window is judged (older ones are superseded, counted); one alert per
  supplier with a difference. Finance-marked.
* ``sheet_on_hand``: the latest on-hand reading per item (an item number matching exactly
  one item cluster's code), dated within ``ON_HAND_FRESH_DAYS``; the lots of one sheet sum,
  two sheets of one day that disagree leave the item unjudged.

Fixture runs are ``isolated-fixture``; no rule names a record, a customer or an item.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

from .proactive_cash import Doc, _norm, ap_docs, ar_docs, similar_numbers
from .proactive_ops import _cleared

CASH_RULE = "cash_application"
STATEMENT_RULE = "statement_reconciliation"
RULES = (CASH_RULE, STATEMENT_RULE)
UNAPPLIED = "unapplied_remittance"
SHORT = "short_payment"
DIFFERENCE = "statement_difference"
FINANCE = '["finance"]'
READINGS_FILE = "sheet-readings.sqlite"
READINGS_ENV = "PLX_SHEET_READINGS"

APPLY_GRACE_DAYS = 7         # a payment this old should be applied in our books
REMIT_DAYS = 120             # remittances paid within this many days are judged
STATEMENT_DAYS = 120
STATEMENT_GRACE_DAYS = 7     # our payment this recent may not have reached the supplier
ON_HAND_FRESH_DAYS = 31
TOLERANCE = Decimal("0.01")
MIN_REF = 4                  # characters of a normalized reference that may match


def readings_path(store_path: Any) -> Path:
    """The readings file the rules read: ``PLX_SHEET_READINGS``, else beside the store."""
    env = os.environ.get(READINGS_ENV)
    return Path(env) if env else Path(store_path).parent / READINGS_FILE


@dataclass(frozen=True)
class Reading:
    file_key: str
    table: int
    seq: int
    cell: str
    fields: dict
    facts: dict
    name: str
    received: date | None
    cite: dict            # the source row cited: l1_handle, object_id, markings


def _day(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw or "")[:10])
    except ValueError:
        return None


def _internal(people: Any, address: str) -> bool:
    domain = address.rsplit("@", 1)[1] if "@" in address else ""
    return bool(domain) and any(domain == d or domain.endswith("." + d)
                                for d in getattr(people, "internal", ()) or ())


def _source_cleared(people: Any, audience: str, participants: Sequence[str]) -> bool:
    if not people.unit_clear(audience):
        return False
    # an address that cannot be decided ("") never clears; an external one is no person of
    # ours; ours must each be cleared
    return all(a and (not _internal(people, a) or _cleared(people, [a]))
               for a in participants)


def load(path: Path, kind: str, people: Any, skipped: Counter) -> list[Reading]:
    """The readings of one kind through their earliest cleared source the store holds."""
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        sources: dict[str, tuple] = {}
        refused: Counter = Counter()
        for row in conn.execute(
                "SELECT s.file_key, s.handle, s.object_id, s.audience_key, s.markings, "
                "s.received_at, s.participants, s.name FROM sources s WHERE s.file_key IN "
                "(SELECT DISTINCT file_key FROM tables WHERE kind = ?) ORDER BY "
                "s.received_at, s.handle", (kind,)):
            key, handle, oid, audience, markings, received, participants, name = row
            if key in sources:
                continue
            if oid is None:
                refused[key] = refused[key] or 1
                continue
            try:
                parts = json.loads(participants or "[]")
            except ValueError:
                parts = [""]
            if not _source_cleared(people, audience or "", parts):
                refused[key] = 2
                continue
            sources[key] = (handle, oid, markings or "[]", _day(received), name)
        for key, why in refused.items():
            if key not in sources:
                skipped["source_not_in_store" if why == 1 else "people_not_cleared"] += 1
        facts = {(k, t): json.loads(f) for k, t, f in conn.execute(
            "SELECT file_key, table_no, facts FROM tables WHERE kind = ?", (kind,))}
        out = []
        for key, t_no, seq, cell, fields in conn.execute(
                "SELECT file_key, table_no, seq, cell, fields FROM readings WHERE kind = ? "
                "ORDER BY file_key, table_no, seq", (kind,)):
            src = sources.get(key)
            if src is None:
                continue
            handle, oid, markings, received, name = src
            out.append(Reading(key, t_no, seq, cell, json.loads(fields),
                               facts.get((key, t_no), {}), name, received,
                               {"l1_handle": handle, "object_id": oid, "markings": markings}))
        return out
    finally:
        conn.close()


def _v(r: Reading, name: str) -> str | None:
    got = r.fields.get(name)
    return got[0] if got else None


def _dec(raw: Any) -> Decimal | None:
    try:
        return Decimal(str(raw)) if raw is not None else None
    except ArithmeticError:
        return None


def _fact_day(facts: Mapping[str, Any], *labels: str) -> date | None:
    for label in labels:
        got = facts.get(label)
        if got:
            from integrations.sheet_readings import parse_date
            day = _day(got[0]) or parse_date(got[0])
            if day is not None:
                return day
    return None


class _Index:
    """Documents by number: the same once punctuation, case and leading zeros go, else one
    number inside the other with only letters around it (``similar_numbers``)."""

    def __init__(self, groups: Mapping[str, list[Doc]]) -> None:
        self.exact: dict[str, list[Doc]] = defaultdict(list)
        self.digits: dict[str, list[Doc]] = defaultdict(list)
        for docs in groups.values():
            for doc in docs:
                key = _norm(doc.number)
                if len(key) >= MIN_REF and doc.kind != "other":
                    self.exact[key].append(doc)
                    if len(found := re.sub(r"\D", "", key)) >= 4:
                        self.digits[found].append(doc)

    def get(self, ref: str, default: Any = None) -> list[Doc]:
        if len(ref) < MIN_REF:
            return default if default is not None else []
        if ref in self.exact:
            return self.exact[ref]
        digits = re.sub(r"\D", "", ref)
        near = [d for d in self.digits.get(digits, ()) if similar_numbers(ref, d.number)] \
            if len(digits) >= 4 else []
        return near or (default if default is not None else [])


def _index(groups: Mapping[str, list[Doc]]) -> _Index:
    return _Index(groups)


def _open(doc: Doc) -> bool:
    if doc.paid is True:
        return False
    return doc.balance is not None and doc.balance > TOLERANCE


def _fmt(n: Decimal) -> str:
    return format(n.quantize(Decimal("0.01")), "f")


def _blocked(people: Any, result: Callable, path: Path) -> dict[str, Any] | None:
    if not path.is_file():   # the reading step has not run on this host: a setup state
        return result([], status="not_set_up", reasons=["sheet_readings_not_set_up"])
    if people is None or getattr(people, "unavailable", None):
        cause = getattr(people, "unavailable", None) or "exclusion_not_loaded"
        return result([], status="blocked", reasons=["people_exclusion_unavailable", cause])
    return None


def people_blocked(people: Any, result: Callable) -> dict[str, Any] | None:
    """``blocked`` when the people exclusion is not loaded (a rule reading mailed sheets
    fails closed), else None."""
    if people is None or getattr(people, "unavailable", None):
        cause = getattr(people, "unavailable", None) or "exclusion_not_loaded"
        return result([], status="blocked", reasons=["people_exclusion_unavailable", cause])
    return None


def _doc_type(doc: Doc) -> str:
    return doc.oid.split(":", 1)[0]


# --------------------------------------------------------------------------- remittances

def cash_application(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                     result: Callable, settled: Sequence[str], skips: Callable,
                     people: Any, readings: Path) -> dict[str, Any]:
    stop = _blocked(people, result, readings)
    if stop is not None:
        return stop
    skipped = skips()
    counts: Counter = Counter()
    lines = load(readings, "remittance", people, skipped)
    ar, ap = _index(ar_docs(st)), _index(ap_docs(st))
    by_table: dict[tuple, list[Reading]] = defaultdict(list)
    for r in lines:
        by_table[(r.file_key, r.table)].append(r)
    gaps = []
    for (key, t_no), rows in sorted(by_table.items()):
        counts["remittances"] += 1
        matched: list[tuple[Reading, list[Doc]]] = []
        outbound = 0
        for r in rows:
            paid_on = _paid_on(r)
            if paid_on is None:
                skipped["amount_or_date_unread"] += 1
                continue
            if not (as_of - timedelta(days=REMIT_DAYS) <= paid_on <= as_of):
                skipped["outside_window"] += 1
                continue
            ref = _norm(_v(r, "ref"))
            docs = ar.get(ref, []) if len(ref) >= MIN_REF else []
            if docs:
                matched.append((r, docs))
            elif len(ref) >= MIN_REF and ap.get(ref):
                outbound += 1
            else:
                skipped["reference_not_matched"] += 1
        if outbound > len(matched):
            counts["outbound_advice"] += 1
            skipped["outbound_advice"] += len(matched)
            continue
        if not matched:
            continue
        # a payer may pay invoices of several of its accounts (bill-to, ship-to) at once
        hits: dict[str, list[tuple[Reading, Doc, Decimal, Decimal, date]]] = defaultdict(list)
        for r, docs in matched:
            parties = {d.party for d in docs}
            if len(parties) != 1:
                skipped["reference_ambiguous"] += 1
                continue
            doc = docs[0]
            counts["lines"] += 1
            paid = _dec(_v(r, "amount_paid") or _v(r, "amount") or (
                _v(r, "payment_amount") if len(rows) == 1 else None))
            discount = _dec(_v(r, "discount")) or Decimal(0)
            paid_on = _paid_on(r)
            if paid is None:
                skipped["amount_or_date_unread"] += 1
                continue
            stated = (r.facts.get("currency") or [None])[0]
            if stated and doc.currency and stated != doc.currency:
                skipped["currency_mismatch"] += 1
                continue
            if not _open(doc):
                counts["applied"] += 1
                continue
            covered = paid + discount
            if covered + TOLERANCE >= doc.balance:
                if (as_of - paid_on).days >= APPLY_GRACE_DAYS:
                    hits[UNAPPLIED].append((r, doc, paid, discount, paid_on))
                else:
                    counts["recent"] += 1
            elif covered > 0:
                hits[SHORT].append((r, doc, paid, discount, paid_on))
        for kind, found in sorted(hits.items()):
            counts[kind] += 1
            found.sort(key=lambda h: (-h[1].balance, h[1].number))
            top = found[0][1]
            currency = {h[1].currency for h in found}
            cur = currency.pop() if len(currency) == 1 else None
            open_sum = sum((h[1].balance for h in found), Decimal(0))
            short_by = sum((h[1].balance - h[2] - h[3] for h in found), Decimal(0))
            when = max(h[4] for h in found)
            n = len(found)
            party = found[0][1].party if len({h[1].party for h in found}) == 1 else \
                f"{len({h[1].party for h in found})} accounts"
            money = (f" ({_fmt(open_sum)} {cur} open in our books)" if kind == UNAPPLIED
                     else f" ({_fmt(short_by)} {cur} short of the open balance)") if cur \
                else ""
            summary = (f"Remittance of {when.isoformat()} from customer {party} pays {n} "
                       f"invoice{'' if n == 1 else 's'} still open in our books{money}"
                       if kind == UNAPPLIED else
                       f"Remittance of {when.isoformat()} from customer {party} pays {n} "
                       f"invoice{'' if n == 1 else 's'} less than they owe{money}")
            groups = [cites([h[0].cite], FINANCE) for h in found] + [
                cites([*h[1].cite(*_doc_props(h[1]))], FINANCE) for h in found]
            made = gap(kind, CASH_RULE, top.oid, _doc_type(top), f"{key}:{t_no}",
                       summary=summary,
                       missing=("Was the payment received and applied to these invoices?"
                                if kind == UNAPPLIED else
                                "Why was less paid: a deduction, a dispute or an error?"),
                       why=("The customer's remittance advice lists these invoices as paid "
                            "in full, but our books still show them open." if kind == UNAPPLIED
                            else "The customer's remittance advice pays these invoices less "
                                 "than their open balance in our books."),
                       priority="medium", about=(_doc_type(top), "balance"),
                       groups=groups,
                       event={"amount": {"value": _fmt(open_sum if kind == UNAPPLIED
                                                       else short_by), "currency": cur}
                              if cur else None,
                              "invoices": [h[1].number for h in found],
                              "cells": [h[0].cell for h in found], "paid_on": when.isoformat()})
            gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = dict(sorted(counts.items()))
    return out


def _paid_on(r: Reading) -> date | None:
    return (_day(_v(r, "payment_date"))
            or _fact_day(r.facts, "payment date", "cheque date", "check date", "value date",
                         "date", "_first_date") or r.received)


def _doc_props(doc: Doc) -> list[str]:
    return [p for p in doc.rows if not p.startswith("_")]


# --------------------------------------------------------------------------- statements

def _line_open(rows: list[Reading]) -> dict[int, Decimal | None]:
    """Each statement line's open amount: its balance, unless the balance column runs
    (each the last plus the line's amount), else its amount or debit less credit."""
    amounts = [_dec(_v(r, "amount") or _v(r, "invoice_amount")) for r in rows]
    balances = [_dec(_v(r, "balance")) for r in rows]
    running = False
    pairs = [(balances[i - 1], amounts[i], balances[i]) for i in range(1, len(rows))
             if None not in (balances[i - 1], amounts[i], balances[i])]
    if len(pairs) >= 2 and sum(1 for a, m, b in pairs if a + m == b) >= 0.8 * len(pairs):
        running = True
    out = {}
    for i, r in enumerate(rows):
        if balances[i] is not None and not running:
            out[r.seq] = balances[i]
        elif amounts[i] is not None:
            out[r.seq] = amounts[i]
        else:
            debit, credit = _dec(_v(r, "debit")), _dec(_v(r, "credit"))
            out[r.seq] = None if debit is None and credit is None else (
                (debit or Decimal(0)) - (credit or Decimal(0)))
    return out


def statement_reconciliation(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                             result: Callable, settled: Sequence[str], skips: Callable,
                             people: Any, readings: Path) -> dict[str, Any]:
    stop = _blocked(people, result, readings)
    if stop is not None:
        return stop
    skipped = skips()
    counts: Counter = Counter()
    lines = load(readings, "statement", people, skipped)
    ap_groups = ap_docs(st)
    ar, ap = _index(ar_docs(st)), _index(ap_groups)
    by_party: dict[str, list[Doc]] = defaultdict(list)
    for docs in ap_groups.values():
        for d in docs:
            by_party[d.party].append(d)
    by_table: dict[tuple, list[Reading]] = defaultdict(list)
    for r in lines:
        by_table[(r.file_key, r.table)].append(r)
    gaps = []
    latest: dict[str, list[tuple]] = defaultdict(list)
    for (key, t_no), rows in sorted(by_table.items()):
        counts["statements"] += 1
        refs = [(r, _norm(_v(r, "ref"))) for r in rows]
        in_ap = Counter(d.party for _r, ref in refs if len(ref) >= MIN_REF
                        for d in ap.get(ref, [])[:1])
        in_ar = sum(1 for _r, ref in refs if len(ref) >= MIN_REF and ar.get(ref))
        if not in_ap:
            counts["customer_statement" if in_ar else "supplier_not_matched"] += 1
            continue
        if in_ar > sum(in_ap.values()):
            counts["customer_statement"] += 1
            continue
        party = in_ap.most_common(1)[0][0]
        first = rows[0]
        stated_on = (_fact_day(first.facts, "statement date", "date", "as of", "as at",
                               "_first_date") or first.received)
        if stated_on is None or not (as_of - timedelta(days=STATEMENT_DAYS) <= stated_on
                                     <= as_of):
            skipped["outside_window"] += 1
            continue
        counts["supplier_statements"] += 1
        latest[party].append((stated_on, max((r.received or date.min) for r in rows), key,
                              t_no, rows, refs))
    for party, found in sorted(latest.items()):
        # a supplier's latest statement states where it stands; older ones are superseded
        stated_on, _got, key, t_no, rows, refs = max(found, key=lambda f: f[:4])
        skipped["superseded_statement"] += len(found) - 1
        opens = _line_open(rows)
        theirs = {d.oid: d for d in by_party[party]}
        listed: set[str] = set()
        diffs: list[tuple[str, Reading | None, Doc | None, Decimal | None]] = []
        for r, ref in refs:
            amount = opens.get(r.seq)
            if amount is None:
                skipped["amount_unread"] += 1
                continue
            doc = next((d for d in ap.get(ref, []) if d.party == party), None) \
                if len(ref) >= MIN_REF else None
            if doc is None:
                if amount != 0:
                    diffs.append(("not_in_our_books", r, None, amount))
                continue
            listed.add(doc.oid)
            if not _open(doc):
                if amount != 0 and (doc.paid_on is None or doc.paid_on <= stated_on
                                    - timedelta(days=STATEMENT_GRACE_DAYS)):
                    diffs.append(("paid_in_our_books", r, doc, amount))
                continue
            if abs(amount - doc.balance) > TOLERANCE:
                diffs.append(("amount_differs", r, doc, amount - doc.balance))
        days = [d for r in rows for d in (_day(_v(r, "invoice_date")), _day(_v(r, "date")))
                if d is not None]
        if days:
            since = min(days)
            for doc in theirs.values():
                if doc.oid in listed or not _open(doc) or doc.kind != "invoice" or \
                        doc.doc_date is None:
                    continue
                if since <= doc.doc_date <= stated_on - timedelta(days=STATEMENT_GRACE_DAYS):
                    diffs.append(("not_on_statement", None, doc, -doc.balance))
        if not diffs:
            counts["agrees"] += 1
            continue
        counts[DIFFERENCE] += 1
        by_class = Counter(c for c, *_ in diffs)
        docs = [d for _c, _r, d, _a in diffs if d is not None]
        top = max(docs, key=lambda d: (d.balance or 0)) if docs else \
            max(theirs.values(), key=lambda d: d.doc_date or date.min)
        currencies = {d.currency for d in theirs.values() if d.currency}
        cur = currencies.pop() if len(currencies) == 1 else None
        net = sum((a for *_x, a in diffs if a is not None), Decimal(0))
        parts = ", ".join(f"{n} {c.replace('_', ' ')}" for c, n in sorted(by_class.items()))
        groups = [cites([r.cite], FINANCE) for _c, r, _d, _a in diffs if r is not None] + [
            cites(d.cite(*_doc_props(d)), FINANCE) for d in docs]
        made = gap(DIFFERENCE, STATEMENT_RULE, top.oid, _doc_type(top), f"{key}:{t_no}",
                   summary=f"Statement of {stated_on.isoformat()} from supplier {party} "
                           f"differs from our payables on {len(diffs)} item"
                           f"{'' if len(diffs) == 1 else 's'} ({parts}"
                           + (f"; net {_fmt(net)} {cur}" if cur else "") + ")",
                   missing="Which side is right for each item: a missing or duplicate "
                           "invoice, a payment not received, or a credit not taken?",
                   why="The supplier's statement and our payables disagree on these items "
                       "(each statement line and each of our documents cited).",
                   priority="medium", about=(_doc_type(top), "balance"),
                   # the statement itself is always cited: a reader who may not open it
                   # never sees an alert about it, even one listing only our documents
                   groups=[cites([rows[0].cite], FINANCE), *groups],
                   event={"amount": {"value": _fmt(abs(net)), "currency": cur} if cur
                          else None, "by_class": dict(sorted(by_class.items())),
                          "cells": [r.cell for _c, r, _d, _a in diffs if r is not None],
                          "stated_on": stated_on.isoformat()})
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = dict(sorted(counts.items()))
    return out


# --------------------------------------------------------------------------- on hand

PRODUCT_CODES = ("code", "erp_code")


def sheet_on_hand(st: Any, as_of: date, readings: Path, people: Any,
                  item_of: Callable[[str], str], wanted: Iterable[str],
                  cites: Callable, skipped: Any) -> dict[str, tuple]:
    """item cluster -> (quantity, unit word, cite group, day) from the latest fresh
    on-hand reading of the items ``wanted``; nothing when people are not cleared."""
    from integrations.sheet_readings import name_date, parse_date

    if people is None or getattr(people, "unavailable", None) or not readings.is_file():
        return {}
    wanted = set(wanted)
    codes: dict[str, set[str]] = defaultdict(set)
    for oid, entry in st.values(["Product"], PRODUCT_CODES).items():
        for prop in PRODUCT_CODES:
            for row in entry["props"].get(prop) or ():
                key = _norm(row.get("value"))
                if len(key) >= 2:
                    codes[key].add(item_of(oid))
    local: Counter = Counter()
    per_item: dict[str, dict[date, dict[str, list[Reading]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list)))
    for r in load(readings, "on_hand", people, local):
        tops = codes.get(_norm(_v(r, "item")), set())
        if len(tops) != 1:
            continue
        (top,) = tops
        if top not in wanted:
            continue
        day = (_day(_v(r, "date")) or _fact_day(r.facts, "inventory date", "count date",
                                                  "as of", "as at", "report date", "date",
                                                  "period ending", "week ending",
                                                  "_first_date")
               or name_date(r.name) or r.received)
        if day is None or day > as_of:
            continue
        per_item[top][day][r.file_key].append(r)
    out: dict[str, tuple] = {}
    for top, by_day in per_item.items():
        day = max(by_day)
        if (as_of - day).days > ON_HAND_FRESH_DAYS:
            skipped.note("on_hand_reading_stale", [top], [(top, "[]")])
            continue
        totals = {}
        for key, rows in by_day[day].items():
            units = {_v(r, "unit") for r in rows}
            qty = sum((_dec(_v(r, "qty")) or Decimal(0) for r in rows), Decimal(0))
            totals[key] = (qty, units.pop() if len(units) == 1 else "", rows)
        if len({(q, u) for q, u, _r in totals.values()}) != 1:
            skipped.note("on_hand_readings_disagree", [top], [(top, "[]")])
            continue
        qty, unit, rows = next(iter(totals.values()))
        out[top] = (qty, unit, cites([rows[0].cite]), day)
    for reason, n in local.items():
        skipped[reason] += n
    return out
