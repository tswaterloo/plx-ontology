"""plx.table over captured mail: the SQL-scoped message set is the mail item gate's, row for row.

A real mail capture (three mailboxes, the capture tests' in-process FakeGraph behind the
GET-only client) projected by the real projector and loaded into a throwaway Postgres
database. The finance mailbox's owner is excluded. Messages: cleared ones, one copied to a
tenant address no mailbox holds (undecided), one copied to the excluded person, one to an
address the capture's purge left undecided (a list whose members it could not read), two
sent copies with a Bcc (to the excluded person; to a cleared colleague), one with no
sender address, one copied to an address two mailboxes share, one to a non-ASCII external
address, an internal message, and one naming Freightcom. For a narrow reader (one
mailbox), a mid reader (two) and the owner reader (all three): every Email the table
counts is one the item gate (``item_gate_from_case_runtime``) serves, and every one the
gate serves is counted, except the Freightcom message, which is counted as withheld.

Evidence label: injected-transport (no network, made-up ids and addresses)."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.people_exclusion import PeopleExclusion
from integrations.live_source_guard import LiveSourceAudience
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_capture_mail import (BOX_A, BOX_B, CAPTURE, ENV, UPN_A, UPN_B,
                                                   Clock, FakeGraph)
from ontology import schema as schema_loader
from ontology.project import MailCaptures, build_store
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_table as tt
from integrations.capture_mail_scope import CaptureScope
from operating.case_mail_scope import MailScope
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.case_tools_host import CaseCaptures
from operating.ontology_tools_results import item_gate_from_case_runtime
from tests import test_ontology_tools_evidence as ev

BOX_C = "12345678-aaaa-bbbb-cccc-00000000000c"
UPN_C = "sales@example-plx.com"
SHARED = "shared@example-plx.com"
SUPPLIER = "supplier@outside.example"
EXCLUDED_B = {**ENV, "PLX_EXCLUDED_PEOPLE_OIDS": BOX_B}
NARROW = Actor("plx", frozenset({"staff:orders"}), "restricted")
MID = Actor("plx", frozenset({"staff:orders", "staff:sales"}), "restricted")
OWNER = Actor("plx", frozenset({"staff:orders", "staff:sales", "staff:finance"}),
              "restricted")
LIST = "dist-list@partner.example"   # left undecided by the purge (members unreadable)
FOLDER_C = "AAMkFOLDER-C="


def audience(principals) -> LiveSourceAudience:
    return LiveSourceAudience({f"mail:{UPN_A}": principals["a"],
                               f"mail:{UPN_B}": principals["b"],
                               f"mail:{UPN_C}": principals["c"]}, tenant="plx",
                              classifications={"mail:*": "restricted"})


READERS = audience({"a": ["staff:orders"], "b": ["staff:finance"], "c": ["staff:sales"]})
CAPTURING = audience({k: ["svc:mail-capture"] for k in "abc"})


def graph() -> FakeGraph:
    g = FakeGraph()
    g.users[BOX_A]["proxyAddresses"].append(f"smtp:{SHARED}")
    g.users[BOX_C] = {"id": BOX_C, "userPrincipalName": UPN_C, "mail": UPN_C,
                      "proxyAddresses": [f"SMTP:{UPN_C}", f"smtp:{SHARED}"]}
    g.folders[BOX_C] = {"inbox": FOLDER_C, FOLDER_C.lower(): FOLDER_C}
    g.messages[BOX_C] = {}

    def put(box, mid, subject, *, sender=SUPPLIER, to=None, cc=(), bcc=()):
        g.put(box, mid, "CK-" + mid, subject=subject)
        m = g.messages[box][mid]
        m["from"] = ({"emailAddress": {"address": sender}} if sender
                     else {"emailAddress": {"name": "Postmaster"}})
        if to is not None:
            m["toRecipients"] = [{"emailAddress": {"address": a}} for a in to]
        m["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
        m["bccRecipients"] = [{"emailAddress": {"address": a}} for a in bcc]
    put(BOX_A, "A-OK", "Order note one")
    put(BOX_A, "A-OK2", "Order note two", cc=["buyer@other.example"])
    put(BOX_A, "A-UND", "Undecided copy", cc=["pat.unlisted@example-plx.com"])
    put(BOX_A, "A-EXC", "Excluded copy", cc=[UPN_B])
    put(BOX_A, "A-LIST", "List copy", cc=[LIST])
    put(BOX_A, "A-BCCX", "Sent with bcc to excluded", sender=UPN_A, to=[SUPPLIER],
        bcc=[UPN_B])
    put(BOX_A, "A-BCCOK", "Sent with bcc to a colleague", sender=UPN_A, to=[SUPPLIER],
        bcc=[UPN_C])
    put(BOX_A, "A-NOFROM", "No sender address", sender=None)
    put(BOX_A, "A-SHARED", "Shared address copy", cc=[SHARED])
    put(BOX_A, "A-UNI", "Accented copy", cc=["josé@outside.example"])
    put(BOX_A, "A-FC", "Freightcom pickup quote")
    put(BOX_C, "C-OK", "Sales note")
    put(BOX_C, "C-INT", "Internal note", sender=UPN_C, to=[UPN_A])
    put(BOX_B, "B-OK", "Finance note")
    # senders of 1, 10 and 60 messages to the sales mailbox (no row lost or doubled)
    for n in (1, 10, 60):
        for k in range(n):
            put(BOX_C, f"C-BULK{n}-{k}", f"Bulk {n} note {k}", sender=f"bulk{n}@vendor.example")
    return g


class TableMailParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_store_mode
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.state = root / "mail"
        scope_list = MailScope([UPN_A, UPN_B, UPN_C])
        scope = CaptureScope([{"address": u, "folders": ["inbox"]}
                              for u in (UPN_A, UPN_B, UPN_C)], window_days=90,
                             internal_domains=["example-plx.com"], allowlist=scope_list)
        store = MailCaptureStore(cls.state, clock=Clock())
        api, _ = graph_read.build_graph(ENV, sender=graph(), retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = MailCapture(store, GraphMailReader(api, CAPTURING), CAPTURE,
                          sleep=lambda s: None).run(scope)
        assert run["complete"], run
        store.close()
        projection = root / "ontology.sqlite"
        with MailCaptures(cls.state, internal_domains=scope.internal_domains,
                          exclusion=PeopleExclusion.from_env(ENV)) as mail:
            cls.compiled = schema_loader.load_schema()
            build_store(projection, cls.compiled, {"graph_mail": mail}, clock=lambda: ev.NOW)
        # the purge could not read the list's members after the projection ran
        from integrations.capture_mail import DB_FILE, STORE_DIR
        with sqlite3.connect(cls.state / STORE_DIR / DB_FILE) as db:
            db.execute("CREATE TABLE IF NOT EXISTS undecided_addresses (address TEXT "
                       "PRIMARY KEY, since TEXT NOT NULL, checked_at TEXT NOT NULL)")
            db.execute("INSERT INTO undecided_addresses VALUES (?, ?, ?)",
                       (LIST, "2026-09-30T00:00:00Z", "2026-09-30T00:00:00Z"))
        cls.sqlite = sqlite3.connect(projection)
        cls.dsn = pg_store_mode._load(str(projection), cls.sqlite)
        cls.emails = {row[0]: row[1] for row in cls.sqlite.execute(
            "SELECT o.object_id, v.l1_handle FROM objects o JOIN object_versions v ON "
            "v.object_id = o.object_id AND v.superseded_at IS NULL "
            "WHERE o.object_type = 'Email'")}
        cls.scope_list = scope_list
        audience_file = root / "audience.json"
        audience_file.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                             "audiences": {}}), encoding="utf-8")
        cls.root = root
        cls.audience_file = audience_file

    @classmethod
    def tearDownClass(cls):
        cls.sqlite.close()
        cls.tmp.cleanup()

    def runtime(self) -> CaseRuntime:
        runtime = CaseRuntime(self.root / "state", client=None, audience=self.audience_file,
                              tenant="plx", live=LiveReaders(audience=READERS),
                              mail_scope=self.scope_list,
                              people_exclusion=PeopleExclusion.from_env(EXCLUDED_B))
        self.addCleanup(runtime.close)
        runtime.captures = CaseCaptures(runtime, mail_capture_dir=self.state)
        return runtime

    def tool(self, runtime):
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection(self.dsn, link_guard=True)
        self.addCleanup(conn.close)
        reader = PgStoreReader(conn, ot.StoreReader(self.sqlite))
        everyone = {"staff:orders", "staff:sales", "staff:finance"}
        policy = ot.ReadPolicy(tenant="plx", audiences={"mail:*": everyone, "api:*": everyone})
        base = ot.OntologyTools(reader, policy, item_gate=item_gate_from_case_runtime(runtime),
                                schema=ot.SchemaView.from_compiled(self.compiled),
                                grammar=None, contracts=ev.CONTRACTS, clock=lambda: ev.NOW)
        return tt.TableTool(base)

    def listed(self, tool, actor, where=()):
        out = tool.call("plx.table", {"from": "Email", "select": ["object_id", "subject"],
                                      "where": list(where), "limit": 200}, actor)
        self.assertTrue(out["completeness"]["complete"], out["completeness"])
        self.assertNotIn("error", out, out)
        return out, {r["object_id"] for r in out["rows"]}

    def gated(self, runtime, actor):
        gate = item_gate_from_case_runtime(runtime)
        return {oid for oid, handle in self.emails.items() if gate(actor, handle)}

    def subjects(self, oids):
        got = set()
        for oid in oids:
            row = self.sqlite.execute(
                "SELECT value FROM object_values WHERE object_id = ? AND property = 'subject'",
                (oid,)).fetchone()
            got.add(row[0] if row else None)
        return got

    def test_sql_scoped_set_is_the_item_gates_row_for_row(self):
        runtime = self.runtime()
        tool = self.tool(runtime)
        expected_visible = {
            NARROW: {"Order note one", "Order note two", "Sent with bcc to a colleague",
                     "Accented copy"},
            MID: {"Order note one", "Order note two", "Sent with bcc to a colleague",
                  "Accented copy", "Sales note", "Internal note",
                  *(f"Bulk {n} note {k}" for n in (1, 10, 60) for k in range(n))},
        }
        expected_visible[OWNER] = expected_visible[MID]
        for actor in (NARROW, MID, OWNER):
            with self.subTest(actor=sorted(actor.principals)):
                gate = self.gated(runtime, actor)
                out, table = self.listed(tool, actor)
                self.assertEqual(table, gate, (self.subjects(table), self.subjects(gate)))
                # the Freightcom message is read like any other (owner 11-23): the table
                # holds whatever the gate serves, nothing withheld
                self.assertEqual(self.subjects(table), expected_visible[actor] | (
                    self.subjects(gate) & {"Freightcom pickup quote"}))
                self.assertEqual(out["withheld"]["objects"], 0, out)
                self.assertFalse(out["partial"])
                counted = tool.call("plx.table", {"from": "Email",
                                                  "measures": [{"fn": "count"}]}, actor)
                self.assertEqual(counted["groups"][0]["measures"][0]["value"],
                                 str(len(table)) if isinstance(
                                     counted["groups"][0]["measures"][0]["value"], str)
                                 else len(table))

    def test_senders_of_1_10_60_messages_are_counted_exactly(self):
        tool = self.tool(self.runtime())
        out = tool.call("plx.table", {"from": "Email", "group_by": ["sender"],
                                      "where": [{"field": "sender", "op": "starts_with",
                                                 "value": "bulk"}]}, MID)
        got = {g["key"]["sender"]["untrusted_text"]: g["measures"][0]["value"]
               for g in out["groups"]}
        self.assertEqual(got, {"bulk1@vendor.example": "1", "bulk10@vendor.example": "10",
                               "bulk60@vendor.example": "60"}, out)
        self.assertNotIn("bulk", json.dumps(tool.call(
            "plx.table", {"from": "Email", "group_by": ["sender"]}, NARROW)))

    def test_past_the_caps_undecided_messages_are_withheld_never_counted(self):
        from operating import ontology_tools_table_items as items
        runtime = self.runtime()
        gate = self.gated(runtime, OWNER)
        with mock.patch.object(items, "PENDING_MAX", 0), mock.patch.object(items, "BCC_MAX", 0):
            out, table = self.listed(self.tool(runtime), OWNER)
        self.assertLess(table, gate)
        # the sent copies (not looked up for a Bcc) and the accented copy (not decided)
        self.assertEqual({"Sent with bcc to a colleague", "Internal note", "Accented copy"},
                         self.subjects(gate - table))
        self.assertTrue(out["partial"])
        self.assertGreaterEqual(out["withheld"]["objects"], 3, out)
        self.assertEqual({g["reason"] for g in out["gaps"]} >= {"item_rules_capped",
                                                               "mail_withheld"}, True, out)
        text = json.dumps(out)
        for secret in ("Undecided copy", "Sent with bcc to excluded", "pat.unlisted"):
            self.assertNotIn(secret, text)

    def test_bcc_lookups_stop_at_the_calls_time_and_withhold_the_rest(self):
        import time
        from operating import call_limits
        from operating import ontology_tools_table as tt
        runtime = self.runtime()
        gate = self.gated(runtime, OWNER)
        # a bounded call whose time leaves the second pass none for lookups
        with mock.patch.object(tt, "SECOND_PASS_MARGIN", 10_000.0), \
                call_limits.sql_deadline(time.monotonic() + 60):
            out, table = self.listed(self.tool(runtime), OWNER)
        self.assertLess(table, gate)
        self.assertTrue({"Sent with bcc to a colleague", "Internal note"}
                        <= self.subjects(gate - table), out)
        self.assertTrue(out["partial"])
        self.assertIn("item_rules_capped", {g["reason"] for g in out["gaps"]})
        self.assertNotIn("Sent with bcc to excluded", json.dumps(out))
        # unbounded, every one is looked up: the table is the gate's set
        out, table = self.listed(self.tool(runtime), OWNER)
        self.assertEqual(table, gate)

    def test_hidden_messages_leave_no_trace_in_any_shape(self):
        runtime = self.runtime()
        tool = self.tool(runtime)
        hidden = ("Undecided copy", "Excluded copy", "List copy", "Sent with bcc to excluded",
                  "No sender address", "Shared address copy", "Finance note",
                  "pat.unlisted", LIST, UPN_B)
        for args in ({"from": "Email", "group_by": ["subject"], "limit": 200},
                     {"from": "Email", "group_by": ["sender"], "limit": 200},
                     {"from": "Email", "group_by": ["to_addresses"], "limit": 200},
                     {"from": "Email", "group_by": ["mailbox"], "limit": 200}):
            out = tool.call("plx.table", args, OWNER)
            self.assertNotIn("error", out, out)
            text = json.dumps(out)
            for secret in hidden:
                self.assertNotIn(secret, text, (args, secret))

    def test_an_unusable_people_guard_serves_no_mail(self):
        runtime = self.runtime()
        runtime.people_exclusion = PeopleExclusion.from_env({})
        out = self.tool(runtime).call("plx.table", {"from": "Email",
                                                    "measures": [{"fn": "count"}]}, OWNER)
        self.assertEqual(out["error"]["code"], "unavailable", out)

    def test_a_host_without_the_runtime_gate_is_refused(self):
        tool = self.tool(self.runtime())
        tool.base.item_gate = lambda actor, handle: True
        out = tool.call("plx.table", {"from": "Email", "measures": [{"fn": "count"}]}, OWNER)
        self.assertEqual(out["error"]["code"], "not_served", out)

    def test_attachments_stay_refused(self):
        out = self.tool(self.runtime()).call(
            "plx.table", {"from": "Attachment", "measures": [{"fn": "count"}]}, OWNER)
        self.assertEqual(out["error"]["code"], "not_served", out)


if __name__ == "__main__":
    unittest.main()
