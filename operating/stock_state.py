"""Whether a lot still holds stock and whether a batch is really on quality hold: the one
definition every rule, count and answer about lots and batches reads (the proactive
engine's ``lot_attention``, the fast answers ``lots_expiring_count``, ``batch_status`` and
``batch_hold_count``). The live-ops answer key (``evals/live_ops_keys.py``) may not import
product code; it restates these rules independently and must agree with them.

* **A lot holds stock** while its QC status says so (``STOCK_LOT``: released, in
  quarantine, ...). A lot used up, rejected, consumed or expired (``GONE_LOT``) does not.
  A lot with no status, or a word neither list knows, is not judged (``None``): never
  claimed, counted as not evaluated.
* **A batch is on quality hold** when its QC value is a hold word, its status is neither
  closed (cancelled) nor unknown, and the hold is NOT its lifecycle state's default in its
  own system: the share ``source_defaults`` names of every batch of that system in that
  status (``source_defaults.bulk_values``; a state too small there is read over every
  system's batches in it). The QC value every unmade batch carries before release says
  nothing about any one of them; a made batch (realized) still on hold is one Quality
  has not released. An unmade batch in a state too small to profile is not judged.
* **A batch and its copy are one batch**: ``Batch.same_record_as`` links a source batch
  (from) to the migrated record it was copied into (to); the origin decides and the batch
  is counted once, on it.

No value of any one record is named here; the words are the sources' vocabularies.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping

from . import source_defaults

HOLD_WORDS = frozenset({"on-hold", "on hold", "hold", "onhold"})
CLOSED_BATCH = frozenset({"cancelled", "canceled", "closed"})
MADE_BATCH = frozenset({"realized", "realised", "completed", "complete", "manufactured",
                        "produced"})
ACTIVE_BATCH = frozenset({"allocated", "committed", "generated", "scheduled", "planned",
                          "in progress", "released"})
BATCH_TWIN_LINK = "Batch.same_record_as"   # from: the origin batch, to: its copy

STOCK_LOT = frozenset({"released", "quarantine", "quarantined", "approved", "available",
                       "on hold", "on-hold", "hold", "in stock"})
GONE_LOT = frozenset({"finished", "rejected", "consumed", "used", "used up", "depleted",
                      "expired", "written off", "scrapped", "destroyed", "returned",
                      "closed", "cancelled", "canceled"})

# What ``batch_hold`` decides.
HOLD, NOT_HELD, STATE_DEFAULT, CLOSED, UNMAPPED, UNPROFILED = (
    "hold", "not_held", "state_default", "closed", "status_unmapped", "state_unprofiled")


def word(value: Any) -> str:
    """A status as a word: case-blind, underscores and runs of spaces one space."""
    return " ".join(str(value if value is not None else "").replace("_", " ").split()
                    ).casefold()


def lot_holds_stock(status: Any) -> bool | None:
    """True: the QC status says the lot holds stock; False: used up, rejected or the
    like; None: no status or an unmapped one (not judged)."""
    said = word(status)
    if said in STOCK_LOT:
        return True
    if said in GONE_LOT:
        return False
    return None


def hold_profile(pairs: Iterable[tuple[Any, Any]]) -> dict[str, Counter]:
    """``(status, QC value)`` of every batch of ONE system -> per status word, how many of
    its batches carry each QC word."""
    out: dict[str, Counter] = defaultdict(Counter)
    for status, qc in pairs:
        if word(status) and word(qc):
            out[word(status)][word(qc)] += 1
    return dict(out)


def pooled(*profiles: Mapping[str, Counter]) -> dict[str, Counter]:
    """Several systems' profiles as one (a state too small in one system is read over
    every system's batches in it)."""
    out: dict[str, Counter] = defaultdict(Counter)
    for profile in profiles:
        for state, counts in profile.items():
            out[state].update(counts)
    return dict(out)


def batch_hold(status: Any, qc: Any, profile: Mapping[str, Counter],
               wider: Mapping[str, Counter] | None = None) -> str:
    """``HOLD`` when the batch is really on quality hold, else why not: ``NOT_HELD`` (no
    hold word), ``CLOSED`` (cancelled), ``UNMAPPED`` (a status neither list knows),
    ``STATE_DEFAULT`` (the QC value a bulk share of the batches in that state carry,
    ``source_defaults.bulk_values``) or ``UNPROFILED``. A state is profiled over its own
    system's batches (``profile``), or over ``wider`` (every system's, ``pooled``) when
    its own holds fewer than ``source_defaults.MINIMUM``; a state too small in both is
    not profiled: a made batch still on hold is a hold all the same, an unmade one is
    not judged (``UNPROFILED``)."""
    qc_word, said = word(qc), word(status)
    if qc_word not in HOLD_WORDS:
        return NOT_HELD
    if said in CLOSED_BATCH:
        return CLOSED
    if said not in ACTIVE_BATCH | MADE_BATCH:
        return UNMAPPED
    counts = next((c for c in ((profile or {}).get(said), (wider or {}).get(said))
                   if c and sum(c.values()) >= source_defaults.MINIMUM), None)
    if counts is None:
        return HOLD if said in MADE_BATCH else UNPROFILED
    if qc_word in source_defaults.bulk_values(counts.elements(),
                                              share=source_defaults.STATE_SHARE):
        return STATE_DEFAULT
    return HOLD


def made(status: Any) -> bool:
    return word(status) in MADE_BATCH


def held_states(profile: Mapping[str, Counter], statuses: Iterable[Any], qc: Any
                ) -> list[str]:
    """Of ``statuses`` (as the source writes them), those where a batch carrying ``qc``
    is really on hold (``batch_hold``), each once in the order given."""
    out: list[str] = []
    for status in statuses:
        if isinstance(status, str) and batch_hold(status, qc, profile) == HOLD \
                and status not in out:
            out.append(status)
    return out
