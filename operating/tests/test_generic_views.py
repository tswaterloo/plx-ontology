"""The generic door (``operating.pg.generic_views`` over ``operating.pg.generic_records``,
DOOR v2 with range-shaped class settings): record_types, records and record_links read
through ``plx.sql`` as the NOLOGIN reader role, checked at the boundary.

A store with records of several systems (Portal customers held twice and joined by an
identity link, a finance-marked customer, an invoice whose amount is finance-marked and one
that is not, a source-deleted record, Mission Control tasks in groups of 1, 10 and 60, one
task an excluded person's own, mail (the comms tables', never in records), a SharePoint list
item under an audience staff hold (a full reader's only: not a business system), a record
naming Freightcom and a property whose name is SQL-shaped) loaded into a throwaway Postgres
and built into the tables: what a full reader, a staff reader and a reader with no grant see
in records and record_links against plx.table, counts that do not leak, the catalog, the
generated prompt lines, the cost guard, unknown input failing closed, a newly excluded
person's rows dropping, an in-place refresh equal to a fresh build, and the coverage check."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from ontology.json_contract import validate
from operating import ontology_tools as ot
from operating import ontology_tools_sql as ts
from operating import ontology_tools_table as tt
from operating.pg import generic_records as gr
from operating.pg import generic_views as gv
from operating.pg import source_coverage as cov
from operating.tests import test_ontology_tools_table as tab
from tests import test_ontology_tools_evidence as ev

TAG = "exclusion-tag-1"
ODD = "a\"b'c); DROP TABLE x; --"
DOMAIN = {
    "format": "plx-ontology-domain/v1", "domain": "gen", "origin": "curated",
    "description": "generic views fixture.",
    "object_types": [
        ev._type("Customer", [("name", "text"), ("code", "text")],
                 [("portal.app_customer", "portal_api"), ("portal.sage_ar_customer",
                                                          "portal_api")], display=["name"]),
        ev._type("SalesInvoice", [("invoice_number", "text"), ("invoice_date", "date"),
                                  ("total_amount", "money")],
                 [("portal.sage_oe_invoice_header", "portal_api")],
                 display=["invoice_number"]),
        ev._type("MCTask", [("title", "text"), ("status", "text"), ("due", "date"),
                            (ODD, "text")],
                 [("mission_control.tasks", "mission_control")], display=["title"]),
        ev._type("Email", [("subject", "text"), ("received_at", "timestamp")],
                 [("graph_mail.messages", "graph_mail")], display=["subject"]),
        ev._type("SharePointListItem", [("title", "text")],
                 [("gen.sharepoint.list_items", "sharepoint")], display=["title"]),
    ],
    "link_types": [ev._link("Customer.same_record_as", "Customer", "Customer", "same_record"),
                   ev._link("SalesInvoice.billed_to", "SalesInvoice", "Customer"),
                   ev._link("MCTask.about", "MCTask", "Customer")],
}
for _spec in DOMAIN["object_types"]:
    for _binding in _spec["bindings"]:
        _binding["relation"] = _binding["id"]
for _lt in DOMAIN["link_types"]:
    _lt.update(name=_lt["id"].split(".", 1)[1],
               cardinality="1:1" if _lt["id"].endswith("same_record_as") else "N:1")
SCHEMA = ot.SchemaView.from_documents([DOMAIN])
FULL, OPS, NOBODY = tab.FULL, tab.OPS, tab.NOBODY
# what OPS may never read; what nobody may ever read
OPS_SECRET = ("Hidden Co", "4444.44", "Mail one", "List row")
NEVER = ("Freightcom", "GONE-9", "Excluded mail", "Excluded task", "Mail one")


def _api(rel, key):
    return f"api://{rel}/%7B%22id%22%3A%22{key}%22%7D@0000000000000001-1"


def obj(s, otype, binding, key, values, *, audience="api:public.x", handle=None,
        markings="[]", cell_marks=None):
    oid = f"{otype}:{binding}:{key}"
    s.obj(oid, audience, handle or _api(binding, key), "gold_etl_from_fm",
          markings=markings)
    for prop, value in values.items():
        kind, value, cur = (("money", *value) if isinstance(value, tuple)
                            else ("text", value, None))
        s.val(oid, prop, kind, value, currency=cur,
              markings=(cell_marks or {}).get(prop))
    return oid


def build():
    s = tab.Store()
    acme_p = obj(s, "Customer", "portal.app_customer", "acme", {"name": "Acme Ltd",
                                                                 "code": "ACME"})
    acme_s = obj(s, "Customer", "portal.sage_ar_customer", "acme", {"name": "ACME LTD",
                                                                     "code": "ACME"})
    s.link("tw", "Customer.same_record_as", acme_p, acme_s, "verified")
    hidden = obj(s, "Customer", "portal.app_customer", "hid", {"name": "Hidden Co"},
                 markings='["finance"]')
    fc = obj(s, "Customer", "portal.app_customer", "fc", {"name": "Freightcom Inc"})
    inv = obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", "i1",
              {"invoice_number": "IN-1", "invoice_date": "20261103",
               "total_amount": ("4444.44", "CAD")}, cell_marks={"total_amount": '["finance"]'})
    plain = obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", "i2",
                {"invoice_number": "IN-2", "invoice_date": "2026-11-04",
                 "total_amount": ("12.50", "USD")})
    s.link("b1", "SalesInvoice.billed_to", inv, acme_s, "verified")
    s.link("b2", "SalesInvoice.billed_to", inv, hidden, "verified")
    s.link("b3", "SalesInvoice.billed_to", inv, fc, "verified")
    s.link("b4", "SalesInvoice.billed_to", plain, acme_s, "verified")
    gone = obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", "g9",
               {"invoice_number": "GONE-9"})
    for n in (1, 10, 60):
        for k in range(n):
            t = obj(s, "MCTask", "mission_control.tasks", f"t{n}-{k}",
                    {"title": f"Task {n}-{k}", "status": f"group{n}", "due": "2026-11-20",
                     ODD: "odd value"}, audience="api:mc.tasks",
                    handle=f"mc://tasks/t{n}-{k}")
        s.link(f"ab{n}", "MCTask.about", t, acme_p, "corroborated")
    s.link("ab60x", "MCTask.about", t, acme_p, "lead")   # the same link again: one row
    mail = obj(s, "Email", "graph_mail.messages", "m1", {"subject": "Mail one"},
               audience="mail:mbx1", handle="graph-mail://mbx1/message/m1")
    ex = obj(s, "Email", "graph_mail.messages", "m2", {"subject": "Excluded mail"},
             audience="mail:mbx1", handle="graph-mail://mbx1/message/m2")
    # links between two business records learned from mail: from mail m1 (a full reader's,
    # once the exclusion decided it) and from the excluded person's mail m2 (nobody's)
    # (m1's by a page of it: a page names its message's record)
    for lid, task, evidence in (("ev1", "t1-0", "graph-mail://mbx1/message/m1#page=1"),
                                ("ev2", "t10-0", "graph-mail://mbx1/message/m2")):
        s.conn.execute(
            "INSERT INTO links(link_id, link_type, from_object, to_object, tier, binding_kind, "
            "reason, why_not, evidence_handles, markings, recorded_at, superseded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (lid, "MCTask.about", f"MCTask:mission_control.tasks:{task}", acme_s,
             "corroborated", "mention", json.dumps([{"feature": "identifier_mention",
                                                     "agrees": True}]),
             "[]", json.dumps([evidence]), "[]", ev.T0,
             None))
    # an audience staff hold, but not a business system: a full reader's only (FULL_KEY)
    obj(s, "SharePointListItem", "gen.sharepoint.list_items", "r1", {"title": "List row"},
        handle="sharepoint://site1/list/r1")
    own = obj(s, "MCTask", "mission_control.tasks", "own", {"title": "Excluded task",
                                                            "status": "own"},
              audience="api:mc.tasks", handle="mc://tasks/own")
    s.conn.execute("UPDATE objects SET missing_from_source_since = ? WHERE object_id = ?",
                   (ev.T0, gone))
    s.conn.commit()
    return s, {"mail": mail, "excluded": ex, "acme_p": acme_p, "acme_s": acme_s, "own": own,
               "plain": plain}


def _order(value):
    return (value is None, str(value))


def _settings(c, values):
    for name, value in values.items():
        c.execute("SELECT set_config(%s, %s, true)", (name, value))


class Fixture:
    @classmethod
    def setUpClass(cls):
        import time
        import psycopg
        from operating.pg import sql_views
        from operating.pg.migrate import MIGRATIONS_DIR
        from operating.tests import pg_store_mode
        cls.store, cls.ids = build()
        cls.dsn = pg_store_mode._load("", cls.store.conn)
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            for name in ("0038_own_issued_documents.sql",
                         "0040_people_excluded_objects.sql", "0041_business_views.sql"):
                c.execute((MIGRATIONS_DIR / name).read_text())
            c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                      "reason) VALUES ('graph-mail://mbx1/message/m2', %s, 'participant')",
                      (cls.ids["excluded"],))
            c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                      "reason) VALUES ('mc://tasks/own', %s, 'participant')", (cls.ids["own"],))
            c.execute("INSERT INTO ontology.people_excluded_state (one, exclusion, built_at, "
                      "objects, handles, covered_ver, updated_at) VALUES (true, %s, 'x', 1, "
                      "1, 0, 'x')", (TAG,))
            c.execute("ANALYZE")   # the cost guard reads the planner's estimates
        for attempt in range(5):   # the role is the cluster's: parallel modules may race
            try:
                sql_views.install(cls.dsn)
                break
            except psycopg.errors.InternalError:
                time.sleep(0.5 * (attempt + 1))
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            c.execute(f"GRANT {sql_views.ROLE} TO CURRENT_USER")
        cls.types = gv.build_catalog(cls.dsn, SCHEMA)
        cls.built = gr.build(cls.dsn, workers=2)

    @classmethod
    def tearDownClass(cls):
        for pool in list(ts._POOLS.values()):
            pool.close()
        ts._POOLS.clear()

    def setUp(self):
        ts._DOOR.clear()
        ts._GRANTS.clear()

    def base(self):
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection(self.dsn, link_guard=True)
        self.addCleanup(conn.close)
        reader = PgStoreReader(conn, ot.StoreReader(self.store.conn))
        return ot.OntologyTools(reader, tab.POLICY, item_gate=lambda a, h: True,
                                schema=SCHEMA, grammar=None, contracts=ev.CONTRACTS,
                                clock=lambda: ev.NOW)

    def call(self, args, actor=FULL):
        full = (lambda base, a: TAG if a is FULL else "")
        with mock.patch("operating.pg.people_excluded.full_reader", full):
            out = ts.SqlTool(self.base()).call("plx.sql", args, actor)
        if "error" not in out and "sql" in args:
            self.assertEqual(validate(out, ts._CONTRACT["output"], root=ev.CONTRACTS), [],
                             json.dumps(out)[:1500])
        return out

    def rows(self, sql, actor=FULL):
        out = self.call({"sql": sql}, actor)
        self.assertNotIn("error", out, out)
        text = lambda v: v["untrusted_text"] if isinstance(v, dict) else v  # noqa: E731
        return [[text(c) for c in r["cells"]] for r in out["rows"]]

    def search(self, args, actor=FULL):
        from operating import ontology_tools_search as srch
        full = (lambda base, a: TAG if a is FULL else "")
        tool = ts.SqlTool(self.base())
        with mock.patch("operating.pg.people_excluded.full_reader", full):
            out = srch.run(lambda a: self.fail("documents called"), args,
                           call_records=lambda a: tool.guarded(tool.records_search, a, actor))
        if "error" not in out:
            self.assertEqual(validate(out, srch._CONTRACT["output"], root=ev.CONTRACTS), [],
                             json.dumps(out)[:1500])
        return out

    def labels(self, otype, actor, prop="name"):
        """plx.table's rows of ``otype`` for ``actor``: the ``prop`` texts."""
        table = tt.TableTool(self.base()).call("plx.table", {
            "from": otype, "select": [prop], "merge_twins": False, "limit": 200}, actor)
        self.assertNotIn("error", table, table)
        val = lambda v: v["untrusted_text"] if isinstance(v, dict) else v  # noqa: E731
        return sorted((val(r["values"][prop]["value"]) for r in table["rows"]), key=_order)


