"""The one purchase-order status definition (operating/po_status.py) and its
``po_status.vocabulary`` live check.

Synthetic records only, with the real status words of the Portal purchase-order headers
(live capture 2026-10-20: Cancel, Complete, DRAFT, Pending, B.Order, Revised, Generated)
in mixed case. The live check runs over an injected transport (evidence label:
injected-transport), never presented as live.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from devloop import live_verify as lv
from integrations.http import SourceHttpError
from operating import po_status as ps

ROOT = Path(__file__).resolve().parents[2]
D = Decimal


class VocabularyTest(unittest.TestCase):
    """Every real word, in the case the source writes it and others, lands in its class."""

    def test_the_real_words_in_mixed_case(self):
        for raw in ("Cancel", "CANCEL", " cancel ", "Cancelled", "canceled"):
            self.assertEqual(ps.classify(raw, D(5), D(0)), ps.PoStatus(ps.CLOSED, "CANCELLED"),
                             raw)
        for raw in ("DRAFT", "Draft", "draft "):
            self.assertEqual(ps.classify(raw, D(5), D(0)), ps.PoStatus(ps.DRAFT, "DRAFT"), raw)
        for raw in ("Complete", "COMPLETE", "complete"):
            self.assertEqual(ps.classify(raw, D(10), D(10)),
                             ps.PoStatus(ps.CLOSED, "RECEIVED"), raw)
            self.assertEqual(ps.classify(raw, D(10), D(12)).cls, ps.CLOSED, raw)
        # ORDERED: what the purchase-order-sent hook writes on a send
        for raw in ("Pending", "PENDING", "B.Order", "b.order", "B.ORDER", "ORDERED"):
            self.assertEqual(ps.classify(raw, D(10), D(0)), ps.PoStatus(ps.OPEN, "SUBMITTED"),
                             raw)
            self.assertEqual(ps.classify(raw, D(10), D(4)), ps.PoStatus(ps.OPEN, "PARTIAL"),
                             raw)
        for raw in ("Revised", "REVISED", "Generated", "generated", "On Hold", "?"):
            self.assertEqual(ps.classify(raw, D(10), D(0)),
                             ps.PoStatus(ps.UNKNOWN, reason="status_unmapped"), raw)
        for raw in (None, "", "   ", True):
            self.assertEqual(ps.classify(raw, D(10), D(0)),
                             ps.PoStatus(ps.UNKNOWN, reason="status_missing"), raw)

    def test_complete_with_an_outstanding_quantity_is_closed_and_counted_canceled_remainder(self):
        # Ross: a Portal/FM Complete header is closed whatever was
        # received; what is still outstanding is a cancelled remainder, never open.
        self.assertEqual(ps.classify("Complete", D(10), D(4)),
                         ps.PoStatus(ps.CLOSED, ps.COMPLETE, canceled_remainder=D(6)))
        self.assertEqual(ps.classify("Complete", D(10), D(0)),
                         ps.PoStatus(ps.CLOSED, ps.COMPLETE, canceled_remainder=D(10)))
        # within EPS of the ordered quantity is received in full, as the Portal reads it
        self.assertEqual(ps.classify("Complete", D(10), D("9.99995")),
                         ps.PoStatus(ps.CLOSED, ps.RECEIVED))
        self.assertEqual(ps.classify("Complete", D(10), D("9.999")).canceled_remainder,
                         D("0.001"))
        # the FM header, which carries no received quantity, closes too (remainder unknown)
        fm = ps.classify("Complete", None, None)
        self.assertEqual(fm, ps.PoStatus(ps.CLOSED, ps.COMPLETE))
        self.assertEqual(ps.record({"fm_status": ["Complete"]}, None), fm)
        both = ps.combine([ps.classify("Complete", D(10), D(4)), fm])
        self.assertEqual((both.cls, both.code), (ps.CLOSED, ps.COMPLETE))
        self.assertEqual(ps.combine([fm, ps.bc("Released", True)]),
                         ps.PoStatus(ps.CLOSED, ps.RECEIVED))   # a twin received in full
        # never late, never at risk: closed
        self.assertEqual(ps.lateness_verdict(both, date(2026, 1, 1), None, date(2026, 10, 1)),
                         (ps.NEITHER, None))

    def test_a_closed_header_is_cancelled(self):
        # upstream open-po-lines/route.ts:46 @5afa0a20 CANCELLED_STATUSES holds "closed"
        for raw in ("closed", "Closed", " CLOSED "):
            self.assertEqual(ps.classify(raw, D(10), D(0)), ps.PoStatus(ps.CLOSED, "CANCELLED"),
                             raw)
            self.assertEqual(ps.header_class(raw), ps.IGNORED, raw)
            self.assertEqual(ps.record({"fm_status": [raw]}, None).code, "CANCELLED", raw)

    def test_an_fm_mirrored_line_with_gold_zero_is_received_by_its_qc_receipts(self):
        # The gold quantity_received is ETL-seeded 0 for FM-mirrored POs (upstream
        # open-po-lines/route.ts:117-120, po-queries.ts:529-533,572-574 @5afa0a20): the FM
        # QC receiving log (receipt sumqty by idpogenitem) is the received quantity.
        gold = [("10", "0", "ea")]
        self.assertEqual(ps.record({"status": ["Pending"]}, gold, [("10", "10", None)]),
                         ps.PoStatus(ps.CLOSED, ps.RECEIVED))
        self.assertEqual(ps.record({"status": ["Pending"]}, gold, [("10", "4", None)]),
                         ps.PoStatus(ps.OPEN, ps.PARTIAL))
        self.assertEqual(ps.record({"status": ["Complete"]}, gold, [("6", "3", None),
                                                                  ("4", None, None)]),
                         ps.PoStatus(ps.CLOSED, ps.COMPLETE, canceled_remainder=D(7)))
        # no QC row: the gold quantity stands; no FM twin: it stands too
        self.assertEqual(ps.record({"status": ["Pending"]}, gold, [("10", None, None)]),
                         ps.PoStatus(ps.OPEN, ps.SUBMITTED))
        self.assertEqual(ps.record({"status": ["Pending"]}, gold, None),
                         ps.PoStatus(ps.OPEN, ps.SUBMITTED))
        # a gold received quantity above 0 is not the seeded 0: the QC log is not read
        self.assertEqual(ps.record({"status": ["Pending"]}, [("10", "2", "ea")],
                                   [("10", "10", None)]), ps.PoStatus(ps.OPEN, ps.PARTIAL))
        # fail closed: the QC log unread, an FM ordered total that is not the gold one (the
        # only proof the unit-less QC quantity is in the gold unit) or an unreadable FM line
        for fm in (ps.UNREAD, [("12", "10", None)], [(ps.UNREAD, "1", None)],
                   [(None, "4", None)], [("10", "x", None)]):
            with self.subTest(fm=fm):
                self.assertEqual(ps.record({"status": ["Pending"]}, gold, fm),
                                 ps.PoStatus(ps.UNKNOWN, reason="quantities_unknown"))

    def test_a_sage_only_po_is_unknown_never_open(self):
        for value in (None, 0, 1, "1", "5"):
            status = ps.sage(value)
            self.assertEqual(status, ps.PoStatus(ps.UNKNOWN, reason="sage_unmapped"))
            self.assertNotEqual(ps.combine([status]).cls, ps.OPEN)
        self.assertEqual(ps.header_class("Complete", "sage"), ps.UNMAPPED)
        self.assertEqual(ps.header_class("DRAFT", "sage"), ps.UNMAPPED)

    def test_unreadable_quantities_and_units_are_unknown_never_summed(self):
        self.assertEqual(ps.classify("Pending", None, D(0)).reason, "quantities_unknown")
        self.assertEqual(ps.classify("Pending", D(0), D(0)).reason, "nothing_ordered")
        self.assertEqual(ps.classify("Pending", D(-3), D(0)).reason, "nothing_ordered")
        self.assertEqual(ps.line_totals([]), (D(0), D(0)))
        self.assertEqual(ps.line_totals([("10", None, "ea"), ("5", "5", "EA ")]),
                         (D(15), D(5)))   # a null received counts 0; units compared trimmed
        self.assertEqual(ps.line_totals([("10", "0", "ea"), ("5", "5", "kg")]), (None, None))
        self.assertEqual(ps.line_totals([("10", "0", "ea"), (None, "5", "ea")]), (None, None))
        self.assertEqual(ps.line_totals([("10", "x", "ea")]), (None, None))
        self.assertEqual(ps.line_totals([("NaN", "0", "ea")]), (None, None))
        self.assertEqual(ps.line_totals([("10", ps.UNREAD, "ea")]), (None, None))
        self.assertEqual(ps.line_totals([("10", "0", ps.UNREAD)]), (None, None))

    def test_bc_reads_its_own_status_and_fully_received(self):
        self.assertEqual(ps.bc("Draft", False).cls, ps.DRAFT)
        self.assertEqual(ps.bc("Open", "FALSE").cls, ps.OPEN)
        self.assertEqual(ps.bc("Released", True), ps.PoStatus(ps.CLOSED, "RECEIVED"))
        self.assertEqual(ps.bc("Released", None).reason, "status_missing")
        self.assertEqual(ps.bc("Released", "maybe").reason, "status_missing")

    def test_twins_that_disagree_are_disputed_and_unknowns_decide_nothing(self):
        open_, closed = ps.classify("Pending", D(5), D(0)), ps.bc("Released", True)
        self.assertEqual(ps.combine([open_, closed]),
                         ps.PoStatus(ps.UNKNOWN, reason="status_disputed"))
        self.assertEqual(ps.combine([ps.sage(), ps.classify("Revised", D(1), D(0))]).reason,
                         "status_unmapped")
        self.assertEqual(ps.combine([]).reason, "status_missing")
        self.assertEqual(ps.combine([open_, ps.classify("B.Order", D(5), D(2))]),
                         ps.PoStatus(ps.OPEN))   # one class, two codes: no code

    def test_one_record_by_its_source(self):
        partial = [("10", "4", "ea")]
        # Complete closes whatever was received; the outstanding quantity is cancelled
        self.assertEqual(ps.record({"status": ["Complete"]}, partial),
                         ps.PoStatus(ps.CLOSED, ps.COMPLETE, canceled_remainder=D(6)))
        self.assertEqual(ps.record({"status": ["Complete"]}, [("10", "10", "ea")]),
                         ps.PoStatus(ps.CLOSED, ps.RECEIVED))
        for lines in (None, []):   # its lines not read, nothing ordered: closed, no remainder
            self.assertEqual(ps.record({"status": ["Complete"]}, lines),
                             ps.PoStatus(ps.CLOSED, ps.COMPLETE), lines)
        # the FM header alone: Complete closes too; its other issued words stay unknown
        self.assertEqual(ps.record({"fm_status": ["Complete"]}, partial),
                         ps.PoStatus(ps.CLOSED, ps.COMPLETE))
        self.assertEqual(ps.record({"fm_status": ["B.Order"]}, partial).reason,
                         "quantities_unknown")
        self.assertEqual(ps.record({"fm_status": ["Cancel"]}, None).cls, ps.CLOSED)
        self.assertEqual(ps.record({"sage_is_complete": ["1"]}, [("5", "5", "ea")]).reason,
                         "sage_unmapped")
        self.assertEqual(ps.record({"status": ["Pending", "Cancel"]}, partial).reason,
                         "status_disputed")
        self.assertEqual(ps.record({"bc_status": ["Open"], "bc_fully_received": [False]},
                                   None).cls, ps.OPEN)
        self.assertEqual(ps.record({"bc_status": ["Draft"]}, None).cls, ps.DRAFT)
        self.assertEqual(ps.record({}, partial).reason, "status_missing")
        self.assertEqual(ps.record({"po_number": ["X"]}, partial).reason, "status_missing")


class OracleCrossCheckTest(unittest.TestCase):
    """The definition agrees with the oracle's derivePoFulfillmentCode
    (evals/oracle/keys.po_fulfillment_code) wherever it decides: every mapped word with
    something ordered. It is stricter where the Portal would guess (an unmapped or null
    word, nothing ordered: unknown, not SUBMITTED)."""

    def test_same_code_for_every_mapped_word_and_quantity(self):
        from evals.oracle import keys

        self.assertEqual(ps.CANCELLED_WORDS, keys.PO_HEADER_CANCELLED)
        self.assertEqual(ps.EPS, keys.PO_FULFILLMENT_EPS)
        # Complete is the one deliberate deviation (Ross): closed
        # whatever was received, asserted below
        words = sorted((ps.CANCELLED_WORDS | ps.DRAFT_WORDS | ps.ISSUED_WORDS)
                       - ps.COMPLETE_WORDS)
        cases = 0
        for raw, ordered, received in itertools.product(
                [w for word in words for w in (word, word.lower(), word.title())],
                (D("0.001"), D(1), D(10), D("10.5")),
                (D(-1), D(0), D("0.00005"), D(4), D("9.99995"), D(10), D("10.5"), D(12))):
            with self.subTest(raw=raw, ordered=ordered, received=received):
                status = ps.classify(raw, ordered, received)
                self.assertEqual(status.code, keys.po_fulfillment_code(raw, ordered, received))
                self.assertEqual(status.cls, {"CANCELLED": ps.CLOSED, "DRAFT": ps.DRAFT,
                                              "RECEIVED": ps.CLOSED}.get(status.code, ps.OPEN))
                cases += 1
        self.assertEqual(cases, len(words) * 3 * 4 * 8)
        self.assertEqual(keys.po_fulfillment_code("Complete", D(10), D(4)), "PARTIAL")
        self.assertEqual(ps.classify("Complete", D(10), D(4)),
                         ps.PoStatus(ps.CLOSED, ps.COMPLETE, canceled_remainder=D(6)))
        self.assertEqual(ps.classify("Complete", D(10), D(10)),
                         ps.PoStatus(ps.CLOSED, ps.RECEIVED))
        # closed mirrors upstream open-po-lines/route.ts:46 CANCELLED_STATUSES @5afa0a20
        self.assertEqual(ps.classify("closed", D(10), D(0)).cls, ps.CLOSED)

    def test_stricter_where_the_portal_would_guess(self):
        from evals.oracle import keys

        for raw in ("Revised", "Generated", None):
            self.assertEqual(keys.po_fulfillment_code(raw, D(5), D(0)), "SUBMITTED")
            self.assertEqual(ps.classify(raw, D(5), D(0)).cls, ps.UNKNOWN)
        self.assertEqual(keys.po_fulfillment_code("Pending", D(0), D(0)), "SUBMITTED")
        self.assertEqual(ps.classify("Pending", D(0), D(0)).cls, ps.UNKNOWN)


class OneDefinitionTest(unittest.TestCase):
    """No reader keeps its own PO status word list: the words live in po_status only."""

    GRAPHS = ("ontology/schema/process/po_delivery_window.json",
              "ontology/schema/process/purchase_to_invoice.json")
    # a status word of another record type, not a purchase order's
    OTHER_RECORDS = {"operating/proactive_gaps.py": ["DRAFT"]}   # QUOTE_OPEN: quotes

    def test_no_reader_or_po_graph_lists_po_status_words(self):
        import re

        listed = re.compile(r"""["'](SUBMITTED|PARTIAL|RECEIVED|CANCEL|CANCELLED|CANCELED|"""
                            r"""DRAFT|B\.ORDER|PENDING|COMPLETE|ORDERED|Cancel|Complete|Pending|"""
                            r"""B\.Order|Draft|Revised|Generated)["']""")
        readers = [path for path in [*sorted((ROOT / "operating").glob("*.py")),
                                     ROOT / "scripts/eval_packaging_oracle_check.py"]
                   if path.name != "po_status.py"
                   and "PurchaseOrder" in path.read_text("utf-8")]
        self.assertGreater(len(readers), 5)
        for path in readers:
            name = path.relative_to(ROOT).as_posix()
            text = path.read_text("utf-8")
            self.assertEqual(listed.findall(text), self.OTHER_RECORDS.get(name, []), name)
            self.assertNotIn("CLOSED_STATUSES", text, name)
        for name in self.GRAPHS:
            graph = json.loads((ROOT / name).read_text("utf-8"))
            for stage in graph["stages"]:
                for evidence in stage.get("evidence", ()):
                    if evidence.get("object_type") == "PurchaseOrder":
                        predicate = evidence["predicate"]
                        self.assertIn(predicate["kind"], ("po_status", "exists", "event"),
                                      (name, stage["id"]))
                        self.assertEqual(listed.findall(json.dumps(predicate)), [],
                                         (name, stage["id"]))
                        self.assertNotIn("ignore", predicate, (name, stage["id"]))
        window = json.loads((ROOT / self.GRAPHS[0]).read_text("utf-8"))
        self.assertEqual(window["open_when"]["definition"], "po_status")
        for rule in window["open_when"]["rules"]:
            self.assertEqual(set(rule), {"binding", "property"})

    CALLERS = ("operating/process_lateness.py", "operating/proactive_engine.py",
               "scripts/eval_packaging_oracle_check.py")

    def test_no_caller_keeps_a_twin_line_or_promise_collector(self):
        import re

        links = re.compile(r"""["'](PurchaseOrder\.same_record_as|PurchaseOrderLine\.line_of|"""
                           r"""SupplierConfirmation\.confirms)["']""")
        collectors = re.compile(r"^\s*(?:def|class)\s+(_twins|_po_lines|_line_quantities|"
                                r"_one_quantity|_po_state|_po_clusters|_po_read|_record_read|"
                                r"_read|_Read|_lines|_lines_spec|_values)\b"
                                r"|^(PO_LINE_LINK|LINE_PAGES)\s*=", re.M)
        for name in self.CALLERS:
            text = (ROOT / name).read_text("utf-8")
            self.assertEqual(links.findall(text), [], name)
            self.assertEqual(collectors.findall(text), [], name)
            self.assertIn("po_status.read(" if "scripts/" in name or "engine" in name
                          else "ps.read(", text, name)
        own = (ROOT / "operating/po_status.py").read_text("utf-8")
        self.assertEqual(len(links.findall(own)), 3)
        self.assertEqual(len(re.findall(r"^def read\(", own, re.M)), 1)


