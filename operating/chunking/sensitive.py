"""Personal financial content, by what a text says (never by a file, site or folder name).

A document that is a card or bank statement, or that carries a payment card or bank
account number, is ``finance`` and ``pii``: its chunks carry both markings, so search
shows them only to a reader holding them, as it does for other marked records.

* **Statement**: at least ``STATEMENT_MIN`` distinct phrases only a card or bank
  statement prints (minimum payment, credit limit, previous balance, cash advance, ...).
  An invoice's "balance due" or "payment terms" is not one of them.
* **Account number**: a payment card number written as cards print it (4-4-4-4 or
  4-6-5 digit groups, passing the Luhn check) within ``NEAR`` characters of a card word
  (card, Visa, Mastercard, Amex, ...); a masked number showing its last four digits is
  not a number. A bare long digit run (an order, lot or tracking number) is not one: one
  in ten passes Luhn. A company's bank details printed for payment on its invoices or
  certificates (an IBAN, a transit and account) are business payee details, not marked.
"""

from __future__ import annotations

import re

MARKS = ("finance", "pii")
STATEMENT_MIN = 3
_STATEMENT = tuple(re.compile(p, re.I) for p in (
    r"\bminimum (?:amount|payment)(?: due)?\b",
    r"\bcredit limit\b",
    r"\bavailable credit\b",
    r"\bprevious (?:statement )?balance\b",
    r"\bnew balance\b",
    r"\bstatement (?:period|date)\b",
    r"\bcash advances?\b",
    r"\b(?:annual )?interest rate\b",
    r"\binterest charged\b",
    r"\bpurchases and (?:other )?(?:charges|debits)\b",
    r"\b(?:opening|closing) balance\b",
    r"\bpoints (?:earned|balance)\b",
))
_CARD = re.compile(r"(?<![\d-])(\d{4}([ -])\d{4}\2\d{4}\2\d{4}|\d{4}([ -])\d{6}\3\d{5})(?![\d-])")
_CARD_WORD = re.compile(r"\b(?:card|visa|master ?card|amex|american express|discover|"
                        r"credit|debit|cardholder)\b", re.I)
NEAR = 60


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card_number(text: str) -> bool:
    for found in _CARD.finditer(text):
        digits = re.sub(r"\D", "", found.group(1))
        if digits[0] not in "23456" or len(set(digits)) <= 2 or not _luhn(digits):
            continue
        around = text[max(0, found.start() - NEAR):found.end() + NEAR]
        if _CARD_WORD.search(around):
            return True
    return False


def personal_financial(text: str) -> str | None:
    """Why ``text`` is personal financial content (``statement``, ``card_number``), or
    None."""
    if sum(1 for p in _STATEMENT if p.search(text)) >= STATEMENT_MIN:
        return "statement"
    if _card_number(text):
        return "card_number"
    return None
