"""``sender_corroboration`` on a store ``operating.pg.mention_collapse`` folded writes, row for
row, what it writes on the uncollapsed store: it writes one
``doccarrier:``/``docfirm:`` row per mention occurrence, so it reads every folded twin back
(``sender_corroboration.as_uncollapsed``). Without that read the folded store loses the
twins' rows (held here too, so the fixture really folds rows the step reads).

The fold is the collapse's rule (``operating.pg.mention_collapse`` ``STAGE_SQL`` and its
``UPDATE``: key, keeper, twin conditions and the reason text it appends), applied to the
build's SQLite store just before the step runs. Evidence label: ``isolated-fixture``
(synthetic mail capture over an injected Graph transport, synthetic Portal rows, the real
projector and steps; no source read, no credential used).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from integrations.tests.test_capture_mail import SCOPE
from ontology import document_fields, identifiers, mentions
from ontology import mention_occurrences as MO
from ontology import sender_corroboration as sc
from ontology.project import MailCaptures, build_store
from ontology.tests.test_po_identity_firm import Source
from ontology.tests.test_sender_corroboration import NOW, SCHEMA, world

# operating.pg.mention_collapse's constants (read side: ontology does not import operating)
TIERS = ("verified", "reviewed", "corroborated", "stored_by_text_join", "lead", "contradicted")
TAIL = ',{"feature":"identifier_occurrences","occurrences":['
SAFE_ID = re.compile(r'^[ !#-\[\]-~]+$')
COLS = ("link_id", "link_type", "from_object", "to_object", "tier", "binding_kind",
        "link_contract", "reason", "why_not", "evidence_handles", "reviewed_by", "reviewed_at",
        "review_action_id", "markings", "recorded_at", "superseded_at")
# each case names its order more than once in one field (one mention row per occurrence)
CASES = [
    ("sales@amberpoly.example", "OC", "We will deliver your order Oct 20th.",
     ["Your PO: PO#7001", "Our ref for your PO: PO#7001"]),            # firm, in the document
    ("sales@amberpoly.example", "Order confirmation PO#7004, again PO#7004", "attached",
     ["Total Amount: USD 10.00"]),                                     # carrier, firm
    ("sales@amberpoly.example", "OC", "attached",
     ["Your PO: PO#7010", "PO#7010 confirmed"]),                       # predates: a lead
]


def _json_text(value):
    return json.dumps(value, ensure_ascii=False)   # PostgreSQL's to_json(text)


def fold(conn) -> int:
    """The collapse over every mention link type of the store: twins folded into their
    keeper, deleted. Returns the twins folded."""
    rows = [dict(zip(COLS, r)) for r in conn.execute(
        f"SELECT {', '.join(COLS)} FROM links WHERE substr(link_id, 1, 8) = 'mention:' AND "
        "superseded_at IS NULL AND binding_kind = 'mention'")]
    keys: dict[tuple, list[dict]] = {}
    for r in rows:
        handles = r["evidence_handles"] or ""
        first = json.loads(handles)[0] if handles.startswith("[") else None
        keys.setdefault((r["link_type"], r["from_object"], r["to_object"], first,
                         r["link_id"][40:]), []).append(r)

    def unreviewed(r):
        return r["reviewed_by"] is None and r["reviewed_at"] is None and \
            r["review_action_id"] is None

    folded = 0
    for members in keys.values():
        k = min(members, key=lambda r: ((TIERS.index(r["tier"]) if r["tier"] in TIERS
                                         else 99), r["link_id"].encode()))
        twins = [t for t in members if t is not k
                 and (t["tier"], t["markings"], t["evidence_handles"], t["link_contract"],
                      t["recorded_at"]) == (k["tier"], k["markings"], k["evidence_handles"],
                                            k["link_contract"], k["recorded_at"])
                 and (t["evidence_handles"] or "").startswith("[")
                 and unreviewed(t) and unreviewed(k)
                 and t["reason"] is not None and t["why_not"] is not None
                 and len(k["reason"] or "") >= 2 and k["reason"].startswith("[")
                 and k["reason"].endswith("]")
                 and TAIL not in t["reason"] and TAIL not in k["reason"]
                 and SAFE_ID.match(t["link_id"]) and SAFE_ID.match(k["link_id"])]
        if not twins:
            continue
        twins.sort(key=lambda t: t["link_id"].encode())
        occurrences = ",".join(
            '{"occurrence":' + _json_text(t["link_id"][8:40]) + ',"reason":' +
            _json_text(t["reason"]) + ',"why_not":' + _json_text(t["why_not"]) + '}'
            for t in twins)
        conn.execute("UPDATE links SET reason = ? WHERE link_id = ?",
                     (k["reason"][:-1] + TAIL + occurrences + "]}]", k["link_id"]))
        for t in twins:
            conn.execute("DELETE FROM links WHERE link_id = ?", (t["link_id"],))
        folded += len(twins)
    return folded


@unittest.skipUnless(importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab"),
                     "confirmations are read from PDFs: needs pypdf and reportlab")
class CollapsedSenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for key in [k for k in os.environ if k.startswith(("PLX_WORKBENCH_API", "BC_",
                                                           "MICROSOFT_GRAPH"))]:
            os.environ.pop(key)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.state, cls.rows = world(cls.root / "world", cases=CASES)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def build(self, name, collapse):
        folded = []

        def step(builder, projector):
            if collapse:
                folded.append(fold(builder.connection))
            return sc.sender_corroboration_step(builder, projector)
        path = self.root / f"{name}.sqlite"
        with MailCaptures(self.state, internal_domains=SCOPE.internal_domains) as captures:
            build_store(path, SCHEMA, {"portal_api": Source("portal_api", self.rows),
                                       "graph_mail": captures},
                        normalizer=identifiers.normalize, clock=lambda: NOW,
                        post_project={"document_fields": document_fields.store_document_links,
                                      "mentions": mentions.store_mention_links,
                                      "sender_corroboration": step})
        import sqlite3
        conn = sqlite3.connect(path)
        try:
            got = sorted(conn.execute(
                f"SELECT {', '.join(COLS)} FROM links WHERE substr(link_id, 1, 8) = "
                "'docfirm:' OR substr(link_id, 1, 11) = 'doccarrier:'").fetchall())
        finally:
            conn.close()
        return got, sum(folded)

    def test_the_collapsed_store_gives_the_uncollapsed_rows_row_for_row(self):
        plain, _ = self.build("plain", False)
        collapsed, folded = self.build("collapsed", True)
        self.assertGreater(folded, 0)   # the fixture holds twins
        prefixes = {r[0].split(":", 1)[0] for r in plain}
        self.assertEqual(prefixes, {"docfirm", "doccarrier"}, plain)
        self.assertEqual(collapsed, plain)   # every column of every row
        # the read without the twins (the step before this fix) loses rows: the fixture
        # folds rows the step writes from
        with mock.patch.object(sc, "as_uncollapsed", lambda rows: [dict(r) for r in rows]):
            naive, _ = self.build("naive", True)
        self.assertLess(len(naive), len(plain))

    def test_a_row_holding_no_twins_reads_as_stored(self):
        rows = [{"link_id": "mention:" + "a" * 32, "reason": '[{"feature":"x"}]'},
                {"link_id": "docfield:b", "reason": "[]"}]
        self.assertEqual(sc.as_uncollapsed(rows), rows)
        keeper = {"link_id": "mention:" + "b" * 32 + "#held", "why_not": "[1]",
                  "reason": '[{"feature":"x"}]' + "", "to_object": "T"}
        keeper["reason"] = keeper["reason"][:-1] + TAIL + '{"occurrence":"' + "a" * 32 + \
            '","reason":"[2]","why_not":"[3]"}]}]'
        self.assertEqual(MO.folded(keeper["reason"])[0]["reason"], "[2]")
        got = sc.as_uncollapsed([keeper])
        self.assertEqual([(r["link_id"], r["reason"], r["why_not"], r["to_object"])
                          for r in got],
                         [("mention:" + "a" * 32 + "#held", "[2]", "[3]", "T"),
                          ("mention:" + "b" * 32 + "#held", '[{"feature":"x"}]', "[1]", "T")])
        # a twin id another row already holds: refused, never two rows of one id
        clash = dict(keeper, link_id="mention:" + "a" * 32 + "#held", reason="[9]")
        with self.assertRaisesRegex(ValueError, "collapsed_twin_id_twice"):
            sc.as_uncollapsed([keeper, clash])


if __name__ == "__main__":
    unittest.main()
