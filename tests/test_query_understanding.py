"""Query understanding (operating.query_understanding) at its boundary: ``understand``.

A throwaway Postgres database (``plx_test_qu_<pid>``, dropped after) holds a fixture
``search.entities`` built from the dev use-case cards' entities plus synthetic noise
rows; the reader's ReadPolicy decides what is visible. The messy-question table
(``tests/fixtures/query_understanding/cases.json``) is scored for entity precision and
recall, intent accuracy, ambiguity prompts, permission (no candidate the reader may not
see, hidden matches only counted) and fast-path latency. Follow-ups, time frames and the
LLM path (an injected fake: never a real model call) have their own tests.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ontology.types import Actor
from integrations.people_exclusion import PeopleExclusion
from operating import query_understanding as qu
from operating.ontology_tools import ReadPolicy
from operating.pg import migrate, permissions

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/query_understanding"
TODAY = dt.date(2026, 10, 29)
NOISE_ROWS = 3000

def _pg_available() -> bool:
    if not shutil.which("createdb") or not shutil.which("psql"):
        return False
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return subprocess.run(["psql", "-X", "-q", "-d", "postgres", "-c", "select 1"],
                          capture_output=True, check=False).returncode == 0


PG = _pg_available()


def _noise(count: int) -> list[tuple]:
    rnd = random.Random(7)
    syllables = ["ka", "lo", "mi", "ra", "ten", "vor", "bel", "sun", "dra", "pex", "qui",
                 "zor", "nal", "fen", "gri", "hol", "ost", "par", "wel", "yun"]
    tails = ["Cosmetics", "Labs", "Trading", "Supply", "Naturals", "Chemicals", "Brands",
             "Logistics", "Packaging", "Botanicals"]
    scents = ["Lavender", "Citrus", "Unscented", "Oat", "Rose", "Mint", "Coconut", "Vanilla"]
    goods = ["Hand Soap", "Bar Soap", "Body Wash", "Shampoo", "Conditioner", "Body Oil",
             "Lotion", "Bottle", "Pump Cap", "Shipper Box", "Label", "Beauty Balm"]
    sizes = ["250 mL", "500 mL", "30 mL", "1 L", "100 g"]
    types = [("Customer", "bc:customers"), ("Supplier", "api:public.app_vendor"),
             ("Product", "bc:items"), ("Contact", "api:public.app_contact")]
    rows = []
    for i in range(count):
        name = "".join(rnd.choice(syllables) for _ in range(rnd.randint(2, 3))).title()
        kind, aud = types[i % len(types)]
        if kind == "Product" and i % 8 == 2:
            # catalogue names made of everyday words, as real items are
            name = " ".join([rnd.choice(scents), rnd.choice(goods), rnd.choice(sizes)]).title()
        elif kind != "Contact":
            name += " " + rnd.choice(tails)
        rows.append((f"noise-{i}", f"{kind}:noise:{i}", kind, name, [f"NZ{i:05d}"],
                     qu.fold(name), aud, []))
    return rows


class _Fixture:
    """The fixture database, policy, actors and graph, shared by the test classes."""

    def __init__(self) -> None:
        self.data = json.loads((FIXTURE / "entities.json").read_text(encoding="utf-8"))
        self.db = f"plx_test_qu_{os.getpid()}"
        subprocess.run(["dropdb", "--if-exists", self.db], capture_output=True, check=False)
        subprocess.run(["createdb", self.db], check=True, capture_output=True)
        import psycopg
        self.dsn = f"postgresql:///{self.db}"
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            migrate.migrate(conn)          # the schema, indexes and functions
            # the name is what normalized holds; attribute words live only in alias
            rows = [(e[0], e[1], e[2], e[3], e[4], qu.fold(e[3]), e[5], e[6])
                    for e in self.data["entities"]]
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO search.entities (entity_id, object_id, type, name, alias,"
                    " normalized, audience_key, markings) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    rows + _noise(NOISE_ROWS))
                cur.executemany("INSERT INTO search.audience_units (audience_key, sharing,"
                                " label) VALUES (%s,%s,%s)", self.data["units"])
            conn.execute("ANALYZE search.entities")
        self.exclusion = PeopleExclusion(self.data["excluded_people"])
        self.excluded_keys = {f"mail:{oid}" for oid in self.data["excluded_people"]}
        self.by_entity = {e[0]: e for e in self.data["entities"]}
        self.object_of = {e[0]: e[1] for e in self.data["entities"]}
        policy = self.data["policy"]
        self.policy = ReadPolicy(policy["tenant"],
                                 {k: frozenset(v) for k, v in policy["audiences"].items()},
                                 {k: frozenset(v) for k, v in policy["markings"].items()})
        self.actors = {name: Actor(policy["tenant"], frozenset(p))
                       for name, p in self.data["actors"].items()}
        self.index = qu.PgEntityIndex(self.dsn, exclusion=self.exclusion,
                                      scope_cache=permissions.ScopeCache())
        self.graph = DictGraph(self.data["links"])

    def visible(self, actor_name: str, object_id: str) -> bool:
        actor = self.actors[actor_name]
        for e in self.data["entities"]:
            if e[1] == object_id:
                return (e[5] not in self.excluded_keys and self.policy.may_read(actor, e[5])
                        and set(e[6]) <= self.policy.held_markings(actor))
        return object_id.split(":")[1] == "noise" and self.policy.may_read(
            actor, {"Customer": "bc:customers", "Supplier": "api:public.app_vendor",
                    "Product": "bc:items", "Contact": "api:public.app_contact"}[
                object_id.split(":")[0]])

    def ask(self, question: str, actor: str = "staff", **kwargs):
        kwargs.setdefault("llm", False)
        return qu.understand(question, actor=self.actors[actor], policy=self.policy,
                             index=self.index, graph=self.graph, today=TODAY, **kwargs)

    def close(self) -> None:
        self.index.close()
        subprocess.run(["dropdb", "--if-exists", self.db], capture_output=True, check=False)


class DictGraph:
    def __init__(self, links):
        self.links: dict[str, dict[str, str]] = {}
        for a, b, kind in links:
            self.links.setdefault(a, {})[b] = kind
            self.links.setdefault(b, {})[a] = kind

    def neighbors(self, object_ids):
        return {i: dict(self.links.get(i, {})) for i in object_ids}


FX: _Fixture | None = None


def setUpModule():
    global FX
    if PG:
        FX = _Fixture()


def tearDownModule():
    if FX is not None:
        FX.close()


@unittest.skipUnless(PG, "Postgres with pg_trgm and psycopg needed")
class MessyQuestionTableTests(unittest.TestCase):
    """The messy-question table: accuracy, ambiguity, permission and latency."""

    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads((FIXTURE / "cases.json").read_text(encoding="utf-8"))["cases"]
        cls.results = []
        for case in cls.cases:
            cls.results.append((case, FX.ask(case["q"], case.get("actor", "staff"))))
        cls.timed = []
        for _ in range(3):   # repeated runs for latency percentiles
            for case in cls.cases:
                started = time.perf_counter()
                FX.ask(case["q"], case.get("actor", "staff"))
                cls.timed.append((time.perf_counter() - started) * 1000)

    def test_the_table_has_at_least_forty_questions(self):
        self.assertGreaterEqual(len(self.cases), 40)

    def test_entity_resolution_precision_and_recall(self):
        tp = fp = fn = 0
        misses = []
        for case, result in self.results:
            expected = [{FX.object_of[i] for i in group} for group in case["expect"]]
            got = set(result.resolved_ids)
            matched = set()
            for group in expected:
                hit = group & got
                if hit:
                    tp += 1
                    matched |= hit
                else:
                    fn += 1
                    misses.append((case["q"], "missed", sorted(group)))
            extra = got - matched
            fp += len(extra)
            if extra:
                misses.append((case["q"], "extra", sorted(extra)))
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 1.0
        print(f"\nentity resolution: tp={tp} fp={fp} fn={fn} precision={precision:.3f} "
              f"recall={recall:.3f}")
        for miss in misses:
            print("  ", miss)
        self.assertGreaterEqual(precision, 0.95, misses)
        self.assertGreaterEqual(recall, 0.9, misses)

    def test_intent_accuracy(self):
        wrong = [(c["q"], c["intent"], r.intent) for c, r in self.results
                 if r.intent != c["intent"]]
        accuracy = 1 - len(wrong) / len(self.results)
        print(f"\nintent accuracy {accuracy:.3f}; wrong: {wrong}")
        self.assertGreaterEqual(accuracy, 0.85, wrong)

    def test_ambiguous_numbers_and_names_ask_did_you_mean(self):
        for case, result in self.results:
            for choices in case.get("ambiguous", ()):
                ids = {FX.object_of[i] for i in choices}
                mention = next((m for m in result.entities if m.ambiguous
                                and ids <= {c.object_id for c in m.candidates}), None)
                self.assertIsNotNone(mention, (case["q"], result.to_dict()["entities"]))
                self.assertIsNone(mention.resolved)
                self.assertIn("Did you mean", mention.prompt)
                self.assertIn(mention.prompt, result.disambiguation)

    def test_no_candidate_is_an_entity_the_reader_may_not_see(self):
        for case, result in self.results:
            actor = case.get("actor", "staff")
            for mention in result.entities:
                for cand in mention.candidates:
                    self.assertTrue(FX.visible(actor, cand.object_id),
                                    (case["q"], actor, cand.object_id))
            if "hidden_min" in case:
                self.assertGreaterEqual(result.hidden["count"], case["hidden_min"], case["q"])

    def test_hidden_items_are_counted_never_labelled(self):
        hidden_names = [e[3] for e in FX.data["entities"]
                        if not FX.visible("staff", e[1])]
        for case, result in self.results:
            if case.get("actor", "staff") != "staff":
                continue
            text = json.dumps(result.to_dict())
            for name in hidden_names:
                if name.lower() in case["q"].lower():
                    continue        # the reader's own words, echoed
                self.assertNotIn(name, text, case["q"])
            for e in FX.data["entities"]:
                if not FX.visible("staff", e[1]):
                    self.assertNotIn(e[1], text, case["q"])

    def test_fast_path_latency(self):
        ordered = sorted(self.timed)
        p50 = statistics.median(ordered)
        p90 = ordered[int(0.9 * (len(ordered) - 1))]
        print(f"\nfast path over {len(ordered)} runs ({NOISE_ROWS + len(FX.data['entities'])} "
              f"entities): p50={p50:.1f} ms p90={p90:.1f} ms max={ordered[-1]:.1f} ms")
        self.assertLess(p90, 300)

    def test_every_understanding_has_three_to_five_rewrites_with_one_passage(self):
        for case, result in self.results:
            self.assertTrue(3 <= len(result.rewrites) <= 5, (case["q"], result.rewrites))
            self.assertEqual(len(set(r.lower() for r in result.rewrites)), len(result.rewrites))

    def test_a_pasted_wall_of_text_stays_in_budget(self):
        wall = ("please check the soap order and the bottle quote again " * 60) + "po5447"
        started = time.perf_counter()
        result = FX.ask(wall)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertTrue(any("first 600 characters" in n for n in result.language_notes))
        self.assertNotIn(FX.object_of["po-5447"], result.resolved_ids)

    def test_typo_rewrite_carries_the_corrected_name(self):
        result = FX.ask("whats up with the g2 beuty quote")
        self.assertTrue(any("G2 Beauty" in r for r in result.rewrites), result.rewrites)
        result = FX.ask("whats the eta on po 5447")
        self.assertIn("expected arrival", result.rewrites[0])


def score(results):
    """(tp, fp, fn, misses) of resolved ids against the expected groups."""
    tp = fp = fn = 0
    misses = []
    for case, result in results:
        expected = [{FX.object_of[i] for i in group} for group in case["expect"]]
        got = set(result.resolved_ids)
        matched = set()
        for group in expected:
            if group & got:
                tp += 1
                matched |= group & got
            else:
                fn += 1
                misses.append((case["q"], "missed", sorted(group)))
        fp += len(got - matched)
        if got - matched:
            misses.append((case["q"], "extra", sorted(got - matched)))
    return tp, fp, fn, misses


@unittest.skipUnless(PG, "Postgres with pg_trgm and psycopg needed")
class HoldoutTests(unittest.TestCase):
    """Questions written after the resolver, never tuned against: measured and
    reported; asserted only on permissions and a precision floor."""

    def test_holdout(self):
        cases = json.loads((FIXTURE / "holdout.json").read_text(encoding="utf-8"))["cases"]
        results = [(c, FX.ask(c["q"], c.get("actor", "staff"))) for c in cases]
        tp, fp, fn, misses = score(results)
        intents = sum(r.intent == c["intent"] for c, r in results)
        print(f"\nholdout ({len(cases)} questions): tp={tp} fp={fp} fn={fn} "
              f"precision={tp / max(tp + fp, 1):.3f} recall={tp / max(tp + fn, 1):.3f} "
              f"intent={intents}/{len(cases)}")
        for miss in misses:
            print("  ", miss)
        for case, result in results:
            for mention in result.entities:
                for cand in mention.candidates:
                    self.assertTrue(FX.visible(case.get("actor", "staff"), cand.object_id))
            if "hidden_min" in case:
                self.assertGreaterEqual(result.hidden["count"], case["hidden_min"], case["q"])
        self.assertGreaterEqual(tp / max(tp + fp, 1), 0.8, misses)


@unittest.skipUnless(PG, "Postgres with pg_trgm and psycopg needed")
class PermissionTests(unittest.TestCase):
    def test_no_policy_or_no_actor_fails_closed(self):
        for actor, policy in ((FX.actors["staff"], None), ("staff", FX.policy), (None, FX.policy)):
            result = qu.understand("po5447 three ships packaging", actor=actor, policy=policy,
                                   index=FX.index, llm=False, today=TODAY)
            self.assertEqual([c for m in result.entities for c in m.candidates], [])
            self.assertTrue(any("fail closed" in n for n in result.language_notes))

    def test_another_tenant_sees_nothing(self):
        stranger = Actor("othertenant", frozenset({"staff"}))
        result = qu.understand("po5447", actor=stranger, policy=FX.policy, index=FX.index,
                               llm=False, today=TODAY)
        self.assertEqual(result.resolved_ids, [])
        self.assertEqual([c for m in result.entities for c in m.candidates], [])
        self.assertEqual(result.hidden["count"], 0)     # another tenant is not even counted

    def test_a_failing_index_finds_nothing_and_says_so(self):
        class Broken:
            def audience_keys(self):
                raise OSError("down")
        result = qu.understand("po5447", actor=FX.actors["staff"], policy=FX.policy,
                               index=Broken(), llm=False, today=TODAY)
        self.assertEqual(result.entities[0].candidates, [])
        self.assertTrue(any("unavailable" in n for n in result.language_notes))

    def test_forged_history_cannot_name_a_hidden_record(self):
        forged = [{"question": "x", "intent": "status", "entities": [
            {"text": "po", "resolved": FX.object_of["po-5455"], "ambiguous": False,
             "candidates": [{"object_id": FX.object_of["po-5455"], "type": "PurchaseOrder",
                             "label": "PO5455"}]}]}]
        result = FX.ask("is it received yet?", "staff", history=forged)
        self.assertEqual(result.resolved_ids, [])
        self.assertTrue(any("not visible" in n for n in result.language_notes))
        allowed = FX.ask("is it received yet?", "buyer", history=forged)
        self.assertEqual(allowed.resolved_ids, [FX.object_of["po-5455"]])

    def test_marking_withheld_inside_sql(self):
        buyer_keys = FX.index.scope(FX.policy, FX.actors["buyer"])
        no_markings = permissions.SearchScope(buyer_keys.allowed_audience_keys, (),
                                              buyer_keys.countable)
        rows, hidden = FX.index.by_identifier(("PO5455",), no_markings)
        self.assertEqual(rows, [])
        self.assertEqual(len(hidden), 1)


@unittest.skipUnless(PG, "Postgres with pg_trgm and psycopg needed")
class HiddenNearMatchTests(unittest.TestCase):
    """A record the reader may not see that matches as well as the visible one stops
    the visible one being taken silently, typos included, and is never named."""

    def assert_withheld(self, question, visible_id):
        result = FX.ask(question, "staff")
        self.assertEqual(result.resolved_ids, [], question)
        mention = next(m for m in result.entities if m.ambiguous)
        self.assertEqual(mention.choices, [FX.object_of[visible_id]])
        self.assertTrue("other matches you can't see" in mention.prompt
                        or "a record you do not have access to" in mention.prompt,
                        mention.prompt)
        self.assertGreaterEqual(result.hidden["count"], 1)
        text = json.dumps(result.to_dict())
        for e in FX.data["entities"]:
            if not FX.visible("staff", e[1]) and e[3].lower() not in question.lower():
                self.assertNotIn(e[3], text)
                self.assertNotIn(e[1], text)
        return result

    def test_equal_and_typo_matches_are_withheld(self):
        self.assert_withheld("lumiere orders", "cust-lumiere")
        self.assert_withheld("lumere skin", "cust-lumiere")
        self.assert_withheld("lumiere skin", "cust-lumiere")

    def test_a_clearly_better_visible_match_still_resolves(self):
        result = FX.ask("lumiere skincare orders", "staff")
        self.assertEqual(result.resolved_ids, [FX.object_of["cust-lumiere"]])

    def test_a_reader_who_sees_both_is_asked_with_both_named(self):
        sales = Actor("plx", frozenset({"staff", "sales_leads"}))
        result = qu.understand("lumiere orders", actor=sales, policy=FX.policy,
                               index=FX.index, llm=False, today=TODAY)
        self.assertEqual(result.hidden["count"], 0)
        self.assertTrue(result.entities[0].ambiguous)
        self.assertEqual(set(result.entities[0].choices),
                         {FX.object_of["cust-lumiere"], FX.object_of["cust-lumiere-labs"]})
        self.assertIn("Lumiere Skin Labs", result.entities[0].prompt)

    def test_a_bare_number_with_one_visible_match_stays_a_lead(self):
        result = FX.ask("349")
        self.assertEqual(result.resolved_ids, [])
        mention = result.entities[0]
        self.assertEqual(mention.choices, [FX.object_of["po-349"]])
        self.assertIn("not an exact identifier", mention.prompt)
        self.assertEqual(FX.ask("po349").resolved_ids, [FX.object_of["po-349"]])

    def test_a_hidden_same_number_record_makes_a_number_ambiguous_unnamed(self):
        result = FX.ask("5447", "staff")
        self.assertEqual(result.resolved_ids, [])
        mention = result.entities[0]
        self.assertEqual(mention.choices, [FX.object_of["po-5447"]])
        self.assertIn("other matches you can't see", mention.prompt)
        self.assertNotIn("SalesOrder", json.dumps(result.to_dict()))
        # the full id is not the hidden number
        self.assertEqual(FX.ask("po5447", "staff").resolved_ids, [FX.object_of["po-5447"]])


@unittest.skipUnless(PG, "Postgres with pg_trgm and psycopg needed")
class LaneAScopeTests(unittest.TestCase):
    """Every lookup uses the SearchScope: excluded people's units never produce a
    candidate or a count; without the exclusion list, personal units are not searched."""

    def test_an_excluded_persons_unit_is_never_a_candidate_nor_counted(self):
        for question in ("quintessa marlowe", "who is quintessa", "the toronto guy"):
            result = FX.ask(question, "staff")
            text = json.dumps(result.to_dict())
            self.assertNotIn(FX.object_of["contact-excluded"], text, question)
            if "quintessa" in question:
                self.assertEqual(result.hidden["count"], 0, question)
        # a mailbox of someone not excluded, which staff may read, still resolves
        self.assertEqual(FX.ask("pat okonkwo").resolved_ids, [FX.object_of["contact-mailbox"]])

    def test_without_the_exclusion_list_personal_units_fail_closed(self):
        index = qu.PgEntityIndex(
            FX.dsn, exclusion=PeopleExclusion(unavailable="excluded_people_oids_absent"),
            scope_cache=None)
        self.addCleanup(index.close)

        def ask(question):
            return qu.understand(question, actor=FX.actors["staff"], policy=FX.policy,
                                 index=index, llm=False, today=TODAY)
        result = ask("pat okonkwo")
        self.assertEqual(result.resolved_ids, [])
        self.assertEqual(result.hidden["count"], 0)
        self.assertTrue(any("excluded-people list did not load" in n
                            for n in result.language_notes))
        # shared units are still searched
        self.assertEqual(ask("f-26-1179").resolved_ids, [FX.object_of["f-26-1179"]])
        self.assertEqual(ask("po5455").hidden["count"], 0)      # no count is given

    def test_without_the_exclusion_list_only_exact_names_resolve(self):
        index = qu.PgEntityIndex(
            FX.dsn, exclusion=PeopleExclusion(unavailable="excluded_people_oids_absent"),
            scope_cache=None)
        self.addCleanup(index.close)

        def ask(question):
            return qu.understand(question, actor=FX.actors["staff"], policy=FX.policy,
                                 index=index, llm=False, today=TODAY)
        hidden_names = [e[3] for e in FX.data["entities"] if not FX.visible("staff", e[1])]
        for question, visible in (("sahajan holdings", "cust-sahajan"),
                                  ("sahajan holding status", "cust-sahajan"),
                                  ("lumiere skin labs", "cust-lumiere"),
                                  ("sahajn quote", "cust-sahajan")):
            result = ask(question)
            self.assertEqual(result.resolved_ids, [], question)
            mention = next(m for m in result.entities if m.ambiguous)
            self.assertIn(FX.object_of[visible], mention.choices, question)
            self.assertIn("other matches you can't see", mention.prompt, question)
            self.assertEqual(result.hidden["count"], 0, question)      # never counted
            text = json.dumps(result.to_dict())
            for name in hidden_names:
                if name.lower() not in question:
                    self.assertNotIn(name, text, question)
        # exact names and exact ids still resolve
        for question, expected in (("sahajan quote", "cust-sahajan"),
                                   ("lumiere skincare orders", "cust-lumiere"),
                                   ("for & against shipment status", "cust-forandagainst"),
                                   ("po5447 status", "po-5447")):
            self.assertEqual(ask(question).resolved_ids, [FX.object_of[expected]], question)

    def test_attribute_words_match_fuzzily_through_alias_text(self):
        scope = FX.index.scope(FX.policy, FX.actors["staff"])
        rows, _hidden = FX.index.by_terms(["torronto"], scope)
        found = {r.object_id for r in rows}
        self.assertIn(FX.object_of["cust-soapbox"], found)
        self.assertNotIn(FX.object_of["contact-excluded"], found)
        self.assertNotIn("toronto", qu.fold(FX.by_entity["cust-soapbox"][3]))


@unittest.skipUnless(PG, "Postgres with pg_trgm and psycopg needed")
class RankingAtScaleTests(unittest.TestCase):
    """Trigram matches are ranked in SQL: the best one comes back even when tens of
    thousands of names share the searched word."""

    @classmethod
    def setUpClass(cls):
        import psycopg
        with psycopg.connect(FX.dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE search.entities_big (LIKE search.entities)")
            cols = ("(entity_id, object_id, type, name, alias, normalized, audience_key,"
                    " markings)")
            conn.execute(f"INSERT INTO search.entities_big {cols} SELECT 'b' || i, 'Supplier:big:' || i,"
                         " 'Supplier', 'Co ' || i || ' packaging', '{}',"
                         " 'co ' || i || ' packaging', 'api:public.app_vendor', '{}'"
                         " FROM generate_series(1, 60000) i")
            conn.execute(f"INSERT INTO search.entities_big {cols} VALUES ('target', 'Supplier:big:t',"
                         " 'Supplier', 'Swc Packaging', '{}', 'swc packaging',"
                         " 'api:public.app_vendor', '{}')")
            conn.execute("CREATE INDEX ON search.entities_big USING gin (normalized gin_trgm_ops)")
            conn.execute("CREATE INDEX ON search.entities_big USING gin "
                         "(search.alias_text(alias) gin_trgm_ops)")
            conn.execute("ANALYZE search.entities_big")
        cls.index = qu.PgEntityIndex(FX.dsn, table="search.entities_big")

    @classmethod
    def tearDownClass(cls):
        cls.index.close()

    def test_best_match_is_first_among_sixty_thousand(self):
        scope = permissions.SearchScope(("api:public.app_vendor",), ())
        started = time.perf_counter()
        rows, _hidden = self.index.by_terms(["swc packagng"], scope)
        elapsed = (time.perf_counter() - started) * 1000
        self.assertEqual(rows[0].object_id, "Supplier:big:t")
        # the shared word alone is a common word: not ranked, and fast
        started = time.perf_counter()
        rows, _hidden = self.index.by_terms(["packaging"], scope)
        self.assertEqual((rows, self.index.common), ([], {"packaging"}))
        self.assertLess((time.perf_counter() - started) * 1000, 300)
        self.assertEqual([r.sql_score for r in rows], sorted((r.sql_score for r in rows),
                                                            reverse=True))
        self.assertLess(elapsed, 300)


@unittest.skipUnless(PG, "Postgres with pg_trgm and psycopg needed")
class FollowUpTests(unittest.TestCase):
    def test_second_one_picks_from_the_choices_the_prompt_offered(self):
        first = FX.ask("5280")
        mention = first.entities[0]
        self.assertTrue(mention.ambiguous)
        # the PO twins (two systems, one number) are one offered choice
        self.assertEqual(len(mention.choices), 2)
        self.assertGreater(len(mention.candidates), len(mention.choices))
        for word, choice in (("PO", mention.choices[0]), ("sales order", mention.choices[1])):
            self.assertIn(word, mention.prompt)
        self.assertLess(mention.prompt.index("PO"), mention.prompt.index("sales order"))
        second = FX.ask("the second one", history=[first])
        self.assertEqual(second.resolved_ids, [FX.object_of["so-5280"]])
        self.assertEqual(second.resolved_ids, [mention.choices[1]])
        first_one = FX.ask("the first one", history=[first.to_dict()])
        self.assertEqual(first_one.resolved_ids, [mention.choices[0]])
        # a history without recorded choices is asked again, never guessed from candidates
        bare = first.to_dict()
        bare["entities"][0]["choices"] = []
        self.assertEqual(FX.ask("the second one", history=[bare]).resolved_ids, [])

    def test_the_other_one_settles_only_after_one_of_two_was_chosen(self):
        first = FX.ask("5280")
        second = FX.ask("the second one", history=[first])
        other = FX.ask("and what about the other one?", history=[first, second])
        self.assertEqual(other.resolved_ids, [first.entities[0].choices[0]])

    def test_the_other_one_after_an_unresolved_ambiguity_asks_again(self):
        first = FX.ask("5280")
        again = FX.ask("the other one", history=[first])
        self.assertEqual(again.resolved_ids, [])
        mention = again.entities[0]
        self.assertTrue(mention.ambiguous)
        self.assertEqual(mention.choices, first.entities[0].choices)
        self.assertIn("Which one", mention.prompt)

    def test_pronoun_refers_to_the_last_entity_and_inherits_nothing_hidden(self):
        first = FX.ask("po5447 status")
        follow = FX.ask("who's the supplier on it?", history=[first.to_dict()])
        self.assertEqual(follow.resolved_ids, [FX.object_of["po-5447"]])
        self.assertEqual(follow.intent, "who")

    def test_the_second_po(self):
        first = FX.ask("po5447 and po349 late?")
        follow = FX.ask("the second po", history=[first])
        self.assertEqual(follow.resolved_ids, [FX.object_of["po-349"]])
        self.assertEqual(follow.intent, "status")

    def test_and_what_about_inherits_the_intent(self):
        first = FX.ask("how much was the g2 beauty quote")
        follow = FX.ask("and soapbox?", history=[first])
        self.assertEqual(follow.resolved_ids, [FX.object_of["cust-soapbox"]])
        self.assertEqual((follow.intent, follow.intent_source), ("how_much", "conversation"))

    def test_the_other_one_after_a_resolved_pair(self):
        first = FX.ask("compare g2 vs soapbox quotes")
        second = FX.ask("tell me more about soapbox", history=[first])
        other = FX.ask("and the other one", history=[first, second])
        self.assertTrue(set(other.resolved_ids) & {FX.object_of["cust-g2-bc"],
                                                   FX.object_of["cust-g2-portal"]})


@unittest.skipUnless(PG, "Postgres with pg_trgm and psycopg needed")
class GraphTests(unittest.TestCase):
    def test_same_record_twins_are_one_choice(self):
        result = FX.ask("g2 beauty")
        self.assertFalse(result.entities[0].ambiguous)
        self.assertEqual({c.object_id for c in result.entities[0].candidates},
                         {FX.object_of["cust-g2-bc"], FX.object_of["cust-g2-portal"]})

    def test_colliding_names_resolve_to_the_one_linked_to_the_question(self):
        alone = FX.ask("three ships")
        self.assertTrue(alone.entities[0].ambiguous)
        linked = FX.ask("po5447 three ships")
        names = [m for m in linked.entities if m.kind == "name"]
        self.assertEqual(names[0].resolved, FX.object_of["sup-threeships-pack"])
        self.assertIn("linked", names[0].candidates[0].why)

    def test_projection_graph_reads_link_rows_read_only(self):
        path = Path(tempfile.mkdtemp()) / "p.sqlite"
        self.addCleanup(shutil.rmtree, path.parent)
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE link_rows (link_id TEXT, link_type TEXT, "
                         "from_object TEXT, to_object TEXT)")
            conn.execute("INSERT INTO link_rows VALUES ('l','PurchaseOrder.ordered_from','a','b')")
        graph = qu.ProjectionGraph(str(path))
        self.assertEqual(graph.neighbors(["b"]), {"b": {"a": "PurchaseOrder.ordered_from"}})
        with self.assertRaises(sqlite3.OperationalError):
            graph._conn().execute("DELETE FROM link_rows")


@unittest.skipUnless(PG, "Postgres with pg_trgm and psycopg needed")
class LlmPathTests(unittest.TestCase):
    def test_llm_may_expand_the_question_never_introduce_a_subject(self):
        def fake(question, context, timeout):
            return {"intent": "why",
                    "rewrites": ["stuck order soapbox customer", "delayed Freightcom shipment"],
                    "hyde": "The Soapbox order is late because the bottles arrived late.",
                    "references": [{"text": "the usual guy", "type": "Customer",
                                    "search_terms": ["soapbox"]},
                                   {"text": "the soapbox crew", "type": "Customer",
                                    "search_terms": ["soapbox"]},
                                   {"text": "the soap place", "type": "Customer",
                                    "search_terms": ["soap"]}]}
        result = FX.ask("why is the usual guy's thing stuck, and the soap place", llm=fake)
        notes = " | ".join(result.language_notes)
        # "the usual guy" names nobody: the model's "soapbox" is not the reader's word
        self.assertIn('"the usual guy" has no search word from the question', notes)
        # a reference text that is not in the question is dropped
        self.assertIn('"the soapbox crew" is not in the question', notes)
        llm = [m for m in result.entities if m.kind == "llm"]
        self.assertEqual([(m.text, m.resolved) for m in llm],
                         [("the soap place", FX.object_of["cust-soapbox"])])
        # a rewrite or passage naming a subject the question does not is dropped
        self.assertNotIn("delayed Freightcom shipment", result.rewrites)
        self.assertNotIn("The Soapbox order is late because the bottles arrived late.",
                         result.rewrites)
        self.assertTrue(3 <= len(result.rewrites) <= 5)
        self.assertEqual(result.llm["status"], "ok")

    def test_a_lowercase_subject_the_question_lacks_is_dropped(self):
        def fake(question, context, timeout):
            return {"rewrites": ["late order for dandylion", "late lotion order",
                                 "overdue order delayed"],
                    "hyde": "the order for remco is late because stock ran out."}
        result = FX.ask("why is the order late", llm=fake)
        self.assertNotIn("late order for dandylion", result.rewrites)
        self.assertNotIn("the order for remco is late because stock ran out.", result.rewrites)
        # a catalogue word in many names is no subject: the rewrite stays
        self.assertIn("late lotion order", result.rewrites)
        self.assertIn("overdue order delayed", result.rewrites)
        self.assertTrue(any("naming a record" in n for n in result.language_notes))

    def test_grounded_llm_reference_resolves_through_the_index(self):
        def fake(question, context, timeout):
            return {"hyde": "Soapbox Soaps in Canada ordered soap; the order is late.",
                    "references": [{"text": "that candian guy", "type": "Customer",
                                    "search_terms": ["canada", "acme"]}]}
        result = FX.ask("why is the soap order from that candian guy late", llm=fake)
        self.assertIn(FX.object_of["cust-soapbox"], result.resolved_ids)
        # "Soapbox Soaps" is not the reader's word: the passage is dropped
        self.assertNotIn("Soapbox Soaps in Canada ordered soap; the order is late.",
                         result.rewrites)

    def test_llm_cannot_name_a_hidden_entity(self):
        def fake(question, context, timeout):
            return {"references": [{"text": "the chem supplier", "type": "Supplier",
                                    "search_terms": ["thamesurf"]}]}
        result = FX.ask("what about the chem supplier", "staff", llm=fake)
        self.assertNotIn(FX.object_of["sup-thamesurf"],
                         [c.object_id for m in result.entities for c in m.candidates])
        self.assertNotIn("Thamesurf", json.dumps(result.to_dict()))

    def test_a_slow_llm_is_timeboxed_and_the_fast_path_answers(self):
        release = threading.Event()

        def slow(question, context, timeout):
            release.wait(5)
            return {"intent": "why"}
        started = time.perf_counter()
        result = FX.ask("po5447 status", llm=slow, llm_timeout=0.3)
        elapsed = time.perf_counter() - started
        release.set()
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result.llm["status"], "timeout")
        self.assertEqual(result.resolved_ids, [FX.object_of["po-5447"]])
        self.assertEqual(result.intent, "status")

    def test_unusable_llm_replies_are_ignored(self):
        for reply in (None, "text", {"intent": "launch_missiles", "rewrites": "no"}, []):
            result = FX.ask("po5447 status", llm=lambda q, c, t, r=reply: r)
            self.assertEqual(result.intent, "status")
            self.assertTrue(3 <= len(result.rewrites) <= 5)
        result = FX.ask("po5447 status", llm=lambda q, c, t: 1 / 0)
        self.assertEqual(result.llm["status"], "error")

    def test_stream_yields_the_fast_reading_first_then_the_model_reading(self):
        release = threading.Event()

        def slow(question, context, timeout):
            release.wait(5)
            return {"rewrites": ["purchase order 5447 status"], "hyde": "PO5447 is due on Friday this week."}
        stream = qu.understand_stream("po5447 status", actor=FX.actors["staff"],
                                      policy=FX.policy, index=FX.index, llm=slow, today=TODAY)
        started = time.perf_counter()
        fast = next(stream)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual((fast.llm["status"], fast.resolved_ids),
                         ("pending", [FX.object_of["po-5447"]]))
        release.set()
        refined = next(stream)
        self.assertEqual(refined.llm["status"], "ok")
        self.assertIn("PO5447 is due on Friday this week.", refined.rewrites)
        self.assertNotIn("PO5447 is due on Friday this week.", fast.rewrites)
        self.assertEqual(list(stream), [])

    def test_llm_gets_question_and_earlier_questions_only(self):
        seen = {}

        def spy(question, context, timeout):
            seen.update(context=context, question=question)
            return None
        first = FX.ask("po5447 status")
        FX.ask("and three ships?", llm=spy, history=[first])
        self.assertEqual(seen["context"], {"earlier_intents": ["status"],
                                           "earlier_entities": ["PO5447"]})
        # a history naming a record the reader may not see reaches the model as nothing
        forged = [{"question": "Thamesurf secret pricing", "intent": "status", "entities": [
            {"text": "x", "resolved": FX.object_of["sup-thamesurf"], "ambiguous": False,
             "candidates": [], "choices": []}]}]
        FX.ask("and three ships?", "staff", llm=spy, history=forged)
        self.assertEqual(seen["context"], {"earlier_intents": ["status"],
                                           "earlier_entities": []})
        self.assertNotIn("Thamesurf", json.dumps(seen))

    def test_claude_understander_parses_claude_json_and_uses_agent_env(self):
        calls = {}

        def spawn(argv, env, timeout, prompt):
            calls.update(argv=argv, env=env, timeout=timeout, prompt=prompt)
            return json.dumps({"result": 'ok {"intent": "status", "rewrites": ["a", "b"]}'})
        reader = qu.ClaudeUnderstander(model="haiku", spawn=spawn)
        os.environ["ANTHROPIC_API_KEY_TEST_SENTINEL"] = "x"
        try:
            out = reader("q", {}, 4.0)
        finally:
            del os.environ["ANTHROPIC_API_KEY_TEST_SENTINEL"]
        self.assertEqual(out["intent"], "status")
        self.assertIn("--model", calls["argv"])
        self.assertNotIn("ANTHROPIC_API_KEY_TEST_SENTINEL", calls["env"])
        self.assertEqual(calls["timeout"], 4.0)
        self.assertIsNone(qu.ClaudeUnderstander(spawn=lambda *a: "not json")("q", {}, 1))


class TimeFrameAndIntentTests(unittest.TestCase):
    def frame(self, text):
        return qu.parse_time_frame(text, TODAY)

    def test_relative_frames(self):
        self.assertEqual((self.frame("orders last month")["start"],
                          self.frame("orders last month")["end"]), ("2026-09-01", "2026-10-01"))
        since = self.frame("what changed since september")
        self.assertEqual((since["start"], since["end"]), ("2026-09-01", "2026-10-30"))
        self.assertEqual(self.frame("since sept 2025")["start"], "2025-09-01")
        self.assertEqual(self.frame("last 30 days")["start"], "2026-09-29")
        self.assertEqual(self.frame("anything yesterday")["start"], "2026-10-28")
        self.assertEqual(self.frame("this week")["start"], "2026-10-26")
        self.assertEqual(self.frame("q3")["start"], "2026-07-01")
        self.assertEqual(self.frame("in november")["start"], "2025-11-01")
        self.assertEqual(self.frame("before oct 15")["end"], "2026-10-15")
        self.assertEqual(self.frame("since 2026-10-02")["start"], "2026-10-02")
        self.assertEqual(self.frame("the 14 oct agenda")["start"], "2026-10-14")
        self.assertIsNone(self.frame("po5447 status"))
        self.assertIsNone(self.frame("500 kg on 31 feb"))

    def test_intent_rules_tolerate_typos(self):
        cases = {"why is it late": "why", "staus of the order": "status",
                 "who signed off": "who", "how much freight": "how_much",
                 "compare the two quotes": "compare", "find the coa pdf": "find_document",
                 "what's the rule for expired lots": "policy",
                 "when did it arrive": "timeline", "hello": "other",
                 "recieved?": "status"}
        for question, intent in cases.items():
            self.assertEqual(qu.classify_intent(question), intent, question)

    def test_token_match_tolerates_typos_not_lookalikes(self):
        row = qu.EntityRow("e", "o", "Customer", "Lowelost Logistics", "lowelost logistics")
        self.assertEqual(qu.rescore("lost", row)[0], 0.0)      # 4 letters inside 8: no
        self.assertGreater(qu.token_match("candian", "canadian"), 0.8)
        self.assertEqual(qu.token_match("final", "minal"), 0.0)  # first letter differs

    def test_unknown_input_shapes_fail_closed(self):
        result = qu.understand(None, actor=None, index=None, llm=False, today=TODAY)
        self.assertEqual(result.entities, [])
        self.assertEqual(result.intent, "other")
        self.assertTrue(3 <= len(result.rewrites) <= 5)


if __name__ == "__main__":
    unittest.main()