class GenericViews(Fixture, unittest.TestCase):
    def test_full_reader_reaches_every_type_but_excluded_gone_and_freightcom(self):
        got = dict(self.rows("SELECT type, count(*) FROM records GROUP BY 1"))
        # mail is the comms tables'; the deleted invoice, the excluded person's task and
        # the Freightcom customer are in no row the AI reads; the list row is not a
        # business system's and the exclusion decided no version yet (covered_ver 0)
        self.assertEqual(got, {"Customer": "3", "SalesInvoice": "2", "MCTask": "71"})
        listed = json.dumps([self.call({"sql": f"SELECT * FROM {v} LIMIT 500"}) for v in
                             ("records", "record_links")])
        for secret in NEVER:
            self.assertNotIn(secret, listed)
        self.assertIn("4444.44", listed)    # the full reader holds finance
        money = self.rows("SELECT (fields->>'total_amount')::numeric, "
                          "fields->>'total_amount_currency', record_date, label "
                          "FROM records WHERE type = 'SalesInvoice' ORDER BY label")
        self.assertEqual(money, [["4444.44", "CAD", "2026-11-03", "IN-1"],
                                 ["12.50", "USD", "2026-11-04", "IN-2"]])
        links = dict(self.rows("SELECT link_type, count(*) FROM record_links GROUP BY 1"))
        self.assertEqual(links, {"Customer.same_record_as": "1", "MCTask.about": "3",
                                 "SalesInvoice.billed_to": "3"})   # not Freightcom's
        self.assertEqual(self.rows("SELECT tier FROM record_links WHERE from_handle = "
                                   "'mc://tasks/t60-59'"), [["corroborated"]])
        # the Freightcom customer is in the table, kept from the AI's view only
        import psycopg
        with psycopg.connect(self.dsn) as c:
            self.assertEqual(c.execute("SELECT ai_ok FROM biz.records WHERE label = "
                                       "'Freightcom Inc'").fetchall(), [(False,)])

    def test_each_reader_sees_exactly_what_the_door_allows(self):
        for actor in (FULL, OPS, NOBODY):
            for otype, prop in (("Customer", "name"), ("MCTask", "title"),
                                ("SalesInvoice", "invoice_number")):
                shown = self.labels(otype, actor, prop)
                ours = sorted((r[0] for r in self.rows(
                    f"SELECT label FROM records WHERE type = '{otype}'", actor)), key=_order)
                # the door's additions to plx.table's rule, for every reader: nothing
                # naming Freightcom, never an excluded person's own record, and a record
                # with a field marked beyond the reader's markings is not shown at all
                hide = {"Freightcom Inc", "Excluded task"} | (
                    set() if actor is FULL else {"IN-1"})
                self.assertEqual(ours, [x for x in shown if x not in hide], (otype, actor))
        # staff: a list row of a non-business system is a full reader's alone
        self.assertEqual(self.rows("SELECT count(*) FROM records WHERE type = "
                                   "'SharePointListItem'", OPS), [["0"]])
        everything = json.dumps([self.call({"sql": f"SELECT * FROM {v} LIMIT 500"}, actor)
                                 for v in ("records", "record_links")
                                 for actor in (OPS, NOBODY)])
        for secret in OPS_SECRET + NEVER + ("IN-1",):
            self.assertNotIn(secret, everything)
        # a link shows when the reader holds both ends: OPS, the unmarked invoice's only
        self.assertEqual(self.rows("SELECT from_type, link_type, to_type, from_handle FROM "
                                   "record_links WHERE from_type = 'SalesInvoice'", OPS),
                         [["SalesInvoice", "SalesInvoice.billed_to", "Customer",
                           _api("portal.sage_oe_invoice_header", "i2")]])
        for view in ("records", "record_links"):
            self.assertEqual(self.rows(f"SELECT count(*) FROM {view}", NOBODY), [["0"]])

    def test_counts_do_not_leak(self):
        # the catalog is the store's structure: listed, with no count or date of records
        cols = self.call({"sql": "SELECT * FROM record_types LIMIT 1"}, NOBODY)["columns"]
        self.assertFalse({"rows", "newest"} & {c["name"] if isinstance(c, dict) else c
                                               for c in cols}, cols)
        self.assertEqual(self.rows("SELECT count(*) FROM record_types", NOBODY), [["5"]])
        for actor, want in ((FULL, 2), (OPS, 1), (NOBODY, 0)):
            described = self.call({"describe": "SalesInvoice"}, actor)
            self.assertEqual(described["sample"]["records_you_can_read"], want, actor)
            self.assertNotIn("4444.44", json.dumps(described) if actor is not FULL else "")
        out = self.call({"sql": "SELECT count(*) FROM records WHERE type = 'SalesInvoice'"},
                        OPS)
        self.assertEqual([r["cells"] for r in out["rows"]], [["1"]])
        self.assertNotIn("records", out.get("withheld") or {})   # nothing counted aside

    def test_unset_or_forged_scope_reads_nothing(self):
        import psycopg
        from operating.pg import door
        with psycopg.connect(self.dsn) as pg:
            pg.execute(f"SET LOCAL ROLE {gv.sql_views.ROLE}")
            for view in ("records", "record_links"):
                self.assertEqual(pg.execute(f"SELECT count(*) FROM biz_sql.{view}"
                                            ).fetchone()[0], 0)
        with psycopg.connect(self.dsn) as pg:
            # ranges and lists empty or unparsable: nothing (fail closed)
            for held in ("{}", ""):
                _settings(pg, {door.HELD: held, "plx.classes": "{}"})
                pg.execute(f"SET LOCAL ROLE {gv.sql_views.ROLE}")
                self.assertEqual(pg.execute("SELECT count(*) FROM biz_sql.records"
                                            ).fetchone()[0], 0)
                pg.execute("RESET ROLE")
            pg.execute(f"SET LOCAL ROLE {gv.sql_views.ROLE}")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                pg.execute("SELECT count(*) FROM biz.records")

    def test_cardinality_1_10_60_and_cost_guard(self):
        got = dict(self.rows("SELECT fields->>'status', count(*) FROM records WHERE type = "
                             "'MCTask' GROUP BY 1"))
        self.assertEqual(got, {"group1": "1", "group10": "10", "group60": "60"})
        handles = self.rows("SELECT handle FROM records WHERE type = 'MCTask'", OPS)
        self.assertEqual(len(handles), 71)
        self.assertEqual(len({h[0] for h in handles}), 71)   # none lost, none twice
        self.assertEqual(self.rows("SELECT label FROM records WHERE handle = "
                                   "'mc://tasks/t60-5'"), [["Task 60-5"]])
        with mock.patch.object(gv, "MAX_RECORDS", 5):   # 8 links, 78 records
            for sql in ("SELECT count(*) FROM records", "SELECT * FROM record_links"):
                out = self.call({"sql": sql})
                self.assertEqual((out.get("error") or {}).get("code"), "too_broad", (sql, out))
                self.assertIn("type", out["error"]["hint"])

    def test_injection_and_unknown_input_fail_closed(self):
        odd = self.rows(f"SELECT fields->>{gv._lit(ODD)} FROM records WHERE handle = "
                        "'mc://tasks/t1-0'")
        self.assertEqual(odd, [["odd value"]])
        for args in ({"describe": "x'; DROP TABLE y"}, {"describe": "NoSuchType"},
                     {"sql": "SELECT set_config('plx.held', '{[1,999)}', true)"},
                     {"sql": "SELECT * FROM biz.records"},
                     {"sql": "SELECT * FROM biz.access_classes"},
                     {"sql": "SELECT * FROM biz_page.records"},
                     {"sql": "SELECT * FROM records; DELETE FROM records"}):
            self.assertIn("error", self.call(args, OPS), args)
        described = self.call({"describe": "MCTask"})
        self.assertEqual(described["record_type"]["type"], "MCTask")
        self.assertIn(ODD, described["sample"]["commonest_values"])
        self.assertEqual(self.rows("SELECT count(*) FROM records WHERE type = 'MCTask'"),
                         [["71"]])   # nothing dropped

    def test_catalog_says_where_each_type_is_held_and_the_prompt_lines(self):
        import psycopg
        cat = {r[0]: r[1:] for r in self.rows(
            "SELECT type, systems, label_fields, date_fields, tables FROM record_types")}
        self.assertEqual(set(cat), {"Customer", "SalesInvoice", "MCTask", "Email",
                                    "SharePointListItem"})
        self.assertEqual(cat["MCTask"][0], ["mission_control"])
        self.assertEqual(cat["SalesInvoice"][2], ["invoice_date"])
        self.assertEqual(cat["MCTask"][3], ["records"])
        self.assertEqual(cat["Email"][3], [])      # no emails table built here
        with psycopg.connect(self.dsn) as c:
            counts = dict(c.execute("SELECT type, rows FROM biz.record_types").fetchall())
            lines = ts.catalog_lines(c)
        self.assertEqual(counts["MCTask"], 71)      # the excluded person's not counted
        self.assertEqual(counts["SalesInvoice"], 2)  # the source-deleted one not counted
        text = "\n".join(lines)
        self.assertIn("- mission_control: MCTask (", text)
        self.assertNotIn("Email", text)             # held by no table: not offered
        self.assertNotIn("71", text)                # no count in the prompt


