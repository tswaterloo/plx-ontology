"""BC-only three-way eval (scripts/eval_bc_three_way.py) at its output contract.

Every request goes to the in-process ``FakeSender`` (BC API v2.0 shapes, honouring
``$filter``/``$select``/``$top``) through the real ``bc_read`` reader, ``bc_purchase``,
projector and ``plx.process``. Records are synthetic; no real case id appears. Evidence
label: injected-transport (the live check stays live-unverified).
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock
from urllib.error import URLError
from urllib.parse import unquote, urlsplit

from verify import live_verify
from integrations.tests.test_bc_purchase import BASE, collection, gid, header, line
from integrations.tests.test_live_source_adapters import COMPANY, ENV, FakeSender, j
from scripts import eval_bc_three_way as ev

C = f"{BASE}/companies({COMPANY})"
BOOKS = {COMPANY: "manufacturing"}
# Live 2026-10-08 samples span three companies.
COMPANIES = (COMPANY, "0f0e0d0c-0b0a-0908-0706-0504030201ff",
             "0f0e0d0c-0b0a-0908-0706-0504030202ff")
SPEC = Path(__file__).resolve().parents[1] / "devloop/live-checks/eval.bc_three_way.json"


class World:
    """One BC company. ``add`` declares a PO by number with its ordered line (of ``item``),
    posted receipts (quantity, unit) and invoices (quantity, status)."""

    def __init__(self, company: str = COMPANY, base: int = 0) -> None:
        """``base`` offsets every record id (one per company: BC ids are global)."""
        self.orders, self.receipts, self.invoices, self.items = [], [], [], {}
        self.company, self.base = company, base
        self.routes: dict = {
            f"{BASE}/companies": j(200, {"value": [{"id": company, "name": "Books"}]})}
        self.n = base

    @property
    def c(self) -> str:
        return f"{BASE}/companies({self.company})"

    def _id(self) -> int:
        self.n += 1
        return self.n

    def add(self, number: str, *, ordered=100, receipts=(), invoices=(), in_bc=True,
            status="Open", recorded=None, invoiced=None, gl_lines=False, item="ITEM-1",
            line_page=0) -> None:
        """``recorded`` / ``invoiced``: the PO line's own receivedQuantity /
        invoicedQuantity (live BC always carries both);
        ``gl_lines``: non-item lines as live documents have them (a PO comment line, a
        G/L Account freight line and an item charge on each invoice)."""
        po = self._id()
        self.items.setdefault(item, header(self.base + 900 + len(self.items), number=item,
                                           displayName="Bottle", type="Inventory",
                                           baseUnitOfMeasureCode="PCS"))
        if in_bc:
            self.orders.append(header(po, number=number, vendorNumber="V1", vendorId=gid(800),
                                      status=status, currencyCode="USD"))
            po_lines = [line(po, po, sequence=10000, lineType="Item", lineObjectNumber=item,
                             unitOfMeasureCode="PCS", quantity=ordered, directUnitCost=1.5,
                             **({} if recorded is None else {"receivedQuantity": recorded}),
                             **({} if invoiced is None else {"invoicedQuantity": invoiced}))]
            if gl_lines:
                po_lines.append(line(300 + po, po, sequence=20000, lineType="Comment",
                                     lineObjectNumber="", unitOfMeasureCode="", quantity=0,
                                     directUnitCost=0))
            self.routes[f"{self.c}/purchaseOrders({gid(po)})/purchaseOrderLines"] = \
                collection(po_lines, page=line_page)
        for qty, unit in receipts:
            r = self._id()
            self.receipts.append(header(r, number=f"RCPT-{r}", orderNumber=number,
                                        vendorNumber="V1"))
            self.routes[f"{self.c}/purchaseReceipts({gid(r)})/purchaseReceiptLines"] = collection([
                line(r, r, sequence=10000, lineType="Item", lineObjectNumber=item,
                     unitOfMeasureCode=unit, quantity=qty, unitCost=1.5)])
        for qty, inv_status in invoices:
            i = self._id()
            self.invoices.append(header(i, number=f"PINV-{i}", orderNumber=number,
                                        orderId=gid(po), vendorNumber="V1", vendorId=gid(800),
                                        status=inv_status, currencyCode="USD"))
            invoice_lines = [line(i, i, sequence=10000, lineType="Item",
                                  lineObjectNumber=item, unitOfMeasureCode="PCS",
                                  quantity=qty, unitCost=1.5)]
            if gl_lines:   # G/L numbers can equal item Nos.: never read or counted as items
                invoice_lines += [
                    line(300 + i, i, sequence=20000, lineType="Account",
                         lineObjectNumber=item, unitOfMeasureCode="", quantity=1,
                         unitCost=25),
                    line(400 + i, i, sequence=30000, lineType="Charge",
                         lineObjectNumber="FREIGHT", unitOfMeasureCode="", quantity=1,
                         unitCost=10)]
            self.routes[f"{self.c}/purchaseInvoices({gid(i)})/purchaseInvoiceLines"] = \
                collection(invoice_lines)

    def sender(self, failing: dict | None = None) -> FakeSender:
        routes = dict(self.routes)
        routes[f"{self.c}/purchaseOrders"] = collection(self.orders)
        routes[f"{self.c}/purchaseReceipts"] = collection(self.receipts)
        routes[f"{self.c}/purchaseInvoices"] = collection(self.invoices)
        routes.setdefault(f"{self.c}/items", collection(list(self.items.values())))
        for path, status in (failing or {}).items():
            routes[path] = j(status, {"error": {"code": "Internal_ServerError"}})
        return FakeSender(routes)


def run(world_or_sender, *args: str, books=BOOKS, failing=None, clock=None
        ) -> tuple[int, dict, str, FakeSender]:
    """One eval run; ``clock`` (a ``Clock``) is the run's time and its backoff sleep,
    otherwise real time with no backoff sleep."""
    sender = world_or_sender.sender(failing) if isinstance(world_or_sender, World) \
        else world_or_sender
    timing = {"clock": clock, "sleep": clock.sleep} if clock else {"sleep": lambda _: None}
    out = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
        code = ev.main(["live", "--work-dir", tmp, "--run-id", "fixture-run", "--count", "8",
                        "--min-scored", "1", *args], environ=ENV, sender=sender, books=books,
                       **timing)
    text = out.getvalue()
    return code, json.loads(text), text, sender


class Clock:
    """The run's injected time: advanced by the transport's latency and the backoff."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class LatencySender:
    """The live cost of each BC GET attempt on a ``Clock``: ``ok`` seconds when answered;
    every ``fail_every``-th attempt (the first ``failures_max``; None: all) hangs ``failed``
    seconds and fails before any status with ``error`` (live: retried.urlerror; its URL
    kept in ``failed_urls``); a ``throttled`` entity set answers 429 with ``Retry-After``
    after ``failed`` seconds: in ``company`` only (None: every company), from its ``after``-th
    attempt on, ``throttles`` times (None: always). ``sent`` counts attempts per entity set,
    ``throttled_sent`` those that matched the throttle."""

    def __init__(self, sender: FakeSender, clock: Clock, *, ok: float, failed: float,
                 fail_every: int = 0, throttled: str | None = None,
                 retry_after: int = 10, company: str | None = None, after: int = 0,
                 throttles: int | None = None, error: Exception | None = None,
                 failures_max: int | None = None) -> None:
        self.sender, self.clock = sender, clock
        self.error, self.failures_max, self.failed_urls = error, failures_max, []
        self.ok, self.failed, self.fail_every = ok, failed, fail_every
        self.throttled, self.retry_after = throttled, retry_after
        self.company, self.after, self.throttles = company, after, throttles
        self.attempts, self.failures, self.sent = 0, 0, Counter()
        self.throttled_sent = 0

    def __getattr__(self, name):
        return getattr(self.sender, name)

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        if urlsplit(url).hostname == "login.microsoftonline.com":
            return self.sender(method, url, headers, body, timeout, max_bytes)
        self.attempts += 1
        entity_set = unquote(urlsplit(url).path).rsplit("/", 1)[-1].split("(")[0]
        self.sent[entity_set] += 1
        if (self.fail_every and self.attempts % self.fail_every == 0
                and (self.failures_max is None or self.failures < self.failures_max)):
            self.clock.now += self.failed
            self.failures += 1
            self.failed_urls.append(url)
            raise self.error or URLError("timed out")
        n = 0
        if entity_set == self.throttled and (
                self.company is None or f"companies({self.company})" in unquote(url)):
            self.throttled_sent += 1
            n = self.throttled_sent - self.after
        if n > 0 and (self.throttles is None or n <= self.throttles):
            self.clock.now += self.failed
            return (429, {"retry-after": str(self.retry_after)},
                    json.dumps({"error": {"code": "TooManyRequests"}}).encode())
        self.clock.now += self.ok
        return self.sender(method, url, headers, body, timeout, max_bytes)


