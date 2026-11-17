"""A BC field page row (Page 50 reference, Page 38 lot) is stored without an ``id``; the
projector adds the record path's id, and the citation reader resolves ``row.id`` the same
way through one shared helper (``ontology.project_bc.field_page_row``).

Evidence label: ``isolated-fixture`` (a stubbed capture read; no source is read)."""

from __future__ import annotations

import unittest

from ontology.project_bc import cited_row, field_page_row
from operating.case_runtime import CaseRuntime

COMPANY = "c0000000-0000-4000-8000-0000000000aa"
RECORD = "b0000000-0000-4000-8000-000000000001"
GEN = "0123456789abcdef"


def handle(entity_set: str) -> str:
    return f"bc://production/{COMPANY}/{entity_set}/{RECORD}@{GEN}-7"


class FieldPageRowTests(unittest.TestCase):
    def test_field_page_row_gets_the_path_id_and_the_stored_row_is_unchanged(self):
        stored = {"No": "PO1", "Your_Reference": "X"}
        shown = field_page_row("purchaseOrderReferences", stored, RECORD)
        self.assertEqual(shown, {**stored, "id": RECORD})
        self.assertNotIn("id", stored)
        self.assertEqual(cited_row(handle("itemLedgerEntryLots"), stored)["id"], RECORD)

    def test_other_rows_and_own_ids_are_returned_as_captured(self):
        plain = {"number": "PO1"}
        self.assertIs(field_page_row("purchaseOrders", plain, RECORD), plain)
        own = {"id": "own"}
        self.assertIs(field_page_row("purchaseOrderReferences", own, RECORD), own)
        self.assertIs(cited_row(handle("purchaseOrders"), plain), plain)
        for bad in ("", "not a handle", "bc://x", None):
            self.assertIs(cited_row(bad, plain), plain)   # fails closed: nothing added

    def test_the_citation_reader_resolves_row_id_for_a_field_page(self):
        found = {"citation": handle("purchaseOrderReferences"), "kind": "bc",
                 "key": f"{COMPANY}/{RECORD}", "row": {"No": "PO1"}, "lineage": "l",
                 "source_version": "v", "digest": "d", "status": "current",
                 "changed_since": False, "capture": {"recorded_at": "t", "superseded_at": None}}
        runtime = object.__new__(CaseRuntime)
        runtime._capture_resolve = lambda h, actor, opening=False: found
        view = runtime._from_capture(found["citation"], None)
        self.assertEqual(view["row"], {"No": "PO1", "id": RECORD})
        self.assertNotIn("id", found["row"])


if __name__ == "__main__":
    unittest.main()
