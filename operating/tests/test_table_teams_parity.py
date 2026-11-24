"""plx.table over captured Teams chats: the SQL-scoped message set is the item gate's, row for row.

A real Teams capture (the capture tests' in-process FakeTeamsGraph behind the GET-only
client) projected by the real projector and loaded into a throwaway Postgres database.
The chat's members are cos (whole history) and a guest; vince joins later with history
from between the first two messages. Messages: the two fixture ones, one naming
Freightcom, and 60 more. For a narrow reader (granted Teams, member of nothing; and one
not granted at all), a mid reader (vince: only the messages from his history on) and the
owner-equivalent reader (cos: every message): every TeamsChatMessage the table counts is
one the item gate (``item_gate_from_case_runtime``) serves, and every one the gate serves
is counted, except the Freightcom message, which is counted as withheld. A member excluded
after the capture withholds the whole chat from everyone, in the table as in the gate.

Evidence label: injected-transport (no network, made-up ids and addresses)."""
from __future__ import annotations

import json
import sqlite3
from unittest import mock

from integrations.people_exclusion import PeopleExclusion
from integrations.tests import test_capture_teams as tct
from ontology import schema as schema_loader
from operating import ontology_tools as ot
from operating import ontology_tools_table as tt
from operating.ontology_tools_results import item_gate_from_case_runtime
from operating.tests.test_case_runtime_teams import WITHHELD, TeamsHostCase
from operating.tests import test_ontology_tools_table as table_tests
from tests import test_ontology_tools_evidence as ev

READERS = (tct.OUTSIDER, WITHHELD, tct.VINCE_ONLY, tct.COS_ONLY)
FC_TEXT = "Freightcom pickup booked for Tuesday"


