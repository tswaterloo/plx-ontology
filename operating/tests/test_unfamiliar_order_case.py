"""An unfamiliar sales order or purchase order opened through the generic case tools.

The per-kind ``operating/order_case_script.py`` is retired (task allowlist-3). This module
keeps its gated path and now drives the replacement: ``operating.ingestion`` fills a state
directory's source mirror from the in-process ``FakeWorkbench`` (``order_fixture``: two
different sales orders and two different received purchase orders plus decoys, so nothing is
keyed to one case), the BC capture (``integrations.tests.test_bc_capture.Harness`` over an
in-process ``FakeBc``) holds synthetic posted sales invoices, and
``operating.case_workspace_live.CaseOpener`` opens every case offline through ``plx.find``,
``plx.object``, ``plx.around`` and ``plx.investigate``. The invoiced-order pick of the
retired script is the join pick ``pick=SalesInvoice>SalesOrder``: the root reasons it
recorded (``no_portal_row_for_external_document_number``, ``not_in_served_replica``,
...) are the join's typed reasons.

Evidence label: injected-transport (no network, no real PLX data).
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

from integrations.live_source_guard import LiveSourceAudience
from integrations.source_mirror import AUDIENCE_SCHEMA, AudienceMap
from integrations.tests import test_bc_capture as tb
from integrations.tests.test_source_mirror import BASE, KEY, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.core import Actor
from ontology.tests.test_project_bc import CAPTURE_ENTITY_SETS, gid, record
from operating import case_workspace_live as live_ws
from integrations.bc_read import load_company_books
from operating.case_runtime import CaseRuntime, LiveReaders, build_live_readers
from operating.ingestion import Ingestor, load_sources
from operating.tests import order_fixture as fixture

ROOT = Path(__file__).resolve().parents[2]
OPS, SALES, PURCHASING, INGEST = ("staff:operations", "staff:sales", "staff:purchasing",
                                  "svc:ingest")
INVESTIGATOR = Actor("plx", frozenset({OPS, SALES, PURCHASING}))
JOIN = "SalesInvoice>SalesOrder"
NO_BC = {"reader": False, "orders": 0, "orders_unreadable": 0, "requests": 0, "lookups": 0,
         "lookup_states": {}, "hits": 0, "scanned": 0}  # no live BC reader: no lookup


def invoice(n: int, external: str | None, day: str, customer: str = "C0001",
            order: str | None = None) -> dict:
    """A posted BC sales invoice: ``order`` is the BC sales order it was posted from
    (orderNumber); without one it is a direct invoice, as the Portal's push made them."""
    fields = {"externalDocumentNumber": external} if external is not None else {}
    fields.update({"orderNumber": order} if order is not None else {})
    return record(gid(1, 12, n), number=f"PSI-{n}", customerNumber=customer, status="Open",
                  postingDate=f"2026-09-{day}", invoiceDate=f"2026-09-{day}",
                  lastModifiedDateTime=f"2026-09-{day}T10:00:00Z", **fields)


# Portal-pushed invoices (no BC order; the Portal order number in externalDocumentNumber),
# newest first: a BC-numbered reference, a UAT-prefixed one, an unknown Portal-shaped one,
# then the invoice that names order A.
INVOICES = [invoice(1, "S-ORD10045", "28"), invoice(2, "UAT-SO100018", "27"),
            invoice(3, "SO100999", "26"), invoice(4, "SO100201", "21")]


class LookupBc(tb.FakeBc):
    """``FakeBc`` that also answers the point lookups of the live readers
    (``$filter=<field> eq '<value>'``), as BC does."""

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        parts = urlsplit(url)
        found = re.fullmatch(r"(\w+) eq '(.*)'", (parse_qs(parts.query).get("$filter")
                                                   or [""])[0])
        target = re.fullmatch(re.escape(tb.BASE) + r"/companies\(([^)]+)\)/(\w+)",
                              unquote(parts.path))
        if found and target and found[1] != "documentId":
            self.calls.append((method, url, dict(headers)))
            rows = [r for r in self.data.get((target[1], target[2]), [])
                    if str(r.get(found[1], "")).lower() == found[2].lower()]
            return 200, {}, self.body({"value": rows})
        return super().__call__(method, url, headers, body, timeout, max_bytes)


def bc_world(invoices=INVOICES, customers=()) -> LookupBc:
    """BC holding ``invoices`` and the customer cards ``customers`` ((number, name) pairs)
    in the first company."""
    fake = LookupBc({name: 0 for name in CAPTURE_ENTITY_SETS}, companies=tb.CO)
    fake.data[(tb.CO[0], "salesInvoices")] = [dict(row) for row in invoices]
    fake.data[(tb.CO[0], "customers")] = [
        record(gid(1, 14, n), number=number, displayName=name)
        for n, (number, name) in enumerate(customers, 1)]
    # The invoice that joins order A has one line: a record one hop from the join's
    # start, so only a lead to the case too.
    for row in invoices:
        if row.get("externalDocumentNumber") == "SO100201":
            fake.lines[(tb.CO[0], "salesInvoices", row["id"])] = [record(
                gid(1, 13, 1), documentId=row["id"], sequence=10000, lineType="Item",
                lineObjectNumber="ITEM-A", quantity="#7", unitOfMeasureCode="EA",
                unitPrice="#2.00", amountExcludingTax="#14.00")]
    return fake


class OrderWorld:
    """Order fixture rows ingested into a fresh state directory (and, with ``invoices``,
    a BC capture of those sales invoices), opened offline per investigator."""

    def __init__(self, test: unittest.TestCase, *, grants: dict | None = None,
                 tables: dict | None = None, invoices=None, outage: bool = False,
                 bc_readers=(OPS, SALES, PURCHASING), customers=(), live_bc=None) -> None:
        temp = tempfile.TemporaryDirectory()
        test.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.state = self.dir / "state"
        self.fake = FakeWorkbench()
        self.fake.catalog = json.loads(json.dumps(fixture.CATALOG))
        self.fake.tables = fixture.tables()
        self.fake.tables.update(tables or {})
        audiences = {name: sorted({*principals, INGEST}) for name, principals in
                     {**fixture.AUDIENCES, **(grants or {})}.items()}
        self.audience = self.dir / "audience.json"
        self.audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA, "tenant": "plx",
                                             "audiences": audiences}), "utf-8")
        _tenant, actor, sources = load_sources({
            "schema": "plx-ingest-sources/v1", "tenant": "plx",
            "actor": {"principals": [INGEST], "clearance": "internal"},
            "sources": [{"id": "portal." + name.replace("_", "-").replace(".", "-"),
                         "kind": "portal_relation", "relation": name}
                        for name in sorted(audiences)]}, {})

        def portal_down(url, headers, *, max_bytes):
            from integrations.http import SourceHttpError
            raise SourceHttpError("HTTP 500", status=500, error_code="internal")

        # A Portal outage: the second ingestion run answers HTTP 500, so the mirror holds
        # the first run's (now stale) rows.
        for fetch in [self.fake] + ([portal_down] if outage else []):
            filler = CaseRuntime(self.state, client=WorkbenchReadApiClient(BASE, KEY,
                                                                           fetch=fetch),
                                 audience=self.audience, tenant="plx",
                                 mirror_options={"retry_delay_seconds": 0})
            ingestor = Ingestor(self.state, sources, actor=actor, mirror=filler.mirror)
            try:
                with mock.patch("time.sleep"):
                    ingestor.run()
            finally:
                ingestor.close()
                filler.close()
        if invoices is not None:
            (self.state / "bc-capture").mkdir(parents=True)
            tb.Harness(test, bc_world(invoices, customers), db=self.state / "bc-capture" /
                       "capture.sqlite").run(entity_sets=CAPTURE_ENTITY_SETS)
        live_audience = LiveSourceAudience({"bc:production:*": list(bc_readers)},
                                           tenant="plx", default_classification="internal")
        # ``live_bc``: the BC the live reader reaches (GET only, in-process), which may
        # hold records the capture above never read.
        self.live_bc = live_bc
        live = LiveReaders(audience=live_audience) if live_bc is None else \
            build_live_readers(live_audience, tb.ENV, bc_sender=live_bc)
        self.runtime = live_ws.offline_runtime(self.state, self.audience, "plx", live=live)
        test.addCleanup(self.runtime.close)

    def opener(self, actor: Actor = INVESTIGATOR) -> live_ws.CaseOpener:
        # A live BC lookup pins the fixture companies, as the host config's books do.
        books = {} if self.live_bc is None else {"bc_books": load_company_books({
            "schema": "plx-bc-company-books/v1", "environments": tb.BOOKS})}
        return live_ws.CaseOpener(self.runtime, actor, self.state,
                                  audience=AudienceMap.from_file(self.audience), **books)

    def view(self, case_id: str, actor: Actor = INVESTIGATOR) -> dict | None:
        return live_ws.workspace(self.runtime, actor, case_id)


