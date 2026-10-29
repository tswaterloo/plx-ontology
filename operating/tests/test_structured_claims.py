"""Structured answer claims (ROADMAP B1 ``structured-answer-claims``): the planner states
each value as a claim (value, citation handle, field, its id) and writes short prose naming
claims ``{id}``; ``operating.answer_gate`` checks each claim by reference against what the
session's tools returned, never by reading the prose; code renders the answer's text from
the checked claims only; a value resting on a lead-tier link passes only marked a lead,
and a claim shows the cell as recorded when the source wrote it otherwise.

Evidence label: ``isolated-fixture`` (the fixture world's bench cards), agent
``injected-transport``: the planner is scripted, never a model. Every run goes the way the
Ask bench runs a card (``scripts.eval_ask_bench``: the object workspace's Ask over the
reader's session, step 0 the card and its family's plan), so the text checked is the one
the workspace renders and the eval scores.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from operating import answer_gate, claim_checks
from scripts import eval_ask_bench as bench

QUANTITY_CARD = "dev-F3-0001-fixture"   # its plan returns the order line's quantity
LEAD_CARD = "dev-F2-0004-fixture"       # its plan reaches the order through a lead link
NEVER_RETURNED = "api://public.app_sales_order_line/%7B%22id%22%3A%22fx-nope%22%7D@0-9"


def _entries(request):
    """The plan's answer entries step 0 returned (``plx.investigate``), as shown."""
    step = request["transcript"][0]
    (plan,) = [o for o in step["observations"] if o["tool"] == "plx.investigate"]
    result = plan["result"]
    return {entry["name"]: entry for entry in result.get("shown", result)["answer"]}


def _cell(entry):
    return {"citation": entry["citation"]["l1_handle"], "field": entry["citation"]["field_path"]}


def _plain(value):
    return value["untrusted_text"] if isinstance(value, dict) else value


class StructuredClaimTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="plx-structured-claims-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def ask(self, card, answers):
        """One Ask session on ``card``; ``answers(request, entries)`` gives each answer
        (the planner's requests are kept). The session's transcript record, the answer
        the workspace showed (``plx-eval-answer/v1``) and the requests."""
        requests = []

        def planner(request):
            requests.append(request)
            return {"action": "answer", "unknowns": [],
                    **answers(request, _entries(requests[0]))}, None
        work = self.root / f"work-{len(list(self.root.iterdir()))}"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = bench.main(["--work-dir", str(work), "--only", card], planner=planner)
        self.assertEqual(code, 0, err.getvalue())
        (path,) = (work / "runs").rglob("*.transcript.json")
        transcript = json.loads(path.read_text(encoding="utf-8"))
        final = transcript["final_text"].strip().removeprefix("```json").removesuffix("```")
        return transcript["agent"], json.loads(final), requests

    def test_a_freely_worded_answer_passes_and_shows_the_cell_as_recorded(self):
        """A claim the record holds passes whatever the prose around it says; the text is
        code's: the template with the claim's value, and the cell as the source wrote it
        when that differs from the stated (canonical) value."""
        recorded = []

        def answers(request, entries):
            entry = entries["ordered"]
            self.assertNotIn("lead_basis", entry)   # a verified link: no lead mark needed
            recorded.append(_plain(entry["value"]))
            stated = str(int(Decimal(recorded[0])))
            return {"text": "Per the order line, the quantity comes to {q}.",
                    "claims": [{"id": "q", "value": stated, **_cell(entry)}]}
        agent, answer, _ = self.ask(QUANTITY_CARD, answers)
        stated = str(int(Decimal(recorded[0])))
        self.assertNotEqual(stated, recorded[0])   # the fixture writes it otherwise
        self.assertEqual((agent["stop"]["class"], agent["claims"]["kept"]), ("answered", 1))
        self.assertEqual(answer["text"], "Per the order line, the quantity comes to "
                                         f'{stated} (recorded as "{recorded[0]}").')
        self.assertEqual([v["value"] for v in answer["values"]], [stated])

    def test_only_checked_claims_reach_the_text(self):
        """A claim citing a handle the session never returned, a number the record does
        not hold, an unknown key and a claim stating no value are each refused with a typed
        reason; a sentence naming a refused claim, or holding a word that is not a frame word
        (a figure, a number word, a negator, a name or status in any case, an accented
        name, a negated state, a comparison, an ordinal, a fraction sign, a minus sign, a
        quantity word no claim it names states) or a name glued to another, is not
        rendered; the checked claim is."""
        def answers(request, entries):
            cell = _cell(entries["ordered"])
            right = str(int(Decimal(_plain(entries["ordered"]["value"]))))
            return {"text": "The quantity is {ok}. It also says {wrong}. "
                            "Then 4801 more came in {ok}. Of those, {ok} were shipped. "
                            "It is not {ok}. Then three more came in {ok}. "
                            "Acme sent {ok}. The order is Cancelled at {ok}. "
                            "The order {ok} was cancelled and remains unpaid. "
                            "The order is open at {ok}. The line is in {ok}, émile says. "
                            "\u00c9clat holds {ok}. The order is from acme: {ok}. "
                            "It exceeds {ok}. The first line is {ok}. It is \u00bd of {ok}. "
                            "It is -{ok}. Its value is {ok}{ok}. Its value is {ok}.{ok}",
                    "claims": [
                        {"id": "ok", "value": right, **cell},
                        {"id": "wrong", "value": str(int(right) + 1), **cell},
                        {"id": "elsewhere", "value": right, "citation": NEVER_RETURNED,
                         "field": cell["field"]},
                        {"id": "odd", "value": right, "note": "trust me", **cell},
                        {"id": "empty", **cell}]}
        agent, answer, requests = self.ask(QUANTITY_CARD, answers)
        self.assertEqual(len(requests), 1)   # one claim passed: answered, no repair
        right = answer["values"][0]["value"]
        self.assertEqual(answer["text"].split(" (recorded as")[0], f"The quantity is {right}")
        self.assertEqual(answer["text"].count("\n"), 0, answer["text"])
        for absent in (str(int(right) + 1), "4801", "trust me", "shipped", "also says",
                       "not", "three", "Acme", "Cancelled", "cancelled", "unpaid", "open",
                       "\u00e9mile", "\u00c9clat", "acme", "exceeds", "first", "\u00bd", "-",
                       right + right, right + "." + right):
            self.assertNotIn(absent, answer["text"])
        self.assertEqual([v["value"] for v in answer["values"]], [right])
        self.assertEqual(sorted((r["reason"], tuple(r["codes"]))
                                for r in agent["claims"]["refused"]),
                         sorted([("claim_check_failed", ("claim_value_mismatch",)),
                          ("claim_invalid", ()), ("claim_states_no_value", ()),
                          ("cites_what_this_run_did_not_return", ())]))
        self.assertEqual((agent["claims"]["kept"], agent["claims"]["unsupported"]), (1, 4))

    def test_a_value_on_a_lead_link_passes_only_marked_a_lead(self):
        """A value from a cell whose every returned entry rests on a lead-tier link
        (``lead_basis``) is refused unmarked (``claim_lead_unmarked``, the answerer asked to
        repair); marked ``lead`` it passes and the text says the link is not verified."""
        def answers(request, entries):
            entry = entries["order_number"]
            self.assertIs(entry["lead_basis"], True)
            return {"text": "The order carrying it is {n}.",
                    "claims": [{"id": "n", "value": _plain(entry["value"]), **_cell(entry),
                                **({"lead": True} if request.get("repair") else {})}]}
        agent, answer, requests = self.ask(LEAD_CARD, answers)
        (refused,) = requests[1]["repair"]["refused_claims"]
        self.assertEqual([f["code"] for f in refused["failures"]], ["claim_lead_unmarked"])
        number = _plain(_entries(requests[0])["order_number"]["value"])
        self.assertEqual(agent["stop"]["class"], "answered")
        self.assertEqual(answer["text"],
                         f"The order carrying it is {number} (lead: link not verified).")

        # The lead is the cell's, not the record's: another cell of the same record, read
        # on the card in view (step 0), passes unmarked beside the refused lead cell.
        def own_cell(request, entries):
            lead = _cell(entries["order_number"])
            (card,) = [o["result"] for o in request["transcript"][0]["observations"]
                       if o["tool"] == "plx.object"]
            other = next(v for v in card["values"]
                         if v["citation"]["l1_handle"] == lead["citation"]
                         and v["citation"]["field_path"] != lead["field"]
                         and isinstance(_plain(v["value"]), str))
            return {"text": "The order carrying it is {n}. Its record reads {own}.",
                    "claims": [{"id": "n", "value": _plain(entries["order_number"]["value"]),
                                **lead},
                               {"id": "own", "value": _plain(other["value"]),
                                "citation": other["citation"]["l1_handle"],
                                "field": other["citation"]["field_path"]}]}
        agent, answer, requests = self.ask(LEAD_CARD, own_cell)
        self.assertEqual((len(requests), agent["stop"]["class"], agent["claims"]["kept"]),
                         (1, "answered", 1))
        self.assertEqual([(r["reason"], r["codes"]) for r in agent["claims"]["refused"]],
                         [("claim_check_failed", ["claim_lead_unmarked"])])
        self.assertTrue(answer["text"].startswith("Its record reads "), answer["text"])
        self.assertNotIn(number, answer["text"])

    def test_n_1_10_20_60_claims_one_wrong_none_lost_or_duplicated(self):
        """Cardinality: N structured claims on one cell, each named by its own sentence,
        one of them wrong, below, at (20) and past the gate's bound: each right one is
        rendered once and in order, the wrong one refused with its sentence, the ones past
        the bound counted dropped; in budget."""
        for count in (1, 10, 20, 60):
            with self.subTest(count=count):
                bad, shown = count // 2, []

                def answers(request, entries, count=count, bad=bad):
                    cell = _cell(entries["ordered"])
                    right = int(Decimal(_plain(entries["ordered"]["value"])))
                    shown[:] = [right]
                    return {"text": " ".join(f"The line is {{c{n}}}."
                                             for n in range(count)),
                            "claims": [{"id": f"c{n}", "value": str(right + (n == bad)),
                                        **cell} for n in range(count)]}
                started = time.monotonic()
                agent, answer, _ = self.ask(QUANTITY_CARD, answers)
                self.assertLess(time.monotonic() - started, 60)
                kept = min(count, 20)
                passed = [n for n in range(kept) if n != bad]
                self.assertEqual(agent["claims"]["kept"], len(passed))
                self.assertEqual(agent["claims"]["unsupported"], int(bad < kept))
                # Past the bound a claim is counted dropped, never silently lost.
                self.assertEqual(agent["claims"]["dropped"], count - len(passed))
                lines = [line.split(" (recorded as")[0]
                         for line in answer["text"].split("\n") if line]
                self.assertEqual(lines, [f"The line is {shown[0]}"] * len(passed))


