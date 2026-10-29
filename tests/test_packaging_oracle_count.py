"""The oracle consumes the real producer's integer line count, and resolves the requested
purchase order the way the served relations do (injected relation reads, no network)."""
import unittest

from scripts import eval_packaging_oracle_check as oracle


class PackagingOracleCountTest(unittest.TestCase):
    def case(self, lines):
        return {
            "root": {"relation": "public.app_purchase_order", "id": "synthetic-root"},
            "lines": lines,
            "checks": {"po_cited": True, "lines_cited": True},
        }

    def reading(self, lines=4, requested=True, found=1):
        return {
            "status": "read", "roots": 1, "requested": found,
            "requested_is_root": requested, "lines": lines, "subtotal": None,
        }

    def test_integer_producer_count_is_compared_and_reported(self):
        case = self.case(4)
        result = oracle.compare(case, 0, self.reading())
        self.assertEqual((result["status"], result["case_lines"]), ("agree", 4))
        self.assertTrue(result["lines_agree"])
        report = oracle.report(case, 0, result, "isolated-fixture")
        self.assertEqual(report["case"]["lines"], 4)

    def test_legacy_list_shape_stays_supported(self):
        case = self.case([{}, {}, {}, {}])
        result = oracle.compare(case, 0, self.reading())
        self.assertEqual((result["status"], result["case_lines"]), ("agree", 4))
        self.assertEqual(oracle.report(case, 0, result, "isolated-fixture")["case"]["lines"], 4)

    def test_wrong_counts_do_not_trust_the_case_flags(self):
        for lines in (3, 5, [{}, {}, {}], [{}, {}, {}, {}, {}]):
            with self.subTest(lines=lines):
                result = oracle.compare(self.case(lines), 0, self.reading())
                self.assertEqual(result["status"], "disagree")
                self.assertFalse(result["lines_agree"])
                self.assertIn("line_count_differs", result["reasons"])

    def test_explicit_zero_is_a_valid_count(self):
        case = self.case(0)
        result = oracle.compare(case, 0, self.reading(lines=0))
        self.assertEqual((result["status"], result["case_lines"]), ("agree", 0))
        self.assertEqual(oracle.report(case, 0, result, "isolated-fixture")["case"]["lines"], 0)

    def test_invalid_counts_cannot_agree_with_an_empty_oracle(self):
        for lines in (None, True, False, -1, "0", 0.0, {}, ()):
            with self.subTest(lines=lines):
                case = self.case(lines)
                result = oracle.compare(case, 0, self.reading(lines=0))
                self.assertEqual(result["status"], "disagree")
                self.assertFalse(result["lines_agree"])
                self.assertIsNone(result["case_lines"])
                self.assertIsNone(oracle.report(case, 0, result, "isolated-fixture")["case"]["lines"])

    def test_missing_count_is_unknown_not_zero(self):
        case = self.case(0)
        del case["lines"]
        result = oracle.compare(case, 0, self.reading(lines=0))
        self.assertEqual(result["status"], "disagree")
        self.assertIsNone(result["case_lines"])

    def test_wrong_root_stays_a_failure(self):
        for lines in (4, [{}, {}, {}, {}]):
            with self.subTest(lines=lines):
                result = oracle.compare(self.case(lines), 0, self.reading(requested=False))
                self.assertEqual(result["status"], "disagree")
                self.assertFalse(result["root_agrees"])
                self.assertFalse(result["lines_agree"])
                self.assertIn("root_not_requested_po", result["reasons"])

    def test_failed_case_stays_a_failure(self):
        self.assertEqual(oracle.compare(self.case(4), 1, self.reading())["status"], "case_failed")
        self.assertEqual(oracle.compare(None, 0, self.reading())["status"], "case_failed")


PO, PO_LINE = "public.app_purchase_order", "public.app_purchase_order_line"
FM_PO = "public.generated_purchase_order"


