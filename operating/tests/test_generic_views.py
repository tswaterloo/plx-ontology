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
import re
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
NEVER = ("GONE-9", "Excluded mail", "Excluded task", "Mail one")


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

    def base(self, owner=False):
        import dataclasses
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection(self.dsn, link_guard=True)
        self.addCleanup(conn.close)
        reader = PgStoreReader(conn, ot.StoreReader(self.store.conn))
        # The one holding ``OWNER_KEY``
        policy = dataclasses.replace(tab.POLICY, unrestricted=True, owner=True) if owner \
            else tab.POLICY
        return ot.OntologyTools(reader, policy, item_gate=lambda a, h: True,
                                schema=SCHEMA, grammar=None, contracts=ev.CONTRACTS,
                                clock=lambda: ev.NOW)

    def call(self, args, actor=FULL, owner=False):
        full = (lambda base, a: TAG if a is FULL else "")
        with mock.patch("operating.pg.people_excluded.full_reader", full):
            out = ts.SqlTool(self.base(owner)).call("plx.sql", args, actor)
        if "error" not in out and "sql" in args:
            self.assertEqual(validate(out, ts._CONTRACT["output"], root=ev.CONTRACTS), [],
                             json.dumps(out)[:1500])
        return out

    def rows(self, sql, actor=FULL, owner=False):
        out = self.call({"sql": sql}, actor, owner)
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
    def test_full_reader_reaches_every_type_but_excluded_and_gone(self):
        got = dict(self.rows("SELECT type, count(*) FROM records GROUP BY 1"))
        # mail is the comms tables'; the deleted invoice, the excluded person's task and
        # (Freightcom's customer is read like any other); the list row is not a
        # business system's and the exclusion decided no version yet (covered_ver 0)
        self.assertEqual(got, {"Customer": "4", "SalesInvoice": "2", "MCTask": "71"})
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
                                 "SalesInvoice.billed_to": "4"})   # Freightcom's too
        self.assertEqual(self.rows("SELECT tier FROM record_links WHERE from_handle = "
                                   "'mc://tasks/t60-59'"), [["corroborated"]])
        # the Freightcom customer is in the table, kept from the AI's view only
        import psycopg
        with psycopg.connect(self.dsn) as c:
            self.assertEqual(c.execute("SELECT ai_ok FROM biz.records WHERE label = "
                                       "'Freightcom Inc'").fetchall(), [(True,)])

    def test_each_reader_sees_exactly_what_the_door_allows(self):
        for actor in (FULL, OPS, NOBODY):
            for otype, prop in (("Customer", "name"), ("MCTask", "title"),
                                ("SalesInvoice", "invoice_number")):
                shown = self.labels(otype, actor, prop)
                ours = sorted((r[0] for r in self.rows(
                    f"SELECT label FROM records WHERE type = '{otype}'", actor)), key=_order)
                # the door's additions to plx.table's rule, for every reader: nothing
                # naming Freightcom, never an excluded person's own record; a record with
                # a field marked beyond the reader's markings is shown, that field withheld
                hide = {"Excluded task"}
                self.assertEqual(ours, [x for x in shown if x not in hide], (otype, actor))
        # staff: a list row of a non-business system is a full reader's alone
        self.assertEqual(self.rows("SELECT count(*) FROM records WHERE type = "
                                   "'SharePointListItem'", OPS), [["0"]])
        everything = json.dumps([self.call({"sql": f"SELECT * FROM {v} LIMIT 500"}, actor)
                                 for v in ("records", "record_links")
                                 for actor in (OPS, NOBODY)])
        for secret in OPS_SECRET + NEVER:
            self.assertNotIn(secret, everything)
        # a link shows when the reader holds both ends: OPS, both invoices' to the customers
        # it reads (never to the finance-marked customer; Freightcom's like any other)
        self.assertEqual(sorted(self.rows(
            "SELECT from_type, link_type, to_type, from_handle FROM record_links WHERE "
            "from_type = 'SalesInvoice'", OPS)),
            [["SalesInvoice", "SalesInvoice.billed_to", "Customer",
              _api("portal.sage_oe_invoice_header", h)] for h in ("i1", "i1", "i2")])
        for view in ("records", "record_links"):
            self.assertEqual(self.rows(f"SELECT count(*) FROM {view}", NOBODY), [["0"]])

    def test_a_marked_field_is_withheld_never_the_record(self):
        # Staff read every business record, finance values withheld
        # (live 2026-11-19: 6.58M Sage records hidden whole for one marked balance each)
        sql = ("SELECT label, fields->>'invoice_number', fields->>'total_amount', "
               "fields->>'total_amount_currency', record_date, withheld FROM records "
               "WHERE type = 'SalesInvoice' ORDER BY label")
        full = self.rows(sql)
        self.assertEqual([r[:5] for r in full], [["IN-1", "IN-1", "4444.44", "CAD",
                                                   "2026-11-03"],
                                                  ["IN-2", "IN-2", "12.50", "USD",
                                                   "2026-11-04"]])
        ops = self.call({"sql": sql}, OPS)
        self.assertNotIn("error", ops, ops)
        cells = [[c.get("untrusted_text", c) if isinstance(c, dict) else c for c in r["cells"]]
                 for r in ops["rows"]]
        self.assertEqual([r[:5] for r in cells], [["IN-1", "IN-1", None, None, "2026-11-03"],
                                                  ["IN-2", "IN-2", "12.50", "USD",
                                                   "2026-11-04"]])
        self.assertIn("fields", json.dumps(cells[0][5]))    # said withheld, not absent
        self.assertNotIn("fields", json.dumps(cells[1][5]))
        self.assertNotIn("4444.44", json.dumps(ops))
        # every way into the row's fields gives the open ones only
        for probe in ("SELECT fields::text FROM records WHERE label = 'IN-1'",
                      "SELECT jsonb_object_keys(fields) FROM records WHERE label = 'IN-1'",
                      "SELECT count(*) FROM records WHERE (fields->>'total_amount')::numeric"
                      " > 4000"):
            self.assertNotIn("4444", json.dumps(self.call({"sql": probe}, OPS)), probe)
        self.assertEqual(self.rows("SELECT count(*) FROM records WHERE "
                                   "(fields->>'total_amount')::numeric > 4000", OPS), [["0"]])
        self.assertEqual(self.rows("SELECT count(*) FROM records WHERE type = "
                                   "'SalesInvoice'", NOBODY), [["0"]])
        # the finance-marked customer is marked as a whole record: still nobody's but FULL
        self.assertEqual(self.rows("SELECT count(*) FROM records WHERE label = 'Hidden Co'",
                                   OPS), [["0"]])

    def test_counts_do_not_leak(self):
        # the catalog is the store's structure: listed, with no count or date of records
        cols = self.call({"sql": "SELECT * FROM record_types LIMIT 1"}, NOBODY)["columns"]
        self.assertFalse({"rows", "newest"} & {c["name"] if isinstance(c, dict) else c
                                               for c in cols}, cols)
        self.assertEqual(self.rows("SELECT count(*) FROM record_types", NOBODY), [["5"]])
        for actor, want in ((FULL, 2), (OPS, 2), (NOBODY, 0)):
            described = self.call({"describe": "SalesInvoice"}, actor)
            self.assertEqual(described["sample"]["records_you_can_read"], want, actor)
            self.assertNotIn("4444.44", json.dumps(described) if actor is not FULL else "")
        out = self.call({"sql": "SELECT count(*) FROM records WHERE type = 'SalesInvoice'"},
                        OPS)
        self.assertEqual([r["cells"] for r in out["rows"]], [["2"]])
        # no row counted aside; the one row read with a field withheld is said so
        self.assertEqual(out.get("withheld") or {}, {"records": {"fields": 1}})

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

    def test_cardinality_1_10_60_and_no_refusal_by_size(self):
        got = dict(self.rows("SELECT fields->>'status', count(*) FROM records WHERE type = "
                             "'MCTask' GROUP BY 1"))
        self.assertEqual(got, {"group1": "1", "group10": "10", "group60": "60"})
        handles = self.rows("SELECT handle FROM records WHERE type = 'MCTask'", OPS)
        self.assertEqual(len(handles), 71)
        self.assertEqual(len({h[0] for h in handles}), 71)   # none lost, none twice
        self.assertEqual(self.rows("SELECT label FROM records WHERE handle = "
                                   "'mc://tasks/t60-5'"), [["Task 60-5"]])
        with mock.patch.object(gv, "MAX_RECORDS", 5):   # 8 links, 78 records
            # every reader reads as at a psql prompt (owner 11-22): no row-count refusal,
            # the statement's own time limit (and the costly slots) bound it
            for sql in ("SELECT count(*) FROM records", "SELECT * FROM record_links"):
                self.assertNotIn("error", self.call({"sql": sql}, OPS), sql)
            self.assertNotIn("error", self.call({"sql": "SELECT count(*) FROM records"}))

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
        with psycopg.connect(self.dsn) as c:
            keyed = {t: ts.catalog_keys(f) for t, f in c.execute(
                "SELECT type, fields FROM biz.record_types WHERE cardinality(tables) > 0")}
        for line in lines:   # a type's kind fields named in its own entry, names only
            for rtype, keys in keyed.items():
                at = line.find(f" {rtype} (")
                if at >= 0 and keys:
                    self.assertIn("kinds by " + "/".join(keys), line[at:].split(")")[0])
        self.assertEqual(ts.catalog_keys('[{"name": "status"}, {"name": "kind"}, '
                                         '{"name": "x"}]'), ["kind", "status"])
        self.assertEqual(ts.catalog_keys("not json"), [])


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


