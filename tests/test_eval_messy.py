"""scripts.eval_messy and scripts.eval_retrieval: every compiled card has three messy
variants that leak no answer and keep the question's numbers; a variant item is asked the
variant only and scored by its card's own scoring; ``--messy`` off leaves the card run
unchanged; a card counts only if its original and every variant are correct; recall@10,
MRR and the latency percentiles are computed right on fixtures. Agents and searches here
are injected, never a session or a host."""

from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from evals import runner, score
from evals.tests import fixtures as fx
from scripts import eval_case_cards as cards
from scripts import eval_messy as messy
from scripts import eval_retrieval as retrieval

NOW = "2026-10-29T00:00:00Z"
_COMPILED: list = []


def compiled():
    if not _COMPILED:
        sets = cards.card_sets()
        rows, _ = cards.compile_cards(sets, now=NOW)
        _COMPILED.append((sets, rows))
    return _COMPILED[0]


class Host(fx.FakeHost):
    def close(self) -> None:
        pass


def answer() -> dict:
    return {"format": score.ANSWER_FORMAT, "answerability": "partial", "values": [],
            "gaps": [{"state": "not_ingested", "subject": "UseCase", "reason": "not_served"}],
            "text": "What the served sources say."}


class VariantIntegrityTest(unittest.TestCase):
    def test_every_compiled_card_has_three_clean_variants(self):
        sets, rows = compiled()
        doc = messy.load()
        self.assertEqual(messy.check(doc, sets, rows), [])
        self.assertEqual(len(doc["cards"]), len(rows))
        self.assertEqual({(e["case"], e["card_id"]) for e in doc["cards"]},
                         {(r["case"], str(r["card_id"])) for r in rows})
        self.assertTrue(all(set(e["variants"]) == set(messy.KINDS) for e in doc["cards"]))

    def test_variant_items_carry_the_cards_scoring_and_ask_the_variant(self):
        sets, rows = compiled()
        entries, skipped = messy.messy_items(rows, messy.load(), sets)
        self.assertEqual((len(entries), skipped), (3 * len(rows), []))
        ids = [e["item"]["id"] for e in entries]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertFalse(set(ids) & {r["item"]["id"] for r in rows})
        by_id = {r["item"]["id"]: r["item"] for r in rows}
        for entry in entries:
            item, parent = entry["item"], by_id[entry["parent_id"]]
            self.assertEqual(runner.validate_item(item), [])
            self.assertEqual(item["id"], parent["id"].replace(
                "-exposed", f"-{entry['messy']}-exposed"))
            self.assertEqual(score.partition_of(item), "exposed")
            self.assertEqual((item["variant"], item["parent_id"]), ("paraphrase", parent["id"]))
            # the scorer reads the card's own question, criterion, scoring and pins
            self.assertEqual(item["question"]["text"], parent["question"]["text"])
            for key in ("scoring", "label", "oracle", "root", "known_outcome",
                        "as_known_at", "must_not", "family"):
                self.assertEqual(item[key], parent[key], key)
            self.assertNotEqual(item["question"]["paraphrases"][0], parent["question"]["text"])

    def test_the_checker_catches_leaks_lost_numbers_source_words_and_changed_cards(self):
        sets, rows = compiled()
        doc = copy.deepcopy(messy.load())
        entry = next(e for e in doc["cards"] if e["card_id"] == "FBA-A4")
        entry["variants"]["m1"] = "Did OR1000154027 bring each SKU up to 90 days?"  # case
        entry["variants"]["m2"] = "did OR1000154027 top up every item to 90 days of supply (45/90 rule)?"
        entry["variants"]["m3"] = "OR1000154027 at 91 days?"   # lost 45/90, leaked 91
        other = next(e for e in doc["cards"] if e["card_id"] == "FBA-01")
        other["question_sha256"] = "0" * 64
        problems = {(p["card_id"], p["variant"], p["code"]) for p in messy.check(doc, sets, rows)}
        self.assertIn(("FBA-A4", "m1", "m1_not_lowercase_typo"), problems)
        self.assertIn(("FBA-A4", "m2", "source_term:supply"), problems)
        self.assertIn(("FBA-A4", "m3", "leak_number:91"), problems)
        self.assertIn(("FBA-A4", "m3", "number_missing:45"), problems)
        self.assertIn(("FBA-01", "", "messy_question_changed"), problems)
        # a changed card's variants never run on its new question
        entries, skipped = messy.messy_items(rows, doc, sets)
        self.assertIn({"case": "amazon-fba", "card_id": "FBA-01",
                       "reason": "messy_question_changed"}, skipped)
        self.assertNotIn("FBA-01", {e["card_id"] for e in entries})
        # a variant saying a word of the card's answer (its topic terms) leaks it
        topic = next(r for r in rows if r["card_id"] == "FBA-01")["item"]["scoring"][
            "topic_terms"]
        leaked = copy.deepcopy(messy.load())
        target = next(e for e in leaked["cards"] if e["card_id"] == "FBA-01")
        target["variants"]["m3"] = f"fba map {topic[-1]}?"
        self.assertIn(("FBA-01", "m3", f"leak_term:{topic[-1]}"),
                      {(p["card_id"], p["variant"], p["code"])
                       for p in messy.check(leaked, sets, rows)})

    def test_every_sub_question_of_the_card_is_asked_by_every_variant(self):
        self.assertEqual(messy.asked_parts(
            "When did the OR1000154027 stock move to the Amazon VMI warehouse, and was "
            "that at the BOL?"), ["When did the OR1000154027 stock move to the Amazon VMI "
                                  "warehouse", "was that at the BOL"])
        parts = messy.asked_parts(
            "Under the stated assumption (seven quoted statement invoices), what does "
            "1584*5+792+1188 give from the figures at lines 1689-1698?")
        self.assertEqual(parts[-1], "(seven quoted statement invoices)")
        self.assertEqual(len(messy.asked_parts(
            "Under the stated assumption (15% of total; not receipt basis), what does "
            "9900*0.15; 9900*0.85 give?")), 2)          # no split inside () or numbers
        self.assertEqual(len(messy.asked_parts("For Soapbox (example D), is it paid?")), 1)
        sets, rows = compiled()
        doc = copy.deepcopy(messy.load())
        entry = next(e for e in doc["cards"] if e["card_id"] == "FBA-A5")
        self.assertEqual(len(entry["parts"]), 2)
        # the m3 that dropped "was that at the BOL", spans declared honestly
        entry["variants"]["m3"] = "OR1000154027 - when did stock move to amazon"
        entry["parts"][0]["spans"]["m3"] = ["OR1000154027 - when did stock move to amazon"]
        entry["parts"][1]["spans"]["m3"] = []
        # an m2 claiming the first part's words for the second, and a one-word span
        entry["parts"][1]["spans"]["m2"] = list(entry["parts"][0]["spans"]["m2"])
        entry["parts"][1]["spans"]["m1"] = ["bol"]
        a08 = next(e for e in doc["cards"] if e["card_id"] == "A08")
        a08["parts"] = a08["parts"][:1]                 # the assumption part left out
        thin = next(e for e in doc["cards"] if e["card_id"] == "RRO-01")
        thin["parts"][1]["spans"]["m3"] = ["vs"]
        problems = {(p["card_id"], p["variant"], p["code"]) for p in messy.check(doc, sets, rows)}
        self.assertIn(("FBA-A5", "m3", "part_dropped:2"), problems)
        self.assertIn(("FBA-A5", "m2", "part_spans_overlap"), problems)
        self.assertIn(("A08", "", "messy_parts_changed"), problems)
        self.assertIn(("RRO-01", "m3", "part_too_thin:2"), problems)
        # an m1 span must read as its part with typos, never one word of it
        self.assertIn(("FBA-A5", "m1", "part_rewritten:2"), problems)

    def test_an_unreadable_or_malformed_variants_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.json"
            for content in ("not json", json.dumps({"format": "other", "cards": []}),
                            json.dumps({"format": messy.FORMAT, "cards": [{"case": "x"}]})):
                path.write_text(content, "utf-8")
                with self.assertRaises(messy.MessyError):
                    messy.load(path)
            # one card listed twice is refused, never one of them silently used
            doc = messy.load()
            path.write_text(json.dumps({**doc, "cards": doc["cards"] + doc["cards"][:1]}),
                            "utf-8")
            with self.assertRaises(messy.MessyError):
                messy.by_card(messy.load(path))
            path.write_text("not json", "utf-8")
            # the card run refuses too: exit 1, no session
            sessions = []
            code, _ = run_main(["--messy", "--messy-variants", str(path),
                                "--items", "dev-F23-0001-exposed"], sessions)
            self.assertEqual((code, sessions), (1, []))


