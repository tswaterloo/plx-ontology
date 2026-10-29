"""Injected-transport tests for continuous ingestion with completeness tracking.

Every read goes through the real GET-only readers (``GraphMailReader``,
``SharePointReader``, ``BusinessCentralReader`` over ``GuardedTransport``) and
the real ``SourceMirror`` + ``WorkbenchReadApiClient``; only the senders are
in-process fakes. No network, no credentials. Evidence label:
``injected-transport``.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from integrations import bc_read, graph_read
from integrations.live_source_guard import LiveSourceAudience
from integrations.source_mirror import SourceMirror
from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.core import Actor
from operating import ingestion
from operating.ingestion import IngestError, Ingestor, load_sources, row_data_check, status


ROOT = Path(__file__).resolve().parents[2]
TENANT_ID = "11111111-2222-3333-4444-555555555555"
SITE_ID = ("petrasoap.sharepoint.com,11111111-0000-0000-0000-000000000001,"
           "11111111-0000-0000-0000-000000000002")
ENV = {"PLX_EXCLUDED_PEOPLE_OIDS": "0e0e0e0e-0000-4000-8000-00000000e0e1",
       "MICROSOFT_GRAPH_TENANT_ID": TENANT_ID,
       "MICROSOFT_GRAPH_CLIENT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
       "MICROSOFT_GRAPH_CLIENT_SECRET": "s3cr3t~CLIENT~value~do-not-leak",
       "BC_TENANT_ID": TENANT_ID, "BC_CLIENT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
       "BC_CLIENT_SECRET": "s3cr3t~CLIENT~value~do-not-leak", "BC_ENVIRONMENT": "Production",
       "SHAREPOINT_SITE_ID": SITE_ID}
MAILBOX = "purchase-order@petrasoap.com"
USER_ID = "12345678-aaaa-bbbb-cccc-000000000001"
INBOX = "INBOX-PO"
DRIVE = "b!qcqa-Drive01"
COMPANY_A = "0f0e0d0c-0b0a-0908-0706-050403020100"
COMPANY_B = "0f0e0d0c-0b0a-0908-0706-0504030201ff"
ACTOR = Actor("plx", frozenset({"svc:ingest", "staff:operations"}), "restricted")
SUBJECT = "Supplier confirmation SC-778812 for PO5447"


class Crash(BaseException):
    """A process death in the middle of a run (not an Exception: nothing catches it)."""


def j(status: int, value) -> tuple[int, dict, bytes]:
    return status, {}, json.dumps(value).encode()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeSources:
    """A stateful Graph mail + drive and BC tenant behind the guarded transport."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.folders: dict[str, dict] = {}   # item id -> folder item
        self.children: dict[str, list[dict]] = {}
        self.bc: dict[str, list[dict]] = {COMPANY_A: [], COMPANY_B: []}
        self.calls: list[str] = []
        self.crash_on: re.Pattern | None = None
        self.fail_on: tuple[re.Pattern, int] | None = None
        # (path pattern, HTTP status): fail only continuation pages ($skiptoken),
        # so the first page of a matching listing is read and the next one fails.
        self.fail_later_pages: tuple[re.Pattern, int] | None = None
        self.root = self._folder("ROOT", None, "", root=True)

    # -- builders --------------------------------------------------------------

    def add_message(self, n: int, received: str, change_key: str = "ck1") -> None:
        self.messages.append({
            "id": f"AAMkMSG{n:04d}", "changeKey": change_key, "subject": f"{SUBJECT} #{n}",
            "parentFolderId": INBOX, "lastModifiedDateTime": received,
            "receivedDateTime": received, "sentDateTime": received,
            "from": {"emailAddress": {"address": "vendor@supplier.example"}},
            "toRecipients": [{"emailAddress": {"address": MAILBOX}}],
            "webLink": "https://outlook.office365.com/owa/?ItemID=x",
            "bodyPreview": "Confirmed qty 1,920", "hasAttachments": False,
            "conversationId": "conv", "internetMessageId": "<m@x>"})

    def _folder(self, item_id: str, parent: dict | None, name: str, *, root: bool = False):
        path = "" if parent is None else (parent["_path"] + "/" + name)
        item = {"id": item_id, "name": name or "root", "eTag": f'"{{{item_id}}},1"',
                "folder": {"childCount": 0}, "_path": path}
        if root:
            item["root"] = {}
        else:
            item["parentReference"] = {"driveId": DRIVE, "id": parent["id"],
                                       "path": f"/drives/{DRIVE}/root:{parent['_path']}"}
        self.folders[item_id] = item
        self.children.setdefault(item_id, [])
        if parent is not None:
            self.children[parent["id"]].append(item)
        return item

    def add_folder(self, item_id: str, parent: dict, name: str) -> dict:
        return self._folder(item_id, parent, name)

    def rename_folder(self, item_id: str, name: str) -> None:
        """Rename in place: the item id stays, every path below it changes."""
        item = self.folders[item_id]
        old = item["_path"]
        new = old.rsplit("/", 1)[0] + "/" + name
        item["name"] = name
        prefix = f"/drives/{DRIVE}/root:"
        for folder in self.folders.values():
            if folder["_path"] == old or folder["_path"].startswith(old + "/"):
                folder["_path"] = new + folder["_path"][len(old):]
        for children in self.children.values():
            for child in children:
                ref = child.get("parentReference") or {}
                path = ref.get("path", "")[len(prefix):]
                if path == old or path.startswith(old + "/"):
                    ref["path"] = prefix + new + path[len(old):]

    def move_folder(self, item_id: str, parent_id: str) -> None:
        """Move under another parent: the item id stays, every path below it changes."""
        item, parent = self.folders[item_id], self.folders[parent_id]
        old_parent = item["parentReference"]["id"]
        self.children[old_parent] = [c for c in self.children[old_parent] if c["id"] != item_id]
        self.children[parent_id].append(item)
        item["parentReference"] = {"driveId": DRIVE, "id": parent_id,
                                   "path": f"/drives/{DRIVE}/root:{parent['_path']}"}
        old = item["_path"]
        new = parent["_path"] + "/" + item["name"]
        prefix = f"/drives/{DRIVE}/root:"
        for folder in self.folders.values():
            if folder["_path"] == old or folder["_path"].startswith(old + "/"):
                folder["_path"] = new + folder["_path"][len(old):]
        for children in self.children.values():
            for child in children:
                ref = child.get("parentReference") or {}
                path = ref.get("path", "")[len(prefix):]
                if child is not item and (path == old or path.startswith(old + "/")):
                    ref["path"] = prefix + new + path[len(old):]

    def delete_folder(self, item_id: str) -> None:
        item = self.folders.pop(item_id)
        parent = item["parentReference"]["id"]
        self.children[parent] = [c for c in self.children[parent] if c["id"] != item_id]

    def add_file(self, item_id: str, parent: dict, name: str, version: int = 1) -> dict:
        item = {"id": item_id, "name": name, "eTag": f'"{{{item_id}}},{version}"',
                "cTag": f"c{version}", "size": 5159139, "file": {"mimeType": "application/pdf"},
                "lastModifiedDateTime": "2026-02-20T16:40:47Z",
                "createdDateTime": "2026-02-20T16:40:47Z",
                "webUrl": f"https://petrasoap.sharepoint.com/sites/QCQA/{name}",
                "parentReference": {"driveId": DRIVE, "id": parent["id"],
                                    "path": f"/drives/{DRIVE}/root:{parent['_path']}"}}
        self.children[parent["id"]].append(item)
        return item

    def add_bc(self, company: str, n: int, etag: str = "W/\"1\"") -> None:
        self.bc[company].append({"@odata.etag": etag, "id": f"aaaaaaaa-0000-0000-0000-{n:012d}",
                                 "number": f"PO{n:05d}", "vendorName": "Vendor Ltd",
                                 "lastModifiedDateTime": f"2026-09-{10 + n % 20:02d}T08:00:00Z"})

    # -- sender ----------------------------------------------------------------

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com":
            return j(200, {"access_token": "eyJ0eXAi.ACCESS.sig", "expires_in": 3599})
        path = unquote(parts.path)
        self.calls.append(path)
        if self.crash_on is not None and self.crash_on.search(path):
            raise Crash()
        if self.fail_on is not None and self.fail_on[0].search(path):
            return j(self.fail_on[1], {"error": {"code": "serviceUnavailable"}})
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        if (self.fail_later_pages is not None and "$skiptoken" in query
                and self.fail_later_pages[0].search(path)):
            return j(self.fail_later_pages[1], {"error": {"code": "serviceUnavailable"}})
        if path == "/v1.0/users":
            # The people-exclusion guard's one directory listing (names nobody).
            return j(200, {"value": [{"id": USER_ID, "userPrincipalName": MAILBOX,
                                      "mail": MAILBOX, "proxyAddresses": [f"SMTP:{MAILBOX}"]}]})
        if re.fullmatch(r"/v1\.0/users/[^/]+", path):
            who = path.rsplit("/", 1)[1].lower()
            if who in (MAILBOX, USER_ID):
                return j(200, {"id": USER_ID, "userPrincipalName": MAILBOX, "mail": MAILBOX,
                               "proxyAddresses": [f"SMTP:{MAILBOX}"]})
            return j(404, {"error": {"code": "ResourceNotFound"}})
        if re.fullmatch(rf"/v1\.0/users/{USER_ID}/mailFolders/[^/]+", path):
            name = path.rsplit("/", 1)[1].lower()
            return j(200, {"id": INBOX}) if name in ("inbox", INBOX.lower()) else \
                j(404, {"error": {"code": "NotFound"}})
        if path == f"/v1.0/users/{USER_ID}/mailFolders/{INBOX}/messages":
            return self._messages(url, query)
        drive = f"/v1.0/drives/{DRIVE}"
        if path == f"{drive}/root":
            return j(200, self._public(self.root))
        if path.startswith(f"{drive}/root:") and path.endswith(":"):
            wanted = path[len(f"{drive}/root:"):-1].rstrip("/")
            for item in self.folders.values():
                if item["_path"] == wanted:
                    return j(200, self._public(item))
            return j(404, {"error": {"code": "itemNotFound"}})
        match = re.fullmatch(rf"{re.escape(drive)}/items/([^/]+)", path)
        if match and match[1] in self.folders:
            return j(200, self._public(self.folders[match[1]]))
        match = re.fullmatch(rf"{re.escape(drive)}/items/([^/]+)/children", path)
        if match and match[1] in self.children:
            return self._page(url, query, [self._public(i) for i in self.children[match[1]]],
                              int(query.get("$top", "200")))
        match = re.fullmatch(r"/v2\.0/[^/]+/Production/api/v2\.0/companies", path)
        if match:
            return j(200, {"value": [{"id": c, "name": f"Company {i}",
                                      "@odata.etag": "W/\"c\""}
                                     for i, c in enumerate(sorted(self.bc))]})
        match = re.fullmatch(r"/v2\.0/[^/]+/Production/api/v2\.0/companies\(([^)]+)\)/"
                             r"purchaseOrders", path)
        if match:
            size = int(re.search(r"maxpagesize=(\d+)", headers.get("Prefer", "")).group(1))
            return self._page(url, query, list(self.bc[match[1]]), size)
        return j(404, {"error": {"code": "itemNotFound"}})

    @staticmethod
    def _public(item: dict) -> dict:
        return {k: v for k, v in item.items() if not k.startswith("_")}

    def _page(self, url, query, values, size):
        start = int(query.get("$skiptoken", "0"))
        body = {"value": values[start:start + size]}
        if start + size < len(values):
            parts = urlsplit(url)
            rest = {k: v for k, v in query.items() if k != "$skiptoken"}
            rest["$skiptoken"] = str(start + size)
            body["@odata.nextLink"] = f"https://{parts.hostname}{parts.path}?{urlencode(rest)}"
        return j(200, body)

    def _messages(self, url, query):
        rows = list(self.messages)
        match = re.fullmatch(r"receivedDateTime ge (\S+)", query.get("$filter", ""))
        if match:
            rows = [m for m in rows if m["receivedDateTime"] >= match[1]]
        descending = query.get("$orderby", "").endswith("desc")
        rows.sort(key=lambda m: (m["receivedDateTime"], m["id"]), reverse=descending)
        return self._page(url, query, rows, int(query.get("$top", "25")))


