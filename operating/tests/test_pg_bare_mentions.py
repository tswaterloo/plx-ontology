"""Row 14: bare identifiers in mail as lead mention links beside the served links."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from ontology.schema import load_schema
from operating.pg import bare_mentions as BM
from operating.pg.sqlite_compat import PgCompatConnection

OWN = BM.own_words()


class FakeConn:
    """The three reads ``mail_rows`` makes, over in-memory rows."""

    def __init__(self, versions, identifiers, served=(), twins=()):
        self.versions, self.identifiers, self.served = versions, identifiers, set(served)
        self.twins = list(twins)
        self.sql: list[str] = []

    def execute(self, sql, params):
        self.sql.append(sql)
        if "object_versions" in sql:
            rows = [v for v in self.versions if v[1] in params[0]]
        elif "same_record" in sql:
            rows = [(a, b) for a, b, tier in self.twins
                    if tier in params[0] and (a in params[1] or b in params[2])]
        elif "ontology.identifiers" in sql:
            rows = [(r + ("l", "h"))[:5] for r in self.identifiers   # lineage, handle
                    if r[1] in params[0] and r[0] in params[1]]
        else:
            rows = [p for p in self.served if p[0] in params[0]]
        return type("R", (), {"fetchall": lambda self_: list(rows)})()


class Candidates(unittest.TestCase):
    def test_closed_shapes(self):
        text = ("RE: winn01 order\nItems MC4206, BR900 and FT600 - 4 cases of BR100.\n"
                "Packing Slip #1000089711; PO#3976; qty 12500; 2024-05-01; 905-555-1234\n"
                "see http://www.example.com/BR950.jpg or mail ab12345@example.com\n"
                "Your Company: [PETRA-1] PetrA1 X12 TWT15x25")
        got = BM.candidates(text, OWN)
        self.assertEqual(got, {"WINN01", "MC4206", "BR900", "FT600", "BR100",
                               "1000089711", "TWT15X25"})

    def test_short_or_letterless_never(self):
        self.assertEqual(BM.candidates("1234567 2023 12-34-56 A1B2 ----", OWN), set())

    def test_dash_suffix_offers_the_longer_code(self):
        self.assertEqual(BM.candidates("Price AC600 \u2013 NF and AC600 - NF, AC600-NF", OWN),
                         {"AC600-NF|AC600", "AC600-NF"})
        self.assertEqual(BM.candidates("AC60 \u2014 NF; MC4206 - BR900; 2024 - 05", OWN),
                         {"AC60-NF", "MC4206-BR900|MC4206|BR900"})

    def test_a_quantity_after_the_dash_is_never_a_suffix(self):
        # live: MC4015-2 is an item code of its own; "2 cases" of MC4015 must not name it
        for text in ("MC4015 - 2 cases", "MC4015 \u2013 2 pcs.", "MC4015 - 12 x 4",
                     "send MC4015 - 2 Cases today"):
            with self.subTest(text=text):
                self.assertEqual(BM.candidates(text, OWN), {"MC4015"})
        self.assertEqual(BM.candidates("MC4015 - 2, thanks", OWN), {"MC4015-2|MC4015"})
        self.assertEqual(BM.candidates("AC600 - NF cases", OWN), {"AC600-NF|AC600"})


class Rows(unittest.TestCase):
    MAIL = "Email:graph_mail.messages:{\"id\":\"m1\"}"

    def conn(self, served=(), holders=1, twins=()):
        ids = [("customer_erp_code", "WINN01", f"Customer:portal:{i}") for i in range(holders)]
        ids += [("customer_erp_code", "WINN01", "SalesInvoice:portal:9"),   # wrong type
                ("item_code", "MC4206", "Product:portal:1"),
                ("sage_ar_document", "IN1000118446", "RelSageArOpenDocument:sage:1")]
        return FakeConn([(self.MAIL, "h1", '["finance"]'), ("Attachment:x", "h1", "[]")],
                        ids, served, twins)

    def test_rows_carry_mail_markings_and_skip_served(self):
        mails = [{"handle": "h1", "text": "RE: WINN01 MC4206 IN1000118446"}]
        rows = BM.mail_rows(self.conn(served=[(self.MAIL, "Product:portal:1")]), mails, OWN,
                            "2026-11-15T00:00:00Z")
        self.assertEqual([(r["link_type"], r["to_object"]) for r in rows],
                         [("Email.mentions_customer", "Customer:portal:0")])
        row = rows[0]
        self.assertEqual((row["from_object"], row["markings"], row["tier"],
                          row["binding_kind"], json.loads(row["evidence_handles"])),
                         (self.MAIL, '["finance"]', "lead", "mention", ["h1"]))
        self.assertTrue(row["link_id"].startswith("bare:"))

    def test_several_holders_need_confirmed_twins_and_a_cap(self):
        mails = [{"handle": "h1", "text": "WINN01"}]
        chain = [(f"Customer:portal:{i}", f"Customer:portal:{i + 1}", "verified")
                 for i in range(BM.MAX_HOLDERS)]
        self.assertEqual(len(BM.mail_rows(self.conn(holders=BM.MAX_HOLDERS, twins=chain),
                                          mails, OWN, "t")), BM.MAX_HOLDERS)
        self.assertEqual(BM.mail_rows(self.conn(holders=BM.MAX_HOLDERS + 1, twins=chain),
                                      mails, OWN, "t"), [])
        stats: dict = {}
        self.assertEqual(BM.mail_rows(self.conn(holders=2), mails, OWN, "t", stats), [])
        self.assertEqual(stats, {"ambiguous": 1})
        lead = [("Customer:portal:0", "Customer:portal:1", "lead")]
        self.assertEqual(BM.mail_rows(self.conn(holders=2, twins=lead), mails, OWN, "t"), [])

    def test_longest_known_code_wins_and_never_the_shorter(self):
        ids = [("erp_item_code", "AC600", "Product:portal.sage_ic_item:1", "sage_bronze"),
               ("erp_item_code", "AC600-NF", "Product:portal.sage_ic_item:2", "sage_bronze")]
        conn = FakeConn([(self.MAIL, "h1", "[]")], ids)
        to = lambda text: sorted(r["to_object"] for r in BM.mail_rows(
            conn, [{"handle": "h1", "text": text}], OWN, "t"))
        for text in ("Quote AC600 \u2013 NF please", "AC600 - NF", "AC600\u2013NF"):
            self.assertEqual(to(text), ["Product:portal.sage_ic_item:2"], text)
        self.assertEqual(to("AC600 - XX"), ["Product:portal.sage_ic_item:1"])
        self.assertEqual(to("AC600 - NF and also AC600 alone"),
                         ["Product:portal.sage_ic_item:1", "Product:portal.sage_ic_item:2"])
        # the longer code known but ambiguous still never falls back to the shorter
        ids.append(("erp_item_code", "AC600-NF", "Product:portal.sage_ic_item:3",
                    "sage_bronze"))
        self.assertEqual(to("AC600 - NF"), [])

    def test_one_holder_per_source_system_links_all(self):
        mails = [{"handle": "h1", "text": "WINN01"}]
        bc = "bc://production/{}/customers/{}"
        across = [("customer_erp_code", "WINN01", 'Customer:bc.customers:{"id":"a"}',
                   "bc_manufacturing", bc.format("co1", "a")),
                  ("customer_erp_code", "WINN01", 'Customer:bc.customers:{"id":"b"}',
                   "bc_distribution", bc.format("co2", "b")),
                  ("customer_erp_code", "WINN01", "Customer:portal.app_customer:c",
                   "gold_etl_from_sage", "api://public.app_customer/c"),
                  ("customer_erp_code", "WINN01", "Customer:portal.sage_ar_customer:d",
                   "sage_bronze", "api://sage.ar_customer/d")]
        conn = lambda ids, twins=(): FakeConn([(self.MAIL, "h1", "[]")], ids, (), twins)
        got = BM.mail_rows(conn(across), mails, OWN, "t")
        self.assertEqual(sorted(r["to_object"] for r in got), sorted(i[2] for i in across))
        same_lineage = across + [("customer_erp_code", "WINN01", "Customer:portal.x:e",
                                  "sage_bronze", "api://x/e")]
        same_company = across[:1] + [("customer_erp_code", "WINN01",
                                      'Customer:bc.customers:{"id":"f"}', "bc_other",
                                      bc.format("co1", "f"))]
        no_lineage = across[:1] + [("customer_erp_code", "WINN01", "Customer:p.y:g", "",
                                    "api://y/g")]
        for ids in (same_lineage, same_company, no_lineage):
            stats: dict = {}
            self.assertEqual(BM.mail_rows(conn(ids), mails, OWN, "t", stats), [], ids[-1])
            self.assertEqual(stats, {"ambiguous": 1})
        # two holders of one system that are confirmed twins: still one record, all link
        twin = [(across[3][2], same_lineage[-1][2], "verified")]
        self.assertEqual(len(BM.mail_rows(conn(same_lineage, twin), mails, OWN, "t")), 5)
        # a customer code that is also an item code, each in another system: ambiguous
        mixed = across[:1] + [("item_code", "WINN01", "Product:portal.sage_ic_item:z",
                               "sage_bronze", "api://sage.ic_item/z")]
        self.assertEqual(BM.mail_rows(conn(mixed), mails, OWN, "t"), [])

    def test_shipped_tokens_link_as_the_text_does(self):
        text = "RE: WINN01 MC4206 IN1000118446"
        by_text = BM.mail_rows(self.conn(), [{"handle": "h1", "text": text}], OWN, "t")
        by_tokens = BM.mail_rows(self.conn(), [{"handle": "h1", "tokens": sorted(
            BM.candidates(text, OWN))}], OWN, "t")
        self.assertEqual(by_tokens, by_text)
        self.assertEqual(len(by_text), 2)

    def test_mail_not_served_gets_nothing(self):
        mails = [{"handle": "unknown", "text": "WINN01"}]
        self.assertEqual(BM.mail_rows(self.conn(), mails, OWN, "t"), [])

    def test_excluded_mailbox_never_read(self):
        class Excl:
            def is_excluded(self, mailbox):
                return mailbox == "x"

        class Store:
            class db:
                @staticmethod
                def execute(sql, params):
                    return type("R", (), {"fetchall": lambda s: [
                        {"mailbox_id": "x", "l1_handle": "hx", "record": "{}"},
                        {"mailbox_id": "y", "l1_handle": "hy",
                         "record": json.dumps({"subject": "WINN01"})}]})()

            def body_text(self, row):
                if row["mailbox_id"] == "x":
                    raise AssertionError("excluded body read")
                return ""

        self.assertEqual([m["handle"] for m in BM.read_mails(Store(), Excl(), "1", ())],
                         ["hy"])


class Serving(unittest.TestCase):
    def test_link_types_are_the_schemas(self):
        BM.check_schema(load_schema().link_types)
        with self.assertRaises(SystemExit):
            BM.check_schema({"Email.mentions_customer": {"from": "Email", "to": "Supplier"}})

    def test_search_path_by_switches(self):
        cases = {(): "ontology, public",
                 ("PLX_PG_MENTION_REPEATS",): "ontology_seen, ontology, public",
                 ("PLX_PG_BARE_MENTIONS",): "ontology_bare, ontology, public",
                 ("PLX_PG_BARE_MENTIONS", "PLX_PG_MENTION_REPEATS"):
                     "ontology_bare_seen, ontology, public"}
        for on, want in cases.items():
            path = BM.search_path({k: "1" for k in on})
            self.assertEqual(path, want)
            try:   # the path is accepted; the connect to no database then fails
                PgCompatConnection("postgresql:///plx_no_such_db", search_path=path)
            except ValueError:
                self.fail(f"search_path refused: {path}")
            except Exception:
                pass


class PgTests(unittest.TestCase):
    """build / refresh / the twin rule on a throwaway database, over a mail store in
    SQLite (the build's own reads, the migration's table)."""

    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_testdb
        from operating.pg import migrate
        cls.pg_testdb = pg_testdb
        cls.name, cls.dsn = pg_testdb.create_db()
        with pg_testdb.psycopg.connect(cls.dsn, autocommit=True) as conn:
            migrate.migrate(conn)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store_path = os.path.join(cls.tmp.name, "mail.db")

    @classmethod
    def tearDownClass(cls):
        cls.pg_testdb.drop_db(cls.name)
        cls.tmp.cleanup()

    def setUp(self):
        self.ver = 0
        with self.conn() as c:
            for t in ("object_versions", "identifiers", "link_rows", "bare_mentions",
                      "change_feed", "change_cycles"):
                c.execute(f"TRUNCATE ontology.{t}")
        db = sqlite3.connect(self.store_path)
        db.execute("DROP TABLE IF EXISTS messages")
        db.execute("CREATE TABLE messages (version_id INTEGER PRIMARY KEY, l1_handle TEXT, "
                   "mailbox_id TEXT, record TEXT, body TEXT, superseded_at TEXT, "
                   "removed_reason TEXT)")
        db.commit()
        db.close()
        excluded = self.excluded = set()

        class Excl:
            unavailable = False

            def is_excluded(self, mailbox):
                return mailbox in excluded

        path = self.store_path

        class Store:
            def __init__(self):
                self.db = sqlite3.connect(path)
                self.db.row_factory = sqlite3.Row

            def body_text(self, row):
                return row["body"]

        dsn = self.dsn
        for name, fake in (("_open_store", lambda state=None: Store()),
                           ("_exclusion", Excl),
                           ("_connect", lambda read_only: self.pg_testdb.psycopg.connect(
                               dsn, autocommit=True)),
                           ("_dsn", lambda: dsn)):
            patcher = mock.patch.object(BM, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def conn(self):
        return self.pg_testdb.psycopg.connect(self.dsn, autocommit=True)

    def mail(self, n, text, mailbox="box"):
        handle, obj = f"graph-mail://m/{n}", f"Email:graph_mail.messages:{n}"
        db = sqlite3.connect(self.store_path)
        db.execute("INSERT INTO messages (version_id, l1_handle, mailbox_id, record, body) "
                   "VALUES (?, ?, ?, ?, ?)", (n, handle, mailbox,
                                              json.dumps({"subject": f"mail {n}"}), text))
        db.commit()
        db.close()
        self.version(obj, handle, '["sales"]')
        return obj

    def version(self, obj, handle, markings="[]"):
        self.ver += 1
        with self.conn() as c:
            c.execute("INSERT INTO ontology.object_versions VALUES "
                      "(%s, %s, %s, 'd', 'l', %s, '2026-01-01', NULL)",
                      (self.ver, obj, handle, markings))

    def ident(self, kind, value, obj, lineage="l", handle=None):
        self.ver += 1
        with self.conn() as c:
            c.execute("INSERT INTO ontology.identifiers VALUES (%s, %s, %s, 'r', %s, 'p', "
                      "%s, 'f', %s, '[]', '2026-01-02', NULL)",
                      (kind, value, value, obj, handle or f"h{self.ver}:{obj}", lineage))

    def supersede_idents(self, obj):
        with self.conn() as c:
            c.execute("UPDATE ontology.identifiers SET superseded_at = '2026-02-01' "
                      "WHERE object_id = %s AND superseded_at IS NULL", (obj,))

    def twin(self, a, b, tier):
        with self.conn() as c:
            c.execute("INSERT INTO ontology.link_rows (link_id, link_type, from_object, "
                      "to_object, tier, binding_kind, why_not, markings, recorded_at, reason, "
                      "evidence_handles) VALUES (%s, 'Customer.same_record_as', %s, %s, %s, "
                      "'same_record', '[]', '[]', '2026-01-01', '[]', '[]')",
                      (f"t:{a}:{b}:{tier}", a, b, tier))

    def stored(self):
        with self.conn() as c:
            return sorted(c.execute("SELECT from_object, to_object, link_type, markings "
                                    "FROM ontology.bare_mentions").fetchall())

    def build(self, **kw):
        return BM.build(1, None, **kw)["rows"]

    # -- the twin rule
    def test_several_holders_link_only_as_confirmed_twins(self):
        m = self.mail(1, "Please ship WINN01 today")
        for i in (1, 2, 3):
            self.ident("customer_erp_code", "WINN01", f"Customer:bc:{i}")
        self.twin("Customer:bc:1", "Customer:bc:2", "corroborated")
        self.twin("Customer:bc:3", "Customer:portal:9", "lead")      # a lead joins nothing
        self.assertEqual(self.build(), 0, "1+2 twins, 3 apart: ambiguous, no link")
        self.twin("Customer:portal:9", "Customer:bc:2", "verified")  # 3 = 9 lead only: still
        self.assertEqual(self.build(), 0)
        self.twin("Customer:bc:3", "Customer:portal:9", "reviewed")  # 3 = 9 = 2 = 1 confirmed
        self.assertEqual(self.build(), 3)
        self.assertEqual([r[:2] for r in self.stored()],
                         [(m, f"Customer:bc:{i}") for i in (1, 2, 3)])

    def test_one_holder_per_system_links_through_the_build(self):
        m = self.mail(1, "WINN01 and MC4206 \u2013 NF")
        self.ident("customer_erp_code", "WINN01", "Customer:bc.customers:1", "bc_a",
                   "bc://production/co1/customers/1")
        self.ident("customer_erp_code", "WINN01", "Customer:bc.customers:2", "bc_b",
                   "bc://production/co2/customers/2")
        self.ident("customer_erp_code", "WINN01", "Customer:portal.sage_ar_customer:3",
                   "sage_bronze")
        self.ident("item_code", "MC4206", "Product:bc.items:1", "bc_a")
        self.ident("item_code", "MC4206-NF", "Product:bc.items:2", "bc_a")
        self.assertEqual(self.build(), 4)
        self.assertEqual([r[1] for r in self.stored()],
                         ["Customer:bc.customers:1", "Customer:bc.customers:2",
                          "Customer:portal.sage_ar_customer:3", "Product:bc.items:2"])
        self.ident("customer_erp_code", "WINN01", "Customer:bc.customers:4", "bc_b",
                   "bc://production/co2/customers/4")      # a second holder in company 2
        self.assertEqual(self.build(), 1)
        self.assertEqual(self.stored()[0][:2], (m, "Product:bc.items:2"))

    def test_customer_code_that_is_also_an_item_code_is_ambiguous(self):
        self.mail(1, "MC4206")
        self.ident("customer_erp_code", "MC4206", "Customer:bc:1")
        self.ident("item_code", "MC4206", "Product:bc:1")
        stats: dict = {}
        with self.conn() as c:
            self.assertEqual(BM.resolve(c, ["MC4206"], stats), {})
        self.assertEqual(stats, {"ambiguous": 1})
        self.assertEqual(self.build(), 0)

    # -- the delta refresh: SQL rechecks, mails read only when they changed
    def feed(self, *changes):
        """One change feed cycle of (object, kind); the mark before it."""
        with self.conn() as c:
            mark = c.execute("SELECT coalesce(max(cycle_id), 0) FROM ontology.change_cycles"
                             ).fetchone()[0]
            cycle = c.execute("INSERT INTO ontology.change_cycles (name, source) VALUES "
                              "('delta:t', 'delta') RETURNING cycle_id").fetchone()[0]
            for obj, kind in changes:
                c.execute("INSERT INTO ontology.change_feed (cycle_id, object_id, object_type, "
                          "kind) VALUES (%s, %s, %s, %s)", (cycle, obj, obj.split(":")[0], kind))
        return mark

    def no_store(self):
        def refused(state=None):
            raise AssertionError("the mail store was read")
        return mock.patch.object(BM, "_open_store", refused)

    def test_a_changed_record_is_rechecked_in_sql_without_reading_mail(self):
        m1 = self.mail(1, "order for WINN01 and MC4206")
        self.mail(2, "WINN01 again")
        self.mail(3, "nothing named")
        self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
        self.ident("item_code", "MC4206", "Product:bc:1")
        self.assertEqual(self.build(), 3)
        before = self.stored()
        with self.no_store():
            # re-projected, same code: every row stays, none read
            self.supersede_idents("Customer:bc:1")
            self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
            got = BM.refresh(self.dsn, ["Customer:bc:1"])
            self.assertEqual((got["mails_read"], got["targets"], got["dropped_unheld"]), (0, 1, 0))
            self.assertEqual(self.stored(), before)
            # the code changes: the rows naming the old code go, the item stays
            self.supersede_idents("Customer:bc:1")
            self.ident("customer_erp_code", "WINN02", "Customer:bc:1")
            self.assertEqual(BM.refresh(self.dsn, ["Customer:bc:1"])["dropped_unheld"], 2)
            self.assertEqual([r[:2] for r in self.stored()], [(m1, "Product:bc:1")])
            # a NEW record now holds the item code (not a twin): ambiguous; only the new
            # record changed, and the row to the old one goes
            self.ident("customer_erp_code", "MC4206", "Customer:bc:7")
            BM.refresh(self.dsn, ["Customer:bc:7"])
            self.assertEqual(self.stored(), [])

    def test_feed_cycle_reads_only_changed_mails_and_rechecks_the_rest(self):
        self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
        self.ident("item_code", "MC4206", "Product:bc:1")
        old = self.mail(1, "WINN01 and MC4206")
        gone = self.mail(2, "WINN01")
        served = self.mail(3, "MC4206")
        self.assertEqual(self.build(), 4)
        new = self.mail(4, "new order WINN01")
        with self.conn() as c:   # mail 1 marked finance (a new version), mail 2 removed
            c.execute("UPDATE ontology.object_versions SET superseded_at = '2026-02-01' "
                      "WHERE object_id IN (%s, %s)", (old, gone))
        self.version(old, "graph-mail://m/1", '["finance"]')
        self.twin(served, "Product:bc:1", "lead")   # stand-in row: make it a served mention
        with self.conn() as c:
            c.execute("UPDATE ontology.link_rows SET link_type = 'Email.mentions_product', "
                      "binding_kind = 'mention' WHERE from_object = %s", (served,))
        mark = self.feed((old, "c"), (gone, "r"), (served, "l"), (new, "n"),
                         ("Product:bc:1", "l"), ("Customer:bc:1", "l"))
        reads = []
        real = BM.read_mails

        def counted(store, excl, where, params):
            reads.extend(params)
            return real(store, excl, where, params)

        with mock.patch.object(BM, "read_mails", counted):
            got = BM.refresh(self.dsn, since_cycle=mark)
        self.assertEqual(sorted(reads), ["graph-mail://m/1", "graph-mail://m/4"],
                         "only the changed mails (1, 4) are read")
        self.assertEqual((got["mails_read"], got["mails_deferred"], got["targets"]), (2, 0, 0))
        self.assertEqual(got["dropped_mail_gone"], 1)
        self.assertEqual(got["dropped_served"], 1)
        self.assertEqual([r[:2] + (r[3],) for r in self.stored()],
                         [(old, "Customer:bc:1", '["finance"]'),
                          (old, "Product:bc:1", '["finance"]'),
                          (new, "Customer:bc:1", '["sales"]')])
        with self.conn() as c:
            last = c.execute("SELECT max(cycle_id) FROM ontology.change_cycles").fetchone()[0]
        self.assertEqual(BM.refresh(self.dsn, since_cycle=last)["objects"], 0,
                         "a later cycle without changes does nothing")

    def test_feed_cycle_is_bounded_and_markings_follow_without_a_read(self):
        self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
        old = self.mail(9, "WINN01")       # sorts after the new ones: not read again
        self.build()
        with self.conn() as c:
            c.execute("UPDATE ontology.object_versions SET superseded_at = '2026-02-01' "
                      "WHERE object_id = %s", (old,))
        self.version(old, "graph-mail://m/9", '["finance"]')
        news = [self.mail(n, f"WINN01 no {n}") for n in (2, 3, 4)]
        mark = self.feed((old, "c"), *[(m, "n") for m in news])
        got = BM.refresh(self.dsn, since_cycle=mark, max_mails=2)
        self.assertEqual((got["mails_read"], got["mails_deferred"], got["remarked"]), (2, 2, 1))
        rows = self.stored()
        self.assertEqual(len(rows), 3, "two of the four changed mails read; the rest nightly")
        self.assertIn((old, "Customer:bc:1", "Email.mentions_customer", '["finance"]'), rows,
                      "markings move in SQL even for a mail not read again")
        self.assertEqual(self.build(), 4)

    def test_feed_refresh_n_1_10_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                self.mail(1000, "seed CUST01")
                self.ident("customer_erp_code", "CUST01", "Customer:bc:1")
                self.build()
                want = []
                for i in range(1, n + 1):
                    code = f"CUST{i:02d}"
                    want.append((self.mail(i, f"re: {code}"), f"Customer:bc:{i}"))
                    self.ident("customer_erp_code", code, f"Customer:bc:{i}")
                mark = self.feed(*[(m, "n") for m, _ in want],
                                 *[(t, "c") for _, t in want])
                t0 = time.monotonic()
                got = BM.refresh(self.dsn, since_cycle=mark)
                self.assertLess(time.monotonic() - t0, 2 + n * 0.05)
                self.assertEqual((got["mails_read"], got["rows"]), (n, n))
                rows = [r[:2] for r in self.stored()]
                self.assertEqual(sorted(rows), sorted(want + [
                    ("Email:graph_mail.messages:1000", "Customer:bc:1")]))

    def test_refresh_without_the_exclusion_changes_nothing(self):
        self.mail(1, "WINN01")
        self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
        self.build()
        before = self.stored()

        def refused():
            raise SystemExit("bare_mentions: people exclusion not loaded; refused")

        with mock.patch.object(BM, "_exclusion", refused):
            with self.assertRaises(SystemExit):
                BM.refresh(self.dsn, ["Customer:bc:2", "Email:graph_mail.messages:1"])
            self.assertEqual(self.stored(), before)
            # no mail to read: no mail store, no exclusion needed
            self.supersede_idents("Customer:bc:1")
            self.assertEqual(BM.refresh(self.dsn, ["Customer:bc:1"])["dropped_unheld"], 1)

    def test_excluded_mailbox_never_linked_by_build_or_refresh(self):
        self.mail(1, "WINN01", mailbox="excluded-box")
        self.mail(2, "WINN01")
        self.excluded.add("excluded-box")
        self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
        self.assertEqual(self.build(), 1)
        BM.refresh(self.dsn, ["Customer:bc:1", "Email:graph_mail.messages:1"])
        mark = self.feed(("Email:graph_mail.messages:1", "n"))
        BM.refresh(self.dsn, since_cycle=mark)
        self.assertEqual([r[0] for r in self.stored()], ["Email:graph_mail.messages:2"])

    def test_a_killed_builds_pending_table_is_dropped_not_filled(self):
        self.mail(1, "WINN01")
        self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
        self.build()
        with self.conn() as c:   # what an OOM-killed build leaves: no build holds the lock
            c.execute(f"CREATE UNLOGGED TABLE {BM.PENDING} (object_id text NOT NULL)")
            c.execute(f"CREATE UNLOGGED TABLE {BM.NEXT} (LIKE {BM.TABLE} INCLUDING ALL)")
        self.assertEqual(BM.refresh(self.dsn, ["Customer:bc:1"])["pending"], 0)
        with self.conn() as c:
            self.assertEqual(c.execute("SELECT to_regclass(%s), to_regclass(%s)",
                                       (BM.PENDING, BM.NEXT)).fetchone(), (None, None))

    def test_bare_links_view_survives_the_swap_cascade(self):
        import psycopg
        with self.conn() as c:
            try:
                with c.transaction():
                    c.execute("DROP TABLE ontology.link_rows CASCADE")   # as ontology_swap
                    self.assertIsNotNone(c.execute(
                        "SELECT to_regclass('ontology_bare.links')").fetchone()[0])
                    self.assertIsNotNone(c.execute(
                        "SELECT to_regclass('ontology_bare_seen.links')").fetchone()[0])
                    raise psycopg.Rollback()
            except psycopg.Rollback:
                pass
            self.assertIsNotNone(c.execute("SELECT to_regclass('ontology.link_rows')"
                                           ).fetchone()[0])

    # -- cardinality: version ranges lose, duplicate or slow nothing
    def test_build_ranges_n_1_10_60(self):
        for n in (1, 10, 60):
            with self.subTest(n=n):
                self.setUp()
                want = []
                for i in range(1, n + 1):
                    code = f"CUST{i:02d}"
                    want.append((self.mail(i, f"re: {code} and ITEM{i:03d}X"),
                                 f"Customer:bc:{i}"))
                    self.ident("customer_erp_code", code, f"Customer:bc:{i}")
                t0 = time.monotonic()
                self.assertEqual(self.build(step=7), n)
                self.assertLess(time.monotonic() - t0, 2 + n * 0.2)
                got = self.stored()
                self.assertEqual(sorted(r[:2] for r in got), sorted(want))
                self.assertTrue(all(r[3] == '["sales"]' for r in got))
                self.assertEqual(self.build(step=1000), n, "one range: the same rows")
                self.assertEqual(self.stored(), got)


    # -- the nightly rebuild (the first-time naming gap) and its safety with the switch on
    def test_nightly_build_links_a_mail_naming_a_record_by_a_new_identifier(self):
        m = self.mail(1, "please quote NEWCO7 again")
        self.mail(2, "WINN01")
        self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
        self.assertEqual(self.build(), 1)
        self.ident("customer_erp_code", "NEWCO7", "Customer:bc:7")   # the delta's new code
        BM.refresh(self.dsn, ["Customer:bc:7"])
        self.assertNotIn((m, "Customer:bc:7"), [r[:2] for r in self.stored()], "the gap")
        self.assertEqual(self.build(), 2)
        self.assertIn((m, "Customer:bc:7"), [r[:2] for r in self.stored()])

    def test_rebuild_keeps_the_served_rows_readable_and_their_first_time(self):
        self.mail(1, "WINN01 and MC4206")
        self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
        self.ident("item_code", "MC4206", "Product:bc:1")
        self.build()
        with self.conn() as c:
            first = dict(c.execute("SELECT link_id, recorded_at FROM ontology.bare_mentions"))
        self.supersede_idents("Product:bc:1")    # the item goes: one row must go, one stay
        seen = []
        real = BM._build_range

        def watched(args):
            n = real(args)
            with self.conn() as c:   # a reader mid-build: the old rows, whole
                seen.append(c.execute("SELECT count(*) FROM ontology.bare_mentions"
                                      ).fetchone()[0])
            return n

        with mock.patch.object(BM, "_build_range", watched):
            report = BM.build(1, None)
        self.assertEqual(seen, [2])
        self.assertEqual((report["rows"], report["removed"], report["added_or_updated"]),
                         (1, 1, 0))
        with self.conn() as c:
            after = dict(c.execute("SELECT link_id, recorded_at FROM ontology.bare_mentions"))
            self.assertIsNone(c.execute("SELECT to_regclass('ontology.bare_mentions_next')"
                                        ).fetchone()[0])
        self.assertEqual(after, {k: v for k, v in first.items() if k in after})

    def test_a_refresh_during_the_build_is_redone_after_its_merge(self):
        self.mail(1, "WINN01")
        self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
        self.build()
        real = BM._build_range

        def delta_mid_build(args):
            n = real(args)                       # staged: mail 1 -> the customer
            self.supersede_idents("Customer:bc:1")
            self.ident("customer_erp_code", "WINN02", "Customer:bc:1")
            self.assertEqual(BM.refresh(self.dsn, ["Customer:bc:1"])["pending"], 1)
            return n

        with mock.patch.object(BM, "_build_range", delta_mid_build):
            report = BM.build(1, None)
        self.assertEqual((report["staged"], report["pending"]), (1, 1))
        self.assertEqual(self.stored(), [], "the stale staged row is not served")
        self.assertEqual(BM.refresh(self.dsn, ["Customer:bc:1"])["pending"], 0)

    def test_a_second_build_or_a_trial_on_a_built_table_is_refused(self):
        self.mail(1, "WINN01")
        self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
        self.assertEqual(self.build(limit=1), 1)
        with self.assertRaises(SystemExit):
            self.build(limit=1)
        with self.conn() as other:
            other.execute("SELECT pg_advisory_lock(%s)", (BM.BUILD_LOCK,))
            with self.assertRaises(SystemExit):
                self.build()
        self.assertEqual(len(self.stored()), 1)

    def test_nightly_skips_before_the_migration(self):
        out = []
        with mock.patch.object(BM, "table_exists", lambda conn: False), \
                mock.patch.object(BM, "build", side_effect=AssertionError("built")), \
                mock.patch("builtins.print", out.append):
            self.assertEqual(BM.main(["build", "--if-migrated"]), 0)
        self.assertEqual(out, ['{"skipped": "not_migrated"}'])

    def test_delta_hook_survives_a_refused_refresh(self):
        from operating.pg import ontology_delta

        def refused():
            raise SystemExit("bare_mentions: people exclusion not loaded; refused")

        self.mail(1, "WINN01")
        self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
        self.build()
        with mock.patch.object(BM, "_exclusion", refused):
            got = ontology_delta.refresh_bare(self.dsn, ["Email:graph_mail.messages:1"])
        self.assertIn("refused", got["error"])

    def test_cycle_hook_reads_the_feed_past_its_mark_else_the_layers_owned(self):
        from operating.pg import ontology_delta
        self.mail(1, "WINN01")
        self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
        self.build()
        self.supersede_idents("Customer:bc:1")
        mark = ontology_delta._feed_mark(self.dsn)
        self.feed(("Customer:bc:1", "c"))
        self.assertIsNone(ontology_delta.refresh_bare_cycle(self.dsn, {"applied": False}, []))
        got = ontology_delta.refresh_bare_cycle(self.dsn, {"bare_since": mark}, [])
        self.assertEqual((got["objects"], got["dropped_unheld"]), (1, 1))
        # no feed: the owned records of the layers applied now
        self.ident("customer_erp_code", "WINN01", "Customer:bc:1")
        self.build()
        self.supersede_idents("Customer:bc:1")
        layer = os.path.join(self.tmp.name, "delta-x.sqlite")
        with sqlite3.connect(layer) as db:
            db.execute("CREATE TABLE IF NOT EXISTS delta_owned (object_id TEXT)")
            db.execute("INSERT INTO delta_owned VALUES ('Customer:bc:1')")
        got = ontology_delta.refresh_bare_cycle(
            self.dsn, {"bare_since": None, "applied_layers": ["delta-x.sqlite"]}, [layer])
        self.assertEqual(got["dropped_unheld"], 1)
        self.assertEqual(self.stored(), [])


if __name__ == "__main__":
    unittest.main()