def run_main(extra, prompts):
    def script(prompt, call):
        prompts.append(prompt)
        return fx.final_text(answer())
    out = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(io.StringIO()):
        code = cards.main(["run", "--work-dir", tmp, "--all", "--measure-only", *extra],
                          agent=runner.ScriptedAgent(script), make_host=Host, environ={})
    return code, (json.loads(out.getvalue()) if code == 0 else None)


class CardRunTest(unittest.TestCase):
    ITEMS = "dev-F23-0001-exposed,dev-F24-0050-exposed"

    def test_flag_off_is_the_default_run_and_flag_on_adds_the_variants_apart(self):
        plain_prompts, messy_prompts = [], []
        code, plain = run_main(["--items", self.ITEMS], plain_prompts)
        self.assertEqual(code, 0)
        self.assertNotIn("messy", plain)
        self.assertEqual(len(plain_prompts), 2)
        code, both = run_main(["--messy", "--items", self.ITEMS], messy_prompts)
        self.assertEqual(code, 0)
        self.assertEqual(len(messy_prompts), 8)
        # the cards' counts and verdict are the default run's: variants stay apart
        for key in ("totals", "cases", "by_layer", "verdict", "sample", "compiled", "refused"):
            self.assertEqual(both[key], plain[key], key)
        block = both["messy"]
        self.assertEqual(block["totals"]["cards"], 2)
        self.assertEqual(sum(block["outcomes"]["m1"].values()), 2)
        # each variant session was asked the variant, never the card's question
        doc = messy.load()
        texts = [e["variants"][k] for e in doc["cards"]
                 if e["card_id"] in ("FBA-01", "PG-05") for k in messy.KINDS]
        sets, rows = compiled()
        originals = [r["item"]["question"]["text"] for r in rows
                     if r["card_id"] in ("FBA-01", "PG-05")]
        variant_prompts = [p for p in messy_prompts if not any(o in p for o in originals)]
        self.assertEqual(len(variant_prompts), 6)
        self.assertEqual(sorted(t for t in texts if any(t in p for p in variant_prompts)),
                         sorted(texts))


