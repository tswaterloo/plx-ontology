"""Order lines pegged to the PO lines that supply them (``ontology.demand_pegging``) and
records linked to their owner Person (``ontology.owner_links``).

Evidence label: ``isolated-fixture`` (made-up Portal orders, POs, products, an assembly
order, people and Mission Control tasks; the real projector, the compiled schema and the
steps; no source read).

* Open demand takes open supply of the same item in need-date order, covering supply
  first, then late supply (``due_after_need``); the assembly order's open component is
  demand of its item; closed, unknown-status, other-unit and cancelled-PO lines peg
  nothing; every link is a lead, Decimal quantities, no quantity lost or duplicated
  (N = 1, 10, 60).
* Owners: a bare MC assignee and a FileMaker requisitioner name reach their one staff
  Person; nothing links to an excluded person, and with no exclusion list the step writes
  nothing.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ontology import demand_pegging as dp
from ontology import identifiers, owner_links as ol, schema
from ontology.project import CapturedRecord, build_store

T = "2026-10-01T00:00:00Z"
KEYS = {"tasks": ("scope", "id"), "people": ("person_key",)}


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


def so(i, status, required):
    return {"id": i, "order_number": f"SO-{i}", "status": status, "required_date": required}


def sol(i, order, product, qty, uom="ea"):
    return {"id": i, "sales_order_id": order, "product_id": product, "quantity": str(qty),
            "uom": uom}


def po(i, status, expected):
    return {"id": i, "po_number": f"PO-{i}", "status": status, "expected_date": expected}


def pol(i, order, product, qty, received=0, uom="ea"):
    return {"id": i, "purchase_order_id": order, "product_id": product, "quantity": str(qty),
            "quantity_received": str(received), "uom": uom}


PORTAL = {
    "public.app_product": [{"id": 1, "code": "RM-1"}, {"id": 2, "code": "FG-2"}],
    "public.app_sales_order": [so(1, "Open", "2026-12-10"), so(2, "Open", "2026-12-01"),
                               so(3, "Closed", "2026-12-01"), so(4, "Weird", "2026-12-01"),
                               so(5, "Open", "2026-12-20"), so(6, "Open", "2026-12-02")],
    "public.app_sales_order_line": [sol(11, 1, 1, 100), sol(12, 2, 1, 50), sol(13, 3, 1, 999),
                                    sol(14, 4, 1, 999), sol(15, 5, 2, 10),
                                    sol(16, 6, 1, 5, uom="kg")],
    "public.app_manufacturing_order": [{"id": 51, "mo_code": "MO-51", "status": "RELEASED",
                                        "sales_order_line_id": 15}],
    "public.app_manufacturing_order_line": [{"id": 61, "assembly_order_id": 51,
                                             "product_id": 1, "quantity": "30",
                                             "actual_amount": "10"}],
    "public.app_purchase_order": [po(21, "Pending", "2026-11-25"), po(22, "Pending", "2026-12-15"),
                                  po(23, "Cancel", "2026-11-20")],
    "public.app_purchase_order_line": [pol(31, 21, 1, 60), pol(32, 22, 1, 200),
                                       pol(33, 23, 1, 500)],
}


def oid(t, b, i):
    return f'{t}:{b}:{{"id":{i}}}'


L = {i: oid("SalesOrderLine", "portal.app_sales_order_line", i) for i in range(11, 17)}
S = {i: oid("SalesOrder", "portal.app_sales_order", i) for i in range(1, 7)}
P = {i: oid("PurchaseOrderLine", "portal.app_purchase_order_line", i) for i in (31, 32, 33)}
PO = {i: oid("PurchaseOrder", "portal.app_purchase_order", i) for i in (21, 22, 23)}


def build(sources, steps):
    temp = tempfile.TemporaryDirectory()
    path = Path(temp.name) / "store.sqlite"
    build_store(path, schema.load_schema(), sources, normalizer=identifiers.normalize,
                clock=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc), post_project=steps)
    conn = sqlite3.connect(str(path))
    rows = conn.execute("SELECT link_type, from_object, to_object, tier, reason FROM links "
                        "WHERE link_id >= 'owner:' AND link_id < 'owner;' OR link_id >= 'peg:'"
                        " AND link_id < 'peg;' ORDER BY 1, 2, 3").fetchall()
    conn.close()
    temp.cleanup()
    return rows


class SupplyPegging(unittest.TestCase):
    def test_open_demand_takes_open_supply_in_date_order(self):
        rows = build({"portal_api": Source("portal_api", PORTAL)},
                     {"supply_pegging": dp.supply_pegging_step})
        got = {(r[0], r[1], r[2]): r for r in rows}
        line = "SalesOrderLine.supplied_by"
        self.assertEqual(sorted(k for k in got if k[0] == line), sorted([
            (line, L[11], P[31]), (line, L[11], P[32]), (line, L[12], P[31]),
            (line, L[15], P[32])]))
        self.assertEqual({r[3] for r in rows}, {"lead"})

        def pegged(key):
            return [(f["quantity"], f["pegging"], f.get("via_component"))
                    for f in json.loads(got[key][4]) if f["feature"] == "pegged_quantity"]
        self.assertEqual(pegged((line, L[12], P[31])), [("50", "covers_need_date", None)])
        self.assertEqual(pegged((line, L[11], P[31])), [("10", "covers_need_date", None)])
        self.assertEqual(pegged((line, L[11], P[32])), [("90", "due_after_need", None)])
        comp = oid("AssemblyOrderComponent", "portal.app_manufacturing_order_line", 61)
        self.assertEqual(pegged((line, L[15], P[32])), [("20", "covers_need_date", comp)])
        order = "SalesOrder.awaits_purchase_order"
        self.assertEqual(sorted(k for k in got if k[0] == order), sorted([
            (order, S[1], PO[21]), (order, S[1], PO[22]), (order, S[2], PO[21]),
            (order, S[5], PO[22])]))

    def test_a_lead_item_twin_joins_items_only_when_it_is_the_one_partner(self):
        got = dp.item_twins([("a", "b", "verified"), ("c", "d", "lead"), ("e", "f", "lead"),
                             ("e", "g", "lead"), ("h", "i", "contradicted")])
        self.assertEqual(got, [("a", "b"), ("c", "d")])

    def test_allocation_loses_and_duplicates_no_quantity(self):
        for n in (1, 10, 60):
            demands = [{"line": f"d{i:03}", "date": "2026-12-01", "quantity": Decimal("1.5"),
                        "uom": None} for i in range(n)]
            supplies = [{"line": "s1", "date": "2026-11-01", "quantity": Decimal(n),
                         "uom": None},
                        {"line": "s2", "date": "2027-01-01", "quantity": Decimal(n),
                         "uom": None}]
            got = dp.allocate(demands, supplies)
            self.assertEqual(sum(q for _i, _j, q, _c in got), Decimal("1.5") * n)
            self.assertEqual(sum(q for _i, j, q, _c in got if j == 0), Decimal(n))
            self.assertTrue(all(c == (j == 0) for _i, j, _q, c in got))
            self.assertEqual(len({(i, j) for i, j, _q, _c in got}), len(got))
            self.assertEqual({i for i, *_ in got}, set(range(n)))


STAFF = "11111111-1111-1111-1111-111111111111"
BARRED = "22222222-2222-2222-2222-222222222222"
PEOPLE = {"people": [
    {"person_key": "p1", "kind": "staff", "object_id": STAFF,
     "addresses": ["jane.doe@plx.test"], "domain": "plx.test"},
    {"person_key": "p2", "kind": "staff", "object_id": BARRED,
     "addresses": ["omar@plx.test"], "domain": "plx.test"}]}
MC = {"tasks": [{"scope": "s", "id": "t1", "title": "Call", "assignee": "jane.doe"},
                {"scope": "s", "id": "t2", "title": "Call", "assignee": "omar"}]}
FM = {"public.generated_purchase_order": [
    {"id": 7, "ponumber": "PO#7", "requisitioner_c": "Jane Doe"},
    {"id": 8, "ponumber": "PO#8", "requisitioner_c": "Omar"}]}


class Owners(unittest.TestCase):
    def test_text_mentions_keep_their_own_link_types(self):
        from ontology import mentions
        pairs = mentions.mention_link_types(dict(schema.load_schema().link_types))
        self.assertEqual(pairs[("MCTask", "Person")], "MCTask.with_person")
        self.assertEqual(pairs[("PlannerTask", "Person")], "PlannerTask.with_person")
        self.assertNotIn(("SalesOrderLine", "PurchaseOrderLine"), pairs)
        self.assertNotIn("SalesOrder.awaits_purchase_order", pairs.values())

    def owners(self, env):
        with mock.patch.dict(os.environ, env, clear=False):
            for name in ("PLX_EXCLUDED_PEOPLE_OIDS", "PLX_EXCLUDED_PEOPLE_FILE"):
                if name not in env:
                    os.environ.pop(name, None)
            return build({"graph_mail": Source("graph_mail", PEOPLE),
                          "mission_control": Source("mission_control", MC),
                          "portal_api": Source("portal_api", FM)},
                         {"with_owners": ol.with_owners_step})

    def test_owner_fields_reach_one_staff_person_never_an_excluded_one(self):
        rows = self.owners({"PLX_EXCLUDED_PEOPLE_OIDS": BARRED, "PLX_EXCLUSION_ENV_FILE": ""})
        got = {(r[0], r[1].split(":", 2)[2], r[3]) for r in rows}
        self.assertEqual(got, {("MCTask.assigned_to", '{"id":"t1","scope":"s"}', "corroborated"),
                               ("PurchaseOrder.buyer", '{"id":7}', "lead")})
        self.assertFalse(any("p2" in r[2] for r in rows))
        other = "33333333-3333-3333-3333-333333333333"
        unbarred = self.owners({"PLX_EXCLUDED_PEOPLE_OIDS": other, "PLX_EXCLUSION_ENV_FILE": ""})
        self.assertEqual(sum("p2" in r[2] for r in unbarred), 2)

    def test_no_exclusion_list_writes_nothing(self):
        self.assertEqual(self.owners({"PLX_EXCLUSION_ENV_FILE": ""}), [])


if __name__ == "__main__":
    unittest.main()
