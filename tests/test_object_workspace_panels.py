"""Object workspace console panels: joined panels, the SharePoint item gate, one object
view build per request, and object views that fail to open.

Split from ``tests/test_object_workspace_http.py`` (NW-16) so each part fits the per-module
limit; the fixture world and evidence label are that module's: isolated-fixture /
injected-transport, no network, nothing live.
"""

from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
from queue import Queue
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import quote
from ontology.core import Actor
from operating.case_runtime import CaseRuntime
from operator_api.workspace import case_console
from operator_api.workspace import object_workspace as objects
from operating import explanations
from operating import ontology_tools as ot
from operating import session_identity
from operator_api.workspace.case_workspace import CaseWorkspace
from operator_api.tests.console_harness import make_server


def _cite(handle, field, digest="a"):
    return {"l1_handle": handle, "field_path": field, "digest": digest * 64,
            "lineage": "gold_etl_from_fm"}


class JoinedPanelsTest(unittest.TestCase):
    """The join over contract-shaped tool answers (stub tool table, no store)."""

    ROOT = 'Header:portal.sample_header:{"id":7}'
    OTHER = 'Party:bc.sample_party:{"id":3}'
    H1 = "api://public.sample_header/%7B%22id%22%3A7%7D@g1-3"
    H1B = "api://public.sample_header/%7B%22id%22%3A7%7D@g1-9"
    MAIL = "graph-mail://box@example.test/m1@c4"

    def table(self, documents=None):
        calls = []

        def tool(name, answer):
            def handler(arguments, actor):
                calls.append((name, arguments, actor))
                return answer(arguments) if callable(answer) else answer
            return {"handler": handler}
        card = {"object": {"object_id": self.ROOT, "object_type": "Header", "display": None,
                           "lineage": "gold_etl_from_fm"},
                "values": [{"property": "number", "value_type": "identifier", "value": "H-7",
                            "citation": _cite(self.H1, "row.number"), "markings": []}],
                "conflicts": [{"kind": "value_disagreement", "subject": "due", "values": [
                    {"property": "due", "value_type": "date", "value": "2099-01-05",
                     "citation": _cite(self.H1, "row.due"), "markings": []},
                    {"property": "due", "value_type": "date", "value": "2099-01-09",
                     "citation": _cite(self.H1B, "row.due", "b"), "markings": []}]}],
                "identity": [], "gaps": [],
                "linked_counts": [{"link_type": "Header.ordered_from", "direction": "forward",
                                   "count": 1}],
                "withheld": {"properties": 1, "objects": None, "reason": "marking"},
                "completeness": {"complete": True, "truncated": False, "returned": 1},
                "meta": {}}
        around = {"set": "set:abcdefgh1", "hops": [],
                  "objects": [{"object_id": self.OTHER, "object_type": "Party",
                               "display": None, "lineage": "bc_sample"}],
                  "edges": [{"link_id": "L1", "link_type": "Header.ordered_from",
                             "from": self.ROOT, "to": self.OTHER, "tier": "lead",
                             "reason": [{"feature": "pair", "agrees": True,
                                         "citations": [_cite(self.MAIL, "body", "c")]}],
                             "why_not": []}],
                  "gaps": [], "completeness": {"complete": True, "truncated": False,
                                               "returned": 1}}

        def timeline(arguments):
            if arguments["from"] == self.ROOT:
                return {"events": [{"event_id": "e1", "event_type": "Header.created",
                                    "time_basis": "source_business_date", "time": "2099-01-02",
                                    "objects": [{"object_id": self.ROOT,
                                                 "qualifier": "subject"}],
                                    "citation": _cite(self.H1, "row.created")}],
                        "gaps": [], "completeness": {"complete": True, "truncated": False,
                                                     "returned": 1}}
            return {"events": [], "gaps": [],
                    "withheld": {"properties": None, "objects": 2, "reason": "audience"},
                    "completeness": {"complete": True, "truncated": False, "returned": 0}}
        table = {"plx.find": tool("plx.find", {}), "plx.object": tool("plx.object", card),
                 "plx.around": tool("plx.around", around),
                 "plx.timeline": tool("plx.timeline", timeline)}
        if documents is not None:
            table[objects.DOCUMENTS_TOOL] = tool(objects.DOCUMENTS_TOOL, documents)
        return table, calls

    def open(self, **kwargs):
        table, calls = self.table(**kwargs)
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"employee:oid-1"})),
                                       sources=object())
        return view.workspace(self.ROOT), calls

    def test_originals_list_every_cited_capture_and_withhold_an_ungranted_scheme(self):
        data, _ = self.open()
        by_handle = {item["l1_handle"]: item for item in data["originals"]["items"]}
        self.assertEqual(set(by_handle), {self.H1, self.H1B, self.MAIL})
        self.assertEqual({key: data["originals"][key] for key in ("total", "truncated")},
                         {"total": 3, "truncated": False})
        first = by_handle[self.H1]
        self.assertEqual((first["record"], first["version"]),
                         (objects.record_of(self.H1), "g1-3"))
        self.assertEqual(first["sha256"], ["a" * 64])
        self.assertEqual(first["cited_by"], ["value number", "event Header.created",
                                             "conflict due"])
        self.assertEqual(first["fields"], ["row.created", "row.due", "row.number"])
        self.assertEqual(first["properties"], ["number"])
        self.assertEqual((first["source"], first["lineages"]),
                         ("Portal API", ["gold_etl_from_fm"]))
        self.assertTrue(first["openable"])
        # The same record at a later capture is its own version, never merged away.
        self.assertEqual(by_handle[self.H1B]["record"], first["record"])
        self.assertEqual(by_handle[self.H1B]["version"], "g1-9")
        # A link reason citing mail: listed, with its digest, withheld with the reason.
        mail = by_handle[self.MAIL]
        self.assertEqual(mail["cited_by"], ["link Header.ordered_from"])
        self.assertFalse(mail["openable"])
        self.assertEqual(mail["withheld"], {"state": "withheld",
                                            "reason": objects.NO_HOST_GRANT})
        self.assertEqual(mail["sha256"], ["c" * 64])
        self.assertEqual(mail["source"], "mail")

    def test_source_system_comes_from_the_scheme_not_the_first_citation(self):
        # A neighbour display origin (no lineage) is the first citation of the capture;
        # a later link reason states the lineage. Neither decides the source system.
        table, _ = self.table()
        around = table["plx.around"]["handler"]({}, None)
        around["objects"][0]["display"] = {"untrusted_text": "Vendor",
                                           "origin": {"citation": self.MAIL,
                                                      "field_path": "subject"}}
        around["edges"][0]["reason"][0]["citations"][0]["lineage"] = "graph_mail_capture"
        table["plx.around"] = {"handler": lambda arguments, actor: around}
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"employee:oid-1"})))
        [mail] = [item for item in view.workspace(self.ROOT)["originals"]["items"]
                  if item["l1_handle"] == self.MAIL]
        self.assertEqual(mail["cited_by"], ["display of linked Party",
                                            "link Header.ordered_from"])
        self.assertEqual((mail["source"], mail["lineages"]), ("mail", ["graph_mail_capture"]))

    def many_events(self, n):
        """A timeline answer citing ``n`` distinct api captures plus one bc capture."""
        events = [{"event_id": f"e{i}", "event_type": "Line.received",
                   "time_basis": "source_business_date", "time": "2099-01-02",
                   "objects": [{"object_id": self.ROOT, "qualifier": "subject"}],
                   "citation": _cite(f"api://public.sample_line/{i:04d}@g1-1", "row.qty")}
                  for i in range(n)]
        events.append({"event_id": "eb", "event_type": "Bc.posted",
                       "time_basis": "source_business_date", "time": "2099-01-03",
                       "objects": [{"object_id": self.ROOT, "qualifier": "subject"}],
                       "citation": _cite("bc://sample/entry/1@c1", "row.amount")})
        return {"events": events, "gaps": [],
                "completeness": {"complete": True, "truncated": False,
                                 "returned": len(events)}}

    def test_originals_past_the_cap_say_so_and_keep_every_withheld_capture(self):
        table, _ = self.table()
        # The timeline keeps MAX_EVENTS events, so card values carry the rest.
        answer = self.many_events(190)
        table["plx.timeline"] = {"handler": lambda arguments, actor: answer}
        card = table["plx.object"]["handler"]({}, None)
        card["values"] += [{"property": f"line{i}", "value_type": "quantity", "value": i,
                            "citation": _cite(f"api://public.sample_extra/{i:04d}@g1-1",
                                              "row.qty"), "markings": []}
                           for i in range(60)]
        table["plx.object"] = {"handler": lambda arguments, actor: card}
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"employee:oid-1"})))
        originals = view.workspace(self.ROOT)["originals"]
        items = {item["l1_handle"]: item for item in originals["items"]}
        # 190 event + 60 value captures, 1 bc, H1, H1B (card) and MAIL (link reason).
        self.assertEqual((originals["total"], originals["truncated"], len(items)),
                         (254, True, objects.MAX_ORIGINALS))
        # The withheld captures sort after api:// and would be the first cut; they stay.
        self.assertIn(self.MAIL, items)
        self.assertIn("bc://sample/entry/1@c1", items)
        self.assertEqual(originals["by_scheme"],
                         {"api": {"total": 252, "shown": 198},
                          "bc": {"total": 1, "shown": 1},
                          "graph-mail": {"total": 1, "shown": 1}})
        self.assertEqual(originals["withheld"], {"total": 2, "shown": 2})
        self.assertEqual(sum(c["shown"] for c in originals["by_scheme"].values()), len(items))

    def test_many_withheld_captures_are_bounded_and_never_crowd_out_openable_ones(self):
        table, _ = self.table()
        around = table["plx.around"]["handler"]({}, None)
        around["edges"][0]["reason"][0]["citations"] += [
            _cite(f"bc://sample/entry/{i:04d}@c1", "row.amount") for i in range(400)]
        table["plx.around"] = {"handler": lambda arguments, actor: around}
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"employee:oid-1"})),
                                       sources=object())
        originals = view.workspace(self.ROOT)["originals"]
        items = {item["l1_handle"]: item for item in originals["items"]}
        # 400 bc + MAIL withheld; H1 (card value) and H1B (conflict) openable.
        self.assertEqual((originals["total"], originals["truncated"], len(items)),
                         (403, True, objects.MAX_ORIGINALS))
        self.assertTrue(items[self.H1]["openable"] and items[self.H1B]["openable"])
        self.assertEqual(originals["withheld"], {"total": 401, "shown": 198})
        self.assertEqual(originals["by_scheme"]["bc"]["shown"]
                         + originals["by_scheme"]["graph-mail"]["shown"], 198)
        # Permission is unchanged: a listed withheld capture never opens.
        held = [item for item in items.values() if not item["l1_handle"].startswith("api://")]
        self.assertTrue(held and all(not item["openable"] and item["withheld"]
                                     == {"state": "withheld", "reason": objects.NO_HOST_GRANT}
                                     for item in held))

    def test_the_cards_own_captures_survive_the_cap_before_timeline_captures(self):
        table, _ = self.table()
        events = [{"event_id": f"e{i}", "event_type": "Line.received",
                   "time_basis": "source_business_date", "time": "2099-01-02",
                   "objects": [{"object_id": self.ROOT, "qualifier": "subject"}],
                   "citation": _cite(f"api://public.aaa_line/{i:04d}@g1-1", "row.qty")}
                  for i in range(199)]
        answer = {"events": events, "gaps": [],
                  "completeness": {"complete": True, "truncated": False, "returned": 199}}
        table["plx.timeline"] = {"handler": lambda arguments, actor: answer}
        card = table["plx.object"]["handler"]({}, None)
        own = "api://public.zzz_header/7@g1-3"
        card["values"].append({"property": "total", "value_type": "money", "value": "5.00",
                               "citation": _cite(own, "row.total"), "markings": []})
        table["plx.object"] = {"handler": lambda arguments, actor: card}
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"employee:oid-1"})),
                                       sources=object())
        originals = view.workspace(self.ROOT)["originals"]
        items = {item["l1_handle"]: item for item in originals["items"]}
        self.assertEqual((originals["total"], originals["truncated"], len(items)),
                         (203, True, objects.MAX_ORIGINALS))
        # Card values, then the conflict's capture, then the timeline's, in that order.
        self.assertIn(own, items)
        self.assertEqual(items[own]["properties"], ["total"])
        self.assertIn(self.H1, items)
        self.assertIn(self.H1B, items)
        self.assertIn(self.MAIL, items)
        self.assertEqual(originals["by_scheme"]["api"], {"total": 202, "shown": 199})
        self.assertEqual(sum(h.startswith("api://public.aaa_line/") for h in items), 196)

    def test_originals_share_the_cap_round_robin_across_granted_families(self):
        # With bc granted too, a long api list cannot crowd bc out (or the reverse).
        handles = ([f"api://public.sample_line/{i:04d}@g1-1" for i in range(300)]
                   + [f"bc://sample/entry/{i:04d}@c1" for i in range(300)]
                   + ["graph-mail://box@example.test/m1@c4"])
        cited = {handle: {"cited_by": ["event x"]} for handle in handles}
        mail = {"graph-mail://box@example.test/m1@c4"}
        kept = objects._cap_originals(cited, objects.MAX_ORIGINALS, mail)
        self.assertEqual(len(kept), objects.MAX_ORIGINALS)
        self.assertIn("graph-mail://box@example.test/m1@c4", kept)
        self.assertEqual(sum(h.startswith("api://") for h in kept), 100)
        self.assertEqual(sum(h.startswith("bc://") for h in kept), 99)
        # A short family is exhausted and its slots go to the others.
        kept = objects._cap_originals(
            {h: cited[h] for h in handles[:300] + handles[300:305]}, 50, mail)
        self.assertEqual(sum(h.startswith("bc://") for h in kept), 5)
        self.assertEqual(len(kept), 50)
        self.assertEqual(objects._cap_originals({h: cited[h] for h in handles[:3]}, 50),
                         sorted(handles[:3]))

    def test_explanation_never_shows_a_withheld_capture_and_labels_an_outage_stale(self):
        table, _ = self.table()
        card = table["plx.object"]["handler"]
        label = ("source-mirror captures as of 2099-01-01T00:00:00Z, live source unavailable "
                 "(500, internal)")

        def held_card(arguments, actor):
            return {**card(arguments, actor), "meta": {"as_known_at": "2099-01-03T00:00:00Z",
                    "freshness": [{"source": "portal_api", "captured_at": "2099-01-01T00:00:00Z",
                                   "completeness": "partial", "stale": True,
                                   "served_from": "persisted_capture", "label": label}]}}
        table["plx.object"] = {"handler": held_card}
        view = objects.ObjectWorkspace(table, Actor("plx", frozenset({"employee:oid-1"})),
                                       sources=object())
        explanation = view.workspace(self.ROOT)["explanation"]
        # The link reason cites mail this console does not grant: the link's why stays,
        # the capture is a counted withheld gap, never a handle or an absence.
        self.assertNotIn(self.MAIL, json.dumps(explanation))
        self.assertEqual({item["handle"] for item in explanation["evidence"]}, {self.H1, self.H1B})
        gaps = {(gap["class"], gap["text"]): gap for gap in explanation["gaps"]}
        self.assertEqual(gaps[("withheld", "Mail records cited here: " + explanations.WITHHELD)][
            "count"], 1)
        self.assertIn(("withheld", "1 value " + explanations.WITHHELD), gaps)
        self.assertIn(("withheld", "2 records " + explanations.WITHHELD), gaps)
        self.assertEqual(gaps[("conflicting", "Sources disagree: value disagreement")]["about"],
                         ["due"])
        self.assertTrue(any(item["kind"] == "link" and "lead" in item["text"]
                            for item in explanation["why"]))
        known = explanation["as_known_at"]
        self.assertEqual((known["state"], known["captured_at"]),
                         ("outage", "2099-01-01T00:00:00Z"))
        self.assertTrue(known["label"].startswith("STALE") and label in known["label"])

    def test_withheld_parts_of_each_call_stay_in_the_answer(self):
        data, calls = self.open()
        self.assertEqual(data["card"]["withheld"]["properties"], 1)
        [neighbour] = [call for call in data["timeline"]["calls"]
                       if call["root"] != "object"]
        self.assertEqual(neighbour["result"]["withheld"],
                         {"properties": None, "objects": 2, "reason": "audience"})
        # Every tool call was made for the session reader.
        self.assertTrue(calls)
        self.assertTrue(all(actor.principals == frozenset({"employee:oid-1"})
                            for _, _, actor in calls))

    def test_documents_join_when_served_and_say_so_when_not(self):
        data, calls = self.open()
        self.assertEqual(data["documents"]["status"], "not_configured")
        self.assertNotIn(objects.DOCUMENTS_TOOL, [name for name, _, _ in calls])
        answer = {"documents": [{"citation": self.MAIL, "kind": "email",
                                 "title": {"untrusted_text": "PO H-7"}, "date": "2099-01-03",
                                 "snippet": None, "mentions": []},
                                {"citation": self.H1, "kind": "original", "title": None,
                                 "date": None, "snippet": None, "mentions": []}],
                  "searched": {"sources": ["graph_mail"]},
                  "gaps": [{"state": "inaccessible", "reason": "not_served",
                            "subject": "sharepoint"}],
                  "withheld": {"properties": None, "objects": None, "reason": "audience"},
                  "completeness": {"complete": True, "truncated": False, "returned": 2}}
        data, calls = self.open(documents=answer)
        [(_, arguments, _)] = [call for call in calls if call[0] == objects.DOCUMENTS_TOOL]
        self.assertEqual(arguments, {"object_id": self.ROOT, "limit": objects.MAX_DOCUMENTS})
        self.assertEqual(data["documents"]["status"], "read")
        mail, row = data["documents"]["result"]["documents"]
        self.assertEqual((mail["openable"], mail["not_openable_reason"]),
                         (False, objects.NO_HOST_GRANT))
        self.assertTrue(row["openable"])
        self.assertEqual(data["documents"]["result"]["withheld"]["reason"], "audience")
        self.assertIn({"from": "plx.documents", "state": "inaccessible",
                       "reason": "not_served", "subject": "sharepoint"},
                      data["gaps"]["items"])
        data, _ = self.open(documents={"error": {"code": "internal", "message": "x"}})
        self.assertEqual(data["documents"], {"status": "error",
                                             "error": {"code": "internal", "message": "x"}})

    def test_every_action_path_state_has_an_approval_stage(self):
        from operating import action_path
        self.assertEqual(set(objects.APPROVAL_STAGES), set(action_path.STATES))
        for state in action_path.STATES:
            approval = objects.approval_of(state)
            self.assertFalse(approval["approved_here"])
            self.assertEqual(approval["where"], "Mission Control")
        # Only a state after an authorizing gate decision reads approved.
        for state in ("proposed", "gate_prepared", "call_approved", "gate_requested",
                      "gate_rejected", "approval_expired", "gate_request_unknown"):
            self.assertNotEqual(objects.approval_of(state)["stage"], "approved", state)
        self.assertEqual(objects.approval_of("gate_authorized")["stage"], "approved")
        self.assertEqual(objects.approval_of("no_such_state")["stage"], "unknown")