def decide(dsn, covered):
    """The people exclusion's update moved ``covered_ver`` to ``covered`` (None: the newest
    version) and the doors were decided against it."""
    import psycopg
    from operating.pg import door
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("UPDATE ontology.people_excluded_state SET covered_ver = COALESCE(%s, "
                  "(SELECT max(ver) FROM ontology.object_versions))", (covered,))
        door.exclude_everywhere(c)
    ts._DOOR.clear()


class Undecided(Fixture, unittest.TestCase):
    """The people exclusion decides versions up to ``covered_ver``: a row of a system other
    than a business system newer than that is served to nobody (the fixture's set covers
    version 0: none of them), and appears once the set decided it."""

    def undecided(self):
        import psycopg
        with psycopg.connect(self.dsn) as c:
            return ({r[0] for r in c.execute("SELECT handle FROM biz.records WHERE NOT biz")},
                    {r[0] for r in c.execute("SELECT handle FROM biz.record_links WHERE "
                                             "NOT biz")})

    def test_a_version_the_exclusion_has_not_decided_is_served_to_nobody(self):
        records, links = self.undecided()
        # not vacuous: the list row and the link learned from mail m1 are in the tables
        self.assertEqual(records, {"sharepoint://site1/list/r1"})
        self.assertEqual(len(links), 1)
        for actor in (FULL, OPS, NOBODY):
            got = {r[0] for r in self.rows("SELECT handle FROM records", actor)}
            got_links = {r[0] for r in self.rows("SELECT handle FROM record_links", actor)}
            self.assertFalse(got & records, actor)
            self.assertFalse(got_links & links, actor)
            self.assertTrue(got or actor is NOBODY, actor)   # business records stand
        rs = self.search({"query": "List row", "sources": ["records"]})
        self.assertEqual(rs["documents"], [])
        # the views' rule itself: no set, or a set never decided, decides nothing
        # (``exclusion`` is NOT NULL in the table, so an empty one is the undecided set)
        import psycopg
        with psycopg.connect(self.dsn) as c:
            for state in ("DELETE FROM ontology.people_excluded_state",
                          "UPDATE ontology.people_excluded_state SET exclusion = ''"):
                c.execute("UPDATE ontology.people_excluded_state SET covered_ver = "
                          "1000000000")
                self.assertEqual(c.execute(f"SELECT count(*) FROM biz.records t WHERE NOT "
                                           f"t.biz AND ({gv.UNDECIDED})").fetchone()[0], 1)
                c.execute(state)
                for table in ("records", "record_links"):
                    self.assertEqual(c.execute(
                        f"SELECT count(*) FROM biz.{table} t WHERE NOT t.biz AND "
                        f"({gv.UNDECIDED})").fetchone()[0], 0, (state, table))
                c.rollback()

    def test_served_once_decided_withheld_again_behind_and_dropped_with_its_evidence(self):
        import psycopg
        decide(self.dsn, None)
        self.assertEqual(self.rows("SELECT label FROM records WHERE type = "
                                   "'SharePointListItem'"), [["List row"]])
        self.assertEqual(self.rows("SELECT label FROM records WHERE type = "
                                   "'SharePointListItem'", OPS), [])   # FULL_KEY
        about = "SELECT count(*) FROM record_links WHERE link_type = 'MCTask.about'"
        self.assertEqual(self.rows(about), [["4"]])        # + the link learned from m1
        self.assertEqual(self.rows(about, OPS), [["3"]])   # learned from mail: full only
        with psycopg.connect(self.dsn) as c:
            (ver,) = c.execute("SELECT ver FROM biz.records WHERE NOT biz").fetchone()
            # the link learned from the excluded person's mail m2 is in no row at all
            self.assertEqual(c.execute("SELECT count(*) FROM biz.record_links WHERE "
                                       "from_handle = 'mc://tasks/t10-0'").fetchone(),
                             (0,))
        decide(self.dsn, ver - 1)   # a set behind the row's version: withheld again
        self.assertEqual(self.rows("SELECT label FROM records WHERE type = "
                                   "'SharePointListItem'"), [])
        decide(self.dsn, None)
        # mail m1's sender is excluded now: the link learned from it drops (lineage
        # read_ids), its two business records stay
        with psycopg.connect(self.dsn, autocommit=True) as c:
            mail = c.execute("SELECT object_id FROM ontology.object_versions WHERE "
                             "l1_handle = 'graph-mail://mbx1/message/m1'").fetchone()[0]
            c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                      "reason) VALUES ('graph-mail://mbx1/message/m1', %s, 'participant')",
                      (mail,))
        decide(self.dsn, None)
        self.assertEqual(self.rows(about), [["3"]])
        self.assertEqual(self.rows("SELECT count(*) FROM records WHERE handle IN "
                                   "('mc://tasks/t1-0')"), [["1"]])
        gr.refresh(self.dsn)   # and a refresh does not read it back
        ts._DOOR.clear()
        self.assertEqual(self.rows(about), [["3"]])