class LatenessVerdictTest(unittest.TestCase):
    """Vince's rule: late once the requested-by date has passed; at risk while it has not
    but the supplier confirmed later; a received PO is neither; no requested-by date is
    reported, never filled from the supplier's date."""

    TODAY = date(2026, 10, 21)

    def test_late_at_risk_and_neither(self):
        open_ = ps.classify("Pending", D(10), D(0))
        partial = ps.classify("B.Order", D(10), D(4))
        received = ps.classify("Complete", D(10), D(10))
        before, after = date(2026, 10, 20), date(2026, 10, 25)
        verdict = ps.lateness_verdict
        self.assertEqual(verdict(open_, before, None, self.TODAY), (ps.LATE, None))
        self.assertEqual(verdict(partial, before, after, self.TODAY), (ps.LATE, None))
        self.assertEqual(verdict(open_, after, date(2026, 10, 28), self.TODAY),
                         (ps.AT_RISK, None))
        self.assertEqual(verdict(open_, after, after, self.TODAY), (ps.NEITHER, None))
        self.assertEqual(verdict(open_, after, before, self.TODAY), (ps.NEITHER, None))
        self.assertEqual(verdict(open_, self.TODAY, None, self.TODAY), (ps.NEITHER, None))
        for done in (received, ps.classify("Cancel", D(1), D(0)),
                     ps.classify("DRAFT", D(1), D(0))):
            self.assertEqual(verdict(done, before, after, self.TODAY), (ps.NEITHER, None))
        self.assertEqual(verdict(open_, None, after, self.TODAY),
                         (ps.UNKNOWN, "no_requested_date"))
        self.assertEqual(verdict(ps.classify("Revised", D(1), D(0)), before, None, self.TODAY),
                         (ps.UNKNOWN, "status_unknown"))
        self.assertEqual(verdict(ps.sage(), before, None, self.TODAY),
                         (ps.UNKNOWN, "status_unknown"))