AUDIENCE = LiveSourceAudience({f"mail:{MAILBOX}": ["svc:ingest"],
                               f"sharepoint:{DRIVE}:/AOs*": ["svc:ingest"],
                               "bc:production:*": ["svc:ingest"]}, tenant="plx")


def sources_document(**overrides) -> dict:
    sources = [
        {"id": "portal.assembly-order", "kind": "portal_relation",
         "relation": "public.assembly_order"},
        {"id": "mail.purchase-order", "kind": "mail_folder", "mailbox": MAILBOX,
         "folder": "inbox", "start_at": "2026-09-01T00:00:00Z", "page_size": 2,
         "max_pages": 2, "max_items": 4, "overlap_seconds": 600},
        {"id": "sp.qcqa.aos", "kind": "sharepoint_folder", "drive_id": DRIVE,
         "path": "/AOs/2026 - AOs", "recursive": True, "page_size": 10},
        {"id": "bc.purchase-orders", "kind": "bc_entity_set", "entity_set": "purchaseOrders",
         "page_size": 2, "max_pages": 2, "max_items": 4},
    ]
    for source in sources:
        source.update(overrides.get(source["id"], {}))
    return {"schema": "plx-ingest-sources/v1", "tenant": "plx",
            "actor": {"principals": ["svc:ingest", "staff:operations"],
                      "clearance": "restricted"},
            "sources": [s for s in sources if s["id"] not in overrides.get("drop", ())]}


class IngestionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "state"
        self.clock = Clock()
        self.fake = FakeSources()
        self.workbench = FakeWorkbench()
        self.mirror_rows = 100_000
        aos = self.fake.add_folder("AOS", self.fake.root, "AOs")
        self.year = self.fake.add_folder("Y2026", aos, "2026 - AOs")
        for index, name in enumerate(("AO-26005B.pdf", "AO-26006.pdf", "AO-26007.pdf")):
            self.fake.add_file(f"PDF{index}", self.year, name)
        self.sub = self.fake.add_folder("SUB", self.year, "Rejected")
        self.fake.add_file("PDF9", self.sub, "AO-26009.pdf")
        for n, day in enumerate(("2026-09-02", "2026-09-03", "2026-09-04")):
            self.fake.add_message(n, f"{day}T10:00:00Z")
        for n in range(3):
            self.fake.add_bc(COMPANY_A, n)
        self.fake.add_bc(COMPANY_B, 7)

    def ingestor(self, document=None, *, audience=None, env=ENV) -> Ingestor:
        tenant, actor, specs = load_sources(document or sources_document(), env)
        api, transport = graph_read.build_graph(env, sender=self.fake)
        bc_api, _t, tenant_id, environment = bc_read.build_bc(ENV, sender=self.fake)
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.workbench)
        audience = audience or AUDIENCE
        mirror = SourceMirror(self.state / "mirror.sqlite", client,
                              {"public.*": ["staff:operations"]}, clock=self.clock,
                              retry_delay_seconds=0, max_rows_per_relation=self.mirror_rows)
        self.addCleanup(mirror.close)
        ingestor = Ingestor(
            self.state, specs, actor=actor, mirror=mirror,
            mail=graph_read.GraphMailReader(api, audience),
            sharepoint={DRIVE: graph_read.SharePointReader(api, audience, DRIVE,
                                                           transport=transport)},
            bc=bc_read.BusinessCentralReader(bc_api, audience, tenant_id=tenant_id,
                                             environment=environment),
            clock=self.clock)
        self.addCleanup(ingestor.close)
        return ingestor

    @staticmethod
    def units(result) -> dict:
        return {u["unit_id"]: u for u in result["units"]}

    def cursors(self, ingestor) -> dict:
        return {row["unit_id"]: json.loads(row["cursor_json"])
                for row in ingestor.store.db.execute("SELECT * FROM unit_cursor")}


