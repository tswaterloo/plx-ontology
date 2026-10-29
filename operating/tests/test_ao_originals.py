"""Ricardo's AO packets read from SharePoint into the AO case (injected transport).

Evidence label: ``injected-transport`` for SharePoint (the real GET-only
``integrations.graph_read.SharePointReader`` over the in-process ``FakeSender`` of
the adapter tests) and the Portal rows (``FakeWorkbench``), ``isolated-fixture``
for the packet bytes and their page text: six synthetic PDFs marked
"SYNTHETIC TEST FIXTURE" and a stub extractor standing in for OCR. Nothing here
is live data; live check ``ao-originals`` is what the live verifier
runs.
"""

from __future__ import annotations

import copy
from hashlib import sha256
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from integrations.document_understanding import ExtractedDocument
from integrations.live_source_guard import LiveSourceAudience
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests.test_live_source_adapters import (CLIENT_SECRET, DRIVE, ENV, SP_HOST,
                                                          FakeSender, drive_item, j)
from integrations.tests.test_source_mirror import BASE, KEY, CATALOG, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from operating import source_reads
from operating.mcp_server import source_read
from operating import sharepoint_originals as originals_mod
from operating.case_runtime import CaseRuntime, build_live_readers
from operating.tests import test_ao_26005b_case as ao_tests
from operating.tests.ao_reference_investigation import load_paper_leads
from operating.tests.test_ao_26005b_case import (AO_CATALOG, AO_TABLES, EXPECTED,
                                                 EXPECTED_DAMAGE, GRANTED, OPS_QUALITY,
                                                 PAPER_LEADS_EXAMPLE, investigate)

ROOT = Path(__file__).resolve().parents[2]
# The expected identity of Ricardo's six packets (evaluation data, never product code).
ORIGINALS_EXPECTED = ROOT / "evals" / "dev" / "cases" / "ao_originals_expected.json"
FOLDER = "/AOs/2026 - AOs"
CODES = ("AO-26005B", "AO-26005C", "AO-26005E", "AO-26006C", "AO-26007D", "AO-26013C")
SP_BASE = f"/v1.0/drives/{DRIVE}"
DOWNLOAD_PATH = "/sites/QCQA/_layouts/15/download.aspx"
PAPER = load_paper_leads(PAPER_LEADS_EXAMPLE)
# Machine-read page text of the synthetic AO-26005B packet (stand-in for OCR).
PAGES = {1: "SYNTHETIC TEST FIXTURE Assembly Order MO AO LOT# AO-26005B OE OR1000151885 "
            "PO: REQUIRED DATE: SIZE* 1509 EA DATE 2026-01-05",
         3: "SYNTHETIC TEST FIXTURE Shop Floor Line clearance 1 QC Retain Filling 1502 "
            "DAMAGES P-0121 7 P-0156 3 P-01274 11",
         11: "SYNTHETIC TEST FIXTURE Finished Goods Release 2026-01-07 shipment released"}


def packet(code: str) -> bytes:
    return (b"%PDF-1.4\n% SYNTHETIC TEST FIXTURE " + code.encode() + b"\n").ljust(400, b"x")


def extractor(pages: dict[int, str]):
    def extract(content, mime_type, *, original_url, filename="", local_pdf_ocr=False):
        text, spans = "", []
        for number in range(1, 13):
            page = pages.get(number, "") if b"AO-26005B" in content else ""
            spans.append({"page": number, "start": len(text), "end": len(text) + len(page)})
            text += page
        return ExtractedDocument("application/pdf", "x", "extracted", "stub-ocr", text, (), (),
                                 (), tuple(spans), ocr_pages=tuple(range(1, 13)))
    return extract


