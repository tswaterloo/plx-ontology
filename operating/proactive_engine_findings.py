"""Shared finding helpers of the proactive engine: citations, markings, the gap and result
records every rule returns, and the link types each rule reads."""

from __future__ import annotations

from collections import Counter
from datetime import date
import functools
import hashlib
import json
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from ontology import store as ostore

from . import po_status
from . import proactive_gaps as pg
from . import time_window

from .proactive_engine_config import QC_LINKS, QC_TYPES, UNREADABLE

if TYPE_CHECKING:
    from .proactive_engine_config import Cite

WHOLE_BY_INDEX_MAX = 300_000   # an end type of up to this many records: read by index


def _link_ends(types: Sequence[str]) -> dict[str, list[str]]:
    """link type -> its declared end types (schema ``from``/``to``): a population reads
    the type whole through the end index of the smaller one (every link of the type has
    that end); none declared: a scan of the link table."""
    from ontology.schema import load_schema

    declared = load_schema().link_types
    return {t: [e for e in ((declared.get(t) or {}).get("from"), (declared.get(t) or {})
                            .get("to")) if isinstance(e, str) and e] for t in types}


def _canonical(value: Any) -> Any:
    """``value`` as JSON in one order (a population's digest)."""
    if isinstance(value, Mapping):
        return sorted(([_canonical(k), _canonical(v)] for k, v in value.items()), key=repr)
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(v) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _markings(*texts: str | None) -> str:
    """The union of markings values, canonical; anything unreadable never shows."""
    held: set[str] = set()
    for text in texts:
        try:
            parsed = json.loads(text) if text else []
        except (TypeError, ValueError):
            return UNREADABLE
        if not isinstance(parsed, list) or not all(isinstance(m, str) for m in parsed):
            return UNREADABLE
        held.update(parsed)
    try:
        return ostore.markings_text(held)
    except ostore.StoreError:
        return UNREADABLE


def _cites(rows: Iterable[Mapping[str, Any]], *extra_markings: str | None) -> list[Cite]:
    out: dict[str, Cite] = {}
    for row in rows:
        handle = row.get("l1_handle")
        if isinstance(handle, str) and handle and handle not in out:
            out[handle] = (handle, row.get("object_id"),
                           _markings(row.get("markings"), *extra_markings))
    return list(out.values())


_clusters = po_status.clusters   # member -> its settled cluster's least object id


def _day(raw: Any) -> date | None:
    return time_window.business_day(raw)


def _gap(kind: str, detector: str, object_id: str, object_type: str, key: str, *,
         summary: str, missing: str, why: str, priority: str, about: tuple[str, str],
         groups: Sequence[Sequence[Cite]], extra: Sequence[Cite] = (),
         days_late: int | None = None, event: Mapping[str, Any] | None = None,
         **facts: Any) -> dict[str, Any] | None:
    """``days_late``: the days a purchase order open by the one status definition is past its
    requested-by date (None: not late; ``late_order``'s line-level lateness is another gap
    kind and never carries it); the inbox lists every late purchase order first, longest late
    first, whatever its kind's priority. ``event``: the facts the briefing ranks by
    (``days``: days between two cited dates; ``amount``: a cited figure, shown only with
    every group or, with a ``handle``, only to a reader who may open that citation;
    ``orders_affected``: a count of cited records), kept beside the follow-up. ``facts``:
    enum words the pages read (``confirmation``: ``none_found``, ``found_unverified`` or
    ``confirmed``; ``due_basis``: where the due date comes from)."""
    groups = [list(group) for group in groups]
    if not groups or not all(groups):
        return None   # a claim with an uncited part is not raised
    handles = list(dict.fromkeys([c[0] for g in groups for c in g] + [c[0] for c in extra]))
    who = pg._who(object_type, {"about": about, "handles": handles})
    return {"id": "pe-" + hashlib.sha256(f"{kind}|{object_id}|{key}".encode("utf-8"))
            .hexdigest()[:16],
            "kind": kind, "detector": detector, "object_id": object_id,
            "object_type": object_type, "summary": pg._clip(summary),
            "groups": groups, "extra": list(extra),
            "follow_up": {"object_id": object_id, "missing": pg._clip(missing, 480),
                          "why": why, "who_can_answer": who, "state": "unknown",
                          "priority": priority,
                          **({"days_late": days_late} if days_late is not None else {}),
                          **({"event": dict(event)} if event else {}),
                          # enums only (``confirmation``, ``due_basis``): never a value
                          **{k: v for k, v in facts.items() if v is not None}}}


