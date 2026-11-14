"""Structured answer claims (the acceptance bar ``structured-answer-claims``): the planner states
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
                # Past the bound a claim is counted dropped, never silently lost, and
                # the answer is never complete (AGT-004).
                self.assertEqual(agent["claims"]["dropped"], count - len(passed))
                if count > 20:
                    self.assertEqual(answer["answerability"], "partial")
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
        rows = lambda runtime, actor, **_kw: (lambda handle: self.ROW if handle == self.HANDLE
                                       else None)
        with mock.patch.object(answer_gate, "citation_rows", lambda runtime, **_kw: None), \
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
        rows = lambda runtime, actor, **_kw: (lambda handle: {"handle": handle, "revision": "0-1",
                                                       **self.ROWS[handle]}
                                       if handle in self.ROWS else None)
        with mock.patch.object(answer_gate, "citation_rows", lambda runtime, **_kw: None), \
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
        rate pair stays one."""
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


class OnQuestionTests(unittest.TestCase):
    """A claim on a record the question's records neither name nor link to stands only
    with a ``via`` that does (``gate_answer``'s ``relevant``, ``mcp_server._relevance``).
    Evidence label ``isolated-fixture``: two rows served to the gate in place of a
    runtime's; the gate and the relevance check over its reads are real."""
    ORDER = "api://public.app_purchase_order/%7B%22id%22%3A%221%22%7D@0-1"
    LINE = "api://public.app_receipt_line/%7B%22id%22%3A%222%22%7D@0-1"
    MOVED = "api://public.app_purchase_order/%7B%22id%22%3A%223%22%7D@0-1"
    GONE = "api://public.app_purchase_order/%7B%22id%22%3A%224%22%7D@0-1"
    NAMED = "api://public.app_receipt_line/%7B%22id%22%3A%225%22%7D@0-1"
    OTHER = "api://public.app_purchase_order/%7B%22id%22%3A%227%22%7D@0-1"
    ALIEN = "api://public.app_receipt_line/%7B%22id%22%3A%228%22%7D@0-1"
    ROWS = {ORDER: {"relation": "public.app_purchase_order",
                    "row": {"po_number": "PO-0103510", "receipt_id": "77"}},
            LINE: {"relation": "public.app_receipt_line", "row": {"quantity": "40"}},
            # a column named like the code, no value saying it
            NAMED: {"relation": "public.app_receipt_line",
                    "row": {"po0103510_quantity": "40"}},
            OTHER: {"relation": "public.app_purchase_order", "row": {"quantity": "40"}},
            ALIEN: {"relation": "public.app_receipt_line", "row": {"quantity": "40"}}}

    def view(self, handle):
        if handle == self.MOVED:
            # The source moved past the revision cited: the answer gate's re-read keeps
            # that revision's row only as ``revision`` (``gate_rows`` moved_on).
            return {"citation": handle, "status": "changed_since", "changed_since": True,
                    "revision": {"handle": handle, "relation": "public.app_purchase_order",
                                 "row": {"po_number": "PO-0103510", "quantity": "40"}}}
        if handle in self.ROWS:
            return {"handle": handle, "revision": "0-1", **self.ROWS[handle]}
        return None

    def gate(self, claim, ontology=None, records=()):
        from operating import mcp_server
        session = mock.Mock(case_runtime=None, actor=None)
        rows = lambda runtime, actor, **_kw: self.view
        server = mock.Mock(ontology=ontology or object(), _anchors={},
                           _anchors_whole=True, _finds=[])
        server._resolve_anchors = lambda *args: mcp_server.MCPServer._resolve_anchors(
            server, *args)

        def anchors(_server, _asked, found=None, **_kw):
            if found is not None:
                found.update(records)
            return mcp_server._anchor_keys(["PO0103510"])
        with mock.patch.object(mcp_server, "_question_anchors", anchors):
            relevant = mcp_server.MCPServer._relevance(server, "Did PO0103510 arrive?")
        with mock.patch.object(answer_gate, "citation_rows", lambda runtime, **_kw: None), \
                mock.patch.object(answer_gate, "gate_rows", rows):
            return answer_gate.gate_answer({"claims": [claim]}, session,
                                           seen={self.ORDER, self.LINE, self.MOVED,
                                                 self.GONE, self.NAMED, self.OTHER,
                                                 self.ALIEN}, returned={},
                                           may_repair=True, relevant=relevant)

    def reasons(self, answer):
        return [u["reason"] for u in answer["unsupported"]]

    def test_a_record_that_moved_on_is_read_as_the_revision_cited(self):
        # the question's own PO, its source ahead of the projection: never off the
        # question (its own moved-on check decides its value)
        moved = {"id": "o", "value": "PO-0103510", "citation": self.MOVED,
                 "field": "row.po_number"}
        self.assertNotIn(answer_gate.OFF_QUESTION, self.reasons(self.gate(moved)))
        line = {"id": "q", "value": "40", "citation": self.LINE, "field": "row.quantity"}
        self.assertEqual([c["id"] for c in self.gate(
            {**line, "via": self.MOVED}, self.linked_world())["claims"]], ["q"])

    def test_a_via_connects_only_a_record_links_join_it_to(self):
        """GATE-001: a via on the question rescues a claim only on a record firm links
        join to the via's record: never one joined to it through a hub alone, through a
        record this reader may not read, or with no store to walk (unknown)."""
        world = self.linked_world()
        line = {"id": "q", "value": "40", "citation": self.LINE, "field": "row.quantity"}
        self.assertEqual([c["id"] for c in self.gate({**line, "via": self.ORDER},
                                                     world)["claims"]], ["q"])
        for handle in (self.ALIEN, self.OTHER):   # joined only through a hub
            claim = {"id": "n", "value": "40", "citation": handle, "field": "row.quantity"}
            self.assertEqual(self.reasons(self.gate({**claim, "via": self.ORDER}, world)),
                             [answer_gate.OFF_QUESTION], handle)
        self.assertEqual(self.reasons(self.gate(
            {**line, "via": self.ORDER}, self.linked_world(hidden={"Receipt:9"}))),
            [answer_gate.OFF_QUESTION])
        self.assertEqual(self.reasons(self.gate({**line, "via": self.ORDER})),
                         [answer_gate.OFF_QUESTION])   # no store: unknown connects nothing

    def linked_world(self, hidden=()):
        """The question's order PO:1 and its links, as the relevance walk reads them: a
        receipt line two links away, a mention, and two hubs it touches (its customer
        and its line's item, each with more than ``HUB_LINKS`` records)."""
        import contextlib
        import sqlite3
        import threading
        import types
        from operating import mcp_server
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.executescript(
            "CREATE TABLE object_versions (object_id TEXT, l1_handle TEXT);"
            "CREATE TABLE link_rows (from_object TEXT, to_object TEXT, superseded_at TEXT,"
            " binding_kind TEXT, tier TEXT);")
        conn.executemany("INSERT INTO object_versions VALUES (?, ?)",
                         [("PO:1", self.ORDER), ("Line:2", self.LINE), ("PO:3", self.MOVED),
                          ("Line:5", self.NAMED), ("PO:7", self.OTHER),
                          ("Line:8", self.ALIEN)])
        links = [("Receipt:9", "PO:1", "fk", "verified"), ("Receipt:9", "PO:3", "fk", "verified"),
                 ("Line:2", "Receipt:9", "fk", "verified"),
                 ("Line:5", "PO:1", "mention", "corroborated"),
                 ("PO:1", "Cust:1", "fk", "verified"), ("PO:7", "Cust:1", "fk", "verified"),
                 ("Line:3", "PO:1", "fk", "verified"), ("Line:3", "Item:1", "fk", "verified"),
                 ("Line:8", "Item:1", "fk", "verified"), ("Line:8", "PO:9", "fk", "verified"),
                 ("PO:9", "Cust:2", "fk", "verified")]
        links += [(f"PO:h{n}", "Cust:1", "fk", "verified")
                  for n in range(mcp_server.HUB_LINKS + 1)]
        links += [(f"Line:h{n}", "Item:1", "fk", "verified")
                  for n in range(mcp_server.HUB_LINKS + 1)]
        conn.executemany("INSERT INTO link_rows VALUES (?, ?, NULL, ?, ?)", links)
        store = types.SimpleNamespace(conn=conn, lock=threading.Lock())
        tools = types.SimpleNamespace(
            store=store, policy=None, _as_known_at=lambda arguments: None,
            _scope=lambda actor, at: "scope",
            _visible_versions=lambda scope, ids: {i: {} for i in ids if i not in hidden})
        return types.SimpleNamespace(
            tools=tools, request=contextlib.nullcontext, per_call=contextlib.nullcontext,
            policy_now=lambda actor: None)

    def test_the_walk_reads_the_store_file_a_generation_swap_put_in_place(self):
        """The relevance walk's own read-only connection follows the file at the store's
        path: a new generation renamed into place is read on a new connection and the
        old file's connection is closed (never links of a generation no longer served)."""
        import sqlite3
        import threading
        import types
        from operating import mcp_server
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ontology.sqlite"

            def generation(name, object_id):
                conn = sqlite3.connect(Path(tmp) / name)
                conn.execute("CREATE TABLE object_versions (object_id TEXT, l1_handle TEXT)")
                conn.execute("INSERT INTO object_versions VALUES (?, 'h')", (object_id,))
                conn.commit()
                conn.close()
            generation("ontology.sqlite", "PO:old")
            store = types.SimpleNamespace(conn=sqlite3.connect(path), lock=threading.Lock())
            server = types.SimpleNamespace(ontology=types.SimpleNamespace(
                tools=types.SimpleNamespace(store=store)))
            self.addCleanup(mcp_server._LINK_CONNS.pop, str(path.resolve()), None)
            first = mcp_server._link_connection(server)
            self.assertIs(mcp_server._link_connection(server), first)
            self.assertEqual(first.execute("SELECT object_id FROM object_versions")
                             .fetchall(), [("PO:old",)])
            generation("next.sqlite", "PO:new")
            (Path(tmp) / "next.sqlite").replace(path)
            store.conn.close()
            store.conn = sqlite3.connect(path)   # the host reopens the swapped generation
            second = mcp_server._link_connection(server)
            self.assertIsNot(second, first)
            self.assertEqual(second.execute("SELECT object_id FROM object_versions")
                             .fetchall(), [("PO:new",)])
            with self.assertRaises(sqlite3.ProgrammingError):
                first.execute("SELECT 1")   # the old file's connection is closed
            second.close()
            store.conn.close()

    def test_a_record_linked_to_the_questions_record_is_on_the_question(self):
        """A line whose row names the question's order only by its id stands without a
        via when a firm link joins it to that order (within the link hops); a mention
        read in text joins nothing; nor does a hub: another order of the order's customer
        and another customer's line of the order's item stay off the question; nor a
        record joined only through one this reader may not read."""
        ontology = self.linked_world()
        line = {"id": "q", "value": "40", "citation": self.LINE, "field": "row.quantity"}
        self.assertEqual([c["id"] for c in self.gate(line, ontology, {"PO:1"})["claims"]],
                         ["q"])
        for handle, field in ((self.NAMED, "row.po0103510_quantity"),
                              (self.OTHER, "row.quantity"), (self.ALIEN, "row.quantity")):
            claim = {"id": "n", "value": "40", "citation": handle, "field": field}
            self.assertEqual(self.reasons(self.gate(claim, ontology, {"PO:1"})),
                             [answer_gate.OFF_QUESTION], handle)
        hidden = self.linked_world(hidden={"Receipt:9"})
        self.assertEqual(self.reasons(self.gate(line, hidden, {"PO:1"})),
                         [answer_gate.OFF_QUESTION])

    def test_a_record_names_the_question_s_code_on_whole_words_only(self):
        """GATE-002: the question's PO0103510 names a row saying PO-0103510, "PO
        0103510" or the number alone as the whole cell, or page text writing the code;
        never a longer code (PO-01035100, PO01035101), the number inside another kind's
        code (SO-0103510) or prose writing the bare number, or a short name inside a
        longer party's cell."""
        from operating import mcp_server
        cells = {"PO-0103510": True, "PO 0103510": True, "0103510": True,
                 "Freight for PO0103510/2": True,
                 "PO-01035100": False, "PO01035101": False, "XPO0103510": False,
                 "SO-0103510": False, "invoice 0103510 paid": False}
        for said, on in cells.items():
            self.ROWS = {**type(self).ROWS, self.OTHER: {
                "relation": "public.app_purchase_order",
                "row": {"ref": said, "quantity": "40"}}}
            claim = {"id": "c", "value": "40", "citation": self.OTHER,
                     "field": "row.quantity"}
            reasons = self.reasons(self.gate(claim))
            self.assertEqual(reasons, [] if on else [answer_gate.OFF_QUESTION], said)
        text = {"handle": self.OTHER, "row": {}, "text": "Re: PO 0103510 arrived late"}
        self.assertTrue(any(mcp_server._names("PO0103510", said, cell=cell)
                            for said, cell in mcp_server._said_values(text)))
        name = mcp_server._letters_digits("Acme Foods")
        for said, on in (("Acme Foods", True), ("ACME-FOODS", True),
                         ("Acme Foods International", False), ("AcmeFoodsCo", False)):
            self.assertEqual(mcp_server._names(name, mcp_server._tokens(said), cell=True),
                             on, said)
        self.assertTrue(mcp_server._names(name, mcp_server._tokens(
            "Spoke with Acme Foods about the late delivery"), cell=False))

    def test_a_via_that_cannot_be_read_connects_nothing(self):
        line = {"id": "q", "value": "40", "citation": self.LINE, "field": "row.quantity"}
        self.assertEqual(self.reasons(self.gate({**line, "via": self.GONE})),
                         [answer_gate.OFF_QUESTION])

    def test_a_column_name_names_nothing(self):
        named = {"id": "n", "value": "40", "citation": self.NAMED,
                 "field": "row.po0103510_quantity"}
        self.assertEqual(self.reasons(self.gate(named)), [answer_gate.OFF_QUESTION])

    def test_the_session_sets_the_question_every_submission_is_decided_against(self):
        from operating import agent_session
        answer = {"claims": [], "question": "something else"}
        self.assertEqual(agent_session.asked_as(answer, "Did PO0103510 arrive?")["question"],
                         "Did PO0103510 arrive?")
        self.assertEqual(agent_session.asked_as({"claims": []}, "Q?")["question"], "Q?")
        self.assertEqual(agent_session.asked_as(answer, None), answer)
        agent = agent_session.ClaudeCodeAgent()
        self.assertEqual(agent.for_question("Q?").asked, "Q?")
        self.assertIsNone(agent.asked)

    def told(self, claims, question, *, entities=None, finds=(), resolve=None):
        """``claims`` decided for ``question`` as a session decides them: told the question
        at its start (``_told_question``; ``entities`` as the app resolved them, None:
        resolved by ``resolve``), then its submission; (decision, resolver calls)."""
        from operating import mcp_server, objective_runner
        calls = []

        def resolving(asked, find):
            calls.append(asked)
            return resolve(asked) if resolve is not None else []
        server = mock.MagicMock(ontology=object(), _anchors={}, _anchors_whole=True,
                                _finds=list(finds))
        for name in ("_resolve_anchors", "_told_question"):
            setattr(server, name, getattr(mcp_server.MCPServer, name).__get__(server))
        session = mock.Mock(case_runtime=None, actor=None)
        rows = lambda runtime, actor, **_kw: self.view
        with mock.patch.object(objective_runner, "resolve_entities", resolving):
            if entities is not False:
                server._told_question({"question": question, "entities": entities})
            relevant = mcp_server.MCPServer._relevance(server, question)
        with mock.patch.object(answer_gate, "citation_rows", lambda runtime, **_kw: None), \
                mock.patch.object(answer_gate, "gate_rows", rows):
            return answer_gate.gate_answer({"claims": claims}, session,
                                           seen={self.ORDER, self.OTHER}, returned={},
                                           may_repair=True, relevant=relevant), calls

    def named_rows(self):
        self.ROWS = {**self.ROWS,
                     self.ORDER: {"relation": "public.app_purchase_order",
                                  "row": {"vendor": "Northwind Paints", "quantity": "40"}},
                     self.OTHER: {"relation": "public.app_purchase_order",
                                  "row": {"vendor": "Contoso Supply", "quantity": "40"}}}
        return ({"id": "n", "value": "40", "citation": self.ORDER, "field": "row.quantity"},
                {"id": "c", "value": "40", "citation": self.OTHER, "field": "row.quantity"})

    def test_the_question_told_at_the_start_is_not_searched_for_at_the_submission(self):
        """The app's front door resolved the question's things before the session: the
        submission makes no search, and an off-question citation is still refused."""
        named, other = self.named_rows()
        entities = [{"mention": "Northwind Paints", "status": "match",
                     "records": [{"object_id": "PO:1", "status": "match"}]}]
        decided, calls = self.told([named, other], "What did Northwind Paints receive?",
                                   entities=entities)
        self.assertEqual(calls, [])
        self.assertEqual([c["id"] for c in decided["claims"]], ["n"])
        self.assertEqual(self.reasons(decided), [answer_gate.OFF_QUESTION])
        # told without them: resolved once, at the start, never again at the submission
        found = lambda asked: entities   # noqa: E731
        decided, calls = self.told([named, other], "What did Northwind Paints receive?",
                                   resolve=found)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.reasons(decided), [answer_gate.OFF_QUESTION])

    def test_a_failed_front_door_lookup_is_not_a_lookup_that_found_nothing(self):
        """The front door's entity lookup failed: the app tells the session nothing (not
        ``[]``), the session resolves the names itself, and an off-question citation is
        still refused."""
        from operating import objective_runner

        def failing(arguments):
            raise RuntimeError("find is down")
        question = "What did Northwind Paints receive?"
        found = objective_runner.find_roots(question, failing)
        self.assertNotIsInstance(found["entities"], list)
        told_entities = found["entities"] if isinstance(found["entities"], list) else None
        named, other = self.named_rows()
        entities = [{"mention": "Northwind Paints", "status": "match",
                     "records": [{"object_id": "PO:1", "status": "match"}]}]
        decided, calls = self.told([named, other], question, entities=told_entities,
                                   resolve=lambda asked: entities)
        self.assertEqual(len(calls), 1)
        self.assertEqual([c["id"] for c in decided["claims"]], ["n"])
        self.assertEqual(self.reasons(decided), [answer_gate.OFF_QUESTION])

    def test_a_resolution_out_of_time_refuses_what_it_cannot_connect(self):
        """Not resolved in time, at the start or the submission's one bounded retry: the
        question's own identifiers still stand, and a claim nothing connects is refused,
        never let through unchecked (fail closed)."""
        from operating import call_limits
        named, other = self.named_rows()

        def late(asked):
            raise call_limits.ToolTimedOut()
        decided, calls = self.told([named, other], "What did Northwind Paints receive?",
                                   resolve=late)
        self.assertEqual(len(calls), 2)   # the start, then one try at the submission
        self.assertEqual(decided["claims"], [])
        self.assertEqual(self.reasons(decided), [answer_gate.OFF_QUESTION] * 2)
        # never told (a client without the gate's notice): resolved at the submission
        order = {"id": "o", "value": "PO-0103510", "citation": self.ORDER,
                 "field": "row.po_number"}
        self.ROWS = dict(type(self).ROWS)
        decided, calls = self.told([order, other], "Did PO0103510 arrive?",
                                   entities=False, resolve=late)
        self.assertEqual(len(calls), 1)
        self.assertEqual([c["id"] for c in decided["claims"]], ["o"])
        self.assertEqual(self.reasons(decided), [answer_gate.OFF_QUESTION])

    def test_the_sessions_own_find_of_the_question_connects_and_no_other_does(self):
        """A session whose resolution ran out of time: its own find of the question's
        words, resolved as one thing, connects that thing's records; a find of words
        the question does not write connects nothing."""
        from operating import call_limits
        named, other = self.named_rows()

        def late(asked):
            raise call_limits.ToolTimedOut()

        def entity_find(text, oid):
            return ({"mode": "entities", "text": text},
                    {"entities": [{"mention": text, "status": "match",
                                   "records": [{"object": {"object_id": oid},
                                                "status": "match"}]}]})
        finds = [entity_find("Northwind Paints", "PO:1"),
                 entity_find("Contoso Supply", "PO:7")]
        decided, _calls = self.told([named, other], "What did Northwind Paints receive?",
                                    resolve=late, finds=finds)
        self.assertEqual([c["id"] for c in decided["claims"]], ["n"])
        self.assertEqual(self.reasons(decided), [answer_gate.OFF_QUESTION])

    def test_the_sessions_find_of_a_name_the_question_writes_connects_its_record(self):
        """A chat's topic the entity index holds no name of (dev 11-10: "R&D + Marketing"
        resolved only as the word "R&D", other records): the session's own find of the
        name the question writes, found as exactly one record of that very name, puts
        that record on the question, so a message linked to it stands; a find of words the
        question does not write, of a record named otherwise, or naming two records,
        connects nothing."""
        import contextlib
        import sqlite3
        import threading
        import types
        from operating import mcp_server
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.executescript(
            "CREATE TABLE object_versions (object_id TEXT, l1_handle TEXT);"
            "CREATE TABLE link_rows (from_object TEXT, to_object TEXT, superseded_at TEXT,"
            " binding_kind TEXT, tier TEXT);")
        conn.executemany("INSERT INTO object_versions VALUES (?, ?)",
                         [("Msg:2", self.LINE), ("Cust:9", self.OTHER)])
        conn.executemany("INSERT INTO link_rows VALUES (?, ?, NULL, 'fk', 'verified')",
                         [("Msg:2", "Chat:1"), ("Chat:1", "Cust:4")])
        store = types.SimpleNamespace(conn=conn, lock=threading.Lock())
        tools = types.SimpleNamespace(
            store=store, policy=None, _as_known_at=lambda arguments: None,
            _scope=lambda actor, at: "scope",
            _visible_versions=lambda scope, ids: {i: {} for i in ids})
        world = types.SimpleNamespace(
            tools=tools, request=contextlib.nullcontext, per_call=contextlib.nullcontext,
            policy_now=lambda actor: None)
        question = "How many messages were posted in Teams chat R&D + Marketing?"
        stray = [{"mention": "R&D", "status": "match",
                  "records": [{"object_id": "Cust:9", "status": "match"}]}]

        def find(query, *names, status="found", object_type="TeamsChat"):
            return ({"query": query, "object_type": object_type},
                    {"status": status, "candidates": [
                        {"object": {"object_id": f"Chat:{n + 1}",
                                    "display": {"untrusted_text": name}}}
                        for n, name in enumerate(names)]})
        message = {"id": "m", "value": "40", "citation": self.LINE, "field": "row.quantity"}

        def decided(*finds, asked=question):
            server = mock.MagicMock(ontology=world, _anchors={}, _anchors_whole=True,
                                    _finds=list(finds))
            server._resolve_anchors = mcp_server.MCPServer._resolve_anchors.__get__(server)
            server._told_question = mcp_server.MCPServer._told_question.__get__(server)
            server._told_question({"question": asked, "entities": stray})
            relevant = mcp_server.MCPServer._relevance(server, asked)
            session = mock.Mock(case_runtime=None, actor=None)
            with mock.patch.object(answer_gate, "citation_rows", lambda runtime, **_kw: None), \
                    mock.patch.object(answer_gate, "gate_rows",
                                      lambda runtime, actor, **_kw: self.view):
                return answer_gate.gate_answer({"claims": [message]}, session,
                                               seen={self.LINE}, returned={},
                                               may_repair=True, relevant=relevant)
        self.assertEqual(self.reasons(decided()), [answer_gate.OFF_QUESTION])
        self.assertEqual([c["id"] for c in decided(find("R&D + Marketing",
                                                        "R&D + Marketing"))["claims"]], ["m"])
        self.assertEqual([c["id"] for c in decided(find(
            "R&D + Marketing", "R&D + Marketing", "R&D + Marketing Team",
            status="ambiguous"))["claims"]], ["m"])
        for finds in ([find("R&D + Sales Team", "R&D + Marketing")],
                      [find("R&D + Marketing", "R&D + Marketing Team")],
                      [find("R&D + Marketing", "R&D + Marketing", "R&D + Marketing",
                            status="ambiguous")],
                      [find("R&D + Marketing", "R&D + Marketing", status="not_found")]):
            with self.subTest(finds=finds[0][0]["query"]):
                self.assertEqual(self.reasons(decided(*finds)), [answer_gate.OFF_QUESTION])
        # A name shorter than MIN_NAME_ANCHOR_CHARS (dev 11-10 G-0018: the entity index
        # read "Gleamin" as a customer): only a find of the type whose noun the question
        # writes beside it connects it; an untyped find, another type's, or a question
        # writing the name inside a longer word connects nothing.
        short = "what is the last activity at of teams channel Gleamin in teams?"
        self.assertEqual([c["id"] for c in decided(
            find("Gleamin", "Gleamin", "Gleamin Prep", status="ambiguous",
                 object_type="TeamsChannel"), asked=short)["claims"]], ["m"])
        for finds, asked in (
                ([find("Gleamin", "Gleamin", object_type=None)], short),
                ([find("Gleamin", "Gleamin", object_type="PlannerTask")], short),
                ([find("Gleamin", "Gleamin", object_type="TeamsChannel")],
                 "what is the last activity of the Gleamins team channel?"),
                ([find("Gle", "Gle", object_type="TeamsChannel")],
                 "what is the last activity of teams channel Gle?")):
            with self.subTest(asked=asked, object_type=finds[0][0]["object_type"]):
                self.assertEqual(self.reasons(decided(*finds, asked=asked)),
                                 [answer_gate.OFF_QUESTION])

    def test_a_session_stopped_at_its_cap_keeps_its_best_gated_answer(self):
        """Stopped while its submission was being decided: that submission is the host's
        to decide (``cut_submission``) and the reply is what the gate passed
        (``decided_reply``), never empty; a decision that already passed stands."""
        from operating import agent_session
        tool = agent_session.ANSWER_GATE_TOOL
        answer = {"claims": [{"id": "q", "value": "40", "citation": self.LINE,
                              "field": "row.quantity"}]}
        cut = [{"seq": 0, "type": "tool_call", "name": "plx.find",
                "arguments": {"query": "PO0103510"}, "result": {"status": "found"}},
               {"seq": 1, "type": "tool_call", "name": tool,
                "arguments": {"answer": answer}, "result": None}]
        self.assertEqual(agent_session.cut_submission(cut), answer)
        self.assertEqual(agent_session.decided_reply(cut), "")
        passed = {"outcome": "answered", "claims": [{
            "id": "q", "check": {"status": "passed"}, "value_claims": [
                {"value": "40", "citation": self.LINE, "field": "row.quantity"}]}],
                  "answerability": {"state": "partial"}, "unknowns": []}
        done = cut + [agent_session._host_event(2, answer, {"result": {
            "content": [{"type": "text", "text": json.dumps(passed)}],
            "structuredContent": passed, "isError": False}})]
        self.assertIsNone(agent_session.cut_submission(done))
        self.assertTrue(agent_session.decided_reply(done).strip())

    def test_a_claim_needs_a_via_when_its_record_is_off_the_question(self):
        line = {"id": "q", "value": "40", "citation": self.LINE, "field": "row.quantity"}
        refused = self.gate(line)
        self.assertEqual(refused["claims"], [])
        self.assertEqual([u["reason"] for u in refused["unsupported"]],
                         [answer_gate.OFF_QUESTION])
        self.assertIn(answer_gate.OFF_QUESTION, refused["repair"]["instruction"])
        passed = self.gate({**line, "via": self.ORDER}, self.linked_world())
        self.assertEqual([c["id"] for c in passed["claims"]], ["q"])
        # the record the question names stands on its own; a via must be evidence
        order = {"id": "o", "value": "PO-0103510", "citation": self.ORDER,
                 "field": "row.po_number"}
        self.assertEqual([c["id"] for c in self.gate(order)["claims"]], ["o"])
        self.assertEqual(self.gate({**line, "via": "api://elsewhere/x@0-1"})["unsupported"][0][
            "reason"], "claim_invalid")