class SharePointFolder:
    """The QCQA AO folder as the fake Graph serves it: six packets and two other items."""

    def __init__(self) -> None:
        self.items = {f"ITEM-{code}": drive_item(
            f"ITEM-{code}", f"{code}.pdf", folder=FOLDER, content=packet(code),
            etag=f"\"{{{code}}},1\"", parent_id="FOLDER-AO") for code in CODES}
        self.content = {item_id: packet(item_id[5:]) for item_id in self.items}
        self.extra = [drive_item("ITEM-README", "README.docx", folder=FOLDER, parent_id="FOLDER-AO",
                                 content=b"PK\x03\x04 not a packet"),
                      drive_item("ITEM-SUB", "Old", folder=FOLDER, parent_id="FOLDER-AO",
                                 is_folder=True)]
        self.sender = FakeSender({
            f"{SP_BASE}/root:{FOLDER}:": j(200, drive_item(
                "FOLDER-AO", "2026 - AOs", folder="/AOs", is_folder=True)),
            f"{SP_BASE}/items/FOLDER-AO/children": lambda url: j(200, {
                "value": [*self.items.values(), *self.extra]}),
            SP_HOST + DOWNLOAD_PATH: lambda url: (200, {}, self.content[
                parse_qs(urlsplit(url).query)["item"][0]]),
        })
        for item_id in self.items:
            self.sender.routes[f"{SP_BASE}/items/{item_id}"] = self._item(item_id)

    def _item(self, item_id: str):
        def route(url: str):
            item = dict(self.items[item_id])
            if "downloadUrl" in url:
                item["@microsoft.graph.downloadUrl"] = (
                    f"https://{SP_HOST}{DOWNLOAD_PATH}?item={item_id}&tempauth=DL-do-not-leak")
            return j(200, item)
        return route

    def expected(self, root: Path) -> Path:
        """An expected-identity file for the synthetic packets (same shape as the real one)."""
        data = json.loads(ORIGINALS_EXPECTED.read_text())
        data.update(drive_id=DRIVE, files=[
            {"name": f"{code}.pdf", "item_id": f"ITEM-{code}", "bytes": len(packet(code)),
             "sha256": sha256(packet(code)).hexdigest()} for code in CODES])
        path = root / "expected.json"
        path.write_text(json.dumps(data), "utf-8")
        return path

    def source_calls(self) -> list[tuple[str, str]]:
        return [(call[0], call[1]) for call in self.sender.calls
                if "login.microsoftonline.com" not in call[1]]


def live_audience(principals=("staff:quality",)) -> LiveSourceAudience:
    return LiveSourceAudience({f"sharepoint:{DRIVE}:/AOs/*": list(principals)}, tenant="plx",
                              default_classification="internal")


