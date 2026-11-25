"""Who a payable is owed to, by how Sage books the vendor: ``payee_class`` of
``supplier_balances`` and ``supplier_invoices`` (11-22: "do we owe suppliers money that's
overdue" summed payroll remittances, card issuers and tax bodies with trade suppliers, and
the vendor group, DEF / USD, cannot tell them apart).

Read from the system of record, never from a name: the vendor's default GL account
(``RelSageApVendor.glaccnt``, the account its invoices post to), or when that is blank,
the vendor's Sage AP distribution set (``RelSageApVendor.distsetid`` -> ``RelSageApDsd``
lines' ``idglacct``); each account resolves through its group (``RelSageGlAccount.acctgrpcod``)
and the group's category (``RelSageGlAcgrp.grpcod``: Sage's fixed list of statement sections).
The class by category:

* ``trade``: inventory and cost of sales (goods we buy to make and sell: raw materials,
  packaging, freight in, outside testing of product);
* ``expense``: operating expenses (services, freight out, rent, IT, consulting);
* ``capital``: fixed assets;
* ``tax_payroll``: other current liabilities and income tax (source deductions, sales
  tax, workplace insurance);
* ``finance``: cash, payables, loans and shareholder debt (card issuers, lenders);
* ``other``: any other category; ``unknown``: the vendor has neither a default account nor
  an unambiguous distribution set in Sage's chart.

Stdlib only; the views read the records (``business_views_supply``)."""
from __future__ import annotations

from collections import Counter
from typing import Any, Mapping

CATEGORY_CLASS = {"30": "trade", "150": "trade",
                  "170": "expense", "180": "expense", "190": "expense", "200": "expense",
                  "50": "capital", "90": "tax_payroll", "210": "tax_payroll",
                  "10": "finance", "80": "finance", "100": "finance", "110": "finance"}
CLASSES = ("trade", "expense", "capital", "tax_payroll", "finance", "other", "unknown")
ACCOUNT_PROPS = ("acctfmttd", "acctid", "acctdesc", "acctgrpcod")
GROUP_PROPS = ("acctgrpcod", "acctgrpdes", "grpcod")
DISTSET_PROPS = ("distset", "idglacct")
DOC = ("who the payable is owed to, by the GL account Sage posts the vendor's invoices to "
       "(the vendor's default account, or its Sage AP distribution set when no single default "
       "account is set, mapped through the GL account's group category): trade (inventory and "
       "cost of sales: raw materials, packaging, freight in, product testing), expense (services, "
       "freight out, rent, IT, consulting), capital (fixed assets), tax_payroll (source "
       "deductions, sales tax, workplace insurance, income tax), finance (cards, lenders), "
       "other, or unknown (no default account or unambiguous distribution set in Sage). Trade "
       "payables = payee_class 'trade'")


def _key(value: Any) -> str:
    return "".join(str(value).split()).replace("-", "").upper() if value is not None else ""


def chart(accounts: Mapping[str, Any], groups: Mapping[str, Any]) -> dict[str, tuple]:
    """Account key (formatted or plain id, dashes and blanks out) -> (account record,
    group record or None) of the GL chart's records (``Rec``: ``.cell(prop)``)."""
    by_group = {}
    for rec in groups.values():
        code = _value(rec, "acctgrpcod")
        if code:
            by_group[code] = rec
    out: dict[str, tuple] = {}
    for rec in accounts.values():
        grp = by_group.get(_value(rec, "acctgrpcod"))
        for prop in ("acctfmttd", "acctid"):
            key = _key(_value(rec, prop))
            if key:
                out.setdefault(key, (rec, grp))
    return out


def distsets(lines: Mapping[str, Any],
             accounts: Mapping[str, tuple]) -> dict[str, tuple[str, tuple[Any, ...]]]:
    """Distribution set code (upper, trimmed) -> (class, referenced records) from Sage AP
    distribution-set lines (``RelSageApDsd``) mapped through the GL chart.

    A distribution set containing a ``finance`` clearing/cash account (card issuers'
    distribution sets) classifies as ``finance``; otherwise a unique strict plurality of
    mapped line classes decides, and a tie fails closed as ``unknown``."""
    grouped: dict[str, list[Any]] = {}
    for rec in lines.values():
        code = _value(rec, "distset").upper()
        if code:
            grouped.setdefault(code, []).append(rec)
    out: dict[str, tuple[str, tuple[Any, ...]]] = {}
    for code, recs in grouped.items():
        counts: Counter[str] = Counter()
        refs: dict[str, Any] = {}
        for rec in recs:
            refs.setdefault(rec.object_id, rec)
            found = accounts.get(_key(_value(rec, "idglacct")))
            if found is None:
                continue
            account, group = found
            refs.setdefault(account.object_id, account)
            if group is None:
                continue
            refs.setdefault(group.object_id, group)
            counts[CATEGORY_CLASS.get(_value(group, "grpcod"), "other")] += 1
        if not counts:
            cls = "unknown"
        elif "finance" in counts:
            cls = "finance"
        else:
            top = counts.most_common(2)
            cls = top[0][0] if len(top) == 1 or top[0][1] > top[1][1] else "unknown"
        out[code] = (cls, tuple(refs.values()))
    return out


def _value(rec: Any, prop: str) -> str:
    cell = rec.cell(prop) if rec is not None else None
    return str(cell.value).strip() if cell is not None and cell.value is not None else ""


def classify(vendor: Any, accounts: Mapping[str, tuple]) -> tuple[str, Any, Any]:
    """(class, account record or None, group record or None) of an AP vendor record."""
    found = accounts.get(_key(_value(vendor, "glaccnt")))
    if found is None:
        return "unknown", None, None
    account, group = found
    if group is None:
        return "unknown", account, None
    return CATEGORY_CLASS.get(_value(group, "grpcod"), "other"), account, group


def classify_vendor(vendor: Any, accounts: Mapping[str, tuple],
                    dist_sets: Mapping[str, tuple[str, tuple[Any, ...]]] | None = None
                    ) -> tuple[str, tuple[Any, ...]]:
    """(class, referenced records) of an AP vendor: its default GL account first, else its
    Sage AP distribution set (``distsetid``) when ``dist_sets`` is given."""
    cls, account, group = classify(vendor, accounts)
    if cls != "unknown":
        return cls, tuple(r for r in (account, group) if r is not None)
    if dist_sets:
        found = dist_sets.get(_value(vendor, "distsetid").upper())
        if found is not None:
            return found
    return "unknown", tuple(r for r in (account, group) if r is not None)
