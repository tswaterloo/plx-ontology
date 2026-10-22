"""Independent check of a purchase order's landed-cost answer against pinned originals.

The live check ``case.packaging_landed_cost`` opens a purchase order on the general path and
runs ``plx.investigate`` ``landed_cost``; this module grades that answer without sharing the
engine's reading. Inputs are injected by the harness (``scripts/eval_packaging_oracle_check.py
landed-cost``): the answer (entries, gaps and conflicts of every page), a reader of the bytes
a citation names (``read_cited``: the cited file, or the cited member of a captured
container; None when it cannot be read) and the case-originals registry
(``plx-case-originals/v1``, loaded by ``scripts/case_originals.py``) with the pinned rows the
case must cite: its proforma invoices and its forwarder quotes. Never a name, a value or a
path of a real original is written here; the pins are the registry's row ids.

The checks:

* every cited value of a supplier confirmation (its amount, unit price and quantity) and of
  an assessed freight quote resolves to bytes whose SHA-256 is a pinned row of the registry
  (``cited_bytes_not_pinned`` when the bytes differ from every pin, ``cited_bytes_unreadable``
  when none are read), and each cited number appears among the numbers of those bytes' text
  (``text_of``, injected: ``cited_value_not_in_bytes``, ``cited_text_unreadable`` when no
  text is read), so a value is checked against the original, not only its provenance;
* each expected quote is cited by a quote assessment, and that assessment carries a scope or
  exclusion code (``SCOPE_CODES``) among its gaps;
* the goods taken from the confirmations equal one confirmed amount only while every
  confirmation the answer cites states a readable amount and their amounts, unit prices and
  quantities all agree; differing amounts, unit prices or quantities (unit price x
  quantity) are unknown goods with a ``PI_VERSION_UNRESOLVED`` conflict listing every
  differing amount, and a confirmation whose amount is unread is
  unknown goods with a ``pi_amount_unread`` gap (never a picked version); unknown goods
  with neither are ``missing`` with the typed code of a gap on the goods that the goods
  entry's steps name (e.g. ``confirmation_currency_differs``) and the value a person must
  supply (``GOODS_NEEDS``, ``pin`` null), else ``goods_unknown_without_version_conflict``;
* the total is unknown while the answer carries a gap on the duty of its lines
  (``duty_rate_source_absent``, or any gap whose subject is the duty of a line).

An expected pin that is not a pinned row of the registry is ``expected_pin_not_pinned``.

An expected pin no answer entry cites is ``missing`` with its typed code
(``QUOTE_NOT_LINKED`` or ``PI_NOT_LINKED``) and the value a person must supply
(``NEEDS``), never a pass. A proforma pin uncited while the reader holds no finance marking
(``held_markings``, the host config's grant to the check's principals) is not a link to ask
anyone for: it is the reason ``finance_marking_not_granted`` (the check's own grant).
``status``: ``agree`` (no reason, nothing missing), ``missing``
(only expected pins uncited) or ``disagree`` (``reasons``). Output holds pins, codes,
counts and booleans only.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import re
from typing import Any, Callable, Collection, Mapping, Sequence

SCHEMA = "plx-landed-cost-oracle/v1"
PI_UNRESOLVED = "PI_VERSION_UNRESOLVED"
PI_UNREAD = "pi_amount_unread"
FINANCE = "finance"  # the marking money read from a document carries
FINANCE_NOT_GRANTED = "finance_marking_not_granted"
DUTY_ABSENT = "duty_rate_source_absent"
GOODS_SUBJECT = "goods of "  # a gap on the goods of the answer's lines (or "goods")
DUTY_SUBJECT = "duty of "  # the subject of a gap on the duty of the answer's lines
# The quote assessment's codes that say what the quote covers or leaves out.
SCOPE_CODES = frozenset({"freight_scope_partial", "freight_excludes_surcharges",
                         "freight_label_contradicts_scope", "freight_scope_contradicts",
                         "freight_exclusions_unread"})
QUOTE_NOT_LINKED = "quote_not_linked_to_po"
PI_NOT_LINKED = "pi_not_linked_to_po"
NEEDS = {QUOTE_NOT_LINKED: "the purchase order the forwarder quote prices (a person links "
                           "the quote to its purchase order)",
         PI_NOT_LINKED: "the purchase order the proforma invoice confirms (a person links "
                        "the proforma invoice to its purchase order)"}
# What a person supplies when the engine leaves the goods unknown for a typed reason
# (``missing`` beside the pins): by code, else generic (None).
GOODS_NEEDS = {
    "confirmation_currency_differs": "the goods amount in the purchase order's currency (a "
                                     "person states it, or the rate converting the supplier "
                                     "confirmation's currency)",
    "confirmation_not_per_line": "the purchase order line the supplier confirmation's amount "
                                 "is for (a person allocates the confirmed amount)",
    None: "the goods value the engine left unknown ({code}; a person supplies it)"}
_PAGE = re.compile(r"&page=[0-9]+$")
_NUMBER = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")
# A comma-decimal amount (``37.600,00``, ``2,50``): dots group thousands, the comma is the
# decimal mark, two decimals (so ``1,000`` stays a thousands-comma number).
_COMMA_DECIMAL = re.compile(r"(?<![0-9.,])[0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2}(?![0-9])")
# A typed code an engine gap carries (``reason``): lower-case, digits and underscores.
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def pinned(registry: Mapping[str, Any]) -> dict[str, frozenset[str]]:
    """SHA-256 -> the ids of the pinned rows of the registry with those bytes."""
    out: dict[str, set[str]] = {}
    for row in registry.get("rows") or []:
        expected = row.get("expected") if isinstance(row.get("expected"), Mapping) else {}
        digest = expected.get("sha256")
        if row.get("status") == "pinned" and isinstance(digest, str):
            out.setdefault(digest.lower(), set()).add(str(row.get("id")))
    return {digest: frozenset(ids) for digest, ids in out.items()}


def _cited(item: Mapping[str, Any]) -> tuple[Any, Any]:
    """(l1_handle, field_path) of an answer entry's or a conflict value's citation."""
    citation = item.get("citation") if isinstance(item.get("citation"), Mapping) else {}
    return citation.get("l1_handle"), citation.get("field_path")


def _decimal(value: Any) -> Decimal | None:
    try:
        found = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return found if found.is_finite() else None


def _numbers(text: str) -> frozenset[Decimal]:
    """Every number of ``text`` (thousands commas dropped)."""
    out = {_decimal(m.group(0).replace(",", "")) for m in _NUMBER.finditer(text)}
    out |= {_decimal(m.group(0).replace(".", "").replace(",", "."))
            for m in _COMMA_DECIMAL.finditer(text)}
    return frozenset(n for n in out if n is not None)


def check(result: Mapping[str, Any], read_cited: Callable[[str], bytes | None],
          registry: Mapping[str, Any], *, quotes: Sequence[str], proformas: Sequence[str],
          text_of: Callable[[bytes], str | None] | None = None,
          held_markings: Collection[str] | None = None,
          total: str = "landed_cost") -> dict[str, Any]:
    """The oracle's verdict on one landed-cost answer (module docstring). ``text_of``: the
    text of a cited original's bytes (None: unreadable; no reader: every cited number is
    unchecked, ``cited_text_unreadable``); ``held_markings``: the markings the reader holds
    (None: not known)."""
    pins = pinned(registry)
    known = {pin for ids in pins.values() for pin in ids}
    reasons: list[str] = ["expected_pin_not_pinned" for pin in [*quotes, *proformas]
                          if pin not in known][:1]
    entries = {e.get("name"): e for e in result.get("answer") or []
               if isinstance(e, Mapping)}
    gaps = [g for g in result.get("gaps") or [] if isinstance(g, Mapping)]
    conflicts = [c for c in result.get("conflicts") or [] if isinstance(c, Mapping)]
    cited: dict[str, frozenset[str]] = {}
    numbers: dict[str, frozenset[Decimal] | None] = {}

    def pins_of(entry: Mapping[str, Any]) -> frozenset[str]:
        """The registry rows the entry's cited bytes are (none: with its reason); a cited
        number must appear in those bytes' text."""
        handle = (entry.get("citation") or {}).get("l1_handle")
        if not isinstance(handle, str):
            reasons.append("cited_value_without_citation")
            return frozenset()
        handle = _PAGE.sub("", handle)
        if handle not in cited:
            data = read_cited(handle)
            cited[handle] = frozenset() if data is None else pins.get(
                hashlib.sha256(data).hexdigest(), frozenset())
            text = None if data is None or text_of is None else text_of(data)
            numbers[handle] = None if text is None else _numbers(text)
            if data is None:
                reasons.append("cited_bytes_unreadable")
            elif not cited[handle]:
                reasons.append("cited_bytes_not_pinned")
        value = _decimal(entry.get("value")) if not isinstance(entry.get("value"), bool) \
            else None
        if value is not None and cited[handle]:
            if numbers[handle] is None:
                reasons.append("cited_text_unreadable")
            elif value not in numbers[handle]:
                reasons.append("cited_value_not_in_bytes")
        return cited[handle]

    confirmation = [e for e in entries.values() if e.get("kind") == "property"
                    and e.get("object_type") == "SupplierConfirmation"
                    and e.get("property") in ("amount", "unit_price", "quantity")]
    amounts = [e for e in confirmation if e.get("property") == "amount"]
    confirmed: dict[str, Decimal] = {}
    confirmed_pins: set[str] = set()
    unit_prices: set[Decimal] = set()
    quantities: set[Decimal] = set()
    for entry in confirmation:   # every cited figure, its bytes and its number checked
        rows, value = pins_of(entry), _decimal(entry.get("value"))
        if entry.get("property") == "amount":   # a proforma is linked by its amount
            confirmed_pins |= rows
        if rows and value is not None and entry.get("property") == "amount":
            confirmed.setdefault(entry["name"], value)
        elif rows and value is not None and entry.get("property") == "unit_price":
            unit_prices.add(value)
        elif rows and value is not None and entry.get("property") == "quantity":
            quantities.add(value)
    # A confirmation the answer cites whose amount is not a readable pinned number.
    unread = sorted({str(e.get("object_id")) for e in confirmation}
                    - {str(e.get("object_id")) for e in amounts if e["name"] in confirmed})

    assessed: list[dict[str, Any]] = []
    for entry in entries.values():
        name = str(entry.get("name"))
        if entry.get("kind") != "computed" or "_freight_quote_" not in name:
            continue
        subject = "freight quote " + name.rsplit("_", 1)[1]
        rows = {pin for n in entry.get("inputs") or [] if n in entries
                for pin in pins_of(entries[n])}
        codes = {g.get("reason") for g in gaps if g.get("subject") == subject}
        assessed.append({"pins": rows, "codes": sorted(codes & SCOPE_CODES)})

    missing: list[dict[str, str]] = []
    quote_report = []
    for pin in quotes:
        found = [q for q in assessed if pin in q["pins"]]
        codes = sorted({c for q in found for c in q["codes"]})
        if not found:
            missing.append({"code": QUOTE_NOT_LINKED, "pin": pin, "needs": NEEDS[QUOTE_NOT_LINKED]})
        elif not codes:
            reasons.append("quote_scope_codes_absent")
        quote_report.append({"pin": pin, "assessed": bool(found), "scope_codes": codes})
    finance_held = None if held_markings is None else FINANCE in set(held_markings)
    for pin in proformas:
        if pin in confirmed_pins:
            continue
        if finance_held is False:   # the check's own grant, not a link a person makes
            reasons.append(FINANCE_NOT_GRANTED)
        else:
            missing.append({"code": PI_NOT_LINKED, "pin": pin, "needs": NEEDS[PI_NOT_LINKED]})

    versions = set(confirmed.values())
    goods = next((e for e in entries.values() if e.get("kind") == "computed"
                  and str(e.get("name")).endswith("_goods")), None)
    unresolved = [c for c in conflicts if str(c.get("subject", "")).startswith(PI_UNRESOLVED)]
    # The answer's conflicts carry the cited values they disagree on (the contract's
    # ``values``), never the engine's input names: an amount is listed when its citation is.
    named = {_cited(v) for c in unresolved for v in c.get("values") or []
             if isinstance(v, Mapping)}
    listed = {confirmed[e["name"]] for e in amounts
              if e["name"] in confirmed and _cited(e) in named}
    # Versions: amounts, unit prices or quantities (so unit price x quantity) that differ.
    differ = len(versions) > 1 or len(unit_prices) > 1 or len(quantities) > 1
    unread_gap = any(g.get("reason") == PI_UNREAD for g in gaps)
    goods_state = None
    if goods is None:
        if versions:
            reasons.append("goods_not_from_confirmation")
    elif goods.get("value") is not None:
        value = _decimal(goods.get("value"))
        goods_state = "one_amount"
        if unresolved or differ:
            reasons.append("goods_picked_a_version")
        elif unread:   # another version's amount is unknown: never pick the one read
            reasons.append("goods_picked_beside_an_unread_amount")
        elif value is None or value not in versions:
            reasons.append("goods_not_a_confirmed_amount")
    else:
        goods_state = "unresolved"
        if differ and not unresolved:
            reasons.append("goods_unknown_without_version_conflict")
        elif differ and len(versions) > 1 and listed != versions:
            reasons.append("version_conflict_omits_an_amount")
        if unread and not unread_gap:
            reasons.append("unread_amount_without_gap")
        # Neither versions nor an unread amount: the goods entry must name the typed engine
        # code of a gap on the goods (e.g. CONFIRMATION_CURRENCY_DIFFERS); that blocks the
        # check on the value a person supplies. With none, unknown goods are a failure.
        named_steps = {str(s).split(":", 1)[0].strip().lower()
                       for s in goods.get("steps") or [] if isinstance(s, str)}
        typed = sorted({str(g.get("reason")) for g in gaps
                        if (str(g.get("subject") or "") == "goods"
                            or str(g.get("subject") or "").startswith(GOODS_SUBJECT))
                        and _CODE.fullmatch(str(g.get("reason") or ""))
                        and str(g.get("reason")) in named_steps
                        and str(g.get("reason")) not in (PI_UNREAD, PI_UNRESOLVED.lower())})
        if not differ and not unread and not typed:
            reasons.append("goods_unknown_without_version_conflict")
        elif not differ and not unread:
            missing += [{"code": code, "pin": None,
                         "needs": GOODS_NEEDS.get(code, GOODS_NEEDS[None]).format(code=code)}
                        for code in typed]

    # The duty of a line is unknown: no duty-rate source, or lines too incomplete to carry
    # one (either way no duty is sourced, so no total may be stated).
    duty_gap = any(g.get("reason") == DUTY_ABSENT
                   or str(g.get("subject") or "").startswith(DUTY_SUBJECT) for g in gaps)
    total_entry = entries.get(total)
    total_unknown = total_entry is not None and total_entry.get("value") is None
    if total_entry is None:
        reasons.append("total_absent")
    elif duty_gap and not total_unknown:
        reasons.append("total_known_without_duty_source")

    reasons = sorted(set(reasons))
    return {"schema": SCHEMA,
            "status": "disagree" if reasons else "missing" if missing else "agree",
            "reasons": reasons, "missing": missing,
            "missing_codes": sorted({m["code"] for m in missing}),
            "cited_documents": len(cited),
            "cited_pinned": sum(1 for rows in cited.values() if rows),
            "quotes": quote_report, "confirmation_amounts": len(amounts),
            "confirmed_versions": len(versions), "unread_amounts": len(unread),
            "finance_granted": finance_held, "goods": goods_state,
            "version_conflict": bool(unresolved), "duty_source_gap": duty_gap,
            "total_unknown": total_unknown}
