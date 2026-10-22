"""Customer-PO coverage of Portal sales orders (injected transport only).

Evidence label: ``injected-transport``. The real ``WorkbenchReadApiClient`` reads
the in-process ``FakeWorkbench`` from the source-mirror tests with fixture rows
shaped after the 2026-09-30 live observations (Sage imports carry the PO in
``po_reference``, sometimes as a person's name; FM migrations in
``custom_fields.custPoCode``; Portal-entered orders in neither). Socket
connections are refused for the whole module, so nothing can touch a network.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from integrations.source_mirror import AUDIENCE_SCHEMA, MirrorError
from integrations.tests.test_source_mirror import BASE, KEY, Clock, FakeWorkbench
from integrations.workbench_read_api import WorkbenchReadApiClient
from ontology.core import Actor
from operating.tests.firewall_view import client_result
from operating.mcp_server import source_read
from operating import source_reads
from operating import customer_po_coverage as cpo
from operating.tests import test_attested_missing as tam
from operating.case_runtime import CaseRuntime
from operating.mcp_server import HOST_READS, MAX_RESULT_BYTES, MCPServer


OPS = frozenset({"staff:operations"})
OPS_FINANCE = frozenset({"staff:operations", "staff:finance"})
OUTSIDER = frozenset({"staff:marketing"})
GRANTED = {"public.*": ["staff:operations"], "sage.*": ["staff:finance"]}
SAGE = {"source": "sage.oe_order_header"}
NON_PO_TEXT = ("Jane Buyer", "Verbal", "Robert O'Neil", "PO 4500", "see email")


def _order(ident, *, po_reference=None, custom_fields=None, number=None,
           updated="2026-09-29T09:00:00"):
    return {"id": ident, "order_number": number or f"OR{ident:04d}",
            "po_reference": po_reference, "total_amount": "10.00",
            "custom_fields": custom_fields, "updated_at": updated}


ORDERS = [
    _order(1, po_reference="PO-5501", custom_fields={**SAGE, "sageOrdNumber": "S1"}),
    _order(2, po_reference="Jane Buyer", custom_fields=dict(SAGE)),
    _order(3, po_reference=None, custom_fields=dict(SAGE), updated="2026-08-01T00:00:00"),
    _order(4, custom_fields={"fmId": 351, "custPoCode": "PO12172025"}),
    _order(5, po_reference="", custom_fields={"fmId": 352, "custPoCode": ""}),
    _order(6, custom_fields={"fmId": 353, "custPoCode": "Verbal"}),
    _order(7, custom_fields={"fmId": 354}),
    _order(8, custom_fields=None, updated="2026-08-15T00:00:00"),
    _order(9, custom_fields={}),
    _order(10, po_reference="Robert O'Neil", custom_fields={"notes": "rush"}),
    _order(11, custom_fields={"source": "fm-oe-ensure"}),  # FM-ensure always stamps fmId
    _order(12, custom_fields="not json"),
    _order(13, custom_fields={"fmId": None}),
    _order(14, custom_fields={"source": "other.system"}),
    _order(15, po_reference="12345"),
    _order(16, custom_fields={"fmId": 356, "custPoCode": "PO 4500"}),
    _order(17, custom_fields={"fmId": 357, "custPoCode": "000"}),
    _order(18, po_reference="PO-1", custom_fields={"fmId": 358, "custPoCode": "PO-2"}),
    _order(19, po_reference="see email", custom_fields={"poNote": "PO-9001"}),
    _order(20, custom_fields={"poNote": "PO-9002"}),
]

EXPECTED = {  # id: (origin, po_reference status, custPoCode status, order status)
    1: ("sage_import", "present_po_like", "field_absent", "has_customer_po"),
    2: ("sage_import", "present_not_po_like", "field_absent", "po_field_not_po_like"),
    3: ("sage_import", "empty", "field_absent", "absent_in_this_source"),
    4: ("fm_migrated", "empty", "present_po_like", "has_customer_po"),
    5: ("fm_migrated", "empty", "empty", "absent_in_this_source"),
    6: ("fm_migrated", "empty", "present_not_po_like", "po_field_not_po_like"),
    7: ("fm_migrated", "empty", "field_absent", "absent_in_this_source"),
    8: ("portal_entered", "empty", "field_absent", "absent_in_this_source"),
    9: ("portal_entered", "empty", "field_absent", "absent_in_this_source"),
    10: ("portal_entered", "present_not_po_like", "field_absent", "po_field_not_po_like"),
    11: ("unknown", "empty", "field_absent", "absent_in_this_source"),
    12: ("unknown", "empty", "not_inspectable", "po_fields_not_inspectable"),
    13: ("unknown", "empty", "field_absent", "absent_in_this_source"),
    14: ("unknown", "empty", "field_absent", "absent_in_this_source"),
    15: ("portal_entered", "present_po_like", "field_absent", "has_customer_po"),
    16: ("fm_migrated", "empty", "present_not_po_like", "po_field_not_po_like"),
    17: ("fm_migrated", "empty", "present_not_po_like", "po_field_not_po_like"),
    18: ("fm_migrated", "present_po_like", "present_po_like", "has_customer_po"),
    19: ("portal_entered", "present_not_po_like", "field_absent", "po_field_not_po_like"),
    20: ("portal_entered", "empty", "field_absent", "absent_in_this_source"),
}

SAGE_HEADER = {"columns": ["_sage_pk", "ordnumber", "ponumber", "audtdate"],
               "key": ["_sage_pk"], "domain": "orders",
               "since": {"column": "audtdate", "mode": "yyyymmdd-text"}}
SAGE_ROWS = [{"_sage_pk": "3", "ordnumber": "S3", "ponumber": "PO-7788", "audtdate": "20260928"},
             {"_sage_pk": "4", "ordnumber": "S4", "ponumber": "Mike", "audtdate": "20260928"}]


def _timed(function) -> float:
    import time
    started = time.monotonic()
    function()
    return time.monotonic() - started


def _assert_linear(test, build, operation) -> None:
    """Growth-ratio guard: load cancels out of t(8n)/t(n); linear ~8, quadratic ~64."""
    def best(count):
        value = build(count)
        return min(_timed(lambda: operation(value)) for _ in range(3))
    small, large = best(_RATIO_UNIT), best(8 * _RATIO_UNIT)
    test.assertLess(large / max(small, 1e-6), 24, (small, large))


_RATIO_UNIT = 125_000


def _refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted in an offline test")


class CoverageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        for target in ("socket.socket.connect", "socket.create_connection"):
            patcher = patch(target, _refuse_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.audience_path = self.root / "audience.json"
        self.audience_path.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx", "audiences": GRANTED}),
            encoding="utf-8")
        self.fake = FakeWorkbench()
        self.fake.tables["public.app_sales_order"] = [dict(row) for row in ORDERS]
        self.fake.catalog["sage.oe_order_header"] = dict(SAGE_HEADER)
        self.fake.tables["sage.oe_order_header"] = [dict(row) for row in SAGE_ROWS]
        self.clock = Clock()
        self.request_id = 100

    def runtime(self) -> CaseRuntime:
        client = WorkbenchReadApiClient(BASE, KEY, fetch=self.fake)
        runtime = CaseRuntime(self.state, client=client, audience=self.audience_path,
                              tenant="plx", mirror_options={
                                  "clock": self.clock, "retry_delay_seconds": 0})
        self.addCleanup(runtime.close)
        return runtime

    def server(self, principals, runtime, *, test_capabilities=False) -> MCPServer:
        server = MCPServer(case_runtime=runtime, actor=Actor("plx", frozenset(principals)),
                           test_capabilities=test_capabilities)
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "po-test", "version": "1"}}})
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def call(self, server, name, arguments=None) -> dict:
        self.request_id += 1
        host = name in HOST_READS  # the drivers' raw citation read is host code
        response = server.handle({"jsonrpc": "2.0", "id": self.request_id,
                                  "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments or {}}},
                                 host_code=host)
        self.assertNotIn("error", response, response)
        if host:
            return response["result"]
        return client_result(name, response["result"])  # through the output boundary

    def ok(self, server, name, arguments=None) -> dict:
        if name in source_reads.READS:  # a driver's direct read, not a host tool
            return source_read(server, name, arguments or {})
        result = self.call(server, name, arguments)
        self.assertFalse(result["isError"], result)
        size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
        self.assertLessEqual(size, MAX_RESULT_BYTES)
        return result["structuredContent"]

    def refused(self, server, name, arguments=None) -> str:
        result = self.call(server, name, arguments)
        self.assertTrue(result["isError"], result)
        return result["content"][0]["text"]

    @staticmethod
    def by_id(orders):
        return {order["key"]["id"]: order for order in orders}


class ShapeAndOriginRuleTests(unittest.TestCase):
    def test_po_shape_rule_is_conservative(self):
        for value in ("PO-5501", "PO12172025", "12345", "CUST-77", "po-1001", "A1B",
                      "4500/12", "PO#991", 4500123):
            self.assertTrue(cpo.po_like(value), value)
        for value in ("Jane Buyer", "Verbal", "PO 4500", "TBD", "N/A", "000", "12", "",
                      "   ", "email", "jane@example.com", "-PO1", "x" * 39 + "1" * 2,
                      None, True, 12.5, {"po": "1"}, "Robert O'Neil"):
            self.assertFalse(cpo.po_like(value), value)

    def test_not_po_like_values_say_why_without_echoing_them(self):
        cases = {"PO 4500": (["whitespace"], True),
                 "P.O. #4500": (["whitespace"], True),
                 "PO# 4500": (["whitespace"], True),
                 "# 4500": (["whitespace", "edge_not_alphanumeric"], True),
                 "4500 12": (["whitespace"], False),  # no marker: phone/date-like
                 "905 555 1234": (["whitespace"], False),
                 "PO 4500 12": (["whitespace"], True),
                 "PO Box 123": (["whitespace"], False), "Order 4500": (["whitespace"], False),
                 "Jane Buyer": (["whitespace", "no_digit"], False),
                 "Jane Buyer 2": (["whitespace"], False),
                 "Robert O'Neil": (["whitespace", "no_digit", "disallowed_chars"], False),
                 "Verbal": (["no_digit"], False), "TBD": (["no_digit"], False),
                 "12": (["too_short"], False), "PO 12": (["whitespace"], False),
                 "000": (["only_zeros"], False), "-PO1": (["edge_not_alphanumeric"], False),
                 "x" * 40 + "1": (["too_long"], False), "ÄB123": (["disallowed_chars",
                                                                   "edge_not_alphanumeric"],
                                                                  False)}
        # A value that is not text or an integer cannot be read as a PO or as empty.
        for value in (12.5, True, ["PO-77"], {"po": "1"}):
            self.assertEqual(cpo.classify_field(True, value),
                             {"status": "not_inspectable", "reason": "value_not_text"}, value)
        for value, (reasons, probable) in cases.items():
            field = cpo.classify_field(True, value)
            self.assertEqual(field, {"status": "present_not_po_like", "value_withheld": True,
                                     "reasons": reasons,
                                     "probable_po_after_normalization": probable}, value)
            self.assertNotIn(str(value), json.dumps(field))
        # Linear on hostile values: a long run of spaces after "P" used to backtrack.
        def both(value):
            self.assertFalse(cpo.probable_po_after_normalization(value))
            self.assertTrue(cpo.not_po_like_reasons(value))
        for build in (lambda k: "P" + " " * k, lambda k: "P" + " ." * (k // 2) + "x",
                      lambda k: "PO " + "1 " * (k // 2)):
            _assert_linear(self, build, both)
        _assert_linear(self, lambda k: "P" + " " * k, cpo._PO_MARKER.match)
        for value in ("PO-5501", "PO#991", "12345"):
            self.assertEqual(cpo.not_po_like_reasons(value), [], value)
            self.assertFalse(cpo.probable_po_after_normalization(value), value)
        # A probable PO is a lead on the order, never a customer PO.
        order = cpo.classify_order({"po_reference": "P.O. #4500", "custom_fields": None},
                                   ["po_reference", "custom_fields"])
        self.assertEqual(order["status"], "po_field_not_po_like")
        self.assertTrue(order["probable_po_after_normalization"])
        self.assertNotIn("customer_po_values", order)
        self.assertNotIn("4500", json.dumps(order))

    def test_probable_po_stops_at_a_second_marker_and_needs_a_real_marker(self):
        for value in ("PO 4500", "PO4500 12", "P.O. #4500", "PO 4500 12", "#4500",
                      "PO NO 4500", "PO: 4500"):
            self.assertTrue(cpo.probable_po_after_normalization(value), value)
        for value in ("PO4500 PO4501", "PO 4500 PO 4501", "PO 4500 #4501", "PO 4500 PO",
                      "Polo2 123", "POA1 2", "Polo2", "POA1", "POST 123",
                      "Jane Buyer 2", "905 555 1234"):
            self.assertFalse(cpo.probable_po_after_normalization(value), value)
        self.assertIsNone(cpo._PO_MARKER.match("Polo2"))
        self.assertIsNotNone(cpo._PO_MARKER.match("PO4500"))

    def test_values_over_80_characters_are_never_flagged(self):
        padding = "please apply the customer reference supplied in the attached e-mail as "
        for tail in ("PO-5501", "PO# 5501-A", "P.O. 5501-A", "PO#A123 4", "invoice # 5501",
                     "Suite # 204", "order # 55012", "2026-01-05", "01/05/2026", "2026",
                     "905-555-1234", "9055551234"):
            value = f"{padding}{tail} thanks"
            self.assertGreater(len(value), 80)
            self.assertFalse(cpo.probable_po_after_normalization(value), tail)
        self.assertTrue(cpo.probable_po_after_normalization("PO#A123 4"))
        # The limit applies to the raw value, as at base: padding counts.
        self.assertFalse(cpo.probable_po_after_normalization(" " * 80 + "PO 4500"))
        field = cpo.classify_field(True, f"{padding}PO-5501 thanks")
        self.assertEqual(field["reasons"], ["too_long", "whitespace"])
        self.assertFalse(field["probable_po_after_normalization"])
        self.assertNotIn("PO-5501", json.dumps(field))

    def test_qualified_and_wrapped_po_markers_are_leads_but_unmarked_text_is_not(self):
        # Real-shaped values from the 2026-09-30 audit's "short whitespace+digit
        # phrases": only an explicit PO marker makes a lead.
        for value in ("PO# 12345", "PO 4500 12", "P/O 4500", "Cust PO 4500",
                      "Customer P.O.# 4500", "Client PO 4500", "PO#: 4500",
                      "PO No: 4500", "Purchase Order 4500", "(PO 4500)", "(PO4500)",
                      "PO 4500.", "'PO 4500'", "P.O. 4500", "PO. 4500", "[P.O. #4500];"):
            self.assertTrue(cpo.probable_po_after_normalization(value), value)
            field = cpo.classify_field(True, value)
            self.assertEqual(field["status"], "present_not_po_like", value)
        for value in ("4500 1234", "905 555 1234", "(905) 555 1234", "905-555 1234",
                      "2026 01 05", "01 05 2026", "1,920 pcs", "1920 pcs", "PO 1920 pcs",
                      "Suite # 204", "Invoice 12345", "Invoice # 12345", "Inv 12345",
                      "Order 4500", "Order # 4500", "per email 45001", "Customer # 4500",
                      "Customer 4500", "Cust 4500", "Purchase Orders 4500",
                      "PO Box 12345", "PO 4500, 4501", "PO 4500 / 4501", "PO 4500; PO 4501",
                      "Cust PO 4500 PO 4501", "'Jane 2'", "(Jane Buyer 2)", "12.", "PO.",
                      "()", '""', "#", "Qty 1920", "$4500", "PO 12", "P.O. Box 123",
                      # Punctuation never makes an unmarked value a lead (review of
                      # 650efe5): years, quantities, amounts, dates, suites.
                      "2026.", "(2026-01-05)", '"2026-01-05"', "123.", "1920.", "1.5.",
                      "4500.", "4500,", "4500;", "4500:", "4500.00.", "(4500.00)", "(#204)",
                      "[#4500]", "(4500)", "'4500'"):
            self.assertFalse(cpo.probable_po_after_normalization(value), value)
        # Still linear on hostile values through the new marker branches.
        for build in (lambda k: "Customer" + " " * k + "x", lambda k: "P" + "/" * k,
                      lambda k: "Purchase" + " " * k, lambda k: "PO#" + " " * k + "x",
                      lambda k: "(" + "1 " * (k // 2)):
            _assert_linear(self, build, cpo._PO_MARKER.match)
            _assert_linear(self, build, cpo.not_po_like_shape)

    def test_shape_classes_are_value_free_buckets(self):
        cases = {
            "905 555 1234": {"reasons": "whitespace", "marker": "none", "tokens": "3",
                             "digit_tokens": "3+", "alpha_tokens": "0",
                             "digit_groups": "3", "max_digit_run": "3-4",
                             "numeric_pattern": "3+3+4", "length": "11-20"},
            "PO 4500 12": {"reasons": "whitespace", "marker": "leading", "tokens": "3",
                           "digit_tokens": "2", "alpha_tokens": "1", "digit_groups": "2",
                           "max_digit_run": "3-4", "numeric_pattern": "mixed",
                           "length": "3-10"},
            "per email 45001": {"reasons": "whitespace", "marker": "none", "tokens": "3",
                                "digit_tokens": "1", "alpha_tokens": "2+",
                                "digit_groups": "1", "max_digit_run": "5-6",
                                "numeric_pattern": "mixed", "length": "11-20"},
            "Jane Buyer": {"reasons": "whitespace+no_digit", "marker": "none", "tokens": "2",
                           "digit_tokens": "0", "alpha_tokens": "2+", "digit_groups": "0",
                           "max_digit_run": "0", "numeric_pattern": "no_digits",
                           "length": "3-10"},
            "x PO4500": {"reasons": "whitespace", "marker": "inner", "tokens": "2",
                         "digit_tokens": "1", "alpha_tokens": "1", "digit_groups": "1",
                         "max_digit_run": "3-4", "numeric_pattern": "mixed",
                         "length": "3-10"},
            "12": {"reasons": "too_short", "marker": "none", "tokens": "1",
                   "digit_tokens": "1", "alpha_tokens": "0", "digit_groups": "1",
                   "max_digit_run": "1-2", "numeric_pattern": "2", "length": "1-2"},
            "2026-01-05 10": {"reasons": "whitespace", "marker": "none", "tokens": "2",
                              "digit_tokens": "2", "alpha_tokens": "0",
                              "digit_groups": "4+", "max_digit_run": "3-4",
                              "numeric_pattern": "4+2+2+2", "length": "11-20"},
        }
        for value, expected in cases.items():
            shape = cpo.not_po_like_shape(value)
            self.assertEqual(shape, expected, value)
            self.assertEqual(tuple(shape), cpo.SHAPE_DIMENSIONS)
            for bucket in shape.values():  # buckets never carry the value's text
                self.assertFalse(len(value) > 2 and value in bucket, value)
        self.assertEqual(cpo.not_po_like_shape(12.5)["reasons"], "not_text")
        self.assertEqual(cpo.not_po_like_shape("1 2 3 4 5 6")["numeric_pattern"],
                         "6+_groups")
        self.assertEqual(cpo.not_po_like_shape("1" * 12 + " 1")["numeric_pattern"], "10++1")

    def test_origin_rule_reports_the_rule_used(self):
        columns = ["id", "custom_fields"]
        # Mirrors Portal lib/wms/sage-ship-queue.ts (isSageBackedSalesOrder: sageOrdUniq |
        # sageSourceOrdUniq | source; isFmOnlySalesOrder: fmId and not Sage-backed) and
        # the two bridge sources (fm-oe-ensure, gold-so-bridge).
        cases = {json.dumps(SAGE): ("sage_import", "sage_etl", "custom_fields.source"),
                 json.dumps({"sageOrdUniq": "8812"}): ("sage_import", "sage_etl",
                                                       "custom_fields.sageOrdUniq"),
                 json.dumps({"sageSourceOrdUniq": 9, "fmId": 5}):
                     ("sage_import", "sage_etl", "Sage-backed wins"),
                 json.dumps({**SAGE, "fmId": 1}): ("sage_import", "sage_etl",
                                                   "Sage-backed wins"),
                 json.dumps({**SAGE, "fmId": None}): ("sage_import", "sage_etl", "also carries"),
                 json.dumps({"source": "gold-so-bridge", "sageOrdUniq": "1"}):
                     ("sage_import", "sage_etl", "overridden by the Sage key"),
                 # JS predicate order: a trimmed Sage key wins before any source check,
                 # and the source marker is compared trimmed.
                 json.dumps({"source": "other.system", "sageOrdUniq": " 7 "}):
                     ("sage_import", "sage_etl", "overridden by the Sage key"),
                 json.dumps({"source": " sage.oe_order_header "}):
                     ("sage_import", "sage_etl", "custom_fields.source"),
                 json.dumps({"source": " fm-oe-ensure ", "fmId": 3}):
                     ("fm_migrated", "fm_oe_ensure", "fm-oe-ensure"),
                 json.dumps({"source": "other.system", "sageOrdUniq": "  "}):
                     ("unknown", "unknown", "unrecognised"),
                 json.dumps({"source": "fm-oe-ensure", "fmId": 351}):
                     ("fm_migrated", "fm_oe_ensure", "fm-oe-ensure"),
                 json.dumps({"source": "gold-so-bridge", "fmId": 900123}):
                     ("portal_entered", "gold_so_bridge", "bridged"),
                 json.dumps({"source": "gold-so-bridge"}):
                     ("portal_entered", "gold_so_bridge", "gold-so-bridge"),
                 json.dumps({"fmId": "9"}): ("fm_migrated", "fm_linked_without_source",
                                             "fmId present"),
                 json.dumps({"fmId": "9", "custPoCode": "x"}):
                     ("fm_migrated", "legacy_fm_sql", "migrate-fm-to-portal.sql"),
                 json.dumps({"sageOrdUniq": " ", "fmId": 4}):
                     ("fm_migrated", "fm_linked_without_source", "fmId present"),
                 "": ("portal_entered", "portal", "no import marker"),
                 # Odd markers still make the origin unknown.
                 json.dumps({"source": "other.system", "fmId": "x"}):
                     ("unknown", "unknown", "unrecognised"),
                 json.dumps({"source": "fm-oe-ensure"}): ("unknown", "unknown", "without fmId"),
                 json.dumps({"fmId": ""}): ("unknown", "unknown", "fmId present but empty"),
                 "[1]": ("unknown", "unknown", "not_object"),
                 "{bad": ("unknown", "unknown", "unparseable")}
        for raw, (origin, lineage, rule) in cases.items():
            verdict = cpo.classify_origin({"custom_fields": raw}, columns)
            self.assertEqual(verdict["value"], origin, raw)
            self.assertEqual(verdict["lineage"], lineage, raw)
            self.assertIn(lineage, cpo.LINEAGES)
            self.assertIn(rule, verdict["rule"], raw)
        self.assertEqual(cpo.classify_origin({}, ["id"]),
                         {"value": "unknown", "lineage": "unknown",
                          "rule": "custom_fields_not_mirrored"})

    def test_a_po_field_that_was_never_read_is_not_inspectable_never_absent(self):
        unread = cpo.classify_order({"id": 1}, ["id"])
        self.assertEqual(unread["fields"],
                         {"po_reference": {"status": "not_inspectable",
                                           "reason": "column_not_mirrored"},
                          "custom_fields.custPoCode": {"status": "not_inspectable",
                                                       "reason": "column_not_mirrored"}})
        self.assertEqual(unread["status"], "po_fields_not_inspectable")
        columns = ["id", "po_reference", "custom_fields"]
        for raw, reason in (("{bad", "json_unparseable"), ("[1]", "json_not_object"),
                            ('"PO-1"', "json_not_object"), (7, "json_not_object"),
                            (["PO-1"], "json_not_object")):
            verdict = cpo.classify_order({"po_reference": None, "custom_fields": raw}, columns)
            self.assertEqual(verdict["fields"]["custom_fields.custPoCode"],
                             {"status": "not_inspectable", "reason": reason}, raw)
            self.assertEqual(verdict["status"], "po_fields_not_inspectable", raw)
        # A readable object without the key, or no custom_fields at all, is absent.
        for raw in (None, "", "  ", "{}", {"notes": "x"}):
            verdict = cpo.classify_order({"po_reference": "", "custom_fields": raw}, columns)
            self.assertEqual(verdict["fields"]["custom_fields.custPoCode"],
                             {"status": "field_absent"}, raw)
            self.assertEqual(verdict["status"], "absent_in_this_source", raw)
        # A PO-like value in a readable field still counts next to an unreadable one.
        mixed = cpo.classify_order({"po_reference": "PO-77", "custom_fields": "{bad"}, columns)
        self.assertEqual(mixed["status"], "has_customer_po")
        # Non-PO text next to an unreadable field: the order's PO state is unknown.
        text = cpo.classify_order({"po_reference": "Jane", "custom_fields": "{bad"}, columns)
        self.assertEqual(text["status"], "po_fields_not_inspectable")

    def test_date_values_are_parsed_as_dates_or_instants(self):
        self.assertEqual(cpo.parse_date_value("2026-08-31"), date(2026, 8, 31))
        self.assertEqual(cpo.parse_date_value("2026-08-31T15:00:00"),
                         datetime(2026, 8, 31, 15, tzinfo=timezone.utc))
        self.assertEqual(cpo.parse_date_value("2026-09-01T02:00:00+05:00"),
                         datetime(2026, 8, 31, 21, tzinfo=timezone.utc))
        self.assertEqual(cpo.parse_date_value("2026-09-01 02:00:00.123456789-0130"),
                         datetime(2026, 9, 1, 3, 30, 0, 123456, tzinfo=timezone.utc))
        self.assertEqual(cpo.parse_date_value("2026-01-05 10:11:12.123+00"),
                         datetime(2026, 1, 5, 10, 11, 12, 123000, tzinfo=timezone.utc))
        self.assertEqual(cpo.parse_date_value("2026-01-05 10:11:12+05"),
                         datetime(2026, 1, 5, 5, 11, 12, tzinfo=timezone.utc))
        self.assertEqual(cpo.parse_date_value("2026-01-05T10:11-08"),
                         datetime(2026, 1, 5, 18, 11, tzinfo=timezone.utc))
        self.assertEqual(cpo.parse_date_value("2026-08-31T23:59Z"),
                         datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc))
        for value in ("2026-02-30", "2026-13-01", "2026-08-01T25:00", "2026-08-01T10:60",
                      "2026-08-01T10:00:61", "2026-08-01T10:00+24", "2026-08-01T10:00+0", "2026-08-01T10:00+000", "2026-08-01T10:00+24:00", "2026-08-01T10:00+05:75",
                      "20260801", "yesterday", "", None, 20260801, "2026-08-01T"):
            self.assertIsNone(cpo.parse_date_value(value), value)


class CoverageTests(CoverageTestCase):
    def test_every_origin_and_field_combination_is_classified_and_cited(self):
        runtime = self.runtime()
        actor = Actor("plx", OPS)
        result = cpo.coverage(runtime.mirror, actor)
        orders = self.by_id(result["orders"])
        self.assertEqual(set(orders), set(EXPECTED))
        for ident, (origin, po_ref, cust, status) in EXPECTED.items():
            order = orders[ident]
            self.assertEqual((order["origin"]["value"], order["fields"]["po_reference"]["status"],
                              order["fields"]["custom_fields.custPoCode"]["status"],
                              order["status"]), (origin, po_ref, cust, status), ident)
            self.assertTrue(order["origin"]["rule"])
            resolved = runtime.mirror.resolve_citation(order["citation"], actor)
            self.assertTrue(resolved["ok"], ident)
            self.assertEqual(resolved["row"]["id"], ident)
            self.assertEqual(order["order_number"], f"OR{ident:04d}")
            if status == "has_customer_po":
                self.assertNotIn("possible_sources", order)
            else:
                sources = {item["source"]: item["availability"]
                           for item in order["possible_sources"]}
                self.assertEqual(sources["microsoft_graph_mail"], "not_ingested")
                self.assertEqual(sources["sharepoint"], "not_ingested")
                self.assertEqual(sources["portal_attachments_and_other_tables"], "unknown")
                if origin == "sage_import":
                    self.assertEqual(sources["sage.oe_order_header.ponumber"], "not_checked")
                if origin == "fm_migrated":
                    self.assertEqual(sources["public.order_entry.custpocode"], "not_checked")
                self.assertNotIn("checked", {v for v in sources.values()})
        self.assertEqual(orders[4]["customer_po_values"], ["PO12172025"])
        self.assertEqual(orders[4]["fields"]["custom_fields.custPoCode"]["value"], "PO12172025")
        self.assertEqual(orders[18]["customer_po_values"], ["PO-1", "PO-2"])
        self.assertTrue(orders[18]["conflicting_po_values"])
        self.assertEqual(result["columns_read"],
                         ["id", "order_number", "po_reference", "custom_fields"])
        self.assertEqual(result["columns_expected_but_not_mirrored"], [])
        self.assertIn("does not mean the order has no customer PO", result["rules"]["absence"])
        self.assertTrue(result["completeness"]["complete"])
        self.assertFalse(result["completeness"]["atomic_upstream_snapshot"])
        self.assertFalse(result["completeness"]["deletions_proven"])

    def test_non_po_text_is_never_echoed(self):
        runtime = self.runtime()
        result = cpo.coverage(runtime.mirror, Actor("plx", OPS))
        text = json.dumps(result)
        for value in NON_PO_TEXT:
            self.assertNotIn(value, text)
        withheld = self.by_id(result["orders"])[2]["fields"]["po_reference"]
        self.assertEqual(withheld, {"status": "present_not_po_like", "value_withheld": True,
                                    "reasons": ["whitespace", "no_digit"],
                                    "probable_po_after_normalization": False,
                                    # Sage ETL: COALESCE(ponumber, po_reference).
                                    "possible_writers": "sage_ponumber_or_retained_earlier_value"})

    def test_aggregates_count_origin_by_status_over_the_same_rows(self):
        runtime = self.runtime()
        result = cpo.coverage(runtime.mirror, Actor("plx", OPS),
                              origins=["fm_migrated"], statuses=["absent_in_this_source"])
        self.assertEqual(sorted(self.by_id(result["orders"])), [5, 7])
        aggregates = result["aggregates"]
        self.assertEqual(aggregates["rows"], len(ORDERS))
        expected = {origin: {status: 0 for status in cpo.ORDER_STATUSES}
                    for origin in cpo.ORIGINS}
        for origin, _, _, status in EXPECTED.values():
            expected[origin][status] += 1
        self.assertEqual(aggregates["by_origin_status"], expected)
        self.assertEqual(aggregates["by_status"],
                         {"has_customer_po": 4, "po_fields_not_inspectable": 1,
                          "po_field_not_po_like": 6, "absent_in_this_source": 9})
        self.assertEqual(aggregates["by_origin_field_status"]["sage_import"]["po_reference"],
                         {"present_po_like": 1, "present_not_po_like": 1,
                          "present_equals_order_number": 0, "empty": 1,
                          "field_absent": 0, "not_inspectable": 0})
        self.assertTrue(aggregates["complete"])
        # Why fields are not PO-like, per origin and field; counts only.
        fm = aggregates["by_origin_field_not_po_like_reason"]["fm_migrated"]
        self.assertEqual(fm["custom_fields.custPoCode"],
                         {"not_text": 0, "too_short": 0, "too_long": 0, "whitespace": 1,
                          "no_digit": 1, "only_zeros": 1, "disallowed_chars": 0,
                          "edge_not_alphanumeric": 0})  # PO 4500, Verbal, 000
        self.assertEqual(aggregates["by_origin_field_probable_po_after_normalization"]
                         ["fm_migrated"]["custom_fields.custPoCode"], 1)  # PO 4500
        self.assertEqual(aggregates["orders_with_probable_po_after_normalization"],
                         {"sage_import": 0, "fm_migrated": 1, "portal_entered": 0,
                          "unknown": 0})
        self.assertEqual(aggregates["by_status"]["has_customer_po"], 4)

    def test_aggregates_histogram_not_po_like_shapes_without_values(self):
        runtime = self.runtime()
        result = cpo.coverage(runtime.mirror, Actor("plx", OPS))
        shapes = result["aggregates"]["by_origin_field_not_po_like_shape"]
        # FM custPoCode not-PO-like: "Verbal", "PO 4500", "000" (counts only).
        fm = shapes["fm_migrated"]["custom_fields.custPoCode"]
        self.assertEqual(fm["marker"], {"leading": 1, "none": 2})
        self.assertEqual(fm["probable"], {"false": 2, "true": 1})
        self.assertEqual(fm["reasons"], {"no_digit": 1, "only_zeros": 1, "whitespace": 1})
        self.assertEqual(fm["numeric_pattern"], {"3": 1, "mixed": 1, "no_digits": 1})
        for dimension in cpo.SHAPE_DIMENSIONS:
            self.assertEqual(sum(fm[dimension].values()), 3, dimension)
        self.assertEqual(sum(shapes["sage_import"]["po_reference"]["tokens"].values()), 1)
        self.assertNotIn("unknown", shapes)  # sparse: nothing not-PO-like there
        self.assertNotIn("by_origin_field_not_po_like_shape_signature", result["aggregates"])
        self.assertIn("never the values", cpo.SHAPE_NOTE)
        text = json.dumps(result["aggregates"])
        for value in NON_PO_TEXT:
            self.assertNotIn(value, text)
        signed = cpo.coverage(runtime.mirror, Actor("plx", OPS), shape_signatures=True)
        signatures = signed["aggregates"]["by_origin_field_not_po_like_shape_signature"]
        fm_signatures = signatures["fm_migrated"]["custom_fields.custPoCode"]
        self.assertEqual(sum(fm_signatures.values()), 3)
        self.assertIn("reasons=whitespace|marker=leading|tokens=2|digit_tokens=1|"
                      "alpha_tokens=1|digit_groups=1|max_digit_run=3-4|"
                      "numeric_pattern=mixed|length=3-10|probable=true", fm_signatures)
        self.assertEqual(signed["aggregates"]["by_origin_status"],
                         result["aggregates"]["by_origin_status"])

    def test_cli_prints_counts_only_and_never_the_key_or_values(self):
        import contextlib
        import io
        from integrations import workbench_read_api

        fake = self.fake
        real = workbench_read_api.WorkbenchReadApiClient
        out = io.StringIO()
        with patch.dict("os.environ", {"PLX_WORKBENCH_API_URL": BASE,
                                       "PLX_WORKBENCH_API_KEY": KEY}), \
                patch.object(workbench_read_api, "WorkbenchReadApiClient",
                             lambda base, key: real(base, key, fetch=fake)), \
                contextlib.redirect_stdout(out):
            code = cpo.main(["--state-dir", str(self.root / "cli"), "--audience",
                             str(self.audience_path), "--principal", "staff:operations"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        report = json.loads(text)
        self.assertEqual(report["schema"], "plx-customer-po-shape-report/v1")
        self.assertTrue(report["completeness"]["complete"])
        aggregates = report["aggregates"]
        self.assertEqual(aggregates["by_status"]["has_customer_po"], 4)
        self.assertIn("by_origin_field_not_po_like_shape_signature", aggregates)
        self.assertEqual(aggregates["by_lineage_status"]["sage_etl"],
                         {"has_customer_po": 1, "po_field_not_po_like": 1,
                          "absent_in_this_source": 1})
        self.assertEqual(aggregates["orders_with_fm_order_entry_created_by_portal"], {})
        self.assertNotIn(KEY, text)
        for value in NON_PO_TEXT + ("PO-5501", "PO12172025", "OR0001", "api://"):
            self.assertNotIn(value, text)
        self.assertNotIn("orders", report)
        # Without the read-only API variables the CLI refuses before any read.
        with patch.dict("os.environ", {}, clear=True), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cpo.main(["--state-dir", str(self.root / "cli2"), "--audience",
                      str(self.audience_path), "--principal", "staff:operations"])

    def test_date_filter_uses_a_mirrored_column_and_rejects_invented_ones(self):
        runtime = self.runtime()
        actor = Actor("plx", OPS)
        result = cpo.coverage(runtime.mirror, actor, date_column="updated_at",
                              date_from="2026-08-01", date_to="2026-08-31")
        self.assertEqual(sorted(self.by_id(result["orders"])), [3, 8])
        self.assertIn("updated_at", result["columns_read"])
        self.assertEqual(result["aggregates"]["rows"], 2)
        with self.assertRaises(MirrorError) as caught:
            cpo.coverage(runtime.mirror, actor, date_column="order_date",
                         date_from="2026-08-01")
        self.assertEqual(caught.exception.code, "unknown_column")
        for kwargs in ({"date_from": "2026-08-01"}, {"date_column": "updated_at"},
                       {"date_column": "updated_at", "date_from": "yesterday"},
                       {"origins": ["sage"]}, {"statuses": []}, {"scan_limit": 0}):
            with self.assertRaises(cpo.CustomerPoError, msg=str(kwargs)):
                cpo.coverage(runtime.mirror, actor, **kwargs)

    def test_actor_without_a_grant_gets_the_mirror_denial_and_no_rows(self):
        runtime = self.runtime()
        with self.assertRaises(MirrorError) as caught:
            cpo.coverage(runtime.mirror, Actor("plx", OUTSIDER))
        self.assertEqual(caught.exception.code, "not_visible")
        self.assertEqual(self.fake.row_requests("public.app_sales_order"), [])

    def test_more_rows_than_one_mirror_page_are_all_scanned(self):
        self.fake.tables["public.app_sales_order"] = [
            _order(ident, custom_fields={"fmId": ident}) for ident in range(1, 1201)]
        runtime = self.runtime()
        result = cpo.coverage(runtime.mirror, Actor("plx", OPS))
        completeness = result["completeness"]
        self.assertEqual((completeness["rows_matched_in_mirror"], completeness["rows_scanned"],
                          completeness["complete"]), (1200, 1200, True))
        self.assertEqual(len({order["key"]["id"] for order in result["orders"]}), 1200)
        self.assertEqual(result["aggregates"]["by_origin_status"]["fm_migrated"]
                         ["absent_in_this_source"], 1200)

    def test_scan_bound_is_reported_as_incomplete(self):
        runtime = self.runtime()
        result = cpo.coverage(runtime.mirror, Actor("plx", OPS), scan_limit=5)
        completeness = result["completeness"]
        self.assertFalse(completeness["complete"])
        self.assertFalse(completeness["scan_complete"])
        self.assertIn("scan_bound_reached", completeness["reasons"])
        self.assertEqual((completeness["rows_scanned"],
                          completeness["rows_matched_in_mirror"]), (5, len(ORDERS)))
        self.assertEqual(result["aggregates"]["rows"], 5)
        self.assertFalse(result["aggregates"]["complete"])

    def test_stale_capture_after_failed_refresh_is_reported_as_incomplete(self):
        runtime = self.runtime()
        actor = Actor("plx", OPS)
        self.assertTrue(cpo.coverage(runtime.mirror, actor)["completeness"]["complete"])
        self.fake.healthy = False
        self.clock.advance(5000)
        result = cpo.coverage(runtime.mirror, actor)
        completeness = result["completeness"]
        self.assertFalse(completeness["complete"])
        self.assertTrue(completeness["scan_complete"])
        self.assertIn("mirror_capture_stale", completeness["reasons"])
        self.assertIn("mirror_refresh_failed_serving_stored_capture", completeness["reasons"])
        self.assertTrue(completeness["mirror_capture"]["refresh_error"])
        self.assertEqual(len(result["orders"]), len(ORDERS))
        # Every order entry carries the stale label, whether a PO is present or absent.
        self.assertTrue(result["orders"])
        for order in result["orders"]:
            self.assertEqual(order["capture"], "stale")
            self.assertIn("may not be the Portal's current state", order["capture_note"])

    def test_a_current_capture_labels_each_order_current(self):
        result = cpo.coverage(self.runtime().mirror, Actor("plx", OPS))
        self.assertTrue(result["orders"])
        for order in result["orders"]:
            self.assertEqual(order["capture"], "current")
            self.assertNotIn("capture_note", order)

    def test_a_capture_change_between_pages_is_reported(self):
        class ShiftingMirror:
            def __init__(self):
                self.calls = 0

            def rows(self, actor, relation, **kwargs):
                self.calls += 1
                offset = kwargs["offset"]
                return {"columns": ["id", "po_reference", "custom_fields"],
                        "total_matched": 3, "returned": 1,
                        "next_offset": offset + 1 if offset < 2 else None,
                        "rows": [{"citation": f"api://public.app_sales_order/x@{offset + 1}",
                                  "key": {"id": offset}, "row": {"id": offset}}],
                        "freshness": {"latest_capture_id": self.calls, "key": ["id"],
                                      "last_refresh_at": "t", "stale": False,
                                      "refresh_error": None, "caveats": []}}

        result = cpo.coverage(ShiftingMirror(), Actor("plx", OPS))
        self.assertFalse(result["completeness"]["complete"])
        self.assertIn("mirror_changed_during_scan", result["completeness"]["reasons"])


class PortalLineageTests(CoverageTestCase):
    """Portal facts mirrored from docs/reference/portal-code-map.md s.3.2-3.4 and s.9
    (petralabx/plx-customer-portal staging@6c9415973; no Portal checkout here)."""

    COLUMNS = ["id", "order_number", "po_reference", "custom_fields"]

    def test_a_value_equal_to_the_order_number_is_ambiguous_not_a_po_and_not_a_miss(self):
        # ensure-fm-oe-from-sales-order.ts: custpocode = po_reference ?? order_number;
        # FM-ensure copies it back into an empty po_reference on link.
        bridged = cpo.classify_order(
            {"order_number": "SO100123", "po_reference": " SO100123",
             "custom_fields": {"fmId": 900123, "source": "gold-so-bridge"}}, self.COLUMNS)
        self.assertEqual(bridged["fields"]["po_reference"],
                         {"status": "present_equals_order_number",
                          "matches": ["order_number"], "possible_writers": "portal_order_entry"})
        self.assertEqual(bridged["status"], "absent_in_this_source")  # was has_customer_po
        self.assertEqual(bridged["equals_order_number_fields"], ["po_reference"])
        self.assertTrue(bridged["fm_order_entry_created_by_portal"])
        self.assertNotIn("customer_po_values", bridged)
        self.assertFalse(bridged["probable_po_after_normalization"])
        # BC renumbering rewrites order_number; the old number stays in fmOeNumber.
        renumbered = cpo.classify_order(
            {"order_number": "101234", "po_reference": "SO100124",
             "custom_fields": {"fmId": 77, "source": "fm-oe-ensure",
                               "fmOeNumber": "SO100124"}}, self.COLUMNS)
        self.assertEqual(renumbered["fields"]["po_reference"]["matches"],
                         ["custom_fields.fmOeNumber"])
        self.assertEqual(renumbered["status"], "absent_in_this_source")
        self.assertNotIn("fm_order_entry_created_by_portal", renumbered)
        # Equality is not proof of the fallback: an explicitly typed value equal to the
        # order number gets the same ambiguous label.
        typed = cpo.classify_order({"order_number": "SO7", "po_reference": "SO7",
                                    "custom_fields": None}, self.COLUMNS)
        self.assertEqual(typed["fields"]["po_reference"]["status"],
                         "present_equals_order_number")
        self.assertEqual(typed["status"], "absent_in_this_source")
        self.assertNotIn("fallback", cpo.FIELD_STATUSES[2])
        # A real PO next to it still counts; a different value is never a match.
        mixed = cpo.classify_order(
            {"order_number": "SO1", "po_reference": "PO-77",
             "custom_fields": {"fmId": 5, "custPoCode": "SO1"}}, self.COLUMNS)
        self.assertEqual(mixed["status"], "has_customer_po")
        self.assertEqual(mixed["customer_po_values"], ["PO-77"])
        self.assertEqual(mixed["fields"]["custom_fields.custPoCode"]["status"],
                         "present_equals_order_number")
        # Fallback next to non-PO text: still a not-PO-like review, not absent.
        text = cpo.classify_order(
            {"order_number": "SO2", "po_reference": "SO2",
             "custom_fields": {"fmId": 6, "custPoCode": "Jane Buyer"}}, self.COLUMNS)
        self.assertEqual(text["status"], "po_field_not_po_like")
        self.assertEqual(cpo.classify_field(True, "SO3", {"order_number": "SO3x"})["status"],
                         "present_po_like")

    def test_po_fields_say_which_portal_writer_filled_them(self):
        cases = (
            ({"fmId": 9, "custPoCode": "Jane Buyer"}, None, "custom_fields.custPoCode",
             "legacy_fm_sql_custpocode"),
            ({"fmId": 9, "custPoCode": ""}, "Jane Buyer", "po_reference",
             "legacy_fm_reference_text_migration"),
            ({"fmId": 9, "source": "fm-oe-ensure"}, "NA", "po_reference",
             "fm_order_entry_custpocode"),
            ({"sageOrdUniq": "5"}, "Mike", "po_reference",
             "sage_ponumber_or_retained_earlier_value"),
            ({}, "PO-1", "po_reference", "portal_order_entry"))
        for fields, reference, path, writer in cases:
            order = cpo.classify_order({"order_number": "X", "po_reference": reference,
                                        "custom_fields": fields}, self.COLUMNS)
            self.assertEqual(order["fields"][path]["possible_writers"], writer, fields)
            self.assertEqual(cpo.possible_writers(path, order["origin"]["lineage"]), writer)

    def test_aggregates_count_lineages_and_portal_created_fm_rows(self):
        self.fake.tables["public.app_sales_order"] = [
            _order(1, po_reference="SO100001", number="SO100001",
                   custom_fields={"fmId": 900001, "source": "gold-so-bridge"}),
            _order(2, po_reference="PO-55", custom_fields={"fmId": 11, "source": "fm-oe-ensure"}),
            _order(3, po_reference="PO-56", custom_fields={"sageOrdUniq": "501", "fmId": 12}),
            _order(4, custom_fields={"fmId": 13, "custPoCode": "PO-57"})]
        result = cpo.coverage(self.runtime().mirror, Actor("plx", OPS))
        aggregates = result["aggregates"]
        self.assertEqual(aggregates["by_lineage_status"],
                         {"sage_etl": {"has_customer_po": 1},
                          "fm_oe_ensure": {"has_customer_po": 1},
                          "legacy_fm_sql": {"has_customer_po": 1},
                          "gold_so_bridge": {"absent_in_this_source": 1}})
        self.assertEqual(aggregates["orders_with_fm_order_entry_created_by_portal"],
                         {"portal_entered": 1})
        self.assertEqual(aggregates["by_origin_field_status"]["portal_entered"]["po_reference"]
                         ["present_equals_order_number"], 1)
        bridged = self.by_id(result["orders"])[1]
        self.assertIn({"source": "public.order_entry.custpocode",
                       "availability": "not_checked"}, bridged["possible_sources"])
        self.assertIn("staging primary", result["evidence_mode"])
        self.assertIn("not the UAT DB", result["evidence_mode"])

    def test_cli_cross_checks_fm_custpocode_and_sage_ponumber_as_counts(self):
        import contextlib
        import io
        from integrations import workbench_read_api

        self.fake.tables["public.app_sales_order"] = [
            _order(1, custom_fields={"fmId": 10, "custPoCode": "Verbal"}),
            _order(2, number="SO100001",
                   custom_fields={"fmId": 900001, "source": "gold-so-bridge"}),
            _order(3, po_reference="PO-55", custom_fields={"fmId": 11, "source": "fm-oe-ensure"}),
            _order(4, custom_fields={"fmId": 12}),
            _order(5, po_reference="Mike", custom_fields={**SAGE, "sageOrdUniq": "501"}),
            _order(6, po_reference="Jane Buyer", custom_fields={"sageOrdUniq": "502"}),
            _order(7, po_reference="PO-9", custom_fields={**SAGE, "sageOrdNumber": "S9"})]
        self.fake.catalog["public.order_entry"] = {
            "columns": ["id", "erpcode", "custpocode", "reference", "audittimestampmodified"],
            "key": ["id"], "domain": "orders",
            "since": {"column": "audittimestampmodified", "mode": "timestamp-utc"}}
        stamp = "2026-09-29T09:00:00"
        self.fake.tables["public.order_entry"] = [
            {"id": 10, "erpcode": "OR10", "custpocode": "PO-4410", "reference": "r",
             "audittimestampmodified": stamp},
            {"id": 900001, "erpcode": "SO100001", "custpocode": "SO100001", "reference": None,
             "audittimestampmodified": stamp},
            {"id": 11, "erpcode": "OR11", "custpocode": "PO-55", "reference": None,
             "audittimestampmodified": stamp}]
        self.fake.catalog["sage.oe_order_header"] = {
            "columns": ["_sage_pk", "orduniq", "ordnumber", "ponumber", "audtdate"],
            "key": ["_sage_pk"], "domain": "orders",
            "since": {"column": "audtdate", "mode": "yyyymmdd-text"}}
        self.fake.tables["sage.oe_order_header"] = [
            {"_sage_pk": "1", "orduniq": "501.000", "ordnumber": "S5", "ponumber": "Mike",
             "audtdate": "20260928"},
            {"_sage_pk": "2", "orduniq": "502", "ordnumber": "S6", "ponumber": " ",
             "audtdate": "20260928"},
            {"_sage_pk": "3", "orduniq": "503", "ordnumber": "S9", "ponumber": "PO-10",
             "audtdate": "20260928"}]
        fake, real = self.fake, workbench_read_api.WorkbenchReadApiClient

        def run(*principals):
            out = io.StringIO()
            argv = ["--state-dir", str(self.root / ("cli-" + "-".join(principals))),
                    "--audience", str(self.audience_path)]
            for principal in principals:
                argv += ["--principal", principal]
            with patch.dict("os.environ", {"PLX_WORKBENCH_API_URL": BASE,
                                           "PLX_WORKBENCH_API_KEY": KEY}), \
                    patch.object(workbench_read_api, "WorkbenchReadApiClient",
                                 lambda base, key: real(base, key, fetch=fake)), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(cpo.main(argv), 0)
            return out.getvalue()

        text = run("staff:operations", "staff:finance")
        for value in ("PO-4410", "Mike", "SO100001", "Jane Buyer", "PO-55", "Verbal", "OR10",
                      "PO-10", "501", KEY, "api://"):
            self.assertNotIn(value, text)
        checks = json.loads(text)["cross_checks"]
        fm = checks["fm_order_entry"]
        self.assertTrue(fm["read"])
        self.assertIsNone(fm["problem"])
        self.assertEqual(fm["by_lineage"], {
            "legacy_fm_sql": {"fm_created_fm_row": 1, "custpocode_po_like_portal_has_no_po": 1},
            "gold_so_bridge": {"portal_created_fm_row": 1,
                               "custpocode_equals_order_number_portal_created": 1},
            "fm_oe_ensure": {"fm_created_fm_row": 1, "custpocode_po_like_same_as_portal_po": 1},
            "fm_linked_without_source": {"fm_created_fm_row": 1, "fm_row_not_found": 1}})
        sage = checks["sage_order_header"]
        self.assertEqual(sage["by_po_reference_status"], {
            "present_not_po_like": {"ponumber_equals_po_reference": 1, "ponumber_empty": 1},
            "present_po_like": {"ponumber_differs": 1, "ponumber_differs_and_po_like": 1}})
        # Without a sage.* grant the Sage cross-check is refused and reported, not raised.
        refused = json.loads(run("staff:operations"))["cross_checks"]["sage_order_header"]
        self.assertFalse(refused["read"])
        self.assertTrue(refused["problem"].startswith("mirror_refused:"))
        self.assertEqual(refused["by_po_reference_status"], {})


class DateFilterAndProjectionTests(CoverageTestCase):
    DATED = [
        _order(1, updated="2026-07-31T23:59:59"),
        _order(2, updated="2026-08-01"),
        _order(3, updated="2026-08-31T15:00:00"),           # final day, after midnight
        _order(4, updated="2026-08-31"),
        _order(5, updated="2026-09-01T00:00:00"),
        _order(6, updated="2026-09-01T02:00:00+05:00"),     # 2026-08-31T21:00Z
        _order(7, updated="2026-08-31T20:00:00-05:00"),     # 2026-09-01T01:00Z
        _order(8, updated=None),
    ]

    def dated(self, **kwargs):
        self.fake.tables["public.app_sales_order"] = [dict(row) for row in self.DATED]
        return cpo.coverage(self.runtime().mirror, Actor("plx", OPS),
                            date_column="updated_at", **kwargs)

    def test_postgres_hour_only_offsets_filter_and_garbage_is_still_counted(self):
        rows = [_order(1, updated="2026-01-05 10:11:12.123+00"),
                _order(2, updated="2026-01-05 23:30:00-08"),    # 2026-01-06T07:30Z
                _order(3, updated="2026-01-05 10:11:12+7"),       # garbage
                _order(4, updated="2026-01-05 10:11:12+0")]      # garbage
        self.fake.tables["public.app_sales_order"] = rows
        result = cpo.coverage(self.runtime().mirror, Actor("plx", OPS), date_column="updated_at",
                              date_from="2026-01-05T00:00:00Z", date_to="2026-01-05T23:59:59Z")
        self.assertEqual(sorted(self.by_id(result["orders"])), [1])
        self.assertEqual(result["completeness"]["date_values_unparseable"], 2)
        self.assertFalse(result["completeness"]["complete"])
        self.assertIn("date_values_unparseable", result["completeness"]["reasons"])

    def test_a_date_only_date_to_includes_that_whole_day(self):
        result = self.dated(date_from="2026-08-01", date_to="2026-08-31")
        # Date bounds compare each value's own calendar date.
        self.assertEqual(sorted(self.by_id(result["orders"])), [2, 3, 4, 7])
        self.assertEqual(result["aggregates"]["rows"], 4)
        self.assertEqual(result["completeness"]["rows_in_date_range"], 4)
        self.assertTrue(result["completeness"]["complete"])
        self.assertIn("whole day", result["filters"]["date_filter_note"])
        same_day = self.dated(date_from="2026-08-31", date_to="2026-08-31")
        self.assertEqual(sorted(self.by_id(same_day["orders"])), [3, 4, 7])

    def test_mixed_offset_timestamps_compare_as_instants(self):
        result = self.dated(date_from="2026-08-31T20:30:00Z", date_to="2026-08-31T23:00:00Z")
        # 6 is 21:00Z (string-wise it sorts after the bound); 7 is 01:00Z next day
        # (string-wise inside the window); 4 is a whole day overlapping the window.
        self.assertEqual(sorted(self.by_id(result["orders"])), [4, 6])
        offset_bound = self.dated(date_to="2026-09-01T01:00:00+01:00")  # 00:00Z
        self.assertEqual(sorted(self.by_id(offset_bound["orders"])), [1, 2, 3, 4, 5, 6])
        naive_bound = self.dated(date_from="2026-09-01T00:30:00")  # read as UTC
        self.assertEqual(sorted(self.by_id(naive_bound["orders"])), [7])

    def test_invalid_or_reversed_bounds_are_refused(self):
        runtime = self.runtime()
        actor = Actor("plx", OPS)
        for kwargs, code in (
                ({"date_from": "2026-02-30"}, "invalid_date_bound"),
                ({"date_to": "2026-13-01"}, "invalid_date_bound"),
                ({"date_from": "2026-08-01T25:00"}, "invalid_date_bound"),
                ({"date_from": "2026-09-01", "date_to": "2026-08-31"}, "reversed_date_bounds"),
                ({"date_from": "2026-08-31T12:00:00Z", "date_to": "2026-08-31T11:59:59Z"},
                 "reversed_date_bounds"),
                ({"date_from": "2026-09-01T00:00:00Z", "date_to": "2026-08-31"},
                 "reversed_date_bounds")):
            with self.assertRaises(cpo.CustomerPoError, msg=str(kwargs)) as caught:
                cpo.coverage(runtime.mirror, actor, date_column="updated_at", **kwargs)
            self.assertEqual(caught.exception.code, code, kwargs)
        self.assertEqual(self.fake.row_requests("public.app_sales_order"), [])
        # A date-only to against a same-day timestamp from is not reversed.
        cpo.coverage(runtime.mirror, actor, date_column="updated_at",
                     date_from="2026-08-31T12:00:00Z", date_to="2026-08-31")

    def test_unparseable_date_values_are_counted_and_make_the_result_incomplete(self):
        self.DATED = self.DATED + [_order(9, updated="31/08/2026"), _order(10, updated="")]
        result = self.dated(date_from="2026-08-01")
        self.assertNotIn(9, self.by_id(result["orders"]))
        self.assertEqual(result["completeness"]["date_values_unparseable"], 2)
        self.assertIn("date_values_unparseable", result["completeness"]["reasons"])
        self.assertFalse(result["completeness"]["complete"])
        self.assertFalse(result["aggregates"]["complete"])

    def test_a_po_column_missing_from_the_projection_is_not_inspectable(self):
        entry = self.fake.catalog["public.app_sales_order"]
        entry["columns"] = [name for name in entry["columns"] if name != "po_reference"]
        result = cpo.coverage(self.runtime().mirror, Actor("plx", OPS))
        self.assertEqual(result["columns_expected_but_not_mirrored"], ["po_reference"])
        orders = self.by_id(result["orders"])
        for ident, order in orders.items():
            self.assertEqual(order["fields"]["po_reference"],
                             {"status": "not_inspectable", "reason": "column_not_mirrored"})
            self.assertNotEqual(order["status"], "absent_in_this_source", ident)
        self.assertEqual(result["aggregates"]["by_status"]["absent_in_this_source"], 0)
        # custPoCode still counts where it holds a PO.
        self.assertEqual(orders[4]["status"], "has_customer_po")
        self.assertEqual(orders[3]["status"], "po_fields_not_inspectable")


class ProposalTestCase(CoverageTestCase):
    """A case with one cited customer-PO proposal recorded through the generic
    ``CaseStore.propose`` (what ``plx.case_propose`` records), for the read-back tests."""

    def setUp(self) -> None:
        super().setUp()
        self.rt = self.runtime()
        self.ops = Actor("plx", OPS)
        self.both = Actor("plx", OPS_FINANCE)
        self.case_id = self.rt.cases.open(self.both, "Which orders lack a customer PO?")
        self.orders = self.by_id(cpo.coverage(self.rt.mirror, self.both)["orders"])
        sage = self.rt.mirror.rows(self.both, "sage.oe_order_header")["rows"]
        self.sage = {item["key"]["_sage_pk"]: item["citation"] for item in sage}

    def propose(self, actor=None, value="PO-7788", values=None):
        order = self.orders[3]
        receipt = self.rt.cases.propose(
            actor or self.both, self.case_id, "portal.sales_order.set_customer_po",
            {"proposed_customer_po": value, "order_citation": order["citation"]},
            ["a person at PLX confirms the evidence belongs to this order"],
            [self.sage["3"], order["citation"]],
            evidence={"statement": "The cited Sage header holds the proposed PO in ponumber."},
            expected_effect={"relation": cpo.RELATION, "key": order["key"],
                             "values": values or {"po_reference": value}})
        return {"proposal": receipt}


ORDER_ENTRY_HEADER = {
    "columns": ["id", "erpcode", "custpocode", "reference", "audittimestampmodified"],
    "key": ["id"], "domain": "orders",
    "since": {"column": "audittimestampmodified", "mode": "timestamp-utc"}}


class PortalDivergenceTests(CoverageTestCase):
    """Customer PO rows shaped
    like the Portal contract (petralabx/plx-customer-portal staging@8da33e4, files
    unchanged since 6c9415973)."""

    COLUMNS = ["id", "order_number", "po_reference", "custom_fields"]

    def test_origin_uses_portal_trim_text_on_any_json_value(self):
        # lib/wms/sage-ship-queue.ts: trimText(v) = String(v).trim() || null, so a
        # numeric sageOrdUniq (Sage orduniq decoded as a float) is Sage-backed and
        # wins over fmId (isFmOnlySalesOrder).
        sage = cpo.classify_origin({"custom_fields": {"sageOrdUniq": 70123.0, "fmId": "351"}},
                                   self.COLUMNS)
        self.assertEqual((sage["value"], sage["lineage"]), ("sage_import", "sage_etl"))
        self.assertIn("isFmOnlySalesOrder", sage["rule"])
        self.assertEqual(cpo.classify_origin(
            {"custom_fields": {"sageSourceOrdUniq": "\ufeff 9 "}}, self.COLUMNS)["value"],
            "sage_import")
        self.assertEqual(cpo.classify_origin(
            {"custom_fields": {"source": "\ufeffsage.oe_order_header\n"}}, self.COLUMNS)["value"],
            "sage_import")
        for blank in (None, "", "  ", "\ufeff", []):
            self.assertIsNone(cpo.portal_trim_text(blank), blank)
            self.assertNotEqual(cpo.classify_origin(
                {"custom_fields": {"sageOrdUniq": blank}}, self.COLUMNS)["value"], "sage_import")
        for value in (0, False, {"a": 1}, [None, 2]):  # String(): "0", "false", ...
            self.assertIsNotNone(cpo.portal_trim_text(value), value)
        # parseFmOeId: a float fmId is truncated, so 900001.0 is a Portal-created FM row.
        bridged = cpo.classify_order({"order_number": "SO100001", "po_reference": None,
                                      "custom_fields": {"fmId": 900001.0,
                                                        "source": "gold-so-bridge"}},
                                     self.COLUMNS)
        self.assertTrue(bridged["fm_order_entry_created_by_portal"])
        self.assertEqual(cpo.parse_fm_oe_id(" 900002 "), 900002)
        for bad in ("9e5", "12a", True, None, "", float("nan")):
            self.assertIsNone(cpo.parse_fm_oe_id(bad), bad)

    def _fm_rows(self, rows):
        self.fake.catalog["public.order_entry"] = dict(ORDER_ENTRY_HEADER)
        stamp = "2026-09-29T09:00:00"
        self.fake.tables["public.order_entry"] = [
            {"reference": None, "audittimestampmodified": stamp, **row} for row in rows]

    def test_coverage_reads_the_fm_order_po_and_treats_the_bridge_fallback_as_no_po(self):
        self.fake.tables["public.app_sales_order"] = [
            # Portal-entered MAKE order bridged to FM, later renumbered by BC
            # (align-portal-so-number.ts): the FM row keeps erpcode = the old number and
            # custpocode = po_reference ?? order_number = that old number.
            _order(1, number="101234",
                   custom_fields={"fmId": "900001", "source": "gold-so-bridge",
                                  "erpCode": "SO100001", "renumberedTo": "101234"}),
            # FM-ensured order whose FM row carries the customer's PO (SO 351 shape).
            _order(2, number="OR1000151885",
                   custom_fields={"fmId": 351, "erpCode": "OR1000151885",
                                  "source": "fm-oe-ensure"}),
            # FM row not in the capture: not found, never "no PO".
            _order(3, custom_fields={"fmId": 352, "source": "fm-oe-ensure"}),
            _order(4, po_reference="PO-1", custom_fields=None)]
        self._fm_rows([
            {"id": 900001, "erpcode": "SO100001", "custpocode": "SO100001"},
            {"id": 351, "erpcode": "OR1000151885", "custpocode": "PO12172025"}])
        runtime = self.runtime()
        actor = Actor("plx", OPS)
        result = cpo.coverage(runtime.mirror, actor)
        orders = self.by_id(result["orders"])
        bridged = orders[1]["fm_order_entry"]
        self.assertEqual(bridged["status"], "present_equals_order_number")
        self.assertEqual(bridged["custpocode"]["matches"], ["public.order_entry.erpcode"])
        self.assertTrue(bridged["created_by_portal"])
        self.assertEqual(bridged["custpocode"]["possible_writers"],
                         "portal_gold_so_bridge_po_reference_or_order_number")
        self.assertNotIn("fm_order_entry_holds_customer_po", orders[1])
        self.assertIn({"source": "public.order_entry.custpocode", "availability": "read"},
                      orders[1]["possible_sources"])
        fm = orders[2]["fm_order_entry"]
        self.assertEqual((fm["status"], fm["custpocode"]["value"], fm["created_by_portal"]),
                         ("present_po_like", "PO12172025", False))
        self.assertEqual(fm["custpocode"]["possible_writers"], "filemaker_user")
        self.assertTrue(orders[2]["fm_order_entry_holds_customer_po"])
        self.assertEqual(orders[2]["status"], "absent_in_this_source")  # the Portal row only
        cited = runtime.mirror.resolve_citation(fm["citation"], actor)
        self.assertEqual((cited["relation"], cited["row"]["custpocode"]),
                         ("public.order_entry", "PO12172025"))
        self.assertEqual(orders[3]["fm_order_entry"]["status"], "fm_row_not_found")
        self.assertNotIn("fm_order_entry", orders[4])
        aggregates = result["aggregates"]
        self.assertEqual(aggregates["by_origin_fm_order_entry_custpocode_status"], {
            "portal_entered": {"present_equals_order_number": 1},
            "fm_migrated": {"fm_row_not_found": 1, "present_po_like": 1}})
        self.assertEqual(aggregates["orders_without_portal_po_whose_fm_order_entry_holds_one"],
                         {"fm_migrated": 1})
        self.assertEqual(result["completeness"]["linked_reads"]["public.order_entry"],
                         {"read": True, "problem": None, "orders_with_fm_id": 3})
        self.assertIn("900000", result["rules"]["fm_order_entry"])

    def test_incomplete_fm_scan_never_labels_an_unread_row_read(self):
        """A capped FM scan that missed the order's row is
        fm_row_not_scanned / read_incomplete, never fm_row_not_found / read."""
        self.fake.tables["public.app_sales_order"] = [
            _order(1, custom_fields={"fmId": 351, "source": "fm-oe-ensure"}),
            _order(2, custom_fields={"fmId": 900001, "source": "gold-so-bridge"})]
        # Portal-created ids (>= 900000) sort last, so a capped scan misses them first.
        self._fm_rows([{"id": 351, "erpcode": "OR1", "custpocode": "PO12172025"},
                       {"id": 352, "erpcode": "OR2", "custpocode": None},
                       {"id": 900001, "erpcode": "SO100001", "custpocode": "SO100001"}])
        runtime = self.runtime()
        with patch.object(cpo, "MAX_SCAN_ROWS", 2):
            result = cpo.coverage(runtime.mirror, Actor("plx", OPS), scan_limit=2)
        orders = self.by_id(result["orders"])
        linked = result["completeness"]["linked_reads"]["public.order_entry"]
        self.assertTrue(linked["read"])
        self.assertTrue(linked["problem"].startswith("incomplete:"), linked)
        read = orders[1]["fm_order_entry"]
        self.assertEqual(read["status"], "present_po_like")
        self.assertIn({"source": "public.order_entry.custpocode", "availability": "read"},
                      orders[1]["possible_sources"])
        missed = orders[2]["fm_order_entry"]
        self.assertEqual(missed["status"], "fm_row_not_scanned")
        self.assertEqual(missed["problem"], linked["problem"])
        self.assertNotIn("citation", missed)
        sources = {item["source"]: item["availability"]
                   for item in orders[2]["possible_sources"]}
        self.assertEqual(sources["public.order_entry.custpocode"], "read_incomplete")
        self.assertEqual(result["aggregates"]["by_origin_fm_order_entry_custpocode_status"], {
            "fm_migrated": {"present_po_like": 1},
            "portal_entered": {"fm_row_not_scanned": 1}})
        with patch.object(cpo, "MAX_SCAN_ROWS", 2):
            checks = cpo._cross_checks(runtime.mirror, Actor("plx", OPS), None)
        self.assertEqual(checks["fm_order_entry"]["by_lineage"]["gold_so_bridge"],
                         {"portal_created_fm_row": 1, "fm_row_not_scanned": 1})

    def test_fm_scan_that_read_every_row_but_changed_is_inconsistent_not_unscanned(self):
        scan = {"rows": [{"row": {"id": 1}}], "columns": ["id"], "freshness": {},
                "total": 1, "reasons": ["mirror_changed_during_scan"]}
        with patch.object(cpo, "_scan", return_value=scan):
            _, problem = cpo._related_rows(None, Actor("plx", OPS), cpo.FM_RELATION, None)
        self.assertEqual(problem, "inconsistent:mirror_changed_during_scan")
        mixed = dict(scan, reasons=["mirror_changed_during_scan", "scan_bound_reached"])
        with patch.object(cpo, "_scan", return_value=mixed):
            _, other = cpo._related_rows(None, Actor("plx", OPS), cpo.FM_RELATION, None)
        self.assertTrue(other.startswith("incomplete:"), other)
        row = _order(2, custom_fields={"fmId": 351, "source": "fm-oe-ensure"})
        columns = list(row)
        block = cpo._fm_order_entry_block(row, columns, {}, {"problem": problem})
        self.assertEqual(block["status"], "fm_scan_inconsistent")
        counts = cpo._fm_cross_check([row], columns, {"rows": [], "columns": ["id"]}, problem)
        self.assertEqual(
            sum(c.get("fm_scan_inconsistent", 0) for c in counts["by_lineage"].values()), 1)
        self.assertNotIn("fm_row_not_scanned", str(counts["by_lineage"]))

    def test_replica_label_is_one_wording(self):
        self.assertIn(cpo.REPLICA_LABEL, cpo.EVIDENCE_MODE)
        stage = Path(cpo.__file__).with_name("process_stage_map.json").read_text("utf-8")
        self.assertIn(cpo.REPLICA_LABEL, stage)
        self.assertNotIn("(FM/Sage/ETL)", stage)

    def test_fm_order_entry_follows_the_actor_audience(self):
        self.audience_path.write_text(json.dumps({
            "schema": AUDIENCE_SCHEMA, "tenant": "plx",
            "audiences": {"public.app_sales_order": ["staff:operations"],
                          "public.order_entry": ["staff:finance"]}}), encoding="utf-8")
        self.fake.tables["public.app_sales_order"] = [
            _order(2, custom_fields={"fmId": 351, "source": "fm-oe-ensure"})]
        self._fm_rows([{"id": 351, "erpcode": "OR1", "custpocode": "PO12172025"}])
        runtime = self.runtime()
        result = cpo.coverage(runtime.mirror, Actor("plx", OPS))
        self.assertNotIn("fm_order_entry", result["orders"][0])
        self.assertNotIn("PO12172025", json.dumps(result))
        linked = result["completeness"]["linked_reads"]["public.order_entry"]
        self.assertFalse(linked["read"])
        self.assertTrue(linked["problem"].startswith("mirror_refused:"))
        both = cpo.coverage(runtime.mirror, Actor("plx", OPS_FINANCE))
        self.assertEqual(both["orders"][0]["fm_order_entry"]["status"], "present_po_like")
        off = cpo.coverage(runtime.mirror, Actor("plx", OPS_FINANCE),
                           read_fm_order_entry=False)
        self.assertEqual(off["completeness"]["linked_reads"]["public.order_entry"]["problem"],
                         "not_requested")

    def test_bc_external_document_no_overwrite_is_accounted_and_no_stale_gap_text(self):
        bc = cpo.POSSIBLE_SOURCES["business_central"]["why"]
        for fact in ("External Document No.", "shipment number", "PostSalesShipOnly",
                     "serializeSalesInvoice", "order number"):
            self.assertIn(fact, bc)
        self.assertRegex(bc, r"before\s+PlxSalesBridge@1c5dddb")
        for stale in ("Entra", "no secret", "unreadable", "not available",
                      "only in the auditor environment"):
            for item in cpo.POSSIBLE_SOURCES.values():
                self.assertNotIn(stale, item["why"])

    def test_counts_only_report_names_the_staging_primary_replica(self):
        self.fake.tables["public.app_sales_order"] = [_order(1, po_reference="PO-1")]
        report = cpo.shape_report(self.runtime().mirror, Actor("plx", OPS), cross_check=False)
        self.assertIn("staging primary", report["evidence_mode"])
        self.assertIn("not the UAT DB", report["evidence_mode"])


_PDF = importlib.util.find_spec("pypdf") and importlib.util.find_spec("reportlab")


@unittest.skipUnless(_PDF, "documents are PDFs: needs pypdf and reportlab")
class DocumentCarrierTests(unittest.TestCase):
    """REC-01: information a captured document states that Portal lacks or contradicts,
    over every Portal record of each attested kind (``document_carriers``). The fixture
    of ``test_attested_missing`` (isolated-fixture / injected-transport): customer PO
    PDFs captured as mail attachments over the injected Graph transport naming Portal
    orders so1 (no PO), so2 (its own number), so3 (another PO), so4 (the same PO), so7
    (PO field not served), so5 (10 documents) and so6 (60 documents), plus shipping
    documents naming a purchase order and a shipment."""

    tools = tam.MissingInRecordTests.tools

    @classmethod
    def setUpClass(cls):
        tam.MissingInRecordTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        tam.MissingInRecordTests.tearDownClass.__func__(cls)

    def order(self, key):
        return f'SalesOrder:portal.app_sales_order:{{"id":"{key}"}}'

    def by_record(self, report):
        out: dict[str, list[dict]] = {}
        for finding in report["findings"]:
            out.setdefault(finding["record"], []).append(finding)
        return out

    def test_a_document_value_portal_lacks_is_missing_a_different_one_a_conflict(self):
        started = time.monotonic()
        report = cpo.document_carriers(self.tools(), tam.READER)
        self.assertLess(time.monotonic() - started, 10.0)
        found = self.by_record(report)
        (missing,) = found[self.order("so1")]
        self.assertEqual((missing["status"], missing["subject"], missing["tier"]),
                         ("missing_in_portal",
                          "SalesOrder.customer_po vs CustomerPoDocument.po_number", "lead"))
        self.assertEqual(missing["document"]["value"], "4500000001")
        self.assertTrue(missing["document"]["citation"]["l1_handle"].startswith(
            "graph-mail://"))
        (cited,) = missing["document_citations"]
        self.assertEqual((cited["page"], cited["l1_handle"]),
                         (1, missing["document"]["citation"]["l1_handle"]))
        (empty,) = missing["portal"]
        self.assertEqual((empty["value"], empty["citation"]["field_path"]),
                         (None, "row.po_reference"))
        self.assertTrue(empty["citation"]["l1_handle"].startswith(
            "api://public.app_sales_order/"))
        # The order's own number in the PO field is empty too.
        self.assertEqual([f["status"] for f in found[self.order("so2")]],
                         ["missing_in_portal"])
        (conflict,) = found[self.order("so3")]
        self.assertEqual((conflict["status"], conflict["document"]["value"],
                          [v["value"]["untrusted_text"] if isinstance(v["value"], dict)
                           else v["value"] for v in conflict["portal"]]),
                         ("conflict", "4500012345", ["4500099999"]))
        # Equal values, and a field the projection never read, report nothing.
        self.assertNotIn(self.order("so4"), found)
        self.assertNotIn(self.order("so7"), found)
        # Every attested Portal kind, through the same declarations (no per-kind code).
        self.assertEqual(report["by_carrier_status"], {
            "PurchaseOrder.incoterms vs ForwarderQuote.incoterm": {"conflict": 1},
            "PurchaseOrder.shipping_cost_estimate vs ForwarderQuote.amount":
                {"missing_in_portal": 2},
            "PurchaseOrder.tracking_number vs FreightBill.tracking_number":
                {"missing_in_portal": 1},
            "SalesOrder.customer_po vs CustomerPoDocument.po_number":
                {"conflict": 1, "missing_in_portal": 72},
            "Shipment.delivery_date vs DeliveryReceipt.delivery_date":
                {"missing_in_portal": 1}})
        self.assertEqual(report["records_checked"],
                         {"PurchaseOrder": 1, "SalesOrder": 7, "Shipment": 1})
        self.assertEqual(report["by_status"],
                         {"missing_in_portal": 76, "conflict": 2, "document_blank": 0})
        self.assertEqual(report["finding_count"], len(report["findings"]))
        # 1, 10 and 60 documents on one order: each stated PO once, none lost.
        for key, count in (("so1", 1), ("so5", 10), ("so6", 60)):
            stated = [f["document"]["value"] for f in found[self.order(key)]]
            self.assertEqual((len(stated), len(set(stated))), (count, count), key)
        with self.assertRaises(cpo.CustomerPoError):
            cpo.document_carriers(self.tools(), "staff:orders")

    def test_a_reader_gets_exactly_what_plx_object_shows_it(self):
        self.assertEqual(cpo.document_carriers(self.tools(), tam.OUTSIDER)["findings"], [])
        report = cpo.document_carriers(self.tools(), tam.NO_FINANCE)
        # Money it may not read gives no finding; the extraction citations are withheld.
        self.assertEqual(report["by_status"],
                         {"missing_in_portal": 74, "conflict": 2, "document_blank": 0})
        self.assertFalse(any("document_citations" in f for f in report["findings"]))
        tools = self.tools()
        for record, findings in self.by_record(report).items():
            shown = tools.call("plx.object", {"object_id": record}, tam.NO_FINANCE)
            if len(findings) > len(shown.get("leads", ())) + len(shown["conflicts"]):
                continue   # plx.object cut its leads to fit one response (said in a gap)
            listed = {json.dumps(item["values"][0], sort_keys=True)
                      for item in shown.get("leads", []) + shown["conflicts"]}
            for finding in findings:
                self.assertIn(json.dumps(finding["document"], sort_keys=True), listed, record)


if __name__ == "__main__":
    unittest.main()
