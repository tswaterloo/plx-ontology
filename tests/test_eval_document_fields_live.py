"""The document-class live check's stage funnel and the document-to-record links
(``scripts/eval_document_fields_live.py``).

``operating.sharepoint_host_check --document-class customer_po`` over the in-process
FakeDrive (one GET-only capture, its projection, the eval tooling's ``evaluate``): every
captured document is counted once, from ``files`` to ``evaluated``, listed by the
capture store's bounded reader (a cut list is ``truncated``, never ``ok``); a scanned
purchase order this host cannot OCR is a counted gap beside a text purchase order that
reaches ``evaluated`` with its fields, and makes the run ``blocked`` with
``ocr_unavailable`` only when nothing was evaluated (never a pass, never "no documents"). A file whose confirmed Portal category is an issued purchase order is excluded
as ``own_issued`` with that provenance signal, and the purchase order beside it with no
signal is evaluated with the gap ``issuer_unknown`` (decision 2026-10-08). ``--links`` over the same FakeDrive plus the in-process FakeBc (two companies that
reuse document numbers) and no Portal: each reference's outcome as the served host reads
it (a Portal record and its verified twin are one candidate, two records sharing a
number stay ambiguous), the agreement of what a linked customer PO attests, and nothing
for a reader withheld the records; a run that measured no link is never ``ok``, and ``requests_sent`` counts the
Portal mirror's GETs; a total ``--budget-seconds`` that cuts the reads or the
measurement stops reading documents and is ``incomplete`` (budget) with counts, never
``ok``; the reserve's sample reads only the rows it keeps (a stride and the records the
documents name), it reserves the documents the run measures at the mean measured over a
sample of them, a run killed mid-estimate leaves the state's own projection to the next
run, a sample that raises is a typed ``incomplete`` report, and a run on a reused state
resumes the BC session its paused units began in. ``roles_unread`` counts reader limits
only (a seen label left unread, an unmapped gap reason), each document once per role,
never what the paper shows (blank, ambiguous, unreadable). Evidence: injected-transport.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "verify/live-checks/ontology.document_fields_customer_po.json"
LINKS_SPEC = ROOT / "verify/live-checks/ontology.document_links.json"
# A customer's PO whose terms start lines with other classes' title words.
TEXT_PO = [["PURCHASE ORDER", "PO Number: 4500000002", "Date: 2026-09-02",
            "Ship to: Northwind DC, Dock 4", "Required date: 2026-10-20",
            "Terms and conditions: each shipment must include a",
            "packing slip showing our PO number.",
            "Order acknowledgement is required within 48 hours."]]
PDFS = importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab")


def one_run_process():
    """The process-wide reading memos empty for one check run and restored after it, as
    in the one-run process the live check is: ``captured_text``'s readings (keyed by
    bytes and name; a read that raised is never remembered, ``ReadingMemo``), its
    container members and ``document_fields``' parsed fields; its profile caches re-read
    from the repo. Without it a run's funnel depends on what earlier tests in the
    process read of the same constant bytes (the seeded drive's PDFs, ``broken.pdf``).
    Each memo is replaced by a fresh one (``ReadingMemo.empty``): a read still running on
    an earlier test's thread remembers into the memo it began with, never this run's."""
    from integrations import captured_text
    from ontology import document_fields
    stack = contextlib.ExitStack()
    for module, name in ((captured_text, "_CACHE"), (captured_text, "_MEMBER_READINGS"),
                         (document_fields, "_FIELDS")):
        stack.enter_context(mock.patch.object(module, name, getattr(module, name).empty()))
    for cached in [f for f in vars(document_fields).values() if hasattr(f, "cache_clear")]:
        cached.cache_clear()
    return stack


@unittest.skipUnless(PDFS and shutil.which("pdftoppm"),
                     "purchase orders are PDFs and scans: needs pypdf, reportlab, pdftoppm")
class StageFunnelTests(unittest.TestCase):
    def run_check(self, files, categories=None, after=None, ocr_fails=None):
        from integrations import capture_sharepoint as cs
        from integrations.tests import test_capture_sharepoint as tsp
        from operating import sharepoint_host_check as check
        from operating.tests.test_employee_permissions import columns_drive
        from scripts import eval_document_fields_live as live
        drive = tsp.seeded_drive() if categories is None else columns_drive()
        if categories is not None:   # the live scope reads DocumentCategory per version
            drive.folder("01QUALITY", "Quality")
            drive.folder("01AOS", "AOs", parent="01QUALITY")
            drive.categories = dict(categories)
        for item_id, name, content in files:
            drive.file(item_id, name, "01AOS", content)
        from integrations import document_understanding as du
        ocr = contextlib.ExitStack()
        ocr.enter_context(mock.patch.object(du, "local_ocr_available",
                                            return_value=ocr_fails is not None))
        if ocr_fails is not None:   # an OCR engine whose every run fails with this cause
            ocr.enter_context(mock.patch.object(du, "_ocr_reading",
                                                side_effect=du.OcrFailed(ocr_fails)))
        with tempfile.TemporaryDirectory() as tmp, one_run_process(), ocr:
            scope, audience = Path(tmp) / "scope.json", Path(tmp) / "audience.json"
            scope.write_text(json.dumps({"schema": cs.SCOPE_SCHEMA, "targets": [
                {"id": "customers", "drive_id": tsp.DRIVE, "folders": [tsp.QUALITY_FOLDER]}],
                **({"list_item_columns": ["DocumentCategory"]}
                   if categories is not None else {})}))
            audience.write_text(json.dumps({
                "schema": "plx-live-source-audience/v1", "tenant": "plx",
                "audiences": {"sharepoint:*": [cs.CAPTURE_PRINCIPAL]}}))
            report = check.run(argparse.Namespace(
                state_dir=str(Path(tmp) / "state"), scope=str(scope),
                live_audience=str(audience), principal=cs.CAPTURE_PRINCIPAL,
                document_class="customer_po", min_documents=1),
                environ=tsp.ENV, sender=drive, sleep=lambda s: None, evaluate=live.evaluate)
            if after is not None:
                after(Path(tmp) / "state", audience)
        self.assertTrue(all(method == "GET" for method, _url in drive.calls))
        return report, check.exit_code(report)

    def assert_sums(self, funnel):
        self.assertTrue(funnel["sums"], funnel)
        self.assertEqual(funnel["files"] + funnel["members"],
                         funnel["evaluated"] + sum(funnel["excluded"].values()), funnel)

    def test_a_scan_without_ocr_is_a_counted_gap_and_blocks_only_when_nothing_was_read(self):
        from verify import live_verify as lv
        from integrations.tests import test_capture_sharepoint as tsp
        from operating.tests.test_attested_missing import pdf
        spec = json.loads(SPEC.read_text("utf-8"))
        lv.validate_spec(spec, stem=spec["id"])
        scan = tsp.scanned_pdf(["PURCHASE ORDER", "PO Number: 4500000003"])

        # The scan alone: no OCR engine is a typed blocked cause, never "no documents".
        report, code = self.run_check([("01SCAN", "PO scan.pdf", scan)])
        funnel = report["funnel"]
        self.assertEqual((report["documents"], report["ok"], report.get("blocked"), code),
                         (0, False, "ocr_unavailable", spec["expect"]["exit_code"]))
        self.assertEqual(funnel["ocr_unavailable"], 1)
        self.assertEqual(funnel["excluded"]["ocr_unavailable"], 1)
        self.assertEqual(funnel["files"], report["capture"]["files_current"])
        self.assert_sums(funnel)
        self.assertEqual(lv.resolve(report, spec["blocked_when"]), ["ocr_unavailable"])
        self.assertTrue(lv.ENUM_STRING.fullmatch(report["blocked"]))
        self.assertEqual(report["unevaluated"], {"ocr_unavailable": 1})

        # With a text PO beside it: the text PO is evaluated and scored with its fields,
        # the scan is a counted gap and no longer blocks the run. The Portal states the
        # text PO's category (the spec's portal_metadata, QA-06).
        report, code = self.run_check([("01SCAN", "PO scan.pdf", scan),
                                       ("01TEXT", "PO text.pdf", pdf(TEXT_PO))],
                                      categories={"01TEXT": "CUSTOMER_PO"})
        funnel = report["funnel"]
        self.assertEqual((report["documents"], funnel["evaluated"], funnel["classified"]
                          ["customer_po"], funnel["dropped_at"]), (1, 1, 1, None), funnel)
        self.assertEqual(report["field_outcomes"]["po_number"]["extracted"], 1)
        self.assertEqual(report["field_outcomes"]["required_date"]["extracted"], 1)
        self.assertEqual(report["citations"], report["citations_resolved"])
        self.assertEqual((report.get("blocked"), report["ok"], code), (None, True, 0))
        self.assertEqual(report["unevaluated"], {"ocr_unavailable": 1})
        self.assertEqual(funnel["excluded"]["ocr_unavailable"], 1)
        self.assert_sums(funnel)
        for secret in ("4500000002", "4500000003", "Northwind", tsp.DRIVE):
            self.assertNotIn(secret, json.dumps(report))
        self.assertEqual(lv.check_expectations(spec, code, report), [])
        recorded = lv._recorded(spec, report)
        self.assertEqual(recorded["funnel"]["excluded"]["ocr_unavailable"], 1)
        self.assertIsNone(recorded.get("blocked"))

    def test_a_failed_ocr_run_is_counted_by_its_typed_cause(self):
        from verify import live_verify as lv
        from integrations.tests import test_capture_sharepoint as tsp
        from operating.tests.test_attested_missing import pdf
        spec = json.loads(SPEC.read_text("utf-8"))
        scan = tsp.scanned_pdf(["PURCHASE ORDER", "PO Number: 4500000003"])
        report, code = self.run_check([("01SCAN", "PO scan.pdf", scan),
                                       ("01TEXT", "PO text.pdf", pdf(TEXT_PO))],
                                      categories={"01TEXT": "CUSTOMER_PO"},
                                      ocr_fails="deadline")
        funnel = report["funnel"]
        # The capture OCR-reads both PDFs and both runs fail; the text PO's own text
        # layer still reaches evaluated, the scan is excluded as ocr_failed.
        self.assertEqual((funnel["ocr_failed"], funnel["ocr_failed_by"],
                          funnel["excluded"].get("ocr_failed"), funnel["evaluated"]),
                         (2, {"deadline": 2}, 1, 1), funnel)
        self.assert_sums(funnel)
        self.assertEqual((report.get("blocked"), report["ok"], code), (None, True, 0))
        self.assertEqual(lv.check_expectations(spec, code, report), [])
        self.assertEqual(lv._recorded(spec, report)["funnel.ocr_failed_by"], {"deadline": 2})

        # A status stored before causes were typed, or with a cause this build does not
        # know, is still counted, never dropped from the sum.
        from ontology.document_fields import stages
        rows = [{"name": "a.pdf", "content_type": "application/pdf",
                 "content_status": "captured", "content_sha256": "0" * 64,
                 "text_status": status}
                for status in ("ocr_failed", "ocr_failed:gremlin", "ocr_failed:tool_exit")]
        with tempfile.TemporaryDirectory() as blobs:
            read = stages(rows, blobs)
        self.assertEqual((read["ocr_failed_by"], read["text"]),
                         ({"untyped": 1, "unknown": 1, "tool_exit": 1}, {"ocr_failed": 3}))

    def test_an_issued_purchase_order_is_excluded_and_the_others_issuer_is_unknown(self):
        """The provenance reaches the live check: the file whose confirmed category is an
        issued purchase order is no customer PO, and the one without a signal is evaluated
        with the issuer recorded unknown."""
        from verify import live_verify as lv
        from operating.tests.test_attested_missing import pdf
        spec = json.loads(SPEC.read_text("utf-8"))
        issued = pdf([["PURCHASE ORDER", "PO Number: 4500000006", "Date: 2026-09-02"]])
        # A PO naming a packing list in a prose title is ambiguous; the funnel says why.
        prose = pdf([["PURCHASE ORDER", "PO100771", "Packing list for order 4500"]])
        report, code = self.run_check(
            [("01ISSUED", "PO issued.pdf", issued),
             ("01TEXT", "PO text.pdf", pdf(TEXT_PO)), ("01PROSE", "PO prose.pdf", prose)],
            categories={"01ISSUED": "ISSUED_PO", "01TEXT": "CUSTOMER_PO"})
        funnel = report["funnel"]
        self.assertEqual((report["documents"], report["ok"], code), (1, True, 0))
        self.assertEqual(funnel["excluded"]["own_issued"], 1)
        self.assertEqual(funnel["signals"]["customer_po"], {"issued_category": 1})
        self.assertEqual(report["gaps"]["issuer_unknown"], report["documents"])
        # The prose-title share of the documents whose class was read.
        self.assertEqual(funnel["ambiguous_class"], {
            "prose_title": 1, "title_lines": 0, "class_read": 3, "prose_title_rate": 0.3333})
        self.assertEqual(funnel["excluded"]["ambiguous_class"], 1)
        self.assert_sums(funnel)
        self.assertEqual(lv.check_expectations(spec, code, report), [])
        self.assertEqual(lv._recorded(spec, report)["funnel.ambiguous_class"],
                         funnel["ambiguous_class"])

    def test_the_funnel_lists_through_the_bounded_store_reader(self):
        """The stages are the store reader's current files: a cut list is truncated
        (never ok), and a reader without a grant is listed nothing."""
        from verify import live_verify as lv
        from integrations import capture_sharepoint as cs
        from integrations.live_source_guard import LiveSourceAudience
        from operating import sharepoint_host_check as check
        from operating.tests.test_attested_missing import pdf
        from ontology.types import Actor
        spec = json.loads(SPEC.read_text("utf-8"))
        files = [(f"01T{n}", f"PO {n}.pdf", pdf([[*TEXT_PO[0][:1], f"PO Number: 45000001{n:02d}",
                                                   *TEXT_PO[0][2:]]])) for n in range(3)]
        seen = {}

        def after(state, audience_file):
            audience = LiveSourceAudience.from_file(audience_file)
            store = cs.SharePointCaptureStore.open_read_only(state)
            try:
                for name, principal in (("reader", cs.CAPTURE_PRINCIPAL),
                                        ("outsider", "staff:outsider")):
                    actor = Actor("plx", frozenset({principal}), "restricted")
                    seen[name] = {limit: check.document_stages(store, actor, audience,
                                                               limit=limit)
                                  for limit in (1, cs.INDEX_LIMIT)}
            finally:
                store.close()

        report, code = self.run_check(files, after=after)
        self.assertEqual((report["documents"], report["ok"], report["funnel"]["truncated"],
                          code), (3, True, False, 0))
        listed = seen["reader"][cs.INDEX_LIMIT]
        self.assertEqual((listed["files"], listed["truncated"], listed["classes"]),
                         (report["capture"]["files_current"], False, {"customer_po": 3}))
        self.assertEqual((seen["reader"][1]["files"], seen["reader"][1]["truncated"]),
                         (1, True))
        self.assertEqual([(seen["outsider"][n]["files"], seen["outsider"][n]["truncated"])
                          for n in (1, cs.INDEX_LIMIT)], [(0, False), (0, False)])
        # The live run over more current files than the read-back bound: never ok.
        with mock.patch.object(cs, "INDEX_LIMIT", 2):
            report, code = self.run_check(files)
        self.assertEqual((report["stages"]["files"], report["funnel"]["truncated"],
                          report["ok"], code), (2, True, False, 1))
        self.assertNotEqual(lv.check_expectations(spec, code, report), [])

    def test_the_stage_that_dropped_every_document_is_named(self):
        from operating.tests.test_attested_missing import pdf
        # Text documents of no class: read, never classified; nothing blocks the run. A
        # letterhead and the title on one text line is no title, but the funnel says the
        # text held the class's title phrase and where.
        memo = pdf([["MEMO", "Please call the customer about artwork."]])
        merged = pdf([["Acme Retail Inc. PURCHASE ORDER", "PO Number: 4500000004"]])
        report, code = self.run_check([("01MEMO", "memo.pdf", memo),
                                       ("01MERGED", "po.pdf", merged)])
        funnel = report["funnel"]
        self.assertEqual((report["documents"], report.get("blocked"), code), (0, None, 1))
        self.assertEqual(funnel["dropped_at"], "classification", funnel)
        self.assertGreaterEqual(funnel["excluded"]["unclassified"], 2)
        # The seeded drive's "Lot release for PO100234, ..." text line starts with the
        # decision record's title phrase and runs on: named, never classified.
        self.assertEqual(funnel["signals"], {"customer_po": {"after_text": 1},
                                             "decision_record": {"not_alone": 1}})
        self.assert_sums(funnel)
        self.assertEqual(funnel["no_candidate"], {
            "stage": "classification", "reason": "title_phrase_not_title",
            "unread_documents": sum(funnel["unread_kinds"].values()), "title_signals": 1,
            "portal_metadata_documents": 0})

        # QA-06: documents of another class only, beside documents left unread before
        # their class was read: documents 0 is recorded with the stage and why, typed.
        from verify import live_verify as lv
        spec = json.loads(SPEC.read_text("utf-8"))
        slip = pdf([["PACKING SLIP", "Ship to: Dock 4", "Qty 10"]])
        report, code = self.run_check([("01SLIP", "slip.pdf", slip),
                                       ("01BAD", "broken.pdf", b"%PDF-1.7 broken")])
        funnel = report["funnel"]
        self.assertEqual((report["documents"], report["ok"], funnel["dropped_at"], code),
                         (0, False, "class", 1), funnel)
        unread = funnel["excluded"]["unsupported"]
        self.assertGreaterEqual(unread, 1)
        self.assertEqual(funnel["no_candidate"], {
            "stage": "class", "reason": "no_title_phrase_unread_documents",
            "unread_documents": unread, "title_signals": 0, "portal_metadata_documents": 0})
        self.assertIn("documents", " ".join(lv.check_expectations(spec, code, report)))
        self.assertEqual(lv._recorded(spec, report)["funnel.no_candidate"],
                         funnel["no_candidate"])
        # Read through, nothing of this class is left: no candidate is named.
        report, _code = self.run_check([("01TEXT", "PO text.pdf", pdf(TEXT_PO))])
        self.assertIsNone(report["funnel"]["no_candidate"])

    def test_a_read_that_failed_in_an_earlier_run_does_not_change_the_next_funnel(self):
        # The funnel of a run is its own reading of the drive: a read that raised in an
        # earlier run of the same process (the seeded drive's constant-byte PDFs and
        # broken.pdf are shared by many tests) is not remembered into this run.
        from operating.tests.test_attested_missing import pdf
        files = [("01SLIP", "slip.pdf", pdf([["PACKING SLIP", "Ship to: Dock 4"]])),
                 ("01BAD", "broken.pdf", b"%PDF-1.7 broken")]
        with mock.patch("integrations.captured_text.read_text",
                        side_effect=RuntimeError("transient read failure")):
            failed, _code = self.run_check(files)
        self.assertEqual(failed["funnel"]["excluded"], {"unreadable": 5}, failed["funnel"])
        report, code = self.run_check(files)
        funnel = report["funnel"]
        # Read in this run: the slip is of another class, broken.pdf and the seeded PDFs
        # are of an unsupported type, the seeded text is read and of no class.
        self.assertEqual((funnel["excluded"], funnel["dropped_at"], code),
                         ({"other_class": 1, "unclassified": 1, "unsupported": 3}, "class", 1),
                         funnel)
        self.assert_sums(funnel)


WITHHELD = "staff:document-links-withheld"


def slip(po):
    return [["PACKING SLIP", f"PO Reference: {po}", "Ship date: 2026-09-15"]]


def customer_po(po, order):
    return [["PURCHASE ORDER", f"PO Number: {po}", "Date: 2026-09-02",
             f"Our ref: Sales Order {order}", "Required date: 2026-10-20"]]


def bc_world(items=0):
    """Two companies (BC numbers are unique only within one): a purchase order only the
    first holds, one both hold, and two sales orders of the first with the customer PO
    the documents name and with another one, and ``items`` items each, a set read after
    those (synthetic ids and numbers only)."""
    from integrations.bc_read import CAPTURE_ENTITY_SETS
    from integrations.tests import test_bc_capture as tb
    from ontology.tests.test_project_bc import gid, record
    fake = tb.FakeBc({name: 0 for name in CAPTURE_ENTITY_SETS} | {"items": items},
                     companies=tb.CO)
    for c, company in ((1, tb.CO[0]), (2, tb.CO[1])):
        fake.data[(company, "purchaseOrders")] = [record(
            gid(c, 1, n), number=number, vendorNumber="V-1", status="Open",
            orderDate="2026-09-01", currencyCode="USD")
            for n, number in enumerate(["PO-1001"] + (["PO-2001"] if c == 1 else []), 1)]
    fake.data[(tb.CO[0], "salesOrders")] = [
        record(gid(1, 2, n), number=number, externalDocumentNumber=external,
               customerNumber="C-1", orderDate="2026-09-02", currencyCode="USD")
        for n, (number, external) in enumerate((("SO-3001", "4500000002"),
                                                 ("SO-3002", "4500000777")), 1)]
    return fake


def portal(purchase_orders=(), generated=(), urls=None):
    """A fixture Portal serving every relation that carries a Petra PO or a sales order
    number a packing slip or a customer PO may link to (``purchase_orders``: rows of its
    own PO relation, ``generated``: of FileMaker's generated POs; the rest empty); each
    URL it is asked for is appended to ``urls`` when given."""
    from evals.oracle.fixture_world import World
    from integrations.workbench_read_api import WorkbenchReadApiClient
    from scripts.eval_dev_baseline import FIXTURE_KEY, FIXTURE_URL
    from scripts.eval_unfamiliar_sample import _fixture_fetch
    served = _fixture_fetch(World({
        "public.app_purchase_order": list(purchase_orders),
        "public.generated_purchase_order": list(generated), "sage.po_header": [],
        "public.app_sales_order": [], "public.order_entry": [], "sage.oe_order_header": []}))

    def fetch(url, *args, **kwargs):
        if urls is not None:
            urls.append(url)
        return served(url, *args, **kwargs)
    return WorkbenchReadApiClient(FIXTURE_URL, FIXTURE_KEY, fetch=fetch)


class IdentityTiersTests(unittest.TestCase):
    def test_the_evals_join_records_at_the_ontology_tools_identity_tiers(self):
        # One rule for which link tiers make two records one: the evals read the ontology
        # tools' own list, so a tier the tools stop treating as identity stops joining here.
        from evals import baseline
        from operating.ontology_tools import _IDENTITY_TIERS
        from scripts import eval_document_fields_live as live
        self.assertIs(baseline.IDENTITY_TIERS, _IDENTITY_TIERS)
        self.assertIs(live.IDENTITY_TIERS, _IDENTITY_TIERS)


class CauseVocabularyTests(unittest.TestCase):
    def test_every_cause_the_product_can_give_is_named_in_the_eval(self):
        """A cause ``agreement_with_causes`` can give is a gap reason ``_compare`` reports,
        the missing-in-record lead or a ``NOT_COMPARED`` cause: each has its own name in
        ``NOT_COMPARABLE_CAUSES``, never ``unclassified`` unnoticed. The gap reasons are
        read from the product's own code (every ``_gap`` of a function ``_compare``
        reaches), so a reason added there and not to ``GAP_REASONS`` fails here."""
        import ast
        from operating import attested_conflicts as ac
        from scripts import eval_document_fields_live as live
        tree = ast.parse(Path(ac.__file__).read_text("utf-8"))
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        constants = {target.id: node.value.value for node in tree.body
                     if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                     for target in node.targets if isinstance(target, ast.Name)}

        def strings(node):   # the string constants an expression can evaluate to
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return {node.value}
            if isinstance(node, ast.Name) and isinstance(constants.get(node.id), str):
                return {constants[node.id]}
            if isinstance(node, ast.IfExp):
                return strings(node.body) | strings(node.orelse)
            self.fail(f"a gap reason that is not a constant: {ast.dump(node)[:200]}")

        reached, todo = set(), ["_compare"]
        while todo:
            name = todo.pop()
            if name in reached or name not in functions:
                continue
            reached.add(name)
            todo += [call.func.id for call in ast.walk(functions[name])
                     if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)]
        emitted: set[str] = set()
        for name in sorted(reached):
            assigned = {}   # reason = ..., reason, why = ..., ...
            for node in ast.walk(functions[name]):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        pairs = zip(target.elts, node.value.elts) if isinstance(
                            target, ast.Tuple) and isinstance(node.value, ast.Tuple) \
                            else [(target, node.value)]
                        for left, right in pairs:
                            if isinstance(left, ast.Name) and left.id == "reason":
                                assigned.setdefault("reason", set()).update(strings(right))
            for call in ast.walk(functions[name]):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and \
                        call.func.id == "_gap":
                    arg = call.args[1]
                    emitted |= assigned["reason"] if isinstance(arg, ast.Name) and \
                        arg.id == "reason" else strings(arg)
        self.assertIn("_not_comparable", reached)
        self.assertEqual(emitted, set(ac.GAP_REASONS))
        causes = {*ac.GAP_REASONS, ac.MISSING_IN_RECORD, *ac.NOT_COMPARED}
        self.assertEqual(len(causes), len(ac.GAP_REASONS) + 1 + len(ac.NOT_COMPARED))
        self.assertLessEqual(causes, set(live.NOT_COMPARABLE_CAUSES) - {"unclassified"})


class RoleReadsTests(unittest.TestCase):
    def test_roles_unread_counts_reader_limits_only_each_document_once(self):
        # Blank, ambiguous and unreadable are what the paper shows: reported per role,
        # never unread. A seen label left unread, or a gap reason the eval does not know,
        # is a reader limit (never a pass). Each document lands in one bucket of a role.
        from ontology import document_fields
        from scripts import eval_document_fields_live as live
        names = [spec["name"] for spec in next(p for p in document_fields.profiles()[
            "profiles"] if p["id"] == "ao_packet")["fields"]]
        roles = live.field_roles("ao_packet")
        produced = sorted(n for n, role in roles.items() if role == "produced")

        def report(per_document):
            rows = [{"documents": 1, "fields": {**dict.fromkeys(names, "not_stated"),
                                                **dict.fromkeys(produced, outcome)}}
                    for outcome in per_document]
            return live.denominators({"documents": len(rows), "document_class": "ao_packet",
                                      "fields": {}, "document_outcomes": rows})

        paper = ("value_missing", "unchecked", "conflicting_values", "unreadable")
        for n in (1, 10, 60):   # no document lost or counted twice
            counts = report([paper[i % len(paper)] for i in range(n)] + ["stated"])
            self.assertEqual(counts["roles_unread"], 0, n)
            self.assertEqual(sum(counts["role_documents"]["produced"].values()), n + 1)
        for reader_limit in ("label_seen_unread", "a_reason_not_mapped"):
            counts = report(["stated", reader_limit, "value_missing"])
            self.assertEqual(counts["roles_unread"], 1, reader_limit)
            self.assertEqual({k: v for k, v in counts["role_documents"]["produced"].items()
                              if v}, {"extracted": 1, live.REASONS.get(
                                  reader_limit, "other_gap"): 1, "blank": 1})
        # A keyed box only handwriting OCR could not key is written in is its own bucket,
        # reported per role and per packet with its citation positions, never in
        # roles_unread; a printed box no key was read from still is.
        damaged = sorted(n for n, role in roles.items() if role == "damaged")
        cited = [{"page": 2, "start": 10, "end": 17}, {"page": 2, "start": 40, "end": 46}]
        for printed, unread in ((False, 0), (True, 1)):
            rows = [{"documents": 1, "fields": {**dict.fromkeys(names, "stated"),
                                                damaged[0]: outcome}}
                    for outcome in ["handwritten_unkeyed"] + ["label_seen_unread"] * printed]
            counts = live.denominators({
                "documents": len(rows), "document_class": "ao_packet", "fields": {},
                "document_outcomes": rows,
                "handwritten_unkeyed": [{"fields": {damaged[0]: cited}}]})
            self.assertEqual((counts["roles_unread"], counts["roles_unread_handwritten"],
                              counts["handwritten_packets"],
                              counts["role_documents"]["damaged"]["handwritten_unkeyed"]),
                             (unread, 1, [{"damaged": cited}], 1), printed)
        # A one-document report's tallies carry why each seen label was left unread.
        single = live.denominators({"documents": 1, "document_class": "ao_packet", "fields": {
            **{n: {"not_stated": 1} for n in names},
            produced[0]: {"label_seen_unread": 1, "unread_by": {"header_unseen": 1}}}})
        self.assertEqual((single["roles_unread"], single["role_outcomes"]["produced"][
            "unread_by"]), (1, {"header_unseen": 1}))


class FieldFloorTests(unittest.TestCase):
    """``field_shares`` over the documents stating a label and ``--floor`` (module
    docstring): the boundary the COA live check's json_min reads."""

    def test_a_share_is_over_the_stating_documents_and_one_is_no_share(self):
        from decimal import Decimal
        from scripts import eval_document_fields_live as live
        for n in (1, 10, 60):   # every document in one bucket, none lost or doubled
            with self.subTest(documents=n):
                absent, conflict = n // 3, n // 5
                read = n - absent - conflict
                out = live.denominators({"documents": n, "document_class":
                                         "certificate_of_analysis", "fields": {"lot": {
                                             "stated": read, "not_stated": absent,
                                             "conflicting_values": conflict},
                                             "supplier": {"not_stated": n}}})
                row = out["field_outcomes"]["lot"]
                self.assertEqual(sum(row[b] for b in live.BUCKETS), n)
                self.assertEqual((row["stated"], row["label_absent"], row["ambiguous"]),
                                 (n - absent, absent, conflict))
                share = (int(read * 10000 / (n - absent)) / 10000
                         if n - absent >= live.MIN_STATED else None)
                self.assertEqual(out["field_shares"]["lot"], share)
                self.assertEqual(row["share_status"], "measured" if share is not None
                                 else "insufficient_stated")
                self.assertEqual(row["raw_share"], int(read * 10000 / n) / 10000)
                # a field no document names: no share, every document label_absent
                self.assertEqual((out["field_shares"]["supplier"],
                                  out["field_outcomes"]["supplier"]["stated"]), (None, 0))
        empty = live.denominators({"documents": 0, "document_class":
                                   "certificate_of_analysis", "fields": {}})
        self.assertEqual((empty["field_outcomes"]["lot"]["share_status"],
                          empty["field_shares"]["lot"],
                          empty["field_outcomes"]["lot"]["raw_share"]),
                         ("no_documents", None, None))
        # An unknown gap reason is a miss in the share, never a pass.
        out = live.denominators({"documents": 2, "document_class": "certificate_of_analysis",
                                 "fields": {"lot": {"stated": 1, "a_reason_not_mapped": 1}}})
        self.assertEqual((out["field_outcomes"]["lot"]["other_gap"],
                          out["field_shares"]["lot"]), (1, 0.5))
        fields = {name: {"documents": 3, **dict.fromkeys(live.BUCKETS, 0), **row}
                  for name, row in (("lot", {"extracted": 2, "ambiguous": 1}),
                                    ("supplier", {"extracted": 1, "label_absent": 2}),
                                    ("expiry_date", {"extracted": 1, "blank": 2}))}
        floor = Decimal("0.6666")
        self.assertEqual(live.floor_outcome(fields, {"lot": floor, "supplier": floor}),
                         {"fields": {"lot": "met", "supplier": "insufficient_stated"},
                          "blocked": "insufficient_stated"})
        self.assertEqual(live.floor_outcome(fields, dict.fromkeys(fields, floor)), {
            "fields": {"expiry_date": "below", "lot": "met",
                       "supplier": "insufficient_stated"}, "blocked": None})
        self.assertIsNone(live.floor_outcome(fields, {}))

    def test_floor_arguments_are_checked_and_never_reach_the_host_check(self):
        from decimal import Decimal
        from scripts import eval_document_fields_live as live
        base = ["--state-dir", "s", "--scope", "x.json", "--live-audience", "a.json",
                "--document-class", "certificate_of_analysis"]
        with mock.patch.object(live.check, "main", return_value=0) as host, \
                mock.patch.object(live, "evaluate", return_value={}) as evaluate:
            self.assertEqual(live.main([*base, "--floor", "lot=0.6666",
                                        "--floor", "supplier=1"]), 0)
            argv = host.call_args.args[0]
            self.assertNotIn("--floor", argv)
            self.assertEqual(sorted(argv), sorted(base))
            host.call_args.kwargs["evaluate"]("store", "report")
            evaluate.assert_called_once_with("store", "report", {
                "lot": Decimal("0.6666"), "supplier": Decimal("1")})
            live.main(base)
            self.assertEqual(host.call_args.args[0], base)
        for bad in (["--floor", "lot"], ["--floor", "lot=0"], ["--floor", "lot=1.5"],
                    ["--floor", "lot=nan"], ["--floor", "lot=inf"], ["--floor", "lot=x"],
                    ["--floor", "not_a_field=0.5"], ["--floor", "lot=0.5", "--floor",
                                                     "lot=0.6"]):
            with self.subTest(bad), mock.patch.object(live.check, "main") as host, \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                live.main([*base, *bad])
            self.assertEqual(raised.exception.code, 2)
            host.assert_not_called()
        with mock.patch.object(live.check, "main") as host, \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            live.main(["--state-dir", "s", "--floor", "lot=0.5"])
        host.assert_not_called()


@unittest.skipUnless(PDFS, "documents are PDFs: needs pypdf and reportlab")
class DocumentLinksTests(unittest.TestCase):
    def run_links(self, files, fake, *, bc_readers=(), portal=None, extra=(),
                  seconds_per_get=None, clock=None, bc_seconds_per_get=None, state=None):
        from integrations import capture_sharepoint as cs
        from integrations.tests import test_bc_capture as tb
        from integrations.tests import test_capture_sharepoint as tsp
        from scripts import eval_document_fields_live as live
        drive = tsp.seeded_drive()
        for item_id, name, content in files:
            drive.file(item_id, name, "01AOS", content)
        sender = drive
        if seconds_per_get is not None:   # each Graph read costs time on ``clock``
            def sender(*request):
                clock[0] += seconds_per_get
                return drive(*request)
        bc_sender = fake
        if bc_seconds_per_get is not None:   # and each BC read
            def bc_sender(*request, **options):
                clock[0] += bc_seconds_per_get
                return fake(*request, **options)
        with tempfile.TemporaryDirectory() as tmp:
            scope, audience = Path(tmp) / "scope.json", Path(tmp) / "audience.json"
            scope.write_text(json.dumps({"schema": cs.SCOPE_SCHEMA, "targets": [
                {"id": "quality", "drive_id": tsp.DRIVE, "folders": [tsp.QUALITY_FOLDER]}]}))
            audience.write_text(json.dumps({
                "schema": "plx-live-source-audience/v1", "tenant": "plx",
                "audiences": {"sharepoint:*": [cs.CAPTURE_PRINCIPAL, WITHHELD],
                              "bc:*": [cs.CAPTURE_PRINCIPAL, *bc_readers]}}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = live.main(["--links", "--state-dir", str(state or Path(tmp) / "state"),
                                  "--scope", str(scope), "--live-audience", str(audience),
                                  *extra],
                                 environ={**tb.ENV, **tsp.ENV}, sender=sender,
                                 bc_sender=bc_sender, bc_books=tb.BOOKS,
                                 portal_client=portal,
                                 monotonic=None if clock is None else lambda: clock[0])
        self.assertTrue(all(method == "GET" for method, _url in drive.calls))
        self.drive_calls = list(drive.calls)
        self.assertTrue(all(method == "GET" for method, url, _ in fake.calls
                            if "login.microsoftonline.com" not in url))
        return json.loads(out.getvalue()), code, out.getvalue()

    def test_each_reference_gets_one_outcome_and_a_withheld_reader_is_shown_nothing(self):
        from verify import live_verify as lv
        from integrations.tests import test_bc_capture as tb
        from operating.tests.test_attested_missing import pdf
        from scripts import eval_document_fields_live as live
        spec = json.loads(LINKS_SPEC.read_text("utf-8"))
        lv.validate_spec(spec, stem=spec["id"])
        files = [("01ONE", "slip one.pdf", pdf(slip("PO-2001"))),      # one company
                 ("01TWO", "slip two.pdf", pdf(slip("PO-1001"))),      # both companies
                 ("01NONE", "slip none.pdf", pdf(slip("PO-9999"))),    # in no capture
                 ("01AGREE", "po agree.pdf", pdf(customer_po("4500000002", "SO-3001"))),
                 ("01CONFLICT", "po conflict.pdf", pdf(customer_po("4500000002", "SO-3002")))]
        urls = []
        client = portal(urls=urls)
        report, code, text = self.run_links(files, bc_world(), portal=client,
                                            extra=["--portal-workers", "3"])
        drive_gets = self.drive_calls
        self.assertEqual((report["ok"], code, report["evidence_label"]),
                         (True, 0, "injected-transport"), text[:3000])
        self.assertEqual(lv.check_expectations(spec, code, report), [])
        self.assertEqual((report["sources"], report["blocked_upstream"]),
                         (["bc", "portal_api", "sharepoint"], []))
        slips = report["by_field"]["packing_slip"]["petra_po"]
        # Every carrier of the kind captured whole: the PO of one company links to it; the
        # PO both companies hold is ambiguous and never corroborated; a PO no source holds
        # is no_record.
        self.assertEqual((slips["references"], slips["linked_one"], slips["ambiguous"],
                          slips["source_not_captured"], slips["no_record"],
                          slips["withheld"]), (3, {"lead": 1}, 1, 0, 1, 0), slips)
        self.assertGreaterEqual(report["cross_scope_references"], 1)
        self.assertEqual(report["cross_scope_corroborated"], 0)
        # The customer POs each link the one sales order they name; what they attest
        # agrees with one and conflicts with the other; BC holds no required date.
        self.assertEqual(report["agreement_by_field"]["customer_po"], {
            "po_number": {"agree": 1, "conflict": 1, "not_comparable": 0},
            "required_date": {"agree": 0, "conflict": 0, "not_comparable": 2}})
        # Each not_comparable is counted under one cause: the required date is a property
        # only the Portal's sales orders carry, never BC's.
        self.assertEqual(report["not_comparable_by_field"]["customer_po"],
                         {"required_date": {"record_source_lacks_property": 2}})
        self.assertEqual(sum(report["agreement_not_comparable"].values()),
                         report["agreement"]["not_comparable"])
        self.assertTrue(set(report["agreement_not_comparable"]) <=
                        set(live.NOT_COMPARABLE_CAUSES))
        # The Portal mirror reads only the relations these documents' kinds are looked up
        # in or compare with: never an assembly order's.
        mirror = report["portal_mirror"]
        self.assertLess(mirror["relations"], mirror["relations_declared"])
        self.assertIsNone(mirror["skipped"])
        self.assertTrue(urls and not [u for u in urls if "assembly_order" in u], urls[:5])
        self.assertEqual(sum(report["outcomes"][k] for k in ("ambiguous", "no_record",
                                                             "source_not_captured",
                                                             "withheld"))
                         + sum(report["outcomes"]["linked_one"].values()),
                         report["references"])
        # A reader granted the files but no record source: every record it would link to
        # is withheld, the served host gives it no edge, object or count.
        self.assertEqual(report["withheld_reader"]["documents"], 5)
        self.assertEqual(report["withheld_reader"]["revealed"], 0)
        self.assertEqual(report["withheld_reader"]["withheld"],
                         report["withheld_reader"]["references"]
                         - report["outcomes"]["source_not_captured"]
                         - report["outcomes"]["no_record"])
        self.assertEqual(report["leaks"], 0)
        self.assertEqual(report["links_measured"], sum(report["outcomes"]["linked_one"].values())
                         + report["outcomes"]["ambiguous"] + report["outcomes"]["no_record"])
        self.assertGreaterEqual(report["links_measured"], 3)
        # The Portal mirror's GETs are part of the run's requests.
        self.assertEqual(report["portal_mirror"]["requests_sent"], client.requests_sent)
        self.assertGreater(client.requests_sent, 0)
        self.assertGreaterEqual(report["requests_sent"], client.requests_sent
                                + report["bc_capture"]["requests_sent"] + len(drive_gets))
        for secret in ("PO-2001", "PO2001", "SO-3001", "4500000002", tb.CO[0], tb.CO[1],
                       "bc://", "sharepoint://"):
            self.assertNotIn(secret, text)
        recorded = lv._recorded(spec, report)
        self.assertEqual(recorded["leaks"], 0)
        self.assertEqual(recorded["by_field"]["packing_slip"]["petra_po"]["ambiguous"], 1)
        self.assertEqual(recorded["not_comparable_by_field"]["customer_po"]["required_date"],
                         {"record_source_lacks_property": 2})
        self.assertEqual(recorded["agreement_not_comparable"],
                         report["agreement_not_comparable"])
        self.assertEqual(recorded["portal_mirror"], mirror)

    def test_a_not_comparable_cause_not_declared_is_counted_unclassified(self):
        from operating import attested_conflicts as ac
        from operating.tests.test_attested_missing import pdf
        files = [("01AGREE", "po agree.pdf", pdf(customer_po("4500000002", "SO-3001")))]
        real = ac.agreement_with_causes
        with mock.patch.object(ac, "agreement_with_causes", lambda *a: {
                field: ("not_comparable", "a_reason_not_declared") for field in real(*a)}):
            report, _code, text = self.run_links(files, bc_world(), portal=portal())
        self.assertEqual(report["agreement"]["not_comparable"], 2, text[:3000])
        self.assertEqual(report["agreement_not_comparable"], {"unclassified": 2})

    def test_cause_from_agreement(self):
        """The cause a not_comparable is counted under is the one the product's agreement
        decided (``agreement_with_causes``), never a second classification in the eval."""
        from operating import attested_conflicts as ac
        from operating.tests.test_attested_missing import pdf
        from scripts import eval_document_fields_live as live
        source = Path(live.__file__).read_text("utf-8")
        self.assertFalse("ac._" in source or re.search(
            r"attested_conflicts import[^\n]*\b_[a-z]", source), "a private product helper")
        files = [("01AGREE", "po agree.pdf", pdf(customer_po("4500000002", "SO-3001"))),
                 ("01CONFLICT", "po conflict.pdf", pdf(customer_po("4500000002", "SO-3002")))]
        # The product's real decision, recorded as the eval reads it.
        real, decided = ac.agreement_with_causes, []

        def spy(*args):
            out = real(*args)
            decided.append(out)
            return out
        with mock.patch.object(ac, "agreement_with_causes", spy):
            report, _code, text = self.run_links(files, bc_world(), portal=portal())
        expected: dict[str, int] = {}
        for fields in decided:
            for outcome, cause in fields.values():
                if outcome == "not_comparable":
                    expected[cause] = expected.get(cause, 0) + 1
        self.assertTrue(expected, text[:3000])
        self.assertEqual(report["agreement_not_comparable"], expected)
        self.assertEqual(report["agreement"]["not_comparable"], sum(expected.values()))
        # A cause the product decides where a re-derivation would see none (the values
        # compare equal): the eval reports the product's, for every field it decided.
        with mock.patch.object(ac, "agreement_with_causes", lambda *a: {
                field: ("not_comparable", "different_measure") for field in real(*a)}):
            report, _code, text = self.run_links(files, bc_world(), portal=portal())
        self.assertEqual(report["agreement_not_comparable"], {"different_measure": 4},
                         text[:3000])
        self.assertEqual(report["not_comparable_by_field"]["customer_po"], {
            "po_number": {"different_measure": 2}, "required_date": {"different_measure": 2}})

    def test_no_record_only_when_every_carrier_was_captured_and_a_leak_is_counted(self):
        from operating.tests.test_attested_missing import pdf
        # A PO only the Portal holds links, a PO no source holds is no_record.
        rows = [{"id": "po-1", "po_number": "PO-5001"}]
        files = [(f"01S{n}", f"slip {n}.pdf", pdf(slip(po)))
                 for n, po in enumerate(("PO-5001", "PO-9999", "PO-2001"))]
        report, code, _ = self.run_links(files, bc_world(), portal=portal(rows))
        self.assertEqual((report["ok"], code, report["sources"], report["blocked_upstream"]),
                         (True, 0, ["bc", "portal_api", "sharepoint"], []))
        self.assertEqual(report["by_field"]["packing_slip"]["petra_po"], {
            "references": 3, "linked_one": {"lead": 2}, "ambiguous": 0, "no_record": 1,
            "source_not_captured": 0, "withheld": 0})
        # The Portal record is withheld from the files-only reader by the mirror audience.
        self.assertEqual((report["withheld_reader"]["withheld"], report["leaks"]), (2, 0))
        # The same reader granted BC: what the served host now gives it is a leak.
        report, code, _ = self.run_links(files, bc_world(), bc_readers=[WITHHELD],
                                         portal=portal(rows))
        self.assertEqual(report["withheld_reader"]["revealed"], 1)
        self.assertGreater(report["leaks"], report["withheld_reader"]["revealed"])
        self.assertEqual((report["ok"], code), (False, 1))

    def test_a_record_and_its_twin_are_one_candidate_and_two_records_stay_ambiguous(self):
        from verify import live_verify as lv
        from operating.tests.test_attested_missing import pdf
        # PO-5001: a Portal PO and the FileMaker PO it was made from (joined by the
        # verified PurchaseOrder.same_record_as), named on the slip's two pages; PO-6001:
        # a Portal PO and an unrelated FileMaker PO that share the number.
        rows = [{"id": "po-1", "po_number": "PO-5001", "custom_fields": {"fmGeneratedPoId": 71}},
                {"id": "po-2", "po_number": "PO-6001", "custom_fields": {"fmGeneratedPoId": 99}}]
        generated = [{"id": 71, "ponumber": "PO-5001"}, {"id": 72, "ponumber": "PO-6001"}]
        files = [("01TWIN", "slip twin.pdf", pdf(slip("PO-5001") * 2)),
                 ("01PAIR", "slip pair.pdf", pdf(slip("PO-6001")))]
        report, code, text = self.run_links(files, bc_world(),
                                            portal=portal(rows, generated))
        self.assertEqual(report["by_field"]["packing_slip"]["petra_po"], {
            "references": 2, "linked_one": {"lead": 1}, "ambiguous": 1, "no_record": 0,
            "source_not_captured": 0, "withheld": 0}, text[:3000])
        self.assertEqual((report["identity_joined"], report["ambiguous_candidates"]),
                         (1, {"2": 1}))
        self.assertEqual((report["cross_scope_corroborated"], report["leaks"]), (0, 0))
        spec = json.loads(LINKS_SPEC.read_text("utf-8"))
        self.assertEqual(lv.check_expectations(spec, code, report), [])
        self.assertEqual(lv._recorded(spec, report)["ambiguous_candidates"], {"2": 1})

    def test_one_candidate_of_a_partial_capture_is_never_linked_one(self):
        from verify import live_verify as lv
        from integrations.tests import test_bc_capture as tb
        from operating.tests.test_attested_missing import pdf
        # The second company's purchase orders fail: the one PO the first company holds
        # may not be the only record its number names, and a PO both hold shows one.
        fake = bc_world()
        fake.fail(rf"companies\({tb.CO[1]}\)/purchaseOrders",
                  *[(500, {}, b'{"error":{"code":"InternalServerError"}}')] * 40)
        files = [("01ONE", "slip one.pdf", pdf(slip("PO-2001"))),
                 ("01TWO", "slip two.pdf", pdf(slip("PO-1001")))]
        report, code, _ = self.run_links(files, fake, portal=portal())
        self.assertFalse(report["bc_capture"]["all_complete"])
        self.assertEqual(report["by_field"]["packing_slip"]["petra_po"], {
            "references": 2, "linked_one": {}, "ambiguous": 0, "no_record": 0,
            "source_not_captured": 2, "withheld": 0})
        self.assertEqual(report["outcomes"]["linked_one"], {})
        # No reference a captured source answered: no link measured, never a pass.
        spec = json.loads(LINKS_SPEC.read_text("utf-8"))
        self.assertEqual((report["links_measured"], report["ok"], code), (0, False, 1))
        self.assertIn("links_measured", " ".join(lv.check_expectations(spec, code, report)))

    def test_no_record_source_blocks_and_never_reads_as_no_record(self):
        from verify import live_verify as lv
        from integrations.tests import test_bc_capture as tb
        from operating.tests.test_attested_missing import pdf
        spec = json.loads(LINKS_SPEC.read_text("utf-8"))
        broken = tb.FakeBc({})   # BC refuses the company listing: nothing is captured
        broken.fail(r"/companies$", (403, {}, b'{"error":{"code":"Authorization_Failed"}}'))
        report, code, _ = self.run_links([("01ONE", "slip.pdf", pdf(slip("PO-2001")))],
                                         broken)
        self.assertEqual((report["blocked"], report["ok"], code), ("no_record_source", False, 0))
        self.assertEqual(lv.resolve(report, spec["blocked_when"]), ["no_record_source"])
        self.assertEqual((report["outcomes"]["source_not_captured"],
                          report["outcomes"]["no_record"]), (1, 0))


    def test_a_mirror_no_document_needs_says_why_it_read_nothing(self):
        from operating.tests.test_attested_missing import pdf
        client = portal()
        report, _code, text = self.run_links(
            [("01ONE", "notes.pdf", pdf("Meeting notes, nothing referenced"))],
            bc_world(), portal=client)
        self.assertEqual((report["portal_mirror"]["relations"],
                          report["portal_mirror"]["skipped"], client.requests_sent),
                         (0, "no_relation_needed", 0), text[:3000])

    def test_a_total_budget_stops_reading_and_is_a_typed_incomplete_never_a_pass(self):
        from verify import live_verify as lv
        from ontology import project
        from operating.host import projection_path
        from operating.tests.test_attested_missing import pdf
        from scripts import eval_document_fields_live as live
        spec = json.loads(LINKS_SPEC.read_text("utf-8"))
        lv.validate_spec(spec, stem=spec["id"])
        self.assertLessEqual(spec["timeout_seconds"], 1800)
        self.assertLess(float(spec["command"][spec["command"].index("--budget-seconds") + 1]),
                        spec["timeout_seconds"])
        files = [(f"01S{n}", f"slip {n}.pdf", pdf(slip(f"PO-{1001 + n}"))) for n in range(5)]
        # Each Graph GET costs 10 s: the SharePoint walk spends the whole budget and stops
        # reading documents part way, no record source is read, and of the documents it
        # read only the first is measured.
        fake, client = bc_world(), portal()
        report, code, text = self.run_links(files, fake, portal=client, seconds_per_get=10,
                                            clock=[0.0], extra=["--budget-seconds", "300"])
        cut = report["incomplete"]
        self.assertEqual((report["ok"], code, report.get("blocked"), report["capture_complete"]),
                         (False, 1, None, False), text[:3000])
        self.assertEqual((cut["reason"], cut["cut"][:3], cut["roots_cut"]),
                         ("budget", ["capture", "portal_mirror", "bc_capture"], 1))
        self.assertEqual((cut["documents_measured"], report["documents"]), (1, 1))
        self.assertLess(report["phase_seconds"]["capture"], 300 + 3 * 10)   # GETs in flight
        self.assertEqual((client.requests_sent, report["bc_capture"]), (0, None))
        self.assertEqual(report["portal_mirror"]["skipped"], "budget")
        self.assertFalse([url for _m, url, _h in fake.calls if "/companies" in url])
        self.assertIn("ok", " ".join(lv.check_expectations(spec, code, report)))
        self.assertEqual(lv._recorded(spec, report)["incomplete"], cut)
        # Reads within budget but a projection that ran past it: the first document is
        # still measured, every other one is counted unmeasured, never a pass.
        clock, build = [0.0], project.build_store

        def slow_build(path, *args, **kwargs):
            built = build(path, *args, **kwargs)
            clock[0] += 10_000 * (Path(path).name == projection_path(Path(".")).name)
            return built
        with mock.patch.object(project, "build_store", slow_build):
            report, code, _ = self.run_links(files, bc_world(), portal=portal(), clock=clock,
                                             extra=["--budget-seconds", "1500"])
        self.assertEqual((report["ok"], code, report["documents"]), (False, 1, 1))
        self.assertEqual((report["incomplete"]["cut"], report["incomplete"]["documents_measured"],
                          report["incomplete"]["documents_unmeasured"]), (["measure"], 1, 4))
        self.assertGreaterEqual(report["links_measured"], 1)
        # Without a cut the same run passes, and a budget that is not a positive number
        # of seconds is refused before any read.
        report, code, _ = self.run_links(files, bc_world(), portal=portal(), clock=[0.0],
                                         extra=["--budget-seconds", "1500"])
        self.assertEqual((report["ok"], code, report["incomplete"]), (True, 0, None))
        for bad in ("0", "-5", "nan", "inf"):
            with self.assertRaises(SystemExit) as stop, \
                    contextlib.redirect_stderr(io.StringIO()):
                live.main(["--links", "--state-dir", "s", "--scope", "x",
                           "--live-audience", "y", "--budget-seconds", bad])
            self.assertEqual(stop.exception.code, 2)

    def test_the_reads_leave_a_projection_over_300_s_its_time_and_measure_every_document(self):
        """QA-04 (live 2026-10-13: reads 1112 s, projection 403.5 s, nothing measured): a
        projection that costs more than the old fixed 300 s reserve, sized from this run's
        own sampled projection, cuts the BC reads before it so that every document is
        measured within the budget; a tighter budget still measures the first."""
        import sqlite3

        from verify import live_verify as lv
        from ontology import project
        from operating.tests.test_attested_missing import pdf
        from scripts import eval_document_fields_live as live
        spec = json.loads(LINKS_SPEC.read_text("utf-8"))
        files = [(f"01S{n}", f"slip {n}.pdf", pdf(slip(f"PO-{1001 + n}"))) for n in range(5)]
        build, served = project.build_store, live._served_links

        def run(budget):
            clock = [0.0]

            def slow_build(path, *args, **kwargs):   # 3 s per projected object
                built = build(path, *args, **kwargs)
                with contextlib.closing(sqlite3.connect(str(path))) as conn:
                    clock[0] += 3 * conn.execute("SELECT count(*) FROM objects").fetchone()[0]
                return built

            def slow_served(*args, **kwargs):   # 1 s per document read for each reader
                clock[0] += 1
                return served(*args, **kwargs)
            with mock.patch.object(project, "build_store", slow_build), \
                    mock.patch.object(live, "_served_links", slow_served):
                report, code, text = self.run_links(
                    files, bc_world(items=64), portal=portal(), clock=clock,
                    seconds_per_get=1, bc_seconds_per_get=5,
                    extra=["--budget-seconds", str(budget)])
            return report, code, text, clock[0]

        report, code, text, ended = run(1000)
        phases = report["phase_seconds"]
        self.assertGreater(phases["project"], 300, text[:3000])
        self.assertEqual((report["ok"], code, report["incomplete"], report["documents"]),
                         (True, 0, None, 5), text[:3000])
        self.assertGreater(phases["measure"], 0)
        self.assertLessEqual(ended, 1000)
        # The BC reads were cut so the projection fits: not every unit was read.
        self.assertFalse(report["bc_capture"]["all_complete"])
        self.assertGreaterEqual(report["reserve"]["bc_slices"], 1)
        self.assertEqual(lv.check_expectations(spec, code, report), [])
        self.assertEqual(lv._recorded(spec, report)["reserve"], report["reserve"])
        # A budget the record reads cannot share: they are cut, never the measurement.
        report, code, text, ended = run(450)
        cut = report["incomplete"] or {"cut": []}
        self.assertNotIn("measure", cut["cut"], text[:3000])
        self.assertEqual((report["documents"], ended <= 450), (5, True))
        # One the SharePoint capture (the documents themselves) and the projection of what
        # it read already spend: still a typed incomplete, with the first document measured.
        report, code, text, _ = run(100)
        cut = report["incomplete"]
        self.assertEqual((report["ok"], code, cut["reason"]), (False, 1, "budget"), text[:3000])
        self.assertEqual(cut["cut"], ["portal_mirror", "bc_capture", "measure"])
        self.assertEqual(cut["documents_measured"], 1)

    def test_the_sample_reads_only_its_rows_and_the_reserve_holds_every_read(self):
        """The reserve's sample decodes only the rows it keeps (a full read scaled by 16
        over-reserved about 3x and starved BC); measured after the last BC slice, its
        estimate holds BC even when BC finishes in one slice, and it reserves each
        document's measurement."""
        import sqlite3

        from integrations import capture_sharepoint as cs
        from ontology import project
        from ontology.project_bc import BcCaptures
        from ontology.schema import load_schema
        from operating.tests.test_attested_missing import pdf
        from scripts import eval_document_fields_live as live
        files = [(f"01S{n}", f"slip {n}.pdf", pdf(slip(f"PO-{1001 + n}"))) for n in range(5)]
        clock, decoded = [0.0], [0]
        build, record = project.build_store, BcCaptures._record

        def slow_build(path, *args, **kwargs):   # 3 s per projected object
            built = build(path, *args, **kwargs)
            with contextlib.closing(sqlite3.connect(str(path))) as conn:
                clock[0] += 3 * conn.execute("SELECT count(*) FROM objects").fetchone()[0]
            return built

        def counted(self, *args):
            decoded[0] += 1
            return record(self, *args)
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            with mock.patch.object(project, "build_store", slow_build):
                report, code, text = self.run_links(files, bc_world(items=64),
                                                    portal=portal(), clock=clock, state=state,
                                                    extra=["--budget-seconds", "100000"])
            # A sample of the BC capture decodes about 1 in 16 of the records a projection
            # of it decodes.
            with mock.patch.object(BcCaptures, "_record", counted), \
                    mock.patch.object(live, "link_outcomes", lambda *a, **k: {}):
                live._estimate(state, load_schema(), set(), "plx", cs.CAPTURE_PRINCIPAL,
                               WITHHELD, lambda: 0.0, {})
                sampled, decoded[0] = decoded[0], 0
                with contextlib.ExitStack() as stack:
                    project.build_store(Path(tmp) / "full.sqlite", load_schema(),
                                        live._captures(state, stack, set()))
        self.assertGreater(decoded[0], 100)
        # (and the rows naming what a slip references: PO-1001 in both companies)
        self.assertLessEqual(sampled, decoded[0] // live.SAMPLE_STRIDE + 2 + 2)
        reserve, phases = report["reserve"], report["phase_seconds"]
        self.assertEqual((report["ok"], code, report["documents"]), (True, 0, 5), text[:3000])
        self.assertEqual((reserve["bc_slices"], reserve["estimates"], reserve.get("failed")),
                         (1, 3, None))
        # The recorded estimate is of what was projected, BC included: neither ~3x over
        # nor short of BC.
        self.assertLess(abs(reserve["projection_estimate_seconds"] - phases["project"]),
                        phases["project"] / 2, (reserve, phases))
        # Every document measured is reserved (here at no measured cost: the clock runs
        # only in the projection).
        self.assertEqual((reserve["documents"], reserve["measure_estimate_seconds"]), (5, 0.0))

    def test_reading_rows_is_reserved_once_so_the_portal_leaves_bc_its_time(self):
        """QA-04 (live 2026-10-15: projection 460 s, reserve 1210 s, BC given 0 s): the
        sample reads every row to pick the ones it keeps, and that read is no part of what
        the kept rows add; scaled by the stride it over-reserved the Portal's rows and cut
        BC. Here each row a projection reads costs time: the estimate stays near the
        projection and BC reads within the spec's budget."""
        from verify import live_verify as lv
        from ontology import project, project_bc
        from operating.tests.test_attested_missing import pdf
        spec = json.loads(LINKS_SPEC.read_text("utf-8"))
        budget = spec["command"][spec["command"].index("--budget-seconds") + 1]
        files = [(f"01S{n}", f"slip {n}.pdf", pdf(slip(f"PO-{1001 + n}"))) for n in range(5)]
        rows = [{"id": f"po-{n}", "po_number": f"PO-{7000 + n}"} for n in range(480)]
        clock, sampled = [0.0], project._sampled

        def costly(sample, read):   # 0.25 s per row a projection or a sample reads
            def each():
                for row in read:
                    clock[0] += 0.25
                    yield row
            return sampled(sample, each())
        with mock.patch.object(project, "_sampled", costly), \
                mock.patch.object(project_bc, "_sampled", costly):
            report, code, text = self.run_links(files, bc_world(items=16),
                                                portal=portal(rows), clock=clock,
                                                bc_seconds_per_get=1,
                                                extra=["--budget-seconds", budget])
        reserve, phases = report["reserve"], report["phase_seconds"]
        self.assertEqual((report["ok"], code, report["incomplete"]), (True, 0, None),
                         text[:3000])
        self.assertGreater(phases["bc_capture"], 0, phases)
        self.assertTrue(report["bc_capture"]["all_complete"], report["bc_capture"])
        self.assertGreater(phases["project"], 120, phases)   # the 480 rows' 120 s at least
        self.assertLess(abs(reserve["projection_estimate_seconds"] - phases["project"]),
                        phases["project"] / 2, (reserve, phases))
        self.assertLessEqual(clock[0], float(budget))
        self.assertEqual(lv.check_expectations(spec, code, report), [])

    def test_each_captured_document_is_reserved_at_its_measured_cost(self):
        """QA-04 (live 2026-10-13: 6 documents captured, the sample projected none, so the
        measurement reserve was 0 s while each document took about 22 s; measure cut): the
        reserve is the documents the capture holds times this run's measured cost of one,
        so the reads are cut, never the measurement."""
        from verify import live_verify as lv
        from operating.tests.test_attested_missing import pdf
        from scripts import eval_document_fields_live as live
        spec = json.loads(LINKS_SPEC.read_text("utf-8"))
        files = [(f"01S{n}", f"slip {n}.pdf", pdf(slip(f"PO-{1001 + n}"))) for n in range(5)]
        clock, served = [0.0], live._served_links

        def slow_served(*args, **kwargs):   # 10 s per reader: 20 s per document measured
            clock[0] += 10
            return served(*args, **kwargs)
        with mock.patch.object(live, "_served_links", slow_served):
            report, code, text = self.run_links(
                files, bc_world(items=64), portal=portal(), clock=clock, seconds_per_get=1,
                bc_seconds_per_get=5, extra=["--budget-seconds", "400"])
        reserve, cut = report["reserve"], report["incomplete"] or {"cut": []}
        self.assertNotIn("measure", cut["cut"], text[:3000])
        self.assertEqual((report["documents"], report["documents_unmeasured"]), (5, 0))
        self.assertLessEqual(clock[0], 400)
        self.assertGreaterEqual(reserve["measure_estimate_seconds"], 5 * 20)
        self.assertTrue(set(cut["cut"]) <= {"portal_mirror", "bc_capture"}, cut)
        self.assertEqual(not lv.check_expectations(spec, code, report), report["ok"])

    def test_the_reserve_is_the_documents_at_the_mean_cost_measured_over_their_records(self):
        """QA-04: the measurement reserve counts the documents the
        run measures (not every captured file), at the mean of a sample of them, each
        measured over the records it links (never one document's cost scaled by the
        stride); every estimate is held with the margin."""
        from verify import live_verify as lv
        from operating.tests.test_attested_missing import pdf
        from scripts import eval_document_fields_live as live
        spec = json.loads(LINKS_SPEC.read_text("utf-8"))
        # PO-1001: a record in each company (2 links), PO-2001: one, PO-9999: none.
        files = [(f"01S{n}", f"slip {n}.pdf", pdf(slip(po))) for n, po in enumerate(
            ["PO-1001", "PO-2001", "PO-9999", "PO-1001", "PO-2001", "PO-9999", "PO-1001"])]
        files.append(("01N0", "notes.txt", b"Minutes of the weekly meeting."))
        clock, served, cost = [0.0], live._served_links, {}

        def slow_served(served_tools, reader, document, link_types):   # 10 s a linked record
            visible, count = served(served_tools, reader, document, link_types)
            cost[document] = max(cost.get(document, 0), 10 * len(visible or ()))   # its reader
            clock[0] += 10 * len(visible or ())
            return visible, count
        with mock.patch.object(live, "_served_links", slow_served):
            report, code, text = self.run_links(
                files, bc_world(items=64), portal=portal(), clock=clock,
                extra=["--budget-seconds", "100000"])
        reserve = report["reserve"]
        self.assertEqual((report["ok"], code, report["documents"]), (True, 0, 7), text[:3000])
        sample = [cost[d] for d in sorted(cost)[:4]]   # live.MEASURE_SAMPLE
        self.assertGreater(len(set(sample)), 1)   # the first document alone is not the mean
        mean = sum(sample) / len(sample)
        self.assertEqual(reserve["measure_seconds_per_document"], round(mean, 1))
        self.assertEqual(reserve["measure_estimate_seconds"], round(7 * mean, 1))
        self.assertEqual((reserve["documents"], live.MEASURE_SAMPLE), (7, 4))
        self.assertGreater(report["capture"]["contents_captured"], reserve["documents"])
        self.assertEqual(lv.check_expectations(spec, code, report), [])
        self.assertEqual(live._allowance(None, 1000.0, lambda: 0.0, {
            "estimate_seconds": 100.0, "projection_estimate_seconds": 100.0,
            "measure_estimate_seconds": 100.0, "failed": None}),
            1000.0 - (1 + live.RESERVE_MARGIN) * 300.0)

    def test_a_projection_a_killed_estimate_set_aside_is_restored_on_the_next_run(self):
        """A run SIGKILLed while an estimate serves its sample at the state's projection
        path never leaves that sample there: the next run starts on the state's own
        projection again."""
        import hashlib
        import subprocess
        import sys

        from integrations import capture_sharepoint as cs
        from operating.host import projection_path
        from operating.tests.test_attested_missing import pdf
        from scripts import eval_document_fields_live as live
        files = [(f"01S{n}", f"slip {n}.pdf", pdf(slip(f"PO-{1001 + n}"))) for n in range(2)]
        args = ["--budget-seconds", "100000"]
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            report, _code, text = self.run_links(files, bc_world(), clock=[0.0], state=state,
                                                 extra=args)
            self.assertTrue(report["ok"], text[:3000])

            def digest():
                path = projection_path(state)
                return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()
            own = digest()
            self.assertTrue(own)
            child = (
                "import os, signal, sys\n"
                "from pathlib import Path\n"
                "from unittest import mock\n"
                "from tests import test_eval_document_fields_live as t\n"
                "from scripts import eval_document_fields_live as live\n"
                "from operating.tests.test_attested_missing import pdf\n"
                "state, outcomes = Path(sys.argv[1]), live.link_outcomes\n"
                "def killed(*a, **k):\n"
                "    if (state / 'links-reserve').exists():\n"
                "        os.kill(os.getpid(), signal.SIGKILL)\n"
                "    return outcomes(*a, **k)\n"
                "files = [(f'01S{n}', f'slip {n}.pdf', pdf(t.slip(f'PO-{1001 + n}')))\n"
                "         for n in range(2)]\n"
                "with mock.patch.object(live, 'link_outcomes', killed):\n"
                "    t.DocumentLinksTests('run_links').run_links(\n"
                "        files, t.bc_world(), clock=[0.0], state=state,\n"
                "        extra=['--budget-seconds', '100000'])\n")
            run = subprocess.run([sys.executable, "-c", child, str(state)], cwd=ROOT,
                                 capture_output=True, text=True, timeout=300)
            self.assertEqual(run.returncode, -9, run.stderr[-3000:])
            self.assertNotEqual(digest(), own)   # killed while its sample was in place
            seen, run_cli = [], cs.run_cli

            def capture(*a, **k):   # the next run's first read: the state's own again
                seen.append(digest())
                return run_cli(*a, **k)
            with mock.patch.object(cs, "run_cli", capture):
                report, _code, text = self.run_links(files, bc_world(), clock=[0.0],
                                                     state=state, extra=args)
            self.assertEqual(seen, [own])
            self.assertTrue(report["ok"], text[:3000])
            self.assertFalse((state / live.RESERVE_DIR).exists())

    def test_a_sample_projection_that_raises_is_a_typed_incomplete_report(self):
        from verify import live_verify as lv
        from ontology import project
        from operating.tests.test_attested_missing import pdf
        spec = json.loads(LINKS_SPEC.read_text("utf-8"))
        build = project.build_store

        def subset_fails(path, *args, **kwargs):   # an error peculiar to the sample
            if "links-reserve" in Path(path).parts:
                raise project.ProjectionError("sample_only_error", "PO-1001")
            return build(path, *args, **kwargs)
        files = [(f"01S{n}", f"slip {n}.pdf", pdf(slip(f"PO-{1001 + n}"))) for n in range(2)]
        client = portal()
        with mock.patch.object(project, "build_store", subset_fails):
            report, code, text = self.run_links(files, bc_world(), portal=client, clock=[0.0],
                                                extra=["--budget-seconds", "1500"])
        cut = report["incomplete"]
        self.assertEqual((report["ok"], code, cut["reason"], cut["cut"]),
                         (False, 1, "sample_projection_failed",
                          ["reserve", "portal_mirror", "bc_capture"]), text[:3000])
        self.assertEqual((report["reserve"]["failed"], report["reserve"]["estimates"]),
                         ("sample_only_error", 0))
        # No read after the failed sample; what was read is still projected and measured.
        self.assertEqual((client.requests_sent, report["bc_capture"]), (0, None))
        self.assertEqual(cut["documents_measured"], 2)
        self.assertNotIn("PO-1001", text)
        self.assertIn("ok", " ".join(lv.check_expectations(spec, code, report)))
        self.assertEqual(lv._recorded(spec, report)["incomplete"], cut)

    def test_a_resumed_run_keeps_the_bc_session_its_paused_units_began_in(self):
        """A run on a reused state whose BC an earlier run left paused resumes those
        units as that run's session (never stale); once BC finished the next run starts a
        new session."""
        from datetime import datetime, timedelta, timezone

        from operating.tests.test_attested_missing import pdf
        from scripts import eval_document_fields_live as live
        files = [("01S0", "slip 0.pdf", pdf(slip("PO-1001")))]
        hours = [0]

        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(hours=hours[0])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(live, "datetime", Later):
            state = Path(tmp) / "state"
            report, _, text = self.run_links(files, bc_world(items=64), clock=[0.0],
                                             bc_seconds_per_get=5, state=state,
                                             extra=["--bc-budget-seconds", "20"])
            self.assertFalse(report["bc_capture"]["all_complete"], text[:3000])
            hours[0] = 2   # the next daily run, within FRESH_SINCE_HOURS
            report, _, text = self.run_links(files, bc_world(items=64), clock=[0.0],
                                             state=state)
            self.assertTrue(report["bc_capture"]["all_complete"], text[:3000])
            # Finished: the next run on this state starts a new session.
            self.assertFalse((state / live.FRESH_SINCE_FILE).exists())


if __name__ == "__main__":
    unittest.main()