class BusinessDayAgreementTest(unittest.TestCase):
    """The Workbench's lateness day (po_status.business_today) and the oracle's own
    (evals/oracle/po_lateness.business_today, written apart) agree at every half hour
    across both 2026 daylight-saving changes, and neither is the UTC day in the ET
    evening."""

    def test_product_and_oracle_name_the_same_et_day(self):
        from evals.oracle import po_lateness as oracle

        utc = timezone.utc
        evenings = 0
        for start in (datetime(2026, 3, 7, tzinfo=utc), datetime(2026, 10, 31, tzinfo=utc)):
            for step in range(4 * 48):   # four days, every half hour
                moment = start + timedelta(minutes=30 * step)
                with self.subTest(moment=moment.isoformat()):
                    self.assertEqual(ps.business_today(moment), oracle.business_today(moment))
                    evenings += ps.business_today(moment) != moment.date()
        # ET is behind UTC by 5 h (EST) or 4 h (EDT): 10 or 8 half hours a UTC day differ.
        # Mar 7, 8 EST (the change is at 07:00Z), Mar 9, 10 EDT: 36; Oct 31, Nov 1 EDT
        # (the change is at 06:00Z), Nov 2, 3 EST: 36
        self.assertEqual(evenings, 72)
        # the oracle's hand-written US rule against zoneinfo, every hour of five years
        hour = datetime(2026, 1, 1, tzinfo=utc)
        while hour.year < 2031:
            if ps.business_today(hour) != oracle.business_today(hour):
                self.fail(f"the two ET days differ at {hour.isoformat()}")
            hour += timedelta(hours=1)
        self.assertEqual(ps.business_today(datetime(2026, 3, 8, 4, 59, tzinfo=utc)),
                         date(2026, 3, 7))
        self.assertEqual(ps.business_today(datetime(2026, 11, 1, 3, 59, tzinfo=utc)),
                         date(2026, 10, 31))