class StateLabelTests(IngestionTestCase):
    def test_ingestion_labels_the_state_directory_live(self):
        from operating import session_identity as si

        self.assertIsNone(si.state_label(self.state))
        self.ingestor()
        self.assertEqual(si.state_label(self.state), "live")
        with self.assertRaises(si.SessionIdentityRefused):  # a fixture host is refused now
            si.bind_state_dir(self.state, fixture=True)

    def test_ingestion_refuses_a_fixture_labelled_directory(self):
        from operating import session_identity as si

        si.bind_state_dir(self.state, fixture=True)
        with self.assertRaises(IngestError) as caught:
            self.ingestor()
        self.assertEqual(caught.exception.code, "ingest_state_is_fixture")
        self.assertEqual(si.state_label(self.state), "fixture")


class IncrementalAndIdempotentTests(IngestionTestCase):
    def test_first_run_ingests_every_source_complete_with_cursors(self):
        result = self.ingestor().run()
        units = self.units(result)
        self.assertEqual(result["summary"]["incomplete"], 0, units)
        mail = units["mail.purchase-order"]
        self.assertEqual(mail["state"], "complete")
        self.assertEqual((mail["counts"]["new"], mail["counts"]["pages"]), (3, 2))
        self.assertEqual(mail["lag"]["lag_seconds"], 0.0)
        sp = units["sp.qcqa.aos"]
        self.assertEqual((sp["state"], sp["counts"]["new"], sp["counts"]["folders_listed"]),
                         ("complete", 5, 2))  # 3 PDFs + subfolder + its PDF
        portal = units["portal.assembly-order"]
        self.assertEqual((portal["state"], portal["counts"]["items_read"]), ("complete", 4))
        bc = [u for u in result["units"] if u["kind"] == "bc_entity_set"]
        self.assertEqual(sorted((u["state"], u["counts"].get("new", 0)) for u in bc),
                         [("complete", 0), ("complete", 1), ("complete", 3)])
        self.assertEqual(bc[0]["unit_id"], "bc.purchase-orders.companies")

    def test_rerun_is_idempotent_and_incremental_run_advances_only_new(self):
        ingestor = self.ingestor()
        ingestor.run()
        first = self.cursors(ingestor)
        items = ingestor.store.db.execute("SELECT COUNT(*) FROM item").fetchone()[0]
        again = self.units(ingestor.run())
        for unit in again.values():
            self.assertEqual(unit["state"], "complete", unit)
            self.assertEqual((unit["counts"]["new"], unit["counts"]["changed"]), (0, 0), unit)
        second = self.cursors(ingestor)
        self.assertEqual(first["mail.purchase-order"], second["mail.purchase-order"])
        self.assertEqual(first["portal.assembly-order"]["portal"],
                         second["portal.assembly-order"]["portal"])
        self.assertEqual(items, ingestor.store.db.execute("SELECT COUNT(*) FROM item")
                         .fetchone()[0])
        # The mail re-read is incremental: from high-water minus the overlap only.
        self.assertEqual(again["mail.purchase-order"]["counts"]["unchanged"], 1)
        # New source content: exactly that is new, cursors move forward.
        self.fake.add_message(9, "2026-09-05T10:00:00Z")
        self.fake.messages[0]["changeKey"] = "ck2"  # an edit of an old message: not re-read
        self.fake.add_file("PDF4", self.year, "AO-26010.pdf")
        self.fake.children["Y2026"][0]["eTag"] = '"{PDF0},2"'
        self.workbench.update("public.assembly_order", 10718, qtytotal="1503",
                              audittimestampmodified="2026-09-29T11:30:00")
        self.clock.advance(60)
        third = self.units(ingestor.run())
        self.assertEqual((third["mail.purchase-order"]["counts"]["new"],
                          third["mail.purchase-order"]["counts"]["changed"]), (1, 0))
        self.assertEqual(self.cursors(ingestor)["mail.purchase-order"]["received_high_water"],
                         "2026-09-05T10:00:00Z")
        self.assertEqual((third["sp.qcqa.aos"]["counts"]["new"],
                          third["sp.qcqa.aos"]["counts"]["changed"]), (1, 1))
        self.assertEqual(third["portal.assembly-order"]["counts"]["refresh_mode_incremental"], 1)
        self.assertEqual(self.cursors(ingestor)["portal.assembly-order"]["portal"]
                         ["watermark"], "2026-09-29T11:30:00.000000Z")

    def test_clean_traversal_marks_unreturned_items_absent_never_on_a_capped_one(self):
        ingestor = self.ingestor()
        ingestor.run()
        removed = self.fake.children["Y2026"].pop(1)
        self.assertEqual(self.units(ingestor.run())["sp.qcqa.aos"]["counts"]["absent_marked"],
                         1)
        absent = ingestor.store.db.execute(
            "SELECT item_key FROM item WHERE absent_since_run IS NOT NULL").fetchall()
        self.assertEqual([r[0] for r in absent], [removed["id"]])
        # A capped traversal proves nothing about absence.
        self.fake.children["Y2026"].pop(0)
        capped = self.ingestor(sources_document(**{"sp.qcqa.aos": {"max_items": 1}}))
        unit = self.units(capped.run())["sp.qcqa.aos"]
        self.assertEqual(unit["state"], "incomplete")
        self.assertEqual(unit["counts"]["absent_marked"], 0)