class AggregateTest(unittest.TestCase):
    def test_a_card_counts_only_if_the_original_and_every_variant_are_correct(self):
        rows = [{"case": "a", "item_id": "dev-F23-0001-exposed", "outcome": "correct"},
                {"case": "a", "item_id": "dev-F23-0002-exposed", "outcome": "correct"},
                {"case": "b", "item_id": "dev-F23-0003-exposed", "outcome": "wrong"},
                {"case": "b", "item_id": "dev-F23-0004-exposed", "outcome": "correct"}]
        outcomes = {"dev-F23-0001-exposed": ["correct"] * 3,
                    "dev-F23-0002-exposed": ["correct", "wrong", "correct"],
                    "dev-F23-0003-exposed": ["correct"] * 3,
                    "dev-F23-0004-exposed": ["correct", "not_run", "correct"]}
        messy_rows = [{"parent_id": pid, "messy": kind, "outcome": out}
                      for pid, outs in outcomes.items()
                      for kind, out in zip(messy.KINDS, outs)]
        report = messy.messy_report(rows, messy_rows)
        self.assertEqual(report["totals"]["all_correct"], 1)
        self.assertEqual(report["totals"]["original_correct"], 3)
        self.assertEqual(report["totals"]["incomplete"], 1)
        self.assertEqual(report["totals"]["m2_correct"], 2)
        self.assertEqual(report["cases"]["a"]["all_correct"], 1)
        self.assertEqual(report["cases"]["b"]["all_correct"], 0)
        # a card whose variants did not run at all is not in the aggregate
        self.assertEqual(messy.messy_report(rows, [])["totals"]["cards"], 0)