class RecordsSearch(Fixture, unittest.TestCase):
    """plx.search's records leg (``SqlTool.records_search`` through plx.sql's door, merged
    by ``ontology_tools_search.run``): each reader finds exactly what its door shows."""

    def titles(self, out):
        self.assertNotIn("error", out, out)
        return sorted(d["title"]["untrusted_text"] for d in out["documents"])

    def test_each_reader_finds_what_its_door_shows_and_nothing_else(self):
        # the list row is undecided until the exclusion covers its version: nobody finds it
        for actor in (FULL, OPS, NOBODY):
            self.assertEqual(self.titles(self.search(
                {"query": "List row", "sources": ["records"]}, actor)), [], actor)
        decide(self.dsn, None)
        self.addCleanup(decide, self.dsn, 0)   # the class's other tests see the fixture's set
        args = {"query": "IN", "sources": ["records"]}
        self.assertEqual(self.titles(self.search(args)), ["IN-1", "IN-2"])
        self.assertEqual(self.titles(self.search(args, OPS)), ["IN-2"])   # IN-1: finance
        self.assertEqual(self.titles(self.search(args, NOBODY)), [])
        for actor in (FULL, OPS, NOBODY):
            for q in ("Freightcom", "GONE", "Excluded task", "Mail one", "Hidden Co",
                      "List row"):
                got = self.search({"query": q, "sources": ["records"]}, actor)
                titles = self.titles(got)
                if actor is FULL and q in ("Hidden Co", "List row"):
                    self.assertEqual(titles, [q])    # a full reader's own
                else:
                    self.assertEqual(titles, [], (q, actor))
        hit = self.search({"query": "Acme Ltd", "sources": ["records"]})["documents"]
        self.assertEqual(hit[0]["title"]["untrusted_text"], "Acme Ltd")   # exact label first
        self.assertEqual(hit[0]["kind"], "record")
        self.assertIn("Customer in portal", hit[0]["snippet"]["untrusted_text"])
        self.assertEqual(self.search({"query": "IN-2", "sources": ["records"]}, OPS)[
            "documents"][0]["date"], "2026-11-04")

    def test_cardinality_1_10_60_paged_without_loss_and_bad_input_fails_closed(self):
        for group, n in (("Task 1-0", 1), ("Task 10-", 10), ("Task 60-", 60)):
            seen, cursor = [], None
            for _ in range(5):
                args = {"query": group, "sources": ["records"], "limit": 25}
                if cursor:
                    args["cursor"] = cursor
                out = self.search(args, OPS)
                seen += [d["citation"] for d in out["documents"]]
                cursor = out["completeness"]["next_cursor"]
                if not cursor:
                    break
            want = {f"mc://tasks/t{n}-{k}" for k in range(n)}
            self.assertEqual(len(seen), len(set(seen)), group)   # none twice
            self.assertTrue(want <= set(seen), group)            # none lost
            if n == 60:
                self.assertEqual(set(seen), want)                # three pages, exactly
        for args in ({"query": "x", "cursor": "r1:99999", "sources": ["records"]},
                     {"query": "IN", "since": "yesterday", "sources": ["records"]},
                     {"query": "%%", "sources": ["records"]},
                     {"participant": "a@b.example", "sources": ["records"]},
                     {"query": "IN", "sources": ["records"], "bogus": 1}):
            self.assertEqual(self.search(args)["error"]["code"], "invalid_input", args)
        with mock.patch.object(gv, "MAX_RECORDS", 0):   # too broad: a gap, not an error
            out = self.search({"query": "Task", "sources": ["records"]})
            self.assertEqual(out["documents"], [])
            self.assertEqual(out["gaps"][0]["reason"], "too_broad")
            self.assertFalse(out["completeness"]["complete"])


