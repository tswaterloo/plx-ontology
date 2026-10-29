"""Actual duty from what PLX paid: broker and duty charges, linked to purchase orders only through
recorded references, turned into an effective duty rate per purchase order and per item.

Sources (read-only relations, rows passed in by the caller):
- Sage AP posted lines (``sage.ap_pjd``) on a G/L account whose description names duty, brokerage,
  customs or tariff (``duty_actual_rules.json``); amounts are in the home currency.
- BC purchase invoices (``public.bc_purchase_invoice_snapshot``) from a vendor whose name names a
  customs broker; BC invoices carry no G/L account here, so the vendor name is the only signal.
Broker invoice mail and attachments are a separate capture; this module reads none.

A charge links to a purchase order only through a recorded reference: the PO number on the AP
invoice (``ponbr``/``ordrnbr``/``idponbr``) or a PO number written in the line text. A supplier
invoice number in the text that equals one recorded on a receipt is a lead only: it corroborates
a recorded PO reference to the same PO and never links a charge alone (a bare token such as a
year can equal any receipt's number). The broker invoice's own numbers are never matched.
Everything else stays unlinked with a reason. Nothing is matched on amount, date or vendor.

The Sage duty account is a combined 'Duties & Brokerage' account, so a derived rate is an upper
bound of the duty and says so (``includes_brokerage``).

Inbound freight is the same kind of charge (``kind="freight"``): AP lines on a G/L account whose
description names inbound freight (``rules['freight']``: outbound shipping, shipping supplies and
freight recovered or charged to customers are excluded by word, and a duty account is never a
freight account), or a BC invoice from a vendor named like a forwarder; the same reference,
lead-only and own-number rules link it, and its rate has its own plausibility ceiling.

The live landed cost reads both through ``ActualsFile``: an artifact built at capture time
(rates, counts and codes per PO, and the incoterm the PO and its supplier record), behind the
reader's finance gate. The same artifact carries estimates from PLX history (``po_parties``,
``history_from_rates``, ``aggregate_ratio``, ``resolve_estimates``) for the many orders with no
linked charge: rates and their basis only, labelled ``estimated_from_history``. Evidence label: isolated-fixture in tests; the live run is
``scripts/duty_actual_report.py`` (live-unverified until the verifier records it).
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

RULES_PATH = Path(__file__).with_name("duty_actual_rules.json")
BASIS_ACTUAL = "actual_recorded"
BASIS_HS_ORIGIN = "recorded_hs_origin_rate"
BASIS_RULE = "provisional_origin_rule"
BASIS_DEFAULT = "default_fallback"


def load_rules(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or RULES_PATH).read_text(encoding="utf-8"))


RULES = load_rules()


def _s(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _dec(value: Any) -> Decimal | None:
    text = _s(value)
    if not text:
        return None
    try:
        out = Decimal(text)
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def acct_key(value: Any) -> str | None:
    """A Sage G/L account id as seven digits (``41101-02`` and ``4110102`` are one account).
    Any other shape is unknown, so it never matches."""
    digits = re.sub(r"[\s-]", "", _s(value))
    return digits if re.fullmatch(r"\d{7}", digits) else None


def po_key(value: Any) -> str | None:
    """A PO number as an upper-case alphanumeric key; blank or no digit is no reference."""
    text = re.sub(r"[^A-Z0-9]", "", _s(value).upper())
    return text if text and re.search(r"\d", text) else None


KINDS = ("duty", "freight")


def kind_rules(kind: str, rules: Mapping[str, Any] = RULES) -> dict[str, Any]:
    """The keyword rules of one charge kind: duty (the top-level keys, a combined
    'Duties & Brokerage' account, so ``includes_brokerage``) or freight (``rules['freight']``).
    Any other kind is refused, never guessed."""
    if kind == "duty":
        return {"gl_description_keywords": rules["gl_description_keywords"],
                "gl_description_exclude": rules.get("gl_description_exclude", []),
                "vendor_name_keywords": rules["vendor_name_keywords"],
                "max_plausible_rate": rules["max_plausible_rate"], "includes_brokerage": True}
    if kind == "freight":
        return {**rules["freight"], "includes_brokerage": False}
    raise ValueError(f"unknown_charge_kind:{kind}")


def _named(text: str, keep: Sequence[str], drop: Sequence[str]) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in keep) and not any(w.lower() in low for w in drop)


def charge_accounts(gl_rows: Sequence[Mapping[str, Any]], kind: str = "duty",
                    rules: Mapping[str, Any] = RULES) -> dict[str, str]:
    """``{account key: description}`` for G/L accounts whose description names a charge of
    ``kind``. A freight account is never also a duty account (one charge, one kind)."""
    kr = kind_rules(kind, rules)
    duty = set(charge_accounts(gl_rows, "duty", rules)) if kind != "duty" else set()
    out: dict[str, str] = {}
    for row in gl_rows:
        key, desc = acct_key(row.get("acctid")), " ".join(_s(row.get("acctdesc")).split())
        if key and key not in duty and _named(desc, kr["gl_description_keywords"],
                                              kr["gl_description_exclude"]):
            out[key] = desc
    return out


def duty_accounts(gl_rows: Sequence[Mapping[str, Any]], rules: Mapping[str, Any] = RULES
                  ) -> dict[str, str]:
    """``{account key: description}`` for G/L accounts whose description names a duty charge."""
    return charge_accounts(gl_rows, "duty", rules)


def charge_vendors(rows: Sequence[Mapping[str, Any]], id_col: str, name_col: str,
                   kind: str = "duty", rules: Mapping[str, Any] = RULES) -> set[str]:
    """Vendor ids whose name names a party of ``kind`` (a customs broker, a forwarder)."""
    words = kind_rules(kind, rules)["vendor_name_keywords"]
    return {_s(r.get(id_col)) for r in rows
            if _s(r.get(id_col)) and _named(_s(r.get(name_col)), words, ())}


def broker_vendors(rows: Sequence[Mapping[str, Any]], id_col: str, name_col: str,
                   rules: Mapping[str, Any] = RULES) -> set[str]:
    """Vendor ids whose name names a customs broker."""
    return charge_vendors(rows, id_col, name_col, "duty", rules)


_LINE_TEXT = ("glref", "gldesc", "descomp", "glcomment")
_HEAD_TEXT = ("invcdesc", "textrmit", "texttrx", "desc", "textref")


def build_charges(rows: Mapping[str, Sequence[Mapping[str, Any]]],
                  rules: Mapping[str, Any] = RULES, kind: str = "duty") -> dict[str, Any]:
    """Charges of ``kind`` (duty and brokerage, or inbound freight), one per (source, vendor,
    invoice).

    ``rows``: ``sage.gl_account``, ``sage.ap_vendor``, ``sage.ap_pjd`` (lines), ``sage.ap_pjh``
    and ``sage.ap_ibh`` / ``sage.ap_obs`` (header text and PO references) and
    ``public.bc_purchase_invoice_snapshot``. Returns ``{charges, excluded, coverage}``. A line
    whose amount is not a number is excluded with a count, never read as zero. A BC vendor
    named like both a broker and a forwarder is neither kind (``vendor_named_both``)."""
    kr = kind_rules(kind, rules)
    accounts = charge_accounts(rows.get("sage.gl_account", ()), kind, rules)
    sage_vendors = charge_vendors(rows.get("sage.ap_vendor", ()), "vendorid", "vendname",
                                  kind, rules)
    refs: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    heads: dict[tuple[str, str], list[str]] = defaultdict(list)
    # The invoice date of a Sage charge: the line's when it holds one (Sage writes 0 for none),
    # else its invoice header's; headers stating different dates give no date.
    head_dates: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in (*rows.get("sage.ap_ibh", ()), *rows.get("sage.ap_pjh", ())):
        day = _when(r.get("dateinvc"))
        if day:
            head_dates[(_s(r.get("idvend")), _s(r.get("idinvc")))].add(day)
    for r in rows.get("sage.ap_ibh", ()):
        k = (_s(r.get("idvend")), _s(r.get("idinvc")))
        for col in ("ponbr", "ordrnbr"):
            if _s(r.get(col)):
                refs[k].append(("po_number", _s(r[col])))
        heads[k] += [_s(r.get(c)) for c in _HEAD_TEXT if _s(r.get(c))]
    for r in rows.get("sage.ap_obs", ()):
        k = (_s(r.get("idvend")), _s(r.get("idinvc")))
        for col in ("idponbr", "idordrnbr"):
            if _s(r.get(col)):
                refs[k].append(("po_number", _s(r[col])))
    for r in rows.get("sage.ap_pjh", ()):
        k = (_s(r.get("idvend")), _s(r.get("idinvc")))
        heads[k] += [_s(r.get(c)) for c in _HEAD_TEXT if _s(r.get(c))]
    pattern = re.compile(rules["po_reference_pattern"], re.IGNORECASE)
    charges: dict[tuple[str, str, str], dict[str, Any]] = {}
    excluded = {"amount_not_numeric": 0, "vendor_named_both": 0}
    for r in rows.get("sage.ap_pjd", ()):
        acct = acct_key(r.get("idacct"))
        if acct not in accounts:
            continue
        amount = _dec(r.get("amtextndhc"))
        if amount is None:
            excluded["amount_not_numeric"] += 1
            continue
        k = (_s(r.get("idvend")), _s(r.get("idinvc")))
        c = charges.setdefault(("sage", *k), {
            "kind": kind, "source": "sage.ap_pjd", "vendor": k[0], "invoice": k[1],
            "amount": Decimal(0), "currency": rules["home_currency"], "accounts": set(),
            "lines": 0, "refs": list(refs.get(k, ())), "texts": list(heads.get(k, ())),
            "broker_by_name": k[0] in sage_vendors, "party_by_name": k[0] in sage_vendors,
            "date": _when(r.get("dateinvc")) or (next(iter(head_dates[k]))
                                                 if len(head_dates.get(k, ())) == 1 else None),
            "includes_brokerage": kr["includes_brokerage"]})
        c["amount"] += amount
        c["lines"] += 1
        c["accounts"].add(acct)
        c["texts"] += [_s(r.get(col)) for col in _LINE_TEXT if _s(r.get(col))]
    bc_rows = rows.get("public.bc_purchase_invoice_snapshot", ())
    bc_vendors = charge_vendors(bc_rows, "vendor_number", "vendor_name", kind, rules)
    other = charge_vendors(bc_rows, "vendor_number", "vendor_name",
                           "freight" if kind == "duty" else "duty", rules)
    for r in bc_rows:
        if _s(r.get("vendor_number")) not in bc_vendors:
            continue
        if _s(r.get("vendor_number")) in other:
            excluded["vendor_named_both"] += 1
            continue
        amount = _dec(r.get("amount_excluding_tax"))
        if amount is None:
            excluded["amount_not_numeric"] += 1
            continue
        k = (_s(r.get("vendor_number")), _s(r.get("number")))
        c = charges.setdefault(("bc", *k), {
            "kind": kind, "source": "public.bc_purchase_invoice_snapshot", "vendor": k[0],
            "invoice": k[1], "amount": Decimal(0),
            "currency": _s(r.get("currency_code")) or rules["home_currency"],
            "accounts": set(), "lines": 0, "refs": [], "texts": [], "broker_by_name": True,
            "party_by_name": True, "date": _when(r.get("invoice_date")),
            "includes_brokerage": kr["includes_brokerage"]})
        c["amount"] += amount
        c["lines"] += 1
        if _s(r.get("order_number")):
            c["refs"].append(("po_number", _s(r["order_number"])))
        if _s(r.get("vendor_invoice_number")):
            c.setdefault("own_numbers", []).append(_s(r["vendor_invoice_number"]))
    out = []
    for c in charges.values():
        for text in c["texts"]:
            for token in pattern.findall(text):
                c["refs"].append(("po_text", token))
        c["accounts"] = sorted(c["accounts"])
        out.append(c)
    party = "broker" if kind == "duty" else "forwarder"
    return {"charges": out, "excluded": excluded,
            "coverage": {"gl_accounts": len(accounts), f"sage_{party}_vendors": len(sage_vendors),
                         f"bc_{party}_vendors": len(bc_vendors - other), "charges": len(out),
                         "sage_charges": sum(c["source"] == "sage.ap_pjd" for c in out),
                         "bc_charges": sum(c["source"].startswith("public.") for c in out)}}


def link_charges(charges: Sequence[Mapping[str, Any]], pos: Mapping[str, Any],
                 supplier_invoices: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Link each charge to one purchase order, or leave it unlinked with a reason.

    ``pos``: ``{po key (po_key): record}`` for every known PO; ``supplier_invoices``:
    ``{supplier invoice number (upper case): [po key, ...]}`` from receipts. A reference counts
    only when it names a known PO. Several different POs make the charge unsplittable (no
    allocation rule is recorded), so it stays unlinked. Returns one dict per charge, in order."""
    out = []
    supplier_invoices = supplier_invoices or {}
    for c in charges:
        found: dict[str, set[str]] = defaultdict(set)
        saw_ref = False
        for kind, value in c.get("refs", ()):
            saw_ref = True
            key = po_key(value)
            if key and key in pos:
                found[key].add(kind)
        # A supplier invoice number found in free text is a lead only: a bare token (a year,
        # an entry number) can equal any receipt's number. It corroborates a recorded PO
        # reference to the same PO and never links a charge alone. The broker invoice's own
        # numbers are never candidates.
        own = {_s(c.get("invoice")).upper(), *(n.upper() for n in c.get("own_numbers", ()))}
        leads: set[str] = set()
        for text in c.get("texts", ()):
            for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-/]{3,}", text):
                if not re.search(r"\d", token) or token.upper() in own:
                    continue
                leads.update(k for k in supplier_invoices.get(token.upper(), ()) if k in pos)
        for key in leads & set(found):
            found[key].add("supplier_invoice")
        base = {"kind": c.get("kind", "duty"), "source": c["source"], "vendor": c["vendor"],
                "invoice": c["invoice"], "amount": c["amount"], "currency": c["currency"],
                "includes_brokerage": c.get("includes_brokerage", True)}
        if len(found) == 1:
            key, kinds = next(iter(found.items()))
            out.append({**base, "linked": True, "po": key, "linked_by": sorted(kinds)})
        elif found:
            out.append({**base, "linked": False, "reason": "MULTIPLE_POS_NO_SPLIT_RULE",
                        "pos": sorted(found)})
        elif leads:
            out.append({**base, "linked": False, "reason": "SUPPLIER_INVOICE_LEAD_ONLY",
                        "lead_pos": sorted(leads)})
        else:
            out.append({**base, "linked": False,
                        "reason": "REFERENCE_NOT_A_KNOWN_PO" if saw_ref
                        else "NO_REFERENCE_RECORDED"})
    return out


