#!/usr/bin/env python3
"""Generate app/src/lib/record-kinds.json: how the record page groups each type's facts.

Everything comes from the ontology schema (ontology/schema/domains/*.json): the family of
a type (event, task, thread, finance) from its domain and its finance markings, the role of
each property from its value type, markings and name, and the role of every type (party,
document, finance, ...) from its domain. The page reads this file by type; no record is
named anywhere.  `python3 tools/gen_record_kinds.py --check` fails when the file is stale.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOMAINS = ROOT / "ontology" / "schema" / "domains"
OUT = ROOT / "app" / "src" / "lib" / "record-kinds.json"

DOMAIN_ROLE = {
    "catalog_parties": "party", "people": "person", "bc_finance": "finance",
    "orders": "document", "purchasing": "document", "sales_documents": "document",
    "quotes": "document", "requisitions": "document", "fulfillment": "document",
    "manufacturing": "document", "production": "document", "documents": "document",
    "meetings": "event", "mission_control": "task", "teams_conversations": "thread",
}
PERSON_NAME = re.compile(r"(?:^|_)(?:assignees?|attendees|organizer|participants|speakers|reporter|"
                         r"owner|accountable_owner|requested_by|decided_by|created_by|completed_by|actor)$")
STATUS_NAME = re.compile(r"^(?:status|stage|percent_complete|priority|health|bucket|bucket_name|retired_in_mc|"
                         r"is_cancelled|blocked|kind|chat_type|membership_type|type)$")
TEXT_NAME = re.compile(r"^(?:body_preview|description|content|note|reason|snippet|checklist|location|topic)$")
AGING_NAME = re.compile(r"^(?:current_amount|period\d+_amount|aged_as_of_date|period_length|due_date|remaining_amount)$")
ACCOUNT_NAME = re.compile(r"(?:^|_)(?:account_number|document_type|document_number|journal|category|sub_category)$")
PARTY_NAME = re.compile(r"(?:^|_)(?:party_number|customer_number|vendor_number)$")


def prop_role(name: str, value_type: str, marked: bool) -> str:
    if value_type == "money" or (marked and re.search(r"amount|balance|total|subtotal|tax|discount", name)):
        return "money"
    if AGING_NAME.match(name):
        return "aging"
    if PERSON_NAME.search(name):
        return "person"
    if PARTY_NAME.search(name):
        return "party"
    if ACCOUNT_NAME.search(name):
        return "account"
    if STATUS_NAME.match(name):
        return "status"
    if TEXT_NAME.match(name):
        return "text"
    if value_type in ("timestamp", "date"):
        return "when"
    return "other"


def family_of(domain: str, type_id: str, props: list[dict], markings: list[str]) -> str | None:
    if type_id == "PlannerTask":
        return "task"
    if domain == "bc_finance":
        if type_id.startswith("ItemLedger"):   # stock movements, not money records
            return None
        return "finance" if ("finance" in markings or any(p.get("markings") for p in props)) else None
    if domain in ("meetings",):
        return "event"
    if domain == "mission_control":
        return "task" if type_id != "KnowledgeEntry" else None
    if domain == "teams_conversations":
        return "thread"
    # a finance document of the other domains: money fields marked finance, and not an order or a party
    money = any(p.get("value_type") == "money" and p.get("markings") for p in props)
    if money and re.search(r"(?:Invoice|Memo|Payment)$", type_id):
        return "finance"
    return None


def build() -> dict:
    types: dict[str, dict] = {}
    for path in sorted(DOMAINS.glob("*.json")):
        d = json.loads(path.read_text())
        stem = path.stem
        for t in d.get("object_types", []):
            props = t.get("properties", [])
            markings = list(t.get("markings") or [])
            fam = family_of(stem, t["id"], props, markings)
            entry: dict = {"role": "person" if t["id"].startswith("Contact") else DOMAIN_ROLE.get(stem, "record")}
            if fam:
                entry["family"] = fam
                entry["props"] = {
                    p["name"]: {"role": prop_role(p["name"], p.get("value_type", ""), bool(p.get("markings") or markings)),
                                **({"marked": sorted(p.get("markings") or markings)} if (p.get("markings") or markings) else {})}
                    for p in props
                }
            types[t["id"]] = entry
    return {"generated_from": "ontology/schema/domains", "types": dict(sorted(types.items()))}


def main() -> int:
    text = json.dumps(build(), indent=1, sort_keys=False) + "\n"
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != text:
            print("record-kinds.json is stale; run tools/gen_record_kinds.py")
            return 1
        return 0
    OUT.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