class CompletenessTests(IngestionTestCase):
    def test_capped_mail_is_incomplete_with_exact_reason_and_drains_over_runs(self):
        for n, day in enumerate(("05", "06", "07", "08"), start=10):
            self.fake.add_message(n, f"2026-09-{day}T10:00:00Z")
        ingestor = self.ingestor()
        first = self.units(ingestor.run(only=["mail.purchase-order"]))["mail.purchase-order"]
        self.assertEqual(first["state"], "incomplete")
        self.assertEqual(first["reasons"], ["cap:page_cap"])  # 2 pages of 2, more follow
        self.assertGreater(first["lag"]["lag_seconds"], 0)
        # Oldest-first: the cursor sits at the newest message read, so nothing older is
        # skipped, and the backlog drains.
        self.assertEqual(self.cursors(ingestor)["mail.purchase-order"]["received_high_water"],
                         "2026-09-05T10:00:00Z")
        second = self.units(ingestor.run(only=["mail.purchase-order"]))["mail.purchase-order"]
        self.assertEqual((second["state"], second["counts"]["new"]), ("complete", 3))
        self.assertEqual(ingestor.store.db.execute(
            "SELECT COUNT(*) FROM item WHERE unit_id = 'mail.purchase-order'").fetchone()[0], 7)
        sent = [c for c in self.fake.calls if c.endswith(f"/{INBOX}/messages")]
        self.assertTrue(sent)

    def test_mail_unit_exclusion_unavailable_is_typed_block(self):
        # No PLX_EXCLUDED_PEOPLE_OIDS: the people guard refuses the mailbox before any
        # request for it. The unit says so by code (never "not_visible", never an id),
        # the other units still run, and the run report names the typed block.
        unset = {k: v for k, v in ENV.items() if k != "PLX_EXCLUDED_PEOPLE_OIDS"}
        ingestor = self.ingestor(env=unset)
        result = self.units(ingestor.run())
        mail = result["mail.purchase-order"]
        self.assertEqual((mail["state"], mail["reasons"]),
                         ("incomplete", ["blocked:people_exclusion:excluded_people_oids_absent"]))
        self.assertEqual(mail["counts"]["new"], 0)
        self.assertFalse([c for c in self.fake.calls if "/messages" in c or "/users/" in c])
        self.assertTrue(any(u["state"] == "complete" for k, u in result.items()
                            if not k.startswith("mail.")))
        self.assertEqual(ingestion.blocked_upstream(result.values()), [
            {"part": "mail.purchase-order", "source": "people-exclusion",
             "http_status": None, "error_code": "excluded_people_oids_absent",
             "cause": "later_ask", "later_ask": "excluded-people-oids"}])
        self.assertEqual(ingestion.blocked_upstream(
            self.units(self.ingestor().run(only=["mail.purchase-order"])).values()), [])

    def test_mail_unit_with_a_failed_directory_listing_is_a_source_error_not_a_typed_block(self):
        # The ID list is set but the guard's /users listing answers 403 or 500: the
        # guard is unresolved and the mailbox is still never read, but the reason is the
        # listing's own failure, never the later_ask (that waits on the list, not Graph).
        for status in (403, 500):
            with self.subTest(status=status):
                self.fake.calls.clear()
                self.fake.fail_on = (re.compile(r"^/v1\.0/users$"), status)
                result = self.units(self.ingestor().run(only=["mail.purchase-order"]))
                reasons = result["mail.purchase-order"]["reasons"]
                self.assertEqual(len(reasons), 1)
                self.assertTrue(reasons[0].startswith("source_error:"), reasons)
                self.assertNotIn("people_exclusion", reasons[0])
                self.assertFalse([c for c in self.fake.calls if "/messages" in c])
                self.assertEqual(ingestion.blocked_upstream(result.values()), [])

    def test_mail_query_is_oldest_first_from_high_water_minus_overlap(self):
        ingestor = self.ingestor()
        ingestor.run(only=["mail.purchase-order"])
        captured = []
        original = self.fake._messages
        self.fake._messages = lambda url, query: (captured.append(dict(query)),
                                                  original(url, query))[1]
        ingestor.run(only=["mail.purchase-order"])
        incremental = [q for q in captured if q.get("$orderby") == "receivedDateTime asc"]
        self.assertEqual(incremental[0]["$filter"], "receivedDateTime ge 2026-09-04T09:50:00Z")

    def test_dense_window_stalls_then_composite_cursor_tie_break_drains_it(self):
        for n in range(10, 16):
            self.fake.add_message(n, "2026-09-04T10:00:00Z")  # 7 messages, one instant
        ingestor = self.ingestor()
        run = lambda: self.units(ingestor.run(only=["mail.purchase-order"]))[
            "mail.purchase-order"]
        run()
        # New ids at the high-water second are progress, not a stall.
        progress = run()
        self.assertEqual((progress["reasons"], progress["counts"]["new"]),
                         (["cap:page_cap"], 2))
        # The same first page again: nothing new past the composite cursor.
        stalled = run()
        self.assertEqual(stalled["reasons"], ["cap:page_cap", "cursor_stalled"])
        self.assertTrue(self.cursors(ingestor)["mail.purchase-order"]["stalled"])
        recovered = run()
        self.assertEqual((recovered["state"], recovered["counts"]["stall_recovery_read"],
                          recovered["counts"]["new"]), ("complete", 1, 3))
        self.assertEqual(ingestor.store.db.execute(
            "SELECT COUNT(*) FROM item WHERE unit_id = 'mail.purchase-order'").fetchone()[0], 9)
        self.assertFalse(self.cursors(ingestor)["mail.purchase-order"]["stalled"])
        self.assertEqual(run()["counts"]["stall_recovery_read"], 0)

    def stall_inside_overlap(self, late: int):
        """Run 1 reads to 10:00; ``late`` messages then arrive inside the overlap
        window (older received time), more than the normal cap: run 2 stalls."""
        ingestor = self.ingestor()
        run = lambda: self.units(ingestor.run(only=["mail.purchase-order"]))[
            "mail.purchase-order"]
        self.assertEqual(run()["state"], "complete")
        for n in range(late):
            self.fake.add_message(30 + n, f"2026-09-04T09:5{1 + n}:00Z")
        self.fake.add_message(40, "2026-09-04T10:30:00Z")
        self.assertIn("cursor_stalled", run()["reasons"])
        return ingestor, run

    def test_stall_recovery_still_reads_the_overlap_window(self):
        ingestor, run = self.stall_inside_overlap(5)
        # Arrives late with a received time inside the overlap window.
        self.fake.add_message(50, "2026-09-04T09:59:00Z")
        recovered = run()
        self.assertEqual((recovered["state"], recovered["counts"]["stall_recovery_read"]),
                         ("complete", 1))
        keys = {r[0] for r in ingestor.store.db.execute(
            "SELECT item_key FROM item WHERE unit_id = 'mail.purchase-order'")}
        self.assertIn("AAMkMSG0050", keys)
        self.assertIn("AAMkMSG0040", keys)
        self.assertFalse(self.cursors(ingestor)["mail.purchase-order"]["stalled"])

    def test_recovery_that_still_ends_inside_the_window_progresses_and_says_so(self):
        ingestor, run = self.stall_inside_overlap(6)
        original = ingestion.STALL_RECOVERY_ITEMS
        ingestion.STALL_RECOVERY_ITEMS = 5  # the window is denser than the widened read
        self.addCleanup(setattr, ingestion, "STALL_RECOVERY_ITEMS", original)
        recovered = run()
        self.assertEqual((recovered["state"], recovered["reasons"]),
                         ("incomplete", ["overlap_window_unread"]))
        keys = {r[0] for r in ingestor.store.db.execute(
            "SELECT item_key FROM item WHERE unit_id = 'mail.purchase-order'")}
        self.assertIn("AAMkMSG0040", keys)  # the cursor moved past the high-water second
        self.assertNotIn("AAMkMSG0035", keys)  # the unread rest of the window is reported
        self.assertFalse(self.cursors(ingestor)["mail.purchase-order"]["stalled"])

    def test_overflowing_overlap_gap_is_persisted_and_read_before_complete(self):
        """Review minor (perm-ingest): every read of the recovery run is counted,
        and the unread rest of the overlap window stays a reason on later runs
        until a run has read it."""
        ingestor, run = self.stall_inside_overlap(6)
        original = ingestion.STALL_RECOVERY_ITEMS
        ingestion.STALL_RECOVERY_ITEMS = 5
        self.addCleanup(setattr, ingestion, "STALL_RECOVERY_ITEMS", original)
        recovered = run()
        self.assertEqual(recovered["reasons"], ["overlap_window_unread"])
        # The widened read (5 in the window) and the high-water read (10:00, 10:30).
        self.assertEqual((recovered["counts"]["items_read"], recovered["counts"]["pages"]),
                         (7, 2))
        cursor = self.cursors(ingestor)["mail.purchase-order"]
        self.assertEqual(cursor["overlap_gap"], {"from": "2026-09-04T09:55:00Z",
                                                 "to": "2026-09-04T10:00:00Z"})
        keys = lambda: {r[0] for r in ingestor.store.db.execute(
            "SELECT item_key FROM item WHERE unit_id = 'mail.purchase-order'")}
        self.assertNotIn("AAMkMSG0035", keys())
        # More mail arrives late inside the gap than one widened read holds.
        for n in range(6):
            self.fake.add_message(60 + n, f"2026-09-04T09:57:0{n}Z")
        still = run()
        self.assertEqual((still["state"], still["reasons"], still["counts"]["overlap_gap_read"]),
                         ("incomplete", ["overlap_window_unread"], 1))
        self.assertIn("AAMkMSG0035", keys())
        self.assertEqual(self.cursors(ingestor)["mail.purchase-order"]["overlap_gap"],
                         {"from": "2026-09-04T09:57:02Z", "to": "2026-09-04T10:00:00Z"})
        drained = run()
        self.assertEqual((drained["state"], drained["reasons"]), ("complete", []))
        self.assertTrue({f"AAMkMSG00{60 + n}" for n in range(6)} <= keys())
        self.assertIsNone(self.cursors(ingestor)["mail.purchase-order"]["overlap_gap"])
        self.assertNotIn("overlap_gap_read", run()["counts"])

    def test_failed_gap_read_keeps_the_gap_and_says_why(self):
        ingestor, run = self.stall_inside_overlap(6)
        original = ingestion.STALL_RECOVERY_ITEMS
        ingestion.STALL_RECOVERY_ITEMS = 5
        self.addCleanup(setattr, ingestion, "STALL_RECOVERY_ITEMS", original)
        run()
        reader = ingestor.mail
        calls = []
        real = reader.list_messages

        def flaky(*args, **kwargs):
            calls.append(kwargs.get("received_since"))
            if kwargs.get("received_since") == "2026-09-04T09:55:00Z":
                from integrations.http import SourceUnavailable
                raise SourceUnavailable("graph GET returned HTTP 503")
            return real(*args, **kwargs)

        reader.list_messages = flaky
        unit = run()
        self.assertEqual((unit["state"], unit["reasons"]),
                         ("incomplete", ["overlap_window_unread",
                                         "source_error:SourceUnavailable:http_503"]))
        self.assertEqual(self.cursors(ingestor)["mail.purchase-order"]["overlap_gap"]["from"],
                         "2026-09-04T09:55:00Z")

    def test_failure_after_a_finished_read_keeps_its_counts_and_the_partials(self):
        """The widened overlap read finished (5 items, 1 page); the high-water read
        then fails after one page. The unit keeps both reads' counts, reports
        the refusal, stores nothing and leaves the cursor where it was."""
        ingestor, run = self.stall_inside_overlap(6)
        original = ingestion.STALL_RECOVERY_ITEMS
        ingestion.STALL_RECOVERY_ITEMS = 5
        self.addCleanup(setattr, ingestion, "STALL_RECOVERY_ITEMS", original)
        reader = ingestor.mail
        real = reader.list_messages
        before = self.cursors(ingestor)["mail.purchase-order"]

        def refuse_high_water_read(*args, **kwargs):
            if kwargs.get("received_since") == "2026-09-04T10:00:00Z":
                from integrations.http import SourceUnavailable
                error = SourceUnavailable("graph nextLink escaped the collection")
                error.pages_read, error.items_read = 1, 1
                raise error
            return real(*args, **kwargs)

        reader.list_messages = refuse_high_water_read
        unit = run()
        self.assertEqual((unit["state"], unit["reasons"]),
                         ("incomplete", ["source_error:SourceUnavailable:nextlink_escaped"]))
        self.assertEqual((unit["counts"]["pages"], unit["counts"]["items_read"]), (2, 6))
        self.assertEqual((unit["counts"]["new"], unit["counts"]["changed"]), (0, 0))
        self.assertEqual(self.cursors(ingestor)["mail.purchase-order"], before)

    def test_bc_scan_failing_on_a_later_page_keeps_the_pages_it_read(self):
        """Defect repro: get_pages raised on page 2 and the BC unit reported
        pages=0 although one page (2 records) was read. The counts survive;
        nothing of the failed scan is stored and the cursor stays put."""
        self.fake.fail_later_pages = (re.compile(r"/purchaseOrders$"), 503)
        ingestor = self.ingestor()
        units = self.units(ingestor.run(only=["bc.purchase-orders"]))
        failed = [u for u in units.values() if u["reasons"]
                  and u["reasons"][0].startswith("source_error:")]
        self.assertEqual(len(failed), 1)  # COMPANY_A has 3 records -> 2 pages
        unit = failed[0]
        self.assertEqual(unit["state"], "incomplete")
        self.assertEqual((unit["counts"]["pages"], unit["counts"]["items_read"]), (1, 2))
        self.assertEqual(unit["counts"]["new"], 0)
        self.assertIsNone(ingestor.store.cursor(unit["unit_id"]))

    def test_sharepoint_listing_failing_on_a_later_page_keeps_the_pages_it_read(self):
        self.fake.fail_later_pages = (re.compile(r"/children$"), 503)
        ingestor = self.ingestor(sources_document(**{"sp.qcqa.aos": {"page_size": 2}}))
        unit = self.units(ingestor.run(only=["sp.qcqa.aos"]))["sp.qcqa.aos"]
        self.assertEqual(unit["state"], "incomplete")
        self.assertIn("source_error:SourceUnavailable:http_503", unit["reasons"])
        self.assertEqual((unit["counts"]["pages"], unit["counts"]["items_read"]), (1, 2))
        self.assertEqual((unit["counts"]["new"], unit["counts"]["folders_listed"]), (0, 0))

    def test_not_found_on_a_later_page_keeps_the_pages_read_before_it(self):
        self.fake.fail_later_pages = (re.compile(r"/children$"), 404)
        ingestor = self.ingestor(sources_document(**{"sp.qcqa.aos": {"page_size": 2}}))
        unit = self.units(ingestor.run(only=["sp.qcqa.aos"]))["sp.qcqa.aos"]
        self.assertEqual(unit["reasons"], ["source_error:SourceNotFound:http_404",
                                           "traversal_in_progress"])
        self.assertEqual((unit["counts"]["pages"], unit["counts"]["items_read"]), (1, 2))

    def test_source_errors_carry_a_fixed_vocabulary_diagnosis(self):
        """Directive e402518f64be: throttling, outage and retry exhaustion are told
        apart; nothing from the source text reaches the reason."""
        self.fake.fail_on = (re.compile(rf"/{INBOX}/messages$"), 429)
        unit = self.units(self.ingestor().run(only=["mail.purchase-order"]))[
            "mail.purchase-order"]
        self.assertEqual(unit["reasons"], ["source_error:SourceUnavailable:http_429"])
        self.fake.fail_on = (re.compile(r"/purchaseOrders$"), 503)
        tenant, actor, specs = load_sources(sources_document(), ENV)
        sleeps: list[float] = []
        retry = bc_read.BoundedBackoff(sleep=sleeps.append, jitter=lambda: 0.0)
        bc_api, _t, tenant_id, environment = bc_read.build_bc(ENV, sender=self.fake,
                                                              retry=retry)
        ingestor = Ingestor(self.state, specs, actor=actor, clock=self.clock,
                            bc=bc_read.BusinessCentralReader(bc_api, AUDIENCE,
                                                             tenant_id=tenant_id,
                                                             environment=environment))
        self.addCleanup(ingestor.close)
        reasons = {r for u in ingestor.run(only=["bc.purchase-orders"])["units"]
                   for r in u["reasons"]}
        self.assertEqual(reasons,
                         {"source_error:SourceUnavailable:retried.http_503"})
        self.assertEqual(len(sleeps), 4)  # two companies, two bounded retries each
        for reason in reasons:
            self.assertLessEqual(len(reason), 64)  # live_verify ENUM_STRING bound

    def test_within_lower_cases_by_segment_and_refuses_dot_segments(self):
        within = ingestion._within
        self.assertTrue(within("/AOs/2026 - AOs", "/AOs/2026 - AOs"))
        self.assertTrue(within("/aos/2026 - aos/Rejected", "/AOs/2026 - AOs/"))
        self.assertTrue(within("/anything", ""))
        self.assertFalse(within("/AOs/2026 - AOs x", "/AOs/2026 - AOs"))
        self.assertFalse(within("/AOs", "/AOs/2026 - AOs"))
        # str.lower, not casefold: these are different names.
        self.assertFalse(within("/Mass/x", "/Maß"))
        self.assertFalse(within("/Maß/x", "/MASS"))
        self.assertFalse(within("/office/x", "/oﬃce"))
        self.assertTrue(within("/MAß/x", "/maß"))
        for path in ("/AOs/2026 - AOs/../Finance", "/AOs/2026 - AOs/./x",
                     "/AOs/2026 - AOs/..", "/AOs/2026 - AOs/x/../../.."):
            self.assertFalse(within(path, "/AOs/2026 - AOs"), path)
        self.assertFalse(within("/AOs/x", "/AOs/../AOs"))
        self.assertFalse(within(None, "/AOs"))

    def test_capped_portal_relation_is_incomplete_and_keeps_its_cursor(self):
        self.mirror_rows = 2  # public.assembly_order has 4 rows
        result = self.units(self.ingestor().run(only=["portal.assembly-order"]))
        unit = result["portal.assembly-order"]
        self.assertEqual((unit["state"], unit["reasons"]),
                         ("incomplete", ["cap:portal_row_bound"]))

    def test_capped_bc_scan_and_traversal_never_complete(self):
        for n in range(3, 8):
            self.fake.add_bc(COMPANY_A, n)  # 8 records > max_items 4
        result = self.ingestor().run(only=["bc.purchase-orders"])
        states = sorted((u["state"], tuple(u["reasons"])) for u in result["units"])
        self.assertIn(("incomplete", ("cap:page_cap",)), states)
        capped = [u for u in result["units"] if u["reasons"] == ["cap:page_cap"]][0]
        self.assertEqual(capped["counts"]["items_read"], 4)

    def test_folder_budget_leaves_traversal_in_progress_then_completes(self):
        document = sources_document(**{"sp.qcqa.aos": {"max_folders_per_run": 1}})
        ingestor = self.ingestor(document)
        first = self.units(ingestor.run(only=["sp.qcqa.aos"]))["sp.qcqa.aos"]
        self.assertEqual((first["state"], first["reasons"]),
                         ("incomplete", ["traversal_in_progress"]))
        self.assertEqual(first["lag"]["folders_pending"], 1)
        second = self.units(ingestor.run(only=["sp.qcqa.aos"]))["sp.qcqa.aos"]
        self.assertEqual(second["state"], "complete")

    def test_erroring_sources_report_exact_error_class(self):
        self.fake.fail_on = (re.compile(r"/children$"), 503)
        ingestor = self.ingestor()
        result = self.units(ingestor.run())
        self.assertEqual(result["sp.qcqa.aos"]["reasons"],
                         ["source_error:SourceUnavailable:http_503", "traversal_in_progress"])
        self.assertEqual(result["sp.qcqa.aos"]["state"], "incomplete")
        self.assertEqual(result["mail.purchase-order"]["state"], "complete")
        # A mailbox the directory does not know reads as not visible.
        other = self.ingestor(sources_document(**{"mail.purchase-order": {
            "mailbox": "nobody@petrasoap.com"}}))
        self.assertEqual(self.units(other.run(only=["mail.purchase-order"]))
                         ["mail.purchase-order"]["reasons"], ["not_visible"])
        # A guarded reader refuses a SharePoint path outside the audience (no I/O).
        denied = self.ingestor(sources_document(**{"sp.qcqa.aos": {"path": "/Finance"}}))
        self.assertEqual(self.units(denied.run(only=["sp.qcqa.aos"]))["sp.qcqa.aos"]
                         ["reasons"], ["not_visible", "traversal_in_progress"])

    def test_missing_environment_and_reader_are_incomplete_never_complete(self):
        document = sources_document(**{"sp.qcqa.aos": {"drive_id": "${QCQA_DRIVE_ID}"}})
        tenant, actor, specs = load_sources(document, ENV)
        ingestor = Ingestor(self.state, specs, actor=actor, clock=self.clock)
        self.addCleanup(ingestor.close)
        units = self.units(ingestor.run())
        self.assertEqual(units["sp.qcqa.aos"]["reasons"], ["not_configured:QCQA_DRIVE_ID"])
        self.assertEqual(units["mail.purchase-order"]["reasons"], ["not_configured:reader"])
        self.assertTrue(all(u["state"] == "incomplete" for u in units.values()))
        with self.assertRaises(IngestError):
            load_sources(sources_document(**{"sp.qcqa.aos": {
                "drive_id": "${MICROSOFT_GRAPH_CLIENT_SECRET}"}}), ENV)


