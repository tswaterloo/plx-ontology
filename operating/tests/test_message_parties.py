"""Messages -> customers/suppliers by participant domains and party names: the rules at the
boundary (``PartyIndex`` over party rows), never one test per helper."""
import unittest
from unittest import mock

from operating.pg import message_parties as mp


def party(kind, handle, name, email=None, website=None, active=True):
    return {"kind": kind, "handle": handle, "name": name, "email": email, "website": website,
            "active": active}


PARTIES = [
    party("customer", "c1", "Acme Foods Inc.", "ap@acmefoods.com"),
    party("customer", "c2", "Acme Foods Ltd", "ap@acmefoods.com"),     # same name: one party
    party("customer", "c3", "Northwind Spa", "x@shared-agency.com"),
    party("supplier", "s1", "Brightline Chemicals", None, "https://www.brightline.ca/x"),
    party("supplier", "s2", "Other Agency", "y@shared-agency.com"),     # two parties: ambiguous
    party("customer", "c4", "Gmail User", "someone@gmail.com"),
    party("customer", "c5", "Ours Customer", "q@ourco.com"),
    party("customer", "c6", "Sunrise", None),
    party("customer", "c7", "Sunrise Foods", None),
    party("customer", "c8", "Unused Name Corp", None, active=False),
    party("customer", "c9", "Venessa Bandiera", None),
    party("customer", "c10", "For and Against", None),
    party("customer", "c11", "Zorbex", None),
    party("customer", "c12", "Express Wash Co", None),
    party("customer", "c13", "Royal Coach Express Wash Co", None),
    party("customer", "c14", "Staff Person Customer", "someone@ourco.com"),
    {"people": ["Venessa Bandiera"]},
]


class DomainRules(unittest.TestCase):
    def setUp(self):
        self.ix = mp.PartyIndex(PARTIES, own_domains=["ourco.com"])

    def test_one_party_per_domain_and_same_name_rows_are_one_party(self):
        got = self.ix.parties_of_domains(["mail.acmefoods.com"])    # a subdomain of a held domain
        self.assertEqual(got, {("customer", "c1"), ("customer", "c2")})
        self.assertEqual(self.ix.parties_of_domains(["brightline.ca"]), {("supplier", "s1")})

    def test_ambiguous_shared_and_own_domains_link_nothing(self):
        for d in ("shared-agency.com", "gmail.com", "ourco.com", "unknown-domain.org"):
            self.assertEqual(self.ix.parties_of_domains([d]), set(), d)
        self.assertEqual(self.ix.ambiguous_domains, 1)


class RedoCli(unittest.TestCase):
    def run_cli(self, got_lock):
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.execute.return_value.fetchone.return_value = (got_lock,)
        with mock.patch("psycopg.connect", return_value=conn), \
                mock.patch.object(mp, "redo_batched", return_value={"rows_updated": 0}) as rb, \
                mock.patch("builtins.print"):
            code = mp.main(["redo", "--view", "emails"])
        return code, rb, conn

    def test_a_hand_redo_never_overlaps_a_views_refresh(self):
        from operating.pg import business_views as bv
        code, rb, conn = self.run_cli(False)
        self.assertEqual((code, rb.called), (2, False))
        self.assertEqual(conn.execute.call_args[0][1], (bv.REFRESH_LOCK,))
        code, rb, _ = self.run_cli(True)
        self.assertEqual((code, rb.called), (0, True))


class NameRules(unittest.TestCase):
    def setUp(self):
        self.ix = mp.PartyIndex(PARTIES, own_domains=["ourco.com"])

    def test_whole_names_found_legal_suffix_and_case_ignored(self):
        self.assertEqual(self.ix.names_in("Call with ACME foods, Zorbex tomorrow"),
                         {"acme foods", "zorbex"})

    def test_names_that_say_nothing_are_not_usable(self):
        usable = set(self.ix.names)
        self.assertNotIn("unused name", usable)          # no business behind it
        self.assertNotIn("venessa bandiera", usable)     # one of our own people
        self.assertNotIn("for and against", usable)      # function words
        self.assertNotIn("sunrise", usable)              # starts another party's name
        self.assertIn("sunrise foods", usable)
        self.assertEqual(self.ix.names_in("Sunrise is closed"), set())
        self.assertEqual(self.ix.names_in("Acme"), set())    # half a name

    def test_the_longest_name_wins_where_a_shorter_one_sits_inside_it(self):
        self.assertEqual(self.ix.names_in("Royal Coach Express Wash Co order"),
                         {"royal coach express wash"})
        self.assertEqual(self.ix.names_in("Express Wash Co and Royal Coach Express Wash Co"),
                         {"express wash", "royal coach express wash"})

    def test_a_party_row_kept_under_our_own_address_names_nobody(self):
        self.assertNotIn("staff person customer", self.ix.names)

    def test_quoted_history_of_a_chat_message_is_not_its_text(self):
        own = mp.own_text("see Acme Foods\nOn Mon, X wrote:\n> Zorbex stuff")
        self.assertIn("Acme Foods", own)
        self.assertNotIn("Zorbex", own)
        self.assertEqual(mp.own_text("plain Zorbex"), "plain Zorbex")

    def test_a_common_name_dropped_from_the_index_is_no_longer_found(self):
        self.assertEqual(self.ix.names_in("Zorbex and Acme Foods"), {"zorbex", "acme foods"})
        self.ix.drop(["zorbex"])
        self.assertEqual(self.ix.names_in("Zorbex and Acme Foods"), {"acme foods"})
        self.assertEqual(self.ix.max_len, 4)

    def test_a_name_in_too_many_messages_is_dropped_and_reported(self):
        texts = [(f"o{i}", "zorbex") for i in range(mp.MAX_NAME_HITS + 1)] + \
                [("x1", "acme foods order")]
        with mock.patch.object(mp, "_texts", return_value=iter(texts)):
            hits, dropped = mp.name_hits(mock.Mock(), self.ix, "teams_messages", "t", "true")
        self.assertEqual(dropped, {"zorbex": mp.MAX_NAME_HITS + 1})
        self.assertEqual(hits, {"x1": {("customer", "c1"), ("customer", "c2")}})

    def test_names_a_full_pass_dropped_stay_out_of_an_incremental_pass(self):
        idx = self.ix
        with mock.patch.object(mp, "name_hits", return_value=({}, {})) as nh, \
                mock.patch.object(mp, "load_parties", return_value=(PARTIES, ["ourco.com"])), \
                mock.patch.object(mp, "common_names", return_value=frozenset()), \
                mock.patch("operating.pg.comm_links.infer", return_value={}):
            mp.infer(mock.Mock(), "teams_messages", "t", "true", drop_names=["zorbex"])
        self.assertNotIn("zorbex", nh.call_args[0][1].names)
        self.assertIn("zorbex", idx.names)


if __name__ == "__main__":
    unittest.main()
