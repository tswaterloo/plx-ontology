"""Live-added Portal relations: ``merge-live`` adds every relation a recorded
``GET /v1/relations`` body serves that the pinned manifest catalog lacks (structure only),
the registry lists each citing the live read, and the committed files agree."""

from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from scripts import gen_ontology_skeleton as gen
from scripts import source_registry as sr

CATALOG_PATH = sr.PORTAL_CATALOG_PATH
SECRET_OR_PAY = re.compile(r"wage|salary|pay_?rate|hourly|password|secret|token|hash|(?:^|_)ssn(?:_|$)", re.I)


def _row(name, columns, key, domain="purchasing"):
    schema, relation = name.split(".", 1)
    return {"name": name, "schema": schema, "relation": relation, "domain": domain,
            "columns": columns, "key": key, "since": None, "unavailable": None}


BASE = {"format": "plx-portal-catalog-snapshot/v1", "evidence_mode": "upstream-manifest",
        "note": "n", "source": {"repo": "plx-customer-portal", "commit": "52c9116f77ab", "paths": []},
        "relations": [dict(_row("public.app_a", ["id", "gone", "name"], ["id"]),
                           key_source="primary-key", relkind="r",
                           column_types={"id": "text", "gone": "text"})]}
LIVE = {"relations": [_row("public.app_a", ["id", "name", "extra"], ["id"]),
                      _row("sage.z_new", ["_sage_pk", "descr"], ["_sage_pk"]),
                      _row("public.other_lane", ["id"], ["id"])]}
REF = "live GET /v1/relations@2026-10-26"


class MergeTests(unittest.TestCase):
    def merge(self, **kw):
        return gen.merge_live_catalog(copy.deepcopy(BASE), copy.deepcopy(LIVE), REF,
                                      as_of="2026-10-26", **kw)

    def test_new_relations_are_added_structure_only_and_a_skipped_one_is_left_to_its_lane(self):
        merged = self.merge(skip=["public.other_lane"])
        by = {r["name"]: r for r in merged["relations"]}
        self.assertEqual(sorted(by), ["public.app_a", "sage.z_new"])
        self.assertEqual(merged["live_added"]["relations"], ["sage.z_new"])
        self.assertEqual(set(by["sage.z_new"]),
                         {"name", "schema", "relation", "domain", "columns", "key", "since",
                          "unavailable", "live_added"})

    def test_a_manifest_relation_keeps_its_facts_and_takes_the_live_columns(self):
        old = {r["name"]: r for r in self.merge()["relations"]}["public.app_a"]
        self.assertEqual(old["columns"], ["id", "name", "extra"])
        self.assertEqual((old["key_source"], old["relkind"]), ("primary-key", "r"))
        self.assertEqual(old["column_types"], {"id": "text"})   # the column no longer served is gone
        self.assertNotIn("live_added", old)

    def test_merge_records_the_served_count_and_structure_digest_of_the_live_read(self):
        block = self.merge()["live_added"]
        self.assertEqual(block["live_served"], 3)
        self.assertEqual(block["live_digest"], gen.served_structure_digest(LIVE["relations"]))
        changed = copy.deepcopy(LIVE)
        changed["relations"][1]["columns"].append("added_later")
        self.assertNotEqual(gen.served_structure_digest(changed["relations"]),
                            block["live_digest"])

    def test_merge_is_idempotent(self):
        once = self.merge()
        again = gen.merge_live_catalog(once, copy.deepcopy(LIVE), REF, as_of="2026-10-26")
        self.assertEqual(once, again)

    def test_a_broken_live_body_fails_closed(self):
        for bad in ({}, {"relations": "x"},
                    {"relations": [_row("public.t", ["id", "id"], ["id"])]},
                    {"relations": [_row("public.t", ["id"], ["nokey"])]},
                    {"relations": [_row("public.t", ["id"], ["id"])] * 2}):
            with self.subTest(bad=str(bad)[:40]), self.assertRaises(ValueError):
                gen.merge_live_catalog(copy.deepcopy(BASE), bad, REF, as_of="2026-10-26")


