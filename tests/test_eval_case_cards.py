"""scripts.eval_case_cards (live check eval.case_cards_answers): every committed use-case
card set compiles, a run takes half the cards by rotation so every case runs within two
runs, a run without a named reader is blocked before any session starts, and the verdict
is coverage and refused share, never accuracy. Agents here are injected transports (a
ClaudeCodeAgent whose spawn returns stream-json, or a scripted agent), never a session."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from verify import live_verify
from evals import runner, score
from evals.tests import fixtures as fx
from scripts import eval_case_cards as tool

NOW = "2026-10-16T00:00:00Z"
CASES = ["amazon-fba", "artwork-lifecycle", "asset-tracker", "brand-requirements",
         "expired-disposition", "grants-program", "packaging-sourcing", "procure-to-gl",
         "quote-to-order", "ricardo-maps", "rnd-maps", "rro", "screening"]
SPEC = tool.ROOT / "devloop" / "live-checks" / "eval.case_cards_answers.json"
# The committed cards that point at one record without naming it ("this AO",
# "product P") and that no committed source names a record for: refused
# ``card_anchor_missing``, each with its ``anchor_gap`` (why no anchor). None today:
# each card's own source names the record its worked example is about.
UNANCHORED: dict = {}
# The committed card anchors: the record each card's own committed source names.
ANCHORED = {
    ("artwork-lifecycle", "AL-06"): ("K18", "PackComponent"),
    ("artwork-lifecycle", "AL-07"): ("P-0116", "Product"),
    ("artwork-lifecycle", "AL-11"): ("Lavender Spa Conditioner", "ArtworkVersion"),
    ("asset-tracker", "AT-03"): ("C-8-006", "Asset"),
    ("asset-tracker", "AT-04"): ("P-8-02", "Asset"),
    ("asset-tracker", "AT-05"): ("P-8-02", "Asset"),
    ("asset-tracker", "AT-09"): ("P-1-003", "Asset"),
    ("asset-tracker", "AT-11"): ("260556", "PurchaseInvoice"),
    ("asset-tracker", "AT-12"): ("P-1-003", "Asset"),
    ("brand-requirements", "BR-09"): ("NBY", "Customer"),
    ("brand-requirements", "BR-10"): ("NBY", "Customer"),
    ("brand-requirements", "BR-11"): ("NBY-001-118", "Product"),
    ("brand-requirements", "BR-12"): ("NBY-001-118", "Product"),
    ("brand-requirements", "BR-13"): ("NBY-001-118", "Product"),
    ("brand-requirements", "BR-14"): ("NBY-001-118", "Product"),
    ("grants-program", "GP-03"): ("1399", "PurchaseInvoice"),
    ("grants-program", "GP-04"): ("8KUVN8DP-0077", "PurchaseInvoice"),
    ("grants-program", "GP-07"): ("260556", "PurchaseInvoice"),
    ("grants-program", "GP-09"): ("1399", "PurchaseInvoice"),
    ("procure-to-gl", "SP-04"): ("PO#4144", "PurchaseOrder"),
    ("procure-to-gl", "SP-05"): ("PO10005687", "PurchaseOrder"),
    ("artwork-lifecycle", "AL-08"): ("PO 5503", "PurchaseOrder"),
    ("artwork-lifecycle", "AL-09"): ("AO-26005E", "AssemblyOrder"),
    ("artwork-lifecycle", "AL-10"): ("AO-26005E", "AssemblyOrder"),
    ("procure-to-gl", "PG-05"): ("PO5447", "PurchaseOrder"),
    ("procure-to-gl", "PG-06"): ("PO5447", "PurchaseOrder"),
    ("procure-to-gl", "PG-07"): ("PO5447", "PurchaseOrder"),
    ("rro", "RRO-12"): ("REM000002495", "SalesOrder")}
ANCHOR = {"identifier": "fixture-record-1", "object_type": "Record"}
_ANCHORED: list = []


def anchored_cards() -> Path:
    """The committed card sets, each proposal set given one set-level ``anchor`` (a test
    fixture: the record the unanchored cards mean), so every one of the 187 cards runs."""
    if not _ANCHORED:
        tmp = tempfile.TemporaryDirectory()
        _ANCHORED.append(tmp)
        for case in CASES:
            doc = json.loads((tool.CARDS_DIR / case / tool.CARDS_FILE).read_text("utf-8"))
            doc["real_example"]["anchor"] = dict(ANCHOR)
            (Path(tmp.name) / case).mkdir()
            (Path(tmp.name) / case / tool.CARDS_FILE).write_text(json.dumps(doc), "utf-8")
    return Path(_ANCHORED[0].name)


def tearDownModule():
    for tmp in _ANCHORED:
        tmp.cleanup()


class Host(fx.FakeHost):
    """The case host's shape; counts the sessions opened on it."""

    opened = 0

    def __init__(self) -> None:
        super().__init__()
        type(self).opened += 1

    def close(self) -> None:
        pass


def answer(answerability: str = "partial") -> dict:
    return {"format": score.ANSWER_FORMAT, "answerability": answerability, "values": [],
            "gaps": [{"state": "not_ingested", "subject": "UseCase",
                      "reason": "not_served"}],
            "text": "What the served sources say, and what they do not."}


def nameless(answerability: str = "blocked") -> dict:
    """An abstention that names no gap subject (its reason code only): on a proposal card
    never a justified abstention, whatever its label (scorer /20)."""
    return {**answer(answerability), "gaps": [{"state": "not_ingested", "subject": "",
                                               "reason": "not_served"}]}


def spawned(final: dict, calls: list):
    """A ClaudeCodeAgent transport: one stream-json session that answers ``final``."""

    def spawn(argv, cwd, env, timeout):
        calls.append(argv)
        lines = [{"type": "system", "subtype": "init", "model": "injected"},
                 {"type": "result", "subtype": "success", "result": fx.final_text(final),
                  "usage": {"input_tokens": 1, "output_tokens": 1}}]
        return 0, "\n".join(json.dumps(line) for line in lines) + "\n"
    return spawn