class TextCheckEnforcedTests(unittest.TestCase):
    """GATE-003/004: what the text check finds changes the answer, not only the
    instruction to the model. Evidence label ``isolated-fixture``: two rows served to the
    real gate (``mcp_server.answer_decision``) in place of a runtime's."""
    A = "api://public.app_purchase_order/%7B%22id%22%3A%221%22%7D@0-1"
    B = "api://public.app_purchase_order/%7B%22id%22%3A%222%22%7D@0-1"
    ROWS = {A: {"relation": "public.app_purchase_order",
                "row": {"po_number": "PO-1", "product_code": "R-0162",
                        "quantity_received": "40", "status": "Shipped"}},
            B: {"relation": "public.app_purchase_order",
                "row": {"po_number": "PO-2", "product_code": "R-0163"}}}
    CODE = {"id": "c", "value": "R-0162", "citation": A, "field": "row.product_code"}
    COUNT = {"id": "n", "value": "2", "op": "count", "of": [{"citation": A}, {"citation": B}]}

    def decide(self, claims, text):
        from operating import mcp_server
        view = lambda handle: {"handle": handle, "revision": "0-1",   # noqa: E731
                               **self.ROWS[handle]} if handle in self.ROWS else None
        server = mock.Mock(case_runtime=None, actor=None, returned_names=())
        with mock.patch.object(answer_gate, "citation_rows", lambda runtime, **_kw: None), \
                mock.patch.object(answer_gate, "gate_rows", lambda runtime, actor, **_kw: view):
            result, _gated, _repairing = mcp_server.answer_decision(
                {"claims": claims, "text": text}, server, seen={self.A, self.B},
                returned={}, may_repair=False, repairs_left=0, unread=None)
        return result

    def test_an_unsupported_business_assertion_is_removed_and_the_answer_partial(self):
        for said in ("The supplier has approved the refund.", "The shipment was delivered.",
                     "The invoice was paid.", "The order was cancelled.",
                     "Acme owns the stock."):
            with self.subTest(said=said):
                result = self.decide([self.CODE], "The product is R-0162. " + said)
                self.assertEqual(result["outcome"], "answered")
                self.assertEqual(result["answerability"]["state"], "partial")
                self.assertNotIn(said, result["reply"]["text"])
                self.assertIn("R-0162", result["reply"]["text"])
        # a state a passed claim is about stays, and the answer stays answerable
        status = {"id": "s", "value": "Shipped", "citation": self.A, "field": "row.status"}
        for said in ("The status is Shipped.", "The order was shipped."):
            result = self.decide([status], said)
            self.assertEqual(result["reply"]["text"], said)
            self.assertEqual(result["answerability"]["state"], "answerable")

    def test_a_lower_bound_count_is_never_an_exact_answerable_total(self):
        result = self.decide([self.COUNT], "There are 2 orders.")
        self.assertEqual(result["answerability"]["state"], "partial")
        self.assertNotIn("There are 2 orders.", result["reply"]["text"])
        self.assertIn("at least 2", result["reply"]["text"])
        self.assertEqual([value["answer_value"].get("bound")
                          for value in result["answer_values"]], ["at_least"])
        # the bound survives a value restated without it (``kept_value``)
        cell = answer_gate.passed_cells({"outcome": "answered", "claims": result["claims"]})
        restated = {"value": "2", "calculation": {"op": "count", "inputs": [
            {"l1_handle": self.A}, {"l1_handle": self.B}]}}
        self.assertEqual(answer_gate.kept_value(restated, cell)["bound"], "at_least")
        # GATE-005: restated over fewer, repeated or other inputs, it keeps the inputs
        # the gate checked it over
        for inputs in ([{"l1_handle": self.A}], [{"l1_handle": self.A}] * 2,
                       [{"l1_handle": self.B}, {"l1_handle": self.A}]):
            with self.subTest(inputs=inputs):
                kept = answer_gate.kept_value(
                    {"value": "2", "calculation": {"op": "count", "inputs": inputs}}, cell)
                self.assertEqual(kept["calculation"]["inputs"],
                                 [{"l1_handle": self.A}, {"l1_handle": self.B}])
        # written as the minimum it is, it stands
        result = self.decide([self.COUNT], "There are at least 2 orders.")
        self.assertIn("at least 2 orders", result["reply"]["text"])
        self.assertTrue(result["text_check"]["passed"])

    def test_removing_evidence_never_raises_the_answer_s_certainty(self):
        """The same text over fewer passed claims is never more
        certain (``CERTAINTY`` order), nor does it keep a sentence the fuller evidence
        dropped."""
        from operating.agent_session import CERTAINTY
        status = {"id": "s", "value": "Shipped", "citation": self.A, "field": "row.status"}
        text = "The product is R-0162. The order was shipped. There are 2 orders."
        full = [self.CODE, status, self.COUNT]
        rank = lambda result: CERTAINTY.index(  # noqa: E731
            (result.get("answerability") or {}).get("state") or "blocked")
        whole = self.decide(full, text)
        for drop in range(len(full)):
            fewer = self.decide(full[:drop] + full[drop + 1:], text)
            with self.subTest(dropped=full[drop]["id"]):
                self.assertGreaterEqual(rank(fewer), rank(whole))

            with self.subTest(dropped=full[drop]["id"], reply="sentences"):
                # nor keeps a sentence the fuller evidence dropped
                for gone in whole["reply"].get("dropped") or ():
                    self.assertNotIn(gone, fewer["reply"]["text"])

    def final(self, claims, text, values):
        """The agent Ask's last step (``agent_session.gate_final_reply``) over the gate's
        decision for ``claims``, the agent's final JSON answer ``text`` with ``values``."""
        from operating import agent_session
        result = self.decide(claims, text)
        answer = {"format": "plx-eval-answer/v1", "answerability": "answerable",
                  "text": text, "values": values, "gaps": []}
        events = [{"type": "tool_call", "name": "plx.case_get",
                   "arguments": {"answer": {"claims": claims, "text": text}},
                   "result": result}]
        gated, _report = agent_session.gate_final_reply(
            "```json\n" + json.dumps(answer) + "\n```", events)
        out, _error = agent_session.parse_answer(gated)
        return out

    def test_a_name_only_memory_returned_is_never_stated_as_read(self):
        """An identifier the reader's memory (``plx.memory``) recalled is what was said
        before, never this session's evidence: the final reply drops the sentence stating
        it, as it does with no memory at all; a source tool returning it backs it."""
        from operating import agent_session
        status = {"id": "s", "value": "Shipped", "citation": self.A, "field": "row.status"}
        text = "The order was shipped. It ships under lot LT-88213."
        result = self.decide([status], text)
        answer = {"format": "plx-eval-answer/v1", "answerability": "answerable",
                  "text": text, "values": [], "gaps": []}
        gate = {"type": "tool_call", "name": "plx.case_get",
                "arguments": {"answer": {"claims": [status], "text": text}}, "result": result}

        def reply(*more):
            gated, _ = agent_session.gate_final_reply(
                "```json\n" + json.dumps(answer) + "\n```", [*more, gate])
            return agent_session.parse_answer(gated)[0]["text"]
        recalled = {"type": "tool_call", "name": "plx.memory", "arguments": {"query": "lot"},
                    "result": {"ok": True, "items": [{"text": "Shipped under lot LT-88213."}]}}
        read = {**recalled, "name": "plx.find"}
        self.assertNotIn("LT-88213", reply())
        self.assertNotIn("LT-88213", reply(recalled))
        self.assertIn("LT-88213", reply(read))

    def test_the_final_reply_keeps_a_lower_bound_count_a_minimum(self):
        """GATE-004 end to end: the Ask's final reply never says an exact count the gate
        established only as a minimum; written as the minimum, it stands."""
        value = {"name": "orders", "value": "2", "calculation": {"op": "count", "inputs": [
            {"l1_handle": self.A}, {"l1_handle": self.B}]}}
        out = self.final([self.COUNT], "There are 2 orders.", [value])
        self.assertNotIn("There are 2 orders.", out["text"])
        self.assertIn("at least 2", out["text"])
        self.assertNotRegex(out["text"], r"(?<!least )\b2\b(?! *\+)")
        self.assertEqual(out["answerability"], "partial")
        self.assertEqual(out["values"][0].get("bound"), "at_least")
        out = self.final([self.COUNT], "There are at least 2 orders.", [value])
        self.assertIn("There are at least 2 orders.", out["text"])

    def test_the_final_reply_never_more_certain_on_fewer_claims(self):
        from operating.agent_session import CERTAINTY
        status = {"id": "s", "value": "Shipped", "citation": self.A, "field": "row.status"}
        text = "The product is R-0162. The order was shipped."
        values = [{"name": "product", "value": "R-0162",
                   "citations": [{"l1_handle": self.A, "field_path": "row.product_code"}]}]
        whole = self.final([self.CODE, status], text, values)
        fewer = self.final([self.CODE], text, values)
        self.assertGreaterEqual(CERTAINTY.index(fewer["answerability"]),
                                CERTAINTY.index(whole["answerability"]))
        self.assertNotIn("The order was shipped.", fewer["text"])


