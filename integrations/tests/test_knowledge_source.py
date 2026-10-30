"""Brain knowledge as a namespace-trimmed ontology source (GOV-13).

Evidence label: injected-transport. Every request goes through the real
``BrainReadClient`` and ``HubTransport`` guard into a fake sender; no network I/O.
Item shape: agentic-swarm ``apps/vmc-web/src/lib/vmc/knowledge/items-core.ts:70-91``
@ e228067c19 (the repo-level pin).
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from integrations.capture_store import CaptureStore, parse_handle
from integrations.http import SourceUnavailable
from integrations.knowledge_source import (NamespaceAuthority, NamespaceDenied, capture,
                                           store_path)
from integrations.live_source_guard import Secret
from integrations.plx_hub_guard import BrainConfig
from integrations.tests.test_mc_brain_guard import VMC, VMC_KEY, FakeSender, hub, ok_json
from integrations.vmc import BrainReadClient
from ontology import schema as schema_loader
from ontology.json_contract import validate
from ontology.project import build_store, capture_source
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_traverse as tt

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
NOW = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)
ENG = Actor("plx", frozenset({"staff:engineering"}))
OPS = Actor("plx", frozenset({"staff:operations"}))
AUTHORITY = NamespaceAuthority({
    "swarm/rollup/company": ["staff:engineering", "staff:operations"],
    "swarm/rag/*": ["staff:engineering"],
    "swarm/rag/finance": [],        # an exact entry denies one namespace of a family
}, tenant="plx")


def item(n, namespace, title):
    return {"kind": "memory", "id": f"00000000-0000-4000-8000-{n:012d}", "title": title,
            "snippet": f"{title} snippet", "namespaceKey": namespace, "memoryType": "note",
            "projectSlug": None, "sourceType": "lesson", "tags": ["t"], "externalUrl": None,
            "createdAt": "2026-09-01T00:00:00Z", "updatedAt": "2026-09-02T00:00:00Z"}


ITEMS = [item(1, "swarm/rollup/company", "Company brief release cadence"),
         item(2, "swarm/rag/portal", "Portal costing module runbook"),
         item(3, "swarm/rag/finance", "Finance close runbook"),
         item(4, "swarm/personal-notes", "Operator private runbook"),
         item(5, "swarm/lessons/auto/dev", "Unrequested runbook lesson")]


def brain(items=ITEMS, grow=0, edit=False):
    """A brain serving ``items``; ``grow`` new items appear in front after the first page
    (``order=recent`` under a concurrent write); ``edit`` moves the last item to the
    front, newer, after the first page (an edit: the total does not move)."""
    served = list(items)

    def respond(method, url, headers, body):
        query = parse_qs(urlsplit(url).query)
        offset, limit = int(query.get("offset", ["0"])[0]), int(query["limit"][0])
        page = served[offset:offset + limit]
        total = len(served)
        if grow and offset == 0:
            served[:0] = [item(900 + n, "swarm/rag/portal", "New runbook") for n in range(grow)]
        if edit and offset == 0 and limit < len(served) and not respond.edited:
            respond.edited = True
            served.insert(0, {**served.pop(), "updatedAt": "2026-10-05T11:00:00Z"})
        return ok_json({"data": {"items": page, "total": total, "enabled": True}})
    respond.edited = False
    sender = FakeSender(fn=respond)
    transport, _ = hub(sender)
    return BrainReadClient(BrainConfig(VMC, Secret(VMC_KEY), "vmc-fallback"),
                           hub=transport), sender


class KnowledgeSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        store_path(self.state).parent.mkdir(parents=True)
        self.store = CaptureStore(store_path(self.state), AUTHORITY,
                                  clock=lambda: NOW - dt.timedelta(hours=1))
        self.addCleanup(self.store.close)

    def test_namespace_authority_fails_closed_before_any_request(self):
        for actor, namespaces, code in (
                (OPS, ["swarm/rag/*"], "knowledge_namespace_denied"),
                (ENG, ["swarm/rag/finance"], "knowledge_namespace_denied"),
                (ENG, ["swarm/lessons/*"], "knowledge_namespace_unknown"),
                (ENG, ["swarm/rollup/company", "dept/qa/"], "knowledge_namespace_unknown"),
                (ENG, ["swarm/personal-notes"], "knowledge_namespace_personal"),
                (Actor("other", ENG.principals), ["swarm/rag/*"], "knowledge_namespace_denied")):
            client, sender = brain()
            with self.assertRaises(NamespaceDenied, msg=namespaces) as caught:
                capture(client, self.store, AUTHORITY, actor, namespaces)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(sender.calls, [])
        for pattern in ("*", "swarm/*", "swarm/personal/x", "personal-notes/*",
                        "Swarm/Personal/x", "Swarm/Personal/*", "SWARM/*", "S*",
                        "Personal-Notes/*"):
            with self.assertRaises(ValueError, msg=pattern):
                NamespaceAuthority({pattern: ["staff:engineering"]}, tenant="plx")

    def test_capture_is_get_only_and_keeps_only_granted_requested_namespaces(self):
        client, sender = brain()
        report = capture(client, self.store, AUTHORITY, ENG,
                         ["swarm/rollup/company", "swarm/rag/*"], page_size=2)
        self.assertEqual({c[0] for c in sender.calls}, {"GET"})
        self.assertEqual({urlsplit(c[1]).path for c in sender.calls},
                         {"/api/vmc/knowledge/agent/items"})
        self.assertIsNone(sender.calls[0][3])
        self.assertEqual(report["requests"], 4)  # 3 pages and the front re-read
        self.assertTrue(report["complete"])
        self.assertEqual((report["kept"], report["withheld"], report["dropped_personal"],
                          report["dropped_unrequested"]), (2, 1, 1, 1))
        stored = {r[0] for r in self.store._db.execute("SELECT unit FROM versions")}
        self.assertEqual(stored, {"brain:swarm/rollup/company", "brain:swarm/rag/portal"})

    def test_cardinality_no_item_lost_or_duplicated(self):
        for n in (1, 10, 60):
            with tempfile.TemporaryDirectory() as tmp, \
                    CaptureStore(Path(tmp) / "c.db", AUTHORITY, clock=lambda: NOW) as store:
                items = [item(i, "swarm/rag/portal", f"runbook {i}") for i in range(n)]
                client, sender = brain(items + items[: n // 2])  # repeats across pages
                report = capture(client, store, AUTHORITY, ENG, ["swarm/rag/*"], page_size=7)
                self.assertEqual((report["kept"], report["complete"]), (n, True), n)
                self.assertEqual(len(store.current_versions("brain", "brain:swarm/rag/portal")),
                                 n)
                pages = -(-(n + n // 2) // 7)
                self.assertEqual(report["requests"], pages + (pages > 1))

    def test_completeness_rereads_and_unknown_shapes(self):
        client, _ = brain()
        capture(client, self.store, AUTHORITY, ENG, ["swarm/rollup/company", "swarm/rag/*"])
        # A full re-read where swarm/rag/portal is gone marks its item missing, though the
        # listing named no item of that namespace.
        client, _ = brain([i for i in ITEMS if i["namespaceKey"] != "swarm/rag/portal"])
        report = capture(client, self.store, AUTHORITY, ENG, ["swarm/rag/*"])
        self.assertEqual((report["kept"], report["units"], report["complete"]), (0, 1, True))
        self.assertEqual(self.store.current_versions("brain", "brain:swarm/rag/portal"), [])
        self.assertEqual(self.store.run("brain", "brain:swarm/rag/portal")["state"], "complete")
        # A total that moves during the read is never complete, and claims no absence.
        client, _ = brain(ITEMS, grow=1)
        report = capture(client, self.store, AUTHORITY, ENG, ["swarm/rag/*"], page_size=2)
        self.assertFalse(report["complete"])
        run = self.store.run("brain", "brain:swarm/rag/portal")
        self.assertEqual((run["state"], run["reason"]), ("partial", "source_changed_during_read"))
        # An item edited between pages moves to the front, past the offset read: the
        # front re-read differs, so the run is partial and the unread item is not missing.
        items = [item(i, "swarm/rag/portal", f"runbook {i}") for i in range(6)]
        client, _ = brain(items)
        capture(client, self.store, AUTHORITY, ENG, ["swarm/rag/*"], page_size=2)
        client, _ = brain(items, edit=True)
        report = capture(client, self.store, AUTHORITY, ENG, ["swarm/rag/*"], page_size=2)
        self.assertEqual((report["kept"], report["complete"]), (5, False))
        run = self.store.run("brain", "brain:swarm/rag/portal")
        self.assertEqual((run["state"], run["reason"]), ("partial", "source_changed_during_read"))
        self.assertEqual(len(self.store.current_versions("brain", "brain:swarm/rag/portal")), 6)
        # An unexpected response shape fails closed before anything is stored.
        before = self.store._db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        for data in ({"items": ITEMS}, {"items": "x", "total": 5}, {"enabled": False},
                     {"items": ITEMS, "total": True}):
            sender = FakeSender(fn=lambda *a, data=data: ok_json({"data": data}))
            transport, _ = hub(sender)
            client = BrainReadClient(BrainConfig(VMC, Secret(VMC_KEY), "vmc-fallback"),
                                     hub=transport)
            with self.assertRaises(SourceUnavailable, msg=data):
                capture(client, self.store, AUTHORITY, ENG, ["swarm/rag/*"])
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
                         before)

    def test_projection_cites_brain_and_find_trims_per_namespace(self):
        client, _ = brain()
        capture(client, self.store, AUTHORITY, ENG, ["swarm/rollup/company", "swarm/rag/*"])
        compiled = schema_loader.load_schema()
        path = self.state / "ontology.sqlite"
        with capture_source("brain", store_path(self.state)) as source:
            report = build_store(path, compiled, {"brain": source}, clock=lambda: NOW)
        self.assertNotIn("KnowledgeItem", json.dumps(report.get("gaps", [])))
        reader = ot.StoreReader.open(path)
        self.addCleanup(reader.conn.close)
        keys = reader.audience_keys()

        def call(actor, tool, arguments):
            tools = ot.OntologyTools(reader, ot.ReadPolicy("plx", AUTHORITY.grants(actor, keys)),
                                     schema=ot.SchemaView.from_compiled(compiled), grammar=None,
                                     contracts=CONTRACTS, clock=lambda: NOW)
            return (tt.TraverseTools(tools) if tool in tt.TOOL_NAMES else tools).call(
                tool, arguments, actor)

        def find(actor, query):
            result = call(actor, "plx.find", {"query": query, "object_type": "KnowledgeItem"})
            self.assertEqual(validate(result, CONTRACTS["tools"]["plx.find"]["output"],
                                      root=CONTRACTS), [], json.dumps(result)[:1500])
            return result

        found = find(ENG, "runbook")
        self.assertEqual(len(found["candidates"]), 1)
        candidate = found["candidates"][0]
        self.assertEqual(candidate["tier"], "lead")
        origin = candidate["matched"]["raw"]["origin"]
        kind, record_path, _, _ = parse_handle(origin["citation"])
        self.assertEqual((kind.name, record_path[0], origin["field_path"]),
                         ("brain", "swarm/rag/portal", "row.title"))
        self.assertNotIn("Finance close", json.dumps(found))
        self.assertEqual(find(OPS, "runbook")["candidates"], [])
        self.assertEqual(len(find(OPS, "release cadence")["candidates"]), 1)
        # A reader the authority names nowhere sees nothing, as if the store had none.
        stranger = Actor("plx", frozenset({"staff:visitor"}))
        self.assertEqual(find(stranger, "release cadence")["candidates"], [])
        # Every other consumer of the object: the granted reader opens it, an ungranted
        # one gets exactly what an unknown id gets.
        object_id = candidate["object"]["object_id"]
        opened = call(ENG, "plx.object", {"object_id": object_id})
        self.assertIn("Portal costing", json.dumps(opened))
        unknown = object_id.replace("000000000002", "000000000999")
        for tool, arguments in (("plx.object", {"object_id": object_id}),
                                ("plx.timeline", {"from": object_id})):
            denied = call(OPS, tool, arguments)
            self.assertEqual(denied["error"]["code"], "object_not_found", tool)
            self.assertNotIn("Portal costing", json.dumps(denied), tool)
            self.assertEqual(json.dumps(denied, sort_keys=True).replace(object_id, "X"),
                             json.dumps(call(OPS, tool, {**arguments, **(
                                 {"object_id": unknown} if "object_id" in arguments
                                 else {"from": unknown})}),
                                 sort_keys=True).replace(unknown, "X"), tool)


    def test_live_check_command_meets_its_own_expectations(self):
        from contextlib import redirect_stdout
        import io

        from verify import live_verify
        from integrations import knowledge_source

        spec = json.loads((ROOT / "devloop/live-checks/knowledge.brain_source.json")
                          .read_text("utf-8"))
        self.assertIn(spec["command"][2], live_verify.ALLOWED_MODULES)
        with tempfile.TemporaryDirectory() as tmp:
            argv = [tmp if a == "{tmp}" else a for a in spec["command"][3:]]
            client, sender = brain()
            out = io.StringIO()
            with redirect_stdout(out):
                code = knowledge_source.main(argv, client=client)
        report = json.loads(out.getvalue())
        self.assertEqual(live_verify.check_expectations(spec, code, report), [], report)
        self.assertGreater(report["granted_reader_candidates"], 0)
        # The relation the source registry credits on a pass (registry_names).
        self.assertEqual(live_verify._recorded(spec, report)["relations"],
                         {"items": report["kept"]})
        # An empty brain cannot pass: each proof count has a positive minimum.
        minimums = ("kept", "projected_objects", "granted_reader_candidates")
        self.assertTrue(all(spec["expect"]["json_min"].get(k, 0) > 0 for k in minimums))
        for key in minimums:
            self.assertNotEqual(
                live_verify.check_expectations(spec, code, {**report, key: 0}), [], key)
        self.assertEqual({c[0] for c in sender.calls}, {"GET"})
        # Every recorded field is a count, a boolean or an enum: no item text leaves.
        self.assertEqual(live_verify.summarize(report, enums=True), report)
        # The verifier's own path (ENV_GUARD runs the module as ``__main__``) with its
        # environment shape: the run reaches the brain configuration and reports a JSON
        # verdict, never a traceback (it once died on a second ``brain`` registration).
        # The host is one the pack does not name, so it fails closed before any request.
        environ = {"PATH": os.environ.get("PATH", ""), "VMC_API_KEY": "not-a-real-key",
                   "VMC_API_URL": "https://unnamed-host.invalid"}
        entry = live_verify.run_spec(spec, environ=environ)
        self.assertEqual((entry["result"], entry["exit_code"]), ("fail", 1), entry)
        self.assertEqual(entry["detail"]["fields"]["error"], "source_unavailable", entry)
        self.assertNotIn("stderr", entry["detail"])
        # A grant that is no namespace pattern is a JSON refusal before any request.
        client, sender = brain()
        out = io.StringIO()
        with redirect_stdout(out):
            code = knowledge_source.main(["--state-dir", self.tmp.name, "--grant", "bad grant"],
                                         client=client)
        self.assertEqual((code, json.loads(out.getvalue()), sender.calls),
                         (1, {"ok": False, "error": "knowledge_namespace_pattern_invalid",
                              "error_class": "ValueError"}, []))


if __name__ == "__main__":
    unittest.main()
