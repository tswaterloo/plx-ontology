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
LISTED = 25            # findings a check lists on the page's first read; the rest are paged''
PAGE_MAX = 100         # findings one page of a check returns


def registered() -> list[Mapping[str, Any]]:
    """The registry's data-health types, in its order."""
    from operating import alert_kinds
    reg = alert_kinds.registry()
    kinds = {k["kind"] for k in reg.get("kinds") or () if k.get("surface") == "data_health"}
    return [t for t in reg.get("types") or () if t.get("kind") in kinds]


def summary(view: Any) -> m.DataHealth:
    """The page's first read: each check's open count with no per-finding decision (cheap,
    well under a second); findings are read per check on demand (``page(view, check)``)."""
    types = registered()
    on = [str(t["type"]) for t in types if t.get("default") == "on"]
    found = view.data_health_counts(on) if on else {"status": "read", "counts": {}}
    status = found.get("status")
    if status == "not_configured":
        return m.DataHealth(status="not_configured")
    if status != "read":
        return m.DataHealth(status="unread")
    counts = found.get("counts") or {}
    checked = found.get("checked") or {}
    checks = []
    for t in types:
        kind = str(t["type"])
        label = str(t.get("label") or kind)
        if t.get("default") != "on":
            checks.append(m.DataHealthCheck(type=kind, label=label, on=False))
        else:
            # a rule that never ran has no count: "not checked", never 0
            known = "checked" in found           # a host that says when each rule ran
            at = checked.get(kind)
            checks.append(m.DataHealthCheck(
                type=kind, label=label, on=True, read=False,
                open=max(0, int(counts.get(kind) or 0)) if at or not known else None,
                checked_at=at))
    return m.DataHealth(status="read", checks=checks, as_of=found.get("as_of"))


def page(view: Any, check: str | None = None,
         checks: list[str] | tuple[str, ...] | None = None) -> m.DataHealth:
    """The findings this reader may see, all checks or one (``check``) or a subset
    (``checks``): the trimmed read."""
    types = registered()
    if check is not None:
        types = [t for t in types if t.get("type") == check]
    elif checks is not None:
        want = set(checks)
        types = [t for t in types if t.get("type") in want]
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
            for g in mine]
        checks.append(m.DataHealthCheck(
            type=kind, label=str(t.get("label") or kind), on=True, count=len(mine),
            hidden=max(0, int(hidden.get(kind) or 0)), items=items,
            open=len(mine) + max(0, int(hidden.get(kind) or 0))))
    return m.DataHealth(status="read", checks=checks, as_of=found.get("as_of"),
                        partial=len(gaps) >= READ_LIMIT or bool(found.get("truncated")))


def _filter_items(items: list[m.DataHealthItem], q: str | None = None,
                  object_type: str | None = None) -> list[m.DataHealthItem]:
    """Filter findings by free-text query ``q`` (summary, record type, record id) and/or
    exact record type ``object_type`` (case-insensitive)."""
    out = items
    if object_type:
        want_type = object_type.strip().lower()
        if want_type:
            out = [it for it in out if it.object_type.strip().lower() == want_type]
    if q:
        needle = q.strip().lower()
        if needle:
            out = [it for it in out if (
                needle in it.summary.lower()
                or needle in it.object_type.lower()
                or needle in it.object_id.lower()
            )]
    return out


def window(full: m.DataHealth, check: str | None = None, offset: int = 0,
           limit: int = LISTED, q: str | None = None,
           object_type: str | None = None) -> m.DataHealth:
    """The page's first read (no ``check``: each check's first ``LISTED`` findings) or one
    check's findings from ``offset`` (``limit`` of them, at most ``PAGE_MAX``), cut from the
    one read the reader's page keeps (a page of findings never reads the store again).
    Optional ``q`` and ``object_type`` filter across ALL rows before slicing so counts
    reflect the exact filtered total. A check the reader does not have is not found: an
    empty page, never another's rows."""
    limit = max(1, min(int(limit), PAGE_MAX))
    offset = max(0, int(offset))
    if full.status != "read":
        return full
    filtering = bool((q and q.strip()) or (object_type and object_type.strip()))
    out = []
    for c in full.checks:
        if check is not None and c.type != check:
            continue
        pool = _filter_items(c.items, q=q, object_type=object_type) if filtering else c.items
        if check is None:
            start, stop = 0, LISTED
        else:
            start, stop = offset, offset + limit
        shown = pool[start:stop]
        update: dict[str, Any] = {
            "items": shown,
            "more": max(0, len(pool) - start - len(shown)),
        }
        if filtering:
            update["count"] = len(pool)
        out.append(c.model_copy(update=update))
    return full.model_copy(update={"checks": out})


def export_rows(full: m.DataHealth, check: str | None = None,
                q: str | None = None, object_type: str | None = None) -> list[list[str]]:
    """Every finding the reader may see (one check's, or all), as rows of
    check, what was found, record type, record page; spreadsheet formulas are defused."""
    filtering = bool((q and q.strip()) or (object_type and object_type.strip()))
    rows = [["Check", "Finding", "Record type", "Record page"]]
    for c in full.checks:
        if not c.on or (check is not None and c.type != check):
            continue
        pool = _filter_items(c.items, q=q, object_type=object_type) if filtering else c.items
        for it in pool:
            rows.append([_cell(c.label), _cell(it.summary), _cell(it.object_type),
                         _cell(it.open_url)])
    return rows


def _cell(text: str) -> str:
    """Text a spreadsheet will not run: a leading = + - @ tab or return gets a quote."""
    t = str(text or "").replace("\r", " ").replace("\n", " ")
    return "'" + t if t[:1] in ("=", "+", "-", "@", "\t") else t
