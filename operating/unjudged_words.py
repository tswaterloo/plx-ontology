"""What a check could not judge, said as a plain sentence a person can act on.

Each line names how many records and why, in words ("64 purchase orders: a document
that may be the supplier's confirmation is linked but not tied to the order, so the
date isn't settled"); never a reason's code name. Lines are ordered by who can fix them:
first what a person fixes by entering or correcting a record, then what needs a person to
read a document, then what waits on us (a source we do not read yet), each by count.

A check's scope (records it never applies to, such as receipts in a system that keeps no
quality checks) is one short note, never a count of records it "cannot check".
"""
from __future__ import annotations

from typing import Any, Mapping

# The records a check judges, singular and plural; a check not named here says "records".
NOUNS = {
    "late_or_unconfirmed_po": ("purchase order", "purchase orders"),
    "late_order": ("purchase order with late lines", "purchase orders with late lines"),
    "receipt_without_qc": ("receipt", "receipts"),
    "mc_task_stuck": ("Mission Control task", "Mission Control tasks"),
    "production_late": ("production order", "production orders"),
}
# A reason about a part of the record (a line), whatever check said it.
PART_NOUNS = {
    "line_order_unlinked": ("late order line", "late order lines"),
    "line_order_ambiguous": ("late order line", "late order lines"),
    "line_value_disputed": ("order line", "order lines"),
}

# Who can fix it: a person in the source record (1), a person reading a document (2),
# or us (3: a source or a meaning we do not read yet).
ENTER, READ, OURS = 1, 2, 3

# reason -> (who can fix it, the sentence after "N records:")
SENTENCES: dict[str, tuple[int, str]] = {
    # a person fixes the record itself
    "no_need_date": (ENTER, "no due date is recorded on the order or any copy of it"),
    "no_required_date": (ENTER, "no required date is recorded"),
    "required_date_default": (ENTER, "the only required date is one an import or form "
                                     "filled in by default"),
    "required_date_disputed": (ENTER, "two different required dates are recorded"),
    "need_date_disputed": (ENTER, "two different due dates are recorded"),
    "status_missing": (ENTER, "no status is recorded"),
    "status_disputed": (ENTER, "two systems record different statuses"),
    "nothing_ordered": (ENTER, "no line has a quantity ordered"),
    "quantities_unknown": (ENTER, "the ordered or received quantities can't be read"),
    "date_unparseable": (ENTER, "a date is recorded in a form that can't be read"),
    "due_date_unreadable": (ENTER, "the due date is recorded in a form that can't be read"),
    "no_scheduled_date": (ENTER, "no scheduled date is set"),
    "due_date_ambiguous": (ENTER, "more than one due date is recorded"),
    "no_order_lines": (ENTER, "the order has no lines"),
    "stage_missing": (ENTER, "no stage is recorded"),
    "line_value_disputed": (ENTER, "the line holds two different dates or quantities"),
    "partly_fulfilled": (ENTER, "part is shipped or invoiced and the rest isn't "
                                "reconciled"),
    # a person reads a document
    "confirmation_unsettled": (READ, "a document that may be the supplier's confirmation "
                                     "is linked but not tied to the order, so the date "
                                     "isn't settled"),
    "confirmation_undated": (READ, "the supplier's confirmation gives no delivery date"),
    "promises_disagree": (READ, "the supplier's confirmations give different dates"),
    "qc_link_unsettled": (READ, "a quality record may belong to the receipt but isn't tied "
                                "to it"),
    "fulfilment_unsettled": (READ, "a shipment or invoice may be theirs but isn't tied to "
                                   "them"),
    "closed_in_unsettled_twin": (READ, "only a copy not yet tied to the record shows it "
                                       "closed"),
    "line_order_ambiguous": (READ, "the line is linked to more than one order"),
    # waits on us
    "status_unmapped": (OURS, "the status recorded isn't one the check reads yet"),
    "stage_unmapped": (OURS, "the stage recorded isn't one the check reads yet"),
    "line_order_unlinked": (OURS, "the line isn't linked to an order"),
    "updated_at_unknown": (OURS, "Mission Control gives no time the task last changed"),
    "status_source_absent": (OURS, "no copy is in the system that keeps its status"),
    "completion_not_served": (OURS, "whether it was finished isn't read yet"),
    "not_projected": (OURS, "the record isn't in the served data yet"),
    "per_unit_unknown": (ENTER, "the cost per unit can't be worked out from the quantity "
                                "and unit recorded"),
    "no_qc_source_for_system": (OURS, "the system that holds it keeps no quality checks"),
    "value_unreadable": (ENTER, "a value recorded on it, or on its copy, can't be read"),
    "free_line": (ENTER, "the line was given at no charge, so there is no price to compare"),
    "quantity_unreadable": (ENTER, "a quantity recorded on it can't be read"),
    "on_hand_unknown": (OURS, "the quantity on hand isn't known"),
    "identity_unsettled": (READ, "the item may be the same as another but isn't tied to it"),
    "line_unreadable": (ENTER, "a line on the order can't be read"),
    "on_hand_reading_stale": (OURS, "the last stock count on hand is too old to rely on"),
    "dates_unreadable": (ENTER, "a date recorded on it is in a form that can't be read"),
}
UNKNOWN_SENTENCE = (OURS, "the check couldn't read what it needs")

