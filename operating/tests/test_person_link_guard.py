"""The read-time same-person guard (``operating.pg.person_link_guard``) at its exits.

A served reader (``PgStoreReader``, the proactive and watcher connections) never sees a
``Contact.same_person_as`` link whose contact end is not a Person-type contact (Company,
blank, missing) nor a firm plus-address ``Person.same_person_as`` link, through any read:
record pages (``plx.around``), the traverse and the raw link reads the other consumers
make; a Person-type contact's link and the lead links stay. Writers are never guarded.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ontology import store_ddl
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_traverse as tt
from operating.pg import person_link_guard as plg
from tests.test_ontology_tools_evidence import CONTRACTS, NOW, T0 as NOW_T, Store, _type

PERSON, PLUS, SUB = "Person:people.person:p-1", "Person:people.person:p-2", \
    "Person:people.person:p-3"
COMPANY, BLANK, UNTYPED, HUMAN = ("Contact:bc.contacts:c-company", "Contact:bc.contacts:c-blank",
                                  "Contact:bc.contacts:c-untyped", "Contact:bc.contacts:c-person")
OTHER = "Contact:bc.contacts:c-other"   # a Person-type contact linked to a Company contact
# a plus-address pair joined by nothing but its firm plus-address link
ALONE, PLUS2 = "Person:people.person:p-4", "Person:people.person:p-5"
HIDDEN = {"l-company", "l-blank", "l-untyped", "l-plus", "l-to-company", "l-plus-verified",
          "l-plus-alone"}
SHOWN = {"l-human", "l-sub", "l-plus-lead", "l-plus-reviewed"}


def _link(lid, src, dst):
    return {"id": lid, "from": src, "to": dst, "description": "Fixture link.",
            "bindings": [{"kind": "reviewed", "tier_ceiling": "reviewed"}]}


DOMAIN = {
    "format": "plx-ontology-domain/v1", "domain": "fixture", "origin": "curated",
    "description": "Same-person guard fixture.",
    "object_types": [
        _type("Person", [("email", "text"), ("name", "text")], [("people.person", "graph_mail")],
              display=["name"]),
        _type("Contact", [("email", "text"), ("contact_type", "enum"), ("name", "text")],
              [("bc.contacts", "bc")], display=["name"]),
    ],
    "link_types": [_link("Contact.same_person_as", "Contact", "Person"),
                   _link("Person.same_person_as", "Person", "Person")],
}
SCHEMA = ot.SchemaView.from_documents([DOMAIN])
# the same domain with ``Person.same_person_as`` an identity type (one object type at both
# ends and a ``same_record`` binding), so plx.identity reads it
ID_DOMAIN = json.loads(json.dumps(DOMAIN))
ID_DOMAIN["link_types"][1]["bindings"].append({"kind": "same_record",
                                               "tier_ceiling": "verified"})
ID_SCHEMA = ot.SchemaView.from_documents([ID_DOMAIN])
POLICY = ot.ReadPolicy(tenant="plx", audiences={"bc:*": {"ops", "full"},
                                                "mail:*": {"ops", "full"}}, markings={})
OWNER = Actor("plx", frozenset({"full"}))
OPS = Actor("plx", frozenset({"ops"}))


def _reason(feature):
    return [{"feature": feature, "agrees": True, "citations": []}]


def build(path: Path) -> sqlite3.Connection:
    s = Store()
    s.conn.execute("UPDATE store_meta SET value = ? WHERE key = 'schema_digest'",
                   ("sha256:" + SCHEMA.digest_hex,))
    for oid, email in ((PERSON, "ann@acme.example"), (PLUS, "ann+orders@acme.example"),
                       (SUB, "ann@mail.acme.example"), (ALONE, "bo@acme.example"),
                       (PLUS2, "bo+ship@acme.example")):
        s.obj(oid, "mail:mbx-a", f"graph-mail://mbx-a/{oid[-3:]}@ck1", "mail")
        s.val(oid, "email", "text", email)
        s.val(oid, "name", "text", "Ann " + oid[-3:])
    for oid, kind in ((COMPANY, "Company"), (BLANK, "  "), (UNTYPED, None),
                      (HUMAN, " Person "), (OTHER, "Person")):
        s.obj(oid, "bc:co/contacts", f"bc://production/co/contacts/{oid[-9:]}@1", "bc_crm")
        s.val(oid, "email", "text", "ann@acme.example")
        s.val(oid, "name", "text", "Contact " + oid[-9:])
        if kind is not None:
            s.val(oid, "contact_type", "enum", kind)
    for lid, src, dst in (("l-company", COMPANY, PERSON), ("l-blank", BLANK, PERSON),
                          ("l-untyped", UNTYPED, PERSON), ("l-human", HUMAN, PERSON),
                          ("l-to-company", OTHER, COMPANY)):
        s.link(lid, "Contact.same_person_as", src, dst, "corroborated", "mention",
               _reason("address"))
    s.link("l-plus", "Person.same_person_as", PLUS, PERSON, "corroborated", "mention",
           _reason("plus_address"))
    s.link("l-plus-alone", "Person.same_person_as", PLUS2, ALONE, "corroborated", "mention",
           _reason("plus_address"))
    s.link("l-plus-verified", "Person.same_person_as", PLUS, SUB, "verified", "reviewed",
           _reason("plus_address"))
    s.link("l-plus-lead", "Person.same_person_as", SUB, PLUS, "lead", "mention",
           _reason("plus_address"), why_not=[{"feature": "plus_tag", "agrees": None}])
    s.link("l-plus-reviewed", "Person.same_person_as", PERSON, SUB, "verified", "reviewed",
           _reason("plus_address"))
    s.conn.execute("UPDATE links SET tier = 'reviewed', reviewed_by = 'ops@plx', reviewed_at "
                   "= ?, review_action_id = 'act-1' WHERE link_id = 'l-plus-reviewed'", (NOW_T,))
    s.link("l-sub", "Person.same_person_as", SUB, PERSON, "lead", "mention",
           _reason("sub_domain"), why_not=[{"feature": "sub_domain_only", "agrees": None}])
    s.conn.commit()
    with sqlite3.connect(path) as disk:
        s.conn.backup(disk)
    disk.close()
    conn = sqlite3.connect(path, check_same_thread=False)
    return conn


class GuardSqlTests(unittest.TestCase):
    """The rewrite's contract on the SQL text (no server)."""

    def test_each_read_of_the_relations_is_guarded_and_nothing_else(self):
        sub = ("(SELECT * FROM links plx_pg WHERE plx_pg.link_type NOT IN ('Contact.same_person_as',"
               " 'Person.same_person_as') UNION ALL SELECT * FROM links plx_pg WHERE")
        for sql, alias in (("SELECT * FROM links l WHERE l.tier = ?", " l WHERE"),
                           ("SELECT * FROM links AS x", " AS x"),
                           ("SELECT count(*) FROM links WHERE link_type = ?", " AS links WHERE"),
                           ("SELECT a.x FROM objects a JOIN links ON 1", " AS links ON"),
                           ("SELECT * FROM objects, links", " AS links"),
                           ("select * from LINKS, objects", " AS links,"),
                           ("SELECT 1 FROM objects o JOIN t ON o.a = t.a, links", " AS links")):
            got = plg.guard(sql)
            self.assertIn(sub, got, sql)
            self.assertIn(")" + alias, got, sql)
        rows = plg.guard("SELECT from_object FROM link_rows WHERE to_object = ?")
        self.assertIn("(SELECT * FROM link_rows plx_pg WHERE plx_pg.link_type NOT IN", rows)
        self.assertIn("UNION ALL SELECT * FROM link_rows plx_pg WHERE", rows)
        self.assertIn(") AS link_rows WHERE to_object", rows)
        numbered = plg.guard("SELECT l.rowid, l.link_id FROM link_rows AS l WHERE l.rowid > ?")
        self.assertEqual(numbered.count("l.row_no"), 2)
        self.assertNotIn("rowid", numbered)
        for untouched in ("SELECT * FROM ontology.links", "SELECT 'FROM links' AS t",
                          "SELECT links FROM objects", "SELECT * FROM links_apply",
                          "SELECT * FROM objects WHERE object_type = 'links'",
                          "SELECT a, links FROM objects", "SELECT f(a, links) FROM t"):
            self.assertEqual(plg.guard(untouched), untouched)

    def test_a_writer_and_an_unguarded_connection_read_as_written(self):
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection.__new__(PgCompatConnection)
        conn.link_guard = True
        for write in ("INSERT INTO links SELECT * FROM links", "DELETE FROM links",
                      "UPDATE links SET tier = 'lead'", " /* c */ DELETE FROM links",
                      "CREATE TEMP TABLE t AS SELECT * FROM links", None):
            self.assertEqual(conn._guarded(write), write)
        # every read is decided by its head alone: an item-gated read (CASE ... END, the
        # words the retry rule turns down) is guarded as any other
        from operating.ontology_tools_traverse import _item_gate_sql
        gated = ("SELECT l.* FROM links l WHERE NOT EXISTS (SELECT 1 FROM json_each("
                 f"l.evidence_handles) h WHERE NOT {_item_gate_sql('g', 'h.value')})")
        for read in ("SELECT * FROM links", gated, "-- note\n  select * from link_rows",
                     "/* x */ WITH a AS (SELECT * FROM links) SELECT * FROM a",
                     "VALUES ((SELECT count(*) FROM links))",
                     "SELECT CASE WHEN 1 THEN 'SET' END FROM links INTO_x"):
            self.assertIn("UNION ALL SELECT * FROM link", conn._guarded(read), read)
        # a read that ends in a write on a reader fails in Postgres, never runs unguarded
        self.assertIn("DELETE FROM (SELECT * FROM links plx_pg",
                      conn._guarded("SELECT 1; DELETE FROM links"))
        conn.link_guard = False
        self.assertEqual(conn._guarded("SELECT * FROM links"), "SELECT * FROM links")

    def test_every_served_reader_opens_guarded_and_no_writer_does(self):
        from operating.pg import ontology_reader, sqlite_compat
        seen = []

        class Spy:
            def __init__(self, dsn, **kw):
                seen.append(kw.get("link_guard", False))
                raise RuntimeError("stop")

        with mock.patch.object(ontology_reader, "PgCompatConnection", Spy):
            with self.assertRaises(RuntimeError):
                ontology_reader.PgStoreReader.open("postgresql:///x", only=False)
        from operating import proactive_engine
        with mock.patch.object(sqlite_compat, "PgCompatConnection", Spy), \
                mock.patch.object(ontology_reader, "pg_only", return_value=False), \
                mock.patch.object(ontology_reader, "serves", return_value=True), \
                mock.patch.object(ontology_reader, "dsn", return_value="postgresql:///x"):
            with self.assertRaises(RuntimeError):
                proactive_engine._connect(Path("/nonexistent/store.sqlite"))
        self.assertEqual(seen, [True, True])
        root = Path(__file__).resolve().parents[2]
        for writer in ("ontology/delta.py", "ontology/pg_step_inplace.py",
                       "operating/pg/whole_steps.py", "operating/pg/people_scope.py",
                       "ontology/link_keys.py"):
            self.assertNotIn("link_guard", (root / writer).read_text("utf-8"), writer)


