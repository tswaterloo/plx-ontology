"""GET-only SharePoint listItem (current column) reads, injected transport only.

No network and no credentials: every request goes to the in-process
``FakeSender`` of ``test_live_source_adapters``. Evidence label:
``injected-transport``. Covers the closed-grammar shape of the read, the
audience/ACL decision before it, Portal-taxonomy field filtering, eTag
capture and the doc-metadata current-column comparison fed by it.

The list-item capture's item permissions (``integrations.capture_sharepoint_lists``,
the fake Graph of ``test_capture_sharepoint_lists``): the beta fallback after a
refused v1.0 read, only ever per item; the whole list stopped at the first beta
403/404; a resumed run incomplete while any current item's ACL is unreadable; and
every ``sharepoint-list://`` item trimmed per item (employee or not) through
``plx.read``.
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
from unittest import mock
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from integrations import graph_read
from integrations import live_source_grammar as grammar
from integrations.graph_read import SharePointReader
from integrations.http import SourceUnavailable
from integrations.live_source_guard import (LiveSourceAudience, NotVisible,
                                            ReadOnlyGuardError, check_request)
from integrations.tests.test_live_source_adapters import (DRIVE, DRIVE_BASE, ENV, CUSTOMERS,
                                                          SITE_ID, FakeSender, drive_item,
                                                          graph_api, j, live_drive_item)
from verify import live_verify as lv
from ontology.core import Actor
from operating import doc_metadata, doc_metadata_taxonomy as taxonomy
from operating.doc_metadata import CURRENT_COLUMN_FIELDS
from tests.test_doc_metadata import (QA, RICARDO_AOS, SITE, TENANT, Fixture, ao_packet,
                                     attributes, by_column, proposals_for)
from integrations import capture_sharepoint_lists as csl
from integrations.capture_sharepoint import SharePointCaptureStore
from integrations.capture_store import make_handle
from integrations.source_mirror import AudienceMap
from integrations.tests import test_capture_sharepoint_lists as tcl
from ontology import schema as schema_loader
from ontology.project import SharePointCaptures, build_store
from operating.case_runtime import CaseRuntime, LiveReaders
from operating.employee_permissions import EmployeePermissionPolicy, EmployeePermissionResolver
from operating.ontology_host import CaseHostTools
from operating.tests.test_employee_permissions import OID, POLICY, Calls, member, portal_user


ETAG = "\"{E1},1\""
SITE_GUID = "11111111-0000-0000-0000-000000000001"
LIST_GUID = "22222222-0000-0000-0000-000000000002"
SP_IDS = {"siteId": SITE_GUID, "listId": LIST_GUID, "listItemId": "7"}
ACCESS = f"sharepoint:{DRIVE}:/Customers/*"
# Portal-shaped values plus fields the Portal taxonomy does not name.
LIST_FIELDS = {"@odata.etag": ETAG, "PartyType": "Internal", "ItemRef": "AO-26005C",
               "DocumentType": "BATCH_RECORD", "ContentType": "PLX Production Order",
               "FileLeafRef": "AO-26005B.pdf", "Author": "Ricardo", "id": "7",
               "_UIVersionString": "3.0"}


def rule_api(drive: str, item: str) -> str:
    """The listItem URL rule doc_metadata enforces."""
    return ("https://graph.microsoft.com/v1.0/drives/" + quote(drive, safe="") + "/items/" +
            quote(item, safe="") + "/listItem?expand=fields")


def file_item(etag: str = ETAG, **extra) -> dict:
    item = drive_item("F1", "AO-26005B.pdf", etag=etag, parent_id="FOLDER-CUST")
    item["sharepointIds"] = dict(SP_IDS)
    item.update(extra)
    return item


class Source:
    """Routes for one file whose eTag can move between reads."""

    def __init__(self, fields=None, *, etags=(ETAG,), list_id="7", tenant="plx"):
        self.etags = list(etags)
        self.fields = dict(LIST_FIELDS if fields is None else fields)
        self.list_id = list_id
        self.reads = 0
        routes = {
            f"{DRIVE_BASE}/root:/Customers:": j(200, CUSTOMERS),
            f"{DRIVE_BASE}/items/FOLDER-CUST/children": j(200, {"value": [file_item()]}),
            f"{DRIVE_BASE}/items/F1": self.item,
            f"{DRIVE_BASE}/items/F1/listItem": lambda url: j(200, {
                "id": self.list_id, "eTag": "\"list-etag,3\"",
                "sharepointIds": dict(SP_IDS), "fields": self.fields}),
            f"{DRIVE_BASE}/items/F1/permissions": j(200, {"value": [{"roles": ["read"],
                "grantedToV2": {"user": {"email": "ricardo@petralabx.com"}}}]}),
        }
        self.sender = FakeSender(routes)

    def item(self, url):
        etag = self.etags[min(self.reads, len(self.etags) - 1)]
        self.reads += 1
        return live_drive_item(file_item(etag), url, "")

    def listitem_calls(self) -> list[str]:
        return [c[1] for c in self.sender.calls if "/listItem" in c[1]]


def reader_for(source: Source, audiences=None, tenant="plx", **kwargs) -> SharePointReader:
    api, transport = graph_api(source.sender)
    audience = LiveSourceAudience(audiences or {ACCESS: ["staff:quality"],
                                                f"sharepoint:{DRIVE}:/Customers/Secret*": []},
                                  tenant=tenant, **kwargs)
    return SharePointReader(api, audience, DRIVE, transport=transport)


ACTOR = Actor("plx", frozenset({"staff:quality"}), "restricted")


class ListItemGrammarTest(unittest.TestCase):
    def test_builds_the_one_listitem_url_of_the_configured_drive_item(self):
        url = grammar.drive_list_item_fields_url(DRIVE, "01ABC=")
        self.assertEqual(url, rule_api(DRIVE, "01ABC="))
        self.assertIn("/drives/b%21drive-AbC123/items/01ABC%3D/listItem?expand=fields", url)
        self.assertEqual(grammar.match("graph.microsoft.com", url.split(".com", 1)[1]
                                       .split("?", 1)[0]), "graph.drive_list_item")
        self.assertEqual(check_request("GET", url, environ={}), "graph.microsoft.com")
        self.assertTrue(grammar.drive_list_item_fields_url_ok(url, DRIVE, "01ABC="))

    def test_refuses_every_other_shape(self):
        good = rule_api(DRIVE, "F1")
        others = [
            rule_api("b!other-drive", "F1"), rule_api(DRIVE, "F2"),
            good.replace("?expand=fields", ""),
            good.replace("expand=fields", "%24expand=fields"),
            good.replace("expand=fields", "expand=fields%28%24select%3DTitle%29"),
            good + "&$top=1",
            good.replace("/listItem?", "/listItem/fields?"),
            good.replace("/listItem?", "/listItem/versions?"),
            good.replace("/listItem?", "/listItem/driveItem?"),
            good.replace("https://", "http://"),
            f"https://graph.microsoft.com/v1.0/sites/{SITE_GUID}/lists/{LIST_GUID}/items/7"
            "?expand=fields",
        ]
        for url in others:
            with self.subTest(url=url):
                self.assertFalse(grammar.drive_list_item_fields_url_ok(url, DRIVE, "F1"))
        for drive, item in ((DRIVE, "../F1"), (DRIVE, "a/b"), ("b!x/../y", "F1"),
                            (DRIVE, "F1%2F"), (DRIVE, ""), (None, "F1")):
            with self.subTest(drive=drive, item=item), self.assertRaises(ValueError):
                grammar.drive_list_item_fields_url(drive, item)
        # The guard parses the path into the closed grammar: no listItem
        # sub-resource or single list-item address exists to send.
        for path in ("/listItem/fields", "/listItem/versions", "/listItem/driveItem"):
            with self.subTest(path=path), self.assertRaises(ReadOnlyGuardError):
                check_request("GET", f"https://graph.microsoft.com/v1.0/drives/{DRIVE}/items/F1"
                              + path, environ={})
        with self.assertRaises(ReadOnlyGuardError):
            check_request("GET", f"https://graph.microsoft.com/v1.0/sites/{SITE_GUID}/lists/"
                          f"{LIST_GUID}/items/7?expand=fields", environ={})

    def test_column_names_are_plain_internal_names(self):
        for name in CURRENT_COLUMN_FIELDS:
            self.assertTrue(grammar.column_name_ok(name), name)
        for bad in ("", "Doc Type", "@odata.etag", "a/b", "x" * 65, 7, None):
            self.assertFalse(grammar.column_name_ok(bad), bad)


class ListItemReadTest(unittest.TestCase):
    def test_no_drive_grant_means_no_request(self):
        source = Source()
        nobody = Actor("plx", frozenset({"staff:x"}), "restricted")
        with self.assertRaises(NotVisible):
            reader_for(source).list_item_fields(nobody, "F1", field_names=CURRENT_COLUMN_FIELDS)
        self.assertEqual(source.sender.calls, [])

    def test_denied_path_or_acl_reads_metadata_only_never_the_listitem(self):
        source = Source()
        # The file lives under /Customers; only /Customers/Acme is granted.
        narrow = reader_for(source, {f"sharepoint:{DRIVE}:/Customers/Acme/*": ["staff:quality"]})
        with self.assertRaises(NotVisible):
            narrow.list_item_fields(ACTOR, "F1", field_names=CURRENT_COLUMN_FIELDS)
        self.assertEqual(source.listitem_calls(), [])
        self.assertTrue(source.sender.api_calls())  # the metadata GET only
        # Source ACL required and it names somebody else: still no listItem read.
        gated = reader_for(source, identities={"staff:quality": ["vince@petralabx.com"]},
                           source_acl_required=[f"sharepoint:{DRIVE}:*"])
        with self.assertRaises(NotVisible):
            gated.list_item_fields(ACTOR, "F1", field_names=CURRENT_COLUMN_FIELDS)
        self.assertEqual(source.listitem_calls(), [])

    def test_bad_field_allowlist_or_item_id_refused_before_io(self):
        source = Source()
        reader = reader_for(source)
        for names in ("DocumentType", [], ["Doc Type"], ["@odata.etag"]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                reader.list_item_fields(ACTOR, "F1", field_names=names)
        with self.assertRaises(NotVisible):
            reader.list_item_fields(ACTOR, "../F1", field_names=CURRENT_COLUMN_FIELDS)
        self.assertEqual(source.sender.calls, [])

    def test_returns_only_taxonomy_columns_with_etag_and_rule_source_api(self):
        source = Source()
        read = reader_for(source).list_item_fields(ACTOR, "F1", field_names=CURRENT_COLUMN_FIELDS)
        snapshot = read["snapshot"]
        self.assertEqual(set(snapshot), {"source", "site_id", "list_id", "list_item_id",
                                         "drive_etag", "source_api", "fields"})
        self.assertEqual(snapshot["fields"], {"ContentType": "PLX Production Order",
                                              "DocumentType": "BATCH_RECORD",
                                              "ItemRef": "AO-26005C", "PartyType": "Internal"})
        self.assertEqual(read["fields_withheld_count"], 4)  # FileLeafRef Author id _UIVersion
        self.assertEqual(snapshot["drive_etag"], ETAG)
        self.assertEqual(read["list_item_etag"], "\"list-etag,3\"")
        # the content identity of the same read, so a caller can bind columns to bytes it
        # read earlier (a column edit moves the eTag, never the cTag or the hashes)
        self.assertEqual(read["ctag"], file_item()["cTag"])
        self.assertEqual(read["source_hashes"], file_item()["file"]["hashes"])
        self.assertEqual(snapshot["source_api"], rule_api(DRIVE, "F1"))
        self.assertEqual((snapshot["site_id"], snapshot["list_id"], snapshot["list_item_id"]),
                         (SITE_GUID, LIST_GUID, "7"))
        self.assertEqual(read["citation"], graph_read.drive_citation(DRIVE, "F1", ETAG))
        # The request sent is exactly the provenance recorded.
        self.assertEqual(source.listitem_calls(), [snapshot["source_api"]])
        self.assertTrue(all(c[0] == "GET" for c in source.sender.api_calls()))
        # sharepointIds are selected on the item reads (not returned by default).
        self.assertTrue(all("sharepointIds" in c[1] for c in source.sender.api_calls()
                            if c[1].split("?")[0].endswith("/items/F1")))
        self.assertEqual(sorted(read["fields_requested"]), sorted(CURRENT_COLUMN_FIELDS))
        # The snapshot is the shape doc_metadata accepts as this document's own
        # listItem at the document's revision.
        attrs = {"source_container": DRIVE, "source_revision": ETAG}
        fields, info = doc_metadata._columns_snapshot(attrs, ETAG, snapshot,
            doc_metadata._listitem_api(attrs, "sharepoint-drive", f"{DRIVE}:F1"))
        self.assertEqual(fields, snapshot["fields"])
        self.assertEqual((info["status"], info["matches_document_revision"]), ("read", True))

    def test_moving_item_or_foreign_listitem_fails_closed(self):
        moving = Source(etags=(ETAG, "\"{E1},2\""))  # item read before / after the listItem
        with self.assertRaisesRegex(SourceUnavailable, "changed during listItem read"):
            reader_for(moving).list_item_fields(ACTOR, "F1", field_names=CURRENT_COLUMN_FIELDS)
        foreign = Source(list_id="8")
        with self.assertRaisesRegex(SourceUnavailable, "identity mismatch"):
            reader_for(foreign).list_item_fields(ACTOR, "F1", field_names=CURRENT_COLUMN_FIELDS)

    def test_column_set_mirrors_the_portal_taxonomy(self):
        self.assertEqual(CURRENT_COLUMN_FIELDS,
                         tuple(c["name"] for c in taxonomy.COLUMNS) + ("ContentType",))
        self.assertEqual(len(set(CURRENT_COLUMN_FIELDS)), 17)
        # The record-link refs (plx-document-schema.ts@19f1d94:86-87) are read as stated metadata.
        self.assertTrue({"ProductRef", "OrderRef"} <= set(CURRENT_COLUMN_FIELDS))


class ProbeTest(unittest.TestCase):
    def test_probe_reports_names_and_counts_never_values(self):
        source = Source()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = graph_read.main(["listitem-probe", "--drive", DRIVE, "--folder", "/Customers"],
                                   environ=ENV, sender=source.sender)
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual((report["files_listed"], report["listitems_read"],
                          report["drive_etag_matches_listing"], report["source_api_rule_ok"]),
                         (1, 1, 1, 1))
        self.assertTrue(report["ok"])
        self.assertEqual(report["field_names_present"],
                         ["ContentType", "DocumentType", "ItemRef", "PartyType"])
        self.assertEqual(report["fields_withheld_total"], 4)
        self.assertEqual(report["ctag_matches_listing"], 1)
        # how the recorded type values read for a playbook kind, never the values
        self.assertEqual(report["document_kind_readings"],
                         {"DocumentType:consistent": 1, "ContentType:consistent": 1})
        for value in ("AO-26005C", "BATCH_RECORD", "PLX Production Order", "Internal",
                      "AO-26005B", "Ricardo", "F1", DRIVE, "sharepoint://", "https://"):
            self.assertNotIn(value, out.getvalue())
        # Taxonomy-column presence per library class, counts only; a library whose
        # name cannot be read is UNRESOLVED, never STANDARD.
        self.assertEqual(report["library_class"], "UNRESOLVED")
        columns = report["taxonomy_columns_by_library_class"]["UNRESOLVED"]["columns"]
        self.assertEqual(set(columns), {c["name"] for c in taxonomy.COLUMNS})
        self.assertEqual({name for name, n in columns.items() if n},
                         {"DocumentType", "ItemRef", "PartyType"})   # ContentType is no column
        for name, expected in (("PLX Finance", "FINANCE"), ("Quality Records", "STANDARD")):
            source = Source()
            source.sender.routes[DRIVE_BASE] = j(200, {"id": DRIVE, "name": name,
                                                       "driveType": "documentLibrary"})
            report = graph_read.listitem_probe(ENV, sender=source.sender, drive=DRIVE,
                                               folder="/Customers")
            self.assertEqual(report["taxonomy_columns_by_library_class"], {expected: {
                "files": 1, "listitems_read": 1, "columns": columns}})
            self.assertNotIn(name, json.dumps(report))

    def test_ao_check_never_reads_the_portal_library(self):
        # The AO listItem check and the Portal library check are separate specs: the AO
        # evidence stays green when only the library read fails (here: every library
        # route answers 404 although SHAREPOINT_DRIVE_ID is configured).
        source = Source()
        argv = AO_SPEC["command"][3:]
        argv[argv.index("--drive") + 1], argv[argv.index("--folder") + 1] = DRIVE, "/Customers"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = graph_read.main(argv, environ=ENV, sender=source.sender)
        report = json.loads(out.getvalue())
        # every expectation but the six-AO-packet minimums the one-file fixture cannot meet
        expect = dict(AO_SPEC["expect"], json_min={})
        self.assertEqual(lv.check_expectations(AO_SPEC, code, report, expect), [])
        self.assertNotIn("SHAREPOINT_DRIVE_ID", AO_SPEC["requires_env"])
        self.assertEqual([c for c in source.sender.api_calls()
                          if urlsplit(c[1]).path.endswith(("/list", "/columns", "/list/items"))],
                         [])


LIST_BASE = f"/v1.0/sites/{SITE_ID}/lists/{LIST_GUID}"
ITEMS = f"{DRIVE_BASE}/list/items"
DOC_TYPE, FOLDER_TYPE = "0x0101009D1CB255DA76424F860D91F20E6C4118", "0x012000AB"
SPECS = Path(__file__).resolve().parents[2] / "devloop" / "live-checks"
SPEC_ID = "graph.sharepoint_library_columns"
SPEC = json.loads((SPECS / f"{SPEC_ID}.json").read_text(encoding="utf-8"))
AO_SPEC = json.loads((SPECS / "graph.sharepoint_listitem.json").read_text(encoding="utf-8"))
SITE_WEB = "https://petralabx.sharepoint.com/sites/PortalDocs/Shared%20Documents"


def stamp(minute: int) -> str:
    return f"2026-01-{1 + minute // 1440:02d}T{minute // 60 % 24:02d}:{minute % 60:02d}:00Z"


class Library:
    """The Portal library's backing list: column definitions and list items, both paged.

    The definitions are derived from ``taxonomy.COLUMNS``: the first ``by_name``
    specs are defined under their internal name, the next one only under its display
    name (another internal name), ``renamed`` adds definitions (spec name -> the
    definition), the rest are not provisioned; two non-taxonomy columns are defined as
    well. ``docs`` are field mappings keyed by spec name, in list order (oldest
    first); document ``n`` was last modified ``stamps[n]`` (default: minute ``n``)."""

    def __init__(self, docs, *, by_name=3, renamed=None, page=25, column_pages=None,
                 folders=1, other=1, stamps=None):
        specs = list(taxonomy.COLUMNS)
        self.named = [c["name"] for c in specs[:by_name]]
        self.display = specs[by_name]
        self.internal = {c: c for c in self.named}
        self.internal[self.display["name"]] = "Mirrored_x0020_" + str(by_name)
        defs = ([{"name": n, "displayName": "x"} for n in self.named]
                + [{"name": self.internal[self.display["name"]],
                    "displayName": self.display["display_name"]},
                   {"name": "Title", "displayName": "Title"},
                   {"name": "FileLeafRef", "displayName": "Name"}])
        for spec_name, definition in (renamed or {}).items():
            self.internal[spec_name] = definition["name"]
            defs.append(definition)
        self.missing = [c["name"] for c in specs if c["name"] not in self.internal]
        stamps = stamps or [stamp(n) for n in range(len(docs))]
        items = [{"id": str(n + 1), "contentType": {"id": DOC_TYPE},
                  **({"lastModifiedDateTime": stamps[n]} if stamps[n] else {}),
                  "fields": {self.internal[k]: v for k, v in doc.items()}}
                 for n, doc in enumerate(docs)]
        full = {self.internal[k]: "set" for k in self.internal}
        items += [{"id": f"f{n}", "contentType": {"id": FOLDER_TYPE}, "fields": dict(full)}
                  for n in range(folders)]
        items += [{"id": f"o{n}", "contentType": {"id": "0x0100AA"}, "fields": dict(full)}
                  for n in range(other)]
        self.sender = FakeSender({
            DRIVE_BASE: j(200, {"id": DRIVE, "name": "Documents",
                                "driveType": "documentLibrary"}),
            f"{DRIVE_BASE}/list": j(200, {"id": LIST_GUID, "name": "Shared Documents",
                                          "webUrl": SITE_WEB,
                                          "parentReference": {"siteId": SITE_ID}}),
            f"{LIST_BASE}/columns": self.pager(
                defs, len(defs) if column_pages is None else column_pages),
            ITEMS: self.pager(items, page),
        })

    @staticmethod
    def pager(rows, size):
        def route(url):
            query = dict(parse_qsl(urlsplit(url).query))
            start = int(query.get("$skiptoken", "0"))
            page = {"value": rows[start:start + size]}
            if start + size < len(rows) or size == 0:
                page["@odata.nextLink"] = (url.split("?")[0] + "?" + urlencode(
                    {**query, "$skiptoken": str(start + max(size, 1))}))
            return j(200, page)
        return route

    def calls(self, part):
        return [c[1] for c in self.sender.api_calls() if part in urlsplit(c[1]).path]


def library_reader(library: Library, audiences=None, **kwargs) -> SharePointReader:
    api, transport = graph_api(library.sender)
    audience = LiveSourceAudience(audiences or {f"sharepoint:{DRIVE}:/*": ["staff:quality"]},
                                  tenant="plx", **kwargs)
    return SharePointReader(api, audience, DRIVE, transport=transport)


class LibraryColumnsTest(unittest.TestCase):
    """The Portal library's taxonomy columns: defined or not, and how often filled."""

    def test_missing_is_told_apart_from_existing_but_empty_and_fills_are_counted(self):
        lib = Library([], by_name=3)
        a, b, c = lib.named
        d = lib.display["name"]
        docs = [{a: "x", b: "", d: True}, {a: "y", b: None, d: False},
                {a: "  ", b: "   ", c: []}, {}]
        lib = Library(docs, by_name=3)
        out = library_reader(lib).library_columns(ACTOR, taxonomy.COLUMNS, max_items=200)
        self.assertEqual(set(out["columns"]), {col["name"] for col in taxonomy.COLUMNS})
        self.assertEqual(out["columns"][a], {"state": "filled", "matched_by": "name", "filled": 2})
        for empty in (b, c):   # defined; blanks, nulls and empty lists fill nothing
            self.assertEqual(out["columns"][empty],
                             {"state": "empty", "matched_by": "name", "filled": 0})
        # defined only under its display name (another internal name): read under that name;
        # a boolean false fills it
        self.assertEqual(out["columns"][d],
                         {"state": "filled", "matched_by": "display_name", "filled": 2})
        for name in lib.missing:
            self.assertEqual(out["columns"][name],
                             {"state": "missing", "matched_by": None, "filled": None})
        self.assertEqual(out["columns_defined"], 6)
        self.assertTrue(out["columns_listing_complete"])
        # the filled folder and the other item never enter the document counts
        self.assertEqual(out["sample"], {"items": 6, "documents_read": 4, "documents": 4,
                                         "folders": 1, "other": 1, "documents_undated": 0,
                                         "documents_without_fields": 0, "complete": True,
                                         "order": "lastModifiedDateTime desc"})
        # GET only; the item read selects only the defined mirrored columns
        self.assertEqual({c[0] for c in lib.sender.api_calls()}, {"GET"})
        selects = {dict(parse_qsl(urlsplit(u).query))["$expand"] for u in lib.calls("/items")}
        self.assertEqual(selects, {"fields($select=" + ",".join(sorted(
            [a, b, c, lib.internal[d]])) + ")"})

    def test_a_column_the_portal_finds_by_any_name_or_display_name_reads_present(self):
        # The Portal's rule: a spec is present when its name or display name equals any
        # definition's name or display name (provision-sharepoint-columns.mjs:141-149).
        lib = Library([], by_name=0)
        target = lib.missing[0]
        lib = Library([{target: "Customer"}, {}], by_name=0, renamed={
            target: {"name": target + "0", "displayName": target}})
        out = library_reader(lib).library_columns(ACTOR, taxonomy.COLUMNS)
        self.assertEqual(out["columns"][target],
                         {"state": "filled", "matched_by": "name", "filled": 1})
        selects = {dict(parse_qsl(urlsplit(u).query))["$expand"] for u in lib.calls("/items")}
        self.assertIn(target + "0", next(iter(selects)))

    def test_empty_is_judged_on_the_newest_documents_across_pages(self):
        lib = Library([])
        old, recent, edited = lib.named
        docs = [{old: "x"}, {edited: "y"}] + [{} for _ in range(288)] + [{recent: "z"}] * 10
        stamps = [stamp(n) for n in range(len(docs))]
        stamps[1] = stamp(len(docs) + 5)   # an old item edited last: newest by modification
        lib = Library(docs, page=50, stamps=stamps)
        out = library_reader(lib).library_columns(ACTOR, taxonomy.COLUMNS, max_items=200)
        self.assertEqual(out["columns"][recent]["state"], "filled")
        self.assertEqual(out["columns"][recent]["filled"], 10)
        self.assertEqual(out["columns"][edited]["filled"], 1)
        # only the oldest document fills it: none of the newest 200 does
        self.assertEqual(out["columns"][old], {"state": "empty", "matched_by": "name",
                                               "filled": 0})
        self.assertEqual({k: out["sample"][k] for k in ("items", "documents_read", "documents",
                                                        "complete")}, {
            "items": 302, "documents_read": 300, "documents": 200, "complete": True})
        self.assertEqual(len(lib.calls("/items")), 7)
        # a walk cut before the list's end never reads empty: newer documents were unread
        lib = Library([{old: "x"}] * 3, page=0)   # every page empty with a nextLink: page cap
        out = library_reader(lib).library_columns(ACTOR, taxonomy.COLUMNS)
        self.assertFalse(out["sample"]["complete"])
        self.assertEqual(out["columns"][recent]["state"], "unknown")
        self.assertEqual(len(lib.calls("/items")), 50)

    def test_truncated_column_listing_reads_unknown_never_missing(self):
        lib = Library([{}], column_pages=0)   # every page empty with a nextLink: page cap
        out = library_reader(lib).library_columns(ACTOR, taxonomy.COLUMNS)
        self.assertFalse(out["columns_listing_complete"])
        self.assertEqual({c["state"] for c in out["columns"].values()}, {"unknown"})
        self.assertEqual(lib.calls("/items"), [])   # nothing defined: no item read

    def test_every_document_counted_once_across_pages(self):
        lib0 = Library([])
        name = lib0.named[0]
        for n in (1, 10, 60):
            with self.subTest(documents=n):
                lib = Library([{name: str(k)} if k % 2 else {} for k in range(n)], page=7,
                              folders=0, other=0)
                out = library_reader(lib).library_columns(ACTOR, taxonomy.COLUMNS,
                                                          max_items=200)
                self.assertEqual((out["sample"]["items"], out["sample"]["documents"]), (n, n))
                self.assertEqual(out["columns"][name]["filled"], n // 2)
                self.assertTrue(out["sample"]["complete"])
                self.assertEqual(len(lib.calls("/items")), -(-n // 7))
        capped = Library([{name: "v"}] * 60, page=7, folders=0, other=0,
                         stamps=[None] * 30 + [stamp(n) for n in range(30)])
        out = library_reader(capped).library_columns(ACTOR, taxonomy.COLUMNS, max_items=20)
        self.assertEqual((out["sample"]["documents_read"], out["sample"]["documents"],
                          out["sample"]["documents_undated"], out["sample"]["complete"],
                          out["columns"][name]["filled"]), (60, 20, 30, True, 20))

    def test_seven_digit_fractions_are_dated_on_python_3_10(self):
        # Graph writes up to seven fraction digits, which
        # datetime.fromisoformat refuses before Python 3.11 (emulated here), so such a
        # document read as undated and sorted after every dated one.
        class Py310(dt.datetime):
            @classmethod
            def fromisoformat(cls, text):
                if re.search(r"\.\d{7}", text):
                    raise ValueError("Invalid isoformat string")
                return super().fromisoformat(text)

        name = Library([]).named[0]
        lib = Library([{name: "old"}, {name: "newest"}, {}], folders=0, other=0, stamps=[
            "2026-01-01T00:00:00Z", "2026-01-03T00:00:00.1234567Z", "2026-01-02T00:00:00.5Z"])
        with mock.patch.object(graph_read, "datetime", Py310):
            out = library_reader(lib).library_columns(ACTOR, taxonomy.COLUMNS, max_items=1)
        self.assertEqual(out["sample"]["documents_undated"], 0)
        self.assertEqual(out["columns"][name], {"state": "filled", "matched_by": "name",
                                                "filled": 1})
        for bad in ("2026-01-03T00:00:00.12345678Z", "2026-01-03T00:00:00+00:00",
                    "2026-13-03T00:00:00Z", "2026-01-03"):
            self.assertIsNone(graph_read._modified({"lastModifiedDateTime": bad}), bad)

    def test_reader_not_covering_the_whole_drive_causes_no_library_read(self):
        restricted = (
            {f"sharepoint:{DRIVE}:/Customers/*": ["staff:quality"]},          # one folder
            {f"sharepoint:{DRIVE}:/*": ["staff:quality"],
             f"sharepoint:{DRIVE}:/Customers/Secret*": []},                   # a deny below
        )
        for audiences in restricted:
            with self.subTest(audiences=audiences):
                lib = Library([{}])
                with self.assertRaises(NotVisible):
                    library_reader(lib, audiences).library_columns(ACTOR, taxonomy.COLUMNS)
                self.assertEqual(lib.sender.calls, [])
        # an item needing its own source ACL, or a folder above the reader's clearance
        cleared = Actor("plx", frozenset({"staff:quality"}), "confidential")
        for actor, kwargs in (
                (ACTOR, {"source_acl_required": [f"sharepoint:{DRIVE}:/Customers/*"]}),
                (cleared, {"classifications": {f"sharepoint:{DRIVE}:/HR*": "restricted"}})):
            with self.subTest(kwargs=kwargs):
                lib = Library([{}])
                with self.assertRaises(NotVisible):
                    library_reader(lib, **kwargs).library_columns(actor, taxonomy.COLUMNS)
                self.assertEqual(lib.sender.calls, [])
        lib = Library([{}])   # the same cleared reader without that folder reads the library
        self.assertEqual(library_reader(lib).library_columns(cleared, taxonomy.COLUMNS)
                         ["sample"]["documents"], 1)

    def test_list_items_are_read_only_from_the_drive_own_list(self):
        self.assertEqual(urlsplit(grammar.url("graph.drive_list_items", drive=DRIVE)).path,
                         f"{DRIVE_BASE}/list/items")
        self.assertEqual(check_request("GET", f"https://graph.microsoft.com{ITEMS}?$top=200",
                                       environ={}), "graph.microsoft.com")
        # A list's items addressed by site and list id are the list-item capture's read
        # (graph.list_items, integrations.capture_sharepoint_lists), never this reader's.
        for path in (f"{LIST_BASE}/items",
                     f"/v1.0/sites/{SITE_ID}/lists/33333333-0000-0000-0000-000000000003/items"):
            self.assertEqual(grammar.match("graph.microsoft.com", path), "graph.list_items")
        # a nextLink to another list's items is refused, never followed
        lib = Library([{}] * 3, page=1)
        lib.sender.routes[ITEMS] = j(200, {"value": [], "@odata.nextLink":
            f"https://graph.microsoft.com{LIST_BASE}/items?$skiptoken=1"})
        with self.assertRaises(SourceUnavailable):
            library_reader(lib).library_columns(ACTOR, taxonomy.COLUMNS)
        self.assertEqual(lib.calls(f"{LIST_BASE}/items"), [])

    def test_unknown_inputs_fail_closed(self):
        lib = Library([{}])
        reader = library_reader(lib)
        for columns in ([], [{"name": "Doc Type", "display_name": "x"}], ["DocumentType"]):
            with self.subTest(columns=columns), self.assertRaises(ValueError):
                reader.library_columns(ACTOR, columns)
        with self.assertRaises(ValueError):
            reader.library_columns(ACTOR, taxonomy.COLUMNS, max_items=0)
        self.assertEqual(lib.sender.calls, [])
        # a backing list whose identity is not a list GUID on a site id is refused
        lib.sender.routes[f"{DRIVE_BASE}/list"] = j(200, {"id": "../x",
                                                          "parentReference": {"siteId": SITE_ID}})
        with self.assertRaisesRegex(SourceUnavailable, "drive list identity invalid"):
            reader.library_columns(ACTOR, taxonomy.COLUMNS)
        # a nextLink that leaves the list's items is refused, never followed
        lib = Library([{}] * 3, page=1)
        lib.sender.routes[ITEMS] = j(200, {"value": [], "@odata.nextLink":
            f"https://graph.microsoft.com{DRIVE_BASE}/items/F1/permissions"})
        with self.assertRaises(SourceUnavailable):
            library_reader(lib).library_columns(ACTOR, taxonomy.COLUMNS)
        # a site URL on another host, or of no /sites/<name> shape, names no site
        for web in ("https://evil.sharepoint.com/sites/PortalDocs/Shared%20Documents",
                    "https://petralabx.sharepoint.com/Shared%20Documents",
                    "http://petralabx.sharepoint.com/sites/PortalDocs/x", None):
            with self.subTest(web=web):
                lib = Library([{}])
                lib.sender.routes[f"{DRIVE_BASE}/list"] = j(200, {
                    "id": LIST_GUID, "webUrl": web, "parentReference": {"siteId": SITE_ID}})
                self.assertIsNone(library_reader(lib).library_columns(
                    ACTOR, taxonomy.COLUMNS)["site_name"])


class LibraryColumnsProbeTest(unittest.TestCase):
    """The library check's own argv, output contract and registry naming."""

    def run_spec(self, lib, registry, env=ENV):
        argv = SPEC["command"][3:]
        argv[argv.index("--registry") + 1] = str(registry)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = graph_read.main(argv, environ=env, sender=lib.sender)
        return code, json.loads(out.getvalue()), out.getvalue()

    def registry(self, *names):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "registry.json"
        path.write_text(json.dumps({"entries": [
            {"id": f"sharepoint.site:{n}", "system": "sharepoint", "kind": "site", "name": n}
            for n in names] + [{"id": "x", "system": "portal", "kind": "relation",
                                "name": "PortalDocs"}]}), encoding="utf-8")
        return path

    def test_probe_reports_states_and_counts_and_meets_the_live_check_spec(self):
        from scripts import source_registry

        lib = Library([], by_name=3)
        a, b = lib.named[:2]
        d = lib.display["name"]
        lib = Library([{a: "PO-SECRET-1", b: ""}, {a: "PO-SECRET-2", d: False}], by_name=3)
        code, report, text = self.run_spec(lib, self.registry("Other", "portaldocs"))
        self.assertEqual((code, report["ok"], report["status"]), (0, True, "read"))
        self.assertEqual(report["evidence_label"], "injected-transport")
        self.assertEqual(report["columns_by_state"]["filled"], sorted([a, d]))
        self.assertEqual(report["columns_by_state"]["empty"], sorted([b, lib.named[2]]))
        self.assertEqual(report["columns_by_state"]["missing"], sorted(lib.missing))
        self.assertEqual((report["fill_counts"][a], report["fill_counts"][b],
                          report["fill_counts"][d]), (2, 0, 1))
        self.assertEqual(report["fill_rule"]["false"], "filled")   # said in the output
        self.assertNotIn(lib.missing[0], report["fill_counts"])
        # the library's site, as the registry spells it; never a name the registry lacks
        self.assertEqual(report["registry_sites"], {"read": ["portaldocs"], "unlisted": 0})
        self.assertEqual(lv.check_expectations(SPEC, code, report), [])
        recorded = lv._recorded(SPEC, report)
        self.assertEqual(recorded["registry_sites.read"], ["portaldocs"])
        self.assertEqual(recorded["fill_rule"]["false"], "filled")
        for marker in ("PO-SECRET", "Mirrored_x0020_", "Shared Documents", "Documents\"",
                       LIST_GUID, SITE_ID, DRIVE, "FileLeafRef", "https://"):
            self.assertNotIn(marker, text)
            self.assertNotIn(marker, json.dumps(recorded))
        # the source registry credits that site, and only it, with this check's result
        live = source_registry.LiveIndex([{"_from": "t", "checks": [{
            "id": SPEC_ID, "source": SPEC["source"], "result": "pass",
            "detail": {"fields": recorded}}]}])
        self.assertEqual(live.naming("sharepoint", {"portaldocs"}), [(SPEC_ID, "pass")])
        self.assertEqual(live.naming("sharepoint", {a, "read", "STANDARD"}), [])

    def test_an_unknown_column_has_no_fill_count(self):
        # A column whose state is unknown showed 0 in
        # fill_counts, which a reader of fill_counts alone takes for empty.
        lib = Library([])
        filled, unread = lib.named[0], lib.named[1:] + [lib.display["name"]]
        # one document per page: the walk stops at its page bound, before the list's end
        lib = Library([{filled: "x"}] * 60, page=1, folders=0, other=0)
        code, report, _text = self.run_spec(lib, self.registry("portaldocs"))
        self.assertFalse(report["sample"]["complete"])
        self.assertEqual(report["columns_by_state"]["unknown"], sorted(unread))
        self.assertEqual(report["fill_counts"], {filled: report["sample"]["documents"]})
        self.assertEqual(lv.check_expectations(SPEC, code, report), [])

    def test_site_outside_the_registry_or_no_library_is_never_named(self):
        lib = Library([{}])   # its site is no registry site entry (a relation shares its name)
        code, report, text = self.run_spec(lib, self.registry("Other"))
        self.assertEqual((code, report["registry_sites"]), (0, {"read": [], "unlisted": 1}))
        self.assertNotIn("PortalDocs", text)
        # no configured Portal drive: reported, not ok, nothing sent
        env = {k: v for k, v in ENV.items() if k != "SHAREPOINT_DRIVE_ID"}
        lib = Library([{}])
        code, report, _text = self.run_spec(lib, self.registry("Other"), env)
        self.assertEqual((code, report["status"], report["ok"]), (1, "not_configured", False))
        self.assertEqual(lib.sender.calls, [])
        # a library read that fails is reported by class and not ok
        lib = Library([{}])
        del lib.sender.routes[f"{DRIVE_BASE}/list"]
        code, report, _text = self.run_spec(lib, self.registry("Other"))
        self.assertEqual((code, report["status"], report["ok"]), (1, "NotVisible", False))
        self.assertNotIn("registry_sites", report)
        # a registry that cannot be read refuses the run before any I/O
        bad = self.registry("Other")
        bad.write_text("{\"entries\": [{\"system\": \"sharepoint\", \"kind\": \"site\", "
                       "\"name\": \"a/b\"}]}", encoding="utf-8")
        lib = Library([{}])
        with contextlib.redirect_stderr(io.StringIO()):
            code = graph_read.main(["library-columns-probe", "--registry", str(bad)],
                                   environ=ENV, sender=lib.sender)
        self.assertEqual((code, lib.sender.calls), (1, []))


def stored_document(fixture: Fixture, revision: str = ETAG) -> str:
    ao, erp, oe, customer, bulk = RICARDO_AOS[0]
    text, rows = ao_packet(ao, erp, oe, customer, bulk)
    return fixture.document(f"{DRIVE}:F1", attributes(text, f"{ao}.pdf", SITE + f"{ao}.pdf",
                                                      rows=rows), revision=revision)


class DocMetadataComparisonTest(unittest.TestCase):
    """A GET-only listItem read feeds ``build_proposals``' current-column comparison.

    (The legacy ledger wrapper that made this read itself went with deletion-8.)
    """

    def compare(self, source: Source, revision: str = ETAG):
        fixture = Fixture()
        doc_id = stored_document(fixture, revision=revision)
        reader = reader_for(source, {ACCESS: ["staff:quality"]}, tenant=TENANT)
        read = reader.list_item_fields(QA, "F1", field_names=CURRENT_COLUMN_FIELDS)
        return read, proposals_for(fixture, QA, doc_id, current_columns=read["snapshot"])

    def test_live_columns_agree_and_differ_at_the_current_revision(self):
        source = Source()
        read, result = self.compare(source)
        snapshot = result["current_columns"]
        self.assertEqual((snapshot["status"], snapshot["matches_document_revision"]),
                         ("read", True))
        self.assertEqual(snapshot["source_api"], rule_api(DRIVE, "F1"))
        self.assertEqual(snapshot["drive_etag"], ETAG)
        self.assertEqual(read["list_item_etag"], "\"list-etag,3\"")
        columns = by_column(result)
        self.assertEqual(columns["PartyType"]["comparison"], "agree")
        self.assertEqual((columns["ItemRef"]["comparison"], columns["ItemRef"]["current_value"]),
                         ("differs", "AO-26005C"))
        self.assertEqual(columns["DocStatus"]["comparison"], "missing")
        self.assertEqual(columns["DocumentType"]["comparison"], "current_only")
        self.assertEqual(result["content_type"]["current_portal_document_type"],
                         "PRODUCTION_ORDER")
        self.assertEqual(len(source.listitem_calls()), 1)
        self.assertEqual(result["business_system_writes"], 0)

    def test_document_older_than_the_item_reports_every_column_stale(self):
        _read, result = self.compare(Source(), revision="\"{E1},0\"")
        self.assertFalse(result["current_columns"]["matches_document_revision"])
        self.assertEqual({row["comparison"] for row in result["columns"]}, {"stale_snapshot"})
        self.assertEqual(by_column(result)["ItemRef"]["comparison_at_snapshot_revision"],
                         "differs")


# ------------------------------------------------------------------ list-item capture ACLs

LIST_PERMS = f"/sites/{tcl.SITE}/lists/{tcl.LIST}/items/{{}}/permissions"


class ListItemAclGrammarTest(unittest.TestCase):
    def test_the_grammar_admits_only_the_get_beta_item_permissions_path(self):
        site, lst = tcl.SITE, tcl.LIST
        beta = grammar.url("graph.list_item_permissions_beta", site=site, list=lst, item="7")
        self.assertEqual(urlsplit(beta).path, "/beta" + LIST_PERMS.format(7))
        self.assertEqual(check_request("GET", beta, environ={}), "graph.microsoft.com")
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            with self.assertRaises(ReadOnlyGuardError, msg=method):
                check_request(method, beta, environ={})
        # Never a list- or site-wide ACL (either version), nor any other beta read.
        for path in (f"/beta/sites/{site}/lists/{lst}/permissions",
                     f"/v1.0/sites/{site}/lists/{lst}/permissions",
                     f"/beta/sites/{site}/permissions", f"/v1.0/sites/{site}/permissions",
                     f"/beta/sites/{site}/lists/{lst}/items",
                     f"/beta/sites/{site}/lists/{lst}",
                     f"/beta/sites/{site}/lists/{lst}/items/7",
                     "/beta" + LIST_PERMS.format(7) + "/1",
                     f"/beta/drives/{DRIVE}/items/F1/permissions"):
            with self.assertRaises(ReadOnlyGuardError, msg=path):
                check_request("GET", "https://graph.microsoft.com" + quote(path, safe="/,"),
                              environ={})


class ListItemAclFallbackTest(tcl.CaptureCase):
    def permission_gets(self) -> list[str]:
        return [p for p in self.graph.gets() if p.endswith("/permissions")]

    def test_a_refused_v1_read_falls_back_to_each_items_beta_permissions(self):
        self.graph.v1_permissions = 400   # Graph v1.0: no listItem permissions segment
        for n in (1, 10, 60):   # one v1.0 refusal per list run, then one beta GET per item
            with self.subTest(items=n):
                self.graph.lists[tcl.LIST]["items"] = {}
                for i in range(1, n + 1):
                    self.graph.add(i)
                self.graph.perms = {"2": [{"id": "9", "roles": ["read"], "grantedToV2": {
                    "user": {"email": "someone.else@example-plx.com"}}}]}
                self.graph.calls.clear()
                run = self.capture.run(tcl.scope(page_size=7))["lists"][0]
                self.assertEqual((run["status"], run["counts"]["acl_read_beta"]),
                                 ("complete", n), run)
                self.assertEqual(self.permission_gets(),
                                 ["/v1.0" + LIST_PERMS.format(1)]
                                 + ["/beta" + LIST_PERMS.format(i) for i in range(1, n + 1)])
                self.assertEqual(sorted(int(r["key"]["id"]) for r in self.visible(tcl.READER)),
                                 [i for i in range(1, n + 1) if i != 2])
                self.assertEqual(self.visible(tcl.OUTSIDER), [])
        self.assertIn("graph.list_item_permissions_beta", self.transport.operations_read)

    def test_the_first_beta_403_or_404_stops_permission_reads_for_the_whole_list(self):
        self.graph.v1_permissions = 403
        for status, reason, blocked in (
                (403, "blocked:missing_permission:Sites.Read.All",
                 [("sharepoint_list.item_permissions", 403)]),
                (404, "acl_endpoint_not_found", [])):
            with self.subTest(status=status):
                self.graph.beta_permissions = status
                self.graph.calls.clear()
                run = self.capture.run(tcl.scope())
                self.assertEqual(run["lists"][0]["reasons"], [reason])
                self.assertEqual([(p["part"], p["http_status"])
                                  for p in run["blocked_upstream"]], blocked)
                self.assertEqual(self.permission_gets(), ["/v1.0" + LIST_PERMS.format(1),
                                                          "/beta" + LIST_PERMS.format(1)])
                self.assertEqual(run["lists"][0]["counts"]["acl_unreadable"], 5)
                self.assertEqual(self.visible(tcl.READER), [])
                state = self.store.operator_completeness(tcl.CAPTURE, csl.LIST_KIND.name,
                                                         self.unit)
                self.assertEqual((state["state"], state["complete"]), ("partial", False))

    def test_a_resumed_run_is_not_complete_while_an_earlier_items_acl_is_unreadable(self):
        capped = tcl.scope(page_size=2, max_pages_per_run=1)
        self.graph.v1_permissions, self.graph.beta_permissions = 400, 404
        first = self.capture.run(capped)["lists"][0]
        self.assertEqual(first["reasons"], ["acl_endpoint_not_found", "cap:page_cap"])
        self.graph.v1_permissions = self.graph.beta_permissions = None   # readable again
        self.capture.run(capped)
        resumed = self.capture.run(capped)["lists"][0]
        # This run read every permission it asked for, but items 1-2 (saved by the first
        # run) still hold no readable ACL: not complete, and the store says so.
        self.assertEqual((resumed["resumed_from"], resumed["counts"]["acl_unreadable"]),
                         ("cursor", 0))
        self.assertEqual((resumed["status"], resumed["reasons"]),
                         ("incomplete", ["acl_unreadable"]))
        state = self.store.operator_completeness(tcl.CAPTURE, csl.LIST_KIND.name, self.unit)
        self.assertEqual((state["state"], state["reason"]), ("partial", "acl_unreadable"))
        self.assertEqual(sorted(r["key"]["id"] for r in self.visible(tcl.READER)),
                         ["3", "4", "5"])
        fresh = self.capture.run(tcl.scope())["lists"][0]   # a new run re-reads them all
        self.assertEqual((fresh["resumed_from"], fresh["status"]), (None, "complete"))
        self.assertEqual(len(self.visible(tcl.READER)), 5)

    def test_an_item_deleted_while_its_acl_was_unreadable_does_not_hold_the_list_partial(self):
        self.graph.v1_permissions = self.graph.beta_permissions = 403
        self.assertEqual(self.capture.run(tcl.scope())["lists"][0]["status"], "blocked")
        # The grant is restored and item 5 is deleted at source: its unreadable ACL can
        # never be read again, so it must not keep every later run partial.
        self.graph.v1_permissions = self.graph.beta_permissions = None
        del self.graph.lists[tcl.LIST]["items"]["5"]
        run = self.capture.run(tcl.scope())["lists"][0]
        self.assertEqual((run["status"], run["reasons"]), ("complete", []))
        state = self.store.operator_completeness(tcl.CAPTURE, csl.LIST_KIND.name, self.unit)
        self.assertEqual((state["state"], state["records_missing"]), ("complete", 1))


EMPLOYEE = Actor("plx", frozenset({"employee:" + OID}))
ROLE = "staff:operations"   # the role principal the fixture employee (Portal MANAGER) holds
EMPLOYEE_EMAIL = "ricardo@petralabx.com"   # portal_user's address, as Graph lower-cases it


class ListItemTrimTest(unittest.TestCase):
    """``sharepoint-list://`` through ``plx.read`` of the case host, item by item: an
    employee by its own identities, any other reader by its item permissions even where the
    audience file requires no source ACL (``sharepoint:list:`` always does)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        graph = tcl.FakeListGraph()
        graph.v1_permissions = 400   # the permissions come from the beta fallback
        graph.perms = {"1": [{"id": "1", "roles": ["read"], "grantedToV2": {
                                 "user": {"email": EMPLOYEE_EMAIL}}}],
                       "2": [{"id": "1", "roles": ["write"], "grantedToV2": {
                                 "user": {"id": OID}}}],
                       "3": [{"id": "1", "roles": ["read"], "grantedToV2": {
                                 "user": {"email": "someone.else@example-plx.com"}}}]}
        audience = LiveSourceAudience(
            {"sharepoint:list:*": ["svc:sharepoint-list-capture", ROLE, "staff:list-reader",
                                   "staff:list-outsider"]},
            tenant="plx", classifications={"sharepoint:list:*": "internal"},
            identities={ROLE: [tcl.OWNER], "staff:list-reader": [tcl.OWNER],
                        "staff:list-outsider": ["outsider@example-plx.com"]})
        store = csl.open_store(tmp.name, audience)
        try:
            api, _ = graph_read.build_graph(tcl.ENV, sender=graph)
            self.assertTrue(csl.ListCapture(store, api, audience, tcl.CAPTURE)
                            .run(tcl.scope())["complete"])
            self.cited = {path[2]: make_handle(csl.LIST_KIND, path, store.generation, capture)
                          for path, capture in store.current_versions(
                              csl.LIST_KIND.name, tcl.scope().lists[0].unit)}
        finally:
            store.close()
        SharePointCaptureStore(tmp.name).db.close()   # an empty drive capture beside it
        projection = Path(tmp.name) / "projection.sqlite"
        with SharePointCaptures(tmp.name) as captured:
            build_store(projection, schema_loader.load_schema(), {"sharepoint": captured},
                        clock=lambda: dt.datetime.now(dt.timezone.utc))
        resolver = EmployeePermissionResolver(EmployeePermissionPolicy.from_json(POLICY),
                                              Calls(portal_user("MANAGER")), Calls(member()))
        self.runtime = CaseRuntime(tmp.name, client=None, audience=AudienceMap({}, tenant="plx"),
                                   tenant="plx", live=LiveReaders(audience),
                                   employee_permissions=resolver)
        self.addCleanup(self.runtime.close)
        self.projection = projection

    def read(self, actor: Actor) -> dict[str, dict]:
        gated = self.runtime.employee_gate(actor)
        host = CaseHostTools(self.runtime, gated, store_path=self.projection)
        self.addCleanup(host.close)
        return {item: host.call("plx.read", {"citation": handle}, gated)
                for item, handle in sorted(self.cited.items())}

    def test_each_reader_reads_only_the_items_their_own_permissions_name(self):
        expected = {EMPLOYEE: ["1", "2"], tcl.READER: ["4", "5"], tcl.OUTSIDER: [],
                    tcl.WITHHELD: []}
        for actor, items in expected.items():
            with self.subTest(actor=sorted(actor.principals)):
                out = self.read(actor)
                self.assertEqual([i for i, r in out.items() if r["status"] == "resolved"],
                                 items)
                hidden = [r for i, r in out.items() if i not in items]
                self.assertEqual({r["status"] for r in hidden},
                                 {"withheld"} if actor is tcl.WITHHELD else {"unresolvable"})
                self.assertNotIn(tcl.VENDOR, json.dumps(hidden))
        row = self.read(tcl.READER)["4"]
        self.assertEqual((row["lineage"], row["citation"]), ("sharepoint", self.cited["4"]))
        fields = row["fields"]
        self.assertEqual(fields["row.fields.VendorName"]["untrusted_text"], tcl.VENDOR)
        self.assertEqual(fields["row.fields.QuotedUnitPrice"]["untrusted_text"], "1.25")
        self.assertNotIn("row.fields.InternalNote", fields)   # never captured


if __name__ == "__main__":
    unittest.main()