class CompileTest(unittest.TestCase):
    def test_committed_sets_refuse_exactly_the_cards_that_name_no_record(self):
        sets = tool.card_sets()
        self.assertEqual(list(sets), CASES)
        compiled, refused = tool.compile_cards(sets, now=NOW)
        self.assertEqual(len(compiled) + len(refused), 187)
        found: dict = {}
        for row in refused:
            self.assertEqual(row["reasons"], ["card_anchor_missing"])
            found.setdefault(row["case"], []).append(row["card_id"])
        self.assertEqual(found, UNANCHORED)
        # anchored, they all compile, under the ids they would have had all along
        anchored, none = tool.compile_cards(tool.card_sets(anchored_cards()), now=NOW)
        self.assertEqual(none, [])
        self.assertEqual({r["item"]["id"] for r in compiled},
                         {r["item"]["id"] for r in anchored})
        # each committed anchor is the item's root and says which source names it; each
        # refused card says why no committed source names its record
        roots = {(r["case"], r["card_id"]): r["item"]["root"] for r in compiled}
        for key, (identifier, object_type) in ANCHORED.items():
            self.assertEqual((roots[key]["identifier"], roots[key]["object_type"]),
                             (identifier, object_type), key)
        for case, (doc, _) in sets.items():
            for card in doc["real_example"]["cards"]:
                key = (case, card["id"])
                if key in ANCHORED:
                    self.assertTrue(card["anchor"]["source"].strip(), key)
                    self.assertNotIn("anchor_gap", card, key)
                elif card["id"] in UNANCHORED.get(case, []):
                    self.assertTrue(card["anchor_gap"].strip(), key)
                    self.assertNotIn("anchor", card, key)
                else:
                    self.assertFalse({"anchor", "anchor_gap"} & set(card), key)

    def test_all_thirteen_sets_compile_to_187_unique_schema_valid_items(self):
        sets = tool.card_sets(anchored_cards())
        self.assertEqual(list(sets), CASES)
        compiled, refused = tool.compile_cards(sets, now=NOW)
        self.assertEqual((len(compiled), refused), (187, []))
        ids = [row["item"]["id"] for row in compiled]
        self.assertEqual(len(set(ids)), 187)
        self.assertTrue(all(i.endswith(score.EXPOSED_SUFFIX) for i in ids))
        self.assertEqual({row["item"]["split"] for row in compiled}, {"dev"})
        self.assertEqual({row["item"]["family"] for row in compiled},
                         {"F21", "F22", "F23", "F24", "F25"})
        self.assertTrue(all(runner.validate_item(row["item"]) == [] for row in compiled))
        per_case = {c: sum(r["case"] == c for r in compiled) for c in CASES}
        self.assertEqual(per_case["rro"], 26)
        self.assertEqual(sum(per_case.values()), 187)
        # a proposal card is a rubric: no expected value, the criterion in the label note
        item = next(r["item"] for r in compiled if r["case"] == "screening")
        self.assertEqual(item["known_outcome"], {"answerability": "partial", "values": [],
                                                 "gaps": []})
        self.assertEqual(item["label"]["status"], "oracle_only")
        self.assertIn("Criterion:", item["label"]["note"])
        self.assertTrue(item["question"]["text"].startswith("In the screening use case: "))

    def test_compiled_ids_are_stable(self):
        first, _ = tool.compile_cards(tool.card_sets(), now=NOW)
        again, _ = tool.compile_cards(tool.card_sets(), now="2027-01-01T00:00:00Z")
        self.assertEqual([r["item"]["id"] for r in first], [r["item"]["id"] for r in again])

    def test_a_bad_card_is_refused_with_its_reason_and_never_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cards = Path(tmp)
            shutil.copytree(tool.CARDS_DIR / "screening", cards / "screening")
            path = cards / "screening" / tool.CARDS_FILE
            doc = json.loads(path.read_text("utf-8"))
            doc["real_example"]["cards"][0]["layer"] = "guessing"
            doc["real_example"]["cards"][1]["question"] = ""
            path.write_text(json.dumps(doc), "utf-8")
            (cards / "odd").mkdir()
            (cards / "odd" / tool.CARDS_FILE).write_text(json.dumps(
                {"format": "other/v1", "real_example": {"cards": [{"id": "X1"}]}}), "utf-8")
            compiled, refused = tool.compile_cards(tool.card_sets(cards), now=NOW)
            self.assertEqual(len(compiled), 6)
            self.assertEqual(sorted(code for r in refused for code in r["reasons"]),
                             ["card_format_unknown", "card_layer_unknown",
                              "card_question_missing"])
            calls: list = []
            code, doc = run(["--all"], agent=agent_for(answer(), calls), cards_dir=cards)
            self.assertEqual(code, 3)  # 3 of 9 cards refused: above the 10% share
            # a refused card is a failed card with its reason, never skipped
            self.assertEqual(doc["verdict"]["fail_reasons"], [
                "card_refused:odd:X1:card_format_unknown",
                "card_refused:screening:SC-01:card_layer_unknown",
                "card_refused:screening:SC-02:card_question_missing",
                "refused_above_share", "coverage_below:odd", "coverage_below:screening"])
            self.assertEqual((doc["cases"]["odd"]["coverage"],
                              doc["cases"]["screening"]["coverage"]), (0.0, 0.75))
            self.assertEqual((doc["cards"], doc["refused"], len(calls)), (9, 3, 6))
            self.assertEqual(doc["cases"]["odd"]["status"], "refused")
            self.assertEqual(doc["refused_reasons"], {"card_format_unknown": 1,
                                                      "card_layer_unknown": 1,
                                                      "card_question_missing": 1})


