"""The real backend over the console's fixture world (isolated-fixture, scripted planner):
the ask payload from the gate's claims, a restricted reader through every route, hidden
items as counts only, conflict cards, the Postgres suggest permission filter, feedback,
and the dev-identity refusals. No network; Postgres only in a throwaway database."""

from __future__ import annotations

import argparse
import contextlib
from concurrent.futures import Future
import io
import json
import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from datetime import date
from typing import Optional
from urllib.parse import quote
from unittest import mock

from fastapi.testclient import TestClient

from operating import objective_runner, proactive_engine
from operator_api import backend as be
from operator_api import models as m
from operator_api.server import main as serve_main
from operator_api.workspace import case_console
from operator_api.workspace import object_workspace as objects
from operator_api.tests import case_fixtures
from operator_api.tests.test_objective_box import _on_record

from operating.pg import scratch_db

scratch_db.use_test_cluster()   # the test cluster when one runs (bare runs too)

OPS = [case_fixtures.OPERATIONS, case_fixtures.PURCHASING, case_fixtures.QUALITY]


HOLD = threading.Event()
HOLD.set()
HELD = threading.Event()


def planner(request):
    if "HOLD" in request["question"] and not request["transcript"]:
        HELD.set()          # an Ask is now running in the pool...
        HOLD.wait(60)       # ...until the test lets it finish
    roots = [line.split(": ", 1)[1] for line in request["question"].splitlines()
             if line.startswith(objective_runner.ROOT_LINE)]
    if not request["transcript"]:
        return {"action": "call", "calls": [
            {"tool": "plx.object", "arguments": {"object_id": oid}} for oid in roots]
            or [{"tool": "plx.find", "arguments": {"query": case_fixtures.PO_NUMBER}}]}, None
    result = request["transcript"][0]["observations"][0]["result"]
    # a card over the observation bound arrives as its first entries under "shown"
    result = result.get("shown", result) if isinstance(result.get("shown"), dict) else result
    if "values" not in result:
        return {"action": "abstain", "reason": "nothing readable"}, None
    return {"action": "answer", "unknowns": ["not in the record: why it is late"],
            **_on_record(result["values"])}, None