class Exclusion(Fixture, unittest.TestCase):
    def test_a_newly_excluded_persons_rows_drop_and_stay_refused_until_then(self):
        import psycopg
        from operating.pg import door
        task = "MCTask:mission_control.tasks:t60-7"
        self.assertEqual(self.rows("SELECT count(*) FROM records WHERE record_id = "
                                   f"{gv._lit(task)}", OPS), [["1"]])
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, object_id, "
                      "reason) VALUES ('mc://tasks/t60-7', %s, 'participant')", (task,))
            c.execute("UPDATE ontology.people_excluded_state SET covered_ver = 1")
        out = self.call({"sql": "SELECT count(*) FROM records"}, OPS)
        self.assertIn("error", out)        # the set grew: refused until decided again
        with psycopg.connect(self.dsn, autocommit=True) as c:
            door.exclude_everywhere(c)
        ts._DOOR.clear()
        self.assertEqual(self.rows("SELECT count(*) FROM records WHERE record_id = "
                                   f"{gv._lit(task)}", FULL), [["0"]])
        self.assertEqual(self.rows("SELECT count(*) FROM records WHERE type = 'MCTask'",
                                   OPS), [["70"]])


class Refresh(Fixture, unittest.TestCase):
    def test_refresh_in_place_equals_a_fresh_build(self):
        import psycopg
        tasks = {k: f"MCTask:mission_control.tasks:t10-{k}" for k in (3, 4, 9)}
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("UPDATE ontology.objects SET missing_from_source_since = '2026-11-01' "
                      "WHERE object_id = %s", (tasks[3],))
            for k in (4, 9):   # t10-9 holds the group's MCTask.about link
                c.execute("INSERT INTO ontology.people_excluded_objects (l1_handle, "
                          "object_id, reason) VALUES (%s, %s, 'participant')",
                          (f"mc://tasks/t10-{k}", tasks[k]))
            # a link the delta deleted with no new version of either end, no new row
            c.execute("DELETE FROM ontology.link_rows WHERE link_id = 'b4'")
        before = self.rows("SELECT count(*) FROM record_links WHERE link_type = "
                           "'SalesInvoice.billed_to'")
        self.assertEqual(before, [["3"]])
        out = gr.refresh(self.dsn)
        self.assertEqual(out["dropped"], 3, out)
        ts._DOOR.clear()
        self.assertEqual(self.rows("SELECT count(*) FROM record_links WHERE link_type = "
                                   "'SalesInvoice.billed_to'"), [["2"]])
        self.assertEqual(self.rows("SELECT count(*) FROM records WHERE type = 'MCTask'"),
                         [["68"]])
        self.assertEqual(self.rows("SELECT count(*) FROM record_links WHERE link_type = "
                                   "'MCTask.about'"), [["2"]])
        dump = ("SELECT rid, record_id, type, label, record_date, access_class, col_class, "
                "ai_ok, fields::text, ver, biz FROM biz.records ORDER BY rid",
                "SELECT from_rid, link_type, to_rid, tier, access_class, handle, ver, biz, "
                "ev_ids FROM biz.record_links ORDER BY 1, 2, 3, 4",
                "SELECT view, handle, object_ids, read_ids FROM biz.lineage WHERE view IN "
                "('records', 'record_links') ORDER BY 1, 2",
                "SELECT view, access_class, col_class, rows FROM biz.class_counts WHERE "
                "view IN ('records', 'record_links') ORDER BY 1, 2, 3",
                "SELECT type, tables FROM biz.record_types ORDER BY 1")
        with psycopg.connect(self.dsn) as c:
            refreshed = [c.execute(q).fetchall() for q in dump]
        gr.build(self.dsn, workers=1)
        with psycopg.connect(self.dsn) as c:
            self.assertEqual([c.execute(q).fetchall() for q in dump], refreshed)