class MetricsTest(unittest.TestCase):
    TARGETS = [{"key": "sha256:a", "handles": ["original://co-111111", "original://sha256/a",
                                               "sharepoint://b!d/I1", "name:mapmd1234"]},
               {"key": "sha256:b", "handles": ["original://co-222222"]},
               {"key": "record:PO5447", "text": "po5447"}]

    def test_recall_mrr_and_hit_over_the_top_k(self):
        hits = [{"handle": "graph-mail://x/1", "title": "Re: hello"},
                {"handle": "sharepoint://b%21d/I1@%22v%22%2C3", "title": "x"},  # a, versioned
                {"handle": "original://co-1#page=4", "title": "x"},             # a again
                {"handle": "api://po/9", "title": "Purchase order PO 5447"}]    # record
        got = retrieval.measure(hits, self.TARGETS, k=10)
        self.assertEqual((got["recall"], got["mrr"], got["hit"]), (2 / 3, 0.5, True))
        self.assertEqual(got["found"], ["record:PO5447", "sha256:a"])
        # only the top k count; the denominator is min(targets, k)
        got = retrieval.measure(hits, self.TARGETS, k=2)
        self.assertEqual((got["recall"], got["mrr"]), (0.5, 0.5))
        got = retrieval.measure([{"handle": "x", "title": "Map.md 1234"}], self.TARGETS, k=1)
        self.assertEqual((got["recall"], got["mrr"]), (1.0, 1.0))
        self.assertEqual(retrieval.measure([], self.TARGETS)["mrr"], 0.0)
        self.assertIsNone(retrieval.measure(hits, [])["recall"])
        summary = retrieval.summarize([{"recall": 1.0, "mrr": 1.0, "hit": True},
                                       {"recall": 0.0, "mrr": 0.0, "hit": False},
                                       {"recall": None, "mrr": None, "hit": None},
                                       {"recall": 0.5, "mrr": 0.25, "hit": True,
                                        "error": "Boom"}])
        self.assertEqual((summary["scored"], summary["no_targets"], summary["errors"]), (3, 1, 1))
        self.assertEqual((summary["recall@k"], summary["mrr"], summary["hit@k"]),
                         (0.5, 0.4167, 0.6667))

    def test_one_slot_per_document_and_recall_at_most_one(self):
        wanted = [{"key": "doc", "handles": ["original://co-111111", "sharepoint://b!d/I1"]},
                  {"key": "record:PO5447", "text": "po5447"}]
        hits = [{"handle": "original://co-111111#page=1", "title": "PO5447 map"},
                {"handle": "sharepoint://b%21d/I1@v2", "title": "PO5447 map"},
                {"handle": "original://co-111111#page=2", "title": "PO5447 map"},
                {"handle": "graph-mail://m/1", "title": "about PO5447"}]
        self.assertEqual([h["handle"] for h in retrieval.dedupe(hits, wanted)],
                         ["original://co-111111#page=1", "graph-mail://m/1"])
        got = retrieval.measure(hits, wanted, k=10)
        # the first hit is the document and names the record: one find, not two
        self.assertEqual(got["relevant"][0], ["doc", "record:PO5447"])
        self.assertEqual((got["found"], got["recall"], got["mrr"]),
                         (["doc", "record:PO5447"], 1.0, 1.0))
        one = retrieval.measure(hits[:1], wanted, k=10)
        self.assertEqual((one["found"], one["recall"]), (["doc"], 0.5))
        many = [{"key": f"d{i}", "handles": [f"original://co-{i:06d}"]} for i in range(20)]
        got = retrieval.measure([{"handle": f"original://co-{i:06d}", "title": "x"}
                                 for i in range(20)], many, k=10)
        self.assertEqual(got["recall"], 1.0)

    def test_targets_join_the_registry_pins_catalog_and_record(self):
        rows = [{"id": "co-111111", "name": "Process Map.md", "expected": {"sha256": "a" * 64},
                 "source": {"drive_id": "b!d", "item_id": "I1"}},
                {"id": "co-222222", "name": "Process Map.md", "expected": {"sha256": "a" * 64}},
                {"id": "co-333333", "name": "README.md", "expected": {"sha256": "b" * 64}},
                {"id": "co-444444", "name": "README.md", "expected": {"sha256": "c" * 64}}]
        by_sha, sha_of = retrieval.registry_handles(rows)
        self.assertEqual(by_sha["a" * 64], {f"original://sha256/{'a' * 64}", "original://co-111111",
                                            "original://co-222222", "sharepoint://b!d/I1",
                                            "name:processmapmd"})
        self.assertNotIn("name:readmemd", by_sha["b" * 64])   # a name of two documents
        catalog = retrieval.catalog_documents({"use_cases": [
            {"id": "case", "documents": [{"original": "co-333333"},
                                         {"sharepoint": {"drive_id": "b!d", "item_id": "I9"}}]}]})
        row = {"case": "case", "item": {
            "scoring": {"source_documents": ["a" * 64]},
            "oracle": {"derived_from": ["card:x/1", f"original://sha256/{'a' * 64}", "co-444444"]},
            "root": {"identifier": "PO5447", "identifier_kind": "po_number"}}}
        keys = [t["key"] for t in retrieval.targets(row, by_sha, sha_of, catalog)]
        self.assertEqual(keys, [f"sha256:{'a' * 64}", f"sha256:{'b' * 64}",
                                f"sha256:{'c' * 64}", "sharepoint:sharepoint://b!d/I9",
                                "record:PO5447"])

    def test_evaluate_measures_every_phrasing_and_counts_a_failed_query(self):
        sets, rows = compiled()
        row = next(r for r in rows if r["card_id"] == "PG-05")
        variants = messy.by_card(messy.load())
        registry = json.loads(cards.REGISTRY_PATH.read_text("utf-8"))["rows"]
        by_sha, sha_of = retrieval.registry_handles(registry)
        catalog = retrieval.catalog_documents(
            json.loads(retrieval.CATALOG_PATH.read_text("utf-8")))
        wanted = retrieval.targets(row, by_sha, sha_of, catalog)
        good = next(t for t in wanted if "handles" in t)["handles"][0]
        asked = []

        def search(question, k):
            asked.append(question)
            if "frieght" in question:          # m1 of PG-05
                raise RuntimeError("backend down")
            if len(asked) == 1:
                return {"hits": [{"handle": "graph-mail://n/1"}, {"handle": good}],
                        "routed": [{"handle": good}]}
            return {"hits": [{"handle": "graph-mail://n/1"}]}

        doc = retrieval.evaluate([row], search, sets=sets, variants=variants, by_sha=by_sha,
                                 sha_of=sha_of, catalog=catalog)
        self.assertEqual([q["phrasing"] for q in doc["queries"]], list(retrieval.PHRASINGS))
        first = doc["queries"][0]
        self.assertEqual((first["mrr"], first["hit"], first["routed_hit"]), (0.5, True, True))
        self.assertEqual(first["served"]["mrr"], 1.0)        # routed first
        self.assertEqual(doc["queries"][2]["error"], "RuntimeError")
        self.assertEqual(doc["overall"]["m1"]["errors"], 1)
        self.assertEqual(doc["overall"]["m1"]["scored"], 1)   # counted, never dropped
        self.assertEqual(doc["overall"]["original"]["mrr"], 0.5)
        self.assertIn("PO5447", asked[1])                     # bare names the record

    def test_an_unknown_search_backend_or_tool_fails_closed(self):
        # backends are a fixed registry, selected by name, never a runtime import
        self.assertEqual(sorted(retrieval.BACKENDS), ["hybrid", "tools"])
        with tempfile.TemporaryDirectory() as tmp, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                retrieval.main(["--out", str(Path(tmp) / "o.json"),
                                "--search", "os:system"], environ={})
        with self.assertRaises(retrieval.RetrievalError):
            retrieval.ToolsSearch(object(), "plx.case_open")
        refused = retrieval.ToolsSearch(type("H", (), {"call": lambda self, n, a: {
            "ok": False, "error": "tool_refused"}})())
        self.assertEqual(refused("q", 10), {"hits": [], "error": "tool_refused"})