class SharePointItemGateTest(unittest.TestCase):
    """SharePoint evidence through the case host's per-item gate.

    A versioned SharePoint capture (integrations/capture_sharepoint.py over the capture
    tests' in-process FakeDrive) projected with a Portal purchase order and mention
    links, served to two readers who both hold the folder: quality (Ricardo's identity)
    and sales, whose identity is on only some files' item permissions. Evidence label:
    injected-transport; no network, no credentials.
    """

    def setUp(self):
        import datetime as dt

        from integrations import graph_read
        from integrations.capture_sharepoint import (SharePointCapture,
                                                     SharePointCaptureStore, version_handle)
        from integrations.graph_read import SharePointReader
        from integrations.live_source_guard import BoundedBackoff
        from integrations.source_mirror import AUDIENCE_SCHEMA
        from integrations.tests import test_capture_sharepoint as tc
        from ontology import mentions
        from ontology import schema as schema_loader
        from ontology.project import SharePointCaptures, build_store
        from ontology.tests.test_mail_documents import StaticPortal, po

        self.tc, self.version_handle = tc, version_handle
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.capture_dir = self.root / "sharepoint"
        drive = tc.seeded_drive()
        # Quality-only: a lot note naming the purchase order (Ricardo alone on its ACL).
        drive.file("01FILED", "lot-note.txt", "01LOTS", b"Quality hold on PO100234 lot 7.",
                   mime="text/plain", acl=(tc.RICARDO,))
        store = SharePointCaptureStore(self.capture_dir, clock=tc.Clock())
        self.addCleanup(store.close)
        api, transport = graph_read.build_graph(tc.ENV, sender=drive, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        run = SharePointCapture(store, lambda d: SharePointReader(
            api, tc.capture_audience(), d, transport=transport, read_permissions=True),
            tc.CAPTURE).run(tc.scope())
        self.assertTrue(run["complete"], run)
        self.captured = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)
        self.projection = self.root / "ontology.sqlite"
        portal = StaticPortal([po("public.app_purchase_order", {"id": "p1"},
                                  {"id": "p1", "po_number": "PO100234"})])
        with SharePointCaptures(self.capture_dir) as files:
            build_store(self.projection, schema_loader.load_schema(),
                        {"portal_api": portal, "sharepoint": files},
                        clock=lambda: self.captured + dt.timedelta(days=1),
                        post_project={"mentions": mentions.store_mention_links})
        self.mirror_audience = self.root / "mirror-audience.json"
        self.mirror_audience.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx",
            "audiences": {"public.app_purchase_order": ["staff:quality", "staff:sales"]}}),
            "utf-8")

    def handle(self, item_id):
        return self.version_handle(self.tc.DRIVE, item_id, f'"{{{item_id}}},1"')

    def build(self, actor):
        """``(runtime, view)`` for one reader, in the calling thread (SQLite affinity)."""
        import datetime as dt

        from operating.case_runtime import LiveReaders
        runtime = CaseRuntime(self.root / "case-state", client=None,
                              audience=self.mirror_audience, tenant="plx",
                              live=LiveReaders(audience=self.tc.reader_audience()))
        soon = self.captured + dt.timedelta(hours=1)  # within the host's max capture age
        access = objects.host_access(runtime, self.projection, actor,
                                     sharepoint_capture_dir=self.capture_dir,
                                     clock=lambda: soon)
        return runtime, objects.ObjectWorkspace(access.tools(), actor,
                                                sources=CaseWorkspace(runtime, actor),
                                                access=access)

    def workspace(self, actor):
        runtime, view = self.build(actor)
        self.addCleanup(runtime.close)
        self.addCleanup(view.close)
        return view

    def served(self, actor):
        """``GET`` on a console serving ``actor``'s workspace for its fixture session
        (runtime and tools built in the serving thread)."""
        ready: Queue = Queue(maxsize=1)

        def run():
            try:
                runtime, view = self.build(actor)
                session = session_identity.fixture_session(
                    "plx", actor.principals, clearance=actor.clearance,
                    channel=session_identity.CHANNEL_CONSOLE)
                server = make_server(session=session, object_workspace=view,
                                     case_workspace=CaseWorkspace(runtime, session.actor))
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

        def get(path):
            conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=60)
            conn.request("GET", path, headers={
                "Authorization": "Bearer " + server.operator_access_token})
            response = conn.getresponse()
            value = response.status, json.loads(response.read())
            conn.close()
            return value
        return get

    def test_a_file_shows_and_opens_for_its_reader_and_is_withheld_from_another(self):
        quality, sales = self.workspace(self.tc.QUALITY), self.workspace(self.tc.EMPLOYEE)
        found = quality.find("PO100234")["find"]
        po = next(c["object"]["object_id"] for c in found["candidates"]
                  if c["object"]["object_type"] == "PurchaseOrder")
        # Through the served console: each reader's own session and grant.
        get_quality, get_sales = self.served(self.tc.QUALITY), self.served(self.tc.EMPLOYEE)
        opened = "/api/object?id=" + quote(po, safe="")
        (_, wide), (_, narrow) = get_quality(opened), get_sales(opened)
        note, release = self.handle("01FILED"), self.handle("01FILEB")
        wide_docs = {d["citation"]: d for d in wide["documents"]["result"]["documents"]}
        narrow_docs = {d["citation"]: d for d in narrow["documents"]["result"]["documents"]}
        # Quality sees both files that name the order, each openable here.
        self.assertTrue({note, release} <= set(wide_docs), sorted(wide_docs))
        self.assertTrue(wide_docs[note]["openable"] and wide_docs[release]["openable"])
        # Files served from the capture in normal operation (the host's own as_of_capture
        # labels) explain as "as of capture", never as an unavailable live source.
        self.assertIn("as_of_capture", {g["reason"] for g in wide["documents"]["result"]["gaps"]})
        explained = wide["explanation"]
        self.assertNotEqual(explained["as_known_at"]["state"], "outage")
        self.assertNotIn("unavailable", explained["as_known_at"]["label"])
        self.assertNotIn("upstream_outage", {g["class"] for g in explained["gaps"]})
        [captured] = [g for g in explained["gaps"] if "as of capture" in g["text"]]
        self.assertEqual(captured["class"], "incomplete_source")
        self.assertTrue(any(d.startswith("as of capture 2") for d in captured["details"]))
        # Sales sees the file its identity is on; the quality-only note is withheld:
        # a marker in the documents answer, never its name, path or text.
        self.assertTrue(set(narrow_docs) <= set(wide_docs))
        self.assertIn(release, narrow_docs)
        self.assertTrue(narrow_docs[release]["openable"])
        self.assertNotIn(note, narrow_docs)
        self.assertIsNotNone(narrow["documents"]["result"].get("withheld"))
        self.assertNotIn("lot-note", json.dumps(narrow))
        self.assertNotIn("Quality hold", json.dumps(narrow))
        # Opening goes through the host's plx.read: the captured file, labelled as of
        # capture, for quality; not found for sales (the same as a missing file).
        source = "/api/object-source?handle="
        status, read = get_quality(source + quote(note, safe=""))
        self.assertEqual((status, read["kind"], read["status"]),
                         (200, "source_read", "resolved"))
        self.assertIn("Quality hold on PO100234", read["content"]["untrusted_text"])
        self.assertIn("as_of_capture", {gap["reason"] for gap in read["gaps"]})
        self.assertEqual(get_sales(source + quote(note, safe="")), (404, {"error": "not_found"}))
        status, read = get_sales(source + quote(release, safe=""))
        self.assertEqual((status, read["status"]), (200, "resolved"))
        # The decision is per item, not per family.
        with sales.access.request():
            decided = sales.access.decide([note, release])
        self.assertEqual(decided, {note: {"state": "withheld", "reason": objects.ITEM_REFUSED},
                                   release: None})