class PackagingOracleRootTest(unittest.TestCase):
    """``oracle_reading`` + ``compare`` over injected served relations: the requested number
    reaches the case's root in the normal form ``plx.find`` searches by, directly or over
    the FM link; a number no served row carries is a typed gap."""

    def run_check(self, gold, fm, number, root_id="gold-a", case_lines=2):
        tables = {PO: gold, FM_PO: fm,
                  PO_LINE: [{"id": f"line-{i}", "purchase_order_id": "gold-a"}
                            for i in range(2)]}
        reads = []

        def read_relation(relation, columns):
            reads.append(relation)
            return {"rows": [dict(r) for r in tables.get(relation, [])], "key": ["id"]}

        reading = oracle.oracle_reading(read_relation, PO, root_id, number)
        case = {"root": {"relation": PO, "id": root_id}, "lines": case_lines}
        return reading, oracle.compare(case, 0, reading), reads

    def test_root_reached_over_the_fm_link_with_a_case_different_number_agrees(self):
        gold = [{"id": "gold-a", "po_number": "BC-0001",
                 "custom_fields": {"fmGeneratedPoId": 71}},
                {"id": "gold-b", "po_number": "BC-0002", "custom_fields": {}}]
        fm = [{"id": 71, "ponumber": " po-71 "}, {"id": 72, "ponumber": "PO72"}]
        reading, check, _ = self.run_check(gold, fm, "PO71")
        self.assertEqual((reading["requested_via"], reading["requested"]), ("link", 1))
        self.assertEqual((check["status"], check["root_agrees"], check["lines_agree"]),
                         ("agree", True, True))
        # The same link reaching another gold row is a disagreement, not a gap.
        _, check, _ = self.run_check(gold, fm, "po71", root_id="gold-b")
        self.assertEqual((check["status"], check["root_agrees"], check["reasons"]),
                         ("disagree", False, ["root_not_requested_po"]))

    def test_a_direct_document_name_wins_and_is_case_insensitive(self):
        gold = [{"id": "gold-a", "po_number": "po9", "custom_fields": {}},
                {"id": "gold-b", "po_number": "BC-2", "custom_fields": {"fmGeneratedPoId": 9}}]
        reading, check, reads = self.run_check(gold, [{"id": 9, "ponumber": "PO9"}], "PO9")
        self.assertEqual((reading["requested_via"], check["status"]), ("number", "agree"))
        self.assertNotIn(FM_PO, reads)

    def test_a_spelling_plx_find_normalises_is_the_same_row_never_a_gap(self):
        """The fixture's own spelling PO#5447 is the row
        ``plx.find`` resolves PO5447 to (``ontology.identifiers.normalize``), so the oracle
        reaches a verdict on it: agree on that row, disagree on any other, never a gap."""
        from ontology import identifiers

        self.assertEqual(identifiers.normalize("petra_po", "PO#5447"),
                         identifiers.normalize("petra_po", "PO5447"))
        gold = [{"id": "gold-a", "po_number": "PO#5447", "custom_fields": {}},
                {"id": "gold-b", "po_number": "PO5448", "custom_fields": {}}]
        reading, check, _ = self.run_check(gold, [], "PO5447")
        self.assertEqual((reading["requested_via"], check["status"]), ("number", "agree"))
        _, check, _ = self.run_check(gold, [], "PO5447", root_id="gold-b")
        self.assertEqual((check["status"], check["reasons"]),
                         ("disagree", ["root_not_requested_po"]))
        # A number with no normal form is never looked up literally: undecided, never a gap.
        _, check, _ = self.run_check(gold, [], "not a po")
        self.assertEqual((check["status"], check["reasons"]),
                         ("oracle_undecided", [oracle.NOT_NORMALIZABLE]))

    def test_a_number_no_served_row_carries_is_a_typed_gap(self):
        from operating.cases import GAP_STATES

        gold = [{"id": "gold-a", "po_number": "PO#72",
                 "custom_fields": {"fmGeneratedPoId": 71}}]
        for fm in ([{"id": 71, "ponumber": "71"}], []):
            with self.subTest(fm=fm):
                reading, check, _ = self.run_check(gold, fm, "PO71")
                self.assertEqual(reading["requested"], 0)
                self.assertEqual(check["status"], "oracle_gap")
                self.assertIsNone(check["root_agrees"])
                self.assertEqual(check["gap"], {"state": "absent_in_source",
                                                "reason": oracle.NO_PORTAL_ROW})
                self.assertEqual(check["reasons"], [oracle.NO_PORTAL_ROW])
                self.assertTrue(check["lines_agree"])
                self.assertIn(check["gap"]["state"], GAP_STATES)
        # A gap never masks a real mismatch: lines that differ beside it disagree, and the
        # report names no gap (the spec's blocked_when cannot fire).
        for case_lines in (3, None):
            with self.subTest(case_lines=case_lines):
                _, check, _ = self.run_check(gold, [], "PO71", case_lines=case_lines)
                self.assertEqual((check["status"], check["lines_agree"], check["reasons"]),
                                 ("disagree", False,
                                  [oracle.NO_PORTAL_ROW, "line_count_differs"]))
                report = oracle.report({"root": {"relation": PO, "id": "gold-a"},
                                        "lines": case_lines}, 0, check, "isolated-fixture")
                self.assertIsNone(report["oracle_gap"])
        # A gap is never a pass, and a case root the oracle cannot find stays a failure.
        _, check, _ = self.run_check(gold, [], "PO71", root_id="gold-x")
        self.assertEqual((check["status"], check["root_agrees"]), ("disagree", False))
        self.assertIn("root_not_found", check["reasons"])

    def test_a_served_fm_row_with_no_link_to_the_root_relation_is_its_own_gap(self):
        gold = [{"id": "gold-a", "po_number": "BC-1",
                 "custom_fields": {"fmGeneratedPoId": 71}}]
        reading, check, _ = self.run_check(gold, [{"id": 80, "ponumber": "po71"}], "PO71")
        self.assertEqual((reading["requested"], reading["unlinked_carriers"]), (0, 1))
        self.assertEqual((check["status"], check["root_agrees"], check["reasons"]),
                         ("oracle_gap", None, [oracle.SERVED_UNLINKED]))
        # The unconfirmed link is an unknown in the case's one gap vocabulary.
        self.assertEqual(check["gap"], {"state": "unknown", "reason": oracle.SERVED_UNLINKED})
        report = oracle.report({"root": {"relation": PO, "id": "gold-a"}, "lines": 2}, 0,
                               check, "isolated-fixture")
        self.assertEqual(report["oracle_gap"], oracle.SERVED_UNLINKED)
        # Agreement and disagreement name no gap.
        _, check, _ = self.run_check(gold, [{"id": 71, "ponumber": "po71"}], "PO71")
        self.assertEqual(check["status"], "agree")
        self.assertIsNone(oracle.report({}, 0, check, "isolated-fixture")["oracle_gap"])

    def test_an_unreadable_link_relation_blocks_instead_of_gapping(self):
        def read_relation(relation, columns):
            if relation == FM_PO:
                raise TimeoutError("down")
            return {"rows": [{"id": "gold-a", "po_number": "BC-1", "custom_fields": {}}]}

        reading = oracle.oracle_reading(read_relation, PO, "gold-a", "PO71")
        check = oracle.compare({"root": {"relation": PO, "id": "gold-a"}, "lines": 0}, 0,
                               reading)
        self.assertEqual(check["status"], "oracle_blocked")