class CrashResumeTests(IngestionTestCase):
    def test_crash_mid_traversal_resumes_from_last_committed_folder(self):
        ingestor = self.ingestor()
        self.fake.crash_on = re.compile(r"/items/SUB/children$")
        with self.assertRaises(Crash):
            ingestor.run(only=["sp.qcqa.aos"])
        committed = self.cursors(ingestor)["sp.qcqa.aos"]["traversal"]
        self.assertEqual(committed["pending"], [["/AOs/2026 - AOs/Rejected", 1, "SUB"]])
        self.assertEqual(ingestor.store.db.execute(
            "SELECT COUNT(*) FROM item").fetchone()[0], 4)
        report = status(self.state)
        self.assertEqual(report["units"][0]["state"], "interrupted")
        self.assertEqual(report["runs"]["interrupted"], 1)
        # A new process resumes: the first folder is not listed again.
        self.fake.crash_on = None
        self.fake.calls.clear()
        resumed = self.ingestor()
        result = resumed.run(only=["sp.qcqa.aos"])
        self.assertEqual(result["previous_runs_interrupted"], 1)
        unit = self.units(result)["sp.qcqa.aos"]
        self.assertEqual((unit["state"], unit["counts"]["folders_listed"],
                          unit["counts"]["new"]), ("complete", 1, 1))
        self.assertNotIn(f"/v1.0/drives/{DRIVE}/items/Y2026/children", self.fake.calls)
        self.assertEqual(status(self.state)["units"][0]["state"], "complete")

    def test_crash_in_a_later_source_keeps_earlier_commits_and_resumes(self):
        ingestor = self.ingestor()
        ingestor.run()
        before = self.cursors(ingestor)
        self.fake.add_message(9, "2026-09-05T10:00:00Z")
        self.fake.add_file("PDF4", self.year, "AO-26010.pdf")
        self.fake.crash_on = re.compile(r"/root:")  # SharePoint dies after mail committed
        self.clock.advance(60)
        with self.assertRaises(Crash):
            self.ingestor().run()
        after_crash = self.cursors(ingestor)
        self.assertEqual(after_crash["mail.purchase-order"]["received_high_water"],
                         "2026-09-05T10:00:00Z")
        self.assertEqual(after_crash["sp.qcqa.aos"], before["sp.qcqa.aos"])
        self.fake.crash_on = None
        units = self.units(self.ingestor().run())
        self.assertEqual(units["mail.purchase-order"]["counts"]["new"], 0)
        self.assertEqual(units["sp.qcqa.aos"]["counts"]["new"], 1)

    def test_second_concurrent_run_is_refused(self):
        ingestor = self.ingestor()
        with ingestor._lock():
            with self.assertRaises(IngestError) as caught:
                self.ingestor().run()
        self.assertEqual(caught.exception.code, "ingest_run_in_progress")


