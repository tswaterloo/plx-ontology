"""The ``plx.investigate`` engine of the landed-cost plan (``ontology/plans/landed_cost.json``,
evidence kind ``compute``) and its estimate mode: plan validation (``plan_check``), the run over a
purchase order's cited values (``plan_run``) and the wording of its gaps' draft requests
(``draft_request``).

Every amount comes from the contract (``operating.landed_cost.compute_landed_cost``) and every
Portal rule from the port (``operating.landed_cost_portal``); this module chooses the inputs,
combines line records through ``LandedOutcome`` and words the answer. ``operating.landed_cost``
re-exports its public names, looked up on first use (no import cycle).
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import Any, Callable, Mapping, Sequence

from ontology.core import ValidationError
from ontology.units import currency_code, ISO_4217, unit_token
from operating.answerers import UNDECLARED
from operating.cases import _canon, _dec_str, parse_unit, _sha
from operating.landed_cost import (
    assess_freight_quote, basis_terms_unread, basis_text_count, _components, compute_landed_cost,
    FORWARDER, freight_basis, freight_scope_terms, money_text, po_line_landed, _QUOTE_GAPS,
    _Reader, Resolver, _sum_calc, TOTAL_FREIGHT,
)
from operating.landed_cost_base import COST_TREATMENT, LandedCostError
from operating.landed_cost_portal import PORTAL_QUOTE_DUTY_RATE, PORTAL_SOURCES, _positive
from operating.landed_cost_results import FreightQuoteScope, LandedOutcome, LineSetCompleteness


# ---- plx.investigate engine (evidence kind "compute" of an ontology/plans plan) ------------------
#
# The landed cost of a purchase order from the cited values a plan collected: one
# ``compute_landed_cost`` record per line (goods = the line's cited unit price per its cited
# unit, freight and duty from ``po_line_landed``), and the purchase order's total only when every
# line's record is computed. Nothing is read here but the plan's answer entries and the citations
# they carry (re-read for the investigating actor by ``resolver``); what the ontology does not
# project stays a typed gap naming what was searched. Roles name plan evidence items:
#   currency          the purchase order's currency (every record of it; two values = mixed)
#   unit_price        line unit price (its entry's unit is the price denominator, currency its own)
#   quantity          line ordered quantity with its unit
#   received          line received quantity (the Portal allocates freight by received value)
#   freight_estimate  header shipping cost estimate; duty_estimate: header duty estimate
#   classification    the line item's HS code (reported; no duty rate is projected)
#   receipt_freight / receipt_duty: receipt actuals (resolvePostedLandedAmounts)
#   units_per_box     the line item's units per case (packaging; case dimensions are not served)
#   quote_*           the typed fields of forwarder quotes naming the purchase order (amount: a
#                     stated total; rate: a price per basis unit; valid_until, basis, the
#                     counts basis_count, volume, chargeable weight and pallet count, each
#                     with its unit, includes, excludes, scope), each quote assessed by
#                     ``assess_freight_quote``: a current quote, never a posted actual
# ``item_link`` is the declared link from a line to the item carrying the classification;
# ``line_link`` the declared link from a line to its purchase order (the complete line set);
# ``quote_link`` the declared link from a quote to the purchase order.
#
# A value a person corrected (``plx.review`` ``correct_value``, handed in by ``plx.investigate``
# as the person's value: this engine takes reviewed inputs) is computed from, never re-read
# from its source: its input is derived from the value it corrects and every record the
# correction cites, each resolved for the actor (``correction_sources``, else unknown), and a
# line whose freight share moves with a sibling line's corrected weight names it as an input.
#
# A line sent unpriced ($0.000, a purchase order issued before the vendor priced it) takes its
# goods from the supplier confirmation (proforma invoice) naming the purchase order
# (``confirmation_link``, a lead): its cited amount, when that confirmation is the line's alone
# (a one-line purchase order whose cited quantity it confirms, or a single-item confirmation,
# unit price x quantity = amount, whose quantity only that line orders). Confirmations whose
# amounts, unit prices or quantities differ are versions none of which is chosen (PI_VERSION_UNRESOLVED, a
# conflict naming every one); a linked confirmation with no readable amount blocks the goods
# too (PI_AMOUNT_UNREAD, a gap naming it), so another one's amount is never taken. Units per
# case are read from the item and its other records (``item_twin_link``); two
# that differ are a UNITS_PER_CASE_CONFLICT, never one picked.
#
# Every freight figure is labelled with its basis: a receipt's historical posted actual, the
# purchase order header estimate (ERP-D-015), or a forwarder's current quote. A header estimate
# within ``ESTIMATE_QUOTE_TOLERANCE`` of a cited quote that is not total freight (same currency)
# is not allocated as freight.

QUOTE_ROLES = ("quote_amount", "quote_rate", "quote_valid_until", "quote_basis",
               "quote_basis_count", "quote_volume", "quote_weight", "quote_pallets",
               "quote_includes", "quote_excludes", "quote_scope")
CONTAINER_UNITS = frozenset(("container", "containers", "cntr", "cntrs"))
# The unit words a count of each basis is cited in; a count in any other unit is not one.
_BASIS_COUNT_UNITS = {"CBM": frozenset(("cbm", "m3")), "KG": frozenset(("kg",)),
                      "PALLET": frozenset(("pallet", "pallets", "plt", "plts"))}
# Bases counted in whole units, a single one of which is a whole unit-shipment.
_WHOLE_BASES = ("PALLET",)
# The roles a quote's count is read from, each a cited value with its unit.
_COUNT_ROLES = ("quote_basis_count", "quote_volume", "quote_weight", "quote_pallets")
# A header shipping estimate this close to the amount of a quote that is not total freight
# (a share of that amount, same currency) is taken to be that quote's figure.
ESTIMATE_QUOTE_TOLERANCE = Decimal("0.005")
CONFIRMATION_ROLES = ("confirmation_amount", "confirmation_unit_price", "confirmation_quantity")
# The real inputs: the FileMaker replica supplier row of the
# order's own supplier for the line's item (``tariff_c``, ``hscode``, ``countryoforigin``),
# FileMaker's own landed cost ``dlt_costlanded_c`` on that row's price tiers (``tierpricing``:
# each tier's minimum quantity and figure, compared as counts only), the Portal rate settings
# ``tariff_rate`` and the Sage receipt's own currency. Each is optional: a plan naming none of
# them runs as before.
SUPPLIER_ROW_ROLES = ("supplier_tariff", "supplier_hs_code", "supplier_origin")
TIER_ROLES = ("fm_landed_cost", "fm_tier_quantity")
REAL_INPUT_ROLES = SUPPLIER_ROW_ROLES + TIER_ROLES + ("rate_setting_tariff",
                                                      "receipt_currency")
# A BC line's directUnitCost is gross: before the line discount, and with tax when the order's
# pricesIncludeTax is true. Its goods are the line's amountExcludingTax over its quantity, else
# it is known net only when the line records no discount and the order no tax-inclusive price.
BC_NET_ROLES = ("bc_line_amount", "bc_line_discount", "prices_include_tax")
BC_GROSS_PRICE = "bc_direct_unit_cost"
PLAN_ROLES = ("currency", "unit_price", "quantity", "received", "freight_estimate",
              "duty_estimate", "classification", "receipt_freight", "receipt_duty",
              "units_per_box", "po_number") + QUOTE_ROLES + CONFIRMATION_ROLES \
    + REAL_INPUT_ROLES + BC_NET_ROLES
_OPTIONAL_ROLES = frozenset(("classification", "receipt_freight", "receipt_duty",
                             "units_per_box", "po_number") + QUOTE_ROLES + CONFIRMATION_ROLES
                            + REAL_INPUT_ROLES + BC_NET_ROLES)
# The links a supplier row is proven to be this order's supplier's row for the line's item by:
# row -> item (``supplier_item_link``), row -> supplier (``supplier_link``), order -> supplier
# (``po_supplier_link``) and supplier twins (``supplier_twin_link``). Without the first three
# no row is ever read.
SUPPLIER_LINK_ARGS = ("supplier_item_link", "supplier_link", "po_supplier_link")
# The link a FileMaker price tier (``tierpricing``, keyed by its supplier row) is that row's
# tier by: tier -> supplier row. Without it no tier, so no FileMaker figure, is ever read.
TIER_LINK_ARG = "tier_link"
# FileMaker's dlt_costlanded_c and ours agree within this share of FileMaker's figure.
FM_AGREEMENT_TOLERANCE = Decimal("0.01")
# Every duty rate stays a labelled assumption until Vince answers later_ask duty-rate-rule.
DUTY_RULE_PENDING = "the duty-rate rule (later_ask duty-rate-rule) is pending with Vince"
_TARIFF_PERCENT = re.compile(r"(\d{1,3}(?:\.\d{1,6})?)\s*%")
_TARIFF_UNITS = {"%": Decimal(100), "percent": Decimal(100), "pct": Decimal(100),
                 "ratio": Decimal(1), "fraction": Decimal(1)}
_LINE_ROLES = ("unit_price", "quantity", "received")
MAX_STEP_CHARS = 400
# The roles whose sources a cost input rests on (named in its gap as what was searched).
_COST_ROLES = {"goods": ("unit_price", "confirmation_amount"), "freight": ("freight_estimate", "receipt_freight"),
               "duty": ("duty_estimate", "classification", "receipt_duty"),
               "receipt_actual": ("receipt_freight", "receipt_duty")}
_QUOTE_SEARCHED = ("quote_amount", "quote_rate", "quote_basis", "quote_basis_count",
                   "quote_includes", "quote_excludes", "quote_valid_until")
# A gap about a quote's count names every role a count is read from (a gap lists 8 sources).
_QUOTE_COUNT_SEARCHED = ("quote_amount", "quote_rate", "quote_basis", *_COUNT_ROLES)
_QUOTE_COUNT_CODES = frozenset(("BASIS_NOT_TOTAL", "FREIGHT_RATE_COUNT_CONFLICT",
                                "FREIGHT_BASIS_TERMS_UNREAD",
                                "FREIGHT_BASIS_COUNTS_CONFLICT", "FREIGHT_BASIS_COUNT_UNKNOWN",
                                "PACK_SPEC_NOT_SERVED"))
# Typed unknowns and the gap state each is reported with (the rest: unknown).
_GAP_STATES = {"DUTY_RATE_SOURCE_ABSENT": "not_ingested",
               "MIXED_CURRENCY_NO_CITED_RATE": "not_ingested",
               "RECEIPT_ACTUAL_CURRENCY_UNRECORDED": "not_ingested",
               "PO_FREIGHT_NOT_RECORDED": "absent_in_source",
               "PO_DUTY_NOT_RECORDED": "absent_in_source",
               "CURRENCY_UNCITED": "absent_in_source",
               "UNIT_PRICE_DENOMINATOR_UNKNOWN": "absent_in_source",
               "UNIT_PRICE_WITHHELD": "inaccessible",
               "PO_FREIGHT_ESTIMATE_WITHHELD": "inaccessible",
               "PO_DUTY_ESTIMATE_WITHHELD": "inaccessible",
               "LINE_QUANTITY_UNKNOWN": "absent_in_source",
               **{code: state for code, (state, _, _) in _QUOTE_GAPS.items()}}
FREIGHT_LABELS = {"posted": "freight basis: historical posted actual (the receipt's freight, "
                            "which the Portal posts over the estimate)",
                  "actual": "freight basis: recorded actual (inbound freight charges posted in "
                            "Sage AP and linked to the purchase order by a recorded reference), "
                            "not the header estimate and not a quote",
                  "estimate": "freight basis: purchase order header estimate (ERP-D-015), not a "
                              "posted actual and not a quote",
                  "quote": "freight basis: current quote (a forwarder's offer read from a "
                           "captured document, a lead), not a posted actual and not the purchase "
                           "order's estimate",
                  "none": "freight basis: none usable (see the freight gap)"}

# ---- estimate mode (before receipt; ERP-D-015: PO header = estimate, receipt = actual) -------
#
# An open purchase order's landed cost is an ESTIMATE, kept apart from the actual landed cost
# (``<name>_line_<i>``): its own entries ``<name>_estimate`` (the purchase order),
# ``<name>_estimate_subtotal`` (goods + freight, every amount cited) and
# ``<name>_estimate_line_<i>`` (per unit of the ordered quantity). Each lists every assumption
# it rests on in its steps and ends with ESTIMATE_LABEL. Freight is the header shipping
# estimate (else one usable forwarder quote total in the purchase order's currency) pro-rated
# over the ORDERED line value; duty is the header duty estimate pro-rated the same way, else a
# person's recorded duty-rate assumption (``duty_assumption``) times the goods, else the Portal
# default tariff rate applied as a labelled assumption beside a gap naming the missing input
# and who can give it, with the range from no duty to that default.
ESTIMATE_LABEL = ("ESTIMATE before receipt (ERP-D-015: the purchase order header is an "
                  "estimate, the receipt the actual); not the actual landed cost, which is "
                  "stated only after receipt")
RECEIVED_ESTIMATE_LABEL = ("ESTIMATE after receipt: the receipts read record no freight or duty "
                           "actual, so each part not recorded is estimated (from PLX history or "
                           "a labelled default); not the actual landed cost")
# The basis of each landed-cost part in an estimate's steps ("part <name>: <basis>"): what was
# paid, what a record states, what PLX history suggests, or a default no record states.
# ``in_freight_quote``: a part the forwarder quote used as freight states it includes (its
# duty is in that one freight-and-duty charge, never added again, and never "recorded")
IN_QUOTE = "included in the freight quote used as freight; never added again"
PART_BASES = ("actual", "recorded", "in_freight_quote", "estimated_from_history", "default",
              "not_applicable")
ESTIMATE_BASIS =("pro-rated by ordered line value (unit price x ordered quantity); the "
                  "Portal's allocatePoFreightPerUnit splits by received line value, which "
                  "does not exist before receipt")
ASSUMPTION_KINDS = ("duty_rate",)
_ASSUMPTION_KEYS = frozenset(("kind", "rate", "source", "recorded_by", "recorded_at",
                              "author_kind", "case_id", "record_id", "subject"))
_ASSUMPTION_TEXT = re.compile(r"[^\x20-\x7e -ɏ]")


def duty_assumption(record: Mapping[str, Any]) -> dict[str, Any]:
    """A person's recorded duty-rate assumption, validated (``LandedCostError`` otherwise):
    ``{kind: "duty_rate", rate: "<decimal string> in [0, 1]", source, recorded_by,
    recorded_at: ISO date-time, author_kind: "human", case_id, record_id, subject}``. Nothing
    unknown is accepted: an extra key, an agent's record, a float rate, a rate outside [0, 1]
    or a blank field fails closed. Returns the normalised record (texts trimmed, the source
    cut to 120 printable characters: it is a person's note, shown, never followed)."""
    if not isinstance(record, Mapping):
        raise LandedCostError("assumption_must_be_mapping")
    extra = set(record) - _ASSUMPTION_KEYS
    if extra:
        raise LandedCostError("assumption_unknown_keys:" + ",".join(sorted(map(str, extra)))[:200])
    if record.get("kind") not in ASSUMPTION_KINDS:
        raise LandedCostError("assumption_kind_unknown")
    if record.get("author_kind") != "human":
        raise LandedCostError("assumption_not_recorded_by_a_person")
    out: dict[str, Any] = {"kind": record["kind"], "author_kind": "human"}
    for key in ("source", "recorded_by", "case_id", "record_id", "subject"):
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            raise LandedCostError(f"assumption_needs_{key}")
        out[key] = _ASSUMPTION_TEXT.sub(" ", value.strip())[:120 if key == "source" else 200]
    rate = record.get("rate")
    if not isinstance(rate, str):
        raise LandedCostError("assumption_rate_must_be_decimal_string")
    try:
        number = Decimal(rate.strip())
    except ArithmeticError as exc:
        raise LandedCostError("assumption_rate_must_be_decimal_string") from exc
    if not number.is_finite() or not Decimal(0) <= number <= Decimal(1):
        raise LandedCostError("assumption_rate_out_of_range")
    out["rate"] = _dec_str(number)
    when = record.get("recorded_at")
    try:
        if not isinstance(when, str) or "T" not in when:
            raise ValueError(when)
        datetime.fromisoformat(when.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise LandedCostError("assumption_recorded_at_must_be_iso_datetime") from exc
    out["recorded_at"] = when.strip()
    return out


def _assumption_text(record: Mapping[str, Any]) -> str:
    return (f"duty rate {record['rate']} (a person's assumption, recorded by "
            f"{record['recorded_by']} at {record['recorded_at']} on case {record['case_id']}, "
            f"record {record['record_id']}; source stated: {record['source']})")


def prorate(total: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """``total`` split over ``weights`` (each > 0) in proportion, at the total's precision or
    cents, whichever is finer: largest remainder, ties to the earlier weight, so the shares
    sum to ``total`` exactly and no share is lost or duplicated."""
    if not weights or any(w <= 0 for w in weights) or total < 0:
        raise LandedCostError("prorate_needs_positive_weights")
    exponent = min(total.as_tuple().exponent, -2) if total.is_finite() else -2
    quantum = Decimal(1).scaleb(exponent)
    # A total with a long tail (a history rate x goods) needs more digits than the default
    # 28 to quantize at its own precision: computed in a wide enough context, never refused.
    with localcontext() as ctx:
        ctx.prec = max(ctx.prec, len(total.as_tuple().digits) - exponent + 30)
        whole = sum(weights)
        exact = [total * w / whole for w in weights]
        shares = [x.quantize(quantum, rounding="ROUND_DOWN") for x in exact]
        left = int(((total - sum(shares)) / quantum).to_integral_value())
        order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - shares[i]), i))
        for i in order[:left]:
            shares[i] += quantum
    return shares


