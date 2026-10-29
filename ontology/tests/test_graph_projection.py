"""Captured Graph mail and SharePoint files as first-class ontology sources (directive
ddc3e0c36691 L4).

One state directory holds a versioned mail capture and a versioned SharePoint capture
(filled over injected Graph transports); with synthetic Portal and BC purchase orders it
is projected through the host's projection entry point (``operating.host
.state_captures`` + ``build_projection``, the path a live capture cycle runs); a Teams
capture is projected through ``TeamsCaptures``. Checked:
attachments are mention documents whose links to Portal and BC objects cite the page and
span they were read from, and stay candidate leads; a restricted reader sees no withheld
mail or file through any consumer; an unchanged re-projection rebuilds nothing; a removed
message is marked missing from the source. No network, no credentials. Evidence:
injected-transport (the PDF text layer is a fake parser: pypdf is optional).
"""

from __future__ import annotations

from contextlib import ExitStack
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from integrations import document_understanding as du
from integrations import graph_read
from integrations.capture_mail import MailCapture, MailCaptureStore
from integrations.graph_read import GraphMailReader
from integrations.live_source_guard import BoundedBackoff
from integrations.people_exclusion import PeopleExclusion
from integrations.source_mirror import AudienceMap
from integrations.tests import test_capture_mail as tm
from integrations.tests import test_capture_sharepoint as tsp
from integrations.tests import test_capture_teams as tct
from ontology import store_ddl
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.generator import graph
from ontology.project import build_store
from ontology.project_graph import TeamsCaptures
from ontology.tests.test_find_identity import Source
from ontology.types import Actor
from operating import host
from operating.case_runtime import CaseRuntime, LiveReaders
from operating import ontology_tools as ot
from operating import ontology_tools_aggregate as oa
from operating import ontology_tools_evidence as oe
from operating import ontology_tools_process as op
from operating import ontology_tools_traverse as tt
from scripts import source_registry as sr

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
NOW = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)
SLIP = b"%PDF-1.7 packing slip"
PRICING = b"%PDF-1.7 finance pricing"
PAGES = {SLIP: ["PACKING LIST\nCartons: 240", "Ship to Petra\nPO: PO100235"],
         PRICING: ["Price list for PO100235 only"]}
ATT_LINK = "Attachment.mentions_purchase_order"
LINK_TYPES = (ATT_LINK, "Email.mentions_purchase_order",
              "SharePointFile.mentions_purchase_order")
# Mailbox A and the Quality folder but not its Ricardo-only file; mailbox B is withheld.
ORDERS = Actor("plx", frozenset({"staff:orders", "staff:sales"}), "restricted")
OUTSIDER = Actor("plx", frozenset({"staff:outsider"}), "restricted")


def fake_read_pages(content, kind, **_options):
    """The one PDF reader's reading of a fixture PDF: its pages read by pypdf."""
    return du._paged(hashlib.sha256(content).hexdigest(), "pypdf-text",
                     [du.Page(number, text, "pypdf-text")
                      for number, text in enumerate(PAGES[content], 1)])


def attachment(aid, name, content, mime):
    return {**tm.file_attachment(aid, content, name), "contentType": mime}


def capture_mail(state, graph, store=None):
    store = store or MailCaptureStore(state, clock=tm.Clock())
    api, _ = graph_read.build_graph(tm.ENV, sender=graph, retry=BoundedBackoff(
        sleep=lambda s: None, jitter=lambda: 0.0))
    run = MailCapture(store, GraphMailReader(api, tm.capture_audience()), tm.CAPTURE,
                      sleep=lambda s: None).run(tm.SCOPE)
    assert run["complete"], run
    return store


def seeded_graph():
    graph = tm.FakeGraph()
    graph.put(tm.BOX_A, "MSG-1", "CK-1-1", subject="Shipment documents", body="See attached.",
              attachments=[attachment("ATT-1", "slip.pdf", SLIP, "application/pdf"),
                           attachment("ATT-2", "note.txt", b"Re PO100236 freight",
                                      "text/plain")])
    graph.put(tm.BOX_A, "MSG-2", "CK-2-1", subject="Order PO100234", body="Confirmed.")
    graph.put(tm.BOX_B, "MSG-B1", "CKB-1", subject="Finance", body="Pricing attached.",
              attachments=[attachment("ATT-B", "pricing.pdf", PRICING, "application/pdf")])
    return graph


