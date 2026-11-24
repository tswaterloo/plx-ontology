"""Every GET route of the API with its family and its time budget (speed plan section 2.7, rule
check 1): a GET route answers from a stored read, never by computing in the request. A route
missing here does not land (``operator_api.tests.test_route_rule``).

Families:

* ``local``: answered from the process (session, config, a counter): no store read.
* ``served``: one indexed read of stored rows through the reader's door (``operator_api.
  served``), passed through.
* ``door``: one read of the business views through the reader's door, computed by Postgres
  in that statement (no worker, no kept page), and only one indexed read of at most 500 rows;
  a route aggregating in the request moves to ``served`` (items or measure rows) by its piece.
* ``computed``: still computed per request (a workspace, a worker, a kept page, a plan); the
  debt. ``replaced_by`` names the speed plan piece that moves it to ``served``. The static
  gate warns on these (``warn`` mode) until piece 9 turns it hard.
* ``file``: a download or an upload's bytes (size-bound, not a page).
* ``external``: a sign-in callback another system calls.

``budget_ms`` is the p99 the route must meet at the API (the page has 100 ms; network and
paint take the rest). For ``computed`` it is still the target; ``measured`` is the last
measured (p50, p95) in ms from the live latency log (24 h to 2026-11-23 7 PM ET, GET only)
so the debt is stated, not hidden. ``file`` and ``external`` carry no budget (None): their
time is the bytes' or the other system's.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

FAMILIES = ("local", "served", "door", "computed", "file", "external")


class Budget(NamedTuple):
    family: str
    budget_ms: Optional[int]
    replaced_by: Optional[int] = None          # speed plan piece (``computed`` only)
    measured: Optional[tuple[int, int]] = None  # (p50, p95) ms, live, when measured
    serves: tuple[str, ...] = ()               # the page families this route serves (SERVES)

    @property
    def sv(self) -> Optional[str]:
        """The serving piece that moves it (speed specs: piece n of the plan is SVn)."""
        return None if self.replaced_by is None else f"SV{self.replaced_by}"


def _c(piece: int, measured: Optional[tuple[int, int]] = None) -> Budget:
    return Budget("computed", 60, piece, measured)


ROUTES: dict[str, Budget] = {
    # process-local
    "/api/health": Budget("local", 20, None, (2, 10)),
    "/api/ready": Budget("local", 20, None, (1, 14)),
    "/api/me": Budget("local", 20, None, (4, 36)),
    "/api/auth/config": Budget("local", 20, None, (2, 18)),
    "/api/alert-kinds": Budget("local", 20, None, (12, 21)),
    "/api/records/views": Budget("local", 20, None, (5, 16)),
    "/api/autopilot/catalog": Budget("local", 20, None, (4, 4)),
    "/api/teams/connection": Budget("local", 20, None, (8, 19)),
    # one read through the door
    "/api/biz/page": Budget("door", 60, None, (38, 412)),
    # the served store (speed specs 1.2): one read of stored items
    "/api/page/{kind}/{key:path}": Budget("served", 60),
    "/api/page/history/{key:path}": Budget("served", 60),
    "/api/records/list": _c(7, (36, 493)),
    # the debt, by the plan piece that serves it from the store
    "/api/object/{object_type}/{object_id:path}": _c(3, (894, 2033)),
    "/api/as-of/{object_type}/{object_id:path}": _c(3),
    "/api/explore/{object_type}/{object_id:path}": _c(3),
    "/api/record-pack/{object_type}/{object_id:path}": _c(3),
    "/api/brief/{object_type}/{object_id:path}": _c(3, (51, 51)),
    "/api/object-section/{section}/{object_type}/{object_id:path}": _c(4, (130, 37815)),
    "/api/object-impact/{mode}/{object_type}/{object_id:path}": _c(4),
    "/api/home": _c(5, (20, 657)),
    "/api/home/bundle": _c(5, (794, 3109)),
    "/api/sidebar": _c(5, (15, 237)),
    "/api/briefing": _c(5, (34, 1515)),
    "/api/briefing/items": _c(5, (60, 479)),
    "/api/briefing/pages": _c(5),
    "/api/briefing/unjudged": _c(5),
    "/api/suggestions": _c(5, (195, 2002)),
    "/api/replies": _c(5, (81, 1688)),
    "/api/approvals": _c(5, (94, 1771)),
    "/api/my-work": _c(5, (561, 61764)),
    "/api/my-work/{item_id}/order-import": _c(5),
    "/api/autopilot": _c(5, (137, 822)),
    "/api/autopilot/drafts": _c(5, (80, 901)),
    "/api/autopilot/audit": _c(5, (2, 54)),
    "/api/autopilot/colleagues": _c(5, (2, 2)),
    "/api/autopilot/schedules": _c(5, (105, 105)),
    "/api/autopilot/workflows/{workflow}": _c(5),
    "/api/actions/state": _c(5, (38, 214)),
    "/api/actions/log": _c(5, (4, 29)),
    "/api/prepared": _c(5, (129, 129)),
    "/api/prepared/{prepared_id}": _c(5),
    "/api/writes": _c(5, (72, 354)),
    "/api/writes/{intent_id}/preview": _c(5),
    "/api/watch": _c(5, (79, 841)),
    "/api/memory": _c(5, (18, 93)),
    "/api/memory/people": _c(7, (23, 979)),
    "/api/work-log": _c(5, (13, 95)),
    "/api/work-log/autopilot-days": _c(5, (14, 53)),
    "/api/work-log/threads/{thread:path}": _c(5),
    "/api/ask/chats": _c(5, (10, 126)),
    "/api/entries/{entry_id}": _c(5),
    "/api/instructions/recurring": _c(5, (7, 20)),
    "/api/meetings/action-items": _c(5, (29, 316)),
    "/api/meetings/follow-up/{meeting_id}": _c(5),
    "/api/alerts/{alert_id}/playbook": _c(5),
    "/api/feeds": _c(5, (15, 89)),
    "/api/digests": _c(5, (2, 2)),
    "/api/feedback": _c(5, (3, 8)),
    "/api/view-as/people": _c(5, (7, 7)),
    "/api/brief/weekly": _c(6, (47202, 150008)),
    "/api/brief/meetings": _c(6, (67, 1763)),
    "/api/late": _c(6, (68, 1294)),
    "/api/team": _c(6, (127, 702)),
    "/api/team/{key}": _c(6, (752, 22940)),
    "/api/labour": _c(6, (101, 237)),
    "/api/workload": _c(6, (84, 411)),
    "/api/workload/grants": _c(6, (46, 100)),
    "/api/trust": _c(6, (171, 567)),
    "/api/time-saved": _c(6, (22, 614)),
    "/api/data-health": _c(6, (29, 3206)),
    "/api/data-health/export": _c(6, (3, 3)),
    "/api/ops/status": _c(6, (15, 87)),
    "/api/insights": _c(7, (29, 126)),
    "/api/insights/{insight_id}": _c(7, (22, 148)),
    "/api/insights/all/stream": _c(7, (3, 25)),
    "/api/insights/{insight_id}/why": _c(7),
    "/api/insight-news": _c(7, (47, 196)),
    "/api/history/{object_type}/{object_id:path}": _c(7, (699, 2572)),
    "/api/label-changes/{object_id:path}": _c(7, (11069, 11069)),
    "/api/label-revisions/{object_id:path}": _c(7, (2353, 11055)),
    "/api/source": _c(7, (5, 244)),
    "/api/source/records": _c(7, (371, 757)),
    "/api/suggest": _c(7, (307, 1216)),
    "/api/templates": _c(7, (37, 259)),
    "/api/templates/answer-bank": _c(7, (11, 1524)),
    "/api/templates/learned": _c(7, (7, 705)),
    "/api/templates/suggest": _c(7),
    # bytes, not pages
    "/api/instruct/files/{file_id}": Budget("file", None),
    "/api/instruct/uploads/{upload_id}": Budget("file", None),
    # another system calls it
    "/api/teams/callback": Budget("external", None),
}


# The page FAMILIES a route serves (business rule 11-23: a served route REPLACES the old one, never
# sits beside it): a family is served by exactly one GET route (``family_routes``; the route rule
# test fails on two). ``/api/page/{kind}/{key}`` names a family here in the SAME commit that
# removes it from the old routes and switches the app (SV3 per kind, SV5 home, SV6 weekly);
# until then it serves none, though its route exists and fills.
RECORD_KINDS = ("PurchaseOrder", "SalesOrder", "Customer", "Supplier", "Product", "Batch",
                "SalesInvoice", "SupplierInvoice")
SERVES: dict[str, tuple[str, ...]] = {
    "/api/object/{object_type}/{object_id:path}": tuple(f"record:{k}" for k in RECORD_KINDS)
    + ("record:other",),
    "/api/biz/page": ("record:Customer", "record:Supplier", "record:PurchaseOrder",
                      "record:SalesOrder", "record:Product", "record:Batch",
                      "record:SupplierInvoice", "chain"),
    "/api/home": ("home",),
    "/api/brief/weekly": ("weekly",),
    # the History tab (speed specs 9.3 V2): a purchase order's from the served store only; every
    # other kind's still computed on the old route until its own switch
    "/api/page/history/{key:path}": ("history:PurchaseOrder",),
    "/api/history/{object_type}/{object_id:path}": tuple(
        f"history:{k}" for k in RECORD_KINDS if k != "PurchaseOrder") + ("history:other",),
}
ROUTES = {path: b._replace(serves=SERVES.get(path, ())) for path, b in ROUTES.items()}


def family_routes() -> dict[str, list[str]]:
    """Each page family with the GET routes that serve it (more than one is a defect)."""
    out: dict[str, list[str]] = {}
    for path, b in sorted(ROUTES.items()):
        for fam in b.serves:
            out.setdefault(fam, []).append(path)
    return out


def debt() -> dict[str, list[str]]:
    """The routes still computed per request, by the serving piece that moves them."""
    out: dict[str, list[str]] = {}
    for path, b in sorted(ROUTES.items()):
        if b.family == "computed":
            out.setdefault(str(b.sv), []).append(path)
    return out
