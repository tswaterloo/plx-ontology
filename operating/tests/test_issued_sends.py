"""Issued PDF -> sent message -> reply -> served PO status (``operating/issued_sends.py``).

Evidence label: ``injected-transport``. Sent messages and a supplier reply are captured
from a fake Graph mailbox (``integrations.capture_mail``), the issued renders and their
DocumentCategory from a fake SharePoint library (``integrations.capture_sharepoint``),
both through the real GET-only readers; PO rows come through the real
``WorkbenchReadApiClient`` and source mirror against the mirror tests' ``FakeWorkbench``.
Names follow the Portal (plx-customer-portal@4af6957): the PDF is ``{poNumber}.pdf``
(purchase-order-document.ts:94), each render is stored as ``{epochMs}-{uuid}-{fileName}``
with ``#``/``&`` replaced by ``_`` (issued-records.ts@595243f:185-196), a legacy send or a
PDF issue (``POST .../pdf/issue``; the ``GET`` preview files nothing,
purchase-order-pdf.ts@595243f:36-98) keeps an ``ISSUED_PO`` render, the hub a ``SIGNED_PO`` one
(purchase-order-send.ts, purchase-order-sent.ts:165), and a non-production send carries
``[STAGING → <recipients>]`` (email-redirect.ts:44).
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import unittest

from devloop import live_verify as lv
from integrations import graph_read
from integrations.capture_mail import CaptureScope, MailCapture, MailCaptureStore
from integrations.capture_sharepoint import CaptureScope as DriveScope
from integrations.graph_read import GraphMailReader, quick_xor_hash
from integrations.http import SourceHttpError
from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience
from integrations.source_mirror import AUDIENCE_SCHEMA
from integrations.tests import test_capture_mail as tm
from integrations.tests import test_capture_sharepoint as tsp
from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.types import Actor
from operating import issued_sends as isd
from operating.case_runtime import CaseRuntime
from operating.tests.test_employee_permissions import columns_drive

READER = Actor("plx", frozenset({"staff:orders", "staff:quality", "staff:purchasing"}),
               "restricted")
NO_FILES = Actor("plx", frozenset({"staff:orders", "staff:purchasing"}), "restricted")
NO_MAIL = Actor("plx", frozenset({"staff:quality", "staff:purchasing"}), "restricted")
NO_ROWS = Actor("plx", frozenset({"staff:orders", "staff:quality"}), "restricted")
VENDOR = "buyer@vendor-co.example"
FIRST, REISSUE, OLD, NEW, HASHED, ISSUED = (
    b"%PDF-1.4 PO100500 render one\n" * 30, b"%PDF-1.4 PO100500 render two\n" * 30,
    b"%PDF-1.4 PO100501 render one\n" * 30, b"%PDF-1.4 PO100501 render two\n" * 30,
    b"%PDF-1.4 PO#7702 render\n" * 30, b"%PDF-1.4 PO100503 download\n" * 30)
PO_CATALOG = {"columns": ["id", "po_number", "status", "updated_at"], "key": ["id"],
              "domain": "purchasing", "since": {"column": "updated_at", "mode": "timestamp-utc"}}
PO_ROWS = [{"id": f"po-{n}", "po_number": number, "status": status,
            "updated_at": "2026-10-04T08:00:00Z"}
           for n, (number, status) in enumerate((
               ("PO100500", "ORDERED"), ("PO100501", "SUBMITTED"), ("PO#7702", "ORDERED"),
               ("PO100502", "ORDERED"), ("PO100503", "DRAFT")))]
SECRETS = ("PO1005", "7702", "vendor-co", "@", "graph-mail://", "sharepoint://", "api://",
           ".pdf", "STAGING")


def render_name(ms: int, uuid_tail: str, name: str) -> str:
    return f"{ms}-0a1b2c3d-0000-4000-8000-{uuid_tail}-{name}"


def audience() -> LiveSourceAudience:
    """One reader audience: mailbox A (staff:orders), the Quality folder whose files
    are shared with Ricardo's identity (staff:quality)."""
    folder = tsp.QUALITY_FOLDER
    return LiveSourceAudience(
        {f"mail:{tm.UPN_A}": ["staff:orders"],
         f"sharepoint:{tsp.DRIVE}:{folder}": ["staff:quality"],
         f"sharepoint:{tsp.DRIVE}:{folder}/*": ["staff:quality"]},
        tenant="plx", classifications={"mail:*": "restricted"},
        identities={"staff:quality": [tsp.RICARDO]},
        source_acl_required=[f"sharepoint:{tsp.DRIVE}:*"])