def events(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


class Served:
    """``python3 -m operator_api serve --dev`` over the fixture state, in a thread."""

    def __init__(self, state: Path, principals, data_dir: Path) -> None:
        got: Queue = Queue(maxsize=1)
        argv = ["--dev", "--insecure-cookie", "--state-dir", str(state), "--ask",
                "--pg-dsn", "", "--app-data-dir", str(data_dir),
                "--console-arg=--source-mirror-audience",
                f"--console-arg={state / case_fixtures.MIRROR_AUDIENCE_FILE}"]
        for principal in principals:
            argv += ["--dev-fixture-principal", principal]

        def run():
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    serve_main(argv, ready=got.put, fixtures=case_fixtures, planner=planner)
            except BaseException as error:  # surfaced to the test thread
                got.put(error)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        server = got.get(timeout=120)
        if isinstance(server, BaseException):
            raise server
        self.app = server.config.app
        self.client = TestClient(self.app, base_url="http://testserver")
        assert self.client.post("/api/auth/dev-login", json={}).status_code == 200

    def stop(self) -> None:
        self.app.state.backend.stop()
        self.thread.join(timeout=30)


class FixtureBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="app-api-"))
        cls.state = cls.tmp / "state"
        case_fixtures.seed(cls.state, "plx")
        store = cls.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(cls.state, store)
        proactive_engine.after_build(store)
        cls.ops = Served(cls.state, OPS, cls.tmp / "ops-app")
        cls.outsider = Served(cls.state, ["staff:outsider"], cls.tmp / "outsider-app")

    @classmethod
    def tearDownClass(cls):
        cls.ops.stop()
        cls.outsider.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ask(self, served, question):
        response = served.client.post("/api/ask", json={"question": question})
        self.assertEqual(response.status_code, 200)
        return events(response.text)

    def test_ask_maps_the_gate_kept_claims_to_cited_facts(self):
        seen = self.ask(self.ops, f"is {case_fixtures.PO_NUMBER} late?")
        names = [n for n, _ in seen]
        self.assertEqual(names[0], "status")
        self.assertIn("understanding", names)
        self.assertEqual(names[-1], "answer")
        for name, data in seen:
            m.ASK_EVENTS[name].model_validate(data)
        answer = seen[-1][1]
        self.assertIn(answer["outcome"], ("answered", "partial"))
        self.assertTrue(answer["facts"])
        gated = [f for f in answer["facts"] if f["check"] == "passed"]
        self.assertTrue(gated, answer["facts"])
        source = gated[0]["sources"][0]
        self.assertEqual(source["system"], "Portal")
        self.assertTrue(source["fields"])           # the value the gate checked
        self.assertTrue(source["open_url"].startswith("/source?handle="))
        self.assertTrue(any("why it is late" in n["what"] for n in answer["not_found"]))
        self.assertEqual(answer["subjects"][0]["type"], "PurchaseOrder")
        # the cited source opens for this reader through /api/source
        handle = source["handle"]
        opened = self.ops.client.get("/api/source", params={"handle": handle})
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertEqual(opened.json()["handle"], handle)
        # feedback on one's own answer is logged; recent questions show it
        fb = self.ops.client.post("/api/feedback", json={
            "answer_id": answer["answer_id"], "rating": -1, "note": "wrong date"})
        self.assertEqual(fb.status_code, 200, fb.text)
        self.assertEqual(fb.json()["stored_in"], ["feedback_log", "feedback_store"])
        kept = self.ops.client.get("/api/feedback").json()["entries"][0]
        self.assertEqual((kept["question"], kept["note"]), (answer["question"], "wrong date"))
        self.assertTrue(kept["claims"])               # the answer's claims, attached
        logged = (self.tmp / "ops-app" / "feedback.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(logged[-1])["note"], "wrong date")
        self.assertEqual(oct((self.tmp / "ops-app" / "feedback.jsonl").stat().st_mode & 0o777),
                         "0o600")
        home = self.ops.client.get("/api/home").json()
        self.assertEqual(home["recent_questions"][0]["answer_id"], answer["answer_id"])
        # another reader cannot rate (or learn of) it
        other = self.outsider.client.post("/api/feedback", json={
            "answer_id": answer["answer_id"], "rating": 1})
        self.assertEqual(other.status_code, 404)

    def test_after_a_deploy_a_kept_page_is_served_only_while_the_new_code_reads_its_records(
            self):
        """A record page kept on disk by the code before a deploy is served (marked stale)
        while the page cannot be read in time, only after the code serving it decides every
        record the page shows readable for the reader again, through the case host's own
        visibility read (``HostAccess.visible_objects``). A deploy whose visibility read
        (here ``operating/ontology_tools.py``'s, a module no list names) withholds one of
        them serves nothing kept."""
        from operating import ontology_tools
        from operator_api.page_parts import shown_objects
        from operator_api.page_store import PageStore
        backend = self.ops.app.state.backend
        store = PageStore(self.tmp / "deploy-pages", "the-code-before")
        kept = mock.patch.object(backend.page_cache, "store", store)
        kept.start()
        self.addCleanup(kept.stop)
        from operator_api.page_store import kept_code

        def new_process():   # nothing in memory: an earlier test's copy is not this one's
            with backend._lock:
                backend._good.clear()
                backend._snap_inputs().clear()

        path = f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}"
        new_process()
        fresh = self.ops.client.get(path)
        self.assertEqual(fresh.status_code, 200, fresh.text)
        records = shown_objects(m.ObjectPage.model_validate(fresh.json()))
        self.assertIn(fresh.json()["object_id"], records)
        self.assertGreater(len(records), 1)
        disk = ("last-good", "staff:operations,staff:purchasing,staff:quality", "dev",
                ("page", "PurchaseOrder", case_fixtures.PO_NUMBER, False))
        for _ in range(400):   # kept off the request
            if store.get(disk, code=kept_code()) is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(store.get(disk, code=kept_code()))

        def busy(*_args, **_kwargs):
            raise TimeoutError("workbench_busy")

        def deployed():
            new_process()   # a deploy: a new process, the disk copy only
            with mock.patch.object(store, "code", "a-deployed-code-version"), \
                    mock.patch.object(be, "object_page", busy):
                return self.ops.client.get(path)

        served = deployed()
        self.assertEqual(served.status_code, 200, served.text)
        self.assertIsNotNone(served.json()["stale_since"])
        self.assertEqual(served.json()["object_id"], fresh.json()["object_id"])
        withheld = sorted(r for r in records if r != fresh.json()["object_id"])[0]
        read = ontology_tools.StoreReader._visible_versions_read

        def new_code(reader, scope, ids):   # the deploy's visibility read withholds one
            return [row for row in read(reader, scope, ids) if row["object_id"] != withheld]

        with mock.patch.object(ontology_tools.StoreReader, "_visible_versions_read", new_code):
            refused = deployed()
        self.assertNotEqual(refused.status_code, 200, refused.text)
        self.assertNotIn(withheld, refused.text)

    def test_object_page_and_suggest_for_the_reader(self):
        found = self.ops.client.get("/api/suggest",
                                    params={"q": case_fixtures.PO_NUMBER}).json()
        self.assertEqual(found["suggestions"][0]["match"], "exact")
        typo = self.ops.client.get("/api/suggest", params={"q": "Synthetic Botle"}).json()
        self.assertTrue(any("Bottle" in s["label"] for s in typo["suggestions"]), typo)
        page = self.ops.client.get(f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}")
        self.assertEqual(page.status_code, 200, page.text)
        body = page.json()
        m.ObjectPage.model_validate(body)
        self.assertEqual(body["label"], case_fixtures.PO_NUMBER)
        self.assertTrue(body["facts"] and body["related"])
        whole = self.ops.client.get(found["suggestions"][0]["open_url"]
                                    .replace("/object/", "/api/object/", 1))
        self.assertEqual(whole.json()["object_id"], body["object_id"])
        # a search type's spelling ("purchase_order", as links once carried) opens the
        # same record, by its number or its whole id; the quick read is the same record
        for path in (f"purchase_order/{case_fixtures.PO_NUMBER}",
                     f"purchase_order/{quote(body['object_id'], safe='')}"):
            snake = self.ops.client.get(f"/api/object/{path}")
            self.assertEqual((snake.status_code, snake.json().get("object_id")),
                             (200, body["object_id"]), path)
        quick = self.ops.client.get(f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}",
                                    params={"quick": 1}).json()
        self.assertEqual(quick["object_id"], body["object_id"])
        # each slow section is its own call, by number or whole id, and carries exactly
        # what the full page shows for it (the full page is the quick read with them)
        parts = {"late": ("late_lines",), "people": ("people",),
                 "landed_cost": ("landed_cost",),
                 "trail": ("trail", "not_found", "proposals")}
        for ref in (case_fixtures.PO_NUMBER, quote(body["object_id"], safe="")):
            for name, fields in parts.items():
                got = self.ops.client.get(f"/api/object-section/{name}/PurchaseOrder/{ref}")
                self.assertEqual(got.status_code, 200, (name, got.text))
                section = m.ObjectSection.model_validate(got.json()).model_dump(mode="json")
                self.assertEqual((section["object_id"], section["section"]),
                                 (body["object_id"], name))
                for field in fields:
                    self.assertEqual(section[field], body[field], (name, field))
                if name == "late" and section["status"] is not None:
                    self.assertEqual(section["status"], body["status"])
        self.assertIsNotNone(body["trail"])
        # the drafts come with the trail, as built from the whole page (its late lines
        # and trail): the app, which merges the sections into the quick read, shows them
        from operator_api import proposals
        drafts = proposals.for_page(m.ObjectPage.model_validate(body))
        self.assertEqual([m.Proposal.model_validate(p) for p in body["proposals"]], drafts)
        # an unknown section is refused before anything is read
        self.assertEqual(self.ops.client.get(
            f"/api/object-section/secrets/PurchaseOrder/{case_fixtures.PO_NUMBER}"
        ).status_code, 422)

    def test_a_po_page_and_answer_carry_its_trail_each_step_opening_for_the_reader(self):
        # the first page read of the class can be refused busy on a loaded machine (the
        # app retries a 503 calmly); only that refusal is asked again
        for _ in range(3):
            got = self.ops.client.get(f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}")
            if got.status_code != 503:
                break
        self.assertEqual(got.status_code, 200, got.text[:300])
        body = got.json()
        trail = body["trail"]
        self.assertEqual(trail["kind"], "purchase_order", trail)
        (po,) = trail["trails"]
        self.assertEqual(po["subject"]["object_id"], body["object_id"])
        self.assertEqual(po["steps"][-1]["kind"], "verdict", po["steps"])
        self.assertTrue(po["verdict"])
        # the fixture PO's status word is unmapped: lateness and promises are not read,
        # so nothing is said about a confirmation either way
        self.assertEqual((po["state"], po["eta"], po["next_action"]),
                         ("undetermined", None, None))
        for step in po["steps"]:
            if step["source"] is None:
                self.assertEqual((step["kind"], po["state"]), ("verdict", "undetermined"))
                continue
            opened = self.ops.client.get("/api/source",
                                         params={"handle": step["source"]["handle"]})
            self.assertEqual(opened.status_code, 200 if step["source"]["open_url"] else 404)
        answer = self.ask(self.ops, f"is {case_fixtures.PO_NUMBER} late?")[-1][1]
        self.assertEqual([t["subject"]["object_id"] for t in answer["trail"]["trails"]],
                         [body["object_id"]])
        self.assertEqual(answer["trail"]["trails"][0]["steps"], po["steps"])

    def test_a_source_lists_the_records_citing_it_for_its_reader_only(self):
        ops = self.ops.client
        po = ops.get(f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}").json()
        handle = po["facts"][0]["sources"][0]["handle"]
        got = ops.get("/api/source/records", params={"handle": handle})
        self.assertEqual(got.status_code, 200, got.text)
        body = m.SourceRecords.model_validate(got.json())
        self.assertEqual(body.handle, handle)
        self.assertTrue(body.served)
        # the PO this row was read into is listed first-hand, once; every listed record
        # opens for the same reader (nothing is named that its page would not show)
        mine = [r for r in body.records if r.record.object_id == po["object_id"]]
        self.assertEqual([r.how for r in mine], ["from"], body)
        self.assertEqual(len({r.record.object_id for r in body.records}), len(body.records))
        for row in body.records:
            self.assertNotEqual(row.record.label, row.record.object_id)
            opened = ops.get(row.record.open_url.replace("/object/", "/api/object/", 1),
                             params={"quick": 1})
            self.assertEqual(opened.status_code, 200, (row, opened.text))
        # not an L1 source, or none at all: not found, as /api/source answers
        for bad in ("teams://x/y", "no-scheme-here", "bc://" + "x" * 2100):
            self.assertEqual(ops.get("/api/source/records",
                                     params={"handle": bad}).status_code,
                             422 if len(bad) > 2000 else 404, bad)

    def test_a_restricted_reader_sees_nothing_through_any_route(self):
        ops = self.ops.client
        po = ops.get(f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}").json()
        handle = po["facts"][0]["sources"][0]["handle"]
        # what the outsider never typed: the supplier's name, record ids, the handle
        secrets_ = {"Synthetic Bottle", "po-synth", handle, po["object_id"]}
        out = self.outsider.client
        bodies = [json.dumps(self.ask(self.outsider, f"is {case_fixtures.PO_NUMBER} late and who "
                                      "supplies it?"))]
        for path, params in (("/api/suggest", {"q": case_fixtures.PO_NUMBER}),
                             ("/api/suggest", {"q": "Synthetic Botle"}),
                             ("/api/home", None), ("/api/briefing", None),
                             ("/api/suggestions", None),
                             (f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}", None),
                             ("/api/object/PurchaseOrder/" + po["object_id"], None),
                             *((f"/api/object-section/{name}/PurchaseOrder/{ref}", None)
                               for name in m.PAGE_SECTIONS
                               for ref in (case_fixtures.PO_NUMBER,
                                           quote(po["object_id"], safe=""))),
                             ("/api/source", {"handle": handle}),
                             ("/api/source/records", {"handle": handle})):
            response = out.get(path, params=params)
            self.assertIn(response.status_code, (200, 404), (path, response.text))
            bodies.append(response.text)
        self.assertEqual(out.get("/api/source", params={"handle": handle}).status_code, 404)
        self.assertEqual(out.get("/api/source/records", params={"handle": handle}).status_code,
                         404)
        for name in m.PAGE_SECTIONS:   # no section of a record they cannot see is read
            self.assertEqual(out.get(f"/api/object-section/{name}/PurchaseOrder/"
                                     + quote(po["object_id"], safe="")).status_code, 404)
        for body in bodies:
            for secret in secrets_:
                self.assertNotIn(json.dumps(secret)[1:-1], body, body[:300])
                self.assertNotIn(secret, body, body[:300])
        answer = json.loads(bodies[0])[-1][1]
        self.assertEqual(answer["facts"], [])

    def test_an_ask_in_progress_never_holds_up_other_routes(self):
        routes = (("/api/home", None), ("/api/suggest", {"q": case_fixtures.PO_NUMBER}),
                  (f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}", None),
                  ("/api/me", None))
        for client in (self.ops.client, self.outsider.client):
            for path, params in routes:   # cold costs are not what this measures
                client.get(path, params=params)
        HOLD.clear()
        streamed: list = []
        asker = threading.Thread(target=lambda: streamed.append(self.ops.client.post(
            "/api/ask", json={"question": f"HOLD is {case_fixtures.PO_NUMBER} late?"})),
            daemon=True)
        asker.start()
        self.assertTrue(HELD.wait(60), "the Ask never reached its planner")
        try:
            po = self.outsider.client  # another reader, and the same reader, both answer
            for client in (self.ops.client, po):
                for path, params in routes:
                    started = time.perf_counter()
                    response = client.get(path, params=params)
                    took = time.perf_counter() - started
                    self.assertIn(response.status_code, (200, 404), path)
                    self.assertLess(took, 1.0, (path, took))
        finally:
            HOLD.set()
            asker.join(60)
        self.assertEqual(events(streamed[0].text)[-1][0], "answer")

    def test_briefing_ranks_what_the_reader_sees_counts_the_rest_and_never_sends(self):
        ops = self.ops.client.get("/api/briefing").json()
        m.Briefing.model_validate(ops)
        self.assertEqual(ops["status"], "read")
        self.assertGreater(ops["count"], 0)
        self.assertEqual([i["rank"] for i in ops["items"]], list(range(1, len(ops["items"]) + 1)))
        first = ops["items"][0]
        self.assertTrue(first["next_action"]["suggestion_only"])
        self.assertTrue(first["open_url"].startswith("/object/"))
        self.assertIs(ops["digest_sent"], False)
        self.assertIn("Preview only", ops["digest_preview"])
        self.assertIn(f"{ops['count']} item", ops["digest_preview"])
        # the outsider is briefed on nothing; what is there for it is a count
        out = self.outsider.client.get("/api/briefing").json()
        self.assertEqual((out["count"], out["items"]), (0, []))
        self.assertGreaterEqual(out["hidden"], ops["count"])
        for item in ops["items"]:
            self.assertNotIn(item["object_id"], json.dumps(out))
            self.assertNotIn(item["summary"], json.dumps(out))
        # every item is in its type's group (the top ones shown, the rest opened as a list)
        self.assertEqual(sum(g["count"] for g in ops["groups"]), ops["count"])
        self.assertLessEqual(len(ops["items"]), 7)
        for group in ops["groups"]:
            page = self.ops.client.get("/api/briefing/items", params={
                "event_type": group["event_type"], "limit": 100}).json()
            m.BriefingItems.model_validate(page)
            # "+N more" lists the group's items past the top ones, never those again
            rest = group["count"] - group["in_top"]
            self.assertEqual((page["total"], len(page["items"])), (rest, rest))
            top_ids = {i["id"] for i in ops["items"]}
            self.assertFalse(top_ids & {i["id"] for i in page["items"]})
            hidden = self.outsider.client.get("/api/briefing/items", params={
                "event_type": group["event_type"]}).json()
            self.assertEqual((hidden["total"], hidden["items"]), (0, []))
        # the late count is Home's
        home = self.ops.client.get("/api/home").json()
        self.assertEqual(ops["late_total"], home["needs_attention"]["late_total"])
        # a late purchase order names its supplier as Home's late list names it; nothing
        # else is given one
        listed = {p["object_id"]: p.get("supplier") for p in home["needs_attention"]["late_pos"]}
        for item in ops["items"]:
            if item["object_type"] != "PurchaseOrder":
                self.assertIsNone(item.get("counterpart"))
            elif item["object_id"] in listed and listed[item["object_id"]]:
                self.assertEqual(item["counterpart"], listed[item["object_id"]])
        self.assertTrue(any(i.get("counterpart") for i in ops["items"]
                            if i["object_id"] in listed), ops["items"])
        self.assertTrue(all(c["words"] for c in ops["checks"]))
        # seen: the next read says when, and nothing first seen before it is new
        seen = self.ops.client.post("/api/briefing/seen", headers={
            "Origin": "http://testserver"}).json()
        self.assertTrue(seen["ok"])
        again = self.ops.client.get("/api/briefing").json()
        self.assertEqual((again["last_visit"], again["new_since_visit"]), (seen["at"], 0))

    def test_suggestions_draft_from_what_the_reader_sees_and_never_send(self):
        ops = self.ops.client.get("/api/suggestions").json()
        m.Suggestions.model_validate(ops)
        self.assertEqual((ops["status"], ops["sent"]), ("read", False))
        self.assertGreater(ops["total"], 0)
        briefing = self.ops.client.get("/api/briefing").json()
        # every briefing finding is on a card or counted by type: none dropped
        self.assertLessEqual(ops["total"] + sum(ops["collapsed"].values()), briefing["count"])
        for card in ops["items"]:
            self.assertTrue(card["action"]["suggestion_only"])
            self.assertEqual((card["draft"]["sent"], card["draft"]["status"]),
                             (False, "draft_not_sent"))
            self.assertIn(card["confidence"]["level"], ("high", "medium", "low"))
            for fact in card["draft"]["facts"]:   # each stated fact opens for this reader
                self.assertNotEqual(self.ops.client.get(
                    "/api/source", params={"handle": fact["source"]["handle"]}).status_code,
                    404, fact)
        out = self.outsider.client.get("/api/suggestions").json()
        self.assertEqual((out["total"], out["items"], out["collapsed"]), (0, [], {}))

    def test_show_all_pages_every_late_order_and_never_another_readers(self):
        home = self.ops.client.get("/api/home").json()
        total = home["needs_attention"]["late_total"]
        seen, offset = [], 0
        while True:
            page = self.ops.client.get("/api/late", params={"offset": offset,
                                                             "limit": 2}).json()
            m.LateList.model_validate(page)
            self.assertEqual(page["total"], total)
            seen += [p["object_id"] for p in page["items"]]
            offset += len(page["items"])
            if not page["more"] or not page["items"]:
                break
        self.assertEqual(len(seen), total)
        self.assertEqual(len(set(seen)), total)   # one line per order, none twice
        # the list states the same data cut-off as Home
        self.assertIsNotNone(home["freshness"]["as_of"])
        self.assertEqual(page["as_of"], home["freshness"]["as_of"])
        self.assertEqual(seen[:len(home["needs_attention"]["late_pos"])],
                         [p["object_id"] for p in home["needs_attention"]["late_pos"]])
        # read (and kept) for ops first: the outsider still sees none of it
        out = self.outsider.client.get("/api/late").json()
        self.assertEqual((out["total"], out["items"]), (0, []))
        out_home = self.outsider.client.get("/api/home").json()["needs_attention"]
        self.assertEqual(out_home["late_pos"], [])

    def test_card_labels_read_in_one_batch_say_what_each_record_alone_says(self):
        """Speed 11-12: Home, the late list and the briefing labelled each record with one
        whole ``plx.object`` (links, twins, attestations) per label, 20-500 per read. They
        are now read in one batch, whose label and supplier are exactly the record's own
        read, for every reader: a reader who may not read a record gets nothing from the
        batch (the record is read alone and says nothing, as before), and a supplier name the
        reader may not read sends the record to its own read."""
        from operator_api.page_parts import shown_objects
        backend = self.ops.app.state.backend
        page = self.ops.client.get(f"/api/object/PurchaseOrder/{case_fixtures.PO_NUMBER}")
        records = sorted(shown_objects(m.ObjectPage.model_validate(page.json())))
        records += [p["object_id"] for p in self.ops.client.get(
            "/api/late", params={"offset": 0, "limit": 100}).json()["items"]]
        records = sorted(set(records))
        self.assertGreater(len(records), 2)

        readers = {}
        for who, served in (("ops", self.ops), ("outsider", self.outsider)):
            serving = served.app.state.backend._serving
            with mock.patch.object(served.app.state.backend, "_serving",
                                   lambda w, *a, **k: readers.setdefault(who, w) and
                                   serving(w, *a, **k)):
                served.client.get("/api/late")

        def alone(view, oid, supplier):
            try:
                return be.card_words(view, oid, supplier=supplier)
            except Exception as error:  # noqa: BLE001 - refused alone: refused batched
                return type(error).__name__
        for who in ("ops", "outsider"):
            backend = (self.ops if who == "ops" else self.outsider).app.state.backend
            for n in (1, 10, 60):
                ids = (records * 60)[:n]
                for supplier in (True, False):
                    def job(view):
                        calls = []
                        real = view._call
                        with view._request():
                            each = {oid: alone(view, oid, supplier) for oid in ids}
                            batch = view.access.object_words(ids, ("vendor_name",))
                            view._call = lambda name, args: calls.append(name) or real(
                                name, args)
                            try:
                                got = be.cards_words(view, ids, supplier=supplier)
                            except Exception as error:  # noqa: BLE001
                                got = type(error).__name__
                            finally:
                                view._call = real
                        return each, batch, got, calls
                    with self.subTest(who=who, n=n, supplier=supplier):
                        each, batch, got, calls = backend.reads.run(readers[who], job)
                        if who == "outsider":
                            self.assertEqual(batch, {})      # the batch reveals nothing
                            self.assertEqual(set(each.values()), {("", None)})
                            self.assertEqual(got, each)      # read alone, refused alone
                            continue
                        self.assertEqual(set(batch), set(ids))
                        self.assertEqual(got, each)          # word for word, in order
                        self.assertEqual(list(got), list(dict.fromkeys(ids)))
                        # no whole record read per label (a lone record reads alone)
                        if len(set(ids)) > 1:
                            self.assertNotIn("plx.object", calls)
        # a supplier name withheld from the reader, or a batch that fails: read alone
        alone_read = []

        def call(name, args):
            alone_read.append(args.get("object_id", args.get("from")))
            return {"object": {"display": "whole"}, "values": []}
        for answer in ({"a": {"display": "A", "values": {"vendor_name": ["V"]},
                              "withheld": set()},
                        "b": {"display": "B", "values": {}, "withheld": {"vendor_name"}}},
                       OSError("store")):
            def words(ids, props, answer=answer):
                if isinstance(answer, Exception):
                    raise answer
                return answer
            alone_read.clear()
            view = SimpleNamespace(_call=call, access=SimpleNamespace(object_words=words))
            got = be.cards_words(view, ["a", "b"], supplier=True)
            if isinstance(answer, Exception):
                self.assertEqual(got, {"a": ("whole", None), "b": ("whole", None)})
            else:
                self.assertEqual(got, {"a": ("A", "V"), "b": ("whole", None)})
                self.assertEqual(alone_read[0], "b")      # b's own read, never a's

    def test_late_pos_on_home_are_longest_late_first(self):
        home = self.ops.client.get("/api/home").json()
        m.Home.model_validate(home)
        days = [p["days_late"] for p in home["needs_attention"]["late_pos"]]
        self.assertEqual(days, sorted(days, reverse=True))


