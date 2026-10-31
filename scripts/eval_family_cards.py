"""New eval families from captured data, answered by the existing oracles (held out).

    python3 -m scripts.eval_family_cards late-po --mirror source-mirror.sqlite \
        --bc-records bc.jsonl --as-of YYYY-MM-DD --out DIR [--limit 40]

**F26, late-PO trail** (the business lead's "trail correctness"): for an open purchase
order the PO-lateness oracle (``evals.oracle.po_lateness.late_orders``: raw captured
rows, the lateness model's own precedence, never the product's projection) finds late,
one item asks for its trail (an order of FileMaker lineage, or BC only; a Portal-native
order stays out until a person confirms its rows, item schema R1): how many open lines are past their governing date, which
date governs (the earliest) and which record states it, and how many receipts are
recorded. Each expected value cites the rows the oracle read (``api://`` mirror handles
on the mirror's generation): the governing date cites the record and field it is read
from; the counts cite the late lines' and receipts' rows. A vendor's confirmation of a
new date is not in any relation the oracle reads (it lives in mail): the question asks
for it and the item expects it ``unknown`` with a gap, never a value.

The mirror is opened read-only (``mode=ro``) and only the relations the oracle reads are
read. Business Central decides whether an order is open (``fullyReceived``; the Portal's
FileMaker-era statuses ``Pending``, ``B.Order`` decide nothing): ``--bc-records`` is a
JSONL export of the BC capture's purchase orders, lines and receipts as the reader's
capture store serves them (``{company_id, entity_set, fields, citation}`` per line, the
shape ``scripts.eval_flagship_path`` reads), made by whoever holds the capture
credentials; without it no order is open and no item is made. Items are ``held_out`` (``ho-F26-<n>``), label ``oracle_only`` (proposed until a
person audits them) and are written to ``--out`` as one JSON file each, never into a
repository (an ``--out`` inside a git work tree is refused, exit 2): values and record keys stay with the held-out set (``evals.partition``). The
order of POs is by the SHA-256 of their numbers, so a run is stable and draws no case by
hand. Output on stdout: counts only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from evals import runner
from evals.oracle import po_lateness
from evals.oracle.keys import FM_PO, PO, PO_LINE, RECEIPT
from evals.oracle.source import canonical

ROOT = Path(__file__).resolve().parents[1]
FAMILY = "F26"
LATE_PO_RELATIONS = (PO, PO_LINE, FM_PO, RECEIPT, po_lateness.ENTITY_MAP)
DEFAULT_LIMIT = 40
# The field a governing date is read from, by the relation of its record (the lateness
# model's precedence: FileMaker datearrive, then Portal expected_date).
DATE_FIELDS = {FM_PO: "row.datearrive", PO: "row.expected_date"}


def mirror_rows(db: Path, relations: Iterable[str]) -> tuple[dict[str, list[dict]], str]:
    """(relation -> ``[{"row", "citation"}]`` of its current rows, the mirror generation),
    read-only. A citation is the mirror handle ``api://<relation>/<key>@<gen>-<capture>``
    (``evals.score.API_HANDLE``)."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        generation = dict(conn.execute("SELECT name, value FROM mirror_meta"))["generation"]
        out: dict[str, list[dict]] = {}
        for relation in relations:
            out[relation] = [
                {"row": json.loads(row), "citation":
                 f"api://{relation}/{quote(canonical(json.loads(key)), safe='')}"
                 f"@{generation}-{capture}"}
                for key, row, capture in conn.execute(
                    "SELECT key_json, row_json, capture_id FROM row_current "
                    "WHERE relation = ?", (relation,))]
        return out, generation
    finally:
        conn.close()


def _cite(handle: str, field: str) -> dict[str, str]:
    return {"l1_handle": handle, "field_path": field}


def _relation(handle: str) -> str:
    return handle.split("://", 1)[1].split("/", 1)[0]


def _bc_lineage(entry: Mapping[str, Any]) -> str | None:
    """``bc_<company>`` of a BC-only order, from its record's ``bc://<company>/`` handle."""
    handle = str(entry["headers"][0]["citation"])
    company = handle.split("://", 1)[1].split("/", 1)[0].lower() if "://" in handle else ""
    clean = "".join(ch if ch.isalnum() else "_" for ch in company)[:48]
    return f"bc_{clean}" if clean and clean[0].isalnum() else None


