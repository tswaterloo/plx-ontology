import unittest
from unittest import mock

from operating.pg import comm_links


class _Conn:
    def __init__(self, lags):
        self.lags = list(lags)

    def execute(self, *_a, **_k):
        outer = self

        class R:
            def fetchone(self):
                return (outer.lags.pop(0) if len(outer.lags) > 1 else outer.lags[0],)
        return R()


class AddressIdentity(unittest.TestCase):
    def setUp(self):
        comm_links._CACHE.update(at=0.0, ident={})

    def parties(self, ident, own=()):
        with mock.patch.object(comm_links, "identities", return_value=ident), \
                mock.patch.object(comm_links, "_own_addresses", return_value=set(own)):
            return comm_links.address_parties(None)

    def test_one_holder_links_public_domain_exact_only(self):
        got = self.parties({"a@acme.com": {("customer", "c1", "Acme")},
                            "bob@gmail.com": {("customer", "c2", "Bob Wash")}})
        self.assertEqual(set(got), {"a@acme.com", "bob@gmail.com"})

    def test_ambiguous_and_conflicting_domains_and_own_links_nothing(self):
        got = self.parties({
            "x@both.com": {("customer", "c1", "Alpha"), ("supplier", "s1", "Beta")},
            "y@agency.com": {("customer", "c3", "Gamma")},
            "z@agency.com": {("customer", "c4", "Delta")},
            "me@plx.com": {("customer", "c5", "Epsilon")}}, own={"me@plx.com"})
        self.assertEqual(got, {})

    def test_same_party_two_systems_keeps_all_holders(self):
        got = self.parties({"a@acme.com": {("customer", "c1", "Acme Inc"),
                                            ("customer", "c9", "ACME Inc.")}})
        self.assertEqual(got["a@acme.com"], {("customer", "c1"), ("customer", "c9")})


class PublicDomain(AddressIdentity):
    def test_public_domain_links_by_exact_address_only(self):
        got = self.parties({"a@gmail.com": {("customer", "c1", "Alpha Wash")},
                            "b@gmail.com": {("customer", "c2", "Beta Wash")},
                            "c@gmail.com": {("customer", "c3", "Gamma"),
                                            ("supplier", "s3", "Delta")}})
        self.assertEqual(got, {"a@gmail.com": {("customer", "c1")},
                               "b@gmail.com": {("customer", "c2")}})

    def test_private_domain_of_two_parties_links_no_address(self):
        got = self.parties({"a@shared.com": {("customer", "c1", "Alpha")},
                            "b@shared.com": {("customer", "c2", "Beta")}})
        self.assertEqual(got, {})


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, *_a):
        return iter(self.rows.get(("customer" if "customer_handle" in sql else "supplier"), []))


class NamedRecordConfidence(unittest.TestCase):
    def run_rule(self, customers=(), suppliers=()):
        rows = {"customer": [("m", c) for c in customers],
                "supplier": [("m", s) for s in suppliers]}
        # the rule queries sales/invoice tables (customer) then PO tables (supplier)
        with mock.patch.object(_Rows, "execute", autospec=True,
                               side_effect=lambda self, sql, *a: (
                                   mock.Mock(fetchone=lambda: (2,)) if "information_schema" in sql
                                   else iter(rows["customer" if "customer_handle" in sql
                                                  else "supplier"]))):
            return comm_links.named_records(_Rows({}), "biz.emails", "true")

    def test_one_party_high(self):
        got = self.run_rule(customers=["c1"])
        self.assertEqual(got["m"], {("customer", "c1"): "high"})

    def test_two_or_three_parties_medium(self):
        got = self.run_rule(customers=["c1", "c2"], suppliers=["s1"])
        self.assertEqual(set(got["m"].values()), {"medium"})
        self.assertEqual(len(got["m"]), 3)

    def test_more_than_three_parties_links_nothing(self):
        self.assertEqual(self.run_rule(customers=["c1", "c2", "c3", "c4"]), {})


class MissingHandles(unittest.TestCase):
    def test_table_without_handles_names_no_party(self):
        c = mock.Mock()
        c.execute.return_value.fetchone.return_value = (1,)
        self.assertEqual(comm_links.named_records(c, "biz.emails", "true"), {})


class _Rec:
    def __init__(self):
        self.sql = []

    def execute(self, sql, *a, **k):
        self.sql.append(sql)
        r = mock.Mock()
        r.rowcount = 1
        r.fetchone.return_value = (0,)
        return r

    def cursor(self):
        cur = mock.MagicMock()
        cur.__enter__.return_value = cur
        cur.copy.return_value.__enter__.return_value = mock.Mock()
        return cur

    def transaction(self):
        return mock.MagicMock()


class WriteLinks(unittest.TestCase):
    def test_message_table_never_updated_and_both_doors_carried(self):
        c = _Rec()
        with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=200e9)):
            comm_links.write_links(c, "emails", {("o1", "customer", "c1"): ("address", "high")})
        sql = "\n".join(c.sql)
        self.assertNotIn("UPDATE biz.emails", sql)
        self.assertIn("UNLOGGED", sql)
        self.assertIn("NOT EXISTS", sql)          # links no longer derived are removed
        for col in ("audience_keys", "rec_marks", "col_marks", "party_access_class",
                    "party_col_class", "party_ai_ok"):
            self.assertIn(col, sql)


class WriteGuard(unittest.TestCase):
    def test_disk_below_floor_stops(self):
        with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=50e9)):
            with self.assertRaises(RuntimeError):
                comm_links.guard(_Conn([0]), path="/x")

    def test_standby_lag_pauses_until_resume_level(self):
        waits = []
        with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=200e9)):
            comm_links.guard(_Conn([9e9, 9e9, 5e9, 1e9]), path="/x", sleep=waits.append)
        self.assertEqual(len(waits), 2)

    def test_small_lag_does_not_wait(self):
        waits = []
        with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=200e9)):
            comm_links.guard(_Conn([3e9]), path="/x", sleep=waits.append)
        self.assertEqual(waits, [])

    def test_the_floor_is_read_on_the_databases_own_disk(self):
        """A build copy on a Spark has no /data: the floor is read where the data lives."""
        seen = []
        with mock.patch("operating.pg.business_comms.disk_path", return_value="/spark/pg"), \
                mock.patch("shutil.disk_usage",
                           side_effect=lambda p: seen.append(p) or mock.Mock(free=200e9)):
            comm_links.guard(_Conn([0]), sleep=lambda s: None)
        self.assertEqual(seen, ["/spark/pg"])

    def test_an_expected_standby_slot_gone_waits_instead_of_reading_zero(self):
        with mock.patch.dict("os.environ", {"PLX_WAL_STANDBY_SLOTS": "plx_s7d3d"}), \
                mock.patch("operating.pg.wal_budget.missing_slots", return_value=["plx_s7d3d"]):
            self.assertEqual(comm_links.lag_gb(_Conn([0])), float("inf"))


if __name__ == "__main__":
    unittest.main()