class AoOriginalsInvestigationTest(unittest.TestCase):
    """The AO-26005B case with its SharePoint packet attached and cited."""

    # The AO case harness (FakeWorkbench rows of the live trace, MCP calls).
    server = ao_tests.Ao26005bCaseTest.server
    call = ao_tests.Ao26005bCaseTest.call
    ok = ao_tests.Ao26005bCaseTest.ok
    caller = ao_tests.Ao26005bCaseTest.caller
    case_view = ao_tests.Ao26005bCaseTest.case_view

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.audience_path = self.root / "audience.json"
        self.audience_path.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx", "audiences": GRANTED}), "utf-8")
        self.fake = FakeWorkbench()
        self.fake.catalog = copy.deepcopy(CATALOG)
        self.fake.catalog.update(copy.deepcopy(AO_CATALOG))
        for name, rows in AO_TABLES.items():
            self.fake.tables[name] = [dict(row) for row in rows]
        self.clock = Clock()
        self.request_id = 100
        self.folder = SharePointFolder()
        self.expected = originals_mod.load_expected(self.folder.expected(self.root))
        self.pages = dict(PAGES)

    def runtime(self, live=True, audience=None) -> CaseRuntime:
        readers = (build_live_readers(audience or live_audience(), ENV,
                                      graph_sender=self.folder.sender) if live else None)
        runtime = CaseRuntime(self.root / "state", client=WorkbenchReadApiClient(
            BASE, KEY, fetch=self.fake), audience=self.audience_path, tenant="plx",
            mirror_options={"clock": self.clock, "retry_delay_seconds": 0},
            originals_options={"extractor": extractor(self.pages)}, live=readers)
        self.addCleanup(runtime.close)
        return runtime

    def originals(self) -> dict:
        return {"drive_id": DRIVE, "folder": FOLDER, "expected": self.expected,
                "name_pattern": self.expected["name_pattern"]}

    def open_case(self, server) -> str:
        return self.ok(server, "plx.case_open", {"question": "Ricardo's AO packets",
                                                 "roots": ["AO-26005B"]})["case_id"]

    # ------------------------------------------------------------------ (e) read path

    def test_every_ao_packet_is_attached_as_a_cited_original_with_its_identity(self):
        server = self.server(OPS_QUALITY, self.runtime())
        call = self.caller(server)
        case_id = self.open_case(server)
        result = originals_mod.attach_originals(call, case_id, drive_id=DRIVE, folder=FOLDER,
                                                name_pattern=self.expected["name_pattern"],
                                                expected=self.expected)
        self.assertEqual(result["status"], "listed")
        self.assertEqual(result["other_items"], 2)  # the .docx and the sub-folder
        self.assertEqual([item["code"] for item in result["originals"]], list(CODES))
        for item in result["originals"]:
            with self.subTest(code=item["code"]):
                self.assertEqual(item["status"], "attached")
                self.assertEqual(item["sha256"], sha256(packet(item["code"])).hexdigest())
                self.assertEqual(item["expected_match"],
                                 {"sha256": True, "item_id": True, "bytes": True})
                self.assertTrue(item["source_citation"].endswith("#sha256=" + item["sha256"]))
                self.assertEqual(len(item["page_citations"]), 12)
        # Each attach recorded a fact finding citing the original and the item version.
        view = self.case_view(server, case_id)
        self.assertEqual(len(view["findings"]["supported"]), 6)
        for finding in view["findings"]["supported"]:
            handles = [cite["handle"] for cite in finding["citations"]]
            self.assertTrue(handles[0].startswith("original://"))
            self.assertTrue(handles[1].startswith("sharepoint://"))
        # GET only; the pre-authenticated download URL never leaves the reader.
        self.assertTrue(all(method == "GET" for method, _ in self.folder.source_calls()))
        self.assertNotIn("tempauth", json.dumps(result))
        self.assertNotIn(CLIENT_SECRET, json.dumps(result))

    def test_a_packet_is_found_by_its_name_never_by_an_item_id(self):
        # The same packet under another item id still attaches; the identity difference
        # against the expectation is reported, not hidden.
        moved = self.folder.items.pop("ITEM-AO-26005B")
        moved = dict(moved, id="ITEM-NEW")
        self.folder.items["ITEM-NEW"] = moved
        self.folder.content["ITEM-NEW"] = packet("AO-26005B")
        self.folder.sender.routes[f"{SP_BASE}/items/ITEM-NEW"] = self.folder._item("ITEM-NEW")
        server = self.server(OPS_QUALITY, self.runtime())
        result = originals_mod.attach_original(
            self.caller(server), self.open_case(server), "AO-26005B", drive_id=DRIVE,
            folder=FOLDER, name_pattern=self.expected["name_pattern"], expected=self.expected)
        self.assertEqual((result["status"], result["item_id"]), ("attached", "ITEM-NEW"))
        self.assertEqual(result["expected_match"], {"sha256": True, "item_id": False,
                                                    "bytes": True})

    def test_unconfigured_ungranted_or_missing_packets_attach_nothing(self):
        def attach(runtime, code="AO-26005B"):
            server = self.server(OPS_QUALITY, runtime)
            caller = lambda name, arguments: _local(server, name, arguments)
            return originals_mod.attach_original(caller, self.open_case(server), code,
                                                 drive_id=DRIVE, folder=FOLDER,
                                                 name_pattern=self.expected["name_pattern"])
        self.assertEqual(attach(self.runtime(live=False))["status"], "source_not_configured")
        denied = self.runtime(audience=live_audience(("staff:vince",)))
        self.assertEqual(attach(denied)["status"], "unavailable")
        self.assertEqual(attach(self.runtime(), "AO-29999Z")["status"], "not_listed")
        downloads = [url for _method, url in self.folder.source_calls()
                     if urlsplit(url).hostname == SP_HOST]
        self.assertEqual(downloads, [])

    # ------------------------------------------------------------------ (b)-(d) conflicts

    def test_conflicts_cite_both_the_fm_rows_and_the_original_page(self):
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server), PAPER, self.originals())
        self.assertEqual(out["results"], EXPECTED)
        self.assertEqual(out["damaged_by_code"], EXPECTED_DAMAGE)
        self.assertTrue(out["checks"]["original_attached"])
        original = out["original"]
        page = {number: f"{original['citation']}#page={number}" for number in (1, 3, 11)}
        view = self.case_view(server, out["case_id"])
        gaps = {gap["missing"]: gap for gap in view["gaps"]}
        # (b) Box damage: FM's damage row against page 3, which shows the paper's figure.
        box = gaps["Which figure for shipper box damage is right: evidence conflict between FM "
                   "and the paper original"]
        self.assertEqual(box["state"], "unknown")
        self.assertEqual([cite["handle"] for cite in box["citations"]],
                         [out["cite"]["damage"][0], page[3]])
        self.assertIn("The cited FM row(s) record 20; page 3 of the attached original reads 11",
                      box["why"])
        self.assertIn("Neither source is taken as the fact", box["why"])
        self.assertEqual({cite["status"] for cite in box["citations"]}, {"resolved"})
        # (d) The production date: FM's date manufactured against the release page.
        made = gaps["Which figure for production and QC release date versus FM's date "
                    "manufactured is right: evidence conflict between FM and the paper "
                    "original"]
        self.assertEqual([cite["handle"] for cite in made["citations"]],
                         [out["cite"]["ao"], page[11]])
        self.assertIn("record 2026-01-05; page 11 of the attached original reads 2026-01-07",
                      made["why"])
        # A date is seen on the page, never claimed to sit in its labelled field.
        self.assertIn("as a date in its machine-read text (its field label was not matched)",
                      made["why"])
        self.assertNotIn("in its labelled field", made["why"])
        self.assertEqual([(entry["lead"], entry["paper_figure_in_page_text"],
                           entry["paper_figure_in_labelled_field"])
                          for entry in out["original_conflicts"]],
                         [("damage_box", True, True), ("production_date", True, False)])
        # (d) Lateness stays a conflict: FM rows plus the original's header and release pages.
        [late] = [gap for gap in view["gaps"] if "governing required date" in gap["missing"]]
        self.assertEqual([cite["handle"] for cite in late["citations"]],
                         [out["cite"]["ao"], out["cite"]["filling"], out["cite"]["order"],
                          page[1], page[11]])
        self.assertIn("holds customer PO PO12172025", late["why"])
        self.assertIn("the original's pages are cited", late["why"])
        self.assertIn("required-date and PO fields are blank", late["why"])
        self.assertFalse([finding for finding in view["findings"]["supported"]
                          if "late" in finding["statement"].lower()])
        # The packet is attached: no "not attached" gap; its figures await Quality.
        self.assertFalse([gap for gap in view["gaps"] if gap["state"] == "not_ingested"])
        [confirm] = [gap for gap in view["gaps"] if "to be confirmed by Quality" in gap["missing"]]
        self.assertEqual(confirm["citations"][0]["handle"], original["citation"])
        self.assertEqual(len(out["original_conflicts"]), 2)
        # (b) Paper damage per component; the mixed sums only reconcile the headline.
        damage = out["paper_damage"]
        self.assertEqual(damage["by_component"]["P-01274"],
                         {"paper": "11", "fm": "20", "agrees": False, "page": 3})
        self.assertNotIn("paper_sum_mixed_units", damage)
        self.assertNotIn("fm_sum_mixed_units", damage)

    def test_a_paper_figure_the_page_text_does_not_show_is_never_written(self):
        self.pages[3] = "SYNTHETIC TEST FIXTURE Shop Floor (handwriting not machine-read)"
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server), PAPER, self.originals())
        [box] = [gap for gap in self.case_view(server, out["case_id"])["gaps"]
                 if "shipper box damage is right" in gap["missing"]]
        self.assertIn("it was not located in its labelled field in the machine-read text, so "
                      "a person must read it there", box["why"])
        self.assertNotIn("handwritten", box["why"])
        self.assertNotRegex(box["missing"] + box["why"], r"\b11\b")
        self.assertEqual(out["original_conflicts"][0]["paper_figure_in_page_text"], False)

    def test_a_figure_elsewhere_on_the_page_is_not_the_labelled_figure(self):
        # An 11 that sits beside another code (P-0121 11) is not the box (P-01274) figure.
        self.pages[3] = ("SYNTHETIC TEST FIXTURE Filling 1502 DAMAGES P-0121 11 P-0156 3 "
                         "P-01274 20")
        server = self.server(OPS_QUALITY, self.runtime())
        out = investigate(self.caller(server), PAPER, self.originals())
        [box] = [gap for gap in self.case_view(server, out["case_id"])["gaps"]
                 if "shipper box damage is right" in gap["missing"]]
        self.assertIn("not located in its labelled field", box["why"])
        self.assertNotRegex(box["missing"] + box["why"], r"\b11\b")

    def test_without_a_reader_the_case_says_the_packet_was_not_attached_and_why(self):
        server = self.server(OPS_QUALITY, self.runtime(live=False))
        out = investigate(lambda name, arguments: _local(server, name, arguments), PAPER,
                          self.originals())
        self.assertEqual(out["original"]["status"], "source_not_configured")
        self.assertFalse(out["checks"]["original_attached"])
        [gap] = [gap for gap in self.case_view(server, out["case_id"])["gaps"]
                 if gap["state"] == "not_ingested"]
        self.assertIn("Reading it from SharePoint was attempted: source_not_configured", gap["why"])


