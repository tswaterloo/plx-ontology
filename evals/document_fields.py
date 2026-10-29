"""Runtime scoring of document-extraction eval items against the deterministic reader (P2/P8).

The exposed extraction items (``evals/exposed``: the P2/P8 bundle, imported from
``EVIDENCE/evals/dot-p2p8-20261004``) expect field values a curator read on one pinned
original (``as_known_at.l1_generations.curated_original_bytes``: the byte SHA-256). This
module scores what the Workbench's own reader extracts from those same bytes
(``ontology.document_fields.extract_fields``, the projection's extractor; a scan is read
from its capture's OCR text): no agent and no model, so the score measures the reader.

Each expected value of object type ``SourceDocument`` (a value printed on the document)
gets one outcome:

* ``correct``: the reader states the expected value (Decimal equality for money and
  quantities, with the same currency or unit when the item names one; a ``null`` expected
  value is correct when the reader states none);
* ``wrong``: the reader states another value, currency or unit, or a value where the
  item expects none;
* ``abstain``: the reader states no value where one is expected (an extraction gap, a
  field no profile of the class reads: ``no_profile_field``, or a document of no single
  class);
* ``blocked``: the pinned original is not available to this run (``original_unavailable``).

An item field binds to a profile field (``ontology/schema/document_fields.json``) by name
or by the stems below, with a part suffix: ``_iso_date`` (the value), ``_raw`` (the cited
literal), ``_term`` (a code) and ``_named_place`` (the place after it). The report holds
outcomes, reasons and ``read_by`` labels, never a value.

CLI (counts per item and per field; no values)::

    python3 -m evals.document_fields --items evals/exposed --state-dir STATE

reads the originals from the SharePoint capture store of ``STATE``
(``integrations/capture_sharepoint.py``); without ``--state-dir`` every item is blocked.
Evidence label: as the capture (``isolated-fixture`` without one); never live by itself.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any, Callable, Iterable, Mapping

from integrations.captured_text import TEXT_OCR
from ontology import document_fields
from ontology.units import unit_token

FORMAT = "plx-document-fields-eval/v1"
OUTCOMES = ("correct", "wrong", "abstain", "blocked")
PIN = "curated_original_bytes"
OBJECT_TYPE = "SourceDocument"
PARTS = ("iso_date", "raw", "term", "named_place")
# Item field stems that name a profile field under another word.
STEMS = {"printed_date": "document_date", "quote_date": "document_date",
         "total_amount": "amount", "quoted_amount": "amount",
         "filling_row_quantity": "filled_quantity", "shop_floor_samples": "retained_samples",
         "rejects": "rejected_quantity"}


@dataclass(frozen=True)
class Original:
    """The pinned bytes of one original and, for a scan, its capture's OCR text."""

    content: bytes
    mime_type: str
    name: str = ""
    text: str | None = None


def pinned(item: Mapping[str, Any]) -> str | None:
    """The byte SHA-256 of the original an extraction item was curated from."""
    return ((item.get("as_known_at") or {}).get("l1_generations") or {}).get(PIN)


