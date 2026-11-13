"""SharePoint list discovery (coverage audit 2026-10-30: one list captured, the lists
available never counted): a scope's ``discover`` block lists every tenant site's lists
(read-only Graph), leaves out libraries, hidden and system lists, excluded lists and every
OneDrive, reads each list's own columns (never SharePoint's bookkeeping or hidden ones)
and captures each list found exactly as a named one: audience grant first, item
permissions per item, every reader trimmed. A failed site read keeps discovery
incomplete; ``--discover-only`` reads no item. Fixtures: the fake Graph of
``test_capture_sharepoint_lists``. Evidence label: injected-transport."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from integrations import capture_sharepoint_lists as csl, graph_read
from integrations.capture_sharepoint_lists import ListCapture, ListCaptureError, ListScope
from integrations.tests import test_capture_sharepoint_lists as tl

TENANT = "petrasoap.sharepoint.com"
SITE_A = f"{TENANT},0000aaaa-1111-4222-8333-000000000001,0000aaaa-1111-4222-8333-000000000002"
PERSONAL = ("petrasoap-my.sharepoint.com,0000bbbb-1111-4222-8333-000000000001,"
            "0000bbbb-1111-4222-8333-000000000002")


def gid(n: int) -> str:
    return f"0000dddd-0000-4000-8000-{n:012d}"


class EstateGraph(tl.FakeListGraph):
    """The list fake plus the estate: a tenant site whose lists are discovered and a
    OneDrive whose lists must never be listed."""

    def __init__(self, lists: int = 3) -> None:
        super().__init__()
        self.site_lists: list[dict] = []
        self.site_items: dict[str, dict] = {}
        for n in range(1, lists + 1):
            self.site_list(gid(n), f"Tracker {n}")
        self.site_list(gid(900), "Documents", template="documentLibrary")
        self.site_list(gid(901), "Hidden", hidden=True)
        self.site_list(gid(902), "User Information List", system=True)
        self.failing_sites: set[str] = set()

    def site_list(self, list_id: str, name: str, *, template: str = "genericList",
                  hidden: bool = False, system: bool = False) -> None:
        raw = {"id": list_id, "displayName": name,
               "list": {"template": template, "hidden": hidden}}
        if system:
            raw["system"] = {}
        self.site_lists.append(raw)
        self.site_items[list_id] = {"1": {
            "id": "1", "eTag": '"x1,1"', "createdDateTime": "2026-09-01T10:00:00Z",
            "contentType": {"id": "0x0100BB"}, "parentReference": {"siteId": SITE_A},
            "fields": {"Title": f"{name} row", "LotNumber": "L-7", "ContentType": "Item",
                       "Secret": "hidden column", "Modified": "2026-09-02T10:00:00Z"}}}

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        parts = urlsplit(url)
        path = unquote(parts.path)
        if parts.hostname == "graph.microsoft.com":
            if path == "/v1.0/sites/getAllSites":
                self.calls.append((method, url))
                return tl.j(200, {"value": [
                    {"id": SITE_A, "webUrl": f"https://{TENANT}/sites/ops"},
                    {"id": PERSONAL, "webUrl": "https://petrasoap-my.sharepoint.com/personal/x"}]})
            found = re.fullmatch(r"/v1\.0/sites/([^/]+)/lists(?:/([^/]+)(/columns|/items"
                                 r"(?:/(\d+)/permissions)?)?)?", path)
            if found and found[1] == SITE_A:
                self.calls.append((method, url))
                if SITE_A in self.failing_sites:
                    return tl.j(403, {"error": {"code": "accessDenied"}})
                if found[2] is None:
                    return tl.j(200, {"value": self.site_lists})
                if found[3] == "/columns":
                    return tl.j(200, {"value": [
                        {"name": "Title"}, {"name": "LotNumber"}, {"name": "ContentType"},
                        {"name": "Modified"}, {"name": "Secret", "hidden": True}]})
                if found[4]:
                    return tl.j(200, {"value": tl.owner_grant()})
                if found[3] == "/items":
                    query = parse_qs(parts.query)
                    assert "$top" in query
                    return tl.j(200, {"value": list(self.site_items[found[2]].values())})
                held = next(raw for raw in self.site_lists if raw["id"] == found[2])
                return tl.j(200, {"id": held["id"], "list": held["list"]})
        return super().__call__(method, url, headers, body, timeout, max_bytes)


def discovering(**discover) -> ListScope:
    return ListScope({"schema": csl.SCOPE_SCHEMA, "lists": [tl.SHIPPED],
                      "discover": {"sites": "all_tenant_sites", **discover}})


class DiscoveryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audience = tl.audience()
        self.store = csl.open_store(self.tmp.name, self.audience)
        self.addCleanup(self.store.close)

    def capture(self, graph: EstateGraph) -> ListCapture:
        api, self.transport = graph_read.build_graph(tl.ENV, sender=graph)
        return ListCapture(self.store, api, self.audience, tl.CAPTURE)

    def rows(self, actor, list_id: str) -> list[dict]:
        unit = f"{csl.UNIT_PREFIX}{SITE_A}/{list_id}"
        try:
            return self.store.records(actor, csl.LIST_KIND.name, unit, limit=1000)["items"]
        except csl.CaptureError:
            return []


class DiscoveryTests(DiscoveryCase):
    def test_every_tenant_sites_lists_are_found_and_captured_with_their_columns(self):
        for n in (1, 10, 60):
            with self.subTest(lists=n):
                self.store.close()
                self.store = csl.open_store(tempfile.mkdtemp(dir=self.tmp.name), self.audience)
                graph = EstateGraph(n)
                capture = self.capture(graph)
                run = capture.run(discovering(exclude_lists=[gid(1)] if n > 1 else []))
                found = run["discovery"]
                expected = n - (1 if n > 1 else 0)
                self.assertEqual(found["status"], "complete")
                self.assertEqual((found["sites_listed"], found["sites_personal_skipped"],
                                  found["sites_read"]), (2, 1, 1))
                self.assertEqual((found["lists_library"], found["lists_hidden_or_system"],
                                  found["lists_excluded"], found["lists_discovered"]),
                                 (1, 2, 1 if n > 1 else 0, expected))
                self.assertTrue(run["complete"], run)
                self.assertEqual(run["bound_total"], expected + 1)   # and the named list
                first = gid(2) if n > 1 else gid(1)
                (row,) = [r["row"] for r in self.rows(tl.READER, first)]
                # The list's own columns; never a hidden or bookkeeping one.
                self.assertEqual(set(row["fields"]), {"Title", "LotNumber"})
                captured = sum(len(self.rows(tl.READER, gid(i))) for i in range(1, n + 1))
                self.assertEqual(captured, expected)   # none lost or twice
                # No OneDrive's lists, no library's or hidden list's items, are read.
                gets = [unquote(urlsplit(u).path) for m, u in graph.calls if m == "GET"]
                self.assertFalse(any("bbbb-1111" in p and "/lists" in p for p in gets))
                for skipped in (gid(900), gid(901), gid(902)):
                    self.assertFalse(any(f"/lists/{skipped}/items" in p for p in gets))
                self.assertIn("graph.site_lists", self.transport.operations_read)

    def test_restricted_readers_see_nothing_of_a_discovered_list(self):
        graph = EstateGraph(2)
        self.capture(graph).run(discovering())
        self.assertEqual(len(self.rows(tl.READER, gid(1))), 1)
        self.assertEqual(self.rows(tl.OUTSIDER, gid(1)), [])   # granted, in no item ACL
        self.assertEqual(self.rows(tl.WITHHELD, gid(1)), [])   # not granted at all
        citation = self.rows(tl.READER, gid(1))[0]["citation"]
        self.assertFalse(self.store.resolve(citation, tl.OUTSIDER).get("ok"))

    def test_a_list_the_audience_does_not_grant_has_nothing_read(self):
        from integrations.live_source_guard import LiveSourceAudience
        graph = EstateGraph(2)
        granted = f"sharepoint:list:{SITE_A}:{gid(1)}"
        self.audience = LiveSourceAudience(
            {f"sharepoint:list:{tl.SITE}:{tl.LIST}": ["svc:sharepoint-list-capture"],
             granted: ["svc:sharepoint-list-capture"]},
            tenant="plx", classifications={"sharepoint:list:*": "restricted"},
            source_acl_required=["sharepoint:list:*"])
        self.store.close()
        self.store = csl.open_store(tempfile.mkdtemp(dir=self.tmp.name), self.audience)
        run = self.capture(graph).run(discovering())
        self.assertEqual(run["discovery"]["lists_not_visible"], 1)
        gets = [unquote(urlsplit(u).path) for m, u in graph.calls if m == "GET"]
        self.assertFalse(any(f"/lists/{gid(2)}" in p for p in gets), gets)
        self.assertTrue(any(f"/lists/{gid(1)}/columns" in p for p in gets))
        statuses = {r["unit"]: r["reasons"] for r in run["lists"]}
        self.assertEqual(statuses[f"{csl.UNIT_PREFIX}{SITE_A}/{gid(2)}"], ["not_visible"])

    def test_a_failed_site_read_keeps_discovery_and_the_run_incomplete(self):
        graph = EstateGraph(2)
        graph.failing_sites.add(SITE_A)
        run = self.capture(graph).run(discovering())
        self.assertEqual(run["discovery"]["status"], "incomplete")
        self.assertEqual(run["discovery"]["sites_failed"], 1)
        self.assertFalse(run["complete"])
        self.assertTrue(run["blocked_upstream"])

    def test_a_cut_or_truncated_column_listing_keeps_discovery_incomplete(self):
        """INT-017: a column listing cut at its page bound, or more columns than are
        kept, is a named reason, never a silently short column set."""
        from unittest import mock
        with mock.patch.object(csl, "MAX_DISCOVERED_COLUMNS", 1):
            run = self.capture(EstateGraph(2)).run(discovering())
        found = run["discovery"]
        self.assertEqual(found["status"], "incomplete")
        self.assertIn("columns_truncated", found["reasons"])
        self.assertEqual(found["lists_columns_truncated"], 2)
        self.assertFalse(run["complete"])
        capture = self.capture(EstateGraph(2))
        real = capture.api.get_pages

        def cut(url, **kwargs):
            items, walk = real(url, **kwargs)
            if "/columns" in unquote(urlsplit(url).path):
                walk = {**walk, "complete": False, "truncated": True}
            return items, walk
        capture.api.get_pages = cut
        found = capture.run(discovering())["discovery"]
        self.assertEqual(found["status"], "incomplete")
        self.assertEqual(found["reasons"], ["columns_listing_incomplete"])

    def test_discover_only_reads_no_item_and_reports_counts(self):
        graph = EstateGraph(4)
        parser = argparse.ArgumentParser()
        csl.add_cli_arguments(parser)
        scope_file = Path(self.tmp.name) / "scope.json"
        scope_file.write_text(json.dumps({"schema": csl.SCOPE_SCHEMA, "lists": [tl.SHIPPED],
                                          "discover": {"sites": "all_tenant_sites"}}))
        audience_file = tl.ROOT / "examples" / "sharepoint_list_capture_audience.live-check.json"
        state = tempfile.mkdtemp(dir=self.tmp.name)
        args = parser.parse_args(["--state-dir", state, "--scope", str(scope_file),
                                  "--live-audience", str(audience_file), "--discover-only"])
        report = csl.run_cli(args, environ=tl.ENV, sender=graph)
        self.assertTrue(report["discover_only"])
        self.assertEqual(report["would_capture"], {"lists_named": 1, "lists_discovered": 4,
                                                   "columns": 17 + 4 * 2})
        self.assertFalse(any("/items" in u for _m, u in graph.calls))
        self.assertNotIn("Tracker", json.dumps(report))   # counts only, never a name

    def test_an_unknown_or_malformed_discover_block_is_refused(self):
        for bad in ({"sites": "everything"}, {"sites": "all_tenant_sites", "extra": 1},
                    {"sites": "all_tenant_sites", "exclude_lists": ["not-a-guid"]},
                    {"sites": "all_tenant_sites", "max_lists": 0}, True):
            with self.subTest(bad=bad), self.assertRaises(ListCaptureError):
                ListScope({"schema": csl.SCOPE_SCHEMA, "lists": [tl.SHIPPED], "discover": bad})


if __name__ == "__main__":
    unittest.main()