class ShippedLiveAudienceTest(unittest.TestCase):
    """The shipped live audience example for Ricardo's packets: the originals folder an
    ``--sharepoint-original`` open attaches from (``operating.case_workspace_live``)."""

    EXAMPLE = ROOT / "examples" / "ao_originals_live_audience.example.json"

    def test_the_shipped_example_grants_quality_the_ao_folder_at_confidential(self):
        from ontology.core import Actor
        audience = LiveSourceAudience.from_file(self.EXAMPLE)
        expected = originals_mod.load_expected(ORIGINALS_EXPECTED)
        drive, folder = expected["drive_id"], expected["folder"]
        keys = [f"sharepoint:{drive}:{folder}", f"sharepoint:{drive}:{folder}/"]
        quality = Actor("plx", frozenset({"staff:quality"}), "confidential")
        self.assertTrue(audience.allows_all(quality, keys))
        for item in expected["files"]:
            self.assertTrue(audience.allows_all(
                quality, [f"sharepoint:{drive}:{folder}/{item['name']}"]))
        self.assertEqual(audience.classification_for(keys[0]), "confidential")
        # Nothing else: another folder of the drive, another drive, another principal.
        self.assertFalse(audience.allows_all(quality, [f"sharepoint:{drive}:/AOs/2025 - AOs"]))
        # The folder is anchored: a sibling whose name only starts the same is not granted.
        self.assertFalse(audience.allows_all(quality, [f"sharepoint:{drive}:{folder} archive"]))
        self.assertFalse(audience.allows_all(
            quality, [f"sharepoint:{drive}:{folder} archive/AO-26005B.pdf"]))
        self.assertFalse(audience.allows_all(quality, [f"sharepoint:{DRIVE}:{folder}"]))
        diagnose = lambda actor: originals_mod.diagnose_refusal(audience, actor, drive, folder,
                                                                "AO-26005C.pdf")
        self.assertEqual(diagnose(quality), "source_error")
        self.assertEqual(diagnose(Actor("plx", frozenset({"staff:quality"}))), "clearance")
        self.assertEqual(diagnose(Actor("plx", frozenset({"staff:operations"}),
                                        "confidential")), "no_grant")
        self.assertEqual(diagnose(Actor("other", frozenset({"staff:quality"}),
                                        "confidential")), "no_grant")