class RefreshMovedRows(Fixture, unittest.TestCase):
    def test_a_record_written_again_under_a_new_row_or_gone_from_the_store(self):
        """11-18 live: a Person deleted and written again by a delta got a new store row;
        the refresh read it under the new rid while its old row stayed, and failed on the
        record id's unique index (every run). It is now dropped by its id and read again;
        a record whose store row is gone without a successor (2,516 rows on 11-18) is
        dropped by the daily sweep; the tables then equal a fresh build."""
        import psycopg
        moved, gone = (f"MCTask:mission_control.tasks:t10-{k}" for k in (5, 6))
        with psycopg.connect(self.dsn, autocommit=True) as c:
            top = c.execute("SELECT max(row_no) FROM ontology.objects").fetchone()[0]
            c.execute("UPDATE ontology.objects SET row_no = %s WHERE object_id = %s",
                      (top + 1, moved))
            c.execute("UPDATE ontology.objects SET row_no = -7 WHERE object_id = %s", (gone,))

        def rids(oid):
            with psycopg.connect(self.dsn) as c:
                return [r[0] for r in c.execute(
                    "SELECT rid FROM biz.records WHERE record_id = %s", (oid,))]
        gr.refresh(self.dsn)
        self.assertEqual(rids(moved), [top + 1])
        self.assertEqual(len(rids(gone)), 1)          # no successor: only the sweep sees it
        gr.refresh(self.dsn, sweep=True)
        self.assertEqual(rids(gone), [])
        dump = ("SELECT rid, record_id, type, label, access_class, ai_ok, fields::text, ver "
                "FROM biz.records ORDER BY rid",
                "SELECT from_rid, link_type, to_rid, tier, handle FROM biz.record_links "
                "ORDER BY 1, 2, 3, 4",
                "SELECT view, handle, object_ids FROM biz.lineage WHERE view IN "
                "('records', 'record_links') ORDER BY 1, 2")
        with psycopg.connect(self.dsn) as c:
            refreshed = [c.execute(q).fetchall() for q in dump]
        gr.build(self.dsn, workers=1)
        with psycopg.connect(self.dsn) as c:
            self.assertEqual([c.execute(q).fetchall() for q in dump], refreshed)