class IssuedSendsTests(tsp.CaptureCase):
    def setUp(self) -> None:
        super().setUp()
        graph = tm.FakeGraph()
        graph.folders[tm.BOX_A]["sentitems"] = tm.FOLDER_A   # a Sent Items-only capture
        self.send(graph, "MSG-REAL", "Purchase Order PO100500", REISSUE, "PO100500.pdf")
        self.send(graph, "MSG-OLD", "Purchase Order PO100501", OLD, "PO100501.pdf")
        # The display name keeps '#'; the stored render replaced it (issued-records.ts:177).
        self.send(graph, "MSG-HASH", "Purchase Order PO#7702", HASHED, "PO#7702.pdf")
        self.send(graph, "MSG-TEST", f"[STAGING → {VENDOR}] Purchase Order PO100503", ISSUED,
                  "PO100503.pdf", to="test-inbox@example-plx.com")
        # The supplier answers the real send; an earlier message in the other send's
        # conversation is no reply to it.
        self.inbound(graph, "MSG-REPLY", "RE: Purchase Order PO100500", "CONV-MSG-REAL",
                     "2026-09-21T09:00:00Z")
        self.inbound(graph, "MSG-EARLIER", "Quote before PO100501", "CONV-MSG-OLD",
                     "2026-09-19T09:00:00Z")
        self.mail = MailCaptureStore(self.state, clock=tm.Clock())
        self.addCleanup(self.mail.close)
        api, _ = graph_read.build_graph(tm.ENV, sender=graph, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))
        scope = CaptureScope([{"address": tm.UPN_A, "folders": ["sentitems"]}], window_days=90,
                             internal_domains=["example-plx.com"], allowlist=tm.SYNTHETIC)
        run = MailCapture(self.mail, GraphMailReader(api, tm.capture_audience()), tm.CAPTURE,
                          sleep=lambda s: None).run(scope)
        assert run["complete"], run
        self.drive = columns_drive()
        self.drive.folder("01QUALITY", "Quality")
        self.drive.folder("01AOS", "AOs", parent="01QUALITY")
        self.drive.categories = {}
        # A re-issue: two renders of PO100500 (hub) and of PO100501 (issued records), the
        # newest last; PO#7702 stored under its safe name; PO100503 downloaded only. The
        # newer hub render carries the column a finalize job writes since TASK-3105
        # (email-hub/attachments.ts@f3e3a53:196-203: documentCategoryColumn(SIGNED_PO) is
        # OTHER), the older one the column written before.
        for iid, name, content, category in (
                ("01R1", render_name(1759600000000, "000000000001", "PO100500.pdf"), FIRST,
                 "SIGNED_PO"),
                ("01R2", render_name(1759700000000, "000000000002", "PO100500.pdf"), REISSUE,
                 "OTHER"),
                ("01R3", render_name(1759600000000, "000000000003", "PO100501.pdf"), OLD,
                 "ISSUED_PO"),
                ("01R4", render_name(1759800000000, "000000000004", "PO100501.pdf"), NEW,
                 "ISSUED_PO"),
                ("01R5", render_name(1759600000000, "000000000005", "PO_7702.pdf"), HASHED,
                 "ISSUED_PO"),
                ("01R6", render_name(1759600000000, "000000000006", "PO100503.pdf"), ISSUED,
                 "ISSUED_PO")):
            self.drive.file(iid, name, "01AOS", content)
            self.drive.categories[iid] = category
        run = self.capture().run(DriveScope(
            [{"id": "quality", "drive_id": tsp.DRIVE, "folders": [tsp.QUALITY_FOLDER],
              "max_depth": 3, "max_folders": 50}], list_item_columns=["DocumentCategory"]))
        assert run["complete"], run
        self.mirror_audience = Path(self.tmp.name) / "mirror-audience.json"
        self.mirror_audience.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx",
            "audiences": {isd.PO_RELATION: ["staff:purchasing"]}}), encoding="utf-8")
        self.fake = FakeWorkbench()
        self.fake.catalog[isd.PO_RELATION] = dict(PO_CATALOG)
        self.fake.tables[isd.PO_RELATION] = [dict(r) for r in PO_ROWS]
        self.runtime = CaseRuntime(self.state, client=WorkbenchReadApiClient(
            BASE, KEY, fetch=self.fake), audience=self.mirror_audience, tenant="plx",
            mirror_options={"clock": Clock(), "retry_delay_seconds": 0})
        self.addCleanup(self.runtime.close)

    @staticmethod
    def send(graph, mid, subject, content, name, *, to=VENDOR):
        graph.put(tm.BOX_A, mid, "CK-" + mid, subject=subject, body="Please find our PO.",
                  attachments=[tm.file_attachment("ATT-" + mid, content, name)])
        graph.messages[tm.BOX_A][mid].update(
            {"from": {"emailAddress": {"address": tm.UPN_A}},
             "toRecipients": [{"emailAddress": {"address": to}}]})

    @staticmethod
    def inbound(graph, mid, subject, conversation, received):
        graph.put(tm.BOX_A, mid, "CK-" + mid, subject=subject, body="Confirmed.",
                  received=received)
        graph.messages[tm.BOX_A][mid].update(
            {"from": {"emailAddress": {"address": VENDOR}}, "conversationId": conversation})

    def report(self, actor=READER):
        return isd.issued_sends(actor, mail=self.mail, sharepoint=self.store,
                                audience=audience(), mirror=self.runtime.mirror,
                                internal_domains=["example-plx.com"])

    def test_sent_bytes_link_render_reply_and_status_and_a_po_without_a_send_is_a_gap(self):
        report = self.report()
        counts = report["counts"]
        self.assertEqual(counts["sends_by_kind"],
                         {"vendor": 3, "test_redirect": 1, "internal_only": 0})
        self.assertEqual(counts["byte_matches"], {"sha256": 3, "quickXorHash": 0})
        self.assertEqual((counts["renders"], counts["render_names"],
                          counts["renders_superseded"]), (6, 4, 2))
        self.assertEqual(counts["renders_by_origin"],
                         {"issued_record": 4, "hub_or_upload": 2, "other": 0, "unread": 0})
        chains = {c["po_number"]: c for c in report["chains"]}
        self.assertEqual(sorted(chains), ["PO#7702", "PO100500", "PO100501"])
        # The real send carried the newest render; the supplier replied; its PO reads ORDERED.
        real = chains["PO100500"]
        self.assertEqual((real["sent_render"], real["outcome"], real["po"]["status"],
                          real["render"]["origin"], real["reply"]),
                         ("newest", "ordered_or_later", "ORDERED", "hub_or_upload",
                          "reply_captured"))
        self.assertEqual(real["render"]["citation"], tsp.version_handle(
            tsp.DRIVE, "01R2", '"{01R2},1"'))
        self.assertEqual(len(real["replies"]), 1)
        self.assertTrue(real["replies"][0].startswith("graph-mail://"))
        self.assertNotEqual(real["replies"][0], real["send"]["citation"])
        # Re-issued after the send: what left PLX is the superseded render; an earlier
        # message in its conversation is no reply.
        old = chains["PO100501"]
        self.assertEqual((old["sent_render"], old["outcome"], old["reply"], old["replies"]),
                         ("superseded", "before_ordered", "no_reply_captured", []))
        self.assertEqual(old["newer_renders"],
                         [tsp.version_handle(tsp.DRIVE, "01R4", '"{01R4},1"')])
        # Stored as PO_7702.pdf (5bc5c63 safeName): still linked, to the served PO#7702.
        self.assertEqual((chains["PO#7702"]["outcome"], chains["PO#7702"]["po"]["status"]),
                         ("ordered_or_later", "ORDERED"))
        self.assertEqual(counts["chain_replies"],
                         {"reply_captured": 1, "no_reply_captured": 2})
        # No send seen: an explicit gap with what was and was not seen, never "not sent".
        gaps = {g["po_number"]: g for g in report["gaps"]}
        self.assertEqual(sorted(gaps), ["PO100502", "PO100503"])
        self.assertEqual((gaps["PO100502"]["kind"], gaps["PO100502"]["reasons"]),
                         ("send_not_captured", ["no_render_captured", "status_ordered_or_later"]))
        # Downloaded (ISSUED_PO) and sent only to the staging test inbox: still a gap,
        # and the redirect holding the render's exact bytes links nothing.
        self.assertEqual((gaps["PO100503"]["kind"], gaps["PO100503"]["reasons"]),
                         ("send_not_captured", ["issued_record_only", "test_redirect_only"]))
        self.assertEqual(gaps["PO100503"]["renders"],
                         [tsp.version_handle(tsp.DRIVE, "01R6", '"{01R6},1"')])
        self.assertEqual(counts["po_gaps"], {"send_not_captured": 2, "send_not_linked": 0})
        self.assertNotIn("not_sent", json.dumps(report))
        self.assertEqual(report["sources"]["po"]["status"], "read")
        self.assertTrue(report["sources"]["sharepoint"]["complete"])

    def test_each_source_is_trimmed_to_the_reader_in_chains_gaps_and_counts(self):
        full = self.report()
        row_citations = [c["po"]["citation"] for c in full["chains"]]
        self.assertTrue(all(row_citations))
        no_files = self.report(NO_FILES)
        counts = no_files["counts"]
        self.assertEqual((counts["renders"], counts["chains"], counts["unmatched"]), (0, 0, 3))
        # Every served PO is a gap; the ones a vendor send names are send_not_linked.
        self.assertEqual(counts["po_gaps"], {"send_not_captured": 2, "send_not_linked": 3})
        # No file grant: the renders it cannot see make the source incomplete for it.
        self.assertFalse(no_files["sources"]["sharepoint"]["complete"])
        gap = next(g for g in no_files["gaps"] if g["po_number"] == "PO100500")
        self.assertEqual(gap["reasons"], ["renders_incomplete", "no_render_captured",
                                          "vendor_send_unlinked", "status_ordered_or_later"])
        self.assertNotIn("sharepoint://", json.dumps(no_files))
        no_mail = self.report(NO_MAIL)
        self.assertEqual((no_mail["counts"]["messages_scanned"], no_mail["counts"]["sends"],
                          no_mail["counts"]["renders"]), (0, 0, 6))
        self.assertNotIn("graph-mail://", json.dumps(no_mail))
        no_rows = self.report(NO_ROWS)
        self.assertEqual(no_rows["counts"]["chain_outcomes"]["po_relation_unread"], 3)
        printed = json.dumps(no_rows)
        self.assertNotIn("ORDERED", printed)
        for citation in row_citations:
            self.assertNotIn(citation, printed)

    def test_a_file_withheld_from_the_reader_makes_every_gap_renders_incomplete(self):
        self.drive.file("01R7", render_name(1759900000000, "000000000007", "PO100502.pdf"),
                        "01AOS", b"%PDF-1.4 PO100502 render\n" * 30,
                        acl=("someone-else@example-plx.com",))
        self.drive.categories["01R7"] = "ISSUED_PO"
        run = self.capture().run(DriveScope(
            [{"id": "quality", "drive_id": tsp.DRIVE, "folders": [tsp.QUALITY_FOLDER],
              "max_depth": 3, "max_folders": 50}], list_item_columns=["DocumentCategory"]))
        self.assertTrue(run["complete"], run)
        report = self.report()
        self.assertFalse(report["sources"]["sharepoint"]["complete"])
        self.assertEqual(report["counts"]["renders"], 6)   # the withheld render stays unseen
        gap = next(g for g in report["gaps"] if g["po_number"] == "PO100502")
        self.assertEqual(gap["reasons"], ["renders_incomplete", "no_render_captured",
                                          "status_ordered_or_later"])
        self.assertNotIn(tsp.version_handle(tsp.DRIVE, "01R7", '"{01R7},1"'),
                         json.dumps(report))

    def cli(self, fetch, *extra, graph_sender=None, grants=None):
        live = Path(self.tmp.name) / "live-audience.json"
        live.write_text(json.dumps({
            "schema": "plx-live-source-audience/v1", "tenant": "plx",
            "audiences": {f"mail:{tm.UPN_A}": ["staff:orders"],
                          f"sharepoint:{tsp.DRIVE}:{tsp.QUALITY_FOLDER}": ["staff:quality"],
                          f"sharepoint:{tsp.DRIVE}:{tsp.QUALITY_FOLDER}/*": ["staff:quality"],
                          **(grants or {})},
            "classifications": {"mail:*": "restricted"},
            "identities": {"staff:quality": [tsp.RICARDO]},
            "source_acl_required": [f"sharepoint:{tsp.DRIVE}:*"]}), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = isd.main(["scan", "--state-dir", str(self.state), "--live-audience",
                             str(live), "--mirror-audience", str(self.mirror_audience),
                             "--principal", "staff:orders", "--principal", "staff:quality",
                             "--principal", "staff:purchasing",
                             "--internal-domain", "example-plx.com", *extra],
                            environ={**(tm.ENV if graph_sender else {}),
                                     "PLX_WORKBENCH_API_URL": BASE,
                                     "PLX_WORKBENCH_API_KEY": KEY},
                            workbench_fetch=fetch, graph_sender=graph_sender)
        self.assertEqual(code, 0)
        printed = out.getvalue()
        for secret in (*SECRETS, KEY):
            self.assertNotIn(secret, printed)
        return json.loads(printed)

    def test_cli_prints_counts_only_and_meets_the_live_spec(self):
        spec = json.loads((Path(lv.ROOT) / lv.SPECS_DIR_NAME / "case.po_sent_chain.json")
                          .read_text(encoding="utf-8"))
        lv.validate_spec(spec, stem="case.po_sent_chain")
        self.assertIn("operating.issued_sends", lv.ALLOWED_MODULES)
        result = self.cli(self.fake)
        self.assertEqual(result["evidence_label"], "injected-transport")
        self.assertEqual(result["counts"]["chain_outcomes"]["ordered_or_later"], 2)
        self.assertEqual(result["counts"]["sends_by_kind"]["test_redirect"], 1)
        self.assertEqual(result["counts"]["po_gaps"]["send_not_captured"], 2)
        self.assertEqual(result["blocked_upstream"], [])
        # The label is the only expectation a fixture run must not meet.
        self.assertEqual(spec["expect"]["json_equals"].pop("evidence_label"), "live-read")
        self.assertEqual(lv.check_expectations(spec, 0, result), [])
        recorded = lv._recorded(spec, result)
        self.assertEqual(recorded["counts.po_gaps"],
                         {"send_not_captured": 2, "send_not_linked": 0})
        self.assertEqual(recorded["counts.chain_replies"]["reply_captured"], 1)

    def test_cli_names_a_failed_portal_read_as_blocked_upstream(self):
        spec = json.loads((Path(lv.ROOT) / lv.SPECS_DIR_NAME / "case.po_sent_chain.json")
                          .read_text(encoding="utf-8"))

        def down(url, headers, *, max_bytes):
            raise SourceHttpError("read-only source GET failed: HTTPError", status=503,
                                  error_code="database-login-failed")

        result = self.cli(down)
        self.assertEqual(result["sources"]["po"]["status"], "unavailable")
        self.assertEqual(result["blocked_upstream"], [{
            "part": "po_source", "source": "portal-workbench-api", "http_status": 503,
            "error_code": "database-login-failed"}])
        self.assertEqual(result["counts"]["chain_outcomes"]["po_relation_unread"], 3)
        spec["expect_blocked"]["json_equals"].pop("evidence_label")
        self.assertEqual(lv.check_expectations(spec, 0, result, spec["expect_blocked"]), [])

    def test_cli_names_mail_graph_refused_on_every_mailbox_as_blocked_upstream(self):
        # Live 2026-10-17 on (case.po_sent_chain daily): the capture's directory lookup
        # answered 403 on every mailbox (an app without User.Read.All) and the check
        # failed on counts.messages_scanned 0. The refused call and its grant are now a
        # blocked part; the rest (renders, PO rows) is what expect_blocked measures.
        spec = json.loads((Path(lv.ROOT) / lv.SPECS_DIR_NAME / "case.po_sent_chain.json")
                          .read_text(encoding="utf-8"))
        scope = Path(self.tmp.name) / "mail-scope.json"
        scope.write_text(json.dumps({
            "schema": "plx-mail-capture-scope/v1", "window_days": 90,
            "internal_domains": ["example-plx.com"],
            "mailboxes": [{"address": "purchase-order@petrasoap.com", "folders": ["sentitems"]},
                          {"address": "vince@petrasoap.com", "folders": ["inbox"]}]}),
            encoding="utf-8")
        graph = tm.FakeGraph()
        graph.directory_forbidden = {"*"}
        result = self.cli(self.fake, "--mail-capture-scope", str(scope), graph_sender=graph,
                          grants={f"mail:{box}@petrasoap.com": ["svc:mail-capture"]
                                  for box in ("purchase-order", "vince")})
        self.assertEqual(result["capture_runs"]["mail"]["reasons"], "forbidden")
        self.assertEqual(result["blocked_upstream"], [{
            "part": "mail.mailbox_lookup", "source": "microsoft-graph", "http_status": 403,
            "error_code": "missing_permission", "permission": "User.Read.All",
            "cause": "host_misconfiguration"}])
        self.assertEqual(result["source_read"]["unblocked_unread"], [])
        self.assertGreaterEqual(result["source_read"]["inputs_read"], 2)
        spec["expect_blocked"]["json_equals"].pop("evidence_label")
        self.assertEqual(lv.check_expectations(spec, 0, result, spec["expect_blocked"]), [])
        self.assertEqual(lv._recorded(spec, result)["blocked_upstream[*].permission"],
                         ["User.Read.All"])

    def test_live_spec_declares_the_bc_credential_its_readers_read(self):
        """The real command under the verifier's env guard with a BC secret set: it reads
        BC_CLIENT_SECRET while building its live readers, so the spec must declare it
        (otherwise the check is an ``undeclared_env`` error). Graph stays unset, so the
        command stops at the mail capture before any request."""
        spec = json.loads((Path(lv.ROOT) / lv.SPECS_DIR_NAME / "case.po_sent_chain.json")
                          .read_text(encoding="utf-8"))
        spec.pop("requires_env")
        result = lv.run_spec(spec, environ={"PATH": "/usr/bin:/bin",
                                            "BC_CLIENT_SECRET": "s" * 12})
        self.assertNotIn("undeclared_env", result.get("detail", {}))
        self.assertEqual((result["result"], result["exit_code"]), ("fail", 2))


class NameRulesTests(unittest.TestCase):
    def test_a_quick_xor_hash_matches_a_render_whose_bytes_were_not_captured(self):
        sends = {"status": "read", "complete": True, "messages_scanned": 1, "sends": [{
            "citation": "m", "kind": "vendor", "sent_at": None, "conversation_id": None,
            "external_domains": ["vendor-co.example"], "attachments": [{
                "citation": "a", "file_name": "PO100501.pdf", "po_number": "PO100501",
                "variant": "standard", "sha256": "0" * 64,
                "quick_xor": quick_xor_hash(NEW)}]}]}
        renders = {"status": "read", "complete": True, "renders": [{
            "citation": "r", "file_name": "PO100501.pdf", "po_number": "PO100501",
            "variant": "standard", "issued_at": None, "category": None, "origin": "unread",
            "sha256": None, "quick_xor": quick_xor_hash(NEW)}]}
        report = isd.link_issued_sends(renders=renders, sends=sends, po_scan={
            "status": "read", "rows": [{"row": PO_ROWS[1], "citation": "api://x"}]})
        self.assertEqual(report["counts"]["byte_matches"], {"sha256": 0, "quickXorHash": 1})
        self.assertEqual(report["chains"][0]["outcome"], "before_ordered")
        self.assertEqual(report["gaps"], [])

    def test_renumbered_and_bc_mapped_pos_resolve_through_the_identifier_space(self):
        """PO numbers resolve through identifiers.json ``petra_po`` carriers by their exact
        rule (plx-customer-portal@f7e3f8c resolveGoldPoIdByNumber): a PDF named before the
        BC renumber (align-portal-po-number.ts@f7e3f8c:44-61 keeps it as fmPoNumber) and
        one named by the BC No. it was aligned to find the same row, in any case. A BC No.
        held only in the unserved app_bc_entity_map is a named gap, and the Portal's
        substring fallback is never a match."""
        def send(i, number):
            return {"citation": f"m{i}", "kind": "vendor", "sent_at": None,
                    "conversation_id": None, "external_domains": ["vendor-co.example"],
                    "attachments": [{"citation": f"a{i}", "file_name": f"{number}.pdf",
                                     "po_number": number, "variant": "standard",
                                     "sha256": f"{i:064x}", "quick_xor": None}]}

        def render(i, number):
            return {"citation": f"s{i}", "file_name": f"{number}.pdf", "po_number": number,
                    "variant": "standard", "issued_at": None, "category": "SIGNED_PO",
                    "origin": "hub_or_upload", "sha256": f"{i:064x}", "quick_xor": None}

        renumbered = {"po_number": "PO100900", "status": "ORDERED", "custom_fields": {
            "fmPoNumber": "PO4821", "renumberedTo": "PO100900"}}
        unsent = {"po_number": "PO100901", "status": "SUBMITTED",
                  "custom_fields": json.dumps({"fmPoNumber": "PO4822",
                                               "renumberedTo": "PO100901"})}
        bc_mapped = {"po_number": "PO100960", "status": "SUBMITTED", "custom_fields": {
            "fmPoNumber": "PO#5280", "renumberedTo": "PO100960"}}
        map_only = {"po_number": "PO#5290", "status": "ORDERED", "custom_fields": {}}
        rows = [{"row": renumbered, "citation": "api://renumbered"},
                {"row": {"po_number": "PO100950", "status": "ORDERED"}, "citation": "api://b"},
                {"row": {"po_number": "PO100950", "status": "DRAFT"}, "citation": "api://c"},
                {"row": unsent, "citation": "api://unsent"},
                {"row": bc_mapped, "citation": "api://bc-mapped"},
                {"row": map_only, "citation": "api://map-only"}]
        # po100960 / po#5280: the BC-aligned row by its BC No. and FM number, any case;
        # PO100970: a BC No. only app_bc_entity_map holds; PO10095: inside PO100950.
        numbers = ("PO4821", "PO100900", "PO100950", "po100960", "po#5280", "PO100970",
                   "PO10095")
        report = isd.link_issued_sends(
            {"status": "read", "complete": True, "messages_scanned": 3,
             "sends": [send(i, n) for i, n in enumerate(numbers)]},
            {"status": "read", "complete": True,
             "renders": [render(i, n) for i, n in enumerate((*numbers, "PO4822"))]},
            {"status": "read", "rows": rows, "complete": True})
        chains = {c["po_number"]: c for c in report["chains"]}
        self.assertEqual((chains["PO4821"]["outcome"], chains["PO4821"]["po"]),
                         ("ordered_or_later", {"citation": "api://renumbered",
                                               "status": "ORDERED",
                                               "matched_by": ["custom_fields.fmPoNumber"]}))
        self.assertEqual((chains["PO100900"]["outcome"], chains["PO100900"]["po"]["matched_by"]),
                         ("ordered_or_later", ["custom_fields.renumberedTo", "po_number"]))
        self.assertEqual((chains["PO100950"]["outcome"], chains["PO100950"]["po_candidates"]),
                         ("po_number_ambiguous", ["api://b", "api://c"]))
        self.assertNotIn("po", chains["PO100950"])
        for number, rules in (("po100960", ["custom_fields.renumberedTo", "po_number"]),
                              ("po#5280", ["custom_fields.fmPoNumber"])):
            self.assertEqual((chains[number]["outcome"], chains[number]["po"]["citation"],
                              chains[number]["po"]["matched_by"]),
                             ("before_ordered", "api://bc-mapped", rules))
        for number in ("PO100970", "PO10095"):
            self.assertEqual(chains[number]["outcome"], "po_not_served")
            self.assertNotIn("po_candidates", chains[number])
        self.assertEqual(report["sources"]["po"]["lookup_gaps"], ["app_bc_entity_map.bc_number"])
        # One gap per unsent PO: the render under its FM number joins its served row.
        self.assertEqual([(g["po_number"], g["po"]["citation"], g["renders"], g["reasons"])
                          for g in report["gaps"]],
                         [("PO100901", "api://unsent", ["s7"], ["hub_render_without_sent_copy"]),
                          ("PO#5290", "api://map-only", [],
                           ["no_render_captured", "status_ordered_or_later"])])
        self.assertEqual(report["counts"]["chain_outcomes"]["po_not_served"], 2)

    def test_every_send_and_every_unsent_po_survives_at_volume(self):
        """N=1/10/60: one chain per sent PO and one gap per unsent PO, none lost or doubled."""
        for n in (1, 10, 60):
            numbers = [f"PO{200000 + i}" for i in range(2 * n)]
            sends = {"status": "read", "complete": True, "messages_scanned": n, "sends": [{
                "citation": f"m{i}", "kind": "vendor", "sent_at": "2026-09-20T09:00:00Z",
                "conversation_id": f"c{i}", "external_domains": ["vendor-co.example"],
                "attachments": [{"citation": f"a{i}", "file_name": f"{numbers[i]}.pdf",
                                 "po_number": numbers[i], "variant": "standard",
                                 "sha256": f"{i:064x}", "quick_xor": None}]}
                for i in range(n)],
                "others": [{"citation": f"r{i}", "conversation_id": f"c{i}",
                            "from_domain": "vendor-co.example", "test_redirect": False,
                            "received_at": "2026-09-21T09:00:00Z"} for i in range(n)]}
            renders = {"status": "read", "complete": True, "renders": [{
                "citation": f"s{i}", "file_name": f"{numbers[i]}.pdf",
                "po_number": numbers[i], "variant": "standard", "issued_at": None,
                "category": "SIGNED_PO", "origin": "hub_or_upload", "sha256": f"{i:064x}",
                "quick_xor": None} for i in range(n)]}
            rows = [{"row": {"po_number": number, "status": "ORDERED"}, "citation": f"api://{i}"}
                    for i, number in enumerate(numbers)]
            report = isd.link_issued_sends(sends, renders, {"status": "read", "rows": rows,
                                                            "complete": True})
            self.assertEqual(sorted(c["po_number"] for c in report["chains"]), numbers[:n])
            self.assertEqual([c["replies"] for c in report["chains"]],
                             [[f"r{i}"] for i in range(n)])
            self.assertEqual(sorted(g["po_number"] for g in report["gaps"]), numbers[n:])
            self.assertEqual(report["counts"]["chain_outcomes"]["ordered_or_later"], n)

    def test_an_issued_name_with_hash_and_ampersand_keeps_one_row_id(self):
        """issued-records.ts@b9b5009 (TASK-2479): the row id hashes the name with ``#`` and
        ``&`` kept, so it does not follow the object-name rule that strips them. Expected
        ids are the Portal's issuedAttachmentId, computed with Node's crypto."""
        name = "PO#5280 & Co.pdf"
        stable = isd.issued_attachment_id("PURCHASE_ORDER", "po-1", "ISSUED_PO", name)
        self.assertEqual(stable, "issued_c8664ce02601e38dce3253a29c373cc3")
        self.assertEqual(isd.issued_attachment_id("PURCHASE_ORDER", "po-1", "ISSUED_PO",
                                                  name + " "), stable)
        # The object name and safe_key fold "#" and "&" away; the row id does not.
        stored = isd.safe_key(name)
        self.assertEqual(stored, isd.safe_key("PO_5280 _ Co.pdf"))
        self.assertEqual(isd.issued_attachment_id("PURCHASE_ORDER", "po-1", "ISSUED_PO",
                                                  "PO_5280 _ Co.pdf"),
                         "issued_5d6ed991a8256d463d93cdd12e53323c")
        self.assertNotEqual(isd.issued_attachment_id("PURCHASE_ORDER", "po-1",
                                                     "ISSUED_CUSTOMS", name), stable)

    def test_portal_names_and_redirect_subjects(self):
        self.assertEqual(isd.po_pdf_name("PO100500 - Customs.pdf"),
                         {"po_number": "PO100500", "variant": "customs",
                          "file_name": "PO100500 - Customs.pdf"})
        stored = isd.po_pdf_name(render_name(1759600000000, "000000000001", "PO_5280.pdf"))
        self.assertEqual(stored["po_number"], "PO_5280")
        self.assertEqual(isd.safe_key(stored["po_number"]), isd.safe_key("PO#5280"))
        for name in ("invoice.pdf", "PO100500 draft.pdf", "PO100500.docx", "draft_note.pdf",
                     None):
            self.assertIsNone(isd.po_pdf_name(name), name)
        self.assertTrue(isd.redirected_test_send("RE: [STAGING → a@b.example] PO100500"))
        self.assertFalse(isd.redirected_test_send("Purchase Order PO100500 [STAGING]"))
        # an exempt auth email (email-redirect.ts@5cb95e1:59-66) reaches its recipient
        self.assertFalse(isd.redirected_test_send("[STAGING] Reset your PLX password"))


if __name__ == "__main__":
    unittest.main()