def claim_failures(world: OrderWorld, case_id: str) -> list[tuple[str, str]]:
    """Claim-gate failures of a case other than a selected or computed answer's number
    (which no cited field holds; the gate reports it on read)."""
    from operating.mcp_server import MCPServer, local_client

    call = local_client(MCPServer(case_runtime=world.runtime, actor=INVESTIGATOR), "gate")
    claims = call("plx.case_get", {"case_id": case_id, "section": "claims"})
    by_id = {item["id"]: item for item in findings(world.runtime.cases.get(INVESTIGATOR,
                                                                         case_id))}
    return [(item["item"], claim.get("code")) for item in claims["items"]
            for claim in item["claims"] if claim["status"] != "passed"
            and not (claim.get("code") == "claim_unstructured" and item["item"] in by_id
                     and by_id[item["item"]]["kind"] == "interpretation"
                     and re.search(r"\((select|computed)\b", by_id[item["item"]]["statement"]))]


def rows(view: dict) -> set[tuple[str, str]]:
    return {(record["relation"], str((record.get("key") or {}).get("id")))
            for record in view["records"]}


def findings(case: dict) -> list[dict]:
    return [item for bucket in case["findings"].values() for item in bucket]


CARDS = (("C0001", "Customer One"), ("C0002", "Customer Two"))
PORTAL_CUSTOMER = 'Customer:portal.app_customer:{"id":"cust-1"}'


def native(external: str, customer: str = "C0001") -> dict:
    """A BC-native invoice (posted from a BC sales order), so its externalDocumentNumber
    is the order's customer PO."""
    return invoice(7, external, "29", customer=customer, order="S-ORD10077")


def customer_link(opener: live_ws.CaseOpener, tier: str) -> None:
    """Set the one projected same_record_as link of the Portal's cust-1 and BC customer
    C0001 to ``tier`` (``reviewed``: as ``ontology.reviews.link_step`` writes a person's
    accepted link, with its reviewer, time and review id)."""
    import sqlite3

    # The stored-key link; the company-name candidate the projector also writes for the
    # pair is a separate lead this case never decides.
    match = ("link_type LIKE '%same_record_as' AND binding_kind <> 'text_match' "
             "AND ? IN (from_object, to_object) "
             "AND 'Customer:bc.customers:' IN (substr(from_object, 1, 22), "
             "substr(to_object, 1, 22))")
    db = sqlite3.connect(opener.projection())
    try:
        # links is a view (store v6): SQLite counts no change made by its INSTEAD OF
        # trigger, so the one link is counted before and its outcome read back after.
        found = db.execute(f"SELECT count(*) FROM links WHERE {match}",
                           (PORTAL_CUSTOMER,)).fetchone()[0]
        with db:
            db.execute("UPDATE links SET tier = ?, reviewed_by = 'employee:reviewer', "
                       "reviewed_at = '2026-10-01T00:00:00Z', review_action_id = 'rev-1' "
                       f"WHERE {match}", (tier, PORTAL_CUSTOMER))
        changed = db.execute(f"SELECT count(*) FROM links WHERE {match} AND tier = ? AND "
                             "review_action_id = 'rev-1'", (PORTAL_CUSTOMER, tier)).fetchone()[0]
    finally:
        db.close()
    assert (found, changed) == (1, 1), (found, changed)


def spec_main(args, fetch=None, sender=None, environ=None) -> tuple[int, str]:
    """``case.unfamiliar_order``'s command arguments (after ``-m <module>``) run in-process
    over the order fixture's ``FakeWorkbench`` (or ``fetch``) and ``bc_world()`` (or
    ``sender``), its host config copied with a books map that pins the fixture companies:
    the exit code and stdout. ``environ`` None: ``os.environ``, as the verifier runs it."""
    fake = FakeWorkbench()
    fake.catalog = json.loads(json.dumps(fixture.CATALOG))
    fake.tables = fixture.tables()
    with tempfile.TemporaryDirectory() as temp:
        config = json.loads((ROOT / "examples" / "unfamiliar_case_host.live-check.json")
                            .read_text("utf-8"))
        for key in ("capture_audience", "live_audience", "mirror_audience"):
            config[key] = str(ROOT / "examples" / config[key])
        (Path(temp) / "books.json").write_text(json.dumps({
            "schema": "plx-bc-company-books/v1", "environments": tb.BOOKS}), "utf-8")
        config["bc"]["books"] = "books.json"
        (Path(temp) / "host.json").write_text(json.dumps(config), "utf-8")
        argv = [str(Path(temp) / "host.json") if arg.endswith("_host.live-check.json")
                else str(ROOT / arg) if arg.startswith("examples/")
                else arg.replace("{tmp}", temp) for arg in args]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = live_ws.main(argv, environ=environ,
                                host_options={"sender": sender or bc_world(),
                                              "workbench_fetch": fetch or fake,
                                              "sleep": lambda _s: None})
    return code, out.getvalue()


