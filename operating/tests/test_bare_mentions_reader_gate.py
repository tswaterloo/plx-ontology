"""PLX_PG_BARE_MENTIONS reader gate: a bare mention lead reaches a reader only through
the ``links`` view (served links + ``ontology.bare_mentions``) under the reader rules, so
a narrow reader sees none whose target, source or evidence handle it may not open."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_traverse as tt
from tests import test_ontology_tools_evidence as ev

P1, P5, P7, PB = ev.P1, "PurchaseOrder:portal.po:po-5", ev.P7, ev.PB
M1, M2 = ev.M1, ev.M2
DENIED = "graph-mail://mbx-a/denied-evidence@ck1"
TYPE = "Email.mentions_purchase_order"
FULL, OPS = ev.FULL, ev.OPS
# (link_id, source, target, evidence handle): P5 is visible to both readers
ROWS = {
    "bare:ok": (M1, P5, ev.M1_H),
    "bare:src": (M2, P5, ev.M2_H),          # source mail: mailbox OPS is not granted
    "bare:tgt": (M1, P7, ev.M1_H),          # target finance-marked: FULL only
    "bare:ev": (M1, PB, DENIED),            # one evidence handle OPS may not open
}


def _gate(actor, handle):
    return handle != DENIED or "full" in actor.principals


class BareReaderGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from operating.tests import pg_store_mode
        cls.dir = Path(tempfile.mkdtemp(prefix="bare-gate-"))
        cls.store = ev.build()
        cls.dsn = pg_store_mode._load("", cls.store.conn)
        import psycopg
        with psycopg.connect(cls.dsn, autocommit=True) as c:
            # migration 0035 as the loaded store lacks it, less the ``_seen`` views
            # (they read 0029's ontology_seen, which the loaded store does not have)
            sql = (Path(__file__).resolve().parents[1] / "pg/migrations/"
                   "0035_bare_mentions.sql").read_text("utf-8")
            c.execute(sql.split("CREATE SCHEMA IF NOT EXISTS ontology_bare_seen;")[0])
            for lid, (src, dst, handle) in ROWS.items():
                c.execute(
                    "INSERT INTO ontology.bare_mentions (link_id, link_type, from_object, "
                    "to_object, tier, binding_kind, why_not, evidence_handles, markings, "
                    "recorded_at) VALUES (%s, %s, %s, %s, 'lead', 'mention', '[]', %s, '[]', "
                    "%s)", (lid, TYPE, src, dst, json.dumps([handle]), ev.T0))

    def tools(self, search_path=None, gate=_gate):
        from operating.pg.ontology_reader import PgStoreReader
        from operating.pg.sqlite_compat import PgCompatConnection
        conn = PgCompatConnection(self.dsn, link_guard=True,
                                  **({"search_path": search_path} if search_path else {}))
        self.addCleanup(conn.close)
        reader = PgStoreReader(conn, ot.StoreReader(self.store.conn))
        base = ot.OntologyTools(reader, ev.POLICY, item_gate=gate, schema=ev.SCHEMA,
                                grammar=None, contracts=ev.CONTRACTS, clock=lambda: ev.NOW)
        return tt.TraverseTools(base)

    def seen(self, tools, actor, root):
        found = tools.call("plx.around", {"from": root, "path": [
            {"link_type": TYPE, "direction": "reverse" if root == P5 else "forward"}],
            "limit": 50, "tier_floor": "lead"}, actor)
        self.assertNotIn("error", found, json.dumps(found)[:1500])
        text = json.dumps(found)
        return {oid for oid in (M1, M2, P5, P7, PB) if oid != root and f'"{oid}"' in text}

    def test_view_off_shows_no_bare_row_to_anyone(self):
        tools = self.tools()
        self.assertEqual(self.seen(tools, FULL, P5), set())

    def test_narrow_reader_sees_only_rows_it_may_open(self):
        tools = self.tools("ontology_bare, ontology, public")
        # P5 page: the row whose source mail OPS may not open is hidden from OPS
        self.assertEqual(self.seen(tools, OPS, P5), {M1})
        self.assertEqual(self.seen(tools, FULL, P5), {M1, M2})
        # M1's page: the finance target and the denied-evidence row are hidden from OPS
        self.assertEqual(self.seen(tools, OPS, M1), {P5})
        self.assertEqual(self.seen(tools, FULL, M1), {P5, P7, PB})

    def test_raw_view_rows_are_all_there(self):
        # the gate, not the view, hides rows: the control that the rows exist at all
        conn = self.tools("ontology_bare, ontology, public").base.store.conn
        got = {r[0] for r in conn.execute("SELECT link_id FROM links WHERE link_id LIKE 'bare:%'")}
        self.assertEqual(got, set(ROWS))


if __name__ == "__main__":
    unittest.main()
