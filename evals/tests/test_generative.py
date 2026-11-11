"""The generative evaluation over made-up records: the same seed builds the same set, the
ground truth is the records' own (negations, counts, sums), the people exclusion holds,
styles never touch the named record, and the grader splits correct, incomplete, wrong and
no answer (and refusals of withheld facts)."""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
import json
import random
import re
import unittest

from evals import generative as gen
from evals import graph_walks as gw

AS_OF = date(2026, 11, 5)
SUP = [f'Supplier:portal.fm_supplier:{{"id":{n}}}' for n in (1, 2)]
POS = [f'PurchaseOrder:portal.fm_generated_purchase_order:{{"id":{n}}}' for n in range(1, 9)]
RCP = [f'Receipt:portal.fm_receipt:{{"id":{n}}}' for n in range(1, 4)]
TASKS = [f'MCTask:mission_control.tasks:{{"id":{n}}}' for n in (1, 2)]
EXCLUDED = "kept.out@plx.example"


def _rows(oid: str) -> list[dict]:
    if oid in SUP:
        return [{"property": "name", "value_type": "text",
                 "value": ["Acme Oils", "Bolt Payroll Services"][SUP.index(oid)]}]
    if oid in POS:
        n = POS.index(oid) + 1
        return [{"property": "po_number", "value_type": "identifier", "value": f"PX{n:04d}"},
                {"property": "order_date", "value_type": "date",
                 "value": f"2026-{9 + n % 2:02d}-{n + 2:02d}"},
                {"property": "status", "value_type": "text",
                 "value": "Open" if n % 3 else "Closed"},
                {"property": "total_cost", "value_type": "money",
                 "value": str(100 * n) if n != 7 else "9000.50", "currency": "CAD",
                 "markings": '["finance"]'}]
    if oid in RCP:
        n = RCP.index(oid) + 1
        return [{"property": "receipt_number", "value_type": "identifier", "value": f"RC{n}"},
                {"property": "received_date", "value_type": "date", "value": f"2026-10-0{n}"}]
    if oid in TASKS:
        n = TASKS.index(oid)
        return [{"property": "title", "value_type": "text", "value": f"Fix label run {n}"},
                {"property": "reporter", "value_type": "text",
                 "value": ["jane.doe@plx.example", EXCLUDED][n]}]
    return []


class FakeSource:
    def __init__(self, blocked: frozenset[str] = frozenset()) -> None:
        self.blocked = blocked
        self.links = {"PurchaseOrder.ordered_from":
                      [(p, SUP[0] if i < 6 else SUP[1]) for i, p in enumerate(POS)],
                      "Receipt.receives": [(RCP[0], POS[0]), (RCP[1], POS[1]),
                                           (RCP[2], POS[2])]}
        self.everything = [*SUP, *POS, *RCP, *TASKS]

    def sample(self, link_type):
        return self.links.get(link_type, [])

    def step(self, ids, link_type, direction):
        pairs = self.links.get(link_type, [])
        if direction == "out":
            return [(a, b) for a, b in pairs if a in ids]
        return [(b, a) for a, b in pairs if b in ids]

    def rows(self, ids):
        return {i: _rows(i) for i in ids}

    def versions(self, ids):
        return {i: (f"api://public.x/{i.rsplit(':', 1)[-1]}@v1", "[]") for i in ids}

    def lookup(self, object_type, label):
        return sorted(i for i in self.everything if gw.object_type(i) == object_type
                      and gw.label_of(_rows(i)) and
                      gw.label_of(_rows(i))[0].casefold() == label.casefold())

    def cleared(self, ids):
        return not (set(ids) & self.blocked)

    def count(self, object_type, binding=""):
        return len(self.all_of(object_type, binding))

    def all_of(self, object_type, binding=""):
        return sorted(i for i in self.everything if gw.object_type(i) == object_type
                      and (not binding or gw.binding_of(i) == binding))

    def objects(self, object_type, k, rng, binding=""):
        pool = self.all_of(object_type, binding)
        return [rng.choice(pool) for _ in range(k)] if pool else []

    def lookup_prop(self, object_type, prop, value):
        return sorted(i for i in self.all_of(object_type) for r in _rows(i)
                      if r["property"] == prop and str(r["value"]).casefold() == value.casefold())

    def address_ok(self, address):
        return address != EXCLUDED

    def sensitive(self, texts, title):
        return any("payroll" in t.casefold() for t in texts)


