"""The BC capture CLI writing into a host's BC store (host ingest mode), injected transport.

Reproduced 2026-10-26 on the shared state: every ingest-mode cycle failed BC with a bare
``CaptureError``. The host's BC store is bound to the host capture audience's tenant
(``operating.host`` ``_bc``), while ``python3 -m integrations.bc_read capture`` bound it
to a service audience of the BC tenant itself, so ``CaptureStore`` refused the run
(``binding_mismatch``) and the CLI printed no code. No network, no credentials: every
request goes to ``test_bc_capture.FakeBc``. Evidence label: ``injected-transport``.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote

from integrations import bc_read
from integrations.capture_store import CaptureError, CaptureStore
from integrations.live_source_guard import BoundedBackoff, LiveSourceAudience
from integrations.tests.test_bc_capture import (MY_COMPANY, OTHERS, PRODUCTION_NAMES, FakeBc)
from integrations.tests.test_live_source_adapters import ENV

ROOT = Path(__file__).resolve().parents[2]
HOST_AUDIENCE = ROOT / "examples" / "ingest_all.capture-audience.json"
HOST_PRINCIPAL = "svc:workbench-host"


def run_cli(argv: list[str], fake: FakeBc) -> tuple[int, dict, dict]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = bc_read.main(argv, environ=ENV, sender=fake,
                            retry=BoundedBackoff(sleep=lambda s: None))
    lines = [line for line in err.getvalue().splitlines() if line.strip()]
    return (code, json.loads(out.getvalue()) if out.getvalue().strip() else {},
            json.loads(lines[-1]) if lines else {})


class HostStoreBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.db = Path(scratch.name) / "bc-capture" / "capture.sqlite"
        self.db.parent.mkdir()
        audience = LiveSourceAudience.from_file(HOST_AUDIENCE)
        CaptureStore(self.db, audience).close()   # the host made it: bound to its tenant
        self.fake = FakeBc({"purchaseOrders": 4}, companies=[OTHERS[0], MY_COMPANY],
                           names=PRODUCTION_NAMES)
        self.base = ["capture", "--db", str(self.db), "--entity-set", "purchaseOrders",
                     "--resolve-sample", "20"]

    def test_without_the_host_audience_the_refusal_names_its_code(self) -> None:
        code, report, error = run_cli(self.base, self.fake)
        self.assertEqual(code, 1)
        self.assertEqual(report, {})
        self.assertEqual((error.get("error"), error.get("error_code")),
                         ("CaptureError", "binding_mismatch"))

    def test_as_the_host_principal_it_captures_orders_and_page_50_references(self) -> None:
        code, report, _ = run_cli([*self.base, "--live-audience", str(HOST_AUDIENCE),
                                   "--principal", HOST_PRINCIPAL], self.fake)
        self.assertEqual(code, 0)
        self.assertTrue(report["all_complete"])
        self.assertEqual(report["books"], ["manufacturing"])
        self.assertTrue(report["resolve_all_ok"])
        # Page 50 PurchaseOrder read once for My Company, at BC's page ceiling, whatever
        # the record page size of the call
        page50 = [(url, headers) for method, url, headers in self.fake.calls
                  if "/ODataV4/" in unquote(url)]
        self.assertEqual(len(page50), 1)
        self.assertIn("Company('My Company')/PurchaseOrder", unquote(page50[0][0]))
        self.assertEqual(page50[0][1].get("Prefer"), "odata.maxpagesize=1000")
        # the references are held in the host's store, readable as the host reads it
        audience = LiveSourceAudience.from_file(HOST_AUDIENCE)
        with CaptureStore(self.db, audience) as store:
            from ontology.core import Actor
            host = Actor(audience.tenant, frozenset({HOST_PRINCIPAL}), "restricted")
            unit = f"bc:{MY_COMPANY}/{bc_read.REFERENCE_SET}"
            records = store.records(host, "bc", unit, limit=100)["items"]
            self.assertEqual(len(records), 4)
            self.assertEqual({tuple(sorted(r["row"])) for r in records},
                             {("Buy_from_Vendor_No", "No", "Your_Reference")})
            outsider = Actor(audience.tenant, frozenset({"staff:nobody"}), "restricted")
            with self.assertRaises(CaptureError) as refused:
                store.records(outsider, "bc", unit, limit=1)
            self.assertEqual(refused.exception.code, "not_visible")

    def test_an_audience_without_its_principal_is_refused_before_any_request(self) -> None:
        for extra in (["--live-audience", str(HOST_AUDIENCE)], ["--principal", HOST_PRINCIPAL]):
            code, report, error = run_cli([*self.base, *extra], self.fake)
            self.assertEqual((code, report, error.get("error")), (1, {}, "ValueError"))
        self.assertEqual(self.fake.calls, [])

    def test_a_principal_the_audience_does_not_grant_reads_nothing(self) -> None:
        code, report, error = run_cli([*self.base, "--live-audience", str(HOST_AUDIENCE),
                                       "--principal", "svc:bc-capture"], self.fake)
        self.assertEqual((code, report, error.get("error")), (1, {}, "NotVisible"))
        self.assertEqual(self.fake.calls, [])

if __name__ == "__main__":
    unittest.main()