TEAMS_DOMAIN = dict(DOMAIN, object_types=DOMAIN["object_types"] + [
    ev._type("TeamsChat", [("topic", "text")], [("teams.chats", "teams")],
             display=["topic"]),
    ev._type("TeamsChatMessage", [("subject", "text")], [("teams.chat_messages", "teams")],
             display=["subject"])])
for _spec in TEAMS_DOMAIN["object_types"][-2:]:
    for _binding in _spec["bindings"]:
        _binding["relation"] = _binding["id"]


def build_with_teams(base=build):
    """The fixture plus a Teams chat (no comms table of its own: a record) and one of its
    messages (the teams_messages table's: never a record)."""
    s, ids = base()
    ids["chat"] = obj(s, "TeamsChat", "teams.chats", "c1", {"topic": "Chat topic"},
                      audience="teams:chat:c1", handle="teams-chat://c1")
    ids["chat_message"] = obj(s, "TeamsChatMessage", "teams.chat_messages", "c1m1",
                              {"subject": "Chat line"}, audience="teams:chat:c1",
                              handle="teams-chat://c1/message/m1")
    s.conn.commit()
    return s, ids


class TeamsRecords(Fixture, unittest.TestCase):
    """A Teams chat has no comms table: it is a record, a full reader's alone (a staff reader
    holding the chat's audience still reads none), served once the exclusion decided it;
    a chat message stays the teams_messages table's."""

    @classmethod
    def setUpClass(cls):
        import sys
        here = sys.modules[__name__]
        with mock.patch.object(here, "build", build_with_teams), \
                mock.patch.object(here, "SCHEMA", ot.SchemaView.from_documents([TEAMS_DOMAIN])):
            super().setUpClass()

    def base(self, owner=False):
        """The fixture's reader policy with the chat's audience granted to staff and the
        full reader alike."""
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection(self.dsn, link_guard=True)
        self.addCleanup(conn.close)
        reader = PgStoreReader(conn, ot.StoreReader(self.store.conn))
        policy = ot.ReadPolicy(tenant="plx", audiences={
            **tab.POLICY.audiences, "teams:*": {"ops", "full"}}, markings=tab.POLICY.markings)
        return ot.OntologyTools(reader, policy, item_gate=lambda a, h: True,
                                schema=ot.SchemaView.from_documents([TEAMS_DOMAIN]),
                                grammar=None, contracts=ev.CONTRACTS, clock=lambda: ev.NOW)

    def test_a_chat_is_a_full_readers_record_and_its_messages_are_not_records(self):
        import psycopg
        with psycopg.connect(self.dsn) as c:
            got = dict(c.execute("SELECT handle, biz FROM biz.records WHERE type LIKE "
                                 "'Teams%'").fetchall())
        self.assertEqual(got, {"teams-chat://c1": False})
        self.assertEqual(self.rows("SELECT count(*) FROM records WHERE type = 'TeamsChat'"),
                         [["0"]])   # not decided by the exclusion yet: nobody's
        decide(self.dsn, None)
        self.assertEqual(self.rows("SELECT label FROM records WHERE type = 'TeamsChat'"),
                         [["Chat topic"]])
        # staff hold the chat's audience and still read none of it: not a business system
        for actor in (OPS, NOBODY):
            self.assertEqual(self.rows("SELECT count(*) FROM records WHERE type LIKE "
                                       "'Teams%'", actor), [["0"]], actor)
            listed = json.dumps(self.call({"sql": "SELECT * FROM records LIMIT 500"}, actor))
            self.assertNotIn("Chat topic", listed)
            self.assertNotIn("Chat line", listed)
            described = self.call({"describe": "TeamsChat"}, actor)
            self.assertEqual(described["sample"]["records_you_can_read"], 0, actor)
            self.assertNotIn("Chat topic", json.dumps(described))
        self.assertEqual(self.call({"describe": "TeamsChat"}, FULL)["sample"][
            "records_you_can_read"], 1)


PEOPLE_FIELDS = ("driver", "technician", "attendee", "organizer", "organiser", "participant",
                 "agent", "driver_name", "sales_agent")