class RenderedQuantityWordTests(unittest.TestCase):
    """A quantity word of the prose frames only the claim named beside it. Evidence label
    ``isolated-fixture``: one assembly-order row (``order_entry_item_assembly``, whose
    contract types ``qtyplan_c`` planned and ``qtymfg_c`` manufactured) served to the gate
    in place of a runtime's rows; the gate, its role contracts and the rendering are
    real."""
    HANDLE = "api://public.order_entry_item_assembly/%7B%22id%22%3A%2210718%22%7D@0-1"
    ROW = {"handle": HANDLE, "relation": "public.order_entry_item_assembly", "revision": "0-1",
           "row": {"qtyplan_c": "1509", "qtymfg_c": "1503"}}

    def gate(self, text):
        session = mock.Mock(case_runtime=None, actor=None)
        claims = [{"id": "p", "value": "1509", "citation": self.HANDLE,
                   "field": "row.qtyplan_c", "role": "planned"},
                  {"id": "m", "value": "1503", "citation": self.HANDLE,
                   "field": "row.qtymfg_c", "role": "manufactured"}]
        rows = lambda runtime, actor: (lambda handle: self.ROW if handle == self.HANDLE
                                       else None)
        with mock.patch.object(answer_gate, "citation_rows", lambda runtime: None), \
                mock.patch.object(answer_gate, "gate_rows", rows):
            return answer_gate.gate_answer({"claims": claims, "text": text}, session,
                                           seen={self.HANDLE}, returned={},
                                           may_repair=False)

    def test_a_quantity_word_beside_another_role_is_not_rendered(self):
        lines = "planned: 1509\nmanufactured: 1503"
        for text, shown in (
                ("The AO had {p} planned and {m} manufactured.",
                 "The AO had 1509 planned and 1503 manufactured."),
                ("The AO had {m} planned and {p} manufactured.", lines),
                ("The AO had planned {m} and manufactured {p}.", lines),
                ("The AO had {p} planned {m}.", lines),
                ("The AO had {m} produced.", lines),
                ("The AO had {p}, planned.", lines)):
            with self.subTest(text=text):
                out = self.gate(text)
                self.assertEqual((out["outcome"], out["unsupported"]), ("answered", []))
                self.assertEqual(out["text"], shown)


