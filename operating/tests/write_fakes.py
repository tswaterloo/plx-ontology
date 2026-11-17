"""Faithful in-process fakes of the systems the autopilot writes (isolated-fixture).

``FakeSystems.send`` has the guarded sender's signature (``live_source_guard.
urllib_sender``: method, url, headers, body, timeout, max_bytes) and answers what the
documented APIs answer for the write shapes the guard admits:

* Business Central API v2.0: ``companies(<id>)/salesOrders|purchaseOrders`` POST (201 with
  ``id``, ``number``, ``status`` Draft, ``@odata.etag``), PATCH / DELETE with ``If-Match``
  (412 on a stale etag, 400 once shipped or received), contained lines POST/PATCH/DELETE;
  an unknown company answers 404. Only the companies it was built with exist.
* Graph calendar: ``users/<u>/events`` POST, ``events/<id>`` PATCH/DELETE.
* Graph Planner: ``planner/tasks`` POST, ``planner/tasks/<id>`` PATCH/DELETE with
  ``If-Match`` (412 when stale).
* Graph SharePoint lists: ``sites/<s>/lists/<l>/items`` POST, ``items/<n>/fields`` PATCH,
  ``items/<n>`` DELETE.

``FakeMC.send`` answers MC's MCP JSON-RPC ``tools/call`` for ``mc_report_progress`` (a
comment on the task, 403 without an active checkout like MC) behind a real
``HubTransport``. Every request is recorded (``requests``) with its bearer redacted.
Nothing here reaches a network.
"""

from __future__ import annotations

import itertools
import json
import re
from typing import Any, Mapping

BC_HOST = "https://api.businesscentral.dynamics.com"
GRAPH_HOST = "https://graph.microsoft.com"
_GUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_BC = re.compile(rf"^/v2\.0/(?P<tenant>{_GUID})/(?P<env>[A-Za-z0-9_-]+)/api/v2\.0/"
                 rf"companies\((?P<company>{_GUID})\)/(?P<set>salesOrders|purchaseOrders)"
                 rf"(?:\((?P<doc>{_GUID})\)(?:/(?P<lines>salesOrderLines|purchaseOrderLines)"
                 rf"(?:\((?P<line>{_GUID})\))?)?)?$")
_EVENTS = re.compile(r"^/v1\.0/users/(?P<user>[^/]+)/events(?:/(?P<id>[^/]+))?$")
_TASKS = re.compile(r"^/v1\.0/planner/tasks(?:/(?P<id>[^/]+))?$")
_ITEMS = re.compile(r"^/v1\.0/sites/(?P<site>[^/]+)/lists/(?P<list>[^/]+)/items"
                    r"(?:/(?P<n>\d+)(?P<fields>/fields)?)?$")


def _answer(status: int, body: Any = None) -> tuple[int, dict[str, str], bytes]:
    raw = b"" if body is None else json.dumps(body).encode()
    return status, {"content-type": "application/json"}, raw