class AccountingTest(unittest.TestCase):
    """The scoreboard's accounting is honest and never flatters: an unscored layer is
    reported unscored, a card about an unnamed record is refused, a card is pinned by
    its originals' bytes and never by the time its set was written, and in every case a
    wrong answer to a scored card stays wrong."""

    def rows(self, ids, final, *, spawn=None, agent=None, cards=None):
        compiled, _ = tool.compile_cards(tool.card_sets(cards or anchored_cards()), now=NOW)
        entries = [r for r in compiled if (r["case"], r["card_id"]) in ids]
        self.assertEqual(len(entries), len(ids))
        agent = agent or runner.ClaudeCodeAgent(spawn=spawn or spawned(final, []))
        with tempfile.TemporaryDirectory() as tmp:
            rows = tool.run_cards(entries, agent, Host, out=Path(tmp),
                                  evidence_label="injected-transport", workers=1,
                                  budget_seconds=60)
        return {r["card_id"]: r for r in rows}

    def test_an_unscored_layer_is_reported_unscored_and_a_scored_one_stays_wrong(self):
        # quote-to-order marks actual_case_answerability "UNSCORED until ..."; rnd-maps
        # marks it "UNANSWERED: ... must be named as missing": scored, so its abstention is
        # wrong. A card carrying its derived expected answer (scorer /26) is scored whatever
        # its layer's mark: amazon-fba's are, and only the five quote-to-order cards whose
        # map no store holds stay unscored.
        compiled, _ = tool.compile_cards(tool.card_sets(), now=NOW)
        marked = {(r["case"], r["layer"]) for r in compiled if r.get("unscored")
                  and not r["unscored"].startswith("UNSCORED: ")}
        self.assertEqual(marked, {("quote-to-order", "actual_case_answerability")})
        self.assertEqual(sorted(r["card_id"] for r in compiled if r.get("unscored")),
                         ["QO-A1", "QO-B1", "QO-C1", "QO-D1", "QO-E1"])
        # IL-03 names no registered original (AG-05 rests on a map no one holds: blocked,
        # test_a_card_on_a_map_no_one_holds_is_blocked_and_the_label_alone_never_correct)
        ids = {("quote-to-order", "QO-A1"), ("rnd-maps", "IL-03"), ("amazon-fba", "FBA-01")}
        rows = self.rows(ids, nameless())
        self.assertEqual({k: r["outcome"] for k, r in rows.items()},
                         {"QO-A1": "unscored", "IL-03": "wrong", "FBA-01": "wrong"})
        self.assertTrue(rows["QO-A1"]["abstained"])   # coverage still counts it
        # the scorer itself says unscored (scorer /20): the item carries the set's mark,
        # so the written score, not only this row, is never wrong (F25-0002, F25-0017)
        compiled, _ = tool.compile_cards(tool.card_sets(anchored_cards()), now=NOW)
        entry = next(r for r in compiled if r["card_id"] == "QO-A1")
        self.assertEqual(entry["item"]["scoring"]["unscored"], entry["unscored"])
        self.assertTrue(entry["unscored"].startswith(score.UNSCORED_MARK))
        self.assertFalse([r["card_id"] for r in compiled
                          if bool(r.get("unscored")) != ("unscored" in r["item"]["scoring"])])
        with tempfile.TemporaryDirectory() as tmp:
            tool.run_cards([entry], agent_for(nameless(), []), Host, out=Path(tmp),
                           evidence_label="injected-transport", workers=1, budget_seconds=60)
            written = json.loads((Path(tmp) / entry["item"]["id"] / "p0-r0.score.json")
                                 .read_text("utf-8"))
        self.assertEqual((written["outcome"], written["excluded"]), ("unscored", "unscored"))
        # a stated, uncited answer (an agent without the session's answer gate): wrong
        # where scored, unscored in the unscored layer and still confident unsupported
        stated = {**answer("answerable"), "gaps": [],
                  "values": [{"name": "moved", "value": "2026-10-13", "citations": []}]}
        rows = self.rows(ids, None, agent=runner.ScriptedAgent(
            lambda prompt, call: fx.final_text(stated)))
        self.assertEqual((rows["QO-A1"]["outcome"], rows["IL-03"]["outcome"]),
                         ("unscored", "wrong"))
        self.assertTrue(rows["QO-A1"]["confident_unsupported"])
        # a failed session stays blocked, never unscored
        rows = self.rows(ids, None, spawn=lambda *a: (1, ""))
        self.assertEqual({r["outcome"] for r in rows.values()}, {"blocked"})
        counted = tool.counts(list(self.rows(ids, nameless()).values()))
        self.assertEqual((counted["unscored"], counted["blocked"]), (1, 0))

    def without_own_anchor(self, tmp: str, card_id: str, shared: dict) -> Path:
        """A copy of the artwork-lifecycle set whose ``card_id`` has no anchor of its own
        and whose set carries ``shared``."""
        shutil.copytree(tool.CARDS_DIR / "artwork-lifecycle", Path(tmp) / "artwork-lifecycle")
        path = Path(tmp) / "artwork-lifecycle" / tool.CARDS_FILE
        doc = json.loads(path.read_text("utf-8"))
        doc["real_example"]["anchor"] = shared
        for card in doc["real_example"]["cards"]:
            if card["id"] == card_id:
                card.pop("anchor")
        path.write_text(json.dumps(doc), "utf-8")
        return Path(tmp)

    def test_an_anchor_is_carried_into_the_question_and_root_and_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            compiled, _ = tool.compile_cards(tool.card_sets(
                self.without_own_anchor(tmp, "AL-11", dict(ANCHOR))), now=NOW)
        item = next(r["item"] for r in compiled if r["card_id"] == "AL-11")
        self.assertEqual((item["root"]["identifier"], item["root"]["object_type"]),
                         ("fixture-record-1", "Record"))   # the set-level anchor
        self.assertTrue(item["question"]["text"].startswith(
            "In the artwork-lifecycle use case, for Record fixture-record-1: Which formula"))
        compiled, _ = tool.compile_cards(tool.card_sets(anchored_cards()), now=NOW)
        by = {(r["case"], r["card_id"]): r["item"] for r in compiled}
        item = by[("artwork-lifecycle", "AL-09")]   # its own anchor wins over the set's
        self.assertEqual((item["root"], item["question"]["text"][:70]), (
            {"identifier": "AO-26005E", "identifier_kind": "ao_number",
             "object_type": "AssemblyOrder"},
            "In the artwork-lifecycle use case, for AssemblyOrder AO-26005E: For th"))
        plain = by[("artwork-lifecycle", "AL-01")]  # names no record: keeps its batch root
        self.assertEqual(plain["root"]["identifier_kind"], "case_card_batch")
        # anchored, the card is scored: an abstention naming no gap is still wrong
        rows = self.rows({("artwork-lifecycle", "AL-11")}, nameless())
        self.assertEqual(rows["AL-11"]["outcome"], "wrong")
        # a malformed anchor is no anchor: refused, never run
        with tempfile.TemporaryDirectory() as tmp:
            compiled, refused = tool.compile_cards(tool.card_sets(self.without_own_anchor(
                tmp, "AL-11", {"identifier": " ", "object_type": "Record"})), now=NOW)
        self.assertEqual(sorted(r["card_id"] for r in refused), ["AL-11"])
        self.assertEqual({tuple(r["reasons"]) for r in refused}, {("card_anchor_missing",)})

    def test_a_blocked_abstention_keeps_its_reasons_and_an_rro_anchor_is_carried(self):
        from operating.case_originals import OriginalStore
        from operating.case_runtime import CASE_RUNTIME_DIR
        with tempfile.TemporaryDirectory() as tmp:
            OriginalStore(Path(tmp) / CASE_RUNTIME_DIR)   # holds no original

            class Empty(Host):
                state_dir = Path(tmp)
            compiled, _ = tool.compile_cards(tool.card_sets(anchored_cards()), now=NOW)
            entry = next(r for r in compiled if r["card_id"] == "AT-01")
            with tempfile.TemporaryDirectory() as out:
                rows = tool.run_cards([entry], agent_for(nameless(), []), Empty,
                                      out=Path(out), evidence_label="injected-transport",
                                      workers=1, budget_seconds=60)
            with tempfile.TemporaryDirectory() as out:
                named = tool.run_cards([entry], agent_for(answer("blocked"), []), Empty,
                                       out=Path(out), evidence_label="injected-transport",
                                       workers=1, budget_seconds=60)
        self.assertEqual((rows[0]["outcome"], rows[0]["abstain_reasons"]),
                         ("blocked", ["not_served"]))
        self.assertIn("originals_not_held", rows[0]["codes"])
        self.assertEqual(tool.counts(rows)["abstain_reasons"], {"not_served": 1})
        # naming its gap, the blocked abstention is justified, held or not (scorer /20)
        self.assertEqual((named[0]["outcome"], named[0]["abstain_reasons"]),
                         ("justified_abstention", ["not_served"]))
        item = next(r["item"] for r in compiled if r["card_id"] == "RRO-12")
        self.assertEqual((item["root"]["identifier"], item["root"]["identifier_kind"]),
                         ("REM000002495", "customer_po"))
        self.assertIn("(Draft 8), for SalesOrder REM000002495: Can this production PO",
                      item["question"]["text"])

    def test_a_card_on_a_map_no_one_holds_is_blocked_and_the_label_alone_never_correct(self):
        # The maps behind these cards are
        # registered card rows (examples/case_originals_card_sources.json). Laura's ZIP is
        # a pinned Teams chat attachment; the R&D maps are pinned by the rendered-text
        # SHA-256 of the source records that hold their complete text. The runner checks
        # the host holds those bytes; a gap row with no SHA-256 stays unpinned (no one
        # holds it).
        registry = tool.registry_pins()
        compiled, refused = tool.compile_cards(tool.card_sets(), now=NOW)
        by = {r["card_id"]: r["item"] for r in compiled}
        zip_pin = registry["co-eb9a6a206832"]
        self.assertEqual(zip_pin, "original://sha256/1c081e639484c04ad5a82dc16244caa917cf3a"
                                  "e33889fb1848ab92cf02afd6d9")
        for card in ("BR-01", "BR-02", "BR-03", "BR-04", "BR-05", "BR-06", "BR-07", "BR-08",
                     "BR-15"):
            self.assertEqual(runner.item_pins(by[card]), [zip_pin], card)
            self.assertEqual(score.unheld_originals(by[card]), [], card)
        agenda, samples, lists = ("a2d0981962e8cecf2c1ea9a61451a711ed1cdcd6ee3b14ae3f6d36dbb79e3446",
                                  "60cfb8f77f4ff6577ff60c2b44810edeaca3e0a8ea56ede18f2996de384d57b7",
                                  "28145aacc2c6f35203766e2936779676f2329348b266bec1974d12b99a988064")
        maps = {"AG-01": agenda, "RS-01": samples, "IL-01": lists, "AG-03": agenda,
                "AG-05": agenda, "AG-06": agenda, "RS-03": samples, "RS-04": samples,
                "IL-02": lists}
        for card, digest in maps.items():
            self.assertEqual((runner.item_pins(by[card]), score.unheld_originals(by[card])),
                             ([f"original://sha256/{digest}"], []), card)
            self.assertEqual(runner.validate_item(by[card]), [], card)
        unpinned = [row for row, pin in registry.items()
                    if (pin or "").startswith("original://unpinned/")]
        self.assertTrue(unpinned)
        for row in unpinned:
            self.assertEqual(registry[row], f"original://unpinned/{row}")
        self.assertFalse([r for r in refused if r["case"] in ("brand-requirements",
                                                               "rnd-maps")
                          and r["reasons"] != ["card_anchor_missing"]])
        # a card on a map no one holds (AG-05 here naming an unpinned gap row instead of
        # its pinned map): a failed abstention is the state's gap; on IL-03 (no
        # registered map) wrong
        with tempfile.TemporaryDirectory() as tmp:
            doc = json.loads((anchored_cards() / "rnd-maps" / tool.CARDS_FILE)
                             .read_text("utf-8"))
            next(c for c in doc["real_example"]["cards"]
                 if c["id"] == "AG-05")["originals"] = unpinned[:1]
            (Path(tmp) / "rnd-maps").mkdir()
            (Path(tmp) / "rnd-maps" / tool.CARDS_FILE).write_text(json.dumps(doc), "utf-8")
            pair = {("rnd-maps", "AG-05"), ("rnd-maps", "IL-03")}
            rows = self.rows(pair, nameless(), cards=Path(tmp))
            self.assertEqual((rows["AG-05"]["outcome"], rows["IL-03"]["outcome"]),
                             ("blocked", "wrong"))
            self.assertIn("originals_not_held", rows["AG-05"]["codes"])
            # an abstention naming its gap is justified on both, labelled blocked (/20)
            # or partial
            labelled = {label: self.rows(pair, answer(label), cards=Path(tmp))
                        for label in ("blocked", "partial")}
        for label, rows in labelled.items():
            self.assertEqual({r["outcome"] for r in rows.values()}, {"justified_abstention"})
        # the partial label with gaps and no evidenced claim: an abstention, never correct
        rows = self.rows({("amazon-fba", "FBA-01"), ("rnd-maps", "IL-03")}, answer())
        self.assertEqual({k: (r["outcome"], r["label_only"], r["correct_with_evidence"])
                          for k, r in rows.items()},
                         {"FBA-01": ("justified_abstention", True, False),
                          "IL-03": ("justified_abstention", True, False)})
        counted = tool.counts(list(rows.values()))
        self.assertEqual((counted["label_only"], counted["correct_with_evidence"]), (2, 0))

    def test_each_proposal_item_carries_its_card_specific_terms_whatever_was_compiled(self):
        compiled, _ = tool.compile_cards(tool.card_sets(), now=NOW)
        generic = tool.generic_terms()
        self.assertTrue({"portal", "order", "review"} <= generic)
        for row in compiled:
            item = row["item"]
            terms = (item.get("scoring") or {}).get("topic_terms")
            if not score.rubric_item(item):
                self.assertIsNone(terms, row["card_id"])   # a value-graded RRO item
                continue
            title = score._subject_terms(f"{row['case']} {item['root']['identifier']}")
            self.assertFalse(set(terms) & (generic | title), row["card_id"])
        # one set compiled alone gets the same terms (generic words are the committed sets')
        by = {r["card_id"]: r["item"] for r in compiled}
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(tool.CARDS_DIR / "rnd-maps", Path(tmp) / "rnd-maps")
            alone, _ = tool.compile_cards(tool.card_sets(Path(tmp)), now=NOW)
        for row in alone:
            self.assertEqual(row["item"]["scoring"]["topic_terms"],
                             by[row["card_id"]]["scoring"]["topic_terms"], row["card_id"])

    def test_each_rubric_item_carries_its_own_and_the_other_sets_source_documents(self):
        """Scorer /21: a card's source documents come from its set's data (watched files,
        archive or original, the registry's case original, its maps' own files); every other set's are foreign, except an original
        the card itself names."""
        sets = tool.card_sets()
        compiled, _ = tool.compile_cards(sets, now=NOW)
        docs = {}
        for row in compiled:
            scoring = row["item"].get("scoring") or {}
            if not score.rubric_item(row["item"]):
                self.assertNotIn("source_documents", scoring, row["card_id"])
                continue
            own, foreign = set(scoring["source_documents"]), set(scoring["foreign_documents"])
            self.assertFalse(own & foreign, row["card_id"])
            docs[row["card_id"]] = (row["item"], own, foreign)

        def source(case):
            return sets[case][0]["source"]

        def prefix(p):
            return [v for k, v in docs.items() if k.split("-")[0] == p]
        fba = {f["sha256"] for f in source("amazon-fba")["watched_originals"]["files"]}
        zip_ = {source("screening")["original_zip"]["sha256"]}
        rro = {source("rro")["original"]["sha256"]}
        qo = {f["sha256"] for f in sets["quote-to-order"][0]["maps"]["QO"]["source_files"]}
        brief = set(tool.case_originals()[sets["asset-tracker"][0]["case"]])
        # the use-case catalog's byte-pinned originals (use_case_documents): the Ricardo
        # maps (registry rows of kind "... process map package") and the procure-to-gl
        # map (a rendered text); a SharePoint catalog item names no bytes
        catalog = tool.use_case_documents()
        ricardo, pg_map = set(catalog["ricardo-maps"]), set(catalog["procure-to-gl"])
        self.assertEqual((len(ricardo), len(pg_map)), (6, 1))
        self.assertFalse(catalog["screening"] or catalog["amazon-fba"])
        for p, expected in (("FBA", fba), ("SC", zip_), ("SP", zip_ | pg_map), ("RRO", rro),
                            ("AT", brief), ("PG", pg_map), ("RM", ricardo), ("BR", {
                                source("brand-requirements")["original_archive"]["sha256"]})):
            self.assertTrue(prefix(p), p)
            for _, own, _ in prefix(p):
                self.assertEqual(own, expected, p)
        # the quote map's own files and the watched quote workbooks, on every card of the set
        for p in ("QO", "QW", "CJ"):
            for _, own, _ in prefix(p):
                self.assertTrue(qo < own and len(own) == len(qo) + len(
                    source("quote-to-order")["watched_originals"]["files"]), p)
        # the packaging-sourcing set names the Sept 30 map
        # as its own source: own on every PS card, foreign to no PS card
        ps_map = {source("packaging-sourcing")["related_advisories"][0]["source_revision"]
                  .partition("sha256:")[2]}
        self.assertTrue(prefix("PS"))
        for _, own, foreign in prefix("PS"):
            self.assertTrue(ps_map <= own and not ps_map & foreign)
        # BR-05's evidence came from the Artwork map; the procure-to-gl PG cards never own
        # the screening map their SP cards share
        artwork = set(tool.case_originals()[sets["artwork-lifecycle"][0]["case"]])
        self.assertTrue(artwork <= docs["BR-05"][2] and zip_ <= docs["PG-01"][2])
        # an original a card names is never foreign to it (AT cards naming the grants brief)
        grants = set(tool.case_originals()[sets["grants-program"][0]["case"]])
        naming = [k for k, (item, _, _) in docs.items() if k.startswith("AT-") and any(
            f"original://sha256/{g}" in item["oracle"]["derived_from"] for g in grants)]
        self.assertTrue(naming)
        for card_id, (item, own, foreign) in docs.items():
            if card_id.startswith("AT-"):
                self.assertEqual(bool(grants & foreign), card_id not in naming, card_id)
        # one set compiled alone carries the same lists (foreign is the committed sets')
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(tool.CARDS_DIR / "screening", Path(tmp) / "screening")
            alone, _ = tool.compile_cards(tool.card_sets(Path(tmp)), now=NOW)
        for row in alone:
            item, own, foreign = docs[row["card_id"]]
            self.assertEqual(row["item"]["scoring"]["foreign_documents"], sorted(foreign))

    # The audit of 2026-10-28 (decision record, "Scorer /21"): the own-document credits
    # judged undeserved, those judged deserved or borderline-meeting the criterion, and
    # the cards whose criterion no transcript can show met.
    # RM-06 (2026-10-29): closed before the use-case catalog's documents became its own;
    # its cost-map claims would otherwise turn a wrong run correct.
    AUDIT_CLOSED = {"SP-02", "SC-02", "SC-03", "AL-09", "EMD-06", "QO-C2", "RM-06"}
    AUDITED = {"FBA-03", "FBA-05", "QO-01", "QO-02", "PS-07", "AT-08", "SC-04", "EMD-07",
               "EMD-11", "EMD-03", "EMD-05", "EMD-02", "QO-D2", "SP-07", "SP-08", "SC-06",
               "SC-07", "PS-02", "QO-A2", "SC-01", "RM-05", "AT-02", "PS-01", "RM-04"}
    CARD_UNSCORED: set = set()   # QW-01, QW-02 carry their expected answer (scorer /26)

    def test_card_audit_fields_reach_the_item_and_a_cards_mark_wins(self):
        compiled, refused = tool.compile_cards(tool.card_sets(), now=NOW)
        self.assertFalse([r for r in refused if r["reasons"][0].startswith(
            ("card_unscored", "card_own_document"))])
        by = {r["card_id"]: r for r in compiled}
        for card_id, row in by.items():
            scoring = row["item"].get("scoring") or {}
            self.assertEqual("own_document_disabled" in scoring,
                             card_id in self.AUDIT_CLOSED, card_id)
            audit = scoring.get("own_document_audited")
            self.assertEqual(audit is not None, card_id in self.AUDITED, card_id)
            if audit is not None:
                # bound to the audited claims (their audit_digest), under a real date
                self.assertIn(audit["date"], ("2026-10-28", "2026-10-29", "2026-10-30"),
                              card_id)
                self.assertTrue(audit["quotes"] and all(
                    len(q) == 64 for q in audit["quotes"]), card_id)
            if card_id in self.CARD_UNSCORED:
                # the card's own mark, though its layer (comprehension, tracing) is scored
                self.assertTrue(scoring["unscored"].startswith("UNSCORED: "), card_id)
                self.assertEqual(row["unscored"], scoring["unscored"])
                self.assertIsNone(tool.unscored_mark(
                    tool.card_sets()["quote-to-order"][0], row["layer"]))

    def test_a_malformed_card_audit_field_refuses_the_card(self):
        HEX = "ab" * 32
        sets = tool.card_sets()
        doc = json.loads(json.dumps(sets["screening"][0]))
        OWN = next(c for c in doc["real_example"]["cards"] if "expected" in c)[
            "expected"]["document"]
        cards = doc["real_example"]["cards"]
        cases = {"card_unscored_invalid": [{"unscored": "later"}, {"unscored": 1},
                                           {"unscored": "UNSCORED" + "x" * 200}],
                 "card_own_document_invalid": [
                     {"own_document_evidence": False},
                     {"own_document_evidence": True, "own_document_note": "why"},
                     {"own_document_note": "why"},
                     {"own_document_evidence": False, "own_document_note": " "},
                     {"own_document_audited": "2026-10-28"},   # the unbound form
                     {"own_document_audited": {"date": "2026-02-30", "quotes": [HEX]}},
                     {"own_document_audited": {"date": "28 Oct", "quotes": [HEX]}},
                     {"own_document_audited": {"date": "2026-10-28", "quotes": []}},
                     {"own_document_audited": {"date": "2026-10-28", "quotes": ["x"]}},
                     {"own_document_audited": {"date": "2026-10-28", "quotes": [HEX, HEX]}},
                     {"own_document_audited": {"date": "2026-10-28", "quotes": [HEX],
                                               "by": "me"}},
                     {"own_document_audited": {"date": "2026-10-28", "quotes": [HEX]},
                      "own_document_evidence": False, "own_document_note": "why"}],
                 "card_expected_invalid": [
                     {"expected": {"document": "x", "answerability": "partial"}},
                     {"expected": {"document": OWN, "answerability": "answerable"}},
                     {"expected": {"document": OWN, "answerability": "certain"}},
                     {"expected": {"document": OWN, "answerability": "answerable",
                                   "facts": [{"value": "41130", "quote": "expensed"}]}},
                     {"expected": {"document": OWN, "answerability": "partial",
                                   "by": "me"}},
                     {"expected": {"document": OWN, "answerability": "partial"},
                      "unscored": "UNSCORED: later"}],
                 # an expected answer is read from the card's own document only
                 "card_expected_document_foreign": [
                     {"expected": {"document": HEX, "answerability": "partial"}}]}
        for reason, edits in cases.items():
            for edit in edits:
                with self.subTest(edit=edit):
                    changed = json.loads(json.dumps(doc))
                    target = changed["real_example"]["cards"][0]
                    for key in tool.CARD_AUDIT_KEYS:
                        target.pop(key, None)
                    target.update(edit)
                    compiled, refused = tool.compile_cards({"screening": (changed, "0" * 64)},
                                                           now=NOW)
                    self.assertIn({"case": "screening", "card_id": cards[0]["id"],
                                   "layer": cards[0]["layer"], "reasons": [reason]}, refused)
                    self.assertNotIn(cards[0]["id"], {r["card_id"] for r in compiled})

    def test_the_card_report_counts_unaudited_own_document_credits_apart(self):
        compiled, _ = tool.compile_cards(tool.card_sets(), now=NOW)
        entry = next(r for r in compiled if r["card_id"] == "SC-04")

        def scored(audited):
            return {"outcome": "correct", "passed": True, "codes": [],
                    "evidence": {"rubric": True, "evidenced_claims": 1, "label_only": False,
                                 "paths": ["own_document"], "own_document_only": True,
                                 "own_document_audited": audited}}
        rows = [tool._row(entry, scored(True), None), tool._row(entry, scored(False), None)]
        self.assertEqual([r["outcome"] for r in rows], ["correct", "correct"])
        counted = tool.counts(rows)
        self.assertEqual((counted["own_document_only"], counted["own_document_unaudited"]),
                         (2, 1))

    def test_use_case_documents_are_only_byte_pinned_originals_and_fail_closed(self):
        """A catalog document is a source document only through a pinned registry row
        with a SHA-256 (any record kind); a SharePoint item, a gap row, a row with no or a
        malformed digest names no bytes; an unreadable catalog stops the compile."""
        from operating.use_case_catalog import CatalogError
        sp = {"drive_id": "b!" + "A" * 20, "item_id": "01" + "B" * 30}
        catalog = {"schema": "plx-use-case-catalog/v1", "note": "test", "use_cases": [
            {"id": "fx-case", "title": "Fixture case", "phrases": ["fixture case"],
             "documents": [{"role": "map", "original": "co-" + "a" * 12},
                           {"role": "brief", "original": "co-" + "b" * 12},
                           {"role": "evidence", "original": "co-" + "c" * 12},
                           {"role": "evidence", "original": "co-" + "d" * 12},
                           {"role": "map", "sharepoint": sp}], "gaps": []}]}
        rows = [{"id": "co-" + "a" * 12, "status": "pinned", "record_kind": "anything",
                 "expected": {"sha256": "ab" * 32}},
                {"id": "co-" + "b" * 12, "status": "gap", "expected": {"sha256": "cd" * 32}},
                {"id": "co-" + "c" * 12, "status": "pinned", "expected": {"sha256": "AB" * 32}},
                {"id": "co-" + "d" * 12, "status": "pinned", "expected": None}]
        with tempfile.TemporaryDirectory() as tmp:
            path, registry = Path(tmp) / "catalog.json", Path(tmp) / "registry.json"
            path.write_text(json.dumps(catalog), "utf-8")
            registry.write_text(json.dumps({"rows": rows}), "utf-8")
            self.assertEqual(tool.use_case_documents(path, registry), {"fx-case": ["ab" * 32]})
            path.write_text(json.dumps({**catalog, "use_cases": [{"id": "fx-case"}]}), "utf-8")
            with self.assertRaises(CatalogError):
                tool.use_case_documents(path, registry)

    def test_set_documents_read_only_well_formed_digests(self):
        good, bad = "ab" * 32, "AB" * 32
        doc = {"case": "fx-case",
               "source": {"watched_originals": {"files": [{"sha256": good}, {"sha256": bad},
                                                          "x", {"sha256": None}]},
                          "original_zip": {"sha256": "pending"}, "original": "text"},
               "maps": {"FX": {"source_files": [{"sha256": "cd" * 32}, 7]}, "FY": None}}
        self.assertEqual(tool.set_documents(doc, {"fx-case": ["ef" * 32]}),
                         {good, "cd" * 32, "ef" * 32})
        for odd in (None, [], {"source": None}, {"source": {"watched_originals": "all"}}):
            self.assertEqual(tool.set_documents(odd, {}), set())
        # Any other revision names no file's bytes.
        related = {"case": "fx-case", "source": {"related_advisories": [
            {"source_revision": "sha256:" + "12" * 32},
            {"source_revision": "version:1.0;modified:x;sha256:" + "34" * 32},
            {"source_revision": "quoted-text-sha256:" + "56" * 32},
            {"source_revision": "manifest-sha256:" + "57" * 32},
            {"source_revision": "78" * 32}, {"source_revision": "sha256:" + "9A" * 32},
            {"source_revision": "sha256:" + "9b" * 32 + ";sha256:" + "9c" * 32},
            {"source_revision": "sha256:pending"}, {"source_revision": None}, "x"]}}
        self.assertEqual(tool.set_documents(related, {}), {"12" * 32, "34" * 32})
        self.assertEqual(tool.set_documents({"source": {"related_advisories": "all"}}, {}),
                         set())

    # The five runs review found rising from justified (scorer /17) to correct (/18), each
    # unsupported: the claim's value, quote and record title as the saved run had them.
    ROSE_UNDER_18 = [
        ("BR-05", "warns and records who released anyway; it doesn't block, for now", ""),
        ("QO-C2", "G2 was given prices before any cost existed.",
         "mission-control-task-drafts.md"),
        ("RS-01", "designed to help R&D keep track of incoming raw materials and monitoring "
                  "our stock of each raw material", "Claude Artifact - R&D Pain Point"),
        ("IL-01", "giulia@petrasoap.com", "You've been invited to edit an artifact"),
        ("SP-06", "Screener job status, qty good / spoiled",
         "screening-process-map-ontology.csv"),
    ]

    def score_claim(self, item, value, title, label="partial", currency=None,
                    reason="not_served", subject="the map"):
        h = fx.handle("public.app_sales_order", {"id": "so-r1"}, 9)
        row = {"id": "so-r1", "notes": value, **({"name": title} if title else {})}
        answer = {"format": score.ANSWER_FORMAT, "answerability": label,
                  "text": "What the served source says.",
                  "values": [{"name": "said", "value": value, **(
                      {"currency": currency} if currency else {}), "citations": [
                      {"l1_handle": h, "field_path": "row.notes", "quote": value}]}],
                  "gaps": [{"state": "not_ingested", "subject": subject,
                            "reason": reason}]}

        def script(prompt, call):
            call("plx.find", {"relation": "public.app_sales_order"})
            return fx.final_text(answer)
        host = fx.FakeHost({h: ("public.app_sales_order", {"id": "so-r1"}, row)})
        result = score.score(item, runner.record(item, runner.ScriptedAgent(script), host))
        self.assertTrue(result["claim_checks"]["passed"], result)
        return result

    def test_no_claim_of_the_five_cards_that_rose_under_scorer_18_is_evidence(self):
        compiled, _ = tool.compile_cards(tool.card_sets(), now=NOW)
        by = {r["card_id"]: r["item"] for r in compiled}
        for card, value, title in self.ROSE_UNDER_18:
            with self.subTest(card=card):
                result = self.score_claim(by[card], value, title)
                self.assertEqual((result["outcome"], result["evidence"]["label_only"]),
                                 ("justified_abstention", True))
        # a claim stating the card's own subject in two of its words, one card-specific,
        # still counts (EMD-A2: the review notes on the justification's risks)
        result = self.score_claim(by["EMD-A2"], "justification cites oxidative change and "
                                  "solvent ratio integrity, neither measured by the panel",
                                  "REVIEW-NOTES.md")
        # its expected answer (scorer /26) is "oxidation", stated here as "oxidative"
        self.assertEqual((result["outcome"], result["evidence"]["evidenced_claims"],
                          result["evidence"]["label_only"], result["codes"]),
                         ("correct", 1, False, []))
        self.assertIn("giulia", by["IL-01"]["scoring"]["topic_names"])

    def test_a_cards_expected_answer_only_adds_failures(self):
        # Scorer /26: an expected answer read from the card's own pinned document. Stating
        # its fact in a gate-passed value is correct; an evidenced claim without it, an
        # ``answerable`` label where the document supports only ``partial``, or an amount
        # where the card forbids one is wrong; it never turns a wrong run correct.
        compiled, _ = tool.compile_cards(tool.card_sets(), now=NOW)
        by = {r["card_id"]: r["item"] for r in compiled}
        expected = by["EMD-A2"]["scoring"]["expected"]
        self.assertEqual(expected["document"], next(iter(score._documents(
            by["EMD-A2"], "source_documents") & {expected["document"]})))
        self.assertIn("original://sha256/" + expected["document"],
                      by["EMD-A2"]["oracle"]["derived_from"])
        said = ("justification cites oxidative change and solvent ratio integrity; "
                "no oxidation test was run")
        self.assertEqual(self.score_claim(by["EMD-A2"], said, "REVIEW-NOTES.md")["outcome"],
                         "correct")
        unexpected = {**by["EMD-A2"], "scoring": {
            k: v for k, v in by["EMD-A2"]["scoring"].items() if k != "expected"}}
        self.assertEqual(self.score_claim(unexpected, said, "REVIEW-NOTES.md")["outcome"],
                         "correct")
        # a fact matches on word stems: the document's own "oxidative" states "oxidation"
        self.assertEqual(self.score_claim(by["EMD-A2"], "the justification names oxidative "
                                          "change and no panel test covers it",
                                          "REVIEW-NOTES.md")["outcome"], "correct")
        # PS-A2: the proposal's mismatch must be stated (in any accepted form), with no
        # amount; the PI number the question names is never the fact
        self.assertEqual(score.expected_echoes(by["PS-A2"]["scoring"]["expected"],
                                               by["PS-A2"]["question"]["text"]), [])
        said = "the proposal says the PI came before the PO and its prices don't match"
        cases = {(said, None): ("correct", []),
                 ("the proposal reports a PI mismatch on the order", None):
                     ("wrong", ["expected_fact_missing"]),
                 (said, "USD"): ("wrong", ["amount_stated"])}
        for (value, currency), want in cases.items():
            with self.subTest(value=value, currency=currency):
                result = self.score_claim(by["PS-A2"], value, "PI-SWC025623 proposal",
                                          "partial", currency)
                self.assertEqual((result["outcome"], result["codes"]), want)
        # BR-15: the account has no source bundle, so the answer stays partial
        said = "the Three Ships supplier switch was never reviewed and Quality was not told"
        result = self.score_claim(by["BR-15"], said, "map", "answerable")
        self.assertEqual((result["outcome"], result["codes"]),
                         ("wrong", ["answerability_mismatch"]))
        # a run whose host could not read the card's own document is blocked, never
        # wrong; an unreadable other document (or "the original" unnamed) excuses nothing
        own = by["EMD-A2"]["scoring"]["expected"]["document_name"]
        for subject, want in ((own, "blocked"), (own.rsplit(".", 1)[0], "blocked"),
                              ("Screening Process Map.zip", "wrong"),
                              ("source:original", "wrong")):
            with self.subTest(subject=subject):
                result = self.score_claim(by["EMD-A2"], "the justification is unread", "map",
                                          reason="text_not_extracted", subject=subject)
                self.assertEqual(result["outcome"], want)
        # an item whose expected answer is malformed is never scored
        broken = {**by["PS-A2"], "scoring": {**by["PS-A2"]["scoring"], "expected": {
            **by["PS-A2"]["scoring"]["expected"], "facts": []}}}
        with self.assertRaises(score.ScoreError):
            self.score_claim(broken, said, "proposal")

    def test_a_fact_must_tell_right_from_wrong(self):
        # a number only whole in its own token; a fact the question states is refused
        fact = {"value": "96", "quote": "12 cases = 96 units"}
        self.assertEqual([score.fact_stated(t, fact) for t in (
            "96 units", "item 0096", "line 96-A", "1,096 units", "96.")],
                         [True, False, False, False, True])
        self.assertTrue(score.fact_stated("in April", {"value": "April 2026", "quote": "x",
                                                       "accept": ["April"]}))
        # polarity: a negated mention never states a positive fact, and a negative fact
        # (the document says it is absent) only where its clause negates it
        def fact(value, polarity="positive"):
            return {"value": value, "quote": "x", "polarity": polarity}
        for text, one, want in (
                ("nothing changed", fact("changed"), False),
                ("the FC changed after the first OR", fact("changed"), True),
                ("not before the BOL", fact("before"), False),
                ("the stock moved before the BOL", fact("before"), True),
                ("No price mismatch", fact("price mismatch"), False),
                ("its prices don't match the item", fact("prices don't match"), True),
                ("all risks including oxidation are covered", fact("oxidation", "negative"),
                 False),
                ("no oxidation test was run", fact("oxidation", "negative"), True),
                ("Yes, the label carries the new expiry", fact("extended", "negative"), False),
                ("it shows the new expiry but not that the lot was extended",
                 fact("extended", "negative"), True),
                ("not 96 units", fact("96"), False)):
            with self.subTest(text=text):
                self.assertEqual(score.fact_stated(text, one), want)
        doc = json.loads(json.dumps(tool.card_sets()["packaging-sourcing"][0]))
        card = next(c for c in doc["real_example"]["cards"] if c["id"] == "PS-A2")
        card["expected"]["facts"] = [{"value": "PI-SWC025623", "quote": "PI-SWC025623 "
                                      "arrived before a PO was raised in the system"}]
        _, refused = tool.compile_cards({"packaging-sourcing": (doc, "0" * 64)}, now=NOW)
        self.assertIn({"case": "packaging-sourcing", "card_id": "PS-A2",
                       "layer": "actual_case_answerability",
                       "reasons": ["card_expected_echoes_question"]}, refused)

    def test_needs_anchor_reads_pointers_and_placeholders_only(self):
        for question in ("For this AO, which lot?", "Which rules apply to product P?",
                         "Which line is this asset on?", "May batch B run?"):
            self.assertTrue(tool.needs_anchor(question), question)
        for question in ("What does this map cover?", "Which inputs were missing in "
                         "examples A to E?", "What does Allison's R&D agenda cover?",
                         "Is the example one transaction from brief to G/L?",
                         "When did PO-00349 (MIDAS2) arrive?",
                         "Which claims do these documents support?",
                         "This AO is late: why?", "What does this source establish?",
                         "Which documents on raw-material R numbers are current?"):
            self.assertFalse(tool.needs_anchor(question), question)

    def test_a_card_is_pinned_by_its_originals_bytes_never_by_its_set_time(self):
        registry = tool.registry_pins()
        compiled, _ = tool.compile_cards(tool.card_sets(), now=NOW)
        item = next(r["item"] for r in compiled if r["card_id"] == "AT-02")
        pins = [ref for ref in item["oracle"]["derived_from"]
                if ref.startswith("original://")]
        # AT-02 names two pinned originals and one the registry lists unfetched (no pin:
        # it cannot be checked as held, so it never excuses an abstention)
        self.assertEqual(pins, sorted(registry[o] for o in
                                      ("co-2b32fe4f123c", "co-14f12ea167ed")))
        self.assertIsNone(registry["co-b24bd7f7afb9"])
        self.assertEqual(runner.item_pins(item), pins)
        prompt = runner.build_prompt(item, item["question"]["text"])
        self.assertNotIn(item["as_known_at"]["time"], prompt)
        self.assertNotIn("as known at", prompt)
        rro = next(r["item"] for r in compiled if r["case"] == "rro")
        self.assertNotIn("as known at", runner.build_prompt(rro, rro["question"]["text"]))
        self.assertEqual(len(runner.item_pins(rro)), 1)   # Draft 8, its line anchors dropped
        # a card carrying its own as_of is time-pinned; an unregistered original refuses
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(tool.CARDS_DIR / "screening", Path(tmp) / "s")
            path = Path(tmp) / "s" / tool.CARDS_FILE
            doc = json.loads(path.read_text("utf-8"))
            doc["real_example"]["cards"][0]["as_of"] = "2026-10-01T00:00:00Z"
            doc["real_example"]["cards"][1]["originals"] = ["co-000000000000"]
            path.write_text(json.dumps(doc), "utf-8")
            compiled, refused = tool.compile_cards(tool.card_sets(Path(tmp)), now=NOW)
        first = compiled[0]["item"]
        self.assertIn("Answer as known at 2026-10-01T00:00:00Z",
                      runner.build_prompt(first, first["question"]["text"]))
        self.assertEqual([(r["card_id"], r["reasons"]) for r in refused],
                         [(doc["real_example"]["cards"][1]["id"],
                           ["card_original_unregistered"])])