# Reasons that are no gap at all: never listed (the check passed or does not apply).
NOT_GAPS = frozenset({"order_not_late"})
# Reasons that are the check's scope, not a fault in a record: said once as a note without a
# count, never as N records it "cannot check". Sage leaves orders open it will never close
# (so an abandoned one never reads as late).
# A copy only the Portal holds of a production order FileMaker keeps (2026-11-17: 394 of
# 400 open Portal-only assembly orders and all 340 batches have no FileMaker record by
# their number while FileMaker still records new ones; on 11-11, 13 of 15 sampled late
# alerts on Portal copies were realized, rescheduled or deleted in FileMaker).
SCOPE_REASONS: dict[Any, str] = {
    "sage_unmapped": "Sage leaves old orders open, so lateness isn't judged for them.",
    ("production_late", "status_source_absent"):
        "Production orders found only in the Portal aren't checked: FileMaker keeps "
        "production status, and the Portal's copies lag it.",
}


def scope_notes() -> dict[str, str]:
    """check -> the one short note on the records it never applies to (said once, without
    a count; ``data_notes`` shows it for a check that ran)."""
    from .proactive_engine_config import NO_QC_SOURCE_BINDINGS

    systems = sorted({_SYSTEMS.get(b.split(".", 1)[0], b.split(".", 1)[0])
                      if not b.startswith("portal.sage") else "Sage"
                      for b in NO_QC_SOURCE_BINDINGS})
    return {
        "receipt_without_qc": f"Receipts kept only in {' or '.join(systems)} aren't "
                              "checked: those systems keep no quality checks.",
        "late_order": "Draft orders nothing was received against aren't checked: a draft "
                      "has no delivery date to be late against.",
    }


_SYSTEMS = {"bc": "Business Central", "portal": "the Portal", "mc": "Mission Control"}


def line(check: str, check_words: str, reason: str, count: int) -> tuple[int, str]:
    """(who can fix it, the sentence) for ``count`` records ``check`` left unjudged for
    ``reason``."""
    rank, sentence = SENTENCES.get(reason, UNKNOWN_SENTENCE)
    one, many = PART_NOUNS.get(reason) or NOUNS.get(check) or ("record", "records")
    noun = one if count == 1 else many
    if check in NOUNS or reason in PART_NOUNS:
        return rank, f"{count:,} {noun}: {sentence}"
    return rank, f"{count:,} {noun} for {check_words}: {sentence}"


def notes(rules: Any, check_words: Mapping[str, str]) -> list[dict[str, Any]]:
    """The data notes of an inbox's ``rules`` (counts this reader's trimmed read gives):
    one per check and reason, ordered by who can fix it then by count, then one scope
    note per check that ran and has one."""
    found: list[tuple[int, int, str, dict[str, Any]]] = []
    ran: list[str] = []
    scoped: list[tuple[str, str]] = []
    for rule in rules or ():
        if not isinstance(rule, Mapping):
            continue
        name = str(rule.get("rule"))
        if rule.get("status") not in (None, "not_run", "not_configured"):
            ran.append(name)
        for reason, n in (rule.get("not_evaluated_by_reason") or {}).items():
            if not isinstance(n, int) or isinstance(n, bool) or n <= 0 or reason in NOT_GAPS:
                continue
            key = (name, str(reason)) if (name, str(reason)) in SCOPE_REASONS else str(reason)
            if key in SCOPE_REASONS:
                if all(key != k for _, k in scoped):   # said once, whichever check
                    scoped.append((name, key))
                continue
            rank, words = line(name, check_words.get(name, name), str(reason), n)
            found.append((rank, -n, f"{name}:{reason}",
                          {"check": name, "reason": str(reason), "count": n,
                           "words": words, "scope": False}))
    out = [note for *_key, note in sorted(found, key=lambda f: f[:3])]
    scopes = scope_notes()
    out += [{"check": name, "reason": "out_of_scope", "count": 0, "words": scopes[name],
             "scope": True} for name in sorted(set(ran)) if name in scopes]
    out += [{"check": name, "reason": key if isinstance(key, str) else key[1], "count": 0,
             "words": SCOPE_REASONS[key], "scope": True}
            for name, key in sorted(scoped, key=lambda s: (s[0], str(s[1])))]
    return out
