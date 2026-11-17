"""A stored mention row standing for several occurrences (CC-C-04 physical collapse).

``ontology/mentions.py`` writes one ``mention:`` row per occurrence of an identifier in a
document (subject and body, a quoted line, each span). ``operating.pg.mention_collapse``
folds a document's repeated rows of one link into one row in place: the kept row (the
**keeper**: of the rows of one key, the strongest tier, then the least id, as
``operating.pg.mention_repeats`` and the read side pick it) keeps its id and every column,
and its reason gains one feature, :data:`FEATURE`, holding each folded row (a **twin**) as
it was: its occurrence digest (:func:`occurrence`), its reason text and its why_not text,
byte for byte. A twin is folded only when every other column equals the keeper's (tier,
markings, evidence handles, contract, recorded and superseded times, no review), so the
twin is the keeper's row with its own reason and why_not.

The readers that judge one occurrence at a time (``ontology.mention_firm`` for
``unique_mentions``, ``ontology.mc_task_links`` for ``task_ids``: mention groups, list-file
counts, vetoes, the exact span of an MC task's text) read :func:`views`: every row as the
rows it stands for, each with the keeper's id. A row without the feature is its own one
view, unchanged (a store never collapsed reads exactly as before). Two views of one keeper
can each give a firm row of the same id; :func:`one_per_lead` keeps the one of the least
occurrence, which is the firm row the uncollapsed store held for the least twin, under the
keeper's id (the remap the collapse applies to ``crb-mid:`` and ``crb-mcid:`` rows).

Stdlib only.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator, Mapping

FEATURE = "identifier_occurrences"
OCCURRENCE = "_occurrence"   # the transient key a view and its firm row carry
LEAD_PREFIX = "mention:"
DIGEST_CHARS = 32


def occurrence(link_id: str) -> str:
    """The occurrence digest of a mention row's id: the 32 hex characters after
    ``mention:`` (the document, the mention's citation, field and span, the target and its
    version), without the ``#held``, candidate piece or ``@<from>`` review piece suffix the
    keeper and its twins share."""
    if link_id.startswith(LEAD_PREFIX):
        return link_id[len(LEAD_PREFIX):len(LEAD_PREFIX) + DIGEST_CHARS]
    return link_id


def _features(text: Any) -> list[Any]:
    if isinstance(text, list):
        return text
    try:
        found = json.loads(text) if text else []
    except (TypeError, ValueError):
        return []
    return found if isinstance(found, list) else []


def folded(reason: Any) -> list[Mapping[str, Any]] | None:
    """The twins a reason's :data:`FEATURE` holds, None for a row that holds none."""
    for feature in _features(reason):
        if isinstance(feature, dict) and feature.get("feature") == FEATURE:
            twins = feature.get("occurrences")
            return [t for t in twins if isinstance(t, dict)] if isinstance(twins, list) else []
    return None


def own_reason(reason_text: str) -> str:
    """A keeper's reason text without :data:`FEATURE`, as the row was before the
    collapse (the feature is appended last, so the text before it is the stored text)."""
    at = reason_text.rfind(_TAIL)
    if at > 0 and reason_text.endswith("]"):
        return reason_text[:at] + "]"   # byte for byte what the row held
    found = _features(reason_text)
    kept = [f for f in found if not (isinstance(f, dict) and f.get("feature") == FEATURE)]
    if len(kept) == len(found):
        return reason_text
    return canon(kept)


# the feature as the collapse appends it to a reason's JSON array (its keys in order); a
# twin's reason inside it is a JSON string, so this text never occurs inside one
_TAIL = ',{"feature":"' + FEATURE + '","occurrences":['


def canon(value: Any) -> str:
    """The mentions step's JSON text (``ontology.mentions``: sorted keys, no spaces)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def views(rows: Iterable[Any]) -> Iterator[dict[str, Any]]:
    """Each stored row as the rows it stands for (module docstring), in order: the row
    itself (its own reason), then its twins in the order the feature lists them. The
    views of a collapsed row carry :data:`OCCURRENCE`; a row holding no twins is its one
    view as stored."""
    for row in rows:
        row = dict(row)
        twins = folded(row.get("reason"))
        if twins is None:   # its own one view, as stored (no transient key)
            yield row
            continue
        yield dict(row, reason=own_reason(row["reason"]), **{OCCURRENCE: occurrence(row["link_id"])})
        for twin in twins:
            yield dict(row, reason=twin.get("reason"), why_not=twin.get("why_not"),
                       **{OCCURRENCE: str(twin.get("occurrence"))})


def one_per_lead(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Firm rows with one per id: of the rows of one id (views of one keeper), the one of
    the least :data:`OCCURRENCE`; first-seen order kept, the transient key dropped."""
    best: dict[str, tuple[str, dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        row = dict(row)
        occ = str(row.pop(OCCURRENCE, ""))
        lid = row["link_id"]
        if lid not in best:
            order.append(lid)
            best[lid] = (occ, row)
        elif occ < best[lid][0]:
            best[lid] = (occ, row)
    return [best[lid][1] for lid in order]


__all__ = ["FEATURE", "OCCURRENCE", "canon", "folded", "occurrence", "one_per_lead",
           "own_reason", "views"]