class FakeSystems:
    """BC, calendar, Planner and SharePoint, in memory."""

    def __init__(self, companies: Mapping[str, str], environment: str) -> None:
        self.companies = {k.lower(): v for k, v in companies.items()}
        self.environment = environment
        self.docs: dict[str, dict[str, Any]] = {}
        self.lines: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self._ids = itertools.count(1)

    def _guid(self) -> str:
        return f"{next(self._ids):08x}-0000-4000-8000-000000000000"

    def _etag(self, row: dict[str, Any]) -> str:
        row["_v"] = row.get("_v", 0) + 1
        row["@odata.etag"] = f'W/"{row["_v"]}"'
        return row["@odata.etag"]

    @staticmethod
    def _public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in row.items() if not k.startswith("_")}

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
             timeout: float, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
        heads = {k.lower(): v for k, v in headers.items()}
        assert heads.get("authorization", "").startswith("Bearer ")
        data = json.loads(body) if body else None
        self.requests.append({"method": method, "url": url, "body": data,
                              "if_match": heads.get("if-match")})
        if url.startswith(BC_HOST):
            return self._bc(method, url[len(BC_HOST):], heads, data)
        if url.startswith(GRAPH_HOST):
            path = url[len(GRAPH_HOST):]
            for pattern, handler in ((_EVENTS, self._event), (_TASKS, self._task),
                                     (_ITEMS, self._item)):
                found = pattern.fullmatch(path)
                if found:
                    return handler(method, found, heads, data)
        return _answer(404, {"error": {"code": "NotFound"}})

    # ---------------------------------------------------------------- BC
    def _bc(self, method: str, path: str, heads: Mapping[str, str], data: Any):
        m = _BC.fullmatch(path)
        if not m or m["env"] != self.environment:
            return _answer(404, {"error": {"code": "BadRequest_ResourceNotFound"}})
        if m["company"] not in self.companies:
            return _answer(404, {"error": {"code": "Internal_CompanyNotFound"}})
        if m["doc"] is None:
            if method != "POST":
                return _answer(405, {"error": {"code": "BadRequest_MethodNotAllowed"}})
            doc = {**data, "id": self._guid(), "number": f"X{len(self.docs) + 1:05d}",
                   "status": "Draft", "_set": m["set"], "_company": m["company"],
                   "_done": 0}
            self._etag(doc)
            self.docs[doc["id"]] = doc
            return _answer(201, self._public(doc))
        doc = self.docs.get(m["doc"])
        if doc is None or doc["_set"] != m["set"] or doc["_company"] != m["company"]:
            return _answer(404, {"error": {"code": "BadRequest_NotFound"}})
        if m["lines"] is None:
            if heads.get("if-match") not in (doc["@odata.etag"], "*"):
                return _answer(412, {"error": {"code": "Request_EntityChanged"}})
            if method == "PATCH":
                doc.update(data or {})
                self._etag(doc)
                return _answer(200, self._public(doc))
            if method == "DELETE":
                if doc["_done"] or doc["status"] not in ("Draft", "Open"):
                    return _answer(400, {"error": {"code": "Application_DialogException"}})
                del self.docs[doc["id"]]
                return _answer(204)
            return _answer(405, {"error": {"code": "BadRequest_MethodNotAllowed"}})
        if m["line"] is None:
            if method != "POST":
                return _answer(405, {"error": {"code": "BadRequest_MethodNotAllowed"}})
            line = {**data, "id": self._guid(), "documentId": doc["id"], "_doc": doc["id"]}
            self._etag(line)
            self.lines[line["id"]] = line
            return _answer(201, self._public(line))
        line = self.lines.get(m["line"])
        if line is None or line["_doc"] != doc["id"]:
            return _answer(404, {"error": {"code": "BadRequest_NotFound"}})
        if heads.get("if-match") not in (line["@odata.etag"], "*"):
            return _answer(412, {"error": {"code": "Request_EntityChanged"}})
        if method == "PATCH":
            line.update(data or {})
            self._etag(line)
            return _answer(200, self._public(line))
        if method == "DELETE":
            del self.lines[line["id"]]
            return _answer(204)
        return _answer(405, {"error": {"code": "BadRequest_MethodNotAllowed"}})

    # ---------------------------------------------------------------- Graph
    def _crud(self, store: dict[str, dict[str, Any]], method: str, key: str | None,
              heads: Mapping[str, str], data: Any, *, etag_required: bool, extra=None):
        if key is None:
            if method != "POST":
                return _answer(405, {"error": {"code": "MethodNotAllowed"}})
            row = {**(data or {}), **(extra or {}), "id": str(next(self._ids))}
            self._etag(row)
            store[row["id"]] = row
            return _answer(201, self._public(row))
        row = store.get(key)
        if row is None:
            return _answer(404, {"error": {"code": "ErrorItemNotFound"}})
        if etag_required and heads.get("if-match") not in (row["@odata.etag"], "*"):
            return _answer(412, {"error": {"code": "PreconditionFailed"}})
        if method == "PATCH":
            row.update(data or {})
            self._etag(row)
            return _answer(200 if not etag_required else 204,
                           self._public(row) if not etag_required else None)
        if method == "DELETE":
            del store[key]
            return _answer(204)
        return _answer(405, {"error": {"code": "MethodNotAllowed"}})

    def _event(self, method, m, heads, data):
        return self._crud(self.events, method, m["id"], heads, data, etag_required=False,
                          extra={"_user": m["user"]})

    def _task(self, method, m, heads, data):
        return self._crud(self.tasks, method, m["id"], heads, data, etag_required=True)

    def _item(self, method, m, heads, data):
        if m["n"] is None:
            return self._crud(self.items, method, None, heads,
                              {"fields": (data or {}).get("fields") or {}},
                              etag_required=False)
        row = self.items.get(m["n"])
        if row is None:
            return _answer(404, {"error": {"code": "itemNotFound"}})
        if method == "PATCH" and m["fields"]:
            row["fields"].update(data or {})
            self._etag(row)
            return _answer(200, dict(row["fields"]))
        if method == "DELETE" and not m["fields"]:
            del self.items[m["n"]]
            return _answer(204)
        return _answer(405, {"error": {"code": "MethodNotAllowed"}})


class FakeMC:
    """MC's MCP door for ``mc_report_progress``: MC adds the note as a comment of the
    operator; without an active checkout of the task it answers 403 (TASK-2560)."""

    URL = "https://mc.plxcustomer.io/api/cursor/mcp"

    def __init__(self, checked_out: set[str]) -> None:
        self.checked_out = set(checked_out)
        self.comments: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
             timeout: float, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
        assert method == "POST" and url == self.URL
        request = json.loads(body or b"{}")
        heads = {k.lower(): v for k, v in headers.items()}
        self.requests.append({"tool": request["params"]["name"],
                              "arguments": request["params"]["arguments"],
                              "operator": heads.get("x-mc-operator-email")})
        args = request["params"]["arguments"]
        if request["params"]["name"] != "mc_report_progress":
            payload, error = {"error": {"code": "forbidden", "message": "no"}}, True
        elif args.get("taskId") not in self.checked_out:
            payload, error = {"error": {"code": "forbidden",
                                        "message": "no active checkout"}}, True
        else:
            self.comments.append({"taskId": args["taskId"], "notes": args["notes"]})
            payload, error = {"data": {"taskId": args["taskId"], "ok": True}}, False
        result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
        if error:
            result["isError"] = True
        return _answer(200, {"jsonrpc": "2.0", "id": request.get("id"), "result": result})