class KindsInTheCatalog(Fixture, unittest.TestCase):
    """The catalog row of a type names the values its category-like text fields take (so a
    question can find a type by what its records say), from records anyone holding the
    record may read, never an audience's own value, a name, an address or a marked field."""

    def kinds(self, rows, types, staff=(), classes=None):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("DROP SCHEMA IF EXISTS kt CASCADE")
            c.execute("CREATE SCHEMA kt")
            c.execute("CREATE TABLE kt.access_classes (id int, audience_keys text[], rec_marks text[], "
                      "blocked bool)")
            c.execute("CREATE TABLE kt.staff (name text, email text)")
            c.execute("CREATE TABLE kt.records (type text, col_class int, ai_ok bool, "
                      "access_class int, fields jsonb)")
            for cid, keys, marks, blocked in (
                    [(i, [f"mail:{i}"], ["[]"], False) for i in range(1, 6)]
                    + [(6, ["mail:6"], ['["finance"]'], False), (7, ["mail:7"], ["[]"], True),
                       (8, ["api:sage.x"], ["[]"], False)]):
                c.execute("INSERT INTO kt.access_classes VALUES (%s, %s, %s, %s)",
                          (cid, keys, marks, blocked))
            for name in staff:
                c.execute("INSERT INTO kt.staff VALUES (%s, NULL)", (name,))
            for t, cls, n, fields in rows:
                for _ in range(n):
                    c.execute("INSERT INTO kt.records VALUES (%s, 0, true, %s, %s::jsonb)",
                              (t, cls, json.dumps(fields)))
            try:
                return gv.kinds_of(c, "kt", types, {t for t, *_ in rows})
            finally:
                c.execute("DROP SCHEMA kt CASCADE")

    def test_values_are_listed_only_when_anyone_holding_the_record_reads_them(self):
        spec = {"Notice": {"properties": [
            {"name": "kind", "value_type": "text"}, {"name": "payer", "value_type": "text",
                                                     "markings": ["finance"]},
            {"name": "ticket", "value_type": "text"}, {"name": "owner", "value_type": "text"},
            {"name": "by", "value_type": "text"},
            {"name": "total", "value_type": "money"}]
            + [{"name": n, "value_type": "text"} for n in PEOPLE_FIELDS]}}
        rows = []
        for cls in (1, 2, 3):            # one value in three audiences' records
            rows.append(("Notice", cls, 3, {"kind": "payment", "payer": "Acme", "total": "5",
                                            **{n: "Dale" for n in PEOPLE_FIELDS},
                                            "owner": "Pat Lee", "by": "PAT",
                                            "ticket": f"T-{cls}"}))
        rows.append(("Notice", 1, 6, {"kind": "private only here"}))     # one audience
        rows.append(("Notice", 6, 9, {"kind": "marked record"}))        # marked class
        rows.append(("Notice", 7, 9, {"kind": "blocked record"}))       # blocked class
        rows.append(("Notice", 1, 6, {"kind": "mail me a@b.com"}))
        for cls in (1, 2, 3, 4):
            rows.append(("Notice", cls, 2, {"kind": "invoice_received"}))
        rows.append(("Notice", 8, 6, {"kind": "from a business system"}))   # api: class
        rows.append(("Notice", 8, 2, {"kind": "too few"}))
        got = self.kinds(rows, spec, staff=("Pat Lee",))
        self.assertEqual(got, {"Notice": {"kind": ["payment", "invoice_received",
                                                   "from a business system"]}})

    def test_a_field_with_many_values_or_a_type_with_no_fields_lists_nothing(self):
        spec = {"Doc": {"properties": [{"name": "title", "value_type": "text"}]},
                "Odd": {"properties": []}}
        rows = [("Doc", 1 + i % 5, 2, {"title": f"title {i}"}) for i in range(45)]
        rows.append(("Odd", 1, 9, {"x": "y"}))
        self.assertEqual(self.kinds(rows, spec), {})

    def test_the_built_catalog_carries_kinds_and_still_no_counts(self):
        import psycopg
        with psycopg.connect(self.dsn) as c:
            c.execute("SELECT kinds FROM biz.record_types LIMIT 1")      # the column exists
        cols = self.call({"sql": "SELECT * FROM record_types LIMIT 1"}, NOBODY)["columns"]
        names = {c["name"] if isinstance(c, dict) else c for c in cols}
        self.assertIn("kinds", names)
        self.assertFalse({"rows", "newest"} & names)


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
        # not vacuous: the list row and the links learned from mail m1 and (the owner's
        # alone) from the excluded person's mail m2 are in the tables
        self.assertEqual(records, {"sharepoint://site1/list/r1"})
        self.assertEqual(len(links), 2)
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
            # the link learned from the excluded person's mail m2 is the owner's alone
            # (Never removed): its class holds OWNER_KEY
            from operating.pg.access_classes import OWNER_KEY
            self.assertEqual(c.execute(
                "SELECT %s = ANY(a.audience_keys) FROM biz.record_links l JOIN "
                "biz.access_classes a ON a.id = l.access_class WHERE l.from_handle = "
                "'mc://tasks/t10-0' AND l.link_type = 'MCTask.about'",
                (OWNER_KEY,)).fetchall(), [(True,)])
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
        # "IN" is in "Freightcom Inc" too (read like any other since 11-23)
        self.assertEqual(self.titles(self.search(args)), ["Freightcom Inc", "IN-1", "IN-2"])
        # IN-1's amount is finance-marked: OPS finds the invoice, never the amount
        self.assertEqual(self.titles(self.search(args, OPS)),
                         ["Freightcom Inc", "IN-1", "IN-2"])
        self.assertEqual(self.titles(self.search(args, NOBODY)), [])
        for actor in (FULL, OPS, NOBODY):
            for q in ("Freightcom", "GONE", "Excluded task", "Mail one", "Hidden Co",
                      "List row"):
                got = self.search({"query": q, "sources": ["records"]}, actor)
                titles = self.titles(got)
                if actor is FULL and q in ("Hidden Co", "List row"):
                    self.assertEqual(titles, [q])    # a full reader's own
                elif actor is not NOBODY and q == "Freightcom":
                    self.assertEqual(titles, ["Freightcom Inc"])   # like any other
                else:
                    self.assertEqual(titles, [], (q, actor))
        hit = self.search({"query": "Acme Ltd", "sources": ["records"]})["documents"]
        self.assertEqual(hit[0]["title"]["untrusted_text"], "Acme Ltd")   # exact label first
        self.assertEqual(hit[0]["kind"], "record")
        self.assertIn("Customer in portal", hit[0]["snippet"]["untrusted_text"])
        self.assertEqual(self.search({"query": "IN-2", "sources": ["records"]}, OPS)[
            "documents"][0]["date"], "2026-11-04")

    def test_a_name_spelled_close_is_found_through_the_same_door(self):
        """Few labels hold every word: the labels spelled close to the query are added,
        each saying so, read through the reader's own door (never a record its word
        search could not show)."""
        import psycopg
        # the test cluster's template may already carry pg_trgm (the box's 5433 one does):
        # start from a database without it, so the no-extension case is really tested
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("DROP EXTENSION IF EXISTS pg_trgm CASCADE")
        decide(self.dsn, None)
        self.assertEqual(self.titles(self.search({"query": "Acmee Ltd",
                                                  "sources": ["records"]})), [])  # no pg_trgm
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        decide(self.dsn, None)
        self.addCleanup(decide, self.dsn, 0)

        def close(query, actor=FULL):
            return self.search({"query": query, "sources": ["records"]}, actor)
        out = close("Acmee Ltd")
        self.assertIn("Acme Ltd", self.titles(out))
        self.assertTrue(all(d["match"] == "close in spelling" for d in out["documents"]))
        self.assertIn("spelled close", out["searched"]["widened"])
        for actor in (FULL, OPS, NOBODY):   # the rows its own word search shows, no other
            self.assertEqual(self.titles(close("Acmee Ltd", actor)),
                             self.titles(close("Acme Ltd", actor)), actor)
        self.assertEqual(self.titles(close("Acmee Ltd", NOBODY)), [])
        self.assertEqual(self.titles(close("Hiden Co")), ["Hidden Co"])   # a full reader's own
        for actor in (OPS, NOBODY):
            self.assertEqual(self.titles(close("Hiden Co", actor)), [], actor)
        for actor in (FULL, OPS, NOBODY):
            self.assertEqual(self.titles(close("Freightcomm", actor)),
                             [] if actor is NOBODY else ["Freightcom Inc"], actor)
            self.assertEqual(self.titles(close("Excluded tasks", actor)), [], actor)
        # every word held: nothing added
        held = close("Acme Ltd")
        self.assertNotIn("match", held["documents"][0])
        self.assertNotIn("widened", held["searched"])

    def test_a_name_holding_every_word_by_its_stem_is_found_through_the_same_door(self):
        """Few labels hold every word as typed: the labels holding every word by its
        English stem (small words dropped) are added first, each saying so, read through
        the reader's own door (never a record its word search could not show)."""
        decide(self.dsn, None)
        self.addCleanup(decide, self.dsn, 0)

        def find(query, actor=FULL):
            return self.search({"query": query, "sources": ["records"]}, actor)
        out = find("the Acme")
        self.assertIn("Acme Ltd", self.titles(out))
        self.assertTrue(all(d["match"] == "every word, by its stem"
                            for d in out["documents"]))
        self.assertIn("by its stem", out["searched"]["widened"])
        for actor in (FULL, OPS, NOBODY):   # the rows its own word search shows, no other
            self.assertEqual(
                [t for t in self.titles(find("the Acme", actor))
                 if t.lower().startswith("acme")],
                [t for t in self.titles(find("Acme", actor))], actor)
        self.assertIn("Hidden Co", self.titles(find("the Hidden", FULL)))
        for actor in (OPS, NOBODY):
            self.assertEqual(self.titles(find("the Hidden", actor)), [], actor)
        for actor in (FULL, OPS, NOBODY):
            self.assertEqual(self.titles(find("the excluded tasks", actor)), [], actor)
            self.assertEqual(self.titles(find("all Freightcoms", actor)),
                             [] if actor is NOBODY else ["Freightcom Inc"], actor)
        held = find("Acme Ltd")   # every word held: nothing added
        self.assertNotIn("match", held["documents"][0])
        self.assertNotIn("widened", held["searched"])

    def test_a_record_of_the_type_the_query_names_is_found_through_the_same_door(self):
        """Few labels hold every word: the records of a type the query names by its plain
        name's stems ("customers") whose label holds the other words by their stem are
        added, each saying so, through the reader's door (never a record its own word
        search could not show); a query naming no type adds none."""
        decide(self.dsn, None)
        self.addCleanup(decide, self.dsn, 0)
        how = "every other word, in a record of the type the query names"

        def typed(query, actor=FULL):
            out = self.search({"query": query, "sources": ["records"]}, actor)
            return [d["title"]["untrusted_text"] for d in out["documents"]
                    if d.get("match") == how], out
        got, out = typed("Acme customers")
        self.assertTrue(got and all(t.lower().startswith("acme") for t in got), got)
        self.assertIn("type the query names", out["searched"]["widened"])
        for actor in (FULL, OPS, NOBODY):
            got, _ = typed("Acme customers", actor)
            self.assertEqual(sorted(got), sorted(
                t for t in self.titles(self.search(
                    {"query": "Acme", "sources": ["records"]}, actor))
                if t.lower().startswith("acme")), actor)
        self.assertIn("Hidden Co", typed("the Hidden customer")[0])
        for actor in (OPS, NOBODY):
            self.assertEqual(typed("the Hidden customer", actor)[0], [], actor)
        self.assertEqual(typed("Acme widgets")[0], [])

    def test_a_record_named_by_a_number_in_the_query_is_found_through_the_same_door(self):
        """Few labels hold every word: the labels holding every word of the query that
        names a record by number (whole) are added, each saying so, through the reader's
        door (the fixture's numbers are short: two digits name a record here)."""
        from operating.pg import records_search as rs
        decide(self.dsn, None)
        self.addCleanup(decide, self.dsn, 0)

        def numbered(query, actor=FULL):
            out = self.search({"query": query, "sources": ["records"]}, actor)
            return [d["title"]["untrusted_text"] for d in out["documents"]
                    if d.get("match") == "every number in the query"], out
        with mock.patch.object(rs, "IDENT_MIN_DIGITS", 2), \
                mock.patch.object(rs, "INDEXED_LETTERS", 2):
            got, out = numbered("the task numbered 60-12")
            self.assertEqual(got, ["Task 60-12"])
            self.assertEqual(out["documents"][0]["match"], "every number in the query")
            self.assertIn("every number", out["searched"]["widened"])
            for actor in (FULL, OPS, NOBODY):   # the rows its word search shows, no other
                shown = self.titles(self.search({"query": "Task 60 12",
                                                 "sources": ["records"]}, actor))
                self.assertEqual(numbered("the task numbered 60-12", actor)[0],
                                 [t for t in shown if t == "Task 60-12"], actor)
            self.assertEqual(numbered("the task numbered 60-12", NOBODY)[0], [])
            self.assertEqual(numbered("the task numbered 601")[0], [])   # not whole
            self.assertNotIn("every number", numbered("Task 60-12")[1]["searched"].get(
                "widened", ""))   # every word held
        self.assertEqual(numbered("the task numbered 60-12")[0], [])   # too few digits

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
        # pages stop at MAX_OFFSET (was 1,000, now 10,000): the stop is said, never a
        # silent end of the list
        from operating.pg import records_search as rs
        with mock.patch.object(rs, "MAX_OFFSET", 25):
            first = self.search({"query": "Task 60-", "sources": ["records"], "limit": 25},
                                OPS)
            self.assertEqual(first["completeness"]["next_cursor"], "r1:25")
            self.assertNotIn("depth", first["searched"])
            last = self.search({"query": "Task 60-", "sources": ["records"], "limit": 25,
                                "cursor": "r1:25"}, OPS)
            self.assertEqual(len(last["documents"]), 25)
            self.assertEqual((last["completeness"]["next_cursor"],
                              last["completeness"]["reason"]), (None, "depth_cap"))
            self.assertIn("pages stop at the 25th match", last["searched"]["depth"])
        self.assertEqual(rs.read_cursor("r1:10000"), 10000)
        # words past the searched ones are named in the depth, never dropped unsaid
        many = " ".join(["Task"] + [f"w{i}" for i in range(rs.MAX_TERMS)])
        out = self.search({"query": many, "sources": ["records"]}, OPS)
        self.assertIn(f"not searched: w{rs.MAX_TERMS - 1}", out["searched"]["depth"])
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


    def test_label_candidates_give_every_reader_the_same_hits_as_reading_every_label(self):
        # the label index finds candidates as the store; the hits are read through the
        # door among them: for every reader, query and candidate cap (a cap the matches
        # pass included), page by page, the hits are those of the whole read through
        # the door, in its order (no row lost, added or moved)
        from operating import ontology_tools_search as srch
        from operating.pg import records_search as rs
        tool = ts.SqlTool(self.base())   # one connection for the whole walk
        full = (lambda base, a: TAG if a is FULL else "")

        def pages(query, actor):
            seen, cursor = [], None
            for _ in range(10):
                args = {"query": query, "sources": ["records"], "limit": 4}
                if cursor:
                    args["cursor"] = cursor
                with mock.patch("operating.pg.people_excluded.full_reader", full):
                    out = srch.run(lambda a: self.fail("documents called"), args,
                                   call_records=lambda a: tool.guarded(tool.records_search,
                                                                       a, actor))
                self.assertNotIn("error", out, out)
                self.assertEqual(out["gaps"], [], out)
                seen.append([d["citation"] for d in out["documents"]])
                cursor = out["completeness"]["next_cursor"]
                if not cursor:
                    return seen
            return seen
        decide(self.dsn, None)
        self.addCleanup(decide, self.dsn, 0)
        queries = ("Task", "Task 60", "task 1", "Ltd", "Acme", "Hidden Co", "Freightcom",
                   "List row", "Mail one", "Excluded", "nothing like it",
                   "Task 1-0")   # short words: checked on the candidates only
        for actor in (FULL, OPS, NOBODY):
            for query in queries:
                with mock.patch.object(rs, "INDEXED_LETTERS", 99):   # no candidates
                    whole = pages(query, actor)
                for floor, per_row in ((1, 1), (2, 1), (1000, 4)):
                    with self.subTest(actor=actor, query=query, cap=(floor, per_row)), \
                            mock.patch.object(rs, "CANDIDATES", floor), \
                            mock.patch.object(rs, "PER_ROW", per_row):
                        self.assertEqual(pages(query, actor), whole)
        self.assertEqual(len(pages("Task 60", OPS)[0]), 4)   # the case was exercised


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


