"""Which purchase orders are BC pilot orders: one rule, used by every late-PO check (the
``po_late``/``po_due``/``po_undated`` alerts, the late list, the sidebar's late by supplier)
and by the supplier lead-time tile, so none carries a second filter of its own."""

from __future__ import annotations

SYSTEMS_OF_RECORD = ("sage", "filemaker")


def bc_pilot_po_sql(alias: str = "") -> str:
    """SQL true for a purchase order only BC holds: a BC pilot PO. An order any other system
    holds too (Portal, Sage, FileMaker) counts as an order: it is left out of nothing.
    ``source_systems`` is the view's comma-joined list of the systems holding the order;
    unknown holders: not a pilot order."""
    col = f"{alias}." if alias else ""
    return f"(coalesce({col}source_systems, '') = 'bc')"


def bc_pilot_record_sql(system_col: str) -> str:
    """SQL true for a record only BC holds (its ``system`` is 'bc', a system that is no
    system of record): BC pilot data, ranked after the systems of record in a search."""
    return f"({system_col} = 'bc' AND 'bc' NOT IN ({', '.join(repr(x) for x in SYSTEMS_OF_RECORD)}))"


def bc_pilot_object_sql(object_id_col: str) -> str:
    """SQL true for an object id of a BC binding (``Type:bc.<table>:<key>``): BC's own copy,
    the pilot's, ranked after the system of record's of the same name or number."""
    return f"({object_id_col} ~ '^[^:]+:bc[.]')"


def is_bc_pilot_object(object_id: str | None) -> bool:
    """The same rule for one object id (``bc_pilot_object_sql``)."""
    head, _, rest = (object_id or "").partition(":")
    return bool(head) and rest.startswith("bc.")


def is_bc_pilot_po(source_systems: str | None) -> bool:
    """The same rule for one order's ``source_systems`` (tests hold the two together)."""
    return (source_systems or "") == "bc"