LINKS = [gw.Link("PurchaseOrder.ordered_from", "ordered_from", "purchase_orders",
                 "PurchaseOrder", "Supplier", 8),
         gw.Link("Receipt.receives", "receives", "received_by", "Receipt", "PurchaseOrder", 3)]
TYPES = {"Supplier": {"portal.fm_supplier": 2}, "PurchaseOrder":
         {"portal.fm_generated_purchase_order": 8}, "Receipt": {"portal.fm_receipt": 3},
         "MCTask": {"mission_control.tasks": 2}}


def build(seed=5, source=None, items=120):
    generator = gen.Generator(LINKS, source or FakeSource(), AS_OF, TYPES)
    return gen.build(generator, items=items, seed=seed, dev_share=0.1, generation="g1")


class NumberedSource(FakeSource):
    """POs that also carry an order number: a record with two identifiers."""

    def rows(self, ids):
        return {i: _rows(i) + ([{"property": "order_number", "value_type": "identifier",
                                 "value": f"OR{POS.index(i) + 1:04d}"}] if i in POS else [])
                for i in ids}


class MemberFormsTest(unittest.TestCase):
    def test_a_set_member_is_named_by_any_identifier_of_its_record(self):
        # dev 11-10: the set named a sales invoice by its order number; the answer named it
        # by its invoice number, the same record, and was graded as missing it
        generator = gen.Generator(LINKS, NumberedSource(), AS_OF, TYPES)
        self.assertEqual(generator.forms(POS[0], "PX0001"), ["PX0001", "OR0001"])
        # a free-text name or a date is never another form
        self.assertEqual(generator.forms(SUP[0], "Acme Oils"), ["Acme Oils"])
        top = {"id": "top", "kind": "set", "members": [generator.forms(POS[0], "PX0001")],
               "records": ["h1"], "allowed": ["api"]}
        self.assertEqual(gen.grade(item([top]), ans("Order OR0001 has the most."), OWNER)[
            "outcome"], "correct")
        self.assertNotEqual(gen.grade(item([top]), ans("Order OR0002 has the most."), OWNER)[
            "outcome"], "correct")


class GenerativeBuildTest(unittest.TestCase):
    def test_the_same_seed_builds_the_same_set_and_another_seed_another(self):
        first, stats = build(seed=5)
        again, _ = build(seed=5)
        self.assertEqual([gen.dumps(i) for i in first], [gen.dumps(i) for i in again])
        other, _ = build(seed=6)
        self.assertNotEqual([i["question"] for i in first], [i["question"] for i in other])
        self.assertGreater(len(first), 20)
        self.assertEqual(stats["dev"] + stats["held_out"], len(first))
        self.assertEqual({i["split"] for i in first}, {"dev", "held_out"})
        self.assertTrue(all(i["id"].startswith(("dev-G-", "ho-G-")) for i in first))
        self.assertTrue(set(i["category"] for i in first) <= set(gen.CATEGORIES))

    def test_ground_truth_is_the_records_own(self):
        items, _ = build(seed=5, items=400)
        received = {POS[0], POS[1], POS[2]}
        no_receipt = {_rows(p)[0]["value"] for p in POS[:6] if p not in received}
        checked = Counter()
        for item in items:
            facts = {f["id"]: f for f in item["facts"]}
            q = item["question"]
            if item["category"] == "negation" and "Acme Oils" in q and "members" in facts:
                self.assertEqual({m[0] for m in facts["members"]["members"]}, no_receipt)
                checked["negation"] += 1
            if item["category"] == "aggregation" and item["rule"].startswith("count where status"):
                want = sum(1 for p in POS if _rows(p)[2]["value"] in q)
                self.assertEqual(facts["count"]["value"], str(want))
                checked["aggregation"] += 1
            if item["category"] == "money" and item["rule"] == "sum of total_cost":
                self.assertEqual(facts["total"]["markings"], ["finance"])
                self.assertRegex(facts["total"]["value"], r"^\d+\.\d\d$")
                checked["money"] += 1
            if item["category"] == "anomaly":
                self.assertEqual(facts["value"]["value"], "9000.5")
                checked["anomaly"] += 1
        self.assertTrue(checked["negation"] and checked["aggregation"] and checked["money"],
                        checked)

    def test_people_exclusion_and_blocked_records_fail_closed(self):
        items, _ = build(seed=5, items=400)
        people = [i for i in items if i["category"] == "people"]
        self.assertTrue(people)
        self.assertFalse([i for i in items if EXCLUDED in json.dumps(i)])
        blocked, _ = build(seed=5, items=400, source=FakeSource(blocked=frozenset({SUP[0]})))
        self.assertFalse([i for i in blocked if "Acme Oils" in json.dumps(i)])

    def test_payroll_content_is_marked_for_a_reader_who_may_see_it(self):
        items, _ = build(seed=5, items=400)
        named = [i for i in items if "Bolt Payroll Services" in json.dumps(i)]
        self.assertTrue(named)
        for item in named:
            self.assertTrue(item["sensitive"])
            self.assertTrue({"finance", "pii"} <= set(item["markings"]))

    def test_styles_never_alter_the_named_record(self):
        q = "What is the total cost of purchase order PX0007 in FileMaker?"
        for style in gen.STYLES:
            for n in range(5):
                out = gen.styled(q, style, "PX0007", f"s{n}")
                self.assertIn("PX0007", out, style)
        self.assertNotEqual(gen.styled(q, "vince", "PX0007", "a"), q)