class OwnerOnly(Fixture, unittest.TestCase):
    """The people set's records and links are kept, never removed: in the tables with
    ``OWNER_KEY`` in their class, read by the one reader marked for it, by no other
    reader."""

    def test_the_owner_reads_the_set_and_no_other_reader_does(self):
        decide(self.dsn, None)
        self.addCleanup(decide, self.dsn, 0)
        task = "SELECT label FROM records WHERE label = 'Excluded task'"
        self.assertEqual(self.rows(task, FULL, owner=True), [["Excluded task"]])
        for actor in (FULL, OPS, NOBODY):
            self.assertEqual(self.rows(task, actor), [], actor)
        about = "SELECT count(*) FROM record_links WHERE link_type = 'MCTask.about'"
        self.assertEqual(self.rows(about, FULL, owner=True), [["5"]])   # + the one from m2
        self.assertEqual(self.rows(about, FULL), [["4"]])


class RecordDates(Fixture, unittest.TestCase):
    """The declared day of records built before the declarations (``date_declare apply``) is
    read through the guarded ``records`` view from ``biz.record_dates``: one row per record
    whatever the number of side rows (N=1/10/60), only for the version it was read from, and
    no reader sees a record or a day it could not before."""

    def counts(self, actor):
        return self.rows("SELECT count(*), count(record_date) FROM records", actor)[0]

    def test_a_side_day_shows_once_for_its_version_to_every_reader_of_the_row(self):
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as c:
            self.assertIsNotNone(c.execute("SELECT to_regclass('biz.record_dates')").fetchone()[0])
            undated = c.execute("SELECT rid, type, ver, record_date IS NULL FROM biz.records "
                                "ORDER BY rid").fetchall()   # every record: side rows on dated ones too
        self.assertGreaterEqual(len(undated), 60)
        base = {a: self.counts(a) for a in (FULL, OPS, NOBODY)}
        try:
            for n in (1, 10, 60):
                with psycopg.connect(self.dsn, autocommit=True) as c:
                    c.execute("DELETE FROM biz.record_dates")
                    for rid, t, ver, _none in undated[:n]:
                        c.execute("INSERT INTO biz.record_dates (type, rid, ver, day, field) "
                                  "VALUES (%s, %s, %s, '2026-02-03', 'docdate')", (t, rid, ver))
                for actor in (FULL, OPS, NOBODY):
                    total, dated = self.counts(actor)
                    self.assertEqual(total, base[actor][0], (n, "no row lost or doubled"))
                    self.assertLessEqual(int(dated) - int(base[actor][1]), n)
                total, dated = self.counts(FULL)
                self.assertEqual(int(dated) - int(base[FULL][1]),   # the full reader: each new day
                                 sum(1 for r in undated[:n] if r[3]))
            with psycopg.connect(self.dsn, autocommit=True) as c:   # a record that changed since
                c.execute("UPDATE biz.record_dates SET ver = ver + 1")
            self.assertEqual(self.counts(FULL), base[FULL])
            # a side day wins over the build's older one for its version, and only for it
            with psycopg.connect(self.dsn, autocommit=True) as c:
                rid, t, ver, was = c.execute("SELECT rid, type, ver, record_date FROM biz.records "
                                             "WHERE record_date IS NOT NULL LIMIT 1").fetchone()
                handle = c.execute("SELECT handle FROM biz.records WHERE rid = %s", (rid,)).fetchone()[0]
                c.execute("INSERT INTO biz.record_dates VALUES (%s, %s, %s, '2001-02-03', 'x')",
                          (t, rid, ver))
            q = f"SELECT record_date FROM records WHERE handle = '{handle}'"
            self.assertEqual(self.rows(q), [["2001-02-03"]])
            with psycopg.connect(self.dsn, autocommit=True) as c:
                c.execute("UPDATE biz.record_dates SET ver = ver + 1")
            self.assertEqual(self.rows(q), [[was]])
        finally:
            with psycopg.connect(self.dsn, autocommit=True) as c:
                c.execute("DELETE FROM biz.record_dates")

    def test_a_side_dated_record_matches_a_since_until_search_on_every_path(self):
        """The candidate statements read the store's table, so they read the side day too: a
        record whose only day is in ``biz.record_dates`` is found by since/until, by the label
        words, by the stems and by the close spelling, and by none outside its days."""
        import psycopg
        from operating.pg import records_search as rs
        with psycopg.connect(self.dsn, autocommit=True) as c:
            rid, t, ver, handle, label = c.execute(
                "SELECT rid, type, ver, handle, label FROM biz.records WHERE "
                "label = 'Acme Ltd' ORDER BY rid LIMIT 1").fetchone()
            words = ["acme", "ltd"]
            c.execute("INSERT INTO biz.record_dates VALUES (%s, %s, %s, '2031-05-06', 'x')",
                      (t, rid, ver))
            self.addCleanup(lambda: psycopg.connect(self.dsn, autocommit=True).execute(
                "DELETE FROM biz.record_dates"))
            inside = {"since": "2031-01-01", "until": "2031-12-31", "cap": 100, "q": label,
                      **{f"w{i}": rs._like(w) for i, w in enumerate(words)},
                      "stq": c.execute("SELECT plainto_tsquery('english', %s)::text",
                                       (label,)).fetchone()[0]}
            inside.update({f"s{i}": p for i, p in enumerate(rs.stems(inside["stq"]))})
            outside = {**inside, "since": "2032-01-01", "until": "2032-12-31"}
            stmts = [rs.candidates_sql([0, 1], biz="biz", since=True, until=True),
                     rs.stem_candidates_sql(len(rs.stems(inside["stq"])), biz="biz", since=True, until=True)]
            if c.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'").fetchone():
                schema = c.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON "
                                   "n.oid = e.extnamespace WHERE e.extname = 'pg_trgm'").fetchone()[0]
                c.execute("SET pg_trgm.similarity_threshold = 0.4")
                stmts.append(rs.close_candidates_sql(biz="biz", trgm=schema, since=True, until=True))
            for sql in stmts:
                self.assertIn(handle, [r[0] for r in c.execute(sql, inside).fetchall()], sql)
                self.assertNotIn(handle, [r[0] for r in c.execute(sql, outside).fetchall()], sql)
        # and through the tool: the reader's own door, the same record
        out = self.search({"query": label, "sources": ["records"], "since": "2031-01-01",
                           "until": "2031-12-31"})
        self.assertIn(handle, [d["citation"] for d in out["documents"]])
        self.assertEqual(self.search({"query": label, "sources": ["records"],
                                      "since": "2032-01-01"})["documents"], [])

    def test_an_empty_side_table_changes_no_label_search_and_the_readers_can_read_it(self):
        """The table made by migration 0077 (before any apply) is empty: the label search
        reads it without error and finds the same record; plx_api / plx_eval hold SELECT."""
        import psycopg
        from operating.pg import records_search as rs
        from operating.pg.migrate import MIGRATIONS_DIR
        with psycopg.connect(self.dsn, autocommit=True) as c:
            for role in ("plx_api", "plx_eval"):
                if not c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                    c.execute(f"CREATE ROLE {role} NOLOGIN")
            c.execute("DELETE FROM biz.record_dates")
            c.execute("REVOKE ALL ON biz.record_dates FROM plx_api, plx_eval")   # the migration grants
            c.execute((MIGRATIONS_DIR / "0077_record_dates.sql").read_text())
            c.execute((MIGRATIONS_DIR / "0077_record_dates.sql").read_text())   # idempotent
            for role in ("plx_api", "plx_eval"):
                self.assertTrue(c.execute("SELECT has_table_privilege(%s, 'biz.record_dates', "
                                          "'SELECT')", (role,)).fetchone()[0], role)
                self.assertFalse(c.execute("SELECT has_table_privilege(%s, 'biz.record_dates', "
                                           "'INSERT')", (role,)).fetchone()[0], role)
            self.assertEqual(c.execute("SELECT count(*) FROM biz.record_dates").fetchone()[0], 0)
            handle, label = c.execute("SELECT handle, label FROM biz.records WHERE "
                                      "label = 'Acme Ltd' ORDER BY rid LIMIT 1").fetchone()
            args = {"since": "2000-01-01", "until": "2099-12-31", "cap": 100, "q": label,
                    "w0": rs._like("acme"), "w1": rs._like("ltd")}
            sql = rs.candidates_sql([0, 1], biz="biz", since=False, until=False)
            self.assertIn(handle, [r[0] for r in c.execute(sql, args).fetchall()], sql)
            dated = rs.candidates_sql([0, 1], biz="biz", since=True, until=True)   # undated: none
            self.assertEqual([], c.execute(dated, args).fetchall())
        out = self.search({"query": label, "sources": ["records"]})
        self.assertIn(handle, [d["citation"] for d in out["documents"]])


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
        self.assertEqual(before, [["4"]])   # Freightcom's too
        out = gr.refresh(self.dsn)
        # the deleted task goes; the two of the people set stay, the owner's alone
        self.assertEqual(out["dropped"], 1, out)
        ts._DOOR.clear()
        self.assertEqual(self.rows("SELECT count(*) FROM record_links WHERE link_type = "
                                   "'SalesInvoice.billed_to'"), [["3"]])
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