# Measured on the live-results branch (eval.bc_three_way, 17 runs 2026-10-07/08): least squares
# of duration over GETs and retries (requests_sent - reads.sent - 1) gives 16 s per failed
# attempt. The slowest hour (bb601a96, 10:33Z, run_budget_exhausted at 483 s with 8 of 14
# items scored): 128 GETs, 24 failed attempts, so 0.77 s per answered attempt; one attempt
# in 6 failed (24 of 152).
LIVE_FAILED_ATTEMPT_SECONDS = 16.0
LIVE_OK_SECONDS = (483 - 24 * LIVE_FAILED_ATTEMPT_SECONDS) / 128
LIVE_FAIL_EVERY = 6


def live_shaped_world() -> World:
    """15 roots shaped like the live 120acb30 sample: 10 received (every third in two
    receipts, two invoiced) and 5 with no receipt or invoice, which the oracle decided
    nothing of before its PO lines' figures proved them absent; an item of its own each."""
    world = World()
    for k in range(10):
        world.add(f"PO9002{k:02d}", receipts=[(60, "PCS"), (40, "PCS")] if k % 3 == 0
                  else [(100, "PCS")], invoices=[(100, "Open")] if k < 2 else (),
                  recorded=100, invoiced=100 if k < 2 else 0, gl_lines=True,
                  item=f"ITEM-{k}")
    for k in range(5):
        world.add(f"PO9003{k:02d}", recorded=0, invoiced=0, item=f"ITEM-U{k}")
    return world


def live_count_world() -> tuple[FakeSender, dict]:
    """The live 10:33Z sample's shape over three companies: 8 POs received (3 to 7 posted
    receipts each, 2 invoiced) and 7 with no receipt or invoice (before decision
    2026-10-14-three-way-absence-proven the oracle decided nothing of them), 15 roots in
    all. Every PO line carries its receivedQuantity and invoicedQuantity, as live BC's do."""
    worlds = [World(company, base=10_000 * k) for k, company in enumerate(COMPANIES)]
    for k in range(8):
        partial = [(10, "PCS")] * (2 + k % 5)
        worlds[k % 3].add(f"PO9004{k:02d}", receipts=[(100 - 10 * len(partial), "PCS"),
                                                      *partial],
                          invoices=[(100, "Open")] if k < 2 else (), recorded=100,
                          invoiced=100 if k < 2 else 0, gl_lines=True, item=f"ITEM-{k}")
    for k in range(7):
        worlds[k % 3].add(f"PO9005{k:02d}", recorded=0, invoiced=0, item=f"ITEM-U{k}")
    routes = {}
    for world in worlds:
        routes.update(world.sender().routes)
    routes[f"{BASE}/companies"] = j(200, {"value": [{"id": c, "name": "Books"}
                                                    for c in COMPANIES]})
    return FakeSender(routes), {c: "manufacturing" for c in COMPANIES}


def by_outcome(report: dict) -> dict:
    return {o: report["eval"][o] for o in
            ("sampled", "blocked_upstream", "not_held", "excluded", "scored", "correct",
             "wrong", "abstain_ok")}


