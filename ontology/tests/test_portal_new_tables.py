"""Relations waiting on a later_ask (portal-new-tables, portal-sage-regrant) in the
drift and health probes.

A relation ``ontology/schema/upstream_asks.json`` declares waiting that the reporting API
does not serve (absent or listed unavailable) is a blocked part naming its ask, never
``missing_unexplained``; a missing relation no ask declares stays unexplained drift; a
declared relation the catalog serves is bound, its columns read from the catalog.
Evidence: injected-transport (the committed catalog snapshot as the fake API's body).
"""

from __future__ import annotations

import json
import unittest

from ontology import schema
from scripts import gen_ontology_skeleton as gen
from scripts import live_read_probes as probes

ASKS = schema.load_upstream_asks()
ABSENT = "sage.ap_vendor"
UNAVAILABLE = "public.assembly_order_status"
UNDECLARED = "silver.customers"


class FakeApi:
    def __init__(self, drop=(), unavailable=()):
        body = json.loads(schema.CATALOG_SNAPSHOT_PATH.read_text("utf-8"))["relations"]
        self.catalog = {e["name"]: {"columns": e["columns"], "key": e["key"],
                                    "since": e["since"], "domain": e["domain"],
                                    "unavailable": e["unavailable"]}
                        for e in body if e["name"] not in drop}
        for name in unavailable:
            self.catalog[name].update(key=None, unavailable="1 approved column is missing "
                                                            "or not granted")

    def relations(self):
        return self.catalog

    def health(self):
        return {"ok": True, "recovery": True}


class LaterAskReportingTest(unittest.TestCase):
    def drift(self, api):
        return gen.drift_live({}, client=api)

    def test_declared_missing_relations_are_blocked_under_their_ask(self):
        report = self.drift(FakeApi(drop=(ABSENT, UNDECLARED), unavailable=(UNAVAILABLE,)))
        parts = {p["part"]: p for p in report["blocked_upstream"]}
        self.assertEqual(parts[ABSENT]["reason"], "later_ask portal-sage-regrant: "
                                                  "not in the API catalog")
        self.assertTrue(parts[UNAVAILABLE]["reason"].startswith("later_ask portal-new-tables: "))
        self.assertNotIn(UNDECLARED, parts)
        # Only the relation no ask declares is unexplained; the rest are blocked parts.
        self.assertEqual(report["missing_unexplained_count"], 1)
        self.assertEqual({m["relation"] for m in report["missing_relations"]} - set(parts),
                         {UNDECLARED})
        self.assertIn(ABSENT, report["waiting_on_later_ask"]["portal-sage-regrant"])
        self.assertFalse(report["ok"])

    def test_served_declared_relation_is_bound_with_catalog_columns(self):
        report = self.drift(FakeApi())
        self.assertEqual(report["missing_relations"], [])
        self.assertEqual(report["waiting_on_later_ask"], {})
        self.assertEqual(report["later_ask_now_served"], sorted(ASKS))
        compiled = schema.load_schema()
        catalog = schema.load_catalog()
        for relation in ASKS:
            owners = compiled.portal_bindings()[relation]
            read = {schema._column(s["path"]) for tid, b in owners
                    for p in compiled.object_types[tid]["properties"]
                    for s in p["sources"] if s["binding"] == b["id"]}
            self.assertTrue(read <= set(catalog.relations[relation].columns), relation)
            if relation.startswith(("sage.", "silver.")):  # generated: every column is bound
                self.assertEqual(read, set(catalog.relations[relation].columns), relation)

    def test_health_names_the_ask_of_an_unavailable_relation(self):
        api = FakeApi(unavailable=(UNAVAILABLE,))
        report = probes.portal_health({}, client=api)
        self.assertTrue(report["unavailable_relations"][UNAVAILABLE]
                        .startswith("later_ask portal-new-tables: "))
        self.assertTrue(report["degraded"])
        self.assertEqual([p["part"] for p in report["blocked_upstream"]], [UNAVAILABLE])

    def test_malformed_or_duplicated_declaration_fails_closed(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asks.json"
            for doc in ({"asks": {"a": {"relations": ["x"]}, "b": {"relations": ["x"]}}},
                        {"asks": {"a": {"relations": "x"}}}, []):
                path.write_text(json.dumps(doc), encoding="utf-8")
                with self.assertRaises(ValueError):
                    schema.load_upstream_asks(path)


if __name__ == "__main__":
    unittest.main()