def po_effective_rates(links: Sequence[Mapping[str, Any]], goods: Mapping[str, Mapping[str, Any]],
                       rules: Mapping[str, Any] = RULES, kind: str = "duty"
                       ) -> dict[str, dict[str, Any]]:
    """Effective rate of ``kind`` per PO (``<kind>_paid``; the ceiling is the kind's own
    ``max_plausible_rate``): linked charges / the PO's goods value, both in the home
    currency. ``goods``: ``{po key: {value, currency, rate, source}}`` where ``rate`` is the
    PO's recorded exchange rate to the home currency (not needed for a home-currency PO). A PO
    whose goods value, currency or rate is missing, or whose charge currency differs, has no
    rate: ``rate`` None and ``gap`` names why. A rate above ``max_plausible_rate`` is no rate
    either (the charge probably covers more than this PO)."""
    home = rules["home_currency"]
    ceiling = Decimal(kind_rules(kind, rules)["max_plausible_rate"])
    per_po: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for link in links:
        if link.get("linked"):
            per_po[link["po"]].append(link)
    out: dict[str, dict[str, Any]] = {}
    for po, items in per_po.items():
        duty = sum((i["amount"] for i in items), Decimal(0))
        base = {f"{kind}_paid": duty, "currency": home, "charges": len(items),
                "includes_brokerage": any(i["includes_brokerage"] for i in items),
                "rate": None, "basis": BASIS_ACTUAL,
                "linked_by": sorted({k for i in items for k in i["linked_by"]})}
        g = goods.get(po)
        value, cur = (_dec(g.get("value")), _s(g.get("currency")).upper()) if g else (None, "")
        if any(i["currency"].upper() != home for i in items):
            out[po] = {**base, "gap": "CHARGE_CURRENCY_NOT_HOME"}
        elif value is None or value <= 0 or not cur:
            out[po] = {**base, "gap": "GOODS_VALUE_NOT_RECORDED"}
        else:
            fx = Decimal(1) if cur == home else _dec(g.get("rate"))
            if fx is None or fx <= 0:
                out[po] = {**base, "gap": "EXCHANGE_RATE_NOT_RECORDED"}
                continue
            goods_home = value * fx
            rate = duty / goods_home
            if rate <= 0 or rate > ceiling:
                out[po] = {**base, "goods_value_home": goods_home,
                           "gap": "RATE_NOT_PLAUSIBLE_CHARGE_MAY_COVER_MORE_THAN_THIS_PO"}
            else:
                out[po] = {**base, "goods_value_home": goods_home, "rate": rate,
                           "goods_source": g.get("source")}
    return out


