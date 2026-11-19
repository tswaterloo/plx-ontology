"""The system's own record before a prepared write (``prepared_writes.Desk(read=...)``).

An edit, a delete or an MC decision is shown and checked against the record as its system
holds it now, never against the model's word: one GET per record through the GET-only
client (``live_source_guard.SourceApi``, closed grammar) on the read applications the
captures use, or Mission Control's read tool (``mc_get_task``). Nothing here writes.

``SystemReader(environ, adapters)(person, action)`` answers:

* ``None``: nothing to read (a new record, a message);
* ``{"name", "values", "condition", "people", "requested_by", "blocked"}``: ``name`` the
  record as its system names it; ``values`` the current value of every field the write
  sets (the card's "before", and the undo's values); ``condition`` what the write's
  precondition must say (the etag the write sends as If-Match, a BC document's status and
  what of it shipped or was received), which the desk fills in or checks against Ask's;
  ``people`` who the record names (cleared by the people exclusion like the write's own);
  ``requested_by`` an MC gate's requester; ``blocked`` why the write cannot run on the
  record as it is (an MC gate already decided);

and raises ``Unreadable`` when the record cannot be read (refused by the desk).

Per system: BC (sales and purchase order headers, sales order lines, an order's delete
with its lines' shipped or received quantities; any entity set the adapter PATCHes by id,
customers and vendors included, through ``bc.entity`` of the environment and company the
adapter writes to), Planner (``graph.planner_task`` on the meetings read app, as captured), the owner's calendar
(``graph.user_event`` on the meetings read app; text body, times in UTC), SharePoint list
items (``graph.list_item_fields``), MC gates and Portal records (one row of the Portal's
read replica, ``integrations.portal_replica``, read-only; without it a Portal edit is
refused). A
SharePoint document's bytes are the document edit's own read
(``prepared_area.DocumentEditMixin``).
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Optional
import re

Get = Callable[[str, Mapping[str, str]], Any]
# (table, record id, columns) -> the row's columns, or None when there is no such row
PortalRow = Callable[[str, str, list], Optional[Mapping[str, Any]]]
REPLICA_DSN_ENV = "PLX_PORTAL_REPLICA_DSN"   # or the parts: integrations.portal_replica

_GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
# what Graph puts beside a record's own fields (never a value of a column the write sets)
_ODATA = re.compile(r"^@odata\.|@odata\.")


class Unreadable(Exception):
    """The record cannot be read now (the desk refuses: no before, no write)."""


def _bc_label(entity: str) -> str:
    return {"salesOrders": "Sales order", "purchaseOrders": "Purchase order",
            "customers": "Customer", "vendors": "Vendor", "salesOrderLines": "Sales line",
            "purchaseOrderLines": "Purchase line"}.get(entity, entity)


class SystemReader:
    """One reader per request (the backend builds it with the host environment and the
    executor's adapters, so it reads the BC environment and company the write goes to).
    ``graph``, ``calendar``, ``bc``: ``(url, headers) -> JSON`` GETs (tests inject fakes;
    default the guarded clients); ``mc``: ``task_id -> mc_get_task data``."""

    def __init__(self, environ: Mapping[str, str], adapters: Mapping[str, Any], *,
                 graph: Optional[Get] = None, calendar: Optional[Get] = None,
                 bc: Optional[Get] = None,
                 mc: Optional[Callable[[str], Mapping[str, Any]]] = None,
                 portal: Optional[PortalRow] = None) -> None:
        self.environ = dict(environ)
        self.adapters = adapters
        self._graph, self._calendar, self._bc, self._mc = graph, calendar, bc, mc
        self._portal = portal

    # ---- the clients (GET only) -------------------------------------------------------

    def _api_get(self, api: Any) -> Get:
        def get(url: str, headers: Mapping[str, str]) -> Any:
            return api.get_json(url, headers=dict(headers))
        return get

    def graph(self) -> Get:
        if self._graph is None:
            from integrations.graph_read import build_graph
            self._graph = self._api_get(build_graph(self.environ)[0])
        return self._graph

    def calendar(self) -> Get:
        if self._calendar is None:
            from integrations.graph_read import build_graph
            from integrations.live_source_guard import meetings_read_credentials
            self._calendar = self._api_get(build_graph(
                self.environ, credentials=meetings_read_credentials(self.environ))[0])
        return self._calendar

    def bc(self) -> Get:
        if self._bc is None:
            from integrations import live_source_guard as guard
            transport = guard.GuardedTransport(environ=self.environ)
            tokens = guard.TokenProvider(guard.ClientCredentials.from_env("BC", self.environ),
                                         guard.BC_SCOPE, transport)
            self._bc = self._api_get(guard.SourceApi("business-central", tokens, transport,
                                                     exact_integers=True))
        return self._bc

    def mc(self) -> Callable[[str], Mapping[str, Any]]:
        if self._mc is None:
            from integrations.mc import MissionControlMcpClient
            from integrations.plx_hub_guard import McConfig
            client = MissionControlMcpClient(McConfig.from_env(self.environ))
            self._mc = lambda task_id: client.get_task(task_id)["data"]
        return self._mc

    def portal(self) -> PortalRow:
        """One row of the Portal's read replica (``integrations.portal_replica``: the DSN or
        the host's replica settings, through the host's tunnel when named), in a read-only
        transaction; the table and columns come from ``PORTAL_ROUTES`` only."""
        if self._portal is None:
            from integrations import portal_replica
            if not portal_replica.configured(self.environ):
                raise Unreadable("no Portal replica")
            environ = dict(self.environ)

            def row(table: str, record: str, columns: list) -> Optional[Mapping[str, Any]]:
                from psycopg import sql
                try:
                    conn = portal_replica.connect(environ)   # read-only
                except portal_replica.ReplicaUnavailable as why:
                    raise Unreadable(f"Portal replica: {why}") from None
                with conn:
                    with conn.cursor() as cur:
                        cur.execute("set local statement_timeout = 5000")
                        cur.execute(sql.SQL("select {} from public.{} where id = %s").format(
                            sql.SQL(", ").join(sql.Identifier(c) for c in columns),
                            sql.Identifier(table)), (record,))
                        found = cur.fetchone()
                return dict(zip(columns, found)) if found else None
            self._portal = row
        return self._portal

    # ---- the one entry ----------------------------------------------------------------

    def __call__(self, person: Any, action: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        kind = str(action.get("kind") or "")
        operation = str(action.get("operation") or "")
        target = action.get("target") if isinstance(action.get("target"), Mapping) else {}
        payload = action.get("payload") if isinstance(action.get("payload"), Mapping) else {}
        step = {("bc_write", True): self._bc_record,
                ("planner_write", operation == "task_update"): self._planner,
                ("calendar_write", operation in ("event_update", "event_respond")): self._event,
                ("sharepoint_write", operation == "list_item_update"): self._list_item,
                ("mc_approval", operation == "approval_decide"): self._gate,
                ("portal_write", True): self._portal_record}.get((kind, True))
        if step is None:
            return None
        try:
            return step(person, operation, target, payload)
        except Unreadable:
            raise
        except Exception as error:  # noqa: BLE001 - any failure of the read: not read
            raise Unreadable(type(error).__name__) from None

    # ---- BC -----------------------------------------------------------------------------

    def _bc_record(self, person: Any, operation: str, target: Mapping[str, Any],
                   payload: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        from integrations import live_source_grammar as grammar
        adapter = self.adapters.get("bc_write")
        if adapter is None:
            raise Unreadable("no BC adapter")
        doc = getattr(adapter, "DOC_UPDATES", {}).get(operation)
        line = getattr(adapter, "LINE_UPDATES", {}).get(operation)
        delete = getattr(adapter, "DELETES", {}).get(operation)
        spec = doc or line or delete
        if spec is None:
            return None                        # a new document or line: nothing to read
        tenant = str(self.environ.get("BC_TENANT_ID") or "").strip().lower()
        # the company the write goes to: the one the action names among the configured
        # ones, else the configured default (``BCAdapter._company``)
        ids = {"tenant": tenant, "environment": adapter._environment(),
               "company": adapter._company({"target": dict(target)})}
        document = str(target.get("document_id") or "")
        entity = spec.get("set") or spec.get("parent")
        get = self.bc()
        record = get(grammar.url("bc.entity", entity=entity, record=document, **ids), {})
        if not isinstance(record, Mapping) or str(record.get("id") or "").lower() \
                != document.lower():
            raise Unreadable("not the document")
        status = record.get("status")
        party = record.get("customerName") or record.get("vendorName") or ""
        name = f"{_bc_label(entity)} {record.get('number') or ''}".strip() + (
            f" ({party})" if party else "")
        if doc is not None:
            return {"name": name, "values": self._fields(record, payload),
                    "condition": {"etag": record.get("@odata.etag"), "status": status}}
        lines_set = grammar.BC_DOCUMENT_LINES[entity]
        lines = get(grammar.url(f"bc.lines.{entity}", record=document, **ids), {})
        rows = [r for r in (lines.get("value") if isinstance(lines, Mapping) else None) or ()
                if isinstance(r, Mapping)]
        if isinstance(lines, Mapping) and lines.get("@odata.nextLink"):
            raise Unreadable("more lines than one page")
        if delete is not None:
            field = {"salesOrderLines": "shippedQuantity",
                     "purchaseOrderLines": "receivedQuantity"}[lines_set]
            done = sum(1 for r in rows if _number(r.get(field)) != 0)
            return {"name": name, "values": {},
                    "condition": {"etag": record.get("@odata.etag"), "status": status,
                                  delete["done_field"]: done}}
        line_id = str(target.get("line_id") or "").lower()
        found = [r for r in rows if str(r.get("id") or "").lower() == line_id]
        if len(found) != 1:
            raise Unreadable("line not found once")
        row = found[0]
        label = row.get("description") or row.get("lineObjectNumber") or row.get("sequence")
        return {"name": f"{name}, line {label}", "values": self._fields(row, payload),
                "condition": {"etag": row.get("@odata.etag"), "status": status}}

    # ---- Portal ---------------------------------------------------------------------------

    def _portal_record(self, person: Any, operation: str, target: Mapping[str, Any],
                       payload: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """An update's record as the Portal's read replica holds it: the values of the
        fields it sets (in the route's own format), the row's ``updated_at``, its name."""
        from operating.autopilot_write_adapters import PORTAL_ROUTES
        route = PORTAL_ROUTES.get(operation) or {}
        spec = route.get("record")
        if spec is None:
            return None                        # a create, a release, a note: nothing to read
        table, label_column, label, columns = spec
        record = str(target.get(route["target"][-1]) or "")
        wanted = [f for f in payload if f in columns]
        row = self.portal()(table, record, [*(columns[f] for f in wanted), label_column,
                                            "updated_at", "id"])
        if not isinstance(row, Mapping) or str(row.get("id") or "") != record:
            raise Unreadable("not the record")
        seen = row.get("updated_at")
        if not hasattr(seen, "isoformat"):
            raise Unreadable("no updated_at")
        values = {f: _portal_value(route["fields"][f][0], row.get(columns[f])) for f in wanted}
        return {"name": f"{label} {row.get(label_column) or ''}".strip(), "values": values,
                "condition": {"updated_at": seen.isoformat()}}

    # ---- Graph ----------------------------------------------------------------------------

    def _planner(self, person: Any, operation: str, target: Mapping[str, Any],
                 payload: Mapping[str, Any]) -> dict[str, Any]:
        from integrations import live_source_grammar as grammar
        # Planner is read on the meetings read app, the one the Planner capture uses
        # (``capture_meetings``); the mail app holds no Planner read (403 live, 11-18)
        task = self.calendar()(grammar.url("graph.planner_task",
                                        task=str(target.get("task_id") or "")), {})
        if not isinstance(task, Mapping) or task.get("id") != target.get("task_id"):
            raise Unreadable("not the task")
        assigned = task.get("assignments") if isinstance(task.get("assignments"), Mapping) \
            else {}
        return {"name": str(task.get("title") or ""), "values": self._fields(task, payload),
                "condition": {"etag": task.get("@odata.etag")},
                "people": sorted(str(k).lower() for k in assigned)}

    def _event(self, person: Any, operation: str, target: Mapping[str, Any],
               payload: Mapping[str, Any]) -> dict[str, Any]:
        from integrations import live_source_grammar as grammar
        owner = str(target.get("user") or "").lower()
        # the owner's own calendar only: their Entra object id from their sign-in
        oids = [i for i in getattr(person, "ids", ()) if _GUID.fullmatch(str(i))]
        if not owner or owner not in getattr(person, "ids", ()) or len(oids) != 1:
            raise Unreadable("the calendar's owner is not known")
        event = self.calendar()(grammar.url(
            "graph.user_event", {"$select": "subject,body,start,end,location,organizer,"
                                            "attendees"},
            user=oids[0], event=str(target.get("event_id") or "")),
            {"Prefer": 'outlook.body-content-type="text", outlook.timezone="UTC"'})
        if not isinstance(event, Mapping):
            raise Unreadable("not an event")
        shaped = {"subject": event.get("subject"),
                  "body": (event.get("body") or {}).get("content")
                  if isinstance(event.get("body"), Mapping) else None,
                  "location": (event.get("location") or {}).get("displayName")
                  if isinstance(event.get("location"), Mapping) else None,
                  **{k: {"dateTime": (event.get(k) or {}).get("dateTime"),
                         "timeZone": (event.get(k) or {}).get("timeZone")}
                     for k in ("start", "end") if isinstance(event.get(k), Mapping)}}
        people = [str(((a or {}).get("emailAddress") or {}).get("address") or "").lower()
                  for a in [event.get("organizer"), *(event.get("attendees") or ())]
                  if isinstance(a, Mapping)]
        return {"name": str(event.get("subject") or ""), "values": self._fields(shaped, payload),
                "condition": {}, "people": [p for p in people if p]}

    def _list_item(self, person: Any, operation: str, target: Mapping[str, Any],
                   payload: Mapping[str, Any]) -> dict[str, Any]:
        from integrations import live_source_grammar as grammar
        fields = self.graph()(grammar.url(
            "graph.list_item_fields", site=str(target.get("site_id") or ""),
            list=str(target.get("list_id") or ""), item=str(target.get("item_id") or "")), {})
        if not isinstance(fields, Mapping):
            raise Unreadable("not a list item")
        # a column Graph leaves out is empty there (it answers only the set ones)
        return {"name": str(fields.get("Title") or f"item {target.get('item_id')}"),
                "values": {k: fields.get(k) for k in payload if not _ODATA.search(str(k))},
                "condition": {"etag": fields.get("@odata.etag")}}

    # ---- Mission Control ----------------------------------------------------------------

    def _gate(self, person: Any, operation: str, target: Mapping[str, Any],
              payload: Mapping[str, Any]) -> dict[str, Any]:
        task_id, gate_id = str(target.get("task_id") or ""), str(target.get("gate_id") or "")
        data = self.mc()(task_id)
        task = data.get("task") if isinstance(data, Mapping) else None
        gates = [g for g in ((task or {}).get("approvalGates") or ())
                 if isinstance(g, Mapping) and g.get("id") == gate_id]
        if len(gates) != 1:
            raise Unreadable("gate not found once")
        gate = gates[0]
        status = str(gate.get("status") or "").strip().lower()
        requester = str(gate.get("requestedBy") or "").strip().lower()
        title = str((task or {}).get("title") or task_id)
        scope = gate.get("scope")
        return {"name": f"{title}: {str(gate.get('reason') or gate_id)[:200]}",
                "values": {}, "condition": {"status": status},
                "requested_by": requester or None, "people": [requester] if requester else [],
                # the gate's own scope ({kind: launch|merge|flags, ...}) as MC states it:
                # the decide route renders exactly this back (``renderedScope``)
                "scope": dict(scope) if isinstance(scope, Mapping) else None,
                "blocked": None if status == "pending" else (
                    f"That gate is {status or 'not open'} in Mission Control now: nothing to "
                    "decide.")}

    # ---- shared --------------------------------------------------------------------------

    @staticmethod
    def _fields(record: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        """The current value of each field the write sets that the record holds (a field
        it does not hold stays out: the desk then cannot show or undo it, so refuses)."""
        return {k: record[k] for k in payload if k in record and not _ODATA.search(str(k))}


def _number(value: Any) -> Any:
    from decimal import Decimal, InvalidOperation
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value


def _portal_value(kind: str, value: Any) -> Any:
    """A replica value in the route's own format (a date as YYYY-MM-DD, an instant in ISO
    form), so the card's before and the undo say what the route takes."""
    if value is None:
        return None
    if kind in ("date", "date_or_null") and hasattr(value, "date"):
        return value.date().isoformat() if hasattr(value, "hour") else value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