if __name__ == "__main__":
    unittest.main()


class OneReaderAcrossCallersTest(unittest.TestCase):
    """One store read by the three callers of ``po_status.read``: ``plx.process``
    (process_lateness over ``ContextSource``), the proactive engine's lateness rule
    (``StoreSource`` over the saved store) and this oracle (``ToolSource`` over the plx
    tools as one principal calls them) give each PO the same status and verdict: a Portal
    Complete PO with a fully received BC twin (closed, neither), a late PO and an at-risk
    PO. A twin link hidden from a restricted reader leaves that reader blind in every
    caller. Synthetic records over the real schema; evidence label: isolated-fixture."""

    TODAY = "2099-02-01"   # tests.test_ontology_tools_process.NOW

    @classmethod
    def setUpClass(cls):
        from ontology.tests import test_po_lateness as pl

        pl.setUpModule()
        cls.pl = pl

    def po(self, s, key, status, need, ordered=("10", "0"), promise=None):
        po = self.pl.portal_po(s, key=key, status=status, number=f"PO-{key}", ordered=ordered)
        s.val(po, "expected_date", "date", need)
        s.event("PurchaseOrder.expected", po, need)
        if promise:
            sc = self.pl.confirm(s, po, f"{key}-sc", promise, "2099-01-02")
            s.val(sc, "delivery_date", "date", promise)
        return po

    def world(self, n=1, hidden="[]"):
        from tests import test_ontology_tools_process as tp

        s = tp.Store()
        expected = {}
        for i in range(n):
            done = self.po(s, f"done-{i}", "Complete", "2099-01-15")
            twin = self.pl.bc_po(s, key=f"bc-{i}", fully_received="true", status="Open")
            s.link("PurchaseOrder.same_record_as", done, twin, tier="verified",
                   kind="same_record")
            # plx.process measures a received PO on its receipt date: on time, neither
            self.pl.receipt(s, done, f"rc-{i}", "2099-01-10")
            late = self.po(s, f"late-{i}", "Pending", "2099-01-20", promise="2099-01-25")
            risky = self.po(s, f"risk-{i}", "Pending", "2099-02-10", promise="2099-02-20")
            # a BC order dated only by its open line's expectedReceiptDate, and the Portal
            # order its number matches capped at lead (bc_company_not_known), as live:
            # the lead is no twin, so its later header date never governs in any caller
            bc_late = self.pl.bc_po(s, key=f"bc-late-{i}", fully_received="false")
            self.pl.line(s, bc_late, f"bc-late-{i}-1", "100", "0",
                         binding="bc.purchase_order_lines", due="2099-01-12")
            lead = self.po(s, f"lead-{i}", "Complete", "2099-03-01", ordered=("10", "10"))
            s.link("PurchaseOrder.same_record_as", lead, bc_late, tier="lead",
                   kind="same_record")
            expected.update({done: ("closed", "RECEIVED", "neither"),
                             late: ("open", "SUBMITTED", "late"),
                             risky: ("open", "SUBMITTED", "at_risk"),
                             bc_late: ("open", None, "late")})
        if hidden != "[]":
            s.conn.execute("UPDATE links SET markings = ? WHERE link_type = ?",
                           (hidden, "PurchaseOrder.same_record_as"))
        return s, expected

    def process(self, s, oid, actor):
        from datetime import date

        out = self.pl.tools_at(s).process({"from": oid, "process": self.pl.PROCESS},
                                          actor).result
        (entry,) = out["lateness"]
        state = entry["state"]
        verdict = ("late" if state == "late" else
                   "at_risk" if entry.get("at_risk") else
                   "neither" if state in ("not_applicable", "not_due", "on_time") else
                   "unknown")
        return verdict, date.fromisoformat(self.TODAY)

    def engine(self, s, path):
        from datetime import date
        import sqlite3

        from operating import proactive_engine as pe

        target = sqlite3.connect(path)
        s.conn.commit()
        s.conn.backup(target)
        target.close()
        st = pe._Store(path)
        self.addCleanup(st.close)
        result = pe._late_or_unconfirmed_po(st, date.fromisoformat(self.TODAY))
        verdicts = {}
        for gap in result["gaps"]:
            # an unconfirmed open PO past its need date is late too (its days late stated)
            late = gap["kind"] == "late_po" or (gap["kind"] == "unconfirmed_po" and (
                gap.get("follow_up") or {}).get("days_late") is not None)
            verdicts[gap["object_id"]] = ("at_risk" if "after the need date" in gap["summary"]
                                          else "late" if late else gap["kind"])
        return st, result, verdicts

    def oracle_call(self, s, actor):
        from operating import ontology_tools as ot
        from operating import ontology_tools_traverse as tt

        base = self.pl.tools_at(s).base if hasattr(self.pl.tools_at(s), "base") else None
        base = base or ot.OntologyTools(ot.StoreReader(s.conn), self.pl.tp.POLICY,
                                        schema=self.pl.tp.SCHEMA, contracts=self.pl.CONTRACTS,
                                        clock=lambda: self.pl.tp.NOW)
        table: dict = {}
        ot.register(table, base)
        tt.register(table, tt.TraverseTools(base))
        return lambda name, args: table[name]["handler"](args, actor)

    def test_three_callers_one_status_and_verdict(self):
        import tempfile
        from datetime import date
        from pathlib import Path
        import time

        from operating import po_status

        today = date.fromisoformat(self.TODAY)
        for n in (1, 10, 60):
            with self.subTest(n=n), tempfile.TemporaryDirectory() as tmp:
                started = time.monotonic()
                s, expected = self.world(n)
                st, result, gaps = self.engine(s, Path(tmp) / "store.sqlite")
                src = po_status.StoreSource(st)
                call = self.oracle_call(s, self.pl.STAFF)
                self.assertEqual(result["counts"]["classes"]["closed"], 2 * n)
                self.assertEqual(result["counts"]["classes"]["open"], 3 * n)
                self.assertEqual((result["counts"]["late"], result["counts"]["at_risk"]),
                                 (2 * n, n))
                for oid, (cls, code, verdict) in expected.items():
                    engine = po_status.read(src, oid, today)
                    tools = oracle.po_read(call, oid, today)
                    for read in (engine, tools):
                        self.assertEqual((read.status.cls, read.status.code, read.verdict[0]),
                                         (cls, code, verdict), (oid, read))
                        self.assertFalse(read.blind, oid)
                    self.assertEqual(self.process(s, oid, self.pl.STAFF)[0], verdict, oid)
                    self.assertEqual(gaps.get(oid, "neither"), verdict, oid)
                self.assertLess(time.monotonic() - started, 60, n)

    def test_a_hidden_twin_link_leaves_the_restricted_reader_blind_in_every_caller(self):
        import json

        s, expected = self.world()
        late = next(oid for oid, want in expected.items() if want[2] == "late")
        # the FM copy of the late Portal PO: its header alone has no quantity, the Portal
        # twin's lines decide; the identity link carries a marking the sales reader lacks
        fm = s.obj("PurchaseOrder:portal.fm_generated_purchase_order:fm-late", "fm_bronze")
        s.val(fm, "fm_status", "text", "Pending")
        s.link("PurchaseOrder.same_record_as", late, fm, tier="verified", kind="same_record")
        s.conn.execute("UPDATE links SET markings = ? WHERE to_object = ?", ('["finance"]', fm))
        # the staff reader reads the FM root with its Portal twin: open and late
        read = oracle.po_read(self.oracle_call(s, self.pl.STAFF), fm, self.today())
        self.assertEqual((read.status.cls, read.verdict[0], read.blind), ("open", "late", False))
        self.assertEqual(set(read.members), {fm, late})
        self.assertEqual(self.process(s, fm, self.pl.STAFF)[0], "late")
        # the restricted reader may not read the identity link: blind, never decided from
        # the part it can read, and the twin is not named to it
        read = oracle.po_read(self.oracle_call(s, self.pl.SALES), fm, self.today())
        self.assertEqual((read.status.cls, read.verdict, read.blind),
                         ("unknown", ("unknown", "status_unknown"), True))
        self.assertEqual(read.members, [fm])
        out = self.pl.tools_at(s).process({"from": fm, "process": self.pl.PROCESS},
                                          self.pl.SALES).result
        (entry,) = out["lateness"]
        self.assertEqual((entry["state"], entry["late"], entry["gap"]["reason"]),
                         ("undetermined", None, "status_unknown"))
        self.assertNotIn("late-0", json.dumps(out))
        self.assertNotIn("Pending", json.dumps(out))

    def today(self):
        from datetime import date

        return date.fromisoformat(self.TODAY)