class ChangedSource(FakeSource):
    """The made-up store later: ``changed`` records hold other rows (a new version), and
    unnamed tasks were added ahead of the others (every later pick by offset shifts)."""

    def __init__(self, changed, added=0, **kwargs):
        super().__init__(**kwargs)
        self.changed = changed
        self.everything = [f'MCTask:mission_control.tasks:{{"id":0{n}}}'
                           for n in range(added)] + self.everything

    def rows(self, ids):
        return {i: self.changed.get(i, _rows(i)) for i in ids}

    def versions(self, ids):
        got = super().versions(ids)
        return {i: (h.replace("@v1", "@v2"), m) if i in self.changed else (h, m)
                for i, (h, m) in got.items()}


class TruthAgainTest(unittest.TestCase):
    """A set's truth composed again over the store as it is later (``gen.Pinned`` and
    ``scripts.eval_generative.replay_one``): the same questions over the same picked
    records; unchanged records give the same truth, changed ones their current truth."""

    def test_the_same_question_over_the_same_records_reads_the_records_now(self):
        from scripts import eval_generative as eg
        noted = {}
        first = gen.Generator(LINKS, gen.Pinned(FakeSource()), AS_OF, TYPES)
        items, _ = gen.build(first, items=400, seed=5, dev_share=0.1, generation="g1",
                             trace=lambda i, q, pins: noted.__setitem__(
                                 i["id"], eg._recipe(i["id"], q, pins, *q.origin)))
        self.assertEqual(items, build(seed=5, items=400)[0])   # noting picks changes nothing
        self.assertEqual(set(noted), {i["id"] for i in items})
        self.assertNotIn("question", json.dumps(list(noted.values())))   # digests, no text
        again = gen.Generator(LINKS, gen.Pinned(FakeSource()), AS_OF, TYPES)
        self.assertEqual({eg.replay_one(again, 5, noted[i["id"]])["status"] for i in items},
                         {"same"})

        po7 = [dict(r, value="120.00") if r["property"] == "total_cost" else r
               for r in _rows(POS[6])]
        po1 = [dict(r, value="Closed") if r["property"] == "status" else r
               for r in _rows(POS[0])]
        later = ChangedSource({POS[6]: po7, POS[0]: po1}, added=3)
        now = gen.Generator(LINKS, gen.Pinned(later), AS_OF, TYPES)
        moved = {f"api://public.x/{p.rsplit(':', 1)[-1]}@{v}" for p in (POS[0], POS[6])
                 for v in ("v1", "v2")}
        status, sums, looked = Counter(), 0, 0
        for item in items:
            got = eg.replay_one(now, 5, noted[item["id"]])
            status[got["status"].split(":")[0]] += 1
            cited = set(item["citations"]) | set(got.get("truth", {}).get("citations", ()))
            # a question neither citing a changed record nor asking by a changed property
            # (a status, an amount: what changed) is the same question with the same truth
            if not cited & moved and not re.search(r"status|total cost|cost|open|closed",
                                                   item["question"], re.I) and \
                    item["category"] not in ("aggregation", "money", "anomaly", "ranking"):
                self.assertEqual(got["status"], "same", item["question"])
            if got["status"] == "same":
                self.assertEqual(got["truth"]["facts"], item["facts"])
            if item["category"] == "money" and item["rule"] == "sum of total_cost" and \
                    got["status"] == "changed" and POS[6].rsplit(":", 1)[-1] in str(cited):
                fact = got["truth"]["facts"][0]   # the cited records' amounts as they are now
                scope = [p for p in POS for h in fact["records"]
                         if h.startswith(f"api://public.x/{p.rsplit(':', 1)[-1]}@")]
                self.assertIn(POS[6], scope)
                self.assertEqual(Decimal(fact["value"]), sum(
                    Decimal(r["value"]) for p in scope for r in later.rows([p])[p]
                    if r["property"] == "total_cost"))
                self.assertNotEqual(fact["value"], item["facts"][0]["value"])
                sums += 1
            if item["rule"] == "current value of status" and \
                    item["citations"] == [f"api://public.x/{POS[0].rsplit(':', 1)[-1]}@v1"]:
                self.assertEqual(got["status"], "changed")   # the same question, read now
                self.assertEqual(got["truth"]["facts"][0]["members"], [["Closed"]])
                looked += 1
            if item["category"] == "anomaly":   # the outlier is gone: no longer asked
                self.assertEqual(got["status"], "dropped:no_outlier")
        self.assertTrue(status["same"] and status["changed"] and sums and looked, status)
        people = [i for i in items if i["category"] == "people"]
        self.assertTrue(people)   # picks by offset shifted by the added tasks; pinned, not
        for item in people:
            self.assertEqual(eg.replay_one(now, 5, noted[item["id"]])["status"], "same")

    def test_a_kept_set_is_noted_by_replaying_its_build(self):
        from concurrent.futures import ThreadPoolExecutor
        from scripts import eval_generative as eg
        kept, _ = build(seed=5, items=400)
        kept[3] = dict(kept[3], facts=[dict(kept[3]["facts"][0], hand_fixed=True)])
        kept[5] = dict(kept[5], question=kept[5]["question"] + " (reworded)")
        pinned = gen.Generator(LINKS, gen.Pinned(FakeSource()), AS_OF, TYPES)
        found, counts = eg.note_set(pinned, kept, items=400, seed=5, dev_items=40,
                                    generation="g1")
        self.assertEqual(set(found), {i["id"] for i in kept})
        self.assertEqual((counts["reproduced_truth_edited_since"], counts["matched_by_truth"],
                          counts["not_found"]), (1, 1, 0), counts)
        again = gen.Generator(LINKS, gen.Pinned(FakeSource()), AS_OF, TYPES)
        self.assertEqual({eg.replay_one(again, 5, r)["status"] for r in found.values()},
                         {"same"})
        # composed ahead by a pool (one worker here; processes in a real run): the same
        eg._WORKER.update(generator=gen.Generator(LINKS, gen.Pinned(FakeSource()), AS_OF,
                                                  TYPES), seed=5)
        self.addCleanup(eg._WORKER.clear)
        with ThreadPoolExecutor(max_workers=1) as pool:
            ahead, _ = eg.note_set(pinned, kept, items=400, seed=5, dev_items=40,
                                   generation="g1", turns=eg.pool_turns(pool, 7))
        self.assertEqual(ahead, found)

    def test_recipes_by_id_read_dev_items_only(self):
        import argparse
        from pathlib import Path
        from scripts import eval_generative as eg
        args = argparse.Namespace(ids="dev-G-0001,ho-G-0001", root=Path("/nonexistent"))
        with self.assertRaises(eg.Refused) as refused:
            eg.recipes(args, {})
        self.assertEqual(str(refused.exception), "ids_mode_reads_dev_items_only")