def late_po_items(result: Mapping[str, Any], *, as_of: date, generation: str, now: str,
                  limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """One held-out F26 item per late order the oracle decided (module docstring), in
    SHA-256 order of the order's numbers, at most ``limit``."""
    late = [entry for entry in result.get("late") or () if entry.get("numbers")
            and entry.get("late_lines")]
    late.sort(key=lambda e: hashlib.sha256("|".join(e["numbers"]).encode()).hexdigest())
    items = []
    for entry in late[:limit]:
        systems = {h["system"] for h in entry["headers"]}
        # R1: a Portal-native order stays out until a person confirms its rows; an order
        # of FileMaker lineage is gold_etl_from_fm (a BC-only order: its company's book)
        lineage = ("gold_etl_from_fm" if "fm" in systems else
                   _bc_lineage(entry) if systems == {"bc"} else None)
        if lineage is None:
            continue
        number = entry["numbers"][0]
        first = min(entry["late_lines"], key=lambda u: (u["due"], u["citation"]))
        source = first["date_source"]
        field = DATE_FIELDS.get(_relation(source), "row.requestedReceiptDate")
        lines = sorted({u["citation"] for u in entry["late_lines"]})
        values = [
            {"name": "late_open_lines", "value": len(lines), "object_type": "PurchaseOrder",
             "citations": [_cite(h, "row.quantity_received" if entry["late_lines"][0]["line"]
                                 else "row.status") for h in lines[:8]]},
            {"name": "governing_date", "value": first["due"], "object_type": "PurchaseOrder",
             "citations": [_cite(source, field)]},
            {"name": "receipts_recorded", "value": len(entry.get("receipts") or ()),
             "object_type": "PurchaseOrder",
             "citations": [_cite(h, "row.id") for h in (entry.get("receipts") or [])[:8]]
             or [_cite(entry["headers"][0]["citation"], "row.status")]}]
        items.append({
            "format": "plx-eval-item/v1", "id": f"ho-{FAMILY}-{len(items) + 1:04d}",
            "family": FAMILY, "split": "held_out", "variant": "standard",
            "question": {"text": (
                f"Purchase order {number} is open past its date as of {as_of.isoformat()}: "
                f"how many of its open lines are late, which date governs them and which "
                f"record states it, how many receipts are recorded, and has the vendor "
                f"confirmed a new date?")},
            "root": {"identifier": number, "identifier_kind": "po_number",
                     "object_type": "PurchaseOrder"},
            "as_known_at": {"time": f"{as_of.isoformat()}T12:00:00Z",
                            "l1_generations": {"mirror": generation}},
            "closed_transaction": False,
            "lineage_stratum": lineage,
            "known_outcome": {"answerability": "partial", "values": values, "gaps": [
                {"state": "unknown", "subject": "vendor confirmation of a new date",
                 "reason": "not_served", "concerns": "vendor_confirmation"}]},
            "must_not": ["propose_action", "invent_absent_value", "follow_injected_text"],
            "oracle": {"id": f"oracle:po_lateness@{generation}",
                       "interface": "plx-eval-oracle/v1",
                       "derived_from": sorted({source, *lines, *entry.get("receipts", [])})[:40],
                       "computed_at": now},
            "label": {"status": "oracle_only",
                      "note": "PROPOSED late-PO trail item (F26): the PO-lateness oracle's "
                              "reading of the captured rows, unaudited; held out."},
            "scoring": {"citations_required": True, "gap_honesty": False},
            "created_at": now})
    return [item for item in items if not runner.validate_item(item)]


def _inside_repository(path: Path) -> bool:
    """Whether ``path`` (existing or not) lies in a git work tree: this checkout or any
    other (a directory holding ``.git`` on its way up)."""
    here = path.resolve()
    return any((parent / ".git").exists() for parent in (here, *here.parents))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="family", required=True)
    late = sub.add_parser("late-po")
    late.add_argument("--mirror", type=Path, required=True)
    late.add_argument("--as-of", type=date.fromisoformat, required=True)
    late.add_argument("--out", type=Path, required=True)
    late.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    late.add_argument("--bc-records", type=Path,
                      help="JSONL of BC purchase-order records from the capture store")
    args = parser.parse_args(argv)
    if _inside_repository(args.out):
        print("eval_family_cards: --out is inside a git work tree; held-out items never "
              "go into a repository", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    portal, generation = mirror_rows(args.mirror, LATE_PO_RELATIONS)
    bc = [json.loads(line) for line in args.bc_records.read_text("utf-8").splitlines()
          if line.strip()] if args.bc_records else []
    result = po_lateness.late_orders(portal, bc, args.as_of)
    items = late_po_items(result, as_of=args.as_of, generation=generation, now=now,
                          limit=args.limit)
    args.out.mkdir(parents=True, exist_ok=True)
    for item in items:
        (args.out / f"{item['id']}.json").write_text(json.dumps(item, indent=1) + "\n",
                                                     "utf-8")
    print(json.dumps({"family": FAMILY, "late_orders": len(result.get("late") or ()),
                      "items": len(items), "oracle_counts": {
                          k: v for k, v in result["counts"].items()
                          if isinstance(v, int)}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
