"""``ontology.owner_links`` ``sage_code``: a Sage order's or customer's salesperson code
reaches a staff Person only through the Sage salesperson row's employee code, as a lead."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from ontology import identifiers, owner_links as ol, schema
from ontology.project import CapturedRecord, build_store

T = "2026-10-01T00:00:00Z"
KEYS = {"people": ("person_key",), "sage.oe_order_header": ("orduniq",),
        "sage.ar_customer": ("idcust",), "sage.ar_salesperson": ("codeslsp",)}
STAFF, BARRED, TWIN_A, TWIN_B = ("11111111-1111-1111-1111-11111111111%d" % i for i in range(4))


def person(key, oid, local, domain="plx.test"):
    return {"person_key": key, "kind": "staff", "object_id": oid,
            "addresses": [f"{local}@{domain}"], "domain": domain}


PEOPLE = {"people": [person("p1", STAFF, "jane.doe"), person("p2", BARRED, "omar"),
                     person("p3", TWIN_A, "shared"), person("p4", TWIN_B, "shared")]}
# code (space padded, as Sage stores it) -> employee code
SALESPEOPLE = [("AA   ", "jane.doe  "), ("BB   ", "      "), ("CC   ", "shared"),
               ("DD   ", "omar"), ("EE   ", "nobody-x")]
ORDERS = {"sage.oe_order_header": [
    {"orduniq": i, "salesper1": code} for i, code in enumerate(
        ["aa  ", "BB", "CC", "DD", "ZZ", "EE", ""], 1)]}
CUSTOMERS = {"sage.ar_customer": [
    {"idcust": f"C{i}", "codeslsp1": code} for i, code in enumerate(
        ["AA", "BB", "ZZ"], 1)]}
SALES = {"sage.ar_salesperson": [{"codeslsp": c, "codeempl": e} for c, e in SALESPEOPLE]}
BARRED_ENV = {"PLX_EXCLUDED_PEOPLE_OIDS": BARRED, "PLX_EXCLUSION_ENV_FILE": ""}


class Source:
    def __init__(self, name, rows):
        self.name, self.rows = name, rows

    def describe(self):
        return {"generation": 1, "relations": {}}

    def records(self, relations):
        out = {}
        for rel in relations:
            out[rel] = []
            for row in self.rows.get(rel, []):
                key = json.dumps({k: row[k] for k in KEYS.get(rel, ("id",))},
                                 sort_keys=True, separators=(",", ":"))
                digest = hashlib.sha256(f"{rel}/{key}".encode()).hexdigest()
                out[rel].append(CapturedRecord(self.name, rel, key, row,
                                               f"{self.name}://{rel}/{key}@X", "row", digest,
                                               T, None, T, f"{self.name}:{rel}"))
        return out


def links(orders=ORDERS, customers=CUSTOMERS, sales=SALES, people=PEOPLE, env=BARRED_ENV):
    with mock.patch.dict(os.environ, env, clear=False):
        os.environ.pop("PLX_EXCLUDED_PEOPLE_FILE", None)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "store.sqlite"
            build_store(path, schema.load_schema(), {
                "graph_mail": Source("graph_mail", people),
                "portal_api": Source("portal_api", {**orders, **customers, **sales})},
                normalizer=identifiers.normalize,
                clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
                post_project={"with_owners": ol.with_owners_step})
            conn = sqlite3.connect(str(path))
            rows = conn.execute(
                "SELECT link_type, from_object, to_object, tier, reason, evidence_handles, "
                "markings FROM links WHERE link_id >= 'owner:' AND link_id < 'owner;' "
                "ORDER BY 1, 2").fetchall()
            conn.close()
    return rows


class SageCode(unittest.TestCase):
    def test_a_listed_code_reaches_the_staff_person_as_a_lead_with_both_citations(self):
        rows = links()
        by = {(r[0], json.loads(r[1].split(":", 2)[2]).get("orduniq")
               or json.loads(r[1].split(":", 2)[2]).get("idcust")): r for r in rows}
        self.assertEqual(sorted(by), [("Customer.account_owner", "C1"),
                                      ("SalesOrder.sales_rep", 1)])
        for row in by.values():
            self.assertEqual(row[3], "lead")
            self.assertIn("p1", row[2])
            features = {f["feature"]: f for f in json.loads(row[4])}
            self.assertEqual(set(features), {"sage_salesperson_code_key", "salesperson_employee",
                                             "person_key"})
            fields = {c["field_path"] for c in features["salesperson_employee"]["citations"]}
            self.assertTrue(any(f.endswith("codeempl") for f in fields), fields)
            handles = json.loads(row[5])
            self.assertTrue(any("sage.ar_salesperson" in h for h in handles), handles)
            self.assertTrue({"finance", "pii"} <= set(json.loads(row[6])), row[6])

    def test_blank_unlisted_ambiguous_unmatched_and_excluded_codes_name_nobody(self):
        # BB blank employee, ZZ unlisted, CC employee shared by two Persons, DD an excluded
        # Person, EE an employee code no Person holds, "" a blank order value.
        rows = links()
        self.assertEqual({r[1] for r in rows if "orduniq" in r[1]}, {
            r[1] for r in rows if r[1].endswith('"orduniq":1}')})
        self.assertFalse(any("p2" in r[2] or "p3" in r[2] or "p4" in r[2] for r in rows))

    def test_without_the_exclusion_list_nothing_is_written(self):
        self.assertEqual(links(env={"PLX_EXCLUSION_ENV_FILE": ""}), [])

    def test_the_excluded_person_is_linked_only_when_not_excluded(self):
        other = "99999999-9999-9999-9999-999999999999"
        rows = links(env={"PLX_EXCLUDED_PEOPLE_OIDS": other, "PLX_EXCLUSION_ENV_FILE": ""})
        self.assertEqual(sum("p2" in r[2] for r in rows), 1)

    def test_a_code_listed_with_two_employee_codes_is_ambiguous(self):
        counts = ol.Counter()
        holder = ol.People()
        sales = {"ff": [{"employee": "a", "rows": [], "handle": "h"},
                        {"employee": "b", "rows": [], "handle": "h"}]}
        self.assertIsNone(ol._sage_owner(holder, sales, "FF", "T.x", counts))
        self.assertEqual(counts["T.x:ambiguous"], 1)

    def test_cardinality_loses_no_row(self):
        for n in (1, 10, 60):
            orders = {"sage.oe_order_header": [{"orduniq": i, "salesper1": "AA"}
                                               for i in range(1, n + 1)]}
            rows = [r for r in links(orders=orders) if r[0] == "SalesOrder.sales_rep"]
            self.assertEqual(len(rows), n)
            self.assertEqual(len({r[1] for r in rows}), n)


if __name__ == "__main__":
    unittest.main()