def _expected(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [v for v in item["known_outcome"]["values"] if v.get("object_type") == OBJECT_TYPE]


def _binding(name: str, fields: Mapping[str, Any]) -> tuple[str | None, str]:
    stem, part = name, "value"
    for suffix in PARTS:
        if name.endswith("_" + suffix):
            stem, part = name[:-len(suffix) - 1], suffix
            break
    field = STEMS.get(stem, stem)
    return (field if field in fields else None), part


def _text(value: Any) -> str:
    return " ".join(str(value).split()).casefold()


def _outcome(expected: Mapping[str, Any], item: Mapping[str, Any], part: str
             ) -> tuple[str, str | None]:
    want = expected["value"]
    if part == "raw":
        literals = [_text(c["text"]) for c in item.get("citations") or ()]
        if not literals:
            return ("correct", None) if want is None else ("abstain", "not_stated")
        return ("correct", None) if want is not None and _text(want) in literals else \
            ("wrong", "literal_differs")
    got = item.get("place") if part == "named_place" else item.get("value")
    if want is None:   # only a field read as absent matches; any other gap abstains
        if got is not None:
            return "wrong", "value_where_absent"
        gap = item.get("gap")
        return ("correct", None) if gap in (None, "value_missing", "not_stated") else \
            ("abstain", gap)
    if got is None:
        return "abstain", item.get("gap") or "not_stated"
    if item.get("kind") in ("money", "quantity"):
        try:
            same = Decimal(str(got)) == Decimal(str(want))
        except (InvalidOperation, ValueError):
            same = False
        if not same:
            return "wrong", "value_differs"
        if expected.get("currency") and expected["currency"] != item.get("currency"):
            return "wrong", "currency_differs"
        if expected.get("unit") and item.get("unit") and \
                unit_token(expected["unit"]) != unit_token(item["unit"]):
            return "wrong", "unit_differs"
        return "correct", None
    return ("correct", None) if _text(got) == _text(want) else ("wrong", "value_differs")


def score_item(item: Mapping[str, Any], extraction: Mapping[str, Any] | None
               ) -> dict[str, Any]:
    """Per-field outcomes of one item against the reader's ``extraction`` of its pinned
    original (None: the original is not available)."""
    status = None if extraction is None else extraction["status"]
    fields = (extraction or {}).get("fields") or {}
    rows = []
    for expected in _expected(item):
        row: dict[str, Any] = {"name": expected["name"]}
        if extraction is None:
            row.update(outcome="blocked", reason="original_unavailable")
        elif status != "classified":
            row.update(outcome="abstain", reason=status)
        else:
            field, part = _binding(expected["name"], fields)
            if field is None:
                row.update(outcome="abstain", reason="no_profile_field")
            else:
                outcome, reason = _outcome(expected, fields[field], part)
                row.update(outcome=outcome, field=field)
                if reason is not None:
                    row["reason"] = reason
                if fields[field].get("read_by"):
                    row["read_by"] = fields[field]["read_by"]
        rows.append(row)
    return {"id": item["id"], "status": status or "blocked",
            "profile": (extraction or {}).get("profile"), "fields": rows,
            "counts": _counts(rows)}


def _counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(OUTCOMES, 0)
    for row in rows:
        counts[row["outcome"]] += 1
    return counts


def score_items(items: Iterable[Mapping[str, Any]],
                original: Callable[[str], Original | None]) -> dict[str, Any]:
    """Score every extraction item (a pinned original and printed values) by re-reading
    its original through ``original`` (``digest -> Original | None``)."""
    scored = []
    for item in items:
        digest = pinned(item)
        if digest is None or not _expected(item):
            continue
        found = original(digest)
        extraction = None
        if found is not None and sha256(found.content).hexdigest() == digest:
            try:
                extraction = document_fields.extract_fields(
                    found.content, found.mime_type, filename=found.name, text=found.text)
            except Exception:  # noqa: BLE001 - unreadable bytes: the reader states nothing
                extraction = {"status": "unreadable"}
        scored.append(score_item(item, extraction))
    return {"format": FORMAT, "extractor": document_fields.extractor_version(),
            "items": scored, "counts": _counts(r for s in scored for r in s["fields"])}


def sharepoint_originals(store: Any) -> Callable[[str], Original | None]:
    """``digest -> Original`` over a SharePoint capture store (its latest version of those
    bytes, with the pinned OCR text of a scan)."""
    def find(digest: str) -> Original | None:
        row = store.db.execute(
            "SELECT record, text_status, text_sha256 FROM items WHERE content_sha256 = ? "
            "ORDER BY version_id DESC LIMIT 1", (digest,)).fetchone()
        if row is None:
            return None
        record = json.loads(row["record"])
        text = None
        if row["text_status"] == TEXT_OCR and row["text_sha256"]:
            text = store.blob(row["text_sha256"]).decode("utf-8")
        return Original(store.blob(digest), record.get("mime_type") or
                        "application/octet-stream", str(record.get("name") or ""), text)
    return find


def main(argv: list[str] | None = None) -> int:
    from evals.runner import load_items
    parser = argparse.ArgumentParser(prog="python3 -m evals.document_fields",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--items", default="evals/exposed", help="dev or exposed item directory")
    parser.add_argument("--state-dir", help="Workbench state with a SharePoint capture")
    args = parser.parse_args(argv)
    items = load_items(Path(args.items), split="dev")
    if args.state_dir is None:
        report = score_items(items, lambda digest: None)
    else:
        from integrations.capture_sharepoint import SharePointCaptureStore
        store = SharePointCaptureStore.open_read_only(Path(args.state_dir).expanduser())
        try:
            report = score_items(items, sharepoint_originals(store))
        finally:
            store.close()
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
