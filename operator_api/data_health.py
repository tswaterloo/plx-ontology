"""Admin, Data health: every check the alert registry files under the data-health surface
(``operating/alert_kinds.json``: customers or suppliers entered twice, key fields missing,
orders without a customer PO, records out of step with their other system), each with the
findings this reader may see and the count of those on records they may not open.

The findings are the scheduled engine's (the gap inbox), read through the reader's own
workspace trim (``ObjectWorkspace.data_health_gaps``): a finding is listed only when its
subject and a citation of each evidence group pass the reader's grant and item decision;
the rest are a number per check, never an id, a subject or a summary. A check the registry
keeps off is named, never read. A failed read is ``unread``, never zeros."""
from __future__ import annotations

from typing import Any, Mapping

from . import models as m
from .serving import object_url

READ_LIMIT = 2000      # findings read per page (the inbox's own order)
LISTED = 25            # findings listed per check; the rest counted


def registered() -> list[Mapping[str, Any]]:
    """The registry's data-health types, in its order."""
    from operating import alert_kinds
    reg = alert_kinds.registry()
    kinds = {k["kind"] for k in reg.get("kinds") or () if k.get("surface") == "data_health"}
    return [t for t in reg.get("types") or () if t.get("kind") in kinds]


def page(view: Any) -> m.DataHealth:
    types = registered()
    on = [str(t["type"]) for t in types if t.get("default") == "on"]
    found = view.data_health_gaps(on, limit=READ_LIMIT) if on else {"status": "read",
                                                                       "gaps": [], "hidden": {}}
    status = found.get("status")
    if status == "not_configured":
        return m.DataHealth(status="not_configured")
    if status != "read":
        return m.DataHealth(status="unread")
    gaps = [g for g in found.get("gaps") or () if isinstance(g, Mapping)]
    hidden = found.get("hidden") or {}
    checks = []
    for t in types:
        kind = str(t["type"])
        if t.get("default") != "on":
            checks.append(m.DataHealthCheck(type=kind, label=str(t.get("label") or kind),
                                            on=False))
            continue
        mine = [g for g in gaps if g.get("kind") == kind]
        items = [m.DataHealthItem(
            id=str(g.get("id")), object_id=str(g.get("object_id")),
            object_type=str(g.get("object_type") or ""), summary=str(g.get("summary") or ""),
            open_url=object_url(str(g.get("object_type") or "Object"), str(g.get("object_id"))))
            for g in mine[:LISTED]]
        checks.append(m.DataHealthCheck(
            type=kind, label=str(t.get("label") or kind), on=True, count=len(mine),
            hidden=max(0, int(hidden.get(kind) or 0)), items=items,
            more=max(0, len(mine) - len(items))))
    return m.DataHealth(status="read", checks=checks, as_of=found.get("as_of"),
                        partial=len(gaps) >= READ_LIMIT or bool(found.get("truncated")))