class BuildDuringRewrite(Fixture, unittest.TestCase):
    def test_a_record_read_twice_by_a_build_keeps_its_newest_row(self):
        """11-18 live: Person records the delta rewrites every cycle moved to a new store
        row while the build's batches ran, so one was read under both rows and the build
        failed on the record id's unique index. The newest row is kept."""
        import psycopg
        from unittest import mock
        real = gr._batches
        calls = []

        def batches(dsn, steps, workers, session):
            got = real(dsn, steps, workers, session)
            if not calls:   # the record batches: one record read again under an older row
                with psycopg.connect(dsn, autocommit=True) as c:
                    c.execute("INSERT INTO biz.records_new SELECT rid - 1000000, record_id, "
                              "type, system, source, handle, 'stale', record_date, first_seen, "
                              "changed_at, access_class, col_class, ai_ok, link_ok, fields, "
                              "ver, biz FROM biz.records_new ORDER BY rid LIMIT 1")
            calls.append(1)
            return got
        with mock.patch.object(gr, "_batches", side_effect=batches):
            gr.build(self.dsn, workers=1)
        with psycopg.connect(self.dsn) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM biz.records WHERE label = "
                                       "'stale'").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) - count(DISTINCT record_id) FROM "
                                       "biz.records").fetchone()[0], 0)