class NeedDateSourceTest(unittest.TestCase):
    """Which date is the requested-by date:
    FM datearrive / Sage exparrival govern, then Portal expected_date, then the BC header
    requestedReceiptDate; a supplier promise or a lower tier never replaces a higher one;
    a blank date is no date. Synthetic ranks and days; the live fill counts are in the
    decision, not here."""

    TODAY = date(2026, 10, 26)

    def test_the_highest_tier_holding_a_date_governs_and_cites_its_item(self):
        need = ps.need_date([(2, date(2026, 10, 1), "bc"), (1, date(2026, 11, 30), "portal"),
                             (0, date(2026, 10, 20), "sage")])
        self.assertEqual((need.lo, need.hi, need.governing), (date(2026, 10, 20),) * 2
                         + (("sage",),))
        self.assertEqual(need.others, ("portal", "bc"))
        open_ = ps.classify("Pending", D(10), D(0))
        # late on the governing (Sage/FM) date although Portal's later date has not passed
        self.assertEqual(ps.verdict(open_, need, None, self.TODAY), (ps.LATE, None))

    def test_a_lower_tier_decides_only_when_the_higher_tiers_hold_no_date(self):
        open_ = ps.classify("B.Order", D(10), D(2))
        need = ps.need_date([(0, None, "sage_blank"), (2, date(2026, 10, 1), "bc")])
        self.assertEqual((need.lo, need.governing, need.unparseable),
                         (date(2026, 10, 1), ("bc",), ("sage_blank",)))
        self.assertEqual(ps.verdict(open_, need, None, self.TODAY)[0], ps.LATE)

    def test_no_requested_date_is_unknown_and_the_promise_never_stands_in(self):
        open_ = ps.classify("Pending", D(10), D(0))
        for blank in (ps.need_date([]), ps.need_date([(0, None, "zero"), (1, None, "x")])):
            self.assertEqual(ps.verdict(open_, blank, date(2026, 12, 1), self.TODAY),
                             (ps.UNKNOWN, "no_requested_date"))
        self.assertIsNone(ps.day_of("0"))
        self.assertIsNone(ps.day_of("00000000"))

    def test_at_risk_needs_a_requested_date_and_a_later_promise(self):
        open_ = ps.classify("Pending", D(10), D(0))
        need = ps.need_date([(0, date(2026, 11, 3), "fm")])
        for promised, expected in ((date(2026, 11, 20), ps.AT_RISK),
                                   (date(2026, 11, 3), ps.NEITHER),
                                   (None, ps.NEITHER)):
            self.assertEqual(ps.verdict(open_, need, promised, self.TODAY)[0], expected)
        # the day itself has not passed: not late
        today = ps.need_date([(0, self.TODAY, "fm")])
        self.assertEqual(ps.verdict(open_, today, None, self.TODAY)[0], ps.NEITHER)

    def test_disagreeing_top_tier_dates_straddling_today_are_disputed(self):
        open_ = ps.classify("Pending", D(10), D(0))
        need = ps.need_date([(0, date(2026, 10, 20), "fm"), (0, date(2026, 11, 20), "sage")])
        self.assertEqual(ps.verdict(open_, need, None, self.TODAY),
                         (ps.UNKNOWN, "need_date_disputed"))


