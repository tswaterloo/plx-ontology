"""Personal financial and HR-confidential content, by what a text says and by the
classification labels its source records (never by a file name alone).

A document that is a card or bank statement, carries a payment card or social insurance
number, is a pay stub or payroll statement, an offer of employment or a termination
letter, or sits under a payroll or HR confidentiality label, is ``finance`` and ``pii``:
its search chunks carry both markings (written at chunking, or in place by
``operating.chunking.remark``, which updates the search store only). Every read path that
can return a document's identity or text (search, ``plx.find``, ``plx.object``,
``plx.read``, ``plx.around``, ``plx.timeline``, ``plx.documents``, record pages and the
attachments listed inside a message) asks ``operating.search.document_marks`` and shows it
only to a reader holding both, withheld from any other as an unreadable object is.

* **Statement**: at least ``STATEMENT_MIN`` distinct phrases only a card or bank
  statement prints (minimum payment, credit limit, previous balance, cash advance, ...).
  An invoice's "balance due" or "payment terms" is not one of them.
* **Account number**: a payment card number written as cards print it (4-4-4-4 or
  4-6-5 digit groups, passing the Luhn check) within ``NEAR`` characters of a card word
  (card, Visa, Mastercard, Amex, ...); a masked number showing its last four digits is
  not a number. A bare long digit run (an order, lot or tracking number) is not one: one
  in ten passes Luhn. A company's bank details printed for payment on its invoices or
  certificates (an IBAN, a transit and account) are business payee details, not marked.
* **Payroll** (``payroll``): at least ``PAY_MIN`` distinct pay-statement phrases (gross or
  net pay, CPP, EI, income tax, YTD, pay period, total deductions, SIN, T4, ...), one of
  them a core one only payroll prints (gross/net pay, CPP, EI, SIN, T4, pay stub). An
  invoice's "net 30", "total" or "tax" is none of them. Fail closed: a title naming pay
  stubs, payroll, T4s, offers or terminations plus one core phrase is enough.
* **SIN** (``sin_number``): a 3-3-3 number passing the Luhn check within ``NEAR``
  characters of "SIN" or "social insurance".
* **Offer letter** (``offer_letter``): an offer-of-employment phrase plus a compensation
  term (salary, wage, hourly rate) and one more employment term (start date, probation,
  benefits, the position).
* **Termination letter** (``termination_letter``): a termination-of-employment phrase plus
  one supporting term (final pay, record of employment, in lieu of notice, severance, ...).
* **Label** (``label``): a captured classification label (a sensitivity or retention label,
  or the site, library or folder a source files the document under) naming confidential
  payroll, HR, personnel or compensation information ("ConfidentialPayrollInformation").
  Callers pass labels, never the file name.
"""

from __future__ import annotations

import re
from typing import Iterable

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


PAY_MIN = 3
_I = re.I
_PAY_CORE = tuple(re.compile(p, f) for p, f in (
    (r"\bgross (?:pay|earnings|wages)\b", _I),
    (r"\bnet (?:pay|earnings)\b", _I),
    (r"\b(?:CPP|C\.P\.P\.?|QPP)\b|\bcanada pension plan\b", 0),
    (r"\bEI\b|\b[Ee]mployment [Ii]nsurance\b", 0),
    (r"\bSIN\b|\b[Ss]ocial [Ii]nsurance (?:[Nn]umber|[Nn]o\.?)", 0),
    (r"\bT4A?\b", 0),
    (r"\b(?:pay ?stub|pay ?slip|pay statement|earnings statement|statement of earnings|"
     r"pay advice|direct deposit advice)s?\b", _I),
))
_PAY_MORE = tuple(re.compile(p, _I) for p in (
    r"\b(?:YTD|year[- ]to[- ]date)\b",
    r"\bpay (?:period|date)\b",
    r"\b(?:federal|provincial|income) (?:income )?tax\b",
    r"\btotal deductions\b",
    r"\b(?:vacation|holiday|stat(?:utory)? holiday) pay\b",
    r"\b(?:regular|overtime) (?:pay|hours|earnings)\b",
    r"\b(?:employee|employer) (?:no\.?|number|id|contributions?)\b",
    r"\bpayroll\b",
))
_SIN = re.compile(r"(?<![\d-])(\d{3}([ -])\d{3}\2\d{3})(?![\d-])")
_SIN_WORD = re.compile(r"\bSIN\b|\b[Ss]ocial [Ii]nsurance\b")
_OFFER = tuple(re.compile(p, _I) for p in (
    r"\boffer of employment\b",
    r"\bemployment offer\b",
    r"\b(?:letter of offer|offer letter)\b",
    r"\boffer you (?:the|a|an) (?:[\w-]+ ){0,3}(?:position|role|job)\b",
    r"\baccept(?:ance of)? this offer of\b",
))
_COMPENSATION = re.compile(r"\b(?:(?:annual|base|starting|gross) (?:salary|wage|compensation)|"
                           r"salary of|wage of|hourly (?:rate|wage)|per (?:hour|annum))\b", _I)