class OrderCaseGenericTest(unittest.TestCase):
    def test_two_different_orders_each_reconstruct_their_own_chain(self):
        world = OrderWorld(self)
        opener = world.opener()
        cases = {number: opener.open_case("sales_order", number)
                 for number in ("SO100201", "SO100305")}
        a, b = (rows(world.view(cases[n]["case_id"])) for n in ("SO100201", "SO100305"))
        self.assertTrue({("public.app_sales_order", "so-a"),
                         ("public.app_sales_order_line", "sol-1"),
                         ("public.app_sales_order_line", "sol-2"),
                         ("public.app_shipment", "sh-1")} <= a, a)
        self.assertTrue({("public.app_sales_order", "so-b"),
                         ("public.app_sales_order_line", "sol-3"),
                         ("public.app_shipment", "sh-2")} <= b, b)
        self.assertFalse({row for row in a if row[1] in ("so-b", "sol-3", "sh-2")})
        self.assertFalse({row for row in b if row[1] in ("so-a", "sol-1", "sol-2", "sh-1")})
        # The same for two received purchase orders.
        po = {number: rows(world.view(opener.open_case("petra_po", number)["case_id"]))
              for number in ("PO100777", "PO100888")}
        self.assertIn(("public.app_purchase_order_line", "pol-1"), po["PO100777"])
        self.assertIn(("public.app_purchase_order_line", "pol-2"), po["PO100888"])
        self.assertFalse({r for r in po["PO100777"] if r[1] in ("po-b", "pol-2", "rc-1")})
        self.assertFalse({r for r in po["PO100888"] if r[1] in ("po-a", "pol-1", "3301")})
        # Quantities in two units are never added up: each line's quantity is its own
        # cited fact with its own row's unit, and a total sums only one unit's lines
        # (both lines here are ea, as the oracle's per-unit sum), stated in that unit.
        statements = [item["statement"] for item in findings(
            world.runtime.cases.get(INVESTIGATOR, cases["SO100201"]["case_id"]))]
        self.assertLessEqual(
            {"SalesOrderLine.ordered_quantity = 100.0 ea (quantity role: ordered).",
             "SalesOrderLine.ordered_quantity = 50 ea (quantity role: ordered)."},
            set(statements))
        self.assertEqual([s for s in statements if re.search(r"= 150(\.0)?\b", s)],
                         ["Ordered, produced and shipped quantities of a sales order: "
                          "ordered = 150 ea (computed)."])

    def test_customer_po_roots_its_order_and_a_name_in_its_field_is_never_echoed(self):
        world = OrderWorld(self)
        opener = world.opener()
        opened = opener.open_case("customer_po", "CPO-7781")
        self.assertEqual(opened["status"], "opened", opened)
        self.assertIn(("public.app_sales_order", "so-a"), rows(world.view(opened["case_id"])))
        self.assertEqual(opener.open_case("customer_po", "CPO-DUP")["missing"],
                         "ambiguous_identifier")
        # An order whose po_reference holds a person's name: the field is cited as source
        # text, never stated as a customer PO number and never echoed.
        opened = opener.open_case("sales_order", "SO100420")
        case = world.runtime.cases.get(INVESTIGATOR, opened["case_id"])
        self.assertNotIn("Gonzalez", json.dumps(case))
        [field] = [item for item in findings(case)
                   if item["statement"].startswith("SalesOrder.customer_po")]
        self.assertEqual(field["statement"], "SalesOrder.customer_po: a text value, in the "
                                             "cited source.")
        self.assertFalse(field.get("value_claims"))

    def test_a_relation_outside_the_audience_is_withheld_never_read_or_absent(self):
        world = OrderWorld(self)
        investigator = Actor("plx", frozenset({OPS, PURCHASING}))  # no customers
        opened = world.opener(investigator).open_case("sales_order", "SO100201")
        self.assertEqual(opened["status"], "opened", opened)
        view = world.view(opened["case_id"], investigator)
        self.assertNotIn("public.app_customer", {r for r, _ in rows(view)})
        self.assertNotIn("absent_in_source", {gap["state"] for gap in view["gaps"]})
        reader = Actor("plx", frozenset({OPS}))
        full = world.view(opened["case_id"], investigator)
        check = live_ws.leak_check(world.runtime, investigator, reader, full,
                                   world.view(opened["case_id"], reader),
                                   AudienceMap.from_file(world.audience))
        self.assertEqual((check["row_leaks"], check["withheld_relation_refs"]), (0, 0))

    def test_a_portal_outage_after_a_good_capture_is_inaccessible_never_absent(self):
        world = OrderWorld(self, outage=True)
        opener = world.opener()
        self.assertTrue(opener.upstream)
        opened = opener.open_case("sales_order", "SO100201")
        self.assertEqual(opened["status"], "opened", opened)
        gaps = world.view(opened["case_id"])["gaps"]
        self.assertNotIn("absent_in_source", {gap["state"] for gap in gaps})
        self.assertIn("inaccessible", {gap["state"] for gap in gaps})
        self.assertEqual(claim_failures(world, opened["case_id"]), [])
        self.assertEqual(opener.open_case("sales_order", "SO999999"),
                         {"request": "sales_order", "status": "blocked",
                          "missing": live_ws.BLOCKED_UPSTREAM})

    def test_po_picks_reach_a_received_line_and_stop_when_it_is_withheld(self):
        world = OrderWorld(self)
        opener = world.opener()
        opened = opener.open_case("pick", "PurchaseOrder+ReceiptLine")
        self.assertEqual(opened["status"], "opened", opened)
        # The newest PO with a received line: Portal PO B or the FM twin of PO A.
        self.assertIn((opener.root["relation"], opener.root["key"]["id"]),
                      {("public.app_purchase_order", "po-b"),
                       ("public.generated_purchase_order", 3301)})
        # A reader the tools show no purchase order: the candidates the projection holds
        # are not reached for it (a typed cause, never "none found").
        no_purchasing = Actor("plx", frozenset({OPS, SALES}))
        self.assertEqual(world.opener(no_purchasing).open_case(
            "pick", "PurchaseOrder+ReceiptLine")["missing"], "pick_not_reached")

    def test_one_ten_or_sixty_lines_are_each_cited_once_or_the_cut_is_named(self):
        """Cardinality probe: a purchase order with N lines. Up to what one case cites,
        every line is cited and none twice; past it the case names the cut as a gap,
        never drops rows silently; each open stays inside a few seconds."""
        import time

        for count in (1, 10, 60):
            lines = [{"id": f"pol-n{i}", "purchase_order_id": "po-b", "product_id": "p-8",
                      "quantity": str(i + 1), "quantity_received": str(i + 1), "uom": "cse",
                      "updated_at": "2026-09-29T08:00:00Z"} for i in range(count)]
            kept = [row for row in fixture.tables()["public.app_purchase_order_line"]
                    if row["purchase_order_id"] != "po-b"]
            world = OrderWorld(self, tables={"public.app_purchase_order_line": kept + lines})
            started = time.monotonic()
            opened = world.opener().open_case("petra_po", "PO100888")
            self.assertLess(time.monotonic() - started, 20)
            view = world.view(opened["case_id"])
            handles = [record["handle"] for record in view["records"]]
            self.assertEqual(len(handles), len(set(handles)))
            cited = {key for relation, key in rows(view)
                     if relation == "public.app_purchase_order_line"}
            with self.subTest(count=count):
                if count <= 10:
                    self.assertEqual(cited, {f"pol-n{i}" for i in range(count)})
                else:
                    self.assertLess(len(cited), count)
                    self.assertIn("answer_truncated",
                                  {gap["why"].split(":")[0] for gap in view["gaps"]})
            if count == 10:
                # Fewer records than reached fit the case: the cut is a named gap too.
                with mock.patch.object(live_ws, "MAX_REACHED", 3):
                    capped = world.view(world.opener().open_case(
                        "petra_po", "PO100888", reinvestigate=True)["case_id"])
                self.assertIn("record_cap", {gap["why"].split(":")[0]
                                             for gap in capped["gaps"]})

    def test_every_claimed_value_is_one_its_cited_row_holds(self):
        from operating.mcp_server import MCPServer, local_client

        world = OrderWorld(self)
        opened = world.opener().open_case("sales_order", "SO100201")
        call = local_client(MCPServer(case_runtime=world.runtime, actor=INVESTIGATOR),
                            "claims")
        claims = call("plx.case_get", {"case_id": opened["case_id"], "section": "claims"})
        self.assertGreater(sum(claim["status"] == "passed" for item in claims["items"]
                               for claim in item["claims"]), 5)
        # Every fact, property answer, count and gap passes; a number the gate still
        # reports is a selected or computed answer no cited field holds (visible).
        self.assertEqual(claim_failures(world, opened["case_id"]), [])
        line = next(c["handle"] for item in findings(world.runtime.cases.get(
            INVESTIGATOR, opened["case_id"])) for c in item["citations"]
                    if "app_sales_order_line/" in c["handle"])
        # A claim the cited row does not hold is reported on read, never passed.
        call("plx.case_add_finding", {
            "case_id": opened["case_id"], "kind": "fact", "status": "supported",
            "statement": "SalesOrderLine.quantity = 999.", "citations": [line],
            "value_claims": [{"value": "999", "citation": line, "field": "row.quantity"}]})
        claims = call("plx.case_get", {"case_id": opened["case_id"], "section": "claims"})
        [verdict] = [claim for item in claims["items"] for claim in item["claims"]
                     if claim["value"] == "999"]
        self.assertEqual((verdict["status"], verdict["code"]),
                         ("failed", "claim_value_mismatch"))

    def test_the_invoiced_pick_joins_a_bc_invoice_to_its_portal_order_through_find(self):
        world = OrderWorld(self, invoices=INVOICES)
        opener = world.opener()
        opened = opener.open_case("pick", JOIN)
        self.assertEqual(opened["status"], "opened", opened)
        self.assertEqual((opener.root["relation"], opener.root["key"]),
                         ("public.app_sales_order", {"id": "so-a"}))
        no_row = "no_portal_row_for_external_document_number"
        others = {no_row: 3, "not_in_served_replica": 1}
        self.assertEqual(opened["pick"], {
            "mode": "join", "scanned": 4, "joined": True,
            "root_reasons": {no_row: 2, "not_in_served_replica": 1},
            # Each scanned invoice once per declared carrier: the order number joined.
            "carrier_outcomes": {
                "portal.app_sales_order.order_number": {
                    no_row: 2, "not_in_served_replica": 1, "joined": 1},
                "portal.app_sales_order.custom_fields.fmOeNumber": others,
                "portal.app_sales_order.custom_fields.erpCode": others,
                # A Portal-pushed invoice's order number is never read as a customer PO.
                "portal.app_sales_order.po_reference": {"no_identifier": 4},
                # The two-hop paths: the reference as a shipment code (no shipment holds
                # one), and a BC orderNumber (renumbered to, or the order's own number),
                # which none of these invoices carries.
                "portal.app_shipment.shipment_code": others,
                "portal.app_sales_order.custom_fields.renumberedTo": {"no_identifier": 4},
                "portal.app_sales_order.order_number/order_number": {"no_identifier": 4}},
            # Joined going forward: the reverse direction is never tried.
            "directions": {"forward": {"scanned": 4, "joined": True, "budget_spent": False},
                           "reverse": {"tried": False, "start_held": 0, "linked_held": 0,
                                       "linked_matched": 0, "scanned": 0, "joined": False,
                                       "reasons": {}, "budget_spent": False,
                                       "bc": NO_BC}}})
        case = world.runtime.cases.get(INVESTIGATOR, opened["case_id"])
        # How the root was picked is the case's own gap: the tools' gaps never crowd it
        # out of the cap, and the gaps they lost to the cap are named.
        [unjoined] = [gap for gap in case["gaps"] if gap["why"].startswith("pick_unjoined")]
        with mock.patch.object(live_ws, "MAX_GAPS", 12):
            capped = world.runtime.cases.get(INVESTIGATOR, world.opener().open_case(
                "pick", JOIN, reinvestigate=True)["case_id"])
        whys = [gap["why"].split(":")[0] for gap in capped["gaps"]]
        # The tools' gaps fill all but OWN_GAPS; this case's own are the pick and the cut.
        self.assertEqual(len(whys), 12 - live_ws.OWN_GAPS + 2)
        self.assertEqual(whys.count("gap_cap"), 1)
        self.assertEqual(whys.count("pick_unjoined"), 1)
        self.assertEqual(unjoined["why"], "pick_unjoined: reasons: no_portal_row_for_external_"
                                          "document_number, not_in_served_replica")
        # The invoice the pick started from is cited beside the root, as a lead.
        invoice_items = [item for item in findings(case) if item["kind"] == "fact"
                         and any(c["handle"].startswith("bc://") for c in item["citations"])]
        self.assertTrue(invoice_items)
        # ... and so are the records one hop from it (its line), never supported facts.
        self.assertTrue(any(item["statement"].startswith("SalesInvoiceLine.")
                            for item in invoice_items), [i["statement"] for i in invoice_items])
        self.assertEqual([item["statement"] for item in invoice_items
                          if item["status"] != "unknown" or "lead-tier" not in item["statement"]],
                         [])
        self.assertTrue(any(any("app_sales_order/" in c["handle"] for c in item["citations"])
                            for item in case["findings"]["supported"]))
        # Only plx tools decided it: no Portal request after ingestion.
        self.assertEqual(world.runtime.client_configured, False)

    def test_each_unjoined_invoice_gets_the_reason_of_its_find_path(self):
        def reasons(world, actor=INVESTIGATOR):
            return world.opener(actor).open_case("pick", JOIN)

        # The Portal order relation withheld from the investigator: never searched.
        world = OrderWorld(self, invoices=INVOICES[3:],
                           grants={"public.app_sales_order": [OPS]})
        seen = []
        real = live_ws._Session.__call__

        def spy(session, tool, arguments):
            seen.append(tool)
            return real(session, tool, arguments)
        with mock.patch.object(live_ws._Session, "__call__", spy):
            result = reasons(world, Actor("plx", frozenset({SALES, PURCHASING})))
        self.assertEqual((result["missing"], result["pick"]["root_reasons"]),
                         ("pick_none_found", {"portal_row_unreadable_for_reader": 1}))
        self.assertNotIn("plx.find", seen)
        # A find error is find_live_error, never a missing row.
        world = OrderWorld(self, invoices=INVOICES[3:])

        def failing(session, tool, arguments):
            if tool == "plx.find":
                return {"error": {"code": "internal_error", "message": "x"}}
            return real(session, tool, arguments)
        with mock.patch.object(live_ws._Session, "__call__", failing):
            self.assertEqual(reasons(world)["pick"]["root_reasons"], {"find_live_error": 1})
        # Two Portal orders under one number: ambiguous, never a winner.
        dup = [*fixture.tables()["public.app_sales_order"],
               {**fixture.tables()["public.app_sales_order"][0], "id": "so-a2",
                "custom_fields": None}]
        world = OrderWorld(self, invoices=INVOICES[3:], tables={"public.app_sales_order": dup})
        self.assertEqual(reasons(world)["pick"]["root_reasons"], {"ambiguous_root": 1})
        # No carried identifier, and a Portal-shaped reference no row holds (its shape is
        # a declared Portal form: not renumbered).
        world = OrderWorld(self, invoices=[invoice(5, None, "25"),
                                           invoice(6, "OR1000999002", "24")])
        result = reasons(world)
        self.assertEqual(result["pick"]["root_reasons"], {
            "no_identifier": 1, "no_portal_row_for_external_document_number": 1})
        self.assertEqual(result["pick"]["carrier_outcomes"]["portal.app_sales_order.po_reference"],
                         {"no_identifier": 2})
        # The Portal unreadable at the last ingestion: its silence is unread, and the pick
        # is blocked, never "no row".
        world = OrderWorld(self, invoices=INVOICES[:1], outage=True)
        result = reasons(world)
        self.assertEqual((result["status"], result["pick"]["root_reasons"]),
                         ("blocked", {"portal_carrier_unread": 1}))
        # BC records the investigator may not read: never read, never joined, counted.
        world = OrderWorld(self, invoices=INVOICES[3:], bc_readers=("staff:finance",))
        result = reasons(world)
        self.assertEqual((result["missing"], result["pick"]["root_reasons"]),
                         ("pick_none_found", {"root_not_readable": 1}))
        # A join needs a declared link between two systems.
        with self.assertRaises(live_ws.OpenError):
            live_ws.parse_open("pick=PurchaseOrder>ReceiptLine")

    def test_the_pick_turns_around_when_the_newest_invoices_name_no_portal_row(self):
        """gap-6d35a1af53db: BC and the served replica hold different order populations,
        so the newest PICK_SCAN invoices all name rows the replica never holds while an
        older one is the twin of a Portal order. The pick then starts from the Portal
        orders the projection shows an invoice carrying and decides each through the same
        join: the case exists in both systems. With no twin the typed reasons stay, and
        each direction's counts are named. Cardinality probe: 1, 10 or 60 newer invoices
        naming no Portal row; each is counted once (at most PICK_SCAN), none is lost and
        the open stays fast."""
        import time

        no_row = "no_portal_row_for_external_document_number"
        twin = invoice(90, "SO100305", "01")  # Portal order B's number, older than all
        for count in (1, 10, 60):
            unmatched = [invoice(10 + n, f"SO3{n:05d}", f"{10 + n % 18:02d}")
                         for n in range(count)]
            scan = min(count, live_ws.PICK_SCAN)
            with mock.patch.object(live_ws, "PICK_SCAN", scan):
                world = OrderWorld(self, invoices=[*unmatched, twin])
                opener = world.opener()
                started = time.monotonic()
                opened = opener.open_case("pick", JOIN)
                elapsed = time.monotonic() - started
            with self.subTest(count=count):
                self.assertEqual(opened["status"], "opened", opened)
                self.assertEqual((opener.root["relation"], opener.root["key"]),
                                 ("public.app_sales_order", {"id": "so-b"}))
                pick = opened["pick"]
                self.assertEqual((pick["joined"], pick["scanned"], pick["root_reasons"]),
                                 (True, scan, {no_row: scan}))
                self.assertEqual(pick["directions"], {
                    "forward": {"scanned": scan, "joined": False, "budget_spent": False},
                    "reverse": {"tried": True, "start_held": count + 1, "linked_held": 6,
                                "linked_matched": 1, "scanned": 1, "joined": True,
                                "reasons": {}, "budget_spent": False, "bc": NO_BC}})
                self.assertLess(elapsed, 120)
        case = world.runtime.cases.get(INVESTIGATOR, opened["case_id"])
        self.assertTrue(any(any("app_sales_order/" in c["handle"] for c in item["citations"])
                            for item in case["findings"]["supported"]))
        # The invoice it started from is cited beside the root, as a lead.
        self.assertTrue([item for item in findings(case) if item["kind"] == "fact"
                         and any(c["handle"].startswith("bc://") for c in item["citations"])])

        unmatched = unmatched[:3]
        with mock.patch.object(live_ws, "PICK_SCAN", 3):
            # No intersection: nothing picked, the forward reasons kept, the reverse counted.
            opened = OrderWorld(self, invoices=unmatched).opener().open_case("pick", JOIN)
            self.assertEqual((opened["missing"], opened["pick"]["root_reasons"]),
                             ("pick_none_found", {no_row: 3}))
            self.assertEqual(opened["pick"]["directions"]["reverse"], {
                "tried": True, "start_held": 3, "linked_held": 6, "linked_matched": 0,
                "scanned": 0, "joined": False, "reasons": {}, "budget_spent": False,
                "bc": NO_BC})
            # A shared value the join refuses (two Portal orders under one number) is
            # proposed, never joined, and keeps its typed reason.
            dup = [*fixture.tables()["public.app_sales_order"],
                   {**fixture.tables()["public.app_sales_order"][0], "id": "so-a2",
                    "custom_fields": None}]
            opened = OrderWorld(self, invoices=[*unmatched, invoice(90, "SO100201", "01")],
                                tables={"public.app_sales_order": dup}
                                ).opener().open_case("pick", JOIN)
            self.assertEqual(opened["missing"], "pick_none_found", opened)
            self.assertEqual(opened["pick"]["directions"]["reverse"], {
                "tried": True, "start_held": 4, "linked_held": 7, "linked_matched": 2,
                "scanned": 1, "joined": False, "reasons": {"ambiguous_root": 1},
                "budget_spent": False, "bc": NO_BC})
            # The Portal order relation withheld from the investigator: the reverse
            # direction never reads its values, so it proposes nothing.
            world = OrderWorld(self, invoices=[*unmatched, twin],
                               grants={"public.app_sales_order": [OPS]})
            seen = []
            real = live_ws._Session.__call__

            def spy(session, tool, arguments):
                seen.append(tool)
                return real(session, tool, arguments)
            with mock.patch.object(live_ws._Session, "__call__", spy):
                opened = world.opener(Actor("plx", frozenset({SALES, PURCHASING}))
                                      ).open_case("pick", JOIN)
        self.assertEqual(opened["missing"], "pick_none_found", opened)
        self.assertEqual(opened["pick"]["directions"]["reverse"]["linked_held"], 0)
        self.assertEqual(opened["pick"]["directions"]["reverse"]["scanned"], 0)
        self.assertNotIn("plx.find", seen)

    def test_the_reverse_pick_asks_bc_for_the_portal_orders_the_capture_never_read(self):
        """gap-6d35a1af53db, live 2026-10-16: the host's BC capture held two invoices, both
        naming a prefix-setting database's rows, against 800 Portal orders, so no
        projection intersection could join. The reverse direction then asks BC itself,
        GET only through the live reader: for each newest Portal order the investigator
        reads (at most PICK_SCAN), one $filter equality per carried value and pinned
        company; a hit is decided by the same join and cited live beside the root, as a
        lead. Counts only (lookups made, hits found). Cardinality probe: 1, 10 or 60
        newer Portal orders BC holds no invoice for; each is asked once (orders read at
        most PICK_SCAN), none twice, and the open stays fast."""
        import time

        prefixed = invoice(2, "UAT-SO100018", "27")
        twin = invoice(90, "SO100305", "01")  # Portal order B's number, never captured
        orders = fixture.tables()["public.app_sales_order"]
        for count in (1, 10, 60):
            newer = [fixture._so(f"so-x{n}", f"SO4{n:05d}", "cust-1", "CONFIRMED", "28")
                     for n in range(count)]
            fake = bc_world([prefixed, twin])
            # BC is granted to sales and purchasing, not to operations.
            world = OrderWorld(self, invoices=[prefixed], live_bc=fake,
                               bc_readers=(SALES, PURCHASING),
                               tables={"public.app_sales_order": [*orders, *newer]})
            opener = world.opener()
            started = time.monotonic()
            opened = opener.open_case("pick", JOIN)
            elapsed = time.monotonic() - started
            asked = [unquote(url) for _m, url, _h in fake.calls if "$filter" in unquote(url)
                     and "/salesInvoices" in url]
            with self.subTest(count=count):
                bc = opened["pick"]["directions"]["reverse"]["bc"]
                self.assertEqual(len(asked), len(set(asked)))  # nothing asked twice
                self.assertTrue(all(" eq 'UAT-" not in url for url in asked))
                # lookups count the probes BC answered; requests every GET sent.
                self.assertEqual(bc["lookups"], len(asked))
                self.assertGreaterEqual(bc["requests"], len(asked))
                if count + 6 <= live_ws.PICK_SCAN:
                    self.assertEqual(opened["status"], "opened", opened)
                    self.assertEqual((opener.root["relation"], opener.root["key"]),
                                     ("public.app_sales_order", {"id": "so-b"}))
                    self.assertEqual((bc["reader"], bc["hits"], bc["scanned"],
                                      opened["pick"]["joined"],
                                      opened["pick"]["directions"]["reverse"]["joined"]),
                                     (True, 1, 1, True, True))
                    self.assertGreater(bc["lookups"], 0)
                else:
                    # The 50 newest orders name nothing BC holds: nothing picked, the
                    # bound kept and every lookup counted.
                    self.assertEqual(opened["missing"], "pick_none_found", opened)
                    self.assertEqual((bc["orders"], bc["hits"]), (live_ws.PICK_SCAN, 0))
                self.assertLessEqual(bc["orders"], live_ws.PICK_SCAN)
                self.assertLess(elapsed, 120)
            if count == 1:
                case = world.runtime.cases.get(INVESTIGATOR, opened["case_id"])
                live = [item for item in findings(case) if any(
                    c["handle"].startswith(f"bc://production/{tb.CO[0]}/salesInvoices/"
                                           f"{twin['id']}") for c in item["citations"])]
                # The invoice read live is cited beside the order, as a lead only.
                self.assertTrue(live, findings(case))
                self.assertTrue(all(item["status"] == "unknown" and "lead" in
                                    item["statement"] for item in live), live)
                self.assertEqual(claim_failures(world, opened["case_id"]), [])
                # A reader BC does not grant sees none of it in the case.
                view = world.view(opened["case_id"], Actor("plx", frozenset({OPS})))
                self.assertIsNotNone(view)
                self.assertNotIn(twin["id"], json.dumps(view))
                self.assertNotIn("PSI-90", json.dumps(view))

        # The same number held by a second BC company: no one invoice, never joined.
        fake = bc_world([prefixed, twin])
        fake.data[(tb.CO[1], "salesInvoices")] = [{**invoice(91, "SO100305", "02"),
                                                   "id": gid(2, 12, 91)}]
        opened = OrderWorld(self, invoices=[prefixed], live_bc=fake
                            ).opener().open_case("pick", JOIN)
        self.assertEqual(opened["missing"], "pick_none_found", opened)
        self.assertEqual(opened["pick"]["directions"]["reverse"]["reasons"],
                         {"cross_scope": 2})
        # A company BC lists outside the books is never read, so the number may be its
        # too: no one invoice, never joined (cross_scope), the unpinned probe counted.
        fake = bc_world([prefixed, twin])
        fake.companies.append(tb.UNPINNED)
        opened = OrderWorld(self, invoices=[prefixed], live_bc=fake
                            ).opener().open_case("pick", JOIN)
        reverse = opened["pick"]["directions"]["reverse"]
        self.assertEqual((opened["missing"], reverse["joined"], set(reverse["reasons"])),
                         ("pick_none_found", False, {"cross_scope"}), opened)
        self.assertGreater(reverse["bc"]["lookup_states"].get("unpinned", 0), 0)
        self.assertFalse([url for _m, url, _h in fake.calls if tb.UNPINNED in unquote(url)
                          and "$filter" in unquote(url)])
        # A reader BC refuses (the live audience grants BC to no principal of theirs):
        # every lookup withheld, nothing found, nothing told.
        fake = bc_world([prefixed, twin])
        opened = OrderWorld(self, invoices=[prefixed], live_bc=fake, bc_readers=(OPS,)
                            ).opener(Actor("plx", frozenset({SALES, PURCHASING}))
                                     ).open_case("pick", JOIN)
        bc = opened["pick"]["directions"]["reverse"]["bc"]
        self.assertEqual((opened["missing"], bc["hits"], bc["scanned"], bc["lookups"],
                          bc["requests"]), ("pick_none_found", 0, 0, 0, 0), opened)
        self.assertFalse([url for _m, url, _h in fake.calls if "$filter" in unquote(url)])
        # The read policy withholds company 1's invoices from the
        # investigator while the live audience allows them. The reverse lookups go
        # through plx.find's one resolver path, so that unit is withheld before any
        # request: no $filter GET to company 1, its twin never found; the others asked.
        import dataclasses

        from integrations.capture_store import bc_unit
        from operating import ontology_host

        withheld = bc_unit(tb.CO[0], "salesInvoices")
        real = ontology_host.case_host_policy

        def withholding(*args, **kwargs):
            policy = real(*args, **kwargs)
            return dataclasses.replace(policy, audiences={**policy.audiences,
                                                          withheld: frozenset()})
        fake = bc_world([prefixed, twin])
        world = OrderWorld(self, invoices=[prefixed], live_bc=fake)
        with mock.patch.object(ontology_host, "case_host_policy", withholding):
            opened = world.opener().open_case("pick", JOIN)
        bc = opened["pick"]["directions"]["reverse"]["bc"]
        self.assertEqual((opened["missing"], bc["hits"]), ("pick_none_found", 0), opened)
        self.assertGreater(bc["lookup_states"].get("withheld", 0), 0, bc)
        self.assertGreater(bc["lookups"], 0, bc)
        filtered = [unquote(url) for _m, url, _h in fake.calls if "$filter" in unquote(url)]
        self.assertFalse([url for url in filtered if tb.CO[0] in url], filtered)
        self.assertTrue([url for url in filtered if tb.CO[1] in url], filtered)
        # No live BC reader: the step is named as not run, never as "no invoice".
        opened = OrderWorld(self, invoices=[prefixed]).opener().open_case("pick", JOIN)
        self.assertEqual(opened["pick"]["directions"]["reverse"]["bc"]["reader"], False)

    def test_the_reverse_pick_keeps_the_join_rules_its_order_and_its_budget(self):
        """The reverse direction refuses what the forward one refuses
        (a case variant of a customer PO, one number in two BC companies), proposes the
        order-number twins before the customer-PO ones (a PO many orders share, such as
        "VERBAL", never crowds them out of the PICK_SCAN slots), stops at its deadline
        and is not run while the Portal it would join could not be read."""
        newer = [invoice(30 + n, f"SO3{n:05d}", "30") for n in range(3)]
        no_row = {"no_portal_row_for_external_document_number": 3}
        with mock.patch.object(live_ws, "PICK_SCAN", 3):
            # A lower-case copy of order A's PO, its customer joined by a reviewed link:
            # the PO cased as the Portal holds it joins, the variant is a lead only.
            for po, joined, reasons in (("CPO-7781", True, {}),
                                        ("cpo-7781", False, {"identity_lead_only": 1})):
                opener = OrderWorld(self, invoices=[*newer, native(po)],
                                    customers=CARDS).opener()
                customer_link(opener, "reviewed")
                opened = opener.open_case("pick", JOIN)
                with self.subTest(po=po):
                    reverse = opened["pick"]["directions"]["reverse"]
                    self.assertEqual((opened["pick"]["root_reasons"], reverse["scanned"],
                                      reverse["joined"], reverse["reasons"]),
                                     (no_row, 1, joined, reasons), opened)
            # Order B's number on invoices of two BC companies, both captured and older
            # than the scanned ones: no one invoice, never joined.
            real = bc_world

            def two_companies(invoices, customers=()):
                fake = real(invoices, customers)
                fake.data[(tb.CO[1], "salesInvoices")] = [
                    {**invoice(20, None, "19", order="SO100305"), "id": gid(2, 12, 20)}]
                return fake
            for second in (False, True):
                with mock.patch(f"{__name__}.bc_world", two_companies if second else real):
                    opened = OrderWorld(self, invoices=[
                        *newer, invoice(10, None, "20", order="SO100305")]
                    ).opener().open_case("pick", JOIN)
                with self.subTest(second_company=second):
                    reverse = opened["pick"]["directions"]["reverse"]
                    self.assertEqual((opened.get("missing"), reverse["joined"],
                                      reverse["reasons"]),
                                     ("pick_none_found", False, {"cross_scope": 2})
                                     if second else (None, True, {}), opened)
        with mock.patch.object(live_ws, "PICK_SCAN", 1):
            # One reverse slot; a customer PO two customers' orders share is newer than
            # order B's twin: the order-number twin is proposed first and joins.
            opener = OrderWorld(self, invoices=[newer[0], native("CPO-DUP"),
                                                invoice(90, "SO100305", "01")],
                                customers=CARDS).opener()
            opened = opener.open_case("pick", JOIN)
            self.assertEqual((opened["status"], (opener.root or {}).get("key"),
                              opened["pick"]["directions"]["reverse"]["scanned"]),
                             ("opened", {"id": "so-b"}, 1), opened)
        # The pick's deadline spent: nothing is read or asked, the forward cut is named
        # and the reverse direction is not run; never "none found".
        prefixed = invoice(2, "UAT-SO100018", "27")
        fake = bc_world([prefixed, invoice(90, "SO100305", "01")])
        with mock.patch.object(live_ws, "PICK_BUDGET_SECONDS", 0.0):
            opened = OrderWorld(self, invoices=[prefixed], live_bc=fake
                                ).opener().open_case("pick", JOIN)
        directions = opened["pick"]["directions"]
        self.assertEqual((opened["missing"], directions["forward"]["budget_spent"],
                          directions["forward"]["scanned"], directions["reverse"]["tried"]),
                         ("pick_budget_spent", True, 0, False), opened)
        self.assertFalse([url for _m, url, _h in fake.calls if "$filter" in unquote(url)])
        # The Portal unreadable after a good capture: every carrier joins a Portal order,
        # so the reverse direction is not run and BC is never asked; blocked, not absent.
        fake = bc_world([prefixed, invoice(90, "SO100305", "01")])
        world = OrderWorld(self, invoices=[prefixed], live_bc=fake, outage=True)
        opened = world.opener().open_case("pick", JOIN)
        self.assertEqual((opened["status"], opened["missing"]),
                         ("blocked", live_ws.BLOCKED_UPSTREAM), opened)
        self.assertFalse([url for _m, url, _h in fake.calls if "$filter" in unquote(url)])

    def test_the_reverse_pick_keeps_its_share_of_one_wall_clock(self):
        """Live checks of 73775fc89 (timed out at 40 min) and 4130ffdf7 (all 420 s spent on
        11 forward roots, the reverse direction never run): each forward root's plx.find
        fell through to live BC probes (every pinned company, entity set and field) that
        could only find the invoice the pick started from. Now a find the projection
        cannot answer asks live only the families of the carriers' targets (the Portal),
        both directions share one wall clock (PICK_BUDGET_SECONDS, every live request
        refused once it has passed and never given longer than what is left), the
        forward one at most FORWARD_SHARE of it, so the reverse one always runs; a clock
        cut is counted and fails the pick as pick_budget_spent, never "none found"."""
        import time

        from integrations import bc_read

        class Slow(LookupBc):
            delay = 0.0

            def __call__(self, method, url, headers, body, timeout, max_bytes):
                self.timeouts.append(timeout)
                time.sleep(self.delay)
                return super().__call__(method, url, headers, body, timeout, max_bytes)

        newer = [invoice(30 + n, f"SO3{n:05d}", "30") for n in range(10)]
        # The live finds use the default books; here they pin the fixture companies, so an
        # unrestricted forward find would reach the live BC.
        books = load_company_books({"schema": "plx-bc-company-books/v1",
                                    "environments": tb.BOOKS})
        for budget, delay in ((live_ws.PICK_BUDGET_SECONDS, 0.0), (6.0, 0.4)):
            fake = bc_world(newer)
            fake.__class__, fake.delay, fake.timeouts = Slow, delay, []
            with mock.patch.object(live_ws, "PICK_BUDGET_SECONDS", budget), \
                    mock.patch.object(bc_read, "load_company_books", lambda *_a, **_k: books):
                world = OrderWorld(self, invoices=newer, live_bc=fake)
                started = time.monotonic()
                opened = world.opener().open_case("pick", JOIN)
                elapsed = time.monotonic() - started
            directions = opened["pick"]["directions"]
            asked = [unquote(url) for _m, url, _h in fake.calls if "$filter" in unquote(url)]
            with self.subTest(budget=budget):
                # No forward root's own number was asked of BC; the reverse lookups were.
                self.assertFalse([url for url in asked if "SO3000" in url], asked)
                self.assertTrue(directions["reverse"]["tried"], opened)
                self.assertGreater(directions["reverse"]["bc"]["requests"], 0, opened)
                if delay:
                    # BC answers slowly: the reverse lookups are cut at the clock.
                    self.assertEqual((opened["missing"], directions["reverse"]["budget_spent"]),
                                     ("pick_budget_spent", True), opened)
                    self.assertLess(elapsed, budget + 2 * delay + 5)
                    self.assertTrue(all(t <= budget for t in fake.timeouts), fake.timeouts)
                else:
                    self.assertEqual((opened["missing"], directions["forward"]["scanned"],
                                      directions["forward"]["budget_spent"],
                                      directions["reverse"]["budget_spent"]),
                                     ("pick_none_found", len(newer), False, False), opened)

        # Forward roots that would take the whole clock: the forward direction stops at
        # its share and the reverse one still joins order B's older twin.
        twin = invoice(90, "SO100305", "01")
        real = live_ws.CaseOpener._join

        def slow_join(opener, *args, **kwargs):
            time.sleep(1.0)
            return real(opener, *args, **kwargs)
        world = OrderWorld(self, invoices=[*newer, twin])
        opener = world.opener()
        with mock.patch.object(live_ws, "PICK_BUDGET_SECONDS", 8.0), \
                mock.patch.object(live_ws.CaseOpener, "_join", slow_join):
            opened = opener.open_case("pick", JOIN)
        directions = opened["pick"]["directions"]
        self.assertEqual((opened["status"], (opener.root or {}).get("key"),
                          directions["forward"]["budget_spent"], directions["reverse"]["joined"]),
                         ("opened", {"id": "so-b"}, True, True), opened)
        self.assertLess(directions["forward"]["scanned"], len(newer))

    def test_a_live_forward_match_is_found_again_over_bc_so_cross_scope_holds(self):
        """A forward root's plx.find the projection cannot answer asks
        live only the Portal; when that live find shows the Portal order a company-scoped
        carrier would root on, the find is repeated over every family, so a BC record of
        another company holding the same number still makes the invoice cross_scope,
        never a silent join. Order B is left out of the projection (a Portal order the
        mirror holds that the projection does not show yet), so only the live fallback
        finds it; BC company 2 then holds a sales order numbered as order B."""
        import sqlite3

        from integrations import bc_read

        order_b = 'SalesOrder:portal.app_sales_order:{"id":"so-b"}'
        carrier = "portal.app_sales_order.order_number"
        for second in (False, True):
            twin = invoice(90, "SO100305", "01")
            fake = bc_world([twin])
            if second:
                fake.data[(tb.CO[1], "salesOrders")] = [record(
                    gid(2, 15, 1), number="SO100305", customerNumber="C0001",
                    status="Open", orderDate="2026-09-02",
                    lastModifiedDateTime="2026-09-02T10:00:00Z")]
            world = OrderWorld(self, invoices=[twin], live_bc=fake)
            opener = world.opener()
            db = sqlite3.connect(opener.projection())
            try:
                with db:
                    for table, column in (("identifiers", "object_id"),
                                          ("object_values", "object_id"),
                                          ("object_versions", "object_id"),
                                          ("event_objects", "object_id"),
                                          ("links", "from_object"), ("links", "to_object"),
                                          ("objects", "object_id")):
                        db.execute(f"DELETE FROM {table} WHERE {column} = ?", (order_b,))
            finally:
                db.close()
            # The live finds pin the opener's books (the host config's), never the default
            # map: an empty default here would pin no company and miss company 2.
            with mock.patch.object(bc_read, "load_company_books", lambda *_a, **_k: {}):
                opened = opener.open_case("pick", JOIN)
            pick = opened["pick"]
            asked = [unquote(url) for _m, url, _h in fake.calls if "$filter" in unquote(url)]
            with self.subTest(second_company=second):
                if second:
                    self.assertEqual((opened["missing"], pick["joined"],
                                      pick["root_reasons"],
                                      pick["carrier_outcomes"][carrier]),
                                     ("pick_none_found", False, {"cross_scope": 1},
                                      {"cross_scope": 1}), opened)
                else:
                    # No other company holds it: the live Portal match joins forward.
                    self.assertEqual((pick["joined"], pick["directions"]["forward"]["joined"],
                                      pick["carrier_outcomes"][carrier]),
                                     (True, True, {"joined": 1}), opened)
                # The repeat over every family asked BC (each pinned company) for the
                # number; the Portal-only find before it asks BC nothing.
                self.assertTrue([url for url in asked if tb.CO[1] in url
                                 and "/salesOrders" in url and "'SO100305'" in url], asked)

    def test_a_po_carried_invoice_joins_an_order_of_its_own_customer_only(self):
        """BC copies the Portal order's po_reference from the BC sales order an invoice is
        posted from into its externalDocumentNumber: such an invoice joins by the
        customer's PO only within its customer, as the Portal resolves it (customer + PO);
        the order-number path is the test above."""
        po = "portal.app_sales_order.po_reference"

        def pick(invoices, actor=INVESTIGATOR, tier=None, **world):
            opener = OrderWorld(self, invoices=invoices, customers=CARDS, **world).opener(actor)
            if tier is not None:
                customer_link(opener, tier)
            opened = opener.open_case("pick", JOIN)
            return opened, opener.root

        # Billed to the order's customer, BC C0001 linked to the Portal's cust-1 only as a
        # lead (the projection caps a BC number to a lead while the BC company is not
        # known): the scope party is unread, never joined, and nothing roots.
        opened, root = pick([native("CPO-7781")])
        self.assertEqual((opened.get("missing"), root, opened["pick"]["root_reasons"]),
                         ("pick_none_found", None, {"scope_unread": 1}), opened)
        self.assertEqual(opened["pick"]["carrier_outcomes"][po], {"scope_unread": 1})
        # The same pair joined by a person's reviewed link (the Portal's LINKED
        # entity-map row): joined.
        opened, root = pick([native("CPO-7781")], tier="reviewed")
        self.assertEqual((opened["status"], root["key"]), ("opened", {"id": "so-a"}), opened)
        self.assertEqual(opened["pick"]["carrier_outcomes"][po], {"joined": 1})
        # The same text on a direct invoice (no BC order; the Portal's push put its order
        # number there) is not a customer PO: never searched as one.
        opened, _root = pick([invoice(7, "CPO-7781", "29")])
        self.assertEqual((opened["missing"], opened["pick"]["carrier_outcomes"][po]),
                         ("pick_none_found", {"no_identifier": 1}))
        # Billed to another customer: the same PO joins nothing.
        opened, _root = pick([native("CPO-7781", customer="C0002")])
        self.assertEqual((opened["missing"], opened["pick"]["root_reasons"]),
                         ("pick_none_found", {"scope_mismatch": 1}))
        # ... unless a customer's identity list was cut (plx.object shows MAX_IDENTITY
        # links and says the rest only as a gap): the left-out link may be the one, so
        # the scope is unread, never a mismatch.
        with mock.patch("operating.ontology_tools.MAX_IDENTITY", 0):
            opened, _root = pick([native("CPO-7781", customer="C0002")])
        self.assertEqual(opened["pick"]["root_reasons"], {"scope_unread": 1})
        # Two customers' orders carry one PO: ambiguous, never a winner.
        opened, _root = pick([native("CPO-DUP")])
        self.assertEqual(opened["pick"]["root_reasons"], {"ambiguous_root": 1})
        # The order's customer withheld from the investigator: the scope is unread, never
        # joined and never a mismatch, however strong the link.
        opened, _root = pick([native("CPO-7781")], Actor("plx", frozenset({OPS})),
                             tier="reviewed")
        self.assertEqual(opened["pick"]["root_reasons"], {"scope_unread": 1})
        # A case variant of the PO is not the owning system's literal text.
        opened, _root = pick([native("cpo-7781")])
        self.assertEqual(opened["pick"]["carrier_outcomes"][po], {"normalized_only": 1})

    def test_find_and_the_case_pick_read_one_party_through_one_rule(self):
        """plx.find's related_same_record and the case pick's scope party are both
        ontology_tools.same_party: on the same pair they give the same verdict."""
        from operating import ontology_tools

        opener = OrderWorld(self, invoices=[native("CPO-7781")], customers=CARDS).opener()
        session = live_ws._Session(opener.runtime, INVESTIGATOR, opener.audience,
                                   opener.projection())
        self.addCleanup(session.close)
        decided: list[tuple[str, str, bool | None]] = []
        real = ontology_tools.same_party

        def spy(first, second, edges, **options):
            verdict = real(first, second, edges, **options)
            decided.append((first, second, verdict))
            return verdict
        with mock.patch.object(ontology_tools, "same_party", spy):
            opened = opener.open_case("pick", JOIN)
            [picked] = [(a, b, v) for a, b, v in decided if PORTAL_CUSTOMER in (a, b)
                        and any(end.startswith("Customer:bc.") for end in (a, b))]
            decided.clear()
            found = session("plx.find", {"query": "C0001"})
            by_find = list(decided)
            decided.clear()
            # The case pick's rule, asked about every pair plx.find decided and the pair
            # the pick decided, through the same plx tools.
            again = [live_ws.CaseOpener._same_record(session, a, b, {})
                     for a, b, _v in [*by_find, picked]]
        self.assertEqual((picked[2], opened["pick"]["root_reasons"]),
                         (None, {"scope_unread": 1}))
        self.assertTrue(by_find)
        self.assertEqual(again, [v for _a, _b, v in [*by_find, picked]])
        # Only a verified or reviewed link joins; a weaker or unknown tier is unread, a
        # contradicted one alone reads nothing.
        one, other = PORTAL_CUSTOMER, "Customer:bc.customers:x"
        self.assertEqual([real(one, other, [(one, other, tier)]) for tier in
                          ("verified", "reviewed", "corroborated", "stored_by_text_join",
                           "lead", None, "contradicted")],
                         [True, True, None, None, None, None, False])
        # plx.find marks a pair one record exactly when the rule says True.
        features = [f for c in found["candidates"] for f in c["why_not"]
                    if f["feature"].startswith("related_")]
        self.assertEqual(len(features), len(by_find))
        self.assertEqual([f["feature"] == "related_same_record" for f in features],
                         [v is True for _a, _b, v in by_find])

    def test_the_spec_command_meets_its_expectations_over_injected_sources(self):
        """case.unfamiliar_order: the committed command (its host config copied with a
        books map that pins the fixture companies) over the in-process FakeWorkbench and
        FakeBc; its expectations hold on the real summary, which records counts and enums
        only; a Portal answering HTTP 500 at capture meets expect_blocked."""
        from devloop import live_verify as lv
        from integrations.http import SourceHttpError
        from integrations.tests.test_live_source_adapters import ENV

        spec = json.loads((ROOT / "devloop" / "live-checks" / "case.unfamiliar_order.json")
                          .read_text("utf-8"))
        self.assertEqual(lv.validate_spec(spec, stem="case.unfamiliar_order"), [])
        self.assertEqual(spec["cadence"], "daily")

        def run(fetch, sender=None):
            code, text = spec_main(spec["command"][3:], fetch, sender, environ={
                **ENV, "PLX_WORKBENCH_API_URL": BASE, "PLX_WORKBENCH_API_KEY": KEY})
            return code, json.loads(text), text

        code, document, text = run(None)
        self.assertEqual(lv.check_expectations(spec, code, document), [], document)
        self.assertEqual(document["pick_root_reasons"], {
            "no_portal_row_for_external_document_number": 2, "not_in_served_replica": 1})
        self.assertEqual(document["pick_carrier_outcomes"]["portal.app_sales_order.order_number"],
                         {"joined": 1, "no_portal_row_for_external_document_number": 2,
                          "not_in_served_replica": 1})
        recorded = json.dumps(lv._recorded(spec, document))
        self.assertIn("no_portal_row_for_external_document_number", recorded)
        # Every carrier's outcomes are recorded under its key, the two-hop paths' too.
        for key in ("portal.app_sales_order.po_reference", "portal.app_shipment.shipment_code",
                    "portal.app_sales_order.custom_fields.renumberedTo"):
            self.assertIn(f'"{key}": {{', recorded)
        for value in ("SO100201", "S-ORD10045", "UAT-SO100018", "PSI-", "so-a", "C0001",
                      "api://", "bc://", "CPO-7781", "PO100777"):
            self.assertNotIn(value, text)
        # The host captures the BC customer cards too, but a Portal customer's erp_code
        # reaches a BC number only as a lead (no served book names its company), so the
        # newest invoice, posted from a BC order and carrying its customer's PO, is
        # counted scope_unread and the pick keeps its verdict on the order-number join;
        # the summary still names no value.
        code, document, text = run(None, bc_world([invoice(7, "CPO-7781", "29",
                                                           order="S-ORD10077"), *INVOICES],
                                                  [("C0001", "Customer One")]))
        self.assertEqual(lv.check_expectations(spec, code, document), [], document)
        self.assertEqual((document["pick_root_reasons"],
                          document["pick_carrier_outcomes"]["portal.app_sales_order.po_reference"]),
                         ({"no_portal_row_for_external_document_number": 2,
                           "not_in_served_replica": 1, "scope_unread": 1},
                          {"no_identifier": 4, "scope_unread": 1}))
        for value in ("CPO-7781", "C0001", "Customer One", "so-a", "PSI-"):
            self.assertNotIn(value, text)
        # gap-6d35a1af53db: every invoice the pick scans going forward names no Portal row,
        # an older one is a Portal order's twin. The pick turns around, the case is cited
        # across both systems and the summary counts each direction (counts only).
        unmatched = [invoice(10 + n, f"SO3{n:05d}", f"{10 + n:02d}") for n in range(3)]
        with mock.patch.object(live_ws, "PICK_SCAN", 3):
            code, document, text = run(None, bc_world([*unmatched,
                                                       invoice(90, "SO100305", "01")]))
        self.assertEqual(lv.check_expectations(spec, code, document), [], document)
        self.assertEqual((document["cited_cross_rooted"], document["pick_root_reasons"]),
                         (1, {"no_portal_row_for_external_document_number": 3}))
        self.assertEqual(document["pick_directions"], {
            "forward": {"joined": 0, "scanned": 3, "budget_spent": 0},
            "reverse": {"joined": 1, "linked_held": 6, "linked_matched": 1, "reasons": {},
                        "scanned": 1, "start_held": 4, "tried": 1,
                        # Joined from the capture: BC was never asked (and the
                        # document counts the cases that had a live BC reader).
                        "budget_spent": 0,
                        "bc": {"reader": 0, "orders": 0, "orders_unreadable": 0,
                               "requests": 0, "lookups": 0, "lookup_states": {},
                               "hits": 0,
                               "scanned": 0}}})
        recorded = json.dumps(lv._recorded(spec, document))
        self.assertIn('"pick_directions": {"forward": {"budget_spent": 0, "joined": 0, '
                      '"scanned": 3}', recorded)
        for value in ("SO100305", "SO3000", "so-b", "PSI-", "bc://", "api://"):
            self.assertNotIn(value, text)
        # The pick's clock spent: the command still ends and prints its counts, red (no
        # cross-rooted citation) with the forward cut counted, never a silent timeout.
        with mock.patch.object(live_ws, "PICK_BUDGET_SECONDS", 0.0):
            code, document, _text = run(None, bc_world([*unmatched,
                                                        invoice(90, "SO100305", "01")]))
        self.assertIn("cited_cross_rooted", json.dumps(lv.check_expectations(spec, code,
                                                                             document)))
        self.assertEqual((document["pick_directions"]["forward"]["budget_spent"],
                          document["pick_directions"]["reverse"]["tried"]), (1, 0), document)
        # ... and with no twin the check stays red with the typed reasons and the counts.
        with mock.patch.object(live_ws, "PICK_SCAN", 3):
            code, document, _text = run(None, bc_world(unmatched))
        self.assertIn("cited_cross_rooted", json.dumps(lv.check_expectations(spec, code,
                                                                             document)))
        self.assertEqual((document["pick_root_reasons"],
                          document["pick_directions"]["reverse"]["linked_matched"]),
                         ({"no_portal_row_for_external_document_number": 3}, 0))

        def portal_down(url, headers, *, max_bytes):
            raise SourceHttpError("HTTP 500", status=500, error_code="internal")

        code, document, _text = run(portal_down)
        self.assertTrue(lv.blocked_parts(document), document)
        self.assertEqual({case["status"] for case in document["cases"]}, {"blocked"})
        self.assertEqual(lv.check_expectations(spec, code, document, spec["expect_blocked"]),
                         [], document)

    def test_a_disjoint_replica_reads_blocked_source_gap_and_other_causes_still_fail(self):
        """gap-6d35a1af53db: BC drains from another environment than the served replica,
        so the two share no order. The pick that ran both directions to the end and
        joined nothing (every reason no_portal_row_* / not_in_served_replica, reverse hits
        0) reads blocked source_gap with failed 0; a cut clock, another reason or an
        intersecting fixture does not; month buckets are counts only."""
        from devloop import live_verify as lv
        from integrations.tests.test_live_source_adapters import ENV

        spec = json.loads((ROOT / "devloop" / "live-checks" / "case.unfamiliar_order.json")
                          .read_text("utf-8"))
        self.assertEqual(lv.validate_spec(spec, stem="case.unfamiliar_order"), [])
        environ = {**ENV, "PLX_WORKBENCH_API_URL": BASE, "PLX_WORKBENCH_API_KEY": KEY}

        def run(world):
            code, text = spec_main(spec["command"][3:], None, world, environ=environ)
            return code, json.loads(text), text

        disjoint = [invoice(10 + n, f"SO3{n:05d}", f"{10 + n:02d}") for n in range(3)]
        with mock.patch.object(live_ws, "PICK_SCAN", 3):
            code, document, text = run(bc_world(disjoint))
            self.assertEqual(lv.resolve(document, spec["blocked_when"]),
                             ["not_in_served_replica"], document)
            self.assertEqual((spec["blocked_cause"], code, document["failed"],
                              document["blocked"], document["cited_cross_rooted"]),
                             ("source_gap", 0, 0, 1, 0), document)
            self.assertEqual(document["pick_date_months"]["SalesInvoice"], {"2026-09": 3})
            self.assertTrue(document["pick_date_months"]["SalesOrder"])
            for value in ("SO3000", "PSI-", "so-a", "bc://", "api://"):
                self.assertNotIn(value, text)
            # Another reason in the mix (a customer PO of another customer: scope_mismatch)
            # still fails, with no blocked string.
            code, document, _text = run(bc_world([*disjoint[:2], native("CPO-7781", "C0002")],
                                                 CARDS))
            self.assertEqual(document["pick_root_reasons"].get("scope_mismatch"), 1,
                             document)
            self.assertNotIn("pick_blocked", document)
            self.assertEqual((document["failed"], document["blocked"]), (1, 0))
            # An intersecting fixture still passes with a cross-rooted case.
            code, document, _text = run(bc_world([*disjoint, invoice(90, "SO100305", "01")]))
            self.assertNotIn("pick_blocked", document)
            self.assertEqual(lv.check_expectations(spec, code, document), [], document)
            self.assertGreaterEqual(document["cited_cross_rooted"], 1)
        # BC not answering the reverse lookups (HTTP 500) is no evidence of none.
        class Down(LookupBc):
            def __call__(self, method, url, headers, body, timeout, max_bytes):
                if "$filter" in unquote(url) and "documentId" not in unquote(url):
                    return 500, {}, b"{}"
                return super().__call__(method, url, headers, body, timeout, max_bytes)

        down = Down({name: 0 for name in CAPTURE_ENTITY_SETS}, companies=tb.CO)
        down.data = bc_world(disjoint).data
        down.lines = bc_world(disjoint).lines
        with mock.patch.object(live_ws, "PICK_SCAN", 3):
            code, document, _text = run(down)
        self.assertNotIn("pick_blocked", document)
        self.assertEqual((document["failed"], document["blocked"]), (1, 0), document)
        self.assertNotEqual(document["pick_directions"]["reverse"]["bc"]["lookup_states"],
                            {"read": 18})
        # The pick's clock spent: a cut direction is no evidence of none.
        with mock.patch.object(live_ws, "PICK_SCAN", 3), \
                mock.patch.object(live_ws, "PICK_BUDGET_SECONDS", 0.0):
            code, document, _text = run(bc_world(disjoint))
        self.assertNotIn("pick_blocked", document)
        self.assertEqual(document["failed"], 1)

    def test_the_spec_passes_under_the_verifier_guard_with_graph_set_but_undeclared(self):
        """case.unfamiliar_order through ``live_verify.run_spec`` with the Graph credentials
        set and undeclared: the verifier builds the child env and its hidden names from the
        spec, and the command runs in-process under that env, guarded as ``ENV_GUARD`` guards
        it. Its BC-only live audience builds no Graph reader (``build_live_readers``, the one
        filter), so it passes reading no Graph or SharePoint variable."""
        from devloop import live_verify as lv
        from integrations.tests.test_live_source_adapters import ENV
        from tests.test_live_readers_filter import GRAPH_PREFIXES, GuardedEnv, Undeclared

        spec = json.loads((ROOT / "devloop" / "live-checks" / "case.unfamiliar_order.json")
                          .read_text("utf-8"))
        self.assertFalse([name for name in (*spec["requires_env"],
                                            *spec.get("optional_env", []))
                          if name.startswith(GRAPH_PREFIXES)])
        seen = {}

        class InProcess:
            def __init__(self, argv, env, **kwargs):
                assert argv[1:3] == ["-c", lv.ENV_GUARD] and argv[4] == spec["command"][2]
                guarded = seen["env"] = GuardedEnv(env, filter(None, argv[3].split(",")))
                self.stderr = b""
                try:
                    with mock.patch.object(os, "environ", guarded):
                        self.returncode, text = spec_main(argv[5:])
                except Undeclared as read:
                    self.returncode, text = lv.UNDECLARED_ENV_EXIT, ""
                    self.stderr = json.dumps({"error": "UndeclaredEnv",
                                              "name": read.args[0]}).encode()
                self.stdout = text.encode()

            def communicate(self, timeout=None):
                return self.stdout, self.stderr

        with mock.patch.object(lv.subprocess, "Popen", InProcess):
            entry = lv.run_spec(spec, environ={
                **ENV, "PLX_WORKBENCH_API_URL": BASE, "PLX_WORKBENCH_API_KEY": KEY})
        self.assertIn("MICROSOFT_GRAPH_CLIENT_SECRET", seen["env"].hidden)
        self.assertEqual(entry["result"], "pass", entry.get("detail"))
        self.assertFalse([name for name in seen["env"].read if name.startswith(GRAPH_PREFIXES)])

    def test_no_case_identifier_is_in_the_generic_opener(self):
        tree = ast.parse((ROOT / "operating" / "case_workspace_live.py").read_text("utf-8"))
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
                      and node.body and isinstance(node.body[0], ast.Expr)}
        literals = " ".join(node.value for node in ast.walk(tree)
                            if isinstance(node, ast.Constant) and isinstance(node.value, str)
                            and id(node) not in docstrings)
        for value in ("SO1002", "SO1003", "PO1007", "PO1008", "CPO-", "PSI-", "AO-27001A",
                      "OR1000151885", "PO12172025", "5447", "26005"):
            self.assertNotIn(value, literals)

    def test_an_absence_the_tools_report_is_recorded_unknown_or_inaccessible(self):
        recorded = []

        def call(tool, arguments):
            recorded.append(arguments)
            return {}
        for upstream, state in ((None, "unknown"), (["source_error:x"], "inaccessible")):
            recorded.clear()
            live_ws._Recorder(call, "case-1", upstream=upstream).tool_gaps(
                [{"state": "absent_in_source", "reason": "no_match",
                  "subject": "SalesOrder query"}], "plx.find")
            [gap] = recorded
            self.assertEqual(gap["state"], state)
            self.assertIn("no cited row shows the absence" if upstream is None
                          else "no evidence of absence", gap["why"])


if __name__ == "__main__":
    unittest.main()