class _View:
    """The workspace parts the mapping reads: ``decide`` withholding chosen handles, and
    a policy that grants nothing."""

    def __init__(self, closed):
        self.closed = set(closed)
        view = self

        class Policy:
            def may_read(self, actor, key):
                return False

        class Access:
            def decide(self, handles):
                return {h: ({"reason": "not_granted"} if h in view.closed else None)
                        for h in handles}

            def reader_policy(self):
                return object(), Policy()
        self.access = Access()

    def _can_open(self, handle):
        return True


OID_OK = "11111111-1111-4111-8111-111111111111"
OID_EXCLUDED = "22222222-2222-4222-8222-222222222222"
UNITS = {"api://public.app_purchase_order/x@g-1": "api:public.app_purchase_order",
         "graph-mail://a/SECRET-1@g-1": f"mail:{OID_OK}",
         "graph-mail://b/SECRET-2@g-1": f"mail:{OID_OK}",
         "graph-mail://c/SECRET-3@g-1": f"mail:{OID_EXCLUDED}",
         "sharepoint://d/SECRET-4": None}


class Mapping(unittest.TestCase):
    def backend(self):
        return be.WorkspaceBackend(lambda s: None, data_label="test")

    def hidden(self, exclusion):
        from integrations.people_exclusion import PeopleExclusion  # noqa: F401
        shown = {"text": "visible", "citations": ["api://public.app_po/1@g-1"]}
        secret = [{"text": f"SECRET subject line {h}", "citations": [h]} for h in UNITS]
        ran = {"claims": [shown], "withheld": len(secret),
               "loop": {"claims": [shown, *secret]}}
        with mock.patch("operator_api.workspace.object_workspace.audience_unit", UNITS.get), \
                mock.patch("operating.pg.permissions.load_exclusion", return_value=exclusion):
            return self.backend()._hidden(_View(set(UNITS)), ran)

    def test_hidden_claims_follow_the_search_rule_and_show_nothing(self):
        from integrations.people_exclusion import PeopleExclusion
        hidden = self.hidden(PeopleExclusion([OID_EXCLUDED]))
        text = hidden.model_dump_json()
        # Portal (shared, named) 1 + two private mails (one unnamed bucket); the excluded
        # person's mail and the unit-less file are never counted
        self.assertEqual(hidden.count, 3)
        self.assertEqual([(o.owner_or_team, o.count, o.named) for o in hidden.by_owner],
                         [("Portal", 1, True),
                          ("Other people's mail, chats and files you can't open", 2, False)])
        for leak in ("SECRET", "graph-mail", OID_OK, OID_EXCLUDED, "sharepoint"):
            self.assertNotIn(leak, text)
        self.assertIn("don't have access", hidden.message)
        unloaded = self.hidden(PeopleExclusion(unavailable="excluded_people_oids_absent"))
        self.assertIsNone(unloaded.count)
        self.assertEqual(unloaded.by_owner, [])

    def test_landed_cost_comes_from_the_plans_computed_entries_not_prose(self):
        line = "bc://c/purchaseOrderLines/1@g-1"
        ran = {"investigation": {"answerability": {"state": "answerable"}, "answer": [
            {"name": "unit_price", "kind": "property", "value": "0.84",
             "citation": {"l1_handle": line, "field_path": "row.directUnitCost"}},
            {"name": "landed_cost_line_1", "kind": "computed", "value": "10080.00",
             "unit": "USD", "inputs": ["unit_price"]},
            {"name": "landed_cost_line_2", "kind": "computed", "value": "2265.675",
             "unit": "USD", "inputs": []},
            {"name": "landed_cost", "kind": "computed", "value": "12345.675", "unit": "USD",
             "inputs": ["landed_cost_line_1", "landed_cost_line_2"]},
            # decoys: a rate, a quantity, a label with capitals and digits
            {"name": "landed_cost_line_3", "kind": "computed", "value": "0.84",
             "unit": "USD/ea"},
            {"name": "landed_cost_line_4", "kind": "computed", "value": "4471",
             "unit": "SKU", "currency": "SKU1"},
            {"name": "goods_qty", "kind": "computed", "value": "12000", "unit": "EA"}]},
            "verdict": {"state": "answered",
                        "text": "Landed cost 12345.68 USD",
                        "facts": [{"text": "Item: 4471 SKU (BC line)", "citation": line,
                                   "openable": True}], "follow_ups": []}}
        cost = be.landed_cost_of(self.backend(), _View(()), ran)
        self.assertEqual(cost.status, "computed")
        self.assertEqual((cost.total.amount, cost.total.currency), ("12345.675", "USD"))
        self.assertEqual([(l.label, l.amount.amount, l.amount.currency) for l in cost.lines],
                         [("Line 1", "10080.00", "USD"), ("Line 2", "2265.675", "USD")])
        self.assertEqual([s.handle for s in cost.lines[0].sources], [line])
        self.assertNotIn("4471", cost.model_dump_json())
        prose_only = {"investigation": {"answer": []}, "verdict": ran["verdict"]}
        nothing = be.landed_cost_of(self.backend(), _View(()), prose_only)
        self.assertEqual((nothing.status, nothing.total, nothing.lines),
                         ("not_established", None, []))

    def test_people_are_named_with_their_roles_and_only_when_openable(self):
        cite = "graph-mail://box/messages/1@g-1"
        closed = "graph-mail://box/messages/2@g-1"
        ran = {"investigation": {"answer": [
            {"name": "person_1", "kind": "computed", "value": {
                "untrusted_text": "Dana Ortiz", "origin": {"citation": cite}},
             "steps": ["identity verified", "roles: owner on 2 record(s), sender on 1 "
                                            "record(s)"]},
            {"name": "person_2", "kind": "computed", "value": {
                "untrusted_text": "Hidden Person", "origin": {"citation": closed}},
             "steps": ["identity verified", "roles: approver on 1 record(s)"]},
            {"name": "person", "kind": "count", "value": "2"}]}}
        people = be.people_of(self.backend(), _View({closed}), ran)
        self.assertEqual([(p.name, p.role) for p in people], [("Dana Ortiz", "owner, sender")])
        self.assertIsNone(be.people_of(self.backend(), _View(()), {"investigation": {
            "answer": []}}))

    def conflict(self, ranks):
        return {"kind": "value_disagreement", "subject": "PurchaseOrder.expected_date",
                "values": [{"property": "expected_date", "value": v,
                            "citation": {"l1_handle": h}, "authority_rank": r}
                           for v, h, r in zip(("2026-10-17", "2026-10-24"),
                                              ("bc://c/purchaseOrders/1", "api://p/po/1"),
                                              ranks)]}

    def test_conflict_cards_trust_the_declared_authority_and_say_why(self):
        [card] = self.backend()._conflicts(_View(()), [self.conflict((1, 2))], "PO 1")
        self.assertEqual([v.trusted for v in card.values], [True, False])
        self.assertIn("BC is the system of record", card.values[0].why)
        self.assertEqual(card.field, "expected date")
        [tie] = self.backend()._conflicts(_View(()), [self.conflict((None, None))], "PO 1")
        self.assertEqual([v.trusted for v in tie.values], [False, False])
        self.assertIn("both are shown", tie.values[0].why)
        hidden_side = self.backend()._conflicts(_View({"bc://c/purchaseOrders/1"}),
                                                [self.conflict((1, 2))], "PO 1")
        self.assertEqual(hidden_side, [])

    def test_a_cached_page_is_decided_again_for_its_sources(self):
        handle = "sharepoint://site/drive/item-1@g-1"
        view = _View(())
        built = []

        class Policy:
            def may_read(self, actor, key):
                return True

        view.access.reader_policy = lambda: (object(), Policy())
        view.access.store_meta = lambda: {"content_digest": "sha256:x"}
        view._request = contextlib.nullcontext
        view.investigate = lambda family, oid: {}

        def workspace(oid):
            built.append(oid)
            return {"object_id": oid, "status": "read", "card": {
                "object": {"object_type": "PurchaseOrder", "display": "PO 1"},
                "values": [{"property": "status", "value": "open",
                            "citation": {"l1_handle": handle}}]}}
        view.workspace = workspace
        backend = self.backend()
        with mock.patch("operating.pg.permissions._fingerprint", lambda p, a: "fp"):
            be.object_page(backend, view, "PurchaseOrder", "PurchaseOrder:demo:1")
            be.object_page(backend, view, "PurchaseOrder", "PurchaseOrder:demo:1")
            self.assertEqual(len(built), 1)                 # served from the cache
            view.closed.add(handle)                          # its item ACL changed
            be.object_page(backend, view, "PurchaseOrder", "PurchaseOrder:demo:1")
            self.assertEqual(len(built), 2)                 # never served stale

    def test_ask_switched_off_says_so_plainly(self):
        from fastapi.testclient import TestClient as Client
        from operator_api.app import create_app
        from operator_api.auth import DevAuth, fixture_identity
        backend = be.WorkspaceBackend(lambda s: None, data_label="test", ask_served=False)
        client = Client(create_app(backend, DevAuth(fixture_identity("plx", ["staff:x"])),
                                   secure_cookie=False))
        client.post("/api/auth/dev-login", json={})
        last = events(client.post("/api/ask", json={"question": "late?"}).text)[-1]
        self.assertEqual(last, ("error", {"code": "ask_not_served",
                                          "message": "Ask isn't switched on on this server."}))

    def test_dev_identity_refusals(self):
        parser = argparse.ArgumentParser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                be.run(parser, argparse.Namespace(dev_ticket_login=False,
                                                  dev_employee_oid=None,
                                                  dev_fixture_principal=[]), "dev",
                       lambda *a: 0)
            tmp = Path(tempfile.mkdtemp(prefix="app-api-live-"))
            self.addCleanup(shutil.rmtree, tmp, True)
            (tmp / "session-state-label.json").write_text(json.dumps(
                {"schema": "plx-session-state-label/v1", "label": "live"}))
            args = argparse.Namespace(dev_fixture_principal=[], dev_employee_oid="x",
                                      dev_ticket_login=False)
            with self.assertRaises(SystemExit):
                be.make_auth(parser, args, "dev", {"resolver": object(), "session": None,
                                                   "state_dir": tmp})


PG = shutil.which("createdb") is not None and os.environ.get("PLX_TEST_NO_PG") != "1"