class RotationTest(unittest.TestCase):
    def test_every_case_runs_within_two_consecutive_runs(self):
        sizes = {c: n for c, n in zip(CASES, (14, 14, 12, 15, 16, 11, 16, 16, 17, 7, 14, 26, 8))}
        first = tool.rotation(sizes, {})
        self.assertGreaterEqual(sum(sizes[c] for c in first), 94)
        ages = {c: 0 if c in first else 1 for c in CASES}
        second = tool.rotation(sizes, ages)
        self.assertEqual(set(first) | set(second), set(CASES))
        self.assertTrue(set(second) - set(first))
        # whatever the ages, the cases never run in the last two runs lead
        third = tool.rotation(sizes, {c: 0 if c in second else ages[c] + 1 for c in CASES})
        self.assertEqual(set(second) | set(third), set(CASES))

    def test_two_runs_through_the_command_answer_all_187_cards(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            code, one = run([], agent=agent_for(answer(), calls), work=Path(tmp) / "a")
            self.assertEqual(code, 0, one["verdict"])
            report = Path(tmp) / "daily.json"
            report.write_text(json.dumps({"checks": [
                {"id": tool.CHECK_ID, "detail": {"fields": {
                    "cases_age": one["cases_age"]}}}]}), "utf-8")
            code, two = run([f"--previous-report={report}"],
                            agent=agent_for(answer(), calls), work=Path(tmp) / "b")
        self.assertEqual(code, 0, two["verdict"])
        self.assertEqual(set(one["cases_run"]) | set(two["cases_run"]), set(CASES))
        ran = {(c, n) for doc in (one, two) for c in doc["cases_run"]
               for n in [doc["cases"][c]["run"]]}
        self.assertEqual(sum(dict(ran).values()), 187)  # every card answered once at least
        self.assertEqual(len(calls), one["totals"]["run"] + two["totals"]["run"])
        self.assertGreaterEqual(one["totals"]["run"], 87)
        self.assertGreaterEqual(two["totals"]["run"], 87)
        self.assertEqual({c for c, n in two["cases_age"].items() if n == 0},
                         set(two["cases_run"]))
        self.assertEqual({c: n for c, n in two["cases_age"].items() if n},
                         {c: 1 for c in set(one["cases_run"]) - set(two["cases_run"])})
        for doc in (one, two):
            self.assertEqual((doc["status"], doc["card_sets"], doc["cards"]),
                             ("run", 13, 187))
            self.assertEqual(doc["agent_evidence"], "injected-transport")
            for case in doc["cases_run"]:
                entry = doc["cases"][case]
                self.assertEqual((entry["status"], entry["coverage"], entry["not_run"]),
                                 ("run", 1.0, 0))
                self.assertEqual(entry["run"], entry["cards"])
            for case in set(CASES) - set(doc["cases_selected"]):
                self.assertEqual(doc["cases"][case]["status"], "not_selected")

    def test_previous_ages_reads_only_this_check_and_well_formed_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps({"checks": [
                {"id": "other", "detail": {"fields": {"cases_age": {"rro": 9}}}},
                {"id": tool.CHECK_ID, "detail": {"fields": {"cases_age": {
                    "rro": 2, "screening": -1, "Bad Name": 1, "grants-program": True,
                    "asset-tracker": 0}}}}]}), "utf-8")
            self.assertEqual(tool.previous_ages(path), {"rro": 2, "asset-tracker": 0})
            self.assertEqual(tool.previous_ages(Path(tmp) / "missing.json"), {})
        self.assertEqual(tool.previous_ages(None), {})