class ObjectViewBuildOnceTest(unittest.TestCase):
    """The workspace probe builds once: the rebuild and the claim sample are the projection
    check's, and a budget stop inside the build keeps the build's allowlisted partial
    counts (the fake clock and fixture wiring of ``tests/test_ontology_traverse_probe.py``)."""

    def test_a_build_over_half_the_budget_still_opens_views_without_a_rebuild(self):
        from scripts import object_workspace_probe as probe
        from tests.test_ontology_traverse_probe import consumer_run
        with tempfile.TemporaryDirectory() as tmp:
            code, document, built = consumer_run(probe.main, tmp, "--sample", "4",
                                                 before=600.0)
        self.assertEqual(built, [f"{tmp}/ontology.sqlite"])   # no rebuild
        self.assertNotIn("status", document)
        self.assertGreater(document["roots_opened"], 0)
        self.assertFalse(document["budget_exhausted"])

    def test_a_budget_stop_passes_the_builds_partial_counts_through(self):
        from scripts import object_workspace_probe as probe
        from tests.test_ontology_traverse_probe import (assert_build_partial_passed,
                                                        consumer_run)
        with tempfile.TemporaryDirectory() as tmp:
            code, document, built = consumer_run(probe.main, tmp, after=1000.0, stray=True)
        self.assertEqual(len(built), 1)
        assert_build_partial_passed(self, code, document)