class _OnPostgres(unittest.TestCase):
    """The fixture loaded into a throwaway Postgres database, read as the host reads it."""

    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_store_mode
        cls.dir = Path(tempfile.mkdtemp(prefix="person-guard-"))
        path = cls.dir / "store.sqlite"
        cls.source = build(path)
        cls.dsn = pg_store_mode._load(str(path), cls.source)

    def reader(self, guard=True):
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection(self.dsn, link_guard=guard)
        reader = PgStoreReader(conn, ot.StoreReader(self.source))
        self.addCleanup(conn.close)
        return reader

    def traverse(self, reader):
        base = ot.OntologyTools(reader, POLICY, item_gate=lambda a, h: True, schema=SCHEMA,
                                grammar=None, contracts=CONTRACTS, clock=lambda: NOW)
        return tt.TraverseTools(base)


class GuardOnPostgresTests(_OnPostgres):
    """Raw reads and record pages."""

    def test_raw_reads_of_every_shape_skip_the_hidden_links(self):
        plain = self.reader(guard=False).conn
        everything = {r[0] for r in plain.execute("SELECT link_id FROM links")}
        self.assertEqual(everything, HIDDEN | SHOWN)
        conn = self.reader().conn
        for sql in ("SELECT link_id FROM links", "SELECT l.link_id FROM links l",
                    "SELECT link_id FROM link_rows WHERE superseded_at IS NULL",
                    "SELECT x.link_id FROM objects o JOIN links x ON x.from_object = "
                    "o.object_id"):
            self.assertEqual({r[0] for r in conn.execute(sql)}, SHOWN, sql)
            self.assertEqual({r[0] for r in conn.stream(sql)}, SHOWN, sql)
        # a row number read through the guard (the proactive engine's ``touching``)
        got = conn.execute("SELECT l.rowid, l.link_id FROM link_rows l WHERE l.from_object = ?",
                           (HUMAN,)).fetchall()
        self.assertEqual([r[1] for r in got], ["l-human"])
        self.assertIsInstance(got[0][0], int)
        # the proactive engine's identity read and the party reads (link_rows by end)
        got = conn.execute("SELECT from_object, link_type, to_object, tier FROM link_rows "
                           "WHERE from_object IN (SELECT value FROM json_each(?)) AND "
                           "superseded_at IS NULL",
                           (json.dumps([COMPANY, BLANK, HUMAN, PLUS, SUB]),)).fetchall()
        self.assertEqual({(r[0], r[2]) for r in got},
                         {(HUMAN, PERSON), (SUB, PERSON), (SUB, PLUS)})

    def test_record_pages_show_no_hidden_link_to_any_reader(self):
        hops = {"Person": [("Contact.same_person_as", "reverse"),
                           ("Person.same_person_as", "forward"),
                           ("Person.same_person_as", "reverse")],
                "Contact": [("Contact.same_person_as", "forward")]}

        def page(tools, root, actor):
            found = set()
            for link_type, direction in hops[root.split(":", 1)[0]]:
                result = tools.call("plx.around", {"from": root, "path": [
                    {"link_type": link_type, "direction": direction}], "limit": 50, "tier_floor": "lead"},
                    actor)
                self.assertNotIn("error", result, json.dumps(result)[:2000])
                text = json.dumps(result)
                found |= {oid for oid in (PERSON, PLUS, SUB, COMPANY, BLANK, UNTYPED, HUMAN,
                                          OTHER) if oid != root and f'"{oid}"' in text}
            return found

        for actor in (OWNER, OPS):
            unguarded = self.traverse(self.reader(guard=False))
            self.assertEqual(page(unguarded, PERSON, actor),
                             {COMPANY, BLANK, UNTYPED, HUMAN, PLUS, SUB})
            tools = self.traverse(self.reader())
            # "Bc contacts" and "Same person as" on the Person page: the Person-type contact
            # and the leads only
            self.assertEqual(page(tools, PERSON, actor), {HUMAN, SUB})
            self.assertEqual(page(tools, PLUS, actor), {SUB})
            self.assertEqual(page(tools, SUB, actor), {PLUS, PERSON})
            # a Company, blank or untyped contact's page names no person
            for contact in (COMPANY, BLANK, UNTYPED):
                self.assertEqual(page(tools, contact, actor), set(), contact)
            self.assertEqual(page(tools, HUMAN, actor), {PERSON})