Part = tuple  # (object_id, canonical markings): one record a skip's cause reads


def _parts(rows: Iterable[Mapping[str, Any]], *extra_markings: str | None) -> list[Part]:
    """The records ``rows`` were read from, each at its row's markings joined with
    ``extra_markings`` (the links that tie it to the rest of the cause)."""
    return [(row.get("object_id"), _markings(row.get("markings"), *extra_markings))
            for row in rows]


class _Skips(Counter):
    """A rule's records it did not judge: the count per reason (the run summary) and each
    skip with its subjects (the records it left unjudged) and its cause (every record and
    link, at its markings, the decision to skip read: ``skips``). A reader is told how many
    of the records it may read a rule left unjudged, counted only over skips whose whole
    cause it may read, the way a gap shows only when the reader may open all of its
    evidence: a skip caused by a record or a link it may not read (an identity twin
    disputing the need date, a lead from a hidden confirmation) is not counted for it, so
    no count names or hints at a hidden record. A skip no record stands for (an order the
    store never projected) or whose cause names a value without its record is
    ``unattributed``: unknown for every reader alike."""

    def __init__(self) -> None:
        super().__init__()
        self.skips: list[tuple[tuple[str, ...], tuple[Part, ...]]] = []
        self.reasons: list[str] = []   # each skip's reason, in ``skips`` order
        self.unattributed = 0

    def note(self, reason: str, subjects: Sequence[str] = (),
             cause: Iterable[Part] = ()) -> None:
        self[reason] += 1
        parts = {(subject, "[]") for subject in subjects}
        for object_id, markings in cause:
            if not isinstance(object_id, str) or not object_id:
                parts = set()   # a cause no reader can be checked against: fail closed
                break
            parts.add((object_id, markings if isinstance(markings, str) else UNREADABLE))
        if not subjects or not parts:
            self.unattributed += 1
            return
        self.skips.append((tuple(sorted(set(subjects))), tuple(sorted(parts))))
        self.reasons.append(reason)


def _result(gaps: Iterable[dict[str, Any] | None], *, status: str = "complete",
            reasons: Sequence[str] = (), not_evaluated: Counter | None = None
            ) -> dict[str, Any]:
    skips = not_evaluated if isinstance(not_evaluated, _Skips) else None
    counted = sum((not_evaluated or Counter()).values())
    return {"status": status, "reasons": list(reasons),
            "gaps": [gap for gap in gaps if gap is not None],
            "not_evaluated": dict(sorted((not_evaluated or Counter()).items())),
            "unjudged": [{"subjects": list(subjects), "cause": [list(p) for p in cause],
                          "reason": reason}
                         for (subjects, cause), reason in zip(skips.skips, skips.reasons)]
            if skips is not None else [],
            # a count without its records (a plain Counter) is attributed to nobody
            "unattributed": skips.unattributed if skips is not None else counted}


