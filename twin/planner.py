"""The Planner twin: Microsoft Graph's plannerTask routes the Planner adapter calls,
answered in process (``TwinTransport``), and the ``planner-task://`` citations of its
tasks for the case store. A task is held under the PlannerTask schema columns
(``ontology/schema/domains/meetings.json``); Graph's own names are served on the routes.
Like Graph, an update needs ``If-Match`` the task's current etag (412 otherwise) and
answers 204; every write bumps the etag. Nothing here reaches the network.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Mapping
from urllib.parse import unquote

from ontology.core import Actor
from operating.cases import CitationError

from .mode import TWIN_LABEL

RELATION = "meetings.planner_tasks"   # the Workbench relation (binding meetings.planner_tasks)
ROUTE = re.compile(r"^/v1\.0/planner/tasks/(?P<id>[^/]+)$")
HANDLE = re.compile(r"^planner-task://(?P<plan>[^/@]{1,200})/(?P<task>[^/@]{1,200})@twin$")
# schema column -> Graph plannerTask property (the ones the twin serves)
GRAPH_NAMES = {"id": "id", "title": "title", "percent_complete": "percentComplete",
               "due_at": "dueDateTime"}
WRITABLE = frozenset({"dueDateTime"})


class PlannerTwin:
    """``readers``: principals that may read the tasks (``None``: any actor)."""

    SYSTEM = "planner"

    def __init__(self, *, readers: frozenset[str] | None = None) -> None:
        self.readers = readers
        self.tasks: dict[str, dict[str, Any]] = {}
        self.etags: dict[str, int] = {}
        self.writes = 0

    def seed(self, row: Mapping[str, Any]) -> str:
        task, plan = row.get("id"), row.get("plan_id")
        if not isinstance(task, str) or not task or not isinstance(plan, str) or not plan:
            raise ValueError("task_without_id_or_plan")
        self.tasks[task] = copy.deepcopy(dict(row))
        self.etags[task] = 1
        return self.citation(plan, task)

    @staticmethod
    def citation(plan: str, task: str) -> str:
        return f"planner-task://{plan}/{task}@twin"

    def _etag(self, task: str) -> str:
        return f'W/"twin-{task}-{self.etags[task]}"'

    def resolve(self, handle: str, actor: Actor) -> dict[str, Any]:
        """The case store's citation resolver for ``planner-task://<plan>/<task>@twin``."""
        if self.readers is not None and not set(actor.principals) & set(self.readers):
            raise CitationError("not visible")
        found = HANDLE.fullmatch(handle) if isinstance(handle, str) else None
        if found is None:
            raise CitationError("not_a_twin_handle")
        row = self.tasks.get(found["task"])
        if row is None or row.get("plan_id") != found["plan"]:
            raise CitationError("twin_row_absent")
        return {"relation": RELATION, "key": {"id": row["id"]}, "row": copy.deepcopy(row),
                "evidence": TWIN_LABEL}

    def handle(self, request: Any) -> dict[str, Any]:
        """Answer one ``OwnerRequest`` as Graph's plannerTask route would."""
        found = ROUTE.fullmatch(request.path)
        task = unquote(found["id"]) if found else None
        if found is None or request.method not in ("GET", "PATCH"):
            return {"status": 404, "body": {"error": {"code": "NotFound"}}}
        row = self.tasks.get(task)
        if row is None:
            return {"status": 404, "body": {"error": {"code": "NotFound"}}}
        if request.method == "GET":
            body = {graph: row.get(column) for column, graph in GRAPH_NAMES.items()}
            return {"status": 200, "body": {**body, "@odata.etag": self._etag(task)}}
        if (request.headers or {}).get("If-Match") != self._etag(task):
            return {"status": 412, "body": {"error": {"code": "PreconditionFailed"}}}
        body = request.body
        if not isinstance(body, Mapping) or not body or set(body) - WRITABLE:
            return {"status": 400, "body": {"error": {"code": "BadRequest"}}}
        columns = {graph: column for column, graph in GRAPH_NAMES.items()}
        for graph, value in body.items():
            row[columns[graph]] = value
        self.etags[task] += 1
        self.writes += 1
        return {"status": 204, "body": None}


__all__ = ["PlannerTwin"]
