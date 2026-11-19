"""Per-field accuracy of the document-field extractor on a hand-labelled set of captured
documents (AUD-07): precision, recall and fill of ``invoice_number``, ``amount``,
``po_reference`` and ``lot``, the fields the gate reads.

The labelled set is a JSON-lines file kept outside git (it names captured documents and
their printed values). One line per document::

    {"store": "mail" | "sharepoint", "l1_handle": "...", "profile": "supplier_invoice",
     "row": {"content_sha256": ..., "content_type" | "mime_type": ..., "name": ...,
             "text_status": ..., "text_sha256": ...},
     "labels": {"invoice_number": "INV-1001" | ["A", "B"] | null,
                "amount": {"digest": "<amount_digest>", "currency": "USD" | null} | null},
     "how": {"invoice_number": "how the label was read"}}

A label is the value the document prints for the field, read by a person from the
document; ``null`` when the document states none; a list when the document prints
several values that each are the field's (any one of them is right). An amount is never
written in the file: only :func:`amount_digest` of its number and the currency the
document states (``null`` when it states none, then the number alone is compared).
Only the fields a document's line labels are scored for it; a line with ``"skip": "<why>"``
(a document the labeller could not read) is left out.

Each labelled field of a document gets one outcome: ``correct`` (the extractor states a
value equal to the label, :func:`same`), ``wrong`` (it states another value, or a value
where the label says none is printed), ``missed`` (it states none where one is printed)
or ``silent`` (neither states one). Per field:

* ``precision`` = correct / (correct + wrong): of the values the extractor states, the
  share that is right; the gate is at least ``GATE_PRECISION`` per field;
* ``recall`` = correct / printed: of the values the documents print, the share read; the
  gate is also at least ``RECALL_FLOOR[field]`` (the recall the 169-document SharePoint set
  measured on 11-15, rounded down: a floor against losing what is read, not a target), and
  a field the documents print but the extractor never states fails it (recall 0);
* ``fill`` = (correct + wrong) / labelled: the share of documents the field has a value.

A document the extractor gives another class (or none) states no field of the labelled
class: its fields are ``missed`` (or ``silent``), counted as ``class_differs``.

CLI (counts and shares only, never a value)::

    python3 -m evals.document_field_gold --gold /path/labels.jsonl \
        [--mail-blobs DIR] [--sharepoint-blobs DIR]

reads each document from its capture's content-addressed blob store, read-only, through
the projection's own reader (``document_fields._documents``). Evidence label: as the
captures (``isolated-fixture`` without them); never live by itself.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable, Mapping

FORMAT = "plx-document-field-gold/v1"
FIELDS = ("invoice_number", "amount", "po_reference", "lot")
OUTCOMES = ("correct", "wrong", "missed", "silent")
GATE_PRECISION = Decimal("0.90")
# Recall floor per field (11-15): the SharePoint gold set measured
# invoice_number 0.8596, amount 0.4103, po_reference 0.75, lot 0.75 at main 9c8756a4cc.
RECALL_FLOOR = {"invoice_number": Decimal("0.85"), "amount": Decimal("0.40"),
                "po_reference": Decimal("0.75"), "lot": Decimal("0.75")}
STORES = ("mail", "sharepoint")
DEFAULT_BLOBS = {"mail": "/data/plx-state/mail-capture/blobs/sha256",
                 "sharepoint": "/data/plx-state/sharepoint-capture/blobs/sha256"}
_DIGEST_SALT = "plx-document-field-gold/amount/v1:"


def amount_digest(number: Any) -> str:
    """The digest an amount's number is labelled and compared by: SHA-256 of its plain
    Decimal form (trailing zeros dropped: '1,250.00' and '1250' are one amount, after
    the caller parsed the separators)."""
    value = Decimal(str(number)).normalize()
    return sha256((_DIGEST_SALT + format(value, "f")).encode("ascii")).hexdigest()


def _fold(value: str) -> str:
    """An identifier as compared: case, spaces, punctuation and a leading 'PO' / 'No'
    word dropped ('PO# 4500-12' and '4500-12' are one reference)."""
    text = re.sub(r"[^0-9a-z]", "", value.casefold())
    return re.sub(r"^(?:po|no|lot|batch|inv)(?=\d)", "", text)


def same(name: str, stated: Mapping[str, Any], label: Any) -> bool:
    """Whether the extractor's stated value of field ``name`` is the label."""
    if name == "amount":
        try:
            digest = amount_digest(stated.get("value"))
        except (InvalidOperation, ValueError, TypeError):
            return False
        if digest != label.get("digest"):
            return False
        return label.get("currency") is None or stated.get("currency") == label["currency"]
    labels = label if isinstance(label, list) else [label]
    value = stated.get("value")
    return isinstance(value, str) and _fold(value) in {_fold(str(x)) for x in labels} \
        and _fold(value) != ""


def outcome(name: str, field: Mapping[str, Any] | None, label: Any) -> str:
    stated = field is not None and field.get("value") is not None
    if label is None:
        return "wrong" if stated else "silent"
    if not stated:
        return "missed"
    return "correct" if same(name, field, label) else "wrong"


