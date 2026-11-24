"""Who a payable is owed to, by how Sage books the vendor: ``payee_class`` of
``supplier_balances`` and ``supplier_invoices`` (11-22: "do we owe suppliers money that's
overdue" summed payroll remittances, card issuers and tax bodies with trade suppliers, and
the vendor group, DEF / USD, cannot tell them apart).

Read from the system of record, never from a name: the vendor's default GL account
(``RelSageApVendor.glaccnt``, the account its invoices post to), that account's group
(``RelSageGlAccount.acctgrpcod``) and the group's category (``RelSageGlAcgrp.grpcod``: Sage's
fixed list of statement sections). The class by category:

* ``trade``: inventory and cost of sales (goods we buy to make and sell: raw materials,
  packaging, freight in, outside testing of product);
* ``expense``: operating expenses (services, freight out, rent, IT, consulting);
* ``capital``: fixed assets;
* ``tax_payroll``: other current liabilities and income tax (source deductions, sales
  tax, workplace insurance);
* ``finance``: cash, payables, loans and shareholder debt (card issuers, lenders);
* ``other``: any other category; ``unknown``: the vendor has no default account, or one
  Sage's chart does not hold (card issuers often have none: their statements post
  everywhere).

Stdlib only; the views read the records (``business_views_supply``)."""
from __future__ import annotations

from typing import Any, Mapping

CATEGORY_CLASS = {"30": "trade", "150": "trade",
                  "170": "expense", "180": "expense", "190": "expense", "200": "expense",
                  "50": "capital", "90": "tax_payroll", "210": "tax_payroll",
                  "10": "finance", "80": "finance", "100": "finance", "110": "finance"}
CLASSES = ("trade", "expense", "capital", "tax_payroll", "finance", "other", "unknown")
ACCOUNT_PROPS = ("acctfmttd", "acctid", "acctdesc", "acctgrpcod")
GROUP_PROPS = ("acctgrpcod", "acctgrpdes", "grpcod")
DOC = ("who the payable is owed to, by the GL account Sage posts the vendor's invoices to "
       "(the vendor's default account's group category): trade (inventory and cost of "
       "sales: raw materials, packaging, freight in, product testing), expense (services, "
       "freight out, rent, IT, consulting), capital (fixed assets), tax_payroll (source "
       "deductions, sales tax, workplace insurance, income tax), finance (cards, lenders), "
       "other, or unknown (no default account in Sage, e.g. card issuers). Trade payables "
       "= payee_class 'trade'")


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