class RegistryTests(unittest.TestCase):
    def registry(self):
        return {"format": sr.FORMAT, "upstream": {}, "counts": {}, "entries_digest": "",
                "entries": [{"id": "portal-reporting-api.relation:public.app_a",
                             "system": "portal-reporting-api", "kind": "relation",
                             "name": "public.app_a", "evidence": ["x"]}]}

    def test_live_added_relations_are_listed_citing_the_live_read_and_the_step_is_idempotent(self):
        catalog = gen.merge_live_catalog(copy.deepcopy(BASE), copy.deepcopy(LIVE), REF,
                                         as_of="2026-10-26", skip=["public.other_lane"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            once = sr.with_live_relations(self.registry(), path)
            twice = sr.with_live_relations(once, path)
        self.assertEqual(once, twice)
        entry = next(e for e in once["entries"] if e["name"] == "sage.z_new")
        self.assertEqual(entry["evidence"], [
            "plx-customer-portal:live GET /v1/relations@2026-10-26 (read-only catalog; not in "
            "the pinned allowlist.json@52c9116f77ab)"])
        self.assertEqual(once["counts"]["by_system"]["portal-reporting-api"], {"relation": 2})
        self.assertEqual(once["entries_digest"], sr.entries_digest(once["entries"]))


class CommittedFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        cls.added = set(cls.catalog["live_added"]["relations"])

    def test_every_live_added_relation_is_in_the_registry_the_host_config_and_the_catalog(self):
        registry = {e["name"] for e in sr.load_registry()["entries"] if e["kind"] == "relation"}
        host = json.loads((sr.ROOT / "examples/host.all-sources.live-check.json").read_text("utf-8"))
        names = {r["name"] for r in self.catalog["relations"]}
        self.assertLessEqual(self.added, names)
        self.assertLessEqual(self.added, registry)
        self.assertLessEqual(self.added, set(host["portal"]["relations"]))
        self.assertEqual(sr.with_live_relations(sr.load_registry())["entries_digest"],
                         sr.load_registry()["entries_digest"])

    def test_the_pinned_catalog_is_exactly_what_the_live_api_served_on_the_recorded_date(self):
        # 248 relations served by GET /v1/relations on 2026-10-26 (all available): the
        # catalog's names, columns and keys reproduce the recorded digest of that read, so a
        # relation dropped, added or changed by hand fails here.
        block = self.catalog["live_added"]
        self.assertEqual(block["as_of"], "2026-10-26")
        self.assertEqual(block["live_served"], 248)
        self.assertEqual(len(self.catalog["relations"]), block["live_served"])
        self.assertEqual(gen.served_structure_digest(self.catalog["relations"]),
                         block["live_digest"])
        self.assertEqual(len(self.added), 144)

    def test_every_served_relation_has_a_mirror_lineage_and_bc_snapshots_are_not_fm_bronze(self):
        from integrations import source_mirror as sm
        rules = sm.load_ingest_config()["lineage_rules"]
        for row in self.catalog["relations"]:
            with self.subTest(relation=row["name"]):
                lineage, rule = sm.classify_lineage(rules, row["name"], {})
                self.assertIn(lineage, sm.LINEAGES, rule)
                if row["name"].startswith("public.bc_"):
                    self.assertEqual(lineage, "gold_native_staging")

    def test_no_live_added_column_is_a_secret_or_pay_column_and_people_access_is_withheld(self):
        for row in self.catalog["relations"]:
            if row["name"] in self.added:
                with self.subTest(relation=row["name"]):
                    self.assertEqual([c for c in row["columns"] if SECRET_OR_PAY.search(c)], [])
        marks = json.loads((sr.ROOT / "ontology/schema/overrides/markings.json").read_text("utf-8"))
        self.assertEqual([(d["domain"], d["markings"]) for d in marks["domain_markings"]],
                         [("people-access", ["pii"])])


if __name__ == "__main__":
    unittest.main()
