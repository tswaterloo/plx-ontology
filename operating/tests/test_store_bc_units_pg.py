"""``ontology_host_bc.store_bc_units`` on Postgres (isolated fixture: an ephemeral Postgres
store loaded from a SQLite fixture, ``pg_store_mode``).

The BC units a store holds are read by a skip scan of the handle index for keys named as
the BC projection names them (``bc:<company>/<entitySet>``), and by the grouped read of every
BC version for any other key. Both give what the grouped read gives: a unit with BC versions
under its own prefix (any letter case of the scheme, any environment: the least handle's), a
unit whose objects have no BC version is never one, and keys of either shape side by side.
"""

from __future__ import annotations

import unittest

from operating import ontology_host_bc as hb
from operating.tests import test_ontology_tools_table as tab


def _old(reader) -> dict[str, str]:
    """The grouped read of every BC version (the previous query, every key)."""
    sql = ("SELECT o.audience_key, MIN(v.l1_handle) FROM object_versions v JOIN objects o "
           "ON o.object_id = v.object_id WHERE lower(substr(o.audience_key, 1, 3)) = 'bc:' "
           f"AND ({hb._BC_HANDLE_RANGES}) GROUP BY o.audience_key")
    with reader.lock:
        return {k: h[len("bc://"):].split("/", 1)[0]
                for k, h in reader.conn.execute(sql).fetchall()}


class StoreBcUnitsPg(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from operating.pg import ontology_reader
        from operating.tests import pg_store_mode
        s = tab.Store()
        n = 0

        def add(key, handle):
            nonlocal n
            n += 1
            s.obj(f"Customer:bc.customers:{n}", key, handle, "gold_etl_from_fm")

        for i in range(60):   # a unit of many versions
            add("bc:co1/customers", f"bc://production/co1/customers/c{i:03d}@g-{i}")
        add("bc:co1/vendors", "BC://production/co1/vendors/v1@g-1")         # scheme case
        add("bc:co2/items", "bc://sandbox/co2/items/i1@g-1")
        add("bc:co2/items", "bc://production/co2/items/i2@g-1")              # two envs
        add("bc:co3/orders", "api://portal.orders/o1@g-1")                   # no BC version
        add("bc:accounts", "bc://synthetic/co9/accounts/a1@g-1")             # other shape
        add("api:public.x", "bc://production/co4/ledger/l1@g-1")             # not a BC key
        # another key's object under co1/customers' prefix, in an environment sorting first:
        # never co1/customers' environment (the prefix counts only its own key's versions)
        add("api:public.y", "bc://aaa/co1/customers/z@g-1")
        # a unit whose only BC version sits under another unit's prefix: the grouped read's
        add("bc:co7/orders", "bc://production/co1/customers/q@g-1")
        add("bc:co5/short", "bc://production/co5/short@g-1")                 # no path after
        s.conn.commit()
        cls.store = s
        cls.dsn = pg_store_mode._load("", s.conn)
        cls.reader = ontology_reader.PgStoreReader.open(cls.dsn)

    @classmethod
    def tearDownClass(cls):
        cls.reader.close()

    def test_units_equal_the_grouped_read(self):
        got = hb.store_bc_units(self.reader)
        self.assertEqual(got, _old(self.reader))
        self.assertEqual(got, {"bc:co1/customers": "production", "bc:co1/vendors": "production",
                               "bc:co2/items": "production", "bc:accounts": "synthetic",
                               "bc:co7/orders": "production", "bc:co5/short": "production"})
        self.assertNotIn("bc:co3/orders", got)

    def test_the_skip_scan_finds_the_keys_it_can_and_names_no_other(self):
        keys = [k for k in self.reader.audience_keys() if hb._UNIT.fullmatch(k)]
        fast = hb._pg_bc_units(self.reader, keys)
        # found through their own prefixes, the least handle's environment of the key's own
        # objects (co2/items: production before sandbox; co1/customers: not api:public.y's)
        self.assertEqual(fast, {"bc:co1/customers": "production", "bc:co1/vendors": "production",
                                "bc:co2/items": "production"})
        # only keys asked for: the bc://.../co4/ledger prefix names no kept key
        self.assertEqual(hb._pg_bc_units(self.reader, ["bc:co1/vendors"]),
                         {"bc:co1/vendors": "production"})


if __name__ == "__main__":
    unittest.main()
