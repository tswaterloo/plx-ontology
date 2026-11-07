"""The permission filter
inside SQL and the hidden count, OR semantics and typo tolerance, fusion and rerank
order, duplicates once, the item gate, the query log, inputs that fail closed, the
cardinality and latency probes, and ``plx.find`` / ``plx.documents`` under
``PLX_SEARCH=hybrid`` (contract-valid output, a hidden mailbox never shown).

Runs against a throwaway Postgres database (``plx_test_d_<pid>_<n>``), never ``plx``;
skipped when Postgres or psycopg is not available.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import unittest
from pathlib import Path
from unittest import mock

try:
    import psycopg  # noqa: F401
    HAVE_PG = shutil.which("createdb") is not None
except ImportError:  # pragma: no cover
    HAVE_PG = False

from operating.search import SearchReader, search
from operating.search import retrievers as rt
from operating.search.config import WeightsError, load_weights, parse_weights
from operating.search.fusion import rrf
from operating.tests.search_fixture import (BC, EXCLUDED, FINANCE, NOW, PURCHASING,
                                            QUALITY, FixtureDb, HashEmbedder, _t, chunks)

TEAMS = "teams:chat:ops-chat-4"
# Mailbox units are keyed by the owner's directory object id (the people exclusion
# drops any other mail key); the exclusion list names someone else.
MBX_A = "mail:11111111-1111-4111-8111-111111111111"
MBX_B = "mail:22222222-2222-4222-8222-222222222222"
EMB = HashEmbedder()
HIDDEN_WORDS = ("Margin on the Maple Suds", "air freight", "3.10 USD", "msg-9", "msg-11",
                FINANCE, "controller", "Soap order pricing", "surcharge", "zanzibar")


def reader(keys, held=(), gate=lambda handle: True, label="tester"):
    keys = set(keys)
    return SearchReader(label, lambda key: key in keys, frozenset(held), item_gate=gate)


class FixedReranker:
    """Scores passages by a fixed table of words: deterministic rerank order."""

    def __init__(self, table):
        self.table = table
        self.calls = 0

    def score(self, query, passages, timeout):
        self.calls += 1
        out = []
        for passage in passages:
            out.append(max([v for w, v in self.table.items() if w in passage] or [0.0]))
        return out, None


class RerankTimeBoxTests(unittest.TestCase):
    def test_a_timed_out_rerank_stops_after_its_batch_and_frees_the_worker(self):
        import threading
        import time as _time
        from operating.search.models import TimeBoxed

        class Slow:
            def __init__(self):
                self.batches = 0
                self.stopped = threading.Event()

            def score(self, query, passages, should_stop=None):
                out = []
                for start in range(0, len(passages), 2):
                    if should_stop is not None and should_stop():
                        self.stopped.set()
                        raise TimeoutError("cancelled")
                    _time.sleep(0.05)
                    self.batches += 1
                    out += [0.5] * len(passages[start:start + 2])
                return out

        slow = Slow()
        boxed = TimeBoxed(slow)
        scores, problem = boxed.score("q", ["p"] * 40, timeout=0.12)
        self.assertEqual((scores, problem), (None, "reranker_timeout"))
        self.assertTrue(slow.stopped.wait(1.0))
        self.assertLess(slow.batches, 20)
        _time.sleep(0.05)
        scores, problem = boxed.score("q", ["p"] * 2, timeout=1.0)
        self.assertEqual((scores, problem), ([0.5, 0.5], None))

    def test_reranks_run_side_by_side_up_to_the_workers(self):
        import threading
        import time as _time
        from operating.search.models import TimeBoxed

        class Held:
            def score(self, query, passages):
                _time.sleep(0.3)
                return [0.5] * len(passages)

        boxed = TimeBoxed(Held(), workers=2)
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(
            boxed.score("q", ["p"], timeout=5.0))) for _ in range(2)]
        began = _time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(answers, [([0.5], None)] * 2)   # neither refused as busy
        self.assertLess(_time.monotonic() - began, 0.55)
        # every worker held: the next call is refused at once
        held = [threading.Thread(target=boxed.score, args=("q", ["p"], 5.0))
                for _ in range(2)]
        for thread in held:
            thread.start()
        _time.sleep(0.05)
        self.assertEqual(boxed.score("q", ["p"], timeout=5.0), (None, "reranker_busy"))
        for thread in held:
            thread.join(5)

    def test_a_timed_out_rerank_keeps_the_batches_it_finished(self):
        import time as _time
        from operating.search.models import TimeBoxed

        class Partial:
            def score(self, query, passages, should_stop=None, sink=None):
                for start in range(0, len(passages), 2):
                    if should_stop():
                        raise TimeoutError("cancelled")
                    _time.sleep(0.05)
                    sink.extend([0.9 - start / 100] * 2)
                return list(sink)

        scores, problem = TimeBoxed(Partial()).score("q", ["p"] * 40, timeout=0.13)
        self.assertGreaterEqual(len(scores), 2)
        self.assertLess(len(scores), 40)
        self.assertEqual(scores[:2], [0.9, 0.9])            # the best candidates first
        self.assertEqual(problem, f"reranker_partial:{len(scores)}/40")

    def test_warm_loads_the_models_once_so_the_first_query_is_reranked(self):
        from operating.search import models, plug

        class Lazy:
            loads = 0

            def __init__(self):
                self._loaded = False

            @property
            def loaded(self):
                return self._loaded

            def warm(self):
                Lazy.loads += 1
                self._loaded = True

            def score(self, query, passages):
                return [0.9] * len(passages)

        class Emb:
            model, calls = "m", 0

            def embed(self, texts):
                Emb.calls += 1
                return [[1.0] for _ in texts]

        cold = models.TimeBoxed(Lazy())
        self.assertEqual(cold.score("q", ["p"], 1.0), (None, "reranker_loading"))
        boxed = models.TimeBoxed(Lazy())
        with mock.patch.dict(models._DEFAULTS, {"reranker": boxed, "embedder:m": Emb()},
                             clear=True), mock.patch.dict(models._WARMING, clear=True), \
                mock.patch.dict(os.environ, {"PLX_EMBED_MODEL": "m", "PLX_RERANK": "on",
                                             "PLX_SEARCH": "hybrid",
                                             "PLX_SEARCH_MODELS": "local"}):
            plug.warm_if_enabled()                          # a session start: background
            self.assertEqual(models.warm(wait=True, timeout=5),
                             {"embedder": True, "reranker": True})
            models.warm(wait=True)                          # once per process
            self.assertEqual(Emb.calls, 1)
            self.assertTrue(boxed.reranker.loaded)
            self.assertEqual(boxed.score("q", ["p", "p"], 1.0), ([0.9, 0.9], None))
        with mock.patch.dict(models._DEFAULTS, {"embedder:m": Emb()}, clear=True), \
                mock.patch.dict(models._WARMING, clear=True), \
                mock.patch.dict(os.environ, {"PLX_EMBED_MODEL": "m", "PLX_RERANK": "off",
                                             "PLX_SEARCH_MODELS": "local"}):
            self.assertEqual(models.warm(wait=True, timeout=5),
                             {"embedder": True, "reranker": None})
        with mock.patch.dict(models._WARMING, clear=True), \
                mock.patch.dict(os.environ, {"PLX_SEARCH": ""}), \
                mock.patch.object(models, "warm") as never:
            plug.warm_if_enabled()                          # search off: nothing loads
            never.assert_not_called()


    def test_an_onnx_folder_switches_the_reranker_and_scores_in_batches(self):
        import tempfile
        import numpy as np
        from operating.search import models
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "model.onnx").write_bytes(b"")
            with mock.patch.dict(os.environ, {"PLX_RERANK_ONNX": folder,
                                              "PLX_SEARCH_MODELS": "local"}), \
                    mock.patch.dict(models._DEFAULTS, clear=True):
                boxed = models.local_reranker()
            self.assertIsInstance(boxed.reranker, models.OnnxReranker)
        calls = []

        class Session:
            def run(self, _out, feed):
                calls.append(feed["input_ids"].shape[0])
                return [np.zeros((feed["input_ids"].shape[0], 1))]

        def tokenizer(queries, passages, **_kw):
            return {"input_ids": np.ones((len(passages), 3)),
                    "attention_mask": np.ones((len(passages), 3))}
        onnx = models.OnnxReranker("unused")
        onnx._session, onnx._tokenizer = Session(), tokenizer
        self.assertEqual(onnx.score("q", ["p"] * 23), [0.5] * 23)
        self.assertEqual(calls, [10, 10, 3])


class SharedModelTests(unittest.TestCase):
    """One model process per host user (operating/search/model_server.py): every search
    process asks it instead of loading its own copy (about 3.4 GB each), only this user
    reaches it, and a search without it is ranked without the models, saying so."""

    def setUp(self):
        import tempfile
        import threading
        from operating.search import model_server, models
        self.dir = Path(tempfile.mkdtemp()) / "models"
        self.addCleanup(__import__("shutil").rmtree, self.dir.parent, True)

        class Boxed:   # TimeBoxed's interface over a fixed scorer
            reranker = type("R", (), {"loaded": True})()

            def warm(self, wait=False):
                pass

            def score(self, query, passages, timeout):
                return [0.25] * len(passages), None
        for patch in (mock.patch.dict(os.environ, {"PLX_SEARCH_MODEL_DIR": str(self.dir),
                                                   "PLX_SEARCH_MODELS": "shared"}),
                      mock.patch.multiple(models, local_embedder=lambda: EMB,
                                          local_reranker=lambda: Boxed()),
                      mock.patch.dict(models._DEFAULTS, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.server = threading.Thread(target=model_server.serve, args=(self.dir, 1.0),
                                       daemon=True)
        self.server.start()
        for _ in range(200):
            if model_server.running(self.dir) if self.dir.exists() else False:
                break
            __import__("time").sleep(0.01)

    def test_search_processes_share_one_copy_and_bad_requests_are_refused(self):
        from operating.search import model_server, models
        embedder, reranker = models.default_embedder(), models.default_reranker()
        self.assertEqual(embedder.model, EMB.model)
        self.assertEqual(embedder.embed(["gasket order"]), EMB.embed(["gasket order"]))
        self.assertEqual(reranker.score("q", ["a", "b"], 1.0), ([0.25, 0.25], None))
        self.assertEqual(oct(self.dir.stat().st_mode & 0o777), "0o700")
        # A second server on the same directory exits at once: one copy per directory.
        self.assertEqual(model_server.serve(self.dir, 1.0), 0)
        for bad in ({"op": "embed", "texts": "x"}, {"op": "embed", "texts": [1]},
                    {"op": "rerank", "query": "q", "passages": ["a"], "timeout": 999},
                    {"op": "unknown"}):
            self.assertEqual(model_server.request(bad, 5.0, self.dir),
                             {"ok": False, "problem": "bad_request"})
        self.server.join(5.0)   # idle past its bound: it stops and frees the directory
        self.assertFalse(self.server.is_alive())
        self.assertFalse(model_server.running(self.dir))
        self.assertEqual(list((self.dir / "req").iterdir()), [])
        self.assertEqual(list((self.dir / "resp").iterdir()), [])

    def test_a_directory_others_may_open_is_refused(self):
        from operating.search import model_server
        os.chmod(self.dir, 0o755)
        with self.assertRaises(PermissionError):
            model_server.request({"op": "ping"}, 1.0, self.dir)

    def test_without_a_server_search_ranks_without_the_models_and_says_so(self):
        from operating.search import model_server, models
        self.server.join(5.0)
        with mock.patch.object(model_server, "START_SECONDS", 0.5), \
                mock.patch.object(model_server.subprocess, "Popen"):   # it never comes up
            embedder, reranker = models.default_embedder(), models.default_reranker()
            self.assertIsNone(embedder.model)
            scores, problem = reranker.score("q", ["a"], 0.2)
        self.assertIsNone(scores)
        self.assertTrue(problem.startswith("reranker_unavailable"), problem)

    def test_a_server_that_ended_idle_is_started_again_and_never_waited_for_twice(self):
        import threading
        import time
        from operating.search import model_server, models
        embedder = models.default_embedder()
        self.assertEqual(embedder.model, EMB.model)
        self.server.join(5.0)   # it ends, idle
        self.assertFalse(model_server.running(self.dir))
        started = threading.Event()
        with mock.patch.object(model_server.subprocess, "Popen",
                               side_effect=lambda *a, **k: started.set()):
            with self.assertRaises(RuntimeError):   # this query ranks without it
                embedder.embed(["gasket order"])
            self.assertTrue(started.wait(5))         # ... and the server starts again
            began = time.monotonic()
            self.assertIsNone(embedder.model)        # not waited for again meanwhile
            self.assertLess(time.monotonic() - began, 1.0)
        # once it serves again, the next query embeds with it
        self.server = threading.Thread(target=model_server.serve, args=(self.dir, 1.0),
                                       daemon=True)
        self.server.start()
        with mock.patch.object(embedder, "RETRY_SECONDS", 0.0):
            for _ in range(200):
                if model_server.running(self.dir):
                    break
                time.sleep(0.01)
            self.assertEqual(embedder.embed(["gasket order"]), EMB.embed(["gasket order"]))
        self.server.join(5.0)


@unittest.skipUnless(HAVE_PG, "Postgres not available")
class HybridSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ, {"PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED})
        cls.env.start()
        cls.db = FixtureDb().__enter__()
        cls.db.add(chunks())

    @classmethod
    def tearDownClass(cls):
        cls.db.__exit__(None, None, None)
        cls.env.stop()

    def run_search(self, question, who, **kw):
        kw.setdefault("embedder", EMB)
        kw.setdefault("reranker", None)
        kw.setdefault("owners", {})
        return search(question, who, dsn=self.db.dsn, now=NOW, **kw)

    def assert_nothing_hidden_leaks(self, result):
        text = json.dumps(result.to_dict(), default=str)
        for word in HIDDEN_WORDS:
            self.assertNotIn(word, text)

    # ------------------------------------------------------------- permissions

    def test_reader_without_a_key_gets_zero_hits_and_hidden_names_no_count(self):
        result = self.run_search("maple suds", reader([]), owners={BC: "Purchasing team"})
        self.assertEqual(result.hits, [])
        self.assertIn("no_granted_unit", result.notes)
        # Decision 2026-10-29: the names of shared units holding matching hidden rows
        # (the BC company, as the operator named it) and one unnamed private flag (the
        # mailboxes): never a number, a title or a unit key.
        self.assertEqual(result.hidden, {"any": True, "shared_units": ["Purchasing team"],
                                         "private": True})
        text = json.dumps(result.to_dict(), default=str)
        for word in ("Maple", "soap", "graph-mail://", "bc://", "Purchase order 9001",
                     PURCHASING, FINANCE, '"count"'):
            self.assertNotIn(word, text)

    def test_an_excluded_persons_unit_is_never_searched_nor_counted(self):
        everything = reader([PURCHASING, FINANCE, BC, QUALITY, TEAMS, "mail:" + EXCLUDED])
        result = self.run_search("zanzibar", everything, embedder=None)
        self.assertEqual(result.hits, [])
        self.assertEqual(result.hidden, {"any": False, "shared_units": [], "private": False})
        blind = self.run_search("zanzibar", reader([]), embedder=None)
        self.assertFalse(blind.hidden["any"])
        self.assert_nothing_hidden_leaks(blind)

    def test_hidden_count_skipped_when_not_asked(self):
        result = self.run_search("soap order", reader([PURCHASING]), hidden=False)
        self.assertEqual(result.hidden, {"asked": False})
        self.assertNotIn("hidden", result.timings)

    def test_bm25_keeps_every_row_of_a_rare_word_whatever_comes_first_on_disk(self):
        unit = "mail:0e0e0e0e-0000-4000-8000-0000000000c5"
        filler = [{"chunk_id": f"scan{n}#0", "source_handle": f"graph-mail://b/scan{n}@1",
                   "kind": "mail", "header": "Source: mail", "audience_key": unit,
                   "authority": 2, "body": f"The scanner shows box {n} on the dock scanner."}
                  for n in range(12)]
        target = {"chunk_id": "scanT#0", "source_handle": "graph-mail://b/scanT@1",
                  "kind": "mail", "header": "Source: mail", "audience_key": unit,
                  "authority": 2, "body": "Box for PO 5394 is not showing on the scanner."}
        db = FixtureDb().__enter__()
        self.addCleanup(db.__exit__, None, None, None)
        db.add(filler + [target])     # the rows of common words written first
        scope = {"allowed": [unit], "held": [], "kinds": None}
        with psycopg.connect(db.dsn, autocommit=True) as conn, \
                mock.patch.object(rt, "RARE_CAP", 3):     # "scanner" and "box" are common
            ranked = rt.bm25(conn, scope, ["box for PO 5394 not showing on the scanner"], 5)
        self.assertEqual(ranked[0][0], "scanT#0")

    def test_permission_filter_is_inside_every_retrievers_sql(self):
        """Rows the reader may not read (finance mailbox; a purchasing_prices marking
        the reader lacks) never reach Python from any retriever."""
        scope = {"allowed": [PURCHASING, BC, QUALITY, TEAMS], "held": [], "kinds": None}
        forbidden = {"fin1#0", "fin2#0"}
        texts = ["margin landed cost soap order pricing surcharge air freight"]
        with psycopg.connect(self.db.dsn, autocommit=True) as conn:
            lists = {
                "bm25": rt.bm25(conn, scope, texts, 50),
                "dense": rt.dense(conn, scope, EMB.embed(texts), EMB.model, 50),
                "fuzzy": rt.fuzzy(conn, scope, "margn pricng surcharg", 50),
                "exact": rt.exact(conn, scope, ["PurchaseOrder:bc:9001"], ["9001"], 50),
                "graph": rt.graph(conn, scope, [("PurchaseOrder:bc:9001", 1.0)], 50),
                "details": list(rt.details(conn, scope, ["fin1#0", "fin2#0", "m1#0"]).items()),
            }
            # Unfiltered, the same text does match them (so the filter did the work).
            open_scope = dict(scope, allowed=[PURCHASING, BC, QUALITY, TEAMS, FINANCE],
                              held=["purchasing_prices"])
            unfiltered = {cid for cid, _ in rt.bm25(conn, open_scope, texts, 50)}
        self.assertTrue(forbidden <= unfiltered)
        for name, ranked in lists.items():
            self.assertFalse(forbidden & {cid for cid, *_ in ranked}, name)
        self.assertEqual([cid for cid, _ in lists["details"]], ["m1#0"])

    def test_marking_and_audience_hidden_flagged_never_counted(self):
        owners = {FINANCE: "Finance", PURCHASING: "Purchasing"}
        result = self.run_search("soap order", reader([PURCHASING, BC, QUALITY, TEAMS]),
                                 owners=owners)
        shown = {h["chunk_id"] for h in result.hits}
        self.assertFalse({"fin1#0", "fin2#0"} & shown)
        # Both are mail (personal units): the private flag only, no name, no number.
        self.assertEqual(result.hidden, {"any": True, "shared_units": [], "private": True})
        self.assert_nothing_hidden_leaks(result)
        # Holding the marking and the finance mailbox: shown, nothing hidden.
        full = self.run_search("soap order",
                               reader([PURCHASING, BC, QUALITY, TEAMS, FINANCE],
                                      held={"purchasing_prices"}))
        self.assertTrue({"fin1#0", "fin2#0"} <= {h["chunk_id"] for h in full.hits},
                        [h["chunk_id"] for h in full.hits])
        self.assertFalse(full.hidden["any"])

    def test_item_gate_denies_shown_hits_and_flags_them_hidden(self):
        who = reader([PURCHASING, BC, QUALITY, TEAMS],
                     gate=lambda handle: not handle.startswith("graph-mail://"))
        result = self.run_search("liquid soap shipment delayed maple suds", who)
        self.assertTrue(result.hits)
        self.assertFalse([h for h in result.hits if h["handle"].startswith("graph-mail://")])
        self.assertTrue(result.hidden["private"])
        # No item gate at all: an item-ACL source is never shown (fail closed).
        none = SearchReader("x", lambda key: True, frozenset(), item_gate=None)
        result = self.run_search("liquid soap shipment delayed maple suds", none)
        self.assertTrue(all(h["handle"].startswith("bc://") for h in result.hits))

    def test_meeting_hits_pass_the_per_record_check_or_are_never_shown(self):
        """Calendar events, Planner tasks and transcripts carry their own ACL (attendees,
        plan people, participants): a reader granted their units still sees a hit only
        when the per-record check admits it; a check refusing everything, or none at all,
        shows no meeting hit (one list with the tools: ``ontology_tools._ITEM_ACL_SCHEMES``)."""
        units = ["meetings:plan:PlanOne", "meetings:calendar:" + MBX_A.partition(":")[2]]
        meetings = ("calendar-event://", "planner-task://", "meeting-transcript://")
        with FixtureDb() as db:
            db.add([
                dict(chunk_id="task1#0", source_handle="planner-task://PlanOne/T1",
                     object_id="PlannerTask:T1", kind="meetings", title="quokka relabel",
                     occurred_at=_t(2), header="Planner task 'quokka relabel'.",
                     body="Relabel the quokka soap cartons before the audit.",
                     audience_key=units[0], authority=1),
                dict(chunk_id="event1#0",
                     source_handle="calendar-event://" + units[1].rpartition(":")[2] + "/E1",
                     object_id="CalendarEvent:E1", kind="meetings", title="quokka review",
                     occurred_at=_t(2), header="Calendar event 'quokka review'.",
                     body="Review of the quokka soap cartons with the supplier.",
                     audience_key=units[1], authority=1)])

            def hits(gate):
                result = search("quokka soap cartons", reader(units, gate=gate),
                                dsn=db.dsn, now=NOW, embedder=EMB, reranker=None, owners={})
                return [h["handle"] for h in result.hits], result
            shown, _ = hits(lambda handle: True)
            self.assertEqual({h.split("://")[0] + "://" for h in shown},
                             {"calendar-event://", "planner-task://"})
            shown, result = hits(lambda handle: False)
            self.assertFalse([h for h in shown if h.startswith(meetings)])
            self.assertTrue(result.hidden["private"])
            none = SearchReader("x", lambda key: key in units, frozenset(), item_gate=None)
            result = search("quokka soap cartons", none, dsn=db.dsn, now=NOW, embedder=EMB,
                            reranker=None, owners={})
            self.assertFalse([h for h in result.hits if h["handle"].startswith(meetings)])
        from operating.ontology_tools import _ITEM_ACL_SCHEMES
        from operating.search.reader import ITEM_ACL_SCHEMES
        self.assertIs(ITEM_ACL_SCHEMES, _ITEM_ACL_SCHEMES)

    # ------------------------------------------------------------- matching

    def test_or_semantics_finds_a_document_when_not_all_words_match(self):
        who = reader([PURCHASING, BC, QUALITY, TEAMS])
        # No chunk holds "vancouver" or "warehouse": an AND match finds nothing.
        result = self.run_search("castile soap pallets vancouver warehouse", who,
                                 embedder=None)
        top = [h["chunk_id"] for h in result.hits[:3]]
        self.assertTrue({"m1#0", "m2#0"} & set(top), top)
        self.assertIsNotNone(result.hits[0]["signals"]["bm25"])
        self.assertIn("dense_unavailable:no_embedder", result.notes)

    def test_the_question_is_embedded_once_beside_the_lexical_retrievers(self):
        # The lexical lists do not wait for the embedding: BM25 runs while the embedder
        # still works, and the dense list uses the one embedding. An embedder that fails
        # is a note ahead of the retrievers' and no dense list, as before.
        import threading
        who = reader([PURCHASING, BC, QUALITY, TEAMS])
        started, calls = threading.Event(), []
        real_bm25 = rt.bm25

        def bm25(*args, **kwargs):
            started.set()
            return real_bm25(*args, **kwargs)

        class Waiting:
            model = EMB.model

            def embed(self, texts):
                calls.append((list(texts), started.wait(5)))
                return EMB.embed(texts)

        with mock.patch.object(rt, "bm25", bm25):
            result = self.run_search("castile soap pallets", who, embedder=Waiting())
        # embedded once, and BM25 had started before the embedding was back
        self.assertEqual(calls, [(["castile soap pallets"], True)])
        self.assertTrue(any(h["signals"].get("dense") is not None for h in result.hits))
        same = self.run_search("castile soap pallets", who)
        self.assertEqual([h["chunk_id"] for h in result.hits],
                         [h["chunk_id"] for h in same.hits])

        class Failing:
            model = EMB.model

            def embed(self, texts):
                raise RuntimeError("model down")

        failed = self.run_search("castile soap pallets", who, embedder=Failing())
        self.assertEqual(failed.notes[0], "dense_unavailable:RuntimeError")
        self.assertFalse([n for n in failed.notes if n.startswith("dense_failed")])
        self.assertNotIn("dense", failed.timings)
        self.assertTrue(all(h["signals"].get("dense") is None for h in failed.hits))
        alone = self.run_search("castile soap pallets", who, embedder=None)
        self.assertEqual([h["chunk_id"] for h in failed.hits],
                         [h["chunk_id"] for h in alone.hits])

    def test_typos_still_find_the_document(self):
        who = reader([PURCHASING, BC, QUALITY, TEAMS])
        result = self.run_search("lable artwrok aproval", who, embedder=None)
        hit = next(h for h in result.hits if h["chunk_id"] == "sop1#0")
        self.assertIsNotNone(hit["signals"]["fuzzy"])
        self.assertEqual(result.hits[0]["chunk_id"], "sop1#0")

    def test_exact_id_and_graph_from_the_understanding(self):
        who = reader([PURCHASING, BC, QUALITY, TEAMS])
        understanding = {"question": "what's going on with it", "intent": "status",
                         "entities": [{"text": "PO 9001",
                                       "resolved": "PurchaseOrder:bc:9001",
                                       "candidates": []},
                                      {"text": "the label SOP", "resolved": None,
                                       "ambiguous": True,
                                       "candidates": [{"object_id":
                                                       "Document:sharepoint:sop-labels",
                                                       "score": 0.9}]}],
                         "rewrites": []}
        result = self.run_search(understanding, who)
        first = result.hits[0]
        self.assertEqual(first["object_id"], "PurchaseOrder:bc:9001")
        self.assertEqual(first["signals"]["exact_id"], 1.0)
        sop = next(h for h in result.hits if h["chunk_id"] == "sop1#0")
        self.assertIsNotNone(sop["signals"]["graph"])
        self.assertIsNone(sop["signals"]["exact_id"])

    def test_lane_c_understanding_object_is_read(self):
        from operating.query_understanding import Candidate, EntityMention, Understanding
        understanding = Understanding(
            question="where is the maple soap", intent="status",
            entities=[EntityMention("maple", "name",
                                    [Candidate("PurchaseOrder:bc:9001", "PurchaseOrder",
                                               "PO 9001", 0.95, "name")],
                                    "PurchaseOrder:bc:9001", False)],
            time_frame=None, rewrites=["maple suds castile soap shipment"],
            language_notes=[])
        result = self.run_search(understanding, reader([PURCHASING, BC, QUALITY, TEAMS]))
        self.assertEqual(result.hits[0]["object_id"], "PurchaseOrder:bc:9001")
        self.assertEqual(result.hits[0]["signals"]["exact_id"], 1.0)

    def test_retrievers_read_the_question_unless_rewrites_are_asked_for(self):
        import dataclasses
        from unittest import mock
        understanding = {"question": "where is the maple soap", "intent": "status",
                         "entities": [], "rewrites": ["customer USE CODY12 soap"]}
        for mode, expected in (("question", ["where is the maple soap"]),
                               ("rewrites", ["where is the maple soap",
                                             "customer USE CODY12 soap"])):
            seen, embedded = [], []

            class Spy:
                model = EMB.model

                def embed(self, texts):
                    embedded.append(list(texts))
                    return EMB.embed(texts)

            def bm25(conn, scope, texts, limit, _real=rt.bm25):
                seen.append(list(texts))
                return _real(conn, scope, texts, limit)
            weights = dataclasses.replace(load_weights(), query_texts=mode,
                                          dense_ef_search=400)
            with self.subTest(mode=mode), mock.patch.object(rt, "bm25", bm25):
                self.run_search(understanding, reader([PURCHASING]), weights=weights,
                                embedder=Spy())
                self.assertEqual((seen, embedded), ([expected], [expected]))

    def test_a_narrower_query_vector_is_padded_to_the_column(self):
        class Narrow:
            model = EMB.model

            def embed(self, texts):
                return [v[:40] for v in EMB.embed(texts)]
        with psycopg.connect(self.db.dsn, autocommit=True) as conn:
            self.assertEqual(rt.column_dim(conn), 64)
        result = self.run_search("castile soap pallets", reader([PURCHASING]),
                                 embedder=Narrow())
        self.assertNotIn("dense_failed", " ".join(result.notes))
        self.assertTrue(any(h["signals"]["dense"] is not None for h in result.hits))

    # ------------------------------------------------------------- fusion + rerank

    def test_rrf_arithmetic(self):
        lists = {"bm25": [("a", 9.0), ("b", 5.0)], "dense": [("b", 0.9), ("c", 0.5)]}
        scores = rrf(lists, {"bm25": 1.0, "dense": 2.0}, 60)
        self.assertAlmostEqual(scores["a"], 1 / 61)
        self.assertAlmostEqual(scores["b"], 1 / 62 + 2 / 61)
        self.assertAlmostEqual(scores["c"], 2 / 62)
        self.assertEqual(sorted(scores, key=lambda c: -scores[c]), ["b", "c", "a"])

    def test_rerank_reorders_the_fused_top(self):
        who = reader([PURCHASING, BC, QUALITY, TEAMS])
        question = "soap order screen print artwork approval late"
        plain = self.run_search(question, who)
        self.assertIn("rerank_off", plain.notes)
        fake = FixedReranker({"working days": 1.0, "castile": 0.2})
        # k within the reranked top (weights.json rerank_top): every hit is reranked.
        ranked = self.run_search(question, who, reranker=fake, k=load_weights().rerank_top)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(ranked.hits[0]["chunk_id"], "sop1#0")
        self.assertEqual(ranked.hits[0]["signals"]["rerank"], 1.0)
        # Every shown hit was reranked (never a mix of reranked and not).
        self.assertTrue(all(h["signals"]["rerank"] is not None for h in ranked.hits))
        # A k past the reranked top loses nothing: the reranked block first, then the
        # rest by their unreranked score.
        import dataclasses
        narrow = dataclasses.replace(load_weights(), rerank_top=3)
        wide = self.run_search("order", who, reranker=FixedReranker({"Order": 0.5}),
                               weights=narrow, k=12)
        self.assertEqual(len(wide.hits), 12)
        flags = [h["signals"]["rerank"] is not None for h in wide.hits]
        self.assertEqual(flags, [True] * 3 + [False] * 9)
        # Rerank time-out: ranked without it, and the note says so.
        late = mock.Mock()
        late.score.return_value = (None, "reranker_timeout")
        timed = self.run_search(question, who, reranker=late)
        self.assertIn("reranker_timeout", timed.notes)
        self.assertEqual([h["chunk_id"] for h in timed.hits],
                         [h["chunk_id"] for h in plain.hits])
        # Past the box with the best two scored: those two are reranked, the rest follow
        half = mock.Mock()
        half.score.return_value = ([0.0, 0.0], "reranker_partial:2/12")
        part = self.run_search(question, who, reranker=half, weights=narrow, k=12)
        self.assertIn("reranker_partial:2/12", part.notes)
        self.assertEqual([h["signals"]["rerank"] is not None for h in part.hits][:3],
                         [True, True, False])

    def test_authority_and_recency_follow_the_intent_weights(self):
        weights = load_weights()
        self.assertEqual(weights.for_intent("policy").w_recency, 0.0)
        self.assertGreater(weights.for_intent("status").w_recency,
                           weights.for_intent("policy").w_recency)
        self.assertEqual(weights.for_intent("no-such-intent"), weights.intents["default"])
        who = reader([PURCHASING, BC, QUALITY, TEAMS])
        result = self.run_search({"question": "maple suds soap order", "intent": "how_much"},
                                 who)
        bc = next(h for h in result.hits if h["kind"] == "bc_text")
        self.assertEqual(bc["signals"]["authority"], 1.0)
        chat = next(h for h in result.hits if h["kind"] == "teams")
        self.assertEqual(chat["signals"]["authority"], 0.0)

    # ------------------------------------------------------------- duplicates

    def test_duplicates_shown_once_with_where_else(self):
        result = self.run_search("castile soap pallets slipped two weeks",
                                 reader([PURCHASING, BC, QUALITY, TEAMS]))
        dup = [h for h in result.hits if h["dup_group"] == "dup-a"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["also_in"]["count"], 1)
        other = {"graph-mail://purchasing/msg-1", "graph-mail://finance/msg-1-fwd"} - {
            dup[0]["handle"]}
        self.assertEqual(dup[0]["also_in"]["handles"], sorted(other))
        handles = [h["handle"] for h in result.hits]
        self.assertEqual(len(handles), len(set(handles)))

    def test_a_denied_copy_gives_way_to_a_readable_one_and_is_never_named(self):
        original, forward = "graph-mail://purchasing/msg-1", "graph-mail://finance/msg-1-fwd"
        keys = [PURCHASING, BC, QUALITY, TEAMS]
        question = "castile soap pallets slipped two weeks"
        for denied, shown in ((original, forward), (forward, original)):
            result = self.run_search(question, reader(keys, gate=lambda h, d=denied: h != d))
            dup = [h for h in result.hits if h["dup_group"] == "dup-a"]
            self.assertEqual([h["handle"] for h in dup], [shown])
            self.assertEqual(dup[0]["also_in"]["count"], 0)
            self.assertNotIn(denied, json.dumps(result.to_dict(), default=str))
        # every copy denied: the passage is not shown at all
        gone = self.run_search(question, reader(
            keys, gate=lambda h: h not in (original, forward)))
        self.assertFalse([h for h in gone.hits if h["dup_group"] == "dup-a"])
        # both readable: the other copy is listed with where it lives, never its text
        both = self.run_search(question, reader(keys))
        hit = next(h for h in both.hits if h["dup_group"] == "dup-a")
        copy = hit["also_in"]["copies"][0]
        self.assertEqual(set(copy), {"handle", "kind", "location", "occurred_at"})
        self.assertTrue(copy["location"])

    # ------------------------------------------------------------- log + inputs

    def test_a_system_senders_mail_ranks_below_a_persons_with_the_same_text(self):
        # Same text, the system sender's copy newer (recency favours it): the authority
        # prior (weights.json "authority") puts the person's mail first and logs why.
        def mail(cid, domain, days):
            return dict(chunk_id=cid, source_handle=f"graph-mail://purchasing/{cid}",
                        object_id=f"EmailMessage:graph:{cid}", kind="mail",
                        title="Gasket order 4417 delivery", author_role=f"external ({domain})",
                        occurred_at=_t(days), header="Email 'Gasket order 4417 delivery'.",
                        body="The gasket order 4417 ships on Friday from the Hamilton plant.",
                        audience_key=PURCHASING, authority=1)
        with FixtureDb() as db:
            db.add([mail("bot", "github.com", 1), mail("person", "vendor.example", 9)])
            result = search("gasket order delivery Hamilton", reader([PURCHASING]),
                            dsn=db.dsn, now=NOW, embedder=EMB, reranker=None, owners={},
                            log=False)
        self.assertEqual([h["chunk_id"] for h in result.hits], ["person", "bot"])
        self.assertLess(result.hits[1]["signals"]["prior"], 0)
        self.assertEqual(result.hits[0]["signals"]["prior"], 0)

    def test_every_query_is_logged_with_signal_values(self):
        result = self.run_search("maple suds soap", reader([PURCHASING, BC]), log=True)
        self.assertIsNotNone(result.query_log_id)
        with psycopg.connect(self.db.dsn) as conn:
            row = conn.execute("SELECT actor, question, intent, results, timings "
                               "FROM search.query_log WHERE id = %s",
                               (result.query_log_id,)).fetchone()
        self.assertEqual(row[:3], ("tester", "maple suds soap", "other"))
        logged = row[3]["hits"]
        self.assertEqual(len(logged), len(result.hits))
        self.assertEqual(set(logged[0]["signals"]),
                         {"exact_id", "graph", "dense", "bm25", "fuzzy", "authority",
                          "prior", "recency", "rerank", "fused"})
        self.assertIn("total", row[4])

    def test_query_log_rows_past_the_retention_are_pruned(self):
        from operating.search.core import prune_query_log, retention_days
        with psycopg.connect(self.db.dsn, autocommit=True) as conn:
            conn.execute("INSERT INTO search.query_log (at, actor, question) VALUES "
                         "(now() - interval '31 days', 'old', 'q'), (now(), 'new', 'q')")
            self.assertEqual(retention_days(), 30)
            self.assertGreaterEqual(prune_query_log(conn), 1)
            actors = {r[0] for r in conn.execute("SELECT actor FROM search.query_log")}
        self.assertNotIn("old", actors)
        self.assertIn("new", actors)
        with mock.patch.dict(os.environ, {"PLX_SEARCH_LOG_RETENTION_DAYS": "junk"}):
            self.assertEqual(retention_days(), 30)

    def test_unknown_inputs_fail_closed(self):
        who = reader([PURCHASING])
        for bad in (0, 101, True, "5"):
            with self.subTest(k=bad), self.assertRaises(ValueError):
                self.run_search("soap", who, k=bad)
        with self.assertRaises(ValueError):
            self.run_search("soap", who, kinds="mail")
        with self.assertRaises(ValueError):
            search(None, who, dsn=self.db.dsn)
        # An Actor without a policy: nothing searched.
        result = self.run_search("soap", object())
        self.assertEqual((result.hits, result.notes), ([], ["no_read_policy"]))
        # A policy that raises denies the unit.
        def broken(key):
            raise RuntimeError("boom")
        result = self.run_search("soap", SearchReader("x", broken, frozenset()))
        self.assertEqual(result.hits, [])
        # Unknown kinds: nothing of them, never everything.
        self.assertEqual(self.run_search("soap", who, kinds=["no_such_kind"]).hits, [])
        # Weights that do not validate are refused whole.
        with open(os.path.join(os.path.dirname(rt.__file__), "weights.json")) as handle:
            raw = json.load(handle)
        for mutate in (lambda r: r.update(fusion="max"),
                       lambda r: r["intents"]["status"]["retrievers"].pop("dense"),
                       lambda r: r["intents"].update(gossip=r["intents"]["status"]),
                       lambda r: r["intents"]["why"].update(w_rerank=2),
                       lambda r: r.update(extra=1)):
            broken_raw = json.loads(json.dumps(raw))
            mutate(broken_raw)
            with self.assertRaises(WeightsError):
                parse_weights(broken_raw)

    # ------------------------------------------------------------- probes

    def test_cardinality_no_row_lost_or_duplicated(self):
        for n in (1, 10, 60, 100):
            with self.subTest(n=n), FixtureDb() as db:
                db.add([dict(chunk_id=f"c{i}#0", source_handle=f"api://public.notes/{i}",
                             object_id=f"Note:api:{i}", kind="portal_text",
                             title=f"note {i}", header=f"Portal note {i} about gaskets",
                             body=f"gasket batch {i} inspection notes",
                             audience_key="api:public.notes", authority=4)
                        for i in range(n)])
                result = search("gasket inspection", reader(["api:public.notes"]),
                                k=min(n, 100), dsn=db.dsn, embedder=EMB, reranker=None,
                                owners={}, now=NOW)
                ids = [h["chunk_id"] for h in result.hits]
                self.assertEqual(len(ids), n)
                self.assertEqual(len(set(ids)), n)
                self.assertLess(result.timings["total"], 2000)

    def test_one_long_document_never_takes_every_slot_of_a_list(self):
        # A 60-stage map (one source item, its pages too) and five one-chunk documents all
        # match: each retriever keeps at most PER_ITEM chunks of the map, so the other
        # documents still reach fusion and the reader's top k (no row of theirs lost).
        import dataclasses
        import psycopg
        unit = "api:public.notes"
        rows = [dict(chunk_id=f"map#{i}", source_handle="original://co-map" + (
                         f"#page={i}" if i % 2 else ""), object_id="Map:1", kind="map_stage",
                     title="gasket map", header=f"Gasket inspection map stage {i}",
                     body=f"gasket inspection stage {i} notes", audience_key=unit,
                     authority=3) for i in range(60)]
        rows += [dict(chunk_id=f"doc{i}#0", source_handle=f"api://public.notes/{i}",
                      object_id=f"Note:{i}", kind="portal_text", title=f"note {i}",
                      header=f"Portal note {i} gasket inspection", body=f"gasket inspection {i}",
                      audience_key=unit, authority=4) for i in range(5)]
        with FixtureDb() as db:
            db.add(rows)
            scope = {"allowed": [unit], "held": [], "kinds": None}
            with psycopg.connect(db.dsn, autocommit=True) as conn:
                lists = {"bm25": rt.bm25(conn, scope, ["gasket inspection"], 10),
                         "dense": rt.dense(conn, scope, EMB.embed(["gasket inspection"]),
                                           EMB.model, 10),
                         "fuzzy": rt.fuzzy(conn, scope, "gaskett inspectoin", 10)}
                # a typo matching more headers than the pool scores only the pool (bounded)
                with mock.patch.object(rt, "FUZZY_POOL", 3):
                    self.assertLessEqual(len(rt.fuzzy(conn, scope, "gaskett inspectoin", 10)), 3)
            # a retriever that fails (a statement timeout) is left out of a tuning
            # question, as search leaves it out, never the whole run
            from operating.search import tune
            with mock.patch.object(rt, "fuzzy", side_effect=RuntimeError("timeout")):
                (item,) = tune.gather([{"question": "gaskett inspection", "relevant": ["x"]}],
                                      dsn=db.dsn, allowed=[unit], held=(), embedder=EMB,
                                      reranker=None)
            self.assertEqual(item.lists["fuzzy"], [])
            self.assertTrue(item.lists["bm25"])
            weights = dataclasses.replace(load_weights(), candidates_per_retriever=6)
            result = search("gasket inspection", reader([unit]), k=6, dsn=db.dsn,
                            embedder=EMB, reranker=None, owners={}, now=NOW,
                            weights=weights, log=False)
        for name, ranked in lists.items():
            with self.subTest(retriever=name):
                ids = [cid for cid, _ in ranked]
                self.assertTrue(ids)
                self.assertEqual(len(ids), len(set(ids)))
                self.assertLessEqual(sum(cid.startswith("map#") for cid in ids), rt.PER_ITEM)
        self.assertTrue({f"doc{i}#0" for i in range(5)} <= {c for c, _ in lists["bm25"]})
        handles = [h["handle"] for h in result.hits]
        self.assertEqual(len(handles), len(set(handles)))
        self.assertTrue({f"api://public.notes/{i}" for i in range(5)} <= set(handles),
                        handles)

    def test_latency_per_step_p50_p90(self):
        who = reader([PURCHASING, BC, QUALITY, TEAMS])
        questions = ["why is the soap order from that canadian guy late",
                     "who approves label artwork", "maple sudz lable", "PO 9001 status",
                     "weekly order summary boxes"] * 4
        steps: dict[str, list[float]] = {}
        for question in questions:
            result = self.run_search(question, who, log=False)
            for step, ms in result.timings.items():
                steps.setdefault(step, []).append(ms)
        report = {step: (statistics.median(v), sorted(v)[int(0.9 * (len(v) - 1))])
                  for step, v in steps.items()}
        print("\nsearch latency ms p50/p90 (fixture, no models):",
              json.dumps({s: [round(a, 1), round(b, 1)] for s, (a, b) in report.items()}))
        self.assertLess(report["total"][1], 1000)


    def test_tuning_harness_never_lowers_the_metric_and_keeps_small_intents(self):
        from operating.search import tune
        rows = [{"question": "why is the soap order late", "intent": "why",
                 "relevant": ["graph-mail://purchasing/msg-1"]},
                {"question": "who approves label artwork", "intent": "who",
                 "relevant": ["sharepoint://quality/sop-labels.docx"]},
                {"question": "maple suds purchase order status", "intent": "status",
                 "relevant": ["PurchaseOrder:bc:9001"]},
                {"question": "canadian soap arrival", "intent": "status",
                 "relevant": ["teams-chat://ops/chat-4/msg-2"]}]
        items = tune.gather(rows, dsn=self.db.dsn, allowed=[PURCHASING, BC, QUALITY, TEAMS],
                            held=(), embedder=EMB, reranker=None)
        self.assertEqual(len(items), 4)
        report = tune.tune(items, load_weights(), min_per_intent=2, now=NOW)
        overall = report["overall"]
        self.assertGreaterEqual(tune._objective(overall["after"]),
                                tune._objective(overall["before"]))
        self.assertEqual(report["intents"]["why"]["kept"], "too few questions")
        self.assertIn("after", report["intents"]["status"])
        # The tuned weights validate as a weights file.
        with open(os.path.join(os.path.dirname(rt.__file__), "weights.json")) as handle:
            base = json.load(handle)
        parse_weights(tune.weights_json(report["weights"], base, report))
        # An eval row without its known sources is refused, never scored as a miss.
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
            handle.write(json.dumps({"question": "x", "relevant": []}) + "\n")
        self.addCleanup(os.unlink, handle.name)
        with self.assertRaises(ValueError):
            tune.load_eval(handle.name)

    def test_tuning_on_lane_g_targets_as_the_reader_on_one_half_of_the_cards(self):
        from operating.search import tune
        from scripts import eval_tune_search as tune_search
        (item,) = tune.gather([{"question": "why is the soap order late",
                                "relevant": ["doc"]}], dsn=self.db.dsn,
                              allowed=[PURCHASING, BC, QUALITY, TEAMS], held=(),
                              embedder=EMB, reranker=None)
        handles = {row["source_handle"] for row in item.info.values()}
        self.assertIn("bc://prod/company-a/purchaseOrders/9001", handles)
        # One document by its (unversioned) handle, one by a record it
        # names; recall over min(targets, 10), each target counted once.
        wanted = [{"key": "sha256:a", "handles": ["bc://prod/company-a/purchaseOrders/9001"]},
                  {"key": "record:zz404", "text": "zz404"}]
        item.match = tune_search.matcher(wanted)
        item.denominator = min(len(wanted), 10)
        got = tune.metrics([item], load_weights(), lambda _i: load_weights().for_intent(
            "other"), NOW)
        self.assertEqual((got["recall@10"], got["n"]), (0.5, 1))
        self.assertGreater(got["mrr"], 0.0)
        # A candidate the reader's item gate refuses is gone before any weight is tried.
        tune_search.gated(item, lambda handle: not handle.startswith("bc://"))
        self.assertNotIn("bc://prod/company-a/purchaseOrders/9001",
                         {row["source_handle"] for row in item.info.values()})
        self.assertTrue(all(cid in item.info for ranked in item.lists.values()
                            for cid, _ in ranked))
        got = tune.metrics([item], load_weights(), lambda _i: load_weights().for_intent(
            "other"), NOW)
        self.assertEqual((got["recall@10"], got["mrr"]), (0.0, 0.0))
        # The split: every card on exactly one side, each set on both, stable.
        rows = [{"case": c, "item": {"id": f"{c}-{i}"}} for c in ("a", "b") for i in range(5)]
        halves = tune_search.split(rows)
        self.assertEqual(halves, tune_search.split(list(reversed(rows))))
        self.assertEqual(sorted(halves["tune"] + halves["held_out"]),
                         sorted(r["item"]["id"] for r in rows))
        self.assertFalse(set(halves["tune"]) & set(halves["held_out"]))
        for c in ("a", "b"):
            self.assertTrue(any(i.startswith(c) for i in halves["tune"]))
            self.assertTrue(any(i.startswith(c) for i in halves["held_out"]))


@unittest.skipUnless(HAVE_PG, "Postgres not available")
class ToolPathTests(unittest.TestCase):
    """plx.find and plx.documents under PLX_SEARCH=hybrid over the evidence-tools store:
    contract-valid, the hybrid hit gated by the store, a hidden mailbox never shown."""

    @classmethod
    def setUpClass(cls):
        from tests import test_ontology_tools_evidence as fx
        cls.fx = fx
        cls.db = FixtureDb().__enter__()
        cls.db.add([
            dict(chunk_id="m-1#0", source_handle=fx.M1_H, object_id=fx.M1, kind="mail",
                 title=fx.SUBJECT, header=f"Email '{fx.SUBJECT}', mailbox a.",
                 body="The carrier is running late; pallets arrive next week.",
                 audience_key=MBX_A, authority=2),
            dict(chunk_id="m-2#0", source_handle=fx.M2_H, object_id=fx.M2, kind="mail",
                 title="Private thread", header="Email 'Private thread', mailbox b.",
                 body="The carrier is running late for the private pallets.",
                 audience_key=MBX_B, authority=2)])

    @classmethod
    def tearDownClass(cls):
        cls.db.__exit__(None, None, None)

    def setUp(self):
        from operating.search.core import AUDIENCE_KEYS
        AUDIENCE_KEYS.clear()
        patches = [mock.patch.dict(os.environ, {"PLX_SEARCH": "hybrid",
                                                "PLX_PG_DSN": self.db.dsn,
                                                "PLX_RERANK": "off",
                                                "PLX_EXCLUDED_PEOPLE_OIDS": EXCLUDED}),
                   mock.patch("operating.search.core._default_embedder", lambda: EMB)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    M1_TEXT = "Hello team. The carrier is running late; pallets arrive next week."

    def _stored_text(self, store):
        """M1's captured body as the store's text index holds it (``text_fields`` /
        ``text_chunks``), cited to the field it was read from. M2 has none."""
        import zlib
        fx = self.fx
        field_id = store.conn.execute(
            "INSERT INTO text_fields (object_id, version_handle, citation, field_path, "
            "document_kind, sender_class, markings, text_digest) VALUES "
            "(?, ?, ?, 'row.body_text', 'email', 'internal', '[]', ?)",
            (fx.M1, fx.M1_H, fx.M1_H, "0" * 64)).lastrowid
        chunk = store.conn.execute(
            "INSERT INTO text_chunks (field_id, start, body) VALUES (?, 0, ?)",
            (field_id, zlib.compress(self.M1_TEXT.encode()))).lastrowid
        store.conn.execute("INSERT INTO chunk_text (rowid, body) VALUES (?, ?)",
                           (chunk, self.M1_TEXT))

    def tools(self, stored=True):
        """The evidence fixture's tools, its policy also granting OPS the mailbox unit
        the search rows of mbx-a carry (FULL reads every mail unit by ``mail:*``);
        ``stored``: with M1's captured body in the store's text index."""
        from operating.ontology_tools import ReadPolicy
        tools = self.fx.tools(extend=self._stored_text if stored else None)
        audiences = dict(self.fx.POLICY.audiences)
        audiences[MBX_A] = frozenset({"ops", "full"})
        tools.base.policy = ReadPolicy(tenant="plx", audiences=audiences,
                                       markings=dict(self.fx.POLICY.markings))
        return tools

    def test_documents_finds_by_meaning_where_and_matching_found_nothing(self):
        fx = self.fx
        tools = self.tools(stored=False)
        question = "why are the pallets late"
        out = fx.ok(self, "plx.documents",
                    tools.call("plx.documents", {"query": question, "sources": ["graph_mail"]},
                               fx.OPS))
        cited = [d["citation"] for d in out["documents"]]
        self.assertEqual(cited[0], fx.M1_H)
        self.assertNotIn(fx.M2_H, json.dumps(out))
        self.assertNotIn("Private thread", json.dumps(out))
        full = fx.ok(self, "plx.documents",
                     tools.call("plx.documents", {"query": question,
                                                  "sources": ["graph_mail"]}, fx.FULL))
        self.assertEqual({d["citation"] for d in full["documents"]}, {fx.M1_H, fx.M2_H})
        with mock.patch.dict(os.environ, {"PLX_SEARCH": ""}):
            off = fx.ok(self, "plx.documents",
                        tools.call("plx.documents", {"query": question,
                                                     "sources": ["graph_mail"]}, fx.OPS))
        self.assertEqual(off["documents"], [])

    def test_find_quotes_the_stored_version_never_the_query(self):
        fx = self.fx
        tools = self.tools()
        # The review's repro: the words asked are not the document's words.
        question = "why shipment delayed pallets"
        out = tools.base.call("plx.find", {"query": question}, fx.FULL)
        fx.ok(self, "plx.find", out)
        objects = [c["object"]["object_id"] for c in out["candidates"]]
        # M2 matched too, but the store holds no text of it to quote: no candidate.
        self.assertEqual(objects, [fx.M1])
        candidate = out["candidates"][0]
        self.assertEqual((candidate["reason"], candidate["tier"]), ("document_text", "lead"))
        raw = candidate["matched"]["raw"]
        self.assertEqual(raw["origin"], {"citation": fx.M1_H, "field_path": "row.body_text",
                                         "sender_class": "internal"})
        # Every quoted span is a substring of the cited version's text, never the query.
        self.assertIn(raw["untrusted_text"], self.M1_TEXT)
        self.assertNotIn(question, json.dumps(raw))
        ops = tools.base.call("plx.find", {"query": question}, fx.OPS)
        self.assertNotIn(fx.M2_H, json.dumps(ops))
        # The item gate on the store side still decides: a gate that denies drops it.
        tools.base.item_gate = lambda actor, handle: False
        denied = tools.base.call("plx.find", {"query": question}, fx.OPS)
        self.assertNotIn(fx.M1, json.dumps(denied))

    def test_a_cursor_is_bound_to_the_ranking_it_pages(self):
        fx = self.fx
        tools = self.tools()
        args = {"query": "why are the pallets late", "sources": ["graph_mail"], "limit": 1}
        first = fx.ok(self, "plx.documents", tools.call("plx.documents", args, fx.FULL))
        self.assertEqual(first["completeness"]["total"], 2)
        cursor = first["completeness"]["next_cursor"]
        # Search unavailable on page 2: an explicit error, never an empty complete page.
        with mock.patch.dict(os.environ, {"PLX_PG_DSN": "postgresql:///plx_no_such_db_d"}):
            second = tools.call("plx.documents", dict(args, cursor=cursor), fx.FULL)
        self.assertEqual(second["error"]["code"], "invalid_cursor")
        # Search switched off mid-pagination: the same.
        with mock.patch.dict(os.environ, {"PLX_SEARCH": ""}):
            off = tools.call("plx.documents", dict(args, cursor=cursor), fx.FULL)
        self.assertEqual(off["error"]["code"], "invalid_cursor")
        # The same ranking: the next page.
        again = fx.ok(self, "plx.documents",
                      tools.call("plx.documents", dict(args, cursor=cursor), fx.FULL))
        self.assertEqual(len(again["documents"]), 1)
        self.assertNotEqual(again["documents"][0]["citation"],
                            first["documents"][0]["citation"])

    def test_a_next_page_reuses_the_ranking_and_a_revoked_item_stays_hidden(self):
        from operating.search import plug
        fx = self.fx
        tools = self.tools()
        args = {"query": "why are the pallets late", "sources": ["graph_mail"], "limit": 1}
        with mock.patch.object(plug, "_ranked_now", wraps=plug._ranked_now) as made:
            first = fx.ok(self, "plx.documents", tools.call("plx.documents", args, fx.FULL))
            cursor = first["completeness"]["next_cursor"]
            second = fx.ok(self, "plx.documents",
                           tools.call("plx.documents", dict(args, cursor=cursor), fx.FULL))
            self.assertEqual(made.call_count, 1)   # one ranking, both pages
            self.assertEqual({first["documents"][0]["citation"],
                              second["documents"][0]["citation"]}, {fx.M1_H, fx.M2_H})
            # The reused ranking is gated again: an item the reader lost is not shown.
            tools.base.item_gate = lambda actor, handle: not handle.startswith(fx.M1_H)
            after = fx.ok(self, "plx.documents",
                          tools.call("plx.documents", dict(args, limit=5), fx.FULL))
            self.assertEqual(made.call_count, 1)
        self.assertNotIn(fx.M1_H, json.dumps(after))
        self.assertIn(fx.M2_H, [d["citation"] for d in after["documents"]])

    def test_the_item_gate_runs_on_use_best_first_until_the_page_is_full(self):
        # Ranked beside the tool's own search, gated in the tool call: N = 1, 10, 60 hits,
        # every other one denied; the allowed ones come back in order, at most FIND_K,
        # and none denied is ever returned (or digested into the cursor's mode).
        from operating.search import plug
        fx = self.fx
        tools = self.tools()
        for n in (1, 10, 60):
            hits = [{"object_id": f"Email:graph_mail.messages:n{i}",
                     "handle": f"graph-mail://mbx-a/n{i}@ck1"} for i in range(n)]
            allowed = [h for i, h in enumerate(hits) if i % 2 == 1]

            def gate(actor, handle):
                return int(handle.split("/n")[1].split("@")[0]) % 2 == 1
            with mock.patch.object(plug, "_ranked_now",
                                   return_value=plug.Ranked(hits, "hybrid")), \
                    plug.call_state() as state:
                plug.prefetch(tools.base.policy, fx.FULL, gate, f"pallets {n}")
                got = plug.ranked(tools.base.policy, fx.FULL, gate, f"pallets {n}")
            self.assertEqual(got.hits, allowed[:plug.FIND_K], n)
            shown = plug._gated(plug.Ranked(allowed, "hybrid"), tools.base.policy, fx.FULL,
                                lambda a, h: True)
            self.assertEqual(state["mode"], shown.mode, n)

    def test_leads_holding_all_but_one_word_are_listed_beside_what_meaning_found(self):
        import zlib
        fx = self.fx
        text = "Pallets are late again at the dock."

        def stored(store):
            self._stored_text(store)
            field = store.conn.execute(
                "INSERT INTO text_fields (object_id, version_handle, citation, field_path, "
                "document_kind, sender_class, markings, text_digest) VALUES "
                "(?, ?, ?, 'row.content_text', 'attachment', 'internal', '[]', ?)",
                (fx.A1, fx.A1_H, fx.A1_H, "1" * 64)).lastrowid
            chunk = store.conn.execute(
                "INSERT INTO text_chunks (field_id, start, body) VALUES (?, 0, ?)",
                (field, zlib.compress(text.encode()))).lastrowid
            store.conn.execute("INSERT INTO chunk_text (rowid, body) VALUES (?, ?)",
                               (chunk, text))
        tools = self.tools(stored=False)
        stored(tools.store)
        out = fx.ok(self, "plx.documents", tools.call(
            "plx.documents", {"query": "pallets late tomorrow", "sources": ["graph_mail"]},
            fx.FULL))
        listed = [d["citation"] for d in out["documents"]]
        self.assertIn(fx.M1_H, listed)        # meaning (and the words) found it
        self.assertIn(fx.A1_H, listed)        # only the words, all but "tomorrow"
        self.assertIn("partial_word_match", [g["reason"] for g in out["gaps"]])

    def test_a_search_fallback_is_stated_in_the_answer(self):
        fx = self.fx
        tools = self.tools()
        with mock.patch.dict(os.environ, {"PLX_PG_DSN": "postgresql:///plx_no_such_db_d"}):
            docs = fx.ok(self, "plx.documents",
                         tools.call("plx.documents", {"query": "packing list",
                                                      "sources": ["graph_mail"]}, fx.OPS))
            found = fx.ok(self, "plx.find",
                          tools.base.call("plx.find", {"query": "packing list"}, fx.OPS))
        for out in (docs, found):
            self.assertIn("search_fallback", [g["reason"] for g in out["gaps"]])

    def test_graph_expansion_follows_the_readers_visible_links(self):
        from operating.search import plug
        fx = self.fx
        tools = self.tools()
        understanding = {"question": "anything new", "intent": "status",
                         "entities": [{"text": "the PO", "resolved": fx.P1}]}
        def run(actor):
            return search(understanding, actor, policy=tools.base.policy,
                          item_gate=lambda a, h: True, dsn=self.db.dsn, embedder=EMB,
                          reranker=None, owners={},
                          neighbours=plug.neighbours_for(tools, actor))
        ops = run(fx.OPS)
        graph = {h["object_id"]: h["signals"]["graph"] for h in ops.hits}
        self.assertEqual(graph.get(fx.M1), plug.GRAPH_WEIGHT)
        self.assertNotIn(fx.M2, graph)   # linked too, but in a mailbox OPS is not granted
        full = run(fx.FULL)
        self.assertEqual({h["object_id"] for h in full.hits
                          if h["signals"]["graph"] is not None}, {fx.M1, fx.M2})

    def test_search_down_falls_back_to_the_word_index(self):
        fx = self.fx
        # the file's search markings are taken as checked (unreadable, a file is
        # withheld: ``test_payroll_marking``); the word index answers the rest
        with mock.patch.dict(os.environ, {"PLX_PG_DSN": "postgresql:///plx_no_such_db_d"}), \
                mock.patch("operating.search.document_marks.withheld", return_value=set()), \
                mock.patch("operating.search.document_marks.withheld_set",
                           return_value=(frozenset(), frozenset())):
            out = fx.ok(self, "plx.documents",
                        fx.tools().call("plx.documents", {"query": "packing list",
                                                          "sources": ["graph_mail"]}, fx.OPS))
        self.assertEqual([d["citation"] for d in out["documents"]], [fx.A1_H])


if __name__ == "__main__":
    unittest.main()