@unittest.skipUnless(PG, "no local Postgres")
class SuggestPermissionFilter(unittest.TestCase):
    """``search.entities`` through the reader scope, on the own migrations in a
    throwaway database."""

    def setUp(self):
        self.db = f"plx_test_appapi_{os.getpid()}"
        if subprocess.run(["createdb", self.db], capture_output=True).returncode != 0:
            self.skipTest("createdb refused")
        self.addCleanup(subprocess.run, ["dropdb", "--if-exists", "--force", self.db],
                        capture_output=True)
        self.dsn = f"postgresql:///{self.db}"
        import psycopg
        from operating.pg.migrate import migrate
        with psycopg.connect(self.dsn) as conn:
            conn.autocommit = True
            migrate(conn)
            for i, (name, key, marks) in enumerate((
                    ("Maple Leaf Soapworks", "api:public.app_supplier", []),
                    ("Maple Grove Secret Holdings", "api:public.app_finance_only", []),
                    ("Maple Street Payroll", "api:public.app_supplier", ["pii"]),
                    ("Northwind Packaging", "api:public.app_supplier", []))):
                conn.execute("INSERT INTO search.entities (entity_id, object_id, type, name, "
                             "normalized, audience_key, markings) VALUES (%s,%s,%s,%s,%s,%s,"
                             "%s)", (f"e{i}", f"Supplier:demo:{i}", "Supplier", name,
                                     be.normalize(name), key, marks))
        self.backend = be.WorkspaceBackend(lambda s: None, data_label="test", pg_dsn=self.dsn)
        self.addCleanup(self.backend.stop)
        from integrations.people_exclusion import PeopleExclusion
        patcher = mock.patch("operating.pg.permissions.load_exclusion",
                             return_value=PeopleExclusion([OID_EXCLUDED]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def grant(self, *keys):
        from ontology.types import Actor
        from operating.ontology_tools import ReadPolicy
        actor = Actor("plx", frozenset({"staff:x"}), "internal")
        policy = ReadPolicy("plx", {key: frozenset({"staff:x"}) for key in keys})
        self.backend._reader_policy = lambda reader: (actor, policy)

    def test_only_granted_audiences_and_held_markings_and_typos_match(self):
        reader = type("R", (), {"key": "staff:x"})()
        self.grant("api:public.app_supplier")
        self.backend._pg_suggest(reader, "warm", 8)   # pool and scope listing warmed
        started = time.perf_counter()
        got = self.backend._pg_suggest(reader, "mapel leaf", 8)
        took = (time.perf_counter() - started) * 1000
        names = [s.label for s in got]
        self.assertIn("Maple Leaf Soapworks", names)              # a typo still matches
        self.assertNotIn("Maple Grove Secret Holdings", names)    # audience not granted
        self.assertNotIn("Maple Street Payroll", names)           # marking not held
        self.assertLess(took, 200)
        # a revoked grant applies on the very next suggestion (no cached grant)
        self.grant()
        self.assertEqual(self.backend._pg_suggest(reader, "maple leaf", 8), [])

    def test_an_item_gated_name_is_suggested_only_when_the_reader_opens_its_record(self):
        """IN-13: a chat topic, file name or mail participant is in a unit the reader may
        be granted while the item itself (a chat's members, a file's ACL, a message's
        mailbox scope) is closed to them: it is suggested only when the reader's own read
        (``visible_objects``) opens its record."""
        import contextlib
        import psycopg
        chat = 'TeamsChat:gen.teams.chats:{"chat_id":"19:abc@thread.v2"}'
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute("INSERT INTO search.entities (entity_id, object_id, type, name, "
                         "normalized, audience_key, markings) VALUES (%s,%s,%s,%s,%s,%s,"
                         "'{}')", ("e-chat", chat, "chat", "Maple Board Offsite",
                                   be.normalize("Maple Board Offsite"), "teams:chat:19:abc"))
        reader = type("R", (), {"key": "staff:x"})()
        self.grant("api:public.app_supplier", "teams:chat:19:abc")
        asked = []

        def run(opens):
            def call(_reader, job, priority=0, deadline=None):
                view = SimpleNamespace(_request=contextlib.nullcontext, access=SimpleNamespace(
                    visible_objects=lambda ids: asked.append(list(ids)) or opens(ids)))
                done: Future = Future()
                try:
                    done.set_result(job(view))
                except Exception as exc:  # noqa: BLE001 - the job's own failure
                    done.set_exception(exc)
                return done
            return mock.patch.object(self.backend.reads, "submit", side_effect=call)

        def broken(ids):
            raise OSError("down")
        for label, opens in (("refused", lambda ids: frozenset()), ("failed", broken),
                             ("not a set", lambda ids: list(ids))):
            with run(opens):
                names = [s.label for s in self.backend._pg_suggest(reader, "maple", 8)]
            self.assertNotIn("Maple Board Offsite", names, label)
            self.assertIn("Maple Leaf Soapworks", names, label)   # an ungated row stays
        with run(lambda ids: frozenset(ids)):
            names = [s.label for s in self.backend._pg_suggest(reader, "maple", 8)]
        self.assertIn("Maple Board Offsite", names)
        self.assertTrue(asked and all(ids == [chat] for ids in asked), asked)


class UnderstandingReaderPolicyTests(unittest.TestCase):
    """UNDERSTAND-001: the optional understanding (PLX_APP_UNDERSTANDING=1) runs under
    the reader's own read policy and never erases a record the reader's ``plx.find``
    already resolved; a record the reader cannot open never leaves it. Evidence label
    ``isolated-fixture``."""

    def view(self, policy, denied=()):
        gated = SimpleNamespace(name="gated")
        return SimpleNamespace(
            actor=SimpleNamespace(name="reader"),
            access=SimpleNamespace(reader_policy=lambda: (gated, policy),
                                   visible_objects=lambda ids: frozenset()),
            _call=lambda tool, arguments: {"error": "not_found"}
            if arguments.get("object_id") in denied else {"object_id": arguments["object_id"]}
        ), gated

    def decide(self, understand, view, found):
        from operator_api import models as m
        backend = SimpleNamespace(understand=understand,
                                  _found_understanding=be.WorkspaceBackend._found_understanding)
        return be.WorkspaceBackend._understanding(
            backend, view, m.AskRequest(question="Did PO12345 and PO99999 arrive?"), found)

    def test_the_reader_s_policy_reaches_understanding_and_found_roots_stay(self):
        from operating.query_understanding import understand
        policy = object()
        view, gated = self.view(policy, denied={"PurchaseOrder:99999"})
        found = {"roots": [{"object_id": "PurchaseOrder:12345", "mention": "PO12345",
                            "object_type": "PurchaseOrder"}]}
        calls = []

        def recording(question, **kwargs):
            calls.append(kwargs)
            return {"intent": "status", "entities": [
                {"text": "PO12345", "resolved": None, "candidates": []},
                {"text": "PO99999", "resolved": "PurchaseOrder:99999",
                 "candidates": [{"object_id": "PurchaseOrder:99999",
                                 "type": "PurchaseOrder", "label": "PO99999"}]}]}
        got = self.decide(recording, view, found)
        self.assertIs(calls[0]["policy"], policy)
        self.assertIs(calls[0]["actor"], gated)
        # IN-13: item-gated names are checked by this reader's own read
        self.assertIs(calls[0]["item_check"], view.access.visible_objects)
        resolved = [e.resolved.object_id for e in got.entities if e.resolved is not None]
        self.assertEqual(resolved, ["PurchaseOrder:12345"])   # found kept, denied dropped
        self.assertNotIn("PurchaseOrder:99999", json.dumps(got.model_dump()))
        self.assertEqual([e.text for e in got.entities if e.text == "PO12345"], ["PO12345"])
        # the real interpreter given no policy resolves nothing: the found root still stands
        view, _ = self.view(None)
        got = self.decide(lambda question, **kw: understand(
            question, **{**kw, "policy": None}, llm=False), view, found)
        self.assertEqual([e.resolved.object_id for e in got.entities if e.resolved],
                         ["PurchaseOrder:12345"])
        # switched off: the grammar's roots alone
        got = self.decide(None, view, found)
        self.assertEqual([e.resolved.object_id for e in got.entities if e.resolved],
                         ["PurchaseOrder:12345"])


if __name__ == "__main__":
    unittest.main()


class HomeFreshnessTest(unittest.TestCase):
    """Home states each source's age from the served projection's own capture time, by
    the one staleness rule answers apply (``ontology_tools.aged``), never from the host
    ledger's capture of a cycle still running (demo rehearsal 2026-10-31: Home said fresh
    while every answer said stale)."""

    def test_a_source_the_delta_serves_is_as_fresh_as_its_newest_layer(self):
        import sqlite3
        tmp = Path(tempfile.mkdtemp(prefix="delta-fresh-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        store = tmp / "projection" / "ontology.sqlite"
        layer = tmp / "projection" / "delta" / "delta-2.sqlite"
        layer.parent.mkdir(parents=True)
        with sqlite3.connect(layer) as conn:
            conn.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany("INSERT INTO store_meta VALUES (?, ?)", [
                ("delta_families", json.dumps(["graph_mail"])),
                ("freshness", json.dumps([
                    {"source": "graph_mail", "captured_at": "2026-11-05T18:55:00Z",
                     "completeness": "partial"},
                    {"source": "teams", "captured_at": "2026-11-05T18:55:00Z"}]))])

        def point(digest):
            (layer.parent / "CURRENT.json").write_text(json.dumps(
                {"format": "plx-delta/v1", "base_digest": digest, "store": str(layer),
                 "layers": [{"store": str(layer)}]}))
        served = [{"source": "graph_mail", "captured_at": "2026-11-05T06:46:00Z",
                   "completeness": "complete"},
                  {"source": "teams", "captured_at": "2026-10-30T02:18:00Z"}]
        point("sha256:gen11")
        merged = be.with_delta_freshness(served, store, "sha256:gen11")
        self.assertEqual([e["captured_at"] for e in merged],
                         ["2026-11-05T18:55:00Z", "2026-10-30T02:18:00Z"])  # not its family
        self.assertEqual(merged[0]["completeness"], "partial")
        point("sha256:gen10")            # a delta over another generation is not served
        self.assertEqual(be.with_delta_freshness(served, store, "sha256:gen11"), served)

    def test_home_and_answers_judge_each_source_alike(self):
        import sqlite3
        from datetime import datetime, timezone
        from operating import ontology_tools

        tmp = Path(tempfile.mkdtemp(prefix="home-fresh-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        now = datetime(2026, 10, 31, 5, 20, tzinfo=timezone.utc)
        entries = [
            {"source": "portal_api", "captured_at": "2026-10-30T15:08:15Z",
             "completeness": "complete", "stale": False},      # 14 h: within 36 h
            {"source": "teams", "captured_at": "2026-10-29T10:15:03Z",
             "completeness": "partial", "stale": False},       # 43 h: past it
            {"source": "bc", "captured_at": "2026-10-31T05:00:00Z",
             "completeness": "complete", "stale": True,         # an outage copy
             "label": "source-mirror captures as of X, live source unavailable (500, x)"},
            {"source": "graph_mail", "captured_at": None, "completeness": "unknown",
             "stale": False},                                   # never captured
            {"source": "sharepoint", "captured_at": "2026-10-31T05:00:00Z",
             "completeness": "partial", "stale": False},       # young, but partial
            {"source": "mission_control", "captured_at": "2026-10-31T05:00:00Z",
             "completeness": "complete", "stale": False}]      # young, last run failed
        store = tmp / "projection" / "ontology.sqlite"
        store.parent.mkdir()
        with sqlite3.connect(store) as conn:
            conn.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO store_meta VALUES ('freshness', ?)",
                         (json.dumps(entries),))
            conn.execute("INSERT INTO store_meta VALUES ('built_at', "
                         "'2026-10-31T04:40:00Z')")
        # the ledger names the cycle now running: never the age of the data served
        (tmp / "host").mkdir()
        with sqlite3.connect(tmp / "host" / "host.sqlite") as conn:
            conn.execute("CREATE TABLE source_state (source TEXT, configured INTEGER, "
                         "captured_at TEXT, stale_since TEXT, error TEXT, attempted_at TEXT)")
            conn.executemany("INSERT INTO source_state VALUES (?, 1, ?, NULL, NULL, ?)",
                             [(s, "2026-10-31T04:24:26Z", "2026-10-31T04:24:26Z")
                              for s in ("teams", "bc")])
            # failed on the attempt the served generation (built 04:40) was built from
            conn.execute("INSERT INTO source_state VALUES ('mc', 1, '2026-10-31T04:24:26Z', "
                         "'2026-10-31T04:30:00Z', 'capture_failed', '2026-10-31T04:24:26Z')")
            # the cycle that built the served data (04:40) started at 04:00
            conn.execute("CREATE TABLE cycle (cycle_id INTEGER, started_at TEXT)")
            conn.executemany("INSERT INTO cycle VALUES (?, ?)",
                             [(1, "2026-10-30T20:00:00Z"), (2, "2026-10-31T04:00:00Z"),
                              (3, "2026-10-31T05:00:00Z")])
            # failed only in the cycle still running (attempted after that build), its
            # last good capture inside the build's cycle: the data served is untouched
            # (vm15 scoreboard diagnosis, 2026-10-31)
            conn.execute("INSERT INTO source_state VALUES ('portal', 1, '2026-10-31T04:24:26Z', "
                         "'2026-10-31T05:10:00Z', 'capture_failed', '2026-10-31T05:05:00Z')")
            # failed in the build's cycle AND again in the running one: the ledger keeps
            # the last attempt only (after the build), but the last good capture predates
            # the build's cycle, so the served data rests on a failed capture: counted
            conn.execute("INSERT INTO source_state VALUES ('sharepoint', 1, "
                         "'2026-10-31T03:00:00Z', '2026-10-31T04:20:00Z', 'capture_failed', "
                         "'2026-10-31T05:06:00Z')")
        bounds = dict.fromkeys(("portal_api", "teams", "bc", "graph_mail", "sharepoint",
                                "mission_control"), 36 * 3600.0)
        backend = be.WorkspaceBackend(lambda session: None, data_label="t", state_dir=tmp,
                                      freshness_bounds=bounds, clock=lambda: now)
        self.addCleanup(backend.stop)
        home = {s.system: s for s in backend.freshness().sources}
        failed = ontology_tools.capture_failures(tmp / "host" / "host.sqlite",
                                                 "2026-10-31T04:40:00Z")
        self.assertEqual(failed, {"mission_control", "sharepoint"})
        # with no served build time to compare, every recorded failure counts
        self.assertEqual(ontology_tools.capture_failures(tmp / "host" / "host.sqlite"),
                         {"mission_control", "portal_api", "sharepoint"})
        # answers find the same ledger from their store's own path
        from types import SimpleNamespace
        conn = sqlite3.connect(store)
        self.addCleanup(conn.close)
        tools = SimpleNamespace(store=SimpleNamespace(conn=conn))
        self.assertEqual(ontology_tools.OntologyTools._capture_failures(
            tools, ontology_tools.OntologyTools._served_at(tools)), failed)
        answers = {e["source"]: ontology_tools.aged(e, bounds, now, failed)["stale"]
                   for e in entries if e["captured_at"]}
        # a failed or never-run capture is stale whatever its age; a partial one
        # (budgeted, continuing next cycle) is judged by age like any other
        self.assertEqual({"Portal": False, "Teams": True, "BC": True, "Email": True,
                          "File": True, "MC": True},
                         {k: v.stale for k, v in home.items()})
        self.assertEqual({k: v.capture for k, v in home.items()},
                         {"Portal": "complete", "Teams": "partial", "BC": "complete",
                          "Email": "never_captured", "File": "failed", "MC": "failed"})
        for family, system in be.STORE_SYSTEMS.items():   # Home and answers: one rule
            if family in answers:
                self.assertEqual(home[system].stale, answers[family], family)
        self.assertEqual(home["Portal"].as_of, "2026-10-30T15:08:15Z")
        self.assertEqual(home["Portal"].age_seconds, 51105)   # 14 h 11 min
        self.assertIsNone(home["Email"].as_of)   # no capture time: never said fresh
        # one data cut-off for every screen: the newest capture served, and a source read
        # over a day before it named beside it, never folded into it (demo-ready: Home
        # said 29 Oct from Teams while the late list said 1 Nov)
        self.assertEqual(backend.freshness().as_of, "2026-10-31T05:00:00Z")
        # the ledger above holds failed captures, so the stale-capture alarm (judged at
        # the backend's clock) closes the label
        alarm = "; captures not refreshed: mc, portal, sharepoint (oldest 2 h)"
        self.assertEqual(backend.freshness().label,
                         "Data as of 31 Oct 2026 1:00 AM ET; Teams to 29 Oct" + alarm)
        # with hybrid search on, a source whose searchable text ends over a day before its
        # capture says so (records after it exist but search cannot find them yet)
        with mock.patch.dict(os.environ, {"PLX_SEARCH": "hybrid"}), mock.patch(
                "operating.search.coverage.coverage",
                return_value={"Teams": "2026-10-27T22:00:00+00:00",
                              "Portal": "2026-10-30T15:00:00+00:00"}):
            fresh = backend.freshness()
        self.assertEqual(fresh.label, "Data as of 31 Oct 2026 1:00 AM ET; Teams to 29 Oct; "
                                      "searchable text: Teams to 27 Oct" + alarm)
        self.assertEqual({s.system: s.searchable_to for s in fresh.sources}["Teams"],
                         "2026-10-27T22:00:00+00:00")
        self.assertIsNone(backend.freshness().sources[0].searchable_to)   # search off
        # past the bound the same source is stale on Home too (fails closed)
        tight = be.WorkspaceBackend(lambda session: None, data_label="t", state_dir=tmp,
                                    freshness_bounds={"portal_api": 3400.0},
                                    clock=lambda: now)
        self.addCleanup(tight.stop)
        self.assertTrue({s.system: s for s in tight.freshness().sources}["Portal"].stale)

    def test_good_capture_after_the_served_build_does_not_excuse_a_failure(self):
        """The source fails and the served build is made; the source then captures well
        but that cycle's build fails or is skipped; the source fails again. The ledger's
        last good capture lies after the served build, so it never reached the served
        data: the failure counts and Home reads stale."""
        import sqlite3
        from datetime import datetime, timezone
        from operating import ontology_tools

        tmp = Path(tempfile.mkdtemp(prefix="home-fresh-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        now = datetime(2026, 10, 31, 8, 20, tzinfo=timezone.utc)
        store = tmp / "projection" / "ontology.sqlite"
        store.parent.mkdir()
        with sqlite3.connect(store) as conn:
            conn.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
            # served data: the last good Portal capture before cycle 1, young by age
            conn.execute("INSERT INTO store_meta VALUES ('freshness', ?)", (json.dumps([
                {"source": "portal_api", "captured_at": "2026-10-30T23:00:00Z",
                 "completeness": "complete", "stale": False}]),))
            conn.execute("INSERT INTO store_meta VALUES ('built_at', "
                         "'2026-10-31T00:40:00Z')")
        (tmp / "host").mkdir()
        ledger = tmp / "host" / "host.sqlite"
        with sqlite3.connect(ledger) as conn:
            conn.execute("CREATE TABLE source_state (source TEXT, configured INTEGER, "
                         "captured_at TEXT, stale_since TEXT, error TEXT, attempted_at TEXT)")
            conn.execute("CREATE TABLE cycle (cycle_id INTEGER, started_at TEXT)")
            # cycle 1 (00:00): Portal fails at 00:10, the served build is made at 00:40;
            # cycle 2 (04:00): Portal captures well at 04:10, the build fails/is skipped;
            # cycle 3 (08:00): Portal fails again at 08:05 (the ledger keeps this attempt)
            conn.executemany("INSERT INTO cycle VALUES (?, ?)",
                             [(1, "2026-10-31T00:00:00Z"), (2, "2026-10-31T04:00:00Z"),
                              (3, "2026-10-31T08:00:00Z")])
            conn.execute("INSERT INTO source_state VALUES ('portal', 1, '2026-10-31T04:10:00Z', "
                         "'2026-10-31T08:05:00Z', 'capture_failed', '2026-10-31T08:05:00Z')")
        self.assertEqual(ontology_tools.capture_failures(ledger, "2026-10-31T00:40:00Z"),
                         {"portal_api"})
        backend = be.WorkspaceBackend(lambda session: None, data_label="t", state_dir=tmp,
                                      freshness_bounds={"portal_api": 36 * 3600.0},
                                      clock=lambda: now)
        self.addCleanup(backend.stop)
        portal = {s.system: s for s in backend.freshness().sources}["Portal"]
        self.assertTrue(portal.stale)
        self.assertEqual(portal.capture, "failed")
        # the same good capture inside the served build's cycle (cycle 1 after the
        # failure, before the build) is what the served data holds: excused
        with sqlite3.connect(ledger) as conn:
            conn.execute("UPDATE source_state SET captured_at = '2026-10-31T00:30:00Z'")
        self.assertEqual(ontology_tools.capture_failures(ledger, "2026-10-31T00:40:00Z"),
                         frozenset())
        # at the build instant itself it is still the build's own capture: excused
        with sqlite3.connect(ledger) as conn:
            conn.execute("UPDATE source_state SET captured_at = '2026-10-31T00:40:00Z'")
        self.assertEqual(ontology_tools.capture_failures(ledger, "2026-10-31T00:40:00Z"),
                         frozenset())


class LateListCountTest(unittest.TestCase):
    """Home and the late-list answer read one list with one limit and state one count
    (demo rehearsal 2026-10-31: the answer said 12 "on your home page" while Home showed
    10). Past the limit the answer says how many more there are; nothing is doubled."""

    def test_home_and_the_answer_list_the_same_orders_and_count(self):
        from types import SimpleNamespace
        for total in (1, 10, 15, 60):
            with self.subTest(total=total):
                gaps = [{"object_id": f"PurchaseOrder:bc.purchase_orders:{i}",
                         "object_type": "PurchaseOrder", "days_late": 200 - i,
                         "summary": "An open purchase order is past its need date",
                         "citations": []} for i in range(total)]
                gaps += [dict(gaps[0])]   # an order read twice is counted once
                inbox = {"status": "read", "run_at": "2026-10-31T01:26:26Z", "gaps": gaps}
                labels = {g["object_id"]: str(106000 + n) for n, g in enumerate(gaps[:-1])}
                home, _ = be.attention_items(inbox, labels, suppliers={
                    gaps[0]["object_id"]: "DSM"})
                fake = SimpleNamespace(freshness=lambda _base: m.Freshness())
                answer = be.late_answer(fake, None, m.AskRequest(question="late pos?"),
                                        inbox, home, "c1", lambda: 0)
                shown = min(total, be.LATE_LIST_LIMIT)
                self.assertEqual(len({p.object_id for p in home}), shown)
                self.assertEqual(len(home), shown)
                self.assertEqual([s.object_id for s in answer.subjects], [
                    p.object_id for p in home][:be.MAX_FACTS])
                self.assertTrue(answer.direct_answer.startswith(
                    f"{total} purchase order"), answer.direct_answer)
                self.assertIn("106000 from DSM (200 days)", answer.direct_answer)
                if total <= be.LATE_LIST_LIMIT:
                    self.assertIn("All of them are listed below", answer.direct_answer)
                    self.assertIsNone(answer.list_total)
                else:
                    # "The first 12 are listed below": the rest are a "show all" away (GET /api/late)
                    self.assertIn(f"The first {shown} are listed below", answer.direct_answer)
                    self.assertEqual(answer.list_total, total)

    def test_no_late_order_is_said_only_when_the_late_rule_ran_over_the_data_served(self):
        """Live repro (gen 7, 2026-11-02): the inbox's late-PO findings were of the previous
        generation (held back, ``refreshing``) and the answer said "No purchase order you
        can see is late", against 23 late in the data. Behind or not run, the rule's list is
        unknown and the answer says so; run over this data, none late is said."""
        from types import SimpleNamespace
        fake = SimpleNamespace(freshness=lambda _base: m.Freshness())
        base = {"status": "read", "run_at": "2026-11-01T08:14:36Z", "gaps": []}
        for inbox, said in (
                ({**base, "refreshing": ["late_or_unconfirmed_po"],
                  "checked_on": "2026-11-01T08:14:36Z"},
                 "The late-order check is being re-run on the newest data (last checked "
                 "2026-11-01), so I can't list late purchase orders yet."),
                ({**base, "not_run": {"late_or_unconfirmed_po": "blocked"}},
                 "The late-order check did not run on the newest data, so I can't list "
                 "late purchase orders yet."),
                ({**base, "refreshing": ["late_order"]},
                 "No purchase order you can see is late as of 2026-11-01."),
                (base, "No purchase order you can see is late as of 2026-11-01.")):
            answer = be.late_answer(fake, None, m.AskRequest(question="late pos?"), inbox, [],
                                    "c1", lambda: 0)
            self.assertEqual(answer.direct_answer, said)
            self.assertEqual(answer.outcome, "answered" if said.startswith("No ")
                             else "not_found")

    def test_the_true_total_past_the_inbox_page(self):
        # The count stopped at the inbox's first page (100)
        import contextlib
        from types import SimpleNamespace
        gaps = [{"object_id": f"PurchaseOrder:bc.purchase_orders:{i}",
                 "object_type": "PurchaseOrder", "days_late": 500 - i,
                 "summary": "late", "citations": []} for i in range(150)]

        class View:
            def gap_inbox(self, *, limit=None):
                shown = gaps[:limit or 100]
                return {"status": "read", "gaps": shown, "more": len(shown) < len(gaps)}

            def _request(self):
                return contextlib.nullcontext()

            def _call(self, tool, arguments):
                return {"object": {"display": "PO"}, "values": []} \
                    if tool == "plx.object" else {"objects": []}

        inbox, labels, suppliers = be.WorkspaceBackend._attention(View())
        self.assertEqual(inbox["late_total"], 150)
        home, _ = be.attention_items(inbox, labels, suppliers=suppliers)
        fake = SimpleNamespace(freshness=lambda _base: m.Freshness())
        answer = be.late_answer(fake, None, m.AskRequest(question="late pos?"), inbox, home,
                                "c1", lambda: 0)
        self.assertTrue(answer.direct_answer.startswith("150 purchase orders"))
        self.assertIn("The first 12 are listed below", answer.direct_answer)
        self.assertEqual(answer.list_total, 150)


class LateWhileTheCheckReruns(unittest.TestCase):
    """Live (2026-11-11): "is anything late" answered "the late-order check is being
    re-run ... can't be listed yet" while the Late page listed the late orders. A check
    that is re-running or did not run on the newest data answers from its last completed
    result (the Late page's read, each order's sources decided for the reader now) with an
    "as of" line; with none either, the Ask goes on to a live read (the agent path)."""

    PO, HIDDEN = "PurchaseOrder:bc.purchase_orders:p1", "PurchaseOrder:bc.purchase_orders:p2"

    def backend(self, held_gaps, *, refused=()):
        import contextlib
        from types import MethodType, SimpleNamespace
        refreshing = {"status": "read", "run_at": "2026-11-11T08:00:00Z", "gaps": [],
                      "refreshing": ["late_or_unconfirmed_po"],
                      "checked_on": "2026-11-10T08:00:00Z"}

        class Access:
            def decide(self, handles):
                # p2's handle refused; any other handle allowed; an unknown one undecided
                return {h: ({"reason": "restricted"} if h in refused else None)
                        for h in handles if h.startswith("bc://")}

        view = SimpleNamespace(access=Access(), _request=contextlib.nullcontext,
                               _can_open=lambda handle: True,
                               gap_inbox=lambda limit=None: refreshing)
        fake = SimpleNamespace(
            _attention_cached=lambda _view: (dict(refreshing), {}, {}),
            test_named=frozenset, today=None, freshness=lambda _base=None: m.Freshness(),
            _source=be.WorkspaceBackend._source,
            _late_held=lambda _view: (refreshing, held_gaps, {self.PO: "PO 1001",
                                                              self.HIDDEN: "PO 1002"}, {}))
        for name in ("_late_fallback", "late_job"):
            setattr(fake, name, MethodType(getattr(be.WorkspaceBackend, name), fake))
        fake.reads = SimpleNamespace(run=lambda reader, job, timeout=None: job(view))
        return fake, view

    def gap(self, oid, handle, days):
        return {"object_id": oid, "object_type": "PurchaseOrder", "kind": "late_order",
                "days_late": days, "summary": "Past its need date", "citations": [handle]}

    def ask(self, fake):
        got = be.WorkspaceBackend._late_ask(fake, None, m.AskRequest(question="is anything late"),
                                            "c1", lambda: 0)
        try:
            while True:
                next(got)
        except StopIteration as done:
            return done.value

    def test_the_last_completed_result_answers_with_an_as_of_line(self):
        held = [self.gap(self.PO, "bc://x/purchaseOrders/p1@1", 30),
                self.gap(self.HIDDEN, "bc://x/purchaseOrders/p2@1", 20),
                self.gap("PurchaseOrder:bc.purchase_orders:p3", "mystery://p3@1", 10)]
        fake, _ = self.backend(held, refused={"bc://x/purchaseOrders/p2@1"})
        answer = self.ask(fake)
        self.assertEqual(answer.outcome, "answered")
        self.assertNotIn("can't", answer.direct_answer)
        self.assertTrue(answer.direct_answer.startswith(
            "1 purchase order you can see is late at the last completed late-order check "
            "(2026-11-10)."), answer.direct_answer)
        self.assertIn("re-running on newer data", answer.direct_answer)
        # the refused and the undecided order are never named or counted
        self.assertEqual([s.object_id for s in answer.subjects], [self.PO])
        self.assertNotIn("1002", answer.model_dump_json())

    def test_no_completed_result_goes_on_to_a_live_read(self):
        fake, _ = self.backend([])
        self.assertIsNone(self.ask(fake))

    def test_an_order_that_cites_nothing_is_not_shown(self):
        # All() over no citations let an uncited order through
        uncited = {**self.gap(self.HIDDEN, "x", 20), "citations": []}
        fake, view = self.backend([self.gap(self.PO, "bc://x/purchaseOrders/p1@1", 30),
                                   uncited])
        late, total = fake._late_fallback(view)
        self.assertEqual(total, 1)
        self.assertEqual([p.object_id for p in late], [self.PO])
        fake, view = self.backend([uncited])
        self.assertEqual(fake._late_fallback(view), ([], 0))


class ResumedAskHostSources(unittest.TestCase):
    """On resume a captured mail original or SharePoint file the
    store leaves to the reader's host (``item_refused``) was counted as open. It is read
    through the reader's host again; refused there, no host, or too many: withheld."""

    MAIL, FILE = "original://mail/m1@1", "sharepoint://site/doc1@1"

    def backend(self, opens, *, serves=True):
        import contextlib
        from types import MethodType, SimpleNamespace
        from operator_api.workspace.object_workspace import ITEM_REFUSED, _withheld
        reads = []

        def read_source(session, handle, offset):
            reads.append(handle)
            if handle not in opens:
                raise KeyError("citation_unresolvable_for_actor")
            return {"text": "x"}

        fake = SimpleNamespace(
            feedback_grant=lambda reader, handles: (frozenset(), {
                h: (_withheld(ITEM_REFUSED) if "://" in h and not h.startswith("bc://")
                    else None) for h in handles}),
            agent_ask=SimpleNamespace(serves=lambda session: serves,
                                      read_source=read_source),
            _original_reads=lambda reader: contextlib.nullcontext())
        for name in ("ask_withheld", "_host_opens"):
            setattr(fake, name, MethodType(getattr(be.WorkspaceBackend, name), fake))
        return fake, reads

    def test_a_host_source_is_read_again_through_the_readers_host(self):
        from types import SimpleNamespace
        reader = SimpleNamespace(key="staff:ana", session=None)
        fake, reads = self.backend({self.MAIL, self.FILE})
        self.assertIsNone(fake.ask_withheld(reader, ["bc://x/p1@1", self.MAIL, self.FILE]))
        self.assertEqual(reads, [self.MAIL, self.FILE])
        fake, _ = self.backend({self.MAIL})        # the site access was revoked
        self.assertEqual(fake.ask_withheld(reader, [self.MAIL, self.FILE]), "withheld")
        fake, _ = self.backend({self.MAIL, self.FILE}, serves=False)   # no host
        self.assertEqual(fake.ask_withheld(reader, [self.MAIL]), "withheld")
        many = [f"sharepoint://site/d{i}@1" for i in range(be.RESUME_HOST_CHECKS + 1)]
        fake, reads = self.backend(set(many))
        self.assertEqual(fake.ask_withheld(reader, many), "withheld")
        self.assertEqual(reads, [])


class OrdersAtRiskTests(unittest.TestCase):
    """Vince's "customer order -> which materials are late": the late list names the
    customer orders a late PO holds up, and "which customer orders are at risk from late
    POs" / "what's late for SO 470" answer from the reader's chain findings
    (``order_at_risk`` and ``po_impact`` gaps), each record of the chain cited. A chain end
    whose own gap the reader may not see is never named."""

    PO, PO2 = "PurchaseOrder:bc.purchase_orders:p1", "PurchaseOrder:bc.purchase_orders:p2"
    SO = "SalesOrder:portal.fm_order_entry:o1"
    CUSTOMER = "Customer:portal.customer:c1"

    def inbox(self, *, order_visible=True, with_ends=True):
        late = [{"object_id": po, "object_type": "PurchaseOrder", "kind": "late_order",
                 "days_late": days, "summary": "An open purchase order is past its need date",
                 "citations": [f"bc://x/purchaseOrders/{po[-2:]}@1"]}
                for po, days in ((self.PO, 30), (self.PO2, 9))]
        impact = {"object_id": self.PO, "object_type": "PurchaseOrder", "kind": "po_impact",
                  "summary": "affects 1 sales order, worst 59 days at risk",
                  "citations": ["bc://x/purchaseOrders/p1@1", "api://public.assembly_order/9@1"],
                  "event": {"days": 59, "orders_affected": 1,
                            **({"orders": [self.SO]} if with_ends else {})}}
        order = {"object_id": self.SO, "object_type": "SalesOrder", "kind": "order_at_risk",
                 "summary": "59 days at risk from 1 late purchase order",
                 "citations": ["bc://x/purchaseOrders/p1@1", "api://public.assembly_order/9@1",
                               "api://public.order_entry/470@1"],
                 "follow_up": {"why": "material 2026-11-02 against the required date "
                                      "2026-09-04."},
                 "event": {"days": 59, "orders_affected": 1,
                           **({"pos": [self.PO]} if with_ends else {})}}
        gaps = late + [impact] + ([order] if order_visible else [])
        return {"status": "read", "run_at": "2026-11-03T00:04:48Z", "gaps": gaps,
                "detectors": {"orders_at_risk": "complete"}}

    def view(self, found=()):
        import contextlib
        from types import SimpleNamespace
        names = {self.PO: "PO100099", self.PO2: "PO100120", self.SO: "470",
                 self.CUSTOMER: "Three Ships"}

        class View:
            access = SimpleNamespace(decide=lambda hs: {h: None for h in hs})

            def _request(self):
                return contextlib.nullcontext()

            def _can_open(self, handle):
                return True

            def _call(_self, tool, arguments):
                if tool == "plx.object":
                    return {"object": {"display": names.get(arguments["object_id"], "")}}
                if tool == "plx.find":
                    return {"candidates": [{"object": dict(c)} for c in found]}
                link = arguments["path"][0]["link_type"]
                ends = [self.CUSTOMER] if (link == be.CUSTOMER_ORDERS
                                           and arguments["from"] == self.SO) else []
                return {"objects": [{"object_id": o} for o in ends]}
        return View()

    def backend(self):
        from types import SimpleNamespace
        return SimpleNamespace(freshness=lambda _base: m.Freshness(),
                               _source=be.WorkspaceBackend._source)

    def test_the_late_list_names_the_customer_order_a_late_po_holds_up(self):
        inbox = self.inbox()
        home, _ = be.attention_items(inbox, {self.PO: "PO100099", self.PO2: "PO100120"})
        answer = be.late_answer(self.backend(), self.view(), m.AskRequest(question="late pos?"),
                                inbox, home, "c1", lambda: 0)
        texts = {f.text.split(":")[0]: f for f in answer.facts}
        self.assertIn("Holds up customer order 470 (59 days at risk).", texts["PO100099"].text)
        self.assertIn("api://public.assembly_order/9@1",
                      {s.handle for s in texts["PO100099"].sources})   # the chain, cited
        self.assertNotIn("Holds up", texts["PO100120"].text)
        # the order's own gap not shown to this reader: the order is never named
        hidden = self.inbox(order_visible=False)
        answer = be.late_answer(self.backend(), self.view(), m.AskRequest(question="late pos?"),
                                hidden, home, "c1", lambda: 0)
        self.assertNotIn("470", " ".join(f.text for f in answer.facts))
        # an older finding with no chain ends: its count, never a guessed order
        older = self.inbox(with_ends=False)
        answer = be.late_answer(self.backend(), self.view(), m.AskRequest(question="late pos?"),
                                older, home, "c1", lambda: 0)
        po_fact = next(f for f in answer.facts if f.text.startswith("PO100099"))
        self.assertIn("past its need date. Holds up 1 customer order (worst 59 days at "
                      "risk).", po_fact.text)
        self.assertNotIn("470", po_fact.text)

    def test_which_orders_are_at_risk_and_whats_late_for_one_order_or_customer(self):
        for question in ("which customer orders are at risk from late POs?",
                         "any sales orders held up by late materials"):
            self.assertEqual(be.ask_shortcut(m.AskRequest(question=question)), "at_risk")
        self.assertEqual(be.ask_shortcut(m.AskRequest(question="which POs are late?")),
                         "late_list")
        self.assertEqual(be.late_for_target("what's late for SO 470?"), "SO 470")
        self.assertEqual(be.late_for_target("which materials are late for customer Three "
                                            "Ships"), "Three Ships")
        self.assertIsNone(be.late_for_target("is Three Ships profitable"))
        ask = lambda q, view: be.at_risk_answer(self.backend(), view, m.AskRequest(
            question=q), self.inbox(), "c1", lambda: 0)
        answer = ask("which customer orders are at risk from late POs?", self.view())
        self.assertTrue(answer.direct_answer.startswith(
            "1 customer order you can see is at risk from late purchase orders as of "
            "2026-11-03: Customer order 470 (Three Ships) is 59 days at risk from late PO "
            "PO100099."),
                        answer.direct_answer)
        self.assertEqual([s.handle for s in answer.facts[0].sources][-1],
                         "api://public.order_entry/470@1")
        # by the order's or customer's own label (no search), else by the reader's plx.find
        by_label = ask("what's late for SO 470", self.view())
        by_name = ask("what's late for customer three ships?", self.view())
        by_order = ask("what's late for S-ORD470", self.view(found=[
            {"object_id": self.SO, "object_type": "SalesOrder", "display": "S-ORD470"}]))
        by_customer = ask("what's late for Three Ships Inc", self.view(found=[
            {"object_id": self.CUSTOMER, "object_type": "Customer",
             "display": "Three Ships Inc"}]))
        for got in (by_label, by_name, by_order, by_customer):
            self.assertEqual([s.object_id for s in got.subjects], [self.SO])
        # a customer with no order at risk: the agent walks its materials, never "none"
        self.assertIsNone(ask("what's late for Other Co", self.view(found=[
            {"object_id": "Customer:portal.customer:c9", "object_type": "Customer",
             "display": "Other Co"}])))
        none = be.at_risk_answer(self.backend(), self.view(), m.AskRequest(
            question="which customer orders are at risk from late POs?"),
            {**self.inbox(order_visible=False), "detectors": {"orders_at_risk": "truncated"}},
            "c1", lambda: 0)
        self.assertEqual(none.direct_answer, "No customer order you can see is at risk from a "
                         "late purchase order as of 2026-11-03 in the orders the check covered.")
        self.assertEqual(len(none.not_found), 1)


class SuggestionRankingTests(unittest.TestCase):
    """The typeahead's ranking and collapsing (``backend.suggestions``) over rows as
    ``search.entities`` returns them within the reader's scope: (words matched, object id,
    type, name, aliases, exact, code, prefix, word, similarity)."""

    @staticmethod
    def row(words, oid, kind, name, alias=(), exact=False, code=False, prefix=False,
            word=False, sim=0.5):
        return (words, oid, kind, name, list(alias), exact, code, prefix, word, sim)

    def test_copies_collapse_two_customers_stay_two_and_whole_names_rank_first(self):
        r = self.row
        rows = [
            # "three ships": the whole name before a word of it ("SHIPPING - 1" x2)
            r(1, 'CustomerLocation:portal.app_customer_location:{"id":"a"}',
              "customer_location", "SHIPPING - 1", ["1"], prefix=False, word=True),
            r(1, 'CustomerLocation:portal.app_customer_location:{"id":"b"}',
              "customer_location", "SHIPPING - 1", ["1"], word=True),
            r(2, 'Customer:bc.customers:{"id":"ts"}', "customer",
              "Three Ships (NIU BODY INC.)", ["NIUB01", "Customer NIUB01"], prefix=True),
            # Diversified: three copies of DIVE11 (one by a recorded same record), and a
            # second customer of the same name
            r(1, 'Customer:portal.app_customer:{"id":"fm-cust-89"}', "customer",
              "Diversified", ["DIVE11", "Customer DIVE11"], exact=True),
            r(1, 'Customer:bc.customers:{"id":"d1"}', "customer", "Diversified",
              ["DIVE11", "Customer DIVE11"], exact=True),
            r(1, 'Customer:portal.fm_customer:{"id":89}', "customer", "Diversified",
              [], exact=True),
            r(1, 'Customer:bc.customers:{"id":"d2"}', "customer", "Diversified",
              ["C01100", "Customer C01100"], exact=True),
            # a near spelling ranks below every match of the words as typed
            r(3, 'Supplier:bc.vendors:{"id":"lt"}', "supplier", "LIPOTRUE", sim=0.61),
            r(1, 'Product:bc.items:{"id":"g"}', "item", "Glycerin Hint: Glycerin",
              ["R-0238", "Item R-0238"], prefix=True),
        ]
        same = {frozenset(('Customer:portal.app_customer:{"id":"fm-cust-89"}',
                           'Customer:portal.fm_customer:{"id":89}'))}
        asked = []

        def same_record(ids):
            asked.append(set(ids))
            return same

        out = be.suggestions(rows, 20, same_record=same_record)
        labels = [(s.label, s.sublabel) for s in out]
        self.assertEqual(out[0].label, "Three Ships (NIU BODY INC.)")
        diversified = [s for s in out if s.label == "Diversified"]
        self.assertEqual(len(diversified), 2, labels)
        self.assertEqual(diversified[0].object_id,
                         'Customer:portal.app_customer:{"id":"fm-cust-89"}')   # Portal first
        self.assertIn("DIVE11", diversified[0].sublabel)
        # the merged row names the copy it absorbed; the other customer of that name has none
        self.assertEqual(diversified[0].same_as, ['Customer:bc.customers:{"id":"d1"}',
                                                  'Customer:portal.fm_customer:{"id":89}'])
        self.assertEqual(diversified[1].same_as, [])
        self.assertIn("BC and Portal", diversified[0].sublabel)
        self.assertIn("C01100", diversified[1].sublabel)
        self.assertEqual(len([s for s in out if s.label == "SHIPPING - 1"]), 2)  # 2 places
        self.assertEqual(out[-1].label, "LIPOTRUE")
        self.assertEqual(out[-1].match, "fuzzy")
        # an item opens as the record it is (the Glycerin 404: /object/item/Product:...)
        glycerin = next(s for s in out if s.label.startswith("Glycerin"))
        self.assertEqual((glycerin.type, glycerin.open_url.split("/")[2]),
                         ("Product", "Product"))
        # only rows the reader was shown are asked about
        self.assertTrue(all(ids <= {row[1] for row in rows} for ids in asked))

    def test_an_order_says_its_supplier_by_name_and_never_joins_by_its_code(self):
        # search showed "PO PO100040 · Purchase order · V08380 · BC": the label doubled at
        # its source and the vendor's code where its name belongs
        r = self.row
        rows = [r(1, 'PurchaseOrder:bc.purchase_orders:{"id":"p1"}', "purchase_order",
                  "PO PO9001", ["PO9001", "V0001", "Supplier V0001"], exact=True),
                r(1, 'PurchaseOrder:bc.purchase_orders:{"id":"p2"}', "purchase_order",
                  "PO PO9001", ["PO9001", "V0001", "Supplier V0001"], exact=True),
                r(1, 'Supplier:bc.vendors:{"id":"v1"}', "supplier", "Synthetic Oils Ltd",
                  ["V0001", "Supplier V0001"], prefix=True)]
        out = be.suggestions(rows, 10, same_record=lambda ids: set(),
                             parties={"V0001": "Synthetic Oils Ltd"})
        orders = [s for s in out if s.type == "PurchaseOrder"]
        # two orders of one supplier sharing a number are two records, not copies
        self.assertEqual(len(orders), 2)
        self.assertEqual({(s.label, s.sublabel) for s in orders}, {(
            "PO9001", "Purchase order · Synthetic Oils Ltd (V0001) · BC")})
        # the supplier itself keeps its own code; a name the reader may not read: the code
        supplier = next(s for s in out if s.type == "Supplier")
        self.assertEqual(supplier.sublabel, "Supplier · V0001 · BC")
        out = be.suggestions(rows[:1], 10, same_record=lambda ids: set(), parties={})
        self.assertEqual(out[0].sublabel, "Purchase order · V0001 · BC")
        from operating.chunking.headers import id_label
        self.assertEqual((id_label("petra_po", "PO9001"), id_label("so_number", "OR77"),
                          id_label("customer_po", "10011")),
                         ("PO9001", "SO OR77", "Customer PO 10011"))

    def test_phrases_are_the_trailing_words_longest_first(self):
        self.assertEqual([p for _, p in be.suggest_phrases("What did we tell Three Ships")],
                         ["we tell three ships", "tell three ships", "three ships", "ships"])
        self.assertEqual(be.suggest_phrases("a"), [])

    def test_a_kept_result_is_read_again_once_a_shown_source_is_withheld(self):
        cache = be.ReaderCache()

        class View:
            closed: set = set()

            def _request(self):
                return contextlib.nullcontext()

            class access:
                @staticmethod
                def decide(handles):
                    return {h: ({"state": "withheld"} if h in View.closed else None)
                            for h in handles}

        reads = []

        def read():
            reads.append(1)
            return {"handles": ["bc://x", "graph-mail://m"]}

        key = ("reader-a", "gen-1")
        for _ in range(2):
            cache.kept(View(), key, read, lambda v: v["handles"])
        self.assertEqual(len(reads), 1)          # reused for the same reader and state
        View.closed = {"graph-mail://m"}         # a mailbox ACL revoked meanwhile
        cache.kept(View(), key, read, lambda v: v["handles"])
        self.assertEqual(len(reads), 2)          # never served from the cache
        cache.kept(View(), None, read, lambda v: v["handles"])
        self.assertEqual(len(reads), 3)          # no key, never kept

    def test_parallel_requests_for_a_cold_key_read_it_once(self):
        cache, reads, started = be.ReaderCache(), [], threading.Event()

        def read():
            reads.append(1)
            started.set()
            time.sleep(0.3)
            return ("late list",)
        got = []
        threads = [threading.Thread(target=lambda: got.append(cache.once(("r", "late"), read)))
                   for _ in range(4)]
        threads[0].start()
        started.wait(5)
        for thread in threads[1:]:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual((len(reads), got), (1, [("late list",)] * 4))

    def test_a_kept_label_is_read_again_once_a_delta_cycle_lands(self):
        stamp, calls = ["cycle-1"], []

        class View:
            class access:
                @staticmethod
                def overlay_stamp():
                    return stamp[0]

            @staticmethod
            def _call(name, arguments):
                calls.append(name)
                return {"object": {"display": f"PO 1 ({stamp[0]})"}}
        cards = be.ReaderCache()
        with mock.patch.object(be, "page_key", lambda view, what: ("reader", what)):
            self.assertEqual(be.card_words(View(), "PurchaseOrder:1", cards=cards)[0],
                             "PO 1 (cycle-1)")
            be.card_words(View(), "PurchaseOrder:1", cards=cards)
            self.assertEqual(len(calls), 1)               # kept within one cycle
            stamp[0] = "cycle-2"                           # the next delta cycle
            self.assertEqual(be.card_words(View(), "PurchaseOrder:1", cards=cards)[0],
                             "PO 1 (cycle-2)")
            self.assertEqual(len(calls), 2)

    def test_a_seen_mark_keeps_every_key_but_that_readers_briefing(self):
        import sqlite3
        tmp = Path(tempfile.mkdtemp(prefix="inbox-stamp-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        store = tmp / "ontology.sqlite"
        inbox = proactive_engine.inbox_path(store)
        with contextlib.closing(sqlite3.connect(inbox)) as conn, conn:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO meta VALUES ('schema', ?)", (proactive_engine.INBOX_SCHEMA,))
            conn.execute("CREATE TABLE gaps (gap_id TEXT PRIMARY KEY, state TEXT)")
            conn.execute("INSERT INTO gaps VALUES ('g1', 'open')")
        view = SimpleNamespace(store_status=SimpleNamespace(store_path=store),
                               actor=SimpleNamespace(tenant="plx", principals=["user:a"]))
        before, visit = be.inbox_stamp(view), be.last_visit_of(view)
        time.sleep(0.01)
        proactive_engine.record_visit(inbox, proactive_engine.reader_key(view.actor))
        self.assertEqual(be.inbox_stamp(view), before)        # Home, lists, cards: kept
        self.assertNotEqual(be.last_visit_of(view), visit)    # the briefing: read again
        time.sleep(0.01)
        with contextlib.closing(sqlite3.connect(inbox)) as conn, conn:
            conn.execute("UPDATE gaps SET state = 'resolved'")   # a run: every key is new
        self.assertNotEqual(be.inbox_stamp(view), before)


class LateRowAndSameNameTests(unittest.TestCase):
    """The late list's row carries the order's due date and its total only as the inbox
    shows them to the reader; a customer page names the other customers of its name the
    reader's search shows, never a copy of itself, and nothing when it cannot tell."""

    def test_a_late_row_states_its_due_date_and_only_a_shown_total(self):
        def gap(oid, amount=None, summary="An open purchase order is past its need date "
                "2026-06-24"):
            return {"object_id": f"PurchaseOrder:bc.purchase_orders:{{\"id\":\"{oid}\"}}",
                    "object_type": "PurchaseOrder", "days_late": 130, "summary": summary,
                    "citations": [], **({"event": {"amount": amount}} if amount else {})}
        inbox = {"gaps": [
            gap("a", {"value": "1993.004", "currency": "CAD", "basis": "total_cost"}),
            gap("b"),   # the engine dropped a total this reader may not open
            gap("c", {"value": "9.00", "currency": "dollars"}),   # no currency code
            gap("d", {"value": "5000000", "currency": "USD", "suspect": "10x its history"},
                summary="An open purchase order needed by 2026-02-30 has no supplier "
                        "confirmation"),
        ]}
        rows, _ = be.attention_items(inbox, {})
        self.assertEqual([(r.due_date, r.amount and (r.amount.amount, r.amount.currency),
                           r.amount_suspect) for r in rows], [
            ("2026-06-24", ("1993.00", "CAD"), None),
            ("2026-06-24", None, None),
            ("2026-06-24", None, None),
            (None, ("5000000", "USD"), "10x its history")])   # no 30 February
        # "no supplier confirmation found" only when none is linked at all; a late order
        # with a link (firm, a lead, or one this reader can't see) says nothing here
        for g, kind in zip(inbox["gaps"], ("late_po", "late_po", "conflicting_x",
                                           "unconfirmed_po")):
            g["kind"] = kind
        rows, _ = be.attention_items(inbox, {})
        self.assertEqual([r.confirmation for r in rows], [None, None, None, "none_found"])


    PORTAL = 'Customer:portal.app_customer:{"id":"fm-cust-89"}'
    BC_COPY = 'Customer:bc.customers:{"id":"d1"}'
    OTHER = 'Customer:bc.customers:{"id":"d2"}'

    def same_name(self, opened, shown, also_in=()):
        r = SuggestionRankingTests.row
        every = {
            self.PORTAL: r(1, self.PORTAL, "customer", "Diversified",
                           ["DIVE11", "Customer DIVE11"], exact=True),
            self.BC_COPY: r(1, self.BC_COPY, "customer", "Diversified",
                            ["DIVE11", "Customer DIVE11"], exact=True),
            self.OTHER: r(1, self.OTHER, "customer", "Diversified",
                          ["C01100", "Customer C01100"], exact=True),
            "loc": r(1, 'CustomerLocation:portal.x:{"id":"l"}', "customer_location",
                     "Diversified", exact=True)}
        backend = be.WorkspaceBackend(lambda s: None, data_label="test")
        asked = []

        def pg_suggest(reader, q, limit, *, members=None):
            asked.append(q)
            return be.suggestions([every[k] for k in shown], limit, members=members,
                                  same_record=lambda ids: set())
        backend._pg_suggest = pg_suggest
        page = m.ObjectPage(object_id=opened, type="Customer", label="Diversified",
                            summary="", status=m.ObjectStatus(state="ok", text="ok"), freshness=m.Freshness(),
                            customer=m.CustomerSummary(also_in=[
                                m.ObjectRef(object_id=o, type="Customer", label="x",
                                            open_url="/x") for o in also_in]))
        out = backend.same_name(SimpleNamespace(), page)
        self.assertEqual(asked, ["Diversified"])
        return [(o.object_id, o.label) for o in out]

    def test_the_other_customer_of_the_name_is_named_never_a_copy(self):
        everyone = [self.PORTAL, self.BC_COPY, self.OTHER, "loc"]
        other = [(self.OTHER, "Diversified (C01100 · BC)")]   # the type word once, on the page
        self.assertEqual(self.same_name(self.PORTAL, everyone), other)
        # opened at the BC copy (one record by its code): the Portal row is not "another"
        self.assertEqual(self.same_name(self.BC_COPY, everyone), other)
        self.assertEqual(self.same_name(self.OTHER, everyone),
                         [(self.PORTAL, "Diversified (DIVE11 · BC and Portal)")])
        # a twin recorded on the page is never listed as another customer
        self.assertEqual(self.same_name(self.PORTAL, [self.PORTAL, self.OTHER],
                                        also_in=[self.OTHER]), [])

    def test_a_reader_who_cannot_see_the_other_or_the_page_record_hears_nothing(self):
        # the restricted reader's search shows only this customer: nothing is said
        self.assertEqual(self.same_name(self.PORTAL, [self.PORTAL, self.BC_COPY]), [])
        # the search does not show the page's own record: undecidable, nothing said
        self.assertEqual(self.same_name(self.PORTAL, [self.OTHER]), [])


class CustomerSummaryTests(unittest.TestCase):
    """``customer_summary`` over a reader's tool calls: the order count is the set's own
    size, never the rows one capped read returned; a refused sum keeps the rows unpriced
    and says why."""

    def view(self, *, total: Optional[int], sums_refused: bool = False,
             states: Optional[dict] = None):
        orders = [{"object_id": f"SalesOrder:portal.app_sales_order:{{\"id\":\"{i}\"}}",
                   "display": f"OR{i:04d}"} for i in range(50)]
        calls = []

        class View:
            access = SimpleNamespace(decide=lambda hs: {h: None for h in hs})

            def _request(self):
                return contextlib.nullcontext()

            def _can_open(self, handle):
                return True

            def _call(self, tool, args):
                calls.append((tool, args))
                if tool == "plx.around":
                    if "same_record_as" in json.dumps(args):
                        return {"objects": []}
                    # one page of 50 with more behind it (the live customer had 844)
                    first = 50 if args.get("cursor") else 0
                    page = [{**o, "object_id": o["object_id"] + f"#{first}"} for o in orders]
                    return {"set": "set:x", "objects": page,
                            "completeness": {"next_cursor": None if args.get("cursor")
                                             else "c1", "total": None}}
                if tool == "plx.aggregate":
                    measure, groups = args["measure"], args.get("group_by") or []
                    if measure.get("op") == "count" and not groups:
                        return ({"error": {"code": "result_too_large"}} if total is None
                                else {"groups": [{"key": {}, "value": str(total)}]})
                    if measure.get("op") == "sum" and sums_refused:
                        return {"error": {"code": "withheld_marking"}}
                    if groups == ["status"]:
                        return {"groups": [{"key": {"status": k}, "value": str(v)}
                                           for k, v in (states or {"Open": 3}).items()]}
                    return {"groups": [{"key": {"order_date": "2026-10-0%d" % (i + 1),
                                                "order_number": f"OR{i:04d}",
                                                "status": "Open"},
                                        "value": "10.00", "currency": "CAD",
                                        "inputs_citations": []} for i in range(3)]}
                return {}
        return View(), calls

    def summary(self, view):
        backend = SimpleNamespace(today=lambda: date(2026, 11, 1),
                                  _source=lambda v, h: m.Source(system="Portal", handle=h,
                                                                label="x"))
        return be.customer_summary(backend, view, "Customer:portal.app_customer:{\"id\":\"c\"}")[0]

    def test_the_count_is_the_whole_set_or_said_as_at_least(self):
        whole = self.summary(self.view(total=844)[0])
        self.assertEqual((whole.orders_total, whole.orders_at_least), (844, None))
        unknown = self.summary(self.view(total=None)[0])
        self.assertEqual((unknown.orders_total, unknown.orders_at_least), (None, 100))

    def test_open_orders_are_counted_by_the_one_status_definition(self):
        # API-013: "partially shipped" held "shipped" and was counted closed
        out = self.summary(self.view(total=60, states={
            "Partially Shipped": 4, "Open": 3, "Shipped": 9, "CANCELLED": 2,
            "Confirmed": 1, "On hold (credit)": 5})[0])
        self.assertEqual(out.open_orders, 8)
        self.assertTrue(any("5 orders hold a status not read as open or closed" in n
                            for n in out.not_established), out.not_established)

    def test_a_portal_draft_is_no_open_order(self):
        # BC's API calls an unreleased order Draft (open); the page groups the Portal's
        # status, where DRAFT is a draft and never an open order
        out = self.summary(self.view(total=60, states={"DRAFT": 7, "Open": 3,
                                                       "In Review": 2})[0])
        self.assertEqual(out.open_orders, 3)
        self.assertTrue(any("2 orders hold a status not read as open or closed" in n
                            for n in out.not_established), out.not_established)

    def test_a_reader_without_the_amounts_keeps_the_orders_unpriced(self):
        out = self.summary(self.view(total=60, sums_refused=True)[0])
        self.assertEqual(out.revenue, [])
        self.assertTrue(out.recent_orders)
        self.assertTrue(all(r.amount is None for r in out.recent_orders))
        self.assertTrue(any("not shown to you" in n for n in out.not_established),
                        out.not_established)
        self.assertFalse(any("mixed" in n for n in out.not_established))


class KnownReaderWarmTests(unittest.TestCase):
    """The read-ahead at start follows only an unexpired, unrevoked real session, keeps no
    name, and its identity serves and audits nothing."""

    def test_only_live_sessions_are_followed_and_nothing_names_the_person(self):
        tmp = Path(tempfile.mkdtemp(prefix="app-known-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        backend = be.WorkspaceBackend(lambda s: None, data_label="test", data_dir=tmp)
        backend.warmer = be.Worker(lambda s: None)   # queued, never run here
        now = int(time.time())
        people = {"29e05178-0bd1-49b9-be6e-b01be1975f70": ("a" * 64, now + 3600),
                  "00000000-0000-4000-8000-000000000001": ("b" * 64, now + 3600),  # revoked
                  "00000000-0000-4000-8000-000000000002": ("c" * 64, now - 1)}     # expired
        for oid, (ref, exp) in people.items():
            session = SimpleNamespace(principals=frozenset({f"employee:{oid}"}),
                                      employee=SimpleNamespace(object_id=oid), email=None,
                                      session_ref=ref, expires_at=exp)
            backend.remember_reader(be.Reader(session, "dev", "Vince Example"))
        path = tmp / be.KNOWN_READERS_FILE
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("Vince", path.read_text())
        self.assertEqual(len(backend.known_readers()), 2)   # the expired one is gone
        backend.revoked = lambda ref: ref == "b" * 64
        made = []

        def identity_for(oid, exp):
            made.append((oid, exp))
            return SimpleNamespace(principals=frozenset({f"employee:{oid}"}), email=None)
        backend.identity_for = identity_for
        self.assertEqual(backend.warm_known(), 1)
        self.assertEqual(made, [("29e05178-0bd1-49b9-be6e-b01be1975f70", now + 3600)])
        # Today first on the read workers (Home, the briefing and its groups, the
        # suggestion cards, the late list, the upcoming meetings), then the pages and
        # last week's review on the warm worker
        self.assertEqual((backend.reads.jobs.qsize(), backend.warmer.jobs.qsize()), (5, 2))
        # signed out: never read ahead for again
        backend.forget_reader(be.Reader(SimpleNamespace(
            principals=frozenset(), employee=SimpleNamespace(
                object_id="29e05178-0bd1-49b9-be6e-b01be1975f70")), "dev", ""))
        self.assertEqual(len(backend.known_readers()), 1)

    def test_a_read_ahead_identity_serves_and_audits_nothing(self):
        from operating import session_identity as si
        from operating.employee_permissions import EmployeePrincipal
        from operator_api.workspace.session_journal import SessionJournal
        principal = EmployeePrincipal("11111111-1111-4111-8111-111111111111",
                                      "29e05178-0bd1-49b9-be6e-b01be1975f70")
        warm = si.SessionIdentity(
            tenant="plx", channel=si.CHANNEL_CONSOLE, author_kind=si.AUTHOR_HUMAN,
            proof=si.PROOF_WARM, employee=principal,
            principals=frozenset({principal.principal}), expires_at=int(time.time()) + 60,
            identities=frozenset({principal.object_id}))
        with self.assertRaises(si.SessionIdentityRefused):
            warm.describe()
        with self.assertRaises(si.SessionIdentityRefused):
            warm.intent_identity(warm.actor)
        tmp = Path(tempfile.mkdtemp(prefix="app-warm-journal-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        with self.assertRaises(PermissionError):
            SessionJournal.for_state_dir(tmp).record(method="GET", path="/api/home",
                                                     status=200, session=warm)
        from fastapi.testclient import TestClient as Client
        from operator_api.app import create_app
        from operator_api.auth import DevAuth
        client = Client(create_app(be.WorkspaceBackend(lambda s: None, data_label="t"),
                                   DevAuth(lambda: warm), secure_cookie=False))
        client.post("/api/auth/dev-login", json={})
        self.assertEqual(client.get("/api/home").status_code, 401)


class WarmYieldsTests(unittest.TestCase):
    """The read-ahead never delays a person: it starts a page only after live reads went
    quiet, and a live request never waits on a page the read-ahead is reading."""

    def test_a_queued_live_request_delays_the_next_warm_page(self):
        backend = be.WorkspaceBackend(lambda s: None, data_label="test")
        reader = SimpleNamespace(session=SimpleNamespace(principals=frozenset({"x"})))
        backend.reads.submit(reader, lambda view: None)   # queued, not yet run
        done = threading.Event()
        with mock.patch.object(be, "WARM_PAUSE_SECONDS", 0.0):
            waiter = threading.Thread(target=lambda: (backend.wait_idle(), done.set()))
            waiter.start()
            self.assertFalse(done.wait(1.5))              # the warm page waits
            backend.reads.jobs.get_nowait()               # the live request is served
            backend.reads.last_done = time.monotonic() - be.WARM_IDLE_SECONDS
            self.assertTrue(done.wait(2.0))
            waiter.join()

    def test_a_live_request_never_waits_on_read_ahead_work(self):
        cache = be.ReaderCache()
        inside, release, live_done = threading.Event(), threading.Event(), threading.Event()

        def warm():
            with cache.flight(("page", "PO-1")):
                inside.set()
                release.wait(5)
        warming = threading.Thread(target=warm, name=be.WARM_THREAD)
        warming.start()
        inside.wait(2)
        live = threading.Thread(target=lambda: (cache.flight(("page", "PO-1")).__enter__(),
                                                live_done.set()))
        live.start()
        self.assertTrue(live_done.wait(1.0))   # read itself, did not wait
        release.set()
        warming.join()
        live.join()

    def test_no_revocation_check_wired_means_no_read_ahead(self):
        backend = be.WorkspaceBackend(lambda s: None, data_label="test")
        self.assertTrue(backend.revoked("a" * 64))   # fails closed


class SearchDefaultTests(unittest.TestCase):
    """The app's agents search with hybrid search unless PLX_SEARCH says otherwise; the
    fixture mode keeps the word index (no search tables)."""

    def test_hybrid_unless_switched_off_and_never_over_fixtures(self):
        env = {}
        self.assertEqual(be.default_search(env, "entra", "postgresql:///plx"), "hybrid")
        self.assertEqual(env, {"PLX_SEARCH": "hybrid", "PLX_PG_DSN": "postgresql:///plx"})
        off = {"PLX_SEARCH": "keyword", "PLX_PG_DSN": "postgresql:///other"}
        self.assertEqual(be.default_search(off, "dev", "postgresql:///plx"), "keyword")
        self.assertEqual(off["PLX_PG_DSN"], "postgresql:///other")
        self.assertEqual(be.default_search({}, "mock", None), "keyword")
        from operating.search import plug
        with mock.patch.dict(os.environ, off, clear=False):
            self.assertFalse(plug.enabled())


class RecordTruthTests(unittest.TestCase):
    """A CEO walk of gen 7: a PO whose receipts (Apr 2025) precede its order date (Mar 2026)
    showed both without a word, and a Portal customer's page showed no margin while Ask
    stated the Business Central customer's. Stubbed reads; evidence label: isolated-fixture."""

    PO = "bc://production/c/purchaseOrders/p1@g"
    RECEIPT = "bc://production/c/purchaseReceipts/r1@g"

    def backend(self):
        return SimpleNamespace(_source=lambda view, handle, **kw: m.Source(
            system="BC", handle=handle, label="BC", fields=kw.get("fields")))

    def view(self, refused=()):
        return SimpleNamespace(access=SimpleNamespace(decide=lambda handles: {
            h: ("withheld" if h in refused else None) for h in handles}))

    def page(self, *receipts):
        return {"timeline": {"events": [
            {"event_type": "Receipt.posted", "at": day, "citation": {"l1_handle": h}}
            for day, h in receipts] + [
            {"event_type": "PurchaseOrder.issued", "at": "2020-01-01",
             "citation": {"l1_handle": self.PO}}]}}

    def test_a_receipt_before_the_order_date_is_a_cited_conflict_never_a_choice(self):
        values = [{"property": "order_date", "value": "2099-03-28",
                   "citation": {"l1_handle": self.PO, "field_path": "row.orderDate"}}]
        page = self.page(("2098-04-22", self.RECEIPT), ("2098-04-14", self.RECEIPT),
                         ("2099-05-01", self.RECEIPT))
        (found,) = be.received_before_ordered(self.backend(), self.view(), values, page, "PO9")
        self.assertEqual(found.field, "order date vs receipt date")
        self.assertEqual([(v.value, v.source.handle, v.trusted) for v in found.values],
                         [("2099-03-28", self.PO, False), ("2098-04-14", self.RECEIPT, False)])
        self.assertIn("2 receipt(s)", found.values[0].why)
        # receipts after the order, no order date, or a receipt the reader may not open:
        # nothing said
        self.assertEqual(be.received_before_ordered(
            self.backend(), self.view(), values, self.page(("2099-05-01", self.RECEIPT)),
            "PO9"), [])
        self.assertEqual(be.received_before_ordered(self.backend(), self.view(), [], page,
                                                    "PO9"), [])
        self.assertEqual(be.received_before_ordered(
            self.backend(), self.view(refused={self.RECEIPT}), values, page, "PO9"), [])

    def test_a_portal_customers_margin_is_its_bc_customers_as_ask_finds_it(self):
        portal = 'Customer:portal.app_customer:{"id":"c1"}'
        bc1, bc2 = 'Customer:bc.customers:{"id":"b1"}', 'Customer:bc.customers:{"id":"b2"}'
        twins: dict = {}
        named: list = []

        def call(name, args):
            if name == "plx.around" and "same_record_as" in args["path"][0]["link_type"]:
                return {"objects": [{"object_id": o} for o in twins.get(
                    (args["from"], args["path"][0]["direction"]), ())]}
            if name == "plx.around":   # every card has orders
                return {"objects": [{"object_id": "SalesOrder:bc.sales_orders:{}"}]}
            if name == "plx.find":
                return {"candidates": [{"object": {"object_id": o, "object_type": "Customer",
                                                   "display": "Synthetic Pet INC"}}
                                       for o in named]}
            raise AssertionError(name)
        view = SimpleNamespace(_call=call)
        page = SimpleNamespace(object_id=portal, type="Customer", label="Synthetic Pet INC")
        with mock.patch.object(be, "customer_names",
                               lambda view, c: {"synthetic pet"}):
            self.assertIsNone(be.bc_customer_of(view, page))   # no BC customer: no margin
            named[:] = [bc1, bc2]
            # API-018: two BC cards of the exact name that no identity link joins are
            # never merged: no margin, each card a choice
            from operating import why_trail
            for linked in (lambda call, ids: False,
                           mock.Mock(side_effect=RuntimeError("link read failed"))):
                with mock.patch.object(why_trail, "same_records", linked):
                    apart: list = []
                    self.assertIsNone(be.bc_customer_of(view, page, apart))
                    self.assertEqual([c["object_id"] for c in apart], [bc1, bc2])
                    self.assertIsNone(be.page_profitability(None, view, page, apart=[]))
            # linked as one record: read together
            with mock.patch.object(why_trail, "same_records", lambda call, ids: True):
                apart = []
                self.assertEqual(be.bc_customer_of(view, page, apart), (bc1, [bc2]))
                self.assertEqual(apart, [])
            twins[(portal, "reverse")] = [bc2]   # a verified twin wins over the name
            self.assertEqual(be.bc_customer_of(view, page), (bc2, []))
        with mock.patch.object(be, "customer_names", lambda view, c: {"synthetic pets"}):
            twins.clear()
            self.assertIsNone(be.bc_customer_of(view, page))   # a near name: never taken