def item(facts, markings=()):
    return {"id": "dev-G-0001", "category": "negation", "difficulty": "easy",
            "style": "plain", "question": "Which POs of Acme Oils have no receipt?",
            "facts": facts}


SET = {"kind": "set", "id": "members", "members": [["PX0004"], ["PX0005"], ["PX0006"]],
       "records": ["api://x"], "allowed": ["api"]}
COUNT = {"kind": "number", "id": "count", "value": "3", "label": ["purchase orders"],
         "records": ["api://x"], "allowed": ["api"]}
MONEY = {"kind": "number", "id": "total", "value": "2100.00", "label": ["total cost"],
         "unit": "CAD", "records": ["api://x"], "allowed": ["api"], "markings": ["finance"]}
NONE = {"kind": "absent", "id": "none", "value": "none", "accept": ["none", "no "]}
OWNER = ["finance", "purchasing_prices", "quality_controlled", "pii"]


def ans(text, outcome="answered"):
    return {"direct_answer": text, "outcome": outcome, "facts": []}


class RunnerGuardTest(unittest.TestCase):
    def test_a_reader_the_exclusion_cannot_decide_is_refused(self):
        from scripts import eval_generative as eg
        for email in ("", "reader@plx.example"):
            with self.assertRaises(eg.Refused):
                eg.reader_cleared(email, {"PLX_EXCLUDED_PEOPLE_FILE": "/nonexistent"}, None)

    def test_the_set_is_never_written_inside_a_git_work_tree(self):
        from pathlib import Path
        from scripts import eval_generative as eg
        with self.assertRaises(eg.Refused):
            eg._outside_git(Path(__file__).resolve().parent)