class TableTeamsParity(TeamsHostCase):
    def setUp(self) -> None:
        super().setUp()
        self.graph.post_chat("3003", FC_TEXT)
        for k in range(60):
            self.graph.post_chat(f"4{k:03d}", f"bulk note {k}")
        # vince joins with history from between the first two messages (01:00, 01:10)
        self.graph.chat_members.append((tct.U_VINCE, tct.VINCE))
        self.graph.history[tct.U_VINCE] = "2026-10-01T01:05:00Z"
        self.assertTrue(self.capture.run(tct.scope())["complete"])
        from operating.tests import pg_store_mode
        path, _ = self.project()
        self.sqlite = sqlite3.connect(path)
        self.addCleanup(self.sqlite.close)
        self.dsn = pg_store_mode._load(str(path), self.sqlite)
        self.messages = {row[0]: row[1] for row in self.sqlite.execute(
            "SELECT o.object_id, v.l1_handle FROM objects o JOIN object_versions v ON "
            "v.object_id = o.object_id AND v.superseded_at IS NULL "
            "WHERE o.object_type = 'TeamsChatMessage'")}
        self.assertEqual(len(self.messages), 63)

    def tool(self):
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection(self.dsn, link_guard=True)
        self.addCleanup(conn.close)
        reader = PgStoreReader(conn, ot.StoreReader(self.sqlite))
        everyone = set().union(*(a.principals for a in READERS)) | {"staff:teams-member"}
        policy = ot.ReadPolicy(tenant="plx", audiences={"teams:*": everyone,
                                                        "api:*": everyone})
        base = ot.OntologyTools(reader, policy,
                                item_gate=item_gate_from_case_runtime(self.runtime),
                                schema=ot.SchemaView.from_compiled(schema_loader.load_schema()),
                                grammar=None, contracts=ev.CONTRACTS, clock=lambda: ev.NOW)
        return tt.TableTool(base)

    def listed(self, tool, actor):
        out = tool.call("plx.table", {"from": "TeamsChatMessage", "select": ["object_id", "id"],
                                      "limit": 200}, actor)
        self.assertNotIn("error", out, out)
        return out, {r["object_id"] for r in out["rows"]}

    def gated(self, actor):
        gate = item_gate_from_case_runtime(self.runtime)
        return {oid for oid, handle in self.messages.items() if gate(actor, handle)}

    def ids(self, oids):
        return {json.loads(oid.split(":", 2)[2])["id"] for oid in oids}

    def fc(self):
        """None: the Freightcom message (3003) is read like any other (owner 11-23)."""
        return set()

    def test_sql_scoped_set_is_the_item_gates_row_for_row(self):
        tool = self.tool()
        bulk = {f"4{k:03d}" for k in range(60)}
        expected = {tct.OUTSIDER: set(), WITHHELD: set(), tct.VINCE_ONLY: {"3002"} | bulk,
                    tct.COS_ONLY: {"3001", "3002"} | bulk}
        for actor in READERS:
            with self.subTest(actor=sorted(actor.principals)):
                gate = self.gated(actor)
                out, table = self.listed(tool, actor)
                self.assertEqual(table, gate - self.fc())
                self.assertEqual(self.ids(table), expected[actor] | (self.ids(gate) & {"3003"}))
                counted = tool.call("plx.table", {"from": "TeamsChatMessage",
                                                  "measures": [{"fn": "count"}]}, actor)
                value = counted["groups"][0]["measures"][0]["value"] if counted["groups"] \
                    else "0"
                self.assertEqual(value, str(len(table)), counted)
                self.assertEqual((out.get("withheld") or {}).get("objects", 0), 0, out)
                self.assertFalse(out["partial"])

    def test_cardinality_1_10_60_by_day_bucket(self):
        # the 63 messages are stamped 10 minutes apart from 2026-10-01T00:10Z (UTC):
        # Eastern days 2026-09-30 (until 04:00Z) and 2026-10-01
        out = self.tool().call("plx.table", {
            "from": "TeamsChatMessage", "group_by": [{"field": "created_at", "bucket": "day"}],
            "measures": [{"fn": "count"}]}, tct.COS_ONLY)
        got = dict(zip(table_tests.TableTool.keys(out, "created_at@day"),
                       (g["measures"][0]["value"] for g in out["groups"])))
        gate = self.gated(tct.COS_ONLY) - self.fc()
        self.assertEqual(sum(int(v) for v in got.values()), len(gate))
        self.assertEqual(set(got), {"2026-09-30", "2026-10-01"}, got)

    def test_a_member_excluded_after_the_capture_withholds_the_chat_from_everyone(self):
        self.runtime.people_exclusion = PeopleExclusion.from_env(
            {**tct.ENV, "PLX_EXCLUDED_PEOPLE_OIDS":
             tct.ENV["PLX_EXCLUDED_PEOPLE_OIDS"] + "," + tct.U_OTHER})
        tool = self.tool()
        for actor in READERS:
            with self.subTest(actor=sorted(actor.principals)):
                self.assertEqual(self.gated(actor), set())
                out, table = self.listed(tool, actor)
                self.assertEqual(table, set())
                self.assertEqual((out.get("withheld") or {}).get("objects", 0), 0, out)
                self.assertNotIn(tct.SECRET_TEXT, json.dumps(out))

    def test_past_the_cap_or_the_time_undecided_messages_are_withheld_never_counted(self):
        from operating import ontology_tools_table_items as items
        gate = self.gated(tct.COS_ONLY) - self.fc()
        with mock.patch.object(items, "TEAMS_MAX", 10):
            out, table = self.listed(self.tool(), tct.COS_ONLY)
        self.assertLess(table, gate)
        self.assertLessEqual(table, gate)
        self.assertTrue(out["partial"])
        # every message the gate serves and the capped call did not decide is withheld
        self.assertEqual((out.get("withheld") or {}).get("objects", 0), len(gate) - len(table), out)
        self.assertIn("item_rules_capped", {g["reason"] for g in out["gaps"]})
        import time
        from operating import call_limits
        with mock.patch.object(tt, "SECOND_PASS_MARGIN", 10_000.0), \
                call_limits.sql_deadline(time.monotonic() + 60):
            out, table = self.listed(self.tool(), tct.COS_ONLY)
        self.assertEqual(table, set())
        self.assertTrue(out["partial"])
        self.assertIn("item_rules_capped", {g["reason"] for g in out["gaps"]})
        # a narrow reader past the cap: nothing it may not read is shown or counted
        with mock.patch.object(items, "TEAMS_MAX", 10):
            out, table = self.listed(self.tool(), tct.OUTSIDER)
        self.assertEqual(table, set())
        self.assertNotIn(tct.SECRET_TEXT, json.dumps(out))

    def test_an_unusable_people_guard_serves_no_teams_message(self):
        self.runtime.people_exclusion = PeopleExclusion.from_env({})
        out = self.tool().call("plx.table", {"from": "TeamsChatMessage",
                                             "measures": [{"fn": "count"}]}, tct.COS_ONLY)
        self.assertEqual(out["error"]["code"], "unavailable", out)

    def test_a_host_without_the_runtime_gate_is_refused(self):
        tool = self.tool()
        tool.base.item_gate = lambda actor, handle: True
        out = tool.call("plx.table", {"from": "TeamsChatMessage",
                                      "measures": [{"fn": "count"}]}, tct.COS_ONLY)
        self.assertEqual(out["error"]["code"], "not_served", out)

    def test_channels_stay_refused(self):
        out = self.tool().call("plx.table", {"from": "TeamsChannelMessage",
                                             "measures": [{"fn": "count"}]}, tct.COS_ONLY)
        self.assertEqual(out["error"]["code"], "not_served", out)


if __name__ == "__main__":
    import unittest
    unittest.main()