class SwapLocks(Fixture, unittest.TestCase):
    def test_the_swap_waits_out_a_reader_and_no_other_reader_waits_long(self):
        """11-19 live: the records swap ran the lineage and counts of ~36M rows after taking
        its renames' locks, 22 minutes in which plx.search's records leg was not ready. The
        heavy statements now run on the new tables first and the lock-taking ones as every
        other view's swap does: a short lock wait, tried again. A reader holding the served
        table open makes the swap try and pause; no new reader queues behind it."""
        import threading
        import time
        import psycopg
        from operating.pg import swap_lock
        slept: list[float] = []
        real = swap_lock.swap_in

        def watched(write, steps, **kw):
            return real(write, steps, sleep=lambda x: (slept.append(x), time.sleep(x)), **kw)

        before = self.rows("SELECT count(*) FROM records")
        holder = psycopg.connect(self.dsn)
        holder.execute("SELECT count(*) FROM biz.records")   # ACCESS SHARE, held open
        out: dict = {}

        def rebuild():
            try:
                with mock.patch.object(swap_lock, "swap_in", watched):
                    out["got"] = gr.build(self.dsn, workers=1)
            except BaseException as exc:  # noqa: BLE001 - reported by the assertion
                out["err"] = exc
        t = threading.Thread(target=rebuild)
        t.start()
        waits: list[float] = []
        try:
            deadline = time.monotonic() + 120
            while t.is_alive() and time.monotonic() < deadline:
                with psycopg.connect(self.dsn, autocommit=True) as c:
                    c.execute("SET lock_timeout = '10s'")   # a queued reader gives up, loudly
                    began = time.monotonic()
                    try:
                        c.execute("SELECT count(*) FROM biz.records").fetchone()
                    except (psycopg.errors.UndefinedTable, psycopg.errors.LockNotAvailable):
                        pass
                    waits.append(time.monotonic() - began)
                if len(slept) >= 3:
                    holder.rollback()   # the swap tried and failed three times: let it through
                time.sleep(0.05)
        finally:
            holder.rollback()
            holder.close()
            t.join(120)
        self.assertNotIn("err", out, out.get("err"))
        self.assertGreaterEqual(len(slept), 3)   # it did wait on the held reader
        self.assertLess(max(waits), 2.0, waits)  # and no new reader queued behind it
        ts._DOOR.clear()
        self.assertEqual(self.rows("SELECT count(*) FROM records"), before)


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


class LinkProbePlan(Fixture, unittest.TestCase):
    def test_dead_link_ids_are_read_by_index_probes_not_a_hash_or_merge_join(self):
        """11-20 live: from 12:54 PM ET every refresh failed past temp_file_limit (10 GB) at
        the statement reading the link ids of the changed records: temp tables made a moment
        before had no statistics, so the planner sorted all 45M live link rows against them
        (a hash join over every partition spilled the same way once they were analysed).
        The refresh now analyses them and turns hash and merge joins off for that
        statement only; both are back on for the inserts after it."""
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("CREATE TEMP TABLE gr_t (x text)")
            c.execute("INSERT INTO gr_t SELECT 'a' || g FROM generate_series(1, 500) g")
            seen = lambda: [c.execute(f"SHOW enable_{j}join").fetchone()[0]  # noqa: E731
                            for j in ("hash", "merge")]
            with c.transaction():
                gr._probe_plan(c, "gr_t")
                self.assertEqual(seen(), ["off", "off"])
                self.assertTrue(c.execute(
                    "SELECT 1 FROM pg_stats WHERE tablename = 'gr_t'").fetchone())
                gr._probe_plan(c, None)
                self.assertEqual(seen(), ["on", "on"])
            self.assertEqual(seen(), ["on", "on"])
        out = gr.refresh(self.dsn)     # the whole refresh still runs with it
        self.assertIn("links_read", out)


