"""Synthetic eval items, rows and a fake case host (evidence label: isolated-fixture).

Every identifier and value here is invented; none is a real PLX transaction."""

from __future__ import annotations

import copy
import json
from typing import Any
from urllib.parse import quote

GEN = "a1b2c3d4e5f60718"
OTHER_GEN = "ffffeeeeddddcccc"


def handle(relation: str, key: dict[str, Any], capture: int, generation: str = GEN) -> str:
    text = json.dumps(key, sort_keys=True, separators=(",", ":"))
    return f"api://{relation}/{quote(text, safe='')}@{generation}-{capture}"


H_LINE = handle("public.order_entry_item", {"id": "oi-t1"}, 7)       # qty: ordered
H_LINE_B = handle("public.order_entry_item", {"id": "oi-t2"}, 7)     # same qty, other row
H_MO = handle("public.app_manufacturing_order", {"id": "mo-t1"}, 8)  # quantity: planned
H_SO = handle("public.app_sales_order", {"id": "so-t1"}, 9)          # no role contract
H_LINE_OLD = handle("public.order_entry_item", {"id": "oi-t1"}, 3, OTHER_GEN)

ROWS = {
    H_LINE: ("public.order_entry_item", {"id": "oi-t1"}, {"id": "oi-t1", "qty": "2400"}),
    H_LINE_B: ("public.order_entry_item", {"id": "oi-t2"}, {"id": "oi-t2", "qty": "2400"}),
    H_MO: ("public.app_manufacturing_order", {"id": "mo-t1"},
           {"id": "mo-t1", "quantity": "2350", "qty_damaged": "17"}),
    H_SO: ("public.app_sales_order", {"id": "so-t1"},
           {"id": "so-t1", "order_number": "SO-TEST-77", "total_amount": "4410.50"}),
    H_LINE_OLD: ("public.order_entry_item", {"id": "oi-t1"}, {"id": "oi-t1", "qty": "2400"}),
}


def item(**changes: Any) -> dict[str, Any]:
    base = {
        "format": "plx-eval-item/v1",
        "id": "dev-F3-0901",
        "family": "F3",
        "split": "dev",
        "variant": "standard",
        "question": {"text": "For sales order SO-TEST-77, how many units were ordered, "
                             "planned and shipped?",
                     "paraphrases": ["What were the ordered, planned and shipped "
                                     "quantities on SO-TEST-77?"]},
        "root": {"identifier": "SO-TEST-77", "identifier_kind": "so_number",
                 "object_type": "SalesOrder", "relation": "public.app_sales_order",
                 "key": {"id": "so-t1"}},
        "as_known_at": {"time": "2099-01-02T03:04:05Z",
                        "l1_generations": {"portal_api": GEN}},
        "closed_transaction": True,
        "lineage_stratum": "gold_etl_from_fm",
        "known_outcome": {
            "answerability": "partial",
            "values": [
                {"name": "ordered", "object_type": "SalesOrderLine", "property": "qty",
                 "value": "2400", "quantity_role": "ordered",
                 "citations": [{"l1_handle": H_LINE, "field_path": "row.qty"}]},
                {"name": "planned", "value": "2350", "quantity_role": "planned",
                 "citations": [{"l1_handle": H_MO, "field_path": "row.quantity"}]},
                {"name": "shipped", "value": None, "quantity_role": "shipped"},
            ],
            "gaps": [{"state": "not_ingested", "subject": "Shipment", "reason": "not_served"}],
        },
        "must_not": ["claim_uncited_number", "invent_absent_value"],
        "oracle": {"id": "oracle:f3_quantities@0123456789ab", "interface": "plx-eval-oracle/v1",
                   "derived_from": ["devloop/CONTEXT/03-PORTAL.md#fixture"],
                   "computed_at": "2099-01-02T03:05:00Z"},
        "label": {"status": "oracle_only"},
        "created_at": "2099-01-02T03:05:00Z",
    }
    base.update(copy.deepcopy(changes))
    return base


def good_answer(**changes: Any) -> dict[str, Any]:
    answer = {
        "format": "plx-eval-answer/v1", "answerability": "partial",
        "text": "Ordered 2400 units and planned 2350; shipment is not ingested.",
        "values": [
            {"name": "ordered_qty", "value": "2400", "quantity_role": "ordered",
             "citations": [{"l1_handle": H_LINE, "field_path": "row.qty"}]},
            {"name": "planned_qty", "value": "2350", "quantity_role": "planned",
             "citations": [{"l1_handle": H_MO, "field_path": "row.quantity"}]},
            {"name": "shipped", "value": None, "quantity_role": "shipped"},
        ],
        "gaps": [{"state": "not_ingested", "subject": "Shipment", "reason": "not_served"}],
    }
    answer.update(copy.deepcopy(changes))
    return answer


def final_text(answer: dict[str, Any]) -> str:
    return "Here is the answer.\n```json\n" + json.dumps(answer) + "\n```\n"


class FakeHost:
    """The case host's shape over ``ROWS``: read tools plus one proposal tool."""

    def __init__(self, rows: dict[str, Any] | None = None,
                 principals: tuple[str, ...] = ("staff:operations",)) -> None:
        self.rows = ROWS if rows is None else rows
        self.actor = {"tenant": "plx", "principals": sorted(principals)}
        self.calls: list[str] = []

    def tools(self) -> list[dict[str, Any]]:
        return [{"name": "plx.find", "read_only": True},
                {"name": "plx.source_citation", "read_only": True},
                {"name": "plx.case_propose", "read_only": False}]

    def read(self, name: str, arguments: dict[str, Any]) -> Any:
        return self.call(name, arguments)  # results are bare already: no output boundary

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append(name)
        if name == "plx.source_citation":
            found = self.rows.get(arguments["citation"])
            if found is None:
                return {"ok": False, "error": "citation_unresolvable"}
            relation, key, row = found
            return {"ok": True, "status": "current", "relation": relation, "key": key,
                    "row": copy.deepcopy(row), "capture": {"capture_id": 1}}
        if name == "plx.find":
            relation = arguments["relation"]
            return {"returned": 1, "rows": [{"citation": h, "row": copy.deepcopy(r)}
                                            for h, (rel, _k, r) in self.rows.items()
                                            if rel == relation]}
        if name == "plx.case_propose":
            return {"proposal_id": "P1"}
        return {"ok": False, "error": "tool_refused"}

    def mcp_server(self) -> dict[str, Any]:
        return {"command": "python3", "args": ["-m", "operating.mcp_server"]}


def scripted(answer: dict[str, Any], *, propose: bool = False):
    """A script that reads the rows it cites, optionally proposes, then answers."""
    def script(prompt: str, call: Any) -> str:
        call("plx.find", {"relation": "public.order_entry_item"})
        call("plx.find", {"relation": "public.app_manufacturing_order"})
        if propose:
            call("plx.case_propose", {"case_id": "C1", "action": "follow_up"})
        return final_text(answer)
    return script