def _part_steps(*, freight_basis: str, freight_label: str, duty_basis: Mapping[str, str],
                history: Mapping[str, Mapping[str, Any]],
                actuals: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """The basis of each part of an estimate, one step each (``part <name>: <basis>``, a
    ``PART_BASES`` word first): goods are always recorded; a history estimate names its
    level, method, sample, period and spread, and the capture it came from."""
    def hist(part: str) -> str:
        h = history[part]
        return f": {h['label']} ({h['note']})"
    steps = ["part goods: recorded (cited unit price x ordered quantity of every line)",
             "part freight: estimated_from_history" + hist("freight")
             if freight_basis == "estimated_from_history" else
             f"part freight: {freight_basis} ({freight_label})"]
    counts = Counter(duty_basis.values())
    if len(counts) == 1:
        [basis] = counts
        steps.append(f"part duty: {basis}"
                     + (hist("duty") if basis == "estimated_from_history" else
                        f" ({IN_QUOTE})" if basis == "in_freight_quote" else ""))
    elif counts:
        steps.append("part duty: " + ", ".join(f"{b} on {counts[b]} line(s)" for b in PART_BASES
                                               if counts.get(b))
                     + (hist("duty") if counts.get("estimated_from_history") else ""))
    duty_from = (actuals.get("duty") if "actual" in counts else None) or (
        history.get("duty") if "estimated_from_history" in counts else None)
    steps.append("part brokerage: included in the duty part (the Sage 'Duties & Brokerage' "
                 "account holds both, so that duty is an upper bound); not stated separately"
                 if duty_from and duty_from.get("includes_brokerage") else
                 "part brokerage: not_applicable (no brokerage charge is recorded for this "
                 "purchase order; none is added)")
    if "estimated_from_history" in (freight_basis, *counts):
        steps.append("ESTIMATE: at least one part is estimated from PLX history, not recorded "
                     "for this purchase order, so the total is an estimate")
    return [s[:MAX_STEP_CHARS] for s in steps]


def _pre_receipt_estimate(*, name: str, target: str | None, line_ids: Sequence[str],
              every_line: Sequence[str], prices: Mapping[str, Mapping[str, Any]],
              quantities: Mapping[str, Mapping[str, Any]], linked: bool, complete: bool,
              covered: bool, held_back: int, freight_est: Mapping[str, Any] | None,
              duty_est: Mapping[str, Any] | None, partial_quote: bool,
              quotes: Sequence[tuple[Decimal, str, Sequence[Mapping[str, Any]], bool]],
              hs_of: Mapping[str, Mapping[str, Any] | None],
              assumptions: Sequence[Mapping[str, Any]], header_ids: set[str], as_of: str,
              resolver: Resolver | None, actor: Any,
              unknown: Callable[..., None], currency_step: str | None = None,
              currency_gap: str | None = None,
              rows: Mapping[str, Mapping[str, Any]] | None = None,
              setting: Mapping[str, Any] | None = None,
              actuals: Mapping[str, Mapping[str, Any]] | None = None,
              net_goods: Mapping[str, Mapping[str, Any]] | None = None,
              history: Mapping[str, Mapping[str, Any]] | None = None,
              incoterm: Mapping[str, Any] | None = None,
              header_withheld: Mapping[str, Sequence[str]] | None = None,
              label: str = ESTIMATE_LABEL) -> list[dict[str, Any]]:
    """The estimate entries of an open purchase order (the block above ``duty_assumption``).

    ``actuals``: the purchase order's recorded duty and freight rates (``_recorded_actuals``);
    a recorded rate comes first, before the header estimate, a quote or any assumed rate.
    ``history``: the order's estimates from PLX history (``_recorded_actuals``). Freight with
    no actual, no header estimate and no forwarder quote is the history rate times the goods;
    a line's duty with no actual, header estimate, person's rate or supplier-row tariff is the
    history rate times its goods, before the Portal rate setting or code default. Where the
    recorded ``incoterm`` leaves that part with the seller, it is a cited zero instead
    (provisional rule). Every part is labelled in the steps (``part <name>: <basis>``,
    ``PART_BASES``); a history estimate is never worded as recorded. ``label``: the estimate
    label (``ESTIMATE_LABEL``, or ``RECEIVED_ESTIMATE_LABEL`` after receipt).

    Amounts are stated only over a line set proven whole (a complete run, a reader whose scope
    covers the lines, every line read with a unit price and an ordered quantity) whose every
    line value (positive unit price per the unit of its ordered quantity, in the purchase
    order's currency) is cited, with a freight source; each line is computed by
    ``compute_landed_cost``, which re-reads every cited input for the actor. Otherwise each
    entry is null and a gap names the missing input and who can give it. Gaps and steps name
    codes, entries, rates and amounts the reader read, never source text.

    Duty, with no header duty estimate and no person's recorded rate, is per line: the
    ``tariff_c`` of the order's own supplier's FileMaker replica row for the line's item
    (``rows[oid]``: ``rate``, the tariff ``entries``, a ``problem`` (code, detail) when a row's
    rate is unreadable or the rows disagree, and the row's ``hs``/``origin`` entries), else
    the Portal rate settings ``tariff_rate`` (``setting``: ``rate``, ``entries``, ``problem``),
    else the code default. Each is a labelled assumption naming its source. The entries a
    rate rests on are re-read for the actor before it is used: a line whose row rate (or, at
    the rate setting, whose setting) this reader cannot re-read has no value and no step or
    gap states that rate (SUPPLIER_TARIFF_UNREAD, RATE_SETTING_TARIFF_UNREAD); it never falls
    back to another rate."""
    def handle(entry: Mapping[str, Any] | None) -> str | None:
        return entry["citation"]["l1_handle"] if entry and entry.get("citation") else None

    labels = {oid: f"{name}_estimate_line_{i}"[:64] for i, oid in enumerate(line_ids, start=1)}
    lines = [{"name": labels[oid], "kind": "computed", "value": None,
              "inputs": [], "steps": [], "basis": [oid], "check": "not_applicable"}
             for oid in line_ids]
    subtotal: dict[str, Any] = {"name": f"{name}_estimate_subtotal"[:64], "kind": "computed",
                                "value": None, "inputs": [], "steps": [],
                                "check": "not_applicable"}
    total: dict[str, Any] = {"name": f"{name}_estimate"[:64], "kind": "computed", "value": None,
                             "inputs": [e["name"] for e in lines][:50], "steps": [],
                             "check": "not_applicable"}
    out = [total, subtotal, *lines]

    def close(why: str) -> list[dict[str, Any]]:
        for entry in out:
            entry["steps"] = [why[:MAX_STEP_CHARS], label]
        return out

    if target is None:
        return close(f"no estimate: {currency_gap or 'CURRENCY_UNCITED'} (no target currency; "
                     "see the purchase order currency gap)")
    for entry in out:
        entry.update(unit=target, currency=target)

    # The duty-rate assumptions a person recorded on this purchase order (validated, else a gap).
    recorded: list[dict[str, Any]] = []
    for item in assumptions:
        try:
            checked = duty_assumption(item)
            if checked["subject"] not in header_ids:
                raise LandedCostError("assumption_subject_not_this_purchase_order")
        except LandedCostError as exc:
            unknown("ASSUMPTION_RECORD_INVALID", "estimate duty assumption",
                    f"a recorded assumption does not validate ({str(exc).split(':')[0][:80]}); "
                    "it is not applied", holds=False, who="FIN")
            continue
        recorded.append(checked)

    missing = [(role, [oid for oid in every_line if _entry_decimal(found.get(oid)) is None])
               for role, found in (("unit price", prices), ("ordered quantity", quantities))]
    if complete and not covered:
        # A reader limit, not a missing input: a value this reader lacks may be withheld from
        # it, so no person is named for it (a grant would answer it).
        unknown("ESTIMATE_READ_SCOPE_UNPROVEN", "estimate lines",
                (f"{held_back} line(s) of the purchase order carry a marking this reader does "
                 "not hold and are withheld from it" if held_back else
                 "this reader's read scope (the markings on the order's lines and the line "
                 "source's audience) is not proven to show every line of the purchase order")
                + "; the estimate pro-rates header freight over every ordered line, never a "
                "partial set", "unit_price", "quantity")
        return close("no estimate: ESTIMATE_READ_SCOPE_UNPROVEN (see the gaps)")
    if not every_line:
        # No line reached: PO_LINES_INCOMPLETE already names that cause and who can give the
        # lines; the estimate cites it rather than stating the same cause twice.
        return close("no estimate: PO_LINES_INCOMPLETE (no line of the purchase order was "
                     "reached; see the gaps)")
    if not (complete and covered and linked) or any(oids for _, oids in missing):
        why = "; ".join(f"{len(oids)} of {len(every_line)} line(s) were read with no {role}"
                        for role, oids in missing if oids) or (
            "the run was truncated or cut by its step budget, so lines past the read are not "
            "read" if not complete else
            "this reader's read scope is not proven to show every line of the purchase order"
            if not covered else "no line of the purchase order was reached")
        unknown("ESTIMATE_LINES_INCOMPLETE", "estimate lines",
                why + "; the estimate pro-rates header freight over every ordered line, never a "
                "partial set", "unit_price", "quantity", who="PROC")
        return close("no estimate: ESTIMATE_LINES_INCOMPLETE (see the gaps)")

    def line_value(oid: str) -> Decimal | None:
        price, qty = prices[oid], quantities[oid]
        unit = unit_token(price.get("unit"))
        value, count = _entry_decimal(price), _entry_decimal(qty)
        if unit is None or unit != unit_token(qty.get("unit")) or price.get("currency") != target \
                or value is None or count is None or value <= 0 or count <= 0:
            return None
        return value * count
    values = {oid: line_value(oid) for oid in line_ids}
    if any(v is None for v in values.values()):
        unknown("ESTIMATE_LINE_VALUE_UNCITED", "estimate lines",
                f"{sum(v is None for v in values.values())} of {len(line_ids)} line(s) state no "
                f"positive unit price per the unit of their ordered quantity in {target}; no "
                "line value to pro-rate over", "unit_price", "quantity", who="PROC")
        return close("no estimate: ESTIMATE_LINE_VALUE_UNCITED (see the gaps)")
    weights = [values[oid] for oid in line_ids]
    net_goods = net_goods or {}
    weight_handles = list(dict.fromkeys(
        h for oid in line_ids
        for h in (handle(prices[oid]), handle(quantities[oid]),
                  *((net_goods.get(oid) or {}).get("derived", {}).get("from") or ())) if h))
    goods_total = sum(weights, Decimal(0))

    # Freight: the recorded actual rate, else the header estimate, else one usable quote total,
    # in the target currency.
    freight: tuple[Decimal, list[Mapping[str, Any]], str] | None = None
    header = _entry_decimal(freight_est)
    actuals = actuals or {}
    history = history or {}

    def seller_bears(part: str) -> bool:
        return bool(incoterm and incoterm.get("bearing")
                    and (incoterm.get("buyer_bears") or {}).get(part) is False)
    freight_actual = actuals.get("freight")
    freight_basis = "recorded"
    if freight_actual is not None:
        freight = (Decimal(freight_actual["rate"]) * goods_total, [],
                   f"recorded actual freight rate {freight_actual['rate']} (inbound freight "
                   f"charges linked by a recorded reference; {freight_actual['note']}) x goods")
        freight_basis = "actual"
    elif header is not None and header > 0 and freight_est.get("currency") == target \
            and not partial_quote:
        freight = (header, [freight_est], f"purchase order header shipping estimate "
                   f"{freight_est['name']}")
    elif header is not None and header > 0 and freight_est.get("currency") != target:
        unknown("PO_ESTIMATE_CURRENCY_DIFFERS", "estimate freight",
                "the header shipping estimate carries no cited currency equal to the purchase "
                "order's; no exchange rate is projected", "freight_estimate", who="PROC")
    elif header is not None and header > 0:
        unknown("FREIGHT_ESTIMATE_IS_PARTIAL_QUOTE", "estimate freight",
                "the header shipping estimate is the amount of a cited forwarder quote that is "
                "not the total freight (see the freight quote gaps)", "freight_estimate",
                who=FORWARDER)
    # A forwarder quote whose total includes duties and taxes is one freight-and-duty charge
    # (BIZ-002): no second duty is added beside it, and with another duty source recorded the
    # split is unknown, so no estimate is stated
    quote_duty = False
    if freight is None:
        totals = sorted({q[0] for q in quotes})
        with_duty = {q[3] for q in quotes}
        if len(totals) == 1 and len(with_duty) > 1:
            unknown("FREIGHT_QUOTES_DIFFER_ON_DUTY", "estimate freight",
                    f"{len(quotes)} usable forwarder quotes state the same total freight in "
                    f"{target}, but not all of them include duties and taxes; none is chosen",
                    "freight_estimate", who=FORWARDER)
        elif len(totals) == 1:
            entries = [e for q in quotes for e in q[2]]
            quote_duty = with_duty == {True}
            freight = (totals[0], entries, "forwarder quote total " + ", ".join(
                q[1] for q in quotes) + (" including duties and taxes" if quote_duty else "")
                + " (a lead read from a captured document)")
        elif totals:
            unknown("FREIGHT_QUOTES_DIFFER", "estimate freight",
                    f"{len(totals)} usable forwarder quotes state different total freight in "
                    f"{target}; none is chosen", "freight_estimate", who="PROC")
        elif header is None or header <= 0:
            if header is None and (header_withheld or {}).get("freight"):
                # Withheld from this reader, not absent: the history estimate below is
                # labelled as such, never as the header's.
                unknown("PO_FREIGHT_ESTIMATE_WITHHELD", "estimate freight",
                        "the purchase order header's shipping estimate carries the "
                        f"{', '.join(sorted(header_withheld['freight']))} marking, which this "
                        "reader does not hold, and no usable forwarder quote states the total "
                        f"freight in {target}", "freight_estimate", who="FIN")
            else:
                unknown("PO_FREIGHT_NOT_RECORDED", "estimate freight",
                        "the purchase order header has no shipping estimate and no usable "
                        f"forwarder quote states the total freight in {target}; the Portal "
                        "would read that as zero freight", "freight_estimate", who=FORWARDER)
            # Nothing records the freight: the recorded incoterm may leave it with the seller,
            # else PLX history estimates it (labelled, never as recorded).
            if seller_bears("freight"):
                freight = (Decimal(0), [], f"incoterm {incoterm['incoterm']} "
                           f"({incoterm['set_by']}): the seller bears freight; provisional rule, "
                           f"{incoterm['status']}")
                freight_basis = "recorded"
            elif history.get("freight") is not None:
                hist = history["freight"]
                freight = (Decimal(hist["rate"]) * goods_total, [],
                           f"freight ESTIMATED FROM PLX HISTORY, rate {hist['rate']} x goods "
                           "(its basis: see 'part freight')")
                freight_basis = "estimated_from_history"
    goods_step = (f"goods: {money_text(goods_total, target)} {target} over {len(line_ids)} line(s) (cited "
                  "unit price x ordered quantity)")
    if freight is None:
        subtotal["steps"] = [goods_step, "freight: no source (see the estimate freight gap); no "
                             "subtotal is stated", label]
        total["steps"] = ["no estimate: no freight source (see the estimate freight gap)",
                          label]
        for entry in lines:
            entry["steps"] = ["no estimate: no freight source (see the estimate freight gap)",
                              label]
        return out
    freight_total, freight_entries, freight_label = freight
    freight_shares = prorate(freight_total, weights)
    freight_from = [h for h in (handle(e) for e in freight_entries) if h] + weight_handles
    if freight_actual is not None:
        freight_from = [freight_actual["_source"]] + weight_handles
    elif freight_basis == "estimated_from_history":
        freight_from = [history["freight"]["_source"]] + weight_handles
    elif not freight_entries and seller_bears("freight") and not quotes:
        freight_from = [incoterm["source"]] + weight_handles
    duty_basis: dict[str, str] = {}

    # Duty: the header duty estimate, else a person's recorded rate, else the Portal default.
    duty_header = _entry_decimal(duty_est)
    rates = sorted({r["rate"] for r in recorded})
    assumption = max(recorded, key=lambda r: r["recorded_at"]) if len(rates) == 1 else None
    if len(rates) > 1:
        unknown("DUTY_ASSUMPTIONS_DISAGREE", "estimate duty assumption",
                f"{len(recorded)} recorded duty-rate assumptions state {len(rates)} different "
                "rates; none is applied", holds=True, who="FIN")
    listed: list[str] = []
    rows = rows or {}
    # Per line: the entries its duty rests on beyond the weights (named as its inputs and
    # re-read for the actor), its duty rule, and its row notes (origin).
    own: dict[str, list[Mapping[str, Any]]] = {oid: [] for oid in line_ids}
    notes: dict[str, list[str]] = {oid: [] for oid in line_ids}
    from_of: dict[str, list[str]] = {}
    rule_of: dict[str, str] = {}
    fallback: list[str] = []          # lines at the rate setting or the code default
    row_rated: set[str] = set()
    withheld: set[str] = set()        # lines whose rate this reader cannot re-read
    set_rate = (setting or {}).get("rate")
    fallback_label = ("the Portal rate settings tariff_rate" if set_rate is not None
                      else "the Portal default tariffRate")
    for oid in line_ids:
        row = rows.get(oid) or {}
        if hs_of.get(oid) is not None and any(hs_of[oid] is e for e in row.get("hs") or ()):
            own[oid].append(hs_of[oid])
        origins = list(row.get("origin") or ())
        if origins:
            own[oid] += origins
            notes[oid].append("origin: " + ", ".join(e["name"] for e in origins)[:200]
                              + " (countryoforigin on the FileMaker replica supplier row; with "
                              "the HS code it decides the duty rate, which no source projects)")
    duty_actual = actuals.get("duty")
    if quote_duty and (duty_actual is not None or assumption is not None
                       or (duty_header is not None and duty_header > 0)):
        source = ("the recorded actual duty rate" if duty_actual is not None else
                  "a person's recorded duty-rate assumption" if assumption is not None else
                  "the purchase order header's duty estimate")
        unknown("DUTY_IN_FREIGHT_QUOTE_AND_RECORDED", "estimate duty",
                "the forwarder quote used as freight includes duties and taxes, and "
                f"{source} is recorded beside it; adding both would count duty twice and "
                "the quote's split is not known, so no estimate is stated",
                "duty_estimate", who=FORWARDER)
        why = ("no estimate: the forwarder quote includes duties and taxes and a separate "
               "duty is recorded (see the estimate duty gap)")
        subtotal["steps"] = [why, label]
        total["steps"] = [why, label]
        for entry in lines:
            entry["steps"] = [why, label]
        return out
    if quote_duty:
        duty_shares = [Decimal(0) for _ in weights]
        quote_from = [h for h in (handle(e) for e in freight_entries) if h] \
            + list(weight_handles)
        duty_rule = ("estimate duty: 0 beside freight; the forwarder quote total used as "
                     "freight includes duties and taxes (one freight-and-duty charge)")
        for oid in line_ids:
            from_of[oid], rule_of[oid] = quote_from, duty_rule
        listed.append("duty: included in the forwarder quote total, never added again")
        duty_basis.update((oid, "in_freight_quote") for oid in line_ids)
    elif duty_actual is not None:
        rate = Decimal(duty_actual["rate"])
        duty_shares = [rate * w for w in weights]
        duty_rule = (f"estimate duty: goods x the recorded actual duty rate {duty_actual['rate']} "
                     f"(broker/duty charges linked by a recorded reference; "
                     f"{duty_actual['note']})" + ("; an upper bound, the G/L account also holds "
                                                  "the broker's fee"
                                                  if duty_actual.get("includes_brokerage")
                                                  else ""))
        for oid in line_ids:
            from_of[oid], rule_of[oid] = [duty_actual["_source"]] + list(weight_handles), \
                duty_rule
        listed.append("actual: the recorded duty rate of this purchase order applies to every "
                      "line by goods value")
        duty_basis.update((oid, "actual") for oid in line_ids)
    elif duty_header is not None and duty_header > 0 and duty_est.get("currency") == target:
        duty_shares = prorate(duty_header, weights)
        header_from = [h for h in (handle(duty_est),) if h] + list(weight_handles)
        duty_rule = (f"estimate duty: purchase order header duty estimate {duty_est['name']} "
                     f"{money_text(duty_header, target)} {target} {ESTIMATE_BASIS}")
        for oid in line_ids:
            from_of[oid], rule_of[oid] = header_from, duty_rule
            own[oid].insert(0, duty_est)
        listed.append("assumption: the header duty estimate spreads by ordered line value (one "
                      "rate for every line; the Portal has no per-line duty allocation)")
        duty_basis.update((oid, "recorded") for oid in line_ids)
    else:
        if duty_header is None and (header_withheld or {}).get("duty"):
            unknown("PO_DUTY_ESTIMATE_WITHHELD", "estimate duty",
                    "the purchase order header's duty estimate carries the "
                    f"{', '.join(sorted(header_withheld['duty']))} marking, which this reader "
                    "does not hold", "duty_estimate", who="FIN")
        if duty_header is not None and duty_header > 0:
            unknown("PO_ESTIMATE_CURRENCY_DIFFERS", "estimate duty",
                    "the header duty estimate carries no cited currency equal to the purchase "
                    "order's; no exchange rate is projected", "duty_estimate", who="FIN")
        if assumption is not None:
            rate = Decimal(assumption["rate"])
            duty_rule = "estimate duty: goods x " + _assumption_text(assumption)
            listed.append("assumption: " + _assumption_text(assumption))
            for oid in line_ids:
                from_of[oid], rule_of[oid] = list(weight_handles), duty_rule
            duty_shares = [rate * w for w in weights]
            duty_basis.update((oid, "recorded") for oid in line_ids)
        else:
            default_rate = Decimal(str(PORTAL_QUOTE_DUTY_RATE))
            if setting and setting.get("problem"):
                unknown("RATE_SETTING_TARIFF_UNREADABLE", "estimate duty rate setting",
                        setting["problem"], "rate_setting_tariff", holds=False, who="FIN")
            set_entries = list((setting or {}).get("entries") or ())
            set_names = ", ".join(e["name"] for e in set_entries)[:120]
            # A rate is used only when every entry it rests on re-reads for this reader;
            # otherwise the line is withheld, its rate named by entry only, never stated.
            reader = _Reader(resolver, actor)

            def rereads(entries: Sequence[Mapping[str, Any]]) -> bool:
                return all(handle(e) and reader.resolve(handle(e)) is not None
                           for e in entries)
            set_withheld = set_rate is not None and not rereads(set_entries)
            row_steps: list[str] = []
            duty_shares = []
            for oid, weight in zip(line_ids, weights):
                row = rows.get(oid) or {}
                if row.get("problem"):
                    code, detail = row["problem"]
                    unknown(code, "estimate duty", detail, "supplier_tariff", line=labels[oid],
                            holds=False, who="FIN")
                rate = row.get("rate")
                if rate is not None and not rereads(row["entries"]):
                    withheld.add(oid)
                    names = ", ".join(e["name"] for e in row["entries"])[:120]
                    unknown("SUPPLIER_TARIFF_UNREAD", "estimate duty",
                            f"{names} (tariff_c on the FileMaker replica supplier row) is not "
                            "readable for this reader; the line's duty rests on it, so the "
                            "line states no estimate and no other rate replaces it",
                            "supplier_tariff", line=labels[oid], who="FIN")
                    duty_shares.append(Decimal(0))
                    continue
                if rate is None and set_withheld and not (
                        seller_bears("duty") or history.get("duty") is not None):
                    withheld.add(oid)
                    unknown("RATE_SETTING_TARIFF_UNREAD", "estimate duty rate setting",
                            f"{set_names} (public.app_rate_settings tariff_rate) is not "
                            "readable for this reader; no estimate line rests on it and no "
                            "other rate replaces it", "rate_setting_tariff",
                            line=labels[oid], who="FIN")
                    duty_shares.append(Decimal(0))
                    continue
                if rate is not None:
                    names = ", ".join(e["name"] for e in row["entries"])[:120]
                    row_rated.add(oid)
                    own[oid] += list(row["entries"])
                    from_of[oid] = [h for h in map(handle, row["entries"]) if h] \
                        + list(weight_handles)
                    rule_of[oid] = (f"estimate duty: goods x {_dec_str(rate)}, the FileMaker "
                                    f"supplier row tariff {names} (tariff_c), an assumption: "
                                    f"{DUTY_RULE_PENDING}")
                    row_steps.append(f"{names} {_dec_str(rate)}")
                    duty_basis[oid] = "recorded"
                elif seller_bears("duty") or history.get("duty") is not None:
                    # Nothing records this line's duty: the recorded incoterm may leave it with
                    # the seller, else PLX history estimates it (labelled, never as recorded).
                    if seller_bears("duty"):
                        rate = Decimal(0)
                        from_of[oid] = [incoterm["source"]] + list(weight_handles)
                        rule_of[oid] = (f"estimate duty: 0, incoterm {incoterm['incoterm']} "
                                        f"({incoterm['set_by']}): the seller bears duty; "
                                        f"provisional rule, {incoterm['status']}")
                        duty_basis[oid] = "recorded"
                        why = f"incoterm {incoterm['incoterm']} leaves duty with the seller"
                    else:
                        hist = history["duty"]
                        rate = Decimal(hist["rate"])
                        from_of[oid] = [hist["_source"]] + list(weight_handles)
                        rule_of[oid] = (f"estimate duty: goods x {hist['rate']}, ESTIMATED FROM "
                                        "PLX HISTORY, not recorded for this purchase order "
                                        "(its basis: see 'part duty')")
                        duty_basis[oid] = "estimated_from_history"
                        why = (f"the estimate applies the duty rate {hist['rate']} estimated "
                               "from PLX history, labelled as such")
                    hs = hs_of.get(oid)
                    code, who = (("DUTY_RATE_SOURCE_ABSENT", "FIN") if _entry_text(hs) else
                                 ("HS_CODE_NOT_CITED", "PROC"))
                    unknown(code, "estimate duty",
                            ("an HS code is cited for the item, but no duty rate is cited or "
                             "recorded; Finance can record the rate (with its source) on the "
                             "case" if code == "DUTY_RATE_SOURCE_ABSENT" else
                             "no HS code is cited for the item and no duty rate is recorded; "
                             "Procurement can give the HS code, Finance the rate")
                            + f"; {why}", "duty_estimate", "classification",
                            line=labels[oid], who=who)
                else:
                    fallback.append(oid)
                    duty_basis[oid] = "default"
                    if set_rate is not None:
                        rate = set_rate
                        own[oid] += set_entries
                        from_of[oid] = [h for h in map(handle, set_entries) if h] \
                            + list(weight_handles)
                        rule_of[oid] = (f"estimate duty: goods x the Portal rate settings "
                                        f"tariff_rate {_dec_str(rate)} ({set_names}), a flat rate "
                                        "and not the item's: an assumption no person recorded")
                    else:
                        rate = default_rate
                        from_of[oid] = list(weight_handles)
                        rule_of[oid] = (f"estimate duty: goods x the Portal default tariffRate "
                                        f"{_dec_str(rate)} ({PORTAL_SOURCES['rate_defaults']} "
                                        "RATE_MASTER_DEFAULTS), an assumption no person recorded")
                    hs = hs_of.get(oid)
                    code, who = (("DUTY_RATE_SOURCE_ABSENT", "FIN") if _entry_text(hs) else
                                 ("HS_CODE_NOT_CITED", "PROC"))
                    unknown(code, "estimate duty",
                            ("an HS code is cited for the item, but no duty rate is cited or "
                             "recorded; Finance can record the rate (with its source) on the "
                             "case" if code == "DUTY_RATE_SOURCE_ABSENT" else
                             "no HS code is cited for the item and no duty rate is recorded; "
                             "Procurement can give the HS code, Finance the rate")
                            + f"; the estimate applies {fallback_label} {_dec_str(rate)} as a "
                            "labelled assumption", "duty_estimate", "classification",
                            line=labels[oid], who=who)
                duty_shares.append(rate * weight)
            if row_steps:
                shown = row_steps[:10] + ([f"... {len(row_steps) - 10} more"]
                                          if len(row_steps) > 10 else [])
                listed.append("assumption: duty at the FileMaker supplier row tariff (tariff_c "
                              "on the FileMaker replica supplier row of the order's own "
                              f"supplier for the line's item) on {len(row_steps)} of "
                              f"{len(line_ids)} line(s); {DUTY_RULE_PENDING}, so each rate is "
                              "an assumption: " + "; ".join(shown))
            if fallback and set_rate is not None:
                listed.append(f"assumption: duty at the Portal rate settings tariff_rate "
                              f"{_dec_str(set_rate)} ({set_names}, public.app_rate_settings) on "
                              f"{len(fallback)} line(s): a flat rate, not the item's; "
                              f"{DUTY_RULE_PENDING} (see the estimate duty gap)")
            elif fallback:
                listed.append(f"assumption: duty at the Portal default tariffRate "
                              f"{_dec_str(default_rate)} ({PORTAL_SOURCES['rate_defaults']}); no "
                              "duty rate is cited or recorded (see the estimate duty gap)")
    if any(_entry_text(hs_of.get(oid)) and oid not in row_rated | withheld
           for oid in line_ids):
        listed.append("tariff: the ontology projects no duty rate for a cited HS code")
    listed += _part_steps(freight_basis=freight_basis, freight_label=freight_label,
                          duty_basis=duty_basis, history=history, actuals=actuals)

    line_totals: list[Decimal] = []
    for oid, entry, share, duty in zip(line_ids, lines, freight_shares, duty_shares):
        if oid in withheld:
            entry["steps"] = ["no estimate: the duty rate this line rests on is not readable "
                              "for this reader (see the estimate duty gaps)", label]
            continue
        price, qty = prices[oid], quantities[oid]
        unit = unit_token(qty.get("unit"))
        entry.update(unit=f"{target}/{unit}"[:20])
        # Inputs name the header entries only: every line's price and quantity entries are
        # already the actual-cost lines' inputs, and naming them on every estimate line would
        # make them shared across all answer pages (result_too_large on a long PO). The line's
        # own cited price and quantity are its basis object and its compute steps.
        header_used = [*freight_entries, *own[oid]]
        entry["inputs"] = list(dict.fromkeys(e["name"] for e in header_used if e))[:50]
        entry["basis"] = sorted({oid} | {e["object_id"] for e in header_used
                                         if e and e.get("object_id")})[:100]
        costs = [{"name": "goods", "category": "goods",
                  **({**net_goods[oid], "unit": f"{target}/{unit}", "currency": target}
                     if oid in net_goods else
                     _corrected_input(price, f"{target}/{unit}", target) or {
                      "citation": handle(price), "field": price["citation"]["field_path"],
                      "unit": f"{target}/{unit}", "currency": target})},
                 {"name": "freight", "category": "freight", "unit": target, "currency": target,
                  "derived": {"value": _dec_str(share), "from": freight_from,
                              "rule": f"estimate freight: {freight_label} "
                                      f"{money_text(freight_total, target)} {target} "
                                      f"{ESTIMATE_BASIS}"}},
                 {"name": "duty", "category": "duty", "unit": target, "currency": target,
                  "derived": {"value": _dec_str(duty), "from": from_of[oid], "rule": rule_of[oid]}}]
        try:
            record = compute_landed_cost(costs, _corrected_input(qty, unit) or {
                "citation": handle(qty), "field": qty["citation"]["field_path"], "unit": unit},
                target, as_of=as_of, resolver=resolver, actor=actor)
        except (ValidationError, ArithmeticError) as exc:
            unknown(_code_of(str(exc)), "estimate inputs",
                    "the landed-cost engine refused the estimate inputs", line=entry["name"])
            entry["steps"] = [f"refused: {_code_of(str(exc))}", label]
            continue
        for item in record["unknowns"]:
            unknown(_code_of(item["reason"]), f"estimate {item['input']}",
                    f"{item['reason']} for this reader", line=entry["name"],
                    who=item.get("who_can_answer") or "PROC")
        rules = [f"goods: {price['name']} x {qty['name']} (this line's cited inputs)",
                 f"freight share: {money_text(share, target)} {target} of the {freight_label} "
                 f"{money_text(freight_total, target)} {target}, {ESTIMATE_BASIS}",
                 f"duty: {money_text(duty, target)} {target}; {rule_of[oid]}", *notes[oid]]
        entry["steps"] = ([s[:MAX_STEP_CHARS] for s in record["steps"]][:30]
                          + [s[:MAX_STEP_CHARS] for s in rules + listed] + [label])
        entry["digest"] = record["digest"]
        try:
            outcome = LandedOutcome.from_record(record)
        except LandedCostError as exc:    # never summed: the line states no value
            unknown(_code_of(str(exc)), "estimate inputs",
                    "the landed-cost record broke its contract", line=entry["name"])
            continue
        if outcome.computed:
            assert outcome.per_unit is not None and outcome.total is not None
            entry["value"] = _dec_str(outcome.per_unit.value)
            line_totals.append(outcome.total.value)
    subtotal["inputs"] = list(dict.fromkeys(
        [e["name"] for e in freight_entries] + [e["name"] for e in lines]))[:50]
    computed = len(line_totals) == len(line_ids)
    duty_total = sum(duty_shares, Decimal(0))
    sub = goods_total + freight_total
    subtotal["steps"] = [goods_step, f"freight: {money_text(freight_total, target)} {target} "
                         f"({freight_label}), {ESTIMATE_BASIS}"[:MAX_STEP_CHARS],
                         f"subtotal: goods + freight = {money_text(sub, target)} {target}",
                         label]
    if not computed:
        subtotal["steps"].insert(-1, "not every line's cited inputs resolved for this reader "
                                     "(see the gaps); no subtotal is stated")
        total["steps"] = [f"{len(line_ids) - len(line_totals)} of {len(line_ids)} estimate "
                          "line(s) have no value; no estimate is stated", label]
        return out
    subtotal["value"] = _dec_str(sub)
    subtotal["digest"] = _sha(_canon({"steps": subtotal["steps"]}))
    estimate = sum(line_totals, Decimal(0))
    steps = [f"{e['name']}: total {money_text(v, target)} {target}"
             for e, v in zip(lines, line_totals)]
    steps = steps[:20] + ([f"... {len(steps) - 20} more line(s)"] if len(steps) > 20 else [])
    steps += [f"subtotal (goods + freight): {money_text(sub, target)} {target}",
              f"duty: {money_text(duty_total, target)} {target}", *listed,
              *([currency_step] if currency_step else [])]
    if fallback and len(fallback) == len(line_ids):
        steps.append(f"range: {money_text(sub, target)} {target} (no duty) to "
                     f"{money_text(estimate, target)} "
                     f"{target} (duty at {fallback_label})")
    elif fallback:
        at = {oid: d for oid, d in zip(line_ids, duty_shares)}
        low = estimate - sum((at[oid] for oid in fallback), Decimal(0))
        steps.append(f"range: {money_text(low, target)} {target} (no duty on the "
                     f"{len(fallback)} line(s) "
                     f"at {fallback_label.replace(' tariffRate', '')}) to "
                     f"{money_text(estimate, target)} {target} (duty at {fallback_label})")
    steps = [s[:MAX_STEP_CHARS] for s in steps][:39] + [label]
    total.update(value=_dec_str(estimate), steps=steps,
                 digest=_sha(_canon({"lines": [e.get("digest") for e in lines],
                                     "steps": steps})))
    total["inputs"] = list(dict.fromkeys([subtotal["name"]] + total["inputs"]))[:50]
    return out


def plan_check(args: Mapping[str, Any], doc: Mapping[str, Any], index: int,
               schema: Any) -> list[str]:
    """Problems with a plan's ``engine_args`` for this engine: every role names a property
    evidence item listed before the compute item, the line roles share one object type, the
    quote roles another, ``item_link`` is a declared link from the line type to the item roles'
    type, ``item_twin_link`` one between records of that type, and ``quote_link`` and
    ``confirmation_link`` each one from the quote (confirmation) type to the root type."""
    out: list[str] = []
    roles = args.get("roles") if isinstance(args, Mapping) else None
    if not isinstance(roles, Mapping):
        return ["engine_args.roles must map roles to evidence names"]
    items = {e["name"]: e for e in doc["evidence"][:index]}
    links = getattr(schema, "link_types", None) or {}
    for role in set(roles) - set(PLAN_ROLES):
        out.append(f"unknown role {role}")
    for role in set(PLAN_ROLES) - _OPTIONAL_ROLES - set(roles):
        out.append(f"role {role} is required")
    for role, name in roles.items():
        if items.get(name, {}).get("kind") != "property":
            out.append(f"role {role}: {name} is not a property evidence item listed before it")

    def types_of(names: Sequence[str]) -> set[str]:
        return {items[roles[r]]["object_type"] for r in names
                if items.get(roles.get(r) or "", {}).get("kind") == "property"}
    line_types = types_of(_LINE_ROLES)
    if len(line_types) > 1:
        out.append("unit_price, quantity and received must be read from one object type")
    from .ontology_tools_investigate import plan_root_types
    root_types = plan_root_types(doc, schema)
    line_link = links.get(args.get("line_link"))
    if schema is not None and (line_link is None or {line_link["from"]} != line_types or
                               line_link["to"] not in root_types):
        out.append("line_link must be a declared link from the line type to the root type")
    for role in ("classification", "units_per_box"):
        item = items.get(roles.get(role) or "", {})
        link = links.get(args.get("item_link"))
        if item.get("kind") == "property" and schema is not None and (
                link is None or {link["from"]} != line_types or link["to"] != item["object_type"]):
            out.append(f"item_link must be a declared link from the line type to the {role}'s "
                       "type")
    if args.get("item_twin_link") is not None:
        link = links.get(args["item_twin_link"])
        box = items.get(roles.get("units_per_box") or "", {})
        if schema is not None and (link is None or link["from"] != link["to"] or
                                   link["from"] != box.get("object_type")):
            out.append("item_twin_link must be a declared link between records of the "
                       "units_per_box's type")
    if len(types_of(SUPPLIER_ROW_ROLES)) > 1:
        out.append("the supplier row roles must be read from one object type")
    if "receipt_currency" in roles and len(types_of(
            ("receipt_freight", "receipt_duty", "receipt_currency"))) > 1:
        out.append("the receipt roles must be read from one object type")
    if set(SUPPLIER_ROW_ROLES) & set(roles) and not all(args.get(a) for a in SUPPLIER_LINK_ARGS):
        out.append("the supplier row roles need " + ", ".join(SUPPLIER_LINK_ARGS))
    if set(TIER_ROLES) & set(roles):
        tier_types = types_of(TIER_ROLES)
        if not set(TIER_ROLES) <= set(roles):
            out.append("the FileMaker tier roles need " + " and ".join(TIER_ROLES))
        if len(tier_types) > 1:
            out.append("the FileMaker tier roles must be read from one object type")
        if not args.get(TIER_LINK_ARG) or not all(args.get(a) for a in SUPPLIER_LINK_ARGS):
            out.append("the FileMaker tier roles need " + ", ".join(
                (TIER_LINK_ARG,) + SUPPLIER_LINK_ARGS))
        link = links.get(args.get(TIER_LINK_ARG))
        row_link = links.get(args.get("supplier_link"))
        if schema is not None and (link is None or {link["from"]} != tier_types or
                                   row_link is None or link["to"] != row_link["from"]):
            out.append("tier_link must be a declared link from the tier type to the supplier "
                       "row type")
    for group, first, link_arg in ((QUOTE_ROLES, "quote_amount", "quote_link"),
                                   (CONFIRMATION_ROLES, "confirmation_amount",
                                    "confirmation_link")):
        if not set(group) & set(roles):
            continue
        kind = link_arg.split("_")[0]
        group_types = types_of(group)
        link = links.get(args.get(link_arg))
        if first not in roles:
            out.append(f"the {kind} roles need {first}")
        if len(group_types) > 1:
            out.append(f"the {kind} roles must be read from one object type")
        if schema is not None and (link is None or {link["from"]} != group_types or
                                   link["to"] not in root_types):
            out.append(f"{link_arg} must be a declared link from the {kind} type to the root "
                       "type")
    return out


def _entry_text(entry: Mapping[str, Any] | None) -> str | None:
    value = (entry or {}).get("value")
    if isinstance(value, Mapping):
        value = value.get("untrusted_text")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _entry_decimal(entry: Mapping[str, Any] | None) -> Decimal | None:
    value = (entry or {}).get("value")
    if value is None or isinstance(value, (bool, float, Mapping)):
        return None
    try:
        number = Decimal(str(value).strip())
    except ArithmeticError:
        return None
    return number if number.is_finite() else None


def supplier_tariff_rate(entry: Mapping[str, Any] | None) -> Decimal | None:
    """The duty rate (a fraction in [0, 1]) a FileMaker supplier row's ``tariff_c`` states,
    only when it is written with its unit: text ``N%`` (``4%``, ``4 %``, ``6.5%``), or a
    number whose cited unit is ``%``/``percent``/``pct`` (divided by 100) or
    ``ratio``/``fraction`` (as is). Anything else (a bare number, whose unit nobody has
    stated, a float, words, a sign, a rate over 100%) is None: never guessed."""
    if entry is None:
        return None
    value = entry.get("value")
    if isinstance(value, Mapping) or (isinstance(value, str) and "%" in value):
        text = _entry_text(entry)
        match = _TARIFF_PERCENT.fullmatch(text or "")
        rate = Decimal(match.group(1)) / 100 if match else None
    else:
        scale = _TARIFF_UNITS.get(unit_token(entry.get("unit")) or "")
        number = _entry_decimal(entry)
        rate = number / scale if scale is not None and number is not None else None
    return rate if rate is not None and Decimal(0) <= rate <= Decimal(1) else None


def rate_setting_tariff(entry: Mapping[str, Any] | None) -> Decimal | None:
    """The Portal rate settings ``tariff_rate``: a decimal fraction in [0, 1] (the Portal
    stores tariffRate as a fraction, rate-masters.ts); a percent sign, a float, text or a
    value outside [0, 1] is None."""
    value = (entry or {}).get("value")
    if isinstance(value, str) and "%" in value:
        return None
    number = _entry_decimal(entry)
    return number if number is not None and Decimal(0) <= number <= Decimal(1) else None


def person_correction(entry: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The person's correction an engine taking reviewed inputs was handed in place of the
    source value (``ontology_tools_investigate._as_corrected``): the entry's ``review`` when
    it is a ``correct_value`` whose value the entry carries, else None (the entry is the
    source's value, read through its citation)."""
    review = (entry or {}).get("review")
    if not isinstance(review, Mapping) or review.get("decision") != "correct_value" or \
            "value" not in review or str(review["value"]) != str((entry or {}).get("value")):
        return None
    return review


def correction_sources(entry: Mapping[str, Any], review: Mapping[str, Any]) -> list[str]:
    """The handles a value a person corrected rests on, each re-read for the actor before the
    person's value is used: the corrected value's own citation and every record the
    correction cites."""
    handles = [entry["citation"]["l1_handle"]] if entry.get("citation") else []
    handles += [e["l1_handle"] for e in review.get("evidence") or ()
                if isinstance(e, Mapping) and isinstance(e.get("l1_handle"), str)]
    return list(dict.fromkeys(handles))


def _corrected_input(entry: Mapping[str, Any], unit: str,
                     currency: str | None = None) -> dict[str, Any] | None:
    """A ``compute_landed_cost`` input of a person's corrected value: derived from the value
    it corrects and the records the correction cites (every one resolved for the actor, else
    unknown); unknown when its value is no number (never the source's value); None when the
    entry is not a correction."""
    review = person_correction(entry)
    if review is None:
        return None
    number = _entry_decimal(entry)
    if number is None:
        return {"unknown": "CORRECTED_VALUE_UNREAD: the person's corrected value is not a "
                           "number", "who_can_answer": "PROC"}
    spec: dict[str, Any] = {"derived": {
        "value": _dec_str(number), "from": correction_sources(entry, review),
        "rule": f"a person's correction (plx.review correct_value {review.get('review_id')}) "
                f"of {entry['name']}"}, "unit": unit}
    if currency is not None:
        spec["currency"] = currency
    return spec


def _iso_date(entry: Mapping[str, Any] | None) -> str | None:
    text = _entry_text(entry)
    try:
        return date.fromisoformat(text).isoformat() if text else None
    except ValueError:
        return None


def _code_of(reason: str) -> str:
    """The typed code of an unknown reason: its upper-case prefix, else a stable code."""
    head = reason.split(":", 1)[0].strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", head):
        return head
    if head.startswith("fx_"):
        return "MIXED_CURRENCY_NO_CITED_RATE"
    if head == "goods_cost_not_positive":
        return "UNPRICED_LINE"
    return re.sub(r"[^A-Z0-9_]", "_", head.upper())[:64] or "UNKNOWN"


def _equal_at(value: Decimal, stated: Decimal) -> bool:
    """``value`` rounded to the precision ``stated`` is written at equals it."""
    try:
        return value.quantize(stated) == stated
    except ArithmeticError:
        return False


def _quote_counts(quote: Mapping[str, Mapping[str, Any] | None], basis: str | None
                  ) -> list[tuple[Decimal, Mapping[str, Any]]]:
    """Every cited count of the quote's basis unit, with the answer entry citing it: a typed
    count field whose unit is that unit, and a basis line that is itself a count. A count in
    another unit, or a fraction of a container or pallet, is not one."""
    if basis is None or basis == "shipment":
        return []
    container = basis[:2] in ("20", "40", "45")
    units = CONTAINER_UNITS if container else _BASIS_COUNT_UNITS[basis]
    found: list[tuple[Decimal, Mapping[str, Any]]] = []
    for role in _COUNT_ROLES:
        entry = quote[role]
        value = _entry_decimal(entry)
        if entry is not None and value is not None and unit_token(entry.get("unit")) in units:
            found.append((value, entry))
    stated = basis_text_count(_entry_text(quote["quote_basis"]), basis)
    if stated is not None:
        found.append((stated, quote["quote_basis"]))
    whole = container or basis in _WHOLE_BASES
    return [(value, entry) for value, entry in found
            if value > 0 and (not whole or value == value.to_integral_value())]


def _quote_record(quote: Mapping[str, Mapping[str, Any] | None], *, as_of: str,
                  target: str | None) -> tuple[dict[str, Any], list[tuple[str, dict[str, str]]],
                                               tuple[Decimal, str] | None,
                                               list[dict[str, Any]]]:
    """(answer-entry fields, typed unknowns with their template values, the (amount,
    currency) of a quote that is not total freight, its conflicts as ``{about, inputs: [answer
    entry names], note}``) for one forwarder quote's typed fields (``quote``: role -> its
    answer entry).

    The count of the basis unit is read from every typed count field cited in that unit
    (containers, volume, chargeable weight, pallets) and from a basis line that is itself a
    count ("1 x 40HQ"); two different counts are a conflict and none is used. An amount read
    under a total label is never multiplied. On a cited basis other than per shipment (an
    unread basis included) it is the total only when a cited rate in its currency times the
    cited count equals it at its precision, or the cited count is one whole container or pallet
    (one unit-shipment, never 1 CBM or 1 kg) and no cited rate disagrees. A cited rate times
    the count that differs from it is a conflict; otherwise it is ``BASIS_NOT_TOTAL``, whatever
    the label. A rate (``quote_rate``) is a price
    per basis unit: it becomes a total only times a cited count of that unit."""
    def money(entry: Mapping[str, Any] | None) -> tuple[Decimal, str] | None:
        value, currency = _entry_decimal(entry), (entry or {}).get("currency")
        return (value, currency) if value is not None and value > 0 and \
            currency in ISO_4217 else None
    stated, rate = money(quote["quote_amount"]), money(quote["quote_rate"])
    steps = [FREIGHT_LABELS["quote"]]
    if stated is None and rate is None:
        steps.append("not total freight: FREIGHT_QUOTE_AMOUNT_UNREAD (see the gaps)")
        return {"steps": steps}, [("FREIGHT_QUOTE_AMOUNT_UNREAD", {})], None, []
    amount_entry = quote["quote_amount"] if stated is not None else quote["quote_rate"]
    amount, currency = stated if stated is not None else rate
    covered, _ = freight_scope_terms(_entry_text(quote["quote_includes"]))
    covered |= freight_scope_terms(_entry_text(quote["quote_scope"]))[0]
    excluded, unread = freight_scope_terms(_entry_text(quote["quote_excludes"]))
    basis_text = _entry_text(quote["quote_basis"])
    basis = freight_basis(basis_text)
    whole = basis is not None and (basis[:2] in ("20", "40", "45") or basis in _WHOLE_BASES)
    # A minimum, maximum or second figure on the basis line: no count of it is applied.
    terms = basis_terms_unread(basis_text, basis)
    readings = [] if terms else _quote_counts(quote, basis)
    distinct = sorted({value for value, _ in readings})
    count_value = distinct[0] if len(distinct) == 1 else None
    per_unit = stated is not None and bool(basis_text) and basis != "shipment"
    # A stated amount on a basis other than per shipment: the total only when it is cited so.
    product = rate[0] * count_value if per_unit and rate is not None and \
        rate[1] == currency and count_value is not None else None
    agrees = product is not None and _equal_at(product, amount)
    rate_conflict = product is not None and not agrees
    not_total = per_unit and (terms or not (agrees or whole and count_value == 1 and
                                            not rate_conflict))
    count = ({"count": count_value, "_source": next(
                 e["citation"]["l1_handle"] for v, e in readings if v == count_value)}
             if stated is None and basis not in (None, "shipment") and count_value is not None
             else None)
    valid_until = _iso_date(quote["quote_valid_until"])
    found: list[tuple[str, dict[str, str]]] = []
    conflicts: list[dict[str, Any]] = []
    unit_label = basis or "an unread basis"
    if stated is None and basis is None:
        found.append(("FREIGHT_BASIS_UNREAD", {}))
    if terms:
        found.append(("FREIGHT_BASIS_TERMS_UNREAD", {"entry": quote["quote_basis"]["name"],
                                                     "basis": basis}))
    if len(distinct) > 1:
        found.append(("FREIGHT_BASIS_COUNTS_CONFLICT", {"counts": str(len(distinct)),
                                                        "basis": unit_label}))
        conflicts.append({"about": f"counts of {unit_label}",
                          "inputs": list(dict.fromkeys(e["name"] for _, e in readings)),
                          "note": f"the quote cites {len(distinct)} different counts of "
                                  f"{unit_label} ({', '.join(map(_dec_str, distinct))}); none "
                                  "is chosen"})
    if rate_conflict:
        found.append(("FREIGHT_RATE_COUNT_CONFLICT", {"basis": unit_label}))
        conflicts.append({"about": "stated amount vs rate x count", "inputs": list(dict.fromkeys(
            [quote["quote_amount"]["name"], quote["quote_rate"]["name"],
             *(e["name"] for v, e in readings if v == count_value)])),
            "note": f"rate {_dec_str(rate[0])} {currency} x {_dec_str(count_value)} "
                    f"{unit_label} = {_dec_str(product)} {currency}, not the stated amount "
                    f"{_dec_str(amount)} {currency}; neither is chosen"})
    elif not_total and len(distinct) <= 1 and not terms:
        found.append(("BASIS_NOT_TOTAL", {"priced": f"per {basis}" if basis else
                                          "on a basis the Workbench does not read"}))
    if unread:
        found.append(("FREIGHT_EXCLUSIONS_UNREAD", {"unread": str(unread)}))
    checks = [f"basis check: {conflicts[0]['note']}"] if len(distinct) > 1 else []
    if terms:
        checks.append(f"basis check: the basis line states a term besides its {basis} unit "
                      "(a minimum, a maximum or a second figure), which is not applied")
    if rate_conflict:
        checks.append(f"basis check: {conflicts[-1]['note']}")
    elif agrees:
        checks.append(f"basis check: rate {_dec_str(rate[0])} {currency} x "
                      f"{_dec_str(count_value)} {basis} = the stated total")
    elif per_unit and not not_total:
        checks.append(f"basis check: the cited count is 1 {basis}")
    steps += [f"stated amount: {_dec_str(amount)} {currency} (rate basis "
              f"{basis or 'not read'}; not cited as the shipment total)" if not_total else
              f"quoted total: {_dec_str(amount)} {currency} (the stated total, never "
              f"multiplied; rate basis {basis or 'not read'})" if stated is not None else
              f"quoted rate: {_dec_str(amount)} {currency} per {unit_label}", *checks,
              f"scope cited: includes {_components(covered) or 'no component named'}; excludes "
              f"{_components(excluded) or 'no component named'}",
              f"valid until {valid_until} (as of {as_of})" if valid_until else
              "validity: not read"]
    try:
        assessed = assess_freight_quote(
            {"_source": amount_entry["citation"]["l1_handle"], "amount": str(amount),
             "currency": currency, "basis": basis if stated is None and basis else "shipment",
             "includes": sorted(covered), "excludes": sorted(excluded),
             "valid_until": valid_until, "label": _entry_text(quote["quote_scope"]) or ""},
            as_of=as_of, basis_count=count)
    except LandedCostError:
        assessed = None
        found.append(("FREIGHT_SCOPE_CONTRADICTS", {}))
    try:
        scope = FreightQuoteScope.from_assessment(assessed) if assessed is not None else None
    except LandedCostError:   # an assessment that contradicts itself stands as nothing
        assessed = scope = None
        found.append(("FREIGHT_SCOPE_CONTRADICTS", {}))
    if assessed is not None:
        params = {"covered": _components(covered) or "no freight component it names",
                  "missing": _components(set(TOTAL_FREIGHT) - covered),
                  "left": _components((set(TOTAL_FREIGHT) - covered) | excluded),
                  "valid_until": valid_until or "", "as_of": as_of, "basis": basis or ""}
        found += [(code, params) for code in scope.open_codes
                  if len(distinct) <= 1 and not terms or
                  code != "FREIGHT_BASIS_COUNT_UNKNOWN"]
        if stated is None and basis not in (None, "shipment") and count is None and \
                len(distinct) <= 1 and not terms:
            found.append(("PACK_SPEC_NOT_SERVED", params))
    usable = scope is not None and scope.usable_as_total_freight and not found
    out: dict[str, Any] = {"unit": currency, "currency": currency}
    if usable:
        assert scope is not None and scope.total is not None
        total = _dec_str(scope.total.value)
        out["value"] = total
        steps.append(f"total freight: {_dec_str(amount)} {currency} x "
                     f"{_dec_str(count['count'])} {basis} = {total} {currency}" if count else
                     f"total freight: {total} {currency} (the quote's stated total)")
    else:
        steps.append("not total freight: " + ", ".join(dict.fromkeys(c for c, _ in found))
                     + " (see the gaps)")
    if scope is None or scope.duties_and_taxes_separate:
        steps.append("duties and taxes: not in this quote; a separate duty input, never freight")
    else:
        steps.append("duties and taxes: included in this quote (one freight-and-duty charge); "
                     "no separate duty is added beside it")
    if target is not None and currency != target:
        steps.append(f"the quote is in {currency}, the purchase order in {target}: no exchange "
                     "rate is projected, so it is not compared with the estimate")
    out["steps"] = steps
    # read by the caller (``quote_totals``), never an answer-entry field
    out["_duties_included"] = scope is not None and not scope.duties_and_taxes_separate
    out["digest"] = _sha(_canon({"amount": _dec_str(amount), "stated_total": stated is not None,
                                 "currency": currency, "basis": basis,
                                 "count": None if count_value is None else _dec_str(count_value),
                                 "includes": sorted(covered), "excludes": sorted(excluded),
                                 "valid_until": valid_until, "as_of": as_of,
                                 "unknowns": [c for c, _ in found], "steps": steps}))
    return out, found, None if usable else (amount, currency), conflicts


def _evidence_edge(edge: Mapping[str, Any]) -> bool:
    """An edge an amount may rest on: no tier stated (a stored link) or a tier at or above the
    traversal's default floor (``stored_by_text_join``); a lead-tier edge (an unconfirmed
    twin, a text match) never is."""
    from .ontology_tools_traverse import DEFAULT_TIER_FLOOR, TIER_ORDER
    tier = edge.get("tier")
    return tier is None or tier in TIER_ORDER[:TIER_ORDER.index(DEFAULT_TIER_FLOOR) + 1]


def _closed(seeds: set[str], pairs: Sequence[tuple[str, str]]) -> set[str]:
    """``seeds`` with every record a twin pair joins to them, transitively."""
    out, grew = set(seeds), True
    while grew:
        grew = False
        for one, other in pairs:
            if (one in out) != (other in out):
                out |= {one, other}
                grew = True
    return out


def _supplier_rows(args: Mapping[str, Any], edges: Sequence[Mapping[str, Any]],
                   line_ids: Sequence[str], po_ids: set[str]) -> dict[str, list[str]]:
    """The FileMaker replica supplier rows of each line: a row (``supplier_item_link``) for the
    line's item (``item_link``, closed over ``item_twin_link``) offered (``supplier_link``) by
    the purchase order's own supplier (``po_supplier_link`` from the order, closed over
    ``supplier_twin_link``). Only edges an amount may rest on count; a plan naming none of the
    three supplier links reads no row."""
    if not all(args.get(arg) for arg in SUPPLIER_LINK_ARGS):
        return {oid: [] for oid in line_ids}
    strong = [e for e in edges if _evidence_edge(e) and isinstance(e.get("from"), str)
              and isinstance(e.get("to"), str)]

    def pairs(arg: str) -> list[tuple[str, str]]:
        return [(e["from"], e["to"]) for e in strong if args.get(arg)
                and e.get("link_type") == args[arg]]
    suppliers = _closed({to for one, to in pairs("po_supplier_link") if one in po_ids},
                        pairs("supplier_twin_link"))
    offered = {}
    for row, supplier in pairs("supplier_link"):
        offered.setdefault(row, set()).add(supplier)
    item_twins = pairs("item_twin_link")
    out: dict[str, list[str]] = {}
    for oid in line_ids:
        items = _closed({to for one, to in pairs("item_link") if one == oid}, item_twins)
        out[oid] = sorted({row for row, item in pairs("supplier_item_link")
                           if item in items and offered.get(row, set()) & suppliers})
    return out


def _row_inputs(got: Mapping[str, Sequence[Mapping[str, Any]]],
                rows_of: Mapping[str, Sequence[str]]) -> dict[str, dict[str, Any]]:
    """Per line, the supplier rows' duty inputs (``_pre_receipt_estimate``'s ``rows``): the
    one rate their ``tariff_c`` states (``supplier_tariff_rate``), its entries, a ``problem``
    (code, detail) when a row's rate is unreadable or the rows state different rates (then no
    rate), and the rows' HS code and origin entries. Details name entries, never values."""
    out: dict[str, dict[str, Any]] = {}
    for oid, rows in rows_of.items():
        def on_rows(role: str) -> list[Mapping[str, Any]]:
            return [e for e in got[role] if e.get("object_id") in rows
                    and e.get("value") is not None]
        # An empty tariff_c states no rate: absent, as a row with no tariff_c is, never a gap.
        tariffs = [e for e in on_rows("supplier_tariff") if not _blank(e.get("value"))]
        rates = [supplier_tariff_rate(e) for e in tariffs]
        names = ", ".join(e["name"] for e in tariffs)[:200]
        problem = None
        if any(r is None for r in rates):
            bad = ", ".join(e["name"] for e, r in zip(tariffs, rates) if r is None)[:200]
            problem = ("SUPPLIER_TARIFF_UNREADABLE",
                       f"{bad} (tariff_c on the FileMaker replica supplier row) is not written "
                       "as a rate with its unit (N%, or a value cited in %, percent or ratio, "
                       "between 0 and 100%); it is not applied")
        elif len(set(rates)) > 1:
            problem = ("SUPPLIER_TARIFF_RECORDS_DISAGREE",
                       f"the supplier rows of the order's supplier for the item ({names}) state "
                       f"{len(set(rates))} different tariff_c rates; none is chosen")
        out[oid] = {"rate": rates[0] if rates and problem is None else None,
                    "entries": tariffs if problem is None else [], "problem": problem,
                    "hs": [e for e in on_rows("supplier_hs_code") if _entry_text(e)],
                    "origin": [e for e in on_rows("supplier_origin") if _entry_text(e)]}
    return out


def _blank(value: Any) -> bool:
    """A cell stating nothing: text (or untrusted text) that is empty or whitespace only."""
    if isinstance(value, Mapping) and set(value) <= {"untrusted_text"}:
        value = value.get("untrusted_text")
        return value is None or (isinstance(value, str) and not value.strip())
    return isinstance(value, str) and not value.strip()


def _rate_setting(found: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The Portal rate settings ``tariff_rate`` (``_pre_receipt_estimate``'s ``setting``):
    the one rate every cited value states, else a ``problem`` and no rate; None when no value
    was read."""
    stated = [e for e in found if e.get("value") is not None]
    if not stated:
        return None
    rates = [rate_setting_tariff(e) for e in stated]
    names = ", ".join(e["name"] for e in stated)[:200]
    if any(r is None for r in rates):
        bad = ", ".join(e["name"] for e, r in zip(stated, rates) if r is None)[:200]
        return {"rate": None, "entries": [], "problem":
                f"{bad} (public.app_rate_settings tariff_rate) is not a decimal fraction "
                "between 0 and 1; it is not applied"}
    if len(set(rates)) > 1:
        return {"rate": None, "entries": [], "problem":
                f"the rate settings records ({names}) state {len(set(rates))} different "
                "tariff_rate values; none is applied"}
    return {"rate": rates[0], "entries": stated, "problem": None}


def _fm_tiers(args: Mapping[str, Any], edges: Sequence[Mapping[str, Any]],
           rows_of: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Per line, the FileMaker price tiers (``tierpricing``) of its supplier rows: a tier
    joined (``tier_link``, tier -> row) to one of the line's rows by an edge an amount may
    rest on. A plan naming no ``tier_link`` reads no tier."""
    link = args.get(TIER_LINK_ARG)
    if not link:
        return {oid: [] for oid in rows_of}
    joined = [(e["from"], e["to"]) for e in edges if e.get("link_type") == link
              and _evidence_edge(e) and isinstance(e.get("from"), str)
              and isinstance(e.get("to"), str)]
    return {oid: sorted({tier for tier, row in joined if row in rows})
            for oid, rows in rows_of.items()}


def _chosen_tier(tiers: Sequence[str], ordered: Mapping[str, Any] | None,
                 least: Sequence[Mapping[str, Any]], figures: Sequence[Mapping[str, Any]],
                 reader: "_Reader") -> tuple[Mapping[str, Any] | None, str | None]:
    """The FileMaker figure of the tier the line's ordered quantity reaches, else why not.

    The rule (the Portal's
    ``pickSupplierCardTierPrice``): the tier with the highest minimum quantity at or below
    the ordered quantity, in Decimal, with no fallback. Every tier's minimum quantity must be
    a number cited in the line's ordered unit and re-read for the reader (a tier it cannot
    read could be the one reached); tiers sharing that minimum must state one figure."""
    count, unit = _entry_decimal(ordered), unit_token((ordered or {}).get("unit"))
    if not tiers:
        return None, "no FileMaker tier price (tierpricing) on the supplier row"
    if count is None or unit is None or count <= 0:
        return None, "no ordered quantity to choose a FileMaker tier by"
    minimum: dict[str, Decimal] = {}
    for tier in tiers:
        stated = [e for e in least if e.get("object_id") == tier and e.get("value") is not None]
        values = {_entry_decimal(e) for e in stated}
        if not stated or None in values or len(values) > 1:
            return None, "a FileMaker tier quantity is not one readable number"
        if any(unit_token(e.get("unit")) != unit for e in stated):
            return None, "a FileMaker tier quantity is not in the line's ordered unit"
        if not all(e.get("citation") and reader.resolve(e["citation"]["l1_handle"]) is not None
                   for e in stated):
            return None, "a FileMaker tier quantity is not readable for this reader"
        minimum[tier] = values.pop()
    reached = [m for m in minimum.values() if m <= count]
    if not reached:
        return None, "no FileMaker tier starts at or below the ordered quantity"
    chosen = [t for t, m in minimum.items() if m == max(reached)]
    found = [e for e in figures if e.get("object_id") in chosen]
    if not found:
        return None, "the FileMaker tier the ordered quantity reaches records no figure"
    if len({str(e["value"]) for e in found}) > 1:
        return None, "the FileMaker tiers the ordered quantity reaches state different figures"
    return found[0], None


def _fm_agreement(*, name: str, target: str | None, line_ids: Sequence[str],
                  quantities: Mapping[str, Mapping[str, Any]],
                  tiers_of: Mapping[str, Sequence[str]], found: Sequence[Mapping[str, Any]],
                  least: Sequence[Mapping[str, Any]],
                  ours: Mapping[str, tuple[Any, str]], currency_handles: Sequence[str],
                  resolver: Resolver | None, actor: Any) -> list[dict[str, Any]]:
    """FileMaker's recorded landed cost (``dlt_costlanded_c`` on the price tier of the line's
    supplier row its ordered quantity reaches, ``_chosen_tier``) against ours, per line, as
    counts only: agree (within ``FM_AGREEMENT_TOLERANCE`` of FileMaker's figure per unit),
    differ, or missing (with why). No amount is stated: the entry's value is null and its
    steps name lines, outcomes and the tier figure compared. FileMaker's figure is re-read
    for the actor; a line whose figure or whose own amount this reader cannot read is
    missing. FileMaker's figure is what FileMaker recorded, never an actual. Empty when the
    run reached no FileMaker figure."""
    stated = [e for e in found if e.get("value") is not None]
    if not stated:
        return []
    reader = _Reader(resolver, actor)
    ours_readable = all(reader.resolve(h) is not None for h in currency_handles)
    steps: list[str] = []
    counts = {"agree": 0, "differ": 0, "missing": 0}
    inputs: list[str] = []
    basis: set[str] = set()
    for index, oid in enumerate(line_ids, start=1):
        label = f"{name}_line_{index}"[:64]
        value, mode = ours.get(oid, (None, "estimate"))
        unit = unit_token((quantities.get(oid) or {}).get("unit"))
        entry, outcome = _chosen_tier(tiers_of.get(oid, ()), quantities.get(oid), least,
                                      stated, reader)
        outcome = f"missing ({outcome})" if outcome else None
        if outcome is None and (target is None or unit is None or
                                entry.get("currency") != target or
                                _fm_unit(entry.get("unit"), target) != unit):
            outcome = "missing (FileMaker's figure is not in the line's currency per its unit)"
        elif outcome is None and (value is None or not ours_readable):
            outcome = f"missing (no {mode} for this reader)"
        if outcome is None:
            try:
                _, fm, _, why = reader.read(entry["name"], {
                    "citation": entry["citation"]["l1_handle"],
                    "field": entry["citation"]["field_path"],
                    "unit": f"{target}/{unit}", "currency": target})
            except (ValidationError, ArithmeticError, ValueError, KeyError, TypeError):
                fm, why = None, "unreadable"
            if fm is None or why is not None or fm <= 0:
                outcome = "missing (FileMaker's figure is not a readable amount for this reader)"
            else:
                agree = abs(Decimal(str(value)) - fm) <= fm * FM_AGREEMENT_TOLERANCE
                outcome = (f"{'agree' if agree else 'differ'} (our {mode} against FileMaker's "
                           f"recorded dlt_costlanded_c {entry['name']})")
            inputs.append(entry["name"])
            basis.add(entry["object_id"])
        counts[outcome.split(" ", 1)[0]] += 1
        steps.append(f"{label}: {outcome}"[:MAX_STEP_CHARS])
        inputs.append(label)
    shown = steps[:38] + ([f"... {len(steps) - 38} more line(s)"] if len(steps) > 38 else [])
    shown.append(f"counts: agree {counts['agree']}, differ {counts['differ']}, missing "
                 f"{counts['missing']} (within {_dec_str(FM_AGREEMENT_TOLERANCE * 100)}% of "
                 "FileMaker's recorded dlt_costlanded_c per unit, at the tier the ordered "
                 "quantity reaches; counts only, no amount is stated)")
    return [{"name": f"{name}_fm_agreement"[:64], "kind": "computed", "value": None,
             "inputs": list(dict.fromkeys(inputs))[:50], "steps": shown,
             "basis": sorted(basis)[:100], "check": "not_applicable"}]


def _fm_unit(unit: Any, target: str) -> str | None:
    """The quantity unit a FileMaker per-unit figure is cited per: ``ea``, or ``USD/ea`` in
    the target currency; None for anything else."""
    if not isinstance(unit, str):
        return None
    if "/" in unit:
        currency, per = unit.split("/", 1)
        return unit_token(per) if currency.strip().upper() == target else None
    return unit_token(unit)


_ACTUALS_SUBJECT = "purchase order recorded charges"
_GAP_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}")


HISTORY_LEVEL_TEXT = {"supplier": "this purchase order's supplier",
                      "country": "suppliers in the supplier's recorded country",
                      "all": "all suppliers (company-wide)"}
HISTORY_METHOD_TEXT = {
    "median_of_po_rates": "median effective rate of POs with charges linked by reference",
    "pooled_period_ratio": "Sage AP charges / goods received, pooled by quarter (no charge "
                           "names a PO)"}
_HISTORY_KEYS = frozenset(("rate", "level", "method", "n", "unit", "period", "median", "q1", "q3",
                           "includes_brokerage"))
_HISTORY_PERIOD = re.compile(r"\d{4}-(?:\d{2}|Q[1-4])\.\.\d{4}-(?:\d{2}|Q[1-4])|undated")


def _history_estimate(rec: Any, part: str, *, default: bool = False) -> dict[str, Any] | None:
    """A capture-built history estimate (``duty_actual.history_from_rates`` /
    ``aggregate_ratio``), validated, or None: nothing unknown is accepted (an extra or missing
    key, a level or method the engine does not word, a rate outside (0, the kind's ceiling], a
    spread outside [0, ceiling] or out of order, a sample below
    ``duty_actual.HISTORY_MIN_SAMPLE`` of its unit (``_history_too_small``), a period not in
    its form). A company-wide ``default`` must be level ``all``."""
    from . import duty_actual as da
    if not isinstance(rec, Mapping) or set(rec) != _HISTORY_KEYS:
        return None
    if rec["level"] not in HISTORY_LEVEL_TEXT or (default and rec["level"] != "all") \
            or rec["method"] not in HISTORY_METHOD_TEXT \
            or da.HISTORY_METHODS.get(rec["method"]) != rec["unit"] \
            or not isinstance(rec["n"], int) or isinstance(rec["n"], bool) \
            or rec["n"] < da.HISTORY_MIN_SAMPLE[rec["unit"]] \
            or not isinstance(rec["period"], str) \
            or not _HISTORY_PERIOD.fullmatch(rec["period"]) \
            or not isinstance(rec["includes_brokerage"], bool):
        return None
    ceiling = Decimal(da.kind_rules(part)["max_plausible_rate"])
    values: dict[str, Decimal] = {}
    for key in ("rate", "median", "q1", "q3"):
        if not isinstance(rec[key], str):
            return None
        try:
            values[key] = Decimal(rec[key])
        except ArithmeticError:
            return None
        if not values[key].is_finite() or values[key] < 0 or values[key] > ceiling:
            return None
    if values["rate"] <= 0 or not values["q1"] <= values["median"] <= values["q3"]:
        return None
    return {**{k: rec[k] for k in ("level", "method", "n", "unit", "period",
                                    "includes_brokerage")},
            **{k: _dec_str(v) for k, v in values.items()}}


def _history_too_small(rec: Any) -> bool:
    """True when ``rec`` is a history estimate whose sample (``n`` of a known unit) is below
    ``duty_actual.HISTORY_MIN_SAMPLE``: too few purchase orders or quarters to estimate from."""
    from . import duty_actual as da
    if not isinstance(rec, Mapping):
        return False
    n, floor = rec.get("n"), da.HISTORY_MIN_SAMPLE.get(str(rec.get("unit")))
    return isinstance(n, int) and not isinstance(n, bool) and floor is not None and n < floor


def _history_sample_gap(part: str, unknown: Callable[..., None], small: int) -> None:
    from . import duty_actual as da
    unknown("HISTORY_SAMPLE_TOO_SMALL", f"history {part} estimate",
            f"{small} {part} estimate(s) from PLX history in the capture rest on fewer than "
            f"{da.HISTORY_MIN_SAMPLE['purchase_orders']} purchase orders or "
            f"{da.HISTORY_MIN_SAMPLE['quarters']} quarters; none of them is used",
            holds=False, who="FIN")


def _history_label(est: Mapping[str, Any], part: str) -> str:
    """The basis of a history estimate, worded: level, method, sample, period and spread."""
    return (f"ESTIMATED FROM PLX HISTORY ({part}), not recorded for this PO: "
            f"{HISTORY_LEVEL_TEXT[est['level']]}, {HISTORY_METHOD_TEXT[est['method']]}; rate "
            f"{est['rate']} over {est['n']} {est['unit'].replace('_', ' ')}, {est['period']}; "
            f"spread median {est['median']}, IQR {est['q1']} to {est['q3']}"
            + ("; includes brokerage"
               if est["includes_brokerage"] else ""))


def _actuals_note(got: Mapping[str, Any]) -> str:
    return f"capture {str(got.get('digest'))[:16]} built {str(got.get('built_at'))[:32]}"


def _history_defaults(got: Mapping[str, Any], cite: str, unknown: Callable[..., None],
                      note: str) -> dict[str, dict[str, Any]]:
    """The capture's company-wide history estimates (level ``all``), validated and labelled,
    for a reader shown no recorded charge; each part without a usable one is named."""
    defaults = got.get("default_estimates")
    defaults = defaults if isinstance(defaults, Mapping) else {}
    history: dict[str, dict[str, Any]] = {}
    for part in ("duty", "freight"):
        chosen = (_history_estimate(defaults[part], part, default=True)
                  if defaults.get(part) is not None else None)
        if chosen is None and _history_too_small(defaults.get(part)):
            _history_sample_gap(part, unknown, 1)
        elif defaults.get(part) is not None and chosen is None:
            unknown("HISTORY_ESTIMATE_UNUSABLE", f"history {part} estimate",
                    f"the company-wide {part} estimate from PLX history in the capture does "
                    "not validate; it is not used", holds=False, who="FIN")
        if chosen is None:
            unknown("HISTORY_ESTIMATE_NOT_AVAILABLE", f"history {part} estimate",
                    f"the capture holds no usable company-wide {part} estimate from PLX "
                    "history", holds=False, who="FIN")
            continue
        history[part] = {**chosen, "basis": "estimated_from_history", "_source": cite,
                         "label": _history_label(chosen, part), "note": note}
    return history


def _recorded_actuals(numbers: Sequence[str], landed_actuals: Callable[[Sequence[str]], Any],
                      cite: str, unknown: Callable[..., None]
                      ) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None,
                                 dict[str, dict[str, Any]]]:
    """The purchase order's recorded duty and freight rates and its incoterm from the
    capture-built charges (``duty_actual.ActualsFile``, through the reader's finance gate),
    looked up by the PO numbers its own records state. -> (``{part: actual}``, incoterm,
    ``{part: history estimate}``).

    A part with no usable actual gets the estimate from PLX history the capture resolved for
    the order (its supplier's, else its country's), else the capture's company-wide estimate;
    numbers of one order that carry different estimates fall back to the company-wide one
    (HISTORY_ESTIMATES_DISAGREE). An estimate that does not validate is named
    (HISTORY_ESTIMATE_UNUSABLE) and never used; none at all is HISTORY_ESTIMATE_NOT_AVAILABLE.
    Each history estimate is labelled ``estimated_from_history`` with its basis.

    Every outcome that is not a usable rate is a named gap that does not by itself make the
    answer partial (the line's freight or duty gap does): a reader who lacks the finance
    marking (ACTUAL_CHARGES_WITHHELD), no capture on this host, a number the capture never
    linked against, numbers of one order carrying different charges, a rate the capture
    flagged (its plausibility code) or one that is not a rate within the kind's ceiling, and
    no recorded incoterm. A usable actual cites ``cite`` (a record of this purchase order the
    run read for this reader) and names the capture it came from."""
    from . import duty_actual as da
    from . import hs_tariff
    keys = sorted({k for k in (da.po_key(n) for n in numbers) if k})
    if not keys:
        unknown("PO_NUMBER_UNCITED", _ACTUALS_SUBJECT,
                "no record of the purchase order states its number, so no recorded duty or "
                "freight charge is looked up", "po_number", holds=False, who="PROC")
        return {}, None, {}
    got = landed_actuals(keys)
    got = got if isinstance(got, Mapping) else {"unavailable": "LANDED_ACTUALS_UNREADABLE"}
    if got.get("withheld"):
        unknown("ACTUAL_CHARGES_WITHHELD", _ACTUALS_SUBJECT,
                "recorded duty and freight charges are money, which carries the finance "
                "marking this reader does not hold; a reader holding it gets them",
                holds=False, who="FIN")
        return {}, None, {}
    if got.get("actuals_withheld"):
        # A purchasing reader: the recorded charges stay finance-only; the labelled
        # company-wide estimate from PLX history (a rate, no per-transaction record) applies.
        unknown("ACTUAL_CHARGES_WITHHELD", _ACTUALS_SUBJECT,
                "recorded duty and freight charges are money, which carries the finance "
                "marking this reader does not hold; a reader holding it gets them, and only "
                "the company-wide estimate from PLX history is used here",
                holds=False, who="FIN")
        return {}, None, _history_defaults(got, cite, unknown, _actuals_note(got))
    if got.get("unavailable") or not isinstance(got.get("records"), Mapping):
        code = str(got.get("unavailable") or "LANDED_ACTUALS_UNREADABLE")
        code = code if _GAP_CODE.fullmatch(code) else "LANDED_ACTUALS_UNREADABLE"
        unknown(code, _ACTUALS_SUBJECT,
                "the recorded duty and freight charges (Sage AP lines linked to purchase "
                "orders by a recorded reference at capture time) are not readable on this "
                "host, so none is used", holds=False, who="FIN")
        return {}, None, {}
    records = {k: r for k, r in got["records"].items() if k in keys and isinstance(r, Mapping)}
    if not set(got.get("known") or ()) & set(keys):
        unknown("PO_NOT_IN_CHARGE_CAPTURE", _ACTUALS_SUBJECT,
                "the purchase order's number is not one the recorded charges were linked "
                "against at capture time, so no charge was looked for", holds=False, who="FIN")
    note = _actuals_note(got)
    out: dict[str, dict[str, Any]] = {}
    for part in ("duty", "freight"):
        found = [r[part] for r in records.values() if isinstance(r.get(part), Mapping)]
        if len(found) > 1:
            unknown("ACTUAL_CHARGES_ON_SEVERAL_PO_NUMBERS", f"recorded {part}",
                    f"{len(found)} numbers of this purchase order each carry recorded {part} "
                    "charges; none is chosen", holds=False, who="FIN")
            continue
        if not found:
            continue
        rec = found[0]
        ceiling = Decimal(da.kind_rules(part)["max_plausible_rate"])
        try:
            rate = Decimal(str(rec.get("rate")))
        except ArithmeticError:
            rate = None
        if rec.get("rate") is None or rate is None or not rate.is_finite() or rate <= 0 \
                or rate > ceiling:
            flag = str(rec.get("gap") or "RATE_NOT_A_PLAUSIBLE_NUMBER")
            flag = flag if _GAP_CODE.fullmatch(flag) else "RATE_NOT_A_PLAUSIBLE_NUMBER"
            unknown(f"ACTUAL_{part.upper()}_UNUSABLE", f"recorded {part}",
                    f"recorded {part} charges are linked to the purchase order, but no "
                    f"usable rate is derived from them ({flag}); none is used",
                    holds=False, who="FIN")
            continue
        # charges the BC snapshot stated no posting for: the rate stands, and says so
        unstated = rec.get("posting_unknown")
        caveat = (f"; posting unknown for {unstated} of its {rec.get('charges')} charges "
                  "(BC did not state it)" if unstated else "")
        out[part] = {"rate": _dec_str(rate), "basis": "actual_recorded", "_source": cite,
                     "includes_brokerage": bool(rec.get("includes_brokerage")),
                     "note": note + caveat, "posting_caveat": caveat}
    terms = {(str((r.get("incoterm") or {}).get("po") or ""),
              str((r.get("incoterm") or {}).get("vendor_default") or ""))
             for r in records.values() if isinstance(r.get("incoterm"), Mapping)}
    incoterm = None
    if len(terms) > 1:
        unknown("INCOTERMS_DISAGREE", "purchase order incoterm",
                f"{len(terms)} numbers of this purchase order record different incoterms; none "
                "is applied", holds=False, who="PROC")
    else:
        po_term, vendor_term = next(iter(terms), ("", ""))
        resolved = hs_tariff.resolve_incoterm(po_term, vendor_term)
        if resolved.get("bearing"):
            incoterm = {**resolved, "source": cite}
        else:
            unknown(resolved.get("gap") or "INCOTERM_NOT_RECORDED", "purchase order incoterm",
                    "neither the purchase order nor its supplier records an incoterm the rules "
                    "state, so who bears freight and duty follows no recorded term"
                    if resolved.get("gap") != "INCOTERM_RULE_NOT_RECORDED" else
                    "the recorded incoterm is not one the incoterm rules state; it is not "
                    "applied", holds=False, who="PROC")
    history: dict[str, dict[str, Any]] = {}
    defaults = got.get("default_estimates")
    defaults = defaults if isinstance(defaults, Mapping) else {}
    for part in ("duty", "freight"):
        if part in out:
            continue
        found: list[dict[str, Any]] = []
        bad = small = 0
        for r in records.values():
            raw = r.get("estimate")
            raw = raw.get(part) if isinstance(raw, Mapping) else None
            if raw is None:
                continue
            checked = _history_estimate(raw, part)
            if checked is None and _history_too_small(raw):
                small += 1
            elif checked is None:
                bad += 1
            elif checked not in found:
                found.append(checked)
        chosen = found[0] if len(found) == 1 else None
        if len(found) > 1:
            unknown("HISTORY_ESTIMATES_DISAGREE", f"history {part} estimate",
                    f"{len(found)} numbers of this purchase order carry different {part} "
                    "estimates from PLX history; the company-wide estimate applies",
                    holds=False, who="FIN")
        if chosen is None and defaults.get(part) is not None:
            chosen = _history_estimate(defaults[part], part, default=True)
            if chosen is None and _history_too_small(defaults[part]):
                small += 1
            else:
                bad += chosen is None
        if small:
            _history_sample_gap(part, unknown, small)
        if bad:
            unknown("HISTORY_ESTIMATE_UNUSABLE", f"history {part} estimate",
                    f"{bad} {part} estimate(s) from PLX history in the capture do not "
                    "validate; none of them is used", holds=False, who="FIN")
        if chosen is None:
            unknown("HISTORY_ESTIMATE_NOT_AVAILABLE", f"history {part} estimate",
                    f"the capture holds no usable {part} estimate from PLX history for this "
                    "purchase order", holds=False, who="FIN")
            continue
        history[part] = {**chosen, "basis": "estimated_from_history", "_source": cite,
                         "label": _history_label(chosen, part), "note": note}
    return out, incoterm, history


def plan_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
             edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Mapping[str, Any]],
             name: str, as_of: str, resolver: Resolver | None, actor: Any,
             complete: bool, covers: Callable[[str], bool],
             assumptions: Sequence[Mapping[str, Any]] | None = None,
             withheld: Callable[[str], int | None] | None = None,
             markings_lacked: Callable[[str], Sequence[str]] | None = None,
             landed_actuals: Callable[[Sequence[str]], Any] | None = None) -> dict[str, Any]:
    """Computed answer entries, gaps and reason codes for one plan run (see the block above).

    ``landed_actuals`` (the host's capture-built recorded charges behind the reader's finance
    gate, ``InvestigateTools``): with a ``po_number`` role, the purchase order's recorded duty
    and freight rates and incoterm (``_recorded_actuals``) feed ``po_line_landed`` and the
    estimate; without one, nothing changes.

    ``evidence``: the answer entries the plan produced, by evidence name; ``edges``: the links
    the run reached; ``items``: the plan's evidence items by name (the gap's searched sources);
    ``complete``: no step of the run was truncated or cut by its budget; ``covers(link_type)``:
    the reader's read scope shows every object that link can attach to the purchase order
    (decided from its policy, so a line withheld from the reader is never silently missing).
    Header freight and duty are allocated only over a line set proven complete: every line the
    ``line_link`` edges name read with a price, a quantity and a received quantity, in a
    complete run, by a reader whose scope covers the ``line_link``, and only when every line's
    price is per the unit of its quantities in the target currency (else no line has an
    allocation weight that is a cited amount). Each forwarder quote the run reached is its own
    entry (``<name>_freight_quote_<n>``): its total freight only when ``assess_freight_quote``
    finds it complete, valid on ``as_of`` and counted; a quote whose own figures disagree is a
    ``value_disagreement`` conflict (``conflicts``: ``inputs`` names the answer entries whose
    cited values disagree, which the caller attaches). A purchase order with a line sent
    unpriced and a supplier confirmation stating an amount has a goods entry
    (``<name>_goods``): the confirmed amount only when every linked confirmation states a
    readable one and they agree; else null, with PI_VERSION_UNRESOLVED when confirmations
    state different amounts, unit prices or quantities and PI_AMOUNT_UNREAD naming each one whose
    amount is unread. The cases each line ships in (``<name>_packaging``)
    come from the item's units per case; a missing one, or records stating different ones, is a
    gap that does not hold up the landed cost. Gaps never quote source text: they name answer
    entries, codes, freight components, parsed dates and ISO 4217 currencies only.

    A header value (currency, freight or duty estimate) read only through a record the run
    reached by a lead-tier link (``lead_basis``: an unconfirmed twin) is dropped: no amount rests
    on it (PO_HEADER_ON_UNCONFIRMED_TWIN). A purchase order not every line of which is fully
    received, with no receipt actual posted, also gets the estimate entries (``ESTIMATE_LABEL``,
    ``_estimate``); ``assumptions``: the person-recorded assumptions of the case on this
    purchase order (``duty_assumption`` records; one that does not validate is a gap, never used).
    ``markings_lacked(evidence name)``: the markings the reader lacks on every property that
    evidence reads (schema and grant only); a unit price it may not read is UNIT_PRICE_WITHHELD
    on each line, a reader limit, never UNPRICED_LINE.
    """
    roles = args["roles"]
    got = {role: list(evidence.get(roles.get(role) or "", ())) for role in PLAN_ROLES}
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    reasons: list[str] = []

    def searched(*role_names: str) -> list[str]:
        out = []
        for role in role_names:
            item = items.get(roles.get(role) or "")
            if item:
                out.append(f"{item['object_type']}.{'|'.join(item['properties'])}"[:64])
        return out

    def unknown(code: str, subject: str, detail: str, *role_names: str,
                line: str | None = None, holds: bool = True, who: str | None = None) -> None:
        """One gap per (code, subject, detail); the lines it holds for are listed in it.
        ``holds``: the gap's code is a reason the answer is partial; ``who``: who can answer
        it (``who_can_answer``; without it the executor names the searched sources' owners)."""
        detail = (detail if detail.startswith(code) else f"{code}: {detail}")[:600]
        gap = grouped.get((code, subject, detail))
        if gap is None:
            gap = grouped[(code, subject, detail)] = {
                "state": _GAP_STATES.get(code, "unknown"), "reason": code.lower()[:64],
                "subject": subject, "detail": detail, "lines": []}
            sources = searched(*role_names)
            if sources:
                gap["searched"] = {"sources": sources[:8]}
        if who and who[:200] not in gap.get("who_can_answer", ()):
            gap["who_can_answer"] = (gap.get("who_can_answer", []) + [who[:200]])[:10]
        if line:
            gap["lines"].append(line)
        if holds and code.lower() not in reasons:
            reasons.append(code.lower()[:64])

    def open_input(line: str, cost: str, reason: str, who: str | None) -> None:
        unknown(_code_of(reason), cost, reason, *_COST_ROLES.get(cost, ()), line=line, who=who)

    def entry_names(role: str) -> str:
        return ", ".join(e["name"] for e in got[role])[:200]

    def by_object(role: str) -> dict[str, Mapping[str, Any]]:
        out: dict[str, Mapping[str, Any]] = {}
        for entry in got[role]:  # a required value no record carried has no object
            if entry.get("object_id") is not None:
                out.setdefault(entry["object_id"], entry)
        return out

    def handle(entry: Mapping[str, Any] | None) -> str | None:
        return entry["citation"]["l1_handle"] if entry and entry.get("citation") else None

    # A header value read only through an unconfirmed twin (a lead-tier link) never feeds a
    # number: it is dropped here, before any role is read, and named in a gap.
    for role in ("currency", "freight_estimate", "duty_estimate"):
        kept = [e for e in got[role] if e.get("lead_basis") is not True]
        dropped = [e for e in got[role] if e.get("lead_basis") is True
                   and e.get("value") is not None]
        if dropped:
            unknown("PO_HEADER_ON_UNCONFIRMED_TWIN", "purchase order " + role,
                    f"{', '.join(e['name'] for e in dropped)[:200]} rest(s) only on a record "
                    "reached by a lead-tier link (an unconfirmed twin of the purchase order); "
                    "no amount rests on it until a person confirms the twin", role,
                    holds=not any(e.get("value") is not None for e in kept), who="PROC")
        got[role] = kept
    header_ids = {e["object_id"] for role in ("currency", "freight_estimate", "duty_estimate")
                  for e in got[role] if isinstance(e.get("object_id"), str)}
    # A real input read only through a lead-tier link never feeds a number either: the
    # supplier row, rate setting or receipt currency it would rest on is not proven this
    # order's (it stays unused; the line's missing input is named as before).
    for role in REAL_INPUT_ROLES:
        got[role] = [e for e in got[role] if e.get("lead_basis") is not True]

    # Each cited currency cell is read by the ontology's one rule (``currency_code``: a code
    # in any case, or an ISO code with its own sign attached such as the Portal's ``CAD$``);
    # a cell no rule reads keeps its text, so it stays not an ISO 4217 code, never guessed.
    folded = {t: currency_code(t) for t in map(_entry_text, got["currency"]) if t}
    currencies = sorted({code or t for t, (code, _) in folded.items()})
    rules = sorted({rule for code, rule in folded.values() if rule and code in currencies})
    target: str | None = None
    currency_gap: str | None = None
    if not currencies:
        currency_gap = "CURRENCY_UNCITED"
        unknown("CURRENCY_UNCITED", "purchase order currency",
                "no record of the purchase order states its currency; none is assumed",
                "currency", who="PROC")
    elif len(currencies) > 1:
        currency_gap = "MIXED_CURRENCY_NO_CITED_RATE"
        valid = [c for c in currencies if c in ISO_4217]
        others = len(currencies) - len(valid)
        unknown("MIXED_CURRENCY_NO_CITED_RATE", "purchase order currency",
                f"the purchase order's records ({entry_names('currency')}) state "
                f"{len(currencies)} currencies: " + " and ".join(
                    valid + ([f"{others} value(s) that are not ISO 4217 codes"] if others else []))
                + "; the ontology projects no exchange rate, so no amount is converted",
                "currency", who="PROC")
    elif currencies[0] not in ISO_4217:
        currency_gap = "CURRENCY_NOT_ISO_4217"
        unknown("CURRENCY_NOT_ISO_4217", "purchase order currency",
                f"the cited currency ({entry_names('currency')}) is not an ISO 4217 code",
                "currency", who="PROC")
    else:
        target = currencies[0]
    currency_step = (f"currency: the cited currency cell(s) read as {target} by the ontology "
                     f"rule {' and '.join(rules)} (ontology.units.currency_code: a written form "
                     "whose currency is not in doubt)") if target and rules else None

    prices, quantities, received = (by_object(r) for r in _LINE_ROLES)
    prices = dict(prices)
    # A BC line's directUnitCost is gross (before the line discount; with tax when the
    # order's pricesIncludeTax is true). Its goods are its amountExcludingTax over its ordered
    # quantity (a derived net unit cost citing both); else it counts as net only when its
    # discounts read zero and the order's prices read tax-exclusive; else BC_PRICE_NOT_NET
    # and the line has no price (no weight, no allocation, no estimate rests on the gross).
    net_goods: dict[str, dict[str, Any]] = {}
    net_used: dict[str, Mapping[str, Any]] = {}
    not_net: dict[str, str] = {}
    if roles.get("bc_line_amount"):
        amounts = by_object("bc_line_amount")
        discounts: dict[str, list[Mapping[str, Any]]] = {}
        for e in got["bc_line_discount"]:
            if e.get("object_id") is not None:
                discounts.setdefault(e["object_id"], []).append(e)
        incl = [e.get("value") for e in got["prices_include_tax"]
                if e.get("lead_basis") is not True and e.get("value") is not None]
        tax_free = bool(incl) and all(v is False or str(v).strip().lower() in ("false", "0")
                                      for v in incl)
        for oid, price in list(prices.items()):
            if price.get("property") != BC_GROSS_PRICE or price.get("value") is None:
                continue
            qty, net = quantities.get(oid), amounts.get(oid)
            amount, count = _entry_decimal(net), _entry_decimal(qty)
            if amount is not None and amount >= 0 and count is not None and count > 0 \
                    and net.get("currency") is not None \
                    and net.get("currency") == price.get("currency") and handle(net):
                unit = amount / count
                prices[oid] = {**price, "value": _dec_str(unit), "unit": qty.get("unit")}
                net_goods[oid] = {"derived": {
                    "value": _dec_str(unit), "per_quantity_unit": True,
                    "from": [handle(net), handle(qty)],
                    "rule": f"BC line {net['name']} (amountExcludingTax: after the line "
                            f"discount, before tax) / {qty['name']}: the net unit cost "
                            "(directUnitCost is gross)"}}
                net_used[oid] = net
                continue
            stated = [_entry_decimal(e) for e in discounts.get(oid, ())]
            if stated and all(d is not None and d == 0 for d in stated) and tax_free:
                continue
            why = ("the line records a discount" if any(d for d in stated if d is not None)
                   else "the order's prices include tax" if incl and not tax_free
                   else "no line discount and tax-exclusive price is read for it")
            prices[oid] = {**price, "value": None}
            not_net[oid] = why
    # The markings the unit price carries that this reader does not hold (schema and grant
    # only, never a row): no line is then priced for it, a withheld input, never a missing one.
    price_lacked = sorted(markings_lacked(roles["unit_price"]) or ()) \
        if markings_lacked is not None and roles.get("unit_price") else []
    price_withheld = (f"the unit price carries the {', '.join(price_lacked)} marking, which "
                      "this reader does not hold") if price_lacked else None
    line_ids = sorted(set(prices) | set(quantities))
    linked = {e["from"] for e in edges if e.get("link_type") == args.get("line_link")}
    unproven = sorted(oid for oid in linked | set(line_ids) if not all(
        (role.get(oid) or {}).get("value") is not None for role in (prices, quantities, received)))
    covered = covers(args["line_link"])
    # Lines the reader's markings withhold (a count, never a row or an amount): named in the
    # gaps, never silently dropped; no total or allocation is stated over the lines it reads.
    held_back = (withheld(args["line_link"]) or 0) if withheld is not None and not covered else 0
    line_set = LineSetCompleteness.of(complete=complete, covered=covered, linked=linked,
                                      line_ids=line_ids, unproven=unproven, withheld=held_back,
                                      price_withheld=price_withheld)
    lines_complete = line_set.whole
    if not lines_complete:
        unknown("PO_LINES_INCOMPLETE", "purchase order lines", f"{line_set.why}"
                + "; header freight and duty are allocated only over every line, never a partial "
                  "set", "unit_price", "quantity", "received")
    item_of = {e["from"]: e["to"] for e in edges if e.get("link_type") == args.get("item_link")}
    classes = by_object("classification")
    # The order's own records only (those its header values were read from): a line linked to
    # another purchase order as well never brings that order's supplier's rows.
    rows_of = _supplier_rows(args, edges, line_ids, header_ids)
    row_inputs = _row_inputs(got, rows_of)
    # The HS code a line's duty reads: the item's classification, else its supplier row's.
    hs_of = {oid: classes.get(item_of.get(oid, "")) or next(iter(row_inputs[oid]["hs"]), None)
             for oid in line_ids}
    setting = _rate_setting(got["rate_setting_tariff"])

    def grounded(oid: str) -> bool:
        """The line's allocation weight (unit price x received quantity) is a cited amount: its
        price is per the unit its ordered and received quantities are in, in the target currency."""
        price, qty, rcv = prices.get(oid), quantities.get(oid), received.get(oid)
        unit = unit_token(price.get("unit")) if price and price.get("value") is not None else None
        return (unit is not None and target is not None and price.get("currency") == target
                and all(q and q.get("value") is not None and unit_token(q.get("unit")) == unit
                        for q in (qty, rcv)))

    # LC-ALLOCATION-DENOMINATOR-UNKNOWN: the Portal splits header freight by line value, so a
    # single line whose price denominator is not cited makes every line's share unknown.
    weights_grounded = bool(line_ids) and all(grounded(oid) for oid in line_ids)
    if lines_complete and target is not None and not weights_grounded:
        unknown("ALLOCATION_DENOMINATOR_UNKNOWN", "purchase order lines",
                f"{sum(not grounded(oid) for oid in line_ids)} of {len(line_ids)} line(s) state "
                "no unit price per the unit of their ordered and received quantities in the "
                "purchase order's currency; header freight and duty are not allocated over "
                "line values that are not cited amounts", "unit_price", "quantity", "received")

    def one(role: str) -> tuple[Mapping[str, Any] | None, bool]:
        """The single non-empty value of a header role, and whether records disagree."""
        values = [e for e in got[role] if e.get("value") is not None]
        distinct = {str(e["value"]) for e in values}
        return (values[0] if len(distinct) == 1 else None), len(distinct) > 1

    freight_est, freight_conflict = one("freight_estimate")
    duty_est, duty_conflict = one("duty_estimate")
    for role, conflict in (("freight_estimate", freight_conflict),
                           ("duty_estimate", duty_conflict)):
        if conflict:
            unknown("PO_ESTIMATE_RECORDS_DISAGREE", "purchase order " + role,
                    "the purchase order's records state different amounts; none is chosen", role)
    off_currency = {part for part, est in (("freight", freight_est), ("duty", duty_est))
                    if est is not None and est.get("currency") != target}
    po = {"_source": handle(freight_est) or handle(duty_est),
          "shippingCost": freight_est["value"] if freight_est else None,
          "dutiesCost": duty_est["value"] if duty_est else None}
    view = [{"id": oid, "_source": handle(prices.get(oid)) or handle(quantities.get(oid)),
             "unitPrice": (prices.get(oid) or {}).get("value"),
             "quantity": (quantities.get(oid) or {}).get("value"),
             "quantityReceived": (received.get(oid) or {}).get("value")} for oid in line_ids]

    # Recorded duty and freight charges and the incoterm, looked up by the order's own
    # (non-lead) numbers; each actual cites the order's currency record the run read.
    # A part with no recorded actual may rest on an estimate from PLX history (a rate, so it
    # applies to goods in any currency); only the estimate entries use it, labelled.
    actuals: dict[str, dict[str, Any]] = {}
    history: dict[str, dict[str, Any]] = {}
    incoterm_used: dict[str, Any] | None = None
    po_cite = next((h for h in map(handle, got["currency"]) if h), None)
    if landed_actuals is not None and roles.get("po_number") and po_cite and target:
        numbers = [t for t in (_entry_text(e) for e in got["po_number"]
                               if e.get("lead_basis") is not True) if t]
        actuals, incoterm_used, history = _recorded_actuals(numbers, landed_actuals, po_cite,
                                                            unknown)

    # Receipt actuals (first receipt of a PO only): read when any receipt value was reached; an
    # actual is used only with a cited currency equal to the target, else that part is unknown.
    receipt: dict[str, Any] | None = None
    receipt_entries = got["receipt_freight"] + got["receipt_duty"]
    # Receipts of this order the run reached (records this reader may read), whether or not
    # one records a freight or duty actual.
    receipts_read = len({e["from"] for e in edges if e.get("link_type") == "Receipt.receives"
                         and e.get("tier") != "lead"})
    # The markings this reader lacks on the receipt's actuals (schema and grant only): a
    # receipt it reads may record an actual it is not shown, which is withheld, not absent.
    # The same for the header's freight and duty estimates (finance; PO prices are
    # purchasing_prices): a purchasing reader is not shown them, which is not 'none recorded'.
    header_lacked = {part: sorted(markings_lacked(roles[role]) or ())
                     for part, role in (("freight", "freight_estimate"), ("duty", "duty_estimate"))
                     if roles.get(role) and markings_lacked is not None}
    header_lacked = {part: lacked for part, lacked in header_lacked.items() if lacked}
    receipt_lacked = sorted({m for role in ("receipt_freight", "receipt_duty")
                             if roles.get(role) and markings_lacked is not None
                             for m in markings_lacked(roles[role]) or ()})

    def currency_records(entry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """The receipt's own currency records (a Sage receipt header's currency)."""
        return [c for c in got["receipt_currency"] if c.get("object_id") is not None
                and c.get("object_id") == entry.get("object_id") and _entry_text(c)]

    def receipt_currency_of(entry: Mapping[str, Any]) -> str | None:
        """The one ISO 4217 code the actual's own currency and its receipt's currency records
        state, else None (none, or two different ones: never a pick)."""
        codes = {entry["currency"]} if entry.get("currency") is not None else set()
        codes |= {currency_code(_entry_text(c))[0] or _entry_text(c)
                  for c in currency_records(entry)}
        code = next(iter(codes)) if len(codes) == 1 else None
        return code if code in ISO_4217 else None
    actual = {part: [e for e in got[role] if _positive(e.get("value"))]
              for part, role in (("freight", "receipt_freight"), ("duty", "receipt_duty"))}
    carriers = sorted({e["object_id"] for found in actual.values() for e in found})
    blocked: set[str] = set()
    if len(carriers) > 1:
        blocked = {part for part, found in actual.items() if found}
        unknown("RECEIPT_ACTUALS_ON_SEVERAL_RECEIPTS", "receipt actuals",
                f"{len(carriers)} receipts record freight or duty actuals; the Portal keeps "
                "them on the first receipt of a purchase order only", "receipt_freight", "receipt_duty")
    else:
        for part, found in actual.items():
            if found and any(receipt_currency_of(e) is None or receipt_currency_of(e) != target
                             for e in found):
                blocked.add(part)
                unknown("RECEIPT_ACTUAL_CURRENCY_UNRECORDED", f"receipt actual {part}",
                        f"the receipt records {part} {found[0]['value']}, which the Portal posts "
                        "over the purchase order estimate, without a cited currency equal to the "
                        "purchase order's", f"receipt_{part}")
    # A zero actual is no actual (resolvePostedLandedAmounts posts only one above zero): the
    # Portal keeps the estimate, and whether nothing was charged is the Warehouse's to say.
    zero_parts = []
    for part, role in (("freight", "receipt_freight"), ("duty", "receipt_duty")):
        stated = [_entry_decimal(e) for e in got[role] if e.get("value") is not None]
        if stated and not actual[part] and all(v is not None and v <= 0 for v in stated):
            zero_parts.append(part)
            unknown("RECEIPT_ACTUAL_ZERO", f"receipt actual {part}",
                    f"the receipt records its {part} actual at zero, which the Portal reads as "
                    f"no actual posted; the Warehouse can say whether any {part} was charged",
                    role, holds=False, who="WH")
    usable: dict[str, Mapping[str, Any]] = {}
    receipt_currency_used: list[Mapping[str, Any]] = []
    if receipt_entries:
        usable = {part: found[0] for part, found in actual.items()
                  if found and part not in blocked}
        receipt_currency_used = list({id(c): c for e in usable.values()
                                      for c in currency_records(e)}.values())
        receipt = {"_source": handle(next(iter(usable.values()), receipt_entries[0])),
                   "freightCost": (usable.get("freight") or {}).get("value"),
                   "dutiesCost": (usable.get("duty") or {}).get("value")}

    # Forwarder quotes naming the purchase order: each a current quote, assessed on its own.
    quote_ids = sorted({e["from"] for e in edges if e.get("link_type") == args.get("quote_link")}
                       | {e["object_id"] for role in QUOTE_ROLES for e in got[role]})
    quote_entries: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    partial_quotes: set[tuple[Decimal, str]] = set()
    # (total, entry name, its entries, whether the quote includes duties and taxes)
    quote_totals: list[tuple[Decimal, str, list[Mapping[str, Any]], bool]] = []
    for index, oid in enumerate(quote_ids, start=1):
        fields = {role: next((e for e in got[role] if e["object_id"] == oid), None)
                  for role in QUOTE_ROLES}
        record, found, partial, disagree = _quote_record(fields, as_of=as_of, target=target)
        duties_included = bool(record.pop("_duties_included", False))
        if partial is not None:
            partial_quotes.add(partial)
        subject = f"freight quote {index}"
        for code, params in found:
            _, who, text = _QUOTE_GAPS[code]
            unknown(code, subject, text.format(**params),
                    *(_QUOTE_COUNT_SEARCHED if code in _QUOTE_COUNT_CODES else _QUOTE_SEARCHED),
                    who=who)
        entry_name = f"{name}_freight_quote_{index}"[:64]
        if record.get("value") is not None and record.get("currency") == target:
            quote_totals.append((Decimal(record["value"]), entry_name,
                                 [e for e in fields.values() if e], duties_included))
        # A machine-read quote disagreeing with itself: a lead, never resolved here.
        conflicts += [{"kind": "value_disagreement", "subject": f"{entry_name}: {c['about']}"[:700],
                       "inputs": c["inputs"][:12], "note": c["note"][:600], "tier": "lead"}
                      for c in disagree]
        quote_entries.append({"name": entry_name, "kind": "computed",
                              "value": None, **record,
                              "inputs": [e["name"] for e in fields.values() if e][:50],
                              "basis": [oid], "check": "not_applicable"})
    estimate = _entry_decimal(freight_est)
    estimate_is_partial_quote = estimate is not None and any(
        currency == freight_est.get("currency") and
        abs(estimate - quoted) <= quoted * ESTIMATE_QUOTE_TOLERANCE
        for quoted, currency in partial_quotes)

    # Supplier confirmations (proforma invoices) naming the purchase order: the goods of a line
    # sent unpriced, read from the confirmation's cited figures (a lead, machine-read).
    def figure(oid: str, role: str) -> Mapping[str, Any] | None:
        return next((e for e in got[role] if e.get("object_id") == oid
                     and _entry_decimal(e) is not None), None)

    def same_quantity(one: Mapping[str, Any] | None, other: Mapping[str, Any] | None) -> bool:
        unit = unit_token((one or {}).get("unit"))
        return unit is not None and unit == unit_token((other or {}).get("unit")) and \
            _entry_decimal(one) == _entry_decimal(other)

    # Every confirmation linked to the purchase order (its link, or a figure the walk read
    # from it) is a version; the goods are one amount only when every version states a
    # readable amount and they and their unit prices all agree. A version whose amount is
    # unread is PI_AMOUNT_UNREAD (its amount unknown, so no other version's is taken).
    confirmation_ids = sorted(
        {e["from"] for e in edges if e.get("link_type") == args.get("confirmation_link")
         and args.get("confirmation_link")}
        | {e["object_id"] for role in CONFIRMATION_ROLES for e in got[role]
           if isinstance(e.get("object_id"), str)})
    amounts = [e for e in got["confirmation_amount"] if _entry_decimal(e) is not None]
    versions = {(_entry_decimal(e), e.get("currency")) for e in amounts}
    unit_prices = [e for e in got["confirmation_unit_price"] if _entry_decimal(e) is not None]
    price_versions = {(_entry_decimal(e), e.get("currency"), unit_token(e.get("unit")))
                      for e in unit_prices}
    counts = [e for e in got["confirmation_quantity"] if _entry_decimal(e) is not None]
    count_versions = {(_entry_decimal(e), unit_token(e.get("unit"))) for e in counts}
    unread = [oid for oid in confirmation_ids
              if not any(e.get("object_id") == oid for e in amounts)]
    # Versions differ when their amounts, unit prices or quantities (so unit price x
    # quantity) differ; any one of them is a person's call.
    differ = len(versions) > 1 or len(price_versions) > 1 or len(count_versions) > 1
    differing = [what for what, found in (("amounts", versions), ("unit prices", price_versions),
                                          ("quantities", count_versions)) if len(found) > 1]
    unpriced = [oid for oid in line_ids if not person_correction(prices.get(oid)) and (
        (prices.get(oid) or {}).get("value") is None or
        (_entry_decimal(prices.get(oid)) is not None and _entry_decimal(prices[oid]) <= 0))]
    confirmed: dict[str, Any] | None = None

    def unread_names() -> str:
        """The answer entries of the confirmations whose amount is unread (their null amount,
        else their other figures, else the confirmation itself)."""
        out = []
        for oid in unread:
            own = [e["name"] for role in CONFIRMATION_ROLES for e in got[role]
                   if e.get("object_id") == oid]
            out.append(next((e["name"] for e in got["confirmation_amount"]
                             if e.get("object_id") == oid), None) or
                       (own[0] if own else f"confirmation {oid}"))
        return ", ".join(out)[:300]

    def unread_text() -> str:
        return (f"PI_AMOUNT_UNREAD: no readable amount is cited for {unread_names()} (see "
                f"{name}_goods), a supplier confirmation of the purchase order; no other "
                "confirmation's amount is taken")

    if confirmation_ids and unpriced:
        names = [e["name"] for e in amounts]
        sources = set(confirmation_ids)
        figures = [e for role in CONFIRMATION_ROLES for e in got[role]
                   if e.get("object_id") in sources and e not in amounts]
        confirmed = {"name": f"{name}_goods"[:64], "kind": "computed", "value": None,
                     "inputs": (names + [e["name"] for e in figures])[:50], "steps": [f"{e['name']}: {e['value']} "
                                                     f"{e.get('currency') or '(no currency)'}"
                                                     for e in amounts][:20],
                     "basis": sorted(sources)[:100],
                     "check": "not_applicable"}
        if differ:
            what = " and ".join(differing)
            confirmed["steps"].append(f"PI_VERSION_UNRESOLVED: {len(confirmation_ids)} supplier "
                                      f"confirmations state different {what}; none is chosen")
            listed = names + ([e["name"] for e in unit_prices]
                              if len(price_versions) > 1 else []) + \
                ([e["name"] for e in counts] if len(count_versions) > 1 else [])
            conflicts.append({"kind": "value_disagreement",
                              "subject": f"PI_VERSION_UNRESOLVED: {confirmed['name']}: the "
                                         "supplier confirmations of the purchase order state "
                                         f"different {what}"[:700],
                              "inputs": listed[:12], "tier": "lead",
                              "note": "a revised proforma invoice is a person's call (which "
                                      "version the purchase order is priced at); none is "
                                      "chosen"})
        if unread:
            confirmed["steps"].append(f"PI_AMOUNT_UNREAD: no readable amount is cited for "
                                      f"{unread_names()}; no other confirmation's amount is "
                                      "taken")
            # Its own gap even beside differing versions: a person reads that amount.
            unknown("PI_AMOUNT_UNREAD", "goods", unread_text(), *_COST_ROLES["goods"],
                    who="PROC")
        if differ or unread:
            pass  # no amount is the goods (the steps above name why)
        elif amounts[0].get("currency") is None or amounts[0]["currency"] != target:
            confirmed["steps"].append("CONFIRMATION_CURRENCY_DIFFERS: the confirmed amount "
                                      "carries no cited currency equal to the purchase order's")
        else:
            confirmed.update(value=str(amounts[0]["value"]), unit=target, currency=target)
            confirmed["steps"].append(f"goods: {amounts[0]['value']} {target}, the amount the "
                                      "supplier confirmation states (a lead, machine-read)")

    def confirmed_goods(oid: str) -> tuple[dict[str, Any], list[Mapping[str, Any]]] | None:
        """The goods input of an unpriced line from the confirmations, with the confirmed amount
        it rests on (none while the versions are unresolved: the goods entry names them); None
        when no confirmation states an amount."""
        if confirmed is None or oid not in unpriced:
            return None
        if differ:
            return {"unknown": f"PI_VERSION_UNRESOLVED: {len(confirmation_ids)} supplier "
                               "confirmations of the purchase order state different "
                               f"{' and '.join(differing)} (see "
                               f"{confirmed['name']}); none is chosen",
                    "who_can_answer": "PROC"}, []
        if unread:
            return {"unknown": unread_text(), "who_can_answer": "PROC"}, []
        amount = amounts[0]
        if confirmed["value"] is None:
            return {"unknown": "CONFIRMATION_CURRENCY_DIFFERS: the supplier confirmation's "
                               "amount carries no cited currency equal to the purchase order's",
                    "who_can_answer": "PROC"}, [amount]
        source = amount["object_id"]
        price, count = (figure(source, r) for r in ("confirmation_unit_price",
                                                    "confirmation_quantity"))
        single_item = price is not None and count is not None and \
            unit_token(price.get("unit")) == unit_token(count.get("unit")) and \
            _equal_at(_entry_decimal(price) * _entry_decimal(count), _entry_decimal(amount))
        if line_ids == [oid]:
            own = (count is None or same_quantity(count, quantities.get(oid))) and \
                (price is None or count is None or single_item)
        else:
            own = single_item and [line for line in line_ids if same_quantity(
                count, quantities.get(line))] == [oid]
        if not own:
            return {"unknown": f"CONFIRMATION_NOT_PER_LINE: the supplier confirmation's amount "
                               f"(see {confirmed['name']}) is not cited as this line's alone",
                    "who_can_answer": "PROC"}, [amount]
        return _corrected_input(amount, target, target) or {
            "citation": handle(amount), "field": amount["citation"]["field_path"],
            "unit": target, "currency": target}, [amount]

    entries: list[dict[str, Any]] = []
    totals: list[tuple[str, str, Decimal]] = []
    weights = [e for line in line_ids for e in (prices.get(line), received.get(line)) if e]
    for index, oid in enumerate(line_ids, start=1):
        label = f"{name}_line_{index}"
        qty, price = quantities.get(oid), prices.get(oid)
        from_confirmation = confirmed_goods(oid)
        used = [e for e in (price, qty, net_used.get(oid), received.get(oid), freight_est, duty_est,
                            hs_of.get(oid), *receipt_entries, *receipt_currency_used,
                            *(from_confirmation or ((), ()))[1]) if e]
        entry: dict[str, Any] = {"name": label[:64], "kind": "computed", "value": None,
                                 "inputs": [e["name"] for e in used][:50], "steps": [],
                                 "basis": sorted({oid} | {e["object_id"] for e in used})[:100],
                                 "check": "not_applicable"}
        entries.append(entry)
        if target is None:
            entry["steps"] = ["no target currency: see the currency gap"]
            continue
        qty_unit = unit_token(qty.get("unit")) if qty and qty.get("value") is not None else None
        if qty_unit is not None:
            entry.update(unit=f"{target}/{qty_unit}"[:20], currency=target)
        price_unit = unit_token(price.get("unit")) if price else None
        if from_confirmation is not None:
            goods: dict[str, Any] = from_confirmation[0]
        elif price is None and price_withheld:
            # A reader limit, not a missing input: Finance, who holds the marking, can give
            # the figure (or a reader holding it reads it).
            goods = {"unknown": f"UNIT_PRICE_WITHHELD: {price_withheld}; a reader holding it "
                                "gets the figure", "who_can_answer": "FIN"}
        elif oid in not_net:
            goods = {"unknown": f"BC_PRICE_NOT_NET: the BC line's directUnitCost is a gross "
                                f"price and {not_net[oid]}; its amount excluding tax is not "
                                "read, so no net goods value is stated",
                     "who_can_answer": "FIN"}
        elif price is None or price.get("value") is None:
            goods = {"unknown": "UNPRICED_LINE: no cited unit price",
                                     "who_can_answer": "PROC"}
        elif price_unit is None:
            goods = {"unknown": "UNIT_PRICE_DENOMINATOR_UNKNOWN: the cited unit price states no "
                                "unit it is per", "who_can_answer": "PROC"}
        elif qty_unit is not None and price_unit != qty_unit:
            goods = {"unknown": f"UNIT_PRICE_DENOMINATOR_MISMATCH: the price is per {price_unit}, "
                                f"the quantity is in {qty_unit}; no conversion is assumed",
                     "who_can_answer": "PROC"}
        elif price.get("currency") is None:
            goods = {"unknown": "UNIT_PRICE_CURRENCY_UNCITED: no currency comes with the cited "
                                "unit price", "who_can_answer": "PROC"}
        elif oid in net_goods:
            goods = {**net_goods[oid], "unit": f"{price['currency']}/{qty_unit}",
                     "currency": price["currency"]}
        else:
            goods = _corrected_input(price, f"{price['currency']}/{qty_unit}",
                                     price["currency"]) or {
                "citation": handle(price), "field": price["citation"]["field_path"],
                "unit": f"{price['currency']}/{qty_unit}", "currency": price["currency"]}
        engine = po_line_landed(po, view, oid, receipt=receipt, receipts_read=receipts_read,
                                actual_duty=actuals.get("duty"),
                                actual_freight=actuals.get("freight"), incoterm=incoterm_used,
                                receipt_withheld=() if usable else receipt_lacked,
                                header_withheld=header_lacked)
        hs = hs_of.get(oid)
        costs: list[dict[str, Any]] = [{"name": "goods", "category": "goods", **goods}]
        # What each derived part is computed from: the header (or the receipt actual the
        # Portal posts over it) and, for freight, every line's allocation weight.
        rests = {"freight": [usable.get("freight", freight_est), *weights],
                 "duty": [usable.get("duty", duty_est)], "receipt_actual": []}
        corrected_inputs: list[Mapping[str, Any]] = []
        for part, category in (("freight", "freight"), ("duty", "duty"),
                               ("receipt_actual", "other")):
            spec = engine[part]
            if spec is None:
                continue
            # A recorded actual is this line's own goods value times the order's recorded
            # rate: no header amount is allocated, so the line-set and estimate checks below
            # do not apply to it (a blocked receipt actual still does).
            recorded_actual = (spec.get("derived") or {}).get("basis") == "actual_recorded"
            if recorded_actual:
                derived = dict(spec["derived"])
                derived["rule"] = f"{derived['rule']} ({actuals[part]['note']})"
                spec = {**spec, "derived": derived}
                rests[part] = [prices.get(oid), quantities.get(oid)]
            if recorded_actual and part not in blocked:
                pass
            elif part in ("freight", "duty") and not lines_complete:
                spec = {"unknown": "PO_LINES_INCOMPLETE: header " + part + " is not allocated "
                                   "over a partial line set (see the lines gap)",
                        "who_can_answer": "PROC"}
            elif part in off_currency and part not in usable:
                spec = {"unknown": "PO_ESTIMATE_CURRENCY_DIFFERS: the header " + part + " "
                                   "estimate carries no cited currency equal to the purchase "
                                   "order's", "who_can_answer": "PROC"}
            elif part in blocked:
                spec = {"unknown": f"RECEIPT_ACTUAL_UNUSABLE: the receipt's actual {part}, which "
                                   "the Portal posts over the estimate, is not usable (see the "
                                   "receipt gap)", "who_can_answer": "WH"}
            elif part == "freight" and "derived" in spec and "freight" not in usable and \
                    estimate_is_partial_quote:
                spec = {"unknown": "FREIGHT_ESTIMATE_IS_PARTIAL_QUOTE: the header shipping "
                                   "estimate is within "
                                   f"{_dec_str(ESTIMATE_QUOTE_TOLERANCE * 100)}% of the amount "
                                   "of a cited forwarder quote that is not the total freight "
                                   "(see the freight quote gaps)",
                        "who_can_answer": FORWARDER}
            elif part in ("freight", "duty") and "derived" in spec and not weights_grounded:
                spec = {"unknown": "ALLOCATION_DENOMINATOR_UNKNOWN: header " + part + " is not "
                                   "allocated over line values whose price denominator is not "
                                   "cited (see the lines gap)", "who_can_answer": "PROC"}
            if "derived" in spec:
                # An amount or allocation weight a person corrected: the derived value rests
                # on the correction too, and the line names it as an input.
                fixed = [e for e in rests[part] if e and person_correction(e)]
                derived = dict(spec["derived"])
                if part in usable and receipt_currency_used:
                    # The receipt's currency the actual is read in is re-read for the actor.
                    derived["from"] = list(dict.fromkeys(list(derived["from"]) + [
                        h for h in map(handle, receipt_currency_used) if h]))
                if fixed:
                    derived["from"] = list(dict.fromkeys(derived["from"] + [
                        h for e in fixed for h in correction_sources(e, person_correction(e))]))
                    derived["rule"] += " over a person's correction of " + ", ".join(
                        e["name"] for e in fixed)
                    corrected_inputs += [e for e in fixed if e not in used]
                spec = {"derived": derived, "unit": f"{target}/{qty_unit}"
                        if spec["derived"]["per_quantity_unit"] else target}
            elif part == "duty" and spec["unknown"].startswith("TARIFF_TREATMENT_UNVERIFIED"):
                # The code itself stays in its cited entry: steps carry no source text.
                spec = {"unknown": "DUTY_RATE_SOURCE_ABSENT: an HS code is cited for the item, "
                                   "but no duty-rate source is ingested (the ontology projects no "
                                   "tariff rate)", "who_can_answer": "FIN"} \
                    if hs and _entry_text(hs) else \
                    {"unknown": "HS_CODE_NOT_CITED: no HS code is cited for the item, so its "
                                "tariff treatment is not known", "who_can_answer":
                     "PROC"}
            costs.append({"name": part, "category": category, **spec})
        if corrected_inputs:   # a sibling line's corrected weight moves this line's share
            entry["inputs"] = list(dict.fromkeys(
                entry["inputs"] + [e["name"] for e in corrected_inputs]))[:50]
            entry["basis"] = sorted(set(entry["basis"]) | {
                e["object_id"] for e in corrected_inputs})[:100]
        freight_cost = next((c for c in costs if c["name"] == "freight"), {})
        freight_label = FREIGHT_LABELS[
            "none" if "derived" not in freight_cost else "posted" if "freight" in usable else
            "actual" if freight_cost["derived"].get("basis") == "actual_recorded" else
            "estimate"]
        if qty_unit is None:
            # The engine divides by a known quantity; every other open input is still named.
            unknown("LINE_QUANTITY_UNKNOWN", "quantity", "no cited ordered quantity with a unit",
                    "quantity", line=label)
            for cost in costs:
                if "unknown" in cost:
                    open_input(label, cost["name"], cost["unknown"], cost.get("who_can_answer"))
            entry["steps"] = ["no cited ordered quantity with a unit: nothing to divide by",
                              freight_label]
            continue
        # A step names an open input by its code; the reason is stated once, in its line gap.
        open_reasons = {}
        for cost in costs:
            if "unknown" in cost:
                open_reasons[cost["name"]] = cost["unknown"]
                cost["unknown"] = _code_of(cost["unknown"]) + ": see the gaps"
        try:
            record = compute_landed_cost(
                costs, _corrected_input(qty, qty_unit) or {
                    "citation": handle(qty), "field": qty["citation"]["field_path"],
                    "unit": qty_unit}, target, as_of=as_of, resolver=resolver, actor=actor)
        except (ValidationError, ArithmeticError) as exc:
            code = _code_of(str(exc))
            unknown(code, "inputs", "the landed-cost engine refused the inputs", line=label)
            entry["steps"] = [f"refused: {code}"]
            continue
        for item in record["unknowns"]:
            open_input(label, item["input"], open_reasons.get(item["input"], item["reason"]),
                       item.get("who_can_answer"))
        # a recorded actual resting on BC charges whose posting the snapshot did not state
        # says so on the line (the rate stands; its posting is unknown)
        unstated = [f"{c['name']} basis: recorded actual{actuals[c['name']]['posting_caveat']}"
                    for c in costs if (c.get("derived") or {}).get("basis") == "actual_recorded"
                    and (actuals.get(c["name"]) or {}).get("posting_caveat")]
        entry["steps"] = [s[:MAX_STEP_CHARS] for s in record["steps"]][:39] + [freight_label] \
            + unstated
        entry["digest"] = record["digest"]
        try:
            outcome = LandedOutcome.from_record(record)
        except LandedCostError as exc:    # never summed: the line states no value
            unknown(_code_of(str(exc)), "inputs", "the landed-cost record broke its contract",
                    line=label)
            continue
        if outcome.computed:
            assert outcome.per_unit is not None and outcome.total is not None
            entry["value"] = _dec_str(outcome.per_unit.value)
            totals.append((label, outcome.digest, outcome.total.value))

    # The total rests on its line entries (inputs), each with its own basis.
    total: dict[str, Any] = {"name": name, "kind": "computed", "value": None,
                             "inputs": [e["name"] for e in entries][:50], "steps": [],
                             "check": "not_applicable"}
    if target is not None:
        total.update(unit=target, currency=target)
    # a total over every line read is the purchase order's total only when the line set is
    # proven whole: a line cut at the read or withheld from this reader may cost anything
    # (BIZ-003), so a partial set states its lines and no total
    if line_ids and len(totals) == len(line_ids) and lines_complete:
        steps: list[str] = [f"{label}: total {money_text(value, target)} {target} "
                            f"(record {digest[:12]})" for label, digest, value in totals]
        values = {f"line_{i}": (value, parse_unit(target))
                  for i, (_, _, value) in enumerate(totals, start=1)}
        result, _ = _sum_calc(values, steps, "purchase order landed cost")
        total.update(value=_dec_str(result), steps=steps,
                     digest=_sha(_canon({"lines": [d for _, d, _ in totals], "steps": steps})))
    elif line_ids and len(totals) == len(line_ids):
        total["steps"] = [f"{len(totals)} line(s) read have a computed landed cost, but the "
                          "purchase order's line set is not proven whole (PO_LINES_INCOMPLETE: "
                          "a line was not read or is withheld); no purchase order total is "
                          "stated"]
    else:
        total["steps"] = [f"{len(line_ids) - len(totals)} of {len(line_ids)} line(s) have no "
                          "computed landed cost; no purchase order total is stated"]
    if currency_step:
        total["steps"].append(currency_step)
    total["steps"].append(("cost treatment: " + COST_TREATMENT["kind"] + "; "
                           + COST_TREATMENT["current_portal_decision"])[:MAX_STEP_CHARS])

    # Estimate mode: an open purchase order (a line not fully received) with no receipt actual.
    def fully_received(oid: str) -> bool:
        ordered, got_in = (_entry_decimal(r.get(oid)) for r in (quantities, received))
        unit = unit_token((quantities.get(oid) or {}).get("unit"))
        return ordered is not None and got_in is not None and ordered > 0 and \
            got_in >= ordered and unit is not None and \
            unit == unit_token((received.get(oid) or {}).get("unit"))
    estimates: list[dict[str, Any]] = []
    # Fully received only over a line set proven whole: a line past the read may be open, so
    # a purchase order cut at the read gets the estimate, which names the missing lines.
    all_received = lines_complete and bool(line_ids) and \
        all(fully_received(oid) for oid in line_ids)
    # A purchase order header with no line reached is open too: its estimate names the lines.
    # A received purchase order whose read receipts record no actual gets the same estimate,
    # labelled as after receipt, when PLX history gives an estimate (else the null estimate
    # below names the receipt gap).
    received_estimate = all_received and not usable and bool(history) and target is not None \
        and (receipt is not None or receipts_read > 0) and not receipt_lacked \
        and not blocked and not zero_parts
    if ((line_ids or header_ids) and not usable and not all_received) or received_estimate:
        estimates = _pre_receipt_estimate(
            name=name, target=target, line_ids=line_ids, every_line=sorted(linked | set(line_ids)),
            prices=prices, quantities=quantities, linked=bool(linked), complete=complete,
            covered=covered, held_back=held_back, freight_est=freight_est, duty_est=duty_est,
            partial_quote=estimate_is_partial_quote, quotes=quote_totals,
            hs_of=hs_of, assumptions=assumptions or (), header_ids=header_ids, as_of=as_of,
            resolver=resolver, actor=actor, unknown=unknown, currency_step=currency_step,
            currency_gap=currency_gap, rows=row_inputs, setting=setting, actuals=actuals,
            net_goods=net_goods, history=history, incoterm=incoterm_used,
            header_withheld=header_lacked,
            label=RECEIVED_ESTIMATE_LABEL if received_estimate else ESTIMATE_LABEL)
    elif all_received and not usable:
        # Every line read fully received with no usable receipt actual: no estimate applies
        # (the receipt is the actual), and the null estimate cites the receipt gap that holds
        # the actual back, so an open purchase order never ends with no estimate entry.
        why = (f"no estimate: {currency_gap} (no target currency; see the purchase order "
               "currency gap)" if target is None else
               "no estimate: RECEIPT_ACTUAL_WITHHELD (every line read is fully received; the "
               "landed cost rests on the receipt actuals, which carry a marking this reader "
               "does not hold; see the gaps)" if receipt_lacked else
               "no estimate: RECEIPT_ACTUAL_NOT_RECORDED (every line read is fully received; "
               "the landed cost rests on the receipt actuals, and the receipt(s) read record "
               "none; see the gaps)" if receipt is None and receipts_read else
               "no estimate: RECEIPT_ACTUAL_NOT_READ (every line read is fully received; the "
               "landed cost rests on the receipt actuals, and no verified receipt of this "
               "purchase order is read; see the gaps)" if receipt is None else
               "no estimate: RECEIPT_ACTUALS_ON_SEVERAL_RECEIPTS (every line read is fully "
               "received; see the gaps)" if blocked and len(carriers) > 1 else
               "no estimate: RECEIPT_ACTUAL_CURRENCY_UNRECORDED (every line read is fully "
               "received; see the gaps)" if blocked else
               "no estimate: RECEIPT_ACTUAL_ZERO (every line read is fully received and the "
               "receipt records a zero actual, which the Portal reads as none; see the gaps)"
               if zero_parts else
               "no estimate: every line read is fully received and the receipt records no "
               "freight or duty actual above zero; the landed cost entries state what the "
               "Portal posts")
        # It rests on the line entries (as the total does), each citing its received quantity.
        inputs = [e["name"] for e in entries][:50]
        steps = [why[:MAX_STEP_CHARS], ESTIMATE_LABEL]
        estimates = [{"name": f"{name}_estimate"[:64], "kind": "computed", "value": None,
                      **({"unit": target, "currency": target} if target else {}),
                      "inputs": inputs, "steps": steps,
                      "digest": _sha(_canon({"inputs": inputs, "steps": steps})),
                      "check": "not_applicable"}]
    agreement = _fm_agreement(
        name=name, target=target, line_ids=line_ids, quantities=quantities,
        tiers_of=_fm_tiers(args, edges, rows_of), found=got["fm_landed_cost"],
        least=got["fm_tier_quantity"], ours={
            oid: (e.get("value"), "landed cost") for oid, e in zip(line_ids, entries)}
        if not estimates or all_received else {
            oid: (e.get("value"), "estimate") for oid, e in zip(
                line_ids, [x for x in estimates if "_estimate_line_" in x["name"]])},
        currency_handles=[h for h in map(handle, got["currency"]) if h],
        resolver=resolver, actor=actor)

    # Packaging (LOG-01): the cases each line ships in, from its item's cited units per case,
    # read from every record of the item (its other records by ``item_twin_link``).
    items_of: dict[str, set[str]] = {}
    for e in edges:
        if e.get("link_type") == args.get("item_link"):
            items_of.setdefault(e["from"], set()).add(e["to"])
    for e in edges:
        if args.get("item_twin_link") and e.get("link_type") == args["item_twin_link"]:
            for records in items_of.values():
                if records & {e["from"], e["to"]}:
                    records.update((e["from"], e["to"]))
    packaging: list[dict[str, Any]] = []
    if any(e.get("value") is not None for e in got["units_per_box"]):
        pack: dict[str, Any] = {"name": f"{name}_packaging"[:64], "kind": "computed",
                                "value": None, "unit": "case", "inputs": [], "steps": [],
                                "basis": [], "check": "not_applicable"}
        cases_total, counted = 0, 0
        for index, oid in enumerate(line_ids, start=1):
            label = f"{name}_line_{index}"
            qty = quantities.get(oid)
            boxes = [e for e in got["units_per_box"] if e.get("object_id") in
                     items_of.get(oid, ()) and _entry_decimal(e) is not None]
            box = boxes[0] if boxes else None
            ordered, per_case = _entry_decimal(qty), _entry_decimal(box)
            if len({_entry_decimal(e) for e in boxes}) > 1:
                pack["steps"].append(f"{label}: UNITS_PER_CASE_CONFLICT: the item's records "
                                     "state different units per case; none is chosen")
                pack["inputs"] += [e["name"] for e in boxes]
                pack["basis"] += [e["object_id"] for e in boxes]
                unknown("UNITS_PER_CASE_CONFLICT", "packaging", "the item's records state "
                        f"{len({_entry_decimal(e) for e in boxes})} different units per case "
                        f"({', '.join(e['name'] for e in boxes)})"[:300]
                        + "; none is chosen", "units_per_box", line=label, holds=False,
                        who="PROC")
                continue
            if per_case is None or per_case <= 0:
                why = "no cited units per case for the line's item"
            elif ordered is None or ordered <= 0 or unit_token((qty or {}).get("unit")) != "ea":
                why = "no ordered quantity in ea, the unit units per case counts"
            else:
                cases = int((ordered / per_case).to_integral_value(rounding="ROUND_CEILING"))
                pack["steps"].append(f"{label}: ceil({_dec_str(ordered)} ea / "
                                     f"{_dec_str(per_case)} ea per case) = {cases} cases")
                pack["inputs"] += [qty["name"], box["name"]]
                pack["basis"] += [oid, box["object_id"]]
                cases_total, counted = cases_total + cases, counted + 1
                continue
            pack["steps"].append(f"{label}: {why}")
            unknown("UNITS_PER_CASE_UNKNOWN", "packaging", why, "units_per_box", "quantity",
                    line=label, holds=False, who="PROC")
        if lines_complete and line_ids and counted == len(line_ids):
            pack["value"] = str(cases_total)
            pack["steps"].append(f"cases: {cases_total} over {counted} line(s)")
        else:
            pack["steps"].append("no case total: not every line of the purchase order is counted")
        pack["steps"].append("containers: not derived; case dimensions and gross weight are in "
                             "app_product_pack_spec, which the reporting API does not serve")
        pack["inputs"], pack["basis"] = pack["inputs"][:50], sorted(set(pack["basis"]))[:100]
        packaging.append(pack)

    gaps = []
    for gap in grouped.values():
        lines = gap.pop("lines")
        if lines:
            gap["subject"] = f"{gap['subject']} of {', '.join(lines)}"
        gap["subject"] = gap["subject"][:700]
        gaps.append(gap)
    return {"entries": [total] + entries + ([confirmed] if confirmed else []) + packaging
            + quote_entries + estimates + agreement, "gaps": gaps,
            "reasons": reasons, "conflicts": conflicts}


# The draft wording of this engine's gap codes (``draft_request``): a freight quote request for a
# freight figure a forwarder's quote settles; the upstream ask its detail names otherwise.
_QUOTE_REQUEST_CODES = frozenset(_QUOTE_GAPS) - {"PACK_SPEC_NOT_SERVED"} | {
    "PO_FREIGHT_NOT_RECORDED", "FREIGHT_ESTIMATE_IS_PARTIAL_QUOTE"}
_UPSTREAM_ASKS = {"PACK_SPEC_NOT_SERVED": "please serve public.app_product_pack_spec (case "
                                          "length, width, height and gross weight) through the "
                                          "Workbench reporting API"}


def draft_request(subject: str, who: Sequence[str], codes: Sequence[str]) -> str | None:
    """The draft request (LOG-07) for one subject's gaps with one answerer, or None when no
    code is one this engine words. Built from codes and the subject only (never source text);
    a draft is a working note: nothing is sent."""
    quote = [code for code in codes if code in _QUOTE_REQUEST_CODES]
    asks = [code for code in codes if code in _UPSTREAM_ASKS]
    if not quote and not asks:
        return None
    # An undeclared answerer is addressed by the owner it names (operating/answerers.py).
    to = ", ".join(w.removeprefix(UNDECLARED) for w in who)
    parts = [f"Draft freight quote request to {to} (not sent): please quote origin charges, "
             "main carriage and destination charges, with the rate basis, the number of "
             "containers or units it is priced for, what is excluded and the validity date. "
             f"Open for {subject}: {', '.join(quote)}"] if quote else []
    parts += [f"Draft request to {to} (not sent): {_UPSTREAM_ASKS[code]}. Open for {subject}: "
              f"{code}" for code in asks]
    return " ".join(parts)[:1900]


# ``reviewed_inputs``: a person's correction in force is computed from (``person_correction``).
PLAN_ENGINES = {"landed_cost": {"check": plan_check, "run": plan_run, "draft": draft_request,
                                "reviewed_inputs": True}}