class LinkIdIndexes(Fixture, unittest.TestCase):
    def test_links_are_indexed_by_the_ids_readers_filter_on_and_live_tables_get_them(self):
        """11-21 live: ``record_links`` was indexed by its row ids only while the guarded
        view shows the text ids, so a reader's ``from_id = ...`` or ``to_id = ...`` scanned
        all 25M links (45-58 s) and a records join timed out. A build makes both id
        indexes; ``ensure_indexes`` adds any the live tables lack (concurrently), makes an
        invalid one again, does nothing when all are there and stops on a short disk."""
        import psycopg
        gb = 1024 ** 3

        def shapes(c):
            defs = [re.sub(r"\s+", "", d).lower() for (d,) in c.execute(
                "SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i WHERE i.indrelid = "
                "'biz.record_links'::regclass AND i.indisvalid").fetchall()]
            return {k for k in ("from_id, link_type", "to_id, link_type")
                    if any(d.endswith(gr._index_shape("btree", k)) for d in defs)}
        with psycopg.connect(self.dsn, autocommit=True) as c:
            self.assertEqual(len(shapes(c)), 2)          # the build made both
            names = [n for (n,) in c.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'biz' AND tablename = "
                "'record_links' AND (indexdef LIKE '%(from_id, link_type)%' OR indexdef LIKE "
                "'%(to_id, link_type)%')").fetchall()]
            self.assertEqual(len(names), 2)
            for name in names:
                c.execute(f'DROP INDEX biz."{name}"')
            # a concurrent build cancelled midway leaves an invalid index of the shape
            with psycopg.connect(self.dsn) as writer:
                writer.execute("UPDATE biz.record_links SET tier = tier WHERE false")
                c.execute("SET statement_timeout = '300ms'")
                with self.assertRaises(psycopg.errors.QueryCanceled):
                    c.execute("CREATE INDEX CONCURRENTLY ON biz.record_links (to_id, link_type)")
                c.execute("RESET statement_timeout")
                writer.rollback()
            self.assertTrue(c.execute(
                "SELECT 1 FROM pg_index WHERE indrelid = 'biz.record_links'::regclass AND "
                "NOT indisvalid").fetchone())
            self.assertEqual(len(shapes(c)), 0)
        with self.assertRaisesRegex(RuntimeError, "below"):
            gr.ensure_indexes(self.dsn, free=lambda p: 1 * gb, wait=0)
        made = gr.ensure_indexes(self.dsn, free=lambda p: 500 * gb)
        self.assertEqual(sorted(made), ["record_links (from_id, link_type)",
                                        "record_links (to_id, link_type)"])
        self.assertEqual(gr.ensure_indexes(self.dsn, free=lambda p: 500 * gb), [])
        with psycopg.connect(self.dsn, autocommit=True) as c:
            self.assertEqual(len(shapes(c)), 2)
            self.assertFalse(c.execute(               # the invalid one is gone, not doubled
                "SELECT 1 FROM pg_index WHERE indrelid = 'biz.record_links'::regclass AND "
                "NOT indisvalid").fetchone())
        # the shape the catalog tells the model to use reads the links by that index
        # through the guarded view (a JOIN between the two security-barrier views cannot)
        from operating import ontology_tools_sql_catalog as cat
        self.assertIn("from_id (or to_id) = ANY(ARRAY(SELECT record_id FROM records",
                      cat.RULES_GENERIC)
        shape = ("SELECT link_type, count(*) FROM record_links WHERE from_id = ANY(ARRAY("
                 "SELECT record_id FROM records WHERE type = 'Customer')) GROUP BY 1")
        joined = ("SELECT l.link_type, count(*) FROM records r JOIN record_links l ON "
                  "l.from_id = r.record_id WHERE r.type = 'Customer' GROUP BY 1")
        self.assertEqual(sorted(self.rows(shape)), sorted(self.rows(joined)))
        from operating.pg import door, sql_views
        with psycopg.connect(self.dsn, autocommit=True) as c:
            classes = c.execute("SELECT audience_keys, rec_marks FROM biz.access_classes "
                                "WHERE NOT blocked").fetchall()
            scope = door.reader_scope(
                c, "biz", audience_keys={k for ks, _ in classes for k in ks},
                marking_sets={m for _, ms in classes for m in ms}, held=(),
                holds_every_doc=True)
            with c.transaction():
                for k, v in scope["settings"].items():
                    c.execute("SELECT set_config(%s, %s, true)", (k, v))
                c.execute("SET LOCAL enable_seqscan = off")
                c.execute(f"SET LOCAL search_path = {sql_views.guarded_schema('biz')}, "
                          "pg_catalog")
                c.execute(f"SET LOCAL ROLE {sql_views.ROLE}")
                plan = "\n".join(r[0] for r in c.execute("EXPLAIN " + shape).fetchall())
        self.assertRegex(plan, r"Index (Only )?Scan using \S*from_id\S* on record_links|"
                               r"Bitmap Index Scan on \S*from_id", plan)
        # the fenced lateral the catalog tells the model for a row-by-row join (two hops:
        # the link, then the record at its other end): the join's rows, each read by index
        self.assertIn("LEFT JOIN LATERAL (SELECT l.to_id FROM record_links l WHERE l.from_id "
                      "= r.record_id AND l.link_type = '<link>' OFFSET 0) l ON true",
                      cat.RULES_GENERIC)
        fenced = ("SELECT r.label, l.link_type, o.label FROM records r JOIN LATERAL (SELECT "
                  "l.link_type, l.to_id FROM record_links l WHERE l.from_id = r.record_id "
                  "OFFSET 0) l ON true JOIN LATERAL (SELECT o.label FROM records o WHERE "
                  "o.record_id = l.to_id OFFSET 0) o ON true WHERE r.type = 'Customer'")
        plain = ("SELECT r.label, l.link_type, o.label FROM records r JOIN record_links l ON "
                 "l.from_id = r.record_id JOIN records o ON o.record_id = l.to_id WHERE "
                 "r.type = 'Customer'")
        self.assertTrue(self.rows(plain))
        self.assertEqual(sorted(self.rows(fenced)), sorted(self.rows(plain)))
        with psycopg.connect(self.dsn, autocommit=True) as c:
            with c.transaction():
                for k, v in scope["settings"].items():
                    c.execute("SELECT set_config(%s, %s, true)", (k, v))
                c.execute(f"SET LOCAL search_path = {sql_views.guarded_schema('biz')}, "
                          "pg_catalog")
                c.execute(f"SET LOCAL ROLE {sql_views.ROLE}")
                c.execute("SET LOCAL enable_seqscan = off")   # the fixture is tiny
                plan = "\n".join(r[0] for r in c.execute("EXPLAIN " + fenced).fetchall())
        self.assertRegex(plan, r"Index (Only )?Scan using \S*from_id\S* on record_links", plan)
        self.assertRegex(plan, r"Index (Only )?Scan using \S*record_id\S* on records", plan)
        # the refresh makes a missing one after each run; a failure there (a role that
        # does not own the table, a short disk) is reported, the refresh still done
        import datetime as dt
        with mock.patch.object(gr, "ensure_indexes",
                               side_effect=RuntimeError("records: 1.0 GB free, below")):
            out = gr.follow(self.dsn, dt.date(2026, 11, 21))
        self.assertIn("links_read", out["records"])
        self.assertIn("below", out["indexes_failed"])