class HedgeKeptTests(unittest.TestCase):
    """A sentence saying a state is not established keeps the reply's uncertainty: the
    state-word check (GATE-003) never drops it. The sentences are from recorded Ask
    sessions (2026-11 replays of 641 sessions, words as the agent wrote them, curly
    apostrophes included) and the gap wording the agent is told to use
    (``operator_api/agent_ask.py``). Evidence label ``recorded-sessions``."""

    HEDGES = (
        "The batch record shows the lot, but I couldn\u2019t confirm that the batch shipped.",
        "I couldn't confirm the freight we paid on this order.",
        "Not yet checked: which lots QC rejected.",
        "I couldn't confirm a matching purchase order in Business Central that was approved.",
        "I haven't found a record that the invoice was paid.",
        "I didn't find any sign the order was cancelled.",
        "There is no source you can open that says it was delivered.",
        "The search returned nothing.",
        "Whether the customer paid is unknown.",
        "It may have shipped on Monday.",
        "Nothing in the records says the shipment was received.",
        "It wasn\u2019t possible to verify that the PO was closed.",
        "I couldn't confirm a ship or delivery date, or that customs has released the "
        "bottles, from a source you can open.",
        "Two things are not yet checked: which certificates of analysis came in during "
        "October, and which of those lots quality control rejected.",
        "My search for October certificates ran out of time before it returned anything.",
        "The November letter names no prices, so there are no figures in it to compare "
        "with what we paid.",
        "It prints no new prices, so I can't line it up figure by figure against what we "
        "paid.",
    )
    ASSERTED = (
        ("The order shipped, but whether it was paid is unknown.", ["shipped"]),
        ("The invoice was not paid.", ["paid"]),
        ("The order shipped in May.", ["shipped"]),
        ("I couldn't confirm the tracking number; the order was rejected.", ["rejected"]),
        ("I couldn't confirm the tracking number, but the order was rejected.", ["rejected"]),
    )

    def test_a_hedge_is_never_dropped_for_its_state_word(self):
        for sentence in self.HEDGES:
            with self.subTest(sentence=sentence):
                self.assertEqual(answer_gate.unbacked_assertions(sentence, frozenset()), [])
                kept = answer_gate.final_reply("The product is R-0162. " + sentence, [])
                self.assertNotIn(sentence.replace("\u2019", "'"),
                                 [d.replace("\u2019", "'") for d in kept.get("dropped") or ()])

    def test_the_hedge_governs_its_own_clause_and_a_negated_state_is_asserted(self):
        for sentence, words in self.ASSERTED:
            with self.subTest(sentence=sentence):
                self.assertEqual(answer_gate.unbacked_assertions(sentence, frozenset()), words)


if __name__ == "__main__":
    unittest.main()
