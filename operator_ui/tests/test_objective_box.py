"""Home's objective box (``POST /api/objective``, ``operator_ui.objective_box`` over
``operating.objective_runner``): a free-form objective with no object id, run by the
employee agent loop for the console's reader, on the case console's fixture world
(``case_fixtures.seed``, projected as ``case_console --seed-fixtures`` does) with scripted
planners in place of a model; and a real browser driving the box on the fixture console
(``--ask --scripted-ask``). Evidence label: isolated-fixture / injected-transport; no
network, nothing live.
"""

from __future__ import annotations

import contextlib
from datetime import date
import hashlib
import io
import json
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from operating import objective_runner, session_identity
from operating.action_path import ActionPath
from operating.case_runtime import CaseRuntime
from operator_ui import case_console, objective_box
from operator_ui import object_workspace as objects
from operator_ui.case_workspace import CaseWorkspace
from operator_ui.server import make_server
from operator_ui.tests import case_fixtures
from operator_ui.tests.test_console_walkthrough import (BROWSERS, NODE_PATHS, ROOT, SKIP,
                                                        _clean_env)

OPS = frozenset({case_fixtures.OPERATIONS, case_fixtures.QUALITY, case_fixtures.PURCHASING})
OUTSIDER = frozenset({"staff:finance"})
AS_OF = date(2026, 10, 19)
MO = "public.app_manufacturing_order"
FM_AO = "public.assembly_order"


def _ids_and_handles(value, out):
    if isinstance(value, dict):
        for item in value.values():
            _ids_and_handles(item, out)
    elif isinstance(value, list):
        for item in value:
            _ids_and_handles(item, out)
    elif isinstance(value, str) and (value.startswith(objects.L1_SCHEMES)
                                     or re.match(r"^[A-Z][A-Za-z]+:portal\.", value)):
        out.add(value)
    return out


def _on_record(values):
    """An answer stating the card's first cited value by reference (structured claim)."""
    value = next(v for v in values if v.get("citation") and v.get("value") is not None)
    plain = value["value"]
    plain = plain["untrusted_text"] if isinstance(plain, dict) else plain
    return {"text": "The purchase order is on record as {po}.",
            "claims": [{"id": "po", "value": plain,
                        "citation": value["citation"]["l1_handle"],
                        "field": value["citation"]["field_path"]}]}


