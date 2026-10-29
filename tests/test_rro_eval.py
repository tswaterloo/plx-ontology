"""Related Release Orders Draft 8: the Dot's 26 PROPOSED items over a pinned original.

The committed card file (``evals/dev/rro/proposed_items.json``) becomes 26
``plx-eval-item/v1`` items (14 comprehension F21, 12 arithmetic F22) through the
use-case eval path; the original enters state only through the guarded shared-link
reader with a matching SHA-256; the per-item report runs on that recorded capture
(injected transport, a synthetic stand-in original: never the real Draft 8 body).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import copy
import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from devloop import live_verify
from evals import case_cards, runner, score
from evals.measure import CONFLICT_REASON
from evals.admission import admissible
from evals.tests import fixtures as fx
from integrations.live_source_guard import LiveSourceAudience, NotVisible
from integrations.tests import test_shared_link as fake
from ontology.core import Actor
from operating import use_case_eval_items as pinned
from scripts import eval_use_case_items as tool

ROOT = Path(__file__).resolve().parents[1]
CARDS = ROOT / "evals" / "dev" / "rro" / "proposed_items.json"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
MARKER = "STAND-IN-BODY-TEXT"


def cards() -> dict:
    return json.loads(CARDS.read_text(encoding="utf-8"))


def stand_in(doc: dict, stop: str = ".") -> bytes:
    """A synthetic original with the cards' anchors in range and each calculation's
    operands printed (with thousands separators) on its first anchored line. Each line
    ends with ``stop``: page text joins lines, so without it a line's last figure takes
    the next line's first word as its meaning ("5,984 STAND"), and a figure printed twice
    has two meanings, which the claim gate refuses as ambiguous (claim_checks prose-figure
    rule, 464b1af27c and a22101fe50)."""
    lines = [f"{MARKER} {n}" for n in range(1, 1901)]
    for card in doc["real_example"]["cards"]:
        if card["layer"] == "arithmetic":
            first = card["source_lines"][0][0]
            lines[first - 1] += " " + " ".join(
                f"{Decimal(o):,}" for o in tool._OPERAND.findall(card["calculation"]))
    return ("\n".join(line + stop for line in lines) + "\n").encode()


class CommittedProposalTests(unittest.TestCase):
    def test_26_items_load_and_validate_in_two_layers_and_stay_proposals(self):
        doc = cards()
        items = tool.proposal_items(doc, now=NOW.isoformat())
        self.assertEqual(len(items), 26)
        for item in items:
            self.assertEqual(runner.validate_item(item), [], item["id"])
            self.assertTrue(item["id"].endswith("-exposed"))
            self.assertTrue(item["label"]["note"].startswith("PROPOSED"))
            anchors = [d for d in item["oracle"]["derived_from"] if "#L" in d]
            self.assertTrue(anchors, item["id"])
        families = [item["family"] for item in items]
        self.assertEqual((families.count("F21"), families.count("F22")), (14, 12))
        sha = doc["source"]["original"]["sha256"]
        self.assertTrue(sha.startswith("2d8b51b6") and sha.endswith("0fbb"))
        by_card = dict(zip((c["id"] for c in doc["real_example"]["cards"]), items))
        # Draft 8 assertions are never gold: comprehension items expect no value.
        for card_id, item in by_card.items():
            if item["family"] == "F21":
                self.assertEqual(item["known_outcome"]["values"], [], card_id)
            else:
                cites = {c["l1_handle"] for v in item["known_outcome"]["values"]
                         for c in v["citations"]}
                self.assertEqual(cites, {f"original://sha256/{sha}"})
        # Arithmetic results cite the byte pin, a pinned source: admitted, and refused once
        # a result cites nothing. A comprehension card expects no value, so it is admitted
        # only with an expected gap (a provenance conflict on its lines).
        for card_id, item in by_card.items():
            expected = [] if item["family"] == "F22" or item["known_outcome"]["gaps"] \
                else ["empty_outcome"]
            self.assertEqual(admissible(item), expected, card_id)
            if item["family"] == "F22":
                bare = copy.deepcopy(item)
                bare["known_outcome"]["values"][0].pop("citations")
                self.assertEqual(score.uncited_expected(bare), ["result"
                                 if len(bare["known_outcome"]["values"]) == 1 else "result_1"])
                self.assertEqual(admissible(bare), ["uncited_values"], card_id)
        # The line-1257 vs 1716 provenance conflict stays an explicit expected gap.
        for card_id in ("RRO-01", "RRO-14"):
            self.assertEqual([g["reason"] for g in by_card[card_id]["known_outcome"]["gaps"]],
                             [CONFLICT_REASON], card_id)
        self.assertEqual(doc["provenance_conflicts"][0]["status"], "unresolved")
        self.assertEqual({p["id"]: p["status"] for p in doc["policy_status"]
                          if p["id"] in ("ERP-D-017", "ERP-D-018")},
                         {"ERP-D-017": "proposed", "ERP-D-018": "proposed"})

    def test_no_business_rows_beyond_item_fields(self):
        doc = cards()
        self.assertEqual(set(doc), {"format", "status", "source", "layers",
                                    "provenance_conflicts", "policy_status",
                                    "missing_originals", "real_example"})
        allowed = {"id", "layer", "question", "criterion", "source_lines", "interpretation",
                   "calculation", "result", "anchor"}
        for card in doc["real_example"]["cards"]:
            self.assertLessEqual(set(card), allowed, card["id"])
            # an anchor names its record and the committed source naming it (card anchors)
            self.assertIsNone(case_cards.anchor_problem(card), card["id"])
        # Kept out of the synthetic dev set the fixture baseline runs over.
        self.assertEqual(len(runner.load_items(ROOT / "evals" / "dev", split="dev")), 30)

    def test_arithmetic_items_recompute_under_the_stated_assumptions(self):
        for card in cards()["real_example"]["cards"]:
            if card["layer"] == "arithmetic":
                expected = card["result"] if isinstance(card["result"], list) \
                    else [card["result"]]
                self.assertEqual(tool.recompute(card["calculation"]),
                                 [Decimal(r) for r in expected], card["id"])


class CaptureAndRunTests(unittest.TestCase):
    stop = "."   # each stand-in line's end (stand_in)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        self.doc = cards()
        self.body = stand_in(self.doc, self.stop)
        self.doc["source"]["original"].update(sha256=sha256(self.body).hexdigest(),
                                              web_url=fake.PERSONAL_LINK)
        self.cards = Path(tmp.name) / "cards.json"
        self.cards.write_text(json.dumps(self.doc), encoding="utf-8")
        patch = mock.patch.object(fake, "BYTES", self.body)
        patch.start()
        self.addCleanup(patch.stop)
        self.drive = fake.Drive(drive=fake.PERSONAL, link=fake.PERSONAL_LINK,
                                download_host=fake.OD_HOST)

    def cli(self, *extra, cards_path=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["proposal", "run", "--cards", str(cards_path or self.cards),
                              "--state-dir", str(self.state),
                              "--evidence-label", "injected-transport", *extra],
                             environ=fake.ENV, graph_sender=self.drive, clock=lambda: NOW)
        return code, out.getvalue(), err.getvalue()

    def capture(self, url, expected):
        actor = Actor("plx", frozenset({"svc:use-case-capture"}), "restricted")
        audience = LiveSourceAudience({"sharepoint:*": ["svc:use-case-capture"]},
                                      tenant="plx")
        return pinned.capture_shared_original(self.state, url, expected_sha256=expected,
                                              actor=actor, audience=audience,
                                              environ=fake.ENV, sender=self.drive)

    def test_capture_refuses_any_route_but_the_guarded_reader_and_a_sha_mismatch(self):
        good = self.doc["source"]["original"]["sha256"]
        for url in ("https://example.com/Related.md", "http://petralabx-my.sharepoint.com/x",
                    "https://petralabx-my.sharepoint.com:8443/x", "file:///tmp/original.md"):
            with self.subTest(url=url), self.assertRaises(NotVisible):
                self.capture(url, good)
        self.assertEqual(self.drive.sent(), [])  # refused before any I/O
        with self.assertRaises(pinned.CaptureRefused) as refused:
            self.capture(fake.PERSONAL_LINK, "0" * 64)
        self.assertEqual(refused.exception.code, "sha256_mismatch")
        root = self.state / "use_cases"
        self.assertFalse(root.exists() and any((root / "originals").iterdir()))
        self.assertFalse((root / pinned.CAPTURES_FILE).exists())
        with self.assertRaises(pinned.CaptureRefused) as missing:
            pinned.captured_original(self.state, good)
        self.assertEqual(missing.exception.code, "original_capture_missing")
        # With no recorded capture the run reports nothing and writes no items.
        code, out, err = self.cli()
        self.assertEqual((code, out, json.loads(err)), (1, "", {"error":
                                                              "original_capture_missing"}))
        self.assertFalse((root / tool.ITEMS_DIR).exists())

    def test_runner_reports_per_item_on_the_captured_original(self):
        code, out, err = self.cli("--capture")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["original"]["body_read"], "proven_by_recorded_capture")
        self.assertEqual(report["original"]["sha256"], sha256(self.body).hexdigest())
        self.assertEqual(report["original"]["drive_citation"], fake.drive_citation(
            fake.PERSONAL, fake.ITEM, self.drive.etags[0], sha256(self.body).hexdigest()))
        self.assertEqual(report["items"]["loaded"], 26)
        self.assertEqual(report["items"]["admitted"], 15)
        self.assertEqual({r["id"]: r["reasons"] for r in report["items"]["refused"]},
                         {item["id"]: ["empty_outcome"] for item in tool.proposal_items(
                             self.doc, now=NOW.isoformat()) if item["family"] == "F21" and
                          not item["known_outcome"]["gaps"]})
        self.assertEqual(len(report["items"]["refused"]), 11)
        self.assertEqual(report["items"]["by_layer"], {"comprehension": 14, "arithmetic": 12})
        self.assertIn("PROPOSED items (14 comprehension, 12 arithmetic)", report["items_label"])
        self.assertEqual(report["anchors"]["out_of_range"], 0)
        self.assertEqual(report["arithmetic"], {"consistent": 12, "not_consistent": 0})
        # Answerability is measured: 12 verified results, 3 cards on the conflict's lines,
        # 11 unaudited rubrics; five declared missing originals keep the case partial.
        case = report["actual_case_answerability"]
        self.assertEqual((case["status"], case["answerability"], case["items"],
                          case["missing_original_count"], case["unresolved_conflicts"]),
                         ("scored", "partial", {"answerable": 12, "partial": 3, "blocked": 0,
                                                "undetermined": 11}, 5, 1))
        self.assertEqual(report["live_product_capability"]["status"], "not_run")
        self.assertEqual([c["status"] for c in report["provenance_conflicts"]], ["unresolved"])
        self.assertEqual(len(report["per_item"]), 26)
        for row in report["per_item"]:
            self.assertIn("tool_output", row)
            if row["layer"] == "comprehension":
                self.assertEqual(row["scored"]["status"], "unscored")
            else:
                operands = row["tool_output"]["operands"]
                self.assertEqual(operands["found_in_anchored_lines"], operands["total"])
        # The report carries hashes and line numbers, never the body or the link.
        self.assertNotIn(MARKER, out)
        self.assertNotIn(fake.PERSONAL_LINK, out)
        captures = (self.state / "use_cases" / pinned.CAPTURES_FILE).read_text("utf-8")
        self.assertNotIn(fake.PERSONAL_LINK, captures)
        # The live check's expectations hold on this output except the pinned Draft 8
        # identity, which a synthetic stand-in must never satisfy.
        spec = json.loads((ROOT / "devloop" / "live-checks" / "eval.use_case_proposal.json")
                          .read_text("utf-8"))
        self.assertEqual(live_verify.check_expectations(spec, code, report),
                         ["original.sha256 != expected", "original.bytes != expected"])
        items = runner.load_items(self.state / "use_cases" / report["items"]["directory"],
                                  split="dev")
        self.assertEqual((len(items), items.refused), (15, report["items"]["refused"]))
        # A tampered stored original is no longer a proven body read.
        stored = self.state / "use_cases" / "originals" / sha256(self.body).hexdigest()
        stored.write_bytes(self.body + b"x")
        code, _, err = self.cli()
        self.assertEqual((code, json.loads(err)), (1, {"error": "original_capture_changed"}))

    def test_a_wrong_proposed_result_or_unknown_layer_fails_closed(self):
        self.assertEqual(self.cli("--capture")[0], 0)
        wrong = copy.deepcopy(self.doc)
        arithmetic = [c for c in wrong["real_example"]["cards"] if c["layer"] == "arithmetic"]
        arithmetic[0]["result"] = "1"
        arithmetic[1]["calculation"] = "__import__('os')"
        path = self.cards.with_name("wrong.json")
        path.write_text(json.dumps(wrong), encoding="utf-8")
        code, out, err = self.cli(cards_path=path)
        self.assertEqual(code, 0, err)
        statuses = {r["card_id"]: r["scored"]["status"] for r in json.loads(out)["per_item"]}
        self.assertEqual((statuses[arithmetic[0]["id"]], statuses[arithmetic[1]["id"]]),
                         ("inconsistent", "unevaluable"))
        case = json.loads(out)["actual_case_answerability"]["items"]
        self.assertEqual((case["answerable"], case["undetermined"]), (10, 13))
        # Anchors past the end of the original: nothing it asks is in these bytes.
        beyond = copy.deepcopy(self.doc)
        for card in beyond["real_example"]["cards"]:
            card["source_lines"] = [[10 ** 6, 10 ** 6]]
        path.write_text(json.dumps(beyond), encoding="utf-8")
        code, out, err = self.cli(cards_path=path)
        self.assertEqual(code, 0, err)
        case = json.loads(out)["actual_case_answerability"]
        self.assertEqual((case["answerability"], case["items"]["blocked"]), ("blocked", 26))
        # An audit is a person's, with a date, stating only what its layer allows.
        for audit in ({"audited_by": "owner", "audited_at": "2026-10-07", "verdict": "ok"},
                      {"audited_by": "owner", "audited_at": "yesterday"},
                      {"audited_by": "owner", "audited_at": "2026-10-07",
                       "values": [{"name": "result", "value": "1"}]}):
            arithmetic[2]["audit"] = audit
            path.write_text(json.dumps(wrong), encoding="utf-8")
            code, _, err = self.cli(cards_path=path)
            self.assertEqual((code, json.loads(err)),
                             (1, {"error": f"card_audit_invalid:{arithmetic[2]['id']}"}))
        del arithmetic[2]["audit"]
        wrong["real_example"]["cards"][0]["layer"] = "verdict"
        path.write_text(json.dumps(wrong), encoding="utf-8")
        code, _, err = self.cli(cards_path=path)
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(err)["error"].startswith("card_layer_unknown:"))


class AnswerRunTests(unittest.TestCase):
    """``--answer``: the admitted items run through a scripted agent on hosts seeded from
    the recorded capture (injected transport for the capture, synthetic stand-in body)."""

    stop = CaptureAndRunTests.stop

    def setUp(self):
        CaptureAndRunTests.setUp(self)
        cards_ = self.doc["real_example"]["cards"]
        self.audited = [c["id"] for c in cards_ if c["layer"] == "arithmetic"][:1]
        self.audited += [c["id"] for c in cards_ if c["layer"] == "comprehension" and
                         c["id"] not in ("RRO-01", "RRO-14")][:1]
        self.wrong = [c["id"] for c in cards_ if c["layer"] == "arithmetic"][2]
        for card in cards_:
            if card["id"] == self.audited[0]:
                card["audit"] = {"audited_by": "release-owner", "audited_at": "2026-10-07"}
            elif card["id"] == self.audited[1]:
                card["audit"] = {"audited_by": "release-owner", "audited_at": "2026-10-07",
                                 "gaps": [{"state": "absent_in_source", "subject": "kit SKU",
                                           "reason": "not_in_document"}]}
        self.cards.write_text(json.dumps(self.doc), encoding="utf-8")
        self.calls = 0
        item = self.drive.item   # the drive item carries the original's own name
        self.drive.item = lambda: {**item(), "name": self.doc["source"]["original"]["name"]}

    def script(self, prompt, call):
        """Find the original by name, read its pages, answer arithmetic with an expression
        over the figures on the anchored line's page; comprehension with its gaps only."""
        self.calls += 1
        question = prompt.split("Question: ", 1)[1].split("\n", 1)[0]
        card = next(c for c in self.doc["real_example"]["cards"]
                    if question.endswith(c["question"]))
        listed = call("plx.documents", {"query": self.doc["source"]["original"]["name"]})
        citation = listed["documents"][0]["citation"]
        pages = []
        while True:
            read = call("plx.read", {"citation": f"{citation}#page={len(pages) + 1}"})
            if not isinstance(read, dict) or read.get("status") != "resolved":
                break
            pages.append((read["citation"], read["content"]["untrusted_text"]))
        answer = {"format": score.ANSWER_FORMAT, "answerability": "partial", "values": [],
                  "gaps": list(card.get("audit", {}).get("gaps", [])), "text": "Not stated."}
        if card["layer"] == "arithmetic":
            line = self.body.decode().split("\n")[card["source_lines"][0][0] - 1]
            page = next(c for c, text in pages if re.search(re.escape(line) + "$", text, re.M))
            parts = card["calculation"].split(";")
            for index, part in enumerate(parts, 1):
                names = iter(range(100))
                value = tool.recompute(part)[0] + (card["id"] == self.wrong)
                answer["values"].append({
                    "name": "result" if len(parts) == 1 else f"result_{index}",
                    "value": str(value), "calculation": {
                        "op": "expression",
                        "expression": tool._OPERAND.sub(lambda m: f"x{next(names)}", part),
                        "inputs": [{"l1_handle": page, "field_path": "text",
                                    "value": f"{Decimal(o):,}"}
                                   for o in tool._OPERAND.findall(part)]}})
            answer.update(answerability="answerable", text="The result is " + " and ".join(
                v["value"] for v in answer["values"]) + ".")
        return fx.final_text(answer)

    def answer(self, *extra, captured_at=None, reader="staff:vince"):
        now = captured_at or datetime.now(timezone.utc)  # recorded permissions age from it
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tool.main(["proposal", "run", "--cards", str(self.cards),
                              "--state-dir", str(self.state), "--evidence-label",
                              "injected-transport", "--capture", "--answer",
                              "--out", str(self.state.parent / "runs"), *extra,
                              *(["--reader", reader] if reader else [])],
                             environ=fake.ENV, graph_sender=self.drive, clock=lambda: now,
                             agent=runner.ScriptedAgent(self.script))
        return code, out.getvalue(), err.getvalue()

    def test_answers_fill_three_buckets_and_accuracy_is_over_scored_items_only(self):
        code, out, err = self.answer()
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        answers = report["answers"]
        self.assertEqual((answers["status"], answers["discovery"]),
                         ("run", "seeded_from_capture"))
        self.assertEqual(answers["original"]["media_type"], "text/plain; charset=utf-8")
        self.assertEqual(self.calls, 16)   # one session per admitted item, none refused
        rows = {r["card_id"]: r for r in answers["per_item"]}
        self.assertEqual({r["discovery"] for r in rows.values()}, {"seeded_from_capture"})
        self.assertEqual({cid: rows[cid]["bucket"] for cid in self.audited},
                         dict.fromkeys(self.audited, "scored"))
        self.assertEqual((rows[self.audited[0]]["outcome"], rows[self.audited[1]]["outcome"]),
                         ("correct", "justified_abstention"))
        self.assertEqual((rows[self.wrong]["bucket"], rows[self.wrong]["outcome"]),
                         ("agreement_with_proposed", "wrong"))
        self.assertIn("calculation_mismatch", rows[self.wrong]["codes"])
        self.assertTrue(all(r["reached_original"] for r in rows.values()
                            if r["outcome"] != "refused"))
        self.assertEqual(answers["totals"], {
            "items": 26, "scored": 2, "agreement_with_proposed": 11, "unscored": 13,
            "gap_only": 4, "blocked": 0, "refused": 10, "coverage": 0.75})
        self.assertEqual({layer: (c["scored"], c["agreement_with_proposed"], c["unscored"],
                                  c["gap_only"], c["refused"])
                          for layer, c in answers["by_layer"].items()},
                         {"comprehension": (1, 0, 13, 4, 10), "arithmetic": (1, 11, 0, 0, 0)})
        # Accuracy is the scored items' only; agreement is never called accuracy.
        self.assertEqual((answers["scored"]["runs"], answers["scored"]["correct"],
                          answers["scored"]["accuracy"], answers["scored"]["coverage"]),
                         (2, 1, 1.0, 0.5))
        self.assertNotIn("accuracy", answers["agreement_with_proposed"])
        self.assertEqual({k: answers["agreement_with_proposed"][k]
                          for k in ("runs", "agree", "disagree", "blocked")},
                         {"runs": 11, "agree": 10, "disagree": 1, "blocked": 0})
        self.assertNotIn(MARKER, out)
        spec = json.loads((ROOT / "devloop" / "live-checks" / "eval.use_case_answers.json")
                          .read_text("utf-8"))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", spec["requires_env"])  # either name:
        self.assertEqual(spec["requires_env_any"],
                         ["CLAUDE_CODE_OAUTH_TOKEN", "PLX_CLAUDE_CODE_OAUTH_TOKEN"])
        self.assertEqual(live_verify.check_expectations(spec, code, report),
                         ["original.sha256 != expected", "original.bytes != expected",
                          "answers.scored.coverage < 0.8"])  # 1 of 2 scored: under B1
        # Audited cards: no blocked part, so a miss stays a fail, never a blocked.
        self.assertNotIn("blocked_upstream", report)
        self.assertIsNone(answers["blocked_reason"])
        # Without the audits nothing is scored and the check cannot pass on agreement.
        for card in self.doc["real_example"]["cards"]:
            card.pop("audit", None)
        self.cards.write_text(json.dumps(self.doc), encoding="utf-8")
        code, out, _ = self.answer()
        report = json.loads(out)
        self.assertEqual((report["answers"]["totals"]["scored"],
                          report["answers"]["totals"]["unscored"],
                          report["answers"]["scored"]["accuracy"]), (0, 14, None))
        self.assertIn("answers.scored.runs < 1",
                      live_verify.check_expectations(spec, code, report))
        # The answers ran, but nothing is scorable: a typed blocker on the owner's audit
        # (a later ask), so the verifier records blocked, never pass.
        self.assertEqual((report["answers"]["status"], report["answers"]["blocked_reason"]),
                         ("run", "no_human_audited_items"))
        self.assertEqual(report["blocked_upstream"], [{
            "part": "use_case_answers_scored", "source": "use-case-cards",
            "error_code": "no_human_audited_items", "cause": "later_ask"}])
        self.assertEqual([p["cause"] for p in live_verify.blocked_parts(report)],
                         ["later_ask"])
        self.assertEqual(live_verify.check_expectations(spec, code, report,
                                                        spec["expect_blocked"]), [])
        # A blocked shape that lost items or the capture still fails expect_blocked.
        for path, value in (("items", {"loaded": 25}), ("original", {"body_read": None})):
            broken = {**report, path: {**report[path], **value}}
            self.assertNotEqual(live_verify.check_expectations(
                spec, code, broken, spec["expect_blocked"]), [], path)

    def test_an_original_the_reader_cannot_reach_blocks_every_item_without_a_session(self):
        # Recorded permissions decide only while the capture is recent: an older capture
        # leaves the reader nothing to list, so nothing runs and nothing passes.
        code, out, err = self.answer(
            captured_at=datetime.now(timezone.utc) - timedelta(hours=49))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        answers = report["answers"]
        self.assertEqual((answers["status"], answers["blocked_reason"], self.calls),
                         ("blocked", "original_not_listed_for_reader", 0))
        self.assertEqual((answers["totals"]["blocked"], answers["totals"]["coverage"],
                          answers["scored"]["correct"]), (16, 0.0, 0))
        self.assertEqual({r["outcome"] for r in answers["per_item"]}, {"blocked", "refused"})
        self.assertEqual(report["blocked_upstream"], [{"part": "use_case_answers",
                                                       "source": "case-originals",
                                                       "cause": "capture_gap"}])
        spec = json.loads((ROOT / "devloop" / "live-checks" / "eval.use_case_answers.json")
                          .read_text("utf-8"))
        self.assertEqual(live_verify.check_expectations(spec, code, report,
                                                        spec["expect_blocked"]), [])
        # An agent principal (or none) is never the reader.
        for reader in ("agent:release-bot", None):
            code, _, err = self.answer(reader=reader)
            self.assertEqual((code, json.loads(err)),
                             (1, {"error": "reader_must_be_an_employee_principal"}))