class VerdictTest(unittest.TestCase):
    def test_an_abstaining_agent_fails_coverage_never_accuracy(self):
        calls: list = []
        code, doc = run(["--all"], agent=agent_for(answer("blocked"), calls))
        self.assertEqual(code, 3)
        self.assertEqual(len(calls), 187)
        self.assertEqual(doc["verdict"]["fail_reasons"],
                         [f"coverage_below:{case}" for case in CASES])
        self.assertNotIn("accuracy", json.dumps(doc["verdict"]["fail_reasons"]))
        # an abstention naming its gap everywhere: every card an honest abstention (its
        # blocked label as a partial one, scorer /20), the nine cards on R&D maps no one
        # holds included (their failed abstention is blocked, AccountingTest)
        self.assertEqual((doc["totals"]["abstained"], doc["totals"]["blocked"],
                          doc["totals"]["answered"]), (187, 0, 0))
        # the cards of a layer its set marks UNSCORED that carry no expected answer (the
        # five quote-to-order cards): reported unscored, never wrong
        self.assertEqual((doc["totals"]["unscored"],
                          doc["by_layer"]["actual_case_answerability"]["unscored"]), (5, 5))
        # The report says why it abstained: the answers' gap reason codes, cards per code.
        self.assertEqual(doc["totals"]["abstain_reasons"], {"not_served": 187})
        self.assertEqual(doc["cases"]["rro"]["abstain_reasons"], {"not_served": 26})

    def test_abstain_reasons_are_codes_never_text(self):
        calls: list = []
        stated = {**answer("blocked"), "gaps": [
            {"state": "inaccessible", "subject": "source:graph_mail",
             "reason": "audience_not_granted"},
            {"state": "unknown", "subject": "x", "reason": "Ask Jane at jane@example.com"}]}
        code, doc = run(["--all", "--per-case", "1"], agent=agent_for(stated, calls))
        self.assertEqual(code, 3)
        self.assertEqual(doc["totals"]["abstain_reasons"],
                         {"audience_not_granted": 13, "other": 13})
        self.assertNotIn("jane", json.dumps(doc))

    def test_a_per_case_sample_touches_every_set_and_says_it_is_a_sample(self):
        calls: list = []
        code, doc = run(["--all", "--per-case", "2"], agent=agent_for(answer(), calls))
        self.assertEqual((code, len(calls)), (0, 26))
        self.assertEqual({c: e["run"] for c, e in doc["cases"].items()},
                         {c: 2 for c in CASES})
        self.assertEqual(doc["sample"], {"per_case": 2, "cards_left_out": 187 - 26})
        self.assertEqual(doc["cards"], 187)  # the set is still counted whole
        code, full = run(["--all"], agent=agent_for(answer(), []))
        self.assertNotIn("sample", full)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run(["--all", "--per-case", "-1"], agent=agent_for(answer(), []))
        # A named sample (the failing cards of an earlier run): only those run.
        picked = ["dev-F23-0001-exposed", "dev-F24-0002-exposed"]
        calls = []
        code, doc = run(["--all", "--items", ",".join(picked)], agent=agent_for(answer(), calls))
        self.assertEqual((len(calls), doc["sample"]),
                         (2, {"items": 2, "cards_left_out": doc["cards"] - 2}))
        with contextlib.redirect_stderr(io.StringIO()):
            code = tool.main(["run", "--work-dir", tempfile.mkdtemp(), "--all", "--items",
                              "dev-F99-0001-exposed"], agent=agent_for(answer(), []),
                             make_host=Host, environ={})
        self.assertEqual(code, 1)   # an id no selected card has is an error, never run

    def test_a_scripted_agent_reads_the_host_and_the_rro_arithmetic_reads_agreement(self):
        def script(prompt, call):
            rows = call("plx.find", {"relation": "public.order_entry_item"})["rows"]
            stated = {**answer("answerable"), "gaps": [], "values": [
                {"name": "qty", "value": rows[0]["row"]["qty"],
                 "citations": [{"l1_handle": rows[0]["citation"], "field_path": "row.qty"}]}]}
            return fx.final_text(stated)
        code, doc = run(["--all"], agent=runner.ScriptedAgent(script))
        self.assertEqual(code, 0, doc["verdict"])
        agreement = doc["cases"]["rro"]["agreement_with_proposed"]
        self.assertEqual((agreement["runs"], agreement["agree"]), (12, 0))
        self.assertEqual(doc["by_layer"]["arithmetic"]["run"], 12)
        self.assertEqual(sum(doc["by_layer"][layer]["run"] for layer in doc["by_layer"]),
                         187)
        self.assertEqual((doc["totals"]["answered"], doc["totals"]["gap_only"]), (187, 0))
        self.assertEqual(doc["totals"]["coverage"], 1.0)

    def test_sessions_that_all_fail_fail_the_run_never_pass_it(self):
        calls: list = []

        def crash(argv, cwd, env, timeout):
            calls.append(argv)
            return 1, ""

        code, doc = run(["--all"], agent=runner.ClaudeCodeAgent(spawn=crash))
        self.assertEqual((code, doc["status"], len(calls)), (3, "run", 187))
        self.assertFalse(doc["verdict"]["passed"])
        self.assertEqual(doc["verdict"]["fail_reasons"],
                         [f"coverage_below:{case}" for case in CASES])
        self.assertEqual({e["status"] for e in doc["cases"].values()}, {"blocked"})
        self.assertEqual(doc["cases_run"], [])
        self.assertEqual(set(doc["cases_age"].values()), {1})  # a failed case keeps ageing

    def test_a_card_not_started_before_the_budget_is_not_run(self):
        calls: list = []
        code, doc = run(["--all", "--budget-seconds", "-1"],
                        agent=agent_for(answer(), calls))
        self.assertEqual((code, len(calls)), (3, 0))  # nothing measured: never a pass
        self.assertEqual(doc["totals"]["not_run"], 187)
        self.assertEqual(doc["verdict"]["fail_reasons"],
                         [f"coverage_below:{case}" for case in CASES])
        self.assertEqual({e["status"] for e in doc["cases"].values()}, {"budget_cut"})
        self.assertEqual(set(doc["cases_age"].values()), {1})  # nothing ran: all age


