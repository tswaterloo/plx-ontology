"""The Mission Control twin: MC's MCP tools as the generator pins them, with a role roster.

``McTwin`` is the in-process MC of ``operating.goal2_exec`` (``IsolatedMissionControl``:
tasks, approval gates, ``mc_create_task`` with its idempotency claims, the human decide
route) behind the tool surface the generator pinned from PLX_MC
(``ontology/schema/generated/meta/mcp_tools.json``, ``ontology.generator.mcp``): a tool
the pin does not list is ``tool_not_found``; a pinned tool's call with an input the pin
does not declare, or without a required one, is ``invalid_input`` (zod's refusal); a pinned
tool the twin does not emulate is ``not_emulated``. Nothing is answered by default.

Its users are MC's ``mc_users`` with role and Entra oid; ``add_user`` adds someone to MC
after a person wrote the approver roster (``operating.goal2_mc_approval.load_approver_roster``
reads it), as happens in MC. The real ``MissionControlMcpClient`` reaches it through
an injected in-process sender only (``client``). Evidence label: ``isolated-fixture``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from operating.goal2_exec import Clock, IsolatedMissionControl

ROOT = Path(__file__).resolve().parents[1]
PIN_PATH = ROOT / "ontology" / "schema" / "generated" / "meta" / "mcp_tools.json"
SERVER = "plx-mc"
EMULATED = frozenset({"mc_self_check", "mc_get_task", "mc_search_tasks", "mc_request_approval",
                      "mc_create_task"})


class McTwin(IsolatedMissionControl):
    """``users``: email -> ``{"role": member|admin|owner, "oid": Entra object id}``."""

    def __init__(self, clock: Clock, users: Mapping[str, Mapping[str, Any]],
                 pin_path: Path = PIN_PATH) -> None:
        super().__init__(clock, {email: user["role"] for email, user in users.items()})
        tools = json.loads(pin_path.read_text(encoding="utf-8"))["servers"][SERVER]["tools"]
        self.inputs = {name: spec.get("input") for name, spec in tools.items()}
        if not EMULATED <= set(self.inputs):
            raise ValueError("emulated_tool_not_pinned")

    def add_user(self, email: str, role: str) -> None:
        self.users[email.lower()] = role

    def created_tasks(self, prefix: str) -> list[str]:
        """Ids of the tasks MC created whose title starts with ``prefix``."""
        return sorted(task_id for task_id, task in self.tasks.items()
                      if task.get("reporter") and str(task["title"]).startswith(prefix))

    def _answer(self, method: str, url: str, headers: Mapping[str, str],
                body: bytes | None) -> tuple[int, dict[str, str], bytes]:
        refusal = None
        if body is not None and not self.down:
            request = json.loads(body)
            name = request.get("params", {}).get("name")
            arguments = request.get("params", {}).get("arguments") or {}
            declared = self.inputs.get(name)
            if name not in self.inputs:
                refusal = "tool_not_found"
            elif declared is None or name not in EMULATED:
                refusal = "not_emulated"
            elif (set(arguments) - set(declared)
                  or any(spec["required"] and param not in arguments
                         for param, spec in declared.items())):
                refusal = "invalid_input"
            if refusal is not None:
                self.tool_calls.append(str(name))
                self.refusals.append(refusal)
                payload = {"error": {"code": refusal, "message": "refused by the MC twin"}}
                result = {"content": [{"type": "text", "text": json.dumps(payload)}],
                          "isError": True}
                answer = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
                return 200, {"content-type": "application/json"}, json.dumps(answer).encode()
        return super()._answer(method, url, headers, body)


__all__ = ["EMULATED", "McTwin"]