def _tree(state: Path) -> dict[str, str]:
    """Every store file under the state directory but the session journal, by digest."""
    return {str(path.relative_to(state)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(state.rglob("*")) if path.is_file()
            and "journal" not in path.name and not path.name.endswith(("-wal", "-shm"))}


class ObjectiveBoxHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="objective-box-"))
        cls.state = cls.tmp / "state"
        case_fixtures.seed(cls.state, "plx")
        cls.store = cls.state / case_console.ONTOLOGY_FILE
        objects.project_mirror(cls.state, cls.store)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def serve(self, principals, planner):
        """The console for one reader, served as the fixture console serves it."""
        ready: Queue = Queue(maxsize=1)

        def run():
            try:
                runtime = CaseRuntime(self.state, client=None, tenant="plx",
                                      audience=self.state / case_fixtures.MIRROR_AUDIENCE_FILE)
                session = session_identity.fixture_session(
                    "plx", principals, channel=session_identity.CHANNEL_CONSOLE)
                view = case_console.object_workspace_for(
                    runtime, self.store, session.actor, state_dir=self.state,
                    action_path=ActionPath(self.state / case_fixtures.ACTIONS_FILE,
                                           runtime.cases), planner=planner, session=session,
                    evidence=case_console.evidence_for(case_fixtures.marker(self.state)))
                server = make_server(case_workspace=CaseWorkspace(runtime, session.actor),
                                     session=session, object_workspace=view)
                ready.put(server)
                server.serve_forever(poll_interval=0.05)
                server.server_close()
                view.close()
                runtime.close()
            except BaseException as error:  # surfaced to the test thread
                ready.put(error)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        server = ready.get(timeout=60)
        if isinstance(server, BaseException):
            raise server
        self.addCleanup(lambda: (server.shutdown(), thread.join(timeout=10)))
        port, token, csrf = server.server_address[1], server.operator_access_token, []

        def request(path, body=None):
            from http.client import HTTPConnection
            headers = {"Authorization": "Bearer " + token}
            data = None
            if body is not None:
                if not csrf:
                    csrf.append(request("/api/session")[1]["csrf_token"])
                headers.update({"Content-Type": "application/json", "X-Operator-Token": csrf[0]})
                data = json.dumps(body).encode("utf-8")
            conn = HTTPConnection("127.0.0.1", port, timeout=60)
            conn.request("GET" if body is None else "POST", path, body=data, headers=headers)
            response = conn.getresponse()
            answer = json.loads(response.read())
            conn.close()
            return response.status, answer
        return request

    def post(self, request, body):
        with mock.patch.object(objective_box, "_today", return_value=AS_OF):
            return request("/api/objective", body)

    def test_an_objective_with_no_id_reaches_its_root_and_answers_cited(self):
        questions = []

        def planner(request):
            # Read only what the box named; answer citing the card's first value.
            questions.append(request["question"])
            roots = [line.split(": ", 1)[1] for line in request["question"].splitlines()
                     if line.startswith(objective_runner.ROOT_LINE)]
            if not request["transcript"]:
                return {"action": "call", "calls": [
                    {"tool": "plx.object", "arguments": {"object_id": oid}} for oid in roots]
                    or [{"tool": "plx.find", "arguments": {"query": case_fixtures.PO_NUMBER,
                                                           "identifier_kind": "petra_po"}}]}, None
            result = request["transcript"][0]["observations"][0]["result"]
            if "candidates" in result:
                # Named by nothing: the agent finds the record itself, then reads it.
                if len(request["transcript"]) == 1:
                    return {"action": "call", "calls": [{"tool": "plx.object", "arguments": {
                        "object_id": result["candidates"][0]["object"]["object_id"]}}]}, None
                result = request["transcript"][1]["observations"][0]["result"]
            return {"action": "answer", "unknowns": ["not in the record: why it is late"],
                    **_on_record(result["values"])}, None

        request = self.serve(OPS, planner)
        status, home = request("/api/home")
        self.assertEqual((status, home["objective"]["served"]), (200, True))
        status, found = request("/api/objects?q=" + case_fixtures.PO_NUMBER)
        po = next(c["object"]["object_id"] for c in found["find"]["candidates"]
                  if c["object"]["object_type"] == "PurchaseOrder")

        status, body = self.post(request, {
            "objective": f"What do we know about {case_fixtures.PO_NUMBER}, and is it late?"})
        self.assertEqual(status, 200, body)
        self.assertEqual([(r["object_id"], r["found_by"]) for r in body["roots"]],
                         [(po, "identifier")])
        self.assertIn(objective_runner.ROOT_LINE + po, questions[0])
        self.assertEqual(body["answer"]["outcome"], "answered")
        [claim] = body["answer"]["claims"]
        self.assertTrue(all(cite["openable"] for cite in claim["citations"]), claim)
        self.assertEqual([gap["subject"] for gap in body["gaps"]],
                         ["not in the record: why it is late"])
        self.assertEqual(body["follow_ups"][0]["proposal"]["object_id"], po)
        self.assertTrue(body["read_only"])

        # No identifier at all: the agent finds the record, and the box keeps it as a root
        # only once the reader's own read holds the record the claim cites.
        status, body = self.post(request, {"objective": "Which purchase order is late?"})
        self.assertEqual(status, 200, body)
        self.assertIn("No identifier in the objective named a record", questions[-1])
        self.assertEqual([(r["object_id"], r["found_by"]) for r in body["roots"]],
                         [(po, "agent")])

    def test_scenario_marks_stale_and_undated_inputs_and_only_proposes_follow_ups(self):
        def planner(request):
            # Read every candidate the box listed; quote the order's quantity (its record
            # states no date) and the FileMaker order's status with its manufacture date.
            if not request["transcript"]:
                line = next(line for line in request["question"].splitlines()
                            if "none chosen: " in line)
                ids = line.split("none chosen: ", 1)[1].split(". Read", 1)[0].split("; ")
                return {"action": "call", "calls": [
                    {"tool": "plx.object", "arguments": {"object_id": oid}} for oid in ids]}, None
            cards = [o["result"] for o in request["transcript"][0]["observations"]]
            handle = {relation: next(v["citation"]["l1_handle"] for card in cards
                                     for v in card["values"]
                                     if v["citation"]["l1_handle"].startswith(
                                         f"api://{relation}/"))
                      for relation in (MO, FM_AO)}
            return {"action": "answer",
                    "unknowns": ["not in the record: an air freight quote"],
                    "claims": [
                        {"text": "The manufacturing order quotes its planned quantity.",
                         "citations": [handle[MO]], "value_claims": [
                             {"value": "1509", "citation": handle[MO], "field": "row.quantity",
                              "role": "planned"}]},
                        {"text": "The FileMaker assembly order is Realized, manufactured "
                                 "2026-01-05.", "citations": [handle[FM_AO]], "value_claims": [
                             {"value": "Realized", "citation": handle[FM_AO],
                              "field": "row.status"},
                             {"value": "2026-01-05", "citation": handle[FM_AO],
                              "field": "row.datemanuf"}]}]}, None

        request = self.serve(OPS, planner)
        before = _tree(self.state)
        code = case_fixtures.AO_CODES[0]
        status, body = self.post(request, {"objective": f"Air-ship the {code} order by Nov 15",
                                           "mode": "scenario"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["target"], {"date": "2026-11-15", "source": "objective_text"})
        [named] = body["ambiguous"]
        self.assertIsNone(named["chosen"])
        self.assertEqual(len(named["candidates"]), 2)
        inputs = {item["field"]: item for item in body["scenario"]["inputs"]}
        self.assertEqual({field: (item["state"], item["source_date"])
                          for field, item in inputs.items()},
                         {"row.quantity": ("unknown", None),
                          "row.status": ("stale", "2026-01-05")})
        self.assertEqual(inputs["row.status"]["age_days"], (AS_OF - date(2026, 1, 5)).days)
        confirm = " | ".join(item["what"] for item in body["scenario"]["must_confirm"])
        for what in ("row.quantity = 1509", "row.status = Realized", f'"{code}" means',
                     "the target date", "air freight quote"):
            self.assertIn(what, confirm)
        proposed = body["follow_ups"]
        self.assertEqual({item["status"] for item in proposed},
                         {objective_runner.FOLLOW_UP_STATUS})
        self.assertEqual({item["proposal"]["state"] for item in proposed}, {"stale", "unknown"})
        self.assertTrue(all(item["proposal"]["who_can_answer"] for item in proposed))
        # Running the objective recorded nothing; proposing one is the person's control.
        self.assertEqual(_tree(self.state), before)
        status, recorded = request("/api/object-follow-up", proposed[0]["proposal"])
        self.assertEqual(status, 200, recorded)

    def test_a_bad_request_an_unserved_console_and_an_outsider_fail_closed(self):
        asked = []

        def planner(request):
            asked.append(request["question"])
            return {"action": "answer", "claims": [], "unknowns": []}, None

        request = self.serve(OPS, planner)
        for body in ({"objective": "x", "object_id": "y"}, {"objective": " "},
                     {"objective": "x", "mode": "predict"},
                     {"objective": "x", "deadline": "Nov 15"}, ["x"]):
            with self.subTest(body=body):
                self.assertEqual(self.post(request, body)[0], 400)
        self.assertEqual(asked, [])
        status, body = self.serve(OPS, None)("/api/objective", {"objective": "x"})
        self.assertEqual((status, body["error"]), (501, "objective_not_served"))

        # A reader the purchase order is not granted to: the box names no record of it,
        # and neither the planner's question nor the answer holds its id or handle.
        request = self.serve(OUTSIDER, planner)
        status, body = self.post(request, {
            "objective": f"Air-ship {case_fixtures.PO_NUMBER} by 2026-11-15",
            "mode": "scenario"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["roots"], body["ambiguous"], body["follow_ups"]), ([], [], []))
        self.assertEqual(_ids_and_handles(body, set()), set())
        self.assertNotIn("PurchaseOrder:", asked[-1])
        self.assertEqual(body["answer"]["outcome"], "abstained")


class ObjectiveBoxLiveCheckTest(unittest.TestCase):
    """``workspace.objective_box`` held to its spec on the probe's own output: blocked
    without a reader before any read, and a pass on the fixture world (injected
    transport, a scripted planner: never live evidence)."""

    SPEC = json.loads((ROOT / "devloop/live-checks/workspace.objective_box.json")
                      .read_text("utf-8"))

    def run_probe(self, **kwargs):
        from scripts import eval_objective_box as probe
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(probe.shutil, "which", return_value="/bin/claude"):
            code = probe.main([], **kwargs)
        return code, json.loads(out.getvalue())

    def test_the_probe_blocks_without_a_reader_and_passes_on_the_fixture_world(self):
        from devloop import live_verify
        self.assertEqual(live_verify.validate_spec(self.SPEC), [])
        code, document = self.run_probe(environ={})
        self.assertEqual((code, document["blocked"]), (0, "no_employee_identity"))
        self.assertEqual(self.SPEC["blocked_causes"][document["blocked"]], "later_ask")
        self.assertEqual(document["objective_box"]["ran"], 0)
        self.assertTrue(live_verify.check_expectations(self.SPEC, code, document))

        tmp = Path(tempfile.mkdtemp(prefix="objective-box-probe-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        state = tmp / "state"
        case_fixtures.seed(state, "plx")
        objects.project_mirror(state, state / case_console.ONTOLOGY_FILE)

        def planner(request):
            # The objectives name nothing: find a record, read it, cite it.
            done = request["transcript"]
            if not done:
                return {"action": "call", "calls": [{"tool": "plx.find", "arguments": {
                    "query": case_fixtures.PO_NUMBER}}]}, None
            last = done[-1]["observations"][0]["result"]
            if "candidates" in last:
                return {"action": "call", "calls": [{"tool": "plx.object", "arguments": {
                    "object_id": last["candidates"][0]["object"]["object_id"]}}]}, None
            return {"action": "answer", "unknowns": ["not in the record: a lead time"],
                    **_on_record(last["values"])}, None

        def make_workspace():
            runtime = CaseRuntime(state, client=None, tenant="plx",
                                  audience=state / case_fixtures.MIRROR_AUDIENCE_FILE)
            actor = session_identity.fixture_session(
                "plx", OPS, channel=session_identity.CHANNEL_CONSOLE).actor
            return runtime, case_console.object_workspace_for(
                runtime, state / case_console.ONTOLOGY_FILE, actor, state_dir=state,
                planner=planner)
        code, document = self.run_probe(environ={}, make_workspace=make_workspace)
        self.assertEqual(live_verify.check_expectations(self.SPEC, code, document), [])
        box = document["objective_box"]
        self.assertEqual((box["ran"], box["cited"]), (3, 3))
        self.assertEqual([item["mode"] for item in box["items"]],
                         ["investigate", "scenario", "investigate"])
        # Counts and enums only: no objective, claim text, id or handle leaves the run.
        self.assertEqual(_ids_and_handles(document, set()), set())
        self.assertNotIn("purchase order is on record", json.dumps(document))


class ObjectiveScenarioDatesTest(unittest.TestCase):
    """A scenario input is dated by its quote or document date only: a record touched
    yesterday (modified stamp) or with a passed event date does not make an old quote
    current; conflicting or missing qualifying dates make it unknown."""

    def scenario(self, dates):
        handle = "api://quotes/row/1"
        card = {"object": {"object_id": "Quote:portal.1", "object_type": "Quote"},
                "values": [{"value": when, "value_type": kind,
                            "citation": {"l1_handle": handle, "field_path": field}}
                           for field, (when, kind) in dates.items()]}
        claims = [{"text": "The supplier quoted 0.42 per bottle.", "citations": [handle],
                   "value_claims": [{"value": "0.42", "citation": handle,
                                     "field": "row.unit_price", "role": "price"}]}]
        [item] = objective_runner._inputs(claims, {"Quote:portal.1": card}, AS_OF, None)
        out = {"as_of": AS_OF.isoformat(), "ambiguous": [],
               "target": {"date": None, "source": "request"}, "loop": {"unknowns": []}}
        return item, objective_runner._must_confirm(out, [item])

    def test_an_old_quote_on_a_recently_modified_record_is_stale_and_must_be_confirmed(self):
        item, confirm = self.scenario({
            "row.quote_date": ("2026-03-02", "date"),
            "row.updated_at": ("2026-10-18T03:00:00Z", "timestamp"),
            "row.shipped_at": ("2026-10-17T09:00:00Z", "timestamp"),
            "row.expected_date": ("2026-10-16", "date")})
        self.assertEqual((item["state"], item["source_date"], item["source_date_field"]),
                         ("stale", "2026-03-02", "row.quote_date"))
        self.assertEqual([entry["what"] for entry in confirm], ["row.unit_price = 0.42"])
        self.assertIn("2026-03-02", confirm[0]["why"])
        # Only a modified stamp: no date says when it was quoted.
        item, confirm = self.scenario({"row.updated_at": ("2026-10-18", "date")})
        self.assertEqual((item["state"], item["source_date"]), ("unknown", None))
        self.assertEqual(len(confirm), 1)
        # Two qualifying dates that disagree: unknown, both listed.
        item, _ = self.scenario({"row.quote_date": ("2026-03-02", "date"),
                                 "row.document_date": ("2026-10-10", "date")})
        self.assertEqual(item["state"], "unknown")
        self.assertEqual(item["conflicting_dates"],
                         [{"field": "row.quote_date", "date": "2026-03-02"},
                          {"field": "row.document_date", "date": "2026-10-10"}])


class ObjectiveBoundsTest(unittest.TestCase):
    def test_sixty_named_records_stay_within_the_lookup_and_question_bounds(self):
        # Probe kept: dropping root lines to fit the loop's question once never ended.
        for count in (1, 10, 60):
            with self.subTest(count=count):
                looked = []

                def find(arguments):
                    looked.append(arguments["query"])
                    return {"status": "found", "candidates": [{"object": {
                        "object_id": "PurchaseOrder:portal.x:" + "k" * 600 + arguments["query"],
                        "object_type": "PurchaseOrder"}}]}
                objective = " ".join(f"PO{5000 + index}" for index in range(count))
                found = objective_runner.find_roots(objective, find)
                self.assertEqual(len(looked), min(count, objective_runner.MAX_MENTIONS))
                self.assertEqual(len(found["roots"]), len(looked))
                self.assertEqual(len(found["unfound"]), int(count > objective_runner.MAX_MENTIONS))
                question = objective_runner.frame(objective, "scenario", found, AS_OF, None)
                self.assertLessEqual(len(question), objective_runner.MAX_QUESTION)
                self.assertIn(objective_runner.SCENARIO_NOTE, question)
                self.assertEqual("(more records named; not listed)" in question, count > 1)


@unittest.skipIf(SKIP is not None, f"browser objective box skipped: {SKIP}")
class ObjectiveBoxBrowserTest(unittest.TestCase):
    """The fixture console (``--ask --scripted-ask``) in headless Chromium: Home shows the
    objective box first; a scenario run shows the cited answer, the quoted input with its
    state, what a person must confirm and a follow-up the person can propose."""

    DRIVE = r"""
const {chromium} = require("playwright");
(async () => {
  const browser = await chromium.launch({headless: true});
  const page = await (await browser.newContext({viewport: {width: 1280, height: 900}})).newPage();
  await page.goto(process.env.CONSOLE_URL);
  await page.waitForSelector("[data-home=objective] textarea", {timeout: 60000});
  const order = await page.$$eval("[data-home]", nodes => nodes.map(node => node.dataset.home));
  await page.fill("[data-home=objective] textarea", process.env.OBJECTIVE);
  await page.selectOption("[data-home=objective] select", "scenario");
  await page.click("[data-home=objective] button[type=submit]");
  await page.waitForSelector("[data-objective=result]", {timeout: 120000});
  const text = await page.$eval("[data-objective=result]", node => node.innerText);
  const chips = await page.$$eval("[data-objective=result] .case-chip", nodes => nodes.map(node => node.className));
  const posted = page.waitForResponse(response => response.url().endsWith("/api/object-follow-up"));
  await page.click("[data-objective=result] >> text=Propose follow-up");
  const response = await posted;
  const notice = await page.$eval("#notice", node => node.textContent);
  console.log(JSON.stringify({order, text, chips, proposed: response.status(), notice}));
  await browser.close();
})().catch(error => { console.error(String(error && error.stack || error)); process.exit(1); });
"""

    def test_home_objective_box_runs_a_scenario_and_proposes_a_follow_up(self):
        temp = tempfile.TemporaryDirectory(prefix="objective-box-browser-")
        self.addCleanup(temp.cleanup)
        env = {**_clean_env(), "PYTHONPATH": str(ROOT)}
        proc = subprocess.Popen(
            [sys.executable, "-m", "operator_ui.tests.case_fixtures", "--state-dir",
             str(Path(temp.name) / "state"), "--fixture", "--seed-fixtures", "--principal",
             "staff:operations", "--port", "0", "--ask", "--scripted-ask"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def stop():
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=20)
            proc.stdout.close()
        self.addCleanup(stop)
        lines: Queue = Queue()
        threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout],
                         daemon=True).start()
        url, seen = None, []
        while url is None:
            try:
                line = lines.get(timeout=300)
            except Empty:
                raise AssertionError("the fixture console printed no URL") from None
            seen.append(line)
            found = re.search(r"Operator console: (http://127\.0\.0\.1:\d+/#access=\S+)", line)
            if found:
                url = found[1]
            elif proc.poll() is not None and lines.empty():
                raise AssertionError("the fixture console exited: " + "".join(seen)[-2000:])
        script = Path(temp.name) / "drive.js"
        script.write_text(self.DRIVE, encoding="utf-8")
        code = case_fixtures.AO_CODES[0]
        driven = subprocess.run(
            ["node", str(script)], capture_output=True, text=True, timeout=600,
            env={**_clean_env(), "NODE_PATH": __import__("os").pathsep.join(NODE_PATHS),
                 "PLAYWRIGHT_BROWSERS_PATH": BROWSERS, "CONSOLE_URL": url,
                 "OBJECTIVE": f"Air-ship the {code} order by Nov 15"})
        token = url.split("#access=", 1)[1]
        self.assertEqual(driven.returncode, 0, driven.stderr.replace(token, "<token>")[-3000:])
        seen = json.loads(driven.stdout)
        self.assertEqual(seen["order"][0], "objective")
        for words in (f"identifier: {code}", "Inputs the records quote",
                      "date unknown", "A person must confirm", f"which record \"{code}\" means",
                      "freight quote", "Proposed follow-ups"):
            self.assertIn(words.lower(), seen["text"].lower())
        self.assertIn("case-chip", seen["chips"])
        self.assertEqual(seen["proposed"], 200)
        self.assertIn("Follow-up proposed", seen["notice"])


if __name__ == "__main__":
    unittest.main()
