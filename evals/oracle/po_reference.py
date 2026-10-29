"""The rule for a BC purchase order's ``Your_Reference`` naming a FileMaker/Portal PO
number: a firm link or a lead.

The link stands on data evidence, not on an business rule. It is firm only when the match is
unique and at least one independent field corroborates it; everything else is a lead:

* unique: exactly one Portal purchase order holds the number (in the one company scope
  the reference was read in), and no other BC order's reference names that Portal order;
* nothing conflicts: neither order is already joined to another order of the other
  system, and the supplier does not disagree;
* corroborated: the supplier agrees, or the two order dates lie at most
  ``DATE_WINDOW_DAYS`` days apart.

A Portal order of a lineage that is never authority (Portal-minted) never qualifies, not
even as a lead. Line-item overlap is not a corroborating field here: neither caller
compares lines (the decision record gives the measured reason).

One rule, two copies. The flagship evaluation's raw-capture oracle
(``evals.oracle.po_lateness``) calls :func:`decide`; the product's build step calls
``your_reference_verdict`` in the ontology package's identity scoring. Two gates keep
product and evaluation code apart in both directions (the oracle import ban,
``scripts/check_oracle_imports.py``, and the no-hardcoded-cases ``eval_reference`` rule),
so neither may import the other. A parity test runs both over every input combination
and fails on any difference in tier, code or corroborating fields (the product's
``test_bc_po_your_reference`` ``RuleParityTests``). Each side reads its own evidence (the
projection, or the raw captures).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

FIRM, LEAD = "firm", "lead"
# The order date the Portal sends and the one BC holds. Live (2026-10-27): of the 236
# unique matches, 118 lie 0 days apart and the rest 21 or more; any window from 0 to 20
# days decides the same. A week allows a weekend and a late send.
DATE_WINDOW_DAYS = 7
SUPPLIER, ORDER_DATE = "supplier", "order_date"
CORROBORATING = (SUPPLIER, ORDER_DATE)
# Every code decide() returns, by tier: None (no link at all), LEAD, FIRM.
NO_LINK_CODES = ("no_match", "lineage_not_authority")
LEAD_CODES = ("ambiguous_fm", "ambiguous_bc", "conflict", "supplier_conflict",
              "uncorroborated")
FIRM_CODE = "firm"


@dataclass(frozen=True)
class Verdict:
    """``tier`` FIRM, LEAD or None (nothing to link); ``code`` why; ``corroborated_by``
    the agreeing fields of a firm verdict (a subset of ``CORROBORATING``)."""
    tier: str | None
    code: str
    corroborated_by: tuple[str, ...] = ()


def days_apart(left: date | None, right: date | None) -> int | None:
    """Whole days between two order dates; None when either is unknown."""
    if not isinstance(left, date) or not isinstance(right, date):
        return None
    return abs((left - right).days)


def decide(*, portal_matches: int, bc_claimants: int, authority: bool,
           other_partner: bool, supplier: bool | None, order_days_apart: int | None
           ) -> Verdict:
    """The verdict on one BC order's reference against one Portal candidate.

    ``portal_matches``: the Portal orders holding the referenced number; ``bc_claimants``:
    the BC orders whose reference names this Portal order; ``authority``: the Portal
    order's lineage may be authority; ``other_partner``: either order is already joined to
    a different order of the other system; ``supplier``: True agrees, False disagrees,
    None unknown; ``order_days_apart``: :func:`days_apart` of the two order dates.
    Unknown inputs never corroborate (a type other than the documented one is unknown)."""
    if not isinstance(portal_matches, int) or isinstance(portal_matches, bool) \
            or portal_matches < 1:
        return Verdict(None, "no_match")
    if authority is not True:
        return Verdict(None, "lineage_not_authority")
    if portal_matches > 1:
        return Verdict(LEAD, "ambiguous_fm")
    if not isinstance(bc_claimants, int) or isinstance(bc_claimants, bool) \
            or bc_claimants != 1:
        return Verdict(LEAD, "ambiguous_bc")
    if other_partner is not False:
        return Verdict(LEAD, "conflict")
    if supplier is False:
        return Verdict(LEAD, "supplier_conflict")
    by = []
    if supplier is True:
        by.append(SUPPLIER)
    if isinstance(order_days_apart, int) and not isinstance(order_days_apart, bool) \
            and 0 <= order_days_apart <= DATE_WINDOW_DAYS:
        by.append(ORDER_DATE)
    if not by:
        return Verdict(LEAD, "uncorroborated")
    return Verdict(FIRM, FIRM_CODE, tuple(by))