class BlockedTest(unittest.TestCase):
    def test_without_a_host_or_with_an_invalid_reader_the_run_is_blocked(self):
        # No reader named is the eval principal (labelled), which still needs the host;
        # a named but invalid reader blocks and never falls back to the eval principal.
        calls: list = []
        Host.opened = 0
        for environ, reason, kind in (
                ({}, "session_secret_missing", "eval_principal"),
                ({"PLX_EVAL_READER": "not json"}, "reader_identity_invalid", "none")):
            out = io.StringIO()
            with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
                code = tool.main(["run", "--work-dir", tmp], environ=environ,
                                 agent=agent_for(answer(), calls))
            doc = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual((doc["status"], doc["blocked_reason"], doc["card_sets"],
                              doc["cards"], doc["reader"]["kind"]),
                             ("blocked", reason, 13, 187, kind))
            self.assertFalse(doc["verdict"]["passed"])
            self.assertTrue(all(e["status"] in ("blocked", "not_selected")
                                for e in doc["cases"].values()))
        self.assertEqual((calls, Host.opened), ([], 0))

    def test_a_reader_the_host_refuses_blocks_the_run(self):
        class Refusing(Host):
            def tools(self):
                raise runner.RunnerError("live_host_identity_refused:portal_user_inactive")
        calls: list = []
        code, doc = run([], agent=agent_for(answer(), calls), make_host=Refusing)
        self.assertEqual((code, doc["status"], doc["blocked_reason"], calls),
                         (0, "blocked", "reader_refused", []))

    def test_a_projection_built_from_another_schema_blocks_the_run(self):
        # full173: the host's projection was built from an older schema, so every tool
        # call carried schema_drift and 173 sessions measured that, not the reader.
        import sqlite3
        from ontology.schema import load_schema
        from operating import host as plx_host
        checkout = load_schema().digest
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            plx_host.Host(state)  # the host ledger
            with contextlib.closing(sqlite3.connect(
                    state / plx_host.HOST_DIR / plx_host.LEDGER_FILE)) as db, db:
                db.execute("INSERT INTO projection (generation, built_at, objects, inputs) "
                           "VALUES (1, '2026-10-27T04:24:31Z', 1, '{}')")
            path = plx_host.projection_path(state)
            path.parent.mkdir(parents=True)
            config = Path(tmp) / "host.json"
            config.write_text("{}", encoding="utf-8")
            environ = {"PLX_WORKBENCH_SESSION_SECRET": "test-only-secret",
                       "PLX_WORKBENCH_STATE_DIR": str(state),
                       "PLX_WORKBENCH_HOST_CONFIG": str(config)}
            for served, blocked in (("sha256:" + "0" * 64, True), (checkout, False)):
                path.unlink(missing_ok=True)
                with contextlib.closing(sqlite3.connect(path)) as db, db:
                    db.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
                    db.execute("INSERT INTO store_meta VALUES ('schema_digest', ?)", (served,))
                drift = tool.projection_schema_drift(state)
                self.assertEqual(drift is not None, blocked, drift)
                if not blocked:
                    continue
                self.assertEqual(drift, {"served_schema": "0" * 12,
                                         "checkout_schema": checkout[7:19]})
                calls: list = []
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = tool.main(["run", "--work-dir", str(Path(tmp) / "work")],
                                     environ=environ, agent=agent_for(answer(), calls))
                doc = json.loads(out.getvalue())
                self.assertEqual((code, doc["status"], doc["blocked_reason"], calls),
                                 (0, "blocked", "projection_schema_drift", []))
                self.assertEqual(doc["blocked_upstream"][0]["cause"], "projection_stale")
                self.assertFalse(doc["verdict"]["passed"])
            path.unlink()  # no projection served: not drift (the ledger and tools say so)
            self.assertIsNone(tool.projection_schema_drift(state))

    def test_a_missing_claude_binary_blocks_a_real_session_run(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            code = tool.main(["run", "--work-dir", tmp, "--claude-bin",
                              "/nonexistent/claude"], environ={}, make_host=Host)
        doc = json.loads(out.getvalue())
        self.assertEqual((code, doc["blocked_reason"], doc["agent_evidence"]),
                         (0, "binary_not_found", "real-claude-session"))


class SpecTest(unittest.TestCase):
    def test_the_spec_is_valid_and_asserts_all_thirteen_sets(self):
        spec = json.loads(SPEC.read_text("utf-8"))
        live_verify.validate_spec(spec)
        self.assertIn("scripts.eval_case_cards", live_verify.ALLOWED_MODULES)
        self.assertEqual(spec["expect"]["json_equals"]["cards"], 187)
        self.assertEqual(spec["expect_blocked"]["json_equals"]["card_sets"], 13)
        self.assertIn("cases_age", spec["record_keys"])
        self.assertIs(spec["carry_previous_measured"], True)   # a timeout keeps the rotation
        self.assertIn(live_verify.DAILY_REPORT_ARG, spec["command"])
        self.assertIn("PLX_WORKBENCH_SESSION_SECRET", spec["optional_env"])


def agent_for(final: dict, calls: list) -> runner.ClaudeCodeAgent:
    return runner.ClaudeCodeAgent(spawn=spawned(final, calls))


def run(extra, *, agent, make_host=Host, cards_dir=None, work: Path | None = None):
    out = io.StringIO()
    with contextlib.ExitStack() as stack:
        if work is None:
            work = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(contextlib.redirect_stdout(out))
        code = tool.main(["run", "--work-dir", str(work), *extra], agent=agent,
                         make_host=make_host, environ={},
                         cards_dir=cards_dir or anchored_cards())
    return code, json.loads(out.getvalue())


if __name__ == "__main__":
    unittest.main()