_IDS = itertools.count(10_000)


def _local(server, name, arguments):
    """``local_client`` semantics: a refused tool raises RuntimeError naming it; the
    direct mirror and live reads are routed as ``local_client`` routes them."""
    if name in source_reads.READS:
        return source_read(server, name, arguments)
    response = server.handle({"jsonrpc": "2.0", "id": next(_IDS), "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}},
                             host_code=True)
    result = response.get("result") or {}
    if "error" in response or result.get("isError"):
        raise RuntimeError(f"{name} refused: {(result.get('content') or [{}])[0].get('text')}")
    return result["structuredContent"]


class LiveCheckTest(unittest.TestCase):
    """``python3 -m operating.sharepoint_originals check``: the live verifier's command."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = SharePointFolder()
        self.expected = self.folder.expected(self.root)
        self.env = dict(ENV, PLX_QCQA_DRIVE_ID=DRIVE)

    def run_check(self) -> tuple[int, dict, str]:
        output = io.StringIO()
        with patch("sys.stdout", output):
            code = originals_mod.main(["check", "--expected", str(self.expected)],
                                      environ=self.env, sender=self.folder.sender)
        return code, json.loads(output.getvalue()), output.getvalue()

    def test_reads_every_packet_and_prints_counts_and_digests_only(self):
        code, report, text = self.run_check()
        self.assertEqual(code, 0, report)
        self.assertEqual((report["matching_files"], report["read"], report["sha256_match_expected"],
                          report["item_id_match_expected"], report["missing_expected"]),
                         (6, 6, 6, 6, []))
        self.assertEqual(report["files"][0], {
            "name": "AO-26005B.pdf", "bytes": 400, "content_status": "read", "pdf": True,
            "sha256": sha256(packet("AO-26005B")).hexdigest(),
            "sha256_matches_expected": True, "item_id_matches_expected": True})
        self.assertEqual(report["evidence_label"], "live-read")
        for leaked in ("tempauth", "ricardo@", CLIENT_SECRET, "webUrl", "SYNTHETIC"):
            self.assertNotIn(leaked, text)
        methods = {(method, urlsplit(url).hostname) for method, url, _h, _b
                   in self.folder.sender.calls}
        self.assertEqual({method for method, host in methods if host != "login.microsoftonline.com"},
                         {"GET"})
        # The spec's expectations hold for this (injected-transport) report.
        spec = json.loads((ROOT / "devloop" / "live-checks" / "ao-originals.json").read_text())
        self.assertEqual(spec["command"][:4],
                         ["python3", "-m", "operating.sharepoint_originals", "check"])
        self.assertEqual((ROOT / spec["command"][5]).resolve(), ORIGINALS_EXPECTED)
        self.assertEqual(spec["expect"]["exit_code"], code)
        from devloop import live_verify
        self.assertEqual(live_verify.check_expectations(spec, code, report), [])

    def test_the_file_rule_comes_from_the_expected_file_never_from_the_code(self):
        # Another folder rule (here: the folder's .docx) reads only what that rule names.
        readme = b"PK\x03\x04 not a packet"
        self.folder.items["ITEM-README"] = self.folder.extra.pop(0)
        self.folder.content["ITEM-README"] = readme
        self.folder.sender.routes[f"{SP_BASE}/items/ITEM-README"] = self.folder._item("ITEM-README")
        data = json.loads(self.expected.read_text())
        data.update(name_pattern=r"^(README)\.docx$", files=[
            {"name": "README.docx", "item_id": "ITEM-README", "bytes": len(readme),
             "sha256": sha256(readme).hexdigest()}])
        self.expected.write_text(json.dumps(data))
        code, report, _text = self.run_check()
        self.assertEqual((code, report["matching_files"], report["sha256_match_expected"]),
                         (0, 1, 1))
        self.assertEqual([item["name"] for item in report["files"]], ["README.docx"])

    def test_a_data_file_naming_a_credential_variable_is_refused_before_any_request(self):
        data = json.loads(self.expected.read_text())
        data["drive_env"] = "MICROSOFT_GRAPH_CLIENT_SECRET"
        self.expected.write_text(json.dumps(data))
        errors = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", errors):
            code = originals_mod.main(["check", "--expected", str(self.expected)],
                                      environ=dict(self.env, MICROSOFT_GRAPH_CLIENT_SECRET=
                                                   CLIENT_SECRET), sender=self.folder.sender)
        self.assertEqual(code, 1)
        self.assertEqual(self.folder.sender.calls, [])
        self.assertIn("drive_env_refused", errors.getvalue())
        self.assertNotIn(CLIENT_SECRET, errors.getvalue())

    def test_a_changed_packet_fails_the_check(self):
        self.folder.content["ITEM-AO-26013C"] = packet("AO-26013C").replace(b"x", b"y")
        self.folder.items["ITEM-AO-26013C"]["file"]["hashes"] = {}
        code, report, _text = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(report["sha256_match_expected"], 5)
        self.assertFalse(report["ok"])

    def test_a_missing_packet_or_missing_credentials_fail_the_check(self):
        del self.folder.items["ITEM-AO-26007D"]
        code, report, _text = self.run_check()
        self.assertEqual((code, report["missing_expected"]), (1, ["AO-26007D.pdf"]))
        self.env = {"PLX_QCQA_DRIVE_ID": DRIVE}
        code, report, _text = self.run_check()
        self.assertEqual((code, report["configured"], report["error"]),
                         (1, False, "source_not_configured"))

    def test_a_failed_packet_download_names_the_packet_status_and_code_class(self):
        """The live check once died with an opaque 26-character SourceUnavailable
        ("download returned HTTP 4xx"); now the packet, status and code class show."""
        planted = "PLANTED-BODY-do-not-leak " + CLIENT_SECRET
        download = self.folder.sender.routes[SP_HOST + DOWNLOAD_PATH]
        self.folder.sender.routes[SP_HOST + DOWNLOAD_PATH] = lambda url: (
            j(403, {"error": {"code": "accessDenied", "message": planted}})
            if "ITEM-AO-26006C" in url else download(url))
        code, report, text = self.run_check()
        self.assertEqual((code, report["ok"], report["read"], report["read_errors"]),
                         (1, False, 5, 1))
        [failed] = [item for item in report["files"] if item["content_status"] == "error"]
        self.assertEqual(failed, {
            "name": "AO-26006C.pdf", "bytes": None, "content_status": "error", "sha256": None,
            "pdf": False, "sha256_matches_expected": False, "item_id_matches_expected": True,
            "error_class": "SourceUnavailable", "http_status": 403,
            "error_code": "access_denied", "detail": "http_403"})
        for leaked in ("PLANTED", CLIENT_SECRET, "tempauth", "DL-do-not-leak"):
            self.assertNotIn(leaked, text)
        spec = json.loads((ROOT / "devloop" / "live-checks" / "ao-originals.json").read_text())
        from devloop import live_verify
        recorded = live_verify._recorded(spec, report)
        self.assertEqual(recorded["files[*].http_status"].count(403), 1)
        self.assertIn("access_denied", recorded["files[*].error_code"])
        self.assertIn("AO-26006C.pdf", recorded["files[*].name"])

    def test_a_failure_before_the_packets_prints_status_and_code_class_on_stderr(self):
        planted = "PLANTED-BODY-do-not-leak " + CLIENT_SECRET
        self.folder.sender.routes[f"{SP_BASE}/root:{FOLDER}:"] = j(
            401, {"error": {"code": "InvalidAuthenticationToken", "message": planted}})
        errors = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", errors):
            code = originals_mod.main(["check", "--expected", str(self.expected)],
                                      environ=self.env, sender=self.folder.sender)
        self.assertEqual(code, 1)
        line = json.loads(errors.getvalue())
        self.assertEqual((line["error"], line["http_status"], line["error_code"],
                          line["detail"]),
                         ("SourceUnavailable", 401, "invalid_authentication_token", "http_401"))
        self.assertNotIn("PLANTED", errors.getvalue())
        self.assertNotIn(CLIENT_SECRET, errors.getvalue())
        from devloop import live_verify
        published = live_verify._stderr_error(errors.getvalue())
        self.assertEqual((published["error"], published["http_status"],
                          published["error_code"]),
                         ("SourceUnavailable", 401, "invalid_authentication_token"))

    def test_the_shipped_expectation_names_ricardos_six_packets(self):
        expected = originals_mod.load_expected(ORIGINALS_EXPECTED)
        self.assertEqual(sorted(item["name"] for item in expected["files"]),
                         [f"{code}.pdf" for code in CODES])
        self.assertEqual(expected["folder"], FOLDER)


if __name__ == "__main__":
    unittest.main()
