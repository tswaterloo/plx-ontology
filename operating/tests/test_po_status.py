"""The one purchase-order status definition (operating/po_status.py) and its
``po_status.vocabulary`` live check.

Synthetic records only, with the real status words of the Portal purchase-order headers
(live capture 2026-10-20: Cancel, Complete, DRAFT, Pending, B.Order, Revised, Generated)
in mixed case. The live check runs over an injected transport (evidence label:
injected-transport), never presented as live.
"""

from __future__ import annotations

import contextlib
from datetime import date
from decimal import Decimal
import io
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from verify import live_verify as lv
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

    def test_fm_complete_with_an_outstanding_quantity_is_open(self):
        # FileMaker's Complete means issued; only the quantities close a PO.
        self.assertEqual(ps.classify("Complete", D(10), D(4)), ps.PoStatus(ps.OPEN, "PARTIAL"))
        self.assertEqual(ps.classify("Complete", D(10), D(0)),
                         ps.PoStatus(ps.OPEN, "SUBMITTED"))
        # within EPS of the ordered quantity is received in full, as the Portal reads it
        self.assertEqual(ps.classify("Complete", D(10), D("9.99995")).cls, ps.CLOSED)
        self.assertEqual(ps.classify("Complete", D(10), D("9.999")).cls, ps.OPEN)
        # the FM twin, whose header carries no received quantity, decides nothing
        fm = ps.classify("Complete", None, None)
        self.assertEqual(fm, ps.PoStatus(ps.UNKNOWN, reason="quantities_unknown"))
        self.assertEqual(ps.combine([ps.classify("Complete", D(10), D(4)), fm]).cls, ps.OPEN)

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
        self.assertEqual(ps.record({"status": ["Complete"]}, partial),
                         ps.PoStatus(ps.OPEN, "PARTIAL"))
        self.assertEqual(ps.record({"status": ["Complete"]}, [("10", "10", "ea")]).cls,
                         ps.CLOSED)
        self.assertEqual(ps.record({"status": ["Complete"]}, None).reason,
                         "quantities_unknown")             # its lines not read
        self.assertEqual(ps.record({"status": ["Complete"]}, []).reason, "nothing_ordered")
        # the FM header alone: issued words unknown, even with lines (no received there)
        self.assertEqual(ps.record({"fm_status": ["Complete"]}, partial).reason,
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
        words = sorted(ps.CANCELLED_WORDS | ps.DRAFT_WORDS | ps.ISSUED_WORDS)
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


class LatenessVerdictTest(unittest.TestCase):
    """Vince's rule: late once the requested-by date has passed; at risk while it has not
    but the supplier confirmed later; a received PO is neither; no requested-by date is
    reported, never filled from the supplier's date."""

    TODAY = date(2026, 10, 21)

    def test_late_at_risk_and_neither(self):
        open_ = ps.classify("Pending", D(10), D(0))
        partial = ps.classify("Complete", D(10), D(4))
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
            "complete": {"open": 1}})
        self.assertEqual(out["classes"]["portal_po"],
                         {"open": 3, "closed": 2, "draft": 1, "unknown": 5})
        self.assertEqual(out["unknown_words"]["portal_po"],
                         {"Generated": 1, "Pending": 2, "Revised": 1, "_null_": 1})
        self.assertEqual(out["unknown_reasons"]["portal_po"], {
            "nothing_ordered": 1, "quantities_unknown": 1, "status_missing": 1,
            "status_unmapped": 2})
        # before: header codes only; none of the real words was open or closed
        self.assertEqual(out["before_engine_rule"]["portal_po"],
                         {"open": 0, "closed": 0, "neither": 11})
        self.assertEqual(out["classes"]["fm_po"],
                         {"open": 0, "closed": 1, "draft": 1, "unknown": 3})
        self.assertEqual(out["unknown_reasons"]["fm_po"],
                         {"quantities_unknown": 1, "status_missing": 1, "status_unmapped": 1})
        self.assertEqual((out["unmapped_rows"], out["open_plus_closed"]), (0, 5))
        self.assertEqual(out["sage_iscomplete"], {
            "0": {"lines_open": 1}, "1": {"lines_complete": 1, "lines_open": 1},
            "_null_": {"unknown": 1}})

    def test_an_incomplete_line_scan_decides_no_quantity(self):
        heads, lines = self.world()
        out = ps.vocabulary({"portal_po": _scan(heads), "portal_line": _scan(lines, False)})
        classes = out["classes"]["portal_po"]
        self.assertEqual((classes["open"], classes["closed"]), (0, 1))   # Cancel only
        self.assertEqual(out["relations"]["portal_line"]["complete"], False)
        self.assertEqual(out["relations"]["sage_po"]["status"], "not_scanned")

    def test_cardinality_no_row_lost_or_duplicated(self):
        for copies in (1, 10, 60):
            heads, lines = self.world(copies)
            out = ps.vocabulary({"portal_po": _scan(heads), "portal_line": _scan(lines)})
            self.assertEqual(sum(out["classes"]["portal_po"].values()), 11 * copies)
            self.assertEqual(out["classes"]["portal_po"],
                             {"open": 3 * copies, "closed": 2 * copies, "draft": copies,
                              "unknown": 5 * copies})
            self.assertEqual(out["unmapped_rows"], 0)

    def test_shown_words_are_enums_the_record_may_copy(self):
        self.assertEqual(ps._shown("On  Hold"), "On_Hold")
        self.assertEqual(ps._shown(""), "_blank_")
        self.assertEqual(ps._shown("x" * 90), "x" * ps.MAX_WORD)
        for shown in ("_null_", ps._shown("B.Order"), ps._shown("a b,c")):
            self.assertTrue(lv.ENUM_STRING.fullmatch(shown), shown)


class LiveCheckTest(unittest.TestCase):
    """``verify/live-checks/po_status.vocabulary.json`` over injected sources: its own
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
        self.spec = json.loads((ROOT / "verify" / "live-checks" / f"{self.SPEC_ID}.json")
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
                         {"open": 3, "closed": 2, "draft": 1, "unknown": 3})
        self.assertEqual(summary["unknown_words"]["portal_po"],
                         {"Generated": 1, "Revised": 1, "_null_": 1})
        self.assertEqual(summary["classes"]["fm_po"],
                         {"open": 0, "closed": 1, "draft": 1, "unknown": 7})
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
