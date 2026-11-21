"""planner.task.set_due_at: a cataloged Planner write that only ever prepares. The
gated adapter builds the exact Graph call (PATCH with If-Match the etag it just read) and
sends nothing for a completed task or a task whose etag it could not read; every real
dispatch is refused while no Workbench Graph identity exists. Evidence: injected-transport
(nothing leaves the process)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from operating import action_adapters as adapters
from operating import action_catalog as ac
from operating import action_path as ap
from operating import injection_guard

CAPABILITY = "planner.task.set_due_at"
PATH = "/v1.0/planner/tasks/task-1"
DUE = "2026-12-04T00:00:00Z"


class Injected:
    evidence = adapters.EVIDENCE_LABEL

    def __init__(self, **task):
        self.task = {"id": "task-1", "percentComplete": 50, "dueDateTime": None,
                     "@odata.etag": 'W/"JzEtVGFzayAgQEBA"', **task}
        self.requests = []

    def send(self, request):
        self.requests.append((request.method, request.path, dict(request.headers),
                              request.body))
        if request.method == "PATCH":
            self.task.update(request.body)
            return {"status": 204, "body": None}
        return {"status": 200, "body": dict(self.task)}


def intent(value=DUE):
    return SimpleNamespace(
        capability=CAPABILITY, owner_system="planner", idempotency_key="k-" + value,
        checked_preconditions=(),
        expected_effect={"relation": "meetings.planner_tasks", "key": {"id": "task-1"},
                         "fields": ["due_at"], "equals": value})


class PlannerDueDateTests(unittest.TestCase):
    def test_cataloged_never_live_and_its_switch_off(self):
        catalog = ac.ActionCatalog.builtin()
        self.assertIn(CAPABILITY, catalog.proposal_ids())
        self.assertEqual(ap.REAL_CAPABILITIES[CAPABILITY].status, "not_authorized")
        self.assertIn("planner", ap.REAL_SYSTEMS)
        row = next(r for r in adapters.status({})["capabilities"]
                   if r["capability"] == CAPABILITY)
        self.assertFalse(row["allow_switch"])
        self.assertEqual(ap.RefusingDispatcher("planner").refusal(intent()),
                         "no_authorized_write_capability")

    def test_the_due_date_is_typed_by_the_planner_task_schema(self):
        cited = lambda column: injection_guard.cited_role(   # noqa: E731
            "planner-task://plan-1/task-1@g1", {"row": {}}, f"row.{column}")
        self.assertEqual((cited("due_at"), cited("title"), cited("percent_complete")),
                         ("timestamp", "text", "integer"))
        # the row envelope and column types of the MCP output firewall are unchanged
        self.assertNotIn("planner-task://", injection_guard.STRUCTURED_SOURCES)

    def test_the_exact_call_reads_then_patches_with_the_etag_just_read(self):
        injected = Injected()
        adapter = adapters.GraphPlannerAdapter(CAPABILITY, injected)
        self.assertTrue(adapter.dispatch(intent()).startswith("planner:http_204:"))
        (get, patch) = injected.requests
        self.assertEqual(get[:2], ("GET", PATH))
        self.assertEqual((patch[0], patch[1], patch[2]["If-Match"], patch[3]),
                         ("PATCH", PATH, 'W/"JzEtVGFzayAgQEBA"', {"dueDateTime": DUE}))
        self.assertEqual(adapter.read_back(intent())["outcome"], "effect_observed")
        with self.assertRaises(adapters.NotSent):   # one send per intent key
            adapter.dispatch(intent())

    def test_a_completed_task_or_an_unread_etag_sends_nothing(self):
        for task, code in (({"percentComplete": 100}, "owner_state_refused:percentComplete=100"),
                           ({"@odata.etag": None}, "owner_etag_unread")):
            with self.subTest(task=task):
                injected = Injected(**task)
                with self.assertRaises(adapters.NotSent) as caught:
                    adapters.GraphPlannerAdapter(CAPABILITY, injected).dispatch(intent())
                self.assertEqual(caught.exception.code, code)
                self.assertEqual([r[0] for r in injected.requests], ["GET"])


if __name__ == "__main__":
    unittest.main()