_EMPLOYMENT = tuple(re.compile(p, _I) for p in (
    r"\bstart(?:ing)? date\b", r"\bprobation(?:ary)? period\b",
    r"\b(?:benefits|vacation)\b", r"\b(?:position|role) of\b", r"\breport(?:ing)? to\b",
))
_TERMINATION = tuple(re.compile(p, _I) for p in (
    r"\btermination of (?:your )?employment\b",
    r"\byour employment (?:with [\w&.,' -]{1,60} )?(?:is|will be|has been) terminated\b",
    r"\bnotice of (?:termination|dismissal)\b",
    r"\b(?:termination|dismissal|lay-?off) (?:letter|notice)\b",
    r"\bletter of termination\b",
))
_TERM_SUPPORT = tuple(re.compile(p, f) for p, f in (
    (r"\bfinal (?:pay|paycheque|paycheck|pay cheque)\b", _I),
    (r"\brecord of employment\b|\bROE\b", 0),
    (r"\bin lieu of notice\b", _I),
    (r"\blast day of (?:work|employment)\b", _I),
    (r"\bseverance\b", _I),
    (r"\boutstanding (?:wages|vacation pay)\b", _I),
    (r"\bbenefits? (?:coverage )?(?:will )?(?:end|cease|continue)\b", _I),
    (r"\bemployment standards\b", _I),
    (r"\breturn (?:all )?(?:company|our) property\b", _I),
))
_TITLE = re.compile(r"\b(?:pay ?stubs?|pay ?slips?|pay statements?|payroll|T4A?s?|"
                    r"offer letters?|letters? of offer|termination|severance)\b", _I)
_LABEL = re.compile(
    r"\b(?:confidential|restricted|private|sensitive|secret)\s+(?:payroll|hr|human resources|"
    r"personnel|employee|employment|compensation|salary)\b|"
    r"\b(?:payroll|hr|human resources|personnel|compensation|salary)\s+(?:information\s+)?"
    r"(?:confidential|restricted|private|sensitive)\b", _I)


def _words(label: str) -> str:
    """A label as words: camel case and punctuation split (``ConfidentialPayrollInformation``
    reads "Confidential Payroll Information"; ``HR_Confidential`` "HR Confidential")."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", label)
    return re.sub(r"[\W_]+", " ", spaced)


def labelled(labels: "Iterable[str | None]") -> bool:
    """Whether any captured label names confidential payroll or HR information."""
    return any(isinstance(label, str) and _LABEL.search(_words(label)) for label in labels)


def _sin_number(text: str) -> bool:
    for found in _SIN.finditer(text):
        digits = re.sub(r"\D", "", found.group(1))
        if digits[0] in "08" or len(set(digits)) <= 2 or not _luhn(digits):
            continue
        around = text[max(0, found.start() - NEAR):found.end() + NEAR]
        if _SIN_WORD.search(around):
            return True
    return False


# A time sheet: one person's hours over a pay period, kept by payroll and HR. Named one
# (its title or its own heading) and carrying hours or the person-and-period frame, or a
# sheet whose columns pair hours with a rate or pay for an employee or a period.
_TIME_NAME = re.compile(r"\b(?:time ?sheets?|time ?cards?|hours worked)\b", _I)
_TIME_HOURS = re.compile(r"\b(?:total|regular|reg\.?|overtime|o/?t|daily|weekly) "
                         r"(?:hours|hrs)\b|\b(?:hours|hrs) (?:worked|total)\b", _I)
_TIME_FRAME = re.compile(r"\bemployee\b|\b(?:pay|payroll) period\b|"
                         r"\b(?:week|period) end(?:ing|ed)\b|\bsupervisor\b", _I)
_PAY_COLUMNS = re.compile(r"^[^\n]*\b(?:hours|hrs)\b[^\n]*\b(?:rate|wages?|pay)\b[^\n]*$|"
                          r"^[^\n]*\b(?:rate|wages?|pay)\b[^\n]*\b(?:hours|hrs)\b[^\n]*$",
                          re.I | re.M)


def _time_sheet(text: str, title: str | None) -> bool:
    named = bool(title and _TIME_NAME.search(title)) or bool(_TIME_NAME.search(text[:2000]))
    if named and (_TIME_HOURS.search(text) or _TIME_FRAME.search(text)):
        return True
    return bool(_PAY_COLUMNS.search(text) and _TIME_FRAME.search(text)
                and _TIME_HOURS.search(text))


def _hr(text: str, title: str | None) -> str | None:
    if _time_sheet(text, title):
        return "time_sheet"
    core = sum(1 for p in _PAY_CORE if p.search(text))
    if core:
        more = sum(1 for p in _PAY_MORE if p.search(text))
        titled = bool(title and _TITLE.search(title))
        if core + more >= PAY_MIN or titled:
            return "payroll"
    if _sin_number(text):
        return "sin_number"
    if any(p.search(text) for p in _OFFER) and _COMPENSATION.search(text) \
            and any(p.search(text) for p in _EMPLOYMENT):
        return "offer_letter"
    if any(p.search(text) for p in _TERMINATION) \
            and any(p.search(text) for p in _TERM_SUPPORT):
        return "termination_letter"
    return None


def personal_financial(text: str, *, title: str | None = None,
                       labels: "Iterable[str | None]" = ()) -> str | None:
    """Why ``text`` is personal financial or HR-confidential content (``statement``,
    ``card_number``, ``time_sheet``, ``payroll``, ``sin_number``, ``offer_letter``,
    ``termination_letter``, ``label``), or None. ``title``: the document's name or subject
    (supports a content signal, never enough alone); ``labels``: its captured
    classification labels and the site, library or folder it is filed under."""
    if labels and labelled(labels):
        return "label"
    if not isinstance(text, str) or not text:
        return None
    if sum(1 for p in _STATEMENT if p.search(text)) >= STATEMENT_MIN:
        return "statement"
    if _card_number(text):
        return "card_number"
    return _hr(text, title)


def folders(path: str | None) -> list[str]:
    """The folders of a ``/``-separated path (its last part, the file name, left out)."""
    if not isinstance(path, str) or "/" not in path:
        return []
    return [part for part in path.split("/")[:-1] if part]