class _FakeSource:
    """A duck-typed purchase-order source: one Portal 'Pending' record with the given
    lines, header need dates and open-line dates."""

    def __init__(self, lines, header=(), line_dates=(), promised=()):
        self._lines, self._header = lines, list(header)
        self._line_dates, self._promised = list(line_dates), list(promised)

    def twins(self, oid):
        return [oid], False, []

    def values(self, oid):
        return {"status": ["Pending"]}, [], False

    def lines(self, oid):
        return self._lines, [], False, []

    def fm_lines(self, oid):
        return [], [], False, []

    def need_dates(self, oid, members):
        return self._header

    def line_dates(self, members):
        return self._line_dates

    def promises(self, oid, members):
        return self._promised


class ToolSourceFirmTwinTest(unittest.TestCase):
    """The plx tools as one principal calls them read a firm twin's open line date too (the
    packaging eval's reader): through ``plx.around`` at the ``corroborated`` floor, never
    its status; a listing the principal cannot read in full lends nothing."""

    TODAY = date(2026, 10, 26)

    def source(self, readable=True):
        seen = []

        def call(name, args):
            seen.append((name, args))
            if name == "plx.around":
                link, direction = args["path"][0]["link_type"], args["path"][0]["direction"]
                obj, floor = args["from"], args.get("tier_floor")
                ids = {("PurchaseOrder.same_record_as", "forward", "PurchaseOrder:p", "corroborated"):
                       ["PurchaseOrder:bc"],
                       ("PurchaseOrderLine.line_of", "reverse", "PurchaseOrder:p", None):
                       ["PurchaseOrderLine:pl"],
                       ("PurchaseOrderLine.line_of", "reverse", "PurchaseOrder:bc", None):
                       ["PurchaseOrderLine:bl"]}.get((link, direction, obj, floor), [])
                if not readable and link == "PurchaseOrder.same_record_as":
                    return {"objects": [], "gaps": [], "withheld": {"objects": 1}}
                return {"objects": [{"object_id": i} for i in ids],
                        "completeness": {"complete": True}}
            values = {"PurchaseOrder:p": {"status": "Pending"},
                      "PurchaseOrderLine:pl": {"ordered_quantity": ("10", "ea"),
                                               "received_quantity": ("0", "ea")},
                      "PurchaseOrderLine:bl": {"bc_expected_receipt_date": "2026-10-01",
                                               "ordered_quantity": ("10", "ea"),
                                               "received_quantity": ("0", "ea")}
                      }.get(args["object_id"], {})
            return {"values": [
                {"property": k, "value": v[0] if isinstance(v, tuple) else v,
                 "value_type": "quantity" if isinstance(v, tuple) else "text",
                 **({"unit": v[1]} if isinstance(v, tuple) else {})}
                for k, v in values.items() if k in args.get("properties", values)]}
        return ps.ToolSource(call)

    def test_the_twins_line_date_makes_the_portal_order_late_and_is_cited(self):
        po = ps.read(self.source(), "PurchaseOrder:p", self.TODAY)
        self.assertEqual(po.verdict, (ps.LATE, None))
        self.assertEqual(po.date_twins, ["PurchaseOrder:bc"])
        self.assertEqual(po.need.governing[0][0], "PurchaseOrderLine:bl")

    def test_a_listing_the_principal_cannot_read_lends_no_date(self):
        po = ps.read(self.source(readable=False), "PurchaseOrder:p", self.TODAY)
        self.assertEqual(po.verdict, (ps.UNKNOWN, "no_requested_date"))
        self.assertEqual(po.date_twins, [])


class LineNeedDateTest(unittest.TestCase):
    """The open lines' own expected date (BC expectedReceiptDate) is the lowest requested-by
    tier, as the lateness oracle (evals/oracle/po_lateness.py) reads it: used only where no
    header holds a date; an order is late once any open line is. Live shape 2026-10-26: 23
    open BC POs, 67 open lines, every one dated, the header requestedReceiptDate blank."""

    TODAY = date(2026, 10, 26)

    def read(self, **kw):
        lines = kw.pop("lines", [(D(10), D(0), "EA")])
        return ps.read(_FakeSource(lines, **kw), "PO", self.TODAY)

    def test_an_open_order_with_a_past_line_date_is_late_and_cites_that_line(self):
        po = self.read(line_dates=[(date(2026, 10, 1), "line-a"), (date(2026, 12, 1), "line-b")])
        self.assertEqual(po.verdict, (ps.LATE, None))
        self.assertEqual((po.need.lo, po.need.governing, po.need.others),
                         (date(2026, 10, 1), ("line-a",), ("line-b",)))

    def test_a_header_date_governs_before_any_line_date(self):
        po = self.read(header=[(0, date(2026, 12, 1), "hdr")],
                       line_dates=[(date(2026, 10, 1), "line-a")])
        self.assertEqual((po.verdict, po.need.governing), ((ps.NEITHER, None), ("hdr",)))

    def test_at_risk_when_the_promise_is_after_the_line_date(self):
        po = self.read(line_dates=[(date(2026, 11, 5), "line-a")],
                       promised=[(None, date(2026, 11, 20), "conf")])
        self.assertEqual(po.verdict, (ps.AT_RISK, None))
        on_time = self.read(line_dates=[(date(2026, 11, 5), "line-a")])
        self.assertEqual(on_time.verdict, (ps.NEITHER, None))

    def test_an_undated_open_line_blocks_not_late_but_not_a_past_date(self):
        undated_future = self.read(line_dates=[(date(2026, 12, 1), "a"), (None, "b")])
        self.assertEqual(undated_future.verdict, (ps.UNKNOWN, "no_requested_date"))
        self.assertEqual(undated_future.need.undated, ("b",))
        past = self.read(line_dates=[(date(2026, 10, 1), "a"), (None, "b")])
        self.assertEqual(past.verdict, (ps.LATE, None))
        none = self.read(line_dates=[])
        self.assertEqual(none.verdict, (ps.UNKNOWN, "no_requested_date"))

    def test_a_closed_status_ignores_line_dates(self):
        class Done(_FakeSource):
            def values(self, oid):
                return {"status": ["Cancel"]}, [], False
        po = ps.read(Done([(D(10), D(0), "EA")], line_dates=[(date(2026, 1, 1), "a")]),
                     "PO", self.TODAY)
        self.assertEqual(po.verdict, (ps.NEITHER, None))

    def test_line_open_is_by_quantity_and_fails_open_on_unreadable(self):
        self.assertTrue(ps.line_open("10", None))
        self.assertTrue(ps.line_open("10", "9.5"))
        self.assertFalse(ps.line_open("10", "10"))
        self.assertFalse(ps.line_open("0", None))
        self.assertTrue(ps.line_open("x", None))