class GuardedToolReadsTests(_OnPostgres):
    """Every item-gated link read the tools make on a guarded reader: plx.identity, the
    plx.investigate document frontier and firm reads (``EvidenceTools._visible_links``),
    plx.aggregate's hidden-reason read and the case workspace's link paths. Each carries
    the gate's ``CASE ... END`` and skips the hidden links."""

    def tools(self, guard=True, schema=SCHEMA):
        from operating import ontology_tools_evidence as ev
        reader = self.reader(guard)
        base = ot.OntologyTools(reader, POLICY, item_gate=lambda a, h: True, schema=schema,
                                grammar=None, contracts=CONTRACTS, clock=lambda: NOW)
        return ev.EvidenceTools(base, reader=None)

    def gated(self, tools, actor, body):
        """``body(scope)`` under a real SQL item gate (as each tool registers it)."""
        return tools._with_gate(actor, lambda gate: body(
            tools.traverse._scope(actor, {}, gate)))

    def test_identity_shows_no_hidden_link(self):
        def edges(tools, root, actor):
            out = tools.call("plx.identity", {"object_ids": [root]}, actor)
            self.assertNotIn("error", out, json.dumps(out)[:2000])
            return {e["link_id"] for e in out["edges"]}

        for actor in (OWNER, OPS):
            plain = self.tools(False, ID_SCHEMA)
            self.assertEqual(edges(plain, PLUS, actor), {"l-plus", "l-plus-verified",
                                                         "l-plus-lead"})
            self.assertEqual(edges(plain, PLUS2, actor), {"l-plus-alone"})
            tools = self.tools(schema=ID_SCHEMA)
            self.assertEqual(edges(tools, PLUS, actor), {"l-plus-lead"})
            self.assertEqual(edges(tools, PLUS2, actor), set())
            self.assertEqual(edges(tools, PERSON, actor), {"l-sub", "l-plus-reviewed"})

    def test_investigate_link_reads_show_no_hidden_link(self):
        from operating import ontology_tools_evidence as ev
        ends = [PERSON, PLUS, SUB, COMPANY, BLANK, UNTYPED, HUMAN, OTHER, ALONE, PLUS2]
        for actor in (OWNER, OPS):
            plain, tools = self.tools(False), self.tools()
            rows = self.gated(plain, actor, lambda scope: plain._visible_links(scope, ends))
            self.assertEqual({r["link_id"] for r in rows}, HIDDEN | SHOWN)
            rows = self.gated(tools, actor, lambda scope: tools._visible_links(scope, ends))
            self.assertEqual({r["link_id"] for r in rows}, SHOWN)
            # each caller's own filter: the carried-message frontier, the firm reads
            for extra, params in ((ev.CARRIED_LINKS, None),
                                  ("AND l.link_type = :lt", {"lt": ev.CONTACT_PEOPLE_LINK}),
                                  ("AND l.link_type LIKE '%.same_person_as'", None)):
                rows = self.gated(tools, actor, lambda scope: tools._visible_links(
                    scope, ends, extra, params))
                self.assertFalse({r["link_id"] for r in rows} & HIDDEN, extra)
            # the document frontier (plx.investigate's linked records): a Company contact,
            # a contact tied only to one, and a plus-address twin reach nothing
            for root in (COMPANY, OTHER, PLUS2):
                found, _ = self.gated(tools, actor, lambda scope: tools._linked_documents(
                    scope, [root], ["bc", "mail"]))
                self.assertEqual(found, set(), root)
                found, _ = self.gated(plain, actor, lambda scope: plain._linked_documents(
                    scope, [root], ["bc", "mail"]))
                self.assertTrue(found, root)
            self.assertEqual(self.gated(tools, actor, lambda scope: tools._party_contacts(
                scope, [COMPANY, PERSON])), ([], False))

    def test_every_tool_read_of_the_links_is_rewritten(self):
        from operating.pg.sqlite_compat import PgCompatConnection
        seen: list[tuple[str, str]] = []
        real = PgCompatConnection._guarded

        def spy(conn, sql):
            out = real(conn, sql)
            seen.append((sql, out))
            return out

        tools = self.tools(schema=ID_SCHEMA)
        with mock.patch.object(PgCompatConnection, "_guarded", spy):
            tools.call("plx.identity", {"object_ids": [PLUS]}, OPS)
            self.gated(tools, OPS, lambda scope: (tools._linked_documents(scope, [PERSON],
                                                                          ["bc", "mail"]),
                                                  tools._party_contacts(scope, [PERSON])))
        reads = [(sql, out) for sql, out in seen
                 if plg.guard(sql) != sql and sql.lstrip().split(None, 1)[0].upper() in (
                     "SELECT", "WITH")]
        self.assertTrue(reads)
        self.assertTrue(any(" END" in sql for sql, _ in reads))
        # the link read that takes the links out of ``links`` first (a WITH, then joins them
        # to their ends) is among those rewritten, never skipped
        self.assertTrue(any(sql.lstrip().upper().startswith("WITH PAGE_LINKS") for sql, _ in reads))
        for sql, out in reads:
            self.assertNotEqual(out, sql, sql[:300])

    def test_aggregate_hidden_reasons_name_no_hidden_link(self):
        from operating.ontology_tools_aggregate import AggregateTools

        def near(guard, link_type, frontier, actor):
            tools = self.tools(guard)
            agg = AggregateTools(tools.base, tools.traverse)
            return set(self.gated(tools, actor, lambda scope: agg._hidden_links(
                scope, link_type, "forward", frontier, set())))

        contacts = [COMPANY, BLANK, UNTYPED, HUMAN, OTHER]
        people = [PLUS, SUB, PERSON, PLUS2]
        for actor in (OWNER, OPS):
            self.assertEqual(near(False, "Contact.same_person_as", contacts, actor),
                             set(contacts))
            self.assertEqual(near(True, "Contact.same_person_as", contacts, actor), {HUMAN})
            self.assertEqual(near(False, "Person.same_person_as", people, actor),
                             {PLUS, PERSON, PLUS2})
            self.assertEqual(near(True, "Person.same_person_as", people, actor), {PERSON})

    def test_case_workspace_link_paths_follow_no_hidden_link(self):
        from operating.case_workspace_live import _link_paths
        plain, conn = self.reader(guard=False).conn, self.reader().conn
        forward = [[{"link_type": "Contact.same_person_as", "direction": "forward"}]]
        for root in (COMPANY, BLANK, UNTYPED):
            self.assertEqual(_link_paths(plain, root, "Person"), forward, root)
            self.assertEqual(_link_paths(conn, root, "Person"), [], root)
        self.assertEqual(_link_paths(conn, HUMAN, "Person"), forward)
        self.assertEqual(_link_paths(plain, PLUS2, "Person"),
                         [[{"link_type": "Person.same_person_as", "direction": "forward"}]])
        self.assertEqual(_link_paths(conn, PLUS2, "Person"), [])
        # a twin of one plus-address link and one lead: only the lead's path is left
        self.assertEqual(_link_paths(conn, PLUS, "Person"),
                         [[{"link_type": "Person.same_person_as", "direction": "reverse"}]])


if __name__ == "__main__":
    unittest.main()