def _with_unlabelled():
    """The fixture's store plus invoices no display field names: one numbered only by an
    identifier, one whose only identifier is finance-marked, one with a padded number."""
    s, ids = _plain_build()
    bare = obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", "i7",
               {"invoice_date": "2026-11-05", "total_amount": ("7.00", "CAD")})
    s.ident(bare, "petra_invoice", "IN-7  ", role="alias", prop="sage_invoice_number")
    marked = obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", "i8",
                 {"invoice_date": "2026-11-06"})
    handle, lineage, _ = s.handles[marked]
    s.conn.execute("INSERT INTO identifiers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("petra_invoice", "IN-8", "IN-8", "primary", marked, "number", handle,
                    "row.number", lineage, '["finance"]', ev.T0, None))
    obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", "i9",
        {"invoice_number": "  IN-9   ", "invoice_date": "2026-11-07"})
    # list items whose columns came as one JSON text (SharePoint's ``fields``); one not JSON
    obj(s, "SharePointListItem", "gen.sharepoint.list_items", "r2",
        {"fields": json.dumps({"Title": " WH pick 42 ", "Status_x0020_": "Open"})},
        handle="sharepoint://site1/list/r2")
    obj(s, "SharePointListItem", "gen.sharepoint.list_items", "r3",
        {"fields": "{not json"}, handle="sharepoint://site1/list/r3")
    # a date column exported as an Excel serial is no title
    obj(s, "SharePointListItem", "gen.sharepoint.list_items", "r4",
        {"fields": json.dumps({"Title": "45678"})}, handle="sharepoint://site1/list/r4")
    # 11-21 review: a FileMaker row id is the only primary identifier of many POs: never
    # their label (5,669 live would have read "5292" for PO5267); the business number is
    fm = obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", "f1", {})
    s.ident(fm, "fm_id", "5292", role="primary", prop="fm_id")
    fm_po = obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", "f2", {})
    s.ident(fm_po, "fm_id", "5293", role="primary", prop="fm_id")
    s.ident(fm_po, "petra_po", "PO#5267", role="alias", prop="po_number")
    # a carrier's tracking number only when nothing else names it, said as one
    tr = obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", "t1", {})
    s.ident(tr, "tracking_number", "1Z999", role="primary", prop="tracking")
    tr2 = obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", "t2", {})
    s.ident(tr2, "tracking_number", "1Z998", role="primary", prop="tracking")
    s.ident(tr2, "lot_number", "390373", role="alias", prop="lot")
    # 11-21: a value that is not a name never labels the record: placeholders, a lone quote,
    # too short; a date is allowed in a display field (a record's own SO number) and in an
    # identifier, only free text (title, name, a list item's Title) refuses one
    for key, junk in (("j1", "TBD"), ("j2", " NA "), ("j3", "'"), ("j4", "2026-11-05"),
                      ("j5", "5/11/2026"), ("j6", "Nov 5, 2026"), ("j7", "x"), ("j8", "--")):
        obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", key,
            {"invoice_number": junk, "invoice_date": "2026-11-07"})
    j9 = obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", "j9", {"invoice_number": "tbd"})
    s.ident(j9, "petra_invoice", "N/A", role="primary", prop="sage_invoice_number")
    obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", "j10", {"invoice_number": "A1"})
    # Sage shipment and receipt headers (live types Shipment / Receipt) carry only raw
    # columns: named by their own number
    obj(s, "Shipment", "portal.sage_oe_shipment_header", "sh1",
        {"sage_shipment_number": "SHI0001", "customer_po": "P1"})
    obj(s, "Shipment", "portal.sage_oe_shipment_header", "sh2",
        {"sage_shipment_number": " 0042 "})
    obj(s, "Receipt", "portal.sage_po_receipt_header", "rc1", {"sage_receipt_number": "RCP77"})
    obj(s, "Shipment", "portal.sage_oe_shipment_header", "sh3",
        {"sage_shipment_number": "n/a"})
    # a record's own identifier is its name whatever it looks like (live 11-21: SO
    # "17-06-2025", customer PO "12/11/2018", lot "09/08/22", CAS "9005-65-6"); a one-character
    # code is kept; true junk (a placeholder) is not
    for key, kind, raw in (("d1", "so_number", "17-06-2025"), ("d2", "customer_po", "12/11/2018"),
                           ("d3", "lot_number", "09/08/22"), ("d4", "cas_number", "9005-65-6"),
                           ("d5", "lot_number", "X"), ("d6", "cas_number", "9"),
                           ("d7", "cas_number", "TBD")):
        o = obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", key, {})
        s.ident(o, kind, raw, role="primary", prop=kind)
    # 11-21 live: 107k RelSageApIbd rows have transnbr 0 (unset) and the real batch in
    # cntbtch: a zero value is no name, the next label field names the record. A non-zero
    # one-character value is kept.
    obj(s, "RelSageApIbd", "gen.sage.ap_ibd", "a1", {"transnbr": "0", "cntbtch": "5884"})
    obj(s, "RelSageApIbd", "gen.sage.ap_ibd", "a2", {"transnbr": "00.0", "cntbtch": "0"})
    obj(s, "RelSageApIbd", "gen.sage.ap_ibd", "a3", {"transnbr": "7", "cntbtch": "5885"})
    z = obj(s, "SalesInvoice", "portal.sage_oe_invoice_header", "z1", {})
    s.ident(z, "lot_number", "0", role="primary", prop="lot")
    # a list item's Title is free text: a date (any month name) is no title, "Foo 5, 2026" is
    for key, title in (("r5", "5/11/2026"), ("r6", "Nov 5, 2026"), ("r7", "Foo 5, 2026"),
                       ("r8", "March 3, 2026")):
        obj(s, "SharePointListItem", "gen.sharepoint.list_items", key,
            {"fields": json.dumps({"Title": title})}, handle=f"sharepoint://site1/list/{key}")
    s.conn.commit()
    return s, ids


_plain_build = build


