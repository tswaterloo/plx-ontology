"""How an amount whose currency the document does not state is shown.

A '$'-only amount on a document is read. Its currency is the
document's when the document states it; otherwise the counterparty's Business Central
currency, shown at read time and labelled as coming from the vendor or customer record;
otherwise ``$ (currency not stated)``. Never guessed, never converted, never stored here.

The extractor (``ontology.document_fields``) marks such a value ``currency_basis``
``unstated`` with ``currency`` empty. A value with no basis field, or any other basis,
is shown exactly as before (:func:`is_unstated` is False and callers change nothing).
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

NOT_STATED = "$ (currency not stated)"
UNSTATED = "unstated"
COUNTERPARTY_KINDS = {"Supplier": "vendor", "Vendor": "vendor", "Customer": "customer"}
_CODE = re.compile(r"[A-Z]{3}")


def is_unstated(value: Any) -> bool:
    """Whether ``value`` (a stored or served value entry) is an amount whose currency the
    document leaves unstated: the explicit basis says so and no currency is on it."""
    if not isinstance(value, Mapping) or value.get("currency"):
        return False
    return UNSTATED in (value.get("currency_basis"), value.get("currency_source"))


def counterparty_currency(code: Any, object_type: Any) -> Optional[dict[str, str]]:
    """``{"currency": "USD", "kind": "vendor"}`` for a Supplier/Vendor/Customer record's
    three-letter currency code, else None (any other type or code states nothing)."""
    kind = COUNTERPARTY_KINDS.get(object_type) if isinstance(object_type, str) else None
    text = code.strip().upper() if isinstance(code, str) else ""
    return {"currency": text, "kind": kind} if kind and _CODE.fullmatch(text) else None


def currency_words(counterparty: Optional[Mapping[str, Any]] = None) -> str:
    """What stands where the currency code would: the counterparty's code labelled with
    its source, else :data:`NOT_STATED`."""
    if counterparty and counterparty.get("kind") in ("vendor", "customer") \
            and _CODE.fullmatch(str(counterparty.get("currency") or "")):
        return f"{counterparty['currency']} (from {counterparty['kind']} record)"
    return NOT_STATED


def amount_words(amount_text: str, counterparty: Optional[Mapping[str, Any]] = None) -> str:
    """An unstated-currency amount as ``617.61 $ (currency not stated)``."""
    return f"{amount_text} {currency_words(counterparty)}"
