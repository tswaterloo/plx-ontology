"""Meeting autopilot measure on 10 synthetic transcripts (``evals/meeting_followup/``:
fictional people, companies and records in Petra's identifier style; Granola markdown,
Circleback JSON and Teams WebVTT) against hand labels (``labels.json``: per meeting the
lines that are true action items, their owner and the records they concern).

* Action precision: extracted items that are a labelled item (cited line within one line
  of it, the same owner) over all extracted; recall the other way.
``heldout/`` holds 4 more meetings written after the rules were tuned on the 10 and never
tuned on (reported apart).

* Linking precision: linked records that the labelled item names over all records linked
  on matched items.

``World`` answers ``plx.find``/``plx.object``/``plx.around`` over a small fictional record
set (isolated-fixture). ``python3 -m evals.meeting_followup_eval`` prints the counts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent / "meeting_followup"


def _v(prop: str, value: str, oid: str, marks: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"property": prop, "value": {"untrusted_text": value},
            "citation": {"l1_handle": f"fixture://{oid}/{prop}"}, "markings": list(marks)}


RECORDS: dict[str, dict[str, Any]] = {
    "Customer:C1": {"name": "Northfield Bakery", "values": [_v("email", "orders@northfield.example", "Customer:C1")]},
    "Customer:C2": {"name": "Lakeshore Foods", "values": [_v("email", "purchasing@lakeshore.example", "Customer:C2")]},
    "Customer:C3": {"name": "Maple Crest Dairy", "values": []},
    "Customer:C4": {"name": "Prairie Mills", "values": []},
    "Supplier:S1": {"name": "Gulf Coast Starch", "values": [_v("rep_email", "sales@gulfcoast.example", "Supplier:S1")]},
    "Supplier:S2": {"name": "Andean Ingredients", "values": []},
    "Supplier:S3": {"name": "Prairie Pulse", "values": []},
    "SalesOrder:104511": {"ids": [("so_number", "104511")], "values": [_v("status", "Released", "SalesOrder:104511")]},
    "SalesOrder:104527": {"ids": [("so_number", "104527")], "values": [_v("status", "Shipped", "SalesOrder:104527")]},
    "SalesOrder:104602": {"ids": [("so_number", "104602")], "values": []},
    "PurchaseOrder:PO100871": {"ids": [("petra_po", "PO100871")], "values": [_v("expected_receipt_date", "2026-11-14", "PurchaseOrder:PO100871")]},
    "PurchaseOrder:PO100903": {"ids": [("petra_po", "PO100903")], "values": []},
    "CustomerPurchaseOrder:NB-55210": {"ids": [("customer_po", "NB-55210")], "values": []},
    "Item:TAP-2200": {"code": "TAP-2200", "values": [_v("on_hand_kg", "12500", "Item:TAP-2200"),
                                                    _v("unit_cost", "1.42", "Item:TAP-2200", ("finance",))]},
    "Item:PPC-80": {"code": "PPC-80", "values": [_v("on_hand_kg", "800", "Item:PPC-80")]},
    "Lot:L24-0912": {"code": "L24-0912", "values": [_v("status", "On hold", "Lot:L24-0912")]},
}


class World:
    """``call(tool, arguments)`` over ``RECORDS``; every call is kept in ``calls``."""

    def __init__(self, records: Mapping[str, Mapping[str, Any]] = RECORDS,
                 edges: list[dict[str, Any]] | None = None) -> None:
        self.records, self.edges, self.calls = records, list(edges or ()), []

    def _obj(self, oid: str) -> dict[str, Any]:
        rec = self.records[oid]
        label = rec.get("name") or rec.get("code") or oid.split(":", 1)[1]
        return {"object_id": oid, "object_type": oid.split(":")[0],
                "display": {"untrusted_text": label,
                            "citation": {"l1_handle": f"fixture://{oid}"}}, "lineage": {}}

    def __call__(self, tool: str, args: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((tool, dict(args)))
        if tool == "plx.find":
            q, kind = str(args.get("query") or "").lower(), args.get("identifier_kind")
            hits = []
            for oid, rec in self.records.items():
                if kind:
                    if any(k == kind and v.lower() == q for k, v in rec.get("ids", ())):
                        hits.append(oid)
                    continue
                words = set((rec.get("name") or "").lower().split())
                if (rec.get("code") or "").lower() == q or oid.split(":", 1)[1].lower() == q \
                        or (words and set(q.split()) <= words):
                    hits.append(oid)
            return {"status": "found" if len(hits) == 1 else "ambiguous" if hits else
                    "not_found", "candidates": [{"object": self._obj(o)} for o in hits]}
        if tool == "plx.object":
            oid = args.get("object_id")
            if oid not in self.records:
                return {"error": {"code": "not_found"}}
            return {"object": self._obj(oid), "values": list(self.records[oid]["values"])}
        if tool == "plx.around":
            oid, link = args.get("from"), args["path"][0]["link_type"]
            return {"edges": [e for e in self.edges if e["link_type"] == link
                              and oid in (e["from"], e["to"])]}
        return {"error": {"code": "unknown_tool"}}


class Guard:
    """A people exclusion that decides every address of the fixture domain."""

    unavailable = None

    def __init__(self, excluded: set[str] = frozenset(), domain: str = "plx.example") -> None:
        self.excluded, self.domain = {e.lower() for e in excluded}, domain

    def decides(self, value: str) -> bool:
        return value.lower().endswith("@" + self.domain)

    def is_excluded(self, value: str) -> bool:
        return value.lower() in self.excluded


def transcripts(directory: Path = HERE) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.is_file() and p.name != "labels.json")


def measure(directory: Path = HERE) -> dict[str, Any]:
    import tempfile
    from operating import meeting_followup as mf
    labels = json.loads((directory / "labels.json").read_text())
    out = {"gold_records": 0, "meetings": 0, "extracted": 0, "correct": 0, "gold": 0, "found": 0,
           "linked": 0, "linked_correct": 0, "misses": [], "wrong": [], "wrong_links": []}
    with tempfile.TemporaryDirectory() as tmp:
        store = mf.FollowUps(tmp, Guard())
        for path in transcripts(directory):
            mid = path.name.split(".")[0]
            src = "circleback" if "circleback" in path.name else "granola" \
                if "granola" in path.name else "teams_vtt"
            raw = json.loads(path.read_text()) if path.suffix == ".json" else path.read_text()
            result = store.ingest(raw, Guard(), World(), source=src)
            assert result["status"] == "kept", (path.name, result)
            record = store.get(result["meeting_id"])
            out["meetings"] += 1
            gold = labels[mid]["actions"]
            out["gold"] += len(gold)
            used = set()
            for a in record["actions"]:
                out["extracted"] += 1
                line = _line(a["citations"][0])
                owner = (a.get("owner") or {}).get("address")
                match = next((g for g in gold if abs(g["line"] - line) <= 1
                              and g["owner"] == owner and id(g) not in used), None)
                if match is None:
                    out["wrong"].append(f"{mid}:{line}:{owner}:{a['text'][:60]}")
                    continue
                used.add(id(match))
                out["gold_records"] += len(match["records"])
                out["correct"] += 1
                for lk in a.get("records") or ():
                    if lk["status"] != "linked":
                        continue
                    out["linked"] += 1
                    if lk["object_id"] in match["records"]:
                        out["linked_correct"] += 1
                    else:
                        out["wrong_links"].append(f"{mid}:{line}:{lk['object_id']}")
            out["found"] += len(used)
            out["misses"] += [f"{mid}:{g['line']}" for g in gold if id(g) not in used]
    out["action_precision"] = round(out["correct"] / max(1, out["extracted"]), 3)
    out["action_recall"] = round(out["found"] / max(1, out["gold"]), 3)
    out["link_precision"] = round(out["linked_correct"] / max(1, out["linked"]), 3)
    out["link_recall"] = round(out["linked_correct"] / max(1, out["gold_records"]), 3)
    out["evidence_label"] = "isolated-fixture"
    return out


def _line(citation: str) -> int:
    """The fixture's line from its clock (20 s + 37 s per line) or its line anchor."""
    anchor = citation.rsplit("#", 1)[1]
    if anchor.startswith("line="):
        return int(anchor[5:])
    h, m, s = (int(x) for x in anchor[2:].split(":"))
    return round(((h * 3600 + m * 60 + s) - 20) / 37)


if __name__ == "__main__":
    print(json.dumps({"tuned_set": measure(), "held_out_set": measure(HERE / "heldout")},
                     indent=1))
