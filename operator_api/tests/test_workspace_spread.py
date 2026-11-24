"""A record's walk read side by side (``object_workspace.WORKSPACE_SPREAD``) is the walk
read one call after another: the same workspace, every hop's timeline read on the runtime
that holds the hop's set, and a spread cut short is no page at all."""

from __future__ import annotations

import threading
import unittest

from ontology.types import Actor
from operator_api.workspace import object_workspace as ow

RECORD = "PurchaseOrder:portal.app_purchase_order:{\"id\":\"p1\"}"
LINKS = ("PurchaseOrder.supplier", "PurchaseOrder.lines", "Email.mentions_po")
HERE = threading.local()   # which runtime a call is made on (a worker's own)


def runtime() -> str:
    return getattr(HERE, "name", "main")


def tools() -> dict:
    held: dict[str, str] = {}   # set handle -> the runtime that made it

    def card(arguments, actor):
        return {"object": {"object_id": arguments["object_id"], "object_type": "PurchaseOrder"},
                "linked_counts": [{"link_type": lt, "direction": "forward", "count": 2}
                                  for lt in LINKS] + [{"link_type": "Empty.none",
                                                       "direction": "reverse", "count": 0}]}

    def around(arguments, actor):
        lt = arguments["path"][0]["link_type"]
        handle = f"set:{lt}:{len(held)}"
        held[handle] = runtime()
        return {"set": handle, "objects": [{"object_id": f"{lt}:{i}", "object_type": "X"}
                                           for i in range(2)]}

    def timeline(arguments, actor):
        root = arguments["from"]
        if root.startswith("set:"):
            if held.get(root) != runtime():
                return {"error": {"code": "set_not_found"}}   # another runtime's set
            lt = root.split(":")[1]
            return {"events": [{"event_id": f"e-{lt}-{i}", "at": f"2026-01-0{i + 1}"}
                               for i in range(2)]}
        return {"events": [{"event_id": "e-own", "at": "2026-01-05"},
                           {"event_id": f"e-{LINKS[0]}-0", "at": "2026-01-01"}]}

    def documents(arguments, actor):
        index = int(arguments.get("cursor") or 0)
        return {"documents": [{"title": f"D{index}", "citation": f"api://public.app_x/{index}"}],
                "completeness": {"next_cursor": str(index + 1) if index < 2 else None}}

    table = {name: {"handler": lambda a, actor: {}} for name in ow.TOOLS}
    table.update({"plx.object": {"handler": card}, "plx.around": {"handler": around},
                  "plx.timeline": {"handler": timeline},
                  "plx.process": {"handler": lambda a, actor: {"error": {"code": "no_process"}}},
                  ow.DOCUMENTS_TOOL: {"handler": documents}})
    return table


def workspace() -> ow.ObjectWorkspace:
    return ow.ObjectWorkspace(tools(), Actor("plx", frozenset({"u:reader"})))


def side_by_side(call, tasks, late):
    """Each task on its own thread and runtime, as the pool's workers read them."""
    out: list = [None] * len(tasks)

    def run(i):
        HERE.name = f"worker-{i}"
        out[i] = tasks[i](call)
    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(tasks))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def strip(page: dict) -> dict:
    """The page without what names the run (set handles differ per read)."""
    page = dict(page)
    page["around"] = {**page["around"], "hops": [
        {**h, "result": {**h["result"], "set": None}} for h in page["around"]["hops"]]}
    return page


class WorkspaceSpreadTest(unittest.TestCase):
    def test_spread_reads_the_same_page_as_serial(self) -> None:
        serial = workspace().workspace(RECORD)
        token = ow.WORKSPACE_SPREAD.set(side_by_side)
        try:
            spread = workspace().workspace(RECORD)
        finally:
            ow.WORKSPACE_SPREAD.reset(token)
        self.assertEqual(strip(spread), strip(serial))
        # every hop's timeline was read where its set is held: no root failed
        calls = spread["timeline"]["calls"]
        self.assertEqual(len(calls), 1 + len(LINKS))
        self.assertFalse([c for c in calls if c["result"].get("error")])
        ids = [e["event_id"] for e in spread["timeline"]["events"]]
        self.assertEqual(len(ids), len(set(ids)))   # merged by event id, none twice
        self.assertEqual(len(ids), 1 + 2 * len(LINKS))
        self.assertEqual([h["link_type"] for h in spread["around"]["hops"]], list(LINKS))
        self.assertTrue(spread["documents"]["withheld"]["complete"])

    def test_a_workspace_without_documents_never_calls_the_documents_tool(self) -> None:
        tools_ = tools()
        called = []
        inner = tools_[ow.DOCUMENTS_TOOL]["handler"]
        tools_[ow.DOCUMENTS_TOOL] = {"handler": lambda a, actor: called.append(a) or inner(a, actor)}
        view = ow.ObjectWorkspace(tools_, Actor("plx", frozenset({"u:reader"})))
        plain = view.workspace(RECORD, documents=False)
        self.assertEqual(called, [])
        self.assertEqual(plain["documents"]["status"], "not_configured")  # said, never an empty list
        self.assertEqual(plain["conflicts"], view.workspace(RECORD)["conflicts"])
        self.assertTrue(called)

    def test_a_spread_cut_short_is_no_page(self) -> None:
        token = ow.WORKSPACE_SPREAD.set(lambda call, tasks, late: [t(call) for t in tasks[:2]])
        try:
            with self.assertRaises(TimeoutError):
                workspace().workspace(RECORD)
        finally:
            ow.WORKSPACE_SPREAD.reset(token)


if __name__ == "__main__":
    unittest.main()
