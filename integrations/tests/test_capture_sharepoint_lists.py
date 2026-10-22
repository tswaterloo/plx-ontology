"""SharePoint list-item capture over an injected Graph transport (evidence: injected-transport).

No network and no credentials: every request goes to an in-process fake Graph holding the
scoped list (the shipped scope's site and list ids), another list on the same site and each
item's permissions. Covers what the capture must hold: GET-only through the closed grammar,
only the scoped list and its columns read, paging with caps and resume at any cardinality,
fail-closed scope and provenance, item permissions as each version's audience (a reader
removed from an item loses its history too), a 403 reported as blocked, the
``sharepoint-list://`` resolver of the case runtime and the projected ``SharePointListItem``
served per reader, and the live-check spec against the fixture run.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from verify import live_verify as lv
from integrations import capture_sharepoint_lists as csl
from integrations import graph_read
from integrations.capture_sharepoint import SharePointCaptureStore
from integrations.capture_sharepoint_lists import ListCapture, ListCaptureError, ListScope
from integrations.live_source_guard import LiveSourceAudience
from integrations.source_mirror import AudienceMap
from ontology import schema as schema_loader
from ontology.project import SharePointCaptures, build_store
from ontology.types import Actor
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.cases import CitationUnresolvable
from operating.ontology_host import CaseHostTools

ROOT = Path(__file__).resolve().parents[2]
ENV = {"PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e1",
       "MICROSOFT_GRAPH_TENANT_ID": "11111111-2222-3333-4444-555555555555",
       "MICROSOFT_GRAPH_CLIENT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
       "MICROSOFT_GRAPH_CLIENT_SECRET": "s3cr3t~CLIENT~value~do-not-leak"}
SHIPPED = json.loads(csl.DEFAULT_SCOPE.read_text())["lists"][0]
SITE, LIST = SHIPPED["site_id"], SHIPPED["list_id"]
OTHER = "0000cccc-0000-0000-0000-000000000099"   # another list on the same site, never named
OWNER, OWNER_ID = "owner.test@example-plx.com", "0000aaaa-0000-0000-0000-0000000000a1"
VENDOR = "Acme Bottles Test Vendor"

CAPTURE = Actor("plx", frozenset({"svc:sharepoint-list-capture"}), "restricted")
READER = Actor("plx", frozenset({"staff:list-reader"}), "restricted")
OUTSIDER = Actor("plx", frozenset({"staff:list-outsider"}), "restricted")
WITHHELD = Actor("plx", frozenset({"staff:list-withheld"}), "restricted")


def j(status, value):
    return status, {}, json.dumps(value).encode()


def owner_grant() -> list[dict]:
    return [{"id": "1", "roles": ["owner"],
             "grantedToV2": {"user": {"email": OWNER, "id": OWNER_ID}}}]


class FakeListGraph:
    """Graph for one site holding the scoped list and another list."""

    def __init__(self, items: int = 5) -> None:
        self.lists = {LIST: {"template": "genericList", "items": {}},
                      OTHER: {"template": "genericList", "items": {}}}
        for n in range(1, items + 1):
            self.add(n)
        self.add(1, list_id=OTHER)
        self.perms: dict[str, list[dict]] = {}
        self.forbidden: list[str] = []
        # The status each version answers item permissions with (None: served).
        self.v1_permissions: int | None = None
        self.beta_permissions: int | None = None
        self.calls: list[tuple[str, str]] = []

    def add(self, n: int, *, list_id: str = LIST, price: str = "1.25", **extra) -> None:
        self.lists[list_id]["items"][str(n)] = {
            "id": str(n), "eTag": f'"e{n},{extra.pop("version", 1)}"',
            "createdDateTime": "2026-09-01T10:00:00Z",
            "lastModifiedDateTime": "2026-09-02T10:00:00Z",
            "webUrl": f"https://example.invalid/Lists/VendorQuotes/{n}_.000",
            "contentType": {"id": "0x0100AA", "name": "Item"},
            "parentReference": {"siteId": SITE},
            "fields": {"Title": f"SP-2026-{n:03d}", "VendorName": VENDOR,
                       "QuotedUnitPrice": f"__{price}__", "MOQ": 5000,
                       "QuoteValidUntil": "2026-12-31", "InternalNote": "never captured"},
            **extra}

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url))
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return j(200, {"access_token": "eyJ.TOKEN-do-not-leak.sig", "expires_in": 3599})
        assert method == "GET", method
        path, query = unquote(parts.path), parse_qs(parts.query)
        if any(re.search(p, path) for p in self.forbidden):
            return j(403, {"error": {"code": "accessDenied"}})
        found = re.fullmatch(r"/(v1\.0|beta)/sites/([^/]+)/lists/([^/]+)(/items)?"
                             r"(?:/(\d+)/permissions)?", path)
        held = self.lists.get(found[3]) if found and found[2] == SITE else None
        if held is None or found[1] == "beta" and not found[5]:
            return j(404, {"error": {"code": "itemNotFound"}})
        refused = {"v1.0": self.v1_permissions, "beta": self.beta_permissions}[found[1]]
        if found[5] and refused:
            return j(refused, {"error": {"code": "BadRequest"}})
        if found[5]:
            return j(200, {"value": self.perms.get(found[5], owner_grant())})
        if not found[4]:
            return j(200, {"id": found[3], "list": {"template": held["template"]}})
        items = sorted(held["items"].values(), key=lambda item: int(item["id"]))
        top, start = int(query["$top"][0]), int(query.get("$skiptoken", ["0"])[0])
        page = {"value": items[start:start + top]}
        if start + top < len(items):
            rest = {k: v[0] for k, v in query.items() if k != "$skiptoken"}
            page["@odata.nextLink"] = (f"https://graph.microsoft.com{parts.path}?"
                                       + urlencode({**rest, "$skiptoken": str(start + top)}))
        # A JSON number as Graph sends it (the fake's "__1.25__" marker keeps it unrounded).
        return 200, {}, re.sub(r'"__([0-9.]+)__"', r"\1", json.dumps(page)).encode()

    def gets(self) -> list[str]:
        return [unquote(urlsplit(url).path) for method, url in self.calls if method == "GET"]


def audience() -> LiveSourceAudience:
    return LiveSourceAudience(
        {"sharepoint:list:*": ["svc:sharepoint-list-capture", "staff:list-reader",
                               "staff:list-outsider"]},
        tenant="plx", classifications={"sharepoint:list:*": "restricted"},
        identities={"staff:list-reader": [OWNER],
                    "staff:list-outsider": ["outsider@example-plx.com"]},
        source_acl_required=["sharepoint:list:*"])


def scope(**caps) -> ListScope:
    return ListScope({"schema": csl.SCOPE_SCHEMA, "lists": [SHIPPED], **caps})


class CaptureCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.graph = FakeListGraph()
        self.audience = audience()
        self.store = csl.open_store(self.tmp.name, self.audience)
        self.addCleanup(self.store.close)
        api, self.transport = graph_read.build_graph(ENV, sender=self.graph)
        self.capture = ListCapture(self.store, api, self.audience, CAPTURE)
        self.unit = scope().lists[0].unit

    def visible(self, actor: Actor) -> list[dict]:
        try:
            return self.store.records(actor, csl.LIST_KIND.name, self.unit, limit=1000)["items"]
        except csl.CaptureError:
            return []


class CaptureTests(CaptureCase):
    def test_the_scoped_list_is_captured_across_pages_and_no_other_list_is_read(self):
        for n in (1, 10, 60):   # cardinality: no row lost, duplicated or out of order
            with self.subTest(items=n):
                self.graph.lists[LIST]["items"] = {}
                for i in range(1, n + 1):
                    self.graph.add(i)
                self.graph.calls.clear()
                run = self.capture.run(scope(page_size=7))
                self.assertTrue(run["complete"], run)
                rows = self.visible(READER)
                self.assertEqual(sorted(int(r["key"]["id"]) for r in rows), list(range(1, n + 1)))
                self.assertEqual(run["lists"][0]["counts"]["pages"], -(-n // 7))
                self.assertTrue(all(p.startswith(f"/v1.0/sites/{SITE}/lists/{LIST}")
                                    for p in self.graph.gets()), self.graph.gets())
        self.assertNotIn(OTHER, json.dumps(self.graph.calls))
        self.assertEqual({op for op in self.transport.operations_read},
                         {"graph.site_list", "graph.list_items", "graph.list_item_permissions"})
        row = self.visible(READER)[0]["row"]
        # Only the scope's columns; numbers as exact decimal text; the generated key paths.
        self.assertNotIn("InternalNote", row["fields"])
        self.assertEqual(row["fields"]["QuotedUnitPrice"], "1.25")
        self.assertEqual((row["parentReference"]["siteId"], row["sharepointIds"]["listId"]),
                         (SITE, LIST))

    def test_caps_pause_the_run_and_the_next_runs_resume_it(self):
        capped = scope(page_size=2, max_pages_per_run=1)
        first = self.capture.run(capped)["lists"][0]
        self.assertEqual((first["status"], first["reasons"]), ("incomplete", ["cap:page_cap"]))
        later = [self.capture.run(capped)["lists"][0] for _ in range(2)]
        self.assertEqual([r["resumed_from"] for r in later], ["cursor", "cursor"])
        self.assertEqual(later[-1]["status"], "complete")
        self.assertEqual(sorted(r["key"]["id"] for r in self.visible(READER)),
                         ["1", "2", "3", "4", "5"])
        self.assertEqual(self.store.operator_completeness(
            CAPTURE, csl.LIST_KIND.name, self.unit)["versions_total"], 5)

    def test_an_unknown_scope_a_library_or_a_foreign_item_fails_closed(self):
        for bad in ({"schema": csl.SCOPE_SCHEMA, "lists": [SHIPPED], "discover": True},
                    {"schema": csl.SCOPE_SCHEMA, "lists": [{**SHIPPED, "list_id": "Vendor"}]},
                    {"schema": csl.SCOPE_SCHEMA, "lists": [SHIPPED, SHIPPED]},
                    {"schema": csl.SCOPE_SCHEMA, "lists": [{**SHIPPED, "columns": ["a b"]}]},
                    {"schema": csl.SCOPE_SCHEMA, "lists": [SHIPPED], "page_size": 0}):
            with self.assertRaises(ListCaptureError):
                ListScope(bad)
        self.graph.lists[LIST]["template"] = "documentLibrary"
        result = self.capture.run(scope())["lists"][0]
        self.assertEqual(result["reasons"], ["document_library"])
        self.assertEqual(self.graph.gets(), [f"/v1.0/sites/{SITE}/lists/{LIST}"])
        self.graph.lists[LIST]["template"] = "genericList"
        self.graph.add(6, parentReference={"siteId": "other-my.sharepoint.com,"
                                           + OTHER + "," + OTHER})
        result = self.capture.run(scope())["lists"][0]
        self.assertEqual((result["status"], result["reasons"]),
                         ("incomplete", ["provenance_dropped"]))
        self.assertNotIn("6", {r["key"]["id"] for r in self.visible(READER)})
        withheld = ListCapture(self.store, self.capture.api, self.audience, WITHHELD)
        self.graph.calls.clear()
        self.assertEqual(withheld.run(scope())["lists"][0]["reasons"], ["not_visible"])
        self.assertEqual(self.graph.gets(), [])   # no I/O for a list the actor may not read


class AudienceTests(CaptureCase):
    def test_item_permissions_trim_every_reader_and_a_revoked_reader_loses_history(self):
        self.graph.perms["2"] = [{"id": "9", "roles": ["read"], "grantedToV2": {
            "user": {"email": "someone.else@example-plx.com"}}}]
        self.assertTrue(self.capture.run(scope())["complete"])
        rows = {r["key"]["id"]: r for r in self.visible(READER)}
        self.assertEqual(sorted(rows), ["1", "3", "4", "5"])   # item 2 is not shared with it
        self.assertEqual((self.visible(OUTSIDER), self.visible(WITHHELD)), ([], []))
        old = rows["1"]["citation"]
        self.assertTrue(old.startswith(f"sharepoint-list://{SITE.replace(',', '%2C')}/{LIST}/1@"))
        for actor in (OUTSIDER, WITHHELD):
            self.assertEqual(self.store.resolve(old, actor)["error"], "not_visible")
        # The item is edited, then its sharing narrowed past the reader: both versions hide.
        self.graph.add(1, price="1.40", version=2)
        self.capture.run(scope())
        self.assertEqual(self.store.resolve(old, READER)["status"], "superseded")
        self.graph.perms["1"] = self.graph.perms["2"]
        self.capture.run(scope())
        self.assertEqual(self.store.resolve(old, READER)["error"], "not_visible")
        self.assertNotIn("1", {r["key"]["id"] for r in self.visible(READER)})

    def test_a_refused_permission_read_is_blocked_and_its_items_serve_no_reader(self):
        self.graph.forbidden = [r"/permissions$"]
        run = self.capture.run(scope())
        self.assertEqual(run["lists"][0]["reasons"],
                         ["blocked:missing_permission:Sites.Read.All"])
        # The one Graph refusal shape (mail and Teams build the same part).
        self.assertEqual(run["blocked_upstream"], [{
            "part": "sharepoint_list.item_permissions", "source": "microsoft-graph",
            "http_status": 403, "error_code": "missing_permission",
            "permission": "Sites.Read.All", "cause": "host_misconfiguration"}])
        # One refused read per endpoint (v1.0, then its beta fallback), not one per item;
        # the items are kept, served to no reader.
        self.assertEqual([p for p in self.graph.gets() if p.endswith("/permissions")],
                         [f"/{v}/sites/{SITE}/lists/{LIST}/items/1/permissions"
                          for v in ("v1.0", "beta")])
        self.assertEqual(self.store.operator_completeness(
            CAPTURE, csl.LIST_KIND.name, self.unit)["records_current"], 5)
        self.assertEqual(self.visible(READER), [])


class ResolverTests(CaptureCase):
    """The ``sharepoint-list://`` citation through the case runtime and the projected
    ``SharePointListItem`` through the ontology host, per reader."""

    def setUp(self) -> None:
        super().setUp()
        self.graph.perms["2"] = []   # shared with nobody the audience names
        self.assertTrue(self.capture.run(scope())["complete"])
        self.runtime = CaseRuntime(self.tmp.name, client=None,
                                   audience=AudienceMap({}, tenant="plx"), tenant="plx",
                                   live=LiveReaders(self.audience))
        self.addCleanup(self.runtime.close)
        self.cited = {r["key"]["id"]: r["citation"] for r in self.visible(READER)}

    def test_a_cited_item_resolves_for_a_permitted_actor_and_for_no_other(self):
        view = self.runtime.resolve_citation(self.cited["1"], READER)
        self.assertEqual((view["source"], view["row"]["fields"]["VendorName"]),
                         ("sharepoint-list", VENDOR))
        self.assertTrue(self.runtime.citation_status(self.cited["1"], READER)["ok"])
        for actor in (OUTSIDER, WITHHELD):
            with self.assertRaises(CitationUnresolvable):
                self.runtime.resolve_citation(self.cited["1"], actor)
            self.assertFalse(self.runtime.citation_status(self.cited["1"], actor)["ok"])

    def test_the_projected_list_items_are_served_only_to_readers_the_items_grant(self):
        SharePointCaptureStore(self.tmp.name).db.close()   # an empty drive capture beside it
        path = Path(self.tmp.name) / "projection.sqlite"
        with SharePointCaptures(self.tmp.name) as captured:
            self.assertIsNone(captured.capture_gap("listItems"))
            build_store(path, schema_loader.load_schema(), {"sharepoint": captured},
                        clock=lambda: dt.datetime.now(dt.timezone.utc))
        find = {"object_type": "SharePointListItem", "limit": 50,
                "filters": [{"property": "id", "op": "present"}]}
        found = {}
        for actor in (READER, OUTSIDER, WITHHELD):
            host = CaseHostTools(self.runtime, actor, store_path=path)
            self.addCleanup(host.close)
            out = host.call("plx.find", find, actor)
            found[actor] = len(out["candidates"])
            if actor is not READER:
                self.assertNotIn(VENDOR, json.dumps(out), actor)
                self.assertNotIn("sharepoint-list://", json.dumps(out), actor)
        self.assertEqual(found, {READER: 4, OUTSIDER: 0, WITHHELD: 0})


class ProbeTests(unittest.TestCase):
    """``python3 -m integrations.graph_read sharepoint-list-capture`` with the live-check
    spec's own argv over the fake Graph: its expectations hold on the fixture output."""

    SPEC = json.loads((ROOT / lv.SPECS_DIR_NAME / "capture.sharepoint_list_items.json")
                      .read_text())

    def run_spec(self, graph: FakeListGraph) -> tuple[int, dict]:
        audience_doc = json.loads((ROOT / self.SPEC["command"][7]).read_text())
        with tempfile.TemporaryDirectory() as tmp:
            # The fixture's owner is the reader the example audience names.
            graph.perms = {i: [{"id": "1", "roles": ["owner"], "grantedToV2": {"user": {
                "email": audience_doc["identities"]["staff:list-reader"][0]}}}]
                for i in graph.lists[LIST]["items"]}
            argv = [a.replace("{tmp}", tmp) for a in self.SPEC["command"][3:]]
            argv = [str(ROOT / a) if a.startswith("examples/") else a for a in argv]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = graph_read.main(argv, environ=ENV, sender=graph)
        return code, json.loads(out.getvalue())

    def test_the_spec_passes_on_the_fixture_and_prints_no_content(self):
        graph = FakeListGraph()
        graph.v1_permissions = 400   # v1.0 refuses listItem permissions: the beta read passes
        code, report = self.run_spec(graph)
        self.assertEqual(lv.check_expectations(self.SPEC, code, report), [], report)
        self.assertIn("graph.list_item_permissions_beta", report["operations_read"])
        code, report = self.run_spec(FakeListGraph())
        self.assertEqual(lv.check_expectations(self.SPEC, code, report), [], report)
        self.assertNotIn("graph.list_item_permissions_beta", report["operations_read"])
        self.assertEqual(report["evidence_label"], "injected-transport")
        self.assertEqual(lv.completeness(self.SPEC, report)["complete"], True)
        self.assertEqual(lv._recorded(self.SPEC, report)["relations"], {"listItems": 5})
        self.assertEqual(report["leak_check"]["outsider_actor_withheld"], 5)
        text = json.dumps(report)
        for value in (VENDOR, "SP-2026-001", SITE, LIST, "1.25", "do-not-leak", "TOKEN"):
            self.assertNotIn(value, text)

    def test_a_refused_list_read_is_blocked_not_failed(self):
        graph = FakeListGraph()
        graph.forbidden = [r"/items$"]
        code, report = self.run_spec(graph)
        self.assertEqual(lv.check_expectations(self.SPEC, code, report,
                                               self.SPEC["expect_blocked"]), [], report)
        self.assertEqual([(p["part"], p["permission"]) for p in report["blocked_upstream"]],
                         [("sharepoint_list.items", "Sites.Read.All")])


if __name__ == "__main__":
    unittest.main()