class DiskRoom(unittest.TestCase):
    def test_a_batch_waits_for_room_and_stops_well_above_the_floor(self):
        """11-18 7:20 PM ET live: a records build of 8 workers took /data from 56 to 22 GB
        free (each batch's temp tables). A batch starts only with the floor (35 GB) plus
        every worker's batch free; it waits for room, and stops when none comes back."""
        from unittest import mock
        gb = 1024 ** 3

        class NoSetting:
            def execute(self, *a, **k):
                raise RuntimeError("no pg_settings here")
        frees, slept = [40 * gb, 44 * gb, 60 * gb], []
        with mock.patch.dict(os.environ, {"PLX_RECORDS_MIN_FREE_GB": ""}):
            gr.room(NoSetting(), 4, free=lambda p: frees.pop(0), sleep=slept.append)
            self.assertEqual(len(slept), 2)            # 35 + 4 x 3 = 47 GB needed
            with self.assertRaisesRegex(RuntimeError, "below the 47 GB"):
                gr.room(NoSetting(), 4, wait=0, free=lambda p: 46 * gb, sleep=slept.append)
            with self.assertRaisesRegex(RuntimeError, "cannot be read"):
                gr.room(NoSetting(), 1, free=mock.Mock(side_effect=OSError("gone")))
        with mock.patch.dict(os.environ, {"PLX_RECORDS_MIN_FREE_GB": "lots"}), \
                self.assertRaisesRegex(RuntimeError, "PLX_RECORDS_MIN_FREE_GB"):
            gr.min_free_bytes()


class TypeChoice(unittest.TestCase):
    def test_an_uncovered_build_takes_the_mirrors_only_when_asked(self):
        """11-18 live: 184 raw relational mirror types (7.8M records, Sage's history) were
        unreachable because the first records build left every Rel* type out for disk;
        ``--mirrors`` builds them in, still without the curated tables' own types."""
        covered = gr.covered_types()
        self.assertIn("Customer", covered)
        # a view of a few records of its type covers none of them (11-18 live: the staff
        # view's 165 rows took all 45,882 Person records out of records; the AR balances'
        # 990 open documents would take 122,751 Sage AR documents)
        self.assertNotIn("Person", covered)
        self.assertNotIn("RelSageArOpenDocument", covered)
        for uncovered, mirrors, skip, rel in ((False, False, [], False),
                                              (True, False, covered, True),
                                              (True, True, covered, False)):
            choice = gr.type_choice(uncovered, mirrors)
            self.assertEqual(choice["skip_types"], skip)
            self.assertEqual("NOT LIKE 'Rel%'" in gr.types_sql(choice), rel)
        # one family of mirrors left out (Sage's 7.5M for the disk); a prefix that is not
        # a plain name is refused, and one in kept notes that is not reads no record
        sql = gr.types_sql(gr.type_choice(True, True, ["RelSage"]))
        self.assertIn("NOT LIKE 'RelSage%'", sql)
        self.assertNotIn("NOT LIKE 'Rel%'", sql)
        with self.assertRaises(ValueError):
            gr.type_choice(True, True, ["Rel%' OR true --"])
        self.assertEqual(gr.types_sql({"skip_prefixes": ["x' OR '1"]}), "false")


class Coverage(Fixture, unittest.TestCase):
    def test_coverage_counts_what_the_door_reaches_and_alarms_on_a_drop_or_new_gap(self):
        import psycopg
        decide(self.dsn, None)   # every version decided: the list row is reachable
        report = cov.check(self.dsn)
        by_type = {r["type"]: r for r in report["types"]}
        # served: what the sources hold (not excluded); reachable: a full reader through
        # the AI's door (Freightcom's customer is not)
        self.assertEqual({t: (r["served"], r["reachable"]) for t, r in by_type.items()},
                         {"Customer": (4, 3), "SalesInvoice": (2, 2), "MCTask": (71, 71),
                          "SharePointListItem": (1, 1), "Email": (1, 0)})
        self.assertEqual(by_type["Email"]["tables"], [])
        kinds = {(f["kind"], f.get("type")) for f in report["findings"]}
        self.assertEqual(kinds, {("unreachable", "Email")})
        systems = {r["system"]: r for r in report["systems"]}
        self.assertEqual((systems["mission_control"]["served"],
                          systems["mission_control"]["reachable"]), (71, 71))
        self.assertEqual(systems["mail"]["unreachable_types"], ["Email"])
        # the first run's gaps are known; a later drop or a new gap is an alarm
        baseline = cov.next_baseline(report, None)
        self.assertEqual(baseline["known_unreachable"], ["Email"])
        self.assertEqual(cov.alarming(report, baseline), [])
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("UPDATE ontology.objects SET missing_from_source_since = '2026-11-01' "
                      "WHERE object_id LIKE 'MCTask:mission_control.tasks:t10-%%'")
        gr.refresh(self.dsn)
        again = cov.check(self.dsn)
        self.assertEqual(next(r["reachable"] for r in again["types"]
                              if r["type"] == "MCTask"), 61)
        alarms = {(f["kind"], f.get("type")) for f in cov.alarming(
            again, {**baseline, "known_unreachable": []})}
        self.assertEqual(alarms, {("dropped", "MCTask"), ("new_unreachable", "Email")})
        self.assertIn("mission_control", cov.render(again))
        # the first run alarms no gap (its gaps are the starting state); a new gap alarms
        # once and is known after, while a drop keeps alarming until it recovers
        self.assertEqual(cov.alarming(report, None), [])
        later = cov.next_baseline(again, {**baseline, "known_unreachable": []})
        self.assertEqual(later["known_unreachable"], ["Email"])
        self.assertEqual({(f["kind"], f.get("type")) for f in cov.alarming(again, later)},
                         {("dropped", "MCTask")})
        self.assertIn(("unreachable", "Email"),
                      {(f["kind"], f.get("type")) for f in again["findings"]})