class StatusTests(IngestionTestCase):
    def test_status_is_counts_only(self):
        ingestor = self.ingestor()
        ingestor.run()
        report = status(self.state, sources=ingestor.sources)
        text = json.dumps(report)
        stored = [r[0] for r in ingestor.store.db.execute(
            "SELECT citation FROM item")] + [r[0] for r in ingestor.store.db.execute(
            "SELECT item_key FROM item")]
        self.assertTrue(stored)
        for value in stored + [SUBJECT, "AO-26005B", MAILBOX, USER_ID, DRIVE, COMPANY_A,
                               "2026-09-04", "Vendor Ltd", "PO00001"]:
            self.assertNotIn(value, text)
        self.assertEqual(row_data_check(self.state, text)["leaked"], 0)
        self.assertGreater(row_data_check(self.state, text)["values_checked"], 10)
        # The self-check does catch a leak.
        self.assertGreater(row_data_check(self.state, text + SUBJECT + " #0")["leaked"], 0)
        units = {u["unit_id"]: u for u in report["units"]}
        self.assertEqual(units["sp.qcqa.aos"]["items_current"], 5)
        self.assertTrue(units["mail.purchase-order"]["cursor_advanced"])
        self.assertEqual(report["summary"]["incomplete"], 0)

    def test_never_run_sources_are_listed(self):
        tenant, actor, specs = load_sources(sources_document(), ENV)
        report = status(self.state, sources=specs)
        self.assertEqual(report["summary"]["never_run"], 4)


