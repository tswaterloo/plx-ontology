"""Which party rows are one business (``party_keys``) and who a payable is owed to
(``payee_class``): the rules on made-up rows and a made-up GL chart.

Evidence label: ``isolated-fixture`` (made-up parties, vendors and accounts)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from operating.pg import business_views as bv
from operating.pg import party_keys as pk
from operating.pg import payee_class as pc


def party(handle, name, email=None, website=None):
    return {"handle": handle, "name": name, "email": email, "website": website}


class PartyKeys(unittest.TestCase):
    def test_same_business_by_name_as_people_write_it(self):
        got = pk.groups([party("a", "NORTHWIND CONTAINERS"), party("b", "Northwind Containers"),
                         party("c", "Northwind Containers Inc. (EFT)"),
                         party("d", "NORTHWIND CONTAINERS - EFT"),
                         party("e", "Northwind Chemicals")])
        self.assertEqual(got["a"], ["a", "b", "c", "d"])
        self.assertEqual(got["c"], ["a", "b", "c", "d"])
        self.assertEqual(got["e"], ["e"])

    def test_same_domain_only_with_a_shared_word(self):
        got = pk.groups([party("a", "Bluefin Labs", "orders@bluefin-labs.example"),
                         party("b", "BLUEFIN LABORATORIES US", None, "www.bluefin-labs.example"),
                         # a factor writing from its client's domain: no shared word
                         party("c", "Harbor Capital Funding", "ar@bluefin-labs.example"),
                         # a shared mail provider joins nothing
                         party("d", "Bluefin Labs West", "bluefinwest@gmail.com"),
                         party("e", "Bluefin Retail", "x@gmail.com")])
        self.assertEqual(got["a"], ["a", "b"])
        self.assertEqual(got["c"], ["c"])
        self.assertEqual(got["d"], ["d"])
        self.assertEqual(got["e"], ["e"])

    def test_fails_closed_on_generic_busy_or_missing(self):
        rows = [party("g1", "Misc Supplier"), party("g2", "MISC SUPPLIER"),
                party("n1", None), party("n2", None)]
        # a domain under more than MAX_DOMAIN_NAMES names: a parent or our own
        rows += [party(f"o{i}", f"Acme {w} Store", f"s{i}@acme-owner.example")
                 for i, w in enumerate(("North", "South", "East", "Lake"))]
        # a name more than MAX_GROUP rows carry is a placeholder
        rows += [party(f"p{i}", "Walk In Customer Trade") for i in range(pk.MAX_GROUP + 1)]
        got = pk.groups(rows)
        for h in ("g1", "g2", "n1", "n2", "o0", "o1", "p0"):
            self.assertEqual(got[h], [h], h)

    def test_cardinality(self):
        for n in (1, 10, 60):
            rows = [party(f"h{i:03d}", f"Vendor {i:03d} Ltd") for i in range(n)]
            rows += [party(f"t{i:03d}", f"VENDOR {i:03d} LTD.") for i in range(n)]
            got = pk.groups(rows)
            self.assertEqual(len(got), 2 * n)   # no row lost or doubled
            self.assertTrue(all(len(v) == 2 for v in got.values()), n)

    def test_the_column_reads_its_twins_records(self):
        rows = []
        for handle, oid, name in (("h1", "Supplier:1", "Atlas Boxes"),
                                  ("h2", "Supplier:2", "ATLAS BOXES"),
                                  ("h3", "Supplier:3", "Other Co")):
            row = bv.Row()
            row.handle, row.objects = handle, [oid]
            row.values.update(name=name, email=None)
            rows.append(row)
        pk.fill(rows)
        self.assertEqual(rows[0].values["party_handles"], ["h1", "h2"])
        self.assertEqual(set(rows[0].col_reads["party_handles"]), {"Supplier:2"})
        self.assertEqual(rows[2].values["party_handles"], ["h3"])
        self.assertNotIn("party_handles", rows[2].col_reads)
        for name in ("suppliers", "customers"):
            cols = {c.name: c for c in bv.VIEWS[name].cols}
            self.assertEqual(cols["party_handles"].kind, "list")
            self.assertIn("ANY(party_handles)", cols["party_handles"].doc)


def rec(oid, **cells):
    return SimpleNamespace(object_id=oid, cell=lambda p: (
        SimpleNamespace(value=cells[p]) if p in cells else None))


class PayeeClass(unittest.TestCase):
    CHART = pc.chart(
        {"a1": rec("acct:1", acctfmttd="41100-3000-01", acctgrpcod="36"),
         "a2": rec("acct:2", acctfmttd="21140-01", acctgrpcod="10 "),
         "a3": rec("acct:3", acctfmttd="42216-01", acctgrpcod="33"),
         "a4": rec("acct:4", acctid="11160 01", acctgrpcod="01"),
         "a5": rec("acct:5", acctfmttd="15000-01", acctgrpcod="05"),
         "a6": rec("acct:6", acctfmttd="40000-01", acctgrpcod="15"),
         "a7": rec("acct:7", acctfmttd="99999-01", acctgrpcod="77")},
        {"g36": rec("grp:36", acctgrpcod="36", grpcod="150"),
         "g10": rec("grp:10", acctgrpcod="10", grpcod="90"),
         "g33": rec("grp:33", acctgrpcod="33", grpcod="170"),
         "g01": rec("grp:01", acctgrpcod="01", grpcod="10"),
         "g05": rec("grp:05", acctgrpcod="05", grpcod="50"),
         "g15": rec("grp:15", acctgrpcod="15", grpcod="140")})

    def test_by_the_account_sage_posts_the_vendor_to(self):
        for gl, want in (("41100-3000-01", "trade"), ("21140-01", "tax_payroll"),
                         ("42216-01 ", "expense"), ("11160-01", "finance"),
                         ("15000-01", "capital"), ("40000-01", "other")):
            cls, account, group = pc.classify(rec("v", glaccnt=gl), self.CHART)
            self.assertEqual(cls, want, gl)
            self.assertIsNotNone(account)
            self.assertIsNotNone(group)

    def test_unknown_when_sage_states_none(self):
        for vendor in (rec("v"), rec("v", glaccnt=""), rec("v", glaccnt="55555-01"),
                       rec("v", glaccnt="99999-01")):   # an account whose group is gone
            self.assertEqual(pc.classify(vendor, self.CHART)[0], "unknown")
        self.assertEqual(set(pc.CATEGORY_CLASS.values()) | {"other", "unknown"},
                         set(pc.CLASSES))

    def test_the_payables_views_carry_it(self):
        for name in ("supplier_balances", "supplier_invoices"):
            view = bv.VIEWS[name]
            self.assertIn("payee_class", [c.name for c in view.cols])
            self.assertTrue({"RelSageGlAccount", "RelSageGlAcgrp"} <= set(view.reads))
            self.assertIn("gl_chart", view.memos)


if __name__ == "__main__":
    unittest.main()