def sources(stack, state):
    found = host.state_captures(state, stack, internal_domains=tm.SCOPE.internal_domains)
    found["portal_api"] = Source("portal_api", {"public.app_purchase_order": [
        {"id": "p1", "po_number": "PO100234"}, {"id": "p2", "po_number": "PO100235"},
        {"id": "p3", "po_number": "PO100236"}]})
    found["bc"] = Source("bc", {"purchaseOrders": [{"id": "b2", "number": "PO100235"}]})
    return found


PDF_TEXT = mock.patch("integrations.document_understanding.read_pages", fake_read_pages)


def setUpModule():
    PDF_TEXT.start()


def tearDownModule():
    PDF_TEXT.stop()


def tools(case, path, mail, sharepoint=None):
    policy = ot.ReadPolicy(tenant="plx", audiences={
        "api:*": {"staff:orders", "staff:outsider"},
        "bc:*": {"staff:orders", "staff:outsider"},
        "mail:*": {"staff:orders", "staff:outsider"},
        "sharepoint:*": {"staff:sales", "staff:outsider"}},
        markings={m: {"staff:orders", "staff:outsider"} for m in store_ddl.MARKINGS})
    mail_gate = mail.item_gate(tm.reader_audience())
    sp_gate = None if sharepoint is None else sharepoint.item_gate(tsp.reader_audience())

    def gate(actor, handle):   # the host's per-scheme item gate
        return (mail_gate if handle.startswith("graph-mail://") else sp_gate)(actor, handle)

    reader = ot.StoreReader.open(path)
    case.addCleanup(reader.conn.close)
    return ot.OntologyTools(reader, policy, item_gate=gate,
                            schema=ot.SchemaView.from_compiled(schema_loader.load_schema()),
                            grammar=None, contracts=CONTRACTS, clock=lambda: NOW)


class GraphProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        cls.case = tsp.CaptureCase("run")
        cls.case.setUp()
        run = cls.case.capture().run(tsp.scope())
        assert run["complete"], run
        cls.state = cls.case.state
        cls.graph = seeded_graph()
        cls.mail = capture_mail(cls.state, cls.graph)
        with ExitStack() as stack:
            found = sources(stack, cls.state)
            assert {"graph_mail", "sharepoint"} <= set(found), found
            cls.built = host.build_projection(cls.state, found)
        cls.path = host.projection_path(cls.state)
        cls.conn = sqlite3.connect(cls.path)
        cls.conn.row_factory = sqlite3.Row
        cls.compiled = schema_loader.load_schema()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.mail.close()
        cls.case.tearDown()

    def oid(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()[0]

    def attachment_object(self, aid):
        return self.oid("SELECT object_id FROM objects WHERE object_type = 'Attachment' AND "
                        "json_extract(native_key, '$.attachment_id') = ?", aid)

    def po(self, key):
        return self.oid("SELECT object_id FROM objects WHERE object_type = 'PurchaseOrder' "
                        "AND json_extract(native_key, '$.id') = ?", key)

    def tools(self):
        return tools(self, self.path, self.mail, self.case.store)

    def ok(self, name, result):
        self.assertEqual(validate(result, CONTRACTS["tools"][name]["output"], root=CONTRACTS),
                         [], json.dumps(result)[:1500])
        self.assertNotIn("error", result, json.dumps(result)[:1500])
        return result

    def test_attachment_text_links_portal_and_bc_orders_as_page_cited_candidates(self):
        self.assertEqual(self.built["post_project"]["mentions"]["counts"]["undeclared_target"],
                         0)
        slip = self.attachment_object("ATT-1")
        rows = self.conn.execute("SELECT * FROM links WHERE link_type = ? AND from_object = ?",
                                 (ATT_LINK, slip)).fetchall()
        # PO100235 on page 2 names a Portal PO and a BC PO: two candidate leads of one
        # mention, never one asserted identity.
        self.assertEqual({r["to_object"] for r in rows}, {self.po("p2"), self.po("b2")})
        groups = set()
        for row in rows:
            self.assertEqual((row["tier"], row["binding_kind"]), ("lead", "mention"))
            why = {f["feature"] for f in json.loads(row["why_not"])}
            self.assertIn("external_assertion", why)
            [cited] = [f for f in json.loads(row["reason"])
                       if f["feature"] == "identifier_mention"]
            groups.add(cited["mention_group"])
            [mention] = cited["citations"]
            self.assertTrue(mention["l1_handle"].endswith("&page=2"), mention)
            self.assertEqual(mention["field_path"], "row.content_text")
            # The cited page and span resolve offline to the text the link names.
            resolved = self.mail.resolve(mention["l1_handle"], tm.READER_A,
                                         tm.reader_audience(), text=True)
            start, end = cited["span"]
            self.assertEqual(resolved["row"]["content_text"][start:end], "PO100235")
        self.assertEqual(len(groups), 1)
        self.assertIsNone(self.mail.resolve(mention["l1_handle"], OUTSIDER,
                                            tm.reader_audience(), text=True))
        # An unpaged attachment is cited at its own handle.
        note = self.conn.execute("SELECT to_object, reason FROM links WHERE link_type = ? AND "
                                 "from_object = ?", (ATT_LINK, self.attachment_object("ATT-2"))
                                 ).fetchall()
        self.assertEqual([r["to_object"] for r in note], [self.po("p3")])
        self.assertNotIn("page=", note[0]["reason"])

    def test_a_restricted_reader_sees_only_permitted_mail_and_files_through_every_consumer(self):
        base = self.tools()
        traverse = tt.TraverseTools(base)
        evidence = oe.EvidenceTools(base, traverse=traverse)
        aggregate = oa.AggregateTools(base, traverse)
        process = op.ProcessTools(base)
        hidden_mail = [r[0] for r in self.conn.execute(
            "SELECT object_id FROM objects WHERE audience_key = ?", (f"mail:{tm.BOX_B}",))]
        self.assertEqual(len(hidden_mail), 2)    # MSG-B1 and its attachment
        hidden_file = self.oid("SELECT object_id FROM objects WHERE object_type = "
                               "'SharePointFile' AND json_extract(native_key, '$.id') = "
                               "'01FILEC'")    # item permissions: Ricardo only
        everything = [*hidden_mail, hidden_file, self.attachment_object("ATT-1")]
        for actor, seen in ((ORDERS, {self.attachment_object("ATT-1")}), (OUTSIDER, set())):
            outputs = []
            for root in (self.po("p2"), self.po("b2")):
                outputs.append(base.call("plx.object", {"object_id": root}, actor))
                outputs.append(base.call("plx.find", {"query": "PO100235"}, actor))
                for link_type in LINK_TYPES:
                    around = traverse.call("plx.around", {"from": root, "path": [
                        {"link_type": link_type, "direction": "reverse"}],
                        "tier_floor": "lead"}, actor)
                    outputs.append(around)
                    if link_type == ATT_LINK:
                        reached = {o["object_id"] for o in around.get("objects", [])}
                        self.assertEqual(reached, seen, (actor, root))
                outputs.append(traverse.call("plx.timeline", {"from": root}, actor))
                outputs.append(evidence.call("plx.documents", {"object_id": root}, actor))
                outputs.append(evidence.call("plx.identity", {"object_ids": [root]}, actor))
                outputs.append(process.call("plx.process", {"from": root}, actor))
            outputs.append(aggregate.call("plx.aggregate", {
                "set": base.sets.new(actor, everything), "measure": {"op": "count"},
                "group_by": ["name"]}, actor))
            for name in ("Attachment", "SharePointFile", "Email"):
                outputs.append(base.call("plx.find", {"query": "pricing", "object_type": name},
                                         actor))
            for hidden in everything:
                if hidden in seen:
                    continue
                denied = base.call("plx.object", {"object_id": hidden}, actor)
                self.assertEqual(denied["error"]["code"], "object_not_found")
            text = json.dumps(outputs)
            for marker in (tm.BOX_B, "MSG-B1", "pricing.pdf", tsp.NAME_SECRET, "01FILEC"):
                self.assertNotIn(marker, text, actor)
            if actor is OUTSIDER:
                self.assertNotIn("graph-mail://", text)
                self.assertNotIn("sharepoint://", text)
        # A count over members some of which are withheld is unknown, never a smaller total.
        counted = [self.ok("plx.aggregate", aggregate.call("plx.aggregate", {
            "set": base.sets.new(ORDERS, members), "measure": {"op": "count"}}, ORDERS))
            for members in (everything, [self.attachment_object("ATT-1")])]
        self.assertEqual([c["groups"][0]["value"] for c in counted], [None, "1"])

    def test_ingestion_reprojection_is_a_no_op_and_absence_is_marked(self):
        again = self._rebuild()
        self.assertEqual(again["build"]["units_rebuilt"], 0)
        self.assertEqual(again["content_digest"], self.built["content_digest"])

    def _rebuild(self):
        with ExitStack() as stack:
            return host.build_projection(self.state, sources(stack, self.state))


class AbsenceTests(unittest.TestCase):
    """A message removed from the source stays its last captured version, marked
    ``missing_from_source_since`` (never deleted, never visible to a reader it was
    withheld from); before the removal it reads as it was."""

    def test_a_removed_message_is_marked_missing_not_deleted(self):
        os.environ.pop("PLX_WORKBENCH_API_KEY", None)
        case = tm.CaptureCase("run")
        case.setUp()
        self.addCleanup(case.tearDown)
        graph = seeded_graph()
        store = capture_mail(case.state, graph)
        self.addCleanup(store.close)
        with ExitStack() as stack:
            first = host.build_projection(case.state, sources(stack, case.state))
        graph.delete(tm.BOX_A, "MSG-2")
        capture_mail(case.state, graph, store)
        with ExitStack() as stack:
            second = host.build_projection(case.state, sources(stack, case.state))
        self.assertNotEqual(first["content_digest"], second["content_digest"])
        with sqlite3.connect(host.projection_path(case.state)) as conn:
            rows_ids = [r[0] for r in conn.execute(
                "SELECT object_id FROM objects WHERE object_type = 'Email'")]
            [first_seen] = conn.execute(
                "SELECT first_recorded_at FROM objects WHERE object_type = 'Email' AND "
                "json_extract(native_key, '$.id') = 'MSG-2'").fetchone()
            rows = dict(conn.execute(
                "SELECT json_extract(native_key, '$.id'), missing_from_source_since FROM "
                "objects WHERE object_type = 'Email'").fetchall())
            current = conn.execute(
                "SELECT COUNT(*) FROM object_versions v JOIN objects o USING (object_id) "
                "WHERE o.object_type = 'Email' AND v.superseded_at IS NULL").fetchone()[0]
            links = conn.execute(
                "SELECT COUNT(*) FROM links l JOIN objects o ON o.object_id = l.from_object "
                "WHERE json_extract(o.native_key, '$.id') = 'MSG-2'").fetchone()[0]
        self.assertIsNotNone(rows["MSG-2"])
        self.assertIsNone(rows["MSG-1"])
        self.assertEqual(current, 3)     # the removed message keeps its last version
        self.assertEqual(links, 1)       # and its mention of PO100234
        base = tools(self, host.projection_path(case.state), store)
        msg2 = next(o for o in rows_ids if "MSG-2" in o)

        def missing(arguments):
            out = base.call("plx.object", dict(arguments, object_id=msg2), tm.READER_A)
            return [g["reason"] for g in out["gaps"] if g["reason"] == "missing_from_source"]

        self.assertEqual(missing({}), ["missing_from_source"])
        self.assertLess(first_seen, rows["MSG-2"])
        self.assertEqual(missing({"as_known_at": first_seen}), [])
        denied = base.call("plx.object", {"object_id": msg2}, OUTSIDER)
        self.assertEqual(denied["error"]["code"], "object_not_found")


class TeamsProjectionTests(tct.CaptureCase):
    """The Teams capture (a team with a standard and a private channel, and a chat, over
    the injected Graph of ``test_capture_teams``; the scope reads the chat too) projected
    through ``TeamsCaptures`` with the generated ``graph_teams`` types, then read through
    the ontology tools with the host's item gate (``CaseRuntime.resolve_citation`` over the
    capture in the runtime's state directory)."""

    def test_a_reader_outside_a_channel_or_chat_gets_no_row(self):
        self.assertTrue(self.capture.run(tct.scope())["complete"])
        path = Path(self.tmp.name) / "projection.sqlite"
        now = lambda: dt.datetime.now(dt.timezone.utc)   # the capture's own clock  # noqa: E731
        with TeamsCaptures(self.store.path) as captured:
            report = build_store(path, schema_loader.load_schema(), {"teams": captured},
                                 clock=now)
        with sqlite3.connect(path) as conn:
            objects = dict(conn.execute(
                "SELECT object_id, audience_key FROM objects "
                "WHERE object_type LIKE 'Teams%Message'"))
        # The chat is projected beside the channels (gated by its captured membership).
        self.assertFalse([g for g in report["gaps"] if g["where"].startswith("gen.teams.")])
        general = {o for o, unit in objects.items() if tct.GENERAL in unit}
        chat = {o for o, unit in objects.items() if unit.startswith("teams:chat:")}
        private = set(objects) - general - chat
        # 2 roots + 2 replies; 1; the chat's 2 messages
        self.assertEqual((len(general), len(private), len(chat)), (4, 1, 2))
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        runtime = CaseRuntime(self.tmp.name, client=None, audience=AudienceMap({}, tenant="plx"),
                              tenant="plx", live=LiveReaders(self.audience),
                              people_exclusion=PeopleExclusion.from_env(tct.ENV))
        self.addCleanup(runtime.close)
        base = ot.OntologyTools(
            reader, ot.ReadPolicy(tenant="plx", audiences={"teams:*": {
                "staff:cos", "staff:vince", "staff:teams-member", "staff:teams-outsider"}}),
            item_gate=ot.item_gate_from_case_runtime(runtime),
            schema=ot.SchemaView.from_compiled(schema_loader.load_schema()), grammar=None,
            contracts=CONTRACTS, clock=now)
        traverse = tt.TraverseTools(base)
        aggregate = oa.AggregateTools(base, traverse)
        process = op.ProcessTools(base)
        nobody = Actor("plx", frozenset({"staff:nobody"}), "restricted")   # no audience grant
        everything = sorted(objects)
        # Cos is in the team and the chat, vince in the team and the private channel.
        for actor, seen in ((tct.MEMBER, set(objects)), (tct.COS_ONLY, general | chat),
                            (tct.VINCE_ONLY, general | private), (tct.OUTSIDER, set()),
                            (nobody, set())):
            found = [base.call("plx.find", {"object_type": object_type, "limit": 50,
                                            "filters": [{"property": "id", "op": "present"}]},
                               actor)
                     for object_type in ("TeamsChannelMessage", "TeamsChatMessage")]
            self.assertEqual({c["object"]["object_id"] for f in found for c in f["candidates"]},
                             seen, actor)
            for object_id in everything:
                read = base.call("plx.object", {"object_id": object_id}, actor)
                self.assertEqual("error" not in read, object_id in seen, (actor, object_id))
            counted = aggregate.call("plx.aggregate", {
                "set": base.sets.new(actor, everything), "measure": {"op": "count"}}, actor)
            self.assertEqual(counted["groups"][0]["value"] if counted.get("groups") else None,
                             "7" if seen == set(objects) else None, actor)
            hidden = sorted(private - seen)[:1] + sorted(chat - seen)[:1] \
                or sorted(set(objects) - seen)[:1]
            around = [traverse.call("plx.timeline", {"from": o}, actor) for o in hidden] + [
                process.call("plx.process", {"from": o}, actor) for o in hidden]
            for out in around:   # a withheld root is not found
                self.assertEqual(out["error"]["code"], "object_not_found", actor)
            text = json.dumps([found, counted, around])
            self.assertNotIn(tct.SECRET_TEXT, text)
            if not seen:
                self.assertNotIn("teams-channel://", text)
            if not seen & chat:
                self.assertNotIn("teams-chat://", text)


    def test_teams_message_text_reaches_search_and_mentions_per_reader(self):
        """A Teams message's body is in the text index and its identifier mentions are
        links (``TeamsCaptures.documents``): a reader finds it by its words, and reaches it
        from the record it names, only in the conversations it is a member of."""
        from ontology.mentions import store_mention_links
        from ontology.text_index import store_text_index

        self.assertTrue(self.capture.run(tct.scope())["complete"])
        path = Path(self.tmp.name) / "projection.sqlite"
        now = lambda: dt.datetime.now(dt.timezone.utc)   # noqa: E731
        portal = Source("portal_api", {"public.app_purchase_order": [
            {"id": "p9", "po_number": "PO12172025"}]})
        with TeamsCaptures(self.store.path) as captured:
            report = build_store(path, schema_loader.load_schema(),
                                 {"teams": captured, "portal_api": portal}, clock=now,
                                 post_project={"text_index": store_text_index,
                                               "mentions": store_mention_links})
        indexed, linked = report["post_project"]["text_index"], report["post_project"]["mentions"]
        self.assertEqual((indexed["documents"], indexed["documents_not_projected"]), (7, 0))
        self.assertEqual(linked["counts"]["links"], 3)   # 1001, 1002 and chat 3001 name it
        with sqlite3.connect(path) as conn:
            units = dict(conn.execute("SELECT object_id, audience_key FROM objects "
                                      "WHERE object_type LIKE 'Teams%Message'"))
            bodies = {o for (o,) in conn.execute(
                "SELECT DISTINCT object_id FROM text_fields WHERE field_path='row.body_text' "
                "AND document_kind='teams_message'")}
            po = conn.execute("SELECT object_id FROM objects WHERE object_type="
                              "'PurchaseOrder'").fetchone()[0]
        self.assertEqual(bodies, set(units))   # every message's body is indexed
        general = {o for o, u in units.items() if tct.GENERAL in u}
        chat = {o for o, u in units.items() if u.startswith("teams:chat:")}
        private = set(units) - general - chat
        quoted = {o for o in units if any(f'"id":"{m}"' in o for m in ("1001", "1002", "3001"))}
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        runtime = CaseRuntime(self.tmp.name, client=None, audience=AudienceMap({}, tenant="plx"),
                              tenant="plx", live=LiveReaders(self.audience),
                              people_exclusion=PeopleExclusion.from_env(tct.ENV))
        self.addCleanup(runtime.close)
        everyone = {"staff:cos", "staff:vince", "staff:teams-member", "staff:teams-outsider"}
        base = ot.OntologyTools(
            reader, ot.ReadPolicy(tenant="plx", audiences={
                "teams:*": everyone, "api:*": everyone | {"staff:nobody"}}),
            item_gate=ot.item_gate_from_case_runtime(runtime),
            schema=ot.SchemaView.from_compiled(schema_loader.load_schema()), grammar=None,
            contracts=CONTRACTS, clock=now)
        nobody = Actor("plx", frozenset({"staff:nobody"}), "restricted")
        for actor, seen in ((tct.MEMBER, set(units)), (tct.COS_ONLY, general | chat),
                            (tct.VINCE_ONLY, general | private), (tct.OUTSIDER, set()),
                            (nobody, set())):
            found = base.call("plx.find", {"query": "freight quote", "limit": 50}, actor)
            hits = {c["object"]["object_id"] for c in found.get("candidates", [])}
            self.assertEqual(hits, quoted & seen, actor)
            read = base.call("plx.object", {"object_id": po}, actor)
            counts = {c["link_type"]: c["count"] for c in read.get("linked_counts", [])}
            self.assertEqual(counts, {f"{kind}.mentions_purchase_order": n for kind, n in (
                ("TeamsChannelMessage", len(quoted & seen & general)),
                ("TeamsChatMessage", len(quoted & seen & chat))) if n}, actor)
            text = json.dumps([found, read])
            for hidden in set(units) - seen:
                self.assertNotIn(hidden, text, actor)
            if not seen:
                self.assertNotIn(tct.SECRET_TEXT, text)


class GraphFamiliesTests(unittest.TestCase):
    """``ontology/generator/graph.py`` over the committed CSDL subset, rules and registry,
    and the source registry reading its endpoint map (evidence: isolated-fixture; the live
    results below are synthetic, shaped as the capture checks record them)."""

    E = "microsoft-graph.endpoint:"

    def test_every_graph_endpoint_is_bound_or_classified_and_the_outputs_are_current(self):
        self.assertEqual(graph.check(), [])   # committed outputs are what the inputs generate
        mapped = json.loads(graph.ENDPOINTS_PATH.read_text(encoding="utf-8"))["endpoints"]
        self.assertEqual(set(mapped), {e["id"] for e in graph.endpoints(sr.load_registry())})
        for item in mapped.values():
            self.assertEqual(len({"source", "not_business_data", "method_unknown",
                                  "unresolved"} & set(item)), 1)
            if "source" in item:   # only a template some upstream request GETs is read
                self.assertIn("GET", item["methods"], item)
            for record in (item.get("decision"), item.get("blocked", {}).get("decision")):
                if record:
                    self.assertTrue((ROOT / record).is_file(), item)

        def outcome(template):
            return {k: v for k, v in mapped[self.E + template].items()
                    if k not in ("resolved", "methods")}
        write = {"not_business_data": "write_operation",
                 "decision": "devloop/DECISIONS/2026-10-07-rec03-production-writes.md"}
        # Upstream only POSTs or PUTs these (a folder create, an upload, a channel post):
        # upstream actions, never read sources, though a rule binds their resource.
        for template, method in (("/drives/{}/{}:/children", "POST"),
                                 ("/sites/{}/drives/{}/{}", "PUT"),
                                 ("/drives/{}/root:/{}:/content", "PUT"),
                                 ("/teams/{}/channels/{}/messages", "POST")):
            self.assertEqual(mapped[self.E + template]["methods"], [method])
            self.assertEqual({k: v for k, v in outcome(template).items() if k != "stopped"},
                             write)
        # A request whose method no rule decides is never guessed to be a read.
        self.assertEqual(mapped[self.E + "/me/messages/{}/$value"]["methods"], ["unknown"])
        self.assertIn("method_unknown", outcome("/me/messages/{}/$value"))
        # A file's own list item is captured with the file; a list's items are list items.
        self.assertEqual(outcome("/drives/{}/root:/{}:/listItem/fields"),
                         {"source": "sharepoint", "relation": "driveItems"})
        self.assertEqual({k: v for k, v in outcome("/sites/{}/lists/{}/items/delta").items()
                          if k != "blocked"}, {"source": "sharepoint", "relation": "listItems"})
        self.assertEqual(outcome("/users/{}/messages/{}"),
                         {"source": "graph_mail", "relation": "messages"})
        self.assertEqual(outcome("/users/{}/sendMail")["not_business_data"], "write_operation")
        self.assertEqual(outcome("/groups/{}/members/$ref")["not_business_data"],
                         "write_operation")
        self.assertEqual(outcome("/users/{}/calendarView")["not_business_data"],
                         "calendar_meetings")
        # Graph v1.0 has no list views (ING-11: a beta-only call), which upstream only
        # creates: a write, where its resolution stopped kept.
        views = outcome("/sites/{}/lists/{}/views")
        self.assertEqual(views["not_business_data"], "write_operation")
        self.assertIn("not a Graph v1.0 member", views["stopped"])
        # A folded path segment is never guessed, only written out as its upstream line reads.
        self.assertEqual(outcome("/drives/{}/{}/{}"), {"as": "/drives/{}/root:/{}",
                                                       "source": "sharepoint",
                                                       "relation": "driveItems"})
        self.assertEqual(json.loads(graph.ENDPOINTS_PATH.read_text(encoding="utf-8"))[
            "counts"]["unresolved"], 0)

    def test_an_unknown_resource_rule_or_field_fails_closed(self):
        model = graph.Csdl.from_doc(json.loads(graph.CSDL_PATH.read_text(encoding="utf-8")))
        families = graph.load_families()
        self.assertIn("unresolved", graph.resolve(model, "/users/{}/messages/{}/notAMember"))
        self.assertIn("unresolved", graph.resolve(model, "/notAnEntitySet"))
        uncovered = dict(families, rules=[r for r in families["rules"]
                                          if "message" not in r["resources"]])
        with self.assertRaisesRegex(graph.GraphError, "no rule"):
            graph.classify(graph.resolve(model, "/users/{}/messages"), uncovered, "x")
        with tempfile.TemporaryDirectory() as tmp:
            for edit, refusal in (
                    (lambda d: d["rules"][-1].update(decision="docs/elsewhere.md"), "decision"),
                    (lambda d: d["rules"][0].update(source="no_such_family"), "unknown source"),
                    (lambda d: d["rules"][0].update(relation_typo="x"), "unknown"),
                    (lambda d: d["operations"].update(media=d["operations"]["action"]),
                     "unknown operation"),
                    (lambda d: d["rules"][0].update(blocked={"reason": "x",
                                                             "decision": "docs/x.md"}),
                     "decision"),
                    (lambda d: d["operations"].pop("write_method"), "write_method missing"),
                    # A declaration writes folded values out; it never rewrites a literal
                    # nor adds a path segment.
                    (lambda d: d["templates"]["/drives/{}/{}/{}"].update(
                        **{"as": "/users/{}/root:/{}"}), "literal segment"),
                    (lambda d: d["templates"]["/drives/{}/{}/{}"].update(
                        **{"as": "/drives/{}/root:/a/b"}), "path segment")):
                doc = json.loads(graph.FAMILIES_PATH.read_text(encoding="utf-8"))
                edit(doc)
                path = Path(tmp) / "families.json"
                path.write_text(json.dumps(doc), encoding="utf-8")
                with self.assertRaisesRegex(graph.GraphError, refusal):
                    graph.load_families(path)
        # A folded value holds no '/', except a drive path it opens as path addressing that
        # the template's own next literal closes.
        self.assertFalse(graph.refines("/users/{}/messages/{}",
                                       "/users/{}/messages/{}/attachments"))
        self.assertFalse(graph.refines("/drives/{}/{}/{}", "/drives/{}/root:/a/b"))
        self.assertTrue(graph.refines("/drives/{}/{}:/children",
                                      "/drives/{}/root:/a/b:/children"))
        self.assertFalse(graph.refines("/drives/{}/{}/x", "/drives/{}/root:/a/b/x"))
        registry = sr.load_registry()
        for edit, refusal in (
                (lambda e: e.pop("methods"), "not upstream HTTP methods"),
                (lambda e: e.update(methods=["GET", "FETCH"]), "not upstream HTTP methods"),
                # A write template needs no declaration: a stale one is refused.
                (lambda e: e.update(methods=["POST"]), "drop its templates declaration")):
            entries = json.loads(json.dumps(registry["entries"]))
            edit(next(e for e in entries if e["id"] == self.E + "/drives/{}/{}/{}"))
            with self.assertRaisesRegex(graph.GraphError, refusal):
                graph.generate(model, {"source": {"commit": "0" * 40, "sha256": "0"}},
                               families, dict(registry, entries=entries))
        for template, edit, refusal in (
                ("/users/{}/messages", {"as": "/users/{}/messages", "evidence": "x"},
                 "resolves without"),   # stale: it resolves as folded
                ("/drives/{}/{}/{}", {"evidence": "PLX_MC:elsewhere.py@0:1"},
                 "not one of the entry's citations"),
                ("/no/such/template", {"as": "/no/such/template", "evidence": "x"},
                 "no registry endpoint")):
            declared = json.loads(json.dumps(families))
            declared["templates"].setdefault(template, {}).update(edit)
            with self.assertRaisesRegex(graph.GraphError, refusal):
                graph.generate(model, {"source": {"commit": "0" * 40, "sha256": "0"}},
                               declared, registry)
        spec = json.loads(json.dumps(families["generate"]["graph_teams"]))
        spec["fields"]["subject"] = "noSuchProperty"
        with self.assertRaisesRegex(graph.GraphError, "no Graph v1.0 property"):
            graph._domain(model, "graph_teams", spec, "pin")

    def test_with_passing_capture_checks_only_classified_or_unread_graph_entries_stay_gaps(self):
        """Passing captures credit the templates of the reads they name (``operations_read``,
        as their adapter tests record them), never every template of a relation they hold:
        a Graph row left open is classified, blocked, or one no named read folds to."""
        reads = {
            "graph.mail_capture": ["graph.folder_messages_delta", "graph.mail_folder",
                                   "graph.message", "graph.message_attachment",
                                   "graph.message_attachments", "graph.user"],
            "capture.sharepoint": ["graph.drive", "graph.drive_children", "graph.drive_item",
                                   "graph.drive_path", "graph.drive_permissions"],
            "graph.teams_capture": ["graph.channel_members", "graph.channel_message_replies",
                                    "graph.channel_messages_delta", "graph.chat_members",
                                    "graph.chat_messages", "graph.team_channels",
                                    "graph.team_members", "graph.user", "graph.user_chats",
                                    "graph.user_joined_teams"]}
        live = [{"_from": "hourly", "generated_at": "2026-10-07T05:00:00Z", "checks": [
            {"id": check, "source": "sharepoint" if check == "capture.sharepoint"
             else "microsoft-graph", "result": "pass", "detail": {"fields": {
                 "operations_read": ops, "relations": {"messages": 3, "driveItems": 3,
                                                       "channelMessages": 4}}}}
            for check, ops in reads.items()]}]
        facts = sr.RepoFacts()
        doc = sr.gaps(sr.load_registry(), live, facts)
        rows = [r for r in doc["uncovered"] if r["system"] == "microsoft-graph"]
        # Every Graph row resolves: none unresolved, none blocked (list items and chats are
        # captured), none not captured without a reason; a template no upstream request is
        # found to GET is not read from.
        self.assertEqual({r["reason"] for r in rows},
                         {"not_business_data", "no_live_check", "graph_method_unknown"})
        read = {op for ops in reads.values() for op in ops}
        for row in rows:
            if row["reason"] == "no_live_check":   # no named read requests the template
                self.assertFalse(facts.graph_operations.get(row["name"], set()) & read, row)
            elif row["reason"] == "graph_method_unknown":
                self.assertNotIn("GET", json.loads(graph.ENDPOINTS_PATH.read_text(
                    encoding="utf-8"))["endpoints"][row["id"]]["methods"], row)
            else:
                self.assertTrue((ROOT / row["decision"]).is_file(), row)
        uncovered = {r["id"] for r in doc["uncovered"]}
        for template in ("/drives/{}/items/{}", "/drives/{}/items/{}/children",
                         "/drives/{}/root:/{}:", "/users/{}/messages/{}",
                         # The same resource addressed another way (by path, under a user's
                         # drive): aligned with the read by its Graph resolution.
                         "/drives/{}/root:/{}:/children", "/users/{}/drive/root",
                         "/users/{}/drive/items/{}/children"):   # evaluated
            self.assertNotIn(self.E + template, uncovered)
        # The capture reads a folder's messages by delta: the listing is another resource,
        # so its template is not credited.
        self.assertIn(self.E + "/users/{}/mailFolders/{}/messages", uncovered)

if __name__ == "__main__":
    unittest.main()
