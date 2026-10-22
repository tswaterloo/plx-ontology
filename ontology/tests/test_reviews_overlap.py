"""A derived alias holds only while its link has an acceptance in force (``alias_spans``):
one recorded after its link's acceptance was retracted holds over no stretch, and is
never served as an inverted [from, until) span. Evidence label: ``isolated-fixture``."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ontology.reviews import ReviewStore, alias_spans

TENANT = "tenant-a"
EVIDENCE = [{"object_id": "obj-po", "digest": "d1", "field_path": "number"}]


class DerivedAliasOverlapTest(unittest.TestCase):
    def test_an_alias_recorded_outside_its_links_acceptance_does_not_overlap(self):
        ticks = iter(datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(hours=hour)
                     for hour in range(10))
        with tempfile.TemporaryDirectory() as tmp:
            store = ReviewStore(Path(tmp) / "reviews.sqlite", clock=lambda: next(ticks))
            person = {"principal": "reviewer@example.invalid"}
            link, _ = store.record(tenant=TENANT, author=person, decision="accept_link",
                                   subject="obj-mail", target="obj-po", link_type="mentions",
                                   evidence=EVIDENCE)

            def alias(normalized):
                return store.record(
                    tenant=TENANT, author={**person, "derived_from": link["review_id"]},
                    decision="alias", subject="obj-po", evidence=EVIDENCE,
                    alias={"identifier_kind": "po_number", "normalized": normalized,
                           "raw": normalized})[0]

            inside = alias("PO-IN")
            retraction, _ = store.record(tenant=TENANT, author=person, decision="retract",
                                         subject="obj-mail", evidence=[],
                                         retracts=link["review_id"])
            outside = alias("PO-OUT")
            self.assertLess(retraction["recorded_at"], outside["recorded_at"])
            spans = alias_spans(store.alias_records(TENANT, None))
        self.assertEqual([(record["review_id"], start, until) for record, start, until in spans],
                         [(inside["review_id"], inside["recorded_at"],
                           retraction["recorded_at"])])


if __name__ == "__main__":
    unittest.main()
