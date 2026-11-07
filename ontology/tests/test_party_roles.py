"""A customer linked to the supplier record of the same party (``ontology.party_roles``).

Evidence label: ``isolated-fixture`` (made-up FileMaker customers and suppliers, the real
projector, the compiled schema and the step; no source read).

* Equal names (legal words, case and punctuation aside) of a customer and a supplier of
  one system are a lead; a name that only begins alike, an own party's name, and a pair a
  person rejected are not linked.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ontology import identifiers, schema
from ontology import party_roles as pr
from ontology.project import CapturedRecord, build_store

T = "2026-10-01T00:00:00Z"


class Source:
    def __init__(self, rows):
        self.rows = rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        out = {}
        for rel in relations:
            out[rel] = []
            for row in self.rows.get(rel, []):
                key = json.dumps({"id": row["id"]})
                digest = hashlib.sha256(f"{rel}/{key}".encode()).hexdigest()
                out[rel].append(CapturedRecord("portal_api", rel, key, row,
                                               f"api://{rel}/{row['id']}@X", "row", digest, T,
                                               None, T, f"api:{rel}"))
        return out


PORTAL = {
    "public.customer": [{"id": 1, "name": "Quorvel Botanicals Inc."},
                        {"id": 2, "name": "Brindlewick"},
                        {"id": 3, "name": "Petra Soap"},
                        {"id": 4, "name": "Zentaro Labs"}],
    "public.supplier": [{"id": 11, "name": "QUORVEL BOTANICALS"},
                        {"id": 12, "name": "Brindlewick Logistics"},
                        {"id": 13, "name": "Petra Soap"},
                        {"id": 14, "name": "Zentaro Labs Ltd"}],
}


CUST = {n: f'Customer:portal.fm_customer:{{"id":{n}}}' for n in (1, 2, 3, 4)}
SUPP = {n: f'Supplier:portal.fm_supplier:{{"id":{n}}}' for n in (11, 12, 13, 14)}


def reject(builder, projector):
    builder.add("links", [{
        "link_id": "review:r1", "link_type": pr.LINK_TYPE, "from_object": CUST[4],
        "to_object": SUPP[14], "tier": "contradicted", "binding_kind": "reviewed",
        "link_contract": None, "reason": '[{"agrees":false,"feature":"review"}]',
        "why_not": "[]", "evidence_handles": '["review:a1"]', "reviewed_by": "tester",
        "reviewed_at": T, "review_action_id": "a1", "markings": "[]", "recorded_at": T,
        "superseded_at": None}])
    return {}


class PartyRoles(unittest.TestCase):
    def test_equal_names_of_one_system_are_a_lead(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "store.sqlite"
            build_store(path, schema.load_schema(), {"portal_api": Source(PORTAL)},
                        normalizer=identifiers.normalize,
                        clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
                        post_project={"reviews": reject, "roles": pr.roles_step})
            conn = sqlite3.connect(str(path))
            try:
                rows = conn.execute(
                    "SELECT from_object, to_object, link_type, tier FROM links WHERE "
                    "link_id >= ? AND link_id < ?", (pr.PREFIX, pr._end(pr.PREFIX))).fetchall()
            finally:
                conn.close()
        self.assertEqual(rows, [(CUST[1], SUPP[11], pr.LINK_TYPE, "lead")])


if __name__ == "__main__":
    unittest.main()
