"""A live check passes only on a measured result, never on a run that judged nothing.

Evidence label: ``injected-transport``. The use-case proposal runs over the RRO tests'
synthetic stand-in original through the fake shared-link drive; REC-10 runs over the
reconciliation tests' fake Workbench mirror and injected BC sender. Both outputs go
through the live verifier.check_expectations`` with the committed specs.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from devloop import live_verify as lv
from integrations.tests import test_shared_link as fake
from integrations.tests.test_bc_read_entities import doc
from integrations.tests.test_live_source_adapters import COMPANY
from operating import reconciliation_rules as rr
from operating.tests import test_reconciliation_rules as rec
from scripts import eval_use_case_items as tool
from tests.test_rro_eval import NOW, cards, stand_in

ROOT = Path(__file__).resolve().parents[1]


def spec(name: str) -> dict:
    loaded = json.loads((ROOT / lv.SPECS_DIR_NAME / f"{name}.json").read_text("utf-8"))
    lv.validate_spec(loaded, stem=name)
    return loaded


class ProposalAnswerabilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.doc = cards()
        self.body = stand_in(self.doc)
        self.doc["source"]["original"].update(sha256=sha256(self.body).hexdigest(),
                                              web_url=fake.PERSONAL_LINK)
        patch = mock.patch.object(fake, "BYTES", self.body)
        patch.start()
        self.addCleanup(patch.stop)
        self.drive = fake.Drive(drive=fake.PERSONAL, link=fake.PERSONAL_LINK,
                                download_host=fake.OD_HOST)
        self.spec = spec("eval.use_case_proposal")
        # A synthetic stand-in never satisfies the pinned Draft 8 identity.
        self.identity = ["original.sha256 != expected", "original.bytes != expected"]

    def run_cards(self, document: dict) -> tuple[int, dict]:
        path = self.tmp / "cards.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["proposal", "run", "--cards", str(path), "--state-dir",
                              str(self.tmp / "state"), "--evidence-label",
                              "injected-transport", "--capture"],
                             environ=fake.ENV, graph_sender=self.drive, clock=lambda: NOW)
        self.assertEqual(code, 0, err.getvalue())
        return code, json.loads(out.getvalue())

    def test_measured_partial_passes_and_an_all_undetermined_case_fails(self):
        code, report = self.run_cards(self.doc)
        self.assertEqual(report["actual_case_answerability"]["answerability"], "partial")
        self.assertEqual(lv.check_expectations(self.spec, code, report), self.identity)
        # Nothing judged: every result unverified, no card on the conflict's lines, every
        # anchor still in range. That is undetermined, not blocked, and never a pass.
        nothing = copy.deepcopy(self.doc)
        nothing["provenance_conflicts"] = []
        for card in nothing["real_example"]["cards"]:
            if card["layer"] == "arithmetic":
                card["result"] = "1" if not isinstance(card["result"], list) else \
                    ["1"] * len(card["result"])
        code, report = self.run_cards(nothing)
        case = report["actual_case_answerability"]
        self.assertEqual(report["anchors"]["out_of_range"], 0)
        self.assertEqual((case["status"], case["answerability"], case["items"]),
                         ("scored", "undetermined", {"answerable": 0, "partial": 0,
                                                     "blocked": 0, "undetermined": 26}))
        failures = lv.check_expectations(self.spec, code, report)
        self.assertIn("actual_case_answerability.answerability != expected", failures)
        # One anchor out of range among undetermined items is blocked, still a failure.
        nothing["real_example"]["cards"][0]["source_lines"] = [[10 ** 6, 10 ** 6]]
        code, report = self.run_cards(nothing)
        self.assertEqual(report["actual_case_answerability"]["answerability"], "blocked")
        self.assertIn("actual_case_answerability.answerability != expected",
                      lv.check_expectations(self.spec, code, report))


class Rec10ClassifiedTests(unittest.TestCase):
    setUp = rec.RecentOrdersTests.setUp
    mirror = rec.RecentOrdersTests.mirror

    def run_split(self, sales_orders: list) -> dict:
        reader, _ = rec.bc_reader(sales_orders)
        args = argparse.Namespace(limit=3, entity_set="salesOrders")
        return json.loads(json.dumps(rr._run_split(self.mirror(), reader, rec.OPS, rec.OPS,
                                                   [COMPANY], args)))

    def test_blank_findings_are_not_classified(self):
        rec10 = spec("case.rec10_external_doc")
        blank = [doc(n, number=f"SO{n}") for n in (1, 2, 3)]  # no External Document No.
        out = self.run_split(blank)
        self.assertEqual(out["summary"]["by_class"]["blank"], 3)
        self.assertEqual((out["summary"]["findings"], out["summary"]["portal_compared"],
                          out["summary"]["bc_only"]), (3, 0, 3))
        self.assertEqual(lv.check_expectations(rec10, 0, out), ["summary.portal_compared < 1"])
        out = self.run_split(blank[1:] + [doc(1, number="SO1", externalDocumentNumber="PO-5501")])
        self.assertEqual((out["summary"]["by_class"]["blank"], out["summary"]["portal_compared"]),
                         (2, 1))
        self.assertEqual(lv.check_expectations(rec10, 0, out), [])


if __name__ == "__main__":
    unittest.main()