def item_rates(po_rates: Mapping[str, Mapping[str, Any]],
               po_lines: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Effective rate per item: ``{item: [{po, rate, exact, ...}]}``. ``po_lines``: ``{po key:
    [{item, value}]}``. A single-line PO gives that item the PO's rate exactly; on a multi-line PO
    the duty is only known for the whole PO, so each item carries the PO rate pro rata by goods
    value (``exact`` False)."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for po, lines in po_lines.items():
        got = po_rates.get(po)
        if not got or got.get("rate") is None:
            continue
        for line in lines:
            if _s(line.get("item")):
                out[_s(line["item"])].append({
                    "po": po, "rate": got["rate"], "exact": len(lines) == 1,
                    "allocation": "whole purchase order" if len(lines) == 1
                    else "pro rata by goods value (one rate for every line of the PO)",
                    "includes_brokerage": got["includes_brokerage"]})
    return dict(out)


def actual_duty_for_po_line(po: Any, po_rates: Mapping[str, Mapping[str, Any]]
                            ) -> dict[str, Any] | None:
    """The ``actual_duty`` argument of ``landed_cost.po_line_landed``: this PO's recorded
    effective rate, or None (no linked charge, or the PO's rate has a gap)."""
    got = po_rates.get(po_key(po) or "")
    if not got or got.get("rate") is None:
        return None
    return {"rate": str(got["rate"]), "basis": BASIS_ACTUAL, "po": po_key(po),
            "includes_brokerage": got["includes_brokerage"],
            "_source": f"broker/duty charges linked by {', '.join(got['linked_by'])} "
                       f"({got['charges']} charge(s)) over the PO's recorded goods value"}


def actual_freight_for_po_line(po: Any, po_rates: Mapping[str, Mapping[str, Any]]
                               ) -> dict[str, Any] | None:
    """The ``actual_freight`` argument of ``landed_cost.po_line_landed``: this PO's recorded
    effective freight rate (inbound freight charges linked by a recorded reference over its
    recorded goods value), or None (no linked charge, or the PO's rate has a gap)."""
    got = po_rates.get(po_key(po) or "")
    if not got or got.get("rate") is None:
        return None
    return {"rate": str(got["rate"]), "basis": BASIS_ACTUAL, "po": po_key(po),
            "_source": f"inbound freight charges linked by {', '.join(got['linked_by'])} "
                       f"({got['charges']} charge(s)) over the PO's recorded goods value"}


def choose_duty(actual: Mapping[str, Any] | None, cited: Mapping[str, Any]) -> dict[str, Any]:
    """Which duty a landed cost uses, and why: an actual recorded duty first, then a recorded
    HS x origin rate, then a provisional business rule, then the labelled default. ``cited`` is
    ``hs_tariff.cite_duty``'s answer. ``basis`` says which; ``rate`` is None on a gap."""
    if actual is not None:
        return {"basis": BASIS_ACTUAL, "rate": Decimal(actual["rate"]), "actual": actual,
                "otherwise": cited.get("rate")}
    status = cited.get("status")
    if status == "cited" and not cited.get("provisional_rules"):
        basis = BASIS_HS_ORIGIN
    elif status == "cited":
        basis = BASIS_RULE
    elif status == "default":
        basis = BASIS_DEFAULT
    else:
        return {"basis": None, "rate": None, "gap": cited.get("gap")}
    return {"basis": basis, "rate": cited["rate"], "chain": cited.get("chain"),
            "provisional_rules": cited.get("provisional_rules", [])}


def sage_po_goods(receipts: Sequence[Mapping[str, Any]], rules: Mapping[str, Any] = RULES
                  ) -> dict[str, dict[str, Any]]:
    """Goods value per PO from Sage PO receipt headers, in the home currency: each receipt's
    ``extended`` (its own currency) times its recorded ``rate``. The PO header and detail
    amounts are zero for most POs, so the received value is the recorded goods value. A
    receipt that spans several POs (``multipor``), has an unreadable amount or has no usable
    rate makes its PO's value unknown (``value`` None), never a partial sum."""
    home = rules["home_currency"]
    out: dict[str, dict[str, Any]] = {}
    for r in receipts:
        key = po_key(r.get("ponumber"))
        if key is None:
            continue
        got = out.setdefault(key, {"value": Decimal(0), "currency": home, "receipts": 0,
                                   "source": "sage.po_receipt_header.extended x rate"})
        amount, cur = _dec(r.get("extended")), _s(r.get("currency")).upper()
        fx = Decimal(1) if cur == home else _dec(r.get("rate"))
        multi = _s(r.get("multipor")).lower() in {"1", "true", "y", "yes"}
        if got["value"] is None or amount is None or amount < 0 or not cur or multi \
                or fx is None or fx <= 0:
            got["value"] = None
        else:
            got["value"] += amount * fx
        got["receipts"] += 1
    return out


def supplier_invoice_index(receipts: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """``{supplier invoice number (upper case): [po key]}`` from receipts that record both. A
    number needs a digit; one number on several POs lists them all, so the link fails as
    ambiguous instead of guessing."""
    out: dict[str, set[str]] = defaultdict(set)
    for r in receipts:
        inv, key = _s(r.get("invnumber")).upper(), po_key(r.get("ponumber"))
        if inv and key and re.search(r"\d", inv):
            out[inv].add(key)
    return {k: sorted(v) for k, v in out.items()}


# ---- estimates from PLX history ---------------------------------------------------------------
#
# Most purchase orders have no linked duty or freight charge. Their landed cost uses a rate
# estimated from what PLX paid on other orders: per supplier, else per recorded supplier
# country, else company-wide (``rules['history']``). Each estimate carries its basis: the
# level, the method, the sample size, the period and the spread (median and interquartile
# range). It is labelled ``estimated_from_history`` and never stands for a recorded value.

BASIS_HISTORY = "estimated_from_history"
HISTORY_LEVELS = ("supplier", "country", "all")
HISTORY_METHODS = {"median_of_po_rates": "purchase_orders",
                   "pooled_period_ratio": "quarters"}
# The smallest sample any history estimate may rest on, per unit: below it there is no
# estimate, whatever ``rules['history']`` says (a rules file may only raise it). The landed
# cost reader refuses a captured estimate below it the same way (HISTORY_SAMPLE_TOO_SMALL).
HISTORY_MIN_SAMPLE = {"purchase_orders": 3, "quarters": 3}


def _when(value: Any) -> str | None:
    """A recorded date as ``YYYY-MM-DD``: Sage stores ``YYYYMMDD`` digits, the Portal an ISO
    date or date-time. Anything else (or Sage's 0 for no date) is no date."""
    text = _s(value)
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(?:\.0+)?", text) or \
        re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    return f"{y:04d}-{mo:02d}-{d:02d}" if 1990 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31 \
        else None


def _quarter(day: str | None) -> str | None:
    return f"{day[:4]}-Q{(int(day[5:7]) - 1) // 3 + 1}" if day else None


def _country(value: Any) -> str | None:
    """A recorded country as written (letters and spaces, upper case). Spellings are never
    merged: 'CN' and 'CHINA' stay two countries."""
    text = " ".join(re.sub(r"[^A-Z ]", " ", _s(value).upper()).split())
    return text or None


def po_parties(rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, dict[str, Any]]:
    """``{po key: {suppliers, countries, date}}`` from the Sage PO header (``vdcode``,
    ``vdcountry``, ``date``) and the Portal purchase order (``supplier_id`` and its supplier's
    ``country``, ``date_generated`` else ``created_at``). A supplier key names its system
    (``sage:`` vendor code, ``portal:`` supplier id); keys of two systems are never merged."""
    out: dict[str, dict[str, Any]] = {}

    def add(key: str | None, supplier: str | None, country: Any, day: str | None) -> None:
        if not key:
            return
        got = out.setdefault(key, {"suppliers": set(), "countries": set(), "dates": set()})
        if supplier:
            got["suppliers"].add(supplier)
        if _country(country):
            got["countries"].add(_country(country))
        if day:
            got["dates"].add(day)
    for r in rows.get("sage.po_header", ()):
        code = _s(r.get("vdcode"))
        add(po_key(r.get("ponumber")), f"sage:{code}" if code else None, r.get("vdcountry"),
            _when(r.get("date")))
    countries = {_s(r.get("id")): r.get("country") for r in rows.get("public.app_supplier", ())}
    for r in rows.get("public.app_purchase_order", ()):
        sid = _s(r.get("supplier_id"))
        add(po_key(r.get("po_number")), f"portal:{sid}" if sid else None, countries.get(sid),
            _when(r.get("date_generated")) or _when(r.get("created_at")))
    return {k: {"suppliers": sorted(v["suppliers"]), "countries": sorted(v["countries"]),
                "date": min(v["dates"]) if v["dates"] else None} for k, v in out.items()}


def _quantile(values: Sequence[Decimal], q: Decimal) -> Decimal:
    """Linear interpolation between closest ranks of sorted ``values`` (the 'inclusive'
    method), exact in Decimal."""
    pos = (len(values) - 1) * q
    low = int(pos)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (pos - low)


def rate_stats(values: Sequence[Decimal]) -> dict[str, Any]:
    """Sample size, median and interquartile range of non-empty ``values``."""
    ordered = sorted(values)
    return {"n": len(ordered), "median": _quantile(ordered, Decimal("0.5")),
            "q1": _quantile(ordered, Decimal("0.25")), "q3": _quantile(ordered, Decimal("0.75"))}


def _period(days: Sequence[str | None]) -> str:
    dated = sorted(d for d in days if d)
    return f"{dated[0][:7]}..{dated[-1][:7]}" if dated else "undated"


def _estimate(stats: Mapping[str, Any], *, rate: Decimal, level: str, method: str, period: str,
              includes_brokerage: bool) -> dict[str, Any]:
    q = Decimal("0.000001")
    return {"rate": str(rate.quantize(q)), "level": level, "method": method, "n": stats["n"],
            "unit": HISTORY_METHODS[method], "period": period,
            "median": str(stats["median"].quantize(q)), "q1": str(stats["q1"].quantize(q)),
            "q3": str(stats["q3"].quantize(q)), "includes_brokerage": includes_brokerage}


def history_from_rates(po_rates: Mapping[str, Mapping[str, Any]],
                       parties: Mapping[str, Mapping[str, Any]], kind: str,
                       rules: Mapping[str, Any] = RULES) -> dict[str, Any]:
    """Rate estimates of ``kind`` per supplier key, per recorded country and company-wide
    (``all``) from the POs' effective rates (``po_effective_rates``). Only a rate above zero
    and within the kind's ceiling counts: a flagged rate (above the ceiling, no goods value,
    another currency) is excluded and counted. A group smaller than ``min_pos`` (``all``:
    ``min_pos_all``), or than ``HISTORY_MIN_SAMPLE`` purchase orders, gets no estimate.
    Each estimate is the median rate."""
    hist, kr = rules["history"], kind_rules(kind, rules)
    ceiling = Decimal(kr["max_plausible_rate"])
    usable: dict[str, Decimal] = {}
    excluded = 0
    for po, r in po_rates.items():
        rate = r.get("rate")
        if rate is not None and Decimal(0) < Decimal(str(rate)) <= ceiling:
            usable[po] = Decimal(str(rate))
        else:
            excluded += 1
    groups: dict[str, dict[str, list[str]]] = {"supplier": defaultdict(list),
                                               "country": defaultdict(list)}
    for po in usable:
        party = parties.get(po) or {}
        for key in party.get("suppliers", ()):
            groups["supplier"][key].append(po)
        if len(party.get("countries", ())) == 1:   # a PO whose records disagree counts nowhere
            groups["country"][party["countries"][0]].append(po)

    def est(pos: Sequence[str], level: str) -> dict[str, Any]:
        stats = rate_stats([usable[p] for p in pos])
        return _estimate(stats, rate=stats["median"], level=level, method="median_of_po_rates",
                         period=_period([(parties.get(p) or {}).get("date") for p in pos]),
                         includes_brokerage=bool(kr["includes_brokerage"]))
    out: dict[str, Any] = {"by_supplier": {}, "by_country": {}, "all": None}
    for level, name in (("supplier", "by_supplier"), ("country", "by_country")):
        for key, pos in groups[level].items():
            if len(pos) >= max(int(hist["min_pos"]), HISTORY_MIN_SAMPLE["purchase_orders"]):
                out[name][key] = est(pos, level)
    if len(usable) >= max(int(hist["min_pos_all"]), HISTORY_MIN_SAMPLE["purchase_orders"]):
        out["all"] = est(sorted(usable), "all")
    out["counts"] = {"pos_with_rate": len(usable), "excluded_rates": excluded,
                     "suppliers_with_estimate": len(out["by_supplier"]),
                     "countries_with_estimate": len(out["by_country"])}
    return out


def aggregate_ratio(charges: Sequence[Mapping[str, Any]],
                    receipts: Sequence[Mapping[str, Any]], kind: str,
                    rules: Mapping[str, Any] = RULES) -> dict[str, Any]:
    """Company-wide rate of ``kind`` from aggregates, for charges that name no purchase order:
    the Sage AP charges (home currency) over the goods value of the Sage receipts (``extended``
    x ``rate``, home currency), both per calendar quarter. Only one ledger is compared with
    itself (a BC invoice has no Sage receipt beside it). A quarter counts only when it has both
    charges and goods and its ratio is within the kind's ceiling; at least ``min_periods``
    quarters (never fewer than ``HISTORY_MIN_SAMPLE``), else no estimate. The rate is
    pooled (sum of charges / sum of goods over the
    counted quarters); the spread is that of the quarterly ratios."""
    hist, kr = rules["history"], kind_rules(kind, rules)
    home, ceiling = rules["home_currency"], Decimal(kr["max_plausible_rate"])
    paid: dict[str, Decimal] = defaultdict(Decimal)
    goods: dict[str, Decimal] = defaultdict(Decimal)
    counts = {"charges": 0, "charges_undated_or_not_home": 0, "receipts": 0,
              "receipts_unreadable": 0}
    for c in charges:
        quarter = _quarter(_when(c.get("date")))
        if c.get("source") != "sage.ap_pjd" or _s(c.get("currency")).upper() != home \
                or quarter is None:
            counts["charges_undated_or_not_home"] += 1
            continue
        paid[quarter] += Decimal(c["amount"])
        counts["charges"] += 1
    for r in receipts:
        quarter = _quarter(_when(r.get("date")))
        amount, cur = _dec(r.get("extended")), _s(r.get("currency")).upper()
        fx = Decimal(1) if cur == home else _dec(r.get("rate"))
        if quarter is None or amount is None or amount < 0 or not cur or fx is None or fx <= 0:
            counts["receipts_unreadable"] += 1
            continue
        goods[quarter] += amount * fx
        counts["receipts"] += 1
    kept = sorted(q for q in paid if goods.get(q, Decimal(0)) > 0 and paid[q] > 0
                  and paid[q] / goods[q] <= ceiling)
    counts.update(quarters_with_both=sum(1 for q in paid if goods.get(q, Decimal(0)) > 0),
                  quarters_kept=len(kept))
    if len(kept) < max(int(hist["min_periods"]), HISTORY_MIN_SAMPLE["quarters"]):
        return {"all": None, "counts": counts}
    stats = rate_stats([paid[q] / goods[q] for q in kept])
    pooled = sum((paid[q] for q in kept), Decimal(0)) / sum((goods[q] for q in kept), Decimal(0))
    return {"all": _estimate(stats, rate=pooled, level="all", method="pooled_period_ratio",
                             period=f"{kept[0]}..{kept[-1]}",
                             includes_brokerage=bool(kr["includes_brokerage"])),
            "counts": counts}


def resolve_estimates(parties: Mapping[str, Mapping[str, Any]],
                      history: Mapping[str, Mapping[str, Any]],
                      rules: Mapping[str, Any] = RULES
                      ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """-> (``{po: {kind: estimate}}`` at supplier or country level, ``{kind: estimate}`` the
    company-wide default). ``history``: ``{kind: {linked: history_from_rates(...),
    aggregate: aggregate_ratio(...)}}``. A PO gets its supplier's estimate when exactly one of
    its supplier keys has one, else its country's when its records name one country that has
    one; otherwise the default applies (``rules['history']['default_order']``)."""
    per_po: dict[str, dict[str, Any]] = {}
    default: dict[str, Any] = {}
    for kind, got in history.items():
        linked = got.get("linked") or {}
        for source in rules["history"]["default_order"][kind]:
            found = (got.get(source) or {}).get("all")
            if found:
                default[kind] = found
                break
        for po, party in parties.items():
            sup = {json.dumps(linked["by_supplier"][k], sort_keys=True)
                   for k in party.get("suppliers", ()) if k in linked.get("by_supplier", {})}
            if len(sup) == 1:
                per_po.setdefault(po, {})[kind] = json.loads(next(iter(sup)))
                continue
            countries = party.get("countries", ())
            if len(countries) == 1 and countries[0] in linked.get("by_country", {}):
                per_po.setdefault(po, {})[kind] = linked["by_country"][countries[0]]
    return per_po, default


# ---- the capture-built artifact the live landed cost reads ----------------------------------
#
# Built at capture time from live read-only relations (``scripts/duty_actual_report.py``) into
# ``<state_dir>/landed-actuals/landed_actuals.json`` (a capture store, never Git) and read by
# the ``plx.investigate`` landed_cost engine per purchase order number (``ActualsFile``). It
# holds rates, counts and codes only: no amount, vendor or invoice of any charge.

ARTIFACT_DIR = "landed-actuals"
ARTIFACT_FILE = "landed_actuals.json"
ARTIFACT_SCHEMA = "plx-landed-actuals/v1"


def incoterms_by_po(rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, dict[str, Any]]:
    """``{po key: {po, vendor_default}}`` from the Portal purchase order's ``incoterms`` (its
    override, ERP-D-016) and its supplier's ``incoterms`` (the default). A blank is None."""
    suppliers = {_s(r.get("id")): _s(r.get("incoterms")) or None
                 for r in rows.get("public.app_supplier", ()) if _s(r.get("id"))}
    out: dict[str, dict[str, Any]] = {}
    for r in rows.get("public.app_purchase_order", ()):
        key = po_key(r.get("po_number"))
        if key:
            out[key] = {"po": _s(r.get("incoterms")) or None,
                        "vendor_default": suppliers.get(_s(r.get("supplier_id")))}
    return out


def _rate_record(r: Mapping[str, Any]) -> dict[str, Any]:
    return {"rate": None if r.get("rate") is None else str(r["rate"]), "gap": r.get("gap"),
            "charges": r["charges"], "linked_by": list(r["linked_by"]),
            "includes_brokerage": bool(r.get("includes_brokerage"))}


def build_artifact(rates: Mapping[str, Mapping[str, Mapping[str, Any]]],
                   incoterms: Mapping[str, Mapping[str, Any]], known_pos: Sequence[str], *,
                   built_at: str, estimates: Mapping[str, Mapping[str, Any]] | None = None,
                   history_default: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The artifact: ``rates`` ``{kind: po_effective_rates(...)}``; ``known_pos`` every PO key
    the charges were linked against (a key outside it was never looked at, which is not 'no
    charge'). Only POs with a linked charge, a recorded incoterm or a supplier- or
    country-level history estimate (``estimates``, ``resolve_estimates``) get a record; the
    company-wide estimates are ``history_default``. Rates, counts and periods only."""
    records: dict[str, dict[str, Any]] = {}
    for po, got in (estimates or {}).items():
        if got:
            records.setdefault(po, {})["estimate"] = {k: dict(v) for k, v in got.items()}
    for kind in KINDS:
        for po, r in (rates.get(kind) or {}).items():
            records.setdefault(po, {})[kind] = _rate_record(r)
    for po, inc in incoterms.items():
        if inc.get("po") or inc.get("vendor_default"):
            records.setdefault(po, {})["incoterm"] = dict(inc)
    return {"schema": ARTIFACT_SCHEMA, "built_at": built_at, "known_pos": sorted(set(known_pos)),
            "records": dict(sorted(records.items())),
            "history_default": {k: dict(v) for k, v in (history_default or {}).items()}}


class ActualsFile:
    """The landed_cost engine's ``landed_actuals`` provider over one artifact file: called with
    PO keys, it answers ``{records: {key: record}, known: [keys], default_estimates: {kind:
    company-wide history estimate}, digest, built_at}``, or
    ``{unavailable: code}`` when the file is missing (LANDED_ACTUALS_NOT_CAPTURED) or is not
    this schema (LANDED_ACTUALS_UNREADABLE): never an empty answer that reads as 'no charge'.
    The file is read again when it changes. The finance gate is the caller's
    (``InvestigateTools``)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._seen: tuple[Any, ...] | None = None
        self._doc: dict[str, Any] | None = None

    @classmethod
    def under(cls, state_dir: Path | str) -> "ActualsFile":
        return cls(Path(state_dir) / ARTIFACT_DIR / ARTIFACT_FILE)

    def _load(self) -> dict[str, Any]:
        try:
            st = self.path.stat()
        except OSError:
            return {"unavailable": "LANDED_ACTUALS_NOT_CAPTURED"}
        mark = (st.st_mtime_ns, st.st_size, st.st_ino)
        if mark != self._seen:
            try:
                raw = self.path.read_bytes()
                doc = json.loads(raw.decode("utf-8"))
            except (OSError, ValueError):
                raw, doc = b"", None
            ok = isinstance(doc, dict) and doc.get("schema") == ARTIFACT_SCHEMA \
                and isinstance(doc.get("records"), dict) and isinstance(doc.get("known_pos"), list)
            self._doc = {"doc": doc, "known": set(doc["known_pos"]),
                         "digest": hashlib.sha256(raw).hexdigest()[:16]} if ok else None
            self._seen = mark
        return self._doc or {"unavailable": "LANDED_ACTUALS_UNREADABLE"}

    def __call__(self, keys: Sequence[str]) -> dict[str, Any]:
        got = self._load()
        if "unavailable" in got:
            return dict(got)
        doc = got["doc"]
        return {"records": {k: doc["records"][k] for k in keys if k in doc["records"]
                            and isinstance(doc["records"][k], dict)},
                "known": [k for k in keys if k in got["known"]],
                "default_estimates": doc.get("history_default")
                if isinstance(doc.get("history_default"), dict) else {},
                "digest": got["digest"], "built_at": doc.get("built_at")}
