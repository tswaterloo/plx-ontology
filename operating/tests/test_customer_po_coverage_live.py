"""The ``case.customer_po_coverage`` live-check spec run over injected sources.

Evidence label: ``injected-transport``. Kept apart from ``test_customer_po_coverage``
(whose fixtures other tests import): this module reads the live-check specs, so the
spec's test selection runs it without pulling in every
importer of those fixtures.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
from operating import customer_po_coverage as cpo

ROOT = Path(__file__).resolve().parents[2]
# Defined here, not imported: the decorator below holds a spec-reading class.
_PDF = importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab")


def _refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


@unittest.skipUnless(_PDF, "the customer PO is a PDF: needs pypdf and reportlab")
class DocumentCarrierLiveCheckTests(unittest.TestCase):
    """live check ``case.customer_po_coverage`` over injected sources: its
    own command, its host config copied with the in-process FakeWorkbench (the order
    fixture plus order OR1000888001 holding no PO) and a FakeDrive for the PLXCUSTOMERS
    site whose customer PO names it; the network refused. Evidence: injected-transport."""

    SPEC_ID = "case.customer_po_coverage"

    def test_the_spec_counts_a_document_po_portal_lacks_and_leaks_no_value(self):
        from verify import live_verify as lv
        from integrations.tests import test_capture_sharepoint as tsp
        from operating.tests import order_fixture as fixture
        from operating.tests.test_case_workspace_live import _customer_drive

        for target in ("socket.socket.connect", "socket.create_connection"):
            patcher = patch(target, _refuse_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        spec = json.loads((ROOT / "devloop" / "live-checks" / f"{self.SPEC_ID}.json")
                          .read_text("utf-8"))
        self.assertEqual(lv.validate_spec(spec, stem=self.SPEC_ID), [])
        fake = FakeWorkbench()
        fake.catalog = json.loads(json.dumps(fixture.CATALOG))
        fake.tables = fixture.tables()
        fake.tables["public.app_sales_order"].append(
            fixture._so("so-m", "OR1000888001", "cust-1", "CONFIRMED", "28"))
        config = json.loads((ROOT / "examples" / "customer_po_coverage_host.live-check.json")
                            .read_text("utf-8"))
        for name in ("capture_audience", "live_audience", "mirror_audience"):
            config[name] = str(ROOT / "examples" / config[name])
        config["sharepoint"]["scope"] = str(ROOT / "examples" / config["sharepoint"]["scope"])
        (root / "host.json").write_text(json.dumps(config), "utf-8")
        argv = [str(root / "host.json") if arg.endswith("_host.live-check.json")
                else str(ROOT / arg) if arg.startswith("examples/")
                else arg for arg in spec["command"][3:]]

        def run(drive, tmp):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cpo.main([arg.replace("{tmp}", str(root / tmp)) for arg in argv],
                                environ={**tsp.ENV, "PLX_WORKBENCH_API_URL": BASE,
                                         "PLX_WORKBENCH_API_KEY": KEY},
                                host_options={"sender": drive, "workbench_fetch": fake,
                                              "sleep": lambda _s: None})
            self.assertTrue(all(method == "GET" for method, _url in drive.calls))
            return code, out.getvalue()

        code, text = run(_customer_drive(), "run")
        report = json.loads(text)
        self.assertEqual(lv.check_expectations(spec, code, report), [], text[-3000:])
        carriers = report["document_carriers"]
        self.assertEqual(carriers["by_status"],
                         {"missing_in_portal": 1, "conflict": 0, "document_blank": 0})
        self.assertEqual(carriers["by_carrier_status"], {
            "SalesOrder.customer_po vs CustomerPoDocument.po_number":
                {"missing_in_portal": 1}})
        self.assertIn("document_carriers", json.dumps(lv._recorded(spec, report)))
        for value in ("4500000001", "OR1000888001", "so-m", "sharepoint://", "api://",
                      "graph-mail://", KEY):
            self.assertNotIn(value, text)
        # A document source that could not be captured is never a clean zero.
        code, text = run(tsp.FakeDrive(), "unreachable")
        report = json.loads(text)
        self.assertEqual(report["document_carriers"]["state"], "read_incomplete")
        self.assertNotEqual(lv.check_expectations(spec, code, report), [])


if __name__ == "__main__":
    unittest.main()