def _share(part: int, whole: int) -> float | None:
    return None if whole == 0 else round(part / whole, 4)


def score(documents: Iterable[Mapping[str, Any]],
          extract: Callable[[Mapping[str, Any]], Mapping[str, Any] | None]) -> dict[str, Any]:
    """Per-field outcome counts, shares and the gate over labelled ``documents``;
    ``extract``: a document's extraction result (``document_fields`` shape) or None."""
    counts = {name: {o: 0 for o in (*OUTCOMES, "labelled", "printed", "class_differs",
                                    "label_seen_unread")} for name in FIELDS}
    per_store: dict[str, dict[str, dict[str, int]]] = {}
    unread_why: dict[str, dict[str, int]] = {name: {} for name in FIELDS}
    documents = list(documents)
    for doc in documents:
        found = extract(doc)
        right_class = found is not None and found.get("status") == "classified" and \
            found.get("profile") == doc["profile"]
        fields = (found or {}).get("fields", {}) if right_class else {}
        for name, label in (doc.get("labels") or {}).items():
            if name not in FIELDS:
                raise ValueError(f"unknown labelled field {name!r}")
            field = fields.get(name)
            result = outcome(name, field, label)
            row = counts[name]
            row[result] += 1
            row["labelled"] += 1
            row["printed"] += label is not None
            row["class_differs"] += not right_class
            if field is not None and field.get("gap") == "label_seen_unread":
                row["label_seen_unread"] += 1
                why = str(field.get("unread") or "none")
                unread_why[name][why] = unread_why[name].get(why, 0) + 1
            store = per_store.setdefault(doc["store"], {}).setdefault(
                name, {o: 0 for o in OUTCOMES})
            store[result] += 1
            store["_printed"] = store.get("_printed", 0) + (label is not None)
    fields_out, passed = {}, True
    for name, row in counts.items():
        stated = row["correct"] + row["wrong"]
        precision = _share(row["correct"], stated)
        recall = _share(row["correct"], row["printed"])
        # Nothing stated is no evidence of precision: it passes only when nothing is
        # printed either (then recall has nothing to measure).
        precise = precision is not None and Decimal(str(precision)) >= GATE_PRECISION \
            if stated else row["printed"] == 0
        recalled = recall is None or Decimal(str(recall)) >= RECALL_FLOOR[name]
        ok = row["labelled"] > 0 and precise and recalled
        passed = passed and (ok or row["labelled"] == 0)
        fields_out[name] = {**row, "precision": precision, "recall": recall,
                            "fill": _share(stated, row["labelled"]),
                            "unread_why": unread_why[name], "gate": ok,
                            "gate_precision": precise, "gate_recall": recalled}
    for fields in per_store.values():
        for row in fields.values():
            row["recall"] = _share(row["correct"], row.pop("_printed", 0))
    return {"format": FORMAT, "documents": len(documents), "fields": fields_out,
            "per_store": per_store, "gate_precision": str(GATE_PRECISION),
            "recall_floor": {k: str(v) for k, v in RECALL_FLOOR.items()},
            "gate": passed and any(r["labelled"] for r in counts.values())}


def capture_extractor(blobs: Mapping[str, str | Path]
                      ) -> Callable[[Mapping[str, Any]], Mapping[str, Any] | None]:
    """A document's extraction as the projection reads it: the file itself (never a
    container member) through ``document_fields._documents`` over its store's blobs."""
    from ontology import document_fields

    def extract(doc: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if doc.get("store") not in STORES:
            raise ValueError(f"unknown store {doc.get('store')!r}")
        try:
            documents = document_fields._documents(dict(doc["row"]), blobs[doc["store"]])
            first = next(iter(documents), None)
        except Exception:  # noqa: BLE001 - an unreadable document states no field
            return None
        return None if first is None else first[1]
    return extract


def load(path: str | Path) -> list[dict[str, Any]]:
    out = []
    for number, line in enumerate(Path(path).read_text("utf-8").splitlines(), 1):
        if line.strip():
            doc = json.loads(line)
            if doc.get("skip"):   # left out by the labeller, with why: never scored
                continue
            if not isinstance(doc.get("labels"), dict) or "profile" not in doc:
                raise ValueError(f"line {number}: no labels or profile")
            out.append(doc)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m evals.document_field_gold",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--gold", required=True, help="labelled set (JSON lines, outside git)")
    parser.add_argument("--mail-blobs", default=DEFAULT_BLOBS["mail"])
    parser.add_argument("--sharepoint-blobs", default=DEFAULT_BLOBS["sharepoint"])
    args = parser.parse_args(argv)
    report = score(load(args.gold), capture_extractor(
        {"mail": args.mail_blobs, "sharepoint": args.sharepoint_blobs}))
    from ontology import document_fields
    report["extractor"] = document_fields.extractor_version()
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report["gate"] else 1


if __name__ == "__main__":
    from scripts.eval_guard import enforce
    enforce()
    sys.exit(main())