class OutputContractTests(unittest.TestCase):
    def test_stage_only_match_is_correct_and_unsupported_sides_abstain(self):
        world = World()
        # Receipt in another unit and no invoice: the oracle decides only the stages.
        world.add("PO900101", receipts=[(100, "BOX")])
        # Fully received and invoiced: the oracle decides both sides, and so does the
        # product from its complete per-PO read (scoped claims; Portal not captured, so
        # decided from BC alone with cross_system_unchecked).
        world.add("PO900102", receipts=[(100, "PCS")], invoices=[(100, "Open")])
        code, report, _, _ = run(world)
        self.assertEqual(code, 0, report["eval"]["fail_reasons"])
        self.assertEqual(by_outcome(report), {"sampled": 2, "blocked_upstream": 0,
                                              "not_held": 0, "excluded": 0, "scored": 2,
                                              "correct": 2, "wrong": 0, "abstain_ok": 0})
        self.assertEqual(report["eval"]["coverage"], "2/2")
        self.assertEqual(report["eval"]["accuracy"], "2/2")
        self.assertEqual(report["eval"]["answered"], "5/5")
        aspects = report["eval"]["by_aspect"]
        self.assertEqual(aspects["stage:receipt"]["agreed"], 2)
        self.assertEqual(aspects["stage:vendor_invoice"]["agreed"], 1)
        self.assertEqual(aspects["side:ordered_received"]["agreed"], 1)
        self.assertEqual(aspects["side:received_invoiced"]["agreed"], 1)
        # Root lookup is reported apart; it never makes an item three-way correct.
        self.assertEqual(report["eval"]["root_lookup"]["found_exact"], 2)
        self.assertTrue(report["eval"]["pass"])
        self.assertEqual(report["evidence_label"], "injected-transport")

    def test_live_shaped_documents_decide_both_sides_and_keep_conflicts_undecided(self):
        # Live 2026-10-05 (ff2879cb): side:ordered_received undecided 8/8 and
        # received_invoiced 2/2 with every stage agreed. Live PO lines carry their own
        # receivedQuantity and documents carry non-item lines (comment, G/L freight,
        # item charge): neither may keep a decidable side undecided.
        world = World()
        world.add("PO900161", receipts=[(60, "PCS"), (40, "PCS")], invoices=[(100, "Open")],
                  recorded=100, gl_lines=True)
        world.add("PO900162", ordered=80, receipts=[(50, "PCS")], invoices=[(50, "Open")],
                  recorded=50, gl_lines=True)
        # BC's own figure disagrees with its posted receipt lines: never decided.
        world.add("PO900163", receipts=[(100, "PCS")], recorded=0, gl_lines=True)
        world.add("PO900164")   # oracle decides nothing beyond the root
        code, report, _, sender = run(world)
        aspects = report["eval"]["by_aspect"]
        self.assertEqual(aspects["side:ordered_received"],
                         {"agreed": 2, "disagreed": 0, "undecided": 1, "unsupported": 0})
        self.assertEqual(aspects["side:received_invoiced"],
                         {"agreed": 2, "disagreed": 0, "undecided": 0, "unsupported": 0})
        self.assertEqual(by_outcome(report), {"sampled": 4, "blocked_upstream": 0,
                                              "not_held": 0, "excluded": 1, "scored": 3,
                                              "correct": 2, "wrong": 0, "abstain_ok": 1})
        self.assertEqual(code, 0, report["eval"]["fail_reasons"])
        # Non-item numbers are never looked up as items, and an excluded root spends no
        # product read (the product alone reads invoices by orderId).
        item_reads = [u for _, u, *_ in sender.api_calls() if urlsplit(u).path == f"{C}/items"]
        self.assertTrue(item_reads)
        self.assertFalse([u for u in item_reads if "FREIGHT" in u])
        self.assertFalse([u for _, u, *_ in sender.api_calls()
                          if f"orderId eq {gid(10)}" in unquote(u)])

    def test_absent_stages_need_a_complete_zero_read_on_both_sides(self):
        # Decision 2026-10-14-three-way-absence-proven: the oracle and the product each
        # decide receipt / vendor_invoice absent_in_bc only from a complete read of every
        # item line's zero receivedQuantity / invoicedQuantity beside a complete query
        # of the posted documents; a partly received line or an incomplete read stays
        # undecided, and an order whose figures are missing decides nothing.
        world = World()
        world.add("PO900171", recorded=0, invoiced=0, item="ITEM-Z")
        world.add("PO900172", recorded=40, invoiced=0, item="ITEM-P")
        world.add("PO900173", item="ITEM-N")
        # two lines served one per page, read at most one page: incomplete
        world.add("PO900174", recorded=0, invoiced=0, gl_lines=True, line_page=1,
                  item="ITEM-I")
        answers, product_aspects = [], ev.product_aspects
        with mock.patch.object(ev, "product_aspects",
                               lambda result: answers.append(product_aspects(result))
                               or answers[-1]), \
                mock.patch.dict(ev.LINE_READ, {"max_pages": 1}):
            code, report, _, _ = run(world)
        self.assertEqual(code, 0, report["eval"]["fail_reasons"])
        self.assertEqual(by_outcome(report), {"sampled": 4, "blocked_upstream": 0,
                                              "not_held": 0, "excluded": 2, "scored": 2,
                                              "correct": 2, "wrong": 0, "abstain_ok": 0})
        aspects = report["eval"]["by_aspect"]
        # the zero order agrees on both absent stages, the partly received one on its
        # absent invoice only: the oracle leaves its receipt undecided, and so does the
        # product
        self.assertEqual(aspects["stage:receipt"]["agreed"], 1)
        self.assertEqual(aspects["stage:vendor_invoice"]["agreed"], 2)
        self.assertEqual(sorted((a["stage:receipt"] or "") for a in answers), ["", "absent"])
        self.assertEqual([a["stage:vendor_invoice"] for a in answers], ["absent", "absent"])

    def test_confident_wrong_answer_is_counted_wrong_and_fails_the_bound(self):
        world = World()
        world.add("PO900111", receipts=[(60, "PCS")], invoices=[(60, "Open")])
        world.add("PO900112", receipts=[(100, "PCS")])
        confident = {"stage:purchase_order": "present", "stage:receipt": "present",
                     "stage:vendor_invoice": "present", "side:ordered_received": True,
                     "side:received_invoiced": True}
        with mock.patch.object(ev, "product_aspects", return_value=confident):
            code, report, _, _ = run(world)
        # PO900111 is short-received (60 of 100): True is wrong. PO900112 has no invoice the
        # oracle can prove absent, so a "present" there is not scored, and the rest agree.
        self.assertEqual(report["eval"]["wrong"], 1)
        self.assertEqual(report["eval"]["correct"], 1)
        wrong = [i for i in report["items"] if i.get("outcome") == "wrong"]
        self.assertEqual(wrong[0]["wrong_aspects"], ["side:ordered_received"])
        self.assertFalse(report["eval"]["wrong_within_bound"])
        self.assertIn("wrong_above_bound", report["eval"]["fail_reasons"])
        self.assertEqual(code, 3)

    def test_product_decision_on_an_incomplete_oracle_read_is_unsupported(self):
        truth = {name: {"value": None, "reason": "stage_not_present"}
                 for name in ev.three_way.ASPECTS}
        truth["stage:purchase_order"] = {"value": "present", "reason": "po_issued"}
        truth["stage:receipt"] = {"value": None, "reason": "bc_read_incomplete"}
        scored = ev.score({"aspects": truth}, {"stage:purchase_order": "present",
                                               "stage:receipt": "absent"})
        self.assertEqual(scored["outcome"], "wrong")
        self.assertEqual(scored["wrong_aspects"], ["stage:receipt"])

    def test_upstream_failure_blocks_items_and_zero_scored_fails_with_its_cause(self):
        world = World()
        world.add("PO900121", receipts=[(100, "PCS")])
        world.add("PO900122")
        code, report, _, _ = run(world, failing={f"{C}/purchaseReceipts": 500})
        self.assertEqual(report["eval"]["blocked_upstream"], 2)
        self.assertEqual(report["eval"]["scored"], 0)
        self.assertEqual(report["eval"]["blocked"]["by_http_status"], {"500": 2})
        self.assertEqual(report["eval"]["blocked"]["by_entity_set"], {"purchaseReceipts": 2})
        self.assertEqual(report["eval"]["zero_scored_cause"], "blocked_upstream")
        self.assertIn("no_scored_items", report["eval"]["fail_reasons"])
        self.assertEqual(report["sample"]["pool_failures"][0]["http_status"], 500)
        self.assertEqual(code, 3)

    def test_a_product_tool_error_is_counted_apart_and_fails_the_run(self):
        world = World()
        world.add("PO900126", receipts=[(100, "BOX")])   # stage-only: correct when answered
        world.add("PO900127", receipts=[(100, "BOX")])
        opened, calls = ev.open_product, []

        def failing_second_process(*args, **kwargs):
            product = opened(*args, **kwargs)
            call = product.call

            def wrapped(name, arguments):
                if name == "plx.process":
                    calls.append(name)
                    if len(calls) == 2:
                        return {"error": {"code": "internal_error"}}
                return call(name, arguments)
            product.call = wrapped
            return product

        with mock.patch.object(ev, "open_product", failing_second_process):
            code, report, _, _ = run(world)
        self.assertEqual(by_outcome(report), {"sampled": 2, "blocked_upstream": 0,
                                              "not_held": 0, "excluded": 0, "scored": 1,
                                              "correct": 1, "wrong": 0, "abstain_ok": 0})
        self.assertEqual(report["eval"]["product_error"], 1)
        self.assertEqual(report["eval"]["product_error_by_code"], {"internal_error": 1})
        self.assertEqual(report["eval"]["fail_reasons"], ["product_error"])
        self.assertEqual(code, 3)

    def test_an_oracle_read_without_permission_is_not_held_never_blocked(self):
        world = World()
        world.add("PO900128", receipts=[(100, "PCS")])
        find = ev.CompanyBcPort.find

        def refused(port, entity_set, field, value):
            if entity_set == "purchaseReceipts":   # the audience refuses this read
                raise ev.NotVisible()
            return find(port, entity_set, field, value)

        with mock.patch.object(ev.CompanyBcPort, "find", refused):
            code, report, _, _ = run(world)
        self.assertEqual((report["eval"]["blocked_upstream"], report["eval"]["not_held"]),
                         (0, 1))
        self.assertEqual(report["eval"]["not_held_by_reason"], {"oracle_not_permitted": 1})
        self.assertEqual(code, 3)

    def test_product_side_read_failure_is_blocked_with_its_status(self):
        world = World()
        world.add("PO900125", receipts=[(100, "PCS")])
        code, report, _, _ = run(world, failing={f"{C}/items": 500})
        [item] = report["items"]
        self.assertEqual((item["status"], item["reason"], item["entity_set"],
                          item["http_status"], item["detail"]),
                         ("blocked", "product_read_failed", "items", 500, "http_500"))
        self.assertEqual(code, 3)
        # Live 2026-10-05: two items reads failed with no HTTP status and no error code;
        # the cause still shows, as its fixed-vocabulary detail.
        world.routes[f"{C}/items"] = j(200, {"value": [{"id": gid(900), "number": "ITEM-1"}]})
        code, report, _, _ = run(world)
        [item] = report["items"]
        self.assertNotIn("http_status", item)
        self.assertEqual((item["reason"], item["entity_set"], item["detail"]),
                         ("product_read_failed", "items", "record_invalid"))
        self.assertEqual(report["eval"]["blocked"]["by_detail"], {"record_invalid": 1})

    def test_unpinned_company_is_not_held_and_a_deleted_order_is_excluded(self):
        world = World()
        world.add("PO900131", receipts=[(100, "PCS")])
        world.add("PO900132", receipts=[(100, "PCS")], in_bc=False)  # fully done, deleted
        code, report, _, _ = run(world, books={})
        self.assertEqual(by_outcome(report), {"sampled": 2, "blocked_upstream": 0,
                                              "not_held": 1, "excluded": 1, "scored": 0,
                                              "correct": 0, "wrong": 0, "abstain_ok": 0})
        self.assertEqual(report["eval"]["not_held_by_reason"], {"bc_company_unpinned": 1})
        self.assertEqual(report["eval"]["excluded_by_reason"], {"order_not_in_source": 1})
        self.assertEqual(report["eval"]["zero_scored_cause"], "excluded")
        self.assertEqual(code, 3)

    def test_bc_only_get_only_seeded_and_no_values_published(self):
        world = World()
        for k in range(6):
            world.add(f"PO90014{k}", receipts=[(100, "PCS")])
        code, first, text, sender = run(world)
        _, again, _, _ = run(world)
        self.assertEqual(first["items_digest"], again["items_digest"])
        self.assertEqual([i["item"] for i in first["items"]],
                         [i["item"] for i in again["items"]])
        hosts = {urlsplit(c[1]).hostname for c in sender.calls}
        self.assertEqual(hosts, {"login.microsoftonline.com", "api.businesscentral.dynamics.com"})
        self.assertEqual({c[0] for c in sender.api_calls()}, {"GET"})
        self.assertEqual(first["row_data_check"]["leaked"], 0)
        self.assertNotIn("PO90014", text)
        self.assertNotIn(COMPANY, text)
        self.assertEqual((first["eval"]["correct"], first["eval"]["coverage"]), (6, "6/6"))
        self.assertEqual(code, 0, first["eval"]["fail_reasons"])
        # A product that abstains on every decided aspect: full coverage never passes alone.
        with mock.patch.object(ev, "product_aspects", return_value={}):
            code, abstained, _, _ = run(world)
        self.assertEqual((abstained["eval"]["abstain_ok"], abstained["eval"]["coverage"]),
                         (6, "6/6"))
        self.assertEqual(abstained["eval"]["fail_reasons"], ["no_correct_answer"])
        self.assertEqual(code, 3)

    def test_live_call_counts_and_latency_finish_fifteen_items_within_budget(self):
        # Live 10:33Z: run_budget_exhausted at 483 s, 8 of 14 items scored, with 24 failed
        # attempts at about 16 s each (the time grew with them, run over run). The same
        # counts and per-attempt cost on an injected clock now finish all 15 items within
        # the budget, and the report says where the time went. Since decision
        # 2026-10-14-three-way-absence-proven the 7 POs with no receipt are decided absent
        # from their PO lines' figures and read by the product too: the run reads every
        # item in full, which the 600 s budget covers at this hour's failure rate.
        clock = Clock()
        sender, books = live_count_world()
        slow = LatencySender(sender, clock, ok=LIVE_OK_SECONDS,
                             failed=LIVE_FAILED_ATTEMPT_SECONDS, fail_every=LIVE_FAIL_EVERY)
        code, report, _, _ = run(slow, "--count", "15", books=books, clock=clock)
        self.assertEqual(code, 0, report["eval"]["fail_reasons"])
        self.assertEqual(by_outcome(report), {"sampled": 15, "blocked_upstream": 0,
                                              "not_held": 0, "excluded": 0, "scored": 15,
                                              "correct": 15, "wrong": 0, "abstain_ok": 0})
        self.assertGreaterEqual(slow.failures, 20)
        self.assertLessEqual(clock.now, ev.DEFAULT_BUDGET)
        # Shared pages: every answered GET is sent once.
        urls = Counter(u for _, u, *_ in sender.api_calls())
        self.assertEqual([u for u, n in urls.items() if n > 1], [])
        self.assertGreater(report["reads"]["shared"], report["reads"]["sent"] // 2)
        # Per phase: seconds that add up to the run, GETs, retries and failed attempts.
        cost, phases = report["cost"], report["cost"]["phases"]
        self.assertEqual(cost["elapsed_seconds"], round(clock.now, 2))
        self.assertAlmostEqual(sum(p["seconds"] for p in phases.values()), clock.now, delta=0.1)
        self.assertEqual(sum(p["gets"] for p in phases.values()), report["reads"]["sent"])
        self.assertEqual(sum(p["retries"] for p in phases.values()), slow.failures)
        self.assertEqual(sum(p["failed_attempts"] for p in phases.values()), slow.failures)
        self.assertEqual(cost["retries"]["by_cause"],
                         {"transport": slow.failures, "http": 0, "retry_after": 0})
        self.assertAlmostEqual(cost["retries"]["failed_attempt_seconds_by_cause"]["transport"],
                               slow.failures * LIVE_FAILED_ATTEMPT_SECONDS, delta=0.1)
        self.assertAlmostEqual(phases["oracle"]["slowest_answer_seconds"], LIVE_OK_SECONDS,
                               delta=0.01)
        # No item is excluded; each reads its PO lines once, the product's from the oracle's.
        self.assertEqual(phases["oracle_excluded"]["gets"], 0)
        self.assertEqual(len([u for u in urls
                              if urlsplit(u).path.endswith("/purchaseOrderLines")]), 15)
        self.assertGreater(phases["product"]["gets"], 0)
        spec = json.loads(SPEC.read_text())
        command = spec["command"]
        self.assertEqual(float(command[command.index("--total-budget-seconds") + 1]),
                         ev.DEFAULT_BUDGET)
        self.assertEqual(int(command[command.index("--count") + 1]), 15)
        self.assertGreater(spec["timeout_seconds"], ev.DEFAULT_BUDGET)

    def test_a_timed_throttle_is_waited_out_only_when_the_rest_still_fits(self):
        # Live: one 429 naming 10 s (beyond the backoff cap, not retried) on an item's read
        # held the set and 13 of 15 items came back blocked_upstream at once. Now the read
        # waits the 10 s out inside the run's budget and is sent again: every item scores,
        # at the live failure rate too.
        # A 480 s wait also ended inside the 540 s budget but left no time for the unread
        # items (5 of 15 sampled, incomplete: a false red). At the live cost (one attempt in
        # 6 failing after 16 s) a wait that leaves less than the measured per-GET cost of
        # the unread items is not slept: the set is held, its company's items are blocked
        # upstream by name and the run completes. So too for a throttle while sampling: the
        # items still to be drawn keep their time.
        # Review (a393ddc6): at the live failure rate a 10 or 30 s Retry-After after the
        # first items was held and an item blocked, although the wait fit: the first items
        # read (here all with receipts, ~15 times an excluded item) were the measure of the
        # rest. Now they weigh beside PRIOR_ITEMS items at the priors and the wait is slept,
        # so too a 10 s wait after 8 invoice reads at one attempt in 8 failing (decision 15
        # amendment: at one in 6 that wait fits only because the items left are mostly
        # excluded, which no per-item mean can know). 60 s is held: waited, this run ends at
        # 542 s, past the budget, and one item is blocked.
        # Decision 2026-10-14-three-way-absence-proven: no item is excluded any more (the
        # 7 unreceived POs are decided absent and read in full), so at one in 6 a 10 s wait
        # after 4 invoice reads or a 30 s one after 3 no longer fits the reserve and is held
        # (one item blocked). A 480 s throttle while sampling with no failures holds the
        # set while its unread items need the time (its company's first 5 reads blocked)
        # and is slept once the items left fit after it ("partly"): the run still ends
        # complete inside the 600 s budget.
        sender, books = live_count_world()
        company_of = {ev.short_hash(COMPANIES[k % 3].lower(), f"PO900{s}{k:02d}"): k % 3
                      for s in (4, 5) for k in range(8)}
        for retry_after, fail_every, after, slept in (
                (10, 0, len(COMPANIES), True), (10, LIVE_FAIL_EVERY, len(COMPANIES), True),
                (10, LIVE_FAIL_EVERY, 4, False), (30, LIVE_FAIL_EVERY, 3, False),
                (10, 8, 8, True), (60, LIVE_FAIL_EVERY, 3, False),
                (480, LIVE_FAIL_EVERY, len(COMPANIES), False), (480, 0, 0, "partly")):
            with self.subTest(retry_after=retry_after, fail_every=fail_every, after=after):
                clock = Clock()
                slow = LatencySender(sender, clock, ok=LIVE_OK_SECONDS,
                                     failed=LIVE_FAILED_ATTEMPT_SECONDS, fail_every=fail_every,
                                     throttled="purchaseInvoices", retry_after=retry_after,
                                     after=after, throttles=1)
                code, report, _, _ = run(slow, "--count", "15", books=books, clock=clock)
                found, cost = report["eval"], report["cost"]
                self.assertEqual(code, 0, found["fail_reasons"])
                self.assertEqual((found["complete"], found["incomplete"]), (True, []))
                self.assertEqual([f["http_status"] for f in report["sample"]["pool_failures"]],
                                 [] if after else [429])
                self.assertEqual(cost["retries"]["retry_after_not_retried"], 1)
                self.assertLessEqual(clock.now, ev.DEFAULT_BUDGET)
                if slept == "partly":
                    blocked = [r["item"] for r in report["items"] if r["status"] == "blocked"]
                    self.assertTrue(blocked)
                    self.assertEqual(len({company_of[item] for item in blocked}), 1)
                    self.assertEqual(found["blocked"]["by_detail"],
                                     {"held.http_429": len(blocked)})
                    self.assertEqual(cost["held_not_sent"], {"purchaseInvoices": len(blocked)})
                    self.assertEqual((found["scored"], found["correct"], found["wrong"]),
                                     (15 - len(blocked), 15 - len(blocked), 0))
                    self.assertEqual(cost["holds"]["waited"], 1)
                    self.assertLessEqual(cost["holds"]["wait_seconds"], float(retry_after))
                    continue
                if slept:
                    self.assertEqual(by_outcome(report), {
                        "sampled": 15, "blocked_upstream": 0, "not_held": 0, "excluded": 0,
                        "scored": 15, "correct": 15, "wrong": 0, "abstain_ok": 0})
                    self.assertEqual(cost["holds"]["waited"], 1)
                    # A hold met while sampling is waited out from the item read's time.
                    self.assertEqual(cost["holds"]["wait_seconds"] == float(retry_after),
                                     bool(after))
                    self.assertLessEqual(cost["holds"]["wait_seconds"], float(retry_after))
                    self.assertEqual(cost["held_not_sent"], {})
                    continue
                self.assertEqual(cost["holds"], {"waited": 0, "wait_seconds": 0.0})
                blocked = [r["item"] for r in report["items"] if r["status"] == "blocked"]
                if retry_after < 480:
                    self.assertEqual(len(blocked), 1)
                else:
                    self.assertGreater(len(blocked), 1)
                self.assertEqual(len({company_of[item] for item in blocked}), 1)
                # The throttled item read (none while sampling), then the reads its hold kept
                # from being sent.
                sent = 1 if after else 0
                held = len(blocked) - sent
                self.assertEqual(found["blocked"]["by_detail"],
                                 {**({"http_429": 1} if sent else {}),
                                  **({"held.http_429": held} if held else {})})
                self.assertEqual(cost["held_not_sent"],
                                 {"purchaseInvoices": held} if held else {})
                self.assertEqual(found["sampled"], 15)
                self.assertEqual(found["blocked_upstream"] + found["excluded"]
                                 + found["scored"], 15)
                self.assertGreaterEqual(found["scored"], 5)

    def test_the_reserve_weighs_the_priors_as_three_items_beside_the_items_read(self):
        # The reserve switched to the measured cost after the first
        # item, and an item that reads its receipts costs ~15 times one the oracle excludes.
        # Each unread item now costs the priors weighed as 3 items read beside the seconds
        # of the items read (a throttle wait is no item's cost), less what the item in
        # flight has spent, plus the slowest failed attempt.
        clock = Clock()
        cost = ev.RunCost(clock)
        prior = ev.ITEM_GETS * ev.GET_SECONDS
        cost.items(0, 15)
        self.assertAlmostEqual(cost.reserve(), 15 * prior)
        with cost.phase("oracle"):
            clock.sleep(49.0)
            cost.get(49.0, answer_seconds=LIVE_OK_SECONDS)
            clock.sleep(10.0)
            cost.waited(10.0)
        cost.items(1, 14)
        per_item = (3 * prior + 49.0) / 4
        self.assertAlmostEqual(cost.reserve(), 14 * per_item)
        with cost.phase("oracle_excluded"):
            clock.sleep(20.0)
            cost.failed_attempt("transport", 16.0)
            self.assertAlmostEqual(cost.reserve(), 14 * per_item - 20.0 + 16.0)

    def test_no_throttle_profile_ends_the_run_over_budget_or_incomplete(self):
        # Review sweep of the reserve: no failures or one attempt in 6, a 429 on the invoices
        # after 0 to 11 of their reads, Retry-After 10 to 480 s. Whatever the reserve
        # sleeps or holds, every run ends inside the budget, complete, every sampled item
        # accounted for once. The product's answer spends none of the run's clock, so it
        # is not run here.
        sender, books = live_count_world()

        def unanswered(pending, *_):
            return [{**row, "status": "not_held", "reason": "not_answered"}
                    for row, _, _ in pending]

        for fail_every in (0, LIVE_FAIL_EVERY):
            for after in range(12):
                for retry_after in (10, 20, 30, 45, 60, 120, 240, 480):
                    with self.subTest(fail_every=fail_every, after=after,
                                      retry_after=retry_after):
                        clock = Clock()
                        slow = LatencySender(
                            sender, clock, ok=LIVE_OK_SECONDS,
                            failed=LIVE_FAILED_ATTEMPT_SECONDS, fail_every=fail_every,
                            throttled="purchaseInvoices", retry_after=retry_after,
                            after=after, throttles=1)
                        with mock.patch.object(ev, "answer", unanswered):
                            _, report, _, _ = run(slow, "--count", "15", books=books,
                                                  clock=clock)
                        found = report["eval"]
                        self.assertEqual((found["complete"], found["incomplete"]), (True, []))
                        self.assertLessEqual(clock.now, ev.DEFAULT_BUDGET)
                        items = [r["item"] for r in report["items"]]
                        self.assertEqual(len(set(items)), len(items))
                        self.assertEqual((found["sampled"], found["blocked_upstream"]
                                          + found["excluded"] + found["not_held"]), (15, 15))

    def test_a_held_set_blocks_only_its_company_and_a_hold_past_the_budget_is_not_waited(self):
        # One company's invoices answer 429 to every read. Its items are blocked upstream;
        # the other companies' items, reading the same entity set, still score. A wait
        # inside the budget is spent once, then the set is held for the run after its bound;
        # a wait past the budget is never slept: the set is held and the run completes.
        sender, books = live_count_world()
        held_company = COMPANIES[0]
        # live_count_world puts every third PO in the first company.
        received = {ev.short_hash(held_company, f"PO9004{k:02d}") for k in range(0, 8, 3)}
        unreceived = {ev.short_hash(held_company, f"PO9005{k:02d}") for k in range(0, 7, 3)}
        for retry_after, sends, waited in ((10, ev.SET_FAILURE_BOUND, 1), (1000, 1, 0)):
            with self.subTest(retry_after=retry_after):
                clock = Clock()
                slow = LatencySender(sender, clock, ok=LIVE_OK_SECONDS,
                                     failed=LIVE_FAILED_ATTEMPT_SECONDS,
                                     throttled="purchaseInvoices", retry_after=retry_after,
                                     company=held_company)
                code, report, _, _ = run(slow, "--count", "15", books=books, clock=clock)
                found, rows = report["eval"], report["items"]
                blocked = {r["item"] for r in rows if r["status"] == "blocked"}
                self.assertEqual(blocked, (received | unreceived) & {r["item"] for r in rows})
                self.assertTrue(blocked & received)
                self.assertEqual(found["blocked"]["by_http_status"], {"429": len(blocked)})
                self.assertEqual(found["blocked"]["by_detail"], {"held.http_429": len(blocked)})
                self.assertEqual((found["sampled"], found["scored"]), (15, 15 - len(blocked)))
                self.assertEqual(slow.throttled_sent, sends)
                self.assertEqual(report["cost"]["held_not_sent"],
                                 {"purchaseInvoices": len(blocked)})
                self.assertEqual(report["cost"]["holds"]["waited"], waited)
                self.assertEqual((found["complete"], found["incomplete"]), (True, []))
                self.assertLess(clock.now, ev.DEFAULT_BUDGET / 2)
                self.assertEqual(code, 0, found["fail_reasons"])

    def test_a_failed_connect_costs_at_most_the_connect_timeout(self):
        # Live: a BC GET that failed before any status cost about 16 s per attempt under the
        # one 30 s socket timeout (connect, TLS and answer alike). The guarded sender now
        # bounds the connect apart; a slow answer still gets the whole read timeout.
        from integrations import live_source_guard as guard
        clock, connects = Clock(), []

        def unreachable(address, timeout=None, *args, **kwargs):
            connects.append(timeout)
            clock.now += timeout
            raise TimeoutError("timed out")

        url = f"https://api.businesscentral.dynamics.com{C}/purchaseOrders"
        with mock.patch("socket.create_connection", unreachable), \
                self.assertRaises(URLError):
            guard.urllib_sender("GET", url, {}, None, 30.0, 1000)
        self.assertEqual(connects, [guard.CONNECT_TIMEOUT_SECONDS])
        self.assertLessEqual(clock.now, guard.CONNECT_TIMEOUT_SECONDS)
        self.assertLess(guard.CONNECT_TIMEOUT_SECONDS, 30.0)

        class Answering:   # a connected socket whose answer is already there
            def __init__(self):
                self.timeouts = []

            def settimeout(self, seconds):
                self.timeouts.append(seconds)

            def sendall(self, data):
                pass

            def makefile(self, mode):
                return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

            def close(self):
                pass

        sock = Answering()

        def connect(conn):
            connects.append(conn.timeout)
            conn.sock = sock

        with mock.patch("http.client.HTTPSConnection.connect", connect):
            status, _, raw = guard.urllib_sender("GET", url, {}, None, 30.0, 1000)
        self.assertEqual((status, raw), (200, b"{}"))
        self.assertEqual(connects[1:], [guard.CONNECT_TIMEOUT_SECONDS])
        self.assertEqual(sock.timeouts, [30.0])

    def test_only_failed_attempts_past_the_connect_bound_fail_the_live_check(self):
        # Live 8e61f782: accuracy 9/9, yet the check failed on 4 connect timeouts at
        # 10.07-10.10 s, the guarded sender's 10 s connect bound plus scheduling. Such
        # attempts pass, a later page's included (timed from its own GET, not the read's
        # first page); an attempt well past the bound (a hung read, 16 s) fails the check
        # by name. Failed transport attempts are reported by class and entity set.
        from integrations import live_source_guard as guard
        spec = json.loads(SPEC.read_text())
        world = live_shaped_world()
        sender = world.sender()   # one posted receipt per page: a two-receipt PO reads 2
        sender.routes[f"{C}/purchaseReceipts"] = collection(world.receipts, page=1)
        bound = guard.CONNECT_TIMEOUT_SECONDS
        clock = Clock()
        connect = LatencySender(sender, clock, ok=LIVE_OK_SECONDS, failed=bound + 0.07,
                                fail_every=4, error=URLError(TimeoutError("timed out")))
        code, report, _, _ = run(connect, "--count", "15", clock=clock)
        retries = report["cost"]["retries"]
        self.assertTrue([u for u in connect.failed_urls if "skiptoken" in u])
        self.assertEqual(retries["slowest_failed_attempt_seconds"], round(bound + 0.07, 2))
        self.assertEqual((retries["failed_attempt_bound_seconds"],
                          retries["connect_timeout_slack_seconds"]),
                         (bound, ev.ATTEMPT_SLACK_SECONDS))
        self.assertEqual(retries["failed_attempts_over_bound"], 0)
        self.assertEqual(retries["transport_by_class"],
                         {"connect_timeout": connect.failures, "connect_error": 0,
                          "read_timeout": 0, "read_error": 0, "unclassified": 0})
        by_set = report["cost"]["transport_failed_by_set"]
        self.assertEqual(sum(c["connect_timeout"] for c in by_set.values()), connect.failures)
        self.assertIn("purchaseReceipts", by_set)
        self.assertEqual(code, 0, report["eval"]["fail_reasons"])
        self.assertEqual(live_verify.check_expectations(spec, code, report),
                         ["evidence_label != expected"])

        clock = Clock()
        hung = LatencySender(world.sender(), clock, ok=LIVE_OK_SECONDS, failed=16.0,
                             fail_every=4, failures_max=1, error=TimeoutError("timed out"))
        code, report, _, _ = run(hung, "--count", "15", clock=clock)
        retries = report["cost"]["retries"]
        self.assertEqual((hung.failures, retries["failed_attempts_over_bound"]), (1, 1))
        self.assertEqual(retries["transport_by_class"]["read_timeout"], 1)
        self.assertEqual(code, 0, report["eval"]["fail_reasons"])
        self.assertEqual(live_verify.check_expectations(spec, code, report),
                         ["evidence_label != expected",
                          "cost.retries.failed_attempts_over_bound > 0"])

    def test_each_get_attempt_is_timed_from_its_send_and_only_a_connect_timeout_has_slack(self):
        # The token POST counted in the next attempt, a get_document
        # attempt was timed from the GET before it, a read's slowest answer was its last
        # page's and a non-connect failure in (10.0, 10.5] s passed the bound. Each request
        # here takes the next scripted seconds (the token POST first, then GET attempts).
        from integrations import live_source_guard as guard
        from ontology.core import Actor
        bound, clock = guard.CONNECT_TIMEOUT_SECONDS, Clock()
        sender = FakeSender({f"{C}/purchaseOrders": collection(
            [{"id": "a", "number": "PO1"}, {"id": "b", "number": "PO2"}], page=1)})
        throttled = (429, {"retry-after": "1"}, b"{}")
        script = [(9.0, None),                                         # token POST
                  (bound + 0.07, URLError(TimeoutError("timed out"))),  # connect timeout
                  (40.0, None),
                  (bound + 0.2, (503, {}, b"{}")),     # $metadata; flagged: no connect
                  (0.5, (200, {}, b"<edmx/>")),
                  (12.0, None),                                        # page 1: slow answer
                  (11.0, throttled),                                   # page 2: slow 429
                  (1.0, None)]

        def timed(method, url, headers, body, timeout, max_bytes):
            seconds, outcome = script.pop(0)
            clock.now += seconds
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome or sender(method, url, headers, body, timeout, max_bytes)

        cost = ev.RunCost(clock)
        reader, _, pages = ev.build_reader(ENV, cost, ev.Deadline(1000, clock),
                                           sender=timed, sleep=clock.sleep)
        url = f"https://api.businesscentral.dynamics.com{C}/purchaseOrders"
        pages.get_json(f"{url}?$top=1")
        actor = Actor(ev.EVAL_TENANT, frozenset({ev.EVAL_PRINCIPAL}), "restricted")
        self.assertEqual(reader.metadata(actor), b"<edmx/>")
        with cost.phase("oracle"):
            rows, _ = pages.get_pages(f"{url}?$top=50", max_pages=5, max_items=50)
        self.assertEqual((len(rows), script), (2, []))
        report = cost.report()
        retries = report["retries"]
        self.assertEqual(retries["failed_attempt_seconds_by_cause"],
                         {"transport": round(bound + 0.07, 2), "http": round(bound + 0.2, 2),
                          "retry_after": 11.0})
        self.assertEqual(retries["transport_by_class"]["connect_timeout"], 1)
        self.assertEqual(retries["failed_attempts_over_bound"], 2)   # 10.2 s 503, 11 s 429
        self.assertEqual(report["phases"]["oracle"]["slowest_answer_seconds"], 12.0)
        self.assertEqual(report["phases"]["oracle"]["gets"], 1)

    def test_a_field_the_product_omits_is_caught_by_the_oracle(self):
        # Shared pages must not blind the oracle: it reads whole records, so a field
        # missing from the product's $select shows up as a failing eval.
        select = {s: tuple(f for f in fields if f != "lineType")
                  for s, fields in ev.bc_purchase.SELECT.items()}
        with mock.patch.dict(ev.bc_purchase.SELECT, select):
            code, report, _, _ = run(live_shaped_world(), "--count", "15")
        self.assertLess(report["eval"]["correct"], report["eval"]["scored"])
        self.assertTrue(report["eval"]["fail_reasons"])
        self.assertEqual(code, 3)

    def test_a_urlerror_on_one_read_blocks_only_its_item(self):
        world = live_shaped_world()
        sender = world.sender()
        receipts = sender.routes[f"{C}/purchaseReceipts"]

        def flaky(url):   # live: retried.urlerror on one PO's posted receipts
            if "PO900204" in unquote(url):
                raise URLError("connection timed out")
            return receipts(url)

        sender.routes[f"{C}/purchaseReceipts"] = flaky
        code, report, _, _ = run(sender, "--count", "15")
        self.assertEqual(by_outcome(report), {"sampled": 15, "blocked_upstream": 1,
                                              "not_held": 0, "excluded": 0, "scored": 14,
                                              "correct": 14, "wrong": 0, "abstain_ok": 0})
        self.assertEqual(report["eval"]["blocked"]["by_detail"], {"retried.urlerror": 1})
        self.assertEqual(report["eval"]["blocked"]["by_entity_set"], {"purchaseReceipts": 1})
        self.assertTrue(report["eval"]["complete"])
        self.assertEqual(code, 0, report["eval"]["fail_reasons"])

    def test_spent_budget_is_an_incomplete_failing_run(self):
        world = World()
        world.add("PO900151", receipts=[(100, "PCS")])
        code, report, _, _ = run(world, "--total-budget-seconds", "0")
        self.assertFalse(report["eval"]["complete"])
        self.assertEqual(report["eval"]["incomplete"], ["run_budget_exhausted"])
        self.assertEqual(report["eval"]["zero_scored_cause"], "nothing_sampled")
        self.assertEqual(code, 3)


class SpecTests(unittest.TestCase):
    def test_spec_runs_without_portal_and_asserts_the_metric_and_bound(self):
        spec = json.loads(SPEC.read_text())
        self.assertEqual(live_verify.validate_spec(spec, stem=SPEC.stem), [])
        self.assertNotIn("requires", spec)
        self.assertFalse([n for n in spec["requires_env"] if n.startswith("PLX_")])
        self.assertIn("scripts.eval_bc_three_way", live_verify.ALLOWED_MODULES)
        expect = spec["expect"]
        self.assertTrue(expect["json_equals"]["eval.wrong_within_bound"])
        self.assertTrue(expect["json_equals"]["eval.pass"])
        self.assertGreaterEqual(expect["json_min"]["eval.scored"], 1)
        for name in ("sampled", "blocked_upstream", "not_held", "scored", "correct", "wrong",
                     "abstain_ok", "coverage", "answered", "root_lookup"):
            self.assertIn(f"eval.{name}", expect["json_fields"])
        # On the eval's own output: only the injected transport's label differs from a live
        # pass, and the verifier records the cost per phase.
        sender, books = live_count_world()
        code, report, _, _ = run(sender, "--count", "15", books=books, clock=Clock())
        self.assertEqual(live_verify.check_expectations(spec, code, report),
                         ["evidence_label != expected"])
        # The live criteria: at most 2 items blocked upstream and no failed attempt past
        # the guarded sender's connect bound plus slack (the slowest stays recorded).
        worse = json.loads(json.dumps(report))
        worse["eval"]["blocked_upstream"] = 3
        worse["cost"]["retries"]["failed_attempts_over_bound"] = 1
        self.assertEqual(live_verify.check_expectations(spec, code, worse)[1:],
                         ["eval.blocked_upstream > 2",
                          "cost.retries.failed_attempts_over_bound > 0"])
        self.assertIn("cost.retries.slowest_failed_attempt_seconds", expect["json_fields"])
        recorded = live_verify._recorded(spec, report)
        self.assertEqual(recorded["cost"]["phases"], report["cost"]["phases"])
        for key in ("by_cause", "transport_by_class"):
            self.assertEqual(recorded["cost"]["retries"][key], report["cost"]["retries"][key])


if __name__ == "__main__":
    unittest.main()
