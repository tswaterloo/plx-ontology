"""plx.investigate family ``landed_cost`` rooted at a Business Central purchase order.

A BC order's lines carry their price as ``directUnitCost`` (``PurchaseOrderLine.
bc_direct_unit_cost``, finance), never the Portal's ``unit_price``: the plan reads either,
so a BC-rooted line is priced, not UNPRICED_LINE. The price carries ``purchasing_prices``
(owner decision 2026-10-27): a purchasing reader without the finance marking reads it, but
not the header's shipping estimate (finance); a reader holding neither gets the price named
withheld (``marking_withheld``, UNIT_PRICE_WITHHELD), never ``not_carried`` or UNPRICED_LINE,
and no amount anywhere in its answer. The order's Portal record names the
BC order (``custom_fields.renumberedTo``), so the twin is reached from the BC root only in
reverse; its header shipping estimate is read and, the twin being a lead, named
PO_HEADER_ON_UNCONFIRMED_TWIN rather than used.

Projected fixture: a synthetic Portal world mirrored through the GET-only reporting client
and BC captured through the GET-only BC client, both with an injected transport, projected by
the one projector (``scripts.eval_unfamiliar_sample.build_projection``). Every output is
validated against the tool contract. Evidence label: injected-transport (synthetic rows,
never live PLX data).
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re
import tempfile
import unittest

from ontology import store_ddl
from ontology.json_contract import validate
from ontology.types import Actor
from operating import ontology_tools as ot
from operating import ontology_tools_investigate as oi
from operating import ontology_tools_traverse as tt

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = json.loads((ROOT / "operating/ontology_tool_contracts.json").read_text("utf-8"))
AT = "2099-01-02T04:00:00Z"
UPDATED = "2099-01-01T12:00:00Z"
NUMBER = "PO300077"      # the BC order's own number
PORTAL_NUMBER = "PO300076"   # its Portal record's number (renumbered to the BC No.)
PRICES = ("2.75", "4.25")
SHIPPING = "191.5"


def _world():
    from evals.oracle.fixture_world import World, bc_record
    order = bc_record("purchaseOrders", 0, 830_001, number=NUMBER, vendorNumber="FXV001",
                      currencyCode="USD", orderDate="2098-12-01", fullyReceived=False)
    lines = [bc_record("purchaseOrderLines", 0, 8_300_010 + n, documentId=order["id"],
                       sequence=10000 * n, lineType="Item", lineObjectNumber=f"FX-{n}",
                       quantity=100.0, receivedQuantity=0.0, invoicedQuantity=0.0,
                       directUnitCost=float(price), unitOfMeasureCode="PCS")
             for n, price in enumerate(PRICES, start=1)]
    relations = {
        "public.app_supplier": [{"id": "fx-s1", "code": "FXS1", "name": "FX Supplier",
                                 "currency": "USD", "updated_at": UPDATED}],
        "public.app_purchase_order": [{
            "id": "fx-po-bc", "po_number": PORTAL_NUMBER, "status": "SUBMITTED",
            "supplier_id": "fx-s1", "currency": "USD", "shipping_cost": SHIPPING,
            "duties_cost": None, "incoterms": "FOB",
            "custom_fields": {"renumberedTo": NUMBER, "source": "etl-silver-to-gold"}, "updated_at": UPDATED}],
        "public.app_purchase_order_line": [],
        "public.app_receipt": [],
    }
    return World(relations, bc={"purchaseOrders": [order]}, bc_lines={order["id"]: lines})


class BcRootedLandedCost(unittest.TestCase):
    PRINCIPAL = "eval:dev-baseline"

    @classmethod
    def setUpClass(cls) -> None:
        from integrations.tests.test_live_source_adapters import ENV
        from integrations.workbench_read_api import WorkbenchReadApiClient
        from integrations.live_source_guard import LiveSourceAudience
        from operating.case_runtime import CaseRuntime, LiveReaders
        from scripts import eval_dev_baseline as script
        from scripts import eval_unfamiliar_sample as ev
        from tests.test_eval_unfamiliar_sample import fixture_bc_sender
        world = _world()
        fake, books = fixture_bc_sender(world)
        client = WorkbenchReadApiClient(script.FIXTURE_URL, script.FIXTURE_KEY,
                                        fetch=ev._fixture_fetch(world))
        cls.tmp = tempfile.TemporaryDirectory(prefix="plx-landed-bc-")
        # The BC capture where the case runtime reads a bc:// citation back (the host's).
        work = Path(cls.tmp.name) / "w"
        (work / "state" / "bc-capture").mkdir(parents=True)
        cls.product = script.build_product(
            client, work, sorted(world.relations), budget_seconds=None,
            bc_db=work / "state" / "bc-capture" / "capture.sqlite",
            clock=script.fixture_clock, bc_capture=lambda db, seconds: ev.capture_bc(
                ENV, db, seconds, sender=fake, books=books, host_tenant=script.TENANT))
        # The claim gate re-reads a bc:// citation from that capture under the live audience.
        cls.runtime = CaseRuntime(cls.product.state_dir, client=None,
                                  audience=cls.product.audience, tenant=script.TENANT,
                                  live=LiveReaders(LiveSourceAudience(
                                      {"bc:production:*": [cls.PRINCIPAL]},
                                      tenant=script.TENANT)))
        cls.actor = Actor(script.TENANT, frozenset({cls.PRINCIPAL}), "restricted")
        reader = ot.StoreReader.open(cls.product.store)
        with closing(reader.conn):
            [cls.root] = [r[0] for r in reader.conn.execute(
                "SELECT object_id FROM objects WHERE object_id LIKE "
                "'PurchaseOrder:bc.purchase_orders:%'")]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.runtime.close()
        cls.tmp.cleanup()

    def investigate(self, markings) -> dict:
        reader = ot.StoreReader.open(self.product.store)
        self.addCleanup(reader.conn.close)
        who = {self.PRINCIPAL}
        policy = ot.ReadPolicy(tenant="plx",
                               audiences={k: set(who) for k in reader.audience_keys()},
                               markings={m: set(who) for m in markings})
        base = ot.OntologyTools(reader, policy, schema=ot.default_schema(),
                                grammar=ot.default_grammar())
        table: dict = {}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        oi.register(table, oi.InvestigateTools(
            table, rows=oi.citation_rows(self.runtime), scope=oi.read_scope(base),
            held_markings=lambda actor: policy.held_markings(actor)))
        result = table["plx.investigate"]["handler"](
            {"family": "landed_cost", "root": self.root, "as_known_at": AT},
            self.actor)
        self.assertEqual(validate(result, CONTRACTS["tools"]["plx.investigate"]["output"],
                                  root=CONTRACTS), [])
        self.assertEqual(result["root"]["object_id"], self.root)
        return result

    def test_bc_lines_are_priced_and_the_portal_twin_header_is_read(self):
        result = self.investigate(store_ddl.MARKINGS)
        prices = [e for e in result["answer"] if e["kind"] == "property"
                  and e.get("property") == "bc_direct_unit_cost"]
        self.assertEqual(sorted(str(e["value"]) for e in prices), sorted(PRICES))
        self.assertEqual({e["object_type"] for e in prices}, {"PurchaseOrderLine"})
        reasons = {g["reason"] for g in result["gaps"]}
        self.assertFalse(reasons & {"unpriced_line", "not_carried", "marking_withheld",
                                    "unit_price_withheld"}, reasons)
        # The Portal record that names this BC order is reached in reverse. The projector
        # caps that twin at lead (the Portal row names no BC company), so its shipping
        # estimate is read, cited and named as resting on an unconfirmed twin: never used
        # for an amount, never reported as absent from the order's header alone.
        freight = [e for e in result["answer"] if e.get("property") == "shipping_cost_estimate"]
        self.assertEqual([str(e["value"]) for e in freight], [SHIPPING])
        self.assertTrue(freight[0]["object_id"].startswith(
            "PurchaseOrder:portal.app_purchase_order:"))
        self.assertTrue(freight[0]["lead_basis"])
        self.assertIn("purchase order freight_estimate",
                      {g["subject"] for g in result["gaps"]
                       if g["reason"] == "po_header_on_unconfirmed_twin"})
        computed = {e["name"]: e for e in result["answer"] if e["kind"] == "computed"}
        self.assertIsNone(computed["landed_cost_estimate"]["value"])

    def test_a_purchasing_reader_reads_the_bc_prices_but_not_the_header_estimate(self):
        result = self.investigate([m for m in store_ddl.MARKINGS if m != "finance"])
        prices = [e for e in result["answer"] if e["kind"] == "property"
                  and e.get("property") == "bc_direct_unit_cost"]
        self.assertEqual(sorted(str(e["value"]) for e in prices), sorted(PRICES))
        reasons = {g["reason"] for g in result["gaps"]}
        self.assertFalse(reasons & {"unpriced_line", "not_carried", "unit_price_withheld"},
                         reasons)
        self.assertFalse([e for e in result["answer"]
                          if e.get("property") == "shipping_cost_estimate"])
        text = json.dumps(result)
        self.assertIsNone(re.search(rf"(?<![0-9A-Za-z.]){re.escape(SHIPPING)}(?![0-9A-Za-z])",
                                    text))
        # The receipt actuals stay finance: named withheld, never absent.
        self.assertIn("receipt_actual_withheld", reasons)

    def test_a_reader_with_neither_marking_gets_the_price_named_withheld_never_missing(self):
        result = self.investigate([m for m in store_ddl.MARKINGS
                                   if m not in ("finance", "purchasing_prices")])
        reasons = {g["reason"] for g in result["gaps"]}
        self.assertFalse(reasons & {"unpriced_line", "not_carried"}, reasons)
        [withheld] = [g for g in result["gaps"] if g["reason"] == "marking_withheld"
                      and g["subject"].startswith("PurchaseOrderLine.unit_price")]
        self.assertEqual(withheld["state"], "inaccessible")
        self.assertIn("purchasing_prices marking, which this reader does not hold",
                      withheld["detail"])
        [goods] = [g for g in result["gaps"] if g["reason"] == "unit_price_withheld"]
        self.assertEqual(goods["state"], "inaccessible")
        self.assertEqual(goods["who_can_answer"], ["FIN"])   # who holds the marking
        [lines] = [g for g in result["gaps"] if g["reason"] == "po_lines_incomplete"
                   and g["subject"] == "purchase order lines"]
        self.assertIn("the unit price carries the purchasing_prices marking", lines["detail"])
        computed = [e for e in result["answer"] if e["kind"] == "computed"]
        self.assertEqual({e["value"] for e in computed}, {None})
        text = json.dumps(result)
        for amount in (*PRICES, SHIPPING, "275", "425"):
            self.assertIsNone(re.search(
                rf"(?<![0-9A-Za-z.]){re.escape(amount)}(?![0-9A-Za-z])", text), amount)


if __name__ == "__main__":
    unittest.main()