def _checked(result: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """(how many records a rule had to judge in this run, judged and left unjudged; how
    many of them it judged): its own ``checked`` count less what it left unjudged, else its
    coverage's ``total`` and ``evaluated``. (None, None), not counted, when it says
    neither, says a count that does not add up (judged above checked, or checked below
    what it left unjudged: never a share above 100%), or its run did not finish (a
    cut-short run judged an unknown part)."""
    unknown = (None, None)
    if result.get("status") != "complete":
        return unknown
    try:
        unjudged = sum(int(v) for v in (result.get("not_evaluated") or {}).values())
    except (TypeError, ValueError, AttributeError):
        return unknown
    coverage = result.get("coverage") if isinstance(result.get("coverage"), Mapping) else {}
    if result.get("checked") is not None:
        checked, judged = result.get("checked"), None
    else:
        checked, judged = coverage.get("total"), coverage.get("evaluated")
    if isinstance(checked, bool) or not isinstance(checked, int) or checked < unjudged:
        return unknown
    if judged is None:
        judged = checked - unjudged
    if isinstance(judged, bool) or not isinstance(judged, int) or not 0 <= judged <= checked \
            or judged > checked - unjudged:
        return unknown
    return checked, judged


def _coverage(total: int, evaluated: int, not_evaluated: Mapping[str, int], *,
              unit: str) -> dict[str, Any]:
    """A detector's coverage: of ``total`` records of ``unit`` it should judge, how many it
    judged and, by reason, how many it did not (never silently omitted)."""
    missing = {k: int(v) for k, v in sorted(not_evaluated.items()) if v}
    return {"unit": unit, "total": int(total), "evaluated": int(evaluated),
            "not_evaluated": missing}


# The rule modules whose detectors read links (``_Store.links``): every link type a module
# names (``_rule_link_types``) is read in the run's one scan, never a scan of its own at its
# detector's turn (2026-11-08 live store: each scan of the 55M-row link table 60-130 s, the
# order-to-cash, price and data-quality rules three to four each).
RULE_MODULES = ("proactive_business", "proactive_ops", "proactive_ceo", "proactive_chasers",
                "proactive_cash", "proactive_stock", "proactive_prices", "proactive_sales",
                "proactive_quality", "proactive_commitments")


@functools.lru_cache(maxsize=1)
def _rule_link_types() -> tuple[str, ...]:
    """Every schema link type the rule modules name in their constants (upper-case module
    names: a text, or texts in a tuple, list, set or mapping), in module then name order."""

    from ontology.schema import load_schema

    known = set(load_schema().link_types)
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if value in known:
                found.append(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                walk(item)
        elif isinstance(value, (set, frozenset)):
            for item in sorted(value, key=str):
                walk(item)
    # imported by name, never a computed one (the HTTP egress gate reads every import)
    from . import (proactive_business, proactive_cash, proactive_ceo, proactive_chasers,
                   proactive_commitments, proactive_ops, proactive_prices, proactive_quality,
                   proactive_sales, proactive_stock)
    modules = {"proactive_business": proactive_business, "proactive_ops": proactive_ops,
               "proactive_ceo": proactive_ceo, "proactive_chasers": proactive_chasers,
               "proactive_cash": proactive_cash, "proactive_stock": proactive_stock,
               "proactive_prices": proactive_prices, "proactive_sales": proactive_sales,
               "proactive_quality": proactive_quality,
               "proactive_commitments": proactive_commitments}
    for name in RULE_MODULES:
        module = modules[name]
        for constant, value in vars(module).items():
            if constant.isupper():
                walk(value)
    return tuple(dict.fromkeys(found))


def _identity_link_types() -> tuple[str, ...]:
    from ontology.schema import load_schema
    from .ontology_tools_identity import identity_link_type

    return tuple(sorted(lid for lid, link in load_schema().link_types.items()
                        if identity_link_type(link)))


@functools.lru_cache(maxsize=1)
def declared_qc_links() -> tuple[str, ...]:
    """Link types the schema declares as a QC decision: ``QC_LINKS`` when declared, and
    every link between a QC record type (served by the Portal later) and anything."""
    from ontology.schema import load_schema

    links = load_schema().link_types
    return tuple(sorted(lid for lid, link in links.items()
                        if lid in QC_LINKS or link.get("from") in QC_TYPES
                        or link.get("to") in QC_TYPES))