def _scan(rows, complete=True):
    return {"status": "ok", "complete": complete, "rows": rows, "reasons": []}


class VocabularySummaryTest(unittest.TestCase):
    """``vocabulary`` counts every captured header by raw word and class, unknowns by
    word and reason, against the old rule (codes only), with no row lost or duplicated."""

    def world(self, copies=1):
        heads, lines = [], []
        spec = [("Cancel", [("5", "0", "ea")]), ("Complete", [("10", "10", "ea")]),
                ("complete", [("10", "4", "ea")]), ("Pending", [("5", None, "ea")]),
                ("B.Order", [("6", "2", "ea"), ("4", "0", "ea")]), ("DRAFT", []),
                ("Revised", [("1", "0", "ea")]), ("Generated", []), (None, []),
                ("Pending", [("1", "0", "ea"), ("1", "0", "kg")]), ("Pending", [])]
        for n in range(copies):
            for index, (raw, its) in enumerate(spec):
                ident = f"po-{n}-{index}"
                heads.append({"id": ident, "status": raw})
                lines += [{"purchase_order_id": ident, "quantity": q, "quantity_received": r,
                           "uom": u} for q, r, u in its]
        return heads, lines

    def test_counts_by_word_class_and_reason(self):
        heads, lines = self.world()
        fm = [{"status": w} for w in ("Cancel", "Complete", "DRAFT", None, "Revised")]
        sage = [{"iscomplete": 1, "lines": 3, "linescmpl": 3},
                {"iscomplete": 0, "lines": 3, "linescmpl": 1},
                {"iscomplete": 1, "lines": 2, "linescmpl": 1},
                {"iscomplete": None, "lines": None, "linescmpl": 0}]
        out = ps.vocabulary({"portal_po": _scan(heads), "portal_line": _scan(lines),
                             "fm_po": _scan(fm), "sage_po": _scan(sage)})
        self.assertEqual(out["words"]["portal_po"], {
            "B.Order": {"open": 1}, "Cancel": {"closed": 1}, "Complete": {"closed": 1},
            "DRAFT": {"draft": 1}, "Generated": {"unknown": 1}, "Pending": {
                "open": 1, "unknown": 2}, "Revised": {"unknown": 1}, "_null_": {"unknown": 1},
            "complete": {"closed": 1}})
        self.assertEqual(out["classes"]["portal_po"],
                         {"open": 2, "closed": 3, "draft": 1, "unknown": 5})
        # the Complete 10/4 is closed with its remainder cancelled; no Complete is open
        self.assertEqual((out["complete_open"], out["canceled_remainder"]),
                         (0, {"portal_po": 1}))
        self.assertEqual(out["unknown_words"]["portal_po"],
                         {"Generated": 1, "Pending": 2, "Revised": 1, "_null_": 1})
        self.assertEqual(out["unknown_reasons"]["portal_po"], {
            "nothing_ordered": 1, "quantities_unknown": 1, "status_missing": 1,
            "status_unmapped": 2})
        # before: header codes only; none of the real words was open or closed
        self.assertEqual(out["before_engine_rule"]["portal_po"],
                         {"open": 0, "closed": 0, "neither": 11})
        self.assertEqual(out["classes"]["fm_po"],
                         {"open": 0, "closed": 2, "draft": 1, "unknown": 2})
        self.assertEqual(out["unknown_reasons"]["fm_po"],
                         {"status_missing": 1, "status_unmapped": 1})
        self.assertEqual((out["unmapped_rows"], out["open_plus_closed"]), (0, 5))
        self.assertEqual(out["sage_iscomplete"], {
            "0": {"lines_open": 1}, "1": {"lines_complete": 1, "lines_open": 1},
            "_null_": {"unknown": 1}})

    def test_an_incomplete_line_scan_decides_no_quantity(self):
        heads, lines = self.world()
        out = ps.vocabulary({"portal_po": _scan(heads), "portal_line": _scan(lines, False)})
        classes = out["classes"]["portal_po"]
        self.assertEqual((classes["open"], classes["closed"]), (0, 3))   # Cancel, Completes
        self.assertEqual(out["relations"]["portal_line"]["complete"], False)
        self.assertEqual(out["relations"]["sage_po"]["status"], "not_scanned")

    def test_cardinality_no_row_lost_or_duplicated(self):
        for copies in (1, 10, 60):
            heads, lines = self.world(copies)
            out = ps.vocabulary({"portal_po": _scan(heads), "portal_line": _scan(lines)})
            self.assertEqual(sum(out["classes"]["portal_po"].values()), 11 * copies)
            self.assertEqual(out["classes"]["portal_po"],
                             {"open": 2 * copies, "closed": 3 * copies, "draft": copies,
                              "unknown": 5 * copies})
            self.assertEqual(out["unmapped_rows"], 0)

    def test_an_fm_mirrored_po_takes_received_from_the_qc_log_or_fails_closed(self):
        # gold quantity_received is ETL-seeded 0 for FM-mirrored POs (upstream
        # open-po-lines/route.ts:117-120, po-queries.ts:529-533,572-574 @5afa0a20): the FM
        # QC receiving log (sumqty, the noitemsqc stand-in) is the received quantity
        def run(receipts_raw, receipts_pack=(), items=None, complete=True, fields=None):
            heads = [{"id": "po-m", "status": "Pending",
                      "custom_fields": fields if fields is not None
                      else json.dumps({"fmGeneratedPoId": 77})}]
            lines = [{"purchase_order_id": "po-m", "quantity": "10", "quantity_received": "0",
                      "uom": "ea"}]
            scans = {"portal_po": _scan(heads), "portal_line": _scan(lines),
                     "fm_po": _scan([{"id": 77, "status": "Pending"}]),
                     "fm_item": _scan(items if items is not None
                                      else [{"id": 701, "idpogen": 77, "qty": "10"}], complete),
                     "fm_receipt_raw": _scan(list(receipts_raw)),
                     "fm_receipt_pack": _scan(list(receipts_pack))}
            out = ps.vocabulary(scans)
            return out["classes"]["portal_po"], out["fm_qc"], out["classes_before_fm_qc"]

        received = [{"idpogenitem": 701, "sumqty": "6"}, {"idpogenitem": 701, "sumqty": "4"}]
        classes, qc, before = run(received)
        self.assertEqual((classes["closed"], classes["open"]), (1, 0))
        self.assertEqual((before["open"], before["closed"]), (1, 0))   # the gold 0 alone
        self.assertEqual(qc, {"mirrored_zero_received": 1, "qc_received": 1, "qc_none": 0,
                              "unread": 0})
        self.assertEqual(run(received[:1])[0]["open"], 1)               # 6 of 10: partial
        self.assertEqual(run([])[0]["open"], 1)                         # no QC: the 0 stands
        self.assertEqual(run([], fields={})[1]["mirrored_zero_received"], 0)   # not mirrored
        # fail closed: unknown, never open or closed, on any part not proven
        for case, args in {
                "packaging row without sumqty": ([], [{"idpogenitem": 701}]),
                "null sumqty": ([{"idpogenitem": 701, "sumqty": None}],),
                "garbage sumqty": ([{"idpogenitem": 701, "sumqty": "ten"}],),
                "FM ordered differs": (received, (), [{"id": 701, "idpogen": 77, "qty": "9"}]),
                "FM qty column missing": (received, (), [{"id": 701, "idpogen": 77}]),
                "item scan incomplete": (received, (), None, False)}.items():
            classes, qc, _ = run(*args)
            self.assertEqual((classes["unknown"], qc["unread"]), (1, 1), case)

    def test_shown_words_are_enums_the_record_may_copy(self):
        self.assertEqual(ps._shown("On  Hold"), "On_Hold")
        self.assertEqual(ps._shown(""), "_blank_")
        self.assertEqual(ps._shown("x" * 90), "x" * ps.MAX_WORD)
        for shown in ("_null_", ps._shown("B.Order"), ps._shown("a b,c")):
            self.assertTrue(lv.ENUM_STRING.fullmatch(shown), shown)