class Labels(Fixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with mock.patch(f"{__name__}.build", _with_unlabelled):
            super().setUpClass()

    def label_of(self, key, actor=FULL):
        return self.rows("SELECT label FROM records WHERE record_id = "
                         f"'SalesInvoice:portal.sage_oe_invoice_header:{key}'", actor)

    def test_a_record_no_display_field_names_is_labelled_by_title_name_or_identifier(self):
        """11-21 live: 3,735 expense receipts and 10,900 Sage/FileMaker POs had no label
        (no display field, or none holding a value) and Sage labels were padded, so label
        ILIKE and plx.search's records leg could not find them by name or number. A build labels them by their title or name field as
        any reader of them reads it, else their first current unmarked identifier, trimmed;
        ``relabel`` labels the served rows that have none in place."""
        import psycopg
        self.assertEqual(self.label_of("i7"), [["IN-7"]])        # its identifier, trimmed
        self.assertEqual(self.label_of("i7", OPS), [["IN-7"]])
        self.assertEqual(self.label_of("i8"), [[None]])          # a marked one never labels
        self.assertEqual(self.label_of("i9"), [["IN-9"]])        # padded display value
        self.assertEqual(self.label_of("f1"), [[None]])          # a row id names nothing
        self.assertEqual(self.label_of("f2"), [["PO#5267"]])     # its PO number, not 5293
        self.assertEqual(self.label_of("t1"), [["Tracking 1Z999"]])
        self.assertEqual(self.label_of("t2"), [["Lot 390373"]])
        for key in ("j1", "j2", "j3", "j7", "j8", "j9", "d7"):
            self.assertEqual(self.label_of(key), [[None]], key)   # no name: no label
        for key, label in (("j4", "2026-11-05"), ("j5", "5/11/2026"), ("j6", "Nov 5, 2026"),
                           ("d1", "SO 17-06-2025"), ("d2", "Customer PO 12/11/2018"),
                           ("d3", "Lot 09/08/22"), ("d4", "9005-65-6"), ("d5", "Lot X"),
                           ("d6", "9")):       # own numbers and display values stay names
            self.assertEqual(self.label_of(key), [[label]], key)
        self.assertEqual(self.label_of("j10"), [["A1"]])
        self.assertEqual(self.label_of("z1"), [[None]])             # a zero identifier names nothing
        for typ, key, label in (("Shipment", "sh1", "Shipment SHI0001"),
                                ("Shipment", "sh2", "Shipment 0042"),
                                ("Receipt", "rc1", "Receipt RCP77"),
                                ("Shipment", "sh3", None)):
            self.assertEqual(self.rows("SELECT label FROM records WHERE record_id LIKE "
                                       f"'{typ}:%:{key}'"), [[label]], key)
        hit = self.search({"query": "IN-7", "sources": ["records"]})["documents"]
        self.assertIn("IN-7", [d["title"]["untrusted_text"] for d in hit])
        # the served rows as an older build left them: no display field, no label
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("UPDATE biz.record_types SET label_fields = '{}' WHERE type = "
                      "'SharePointListItem'")
            c.execute("UPDATE biz.records SET label = NULL WHERE type = 'SharePointListItem'")
            self.assertEqual(c.execute("SELECT count(*) FROM biz.records WHERE type = "
                                       "'SharePointListItem'").fetchone()[0], 8)
            c.execute("UPDATE biz.records SET label = 'tbd' WHERE record_id LIKE "
                      "'SalesInvoice:%:i7'")      # a junk label an older build wrote
            # the AP batch lines as the live catalog declares them; their labels as stored
            c.execute("UPDATE biz.record_types SET label_fields = '{transnbr,cntbtch,cntitem}' "
                      "WHERE type = 'RelSageApIbd'")
            c.execute("UPDATE biz.records SET label = NULL WHERE record_id LIKE "
                      "'RelSageApIbd:%:a2' OR record_id LIKE 'RelSageApIbd:%:a3'")
            c.execute("UPDATE biz.records SET label = '0' WHERE record_id LIKE "
                      "'RelSageApIbd:%:a1'")      # the stored zero: the batch names it
            c.execute("UPDATE biz.records SET label = '5/11/2026' WHERE record_id LIKE "
                      "'SharePointListItem:%:r5'")   # a stored date title
        got = gr.relabel(self.dsn, step=2)                       # many short batches
        self.assertEqual(got, {"cleared": 3, "labelled": 6})
        for key, label in (("a1", "5884"), ("a2", None), ("a3", "5885")):   # a2: all zero; a3: "7" is under two characters in a display field
            self.assertEqual(self.rows("SELECT label FROM records WHERE record_id LIKE "
                                       f"'RelSageApIbd:%:{key}'"), [[label]], key)
        self.assertEqual(self.label_of("i7"), [["IN-7"]])
        with psycopg.connect(self.dsn, autocommit=True) as c:
            self.assertEqual(sorted(c.execute(
                "SELECT label FROM biz.records WHERE type = 'SharePointListItem'").fetchall(),
                key=str), [("Foo 5, 2026",), ("List row",), ("WH pick 42",)]
                + [(None,)] * 5)
        self.assertEqual(gr.relabel(self.dsn), {"cleared": 0, "labelled": 0})
        # the list row stays a full reader's only, labelled or not
        self.assertEqual(self.rows("SELECT label FROM records WHERE type = "
                                   "'SharePointListItem'", OPS), [])


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


class RefreshOverAnOlderBuild(Fixture, unittest.TestCase):
    def test_a_refresh_over_a_table_built_before_withholds_and_never_shows_unmasked(self):
        """The served table was built under the old rule (no ``fields_open``, a marked
        field's markings in the record's class, no column class, views masking nothing).
        The first refresh after the change adds the column and reads changed records under
        the new rule; the views are made again with ``fields`` masked in the same
        transaction, so no reader ever reads the row with its marked field unmasked."""
        import psycopg
        from operating.pg import door
        from operating.pg import business_views as bv
        S = bv.SCHEMA
        with psycopg.connect(self.dsn, autocommit=True) as c:
            for name in ("record_links", "records"):
                gv.sql_views.drop(c, name, S)
            c.execute(f"ALTER TABLE {S}.records DROP COLUMN fields_open")
            c.execute(f"UPDATE {S}.records SET col_class = 0")
            door.recount(c, S, "records")
            gv.make_views(c, S)
            self.assertEqual(door.masked_columns(c, S, "records"), [])
            # every record read again by the next refresh
            c.execute(f"UPDATE {S}.state SET notes = jsonb_set(notes, '{{marks,ver}}', "
                      "'0') WHERE view = 'records'")
        gr.refresh(self.dsn, S)
        with psycopg.connect(self.dsn) as c:
            self.assertEqual(door.masked_columns(c, S, "records"), ["fields"])
            (masked,) = c.execute(f"SELECT masked FROM {S}.door_views WHERE view = "
                                  "'records'").fetchone()
            self.assertEqual(list(masked), ["fields"])
        sql = ("SELECT label, fields->>'total_amount' FROM records WHERE type = "
               "'SalesInvoice' ORDER BY label")
        self.assertEqual(self.rows(sql), [["IN-1", "4444.44"], ["IN-2", "12.50"]])
        self.assertEqual(self.rows(sql, OPS), [["IN-1", None], ["IN-2", "12.50"]])
        self.assertNotIn("4444.44", json.dumps(self.call({"sql": "SELECT * FROM records LIMIT 500"},
                                                    OPS)))


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
        from operating import disk_budget
        floor = disk_budget.floor_gb()   # the one floor (12 GB of 4 batches' temp on top)
        frees, slept = [(floor + 5) * gb, (floor + 9) * gb, (floor + 25) * gb], []
        gr.room(NoSetting(), 4, free=lambda p: frees.pop(0), sleep=slept.append, wal=False)
        self.assertEqual(len(slept), 2)
        with self.assertRaisesRegex(RuntimeError, f"below the {floor + 12:.0f} GB"):
            gr.room(NoSetting(), 4, wait=0, free=lambda p: (floor + 11) * gb, sleep=slept.append,
                    wal=False)
        with self.assertRaisesRegex(RuntimeError, "cannot be read"):
            gr.room(NoSetting(), 1, free=mock.Mock(side_effect=OSError("gone")), wal=False)
        with mock.patch.dict(os.environ, {"PLX_DISK_FLOOR_GB_DATA": "lots"}), \
                self.assertRaisesRegex(RuntimeError, "PLX_DISK_FLOOR_GB_DATA"):
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
        # with the business types: every system's copy of an order, invoice, product, batch,
        # customer or supplier is a record of its own (its own status words), only the
        # communications tables' types stay out
        business = gr.type_choice(True, True, ["RelSage"], business=True)
        self.assertEqual(business["skip_types"], gr.comms_types())
        self.assertEqual(set(gr.comms_types()),
                         {"Email", "TeamsChatMessage", "CalendarEvent", "MeetingTranscript",
                          "Attachment"})   # the attachments table is a communications table too
        for kept in ("SalesOrder", "PurchaseOrder", "PurchaseOrderLine", "Batch", "Customer"):
            self.assertIn(kept, covered)
            self.assertNotIn(kept, business["skip_types"])
        self.assertIn("NOT LIKE 'RelSage%'", gr.types_sql(business))

    def test_a_rebuild_that_leaves_out_served_types_is_named(self):
        """11-19 live: a rebuild with --uncovered and without --business left every
        customer, product, order, invoice, supplier, PO and batch out of records."""
        full = gr.type_choice(False, False)
        business = gr.type_choice(True, True, business=True)
        uncovered = gr.type_choice(True, True)
        lost = gr.narrowed(business, uncovered)
        for gone in ("Customer", "Product", "SalesOrder", "SalesInvoice", "Supplier",
                     "PurchaseOrder", "Batch"):
            self.assertIn(gone, lost)
        self.assertNotIn("Email", lost)   # the business build left the comms types out too
        self.assertEqual(gr.narrowed(uncovered, business), [])   # widening loses nothing
        self.assertEqual(gr.narrowed(business, business), [])
        self.assertIn("Rel*", gr.narrowed(full, gr.type_choice(True, False)))
        self.assertEqual(gr.narrowed(full, gr.type_choice(False, False, ["RelSage"])),
                         ["RelSage*"])
        # a prefix the served build already left out (all mirrors, or a shorter prefix)
        self.assertEqual(gr.narrowed(gr.type_choice(True, False),
                                     gr.type_choice(True, False, ["RelSage"])), [])
        self.assertEqual(gr.narrowed({"skip_prefixes": ["Rel"]}, {"skip_prefixes": ["RelSage"],
                                                                   "skip_types": ["RelSageX"]}),
                         [])


class NarrowBuild(Fixture, unittest.TestCase):
    def test_a_build_narrower_than_the_served_one_is_refused_before_it_writes(self):
        import psycopg
        q = "SELECT count(*), max(built_at) FROM biz.records, biz.state WHERE view = 'records'"
        with psycopg.connect(self.dsn) as c:
            before = c.execute(q).fetchone()
        with self.assertRaisesRegex(ValueError, "Customer.*--narrow"):
            gr.build(self.dsn, workers=1, uncovered=True)
        with psycopg.connect(self.dsn) as c:
            self.assertEqual(c.execute(q).fetchone(), before)
            self.assertIsNone(c.execute("SELECT to_regclass('biz.records_new')").fetchone()[0])
        gr.build(self.dsn, workers=1, uncovered=True, narrow=True)   # on purpose
        with psycopg.connect(self.dsn) as c:
            notes = gr.served_notes(c, "biz")
        self.assertIn("Customer", notes["skip_types"])
        gr.build(self.dsn, workers=1, uncovered=True, mirrors=True, business=True)  # wider


class Coverage(Fixture, unittest.TestCase):
    def test_coverage_counts_what_the_door_reaches_and_alarms_on_a_drop_or_new_gap(self):
        import psycopg
        decide(self.dsn, None)   # every version decided: the list row is reachable
        report = cov.check(self.dsn)
        by_type = {r["type"]: r for r in report["types"]}
        # served: what the sources hold (not excluded); reachable: a full reader through
        # the AI's door (Freightcom's customer too: read like any other since 11-23)
        self.assertEqual({t: (r["served"], r["reachable"]) for t, r in by_type.items()},
                         {"Customer": (4, 4), "SalesInvoice": (2, 2), "MCTask": (71, 71),
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

    def test_a_failed_records_read_is_a_failed_run_not_every_type_unreachable(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        decide(self.dsn, None)
        real = cov._door_counts

        def broken(pg, s, settings, views):
            got = real(pg, s, settings, views)
            got["views"]["records"], got["records"] = None, {}
            return got
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cov, "RETRY_WAIT_S", 0):
            home = Path(tmp) / cov.STATUS_DIR
            cov.run(self.dsn, Path(tmp))
            good = (home / "baseline.json").read_text()
            with mock.patch.object(cov, "_door_counts", broken):
                bad = cov.run(self.dsn, Path(tmp))
            self.assertIs(bad["ok"], False)
            self.assertIn("records view", bad["error"])
            self.assertEqual((home / "baseline.json").read_text(), good)
