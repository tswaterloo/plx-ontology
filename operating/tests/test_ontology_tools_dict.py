"""One-call dictionary read (Ask spec 20 G9 / S13, A6): ``plx.sql {"describe": "<Type>"}``
and ``plx.sql {"describe": "dictionary"}`` return type, date key, amount key, party key
and live links with counts in one read through the reader's own door."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from ontology.json_contract import validate
from operating import ontology_tools_dict as otd
from operating import ontology_tools_sql as ts
from operating.tests import test_chain_read as tcr
from operating.tests import test_generic_views as tgv
from tests import test_ontology_tools_evidence as ev


class _FakeCur:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakePg:
    def __init__(self, link_rows):
        self.calls: list[tuple[str, tuple]] = []
        self._link_rows = link_rows

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        if "to_regclass('record_links')" in sql:
            return _FakeCur([("record_links",)])
        return _FakeCur(self._link_rows)


class TypeKeysUnitTest(unittest.TestCase):
    def test_extracts_date_amount_and_party_keys_from_schema_only(self):
        entry = {
            "type": "PurchaseOrder",
            "name": "Purchase order",
            "systems": ["bc", "portal"],
            "tables": ["purchase_orders"],
            "label_fields": ["po_number"],
            "date_fields": ["order_date", "updated_at"],
            "dates": [
                {"field": "updated_at", "role": "stamp", "means": "last write", "dated_share": 1.0},
                {"field": "order_date", "role": "record", "means": "PO issue date", "dated_share": 0.99},
            ],
            "fields": [
                {"name": "order_date", "kind": "date"},
                {"name": "total_amount", "kind": "money"},
                {"name": "total_amount_currency", "kind": "currency"},
                {"name": "line_count", "kind": "integer"},
            ],
            "links_out": [
                {"link_type": "PurchaseOrder.vendor", "to": "Supplier"},
                {"link_type": "PurchaseOrder.has_line", "to": "PurchaseOrderLine"},
            ],
            "links_in": [],
        }
        got = otd.type_keys(entry)
        self.assertEqual(got["type"], "PurchaseOrder")
        # prefers role == "record" over stamp
        self.assertEqual((got["date_key"], got["date_role"]), ("order_date", "record"))
        # prefers money field and notes its currency sibling
        self.assertEqual(got["amount_key"], "total_amount")
        self.assertEqual(got["amount_keys"][0]["currency_field"], "total_amount_currency")
        # party key comes from links to PARTY_TYPES
        self.assertEqual(got["party_key"], "PurchaseOrder.vendor -> Supplier")
        self.assertEqual(len(got["party_keys"]), 1)

    def test_party_type_prefers_self_label_over_linked_party_types(self):
        entry = {
            "type": "Customer",
            "name": "Customer",
            "label_fields": ["name", "code"],
            "links_out": [{"link_type": "Customer.primary_contact", "to": "Contact"}],
            "links_in": [{"link_type": "Customer.same_record_as", "from": "Customer"}],
        }
        got = otd.type_keys(entry)
        self.assertEqual(got["party_key"], "name")
        self.assertEqual([pk["via"] for pk in got["party_keys"]],
                         ["self_label", "self_label", "link_out", "link_in"])

    def test_link_in_party_key_uses_incoming_arrow(self):
        entry = {
            "type": "Shipment",
            "name": "Shipment",
            "label_fields": ["shipment_number"],
            "links_out": [],
            "links_in": [{"link_type": "Supplier.ships", "from": "Supplier"}],
        }
        got = otd.type_keys(entry)
        self.assertEqual(got["party_key"], "Supplier.ships <- Supplier")
        self.assertEqual(got["party_keys"], [{"via": "link_in", "link_type": "Supplier.ships",
                                              "party_type": "Supplier"}])

    def test_malformed_catalog_json_logs_warning_and_raises(self):
        entry = {"type": "Broken", "fields": "{not valid json"}
        with self.assertLogs("operating.ontology_tools_dict", level="WARNING") as logs:
            with self.assertRaises(ValueError):
                otd.type_keys(entry)
        self.assertTrue(any("Broken.fields" in m for m in logs.output), logs.output)

    def test_link_counts_for_type_filters_by_declared_link_types_in_one_pass(self):
        pg = _FakePg([
            ("Customer", "Customer.same_record_as", "Customer", "verified", 2),
            ("MCTask", "MCTask.about", "Customer", "corroborated", 5),
        ])
        entry = {
            "type": "Customer",
            "label_fields": ["name"],
            "links_out": [{"link_type": "Customer.same_record_as", "to": "Customer"}],
            "links_in": [{"link_type": "MCTask.about", "from": "MCTask"}],
        }
        got = otd.for_type(pg, entry, 10)
        self.assertEqual(len(pg.calls), 2)
        sql, params = pg.calls[1]
        self.assertIn("link_type = ANY(%s)", sql)
        self.assertNotIn("UNION ALL", sql)
        self.assertEqual(params, (["Customer.same_record_as", "MCTask.about"],
                                  "Customer", "Customer"))
        self.assertEqual(got["links_with_counts"], [
            {"direction": "in", "link_type": "MCTask.about", "other_type": "MCTask",
             "tier": "corroborated", "count": 5},
            {"direction": "in", "link_type": "Customer.same_record_as", "other_type": "Customer",
             "tier": "verified", "count": 2},
            {"direction": "out", "link_type": "Customer.same_record_as", "other_type": "Customer",
             "tier": "verified", "count": 2},
        ])

    def test_link_counts_for_type_fallback_without_declared_links_is_single_pass(self):
        pg = _FakePg([("Undeclared", "Undeclared.ref", "Customer", "lead", 3)])
        got = otd.link_counts_for_type(pg, "Undeclared")
        sql, params = pg.calls[1]
        self.assertNotIn("link_type = ANY(%s)", sql)
        self.assertNotIn("UNION ALL", sql)
        self.assertEqual(params, ("Undeclared", "Undeclared"))
        self.assertEqual(got, [{"direction": "out", "link_type": "Undeclared.ref",
                                "other_type": "Customer", "tier": "lead", "count": 3}])


class DictionaryThroughTheDoorTest(tcr.ChainRead):
    def _dictionary(self, actor=tcr.FULL):
        out = self.call({"describe": "dictionary"}, actor)
        self.assertNotIn("error", out, out)
        self.assertEqual(validate(out, ts._CONTRACT["output"], root=ev.CONTRACTS), [],
                         json.dumps(out)[:1500])
        return out

    def test_describe_type_includes_dictionary_with_links_and_counts(self):
        got = self.call({"describe": "MCTask"}, tcr.FULL)
        self.assertNotIn("error", got, got)
        d = got["dictionary"]
        self.assertEqual(d["type"], "MCTask")
        self.assertGreaterEqual(d["records"], 1)
        self.assertTrue(any(lk["link_type"] == "MCTask.about" and lk["tier"] == "corroborated"
                            and lk["count"] >= 1 for lk in d["links_with_counts"]),
                        d["links_with_counts"])
        self.assertTrue(any(lk["link_type"] == "MCTask.about" and lk["tier"] == "lead"
                            and lk["count"] >= 1 for lk in d["links_with_counts"]),
                        d["links_with_counts"])
        self.assertEqual(d["party_key"], "MCTask.about -> Customer")

        # party type leads with its own label field and shows both sides of self-links
        cust = self.call({"describe": "Customer"}, tcr.FULL)
        self.assertNotIn("error", cust, cust)
        cd = cust["dictionary"]
        self.assertEqual(cd["party_key"], "name")
        self.assertEqual(cd["party_keys"][0]["via"], "self_label")
        same_dirs = {(lk["direction"], lk["tier"]) for lk in cd["links_with_counts"]
                     if lk["link_type"] == "Customer.same_record_as"}
        self.assertEqual(same_dirs, {("out", "verified"), ("in", "verified"),
                                     ("out", "lead"), ("in", "lead")})

    def test_describe_dictionary_returns_all_types_in_one_pageable_sql_result(self):
        got = self._dictionary(tcr.FULL)
        self.assertTrue(got["result"].startswith("sql://"))
        self.assertIn("dictionary:", got["scope"])
        self.assertIsInstance(got.get("data_as_of"), dict)
        self.assertIn(got["data_as_of"].get("read_from"), ("primary", "standby"))
        self.assertTrue(got["data_as_of"].get("as_of"))

        rows = self.cells(got)
        by_type = {r["type"]: r for r in rows}
        self.assertIn("MCTask", by_type)
        self.assertEqual(by_type["MCTask"]["party_key"], "MCTask.about -> Customer")
        self.assertEqual(by_type["Customer"]["party_key"], "name")

        # tier is preserved in all_types and matches per-type describe
        mc_links = json.loads(by_type["MCTask"]["links_with_counts"])
        self.assertTrue(all("tier" in lk for lk in mc_links), mc_links)
        mc_tiers = {lk["tier"] for lk in mc_links if lk["link_type"] == "MCTask.about"}
        self.assertEqual(mc_tiers, {"corroborated", "lead"})
        mc_single = self.call({"describe": "MCTask"}, tcr.FULL)["dictionary"]["links_with_counts"]
        self.assertEqual(mc_links, mc_single)

        # respects per-reader door: NOBODY sees 0 records and 0 link counts
        nobody = self._dictionary(tcr.NOBODY)
        by_type_n = {r["type"]: r for r in self.cells(nobody)}
        self.assertEqual(int(by_type_n["MCTask"]["records"]), 0)
        self.assertEqual(json.loads(by_type_n["MCTask"]["links_with_counts"]), [])

    def test_ops_reader_excludes_finance_and_full_only_records_and_links(self):
        full_by = {r["type"]: r for r in self.cells(self._dictionary(tcr.FULL))}
        ops_by = {r["type"]: r for r in self.cells(self._dictionary(tcr.OPS))}

        # Customer: OPS excludes finance-marked Hidden Co but keeps operational customers
        self.assertEqual(int(ops_by["Customer"]["records"]),
                         int(full_by["Customer"]["records"]) - 1)
        self.assertGreater(int(ops_by["Customer"]["records"]), 0)

        # SalesInvoice.billed_to: OPS excludes the link to finance-marked Hidden Co
        full_inv = {(lk["direction"], lk["link_type"], lk["other_type"], lk["tier"]): lk["count"]
                    for lk in json.loads(full_by["SalesInvoice"]["links_with_counts"])}
        ops_inv = {(lk["direction"], lk["link_type"], lk["other_type"], lk["tier"]): lk["count"]
                   for lk in json.loads(ops_by["SalesInvoice"]["links_with_counts"])}
        key = ("out", "SalesInvoice.billed_to", "Customer", "verified")
        self.assertEqual(ops_inv[key], full_inv[key] - 1)
        self.assertGreaterEqual(ops_inv[key], 2)

        # Single-type describe matches all_types for OPS too
        ops_single = self.call({"describe": "SalesInvoice"}, tcr.OPS)
        self.assertNotIn("error", ops_single, ops_single)
        self.assertEqual(ops_single["dictionary"]["links_with_counts"],
                         json.loads(ops_by["SalesInvoice"]["links_with_counts"]))

        # Once people_excluded covers current versions, FULL_KEY non-business records
        # (SharePointListItem) and mail-evidenced links (MCTask.about) appear for FULL only.
        tgv.decide(self.dsn, None)
        try:
            full_dec = {r["type"]: r for r in self.cells(self._dictionary(tcr.FULL))}
            ops_dec = {r["type"]: r for r in self.cells(self._dictionary(tcr.OPS))}
            self.assertEqual(int(full_dec["SharePointListItem"]["records"]), 1)
            self.assertEqual(int(ops_dec["SharePointListItem"]["records"]), 0)
            full_mc = {(lk["direction"], lk["link_type"], lk["other_type"], lk["tier"]): lk["count"]
                       for lk in json.loads(full_dec["MCTask"]["links_with_counts"])}
            ops_mc = {(lk["direction"], lk["link_type"], lk["other_type"], lk["tier"]): lk["count"]
                      for lk in json.loads(ops_dec["MCTask"]["links_with_counts"])}
            mc_key = ("out", "MCTask.about", "Customer", "corroborated")
            self.assertEqual(ops_mc[mc_key], full_mc[mc_key] - 1)
            self.assertGreater(ops_mc[mc_key], 0)
        finally:
            tgv.decide(self.dsn, 0)

    def test_all_types_refuses_when_view_refused_and_gates_with_costly_slot(self):
        real_decide = ts._decide

        def refused_decide(pg, grant):
            dec = real_decide(pg, grant)
            return {**dec, "refused": set(dec["refused"]) | {"record_links"}}

        with mock.patch.object(ts, "_decide", side_effect=refused_decide):
            out = self.call({"describe": "dictionary"}, tcr.FULL)
        self.assertEqual(out["error"]["code"], "not_ready")
        self.assertIn("record_links", out["error"]["message"])

        real_slot = ts._costly_slot
        attempts = {"n": 0}

        def slot_once_busy(pg):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ts._NoSlot()
            return real_slot(pg)

        with mock.patch.object(ts, "_costly_slot", side_effect=slot_once_busy), \
                mock.patch.object(ts, "FULL_COSTLY_POLL_SECONDS", 0.01):
            out = self._dictionary(tcr.FULL)
        self.assertEqual(attempts["n"], 2)
        self.assertTrue(out["result"].startswith("sql://"))


if __name__ == "__main__":
    unittest.main()