def _handle(relation, key):
    return f"api://{relation}/%7B%22id%22%3A%22{key}%22%7D@0-1"


class OneProseScreenTests(unittest.TestCase):
    """One prose screen (``claim_checks.screen_prose``) for the answer the gate renders and
    the answer the eval scores. Evidence label ``isolated-fixture``: Portal mirror rows of a
    purchase order, a receipt, its line and a supplier served to the gate in place of a
    runtime's rows; the gate, the screen, the schema and the scorer are real."""
    PO = _handle("public.app_purchase_order", "p1")
    RECEIPT = _handle("public.app_receipt", "r1")
    LINE = _handle("public.app_receipt_line", "l1")
    SUPPLIER = _handle("public.app_supplier", "s1")
    BC_PO = ("bc://production/c0000000-0000-4000-8000-000000000001/purchaseOrders/"
             "b0000000-0000-4000-8000-000000000001@0-1")
    SHIPMENT = _handle("public.app_shipment", "sh1")
    ROWS = {
        PO: {"relation": "public.app_purchase_order",
             "row": {"po_number": "PO#5447", "status": "OPEN", "expected_date": "2026-03-01",
                     "confirmed_date": "2026-02-20"}},
        RECEIPT: {"relation": "public.app_receipt",
                  "row": {"receipt_number": "RC-0042", "received_date": "2026-03-04",
                          "fully_received": "false"}},
        LINE: {"relation": "public.app_receipt_line",
               "row": {"supplier_lot_number": "LOT-FXA", "lab_id": "LAB-9"}},
        SUPPLIER: {"relation": "public.app_supplier", "row": {"name": "Acme Labs"}},
        BC_PO: {"relation": "purchaseOrders",
                "row": {"number": "PO-1001", "requestedReceiptDate": "2026-03-09",
                        "expectedReceiptDate": "2026-03-09", "fullyReceived": "false"}},
        SHIPMENT: {"relation": "public.app_shipment",
                   "row": {"shipment_code": "SH-7", "shipped_at": "2026-03-02"}}}
    CELLS = {"po": (PO, "po_number"), "st": (PO, "status"), "e": (PO, "expected_date"),
             "c": (PO, "confirmed_date"), "r": (RECEIPT, "receipt_number"),
             "d": (RECEIPT, "received_date"), "f": (RECEIPT, "fully_received"),
             "l": (LINE, "supplier_lot_number"), "s": (SUPPLIER, "name"),
             "bn": (BC_PO, "number"), "rq": (BC_PO, "requestedReceiptDate"),
             "ex": (BC_PO, "expectedReceiptDate"), "sc": (SHIPMENT, "shipment_code"),
             "sa": (SHIPMENT, "shipped_at")}

    def claim(self, cid):
        handle, column = self.CELLS[cid]
        return {"id": cid, "value": self.ROWS[handle]["row"][column], "citation": handle,
                "field": f"row.{column}"}

    def gate(self, text, ids, *, claims=None, returned=None):
        session = mock.Mock(case_runtime=None, actor=None)
        rows = lambda runtime, actor: (lambda handle: {"handle": handle, "revision": "0-1",
                                                       **self.ROWS[handle]}
                                       if handle in self.ROWS else None)
        with mock.patch.object(answer_gate, "citation_rows", lambda runtime: None), \
                mock.patch.object(answer_gate, "gate_rows", rows):
            return answer_gate.gate_answer(
                {"claims": claims if claims is not None else [self.claim(i) for i in ids],
                 "text": text}, session, seen=set(self.ROWS), returned=returned or {},
                may_repair=False)

    def test_lot_receipt_and_supplier_templates_render_their_prose(self):
        """A template about a lot, a receipt or a supplier frames its claims with the
        ontology's own object-type nouns (``Lot``, ``Receipt``, ``Supplier``): it renders,
        never falls back to bare lines."""
        for text, ids, shown in (
                ("The supplier lot number is {l}.", "l",
                 "The supplier lot number is LOT-FXA."),
                ("The receipt number is {r}.", "r", "The receipt number is RC-0042."),
                ("The supplier is {s}.", "s", "The supplier is Acme Labs."),
                ("Lots of this receipt line: {l}.", "l", "Lots of this receipt line: LOT-FXA.")):
            with self.subTest(text=text):
                out = self.gate(text, ids)
                self.assertEqual((out["outcome"], out["unsupported"]), ("answered", []))
                self.assertEqual(out["text"], shown)

    def test_a_state_word_renders_only_bound_to_a_checked_claim_that_states_it(self):
        """``due``, ``confirmed``, ``received`` and ``not`` frame a value only when a checked
        claim of the sentence is about that state (the expected date, the confirmed date,
        the received date, a false received flag); ``supplies`` beside a supplier's name
        (no claim it supplies), ``late`` with no
        lateness claim, a state its claim's false value denies, and a negated claim value
        are not rendered (the claims fall back to lines under their schema labels)."""
        for text, ids, shown in (
                ("PO {po} was due on {e}.", ("po", "e"), "PO PO#5447 was due on 2026-03-01."),
                ("The supplier confirmed {c}.", ("c",), "The supplier confirmed 2026-02-20."),
                ("{s} supplies the order.", ("s",), "name: Acme Labs"),
                ("Receipt {r} was received on {d}.", ("r", "d"),
                 "Receipt RC-0042 was received on 2026-03-04."),
                ("The receipt is not received: {f}.", ("f",),
                 "The receipt is not received: false."),
                ("PO {po} is late.", ("po",), "PO number: PO#5447"),
                ("The receipt was received: {f}.", ("f",), "fully received: false"),
                ("The order is not {st}.", ("st",), "status: OPEN"),
                ("PO {po} was due.", ("po",), "PO number: PO#5447")):
            with self.subTest(text=text):
                out = self.gate(text, ids)
                self.assertEqual((out["outcome"], out["unsupported"]), ("answered", []))
                self.assertEqual(out["text"], shown)

    def test_an_event_word_binds_only_to_a_field_recording_the_event(self):
        """``received`` and ``shipped`` say the event happened: they bind only to a field
        that records it (``received_date``, ``shipped_at``), never to a planned, expected or
        requested date (``requestedReceiptDate`` on a PO not fully received), to a code on
        the shipment record (``shipment_code``) or to a supplier's name; in the gate the
        claims fall back to lines, in the scorer the sentence is unchecked."""
        from evals import score
        wrong = (("The PO was received on {rq}.", ("rq",), "received"),
                 ("The PO was received on {ex}.", ("ex",), "received"),
                 ("PO {bn} was received on {rq}.", ("bn", "rq"), "received"),
                 ("Shipment {sc} shipped.", ("sc",), "shipped"),
                 ("{s} supplied the order.", ("s",), "supplied"))
        for template, ids, word in wrong:
            with self.subTest(path="gate", template=template):
                out = self.gate(template, ids)
                self.assertEqual(out["text"], "\n".join(c["text"] for c in out["claims"]))
                self.assertNotIn(word, out["text"])
            with self.subTest(path="scorer", template=template):
                text = template.format(**{i: self.claim(i)["value"] for i in ids})
                values = [{"value": self.claim(i)["value"], "citations": [
                    {"l1_handle": self.CELLS[i][0], "field_path": f"row.{self.CELLS[i][1]}"}]}
                    for i in ids]
                evidence = {self.CELLS[i][0]: {"ok": True, **self.ROWS[self.CELLS[i][0]]}
                            for i in ids}
                report = score.gate({"text": text, "values": values}, evidence)["report"]
                self.assertEqual([(f["code"], f["value"]) for f in report["failures"]],
                                 [("claim_prose_unchecked", word)])
        for template, ids, shown in (
                ("Shipment {sc} shipped on {sa}.", ("sc", "sa"),
                 "Shipment SH-7 shipped on 2026-03-02."),
                ("The PO was due on {rq}.", ("rq",), "The PO was due on 2026-03-09.")):
            with self.subTest(path="gate", template=template):
                self.assertEqual(self.gate(template, ids)["text"], shown)

    def test_an_unstated_number_is_reported_whole(self):
        failures = claim_checks.screen_prose("PO PO#5447 has 99 lines.",
                                             [self.claim("po")])
        self.assertEqual([(f["code"], f["value"]) for f in failures],
                         [("claim_prose_unchecked", "99")])

    def test_a_fallback_line_uses_the_schema_label_never_the_field_path(self):
        out = self.gate("", ("po", "d", "c"))
        self.assertEqual(out["text"], "PO number: PO#5447\nreceived date: 2026-03-04\n"
                                      "confirmed date: 2026-02-20")
        self.assertEqual([claim["text"] for claim in out["claims"]],
                         ["PO number: PO#5447", "received date: 2026-03-04",
                          "confirmed date: 2026-02-20"])

    def test_a_negated_value_is_refused_by_the_one_screen_in_the_gate_and_the_scorer(self):
        """``The order is not OPEN`` states no ``OPEN``: the gate does not render it, and
        the scorer refuses it in an answer's text (``claim_value_unstated``, blocking),
        both through ``claim_checks.screen_prose``; the same sentence unnegated passes."""
        from evals import score
        from operating import claim_checks
        answer = lambda text: {"text": text, "values": [
            {"value": "OPEN", "citations": [{"l1_handle": self.PO, "field_path": "row.status"}]}]}
        evidence = {self.PO: {"ok": True, **self.ROWS[self.PO]}}
        with mock.patch.object(claim_checks, "screen_prose",
                               wraps=claim_checks.screen_prose) as screen:
            gated = self.gate("The order is not {st}. The order status is {st}.", ("st",))
            self.assertEqual(gated["text"], "The order status is OPEN.")
            self.assertTrue(screen.called)
            screen.reset_mock()
            refused = score.gate(answer("The order is not OPEN."), evidence)["report"]
            self.assertTrue(screen.called)
        codes = [(f["item"], f["code"]) for f in refused["failures"]]
        self.assertIn(("outcome", "claim_value_unstated"), codes)
        self.assertIn("claim_value_unstated", score.BLOCKING_GATE_CODES)
        self.assertFalse(refused["passed"])
        counts = refused["counts"]   # the refused claim moves from passed to failed
        self.assertEqual((counts["claims"], counts["passed"], counts["failed"]), (1, 0, 1))
        stated = score.gate(answer("The order status is OPEN."), evidence)["report"]
        self.assertEqual(stated["failures"], [])
        self.assertTrue(stated["passed"])

    def test_the_scorer_reports_unchecked_prose_as_unverified_without_splitting_b1(self):
        """A word the screen cannot tie to a checked claim (``late``) is reported on the
        eval path as ``claim_prose_unchecked``: unverified, never blocking, so the one B1
        rate pair stays one (decision 2026-10-19-b1-one-answer-rate)."""
        from evals import score
        answer = {"text": "PO PO#5447 is late.", "values": [
            {"value": "PO#5447", "citations": [{"l1_handle": self.PO,
                                                "field_path": "row.po_number"}]}]}
        report = score.gate(answer, {self.PO: {"ok": True, **self.ROWS[self.PO]}})["report"]
        self.assertEqual([(f["code"], f["value"]) for f in report["failures"]],
                         [("claim_prose_unchecked", "late")])
        self.assertIn("claim_prose_unchecked", score.UNVERIFIED_GATE_CODES)
        self.assertNotIn("claim_prose_unchecked", score.BLOCKING_GATE_CODES)

    def test_the_scorer_screen_reads_declared_gaps_as_stated_and_counts_no_prose_claim(self):
        """An eval answer may say the gap it declares, as declared (``the receipt is not
        ingested`` beside a ``not_ingested`` Receipt gap), and name the field of a claim it
        writes (``purchase order number``). An undeclared, unknown, other-subject or
        inverted gap is unchecked; a prose finding is no claim and fails no report."""
        from evals import score
        evidence = {self.PO: {"ok": True, **self.ROWS[self.PO]}}
        values = [{"value": "PO#5447", "citations": [{"l1_handle": self.PO,
                                                      "field_path": "row.po_number"}]}]
        receipt_gap = [{"state": "not_ingested", "subject": "Receipt"}]
        said = "The purchase order number is PO#5447; the receipt is not ingested."
        cases = [
            (said, receipt_gap, []),
            (said, [], [("claim_prose_unchecked", "not")]),
            (said, [{"state": "not_ingested", "subject": "Supplier"}],
             [("claim_prose_unchecked", "not")]),
            (said, [{"state": "lost_forever", "subject": "Receipt"}],
             [("claim_prose_unchecked", "not")]),
            (said, "not_ingested", [("claim_prose_unchecked", "not")]),
            (said, [{"state": "not_ingested", "subject": "Receipts!"}],
             [("claim_prose_unchecked", "not")]),
            ("The PO number is PO#5447; the receipt is ingested.", receipt_gap,
             [("claim_prose_unchecked", "ingested")]),
            ("The PO number is PO#5447; the receipt is not stale.",
             [{"state": "stale", "subject": "Receipt"}], [("claim_prose_unchecked", "not")]),
            ("The PO number is PO#5447; the receipt is stale and unknown.",
             [{"state": "stale", "subject": "Receipt"}],
             [("claim_prose_unchecked", "unknown")]),
            ("The PO total is PO#5447.", [], [("claim_prose_unchecked", "total")]),
        ]
        for text, gaps, expected in cases:
            with self.subTest(text=text, gaps=gaps):
                report = score.gate({"text": text, "values": values, "gaps": gaps},
                                    evidence)["report"]
                self.assertEqual([(f["code"], f["value"]) for f in report["failures"]],
                                 expected)
                self.assertEqual(report["counts"]["claims"], 1)
                self.assertEqual(report["counts"]["failed"], 0)
                self.assertTrue(report["passed"])

    def test_n_1_10_60_scored_sentences_one_finding_each_none_lost(self):
        """Cardinality on the eval path: N sentences each stating an unchecked state give N
        findings (one per sentence, none merged or lost), N plain ones none; in budget."""
        from evals import score
        evidence = {self.PO: {"ok": True, **self.ROWS[self.PO]}}
        for count in (1, 10, 60):
            with self.subTest(count=count):
                values = [{"value": "PO#5447", "citations": [
                    {"l1_handle": self.PO, "field_path": "row.po_number"}]}]
                started = time.monotonic()
                late = score.gate({"text": " ".join(["PO PO#5447 is late."] * count),
                                   "values": values}, evidence)["report"]
                plain = score.gate({"text": "\n".join(["The PO number is PO#5447."] * count),
                                    "values": values}, evidence)["report"]
                self.assertLess(time.monotonic() - started, 30)
                self.assertEqual([(f["code"], f["value"]) for f in late["failures"]],
                                 [("claim_prose_unchecked", "late")] * count)
                self.assertEqual(plain["failures"], [])

    def test_no_schema_no_frame_nouns_and_the_template_falls_back(self):
        """Fail closed: when the ontology schema does not load the screen has no frame
        nouns, so a template framed by one falls back to its line (the column as words)."""
        from operating import claim_checks
        cached = (claim_checks.frame_nouns, claim_checks._schema_names)
        for function in cached:
            function.cache_clear()
        try:
            with mock.patch("ontology.schema.load_schema", side_effect=RuntimeError("down")):
                self.assertEqual(claim_checks.frame_nouns(), frozenset())
                out = self.gate("The receipt number is {r}.", ("r",))
        finally:
            for function in cached:
                function.cache_clear()
        self.assertEqual((out["outcome"], out["text"]), ("answered", "receipt number: RC-0042"))
        self.assertIn("receipt", claim_checks.frame_nouns())

    def test_an_mcp_shaped_claim_carries_its_lead_mark(self):
        """A claim in the MCP shape (text, citations, value_claims) marked ``lead`` keeps
        the mark on each value claim it gives: it passes as a lead, said to be one;
        unmarked on a lead cell it is refused."""
        def legacy(lead):
            return [{"text": "the receipt number", "citations": [self.RECEIPT], **lead,
                     "value_claims": [{"value": "RC-0042", "citation": self.RECEIPT,
                                       "field": "row.receipt_number"}]}]
        leads = {"leads": {(self.RECEIPT, "row.receipt_number"): True}}
        marked = self.gate("", (), claims=legacy({"lead": True}), returned=leads)
        self.assertEqual(marked["unsupported"], [])
        self.assertEqual([claim["lead"] for claim in marked["claims"]], [True])
        self.assertEqual(marked["text"], "receipt number: RC-0042 (lead: link not verified)")
        unmarked = self.gate("", (), claims=legacy({}), returned=leads)
        self.assertEqual([f["code"] for u in unmarked["unsupported"] for f in u["failures"]],
                         ["claim_lead_unmarked"])

    def test_the_gate_keeps_no_word_lists_of_its_own(self):
        """Frame nouns come from the ontology's object types and codes from
        ``KNOWN_ID_PREFIXES``: the gate has no hand-written frame lists, imports nothing
        private from the claim checks, and the screen's nouns are the schema's."""
        from operating import claim_checks
        for name in ("FRAME_WORDS", "FRAME_CODES", "_frames_only"):
            self.assertFalse(hasattr(answer_gate, name), name)
        source = Path(answer_gate.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_dig", source)
        nouns = claim_checks.frame_nouns()
        for noun in ("lot", "lots", "receipt", "receipts", "supplier", "suppliers", "order",
                     "purchase", "entries"):
            self.assertIn(noun, nouns)
        for word in ("late", "open", "cancelled", "not", "shipped", "damage", "approval",
                     "production", "release", "aged"):
            self.assertNotIn(word, nouns)


if __name__ == "__main__":
    unittest.main()
