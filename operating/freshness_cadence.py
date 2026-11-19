"""The served freshness rule for a source with a unit late against its own cadence.

``ontology_tools_policy.aged`` judges a source by its newest capture time against the host's
bound. A delta read also records ``refresh_overdue`` (``ontology.delta.CADENCE_SECONDS``:
some unit of the source was not read within its usual interval, or the capture stopped), and
the newest unit's time must not call such a source fresh. Kept here, beside and not inside
``ontology_tools_policy``, so the derivation and document code digests stay as they are.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Mapping

from operating import ontology_tools_policy as policy


def aged(entry: Mapping[str, Any], bounds: Mapping[str, float], now: dt.datetime,
         failed: Iterable[str] = ()) -> dict:
    """``ontology_tools_policy.aged``, then stale with a label when the entry says a unit is
    overdue (an entry already stale keeps its own label). A copy served while the live source
    was out (``served_from``) is left to the policy."""
    out = policy.aged(entry, bounds, now, failed)
    if entry.get("refresh_overdue") is True and entry.get("served_from") in (None, "none"):
        return dict(out, stale=True, label=entry.get("label") or (
            "some of this source's units were not read within their usual interval"
            + (f" (newest read {entry['captured_at']})" if entry.get("captured_at") else "")))
    return out