class LiveCheckTest(unittest.TestCase):
    """live check ``po_status.vocabulary`` over injected sources: its own
    command over a FakeWorkbench holding the real words on synthetic POs, the network
    refused. Evidence label: injected-transport, never live."""

    SPEC_ID = "po_status.vocabulary"

    def setUp(self) -> None:
        import socket

        def refuse(*_args, **_kwargs):
            raise AssertionError("network access attempted in an offline test")

        for target in ("connect", "connect_ex"):
            patcher = mock.patch.object(socket.socket, target, refuse)
            patcher.start()
            self.addCleanup(patcher.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.spec = json.loads((ROOT / "devloop" / "live-checks" / f"{self.SPEC_ID}.json")
                               .read_text("utf-8"))

    def fake(self):
        from integrations.tests.test_source_mirror import FakeWorkbench
        from operating.tests import order_fixture as fixture

        fake = FakeWorkbench()
        fake.catalog = json.loads(json.dumps(fixture.CATALOG))
        fake.catalog["public.generated_purchase_order"]["columns"].insert(2, "status")
        fake.catalog["sage.po_header"] = {
            "columns": ["porhseq", "ponumber", "iscomplete", "lines", "linescmpl", "audtdate"],
            "key": ["porhseq"], "domain": "test",
            "since": {"column": "audtdate", "mode": "yyyymmdd-text"}}
        fake.tables = fixture.tables()
        words = [("Cancel", "5", "0"), ("Complete", "10", "10"), ("complete", "10", "4"),
                 ("Pending", "5", "0"), ("B.Order", "5", "2"), ("DRAFT", None, None),
                 ("Revised", "1", "0"), ("Generated", None, None), (None, None, None)]
        fake.tables["public.app_purchase_order"] = [
            fixture._po(f"po-v{index}", f"PO30000{index}", "sup-1", raw, "20")
            for index, (raw, _q, _r) in enumerate(words)]
        fake.tables["public.app_purchase_order_line"] = [
            fixture._row(f"pol-v{index}", purchase_order_id=f"po-v{index}", product_id="p-1",
                         quantity=q, quantity_received=r, uom="ea")
            for index, (_raw, q, r) in enumerate(words) if q is not None]
        fake.tables["public.generated_purchase_order"] = [
            fixture._fm(3300 + index, ponumber=f"PO30000{index}", status=raw)
            for index, (raw, _q, _r) in enumerate(words)]
        fake.tables["sage.po_header"] = [
            {"porhseq": 1, "ponumber": "PO300001", "iscomplete": 1, "lines": 2,
             "linescmpl": 2, "audtdate": "20260929"},
            {"porhseq": 2, "ponumber": "PO300002", "iscomplete": 0, "lines": 2,
             "linescmpl": 1, "audtdate": "20260929"}]
        return fake

    def run_spec(self, *, fetch=None, state="run"):
        from integrations.tests.test_source_mirror import BASE, KEY

        config = json.loads((ROOT / "examples" / "po_status_host.live-check.json")
                            .read_text("utf-8"))
        for key in ("capture_audience", "live_audience", "mirror_audience"):
            config[key] = str(ROOT / "examples" / config[key])
        (self.temp / "host.json").write_text(json.dumps(config), "utf-8")
        argv = [str(self.temp / "host.json") if arg.endswith("_host.live-check.json")
                else str(ROOT / arg) if arg.startswith("examples/")
                else arg.replace("{tmp}", str(self.temp / state))
                for arg in self.spec["command"][3:]]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ps.main(argv, environ={"PLX_WORKBENCH_API_URL": BASE,
                                          "PLX_WORKBENCH_API_KEY": KEY},
                           host_options={"workbench_fetch": fetch or self.fake(),
                                         "sleep": lambda _s: None})
        return code, json.loads(out.getvalue()), out.getvalue()

    def test_the_spec_passes_on_the_real_words_and_reads_blocked_on_a_portal_outage(self):
        lv.validate_spec(self.spec, stem=self.SPEC_ID)
        self.assertEqual(self.spec["command"][:3], ["python3", "-m", "operating.po_status"])
        code, summary, text = self.run_spec()
        self.assertEqual(lv.check_expectations(self.spec, code, summary), [], summary)
        self.assertEqual(summary["classes"]["portal_po"],
                         {"open": 2, "closed": 3, "draft": 1, "unknown": 3})
        self.assertEqual(summary["complete_open"], 0)
        self.assertEqual(summary["unknown_words"]["portal_po"],
                         {"Generated": 1, "Revised": 1, "_null_": 1})
        self.assertEqual(summary["classes"]["fm_po"],
                         {"open": 0, "closed": 3, "draft": 1, "unknown": 5})
        self.assertEqual(summary["sage_iscomplete"],
                         {"0": {"lines_open": 1}, "1": {"lines_complete": 1}})
        self.assertNotIn("PO30000", text)   # counts and enums only
        self.assertNotIn("po-v", text)
        recorded = lv._recorded(self.spec, summary)
        self.assertEqual(recorded["words.portal_po"]["B.Order"], {"open": 1})
        self.assertEqual(recorded["sage_iscomplete"]["1"], {"lines_complete": 1})

        def portal_down(url, headers, *, max_bytes):
            raise SourceHttpError("HTTP 500", status=500, error_code="internal")

        # down after a capture (the mirror's copy is read) and down from the start
        for state in ("run", "down"):
            code, summary, _text = self.run_spec(fetch=portal_down, state=state)
            self.assertTrue(lv.blocked_parts(summary), state)
            self.assertEqual(lv.check_expectations(
                self.spec, code, summary, self.spec["expect_blocked"]), [], (state, summary))
        self.assertNotEqual(lv.check_expectations(self.spec, code, summary), [])

    def test_an_fm_mirrored_po_received_in_the_qc_log_is_closed_live_shaped(self):
        from operating.tests import order_fixture as fixture

        fake = self.fake()
        fake.catalog["public.generated_purchase_order_item"]["columns"].insert(2, "qty")
        fake.tables["public.app_purchase_order"].append(fixture._po(
            "po-m", "PO300099", "sup-1", "Pending", "20", {"fmGeneratedPoId": 3399}))
        fake.tables["public.app_purchase_order_line"].append(fixture._row(
            "pol-m", purchase_order_id="po-m", product_id="p-1", quantity="10",
            quantity_received="0", uom="ea"))
        fake.tables["public.generated_purchase_order"].append(
            fixture._fm(3399, ponumber="PO300099", status="Pending"))
        fake.tables["public.generated_purchase_order_item"] = [
            fixture._fm(3499, idpogen=3399, qty="10")]
        fake.tables["public.receipt_packaging"] = []
        fake.tables["public.receipt_raw_material"] = [
            fixture._fm(3599, idpogenitem=3499, idreceipt=78, sumqty="10")]
        code, summary, _text = self.run_spec(fetch=fake)
        self.assertEqual(lv.check_expectations(self.spec, code, summary), [], summary)
        self.assertEqual(summary["fm_qc"]["qc_received"], 1, summary["fm_qc"])
        self.assertEqual(summary["classes"]["portal_po"]["closed"], 4)
        self.assertEqual(summary["classes_before_fm_qc"]["closed"], 3)
        self.assertEqual(summary["complete_open"], 0)
        # a packaging receipt against the item: the served catalog lacks sumqty, unread
        fake.tables["public.receipt_packaging"] = [
            fixture._fm(3598, idpogenitem=3499, idreceipt=79)]
        code, summary, _text = self.run_spec(fetch=fake, state="pack")
        self.assertEqual(summary["fm_qc"]["unread"], 1, summary["fm_qc"])
        self.assertEqual(summary["classes"]["portal_po"]["unknown"], 4)

    def test_a_new_word_is_counted_by_name_and_the_check_still_passes(self):
        fake = self.fake()
        fake.tables["public.app_purchase_order"].append(
            {**fake.tables["public.app_purchase_order"][0], "id": "po-new", "status": "On Hold"})
        code, summary, _text = self.run_spec(fetch=fake)
        self.assertEqual(lv.check_expectations(self.spec, code, summary), [])
        self.assertEqual(summary["unknown_words"]["portal_po"]["On_Hold"], 1)
        self.assertEqual(summary["unknown_reasons"]["portal_po"]["status_unmapped"], 3)

    def test_nothing_open_or_closed_fails(self):
        fake = self.fake()
        fake.tables["public.app_purchase_order"] = [
            row for row in fake.tables["public.app_purchase_order"]
            if row["status"] in ("DRAFT", "Revised", None)]
        code, summary, _text = self.run_spec(fetch=fake)
        self.assertEqual(summary["open_plus_closed"], 0)
        self.assertTrue(lv.check_expectations(self.spec, code, summary))


if __name__ == "__main__":
    unittest.main()