class GatedBackendTest(unittest.TestCase):
    """A registered backend reads as the tools host's reader: hybrid search runs inside
    that host under its policy and item gate, and no backend runs without that host."""

    class Host:
        def __init__(self, answer):
            self.answer, self.reads = answer, []

        def read(self, name, arguments):
            self.reads.append((name, dict(arguments)))
            return self.answer

    def test_hybrid_runs_inside_the_host_as_its_reader_and_a_refusal_is_an_error(self):
        hits = [{"handle": "original://co-111111", "title": "t", "signals": {"dense": 0.7}}]
        host = self.Host({"ok": True, "hits": hits, "extra": {"notes": []}})
        search = retrieval.BACKENDS["hybrid"](host, "plx.documents")
        got = search("q", 10)
        self.assertEqual(got["hits"], hits)
        self.assertEqual(host.reads, [("plx.search_ranked", {"question": "q", "k": 30})])
        search("q", 50)
        self.assertEqual(host.reads[-1][1]["k"], 100)          # capped at the search's max
        for refused in ({"ok": False, "error": "invalid_search"}, None, {"hits": hits}):
            with self.subTest(refused=refused), self.assertRaises(retrieval.RetrievalError):
                retrieval.BACKENDS["hybrid"](self.Host(refused), "plx.documents")("q", 10)
        # a refused search is counted as an error, never an empty ranking that scores 0
        doc = retrieval.evaluate(
            [{"case": "c", "card_id": "x", "item": {"id": "i", "question": {"text": "q"}}}],
            retrieval.BACKENDS["hybrid"](self.Host(None), "plx.documents"), sets={},
            variants={}, by_sha={}, sha_of={}, catalog={}, kinds=["original"])
        self.assertEqual(doc["queries"][0]["error"], "RetrievalError")

    def test_the_host_side_search_uses_the_readers_call_policy_and_item_gate(self):
        from evals import harness_reads
        from operating.search.core import SearchResult
        calls = []

        class Tools:
            policy = None

            def _item_allowed(self, actor, handle):
                return handle.startswith("api://")

        class Ontology:
            tools = Tools()
            _call_bound = type("B", (), {"search": False})()

            @contextlib.contextmanager
            def request(self):
                yield self

            @contextlib.contextmanager
            def per_call(self):
                calls.append(("searching_inside", None))
                yield None

            def policy_now(self, actor):
                return ("policy-of", actor)

        class Runtime:
            def employee_gate(self, actor):
                return ("gated", actor)

        server = type("S", (), {"case_runtime": Runtime(), "actor": "reader",
                                "ontology": Ontology()})()

        def search(understanding, actor, **kw):
            calls.append((actor, kw["policy"], kw["item_gate"](actor, "api://x"),
                          kw["item_gate"](actor, "sharepoint://d/i"), kw["log"],
                          server.ontology._call_bound.search))
            return SearchResult([{"handle": "api://x", "title": "t", "score": 0.5,
                                  "signals": {"dense": 0.5}}], {}, {"total": 1.0}, ["n"])

        session = harness_reads.Session(server)
        with mock.patch("operating.search.core.search", search), \
                mock.patch("operating.search.models.warm") as warm, \
                mock.patch("operating.query_understanding.understand",
                           side_effect=RuntimeError("no index")):
            got = session.read("plx.search_ranked", {"question": "late orders", "k": 30})
            for bad in ({"question": "q"}, {"question": "", "k": 3}, {"question": "q", "k": 0},
                        {"question": "q", "k": 101}, {"question": "q", "k": True},
                        {"question": "q", "k": 3, "reader": "all"}, {"question": 5, "k": 3}):
                with self.subTest(bad=bad):
                    self.assertEqual(session.read("plx.search_ranked", bad),
                                     {"ok": False, "error": "invalid_search"})
        self.assertTrue(got["ok"], got)
        warm.assert_called_once_with(wait=True)
        gated = ("gated", "reader")
        self.assertEqual(calls[1], (gated, ("policy-of", gated), True, False, False, True))
        self.assertFalse(server.ontology._call_bound.search)   # restored after the call
        self.assertEqual(got["hits"][0]["signals"], {"dense": 0.5, "score": 0.5})
        self.assertEqual(got["extra"]["understanding_failed"], "RuntimeError")
        self.assertEqual(len(calls), 2)                        # invalid reads search nothing

    def test_a_host_that_does_not_start_stops_the_run_and_gets_the_directory_keys(self):
        made = []

        class Live:
            def __init__(self, *args, **kwargs):
                made.append(kwargs)

            def tools(self):
                raise RuntimeError("live_host_start_refused:entra_directory_credentials_unavailable")

            def close(self):
                made.append("closed")

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "host.json"
            config.write_text(json.dumps({"tenant": "plx", "entra_directory": True}), "utf-8")
            environ = {"PLX_WORKBENCH_STATE_DIR": tmp, "MICROSOFT_GRAPH_CLIENT_ID": "id",
                       "MICROSOFT_GRAPH_CLIENT_SECRET": "s", "OTHER_SECRET": "x"}
            with mock.patch("scripts.eval_real_agent.reader_identity",
                            return_value=({"uid": "u"}, None)), \
                    mock.patch("scripts.eval_real_agent.LiveHost", Live):
                with self.assertRaises(retrieval.RetrievalError) as raised:
                    retrieval.live_host(environ, config)
        self.assertIn("host_unavailable:live_host_start_refused", str(raised.exception))
        self.assertEqual(made[0]["extra_env"], {"MICROSOFT_GRAPH_CLIENT_ID": "id",
                                                "MICROSOFT_GRAPH_CLIENT_SECRET": "s"})
        self.assertEqual(made[-1], "closed")

    def test_a_backend_without_the_tools_host_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.redirect_stdout(io.StringIO()):
            code = retrieval.main(["--out", str(Path(tmp) / "o.json"), "--search",
                                   "hybrid", "--limit", "1"],
                                  environ={})
            self.assertEqual(code, 1)
            self.assertIn("host_unavailable", err.getvalue())
            self.assertFalse((Path(tmp) / "o.json").exists())


