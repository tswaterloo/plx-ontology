"""Whether an order is confirmed, by the one rule (``po_status.confirmation_state``), read
with the ``plx`` tools as one principal. Every page and answer that says whether a purchase
order is confirmed reads it here (the record page's verdict and supplier line, the evidence
trail), so they never disagree. Kept apart from ``po_status`` (which feeds the build's
digests) as a reader of its rules, not a rule of its own."""
from __future__ import annotations

from typing import Any, Callable, Mapping

from operating import po_status as ps


def order_confirmation(call: Callable[[str, dict], Mapping[str, Any]], oid: str,
                       most_copies: int | None = None) -> dict[str, Any]:
    """The order ``oid``'s confirmations: ``members`` (its copies, ``ps.copies``; the first
    ``most_copies`` when given, the root always first), ``blind`` (a twin link hidden from
    this reader), ``links`` (the counted links, ``ps.counted_confirmations``; None when a
    copy's listing is not complete for this reader), ``collides`` (a lead may be another
    order's: its number is held twice, or unread) and ``state``
    (``ps.confirmation_state``; None when the links could not all be read, so neither found
    nor none is said)."""
    source = ps.ToolSource(call)
    members, blind = ps.copies(source, oid)
    if most_copies is not None:
        members = members[:most_copies]
    raw = source.confirm_links(members)
    collides = raw is None or (any(link["tier"] not in ps.CONFIRMING for link in raw)
                               and source.colliding(members))
    counted = None if raw is None else ps.counted_confirmations(raw, lambda _oid: collides)
    state = None if raw is None else ps.confirmation_state(
        counted, ps.reused_leads(raw, lambda _oid: collides))
    return {"members": members, "blind": bool(blind), "links": counted,
            "collides": collides, "state": state}