class FrontierRepairTests(IngestionTestCase):
    """A queued folder that moved or vanished never stalls the source (review 52f6c8e)."""

    def budget_one(self) -> Ingestor:
        return self.ingestor(sources_document(**{"sp.qcqa.aos": {"max_folders_per_run": 1}}))

    def run_sp(self, ingestor) -> dict:
        return self.units(ingestor.run(only=["sp.qcqa.aos"]))["sp.qcqa.aos"]

    def test_renamed_pending_folder_is_followed_by_item_id(self):
        ingestor = self.budget_one()
        self.assertEqual(self.run_sp(ingestor)["reasons"], ["traversal_in_progress"])
        self.fake.rename_folder("SUB", "Rejected 2025")
        unit = self.run_sp(ingestor)
        self.assertEqual((unit["state"], unit["counts"]["folders_relocated"],
                          unit["counts"]["folders_listed"]), ("complete", 1, 1))
        self.assertEqual(ingestor.store.db.execute(
            "SELECT COUNT(*) FROM item WHERE item_key = 'PDF9'").fetchone()[0], 1)

    def test_deleted_pending_folder_is_dropped_typed_then_next_traversal_runs(self):
        ingestor = self.budget_one()
        self.run_sp(ingestor)
        self.fake.delete_folder("SUB")
        dropped = self.run_sp(ingestor)
        self.assertEqual((dropped["state"], dropped["reasons"], dropped["counts"]["absent_marked"]),
                         ("incomplete", ["folder_not_visible"], 0))
        self.assertNotIn("not_visible", dropped["reasons"])
        self.assertIsNone(self.cursors(ingestor)["sp.qcqa.aos"]["traversal"])
        self.fake.add_file("PDF5", self.year, "AO-26011.pdf")
        fresh = self.run_sp(ingestor)  # a new traversal starts at the configured folder
        self.assertEqual((fresh["state"], fresh["counts"]["new"]), ("complete", 1))
        # The deleted folder is absent from the clean traversal (never a deletion proof).
        absent = {r[0] for r in ingestor.store.db.execute(
            "SELECT item_key FROM item WHERE absent_since_run IS NOT NULL")}
        self.assertEqual(absent, {"SUB"})

    def test_pending_folder_moved_outside_the_configured_path_is_not_followed(self):
        ingestor = self.budget_one()
        self.run_sp(ingestor)
        # Still inside the /AOs* grant, but outside the configured /AOs/2026 - AOs.
        self.fake.move_folder("SUB", "AOS")
        unit = self.run_sp(ingestor)
        self.assertEqual((unit["state"], unit["reasons"], unit["counts"]["absent_marked"]),
                         ("incomplete", ["folder_moved_out"], 0))
        self.assertNotIn("folders_relocated", unit["counts"])
        self.assertEqual(ingestor.store.db.execute(
            "SELECT COUNT(*) FROM item WHERE item_key = 'PDF9'").fetchone()[0], 0)
        self.assertFalse(any(c.endswith("/items/SUB/children") for c in self.fake.calls))

    def test_pending_folder_moved_deeper_inside_the_configured_path_is_followed(self):
        ingestor = self.budget_one()
        self.run_sp(ingestor)
        archive = self.fake.add_folder("ARCH", self.year, "archive")
        self.fake.move_folder("SUB", archive["id"])
        unit = self.run_sp(ingestor)
        self.assertEqual(unit["counts"]["folders_relocated"], 1)
        self.assertEqual(ingestor.store.db.execute(
            "SELECT json_extract(record_json, '$.path') FROM item WHERE item_key = 'PDF9'"
        ).fetchone()[0],
            "/AOs/2026 - AOs/archive/Rejected/AO-26009.pdf")

    def test_pending_folder_hidden_by_audience_is_not_called_vanished(self):
        ingestor = self.budget_one()
        self.run_sp(ingestor)
        hidden = LiveSourceAudience({f"sharepoint:{DRIVE}:/AOs*": ["svc:ingest"],
                                     f"sharepoint:{DRIVE}:/AOs/2026 - AOs/Rejected*": []},
                                    tenant="plx")
        revoked = self.ingestor(sources_document(**{"sp.qcqa.aos": {"max_folders_per_run": 1}}),
                                audience=hidden)
        unit = self.run_sp(revoked)
        self.assertEqual((unit["state"], unit["reasons"], unit["counts"]["absent_marked"]),
                         ("incomplete", ["folder_not_visible"], 0))
        self.assertNotIn("folder_vanished", json.dumps(unit))
        self.assertEqual(ingestor.store.db.execute(
            "SELECT COUNT(*) FROM item WHERE absent_since_run IS NOT NULL").fetchone()[0], 0)

    def test_repeatedly_failing_pending_folder_is_dropped_after_three_runs(self):
        ingestor = self.budget_one()
        self.run_sp(ingestor)
        self.fake.fail_on = (re.compile(r"/items/SUB/children$"), 503)
        for _ in range(2):
            unit = self.run_sp(ingestor)
            self.assertEqual(unit["reasons"],
                             ["source_error:SourceUnavailable:http_503", "traversal_in_progress"])
        third = self.run_sp(ingestor)
        self.assertEqual((third["state"], third["reasons"]),
                         ("incomplete", ["folder_failed:SourceUnavailable"]))
        self.fake.fail_on = None
        self.assertEqual(self.run_sp(ingestor)["reasons"], ["traversal_in_progress"])
        self.assertEqual(self.run_sp(ingestor)["state"], "complete")  # new traversal done

    def test_root_folder_failure_keeps_retrying_and_is_not_dropped(self):
        ingestor = self.ingestor()
        self.fake.fail_on = (re.compile(r"/items/Y2026/children$"), 503)
        for _ in range(4):
            unit = self.run_sp(ingestor)
            self.assertEqual(unit["reasons"],
                             ["source_error:SourceUnavailable:http_503", "traversal_in_progress"])


class WithheldAndMailCaveatTests(IngestionTestCase):
    def test_withheld_items_are_a_reason_and_never_marked_absent(self):
        ingestor = self.ingestor()
        ingestor.run(only=["sp.qcqa.aos"])
        revoked = LiveSourceAudience({f"sharepoint:{DRIVE}:/AOs*": ["svc:ingest"],
                                      f"sharepoint:{DRIVE}:/AOs/2026 - AOs/AO-26006.pdf": []},
                                     tenant="plx")
        unit = self.units(self.ingestor(audience=revoked).run(only=["sp.qcqa.aos"]))[
            "sp.qcqa.aos"]
        self.assertEqual((unit["state"], unit["reasons"]), ("incomplete", ["withheld:1"]))
        self.assertEqual(unit["counts"]["absent_marked"], 0)
        self.assertEqual(ingestor.store.db.execute(
            "SELECT COUNT(*) FROM item WHERE absent_since_run IS NOT NULL").fetchone()[0], 0)

    def test_mail_complete_means_received_time_window_not_absence(self):
        ingestor = self.ingestor()
        self.assertEqual(self.units(ingestor.run(only=["mail.purchase-order"]))
                         ["mail.purchase-order"]["state"], "complete")
        # Moved in later with an older received time: not read, still "complete".
        self.fake.add_message(20, "2026-09-02T12:00:00Z")
        unit = self.units(ingestor.run(only=["mail.purchase-order"]))["mail.purchase-order"]
        self.assertEqual((unit["state"], unit["counts"]["new"]), ("complete", 0))
        report = status(self.state)
        mail = [u for u in report["units"] if u["kind"] == "mail_folder"][0]
        self.assertEqual(mail["completeness_basis"], "received_time_window_not_absence")
        self.assertTrue(any("older received time" in c for c in report["caveats"]))