class LatencyTest(unittest.TestCase):
    def test_percentiles_per_tool_and_answer(self):
        self.assertEqual(messy.percentile([5, 1, 3, 2, 4], 50), 3)
        self.assertEqual(messy.percentile(list(range(1, 11)), 90), 9)
        self.assertIsNone(messy.percentile([], 50))
        with tempfile.TemporaryDirectory() as tmp:
            for i, ms in enumerate((100, 300, 200)):
                path = Path(tmp) / f"dev-F23-000{i}-exposed" / "p0-r0.transcript.json"
                path.parent.mkdir()
                path.write_text(json.dumps({"timing": {"answer_ms": 10 * ms}, "events": [
                    {"type": "tool_call", "name": "plx.documents", "elapsed_ms": ms},
                    {"type": "tool_call", "name": "plx.read", "elapsed_ms": 50},
                    {"type": "answer_check", "name": "x"}]}), "utf-8")
            (Path(tmp) / "broken.transcript.json").write_text("{", "utf-8")
            got = messy.latency([Path(tmp)])
        self.assertEqual(got["transcripts"], 3)
        self.assertEqual(got["by_tool"]["plx.documents"],
                         {"n": 3, "p50": 200, "p90": 300, "max": 300})
        self.assertEqual(got["first_search"]["p50"], 200)
        self.assertEqual(got["answer"]["p90"], 3000)
        self.assertEqual(got["outside_tools"]["p50"], 2000 - 250)
        self.assertEqual(got["calls_per_session"]["p50"], 2)


if __name__ == "__main__":
    unittest.main()
