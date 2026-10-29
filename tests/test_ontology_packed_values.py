"""Packed values (store layout v10) at the tool boundary.

A binding declared ``value_storage: packed`` (``ontology/schema/overrides/storage.json``:
the high-volume Sage AP ledger relations) stores each record's values as one packed record
and does not word-index them. What must hold through ``plx.find`` and ``plx.object``:

- a ledger line is found by its key, its amount and its date (property filters), and
  ``plx.object`` reads every value of it with its citation, at 1, 10 and 60 lines;
- a reader without ``finance`` never sees a finance-marked amount of a packed record, and a
  filter on it is never answered as a match or as an absence;
- a text-heavy relation stored as cells is still found by its words (the word index);
  a packed record's words are not in the index.

Evidence label: isolated-fixture (static captures over the committed schema; no business
system is read).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from ontology import identifiers
from ontology.core import Actor
from ontology.project import CapturedRecord, build_store
from ontology.schema import load_schema
from operating import ontology_tools as ot

T = datetime(2026, 10, 16, tzinfo=timezone.utc)
AT = "2026-10-01T00:00:00.000000Z"
LEDGER = "sage.ap_pjd"
LEDGER_TYPE = "RelSageApPjd"
FINANCE = Actor("plx", frozenset({"staff:finance"}), "restricted")
OPERATIONS = Actor("plx", frozenset({"staff:operations"}), "restricted")
_SCHEMA: list = []


def schema():
    if not _SCHEMA:
        _SCHEMA.append(load_schema())
    return _SCHEMA[0]


class Captures:
    source = "portal_api"

    def __init__(self, records):
        self._records = records

    def records(self, relations):
        return {r: [x for x in self._records if x.relation == r] for r in relations}

    def describe(self):
        return {"generation": "static", "relations": {}}


def ledger_line(n: int) -> CapturedRecord:
    key = f"AD|10|19|1|{n}"
    row = {"_sage_pk": key, "amtextndhc": f"-{n}.700", "datebus": f"2017{11:02d}{n % 28 + 1:02d}",
           "idinvc": f"HEALTH INS NOV/{n}     ", "idvend": "BOYC02      ",
           "glcomment": " " * 250, "gldesc": f"HEALTH INS NOV/{n}" + " " * 40, "cntline": str(n)}
    return CapturedRecord("portal_api", LEDGER, json.dumps({"_sage_pk": key}), row,
                          f"api://{LEDGER}/{n}@g-1", "row", f"{n:064d}", AT, None, AT,
                          f"api:{LEDGER}")


def supplier() -> CapturedRecord:
    row = {"id": "s1", "code": "ACME01", "name": "Acmewidget Fabrication"}
    return CapturedRecord("portal_api", "public.app_supplier", '{"id":"s1"}', row,
                          "api://public.app_supplier/s1@g-1", "row", "f" * 64, AT, None, AT,
                          "api:public.app_supplier")


class PackedLedgerTests(unittest.TestCase):

    def build(self, lines: int):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.sqlite"
        records = [ledger_line(n) for n in range(1, lines + 1)] + [supplier()]
        build_store(path, schema(), {"portal_api": Captures(records)},
                    normalizer=identifiers.normalize, clock=lambda: T)
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        policy = ot.ReadPolicy("plx", audiences={
            "api:*": frozenset({"staff:finance", "staff:operations"})},
            markings={"finance": frozenset({"staff:finance"})})
        tools = ot.OntologyTools(reader, policy, schema=ot.SchemaView.from_compiled(schema()),
                                 grammar=identifiers, contracts=ot.load_contracts())
        return reader.conn, tools

    @staticmethod
    def oid(n: int) -> str:
        return f'{LEDGER_TYPE}:gen.{LEDGER}:{json.dumps({"_sage_pk": f"AD|10|19|1|{n}"}, separators=(",", ":"))}'

    def found(self, out) -> list[str]:
        return [c["object"]["object_id"] for c in out.get("candidates", [])] + (
            [out["object"]["object_id"]] if out.get("status") == "found" and out.get("object")
            else [])

    def test_a_ledger_line_is_found_and_read_by_key_amount_and_date_at_any_cardinality(self):
        binding = next(b for t in schema().object_types.values() for b in t.get("bindings", [])
                       if b["id"] == f"gen.{LEDGER}")
        self.assertEqual(binding.get("value_storage"), "packed")   # declared, with evidence
        for lines in (1, 10, 60):
            with self.subTest(lines=lines):
                conn, tools = self.build(lines)
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM value_cells c JOIN value_sources s ON s.src = c.src "
                    "WHERE c.field IS NULL AND s.binding_id = ?", (f"gen.{LEDGER}",)
                    ).fetchone()[0], lines)
                n = lines
                for prop, value in (("sage_pk", f"AD|10|19|1|{n}"), ("amtextndhc", f"-{n}.700"),
                                    ("datebus", f"201711{n % 28 + 1:02d}")):
                    out = tools.call("plx.find", {"object_type": LEDGER_TYPE, "filters": [
                        {"property": prop, "op": "eq", "value": value}]}, FINANCE)
                    self.assertIn(self.oid(n), self.found(out), (prop, out))
                    if prop != "datebus":
                        self.assertEqual(self.found(out), [self.oid(n)], (prop, out))
                read = tools.call("plx.object", {"object_id": self.oid(n)}, FINANCE)
                values = {v["property"]: v for v in read["values"]}
                self.assertEqual(len(values), 8)
                amount = values["amtextndhc"]
                self.assertEqual(json.dumps(amount["value"]).count(f"-{n}.700"), 1)
                self.assertEqual((amount["citation"]["l1_handle"], amount["citation"]["field_path"]),
                                 (f"api://{LEDGER}/{n}@g-1", "row.amtextndhc"))
                self.assertIn(" " * 250, json.dumps(values["glcomment"]["value"]))  # as written
                # every value of every line reads back (no row lost or duplicated)
                self.assertEqual(conn.execute("SELECT count(*) FROM object_values WHERE "
                                              "binding_id = ?", (f"gen.{LEDGER}",)).fetchone()[0],
                                 8 * lines)

    def test_a_reader_without_finance_never_sees_a_packed_amount(self):
        conn, tools = self.build(10)
        read = tools.call("plx.object", {"object_id": self.oid(3)}, OPERATIONS)
        self.assertNotIn("amtextndhc", {v["property"] for v in read["values"]})
        self.assertNotIn("-3.700", json.dumps(read))
        self.assertIn("idinvc", {v["property"] for v in read["values"]})
        out = tools.call("plx.find", {"object_type": LEDGER_TYPE, "filters": [
            {"property": "amtextndhc", "op": "eq", "value": "-3.700"}]}, OPERATIONS)
        self.assertNotIn(self.oid(3), self.found(out), out)
        self.assertNotEqual(out.get("status"), "found")
        self.assertNotIn("-3.700", json.dumps(out.get("candidates", [])))
        # never claimed absent: the withheld value is an unknown, the answer incomplete
        self.assertIn(("unknown", "marking_withheld"),
                      {(g["state"], g["reason"]) for g in out["gaps"]}, out)
        self.assertFalse(out["completeness"]["complete"], out)

    def test_text_relations_keep_the_word_index_and_packed_words_are_not_in_it(self):
        conn, tools = self.build(10)
        out = tools.call("plx.find", {"query": "Acmewidget"}, OPERATIONS)
        self.assertTrue(any("public.app_supplier" in oid or "Supplier" in oid
                            for oid in self.found(out)), out)
        self.assertGreater(conn.execute("SELECT count(*) FROM value_text WHERE value_text "
                                        "MATCH 'Acmewidget'").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT count(*) FROM value_text WHERE value_text "
                                      "MATCH 'HEALTH'").fetchone()[0], 0)
        out = tools.call("plx.find", {"query": "HEALTH INS"}, FINANCE)
        self.assertFalse(any(oid.startswith(LEDGER_TYPE) for oid in self.found(out)), out)


class StorageOverrideTests(unittest.TestCase):

    def test_a_storage_entry_the_catalog_does_not_generate_fails_the_generation(self):
        import dataclasses
        from unittest import mock
        from ontology.generator.overrides import OverrideError
        from scripts import gen_ontology_skeleton as skeleton
        typo = dataclasses.replace(skeleton.OVERRIDES, storage={
            "portal_api": {**skeleton.OVERRIDES.storage["portal_api"], "sage.ap_pjdd": "packed"}})
        with mock.patch.object(skeleton, "OVERRIDES", typo), \
                self.assertRaisesRegex(OverrideError, "sage.ap_pjdd"):
            skeleton.build()


if __name__ == "__main__":
    unittest.main()