class InProcessRunTest(unittest.TestCase):
    """``run`` asks the app's own Ask in this process (no HTTP) over the console's fixture
    world: the answer the route would stream is kept and graded, a run resumes by name,
    and a signed-in identity other than the cleared reader, or flags that are not
    ``--dev``, are refused before any question."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        from pathlib import Path
        from operating import proactive_engine
        from operator_api.workspace import case_console
        from operator_api.workspace import object_workspace as objects
        from operator_api.tests import case_fixtures
        from operator_api.tests import test_backend_fixture as tbf
        cls.fixtures, cls.po = case_fixtures, case_fixtures.PO_NUMBER
        cls.planner = staticmethod(tbf.planner)   # a plain function, not a method
        cls.tmp = Path(tempfile.mkdtemp(prefix="eval-gen-run-"))
        state = cls.tmp / "state"
        case_fixtures.seed(state, "plx")
        store = state / case_console.ONTOLOGY_FILE
        objects.project_mirror(state, store)
        proactive_engine.after_build(store)
        cls.api = (f"--dev --insecure-cookie --state-dir {state} --ask --pg-dsn '' "
                   f"--app-data-dir {cls.tmp / 'app'} --console-arg=--source-mirror-audience "
                   f"--console-arg={state / case_fixtures.MIRROR_AUDIENCE_FILE} "
                   + " ".join(f"--dev-fixture-principal {p}" for p in tbf.OPS))
        cls.root = cls.tmp / "set"
        (cls.root / "dev").mkdir(parents=True)
        rows = [{**item([{"kind": "set", "id": "po", "members": [[cls.po]],
                          "records": ["api://x"], "allowed": ["api"]}]),
                 "id": "dev-G-0001", "question": f"is {cls.po} late?"}]
        (cls.root / "dev" / "items.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def args(self, name, email="", api=None):
        import argparse
        return argparse.Namespace(root=self.root, set="dev", name=name, state=None,
                                  reader_email=email, reader_env="", ids="", limit=0,
                                  mint_cmd="", access_link="/nonexistent",
                                  api_args=self.api if api is None else api,
                                  concurrency=2, min_available_gb=0,
                                  reader_markings=",".join(OWNER))

    def run_eval(self, args):
        import contextlib
        import io
        from unittest import mock
        from scripts import eval_generative as eg
        with mock.patch.object(eg, "reader_cleared"), \
                contextlib.redirect_stderr(io.StringIO()):
            return eg.run(args, {}, fixtures=self.fixtures, planner=self.planner)

    def test_the_apps_own_ask_answers_in_process_and_a_run_resumes(self):
        report = self.run_eval(self.args("r1"))
        self.assertEqual(report["run"]["asked"], 1)
        self.assertEqual(report["run"]["statuses"], {"ok": 1})
        kept = json.loads((self.root / "runs" / "r1" / "answers" / "dev-G-0001.json")
                          .read_text())
        from operator_api import models as m
        m.AnswerPayload.model_validate(kept)          # the route's own answer event
        self.assertIn(kept["outcome"], ("answered", "partial"))
        cited = [f for f in kept["facts"] if self.po in f["text"]]      # the record, cited
        self.assertEqual(cited[0]["sources"][0]["system"], "Portal", kept["facts"])
        self.assertEqual(sum(report["outcomes"].values()), 1)           # graded once
        again = self.run_eval(self.args("r1"))       # resumes by name: nothing asked
        self.assertEqual((again["run"]["asked"], again["run"]["skipped_done"]), (1 - 1, 1))
        log = (self.root / "runs" / "r1" / "log.jsonl").read_text().splitlines()
        self.assertEqual(len(log), 1)
        self.assertNotIn("late", log[0])              # ids and outcomes only, no question

    def test_another_signed_in_identity_or_non_dev_flags_are_refused(self):
        from scripts import eval_generative as eg
        with self.assertRaises(eg.Refused) as refused:
            self.run_eval(self.args("r2", email="someone.else@plx.example"))
        self.assertEqual(str(refused.exception), "signed_in_reader_is_not_the_cleared_reader")
        self.assertFalse(list((self.root / "runs" / "r2" / "answers").glob("*")))
        for api in ("--mock", f"--entra-resource /nonexistent --state-dir {self.tmp}"):
            with self.assertRaises(eg.Refused):
                self.run_eval(self.args("r3", api=api))

    def test_the_eval_app_never_uses_the_live_servers_app_data(self):
        from scripts import eval_generative as eg
        from operator_api.serving import DEFAULT_DATA_DIR
        bare = self.api.replace(f"--app-data-dir {self.tmp / 'app'}", "")
        for api, code in ((bare, "api_args_need_own_app_data_dir"),
                          (f"{bare} --app-data-dir {DEFAULT_DATA_DIR}",
                           "api_args_app_data_dir_is_the_live_servers")):
            with self.assertRaises(eg.Refused) as refused:
                self.run_eval(self.args("r4", api=api))
            self.assertEqual(str(refused.exception), code)
        self.assertFalse(list((self.root / "runs" / "r4" / "answers").glob("*")))


class GraderTest(unittest.TestCase):
    def outcome(self, facts, answer, markings=OWNER):
        return gen.grade(item(facts), answer, markings)["outcome"]

    def test_a_day_written_as_the_source_records_it_compactly_is_that_day(self):
        # graph dev 11-10 GW-0065: "OR1000131263 ... with order date 20230906 in Sage"
        day = {"id": "latest", "kind": "date", "value": "2023-09-06"}
        for said in ("Sales order OR1000131263 has order date 20230906.",
                     "It was ordered on 2023-09-06."):
            with self.subTest(said=said):
                self.assertEqual(self.outcome([day], ans(said)), "correct")
        for said in ("Sales order OR1000131263 has order date 20230907.",
                     "It is sales order OR20230906.", "Its number is 202309061."):
            with self.subTest(said=said):
                self.assertNotEqual(self.outcome([day], ans(said)), "correct")

    def test_correct_incomplete_wrong_and_no_answer(self):
        self.assertEqual(self.outcome([SET], ans("PX0004, PX0005 and PX0006 have none.")),
                         "correct")
        self.assertEqual(self.outcome([SET], ans("Only PX0004 lacks a receipt.")),
                         "incomplete")
        self.assertEqual(self.outcome([SET], ans("PX0001 and PX0002 have no receipt.")),
                         "wrong")
        self.assertEqual(self.outcome([SET], None), "no_answer")
        self.assertEqual(self.outcome([SET], ans("", outcome="not_found")), "no_answer")
        self.assertEqual(self.outcome([COUNT], ans("There are 3 purchase orders.")), "correct")
        self.assertEqual(self.outcome([COUNT], ans("There are 4 purchase orders.")), "wrong")

    def test_money_matches_to_the_cent_and_none_is_graded(self):
        self.assertEqual(self.outcome([MONEY], ans("The total cost is $2,100.00 CAD.")),
                         "correct")
        self.assertEqual(self.outcome([MONEY], ans("The total cost is $2,100.40.")), "wrong")
        self.assertEqual(self.outcome([NONE], ans("None: every PO has a receipt.")),
                         "correct")
        self.assertEqual(self.outcome([NONE], ans("PX0004 is missing its receipt.")), "wrong")

    def test_a_text_fact_is_graded_by_its_value(self):
        # walk "which record is the latest" facts are text; they once always graded missing
        latest = {"id": "latest_record", "kind": "text", "value": "PX0005",
                  "records": ["h5"], "allowed": ["api"]}
        self.assertEqual(self.outcome([latest], ans("The latest is PX0005.")), "correct")
        self.assertEqual(self.outcome([latest], ans("The latest is PX0004.")), "wrong")

    def test_a_small_count_written_out_before_its_label_is_its_figure(self):
        # dev 11-10: "One unit of measure, CSE, ..." graded wrong on the "2" of a line
        uom = {"id": "count", "kind": "number", "value": "1", "records": ["h1"],
               "allowed": ["api"], "label": ["units of measure", "measure"]}
        said = ("One unit of measure, CSE, is used for the pads product. It appears on two "
                "invoice lines; one of those lines invoices 2 in unit CSE.")
        self.assertEqual(self.outcome([uom], ans(said)), "correct")
        self.assertEqual(self.outcome([uom], ans("Two units of measure are used.")), "wrong")
        # a spelled word that is not the label's figure is never read as it
        self.assertNotEqual(self.outcome([uom], ans("One of the lines uses 3 units of measure.")),
                            "correct")
        self.assertEqual(self.outcome([COUNT], ans("There are three purchase orders.")),
                         "correct")

    def test_none_against_a_count_is_read_only_of_the_counted_thing(self):
        # dev 11-10: refusals ("the count named no order records it was taken from") and
        # answers ("no field on its record gives a reason") graded as answering none
        for said in ("Not settled: the count of purchase orders, because the count named no "
                     "order records it was taken from.",
                     "Its status is Active, but no field on its record gives a reason."):
            with self.subTest(said=said):
                verdict = gen.grade(item([COUNT]), ans(said), OWNER)
                self.assertEqual(verdict["facts"], {"count": "miss"})
                self.assertNotIn("fact_contradicted", verdict["codes"])
        for said in ("There are no purchase orders.", "None of the purchase orders match.",
                     "Purchase orders matching: none."):
            with self.subTest(said=said):
                verdict = gen.grade(item([COUNT]), ans(said), OWNER)
                self.assertEqual(verdict["facts"], {"count": "wrong"})
        zero = dict(COUNT, value="0")
        self.assertEqual(self.outcome([zero], ans("There are no purchase orders.")),
                         "correct")
        self.assertNotEqual(self.outcome([zero], ans("No field gives a reason.")), "correct")
        # a figure stays the answer when one is given
        self.assertEqual(self.outcome([COUNT], ans("No delay: there are 3 purchase orders.")),
                         "correct")

    def test_an_identifier_member_is_read_in_a_readers_spelling(self):
        # dev 11-10: "PO#4499 has the highest total cost" graded as missing PO4499
        outlier = {"kind": "set", "id": "outlier", "members": [["PO4499"]],
                   "records": ["api://x"], "allowed": ["api"]}
        for said in ("PO#4499 has the highest total cost.", "PO 4499 stands out.",
                     "PO-4499 stands out.", "PO4499 stands out."):
            with self.subTest(said=said):
                self.assertEqual(self.outcome([outlier], ans(said)), "correct")
        padded = {**outlier, "members": [["809", "PO784"]]}
        for said in ("Lot 1618 came in on purchase order PO#0784.", "It came in on PO784."):
            with self.subTest(said=said):
                self.assertEqual(self.outcome([padded], ans(said)), "correct")
        for said in ("It came in on PO#07841.", "It came in on PO#1784."):
            with self.subTest(said=said):
                self.assertNotEqual(self.outcome([padded], ans(said)), "correct")
        for said in ("PO#44991 stands out.", "XPO4499 stands out.", "PO#4498 stands out."):
            with self.subTest(said=said):
                self.assertNotEqual(self.outcome([outlier], ans(said)), "correct")

    def test_a_withheld_fact_must_be_refused_never_stated(self):
        reader = ["purchasing_prices"]
        self.assertEqual(self.outcome([MONEY], ans("I can't share cost figures."), reader),
                         "correct")
        self.assertEqual(self.outcome([MONEY], ans("Total cost: 2100.00."), reader), "wrong")
        verdict = gen.grade(item([MONEY, SET]), ans("PX0004, PX0005, PX0006."), reader)
        self.assertEqual(verdict["outcome"], "correct")
        self.assertIn("partly_withheld", verdict["codes"])

    def test_the_summary_carries_no_question_text_or_ids(self):
        verdicts = [gen.grade(item([SET]), ans("PX0004"), OWNER),
                    gen.grade(item([SET]), None, OWNER)]
        text = json.dumps(gen.summary(verdicts))
        self.assertNotIn("Acme", text)
        self.assertNotIn("dev-G-0001", text)
        self.assertEqual(gen.summary(verdicts)["outcomes"], {"incomplete": 1, "no_answer": 1})


from collections import Counter  # noqa: E402  (used by the ground-truth test)

if __name__ == "__main__":
    unittest.main()