class AmbiguousPageFigureTests(unittest.TestCase):
    """The same scripted answers over a stand-in whose lines end on a figure. A figure's
    meaning and label word come from its own line only (``claim_checks._line_breaks``):
    "... 5,984" ending a line never means the "STAND" opening the next, so A04, A08 and
    A12 are credited as on the stand-in with stops. A figure printed with two meanings,
    each on its own line, is still refused bare, never guessed."""

    stop = ""
    setUp, script, answer = AnswerRunTests.setUp, AnswerRunTests.script, AnswerRunTests.answer

    def test_a_line_end_figure_takes_no_meaning_from_the_next_line(self):
        code, out, err = self.answer()
        self.assertEqual(code, 0, err)
        rows = {r["card_id"]: r for r in json.loads(out)["answers"]["per_item"]}
        # A04: 5,984 twice, the last ends its line; A12: 12 ends its line and is printed
        # mid-line too; A08: 1,584 is also the page's line number 1584 at a line end.
        for cid in ("A04", "A08", "A12"):
            self.assertEqual(rows[cid]["outcome"], "correct", (cid, rows[cid]["codes"]))
        self.assertEqual({cid for cid, r in rows.items() if r["layer"] == "arithmetic"
                          and r["outcome"] == "wrong"}, {self.wrong})

    def test_a_figure_printed_with_two_meanings_on_two_lines_is_refused_not_guessed(self):
        from operating import claim_checks
        page = "original://doc#page=1"
        text = "Ordered 5,984 units\nSTAND 5,984 kits on hand\n"

        def checked(quote=None):
            claim = {"value": "5984", "citation": page, "field": "text",
                     **({"quote": quote} if quote else {})}
            parsed = claim_checks.parse_value_claims([claim], [page], [])
            return claim_checks.check_items(
                [{"id": "F1", "texts": [], "value_claims": parsed,
                  "citations": [{"handle": page, "status": "resolved"}]}],
                rows={page: {"page": 1, "text": text}}.get)
        bare = checked()
        self.assertEqual([f["code"] for f in bare["failures"]], ["claim_value_mismatch"])
        self.assertIn("ambiguous", bare["failures"][0]["detail"])
        self.assertTrue(checked("Ordered 5,984 units")["passed"])
        # The same two lines, the first ending on its figure: one meaning, no ambiguity.
        text = "Ordered 5,984\nSTAND 5,984 kits on hand\n"
        self.assertFalse(checked()["passed"])   # "Ordered 5,984" vs "5,984 kits": two
        text = "Ordered 5,984\nSTAND by. Kept 5,984\n"
        self.assertTrue(checked()["passed"], checked()["failures"])


if __name__ == "__main__":
    unittest.main()