# The probe's typed causes of ObjectViewFailedOpenTest's four failed roots that are no
# permission withhold.
OPEN_ERRORS = {"workspace_KeyError": 1, "workspace_ValueError": 1,
               "not_found_no_current_version": 1, "not_found_gate_error": 1}


class ObjectViewFailedOpenTest(unittest.TestCase):
    """The workspace probe samples the store whatever the reader's grant, so a failed root
    open is classified by the host's own access decision. ``plx.object`` answers one
    ``object_not_found`` for a withheld root and a broken one, so the host types it
    (``CaseHostTools.not_found_reason``) and only a permission reason counts as withheld.
    Six sampled roots fail to open over the fixture world: ``hidden`` lies outside the
    reader's audience and ``marked`` carries a marking the reader lacks on its latest
    version (withheld); ``stale`` has a ``recorded_at`` that does not parse (no current
    version) and ``gated`` is an original whose item gate raises (broken); ``broken`` is
    readable and a panel raises the very ``KeyError("object_not_found")`` an invisible card
    raises; ``undecided`` is readable, a panel raises and the host's decision call itself
    fails. Evidence: injected-transport."""

    @classmethod
    def setUpClass(cls):
        from operating import ontology_host
        from operating.host import projection_path
        from scripts import object_workspace_probe as probe
        from scripts import ontology_projection_probe as projection
        from tests.test_ontology_traverse_probe import consumer_run

        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        code, _, _ = consumer_run(probe.main, tmp.name, "--sample", "1")
        assert code == 0, code
        store = f"{tmp.name}/ontology.sqlite"
        host = projection.served_host(store)
        served = projection_path(host.state_dir)
        cls.roots = probe.sample_roots(str(served), sample=40, per_type=4)
        hidden, broken, undecided, stale, marked, gated = (oid for _, oid in cls.roots[:6])
        cls.named = dict(hidden=hidden, broken=broken, stale=stale, marked=marked,
                         gated=gated, absent="Nothing:none:absent")
        gate_handle = "original://object-view-probe-gate"
        with sqlite3.connect(served) as conn:
            conn.execute("UPDATE objects SET audience_key = 'not-granted:object-view-probe' "
                         "WHERE object_id = ?", (hidden,))
            conn.execute("UPDATE object_versions SET markings = '[\"finance\"]' "
                         "WHERE object_id = ?", (marked,))
            conn.execute("UPDATE object_versions SET recorded_at = 'not-a-time', "
                         "superseded_at = NULL WHERE object_id = ?", (stale,))
            conn.execute("UPDATE object_versions SET l1_handle = ? WHERE object_id = ?",
                         (gate_handle, gated))
        conn.close()
        process, card = objects.ObjectWorkspace._process, ot.OntologyTools._object
        runtime_gate = ontology_host.item_gate_from_case_runtime

        def raising_process(self, object_id):
            if object_id == broken:
                raise KeyError("object_not_found")
            if object_id == undecided:
                raise ValueError("panel_failed")
            return process(self, object_id)

        def card_or_outage(self, arguments, actor):
            # the decision call (no detailed card) on ``undecided`` gets no answer
            if arguments["object_id"] == undecided and "response_format" not in arguments:
                raise ot.ToolError("store_unavailable", "no answer")
            return card(self, arguments, actor)

        def raising_gate(runtime):
            fallback = runtime_gate(runtime)

            def gate(actor, handle):
                if handle == gate_handle:
                    raise RuntimeError("item_gate_unreachable")
                return fallback(actor, handle)
            return gate

        with mock.patch.object(objects.ObjectWorkspace, "_process", raising_process), \
                mock.patch.object(ot.OntologyTools, "_object", card_or_outage), \
                mock.patch.object(ontology_host, "item_gate_from_case_runtime", raising_gate):
            cls.report = probe.check_views(projection.served_host(store), sample=40,
                                           per_type=4, max_opens=0, budget_seconds=None)
            # The reader's own answers and the host's typed reasons, through the console's
            # workspace as the probe opens it.
            host = projection.served_host(store)
            reader = projection.reader_actor(host)
            with host.served_tools(reader) as tools:
                workspace = case_console.object_workspace_for(tools.runtime, served, reader,
                                                              state_dir=host.state_dir)
                try:
                    with workspace.access.request():
                        cls.answers = {name: workspace.tools["plx.object"]["handler"](
                            {"object_id": oid}, reader) for name, oid in cls.named.items()}
                        gated_reader = tools.runtime.employee_gate(reader)
                        cls.reasons = {name: workspace.access.host.not_found_reason(
                            oid, gated_reader) for name, oid in cls.named.items()}
                finally:
                    workspace.close()

    def test_a_withheld_root_is_no_error_and_a_broken_or_readable_one_keeps_its_cause(self):
        report = self.report
        self.assertGreaterEqual(len(self.roots), 7)   # at least one root opens
        self.assertEqual(report["roots_sampled"], len(self.roots))
        self.assertEqual(report["withheld_roots"], 2)
        self.assertEqual(report["workspace_errors"], 4)
        self.assertEqual(report["open_errors"], OPEN_ERRORS)
        self.assertEqual(report["roots_opened"], len(self.roots) - 6)
        self.assertEqual(report["denied_leaks"], 0)
        self.assertEqual(report["denied_not_found"], report["denied_probed"])

    def test_the_host_types_not_found_and_the_reader_sees_one_answer(self):
        self.assertEqual(self.reasons, {
            "hidden": "withheld", "marked": "withheld", "stale": "no_current_version",
            "gated": "gate_error", "absent": "absent", "broken": None})
        # Withheld, broken and absent read alike to the reader: no reason reaches it.
        not_found = {"code": "object_not_found",
                     "message": "no such object is visible to this reader",
                     "hint": "resolve the identifier with plx.find first"}
        for name, answer in self.answers.items():
            if name != "broken":
                self.assertEqual(answer["error"], not_found, name)
        self.assertEqual(self.answers["broken"]["object"]["object_id"], self.named["broken"])

    def test_the_spec_requires_and_keeps_the_typed_cause_under_the_detail_cap(self):
        from verify import live_verify as lv
        from tests import test_live_wiring as wiring
        from scripts import object_workspace_probe as probe

        spec = wiring.spec("workspace.object_view")
        document = {**self.report, "probe": probe.PROBE}
        for expect in (spec["expect"], spec["expect_blocked"]):
            failures = lv.check_expectations(spec, 0, document, expect)
            self.assertFalse([f for f in failures if "open_errors" in f
                              or "withheld_roots" in f], failures)
            failures = lv.check_expectations(spec, 0, {
                k: v for k, v in document.items() if k not in ("open_errors", "withheld_roots")},
                expect)
            self.assertIn("missing open_errors", failures)
            self.assertIn("missing withheld_roots", failures)
        # A live-sized answer (40 types found by) overflows the cap: the cause still stays.
        wide = {**document, "find_by_type": {f"Type{n}": dict.fromkeys(probe.FIND_COUNTS, 1)
                                             for n in range(40)}}
        detail = lv._cap({"fields": lv._recorded(spec, wide)})
        self.assertIn("fields_truncated", detail)
        self.assertEqual(detail["fields"]["open_errors"], OPEN_ERRORS)
        self.assertEqual((detail["fields"]["workspace_errors"],
                          detail["fields"]["withheld_roots"]), (4, 2))
        self.assertNotIn("not-granted", json.dumps(detail))


if __name__ == "__main__":
    unittest.main()