class MailWindowAnchorTests(IngestionTestCase):
    """case.mail_evidence failed live (directive 9f804f59e9b4): the inbox's newest
    message was ~29 days old, so a 14-day window anchored on the clock read
    nothing. The live-check sources file anchors the first window on the
    folder's newest message instead."""

    LIVE_FILE = ROOT / "examples/case_mail_sources.live-check.json"

    def quiet_inbox(self) -> None:
        self.fake.messages = []
        now = self.clock()
        for n, days in enumerate((29, 31, 40, 50)):  # 50 days: outside newest - 14 days
            self.fake.add_message(n, (now - timedelta(days=days)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"))

    def live_sources(self) -> dict:
        return json.loads(self.LIVE_FILE.read_text(encoding="utf-8"))

    def test_live_check_sources_read_a_quiet_inbox_whose_newest_mail_is_29_days_old(self):
        self.quiet_inbox()
        captured = []
        original = self.fake._messages
        self.fake._messages = lambda url, query: (captured.append(dict(query)),
                                                  original(url, query))[1]
        ingestor = self.ingestor(self.live_sources())
        unit = self.units(ingestor.run())["mail.purchase-order.inbox"]
        self.assertEqual((unit["state"], unit["reasons"]), ("complete", []), unit)
        self.assertEqual((unit["counts"]["items_read"], unit["counts"]["new"]), (3, 3))
        self.assertEqual(unit["counts"]["initial_window_anchored"], 1)
        # The anchor is one newest-first metadata GET; the window read is
        # oldest-first from the newest message minus 14 days.
        self.assertEqual((captured[0]["$top"], captured[0]["$orderby"]),
                         ("1", "receivedDateTime desc"))
        self.assertNotIn("$filter", captured[0])
        newest = self.clock() - timedelta(days=29)
        self.assertEqual(captured[1]["$filter"], "receivedDateTime ge " + (
            newest - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(captured[1]["$orderby"], "receivedDateTime asc")
        self.assertEqual(self.cursors(ingestor)["mail.purchase-order.inbox"]
                         ["received_high_water"], newest.strftime("%Y-%m-%dT%H:%M:%SZ"))
        # Every request of the run was a GET of the closed grammar (the guard
        # refuses anything else before I/O); none of them reached a write path.
        self.assertTrue(all(c == "/v1.0/users" or c.startswith("/v1.0/users/")
                            for c in self.fake.calls))

    def test_clock_anchored_window_reads_nothing_from_the_same_quiet_inbox(self):
        # The failure the live verifier saw, kept as the contrast: same file with
        # the default anchor reads zero messages (honest, but proves no reading).
        self.quiet_inbox()
        document = self.live_sources()
        del document["sources"][0]["initial_window_anchor"]
        unit = self.units(self.ingestor(document).run())["mail.purchase-order.inbox"]
        self.assertEqual(unit["counts"]["items_read"], 0)
        self.assertNotIn("initial_window_anchored", unit["counts"])

    def test_empty_folder_anchors_at_now_and_reads_nothing(self):
        self.fake.messages = []
        unit = self.units(self.ingestor(self.live_sources()).run())[
            "mail.purchase-order.inbox"]
        self.assertEqual((unit["state"], unit["counts"]["items_read"],
                          unit["counts"]["initial_window_anchored"]), ("complete", 0, 0))

    def test_anchor_applies_to_the_first_window_only(self):
        self.quiet_inbox()
        ingestor = self.ingestor(self.live_sources())
        ingestor.run()
        self.fake.add_message(9, (self.clock() - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"))
        unit = self.units(ingestor.run())["mail.purchase-order.inbox"]
        self.assertEqual(unit["counts"]["new"], 1)
        self.assertNotIn("initial_window_anchored", unit["counts"])

    def test_anchor_is_validated_and_a_default_anchor_keeps_the_config_digest(self):
        def mail(**fields):
            return load_sources(sources_document(**{"mail.purchase-order": fields}),
                                ENV)[2][1]
        base = mail()
        self.assertNotIn("initial_window_anchor", base)
        self.assertEqual(mail(start_at=None, initial_window_anchor="now")["config_digest"],
                         mail(start_at=None)["config_digest"])
        self.assertEqual(mail(start_at=None, initial_window_anchor="newest_message")
                         ["initial_window_anchor"], "newest_message")
        for bad in ({"initial_window_anchor": "newest_message"},  # with start_at
                    {"start_at": None, "initial_window_anchor": "2026-09-01"},
                    {"start_at": None, "initial_window_anchor": True}):
            with self.assertRaises(IngestError) as caught:
                mail(**bad)
            self.assertEqual(caught.exception.code, "ingest_source_initial_window_anchor_invalid")


class SiteSourceConfigTests(unittest.TestCase):
    """A sharepoint_folder may name a site instead of a drive (directive e2d2f62b791d)."""

    @staticmethod
    def spec(environ=ENV, **fields):
        source = {"id": "sp.site", "kind": "sharepoint_folder", "path": "/Ontology UseCases",
                  **fields}
        return load_sources({"schema": "plx-ingest-sources/v1", "tenant": "plx",
                             "actor": {"principals": ["svc:ingest"]},
                             "sources": [source]}, environ)[2][0]

    def test_site_is_validated_and_needs_the_configured_site_host(self):
        spec = self.spec(site="AGENTS")
        self.assertEqual((spec["site"], spec["drive_id"], spec["not_configured"]),
                         ("AGENTS", None, []))
        self.assertEqual(self.spec({}, site="AGENTS")["not_configured"],
                         ["SHAREPOINT_SITE_ID"])
        for bad in ({"site": "AGENTS", "drive_id": DRIVE}, {"site": "AGENTS/x"},
                    {"site": "sites/AGENTS"}, {"site": ""}, {"site": 7}):
            with self.assertRaises(IngestError) as caught:
                self.spec(**bad)
            self.assertEqual(caught.exception.code, "ingest_source_site_invalid")

    def test_resolution_sets_the_drive_and_its_digest_once_per_site(self):
        from integrations.live_source_guard import SourceNotFound

        class Api:
            def __init__(self):
                self.urls = []

            def get_json(self, url, **_kw):
                self.urls.append(url)
                if "/drive" in url:
                    return {"id": "b!agents"}
                if ":/sites/GONE" in url:
                    raise SourceNotFound("graph GET returned HTTP 404")
                return {"id": SITE_ID}

        specs = [self.spec(site="AGENTS"), {**self.spec(site="AGENTS"), "id": "sp.site2"},
                 self.spec(site="GONE"), self.spec(site="OTHER")]
        before = specs[0]["config_digest"]
        api = Api()
        sites = ingestion.resolve_sites(specs, api, ENV, granted={"AGENTS", "GONE"})
        self.assertEqual(sites, {"AGENTS": "b!agents"})
        self.assertEqual((specs[0]["drive_id"], specs[1]["drive_id"]), ("b!agents", "b!agents"))
        self.assertNotEqual(specs[0]["config_digest"], before)
        self.assertEqual(specs[2]["site_unresolved"], ["not_visible", "site_unresolved:not_found"])
        self.assertEqual(specs[3]["site_unresolved"], ["not_visible", "site_unresolved:no_grant"])
        self.assertEqual(len(api.urls), 3)  # AGENTS (site + drive) once, GONE once, OTHER never


class CliAndSpecTests(IngestionTestCase):
    def test_validate_and_live_audience_example(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ingestion.main(["validate", "--sources",
                                   str(ROOT / "examples/ingestion_sources.live-check.json")],
                                  environ={"SHAREPOINT_DRIVE_ID": "b!x"})
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["valid"])
        audience = ingestion.load_live_audience(
            ROOT / "examples/ingestion_live_audience.live-check.json",
            {"SHAREPOINT_DRIVE_ID": "b!x"})
        self.assertEqual(audience.tenant, "plx")
        ingest = Actor("plx", frozenset({"svc:ingest"}), "restricted")
        self.assertTrue(audience.allows(ingest, "sharepoint:b!x:/AGENTS"))
        self.assertFalse(audience.allows(ingest, "sharepoint:b!x:/AGENTS/Contracts"))
        self.assertFalse(audience.allows(ingest, "sharepoint:b!x:/Finance"))

    def test_cli_run_without_credentials_reports_every_unit_incomplete(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ingestion.main([
                "run", "--state-dir", str(self.state), "--runs", "2",
                "--sources", str(ROOT / "examples/ingestion_sources.live-check.json"),
                "--live-audience", str(ROOT / "examples/ingestion_live_audience.live-check.json"),
                "--mirror-audience", str(ROOT / "examples/source_mirror_audience.example.json")],
                environ={})
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["status"]["summary"]["complete"], 0)
        reasons = {u["unit_id"]: u["reasons"] for u in report["status"]["units"]}
        self.assertEqual(reasons["portal.assembly-order"],
                         ["not_configured:PLX_WORKBENCH_API_URL"])
        self.assertEqual(reasons["sp.plxcustomers.root"], ["not_configured:SHAREPOINT_DRIVE_ID"])
        self.assertEqual(reasons["mail.purchase-order.inbox"], ["not_configured:reader"])
        self.assertEqual((report["cursor_regressions"], report["row_data_check"]["leaked"]),
                         (0, 0))

    def test_cli_status_prints_counts_only(self):
        self.ingestor().run()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ingestion.main(["status", "--state-dir", str(self.state)],
                                            environ={}), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["row_data_check"]["leaked"], 0)
        self.assertNotIn(SUBJECT, out.getvalue())


if __name__ == "__main__":
    unittest.main()
